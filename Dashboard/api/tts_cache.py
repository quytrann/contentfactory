"""Per-scene TTS cache — reuse a synthesized wav across jobs whenever the exact
same narration is voiced with the exact same engine/voice/emotion/tuning.

Completely JOB-INDEPENDENT (like cache_util.py for ingest): the key is a content
hash of WHAT is spoken + HOW it is voiced, so two different jobs (or a re-run of
the same job) that ask for an identical line share one wav and skip TTS entirely.

Cache key (sha256 hex) over, joined by "|":
    narration | engine | voice_id | emotion | str(temperature) | str(rep_penalty)
where:
  - voice_id : for a CLONE (ref_audio_path given) -> sha256 of the REFERENCE FILE
               CONTENT (stable even if the path/name changes; busts when the clip
               is re-recorded). For a preset voice -> `voice or "default"`.
  - temperature / rep_penalty : the literal str() of the value, so a None default
               and an explicit number key to different entries (str(None) == "None").

Cache lives OUTSIDE the repo under CONTENT_OUTPUT_ROOT (E:\ContentFactory):
    <root>/_cache/tts/<key[:2]>/<key>.wav   # one wav per (content, voicing) combo
The two-char prefix subdir keeps any single directory from holding tens of
thousands of files (filesystem-friendly, same pattern many caches use).

Honesty / caveats:
  - STALE: a model/checkpoint change is NOT in the key, so a cached wav was made by
    whatever engine build existed when it was first synthesized. Accepted; set
    `TTS_CACHE=0` to disable reuse (still WRITES on a miss so the cache warms), or
    `TTS_CACHE=off` to disable BOTH reads and writes (same convention as INGEST_CACHE).
  - CORRUPTION: a missing / zero-byte / unreadable cached wav is a MISS, re-synth'd.
  - PATH-GUARD: every cache path is verified to live inside CONTENT_OUTPUT_ROOT
    before any read/write (local/free constraint — never write into the repo).
  - NEVER FATAL: every public op is best-effort; the caller wraps these in try/except
    and falls through to the worker on any error.

HIT/MISS logging is explicit and greppable:
    [tts_cache] HIT <key[:8]> scene <n>
    [tts_cache] MISS <key[:8]> scene <n>
"""

import hashlib
import os
import shutil
import threading
import time


# Resolved from the same env var generate.py / cache_util.py use (default E:\ContentFactory).
_ROOT = os.getenv("CONTENT_OUTPUT_ROOT", r"E:\ContentFactory")


def _cache_root() -> str:
    return os.path.join(_ROOT, "_cache")


def tts_dir() -> str:
    return os.path.join(_cache_root(), "tts")


# ---- cache enable/disable ---------------------------------------------------

def _flag() -> str:
    """TTS_CACHE env, lowercased. Empty => default 'on'."""
    return (os.getenv("TTS_CACHE", "1") or "").strip().lower()


def cache_reads_enabled() -> bool:
    """Reuse (read) the cache? Disabled by TTS_CACHE in {0,off,false,no}."""
    return _flag() not in ("0", "off", "false", "no")


def cache_writes_enabled() -> bool:
    """Populate (write) the cache on a fresh synth? Always on EXCEPT TTS_CACHE=off,
    so that even with reads disabled we keep warming the cache (toggling the flag
    back on later then hits). 'off' is the hard kill-switch (no read, no write)."""
    return _flag() != "off"


# ---- path-guard -------------------------------------------------------------

def _guard(path: str) -> str:
    """Refuse any path that resolves outside CONTENT_OUTPUT_ROOT (local/free
    constraint — never write into the repo or elsewhere)."""
    root = os.path.realpath(_ROOT)
    full = os.path.realpath(path)
    if full != root and not full.startswith(root + os.sep):
        raise ValueError(f"tts cache path escapes content root: {path}")
    return full


def _valid_file(path: str | None) -> bool:
    """A cached wav is usable iff it exists and is non-empty. Missing / zero-byte
    / unreadable => treated as a MISS (corruption handling)."""
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


# ---- cache key --------------------------------------------------------------

def _voice_id(voice: str | None, ref_audio_path: str | None) -> str:
    """The voice component of the key.

    For a CLONE (ref_audio_path given) the id is the sha256 of the reference file's
    CONTENT — stable across path/name changes, and it busts when the clip is
    re-recorded under the same name. If the ref file is missing/unreadable we fall
    back to hashing the path STRING so a key is still produced (never crash).
    For a preset voice the id is `voice or "default"`."""
    if ref_audio_path:
        try:
            with open(ref_audio_path, "rb") as f:
                return "clone_" + hashlib.sha256(f.read()).hexdigest()
        except OSError:
            # File gone/unreadable — degrade to the path string so we still key
            # deterministically (caller never crashes on a cache op).
            return "clonepath_" + hashlib.sha256(
                ref_audio_path.encode("utf-8")
            ).hexdigest()
    return voice or "default"


