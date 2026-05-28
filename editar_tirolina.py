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

EXTENSIONES = (".mp4", ".mov", ".MP4", ".MOV", ".avi", ".AVI", ".mkv", ".MKV")

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
            print(f"  {'⚠' if nivel == 'aviso' else '✓'} {msg}")

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
    "Tres, dos, uno. ¡Buen vuelo! Disfruta del vuelo. Allá vamos. "
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
# - N_STD=3: el RMS del lanzamiento típicamente supera el baseline (charla en
#   plataforma) en >5 desviaciones; 3 deja margen para clips con viento ambiente.
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

# ─── Backtrack al inicio de la rampa de subida ───
# El muro detecta el final de la rampa (cuando RMS se establece sostenido
# sobre el umbral), no el inicio del salto. Hay una rampa de 2-7 s entre
# "rider se suelta" y "viento al máximo" porque el rider está acelerando.
# En plataforma silenciosa la rampa es corta; en plataforma con viento
# ambiente alto (baseline cerca del umbral), la rampa puede ser de 5-7 s.
# Sin este backtrack, el corte empieza con el rider ya tirado al aire.
#
# Algoritmo: desde el frame del muro, retroceder hasta encontrar un frame
# por debajo del nivel medio (baseline + 50 % del camino al umbral). Ese es
# el último punto en zona "baja" antes de la subida → buen proxy del salto.
# Si en MURO_RAMPA_MAX_S no se encuentra zona baja (plataforma muy ventosa
# sin transición visible), aplicar offset fijo conservador.
MURO_RAMPA_NIVEL = 0.5            # punto del camino baseline→umbral
MURO_RAMPA_MAX_S = 6.0            # backtrack máximo (más arriesga incluir charla)
MURO_RAMPA_OFFSET_FIJO_S = 3.0    # retroceso por defecto si no hay rampa visible


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
      t_inicio: float | None   — segundos del INICIO de la rampa (None si no se halló)
      t_muro:   float | None   — segundos del fin de la rampa (donde RMS se estabilizó sobre el umbral)
      origen_inicio: str       — "rampa 50%" (encontró cruce) o "offset fijo" (sin rampa visible)
      baseline: float          — RMS p20 del primer `BASELINE_VENTANA_S`
      umbral:   float          — baseline + n_std * std (con suelo)
      pico:     float          — RMS máximo encontrado en [0, hasta_s]
      motivo:   str | None     — explicación cuando t_inicio es None

    NOTA SENSIBILIDAD: si en el futuro las GoPro tienen el "Wind Filter"
    activado, el pico de RMS baja (la cámara atenúa la banda <500 Hz que
    contiene la mayor parte del muro de viento). Síntomas: el detector
    devuelve None con `pico` apenas por encima del umbral. Ajustes:
      - bajar MURO_VIENTO_N_STD a 2.0 (menos exigente)
      - bajar MURO_VIENTO_SOSTENIDO_S a 2.0
      - en último recurso bajar MURO_VIENTO_UMBRAL_MIN a 0.005
    """
    import numpy as np

    res = {
        "t_inicio": None, "t_muro": None, "origen_inicio": "",
        "baseline": 0.0, "umbral": 0.0,
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

    i_muro = None
    for i in range(idx_min, idx_max - frames_sost + 1):
        if (rms[i : i + frames_sost] > umbral).all():
            i_muro = i
            break

    if i_muro is None:
        res["motivo"] = (
            f"sin tramo sostenido > {umbral:.4f} durante {sostenido_s:.0f}s "
            f"en [{MURO_VIENTO_BUSQUEDA_DESDE_S:.0f}s, {hasta_s:.0f}s] "
            f"(pico observado {pico_ventana:.4f})"
        )
        return res

    res["t_muro"] = i_muro * frame_por_s

    # ─── Backtrack al inicio de la rampa ───
    # El muro detecta el fin de la rampa de subida del viento (rider ya con
    # velocidad). Para anclar el corte al INICIO del salto, retrocedemos
    # hasta el último frame por debajo del nivel medio (50 % del camino
    # baseline→umbral). Si no hay zona "baja" en MURO_RAMPA_MAX_S, aplicamos
    # offset fijo (plataforma sin transición visible).
    nivel_rampa = baseline + MURO_RAMPA_NIVEL * (umbral - baseline)
    frames_rampa_max = max(1, int(round(MURO_RAMPA_MAX_S / frame_por_s)))
    limite_atras = max(idx_min, i_muro - frames_rampa_max)

    i_rampa = None
    for j in range(i_muro - 1, limite_atras - 1, -1):
        if rms[j] <= nivel_rampa:
            i_rampa = j
            break

    if i_rampa is not None:
        res["t_inicio"] = i_rampa * frame_por_s
        res["origen_inicio"] = f"rampa 50% en {i_rampa * frame_por_s:.1f}s"
    else:
        # Sin zona baja → plataforma ventosa. Aplicar offset fijo conservador.
        frames_offset = max(1, int(round(MURO_RAMPA_OFFSET_FIJO_S / frame_por_s)))
        i_offset = max(idx_min, i_muro - frames_offset)
        res["t_inicio"] = i_offset * frame_por_s
        res["origen_inicio"] = (
            f"offset fijo {MURO_RAMPA_OFFSET_FIJO_S:.0f}s "
            f"(plataforma ventosa, sin rampa visible)"
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


# ========== RESOLUCIÓN DE CORTE COMPARTIDA ==========
# Esta función es la ÚNICA fuente de verdad para decidir t_inicio/t_fin.
# Tanto CLI (procesar_video) como GUI (gui_tirolina._worker) la usan.
# Cualquier cambio en la lógica de detección se aplica automáticamente a ambos flujos.

# ─── Historial dinámico de duraciones de vuelo ───
# Reemplaza a la antigua constante DURACION_VUELO_TIPICA_S=80. Se va aprendiendo
# de los vuelos que SÍ se detectaron bien (muro+fin físicos) y la mediana se usa
# como fallback cuando el muro de viento no se detecta. Datos por parque y
# tirolina concretos → mucho mejor que un número mágico.
HISTORIAL_VUELOS_FILE = _BASE / "historial_vuelos.json"
HISTORIAL_MAX_ENTRADAS = 50          # límite de ventana — descarta lo antiguo
HISTORIAL_MIN_ENTRADAS = 3           # bajo esto preferimos DEFECTO antes que mediana
DURACION_VUELO_DEFECTO_S = 60.0      # último recurso si el historial está vacío

# ─── Filtro de calidad antes de guardar al historial ───
# Una mala detección del muro (pico apenas por encima del umbral, plataforma con
# viento ambiente) puede colarse y polucionar la mediana del fallback. Estos
# umbrales discriminan onsets "confiados" de los marginales.
#
# Calibrados con los clips reales: los 3 fallos de la tirada (3.2/4.3/5.1) tenían
# ratio pico/umbral en 1.36-1.51 y margen 0.056-0.069, mientras los buenos
# estaban en ratio ≥1.73 y margen ≥0.075. El umbral 1.5/0.05 caza la mayoría
# de los marginales sin descartar los buenos.
HISTORIAL_PICO_UMBRAL_RATIO_MIN = 1.5
HISTORIAL_PICO_UMBRAL_MARGEN_MIN = 0.05
HISTORIAL_DURACION_MIN_S = 40        # vuelos de tirolina típicos: 50-90 s
HISTORIAL_DURACION_MAX_S = 120
HISTORIAL_OUTLIER_DELTA_S = 25       # rechazo si dista >25 s de la mediana actual

TOL_FIN_VS_VIENTO_S    = 15   # frase de llegada >15 s tras el fin del viento = post-charla, no llegada
VENTANA_BUSQUEDA_LLEGADA_S = 20  # al recortar llegada tardía, buscar en los primeros 20 s tras el viento
TOL_FIN_DENTRO_VIENTO_S = 10  # frase de llegada >10 s antes del fin del viento = exclamación mid-flight


def _cargar_historial_duraciones():
    """Lista de duraciones (segundos) de vuelos detectados con éxito.

    Devuelve [] si el archivo no existe o está corrupto — el caller debe estar
    preparado para historial vacío (en ese caso se usa DURACION_VUELO_DEFECTO_S).
    """
    try:
        with open(HISTORIAL_VUELOS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    duraciones = data.get("duraciones", [])
    return [float(d) for d in duraciones if isinstance(d, (int, float)) and d > 0]


def _chequear_confianza_onset(pico, umbral):
    """¿El par (pico, umbral) representa un onset confiable para APRENDIZAJE?

    Solo gate del guardado al historial: la confianza pura (ratio≥1.5 Y
    margen≥0.05) rechaza detecciones marginales que no merecen propagarse
    a la mediana de fallback. Para la DECISIÓN de corte usamos un criterio
    distinto (coherencia muro+t_fin en `resolver_corte`) porque rechazar por
    ratio crearía falsos negativos en plataformas con viento ambiente alto
    pero detección real correcta.

    Devuelve (confiable: bool, motivo: str). motivo es "" si pasa o una
    descripción legible si se rechaza.
    """
    if umbral <= 0:
        return False, "umbral inválido (≤0)"
    ratio = pico / umbral
    if ratio < HISTORIAL_PICO_UMBRAL_RATIO_MIN:
        return False, (
            f"onset marginal (pico/umbral={ratio:.2f} < "
            f"{HISTORIAL_PICO_UMBRAL_RATIO_MIN})"
        )
    margen = pico - umbral
    if margen < HISTORIAL_PICO_UMBRAL_MARGEN_MIN:
        return False, (
            f"onset marginal (pico−umbral={margen:.3f} < "
            f"{HISTORIAL_PICO_UMBRAL_MARGEN_MIN})"
        )
    return True, ""


def _guardar_duracion_vuelo(duracion_s, pico, umbral):
    """Añade `duracion_s` al historial si pasa el filtro de calidad.

    Solo se llama cuando inicio y fin se detectaron por señales FÍSICAS
    (muro de viento + fin del viento o frase de llegada) — nunca tras un
    fallback, para no autoalimentarse con valores derivados.

    Filtro de calidad (en orden):
      1) Duración en rango razonable (40-120 s, típico de tirolina).
      2) Confianza del onset (vía `_chequear_confianza_onset`): los marginales
         producen duraciones cortas que envenenan la mediana.
      3) Outlier vs mediana actual (sólo si hay ≥ HISTORIAL_MIN_ENTRADAS):
         duraciones que distan > 25 s de la mediana actual probablemente
         vienen de una mala detección.

    Devuelve (guardado: bool, motivo: str). El caller decide cómo loguear.
    """
    if not (HISTORIAL_DURACION_MIN_S <= duracion_s <= HISTORIAL_DURACION_MAX_S):
        return False, (
            f"fuera de rango [{HISTORIAL_DURACION_MIN_S}, "
            f"{HISTORIAL_DURACION_MAX_S}]s"
        )

    confiable, motivo = _chequear_confianza_onset(pico, umbral)
    if not confiable:
        return False, motivo

    duraciones = _cargar_historial_duraciones()
    if len(duraciones) >= HISTORIAL_MIN_ENTRADAS:
        from statistics import median
        med = median(duraciones)
        if abs(duracion_s - med) > HISTORIAL_OUTLIER_DELTA_S:
            return False, (
                f"outlier vs mediana actual ({med:.0f}s, "
                f"Δ={duracion_s - med:+.0f}s)"
            )

    duraciones.append(float(duracion_s))
    duraciones = duraciones[-HISTORIAL_MAX_ENTRADAS:]
    try:
        with open(HISTORIAL_VUELOS_FILE, "w", encoding="utf-8") as f:
            json.dump({"duraciones": duraciones}, f, ensure_ascii=False, indent=2)
    except OSError as e:
        # No es crítico: el sistema sigue funcionando con DURACION_VUELO_DEFECTO_S.
        return False, f"error de E/S al persistir ({e})"
    return True, "guardada"


def borrar_historial_duraciones():
    """Borra el archivo de historial. Idempotente: si no existe, no error.

    Pensado para limpieza manual cuando el usuario sospecha que el fallback
    está cortando mal (probablemente por duraciones contaminadas en el JSON).
    Devuelve True si tras la operación el archivo no existe.
    """
    try:
        HISTORIAL_VUELOS_FILE.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _duracion_vuelo_estimada():
    """Mediana del historial si hay ≥ HISTORIAL_MIN_ENTRADAS, si no DEFECTO.

    Devuelve (duracion_s, origen) — origen es "mediana(N=…)" o "defecto" para
    poder loguear de dónde salió el valor.
    """
    duraciones = _cargar_historial_duraciones()
    if len(duraciones) >= HISTORIAL_MIN_ENTRADAS:
        # Mediana sin numpy: import barato y evita cargar numpy si esta es la
        # única llamada del flujo (la GUI lo precarga, así que en práctica ya
        # está en memoria — pero por simplicidad usamos statistics).
        from statistics import median
        return median(duraciones), f"mediana(N={len(duraciones)})"
    return DURACION_VUELO_DEFECTO_S, "defecto"


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


def resolver_corte(transcripcion, duracion, audio_path):
    """
    Estrategia híbrida (FIN por Whisper / INICIO por física del audio):
      1) FIN — señal más fiable: frase de llegada del rider en el último 30 %
         reconciliada con la caída del viento ("¿qué tal?", "madre mía"...).
         Whisper sigue siendo apropiado aquí: el silencio relativo tras llegar
         a plataforma hace que la transcripción del fin sea muy estable.

      2) INICIO — muro de viento como fuente primaria. Cadena de fallbacks:
         a) Muro de viento COHERENTE (`_detectar_onset_lanzamiento`): primer
            instante en que el RMS sube sobre el ruido base y se mantiene
            ≥3 s. Coherente = duración resultante muro→t_fin cae en
            [40,120]s. Si no es coherente, uno de los dos anclajes miente y
            es mejor el fallback. (Si no hay t_fin se acepta el muro tal cual,
            mejor que nada.)
         b) `t_fin - mediana_historial`: si tenemos ≥3 vuelos detectados con
            éxito en el pasado, su mediana de duración como ancla. Tiene
            PRIORIDAD sobre v_ini porque combina señal física (t_fin) con
            estadística aprendida del parque concreto.
         c) `t_fin - DURACION_VUELO_DEFECTO_S` (60 s): mismo origen que (b)
            pero sin historial suficiente.
         d) `v_ini` (inicio del bloque de viento principal): umbral más laxo
            que el muro. Último recurso cuando no hay ni muro ni t_fin —
            raro, pero evita perder vídeos enteros.

      3) APRENDIZAJE: cuando inicio y fin se determinan por SEÑALES FÍSICAS
         (muro + fin del viento / frase reconciliada con audio), la duración
         se persiste en historial_vuelos.json. Esto adapta el fallback al
         parque y tirolina concretos (mucho mejor que un número mágico).

      4) Whisper NO se usa para detectar el INICIO. Los falsos positivos
         de la charla de plataforma ("vamos", "listo", "acércate", "número
         uno") son demasiado frecuentes y caen exactamente en la ventana
         que más daño hace (justo antes del salto).

    Devuelve dict:
      t_inicio, t_fin              — segundos absolutos con márgenes aplicados
      t_inicio_raw, t_fin_raw      — segundos sin márgenes (None si no detectado)
      texto_inicio, texto_fin      — origen del timestamp ("[muro viento]",
                                     "[inicio bloque viento]", "[fin − Ns]",
                                     "[inicio + Ns]", "[audio]", o frase de llegada)
      sin_vuelo                    — True si no se pudo detectar ningún fin
      fin_sintetizado              — True si t_fin se calculó por start-anclado
                                     (sin evidencia física del fin) → revisar manualmente
      logs                         — lista de (nivel, mensaje) para que el caller imprima
    """
    t_fin_raw, texto_fin = buscar_fin(transcripcion, duracion)

    bloques = _bloques_de_viento(audio_path)
    principal = _bloque_principal(bloques)
    if principal is not None:
        v_ini, v_fin = principal
    else:
        v_ini = v_fin = None

    logs: list[tuple[str, str]] = []

    if detectar_multiples_vuelos(audio_path):
        logs.append((
            "aviso",
            "Se detectaron 2 o más vuelos en este clip — "
            "se cortará solo el más energético. Si necesitas los otros, "
            "ajústalo a mano."
        ))

    # ─── FIN: la señal ancla ───
    # Prioridad: 1) frase de llegada cercana al fin del viento;
    #            2) fin del bloque de viento;
    #            3) frase de llegada aislada.
    if v_fin is not None:
        if t_fin_raw is None:
            t_fin_raw, texto_fin = v_fin, "[audio]"
            logs.append(("ok", f"Fin por audio (caída del viento): {segundos_a_mmss(v_fin)}"))
        elif v_ini is not None and t_fin_raw < v_ini:
            logs.append((
                "aviso",
                f"Frase de llegada ({segundos_a_mmss(t_fin_raw)}) cae ANTES del "
                f"vuelo ({segundos_a_mmss(v_ini)}) — falso positivo, usando audio"
            ))
            t_fin_raw, texto_fin = v_fin, "[audio]"
        elif t_fin_raw < v_fin - TOL_FIN_DENTRO_VIENTO_S:
            # La frase cae bien dentro del bloque de vuelo: típicamente una
            # exclamación del rider mid-flight ("¡madre mía!", "wow") que
            # Whisper transcribió como llegada. La llegada real es el fin del
            # viento (cuando deja de soplar al frenar).
            logs.append((
                "aviso",
                f"Frase de llegada ({segundos_a_mmss(t_fin_raw)}) cae dentro del "
                f"vuelo (Δ={v_fin - t_fin_raw:.0f}s antes del fin del viento) "
                f"— exclamación mid-flight, usando audio"
            ))
            t_fin_raw, texto_fin = v_fin, "[audio]"
        elif t_fin_raw > v_fin + TOL_FIN_VS_VIENTO_S:
            mejor = _primera_frase_llegada(
                transcripcion["segments"],
                desde=v_fin - 5,
                hasta=v_fin + VENTANA_BUSQUEDA_LLEGADA_S,
            )
            if mejor is not None:
                logs.append((
                    "aviso",
                    f"Frase de llegada muy tardía ({segundos_a_mmss(t_fin_raw)}); "
                    f"usando primera frase tras el vuelo: {segundos_a_mmss(mejor[0])}"
                ))
                t_fin_raw, texto_fin = mejor
            else:
                logs.append((
                    "aviso",
                    f"Frase de llegada muy tardía ({segundos_a_mmss(t_fin_raw)}); "
                    f"sin alternativa cerca del fin del viento — usando audio"
                ))
                t_fin_raw, texto_fin = v_fin, "[audio]"

    # ─── INICIO: cadena muro_confiable → fin-anclado → v_ini → None ───
    # Acotamos al primer 60 % del clip por defecto. Si ya tenemos t_fin_raw,
    # cerramos además 10 s antes del fin: el muro NO puede estar dentro del
    # vuelo (allí ya hay viento continuo, no transición).
    hasta_muro = duracion * 0.60
    if t_fin_raw is not None:
        hasta_muro = min(hasta_muro, max(10.0, t_fin_raw - 10))

    muro = _detectar_onset_lanzamiento(audio_path, hasta_s=hasta_muro)

    # ─── FIN SINTETIZADO (start-anclado) ───
    # Si tenemos muro pero no t_fin (ni Whisper ni caída del viento), el cut
    # iría hasta el final del clip → potencialmente 3+ minutos de viento sin
    # acción. Sintetizamos t_fin = t_muro + mediana_historial (o defecto).
    # Es el fin-anclado pero hacia delante: la mediana acota la duración al
    # rango típico del parque. Marcamos `fin_sintetizado=True` para que la
    # GUI lo destaque para revisión manual — no tenemos evidencia física del
    # fin, solo una estimación estadística.
    fin_sintetizado = False
    if muro["t_inicio"] is not None and t_fin_raw is None:
        duracion_est, origen = _duracion_vuelo_estimada()
        t_fin_raw = min(duracion, muro["t_inicio"] + duracion_est)
        texto_fin = f"[inicio + {duracion_est:.0f}s {origen}]"
        fin_sintetizado = True
        logs.append((
            "aviso",
            f"[DEBUG FIN] Sin llegada detectada (ni voz ni caída del viento) — "
            f"sintetizado en {segundos_a_mmss(t_fin_raw)} "
            f"(t_inicio + {duracion_est:.0f}s vía {origen}). "
            f"Revisar manualmente."
        ))

    # Gate del muro como ancla de corte: la confianza pura (ratio pico/umbral)
    # rechaza falsos negativos pero también detecciones reales en plataforma
    # ventosa. Mejor heurística: coherencia muro+t_fin. Si la duración
    # resultante cae en [40,120]s (rango típico de tirolina), los dos anclajes
    # se sostienen mutuamente. Si está fuera, uno de los dos miente → mejor
    # usar fin-anclado. Si no hay t_fin, no se puede chequear coherencia y
    # confiamos en el muro (mejor que nada). Nota: tras la síntesis anterior,
    # si llegamos aquí con t_fin_raw None es porque tampoco hay muro detectado.
    if muro["t_inicio"] is not None and t_fin_raw is not None:
        duracion_si_muro = t_fin_raw - muro["t_inicio"]
        muro_coherente = (
            HISTORIAL_DURACION_MIN_S <= duracion_si_muro <= HISTORIAL_DURACION_MAX_S
        )
        motivo_muro = (
            ""
            if muro_coherente
            else (
                f"duración resultante {duracion_si_muro:.0f}s fuera de rango "
                f"[{HISTORIAL_DURACION_MIN_S}, {HISTORIAL_DURACION_MAX_S}]s"
            )
        )
    elif muro["t_inicio"] is not None:
        muro_coherente = True
        motivo_muro = ""
    else:
        muro_coherente = False
        motivo_muro = muro["motivo"]

    # Sanity check para v_ini como último recurso: rechaza valores degenerados
    # (≈0 → viento desde el primer frame, normalmente viento ambiente
    # constante) y los que caen demasiado cerca del fin (no son inicio
    # de vuelo). Solo así v_ini es información útil.
    v_ini_util = (
        v_ini is not None
        and v_ini >= 1.0
        and (t_fin_raw is None or v_ini <= t_fin_raw - 10)
    )

    if muro_coherente:
        t_inicio_raw = muro["t_inicio"]
        texto_inicio = "[muro viento]"
        info_muro = (
            f"muro en {segundos_a_mmss(muro['t_muro'])} ({muro['origen_inicio']})"
            if muro.get("t_muro") is not None
            else ""
        )
        logs.append((
            "ok",
            f"[DEBUG INICIO] Onset de viento detectado en "
            f"{segundos_a_mmss(t_inicio_raw)} "
            f"({info_muro}, baseline={muro['baseline']:.4f}, "
            f"umbral={muro['umbral']:.4f}, pico={muro['pico']:.4f}, "
            f"ventana 0-{hasta_muro:.0f}s)"
        ))
    elif t_fin_raw is not None:
        # Fin-anclado: prioridad sobre v_ini. Si el muro no es coherente
        # (ya sea porque no se detectó o porque su duración resultante cae
        # fuera de [40,120]s), v_ini sobre la misma señal con criterio más
        # laxo (umbral 30 % del pico) probablemente esté midiendo viento
        # ambiente, no el lanzamiento. La mediana del historial aprendida
        # del parque concreto + t_fin físico es ancla más fiable.
        duracion_est, origen = _duracion_vuelo_estimada()
        t_inicio_raw = max(0, t_fin_raw - duracion_est)
        texto_inicio = f"[fin − {duracion_est:.0f}s {origen}]"
        logs.append((
            "aviso",
            f"[DEBUG INICIO] Fallback fin-anclado usado en "
            f"{segundos_a_mmss(t_inicio_raw)} "
            f"(t_fin − {duracion_est:.0f}s vía {origen}; "
            f"muro descartado: {motivo_muro}; "
            f"baseline={muro['baseline']:.4f}, umbral={muro['umbral']:.4f}, "
            f"pico={muro['pico']:.4f})"
        ))
    elif v_ini_util:
        # Último recurso: sin muro coherente Y sin t_fin para anclar. v_ini
        # es la peor señal (umbral laxo sobre viento ambiente posible), pero
        # mejor que nada — sin ella perderíamos el vídeo entero. Raro: requiere
        # que falle tanto el muro como Whisper+caída del viento del fin.
        t_inicio_raw = v_ini
        texto_inicio = "[inicio bloque viento]"
        logs.append((
            "aviso",
            f"[DEBUG INICIO] Sin muro coherente ({motivo_muro}) y sin "
            f"fin físico — último recurso: inicio del bloque de viento "
            f"en {segundos_a_mmss(v_ini)}"
        ))
    else:
        t_inicio_raw = None
        texto_inicio = None
        logs.append((
            "aviso",
            f"[DEBUG INICIO] Sin ancla de inicio "
            f"(muro: {motivo_muro}; sin fin; sin bloque de viento útil)"
        ))

    # ─── SIN VUELO ───
    # No hay fin Y no hay ningún inicio reconocible → vídeo de instrucciones,
    # prueba o subido por error.
    sin_vuelo = False
    if t_inicio_raw is None and t_fin_raw is None:
        sin_vuelo = True
    elif t_inicio_raw is not None and t_fin_raw is not None:
        if t_fin_raw - t_inicio_raw < 10:
            sin_vuelo = True

    # ─── APRENDIZAJE ───
    # Persistimos la duración SOLO cuando la pareja (inicio, fin) viene de
    # señales físicas Y el filtro de calidad la acepta. Si el inicio es
    # fallback (mediana o v_ini), guardar esa duración crearía un bucle de
    # retroalimentación — la mediana se estabilizaría en su propio valor.
    # El filtro de calidad además bloquea onsets marginales que producen
    # duraciones cortas envenenando la mediana (caso de los 3 fallos
    # observados con plataforma ventosa).
    if (
        not sin_vuelo
        and texto_inicio == "[muro viento]"
        and t_fin_raw is not None
        and t_inicio_raw is not None
    ):
        duracion_vuelo = t_fin_raw - t_inicio_raw
        guardado, motivo = _guardar_duracion_vuelo(
            duracion_vuelo, pico=muro["pico"], umbral=muro["umbral"]
        )
        if guardado:
            logs.append((
                "ok",
                f"[DEBUG HISTORIAL] Duración {duracion_vuelo:.0f}s guardada"
            ))
        else:
            logs.append((
                "aviso",
                f"[DEBUG HISTORIAL] Duración {duracion_vuelo:.0f}s descartada: "
                f"{motivo}"
            ))

    # ─── MÁRGENES ───
    if t_inicio_raw is not None:
        t_inicio = max(0, t_inicio_raw - SEGUNDOS_ANTES_INICIO)
    else:
        t_inicio = 0.0

    if t_fin_raw is not None:
        t_fin = min(duracion, t_fin_raw + SEGUNDOS_DESPUES_FIN)
    else:
        t_fin = duracion

    return {
        "t_inicio": t_inicio,
        "t_fin": t_fin,
        "t_inicio_raw": t_inicio_raw,
        "t_fin_raw": t_fin_raw,
        "texto_inicio": texto_inicio,
        "texto_fin": texto_fin,
        "sin_vuelo": sin_vuelo,
        "fin_sintetizado": fin_sintetizado,
        "logs": logs,
    }


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

        # Detección de corte (lógica compartida con el GUI)
        print("→ Buscando señal de salida y llegada y analizando audio...")
        corte = resolver_corte(transcripcion, duracion, audio_tmp)
        for nivel, msg in corte["logs"]:
            prefijo = "  ⚠" if nivel == "aviso" else "  ✓"
            print(f"{prefijo} {msg}")

        # Caso "sin vuelo": no generar salida, devolver error claro.
        if corte["sin_vuelo"]:
            print("  ⚠ No se detectó ningún vuelo en este clip — saltando")
            return False, "sin vuelo detectado (vídeo de instrucciones, prueba o subido por error)"

        t_inicio, t_fin = corte["t_inicio"], corte["t_fin"]
        texto_inicio, texto_fin = corte["texto_inicio"], corte["texto_fin"]

        if corte["t_inicio_raw"] is not None and texto_inicio != "[audio]":
            print(f"  ✓ Salida detectada:  '{texto_inicio.strip()}' → {segundos_a_mmss(corte['t_inicio_raw'])}")
        elif corte["t_inicio_raw"] is None:
            print("  ⚠ Inicio no detectado, empezando desde 0s")

        if corte.get("fin_sintetizado"):
            print(f"  ⚠ Llegada SINTETIZADA: '{texto_fin.strip()}' → {segundos_a_mmss(corte['t_fin_raw'])}")
            print(f"    (sin voz ni caída del viento — REVISAR el corte manualmente)")
        elif corte["t_fin_raw"] is not None and texto_fin != "[audio]":
            print(f"  ✓ Llegada detectada: '{texto_fin.strip()}' → {segundos_a_mmss(corte['t_fin_raw'])}")
        elif corte["t_fin_raw"] is None:
            print("  ⚠ Fin no detectado, usando fin del clip")
            print(f"    (revisa {transcript_path.name} para ver qué dijo el monitor)")

        # Confirmar con el usuario (solo en modo manual)
        if not MODO_AUTO:
            t_inicio, t_fin = confirmar_o_ajustar(t_inicio, t_fin, duracion, video_path)
        else:
            print(f"\n  Corte automático: {segundos_a_mmss(t_inicio)} → {segundos_a_mmss(t_fin)} ({t_fin - t_inicio:.0f}s)")

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
        if f.is_file() and f.suffix in EXTENSIONES
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
