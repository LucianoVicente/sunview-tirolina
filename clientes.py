"""Búsqueda de cliente (email) por número de ORDEN en el Google Sheets.

Lee la hoja "VIDEOS SUNVIEW PARK MAYO2026" en SOLO LECTURA mediante una
*cuenta de servicio* de Google (un robot): la hoja sigue privada, se comparte
solo con el email del robot y la clave JSON se queda en el PC. Sin OAuth
interactivo, ideal para el PC de recepción.

Estructura real de la hoja (ver memoria del proyecto):
  * Una pestaña por DÍA DE LA SEMANA: MIERCOLES, JUEVES, ... DOMINGO.
  * Dentro de cada pestaña, bloques por FECHA separados por filas vacías.
    La fecha (col A) solo aparece en la primera fila del bloque; las de abajo
    la heredan (forward-fill).
  * El ORDEN (col B) se reinicia en cada bloque de fecha.
  * Columnas: A=DÍA, B=ORDEN, C=VIDEOS, D=E-MAIL, E=Teléfono, F=Instagram,
    G=Estado del vídeo.

Búsqueda: día de la semana de hoy -> pestaña; fecha de hoy -> bloque; ORDEN -> fila.

Configuración en config_sheets.json (junto a este archivo):
{
    "service_account_json": "ruta/al/robot.json",
    "spreadsheet_id": "ID_de_la_hoja_(de_la_URL)",
    "columnas": {"orden": 1, "videos": 2, "email": 3}
}
(los índices de columna son base 0: A=0, B=1, C=2, D=3...)
"""

import datetime
import json
from pathlib import Path

_BASE = Path(__file__).resolve().parent
CONFIG_PATH = _BASE / "config_sheets.json"

# Día de la semana de Python (Monday=0) -> nombre de pestaña en la hoja.
DIAS_PESTANA = {
    0: "LUNES",
    1: "MARTES",
    2: "MIERCOLES",
    3: "JUEVES",
    4: "VIERNES",
    5: "SABADO",
    6: "DOMINGO",
}

# Índices de columna por defecto (base 0), por si falta en el config.
COLUMNAS_DEFECTO = {"orden": 1, "videos": 2, "email": 3, "nombre": 3}

# Nombre del mes tal y como aparece en el título de la hoja mensual
# ("VIDEOS SUNVIEW PARK MAYO2026", "... JUNIO2026", ...).
MESES_NOMBRE = {
    1: "ENERO", 2: "FEBRERO", 3: "MARZO", 4: "ABRIL", 5: "MAYO", 6: "JUNIO",
    7: "JULIO", 8: "AGOSTO", 9: "SEPTIEMBRE", 10: "OCTUBRE", 11: "NOVIEMBRE",
    12: "DICIEMBRE",
}

# Cache por (año, mes) del ID descubierto vía Drive, para no listar en cada
# búsqueda dentro de la misma sesión de la GUI.
_CACHE_HOJA_MES = {}


class ClientesError(Exception):
    """Error de configuración o de lectura del Sheets, con mensaje para la GUI."""


def _cargar_config():
    if not CONFIG_PATH.exists():
        raise ClientesError(
            "Falta config_sheets.json. Aún no está configurada la conexión con "
            "el Google Sheets (cuenta de servicio + ID de la hoja)."
        )
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise ClientesError(f"No se pudo leer config_sheets.json: {exc}") from exc

    sa = cfg.get("service_account_json")
    if sa:
        # Ruta relativa = relativa a la carpeta de la app, no al cwd. Así el
        # config puede llevar solo el nombre del .json y vale en cualquier PC.
        sa_path = Path(sa)
        if not sa_path.is_absolute():
            sa_path = _BASE / sa_path
        cfg["service_account_json"] = str(sa_path)
    if not sa or not Path(cfg["service_account_json"]).exists():
        raise ClientesError(
            "La clave de la cuenta de servicio (service_account_json) no existe "
            f"en la ruta indicada: {sa!r}"
        )
    # 'spreadsheet_id' ya no es obligatorio: si falta, la app busca la hoja
    # del mes entre las compartidas con el robot (requiere Drive API).
    # Mezclar con los defaults clave a clave: un config con solo {"orden": 1}
    # debe seguir teniendo "email" y "videos" (setdefault no mezcla dicts).
    columnas = dict(COLUMNAS_DEFECTO)
    columnas.update(cfg.get("columnas") or {})
    cfg["columnas"] = columnas
    return cfg


