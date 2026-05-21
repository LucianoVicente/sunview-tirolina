@echo off
REM ============================================================
REM   Sunview Park - Instalador Windows
REM ============================================================
cd /d "%~dp0"
echo.
echo ============================================================
echo   INSTALADOR - Editor automatico de tirolina
echo ============================================================
echo.

REM Comprobar Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python no esta instalado.
    echo.
    echo Descargalo desde: https://www.python.org/downloads/
    echo IMPORTANTE: durante la instalacion, marca la casilla
    echo             "Add Python to PATH"
    echo.
    pause
    exit /b 1
)
echo [OK] Python encontrado
python --version

REM Comprobar FFmpeg
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] FFmpeg no encontrado.
    echo.
    echo Descargalo desde: https://www.gyan.dev/ffmpeg/builds/
    echo Elige "ffmpeg-release-essentials.zip", descomprime en C:\ffmpeg
    echo y anade C:\ffmpeg\bin a la variable PATH del sistema.
    echo.
    echo Despues de instalarlo, vuelve a ejecutar este archivo.
    echo.
    pause
    exit /b 1
)
echo [OK] FFmpeg encontrado

REM Crear entorno virtual
echo.
if not exist ".venv" (
    echo Creando entorno virtual...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] No se pudo crear el entorno virtual.
        pause
        exit /b 1
    )
)
echo [OK] Entorno virtual listo

REM Instalar dependencias
echo.
echo Instalando dependencias (puede tardar unos minutos)...
.venv\Scripts\pip install --upgrade pip --quiet
.venv\Scripts\pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] No se pudieron instalar las dependencias.
    pause
    exit /b 1
)
echo [OK] Dependencias instaladas

REM Descargar modelo Whisper
echo.
echo Descargando modelo de IA (primera vez ~240 MB, luego no hace falta)...
.venv\Scripts\python -c "import whisper; whisper.load_model('small'); print('[OK] Modelo de IA listo')"
if errorlevel 1 (
    echo [AVISO] No se descargo el modelo ahora. Se descargara la primera vez que uses el programa.
)

echo.
echo ============================================================
echo   INSTALACION COMPLETADA
echo ============================================================
echo.
echo Para editar videos haz doble clic en:  editar.bat
echo.
pause
