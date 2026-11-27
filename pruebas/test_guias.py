"""
Pruebas de las guías de remisión (app/guias.py) sin SUNAT real: el certificado es uno
autofirmado generado al vuelo y lo único que se reemplaza es gre_cliente._http (la red).
Lo que SÍ se verifica de verdad: que el XML firmado valide con una librería independiente
(signxml), el zip y hash que se envían, el flujo ticket 98 -> 0, el reenvío del mismo número
y que lo aceptado no se reenvíe. Se ejecuta como script:
    python pruebas/test_guias.py
"""
import base64, datetime as dt, hashlib, io, json, os, sys, tempfile, zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from lxml import etree

import app.configuracion as configuracion
import app.gre_cliente as gre
import app.guias as guias
import app.main as main_mod

RUC = "20111111111"
base = tempfile.mkdtemp()
os.environ["FACTURADOR_API_RECHAZOS_LOG"] = os.path.join(base, "rechazos.jsonl")
os.environ["FACTURADOR_API_REINTENTOS_BD"] = os.path.join(base, "reintentos.db")
guias.ESPERA_TICKET_SEG = 0

# Certificado autofirmado -> .p12 con clave
llave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "GRIFO SAC")])
ahora = dt.datetime.now(dt.timezone.utc)
cert = (x509.CertificateBuilder().subject_name(nombre).issuer_name(nombre).public_key(llave.public_key())
        .serial_number(1).not_valid_before(ahora - dt.timedelta(days=1)).not_valid_after(ahora + dt.timedelta(days=30))
        .sign(llave, hashes.SHA256()))
ruta_p12 = os.path.join(base, "certificado.p12")
with open(ruta_p12, "wb") as fh:
    fh.write(pkcs12.serialize_key_and_certificates(
        b"x", llave, cert, None, serialization.BestAvailableEncryption(b"clave123")))

empresa = configuracion.Empresa(
    ruc=RUC, nombre="GRIFO SAC", sfs_base_url="http://localhost:0", ruta_base=base,
    sol_usuario="USUARIO1", sol_clave="clavesol", sol_client_id="cid", sol_client_secret="csec",
    certificado_ruta=ruta_p12, certificado_clave="clave123")
main_mod.configuracion.obtener_empresa = lambda ruc: empresa if ruc == RUC else None
client = TestClient(main_mod.app)

# --- SUNAT simulado -----------------------------------------------------------
llamadas = []
ticket_respuestas = []  # cola de respuestas del GET ticket


def cdr_zip(codigo="0", descripcion="La Guia numero T001-1, ha sido aceptada"):
    xml = (f'<ar:ApplicationResponse xmlns:ar="urn:oasis:names:specification:ubl:schema:xsd:ApplicationResponse-2" '
           f'xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" '
           f'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"><cac:DocumentResponse>'
           f'<cac:Response><cbc:ResponseCode>{codigo}</cbc:ResponseCode><cbc:Description>{descripcion}</cbc:Description>'
           f'</cac:Response></cac:DocumentResponse></ar:ApplicationResponse>')
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("R-cdr.xml", xml)
    return base64.b64encode(b.getvalue()).decode()


def http_falso(metodo, url, *, cuerpo=None, cabeceras=None):
    llamadas.append((metodo, url, cuerpo, cabeceras))
    if "oauth2/token" in url:
        return 200, {"access_token": "TOKEN-1", "expires_in": 3600}
    if "/envios/" in url:
        return 200, ticket_respuestas.pop(0)
    return 200, {"numTicket": "TK-001"}


gre._http = http_falso


def hubo(f):
    return [l for l in llamadas if f(l)]


def guia_09(**cambios):
    g = {
        "tipo_guia": "REMITENTE", "numeracion": "T001-000001",
        "fecha_emision": dt.datetime.now().replace(microsecond=0).isoformat(),
        "fecha_traslado": dt.date.today().isoformat(),
        "motivo_traslado": "02", "modalidad_traslado": "01", "peso_bruto": 6155,
        "remitente": {"numero_documento": RUC, "razon_social": "GRIFO SAC"},
        "destinatario": {"numero_documento": RUC, "razon_social": "GRIFO SAC"},
        "proveedor": {"numero_documento": "20100011884", "razon_social": "PLANTA SA"},
        "transportista": {"numero_documento": "20222222222", "razon_social": "TRANSPORTES SAC", "registro_mtc": "MTC-123"},
        "partida": {"ubigeo": "150101", "direccion": "AV. PLANTA 100"},
        "llegada": {"ubigeo": "150115", "direccion": "AV. GRIFO 200"},
        "conductor": {"numero_documento": "12345678", "nombres": "JUAN", "apellidos": "PEREZ", "licencia": "Q12345678"},
        "vehiculo": {"placa": "ABC123", "tuc": "TUC-001"},
        "items": [{"descripcion": "DIESEL B5 SCOP 123", "cantidad": 1900, "medida": "GLL"}],
    }
    g.update(cambios)
    return g


