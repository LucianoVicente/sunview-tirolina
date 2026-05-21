@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" gui_tirolina.py
) else (
    start "" pythonw gui_tirolina.py
)
