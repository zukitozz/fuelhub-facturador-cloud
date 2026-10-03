"""
Arma y envía el resumen diario de boletas — ver sfs_cliente.TIPOS_SIN_ENVIO_INDIVIDUAL
para el porqué existe esto: una boleta nunca se envía sola, se agrupa en un RC
(confirmado en el daemon original, aplicacion/ciclo_generacion.py).

Sin base de datos propia: el "quién está pendiente" sale de la propia bandeja de
SFS (ver app/consultas.py — boletas en ind_situ='02', nunca enviadas), y el
detalle de cada boleta (fecha, receptor, montos) se relee de los .CAB que
escribir_comprobante() ya dejó en DATA — es la única copia persistente de esos
datos que facturador-api tiene, dado que no guarda nada de eso en ningún otro
lado. El correlativo del RC también sale de la bandeja de SFS (siguiente_
correlativo_rc), no de un contador propio.

Simplificaciones conscientes respecto del daemon original (ver
estado/resumenes.py de fuelhub-facturador para la versión completa):
  - No hay equivalente de resumenes.json ni de _motivo_para_frenar (cuántas
    veces se declaró cada boleta): acá no hay un ciclo automático que reintente
    solo, así que el riesgo de un bucle de redeclaración es menor — cada
    llamado a este endpoint es una decisión explícita de quien lo invoca. Sí se
    mantiene el freno simple de MAX_RESUMENES_DIA, derivado de la bandeja de
    SFS en vez de un archivo aparte.
  - No hay recuperación de CDR por ticket (recuperar_cdr_resumenes del daemon):
    GET /empresas/{ruc}/comprobantes/RC-{codigo} ya sirve para consultar el
    estado después, pero si SUNAT tarda en resolver el ticket, hay que
    reintentar esa consulta a mano o agregar ese mecanismo más adelante.
"""
from datetime import date, datetime, timedelta, timezone

from dominio.resumen_diario import _linea_rdi, _linea_trd

from . import archivos, consultas, estados, sfs_cliente

# Perú no tiene horario de verano: un offset fijo alcanza, sin depender de qué
# zona horaria tenga configurado el sistema operativo del servidor (suele venir
# en UTC por defecto en una instancia nueva).
_LIMA = timezone(timedelta(hours=-5))

MAX_BOLETAS_RESUMEN = 200
MAX_RESUMENES_DIA = 20


class SinPendientes(Exception):
    """No hay boletas de días anteriores listas para resumir."""


def armar_y_enviar_resumen(empresa, ruc: str) -> dict:
    hoy = datetime.now(_LIMA).date()
    fecha_rc = hoy.strftime("%Y%m%d")

    if consultas.contar_resumenes_hoy(empresa.bd_path, ruc, fecha_rc) >= MAX_RESUMENES_DIA:
        raise RuntimeError(
            f"Ya se armaron {MAX_RESUMENES_DIA} resúmenes hoy para este RUC — "
            "freno de seguridad contra un bucle de reintentos; revisar a mano "
            "antes de forzar otro."
        )

    pendientes = consultas.boletas_pendientes_resumen(empresa.bd_path, ruc)
    candidatas = []
    for numero in pendientes:
        cab = archivos.leer_cabecera_boleta(empresa.data_dir, ruc, numero)
        if cab is None:
            continue  # archivo ya no está; no hay de dónde sacar sus datos
        try:
            fecha_boleta = date.fromisoformat(cab["fecha_emision"])
        except ValueError:
            continue
        # Las de hoy se dejan para el resumen de un día siguiente — recién
        # "cerraron" su día una vez que termina (mismo criterio que
        # aplicacion/lecturas.py:obtener_boletas_para_resumen del daemon original:
        # mandar un resumen a medio día se presta a que lleguen más boletas
        # después y queden fuera).
        if fecha_boleta >= hoy:
            continue
        candidatas.append((fecha_boleta, cab))

    if not candidatas:
        raise SinPendientes()

    # Un resumen declara una sola fecha de referencia (<cbc:ReferenceDate> en
    # ConvertirRBoletasXML.ftl del SFS): se toma la más antigua pendiente, el
    # resto espera al próximo llamado (mismo criterio que
    # sfs/archivos.py:generar_resumen_diario del daemon original).
    dia = min(f for f, _ in candidatas)
    boletas = [c for f, c in candidatas if f == dia][:MAX_BOLETAS_RESUMEN]

    numeracion_rc = f"RC-{fecha_rc}-{consultas.siguiente_correlativo_rc(empresa.bd_path, ruc, fecha_rc):03d}"
    fecha_resumen_str = hoy.strftime("%Y-%m-%d")

    lineas_rdi, lineas_trd = [], []
    for i, boleta in enumerate(boletas, start=1):
        lineas_rdi.append(_linea_rdi(boleta["fecha_emision"], fecha_resumen_str, boleta, boleta["receptor"]))
        lineas_trd.append(_linea_trd(i, boleta))
    archivos.escribir_resumen(empresa.data_dir, ruc, numeracion_rc, lineas_rdi, lineas_trd)

    fila = sfs_cliente.generar_y_enviar(empresa.sfs_base_url, ruc, "RC", numeracion_rc)

    # Una vez que el RC quedó firmado (ind_situ pasó de '01'/'06'), las boletas ya
    # están declaradas DENTRO de su XML — se consumen sin importar si SUNAT ya
    # confirmó el envío o sigue resolviendo el ticket (el resumen es asíncrono,
    # ver sfs_cliente.generar_y_enviar), porque reintentarlas sueltas después
    # sería declararlas dos veces.
    if fila.get("ind_situ") not in ("01", "06"):
        for boleta in boletas:
            numero = boleta["numeracion_comprobante"]
            archivos.limpiar_boleta(empresa.data_dir, ruc, numero)
            consultas.eliminar_documento(empresa.bd_path, ruc, "03", numero)

    return {
        "numeracion_rc": numeracion_rc,
        "cantidad_boletas": len(boletas),
        "estado": estados.legible(fila.get("ind_situ", "")),
        "des_obse": fila.get("des_obse", ""),
    }
