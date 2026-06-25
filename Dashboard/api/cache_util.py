"""Cross-job INGEST cache — reuse the downloaded source video and the transcript
across jobs so re-processing the same source URL does not re-download or
re-transcribe.

Keyed by a STABLE id derived from the source link:
  - YouTube  -> the canonical 11-char YouTube video id (watch?v=, youtu.be/,
                shorts/, embed/, /v/, /live/). Same video, any URL form -> same id.
  - other    -> sha1 of the NORMALIZED url, prefixed "u_" so it can't collide with
                an 11-char YouTube id.

Cache lives OUTSIDE the repo under CONTENT_OUTPUT_ROOT (E:\ContentFactory):
    <root>/_cache/sources/<id>.<ext>     # the downloaded source video (TASK 2)
    <root>/_cache/transcripts/<id>.json  # the whisper transcript "script" (TASK 3)

Honesty / caveats handled here:
  - STALE: if the source changes, the cache serves an old copy. Accepted by the
    owner, but `INGEST_CACHE=0` (env) disables ALL reuse (still WRITES fresh on a
    miss so subsequent runs are warm — set INGEST_CACHE=off to also skip writes).
  - CORRUPTION: a missing / zero-byte / unreadable cached file is treated as a
    MISS and re-fetched — never crashes the pipeline.
  - PATH-GUARD: every cache path is verified to live inside CONTENT_OUTPUT_ROOT
    before any read/write; an out-of-root path is refused (local/free constraint).

HIT/MISS logging is explicit and greppable:
    [cache] source HIT <id> -> <path>
    [cache] source MISS <id> — will download
    [cache] transcript HIT <id> -> <path>
    [cache] transcript MISS <id> — transcribing
"""

import hashlib
import json
import os
import re
import shutil
import urllib.parse


# Resolved from the same env var generate.py uses (default E:\ContentFactory).
_ROOT = os.getenv("CONTENT_OUTPUT_ROOT", r"E:\ContentFactory")


def _cache_root() -> str:
    return os.path.join(_ROOT, "_cache")


def sources_dir() -> str:
    return os.path.join(_cache_root(), "sources")


def transcripts_dir() -> str:
    return os.path.join(_cache_root(), "transcripts")


# ---- cache enable/disable ---------------------------------------------------

def _flag() -> str:
    """INGEST_CACHE env, lowercased. Empty => default 'on'."""
    return (os.getenv("INGEST_CACHE", "1") or "").strip().lower()


def cache_reads_enabled() -> bool:
    """Reuse (read) the cache? Disabled by INGEST_CACHE in {0,off,false,no}."""
    return _flag() not in ("0", "off", "false", "no")


def cache_writes_enabled() -> bool:
    """Populate (write) the cache on a fresh fetch? Always on EXCEPT INGEST_CACHE=off,
    so that even with reads disabled we keep warming the cache (toggling the flag
    back on later then hits). 'off' is the hard kill-switch (no read, no write)."""
    return _flag() != "off"


# ---- stable id from a source link -------------------------------------------

# Canonical YouTube ids are 11 chars of [A-Za-z0-9_-].
_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Path-form ids: /shorts/<id>, /embed/<id>, /v/<id>, /live/<id>, youtu.be/<id>.
_YT_PATH_RE = re.compile(r"/(?:shorts|embed|v|live)/([A-Za-z0-9_-]{11})")


def youtube_video_id(link: str) -> str | None:
    """Return the canonical YouTube video id for a link, or None if not YouTube.

    Handles: watch?v=ID, youtu.be/ID, /shorts/ID, /embed/ID, /v/ID, /live/ID,
    with or without scheme, extra query params, trailing path, etc."""
    if not link:
        return None
    s = link.strip()
    if "://" not in s:
        s = "https://" + s  # urlsplit needs a scheme to populate netloc
    try:
        u = urllib.parse.urlsplit(s)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    is_yt = host in ("youtube.com", "m.youtube.com", "music.youtube.com",
                     "youtu.be", "youtube-nocookie.com")
    if not is_yt:
        return None
    # youtu.be/<id>  -> id is the first path segment
    if host == "youtu.be":
        seg = u.path.lstrip("/").split("/", 1)[0]
        return seg if _YT_ID_RE.match(seg) else None
    # watch?v=<id>
    qs = urllib.parse.parse_qs(u.query)
    v = (qs.get("v") or [None])[0]
    if v and _YT_ID_RE.match(v):
        return v
    # /shorts/<id>, /embed/<id>, /v/<id>, /live/<id>
    m = _YT_PATH_RE.search(u.path)
    if m:
        return m.group(1)
    return None


