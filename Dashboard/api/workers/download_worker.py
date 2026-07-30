"""Source-video download worker (cf-venv) — footage mode.

Downloads the first `window` seconds of a source video (a single range ANCHORED
AT 0). This matters: YouTube's ANDROID_VR formats (the only ones available
without a PO token / JS runtime) reject ffmpeg byte-range requests that seek
into the MIDDLE of a stream (HTTP 403), but a range starting at 0 downloads
fine. Since ingest is capped to the first N seconds anyway, scenes are always
within [0, window], so one start-anchored download covers them all — individual
scene clips are then cut locally (host-side ffmpeg), no further network seeks.

Invoked as: cf-venv/python.exe download_worker.py <input.json> <output.json>

input.json:  {"link","outDir","window","maxHeight","ffmpegLocation"}
output.json: {"videoPath","videoId","durationS","width","height"}

THUMBNAIL mode (cover-from-source feature): when input.json carries
`"mode":"thumbnail"` the worker does NOT download the video — it fetches only the
source video's thumbnail image (yt-dlp writethumbnail + skip_download, converted to
JPG via the project ffmpeg) into `outDir`, and returns its local path.

input.json (thumbnail):  {"mode":"thumbnail","link","outDir","ffmpegLocation"}
output.json (thumbnail): {"thumbPath","thumbUrl","videoId"}
"""

import json
import os
import subprocess
import sys
import traceback
from pathlib import Path


def _ffprobe_bin(ffmpeg_location: str | None) -> str:
    """Resolve the ffprobe binary. yt-dlp's `ffmpegLocation` is the directory
    holding ffmpeg(.exe); ffprobe lives alongside it. Fall back to PATH."""
    if ffmpeg_location:
        cand = os.path.join(ffmpeg_location, "ffprobe.exe" if os.name == "nt" else "ffprobe")
        if os.path.isfile(cand):
            return cand
    return "ffprobe"


def _probed_duration(path: str, ffprobe_bin: str) -> float | None:
    """Container duration (seconds) per ffprobe, or None on any failure."""
    try:
        r = subprocess.run([
            ffprobe_bin, "-v", "quiet", "-print_format", "json",
            "-show_format", path,
        ], capture_output=True, text=True, timeout=30)
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return None


def _apply_yt_hardening(ydl_opts: dict) -> None:
    """Apply the shared YouTube 403 / throttling hardening (player-client ladder,
    node JS runtime for nsig, retries). Mutates `ydl_opts` in place so both the
    video download and the thumbnail fetch use the SAME nsig-free clients — a
    thumbnail request hits the same extractor as a video request, so it needs the
    same defenses. See the long comments in the video path below for the rationale."""
    player_client = (os.getenv("YTDLP_PLAYER_CLIENT") or "").strip()
    clients = ([c.strip() for c in player_client.split(",") if c.strip()]
               if player_client else ["android_vr", "ios", "web_safari", "tv"])
    ydl_opts["extractor_args"] = {"youtube": {"player_client": clients}}

    js_rt = (os.getenv("YTDLP_JS_RUNTIME") or "node").strip()
    rt_name, _, rt_path = js_rt.partition(":")
    ydl_opts["js_runtimes"] = {rt_name.lower(): {"path": rt_path or None}}

    ydl_opts.update({
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "sleep_interval_requests": 1,
    })


