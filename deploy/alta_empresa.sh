#!/bin/bash
# alta_empresa.sh — da de alta una empresa nueva: arma su instancia de SFS desde
# cero (carpetas, certificado, base, configuración) y la agrega a empresas.yaml de
# facturador-api. Corre en el mismo servidor donde ya vive la primera empresa.
#
# Automatiza los pasos manuales que costó descubrir dando de alta las dos primeras
# empresas (ver memoria de la migración / README de este repo). Varios de esos
# pasos tenían trampas reales que casi obligan a hacerlos a mano, y se resolvieron
# leyendo el bytecode del jar de SFS (decompilado con CFR) en vez de adivinar:
#
#   - El certificado NO se importa llamando a la pantalla web tal cual: ese
#     endpoint (/api/ImportarCertificado.htm) ejecuta su propio `keytool` para
#     armar el .jks y en Linux no siempre lo logra bien — pero SÍ hace falta
#     llamarlo, porque es el único lugar que registra PRKCRT/NOMCERT en
#     PARAMETRO (sin eso SFS rechaza todo con "Debe importar su certificado
#     digital", así se haya firmado bien). Por eso el .jks se genera DOS veces:
#     una antes de levantar SFS (para tener algo válido desde el arranque) y
#     otra vez después de llamar a ImportarCertificado.htm (por si su keytool
#     interno lo corrompió). El alias/contraseña del .jks final son fijos y
#     iguales para TODA empresa (certContribuyente / SuN@TF4CT) — no son la
#     contraseña real del .p12, son constantes hardcodeadas del vendor.
#     Confirmado firmando contra SUNAT producción el 2026-10-02.
#   - RUC/usuario SOL/clave SOL/ruta de trabajo se guardan llamando al endpoint
#     real (POST /api/GrabarParametro.htm): la clave SOL se guarda encriptada
#     con un método propio del jar (generarDocumentosService.Encriptar) que no
#     tiene sentido reimplementar.
#   - Nombre comercial/UBIGEO/dirección se guardan con OTRO endpoint aparte
#     (POST /api/GrabarOtrosParametros.htm) que GrabarParametro.htm NO toca.
#     Sin esto, SFS rechaza todo con "Debe ingresar el parámetro de nombre
#     completo..." aunque el RUC/SOL/certificado ya estén bien. Se descubrió
#     dando de alta la segunda empresa (Spaxion): la primera (Sircon) nunca lo
#     notó porque heredó esos campos ya llenos de su base real de producción.
#
# Uso (variables de entorno, no posicionales — son muchas para una lista ordenada):
#   RUC=20123456789 RAZON_SOCIAL="EMPRESA DOS S.A.C." USUARIO_SOL=FACTURA2 \
#   CLAVE_SOL=miClaveSOL PUERTO=9001 RUTA_CERT_ORIGEN=/home/ubuntu/subidas/cert.p12 \
#   CLAVE_CERT=miClaveCert UBIGEO=150101 DIRECCION="AV. EJEMPLO 123" \
#   DEPARTAMENTO=LIMA PROVINCIA=LIMA DISTRITO=LIMA URBANIZACION="-" \
#   [NOMBRE_COMERCIAL="EMPRESA DOS"] [CARPETA=empresa2] \
#   ./deploy/alta_empresa.sh
set -euo pipefail

# --- Validaciones básicas: fallar temprano y claro, no a mitad del proceso ---
for var in RUC RAZON_SOCIAL USUARIO_SOL CLAVE_SOL PUERTO RUTA_CERT_ORIGEN CLAVE_CERT \
           UBIGEO DIRECCION DEPARTAMENTO PROVINCIA DISTRITO URBANIZACION; do
    if [ -z "${!var:-}" ]; then
        echo "Error: falta la variable de entorno ${var}. Ver el comentario de 'Uso' al inicio del script." >&2
        exit 1
    fi
done
NOMBRE_COMERCIAL="${NOMBRE_COMERCIAL:-$RAZON_SOCIAL}"
CARPETA="${CARPETA:-empresa-${RUC}}"

if ! [[ "$RUC" =~ ^[0-9]{11}$ ]]; then
    echo "Error: el RUC debe tener 11 dígitos (recibido: '$RUC')." >&2
    exit 1
