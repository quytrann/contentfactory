# Creates a Desktop shortcut "ContentFactory" that opens the dashboard as a
# native window with the app icon and NO console window (launches the API venv's
# pythonw.exe directly). Re-run any time to (re)create/refresh the shortcut.
$ErrorActionPreference = 'Stop'

$root    = Split-Path -Parent $MyInvocation.MyCommand.Definition
$pyw     = Join-Path $root 'Dashboard\api\.venv\Scripts\pythonw.exe'
$app     = Join-Path $root 'Dashboard\app.py'
$icon    = Join-Path $root 'Dashboard\assets\contentfactory.ico'
$workdir = Join-Path $root 'Dashboard\api'

foreach ($p in @($pyw, $app, $icon)) {
    if (-not (Test-Path $p)) { throw "Missing required file: $p" }
}

$desktop = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desktop 'ContentFactory.lnk'

$wsh = New-Object -ComObject WScript.Shell
$sc  = $wsh.CreateShortcut($lnkPath)
$sc.TargetPath       = $pyw
$sc.Arguments        = '"' + $app + '"'
$sc.WorkingDirectory = $workdir   # cwd = Dashboard/api, same as run-api.ps1
$sc.IconLocation     = $icon
$sc.WindowStyle      = 1
$sc.Description       = 'ContentFactory desktop dashboard'
$sc.Save()

Write-Host "Created Desktop shortcut: $lnkPath"
Write-Host "Target : $pyw"
Write-Host "Args   : $app"
Write-Host "Icon   : $icon"