def tts_cache_key(
    narration: str,
    engine: str,
    voice: str | None,
    ref_audio_path: str | None,
    emotion: str,
    temperature: float | None,
    rep_penalty: float | None,
) -> str:
    """Stable sha256 hex key over (narration, engine, voice_id, emotion,
    temperature, rep_penalty). See module docstring for the exact recipe."""
    vid = _voice_id(voice, ref_audio_path)
    raw = "|".join(
        [
            narration or "",
            engine or "",
            vid,
            emotion or "",
            str(temperature),
            str(rep_penalty),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_path(key: str) -> str:
    """The on-disk path for a key: <tts>/<key[:2]>/<key>.wav, path-guarded."""
    return _guard(os.path.join(tts_dir(), key[:2], f"{key}.wav"))


# ---- read / write -----------------------------------------------------------

def find_cached_tts(key: str) -> str | None:
    """Return the path of a VALID cached wav for `key`, or None (MISS). Reads
    disabled (TTS_CACHE in {0,off,false,no}) => always None. Never raises."""
    if not cache_reads_enabled():
        return None
    try:
        p = _cache_path(key)
    except ValueError:
        return None
    if not _valid_file(p):
        return None
    # TTL last-used tracking: bump mtime to "now" on every HIT so the eviction
    # sweep (evict_stale_tts) measures time-since-last-USE, not time-since-synth.
    # Without this, a wav reused daily but first synthesized >24h ago would be
    # wrongly evicted. Best-effort: an mtime-bump failure must NOT turn a HIT
    # into a miss — we still return the valid path.
    try:
        os.utime(p, None)
    except OSError:
        pass
    return p


def delete_tts(key: str) -> bool:
    """Evict the cached wav for `key` (best-effort, NEVER raises).

    Used by the bypass-cache (force-fresh) path to drop a stale entry BEFORE
    re-synthesizing, so the slot is clean and any corrupt/zero-byte cached wav
    is removed. NOT gated on cache_writes_enabled() — eviction must work even
    when writes are toggled off (it removes, it doesn't populate). No-ops
    cleanly if the path can't be resolved (guard ValueError) or nothing exists.
    Returns True iff something was actually deleted, else False."""
    try:
        path = _cache_path(key)
    except ValueError:
        return False
    deleted = False
    try:
        if os.path.isfile(path):
            os.remove(path)
            deleted = True
        part = path + ".part"
        if os.path.isfile(part):
            os.remove(part)
            deleted = True
    except OSError:
        return deleted
    if deleted:
        print(f"[tts_cache] DELETE {key[:8]}")
    return deleted


def store_tts(key: str, wav_path: str) -> str | None:
    """Copy a freshly-synthesized wav into the cache as <key[:2]>/<key>.wav.

    Best-effort: a copy failure must NOT fail the job (we still have the live file).
    Returns the cached path on success, else None. No-op when writes are disabled
    or the source wav is invalid. Atomic (.part + os.replace) so a reader never
    sees a half-copied file. Never raises."""
    if not cache_writes_enabled() or not _valid_file(wav_path):
        return None
    try:
        dest = _cache_path(key)
    except ValueError:
        return None
    if os.path.realpath(wav_path) == dest:
        return dest  # already the cached file — nothing to copy
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        shutil.copyfile(wav_path, tmp)
        os.replace(tmp, dest)  # atomic
        return dest
    except OSError as e:
        print(f"[tts_cache] store failed for {key[:8]}: {e}")
        try:
            if os.path.isfile(dest + ".part"):
                os.remove(dest + ".part")
        except OSError:
            pass
        return None


# ---- TTL eviction -----------------------------------------------------------

def evict_stale_tts(max_age_seconds: int = 86400) -> int:
    """Remove cached wavs (and stray .part files) not used within max_age_seconds.

    "Last used" is the file MTIME — find_cached_tts() bumps mtime on every HIT,
    so this measures time-since-last-use, not time-since-first-synth (Windows
    atime is unreliable, hence mtime). Default 86400s = 24h.

    BEST-EFFORT / NEVER RAISES: the whole scan and each per-file op are wrapped
    in try/except so one bad file can't abort the sweep and the sweep can't crash
    the caller. Returns the number of wav files deleted (0 if the dir doesn't
    exist yet). Empty <key[:2]> shard subdirs are pruned afterwards (best-effort).
    """
    root = tts_dir()
    if not os.path.isdir(root):
        return 0
    now = time.time()
    deleted = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                fpath = os.path.join(dirpath, name)
                try:
                    if name.endswith(".wav.part"):
                        # Stray partial copy (interrupted store) — always sweep.
                        _guard(fpath)
                        os.remove(fpath)
                        continue
                    if not name.endswith(".wav"):
                        continue
                    if now - os.path.getmtime(fpath) > max_age_seconds:
                        _guard(fpath)
                        os.remove(fpath)
                        deleted += 1
                except (OSError, ValueError):
                    # Bad file / path escapes guard — skip, keep sweeping.
                    continue
    except OSError:
        # Walk itself blew up — return whatever we managed to delete.
        pass
    # Prune now-empty <key[:2]> shard subdirs so the cache dir doesn't accumulate
    # empty shards. Best-effort; ignore any error (e.g. non-empty / locked).
    try:
        for entry in os.listdir(root):
            sub = os.path.join(root, entry)
            if os.path.isdir(sub) and not os.listdir(sub):
                try:
                    os.rmdir(sub)
                except OSError:
                    pass
    except OSError:
        pass
    if deleted:
        hours = max_age_seconds / 3600
        plural = "entry" if deleted == 1 else "entries"
        print(f"[tts_cache] evicted {deleted} stale {plural} (>{hours:g}h)")
    return deleted


def start_eviction_async(max_age_seconds: int = 86400) -> None:
    """Spawn a daemon thread running evict_stale_tts() so the sweep never blocks
    the caller (e.g. API startup). Whole thing wrapped in try/except so spawning
    a thread can never raise into the caller (best-effort)."""
    try:
        threading.Thread(
            target=evict_stale_tts,
            args=(max_age_seconds,),
            daemon=True,
        ).start()
    except Exception as e:  # noqa: BLE001 — spawning must never break the caller
        print(f"[tts_cache] eviction thread spawn failed: {e}")
