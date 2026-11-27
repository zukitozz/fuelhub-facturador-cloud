"""
Cliente de la API REST de guías de remisión de SUNAT (GRE), la que reemplazó a SOAP:

  token    POST {SEGURIDAD}/{client_id}/oauth2/token/      (OAuth, grant_type=password)
  enviar   POST {CPE}/contribuyente/gem/comprobantes/{ruc}-{tipo}-{serie}-{correlativo}
  ticket   GET  {CPE}/contribuyente/gem/comprobantes/envios/{numTicket}

Las URLs son las que ya trae SFS en su constantes.properties (URI_BASE_*_GRE). El envío
es asíncrono: devuelve un ticket y el CDR se pide después con consultar_ticket().

Se usa urllib (como el resto del servicio) y todo lo que toca la red pasa por _http(),
que es lo único que las pruebas reemplazan.
"""
import base64
import hashlib
import io
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

URL_TOKEN = os.getenv("GRE_URL_TOKEN", "https://api-seguridad.sunat.gob.pe/v1/clientessol")
URL_API = os.getenv("GRE_URL_API", "https://api-cpe.sunat.gob.pe/v1/contribuyente/gem/comprobantes")
SCOPE = "https://api-cpe.sunat.gob.pe"
_TIMEOUT_SEG = 30

# codRespuesta del ticket
TICKET_ACEPTADO = "0"
TICKET_EN_PROCESO = "98"
TICKET_ERROR = "99"


class GreError(Exception):
    """Fallo hablando con SUNAT. `red` = no hubo respuesta utilizable (se puede reintentar
    el mismo número); `http` = el código HTTP si SUNAT respondió; `codigo` = el código de
    error de SUNAT si lo trae."""

    def __init__(self, mensaje: str, *, red: bool = False, http: int | None = None, codigo: str | None = None):
        super().__init__(mensaje)
        self.red = red
        self.http = http
        self.codigo = codigo


