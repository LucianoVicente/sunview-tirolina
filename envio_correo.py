"""Asistente de entrega del vídeo al cliente.

Flujo principal (automático, sin coste, sin cuenta): el vídeo se sube a
Gofile.io con su API pública, que devuelve un enlace de descarga. El humano
solo revisa el Gmail pre-redactado (ya con el link) y pulsa enviar.

Por qué Gofile y no WeTransfer: WeTransfer eliminó la subida anónima de su
API (el endpoint devuelve 404 y ahora exige cuenta + verificación), y
SwissTransfer exige un captcha por subida. Gofile es el único equivalente
con API oficial documentada y subida anónima (verificado 06/2026).

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


# ─── Subida automática a Gofile ───
GOFILE_UPLOAD_URL = "https://upload.gofile.io/uploadfile"
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SunviewPark/1.0"


def subir_video_gofile(archivo, progreso=None):
    """Sube el vídeo a Gofile (anónimo, API oficial) y devuelve el enlace.

    `progreso` es un callable(pct:int) opcional. La subida es streaming
    (no carga el vídeo entero en RAM). Lanza EnvioError si algo falla.
    """
    try:
        import requests
        from requests_toolbelt.multipart.encoder import (
            MultipartEncoder, MultipartEncoderMonitor)
    except ImportError as exc:
        raise EnvioError(
            "Faltan dependencias. Instala con:\n"
            "  pip install requests requests-toolbelt"
        ) from exc

    archivo = Path(archivo)
    if not archivo.exists():
        raise EnvioError(f"No existe el archivo: {archivo}")

    with open(archivo, "rb") as f:
        encoder = MultipartEncoder(
            fields={"file": (archivo.name, f, "video/mp4")})

        def _monitor(m):
            if progreso is not None and encoder.len:
                progreso(min(100, int(m.bytes_read / encoder.len * 100)))

        try:
            r = requests.post(
                GOFILE_UPLOAD_URL,
                data=MultipartEncoderMonitor(encoder, _monitor),
                headers={"Content-Type": encoder.content_type,
                         "User-Agent": _USER_AGENT},
                timeout=(30, 1800),  # vídeos grandes con subida lenta
            )
        except requests.RequestException as exc:
            raise EnvioError(f"No se pudo conectar con Gofile: {exc}") from exc

    if r.status_code != 200:
        raise EnvioError(f"Gofile respondió {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
    except ValueError as exc:
        raise EnvioError(f"Respuesta inesperada de Gofile: {r.text[:200]}") from exc
    if data.get("status") != "ok" or not data.get("data", {}).get("downloadPage"):
        raise EnvioError(f"Gofile no devolvió enlace: {str(data)[:200]}")
    return data["data"]["downloadPage"]


# ─── Subida automática a Google Drive ───
# Reutiliza la cuenta de servicio del Sheets. El robot tiene 15 GB propios de
# Drive; con retención de unos días no se llenan nunca (≈150 MB por vídeo).

# Días que el vídeo queda disponible antes del borrado automático. La
# plantilla del correo avisa de que "el enlace está disponible solo unos
# días", así que ambos deben ser coherentes. Override: "drive_retencion_dias".
RETENCION_DIAS_DEFECTO = 7


class EnvioError(Exception):
    """Error de subida/configuración de Drive, con mensaje legible para la GUI."""


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
PLACEHOLDER_LINK = "👉 (pega aquí el enlace de WeTransfer)"

# Plantillas por idioma. {nombre} y {link} los rellena la app.
PLANTILLAS = {
    "es": {
        "asunto": "Tu vídeo de tirolina en Sunview Park",
        "cuerpo": (
            "¡Hola {nombre}!\n\n"
            "Aquí tienes el vídeo de tu salto en la tirolina de Sunview Park.\n"
            "Puedes descargarlo desde este enlace:\n\n"
            "{link}\n\n"
            "El enlace está disponible solo unos días, así que guárdalo cuanto antes.\n\n"
            "¡Gracias por volar con nosotros!\n"
            "Equipo Sunview Park"
        ),
    },
    "en": {
        "asunto": "Your zipline video at Sunview Park",
        "cuerpo": (
            "Hi {nombre}!\n\n"
            "Here is the video of your zipline jump at Sunview Park.\n"
            "You can download it from this link:\n\n"
            "{link}\n\n"
            "The link is only available for a few days, so please save it soon.\n\n"
            "Thanks for flying with us!\n"
            "The Sunview Park Team"
        ),
    },
}

IDIOMAS_DISPONIBLES = ("es", "en")


def redactar_correo(idioma, nombre, link=None):
    """Devuelve (asunto, cuerpo) con la plantilla del idioma ya rellenada.

    Si `link` es None se usa un marcador para que el humano lo pegue. Si
    `nombre` está vacío se usa un saludo genérico para no dejar "¡Hola !".
    """
    plantilla = PLANTILLAS.get(idioma, PLANTILLAS["es"])
    nombre = (nombre or "").strip() or ("there" if idioma == "en" else "")
    link_txt = link.strip() if link else PLACEHOLDER_LINK
    asunto = plantilla["asunto"]
    cuerpo = plantilla["cuerpo"].format(nombre=nombre, link=link_txt)
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
