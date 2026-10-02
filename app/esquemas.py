"""
Modelos de entrada/salida del API. Los nombres de campo calcan los que ya espera
dominio/comprobante.py (numeracion_comprobante, tipo_comprobante, etc.) a propósito:
así el adaptador de facturador_api/archivos.py pasa el payload casi tal cual, sin
una capa de traducción de nombres que solo agregaría una fuente más de bugs.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from dominio.texto import _tipo_sunat

# Lo que el daemon ya emite (ver config.py: _TIPOS_SFS) menos el resumen diario
# ("RC"), que es un lote de boletas armado por el propio daemon desde su BD, no
# algo que tenga sentido recibir como un único comprobante por REST.
_TIPOS_VALIDOS = {"01", "03", "07", "08"}


class Item(BaseModel):
    descripcion: str
    cantidad: float = 1
    medida: str = "NIU"
    codigo_producto: Optional[str] = None
    # Valor unitario SIN IGV (6 decimales en el .det — ver dominio/comprobante.py:
    # _linea_detalle, el comentario de por qué 6 y no 2).
    valor: float
    valor_venta: float
    igv_venta: float
    precio: float  # unitario CON IGV


class Referencia(BaseModel):
    """Solo para notas de crédito/débito — el documento que la nota afecta."""
    tipo_nota: str
    tipo_documento_afectado: str
    numeracion_documento_afectado: str
    motivo_documento_afectado: Optional[str] = None


class Receptor(BaseModel):
    tipo_documento: str = "0"
    numero_documento: str = "00000000"
    razon_social: str = "CLIENTE VARIOS"


class ComprobanteEntrada(BaseModel):
    numeracion_comprobante: str = Field(..., examples=["F002-000001"])
    tipo_comprobante: str = Field(..., examples=["FACTURA", "01"])
    fecha_emision: datetime
    total: float
    gravadas: Optional[float] = None
    igv: Optional[float] = None
    tipo_moneda: str = "PEN"
    monto_letras: Optional[str] = None
    receptor: Receptor = Receptor()
    items: list[Item]
    referencia: Optional[Referencia] = None  # obligatoria si tipo_comprobante es nota

    @field_validator("tipo_comprobante")
    @classmethod
    def _normalizar_tipo(cls, v: str) -> str:
        codigo = _tipo_sunat(v) or v
        if codigo not in _TIPOS_VALIDOS:
            raise ValueError(
                f"tipo_comprobante {v!r} no reconocido "
                f"(esperado FACTURA/BOLETA/NOTA_CREDITO/NOTA_DEBITO o 01/03/07/08)"
            )
        return codigo


class ComprobanteAceptado(BaseModel):
    codigo: str = Field(..., description="tipo-numeracion, usar en GET /comprobantes/{codigo}")
    ind_situ: str
    des_obse: str


class EstadoComprobante(BaseModel):
    codigo: str
    ind_situ: str
    des_obse: str
    fec_gene: Optional[str] = None
    fec_envi: Optional[str] = None
    cdr_xml: Optional[str] = Field(None, description="XML de la respuesta de SUNAT, en texto plano, si ya está disponible")
