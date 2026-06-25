# Canonical dev launcher for the ContentFactory API.
# Restart manually when needed — no auto-polling.
#
# NOTE: do NOT set $ErrorActionPreference='Stop' here — uvicorn writes its normal
# INFO logs to STDERR, and under 'Stop' PowerShell would treat the first such line
# as a terminating NativeCommandError and kill the server on startup.
Set-Location $PSScriptRoot

# Build args as an array (avoids PowerShell backtick line-continuation pitfalls).
$uvicornArgs = @(
    "-m", "uvicorn", "main:app",
    "--host", "127.0.0.1", "--port", "4000",
    "--reload",
    "--reload-dir", "$PSScriptRoot"
)

& "$PSScriptRoot\.venv\Scripts\python.exe" @uvicornArgs
