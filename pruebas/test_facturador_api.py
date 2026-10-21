"""
Pruebas de humo de facturador-api, sin necesidad de un SFS real: se apunta
configuracion.obtener_empresa a una carpeta temporal y se reemplaza
sfs_cliente.generar_y_enviar por un doble que no toca la red. Lo que SÍ se
verifica de verdad es lo que no depende de SFS: que los 5 archivos se escriben
con extensión en MAYÚSCULAS (la causa real del primer bug encontrado migrando a
Linux) y que la consulta de estado/CDR lee bien lo que ya dejó SFS en disco.
"""
import os, sys, sqlite3, zipfile, tempfile, io

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import app.configuracion as configuracion
import app.sfs_cliente as sfs_cliente
import app.main as main_mod
from app.main import app as fastapi_app

RUC = "20609785269"

base = tempfile.mkdtemp()
# El registro de rechazos (app/rechazos.py) escribe aparte: que las pruebas no
# ensucien el rechazos.jsonl real del repo.
os.environ["FACTURADOR_API_RECHAZOS_LOG"] = os.path.join(base, "rechazos.jsonl")
data_dir = os.path.join(base, "sunat_archivos", "sfs", "DATA")
rpta_dir = os.path.join(base, "sunat_archivos", "sfs", "RPTA")
procesados_dir = os.path.join(rpta_dir, "procesados")
bd_dir = os.path.join(base, "bd")
for d in (data_dir, rpta_dir, procesados_dir, bd_dir):
    os.makedirs(d, exist_ok=True)

empresa_fake = configuracion.Empresa(
    ruc=RUC, nombre="PRUEBA", sfs_base_url="http://localhost:0", ruta_base=base
)
# Se mockea sobre el módulo que main.py usa de verdad (configuracion.obtener_empresa
# llamado como atributo de módulo, no importado por nombre) — ver el bug real que
# encontramos en la primera versión: un "from .configuracion import obtener_empresa"
# en main.py hacía que este mock no tuviera ningún efecto.
main_mod.configuracion.obtener_empresa = lambda ruc: empresa_fake if ruc == RUC else None

# Esquema mínimo de DOCUMENTO, igual al real (confirmado contra la instancia de SFS
# migrada a Lightsail con ".schema PARAMETRO"/inspección directa de la tabla).
with sqlite3.connect(os.path.join(bd_dir, "BDFacturador.db")) as conn:
    conn.execute("""
        CREATE TABLE DOCUMENTO (
            NUM_RUC TEXT, TIP_DOCU TEXT, NUM_DOCU TEXT, NOM_ARCH TEXT,
            IND_SITU TEXT, DES_OBSE TEXT, FEC_GENE TEXT, FEC_ENVI TEXT, NUM_TICKET TEXT
        )
    """)
    # Catálogo de errores de SUNAT que SFS lleva en cada base (deploy/esquema_bd.sql).
    conn.execute("CREATE TABLE ERROR (COD_CATAERRO VARCHAR(4) PRIMARY KEY NOT NULL, "
                 "NOM_CATAERRO VARCHAR(120) NOT NULL, IND_ESTADO VARCHAR(1) NOT NULL)")
    conn.execute("INSERT INTO ERROR VALUES ('2800', "
                 "'El dato ingresado en el tipo de documento de identidad del receptor no esta permitido.', '1')")

client = TestClient(fastapi_app)

# 1) RUC no dado de alta -> 404, sin tocar nada más.
r = client.post("/empresas/00000000000/comprobantes", json={
    "numeracion_comprobante": "F001-1", "tipo_comprobante": "FACTURA",
    "fecha_emision": "2026-10-02T10:00:00", "total": 1.0, "items": [],
})
print("1. RUC no dado de alta ->", r.status_code)
assert r.status_code == 404

# 2) Nota sin 'referencia' -> 422, no debe escribir archivos.
r = client.post(f"/empresas/{RUC}/comprobantes", json={
    "numeracion_comprobante": "FC01-1", "tipo_comprobante": "NOTA_CREDITO",
    "fecha_emision": "2026-10-02T10:00:00", "total": 1.0, "items": [],
})
print("2. Nota sin referencia ->", r.status_code)
assert r.status_code == 422
assert not os.listdir(data_dir), "no debió escribir nada en DATA"

# 2b) Factura sin RUC de receptor -> 422 (SUNAT daría 2800), nada escrito.
r = client.post(f"/empresas/{RUC}/comprobantes", json={
    "numeracion_comprobante": "F002-000009", "tipo_comprobante": "FACTURA",
    "fecha_emision": "2026-10-02T10:00:00", "total": 1.0, "items": [],
})
print("2b. Factura sin RUC ->", r.status_code)
assert r.status_code == 422
assert not os.listdir(data_dir), "no debió escribir nada en DATA"

