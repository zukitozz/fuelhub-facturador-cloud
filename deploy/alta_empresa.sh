#!/bin/bash
# alta_empresa.sh — da de alta una empresa nueva: arma su instancia de SFS desde
# cero (carpetas, certificado, base, configuración) y la agrega a empresas.yaml de
# facturador-api. Corre en el mismo servidor donde ya vive la primera empresa.
#
# Automatiza los 7 pasos manuales que costó descubrir migrando la primera empresa
# (ver memoria de la migración / README de este repo). Dos de esos pasos tenían
# trampas reales que casi obligan a hacerlos a mano, y las resolvimos leyendo el
# bytecode del jar de SFS (decompilado con CFR) en vez de adivinar:
#
#   - El certificado NO se importa llamando a la pantalla web (ese endpoint está
#     roto en Linux: devuelve EXITO pero nunca genera el .jks). Se genera acá
#     directo con `keytool -importkeystore`, usando el alias y la contraseña
#     fijos que el propio código del jar usa para TODAS las empresas
#     (certContribuyente / SuN@TF4CT) — no son la contraseña real del .p12, son
#     constantes hardcodeadas del vendor. Confirmado funcionando end-to-end
#     (firma real contra SUNAT producción) el 2026-10-02.
#   - RUC/usuario SOL/clave SOL/ruta de trabajo SÍ se guardan llamando al
#     endpoint real (POST /api/GrabarParametro.htm) en vez de tocar la tabla
#     PARAMETRO por SQL: la clave SOL se guarda encriptada con un método propio
#     del jar (generarDocumentosService.Encriptar) que no tiene sentido
#     reimplementar — se deja que la propia app la encripte.
#
# Uso:
#   ./alta_empresa.sh <ruc> <razon_social> <usuario_sol> <clave_sol> <puerto> \
#                      <ruta_al_certificado.p12> <clave_certificado> [nombre_carpeta]
#
# Ejemplo:
#   ./alta_empresa.sh 20123456789 "EMPRESA DOS S.A.C." FACTURA2 miClaveSOL 9001 \
#                      /home/ubuntu/subidas/certificado2.p12 miClaveCert empresa2
set -euo pipefail

if [ "$#" -lt 7 ]; then
    echo "Uso: $0 <ruc> <razon_social> <usuario_sol> <clave_sol> <puerto> <cert.p12> <clave_cert> [carpeta]" >&2
    exit 1
fi

RUC="$1"
RAZON_SOCIAL="$2"
USUARIO_SOL="$3"
CLAVE_SOL="$4"
PUERTO="$5"
RUTA_CERT_ORIGEN="$6"
CLAVE_CERT="$7"
CARPETA="${8:-empresa-${RUC}}"

# --- Validaciones básicas: fallar temprano y claro, no a mitad del proceso ---
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
FACTURADOR_API_DIR="/home/ubuntu/facturador-api"
RUTA_BASE="/home/ubuntu/empresas/${CARPETA}"

if [ -d "$RUTA_BASE" ]; then
    echo "Error: ya existe '$RUTA_BASE' — si es una empresa nueva, usa otro nombre de carpeta." >&2
    exit 1
fi

echo "== 1/7: Creando estructura de carpetas en ${RUTA_BASE} =="
mkdir -p "${RUTA_BASE}"/sunat_archivos/sfs/{DATA,RPTA/procesados,RPTA/errores,CERT,VALI,ALMCERT,ENVIO,FORM,ORIDAT,PARSE,REPO,TEMP,FIRMA}
mkdir -p "${RUTA_BASE}/bd"

echo "== 2/7: Copiando plantillas VALI (genéricas, no tienen nada de otra empresa) =="
cp -r "${VALI_ORIGEN}/." "${RUTA_BASE}/sunat_archivos/sfs/VALI/"

echo "== 3/7: Copiando certificado =="
NOMBRE_CERT="certificado.p12"
cp "${RUTA_CERT_ORIGEN}" "${RUTA_BASE}/sunat_archivos/sfs/CERT/${NOMBRE_CERT}"

echo "== 4/7: Creando BDFacturador.db (esquema limpio + catálogo ERROR copiado) =="
sqlite3 "${RUTA_BASE}/bd/BDFacturador.db" < "${ESQUEMA_SQL}"
sqlite3 "${RUTA_BASE}/bd/BDFacturador.db" \
    "ATTACH DATABASE '${BD_ORIGEN}' AS origen; INSERT INTO ERROR SELECT * FROM origen.ERROR; DETACH DATABASE origen;"

echo "== 5/7: Generando ALMCERT/FacturadorKey.jks =="
ALIAS_CERT=$(keytool -list -keystore "${RUTA_BASE}/sunat_archivos/sfs/CERT/${NOMBRE_CERT}" \
    -storetype PKCS12 -storepass "${CLAVE_CERT}" 2>/dev/null \
    | grep -i "PrivateKeyEntry" | cut -d',' -f1)
if [ -z "$ALIAS_CERT" ]; then
    echo "Error: no se encontró una PrivateKeyEntry en el certificado — ¿contraseña incorrecta?" >&2
    exit 1
fi
keytool -importkeystore \
    -srckeystore "${RUTA_BASE}/sunat_archivos/sfs/CERT/${NOMBRE_CERT}" -srcstoretype PKCS12 -srcstorepass "${CLAVE_CERT}" \
    -destkeystore "${RUTA_BASE}/sunat_archivos/sfs/ALMCERT/FacturadorKey.jks" -deststoretype JKS -deststorepass 'SuN@TF4CT' \
    -srcalias "${ALIAS_CERT}" -destalias certContribuyente

echo "== 6/7: Generando prod.yaml (puerto ${PUERTO}) y levantando SFS con PM2 =="
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

echo "== 7/7: Guardando RUC/usuario SOL/ruta de trabajo (GrabarParametro.htm) =="
# Las variables van ANTES de "python3 -c" para que bash las exporte como entorno
# de ese proceso — puestas después del script (como estaban en una version
# anterior de este archivo) quedan como argv, no como entorno, y
# os.environ["RUC"] revienta con KeyError. Confirmado a la fuerza dando de alta
# Spaxion: el script se cayó justo acá.
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

echo "== Agregando entrada a empresas.yaml de facturador-api =="
RUC="$RUC" RAZON_SOCIAL="$RAZON_SOCIAL" PUERTO="$PUERTO" RUTA_BASE="$RUTA_BASE" \
    EMPRESAS_YAML="${FACTURADOR_API_DIR}/empresas.yaml" python3 <<'PYEOF'
import os
import yaml

ruta = os.environ["EMPRESAS_YAML"]
with open(ruta, encoding="utf-8") as f:
    data = yaml.safe_load(f) or {}
data.setdefault("empresas", {})
if os.environ["RUC"] in data["empresas"]:
    raise SystemExit(f"Error: el RUC {os.environ['RUC']} ya está en {ruta} — revísalo a mano.")
data["empresas"][os.environ["RUC"]] = {
    "nombre": os.environ["RAZON_SOCIAL"],
    "puerto_sfs": int(os.environ["PUERTO"]),
    "ruta_base": os.environ["RUTA_BASE"],
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
