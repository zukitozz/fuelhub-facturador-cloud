"""
Estado de las guías de remisión (GRE). A diferencia de facturas y notas, las guías no pasan
por SFS (ver app/guias.py), así que no hay SQLite de SFS donde leer su estado: se guarda acá,
en la misma SQLite del seguimiento de reintentos (FACTURADOR_API_REINTENTOS_BD).

Una fila por guía (ruc + código "09-T001-000001"), que se actualiza al reenviar el mismo
número. Guarda el XML firmado y el CDR para devolverlos en el GET sin volver a SUNAT.
"""
import os
import sqlite3
import threading
import time
from contextlib import closing

_RAIZ_PROYECTO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_lock = threading.Lock()

_COLUMNAS = ("ruc", "codigo", "estado", "des_obse", "ticket", "xml", "cdr", "codigo_error",
             "fecha_emision", "fec_gene", "fec_envi", "actualizado_en")


def _ruta() -> str:
    return os.getenv("FACTURADOR_API_REINTENTOS_BD", os.path.join(_RAIZ_PROYECTO, "reintentos.db"))


def _conectar() -> sqlite3.Connection:
    ruta = _ruta()
    os.makedirs(os.path.dirname(ruta) or ".", exist_ok=True)
    conexion = sqlite3.connect(ruta, timeout=10)
    conexion.row_factory = sqlite3.Row
    conexion.execute(
        "CREATE TABLE IF NOT EXISTS guias ("
        "ruc TEXT NOT NULL, codigo TEXT NOT NULL, estado TEXT NOT NULL, des_obse TEXT, ticket TEXT, "
        "xml TEXT, cdr TEXT, codigo_error TEXT, fecha_emision TEXT, fec_gene TEXT, fec_envi TEXT, "
        "actualizado_en INTEGER NOT NULL, PRIMARY KEY (ruc, codigo))"
    )
    return conexion


def obtener(ruc: str, codigo: str) -> dict | None:
    with _lock, closing(_conectar()) as conexion:
        fila = conexion.execute("SELECT * FROM guias WHERE ruc=? AND codigo=?", (ruc, codigo)).fetchone()
    return dict(fila) if fila else None


def guardar(ruc: str, codigo: str, **campos) -> dict:
    """Crea o actualiza la fila; solo cambia los campos que se pasan."""
    desconocidos = set(campos) - set(_COLUMNAS)
    if desconocidos:
        raise ValueError(f"campos desconocidos: {sorted(desconocidos)}")
    with _lock, closing(_conectar()) as conexion:
        actual = conexion.execute("SELECT * FROM guias WHERE ruc=? AND codigo=?", (ruc, codigo)).fetchone()
        fila = dict(actual) if actual else {c: None for c in _COLUMNAS}
        fila.update(ruc=ruc, codigo=codigo, actualizado_en=int(time.time()), **campos)
        fila["estado"] = fila.get("estado") or "pendiente"
        conexion.execute(
            f"INSERT OR REPLACE INTO guias ({','.join(_COLUMNAS)}) VALUES ({','.join('?' * len(_COLUMNAS))})",
            tuple(fila[c] for c in _COLUMNAS))
        conexion.commit()
    return fila
