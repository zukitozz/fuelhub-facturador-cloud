# Especificación: consumir facturador-api

Referencia única para los sistemas que emiten comprobantes electrónicos de SUNAT (factura, boleta, nota de crédito, nota de débito) a través del servicio y consultan su estado. Reemplaza a las guías anteriores por empresa.

- **URL base (producción):** `https://facturador.peru-hub.com` (Swagger en `/docs`)
- **Formato:** JSON sobre HTTPS, UTF-8.
- **`{ruc}`:** el RUC emisor (11 dígitos). Va en la URL y el servicio enruta a la instancia de SFS de esa empresa. Un RUC no dado de alta responde `404`.

## Qué hace el servicio por su cuenta

Desde esta versión el servicio **ya no depende de que quien emite vuelva a llamarlo** para terminar el trabajo:

| Tarea automática | Cuándo | Qué implica para quien consume |
|---|---|---|
| **Resumen diario de boletas** | Todos los días a las 00:10 (hora de Lima), por empresa | **Dejar de llamar a `POST /resumenes-diarios`** y de depender de su respuesta (sección 3) |
| **Cierre del resumen** (ticket de SUNAT) | Cada 2 minutos hasta que SUNAT responda | Ya no hay que consultar el resumen para cerrarlo |
| **Reintento de comprobantes** que fallaron por un corte de SUNAT o de red | 5 intentos cada 10 min, pausa de 1 día, hasta 3 días | No hace falta reenviar a mano esos casos (sección 4) |
| **Anulación por plazo** (7 días) | Si SUNAT no lo recibió en 7 días desde la emisión | El comprobante pasa a `anulado`; hay que emitir uno nuevo (sección 4) |

Si ya tenían integrado el servicio, revisen la sección 3 (cambia dónde se ve la boleta ya declarada) y la 5 (consultas nuevas).

## 0. Reglas que no se pueden pasar por alto

1. **El servicio no tiene autenticación ni CORS todavía.** Hay que llamarlo solo desde el backend, nunca desde el navegador: un navegador no podría (CORS) y, si pudiera, cualquiera con la URL y el RUC emitiría a nombre de la empresa. El mecanismo de acceso (API key en cabecera o restricción por IP) está por definir; esta especificación se actualizará.
2. **Credenciales SOL.** El usuario SOL secundario de la empresa debe tener el perfil de envío de comprobantes electrónicos. Si no, SUNAT rechaza con `Client.0111`.
3. **`fecha_emision` va en hora de Lima**, sin zona horaria ni `Z` (`2026-10-15T10:30:00`). Se usa tal cual llega.
4. **Solo IGV 18% gravado.** No hay soporte para exonerado, inafectado ni gratuito. Si no se mandan `gravadas` ni `igv`, se asume todo gravado al 18%.
5. **El éxito se decide por el campo `estado`, no por el HTTP.** Un comprobante rechazado por SUNAT también responde `201`.
6. **Para reintentar se reenvía el mismo número.** Nunca un número nuevo para reintentar un error, un `502` o un timeout: se salta un correlativo o se duplica.
7. **El `xml` y el `cdr` se guardan sin reformatear.** Si cambia un solo espacio, la firma deja de validar.
8. **Plazo de envío de 7 días.** SUNAT solo recibe un comprobante hasta 7 días después de su emisión. Pasado ese plazo ya no se puede enviar (sección 4). Emitir y reintentar dentro de plazo.

## 1. Emitir: `POST /empresas/{ruc}/comprobantes`

Query opcional: `?incluir=xml,cdr` agrega esos archivos a la respuesta (texto plano). Un valor desconocido da `400`.

### Cuerpo

| Campo | Tipo | Oblig. | Notas |
|---|---|---|---|
| `numeracion_comprobante` | string | sí | `SERIE-CORRELATIVO`, ej. `F002-000001`, `B001-000044` |
| `tipo_comprobante` | string | sí | `FACTURA`, `BOLETA`, `NOTA_CREDITO`, `NOTA_DEBITO` (o `01`, `03`, `07`, `08`) |
| `fecha_emision` | datetime ISO | sí | Hora de Lima |
| `total` | número | sí | Total con IGV |
| `gravadas` | número | no | Base imponible. Si se manda, mandar también `igv` |
| `igv` | número | no | Si se omiten ambos, se calculan al 18% desde `total` |
| `tipo_moneda` | string | no | Por defecto `PEN` |
| `monto_letras` | string | no | Ej. `CIENTO DIECIOCHO CON 00/100 SOLES` |
| `receptor` | objeto | ver abajo | `tipo_documento`, `numero_documento`, `razon_social` |
| `items` | lista | sí | Ver abajo |
| `referencia` | objeto | solo notas | Ver abajo |