fi
if [ ! -f "$RUTA_CERT_ORIGEN" ]; then
    echo "Error: no existe el archivo de certificado '$RUTA_CERT_ORIGEN'." >&2
    exit 1
fi
for cmd in keytool sqlite3 pm2 python3 curl; do
    command -v "$cmd" >/dev/null || { echo "Error: falta el comando '$cmd' en este servidor." >&2; exit 1; }
done

# --- Rutas de referencia (la PRIMERA empresa ya instalada, como plantilla) ---
SFS_HOME="/home/ubuntu/sfs"
JAR="${SFS_HOME}/facturadorApp-2.1.jar"
VALI_ORIGEN="${SFS_HOME}/sunat_archivos/sfs/VALI"
BD_ORIGEN="${SFS_HOME}/bd/BDFacturador.db"
ESQUEMA_SQL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/esquema_bd.sql"
RUTA_BASE="/home/ubuntu/empresas/${CARPETA}"
# FUERA del repo a propósito — un `git reset --hard` de un deploy no debe poder
# borrar empresas ya dadas de alta. Debe ser la MISMA ruta que usa el
# FACTURADOR_API_EMPRESAS_YAML con el que corre el proceso de PM2 (ver README).
EMPRESAS_YAML="/home/ubuntu/config/empresas.yaml"

if [ -d "$RUTA_BASE" ]; then
    echo "Error: ya existe '$RUTA_BASE' — si es una empresa nueva, usa otro nombre de carpeta." >&2
    exit 1
fi

echo "== 1/9: Creando estructura de carpetas en ${RUTA_BASE} =="
mkdir -p "${RUTA_BASE}"/sunat_archivos/sfs/{DATA,RPTA/procesados,RPTA/errores,CERT,VALI,ALMCERT,ENVIO,FORM,ORIDAT,PARSE,REPO,TEMP,FIRMA}
mkdir -p "${RUTA_BASE}/bd"

echo "== 2/9: Copiando plantillas VALI (genéricas, no tienen nada de otra empresa) =="
cp -r "${VALI_ORIGEN}/." "${RUTA_BASE}/sunat_archivos/sfs/VALI/"

echo "== 3/9: Copiando certificado =="
NOMBRE_CERT="certificado.p12"
RUTA_CERT="${RUTA_BASE}/sunat_archivos/sfs/CERT/${NOMBRE_CERT}"
cp "${RUTA_CERT_ORIGEN}" "${RUTA_CERT}"

echo "== 4/9: Creando BDFacturador.db (esquema limpio + catálogo ERROR copiado) =="
sqlite3 "${RUTA_BASE}/bd/BDFacturador.db" < "${ESQUEMA_SQL}"
sqlite3 "${RUTA_BASE}/bd/BDFacturador.db" \
    "ATTACH DATABASE '${BD_ORIGEN}' AS origen; INSERT INTO ERROR SELECT * FROM origen.ERROR; DETACH DATABASE origen;"

RUTA_JKS="${RUTA_BASE}/sunat_archivos/sfs/ALMCERT/FacturadorKey.jks"

generar_jks() {
    # -noprompt: sin esto, keytool pregunta "overwrite?" la segunda vez (ya hay
    # una entrada de un intento previo) y se queda esperando una respuesta que
    # nunca llega en un script no interactivo. Pasó de verdad dando de alta
    # Spaxion por SSH a mano, sin -noprompt.
    keytool -importkeystore -noprompt \
        -srckeystore "${RUTA_CERT}" -srcstoretype PKCS12 -srcstorepass "${CLAVE_CERT}" \
        -destkeystore "${RUTA_JKS}" -deststoretype JKS -deststorepass 'SuN@TF4CT' \
        -srcalias "${ALIAS_CERT}" -destalias certContribuyente
}

echo "== 5/9: Generando ALMCERT/FacturadorKey.jks (primera pasada) =="
ALIAS_CERT=$(keytool -list -keystore "${RUTA_CERT}" -storetype PKCS12 -storepass "${CLAVE_CERT}" 2>/dev/null \
    | grep -i "PrivateKeyEntry" | cut -d',' -f1)
