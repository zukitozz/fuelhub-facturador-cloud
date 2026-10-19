"""
RUC de pruebas: 20000000001 NO se da de alta ni pasa por SFS ni por SUNAT. Cualquier
comprobante que se le mande se acepta al instante, y el GET devuelve siempre un
CDR "aceptado" falso. Sirve para probar la integración de punta a punta (payload,
estados, QR, XML, CDR) sin certificado, sin clave SOL y sin tocar nada real.

Es sin estado a propósito: no guarda nada, así que el GET responde aceptado para
cualquier código, haya o no un POST antes. Nada de esto se escribe en disco ni en
rechazos.json.
"""
import hashlib
from datetime import datetime

from . import qr
from .esquemas import ComprobanteAceptado, ComprobanteEntrada, EstadoComprobante

RUC_SIMULADO = "20000000001"
_DES_OBSE = "[SIMULADO] El comprobante ha sido aceptado"


def es_simulado(ruc: str) -> bool:
    return ruc == RUC_SIMULADO


def _cdr(tipo: str, numero: str) -> str:
    ahora = datetime.now()
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<ar:ApplicationResponse xmlns:ar="urn:oasis:names:specification:ubl:schema:xsd:ApplicationResponse-2" xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
<cbc:UBLVersionID>2.0</cbc:UBLVersionID>
<cbc:CustomizationID>1.0</cbc:CustomizationID>
<cbc:ID>{int(ahora.timestamp())}</cbc:ID>
<cbc:IssueDate>{ahora:%Y-%m-%d}</cbc:IssueDate>
<cbc:IssueTime>{ahora:%H:%M:%S}</cbc:IssueTime>
<cbc:ResponseDate>{ahora:%Y-%m-%d}</cbc:ResponseDate>
<cbc:ResponseTime>{ahora:%H:%M:%S}</cbc:ResponseTime>
<cac:SenderParty><cac:PartyIdentification><cbc:ID>20131312955</cbc:ID></cac:PartyIdentification></cac:SenderParty>
<cac:ReceiverParty><cac:PartyIdentification><cbc:ID>{RUC_SIMULADO}</cbc:ID></cac:PartyIdentification></cac:ReceiverParty>
<cac:DocumentResponse>
<cac:Response>
<cbc:ReferenceID>{numero}</cbc:ReferenceID>
<cbc:ResponseCode>0</cbc:ResponseCode>
<cbc:Description>[SIMULADO] El comprobante {numero} ha sido aceptado</cbc:Description>
</cac:Response>
</cac:DocumentResponse>
</ar:ApplicationResponse>"""


def _xml_firmado(tipo: str, numero: str, hash_: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<!-- [SIMULADO] XML sin valor tributario -->
<Invoice><cbc:ID xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">{numero}</cbc:ID>
<ds:Signature xmlns:ds="http://www.w3.org/2000/09/xmldsig#"><ds:SignedInfo><ds:Reference><ds:DigestValue>{hash_}</ds:DigestValue></ds:Reference></ds:SignedInfo></ds:Signature></Invoice>"""


def _hash(tipo: str, numero: str) -> str:
    return hashlib.sha1(f"{RUC_SIMULADO}-{tipo}-{numero}".encode()).hexdigest()


def aceptar(comprobante: ComprobanteEntrada, pedido: set) -> ComprobanteAceptado:
    tipo = comprobante.tipo_comprobante
    numero = comprobante.numeracion_comprobante
    serie, correlativo = numero.split("-", 1) if "-" in numero else ("0000", numero)
    igv = comprobante.igv if comprobante.igv is not None else round(comprobante.total - comprobante.total / 1.18, 2)
    hash_ = _hash(tipo, numero)
    return ComprobanteAceptado(
        codigo=f"{tipo}-{numero}",
        estado="aceptado",
        des_obse=_DES_OBSE,
        hash=hash_,
        qr=qr.construir(
            ruc_emisor=RUC_SIMULADO, tipo=tipo, serie=serie, correlativo=correlativo,
            igv=igv, total=comprobante.total,
            fecha_emision=comprobante.fecha_emision.strftime("%Y-%m-%d"),
            tipo_doc_receptor=comprobante.receptor.tipo_documento,
            num_doc_receptor=comprobante.receptor.numero_documento,
            hash_=hash_,
        ),
        xml=_xml_firmado(tipo, numero, hash_) if "xml" in pedido else None,
        cdr=_cdr(tipo, numero) if "cdr" in pedido else None,
    )


def consultar(codigo: str) -> EstadoComprobante:
    tipo, numero = codigo.split("-", 1)
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return EstadoComprobante(
        codigo=codigo, estado="aceptado", des_obse=_DES_OBSE, fec_gene=ahora, fec_envi=ahora,
        xml=_xml_firmado(tipo, numero, _hash(tipo, numero)), cdr=_cdr(tipo, numero),
    )
