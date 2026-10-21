#!/usr/bin/env python3
"""
Limpieza diaria de los logs de SFS: conserva solo lo que sirve para analizar un
comprobante con error y borra el resto.

SFS escribe en DEBUG (300-450 MB por día en las empresas activas) y cada
comprobante son decenas de líneas mezcladas, así que el log completo sirve de poco
una vez que el día cerró bien. Lo que sí vale la pena guardar es el contexto de los
comprobantes que fallaron. Cuáles fallaron lo sabe la API (rechazos.jsonl y la
tabla de reintentos), no hay que adivinarlo leyendo el log.

Qué hace, por empresa:
  1) Toma los archivos cerrados del log (<ruta_base>/facturador-AAAA-MM-DD.log, que
     SFS rota a medianoche UTC). Nunca toca el facturador.log del día, que sigue
     abierto.
  2) Para cada comprobante que alguna vez falló (rechazos + reintentos, cualquier
     estado: ahí entran los error, rechazado, manual, anulado y los resueltos tras
     reintentar), extrae del log las líneas de su contexto a
     <ruta_base>/logs_errores/AAAA-MM-DD/<numeración>.log, con la clave SOL tachada.
  3) Borra el log completo del día, solo si la extracción terminó sin error.
  4) Borra de logs_errores/ lo que tenga más de DIAS_RETENCION_ERRORES días.

La extracción es por contexto, no exacta: el log no repite el número en cada línea
(el SOAP de SUNAT y los stack traces no lo traen). Cada vez que el log marca el inicio
de la generación o del envío de un comprobante con error (_ANCLAS), se guardan las
siguientes LINEAS_POSTERIORES líneas, o hasta que empiece el envío de otro comprobante.
Un comprobante guarda como máximo MAX_LINEAS_POR_COMPROBANTE líneas.

Por seguridad NO hace nada por defecto: sin --aplicar solo informa qué guardaría y
qué borraría. Tampoco está en el cron: hay que agregarlo a mano, después de revisar
una corrida en simulación.

No cubre los logs de PM2 (~/.pm2/logs): duplican a SFS y se rotan con pm2-logrotate.

Uso:
    python3 deploy/mantenimiento_logs.py            # simulación
    python3 deploy/mantenimiento_logs.py --aplicar  # guarda y borra
"""
import argparse
import glob
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import configuracion, rechazos, reintentos  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DIAS_RETENCION_ERRORES = 15
LINEAS_POSTERIORES = 300  # de un envío: la respuesta de SUNAT llega ~60-120 líneas después
MAX_LINEAS_POR_COMPROBANTE = 3000
# Solo estas líneas abren el contexto de un comprobante: el inicio de su generación o de
# su envío. SFS relee su bandeja cada pocos segundos y deja cada archivo pendiente en el
# log miles de veces (cargarArchivosContribuyente...); si esas menciones abrieran
# contexto, un comprobante atascado terminaría guardando casi el log entero.
_ANCLAS = ("Enviando archivo a SUNAT, filename", "getNum_docu() :", "Numero ticket")
_NUMERACION = re.compile(r"^(?:[A-Z0-9]{4}-\d{1,8}|RC-\d{8}-\d{3})$")

_SEG_POR_DIA = 86400
_LOG_DIARIO = re.compile(r"^facturador-(\d{4}-\d{2}-\d{2})\.log$")
# La clave SOL sale en claro en el log de SFS ("password:  xxx" y <wsse:Password>).
_CLAVE_LINEA = re.compile(r"(password:\s+)\S+", re.IGNORECASE)
_CLAVE_XML = re.compile(r"(<wsse:Password[^>]*>)[^<]*", re.IGNORECASE)


def _tachar(linea: str) -> str:
    return _CLAVE_XML.sub(r"\1***", _CLAVE_LINEA.sub(r"\1***", linea))


def _necesarios(ruc: str) -> set[str]:
    """Numeraciones (F003-013072, RC-20261020-003) de todo comprobante que falló alguna vez."""
    codigos = {e.get("codigo") for e in rechazos.listar(ruc, limite=10**9)}
    codigos |= {f["codigo"] for f in reintentos.listar(ruc)}
    # "01-F003-013072" -> "F003-013072"; "RC-RC-20261020-003" -> "RC-20261020-003"
    numeraciones = {c.split("-", 1)[1] for c in codigos if c and "-" in c}
    return {n for n in numeraciones if _NUMERACION.match(n)}


