"""
Pruebas de humo de facturador-api, sin necesidad de un SFS real: se apunta
configuracion.obtener_empresa a una carpeta temporal y se reemplaza
sfs_cliente.generar_y_enviar por un doble que no toca la red. Lo que SÍ se
verifica de verdad es lo que no depende de SFS: que los 5 archivos se escriben
con extensión en MAYÚSCULAS (la causa real del primer bug encontrado migrando a
Linux) y que la consulta de estado/CDR lee bien lo que ya dejó SFS en disco.
"""
import os, sys, sqlite3, zipfile, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import app.configuracion as configuracion
import app.sfs_cliente as sfs_cliente
import app.main as main_mod
from app.main import app as fastapi_app

RUC = "20609785269"

base = tempfile.mkdtemp()
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
    "receptor": {"razon_social": "CLIENTE DE PRUEBA"},
    "items": [{"descripcion": "ITEM", "cantidad": 1,
               "valor": 0.847458, "valor_venta": 0.85, "igv_venta": 0.15, "precio": 1.00}],
})
print("3. Factura válida ->", r.status_code, r.json())
assert r.status_code == 201
assert r.json() == {"codigo": "01-F002-000002", "ind_situ": "11", "des_obse": "-"}

archivos_escritos = sorted(os.listdir(data_dir))
print("   archivos en DATA:", archivos_escritos)
esperado = sorted(f"{RUC}-01-F002-000002.{e}" for e in ("CAB", "DET", "TRI", "LEY", "PAG"))
assert archivos_escritos == esperado, "extensiones deben quedar en MAYÚSCULAS (el bug de Linux)"

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
assert r.json()["cdr_xml"] is None

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
assert "ACEPTADO" in r.json()["cdr_xml"]

print("\nTODO OK")