def _http(metodo: str, url: str, *, cuerpo: bytes | None = None, cabeceras: dict | None = None) -> tuple[int, dict]:
    """(status, json). Un 4xx/5xx con cuerpo JSON se devuelve igual: SUNAT explica el
    motivo ahí. GreError(red=True) si no hubo respuesta."""
    req = urllib.request.Request(url, data=cuerpo, headers=cabeceras or {}, method=metodo)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SEG) as r:
            return r.status, _json(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _json(e.read())
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GreError(f"SUNAT no respondió ({url}): {e}", red=True) from e


def _json(crudo: bytes) -> dict:
    try:
        datos = json.loads(crudo.decode("utf-8") or "{}")
        return datos if isinstance(datos, dict) else {"_": datos}
    except (ValueError, UnicodeDecodeError):
        return {"_texto": crudo[:300].decode("utf-8", errors="replace")}


def _mensaje(datos: dict) -> str:
    """El motivo que dio SUNAT, venga con la forma que venga."""
    partes = [str(datos.get(k)) for k in ("cod", "msg", "message", "error_description", "error", "_texto") if datos.get(k)]
    for e in datos.get("errors") or []:
        if isinstance(e, dict):
            partes.append(f"{e.get('cod', '')} {e.get('msg', '')}".strip())
    return "; ".join(p for p in partes if p) or "sin detalle"


# --- token ------------------------------------------------------------------

_tokens: dict = {}
_lock = threading.Lock()


def obtener_token(ruc: str, client_id: str, client_secret: str, usuario_sol: str, clave_sol: str,
                  forzar: bool = False) -> str:
    """Token vigente para esa empresa; se reutiliza hasta poco antes de vencer (dura ~1 h)."""
    clave = (ruc, client_id, usuario_sol)
    with _lock:
        guardado = _tokens.get(clave)
        if guardado and not forzar and guardado[1] > time.time() + 60:
            return guardado[0]
    cuerpo = urllib.parse.urlencode({
        "grant_type": "password", "scope": SCOPE,
        "client_id": client_id, "client_secret": client_secret,
        "username": f"{ruc}{usuario_sol}", "password": clave_sol,
    }).encode()
    status, datos = _http("POST", f"{URL_TOKEN}/{client_id}/oauth2/token/", cuerpo=cuerpo,
                          cabeceras={"Content-Type": "application/x-www-form-urlencoded"})
    token = datos.get("access_token")
    if status != 200 or not token:
        raise GreError(f"SUNAT no entregó el token ({status}): {_mensaje(datos)}", http=status)
    with _lock:
        _tokens[clave] = (token, time.time() + int(datos.get("expires_in") or 3600))
    return token


# --- enviar / ticket ---------------------------------------------------------

def empaquetar(nombre_base: str, xml: bytes) -> tuple[str, str, str]:
    """(nombre del zip, zip en base64, SHA-256 del zip en hex): lo que pide el envío.
    El zip lleva un solo archivo, {nombre_base}.xml."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{nombre_base}.xml", xml)
    crudo = buffer.getvalue()
    return f"{nombre_base}.zip", base64.b64encode(crudo).decode(), hashlib.sha256(crudo).hexdigest()


def enviar(token: str, nombre_base: str, xml: bytes) -> str:
    """Envía la guía y devuelve el ticket. GreError si SUNAT la rechaza de entrada."""
    nombre_zip, zip_b64, hash_zip = empaquetar(nombre_base, xml)
    cuerpo = json.dumps({"archivo": {"nomArchivo": nombre_zip, "arcGreZip": zip_b64, "hashZip": hash_zip}}).encode()
    status, datos = _http("POST", f"{URL_API}/{nombre_base}", cuerpo=cuerpo, cabeceras={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    ticket = datos.get("numTicket")
    if status == 200 and ticket:
        return str(ticket)
    raise GreError(f"SUNAT rechazó el envío ({status}): {_mensaje(datos)}", http=status,
                   codigo=str(datos.get("cod") or "") or None)


def consultar_ticket(token: str, ticket: str) -> dict:
    """
    {"codigo": "0"|"98"|"99", "cdr": xml|None, "cdr_codigo": ResponseCode|None,
     "mensaje": str, "error": codigo|None}
    """
    status, datos = _http("GET", f"{URL_API}/envios/{ticket}", cabeceras={"Authorization": f"Bearer {token}"})
    if status != 200:
        raise GreError(f"SUNAT no devolvió el ticket {ticket} ({status}): {_mensaje(datos)}", http=status,
                       codigo=str(datos.get("cod") or "") or None)
    codigo = str(datos.get("codRespuesta", ""))
    error = datos.get("error") if isinstance(datos.get("error"), dict) else {}
    cdr, cdr_codigo, descripcion = _leer_cdr(datos.get("arcCdr"))
    mensaje = descripcion or ""
    if codigo == TICKET_ERROR and not mensaje:
        mensaje = f"{error.get('numError', '')} {error.get('desError', '')}".strip() or "SUNAT informó un error"
    return {"codigo": codigo, "cdr": cdr, "cdr_codigo": cdr_codigo, "mensaje": mensaje,
            "error": str(error.get("numError")) if error.get("numError") else None}


def _leer_cdr(arc_cdr) -> tuple[str | None, str | None, str | None]:
    """(xml, ResponseCode, Description) del CDR que viene como zip en base64."""
    if not arc_cdr:
        return None, None, None
    try:
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(arc_cdr))) as z:
            nombre = next(n for n in z.namelist() if n.lower().endswith(".xml"))
            contenido = z.read(nombre)
        raiz = ET.fromstring(contenido)
    except (ValueError, StopIteration, zipfile.BadZipFile, ET.ParseError):
        return None, None, None

    def _texto(sufijo: str):
        for el in raiz.iter():
            if el.tag.endswith("}" + sufijo) and el.text:
                return el.text.strip()
        return None
    return contenido.decode("utf-8", errors="replace"), _texto("ResponseCode"), _texto("Description")
