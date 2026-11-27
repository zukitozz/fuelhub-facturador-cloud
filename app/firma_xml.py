"""
Firma XMLDSig (enveloped, RSA-SHA256, c14n 1.0) del XML de una guía, con el certificado
.p12 de la empresa. Es lo que SFS hace por su cuenta con facturas y notas; las guías van
por la API REST de SUNAT sin pasar por SFS (ver app/guias.py), así que la firma se hace acá.

La firma queda dentro de ext:UBLExtensions/ext:UBLExtension/ext:ExtensionContent, con
Id="SignatureSP" (el mismo que referencia cac:Signature, ver dominio/guia_xml.py).

El digest se calcula sobre el documento ANTES de insertar la firma: es justo lo que da la
transformada "enveloped-signature" al verificar (el documento sin su Signature). Por eso
el hueco de ExtensionContent se deja vacío y la firma no cambia los namespaces: ds ya
está declarado en la raíz.
"""
import base64
import hashlib
import os
import re
import subprocess

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12
from lxml import etree

from dominio.guia_xml import ID_FIRMA, NS

_DS = "{%s}" % NS["ds"]
_EXT = "{%s}" % NS["ext"]
_C14N = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"


class CertificadoError(Exception):
    """No se pudo leer el certificado (falta, clave incorrecta, sin llave privada)."""


def _cargar_con_openssl(ruta: str, clave: str):
    """Los .p12 de SUNAT/certificadoras suelen venir con cifrado antiguo (RC2-40) que la
    librería no abre con OpenSSL 3. El binario openssl sí, con -legacy: se le pide el PEM y
    se carga de ahí. La clave pasa por variable de entorno, nunca por la línea de comandos."""
    r = None
    # -legacy existe desde OpenSSL 3; en versiones anteriores ese cifrado ya se abre sin él.
    for extra in (["-legacy"], []):
        try:
            r = subprocess.run(
                ["openssl", "pkcs12", *extra, "-in", ruta, "-nodes", "-passin", "env:CLAVE_P12"],
                capture_output=True, env={**os.environ, "CLAVE_P12": clave or ""}, timeout=20)
        except (OSError, subprocess.SubprocessError) as e:
            raise CertificadoError(f"No se pudo abrir el certificado con openssl: {e}") from e
        if r.returncode == 0:
            break
    if r.returncode != 0:
        raise CertificadoError(f"No se pudo abrir el certificado {os.path.basename(ruta)} (¿clave incorrecta?)")
    texto = r.stdout.decode("utf-8", errors="replace")
    llaves = re.findall(r"-----BEGIN (?:\w+ )?PRIVATE KEY-----.*?-----END (?:\w+ )?PRIVATE KEY-----", texto, re.S)
    certs = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", texto, re.S)
    if not llaves or not certs:
        raise CertificadoError(f"El certificado {os.path.basename(ruta)} no trae llave privada y certificado.")
    llave = serialization.load_pem_private_key(llaves[0].encode(), None)
    publica = llave.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    for pem in certs:  # el de la llave; los demás son la cadena
        cert = x509.load_pem_x509_certificate(pem.encode())
        if cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) == publica:
            return llave, cert
    raise CertificadoError(f"Ningún certificado de {os.path.basename(ruta)} corresponde a su llave privada.")


def cargar_certificado(ruta: str, clave: str):
    """(llave privada, certificado) del .p12. CertificadoError con el motivo si falla."""
    if not os.path.exists(ruta):
        raise CertificadoError(f"No existe el certificado {ruta}.")
    try:
        with open(ruta, "rb") as fh:
            llave, cert, _ = pkcs12.load_key_and_certificates(fh.read(), clave.encode() if clave else None)
    except (ValueError, OSError):
        # Clave incorrecta o cifrado antiguo: la alternativa lo distingue (falla si es la clave).
        return _cargar_con_openssl(ruta, clave)
    if llave is None or cert is None:
        raise CertificadoError(f"El certificado {os.path.basename(ruta)} no trae llave privada y certificado.")
    return llave, cert


def _c14n(nodo) -> bytes:
    return etree.tostring(nodo, method="c14n", exclusive=False, with_comments=False)


def _el(padre, nombre: str, texto=None, **atributos):
    el = etree.SubElement(padre, _DS + nombre)
    if texto is not None:
        el.text = texto
    for k, v in atributos.items():
        el.set(k, v)
    return el


def firmar(xml: bytes, llave, cert) -> bytes:
    """Devuelve el XML con la firma insertada en ExtensionContent."""
    raiz = etree.fromstring(xml)
    contenido = raiz.find(f"{_EXT}UBLExtensions/{_EXT}UBLExtension/{_EXT}ExtensionContent")
    if contenido is None:
        raise ValueError("El XML no trae ext:ExtensionContent donde poner la firma.")

    digest = base64.b64encode(hashlib.sha256(_c14n(raiz)).digest()).decode()

    firma = etree.SubElement(contenido, _DS + "Signature", Id=ID_FIRMA)
    info = _el(firma, "SignedInfo")
    _el(info, "CanonicalizationMethod", Algorithm=_C14N)
    _el(info, "SignatureMethod", Algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256")
    ref = _el(info, "Reference", URI="")
    transformaciones = _el(ref, "Transforms")
    _el(transformaciones, "Transform", Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature")
    _el(ref, "DigestMethod", Algorithm="http://www.w3.org/2001/04/xmlenc#sha256")
    _el(ref, "DigestValue", digest)
    valor = _el(firma, "SignatureValue")
    cert_b64 = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()
    _el(_el(_el(firma, "KeyInfo"), "X509Data"), "X509Certificate", cert_b64)

    # SignedInfo se canoniza ya dentro del documento: hereda los namespaces del contexto,
    # igual que lo hará quien verifique.
    bytes_firmados = _c14n(info)
    firmado = llave.sign(bytes_firmados, padding.PKCS1v15(), hashes.SHA256())
    valor.text = base64.b64encode(firmado).decode()

    return etree.tostring(raiz, xml_declaration=True, encoding="UTF-8", standalone=False)