if [ -z "$ALIAS_CERT" ]; then
    echo "Error: no se encontró una PrivateKeyEntry en el certificado — ¿contraseña incorrecta?" >&2
    exit 1
fi
generar_jks

echo "== 6/9: Generando prod.yaml (puerto ${PUERTO}) y levantando SFS con PM2 =="
# El admin port de Dropwizard (18081 en la instalación original) también debe ser
# único por instancia, si no la segunda empresa no puede ni arrancar (puerto ocupado).
ADMIN_PUERTO=$((PUERTO + 9081))
sed -e "s/port: 9000/port: ${PUERTO}/" -e "s/port: 18081/port: ${ADMIN_PUERTO}/" \
    "${SFS_HOME}/prod.yaml" > "${RUTA_BASE}/prod.yaml"

NOMBRE_PM2="sfs-${RUC}"
pm2 start java --name "${NOMBRE_PM2}" --interpreter none --cwd "${RUTA_BASE}" -- \
    -Xms64m -Xmx512m -XX:MaxMetaspaceSize=256m -XX:+UseSerialGC -jar "${JAR}" server prod.yaml
pm2 save

echo "Esperando a que SFS termine de arrancar..."
# Sondea en vez de un sleep fijo adivinado: un JVM nuevo arrancando compite por
# CPU con las otras instancias ya corriendo (visto en vivo: 100% CPU en dos SFS a
# la vez al dar de alta la segunda empresa, y el servidor llegó a colgarse del
# todo con RAM al 81%+ antes de que PM2 tuviera pm2 startup configurado). Más
# vale esperar lo que haga falta que fallar por apurado.
INTENTOS=0
until curl -s -o /dev/null "http://localhost:${PUERTO}/" || [ "$INTENTOS" -ge 30 ]; do
    sleep 2
    INTENTOS=$((INTENTOS + 1))
done
if [ "$INTENTOS" -ge 30 ]; then
    echo "Advertencia: SFS no respondió tras 60s — puede seguir arrancando; revisa 'pm2 logs ${NOMBRE_PM2}' si lo que sigue falla." >&2
fi

