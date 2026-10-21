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
# -> {"numeracion_rc": "RC-20261003-001", "cantidad_boletas": 7,
#     "boletas": ["B001-000044", "B001-000045", "..."], "estado": "aceptado", "des_obse": "-"}
#    "boletas" lista cuáles entraron: si el resumen se rechaza o SUNAT observa
#    alguna, es lo que permite marcar/reencolar las afectadas en el sistema que llama.
# o, sin pendientes:
# -> {"mensaje": "No hay boletas de días anteriores pendientes de resumir."}
```

Sin base de datos propia, igual que el resto: qué boletas están pendientes sale
de la propia bandeja de SFS, y el detalle de cada una (fecha, receptor, montos)
se relee del `.CAB` que ya quedó en `DATA` al generarla — ver
[app/resumenes.py](app/resumenes.py) para las simplificaciones conscientes
respecto del mecanismo completo del daemon original.

Un resumen (`RC`) es asíncrono en SUNAT: la respuesta inmediata es un ticket, no
un CDR. `GET /empresas/{ruc}/comprobantes/{codigo}` (ver más abajo) resuelve ese
ticket solo, consultando a SUNAT directo cada vez que se llama sobre un RC que
todavía no tiene CDR — ver [app/ticket_resumen.py](app/ticket_resumen.py). Esto
requiere `sol_usuario`/`sol_clave` en `empresas.yaml` para esa empresa (ver más
abajo); sin ellos, el resumen sigue consultable igual, solo que esta resolución
automática queda inactiva hasta que SFS lo cierre por su cuenta.

Documentación interactiva (Swagger) en `http://localhost:8000/docs` una vez
levantado el servicio.

## Correr localmente

```bash
python -m venv .venv && source .venv/bin/activate   # o .venv\Scripts\activate en Windows
pip install -r requirements-dev.txt
pytest pruebas/ -q   # o: python pruebas/test_facturador_api.py
uvicorn app.main:app --reload --port 8000
```

Cada empresa en `empresas.yaml` puede llevar opcionalmente `sol_usuario`/
`sol_clave` (el usuario SOL secundario, en texto plano a propósito — ver
[app/ticket_resumen.py](app/ticket_resumen.py)): solo los usa la resolución de
ticket de un resumen diario, nada más de este servicio los toca. `deploy/
alta_empresa.sh` ya los guarda para empresas nuevas; para Sircon/Spaxion (dadas
de alta antes de que existiera este campo) hay que agregarlos a mano en el
`empresas.yaml` del servidor si se quiere esa resolución automática.

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

Los datos van como variables de entorno (no posicionales), y el script se invoca
con `bash` delante porque no conserva el permiso de ejecución al venir de git.
Lo más cómodo es pegarlo en **una sola línea** (los saltos de línea al pegar en la
terminal del navegador rompen el comando); claves entre comillas simples:

```bash
cd /home/ubuntu/facturador-api
RUC=20123456789 RAZON_SOCIAL="EMPRESA DOS S.A.C." USUARIO_SOL=FACTURA2 CLAVE_SOL='miClaveSOL' PUERTO=9001 RUTA_CERT_ORIGEN=/home/ubuntu/subidas/certificado2.p12 CLAVE_CERT='miClaveCert' UBIGEO=150101 DIRECCION="AV. EJEMPLO 123" DEPARTAMENTO=LIMA PROVINCIA=LIMA DISTRITO=LIMA URBANIZACION="-" CARPETA=empresa2 bash ./deploy/alta_empresa.sh
```

Opcionales: `NOMBRE_COMERCIAL` (por defecto la razón social), `CARPETA` (por defecto
`empresa-<ruc>`) y `NOMBRE_PM2`. Con `CARPETA` dada, el proceso de PM2 se llama
`<CARPETA EN MAYÚSCULAS> - <ruc>` (ej. `EMPRESA2 - 20123456789`).