# 3) Factura válida: se reemplaza sfs_cliente.generar_y_enviar por un doble — acá
# NO se prueba la llamada HTTP a SFS (eso ya se validó a mano contra el SFS real
# en la migración), se prueba que facturador-api arma bien todo lo anterior.
sfs_cliente.generar_y_enviar = lambda base_url, ruc, tipo, numero: {
    "ind_situ": "11", "des_obse": "-", "fec_gene": "02/10/2026 10:00:00", "fec_envi": "02/10/2026 10:00:01",
}
r = client.post(f"/empresas/{RUC}/comprobantes", json={
    "numeracion_comprobante": "F002-000002",
    "tipo_comprobante": "FACTURA",
    "fecha_emision": "2026-10-02T10:00:00",
    "total": 1.00,
    "monto_letras": "UN CON 00/100 SOLES",
    "receptor": {"tipo_documento": "6", "numero_documento": "20123456789", "razon_social": "CLIENTE DE PRUEBA"},
    "items": [{"descripcion": "ITEM", "cantidad": 1,
               "valor": 0.847458, "valor_venta": 0.85, "igv_venta": 0.15, "precio": 1.00}],
})
print("3. Factura válida ->", r.status_code, r.json())
assert r.status_code == 201
# hash/qr quedan None acá: "11" ya cuenta como firmado, pero no existe ningún
# XML real en FIRMA/ en esta prueba (se cubre aparte en 3c, con un XML de verdad).
assert r.json() == {"codigo": "01-F002-000002", "estado": "aceptado", "des_obse": "-", "hash": None, "qr": None,
                    "codigo_error": None, "descripcion_error": None, "xml": None, "cdr": None}

archivos_escritos = sorted(os.listdir(data_dir))
print("   archivos en DATA:", archivos_escritos)
esperado = sorted(f"{RUC}-01-F002-000002.{e}" for e in ("CAB", "DET", "TRI", "LEY", "PAG"))
assert archivos_escritos == esperado, "extensiones deben quedar en MAYÚSCULAS (el bug de Linux)"

# 3b) Boleta: NUNCA se envía sola a SUNAT (se agrupa en el resumen diario — ver
# dominio/resumen_diario.py y el comentario de ciclo_generacion.py del daemon
# original). Si esto llama a generar_y_enviar en vez de generar(), es el mismo
# error real que se mandó por accidente la primera vez con una empresa real.
sfs_cliente.generar_y_enviar = lambda base_url, ruc, tipo, numero: (_ for _ in ()).throw(
    AssertionError("una BOLETA no debe pasar por generar_y_enviar (se enviaría sola a SUNAT)")
)
sfs_cliente.generar = lambda base_url, ruc, tipo, numero: {"ind_situ": "02", "des_obse": "-"}
r = client.post(f"/empresas/{RUC}/comprobantes", json={
    "numeracion_comprobante": "B001-000001",
    "tipo_comprobante": "BOLETA",
    "fecha_emision": "2026-10-02T10:00:00",
    "total": 1.00,
    "items": [{"descripcion": "ITEM", "cantidad": 1,
               "valor": 0.847458, "valor_venta": 0.85, "igv_venta": 0.15, "precio": 1.00}],
})
print("3b. Boleta -> solo generar(), nunca enviar ->", r.status_code, r.json())
assert r.status_code == 201
assert r.json() == {"codigo": "03-B001-000001", "estado": "generado", "des_obse": "-", "hash": None, "qr": None,
                    "codigo_error": None, "descripcion_error": None, "xml": None, "cdr": None}

# 3c) hash/qr SÍ se arman cuando ya existe un XML real en FIRMA/ (como lo deja
# SFS de verdad al firmar) — acá no se mockea nada de lectura de archivos, se
# prueba la extracción real del DigestValue y el armado del QR de 10 campos.
firma_dir = os.path.join(base, "sunat_archivos", "sfs", "FIRMA")
os.makedirs(firma_dir, exist_ok=True)
with open(os.path.join(firma_dir, f"{RUC}-01-F002-000099.xml"), "w", encoding="utf-8") as fh:
    fh.write(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Invoice xmlns:ds="http://www.w3.org/2000/09/xmldsig#">'
        '<ds:Signature><ds:SignedInfo><ds:Reference>'
        '<ds:DigestValue>HASHDEPRUEBA==</ds:DigestValue>'
        '</ds:Reference></ds:SignedInfo></ds:Signature>'
        '</Invoice>'
    )
sfs_cliente.generar_y_enviar = lambda base_url, ruc, tipo, numero: {"ind_situ": "11", "des_obse": "-"}
r = client.post(f"/empresas/{RUC}/comprobantes", json={
    "numeracion_comprobante": "F002-000099",
    "tipo_comprobante": "FACTURA",
    "fecha_emision": "2026-10-05T10:00:00",
    "total": 118.00,
    "gravadas": 100.00,
    "igv": 18.00,
    "receptor": {"tipo_documento": "6", "numero_documento": "20123456789", "razon_social": "CLIENTE S.A.C."},
    "items": [{"descripcion": "ITEM", "cantidad": 1,
               "valor": 100.0, "valor_venta": 100.0, "igv_venta": 18.0, "precio": 118.0}],
})
print("3c. Con XML firmado real -> hash/qr ->", r.status_code, r.json())
assert r.status_code == 201
cuerpo_3c = r.json()
assert cuerpo_3c["estado"] == "aceptado"
assert cuerpo_3c["hash"] == "HASHDEPRUEBA=="
assert cuerpo_3c["qr"] == f"{RUC}|01|F002|000099|18.00|118.00|2026-10-05|6|20123456789|HASHDEPRUEBA=="

