"""
Guías de remisión electrónicas (GRE remitente `09` y transportista `31`).

Camino distinto al de facturas y notas: SFS 2.1 no sabe de la `31` y su formato de `09` es el
anterior a la API REST de SUNAT, así que acá el servicio arma el XML UBL 2.1
(dominio/guia_xml.py), lo firma con el .p12 de la empresa (app/firma_xml.py) y lo envía a la
API de guías de SUNAT (app/gre_cliente.py). El estado se guarda en app/registro_guias.py.

Reglas que se mantienen respecto de las facturas:
  - quien emite manda la numeración; reintentar es reenviar el MISMO número;
  - lo ya aceptado (o anulado) no se reenvía: se devuelve lo registrado;
  - lo que quedó "enviado" (ticket en proceso) no se reenvía: se vuelve a preguntar por el ticket;
  - el éxito se decide por `estado`, no por el HTTP.

Estados: pendiente (firmada, aún sin ticket) | enviado (ticket en proceso) | aceptado |
aceptado_con_observaciones | rechazado | error (no se pudo hablar con SUNAT) | anulado (plazo).
"""
import logging
import re
import time
from datetime import datetime, timedelta

from . import firma_xml, gre_cliente, registro_guias, rechazos
from .configuracion import Empresa
from .esquemas import ComprobanteAceptado, EstadoComprobante, GuiaEntrada
from dominio import guia_xml

logger = logging.getLogger(__name__)

TIPOS = ("09", "31")
PLAZO_DIAS = 3
# Cuánto se espera el veredicto de SUNAT dentro del mismo POST antes de devolver "enviado".
ESPERA_TICKET_SEG = 3.0
INTENTOS_TICKET = 2

_NUMERACION = re.compile(r"^[A-Z0-9]{4}-\d{1,8}$")
_TERMINALES = {"aceptado", "aceptado_con_observaciones", "anulado"}


class GuiaInvalida(ValueError):
    """El payload no cumple las reglas de la guía: responsabilidad de quien llama (422)."""


class GuiaNoEnviable(Exception):
    """No se pudo completar el envío por algo del servicio o de SUNAT, no del payload (502/409)."""

    def __init__(self, mensaje: str, http: int = 502):
        super().__init__(mensaje)
        self.http = http


def es_guia(codigo: str) -> bool:
    return codigo[:3] in ("09-", "31-")


def _hora_lima() -> datetime:
    return datetime.now(rechazos._LIMA).replace(tzinfo=None)


def validar(g: GuiaEntrada, ruc: str) -> None:
    """Reglas que el esquema no puede expresar. GuiaInvalida con el motivo."""
    if not _NUMERACION.match(g.numeracion):
        raise GuiaInvalida("numeracion debe ser SERIE-CORRELATIVO, ej. T001-000001 (serie de 4 caracteres).")
    inicial = "T" if g.tipo_guia == "09" else "V"
    if not g.numeracion.startswith(inicial):
        raise GuiaInvalida(
            f"La serie de la {'GRE remitente (09)' if g.tipo_guia == '09' else 'GRE transportista (31)'} "
            f"debe empezar con '{inicial}'.")
    if not g.items:
        raise GuiaInvalida("La guía requiere al menos un ítem.")
    if g.peso_bruto <= 0:
        raise GuiaInvalida("peso_bruto debe ser mayor a 0.")
    if g.guia_remitente and not _NUMERACION.match(g.guia_remitente):
        raise GuiaInvalida("guia_remitente debe ser SERIE-CORRELATIVO, ej. T001-000123.")
    if not g.remitente or not g.destinatario:
        raise GuiaInvalida("La guía requiere 'remitente' y 'destinatario'.")

    if g.tipo_guia == "09":
        if not g.motivo_traslado:
            raise GuiaInvalida("La GRE remitente requiere 'motivo_traslado' (catálogo 20).")
        if g.modalidad_traslado not in ("01", "02"):
            raise GuiaInvalida("La GRE remitente requiere 'modalidad_traslado' '01' (público) o '02' (privado).")
        if g.motivo_traslado == "02" and not g.proveedor:
            raise GuiaInvalida("Con motivo_traslado '02' (compra) la guía requiere 'proveedor'.")
        # El emisor de la 09 es el remitente: si no coincide, SUNAT la rechaza.
        if g.remitente.numero_documento != ruc:
            raise GuiaInvalida(f"En la GRE remitente, remitente.numero_documento debe ser el RUC emisor ({ruc}).")
        if g.modalidad_traslado == "01" and not g.transportista:
            raise GuiaInvalida("Con modalidad_traslado '01' la guía requiere 'transportista'.")
        if g.modalidad_traslado == "02" and (not g.conductor or not g.vehiculo):
            raise GuiaInvalida("Con modalidad_traslado '02' la guía requiere 'conductor' y 'vehiculo'.")
    else:
        if not g.registro_mtc:
            raise GuiaInvalida("La GRE transportista requiere 'registro_mtc'.")
        if not g.conductor or not g.vehiculo:
            raise GuiaInvalida("La GRE transportista requiere 'conductor' y 'vehiculo'.")


