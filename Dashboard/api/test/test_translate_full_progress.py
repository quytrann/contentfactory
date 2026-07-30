"""Regression tests for LIVE render progress in `assemble_translate_full`.

THE BUG (now fixed)
-------------------
The runner installed an ffmpeg progress callback (set_ff_progress_cb) and set the job
row to "Dựng video (dịch) 0%" right before calling assemble_translate_full — but that
assembler never fed the callback. So for the WHOLE render step (6-10+ min, dominated by
the EasyOCR caption-cover detection) the dashboard chip sat frozen at 85% / "0%", then
jumped straight to done. assemble_footage already reported live progress via
_AssembleProgress; only translate_full was unwired, and it passed prog=None to
_finish_video, so even the credit-slate/concat tail was invisible.

THE FIX
-------
assemble_translate_full now builds the SAME _AssembleProgress controller and spends a
weighted slice on every sub-stage: per-scene cut, body concat, OCR detect (real
per-frame progress forwarded from the cf-venv worker's progressFile), the karaoke
whisper pass (capped time ramp — no signal available), the blur+subtitle burn, and the
_finish_video tail (prog is now passed through).

These tests assert the observable contract WITHOUT ffmpeg/EasyOCR/whisper:
  (a) progress is emitted repeatedly and is MONOTONIC, starting at 0 and ending at 100;
  (b) the OCR stage is the DOMINANT slice (the fix's whole point) and its worker
      progress is forwarded live, not frozen;
  (c) the ramp for a no-signal stage never claims the stage is finished before it is;
  (d) _finish_video receives a real prog (not None).

Run: cd Dashboard/api && .venv/Scripts/python.exe -m pytest test/test_translate_full_progress.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate  # noqa: E402
from generate import (  # noqa: E402
    FootageScene,
    TranslateFullAssembleRequest,
    _AssembleProgress,
    _ramped_step,
)


# --------------------------------------------------------------------------- #
# Unit: the primitives the fix adds to the shared controller                   #
# --------------------------------------------------------------------------- #
def test_step_manual_maps_fraction_into_its_own_slice_and_pins_the_end():
    seen = []
    prog = _AssembleProgress(lambda pct, msg: seen.append(pct), total_weight=100.0)
    prog.step(50.0, 1.0, lambda: None)          # first half consumed by a normal step
    prog.step_manual(50.0, lambda report: [report(0.0), report(0.5), report(1.0)])
    # The manual step's 0/0.5/1.0 must land at 50/75/100 of the whole, and the
    # accumulator must be pinned to the slice end afterwards.
    # [50 (first step's pin), 50, 75, 100 (the manual reports), 100 (the pin)]
    assert seen == [50, 50, 75, 100, 100]
    assert prog.done == pytest.approx(100.0)


def test_step_manual_phase_suffix_is_scoped_to_the_step():
    msgs = []
    prog = _AssembleProgress(lambda pct, msg: msgs.append(msg), total_weight=10.0,
                             label="Dựng video (dịch)")
    prog.step_manual(10.0, lambda report: report(0.5), phase=" — dò phụ đề gốc")
    assert msgs[0] == "Dựng video (dịch) 50% — dò phụ đề gốc"
    assert prog.phase == ""          # restored, so the next stage isn't mislabeled


def test_assemble_footage_label_and_message_unchanged():
    """The phase suffix is opt-in: controllers that never set it emit the old text."""
    msgs = []
    prog = _AssembleProgress(lambda pct, msg: msgs.append(msg), total_weight=10.0)
    prog.step(5.0, 1.0, lambda: None)
    assert msgs[-1] == "Dựng video 50%"


def test_ramped_step_never_claims_done_before_the_call_returns():
    """Honesty contract (mirrors runner._run_with_time_ramp): the estimate is capped
    below the slice top until fn actually returns."""
    import threading
    import time

    seen = []
    prog = _AssembleProgress(lambda pct, msg: seen.append(pct), total_weight=100.0)
    release = threading.Event()

    def _slow():
        release.wait(timeout=10)
        return "ok"

    t = threading.Thread(target=lambda: (time.sleep(1.6), release.set()), daemon=True)
    t.start()
    # tau tiny -> the ramp saturates almost immediately; it must STILL stop at 92%.
    assert _ramped_step(prog, 100.0, 0.05, " — căn phụ đề", _slow) == "ok"
    t.join()
    mid = [p for p in seen[:-1]]
    assert mid, "the ramp emitted nothing while the call was blocked"
    assert max(mid) <= 92, f"claimed {max(mid)}% before the stage finished"
    assert seen[-1] == 100          # snaps to the slice end once fn returns


# --------------------------------------------------------------------------- #
# Integration: the real assembler, with every heavy dependency stubbed          #
# --------------------------------------------------------------------------- #
@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    """Stub ffmpeg / EasyOCR / whisper so the assembler's PROGRESS wiring is what runs."""
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"0")
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"0")

    calls = {"finish_prog": "unset", "ocr_reports": 0}

    def _fake_scene_clip(*a, **k):
        # a[8] is the scene number in the positional call the assembler makes.
        p = tmp_path / f"s{a[8]}.mp4"
        p.write_bytes(b"0")
        return str(p)

    def _fake_cover(video_path, band, *a, **k):
        # Emulate the cf-venv worker streaming its per-frame progress through the
        # thread-local cb the assembler installs for this call.
        cb = getattr(generate._progress_local, "cb", None)
        assert cb is not None, "assembler did not install a worker progress cb for OCR"
        for pct in (6, 30, 60, 99, 100):
            cb(pct, "dò phụ đề gốc")
            calls["ocr_reports"] += 1
        return {"srcW": 1080, "srcH": 1920, "intervals": []}

    def _fake_finish(work, clips, out_path, **k):
        calls["finish_prog"] = k.get("prog")
        with open(out_path, "wb") as f:
            f.write(b"0")

    monkeypatch.setattr(generate, "_footage_scene_clip", _fake_scene_clip)
    monkeypatch.setattr(generate, "_run_ffmpeg", lambda *a, **k: None)
    monkeypatch.setattr(generate, "_caption_cover_intervals", _fake_cover)
    monkeypatch.setattr(generate, "_build_translate_full_karaoke",
                        lambda *a, **k: (None, []))
    monkeypatch.setattr(generate, "_finish_video", _fake_finish)
    monkeypatch.setattr(generate, "_probe_duration", lambda p: 12.0)
    return calls, str(clip), str(audio), str(tmp_path)


