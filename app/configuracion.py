"""
Mapeo RUC -> instancia de SFS de esa empresa. Un archivo YAML a propósito, no una
BD: son cinco filas hoy y unas pocas más el día que se sume una empresa — una BD
para esto sería más infraestructura que datos. Lo edita deploy/alta_empresa.sh.

IMPORTANTE: en producción, FACTURADOR_API_EMPRESAS_YAML apunta FUERA del repo
(ver README — "Dónde vive empresas.yaml en producción"). Es estado vivo del
servidor, no código: si viviera dentro del repo, el próximo `git reset --hard`
de un deploy borraría las empresas dadas de alta después del último commit.
Pasó de verdad dando de alta la segunda empresa — por eso la ruta por defecto de
acá abajo (la del repo) es solo para desarrollo local, nunca para el servidor.
"""
import os
import threading
from dataclasses import dataclass

import yaml

_RAIZ_PROYECTO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # app/ -> raíz del repo
_RUTA_EMPRESAS = os.getenv(
    "FACTURADOR_API_EMPRESAS_YAML",
    os.path.join(_RAIZ_PROYECTO, "empresas.example.yaml"),
)

# Se relee en cada request (ver obtener_empresa): dar de alta una empresa nueva no
# debe exigir reiniciar facturador-api, que para las demás empresas sigue atendiendo
# tráfico real. El lock es solo para no leer el archivo a medio escribir si el alta
# ocurre justo en ese instante.
_lock = threading.Lock()


@dataclass(frozen=True)
class Empresa:
    ruc: str
    nombre: str
    # http://localhost:PUERTO — cada empresa tiene su propia instancia de SFS,
    # nunca se comparte una entre dos RUCs (ver README de este paquete / memoria
    # de la migración: SFS es de un solo RUC por proceso).
    sfs_base_url: str
    # Carpeta raíz de ESA empresa: de acá cuelgan sunat_archivos/sfs/{DATA,RPTA,...}
    # y bd/BDFacturador.db, con la misma estructura que se armó a mano para la
    # primera instalación.
    ruta_base: str

    @property
    def data_dir(self) -> str:
        return os.path.join(self.ruta_base, "sunat_archivos", "sfs", "DATA")

    @property
    def firma_dir(self) -> str:
        """Donde SFS deja el XML ya firmado de cada documento — de ahí sale el
        hash para el QR (ver app/qr.py)."""
        return os.path.join(self.ruta_base, "sunat_archivos", "sfs", "FIRMA")

    @property
    def rpta_dir(self) -> str:
        return os.path.join(self.ruta_base, "sunat_archivos", "sfs", "RPTA")

    @property
    def procesados_dir(self) -> str:
        return os.path.join(self.rpta_dir, "procesados")

    @property
    def bd_path(self) -> str:
        return os.path.join(self.ruta_base, "bd", "BDFacturador.db")


def _cargar() -> dict:
    if not os.path.exists(_RUTA_EMPRESAS):
        return {}
    with _lock:
        with open(_RUTA_EMPRESAS, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    empresas = {}
    for ruc, cfg in (data.get("empresas") or {}).items():
        empresas[str(ruc)] = Empresa(
            ruc=str(ruc),
            nombre=cfg.get("nombre", ""),
            sfs_base_url=f"http://localhost:{cfg['puerto_sfs']}",
            ruta_base=cfg["ruta_base"],
        )
    return empresas


def obtener_empresa(ruc: str) -> Empresa | None:
    """None si el RUC no está dado de alta — el caller responde 404, nunca adivina."""
    return _cargar().get(str(ruc))


def listar_empresas() -> list[Empresa]:
    return list(_cargar().values())