echo "== 7/9: Importando certificado (registra PRKCRT/NOMCERT en PARAMETRO) =="
RESP_CERT=$(curl -s -X POST "http://localhost:${PUERTO}/api/ImportarCertificado.htm" \
    -H "Content-Type: application/json" \
    -d "$(NOMBRE_CERT="$NOMBRE_CERT" CLAVE_CERT="$CLAVE_CERT" python3 -c '
import json, os
print(json.dumps({"nombreCertificado": os.environ["NOMBRE_CERT"], "passPrivateKey": os.environ["CLAVE_CERT"]}))
')")
echo "Respuesta de ImportarCertificado.htm: ${RESP_CERT}"

echo "== 7/9 (continuación): regenerando el .jks por si el import lo corrompió =="
generar_jks

echo "== 8/9: Guardando nombre comercial/dirección (GrabarOtrosParametros.htm) =="
RESP_OTROS=$(curl -s -X POST "http://localhost:${PUERTO}/api/GrabarOtrosParametros.htm" \
    -H "Content-Type: application/json" \
    -d "$(NOMBRE_COMERCIAL="$NOMBRE_COMERCIAL" UBIGEO="$UBIGEO" DIRECCION="$DIRECCION" \
          DEPARTAMENTO="$DEPARTAMENTO" PROVINCIA="$PROVINCIA" DISTRITO="$DISTRITO" URBANIZACION="$URBANIZACION" \
          python3 -c '
import json, os
print(json.dumps({
    "txtNombreComercial": os.environ["NOMBRE_COMERCIAL"],
    "txtUbigeo": os.environ["UBIGEO"],
    "txtDireccion": os.environ["DIRECCION"],
    "txtDepartamento": os.environ["DEPARTAMENTO"],
    "txtProvincia": os.environ["PROVINCIA"],
    "txtDistrito": os.environ["DISTRITO"],
    "txtUrbanizacion": os.environ["URBANIZACION"],
}))
')")
echo "Respuesta de GrabarOtrosParametros.htm: ${RESP_OTROS}"
if ! echo "$RESP_OTROS" | grep -q '"EXITO"'; then
    echo "ADVERTENCIA: la respuesta no dice EXITO — revisa a mano antes de seguir." >&2
fi

echo "== 9/9: Guardando RUC/usuario SOL/ruta de trabajo (GrabarParametro.htm) =="
# Las variables van ANTES de "python3 -c" para que bash las exporte como entorno
# de ese proceso — puestas después del script quedan como argv, no como entorno,
# y os.environ["RUC"] revienta con KeyError. Confirmado a la fuerza dando de
# alta Spaxion: el script se cayó justo acá en una versión anterior.
RESPUESTA=$(curl -s -X POST "http://localhost:${PUERTO}/api/GrabarParametro.htm" \
    -H "Content-Type: application/json" \
    -d "$(RUC="$RUC" USUARIO_SOL="$USUARIO_SOL" CLAVE_SOL="$CLAVE_SOL" RAZON_SOCIAL="$RAZON_SOCIAL" RUTA_BASE="$RUTA_BASE" python3 -c '
import json, os
print(json.dumps({
    "txtNumeroRuc": os.environ["RUC"],
    "txtUsuarioSol": os.environ["USUARIO_SOL"],
    "txtClaveSol": os.environ["CLAVE_SOL"],
    "cmbFuncionamiento": "02",  # "No usar temporizador": facturador-api dispara todo explicito
    "txtRazonSocial": os.environ["RAZON_SOCIAL"],
    "cmbTiempoGenera": "",
    "cmbTiempoEnvia": "",
    "txtRutaSolucion": os.environ["RUTA_BASE"],
    "txtUsuarioSolPrincipal": os.environ["USUARIO_SOL"],
    "txtClaveSolPrincipal": os.environ["CLAVE_SOL"],
}))
')")

echo "Respuesta de GrabarParametro.htm: ${RESPUESTA}"
if ! echo "$RESPUESTA" | grep -q '"EXITO"'; then
    echo "ADVERTENCIA: la respuesta no dice EXITO — revisa a mano antes de seguir." >&2
fi

echo "== Agregando entrada a ${EMPRESAS_YAML} =="
mkdir -p "$(dirname "${EMPRESAS_YAML}")"
RUC="$RUC" RAZON_SOCIAL="$RAZON_SOCIAL" PUERTO="$PUERTO" RUTA_BASE="$RUTA_BASE" \
    USUARIO_SOL="$USUARIO_SOL" CLAVE_SOL="$CLAVE_SOL" \
    EMPRESAS_YAML="${EMPRESAS_YAML}" python3 <<'PYEOF'
import os
import yaml

ruta = os.environ["EMPRESAS_YAML"]
if os.path.exists(ruta):
    with open(ruta, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
else:
    data = {}
data.setdefault("empresas", {})
if os.environ["RUC"] in data["empresas"]:
    raise SystemExit(f"Error: el RUC {os.environ['RUC']} ya está en {ruta} — revísalo a mano.")
data["empresas"][os.environ["RUC"]] = {
    "nombre": os.environ["RAZON_SOCIAL"],
    "puerto_sfs": int(os.environ["PUERTO"]),
    "ruta_base": os.environ["RUTA_BASE"],
    # En texto plano a propósito — ver app/ticket_resumen.py. Solo se usa para
    # resolver el ticket de un resumen diario cuando SUNAT tarda en responder.
    "sol_usuario": os.environ["USUARIO_SOL"],
    "sol_clave": os.environ["CLAVE_SOL"],
}
with open(ruta, "w", encoding="utf-8") as f:
    yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
print(f"Agregado {os.environ['RUC']} a {ruta}")
PYEOF

echo ""
echo "Listo. Empresa ${RUC} (${RAZON_SOCIAL}) dada de alta:"
echo "  - SFS corriendo como '${NOMBRE_PM2}' en el puerto ${PUERTO} (PM2)."
echo "  - Carpeta: ${RUTA_BASE}"
echo "  - facturador-api relee empresas.yaml en cada request — no hace falta reiniciarlo."
echo ""
echo "Prueba con un comprobante de la serie de pruebas antes de usarla en real:"
echo "  curl -X POST https://facturador.peru-hub.com/empresas/${RUC}/comprobantes -H 'Content-Type: application/json' -d '{...}'"
