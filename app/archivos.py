"""
Escritura de los 5 archivos que SFS lee de su carpeta DATA, a partir del payload
que llega por REST — no de una fila de BD, así que no hace falta obtener_items()
ni obtener_receptor() (ver sfs/archivos.py, la versión del daemon): acá el
receptor y los ítems ya vienen completos en el request.

Dos diferencias a propósito respecto de sfs/archivos.py:
  1. Extensiones en MAYÚSCULAS. El jar de SFS las busca así (.CAB/.DET/.TRI/.LEY),
     y en Windows nunca importó porque NTFS no distingue mayúsculas de minúsculas;
     en Linux sí. Confirmado a la fuerza migrando SFS a Lightsail — ver la memoria
     de esa migración. sfs/archivos.py sigue en minúsculas porque hoy solo corre en
     Windows; el día que el daemon también corra en Linux, aplica el mismo cambio.
  2. fecha_emision se usa tal cual llega, sin pasar por fecha_local()/desfase de
     reloj de BD: ese ajuste existía porque el daemon leía fechas en UTC desde SQL
     Server. Acá el caller ya manda la hora local de Lima directamente — es su
     responsabilidad, se documenta en el esquema de entrada.
"""
import os
from datetime import datetime

from dominio.comprobante import _nombre_base, _linea_detalle
from dominio.montos import formatear_decimal, _base_e_igv
from dominio.texto import _campo_pipe

from .esquemas import ComprobanteEntrada

_EXT_CABECERA = {"07": "NOT", "08": "NOT"}
_EXT_CABECERA_POR_DEFECTO = "CAB"
_EXT_DATA = ("CAB", "NOT", "DET", "TRI", "LEY", "PAG")
_TIPOS_SIN_FORMA_PAGO = {"03", "07", "08"}
_TIPOS_NOTA = {"07", "08"}


def _escribir_archivo(ruta: str, contenido: str):
    """Escritura atómica (tmp + rename), igual que utilidades_files.escribir_archivo
    — duplicada acá en vez de importada para no arrastrar config.py (ver __init__.py
    de este paquete: esa importación dispara logging.basicConfig() del daemon)."""
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(contenido)
    os.replace(tmp, ruta)


def _borrar_si_existe(ruta: str):
    """Limpia un archivo de una generación anterior que ya no corresponde (p.ej. un
    .CAB viejo si ahora el mismo número se reenvía como nota) — sin esto, SFS vería
    los dos y tomaría el que no es."""
    try:
        os.remove(ruta)
    except FileNotFoundError:
        pass


def escribir_comprobante(comp: ComprobanteEntrada, ruc_emisor: str, data_dir: str) -> str:
    """
    Escribe los 5 archivos en data_dir. Devuelve el "nombre base"
    (<ruc>-<tipo>-<serie>-<correlativo>) que SFS usa como NOM_ARCH — es justo lo
    que necesitamos para después consultar su estado por sfs_cliente/consultas.
    """
    tipo_comp = comp.tipo_comprobante  # ya normalizado a código por el validator
    num_comp = comp.numeracion_comprobante
    es_nota = tipo_comp in _TIPOS_NOTA

    if es_nota and comp.referencia is None:
        raise ValueError("Una nota de crédito/débito requiere 'referencia'.")

    base = _nombre_base(ruc_emisor, tipo_comp, num_comp)
    os.makedirs(data_dir, exist_ok=True)
    ext_cab = _EXT_CABECERA.get(tipo_comp, _EXT_CABECERA_POR_DEFECTO)
    rutas = {e: os.path.join(data_dir, f"{base}.{e}") for e in _EXT_DATA}

    tipo_doc_rec = _campo_pipe(comp.receptor.tipo_documento, "0")
    num_doc_rec = _campo_pipe(comp.receptor.numero_documento, "00000000")
    razon_social = _campo_pipe(comp.receptor.razon_social, "CLIENTE VARIOS")
    moneda = _campo_pipe(comp.tipo_moneda, "PEN")
    monto_letras = _campo_pipe(comp.monto_letras, "SIN DESCRIPCION")

    fecha_str = comp.fecha_emision.strftime("%Y-%m-%d")
    hora_str = comp.fecha_emision.strftime("%H:%M:%S")

    tot_venta = formatear_decimal(comp.total)
    # Si no mandan gravadas/igv por separado, se asume todo gravado al 18% —
    # mismo criterio que dominio/montos.py:_base_e_igv (ver su docstring: es lo
    # que corresponde salvo que haya ítems exonerados/gratuitos, que necesitarían
    # el tipo de afectación, no soportado todavía acá).
    if comp.gravadas is not None and comp.igv is not None:
        tot_grav = formatear_decimal(comp.gravadas)
        tot_igv = formatear_decimal(comp.igv)
    else:
        base_calc, igv_calc = _base_e_igv(comp.total)
        tot_grav, tot_igv = formatear_decimal(base_calc), formatear_decimal(igv_calc)

    totales = (
        f"{tot_igv:.2f}|{tot_grav:.2f}|{tot_venta:.2f}|"
        f"0.00|0.00|0.00|{tot_venta:.2f}|2.1|2.0|\n"
    )

    if es_nota:
        ref = comp.referencia
        cod_motivo = _campo_pipe(ref.tipo_nota)
        tip_afectado = _campo_pipe(ref.tipo_documento_afectado)
        num_afectado = _campo_pipe(ref.numeracion_documento_afectado)
        des_motivo = _campo_pipe(ref.motivo_documento_afectado, "OTROS CONCEPTOS")
        _escribir_archivo(rutas["NOT"],
            f"0101|{fecha_str}|{hora_str}|0000|{tipo_doc_rec}|{num_doc_rec}|"
            f"{razon_social}|{moneda}|{cod_motivo}|{des_motivo}|{tip_afectado}|{num_afectado}|"
            + totales
        )
        # Un .CAB sobrante haría que SFS tome la nota por una factura/boleta (SFS
        # reconoce el tipo por la extensión de la cabecera, no por el contenido).
        _borrar_si_existe(rutas["CAB"])
    else:
        _escribir_archivo(rutas["CAB"],
            f"0101|{fecha_str}|{hora_str}|-|0000|{tipo_doc_rec}|{num_doc_rec}|"
            f"{razon_social}|{moneda}|" + totales
        )
        _borrar_si_existe(rutas["NOT"])

    if tipo_comp in _TIPOS_SIN_FORMA_PAGO:
        _borrar_si_existe(rutas["PAG"])
    else:
        _escribir_archivo(rutas["PAG"], f"Contado|{tot_venta:.2f}|{moneda}|\n")

    _escribir_archivo(rutas["TRI"], f"1000|IGV|VAT|{tot_grav:.2f}|{tot_igv:.2f}|\n")
    _escribir_archivo(rutas["LEY"], f"1000|{monto_letras}|\n")

    lineas_det = [_linea_detalle(item.model_dump()) for item in comp.items]
    _escribir_archivo(rutas["DET"], "".join(lineas_det))

    return base


