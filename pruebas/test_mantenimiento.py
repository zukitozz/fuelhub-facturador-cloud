"""
Pruebas de humo de deploy/mantenimiento_diario.py. Se simulan los dos umbrales
de antigüedad retrocediendo el mtime de los archivos con os.utime (no se espera
días reales) — eso es lo único que el script mira para decidir, así que alcanza
para probar las dos fases de punta a punta: DATA+CDR -> archivo_frio/ -> borrado
definitivo, dejando siempre la fila de DOCUMENTO intacta.
"""
import os, sys, sqlite3, zipfile, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.configuracion as configuracion
import deploy.mantenimiento_diario as mantenimiento

RUC = "20609785269"

base = tempfile.mkdtemp()
data_dir = os.path.join(base, "sunat_archivos", "sfs", "DATA")
rpta_dir = os.path.join(base, "sunat_archivos", "sfs", "RPTA")
procesados_dir = os.path.join(rpta_dir, "procesados")
bd_dir = os.path.join(base, "bd")
for d in (data_dir, rpta_dir, procesados_dir, bd_dir):
    os.makedirs(d, exist_ok=True)

empresa = configuracion.Empresa(ruc=RUC, nombre="PRUEBA", sfs_base_url="http://localhost:0", ruta_base=base)

bd_path = os.path.join(bd_dir, "BDFacturador.db")
with sqlite3.connect(bd_path) as conn:
    conn.execute("""
        CREATE TABLE DOCUMENTO (
            NUM_RUC TEXT, TIP_DOCU TEXT, NUM_DOCU TEXT, NOM_ARCH TEXT,
            IND_SITU TEXT, DES_OBSE TEXT, FEC_GENE TEXT, FEC_ENVI TEXT, NUM_TICKET TEXT
        )
    """)
    conn.execute(
        "INSERT INTO DOCUMENTO VALUES (?,?,?,?,?,?,?,?,?)",
        (RUC, "01", "F002-000002", f"{RUC}-01-F002-000002", "11", "-",
         "01/10/2026 10:00:00", "01/10/2026 10:00:01", None),
    )

# Archivos como los dejaría facturador-api de verdad: 5 en DATA + el CDR en procesados/.
for ext in ("CAB", "DET", "TRI", "LEY", "PAG"):
    with open(os.path.join(data_dir, f"{RUC}-01-F002-000002.{ext}"), "w") as fh:
        fh.write("contenido de prueba")

cdr_path = os.path.join(procesados_dir, f"R{RUC}-01-F002-000002.zip")
with zipfile.ZipFile(cdr_path, "w") as z:
    z.writestr(f"R{RUC}-01-F002-000002.xml", "<ApplicationResponse>ACEPTADO</ApplicationResponse>")

# 1) Recién cerrado (CDR de hoy): todavía no debe moverse a frío.
mantenimiento.procesar_empresa(empresa)
print("1. Recién cerrado -> sigue en DATA:", sorted(os.listdir(data_dir)))
assert len(os.listdir(data_dir)) == 5, "no debió tocar nada, el CDR es de hoy"
assert os.path.exists(cdr_path)

# 2) CDR de hace 8 días (pasó el umbral de 7): debe moverse a archivo_frio/ y
# desaparecer de DATA/procesados.
hace_8_dias = time.time() - 8 * mantenimiento._SEG_POR_DIA
os.utime(cdr_path, (hace_8_dias, hace_8_dias))
mantenimiento.procesar_empresa(empresa)
frio_dir = os.path.join(base, "archivo_frio")
tar_path = os.path.join(frio_dir, f"{RUC}-01-F002-000002.tar.gz")
print("2. Tras 8 días -> DATA:", os.listdir(data_dir), "| frio:", os.listdir(frio_dir))
assert os.listdir(data_dir) == [], "los 5 archivos debieron moverse a frío"
assert not os.path.exists(cdr_path), "el CDR original debió borrarse tras empaquetarlo"
assert os.path.exists(tar_path), "debió crearse el .tar.gz en archivo_frio/"

# La fila de DOCUMENTO nunca se toca — el estado sigue consultable.
with sqlite3.connect(bd_path) as conn:
    fila = conn.execute(
        "SELECT IND_SITU FROM DOCUMENTO WHERE NUM_RUC=? AND NUM_DOCU='F002-000002'", (RUC,)
    ).fetchone()
assert fila == ("11",), "la fila de DOCUMENTO no debía tocarse"

# 3) Mismo archivo en frío, pero todavía no pasó el segundo umbral (8 días ahí):
# una corrida más no debe borrarlo antes de tiempo.
mantenimiento.procesar_empresa(empresa)
print("3. Recién archivado -> sigue en frío:", os.listdir(frio_dir))
assert os.path.exists(tar_path), "no debió borrarse antes del segundo umbral"

# 4) El .tar.gz lleva 9 días en frío (pasó el umbral de 8): debe borrarse del todo.
hace_9_dias = time.time() - 9 * mantenimiento._SEG_POR_DIA
os.utime(tar_path, (hace_9_dias, hace_9_dias))
mantenimiento.procesar_empresa(empresa)
print("4. Tras 9 días en frío -> archivo_frio:", os.listdir(frio_dir))
assert os.listdir(frio_dir) == [], "el .tar.gz debió borrarse definitivamente"

# La fila sigue intacta incluso después del borrado definitivo.
with sqlite3.connect(bd_path) as conn:
    fila = conn.execute(
        "SELECT IND_SITU FROM DOCUMENTO WHERE NUM_RUC=? AND NUM_DOCU='F002-000002'", (RUC,)
    ).fetchone()
assert fila == ("11",), "la fila de DOCUMENTO debe sobrevivir incluso al borrado definitivo"

print("\nTODO OK")
