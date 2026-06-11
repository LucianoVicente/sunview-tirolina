# Crea el acceso directo "Sunview Videos" en el escritorio del usuario.
# Se ejecuta con doble clic en crear_acceso_directo.bat (una sola vez por PC).

$base = Split-Path -Parent $MyInvocation.MyCommand.Path
$escritorio = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $escritorio "Sunview Videos.lnk"

$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)

# Si el entorno virtual ya esta instalado, lanzar pythonw directamente
# (sin ventana de consola). Si no, usar editar.bat minimizado como respaldo.
$pyw = Join-Path $base ".venv\Scripts\pythonw.exe"
if (Test-Path $pyw) {
    $s.TargetPath = $pyw
    $s.Arguments = '"' + (Join-Path $base "gui_tirolina.py") + '"'
} else {
    $s.TargetPath = Join-Path $base "editar.bat"
    $s.WindowStyle = 7
}
$s.WorkingDirectory = $base
$ico = Join-Path $base "assets\logo.ico"
if (Test-Path $ico) { $s.IconLocation = $ico }
$s.Description = "Editor de videos Sunview Park"
$s.Save()

Write-Host ""
Write-Host "Listo: acceso directo 'Sunview Videos' creado en el escritorio."
Write-Host ($lnk)
