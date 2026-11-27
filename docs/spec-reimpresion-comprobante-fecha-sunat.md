# Spec: reimprimir un comprobante ya emitido con la fecha que tiene SUNAT

Dirigido a: quien mantiene el sistema que crea e imprime los comprobantes (consumidor de facturador-api) de Andahuasi, Chancayllo y Mala.
Fecha: 2026-11-24. Relacionado: `docs/spec-correccion-fecha-emision-consumidor.md` (causa de fondo).

## 1. Qué se pide

Poder generar de nuevo, **con el formato propio del sistema** (el mismo del ticket/PDF que ya emite), la representación impresa de un comprobante **ya aceptado por SUNAT**, mostrando la **fecha de emisión que SUNAT tiene registrada** en lugar de la fecha del ticket original.

Caso que lo motiva: **Mala, factura F003-015470** (RUC emisor 20605858601).
El cliente la consulta en SUNAT con la fecha impresa en el ticket (10/11/2026) y SUNAT responde que no existe, porque la tiene registrada el 09/11/2026. Ya se verificó que está **aceptada** (CDR código 0).

## 2. Datos de SUNAT para la F003-015470 (fuente de verdad)

| Campo | Valor |
|---|---|
| Tipo / serie / número | 01 / F003 / 015470 |
| Fecha de emisión en SUNAT | **2026-11-09** (el XML dice `19:00:00`, hora que no es real; no imprimirla) |
| Cliente | PROGRAMA SUBSECTORIAL DE IRRIGACIONES-PSI, RUC 20414868216 (tipo doc. 6) |
| Detalle | DIESEL B5 S-50 UV-2, 6.276151 GLL, valor unitario 20.254237, precio unitario 23.90 |
| Op. gravada / IGV / Total | 127.12 / 22.88 / 150.00 PEN |
| Pago | Contado |
| Leyenda | CIENTO CINCUENTA Y 0/100 SOLES |
| Hash (DigestValue) | `fwMf1fTBTGtvid+5mwYyST/CW1Q=` |

El ticket original de la estación debe seguir siendo la fuente para lo que no viaja a SUNAT (placa, kilometraje, surtidor, atendedor, dirección del cliente, etc.).

## 3. Cambio requerido en el consumidor

1. Agregar una acción de **reimpresión/PDF** sobre un comprobante ya emitido que **no vuelva a enviarlo** a facturador-api ni a SUNAT. No genera un comprobante nuevo ni cambia serie/número.
2. Esa reimpresión usa el **mismo formato/plantilla** que el comprobante normal, con estas diferencias:
   - **Fecha de emisión** = la que SUNAT tiene (`2026-11-09` en este caso), en vez de la del ticket.
   - **Código QR** con la misma fecha: `RUC|01|F003|015470|IGV|TOTAL|2026-11-09|6|RUC_CLIENTE|`.
   - Misma serie, número, montos, cliente y hash que el original.
3. La fecha a usar debe leerse del comprobante tal como quedó en SUNAT (campo 2 del `.CAB` del servidor, o la `IssueDate` del XML), no recalcularse.
4. Dejar constancia interna de que es una reimpresión (marca en el sistema, no necesariamente visible en el PDF) para no confundirla con un duplicado.

## 4. Criterios de aceptación

- El PDF de F003-015470 se ve igual que cualquier comprobante del sistema, pero con fecha **09/11/2026**.
- El QR escaneado y la consulta de validez en SUNAT con fecha 09/11/2026, RUC, serie, número y monto 150.00 devuelven "comprobante informado/aceptado".
- No aparece un comprobante nuevo en la bandeja de SFS ni en `facturador-api`; la F003-015470 sigue una sola vez.

## 5. Qué NO se hace

- **No reenviar ni reemitir** la F003-015470: ya está aceptada y SUNAT rechazaría el duplicado.
- No usar la fecha real del ticket (10/11) en el PDF: no coincide con SUNAT y el cliente volvería a recibir "no existe".
- Una nota de crédito + factura nueva con la fecha correcta es una decisión de negocio aparte; no es necesaria para que el cliente valide.

## 6. Cómo verificar los datos en el servidor (solo lectura)

```
ssh -i <pem> ubuntu@18.220.39.219
tar xzOf /home/ubuntu/empresas/mala/archivo_frio/20605858601-01-F003-015470.tar.gz 20605858601-01-F003-015470.CAB
```

La corrección para que los comprobantes **nuevos** salgan con la fecha correcta está en el otro spec; esta reimpresión solo cubre los ya emitidos con fecha corrida.
