# Editor de tirolina — Sunview Park Málaga

Asistente para recortar y entregar los vídeos de GoPro de la tirolina. La IA
(Whisper) y el análisis de audio hacen lo tedioso; una persona valida cada corte
en segundos y, opcionalmente, envía el vídeo al cliente.

## Qué hace

Trabaja en **3 fases** dentro de una misma ventana:

1. **Análisis automático** — por cada vídeo: extrae el audio, lo transcribe con
   IA, detecta el **inicio** (el "muro de viento" del lanzamiento) y la
   **llegada** (frases tipo "¿qué tal?", "madre mía", "thank you"…), detecta el
   **nº de cliente** que grita el monitor ("número 7"), y prepara un storyboard
   de frames + la onda de audio. No recorta nada todavía.
2. **Revisión rápida** — una tarjeta por clip con los 3 frames del inicio, la
   onda con los marcadores de corte y campos editables. Apruebas o ajustas con
   el teclado en un par de segundos.
3. **Generación + entrega** — recorta solo los clips aprobados, añade el logo,
   exporta a 1080p comprimido (~30–50 MB) y abre un panel para enviar cada vídeo
   al cliente (WeTransfer + Gmail ya redactado).

---

## Instalación (solo la primera vez)

### Requisitos previos

**1. Python 3.10 o superior**
- Descargar: https://www.python.org/downloads/
- Durante la instalación, **marcar "Add Python to PATH"**

**2. FFmpeg**
- Descargar: https://www.gyan.dev/ffmpeg/builds/ → "release essentials build" (.zip)
- Descomprimir en `C:\ffmpeg`
- Añadir `C:\ffmpeg\bin` al PATH del sistema:
  - Buscar "Editar variables de entorno del sistema" en el menú inicio
  - Variables de entorno → Path → Editar → Nuevo → `C:\ffmpeg\bin`
  - Cerrar y reabrir el terminal

### Instalar dependencias

Doble clic en **`instalar.bat`**

Crea el entorno virtual local (`.venv`), instala las librerías y descarga el
modelo de IA (~500 MB la primera vez). Si te falta Python o FFmpeg, el
instalador te avisa con instrucciones.

---

## Uso diario

1. Doble clic en **`editar.bat`** (abre la ventana del asistente).
2. **Arrastra los vídeos** a la zona indicada, o usa "Seleccionar vídeos
   manualmente".
3. Pulsa **"Analizar N vídeo(s)"** y espera a que termine el análisis.
4. **Revisa cada clip** (ver atajos abajo): aprueba los que estén bien, ajusta
   los que no.
5. Pulsa **"Generar N vídeo(s) aprobados"**.
6. Los resultados quedan en la carpeta **`salida/`** con el sufijo `_FINAL.mp4`,
   y se abre el panel de entrega por correo.

### Atajos de la pantalla de revisión

| Tecla / acción | Qué hace |
|---|---|
| `↑` / `↓` | Cambiar de clip |
| `←` / `→` | Mover el inicio ±1 segundo |
| Botones `−5s` / `+5s` | Mover inicio o fin ±5 segundos |
| **Click** en la onda | Poner el **inicio** en ese punto |
| **Shift + click** en la onda | Poner el **fin** en ese punto |
| Campos `Inicio` / `Fin` | Escribir el tiempo a mano (formato `m:ss`) |
| `Espacio` | Reproducir 6 s de audio alrededor del inicio |
| `V` | Abrir un clip de 6 s en el reproductor de vídeo |
| `Enter` | Aprobar el clip y pasar al siguiente |

Cada clip muestra también la **pista** que usó la IA para sugerir el corte, el
**nº de cliente** detectado (editable) y el **idioma** del correo (ES/EN).

---

## Entrega por correo (opcional)

En la fase de entrega, por cada vídeo hay dos botones:

- **📤 Subir vídeo** — abre WeTransfer y el explorador con el vídeo ya
  seleccionado, listo para arrastrarlo. Subes y copias el enlace.
- **✉️ Abrir correo** — abre Gmail con el correo ya redactado. Si está conectado
  el Google Sheets, rellena el **email del cliente** a partir del **nº** que
  hayas puesto; y si copiaste el enlace de WeTransfer, lo pega en el cuerpo.

Funciona sin configurar nada (abre Gmail con el destinatario vacío para que lo
escribas), pero para que rellene el email solo hay que conectar el Google Sheets
una vez. **Ver `CONFIGURAR_CORREO.md`** para los pasos.

Para comprobar la conexión al Sheets sin abrir la app:

```
.venv\Scripts\python.exe probar_conexion_sheets.py 1
```

---

## Estructura del proyecto

```
sunview-tirolina/
├── gui_tirolina.py        — interfaz gráfica (las 3 fases)
├── editar_tirolina.py     — motor: IA, detección de corte, FFmpeg, verificación
├── clientes.py            — busca el email del cliente por nº en el Google Sheets
├── envio_correo.py        — plantillas de correo + abrir WeTransfer/Gmail
├── probar_conexion_sheets.py — test rápido de la conexión al Sheets
├── instalar.bat           — instalador (una sola vez)
├── editar.bat             — lanzador diario (abre la GUI)
├── requirements.txt       — dependencias Python
├── CONFIGURAR_CORREO.md   — cómo conectar el Google Sheets (una vez)
├── config_sheets.example.json — plantilla de configuración del correo
├── assets/
│   └── logo.png           — logo de Sunview Park
└── salida/                — vídeos editados (se crea automáticamente)
```

> Las credenciales (`config_sheets.json`, la clave `*.json` de la cuenta de
> servicio) están en `.gitignore` y **nunca** se suben al repositorio.

