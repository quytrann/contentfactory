"""PART B test — prove the SCRIPT-REUSE path BYPASSES script-gen (no `claude -p`).

These tests drive runner._process_job directly for a fake job dict that carries
`reuse_script_video_id`, with every external side effect (DB, models, FFmpeg,
publish, render-cache) stubbed so the test is hermetic (no Postgres / GPU / FFmpeg).

WHAT EACH TEST PROVES (read honestly):

  test_footage_reuse_bypasses_claude
    - For a FOOTAGE link job with reuse_script_video_id set, _process_job runs to
      _job_done WITHOUT calling ANY of the three script-gen entry points
      (generate_script / generate_script_footage / generate_script_transform) AND
      without ever reaching generate.subprocess.Popen (the literal `claude -p`
      spawn in _run_claude_script_once). It asserts the spawn args, if Popen were
      called, would have been [CLAUDE_BIN, "-p", ...] — but it is never called.
    - generate_ingest IS called (footage reuse still ingests: the cut step needs
      the source mp4 + each scene's sourceStart/sourceEnd).
    - generate_tts receives the EXACT reused scenes (same scene numbers + narration
      as the stored script), proving the cached script reaches TTS unchanged.

  test_image_reuse_bypasses_claude_and_ingest
    - Same bypass guarantees for an IMAGE job, PLUS: generate_ingest is NOT called
      (image/stickman reuse skips ingest entirely), and download_source_video is
      NOT called. generate_tts still receives the reused scenes.

  test_missing_source_script_fails_fast_no_claude
    - When the source video has NO saved script, the job fails fast with the
      Vietnamese message and never calls script-gen, Popen, ingest, or TTS.

WHAT THESE DO NOT PROVE: the actual media output (no real TTS/whisper/SDXL/FFmpeg
is run — those are stubbed). The cut/assemble steps are stubbed, so this does not
verify the produced clips are correct; it verifies the CONTROL FLOW reaches TTS
with the reused scenes and never reaches `claude -p`. End-to-end media correctness
requires the GPU/model stack and is out of scope here.

Run (cwd = Dashboard/api):
  .venv/Scripts/python.exe -m pytest test/fixtures/test_script_reuse_bypass.py -v
"""

import os
import sys
import tempfile

import pytest

# Point CONTENT_OUTPUT_ROOT at a throwaway dir before importing app modules (same
# pattern as test_clone_reassemble.py); nothing here writes media, but keep it safe.
_SCRATCH = tempfile.mkdtemp(prefix="cf_reuse_test_")
os.environ.setdefault("CONTENT_OUTPUT_ROOT", _SCRATCH)

# Make the api package importable (this file lives under api/test/fixtures/).
_API_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _API_DIR not in sys.path:
    sys.path.insert(0, _API_DIR)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_API_DIR, ".env"))
    os.environ["CONTENT_OUTPUT_ROOT"] = _SCRATCH  # re-assert over .env
except Exception:
    pass

import generate       # noqa: E402
import render_cache    # noqa: E402
import runner          # noqa: E402

# The known reused script the source video "holds". Footage scenes carry
# sourceStart/sourceEnd (what the cut step needs); narration is what must reach TTS.
REUSED_SCENES = [
    {"scene": 1, "narration": "Cau mot da luu", "sourceStart": 0.0, "sourceEnd": 4.0},
    {"scene": 2, "narration": "Cau hai da luu", "sourceStart": 4.0, "sourceEnd": 9.0},
    {"scene": 3, "narration": "Cau ba da luu", "sourceStart": 9.0, "sourceEnd": 13.5},
]
# An IMAGE-mode reused script: same narration, but image scenes carry image_prompt
# (the SDXL render path reads s["image_prompt"]) instead of source cut timecodes.
REUSED_IMAGE_SCENES = [
    {"scene": 1, "narration": "Cau mot da luu", "image_prompt": "a calm forest"},
    {"scene": 2, "narration": "Cau hai da luu", "image_prompt": "a quiet river"},
    {"scene": 3, "narration": "Cau ba da luu", "image_prompt": "a starry night"},
]
SRC_VIDEO_ID = 4242