def _fetch_thumbnail(cfg: dict, out_dir: str, ff: str | None, out_path: str) -> None:
    """THUMBNAIL mode: fetch ONLY the source video's thumbnail (no video download).

    Uses yt-dlp's writethumbnail + skip_download, then the FFmpegThumbnailsConvertor
    postprocessor to guarantee a JPG (YouTube often serves .webp, which downstream
    OCR/Pillow may not read reliably). Writes into `out_dir` and returns the local
    JPG path. Reuses the same nsig-free player clients as the video path."""
    import yt_dlp

    os.makedirs(out_dir, exist_ok=True)
    ydl_opts = {
        "outtmpl": os.path.join(out_dir, "%(id)s_thumb.%(ext)s"),
        "skip_download": True,
        "writethumbnail": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        # Convert whatever YouTube serves (often webp) to JPG so OCR/Pillow read it.
        "postprocessors": [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
        ],
    }
    if ff:
        ydl_opts["ffmpeg_location"] = ff
    _apply_yt_hardening(ydl_opts)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(cfg["link"], download=True)

    vid = info["id"]
    # Preferred deterministic name from the outtmpl after JPG conversion.
    thumb = os.path.join(out_dir, f"{vid}_thumb.jpg")
    if not os.path.isfile(thumb):
        # Fallback: the convertor may keep the original ext, or yt-dlp may have
        # named it differently — pick the newest image file for this id.
        import glob
        cands = [p for ext in ("jpg", "jpeg", "png", "webp")
                 for p in glob.glob(os.path.join(out_dir, f"{vid}_thumb.{ext}"))]
        if not cands:
            raise FileNotFoundError(f"thumbnail not downloaded for {vid}")
        thumb = max(cands, key=os.path.getmtime)

    out = {
        "thumbPath": thumb,
        "thumbUrl": info.get("thumbnail"),
        "videoId": vid,
    }
    Path(out_path).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    in_path, out_path = sys.argv[1], sys.argv[2]
    cfg = json.loads(Path(in_path).read_text(encoding="utf-8"))

    ff = cfg.get("ffmpegLocation")
    if ff:
        os.environ["PATH"] = ff + os.pathsep + os.environ.get("PATH", "")

    # Thumbnail-only mode: fetch the source thumbnail and exit (no video download).
    if (cfg.get("mode") or "").strip().lower() == "thumbnail":
        out_dir = cfg["outDir"]
        _fetch_thumbnail(cfg, out_dir, ff, out_path)
        return

    import yt_dlp
    from yt_dlp.utils import download_range_func

    out_dir = cfg["outDir"]
    os.makedirs(out_dir, exist_ok=True)
    window = int(cfg.get("window") or 0)
    max_h = int(cfg.get("maxHeight", 720))

    # Source resolution FLOORS the final quality: a 720p landscape source upscaled
    # to 1080x1920 is inherently soft, and downscaling a 1440p source to 1080 is
    # sharper than a native 1080 source. So bias the picker toward ~1440p while
    # never lowering an explicitly-higher cap (pref_h = max(cap, 1440)). The ladder
    # falls back gracefully — best video up to the ceiling + best audio (merged to
    # mp4), then best video of ANY height + audio, then any pre-muxed stream — so a
    # source that only offers 720p still downloads instead of failing. This is
    # strictly more permissive than the old `bv*[..]+ba/b[..]` (which had no
    # unbounded fallback), so it never fails where the old selection succeeded.
    #
    # AUDIO: prefer m4a/AAC over webm/opus. Plain `ba` picks the highest-bitrate
    # audio, which on YouTube is usually the webm/opus stream (fmt 251) — and that
    # stream is sometimes served BROKEN by the CDN: the connection closes after
    # ~992 bytes and every retry reproduces it ("992 bytes read, N more expected.
    # Giving up after 10 retries"). It is deterministic per video, not a network
    # flake, and yt-dlp's `/` ladder does NOT rescue it (`/` only falls back when a
    # format is UNAVAILABLE, never when its download fails). The m4a stream (fmt
    # 140) on the same video downloads fine, so ask for it first; the plain `+ba`
    # branch stays right behind it for sources that only offer opus. Quality cost
    # is negligible (AAC ~129kbps vs Opus ~118kbps) and downstream FFmpeg/whisper
    # handle AAC natively.
    pref_h = max(max_h, 1440)
    fmt = (f"bv*[height<={pref_h}]+ba[ext=m4a]/"
           f"bv*[height<={pref_h}]+ba/"
           f"bv*+ba[ext=m4a]/"
           f"bv*+ba/"
           f"b[height<={pref_h}]/"
           f"b")

    ydl_opts = {
        "format": fmt,
        "outtmpl": os.path.join(out_dir, "%(id)s_src.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    if ff:
        ydl_opts["ffmpeg_location"] = ff

    # --- YouTube 403 / throttling hardening -------------------------------------
    # YouTube returns HTTP 403 ("Forbidden") on media URLs when yt-dlp's chosen
    # client either can't solve the nsig JS challenge (the WEB client) or is being
    # IP-throttled. Two defenses, both mirroring the CLI invocation that downloads
    # reliably:
    #
    # 1) Player-client ladder. Default order prioritises clients that BOTH avoid the
    #    web nsig challenge AND still expose high-res formats without a PO token:
    #    android_vr (verified to yield 720p here and nsig-free) and ios first, then
    #    web_safari (720p, needs nsig -> node, see below) and tv as last resorts.
    #    yt-dlp tries them in order until one yields working media URLs. NOTE: plain
    #    "android"/"tv" alone only expose ~360p here, so they must NOT lead the
    #    ladder. Override with YTDLP_PLAYER_CLIENT (comma-separated) if YT rotates.
    # 2) JS runtime for nsig (node; yt-dlp's default 'deno' is not installed here).
    # 3) Transient-403 / throttling mitigations (retries + request pacing).
    # All three live in _apply_yt_hardening so the thumbnail path shares them.
    _apply_yt_hardening(ydl_opts)

    # Probe metadata first (no download) to learn the source duration so we can
    # decide whether a trim is genuine. Setting download_ranges=(0, window) with
    # force_keyframes_at_cuts when `window` already covers the whole video
    # intermittently makes yt-dlp/ffmpeg emit an mp4 with ~2x container duration
    # (the "doubled source duration" bug). Only trim when we're actually cutting.
    with yt_dlp.YoutubeDL(ydl_opts) as probe_ydl:
        meta = probe_ydl.extract_info(cfg["link"], download=False)
    src_duration = float(meta.get("duration") or 0)

    if window > 0 and src_duration > 0 and window < src_duration:
        # Genuine trim: window covers less than the full video. Start-anchored
        # range only — see module docstring on the 403 behavior.
        ydl_opts["download_ranges"] = download_range_func(None, [(0, window)])
        ydl_opts["force_keyframes_at_cuts"] = True

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(cfg["link"], download=True)

    vid = info["id"]
    video_path = os.path.join(out_dir, f"{vid}_src.mp4")
    if not os.path.isfile(video_path):
        for d in info.get("requested_downloads", []):
            p = d.get("filepath")
            if p and os.path.isfile(p):
                video_path = p
                break
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"downloaded video not found for {vid}")

    # Guard against the "doubled source duration" bug: verify the actual
    # container duration. If the file is >30% longer than yt-dlp metadata says,
    # the metadata is unreliable — trust the probed value so ingest sizes the
    # script window to the real length.
    meta_dur = float(info.get("duration") or 0)
    eff_dur = meta_dur
    probed_dur = _probed_duration(video_path, _ffprobe_bin(ff))
    if probed_dur is not None and meta_dur > 0 and probed_dur > meta_dur * 1.3:
        print(f"[download] WARNING: file duration {probed_dur:.1f}s > "
              f"metadata {meta_dur:.1f}s — using probed value")
        eff_dur = probed_dur

    out = {
        "videoPath": video_path,
        "videoId": vid,
        "durationS": round(eff_dur, 3),
        "width": info.get("width"),
        "height": info.get("height"),
    }
    Path(out_path).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
