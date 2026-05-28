# Editor automático de tirolina — Sunview Park Málaga

Procesa y recorta automáticamente los vídeos de GoPro de la tirolina usando IA de transcripción de voz (Whisper).

## Qué hace

1. Detecta el **inicio** escuchando la cuenta atrás del monitor ("3, 2, 1", "vamos", etc.)
2. Detecta el **final** escuchando la llegada ("¿qué tal?", "¡hola!", "¿cómo estuvo?", etc.)
3. Recorta el vídeo entre esos dos puntos
4. Añade el logo de Sunview Park en la esquina superior derecha
5. Exporta a 1080p comprimido (~30–50 MB, listo para enviar)
6. Si la detección falla en algún vídeo, muestra un panel para ajustar el corte manualmente

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

Descarga e instala Whisper y las librerías necesarias en un entorno virtual local (`.venv`). La primera vez tarda varios minutos porque descarga el modelo de IA (~500 MB).

---

## Uso diario

1. Doble clic en **`editar.bat`**
2. En la ventana que se abre, **arrastra los vídeos** o usa el botón "Seleccionar vídeos"
3. Pulsa **"Procesar N vídeos"**
4. Espera — el log muestra el progreso en tiempo real
5. Los resultados aparecen en la carpeta **`salida/`** con el sufijo `_FINAL.mp4`

Si algún vídeo no se detectó correctamente, aparece automáticamente un **panel de ajuste manual** con campos de Inicio y Fin para corregirlo sin salir de la app.

---

## Estructura del proyecto

```
sunview-tirolina/
├── editar_tirolina.py     — motor de procesamiento (Whisper + FFmpeg)
├── gui_tirolina.py        — interfaz gráfica (tkinter)
├── instalar.bat           — instalador (una sola vez)
├── editar.bat             — lanzador diario
├── requirements.txt       — dependencias Python
├── assets/
│   └── logo.png           — logo de Sunview Park
└── salida/                — vídeos editados (se crea automáticamente)
```

---

## Cómo funciona internamente

### Detección de inicio (`buscar_inicio`)
Whisper transcribe el audio completo con `temperature=0` (salida determinista). Luego se busca en el **primer 55% del vídeo** cualquier frase de `PALABRAS_INICIO`. Si hay varios candidatos, se toma el último (el más cercano al momento de saltar). Se añade un margen de `SEGUNDOS_ANTES_INICIO` antes del corte.

### Detección de llegada (`buscar_fin`)
Se busca en el **último 30% del vídeo** cualquier frase de `PALABRAS_FIN`, recorriendo los segmentos de atrás hacia delante. Se añade un margen de `SEGUNDOS_DESPUES_FIN` después del corte.

### Panel de ajuste manual
Si `t_inicio` o `t_fin` son `None` (no detectados), el vídeo se procesa igualmente con valores por defecto (0s y duración total) y se muestra una fila en el panel con los campos editables. El botón "Cortar" vuelve a generar el `_FINAL.mp4` en segundo plano con los tiempos indicados.

### Interfaz gráfica
- Construida con **tkinter** (incluido en Python, sin instalación extra)
- Drag & drop con **tkinterdnd2** (se activa automáticamente si está instalado)
- El procesamiento corre en un hilo separado; la UI se comunica con el worker mediante una `queue.Queue` que se lee cada 100 ms

---

## Ajustes de configuración (en `editar_tirolina.py`)

| Variable | Valor por defecto | Qué controla |
|---|---|---|
| `MODELO_WHISPER` | `"small"` | `tiny`/`base`/`small`/`medium` — más grande = más preciso pero más lento |
| `SEGUNDOS_ANTES_INICIO` | `1.5` | Margen antes del "3, 2, 1" |
| `SEGUNDOS_DESPUES_FIN` | `2.0` | Margen después de la pregunta de llegada |
| `DURACION_VUELO_TIPICA_S` | `80` | Duración típica del vuelo — el inicio se ancla al fin restando este valor |
| `PALABRAS_INICIO` | lista | Frases que indican salida |
| `PALABRAS_FIN` | lista | Frases que indican llegada |
| `RESOLUCION` | `"1920:1080"` | Resolución de salida |
| `CRF` | `23` | Calidad: 18 = alta / 23 = balance / 28 = más compresión |

---

## Rendimiento estimado

Por vídeo de ~2 minutos (modelo `small`):
- Transcripción Whisper: 30 s – 2 min según el PC
- Edición FFmpeg: 20 – 60 s
- **Total: ~1–3 minutos por vídeo**

---

## Problemas comunes

**La detección falla en muchos vídeos**
→ Revisa el archivo `_transcripcion.txt` en `salida/` para ver qué oyó Whisper. Si usa frases distintas, añádelas a `PALABRAS_INICIO` o `PALABRAS_FIN`.

**Tarda demasiado**
→ Cambia `MODELO_WHISPER` a `"base"` o `"tiny"`.

**El vídeo final pesa demasiado**
→ Sube `CRF` a 26–28 o cambia `RESOLUCION` a `"1280:720"`.

**Error "FFmpeg no encontrado"**
→ Comprueba que `C:\ffmpeg\bin` está en el PATH y reinicia el ordenador.