**`receptor`** (por defecto `"0"`, `"00000000"`, `"CLIENTE VARIOS"`)
- Factura: `tipo_documento` debe ser `"6"` y `numero_documento` un RUC de 11 dígitos. Si no, responde `422`.
- Boleta: puede ir con los valores por defecto (consumidor final), o con `"1"` y un DNI de 8 dígitos.

**`items[]`**

| Campo | Notas |
|---|---|
| `descripcion` | obligatorio |
| `cantidad` | por defecto 1 |
| `medida` | por defecto `NIU` |
| `codigo_producto` | opcional |
| `valor` | valor unitario **sin** IGV (hasta 6 decimales) |
| `valor_venta` | `valor × cantidad` sin IGV, redondeado a 2 decimales |
| `igv_venta` | IGV de la línea |
| `precio` | precio unitario **con** IGV |

Las sumas de las líneas deben cuadrar con `total`, `gravadas` e `igv`. La API no lo valida y SUNAT rechaza si no cuadran (error `3103`, que **no se arregla reintentando**: hay que corregir el cálculo en el sistema que emite).

**`referencia`** (obligatoria en notas de crédito y de débito; sin ella responde `422`)
- `tipo_nota`: código del catálogo 09 (crédito) o 10 (débito), ej. `01`.
- `tipo_documento_afectado`: `01` o `03`.
- `numeracion_documento_afectado`: ej. `F002-000001`.
- `motivo_documento_afectado`: texto libre y opcional (por defecto `OTROS CONCEPTOS`).

### Ejemplo

```json
POST /empresas/{ruc}/comprobantes?incluir=xml
{
  "numeracion_comprobante": "F002-000010",
  "tipo_comprobante": "FACTURA",
  "fecha_emision": "2026-10-15T10:30:00",
  "total": 118.00, "gravadas": 100.00, "igv": 18.00,
  "monto_letras": "CIENTO DIECIOCHO CON 00/100 SOLES",
  "receptor": {"tipo_documento": "6", "numero_documento": "20123456789", "razon_social": "CLIENTE SAC"},
  "items": [{"descripcion": "SERVICIO", "cantidad": 1, "valor": 100.000000,
             "valor_venta": 100.00, "igv_venta": 18.00, "precio": 118.00}]
}
```

### Respuesta `201`

```json
{"codigo": "01-F002-000010", "estado": "aceptado", "des_obse": "-",
 "hash": "fQpBhx...=", "qr": "{ruc}|01|F002|000010|18.00|118.00|2026-10-15|6|20123456789|fQpBhx...=",
 "codigo_error": null, "descripcion_error": null, "xml": "<?xml ...>", "cdr": null}
```

- `codigo` es `{tipo}-{numeracion}` y sirve para consultar después.
- `hash` y `qr` son `null` hasta que el comprobante se firma. El `qr` (10 campos separados por `|`) se codifica tal cual como código QR en la representación impresa.
- `codigo_error` y `descripcion_error` (código de SUNAT y su significado) solo vienen en estados `rechazado` o `error`.

### Estados

| `estado` | Significado | Qué hacer |
|---|---|---|
| `aceptado` | SUNAT lo aceptó | Listo |
| `aceptado_con_observaciones` | Aceptado con advertencias (ver `des_obse`) | Listo; revisar la observación |
| `enviado` | SUNAT ya lo tiene, pero el CDR aún no se descargó (también cuando un reintento detecta que ya estaba) | Tratarlo como emitido y consultar con `GET` luego |
| `generado` | Firmado y en espera. **Toda boleta queda así** hasta que su resumen la declara | Ver sección 3 |
| `pendiente`, `validando` | En proceso | Consultar con `GET` más tarde |
| `rechazado` | SUNAT lo rechazó (`codigo_error` explica) | Si es por contenido, corregir el dato y reenviar el mismo número. Si es una caída de SUNAT, la API lo reintenta sola (sección 4) |
| `error` | SFS no pudo firmar o generar, o hubo un corte al enviar | Reintentar el mismo número, o esperar el reintento automático |
| `anulado` | Anulado. También lo deja la API cuando venció el plazo de 7 días y SUNAT no lo recibió | Final. Si corresponde, emitir uno nuevo dentro de plazo |

