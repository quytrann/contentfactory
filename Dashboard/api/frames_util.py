"""Host-side helpers for the no-speech source fallback (job runs in the FastAPI
host process, NOT cf-venv).

When faster-whisper returns ZERO segments for a source video (e.g. an animation
with only music / SFX and no spoken words), the pipeline has no transcript to
rewrite. The fallback instead shows Claude Code (vision) a handful of frames
sampled across the video plus the source metadata, and asks it to write a
Vietnamese explainer narration. This module produces those two inputs:

  * sample_frames(...)          -> evenly-spaced downscaled JPEG frames
  * fetch_source_metadata(...)  -> title / description / tags / categories

Both use the PROJECT tool binaries, resolved exactly like the rest of the API:
  * FFmpeg / ffprobe come from FFMPEG_BIN / FFPROBE_BIN in Dashboard/api/.env
    (a Gyan full build under E:\Installed\FFmpeg, NOT on PATH).
  * yt-dlp is invoked through the cf-venv Python's module route
    (`<cf-venv>\python.exe -m yt_dlp ...`) because the standalone yt-dlp.exe is
    broken on this machine. The CLI flags mirror the in-process ydl_opts used by
    the ingest / download workers (--js-runtimes node, --extractor-args
    player_client ladder, --ffmpeg-location <project ffmpeg dir>).

Subprocess output is always captured with encoding="utf-8", errors="replace"
(Windows defaults to cp1252 with text=True, which silently corrupts / crashes on
Vietnamese metadata).
"""

import json
import os
import subprocess
from pathlib import Path

try:
    # Load Dashboard/api/.env so FFMPEG_BIN / FFPROBE_BIN / CF_VENV_PYTHON are
    # available even when this module is imported/run standalone (the running API
    # already loads it via db.load_dotenv(); this is a harmless no-op then).
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass


# --- Tool-binary resolution (matches generate.py / the workers) --------------
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe")
# cf-venv Python — same default as generate.CF_VENV_PYTHON.
CF_VENV_PYTHON = os.getenv("CF_VENV_PYTHON", r"E:\Installed\cf-venv\Scripts\python.exe")


def _ffmpeg_dir() -> str | None:
    """Directory holding ffmpeg(.exe) — what yt-dlp's --ffmpeg-location wants.
    Derived from FFMPEG_BIN so it stays in lockstep with the .env path."""
    if FFMPEG_BIN and os.path.isfile(FFMPEG_BIN):
        return os.path.dirname(FFMPEG_BIN)
    return None


def _probe_duration(video_path: str) -> float:
    """Container duration (seconds) via the project ffprobe. 0.0 on any failure."""
    try:
        r = subprocess.run(
            [FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
             "-show_format", video_path],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        )
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def sample_frames(video_path: str, out_dir: str, n: int = 14,
                  long_edge: int = 576) -> list[dict]:
    """Extract `n` JPEG frames spread EVENLY across the video's duration.

    Probes the duration with the project ffprobe, then seeks to evenly-spaced
    timestamps inside [0.5s, duration-0.5s] (avoids black/blank first/last frames).
    Each frame is downscaled so its LONG edge == `long_edge` px (aspect kept),
    which keeps each image at ~500-970 vision tokens. Writes JPEGs at q=3.

    Returns a list of {"path": <abs jpg path>, "tsSec": <float source timestamp>}
    sorted by tsSec. Frames that fail to extract are skipped (best-effort).
    """
    os.makedirs(out_dir, exist_ok=True)
    dur = _probe_duration(video_path)

    # Build the evenly-spaced timestamp list inside a margin-trimmed window.
    if dur > 1.2:
        start, end = 0.5, dur - 0.5
    else:
        # Degenerate/short/unprobed input: fall back to a tiny sweep from 0.
        start, end = 0.0, max(dur, 0.0)

    n = max(1, int(n))
    if n == 1 or end <= start:
        timestamps = [round((start + end) / 2.0, 3)]
    else:
        span = end - start
        timestamps = [round(start + span * i / (n - 1), 3) for i in range(n)]

    # Long-edge downscale keeping aspect: landscape (aspect>1) -> width=long_edge,
    # portrait/square -> height=long_edge. -2 keeps the other edge even (JPEG-safe).
    scale = (f"scale='if(gt(a,1),{long_edge},-2)':'if(gt(a,1),-2,{long_edge})'")

    frames: list[dict] = []
    for i, ts in enumerate(timestamps):
        out_jpg = os.path.join(out_dir, f"frame_{i:02d}_{ts:.2f}s.jpg")
        # -ss BEFORE -i = fast input seek (keyframe-accurate is fine for samples).
        cmd = [
            FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{ts:.3f}", "-i", video_path,
            "-frames:v", "1", "-vf", scale, "-q:v", "3",
            out_jpg,
        ]
        try:
            subprocess.run(cmd, capture_output=True, encoding="utf-8",
                           errors="replace", timeout=60)
        except Exception:
            continue
        if os.path.isfile(out_jpg) and os.path.getsize(out_jpg) > 0:
            frames.append({"path": os.path.abspath(out_jpg), "tsSec": float(ts)})

    frames.sort(key=lambda f: f["tsSec"])
    return frames


def fetch_source_metadata(link: str) -> dict:
    """Fetch source metadata (no media download) via yt-dlp's module route.

    Returns {"title","description","tags","categories"} — strings/lists, with
    missing keys tolerated. Best-effort: on ANY failure returns the dict with
    empty values and never raises.

    Invokes `<cf-venv python> -m yt_dlp --dump-json --skip-download ...` because
    the standalone yt-dlp.exe is broken here. Flags mirror the workers' ydl_opts:
    --js-runtimes node (nsig solver) and the player_client ladder; plus
    --ffmpeg-location <project ffmpeg dir>.
    """
    empty = {"title": "", "description": "", "tags": [], "categories": []}
    if not link:
        return dict(empty)

    cmd = [
        CF_VENV_PYTHON, "-m", "yt_dlp",
        "--dump-json", "--skip-download",
        "--no-warnings", "--quiet", "--no-playlist",
        "--js-runtimes", "node",
        "--extractor-args", "youtube:player_client=android_vr,ios,web_safari,tv",
    ]
    ff_dir = _ffmpeg_dir()
    if ff_dir:
        cmd += ["--ffmpeg-location", ff_dir]
    cmd.append(link)

    try:
        r = subprocess.run(
            cmd, capture_output=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
        if r.returncode != 0 or not (r.stdout or "").strip():
            return dict(empty)
        # --dump-json emits one JSON object per line; take the first non-empty.
        line = next((ln for ln in r.stdout.splitlines() if ln.strip()), "")
        info = json.loads(line)
    except Exception:
        return dict(empty)

    return {
        "title": info.get("title") or "",
        "description": info.get("description") or "",
        "tags": info.get("tags") or [],
        "categories": info.get("categories") or [],
    }
