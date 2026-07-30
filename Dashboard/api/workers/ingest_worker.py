"""Ingest worker — download a source video's audio + transcribe it (cf-venv).

For translate/reup pages (e.g. CTG Gaming) the pipeline starts from someone
else's video: we pull the audio with yt-dlp, then run faster-whisper to get the
SOURCE transcript that the script-gen stage rewrites (Commentary/Recap/...).

Invoked by the FastAPI host as:
    cf-venv/python.exe ingest_worker.py <input.json> <output.json>

input.json:  {"link":"https://...", "outDir":"...", "model":"medium",
              "device":"cpu", "compute":"int8", "language":null,
              "ffmpegLocation":"...\\bin", "sampleRate":16000}
output.json: {"sourceUrl","videoId","title","uploader","durationS","thumbnail",
              "audioPath","language","transcript","segments":[{start,end,text}]}

`language` null → autodetect (source is usually not Vietnamese). The audio is
extracted to 16 kHz mono wav (small, whisper-native) — no need to keep video.
"""

import json
import os
import sys
import traceback
from pathlib import Path


def _apply_yt_hardening(ydl_opts: dict) -> None:
    """Apply the shared YouTube bot-check / throttling hardening (player-client
    ladder, node JS runtime for nsig, retries). Mutates `ydl_opts` in place.

    Needed even for METADATA-ONLY fetches: yt-dlp's default "web" client
    intermittently gets served "Sign in to confirm you're not a bot" on a plain
    extract_info(download=False). The android_vr / ios clients don't trigger that
    challenge and need no cookies or login.

    Kept as a local copy rather than a shared import on purpose: every file in
    this directory is a STANDALONE script invoked as
    `cf-venv/python.exe <worker>.py in.json out.json` with stdlib-only top-level
    imports and no cross-worker dependencies. Mirrors _apply_yt_hardening in
    download_worker.py — keep the three copies in sync."""
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


def _enable_cuda_dlls() -> None:
    """Put torch's bundled cuDNN9 / cuBLAS12 DLLs on the DLL search path so
    CTranslate2 (faster-whisper on CUDA) can load them. PATH alone is not honored
    for native DLL search on modern Windows Python — os.add_dll_directory is the
    robust mechanism. Host passes the dir via CF_TORCH_LIB; torch.__file__ is the
    fallback. Best-effort (no-op on failure)."""
    cand = os.getenv("CF_TORCH_LIB")
    if not (cand and os.path.isdir(cand)):
        try:
            import torch  # noqa
            cand = os.path.join(os.path.dirname(torch.__file__), "lib")
        except Exception:
            cand = None
    if cand and os.path.isdir(cand):
        try:
            os.add_dll_directory(cand)
        except Exception:
            pass
        os.environ["PATH"] = cand + os.pathsep + os.environ.get("PATH", "")


def _load_whisper(WhisperModel, model_name, device, compute):
    """Construct a WhisperModel; fall back to CPU/int8 on a CUDA load failure
    instead of hard-crashing ingest."""
    try:
        m = WhisperModel(model_name, device=device, compute_type=compute)
        print(f"[ingest] whisper device={device} compute={compute} model={model_name}", file=sys.stderr)
        return m
    except Exception as e:
        if device == "cuda":
            print(f"[ingest] whisper CUDA init failed ({e}); falling back to cpu/int8", file=sys.stderr)
            m = WhisperModel(model_name, device="cpu", compute_type="int8")
            print(f"[ingest] whisper device=cpu compute=int8 (fallback)", file=sys.stderr)
            return m
        raise


def _mk_progress(prog_file: str | None):
    """Return a write(pct, msg) that atomically updates the progress JSON file.

    No-op when prog_file is None (worker invoked without a progress sink). The
    host (generate._run_cf_worker) polls this file and forwards it to the job."""
    if not prog_file:
        return lambda pct, msg: None

    def write(pct, msg):
        try:
            tmp = prog_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"pct": int(pct), "msg": msg}, f, ensure_ascii=False)
            os.replace(tmp, prog_file)  # atomic — host never reads a half-written file
        except Exception:
            pass

    return write


