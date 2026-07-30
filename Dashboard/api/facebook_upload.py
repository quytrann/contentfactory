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
import time
import unicodedata

import requests

GRAPH_HOST = "https://graph.facebook.com"
RUPLOAD_HOST = "https://rupload.facebook.com"
DEFAULT_GRAPH_VERSION = "v25.0"

# Feed resumable upload: cap how many bytes we hold in memory / send per transfer
# POST. Facebook's start/transfer responses drive the offsets; we send at most this
# many bytes per request (re-reading the returned start_offset each loop) so a huge
# returned window can't balloon memory. 8 MiB is a safe, typical chunk size.
FEED_UPLOAD_CHUNK = 8 * 1024 * 1024

# In-process upload progress store for the chunked feed upload, so the FE can poll a
# % bar during the slow multi-minute transfer. Keyed by a caller-supplied
# `progress_key` (publish_core uses "<video_id>:facebook"). Single writer per key
# (the publish POST runs in one threadpool worker) while the progress GET only READS
# — a plain dict is safe here; readers must tolerate a missing key. Entries linger
# ~PROGRESS_TTL seconds after done/error so the FE can read the final 100%, then a
# lazy sweep on each write drops them (bounded growth, no background thread).
_UPLOAD_PROGRESS: dict[str, dict] = {}
PROGRESS_TTL = 30.0  # seconds to keep a done/error entry before eviction


def _progress_set(key: str | None, **fields) -> None:
    """Merge fields into a progress entry (best-effort; no-op when key is None).
    Stamps updatedAt and lazily evicts stale done/error entries on every write."""
    if not key:
        return
    entry = _UPLOAD_PROGRESS.get(key)
    if entry is None:
        entry = {"phase": "start", "bytesSent": 0, "bytesTotal": 0, "pct": 0.0}
        _UPLOAD_PROGRESS[key] = entry
    entry.update(fields)
    entry["updatedAt"] = time.time()
    # Lazy sweep: drop any finished entry that has outlived the TTL.
    now = time.time()
    for k in [k for k, e in _UPLOAD_PROGRESS.items()
              if e.get("phase") in ("done", "error")
              and now - (e.get("updatedAt") or now) > PROGRESS_TTL]:
        _UPLOAD_PROGRESS.pop(k, None)


def get_upload_progress(key: str) -> dict | None:
    """Read a progress entry (or None). Evicts it if it's a done/error entry that has
    outlived the TTL, so a stale 100%/error doesn't report as active forever."""
    entry = _UPLOAD_PROGRESS.get(key)
    if entry is None:
        return None
    if entry.get("phase") in ("done", "error"):
        if time.time() - (entry.get("updatedAt") or 0) > PROGRESS_TTL:
            _UPLOAD_PROGRESS.pop(key, None)
            return None
    return entry

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

# Regular Page video post (/{page_id}/videos). Unlike Reels this accepts ANY
# orientation (landscape 16:9, square, portrait) and much longer durations, so a
# 16:9 / >90s video that Reels rejects is posted here as a normal feed video.
# Container/codec stay the platform-accepted set; duration ceiling is the Page
# video hard max (240 min). Aspect is NOT gated.
FEED_MIN_DURATION = 1.0
FEED_MAX_DURATION = 14400.0           # 240 min (Page video upload ceiling)

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


