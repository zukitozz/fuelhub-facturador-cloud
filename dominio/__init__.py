"""
Copia vendorizada de dominio/ de fuelhub-facturador (comprobante.py, montos.py,
texto.py, fechas.py) — solo lo que facturador-api necesita para armar el formato
pipe-delimited que SFS lee de su carpeta DATA.

Se copia a propósito en vez de depender del repo fuelhub-facturador (submódulo,
paquete instalable, etc.): facturador-api es un proyecto independiente, y este
formato lo dicta el proveedor de SFS, no la lógica de negocio de ninguno de los
dos proyectos — cambia rarísima vez. Si algún día cambia, se sincroniza a mano
comparando contra dominio/ en fuelhub-facturador (son ~170 líneas en total).

Lógica de negocio pura: formato, validación y cálculo, sin tocar disco ni red ni
base de datos. Nada de acá sabe que existe el SFS, SUNAT o la BD de la app.
"""
