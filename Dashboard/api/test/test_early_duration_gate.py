"""Tests for the EARLY post-TTS zero-tolerance duration gate (owner-requested).

WHY
---
Job 45 (and job 14 before it) wasted ~50 min of whisper + per-scene encode + concat
only to FAIL at the post-assembly duration guard (_enforce_duration_guard, the 99%
gate). The owner asked for a STRICT, fail-fast check at the EARLIEST point the REAL
output length is known: right after TTS produces every scene's VO (probed seconds),
BEFORE whisper/encode/concat. If VO + credit-slate seconds >= source by ANY amount
(zero tolerance, epsilon == 0) the job fails immediately.

WHAT THESE PROVE
----------------
- _enforce_post_tts_duration sums the ACTUAL per-scene VO durations + the slate and
  raises a RuntimeError with the Vietnamese length-violation message when total >=
  source — with ZERO tolerance (equal fails, and 1ms over fails).
- It is gated EXACTLY like the post-assembly guard: it only runs for source-tracking
  modes (summary/recap); the runner skips it for commentary/educational/dubbed.
- It self-skips (logs, no raise) when src_dur is unknown — mirroring the post-assembly
  guard so we never false-fail a job that gate intentionally skips.
- The slate is added only when assemble_footage would actually append it.

These are pure unit tests — no DB, no ffmpeg, no claude, no whisper.

Run: cd Dashboard/api && .venv/Scripts/python.exe -m pytest test/test_early_duration_gate.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import runner  # noqa: E402

SRC_DUR = 100.0
SLATE = runner._BUDGET_SLATE_SEC  # 3.0


def _scenes(n: int) -> list:
    return [{"scene": i + 1, "narration": "x"} for i in range(n)]


def _audio(durations: list[float]) -> dict:
    return {i + 1: {"audioPath": f"/tmp/s{i+1}.wav", "durationS": d}
            for i, d in enumerate(durations)}


# ---------------------------------------------------------------------------
# Zero tolerance: total >= source fails (no slack), total < source passes.
# ---------------------------------------------------------------------------
def test_epsilon_is_exactly_zero():
    """The threshold constant is 0 — any overshoot fails (owner: zero tolerance)."""
    assert runner._EARLY_DURATION_EPSILON_SEC == 0.0


def test_vo_plus_slate_over_source_fails_fast():
    # 90s VO + 3s slate = 93s < 100s would pass; push VO to 98 → 101 >= 100 → FAIL.
    scenes = _scenes(2)
    audio = _audio([60.0, 38.0])  # 98 + 3 = 101 >= 100
    with pytest.raises(RuntimeError) as ei:
        runner._enforce_post_tts_duration(1, audio, scenes, SRC_DUR,
                                          add_credit=True, has_slate_content=True)
    msg = str(ei.value)
    assert "dài hơn hoặc bằng video gốc" in msg
    assert "dừng ngay" in msg
    assert "giảm nội dung kịch bản" in msg


def test_exactly_equal_fails_zero_tolerance():
    """total == source must FAIL (the output must be STRICTLY shorter)."""
    scenes = _scenes(1)
    audio = _audio([SRC_DUR - SLATE])  # (100-3) + 3 = exactly 100 == source
    with pytest.raises(RuntimeError):
        runner._enforce_post_tts_duration(1, audio, scenes, SRC_DUR,
                                          add_credit=True, has_slate_content=True)


def test_one_millisecond_over_fails():
    """0.001s overshoot fails — proves there is no hidden tolerance."""
    scenes = _scenes(1)
    audio = _audio([SRC_DUR - SLATE + 0.001])  # total = 100.001 > 100
    with pytest.raises(RuntimeError):
        runner._enforce_post_tts_duration(1, audio, scenes, SRC_DUR,
                                          add_credit=True, has_slate_content=True)


def test_under_source_passes():
    scenes = _scenes(2)
    audio = _audio([50.0, 40.0])  # 90 + 3 = 93 < 100 → ok
    runner._enforce_post_tts_duration(1, audio, scenes, SRC_DUR,
                                      add_credit=True, has_slate_content=True)  # no raise


# ---------------------------------------------------------------------------
# Slate is counted only when it will actually be appended.
# ---------------------------------------------------------------------------
def test_no_slate_when_credit_disabled():
    """Without the slate, the same VO that failed WITH the slate now passes."""
    scenes = _scenes(1)
    audio = _audio([SRC_DUR - 1.0])  # 99 VO; +3 slate = 102 (fail), but no slate = 99 (pass)
    # With slate → fail
    with pytest.raises(RuntimeError):
        runner._enforce_post_tts_duration(1, audio, scenes, SRC_DUR,
                                          add_credit=True, has_slate_content=True)
    # Credit disabled → no slate → 99 < 100 → pass
    runner._enforce_post_tts_duration(1, audio, scenes, SRC_DUR,
                                      add_credit=False, has_slate_content=True)
    # No slate content (no logo/handle/name) → no slate either → pass
    runner._enforce_post_tts_duration(1, audio, scenes, SRC_DUR,
                                      add_credit=True, has_slate_content=False)


# ---------------------------------------------------------------------------
# Unknown source duration → skip + log (mirror the post-assembly guard).
# ---------------------------------------------------------------------------
def test_unknown_src_dur_skips():
    scenes = _scenes(1)
    audio = _audio([9999.0])  # absurdly long, but src_dur unknown → no raise
    runner._enforce_post_tts_duration(1, audio, scenes, 0.0,
                                      add_credit=True, has_slate_content=True)
    runner._enforce_post_tts_duration(1, audio, scenes, None,
                                      add_credit=True, has_slate_content=True)


# ---------------------------------------------------------------------------
# Missing/garbage per-scene duration is treated as 0 (never crashes the gate).
# ---------------------------------------------------------------------------
def test_missing_scene_duration_treated_as_zero():
    scenes = _scenes(2)
    audio = {1: {"durationS": 50.0}, 2: {"durationS": None}}  # scene 2 unknown → 0
    # 50 + 0 + 3 = 53 < 100 → pass (no crash on the None)
    runner._enforce_post_tts_duration(1, audio, scenes, SRC_DUR,
                                      add_credit=True, has_slate_content=True)


# ---------------------------------------------------------------------------
# Mode gating: the runner only CALLS the gate for source-tracking modes.
# (The helper itself is mode-agnostic by design; the gating lives at the call
#  site, mirroring _enforce_duration_guard. We pin that contract here.)
# ---------------------------------------------------------------------------
def test_mode_tracks_source_gating_matches_assembly_guard():
    # The early gate must be gated by the SAME predicate as the post-assembly guard.
    assert runner._mode_tracks_source("summary") is True
    assert runner._mode_tracks_source("recap") is True
    assert runner._mode_tracks_source("commentary") is False
    assert runner._mode_tracks_source("educational") is False
    assert runner._mode_tracks_source("dubbed") is False
    assert runner._mode_tracks_source(None) is False


def test_gate_is_called_before_assembly_on_footage_path():
    """Static proof that the early gate sits BEFORE assemble_footage in the footage
    branch (so it fires before whisper/encode/concat). We scan runner.py and assert the
    first _enforce_post_tts_duration( call appears before the footage assemble_footage(
    call, and that the call is guarded by _mode_tracks_source."""
    src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runner.py")
    with open(src_path, encoding="utf-8") as fh:
        text = fh.read()
    call = text.find("_enforce_post_tts_duration(\n")
    # Fall back to any call form if the multiline form isn't matched verbatim.
    if call == -1:
        call = text.find("_enforce_post_tts_duration(")
        # skip the def line
        defpos = text.find("def _enforce_post_tts_duration(")
        assert call != -1 and call != defpos
        call = text.find("_enforce_post_tts_duration(", defpos + 1)
    assert call != -1, "early gate is never called"
    assemble = text.find("res = assemble_footage(", call)
    assert assemble != -1 and assemble > call, (
        "early gate must be called BEFORE assemble_footage on the footage path"
    )
    # The call must be guarded by the source-tracking predicate just above it.
    guard = text.rfind("if _mode_tracks_source(edit_mode):", 0, call + 1)
    assert guard != -1 and (call - guard) < 600, (
        "early gate call must be guarded by _mode_tracks_source(edit_mode)"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-q"]))
