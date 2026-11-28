"""
API keys con alcance por RUC.

Cada cliente (fuelhub-facturador, el gimnasio, …) recibe una clave que solo sirve para los
RUC que se le asignaron. Se envía en el encabezado `X-API-Key`.

MIGRACIÓN PROGRESIVA: estamos en producción y los servicios aún no mandan clave, así que el
modo por defecto es permisivo:
  - con clave: se valida (desconocida → 401, RUC fuera de su alcance → 403);
  - sin clave: pasa, pero se registra en el log quién fue (para saber a quién falta migrar).
El corte se hace sin tocar código, en el archivo de claves:
  exigir: true              -> toda la API exige clave
  exigir_rucs: ["2060…"]    -> solo esas empresas la exigen (corte empresa por empresa)

Archivo (api_keys.yaml, junto a empresas.yaml, o FACTURADOR_API_KEYS_YAML):

  exigir: false
  exigir_rucs: []
  claves:
    - nombre: fuelhub-facturador
      hash: <sha256 hex de la clave>     # la clave en claro nunca se guarda
      rucs: ["20609785269"]              # o "*" para todos

Se genera con `python -m app.claves nueva --nombre X --rucs 2060…,2010…`.
El archivo se relee cuando cambia (sin reiniciar el servicio).
"""
import hashlib
import hmac
import logging
import os
import threading

import yaml
from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader

from . import configuracion

logger = logging.getLogger(__name__)

_cabecera = APIKeyHeader(name="X-API-Key", auto_error=False, description="Clave del cliente (alcance por RUC)")
_lock = threading.Lock()
_cache: dict = {"ruta": None, "mtime": None, "datos": None}


def ruta_archivo() -> str:
    return os.getenv("FACTURADOR_API_KEYS_YAML") or os.path.join(
        os.path.dirname(os.path.abspath(configuracion._RUTA_EMPRESAS)), "api_keys.yaml")


def hash_de(clave: str) -> str:
    return hashlib.sha256(clave.encode()).hexdigest()


def _datos() -> dict:
    """Contenido del archivo de claves ({} si no existe: todo abierto)."""
    ruta = ruta_archivo()
    try:
        mtime = os.path.getmtime(ruta)
    except OSError:
        return {}
    with _lock:
        if _cache["ruta"] != ruta or _cache["mtime"] != mtime:
            try:
                with open(ruta, encoding="utf-8") as fh:
                    leido = yaml.safe_load(fh) or {}
            except (OSError, yaml.YAMLError) as e:
                # Un archivo roto no debe tumbar la API ni abrirla: se conserva lo último bueno.
                logger.error("api_keys.yaml ilegible (%s): se conserva la configuración anterior", e)
                return _cache["datos"] or {}
            _cache.update(ruta=ruta, mtime=mtime, datos=leido if isinstance(leido, dict) else {})
        return _cache["datos"] or {}


def _cliente_de(clave: str, datos: dict) -> dict | None:
    huella = hash_de(clave)
    encontrado = None
    for c in datos.get("claves") or []:  # sin cortar el bucle: tiempo constante
        if isinstance(c, dict) and hmac.compare_digest(str(c.get("hash", "")), huella):
            encontrado = c
    return encontrado


def _alcance_incluye(cliente: dict, ruc: str) -> bool:
    rucs = cliente.get("rucs") or []
    if rucs == "*" or rucs == ["*"]:
        return True
    return str(ruc) in {str(r) for r in (rucs if isinstance(rucs, list) else [rucs])}


def _ip(request: Request) -> str:
    adelante = request.headers.get("x-forwarded-for")
    return adelante.split(",")[0].strip() if adelante else (request.client.host if request.client else "?")


async def autorizar(request: Request, clave: str | None = Depends(_cabecera)) -> None:
    """Dependencia global. Rutas sin {ruc} (p. ej. /salud) quedan abiertas."""
    ruc = request.path_params.get("ruc")
    if not ruc:
        return  # /salud, /docs: no manejan datos de una empresa
    datos = _datos()

    if clave:
        cliente = _cliente_de(clave, datos)
        if cliente is None:
            logger.warning("API key desconocida: %s %s desde %s", request.method, request.url.path, _ip(request))
            raise HTTPException(status_code=401, detail="API key inválida.")
        if not _alcance_incluye(cliente, ruc):
            logger.warning("API key '%s' sin alcance al RUC %s: %s %s", cliente.get("nombre"), ruc,
                           request.method, request.url.path)
            raise HTTPException(status_code=403, detail=f"Esta API key no tiene acceso al RUC {ruc}.")
        request.state.cliente = cliente.get("nombre")
        return

    exige = bool(datos.get("exigir")) or str(ruc) in {str(r) for r in datos.get("exigir_rucs") or []}
    if exige:
        raise HTTPException(status_code=401, detail="Falta el encabezado X-API-Key.")
    logger.warning("SIN API KEY (aún permitido): %s %s desde %s", request.method, request.url.path, _ip(request))
