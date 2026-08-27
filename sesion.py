"""Guarda el trabajo en curso para poder recuperarlo si la app se cierra.

Motivo: en la tirolina se editan lotes enteros (analizar, revisar, renderizar
y subir cada vídeo) y una sola caída obligaba a repetirlo TODO — incluidas las
subidas, que son lo más caro con esa conexión. Aquí se vuelca el estado a un
JSON después de cada paso importante; al abrir la app, si hay trabajo sin
terminar, se ofrece continuar donde se dejó.

Qué se guarda (todo lo que costó tiempo o decisiones del operador):
  * Análisis: cortes sugeridos, transcripción, onda de audio, duración.
  * Revisión: t0/t1 ya ajustados, Nº de cliente, idioma y clips aprobados.
  * Entrega: ruta del vídeo final y el enlace de Gofile de cada uno.

Dónde: %LOCALAPPDATA%\\SunviewTirolina\\sesion_en_curso.json — a propósito
FUERA de la carpeta de OneDrive, para que el guardado (que ocurre a menudo) no
pelee con la sincronización ni deje el archivo bloqueado a media escritura.

El guardado NUNCA puede tumbar la app: si falla, se ignora en silencio. Es una
red de seguridad, no parte del flujo.
"""

import datetime
import json
import os
import tempfile
from pathlib import Path

VERSION = 1

# Pasados estos días damos la sesión por caducada: los enlaces de Gofile de
# invitado no duran para siempre y recuperar un lote de la semana pasada
# confunde más que ayuda.
DIAS_VALIDEZ = 5


def _carpeta_datos():
    """Carpeta local (no OneDrive) donde vive el archivo de sesión."""
    base = os.environ.get("LOCALAPPDATA")
    carpeta = (Path(base) / "SunviewTirolina" if base
               else Path(tempfile.gettempdir()) / "SunviewTirolina")
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta


def ruta_archivo():
    return _carpeta_datos() / "sesion_en_curso.json"


# ─────────────────────────────────────────────────────────────────────────
# Serialización de un clip
# ─────────────────────────────────────────────────────────────────────────

def _a_texto(valor):
    """Path -> str; None se queda como None."""
    return None if valor is None else str(valor)


def _clip_a_json(c):
    """Convierte un clip de la GUI en algo que JSON pueda guardar.

    `rms` es un array de numpy (800 puntos de la onda) y `frames_grid` un dict
    con claves enteras: ambos necesitan conversión explícita.
    """
    rms = c.get("rms")
    if rms is not None:
        try:
            rms = [round(float(v), 5) for v in rms]
        except (TypeError, ValueError):
            rms = None

    grid = {}
    for segundo, ruta in (c.get("frames_grid") or {}).items():
        grid[str(segundo)] = str(ruta)

    return {
        "video_path": _a_texto(c.get("video_path")),
        "clave": c.get("clave"),
        "audio_tmp": _a_texto(c.get("audio_tmp")),
        "duracion": c.get("duracion", 0),
        "t_inicio_raw": c.get("t_inicio_raw"),
        "t_fin_raw": c.get("t_fin_raw"),
        "texto_inicio": c.get("texto_inicio", ""),
        "texto_fin": c.get("texto_fin", ""),
        "t0": c.get("t0", 0.0),
        "t1": c.get("t1", 0.0),
        "rms": rms,
        "frames_grid": grid,
        "orden": c.get("orden", ""),
        "idioma": c.get("idioma", "es"),
        "ok": bool(c.get("ok")),
        "rendered": bool(c.get("rendered")),
        "error": c.get("error"),
        "render_error": c.get("render_error"),
        "salida": _a_texto(c.get("salida")),
        "link": c.get("link"),
    }


def _clip_desde_json(d):
    """Reconstruye el clip tal y como lo espera la GUI.

    Los archivos temporales (audio y frames) viven en una carpeta que se borra
    al cerrar bien la app: si ya no están, se descartan y la GUI enseña
    "(sin frame)" en lugar de fallar. El vídeo final sí se comprueba en disco,
    porque de él depende poder subirlo o reenviar el enlace.
    """
    import numpy as np

    rms = d.get("rms")
    if rms is not None:
        rms = np.array(rms, dtype=np.float32)

    grid = {}
    for segundo, ruta in (d.get("frames_grid") or {}).items():
        p = Path(ruta)
        if p.exists():                      # los temporales pueden haberse ido
            grid[int(segundo)] = p

    audio_tmp = d.get("audio_tmp")
    if audio_tmp and not Path(audio_tmp).exists():
        audio_tmp = None
    elif audio_tmp:
        audio_tmp = Path(audio_tmp)

    salida = d.get("salida")
    rendered = bool(d.get("rendered"))
    if salida and Path(salida).exists():
        salida = Path(salida)
    else:
        # El mp4 final ya no está: hay que volver a generarlo, y sin vídeo el
        # enlace antiguo no sirve de nada para esta fila.
        salida, rendered = None, False

    clip = {
        "video_path": Path(d["video_path"]) if d.get("video_path") else None,
        "clave": d.get("clave"),
        "audio_tmp": audio_tmp,
        "duracion": d.get("duracion", 0),
        "t_inicio_raw": d.get("t_inicio_raw"),
        "t_fin_raw": d.get("t_fin_raw"),
        "texto_inicio": d.get("texto_inicio", ""),
        "texto_fin": d.get("texto_fin", ""),
        "t0": d.get("t0", 0.0),
        "t1": d.get("t1", 0.0),
        "rms": rms,
        "frames_grid": grid,
        "orden": d.get("orden", ""),
        "idioma": d.get("idioma", "es"),
        "ok": bool(d.get("ok")),
        "rendered": rendered,
        "error": d.get("error"),
        "render_error": d.get("render_error"),
    }
    if salida is not None:
        clip["salida"] = salida
        if d.get("link"):
            clip["link"] = d["link"]
    return clip


