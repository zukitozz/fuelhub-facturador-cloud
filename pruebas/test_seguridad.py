"""API keys con alcance por RUC y migración progresiva (app/seguridad.py, app/claves.py).
Se usa GET /empresas/{ruc}/rechazos: si pasa la autorización responde 404 (RUC no dado de alta
en esta prueba), así que 404 = "autorizado", 401/403 = bloqueado."""
import os, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
base = tempfile.mkdtemp()
RUTA = os.path.join(base, "api_keys.yaml")
os.environ["FACTURADOR_API_KEYS_YAML"] = RUTA
os.environ["FACTURADOR_API_RECHAZOS_LOG"] = os.path.join(base, "rechazos.jsonl")
os.environ["FACTURADOR_API_REINTENTOS_BD"] = os.path.join(base, "reintentos.db")

import yaml
from fastapi.testclient import TestClient

import app.claves as claves
import app.main as main_mod

client = TestClient(main_mod.app)
A, B = "20609785269", "20100011884"


def get(ruc, clave=None):
    codigo = client.get(f"/empresas/{ruc}/rechazos", headers={"X-API-Key": clave} if clave else {}).status_code
    return codigo if codigo in (401, 403) else 404  # cualquier otra respuesta = pasó la autorización


def escribir(datos):
    with open(RUTA, "w", encoding="utf-8") as fh:
        yaml.safe_dump(datos, fh)
    t = time.time() + escribir.n; escribir.n += 2
    os.utime(RUTA, (t, t))  # el cache se invalida por mtime
escribir.n = 2


# 1. Sin archivo de claves: todo abierto (producción actual).
assert not os.path.exists(RUTA)
assert get(A) == 404 and client.get("/salud").status_code == 200

# 2. Alta de claves por CLI: imprime la clave una vez y guarda solo el hash.
import io, contextlib
def nueva(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert claves.main(["nueva", *args]) == 0
    return out.getvalue().strip().splitlines()[-1]

clave_a = nueva("--nombre", "fuelhub", "--rucs", A)
clave_todos = nueva("--nombre", "gimnasio", "--rucs", "*")
texto = open(RUTA, encoding="utf-8").read()
assert clave_a not in texto and clave_todos not in texto, "la clave en claro no debe quedar en el archivo"
assert claves.main(["nueva", "--nombre", "fuelhub", "--rucs", A]) == 1      # nombre repetido
assert claves.main(["nueva", "--nombre", "x", "--rucs", "123"]) == 1        # RUC inválido

# 3. Modo permisivo: sin clave pasa; con clave se valida y respeta el alcance.
assert get(A) == 404 and get(B) == 404                     # sin clave: sigue abierto
assert get(A, clave_a) == 404                              # clave válida y RUC en su alcance
assert get(B, clave_a) == 403                              # RUC fuera de su alcance
assert get(B, clave_todos) == 404                          # "*"
assert get(A, "fh_inventada") == 401                       # clave desconocida
assert client.get("/salud", headers={"X-API-Key": "fh_inventada"}).status_code == 200  # /salud sin RUC: abierto

# 4. Corte progresivo por empresa: solo A exige clave.
datos = yaml.safe_load(open(RUTA, encoding="utf-8")); datos["exigir_rucs"] = [A]; escribir(datos)
assert get(A) == 401 and get(B) == 404
assert get(A, clave_a) == 404

# 5. Corte global.
datos["exigir_rucs"] = []; datos["exigir"] = True; escribir(datos)
assert get(A) == 401 and get(B) == 401 and get(B, clave_todos) == 404
assert client.get("/salud").status_code == 200

# 6. Un archivo roto no abre ni tumba la API: se conserva lo último bueno.
with open(RUTA, "w", encoding="utf-8") as fh:
    fh.write("claves: [\n")
t = time.time() + 100; os.utime(RUTA, (t, t))
assert get(A) == 401 and get(B, clave_todos) == 404

# 7. Swagger declara el esquema de seguridad.
assert "APIKeyHeader" in client.get("/openapi.json").text

print("OK: API keys — alcance por RUC, modo permisivo, corte por empresa y global, archivo roto")