# 4) Consultar antes de que exista CDR: ind_situ sin CDR todavía.
with sqlite3.connect(os.path.join(bd_dir, "BDFacturador.db")) as conn:
    conn.execute(
        "INSERT INTO DOCUMENTO VALUES (?,?,?,?,?,?,?,?,?)",
        (RUC, "01", "F002-000002", f"{RUC}-01-F002-000002", "02", "-",
         "02/10/2026 10:00:00", None, None),
    )
r = client.get(f"/empresas/{RUC}/comprobantes/01-F002-000002")
print("4. Estado sin CDR ->", r.status_code, r.json())
assert r.status_code == 200
assert r.json()["cdr"] is None

# 5) Con CDR ya descargado (ind_situ=11): arma el .zip como lo dejaría SFS y
# confirma que se extrae el XML de adentro.
with sqlite3.connect(os.path.join(bd_dir, "BDFacturador.db")) as conn:
    conn.execute(
        "UPDATE DOCUMENTO SET IND_SITU='11', FEC_ENVI=? WHERE NUM_DOCU='F002-000002'",
        ("02/10/2026 10:00:01",),
    )
zip_path = os.path.join(procesados_dir, f"R{RUC}-01-F002-000002.zip")
with zipfile.ZipFile(zip_path, "w") as z:
    z.writestr("R20609785269-01-F002-000002.xml", "<ApplicationResponse>ACEPTADO</ApplicationResponse>")

r = client.get(f"/empresas/{RUC}/comprobantes/01-F002-000002")
print("5. Estado con CDR ->", r.status_code, r.json())
assert r.status_code == 200
assert "ACEPTADO" in r.json()["cdr"]

# 6) Resumen diario: una boleta de AYER, generada pero nunca enviada sola (como
# haría de verdad sfs_cliente.generar), debe entrar al resumen. Se mockea
# sfs_cliente.generar (para escribir el .CAB real sin tocar red) y luego
# generar_y_enviar (para el envío del propio RC).
#
# "Ayer" se calcula con la MISMA hora de Lima fija que usa app.resumenes, no con
# la hora local de la máquina que corre la prueba — si no, un CI corriendo ya
# "mañana" en su propia zona horaria podría ver esta fecha como "hoy" en Lima y
# la boleta quedaría excluida por error.
import datetime as _dt
from app.resumenes import _LIMA
ayer = (_dt.datetime.now(_LIMA) - _dt.timedelta(days=1)).strftime("%Y-%m-%dT10:00:00")

sfs_cliente.generar = lambda base_url, ruc, tipo, numero: {"ind_situ": "02", "des_obse": "-"}
r = client.post(f"/empresas/{RUC}/comprobantes", json={
    "numeracion_comprobante": "B001-000050",
    "tipo_comprobante": "BOLETA",
    "fecha_emision": ayer,
    "total": 1.00,
    "items": [{"descripcion": "ITEM", "cantidad": 1,
               "valor": 0.847458, "valor_venta": 0.85, "igv_venta": 0.15, "precio": 1.00}],
})
assert r.status_code == 201, r.json()
# Simula lo que de verdad haría SFS al generarla: registrarla en DOCUMENTO.
with sqlite3.connect(os.path.join(bd_dir, "BDFacturador.db")) as conn:
    conn.execute(
        "INSERT INTO DOCUMENTO VALUES (?,?,?,?,?,?,?,?,?)",
        (RUC, "03", "B001-000050", f"{RUC}-03-B001-000050", "02", "-",
         "02/10/2026 10:00:00", None, None),
    )

llamado_con = {}
def _generar_y_enviar_rc(base_url, ruc, tipo, numero):
    llamado_con["tipo"] = tipo
    llamado_con["numero"] = numero
    return {"ind_situ": "11", "des_obse": "-"}
sfs_cliente.generar_y_enviar = _generar_y_enviar_rc

r = client.post(f"/empresas/{RUC}/resumenes-diarios")
print("6. Resumen diario ->", r.status_code, r.json())
assert r.status_code == 200
cuerpo = r.json()
assert cuerpo["cantidad_boletas"] == 1
assert cuerpo["boletas"] == ["B001-000050"]
assert cuerpo["estado"] == "aceptado"
assert llamado_con["tipo"] == "RC"
assert cuerpo["numeracion_rc"] == llamado_con["numero"]
assert cuerpo["numeracion_rc"].startswith("RC-")

# El .RDI/.TRD del resumen deben existir, y los archivos de la boleta ya
# consumida deben haber desaparecido (se "gastó" dentro del resumen).
archivos_tras_resumen = sorted(os.listdir(data_dir))
print("   archivos en DATA tras el resumen:", archivos_tras_resumen)
assert any(n.endswith(".RDI") for n in archivos_tras_resumen)
assert any(n.endswith(".TRD") for n in archivos_tras_resumen)
assert not any("B001-000050" in n for n in archivos_tras_resumen), "la boleta consumida debió borrarse"

