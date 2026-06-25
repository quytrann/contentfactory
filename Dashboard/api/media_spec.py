"""Generic, path-only pre-upload spec validator for YouTube / TikTok / Instagram.

Mirrors facebook_upload.check_reel_spec but is GENERIC: one pure function
`check_spec(video_path, rules) -> {ok, reason?, info}` driven by a per-platform
RULE set. It reuses the UTF-8-safe ffprobe logic (see _ffprobe_json) so it never
chokes on Vietnamese paths/metadata on Windows.

OWNER POLICY = LENIENT. We HARD-REJECT only things the platform genuinely will
not accept at upload time. We do NOT emit soft warnings (plain lenient, not
lenient+warn). The lenient HARD rejects are:
  - container not in the platform's accepted set,
  - video codec not accepted (h264 always; hevc allowed to avoid false rejects),
  - duration over the platform's REAL hard max (YT 12h, TikTok 60min, IG 15min)
    or <= 0 (under a sane floor),
  - missing video stream / unreadable file (ffprobe basics),
  - aspect ratio not ~9:16 — gated ONLY for REELS-type platforms (Instagram).
    YouTube and TikTok accept multiple orientations, so aspect is NOT gated there.
Audio is NOT required for YT/TikTok/IG (it is not a hard upload failure). Facebook
keeps its own stricter rules in facebook_upload.check_reel_spec (h264-only,
9:16 enforced, AAC required, 3..90s) — this module does NOT change Facebook.

Why aspect is gated only for Reels: Facebook/Instagram Reels are a portrait-only
surface; a landscape file is genuinely rejected/forced off the Reels shelf. By
contrast YouTube has one upload pipeline that accepts any orientation (a 16:9
upload is a normal long-form video, just not a "Short"), and TikTok plays
non-vertical videos with bars. So a non-9:16 file is only a HARD upload failure
for the Reels platforms.

No credentials, no secrets, no network: probing a local file only.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field


def _ffprobe_bin() -> str:
    # Resolved at call time, not import time (env may load after import).
    return os.getenv("FFPROBE_BIN", "ffprobe")


# --------------------------------------------------------------------------- #
# Per-platform rule sets. LENIENT hard limits only. Numbers/sources from
# researcher's _workspace/research_platform_upload_specs.md (June 2026).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SpecRules:
    platform: str
    # accepted containers (lowercase, no dot). mp4/mov baseline; webm where listed.
    containers: tuple[str, ...]
    # accepted video codecs (ffprobe codec_name, lowercase). h264 always; hevc to
    # avoid false rejects on a perfectly playable file. (avc1 is the same as h264.)
    vcodecs: tuple[str, ...]
    # REAL hard duration ceiling in seconds (the documented upload max, not the
    # recommended Shorts/Reels length).
    hard_max_duration_s: float
    # minimum duration; lenient = just > 0 for non-FB platforms.
    min_duration_s: float
    # gate the ~9:16 portrait aspect as a HARD reject? True only for Reels surfaces.
    enforce_aspect: bool
    # 9:16 target + tolerance, only consulted when enforce_aspect is True.
    target_ar: float = 9.0 / 16.0
    ar_tolerance: float = 0.02


# h264 family tags ffprobe may emit; treat as h264.
_H264_TAGS = ("h264", "avc1")

# YouTube: ONE upload pipeline, any orientation. Hard max = 12h (43200s) for a
# verified account. Containers/codecs are the general upload pipeline's. Aspect
# NOT gated (16:9 long-form is valid; only the Shorts shelf wants vertical).
RULES_YOUTUBE = SpecRules(
    platform="youtube",
    containers=("mp4", "mov", "webm", "avi", "wmv", "flv"),
    vcodecs=(*_H264_TAGS, "hevc", "hev1", "av1", "vp9"),
    hard_max_duration_s=43200.0,   # 12 hours (official ceiling; 256GB whichever less)
    min_duration_s=0.0,            # lenient: just > 0
    enforce_aspect=False,
)

# TikTok: 60-min web-upload ceiling. Accepts 9:16 / 1:1 / 16:9 (bars/crop), so
# aspect NOT gated. webm accepted on web.
RULES_TIKTOK = SpecRules(
    platform="tiktok",
    containers=("mp4", "mov", "webm"),
    vcodecs=(*_H264_TAGS, "hevc", "hev1"),
    hard_max_duration_s=3600.0,    # 60 min (region-dependent web ceiling)
    min_duration_s=0.0,
    enforce_aspect=False,
)

# Instagram Reels: REELS surface -> portrait gated (HARD). Hard max = the
# documented camera-roll upload ceiling. The research doc lists 20 min (1200s)
# as the confirmed camera-roll ceiling; the feature brief said "~15min (use the
# documented hard ceiling)". We pick the documented ceiling = 1200s (20 min) and
# note it; anything above that is genuinely rejected at upload. (Picking the
# larger documented ceiling keeps the policy LENIENT — fewer false rejects.)
RULES_INSTAGRAM = SpecRules(
    platform="instagram",
    containers=("mp4", "mov"),
    vcodecs=(*_H264_TAGS, "hevc", "hev1"),
    hard_max_duration_s=1200.0,    # 20 min camera-roll ceiling (documented)
    min_duration_s=0.0,
    enforce_aspect=True,           # Reels surface: must be ~9:16 portrait
)

# Registry for callers that want to look up by platform name.
RULES_BY_PLATFORM: dict[str, SpecRules] = {
    "youtube": RULES_YOUTUBE,
    "tiktok": RULES_TIKTOK,
    "instagram": RULES_INSTAGRAM,
}


# --------------------------------------------------------------------------- #
# ffprobe (UTF-8 safe — same fix as facebook_upload._ffprobe_json)
# --------------------------------------------------------------------------- #
def _ffprobe_json(video_path: str) -> dict:
    # ffprobe emits UTF-8 JSON (paths/metadata can be Vietnamese). On Windows,
    # text=True decodes with the locale codec (cp1252) and chokes on non-ASCII
    # bytes -> empty stdout -> {} -> false "no video stream". Force UTF-8.
    proc = subprocess.run(
        [_ffprobe_bin(), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", video_path],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {(proc.stderr or '').strip()[:300]}")
    return json.loads(proc.stdout or "{}")


def probe_info(video_path: str) -> dict:
    """Probe a file into the same `info` dict shape check_spec returns.

    Separated so tests can build synthetic ffprobe-info dicts and feed
    check_spec_info() directly without invoking ffprobe.
    """
    meta = _ffprobe_json(video_path)
    streams = meta.get("streams", [])
    vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
    astream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = meta.get("format", {})

    if not vstream:
        return {"has_video": False}

    width = int(vstream.get("width") or 0)
    height = int(vstream.get("height") or 0)
    vcodec = (vstream.get("codec_name") or "").lower()
    acodec = (astream.get("codec_name") or "").lower() if astream else None
    try:
        duration = float(fmt.get("duration") or vstream.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    ar = (width / height) if height else 0.0

    return {
        "has_video": True,
        "width": width, "height": height, "duration": round(duration, 2),
        "aspect_ratio": round(ar, 4), "vcodec": vcodec, "acodec": acodec,
    }


# --------------------------------------------------------------------------- #
# The generic validator
# --------------------------------------------------------------------------- #
def check_spec_info(info: dict, container: str, rules: SpecRules) -> dict:
    """Apply `rules` to an already-probed `info` dict + `container` (no dot).

    Pure, no ffprobe — used both by check_spec() and by unit tests that pass
    synthetic info dicts. Returns {ok, reason?, info}. `info` is echoed back with
    `container` merged in.
    """
    out_info = {**info, "container": container}

    # Container (lenient: must be in the platform's accepted set).
    if container not in rules.containers:
        return {"ok": False,
                "reason": f"container '.{container}' not accepted by {rules.platform} "
                          f"(allowed: {', '.join('.' + c for c in rules.containers)})",
                "info": out_info}

    if not info.get("has_video"):
        return {"ok": False, "reason": "no video stream found", "info": out_info}

    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    vcodec = (info.get("vcodec") or "").lower()
    duration = float(info.get("duration") or 0.0)
    ar = (width / height) if height else 0.0

    # Video codec (lenient: h264 always; hevc allowed; plus platform extras).
    if vcodec not in rules.vcodecs:
        return {"ok": False,
                "reason": f"video codec '{vcodec}' not accepted by {rules.platform} "
                          f"(allowed: {', '.join(rules.vcodecs)})",
                "info": out_info}

    # Duration: under a sane floor (lenient: > 0) or over the REAL hard max.
    if duration <= rules.min_duration_s:
        return {"ok": False,
                "reason": f"duration {duration:.2f}s is not greater than min "
                          f"{rules.min_duration_s:.2f}s",
                "info": out_info}
    if duration > rules.hard_max_duration_s:
        return {"ok": False,
                "reason": f"duration {duration:.2f}s exceeds {rules.platform} hard max "
                          f"{rules.hard_max_duration_s:.0f}s",
                "info": out_info}

    # Aspect ratio — HARD reject ONLY for Reels-type platforms (enforce_aspect).
    if rules.enforce_aspect:
        if width >= height:
            return {"ok": False,
                    "reason": f"not portrait: {width}x{height} (Reels need ~9:16)",
                    "info": out_info}
        if abs(ar - rules.target_ar) > rules.ar_tolerance:
            return {"ok": False,
                    "reason": f"aspect ratio {ar:.4f} not ~9:16 "
                              f"({rules.target_ar:.4f} ±{rules.ar_tolerance}) — required for Reels",
                    "info": out_info}

    # NOTE: audio is intentionally NOT required for YT/TikTok/IG (lenient).
    return {"ok": True, "info": out_info}


def check_spec(video_path: str, rules: SpecRules) -> dict:
    """Validate a file against `rules` WITHOUT uploading. Path-only; no creds.

    Returns {"ok": True, "info": {...}} when acceptable, otherwise
    {"ok": False, "reason": "<why>", "info": {...}}. Same shape as
    facebook_upload.check_reel_spec so publish_core can treat both identically.
    """
    if not os.path.isfile(video_path):
        return {"ok": False, "reason": f"video file not found: {video_path}", "info": {}}

    container = os.path.splitext(video_path)[1].lower().lstrip(".")

    # Probe first only if the container is acceptable? No — probe always so the
    # info dict is populated even on a container reject would be nice, but a bad
    # container means we should not even spend a probe. Mirror FB: container check
    # is cheap and first. We still probe to fill info for accepted containers.
    if container not in rules.containers:
        return {"ok": False,
                "reason": f"container '.{container}' not accepted by {rules.platform} "
                          f"(allowed: {', '.join('.' + c for c in rules.containers)})",
                "info": {"container": container}}

    try:
        info = probe_info(video_path)
    except Exception as exc:
        return {"ok": False, "reason": str(exc), "info": {"container": container}}

    return check_spec_info(info, container, rules)
