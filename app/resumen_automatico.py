"""
Envío automático del resumen diario de boletas, una vez por día, al terminar el día.

Antes lo disparaba cada sistema consumidor con POST /empresas/{ruc}/resumenes-diarios.
Ahora lo hace la API sola, para todas las empresas dadas de alta, pasada la medianoche de
Lima (FACTURADOR_API_RESUMEN_HORA, por defecto 00:10): las boletas "de días anteriores" son
justo las del día que terminó. El endpoint sigue existiendo y es compatible: si alguien lo
llama de más, solo responde que no hay nada que resumir (un candado por empresa evita que
se crucen).

Qué hace cada corrida de una empresa:
  1. Boletas pendientes cuyo plazo de envío venció (PLAZO_ENVIO_DIAS, 7) se anulan: un RC
     declara una sola fecha de referencia, y SUNAT rechaza el resumen entero si esa fecha
     está fuera de plazo; una boleta vencida no se puede salvar, así que no arrastra a las
     demás. Quedan anuladas en SFS y en /reintentos (estado anulado).
  2. Se arma y envía un resumen por cada fecha pendiente, hasta que no queden.
  3. Un resumen que SFS deja en error o rechazado queda en /reintentos como 'manual', con
     las boletas que declaraba. El ticket de los enviados lo resuelve app/reintentos.py.
  4. Qué boletas entraron en cada resumen queda en app/registro_resumenes.py, y se consulta
     con GET /empresas/{ruc}/resumenes o con el GET de la propia boleta.

Si la corrida falla (SFS caído, SFS dejó el RC en error), se repite con la misma política que
los reintentos: cada ESPERA_ENTRE_INTENTOS_SEG, hasta INTENTOS_POR_RONDA veces ese día; si
no, queda para intervención manual y mañana se corre de nuevo.
"""
import logging
import os
from datetime import date, datetime, time as hora_del_dia

from . import archivos, consultas, rechazos, reintentos, registro_resumenes, resumenes, sfs_cliente
from .reintentos import bloqueo

logger = logging.getLogger(__name__)

HORA_TEXTO = os.getenv("FACTURADOR_API_RESUMEN_HORA", "00:10")
MAX_RESUMENES_POR_CORRIDA = 10   # fechas distintas pendientes: sobra, es un freno

HECHA, REINTENTANDO, MANUAL = "hecha", "reintentando", "manual"


def _hora() -> hora_del_dia:
    try:
        h, m = HORA_TEXTO.split(":")
        return hora_del_dia(int(h), int(m))
    except ValueError:
        return hora_del_dia(0, 10)


def _anular_vencidas(empresa, hoy: date) -> list[str]:
    """Anula las boletas pendientes que ya no se pueden declarar. Devuelve sus números."""
    ruc = empresa.ruc
    anuladas = []
    for numero in consultas.boletas_pendientes_resumen(empresa.bd_path, ruc):
        cab = archivos.leer_cabecera_boleta(empresa.data_dir, ruc, numero)
        if cab is None:
            continue
        try:
            emision = date.fromisoformat(cab["fecha_emision"])
        except ValueError:
            continue
        dias = (hoy - emision).days
        if dias <= reintentos.PLAZO_ENVIO_DIAS:
            continue
        motivo = (f"Anulada: venció el plazo de {reintentos.PLAZO_ENVIO_DIAS} días para declararla a SUNAT "
                  f"(emitida el {emision:%d/%m/%Y}, {dias} días) y no entró en ningún resumen")
        if consultas.anular_documento(empresa.bd_path, ruc, "03", numero, motivo, estados=("02",)):
            reintentos.registrar_anulado(ruc, "03", numero, motivo)
            logger.warning("%s 03-%s: %s", ruc, numero, motivo)
            anuladas.append(numero)
    return anuladas


