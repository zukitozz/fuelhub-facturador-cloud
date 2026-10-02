"""
Servicio REST multiempresa que reemplaza el polling a la BD del daemon por un
flujo de request/response: POST para recibir un comprobante y dispararle la
firma+envío a SFS, GET para consultar el XML de respuesta de SUNAT (el CDR).

Proyecto independiente del daemon de fuelhub-facturador (main.py de ese repo) —
no comparte proceso, base de código ni despliegue con él. Pensado para correr
junto a N instancias de SFS (una por empresa) en la misma máquina. Por eso no
importa config.py/sfs/*.py/utilidades_files.py de aquel repo: son de una sola
empresa (leen SFS_DATA_DIR, SFS_BASE_URL, etc. como constantes globales) y
dispararían su propio logging.basicConfig() apenas se importan. Lo único que se
toma de ahí es dominio/* — copiado (vendorizado) en este mismo repo, ver
dominio/__init__.py para el porqué.
"""