def guia_31(**cambios):
    g = guia_09(tipo_guia="31", numeracion="V001-000001", guia_remitente="T001-000001", registro_mtc="MTC-123",
                remitente={"numero_documento": "20100011884", "razon_social": "PLANTA SA"})
    for k in ("motivo_traslado", "modalidad_traslado", "proveedor", "transportista"):
        g.pop(k)
    g.update(cambios)
    return g


def verificar_firma(xml: str):
    """La firma debe validar con una implementación independiente (signxml)."""
    from signxml import XMLVerifier
    XMLVerifier().verify(xml.encode(), x509_cert=cert.public_bytes(serialization.Encoding.PEM).decode(),
                         expect_references=1)


# 1. 09: queda en "enviado" con ticket 98, y el GET lo resuelve a aceptado con su CDR
ticket_respuestas[:] = [{"codRespuesta": "98"}, {"codRespuesta": "98"}]
r = client.post(f"/empresas/{RUC}/guias?incluir=xml", json=guia_09())
assert r.status_code == 201, r.text
cuerpo = r.json()
assert cuerpo["codigo"] == "09-T001-000001" and cuerpo["estado"] == "enviado", cuerpo
verificar_firma(cuerpo["xml"])
raiz = etree.fromstring(cuerpo["xml"].encode())
ns = {"cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
      "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"}
assert raiz.findtext("cbc:ID", namespaces=ns) == "T001-1"
assert raiz.findtext("cbc:DespatchAdviceTypeCode", namespaces=ns) == "09"
assert raiz.find(".//cac:DespatchLine/cbc:DeliveredQuantity", ns).get("unitCode") == "GLL"
assert raiz.find(".//cac:SellerSupplierParty", ns) is not None          # proveedor (compra)
assert raiz.find(".//cac:ShipmentStage/cac:CarrierParty", ns) is not None  # transporte público
assert raiz.find(".//cac:DriverPerson", ns) is None                       # ...sin conductor
assert raiz.find(".//cac:TransportEquipment", ns) is None                 # ...ni vehículo

# lo que se envió a SUNAT: zip con el xml, hash correcto, bearer
envio = hubo(lambda l: l[0] == "POST" and l[1].endswith(f"/{RUC}-09-T001-000001"))[0]
assert envio[3]["Authorization"] == "Bearer TOKEN-1"
archivo = json.loads(envio[2])["archivo"]
assert archivo["nomArchivo"] == f"{RUC}-09-T001-000001.zip"
crudo = base64.b64decode(archivo["arcGreZip"])
assert hashlib.sha256(crudo).hexdigest() == archivo["hashZip"]
assert zipfile.ZipFile(io.BytesIO(crudo)).namelist() == [f"{RUC}-09-T001-000001.xml"]
# el token se pidió con el usuario SOL completo
tok = hubo(lambda l: "oauth2/token" in l[1])[0]
assert b"username=20111111111USUARIO1" in tok[2] and b"grant_type=password" in tok[2]

ticket_respuestas[:] = [{"codRespuesta": "0", "arcCdr": cdr_zip(), "indCdrGenerado": "1"}]
g = client.get(f"/empresas/{RUC}/comprobantes/09-T001-000001").json()
assert g["estado"] == "aceptado" and "ResponseCode" in g["cdr"] and g["xml"], g

# 2. Reenviar lo aceptado no vuelve a SUNAT
n_envios = len(hubo(lambda l: l[0] == "POST" and "/comprobantes/" in l[1]))
r = client.post(f"/empresas/{RUC}/guias", json=guia_09())
assert r.json()["estado"] == "aceptado"
assert len(hubo(lambda l: l[0] == "POST" and "/comprobantes/" in l[1])) == n_envios, "reenvió una guía aceptada"
# el token se reutilizó: una sola petición de token en toda la prueba
assert len(hubo(lambda l: "oauth2/token" in l[1])) == 1