def _correr(empresa, hoy: date) -> tuple[list[dict], list[str], str | None]:
    """(resúmenes enviados, boletas anuladas, motivo de fallo o None)."""
    ruc = empresa.ruc
    anuladas = _anular_vencidas(empresa, hoy)
    enviados = []
    for _ in range(MAX_RESUMENES_POR_CORRIDA):
        try:
            resultado = resumenes.armar_y_enviar_resumen(empresa, ruc)
        except resumenes.SinPendientes:
            break
        enviados.append(resultado)
        if resultado["estado"] in rechazos.ESTADOS_RECHAZO:
            rechazos.registrar(ruc, resultado["numeracion_rc"], resultado["estado"], resultado["des_obse"],
                               "resumen", detalle={"boletas": resultado["boletas"]},
                               descripcion_error=resultado["descripcion_error"])
            reintentos.registrar_manual(
                ruc, "RC", resultado["numeracion_rc"],
                f"el resumen quedó en '{resultado['estado']}'; boletas: {', '.join(resultado['boletas'])}",
                resultado["des_obse"])
            if resultado["estado"] == "error":
                # SFS no lo envió y las boletas siguen pendientes: se vuelve a intentar.
                return enviados, anuladas, f"el resumen {resultado['numeracion_rc']} quedó en error: {resultado['des_obse']}"
    return enviados, anuladas, None


def ciclo_empresa(empresa) -> None:
    ruc = empresa.ruc
    ahora = datetime.fromtimestamp(reintentos._ahora(), reintentos._LIMA)
    if ahora.time() < _hora():
        return
    fecha = ahora.date().isoformat()
    previa = registro_resumenes.corrida(ruc, fecha)
    if previa and (previa["estado"] in (HECHA, MANUAL) or (previa["proximo"] or 0) > reintentos._ahora()):
        return

    with bloqueo(ruc, "RESUMEN", esperar=False) as libre:
        if not libre:
            return
        intentos = (previa["intentos"] if previa else 0) + 1
        try:
            enviados, anuladas, fallo = _correr(empresa, ahora.date())
        except (sfs_cliente.SfsError, RuntimeError) as e:
            enviados, anuladas, fallo = [], [], str(e)
        except Exception as e:
            logger.exception("Falló el resumen automático de %s", ruc)
            enviados, anuladas, fallo = [], [], f"{type(e).__name__}: {e}"

        if fallo is None:
            registro_resumenes.guardar_corrida(
                ruc, fecha, HECHA, intentos, None,
                f"{len(enviados)} resumen(es), {sum(r['cantidad_boletas'] for r in enviados)} boletas, "
                f"{len(anuladas)} anulada(s) por plazo")
            if enviados or anuladas:
                logger.info("%s resumen diario: %s enviado(s), %s boleta(s) anulada(s) por plazo",
                            ruc, len(enviados), len(anuladas))
        elif intentos >= reintentos.INTENTOS_POR_RONDA:
            registro_resumenes.guardar_corrida(ruc, fecha, MANUAL, intentos, None, fallo)
            logger.error("%s resumen diario: %s intentos fallidos hoy, requiere intervención: %s", ruc, intentos, fallo)
            reintentos.registrar_manual(ruc, "RC", f"AUTOMATICO-{fecha}",
                                        f"el envío automático del resumen falló {intentos} veces", fallo)
        else:
            registro_resumenes.guardar_corrida(
                ruc, fecha, REINTENTANDO, intentos, reintentos._ahora() + reintentos.ESPERA_ENTRE_INTENTOS_SEG, fallo)
            logger.warning("%s resumen diario falló (intento %s): %s", ruc, intentos, fallo)

    # Resúmenes enviados antes de que existiera el registro: se reconstruyen de su .RDI.
    try:
        registro_resumenes.reconstruir(empresa, consultas.numeraciones_resumenes(empresa.bd_path, ruc))
    except Exception:
        logger.exception("No se pudo reconstruir el registro de resúmenes de %s", ruc)
