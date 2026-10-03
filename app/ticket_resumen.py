"""
Resolución del ticket de un resumen diario (RC) directo contra SUNAT (SOAP
getStatus) — vendored de sunat/ticket.py y sunat/consulta.py del daemon original
(fuelhub-facturador), adaptado al modelo on-demand de este servicio.

Por qué esto no lo hace SFS: SFS solo resuelve tickets desde un job programado
(ActualizarBajasJob) que exige tener el temporizador prendido, y encenderlo
reactivaría también sus jobs de generar/enviar — justo lo que facturador-api evita
a propósito (todas las empresas se dan de alta con cmbFuncionamiento='02',
temporizador OFF; ver deploy/alta_empresa.sh). Por eso la consulta se hace acá,
directo a SUNAT, igual que el daemon original.

Por qué no hay reintentos ni cooldown propios, a diferencia de
aplicacion/recuperacion_cdr.py: ahí un hilo de fondo reconsulta solo cada ciclo, así
que necesita frenos (MAX_CONSULTAS_FALLIDAS, cooldown) para no golpear a SUNAT sin
parar. Acá cada llamado a GET /empresas/{ruc}/comprobantes/{codigo} YA es un intento
explícito de quien consulta — el freno es, sencillamente, cuántas veces alguien
vuelve a pedir el estado.

Por qué necesita sol_usuario/sol_clave en texto plano en empresas.yaml (ver
app/configuracion.py): SFS guarda su propia copia encriptada de la clave SOL
secundaria, pero facturador-api nunca implementó la encriptación/desencriptación
propia de SFS (Encriptar()/Desencriptar() del .jar), así que no hay forma de
reusarla. Es un retroceso consciente frente al resto de este servicio, que no
guarda secretos — aceptado explícitamente para no reactivar el temporizador de SFS.
"""
import base64
import binascii
import logging
import os
import re
import sqlite3
import urllib.error
import urllib.request
from contextlib import closing

logger = logging.getLogger(__name__)

_SOBRE_TICKET = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:ser="http://service.sunat.gob.pe">
  <soapenv:Header>
    <wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
      <wsse:UsernameToken>
        <wsse:Username>{usuario}</wsse:Username>
        <wsse:Password>{clave}</wsse:Password>
      </wsse:UsernameToken>
    </wsse:Security>
  </soapenv:Header>
  <soapenv:Body>
    <ser:getStatus>
      <ticket>{ticket}</ticket>
    </ser:getStatus>
  </soapenv:Body>
