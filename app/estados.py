"""
Traduce IND_SITU (el código interno de 2 dígitos de SFS, ver config.py:_NOMBRE_SITU
del daemon original para el catálogo completo) a un vocabulario simple para quien
consume la API — nadie fuera de este proyecto tiene por qué conocer la numeración
interna del vendor para saber si un comprobante quedó bien o mal.
"""
_MAPA = {
    "01": "pendiente",
    "02": "generado",
    "03": "aceptado",
    "04": "aceptado_con_observaciones",
    "05": "anulado",
    "06": "error",
    "07": "validando",
    "08": "enviado",
    "09": "enviado",
    "10": "rechazado",
    "11": "aceptado",
    "12": "aceptado_con_observaciones",
}

# Estados en los que SFS ya firmó el documento — recién ahí existe un XML en
# FIRMA/ del que se puede sacar el hash para el QR (ver app/qr.py). Antes de
# "generado" no hay nada que firmar todavía.
_FIRMADO_O_MAS = {"02", "03", "04", "08", "09", "10", "11", "12"}


def legible(ind_situ: str) -> str:
    return _MAPA.get(ind_situ, "desconocido")


def firmado(ind_situ: str) -> bool:
    return ind_situ in _FIRMADO_O_MAS
