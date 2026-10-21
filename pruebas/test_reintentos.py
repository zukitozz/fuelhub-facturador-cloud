"""
Pruebas del reintento automático (app/reintentos.py) sin SFS ni SUNAT reales: la bandeja de
SFS es una SQLite temporal, sfs_cliente y la consulta a SUNAT son dobles, y el reloj es
falso para recorrer "un día" sin esperar. Se ejecuta como script (igual que las demás):
    python pruebas/test_reintentos.py
"""
import os, sys, sqlite3, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import app.configuracion as configuracion
import app.consulta_integrada as ci
import app.errores as errores
import app.main as main_mod
import app.reintentos as reintentos
import app.sfs_cliente as sfs_cliente
from app import archivos

RUC = "20612016527"
base = tempfile.mkdtemp()
os.environ["FACTURADOR_API_RECHAZOS_LOG"] = os.path.join(base, "rechazos.jsonl")
os.environ["FACTURADOR_API_REINTENTOS_BD"] = os.path.join(base, "reintentos.db")
data_dir = os.path.join(base, "sunat_archivos", "sfs", "DATA")
bd_dir = os.path.join(base, "bd")
for d in (data_dir, os.path.join(base, "sunat_archivos", "sfs", "FIRMA"), bd_dir):
    os.makedirs(d, exist_ok=True)

empresa = configuracion.Empresa(ruc=RUC, nombre="PRUEBA", sfs_base_url="http://localhost:0", ruta_base=base)
configuracion.obtener_empresa = lambda ruc: empresa if ruc == RUC else None
configuracion.listar_empresas = lambda: [empresa]

BD = os.path.join(bd_dir, "BDFacturador.db")
with sqlite3.connect(BD) as c:
    c.execute("CREATE TABLE DOCUMENTO (NUM_RUC TEXT, TIP_DOCU TEXT, NUM_DOCU TEXT, NOM_ARCH TEXT, "
              "IND_SITU TEXT, DES_OBSE TEXT, FEC_GENE TEXT, FEC_ENVI TEXT, NUM_TICKET TEXT)")

# Parámetros chicos y reloj falso: 2 intentos por ronda, 2 rondas, espera 10, pausa 1000.
reintentos.INTENTOS_POR_RONDA, reintentos.RONDAS = 2, 2
reintentos.ESPERA_ENTRE_INTENTOS_SEG, reintentos.PAUSA_ENTRE_RONDAS_SEG = 10, 1000
reloj = [1_000_000]
reintentos._ahora = lambda: reloj[0]


def doc(tipo, numero, ind_situ, obse="-"):
    with sqlite3.connect(BD) as c:
        c.execute("DELETE FROM DOCUMENTO WHERE NUM_DOCU=?", (numero,))
        c.execute("INSERT INTO DOCUMENTO VALUES (?,?,?,?,?,?,?,?,?)",
                  (RUC, tipo, numero, f"{RUC}-{tipo}-{numero}", ind_situ, obse, "20/10/2026 10:00:00", None, None))


def estado_sfs(numero):
    with sqlite3.connect(BD) as c:
        return c.execute("SELECT IND_SITU FROM DOCUMENTO WHERE NUM_DOCU=?", (numero,)).fetchone()


def archivos_data(tipo, numero, total="15.00"):
    ext = "NOT" if tipo in ("07", "08") else "CAB"
    nombre = os.path.join(data_dir, f"{RUC}-{tipo}-{numero}.{ext}")
    if ext == "CAB":
        linea = f"0101|2026-10-17|19:00:00|-|0000|6|20602831214|CLIENTE SAC|PEN|2.29|12.71|{total}|0.00|0.00|0.00|{total}|2.1|2.0|"
    else:
        linea = f"0101|2026-10-17|19:00:00|0000|6|20602831214|CLIENTE SAC|PEN|01|ANULA|01|F003-000001|2.29|12.71|{total}|0.00|0.00|0.00|{total}|2.1|2.0|"
    open(nombre, "w", encoding="utf-8").write(linea + "\n")


def seguimiento(numero):
    return {f["codigo"]: f for f in reintentos.listar(RUC)}[f"01-{numero}"]


# Dobles: SUNAT (consulta integrada) y SFS (qué responde al reenviar).
sunat = {"estado": ci.NO_EXISTE}
consultas_sunat = []
def _consulta(emp, ruc, tipo, serie, numero, fecha, monto):
    consultas_sunat.append((tipo, serie, numero, fecha.strftime("%d/%m/%Y"), monto))
    return sunat["estado"], "x"
ci.consultar_comprobante = _consulta

