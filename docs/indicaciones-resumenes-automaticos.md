# Cambios para los sistemas que consumen facturador-api: resumen diario automático

**Para:** equipos de los sistemas que emiten boletas (Chancayllo, Andahuasi, Spaxion, Pachacutec y las que se sumen).
**Desde:** facturador-api, versión con envío automático de resúmenes.

## Qué cambia

Hasta ahora cada sistema debía llamar una vez al día a `POST /empresas/{ruc}/resumenes-diarios`, leer la
respuesta y consultar después el resumen. **Desde ahora la API envía sola el resumen diario de todas las
empresas**, pasada la medianoche de Lima (00:10), y se encarga de resolver el ticket de SUNAT.

Lo que **no** cambia: cómo se emite (`POST /comprobantes`), cómo se consulta un comprobante y el
significado de `estado`. La boleta sigue quedando `generado` y no sale sola a SUNAT.

## Qué tienen que hacer ustedes

### 1. Dejar de disparar el resumen (obligatorio de entender, no urgente)

Pueden **dejar de llamar** a `POST /empresas/{ruc}/resumenes-diarios`. No hace falta apagarlo el mismo día: si lo
siguen llamando, la API no duplica nada (responde `200` con `{"mensaje": "No hay boletas de días anteriores
pendientes de resumir."}`). Retiren el cron/ciclo cuando puedan, y **no lo usen como mecanismo normal**.

> **Atención con el código de estado.** Ese endpoint responde **`200`**, no `201`. Si su ciclo exige `201`
> (como `ciclo_generacion.py:372` en Chancayllo), descarta la respuesta. Al quitar la llamada el problema
> desaparece; si la conservan, acepten `200`.

### 2. Dejar de depender de la respuesta del POST para saber qué boletas entraron

Antes la lista `boletas` venía en la respuesta del POST. Ahora **ya no hay respuesta que leer**. Para saber en qué
resumen quedó cada boleta hay dos consultas nuevas:

**a) Por boleta** (lo más simple): `GET /empresas/{ruc}/comprobantes/03-{numeración}`.

| Situación de la boleta | Respuesta |
|---|---|
| Emitida, todavía sin declarar | `estado: "generado"` (como hoy) |
| Ya declarada en un resumen | `estado` = el del resumen: `enviado` (SUNAT procesa el ticket), `aceptado`, `rechazado`…; `des_obse: "Declarada en el resumen RC-20261019-001: ..."`; `cdr` = el CDR del resumen |
| Anulada por plazo vencido | `estado: "anulado"` y el motivo en `des_obse` |
| No existe para ese RUC | `404` |

Antes, una boleta ya declarada daba `404`. **Ahora responde con el estado de su resumen.** Si su código trataba el
`404` como "ya fue declarada", cámbienlo: ahora un `404` significa que la API nunca la vio.

**b) Por resumen:** `GET /empresas/{ruc}/resumenes?desde=AAAA-MM-DD&limite=100`

```json
{"ruc": "20612016527", "total": 1, "resumenes": [{
  "numeracion_rc": "RC-20261019-001", "codigo": "RC-RC-20261019-001",
  "fecha_referencia": "2026-10-18", "enviado_en": "2026-10-19T00:12:03-05:00",
  "cantidad_boletas": 118, "boletas": ["B003-019178", "..."],
  "estado": "aceptado", "des_obse": "Aceptado (CDR recuperado por ticket)"}]}
```
Úsenlo para conciliar: un barrido diario de los resúmenes de los últimos días y marcar sus boletas.

### 3. Reaccionar a los estados finales

| `estado` de la boleta/resumen | Qué hacer |
|---|---|
| `aceptado` | Marcar la boleta como declarada. Guardar el `cdr` si lo archivan. |
| `enviado` | Esperar: la API consulta el ticket sola cada 2 minutos. Volver a preguntar más tarde. |
| `rechazado` / `error` | **Intervención manual.** La boleta ya salió de la bandeja de SFS; el resumen figura en `GET /empresas/{ruc}/reintentos?estado=manual` con las boletas que llevaba. Decidan si reencolar o anular. |
| `anulado` | La boleta pasó el plazo de 7 días sin declararse; SUNAT ya no la recibe. Tratarla como anulada y, si corresponde, emitir una nueva. |

### 4. Boletas con plazo: no dejarlas envejecer

SUNAT recibe una boleta hasta 7 días después de su emisión. La API **anula** las boletas pendientes que lo
superen (para que no arrastren al resumen entero, que lleva una sola fecha de referencia). Si su sistema reenvía o
emite con retraso, emitan dentro de plazo. Las anuladas figuran en `GET /empresas/{ruc}/reintentos?estado=anulado`.

### 5. Seguimiento de lo que quedó para revisar (recomendado)

`GET /empresas/{ruc}/reintentos?estado=manual` lista lo que necesita a una persona: facturas con error definitivo,
resúmenes rechazados o que no se pudieron enviar, comprobantes que agotaron sus reintentos. Incluyan esa consulta en un
panel o alerta diaria. `?estado=anulado` lista lo anulado por plazo.

## Resumen de endpoints

| Endpoint | Para qué | ¿Nuevo? |
|---|---|---|
| `GET /empresas/{ruc}/resumenes` | Resúmenes enviados y las boletas de cada uno | **Nuevo** |
| `GET /empresas/{ruc}/comprobantes/03-{n}` | Estado de una boleta; si ya se declaró, el de su resumen | Cambia el caso "ya declarada" (antes `404`) |
| `GET /empresas/{ruc}/reintentos?estado=` | Seguimiento: `activo`, `manual`, `resuelto`, `anulado` | Nuevo |
| `POST /empresas/{ruc}/resumenes-diarios` | Forzar un resumen a mano | Sigue; ya no es necesario |

## Qué hace la API por su cuenta (no hay que hacer nada)

- Envía el resumen de cada fecha pendiente a las 00:10 (hora de Lima), una vez al día por empresa.
- Si SFS está caído, reintenta cada 10 minutos hasta 5 veces ese día; si no, lo deja en `manual` y lo intenta al día siguiente.
- Resuelve el ticket del resumen con SUNAT y deja el CDR.
- Reenvía solas las facturas que fallan por cortes de SUNAT (5 intentos cada 10 minutos, pausa de un día, hasta 3 días) y, vencido el plazo de 7 días sin que SUNAT las tenga, las anula.

## Para Chancayllo en particular

- El resumen **no falló** en la API: se enviaron y aceptaron los del 17 al 20/10. Lo que falló fue interpretar el `200`.
- No carguen como pendientes los resúmenes viejos. La lista de boletas de cada uno se consulta con `GET /resumenes`.
- Dejen de exigir `201` en el resumen; basta con quitar la llamada.
