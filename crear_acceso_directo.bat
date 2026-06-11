@echo off
rem Crea el acceso directo "Sunview Videos" en el escritorio (doble clic).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0crear_acceso_directo.ps1"
echo.
pause