respuesta = {"fila": ("11", "-"), "corte": False}
envios = []
def _sfs(base_url, ruc, tipo, numero):
    envios.append(numero)
    if respuesta["corte"]:
        raise sfs_cliente.SfsError("sin conexion")
    ind, obse = respuesta["fila"]
    doc(tipo, numero, ind, obse)    # SFS vuelve a registrar el documento con el resultado
    return {"ind_situ": ind, "des_obse": obse}
sfs_cliente.generar_y_enviar = _sfs

TRANSITORIO = "El sistema no puede responder su solicitud. (El servicio de autenticación no está disponible)"
DEFINITIVO = "3103 - Error en la linea: 1 Tributo: 1000, MontoTributoCalculado: 9.91, MontoTributo: 0.50"


def ciclo():
    reintentos.observar(empresa)
    reintentos.observar_resumenes(empresa)
    return reintentos.procesar_vencidos(empresa)


# 1) Clasificación de errores: los textos reales de producción.
for texto, esperado in [
    (TRANSITORIO, True),
    ("No se pudo procesar su solicitud. (Ocurrio un error en el batch) - Detalle: xxx", True),
    ("El sistema no puede responder su solicitud. (No se pudo obtener el ticket de proceso)", True),
    ("Hubo un problema al invocar servicio SUNAT: Could not send Message.", True),
    ("Read timed out", True),
    (DEFINITIVO, False),
    ("2800 - El dato ingresado en el tipo de documento no es valido", False),
    ("0102 - Usuario o contrasena incorrectos", False),
    ("", False), (None, False), ("-", False),
]:
    assert errores.es_transitorio(texto) is esperado, texto
print("1. Clasificación transitorio/definitivo OK")

# 2) Fecha y total salen de la cabecera de DATA (factura y nota).
archivos_data("01", "F003-000100", "283.08")
f, t = archivos.leer_fecha_y_total(data_dir, RUC, "01", "F003-000100")
assert (f.strftime("%d/%m/%Y"), t) == ("17/10/2026", 283.08)
archivos_data("07", "FC03-000005", "50.00")
assert archivos.leer_fecha_y_total(data_dir, RUC, "07", "FC03-000005")[1] == 50.0
assert archivos.leer_fecha_y_total(data_dir, RUC, "01", "F003-999999") is None
print("2. leer_fecha_y_total OK")

# 3) Un fallo definitivo va directo a manual y NUNCA se reenvía; uno transitorio queda activo.
archivos_data("01", "F003-000001"); doc("01", "F003-000001", "06", DEFINITIVO)
archivos_data("01", "F003-000002"); doc("01", "F003-000002", "10", TRANSITORIO)
assert ciclo() == 0, "recién descubierto: el primer reintento espera su turno"
assert seguimiento("F003-000001")["estado"] == "manual"
assert "definitivo" in seguimiento("F003-000001")["motivo"]
assert seguimiento("F003-000002")["estado"] == "activo" and seguimiento("F003-000002")["ronda"] == 1
reloj[0] += 1000
assert ciclo() == 1 and envios == ["F003-000002"], "el transitorio sí se reintenta; el definitivo no"
envios.clear(); consultas_sunat.clear()
print("3. Definitivo -> manual; transitorio -> activo OK")

# 4) Antes del reenvío se consulta a SUNAT; si ya lo tiene, NO se reenvía y queda resuelto.
archivos_data("01", "F003-000010"); doc("01", "F003-000010", "10", TRANSITORIO)
reintentos.observar(empresa)
reloj[0] += 11
sunat["estado"] = ci.ACEPTADO
respuesta["fila"] = ("11", "-")
envios.clear(); consultas_sunat.clear()
ciclo()
assert "F003-000010" not in envios, "SUNAT ya lo tenía: no se reenvía"
assert ("01", "F003", "000010", "17/10/2026", 15.0) in consultas_sunat, consultas_sunat
assert seguimiento("F003-000010")["estado"] == "resuelto"
assert "SUNAT ya lo tiene" in seguimiento("F003-000010")["motivo"]
ciclo(); ciclo()
assert seguimiento("F003-000010")["estado"] == "resuelto", "la fila vieja de SFS no debe reabrirlo"
print("4. SUNAT ya lo tiene -> no se reenvía, resuelto y no se reabre OK")

# 5) SUNAT no lo tiene -> se reenvía; si sale bien, resuelto.
archivos_data("01", "F003-000011"); doc("01", "F003-000011", "06", "Could not send Message")
reintentos.observar(empresa)
reloj[0] += 11
sunat["estado"] = ci.NO_EXISTE
envios.clear()
ciclo()
assert envios == ["F003-000011"]
assert seguimiento("F003-000011")["estado"] == "resuelto" and "con éxito" in seguimiento("F003-000011")["motivo"]
assert estado_sfs("F003-000011") == ("11",)
print("5. Reenvío exitoso -> resuelto OK")

