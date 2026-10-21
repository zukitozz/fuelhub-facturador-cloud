"""
Reintento automático de los comprobantes que fallaron por un motivo transitorio
(SUNAT sin servicio, corte de red; ver app/errores.py), con un tope para no insistir
para siempre:

    ronda 1:  hasta INTENTOS_POR_RONDA reenvíos, uno cada ESPERA_ENTRE_INTENTOS_SEG
    pausa de PAUSA_ENTRE_RONDAS_SEG (un día)
    ronda 2, pausa, ronda 3 ...
    agotadas las RONDAS (3): el comprobante queda con su último estado, en 'manual'.

Un fallo definitivo (monto mal calculado, dato inválido...) no se reintenta: pasa directo
a 'manual', porque reenviar lo mismo da lo mismo. Antes de CADA reenvío se le pregunta a
SUNAT si ya tiene el comprobante (reenvio.preparar): si lo tiene, se da por resuelto y no
se manda de nuevo.

Cómo se entera de los fallos: no depende de quién envió el comprobante. Cada ciclo mira la
bandeja de SFS de cada empresa (documentos en '06'/'10') y lleva la cuenta en su propia
SQLite (FACTURADOR_API_REINTENTOS_BD; sin configurar, reintentos.db en la raíz del repo,
ignorado por git igual que rechazos.jsonl). SFS sobrescribe su estado, esta cuenta no.

Estados de cada seguimiento: activo (se sigue reintentando), manual (hay que intervenir),
resuelto (aceptado, o SUNAT ya lo tenía). Se consultan en GET /empresas/{ruc}/reintentos.

También resuelve los tickets de los resúmenes diarios (RC) que SUNAT recibió y SFS dejó en
'08': sin esto solo se cerraban si alguien consultaba el RC con GET (ver
app/ticket_resumen.py). Misma política de rondas, pero mirando cada ESPERA_RESUMEN_SEG.

Necesita los archivos del comprobante en DATA (fecha y total para preguntarle a SUNAT, y
que SFS pueda volver a leerlo); sin ellos pasa a 'manual': hay que reenviarlo desde el
sistema de origen.
"""
import logging
import os
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone

from . import archivos, consulta_integrada, consultas, errores, rechazos, reenvio, sfs_cliente, ticket_resumen

logger = logging.getLogger(__name__)

_RAIZ_PROYECTO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LIMA = timezone(timedelta(hours=-5))


def _entero_de_entorno(nombre: str, defecto: int) -> int:
    try:
        return int(os.getenv(nombre, defecto))
    except ValueError:
        return defecto


INTENTOS_POR_RONDA = _entero_de_entorno("FACTURADOR_API_REINTENTOS_POR_RONDA", 5)
ESPERA_ENTRE_INTENTOS_SEG = _entero_de_entorno("FACTURADOR_API_REINTENTOS_ESPERA_SEG", 10 * 60)
PAUSA_ENTRE_RONDAS_SEG = _entero_de_entorno("FACTURADOR_API_REINTENTOS_PAUSA_SEG", 24 * 60 * 60)
RONDAS = _entero_de_entorno("FACTURADOR_API_REINTENTOS_RONDAS", 3)
# Un resumen diario (RC) no se "reenvía": SUNAT ya lo recibió y solo falta leer su ticket.
# Suele estar listo en 1-2 minutos, por eso se mira más seguido que un reenvío.
ESPERA_RESUMEN_SEG = _entero_de_entorno("FACTURADOR_API_REINTENTOS_ESPERA_RESUMEN_SEG", 2 * 60)
TIPO_RESUMEN = "RC"
# Días desde la emisión durante los que SUNAT recibe el comprobante. Pasado el plazo, si
# SUNAT no lo tiene, ya no hay forma de enviarlo: se da por anulado.
PLAZO_ENVIO_DIAS = _entero_de_entorno("FACTURADOR_API_PLAZO_ENVIO_DIAS", 7)

ACTIVO, MANUAL, RESUELTO, ANULADO = "activo", "manual", "resuelto", "anulado"

# Estados de SFS en los que el comprobante dejó de estar en falla: aceptado, enviado,
# anulado o validándose. Hasta acá no hay nada más que reintentar.
_ESTADOS_SALIDOS = {"03", "04", "05", "07", "08", "09", "11", "12"}
# Un reenvío que quedó en '01'/'02' no avanzó (SFS no lo firmó o no lo envió): se cuenta
# como intento fallido, no como definitivo.
_ESTADOS_SIN_AVANZAR = {"01", "02"}

