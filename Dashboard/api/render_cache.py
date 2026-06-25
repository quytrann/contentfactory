"""Render-content cache — snapshot a finished video's REUSABLE content so it can
be re-assembled later at a DIFFERENT aspect ratio (16:9 <-> 9:16) WITHOUT re-running
ingest / Claude script / TTS / SDXL. Only the FFmpeg ASSEMBLE step is re-run from
the cached pieces.

Why this exists: after a job succeeds, `_cleanup_job_intermediates` (runner.py)
DELETES the per-scene intermediates (cut clips, SDXL images, scene wavs) to free
disk. So before cleanup runs we COPY the reusable content into a durable cache that
survives cleanup:

    <CONTENT_OUTPUT_ROOT>/_cache/renders/<video_id>/
        manifest.json          # everything needed to re-assemble at a new aspect
        scene001.<ext>         # cached VISUAL  (cut clip for footage/stickman, image for image mode)
        scene001.wav           # cached AUDIO   (the scene's Vietnamese VO)
        ...

The cache lives under _cache, which `_cleanup_job_intermediates` NEVER touches
(it's in _CLEANUP_NEVER_DIRS), so a normal post-job cleanup leaves it intact.

manifest.json shape (stable contract consumed by the runner clone path + the
clone endpoint):
    {
      "videoId": <int>,
      "page": "<page name>",
      "renderMode": "footage" | "stickman" | "image",
      "visualKind": "clip" | "image",
      "title": "<video title>",
      "aspect": "16:9" | "9:16" | ...,        # the SOURCE aspect (for the 409 same-aspect guard)
      "width": <int>, "height": <int>,        # source frame size
      "sourceName": <str|null>, "sourceLink": <str|null>,
      "sourceLogo": <str|null>, "sourceHandle": <str|null>,
      "addCredit": <bool>,
      "bgmPath": <str|null>, "bgmVolume": <float|null>,
      "srcAudioVolume": <float>,
      "scenes": [
        {"scene": <int>, "narration": "<text>", "caption": "<text>",
         "durationS": <float|null>, "visual": "scene001.mp4", "audio": "scene001.wav"}
      ]
    }

Path-guard + env conventions mirror cache_util.py / runner.py:
  - RENDER_CACHE env (default ON). 0/off/false/no => snapshot is a no-op AND the
    clone endpoint reports "not available".
  - Every path is verified inside CONTENT_OUTPUT_ROOT before any read/write.
  - Copies are atomic-ish (copy to .part, os.replace) so a reader never sees a
    half-copied file. Best-effort: a snapshot failure must NEVER fail the job.

Greppable log on success:
    [rendercache] video <id>: stored N scenes -> _cache/renders/<id>/
"""

import json
import os
import shutil


_ROOT = os.getenv("CONTENT_OUTPUT_ROOT", r"E:\ContentFactory")

MANIFEST_NAME = "manifest.json"


def _flag() -> str:
    return (os.getenv("RENDER_CACHE", "1") or "").strip().lower()


def render_cache_enabled() -> bool:
    """Snapshot + clone available? Disabled by RENDER_CACHE in {0,off,false,no}."""
    return _flag() not in ("0", "off", "false", "no")


def renders_root() -> str:
    return os.path.join(_ROOT, "_cache", "renders")


def render_dir(video_id: int) -> str:
    return os.path.join(renders_root(), str(int(video_id)))


def manifest_path(video_id: int) -> str:
    return _guard(os.path.join(render_dir(video_id), MANIFEST_NAME))


# ---- path-guard (same contract as cache_util._guard) ------------------------

def _guard(path: str) -> str:
    """Refuse any path that resolves outside CONTENT_OUTPUT_ROOT."""
    root = os.path.realpath(_ROOT)
    full = os.path.realpath(path)
    if full != root and not full.startswith(root + os.sep):
        raise ValueError(f"render cache path escapes content root: {path}")
    return full


def _valid_file(path: str | None) -> bool:
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _atomic_copy(src: str, dest: str) -> bool:
    """Copy src -> dest atomically (via .part + os.replace). Returns True on success.
    No-op success if src is already dest. Best-effort: returns False on any error."""
    try:
        dest = _guard(dest)
    except ValueError as e:
        print(f"[rendercache] refused copy (outside root): {e}")
        return False
    if not _valid_file(src):
        return False
    if os.path.realpath(src) == dest:
        return True
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
        return True
    except OSError as e:
        print(f"[rendercache] copy failed {src} -> {dest}: {e}")
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


# ---- snapshot (write) -------------------------------------------------------

