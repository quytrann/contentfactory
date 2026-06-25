"""Facebook Page Reels upload — a publish step, lazy/best-effort like youtube_upload.

Publishing requires the page's OWN Facebook Page + a Page access token (the
per-page account-isolation rule). Neither the token nor the Page belongs to the
borrowed Claude account — they belong to the project owner.

Creds live OUTSIDE the DB/config as a path-only ref:
    Dashboard/secrets/<slug>/facebook.json   (gitignored)
FLAT, already-normalized shape (no nesting):
    {
      "platform": "facebook",
      "page_id": "...",
      "page_access_token": "...",   # NEVER printed/logged — refer to it as "(present)"
      "app_id": "...",
      "graph_version": "v25.0"
    }

This module talks to the Graph API directly over `requests` (no SDK). It implements
the 3-step Page Reels resumable upload (start -> upload bytes -> finish), enforces
the Reels media spec up front via ffprobe (so assembly knows to fix a bad file),
and never raises for the expected "not configured" cases — those come back as
{"ok": False, "reason": ...} so the runner can leave the video 'ready' instead of
failing the whole job.

NOTE: This is BUILD-ONLY for now. It is intentionally NOT wired into runner.py or
the publish endpoint — real publishing is owner-gated and comes later (it still
needs: a posts-table write, and the Reels rate limit of 30 posts / 24h enforced).
"""

import json
import os
import re
import subprocess
import unicodedata

import requests

GRAPH_HOST = "https://graph.facebook.com"
RUPLOAD_HOST = "https://rupload.facebook.com"
DEFAULT_GRAPH_VERSION = "v25.0"

# Reels media spec (Page Reels, Graph v25.0). Sources:
#   - 9:16 portrait, aspect ratio tolerance kept tight but not pixel-exact.
#   - duration 3s .. 90s.
#   - container mp4/mov, video H.264, audio AAC.
#   - resolution: at least 540x960 recommended; we cap the upper bound generously.
REELS_MIN_DURATION = 3.0
REELS_MAX_DURATION = 90.0
REELS_TARGET_AR = 9.0 / 16.0          # 0.5625
REELS_AR_TOLERANCE = 0.02             # allow ~0.5425..0.5825 (rounding / 1080x1920 etc.)
REELS_MIN_WIDTH = 540
REELS_MIN_HEIGHT = 960
REELS_MAX_WIDTH = 1920
REELS_MAX_HEIGHT = 1920
ALLOWED_VCODECS = {"h264", "avc1"}
ALLOWED_ACODECS = {"aac"}

def _ffprobe_bin() -> str:
    # Resolved at call time, not import time: main.py imports this module before
    # load_dotenv() runs, so reading the env eagerly would miss FFPROBE_BIN.
    return os.getenv("FFPROBE_BIN", "ffprobe")


# --------------------------------------------------------------------------- #
# Credential resolution (path-only; the token never leaves this module)
# --------------------------------------------------------------------------- #
def _slug(name: str) -> str:
    """ASCII-safe slug for per-page secrets folders (mirrors youtube_auth._slug).

    >>> _slug("Giải Thích Mọi Thứ")
    'giai-thich-moi-thu'
    """
    s = name.strip().replace("đ", "d").replace("Đ", "D")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


assert _slug("Giải Thích Mọi Thứ") == "giai-thich-moi-thu"


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def resolve_creds_path(page) -> str | None:
    """Resolve the path to a page's facebook.json.

    `page` may be:
      - a dict with 'credentials_ref' (preferred; from platform_accounts), and/or
        'name'/'slug' to fall back on the slug convention.
      - a plain page-name string.
    Returns an absolute path or None. Does NOT open the file.
    """
    ref = None
    name = None
    if isinstance(page, dict):
        ref = page.get("credentials_ref")
        name = page.get("name") or page.get("page_name") or page.get("slug")
    elif isinstance(page, str):
        name = page

    if ref:
        return ref if os.path.isabs(ref) else os.path.join(_repo_root(), ref)
    if name:
        slug = name if re.fullmatch(r"[a-z0-9-]+", name or "") else _slug(name)
        return os.path.join(_repo_root(), "Dashboard", "secrets", slug, "facebook.json")
    return None