def _download_audio(link: str, out_dir: str, sample_rate: int, ffmpeg_location: str | None,
                    clip_sec: int | None, progress=None) -> dict:
    """Pull bestaudio with yt-dlp, extract to a mono wav. Returns the info dict
    with the produced audio path added under 'audioPath'.

    clip_sec > 0 downloads ONLY the first clip_sec seconds (yt-dlp section
    download) — essential for hours-long sources where full transcription is
    infeasible on CPU."""
    import yt_dlp

    os.makedirs(out_dir, exist_ok=True)
    # Stable filename keyed by the video id so re-ingest overwrites, not piles up.
    outtmpl = os.path.join(out_dir, "%(id)s.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "wav", "preferredquality": "0"},
        ],
        # Force whisper-friendly 16 kHz mono so the wav stays small.
        "postprocessor_args": {"extractaudio": ["-ar", str(sample_rate), "-ac", "1"]},
    }
    if progress:
        def _hook(d):
            if d.get("status") == "downloading":
                tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                got = d.get("downloaded_bytes") or 0
                frac = (got / tot) if tot else 0.0
                progress(round(frac * 50), f"Tải nguồn {round(frac * 100)}%")  # download = first 50%
            elif d.get("status") == "finished":
                progress(50, "Đã tải, đang xử lý âm thanh")
        ydl_opts["progress_hooks"] = [_hook]
    if ffmpeg_location:
        ydl_opts["ffmpeg_location"] = ffmpeg_location
    if clip_sec and clip_sec > 0:
        from yt_dlp.utils import download_range_func

        ydl_opts["download_ranges"] = download_range_func(None, [(0, clip_sec)])
        ydl_opts["force_keyframes_at_cuts"] = True

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(link, download=True)

    audio_path = os.path.join(out_dir, f"{info['id']}.wav")
    if not os.path.isfile(audio_path):
        # Fallback: yt-dlp records the final path under requested_downloads.
        for d in info.get("requested_downloads", []):
            cand = d.get("filepath")
            if cand and cand.lower().endswith(".wav") and os.path.isfile(cand):
                audio_path = cand
                break
    if not os.path.isfile(audio_path):
        raise FileNotFoundError(f"extracted audio not found for {info.get('id')}")
    info["audioPath"] = audio_path
    return info


def _audio_from_local(link: str, local_media: str, out_dir: str, sample_rate: int,
                      ffmpeg_location: str | None, clip_sec: int | None,
                      progress=None) -> dict:
    """De-dup path (footage mode): the source VIDEO has already been downloaded by
    download_worker. Instead of a SECOND network pull of bestaudio, extract the
    whisper-friendly 16 kHz mono wav straight from that local mp4 with ffmpeg, and
    fetch ONLY lightweight metadata (skip_download) for the title/uploader/credit.

    This eliminates the redundant source download while preserving everything the
    runner needs (metadata for the end credit, a 16 kHz mono wav for whisper)."""
    import subprocess

    import yt_dlp

    os.makedirs(out_dir, exist_ok=True)
    if progress:
        progress(10, "Dùng nguồn đã tải — bóc âm thanh")

    # Metadata only — no media download (cheap; preserves title/uploader/logo for
    # the credit). Best-effort: if it fails (e.g. offline), fall back to a minimal
    # info dict derived from the local file.
    info: dict = {}
    try:
        # Metadata-only: the media is ALREADY downloaded locally, so a
        # format-selection failure (DRM / "requested format not available") must
        # not cost us the title/uploader used for the end credit.
        meta_opts = {"skip_download": True, "quiet": True,
                     "no_warnings": True, "noprogress": True,
                     "ignore_no_formats_error": True}
        _apply_yt_hardening(meta_opts)
        with yt_dlp.YoutubeDL(meta_opts) as ydl:
            info = ydl.extract_info(link, download=False) or {}
    except Exception as e:
        print(f"[ingest] metadata fetch failed for local-media path ({e}); "
              f"using minimal info", file=sys.stderr)
        info = {"id": os.path.splitext(os.path.basename(local_media))[0]}

    ffmpeg = "ffmpeg"
    if ffmpeg_location:
        cand = os.path.join(ffmpeg_location, "ffmpeg.exe")
        ffmpeg = cand if os.path.isfile(cand) else "ffmpeg"

    vid = info.get("id") or os.path.splitext(os.path.basename(local_media))[0]
    audio_path = os.path.join(out_dir, f"{vid}.wav")
    cmd = [ffmpeg, "-y"]
    # Mirror ingest's first-N-seconds cap so the transcription scope matches the
    # downloaded (start-anchored) video window.
    if clip_sec and clip_sec > 0:
        cmd += ["-t", str(int(clip_sec))]
    cmd += ["-i", local_media, "-vn", "-ar", str(sample_rate), "-ac", "1", audio_path]
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0 or not os.path.isfile(audio_path):
        err = (res.stderr or b"").decode("utf-8", "replace")[-400:]
        raise RuntimeError(f"ffmpeg failed to extract audio from {local_media}: {err}")
    if progress:
        progress(50, "Đã bóc âm thanh từ nguồn đã tải")
    info["audioPath"] = audio_path
    return info