with sqlite3.connect(os.path.join(bd_dir, "BDFacturador.db")) as conn:
    fila = conn.execute(
        "SELECT 1 FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU='03' AND NUM_DOCU='B001-000050'",
        (RUC,),
    ).fetchone()
assert fila is None, "la fila de la boleta consumida debió borrarse de DOCUMENTO"

# 7) Sin boletas pendientes (ya se consumió la única), no debe armar nada.
r = client.post(f"/empresas/{RUC}/resumenes-diarios")
print("7. Resumen sin pendientes ->", r.status_code, r.json())
assert r.status_code == 200
assert "mensaje" in r.json()

# 8) Resumen (RC) con ticket abierto y SUNAT ya con veredicto: el GET debe
# resolverlo él mismo (ticket_resumen.resolver_ticket_pendiente), dejar el CDR en
# RPTA y cerrar la fila en la bandeja de SFS — ver app/ticket_resumen.py para el
# porqué esto no lo hace SFS solo (exigiría prender su temporizador).
import app.ticket_resumen as ticket_resumen_mod

with sqlite3.connect(os.path.join(bd_dir, "BDFacturador.db")) as conn:
    conn.execute(
        "INSERT INTO DOCUMENTO VALUES (?,?,?,?,?,?,?,?,?)",
        (RUC, "RC", "RC-20261001-001", f"{RUC}-RC-RC-20261001-001", "08", "-",
         "01/10/2026 10:00:00", "01/10/2026 10:00:01", "TICKET-ABC"),
    )

_cdr_buf = io.BytesIO()
with zipfile.ZipFile(_cdr_buf, "w") as z:
    z.writestr("R20609785269-RC-RC-20261001-001.xml", "<ApplicationResponse>ACEPTADO POR TICKET</ApplicationResponse>")
ticket_resumen_mod._consultar_ticket_sunat = lambda empresa, ruc, ticket: ("0", "Aceptado", _cdr_buf.getvalue())

r = client.get(f"/empresas/{RUC}/comprobantes/RC-RC-20261001-001")
print("8. Resumen con ticket resuelto por SUNAT ->", r.status_code, r.json())
assert r.status_code == 200
assert "ACEPTADO POR TICKET" in r.json()["cdr"]

with sqlite3.connect(os.path.join(bd_dir, "BDFacturador.db")) as conn:
    (ind_situ,) = conn.execute(
        "SELECT IND_SITU FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU='RC' AND NUM_DOCU='RC-20261001-001'",
        (RUC,),
    ).fetchone()
assert ind_situ == "03", "el resumen debió cerrarse en la bandeja de SFS tras resolver el ticket"

# 8b) Ticket "todavía procesando" (98): no debe cerrar la fila ni reventar — el
# llamador puede volver a preguntar más tarde con otro GET.
with sqlite3.connect(os.path.join(bd_dir, "BDFacturador.db")) as conn:
    conn.execute(
        "INSERT INTO DOCUMENTO VALUES (?,?,?,?,?,?,?,?,?)",
        (RUC, "RC", "RC-20261001-002", f"{RUC}-RC-RC-20261001-002", "08", "-",
         "01/10/2026 10:00:00", "01/10/2026 10:00:01", "TICKET-XYZ"),
    )
ticket_resumen_mod._consultar_ticket_sunat = lambda empresa, ruc, ticket: ("98", "En proceso", None)

r = client.get(f"/empresas/{RUC}/comprobantes/RC-RC-20261001-002")
print("8b. Resumen con ticket todavía en proceso ->", r.status_code, r.json())
assert r.status_code == 200
assert r.json()["cdr"] is None

with sqlite3.connect(os.path.join(bd_dir, "BDFacturador.db")) as conn:
    (ind_situ,) = conn.execute(
        "SELECT IND_SITU FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU='RC' AND NUM_DOCU='RC-20261001-002'",
        (RUC,),
    ).fetchone()
assert ind_situ == "08", "no debe cerrarse mientras SUNAT sigue procesando el ticket"

print("\nTODO OK")

# 9) Corte de SFS tras el envío: se consulta a SUNAT antes de dejar que reintenten.
import app.consulta_integrada as ci
_consultar_real = ci.consultar_comprobante
def _sfs_caido(*a, **k):
    raise sfs_cliente.SfsError("read timed out")
sfs_cliente.generar_y_enviar = _sfs_caido
cuerpo9 = {
    "numeracion_comprobante": "F002-000010", "tipo_comprobante": "FACTURA",
    "fecha_emision": "2026-10-02T10:00:00", "total": 1.00, "monto_letras": "UN CON 00/100 SOLES",
    "receptor": {"tipo_documento": "6", "numero_documento": "20123456789", "razon_social": "C"},
    "items": [{"descripcion": "ITEM", "valor": 0.847458, "valor_venta": 0.85, "igv_venta": 0.15, "precio": 1.0}],
}
llamadas = []
def _falso(estado):
    def f(empresa, ruc, tipo, serie, numero, fecha, monto):
        llamadas.append((tipo, serie, numero, fecha.strftime("%d/%m/%Y"), monto))
        return estado, "x"
    return f