_COLUMNAS = ("ruc", "tipo", "numero", "estado", "ronda", "intentos", "proximo_intento",
             "primer_fallo", "ultimo_intento", "ultimo_error", "motivo")

_lock_bd = threading.Lock()
_bloqueos: dict[tuple, threading.Lock] = {}
_lock_bloqueos = threading.Lock()


def _ruta() -> str:
    return os.getenv("FACTURADOR_API_REINTENTOS_BD", os.path.join(_RAIZ_PROYECTO, "reintentos.db"))


def _conectar() -> sqlite3.Connection:
    ruta = _ruta()
    os.makedirs(os.path.dirname(ruta) or ".", exist_ok=True)
    conexion = sqlite3.connect(ruta, timeout=10)
    conexion.row_factory = sqlite3.Row
    conexion.execute(
        "CREATE TABLE IF NOT EXISTS reintentos ("
        "ruc TEXT NOT NULL, tipo TEXT NOT NULL, numero TEXT NOT NULL, estado TEXT NOT NULL, "
        "ronda INTEGER NOT NULL, intentos INTEGER NOT NULL, proximo_intento INTEGER, "
        "primer_fallo INTEGER NOT NULL, ultimo_intento INTEGER, ultimo_error TEXT, motivo TEXT, "
        "PRIMARY KEY (ruc, tipo, numero))"
    )
    return conexion


def _ahora() -> int:
    return int(time.time())


@contextmanager
def bloqueo(ruc: str, codigo: str, esperar: bool = True):
    """Un solo envío a la vez por comprobante: el POST de quien factura y el reintento
    automático no deben mandar el mismo a SUNAT a la vez. Con esperar=False cede (False)
    si ya hay otro enviándolo."""
    clave = (ruc, codigo)
    with _lock_bloqueos:
        candado = _bloqueos.setdefault(clave, threading.Lock())
    obtenido = candado.acquire(blocking=esperar)
    try:
        yield obtenido
    finally:
        if obtenido:
            candado.release()


def _guardar(fila: dict):
    with _lock_bd, closing(_conectar()) as conexion:
        conexion.execute(
            f"INSERT OR REPLACE INTO reintentos ({', '.join(_COLUMNAS)}) VALUES ({', '.join('?' * len(_COLUMNAS))})",
            [fila.get(c) for c in _COLUMNAS],
        )
        conexion.commit()


def _seleccionar(sql: str, parametros: tuple) -> list[dict]:
    with _lock_bd, closing(_conectar()) as conexion:
        return [dict(r) for r in conexion.execute(sql, parametros).fetchall()]


def _a_manual(fila: dict, motivo: str, error: str):
    fila.update(estado=MANUAL, proximo_intento=None, motivo=motivo, ultimo_error=error)
    _guardar(fila)
    logger.warning("%s %s-%s pasa a intervención manual: %s", fila["ruc"], fila["tipo"], fila["numero"], motivo)
    rechazos.registrar(fila["ruc"], f"{fila['tipo']}-{fila['numero']}", "error", error, "reintento",
                       detalle={"motivo": motivo, "ronda": fila["ronda"], "intentos": fila["intentos"]})


def _a_resuelto(fila: dict, motivo: str, error: str | None = None):
    fila.update(estado=RESUELTO, proximo_intento=None, motivo=motivo)
    if error is not None:
        fila["ultimo_error"] = error   # el que sigue en SFS: sirve para no reabrirlo (ver observar)
    _guardar(fila)
    logger.info("%s %s-%s resuelto: %s", fila["ruc"], fila["tipo"], fila["numero"], motivo)


def _intento_fallido(fila: dict, error: str):
    """Cuenta el intento y agenda el siguiente: dentro de la ronda, el siguiente tras la
    espera corta; al terminar la ronda, la pausa larga y la ronda siguiente; al terminar
    la última, intervención manual."""
    ahora = _ahora()
    fila.update(intentos=fila["intentos"] + 1, ultimo_intento=ahora, ultimo_error=error)
    if fila["intentos"] < INTENTOS_POR_RONDA:
        espera = ESPERA_RESUMEN_SEG if fila["tipo"] == TIPO_RESUMEN else ESPERA_ENTRE_INTENTOS_SEG
        fila["proximo_intento"] = ahora + espera
        _guardar(fila)
    elif fila["ronda"] < RONDAS:
        fila.update(ronda=fila["ronda"] + 1, intentos=0, proximo_intento=ahora + PAUSA_ENTRE_RONDAS_SEG)
        _guardar(fila)
        logger.info("%s %s-%s: ronda agotada, se pausa hasta la ronda %s",
                    fila["ruc"], fila["tipo"], fila["numero"], fila["ronda"])
    else:
        _a_manual(fila, f"agotó {RONDAS} rondas de {INTENTOS_POR_RONDA} intentos", error)


