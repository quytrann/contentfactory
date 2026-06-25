# areach.ps1 — Per-project wrapper for agent-reach (installed globally via pipx)
# Copy this file into the ROOT of each project. Put a .env file next to it.
# What it does:
#   - Loads the project's .env  -> a SEPARATE identity per project (UPPERCASE env vars)
#   - Calls the global agent-reach (code runs from the pipx venv on E:, source repo stays clean)
#   - Output: just pass a relative --output path -> the file lands in the project
#
# Examples:
#   .\areach.ps1 doctor --json
#   .\areach.ps1 read "https://example.com"
#   .\areach.ps1 transcribe "<url>" --output .\out\transcript.txt
#
# IMPORTANT: do NOT run "agent-reach configure" (it saves credentials to the global
# config on E:, and the config FILE overrides ENV -> this disables per-project identity).

$ErrorActionPreference = "Stop"

# Path to the agent-reach binary installed by pipx (on E:). Change if on another machine.
$AgentReach = "E:\Installed\Users\Jake\.local\bin\agent-reach.exe"
if (-not (Test-Path $AgentReach)) {
    # Fallback: look it up on PATH (after opening a new terminal)
    $cmd = Get-Command agent-reach -ErrorAction SilentlyContinue
    if ($cmd) { $AgentReach = $cmd.Source } else {
        Write-Error "agent-reach not found. Run: py -m pipx ensurepath, then open a new terminal."
        exit 1
    }
}

# 1. Load per-project identity from .env (KEY=VALUE lines, skipping # and blank lines)
$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $idx = $line.IndexOf("=")
            $k = $line.Substring(0, $idx).Trim()
            $v = $line.Substring($idx + 1).Trim().Trim('"').Trim("'")
            if ($k) { Set-Item -Path "Env:$k" -Value $v }
        }
    }
    Write-Host "[areach] Loaded identity from $envFile" -ForegroundColor DarkGray
}

# 2. Call agent-reach with all passed arguments
& $AgentReach @args
exit $LASTEXITCODE
