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


def main() -> None:
    in_path, out_path = sys.argv[1], sys.argv[2]
    cfg = json.loads(Path(in_path).read_text(encoding="utf-8"))

    ff = cfg.get("ffmpegLocation")
    if ff:
        os.environ["PATH"] = ff + os.pathsep + os.environ.get("PATH", "")

    import yt_dlp
    from yt_dlp.utils import download_range_func

    out_dir = cfg["outDir"]
    os.makedirs(out_dir, exist_ok=True)
    window = int(cfg.get("window") or 0)
    max_h = int(cfg.get("maxHeight", 720))

    ydl_opts = {
        "format": f"bv*[height<={max_h}]+ba/b[height<={max_h}]",
        "outtmpl": os.path.join(out_dir, "%(id)s_src.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    if ff:
        ydl_opts["ffmpeg_location"] = ff

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
