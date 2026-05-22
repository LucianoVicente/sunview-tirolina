"""
Sunview Park — Interfaz gráfica para edición de vídeos de tirolina
"""

import tkinter as tk
from tkinter import ttk, filedialog
import threading
import queue
import os
import sys
from pathlib import Path

# pythonw.exe no tiene consola — algunas librerías (tqdm, whisper) fallan
# al escribir en sys.stdout/stderr si son None
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DRAG_DROP = True
except ImportError:
    DRAG_DROP = False

from editar_tirolina import (
    extraer_audio, obtener_duracion, transcribir_audio,
    buscar_inicio, buscar_fin, detectar_vuelo_por_audio,
    editar_video, verificar_corte,
    verificar_duracion, verificar_resolucion, verificar_audio_nivel,
    verificar_inicio_limpio, verificar_llegada_detectada,
    segundos_a_mmss, CARPETA_SALIDA, EXTENSIONES, MODELO_WHISPER,
    SEGUNDOS_ANTES_INICIO, SEGUNDOS_DESPUES_FIN,
    BUSCAR_INICIO_HASTA_PORCENTAJE,
)

COLORES = {
    "fondo":        "#F5F7FA",
    "panel":        "#FFFFFF",
    "acento":       "#1A73E8",
    "ok":           "#34A853",
    "aviso":        "#C07700",
    "error":        "#EA4335",
    "texto":        "#202124",
    "texto_suave":  "#5F6368",
    "borde":        "#DADCE0",
}
FUENTE = "Segoe UI"

BaseVentana = TkinterDnD.Tk if DRAG_DROP else tk.Tk