ci.consultar_comprobante = _falso(ci.ACEPTADO)
r = client.post(f"/empresas/{RUC}/comprobantes", json=cuerpo9)
print("9a. Corte pero SUNAT ya lo tiene ->", r.status_code, r.json()["estado"])
assert r.status_code == 201 and r.json()["estado"] == "enviado"
assert llamadas == [("01", "F002", "000010", "02/10/2026", 1.0)], llamadas
ci.consultar_comprobante = _falso(ci.NO_EXISTE)
r = client.post(f"/empresas/{RUC}/comprobantes", json=cuerpo9)
print("9b. Corte y SUNAT no lo tiene ->", r.status_code)
assert r.status_code == 502
_ruta_orig = configuracion._RUTA_EMPRESAS
configuracion._RUTA_EMPRESAS = os.path.join(base, "no_existe.yaml")
assert configuracion.credenciales_consulta(empresa_fake) is None
assert _consultar_real(empresa_fake, RUC, "01", "F002", "1", __import__("datetime").datetime(2026,10,2), 1.0)[0] == ci.DESCONOCIDO
print("9c. Sin credenciales -> DESCONOCIDO")

# 9d) Credenciales globales (bloque consulta_integrada) y prioridad de las propias.
ruta_yaml = os.path.join(base, "empresas.yaml")
open(ruta_yaml, "w", encoding="utf-8").write(
    "consulta_integrada: {ruc: '20111111111', client_id: gid, client_secret: gsec}\nempresas: {}\n")
configuracion._RUTA_EMPRESAS = ruta_yaml
assert configuracion.credenciales_consulta(empresa_fake) == configuracion.CredencialesConsulta("20111111111", "gid", "gsec")
propia = __import__("dataclasses").replace(empresa_fake, sol_client_id="pid", sol_client_secret="psec")
assert configuracion.credenciales_consulta(propia) == configuracion.CredencialesConsulta(RUC, "pid", "psec")
configuracion._RUTA_EMPRESAS = _ruta_orig
print("9d. Credenciales globales y propias OK")

# 10) Rechazos: se anotan, se pueden listar y traen el código de SUNAT separado.
import app.rechazos as rechazos
for texto, esperado_cod in [
    ("2800 - El dato ingresado en el tipo de documento no es valido", "2800"),
    ("soap-env:Client.0111 - No tiene el perfil", "0111"),
    ("Error 3111 en el XML", "3111"),
    ("Error al firma archivo XML", None),
    ("Documento del 2026-10-02 10:00:00 no enviado", None),   # una fecha no es un código
    ("", None), (None, None),
]:
    assert rechazos.extraer_codigo(texto) == esperado_cod, (texto, rechazos.extraer_codigo(texto))
print("10a. extraer_codigo OK")

sfs_cliente.generar_y_enviar = lambda base_url, ruc, tipo, numero: {
    "ind_situ": "10", "des_obse": "2800 - El dato ingresado en el tipo de documento no es valido",
}
cuerpo10 = dict(cuerpo9, numeracion_comprobante="F002-000011")
r = client.post(f"/empresas/{RUC}/comprobantes", json=cuerpo10)
print("10b. POST rechazado ->", r.status_code, r.json())
assert r.status_code == 201
assert r.json()["estado"] == "rechazado" and r.json()["codigo_error"] == "2800"
assert "receptor no esta permitido" in r.json()["descripcion_error"], "descripción del catálogo ERROR de SFS"

# Código que no está en el catálogo, o sin código: descripción None, sin romper nada.
import app.consultas as consultas_mod
bd_prueba = os.path.join(bd_dir, "BDFacturador.db")
assert consultas_mod.descripcion_error(bd_prueba, "9999") is None
assert consultas_mod.descripcion_error(bd_prueba, None) is None
assert consultas_mod.descripcion_error(os.path.join(base, "no_existe.db"), "2800") is None
print("10b2. descripcion_error OK")

# El GET del mismo comprobante rechazado no debe duplicar la anotación.
with sqlite3.connect(os.path.join(bd_dir, "BDFacturador.db")) as conn:
    conn.execute(
        "INSERT INTO DOCUMENTO VALUES (?,?,?,?,?,?,?,?,?)",
        (RUC, "01", "F002-000011", f"{RUC}-01-F002-000011", "10",
         "2800 - El dato ingresado en el tipo de documento no es valido",
         "02/10/2026 10:00:00", "02/10/2026 10:00:01", None),
    )
r = client.get(f"/empresas/{RUC}/comprobantes/01-F002-000011")
assert r.status_code == 200 and r.json()["estado"] == "rechazado" and r.json()["codigo_error"] == "2800"
assert "receptor no esta permitido" in r.json()["descripcion_error"]
assert r.json()["cdr"] is None