def _nuevo(ruc: str, d: dict) -> dict:
    ahora = _ahora()
    espera = ESPERA_RESUMEN_SEG if d["tipo"] == TIPO_RESUMEN else ESPERA_ENTRE_INTENTOS_SEG
    return {"ruc": ruc, "tipo": d["tipo"], "numero": d["numero"], "estado": ACTIVO, "ronda": 1,
            "intentos": 0, "proximo_intento": ahora + espera, "primer_fallo": ahora,
            "ultimo_intento": None, "ultimo_error": d["des_obse"], "motivo": None}


def _atender_vencido(empresa, d: dict, previo: dict | None) -> bool:
    """Si el plazo de envío del comprobante ya venció, lo resuelve y devuelve True: SUNAT ya
    lo tiene (resuelto) o no lo tiene y entonces se anula. Si no venció, o SUNAT no pudo
    confirmar, devuelve False y sigue el flujo normal (que no reenvía a ciegas)."""
    ruc, tipo, numero = empresa.ruc, d["tipo"], d["numero"]
    cabecera = archivos.leer_fecha_y_total(empresa.data_dir, ruc, tipo, numero)
    if cabecera is None:
        return False
    fecha, total = cabecera
    hoy = datetime.fromtimestamp(_ahora(), _LIMA).date()
    dias = (hoy - fecha.date()).days
    if dias <= PLAZO_ENVIO_DIAS:
        return False

    serie, correlativo = numero.split("-", 1) if "-" in numero else ("0000", numero)
    estado_sunat, detalle = consulta_integrada.consultar_comprobante(
        empresa, ruc, tipo, serie, correlativo, fecha, total)
    fila = previo or _nuevo(ruc, d)
    if estado_sunat in (consulta_integrada.ACEPTADO, consulta_integrada.ANULADO):
        _a_resuelto(fila, f"SUNAT ya lo tiene ({estado_sunat}: {detalle})", d["des_obse"])
        return True
    if estado_sunat != consulta_integrada.NO_EXISTE:
        return False   # sin confirmación de SUNAT no se anula nada
    with bloqueo(ruc, f"{tipo}-{numero}", esperar=False) as libre:
        if not libre:
            return True   # lo está enviando otro; se ve en el próximo ciclo
        motivo = (f"Anulado: venció el plazo de {PLAZO_ENVIO_DIAS} días para enviarlo a SUNAT "
                  f"(emitido el {fecha:%d/%m/%Y}, {dias} días) y SUNAT no lo tiene")
        if not consultas.anular_documento(empresa.bd_path, ruc, tipo, numero, motivo):
            return False
        fila.update(estado=ANULADO, proximo_intento=None, motivo=motivo, ultimo_error=d["des_obse"])
        _guardar(fila)
        logger.warning("%s %s-%s: %s", ruc, tipo, numero, motivo)
        rechazos.registrar(ruc, f"{tipo}-{numero}", "anulado", d["des_obse"], "reintento",
                           detalle={"motivo": motivo, "dias": dias})
    return True


def _debe_alta(previo: dict | None, d: dict) -> bool:
    """¿Hay que (re)abrir el seguimiento de este fallo? Sí si no se sigue; también si se dio
    por resuelto y volvió a fallar, salvo cuando se resolvió porque SUNAT ya lo tenía y SFS
    conserva la fila vieja con el mismo error."""
    if previo is None:
        return True
    if previo["estado"] != RESUELTO:
        return False
    sunat_ya_lo_tenia = (previo["motivo"] or "").startswith("SUNAT ya lo tiene")
    return not (sunat_ya_lo_tenia and previo["ultimo_error"] == d["des_obse"])