# 6) Rondas: 2 intentos separados 10 s, pausa de 1000 s, otra ronda de 2 y luego manual.
archivos_data("01", "F003-000012"); doc("01", "F003-000012", "10", TRANSITORIO)
reintentos.observar(empresa)
respuesta["fila"] = ("10", TRANSITORIO)
envios.clear()
reloj[0] += 11; ciclo()
s = seguimiento("F003-000012")
assert (s["estado"], s["ronda"], s["intentos_en_la_ronda"]) == ("activo", 1, 1), s
ciclo()
assert len(envios) == 1, "todavía no pasaron los 10 s entre intentos"
reloj[0] += 11; ciclo()
s = seguimiento("F003-000012")
assert (s["estado"], s["ronda"], s["intentos_en_la_ronda"]) == ("activo", 2, 0), s
reloj[0] += 500; ciclo()
assert len(envios) == 2, "en la pausa no se reintenta"
reloj[0] += 600; ciclo()
assert len(envios) == 3
reloj[0] += 11; ciclo()
s = seguimiento("F003-000012")
assert s["estado"] == "manual" and "agotó 2 rondas" in s["motivo"], s
assert s["ultimo_error"] == TRANSITORIO, "queda con el último estado"
n = len(envios)
reloj[0] += 5000; ciclo()
assert len(envios) == n, "en manual no se reintenta más"
print("6. Rondas, pausa y paso a manual OK")

# 7) Si en el reintento SUNAT rechaza por contenido, no se insiste: manual.
archivos_data("01", "F003-000013"); doc("01", "F003-000013", "10", TRANSITORIO)
reintentos.observar(empresa)
respuesta["fila"] = ("10", DEFINITIVO)
reloj[0] += 11; ciclo()
s = seguimiento("F003-000013")
assert s["estado"] == "manual" and "contenido" in s["motivo"] and s["ultimo_error"] == DEFINITIVO
print("7. Rechazo por contenido en el reintento -> manual OK")

# 8) Cortes de SFS cuentan como intento; sin poder verificar a SUNAT no se reenvía a ciegas.
archivos_data("01", "F003-000014"); doc("01", "F003-000014", "06", "Could not send Message")
reintentos.observar(empresa)
sunat["estado"] = ci.DESCONOCIDO
envios.clear()
reloj[0] += 11; ciclo()
assert envios == [] and seguimiento("F003-000014")["intentos_en_la_ronda"] == 1
assert "no se pudo verificar" in seguimiento("F003-000014")["ultimo_error"]
sunat["estado"] = ci.NO_EXISTE; respuesta["corte"] = True
reloj[0] += 11; ciclo()
assert envios == ["F003-000014"] and "SFS no disponible" in seguimiento("F003-000014")["ultimo_error"]
respuesta["corte"] = False
print("8. SUNAT sin confirmar y SFS caído: cuentan como intento, sin reenvío a ciegas OK")

# 9) Sin los archivos de DATA no se puede preguntar a SUNAT ni releer: manual.
doc("01", "F003-000015", "06", "Could not send Message")
reintentos.observar(empresa)
reloj[0] += 11; ciclo()
assert seguimiento("F003-000015")["estado"] == "manual" and "DATA" in seguimiento("F003-000015")["motivo"]
print("9. Sin archivos en DATA -> manual OK")

# 10) Un manual que se corrige y se reenvía bien (POST de quien factura) se cierra solo.
doc("01", "F003-000001", "11")
reintentos.observar(empresa)
assert seguimiento("F003-000001")["estado"] == "resuelto"
print("10. Manual corregido -> resuelto OK")

# 11) El reintento no se cruza con un POST del mismo comprobante.
archivos_data("01", "F003-000016"); doc("01", "F003-000016", "10", TRANSITORIO)
reintentos.observar(empresa)
sunat["estado"] = ci.NO_EXISTE; respuesta["fila"] = ("11", "-"); envios.clear()
reloj[0] += 11
with reintentos.bloqueo(RUC, "01-F003-000016"):
    assert ciclo() == 0 and envios == [], "lo está enviando otro: se cede"
assert ciclo() == 1 and envios == ["F003-000016"]
print("11. Bloqueo por comprobante OK")

