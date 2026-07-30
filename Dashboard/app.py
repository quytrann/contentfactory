"""ContentFactory launcher — opens the dashboard as a browser TAB.

Clicking the Desktop icon (or ContentFactory.bat) runs this. It makes sure the
API is serving on http://127.0.0.1:4000 (which also serves the built frontend —
no Vite), then opens that URL as a new tab in the default browser. It does NOT
wrap the app in its own window.

Lifecycle: the API runs as a DETACHED background process so it keeps serving
after this launcher exits (and after the browser tab is closed). Launch again
any time to open a fresh tab against the same running server. To STOP the API,
use the zombie-sweep in CLAUDE.md (kill the `uvicorn` python process on :4000).

STARTS ON LAUNCH (idempotent — skipped if already up):
  * the API/uvicorn on :4000 (also serves the frontend), and
  * ComfyUI (:8188) — the SDXL image/cover backend. It is a portable build (not a
    Windows service), so it does NOT auto-start on boot; the launcher brings it up
    so a fresh reboot + one click has everything for image/cover generation too.

NOT started (does not need to be):
  * PostgreSQL — it is installed as a Windows service set to Automatic, so it
    already auto-starts on boot. The dashboard needs it; if it is somehow stopped,
    the page still loads but data calls error until it is running.

Run:
  * Owner (1-click):  double-click  ContentFactory.bat  (or the Desktop shortcut)
  * Dev/manual:       Dashboard\api\.venv\Scripts\python.exe  Dashboard\app.py
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

# --- Paths (resolve exactly like run-api.ps1: cwd = Dashboard/api) -----------
_THIS = Path(__file__).resolve()
API_DIR = _THIS.parent / "api"
VENV_PY = API_DIR / ".venv" / "Scripts" / "python.exe"
# pythonw.exe = GUI-subsystem interpreter → NEVER opens a console window. Used for the
# detached API so no stray Python console lingers after the app opens.
VENV_PYW = API_DIR / ".venv" / "Scripts" / "pythonw.exe"

HOST = "127.0.0.1"
DEFAULT_PORT = 4000

# ComfyUI (SDXL image/cover backend). Portable build → not a Windows service, so
# it never auto-starts on boot; the launcher starts it. Path/port env-overridable.
COMFY_DIR = Path(os.getenv("CF_COMFY_DIR", r"E:\Installed\ComfyUI\ComfyUI_windows_portable"))
COMFY_PORT = int(os.getenv("CF_COMFY_PORT", "8188"))

# Windows process-creation flags: run the API fully detached + windowless so it
# outlives this launcher and shows no console.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def _read_env_hostport() -> tuple[str, int]:
    """Read API_HOST / API_PORT from Dashboard/api/.env — the single source of
    truth for the port (never assume 8000). Falls back to 127.0.0.1:4000."""
    host, port = HOST, DEFAULT_PORT
    envf = API_DIR / ".env"
    try:
        for raw in envf.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.split("#", 1)[0].strip()
            if key == "API_HOST" and val:
                host = val
            elif key == "API_PORT" and val.isdigit():
                port = int(val)
    except OSError:
        pass
    return host, port


def _http_ok(url: str, timeout: float = 1.5) -> bool:
    """True iff a GET returns HTTP 200. We probe '/' (the built index.html served
    by StaticFiles) rather than an /api route: '/' proves uvicorn is up WITHOUT
    needing PostgreSQL, so the tab opens even if the DB is down (the UI then
    surfaces its own data errors)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (localhost only)
            return r.status == 200
    except Exception:
        return False


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _wait_http(url: str, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _http_ok(url):
            return True
        time.sleep(0.5)
    return False


def _start_api_detached(host: str, port: int) -> None:
    """Start the backend (API + ComfyUI) via run-api.ps1 inside a HIDDEN PowerShell
    console — the proven no-window, no-flashing path.

    Why not pythonw / a windowless python subprocess: pythonw gives the API NO console
    at all, so every console CHILD it spawns during a job (cf-venv workers, ffmpeg,
    yt-dlp, claude -p) has no console to inherit and Windows allocates a NEW visible
    console for EACH one → the screen flashes windows continuously. run-api.ps1 runs the
    whole server under ONE hidden console (exactly like our manual
    `Start-Process -WindowStyle Hidden`), so the entire subprocess tree inherits it and
    nothing pops a window. run-api.ps1 also idempotently starts ComfyUI, so main() does
    not start it separately. host/port are unused here — run-api.ps1 reads them from the
    same .env. The hidden PowerShell process outlives this launcher."""
    creationflags = _CREATE_NO_WINDOW if os.name == "nt" else 0
    run_ps1 = API_DIR / "run-api.ps1"
    subprocess.Popen(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", f"& '{run_ps1}'"],
        cwd=str(API_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )


def _start_comfyui_if_down() -> None:
    """Idempotently start the portable ComfyUI on :8188 if nothing is serving it.

    Non-blocking: ComfyUI warms up in parallel and idles until the first render
    (an idle process does not pin the 8GB GPU). Mirrors the auto-start in
    run-api.ps1 so a fresh reboot + one click also covers image/cover generation."""
    if _port_in_use(HOST, COMFY_PORT):
        print(f"[launcher] ComfyUI already on :{COMFY_PORT}.")
        return
    py = COMFY_DIR / "python_embeded" / "python.exe"
    if not py.is_file():
        print(f"[launcher] ComfyUI not found at {COMFY_DIR} — "
              "image/cover generation will fail until it is started.")
        return
    creationflags = 0
    if os.name == "nt":
        creationflags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
    print(f"[launcher] starting ComfyUI (:{COMFY_PORT}) ...")
    subprocess.Popen(
        [str(py), "-s", "ComfyUI\\main.py", "--windows-standalone-build"],
        cwd=str(COMFY_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )


def main() -> int:
    host, port = _read_env_hostport()
    base = f"http://{host}:{port}"
    index = f"{base}/"

    if _http_ok(index):
        # Already serving — just open a fresh tab against the running server.
        print(f"[launcher] {base} already serving — opening a new tab.")
    elif _port_in_use(host, port):
        # Something holds the port but isn't serving our UI. Don't fight it;
        # open the tab anyway so the owner sees what's there.
        print(f"[launcher] port {port} in use but not serving ContentFactory — opening a tab anyway.")
    else:
        # Starts API + ComfyUI under one hidden console (run-api.ps1) — no window, no flashing.
        print(f"[launcher] starting backend (API + ComfyUI) on {base} ...")
        _start_api_detached(host, port)
        if not _wait_http(index, timeout=60.0):
            # Open anyway: uvicorn may still be warming up, or PostgreSQL is down
            # (the page loads but data calls error). Better than a silent no-op.
            print("[launcher] API not ready in 60s — opening a tab anyway "
                  "(check PostgreSQL if the dashboard shows data errors).")

    webbrowser.open_new_tab(base)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
