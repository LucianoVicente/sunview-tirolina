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
CARPETA_ENTRADA = Path("entrada")
CARPETA_SALIDA = Path("salida")
LOGO_PATH = Path("assets/logo.png")
MODELO_WHISPER = "small"

# Recorte
SEGUNDOS_ANTES_INICIO = 1.5
SEGUNDOS_DESPUES_FIN = 2.0

# Buscar inicio solo en el primer X% del vídeo (la cuenta atrás ocurre antes del vuelo)
BUSCAR_INICIO_HASTA_PORCENTAJE = 0.55  # primer 55% del clip

# Frases de SALIDA — el monitor las dice justo antes de soltar al viajero.
# Whisper transcribe los números con comas ("3, 2, 1") y en español con comas también.
PALABRAS_INICIO = [
    "3, 2, 1",
    "3 2 1",
    "tres, dos, uno",   # Whisper con comas
    "tres dos uno",
    "uno dos tres",
    "vamos, 3",
    "vamos 3",
    "2, 1",             # a veces Whisper solo capta el final de la cuenta
]

# Palabras clave de LLEGADA — frases observadas en los vídeos reales
PALABRAS_FIN = [
    # Frases detectadas en los vídeos
    "todo bien",            # "¿todo bien?" — variante frecuente de Whisper
    "está bien",            # "¿tú está bien?" / "todo está bien"
    "sobrevivimos",
    "perfecto",
    "apísima",
    "cómo estás", "como estás",
    # Frases genéricas de bienvenida
    "bien bien bien", "bien, bien, bien",
    "qué tal", "que tal",
    "cómo ha estado", "como ha estado",
    "cómo estuvo", "como estuvo",
    "te ha gustado", "qué te ha parecido",
    "bienvenido",
    "yeee", "yee", "yuhu",
    "cómo fue", "como fue",
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
VERIFICAR_BORDES = True # Re-transcribe los extremos del clip para confirmar el corte
SEGUNDOS_BORDE = 8      # Segundos a analizar en cada extremo del clip editado


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
        verbose=False,
        word_timestamps=True
    )
    return result


def buscar_inicio(transcripcion, duracion):
    """
    Busca la cuenta atrás en la primera parte del vídeo (antes del vuelo).
    Devuelve el ÚLTIMO candidato encontrado (el más cercano al momento de soltar).
    """
    limite = duracion * BUSCAR_INICIO_HASTA_PORCENTAJE
    segmentos = [s for s in transcripcion["segments"] if s["start"] <= limite]
    candidatos = []

    for i, seg in enumerate(segmentos):
        # Texto del segmento actual + siguiente (por si la cuenta atrás se parte en dos)
        texto_ventana = seg["text"].lower().strip()
        if i + 1 < len(segmentos):
            sig = segmentos[i + 1]
            if sig["start"] - seg["start"] < 10:
                texto_ventana += " " + sig["text"].lower().strip()

        for palabra in PALABRAS_INICIO:
            if palabra in texto_ventana:
                candidatos.append((seg["start"], seg["text"]))
                break

    if not candidatos:
        return None, None

    return candidatos[-1]


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


def verificar_corte(video_salida, t_inicio_raw, t_fin_raw, model):
    """
    Comprueba que el clip editado tiene sentido: tamaño, duración y,
    si VERIFICAR_BORDES, que la cuenta atrás y la llegada están presentes.
    Devuelve (ok: bool, detalle: str).
    """
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

    if t_inicio_raw is None:
        return False, "inicio no fue detectado automáticamente — revisar manualmente"
    if t_fin_raw is None:
        return False, "fin no fue detectado automáticamente — revisar manualmente"

    if not VERIFICAR_BORDES:
        return True, f"OK ({dur:.0f}s, {size_mb:.1f} MB)"

    audio_inicio = Path("_check_inicio.wav")
    audio_fin = Path("_check_fin.wav")
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", str(video_salida),
            "-t", str(SEGUNDOS_BORDE),
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            str(audio_inicio)
        ], capture_output=True, check=True)

        inicio_seg = max(0, dur - SEGUNDOS_BORDE)
        subprocess.run([
            "ffmpeg", "-y", "-i", str(video_salida),
            "-ss", str(inicio_seg),
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            str(audio_fin)
        ], capture_output=True, check=True)

        tx_i = model.transcribe(str(audio_inicio), language="es", verbose=False)
        tx_f = model.transcribe(str(audio_fin), language="es", verbose=False)
        texto_i = tx_i["text"].lower()
        texto_f = tx_f["text"].lower()

        tiene_cuenta = any(p in texto_i for p in PALABRAS_INICIO)
        tiene_llegada = any(p in texto_f for p in PALABRAS_FIN)

        alertas = []
        if not tiene_cuenta:
            alertas.append("no se oye cuenta atrás al inicio")
        if not tiene_llegada:
            alertas.append("no se oye llegada al final")

        if alertas:
            return False, "REVISAR — " + "; ".join(alertas) + f" ({dur:.0f}s)"
        return True, f"OK ({dur:.0f}s, {size_mb:.1f} MB)"

    finally:
        audio_inicio.unlink(missing_ok=True)
        audio_fin.unlink(missing_ok=True)


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
    print("→ Buscando señal de salida...")
    t_inicio_raw, texto_inicio = buscar_inicio(transcripcion, duracion)

    print("→ Buscando señal de llegada...")
    t_fin_raw, texto_fin = buscar_fin(transcripcion, duracion)

    # Aplicar márgenes
    if t_inicio_raw is not None:
        print(f"  ✓ Salida detectada:  '{texto_inicio.strip()}' → {segundos_a_mmss(t_inicio_raw)}")
        t_inicio = max(0, t_inicio_raw - SEGUNDOS_ANTES_INICIO)
    else:
        print("  ⚠ Señal de salida no detectada, empezando desde 0s")
        print(f"    (revisa {transcript_path.name} para ver qué dijo el monitor)")
        t_inicio = 0

    if t_fin_raw is not None:
        print(f"  ✓ Llegada detectada: '{texto_fin.strip()}' → {segundos_a_mmss(t_fin_raw)}")
        t_fin = min(duracion, t_fin_raw + SEGUNDOS_DESPUES_FIN)
    else:
        print("  ⚠ Señal de llegada no detectada, usando fin del clip")
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
    ok, detalle = verificar_corte(video_salida, t_inicio_raw, t_fin_raw, model)
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
