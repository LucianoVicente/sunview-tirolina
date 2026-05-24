"""
Sunview Park - Editor automático de vídeos de tirolina
========================================================
"""

import atexit
import os
import subprocess
import shutil
import sys
import tempfile
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

# Buscar inicio solo en el primer X% del vídeo (la cuenta atrás ocurre antes del vuelo)
BUSCAR_INICIO_HASTA_PORCENTAJE = 0.55  # primer 55% del clip

# Frases de SALIDA — el monitor las dice justo antes de soltar al viajero.
# CUENTA ATRÁS = señal fuerte e inequívoca del lanzamiento.
PALABRAS_INICIO_FUERTES = [
    "3, 2, 1",
    "3 2 1",
    "tres, dos, uno",
    "tres dos uno",
    "uno dos tres",
    "3, 2,",
    "tres, dos,",
    # Variante observada en Sunview (cuenta "una, dos, uno")
    "una, dos, uno",
    "uno, dos, uno",
]

# SEND-OFF = frases más débiles que el monitor también dice como saludo/presentación.
# Solo se usan si NO se encuentra una cuenta atrás fuerte.
PALABRAS_INICIO_DEBILES = [
    "buen vuelo",       # también se dice al PRESENTAR al rider — ver filtro abajo
    "nos vamos",        # "piernas arriba que nos vamos"
    "allá vamos",
    "ya vamos",
    "disfruta",
    "disfruta del vuelo",
    "venga, nos vamos",
    "venga nos vamos",
]

# Compatibilidad: alias para tests/otros consumidores
PALABRAS_INICIO = PALABRAS_INICIO_FUERTES + PALABRAS_INICIO_DEBILES

# Zona de presentación: en los primeros segundos el monitor dice
# "Número X, número X, buen vuelo" como presentación, NO como lanzamiento.
# Ignorar matches en esta ventana si el segmento contiene "número".
ZONA_PRESENTACION_S = 12