def _fetch_channel_logo(info: dict, out_dir: str) -> str | None:
    """Best-effort: download the source channel's avatar (for the end credit).

    The avatar isn't in the video info, so we extract the channel page and pick
    its 'avatar_uncropped' thumbnail. Returns a local path, or None on any error.
    """
    import urllib.request

    chan_url = info.get("uploader_url") or info.get("channel_url")
    if not chan_url:
        return None
    try:
        import yt_dlp

        # Channel page — an avatar fetch, never a media fetch, so formats are
        # irrelevant here by construction.
        chan_opts = {"skip_download": True, "playlist_items": "0",
                     "quiet": True, "no_warnings": True,
                     "ignore_no_formats_error": True}
        _apply_yt_hardening(chan_opts)
        with yt_dlp.YoutubeDL(chan_opts) as ydl:
            chan = ydl.extract_info(chan_url, download=False)
        thumbs = chan.get("thumbnails") or []
        avatar = next((t for t in thumbs if t.get("id") == "avatar_uncropped"), None)
        if not avatar:
            squares = [t for t in thumbs if t.get("width") and t.get("width") == t.get("height")]
            avatar = max(squares, key=lambda t: t["width"]) if squares else None
        if not avatar or not avatar.get("url"):
            return None
        raw = os.path.join(out_dir, f"{info['id']}_logo_raw.jpg")
        dest = os.path.join(out_dir, f"{info['id']}_logo.jpg")
        req = urllib.request.Request(avatar["url"], headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r, open(raw, "wb") as f:
            f.write(r.read())
        # Downscale to a fixed 200x200 to keep the asset light (ffmpeg is on PATH).
        import subprocess

        try:
            subprocess.run(["ffmpeg", "-y", "-i", raw, "-vf", "scale=200:200:force_original_aspect_ratio=decrease",
                            dest], capture_output=True, timeout=30)
        except Exception:
            pass
        if not os.path.isfile(dest) or os.path.getsize(dest) == 0:
            dest = raw  # fall back to the original if the resize failed
        return dest if os.path.isfile(dest) and os.path.getsize(dest) > 0 else None
    except Exception:
        return None


def main() -> None:
    in_path, out_path = sys.argv[1], sys.argv[2]
    cfg = json.loads(Path(in_path).read_text(encoding="utf-8"))
    progress = _mk_progress(cfg.get("progressFile"))

    # yt-dlp's section-download path (download_ranges) probes for ffmpeg on PATH,
    # not just via ffmpeg_location — ffmpeg isn't on this machine's PATH, so put
    # its directory there for the duration of this run.
    ff = cfg.get("ffmpegLocation")
    if ff:
        os.environ["PATH"] = ff + os.pathsep + os.environ.get("PATH", "")

    local_media = cfg.get("localMedia")
    if local_media and os.path.isfile(local_media):
        # De-dup: footage mode already downloaded the source video — extract audio
        # from it locally instead of a second network pull.
        info = _audio_from_local(
            cfg["link"],
            local_media,
            cfg["outDir"],
            int(cfg.get("sampleRate", 16000)),
            cfg.get("ffmpegLocation") or None,
            cfg.get("clipSec") or None,
            progress,
        )
    else:
        info = _download_audio(
            cfg["link"],
            cfg["outDir"],
            int(cfg.get("sampleRate", 16000)),
            cfg.get("ffmpegLocation") or None,
            cfg.get("clipSec") or None,
            progress,
        )

    progress(50, "Tải xong — nạp mô hình bóc lời")
    _enable_cuda_dlls()
    from faster_whisper import WhisperModel

    model = _load_whisper(
        WhisperModel,
        cfg.get("model", "medium"),
        cfg.get("device", "cpu"),
        cfg.get("compute", "int8"),
    )
    language = cfg.get("language") or None        # None -> autodetect the source

    segments, tr_info = model.transcribe(info["audioPath"], language=language)
    total = float(tr_info.duration or info.get("duration") or 0)

    seg_out = []
    parts = []
    for seg in segments:                          # generator -> realize it
        text = seg.text.strip()
        seg_out.append({"start": round(seg.start, 3), "end": round(seg.end, 3), "text": text})
        parts.append(text)
        if total:                                 # transcribe = second 50% of the band
            frac = min(1.0, seg.end / total)
            progress(50 + round(frac * 50), f"Bóc lời {round(frac * 100)}%")
    progress(100, "Hoàn tất bóc lời")

    out = {
        "sourceUrl": info.get("webpage_url") or cfg["link"],
        "videoId": info.get("id"),
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "handle": info.get("uploader_id"),        # e.g. "@KezzaZomboid"
        # Extra source metadata — feeds the no-speech fallback (0 transcript
        # segments) so Claude can describe the video from title/desc/tags even
        # without spoken words. Old cached transcripts lack these keys; the
        # backend tolerates their absence (falls back to fetch_source_metadata).
        "description": info.get("description") or "",
        "tags": info.get("tags") or [],
        "categories": info.get("categories") or [],
        "logoPath": _fetch_channel_logo(info, os.path.dirname(info["audioPath"])),
        # Use the actually-transcribed audio length (correct even when clipped),
        # falling back to the source's full duration.
        "durationS": round(float(tr_info.duration or info.get("duration") or 0), 3),
        "thumbnail": info.get("thumbnail"),
        "audioPath": info["audioPath"],
        "language": tr_info.language,
        "transcript": " ".join(parts).strip(),
        "segments": seg_out,
    }
    Path(out_path).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