def observar(empresa) -> None:
    """Pone al día el seguimiento con lo que SFS tiene hoy: da de alta los fallos nuevos
    (transitorio -> activo, definitivo -> manual) y cierra los que ya no están en falla."""
    ruc = empresa.ruc
    fallidos = consultas.documentos_fallidos(empresa.bd_path, ruc)
    en_falla = {(d["tipo"], d["numero"]) for d in fallidos}
    for d in fallidos:
        previo = _seleccionar("SELECT * FROM reintentos WHERE ruc=? AND tipo=? AND numero=?",
                              (ruc, d["tipo"], d["numero"]))
        previo = previo[0] if previo else None
        if previo and previo["estado"] == RESUELTO and not _debe_alta(previo, d):
            continue   # SUNAT ya lo tenía y SFS conserva la fila vieja
        if _atender_vencido(empresa, d, previo):
            continue
        if not _debe_alta(previo, d):
            continue   # ya se sigue (activo o manual)
        fila = _nuevo(ruc, d)
        if errores.es_transitorio(d["des_obse"]):
            _guardar(fila)
            logger.info("%s %s-%s: falla transitoria (%s); se reintentará solo.",
                        ruc, d["tipo"], d["numero"], d["des_obse"][:100])
        else:
            _a_manual(fila, "error definitivo: reenviarlo igual no lo arregla", d["des_obse"])
    for fila in _seleccionar("SELECT * FROM reintentos WHERE ruc=? AND tipo<>? AND estado IN (?,?)",
            (ruc, TIPO_RESUMEN, ACTIVO, MANUAL)):
        if (fila["tipo"], fila["numero"]) in en_falla:
            continue
        actual = consultas.consultar_estado(empresa.bd_path, ruc, fila["tipo"], fila["numero"])
        # Ausente no es resuelto: reenvio.preparar borra la fila un instante antes de regenerarla.
        # Solo un estado final: en '01'/'02' puede ser un reenvío en curso (el POST regenera la fila).
        if actual is not None and actual["ind_situ"] in _ESTADOS_SALIDOS:
            _a_resuelto(fila, f"ya no está en error (estado SFS {actual['ind_situ']})")


def observar_resumenes(empresa) -> None:
    """Toma los resúmenes diarios enviados a SUNAT cuyo ticket nadie ha resuelto (SFS los
    deja en '08') y cierra el seguimiento de los que ya se cerraron por otro camino."""
    ruc = empresa.ruc
    abiertos = set(ticket_resumen.resumenes_abiertos(empresa.bd_path, ruc))
    for numero in sorted(abiertos):
        if not _seleccionar("SELECT 1 FROM reintentos WHERE ruc=? AND tipo=? AND numero=?",
                            (ruc, TIPO_RESUMEN, numero)):
            _guardar(_nuevo(ruc, {"tipo": TIPO_RESUMEN, "numero": numero, "des_obse": ""}))
            logger.info("%s %s-%s: resumen con ticket sin resolver; se consultará solo.", ruc, TIPO_RESUMEN, numero)
    for fila in _seleccionar("SELECT * FROM reintentos WHERE ruc=? AND tipo=? AND estado IN (?,?)",
                             (ruc, TIPO_RESUMEN, ACTIVO, MANUAL)):
        if fila["numero"] not in abiertos:
            _a_resuelto(fila, "el resumen ya no está abierto en SFS")


def _resolver_resumen(empresa, fila: dict) -> None:
    diagnostico = ticket_resumen.resolver_ticket_pendiente(empresa, fila["ruc"], fila["numero"])
    if diagnostico is None:
        _a_resuelto(fila, "el resumen ya no tiene ticket abierto")
    elif diagnostico.startswith(ticket_resumen.TICKET_CERRADO):
        fila.update(intentos=fila["intentos"] + 1, ultimo_intento=_ahora())
        _a_resuelto(fila, diagnostico)
    elif diagnostico.startswith(ticket_resumen.TICKET_PERDIDO):
        _a_manual(fila, "SUNAT ya no conoce el ticket: revisar el resumen en el portal", diagnostico)
    else:
        _intento_fallido(fila, diagnostico)


