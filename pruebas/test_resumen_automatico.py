"""
Pruebas del envío automático del resumen diario (app/resumen_automatico.py) y del registro de
qué boletas entró en cada resumen (app/registro_resumenes.py), sin SFS ni SUNAT reales.
    python pruebas/test_resumen_automatico.py
"""
import os, sys, sqlite3, tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import app.configuracion as configuracion
import app.main as main_mod
import app.registro_resumenes as registro
import app.reintentos as reintentos
import app.resumen_automatico as auto
import app.sfs_cliente as sfs_cliente

RUC = "20612024821"
LIMA = timezone(timedelta(hours=-5))
base = tempfile.mkdtemp()
os.environ["FACTURADOR_API_RECHAZOS_LOG"] = os.path.join(base, "rechazos.jsonl")
os.environ["FACTURADOR_API_REINTENTOS_BD"] = os.path.join(base, "reintentos.db")
data_dir = os.path.join(base, "sunat_archivos", "sfs", "DATA")
bd_dir = os.path.join(base, "bd")
for d in (data_dir, os.path.join(base, "sunat_archivos", "sfs", "RPTA", "procesados"), bd_dir):
    os.makedirs(d, exist_ok=True)

empresa = configuracion.Empresa(ruc=RUC, nombre="PRUEBA", sfs_base_url="http://localhost:0", ruta_base=base)
configuracion.obtener_empresa = lambda ruc: empresa if ruc == RUC else None
configuracion.listar_empresas = lambda: [empresa]

BD = os.path.join(bd_dir, "BDFacturador.db")
with sqlite3.connect(BD) as c:
    c.execute("CREATE TABLE DOCUMENTO (NUM_RUC TEXT, TIP_DOCU TEXT, NUM_DOCU TEXT, NOM_ARCH TEXT, "
              "IND_SITU TEXT, DES_OBSE TEXT, FEC_GENE TEXT, FEC_ENVI TEXT, NUM_TICKET TEXT)")
    c.execute("CREATE TABLE ERROR (COD_CATAERRO VARCHAR(4) PRIMARY KEY NOT NULL, NOM_CATAERRO VARCHAR(120) NOT NULL, IND_ESTADO VARCHAR(1) NOT NULL)")

hoy = datetime.now(LIMA).date()
def lima(hora, minuto):
    return int(datetime(hoy.year, hoy.month, hoy.day, hora, minuto, tzinfo=LIMA).timestamp())
reloj = [lima(0, 5)]
reintentos._ahora = lambda: reloj[0]
reintentos.ESPERA_ENTRE_INTENTOS_SEG = 600
reintentos.INTENTOS_POR_RONDA = 3


def boleta(numero, dias_atras, ind_situ="02"):
    fecha = (hoy - timedelta(days=dias_atras)).isoformat()
    open(os.path.join(data_dir, f"{RUC}-03-{numero}.CAB"), "w", encoding="utf-8").write(
        f"0101|{fecha}|10:00:00|-|0000|0|00000000|CLIENTES VARIOS|PEN|1.53|8.47|10.00|0.00|0.00|0.00|10.00|2.1|2.0|\n")
    with sqlite3.connect(BD) as c:
        c.execute("DELETE FROM DOCUMENTO WHERE NUM_DOCU=?", (numero,))
        c.execute("INSERT INTO DOCUMENTO VALUES (?,?,?,?,?,?,?,?,?)",
                  (RUC, "03", numero, f"{RUC}-03-{numero}", ind_situ, "-", "x", None, None))


def fila(tipo, numero):
    with sqlite3.connect(BD) as c:
        return c.execute("SELECT IND_SITU, DES_OBSE FROM DOCUMENTO WHERE TIP_DOCU=? AND NUM_DOCU=?", (tipo, numero)).fetchone()


# SFS doble: el envío del RC lo deja registrado como lo haría SFS, según `modo`.
modo = {"v": "ok"}
enviados = []
def _sfs(base_url, ruc, tipo, numero):
    enviados.append(numero)
    if modo["v"] == "corte":
        raise sfs_cliente.SfsError("sin conexion")
    ind, obse = ("06", "Could not send Message") if modo["v"] == "error" else ("08", "Nro. Ticket: 123")
    with sqlite3.connect(BD) as c:
        c.execute("INSERT INTO DOCUMENTO VALUES (?,?,?,?,?,?,?,?,?)",
                  (RUC, tipo, numero, f"{RUC}-RC-{numero}", ind, obse, "x", None, "T-1"))
    return {"ind_situ": ind, "des_obse": obse}
sfs_cliente.generar_y_enviar = _sfs


# 1) Antes de la hora (00:05 < 00:10): no hace nada.
boleta("B001-000001", 1); boleta("B001-000002", 1); boleta("B001-000003", 2)
auto.ciclo_empresa(empresa)
assert enviados == [] and registro.corrida(RUC, hoy.isoformat()) is None
print("1. Antes de las 00:10 no envía OK")

# 2) Pasada la hora: un resumen por cada fecha pendiente (ayer y anteayer), con sus boletas.
reloj[0] = lima(0, 20)
auto.ciclo_empresa(empresa)
assert len(enviados) == 2, enviados
assert fila("03", "B001-000001") is None and fila("03", "B001-000003") is None, "las declaradas salen de la bandeja"
rcs = {r["numeracion_rc"]: r for r in registro.listar(RUC)}
assert sorted(sum((r["boletas"] for r in rcs.values()), [])) == ["B001-000001", "B001-000002", "B001-000003"]
assert registro.corrida(RUC, hoy.isoformat())["estado"] == "hecha"
print("2. Envía un resumen por fecha pendiente y registra sus boletas OK")

