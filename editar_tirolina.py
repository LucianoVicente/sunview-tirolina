"""
Sunview Park - Editor automático de vídeos de tirolina
========================================================
"""

import os
import sys
import subprocess
import shutil
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
PALABRAS_INICIO = [
    # Cuenta atrás clásica
    "3, 2, 1",
    "3 2 1",
    "tres, dos, uno",
    "tres dos uno",
    "uno dos tres",
    "2, 1",
    "vamos, 3",
    "vamos 3",
    # Variante observada en Sunview (cuenta "una, dos, uno")
    "una, dos",
    "uno, dos",
    # Send-off del monitor — frases observadas en los vídeos reales
    "buen vuelo",       # dicho justo antes de soltar (muy frecuente)
    "nos vamos",        # "piernas arriba que nos vamos"
    "allá vamos",
    "ya vamos",
    "disfruta",
    "disfruta del vuelo",
    "venga",            # comando de lanzamiento habitual
]

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
    # Otras frases de llegada
    "sobrevivimos",
    "perfecto",
    "bienvenido",
    "bien bien bien", "bien, bien, bien",
    "yeee", "yee", "yuhu",
    "apísima",
]

# Calidad de salida
RESOLUCION = "1920:1080"
CRF = 23
PRESET = "medium"
LOGO_ESCALA = 0.65  # tamaño del logo
LOGO_MARGEN = 60   # distancia desde la esquina en píxeles

EXTENSIONES = (".mp4", ".mov", ".MP4", ".MOV", ".avi", ".AVI", ".mkv", ".MKV")

# ========== MODO AUTOMÁTICO ==========
MODO_AUTO = True        # True = sin preguntas, procesa todo solo


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


def buscar_inicio(transcripcion, duracion):
    """
    Busca el momento de lanzamiento en la primera parte del vídeo.
    Estrategia 1: busca frases de PALABRAS_INICIO (cuenta atrás, send-off del monitor).
    Estrategia 2 (fallback): detecta dónde empieza el ruido de viento; el segmento
                             justo anterior es el último momento antes del vuelo.
    """
    limite = duracion * BUSCAR_INICIO_HASTA_PORCENTAJE
    segmentos = [s for s in transcripcion["segments"] if s["start"] <= limite]
    candidatos = []

    for i, seg in enumerate(segmentos):
        texto_ventana = seg["text"].lower().strip()
        if i + 1 < len(segmentos):
            sig = segmentos[i + 1]
            if sig["start"] - seg["start"] < 10:
                texto_ventana += " " + sig["text"].lower().strip()

        for palabra in PALABRAS_INICIO:
            if palabra in texto_ventana:
                candidatos.append((seg["start"], seg["text"]))
                break

    if candidatos:
        return candidatos[-1]

    # Fallback: el vuelo empieza donde empieza el ruido de viento
    for i, seg in enumerate(segmentos):
        if _es_ruido_viento(seg["text"]):
            prev = segmentos[i - 1] if i > 0 else seg
            return prev["start"], f"[ruido viento] {prev['text']}"

    return None, None


def buscar_fin(transcripcion, duracion):
    """
    Busca la señal de llegada desde el final hacia atrás.
    Solo busca en el último 30% del vídeo para evitar falsos positivos.
    """
    limite_fin = duracion * 0.70
    for seg in reversed(transcripcion["segments"]):
        if seg["start"] < limite_fin:
            break
        texto = seg["text"].lower().strip()
        for palabra in PALABRAS_FIN:
            if palabra in texto:
                return seg["start"], seg["text"]
    return None, None


