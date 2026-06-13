"""
Sunview Park — Asistente de edición de tirolina (flujo de 3 fases)

  Fase 1 (Análisis automático): extrae audio, transcribe, sugiere t_inicio/t_fin,
          extrae 3 frames y la onda. No renderiza nada.
  Fase 2 (Revisión humana): tarjeta por clip con storyboard + onda + controles
          editables. Atajos: ↑↓ navega, ←→ ±1s inicio, Enter aprueba, Espacio
          reproduce audio, V abre clip en reproductor externo.
  Fase 3 (Render batch): genera los vídeos finales solo de los clips aprobados.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import queue
import os
import re
import sys
import tempfile
import shutil
import winsound
import datetime
from pathlib import Path

# pythonw.exe no tiene consola — librerías como tqdm/whisper fallan al
# escribir en sys.stdout/stderr si son None. Mantenemos handles a devnull
# y los cerramos al salir para no fugar file descriptors.
import atexit as _atexit
_DEVNULL_HANDLES = []
# encoding="utf-8" es imprescindible: con la codificación por defecto (cp1252)
# cualquier print con "✓"/"⚠" lanza UnicodeEncodeError AUNQUE el destino sea
# devnull — y tumbaba la carga del modelo Whisper bajo pythonw.
if sys.stdout is None:
    _h = open(os.devnull, "w", encoding="utf-8")
    _DEVNULL_HANDLES.append(_h)
    sys.stdout = _h
if sys.stderr is None:
    _h = open(os.devnull, "w", encoding="utf-8")
    _DEVNULL_HANDLES.append(_h)
    sys.stderr = _h


@_atexit.register
def _cerrar_devnull():
    for h in _DEVNULL_HANDLES:
        try:
            h.close()
        except OSError:
            pass


try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DRAG_DROP = True
except ImportError:
    DRAG_DROP = False

from PIL import Image, ImageTk

# FigureCanvasTkAgg ya fija el backend correcto (TkAgg). No forzamos otro
# antes para no romper el embedding en Tkinter.
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from editar_tirolina import (
    extraer_audio, obtener_duracion, transcribir_audio,
    editar_video, verificar_corte, validar_video,
    sugerir_corte, cargar_modelo_whisper, detectar_numero_cliente,
    extraer_3_frames, extraer_frames_grid, frame_mas_cercano,
    extraer_audio_snippet, extraer_clip_preview,
    envolvente_rms,
    segundos_a_mmss, CARPETA_SALIDA, EXTENSIONES,
    SEGUNDOS_ANTES_INICIO, SEGUNDOS_DESPUES_FIN,
)
import envio_correo
import clientes

COLORES = {
    "fondo":        "#F5F7FA",
    "panel":        "#FFFFFF",
    "panel_sel":    "#E3F0FF",
    "acento":       "#1A73E8",
    "ok":           "#34A853",
    "aviso":        "#C07700",
    "error":        "#EA4335",
    "texto":        "#202124",
    "texto_suave":  "#5F6368",
    "borde":        "#DADCE0",
    "marcador_ini": "#34A853",
    "marcador_fin": "#EA4335",
}
FUENTE = "Segoe UI"

BaseVentana = TkinterDnD.Tk if DRAG_DROP else tk.Tk

# Tamaños de UI
ANCHO_FRAME = 220
ALTO_FRAME = 124
PREVIEW_DUR_S = 6.0          # ventana de audio/vídeo de preview (3s antes + 3s despues)


class App(BaseVentana):
    def __init__(self):
        super().__init__()
        self.title("Sunview Park — Asistente de edición")
        self.geometry("1180x720")
        self.minsize(960, 600)
        self.configure(bg=COLORES["fondo"])

        # Estado global
        self.fase = "inicio"          # "inicio" | "analizando" | "revision" | "rendering" | "fin"
        self.videos: list[Path] = []
        self.clips: list[dict] = []   # un dict por clip tras analizar
        self.clip_sel: int = -1       # índice del clip mostrado en el panel
        self.cola: queue.Queue = queue.Queue()

        # Carpeta temporal para frames, snippets, previews — se borra al cerrar
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="sunview_review_"))
        self.protocol("WM_DELETE_WINDOW", self._on_cerrar)

        # Pre-carga del modelo Whisper en background
        self._modelo = None
        self._modelo_error = None
        self._modelo_event = threading.Event()
        threading.Thread(target=self._precargar_modelo, daemon=True).start()

        # Cache de referencias a PhotoImage (sin esto Tk las garbage-collecta)
        self._photos: dict = {}

        self._construir_ui_inicio()
        self._poll_cola()
        self._bind_atajos()

    # ─────────────────────────────────────────────────────────────────────
    # PRECARGA MODELO
    # ─────────────────────────────────────────────────────────────────────
    def _precargar_modelo(self):
        self._modelo_error = None
        try:
            # log → cola de la GUI: evita print (sin consola bajo pythonw) y
            # muestra qué modelo se cargó si el análisis ya está en marcha.
            self._modelo, _ = cargar_modelo_whisper(
                log=lambda nivel, msg: self.cola.put(("log", (f"{msg}\n", nivel))))
        except Exception:
            import traceback
            self._modelo_error = traceback.format_exc()
            self.cola.put(("modelo_error", self._modelo_error))
        finally:
            self._modelo_event.set()

    # ─────────────────────────────────────────────────────────────────────
    # FASE 0 — Inicio: selección de vídeos
    # ─────────────────────────────────────────────────────────────────────
    def _construir_ui_inicio(self):
        self._limpiar_ventana()

        cab = tk.Frame(self, bg=COLORES["acento"], pady=18)
        cab.pack(fill="x")
        tk.Label(cab, text="Sunview Park — Asistente de edición",
                 font=(FUENTE, 16, "bold"),
                 bg=COLORES["acento"], fg="white").pack()
        tk.Label(cab,
                 text="Arrastra los vídeos. La IA analiza, tú validas en segundos.",
                 font=(FUENTE, 10),
                 bg=COLORES["acento"], fg="#BDD7F5").pack()

        cuerpo = tk.Frame(self, bg=COLORES["fondo"], padx=40, pady=30)
        cuerpo.pack(fill="both", expand=True)

        self.zona_drop = tk.Frame(
            cuerpo, bg=COLORES["panel"],
            highlightbackground=COLORES["borde"],
            highlightthickness=2, height=220,
        )
        self.zona_drop.pack(fill="both", expand=True, pady=(0, 20))
        self.zona_drop.pack_propagate(False)

        tk.Label(self.zona_drop, text="📂",
                 font=(FUENTE, 36), bg=COLORES["panel"],
                 fg=COLORES["texto_suave"]).pack(pady=(40, 4))
        msg = ("Arrastra los vídeos aquí"
               if DRAG_DROP else "Pulsa el botón para seleccionar vídeos")
        tk.Label(self.zona_drop, text=msg,
                 font=(FUENTE, 12), bg=COLORES["panel"],
                 fg=COLORES["texto"]).pack()
        tk.Label(self.zona_drop, text="o pulsa el botón abajo",
                 font=(FUENTE, 9), bg=COLORES["panel"],
                 fg=COLORES["texto_suave"]).pack(pady=(0, 12))

        self.lbl_seleccion = tk.Label(self.zona_drop, text="",
                                       font=(FUENTE, 10, "bold"),
                                       bg=COLORES["panel"],
                                       fg=COLORES["acento"])
        self.lbl_seleccion.pack()

        if DRAG_DROP:
            self.zona_drop.drop_target_register(DND_FILES)
            self.zona_drop.dnd_bind("<<Drop>>", self._on_drop)

        tk.Button(cuerpo, text="📁  Seleccionar vídeos manualmente",
                  font=(FUENTE, 10), bg=COLORES["fondo"],
                  fg=COLORES["acento"], relief="flat", bd=0, cursor="hand2",
                  command=self._seleccionar).pack(pady=(0, 6))

        self.btn_analizar = tk.Button(
            cuerpo, text="Selecciona vídeos primero",
            font=(FUENTE, 12, "bold"), height=2,
            bg=COLORES["borde"], fg=COLORES["texto_suave"],
            state="disabled", relief="flat", cursor="arrow",
            command=self._iniciar_analisis,
        )
        self.btn_analizar.pack(fill="x")

    def _seleccionar(self):
        archivos = filedialog.askopenfilenames(
            title="Selecciona vídeos",
            filetypes=[("Vídeos", "*.mp4 *.MP4 *.mov *.MOV *.avi *.AVI *.mkv *.MKV")],
        )
        if archivos:
            self._set_videos([Path(a) for a in archivos])

    def _on_drop(self, event):
        raw = self.tk.splitlist(event.data)
        videos = [Path(p) for p in raw
                  if Path(p).is_file() and Path(p).suffix.lower() in EXTENSIONES]
        if videos:
            self._set_videos(videos)

    def _set_videos(self, videos: list[Path]):
        # dict.fromkeys deduplica conservando el orden: el mismo archivo
        # arrastrado dos veces se analizaría y renderizaría duplicado.
        videos = list(dict.fromkeys(videos))
        self.videos = videos
        if videos:
            self.lbl_seleccion.config(
                text=f"{len(videos)} vídeo(s) listo(s) para analizar")
            self.btn_analizar.config(
                state="normal",
                bg=COLORES["acento"], fg="white",
                text=f"▶  Analizar {len(videos)} vídeo(s)",
                cursor="hand2",
            )

    def _iniciar_analisis(self):
        if not self.videos or self.fase == "analizando":
            return
        try:
            self.fase = "analizando"
            self._construir_ui_analizando()
            threading.Thread(target=self._worker_analizar_safe,
                             args=(self.videos,), daemon=True).start()
        except Exception:
            import traceback
            messagebox.showerror(
                "Error al iniciar análisis",
                f"Detalle:\n\n{traceback.format_exc()}",
            )
            self.fase = "inicio"
            self._construir_ui_inicio()

    def _worker_analizar_safe(self, videos):
        """Wrapper de _worker_analizar que captura cualquier excepción y la
        muestra en el log de la GUI en vez de morir silenciosamente."""
        try:
            self._worker_analizar(videos)
        except Exception:
            import traceback
            tb = traceback.format_exc()
            self.cola.put(("log", (f"\n✗ ERROR FATAL EN WORKER:\n{tb}\n", "error")))
            # Permitir reset
            self.cola.put(("fin_analisis", []))

    # ─────────────────────────────────────────────────────────────────────
    # FASE 1 — Análisis: progreso por clip
    # ─────────────────────────────────────────────────────────────────────
    def _construir_ui_analizando(self):
        self._limpiar_ventana()
        cuerpo = tk.Frame(self, bg=COLORES["fondo"], padx=40, pady=40)
        cuerpo.pack(fill="both", expand=True)

        tk.Label(cuerpo, text="Analizando vídeos...",
                 font=(FUENTE, 16, "bold"),
                 bg=COLORES["fondo"], fg=COLORES["texto"]).pack(pady=(0, 8))
        self.lbl_prog = tk.Label(cuerpo, text="Preparando...",
                                  font=(FUENTE, 10),
                                  bg=COLORES["fondo"],
                                  fg=COLORES["texto_suave"])
        self.lbl_prog.pack(pady=(0, 16))

        self.barra = ttk.Progressbar(cuerpo, orient="horizontal",
                                      length=720, mode="determinate",
                                      maximum=len(self.videos))
        self.barra.pack(pady=(0, 24))

        self.log = tk.Text(cuerpo, height=14, font=(FUENTE, 9),
                           bg="#1A1A1A", fg="#E0E0E0",
                           relief="flat", padx=10, pady=8)
        self.log.pack(fill="both", expand=True)
        for nivel, color in [("ok", "#7DD081"), ("aviso", "#F0B848"),
                              ("error", "#E84A4A"), ("info", "#E0E0E0"),
                              ("header", "#7AB7FF")]:
            self.log.tag_config(nivel, foreground=color)
        self.log.config(state="disabled")

    def _worker_analizar(self, videos):
        """Hilo: analiza cada vídeo y produce un dict por clip.

        Para cada vídeo: valida → extrae audio (PERMANENTE durante revisión)
        → transcribe → sugiere corte → extrae 3 frames + onda. Si algún
        paso falla, se incluye el clip con `error` para que el usuario lo
        vea pero no se pueda renderizar.
        """
        self.cola.put(("log", ("Cargando modelo de IA...\n", "info")))
        self._modelo_event.wait()
        if self._modelo is None:
            msg = (self._modelo_error or
                   "Whisper no se pudo cargar (causa desconocida).")
            self.cola.put(("log", (f"✗ {msg}\n", "error")))
            # Generar clips-error para que la pantalla siguiente muestre el motivo
            clips_err = [self._clip_error(v, f"Whisper no cargó: {msg}")
                          for v in videos]
            self.cola.put(("fin_analisis", clips_err))
            return
        self.cola.put(("log", ("✓ Modelo listo.\n\n", "ok")))

        clips = []
        for i, video in enumerate(videos, 1):
            self.cola.put(("prog_label",
                           f"[{i}/{len(videos)}] {video.name}"))
            self.cola.put(("log",
                           (f"\n── {video.name} ──\n", "header")))

            ok_val, motivo = validar_video(video)
            if not ok_val:
                self.cola.put(("log", (f"  ⚠ {motivo}\n", "aviso")))
                clips.append(self._clip_error(video, motivo))
                self.cola.put(("prog_barra", i))
                continue

            try:
                # Clave única por clip. Dos vídeos con el MISMO nombre de
                # archivo (típico entre tarjetas GoPro: GX010001.MP4 se repite)
                # compartirían WAV, rejilla y previews y se contaminarían entre
                # sí (oír/ver el clip equivocado al revisar). El índice lo evita.
                clave = f"{i:03d}_{video.stem}"
                # WAV temporal de este clip — vive hasta cerrar la app
                audio_tmp = self.tmp_dir / f"{clave}.wav"
                self.cola.put(("log", ("  Extrayendo audio...\n", "info")))
                extraer_audio(video, audio_tmp)
                duracion = obtener_duracion(video)

                self.cola.put(("log", ("  Transcribiendo...\n", "info")))
                tx = transcribir_audio(audio_tmp, self._modelo)

                txt_path = CARPETA_SALIDA / f"{video.stem}_transcripcion.txt"
                CARPETA_SALIDA.mkdir(exist_ok=True)
                with open(txt_path, "w", encoding="utf-8") as f:
                    for seg in tx["segments"]:
                        f.write(f"[{seg['start']:6.2f}s - {seg['end']:6.2f}s] {seg['text']}\n")

                self.cola.put(("log", ("  Sugiriendo corte...\n", "info")))
                sug = sugerir_corte(tx, duracion, audio_tmp)
                for nivel, msg in sug["logs"]:
                    self.cola.put(("log", (f"    {msg}\n", nivel)))

                # Sugerencias con márgenes aplicados (lo que se cortaría)
                t0_sug = (max(0.0, sug["t_inicio_raw"] - SEGUNDOS_ANTES_INICIO)
                          if sug["t_inicio_raw"] is not None else 0.0)
                t1_sug = (min(duracion, sug["t_fin_raw"] + SEGUNDOS_DESPUES_FIN)
                          if sug["t_fin_raw"] is not None else duracion)

                # Nº de cliente: primero del nombre del archivo — los
                # monitores ya renombran cada descarga "N.K" (cliente N,
                # vídeo K), p. ej. "2.1.mp4" → cliente 2 —; si el archivo no
                # sigue ese patrón, del audio ("número X" del monitor).
                m_nombre = re.match(r"\s*(\d+)\.(\d+)", video.stem)
                det_num = detectar_numero_cliente(tx, t_centro=sug["t_inicio_raw"])
                if m_nombre:
                    orden_sug = m_nombre.group(1)
                    self.cola.put(("log",
                                   (f"  ✓ Nº cliente {orden_sug} "
                                    f"(del nombre del archivo {video.name})\n",
                                    "ok")))
                elif det_num:
                    orden_sug = det_num["numero"]
                    self.cola.put(("log",
                                   (f"  ✓ Nº cliente detectado: {orden_sug} "
                                    f"(«{det_num['texto']}»)\n", "ok")))
                else:
                    orden_sug = ""
                    self.cola.put(("log",
                                   ("  ⚠ No se detectó el «número X» del monitor "
                                    "(ponlo a mano)\n", "aviso")))

                # Storyboard (rejilla 1 frame/s) + onda
                self.cola.put(("log", ("  Generando rejilla de frames...\n", "info")))
                grid_dir = Path(self.tmp_dir) / f"{clave}_grid"
                frames_grid = extraer_frames_grid(
                    video, grid_dir, ancho=ANCHO_FRAME, paso_s=1.0,
                )
                if not frames_grid:
                    # Fallback real: 3 frames sueltos alrededor del t0 sugerido,
                    # mapeados a una mini-rejilla {segundo: ruta} para que el
                    # storyboard los encuentre con frame_mas_cercano (antes el
                    # mensaje lo prometía pero no se extraía ninguno).
                    self.cola.put(("log",
                                   ("  ⚠ Rejilla vacía, usando 3 frames sueltos\n",
                                    "aviso")))
                    sueltos = extraer_3_frames(
                        video, t0_sug, grid_dir, ancho=ANCHO_FRAME, gap_s=5.0)
                    for clave_f, off in (("antes", -5.0), ("inicio", 0.0),
                                         ("despues", 5.0)):
                        ruta = sueltos.get(clave_f)
                        if ruta:
                            frames_grid[int(round(max(0.0, t0_sug + off)))] = ruta
                rms, _ = envolvente_rms(audio_tmp, n_puntos=800)

                clips.append({
                    "video_path": video,
                    "clave": clave,       # prefijo único para archivos temporales
                    "audio_tmp": audio_tmp,
                    "duracion": duracion,
                    "t_inicio_raw": sug["t_inicio_raw"],
                    "t_fin_raw": sug["t_fin_raw"],
                    "texto_inicio": sug["texto_inicio"] or "(no detectado)",
                    "texto_fin": sug["texto_fin"] or "(no detectado)",
                    "t0": t0_sug,
                    "t1": t1_sug,
                    "rms": rms,
                    "frames_grid": frames_grid,
                    "orden": orden_sug,   # nº de cliente detectado (confirmable)
                    "idioma": "es",       # idioma del correo (es/en)
                    "ok": False,
                    "rendered": False,
                    "error": None,
                    "render_error": None,
                })
                self.cola.put(("log",
                               (f"  ✓ Sugerencia: {segundos_a_mmss(t0_sug)} → "
                                f"{segundos_a_mmss(t1_sug)} ({t1_sug - t0_sug:.0f}s)\n",
                                "ok")))
            except Exception as exc:
                import traceback
                self.cola.put(("log",
                               (f"  ✗ Error: {exc}\n{traceback.format_exc()}\n",
                                "error")))
                clips.append(self._clip_error(video, str(exc)))

            self.cola.put(("prog_barra", i))

        self.cola.put(("fin_analisis", clips))

    def _clip_error(self, video, motivo):
        return {
            "video_path": video, "audio_tmp": None, "duracion": 0,
            "t_inicio_raw": None, "t_fin_raw": None,
            "texto_inicio": "", "texto_fin": "",
            "t0": 0.0, "t1": 0.0, "rms": None, "frames_grid": {},
            "orden": "", "idioma": "es",
            "ok": False, "rendered": False, "error": motivo,
            "render_error": None,
        }

    # ─────────────────────────────────────────────────────────────────────
    # FASE 2 — Revisión: lista + detalle con storyboard, onda, controles
    # ─────────────────────────────────────────────────────────────────────
    def _entrar_revision(self, clips):
        self.clips = clips
        # Si no hay clips O todos tienen error → no entramos en revisión;
        # mostramos un resumen con la causa y permitimos volver al inicio.
        clips_validos = [c for c in clips if not c.get("error")]
        if not clips_validos:
            self._mostrar_ui_sin_clips_validos(clips)
            return
        self.fase = "revision"
        self._construir_ui_revision()
        # Seleccionar el primer clip válido
        for i, c in enumerate(clips):
            if c.get("error") is None:
                self._seleccionar_clip(i)
                return

    def _mostrar_ui_sin_clips_validos(self, clips):
        """Pantalla informativa cuando ningún vídeo pasa la fase de análisis."""
        self.fase = "fin"
        self._limpiar_ventana()
        cuerpo = tk.Frame(self, bg=COLORES["fondo"], padx=40, pady=40)
        cuerpo.pack(fill="both", expand=True)
        tk.Label(cuerpo, text="No hay vídeos para revisar",
                 font=(FUENTE, 16, "bold"),
                 bg=COLORES["fondo"], fg=COLORES["error"]).pack(pady=(0, 8))
        if clips:
            tk.Label(
                cuerpo,
                text=(f"Los {len(clips)} vídeo(s) seleccionado(s) fallaron "
                       "durante el análisis. Detalle:"),
                font=(FUENTE, 10), bg=COLORES["fondo"],
                fg=COLORES["texto_suave"]).pack(pady=(0, 14))
            lista = tk.Text(cuerpo, height=12, font=(FUENTE, 9),
                             bg="#1A1A1A", fg="#E0E0E0",
                             relief="flat", padx=10, pady=8)
            lista.pack(fill="both", expand=True)
            for c in clips:
                lista.insert("end",
                              f"✗ {c['video_path'].name}: {c.get('error', '?')}\n")
            lista.config(state="disabled")
        else:
            tk.Label(
                cuerpo,
                text="No se ha analizado ningún vídeo.",
                font=(FUENTE, 10), bg=COLORES["fondo"],
                fg=COLORES["texto_suave"]).pack(pady=(0, 14))

        tk.Button(cuerpo, text="↻ Volver al inicio",
                  font=(FUENTE, 11, "bold"),
                  bg=COLORES["acento"], fg="white",
                  relief="flat", cursor="hand2", padx=18, pady=6,
                  command=self._reset).pack(pady=(20, 0))

    def _construir_ui_revision(self):
        self._limpiar_ventana()

        # Cabecera
        cab = tk.Frame(self, bg=COLORES["acento"], pady=10)
        cab.pack(fill="x")
        tk.Label(cab, text="Revisión rápida — aprueba o ajusta cada clip",
                 font=(FUENTE, 14, "bold"),
                 bg=COLORES["acento"], fg="white").pack(side="left", padx=16)
        self.lbl_atajos = tk.Label(
            cab,
            text="↑↓ navegar  ←→ ±1s inicio  Enter aprobar  Espacio audio  V vídeo",
            font=(FUENTE, 9), bg=COLORES["acento"], fg="#BDD7F5",
        )
        self.lbl_atajos.pack(side="right", padx=16)

        # Cuerpo: izquierda lista, derecha detalle
        cuerpo = tk.Frame(self, bg=COLORES["fondo"])
        cuerpo.pack(fill="both", expand=True)

        # Lista
        izq = tk.Frame(cuerpo, bg=COLORES["panel"], width=260)
        izq.pack(side="left", fill="y", padx=(8, 4), pady=8)
        izq.pack_propagate(False)
        tk.Label(izq, text="Vídeos", font=(FUENTE, 11, "bold"),
                 bg=COLORES["panel"], fg=COLORES["texto"]).pack(pady=(6, 4))

        self.lista_frame = tk.Frame(izq, bg=COLORES["panel"])
        self.lista_frame.pack(fill="both", expand=True, padx=4)
        self._refrescar_lista()

        # Detalle
        der = tk.Frame(cuerpo, bg=COLORES["fondo"])
        der.pack(side="left", fill="both", expand=True, padx=(4, 8), pady=8)

        self.detalle = tk.Frame(der, bg=COLORES["panel"])
        self.detalle.pack(fill="both", expand=True)

        # Footer: contador + botón generar
        pie = tk.Frame(self, bg=COLORES["fondo"], pady=10)
        pie.pack(fill="x", padx=8, pady=(0, 8))
        self.lbl_contador = tk.Label(pie, text="",
                                       font=(FUENTE, 10),
                                       bg=COLORES["fondo"],
                                       fg=COLORES["texto_suave"])
        self.lbl_contador.pack(side="left", padx=16)
        self.btn_render = tk.Button(
            pie, text="Generar vídeos aprobados",
            font=(FUENTE, 11, "bold"), bg=COLORES["ok"], fg="white",
            relief="flat", cursor="hand2", padx=18, pady=6,
            command=self._iniciar_render,
        )
        self.btn_render.pack(side="right", padx=16)
        self._refrescar_contador()

    def _refrescar_lista(self):
        for w in self.lista_frame.winfo_children():
            w.destroy()
        for i, c in enumerate(self.clips):
            fila = tk.Frame(
                self.lista_frame,
                bg=COLORES["panel_sel"] if i == self.clip_sel else COLORES["panel"],
                cursor="hand2", pady=2,
            )
            fila.pack(fill="x", pady=1)
            if c.get("error"):
                icono = "✗"
                color = COLORES["error"]
            elif c.get("ok"):
                icono = "✓"
                color = COLORES["ok"]
            else:
                icono = "•"
                color = COLORES["texto_suave"]
            tk.Label(fila, text=icono, font=(FUENTE, 11, "bold"),
                     bg=fila.cget("bg"), fg=color, width=2).pack(side="left")
            nombre = c["video_path"].name
            if len(nombre) > 22:
                nombre = nombre[:20] + "…"
            tk.Label(fila, text=nombre, font=(FUENTE, 9),
                     bg=fila.cget("bg"), fg=COLORES["texto"],
                     anchor="w").pack(side="left", fill="x", expand=True)
            fila.bind("<Button-1>", lambda e, idx=i: self._seleccionar_clip(idx))
            for child in fila.winfo_children():
                child.bind("<Button-1>", lambda e, idx=i: self._seleccionar_clip(idx))

    def _refrescar_contador(self):
        if not hasattr(self, "lbl_contador"):
            return
        ok = sum(1 for c in self.clips if c.get("ok") and not c.get("error"))
        rev = sum(1 for c in self.clips if not c.get("ok") and not c.get("error"))
        err = sum(1 for c in self.clips if c.get("error"))
        partes = [f"{ok} aprobados", f"{rev} por revisar"]
        if err:
            partes.append(f"{err} con error")
        self.lbl_contador.config(text="  ·  ".join(partes))
        self.btn_render.config(
            text=f"Generar {ok} vídeo(s) aprobados" if ok else "Aprueba al menos uno",
            state="normal" if ok > 0 else "disabled",
            bg=COLORES["ok"] if ok > 0 else COLORES["borde"],
            fg="white" if ok > 0 else COLORES["texto_suave"],
            cursor="hand2" if ok > 0 else "arrow",
        )

    def _seleccionar_clip(self, idx):
        if not (0 <= idx < len(self.clips)):
            return
        self.clip_sel = idx
        self._refrescar_lista()
        self._mostrar_detalle(self.clips[idx])

    def _mostrar_detalle(self, c: dict):
        for w in self.detalle.winfo_children():
            w.destroy()
        self._photos.clear()  # liberar referencias del clip anterior

        cont = tk.Frame(self.detalle, bg=COLORES["panel"], padx=18, pady=14)
        cont.pack(fill="both", expand=True)

        # Nombre + estado
        cab = tk.Frame(cont, bg=COLORES["panel"])
        cab.pack(fill="x", pady=(0, 8))
        tk.Label(cab, text=c["video_path"].name,
                 font=(FUENTE, 12, "bold"),
                 bg=COLORES["panel"], fg=COLORES["texto"]).pack(side="left")

        if c.get("error"):
            tk.Label(cont, text=f"⚠ {c['error']}",
                     font=(FUENTE, 10),
                     bg=COLORES["panel"], fg=COLORES["error"],
                     wraplength=820, justify="left",
                     ).pack(anchor="w", pady=10)
            return

        # ── Storyboard de 3 frames (refrescable desde rejilla) ──
        storyboard = tk.Frame(cont, bg=COLORES["panel"])
        storyboard.pack(pady=(0, 10))
        self._storyboard_labels = {}
        etiquetas = [
            ("antes", "-5s antes"),
            ("inicio", "INICIO"),
            ("despues", "+5s después"),
        ]
        for clave, texto in etiquetas:
            celda = tk.Frame(storyboard, bg=COLORES["panel"], padx=6)
            celda.pack(side="left")
            tk.Label(celda, text=texto,
                     font=(FUENTE, 9, "bold"),
                     bg=COLORES["panel"],
                     fg=COLORES["acento"] if clave == "inicio" else COLORES["texto_suave"],
                     ).pack()
            lbl_img = tk.Label(celda, bg=COLORES["borde"],
                                width=int(ANCHO_FRAME / 8),
                                height=int(ALTO_FRAME / 16),
                                fg=COLORES["texto_suave"], font=(FUENTE, 9))
            lbl_img.pack()
            self._storyboard_labels[clave] = lbl_img
        self._refrescar_storyboard(c, c["t0"])

        # ── Waveform ──
        wf_frame = tk.Frame(cont, bg=COLORES["panel"])
        wf_frame.pack(fill="x", pady=(4, 10))
        self._dibujar_waveform(wf_frame, c)

        # ── Controles ──
        controles = tk.Frame(cont, bg=COLORES["panel"])
        controles.pack(fill="x", pady=(0, 6))

        # Variables editables del clip seleccionado
        self.var_t0 = tk.StringVar(value=segundos_a_mmss(c["t0"]))
        self.var_t1 = tk.StringVar(value=segundos_a_mmss(c["t1"]))

        fila_ini = tk.Frame(controles, bg=COLORES["panel"])
        fila_ini.pack(fill="x", pady=2)
        tk.Label(fila_ini, text="Inicio", font=(FUENTE, 10, "bold"),
                 bg=COLORES["panel"], fg=COLORES["marcador_ini"], width=7,
                 anchor="w").pack(side="left")
        ent_t0 = tk.Entry(fila_ini, textvariable=self.var_t0, width=8,
                           font=(FUENTE, 11), justify="center")
        ent_t0.pack(side="left", padx=4)
        ent_t0.bind("<Return>", lambda e: self._aplicar_tiempos())
        tk.Button(fila_ini, text="−5s", font=(FUENTE, 9),
                  bg=COLORES["fondo"], relief="flat", cursor="hand2",
                  command=lambda: self._ajustar_t0(-5)).pack(side="left", padx=2)
        tk.Button(fila_ini, text="+5s", font=(FUENTE, 9),
                  bg=COLORES["fondo"], relief="flat", cursor="hand2",
                  command=lambda: self._ajustar_t0(+5)).pack(side="left", padx=2)
        tk.Label(fila_ini, text=f"  pista: {c.get('texto_inicio') or '—'}",
                 font=(FUENTE, 9), bg=COLORES["panel"],
                 fg=COLORES["texto_suave"]).pack(side="left", padx=8)

        fila_fin = tk.Frame(controles, bg=COLORES["panel"])
        fila_fin.pack(fill="x", pady=2)
        tk.Label(fila_fin, text="Fin", font=(FUENTE, 10, "bold"),
                 bg=COLORES["panel"], fg=COLORES["marcador_fin"], width=7,
                 anchor="w").pack(side="left")
        ent_t1 = tk.Entry(fila_fin, textvariable=self.var_t1, width=8,
                           font=(FUENTE, 11), justify="center")
        ent_t1.pack(side="left", padx=4)
        ent_t1.bind("<Return>", lambda e: self._aplicar_tiempos())
        tk.Button(fila_fin, text="−5s", font=(FUENTE, 9),
                  bg=COLORES["fondo"], relief="flat", cursor="hand2",
                  command=lambda: self._ajustar_t1(-5)).pack(side="left", padx=2)
        tk.Button(fila_fin, text="+5s", font=(FUENTE, 9),
                  bg=COLORES["fondo"], relief="flat", cursor="hand2",
                  command=lambda: self._ajustar_t1(+5)).pack(side="left", padx=2)
        tk.Label(fila_fin, text=f"  pista: {c.get('texto_fin') or '—'}",
                 font=(FUENTE, 9), bg=COLORES["panel"],
                 fg=COLORES["texto_suave"]).pack(side="left", padx=8)

        # Duración
        dur = c["t1"] - c["t0"]
        self.lbl_duracion = tk.Label(
            controles,
            text=f"Duración del corte: {dur:.0f}s",
            font=(FUENTE, 10),
            bg=COLORES["panel"], fg=COLORES["texto_suave"],
        )
        self.lbl_duracion.pack(anchor="w", pady=(6, 0))

        # ── Datos de envío: Nº de cliente (ORDEN) + idioma del correo ──
        # Se capturan aquí, al revisar (donde ya se oye el número en el audio).
        # El envío en sí se hace en la Fase 3, cuando el vídeo final existe.
        envio = tk.Frame(controles, bg=COLORES["panel"])
        envio.pack(fill="x", pady=(8, 0))
        tk.Label(envio, text="Nº cliente", font=(FUENTE, 10, "bold"),
                 bg=COLORES["panel"], fg=COLORES["texto"], width=10,
                 anchor="w").pack(side="left")
        self.var_orden = tk.StringVar(value=c.get("orden", ""))
        self.var_orden.trace_add(
            "write",
            lambda *_: self._guardar_orden())
        tk.Entry(envio, textvariable=self.var_orden, width=6,
                 font=(FUENTE, 11), justify="center").pack(side="left", padx=4)
        tk.Label(envio, text="  Idioma:", font=(FUENTE, 10),
                 bg=COLORES["panel"], fg=COLORES["texto"]).pack(side="left",
                                                                padx=(12, 2))
        self.var_idioma = tk.StringVar(value=c.get("idioma", "es"))
        for cod, etiq in (("es", "Español"), ("en", "English")):
            tk.Radiobutton(
                envio, text=etiq, value=cod, variable=self.var_idioma,
                font=(FUENTE, 10), bg=COLORES["panel"], fg=COLORES["texto"],
                selectcolor=COLORES["panel"], activebackground=COLORES["panel"],
                command=self._guardar_idioma,
            ).pack(side="left", padx=2)

        # Acciones
        acciones = tk.Frame(cont, bg=COLORES["panel"])
        acciones.pack(fill="x", pady=(10, 0))

        tk.Button(acciones, text="🔊 Audio (Espacio)",
                  font=(FUENTE, 10), bg=COLORES["fondo"],
                  fg=COLORES["texto"], relief="flat", cursor="hand2",
                  command=self._reproducir_audio).pack(side="left", padx=(0, 6))
        tk.Button(acciones, text="🎬 Ver vídeo (V)",
                  font=(FUENTE, 10), bg=COLORES["fondo"],
                  fg=COLORES["texto"], relief="flat", cursor="hand2",
                  command=self._abrir_video_externo).pack(side="left", padx=6)

        self.btn_ok = tk.Button(
            acciones,
            text="✓ Aprobar (Enter)" if not c.get("ok") else "✓ Aprobado · desmarcar",
            font=(FUENTE, 11, "bold"),
            bg=COLORES["ok"] if not c.get("ok") else COLORES["aviso"],
            fg="white", relief="flat", cursor="hand2", padx=14, pady=4,
            command=self._toggle_ok,
        )
        self.btn_ok.pack(side="right")

    def _dibujar_waveform(self, parent, c: dict):
        rms = c.get("rms")
        duracion = c["duracion"]
        if rms is None or len(rms) == 0 or duracion <= 0:
            tk.Label(parent, text="(sin onda disponible)",
                     bg=COLORES["panel"],
                     fg=COLORES["texto_suave"]).pack()
            return

        fig = Figure(figsize=(10, 1.6), dpi=80)
        fig.subplots_adjust(left=0.04, right=0.99, top=0.92, bottom=0.22)
        ax = fig.add_subplot(111)

        n = len(rms)
        x = [i * duracion / n for i in range(n)]
        ax.fill_between(x, 0, rms, color=COLORES["acento"], alpha=0.55, linewidth=0)
        ax.plot(x, rms, color=COLORES["acento"], linewidth=0.6)

        # Marcadores de inicio y fin (guardamos las refs para movernos sin
        # redibujar la onda entera al ajustar t0/t1).
        self._wf_marker_ini = ax.axvline(
            c["t0"], color=COLORES["marcador_ini"], linewidth=2)
        self._wf_marker_fin = ax.axvline(
            c["t1"], color=COLORES["marcador_fin"], linewidth=2)

        ax.set_xlim(0, duracion)
        ax.set_ylim(0, max(0.05, float(rms.max()) * 1.05))
        ax.set_yticks([])
        ax.set_xlabel("tiempo (s)", fontsize=8)
        ax.tick_params(axis="x", labelsize=8)
        ax.grid(axis="x", color="#E0E0E0", linewidth=0.5)
        ax.set_facecolor(COLORES["panel"])
        fig.patch.set_facecolor(COLORES["panel"])

        self._wf_canvas = FigureCanvasTkAgg(fig, master=parent)
        widget = self._wf_canvas.get_tk_widget()
        widget.pack(fill="x")
        widget.bind("<Button-1>",
                    lambda e: self._click_waveform(e, ax, duracion))
        self._wf_fig = fig
        self._wf_ax = ax
        self._wf_canvas.draw()

    def _click_waveform(self, evt, ax, duracion):
        """Click izquierdo → mueve t_inicio; Shift+click → mueve t_fin."""
        widget = evt.widget
        ancho = widget.winfo_width()
        if ancho <= 0:
            return
        # Convertir coords de tkinter a coords de datos vía matplotlib
        x_norm = evt.x / ancho
        # ax tiene margen — usamos su posición en la figura
        bbox = ax.get_position()
        x_dentro = (x_norm - bbox.x0) / (bbox.x1 - bbox.x0)
        if not (0.0 <= x_dentro <= 1.0):
            return
        t = x_dentro * duracion
        c = self.clips[self.clip_sel]
        if evt.state & 0x0001:  # Shift
            c["t1"] = max(c["t0"] + 0.5, min(duracion, t))
        else:
            c["t0"] = max(0.0, min(c["t1"] - 0.5, t))
        self.var_t0.set(segundos_a_mmss(c["t0"]))
        self.var_t1.set(segundos_a_mmss(c["t1"]))
        self._actualizar_marcadores_waveform(c)
        self._actualizar_duracion_label(c)
        self._refrescar_storyboard(c, c["t0"])

    def _actualizar_marcadores_waveform(self, c: dict):
        if not hasattr(self, "_wf_marker_ini"):
            return
        self._wf_marker_ini.set_xdata([c["t0"], c["t0"]])
        self._wf_marker_fin.set_xdata([c["t1"], c["t1"]])
        self._wf_canvas.draw_idle()

    def _actualizar_duracion_label(self, c: dict):
        if hasattr(self, "lbl_duracion"):
            self.lbl_duracion.config(
                text=f"Duración del corte: {c['t1'] - c['t0']:.0f}s")

    # ── Acciones sobre el clip seleccionado ──
    def _clip_actual(self):
        if 0 <= self.clip_sel < len(self.clips):
            c = self.clips[self.clip_sel]
            if not c.get("error"):
                return c
        return None

    def _ajustar_t0(self, delta):
        c = self._clip_actual()
        if c is None:
            return
        c["t0"] = max(0.0, min(c["t1"] - 0.5, c["t0"] + delta))
        self.var_t0.set(segundos_a_mmss(c["t0"]))
        self._actualizar_marcadores_waveform(c)
        self._actualizar_duracion_label(c)
        self._refrescar_storyboard(c, c["t0"])

    def _ajustar_t1(self, delta):
        c = self._clip_actual()
        if c is None:
            return
        c["t1"] = max(c["t0"] + 0.5, min(c["duracion"], c["t1"] + delta))
        self.var_t1.set(segundos_a_mmss(c["t1"]))
        self._actualizar_marcadores_waveform(c)
        self._actualizar_duracion_label(c)

    def _aplicar_tiempos(self):
        """Lee los Entry y actualiza el clip."""
        c = self._clip_actual()
        if c is None:
            return
        try:
            t0 = _mmss_a_segundos(self.var_t0.get())
            t1 = _mmss_a_segundos(self.var_t1.get())
        except ValueError:
            messagebox.showwarning("Formato", "Usa formato m:ss (ej. 1:42)")
            return
        if t1 - t0 < 0.5:
            messagebox.showwarning("Tiempos", "El fin debe ser posterior al inicio")
            return
        c["t0"] = max(0.0, min(c["duracion"] - 0.5, t0))
        c["t1"] = max(c["t0"] + 0.5, min(c["duracion"], t1))
        self._actualizar_marcadores_waveform(c)
        self._actualizar_duracion_label(c)
        self._refrescar_storyboard(c, c["t0"])

    def _sync_entries_silencioso(self):
        """Vuelca los Entry de Inicio/Fin del clip mostrado al modelo, sin
        popups de error. Para edición de última hora antes de renderizar; si el
        formato es inválido se ignora (el clip conserva sus tiempos previos)."""
        c = self._clip_actual()
        if c is None or not hasattr(self, "var_t0"):
            return
        try:
            t0 = _mmss_a_segundos(self.var_t0.get())
            t1 = _mmss_a_segundos(self.var_t1.get())
        except ValueError:
            return
        if t1 - t0 < 0.5:
            return
        c["t0"] = max(0.0, min(c["duracion"] - 0.5, t0))
        c["t1"] = max(c["t0"] + 0.5, min(c["duracion"], t1))

    def _guardar_orden(self):
        c = self._clip_actual()
        if c is not None and hasattr(self, "var_orden"):
            c["orden"] = self.var_orden.get().strip()

    def _guardar_idioma(self):
        c = self._clip_actual()
        if c is not None and hasattr(self, "var_idioma"):
            c["idioma"] = self.var_idioma.get()

    def _refrescar_storyboard(self, c: dict, t_centro: float):
        """Actualiza las 3 miniaturas (t-2, t, t+2) desde la rejilla pre-extraída.

        Si no hay rejilla, deja los placeholders. Mantiene una caché de PhotoImages
        en self._photos para evitar que el GC libere la imagen mostrada.
        """
        labels = getattr(self, "_storyboard_labels", None)
        if not labels:
            return
        grid = c.get("frames_grid") or {}
        offsets = {"antes": -5.0, "inicio": 0.0, "despues": 5.0}
        for clave, lbl in labels.items():
            t = max(0.0, t_centro + offsets[clave])
            path = frame_mas_cercano(grid, t)
            if path and Path(path).exists():
                try:
                    img = Image.open(path)
                    img.thumbnail((ANCHO_FRAME, ALTO_FRAME), Image.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                except Exception:
                    photo = None
            else:
                photo = None
            if photo is not None:
                self._photos[clave] = photo  # ancla anti-GC
                lbl.config(image=photo, text="", width=0, height=0)
            else:
                lbl.config(image="", text="(sin frame)",
                           width=int(ANCHO_FRAME / 8),
                           height=int(ALTO_FRAME / 16))

    def _toggle_ok(self):
        c = self._clip_actual()
        if c is None:
            return
        self._aplicar_tiempos()  # asegura que los Entry editados se aplican
        c["ok"] = not c.get("ok")
        self._refrescar_lista()
        self._refrescar_contador()
        # Refrescar botón de aprobación
        self._mostrar_detalle(c)

    def _reproducir_audio(self):
        """Espacio → reproduce 6 s del audio centrado en t0."""
        c = self._clip_actual()
        if c is None or c.get("audio_tmp") is None:
            return
        t0_pre = max(0.0, c["t0"] - PREVIEW_DUR_S / 2)
        clave = c.get("clave") or c["video_path"].stem
        snippet = self.tmp_dir / f"_preview_{clave}.wav"
        # Detener la reproducción anterior: winsound mantiene el WAV abierto
        # mientras suena y ffmpeg no puede sobrescribirlo — pulsar Espacio
        # otra vez antes de que acabe se quedaba mudo.
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except RuntimeError:
            pass
        if extraer_audio_snippet(c["audio_tmp"], t0_pre, PREVIEW_DUR_S, snippet):
            try:
                winsound.PlaySound(
                    str(snippet),
                    winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                )
            except RuntimeError:
                pass

    def _abrir_video_externo(self):
        """V → genera clip de 6 s y abre con reproductor por defecto."""
        c = self._clip_actual()
        if c is None:
            return
        t0_pre = max(0.0, c["t0"] - PREVIEW_DUR_S / 2)
        clave = c.get("clave") or c["video_path"].stem
        clip_out = self.tmp_dir / f"_preview_{clave}.mp4"
        ok = extraer_clip_preview(c["video_path"], t0_pre, PREVIEW_DUR_S, clip_out)
        if ok:
            try:
                os.startfile(str(clip_out))
            except OSError as e:
                messagebox.showerror("Reproductor",
                                      f"No se pudo abrir el preview: {e}")

    def _aprobar_y_siguiente(self):
        c = self._clip_actual()
        if c is None:
            return
        self._aplicar_tiempos()
        c["ok"] = True
        self._refrescar_lista()
        self._refrescar_contador()
        # Avanzar al primer no-aprobado
        n = len(self.clips)
        for j in range(1, n + 1):
            idx = (self.clip_sel + j) % n
            if (not self.clips[idx].get("ok")
                    and not self.clips[idx].get("error")):
                self._seleccionar_clip(idx)
                return
        # Todos aprobados: refrescar la vista del actual
        self._mostrar_detalle(c)

    def _navegar(self, delta):
        if not self.clips:
            return
        n = len(self.clips)
        nuevo = (self.clip_sel + delta) % n
        self._seleccionar_clip(nuevo)

    # ─────────────────────────────────────────────────────────────────────
    # FASE 3 — Render: genera vídeos aprobados
    # ─────────────────────────────────────────────────────────────────────
    def _iniciar_render(self):
        # Aplica cualquier edición pendiente en los Entry del clip mostrado:
        # si el usuario ajusta Inicio/Fin y pulsa "Generar" sin confirmar con
        # Enter, sin esto se renderizaría con los tiempos antiguos.
        self._sync_entries_silencioso()
        aprobados = [c for c in self.clips
                     if c.get("ok") and not c.get("error")]
        if not aprobados:
            return
        if self.fase == "rendering":
            return
        self.fase = "rendering"
        CARPETA_SALIDA.mkdir(exist_ok=True)
        self._construir_ui_rendering(len(aprobados))
        threading.Thread(target=self._worker_render,
                         args=(aprobados,), daemon=True).start()

    def _construir_ui_rendering(self, total):
        self._limpiar_ventana()
        cuerpo = tk.Frame(self, bg=COLORES["fondo"], padx=40, pady=40)
        cuerpo.pack(fill="both", expand=True)
        tk.Label(cuerpo, text="Generando vídeos finales...",
                 font=(FUENTE, 16, "bold"),
                 bg=COLORES["fondo"], fg=COLORES["texto"]).pack(pady=(0, 8))
        self.lbl_prog = tk.Label(cuerpo, text="Preparando...",
                                  font=(FUENTE, 10),
                                  bg=COLORES["fondo"],
                                  fg=COLORES["texto_suave"])
        self.lbl_prog.pack(pady=(0, 16))
        self.barra = ttk.Progressbar(cuerpo, orient="horizontal",
                                      length=720, mode="determinate",
                                      maximum=total)
        self.barra.pack(pady=(0, 24))
        self.log = tk.Text(cuerpo, height=16, font=(FUENTE, 9),
                           bg="#1A1A1A", fg="#E0E0E0",
                           relief="flat", padx=10, pady=8)
        self.log.pack(fill="both", expand=True)
        for nivel, color in [("ok", "#7DD081"), ("aviso", "#F0B848"),
                              ("error", "#E84A4A"), ("info", "#E0E0E0"),
                              ("header", "#7AB7FF")]:
            self.log.tag_config(nivel, foreground=color)
        self.log.config(state="disabled")

    @staticmethod
    def _carpeta_salida_hoy():
        """Subcarpeta de salida por día (ej. salida/11-06-2026).

        Los nombres comerciales "1.1 SUNVIEW PARK" se repiten entre días
        porque el ORDEN se reinicia cada mañana; separar por fecha evita que
        el vídeo de hoy pise el de ayer.
        """
        carpeta = CARPETA_SALIDA / datetime.date.today().strftime("%d-%m-%Y")
        carpeta.mkdir(parents=True, exist_ok=True)
        return carpeta

    @staticmethod
    def _salida_para(c, carpeta, usados):
        """Nombre comercial: nombre original + " SUNVIEW PARK".

        Los monitores ya nombran cada descarga "N.K" (cliente N, vídeo K);
        a la app solo le toca añadir la marca: "2.1.mp4" → "2.1 SUNVIEW
        PARK.mp4". Si el nombre se repite dentro del lote (mismo archivo en
        dos tarjetas), se añade (2), (3)… para no pisar al primero.
        Reprocesar un vídeo en otra sesión sí reemplaza su salida
        (comportamiento esperado).
        """
        stem = c["video_path"].stem.strip()
        base = carpeta / f"{stem} SUNVIEW PARK.mp4"
        if base not in usados:
            return base
        n = 2
        while True:
            cand = carpeta / f"{stem} SUNVIEW PARK ({n}).mp4"
            if cand not in usados:
                return cand
            n += 1

    def _worker_render(self, aprobados):
        usados: set = set()
        carpeta = self._carpeta_salida_hoy()
        for i, c in enumerate(aprobados, 1):
            self.cola.put(("prog_label",
                           f"[{i}/{len(aprobados)}] {c['video_path'].name}"))
            self.cola.put(("log",
                           (f"\n── {c['video_path'].name} ──\n", "header")))
            salida = self._salida_para(c, carpeta, usados)
            usados.add(salida)
            try:
                editar_video(c["video_path"], c["t0"], c["t1"], salida)
                self.cola.put(("log",
                               (f"  ✓ Generado: {salida.name}\n", "ok")))
                checks = verificar_corte(salida)
                for nombre, chk_ok, det in checks:
                    self.cola.put((
                        "log",
                        (f"    {'✓' if chk_ok else '⚠'} {nombre}: {det}\n",
                         "ok" if chk_ok else "aviso"),
                    ))
                c["rendered"] = True
                c["salida"] = salida
            except Exception as exc:
                self.cola.put(("log", (f"  ✗ Error: {exc}\n", "error")))
                salida.unlink(missing_ok=True)
                c["render_error"] = str(exc)
            self.cola.put(("prog_barra", i))
        self.cola.put(("fin_render", aprobados))

    def _on_fin_render(self, aprobados):
        ok = sum(1 for c in aprobados if c.get("rendered"))
        err = [c for c in aprobados if c.get("render_error")]
        self.lbl_prog.config(text=f"Completado — {ok}/{len(aprobados)} vídeos generados")
        if err:
            self._append_log(f"\n⚠ {len(err)} vídeo(s) fallaron al renderizar:\n", "aviso")
            for c in err:
                self._append_log(f"  · {c['video_path'].name}: {c['render_error']}\n",
                                  "error")
        renderizados = [c for c in aprobados if c.get("rendered")]

        # El render terminó: encoger su registro para dejar sitio al panel
        # de entrega (el detalle sigue ahí, con scroll).
        self.log.config(height=5)

        # Botón final para volver al inicio + abrir carpeta. Se empaqueta
        # ANTES que el panel de entrega y anclado abajo: en pack, los widgets
        # tardíos son los primeros en quedarse sin sitio, y este pie quedaba
        # fuera de la ventana cuando la lista de vídeos era larga.
        pie = tk.Frame(self, bg=COLORES["fondo"])
        pie.pack(side="bottom", fill="x", pady=10)
        tk.Button(pie, text="📂 Abrir carpeta salida/",
                  font=(FUENTE, 10), bg=COLORES["acento"], fg="white",
                  relief="flat", cursor="hand2", padx=14, pady=6,
                  command=self._abrir_carpeta).pack(side="left", padx=16)
        tk.Button(pie, text="↻ Procesar más vídeos",
                  font=(FUENTE, 10), bg=COLORES["fondo"], fg=COLORES["acento"],
                  relief="flat", cursor="hand2", padx=14, pady=6,
                  command=self._reset).pack(side="right", padx=16)

        if renderizados:
            self._construir_panel_entrega(renderizados)

    # ─────────────────────────────────────────────────────────────────────
    # FASE 3b — Entrega: por cada vídeo, subir a WeTransfer + abrir Gmail
    # ─────────────────────────────────────────────────────────────────────
    def _construir_panel_entrega(self, clips):
        """Lista scrollable: una fila por vídeo con Nº cliente + botones de envío."""
        marco = tk.LabelFrame(
            self, text=" Entrega por correo ",
            font=(FUENTE, 11, "bold"), bg=COLORES["fondo"], fg=COLORES["texto"],
            padx=8, pady=8,
        )
        marco.pack(fill="both", expand=True, padx=16, pady=(4, 0))

        metodo = envio_correo.metodo_entrega()
        if metodo == "manual":
            aviso = ("«Subir vídeo» abre WeTransfer y tú arrastras el archivo; "
                     "copia el link y pulsa «Abrir correo».")
        else:
            destino = "Google Drive" if metodo == "drive" else "Gofile"
            aviso = (f"«Subir vídeo» sube a {destino} automáticamente; "
                     "«Abrir correo» junta TODOS los enlaces del mismo Nº de "
                     "cliente en un solo correo (sube primero todos los suyos).")
        if not clientes.configurado():
            aviso += " (Sheets sin configurar: el email habrá que ponerlo a mano.)"
        tk.Label(marco, text=aviso, font=(FUENTE, 9),
                 bg=COLORES["fondo"], fg=COLORES["texto_suave"],
                 anchor="w").pack(fill="x", pady=(0, 6))

        # Fecha de los vídeos (para localizar el bloque/pestaña en el Sheets).
        # Por defecto HOY; solo se cambia si se procesan vídeos de otro día.
        fila_fecha = tk.Frame(marco, bg=COLORES["fondo"])
        fila_fecha.pack(fill="x", pady=(0, 6))
        tk.Label(fila_fecha, text="Fecha de los vídeos:", font=(FUENTE, 9),
                 bg=COLORES["fondo"], fg=COLORES["texto"]).pack(side="left")
        self.var_fecha_entrega = tk.StringVar(
            value=datetime.date.today().strftime("%d/%m/%Y"))
        tk.Entry(fila_fecha, textvariable=self.var_fecha_entrega, width=12,
                 font=(FUENTE, 10), justify="center").pack(side="left", padx=6)
        tk.Label(fila_fecha, text="(dd/mm/aaaa — normalmente hoy)",
                 font=(FUENTE, 8), bg=COLORES["fondo"],
                 fg=COLORES["texto_suave"]).pack(side="left")

        # Canvas + scrollbar para soportar muchos vídeos
        cont = tk.Frame(marco, bg=COLORES["fondo"])
        cont.pack(fill="both", expand=True)
        canvas = tk.Canvas(cont, bg=COLORES["fondo"], highlightthickness=0, height=220)
        sb = tk.Scrollbar(cont, orient="vertical", command=canvas.yview)
        interior = tk.Frame(canvas, bg=COLORES["fondo"])
        interior.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=interior, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._entrega_vars = {}  # id(clip) -> StringVar del Nº cliente
        self._clips_entrega = clips  # para agrupar links por Nº de cliente
        for c in clips:
            self._fila_entrega(interior, c)

    def _fila_entrega(self, parent, c):
        fila = tk.Frame(parent, bg=COLORES["panel"], padx=8, pady=6)
        fila.pack(fill="x", pady=2)

        tk.Label(fila, text=c["salida"].name, font=(FUENTE, 10),
                 bg=COLORES["panel"], fg=COLORES["texto"], width=28,
                 anchor="w").pack(side="left")

        tk.Label(fila, text="Nº", font=(FUENTE, 10, "bold"),
                 bg=COLORES["panel"], fg=COLORES["texto"]).pack(side="left",
                                                                padx=(8, 2))
        var = tk.StringVar(value=c.get("orden", ""))
        var.trace_add("write", lambda *_a, cc=c, v=var: cc.update(orden=v.get().strip()))
        self._entrega_vars[id(c)] = var
        tk.Entry(fila, textvariable=var, width=5, font=(FUENTE, 11),
                 justify="center").pack(side="left", padx=2)

        idi = tk.Label(fila, text=("EN" if c.get("idioma") == "en" else "ES"),
                       font=(FUENTE, 9), bg=COLORES["panel"],
                       fg=COLORES["texto_suave"])
        idi.pack(side="left", padx=6)

        estado = tk.Label(fila, text="", font=(FUENTE, 9),
                          bg=COLORES["panel"], fg=COLORES["texto_suave"])
        estado.pack(side="right", padx=(6, 0))

        tk.Button(fila, text="✉️ Abrir correo", font=(FUENTE, 10),
                  bg=COLORES["acento"], fg="white", relief="flat",
                  cursor="hand2", padx=10,
                  command=lambda cc=c, st=estado: self._abrir_correo_cliente(cc, st),
                  ).pack(side="right", padx=4)
        tk.Button(fila, text="📤 Subir vídeo", font=(FUENTE, 10),
                  bg=COLORES["fondo"], fg=COLORES["texto"], relief="flat",
                  cursor="hand2", padx=10,
                  command=lambda cc=c, st=estado: self._subir_video(cc, st),
                  ).pack(side="right", padx=4)

    def _subir_video(self, c, estado_lbl):
        """Sube el vídeo en segundo plano y guarda el enlace de descarga.

        Método según config: gofile (defecto, sin configuración), drive
        (cuenta de servicio) o manual (abre WeTransfer + explorador).
        """
        metodo = envio_correo.metodo_entrega()
        if metodo == "drive" and not envio_correo.drive_configurado():
            metodo = "manual"
        if metodo == "manual":
            envio_correo.abrir_wetransfer()
            envio_correo.abrir_carpeta_seleccionando(c["salida"])
            estado_lbl.config(
                text="WeTransfer abierto → arrastra el vídeo y copia el link",
                fg=COLORES["acento"])
            return
        if c.get("_subiendo"):
            return
        c["_subiendo"] = True
        estado_lbl.config(text="Subiendo… 0%", fg=COLORES["acento"])
        subir = (envio_correo.subir_video_drive if metodo == "drive"
                 else envio_correo.subir_video_gofile)
        threading.Thread(target=self._worker_subir,
                         args=(c, estado_lbl, subir, metodo),
                         daemon=True).start()

    def _worker_subir(self, c, estado_lbl, subir, metodo):
        """Hilo de subida: progreso y resultado via cola (tk solo en main thread)."""
        def _progreso(pct):
            self.cola.put(("entrega_estado",
                           (estado_lbl, f"Subiendo… {pct}%", COLORES["acento"])))
        try:
            link = subir(c["salida"], progreso=_progreso)
            c["link"] = link
            self.cola.put(("entrega_link", (c, estado_lbl, link)))
            if metodo == "drive":
                # Mantenimiento: borrar del Drive los vídeos antiguos
                envio_correo.limpiar_drive_antiguos()
        except envio_correo.EnvioError as exc:
            self.cola.put(("entrega_estado",
                           (estado_lbl, f"⚠ {exc}", COLORES["error"])))
        except Exception as exc:  # red caída, timeout…
            self.cola.put(("entrega_estado",
                           (estado_lbl, f"⚠ Error de red subiendo: {exc}",
                            COLORES["error"])))
        finally:
            c["_subiendo"] = False

    def _on_entrega_estado(self, estado_lbl, texto, color):
        try:
            estado_lbl.config(text=texto, fg=color)
        except tk.TclError:
            pass  # el panel se cerró mientras subía

    def _on_entrega_link(self, c, estado_lbl, link):
        try:
            self.clipboard_clear()
            self.clipboard_append(link)
        except tk.TclError:
            pass
        self._on_entrega_estado(
            estado_lbl,
            "✓ Subido — link copiado, pulsa «Abrir correo»",
            COLORES["ok"])

    def _fecha_entrega(self):
        """Lee la fecha del panel de entrega (dd/mm/aaaa); si falla, hoy."""
        txt = ""
        if hasattr(self, "var_fecha_entrega"):
            txt = self.var_fecha_entrega.get().strip()
        try:
            d, m, a = txt.split("/")
            return datetime.date(int(a), int(m), int(d))
        except (ValueError, AttributeError):
            return datetime.date.today()

    def _abrir_correo_cliente(self, c, estado_lbl):
        """Busca el email por Nº cliente y abre Gmail con sus enlaces.

        Cada vídeo se sube por separado (un enlace por vídeo), pero un
        cliente con varios vídeos recibe UN solo correo con todos sus
        enlaces, uno por línea.
        """
        orden = (c.get("orden") or "").strip()
        fecha = self._fecha_entrega()
        # 1) Email del cliente (desde el Sheets, si está configurado).
        #    Si no se consigue, guardamos el MOTIVO para avisarlo de forma
        #    bien visible (antes solo iba a una etiqueta pequeña que pasaba
        #    desapercibida: el correo se abría sin destinatario y nadie sabía
        #    por qué).
        email = ""
        motivo_sin_email = ""
        if not orden:
            motivo_sin_email = (
                "Esta fila no tiene Nº de cliente.\n\n"
                "Escríbelo en el campo «Nº» de la fila y vuelve a pulsar "
                "«Abrir correo».")
        elif not clientes.configurado():
            motivo_sin_email = (
                "La conexión con el Google Sheets no está configurada en este "
                "PC, así que no puedo buscar el email.\n\n"
                "Tendrás que escribirlo a mano en el correo.")
        else:
            try:
                info = clientes.buscar_cliente(orden, fecha)
                email = info.get("email", "")
                if not email:
                    motivo_sin_email = (
                        f"Encontré al cliente Nº {orden} del {fecha:%d/%m/%Y} "
                        f"en la hoja «{info.get('hoja', '?')}», pero su casilla "
                        "de email está vacía.\n\nComprueba el Sheets o escribe "
                        "el email a mano.")
            except clientes.ClientesError as exc:
                motivo_sin_email = str(exc)

        if email:
            estado_lbl.config(text=f"✓ Email: {email}", fg=COLORES["ok"])
        else:
            estado_lbl.config(
                text=f"⚠ Sin email (Nº {orden or '—'}, {fecha:%d/%m/%Y})",
                fg=COLORES["aviso"])

        # 2) Enlaces de descarga: todos los vídeos del lote con este mismo
        #    Nº de cliente van en el mismo correo (subidas separadas, un
        #    solo envío). Sin Nº, solo el vídeo de esta fila.
        if orden:
            grupo = [cc for cc in getattr(self, "_clips_entrega", [c])
                     if (cc.get("orden") or "").strip() == orden]
        else:
            grupo = [c]
        links = [cc["link"] for cc in grupo if cc.get("link")]
        pendientes = len(grupo) - len(links)
        if links and pendientes:
            if not messagebox.askyesno(
                    "Faltan vídeos por subir",
                    f"El cliente {orden} tiene {len(grupo)} vídeo(s) en este "
                    f"lote pero solo {len(links)} subido(s).\n\n"
                    "¿Abrir el correo solo con los enlaces ya subidos?"):
                estado_lbl.config(
                    text=f"⚠ Sube los {pendientes} vídeo(s) que faltan y vuelve a pulsar",
                    fg=COLORES["aviso"])
                return
        if not links:
            # Flujo manual (WeTransfer): un único link desde el portapapeles.
            try:
                posible = self.clipboard_get()
                if envio_correo.parece_enlace_wetransfer(posible):
                    links = [posible.strip()]
            except tk.TclError:
                pass  # portapapeles vacío o no-texto

        # Aviso BIEN visible si el Sheets está configurado pero aun así no se
        # consiguió el email: el correo se abrirá sin destinatario y conviene
        # que el operador sepa por qué (y lo escriba a mano). En PCs sin
        # Sheets el flujo manual es lo normal, así que no molestamos.
        if not email and clientes.configurado():
            messagebox.showwarning(
                "No pude poner el email automáticamente", motivo_sin_email)

        # 3) Abrir el webmail (Gmail u Outlook según remitente) ya redactado
        asunto, cuerpo = envio_correo.redactar_correo(
            c.get("idioma", "es"), "", links or None)
        envio_correo.abrir_correo_redactado(email, asunto, cuerpo)
        if len(links) > 1:
            estado_lbl.config(
                text=f"Correo abierto con los {len(links)} enlaces del cliente → revisa y envía",
                fg=COLORES["ok"])
        elif links:
            estado_lbl.config(text="Correo abierto con el link puesto → revisa y envía",
                              fg=COLORES["ok"])
        elif estado_lbl.cget("text") in ("", None):
            estado_lbl.config(text="Correo abierto (pega el link y envía)",
                              fg=COLORES["acento"])

    def _abrir_carpeta(self):
        hoy = CARPETA_SALIDA / datetime.date.today().strftime("%d-%m-%Y")
        try:
            os.startfile(str(hoy if hoy.exists() else CARPETA_SALIDA))
        except OSError:
            pass

    def _reset(self):
        self.videos = []
        self.clips = []
        self.clip_sel = -1
        self.fase = "inicio"
        self._construir_ui_inicio()

    # ─────────────────────────────────────────────────────────────────────
    # Atajos de teclado globales
    # ─────────────────────────────────────────────────────────────────────
    def _bind_atajos(self):
        self.bind("<Up>", self._atajo(lambda: self._navegar(-1)))
        self.bind("<Down>", self._atajo(lambda: self._navegar(+1)))
        self.bind("<Left>", self._atajo(lambda: self._ajustar_t0(-1)))
        self.bind("<Right>", self._atajo(lambda: self._ajustar_t0(+1)))
        self.bind("<Return>", self._atajo(self._aprobar_y_siguiente))
        self.bind("<space>", self._atajo(self._reproducir_audio))
        self.bind("v", self._atajo(self._abrir_video_externo))
        self.bind("V", self._atajo(self._abrir_video_externo))

    def _atajo(self, accion):
        """Envuelve un atajo global: solo actúa en fase de revisión y NUNCA
        mientras se escribe en un Entry. Sin esta guarda, Enter en el campo
        Inicio aplicaba el tiempo Y aprobaba el clip saltando al siguiente;
        teclear 'v' en Nº cliente abría el reproductor; espacio lanzaba audio."""
        def handler(_e):
            if self.fase != "revision":
                return
            if isinstance(self.focus_get(), tk.Entry):
                return
            accion()
        return handler

    # ─────────────────────────────────────────────────────────────────────
    # Polling de cola para comunicación con workers
    # ─────────────────────────────────────────────────────────────────────
    def _poll_cola(self):
        try:
            while True:
                tipo, data = self.cola.get_nowait()
                if tipo == "log":
                    self._append_log(*data)
                elif tipo == "prog_label":
                    if hasattr(self, "lbl_prog"):
                        self.lbl_prog.config(text=data)
                elif tipo == "prog_barra":
                    if hasattr(self, "barra"):
                        self.barra["value"] = data
                elif tipo == "fin_analisis":
                    self._entrar_revision(data)
                elif tipo == "fin_render":
                    self._on_fin_render(data)
                elif tipo == "entrega_estado":
                    self._on_entrega_estado(*data)
                elif tipo == "entrega_link":
                    self._on_entrega_link(*data)
                elif tipo == "modelo_error":
                    pass  # se logueará al iniciar análisis
        except queue.Empty:
            pass
        self.after(80, self._poll_cola)

    def _append_log(self, texto: str, nivel: str = "info"):
        if not hasattr(self, "log"):
            return
        self.log.config(state="normal")
        self.log.insert("end", texto, nivel)
        self.log.see("end")
        self.log.config(state="disabled")

    # ─────────────────────────────────────────────────────────────────────
    # Limpieza
    # ─────────────────────────────────────────────────────────────────────
    def _limpiar_ventana(self):
        for w in self.winfo_children():
            w.destroy()

    def _on_cerrar(self):
        # Soltar el WAV en reproducción para que el rmtree del tmp no falle.
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except RuntimeError:
            pass
        try:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        except OSError:
            pass
        self.destroy()


def _mmss_a_segundos(texto: str) -> float:
    """Parsea 'm:ss' o 'ss' a segundos. Lanza ValueError si malformado."""
    texto = texto.strip()
    if ":" in texto:
        m, s = texto.split(":", 1)
        return int(m) * 60 + float(s)
    return float(texto)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