---

## Cómo funciona internamente

Toda la lógica de detección vive en `editar_tirolina.py` y la comparten la GUI y
el modo consola. La idea clave: **la IA sugiere, el humano decide.**

### Detección del inicio — "muro de viento" (`_detectar_onset_lanzamiento`)
La señal física más fiable del salto es el golpe de viento sostenido. Se calcula
el nivel de audio (RMS) del principio como "ruido de plataforma" y se busca el
primer instante en que el sonido sube por encima de ese suelo y se mantiene allí
varios segundos. Es robusto a saturación del micro y a lanzamientos tempranos.
Si no se detecta, no se inventa nada: el inicio queda sin sugerir y el revisor lo
marca con un click en la onda.

### Detección de la llegada (`buscar_fin` + audio)
Se busca la primera frase de `PALABRAS_FIN` en el último 30 % del vídeo y se
reconcilia con la caída del bloque de viento (`_bloques_de_viento`). Esto corrige
falsos positivos habituales: una frase antes del vuelo, una exclamación a mitad
de vuelo, o charla post-llegada demasiado tardía.

### Sugerencia de corte (`sugerir_corte`)
Combina lo anterior y devuelve `t_inicio`/`t_fin` **como sugerencia**, con un log
explicando de dónde sale cada tiempo. Si el clip contiene **2+ vuelos**
(`detectar_multiples_vuelos`), avisa para que se parta a mano.

### Nº de cliente (`detectar_numero_cliente`)
Busca el patrón "número X" en la transcripción cerca del lanzamiento y lo
convierte a cifra (admite "tres", "trece", "treinta y dos"…). El revisor lo
confirma.

### Validación previa (`validar_video`)
Antes de procesar, descarta errores comunes que de otro modo crashearían a mitad:
archivo de OneDrive sin descargar, sin pista de audio, corrupto o ilegible.

### Verificación del resultado (`verificar_corte`)
Tras renderizar, cada vídeo pasa varios chequeos independientes: duración
(20–180 s), resolución (1920×1080), nivel de audio (ni mudo ni saturado), inicio
no silencioso y llegada con voz.

### Rendimiento e IA
- Transcripción con **faster-whisper**. Auto-detecta **GPU NVIDIA** (CUDA) y, si
  no está disponible, usa CPU. El render usa **NVENC** si existe, con fallback a
  libx264.
- Si el modelo elegido no cabe en memoria, degrada solo: `small → base → tiny`.

### Interfaz gráfica
- **tkinter** (incluido en Python). Drag & drop con **tkinterdnd2** (opcional).
- El análisis y el render corren en hilos separados; la UI se refresca leyendo
  una `queue.Queue` periódicamente, así nunca se bloquea.

---

## Ajustes de configuración (en `editar_tirolina.py`)

| Variable | Por defecto | Qué controla |
|---|---|---|
| `MODELO_WHISPER` | `"small"` | `tiny`/`base`/`small`/`medium` — más grande = más preciso pero más lento |
| `SEGUNDOS_ANTES_INICIO` | `1.5` | Margen que se añade antes del inicio detectado |
| `SEGUNDOS_DESPUES_FIN` | `2.0` | Margen que se añade tras la llegada |
| `PALABRAS_FIN` | lista | Frases de llegada (incluye EN/FR/IT/DE para clientes extranjeros) |
| `RESOLUCION` | `"1920:1080"` | Resolución de salida |
| `CRF` | `23` | Calidad: 18 = alta / 23 = balance / 28 = más compresión |
| `PRESET` | `"medium"` | Preset de libx264 (velocidad vs. tamaño) |
| `LOGO_ESCALA` | `0.65` | Tamaño del logo |
| `LOGO_MARGEN` | `60` | Distancia del logo a la esquina (píxeles) |

Para afinar la sensibilidad del "muro de viento" hay constantes `MURO_VIENTO_*`
con notas en el propio archivo (útiles si las GoPro activan el filtro de viento).

---

## Rendimiento estimado

Por vídeo de ~2 minutos (modelo `small`):
- Análisis (audio + transcripción + frames): ~30 s – 2 min según el PC (más
  rápido con GPU)
- Render FFmpeg: 20 – 60 s
- La revisión humana añade solo unos segundos por clip

---

## Problemas comunes

**La detección de la llegada falla en muchos vídeos**
→ Revisa el archivo `_transcripcion.txt` en `salida/` para ver qué oyó la IA. Si
usa frases distintas, añádelas a `PALABRAS_FIN`. El inicio siempre se puede
marcar a click en la onda.

**Tarda demasiado**
→ Cambia `MODELO_WHISPER` a `"base"` o `"tiny"`.

**El vídeo final pesa demasiado**
→ Sube `CRF` a 26–28 o cambia `RESOLUCION` a `"1280:720"`.

**Error "FFmpeg no encontrado"**
→ Comprueba que `C:\ffmpeg\bin` está en el PATH y reinicia el ordenador.

**Un vídeo aparece como "archivo en la nube sin descargar"**
→ En OneDrive, click derecho sobre el vídeo → "Mantener siempre en este
dispositivo", y vuelve a analizarlo.

**El botón "Abrir correo" muestra el formulario de crear Gmail**
→ La cuenta activa no tiene buzón de Gmail. Ver el Paso 4 de `CONFIGURAR_CORREO.md`.

---

## Modo consola (alternativo)

`editar_tirolina.py` también se puede ejecutar directo: procesa todos los vídeos
de la carpeta `entrada/` en modo automático *best-effort* (sin revisión humana) y
deja los resultados en `salida/`. El flujo recomendado es la GUI (`editar.bat`).
