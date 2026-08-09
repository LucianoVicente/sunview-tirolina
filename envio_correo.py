"""Asistente de entrega del vídeo al cliente.

Flujo principal (automático, sin coste, sin cuenta): el vídeo se sube a
Gofile.io con su API pública, que devuelve un enlace de descarga. El humano
solo revisa el Gmail pre-redactado (ya con el link) y pulsa enviar.

Por qué Gofile y no WeTransfer: WeTransfer eliminó la subida anónima de su
API (el endpoint devuelve 404 y ahora exige cuenta + verificación), y
SwissTransfer exige un captcha por subida. Gofile es el único equivalente
con API oficial documentada y subida anónima (verificado 08/2026).

Métodos disponibles (config_sheets.json → "entrega_metodo"):
  * "gofile" (defecto) — subida automática, sin configuración.
  * "drive"  — sube al Google Drive de la cuenta de servicio del Sheets
               (requiere API de Drive habilitada y cuota libre).
  * "manual" — flujo antiguo: abre WeTransfer + explorador para arrastrar.
Si el método automático falla, la GUI muestra el motivo y puede usarse el
flujo manual.

Aquí vive también:
  * Plantillas de correo (ES / EN) con marcadores {nombre} y {link}.
  * Construir la URL de "compose" de Gmail con destinatario + asunto + cuerpo
    ya escritos (no requiere API ni OAuth de Gmail).

La búsqueda de email por número de cliente vive aparte, en clientes.py.
"""

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

WETRANSFER_URL = "https://wetransfer.com/"

# Config opcional compartida con clientes.py. De aquí solo leemos, si existe, la
# clave 'remitente_gmail': la cuenta de Gmail desde la que se redacta el correo.
# Sirve para que, si hay varias sesiones de Google abiertas (p. ej. una de
# Hotmail SIN buzón Gmail), el navegador abra la cuenta correcta y no el
# formulario de "crear Gmail".
_CONFIG_PATH = Path(__file__).resolve().parent / "config_sheets.json"


def _cargar_config():
    """Lee config_sheets.json como dict; {} si falta o está corrupto."""
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def remitente_configurado():
    """Devuelve la cuenta de correo del remitente si está en config, o "".

    Clave nueva 'remitente_correo'; se acepta la antigua 'remitente_gmail'
    por compatibilidad.
    """
    cfg = _cargar_config()
    return (cfg.get("remitente_correo") or cfg.get("remitente_gmail") or "").strip()


# Dominios cuyo buzón vive en Outlook web (cuentas Microsoft personales).
_DOMINIOS_OUTLOOK = ("hotmail.", "outlook.", "live.", "msn.")


def proveedor_correo(remitente=None):
    """'outlook' si el remitente es Hotmail/Outlook/Live/MSN; si no, 'gmail'.

    Crítico para cuentas de Google creadas con un correo de Hotmail: NO tienen
    buzón de Gmail, y la URL de compose de Gmail les muestra el formulario
    "Agrega Gmail a tu cuenta" en vez del correo.
    """
    if remitente is None:
        remitente = remitente_configurado()
    dominio = remitente.split("@")[-1].lower() if "@" in remitente else ""
    if any(dominio.startswith(d) for d in _DOMINIOS_OUTLOOK):
        return "outlook"
    return "gmail"


def metodo_entrega():
    """Método de subida configurado: 'gofile' (defecto), 'drive' o 'manual'."""
    m = (_cargar_config().get("entrega_metodo") or "").strip().lower()
    return m if m in ("gofile", "drive", "manual") else "gofile"


class EnvioError(Exception):
    """Error de subida o de configuración, con mensaje legible para la GUI."""


class _LimiteGofile(EnvioError):
    """Gofile respondió 429: el cupo por IP está agotado y solo cabe esperar.

    Se distingue del resto de errores porque cambiar de servidor no arregla
    nada: el límite es del equipo que sube, no del servidor que recibe.
    """