def check_feed_spec(video_path: str) -> dict:
    """Validate a file against the regular Page video-post spec (NOT Reels).

    Lenient, orientation-agnostic: container mp4/mov, H.264 video, duration within
    1s..240min. Aspect ratio is NOT gated (16:9 landscape is the whole point).
    Same return shape as check_reel_spec so publish_core treats both identically.
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

    if vcodec not in ALLOWED_VCODECS:
        return {"ok": False, "reason": f"video codec '{vcodec}' not allowed (need H.264)", "info": info}
    if duration < FEED_MIN_DURATION:
        return {"ok": False, "reason": f"duration {duration:.2f}s below min {FEED_MIN_DURATION}s", "info": info}
    if duration > FEED_MAX_DURATION:
        return {"ok": False,
                "reason": f"duration {duration:.2f}s exceeds Page video max {FEED_MAX_DURATION:.0f}s", "info": info}

    return {"ok": True, "info": info}


def decide_mode(video_path: str) -> dict:
    """Recommend the Facebook publish surface from the video's shape (mirrors how
    YouTube auto-handles orientation). Portrait ~9:16 AND 3..90s → 'reel';
    everything else (16:9, square, or too long/short for a Reel) → 'feed'.

    Returns {"mode": "reel"|"feed", "info": {...}, "reel_ok": bool, "reel_reason": str?}.
    Probes once; on an unreadable file it falls back to 'feed' (the lenient path).
    """
    reel = check_reel_spec(video_path)
    if reel["ok"]:
        return {"mode": "reel", "info": reel["info"], "reel_ok": True}
    return {"mode": "feed", "info": reel.get("info", {}),
            "reel_ok": False, "reel_reason": reel.get("reason")}


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


def _graph_reason(status: int, body: dict | None, raw_text: str, token: str | None) -> str:
    """Build a MEANINGFUL failure reason even when the response body is empty or
    non-JSON (e.g. the HTTP 413 'Payload Too Large' that a too-big single upload
    returns with a blank body — which previously surfaced as an empty reason).

    Prefers Graph's structured error message; falls back to a status-specific note
    (413 → 'payload too large') or the raw text; never leaks the token."""
    if isinstance(body, dict) and (body.get("error") or {}).get("message"):
        return _scrub(str(body["error"]["message"]), token)
    if status == 413:
        return f"HTTP {status} (payload too large)"
    raw = (raw_text or "").strip()
    return f"HTTP {status}: {_scrub(raw, token) if raw else 'no error body'}"


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


# Page-level "Views" metric. Meta deprecated the classic page_impressions/page_fans
# on 2025-11-15 and replaced them with the unified `page_media_view` ("Facebook
# views" in Business Suite). Requires a PAGE token WITH read_insights + the ANALYZE
# task on the Page (a User token or a token without read_insights returns
# (#190)/empty). See fetch_page_analytics for how this feeds the traffic chart.
def page_insights_views(page, since: int | None = None, until: int | None = None,
                        period: str = "day") -> dict:
    """Daily page 'views' (page_media_view) time series over [since, until] (unix).

    Returns {ok, points: [{date: 'YYYY-MM-DD', value: int}], reason?}. Never raises
    for the expected cases; the token is never echoed in an error string.
    """
    try:
        creds = load_creds(page)
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}
    token = creds["page_access_token"]
    params = {"metric": "page_media_view", "period": period, "access_token": token}
    if since:
        params["since"] = int(since)
    if until:
        params["until"] = int(until)
    try:
        r = requests.get(_graph_url(creds, f"{creds['page_id']}/insights"), params=params, timeout=45)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or "error" in body:
            msg = body.get("error", {}).get("message", r.text)
            return {"ok": False, "status": r.status_code, "reason": _scrub(str(msg), token)}
        data = body.get("data") or []
        values = data[0].get("values", []) if data else []
        points = [
            {"date": (v.get("end_time") or "")[:10], "value": int(v.get("value") or 0)}
            for v in values
        ]
        return {"ok": True, "points": points}
    except Exception as exc:
        return {"ok": False, "reason": _scrub(str(exc), token)}


def page_followers(page) -> dict:
    """Page follower + likes counts via the Graph node fields.

    GET /{page-id}?fields=followers_count,fan_count using the SAME page access token
    + page-id resolution as page_insights_views (reuses load_creds — path-only token,
    no duplicate secret handling). `followers_count` = people following the Page;
    `fan_count` = people who like the Page.

    Returns {ok, followers: int|None, fanCount: int|None, reason?}. Best-effort:
    NEVER raises; on any failure returns ok:False with the scrubbed error and null
    counts. The token is never echoed in an error string.
    """
    try:
        creds = load_creds(page)
    except ValueError as exc:
        return {"ok": False, "followers": None, "fanCount": None, "reason": str(exc)}
    token = creds["page_access_token"]
    try:
        r = requests.get(
            _graph_url(creds, str(creds["page_id"])),
            params={"fields": "followers_count,fan_count", "access_token": token},
            timeout=45,
        )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or "error" in body:
            msg = body.get("error", {}).get("message", r.text)
            return {"ok": False, "followers": None, "fanCount": None,
                    "status": r.status_code, "reason": _scrub(str(msg), token)}
        followers = body.get("followers_count")
        fan = body.get("fan_count")
        return {
            "ok": True,
            "followers": int(followers) if followers is not None else None,
            "fanCount": int(fan) if fan is not None else None,
        }
    except Exception as exc:
        return {"ok": False, "followers": None, "fanCount": None,
                "reason": _scrub(str(exc), token)}


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


def _reels_finish(creds: dict, video_id: str, caption: str, state: str,
                  scheduled_publish_time: int | None = None) -> dict:
    """upload_phase=finish -> publish, draft, or schedule. {ok, reason?}."""
    token = creds["page_access_token"]
    url = _graph_url(creds, f"{creds['page_id']}/video_reels")
    data = {
        "upload_phase": "finish",
        "video_id": video_id,
        "video_state": state,           # PUBLISHED | DRAFT | SCHEDULED
        "description": caption or "",
        "access_token": token,
    }
    # SCHEDULED needs a unix publish time (Facebook enforces ~10 min..months ahead).
    if state == "SCHEDULED" and scheduled_publish_time:
        data["scheduled_publish_time"] = str(int(scheduled_publish_time))
    r = requests.post(url, data=data, timeout=120)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "error" in body:
        msg = body.get("error", {}).get("message", r.text)
        return {"ok": False, "status": r.status_code, "reason": _scrub(str(msg), token)}
    return {"ok": True, "post_id": body.get("post_id"), "success": body.get("success", True)}


def _set_video_thumbnail(creds: dict, video_id: str, thumb_path: str) -> dict:
    """Set a custom cover image on a Page video/reel via the Video Thumbnails edge.

    POST /{video_id}/thumbnails as multipart/form-data with source=@<image> and
    is_preferred=true (Graph v25.0 — CONFIRMED for BOTH regular Page videos AND
    reels: a reel's video_id is a Page-associated video, and Meta's Reels
    Publishing guide sanctions "Add a custom cover photo for your reel" via this
    same edge; the video_reels finish call itself has NO cover/thumb parameter).
    Docs: /docs/graph-api/reference/video/thumbnails/ and /docs/video-api/guides/reels-publishing/.

    BEST-EFFORT and NON-FATAL: returns {ok, reason?} and NEVER raises. The token is
    scrubbed from any error text. Meta caps the thumbnail at 10MB and recommends the
    same aspect ratio as the video (we don't hard-enforce — a bad image just yields
    a captured Graph error). The create response is documented as {"success": true}
    (the thumbnail id is read-back-only), so we don't rely on an id in the reply."""
    token = creds["page_access_token"]
    if not thumb_path or not os.path.isfile(thumb_path):
        return {"ok": False, "reason": "thumbnail file not found on disk"}
    # Soft guard on Meta's 10MB cap so we return a clear reason instead of a Graph error.
    try:
        if os.path.getsize(thumb_path) > 10 * 1024 * 1024:
            return {"ok": False, "reason": "thumbnail exceeds Facebook's 10MB limit"}
    except OSError:
        pass
    url = _graph_url(creds, f"{video_id}/thumbnails")
    try:
        with open(thumb_path, "rb") as f:
            r = requests.post(
                url,
                data={"is_preferred": "true", "access_token": token},
                files={"source": f},
                timeout=120,
            )
    except Exception as exc:
        return {"ok": False, "reason": _scrub(str(exc), token)}
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "error" in body:
        msg = body.get("error", {}).get("message", r.text) if body else r.text
        return {"ok": False, "status": r.status_code, "reason": _scrub(str(msg), token)}
    return {"ok": True, "success": body.get("success", True)}


