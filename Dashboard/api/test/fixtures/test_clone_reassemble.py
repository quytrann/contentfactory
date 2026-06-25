"""Unit test for PART 2: the runner's CLONE assemble-only path.

Builds a TINY fake render cache (_cache/renders/<id>/ with a real clip+audio+manifest)
under a scratch CONTENT_OUTPUT_ROOT, then drives runner._process_clone_job directly
and asserts:
  - it reconstructs scenes from the manifest's cached clip/image + audio paths,
  - it calls the EXISTING assemble() with the NEW width/height,
  - the produced output video is at the NEW aspect's frame size (verified via ffprobe),
  - it reuses the cached files only — NO TTS / whisper / SDXL is invoked
    (asserted by stubbing _run_cf_worker and generate_images to raise if called,
     and by using image mode so assemble() never needs whisper).

DB + publish + cleanup side effects are stubbed so the test is hermetic (no Postgres).
Run:  .venv/Scripts/python.exe test/fixtures/test_clone_reassemble.py
"""

import os
import subprocess
import sys
import tempfile

# Point the content root at a throwaway dir BEFORE importing the app modules, so
# every cache path resolves inside it (path-guard + RENDER_CACHE both honored).
_SCRATCH = tempfile.mkdtemp(prefix="cf_clone_test_")
os.environ["CONTENT_OUTPUT_ROOT"] = _SCRATCH
os.environ["RENDER_CACHE"] = "1"

# Make the api package importable (this file lives under api/test/fixtures/).
_API_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _API_DIR)

# Load the api/.env so FFMPEG_BIN / FFPROBE_BIN (full paths, not on PATH) resolve,
# exactly as the running server does — but KEEP our scratch CONTENT_OUTPUT_ROOT.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_API_DIR, ".env"))
    os.environ["CONTENT_OUTPUT_ROOT"] = _SCRATCH  # re-assert (in case .env set it)
except Exception:
    pass

import generate           # noqa: E402
import render_cache       # noqa: E402
import runner             # noqa: E402

FFMPEG = generate.FFMPEG_BIN
FFPROBE = generate.FFPROBE_BIN


def _make_tiny_clip(path: str, w: int = 1920, h: int = 1080, secs: float = 1.0) -> None:
    """A 1s silent test clip at the SOURCE aspect (16:9) — the thing a real cut clip is."""
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate=30:duration={secs}",
         "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono",
         "-t", f"{secs}", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", path],
        check=True, capture_output=True,
    )


def _make_tiny_image(path: str, w: int = 1920, h: int = 1080) -> None:
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate=1:duration=0.1",
         "-frames:v", "1", path],
        check=True, capture_output=True,
    )


def _make_tiny_wav(path: str, secs: float = 1.0) -> None:
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={secs}",
         "-ar", "48000", "-ac", "1", path],
        check=True, capture_output=True,
    )


def _probe_dims(path: str) -> tuple[int, int]:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0:s=x", path],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    w, h = out.split("x")
    return int(w), int(h)