# Payload que FastAPI rechaza antes del handler (falta 'total'): también queda anotado.
r = client.post(f"/empresas/{RUC}/comprobantes", json={"numeracion_comprobante": "F002-000012", "tipo_comprobante": "FACTURA"})
assert r.status_code == 422
# ...pero con un RUC desconocido NO se anota nada (cualquiera podría llenar el archivo).
client.post("/empresas/99999999999/comprobantes", json={"numeracion_comprobante": "X"})

r = client.get(f"/empresas/{RUC}/rechazos")
assert r.status_code == 200, r.text
lista = r.json()["rechazos"]
print("10c. GET /rechazos ->", [(e["origen"], e["codigo"], e["codigo_error"]) for e in lista])
por_origen = {}
for e in lista:
    por_origen.setdefault(e["origen"], []).append(e)
assert [e["codigo"] for e in por_origen["sunat"]] == ["01-F002-000011"], "el GET no debe duplicar el rechazo del POST"
assert "consulta" not in por_origen
assert por_origen["sunat"][0]["codigo_error"] == "2800"
assert "receptor no esta permitido" in por_origen["sunat"][0]["descripcion_error"], "el registro guarda la descripción"
assert any(e["codigo"] == "F002-000012" and "total" in e["des_obse"] for e in por_origen["validacion"])
assert any(e["codigo"] == "01-F002-000010" for e in por_origen["red"])          # los cortes del caso 9
assert not any(e["ruc"] != RUC for e in lista)
assert lista[0]["fecha"] >= lista[-1]["fecha"], "lo más reciente primero"

assert [e["origen"] for e in client.get(f"/empresas/{RUC}/rechazos?origen=sunat").json()["rechazos"]] == ["sunat"]
assert client.get(f"/empresas/{RUC}/rechazos?desde=2999-01-01").json()["total"] == 0
assert client.get(f"/empresas/{RUC}/rechazos?limite=1").json()["total"] == 1
assert client.get("/empresas/00000000000/rechazos").status_code == 404

# Un rechazo hallado SOLO al consultar (no pasó por un POST) se anota una única vez.
with sqlite3.connect(os.path.join(bd_dir, "BDFacturador.db")) as conn:
    conn.execute(
        "INSERT INTO DOCUMENTO VALUES (?,?,?,?,?,?,?,?,?)",
        (RUC, "01", "F002-000013", f"{RUC}-01-F002-000013", "06", "Error al firma archivo XML",
         "02/10/2026 10:00:00", None, None),
    )
for _ in range(3):
    r = client.get(f"/empresas/{RUC}/comprobantes/01-F002-000013")
assert r.json()["estado"] == "error" and r.json()["codigo_error"] is None
consultas_anotadas = client.get(f"/empresas/{RUC}/rechazos?origen=consulta").json()["rechazos"]
assert [e["codigo"] for e in consultas_anotadas] == ["01-F002-000013"]
print("10d. Rechazos: filtros, sin duplicados y sin basura OK")

# 11) XML firmado y CDR en la respuesta, solo si se piden con ?incluir= y solo si ya existen.
# F002-000099 ya tiene su XML firmado en FIRMA/ (caso 3c): se le suma un CDR en RPTA/.
with zipfile.ZipFile(os.path.join(rpta_dir, f"R{RUC}-01-F002-000099.zip"), "w") as z:
    z.writestr(f"R{RUC}-01-F002-000099.xml", "<ApplicationResponse>CDR 99</ApplicationResponse>")
sfs_cliente.generar_y_enviar = lambda base_url, ruc, tipo, numero: {"ind_situ": "11", "des_obse": "-"}
cuerpo99 = {
    "numeracion_comprobante": "F002-000099", "tipo_comprobante": "FACTURA",
    "fecha_emision": "2026-10-05T10:00:00", "total": 118.00, "gravadas": 100.00, "igv": 18.00,
    "receptor": {"tipo_documento": "6", "numero_documento": "20123456789", "razon_social": "CLIENTE S.A.C."},
    "items": [{"descripcion": "ITEM", "cantidad": 1, "valor": 100.0, "valor_venta": 100.0, "igv_venta": 18.0, "precio": 118.0}],
}
r = client.post(f"/empresas/{RUC}/comprobantes", json=cuerpo99)
assert r.json()["xml"] is None and r.json()["cdr"] is None, "sin ?incluir no se adjunta nada"

r = client.post(f"/empresas/{RUC}/comprobantes?incluir=xml,cdr", json=cuerpo99)
print("11a. POST ?incluir=xml,cdr ->", r.status_code, {k: (v[:40] + "…" if isinstance(v, str) and len(v) > 40 else v) for k, v in r.json().items()})
assert r.status_code == 201
assert "HASHDEPRUEBA==" in r.json()["xml"] and r.json()["xml"].startswith("<?xml")
assert "CDR 99" in r.json()["cdr"]
assert r.json()["hash"] == "HASHDEPRUEBA=="

r = client.post(f"/empresas/{RUC}/comprobantes?incluir=xml", json=cuerpo99)
assert r.json()["xml"] and r.json()["cdr"] is None, "solo lo pedido"