# 3. 31: lleva conductor, vehículo (con TUC) y la 09 relacionada; SUNAT responde que se acepta de una
ticket_respuestas[:] = [{"codRespuesta": "0", "arcCdr": cdr_zip()}]
r = client.post(f"/empresas/{RUC}/guias?incluir=xml,cdr", json=guia_31())
assert r.status_code == 201 and r.json()["estado"] == "aceptado", r.text
x = etree.fromstring(r.json()["xml"].encode())
verificar_firma(r.json()["xml"])
assert x.findtext("cbc:DespatchAdviceTypeCode", namespaces=ns) == "31"
assert x.findtext(".//cac:AdditionalDocumentReference/cbc:ID", namespaces=ns) == "T001-1"
assert x.findtext(".//cac:DriverPerson/cbc:FirstName", namespaces=ns) == "JUAN"
assert x.findtext(".//cac:TransportEquipment/cbc:ID", namespaces=ns) == "ABC123"
assert x.findtext(".//cac:CarrierParty/cac:PartyLegalEntity/cbc:CompanyID", namespaces=ns) == "MTC-123"
assert r.json()["cdr"]

# 4. Rechazo por ticket (99): estado rechazado con el código, y un reenvío del mismo número lo vuelve a mandar
ticket_respuestas[:] = [{"codRespuesta": "99", "error": {"numError": "3207", "desError": "Serie no autorizada"}}]
r = client.post(f"/empresas/{RUC}/guias", json=guia_09(numeracion="T001-000002"))
assert r.json()["estado"] == "rechazado" and r.json()["codigo_error"] == "3207", r.json()
assert "Serie no autorizada" in r.json()["des_obse"]
ticket_respuestas[:] = [{"codRespuesta": "0", "arcCdr": cdr_zip()}]
r = client.post(f"/empresas/{RUC}/guias", json=guia_09(numeracion="T001-000002"))
assert r.json()["estado"] == "aceptado", r.json()

# 5. Aceptada con observaciones (ResponseCode del CDR distinto de 0)
ticket_respuestas[:] = [{"codRespuesta": "0", "arcCdr": cdr_zip("4001", "observación de prueba")}]
r = client.post(f"/empresas/{RUC}/guias", json=guia_09(numeracion="T001-000003"))
assert r.json()["estado"] == "aceptado_con_observaciones", r.json()

# 6. SUNAT rechaza el envío de entrada (HTTP 400 con cuerpo): queda rechazado, no es un 5xx
_orig = gre._http


def http_400(metodo, url, **kw):
    if metodo == "POST" and "/comprobantes/" in url:
        return 400, {"cod": "1000", "msg": "El comprobante ya fue enviado"}
    return _orig(metodo, url, **kw)


gre._http = http_400
r = client.post(f"/empresas/{RUC}/guias", json=guia_09(numeracion="T001-000004"))
assert r.status_code == 201 and r.json()["estado"] == "rechazado" and r.json()["codigo_error"] == "1000", r.json()

# 7. Sin red: 502, y el mismo número se puede reintentar
def http_caida(metodo, url, **kw):
    if metodo == "POST" and "/comprobantes/" in url:
        raise gre.GreError("sin conexión", red=True)
    return _orig(metodo, url, **kw)


gre._http = http_caida
r = client.post(f"/empresas/{RUC}/guias", json=guia_09(numeracion="T001-000005"))
assert r.status_code == 502, r.text
gre._http = _orig
ticket_respuestas[:] = [{"codRespuesta": "0", "arcCdr": cdr_zip()}]
r = client.post(f"/empresas/{RUC}/guias", json=guia_09(numeracion="T001-000005"))
assert r.json()["estado"] == "aceptado", r.json()

# 8. Validaciones -> 422 y nada sale hacia SUNAT
antes = len(llamadas)
casos = {
    "serie V en una 09": guia_09(numeracion="V001-000009"),
    "serie T en una 31": guia_31(numeracion="T001-000009"),
    "remitente distinto del emisor": guia_09(remitente={"numero_documento": "20999999999", "razon_social": "OTRO"}),
    "compra sin proveedor": guia_09(proveedor=None),
    "público sin transportista": guia_09(transportista=None),
    "privado sin conductor": guia_09(modalidad_traslado="02", conductor=None),
    "31 sin registro_mtc": guia_31(registro_mtc=None),
    "31 sin vehiculo": guia_31(vehiculo=None),
    "sin items": guia_09(items=[]),
}
for nombre_caso, cuerpo in casos.items():
    r = client.post(f"/empresas/{RUC}/guias", json=cuerpo)
    assert r.status_code == 422, (nombre_caso, r.status_code, r.text)
