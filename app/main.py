"""
API REST multiempresa: recibe un comprobante, lo hace firmar y enviar por la
instancia de SFS de esa empresa, y permite consultar después el XML de respuesta
de SUNAT (el CDR). Sin base de datos propia — el estado vive en la SQLite y las
carpetas que SFS ya mantiene por su cuenta (ver consultas.py).

Correr con: uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import logging
from datetime import date
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError

from . import archivos, configuracion, consulta_integrada, consultas, estados, qr, rechazos, reenvio, resumenes, sfs_cliente, ticket_resumen
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


@app.exception_handler(RequestValidationError)
async def _registrar_validacion(request: Request, exc: RequestValidationError):
    """El payload ni llegó al handler (campo faltante, tipo incorrecto): FastAPI
    responde 422 por su cuenta. Se anota igual, para que soporte vea qué mandó mal
    cada sistema. Solo para empresas dadas de alta: con cualquier otro RUC cualquiera
    podría llenar el archivo de basura."""
    ruc = request.path_params.get("ruc")
    if (request.method == "POST" and request.url.path.endswith("/comprobantes")
            and ruc and configuracion.obtener_empresa(ruc)):
        cuerpo = exc.body if isinstance(exc.body, dict) else {}
        errores = [
            {"campo": ".".join(str(p) for p in e["loc"] if p != "body"), "msg": e["msg"]}
            for e in exc.errors()
        ]
        rechazos.registrar(
            ruc, str(cuerpo.get("numeracion_comprobante") or "(sin numeración)"), "invalido",
            "; ".join(f"{e['campo']}: {e['msg']}" for e in errores), "validacion", detalle=errores,
        )
    return await request_validation_exception_handler(request, exc)


_INCLUIR_VALIDOS = {"xml", "cdr"}


def _incluir_o_400(incluir: Optional[str]) -> set:
    """?incluir=xml,cdr -> {"xml", "cdr"}. Un valor desconocido es un 400, no se
    ignora en silencio: quien escribe 'xlm' esperaría el XML y recibiría null."""
    pedido = {p.strip().lower() for p in (incluir or "").split(",") if p.strip()}
    desconocidos = pedido - _INCLUIR_VALIDOS
    if desconocidos:
        raise HTTPException(
            status_code=400,
            detail=f"incluir desconoce {sorted(desconocidos)}; valores válidos: {sorted(_INCLUIR_VALIDOS)}",
        )
    return pedido


def _adjuntos(empresa, ruc: str, tipo: str, numero: str, ind_situ: str, pedido: set):
    """(xml, cdr) de un comprobante: el XML firmado desde que SFS lo firma, el CDR
    (respuesta de SUNAT) desde que llega. Solo lo que esté en `pedido` y ya exista;
    lo demás, None. Ambos en texto plano."""
    xml = (consultas.obtener_xml_firmado(empresa.firma_dir, ruc, tipo, numero)
           if "xml" in pedido and estados.firmado(ind_situ) else None)
    cdr = (consultas.obtener_cdr_xml(empresa.rpta_dir, empresa.procesados_dir, ruc, tipo, numero)
           if "cdr" in pedido and ind_situ in consultas._ESTADOS_CON_CDR else None)
    return xml, cdr


@app.get("/salud")
def salud():
    return {"estado": "ok"}


@app.post(
    "/empresas/{ruc}/comprobantes",
    response_model=ComprobanteAceptado,
    status_code=201,
)
def crear_comprobante(
    ruc: str,
    comprobante: ComprobanteEntrada,
    incluir: Optional[str] = Query(
        None, description="xml y/o cdr, separados por coma: adjunta el XML firmado y/o el CDR en la respuesta"),
):
    pedido = _incluir_o_400(incluir)
    empresa = _empresa_o_404(ruc)

    try:
        escrito = archivos.escribir_comprobante(comprobante, ruc, empresa.data_dir)
    except ValueError as e:
        # Dato de entrada incompleto (p.ej. nota sin 'referencia') — responsabilidad
        # de quien llama, no un fallo del servicio.
        rechazos.registrar(
            ruc, f"{comprobante.tipo_comprobante}-{comprobante.numeracion_comprobante}",
            "invalido", str(e), "validacion",
        )
        raise HTTPException(status_code=422, detail=str(e)) from e

    tipo = comprobante.tipo_comprobante
    numero = comprobante.numeracion_comprobante
    codigo = f"{tipo}-{numero}"

    # Un reintento de algo que quedó en error o rechazado: se limpia en SFS para que el
    # reenvío sea real (si no, SFS devuelve el mismo estado viejo sin enviar nada), y un
    # error con riesgo de duplicado se verifica antes con SUNAT. Ver app/reenvio.py.
    accion, detalle_previo = reenvio.preparar(
        empresa, ruc, tipo, numero, comprobante.fecha_emision, escrito["total"])
    if accion == reenvio.YA_EN_SUNAT:
        return ComprobanteAceptado(
            codigo=codigo, estado="enviado",
            des_obse=f"SUNAT ya registró este comprobante ({detalle_previo}); no reenviar.",
        )
    if accion == reenvio.INDETERMINADO:
        rechazos.registrar(ruc, codigo, "no_verificable", detalle_previo, "red")
        raise HTTPException(
            status_code=502,
            detail=f"El comprobante está en error y no se pudo verificar en SUNAT si ya lo tiene "
                   f"({detalle_previo}); no se reenvía a ciegas. Reintentar más tarde.",
        )

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
        rechazos.registrar(ruc, codigo, "no_disponible", str(e), "red")
        # El corte pudo ocurrir después de que SUNAT recibió el comprobante: antes de
        # que el caller reintente (y duplique), se le pregunta a SUNAT.
        if tipo not in sfs_cliente.TIPOS_SIN_ENVIO_INDIVIDUAL:
            serie, corr = numero.split("-", 1) if "-" in numero else ("0000", numero)
            estado_sunat, detalle = consulta_integrada.consultar_comprobante(
                empresa, ruc, tipo, serie, corr, comprobante.fecha_emision, escrito["total"])
            if estado_sunat == consulta_integrada.ACEPTADO:
                logger.warning("%s-%s: SFS falló pero SUNAT ya lo tiene; no se reenvía.", ruc, codigo)
                return ComprobanteAceptado(
                    codigo=codigo, estado="enviado",
                    des_obse="SUNAT ya registró este comprobante (CDR pendiente de descarga); no reenviar.",
                )
            raise HTTPException(
                status_code=502,
                detail=f"SFS no disponible: {e} (consulta a SUNAT: {estado_sunat}, {detalle})",
            ) from e
        # 502: el comprobante YA quedó escrito en DATA (ver 'escrito' arriba) — un
        # reintento posterior de GET/alguna operación de soporte puede recuperarlo
        # sin que el caller tenga que volver a mandar el payload.
        raise HTTPException(status_code=502, detail=f"SFS no disponible: {e}") from e

    ind_situ = fila.get("ind_situ", "")
    estado = estados.legible(ind_situ)
    codigo_error = descripcion_error = None
    if estado in rechazos.ESTADOS_RECHAZO:
        codigo_error = rechazos.extraer_codigo(fila.get("des_obse", ""))
        descripcion_error = consultas.descripcion_error(empresa.bd_path, codigo_error)
        logger.warning("%s %s: %s (%s)", ruc, codigo, estado, fila.get("des_obse", ""))
        rechazos.registrar(ruc, codigo, estado, fila.get("des_obse", ""),
                           "sunat" if estado == "rechazado" else "sfs",
                           descripcion_error=descripcion_error)
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

    # Se piden en este mismo POST porque en una BOLETA no hay otra oportunidad: una vez
    # que el resumen la consume, su fila sale de la bandeja de SFS y el GET da 404.
    xml, cdr = _adjuntos(empresa, ruc, tipo, numero, ind_situ, pedido)

    return ComprobanteAceptado(
        codigo=codigo,
        estado=estado,
        des_obse=fila.get("des_obse", ""),
        hash=hash_,
        qr=qr_str,
        codigo_error=codigo_error,
        descripcion_error=descripcion_error,
        xml=xml,
        cdr=cdr,
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

    # El GET siempre adjunta ambos (cuando ya existen): lo usa quien necesita archivar
    # el comprobante una vez aceptado, y pesan pocos KB.
    xml, cdr = _adjuntos(empresa, ruc, tipo, numero, estado_sfs["ind_situ"], _INCLUIR_VALIDOS)

    estado = estados.legible(estado_sfs["ind_situ"])
    codigo_error = descripcion_error = None
    if estado in rechazos.ESTADOS_RECHAZO:
        codigo_error = rechazos.extraer_codigo(estado_sfs["des_obse"])
        descripcion_error = consultas.descripcion_error(empresa.bd_path, codigo_error)
        # Cubre lo que no pasó por un POST (un resumen resuelto por ticket, o un
        # rechazo anterior a este registro); no repite el que ya se anotó.
        rechazos.registrar(ruc, codigo, estado, estado_sfs["des_obse"], "consulta",
                           descripcion_error=descripcion_error)

    return EstadoComprobante(
        codigo=codigo,
        estado=estado,
        des_obse=estado_sfs["des_obse"],
        fec_gene=estado_sfs["fec_gene"],
        fec_envi=estado_sfs["fec_envi"],
        codigo_error=codigo_error,
        descripcion_error=descripcion_error,
        xml=xml,
        cdr=cdr,
    )


@app.get("/empresas/{ruc}/rechazos")
def listar_rechazos(
    ruc: str,
    desde: Optional[date] = Query(None, description="Solo desde este día (hora de Lima), AAAA-MM-DD"),
    origen: Optional[str] = Query(None, description="sunat | sfs | validacion | red | resumen | consulta"),
    limite: int = Query(100, ge=1, le=1000),
):
    """
    Lo que salió mal con los comprobantes de esta empresa, lo más reciente primero:
    rechazos de SUNAT, fallos de SFS, payloads inválidos y cortes. Es el registro que
    no se pisa cuando un comprobante se corrige y se reenvía bien. Ver app/rechazos.py.
    """
    _empresa_o_404(ruc)
    items = rechazos.listar(ruc, desde=desde, origen=origen, limite=limite)
    return {"ruc": ruc, "total": len(items), "rechazos": items}


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
        resultado = resumenes.armar_y_enviar_resumen(empresa, ruc)
        if resultado["estado"] in rechazos.ESTADOS_RECHAZO:
            # Las boletas van en el detalle: es lo que permite saber cuáles reencolar
            rechazos.registrar(ruc, resultado["numeracion_rc"], resultado["estado"],
                               resultado["des_obse"], "resumen",
                               detalle={"boletas": resultado["boletas"]},
                               descripcion_error=resultado["descripcion_error"])
        return resultado
    except resumenes.SinPendientes:
        return {"mensaje": "No hay boletas de días anteriores pendientes de resumir."}
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except sfs_cliente.SfsError as e:
        logger.error("SFS no disponible armando resumen para %s: %s", ruc, e)
        raise HTTPException(status_code=502, detail=f"SFS no disponible: {e}") from e
