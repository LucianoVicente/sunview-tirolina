"""Prueba rápida de la conexión al Google Sheets.

Uso (tras crear config_sheets.json siguiendo CONFIGURAR_CORREO.md):

    .venv\\Scripts\\python.exe probar_conexion_sheets.py 1
    .venv\\Scripts\\python.exe probar_conexion_sheets.py 1 22/02/2026

Primer argumento: el número de ORDEN a buscar.
Segundo (opcional): la fecha dd/mm/aaaa; por defecto hoy.
"""

import datetime
import sys

import clientes


def main():
    if not clientes.configurado():
        print("[ERROR] Falta configuracion. Crea config_sheets.json "
              "(ver CONFIGURAR_CORREO.md).")
        return 1

    orden = sys.argv[1] if len(sys.argv) > 1 else "1"
    if len(sys.argv) > 2:
        d, m, a = sys.argv[2].split("/")
        fecha = datetime.date(int(a), int(m), int(d))
    else:
        fecha = datetime.date.today()

    print(f"Buscando ORDEN {orden} en la fecha {fecha:%d/%m/%Y} "
          f"(pestaña '{clientes._nombre_pestana(fecha)}')...")
    try:
        info = clientes.buscar_cliente(orden, fecha)
    except clientes.ClientesError as exc:
        print(f"[ERROR] {exc}")
        return 1

    print("[OK] Encontrado:")
    print(f"    Email : {info['email']}")
    print(f"    Vídeos: {info['videos']}")
    print(f"    Pestaña/fecha: {info['pestana']} / {info['fecha']:%d/%m/%Y}")
    print(f"    Hoja usada: {info.get('hoja', '(desconocida)')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