def _credenciales(empresa: Empresa) -> tuple[str, str, str, str]:
    faltan = [n for n, v in (("sol_client_id", empresa.sol_client_id), ("sol_client_secret", empresa.sol_client_secret),
                             ("sol_usuario", empresa.sol_usuario), ("sol_clave", empresa.sol_clave)) if not v]
    if faltan:
        raise GuiaNoEnviable(f"La empresa {empresa.ruc} no tiene configurado para guías: {', '.join(faltan)}.", http=409)
    return empresa.sol_client_id, empresa.sol_client_secret, empresa.sol_usuario, empresa.sol_clave


def _token(empresa: Empresa, forzar: bool = False) -> str:
    cid, secreto, usuario, clave = _credenciales(empresa)
    try:
        return gre_cliente.obtener_token(empresa.ruc, cid, secreto, usuario, clave, forzar=forzar)
    except gre_cliente.GreError as e:
        # Sin respuesta es de red; con respuesta, son credenciales que SUNAT no aceptó.
        raise GuiaNoEnviable(str(e), http=502 if e.red else 409) from e


def _con_token(empresa: Empresa, operacion):
    """Corre operacion(token); si SUNAT dice 401 (token vencido) renueva y repite una vez."""
    try:
        return operacion(_token(empresa))
    except gre_cliente.GreError as e:
        if e.http != 401:
            raise
    return operacion(_token(empresa, forzar=True))


def _base(ruc: str, tipo: str, numeracion: str) -> str:
    return f"{ruc}-{tipo}-{numeracion}"


def _aplicar_ticket(empresa: Empresa, codigo: str, ticket: str, resultado: dict) -> dict:
    """Vuelca al registro el veredicto del ticket; devuelve la fila."""
    cod = resultado["codigo"]
    if cod == gre_cliente.TICKET_ACEPTADO:
        # ResponseCode del CDR: 0 aceptado; distinto de 0 con CDR = aceptado con observaciones.
        cdr_cod = resultado["cdr_codigo"]
        estado = "aceptado" if cdr_cod in (None, "0") else "aceptado_con_observaciones"
        return registro_guias.guardar(
            empresa.ruc, codigo, estado=estado, cdr=resultado["cdr"], codigo_error=None,
            des_obse=resultado["mensaje"] or "La guía ha sido aceptada")
    if cod == gre_cliente.TICKET_ERROR:
        fila = registro_guias.guardar(
            empresa.ruc, codigo, estado="rechazado", cdr=resultado["cdr"],
            codigo_error=resultado["error"], des_obse=resultado["mensaje"])
        rechazos.registrar(empresa.ruc, codigo, "rechazado", fila["des_obse"], "sunat")
        return fila
    # 98 (o algo desconocido): sigue en proceso en SUNAT.
    return registro_guias.guardar(empresa.ruc, codigo, estado="enviado", des_obse="SUNAT está procesando la guía")


def _preguntar_ticket(empresa: Empresa, codigo: str, ticket: str, intentos: int, espera: float) -> dict:
    fila = registro_guias.obtener(empresa.ruc, codigo)
    for i in range(intentos):
        if i:
            time.sleep(espera)
        resultado = _con_token(empresa, lambda t: gre_cliente.consultar_ticket(t, ticket))
        fila = _aplicar_ticket(empresa, codigo, ticket, resultado)
        if fila["estado"] != "enviado":
            break
    return fila