# 12) El listado por API.
client = TestClient(main_mod.app)
r = client.get(f"/empresas/{RUC}/reintentos?estado=manual")
assert r.status_code == 200, r.text
codigos = {i["codigo"] for i in r.json()["reintentos"]}
assert "01-F003-000012" in codigos and "01-F003-000010" not in codigos
assert all(i["estado"] == "manual" for i in r.json()["reintentos"])
assert client.get(f"/empresas/{RUC}/reintentos?estado=raro").status_code == 400
assert client.get("/empresas/00000000000/reintentos").status_code == 404
assert client.get(f"/empresas/{RUC}/reintentos").json()["total"] >= 8
print("12. GET /reintentos OK")

# 13) Resúmenes diarios (RC) con el ticket sin resolver: el ciclo los resuelve solo.
import app.ticket_resumen as tr
def rc(numero, ind_situ="08", ticket="T-1"):
    with sqlite3.connect(BD) as c:
        c.execute("DELETE FROM DOCUMENTO WHERE NUM_DOCU=?", (numero,))
        c.execute("INSERT INTO DOCUMENTO VALUES (?,?,?,?,?,?,?,?,?)",
                  (RUC, "RC", numero, f"{RUC}-RC-{numero}", ind_situ, "Nro. Ticket", "20/10/2026 05:00:00", None, ticket))
def seg_rc(numero):
    return {f["codigo"]: f for f in reintentos.listar(RUC)}[f"RC-{numero}"]

reintentos.ESPERA_RESUMEN_SEG = 5
consultas_ticket = []
def sunat_ticket(codigo, mensaje="Aceptado"):
    def f(emp, ruc, ticket):
        consultas_ticket.append(ticket)
        return codigo, mensaje, None
    return f

rc("RC-20261020-001"); rc("RC-20261020-002", ticket="T-2"); rc("RC-20261020-003", ticket="T-3")
rc("RC-20261020-004", ind_situ="06", ticket=None)    # nunca llegó a SUNAT: sin ticket, no hay qué leer
ciclo()
assert seg_rc("RC-20261020-001")["estado"] == "activo"
assert "RC-RC-20261020-004" not in {f["codigo"] for f in reintentos.listar(RUC)}
assert consultas_ticket == [], "recién descubierto: espera su turno"

# SUNAT todavía procesa -> cuenta intento y sigue; con veredicto -> se cierra la fila y queda resuelto.
tr._consultar_ticket_sunat = sunat_ticket("98", "En proceso")
reloj[0] += 6; ciclo()
assert seg_rc("RC-20261020-001")["intentos_en_la_ronda"] == 1 and seg_rc("RC-20261020-001")["estado"] == "activo"
cdr_zip = __import__("io").BytesIO()
with __import__("zipfile").ZipFile(cdr_zip, "w") as z:
    z.writestr("R.xml", "<ApplicationResponse/>")
tr._consultar_ticket_sunat = lambda emp, ruc, ticket: (
    ("0", "Aceptado", cdr_zip.getvalue()) if ticket == "T-1" else ("98", "En proceso", None))
reloj[0] += 6; ciclo()
assert seg_rc("RC-20261020-001")["estado"] == "resuelto" and "CERRADO" in seg_rc("RC-20261020-001")["motivo"]
assert estado_sfs("RC-20261020-001") == ("03",), "el resumen se cierra en la bandeja de SFS"
assert os.path.exists(os.path.join(empresa.rpta_dir, f"R{RUC}-RC-RC-20261020-001.zip")), "y deja su CDR"

# Un ticket que SUNAT ya no conoce es definitivo: manual, no se insiste.
tr._consultar_ticket_sunat = sunat_ticket("127", "ticket inexistente")
reloj[0] += 1000   # pasó la pausa de la ronda 1 (el 002 llevaba 2 intentos "en proceso")
ciclo()
assert seg_rc("RC-20261020-002")["estado"] == "manual" and "portal" in seg_rc("RC-20261020-002")["motivo"]
consultadas = consultas_ticket.count("T-2")
reloj[0] += 6; ciclo()
assert consultas_ticket.count("T-2") == consultadas, "en manual no se vuelve a consultar"

# Cerrado por otro camino (alguien hizo el GET): el seguimiento se cierra sin consultar a SUNAT.
rc("RC-20261020-003", ind_situ="03", ticket="T-3")
antes = len(consultas_ticket)
ciclo()
assert seg_rc("RC-20261020-003")["estado"] == "resuelto" and "T-3" not in consultas_ticket[antes:]
print("13. Resúmenes: se resuelven solos, 127 -> manual, cerrados por otro camino OK")

# 14) El hilo arranca y se detiene, y se puede apagar.
os.environ["FACTURADOR_API_REINTENTOS"] = "0"
assert reintentos.iniciar() is None
os.environ["FACTURADOR_API_REINTENTOS"] = "1"
parar = reintentos.iniciar(); assert parar is not None; parar.set()
print("14. Hilo de fondo OK")
print("TODO OK")