# 3) El mismo día no repite (ni con ciclos de más, ni si alguien llama al endpoint).
n = len(enviados)
reloj[0] += 3600; auto.ciclo_empresa(empresa); auto.ciclo_empresa(empresa)
assert len(enviados) == n
client = TestClient(main_mod.app)
r = client.post(f"/empresas/{RUC}/resumenes-diarios")
assert r.status_code == 200 and "No hay boletas" in r.json()["mensaje"], r.text
print("3. No repite el mismo día; el endpoint sigue compatible OK")

# 4) Consulta: GET /resumenes y el GET de una boleta ya declarada.
r = client.get(f"/empresas/{RUC}/resumenes")
assert r.status_code == 200 and r.json()["total"] == 2
rc1 = next(x for x in r.json()["resumenes"] if "B001-000001" in x["boletas"])
assert rc1["estado"] == "enviado" and rc1["cantidad_boletas"] == 2 and rc1["fecha_referencia"] == (hoy - timedelta(days=1)).isoformat()
r = client.get(f"/empresas/{RUC}/comprobantes/03-B001-000001")
assert r.status_code == 200 and r.json()["estado"] == "enviado" and rc1["numeracion_rc"] in r.json()["des_obse"], r.text
assert client.get(f"/empresas/{RUC}/comprobantes/03-B001-999999").status_code == 404
assert client.get(f"/empresas/{RUC}/resumenes?desde=2999-01-01").json()["total"] == 0
assert client.get("/empresas/00000000000/resumenes").status_code == 404
print("4. GET /resumenes y GET de boleta declarada OK")

# 5) Al día siguiente corre de nuevo; una boleta vencida (9 días) se anula y NO entra al resumen.
reloj[0] += 86400
boleta("B001-000010", 1); boleta("B001-000011", 9)
enviados.clear()
auto.ciclo_empresa(empresa)
assert fila("03", "B001-000011")[0] == "05" and "venció el plazo de 7 días" in fila("03", "B001-000011")[1]
assert [i["codigo"] for i in reintentos.listar(RUC, "anulado")] == ["03-B001-000011"]
assert len(enviados) == 1 and "B001-000011" not in sum((r["boletas"] for r in registro.listar(RUC)), [])
print("5. Boleta vencida: anulada y fuera del resumen OK")

# 6) SFS caído: reintenta cada 10 min, 3 veces como máximo ese día, y deja el caso en manual.
reloj[0] += 86400
boleta("B001-000020", 1); modo["v"] = "corte"; enviados.clear()
auto.ciclo_empresa(empresa)
c = registro.corrida(RUC, (hoy + timedelta(days=0)).isoformat())
fecha_corrida = datetime.fromtimestamp(reloj[0], LIMA).date().isoformat()
c = registro.corrida(RUC, fecha_corrida)
assert c["estado"] == "reintentando" and c["intentos"] == 1 and c["proximo"] == reloj[0] + 600
auto.ciclo_empresa(empresa)
assert len(enviados) == 1, "todavía no pasaron los 10 minutos"
reloj[0] += 601; auto.ciclo_empresa(empresa)
reloj[0] += 601; auto.ciclo_empresa(empresa)
c = registro.corrida(RUC, fecha_corrida)
assert c["estado"] == "manual" and c["intentos"] == 3 and "sin conexion" in c["detalle"]
assert fila("03", "B001-000020")[0] == "02", "la boleta sigue pendiente"
manuales = [i["codigo"] for i in reintentos.listar(RUC, "manual")]
assert any(m.startswith("RC-AUTOMATICO-") for m in manuales), manuales
n = len(enviados); reloj[0] += 5000; auto.ciclo_empresa(empresa)
assert len(enviados) == n, "en manual no insiste ese día"
print("6. SFS caído: 3 intentos y luego manual OK")

# 7) Mañana, con SFS de vuelta, la boleta pendiente sale sola.
reloj[0] += 86400; modo["v"] = "ok"; enviados.clear()
auto.ciclo_empresa(empresa)
assert len(enviados) == 1 and fila("03", "B001-000020") is None
assert any("B001-000020" in r["boletas"] for r in registro.listar(RUC))
print("7. Al día siguiente la boleta pendiente sale sola OK")

# 8) Un resumen que SFS deja en error: las boletas siguen pendientes y se reintenta.
reloj[0] += 86400
boleta("B001-000030", 1); modo["v"] = "error"; enviados.clear()
auto.ciclo_empresa(empresa)
fecha_corrida = datetime.fromtimestamp(reloj[0], LIMA).date().isoformat()
assert registro.corrida(RUC, fecha_corrida)["estado"] == "reintentando"
assert fila("03", "B001-000030")[0] == "02", "con el RC en error las boletas no se consumen"
assert not any("B001-000030" in r["boletas"] for r in registro.listar(RUC))
print("8. RC en error: boletas pendientes y reintento OK")

# 9) El registro se reconstruye de un .RDI para resúmenes anteriores al registro.
open(os.path.join(data_dir, f"{RUC}-RC-20260101-001.RDI"), "w", encoding="utf-8").write(
    "2025-12-31|2026-01-01|03|B009-000001|0|0|PEN|1.00|0.00|0.00|0.00|0.00|0.00|1.18|||||||||1|\n"
    "2025-12-31|2026-01-01|03|B009-000002|0|0|PEN|1.00|0.00|0.00|0.00|0.00|0.00|1.18|||||||||1|\n")
assert registro.reconstruir(empresa, ["RC-20260101-001", "RC-20260101-999"]) == 1
assert registro.rc_de_boleta(RUC, "B009-000002") == "RC-20260101-001"
assert registro.reconstruir(empresa, ["RC-20260101-001"]) == 0, "no duplica"
print("9. Reconstrucción desde .RDI OK")
print("TODO OK")