assert client.post(f"/empresas/{RUC}/guias", json=guia_09(tipo_guia="OTRA")).status_code == 422
assert client.post(f"/empresas/{RUC}/guias", json=guia_09(partida={"ubigeo": "15", "direccion": "x"})).status_code == 422
assert len(llamadas) == antes

# 9. Modalidad privada: conductor y vehículo sí van, y no hay transportista
ticket_respuestas[:] = [{"codRespuesta": "0", "arcCdr": cdr_zip()}]
r = client.post(f"/empresas/{RUC}/guias?incluir=xml", json=guia_09(numeracion="T001-000006", modalidad_traslado="02"))
x = etree.fromstring(r.json()["xml"].encode())
assert x.find(".//cac:DriverPerson", ns) is not None and x.find(".//cac:CarrierParty", ns) is None

# 10. Plazo de 3 días: no se envía y queda anulada
antes = len(llamadas)
vieja = (dt.datetime.now() - dt.timedelta(days=5)).replace(microsecond=0).isoformat()
r = client.post(f"/empresas/{RUC}/guias", json=guia_09(numeracion="T001-000007", fecha_emision=vieja))
assert r.json()["estado"] == "anulado" and len(llamadas) == antes, r.json()

# 11. Sin credenciales de guías: 409 explícito
sin = configuracion.Empresa(ruc=RUC, nombre="GRIFO SAC", sfs_base_url="x", ruta_base=base,
                            certificado_ruta=ruta_p12, certificado_clave="clave123")
main_mod.configuracion.obtener_empresa = lambda ruc: sin if ruc == RUC else None
r = client.post(f"/empresas/{RUC}/guias", json=guia_09(numeracion="T001-000008"))
assert r.status_code == 409 and "sol_client_id" in r.json()["detail"], r.text
# certificado con clave equivocada: 409 con el motivo
mal = configuracion.Empresa(ruc=RUC, nombre="GRIFO SAC", sfs_base_url="x", ruta_base=base, sol_usuario="u",
                            sol_clave="c", sol_client_id="a", sol_client_secret="b",
                            certificado_ruta=ruta_p12, certificado_clave="incorrecta")
main_mod.configuracion.obtener_empresa = lambda ruc: mal if ruc == RUC else None
r = client.post(f"/empresas/{RUC}/guias", json=guia_09(numeracion="T001-000008"))
assert r.status_code == 409 and "clave" in r.json()["detail"], r.text
main_mod.configuracion.obtener_empresa = lambda ruc: empresa if ruc == RUC else None

# 12. GET de una guía inexistente: 404; y un RUC no dado de alta: 404
assert client.get(f"/empresas/{RUC}/comprobantes/09-T009-000001").status_code == 404
assert client.post("/empresas/20555555555/guias", json=guia_09()).status_code == 404

# 13. .p12 con cifrado antiguo (RC2-40): lo abre la alternativa con openssl; con clave mala falla claro
import shutil, subprocess
if shutil.which("openssl"):
    k, c = os.path.join(base, "k.pem"), os.path.join(base, "c.pem")
    with open(k, "wb") as fh:
        fh.write(llave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    with open(c, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    viejo = os.path.join(base, "viejo.p12")
    r = subprocess.run(["openssl", "pkcs12", "-export", "-inkey", k, "-in", c, "-out", viejo, "-passout", "pass:clave123"]
                       + (["-legacy"] if b"OpenSSL 3" in subprocess.run(["openssl", "version"], capture_output=True).stdout else []),
                       capture_output=True)
    if r.returncode == 0:
        from app import firma_xml
        l2, c2 = firma_xml.cargar_certificado(viejo, "clave123")
        assert c2.serial_number == cert.serial_number
        try:
            firma_xml.cargar_certificado(viejo, "mala")
            raise AssertionError("aceptó una clave incorrecta")
        except firma_xml.CertificadoError as e:
            assert "clave" in str(e)

print("OK: guías (09 y 31) — XML firmado válido, envío por ticket, reenvío, rechazos y validaciones")