class App(BaseVentana):
    def __init__(self):
        super().__init__()
        self.title("Sunview Park — Editor de Tirolina")
        self.geometry("700x600")
        self.minsize(580, 480)
        self.configure(bg=COLORES["fondo"])

        self.videos: list[Path] = []
        self.cola: queue.Queue = queue.Queue()
        self.procesando = False

        self._construir_ui()
        self._poll_cola()

    # ── UI ──────────────────────────────────────────────────────────────────

    def _construir_ui(self):
        # Cabecera
        cab = tk.Frame(self, bg=COLORES["acento"], pady=14)
        cab.pack(fill="x")
        tk.Label(cab, text="Sunview Park", font=(FUENTE, 17, "bold"),
                 bg=COLORES["acento"], fg="white").pack()
        tk.Label(cab, text="Editor automático de vídeos de tirolina",
                 font=(FUENTE, 10), bg=COLORES["acento"], fg="#BDD7F5").pack()

        self.cuerpo = tk.Frame(self, bg=COLORES["fondo"], padx=20, pady=16)
        self.cuerpo.pack(fill="both", expand=True)

        # Zona de arrastre
        self.zona = tk.Frame(self.cuerpo, bg=COLORES["panel"],
                             highlightbackground=COLORES["borde"],
                             highlightthickness=2)
        self.zona.pack(fill="x", pady=(0, 10))

        msg = "Arrastra los vídeos aquí" if DRAG_DROP else "Selecciona los vídeos"
        self.lbl_zona = tk.Label(self.zona, text=msg,
                                  font=(FUENTE, 12), bg=COLORES["panel"],
                                  fg=COLORES["texto_suave"], pady=18)
        self.lbl_zona.pack()

        tk.Button(self.zona, text="Seleccionar vídeos...",
                  font=(FUENTE, 10), bg=COLORES["acento"], fg="white",
                  relief="flat", padx=16, pady=6, cursor="hand2",
                  command=self._seleccionar).pack(pady=(0, 14))

        if DRAG_DROP:
            for widget in (self.zona, self.lbl_zona):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)

        # Lista de vídeos seleccionados
        self.frame_lista = tk.Frame(self.cuerpo, bg=COLORES["fondo"])
        self.frame_lista.pack(fill="x", pady=(0, 10))

        # Botón principal
        self.btn = tk.Button(self.cuerpo, text="Selecciona vídeos para empezar",
                             font=(FUENTE, 12, "bold"),
                             bg=COLORES["borde"], fg=COLORES["texto_suave"],
                             relief="flat", padx=20, pady=10,
                             state="disabled", command=self._iniciar)
        self.btn.pack(fill="x", pady=(0, 10))

        # Barra de progreso (oculta hasta que empiece)
        self.frame_prog = tk.Frame(self.cuerpo, bg=COLORES["fondo"])
        self.lbl_prog = tk.Label(self.frame_prog, text="",
                                  font=(FUENTE, 9), bg=COLORES["fondo"],
                                  fg=COLORES["texto_suave"])
        self.lbl_prog.pack(anchor="w")
        self.barra = ttk.Progressbar(self.frame_prog, mode="determinate")
        self.barra.pack(fill="x", pady=(2, 0))

        # Log de resultados (oculto hasta que empiece)
        self.frame_log = tk.Frame(self.cuerpo, bg=COLORES["panel"],
                                   highlightbackground=COLORES["borde"],
                                   highlightthickness=1)
        self.log = tk.Text(self.frame_log, height=12, font=("Consolas", 9),
                            bg=COLORES["panel"], fg=COLORES["texto"],
                            relief="flat", state="disabled",
                            wrap="word", padx=8, pady=8)
        scroll = ttk.Scrollbar(self.frame_log, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # Botón abrir carpeta (oculto hasta terminar)
        self.btn_carpeta = tk.Button(self.cuerpo, text="Abrir carpeta de resultados",
                                      font=(FUENTE, 10, "bold"),
                                      bg=COLORES["ok"], fg="white",
                                      relief="flat", padx=16, pady=8,
                                      cursor="hand2", command=self._abrir_carpeta)

    # ── Selección de vídeos ─────────────────────────────────────────────────

    def _seleccionar(self):
        archivos = filedialog.askopenfilenames(
            title="Seleccionar vídeos",
            filetypes=[("Vídeos", "*.mp4 *.MP4 *.mov *.MOV *.avi *.AVI *.mkv *.MKV"),
                       ("Todos los archivos", "*.*")]
        )
        if archivos:
            self._set_videos([Path(a) for a in archivos])

    def _on_drop(self, event):
        rutas = self.tk.splitlist(event.data)
        validos = [Path(r) for r in rutas
                   if Path(r).suffix in EXTENSIONES]
        if validos:
            self._set_videos(validos)

    def _set_videos(self, videos: list[Path]):
        self.videos = videos
        for w in self.frame_lista.winfo_children():
            w.destroy()
        for v in videos:
            tk.Label(self.frame_lista, text=f"  • {v.name}",
                     font=(FUENTE, 9), bg=COLORES["fondo"],
                     fg=COLORES["texto"]).pack(anchor="w")
        n = len(videos)
        self.btn.config(
            state="normal",
            bg=COLORES["acento"], fg="white",
            text=f"Procesar {n} vídeo{'s' if n > 1 else ''}"
        )

    # ── Procesamiento ───────────────────────────────────────────────────────

    def _iniciar(self):
        if not self.videos or self.procesando:
            return
        self.procesando = True
        self.btn.config(state="disabled", bg=COLORES["borde"],
                        fg=COLORES["texto_suave"], text="Procesando...")
        CARPETA_SALIDA.mkdir(exist_ok=True)

        # Mostrar barra y log
        self.frame_prog.pack(fill="x", pady=(0, 8))
        self.barra["maximum"] = len(self.videos)
        self.barra["value"] = 0
        self.frame_log.pack(fill="both", expand=True, pady=(0, 10))
        self.btn_carpeta.pack_forget()

        threading.Thread(target=self._worker,
                         args=(list(self.videos),),
                         daemon=True).start()

    def _worker(self, videos: list[Path]):
        import whisper as _whisper
        self._log("Cargando modelo de IA...\n", "info")
        try:
            model = _whisper.load_model(MODELO_WHISPER)
        except Exception as exc:
            self._log(f"Error cargando modelo: {exc}\n", "error")
            self.cola.put(("fin", []))
            return
        self._log("Modelo listo.\n\n", "info")

        resultados = []
        audio_tmp = Path("_audio_tmp.wav")

        for i, video in enumerate(videos, 1):
            self.cola.put(("prog_label", f"[{i}/{len(videos)}] {video.name}"))
            self._log(f"{'─'*45}\n{video.name}\n", "header")
            duracion = 0
            t0, t1 = 0, 0
            t0_raw, t1_raw = None, None
            salida = CARPETA_SALIDA / f"{video.stem}_FINAL.mp4"
            try:
                self._log("Extrayendo audio...\n", "info")
                extraer_audio(video, audio_tmp)
                duracion = obtener_duracion(video)

                self._log("Transcribiendo con IA...\n", "info")
                tx = transcribir_audio(audio_tmp, model)

                txt_path = CARPETA_SALIDA / f"{video.stem}_transcripcion.txt"
                with open(txt_path, "w", encoding="utf-8") as f:
                    for seg in tx["segments"]:
                        f.write(f"[{seg['start']:6.2f}s - {seg['end']:6.2f}s] {seg['text']}\n")

                t0_raw, txt0 = buscar_inicio(tx, duracion)
                t1_raw, txt1 = buscar_fin(tx, duracion)

                # Fallback: análisis de energía de audio cuando la transcripción no detecta
                if t0_raw is None or t1_raw is None:
                    self._log("Analizando energía de audio...\n", "info")
                    t_audio_ini, t_audio_fin = detectar_vuelo_por_audio(audio_tmp, duracion)
                    if t0_raw is None and t_audio_ini is not None:
                        t0_raw, txt0 = t_audio_ini, "[audio]"
                    if t1_raw is None and t_audio_fin is not None:
                        t1_raw, txt1 = t_audio_fin, "[audio]"

                if t0_raw is not None:
                    t0 = max(0, t0_raw - SEGUNDOS_ANTES_INICIO)
                    if txt0 == "[audio]":
                        self._log(f"✓ Salida (audio):  {segundos_a_mmss(t0_raw)}\n", "ok")
                    else:
                        self._log(f"✓ Salida:  '{txt0.strip()}' → {segundos_a_mmss(t0_raw)}\n", "ok")
                else:
                    t0 = 0
                    self._log("⚠ Inicio no detectado — lo que oyó Whisper al principio:\n", "aviso")
                    limite = duracion * BUSCAR_INICIO_HASTA_PORCENTAJE
                    segs = [s for s in tx["segments"] if s["start"] <= limite and s["text"].strip()]
                    for s in segs:
                        self._log(f"    [{segundos_a_mmss(s['start'])}] {s['text'].strip()}\n", "info")

                if t1_raw is not None:
                    t1 = min(duracion, t1_raw + SEGUNDOS_DESPUES_FIN)
                    if txt1 == "[audio]":
                        self._log(f"✓ Llegada (audio): {segundos_a_mmss(t1_raw)}\n", "ok")
                    else:
                        self._log(f"✓ Llegada: '{txt1.strip()}' → {segundos_a_mmss(t1_raw)}\n", "ok")
                else:
                    t1 = duracion
                    self._log("⚠ Llegada no detectada — lo que oyó Whisper al final:\n", "aviso")
                    limite_fin = duracion * 0.70
                    segs = [s for s in tx["segments"] if s["start"] >= limite_fin and s["text"].strip()]
                    if segs:
                        for s in segs:
                            self._log(f"    [{segundos_a_mmss(s['start'])}] {s['text'].strip()}\n", "info")
                    else:
                        self._log("    (sin audio detectado en el último 30%)\n", "info")

                self._log(f"Corte: {segundos_a_mmss(t0)} → {segundos_a_mmss(t1)} "
                          f"({t1 - t0:.0f}s)\n", "info")

                self._log("Generando vídeo final...\n", "info")
                editar_video(video, t0, t1, salida)

                self._log("Verificando...\n", "info")
                checks = verificar_corte(salida)
                for ch_nombre, ch_ok, ch_det in checks:
                    self._log(
                        f"  {'✓' if ch_ok else '⚠'} {ch_nombre}: {ch_det}\n",
                        "ok" if ch_ok else "aviso",
                    )
                ok     = all(ch_ok for _, ch_ok, _ in checks)
                detalle = (
                    " | ".join(f"{n}:{d}" for n, ch_ok, d in checks if not ch_ok)
                    or "OK"
                )
                self._log(("\n" if ok else "⚠ Revisa los puntos marcados arriba.\n\n"), "aviso" if not ok else "info")
                resultados.append({
                    "nombre": video.name,
                    "ok": ok,
                    "detalle": detalle,
                    "necesita_ajuste": not ok,
                    "video_path": video,
                    "salida_path": salida,
                    "duracion": duracion,
                    "t0": t0,
                    "t1": t1,
                })

            except Exception as exc:
                import traceback
                tb = traceback.format_exc()
                self._log(f"✗ Error: {exc}\n{tb}\n", "error")
                resultados.append({
                    "nombre": video.name,
                    "ok": False,
                    "detalle": str(exc),
                    "necesita_ajuste": False,
                    "video_path": video,
                    "salida_path": salida,
                    "duracion": duracion,
                    "t0": t0,
                    "t1": t1,
                })
            finally:
                audio_tmp.unlink(missing_ok=True)
                self.cola.put(("prog_barra", i))

        self.cola.put(("fin", resultados))

    # ── Cola UI ─────────────────────────────────────────────────────────────

    def _log(self, texto: str, nivel: str = "info"):
        self.cola.put(("log", texto, nivel))

    def _poll_cola(self):
        colores_log = {
            "ok":     COLORES["ok"],
            "aviso":  COLORES["aviso"],
            "error":  COLORES["error"],
            "header": COLORES["acento"],
            "info":   COLORES["texto"],
        }
        try:
            while True:
                msg = self.cola.get_nowait()
                if msg[0] == "log":
                    _, texto, nivel = msg
                    self.log.config(state="normal")
                    tag = f"c_{nivel}"
                    self.log.tag_config(tag, foreground=colores_log.get(nivel, COLORES["texto"]))
                    self.log.insert("end", texto, tag)
                    self.log.see("end")
                    self.log.config(state="disabled")
                elif msg[0] == "prog_label":
                    self.lbl_prog.config(text=msg[1])
                elif msg[0] == "prog_barra":
                    self.barra["value"] = msg[1]
                elif msg[0] == "estado":
                    _, widget, texto, color = msg
                    widget.config(text=texto, fg=color)
                elif msg[0] == "fin":
                    self._on_fin(msg[1])
        except queue.Empty:
            pass
        self.after(100, self._poll_cola)

    def _on_fin(self, resultados: list):
        self.procesando = False
        if not resultados:
            self.btn.config(state="normal", bg=COLORES["acento"], fg="white",
                            text="Selecciona vídeos para empezar")
            return
        n_ok = sum(1 for r in resultados if r["ok"])
        n = len(resultados)
        self._log(f"{'='*45}\n", "header")
        self._log(f"RESULTADO: {n_ok}/{n} vídeos correctos\n", "header")
        for r in resultados:
            self._log(f"  {'✓' if r['ok'] else '⚠'} {r['nombre']}: {r['detalle']}\n",
                      "ok" if r["ok"] else "aviso")
        if any(not r["ok"] for r in resultados):
            self._log("\nLos vídeos marcados con ⚠ pueden necesitar revisión.\n", "aviso")

        self.lbl_prog.config(text=f"Completado — {n_ok}/{n} vídeos correctos")
        self.btn_carpeta.pack(fill="x", pady=(4, 0))

        ajustes = [r for r in resultados if r["necesita_ajuste"]]
        if ajustes:
            self._mostrar_panel_ajuste(ajustes)

    # ── Panel de ajuste manual ───────────────────────────────────────────────

    def _mostrar_panel_ajuste(self, ajustes: list):
        frame = tk.LabelFrame(
            self.cuerpo, text="Ajuste manual de corte",
            font=(FUENTE, 10, "bold"),
            bg=COLORES["fondo"], fg=COLORES["aviso"],
            bd=1, relief="groove", padx=10, pady=8,
        )
        frame.pack(fill="x", pady=(10, 0))

        tk.Label(frame,
                 text="La detección automática falló en estos vídeos. "
                      "Edita los tiempos y pulsa Cortar.",
                 font=(FUENTE, 9), bg=COLORES["fondo"],
                 fg=COLORES["texto_suave"], wraplength=600, justify="left",
                 ).pack(anchor="w", pady=(0, 6))

        for r in ajustes:
            self._fila_ajuste(frame, r)

    def _fila_ajuste(self, frame, r: dict):
        fila = tk.Frame(frame, bg=COLORES["fondo"])
        fila.pack(fill="x", pady=4)

        nombre = r["nombre"]
        if len(nombre) > 22:
            nombre = nombre[:19] + "..."
        tk.Label(fila, text=nombre, font=(FUENTE, 9, "bold"),
                 bg=COLORES["fondo"], fg=COLORES["texto"],
                 width=24, anchor="w").pack(side="left")

        tk.Label(fila, text="Inicio:", font=(FUENTE, 9),
                 bg=COLORES["fondo"], fg=COLORES["texto_suave"]).pack(side="left")
        var_t0 = tk.StringVar(value=segundos_a_mmss(r["t0"]))
        tk.Entry(fila, textvariable=var_t0, font=(FUENTE, 9),
                 width=7).pack(side="left", padx=(2, 10))

        tk.Label(fila, text="Fin:", font=(FUENTE, 9),
                 bg=COLORES["fondo"], fg=COLORES["texto_suave"]).pack(side="left")
        var_t1 = tk.StringVar(value=segundos_a_mmss(r["t1"]))
        tk.Entry(fila, textvariable=var_t1, font=(FUENTE, 9),
                 width=7).pack(side="left", padx=(2, 10))

        lbl_estado = tk.Label(fila, text="", font=(FUENTE, 9),
                               bg=COLORES["fondo"], fg=COLORES["texto_suave"])
        lbl_estado.pack(side="left")

        tk.Button(
            fila, text="Cortar",
            font=(FUENTE, 9, "bold"),
            bg=COLORES["acento"], fg="white",
            relief="flat", padx=12, pady=3, cursor="hand2",
            command=lambda: self._recutar(r, var_t0, var_t1, lbl_estado),
        ).pack(side="right")

    def _recutar(self, r: dict, var_t0: tk.StringVar, var_t1: tk.StringVar,
                 lbl_estado: tk.Label):
        try:
            t0 = self._mmss_a_segundos(var_t0.get())
            t1 = self._mmss_a_segundos(var_t1.get())
        except ValueError:
            lbl_estado.config(text="Formato inválido (usa M:SS)", fg=COLORES["error"])
            return

        if t1 <= t0:
            lbl_estado.config(text="Fin debe ser mayor que Inicio", fg=COLORES["error"])
            return

        lbl_estado.config(text="Cortando...", fg=COLORES["texto_suave"])
        self.update_idletasks()

        def _do():
            try:
                editar_video(r["video_path"], t0, t1, r["salida_path"])
                self.cola.put(("estado", lbl_estado,
                               f"✓ OK ({t1 - t0:.0f}s)", COLORES["ok"]))
            except Exception as exc:
                self.cola.put(("estado", lbl_estado,
                               f"✗ {exc}", COLORES["error"]))

        threading.Thread(target=_do, daemon=True).start()

    def _mmss_a_segundos(self, texto: str) -> float:
        texto = texto.strip()
        if ":" in texto:
            partes = texto.split(":", 1)
            return int(partes[0]) * 60 + float(partes[1])
        return float(texto)

    def _abrir_carpeta(self):
        carpeta = CARPETA_SALIDA.absolute()
        carpeta.mkdir(exist_ok=True)
        os.startfile(str(carpeta))


if __name__ == "__main__":
    app = App()
    app.mainloop()
