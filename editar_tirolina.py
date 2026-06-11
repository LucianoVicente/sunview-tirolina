"""
Sunview Park - Editor automático de vídeos de tirolina
========================================================
"""

import json
import os
import subprocess
import shutil
import sys
import tempfile
import threading
import wave
from contextlib import contextmanager
from pathlib import Path

# ========== CONFIGURACIÓN ==========
_BASE = Path(__file__).parent          # siempre la carpeta del script
CARPETA_ENTRADA = _BASE / "entrada"
CARPETA_SALIDA  = _BASE / "salida"
LOGO_PATH       = _BASE / "assets/logo.png"
MODELO_WHISPER = "small"

# Recorte
SEGUNDOS_ANTES_INICIO = 1.5
SEGUNDOS_DESPUES_FIN = 2.0

# Palabras clave de LLEGADA — frases observadas en los vídeos reales.
# Nota: "hola" se omite a propósito: es saludo genérico que aparece en cualquier
# parte del clip (apertura de cámara, conversación previa) y generaba demasiados
# falsos positivos. Si va acompañada de una llegada real ("hola, qué tal"),
# el match de la otra palabra ya dispara la detección.
PALABRAS_FIN = [
    # Exclamaciones de llegada observadas en los vídeos reales
    "hala",
    "hijos",            # exclamación frecuente al llegar
    "no me lo",         # "¡no me lo creo!"
    "madre mía", "madre mia",
    # Preguntas de llegada
    "todo bien",
    "está bien",
    "cómo estás", "como estás",
    "qué tal", "que tal",
    "cómo ha estado", "como ha estado",
    "cómo estuvo", "como estuvo",
    "cómo fue", "como fue",
    "te ha gustado", "qué te ha parecido",
    # Otras frases de llegada en español
    "sobrevivimos",
    "perfecto",
    "bienvenido",
    "bien bien bien", "bien, bien, bien",
    "bien, bien", "bien bien",
    "buenas",          # "Buenas" suelto al llegar
    "yeee", "yee", "yuhu",
    "apísima",
    # Reacciones de llegada en otros idiomas (clientes extranjeros).
    # Whisper en modo "es" capta fonéticamente palabras comunes EN/FR/IT/DE
    # cuando son cortas y muy marcadas.
    # — Inglés
    "thank you", "thanks", "amazing", "awesome", "incredible", "wow",
    "oh my god", "all good", "very good", "fantastic",
    # — Francés
    "merci", "génial", "incroyable", "trop bien", "magnifique",
    # — Italiano
    "grazie", "bellissimo", "fantastico", "incredibile",
    # — Alemán
    "danke", "wunderbar",
]

# Calidad de salida
RESOLUCION = "1920:1080"
CRF = 23
PRESET = "medium"
LOGO_ESCALA = 0.65  # tamaño del logo
LOGO_MARGEN = 60   # distancia desde la esquina en píxeles

# Comparar siempre con suffix.lower(): cubre .MP4, .Mp4, .mkV, etc.
EXTENSIONES = (".mp4", ".mov", ".avi", ".mkv")

# En Windows, cada subprocess.run lanzando ffmpeg/ffprobe abre una ventana de
# consola visible que parpadea (especialmente molesto desde la GUI, que procesa
# 10+ vídeos seguidos). CREATE_NO_WINDOW la oculta. En otras plataformas el flag
# no existe y se queda en 0 (no-op).
SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Orden de fallback si el modelo principal no cabe en RAM/VRAM.
# small ≈ 1.5 GB, base ≈ 0.5 GB, tiny ≈ 0.2 GB.
MODELOS_WHISPER_FALLBACK = ["small", "base", "tiny"]

# ========== MODO AUTOMÁTICO ==========
MODO_AUTO = True        # True = sin preguntas, procesa todo solo


def _cuda_runtime_disponible():
    """
    Verifica que las DLLs runtime de CUDA (cuBLAS) están realmente cargables.
    `get_cuda_device_count` solo mira el driver — no garantiza que las
    librerías de runtime estén instaladas. Sin esta comprobación, faster-whisper
    carga el modelo en GPU OK pero crashea en el primer encode con
    "cublas64_12.dll not found".
    """
    if sys.platform != "win32":
        # En Linux/macOS faster-whisper bundlea o usa rpath; con que get_cuda_device_count
        # responda OK suele bastar.
        return True
    try:
        import ctypes
        ctypes.WinDLL("cublas64_12.dll")
        return True
    except OSError:
        return False


def _detectar_dispositivo_whisper():
    """
    Devuelve (device, compute_type) según hardware disponible.
    - GPU NVIDIA con CUDA y DLLs runtime → cuda/float16 (~5× más rápido)
    - Resto → cpu/int8 (2× más rápido que float32, mitad RAM)
    """
    try:
        from ctranslate2 import get_cuda_device_count
        if get_cuda_device_count() > 0 and _cuda_runtime_disponible():
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def cargar_modelo_whisper(preferido=None, log=None):
    """
    Carga el modelo faster-whisper más grande que quepa en memoria.
    Empieza por `preferido` (o MODELO_WHISPER) y degrada a base/tiny si OOM.
    Auto-detecta GPU NVIDIA si está disponible; en su defecto, CPU int8.

    `log` es un callable opcional log(nivel, msg) — la GUI lo usa para mostrar
    qué modelo se cargó. Si es None, se imprime en consola.

    Devuelve (modelo, nombre_modelo_cargado).
    Lanza RuntimeError con todos los errores acumulados si ninguno carga.
    """
    from faster_whisper import WhisperModel

    def _say(nivel, msg):
        if log is not None:
            log(nivel, msg)
        else:
            try:
                print(f"  {'⚠' if nivel == 'aviso' else '✓'} {msg}")
            except (UnicodeEncodeError, OSError, ValueError):
                # stdout cp1252 o cerrado: informar nunca debe tumbar la carga
                # del modelo (el UnicodeEncodeError del "✓" se confundía con
                # un fallo de Whisper y degradaba small→base→tiny→error).
                pass

    pref = preferido or MODELO_WHISPER
    orden = [pref] + [m for m in MODELOS_WHISPER_FALLBACK if m != pref]

    # Probar primero GPU; si falla por falta de cuDNN/driver, degradar a CPU.
    dispositivos = [_detectar_dispositivo_whisper()]
    if dispositivos[0][0] == "cuda":
        dispositivos.append(("cpu", "int8"))

    errores = []
    for device, compute_type in dispositivos:
        for nombre in orden:
            try:
                modelo = WhisperModel(nombre, device=device, compute_type=compute_type)
                etiqueta = f"{nombre} ({device}/{compute_type})"
                if nombre != pref:
                    _say("aviso", f"Modelo '{pref}' no cargó; usando '{etiqueta}'")
                else:
                    _say("ok", f"Modelo Whisper '{etiqueta}' cargado")
                return modelo, nombre
            except Exception as e:
                errores.append(f"{nombre} en {device}: {type(e).__name__}: {e}")
                continue
        if device == "cuda":
            _say("aviso", "GPU no utilizable — usando CPU")

    raise RuntimeError(
        "No se pudo cargar NINGÚN modelo Whisper. Revisa:\n"
        "  • Conexión a internet (primera vez se descarga ~500 MB)\n"
        "  • Espacio libre en disco\n"
        "  • Memoria RAM disponible\n"
        "Detalles:\n  " + "\n  ".join(errores)
    )


# ========== UTILIDADES ==========
def print_header(texto):
    print(f"\n{'='*60}\n  {texto}\n{'='*60}")


