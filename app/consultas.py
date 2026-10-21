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

from dominio.comprobante import _nombre_base

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


def documentos_fallidos(bd_path: str, ruc: str) -> list[dict]:
    """
    Facturas y notas que SFS tiene en error ('06') o rechazadas ('10'): lo que
    app/reintentos.py vigila. Sin boletas (nunca salen solas a SUNAT) ni resúmenes.
    """
    if not os.path.exists(bd_path):
        return []
    with closing(sqlite3.connect(bd_path)) as conexion:
        filas = conexion.execute(
            "SELECT TIP_DOCU, NUM_DOCU, IND_SITU, DES_OBSE FROM DOCUMENTO "
            "WHERE NUM_RUC=? AND IND_SITU IN ('06','10') AND TIP_DOCU IN ('01','07','08')",
            (ruc,),
        ).fetchall()
    return [{"tipo": t, "numero": n, "ind_situ": s, "des_obse": o or ""} for t, n, s, o in filas]


def numeraciones_resumenes(bd_path: str, ruc: str) -> list[str]:
    """Numeración de los resúmenes (RC) que SFS envió. Sin los que quedaron en '01'/'06':
    sus boletas no se consumieron (ver app/resumenes.py), así que no están declaradas."""
    if not os.path.exists(bd_path):
        return []
    with closing(sqlite3.connect(bd_path)) as conexion:
        filas = conexion.execute(
            "SELECT NUM_DOCU FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU='RC' "
            "AND IND_SITU NOT IN ('01','06')", (ruc,)).fetchall()
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


def reiniciar_documento(bd_path: str, firma_dir: str, ruc: str, tipo: str, numero: str) -> bool:
    """
    Saca de la bandeja de SFS un documento que quedó en error ('06') o rechazado ('10'),
    y borra su XML firmado viejo, para poder regenerarlo y reenviarlo (ver app/reenvio.py).

    El DELETE lleva la condición de estado a propósito: si entre que se miró y se borra
    el documento ya cambió (p.ej. SUNAT lo aceptó), no se toca. Un aceptado nunca se
    reinicia. Devuelve True si borró la fila.
    """
    if not os.path.exists(bd_path):
        return False
    with closing(sqlite3.connect(bd_path, timeout=10)) as conexion:
        with conexion:
            cursor = conexion.execute(
                "DELETE FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=? "
                "AND IND_SITU IN ('06','10')",
                (ruc, tipo, numero),
            )
    if cursor.rowcount <= 0:
        return False
    try:
        os.remove(os.path.join(firma_dir, f"{_nombre_base(ruc, tipo, numero)}.xml"))
    except FileNotFoundError:
        pass
    return True


def anular_documento(bd_path: str, ruc: str, tipo: str, numero: str, motivo: str,
                     estados: tuple = ("06", "10")) -> bool:
    """
    Deja un documento fallido ('06'/'10', o los `estados` que se indiquen: una boleta que
    nunca salió está en '02') como anulado ('05') con el motivo en DES_OBSE.
    Solo lo marca en la bandeja de SFS, que es lo que ven quienes consultan la API: no
    se comunica nada a SUNAT. El UPDATE lleva la condición de estado a propósito, igual
    que reiniciar_documento: si el documento ya cambió (p.ej. SUNAT lo aceptó) no se toca.
    """
    if not os.path.exists(bd_path):
        return False
    with closing(sqlite3.connect(bd_path, timeout=10)) as conexion:
        with conexion:
            cursor = conexion.execute(
                "UPDATE DOCUMENTO SET IND_SITU='05', DES_OBSE=? "
                f"WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=? AND IND_SITU IN ({','.join('?' * len(estados))})",
                (motivo[:250], ruc, tipo, numero, *estados),
            )
    return cursor.rowcount > 0


def descripcion_error(bd_path: str, codigo: str | None) -> str | None:
    """
    Qué significa un código de error de SUNAT (p.ej. '2800'), según el catálogo que
    SFS lleva en su propia base (tabla ERROR: ~1600 códigos con su texto oficial).
    None si no hay código, no está en el catálogo o no se puede leer: la descripción
    es una ayuda, nunca debe romper una respuesta.
    """
    if not codigo or not os.path.exists(bd_path):
        return None
    try:
        with closing(sqlite3.connect(bd_path)) as conexion:
            fila = conexion.execute(
                "SELECT NOM_CATAERRO FROM ERROR WHERE COD_CATAERRO=?", (codigo,)
            ).fetchone()
    except sqlite3.Error:
        return None
    return fila[0] if fila else None


def obtener_xml_firmado(firma_dir: str, ruc: str, tipo: str, numero: str) -> str | None:
    """
    El XML firmado que SFS dejó en FIRMA/ — lo que se envía a SUNAT (o, en una boleta,
    lo que se entrega al cliente y va dentro del resumen) — como texto, o None si
    todavía no se firmó o no se puede leer. Se valida que sea XML bien formado antes
    de devolverlo, igual que el CDR.
    """
    ruta = os.path.join(firma_dir, f"{_nombre_base(ruc, tipo, numero)}.xml")
    try:
        with open(ruta, "rb") as fh:
            contenido = fh.read()
        ET.fromstring(contenido)
    except (OSError, ET.ParseError):
        return None
    return contenido.decode("utf-8", errors="replace")


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
