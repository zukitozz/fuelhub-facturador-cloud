"""
Qué hacer cuando llega un POST de un comprobante que SFS ya tiene en estado fallido.

El problema: SFS no reprocesa un comprobante que ya está en su bandeja con error ('06') o
rechazado ('10'). generar() devuelve esa fila tal cual, y como no está en "XML generado"
('02'), generar_y_enviar() se detiene ahí: el reintento "funcionaba" pero devolvía el
mismo estado viejo, sin enviar nada, aunque el dato ya se hubiera corregido. El daemon
original lo resolvía borrando la fila de DOCUMENTO antes de regenerar (ver
resetear_rechazados en fuelhub-facturador/aplicacion/ciclo_generacion.py); acá se hace lo
mismo, y solo en esos dos estados.

Regla (de lo más a lo menos peligroso):
  - '10' rechazado: SUNAT lo rechazó, no lo registró. Se limpia la fila y se reenvía.
    Salvo que el motivo sea una caída de SUNAT (ver app/errores.py): SFS también lo deja
    en '10' y ahí sí pudo haber llegado, así que se trata como el '06'.
  - '06' error, en factura/nota: pudo haber salido y perderse la respuesta, y reenviar un
    comprobante que SUNAT ya tiene lo duplica (solo se deshace con una nota de crédito).
    Antes de limpiar se le PREGUNTA a SUNAT (consulta integrada):
        aceptado/anulado -> no se reenvía; se responde que ya lo tiene
        no existe        -> se limpia y se reenvía
        cualquier duda   -> no se reenvía (502): ante la duda, nunca a ciegas
  - '06' en boleta: la boleta nunca se envía sola a SUNAT (solo se firma; ver
    sfs_cliente.TIPOS_SIN_ENVIO_INDIVIDUAL), así que un error acá es de firma y no hay nada
    que preguntar: se limpia y se vuelve a generar.
  - Cualquier otro estado (aceptado, enviado, en proceso, pendiente, anulado): no se toca.
    Un comprobante aceptado jamás se reinicia.
"""
import logging
from datetime import datetime

from . import consulta_integrada, consultas, errores, sfs_cliente

logger = logging.getLogger(__name__)

SEGUIR = "seguir"                  # flujo normal (tras limpiar la fila, si hacía falta)
YA_EN_SUNAT = "ya_en_sunat"        # SUNAT ya lo tiene: no reenviar
INDETERMINADO = "indeterminado"    # no se pudo verificar: no reenviar

_ESTADOS_REINICIABLES = ("06", "10")


def preparar(empresa, ruc: str, tipo: str, numero: str,
             fecha_emision: datetime, total: float) -> tuple[str, str]:
    """(accion, detalle). detalle explica YA_EN_SUNAT e INDETERMINADO; vacío en SEGUIR."""
    previo = consultas.consultar_estado(empresa.bd_path, ruc, tipo, numero)
    if previo is None or previo["ind_situ"] not in _ESTADOS_REINICIABLES:
        return SEGUIR, ""

    es_boleta = tipo in sfs_cliente.TIPOS_SIN_ENVIO_INDIVIDUAL
    # Un '10' por una caída de SUNAT (no por contenido) también pudo haber llegado.
    puede_haber_salido = previo["ind_situ"] == "06" or errores.es_transitorio(previo["des_obse"])
    if puede_haber_salido and not es_boleta:
        serie, correlativo = numero.split("-", 1) if "-" in numero else ("0000", numero)
        estado_sunat, detalle = consulta_integrada.consultar_comprobante(
            empresa, ruc, tipo, serie, correlativo, fecha_emision, total)
        if estado_sunat in (consulta_integrada.ACEPTADO, consulta_integrada.ANULADO):
            logger.warning("%s %s-%s: estaba en error pero SUNAT ya lo tiene (%s); no se reenvía.",
                           ruc, tipo, numero, estado_sunat)
            return YA_EN_SUNAT, f"{estado_sunat}: {detalle}"
        if estado_sunat != consulta_integrada.NO_EXISTE:
            logger.warning("%s %s-%s: en error y SUNAT no pudo confirmar (%s: %s); no se reenvía.",
                           ruc, tipo, numero, estado_sunat, detalle)
            return INDETERMINADO, f"{estado_sunat}: {detalle}"

    if consultas.reiniciar_documento(empresa.bd_path, empresa.firma_dir, ruc, tipo, numero):
        logger.info("%s %s-%s: estaba en estado %s; se limpia en SFS para reenviarlo.",
                    ruc, tipo, numero, previo["ind_situ"])
    return SEGUIR, ""
