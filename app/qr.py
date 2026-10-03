"""
QR de SUNAT: 10 campos separados por "|" (RUC emisor, tipo, serie, correlativo,
IGV, total, fecha de emisión, tipo y número de documento del receptor, hash).

Los primeros 9 ya los tiene facturador-api del propio request — el único que
hace falta sacar del XML firmado es el hash (el DigestValue de la firma
XML-DSig). Confirmado inspeccionando un XML real generado por esta instalación
de SFS: NO arma el QR solo — el <cbc:Note languageLocaleID="1000"> lo usa para
el monto en letras, no para el QR — así que lo arma facturador-api.
"""
import os
import xml.etree.ElementTree as ET

from dominio.comprobante import _nombre_base

_NS_DS = "{http://www.w3.org/2000/09/xmldsig#}"


def extraer_hash(firma_dir: str, ruc_emisor: str, tipo: str, numero: str) -> str | None:
    """DigestValue del XML firmado que SFS dejó en FIRMA/, o None si el archivo
    no existe todavía o no se pudo leer (p.ej. llamado antes de que SFS termine
    de escribirlo)."""
    base = _nombre_base(ruc_emisor, tipo, numero)
    ruta = os.path.join(firma_dir, f"{base}.xml")
    try:
        root = ET.parse(ruta).getroot()
    except (OSError, ET.ParseError):
        return None
    digest = root.find(f".//{_NS_DS}DigestValue")
    return digest.text if digest is not None else None


def construir(
    ruc_emisor: str, tipo: str, serie: str, correlativo: str,
    igv: float, total: float, fecha_emision: str,
    tipo_doc_receptor: str, num_doc_receptor: str, hash_: str,
) -> str:
    return "|".join([
        ruc_emisor, tipo, serie, correlativo,
        f"{igv:.2f}", f"{total:.2f}", fecha_emision,
        tipo_doc_receptor, num_doc_receptor, hash_,
    ])
