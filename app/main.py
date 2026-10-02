"""
API REST multiempresa: recibe un comprobante, lo hace firmar y enviar por la
instancia de SFS de esa empresa, y permite consultar después el XML de respuesta
de SUNAT (el CDR). Sin base de datos propia — el estado vive en la SQLite y las
carpetas que SFS ya mantiene por su cuenta (ver consultas.py).

Correr con: uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import logging

from fastapi import FastAPI, HTTPException

from . import archivos, configuracion, consultas, sfs_cliente
from .esquemas import ComprobanteAceptado, ComprobanteEntrada, EstadoComprobante

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="facturador-api",
    description="Recibe comprobantes, los firma y envía a SUNAT vía SFS, y expone el CDR de respuesta.",
)


def _empresa_o_404(ruc: str):
    empresa = configuracion.obtener_empresa(ruc)
    if empresa is None:
        raise HTTPException(status_code=404, detail=f"RUC {ruc} no está dado de alta.")
    return empresa


@app.get("/salud")
def salud():
    return {"estado": "ok"}


@app.post(
    "/empresas/{ruc}/comprobantes",
    response_model=ComprobanteAceptado,
    status_code=201,
)
def crear_comprobante(ruc: str, comprobante: ComprobanteEntrada):
    empresa = _empresa_o_404(ruc)

    try:
        base = archivos.escribir_comprobante(comprobante, ruc, empresa.data_dir)
    except ValueError as e:
        # Dato de entrada incompleto (p.ej. nota sin 'referencia') — responsabilidad
        # de quien llama, no un fallo del servicio.
        raise HTTPException(status_code=422, detail=str(e)) from e

    tipo = comprobante.tipo_comprobante
    numero = comprobante.numeracion_comprobante
    codigo = f"{tipo}-{numero}"

    try:
        fila = sfs_cliente.generar_y_enviar(empresa.sfs_base_url, ruc, tipo, numero)
    except sfs_cliente.SfsError as e:
        logger.error("SFS no disponible para %s (%s): %s", ruc, base, e)
        # 502: el comprobante YA quedó escrito en DATA (ver 'base' arriba) — un
        # reintento posterior de GET/alguna operación de soporte puede recuperarlo
        # sin que el caller tenga que volver a mandar el payload.
        raise HTTPException(status_code=502, detail=f"SFS no disponible: {e}") from e

    return ComprobanteAceptado(
        codigo=codigo,
        ind_situ=fila.get("ind_situ", ""),
        des_obse=fila.get("des_obse", ""),
    )


@app.get(
    "/empresas/{ruc}/comprobantes/{codigo}",
    response_model=EstadoComprobante,
)
def consultar_comprobante(ruc: str, codigo: str):
    empresa = _empresa_o_404(ruc)

    if "-" not in codigo:
        raise HTTPException(status_code=400, detail="Formato esperado: {tipo}-{numeracion}, ej. 01-F002-000001")
    tipo, numero = codigo.split("-", 1)

    estado = consultas.consultar_estado(empresa.bd_path, ruc, tipo, numero)
    if estado is None:
        raise HTTPException(status_code=404, detail=f"No existe el comprobante {codigo} para el RUC {ruc}.")

    cdr_xml = None
    if estado["ind_situ"] in consultas._ESTADOS_CON_CDR:
        cdr_xml = consultas.obtener_cdr_xml(
            empresa.rpta_dir, empresa.procesados_dir, ruc, tipo, numero
        )

    return EstadoComprobante(codigo=codigo, cdr_xml=cdr_xml, **estado)
