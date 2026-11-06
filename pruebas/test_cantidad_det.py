"""La cantidad del .det conserva sus decimales (hasta 10); solo se rellena a 2 como mínimo.

Caso real: F003-010225, 0.77 de combustible a 24.20 el galón = 0.031818 galones. Redondeada
a 0.03, SUNAT calcula 0.77 / 0.03 = 25.67 contra 24.20 y rechaza con el error 3270.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dominio.comprobante import _linea_detalle
from dominio.montos import formatear_cantidad

# Lo que ya salía no cambia
assert formatear_cantidad(1) == "1.00"
assert formatear_cantidad(2.5) == "2.50"
assert formatear_cantidad("3") == "3.00"
assert formatear_cantidad(None) == "1.00" and formatear_cantidad(0) == "1.00"
assert formatear_cantidad("basura") == "1.00"

# Lo que antes se redondeaba mal
assert formatear_cantidad(0.031818) == "0.031818"
assert formatear_cantidad(0.0318) == "0.0318"
assert formatear_cantidad(0.244) == "0.244"
assert formatear_cantidad(0.718) == "0.718"
assert formatear_cantidad("0.12345678905") == "0.1234567891"   # tope de 10 decimales (ROUND_HALF_UP)
assert formatear_cantidad(12) == "12.00"

# En la línea del .det, segunda columna (después de la unidad de medida)
item = {"descripcion": "GASOHOL", "cantidad": 0.031818, "medida": "GLL",
        "valor": 20.508475, "valor_venta": 0.65, "igv_venta": 0.12, "precio": 24.20}
columnas = _linea_detalle(item).split("|")
assert columnas[0] == "GLL" and columnas[1] == "0.031818", columnas[:3]

# Con cantidad redondeada, SUNAT veía 0.77 / 0.03 = 25.67; con la real cuadra con el precio.
cantidad = float(columnas[1])
assert abs(0.77 / cantidad - 24.20) < 0.01

print("TODO OK")