# Antes de firmar no hay XML aunque se pida (F002-000010 nunca tuvo uno en FIRMA/).
sfs_cliente.generar_y_enviar = lambda base_url, ruc, tipo, numero: {"ind_situ": "01", "des_obse": "-"}
r = client.post(f"/empresas/{RUC}/comprobantes?incluir=xml,cdr", json=cuerpo9)
assert r.status_code == 201 and r.json()["xml"] is None and r.json()["cdr"] is None

# Un valor desconocido es un 400, no un null silencioso.
r = client.post(f"/empresas/{RUC}/comprobantes?incluir=xlm", json=cuerpo99)
assert r.status_code == 400, r.text

# GET: SIEMPRE adjunta xml y cdr (sin parámetro), cada uno cuando ya existe.
with sqlite3.connect(os.path.join(bd_dir, "BDFacturador.db")) as conn:
    conn.execute(
        "INSERT INTO DOCUMENTO VALUES (?,?,?,?,?,?,?,?,?)",
        (RUC, "01", "F002-000099", f"{RUC}-01-F002-000099", "11", "-",
         "05/10/2026 10:00:00", "05/10/2026 10:00:01", None),
    )
r = client.get(f"/empresas/{RUC}/comprobantes/01-F002-000099")
print("11b. GET -> xml:", bool(r.json()["xml"]), "cdr:", bool(r.json()["cdr"]))
assert "HASHDEPRUEBA==" in r.json()["xml"]
assert "CDR 99" in r.json()["cdr"]
assert "cdr_xml" not in r.json() and "xml_firmado" not in r.json(), "los nombres viejos ya no existen"

# Una boleta recién firmada (ind_situ 02): xml sí, cdr nunca (no tiene CDR propio).
with open(os.path.join(firma_dir, f"{RUC}-03-B001-000777.xml"), "w", encoding="utf-8") as fh:
    fh.write('<?xml version="1.0"?><Invoice xmlns:ds="http://www.w3.org/2000/09/xmldsig#"><ds:DigestValue>HB</ds:DigestValue></Invoice>')
with sqlite3.connect(os.path.join(bd_dir, "BDFacturador.db")) as conn:
    conn.execute(
        "INSERT INTO DOCUMENTO VALUES (?,?,?,?,?,?,?,?,?)",
        (RUC, "03", "B001-000777", f"{RUC}-03-B001-000777", "02", "-", "05/10/2026 10:00:00", None, None),
    )
r = client.get(f"/empresas/{RUC}/comprobantes/03-B001-000777")
assert r.json()["estado"] == "generado"
assert "<Invoice" in r.json()["xml"]
assert r.json()["cdr"] is None
print("11c. Boleta: xml sí, cdr None OK")

# Sin firmar todavía (ind_situ 01): ninguno de los dos.
with sqlite3.connect(os.path.join(bd_dir, "BDFacturador.db")) as conn:
    conn.execute(
        "INSERT INTO DOCUMENTO VALUES (?,?,?,?,?,?,?,?,?)",
        (RUC, "01", "F002-000778", f"{RUC}-01-F002-000778", "01", "-", "05/10/2026 10:00:00", None, None),
    )
r = client.get(f"/empresas/{RUC}/comprobantes/01-F002-000778")
assert r.json()["xml"] is None
assert r.json()["cdr"] is None
print("11d. Sin firmar: xml y cdr None OK")

# 12) Reintento de un comprobante que SFS ya tiene en error ('06') o rechazado ('10').
# Sin esto SFS devuelve la fila vieja y no reenvía nada (ver app/reenvio.py).
def _doc(tipo, numero, ind_situ, obse="-"):
    with sqlite3.connect(os.path.join(bd_dir, "BDFacturador.db")) as c:
        c.execute("DELETE FROM DOCUMENTO WHERE NUM_DOCU=?", (numero,))
        c.execute("INSERT INTO DOCUMENTO VALUES (?,?,?,?,?,?,?,?,?)",
                  (RUC, tipo, numero, f"{RUC}-{tipo}-{numero}", ind_situ, obse, "02/10/2026 10:00:00", None, None))

def _hay_doc(numero):
    with sqlite3.connect(os.path.join(bd_dir, "BDFacturador.db")) as c:
        return c.execute("SELECT IND_SITU FROM DOCUMENTO WHERE NUM_DOCU=?", (numero,)).fetchone()

enviados = []
def _sfs_envia(base_url, ruc, tipo, numero):
    # Lo que importa: al llegar acá la fila vieja YA no debe estar (si no, SFS la devolvería tal cual)
    enviados.append((numero, _hay_doc(numero)))
    return {"ind_situ": "11", "des_obse": "-"}
sfs_cliente.generar_y_enviar = _sfs_envia
def _cuerpo(numero):
    return dict(cuerpo9, numeracion_comprobante=numero)
_ci_original = ci.consultar_comprobante
def _ci_no_debe_llamarse(*a, **k):
    raise AssertionError("no debía consultarse a SUNAT")

