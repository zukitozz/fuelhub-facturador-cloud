# Spec: corregir `fecha_emision` que el consumidor envía a facturador-api

Dirigido a: quien mantiene el sistema que consume facturador-api para Andahuasi, Chancayllo y Mala.
Fecha del hallazgo: 2026-11-12. Estado: pendiente de corregir en el consumidor.

## 1. Resumen

El consumidor envía `fecha_emision` un día antes de la fecha real y con la hora fija `19:00:00`.
SUNAT acepta el comprobante, pero lo registra con esa fecha equivocada. La factura impresa dice
una fecha y SUNAT tiene otra, así que el cliente no la encuentra al validarla.

La causa está en el consumidor. facturador-api no modifica la fecha: la escribe tal cual llega.

## 2. Cómo se manifestó

Reclamos recibidos el 2026-11-12, con comprobantes aceptados por SUNAT pero "no encontrados" al consultarlos:

| Estación | Factura | Fecha impresa en el comprobante | Fecha que tiene SUNAT |
|---|---|---|---|
| Chancayllo (20612016527) | F003-006960 | 09/11/2026 13:52:56 | 2026-11-08 19:00:00 |
| Andahuasi (20612024821) | F002-000234 | 07/11/2026 | 2026-11-06 19:00:00 |
| Andahuasi (20612024821) | F002-000235 | 07/11/2026 | 2026-11-06 19:00:00 |
| Mala (20605858601) | F003-014409 | 01/11/2026 (cargada 15:54) | 2026-10-31 (SUNAT CAR, periodo 202610) |

Las cuatro están en situación 03 (aceptadas) en la bandeja de SFS, con su CDR en `RPTA`.
No hay error de envío: el único defecto es la fecha.

## 3. Evidencia

Se leyeron los `.CAB` en el servidor (`sunat_archivos/sfs/DATA/<ruc>-<tipo>-<numero>.CAB`, campos 2 y 3 = fecha y hora).

- **Todos** los comprobantes de las tres estaciones salen con hora `19:00:00`, sin excepción:
  - Andahuasi: 387 facturas y 204 boletas.
  - Chancayllo: 411 facturas y 157 boletas.
  - Mala: 998 facturas y 193 boletas.
- La fecha es la real menos un día. Ejemplo en Andahuasi, facturas cargadas el 07/11: su `.CAB` dice `2026-11-06|19:00:00`.
- **Spaxion no tiene el problema** (sus comprobantes llevan la hora real, p. ej. `15:23:19`). Sirve de referencia de cómo debe verse.
- La hora real sí existe en el origen: el ticket de F003-006960 imprime 13:52:56, pero a la API le llegó `19:00:00`.

## 4. Diagnóstico

`19:00:00` es medianoche UTC convertida a hora de Lima (UTC−5). El patrón típico:

1. El consumidor tiene solo una **fecha** (sin hora), p. ej. `2026-11-09`.
2. La interpreta como `2026-11-09T00:00:00Z` (UTC).
3. Al formatearla en hora de Lima resulta `2026-11-08T19:00:00`.
4. Envía ese valor a la API.

Por eso se pierde la hora real y la fecha retrocede un día. Los lugares donde suele ocurrir:

- JavaScript: `new Date("2026-11-09")` se interpreta en UTC; luego `toISOString()`, o formateo con zona de Lima.
- Python: `datetime.fromtimestamp`/`astimezone` aplicados a una fecha en UTC, o `date` convertido a `datetime` con `tzinfo=UTC`.
- SQL Server: campo `date` o columna guardada en UTC convertida al leerla.

El consumidor debe ubicar en su propio código dónde se construye `fecha_emision`; este documento no puede
señalar la línea exacta porque ese repositorio no se revisó.

## 5. Lo que exige el contrato de la API

De `docs/especificacion-consumo-api.md` (línea 26 y tabla de campos):

- `fecha_emision` va **en hora de Lima, sin zona horaria ni `Z`**, p. ej. `2026-10-15T10:30:00`.
- Se usa **tal cual llega**: la API no corrige ni convierte nada.
- Debe ser la **fecha y hora reales de emisión** del comprobante, la misma que se imprime en el ticket.

## 6. Cambio requerido en el consumidor

1. Enviar `fecha_emision` como la fecha y hora **locales de Lima** de emisión real, en formato `YYYY-MM-DDTHH:MM:SS`, sin sufijo `Z` ni offset.
2. Tomar esa hora de la misma fuente que imprime el ticket (para F003-006960 sería `2026-11-09T13:52:56`), no reconstruirla desde una fecha sin hora.
3. No aplicar ninguna conversión de zona horaria a ese valor. Si el origen guarda UTC, convertirlo **una sola vez** a hora de Lima (UTC−5, sin horario de verano), no volver a convertirlo.
4. Aplicarlo a **facturas, boletas y notas** por igual.

### Criterios de aceptación

- Un comprobante emitido a las 13:52:56 del 09/11 llega a la API como `2026-11-09T13:52:56`.
- El `.CAB` generado en el servidor muestra esa fecha y hora (campos 2 y 3), no `19:00:00`.
- La fecha del comprobante en la consulta a SUNAT coincide con la impresa en el ticket.
- Un comprobante emitido entre las 19:00 y las 23:59 de Lima conserva su fecha del día (caso límite: es el que más fácilmente se desplaza).
- Un comprobante emitido entre las 00:00 y las 04:59 de Lima también conserva su fecha.

### Cómo verificar en el servidor (solo lectura)

```
ssh -i <pem> ubuntu@18.220.39.219
cut -d'|' -f2,3 /home/ubuntu/empresas/<estacion>/sunat_archivos/sfs/DATA/<ruc>-01-<numero>.CAB
```

Después de corregir, los comprobantes nuevos no deben mostrar `19:00:00` fija.

## 7. Impacto también en boletas

Las boletas usan la misma fecha. facturador-api la toma de ahí para el `.RDI` del resumen diario y para
calcular el plazo de 7 días. Con la fecha corrida un día, el resumen declara una fecha de referencia
anterior a la real y el plazo se cuenta un día antes. SUNAT hoy las acepta, pero el desfase es el mismo.

## 8. Comprobantes ya emitidos

Están aceptados con la fecha equivocada y **este cambio no los corrige**. Regularizarlos (nota de crédito/baja y
reemisión) es una decisión de negocio, a tomar caso por caso. Conviene medir primero cuántos hay desde que empezó el
desfase: la fecha de inicio no se conoce todavía, y se puede obtener revisando los `.CAB` más antiguos con `19:00:00`.

## 9. Fuera de alcance

- facturador-api no se modifica en este cambio.
- Spaxion no requiere cambio.
