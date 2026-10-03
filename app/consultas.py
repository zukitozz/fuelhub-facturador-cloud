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


def boletas_pendientes_resumen(bd_path: str, ruc: str) -> list[str]:
    """
    Numeraciones de boletas firmadas (ind_situ='02') y nunca enviadas, listas para
    entrar a un resumen diario — ver app/resumenes.py. '02' y no otro estado porque
    es justo donde las deja sfs_cliente.generar() a propósito, sin pasar por
    enviarXML.htm (ver TIPOS_SIN_ENVIO_INDIVIDUAL en sfs_cliente.py).
    """
    if not os.path.exists(bd_path):
        return []
    with closing(sqlite3.connect(bd_path)) as conexion:
        filas = conexion.execute(
            "SELECT NUM_DOCU FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU='03' AND IND_SITU='02'",
            (ruc,),
        ).fetchall()
    return [f[0] for f in filas]


def siguiente_correlativo_rc(bd_path: str, ruc: str, fecha_yyyymmdd: str) -> int:
    """
    Primer correlativo libre para un RC de hoy (RC-{fecha}-NNN), mirando los que
    SFS ya tiene registrados — no hace falta un contador propio en facturador-api,
    es lo mismo que haría estado/resumenes.py del daemon pero derivado de la
    propia bandeja de SFS en vez de un resumenes.json aparte.
    """
    if not os.path.exists(bd_path):
        return 1
    prefijo = f"RC-{fecha_yyyymmdd}-"
    with closing(sqlite3.connect(bd_path)) as conexion:
        filas = conexion.execute(
            "SELECT NUM_DOCU FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU='RC' AND NUM_DOCU LIKE ?",
            (ruc, f"{prefijo}%"),
        ).fetchall()
    usados = set()
    for (num_docu,) in filas:
        try:
            usados.add(int(num_docu[len(prefijo):]))
        except ValueError:
            continue
    n = 1
    while n in usados:
        n += 1
    return n


def contar_resumenes_hoy(bd_path: str, ruc: str, fecha_yyyymmdd: str) -> int:
    """Cuántos RC ya se armaron hoy — freno de seguridad contra un bucle de
    reintentos que arme resumen tras resumen (ver MAX_RESUMENES_DIA del daemon
    original, mismo criterio, derivado acá de la bandeja de SFS)."""
    if not os.path.exists(bd_path):
        return 0
    with closing(sqlite3.connect(bd_path)) as conexion:
        (total,) = conexion.execute(
            "SELECT COUNT(*) FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU='RC' AND NUM_DOCU LIKE ?",
            (ruc, f"RC-{fecha_yyyymmdd}-%"),
        ).fetchone()
    return total


def eliminar_documento(bd_path: str, ruc: str, tipo: str, numero: str):
    """Saca una fila de DOCUMENTO — se usa para limpiar una boleta ya incluida y
    enviada dentro de un resumen, para que no quede ocupando la bandeja de SFS
    para siempre en ind_situ='02' sin que nada vuelva a mirarla."""
    if not os.path.exists(bd_path):
        return
    with closing(sqlite3.connect(bd_path)) as conexion:
        with conexion:
            conexion.execute(
                "DELETE FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=?",
                (ruc, tipo, numero),
            )


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