Antes de correrlo: subir el `.p12` al servidor (`scp` con la llave `.pem` de
Lightsail), tener creado el usuario SOL secundario con el perfil de emisión desde
el SFS, y elegir un puerto libre.

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

## XML firmado y CDR en la respuesta

Dos campos, en texto plano (no base64), con el mismo nombre en el `GET` y en el `POST`:

| Campo | Contenido | Cuándo trae valor |
|---|---|---|
| `xml` | el XML UBL firmado: el que se envía a SUNAT (en una boleta, el que se entrega al cliente) | desde que SFS lo firma; antes, `null` |
| `cdr` | la respuesta de SUNAT (`ApplicationResponse`). Antes se llamaba `cdr_xml` | solo en estados aceptados. Un rechazado no lo trae |

| Llamada | Cómo |
|---|---|
| `GET /empresas/{ruc}/comprobantes/{codigo}` | **siempre** adjunta `xml` y `cdr` (los que ya existan) |
| `POST /empresas/{ruc}/comprobantes?incluir=xml,cdr` | los adjunta solo si se piden; un valor desconocido da 400 |

- Son **texto XML exacto**: guardarlos sin reformatear, o la firma deja de validar.
- **Boletas:** `xml` sí, `cdr` nunca (no tienen CDR propio: es el del resumen, que se
  obtiene con `GET` del `RC-…`). Y **hay que pedir el XML antes de que el resumen la
  consuma**, porque entonces su fila sale de la bandeja de SFS y el `GET` da 404:
  conviene pedirlo en el propio `POST` (`?incluir=xml`).
- **Hasta cuándo están:** el `xml` queda en `FIRMA/` y el mantenimiento diario no lo
  toca. El `cdr` sale del `GET` a los 7 días de aceptado (lo archiva
  `deploy/mantenimiento_diario.py`, ver más abajo). Quien archive el CDR debe pedirlo
  dentro de ese plazo.

## Reintentar un comprobante fallido

Repetir el `POST` del **mismo número** es la forma de reintentar. La API decide según el
estado que SFS ya tiene de ese comprobante (código en `app/reenvio.py`):

| Estado previo en SFS | Qué hace el POST |
|---|---|
| ninguno, pendiente o en proceso | flujo normal |
| aceptado, enviado, anulado | **no reenvía nada**; devuelve el estado actual (idempotente) |
| `rechazado` | limpia el comprobante en SFS y lo **reenvía** (SUNAT no lo registró) |
| `error`, factura/nota | **pregunta antes a SUNAT** (consulta integrada): si ya lo tiene, responde `enviado` y no reenvía; si no existe, limpia y reenvía; si no pudo confirmar, **502** y no reenvía |
| `error`, boleta | limpia y vuelve a generar (la boleta nunca sale sola a SUNAT, el error es de firma) |

Sin esta limpieza SFS devolvía la fila vieja sin enviar nada, aunque el dato ya se
hubiera corregido. Un comprobante aceptado nunca se reinicia. La verificación de un
`error` requiere las credenciales de consulta integrada (`consulta_integrada` en
`empresas.yaml`); sin ellas el reintento responde 502 en vez de arriesgar un duplicado.

## Reintento automático de comprobantes fallidos

Además del reintento a pedido (repetir el `POST`), la API reintenta sola los comprobantes
que fallaron por un motivo **transitorio** (código en `app/reintentos.py`). No depende de
quién envió el comprobante: cada minuto mira la bandeja de SFS de cada empresa y toma lo
que esté en `error`/`rechazado`.

| Fallo | Qué hace |
|---|---|
| **Transitorio**: SUNAT sin servicio ("servicio de autenticación no disponible", "error en el batch", "no se pudo obtener el ticket"), `Could not send Message`, timeouts, SFS caído | reintenta: **5 intentos, uno cada 10 min** (una ronda); pausa de **1 día**; otra ronda; pausa; tercera ronda. Agotadas las 3 rondas queda en `manual` con su último estado |
| **Definitivo**: SUNAT rechaza el contenido (p.ej. `3103` IGV mal calculado, `2800`), credenciales, lo que no se reconoce | **no se reintenta**: pasa directo a `manual`, porque reenviar lo mismo da lo mismo |