## 2. Consultar: `GET /empresas/{ruc}/comprobantes/{codigo}`

El `codigo` es `{tipo}-{numeracion}`, ej. `01-F002-000010`.

La respuesta trae `codigo`, `estado`, `des_obse`, `fec_gene`, `fec_envi`, `codigo_error`, `descripcion_error`, y siempre `xml` y `cdr` cuando ya existen.

- Da `404` si no existe y `400` si el formato es inválido.
- El `cdr` está disponible solo **7 días** tras la aceptación. Después se archiva en el servidor, así que hay que descargarlo y guardarlo dentro de ese plazo.
- El `xml` no tiene ese límite.
- **Boleta ya declarada:** responde con el estado de su resumen (ver sección 3). Ya no da `404`.

## 3. Boletas y resumen diario

La boleta **nunca se envía sola a SUNAT**: queda `generado` y se informa en un resumen diario (RC). **El resumen lo envía la API sola.**

### Qué hace la API

- Todos los días a las **00:10 (hora de Lima)** arma y envía, por empresa, **un resumen por cada fecha pendiente** (un resumen declara una sola fecha; máximo 200 boletas). Incluye solo boletas de días anteriores; las de hoy esperan al día siguiente.
- Resuelve el ticket de SUNAT solo, cada 2 minutos, y deja el CDR. No hay que consultar el resumen para cerrarlo.
- Si SFS está caído, reintenta cada 10 minutos hasta 5 veces ese día; si no, lo deja para intervención manual (sección 5) y vuelve a intentarlo al día siguiente.
- **Anula** las boletas pendientes que ya superaron el plazo de 7 días (si no, SUNAT rechazaría el resumen entero por la fecha vencida).

### Qué hace quien emite

1. El `POST` de una boleta devuelve `generado` con `hash` y `qr`: ya se puede imprimir o entregar al cliente.
2. **Pedir `?incluir=xml` en ese mismo POST.** Es la única oportunidad práctica de guardar el XML sin consultar después.
3. **No hace falta llamar a `POST /empresas/{ruc}/resumenes-diarios`.** Sigue existiendo, para forzar un resumen a mano; llamarlo de más es inofensivo (responde `{"mensaje": "No hay boletas de días anteriores pendientes de resumir."}`).
   > **Ojo:** ese endpoint responde **`200`**, no `201`. Si el ciclo de un sistema exige `201`, descarta la respuesta sin leerla. Lo correcto es quitar esa llamada; si la conservan, deben aceptar `200`.
4. **Saber en qué resumen quedó una boleta** (la respuesta del POST ya no existe, y al declararse la boleta sale de la bandeja de SFS):
   - **Por boleta:** `GET /empresas/{ruc}/comprobantes/03-{numeración}`.

     | Situación de la boleta | Respuesta |
     |---|---|
     | Emitida, todavía sin declarar | `estado: "generado"` |
     | Declarada en un resumen | `estado` = el del resumen (`enviado` mientras SUNAT procesa el ticket, luego `aceptado`, `rechazado`…); `des_obse: "Declarada en el resumen RC-20261019-001: ..."`; `cdr` = el del resumen |
     | Anulada por plazo | `estado: "anulado"` y el motivo en `des_obse` |
     | Nunca la vio la API | `404` |

     **Cambio de comportamiento:** una boleta ya declarada antes daba `404`. Si el código de quien consume lo trataba como "ya fue declarada", debe cambiarse: ahora un `404` significa que la API nunca la recibió.
   - **Por resumen:** `GET /empresas/{ruc}/resumenes?desde=AAAA-MM-DD&limite=100`

     ```json
     {"ruc": "20612016527", "total": 1, "resumenes": [{
       "numeracion_rc": "RC-20261019-001", "codigo": "RC-RC-20261019-001",
       "fecha_referencia": "2026-10-18", "enviado_en": "2026-10-19T00:12:03-05:00",
       "cantidad_boletas": 118, "boletas": ["B003-019178", "..."],
       "estado": "aceptado", "des_obse": "Aceptado (CDR recuperado por ticket)"}]}
     ```
     Sirve para conciliar: un barrido diario de los resúmenes de los últimos días y marcar sus boletas. Los enviados antes de esta versión también figuran.
