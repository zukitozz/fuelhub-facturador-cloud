"""
Consulta Integrada de Comprobantes de Pago (API REST de SUNAT): permite saber, tras
un corte de red, si SUNAT llegó a registrar un comprobante antes de reenviarlo.

Limitaciones que condicionan cómo se usa (ver README):
  - No devuelve el CDR, solo si SUNAT tiene el comprobante.
  - "No existe" es igual para "nunca llegó" y "fue rechazado": no basta para decidir
    un reenvío si ya hay un rechazo guardado.
  - Fecha y monto deben coincidir exactamente con lo declarado, o responde "no existe"
    aunque esté aceptado.
  - Una credencial sirve para consultar comprobantes de cualquier emisor: el RUC de
    la URL es el de quien consulta y numRuc (cuerpo) el del emisor.

Verificado contra SUNAT real el 2026-10-13 (token, estadoCp 0/1, emisor ajeno).
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import configuracion

logger = logging.getLogger(__name__)

_URL_TOKEN = "https://api-seguridad.sunat.gob.pe/v1/clientesextranet/{client_id}/oauth2/token/"
_URL_VALIDAR = "https://api.sunat.gob.pe/v1/contribuyente/contribuyentes/{ruc}/validarcomprobante"
_SCOPE = "https://api.sunat.gob.pe/v1/contribuyente/contribuyentes"
_TIMEOUT_SEG = 30
_MARGEN_TOKEN_SEG = 60

# estadoCp de la respuesta de SUNAT.
NO_EXISTE = "NO_EXISTE"
ACEPTADO = "ACEPTADO"
ANULADO = "ANULADO"
DESCONOCIDO = "DESCONOCIDO"  # sin credenciales, falla de red o respuesta inesperada
_ESTADO_CP = {"0": NO_EXISTE, "1": ACEPTADO, "2": ANULADO, "3": ACEPTADO, "4": NO_EXISTE}

_lock = threading.Lock()
_tokens: dict[str, tuple[str, float]] = {}  # client_id -> (access_token, vence_epoch)


def _post(url: str, data: bytes, headers: dict) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SEG) as r:
        return json.loads(r.read().decode("utf-8"))


def _token(cred) -> str:
    with _lock:
        guardado = _tokens.get(cred.client_id)
        if guardado and guardado[1] - _MARGEN_TOKEN_SEG > time.time():
            return guardado[0]
        cuerpo = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "scope": _SCOPE,
            "client_id": cred.client_id,
            "client_secret": cred.client_secret,
        }).encode()
        r = _post(
            _URL_TOKEN.format(client_id=urllib.parse.quote(cred.client_id, safe="")),
            cuerpo, {"Content-Type": "application/x-www-form-urlencoded"},
        )
        token = r["access_token"]
        _tokens[cred.client_id] = (token, time.time() + float(r.get("expires_in", 3600)))
        return token


def consultar_comprobante(empresa, ruc: str, tipo: str, serie: str, numero: str,
                          fecha_emision, monto: float) -> tuple[str, str]:
    """(estado, detalle). Nunca lanza: ante cualquier duda devuelve DESCONOCIDO, que
    el caller debe tratar como "no reenviar a ciegas"."""
    cred = configuracion.credenciales_consulta(empresa)
    if cred is None:
        return DESCONOCIDO, "faltan credenciales de consulta (bloque consulta_integrada o sol_client_* en empresas.yaml)"
    cuerpo = {
        "numRuc": ruc,
        "codComp": tipo,
        "numeroSerie": serie,
        "numero": str(int(numero)),  # SUNAT espera el correlativo sin ceros a la izquierda
        "fechaEmision": fecha_emision.strftime("%d/%m/%Y"),
        "monto": f"{monto:.2f}",
    }
    try:
        token = _token(cred)
        r = _post(
            _URL_VALIDAR.format(ruc=urllib.parse.quote(cred.ruc, safe="")),
            json.dumps(cuerpo).encode(),
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
    except urllib.error.HTTPError as e:
        if e.code == 401:  # token vencido antes de lo anunciado: se renueva en la próxima
            _tokens.pop(cred.client_id, None)
        logger.warning("Consulta integrada de %s-%s rechazada: HTTP %s", serie, numero, e.code)
        return DESCONOCIDO, f"HTTP {e.code}"
    except Exception as e:
        logger.warning("No se pudo consultar a SUNAT por %s-%s: %s", serie, numero, e)
        return DESCONOCIDO, str(e)

    codigo = str((r.get("data") or {}).get("estadoCp", ""))
    estado = _ESTADO_CP.get(codigo, DESCONOCIDO)
    return estado, r.get("message") or f"estadoCp={codigo}"
