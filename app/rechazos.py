"""
Registro persistente de lo que salió mal con un comprobante, para soporte y para
mejorar: qué se rechazó, por qué y cuántas veces.

Por qué existe: la API no tiene base de datos propia, y el estado de un comprobante
vive en la SQLite de SFS de cada empresa — que SOBRESCRIBE: si un rechazado se
corrige y se reenvía bien, el rechazo desaparece de ahí. Para saber "qué error se
repite más" o "qué le pasó a F002-000150 el martes" hace falta una huella que no se
pise. Es un archivo JSON-lines (una línea por evento), sin más infraestructura.

Ruta: FACTURADOR_API_RECHAZOS_LOG. En producción apuntarla FUERA del repo, igual que
empresas.yaml (ver README). Sin configurar, usa rechazos.jsonl en la raíz del repo
(solo desarrollo; está en .gitignore).

Nunca rompe un request: si no se puede escribir, lo deja en el log de la app y sigue.
"""
import json
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_RAIZ_PROYECTO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LIMA = timezone(timedelta(hours=-5))
_MAX_TEXTO = 1000

# Estados (ver app/estados.py) que cuentan como fallo del comprobante en sí.
ESTADOS_RECHAZO = {"rechazado", "error"}

_lock = threading.Lock()
# Para no repetir el mismo rechazo cada vez que alguien consulta el GET de un
# comprobante ya rechazado (solo aplica a origen="consulta"). En memoria: tras un
# reinicio el primer GET lo vuelve a anotar una vez, y es aceptable.
_vistos: set = set()
_MAX_VISTOS = 5000

# Código de SUNAT dentro del texto libre que deja SFS en DES_OBSE. Las formas
# conocidas: "2800 - El dato ingresado...", "soap-env:Client.0111", "Error 3111".
# Ojo con las fechas: "2026-10-02" no debe leerse como el código 2026.
_PATRONES_CODIGO = (
    re.compile(r"(?:Client|Server)\.(\d{4})"),
    re.compile(r"(?:error|c[oó]digo)\s*:?\s*(\d{4})(?!\d)", re.IGNORECASE),
    re.compile(r"(?<!\d)(\d{4})(?!\d)(?!-\d{2}-\d{2})\s*[-:–]\s*\S"),
)


def extraer_codigo(des_obse: Optional[str]) -> Optional[str]:
    """El código de error de SUNAT dentro de des_obse, o None si el texto no trae uno
    (p.ej. 'Error al firma archivo XML' es de SFS, no de SUNAT)."""
    texto = des_obse or ""
    for patron in _PATRONES_CODIGO:
        m = patron.search(texto)
        if m:
            return m.group(1)
    return None


def _ruta() -> str:
    return os.getenv("FACTURADOR_API_RECHAZOS_LOG", os.path.join(_RAIZ_PROYECTO, "rechazos.jsonl"))


def registrar(ruc: str, codigo: str, estado: str, des_obse: Optional[str], origen: str,
              detalle=None, descripcion_error: Optional[str] = None) -> None:
    """
    origen: sunat (SUNAT rechazó) | sfs (SFS no pudo firmar/generar) | validacion (el
    payload no pasó las reglas, ni llegó a SFS) | red (SFS no disponible) |
    resumen (un resumen diario fallido) | consulta (hallado al consultar el estado).
    """
    try:
        texto = (des_obse or "")[:_MAX_TEXTO]
        if origen == "consulta":
            clave = (ruc, codigo, estado, texto)
            if clave in _vistos:
                return
        with _lock:
            if len(_vistos) >= _MAX_VISTOS:
                _vistos.clear()
            _vistos.add((ruc, codigo, estado, texto))
            ruta = _ruta()
            os.makedirs(os.path.dirname(ruta) or ".", exist_ok=True)
            with open(ruta, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "fecha": datetime.now(_LIMA).isoformat(timespec="seconds"),
                    "ruc": ruc,
                    "codigo": codigo,
                    "estado": estado,
                    "codigo_error": extraer_codigo(texto),
                    "descripcion_error": descripcion_error,
                    "des_obse": texto,
                    "origen": origen,
                    "detalle": detalle,
                }, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("No se pudo registrar el rechazo de %s %s", ruc, codigo)


def listar(ruc: str, desde: Optional[date] = None, origen: Optional[str] = None,
           limite: int = 100) -> list:
    """Los más recientes primero. Salta las líneas ilegibles en vez de fallar."""
    ruta = _ruta()
    if not os.path.exists(ruta):
        return []
    with _lock:
        with open(ruta, "r", encoding="utf-8") as fh:
            lineas = fh.readlines()
    resultado = []
    for linea in reversed(lineas):
        try:
            e = json.loads(linea)
        except json.JSONDecodeError:
            continue
        if e.get("ruc") != ruc:
            continue
        if origen and e.get("origen") != origen:
            continue
        # La fecha va en hora de Lima: su prefijo es el día local de Lima.
        if desde and str(e.get("fecha", ""))[:10] < desde.isoformat():
            continue
        resultado.append(e)
        if len(resultado) >= limite:
            break
    return resultado
