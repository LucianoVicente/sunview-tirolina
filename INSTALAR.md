# Instalar la app en un PC nuevo (ej. el de recepción)

Tiempo estimado: 20–30 minutos. Hace falta conexión a internet.

## Qué llevar preparado en un USB

Estos 2 archivos **nunca** se descargan de GitHub (son privados):

1. `config_sheets.json` — la configuración (ver plantilla abajo).
2. `sunviewparkvideos-XXXX.json` — la clave del robot de Google.

> En el PC de recepción, el `config_sheets.json` **no** debe llevar la línea
> `"edge_perfil"` (eso es solo para PCs con varios perfiles de Edge).
> Plantilla recomendada (la clave del robot va en la misma carpeta que la app,
> así basta con poner su nombre):
>
> ```json
> {
>     "service_account_json": "sunviewparkvideos-XXXX.json",
>     "columnas": { "orden": 1, "videos": 2, "email": 3, "nombre": 3 }
> }
> ```
>
> No hace falta `spreadsheet_id`: la app encuentra sola la hoja del mes
> (basta que cada mes se comparta el Sheets nuevo con el robot).

## Pasos en el PC nuevo

### 1. Instalar Python

1. Descarga Python desde https://www.python.org/downloads/ (botón amarillo).
2. Al ejecutar el instalador, **marca la casilla "Add python.exe to PATH"**
   (abajo del todo) antes de pulsar *Install Now*.

### 2. Descargar la app

Opción A (sin programas extra): entra en el repositorio de GitHub → botón
verde **Code** → **Download ZIP** → descomprime donde quieras
(ej. `C:\sunview-tirolina`).

Opción B: copiar la carpeta entera por USB desde otro PC (sin la subcarpeta
`.venv`, que se regenera en cada equipo).

### 3. Descargar ffmpeg (el motor de vídeo)

1. Descarga **ffmpeg-release-essentials.zip** de
   https://www.gyan.dev/ffmpeg/builds/
2. Descomprímelo, **renombra la carpeta resultante a `ffmpeg`** y muévela
   **dentro de la carpeta de la app** (debe quedar `ffmpeg\bin\ffmpeg.exe`).
3. No hay que tocar el PATH de Windows: la app la encuentra sola.

### 4. Copiar los archivos privados del USB

Copia `config_sheets.json` y `sunviewparkvideos-XXXX.json` dentro de la
carpeta de la app (junto a `gui_tirolina.py`).

### 5. Ejecutar el instalador

Doble clic en **`instalar.bat`**. Él solo:

- comprueba Python y ffmpeg,
- crea el entorno e instala las librerías (varios minutos),
- descarga el modelo de voz (~500 MB, solo la primera vez),
- crea el icono **"Sunview Videos"** en el escritorio.

### 6. Probar

1. Doble clic en el icono **"Sunview Videos"** del escritorio.
2. Arrastra un vídeo de prueba y dale a **Analizar**.
3. Tras el render, prueba **"📤 Subir vídeo"** y **"✉️ Abrir correo"**:
   debe abrirse Gmail con el email del cliente ya puesto (el navegador debe
   tener iniciada la sesión de Gmail de la tirolina).

## Si algo falla

- **"Python no está instalado"** → repite el paso 1 marcando *Add to PATH*.
- **"FFmpeg no encontrado"** → revisa que exista `ffmpeg\bin\ffmpeg.exe`
  dentro de la carpeta de la app (paso 3).
- **No rellena el email del cliente** → faltan los archivos privados (paso 4)
  o la hoja del mes no está compartida con el robot
  (`lector-videos@sunviewparkvideos.iam.gserviceaccount.com`, permiso Lector).
- **Al abrir el correo sale otra cuenta** → en ese navegador hay que iniciar
  sesión con el Gmail de la tirolina.
- El PC no necesita tarjeta gráfica NVIDIA: sin ella todo funciona igual,
  solo el análisis y el render tardan algo más.

## Cada mes (lo único recurrente)

Cuando se cree el Google Sheets del mes nuevo: botón **Compartir** →
`lector-videos@sunviewparkvideos.iam.gserviceaccount.com` → **Lector**.
Nada más: la app lo encuentra automáticamente.
