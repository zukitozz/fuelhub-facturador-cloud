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
DATA → generar y firmar XML → enviar a SUNAT). Para factura/boleta/nota, SUNAT
responde en la misma llamada.

```bash
curl -X POST http://localhost:8000/empresas/20609785269/comprobantes \
  -H "Content-Type: application/json" \
  -d '{
    "numeracion_comprobante": "F002-000002",
    "tipo_comprobante": "FACTURA",
    "fecha_emision": "2026-10-02T10:00:00",
    "total": 1.00,
    "monto_letras": "UN CON 00/100 SOLES",
    "receptor": {"razon_social": "CLIENTE DE PRUEBA"},
    "items": [
      {"descripcion": "ITEM DE PRUEBA", "cantidad": 1,
       "valor": 0.847458, "valor_venta": 0.85, "igv_venta": 0.15, "precio": 1.00}
    ]
  }'
# -> {"codigo": "01-F002-000002", "ind_situ": "11", "des_obse": "-"}
```

### `GET /empresas/{ruc}/comprobantes/{codigo}`

`codigo` es `{tipo}-{numeracion}` (el RUC ya va en la URL). Devuelve el estado
actual y, si ya está disponible, el XML del CDR como texto plano.

```bash
curl http://localhost:8000/empresas/20609785269/comprobantes/01-F002-000002
```

Documentación interactiva (Swagger) en `http://localhost:8000/docs` una vez
levantado el servicio.

## Correr localmente

```bash
python -m venv .venv && source .venv/bin/activate   # o .venv\Scripts\activate en Windows
pip install -r requirements-dev.txt
pytest pruebas/ -q   # o: python pruebas/test_facturador_api.py
uvicorn app.main:app --reload --port 8000
```

Variable de entorno opcional: `FACTURADOR_API_EMPRESAS_YAML` para apuntar a un
`empresas.yaml` en otra ruta (por defecto, el de la raíz del repo).

## Despliegue

Ver [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml): cada push a
`main` hace SSH al servidor y corre `deploy/actualizar.sh` (pull + reinstalar
dependencias si cambiaron + `pm2 restart facturador-api`). Requiere los secrets
de repo `SSH_HOST`, `SSH_USER` y `SSH_KEY` (ver sección de configuración abajo).

### Configurar el despliegue automático (una sola vez)

1. En el servidor, clonar este repo una vez a mano en la ruta que vaya a usar PM2
   (p.ej. `/home/ubuntu/facturador-api`) y dejarlo corriendo con PM2:
   ```bash
   git clone <url-del-repo> /home/ubuntu/facturador-api
   cd /home/ubuntu/facturador-api
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   pm2 start .venv/bin/uvicorn --name facturador-api -- app.main:app --host 0.0.0.0 --port 8000
   pm2 save
   ```
2. En GitHub: Settings → Secrets and variables → Actions, agregar:
   - `SSH_HOST`: la IP pública del servidor.
   - `SSH_USER`: `ubuntu`.
   - `SSH_KEY`: el contenido completo del `.pem` (la misma llave que usas para
     conectarte por SSH/SCP hoy).
3. Listo — el próximo push a `main` dispara el despliegue solo.

## Dar de alta una empresa (manual por ahora)

1. Crear la estructura de carpetas bajo una `ruta_base` nueva (igual a la de la
   primera instalación: `sunat_archivos/sfs/{DATA,RPTA/{procesados,errores},CERT,
   VALI,ALMCERT,ENVIO,FORM,ORIDAT,PARSE,REPO,TEMP,FIRMA}`, `bd/`).
2. Copiar `VALI/` completa desde una instalación existente (plantillas FTL,
   `commons/`, reportes `.jasper` — son genéricas, no tienen nada de la empresa).
3. Copiar el `.p12` de esa empresa a `CERT/`.
4. Armar un `BDFacturador.db` con el esquema (`PARAMETRO`/`DOCUMENTO`/`ERROR`) pero
   sin datos de otra empresa — hoy se copia uno existente y se actualiza
   `RUTSOL`/`NUMRUC`/`NOMCERT`/etc. a mano por SQL.
5. Agregar la entrada a `empresas.yaml` (RUC, puerto, ruta_base).
6. Agregar la instancia de SFS al `ecosystem.config.js` de PM2, con su propio
   `prod.yaml` (puerto distinto) y `cwd`.
7. Levantar esa instancia de SFS y, por un túnel SSH a su puerto, importar el
   certificado desde su pantalla web (`Importar Certificado`) — esto genera el
   `ALMCERT/FacturadorKey.jks` interno que SFS necesita para firmar.

**Pendiente:** un script único que automatice los 7 pasos.