# ─── Subida automática a Gofile ───
# La API pide primero un servidor de almacenamiento y luego sube a ese host
# concreto. NO se cablea aquí ningún host de subida: el endpoint genérico
# "upload.gofile.io/uploadfile" que usábamos antes fue retirado y ahora
# devuelve 500 (error-createFolderResponse) con ficheros pequeños y corta la
# conexión a media subida con los vídeos grandes (WinError 10054).
#
# Gofile además limita por IP el uso anónimo, y cada subida sin token crea una
# cuenta de invitado nueva. Al lanzar los siete vídeos de golpe se agotaba el
# cupo y la API devolvía 429 (error-rateLimit) a lo siguiente que se le pidiera
# —el "pedir servidor" de las últimas filas—. De ahí las tres medidas de abajo:
# un vídeo cada vez, un único token de invitado para todos, y esperar cuando
# aun así llegue un 429.
GOFILE_SERVERS_URL = "https://api.gofile.io/servers"
_GOFILE_UPLOAD_PATH = "/contents/uploadfile"
_ZONA_PREFERIDA = "eu"  # servidor cercano: la subida desde España va más rápida
_MAX_SERVIDORES = 3     # si un store concreto está caído, se prueba el siguiente
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SunviewPark/1.0"

SUBIDAS_SIMULTANEAS = 1      # las demás esperan turno en el semáforo
_ESPERAS_429 = (10, 30, 60)  # segundos antes de cada reintento tras un 429
_CACHE_SERVIDORES_SEG = 600  # la lista de servidores apenas cambia

_semaforo_subida = threading.BoundedSemaphore(SUBIDAS_SIMULTANEAS)
_lock_gofile = threading.Lock()  # protege el token y la caché de servidores
_token_invitado = None           # cuenta de invitado reutilizada entre vídeos
_cache_servidores = None         # (momento, [nombres])


def _deps_subida():
    """Importa requests + toolbelt bajo demanda (no son dependencia dura)."""
    try:
        import requests
        from requests_toolbelt.multipart.encoder import (
            MultipartEncoder, MultipartEncoderMonitor)
    except ImportError as exc:
        raise EnvioError(
            "Faltan dependencias. Instala con:\n"
            "  pip install requests requests-toolbelt"
        ) from exc
    return requests, MultipartEncoder, MultipartEncoderMonitor


_MSG_429 = ("Gofile está limitando las subidas desde este equipo. "
            "Espera unos minutos y vuelve a pulsar «Subir vídeo».")


def _olvidar_token(token):
    """Descarta la cuenta de invitado, si sigue siendo la que ha fallado."""
    global _token_invitado
    with _lock_gofile:
        if _token_invitado == token:
            _token_invitado = None


def _servidores_gofile():
    """Nombres de los servidores de subida, los de la zona preferida primero.

    La lista se cachea unos minutos: con varios vídeos en cola no aporta nada
    volver a preguntar lo mismo, y cada petición de más gasta cupo. Lanza
    EnvioError si la API no responde o no devuelve ninguno: sin servidor no hay
    subida posible, y cablear uno fijo es justo lo que se rompió antes.
    """
    global _cache_servidores
    with _lock_gofile:
        if (_cache_servidores
                and time.time() - _cache_servidores[0] < _CACHE_SERVIDORES_SEG):
            return list(_cache_servidores[1])

    requests, _, _ = _deps_subida()
    try:
        r = requests.get(GOFILE_SERVERS_URL,
                         headers={"User-Agent": _USER_AGENT}, timeout=30)
    except requests.RequestException as exc:
        raise EnvioError(f"No se pudo conectar con Gofile: {exc}") from exc
    if r.status_code == 429:
        raise _LimiteGofile(_MSG_429)
    if r.status_code != 200:
        raise EnvioError(
            f"Gofile respondió {r.status_code} al pedir servidor: {r.text[:200]}")
    try:
        servidores = r.json()["data"]["servers"]
    except (ValueError, KeyError, TypeError) as exc:
        raise EnvioError(
            f"Respuesta inesperada de Gofile: {r.text[:200]}") from exc
    # Orden estable: la zona preferida delante, respetando el orden de la API.
    servidores.sort(key=lambda s: s.get("zone") != _ZONA_PREFERIDA)
    nombres = [s["name"] for s in servidores if s.get("name")]
    if not nombres:
        raise EnvioError("Gofile no devolvió ningún servidor de subida.")
    with _lock_gofile:
        _cache_servidores = (time.time(), list(nombres))
    return nombres