def leer_cabecera_boleta(data_dir: str, ruc_emisor: str, numero: str) -> dict | None:
    """
    Relee el .CAB que escribir_comprobante() ya dejó para esta boleta, para
    reconstruir sus datos sin tener una BD propia — los necesita
    app/resumenes.py para armar la línea del .RDI/.TRD. None si el archivo no
    está (boleta ya consumida, o nunca existió).

    Columnas del .CAB de una boleta/factura (ver escribir_comprobante, rama no
    nota): 0101|fecha|hora|-|0000|tipoDocRec|numDocRec|razonSocial|moneda|
    igv|grav|total|0.00|0.00|0.00|total|2.1|2.0|
    """
    base = _nombre_base(ruc_emisor, "03", numero)
    ruta = os.path.join(data_dir, f"{base}.CAB")
    try:
        with open(ruta, encoding="utf-8") as fh:
            campos = fh.readline().rstrip("\n").split("|")
    except FileNotFoundError:
        return None
    if len(campos) < 12:
        return None
    return {
        "numeracion_comprobante": numero,
        "fecha_emision": campos[1],
        "receptor": {"tipo_documento": campos[5], "numero_documento": campos[6]},
        "igv": float(campos[9]),
        "gravadas": float(campos[10]),
        "total": float(campos[11]),
    }


def escribir_resumen(data_dir: str, ruc_emisor: str, numeracion_rc: str, lineas_rdi: list, lineas_trd: list) -> str:
    """Escribe el .RDI/.TRD de un resumen diario. numeracion_rc lleva el prefijo
    completo ("RC-20261003-001"); el nombre de archivo en DATA va sin él —
    validarNombreArchivo() del SFS exige exactamente 4 tramos (ruc-tipo-serie-
    numero), y con el prefijo serían 5 y el SFS lo descarta en silencio."""
    sin_prefijo = numeracion_rc[3:] if numeracion_rc.startswith("RC-") else numeracion_rc
    base = _nombre_base(ruc_emisor, "RC", sin_prefijo)
    os.makedirs(data_dir, exist_ok=True)
    _escribir_archivo(os.path.join(data_dir, f"{base}.RDI"), "".join(lineas_rdi))
    _escribir_archivo(os.path.join(data_dir, f"{base}.TRD"), "".join(lineas_trd))
    return base


def limpiar_boleta(data_dir: str, ruc_emisor: str, numero: str):
    """Borra los archivos de una boleta ya incluida y enviada dentro de un
    resumen — queda 'consumida', igual que _limpiar_data_cerrados() del daemon
    hace con cualquier documento ya cerrado."""
    base = _nombre_base(ruc_emisor, "03", numero)
    for ext in ("CAB", "DET", "TRI", "LEY"):
        _borrar_si_existe(os.path.join(data_dir, f"{base}.{ext}"))