def load_creds(page) -> dict:
    """Load + validate the flat facebook.json. Raises ValueError with a clear,
    token-free message on any problem."""
    path = resolve_creds_path(page)
    if not path:
        raise ValueError("could not resolve a facebook credentials path for this page")
    if not os.path.isfile(path):
        raise ValueError(f"facebook credentials not found at {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    missing = [k for k in ("page_id", "page_access_token") if not data.get(k)]
    if missing:
        raise ValueError(f"facebook.json is missing required key(s): {', '.join(missing)}")
    data.setdefault("graph_version", DEFAULT_GRAPH_VERSION)
    return data


# --------------------------------------------------------------------------- #
# Spec enforcement (ffprobe). Pure function: probe -> {ok, reason?, info}
# --------------------------------------------------------------------------- #
def _ffprobe_json(video_path: str) -> dict:
    # ffprobe emits UTF-8 JSON (paths/metadata can be Vietnamese). On Windows,
    # text=True decodes with the locale codec (cp1252) and chokes on non-ASCII
    # bytes (e.g. 0x8d) -> UnicodeDecodeError in the reader thread -> empty
    # stdout -> {} -> false "no video stream found". Force UTF-8 decoding.
    proc = subprocess.run(
        [_ffprobe_bin(), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", video_path],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {(proc.stderr or '').strip()[:300]}")
    return json.loads(proc.stdout or "{}")


def check_reel_spec(video_path: str) -> dict:
    """Validate a file against the Page Reels spec WITHOUT uploading.

    Returns {"ok": True, "info": {...}} when acceptable, otherwise
    {"ok": False, "reason": "<why>", "info": {...}} so assembly can fix it.
    """
    if not os.path.isfile(video_path):
        return {"ok": False, "reason": f"video file not found: {video_path}", "info": {}}

    ext = os.path.splitext(video_path)[1].lower()
    if ext not in (".mp4", ".mov"):
        return {"ok": False, "reason": f"container must be .mp4/.mov, got '{ext}'", "info": {}}

    try:
        meta = _ffprobe_json(video_path)
    except Exception as exc:
        return {"ok": False, "reason": str(exc), "info": {}}

    streams = meta.get("streams", [])
    vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
    astream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = meta.get("format", {})

    if not vstream:
        return {"ok": False, "reason": "no video stream found", "info": {}}

    width = int(vstream.get("width") or 0)
    height = int(vstream.get("height") or 0)
    vcodec = (vstream.get("codec_name") or "").lower()
    acodec = (astream.get("codec_name") or "").lower() if astream else None
    try:
        duration = float(fmt.get("duration") or vstream.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    ar = (width / height) if height else 0.0

    info = {
        "width": width, "height": height, "duration": round(duration, 2),
        "aspect_ratio": round(ar, 4), "vcodec": vcodec, "acodec": acodec,
        "container": ext.lstrip("."),
    }

    # Orientation / aspect ratio (must be ~9:16 portrait)
    if width >= height:
        return {"ok": False, "reason": f"not portrait: {width}x{height} (need 9:16)", "info": info}
    if abs(ar - REELS_TARGET_AR) > REELS_AR_TOLERANCE:
        return {"ok": False,
                "reason": f"aspect ratio {ar:.4f} not ~9:16 ({REELS_TARGET_AR:.4f} ±{REELS_AR_TOLERANCE})",
                "info": info}

    # Resolution bounds
    if width < REELS_MIN_WIDTH or height < REELS_MIN_HEIGHT:
        return {"ok": False,
                "reason": f"resolution {width}x{height} below Reels min {REELS_MIN_WIDTH}x{REELS_MIN_HEIGHT}",
                "info": info}
    if width > REELS_MAX_WIDTH or height > REELS_MAX_HEIGHT:
        return {"ok": False,
                "reason": f"resolution {width}x{height} above Reels max {REELS_MAX_WIDTH}x{REELS_MAX_HEIGHT}",
                "info": info}

    # Duration
    if duration < REELS_MIN_DURATION:
        return {"ok": False,
                "reason": f"duration {duration:.2f}s below min {REELS_MIN_DURATION}s", "info": info}
    if duration > REELS_MAX_DURATION:
        return {"ok": False,
                "reason": f"duration {duration:.2f}s exceeds Reels max {REELS_MAX_DURATION}s", "info": info}

    # Codecs
    if vcodec not in ALLOWED_VCODECS:
        return {"ok": False,
                "reason": f"video codec '{vcodec}' not allowed (need H.264)", "info": info}
    if astream is None:
        return {"ok": False, "reason": "no audio stream (Reels needs AAC audio)", "info": info}
    if acodec not in ALLOWED_ACODECS:
        return {"ok": False,
                "reason": f"audio codec '{acodec}' not allowed (need AAC)", "info": info}

    return {"ok": True, "info": info}


# --------------------------------------------------------------------------- #
# Graph API low-level helpers. The token is read from creds and sent to Graph,
# but it is NEVER returned, printed, or placed in an error string.
# --------------------------------------------------------------------------- #
def _graph_url(creds: dict, path: str) -> str:
    ver = creds.get("graph_version") or DEFAULT_GRAPH_VERSION
    return f"{GRAPH_HOST}/{ver}/{path}"


def _scrub(text: str, token: str | None) -> str:
    """Defensive: strip the token out of any string before it reaches a caller."""
    if token and text:
        text = text.replace(token, "(present)")
    return text


def health_check(page) -> dict:
    """GET /me using the Page token. Returns {ok, id?, name?, reason?}. No token echoed."""
    try:
        creds = load_creds(page)
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}
    token = creds["page_access_token"]
    try:
        r = requests.get(_graph_url(creds, "me"),
                         params={"fields": "id,name", "access_token": token}, timeout=30)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or "error" in body:
            msg = body.get("error", {}).get("message", r.text)
            return {"ok": False, "status": r.status_code, "reason": _scrub(str(msg), token)}
        return {"ok": True, "id": body.get("id"), "name": body.get("name")}
    except Exception as exc:
        return {"ok": False, "reason": _scrub(str(exc), token)}


def _reels_start(creds: dict) -> dict:
    """upload_phase=start -> {ok, video_id?, upload_url?, reason?}."""
    token = creds["page_access_token"]
    url = _graph_url(creds, f"{creds['page_id']}/video_reels")
    r = requests.post(url, data={"upload_phase": "start", "access_token": token}, timeout=60)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "error" in body:
        msg = body.get("error", {}).get("message", r.text)
        return {"ok": False, "status": r.status_code, "reason": _scrub(str(msg), token)}
    return {"ok": True, "video_id": body.get("video_id"), "upload_url": body.get("upload_url")}


def _reels_upload(creds: dict, video_id: str, video_path: str) -> dict:
    """Phase 2: POST raw bytes to rupload.facebook.com. {ok, reason?}."""
    token = creds["page_access_token"]
    ver = creds.get("graph_version") or DEFAULT_GRAPH_VERSION
    size = os.path.getsize(video_path)
    url = f"{RUPLOAD_HOST}/video-upload/{ver}/{video_id}"
    headers = {
        "Authorization": f"OAuth {token}",
        "offset": "0",
        "file_size": str(size),
    }
    with open(video_path, "rb") as f:
        r = requests.post(url, headers=headers, data=f, timeout=600)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "error" in body or not body.get("success", True):
        msg = body.get("error", {}).get("message", r.text) if body else r.text
        return {"ok": False, "status": r.status_code, "reason": _scrub(str(msg), token)}
    return {"ok": True}


def _reels_finish(creds: dict, video_id: str, caption: str, state: str) -> dict:
    """upload_phase=finish -> publish or draft. {ok, reason?}."""
    token = creds["page_access_token"]
    url = _graph_url(creds, f"{creds['page_id']}/video_reels")
    data = {
        "upload_phase": "finish",
        "video_id": video_id,
        "video_state": state,           # PUBLISHED | DRAFT | SCHEDULED
        "description": caption or "",
        "access_token": token,
    }
    r = requests.post(url, data=data, timeout=120)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "error" in body:
        msg = body.get("error", {}).get("message", r.text)
        return {"ok": False, "status": r.status_code, "reason": _scrub(str(msg), token)}
    return {"ok": True, "post_id": body.get("post_id"), "success": body.get("success", True)}


def delete_video(page, video_id: str) -> dict:
    """DELETE /{video_id} — used to clean up a test DRAFT. {ok, reason?}."""
    try:
        creds = load_creds(page)
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}
    token = creds["page_access_token"]
    try:
        r = requests.delete(_graph_url(creds, str(video_id)),
                            params={"access_token": token}, timeout=60)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or "error" in body:
            msg = body.get("error", {}).get("message", r.text)
            return {"ok": False, "status": r.status_code, "reason": _scrub(str(msg), token)}
        return {"ok": True, "success": body.get("success", True)}
    except Exception as exc:
        return {"ok": False, "reason": _scrub(str(exc), token)}


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def publish_reel(page, video_path: str, caption: str = "",
                 state: str = "PUBLISHED", dry_run: bool = False) -> dict:
    """Publish one MP4 as a Page Reel. Returns {ok, video_id?, post_id?, reason?, ...}.

    Steps: load creds -> spec check -> start -> (upload -> finish) unless dry_run.

    - dry_run=True stops after `start` (no bytes uploaded, no post created); proves
      auth + permission + endpoint with zero published content.
    - state="DRAFT" finishes as a non-public draft (caller is responsible for
      deleting it afterwards via delete_video()); "PUBLISHED" goes public.

    Never raises for the expected cases — returns {"ok": False, "reason": ...}.
    """
    state = (state or "PUBLISHED").upper()
    if state not in ("PUBLISHED", "DRAFT", "SCHEDULED"):
        return {"ok": False, "reason": f"invalid state '{state}'"}

    try:
        creds = load_creds(page)
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}

    spec = check_reel_spec(video_path)
    if not spec["ok"]:
        return {"ok": False, "reason": f"spec check failed: {spec['reason']}", "spec": spec}

    started = _reels_start(creds)
    if not started["ok"]:
        return {"ok": False, "reason": f"start phase failed: {started['reason']}"}
    video_id = started["video_id"]

    if dry_run:
        return {"ok": True, "phase": "start", "video_id": video_id,
                "note": "dry_run: no bytes uploaded, no post created", "spec": spec["info"]}

    up = _reels_upload(creds, video_id, video_path)
    if not up["ok"]:
        return {"ok": False, "reason": f"upload phase failed: {up['reason']}", "video_id": video_id}

    fin = _reels_finish(creds, video_id, caption, state)
    if not fin["ok"]:
        return {"ok": False, "reason": f"finish phase failed: {fin['reason']}", "video_id": video_id}

    return {"ok": True, "phase": "finish", "video_id": video_id,
            "post_id": fin.get("post_id"), "state": state, "spec": spec["info"]}