# Palabras clave de LLEGADA — frases observadas en los vídeos reales
PALABRAS_FIN = [
    # Exclamaciones de llegada observadas en los vídeos reales
    "hola",
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

# Orden de fallback si el modelo principal no cabe en RAM/VRAM.
# small ≈ 1.5 GB, base ≈ 0.5 GB, tiny ≈ 0.2 GB.
MODELOS_WHISPER_FALLBACK = ["small", "base", "tiny"]

# ========== MODO AUTOMÁTICO ==========
MODO_AUTO = True        # True = sin preguntas, procesa todo solo


def cargar_modelo_whisper(preferido=None, log=None):
    """
    Carga el modelo Whisper más grande que quepa en memoria.
    Empieza por `preferido` (o MODELO_WHISPER) y degrada a base/tiny si OOM.

    `log` es un callable opcional log(nivel, msg) — la GUI lo usa para mostrar
    qué modelo se cargó. Si es None, se imprime en consola.

    Devuelve (modelo, nombre_modelo_cargado).
    Lanza RuntimeError con todos los errores acumulados si ninguno carga.
    """
    import whisper

    def _say(nivel, msg):
        if log is not None:
            log(nivel, msg)
        else:
            print(f"  {'⚠' if nivel == 'aviso' else '✓'} {msg}")

    # Construir orden: empezar por el preferido, luego degradar.
    pref = preferido or MODELO_WHISPER
    orden = [pref] + [m for m in MODELOS_WHISPER_FALLBACK if m != pref]

    errores = []
    for nombre in orden:
        try:
            modelo = whisper.load_model(nombre)
            if nombre != pref:
                _say("aviso", f"Modelo '{pref}' no cargó; usando '{nombre}' (menos preciso pero ligero)")
            else:
                _say("ok", f"Modelo Whisper '{nombre}' cargado")
            return modelo, nombre
        except (MemoryError, RuntimeError, OSError) as e:
            errores.append(f"{nombre}: {type(e).__name__}: {e}")
            continue

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
        import whisper
        print("✓ Whisper encontrado")
    except ImportError:
        print("❌ Whisper no instalado. Ejecuta: pip install openai-whisper")
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
    subprocess.run(cmd, capture_output=True, check=True)


def obtener_duracion(video_path):
    cmd = [
        "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(result.stdout.strip())


def transcribir_audio(audio_path, model):
    result = model.transcribe(
        str(audio_path),
        language="es",
        verbose=None,
        temperature=0,
    )
    return result


def _es_ruido_viento(texto: str) -> bool:
    """
    Detecta si un segmento es alucinación de ruido de viento.
    Whisper transcribe el viento como palabras muy repetidas:
    "no, no, no, no..." o "pero no lo sé, pero no lo sé..."
    """
    palabras = texto.lower().split()
    if len(palabras) < 6:
        return False
    ratio_unicas = len(set(palabras)) / len(palabras)
    return ratio_unicas < 0.35


def _es_presentacion(seg, texto_ventana):
    """
    Detecta el saludo inicial del monitor: "Número X, número X, buen vuelo".
    Se dice al PRESENTAR al rider (mucho antes del lanzamiento real) y es
    el principal falso positivo de inicio. Solo aplica en la zona de presentación.
    """
    if seg["start"] >= ZONA_PRESENTACION_S:
        return False
    return "número" in texto_ventana or "numero" in texto_ventana


def buscar_inicio(transcripcion, duracion):
    """
    Busca el momento de lanzamiento en la primera parte del vídeo.

    Estrategia (en orden):
    1. Cuenta atrás fuerte ("3, 2, 1", "tres, dos, uno") — señal inequívoca.
    2. Send-off débil ("nos vamos", "buen vuelo"...) — ignorando la zona de
       presentación (primeros segundos cuando el monitor dice "Número X buen vuelo").
    3. Ruido de viento — el bloque previo al inicio del viento.
    """
    limite = duracion * BUSCAR_INICIO_HASTA_PORCENTAJE
    segmentos = [s for s in transcripcion["segments"] if s["start"] <= limite]

    def texto_con_lookahead(i, seg):
        texto = seg["text"].lower().strip()
        if i + 1 < len(segmentos):
            sig = segmentos[i + 1]
            if sig["start"] - seg["start"] < 10:
                texto += " " + sig["text"].lower().strip()
        return texto

    # Pasada 1: cuenta atrás fuerte (último match)
    fuertes = []
    for i, seg in enumerate(segmentos):
        texto = texto_con_lookahead(i, seg)
        for palabra in PALABRAS_INICIO_FUERTES:
            if palabra in texto:
                fuertes.append((seg["start"], seg["text"]))
                break
    if fuertes:
        return fuertes[-1]

    # Pasada 2: send-off débil, ignorando la presentación inicial
    debiles = []
    for i, seg in enumerate(segmentos):
        texto = texto_con_lookahead(i, seg)
        if _es_presentacion(seg, texto):
            continue
        for palabra in PALABRAS_INICIO_DEBILES:
            if palabra in texto:
                debiles.append((seg["start"], seg["text"]))
                break
    if debiles:
        return debiles[-1]

    # Pasada 3: ruido de viento como fallback
    for i, seg in enumerate(segmentos):
        if _es_ruido_viento(seg["text"]):
            prev = segmentos[i - 1] if i > 0 else seg
            return prev["start"], f"[ruido viento] {prev['text']}"

    return None, None


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


def _bloques_de_viento(audio_path):
    """
    Analiza el audio y devuelve [(t_ini, t_fin), ...] con todos los bloques
    de viento sostenido. Lista vacía si no se pudo leer o no hay viento.
    """
    import wave
    import numpy as np

    try:
        with wave.open(str(audio_path), 'rb') as wf:
            sr  = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
    except Exception:
        return []

    y = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

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

    # Viento = energía por encima del suelo (5 % del pico) Y baja variabilidad.
    noise_floor = np.max(rms_s) * 0.05
    wind = (rms_s > noise_floor) & (cv < 0.50)

    # Rellenar huecos de hasta 5 s (gritos del pasajero).
    GAP = 20
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

    # Convertir a segundos y filtrar bloques cortos (<10 s ≠ vuelo real)
    bloques = [
        (a * HOP / sr, b * HOP / sr)
        for a, b in runs
        if (b - a) * HOP / sr >= 10
    ]
    return bloques


def detectar_vuelo_por_audio(audio_path, duracion):
    """
    Detecta inicio y fin del vuelo analizando el audio crudo, sin depender
    de lo que diga el monitor. El vuelo = bloque más largo de viento sostenido.
    Funciona con cualquier idioma, monitor o parque.
    Devuelve (t_inicio, t_fin) en segundos, o (None, None).
    """
    bloques = _bloques_de_viento(audio_path)
    if not bloques:
        return None, None
    best = max(bloques, key=lambda b: b[1] - b[0])
    return best


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
    for (_, fin1), (ini2, _) in zip(bloques, bloques[1:]):
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


def editar_video(video_entrada, inicio, fin, video_salida):
    duracion_corte = fin - inicio
    if duracion_corte <= 0:
        raise ValueError(
            f"duración de corte inválida (inicio={inicio:.1f}s, fin={fin:.1f}s)"
        )
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(inicio),
        "-i", str(video_entrada),
        "-i", str(LOGO_PATH),
        "-t", str(duracion_corte),
        "-filter_complex",
        f"[1:v]scale=iw*{LOGO_ESCALA}:-1[logo];"
        f"[0:v]scale={RESOLUCION}:force_original_aspect_ratio=decrease,"
        f"pad={RESOLUCION}:(ow-iw)/2:(oh-ih)/2[v];"
        f"[v][logo]overlay=W-w-{LOGO_MARGEN}:{LOGO_MARGEN}",
        "-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF),
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(video_salida)
    ]
    subprocess.run(cmd, capture_output=True, check=True)


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
        out = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
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
        stderr = subprocess.run(cmd, capture_output=True, text=True).stderr
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


def _wav_rms_cv(video_path, ss=None, duracion=None):
    """Extrae un segmento de audio y devuelve (rms_medio, coef_variacion)."""
    import wave
    import tempfile
    import numpy as np
    fd, tmp_name = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        cmd = ["ffmpeg", "-y"]
        if ss is not None:
            cmd += ["-ss", str(ss)]
        cmd += ["-i", str(video_path)]
        if duracion is not None:
            cmd += ["-t", str(duracion)]
        cmd += ["-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(tmp)]
        subprocess.run(cmd, capture_output=True, check=True)
        with wave.open(str(tmp), "rb") as wf:
            sr  = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        y = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        HOP, WIN = int(sr * 0.1), int(sr * 0.2)
        n = max(1, (len(y) - WIN) // HOP)
        frames = np.array([np.sqrt(np.mean(y[i*HOP:i*HOP+WIN]**2)) for i in range(n)])
        rms = float(frames.mean())
        cv  = float(frames.std() / (rms + 1e-8))
        return rms, cv
    finally:
        tmp.unlink(missing_ok=True)


def verificar_inicio_limpio(video_path):
    """
    Los primeros 3s no deben ser silencio.
    Silencio al inicio indica que el corte fue demasiado tarde
    o que el clip empieza con pantalla negra.
    """
    try:
        rms, _ = _wav_rms_cv(video_path, duracion=3)
        if rms < 0.005:
            return False, f"inicio silencioso (RMS={rms:.4f}) — posible corte tarde"
        return True, f"RMS={rms:.3f}"
    except Exception as e:
        return False, str(e)


def verificar_llegada_detectada(video_path):
    """
    Los últimos 5s deben tener voz (energía variable).
    Ruido constante = aún en vuelo. Silencio = corte antes de la llegada.
    """
    try:
        dur = obtener_duracion(video_path)
        rms, cv = _wav_rms_cv(video_path, ss=max(0, dur - 5))
        if rms < 0.005:
            return False, "final silencioso — corte antes de la llegada"
        if cv < 0.30:
            return False, f"final con ruido constante (CV={cv:.2f}) — vuelo sin llegada capturada"
        return True, f"voz detectada (CV={cv:.2f})"
    except Exception as e:
        return False, str(e)


def verificar_corte(video_path, *_args, **_kwargs):
    """
    Orquestador: ejecuta todos los verificadores y devuelve
    lista de (nombre, ok, detalle).
    """
    video_path = Path(video_path)
    if not video_path.exists():
        return [("archivo", False, "no encontrado")]

    size_mb = video_path.stat().st_size / (1024 * 1024)
    if size_mb < 0.5:
        return [("archivo", False, f"muy pequeño ({size_mb:.1f} MB)")]

    checks = [
        ("duración",   verificar_duracion),
        ("resolución", verificar_resolucion),
        ("audio",      verificar_audio_nivel),
        ("inicio",     verificar_inicio_limpio),
        ("llegada",    verificar_llegada_detectada),
    ]
    resultados = []
    for nombre, fn in checks:
        ok, detalle = fn(video_path)
        resultados.append((nombre, ok, detalle))
    return resultados


# ========== VALIDACIÓN PREVIA ==========
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

    # OneDrive guarda placeholders de pocos bytes para archivos no descargados.
    # Cualquier vídeo real pesa muchísimo más que 1 KB.
    try:
        size = video_path.stat().st_size
    except OSError as e:
        return False, f"no se puede leer: {e}"
    if size < 1024:
        return False, "archivo vacío o sin descargar (placeholder de OneDrive)"

    # ffprobe valida formato y presencia de pista de audio/vídeo.
    if not shutil.which("ffprobe"):
        return False, "ffprobe no instalado (instala FFmpeg)"

    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, "ffprobe se colgó leyendo el archivo (posible corrupción)"
    except Exception as e:
        return False, f"error al validar: {e}"

    if result.returncode != 0:
        msg = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "formato no reconocido"
        return False, f"no es un vídeo válido ({msg[:80]})"

    codecs = [c for c in result.stdout.strip().splitlines() if c]
    if "video" not in codecs:
        return False, "no tiene pista de vídeo"
    if "audio" not in codecs:
        return False, "sin pista de audio (cámara con micrófono apagado)"

    return True, "ok"


# ========== ARCHIVO TEMPORAL DE AUDIO ==========
# Único por proceso para evitar colisiones cuando hay dos instancias corriendo.
# Se borra al salir incluso si hay crash.
_AUDIO_TMP_PATH = None


def audio_tmp_path():
    """Devuelve la ruta del WAV temporal de este proceso, creándola si hace falta."""
    global _AUDIO_TMP_PATH
    if _AUDIO_TMP_PATH is None:
        fd, name = tempfile.mkstemp(prefix="sunview_audio_", suffix=".wav")
        os.close(fd)
        _AUDIO_TMP_PATH = Path(name)
        atexit.register(_limpiar_audio_tmp)
    return _AUDIO_TMP_PATH


def _limpiar_audio_tmp():
    if _AUDIO_TMP_PATH is not None:
        try:
            _AUDIO_TMP_PATH.unlink(missing_ok=True)
        except Exception:
            pass


# ========== RESOLUCIÓN DE CORTE COMPARTIDA ==========
# Esta función es la ÚNICA fuente de verdad para decidir t_inicio/t_fin.
# Tanto CLI (procesar_video) como GUI (gui_tirolina._worker) la usan.
# Cualquier cambio en la lógica de detección se aplica automáticamente a ambos flujos.
def resolver_corte(transcripcion, duracion, audio_path):
    """
    Combina detección por frase + audio para devolver el corte final.

    Devuelve dict:
      t_inicio, t_fin              — segundos absolutos con márgenes aplicados
      t_inicio_raw, t_fin_raw      — segundos sin márgenes (None si no detectado)
      texto_inicio, texto_fin      — frase que disparó la detección, o "[audio]"
      sin_vuelo                    — True si ni frase ni audio detectaron un vuelo
      logs                         — lista de (nivel, mensaje) para que el caller imprima/loguee
    """
    t_inicio_raw, texto_inicio = buscar_inicio(transcripcion, duracion)
    t_fin_raw,    texto_fin    = buscar_fin(transcripcion, duracion)
    t_audio_ini, t_audio_fin   = detectar_vuelo_por_audio(audio_path, duracion)

    logs: list[tuple[str, str]] = []

    # Aviso multi-vuelo: si hay 2+ vuelos en el clip, advertir.
    # No intentamos auto-split (mantenemos el corte del bloque más largo).
    if detectar_multiples_vuelos(audio_path):
        logs.append((
            "aviso",
            "Se detectaron 2 o más vuelos en este clip — "
            "se cortará solo el más largo. Si necesitas los otros, "
            "ajústalo a mano."
        ))

    # ─── INICIO ───
    # Solo aceptamos el override de audio cuando la frase está muy lejos del
    # viento real (>30 s) — claro falso positivo (saludo/instrucciones).
    if t_inicio_raw is None and t_audio_ini is not None:
        t_inicio_raw, texto_inicio = t_audio_ini, "[audio]"
        logs.append(("ok", f"Inicio por audio: {segundos_a_mmss(t_inicio_raw)}"))
    elif t_inicio_raw is not None and t_audio_ini is not None:
        if t_audio_ini > t_inicio_raw + 30:
            logs.append((
                "aviso",
                f"Inicio por frase ({segundos_a_mmss(t_inicio_raw)}) parece falso "
                f"positivo; viento empieza en {segundos_a_mmss(t_audio_ini)} — corrigiendo"
            ))
            t_inicio_raw, texto_inicio = t_audio_ini, "[audio]"

    # ─── FIN ───
    # Si hay frase de llegada, SIEMPRE se respeta. El audio puede terminar el
    # "wind block" prematuramente cuando el rider grita; eso cortaría antes de
    # la llegada. La primera frase de llegada es la señal definitiva.
    if t_fin_raw is None and t_audio_fin is not None:
        t_fin_raw, texto_fin = t_audio_fin, "[audio]"
        logs.append(("ok", f"Fin por audio: {segundos_a_mmss(t_fin_raw)}"))

    # ─── SIN VUELO ───
    # Casos: ni frase ni audio encontraron nada, o el bloque detectado es
    # demasiado corto para ser un vuelo real (típicamente vídeo subido por error).
    sin_vuelo = False
    if t_inicio_raw is None and t_fin_raw is None:
        sin_vuelo = True
    elif t_inicio_raw is not None and t_fin_raw is not None:
        if t_fin_raw - t_inicio_raw < 10:
            sin_vuelo = True

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

    audio_tmp = audio_tmp_path()
    video_salida = CARPETA_SALIDA / f"{video_path.stem}_FINAL.mp4"

    try:
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

        if corte["t_fin_raw"] is not None and texto_fin != "[audio]":
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
    finally:
        # El audio temporal se borra siempre (atexit también lo cubre por si crashea).
        audio_tmp.unlink(missing_ok=True)


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