def _req(clip, audio, out_dir):
    return TranslateFullAssembleRequest(
        page="t", title="t", outDir=out_dir, width=1080, height=1920, fps=30,
        scenes=[FootageScene(scene=i, clipPath=clip, audioPath=audio,
                             caption=f"c{i}", durationS=10.0) for i in range(1, 5)],
    )


def test_progress_is_live_and_monotonic_across_every_substage(stubbed):
    calls, clip, audio, out_dir = stubbed
    seen = []
    generate.set_ff_progress_cb(lambda pct, msg: seen.append((pct, msg)))
    try:
        generate.assemble_translate_full(_req(clip, audio, out_dir))
    finally:
        generate.set_ff_progress_cb(None)

    pcts = [p for p, _ in seen]
    assert pcts, "the render step emitted NO progress (the original frozen-bar bug)"
    assert pcts == sorted(pcts), f"progress went backwards: {pcts}"
    # Ends at the top of the pre-tail band: _finish_video is stubbed here, so the
    # slate/concat/bgm weights it owns are never spent (the real call reaches 100).
    # HONESTY: it must NOT read 100% yet — the output file does not exist until the
    # tail's concat runs (observed on job 355 before the tail weight was floored).
    assert pcts[0] == 0
    assert 97 <= pcts[-1] <= 99, f"pre-tail percent was {pcts[-1]}"
    # It must MOVE, not just report the two endpoints.
    assert len(set(pcts)) >= 6, f"only {len(set(pcts))} distinct values: {pcts}"
    assert all(m.startswith("Dựng video (dịch) ") for _, m in seen)


def test_ocr_detection_is_the_dominant_slice_and_reports_live(stubbed):
    calls, clip, audio, out_dir = stubbed
    seen = []
    generate.set_ff_progress_cb(lambda pct, msg: seen.append((pct, msg)))
    try:
        generate.assemble_translate_full(_req(clip, audio, out_dir))
    finally:
        generate.set_ff_progress_cb(None)

    assert calls["ocr_reports"] == 5, "worker progress was not forwarded"
    ocr = [p for p, m in seen if "dò phụ đề gốc" in m]
    assert len(ocr) >= 5, f"OCR stage emitted only {len(ocr)} updates"
    # The dominant cost must own the biggest span of the bar.
    span = max(ocr) - min(ocr)
    assert span >= 50, f"OCR span is only {span} points ({min(ocr)}..{max(ocr)})"


def test_finish_video_gets_a_real_progress_controller(stubbed):
    calls, clip, audio, out_dir = stubbed
    generate.set_ff_progress_cb(lambda pct, msg: None)
    try:
        generate.assemble_translate_full(_req(clip, audio, out_dir))
    finally:
        generate.set_ff_progress_cb(None)
    prog = calls["finish_prog"]
    assert prog is not None, "_finish_video still receives prog=None (tail invisible)"
    # It must know the REAL output length, not the OCR-inflated total.
    assert prog.video_secs == pytest.approx(40.0)
    assert prog.total > prog.video_secs


def test_no_progress_sink_still_renders(stubbed):
    """Direct HTTP call (no runner cb installed) must behave exactly as before."""
    calls, clip, audio, out_dir = stubbed
    generate.set_ff_progress_cb(None)
    res = generate.assemble_translate_full(_req(clip, audio, out_dir))
    assert res["scenes"] == 4
