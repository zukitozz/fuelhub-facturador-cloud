"""
Separa los fallos de un comprobante en dos clases, para decidir si vale la pena
reintentarlo solo (ver app/reintentos.py):

  - TRANSITORIO: el comprobante no tiene nada malo, falló el camino (SUNAT sin servicio,
    corte de red). Reenviarlo más tarde puede funcionar.
  - DEFINITIVO: reenviar lo mismo da el mismo resultado (monto mal calculado, dato
    inválido, credenciales malas). Solo lo arregla una persona.

SFS mezcla las dos clases en el mismo estado ('10' rechazado, '06' error) y solo deja el
motivo en texto libre (DES_OBSE), así que se reconoce por el texto. Lo que no se reconoce
cuenta como definitivo: ante la duda no se reintenta a ciegas.
"""
import unicodedata

# Frases (sin tildes, en minúsculas) vistas en producción o propias de un corte.
_FRASES_TRANSITORIAS = (
    "no esta disponible",
    "no disponible",
    "no puede responder su solicitud",
    "no se pudo obtener el ticket",
    "error en el batch",
    "could not send message",
    "timed out",
    "timeout",
    "connection",
    "conexion",
    "temporalmente",
    "service unavailable",
    "error al verificar estado del ticket",
)


def _normalizar(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return sin_tildes.lower()


def es_transitorio(des_obse: str | None) -> bool:
    texto = _normalizar(des_obse or "")
    return any(frase in texto for frase in _FRASES_TRANSITORIAS)
