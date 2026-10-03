"""
Cliente HTTP del SFS de UNA empresa (su base_url, http://localhost:<puerto_sfs>).
Es la versión parametrizada de sfs/api.py: misma secuencia de llamadas
(ActualizarPantalla -> GenerarComprobante -> enviarXML) que ya usa el daemon, pero
recibiendo el host como argumento en vez de leerlo de SFS_BASE_URL en config.py —
acá hay una instancia de SFS por empresa, no una sola global.

Nota sobre por qué no hace falta tocar la SQLite de SFS para esto (a diferencia de
sfs/bd.py): cada respuesta de SFS ya trae el estado actual del documento en
"listaBandejaFacturador" — lo mismo que una fila de DOCUMENTO, pero sin abrir la
base. Confirmado en las pruebas manuales de la migración (ver memoria):
GenerarComprobante.htm y enviarXML.htm devuelven ind_situ/des_obse/fec_gene/fec_envi
del documento recién procesado.
"""
import json
import time
import urllib.error
import urllib.request

_TIMEOUT_SEG = 30
_ESPERA_XML_SEG = 2  # mismo valor que config.py: tiempo que tarda SFS en generar el XML tras registrarlo


class SfsError(Exception):
    """SFS no respondió, o respondió algo que no se pudo interpretar."""


def _post(base_url: str, path: str, payload: dict) -> dict:
    url = f"{base_url}/{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SEG) as r:
            return json.loads(r.read().decode())
    except urllib.error.URLError as e:
        raise SfsError(f"SFS no disponible en {url}: {e}") from e
    except json.JSONDecodeError as e:
        raise SfsError(f"Respuesta de SFS no es JSON válido ({url}): {e}") from e


def _fila_documento(respuesta: dict, ruc: str, tipo: str, numero: str) -> dict | None:
    for fila in respuesta.get("listaBandejaFacturador") or []:
        if (fila.get("num_ruc"), fila.get("tip_docu"), fila.get("num_docu")) == (ruc, tipo, numero):
            return fila
    return None


def sincronizar_bandeja(base_url: str) -> bool:
    """Fuerza a SFS a releer su carpeta DATA. Ver el docstring largo del equivalente
    en sfs/api.py:sincronizar_bandeja_sfs — resume por qué hace falta este endpoint
    puntual y no otro."""
    r = _post(base_url, "api/ActualizarPantalla.htm", {})
    return r.get("validacion") == "EXITO"


def generar(base_url: str, ruc: str, tipo: str, numero: str) -> dict:
    """
    Sincroniza y genera (firma) el XML, SIN enviarlo a SUNAT. Devuelve la fila de
    DOCUMENTO tal como la reporta SFS (ind_situ, des_obse, fec_gene, ...).

    Es el único paso válido para una BOLETA: ver TIPOS_SIN_ENVIO_INDIVIDUAL más
    abajo para el porqué. El documento queda firmado y a la espera en la bandeja
    de SFS (ind_situ='02') hasta que el resumen diario lo incluya.
    """
    payload = {"num_ruc": ruc, "tip_docu": tipo, "num_docu": numero}

    if not sincronizar_bandeja(base_url):
        raise SfsError("SFS no pudo releer su carpeta DATA (ActualizarPantalla.htm falló).")

    r = _post(base_url, "api/GenerarComprobante.htm", payload)
    fila = _fila_documento(r, ruc, tipo, numero)
    if fila is None:
        raise SfsError(f"SFS no registró el documento {numero} en su bandeja tras generarlo.")

    # Primera pasada: SFS solo registró el archivo (ind_situ sigue '01'), el XML se
    # genera en una segunda pasada interna — igual que documenta sfs/api.py. Un
    # segundo intento después de una espera corta resuelve esto en la práctica.
    if fila.get("ind_situ") == "01":
        time.sleep(_ESPERA_XML_SEG)
        r = _post(base_url, "api/GenerarComprobante.htm", payload)
        fila = _fila_documento(r, ruc, tipo, numero) or fila

    return fila


def generar_y_enviar(base_url: str, ruc: str, tipo: str, numero: str) -> dict:
    """
    generar() + enviar a SUNAT. NO usar para tipo="03" (boleta) — ver
    TIPOS_SIN_ENVIO_INDIVIDUAL: el daemon original (aplicacion/ciclo_generacion.py,
    comentario "Las boletas no entran al loop de arriba... se agrupan acá en un
    resumen diario") nunca envía una boleta suelta, siempre agrupada en el
    resumen del día — es ese resumen (tipo "RC"), no la boleta, el que pasa por
    enviarXML.htm. main.py hace cumplir esto antes de llamar acá.

    Para factura/nota, enviarXML.htm es síncrono: SUNAT responde en la misma
    llamada (confirmado en la migración: ind_situ pasó directo a '11' - CDR
    descargado - en el mismo request).
    """
    fila = generar(base_url, ruc, tipo, numero)
    if fila.get("ind_situ") != "02":
        # No llegó a "XML generado": no tiene sentido pedirle a SFS que envíe algo
        # que no firmó. des_obse ya trae el motivo (p.ej. "Error al firma archivo XML").
        return fila

    payload = {"num_ruc": ruc, "tip_docu": tipo, "num_docu": numero}
    r = _post(base_url, "api/enviarXML.htm", payload)
    return _fila_documento(r, ruc, tipo, numero) or fila


# Tipos que SUNAT/el sistema no declara de forma individual — ver docstring de
# generar_y_enviar. Hoy solo la boleta; el resumen diario (RC) todavía no se
# arma desde facturador-api (pendiente, ver memoria de la migración).
TIPOS_SIN_ENVIO_INDIVIDUAL = {"03"}