def detectar_vuelo_por_audio(audio_path, duracion):
    """
    Detecta inicio y fin del vuelo analizando el audio crudo, sin depender
    de lo que diga el monitor. El vuelo = período más largo de ruido constante
    (viento): energía alta y variabilidad baja, a diferencia del habla humana
    que oscila mucho entre sílabas y silencios.
    Funciona con cualquier idioma, monitor o parque.
    Devuelve (t_inicio, t_fin) en segundos, o (None, None).
    """
    import wave
    import numpy as np

    try:
        with wave.open(str(audio_path), 'rb') as wf:
            sr  = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
    except Exception:
        return None, None

    y = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    HOP = int(sr * 0.25)   # salto 0.25 s
    WIN = int(sr * 0.50)   # ventana 0.5 s
    n   = max(1, (len(y) - WIN) // HOP)

    # Energía RMS por ventana
    rms = np.array([
        np.sqrt(np.mean(y[i*HOP : i*HOP + WIN] ** 2))
        for i in range(n)
    ])

    # Suavizado leve
    k = min(7, n)
    rms_s = np.convolve(rms, np.ones(k) / k, mode='same')

    # Coeficiente de variación local (ventana ±2 s = ±8 frames a 0.25 s/frame)
    CV_WIN = 8
    cv = np.array([
        rms_s[max(0, i - CV_WIN) : i + CV_WIN + 1].std()
        / (rms_s[max(0, i - CV_WIN) : i + CV_WIN + 1].mean() + 1e-8)
        for i in range(n)
    ])

    # Zona de viento: energía > mediana×1.2  Y  variabilidad baja (CV < 0.5)
    umbral_rms = np.median(rms_s) * 1.2
    wind = (rms_s > umbral_rms) & (cv < 0.50)

    # Rellenar huecos cortos (≤ 2 s) para no fragmentar el vuelo
    GAP = 8
    wind_s = np.array([
        wind[max(0, i - GAP) : min(n, i + GAP + 1)].mean() >= 0.5
        for i in range(n)
    ])

    # Extraer todos los bloques contiguos de "viento"
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

    if not runs:
        return None, None

    # El vuelo = bloque de viento más largo del clip
    best = max(runs, key=lambda r: r[1] - r[0])
    t_inicio = best[0] * HOP / sr
    t_fin    = best[1] * HOP / sr

    # Descartar si el período es demasiado corto para ser un vuelo real
    if t_fin - t_inicio < 10:
        return None, None

    return t_inicio, t_fin


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


def verificar_corte(video_salida, t_inicio_raw, t_fin_raw):
    """Comprueba tamaño y duración del clip. Devuelve (ok: bool, detalle: str)."""
    if not video_salida.exists():
        return False, "archivo de salida no encontrado"

    size_mb = video_salida.stat().st_size / (1024 * 1024)
    if size_mb < 0.5:
        return False, f"archivo muy pequeño ({size_mb:.1f} MB)"

    dur = obtener_duracion(video_salida)
    if dur < 15:
        return False, f"clip demasiado corto ({dur:.0f}s)"
    if dur > 300:
        return False, f"clip demasiado largo ({dur:.0f}s)"

    if t_inicio_raw is None or t_fin_raw is None:
        return False, f"requiere ajuste manual ({dur:.0f}s)"

    return True, f"OK ({dur:.0f}s, {size_mb:.1f} MB)"


# ========== PROCESAMIENTO PRINCIPAL ==========
def procesar_video(video_path, model):
    print_header(f"Procesando: {video_path.name}")

    print("→ Extrayendo audio...")
    audio_tmp = Path("_audio_tmp.wav")
    extraer_audio(video_path, audio_tmp)

    duracion = obtener_duracion(video_path)

    print("→ Transcribiendo audio con IA...")
    transcripcion = transcribir_audio(audio_tmp, model)

    # Guardar transcripción para debug
    transcript_path = CARPETA_SALIDA / f"{video_path.stem}_transcripcion.txt"
    with open(transcript_path, "w", encoding="utf-8") as f:
        for seg in transcripcion["segments"]:
            f.write(f"[{seg['start']:6.2f}s - {seg['end']:6.2f}s] {seg['text']}\n")

    # Detectar puntos de corte (guardamos los valores raw antes de aplicar márgenes)
    print("→ Buscando señal de salida y llegada...")
    t_inicio_raw, texto_inicio = buscar_inicio(transcripcion, duracion)
    t_fin_raw,    texto_fin    = buscar_fin(transcripcion, duracion)

    # Fallback de audio: si alguno no se detectó por transcripción, analizar el WAV
    if t_inicio_raw is None or t_fin_raw is None:
        print("→ Aplicando análisis de audio (energía de viento)...")
        t_audio_ini, t_audio_fin = detectar_vuelo_por_audio(audio_tmp, duracion)
        if t_inicio_raw is None and t_audio_ini is not None:
            t_inicio_raw  = t_audio_ini
            texto_inicio  = "[audio]"
            print(f"  ✓ Inicio por audio:  {segundos_a_mmss(t_inicio_raw)}")
        if t_fin_raw is None and t_audio_fin is not None:
            t_fin_raw   = t_audio_fin
            texto_fin   = "[audio]"
            print(f"  ✓ Fin por audio:     {segundos_a_mmss(t_fin_raw)}")

    # Aplicar márgenes
    if t_inicio_raw is not None:
        if texto_inicio != "[audio]":
            print(f"  ✓ Salida detectada:  '{texto_inicio.strip()}' → {segundos_a_mmss(t_inicio_raw)}")
        t_inicio = max(0, t_inicio_raw - SEGUNDOS_ANTES_INICIO)
    else:
        print("  ⚠ Inicio no detectado, empezando desde 0s")
        t_inicio = 0

    if t_fin_raw is not None:
        if texto_fin != "[audio]":
            print(f"  ✓ Llegada detectada: '{texto_fin.strip()}' → {segundos_a_mmss(t_fin_raw)}")
        t_fin = min(duracion, t_fin_raw + SEGUNDOS_DESPUES_FIN)
    else:
        print("  ⚠ Fin no detectado, usando fin del clip")
        print(f"    (revisa {transcript_path.name} para ver qué dijo el monitor)")
        t_fin = duracion

    # Confirmar con el usuario (solo en modo manual)
    if not MODO_AUTO:
        t_inicio, t_fin = confirmar_o_ajustar(t_inicio, t_fin, duracion, video_path)
    else:
        print(f"\n  Corte automático: {segundos_a_mmss(t_inicio)} → {segundos_a_mmss(t_fin)} ({t_fin - t_inicio:.0f}s)")

    # Editar
    print("→ Generando vídeo final (recorte + logo + compresión)...")
    video_salida = CARPETA_SALIDA / f"{video_path.stem}_FINAL.mp4"
    editar_video(video_path, t_inicio, t_fin, video_salida)

    # Verificar resultado
    print("→ Verificando resultado...")
    ok, detalle = verificar_corte(video_salida, t_inicio_raw, t_fin_raw)
    if ok:
        print(f"  ✓ {detalle}")
    else:
        print(f"  ⚠ {detalle}")

    audio_tmp.unlink(missing_ok=True)
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
    import whisper
    model = whisper.load_model(MODELO_WHISPER)
    print("  ✓ Lista")

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