# 12a) Rechazado ('10'): se limpia y se reenvía, sin consultar a SUNAT (no lo registró).
_doc("01", "F002-000020", "10", "2800 - dato invalido")
ci.consultar_comprobante = _ci_no_debe_llamarse
r = client.post(f"/empresas/{RUC}/comprobantes", json=_cuerpo("F002-000020"))
print("12a. Rechazado reintentado ->", r.status_code, r.json()["estado"])
assert r.status_code == 201
assert r.json()["estado"] == "aceptado"
assert enviados[-1] == ("F002-000020", None), "la fila vieja debió borrarse antes de reenviar"

# 12b) Error ('06') y SUNAT YA lo tiene: no se reenvía.
_doc("01", "F002-000021", "06", "No se pudo enviar el comprobante")
ci.consultar_comprobante = _falso(ci.ACEPTADO)
n_antes = len(enviados)
r = client.post(f"/empresas/{RUC}/comprobantes", json=_cuerpo("F002-000021"))
print("12b. Error pero SUNAT ya lo tiene ->", r.status_code, r.json()["estado"], r.json()["des_obse"][:50])
assert r.status_code == 201
assert r.json()["estado"] == "enviado"
assert len(enviados) == n_antes, "no debe reenviarse a SUNAT"
assert _hay_doc("F002-000021") == ("06",), "y su fila no se toca"

# 12c) Error ('06') y SUNAT NO lo tiene: se limpia y se reenvía.
_doc("01", "F002-000022", "06", "No se pudo enviar el comprobante")
ci.consultar_comprobante = _falso(ci.NO_EXISTE)
r = client.post(f"/empresas/{RUC}/comprobantes", json=_cuerpo("F002-000022"))
assert r.status_code == 201
assert r.json()["estado"] == "aceptado"
assert enviados[-1] == ("F002-000022", None)

# 12d) Error ('06') y SUNAT no pudo confirmar: ante la duda NO se reenvía (502).
_doc("01", "F002-000023", "06", "No se pudo enviar el comprobante")
ci.consultar_comprobante = _falso(ci.DESCONOCIDO)
n_antes = len(enviados)
r = client.post(f"/empresas/{RUC}/comprobantes", json=_cuerpo("F002-000023"))
print("12d. Error y SUNAT sin confirmar ->", r.status_code)
assert r.status_code == 502
assert len(enviados) == n_antes
assert _hay_doc("F002-000023") == ("06",)

# 12e) Un comprobante aceptado NUNCA se reinicia.
_doc("01", "F002-000024", "11")
ci.consultar_comprobante = _ci_no_debe_llamarse
client.post(f"/empresas/{RUC}/comprobantes", json=_cuerpo("F002-000024"))
assert enviados[-1] == ("F002-000024", ("11",)), "la fila de un aceptado debe seguir ahí"

# 12f) Boleta en error ('06', falló la firma): nunca salió a SUNAT, no hay nada que
# preguntar; se limpia y se vuelve a generar.
_doc("03", "B001-000030", "06", "Error al firma archivo XML")
ci.consultar_comprobante = _ci_no_debe_llamarse
vistos_generar = []
def _generar_boleta(base_url, ruc, tipo, numero):
    vistos_generar.append((numero, _hay_doc(numero)))
    return {"ind_situ": "02", "des_obse": "-"}
sfs_cliente.generar = _generar_boleta
r = client.post(f"/empresas/{RUC}/comprobantes", json=dict(
    cuerpo9, numeracion_comprobante="B001-000030", tipo_comprobante="BOLETA",
    receptor={"tipo_documento": "0", "numero_documento": "00000000", "razon_social": "CLIENTE VARIOS"}))
assert r.status_code == 201
assert r.json()["estado"] == "generado"
assert vistos_generar == [("B001-000030", None)]

# 12g) El XML firmado viejo se borra al reiniciar (para no confundirlo con el nuevo).
_doc("01", "F002-000025", "10", "2800 - dato invalido")
xml_viejo = os.path.join(firma_dir, f"{RUC}-01-F002-000025.xml")
open(xml_viejo, "w").write("<Invoice/>")
ci.consultar_comprobante = _ci_no_debe_llamarse
client.post(f"/empresas/{RUC}/comprobantes", json=_cuerpo("F002-000025"))
assert not os.path.exists(xml_viejo)
ci.consultar_comprobante = _ci_original
# 12h) Anulado por plazo vencido ('05'): un POST posterior no lo reenvía ni lo regenera
# (si no, vuelve a error y el reintento lo anula otra vez, en bucle).
_doc("01", "F002-000026", "05", "Anulado: venció el plazo de 7 días para enviarlo a SUNAT (emitido el 01/10/2026, 20 días) y SUNAT no lo tiene")
ci.consultar_comprobante = _ci_no_debe_llamarse
n_antes = len(enviados)
r = client.post(f"/empresas/{RUC}/comprobantes", json=_cuerpo("F002-000026"))
assert r.status_code == 201 and r.json()["estado"] == "anulado", r.text
assert len(enviados) == n_antes, "un anulado por plazo no debe enviarse de nuevo"
print("12. Reintentos: rechazado y error se reenvían; aceptado nunca; ante la duda no OK")