**Antes de cada reenvío se le pregunta a SUNAT** (consulta integrada) si ya tiene el
comprobante: si lo tiene, se da por resuelto y no se manda de nuevo; si no pudo confirmar,
cuenta como intento fallido y no se envía a ciegas. Un solo envío a la vez por comprobante:
el reintento cede si en ese momento está entrando el `POST` de quien factura.

**Plazo de envío (7 días).** SUNAT solo recibe un comprobante hasta 7 días después de su emisión
(`FACTURADOR_API_PLAZO_ENVIO_DIAS`). Pasado el plazo, a una factura o nota que sigue en
`error`/`rechazado` se le pregunta a SUNAT: si **ya la tiene**, queda `resuelto`; si **no la
tiene**, se **anula** (`IND_SITU='05'` en SFS, con el motivo en `des_obse`, y estado `anulado` en
`/reintentos`) y deja de reenviarse; si SUNAT no pudo confirmar, no se anula nada y se vuelve a
mirar en el siguiente ciclo. La anulación es solo interna: no se comunica nada a SUNAT (que nunca
recibió el comprobante). Quien emitió la factura debe reemplazarla por una nueva dentro de plazo.

**Resúmenes diarios.** El mismo ciclo resuelve los tickets de los resúmenes (`RC`) que SUNAT
recibió y SFS dejó en `08`: consulta el ticket cada 2 minutos (`_ESPERA_RESUMEN_SEG`), deja el
CDR y cierra el resumen. Así nadie tiene que hacer el `GET` del RC para que se confirme (hacerlo
sigue funcionando). Un ticket que SUNAT ya no conoce (127) pasa a `manual`: hay que revisarlo en
el portal. Aparecen en `/reintentos` con código `RC-<numeración>`.

Necesita los archivos del comprobante en `DATA` (de ahí salen la fecha y el total para
preguntar a SUNAT); si no están, pasa a `manual` y hay que reenviarlo desde el sistema de origen.

```bash
curl "http://localhost:8000/empresas/20612016527/reintentos?estado=manual"
```
`estado`: `anulado` (venció el plazo, ver abajo), `activo` (se sigue reintentando; trae `ronda`, `intentos_en_la_ronda` y
`proximo_intento`), `manual` (**requiere intervención**; `motivo` y `ultimo_error` dicen por
qué) o `resuelto` (aceptado, o SUNAT ya lo tenía). Un `manual` que se corrige y se reenvía
bien pasa solo a `resuelto`. Las derivaciones a `manual` también quedan en `/rechazos`
(`origen=reintento`).

**Dónde vive:** SQLite propia, variable `FACTURADOR_API_REINTENTOS_BD` (sin configurar,
`reintentos.db` en la raíz del repo, ignorado por git). Se puede ajustar con
`FACTURADOR_API_REINTENTOS_POR_RONDA` (5), `_ESPERA_SEG` (600), `_PAUSA_SEG` (86400),
`_RONDAS` (3) y `_CICLO_SEG` (60); `FACTURADOR_API_REINTENTOS=0` apaga todo. Corre en un hilo
del mismo proceso (`app/ciclo.py`), así que depende de un solo proceso de uvicorn (como hoy bajo PM2).

## Resumen diario automático de boletas

**La API envía sola el resumen diario de todas las empresas**, una vez al día, pasada la
medianoche de Lima (`FACTURADOR_API_RESUMEN_HORA`, por defecto `00:10`; código en
`app/resumen_automatico.py`). Los sistemas que emiten boletas ya no tienen que llamar a
`POST /empresas/{ruc}/resumenes-diarios`: el endpoint sigue existiendo y es compatible (si alguien
lo llama de más responde "No hay boletas…").

