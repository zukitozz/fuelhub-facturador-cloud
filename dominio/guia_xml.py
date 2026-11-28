"""
XML UBL 2.1 `DespatchAdvice` de una guía de remisión electrónica (GRE): remitente `09`
y transportista `31`. Devuelve el documento SIN firmar, con el hueco
`ext:UBLExtensions/ext:UBLExtension/ext:ExtensionContent` donde app/firma_xml.py inserta
la firma. Se arma con lxml (no con texto) para que el escapado de los datos sea siempre
correcto.

El orden de los elementos es el de la secuencia del esquema UBL (DespatchAdviceType,
ShipmentType...): SUNAT valida contra el XSD, así que un elemento fuera de lugar es un
rechazo aunque el dato sea correcto.

Quién va en cada rol (catálogo de la GRE de SUNAT):
  09 remitente    DespatchSupplierParty = remitente (el emisor), DeliveryCustomerParty =
                  destinatario, SellerSupplierParty = proveedor (solo motivo 02, compra).
  31 transportista DespatchSupplierParty = remitente (quien envía la carga),
                  DeliveryCustomerParty = destinatario, y el emisor (transportista) va en
                  Shipment/ShipmentStage/CarrierParty.
"""
from decimal import Decimal

from lxml import etree

NS = {
    None: "urn:oasis:names:specification:ubl:schema:xsd:DespatchAdvice-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
    "ext": "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2",
}
_CAC = "{%s}" % NS["cac"]
_CBC = "{%s}" % NS["cbc"]
_EXT = "{%s}" % NS["ext"]
_RAIZ = "{%s}DespatchAdvice" % NS[None]

ID_FIRMA = "SignatureSP"


def numero(valor) -> str:
    """Número sin notación científica ni ceros de relleno: 1900.0 -> '1900', 0.5 -> '0.5'."""
    texto = format(Decimal(str(valor)).normalize(), "f")
    return texto


def _sub(padre, nombre: str, texto=None, **atributos):
    """Hijo `cac:`/`cbc:`/`ext:` con texto y atributos opcionales (None se omite)."""
    prefijo, local = nombre.split(":")
    espacio = {"cac": _CAC, "cbc": _CBC, "ext": _EXT}[prefijo]
    el = etree.SubElement(padre, espacio + local)
    if texto is not None:
        el.text = str(texto)
    for clave, valor in atributos.items():
        if valor is not None:
            el.set(clave, str(valor))
    return el


def _parte_identificada(padre, rol: str, parte: dict):
    """<rol><cac:Party>… con documento y razón social."""
    r = _sub(padre, rol)
    p = _sub(r, "cac:Party")
    pid = _sub(p, "cac:PartyIdentification")
    _sub(pid, "cbc:ID", parte["numero_documento"], schemeID=parte.get("tipo_documento", "6"))
    le = _sub(p, "cac:PartyLegalEntity")
    _sub(le, "cbc:RegistrationName", parte["razon_social"])
    return r


def _direccion(padre, rol: str, d: dict):
    """DeliveryAddress / DespatchAddress: ubigeo, establecimiento anexo (opcional), línea."""
    contenedor = _sub(padre, rol)
    _sub(contenedor, "cbc:ID", d["ubigeo"], schemeAgencyName="PE:INEI", schemeName="Ubigeos")
    if d.get("codigo_establecimiento"):
        _sub(contenedor, "cbc:AddressTypeCode", d["codigo_establecimiento"],
             listAgencyName="PE:SUNAT", listName="Establecimientos anexos")
    linea = _sub(contenedor, "cac:AddressLine")
    _sub(linea, "cbc:Line", d["direccion"])