def main() -> int:
    SRC_VIDEO_ID = 12345
    NEW_VIDEO_ID = 12346

    # --- 1) Build a fake render cache for SRC_VIDEO_ID in IMAGE mode (no whisper). ---
    cdir = render_cache.render_dir(SRC_VIDEO_ID)
    os.makedirs(cdir, exist_ok=True)
    img1 = os.path.join(cdir, "scene001.png")
    img2 = os.path.join(cdir, "scene002.png")
    wav1 = os.path.join(cdir, "scene001.wav")
    wav2 = os.path.join(cdir, "scene002.wav")
    _make_tiny_image(img1, 1920, 1080)
    _make_tiny_image(img2, 1920, 1080)
    _make_tiny_wav(wav1, 1.0)
    _make_tiny_wav(wav2, 1.0)

    import json
    manifest = {
        "videoId": SRC_VIDEO_ID, "page": "default", "renderMode": "image",
        "visualKind": "image", "title": "clone unit test", "aspect": "16:9",
        "width": 1920, "height": 1080,
        "sourceName": None, "sourceLink": None, "sourceLogo": None, "sourceHandle": None,
        "addCredit": False, "bgmPath": None, "bgmVolume": None, "srcAudioVolume": 0.0,
        "scenes": [
            {"scene": 1, "narration": "canh mot", "caption": "canh mot",
             "durationS": 1.0, "visual": "scene001.png", "audio": "scene001.wav"},
            {"scene": 2, "narration": "canh hai", "caption": "canh hai",
             "durationS": 1.0, "visual": "scene002.png", "audio": "scene002.wav"},
        ],
    }
    with open(render_cache.manifest_path(SRC_VIDEO_ID), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)

    # --- 2) Assert load_manifest resolves absolute cached paths. ---
    loaded = render_cache.load_manifest(SRC_VIDEO_ID)
    assert loaded is not None, "load_manifest returned None for a valid fake cache"
    assert len(loaded["scenes"]) == 2
    assert os.path.isfile(loaded["scenes"][0]["visualPath"])
    assert os.path.isfile(loaded["scenes"][0]["audioPath"])
    print("[test] load_manifest OK — 2 scenes, abs paths resolved")

    # --- 3) Stub side effects so the clone path is hermetic (no DB / publish / models). ---
    captured = {}
    # _load_page now returns only id/name/creator_name (architecture_type/config dropped);
    # the clone render path keys off render_mode/render_model, not the page.
    runner._load_page = lambda pid: {"id": pid, "name": "default", "creator_name": None}
    runner._save_script = lambda *a, **k: None
    runner._set_progress = lambda *a, **k: None
    runner._job_done = lambda *a, **k: captured.__setitem__("done", True)
    runner._job_failed = lambda jid, vid, msg: captured.__setitem__("failed", msg)
    runner._finalize_video = lambda *a, **k: captured.__setitem__("finalize", a)
    runner.set_model_busy = lambda *a, **k: None
    runner.set_ff_progress_cb = lambda *a, **k: None
    runner.publish_video_core = lambda *a, **k: {"results": []}
    runner._cleanup_job_intermediates = lambda *a, **k: None
    # render_cache.store_render (clone snapshots its own content) is REAL — exercise it.

    # Guard rails: assert NO model worker / image gen ever runs on the clone path.
    def _boom_worker(*a, **k):
        raise AssertionError("clone path invoked _run_cf_worker (whisper/tts) — must not!")

    def _boom_images(*a, **k):
        raise AssertionError("clone path invoked generate_images (SDXL) — must not!")

    generate._run_cf_worker = _boom_worker
    generate.generate_images = _boom_images

    # --- 4) Drive the clone path: 16:9 source -> 9:16 target (1080x1920). ---
    job = {
        "id": 99001, "page_id": 1, "clone_of_video_id": SRC_VIDEO_ID,
        "aspect": "9:16", "publish": False, "_video_id": NEW_VIDEO_ID,
    }
    runner._process_clone_job(job)

    assert "failed" not in captured, f"clone job FAILED: {captured.get('failed')}"
    assert captured.get("done"), "clone job did not reach _job_done"
    assert "finalize" in captured, "clone job did not call _finalize_video"

    # _finalize_video(video_id, audio_path, video_path, duration_s, width, height, thumb)
    fz = captured["finalize"]
    out_video = fz[2]
    out_w, out_h = fz[4], fz[5]
    assert os.path.isfile(out_video), f"output video not produced: {out_video}"

    probe_w, probe_h = _probe_dims(out_video)
    print(f"[test] finalize width/height = {out_w}x{out_h}; ffprobe = {probe_w}x{probe_h}")
    assert (probe_w, probe_h) == (1080, 1920), \
        f"output NOT at new 9:16 aspect: got {probe_w}x{probe_h}, expected 1080x1920"
    assert (out_w, out_h) == (1080, 1920), \
        f"finalize recorded wrong dims: {out_w}x{out_h}"

    # Clone snapshotted its OWN content (so it can be re-cloned).
    assert render_cache.has_cached_render(NEW_VIDEO_ID), "clone did not cache its own render"
    print(f"[test] clone re-cache OK — _cache/renders/{NEW_VIDEO_ID}/ exists")

    print("\nPASS: clone reconstructs scenes from cache + assembles at NEW aspect "
          "(1080x1920), no TTS/whisper/SDXL invoked.")
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        import shutil
        shutil.rmtree(_SCRATCH, ignore_errors=True)
        print(f"[test] cleaned up fake cache: {_SCRATCH}")
    sys.exit(code)