def store_render(
    video_id: int,
    *,
    page: str,
    render_mode: str,
    visual_kind: str,
    title: str | None,
    aspect: str | None,
    width: int | None,
    height: int | None,
    source_name: str | None,
    source_link: str | None,
    source_logo: str | None,
    source_handle: str | None,
    add_credit: bool,
    bgm_path: str | None,
    bgm_volume: float | None,
    src_audio_volume: float,
    scenes: list[dict],
    visuals: dict,
    audio: dict,
) -> str | None:
    """Snapshot a finished video's reusable content into _cache/renders/<video_id>/.

    `scenes`  : the script scene list (each has 'scene', 'narration', optional 'caption').
    `visuals` : {scene_index: <visual file path>} — cut clip (footage/stickman) or SDXL image.
    `audio`   : {scene_index: {"audioPath": <wav>, "durationS": <float>}} (the tts results map).

    Best-effort and env-guarded. Returns the cache dir on success, else None.
    A scene whose visual or audio file is missing/invalid is SKIPPED (logged) rather
    than crashing — but if ZERO scenes survive, the snapshot is abandoned (no manifest)
    so a later clone correctly reports "not available" instead of producing an empty video.
    """
    if not render_cache_enabled():
        print(f"[rendercache] video {video_id}: disabled (RENDER_CACHE off), not stored")
        return None
    try:
        cdir = _guard(render_dir(video_id))
    except ValueError as e:
        print(f"[rendercache] video {video_id}: refused ({e})")
        return None

    try:
        os.makedirs(cdir, exist_ok=True)
        manifest_scenes: list[dict] = []
        for s in scenes:
            n = s["scene"]
            vsrc = visuals.get(n)
            arec = audio.get(n) or {}
            asrc = arec.get("audioPath") if isinstance(arec, dict) else None
            if not _valid_file(vsrc) or not _valid_file(asrc):
                print(f"[rendercache] video {video_id}: scene {n} skipped "
                      f"(visual or audio missing: visual={vsrc}, audio={asrc})")
                continue
            vext = (os.path.splitext(vsrc)[1] or ".mp4").lower()
            aext = (os.path.splitext(asrc)[1] or ".wav").lower()
            vname = f"scene{int(n):03d}{vext}"
            aname = f"scene{int(n):03d}{aext}"
            if not _atomic_copy(vsrc, os.path.join(cdir, vname)):
                print(f"[rendercache] video {video_id}: scene {n} visual copy failed, skipped")
                continue
            if not _atomic_copy(asrc, os.path.join(cdir, aname)):
                print(f"[rendercache] video {video_id}: scene {n} audio copy failed, skipped")
                continue
            manifest_scenes.append({
                "scene": int(n),
                "narration": s.get("narration"),
                "caption": s.get("caption") if s.get("caption") is not None else s.get("narration"),
                "durationS": (arec.get("durationS") if isinstance(arec, dict) else None),
                "visual": vname,
                "audio": aname,
            })

        if not manifest_scenes:
            print(f"[rendercache] video {video_id}: no usable scenes, snapshot abandoned")
            return None

        manifest = {
            "videoId": int(video_id),
            "page": page,
            "renderMode": render_mode,
            "visualKind": visual_kind,
            "title": title,
            "aspect": aspect,
            "width": width,
            "height": height,
            "sourceName": source_name,
            "sourceLink": source_link,
            "sourceLogo": source_logo,
            "sourceHandle": source_handle,
            "addCredit": bool(add_credit),
            "bgmPath": bgm_path,
            "bgmVolume": bgm_volume,
            "srcAudioVolume": float(src_audio_volume or 0.0),
            "scenes": manifest_scenes,
        }
        mp = manifest_path(video_id)
        tmp = mp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        os.replace(tmp, mp)
        print(f"[rendercache] video {video_id}: stored {len(manifest_scenes)} scenes "
              f"-> _cache/renders/{video_id}/")
        return cdir
    except Exception as e:  # best-effort: snapshot must never fail the job
        print(f"[rendercache] video {video_id}: snapshot error (ignored): {e}")
        return None


# ---- load (read) ------------------------------------------------------------

def has_cached_render(video_id: int) -> bool:
    """True iff a valid manifest exists for this video (clone is possible)."""
    if not render_cache_enabled():
        return False
    try:
        return _valid_file(manifest_path(video_id))
    except ValueError:
        return False


def load_manifest(video_id: int) -> dict | None:
    """Load + validate the manifest for `video_id`, resolving each scene's visual /
    audio to an ABSOLUTE cached path. Returns None on miss / corruption / shape
    mismatch / any scene file gone missing (so the caller reports "not available"
    rather than feeding a broken assemble).
    """
    if not render_cache_enabled():
        return None
    try:
        mp = manifest_path(video_id)
    except ValueError:
        return None
    if not _valid_file(mp):
        return None
    try:
        with open(mp, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("scenes"), list) or not data["scenes"]:
        return None

    cdir = render_dir(video_id)
    for sc in data["scenes"]:
        if not isinstance(sc, dict) or "visual" not in sc or "audio" not in sc:
            return None
        vpath = os.path.join(cdir, sc["visual"])
        apath = os.path.join(cdir, sc["audio"])
        try:
            vpath = _guard(vpath)
            apath = _guard(apath)
        except ValueError:
            return None
        if not _valid_file(vpath) or not _valid_file(apath):
            print(f"[rendercache] video {video_id}: scene {sc.get('scene')} file missing "
                  f"-> manifest unusable")
            return None
        sc["visualPath"] = vpath
        sc["audioPath"] = apath
    return data