def _subir_a_servidor_gofile(servidor, archivo, progreso):
    """Un intento de subida a un servidor concreto; devuelve el enlace.

    Reutiliza el token de invitado que devolvió la primera subida, para que
    todos los vídeos vayan a la misma cuenta en vez de crear una nueva cada
    vez (crear cuentas a ráfagas es lo que dispara el 429). Cada vídeo sigue
    teniendo su propia carpeta y su propio enlace.

    Los mensajes de EnvioError son fragmentos en minúscula: quien llama los
    compone con el nombre del servidor.
    """
    global _token_invitado
    requests, MultipartEncoder, MultipartEncoderMonitor = _deps_subida()
    url = f"https://{servidor}.gofile.io{_GOFILE_UPLOAD_PATH}"
    with _lock_gofile:
        token = _token_invitado

    with open(archivo, "rb") as f:
        encoder = MultipartEncoder(
            fields={"file": (archivo.name, f, "video/mp4")})
        cabeceras = {"Content-Type": encoder.content_type,
                     "User-Agent": _USER_AGENT}
        if token:
            cabeceras["Authorization"] = f"Bearer {token}"

        def _monitor(m):
            if progreso is not None and encoder.len:
                progreso(min(100, int(m.bytes_read / encoder.len * 100)))

        try:
            r = requests.post(
                url,
                data=MultipartEncoderMonitor(encoder, _monitor),
                headers=cabeceras,
                timeout=(30, 1800),  # vídeos grandes con subida lenta
            )
        except requests.RequestException as exc:
            raise EnvioError(f"conexión interrumpida ({exc})") from exc

    if r.status_code == 429:
        raise _LimiteGofile(_MSG_429)
    if r.status_code in (401, 403) and token:
        # La cuenta de invitado ya no vale: se olvida y el siguiente intento
        # sube de forma anónima, creando una nueva.
        _olvidar_token(token)
        raise EnvioError("la sesión de invitado caducó")
    if r.status_code != 200:
        raise EnvioError(f"respondió {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
    except ValueError as exc:
        raise EnvioError(f"respuesta inesperada: {r.text[:200]}") from exc
    if data.get("status") != "ok" or not data.get("data", {}).get("downloadPage"):
        raise EnvioError(f"no devolvió enlace: {str(data)[:200]}")
    with _lock_gofile:
        _token_invitado = data["data"].get("guestToken") or _token_invitado
    return data["data"]["downloadPage"]


def _intento_gofile(archivo, progreso):
    """Pide servidor y prueba a subir en los primeros de la lista."""
    servidores = _servidores_gofile()[:_MAX_SERVIDORES]
    ultimo_error = ""
    for servidor in servidores:
        try:
            return _subir_a_servidor_gofile(servidor, archivo, progreso)
        except _LimiteGofile:
            raise  # el cupo es por IP: cambiar de servidor no arregla nada
        except EnvioError as exc:
            ultimo_error = f"{servidor}: {exc}"
    raise EnvioError(f"Gofile falló en {len(servidores)} servidores. "
                     f"Último error → {ultimo_error}")


def subir_video_gofile(archivo, progreso=None):
    """Sube el vídeo a Gofile (API oficial) y devuelve el enlace de descarga.

    `progreso` es un callable(pct:int) opcional, y no se llama hasta que llega
    el turno: mientras la barra siga a la espera, el vídeo está en cola. La
    subida es streaming (no carga el vídeo entero en RAM), se reintenta en otro
    servidor si el primero falla y espera si Gofile limita el ritmo.

    Solo sube un vídeo a la vez a propósito: en paralelo se reparten la misma
    línea (todos lentos y ninguno listo) y agotan el cupo por IP de Gofile.
    Lanza EnvioError si no lo consigue.
    """
    archivo = Path(archivo)
    if not archivo.exists():
        raise EnvioError(f"No existe el archivo: {archivo}")

    with _semaforo_subida:  # los demás vídeos esperan aquí su turno
        limite = None
        for espera in (0,) + _ESPERAS_429:
            if espera:
                time.sleep(espera)
            try:
                return _intento_gofile(archivo, progreso)
            except _LimiteGofile as exc:
                limite = exc  # esperar y reintentar es lo único que ayuda
        raise limite


# ─── Subida automática a Google Drive ───
# Reutiliza la cuenta de servicio del Sheets. El robot tiene 15 GB propios de
# Drive; con retención de unos días no se llenan nunca (≈150 MB por vídeo).

# Días que el vídeo queda disponible antes del borrado automático. La
# plantilla del correo avisa de que "el enlace está disponible solo unos
# días", así que ambos deben ser coherentes. Override: "drive_retencion_dias".
RETENCION_DIAS_DEFECTO = 7


def drive_configurado():
    """True si existe la clave de cuenta de servicio (no comprueba la API)."""
    sa = _cargar_config().get("service_account_json")
    return bool(sa and Path(sa).exists())


def _sesion_drive():
    """AuthorizedSession con scope drive.file (solo archivos creados por la app)."""
    try:
        from google.oauth2.service_account import Credentials
        from google.auth.transport.requests import AuthorizedSession
    except ImportError as exc:
        raise EnvioError(
            "Faltan dependencias de Google. Instala con:\n"
            "  pip install google-auth"
        ) from exc

    cfg = _cargar_config()
    sa = cfg.get("service_account_json")
    if not sa or not Path(sa).exists():
        raise EnvioError(
            "No está configurada la cuenta de servicio (config_sheets.json → "
            "service_account_json). Ver CONFIGURAR_CORREO.md."
        )
    creds = Credentials.from_service_account_file(
        sa, scopes=["https://www.googleapis.com/auth/drive.file"])
    return AuthorizedSession(creds)


def _msg_error_drive(resp):
    """Convierte una respuesta de error de la API de Drive en mensaje útil."""
    texto = ""
    try:
        texto = resp.json().get("error", {}).get("message", "")
    except Exception:  # noqa: BLE001 — cuerpo no-JSON
        texto = (resp.text or "")[:200]
    if "accessNotConfigured" in resp.text or "has not been used in project" in texto:
        return (
            "La API de Google Drive no está habilitada en el proyecto de la "
            "cuenta de servicio. Entra en console.cloud.google.com → APIs y "
            "servicios → Biblioteca → 'Google Drive API' → Habilitar."
        )
    return f"Drive respondió {resp.status_code}: {texto}"


def subir_video_drive(archivo, progreso=None):
    """Sube el vídeo a Drive y devuelve el enlace de descarga compartido.

    Subida resumable en trozos de 8 MB (el upload simple de Drive tope a 5 MB
    y los vídeos pesan 50-300 MB). `progreso` es un callable(pct:int) opcional.
    Tras subir, comparte como 'cualquiera con el enlace' (lector).
    Lanza EnvioError con mensaje legible si algo falla.
    """
    archivo = Path(archivo)
    if not archivo.exists():
        raise EnvioError(f"No existe el archivo: {archivo}")
    size = archivo.stat().st_size
    sesion = _sesion_drive()

    # 1) Abrir sesión de subida resumable
    r = sesion.post(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable",
        json={"name": archivo.name, "mimeType": "video/mp4"},
        headers={
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(size),
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise EnvioError(_msg_error_drive(r))
    upload_url = r.headers.get("Location")
    if not upload_url:
        raise EnvioError("Drive no devolvió URL de subida (respuesta inesperada).")

    # 2) Subir en trozos (8 MB = múltiplo de 256 KiB, requisito de la API)
    CHUNK = 8 * 1024 * 1024
    file_id = None
    with open(archivo, "rb") as f:
        offset = 0
        while offset < size:
            datos = f.read(CHUNK)
            fin = offset + len(datos)
            r = sesion.put(
                upload_url,
                data=datos,
                headers={"Content-Range": f"bytes {offset}-{fin - 1}/{size}"},
                timeout=300,
            )
            if r.status_code in (200, 201):
                file_id = r.json().get("id")
            elif r.status_code != 308:  # 308 = trozo aceptado, sigue
                raise EnvioError(_msg_error_drive(r))
            offset = fin
            if progreso is not None:
                progreso(min(100, int(offset / size * 100)))
    if not file_id:
        raise EnvioError("La subida terminó pero Drive no devolvió el ID del archivo.")

    # 3) Compartir: cualquiera con el enlace puede ver/descargar
    r = sesion.post(
        f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
        json={"role": "reader", "type": "anyone"},
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise EnvioError("Subido pero no se pudo compartir: " + _msg_error_drive(r))

    return f"https://drive.google.com/file/d/{file_id}/view"


def limpiar_drive_antiguos(dias=None):
    """Borra del Drive del robot los vídeos con más de `dias` días.

    Mantiene libres los 15 GB de la cuenta de servicio. Best-effort: cualquier
    fallo se ignora (se reintentará en la siguiente subida). Devuelve cuántos
    archivos borró.
    """
    import datetime
    if dias is None:
        try:
            dias = int(_cargar_config().get("drive_retencion_dias",
                                            RETENCION_DIAS_DEFECTO))
        except (TypeError, ValueError):
            dias = RETENCION_DIAS_DEFECTO
    borrados = 0
    try:
        sesion = _sesion_drive()
        limite = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=dias)).strftime("%Y-%m-%dT%H:%M:%S")
        r = sesion.get(
            "https://www.googleapis.com/drive/v3/files",
            params={
                "q": f"modifiedTime < '{limite}' and trashed = false",
                "fields": "files(id,name)",
                "pageSize": 100,
            },
            timeout=30,
        )
        if r.status_code != 200:
            return 0
        for f in r.json().get("files", []):
            dr = sesion.delete(
                f"https://www.googleapis.com/drive/v3/files/{f['id']}",
                timeout=30,
            )
            if dr.status_code in (200, 204):
                borrados += 1
    except Exception:  # noqa: BLE001 — la limpieza nunca debe romper nada
        pass
    return borrados

# Texto que se deja en el cuerpo cuando aún no tenemos el enlace; el humano lo
# sustituye al pegar, o la GUI lo rellena leyendo el portapapeles.
PLACEHOLDER_LINK = "👉 (pega aquí el enlace de descarga)"

# Enlaces fijos de marca que van en cada mensaje (correo y WhatsApp).
# La reseña abre directo el cuadro de valoración de Google (las 5★ las marca
# el cliente: Google no permite precargarlas).
RESENA_URL = ("https://search.google.com/local/writereview"
              "?placeid=ChIJyyiD6j7vcg0RqDMcA3PjXac")
INSTAGRAM_URL = "https://www.instagram.com/sunviewparkadventure/"
TIKTOK_URL = "https://www.tiktok.com/@sunviewpark"

# Firma común (reseña + redes + despedida). Se concatena al final del cuerpo;
# OJO: sin llaves {}, porque luego se hace .format() sobre el cuerpo completo.
_FIRMA_ES = (
    "\n\n━━━━━━━━━━━━━━━\n"
    "⭐ ¿Te lo has pasado en grande? ¡Déjanos 5 estrellas! Es solo un "
    "momento y nos alegras el día:\n"
    + RESENA_URL +
    "\n\n📲 Síguenos y etiquétanos en tus vídeos, ¡nos encanta verlos!\n"
    "📸 Instagram: " + INSTAGRAM_URL + "\n"
    "🎵 TikTok: " + TIKTOK_URL +
    "\n\n¡Gracias por volar con nosotros! 🦅☀️\n"
    "Equipo Sunview Park"
)
_FIRMA_EN = (
    "\n\n━━━━━━━━━━━━━━━\n"
    "⭐ Had a blast? Leave us 5 stars! It only takes a moment and makes "
    "our day:\n"
    + RESENA_URL +
    "\n\n📲 Follow us and tag us in your videos, we love seeing them!\n"
    "📸 Instagram: " + INSTAGRAM_URL + "\n"
    "🎵 TikTok: " + TIKTOK_URL +
    "\n\nThanks for flying with us! 🦅☀️\n"
    "The Sunview Park Team"
)

# Plantillas por idioma. {link} lo rellena la app (la firma ya trae sus URLs).
# "cuerpo_varios" se usa cuando el cliente tiene más de un vídeo: un solo
# mensaje con todos sus enlaces, uno por línea.
PLANTILLAS = {
    "es": {
        "asunto": "🪂 ¡Tu vídeo de tirolina en Sunview Park!",
        "cuerpo": (
            "¡Hola! 👋\n\n"
            "Aquí tienes el vídeo de tu salto en la tirolina de Sunview Park "
            "para que lo revivas las veces que quieras 🪂💨\n\n"
            "👉 Descárgalo aquí:\n{link}\n\n"
            "⏳ El enlace caduca en unos días, ¡guárdalo cuanto antes!"
            + _FIRMA_ES
        ),
        "cuerpo_varios": (
            "¡Hola! 👋\n\n"
            "Aquí tienes los vídeos de tus saltos en la tirolina de Sunview "
            "Park para que los revivas las veces que quieras 🪂💨\n\n"
            "👉 Descárgalos aquí:\n{link}\n\n"
            "⏳ Los enlaces caducan en unos días, ¡guárdalos cuanto antes!"
            + _FIRMA_ES
        ),
    },
    "en": {
        "asunto": "🪂 Your zipline video at Sunview Park!",
        "cuerpo": (
            "Hi there! 👋\n\n"
            "What a ride! 🪂💨 Here's the video of your zipline jump at Sunview "
            "Park so you can relive it as many times as you want (and show it "
            "off a little 😎).\n\n"
            "👉 Download it here:\n{link}\n\n"
            "⏳ The link expires in a few days, so save it soon!"
            + _FIRMA_EN
        ),
        "cuerpo_varios": (
            "Hi there! 👋\n\n"
            "What rides! 🪂💨 Here are the videos of your zipline jumps at "
            "Sunview Park so you can relive them as many times as you want (and "
            "show them off a little 😎).\n\n"
            "👉 Download them here:\n{link}\n\n"
            "⏳ The links expire in a few days, so save them soon!"
            + _FIRMA_EN
        ),
    },
}

IDIOMAS_DISPONIBLES = ("es", "en")


def redactar_correo(idioma, nombre, link=None):
    """Devuelve (asunto, cuerpo) con la plantilla del idioma ya rellenada.

    `link` puede ser un enlace, una LISTA de enlaces (cliente con varios
    vídeos: un solo correo con un enlace por línea) o None — en ese caso se
    usa un marcador para que el humano lo pegue. Si `nombre` está vacío se
    usa un saludo genérico para no dejar "¡Hola !".
    """
    plantilla = PLANTILLAS.get(idioma, PLANTILLAS["es"])
    nombre = (nombre or "").strip() or ("there" if idioma == "en" else "")
    if isinstance(link, (list, tuple)):
        links = [l.strip() for l in link if l and l.strip()]
    else:
        links = [link.strip()] if link and link.strip() else []
    link_txt = "\n".join(links) if links else PLACEHOLDER_LINK
    asunto = plantilla["asunto"]
    clave_cuerpo = "cuerpo_varios" if len(links) > 1 else "cuerpo"
    cuerpo = plantilla[clave_cuerpo].format(nombre=nombre, link=link_txt)
    # Si no había nombre en español, limpia el doble espacio del saludo.
    cuerpo = cuerpo.replace("¡Hola !", "¡Hola!").replace("Hi !", "Hi there!")
    return asunto, cuerpo


def construir_url_gmail(destinatario, asunto, cuerpo, remitente=None):
    """URL de redacción de Gmail con los campos ya escritos.

    `view=cm&fs=1` abre la ventana de redacción a pantalla completa. No usa
    API ni credenciales: simplemente abre Gmail en el navegador con todo
    puesto, listo para que el humano revise y pulse enviar.

    Si `remitente` es una cuenta de Gmail, se añade `authuser=<cuenta>` para
    forzar que el navegador use ESA sesión de Google (útil cuando hay varias
    cuentas abiertas y la activa no tiene buzón Gmail).
    """
    params = {
        "view": "cm",
        "fs": "1",
        "to": destinatario or "",
        "su": asunto or "",
        "body": cuerpo or "",
    }
    remitente = (remitente or "").strip()
    if remitente:
        params["authuser"] = remitente
    return "https://mail.google.com/mail/?" + urlencode(params)


def abrir_wetransfer():
    """Abre WeTransfer en el navegador por defecto (siempre en pestaña nueva)."""
    webbrowser.open(WETRANSFER_URL, new=2)


def normalizar_telefono(numero):
    """Deja el teléfono en formato que entiende wa.me: solo dígitos.

    Quita +, espacios, guiones y paréntesis. Un '00' inicial (prefijo de
    salida internacional) se descarta porque WhatsApp espera el número en
    formato internacional sin signos. El número del Sheets DEBE llevar el
    prefijo de país (ej. 34 para España) para que el chat abra bien.
    """
    if not numero:
        return ""
    digitos = "".join(ch for ch in str(numero) if ch.isdigit())
    if digitos.startswith("00"):
        digitos = digitos[2:]
    return digitos


def construir_url_whatsapp(numero, mensaje):
    """URL de WhatsApp Web (wa.me) con el número y el mensaje ya escritos.

    Sin número válido → abre WhatsApp sin destinatario para elegir el contacto
    a mano. wa.me redirige a WhatsApp Web en el navegador.
    """
    num = normalizar_telefono(numero)
    params = {"text": mensaje or ""}
    base = f"https://wa.me/{num}" if num else "https://wa.me/"
    return base + "?" + urlencode(params)


def construir_uri_whatsapp_app(numero, mensaje):
    """URI 'whatsapp://' para la app de escritorio con el chat y el mensaje.

    Es el esquema que registra WhatsApp Desktop al instalarse; abrirlo lanza
    la app (no el navegador). Sin número, abre la app para elegir el contacto.
    """
    num = normalizar_telefono(numero)
    if num:
        params = {"phone": num, "text": mensaje or ""}
    else:
        params = {"text": mensaje or ""}
    return "whatsapp://send?" + urlencode(params)


def abrir_whatsapp(numero, mensaje):
    """Abre el chat del cliente en la app de WhatsApp de escritorio.

    En recepción usan la app de escritorio, así que probamos primero el
    esquema 'whatsapp://' (lo abre ShellExecute vía os.startfile). Si la app
    no está instalada/registrada, os.startfile lanza OSError y caemos a
    WhatsApp Web en el navegador para no quedarnos sin enviar.
    """
    if sys.platform == "win32":
        try:
            os.startfile(construir_uri_whatsapp_app(numero, mensaje))
            return
        except OSError:
            pass  # la app no está instalada: usamos la web
    webbrowser.open(construir_url_whatsapp(numero, mensaje), new=2)


def construir_url_outlook(destinatario, asunto, cuerpo):
    """URL de redacción de Outlook web (Hotmail/Outlook/Live) con campos puestos.

    El deeplink de compose abre la ventana de redacción de la sesión de
    outlook.live.com ya iniciada en el navegador.
    """
    params = {
        "to": destinatario or "",
        "subject": asunto or "",
        "body": cuerpo or "",
    }
    return "https://outlook.live.com/mail/0/deeplink/compose?" + urlencode(params)


# Rutas típicas de Edge (no está en PATH; el registro App Paths solo lo
# resuelve ShellExecute, no subprocess).
_EDGE_PATHS = (
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    / "Microsoft/Edge/Application/msedge.exe",
)


def _abrir_url_correo(url):
    """Abre la URL del webmail respetando 'edge_perfil' si está configurado.

    'edge_perfil' (ej. "Profile 1") sirve para PCs con varios perfiles de
    Edge donde la sesión del correo vive en un perfil que NO es el que abre
    por defecto. OJO: el nombre visible no coincide con la carpeta interna
    ("Perfil 2" suele ser "Profile 1"). Sin la clave, navegador por defecto.
    """
    perfil = (_cargar_config().get("edge_perfil") or "").strip()
    if perfil and sys.platform == "win32":
        for exe in _EDGE_PATHS:
            if exe.exists():
                subprocess.Popen(
                    [str(exe), f"--profile-directory={perfil}", url])
                return
    webbrowser.open(url, new=2)


def abrir_correo_redactado(destinatario, asunto, cuerpo, remitente=None):
    """Abre el webmail del remitente con el correo ya redactado.

    Elige Gmail u Outlook web según el dominio del remitente configurado
    (config_sheets.json → 'remitente_correo'). Forzamos `new=2` (pestaña
    nueva) porque, si el webmail ya está abierto, el navegador reutilizaría
    la pestaña existente sin releer los parámetros de la URL y la ventana de
    redacción no llegaría a aparecer.
    """
    if remitente is None:
        remitente = remitente_configurado()
    if proveedor_correo(remitente) == "outlook":
        url = construir_url_outlook(destinatario, asunto, cuerpo)
    else:
        url = construir_url_gmail(destinatario, asunto, cuerpo, remitente)
    _abrir_url_correo(url)


# Alias del nombre antiguo, por si algún script externo lo usa.
abrir_gmail_redactado = abrir_correo_redactado


def abrir_carpeta_seleccionando(archivo):
    """Abre el explorador con el archivo final ya seleccionado para arrastrar.

    En Windows usa `explorer /select,<ruta>` para resaltar el vídeo concreto.
    En otros SO abre la carpeta contenedora. Devuelve True si lo intentó.
    """
    archivo = Path(archivo)
    if sys.platform == "win32":
        # explorer devuelve códigos de salida no-cero aunque funcione; no lo
        # tratamos como error. La ruta DEBE ir pegada a "/select," en un único
        # argumento: si se pasan separados, explorer ignora la selección y abre
        # una carpeta cualquiera sin resaltar el vídeo.
        subprocess.run(["explorer", f"/select,{archivo}"])
        return True
    carpeta = archivo.parent if archivo.is_file() else archivo
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(carpeta)])
        else:
            subprocess.run(["xdg-open", str(carpeta)])
        return True
    except Exception:
        return False


def parece_enlace_wetransfer(texto):
    """Heurística para validar que el portapapeles trae un enlace de descarga.

    Acepta dominios típicos de WeTransfer (we.tl, wetransfer.com) y, por si
    cambian de servicio, cualquier https:// razonablemente corto y sin espacios.
    """
    if not texto:
        return False
    t = texto.strip()
    if " " in t or "\n" in t:
        return False
    if not t.lower().startswith(("http://", "https://")):
        return False
    dominios = ("we.tl", "wetransfer.com", "fromsmash.com", "smash")
    if any(d in t.lower() for d in dominios):
        return True
    # Enlace https genérico, plausible como link de descarga
    return len(t) <= 300