5. El **CDR de una boleta es el del resumen**; la boleta individual nunca tiene uno propio.
6. El detalle de un resumen también se puede ver con `GET /empresas/{ruc}/comprobantes/RC-{numeracion_rc}` (el código es `RC-` + la `numeracion_rc`, o sea `RC-RC-20261019-001`).

### Cómo reaccionar al estado

| Estado de la boleta / resumen | Qué hacer |
|---|---|
| `aceptado` | Marcar la boleta como declarada. Guardar el `cdr` si lo archivan |
| `enviado` | Esperar: la API consulta el ticket sola. Volver a preguntar más tarde |
| `rechazado` / `error` | **Intervención manual.** El resumen figura en `GET /empresas/{ruc}/reintentos?estado=manual` con las boletas que llevaba. Decidir si reencolar o anular |
| `anulado` | Pasó el plazo de 7 días sin declararse. Tratarla como anulada y emitir una nueva si corresponde |

## 4. Reintentos, plazo e idempotencia

### A pedido: repetir el `POST` con el mismo número

El servicio decide según el estado que ya tiene:

| Estado previo | Resultado |
|---|---|
| aceptado, enviado, anulado | No reenvía y devuelve el estado actual: repetirlo es seguro |
| rechazado por contenido | Limpia y reenvía con el dato corregido |
| rechazado o error por una caída de SUNAT, factura o nota | **Consulta antes a SUNAT.** Si ya lo tiene responde `enviado`; si no, lo reenvía; si no puede confirmar, `502` |
| error (boleta) | La vuelve a generar |

No se reintenta un `rechazado` por contenido sin cambiar nada: se rechaza igual. Se corrige según `codigo_error` y `descripcion_error`.

### Automático: lo que la API reintenta sola

Cada minuto la API revisa los comprobantes en `error` o `rechazado` y los clasifica:

| Tipo de fallo | Ejemplos | Qué hace |
|---|---|---|
| **Transitorio** | SUNAT sin servicio ("servicio de autenticación no disponible", "error en el batch", "no se pudo obtener el ticket"), `Could not send Message`, error al verificar el ticket, timeouts | **5 intentos, uno cada 10 minutos** (una ronda); pausa de **1 día**; nueva ronda; en total **3 rondas**. Agotadas, queda en `manual` con su último estado |
| **Definitivo** | SUNAT rechaza el contenido (`3103` IGV mal calculado, `2800` dato inválido), credenciales, cualquier texto no reconocido | **No se reintenta**: pasa directo a `manual`, porque reenviar lo mismo da lo mismo |

- **Antes de cada reenvío se consulta a SUNAT** si ya tiene el comprobante: si lo tiene, queda `resuelto` y no se envía de nuevo; si no pudo confirmar, no se envía a ciegas.
- Necesita los archivos del comprobante guardados en la API; si faltan, pasa a `manual` y hay que reenviarlo desde el sistema de origen.
- Un `manual` que se corrige y se reenvía bien (con el `POST`) pasa solo a `resuelto`.

### Plazo de 7 días

Si pasaron más de 7 días desde la emisión y el comprobante sigue sin llegar a SUNAT, **la API lo anula** (`estado: "anulado"`, con el motivo en `des_obse`) y deja de reenviarlo. Antes de anular consulta a SUNAT: si SUNAT ya lo tiene, queda `resuelto`; si no pudo confirmar, no anula nada.

La anulación es **solo interna**: no se comunica nada a SUNAT, que nunca recibió el comprobante. Quien lo emitió debe reemplazarlo por uno nuevo dentro de plazo. Lo mismo rige para boletas pendientes (sección 3).

## 5. Seguimiento y soporte

| Endpoint | Para qué |
|---|---|
| `GET /empresas/{ruc}/reintentos?estado=` | Seguimiento de los reintentos automáticos. `estado`: `activo` (se sigue reintentando; trae `ronda`, `intentos_en_la_ronda`, `proximo_intento`), **`manual` (requiere una persona: `motivo` y `ultimo_error` dicen por qué)**, `resuelto`, `anulado`. Códigos con el mismo formato que `GET /comprobantes` (`01-F003-000123`, `RC-RC-...`) |
| `GET /empresas/{ruc}/resumenes` | Resúmenes enviados y las boletas de cada uno (sección 3) |
| `GET /empresas/{ruc}/rechazos?desde=AAAA-MM-DD&origen=sunat&limite=50` | Historial de fallos, lo más reciente primero. Útil para saber qué pasó con un número aunque ya se haya corregido. `origen`: `sunat`, `sfs`, `validacion`, `red`, `resumen`, `consulta`, `reintento`. El máximo de `limite` es 1000 |
| `GET /salud` | `{"estado": "ok"}`, para monitoreo |