def _reintentar(empresa, fila: dict) -> None:
    ruc, tipo, numero = fila["ruc"], fila["tipo"], fila["numero"]
    if tipo == TIPO_RESUMEN:
        _resolver_resumen(empresa, fila)
        return
    cabecera = archivos.leer_fecha_y_total(empresa.data_dir, ruc, tipo, numero)
    if cabecera is None:
        _a_manual(fila, "faltan los archivos del comprobante en DATA; reenviarlo desde el sistema de origen",
                  fila["ultimo_error"] or "")
        return
    fecha, total = cabecera

    accion, detalle = reenvio.preparar(empresa, ruc, tipo, numero, fecha, total)
    if accion == reenvio.YA_EN_SUNAT:
        en_sfs = consultas.consultar_estado(empresa.bd_path, ruc, tipo, numero)
        _a_resuelto(fila, f"SUNAT ya lo tiene ({detalle})", en_sfs["des_obse"] if en_sfs else None)
        return
    if accion == reenvio.INDETERMINADO:
        _intento_fallido(fila, f"no se pudo verificar en SUNAT: {detalle}")
        return

    try:
        resultado = sfs_cliente.generar_y_enviar(empresa.sfs_base_url, ruc, tipo, numero)
    except sfs_cliente.SfsError as e:
        _intento_fallido(fila, f"SFS no disponible: {e}")
        return

    ind_situ, des_obse = resultado.get("ind_situ", ""), resultado.get("des_obse", "")
    if ind_situ in _ESTADOS_SALIDOS:
        fila.update(intentos=fila["intentos"] + 1, ultimo_intento=_ahora())
        _a_resuelto(fila, f"reenviado con éxito (estado SFS {ind_situ})")
    elif ind_situ in _ESTADOS_SIN_AVANZAR or errores.es_transitorio(des_obse):
        _intento_fallido(fila, des_obse or f"quedó en estado {ind_situ} sin enviarse")
    else:
        fila.update(intentos=fila["intentos"] + 1, ultimo_intento=_ahora())
        _a_manual(fila, "SUNAT lo rechazó por contenido: reenviarlo igual no lo arregla", des_obse)


def procesar_vencidos(empresa) -> int:
    """Reintenta los seguimientos activos de la empresa cuya hora ya llegó. Devuelve
    cuántos intentó."""
    hechos = 0
    vencidos = _seleccionar("SELECT * FROM reintentos WHERE ruc=? AND estado=? AND proximo_intento<=?",
                            (empresa.ruc, ACTIVO, _ahora()))
    for fila in vencidos:
        with bloqueo(empresa.ruc, f"{fila['tipo']}-{fila['numero']}", esperar=False) as libre:
            if not libre:
                continue   # lo está enviando el POST de quien factura; se ve en el próximo ciclo
            try:
                _reintentar(empresa, fila)
                hechos += 1
            except Exception:
                logger.exception("Falló el reintento de %s %s-%s", empresa.ruc, fila["tipo"], fila["numero"])
    return hechos


def ciclo_empresa(empresa) -> None:
    """Un ciclo de reintentos de una empresa (lo invoca app/ciclo.py cada minuto)."""
    observar(empresa)
    observar_resumenes(empresa)
    procesar_vencidos(empresa)


def registrar_anulado(ruc: str, tipo: str, numero: str, motivo: str, error: str = "") -> None:
    """Deja constancia de un comprobante anulado por otro camino (p.ej. una boleta vencida)."""
    fila = _nuevo(ruc, {"tipo": tipo, "numero": numero, "des_obse": error})
    fila.update(estado=ANULADO, proximo_intento=None, motivo=motivo)
    _guardar(fila)
    rechazos.registrar(ruc, f"{tipo}-{numero}", "anulado", error, "reintento", detalle={"motivo": motivo})


def registrar_manual(ruc: str, tipo: str, numero: str, motivo: str, error: str = "") -> None:
    """Deja un comprobante (o resumen) para intervención manual desde fuera de este módulo."""
    _a_manual(_nuevo(ruc, {"tipo": tipo, "numero": numero, "des_obse": error}), motivo, error)


def _iso(epoch):
    return datetime.fromtimestamp(epoch, _LIMA).isoformat(timespec="seconds") if epoch else None


def listar(ruc: str, estado: str | None = None) -> list[dict]:
    if estado:
        filas = _seleccionar("SELECT * FROM reintentos WHERE ruc=? AND estado=? ORDER BY primer_fallo DESC", (ruc, estado))
    else:
        filas = _seleccionar("SELECT * FROM reintentos WHERE ruc=? ORDER BY primer_fallo DESC", (ruc,))
    return [{
        "codigo": f"{f['tipo']}-{f['numero']}",
        "estado": f["estado"],
        "ronda": f["ronda"],
        "intentos_en_la_ronda": f["intentos"],
        "primer_fallo": _iso(f["primer_fallo"]),
        "ultimo_intento": _iso(f["ultimo_intento"]),
        "proximo_intento": _iso(f["proximo_intento"]),
        "ultimo_error": f["ultimo_error"],
        "motivo": f["motivo"],
    } for f in filas]