def comprobar_dependencias():
    print_header("Comprobando dependencias")
    if not shutil.which("ffmpeg"):
        print("❌ FFmpeg no encontrado.")
        print("   Descárgalo de: https://www.gyan.dev/ffmpeg/builds/")
        return False
    print("✓ FFmpeg encontrado")
    if not shutil.which("ffprobe"):
        print("❌ FFprobe no encontrado (suele venir junto con FFmpeg).")
        print("   Asegúrate de que C:\\ffmpeg\\bin está en el PATH.")
        return False
    print("✓ FFprobe encontrado")
    try:
        import faster_whisper  # noqa: F401
        print("✓ faster-whisper encontrado")
    except ImportError:
        print("❌ faster-whisper no instalado. Ejecuta: pip install faster-whisper")
        return False
    if not LOGO_PATH.exists():
        print(f"❌ Logo no encontrado en {LOGO_PATH}")
        return False
    print("✓ Logo encontrado")
    return True


def extraer_audio(video_path, audio_path):
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(audio_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg falló al extraer audio:\n{(result.stderr or '')[-500:]}"
        )


def obtener_duracion(video_path):
    cmd = [
        "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, creationflags=SUBPROCESS_FLAGS)
    return float(result.stdout.strip())


# initial_prompt sesga a Whisper hacia el vocabulario esperado en la tirolina.
# Reduce errores tipo "3, 2, 1" → "32 mil" o "qué tal" → "queta".
_WHISPER_PROMPT = (
    "Número catorce, número catorce. Tres, dos, uno. ¡Buen vuelo! "
    "Disfruta del vuelo. Allá vamos. "
    "¿Qué tal? ¿Cómo estás? ¡Bienvenido! ¡Madre mía! Sobrevivimos."
)


def transcribir_audio(audio_path, model, on_segment=None):
    """
    Transcribe el audio y devuelve {"segments": [{"start","end","text"}, ...]}.

    `on_segment` (opcional): callable(pct:int, texto:str) que se llama al cerrar
    cada segmento — la GUI lo usa para mostrar progreso de transcripción.
    """
    segments_gen, info = model.transcribe(
        str(audio_path),
        language="es",
        temperature=0,
        initial_prompt=_WHISPER_PROMPT,
        # Evita que alucinaciones contaminen los siguientes segmentos
        # (típico cuando hay viento sostenido).
        condition_on_previous_text=False,
        # VAD descarta silencio puro: 20-40 % menos tiempo en clips con largos
        # tramos de viento sin voz, sin perder ninguna frase.
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
    )

    duracion = max(0.01, float(info.duration))
    segments = []
    for s in segments_gen:
        segments.append({
            "start": float(s.start),
            "end": float(s.end),
            "text": s.text,
        })
        if on_segment is not None:
            pct = min(100, int(s.end / duracion * 100))
            on_segment(pct, s.text)
    return {"segments": segments}


def buscar_fin(transcripcion, duracion):
    """
    Busca la PRIMERA señal de llegada en el último 30% del vídeo.
    Buscar la primera (no la última) evita extender el corte con conversación
    post-llegada (saludos repetidos al equipo de tierra, reacciones, etc.).
    """
    limite_fin = duracion * 0.70
    for seg in transcripcion["segments"]:
        if seg["start"] < limite_fin:
            continue
        texto = seg["text"].lower().strip()
        for palabra in PALABRAS_FIN:
            if palabra in texto:
                return seg["start"], seg["text"]
    return None, None


def _leer_audio_float32(audio_path):
    """
    Lee un WAV PCM como numpy float32 normalizado a [-1, 1].
    Devuelve (y, sample_rate) o (None, None) si no se pudo leer.

    Excepciones esperadas (formato/IO) se silencian — el caller decide cómo
    manejar el fallback. Errores graves (MemoryError, KeyboardInterrupt)
    propagan a propósito para no enmascarar problemas serios.
    """
    import numpy as np
    try:
        with wave.open(str(audio_path), "rb") as wf:
            sr = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
    except (wave.Error, OSError, EOFError):
        return None, None
    y = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return y, sr


@contextmanager
def audio_temp_file():
    """
    Context manager que crea un WAV temporal con nombre único y garantiza
    su borrado al salir del bloque (incluso si hay excepción).

    Reemplaza al patrón anterior basado en un singleton de proceso, que
    asumía un único uso simultáneo. Con este context manager cada llamada
    es independiente — más simple de razonar, sin estado oculto.
    """
    fd, name = tempfile.mkstemp(prefix="sunview_audio_", suffix=".wav")
    os.close(fd)
    path = Path(name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


# ─── Helpers de UI: frames, audio snippets, clips de preview ───
# Estos helpers los consume la GUI de revisión semi-manual. Conviven aquí
# (no en gui_tirolina.py) porque son envoltorios sobre ffmpeg/numpy y deben
# ser invocables también desde scripts/tests sin tocar tkinter.

def extraer_frame(video_path, t_s, out_path, ancho=320):
    """Extrae un único frame del vídeo en t_s segundos como JPG escalado.

    Usa `-ss` ANTES del input para seek rápido. Calidad q:v 3 = balance
    entre tamaño y nitidez para miniaturas (no necesitamos prensa).
    Devuelve True si el archivo se creó, False si ffmpeg falló.
    """
    t_s = max(0.0, float(t_s))
    cmd = [
        "ffmpeg", "-y", "-ss", f"{t_s:.2f}", "-i", str(video_path),
        "-vframes", "1", "-q:v", "3",
        "-vf", f"scale={int(ancho)}:-2",
        str(out_path),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS,
    )
    return result.returncode == 0 and Path(out_path).exists()


def extraer_3_frames(video_path, t_inicio_s, out_dir, ancho=240, gap_s=2.0):
    """Storyboard de 3 frames alrededor de t_inicio_s: t-gap, t, t+gap.

    Útil para que el revisor "vea movimiento" sin reproducir vídeo. Si la
    extracción de algún frame falla, se devuelve None en esa posición.
    Devuelve dict {"antes": Path|None, "inicio": Path|None, "despues": Path|None}.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(video_path).stem
    resultados = {}
    for etiqueta, offset in [("antes", -gap_s), ("inicio", 0.0), ("despues", gap_s)]:
        out_path = out_dir / f"{stem}_frame_{etiqueta}.jpg"
        ok = extraer_frame(video_path, t_inicio_s + offset, out_path, ancho=ancho)
        resultados[etiqueta] = out_path if ok else None
    return resultados


def extraer_frames_grid(video_path, out_dir, ancho=240, paso_s=1.0):
    """Pre-extrae una rejilla de frames de todo el vídeo (un frame cada `paso_s`).

    Permite a la GUI refrescar el storyboard al instante cuando el revisor mueve
    t0/t1, sin nuevas llamadas a ffmpeg. Una sola pasada con `fps=1/paso_s` es
    mucho más rápida que N seeks independientes.

    Devuelve dict {segundo_int: Path} con todos los frames disponibles. Si la
    extracción falla, devuelve {} (la GUI debe degradar a 3 frames sueltos).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fps = 1.0 / max(0.1, float(paso_s))
    patron = str(out_dir / "grid_%05d.jpg")
    # `-hwaccel auto` deja a ffmpeg escoger DXVA2/D3D11/CUDA según GPU; en
    # GoPro H.264 reduce la decodificación ~4x. Si no hay GPU disponible
    # ffmpeg degrada a CPU sin fallar.
    cmd = [
        "ffmpeg", "-y", "-hwaccel", "auto", "-i", str(video_path),
        "-vf", f"fps={fps},scale={int(ancho)}:-2",
        "-q:v", "4",
        patron,
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS,
    )
    if result.returncode != 0:
        # Reintento sin hwaccel por si el "auto" elegido no soporta el codec
        cmd_cpu = [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vf", f"fps={fps},scale={int(ancho)}:-2",
            "-q:v", "4",
            patron,
        ]
        result = subprocess.run(
            cmd_cpu, capture_output=True, text=True,
            creationflags=SUBPROCESS_FLAGS,
        )
        if result.returncode != 0:
            return {}
    # ffmpeg numera 00001, 00002, ...; el frame N corresponde al segundo
    # (N-1) * paso_s del vídeo (frame 1 = t=0, frame 2 = t=paso_s, ...).
    grid = {}
    for path in sorted(out_dir.glob("grid_*.jpg")):
        try:
            idx = int(path.stem.split("_")[1])
        except (ValueError, IndexError):
            continue
        t = (idx - 1) * float(paso_s)
        grid[int(round(t))] = path
    return grid


def frame_mas_cercano(grid, t_s):
    """Devuelve el Path del frame de la rejilla más cercano a t_s, o None.

    La rejilla puede tener huecos al final (si ffmpeg corta antes); buscamos
    el segundo entero más cercano disponible para no devolver None por 0.4s.
    """
    if not grid:
        return None
    t_int = int(round(float(t_s)))
    if t_int in grid:
        return grid[t_int]
    # Búsqueda del entero más cercano disponible
    claves = sorted(grid.keys())
    mejor = min(claves, key=lambda k: abs(k - t_int))
    return grid[mejor]


def extraer_audio_snippet(audio_path, t0_s, duracion_s, out_path):
    """Recorta un trozo del WAV de audio temporal para reproducirlo.

    Útil para preview rápido en la GUI (espacio = reproducir 6 s del corte).
    Asume `audio_path` es WAV mono 16 kHz (lo que produce `extraer_audio`).
    """
    cmd = [
        "ffmpeg", "-y", "-ss", f"{max(0.0, t0_s):.2f}",
        "-i", str(audio_path),
        "-t", f"{max(0.1, duracion_s):.2f}",
        "-c:a", "pcm_s16le",
        str(out_path),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS,
    )
    return result.returncode == 0 and Path(out_path).exists()


def extraer_clip_preview(video_path, t0_s, duracion_s, out_path):
    """Genera un MP4 corto (t0 → t0+duracion) para reproducir externamente.

    Copy-stream sin recodificar: rápido y conserva calidad. Usado por la
    tecla "V" / botón "Ver vídeo" → os.startfile(out_path) abre con el
    reproductor por defecto de Windows.
    """
    cmd = [
        "ffmpeg", "-y", "-ss", f"{max(0.0, t0_s):.2f}",
        "-i", str(video_path),
        "-t", f"{max(0.5, duracion_s):.2f}",
        "-c", "copy",
        str(out_path),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS,
    )
    return result.returncode == 0 and Path(out_path).exists()


def envolvente_rms(audio_path, n_puntos=800):
    """Devuelve (rms_array, sr_efectivo) downsampleado a n_puntos.

    Para visualizar la onda en matplotlib sin enviar 1M de muestras. El
    eje X efectivo es len(rms_array) y cada bin representa
    `duracion_audio / n_puntos` segundos.
    """
    import numpy as np
    y, sr = _leer_audio_float32(audio_path)
    if y is None or len(y) == 0:
        return np.zeros(n_puntos, dtype=np.float32), 0
    bin_size = max(1, len(y) // n_puntos)
    n_efectivo = len(y) // bin_size
    truncado = y[:n_efectivo * bin_size]
    rms = np.sqrt(np.mean(truncado.reshape(n_efectivo, bin_size) ** 2, axis=1))
    return rms.astype(np.float32), sr


def _bloques_de_viento(audio_path):
    """
    Analiza el audio y devuelve [(t_ini, t_fin, energía_media), ...] con todos
    los bloques de viento sostenido. La energía permite distinguir vuelo real
    (viento fuerte) de ruido ambiente sostenido (CV bajo pero RMS medio).
    Lista vacía si no se pudo leer o no hay viento.
    """
    import numpy as np

    y, sr = _leer_audio_float32(audio_path)
    if y is None:
        return []

    HOP = int(sr * 0.25)
    WIN = int(sr * 0.50)
    n   = max(1, (len(y) - WIN) // HOP)

    rms = np.array([
        np.sqrt(np.mean(y[i*HOP : i*HOP + WIN] ** 2))
        for i in range(n)
    ])
    k = min(7, n)
    rms_s = np.convolve(rms, np.ones(k) / k, mode='same')

    CV_WIN = 8
    cv = np.array([
        rms_s[max(0, i - CV_WIN) : i + CV_WIN + 1].std()
        / (rms_s[max(0, i - CV_WIN) : i + CV_WIN + 1].mean() + 1e-8)
        for i in range(n)
    ])

    # Viento = energía por encima del suelo Y baja variabilidad.
    # 30 % del pico: el viento real de vuelo es el sonido MÁS FUERTE del clip.
    # Cualquier cosa por debajo (voces, ventilación, ambiente) se descarta.
    noise_floor = np.max(rms_s) * 0.30
    wind = (rms_s > noise_floor) & (cv < 0.50)

    # Gap fill de 2 s: rellena gritos puntuales del pasajero pero mantiene
    # separado el pre-vuelo del vuelo real (antes 5 s los fusionaba).
    GAP = 8
    wind_s = np.array([
        wind[max(0, i - GAP) : min(n, i + GAP + 1)].mean() >= 0.5
        for i in range(n)
    ])

    runs = []
    in_run, start_run = False, 0
    for i, w in enumerate(wind_s):
        if w and not in_run:
            in_run, start_run = True, i
        elif not w and in_run:
            in_run = False
            runs.append((start_run, i))
    if in_run:
        runs.append((start_run, n))

    # Convertir a segundos + energía media. Filtrar bloques <10 s (no son vuelo).
    bloques = []
    for a, b in runs:
        dur = (b - a) * HOP / sr
        if dur < 10:
            continue
        mean_rms = float(rms_s[a:b].mean())
        bloques.append((a * HOP / sr, b * HOP / sr, mean_rms))
    return bloques


def _bloque_principal(bloques):
    """
    Elige el bloque de vuelo real por ENERGÍA (mean_rms × duración).
    Pre-vuelo con ruido sostenido puede durar más que el vuelo real, pero
    el vuelo tiene más energía. Devuelve (t_ini, t_fin) o None.
    """
    if not bloques:
        return None
    mejor = max(bloques, key=lambda b: b[2] * (b[1] - b[0]))
    return mejor[0], mejor[1]


# Muro de viento: parámetros calibrados sobre clips reales de Sunview.
# - N_STD=2.0: el RMS del lanzamiento típicamente supera el baseline (charla en
#   plataforma) en >5 desviaciones; 2.0 deja margen para clips con viento ambiente.
# - SOSTENIDO_S=3: las ráfagas puntuales o gritos del monitor duran <1 s; el
#   viento del vuelo es sostenido. 3 s descarta picos espurios sin perder
#   onsets reales.
# - UMBRAL_RMS_MIN=0.01 (~-40 dBFS): suelo absoluto. Sin esto, en clips muy
#   silenciosos donde baseline≈0 el umbral cae a 0 y cualquier ruido dispara.
# - BASELINE_VENTANA_S=5: ventana FIJA del baseline (antes era 20 % del clip,
#   que para 60-180 s significaba 12-36 s — demasiado largo y solapaba con el
#   vuelo en clips con lanzamiento temprano).
# - BUSQUEDA_DESDE_S=1: empezamos a buscar el muro desde el segundo 1.
#   Antes saltábamos toda la ventana del baseline → si el lanzamiento era
#   en los primeros segundos NO se detectaba. Ahora sólo evitamos el primer
#   segundo (la GoPro aún se está estabilizando).
MURO_VIENTO_N_STD = 2.0
MURO_VIENTO_SOSTENIDO_S = 3.0
MURO_VIENTO_UMBRAL_MIN = 0.01
MURO_VIENTO_BASELINE_VENTANA_S = 5.0
MURO_VIENTO_BUSQUEDA_DESDE_S = 1.0

def _detectar_onset_lanzamiento(
    audio_path,
    hasta_s,
    n_std=MURO_VIENTO_N_STD,
    sostenido_s=MURO_VIENTO_SOSTENIDO_S,
):
    """
    Detecta el "muro de viento" del lanzamiento: primer instante en que el RMS
    sube por encima del ruido base de la plataforma y se mantiene allí durante
    ≥`sostenido_s`. Esta es la señal física más directa del salto.

    Estrategia (robusta a saturación, viento ambiente y lanzamientos tempranos):
      - Baseline: percentil 20 del RMS sobre los primeros 5 segundos del audio
        (`MURO_VIENTO_BASELINE_VENTANA_S`). El p20 (en lugar de la mediana) es
        el truco clave para tolerar lanzamientos QUE EMPIEZAN dentro de la
        ventana del baseline: aunque el 50 % de esos 5 s ya sea viento, el p20
        sigue capturando el suelo de ruido — la mediana se contaminaría.
      - Umbral = max(0.01, baseline + n_std * std(RMS_primer_5s)).
      - Búsqueda: primer frame en [1 s, hasta_s] tal que él y los siguientes
        `sostenido_s` segundos están TODOS por encima del umbral. Empezando en
        el segundo 1 (no después del baseline) detectamos lanzamientos tempranos
        — críticos si la GoPro se enciende justo antes del salto.
      - Saturación (clipping) del micro GoPro: el RMS de una señal clippeada
        sigue siendo muy alto y plano (amplitud acotada, no anulada) → cae
        cómodamente por encima del umbral. El detector no se ve afectado por
        clipping, sólo por wind-filter de la cámara (ver nota más abajo).

    `hasta_s` acota la búsqueda al primer 60 % del clip (lo pasa el caller).
    Sin esto el detector podría dispararse con un pico DENTRO del vuelo.

    Devuelve dict:
      t_inicio: float | None   — segundos del muro de viento (None si no se halló)
      baseline: float          — RMS p20 del primer `BASELINE_VENTANA_S`
      umbral:   float          — baseline + n_std * std (con suelo)
      pico:     float          — RMS máximo encontrado en [0, hasta_s]
      motivo:   str | None     — explicación cuando t_inicio es None

    Es solo una sugerencia para la revisión humana. No intenta resolver el
    sesgo del fin-de-rampa (el muro detecta cuando el rider ya tiene
    velocidad, no el salto exacto). El usuario corrige a click en la GUI.

    NOTA SENSIBILIDAD: si en el futuro las GoPro tienen el "Wind Filter"
    activado, el pico de RMS baja (la cámara atenúa la banda <500 Hz que
    contiene la mayor parte del muro de viento). Síntomas: el detector
    devuelve None con `pico` apenas por encima del umbral. Ajustes (los
    valores por defecto actuales ya son N_STD=2.0, SOSTENIDO=3.0):
      - bajar MURO_VIENTO_N_STD a 1.5 (menos exigente)
      - bajar MURO_VIENTO_SOSTENIDO_S a 2.0
      - en último recurso bajar MURO_VIENTO_UMBRAL_MIN a 0.005
    """
    import numpy as np

    res = {
        "t_inicio": None, "baseline": 0.0, "umbral": 0.0,
        "pico": 0.0, "motivo": None,
    }

    y, sr = _leer_audio_float32(audio_path)
    if y is None:
        res["motivo"] = "no se pudo leer el audio"
        return res

    HOP = int(sr * 0.25)
    WIN = int(sr * 0.50)
    n = max(1, (len(y) - WIN) // HOP)
    if n < 20:
        res["motivo"] = f"audio demasiado corto ({n} frames)"
        return res

    rms = np.array([
        np.sqrt(np.mean(y[i * HOP : i * HOP + WIN] ** 2))
        for i in range(n)
    ])

    frame_por_s = HOP / sr  # 0.25 s

    # Ventana del baseline = primeros 5 s. Mín. 5 frames para clips muy cortos
    # (el detector no es fiable allí, pero al menos no peta).
    n_baseline = min(n, max(5, int(round(MURO_VIENTO_BASELINE_VENTANA_S / frame_por_s))))
    rms_base = rms[:n_baseline]

    # p20 (no mediana): si el lanzamiento entra dentro de los 5 s del baseline,
    # la mediana se contaminaría con el viento. p20 conserva el suelo de ruido
    # mientras haya al menos un 20 % de muestras silenciosas en la ventana.
    baseline = float(np.percentile(rms_base, 20))

    # std SOLO del cuartil inferior. Si el lanzamiento entra a mitad de la
    # ventana baseline, la std completa explota (viento + silencio mezclados)
    # y el umbral queda por encima del pico → no se detecta nada. El cuartil
    # inferior aproxima la variabilidad del silencio puro, asumiendo que al
    # menos ~1 s de los 5 s de baseline esté libre de viento.
    n_cuartil = max(2, len(rms_base) // 4)
    cuartil_bajo = np.sort(rms_base)[:n_cuartil]
    std_base = float(cuartil_bajo.std())
    umbral = max(MURO_VIENTO_UMBRAL_MIN, baseline + n_std * std_base)
    res["baseline"], res["umbral"] = baseline, umbral

    idx_max = min(n, int(round(hasta_s / frame_por_s)))
    # Búsqueda desde el segundo 1 (no después del baseline). Si la ventana
    # de búsqueda es ridícula no podemos decidir nada.
    idx_min = int(round(MURO_VIENTO_BUSQUEDA_DESDE_S / frame_por_s))
    if idx_max <= idx_min:
        res["pico"] = float(rms[:idx_max].max()) if idx_max > 0 else 0.0
        res["motivo"] = f"ventana de búsqueda demasiado corta ({hasta_s:.0f}s)"
        return res

    pico_ventana = float(rms[:idx_max].max())
    res["pico"] = pico_ventana

    frames_sost = max(1, int(round(sostenido_s / frame_por_s)))

    for i in range(idx_min, idx_max - frames_sost + 1):
        if (rms[i : i + frames_sost] > umbral).all():
            res["t_inicio"] = i * frame_por_s
            return res

    res["motivo"] = (
        f"sin tramo sostenido > {umbral:.4f} durante {sostenido_s:.0f}s "
        f"en [{MURO_VIENTO_BUSQUEDA_DESDE_S:.0f}s, {hasta_s:.0f}s] "
        f"(pico observado {pico_ventana:.4f})"
    )
    return res


def detectar_vuelo_por_audio(audio_path, duracion):
    """
    Detecta inicio y fin del vuelo analizando el audio crudo, sin depender
    de lo que diga el monitor. El vuelo = bloque de viento con más ENERGÍA
    (no el más largo: pre-vuelo ambiente puede durar más pero ser más flojo).
    Funciona con cualquier idioma, monitor o parque.
    Devuelve (t_inicio, t_fin) en segundos, o (None, None).
    """
    bloques = _bloques_de_viento(audio_path)
    principal = _bloque_principal(bloques)
    if principal is None:
        return None, None
    return principal


def detectar_multiples_vuelos(audio_path, separacion_min=15):
    """
    Devuelve True si hay 2+ bloques de viento ≥10 s separados por
    ≥`separacion_min` s de no-viento. Indica que el clip contiene
    más de un vuelo y debe partirse manualmente.
    """
    bloques = _bloques_de_viento(audio_path)
    if len(bloques) < 2:
        return False
    bloques = sorted(bloques)
    for (_, fin1, _), (ini2, _, _) in zip(bloques, bloques[1:]):
        if ini2 - fin1 >= separacion_min:
            return True
    return False


def segundos_a_mmss(s):
    m = int(s) // 60
    seg = int(s) % 60
    return f"{m}:{seg:02d}"


def confirmar_o_ajustar(inicio, fin, duracion, video_path):
    print(f"\n  Duración total del clip:  {segundos_a_mmss(duracion)} ({duracion:.0f}s)")
    print(f"  ✂ Inicio del corte:       {segundos_a_mmss(inicio)} ({inicio:.0f}s)")
    print(f"  ✂ Fin del corte:          {segundos_a_mmss(fin)} ({fin:.0f}s)")
    print(f"  ▶ Duración vídeo final:   {segundos_a_mmss(fin - inicio)} ({fin - inicio:.0f}s)")

    while True:
        resp = input("\n  ¿Aceptar? [s = sí / a = ajustar segundos / r = ver vídeo] ").strip().lower()

        if resp in ("s", "si", "sí", ""):
            return inicio, fin

        elif resp == "a":
            try:
                nuevo_inicio = input(f"  Nuevo inicio en segundos (actual {inicio:.0f}): ").strip()
                if nuevo_inicio:
                    inicio = float(nuevo_inicio)
                nuevo_fin = input(f"  Nuevo fin en segundos (actual {fin:.0f}): ").strip()
                if nuevo_fin:
                    fin = float(nuevo_fin)
                print(f"  → Corte actualizado: {segundos_a_mmss(inicio)} → {segundos_a_mmss(fin)} ({fin-inicio:.0f}s)")
            except ValueError:
                print("  ⚠ Valor no válido, introduce solo números")

        elif resp == "r":
            print(f"  Abriendo vídeo...")
            if sys.platform == "win32":
                os.startfile(str(video_path))
            elif sys.platform == "darwin":
                subprocess.run(["open", str(video_path)])
            else:
                subprocess.run(["xdg-open", str(video_path)])


# Cache mutable: una vez NVENC falla en runtime, no volver a intentarlo en la sesión.
# Protegido por lock — hoy la GUI procesa un vídeo por vez, pero si en el
# futuro se paraleliza varios writers a esta variable sin lock causarían
# races (doble probe de ffmpeg, o un fallo en GPU enmascarado por el éxito
# de otro hilo).
_NVENC_DISPONIBLE = None  # None = no probado, True/False = resultado
_nvenc_lock = threading.Lock()


def _tiene_nvenc():
    """True si ffmpeg lista h264_nvenc y no ha fallado previamente en esta sesión."""
    global _NVENC_DISPONIBLE
    with _nvenc_lock:
        if _NVENC_DISPONIBLE is not None:
            return _NVENC_DISPONIBLE
        try:
            out = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=10,
                creationflags=SUBPROCESS_FLAGS,
            ).stdout
            _NVENC_DISPONIBLE = "h264_nvenc" in out
        except Exception:
            _NVENC_DISPONIBLE = False
        return _NVENC_DISPONIBLE


def _marcar_nvenc_no_disponible():
    """Marca NVENC como no usable tras un fallo en runtime (driver/GPU/formato)."""
    global _NVENC_DISPONIBLE
    with _nvenc_lock:
        _NVENC_DISPONIBLE = False


def _construir_cmd_edicion(video_entrada, inicio, duracion_corte, video_salida, usar_gpu):
    """Construye el comando ffmpeg para recortar + logo + comprimir.

    Flags defensivos para GoPro:
    - `-fflags +genpts` regenera timestamps (PTS no monotónicos por GPMD).
    - `-avoid_negative_ts make_zero` evita timestamps negativos tras el seek.
    - `[0:v:0]` selecciona la primera pista de vídeo (ignora GPMD/timecode).
    - `force_divisible_by=2` en scale: H.264 exige dimensiones pares; sin esto
      cierto contenido GoPro produce 1920x1081 y libx264 lo rechaza.
    - Logo con `-2` (no `-1`): asegura altura par también en el overlay.
    - `setsar=1` y `format=yuv420p` para pixel format compatible universal.
    - NO usar -noautorotate: GoPro graba en orientación variable y los
      metadatos de rotación son legítimos; sin autorotate sale invertido.
    """
    cmd = [
        "ffmpeg", "-y",
        "-fflags", "+genpts",
        "-ss", str(inicio),
        "-i", str(video_entrada),
        "-i", str(LOGO_PATH),
        "-t", str(duracion_corte),
        "-avoid_negative_ts", "make_zero",
        "-filter_complex",
        f"[1:v]scale=iw*{LOGO_ESCALA}:-2[logo];"
        f"[0:v:0]scale={RESOLUCION}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={RESOLUCION}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[v];"
        f"[v][logo]overlay=W-w-{LOGO_MARGEN}:{LOGO_MARGEN},format=yuv420p[outv]",
        "-map", "[outv]",
        "-map", "0:a:0?",   # primera pista de audio (opcional con '?')
    ]
    if usar_gpu:
        # NVENC -cq ≈ libx264 -crf pero escala distinta: +4 da calidad equivalente.
        cmd += [
            "-c:v", "h264_nvenc", "-preset", "p5",
            "-cq", str(CRF + 4), "-b:v", "0",
        ]
    else:
        cmd += ["-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF)]
    cmd += [
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(video_salida),
    ]
    return cmd


def editar_video(video_entrada, inicio, fin, video_salida):
    """
    Edita el vídeo intentando primero NVENC (GPU) y cayendo a libx264 si falla.
    NVENC puede estar presente en ffmpeg pero fallar en runtime por driver
    viejo, GPU incompatible, o formato GoPro raro. El fallback es transparente.
    """
    duracion_corte = fin - inicio
    if duracion_corte <= 0:
        raise ValueError(
            f"duración de corte inválida (inicio={inicio:.1f}s, fin={fin:.1f}s)"
        )

    intentos = [True, False] if _tiene_nvenc() else [False]
    ultimo_stderr = ""
    ultimo_cmd = []

    for usar_gpu in intentos:
        cmd = _construir_cmd_edicion(
            video_entrada, inicio, duracion_corte, video_salida, usar_gpu
        )
        result = subprocess.run(cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        if result.returncode == 0:
            return
        ultimo_stderr = result.stderr or ""
        ultimo_cmd = cmd
        # Limpiar fichero parcial antes del siguiente intento.
        Path(video_salida).unlink(missing_ok=True)
        if usar_gpu:
            # NVENC falló: marcar como no disponible para los siguientes vídeos.
            _marcar_nvenc_no_disponible()

    # Volcar diagnóstico completo a un log junto al vídeo de salida.
    # Útil para enviar el log si hay que diagnosticar fallos puntuales.
    log_path = Path(video_salida).with_suffix(".ffmpeg_error.log")
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("Comando ejecutado:\n")
            f.write(" ".join(f'"{a}"' if " " in str(a) else str(a) for a in ultimo_cmd))
            f.write("\n\nSTDERR completo:\n")
            f.write(ultimo_stderr)
    except Exception:
        pass

    raise RuntimeError(
        f"ffmpeg falló al editar el vídeo (ver {log_path.name} para detalles):\n"
        f"{ultimo_stderr[-1500:]}"
    )


# ========== VERIFICACIÓN MULTI-AGENTE ==========
# Cada función devuelve (ok: bool, detalle: str) y es independiente.

def verificar_duracion(video_path):
    """Duración entre 20 y 180 segundos."""
    try:
        dur = obtener_duracion(video_path)
        if dur < 20:
            return False, f"muy corto ({dur:.0f}s, mínimo 20s)"
        if dur > 180:
            return False, f"muy largo ({dur:.0f}s, máximo 3 min)"
        return True, f"{dur:.0f}s"
    except Exception as e:
        return False, str(e)


def verificar_resolucion(video_path):
    """Resolución exactamente 1920×1080."""
    try:
        cmd = [
            "ffprobe", "-v", "quiet", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0", str(video_path),
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS).stdout.strip()
        w, h = (int(x) for x in out.split(","))
        if w == 1920 and h == 1080:
            return True, f"{w}×{h}"
        return False, f"esperado 1920×1080, obtenido {w}×{h}"
    except Exception as e:
        return False, str(e)


def verificar_audio_nivel(video_path):
    """Nivel de audio: no mudo (< −50 dBFS) ni saturado (max > −1 dBFS)."""
    import re
    try:
        cmd = ["ffmpeg", "-i", str(video_path), "-af", "volumedetect", "-f", "null", "-"]
        stderr = subprocess.run(cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS).stderr
        m_mean = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", stderr)
        m_max  = re.search(r"max_volume:\s*([-\d.]+)\s*dB", stderr)
        if not m_mean:
            return False, "no se pudo leer el nivel de audio"
        mean = float(m_mean.group(1))
        peak = float(m_max.group(1)) if m_max else mean
        if mean < -50:
            return False, f"audio mudo ({mean:.1f} dBFS)"
        if peak > -1:
            return False, f"audio saturado (pico {peak:.1f} dBFS)"
        return True, f"media {mean:.1f} dBFS, pico {peak:.1f} dBFS"
    except Exception as e:
        return False, str(e)


def _rms_cv_de_segmento(y, sr, start_s=0, end_s=None):
    """
    Calcula (rms_medio, coef_variacion) de un segmento [start_s, end_s] de
    un array numpy float32 ya cargado. No toca ffmpeg ni disco.
    """
    import numpy as np
    s = max(0, int(start_s * sr))
    e = int(end_s * sr) if end_s is not None else len(y)
    segmento = y[s:e]
    HOP, WIN = int(sr * 0.1), int(sr * 0.2)
    n = max(1, (len(segmento) - WIN) // HOP)
    frames = np.array([
        np.sqrt(np.mean(segmento[i*HOP : i*HOP + WIN] ** 2))
        for i in range(n)
    ])
    rms = float(frames.mean())
    cv = float(frames.std() / (rms + 1e-8))
    return rms, cv


def verificar_inicio_limpio(audio_y, sr):
    """
    Los primeros 3s no deben ser silencio.
    Silencio al inicio indica que el corte fue demasiado tarde
    o que el clip empieza con pantalla negra.
    """
    try:
        rms, _ = _rms_cv_de_segmento(audio_y, sr, end_s=3)
        if rms < 0.005:
            return False, f"inicio silencioso (RMS={rms:.4f}) — posible corte tarde"
        return True, f"RMS={rms:.3f}"
    except (ValueError, ZeroDivisionError) as e:
        return False, str(e)


def verificar_llegada_detectada(audio_y, sr):
    """
    Los últimos 5s deben tener voz (energía variable).
    Ruido constante = aún en vuelo. Silencio = corte antes de la llegada.

    Umbral CV 0.25 (no 0.30): en clips multi-vuelo el viento del siguiente
    rider puede solapar con el final y bajar la variabilidad sin que sea fallo.
    """
    try:
        dur = len(audio_y) / sr
        rms, cv = _rms_cv_de_segmento(audio_y, sr, start_s=max(0, dur - 5))
        if rms < 0.005:
            return False, "final silencioso — corte antes de la llegada"
        if cv < 0.25:
            return False, f"final con ruido constante (CV={cv:.2f}) — vuelo sin llegada capturada"
        return True, f"voz detectada (CV={cv:.2f})"
    except (ValueError, ZeroDivisionError) as e:
        return False, str(e)


def verificar_corte(video_path, *_args, **_kwargs):
    """
    Orquestador: ejecuta todos los verificadores y devuelve
    lista de (nombre, ok, detalle).

    Optimización: el audio del vídeo final se extrae una sola vez para los
    chequeos de inicio y llegada (antes eran 2 ffmpeg adicionales por vídeo).
    """
    video_path = Path(video_path)
    if not video_path.exists():
        return [("archivo", False, "no encontrado")]

    size_mb = video_path.stat().st_size / (1024 * 1024)
    if size_mb < 0.5:
        return [("archivo", False, f"muy pequeño ({size_mb:.1f} MB)")]

    # Extraer audio una vez para inicio + llegada (era 2 invocaciones ffmpeg).
    audio_y = audio_sr = None
    with audio_temp_file() as tmp:
        cmd = [
            "ffmpeg", "-y", "-i", str(video_path), "-vn",
            "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(tmp),
        ]
        try:
            subprocess.run(cmd, capture_output=True, check=True, creationflags=SUBPROCESS_FLAGS)
            audio_y, audio_sr = _leer_audio_float32(tmp)
        except subprocess.CalledProcessError:
            pass

    resultados = [
        ("duración",   *verificar_duracion(video_path)),
        ("resolución", *verificar_resolucion(video_path)),
        ("audio",      *verificar_audio_nivel(video_path)),
    ]
    if audio_y is not None and audio_sr is not None:
        resultados.append(("inicio",  *verificar_inicio_limpio(audio_y, audio_sr)))
        resultados.append(("llegada", *verificar_llegada_detectada(audio_y, audio_sr)))
    else:
        resultados.append(("inicio",  False, "no se pudo extraer audio para verificar"))
        resultados.append(("llegada", False, "no se pudo extraer audio para verificar"))
    return resultados


# ========== VALIDACIÓN PREVIA ==========
def _es_placeholder_onedrive(video_path):
    """
    Detecta archivos OneDrive "files on demand" (icono de nube): el archivo
    existe en el explorador con su tamaño completo, pero el contenido no
    está descargado. Cualquier intento de leer dispararía una descarga lenta
    o un fallo, según política de OneDrive.

    Windows marca estos archivos con FILE_ATTRIBUTE_OFFLINE o reparse points
    de tipo RECALL_*. Detectarlos por atributo es más fiable que por tamaño.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(video_path))
        if attrs == -1:  # INVALID_FILE_ATTRIBUTES
            return False
        FILE_ATTRIBUTE_OFFLINE              = 0x1000
        FILE_ATTRIBUTE_RECALL_ON_OPEN       = 0x40000
        FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
        return bool(attrs & (
            FILE_ATTRIBUTE_OFFLINE
            | FILE_ATTRIBUTE_RECALL_ON_OPEN
            | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        ))
    except Exception:
        return False


def validar_video(video_path):
    """
    Comprobaciones tempranas antes de procesar.
    Detecta errores comunes que de otro modo crashearían a mitad de proceso
    (sin audio, placeholder de OneDrive, fichero corrupto, ruta inválida).
    Devuelve (ok: bool, motivo: str).
    """
    video_path = Path(video_path)

    if not video_path.exists():
        return False, "no encontrado"

    # OneDrive: archivos "files on demand" no descargados.
    # Detectar por atributo de Windows ANTES de leer tamaño/llamar a ffprobe.
    if _es_placeholder_onedrive(video_path):
        return False, (
            "archivo en la nube sin descargar — "
            "abre OneDrive, click derecho → 'Mantener siempre en este dispositivo'"
        )

    try:
        size = video_path.stat().st_size
    except OSError as e:
        return False, f"no se puede leer: {e}"
    if size < 1024:
        return False, "archivo vacío o truncado"

    # ffprobe valida formato y presencia de pista de audio/vídeo.
    if not shutil.which("ffprobe"):
        return False, "ffprobe no instalado (instala FFmpeg)"

    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, timeout=30,
            creationflags=SUBPROCESS_FLAGS,
        )
    except subprocess.TimeoutExpired:
        return False, "ffprobe se colgó leyendo el archivo (posible corrupción o cloud-only)"
    except Exception as e:
        return False, f"error al validar: {e}"

    if result.returncode != 0:
        msg = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "formato no reconocido"
        return False, f"no es un vídeo válido ({msg[:120]})"

    # ffprobe CSV puede meter comas trailing en algunos streams (GoPro tiene
    # streams de timecode/metadatos que salen como "video," "data,"). Tomamos
    # solo el primer campo de cada línea, ignorando comas y espacios.
    codecs = []
    for linea in result.stdout.strip().splitlines():
        valor = linea.split(",")[0].strip().lower()
        if valor:
            codecs.append(valor)

    # Si ffprobe no devuelve NINGÚN stream pero el returncode es 0,
    # casi siempre es un placeholder cloud-only que no detectamos antes,
    # o un archivo recortado/corrupto.
    if not codecs:
        stderr_resumen = (result.stderr or "").strip().splitlines()
        pista = stderr_resumen[-1][:120] if stderr_resumen else "sin streams legibles"
        return False, f"archivo ilegible (¿OneDrive sin sincronizar?): {pista}"

    if "video" not in codecs:
        return False, f"no tiene pista de vídeo (codecs detectados: {', '.join(codecs)})"
    if "audio" not in codecs:
        return False, "sin pista de audio (cámara con micrófono apagado)"

    return True, "ok"


# Archivo temporal de audio: ver `audio_temp_file()` arriba — context manager
# que crea un WAV único por uso y garantiza el borrado al salir del bloque,
# incluso si hay excepción. Reemplaza al singleton anterior.


# ========== SUGERENCIAS DE CORTE PARA REVISIÓN HUMANA ==========
# resolver_corte ya no decide nada definitivo: produce sugerencias que el humano
# valida/ajusta en la GUI. Detectamos el muro como pista de inicio y reconciliamos
# Whisper+caída del viento para sugerir fin. Si el muro no se detecta, t_inicio
# sugerido = None y el humano lo marca a click en la onda.

TOL_FIN_VS_VIENTO_S    = 15   # frase de llegada >15 s tras el fin del viento = post-charla, no llegada
VENTANA_BUSQUEDA_LLEGADA_S = 20  # al recortar llegada tardía, buscar en los primeros 20 s tras el viento
TOL_FIN_DENTRO_VIENTO_S = 10  # frase de llegada >10 s antes del fin del viento = exclamación mid-flight


# ─── Detección del número de cliente (ORDEN) que dice el monitor ───
# El monitor anuncia "número X" en el lanzamiento; queda en el audio. Buscamos
# ese patrón en la transcripción para rellenar el Nº cliente automáticamente
# (el humano luego lo confirma). Cubre cifras ("número 3") y palabras en
# español ("tres", "trece", "veintitrés", "treinta y dos").

_NUM_UNIDADES = {
    "cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4,
    "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9,
}
_NUM_ESPECIALES = {
    "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14,
    "quince": 15, "dieciseis": 16, "diecisiete": 17, "dieciocho": 18,
    "diecinueve": 19, "veinte": 20, "veintiuno": 21, "veintidos": 22,
    "veintitres": 23, "veinticuatro": 24, "veinticinco": 25, "veintiseis": 26,
    "veintisiete": 27, "veintiocho": 28, "veintinueve": 29,
}
_NUM_DECENAS = {
    "treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60,
    "setenta": 70, "ochenta": 80, "noventa": 90,
}


def _quitar_acentos(texto):
    import unicodedata
    t = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in t if not unicodedata.combining(c))


def _palabras_a_numero(tokens):
    """Convierte una lista de tokens en español a un entero, o None.

    Maneja: '3', 'tres', 'trece', 'veintitres', 'treinta y dos'.
    Solo mira el comienzo de la lista (la primera expresión numérica).
    """
    if not tokens:
        return None
    t0 = tokens[0]
    if t0.isdigit():
        return int(t0)
    if t0 in _NUM_ESPECIALES:
        return _NUM_ESPECIALES[t0]
    if t0 in _NUM_DECENAS:
        base = _NUM_DECENAS[t0]
        # 'treinta y dos'
        if len(tokens) >= 3 and tokens[1] == "y" and tokens[2] in _NUM_UNIDADES:
            return base + _NUM_UNIDADES[tokens[2]]
        return base
    if t0 in _NUM_UNIDADES:
        return _NUM_UNIDADES[t0]
    return None


def detectar_numero_cliente(transcripcion, t_centro=None, ventana=None):
    """Busca 'número X' en la transcripción y devuelve el ORDEN detectado.

    El ancla es la palabra 'numero', lo que evita confundirlo con la cuenta
    atrás ('tres, dos, uno'). Devuelve dict {"numero", "texto", "start"} o None.

    Selección del candidato (calibrada con transcripciones reales):
      * El monitor anuncia el número al ENCENDER la cámara (primeros segundos
        del clip), mucho antes del lanzamiento (`t_centro`, que puede llegar
        60-140 s después). Por eso NO se filtra por distancia al lanzamiento
        — la versión anterior usaba una ventana de ±20 s y descartaba el
        anuncio en la mayoría de los clips.
      * Si hay varios anuncios (clips multi-vuelo: "número uno" del cliente
        anterior y "número dos" del que salta), el válido es el ÚLTIMO antes
        del lanzamiento. Si todos son posteriores, el más cercano.

    `ventana` se ignora (se mantiene por compatibilidad de firma).
    """
    import re

    segs = transcripcion.get("segments", []) if transcripcion else []
    candidatos = []  # (start, num, texto)
    for s in segs:
        texto_norm = _quitar_acentos(s["text"].lower())
        # Buscar 'numero' y tomar las palabras siguientes
        for m in re.finditer(r"\bnumero[s]?\b[:\s]*([\wáéíóú]+(?:\s+y\s+\w+)?)",
                             texto_norm):
            cola = m.group(1).replace(",", " ").split()
            num = _palabras_a_numero(cola)
            if num is not None:
                candidatos.append((s["start"], num, s["text"].strip()))
                break
    if not candidatos:
        return None

    if t_centro is not None:
        previos = [c for c in candidatos if c[0] <= t_centro]
        if previos:
            elegido = max(previos, key=lambda c: c[0])   # último antes del salto
        else:
            elegido = min(candidatos, key=lambda c: abs(c[0] - t_centro))
    else:
        elegido = candidatos[0]  # sin lanzamiento conocido: primer anuncio

    start, num, texto = elegido
    return {"numero": str(num), "texto": texto, "start": start}


def _primera_frase_llegada(segments, desde, hasta):
    """Primera frase de PALABRAS_FIN dentro del intervalo [desde, hasta], o None."""
    for s in segments:
        if s["start"] < desde:
            continue
        if s["start"] > hasta:
            break
        texto = s["text"].lower().strip()
        for palabra in PALABRAS_FIN:
            if palabra in texto:
                return s["start"], s["text"]
    return None


def sugerir_corte(transcripcion, duracion, audio_path):
    """Produce sugerencias de t_inicio/t_fin para revisión humana.

    NO decide nada definitivo. Detecta el muro como pista de inicio y
    reconcilia Whisper + caída del viento para sugerir fin. El humano
    valida o ajusta a click en la onda.

    Devuelve dict:
      t_inicio_raw, t_fin_raw  — segundos sin márgenes (None si no se detectó)
      texto_inicio, texto_fin  — origen del timestamp (para mostrar pista)
      logs                     — lista de (nivel, mensaje) para el caller
    """
    logs: list[tuple[str, str]] = []

    # ─── FIN sugerido (Whisper + audio) ───
    t_fin_raw, texto_fin = buscar_fin(transcripcion, duracion)

    bloques = _bloques_de_viento(audio_path)
    principal = _bloque_principal(bloques)
    v_ini, v_fin = principal if principal is not None else (None, None)

    if detectar_multiples_vuelos(audio_path):
        logs.append((
            "aviso",
            "Se detectaron 2 o más vuelos en este clip — sugerencia para el "
            "más energético. Ajústalo a mano si hay que partir."
        ))

    if v_fin is not None:
        if t_fin_raw is None:
            t_fin_raw, texto_fin = v_fin, "[audio]"
            logs.append(("ok", f"Fin por audio (caída del viento): {segundos_a_mmss(v_fin)}"))
        elif v_ini is not None and t_fin_raw < v_ini:
            logs.append(("aviso",
                f"Frase de llegada ({segundos_a_mmss(t_fin_raw)}) ANTES del vuelo "
                f"({segundos_a_mmss(v_ini)}) — falso positivo, usando audio"))
            t_fin_raw, texto_fin = v_fin, "[audio]"
        elif t_fin_raw < v_fin - TOL_FIN_DENTRO_VIENTO_S:
            logs.append(("aviso",
                f"Frase ({segundos_a_mmss(t_fin_raw)}) dentro del vuelo — exclamación "
                f"mid-flight, usando audio"))
            t_fin_raw, texto_fin = v_fin, "[audio]"
        elif t_fin_raw > v_fin + TOL_FIN_VS_VIENTO_S:
            mejor = _primera_frase_llegada(
                transcripcion["segments"], desde=v_fin - 5,
                hasta=v_fin + VENTANA_BUSQUEDA_LLEGADA_S,
            )
            if mejor is not None:
                logs.append(("aviso",
                    f"Llegada tardía ({segundos_a_mmss(t_fin_raw)}); "
                    f"usando primera frase tras el vuelo: {segundos_a_mmss(mejor[0])}"))
                t_fin_raw, texto_fin = mejor
            else:
                logs.append(("aviso",
                    f"Llegada tardía ({segundos_a_mmss(t_fin_raw)}); usando audio"))
                t_fin_raw, texto_fin = v_fin, "[audio]"

    # ─── INICIO sugerido (muro de viento) ───
    hasta_muro = duracion * 0.60
    if t_fin_raw is not None:
        hasta_muro = min(hasta_muro, max(10.0, t_fin_raw - 10))

    muro = _detectar_onset_lanzamiento(audio_path, hasta_s=hasta_muro)

    if muro["t_inicio"] is not None:
        t_inicio_raw = muro["t_inicio"]
        texto_inicio = "[muro viento]"
        logs.append((
            "ok",
            f"Inicio sugerido en {segundos_a_mmss(t_inicio_raw)} "
            f"(baseline={muro['baseline']:.3f}, umbral={muro['umbral']:.3f}, "
            f"pico={muro['pico']:.3f})"
        ))
    else:
        t_inicio_raw = None
        texto_inicio = None
        logs.append((
            "aviso",
            f"Inicio NO detectado por muro ({muro['motivo']}) — marca a click en la onda"
        ))

    return {
        "t_inicio_raw": t_inicio_raw,
        "t_fin_raw": t_fin_raw,
        "texto_inicio": texto_inicio,
        "texto_fin": texto_fin,
        "logs": logs,
    }


# Alias hacia atrás para compatibilidad con scripts externos eventuales.
resolver_corte = sugerir_corte


# ========== PROCESAMIENTO PRINCIPAL ==========
def procesar_video(video_path, model):
    print_header(f"Procesando: {video_path.name}")

    # Validación previa: detecta sin audio, placeholder OneDrive, corrupto, etc.
    ok_val, motivo = validar_video(video_path)
    if not ok_val:
        print(f"  ⚠ {motivo}")
        return False, motivo

    video_salida = CARPETA_SALIDA / f"{video_path.stem}_FINAL.mp4"

    # Temporal de audio local al procesamiento: se borra al salir del bloque
    # `with` (incluso si hay excepción), sin estado global compartido.
    with audio_temp_file() as audio_tmp:
        print("→ Extrayendo audio...")
        extraer_audio(video_path, audio_tmp)

        duracion = obtener_duracion(video_path)

        print("→ Transcribiendo audio con IA...")
        transcripcion = transcribir_audio(audio_tmp, model)

        # Guardar transcripción para debug
        transcript_path = CARPETA_SALIDA / f"{video_path.stem}_transcripcion.txt"
        with open(transcript_path, "w", encoding="utf-8") as f:
            for seg in transcripcion["segments"]:
                f.write(f"[{seg['start']:6.2f}s - {seg['end']:6.2f}s] {seg['text']}\n")

        # Sugerencia de corte (lógica compartida con el GUI)
        print("→ Buscando señal de salida y llegada y analizando audio...")
        corte = sugerir_corte(transcripcion, duracion, audio_tmp)
        for nivel, msg in corte["logs"]:
            prefijo = "  ⚠" if nivel == "aviso" else "  ✓"
            print(f"{prefijo} {msg}")

        t_inicio_raw = corte["t_inicio_raw"]
        t_fin_raw = corte["t_fin_raw"]

        # Si no hay ni inicio ni fin sugeridos, no hay nada que cortar.
        if t_inicio_raw is None and t_fin_raw is None:
            print("  ⚠ Sin inicio ni fin detectados — saltando")
            return False, "sin sugerencia de corte (revisar manualmente en la GUI)"

        # Aplicar márgenes (mismas constantes que antes).
        t_inicio = (
            max(0.0, t_inicio_raw - SEGUNDOS_ANTES_INICIO)
            if t_inicio_raw is not None else 0.0
        )
        t_fin = (
            min(duracion, t_fin_raw + SEGUNDOS_DESPUES_FIN)
            if t_fin_raw is not None else duracion
        )

        if t_inicio_raw is not None:
            print(f"  ✓ Inicio sugerido: '{corte['texto_inicio']}' → {segundos_a_mmss(t_inicio_raw)}")
        else:
            print("  ⚠ Inicio no sugerido, empezando desde 0s — revisar manualmente")
        if t_fin_raw is not None:
            print(f"  ✓ Fin sugerido: '{corte['texto_fin']}' → {segundos_a_mmss(t_fin_raw)}")
        else:
            print("  ⚠ Fin no sugerido, usando fin del clip — revisar manualmente")

        # Confirmar con el usuario (solo en modo manual)
        if not MODO_AUTO:
            t_inicio, t_fin = confirmar_o_ajustar(t_inicio, t_fin, duracion, video_path)
        else:
            print(f"\n  Corte automático (best-effort): {segundos_a_mmss(t_inicio)} → {segundos_a_mmss(t_fin)} ({t_fin - t_inicio:.0f}s)")

        # Editar — si falla a medias, borrar el MP4 parcial.
        print("→ Generando vídeo final (recorte + logo + compresión)...")
        try:
            editar_video(video_path, t_inicio, t_fin, video_salida)
        except Exception:
            video_salida.unlink(missing_ok=True)
            raise

        # Verificar resultado (multi-agente)
        print("→ Verificando resultado...")
        checks = verificar_corte(video_salida)
        for nombre, chk_ok, det in checks:
            print(f"  {'✓' if chk_ok else '⚠'} {nombre}: {det}")
        ok = all(chk_ok for _, chk_ok, _ in checks)
        detalle = " | ".join(f"{n}:{d}" for n, chk_ok, d in checks if not chk_ok) or "OK"

        return ok, detalle


def main():
    print_header("SUNVIEW PARK — Editor automático de tirolina")

    if not comprobar_dependencias():
        sys.exit(1)

    CARPETA_ENTRADA.mkdir(exist_ok=True)
    CARPETA_SALIDA.mkdir(exist_ok=True)

    videos = [
        f for f in CARPETA_ENTRADA.iterdir()
        if f.is_file() and f.suffix.lower() in EXTENSIONES
    ]

    if not videos:
        print(f"\n⚠ No hay vídeos en la carpeta 'entrada/'")
        print(f"   Copia los clips de la GoPro ahí y vuelve a ejecutar.")
        input("\n  Pulsa Enter para cerrar...")
        sys.exit(0)

    print(f"\n→ {len(videos)} vídeo(s) encontrado(s)")

    print(f"\n→ Cargando IA de transcripción...")
    print("  (la primera vez tarda un poco, luego es rápido)")
    try:
        model, _ = cargar_modelo_whisper()
    except RuntimeError as e:
        print(f"\n❌ {e}")
        input("\n  Pulsa Enter para cerrar...")
        sys.exit(1)

    resultados = []  # (nombre, ok, detalle)
    for i, video in enumerate(videos, 1):
        print(f"\n\n[{i}/{len(videos)}]", end=" ")
        try:
            ok, detalle = procesar_video(video, model)
            resultados.append((video.name, ok, detalle))
        except KeyboardInterrupt:
            print("\n\n⚠ Cancelado por el usuario")
            break
        except Exception as e:
            print(f"\n  ❌ Error: {e}")
            resultados.append((video.name, False, str(e)))

    print_header("INFORME FINAL")
    ok_count = sum(1 for _, ok, _ in resultados if ok)
    for nombre, ok, detalle in resultados:
        icono = "✓" if ok else "⚠"
        print(f"  {icono} {nombre}: {detalle}")
    print(f"\n  {ok_count}/{len(resultados)} vídeos correctos")
    if any(not ok for _, ok, _ in resultados):
        print("  Los vídeos marcados con ⚠ pueden necesitar revisión manual.")
    print(f"\n  Resultados en: {CARPETA_SALIDA.absolute()}")
    input("\n  Pulsa Enter para cerrar...")


if __name__ == "__main__":
    main()
