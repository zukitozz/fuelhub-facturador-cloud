"""
API REST multiempresa: recibe un comprobante, lo hace firmar y enviar por la
instancia de SFS de esa empresa, y permite consultar después el XML de respuesta
de SUNAT (el CDR). Sin base de datos propia — el estado vive en la SQLite y las
carpetas que SFS ya mantiene por su cuenta (ver consultas.py).

Correr con: uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import logging

from fastapi import FastAPI, HTTPException

from . import archivos, configuracion, consultas, estados, qr, resumenes, sfs_cliente, ticket_resumen
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
        escrito = archivos.escribir_comprobante(comprobante, ruc, empresa.data_dir)
    except ValueError as e:
        # Dato de entrada incompleto (p.ej. nota sin 'referencia') — responsabilidad
        # de quien llama, no un fallo del servicio.
        raise HTTPException(status_code=422, detail=str(e)) from e

    tipo = comprobante.tipo_comprobante
    numero = comprobante.numeracion_comprobante
    codigo = f"{tipo}-{numero}"

    try:
        if tipo in sfs_cliente.TIPOS_SIN_ENVIO_INDIVIDUAL:
            # Boleta: se genera/firma y se deja en la bandeja de SFS, pero NO se
            # envía sola — ver sfs_cliente.generar_y_enviar para el porqué. El
            # resumen diario (POST /empresas/{ruc}/resumenes-diarios) es quien de
            # verdad la envía, agrupada con las demás boletas pendientes.
            fila = sfs_cliente.generar(empresa.sfs_base_url, ruc, tipo, numero)
        else:
            fila = sfs_cliente.generar_y_enviar(empresa.sfs_base_url, ruc, tipo, numero)
    except sfs_cliente.SfsError as e:
        logger.error("SFS no disponible para %s (%s): %s", ruc, escrito["base"], e)
        # 502: el comprobante YA quedó escrito en DATA (ver 'escrito' arriba) — un
        # reintento posterior de GET/alguna operación de soporte puede recuperarlo
        # sin que el caller tenga que volver a mandar el payload.
        raise HTTPException(status_code=502, detail=f"SFS no disponible: {e}") from e

    ind_situ = fila.get("ind_situ", "")
    hash_, qr_str = None, None
    if estados.firmado(ind_situ):
        hash_ = qr.extraer_hash(empresa.firma_dir, ruc, tipo, numero)
        if hash_:
            serie, correlativo = numero.split("-", 1) if "-" in numero else ("0000", numero)
            qr_str = qr.construir(
                ruc_emisor=ruc, tipo=tipo, serie=serie, correlativo=correlativo,
                igv=escrito["igv"], total=escrito["total"],
                fecha_emision=comprobante.fecha_emision.strftime("%Y-%m-%d"),
                tipo_doc_receptor=comprobante.receptor.tipo_documento,
                num_doc_receptor=comprobante.receptor.numero_documento,
                hash_=hash_,
            )

    return ComprobanteAceptado(
        codigo=codigo,
        estado=estados.legible(ind_situ),
        des_obse=fila.get("des_obse", ""),
        hash=hash_,
        qr=qr_str,
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

    estado_sfs = consultas.consultar_estado(empresa.bd_path, ruc, tipo, numero)
    if estado_sfs is None:
        raise HTTPException(status_code=404, detail=f"No existe el comprobante {codigo} para el RUC {ruc}.")

    if tipo == "RC" and estado_sfs["ind_situ"] not in consultas._ESTADOS_CON_CDR:
        # El resumen diario es asíncrono en SUNAT (ticket, no CDR inmediato) — si
        # ya hay veredicto, esto lo resuelve acá mismo. Ver app/ticket_resumen.py
        # para el porqué no lo hace SFS solo.
        diagnostico = ticket_resumen.resolver_ticket_pendiente(empresa, ruc, numero)
        if diagnostico:
            logger.info("Resumen %s-%s: %s", ruc, numero, diagnostico)
            estado_sfs = consultas.consultar_estado(empresa.bd_path, ruc, tipo, numero)

    cdr_xml = None
    if estado_sfs["ind_situ"] in consultas._ESTADOS_CON_CDR:
        cdr_xml = consultas.obtener_cdr_xml(
            empresa.rpta_dir, empresa.procesados_dir, ruc, tipo, numero
        )

    return EstadoComprobante(
        codigo=codigo,
        estado=estados.legible(estado_sfs["ind_situ"]),
        des_obse=estado_sfs["des_obse"],
        fec_gene=estado_sfs["fec_gene"],
        fec_envi=estado_sfs["fec_envi"],
        cdr_xml=cdr_xml,
    )


@app.post("/empresas/{ruc}/resumenes-diarios")
def crear_resumen_diario(ruc: str):
    """
    Arma y envía el resumen diario (RC) con las boletas de días anteriores que
    quedaron generadas y a la espera (ver sfs_cliente.TIPOS_SIN_ENVIO_INDIVIDUAL).
    Pensado para dispararse una vez al día (cron, o a mano) — no hace nada malo
    si se llama de más: sin boletas pendientes, responde sin crear un RC vacío.
    """
    empresa = _empresa_o_404(ruc)
    try:
        return resumenes.armar_y_enviar_resumen(empresa, ruc)
    except resumenes.SinPendientes:
        return {"mensaje": "No hay boletas de días anteriores pendientes de resumir."}
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except sfs_cliente.SfsError as e:
        logger.error("SFS no disponible armando resumen para %s: %s", ruc, e)
        raise HTTPException(status_code=502, detail=f"SFS no disponible: {e}") from e