def _normalize_url(link: str) -> str:
    """Normalize a non-YouTube URL for stable hashing: lowercase scheme+host,
    strip a trailing slash and a default port, drop the fragment. Query is kept
    (it can be load-bearing for non-YouTube sources)."""
    s = link.strip()
    if "://" not in s:
        s = "https://" + s
    try:
        u = urllib.parse.urlsplit(s)
    except ValueError:
        return s.lower()
    host = (u.hostname or "").lower()
    port = "" if (u.port in (None, 80, 443)) else f":{u.port}"
    path = u.path.rstrip("/")
    return urllib.parse.urlunsplit((u.scheme.lower(), host + port, path, u.query, ""))


def source_id(link: str) -> str:
    """STABLE cache id for a source link. YouTube -> the video id; otherwise a
    'u_'-prefixed sha1 of the normalized URL (so it can never look like an 11-char
    YouTube id -> no collision between the two id spaces)."""
    yid = youtube_video_id(link)
    if yid:
        return yid
    h = hashlib.sha1(_normalize_url(link).encode("utf-8")).hexdigest()
    return "u_" + h[:24]


# ---- path-guard -------------------------------------------------------------

def _guard(path: str) -> str:
    """Refuse any path that resolves outside CONTENT_OUTPUT_ROOT (local/free
    constraint — never write into the repo or elsewhere)."""
    root = os.path.realpath(_ROOT)
    full = os.path.realpath(path)
    if full != root and not full.startswith(root + os.sep):
        raise ValueError(f"cache path escapes content root: {path}")
    return full


def _valid_file(path: str | None) -> bool:
    """A cached file is usable iff it exists and is non-empty. Missing / zero-byte
    / unreadable => treated as a MISS (corruption handling)."""
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


# ---- TASK 2: downloaded source video ----------------------------------------

def find_cached_source(sid: str) -> str | None:
    """Return the path of a VALID cached source video for `sid`, or None (MISS).

    Looks for <sources>/<sid>.<ext> across common video extensions; a present but
    zero-byte/unreadable file is ignored (treated as a miss)."""
    d = sources_dir()
    for ext in ("mp4", "mkv", "webm", "mov", "m4v"):
        cand = os.path.join(d, f"{sid}.{ext}")
        try:
            cand = _guard(cand)
        except ValueError:
            continue
        if _valid_file(cand):
            return cand
    return None


def store_source(sid: str, src_path: str) -> str | None:
    """Copy a freshly-downloaded source video into the cache as <sid>.<ext>.

    Best-effort: a copy failure must NOT fail the job (we still have the live
    file). Returns the cached path on success, else None. No-op if writes are
    disabled or the source file is invalid."""
    if not cache_writes_enabled() or not _valid_file(src_path):
        return None
    ext = (os.path.splitext(src_path)[1] or ".mp4").lstrip(".").lower() or "mp4"
    d = sources_dir()
    os.makedirs(d, exist_ok=True)
    dest = _guard(os.path.join(d, f"{sid}.{ext}"))
    if os.path.realpath(src_path) == dest:
        return dest  # already the cached file (e.g. reused) — nothing to copy
    try:
        tmp = dest + ".part"
        shutil.copyfile(src_path, tmp)
        os.replace(tmp, dest)  # atomic — a reader never sees a half-copied file
        return dest
    except OSError as e:
        print(f"[cache] source store failed for {sid}: {e}")
        try:
            if os.path.isfile(dest + ".part"):
                os.remove(dest + ".part")
        except OSError:
            pass
        return None


# ---- TASK 3: transcript ("bóc lời") -----------------------------------------

def transcript_path(sid: str) -> str:
    return _guard(os.path.join(transcripts_dir(), f"{sid}.json"))


def load_transcript(sid: str) -> dict | None:
    """Load a cached transcript for `sid`, or None (MISS). A missing / empty /
    unparseable / shape-invalid file is treated as a miss (corruption handling).

    The shape MUST match the ingest worker's output so downstream reuse is
    transparent: we require at least 'transcript' and a list 'segments'."""
    try:
        p = transcript_path(sid)
    except ValueError:
        return None
    if not _valid_file(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if "transcript" not in data or not isinstance(data.get("segments"), list):
        return None  # not the expected shape -> miss, re-transcribe
    return data


def store_transcript(sid: str, data: dict) -> str | None:
    """Persist a transcript dict (the ingest worker's output) for `sid`. Best-effort;
    no-op when writes are disabled or the payload is the wrong shape. Returns the
    cached path on success, else None."""
    if not cache_writes_enabled():
        return None
    if not isinstance(data, dict) or "transcript" not in data \
            or not isinstance(data.get("segments"), list):
        return None
    d = transcripts_dir()
    os.makedirs(d, exist_ok=True)
    p = transcript_path(sid)
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)  # atomic
        return p
    except OSError as e:
        print(f"[cache] transcript store failed for {sid}: {e}")
        return None
