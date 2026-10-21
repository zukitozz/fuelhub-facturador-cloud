"""
El hilo de fondo de facturador-api: cada INTERVALO_CICLO_SEG recorre las empresas dadas de
alta y hace, para cada una, las tareas automáticas:
  - app/reintentos.py: reenviar solo los comprobantes que fallaron por un motivo transitorio,
    y resolver los tickets de los resúmenes pendientes;
  - app/resumen_automatico.py: enviar al terminar el día el resumen de boletas.

Una empresa o una tarea que falla no detiene a las demás. Corre dentro del mismo proceso de
uvicorn (ver app/main.py), así que depende de que haya uno solo, como hoy bajo PM2.
"""
import logging
import os
import threading

from . import configuracion, reintentos, resumen_automatico

logger = logging.getLogger(__name__)

INTERVALO_CICLO_SEG = reintentos._entero_de_entorno("FACTURADOR_API_REINTENTOS_CICLO_SEG", 60)


def ciclo() -> None:
    for empresa in configuracion.listar_empresas():
        for tarea in (reintentos.ciclo_empresa, resumen_automatico.ciclo_empresa):
            try:
                tarea(empresa)
            except Exception:
                logger.exception("Falló %s para %s", tarea.__module__, empresa.ruc)


def iniciar() -> threading.Event | None:
    """Arranca el hilo. Devuelve el Event que lo detiene, o None si está apagado
    (FACTURADOR_API_REINTENTOS=0, que apaga reintentos y envío automático de resúmenes)."""
    if os.getenv("FACTURADOR_API_REINTENTOS", "1") == "0":
        logger.info("Tareas automáticas apagadas (FACTURADOR_API_REINTENTOS=0).")
        return None
    parar = threading.Event()

    def bucle():
        while not parar.is_set():
            ciclo()
            parar.wait(INTERVALO_CICLO_SEG)

    threading.Thread(target=bucle, name="ciclo", daemon=True).start()
    logger.info("Reintentos: %s intentos cada %s s por ronda, %s rondas, pausa %s s. "
                "Resumen diario automático a las %s (Lima).",
                reintentos.INTENTOS_POR_RONDA, reintentos.ESPERA_ENTRE_INTENTOS_SEG, reintentos.RONDAS,
                reintentos.PAUSA_ENTRE_RONDAS_SEG, resumen_automatico.HORA_TEXTO)
    return parar
