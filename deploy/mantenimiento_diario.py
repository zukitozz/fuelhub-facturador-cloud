#!/usr/bin/env python3
"""
Mantenimiento diario de disco: archiva y, después, borra en serio los archivos
de los comprobantes individuales (factura, nota, resumen) que SUNAT ya cerró.

Por qué hace falta: a diferencia de la boleta, que se borra sola de DATA/ y de
DOCUMENTO en el momento en que entra a un resumen (ver app/resumenes.py),
factura/nota/el propio RC nunca se limpian solos — sus archivos en DATA/ y el
.zip del CDR en RPTA/(procesados/) se quedan ahí para siempre. Con bajo volumen
no importa; con del orden de 10 mil comprobantes/mes por empresa, en pocos
meses eso son cientos de miles de archivos sueltos.

No sube nada a almacenamiento externo (ni S3 ni Lightsail Bucket): quien emite
los comprobantes ya los guarda en su propia base — acá alcanza con un colchón
local corto antes de borrar en serio, para absorber el caso de necesitar
revisar algo reciente sin tener que ir a buscarlo a otro lado.

Dos fases, cada umbral contado en días desde que SUNAT aceptó el documento
(se usa el mtime del .zip del CDR como fecha de cierre, no la de emisión):
  1) A los DIAS_HASTA_FRIO días: empaqueta los archivos de DATA + el CDR en un
     .tar.gz dentro de <ruta_base>/archivo_frio/, y borra los originales.
  2) A los DIAS_HASTA_BORRAR días de estar en archivo_frio/ (≈15 desde que
     cerró, con los valores por defecto), borra el .tar.gz definitivamente.

La fila en DOCUMENTO de SFS NUNCA se borra acá (decisión explícita): el estado
(aceptado/rechazado, fechas) sigue pudiéndose consultar por
GET /comprobantes/{codigo} aunque cdr_xml ya dé null por no quedar archivo.

El diseño es re-entrante sin necesitar una tabla propia de "ya procesado": una
vez que el CDR se archivó, _ruta_cdr() ya no lo encuentra en DATA/RPTA, así que
la próxima corrida simplemente lo salta.

Pensado para correr una vez al día por cron (los umbrales son en días; correrlo
con menos frecuencia los pasa de largo igual, pero sin el escalonado fino que
se buscó al definirlos). Uso:
    python3 deploy/mantenimiento_diario.py
"""
import glob
import logging
import os
import sqlite3
import sys
import tarfile
import time
from contextlib import closing

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import configuracion, consultas  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DIAS_HASTA_FRIO = 7
DIAS_HASTA_BORRAR = 8  # contados desde que entra a archivo_frio/, no desde el cierre

# La boleta no entra acá: ya se borra sola al consumirse en un resumen (ver
# app/resumenes.py) — para cuando esto corre, ya no queda ninguna fila suya en
# DOCUMENTO que mirar.
_TIPOS_A_MANTENER = ("01", "07", "08", "RC")

_SEG_POR_DIA = 86400


def _documentos_cerrados(bd_path: str, ruc: str) -> list[tuple[str, str]]:
    if not os.path.exists(bd_path):
        return []
    marcas_tipo = ",".join("?" * len(_TIPOS_A_MANTENER))
    marcas_estado = ",".join("?" * len(consultas._ESTADOS_CON_CDR))
    with closing(sqlite3.connect(bd_path)) as conexion:
        return conexion.execute(
            f"SELECT TIP_DOCU, NUM_DOCU FROM DOCUMENTO WHERE NUM_RUC=? "
            f"AND TIP_DOCU IN ({marcas_tipo}) AND IND_SITU IN ({marcas_estado})",
            (ruc, *_TIPOS_A_MANTENER, *consultas._ESTADOS_CON_CDR),
        ).fetchall()


def _ruta_cdr(empresa, ruc: str, tipo: str, numero: str) -> str | None:
    """Mismo orden de búsqueda que consultas.obtener_cdr_xml: procesados/
    primero (donde queda el CDR ya revisado), RPTA/ directo después."""
    nombre = f"R{ruc}-{tipo}-{numero}.zip"
    for carpeta in (empresa.procesados_dir, empresa.rpta_dir):
        ruta = os.path.join(carpeta, nombre)
        if os.path.exists(ruta):
            return ruta
    return None


def _archivos_data(empresa, ruc: str, tipo: str, numero: str) -> list[str]:
    return glob.glob(os.path.join(empresa.data_dir, f"{ruc}-{tipo}-{numero}.*"))


def _mover_a_frio(empresa, ruc: str, tipo: str, numero: str, ruta_cdr: str) -> None:
    archivos = _archivos_data(empresa, ruc, tipo, numero) + [ruta_cdr]
    frio_dir = os.path.join(empresa.ruta_base, "archivo_frio")
    os.makedirs(frio_dir, exist_ok=True)
    destino = os.path.join(frio_dir, f"{ruc}-{tipo}-{numero}.tar.gz")
    with tarfile.open(destino + ".tmp", "w:gz") as tar:
        for archivo in archivos:
            tar.add(archivo, arcname=os.path.basename(archivo))
    os.replace(destino + ".tmp", destino)
    for archivo in archivos:
        os.remove(archivo)
    logger.info("Archivado a frío: %s-%s (%d archivos)", tipo, numero, len(archivos))


def procesar_empresa(empresa) -> None:
    ahora = time.time()

    for tipo, numero in _documentos_cerrados(empresa.bd_path, empresa.ruc):
        ruta_cdr = _ruta_cdr(empresa, empresa.ruc, tipo, numero)
        if not ruta_cdr:
            continue  # ya archivado antes, o sin CDR en disco todavía
        edad_dias = (ahora - os.path.getmtime(ruta_cdr)) / _SEG_POR_DIA
        if edad_dias < DIAS_HASTA_FRIO:
            continue
        try:
            _mover_a_frio(empresa, empresa.ruc, tipo, numero, ruta_cdr)
        except OSError:
            logger.exception("No se pudo archivar %s-%s a frío", tipo, numero)

    frio_dir = os.path.join(empresa.ruta_base, "archivo_frio")
    for ruta in glob.glob(os.path.join(frio_dir, "*.tar.gz")):
        edad_dias = (ahora - os.path.getmtime(ruta)) / _SEG_POR_DIA
        if edad_dias < DIAS_HASTA_BORRAR:
            continue
        try:
            os.remove(ruta)
            logger.info("Borrado definitivo: %s", os.path.basename(ruta))
        except OSError:
            logger.exception("No se pudo borrar %s", ruta)


def main() -> None:
    for empresa in configuracion.listar_empresas():
        logger.info("Mantenimiento de %s (%s)...", empresa.ruc, empresa.nombre)
        procesar_empresa(empresa)


if __name__ == "__main__":
    main()