Cada corrida, por empresa: (1) **anula** las boletas pendientes cuyo plazo de 7 días venció (un
resumen declara una sola fecha y SUNAT lo rechazaría entero); (2) arma y envía **un resumen por
cada fecha pendiente**; (3) deja el resumen que SFS dé en error o rechazado en `/reintentos` como
`manual`. Si la corrida falla (SFS caído) se repite cada 10 min, hasta 5 veces ese día, y si no
queda en `manual` y se vuelve a intentar al día siguiente. El ticket de cada resumen lo resuelve
solo el ciclo de reintentos.

**Cómo saber en qué resumen quedó cada boleta** (la API lo recuerda; SFS borra la boleta al declararla):

```bash
curl "http://localhost:8000/empresas/20612016527/resumenes?desde=2026-10-18"
# -> {"ruc": "...", "total": 1, "resumenes": [{"numeracion_rc": "RC-20261019-001", "codigo": "RC-RC-20261019-001",
#      "fecha_referencia": "2026-10-18", "enviado_en": "2026-10-19T00:12:03-05:00", "cantidad_boletas": 118,
#      "boletas": ["B003-019178", "..."], "estado": "aceptado", "des_obse": "Aceptado (CDR recuperado por ticket)"}]}

curl http://localhost:8000/empresas/20612016527/comprobantes/03-B003-019178
# -> la boleta ya declarada responde con el estado de su resumen:
#    {"codigo": "03-B003-019178", "estado": "aceptado", "des_obse": "Declarada en el resumen RC-20261019-001: ...", "cdr": "<...CDR del resumen...>"}
```
`estado` es el del resumen (`enviado` mientras SUNAT procesa el ticket, `aceptado` al cerrarse).
Una boleta **anulada por plazo** responde `anulado` (y figura en `/reintentos?estado=anulado`).
Los resúmenes anteriores al registro se reconstruyen solos desde su `.RDI`.

Ver [docs/especificacion-consumo-api.md](docs/especificacion-consumo-api.md) (sección 3) para
los cambios que hacen los sistemas que consumen la API.

## Rechazos y soporte: dónde mirar

Cuando un comprobante no sale bien, la información está en varios lugares. De lo
más rápido a lo más profundo:

| Dónde | Qué trae | Cómo |
|---|---|---|
| Respuesta del `POST` | `estado` (`rechazado`/`error`), `des_obse`, `codigo_error` | la recibe quien llama |
| `GET /empresas/{ruc}/comprobantes/{codigo}` | estado actual, `des_obse`, `codigo_error`, fechas | HTTP |
| **`GET /empresas/{ruc}/rechazos`** | historial de fallos de esa empresa, lo más reciente primero | HTTP (abajo) |
| Base de SFS (tabla `DOCUMENTO`) | la fuente original: `IND_SITU`, `DES_OBSE` | `sqlite3 <ruta_base>/bd/BDFacturador.db` |
| Tabla `ERROR` de esa misma base | catálogo de códigos de SUNAT y su descripción | misma base |
| Logs de PM2 | cortes de SFS, avisos de rechazo | `pm2 logs facturador-api`, `pm2 logs "<proceso SFS>"` |
| `<ruta_base>/sunat_archivos/sfs/DATA` | los archivos tal como se enviaron | servidor |

```bash
sqlite3 /home/ubuntu/empresas/chancayllo/bd/BDFacturador.db \
  "select TIP_DOCU, NUM_DOCU, IND_SITU, DES_OBSE from DOCUMENTO where IND_SITU in ('06','10') order by FEC_GENE desc limit 20;"
```
`06` = error de SFS (no pudo firmar, corte de red); `10` = rechazado por SUNAT.

### El registro de rechazos (`/empresas/{ruc}/rechazos`)

