"""
Lectura del estado de un documento y del XML de su CDR, directo de lo que SFS ya
mantiene (su SQLite y sus carpetas RPTA/procesados) — nada de esto lo escribe
facturador_api, solo lo lee. Es la versión parametrizada (bd_path/rpta_dir por
empresa) de lo que sfs/bd.py ya hace para el daemon.

Por qué es seguro leer la SQLite de SFS mientras su JVM puede estar escribiendo:
el daemon actual ya lo hace así en producción (ver sfs/bd.py), con conexiones de
solo lectura y consultas puntuales por clave primaria — no hay nada nuevo de riesgo
acá, solo la misma receta ya probada, parametrizada por empresa.
"""
import os
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from contextlib import closing

# Estados de IND_SITU en los que ya hay (o puede haber) un CDR para leer —
# ver config.py:_NOMBRE_SITU para el catálogo completo.
_ESTADOS_CON_CDR = {"03", "04", "11", "12"}


def consultar_estado(bd_path: str, ruc: str, tipo: str, numero: str) -> dict | None:
    """None si el documento no existe en la bandeja de SFS (código inválido o
    nunca se generó)."""
    if not os.path.exists(bd_path):
        return None
    with closing(sqlite3.connect(bd_path)) as conexion:
        fila = conexion.execute(
            "SELECT IND_SITU, DES_OBSE, FEC_GENE, FEC_ENVI FROM DOCUMENTO "
            "WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=?",
            (ruc, tipo, numero),
        ).fetchone()
    if fila is None:
        return None
    ind_situ, des_obse, fec_gene, fec_envi = fila
    return {
        "ind_situ": ind_situ or "",
        "des_obse": des_obse or "",
        "fec_gene": fec_gene or None,
        "fec_envi": fec_envi or None,
    }


def obtener_cdr_xml(rpta_dir: str, procesados_dir: str, ruc: str, tipo: str, numero: str) -> str | None:
    """
    XML de la respuesta de SUNAT (el CDR), como texto, o None si todavía no está.

    Busca primero en procesados/ (donde el barrido del daemon archiva los CDR ya
    procesados) y después en RPTA/ directo (recién descargado, antes de archivar) —
    mismo orden que sfs/bd.py:_veredicto_archivado.
    """
    nombre = f"R{ruc}-{tipo}-{numero}.zip"
    for carpeta in (procesados_dir, rpta_dir):
        ruta = os.path.join(carpeta, nombre)
        try:
            if os.path.getsize(ruta) == 0:
                continue
            with zipfile.ZipFile(ruta) as z:
                xmls = [n for n in z.namelist() if n.lower().endswith(".xml")]
                if not xmls:
                    continue
                contenido = z.read(xmls[0])
                ET.fromstring(contenido)  # valida que sea XML bien formado antes de devolverlo
                return contenido.decode("utf-8", errors="replace")
        except OSError:
            continue
        except (zipfile.BadZipFile, ET.ParseError):
            continue
    return None