def _extraer(ruta_log: str, numeraciones: set[str]) -> dict[str, list[str]]:
    """Recorre el log una vez (en streaming: son cientos de MB) y arma, por numeración,
    las líneas de su contexto."""
    if not numeraciones:
        return {}
    patron = re.compile("|".join(re.escape(n) for n in sorted(numeraciones, key=len, reverse=True)))
    pendientes: dict[str, int] = {}  # numeración -> cuántas líneas posteriores faltan
    salida: dict[str, list[str]] = {}
    with open(ruta_log, "r", encoding="utf-8", errors="replace") as fh:
        for linea in fh:
            if any(a in linea for a in _ANCLAS):
                # Empieza otro envío/generación: se cierra lo que seguía abierto y se
                # abre solo para los comprobantes con error que menciona.
                pendientes = {n: LINEAS_POSTERIORES for n in set(patron.findall(linea))}
            for n in list(pendientes):
                lineas = salida.setdefault(n, [])
                if len(lineas) < MAX_LINEAS_POR_COMPROBANTE:
                    lineas.append(_tachar(linea))
                pendientes[n] -= 1
                if pendientes[n] <= 0:
                    del pendientes[n]
    return salida


def _procesar_log(empresa, ruta_log: str, dia: str, numeraciones: set[str], aplicar: bool) -> None:
    tamano_mb = os.path.getsize(ruta_log) / 1048576
    contexto = _extraer(ruta_log, numeraciones)
    logger.info("%s %s (%.0f MB): %d comprobante(s) con error para conservar",
                empresa.ruc, os.path.basename(ruta_log), tamano_mb, len(contexto))
    if not aplicar:
        for n in sorted(contexto):
            logger.info("  [simulación] guardaría %s (%d líneas)", n, len(contexto[n]))
        logger.info("  [simulación] borraría %s", ruta_log)
        return
    destino = os.path.join(empresa.ruta_base, "logs_errores", dia)
    if contexto:
        os.makedirs(destino, exist_ok=True)
        for n, lineas in contexto.items():
            with open(os.path.join(destino, f"{n}.log"), "w", encoding="utf-8") as fh:
                fh.writelines(lineas)
    os.remove(ruta_log)
    logger.info("  borrado %s", ruta_log)


def _vencidos(empresa, aplicar: bool) -> None:
    base = os.path.join(empresa.ruta_base, "logs_errores")
    ahora = time.time()
    for carpeta in glob.glob(os.path.join(base, "*")):
        if not os.path.isdir(carpeta):
            continue
        if (ahora - os.path.getmtime(carpeta)) / _SEG_POR_DIA < DIAS_RETENCION_ERRORES:
            continue
        if aplicar:
            shutil.rmtree(carpeta)
            logger.info("%s: borrado %s (más de %d días)", empresa.ruc, carpeta, DIAS_RETENCION_ERRORES)
        else:
            logger.info("%s: [simulación] borraría %s (más de %d días)", empresa.ruc, carpeta, DIAS_RETENCION_ERRORES)


def procesar_empresa(empresa, aplicar: bool) -> None:
    hoy_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    numeraciones = _necesarios(empresa.ruc)
    for ruta in sorted(glob.glob(os.path.join(empresa.ruta_base, "facturador-*.log"))):
        m = _LOG_DIARIO.match(os.path.basename(ruta))
        if not m or m.group(1) >= hoy_utc:
            continue  # el del día en curso (UTC) puede seguir abierto
        try:
            _procesar_log(empresa, ruta, m.group(1), numeraciones, aplicar)
        except OSError:
            logger.exception("%s: no se pudo procesar %s; no se borra", empresa.ruc, ruta)
    _vencidos(empresa, aplicar)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--aplicar", action="store_true",
                        help="guardar los contextos de error y borrar los logs; sin esto solo informa")
    args = parser.parse_args()
    if not args.aplicar:
        logger.info("MODO SIMULACIÓN: no se escribe ni se borra nada (usar --aplicar).")
    for empresa in configuracion.listar_empresas():
        procesar_empresa(empresa, args.aplicar)


if __name__ == "__main__":
    main()