La base de SFS **sobrescribe**: si un rechazado se corrige y se reenvía bien, el
rechazo desaparece de ahí. Por eso la API deja su propia huella en un archivo
JSON-lines (`rechazos.jsonl`, una línea por evento), que no se pisa. Sirve para
soporte ("¿qué pasó con F002-000150?") y para mejorar ("¿qué error se repite más?").

```bash
curl "http://localhost:8000/empresas/20609785269/rechazos?desde=2026-10-01&origen=sunat&limite=50"
```
Filtros: `desde` (AAAA-MM-DD, hora de Lima), `origen`, `limite` (máx. 1000). Cada
evento trae `fecha`, `codigo` (`tipo-numeración`), `estado`, `codigo_error` (el código
de SUNAT, p.ej. `2800`, ya separado del texto; `null` si el error es de SFS o de red),
`descripcion_error` (qué significa ese código), `des_obse`, `origen` y `detalle`.

`descripcion_error` sale de la tabla `ERROR` que SFS lleva en la base de cada empresa
(~1600 códigos con el texto oficial de SUNAT). Aparece también en el `POST`, en el
`GET` del comprobante y en la respuesta del resumen diario; es `null` si no hay código
o no está en el catálogo. Es una ayuda: nunca hace fallar una respuesta.

| `origen` | Qué es |
|---|---|
| `sunat` | SUNAT rechazó el comprobante |
| `sfs` | SFS no pudo firmarlo o generarlo |
| `validacion` | el payload no pasó las reglas (422): ni llegó a SFS. `detalle` dice qué campo |
| `red` | SFS no disponible (el comprobante pudo o no llegar a SUNAT; ver 502 de la API) |
| `resumen` | un resumen diario fallido; `detalle.boletas` lista cuáles incluía |
| `consulta` | hallado al consultar el estado (no pasó por un POST); se anota una vez |

Los 422 solo se anotan para empresas dadas de alta (con otro RUC no se escribe nada).

**Dónde vive:** variable `FACTURADOR_API_RECHAZOS_LOG`. Sin configurar usa
`rechazos.jsonl` en la raíz del repo, que está en `.gitignore` y sobrevive al
`git reset --hard` del deploy. Para ponerlo fuera del repo, como `empresas.yaml`:
exportarla y `pm2 restart facturador-api --update-env`.

**Pendiente:** el `GET` de un comprobante rechazado no devuelve el CDR (solo lo hace
para los aceptados). Falta comprobar con un rechazo real si SFS deja el CDR de rechazo
en disco; de ser así, se agrega.

## Mantenimiento diario de disco

`deploy/mantenimiento_diario.py` archiva y después borra en serio los archivos
de factura/nota/resumen ya cerrados (`DATA/` + el `.zip` del CDR en `RPTA/`) —
la boleta no necesita esto porque ya se limpia sola al entrar a un resumen (ver
`app/resumenes.py`). Sin este mantenimiento, con volumen alto (del orden de 10
mil comprobantes/mes por empresa) `DATA/` crece sin límite.

Dos pasos, contados en días desde que SUNAT aceptó cada documento:
1. A los **7 días**: empaqueta esos archivos en un `.tar.gz` dentro de
   `<ruta_base>/archivo_frio/` (local, sin subir a ningún lado — quien emite
   los comprobantes ya los guarda en su propia base) y borra los originales.
2. A los **8 días más** en `archivo_frio/` (≈15 días desde el cierre), borra el
   `.tar.gz` definitivamente.

La fila en `DOCUMENTO` de SFS **nunca se borra**: `GET /comprobantes/{codigo}`
sigue devolviendo el estado aunque `cdr` ya dé `null` por no quedar
archivo. Correrlo una vez al día por cron en el servidor:

```bash
# crontab -e (usuario ubuntu)
0 3 * * * cd /home/ubuntu/facturador-api && .venv/bin/python3 deploy/mantenimiento_diario.py >> /home/ubuntu/config/mantenimiento.log 2>&1
```
