# facturador-api

Servicio REST multiempresa que recibe un comprobante, lo hace firmar y enviar por
la instancia de SFS de la empresa correspondiente, y permite consultar después el
XML de respuesta de SUNAT (el CDR). Sin base de datos propia: el estado de cada
comprobante vive en la SQLite y las carpetas que ya mantiene SFS por su cuenta.

Proyecto **independiente** de [fuelhub-facturador](https://github.com/xt9052/fuelhub-facturador-py)
(el daemon Windows/PM2 de una sola estación) — no comparte proceso, despliegue ni
repositorio con él. La carpeta `dominio/` de acá es una copia a propósito (ver su
`__init__.py`) de los ~170 líneas de formato puro que ambos necesitan; se sincroniza
a mano si algún día cambia.

## Arquitectura en una línea

Una instancia de SFS por empresa (cada una su propio puerto, su propia carpeta, su
propio certificado — SFS es de un solo RUC por proceso). `facturador-api` es el
único proceso compartido: enruta cada request según el RUC de la URL hacia la
instancia de SFS que le corresponde.

```
PM2 en el servidor:
  sfs-empresa1    (java, puerto 9000, cwd /home/ubuntu/sfs)
  sfs-empresa2    (java, puerto 9001, cwd /home/ubuntu/empresas/empresa2)
  facturador-api  (uvicorn, puerto 8000 — el único expuesto)
```

## Endpoints

### `POST /empresas/{ruc}/comprobantes`

Body: ver [app/esquemas.py](app/esquemas.py):`ComprobanteEntrada`. Escribe los 5
archivos en `DATA` de esa empresa y dispara la secuencia completa de SFS (releer
DATA → generar y firmar XML → enviar a SUNAT). Para factura/nota, SUNAT responde
en la misma llamada.

**Excepción: la boleta (`tipo_comprobante: "BOLETA"`) nunca se envía sola.**
Queda generada/firmada y a la espera (`estado: "generado"`) hasta que el resumen
diario la incluya — ver `POST /empresas/{ruc}/resumenes-diarios` más abajo. Esto
replica el comportamiento del daemon original
(`aplicacion/ciclo_generacion.py:332` de fuelhub-facturador: "Las boletas no
entran al loop de arriba... se agrupan en un resumen diario"); enviarla
individual sería una declaración fuera del proceso normal — pasó de verdad una
vez durante la migración, antes de que se corrigiera (ver
`app/sfs_cliente.py:TIPOS_SIN_ENVIO_INDIVIDUAL`).

```bash
curl -X POST http://localhost:8000/empresas/20609785269/comprobantes \
  -H "Content-Type: application/json" \
  -d '{
    "numeracion_comprobante": "F002-000002",
    "tipo_comprobante": "FACTURA",
    "fecha_emision": "2026-10-02T10:00:00",
    "total": 1.00,
    "monto_letras": "UN CON 00/100 SOLES",
    "receptor": {"tipo_documento": "6", "numero_documento": "20123456789", "razon_social": "CLIENTE DE PRUEBA"},
    "items": [
      {"descripcion": "ITEM DE PRUEBA", "cantidad": 1,
       "valor": 0.847458, "valor_venta": 0.85, "igv_venta": 0.15, "precio": 1.00}
    ]
  }'
# -> {
#      "codigo": "01-F002-000002",
#      "estado": "aceptado",
#      "des_obse": "-",
#      "hash": "fQpBhxCjB5Y3pj/1xZIpjYkN1dM=",
#      "qr": "20609785269|01|F002|000002|0.15|1.00|2026-10-02|6|20123456789|fQpBhxCjB5Y3pj/1xZIpjYkN1dM="
#    }
```

`estado` traduce el código interno de SFS a un vocabulario simple — ver
[app/estados.py](app/estados.py) para el mapeo completo (`pendiente`,
`generado`, `aceptado`, `aceptado_con_observaciones`, `rechazado`, `error`,
`anulado`, `validando`, `enviado`). `hash`/`qr` solo se completan una vez que
SFS firmó el documento (`estado` ya no es `pendiente`/`error`) — el `hash` es
el `DigestValue` de la firma XML-DSig, extraído del XML real que SFS dejó en
`FIRMA/`; `qr` son los 10 campos del QR oficial de SUNAT ya armados y
separados por `|`, listos para codificar (ver [app/qr.py](app/qr.py) — SFS no
arma ese string solo, confirmado inspeccionando un XML real).

### `GET /empresas/{ruc}/comprobantes/{codigo}`

`codigo` es `{tipo}-{numeracion}` (el RUC ya va en la URL). Devuelve el estado
actual y, si ya está disponible, el XML del CDR como texto plano.

```bash
curl http://localhost:8000/empresas/20609785269/comprobantes/01-F002-000002
```

### `POST /empresas/{ruc}/resumenes-diarios`

Arma y envía el resumen diario (tipo `RC`) con las boletas de **días anteriores**
que quedaron generadas y a la espera (las de hoy se dejan para el próximo
llamado — un resumen a medio día se presta a que lleguen más boletas después y
queden fuera, mismo criterio que el daemon original). Pensado para dispararse
una vez al día (cron externo, o a mano); llamarlo de más no hace daño, sin
boletas pendientes solo responde que no hay nada que resumir.

```bash
curl -X POST http://localhost:8000/empresas/20609785269/resumenes-diarios
# -> {"numeracion_rc": "RC-20261003-001", "cantidad_boletas": 7, "estado": "aceptado", "des_obse": "-"}
# o, sin pendientes:
# -> {"mensaje": "No hay boletas de días anteriores pendientes de resumir."}
```

Sin base de datos propia, igual que el resto: qué boletas están pendientes sale
de la propia bandeja de SFS, y el detalle de cada una (fecha, receptor, montos)
se relee del `.CAB` que ya quedó en `DATA` al generarla — ver
[app/resumenes.py](app/resumenes.py) para las simplificaciones conscientes
respecto del mecanismo completo del daemon original (no hay recuperación de CDR
por ticket todavía; para eso, reconsultar `GET .../comprobantes/RC-{código}` a
mano mientras no exista ese mecanismo).

Documentación interactiva (Swagger) en `http://localhost:8000/docs` una vez
levantado el servicio.

## Correr localmente

```bash
python -m venv .venv && source .venv/bin/activate   # o .venv\Scripts\activate en Windows
pip install -r requirements-dev.txt
pytest pruebas/ -q   # o: python pruebas/test_facturador_api.py
uvicorn app.main:app --reload --port 8000
```

Variable de entorno `FACTURADOR_API_EMPRESAS_YAML` para decirle dónde está el
`empresas.yaml` real. En desarrollo local, sin configurarla, usa
`empresas.example.yaml` del repo (solo de referencia, con una empresa de
ejemplo). **En producción es obligatorio apuntarla fuera del repo** — ver
"Dónde vive empresas.yaml en producción" más abajo.

## Despliegue

Ver [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml): cada push a
`main` corre las pruebas y, si pasan, hace SSH al servidor y corre `git fetch &&
git reset --hard origin/main && pip install -r requirements.txt && pm2 restart
facturador-api`. Requiere los secrets de repo `SSH_HOST`, `SSH_USER` y `SSH_KEY`
(ver sección de configuración abajo).

**Producción actual:** `https://facturador.peru-hub.com` (Caddy gestiona el
certificado TLS solo, vía `/etc/caddy/Caddyfile` en el servidor — no vive en este
repo porque es configuración del servidor, no de la app).

### Configurar el despliegue automático (una sola vez)

1. En el servidor, clonar este repo una vez a mano en la ruta que vaya a usar PM2
   (p.ej. `/home/ubuntu/facturador-api`) y dejarlo corriendo con PM2:
   ```bash
   git clone <url-del-repo> /home/ubuntu/facturador-api
   cd /home/ubuntu/facturador-api
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   export FACTURADOR_API_EMPRESAS_YAML=/home/ubuntu/config/empresas.yaml
   pm2 start .venv/bin/uvicorn --name facturador-api --interpreter none --cwd /home/ubuntu/facturador-api -- app.main:app --host 0.0.0.0 --port 8000
   pm2 save
   ```
   El `export` antes del `pm2 start` es imprescindible — PM2 (modo fork) toma el
   entorno de la shell que lo lanza. `--interpreter none` también lo es: sin eso
   PM2 intenta correr `uvicorn` con Node.js y falla con `SyntaxError: Unexpected
   identifier` (pasó de verdad la primera vez).
2. En GitHub: Settings → Secrets and variables → Actions, agregar:
   - `SSH_HOST`: la IP pública del servidor.
   - `SSH_USER`: `ubuntu`.
   - `SSH_KEY`: el contenido completo del `.pem` (la misma llave que usas para
     conectarte por SSH/SCP hoy).
3. Listo — el próximo push a `main` dispara el despliegue solo.

### Dónde vive `empresas.yaml` en producción

**`/home/ubuntu/config/empresas.yaml` — fuera del repo, a propósito.** El deploy
automático corre `git reset --hard origin/main`, y si `empresas.yaml` viviera
dentro del repo clonado, cada deploy borraría las empresas dadas de alta después
del último commit. Pasó de verdad: se perdió el alta de la segunda empresa
(Spaxion) en el primer deploy después de agregarla. `deploy/alta_empresa.sh` ya
escribe ahí; `empresas.example.yaml` en el repo es solo documentación del formato,
nunca se lee en producción.

## Dar de alta una empresa

Un solo script (`deploy/alta_empresa.sh`), corrido en el servidor:

```bash
cd /home/ubuntu/facturador-api
./deploy/alta_empresa.sh <ruc> "<razon_social>" <usuario_sol> <clave_sol> <puerto> \
    <ruta_al_certificado.p12> <clave_certificado> [nombre_carpeta]
```

Ejemplo:
```bash
./deploy/alta_empresa.sh 20123456789 "EMPRESA DOS S.A.C." FACTURA2 miClaveSOL 9001 \
    /home/ubuntu/subidas/certificado2.p12 miClaveCert empresa2
```

Hace todo de punta a punta: crea las carpetas (`sunat_archivos/sfs/{DATA,RPTA/...,
CERT,VALI,ALMCERT,...}`, `bd/`), copia las plantillas `VALI/` genéricas, arma la
`BDFacturador.db` con el esquema limpio (`deploy/esquema_bd.sql`), genera el
`ALMCERT/FacturadorKey.jks` con `keytool` (ver el comentario al inicio del script:
el import por la pantalla web está roto en Linux, esto lo reemplaza), levanta esa
instancia de SFS con PM2 en el puerto que le des, guarda RUC/usuario
SOL/ruta de trabajo llamando a `GrabarParametro.htm` (la propia app encripta la
clave SOL, el script no la toca en texto plano en ningún lado persistente salvo el
argumento de línea de comandos), y agrega la entrada a `empresas.yaml` —
`facturador-api` la recoge sola en el próximo request, sin reiniciar nada.

Al final te deja un `curl` de ejemplo para probar con una factura de prueba antes
de usarla con datos reales — hazlo siempre primero, como con la primera empresa.

**Puertos:** cada empresa necesita un puerto único (y automáticamente un puerto de
administración de Dropwizard único también, `puerto + 9081`) — lleva la cuenta de
cuáles ya están en uso en `empresas.yaml`.