def emitir(empresa: Empresa, guia: GuiaEntrada) -> dict:
    """Emite (o continúa) una guía y devuelve su fila del registro. Excepciones:
    GuiaInvalida (422), GuiaNoEnviable (502/409)."""
    ruc = empresa.ruc
    validar(guia, ruc)
    codigo = f"{guia.tipo_guia}-{guia.numeracion}"

    previa = registro_guias.obtener(ruc, codigo)
    if previa and previa["estado"] in _TERMINALES:
        return previa  # ya cerrada: no se reenvía nunca
    if previa and previa["estado"] == "enviado" and previa["ticket"]:
        try:
            return _preguntar_ticket(empresa, codigo, previa["ticket"], 1, 0)
        except gre_cliente.GreError as e:
            raise GuiaNoEnviable(f"No se pudo consultar el ticket {previa['ticket']}: {e}") from e

    if guia.fecha_emision < _hora_lima() - timedelta(days=PLAZO_DIAS):
        motivo = f"Anulada: pasaron más de {PLAZO_DIAS} días desde la emisión sin que SUNAT la recibiera."
        return registro_guias.guardar(ruc, codigo, estado="anulado", des_obse=motivo,
                                      fecha_emision=guia.fecha_emision.isoformat(timespec="seconds"))

    datos = guia.model_dump()
    try:
        llave, cert = firma_xml.cargar_certificado(empresa.certificado_p12, empresa.certificado_clave)
        xml = firma_xml.firmar(guia_xml.construir(datos, ruc, empresa.nombre or ruc), llave, cert)
    except firma_xml.CertificadoError as e:
        raise GuiaNoEnviable(str(e), http=409) from e
    ahora = _hora_lima().isoformat(timespec="seconds")
    registro_guias.guardar(ruc, codigo, estado="pendiente", xml=xml.decode("utf-8"), ticket=None, cdr=None,
                           codigo_error=None, des_obse="Firmada, a la espera de enviarse a SUNAT",
                           fecha_emision=guia.fecha_emision.isoformat(timespec="seconds"), fec_gene=ahora)

    base = _base(ruc, guia.tipo_guia, guia.numeracion)
    try:
        ticket = _con_token(empresa, lambda t: gre_cliente.enviar(t, base, xml))
    except gre_cliente.GreError as e:
        if e.red:
            registro_guias.guardar(ruc, codigo, estado="error", des_obse=str(e))
            rechazos.registrar(ruc, codigo, "error", str(e), "red")
            raise GuiaNoEnviable(f"SUNAT no disponible: {e}") from e
        # SUNAT respondió y no la recibió (datos inválidos, serie no habilitada...): rechazo.
        fila = registro_guias.guardar(ruc, codigo, estado="rechazado", des_obse=str(e), codigo_error=e.codigo)
        rechazos.registrar(ruc, codigo, "rechazado", str(e), "sunat")
        return fila

    registro_guias.guardar(ruc, codigo, estado="enviado", ticket=ticket, fec_envi=_hora_lima().isoformat(timespec="seconds"),
                           des_obse="Enviada a SUNAT; falta su respuesta")
    try:
        return _preguntar_ticket(empresa, codigo, ticket, INTENTOS_TICKET, ESPERA_TICKET_SEG)
    except gre_cliente.GreError:
        # El envío ya se hizo; el veredicto lo dará el GET. No es un fallo de la emisión.
        return registro_guias.obtener(ruc, codigo)


def consultar(empresa: Empresa, codigo: str) -> dict | None:
    """Fila del registro; si está 'enviado', antes le pregunta a SUNAT por el ticket.
    None si esa guía no existe."""
    fila = registro_guias.obtener(empresa.ruc, codigo)
    if fila and fila["estado"] == "enviado" and fila["ticket"]:
        try:
            fila = _preguntar_ticket(empresa, codigo, fila["ticket"], 1, 0)
        except (gre_cliente.GreError, GuiaNoEnviable) as e:
            logger.warning("%s %s: no se pudo consultar el ticket: %s", empresa.ruc, codigo, e)
    return fila


def a_aceptado(fila: dict, incluir: set) -> ComprobanteAceptado:
    return ComprobanteAceptado(
        codigo=fila["codigo"], estado=fila["estado"], des_obse=fila["des_obse"] or "",
        codigo_error=fila["codigo_error"],
        xml=fila["xml"] if "xml" in incluir else None, cdr=fila["cdr"] if "cdr" in incluir else None)


def a_estado(fila: dict) -> EstadoComprobante:
    return EstadoComprobante(
        codigo=fila["codigo"], estado=fila["estado"], des_obse=fila["des_obse"] or "",
        fec_gene=fila["fec_gene"], fec_envi=fila["fec_envi"], codigo_error=fila["codigo_error"],
        xml=fila["xml"], cdr=fila["cdr"])