**Recomendado:** consultar `GET /empresas/{ruc}/reintentos?estado=manual` una vez al día (o en un panel/alerta): es la lista de lo que necesita a una persona. `?estado=anulado` lista lo anulado por plazo.

## 6. Errores HTTP

| HTTP | Cuándo | Acción |
|---|---|---|
| `201` | `POST` de comprobante procesado | Revisar `estado` |
| `200` | Los `GET` y `POST /resumenes-diarios` | — |
| `400` | `incluir` o `estado` inválido, o `codigo` mal formado | Corregir la llamada |
| `404` | RUC no dado de alta, o comprobante inexistente | Verificar |
| `409` | No se pudo armar el resumen a mano (`detail`) | Leer el detalle |
| `422` | Dato inválido: campo faltante, factura sin RUC, nota sin `referencia` | Corregir y **no reintentar igual** |
| `502` | SFS caído, o no se pudo verificar en SUNAT | Reintentar luego con el **mismo número** (o esperar el reintento automático) |

El formato de error es `{"detail": "texto"}`. En un `422` de campos, `detail` es una lista de `{loc, msg, type}`.

**Timeouts:** el `POST` es síncrono (firma + envío a SUNAT) y puede tardar de 1 a 10 segundos o más. Conviene un timeout de cliente de al menos 60 s. Si se agota, **no asumir que falló**: hacer `GET` del `codigo`. Si da `404`, no llegó y se reenvía el mismo `POST`; si existe, se usa su estado.

## 7. Flujo recomendado

1. Armar el payload y calcular `valor`, `valor_venta`, `igv_venta`, `precio`, `gravadas`, `igv` y `total` de forma consistente.
2. Hacer `POST` con `?incluir=xml` y timeout de 60 s o más.
3. Decidir por `estado`:
   - **Éxito** (`aceptado`, `aceptado_con_observaciones`, `enviado`): guardar `xml`, `hash` y `qr`.
   - **Boleta `generado`**: guardar `xml`, `hash` y `qr`; **no hace falta hacer nada más**, la API la declara a las 00:10.
   - **`pendiente` o `validando`**: consultar con `GET` más tarde.
   - **`rechazado` por contenido**: mostrar `descripcion_error` y corregir el dato.
   - **`error`, `502` o timeout**: reintentar con el mismo número, o esperar el reintento automático.
4. Para facturas y notas: consultar con `GET` hasta tener el `cdr` y guardarlo dentro de los 7 días.
5. Para boletas: conciliar una vez al día con `GET /resumenes` o con el `GET` de cada boleta.
6. Revisar a diario `GET /reintentos?estado=manual`.

## 8. Checklist de integración

- [ ] Llamar solo desde el backend (no desde el navegador).
- [ ] Definir el mecanismo de autenticación con quien administra el servicio.
- [ ] Usuario SOL secundario con perfil de envío (si no, `Client.0111`).
- [ ] Probar primero con un comprobante de prueba de monto bajo antes de usar datos reales.
- [ ] Hora de Lima en `fecha_emision`.
- [ ] Decidir éxito por `estado`, no por HTTP `201`.
- [ ] Guardar `xml` (y `cdr` dentro de 7 días) sin reformatear.
- [ ] Pedir `?incluir=xml` en las boletas.
- [ ] **Quitar la llamada diaria a `POST /resumenes-diarios`** (o aceptar `200`); ya no es necesaria.
- [ ] **Conciliar boletas** con `GET /resumenes` o con el `GET` de cada boleta; dejar de tratar `404` como "ya declarada".
- [ ] Reintentar siempre con el mismo número.
- [ ] Emitir y reenviar dentro del plazo de 7 días.
- [ ] Revisar a diario `GET /reintentos?estado=manual`.

## Pendientes conocidos

- Autenticación: hoy no hay ninguna; el mecanismo está por definir.
- Un comprobante `rechazado` aún no devuelve CDR en el `GET`.
- El estado de un resumen que SUNAT cierra con errores (código `99` del ticket) se muestra como `aceptado`: el veredicto real está en el `cdr` del resumen (`ResponseCode`), que conviene revisar al archivarlo.