# Per-test override: which scenes _load_reusable_script returns for SRC_VIDEO_ID.
_REUSE_RETURN = {"scenes": REUSED_SCENES}


@pytest.fixture
def spies(monkeypatch):
    """Stub every external side effect of _process_job and install spies on the
    script-gen entry points + the literal `claude -p` subprocess spawn."""
    # Default reused script = footage scenes; image test overrides before driving.
    _REUSE_RETURN["scenes"] = REUSED_SCENES
    rec = {
        "script_gen_calls": [],   # any generate_script* call -> records its name
        "popen_calls": [],        # any generate.subprocess.Popen call -> records argv
        "ingest_called": False,
        "download_called": False,
        "tts_scenes": None,       # the TtsScene list generate_tts actually received
        "done": False,
        "failed": None,
    }

    # --- script-gen spies: record-if-called (they must NOT be called on reuse). ---
    def _spy_script_name(name):
        def _inner(*a, **k):
            rec["script_gen_calls"].append(name)
            # Return a shape that would let the pipeline continue, so that IF the
            # bypass were broken the test still reaches the assertions (rather than
            # crashing) and reports the violation clearly. Carry image_prompt so the
            # IMAGE render path (runner builds ImageScene(scene, image_prompt)) can
            # consume these scenes; footage/transform ignore the extra key. This keeps
            # the discriminating witness about WHICH script-gen fn was called valid
            # for both footage and image render modes.
            return {"scenes": [{**dict(s), "image_prompt": s.get("image_prompt", "x")}
                               for s in _REUSE_RETURN["scenes"]]}
        return _inner

    monkeypatch.setattr(runner, "generate_script", _spy_script_name("generate_script"))
    monkeypatch.setattr(runner, "generate_script_footage", _spy_script_name("generate_script_footage"))
    monkeypatch.setattr(runner, "generate_script_transform", _spy_script_name("generate_script_transform"))

    # --- the literal `claude -p` spawn: prove it is never reached. ---
    # _run_claude_script_once does subprocess.Popen([CLAUDE_BIN, "-p", ...]) using the
    # `subprocess` name bound in generate.py. Wrap THAT: record + hard-fail ONLY when
    # the argv is a `claude -p` invocation (the script-gen spawn). Other Popen calls in
    # generate.py are legitimate FFmpeg/ffprobe probes (e.g. _nvenc_usable, _cut_clip)
    # that are unrelated to script-gen — those must pass through to the real subprocess
    # so the bypass test isn't confused by an encoder-capability check.
    _real_popen = generate.subprocess.Popen

    def _is_claude_spawn(args) -> bool:
        if not isinstance(args, (list, tuple)) or not args:
            return False
        argv = [str(x) for x in args]
        head = os.path.basename(argv[0]).lower()
        is_claude_bin = (argv[0] == generate.CLAUDE_BIN) or head.startswith("claude")
        return is_claude_bin and "-p" in argv

    def _guarded_popen(args, *a, **k):
        if _is_claude_spawn(args):
            rec["popen_calls"].append(list(args) if isinstance(args, (list, tuple)) else [args])
            raise AssertionError(f"claude -p was spawned on the reuse path! argv={args}")
        return _real_popen(args, *a, **k)

    monkeypatch.setattr(generate.subprocess, "Popen", _guarded_popen)

    # --- DB / video-row helpers ---
    # Signature note: runner._create_video(job_id, page_id, facebook_tags=None) — the
    # third arg was added after this fixture was written; accept it so the stub keeps
    # matching the real call site (runner.py:1518).
    monkeypatch.setattr(runner, "_create_video",
                        lambda job_id, page_id, facebook_tags=None: 99999)
    # 5th element (facebook_tags) mirrors the real _load_reusable_script signature
    # (runner.py:329) — a non-empty value here lets tests verify the reuse path copies
    # the source video's tags instead of firing a redundant claude -p hashtag call.
    monkeypatch.setattr(
        runner, "_load_reusable_script",
        lambda src_id: ([dict(s) for s in _REUSE_RETURN["scenes"]], "Tieu de nguon", "Nguon Name",
                        "https://src/link", "#tieuderused\n#test")
        if src_id == SRC_VIDEO_ID else (None, None, None, None, None),
    )
    monkeypatch.setattr(runner, "_save_script", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_save_assets", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_finalize_video", lambda *a, **k: None)
    # Auto Facebook-hashtag generation is a THIRD, independent `claude -p` call (runner.
    # _auto_fill_fb_tags) added after this fixture was written — it fires on every
    # produced video unless videos.facebook_tags is already set. It is out of scope for
    # this suite (script-gen bypass specifically) and would otherwise trip the popen
    # guard below, since _create_video is stubbed to skip the real INSERT its own
    # NULL-check depends on. The reuse-path facebook_tags copy this fix added is real
    # production code (runner.py, PART B) — covered separately, not by this stub.
    monkeypatch.setattr(runner, "_auto_fill_fb_tags", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_set_progress", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_job_done", lambda jid: rec.__setitem__("done", True))
    monkeypatch.setattr(runner, "_job_failed", lambda jid, vid, msg: rec.__setitem__("failed", msg))

    # --- model / IO stubs ---
    monkeypatch.setattr(runner, "set_model_busy", lambda *a, **k: None)
    monkeypatch.setattr(runner, "set_progress_cb", lambda *a, **k: None)
    monkeypatch.setattr(runner, "set_ff_progress_cb", lambda *a, **k: None)
    monkeypatch.setattr(runner, "make_thumbnail", lambda *a, **k: None)

    def _spy_download(req, *a, **k):
        rec["download_called"] = True
        return {"videoPath": os.path.join(_SCRATCH, "src.mp4")}
    monkeypatch.setattr(runner, "download_source_video", _spy_download)

    def _spy_ingest(req, *a, **k):
        rec["ingest_called"] = True
        # Minimal shape _process_job reads from ingest. Note: on the reuse footage
        # path the runner uses these only for source_name/title/duration; the scenes
        # come from the reused script, NOT from any script-gen over these segments.
        return {
            "uploader": "Ingest Uploader", "sourceUrl": "https://ingest/url",
            "logoPath": None, "handle": "@ing", "title": "Ingest Title",
            "segments": [{"start": 0.0, "end": 13.5, "text": "src transcript"}],
            "transcript": "src transcript", "language": "en", "durationS": 13.5,
        }
    monkeypatch.setattr(runner, "generate_ingest", _spy_ingest)

    def _spy_tts(req, *a, **k):
        rec["tts_scenes"] = list(req.scenes)
        # Return one audio result per requested scene so cut/assemble can proceed.
        return {"results": [
            {"scene": s.scene, "audioPath": os.path.join(_SCRATCH, f"a{s.scene}.wav"), "durationS": 1.0}
            for s in req.scenes
        ]}
    monkeypatch.setattr(runner, "generate_tts", _spy_tts)

    # cut / assemble / images -> no-op stubs (no FFmpeg / GPU).
    monkeypatch.setattr(runner, "_cut_clip", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_localize_images",
                        lambda page, jid, results: {r["scene"]: f"img{r['scene']}.png" for r in results})

    def _spy_images(req, *a, **k):
        return {"results": [{"scene": s.scene, "image_prompt": ""} for s in req.scenes]}
    monkeypatch.setattr(runner, "generate_images", _spy_images)

    def _fake_assemble(req, *a, **k):
        return {"videoPath": os.path.join(_SCRATCH, "out.mp4"), "durationS": 13.5,
                "width": 1080, "height": 1920}
    monkeypatch.setattr(runner, "assemble", _fake_assemble)
    monkeypatch.setattr(runner, "assemble_footage", _fake_assemble)

    # render-cache snapshot + post-success cleanup -> no-op.
    monkeypatch.setattr(render_cache, "store_render", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_cleanup_job_intermediates", lambda *a, **k: None)

    return rec


def _base_job(**over):
    job = {
        "id": 70001, "page_id": 1, "input_type": "link",
        "input_payload": "https://youtube.com/watch?v=abc",
        "voice": None, "edit_mode": "summary", "comment": None,
        "source_video_id": None, "aspect": "9:16", "target_sec": None,
        "add_credit": True, "publish": False, "render_model": None,
        "voice_clone_model": None, "src_audio_volume": 0.0,
        "clone_of_video_id": None, "reuse_script_video_id": SRC_VIDEO_ID,
        "title": None, "publish_platform": None,
    }
    job.update(over)
    return job


# NOTE: pages.architecture_type/config were dropped in the schema redesign; _load_page
# now returns only id/name/creator_name. render_mode is driven by the JOB (render_mode,
# render_model fallback), not the page — both fixtures below match the new _load_page
# shape and each test sets the job's render_model explicitly to pick the render mode.
def _footage_page():
    return {"id": 1, "name": "default", "creator_name": None}


def _image_page():
    return {"id": 1, "name": "default", "creator_name": None}


def test_footage_reuse_bypasses_claude(spies, monkeypatch):
    """FOOTAGE reuse: ingest KEPT, script-gen + claude -p BYPASSED, scenes -> TTS."""
    monkeypatch.setattr(runner, "_load_page", lambda pid: _footage_page())
    job = _base_job(render_model="passthrough-trim")  # -> render_mode "footage"

    runner._process_job(job)

    assert spies["failed"] is None, f"job unexpectedly failed: {spies['failed']}"
    assert spies["done"] is True, "footage reuse job did not reach _job_done"
    # CORE PROOF: no script-gen, no `claude -p`.
    assert spies["script_gen_calls"] == [], \
        f"script-gen was called on the reuse path: {spies['script_gen_calls']}"
    assert spies["popen_calls"] == [], \
        f"claude -p (subprocess.Popen) was spawned: {spies['popen_calls']}"
    # Footage reuse STILL ingests (cut needs source + timecodes).
    assert spies["ingest_called"] is True, "footage reuse should still ingest"
    # The reused scenes reach TTS unchanged (same numbers + narration).
    assert spies["tts_scenes"] is not None, "generate_tts was never called"
    got = [(s.scene, s.narration) for s in spies["tts_scenes"]]
    want = [(s["scene"], s["narration"]) for s in REUSED_SCENES]
    assert got == want, f"TTS did not receive the reused scenes: got={got} want={want}"


def test_image_reuse_bypasses_claude_and_ingest(spies, monkeypatch):
    """IMAGE reuse: ingest SKIPPED, download SKIPPED, script-gen + claude -p BYPASSED.

    CONTRACT NOTE (Phase 7 / H2): the IMAGE render mode is now selected by the
    render ENGINE, not by an implicit page/input_type default. We pass an EXPLICIT
    SDXL render_model ('sdxl-base', a key in generate.RENDER_CHECKPOINTS) so
    render_mode resolves to "image" at the `rm in RENDER_CHECKPOINTS` switch
    (runner.py ~969) — BEFORE the H2 default branch is ever reached, and regardless
    of input_type. This makes the ingest-skip assertion hold for the RIGHT reason
    (an explicit image engine), not the old implicit `arch!=translate -> image`
    default that H2 deliberately removed. We deliberately keep input_type="link"
    here to prove that an explicit image engine skips ingest EVEN with a source link
    present (the reuse path needs no source transcript for image mode)."""
    monkeypatch.setattr(runner, "_load_page", lambda pid: _image_page())
    # Reuse an IMAGE-mode script (scenes carry image_prompt for the SDXL path).
    _REUSE_RETURN["scenes"] = REUSED_IMAGE_SCENES
    # Explicit SDXL engine -> render_mode "image" via the RENDER_CHECKPOINTS switch,
    # independent of input_type. (Was: relied on the old story_voiceover default.)
    assert "sdxl-base" in generate.RENDER_CHECKPOINTS, \
        "sdxl-base must be a known image engine for this test's contract"
    job = _base_job(render_model="sdxl-base", input_type="link")

    runner._process_job(job)

    assert spies["failed"] is None, f"job unexpectedly failed: {spies['failed']}"
    assert spies["done"] is True, "image reuse job did not reach _job_done"
    assert spies["script_gen_calls"] == [], \
        f"script-gen was called on the reuse path: {spies['script_gen_calls']}"
    assert spies["popen_calls"] == [], \
        f"claude -p (subprocess.Popen) was spawned: {spies['popen_calls']}"
    # Image/stickman reuse skips ingest AND the source download entirely.
    assert spies["ingest_called"] is False, "image reuse must NOT ingest"
    assert spies["download_called"] is False, "image reuse must NOT download source"
    assert spies["tts_scenes"] is not None, "generate_tts was never called"
    got = [(s.scene, s.narration) for s in spies["tts_scenes"]]
    want = [(s["scene"], s["narration"]) for s in REUSED_IMAGE_SCENES]
    assert got == want, f"TTS did not receive the reused scenes: got={got} want={want}"


def test_footage_reuse_roundtrips_sourceStart_sourceEnd_to_cut(spies, monkeypatch):
    """SECTION E REGRESSION PIN: on the FOOTAGE reuse path, each reused scene's
    sourceStart/sourceEnd cut timecodes must round-trip UNTOUCHED from
    _load_reusable_script -> `scenes` -> the cut step (_cut_clip).

    WHY THIS IS DISTINCT FROM test_footage_reuse_bypasses_claude: that test only
    asserts (scene, narration) reach TTS. It stubs _cut_clip to a no-op and never
    inspects what timecodes the cut step received — so a regression that dropped or
    rewrote sourceStart/sourceEnd on the reuse path (e.g. rebuilding scene dicts from
    only {scene,narration}) would still PASS it. This test captures the actual
    (start, end) handed to _cut_clip per scene and asserts they equal the stored
    sourceStart/sourceEnd, proving footage cut timecodes survive the reuse round-trip.

    The cut step clamps timecodes against the probed source duration; we stub
    _probe_duration -> 0.0 (no clamp) so the captured values are the raw reused
    fields, making the assertion exact rather than clamp-dependent."""
    monkeypatch.setattr(runner, "_load_page", lambda pid: _footage_page())
    # commentary => NOT source-tracking => no per-scene budget / VO cap mangling, so
    # narration+timecodes pass through verbatim (Section D gate _mode_tracks_source).
    job = _base_job(render_model="passthrough-trim", edit_mode="commentary")

    # Force serial cuts so captured order == scene order (deterministic assertion).
    monkeypatch.setattr(runner, "_cut_workers", lambda: 1)
    # No clamp: probed source duration 0.0 disables the EOF clamp in _do_cut, so the
    # captured (start, end) are exactly the reused sourceStart/sourceEnd.
    monkeypatch.setattr(runner, "_probe_duration", lambda *a, **k: 0.0)

    cut_calls = []  # (src_path, start, end, dest) per _cut_clip invocation

    def _spy_cut(src_path, start, end, dest, *a, **k):
        cut_calls.append((start, end))
    monkeypatch.setattr(runner, "_cut_clip", _spy_cut)

    runner._process_job(job)

    assert spies["failed"] is None, f"footage reuse job failed: {spies['failed']}"
    assert spies["done"] is True, "footage reuse job did not reach _job_done"
    # No LLM on the reuse path (same core guarantee as the sibling tests).
    assert spies["script_gen_calls"] == [], \
        f"script-gen called on reuse path: {spies['script_gen_calls']}"
    assert spies["popen_calls"] == [], f"claude -p spawned: {spies['popen_calls']}"
    # CORE PROOF: the cut step received one cut per scene with the EXACT stored
    # sourceStart/sourceEnd — the footage cut timecodes round-tripped untouched.
    assert len(cut_calls) == len(REUSED_SCENES), \
        f"expected {len(REUSED_SCENES)} cuts, got {len(cut_calls)}: {cut_calls}"
    got = cut_calls
    want = [(s["sourceStart"], s["sourceEnd"]) for s in REUSED_SCENES]
    assert got == want, \
        f"sourceStart/sourceEnd did NOT round-trip into cut: got={got} want={want}"
    # And narration still reaches TTS unchanged alongside the timecodes.
    tts_got = [(s.scene, s.narration) for s in spies["tts_scenes"]]
    tts_want = [(s["scene"], s["narration"]) for s in REUSED_SCENES]
    assert tts_got == tts_want, f"TTS scenes wrong: got={tts_got} want={tts_want}"


def test_h2_link_no_render_model_defaults_to_footage(spies, monkeypatch):
    """PHASE 7 / H2 CONTRACT PIN (regression guard for the changed default).

    H2 (runner.py ~972) decoupled the implicit render-mode default from the page's
    architecture_type and re-keyed it off the SOURCE-LINK presence:

        OLD: render_mode = cfg.render_mode or ("footage" if arch=="translate" else "image")
        NEW: render_mode = cfg.render_mode or ("footage" if input_type=="link" else "image")

    This test pins the NEW behavior so a future regression back to the arch-keyed
    default is caught. We build a FRESH (non-reuse) job with render_model=None and
    input_type="link" on a NON-translate page (story_voiceover, via _image_page):

      * Under the NEW (H2) default: render_model=None + link  -> render_mode "footage".
        Footage+link DOWNLOADS the source (runner ~1056), INGESTS it (runner ~1076),
        and dispatches script-gen to generate_script_FOOTAGE (the `render_mode ==
        "footage"` branch, runner ~1149).
      * Under the OLD default this same job (non-translate page) would have resolved
        render_mode "image": NO source download, and script-gen would dispatch to
        generate_script_TRANSFORM (the trailing `else` branch), NOT footage.

    So the chosen script-gen entry point (footage vs transform) and download_called
    are an exact, discriminating witness of which default fired. We assert the H2
    (footage) witnesses. NOTE: render_model is None here (no explicit engine), which
    is the ONLY set H2 affects — the live DB has zero such link jobs (all live jobs
    carry render_model='passthrough-trim'), so this exercises the new contract on the
    exact slice H2 changed, without contradicting live behavior."""
    # Non-translate page: under the OLD arch-keyed default this would be "image".
    monkeypatch.setattr(runner, "_load_page", lambda pid: _image_page())
    # Fresh job: no reuse (so script-gen actually runs and reveals the render_mode),
    # render_model=None (the only H2-affected set), input_type="link", no config override.
    job = _base_job(render_model=None, input_type="link", reuse_script_video_id=None)
    assert job["render_model"] is None and job["input_type"] == "link"

    runner._process_job(job)

    assert spies["failed"] is None, f"H2 footage-default job unexpectedly failed: {spies['failed']}"
    assert spies["done"] is True, "H2 footage-default job did not reach _job_done"
    # WITNESS 1: footage script-gen entry point chosen (NOT transform/image) => render_mode=="footage".
    assert spies["script_gen_calls"] == ["generate_script_footage"], (
        "H2 contract: render_model=None + input_type='link' must resolve render_mode "
        f"'footage' and dispatch generate_script_footage, got {spies['script_gen_calls']} "
        "(if this is ['generate_script_transform'] the OLD arch-keyed 'image' default has regressed)"
    )
    # WITNESS 2: footage path downloads + ingests the source (image path would not download).
    assert spies["download_called"] is True, \
        "H2 footage default must download the source mp4 (footage+link path)"
    assert spies["ingest_called"] is True, "H2 footage default must ingest the source link"
    # Still no `claude -p` literal spawn here (the footage script-gen function is spied).
    assert spies["popen_calls"] == [], f"claude -p spawned: {spies['popen_calls']}"


def test_h2_no_link_no_render_model_defaults_to_image(spies, monkeypatch):
    """PHASE 7 / H2 CONTRACT PIN (the other side of the default).

    The NEW default yields "image" when there is NO source link: render_model=None +
    input_type='topic' -> render_mode "image". A topic-only job (no link) takes the
    trailing `else` branch (runner ~1182): NO download, NO ingest, and script-gen via
    generate_script (topic-only). This pins that render_model=None + non-link still
    means image, so the H2 condition is `input_type=="link"`, not "always footage"."""
    monkeypatch.setattr(runner, "_load_page", lambda pid: _image_page())
    # Topic-only job: no link, no reuse, no explicit engine -> H2 default "image".
    job = _base_job(render_model=None, input_type="topic", reuse_script_video_id=None,
                    input_payload="mot chu de bat ky")

    runner._process_job(job)

    assert spies["failed"] is None, f"H2 image-default job unexpectedly failed: {spies['failed']}"
    assert spies["done"] is True, "H2 image-default job did not reach _job_done"
    # Topic-only image path: generate_script (NOT footage/transform), no source touch.
    assert spies["script_gen_calls"] == ["generate_script"], (
        "H2 contract: render_model=None + non-link must resolve render_mode 'image' "
        f"(topic-only path -> generate_script), got {spies['script_gen_calls']}"
    )
    assert spies["download_called"] is False, "no-link image default must NOT download a source"
    assert spies["ingest_called"] is False, "no-link image default must NOT ingest"
    assert spies["popen_calls"] == [], f"claude -p spawned: {spies['popen_calls']}"


def test_non_reuse_DOES_call_script_gen(spies, monkeypatch):
    """NEGATIVE CONTROL (discriminating): a FRESH footage job (reuse_script_video_id
    = None) MUST go through generate_script_footage. This proves the script-gen spies
    are live and wired to the real call sites — so the bypass tests' "empty list"
    assertion is meaningful, not a no-op that would pass even if nothing were spied.

    (The spy short-circuits the real Claude call by returning scenes directly, so this
    still never spawns `claude -p`; we assert script-gen WAS entered, while popen_calls
    stays empty because the spy replaced the function that would have spawned it.)"""
    monkeypatch.setattr(runner, "_load_page", lambda pid: _footage_page())
    job = _base_job(render_model="passthrough-trim", reuse_script_video_id=None)

    runner._process_job(job)

    assert spies["failed"] is None, f"fresh job unexpectedly failed: {spies['failed']}"
    assert spies["script_gen_calls"] == ["generate_script_footage"], \
        f"fresh footage job should call generate_script_footage exactly once, got {spies['script_gen_calls']}"
    assert spies["ingest_called"] is True, "fresh footage job should ingest"


def test_missing_source_script_fails_fast_no_claude(spies, monkeypatch):
    """Source video with NO saved script -> fail fast, no script-gen/claude/ingest/TTS."""
    monkeypatch.setattr(runner, "_load_page", lambda pid: _footage_page())
    # Point reuse at an id our _load_reusable_script stub returns (None,...) for.
    job = _base_job(render_model="passthrough-trim", reuse_script_video_id=999999)

    runner._process_job(job)

    assert spies["done"] is False, "job should NOT complete when source script missing"
    assert spies["failed"] is not None, "missing source script should fail the job"
    assert "Video nguồn không có kịch bản" in spies["failed"], \
        f"unexpected failure message: {spies['failed']!r}"
    assert spies["script_gen_calls"] == [], "no script-gen on a fail-fast reuse"
    assert spies["popen_calls"] == [], "no claude -p on a fail-fast reuse"
    assert spies["ingest_called"] is False, "fail-fast should not ingest"
    assert spies["tts_scenes"] is None, "fail-fast should not reach TTS"
