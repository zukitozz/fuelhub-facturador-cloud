"""
Qué boletas entraron en cada resumen diario (RC), y qué corridas del envío automático
ya se hicieron (app/resumen_automatico.py).

Por qué existe: al enviarse un resumen, SFS borra de su bandeja las boletas que declara
(ver app/resumenes.py), y el RC no guarda la relación. Quien emitió la boleta necesita
saber en qué resumen quedó y cómo le fue a ese resumen; antes lo recibía en la respuesta
del POST, pero con el envío automático nadie recibe esa respuesta. Se guarda acá, en la
misma SQLite del seguimiento de reintentos (FACTURADOR_API_REINTENTOS_BD).

Los resúmenes anteriores a este registro se reconstruyen de su archivo .RDI en DATA, que
tiene una línea por boleta (ver reconstruir()).
"""
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import closing

logger = logging.getLogger(__name__)

_RAIZ_PROYECTO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_lock = threading.Lock()


def _ruta() -> str:
    # La misma base que app/reintentos.py (un solo archivo vivo del servidor).
    return os.getenv("FACTURADOR_API_REINTENTOS_BD", os.path.join(_RAIZ_PROYECTO, "reintentos.db"))


def _conectar() -> sqlite3.Connection:
    ruta = _ruta()
    os.makedirs(os.path.dirname(ruta) or ".", exist_ok=True)
    conexion = sqlite3.connect(ruta, timeout=10)
    conexion.row_factory = sqlite3.Row
    conexion.executescript(
        "CREATE TABLE IF NOT EXISTS resumenes_enviados ("
        "ruc TEXT NOT NULL, numeracion_rc TEXT NOT NULL, fecha_referencia TEXT, enviado_en INTEGER NOT NULL, "
        "boletas TEXT NOT NULL, PRIMARY KEY (ruc, numeracion_rc));"
        "CREATE TABLE IF NOT EXISTS boletas_resumen ("
        "ruc TEXT NOT NULL, numero TEXT NOT NULL, numeracion_rc TEXT NOT NULL, PRIMARY KEY (ruc, numero));"
        "CREATE TABLE IF NOT EXISTS corridas_resumen ("
        "ruc TEXT NOT NULL, fecha TEXT NOT NULL, estado TEXT NOT NULL, intentos INTEGER NOT NULL, "
        "proximo INTEGER, detalle TEXT, PRIMARY KEY (ruc, fecha));"
    )
    return conexion


def _consultar(sql: str, parametros: tuple = ()) -> list[dict]:
    with _lock, closing(_conectar()) as conexion:
        return [dict(r) for r in conexion.execute(sql, parametros).fetchall()]


def guardar(ruc: str, numeracion_rc: str, fecha_referencia: str | None, boletas: list[str],
            enviado_en: int | None = None) -> None:
    with _lock, closing(_conectar()) as conexion:
        conexion.execute(
            "INSERT OR REPLACE INTO resumenes_enviados VALUES (?,?,?,?,?)",
            (ruc, numeracion_rc, fecha_referencia, enviado_en or int(time.time()), json.dumps(boletas)))
        conexion.executemany(
            "INSERT OR REPLACE INTO boletas_resumen VALUES (?,?,?)",
            [(ruc, b, numeracion_rc) for b in boletas])
        conexion.commit()


def listar(ruc: str, desde_epoch: int | None = None, limite: int = 100) -> list[dict]:
    """Los resúmenes registrados de esa empresa, el más reciente primero."""
    filas = _consultar(
        "SELECT * FROM resumenes_enviados WHERE ruc=? AND enviado_en>=? ORDER BY enviado_en DESC, numeracion_rc DESC LIMIT ?",
        (ruc, desde_epoch or 0, limite))
    for f in filas:
        f["boletas"] = json.loads(f["boletas"])
    return filas


def rc_de_boleta(ruc: str, numero: str) -> str | None:
    filas = _consultar("SELECT numeracion_rc FROM boletas_resumen WHERE ruc=? AND numero=?", (ruc, numero))
    return filas[0]["numeracion_rc"] if filas else None


def existe(ruc: str, numeracion_rc: str) -> bool:
    return bool(_consultar("SELECT 1 FROM resumenes_enviados WHERE ruc=? AND numeracion_rc=?", (ruc, numeracion_rc)))


def reconstruir(empresa, numeraciones_rc: list[str]) -> int:
    """
    Registra los resúmenes que la bandeja de SFS conoce pero este registro no (los enviados
    antes de que existiera), leyendo las boletas de su .RDI en DATA. Devuelve cuántos.
    Línea del .RDI: fecha_emision|fecha_resumen|03|<boleta>|...  (ver dominio/resumen_diario.py)
    """
    nuevos = 0
    for numeracion in numeraciones_rc:
        if existe(empresa.ruc, numeracion):
            continue
        sin_prefijo = numeracion[3:] if numeracion.startswith("RC-") else numeracion
        ruta = os.path.join(empresa.data_dir, f"{empresa.ruc}-RC-{sin_prefijo}.RDI")
        try:
            with open(ruta, encoding="utf-8") as fh:
                lineas = [l.rstrip("\n").split("|") for l in fh if l.strip()]
            boletas = [c[3] for c in lineas if len(c) > 3]
            fecha_ref = lineas[0][0] if lineas else None
        except (OSError, IndexError):
            continue
        if boletas:
            guardar(empresa.ruc, numeracion, fecha_ref, boletas, enviado_en=int(os.path.getmtime(ruta)))
            nuevos += 1
            logger.info("%s %s: registro reconstruido desde su .RDI (%s boletas)", empresa.ruc, numeracion, len(boletas))
    return nuevos


# --- corridas diarias del envío automático -------------------------------------------------

def corrida(ruc: str, fecha: str) -> dict | None:
    filas = _consultar("SELECT * FROM corridas_resumen WHERE ruc=? AND fecha=?", (ruc, fecha))
    return filas[0] if filas else None


def guardar_corrida(ruc: str, fecha: str, estado: str, intentos: int, proximo: int | None, detalle: str = "") -> None:
    with _lock, closing(_conectar()) as conexion:
        conexion.execute("INSERT OR REPLACE INTO corridas_resumen VALUES (?,?,?,?,?,?)",
                         (ruc, fecha, estado, intentos, proximo, detalle[:1000]))
        conexion.commit()
