# exit_project.ps1 — "Exit project" killer for the ContentFactory stack.
#
# Launched DETACHED (not a child of the API) by POST /api/shutdown so that when
# this script kills the API/uvicorn LAST, it is not killing its own parent
# mid-run. Runs in a hidden PowerShell console; self-exits at the end.
#
# ==========================  HARD SAFETY RULES  ==============================
#  * NEVER match or kill anything related to `postgres` / PostgreSQL. The DB is a
#    Windows service (postgresql-x64-16) under NetworkService and is OFF LIMITS.
#    None of the patterns below reference it, and no port fallback touches 5432.
#  * NEVER kill by a bare generic pattern like `python` or `node` alone. Every
#    match REQUIRES a project-specific qualifier: a worker script name, the
#    literal `ContentFactory`, `uvicorn`/`multiprocessing`/`spawn_main`,
#    `ComfyUI`, `vite`, or `run-api.ps1`.
#  * Kill the API / uvicorn / run-api.ps1 group LAST, so every earlier step
#    (workers, media, ComfyUI, vite) has already completed before this script's
#    own HTTP server dies.
# =============================================================================

param([int]$ApiPort = 4000, [int]$ComfyPort = 8188)

# Let the HTTP 200 flush and the browser tab react/close before we start tearing
# down (this script's own API server is the LAST thing killed, further below).
Start-Sleep -Milliseconds 1500

# Kill every process whose FULL command line matches $Pattern (regex). Guarded so
# a no-match / access-denied never aborts the sweep. Get-CimInstance Win32_Process
# is the reliable way to read a process's command line on Windows.
function Kill-ByCmdline {
    param([string]$Pattern)
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and ($_.CommandLine -match $Pattern) } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
}

# Kill the LISTENer owning a TCP port (fallback when the cmdline match misses).
function Kill-ByPort {
    param([int]$Port)
    try {
        Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique |
            ForEach-Object {
                Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
            }
    } catch {}
}

# (a) cf-venv job workers (whisper / tts / ingest / download / probe / stickman /
#     caption-cover / prewarm). These are the project's own worker scripts run in the
#     cf-venv via subprocess. Every token is a project-specific worker filename, so
#     there is no false-positive risk against unrelated python processes.
Kill-ByCmdline 'whisper_worker|tts_worker|ingest_worker|download_worker|probe_worker|stickman_procedural|caption_cover|prewarm_worker'

# (b) project media subprocesses. Scope TIGHTLY: only ffmpeg/ffprobe whose command
#     line ALSO contains the project root token `ContentFactory` (output/input paths
#     live under E:\ContentFactory\<page> or the repo) — so unrelated ffmpeg on the
#     machine is never touched. Same qualifier for yt-dlp (run as `python -m yt_dlp`).
Kill-ByCmdline '(ffmpeg|ffprobe).*ContentFactory'
Kill-ByCmdline 'ContentFactory.*(ffmpeg|ffprobe)'
Kill-ByCmdline 'yt_dlp.*ContentFactory'
Kill-ByCmdline 'ContentFactory.*yt_dlp'

# (c) ComfyUI (portable build running `ComfyUI\main.py` on :8188). Cmdline match
#     first, then the port owner as a fallback.
Kill-ByCmdline 'ComfyUI'
Kill-ByPort $ComfyPort

# (d) vite dev server (dev only; a node process whose cmdline contains `vite`).
#     Harmless no-op in production where no vite runs.
Kill-ByCmdline 'vite'

# (e) LAST: the API itself + its launcher. Covers the uvicorn parent, its
#     multiprocessing/spawn_main worker children (the CLAUDE.md zombie sweep), and
#     the hidden `run-api.ps1` PowerShell launcher. Port $ApiPort owner as fallback.
#     NOTE: this killer runs from exit_project.ps1, which matches NONE of these
#     tokens, so it does not kill itself before finishing.
Kill-ByCmdline 'uvicorn|multiprocessing|spawn_main|run-api\.ps1'
Kill-ByPort $ApiPort

# Self-clean: the hidden console exits on its own once the script returns. Nothing
# to delete (the script file stays for the next "Exit project"). Done.
exit 0
