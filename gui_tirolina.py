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
import sys
import tempfile
import shutil
import winsound
from pathlib import Path

# pythonw.exe no tiene consola — librerías como tqdm/whisper fallan al
# escribir en sys.stdout/stderr si son None. Mantenemos handles a devnull
# y los cerramos al salir para no fugar file descriptors.
import atexit as _atexit
_DEVNULL_HANDLES = []
if sys.stdout is None:
    _h = open(os.devnull, "w")
    _DEVNULL_HANDLES.append(_h)
    sys.stdout = _h
if sys.stderr is None:
    _h = open(os.devnull, "w")
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
    sugerir_corte, cargar_modelo_whisper,
    extraer_3_frames, extraer_audio_snippet, extraer_clip_preview,
    envolvente_rms,
    segundos_a_mmss, CARPETA_SALIDA, EXTENSIONES,
    SEGUNDOS_ANTES_INICIO, SEGUNDOS_DESPUES_FIN,
)

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
            self._modelo, _ = cargar_modelo_whisper()
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
                  if Path(p).is_file() and Path(p).suffix in EXTENSIONES]
        if videos:
            self._set_videos(videos)

    def _set_videos(self, videos: list[Path]):
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
                # WAV temporal de este clip — vive hasta cerrar la app
                audio_tmp = self.tmp_dir / f"{video.stem}.wav"
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

                # Storyboard + onda
                self.cola.put(("log", ("  Generando storyboard...\n", "info")))
                frame_paths = extraer_3_frames(
                    video, t0_sug, self.tmp_dir, ancho=ANCHO_FRAME,
                )
                rms, _ = envolvente_rms(audio_tmp, n_puntos=800)

                clips.append({
                    "video_path": video,
                    "audio_tmp": audio_tmp,
                    "duracion": duracion,
                    "t_inicio_raw": sug["t_inicio_raw"],
                    "t_fin_raw": sug["t_fin_raw"],
                    "texto_inicio": sug["texto_inicio"] or "(no detectado)",
                    "texto_fin": sug["texto_fin"] or "(no detectado)",
                    "t0": t0_sug,
                    "t1": t1_sug,
                    "rms": rms,
                    "frame_paths": frame_paths,
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
            "t0": 0.0, "t1": 0.0, "rms": None, "frame_paths": {},
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

        # ── Storyboard de 3 frames ──
        storyboard = tk.Frame(cont, bg=COLORES["panel"])
        storyboard.pack(pady=(0, 10))
        etiquetas = [
            ("antes", f"-{int(2)}s antes"),
            ("inicio", "INICIO"),
            ("despues", f"+{int(2)}s después"),
        ]
        for clave, texto in etiquetas:
            celda = tk.Frame(storyboard, bg=COLORES["panel"], padx=6)
            celda.pack(side="left")
            tk.Label(celda, text=texto,
                     font=(FUENTE, 9, "bold"),
                     bg=COLORES["panel"],
                     fg=COLORES["acento"] if clave == "inicio" else COLORES["texto_suave"],
                     ).pack()
            path = c["frame_paths"].get(clave) if c.get("frame_paths") else None
            if path and Path(path).exists():
                img = Image.open(path)
                img.thumbnail((ANCHO_FRAME, ALTO_FRAME), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                self._photos[clave] = photo
                tk.Label(celda, image=photo,
                         bg=COLORES["panel"]).pack()
            else:
                tk.Label(celda, text="(sin frame)",
                         width=int(ANCHO_FRAME / 8), height=int(ALTO_FRAME / 16),
                         bg=COLORES["borde"], fg=COLORES["texto_suave"],
                         font=(FUENTE, 9)).pack()

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
        tk.Button(fila_ini, text="−1s", font=(FUENTE, 9),
                  bg=COLORES["fondo"], relief="flat", cursor="hand2",
                  command=lambda: self._ajustar_t0(-1)).pack(side="left", padx=2)
        tk.Button(fila_ini, text="+1s", font=(FUENTE, 9),
                  bg=COLORES["fondo"], relief="flat", cursor="hand2",
                  command=lambda: self._ajustar_t0(+1)).pack(side="left", padx=2)
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
        tk.Button(fila_fin, text="−1s", font=(FUENTE, 9),
                  bg=COLORES["fondo"], relief="flat", cursor="hand2",
                  command=lambda: self._ajustar_t1(-1)).pack(side="left", padx=2)
        tk.Button(fila_fin, text="+1s", font=(FUENTE, 9),
                  bg=COLORES["fondo"], relief="flat", cursor="hand2",
                  command=lambda: self._ajustar_t1(+1)).pack(side="left", padx=2)
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
        c["t0"] = max(0.0, min(c["duracion"], t0))
        c["t1"] = max(c["t0"] + 0.5, min(c["duracion"], t1))
        self._actualizar_marcadores_waveform(c)
        self._actualizar_duracion_label(c)

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
        snippet = self.tmp_dir / f"_preview_{c['video_path'].stem}.wav"
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
        clip_out = self.tmp_dir / f"_preview_{c['video_path'].stem}.mp4"
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

    def _worker_render(self, aprobados):
        for i, c in enumerate(aprobados, 1):
            self.cola.put(("prog_label",
                           f"[{i}/{len(aprobados)}] {c['video_path'].name}"))
            self.cola.put(("log",
                           (f"\n── {c['video_path'].name} ──\n", "header")))
            salida = CARPETA_SALIDA / f"{c['video_path'].stem}_FINAL.mp4"
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
        # Botón final para volver al inicio + abrir carpeta
        pie = tk.Frame(self, bg=COLORES["fondo"])
        pie.pack(fill="x", pady=10)
        tk.Button(pie, text="📂 Abrir carpeta salida/",
                  font=(FUENTE, 10), bg=COLORES["acento"], fg="white",
                  relief="flat", cursor="hand2", padx=14, pady=6,
                  command=self._abrir_carpeta).pack(side="left", padx=16)
        tk.Button(pie, text="↻ Procesar más vídeos",
                  font=(FUENTE, 10), bg=COLORES["fondo"], fg=COLORES["acento"],
                  relief="flat", cursor="hand2", padx=14, pady=6,
                  command=self._reset).pack(side="right", padx=16)

    def _abrir_carpeta(self):
        try:
            os.startfile(str(CARPETA_SALIDA))
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
        self.bind("<Up>", lambda e: self._navegar(-1) if self.fase == "revision" else None)
        self.bind("<Down>", lambda e: self._navegar(+1) if self.fase == "revision" else None)
        self.bind("<Left>", self._on_izq)
        self.bind("<Right>", self._on_der)
        self.bind("<Return>", lambda e: self._aprobar_y_siguiente() if self.fase == "revision" else None)
        self.bind("<space>", lambda e: self._reproducir_audio() if self.fase == "revision" else None)
        self.bind("v", lambda e: self._abrir_video_externo() if self.fase == "revision" else None)
        self.bind("V", lambda e: self._abrir_video_externo() if self.fase == "revision" else None)

    def _on_izq(self, e):
        if self.fase != "revision":
            return
        # Si el foco está en el Entry, dejar al Entry mover el cursor
        w = self.focus_get()
        if isinstance(w, tk.Entry):
            return
        self._ajustar_t0(-1)

    def _on_der(self, e):
        if self.fase != "revision":
            return
        w = self.focus_get()
        if isinstance(w, tk.Entry):
            return
        self._ajustar_t0(+1)

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