def construir(guia: dict, ruc_emisor: str, razon_social_emisor: str) -> bytes:
    """
    `guia` es el payload ya validado (ver app/guias.py) como dict: `tipo_guia` ya es
    "09" o "31", `fecha_emision` es datetime, `fecha_traslado` es date.
    """
    tipo = guia["tipo_guia"]
    es_remitente = tipo == "09"
    serie, correlativo = guia["numeracion"].split("-", 1)

    raiz = etree.Element(_RAIZ, nsmap=NS)

    ext = _sub(raiz, "ext:UBLExtensions")
    _sub(_sub(ext, "ext:UBLExtension"), "ext:ExtensionContent")

    _sub(raiz, "cbc:UBLVersionID", "2.1")
    _sub(raiz, "cbc:CustomizationID", "2.0")
    _sub(raiz, "cbc:ID", f"{serie}-{int(correlativo)}")
    _sub(raiz, "cbc:IssueDate", guia["fecha_emision"].strftime("%Y-%m-%d"))
    _sub(raiz, "cbc:IssueTime", guia["fecha_emision"].strftime("%H:%M:%S"))
    _sub(raiz, "cbc:DespatchAdviceTypeCode", tipo,
         listAgencyName="PE:SUNAT", listName="Tipo de Documento", listURI="urn:pe:gob:sunat:cpe:see:gem:catalogos:catalogo01")
    if guia.get("observaciones"):
        _sub(raiz, "cbc:Note", guia["observaciones"])

    # La 31 informa la 09 con la que viaja la carga.
    if not es_remitente and guia.get("guia_remitente"):
        s, c = guia["guia_remitente"].split("-", 1)
        ref = _sub(raiz, "cac:AdditionalDocumentReference")
        _sub(ref, "cbc:ID", f"{s}-{int(c)}")
        _sub(ref, "cbc:DocumentTypeCode", "09", listAgencyName="PE:SUNAT",
             listName="Tipo de Documento", listURI="urn:pe:gob:sunat:cpe:see:gem:catalogos:catalogo01")
        _sub(ref, "cbc:DocumentType", "Guia de Remision Remitente")
        _sub(_sub(ref, "cac:IssuerParty"), "cac:PartyIdentification").append(
            _id_con_esquema(guia["remitente"]["numero_documento"], "6"))

    # Referencia a la firma que app/firma_xml.py inserta en ext:ExtensionContent.
    firma = _sub(raiz, "cac:Signature")
    _sub(firma, "cbc:ID", ruc_emisor)
    firmante = _sub(firma, "cac:SignatoryParty")
    _sub(_sub(firmante, "cac:PartyIdentification"), "cbc:ID", ruc_emisor)
    _sub(_sub(firmante, "cac:PartyName"), "cbc:Name", razon_social_emisor)
    _sub(_sub(_sub(firma, "cac:DigitalSignatureAttachment"), "cac:ExternalReference"),
         "cbc:URI", f"#{ID_FIRMA}")

    _parte_identificada(raiz, "cac:DespatchSupplierParty", guia["remitente"])
    _parte_identificada(raiz, "cac:DeliveryCustomerParty", guia["destinatario"])
    if es_remitente and guia.get("proveedor"):
        _parte_identificada(raiz, "cac:SellerSupplierParty", guia["proveedor"])

    envio = _sub(raiz, "cac:Shipment")
    _sub(envio, "cbc:ID", "SUNAT_Envio")
    if es_remitente:
        _sub(envio, "cbc:HandlingCode", guia["motivo_traslado"],
             listAgencyName="PE:SUNAT", listName="Motivo de traslado",
             listURI="urn:pe:gob:sunat:cpe:see:gem:catalogos:catalogo20")
    _sub(envio, "cbc:GrossWeightMeasure", numero(guia["peso_bruto"]), unitCode=guia.get("unidad_peso") or "KGM")

    etapa = _sub(envio, "cac:ShipmentStage")
    modalidad = guia["modalidad_traslado"] if es_remitente else "01"
    _sub(etapa, "cbc:TransportModeCode", modalidad, listAgencyName="PE:SUNAT",
         listName="Modalidad de traslado", listURI="urn:pe:gob:sunat:cpe:see:gem:catalogos:catalogo18")
    _sub(_sub(etapa, "cac:TransitPeriod"), "cbc:StartDate", guia["fecha_traslado"].strftime("%Y-%m-%d"))

    # Transporte público (09/01): el transportista va identificado y NO se informa
    # conductor ni vehículo. En la 31 el transportista es el propio emisor.
    if es_remitente and modalidad == "01":
        t = guia["transportista"]
        carrier = _sub(etapa, "cac:CarrierParty")
        _sub(_sub(carrier, "cac:PartyIdentification"), "cbc:ID", t["numero_documento"], schemeID="6")
        le = _sub(carrier, "cac:PartyLegalEntity")
        _sub(le, "cbc:RegistrationName", t["razon_social"])
        if t.get("registro_mtc"):
            _sub(le, "cbc:CompanyID", t["registro_mtc"])
    elif not es_remitente:
        carrier = _sub(etapa, "cac:CarrierParty")
        _sub(_sub(carrier, "cac:PartyIdentification"), "cbc:ID", ruc_emisor, schemeID="6")
        le = _sub(carrier, "cac:PartyLegalEntity")
        _sub(le, "cbc:RegistrationName", razon_social_emisor)
        _sub(le, "cbc:CompanyID", guia["registro_mtc"])

    if modalidad == "02" or not es_remitente:
        c = guia["conductor"]
        conductor = _sub(etapa, "cac:DriverPerson")
        _sub(conductor, "cbc:ID", c["numero_documento"], schemeID=c.get("tipo_documento", "1"))
        _sub(conductor, "cbc:FirstName", c["nombres"])
        _sub(conductor, "cbc:FamilyName", c["apellidos"])
        _sub(conductor, "cbc:JobTitle", "Principal")
        _sub(_sub(conductor, "cac:IdentityDocumentReference"), "cbc:ID", c["licencia"])

    entrega = _sub(envio, "cac:Delivery")
    _direccion(entrega, "cac:DeliveryAddress", guia["llegada"])
    salida = _sub(entrega, "cac:Despatch")
    if es_remitente and modalidad == "01":
        # Transporte público: SUNAT (3617) pide la fecha de entrega de los bienes al transportista.
        _sub(salida, "cbc:ActualDespatchDate", guia["fecha_traslado"].strftime("%Y-%m-%d"))
    _direccion(salida, "cac:DespatchAddress", guia["partida"])

    if modalidad == "02" or not es_remitente:
        v = guia["vehiculo"]
        equipo = _sub(_sub(envio, "cac:TransportHandlingUnit"), "cac:TransportEquipment")
        _sub(equipo, "cbc:ID", v["placa"])
        if v.get("tuc"):
            _sub(_sub(equipo, "cac:ApplicableTransportMeans"), "cbc:RegistrationNationalityID", v["tuc"])

    for i, item in enumerate(guia["items"], start=1):
        linea = _sub(raiz, "cac:DespatchLine")
        _sub(linea, "cbc:ID", i)
        _sub(linea, "cbc:DeliveredQuantity", numero(item["cantidad"]), unitCode=item.get("medida") or "NIU")
        _sub(_sub(linea, "cac:OrderLineReference"), "cbc:LineID", i)
        it = _sub(linea, "cac:Item")
        _sub(it, "cbc:Description", item["descripcion"])
        if item.get("codigo_producto"):
            _sub(_sub(it, "cac:SellersItemIdentification"), "cbc:ID", item["codigo_producto"])

    return etree.tostring(raiz, xml_declaration=True, encoding="UTF-8", standalone=False)


def _id_con_esquema(valor: str, esquema: str):
    el = etree.Element(_CBC + "ID")
    el.text = valor
    el.set("schemeID", esquema)
    return el