def get_permalink(creds: dict, video_id: str) -> dict:
    """GET /{video_id}?fields=permalink_url — Facebook's DECLARED canonical link to
    the object. This is authoritative and reflects Facebook's video/reels MERGE:
    for this account FB returns "/reel/{id}/" even for a landscape Page video (it
    files everything under Reels), so we must store what FB says rather than guess a
    "/watch/?v=" URL that FB does not use.

    BEST-EFFORT: returns {ok, url?} and NEVER raises; the token is never echoed. When
    Graph omits permalink_url (e.g. a not-yet-public DRAFT/SCHEDULED post) ok=False
    and the caller falls back to a surface-specific id URL."""
    token = creds["page_access_token"]
    try:
        r = requests.get(_graph_url(creds, str(video_id)),
                         params={"fields": "permalink_url", "access_token": token}, timeout=30)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or "error" in body:
            return {"ok": False}
        link = body.get("permalink_url")
        if not link:
            return {"ok": False}
        # permalink_url is a SITE-RELATIVE path (e.g. "/reel/123/"); make it absolute.
        if str(link).startswith("http"):
            url = link
        else:
            url = "https://www.facebook.com" + (link if link.startswith("/") else "/" + link)
        return {"ok": True, "url": url}
    except Exception:
        return {"ok": False}


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
            err = body.get("error", {})
            msg = err.get("message", r.text)
            # Surface Graph's error code + subcode so callers can distinguish an
            # already-gone object (code 100 / subcode 33) from a real permission
            # or transient failure. Deleting an object FB has already removed
            # returns code 100 / subcode 33 (NOT a clean HTTP 404).
            return {
                "ok": False,
                "status": r.status_code,
                "reason": _scrub(str(msg), token),
                "code": err.get("code"),
                "error_subcode": err.get("error_subcode"),
            }
        return {"ok": True, "success": body.get("success", True)}
    except Exception as exc:
        return {"ok": False, "reason": _scrub(str(exc), token)}


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def publish_reel(page, video_path: str, caption: str = "",
                 state: str = "PUBLISHED", dry_run: bool = False,
                 scheduled_publish_time: int | None = None,
                 thumb_path: str | None = None,
                 progress_key: str | None = None) -> dict:
    """Publish one MP4 as a Page Reel. Returns {ok, video_id?, post_id?, reason?, ...}.

    Steps: load creds -> spec check -> start -> (upload -> finish) unless dry_run.

    - dry_run=True stops after `start` (no bytes uploaded, no post created); proves
      auth + permission + endpoint with zero published content.
    - state="PUBLISHED" goes public; "SCHEDULED" publishes at scheduled_publish_time
      (a unix timestamp; shows in the Page's scheduled content); "DRAFT" finishes as
      a non-public draft.
    - thumb_path (optional): a custom cover image set BEST-EFFORT after finish via
      POST /{video_id}/thumbnails (a reel has NO cover param on the finish call). A
      thumbnail failure never fails the publish — the outcome is reported under the
      returned "thumb" key.

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

    # Reels use a single-shot rupload (no chunk offsets), so progress is coarse:
    # start (0%) -> done (100%). Report the file size as the total for the FE bar.
    _total = os.path.getsize(video_path) if os.path.isfile(video_path) else 0
    _progress_set(progress_key, phase="start", bytesSent=0, bytesTotal=_total, pct=0.0)

    started = _reels_start(creds)
    if not started["ok"]:
        _progress_set(progress_key, phase="error")
        return {"ok": False, "reason": f"start phase failed: {started['reason']}"}
    video_id = started["video_id"]

    if dry_run:
        return {"ok": True, "phase": "start", "video_id": video_id,
                "note": "dry_run: no bytes uploaded, no post created", "spec": spec["info"]}

    up = _reels_upload(creds, video_id, video_path)
    if not up["ok"]:
        _progress_set(progress_key, phase="error")
        return {"ok": False, "reason": f"upload phase failed: {up['reason']}", "video_id": video_id}

    _progress_set(progress_key, phase="finish", bytesSent=_total, bytesTotal=_total, pct=100.0)
    fin = _reels_finish(creds, video_id, caption, state, scheduled_publish_time)
    if not fin["ok"]:
        _progress_set(progress_key, phase="error")
        return {"ok": False, "reason": f"finish phase failed: {fin['reason']}", "video_id": video_id}

    _progress_set(progress_key, phase="done", bytesSent=_total, bytesTotal=_total, pct=100.0)
    out = {"ok": True, "phase": "finish", "video_id": video_id,
           "post_id": fin.get("post_id"), "state": state, "spec": spec["info"]}
    # Store Facebook's DECLARED canonical link (best-effort); reflects the video/reels merge.
    perma = get_permalink(creds, video_id)
    if perma.get("ok"):
        out["permalink_url"] = perma["url"]
    # Custom cover: separate best-effort POST AFTER finish (never fails the publish).
    if thumb_path:
        out["thumb"] = _set_video_thumbnail(creds, video_id, thumb_path)
    return out


def publish_feed_video(page, video_path: str, caption: str = "",
                       state: str = "PUBLISHED", dry_run: bool = False,
                       scheduled_publish_time: int | None = None,
                       thumb_path: str | None = None,
                       progress_key: str | None = None) -> dict:
    """Publish one video as a REGULAR Page video post (/{page_id}/videos).

    This is the non-Reels path for 16:9 / landscape / square / long videos that
    the Reels surface rejects. Uses the 3-phase RESUMABLE (chunked) upload
    (_feed_resumable_upload) so large files no longer 413 on a single multipart POST.

    - state="PUBLISHED" → published=true (goes live on the Page feed).
    - state="SCHEDULED" → published=false + scheduled_publish_time (a unix
                          timestamp; the post shows in the Page's scheduled content
                          and auto-publishes at that time).
    - state="DRAFT"     → published=false (uploaded but not shown).
    - dry_run=True stops after the spec check (no bytes uploaded, no post created).
    - thumb_path (optional): a custom cover image set BEST-EFFORT after upload via
      POST /{video_id}/thumbnails (is_preferred=true). A thumbnail failure never
      fails the publish — the outcome is reported under the returned "thumb" key.

    Never raises for the expected cases — returns {"ok": False, "reason": ...}.
    """
    state = (state or "PUBLISHED").upper()
    if state not in ("PUBLISHED", "DRAFT", "SCHEDULED"):
        return {"ok": False, "reason": f"invalid state '{state}'"}

    try:
        creds = load_creds(page)
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}

    spec = check_feed_spec(video_path)
    if not spec["ok"]:
        return {"ok": False, "reason": f"spec check failed: {spec['reason']}", "spec": spec}

    if dry_run:
        return {"ok": True, "kind": "feed", "note": "dry_run: no bytes uploaded, no post created",
                "spec": spec["info"]}

    up = _feed_resumable_upload(creds, video_path, caption, state, scheduled_publish_time,
                                progress_key=progress_key)
    if not up["ok"]:
        return up

    video_id = up["video_id"]
    out = {"ok": True, "kind": "feed", "video_id": video_id, "post_id": video_id,
           "state": state, "spec": spec["info"],
           "chunks": up.get("chunks"), "bytes": up.get("bytes")}
    # Store Facebook's DECLARED canonical link (best-effort); reflects the video/reels merge.
    perma = get_permalink(creds, video_id)
    if perma.get("ok"):
        out["permalink_url"] = perma["url"]
    # Custom cover: separate best-effort POST AFTER the video is created.
    if thumb_path and video_id:
        out["thumb"] = _set_video_thumbnail(creds, video_id, thumb_path)
    return out


def _feed_resumable_upload(creds: dict, video_path: str, caption: str, state: str,
                           scheduled_publish_time: int | None,
                           progress_key: str | None = None) -> dict:
    """3-phase resumable (chunked) upload to /{page_id}/videos — the fix for HTTP 413
    on large feed videos (a single multipart POST is rejected once the file is big).

    Authoritative phased protocol (Graph v25.0, doc-confirmed):
      start   : POST graph.facebook.com/{gv}/{page_id}/videos with upload_phase=start,
                file_size=<bytes> -> upload_session_id, video_id, start_offset,
                end_offset, and upload_domain (+ region_hint).
      transfer: loop POST upload_phase=transfer, upload_session_id, start_offset,
                video_file_chunk=<bytes[start:end]> (multipart) to the UPLOAD_DOMAIN
                host -> next start/end; done when start_offset == end_offset.
      finish  : POST upload_phase=finish, upload_session_id, description, published/
                scheduled_publish_time -> success (reuse the video_id from start).

    HOST: START always goes to graph.facebook.com; TRANSFER/FINISH go to the
    `upload_domain` Facebook returns from start (a regional upload host), falling
    back to graph.facebook.com when it's absent. The deprecated
    graph-video.facebook.com host is NOT used. Memory is bounded: we send at most
    FEED_UPLOAD_CHUNK bytes per transfer, re-reading from the returned start_offset.

    Returns {ok, video_id?, chunks?, bytes?, uploadHost?, reason?, status?}; never
    raises. Token scrubbed from every error string."""
    token = creds["page_access_token"]
    file_size = os.path.getsize(video_path)
    ver = creds.get("graph_version") or DEFAULT_GRAPH_VERSION
    page_path = f"{creds['page_id']}/videos"
    _progress_set(progress_key, phase="start", bytesSent=0, bytesTotal=file_size, pct=0.0)

    # --- START (always on graph.facebook.com) ---
    try:
        r = requests.post(_graph_url(creds, page_path), data={
            "upload_phase": "start", "file_size": str(file_size), "access_token": token,
        }, timeout=120)
    except Exception as exc:
        _progress_set(progress_key, phase="error")
        return {"ok": False, "reason": f"start phase failed: {_scrub(str(exc), token)}"}
    start_body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or not start_body.get("upload_session_id"):
        _progress_set(progress_key, phase="error")
        return {"ok": False, "status": r.status_code,
                "reason": f"start phase failed: {_graph_reason(r.status_code, start_body, r.text, token)}"}

    session_id = start_body["upload_session_id"]
    video_id = start_body.get("video_id")
    start_offset = int(start_body.get("start_offset") or 0)
    end_offset = int(start_body.get("end_offset") or 0)

    # TRANSFER/FINISH go to the returned upload_domain (regional host); fall back to
    # the regular graph host if Facebook did not supply one.
    upload_domain = (start_body.get("upload_domain") or "").strip()
    if upload_domain:
        host = upload_domain if upload_domain.startswith("http") else f"https://{upload_domain}"
        video_url = f"{host.rstrip('/')}/{ver}/{page_path}"
    else:
        video_url = _graph_url(creds, page_path)

    # --- TRANSFER loop ---
    chunks = 0
    with open(video_path, "rb") as f:
        while start_offset < end_offset:
            want = min(end_offset - start_offset, FEED_UPLOAD_CHUNK)
            f.seek(start_offset)
            chunk = f.read(want)
            if not chunk:
                _progress_set(progress_key, phase="error")
                return {"ok": False, "reason": f"transfer read empty at offset {start_offset}"}
            try:
                r = requests.post(video_url, data={
                    "upload_phase": "transfer",
                    "upload_session_id": session_id,
                    "start_offset": str(start_offset),
                    "access_token": token,
                }, files={"video_file_chunk": ("chunk", chunk, "application/octet-stream")},
                    timeout=600)
            except Exception as exc:
                _progress_set(progress_key, phase="error")
                return {"ok": False, "reason": f"transfer failed: {_scrub(str(exc), token)}"}
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code != 200 or "error" in body:
                _progress_set(progress_key, phase="error")
                return {"ok": False, "status": r.status_code,
                        "reason": f"transfer failed: {_graph_reason(r.status_code, body, r.text, token)}"}
            chunks += 1
            start_offset = int(body.get("start_offset") or end_offset)
            end_offset = int(body.get("end_offset") or end_offset)
            # The new start_offset = bytes Facebook has confirmed received.
            _progress_set(progress_key, phase="transfer", bytesSent=start_offset,
                          bytesTotal=file_size,
                          pct=round(start_offset / file_size * 100, 1) if file_size else 0.0)

    # --- FINISH ---
    data = {
        "upload_phase": "finish",
        "upload_session_id": session_id,
        "description": caption or "",
        # PUBLISHED goes live now; SCHEDULED/DRAFT are uploaded unpublished. A
        # SCHEDULED post also carries scheduled_publish_time so Facebook auto-
        # publishes it (and it appears under the Page's scheduled content).
        "published": "true" if state == "PUBLISHED" else "false",
        "access_token": token,
    }
    if state == "SCHEDULED" and scheduled_publish_time:
        data["scheduled_publish_time"] = str(int(scheduled_publish_time))
    _progress_set(progress_key, phase="finish", bytesSent=file_size, bytesTotal=file_size, pct=100.0)
    try:
        r = requests.post(video_url, data=data, timeout=180)
    except Exception as exc:
        _progress_set(progress_key, phase="error")
        return {"ok": False, "reason": f"finish phase failed: {_scrub(str(exc), token)}"}
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "error" in body:
        _progress_set(progress_key, phase="error")
        return {"ok": False, "status": r.status_code,
                "reason": f"finish phase failed: {_graph_reason(r.status_code, body, r.text, token)}"}

    # The finish reply may echo the video id; otherwise reuse the one from start.
    video_id = body.get("id") or video_id
    _progress_set(progress_key, phase="done", bytesSent=file_size, bytesTotal=file_size, pct=100.0)
    return {"ok": True, "video_id": video_id, "chunks": chunks, "bytes": file_size,
            "uploadHost": video_url.split("/")[2]}