</soapenv:Envelope>"""

# Códigos de getStatus (ver config.py del daemon original para el catálogo
# completo verificado contra el servicio real de SUNAT).
_TICKET_CON_CDR = ("0", "99")
_TICKET_EN_PROCESO = "98"
_TICKET_NO_EXISTE = "127"

# Mismos estados que _ESTADOS_RESUMEN_ABIERTO de sfs/bd.py: un resumen en
# cualquiera de ellos todavía puede resolverse por su ticket (incluye '05', al que
# SFS cae cuando una consulta de ticket falla, no solo '08'/'09' de "enviado").
_ESTADOS_RESUMEN_ABIERTO = ("05", "06", "08", "09", "10")

# Ancho de la columna DES_OBSE del SFS — ver esquema_bd.sql.
_MAX_DES_OBSE = 250


def _texto_de_nodo(xml: str, etiqueta: str) -> str:
    """Contenido de un nodo de la respuesta SOAP, sin importar su prefijo."""
    m = re.search(rf"<(?:\w+:)?{etiqueta}>(.*?)</(?:\w+:)?{etiqueta}>", xml, re.S)
    return m.group(1).strip() if m else ""


def _norm_codigo_ticket(codigo) -> str:
    """El código de getStatus sin los ceros de la izquierda — SUNAT lo devuelve
    en anchos distintos ('0' contra '0098') y compararlo crudo falla justo en el
    caso más frecuente. El '0' se conserva como '0', no como cadena vacía: vacío
    significa "SUNAT no dijo nada" (falla de transporte) y son cosas opuestas."""
    texto = (codigo or "").strip()
    if not texto:
        return ""
    return texto.lstrip("0") or "0"


def _codigo_de_fault(cuerpo: str) -> str:
    """Código de SUNAT dentro del faultcode de un error SOAP (viene pegado al
    espacio de nombres, 'soap-env:Client.0127')."""
    m = re.search(r"<(?:\w+:)?faultcode>(.*?)</(?:\w+:)?faultcode>", cuerpo, re.S)
    if not m:
        return ""
    n = re.search(r"(\d{3,4})\s*$", m.group(1).strip())
    return n.group(1) if n else ""


def _url_bill_service(vali_dir: str) -> str:
    """
    Endpoint de envío del SFS (RUTA_SERV_CDP de constantes.properties), que es el
    mismo servicio donde se consulta el ticket. Se lee de ahí para que la consulta
    salga siempre al ambiente al que el SFS de esa empresa está enviando.
    """
    ruta = os.path.join(vali_dir, "constantes.properties")
    try:
        # utf-8-sig y no utf-8: si alguien edita el archivo con el Bloc de notas
        # le queda un BOM al inicio que se pegaría al nombre de la primera propiedad.
        with open(ruta, encoding="utf-8-sig", errors="replace") as fh:
            for linea in fh:
                linea = linea.strip()
                if linea.startswith("RUTA_SERV_CDP="):
                    return linea.split("=", 1)[1].strip()
    except OSError:
        logger.exception("No se pudo leer %s para ubicar el servicio de SUNAT.", ruta)
    return ""


def _consultar_ticket_sunat(empresa, ruc: str, ticket: str):
    """(codigo, mensaje, cdr_zip). codigo=None significa "no sé" (falla de
    transporte); un fault codificado de SUNAT SÍ vuelve en codigo, porque es la
    única forma de distinguir un ticket ya consumido de una falla pasajera."""
    if not (empresa.sol_usuario and empresa.sol_clave):
        return None, "faltan sol_usuario/sol_clave en empresas.yaml para este RUC", None
    url = _url_bill_service(empresa.vali_dir)
    if not url:
        return None, "no se pudo determinar el servicio de SUNAT (RUTA_SERV_CDP)", None

    sobre = _SOBRE_TICKET.format(
        usuario=f"{ruc}{empresa.sol_usuario}", clave=empresa.sol_clave, ticket=ticket
    )
    peticion = urllib.request.Request(
        url,
        data=sobre.encode("utf-8"),
        headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": "urn:getStatus"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(peticion, timeout=30) as r:
            respuesta = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode("utf-8", "replace")
        detalle = _texto_de_nodo(cuerpo, "faultstring") or f"HTTP {e.code}"
        codigo_fault = _codigo_de_fault(cuerpo)
        logger.warning("Consulta del ticket %s rechazada por SUNAT: %s", ticket, detalle)
        return codigo_fault or None, detalle, None
    except Exception as e:
        logger.warning("No se pudo consultar el ticket %s: %s", ticket, e)
        return None, str(e), None

    codigo  = _texto_de_nodo(respuesta, "statusCode")
    mensaje = _texto_de_nodo(respuesta, "statusMessage")
    b64     = _texto_de_nodo(respuesta, "content")
    cdr = None
    if b64:
        try:
            cdr = base64.b64decode(b64)
        except (ValueError, binascii.Error):
            logger.exception("SUNAT devolvió un CDR ilegible para el ticket %s", ticket)
    return codigo or None, mensaje, cdr


def _obtener_ticket(bd_path: str, ruc: str, numeracion: str) -> str | None:
    if not os.path.exists(bd_path):
        return None
    with closing(sqlite3.connect(bd_path)) as conexion:
        fila = conexion.execute(
            "SELECT NUM_TICKET FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU='RC' AND NUM_DOCU=?",
            (ruc, numeracion),
        ).fetchone()
    return (fila[0] or None) if fila else None


def _guardar_cdr(rpta_dir: str, ruc: str, numeracion: str, cdr: bytes) -> None:
    """Deja el CDR en RPTA con el mismo nombre que usaría el envío normal —de ahí
    lo lee consultas.obtener_cdr_xml() sin saber que vino por este camino."""
    os.makedirs(rpta_dir, exist_ok=True)
    destino = os.path.join(rpta_dir, f"R{ruc}-RC-{numeracion}.zip")
    with open(destino + ".tmp", "wb") as fh:
        fh.write(cdr)
    os.replace(destino + ".tmp", destino)


def _cerrar_resumen_en_sfs(bd_path: str, ruc: str, numeracion: str, veredicto: str) -> None:
    """
    Cierra la fila del resumen en la bandeja de SFS ('03' = "ya no me ocupo de
    esto", tanto para un aceptado como para un rechazado). El veredicto real vive
    en el CDR ya guardado, no acá — esto solo evita que el resumen quede
    reportándose como abierto para siempre (SFS nunca lo tocará: su ticket ya se
    consumió al consultarlo).
    """
    if not os.path.exists(bd_path):
        return
    marcas = ",".join("?" * len(_ESTADOS_RESUMEN_ABIERTO))
    with closing(sqlite3.connect(bd_path)) as conexion:
        with conexion:
            conexion.execute(
                f"UPDATE DOCUMENTO SET IND_SITU='03', DES_OBSE=? "
                f"WHERE NUM_RUC=? AND TIP_DOCU='RC' AND NUM_DOCU=? "
                f"AND IND_SITU IN ({marcas})",
                (veredicto[:_MAX_DES_OBSE], ruc, numeracion, *_ESTADOS_RESUMEN_ABIERTO),
            )


def resolver_ticket_pendiente(empresa, ruc: str, numeracion: str) -> str | None:
    """
    Si el RC {numeracion} tiene un ticket abierto en la bandeja de SFS, le
    pregunta a SUNAT por su resultado y, si ya hay veredicto, deja el CDR en RPTA
    y cierra la fila en SFS. Devuelve un mensaje corto de diagnóstico (para log),
    o None si no había ticket que consultar.

    Best-effort a propósito: nunca lanza por un fallo de red o de SUNAT — el
    llamador (GET /comprobantes/{codigo}) sigue respondiendo con el estado que ya
    tenía, y quien consulta puede reintentar llamando de nuevo más tarde.
    """
    ticket = _obtener_ticket(empresa.bd_path, ruc, numeracion)
    if not ticket:
        return None

    codigo, mensaje, cdr = _consultar_ticket_sunat(empresa, ruc, ticket)
    codigo = _norm_codigo_ticket(codigo)

    if codigo == _TICKET_NO_EXISTE:
        # Definitivo: el ticket se consumió y ya no hay nada que preguntarle a
        # SUNAT. No se cierra solo —la numeración puede no estar aceptada y
        # requiere revisión manual en el portal antes de decidir qué hacer con
        # las boletas que agrupaba (ya quedaron fuera de DATA al generar el RC).
        return f"el ticket {ticket} ya no existe en SUNAT ({mensaje}) — requiere revisión manual"
    if codigo == _TICKET_EN_PROCESO:
        return f"SUNAT todavía procesa el ticket {ticket} ({mensaje})"
    if not (cdr and codigo in _TICKET_CON_CDR):
        return f"ticket {ticket}: sin respuesta útil todavía ({mensaje})"

    _guardar_cdr(empresa.rpta_dir, ruc, numeracion, cdr)
    _cerrar_resumen_en_sfs(empresa.bd_path, ruc, numeracion, mensaje or "Aceptado (CDR recuperado por ticket)")
    return f"CDR del ticket {ticket} recuperado y resumen cerrado ({mensaje})"
