# Canonical dev launcher for the ContentFactory API.
# Restart manually when needed — no auto-polling.
#
# Reads API_HOST and API_PORT from .env (same file uvicorn loads at startup),
# so the port is always authoritative from one place: .env only.
#
# NOTE: do NOT set $ErrorActionPreference='Stop' here — uvicorn writes its normal
# INFO logs to STDERR, and under 'Stop' PowerShell would treat the first such line
# as a terminating NativeCommandError and kill the server on startup.
Set-Location $PSScriptRoot

# Parse API_HOST / API_PORT from .env (lines like KEY=value, ignores comments).
$envFile = Join-Path $PSScriptRoot ".env"
$apiHost = "127.0.0.1"
$apiPort = "4000"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*API_HOST\s*=\s*(.+)$') { $apiHost = $Matches[1].Trim() }
        if ($_ -match '^\s*API_PORT\s*=\s*(.+)$') { $apiPort = $Matches[1].Trim() }
    }
}

# --- Auto-start ComfyUI (SDXL image/cover generation backend) --------------
# generate.py talks to ComfyUI at :8188 (COMFY_URL). Launch the portable build
# hidden if nothing is already holding the port. Idempotent (skips if up) and
# non-blocking (Start-Process returns immediately; ComfyUI warms up in parallel
# with the API — it idles until the first render). Models still run sequentially
# on the 8GB GPU; an idle ComfyUI process does not pin VRAM.
$comfyDir = "E:\Installed\ComfyUI\ComfyUI_windows_portable"
$comfyUp = $false
try { if (Get-NetTCPConnection -LocalPort 8188 -State Listen -ErrorAction Stop) { $comfyUp = $true } } catch { $comfyUp = $false }
if ($comfyUp) {
    Write-Host "ComfyUI already running on :8188"
} elseif (Test-Path "$comfyDir\python_embeded\python.exe") {
    Write-Host "Starting ComfyUI (:8188) ..."
    Start-Process -FilePath "$comfyDir\python_embeded\python.exe" -ArgumentList @('-s', 'ComfyUI\main.py', '--windows-standalone-build') -WorkingDirectory $comfyDir -WindowStyle Hidden
} else {
    Write-Host "WARN: ComfyUI not found at $comfyDir - image/cover generation will fail until it is started."
}

Write-Host "Starting API on $apiHost`:$apiPort ..."

# Build args as an array (avoids PowerShell backtick line-continuation pitfalls).
$uvicornArgs = @(
    "-m", "uvicorn", "main:app",
    "--host", $apiHost, "--port", $apiPort
)

# Call Python311 directly (bypasses the venv stub launcher so there is only ONE
# process instead of stub + child). The venv site-packages are injected via
# PYTHONPATH so imports resolve correctly without activating the venv.
$env:PYTHONPATH = "$PSScriptRoot\.venv\Lib\site-packages"
& "E:\Installed\Python311\python.exe" @uvicornArgs