# ─────────────────────────────────────────────────────────────────────────
# Guardar / cargar / borrar
# ─────────────────────────────────────────────────────────────────────────

def guardar(fase, videos, clips, clip_sel=-1, fecha_entrega="", tmp_dir=None):
    """Vuelca el estado a disco. Devuelve True si se guardó.

    Nunca lanza: si el guardado falla (disco lleno, permisos, antivirus), la
    app debe seguir funcionando igual. Se escribe primero a un archivo
    temporal y luego se renombra, para que un corte a media escritura no deje
    un JSON roto que impida recuperar nada.
    """
    try:
        estado = {
            "version": VERSION,
            "guardado": datetime.datetime.now().isoformat(timespec="seconds"),
            "fase": fase,
            "videos": [str(v) for v in (videos or [])],
            "clip_sel": clip_sel,
            "fecha_entrega": fecha_entrega,
            "tmp_dir": _a_texto(tmp_dir),
            "clips": [_clip_a_json(c) for c in (clips or [])],
        }
        destino = ruta_archivo()
        provisional = destino.with_suffix(".json.tmp")
        with open(provisional, "w", encoding="utf-8") as f:
            json.dump(estado, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(provisional, destino)
        return True
    except Exception:  # noqa: BLE001 — guardar es opcional, jamás bloquea
        return False


def cargar():
    """Lee la sesión guardada. Devuelve el dict de estado o None.

    Devuelve None si no hay archivo, si está corrupto, si es de una versión
    distinta, si ha caducado o si no queda nada aprovechable que recuperar.
    """
    try:
        destino = ruta_archivo()
        if not destino.exists():
            return None
        with open(destino, encoding="utf-8") as f:
            estado = json.load(f)
    except Exception:  # noqa: BLE001 — un JSON roto no debe impedir abrir
        return None

    if estado.get("version") != VERSION:
        return None

    try:
        guardado = datetime.datetime.fromisoformat(estado["guardado"])
    except (KeyError, ValueError):
        return None
    if (datetime.datetime.now() - guardado).days >= DIAS_VALIDEZ:
        return None

    try:
        estado["clips"] = [_clip_desde_json(d) for d in estado.get("clips", [])]
        estado["videos"] = [Path(v) for v in estado.get("videos", [])]
    except Exception:  # noqa: BLE001
        return None

    estado["_guardado_dt"] = guardado
    return estado if hay_algo_que_recuperar(estado) else None


def borrar():
    """Elimina la sesión guardada (lote terminado o descartado)."""
    try:
        ruta_archivo().unlink(missing_ok=True)
        ruta_archivo().with_suffix(".json.tmp").unlink(missing_ok=True)
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────────────────
# Ayudas para decidir qué ofrecer al operador
# ─────────────────────────────────────────────────────────────────────────

def hay_algo_que_recuperar(estado):
    """True si la sesión guardada aporta algo que valga la pena restaurar.

    Un lote recién abierto (sin analizar) no cuenta: repetir la selección de
    archivos es trivial y preguntarlo solo estorba.
    """
    clips = estado.get("clips") or []
    return any(c.get("video_path") and not c.get("error") for c in clips)


def fase_a_restaurar(estado):
    """Fase segura en la que reabrir la app.

    Las fases con un proceso a medias no se pueden reanudar tal cual (el hilo
    que analizaba o renderizaba murió con la app), así que se retrocede a la
    última pantalla estable desde la que el operador puede continuar:
      analizando -> inicio    (el análisis quedó incompleto: se repite)
      rendering  -> revision  (se vuelve a pulsar «Generar»)
    """
    fase = estado.get("fase", "inicio")
    if fase == "analizando":
        return "inicio"
    if fase == "rendering":
        return "revision"
    if fase == "fin" and not any(c.get("rendered") for c in estado.get("clips", [])):
        # Se cerró en la pantalla final pero ningún vídeo sobrevivió en disco.
        return "revision"
    return fase


def resumen(estado):
    """Texto corto para el diálogo de recuperación, en cristiano."""
    clips = estado.get("clips") or []
    utiles = [c for c in clips if not c.get("error")]
    renderizados = [c for c in utiles if c.get("rendered")]
    con_enlace = [c for c in utiles if c.get("link")]
    aprobados = [c for c in utiles if c.get("ok")]

    cuando = estado.get("_guardado_dt")
    linea_fecha = (f"del {cuando:%d/%m/%Y} a las {cuando:%H:%M}"
                   if cuando else "anterior")

    partes = [f"{len(utiles)} vídeo(s)"]
    if renderizados:
        partes.append(f"{len(renderizados)} ya generado(s)")
    elif aprobados:
        partes.append(f"{len(aprobados)} aprobado(s)")
    if con_enlace:
        partes.append(f"{len(con_enlace)} ya subido(s) con enlace")

    fase = fase_a_restaurar(estado)
    donde = {
        "inicio": "Se quedó en la selección de vídeos.",
        "revision": "Se quedó revisando los cortes.",
        "fin": "Se quedó en la pantalla de entrega.",
    }.get(fase, "")

    aviso = ""
    if con_enlace:
        aviso = ("\n\nLos enlaces ya subidos se conservan: no hará falta "
                 "volver a subir esos vídeos.")

    return (f"Encontré trabajo sin terminar {linea_fecha}:\n\n"
            f"    · " + "\n    · ".join(partes) + f"\n\n{donde}{aviso}\n\n"
            "¿Continuar donde lo dejaste?")