def _email_robot(cfg):
    """Email de la cuenta de servicio, para mostrarlo en los mensajes de error."""
    try:
        with open(cfg["service_account_json"], encoding="utf-8") as f:
            return json.load(f).get("client_email") or "el email del robot"
    except Exception:  # noqa: BLE001
        return "el email del robot"


def _cliente_gspread(cfg):
    """Crea el cliente autorizado de gspread, o lanza ClientesError claro."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as exc:
        raise ClientesError(
            "Faltan dependencias para leer Google Sheets. Instala con:\n"
            "  pip install gspread google-auth"
        ) from exc

    # drive.readonly solo se usa para LISTAR las hojas compartidas con el
    # robot (descubrir la del mes). Si la Drive API no está habilitada, esa
    # parte falla en silencio y se usa el 'spreadsheet_id' del config.
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    try:
        creds = Credentials.from_service_account_file(
            cfg["service_account_json"], scopes=scopes
        )
        return gspread.authorize(creds)
    except Exception as exc:  # noqa: BLE001 — queremos mensaje único para la GUI
        raise ClientesError(
            f"No se pudo autenticar la cuenta de servicio.\nDetalle: {exc}"
        ) from exc


def _descubrir_hoja_mes(cliente, fecha):
    """Busca entre las hojas compartidas con el robot la del mes de `fecha`.

    Cada mes crean un Sheets nuevo ("VIDEOS SUNVIEW PARK JUNIO2026"...); basta
    con que lo compartan con el robot para que esta función lo encuentre por
    el nombre del mes + año en el título. Devuelve el ID o None.
    """
    clave = (fecha.year, fecha.month)
    if clave in _CACHE_HOJA_MES:
        return _CACHE_HOJA_MES[clave]

    mes = MESES_NOMBRE[fecha.month]
    anios = (str(fecha.year), f"{fecha.year % 100:02d}")
    encontrado = None
    try:
        archivos = cliente.list_spreadsheet_files()
    except Exception:  # noqa: BLE001 — Drive API no habilitada / sin permiso
        archivos = []
    for archivo in archivos:
        nombre = _normalizar(archivo.get("name", ""))
        if mes in nombre and any(a in nombre for a in anios):
            encontrado = archivo.get("id")
            break

    _CACHE_HOJA_MES[clave] = encontrado
    return encontrado


def _abrir_hoja(cfg, fecha=None):
    """Devuelve el objeto spreadsheet de gspread, o lanza ClientesError claro.

    Si se pasa `fecha`, intenta primero localizar la hoja de ese mes entre las
    compartidas con el robot; si no, usa el 'spreadsheet_id' del config.
    """
    cliente = _cliente_gspread(cfg)

    sid = _descubrir_hoja_mes(cliente, fecha) if fecha is not None else None
    sid = sid or cfg.get("spreadsheet_id")
    if not sid:
        raise ClientesError(
            "No se encontró la hoja del mes entre las compartidas con el robot "
            f"({_email_robot(cfg)}) y tampoco hay 'spreadsheet_id' en "
            "config_sheets.json. Comparte el Sheets del mes con el robot "
            "(permiso Lector) o pon su ID en el config."
        )
    try:
        return cliente.open_by_key(sid)
    except Exception as exc:  # noqa: BLE001
        raise ClientesError(
            f"No se pudo abrir el Google Sheets. Revisa que el robot tenga "
            f"acceso a la hoja y que el ID sea correcto.\nDetalle: {exc}"
        ) from exc


def _nombre_pestana(fecha):
    return DIAS_PESTANA.get(fecha.weekday())


def _normalizar(texto):
    """Quita acentos, espacios sobrantes y pasa a mayúsculas para comparar.

    Así 'MIÉRCOLES', 'Miercoles' y 'miercoles ' se consideran iguales.
    """
    import unicodedata
    t = unicodedata.normalize("NFKD", texto or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.strip().upper()


def _buscar_pestana(hoja, nombre):
    """Devuelve la worksheet cuyo título coincide con `nombre` (sin acentos ni
    distinción de mayúsculas), o None si no hay ninguna."""
    objetivo = _normalizar(nombre)
    try:
        hojas = hoja.worksheets()
    except Exception:  # noqa: BLE001
        return None
    for ws in hojas:
        if _normalizar(ws.title) == objetivo:
            return ws
    return None


def _bloque_de_fecha(filas, fecha, col_orden):
    """Recorta las filas que pertenecen al bloque de `fecha`.

    La fecha vive en la col A solo en la primera fila del bloque; se hace
    forward-fill. Devuelve la lista de filas (listas de celdas) de ese bloque.
    `fecha` se compara como dd/mm/yyyy y dd/mm/yy por flexibilidad.
    """
    # Formatos posibles en la hoja: día y mes con o sin cero a la izquierda
    # (independientes entre sí — "5/6/2026" es lo más común al teclear a mano),
    # año de 4 o 2 dígitos. Producto cartesiano de las 8 combinaciones.
    objetivo = {
        f"{d}/{m}/{a}"
        for d in (str(fecha.day), f"{fecha.day:02d}")
        for m in (str(fecha.month), f"{fecha.month:02d}")
        for a in (str(fecha.year), f"{fecha.year % 100:02d}")
    }

    bloque = []
    fecha_actual = None
    capturando = False
    for fila in filas:
        celda_fecha = (fila[0].strip() if fila and len(fila) > 0 else "")
        if celda_fecha:
            fecha_actual = celda_fecha
            # ¿empieza (o termina) el bloque que buscamos?
            if capturando and celda_fecha not in objetivo:
                break  # llegó la siguiente fecha: fin del bloque
            capturando = celda_fecha in objetivo
        if capturando:
            # Solo filas con ORDEN real: descarta separadores vacíos y la
            # cabecera. Las filas de continuación heredan la fecha del bloque.
            orden_val = (fila[col_orden].strip()
                         if len(fila) > col_orden else "")
            if orden_val:
                bloque.append(fila)
    return bloque


def buscar_cliente(orden, fecha=None):
    """Busca el cliente por número de ORDEN en el bloque de la fecha dada.

    `orden`: el número que grita el monitor (entero o str).
    `fecha`: datetime.date; por defecto hoy.
    Devuelve dict {"email", "videos", "orden", "pestana", "fecha"} o lanza
    ClientesError con un mensaje legible si no se encuentra / no configurado.
    """
    fecha = fecha or datetime.date.today()
    cfg = _cargar_config()
    cols = cfg["columnas"]

    pestana = _nombre_pestana(fecha)
    if not pestana:
        raise ClientesError(f"No hay pestaña asignada para {fecha:%A}.")

    hoja = _abrir_hoja(cfg, fecha)
    ws = _buscar_pestana(hoja, pestana)
    if ws is None:
        try:
            disponibles = ", ".join(w.title for w in hoja.worksheets())
        except Exception:  # noqa: BLE001
            disponibles = "(no se pudieron listar)"
        raise ClientesError(
            f"No existe la pestaña '{pestana}' en la hoja.\n"
            f"Pestañas disponibles: {disponibles}"
        )

    filas = ws.get_all_values()
    bloque = _bloque_de_fecha(filas, fecha, cols["orden"])
    if not bloque:
        # Pista clave: cada mes se crea un Sheets nuevo. Si la fecha buscada
        # no está en la hoja usada, casi seguro que la del mes actual aún no
        # se ha compartido con el robot (la app la encuentra sola al hacerlo).
        raise ClientesError(
            f"No hay ningún bloque con la fecha {fecha:%d/%m/%Y} en la pestaña "
            f"'{pestana}' (hoja usada: '{hoja.title}'). Si ya empezó un mes "
            f"nuevo, comparte el Sheets de este mes con el robot "
            f"({_email_robot(cfg)}, permiso Lector) y vuelve a intentarlo: la "
            f"app lo encontrará automáticamente."
        )

    orden_str = str(orden).strip()
    for fila in bloque:
        if len(fila) <= max(cols["orden"], cols["email"]):
            continue
        val_orden = fila[cols["orden"]].strip()
        if val_orden == orden_str:
            email = fila[cols["email"]].strip()
            videos = fila[cols["videos"]].strip() if len(fila) > cols["videos"] else ""
            return {
                "email": email,
                "videos": videos,
                "orden": orden_str,
                "pestana": pestana,
                "fecha": fecha,
                "hoja": hoja.title,
            }

    raise ClientesError(
        f"No se encontró el ORDEN {orden_str} en el bloque {fecha:%d/%m/%Y} "
        f"de '{pestana}'."
    )


def configurado():
    """True si config_sheets.json existe y es legible (sin abrir la hoja)."""
    try:
        _cargar_config()
        return True
    except ClientesError:
        return False
