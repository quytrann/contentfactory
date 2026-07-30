@echo off
REM ============================================================================
REM ContentFactory - one-click launcher.
REM Ensures the API is serving on :4000 (which also serves the frontend - no Vite,
REM no separate API terminal), then opens the dashboard as a new tab in your
REM default browser. The API keeps running in the background after the tab opens.
REM Just double-click this file.
REM
REM Prerequisites (owner starts these, as before):
REM   * PostgreSQL must be running (dashboard needs it to load data).
REM   * ComfyUI (:8188) only if you use image-mode / cover generation.
REM ============================================================================
setlocal
set "ROOT=%~dp0"
start "" "%ROOT%Dashboard\api\.venv\Scripts\pythonw.exe" "%ROOT%Dashboard\app.py"
endlocal
