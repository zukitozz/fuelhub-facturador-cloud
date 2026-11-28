"""
Administra api_keys.yaml.

  python -m app.claves nueva --nombre fuelhub-facturador --rucs 20609785269,20100011884
  python -m app.claves nueva --nombre gimnasio --rucs "*"
  python -m app.claves listar

`nueva` imprime la clave UNA sola vez (en el archivo solo queda su hash) y la agrega al
archivo. Para revocar una clave se borra su entrada del yaml; no hace falta reiniciar.
"""
import argparse
import os
import secrets
import sys

import yaml

from . import seguridad


def _leer(ruta: str) -> dict:
    if os.path.exists(ruta):
        with open(ruta, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    return {}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.claves")
    sub = p.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("nueva")
    n.add_argument("--nombre", required=True)
    n.add_argument("--rucs", required=True, help='RUC separados por coma, o "*" para todos')
    sub.add_parser("listar")
    args = p.parse_args(argv)

    ruta = seguridad.ruta_archivo()
    datos = _leer(ruta)
    datos.setdefault("exigir", False)
    datos.setdefault("exigir_rucs", [])
    datos.setdefault("claves", [])

    if args.cmd == "listar":
        print(f"Archivo: {ruta}  (exigir={datos['exigir']}, exigir_rucs={datos['exigir_rucs']})")
        for c in datos["claves"]:
            print(f"- {c.get('nombre')}: {c.get('rucs')}  hash={str(c.get('hash'))[:8]}…")
        return 0

    if any(c.get("nombre") == args.nombre for c in datos["claves"]):
        print(f"Ya existe una clave llamada '{args.nombre}'. Bórrala del archivo antes de crear otra.", file=sys.stderr)
        return 1
    rucs = ["*"] if args.rucs.strip() == "*" else [r.strip() for r in args.rucs.split(",") if r.strip()]
    if not rucs or any(r != "*" and not (r.isdigit() and len(r) == 11) for r in rucs):
        print("Los RUC deben tener 11 dígitos (o usar \"*\").", file=sys.stderr)
        return 1

    clave = "fh_" + secrets.token_urlsafe(32)
    datos["claves"].append({"nombre": args.nombre, "hash": seguridad.hash_de(clave), "rucs": rucs})
    fd = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        yaml.safe_dump(datos, fh, sort_keys=False, allow_unicode=True)
    print(f"Clave para '{args.nombre}' (guárdala ahora, no se vuelve a mostrar):\n{clave}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
