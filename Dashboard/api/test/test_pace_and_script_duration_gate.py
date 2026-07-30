"""Tests for the 2026-06-27 script-gen tuning pass:
  * _VI_WORDS_PER_SEC recalibration (measured 3.25) + the under-source invariant,
  * the PRIMARY pre-TTS script-step duration gate (_enforce_script_duration),
  * the constant changes (band, regen attempts, summary chunk).

Pure unit tests — no LLM/DB/network/ffmpeg.

Run: cd Dashboard/api && .venv/Scripts/python.exe -m pytest test/test_pace_and_script_duration_gate.py -q
"""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate as g  # noqa: E402
from fastapi import HTTPException  # noqa: E402


# ---------------------------------------------------------------------------
# Pace recalibration + under-source invariant.
# ---------------------------------------------------------------------------
# Slowest per-job pace observed across the 38 cached manifests measured 2026-06-27
# (the worst case the budget must survive). PRE-DATES the owner's 2026-07-26 reading-
# pace slowdown (voice reads slower now, so this is a conservative/faster-than-today
# anchor, not a live re-measurement) — the budget pace was set near this so the
# under-source invariant holds without leaning on the runtime gates.
_SLOWEST_OBSERVED_PACE = 2.23


def test_budget_pace_is_owner_safe_value():
    """Owner's safe choice (2026-06-27): the BUDGET pace is near the slow end of the
    measured pooled 3.25, NOT the mean — so the under-source invariant holds for the
    slowest jobs (reliability over hitting target length). Owner 2026-07-26: overall
    reading pace slowed further, dropping this from 2.5 to 2.2 in lockstep with
    GLOBAL_TARGET_MS_PER_SYL so the slower voice reads proportionally fewer words."""
    assert g._VI_WORDS_PER_SEC == 2.2


def test_under_source_invariant_at_budget_pace():
    """A script sized to the AUTO word ceiling renders to a VO that is UNDER source when
    delivered at the BUDGET pace (and far under at the measured mean ~3.27)."""
    for src in (60, 120, 300, 499.9, 700, 1200):
        ceiling = g._auto_word_ceiling(src)
        vo = ceiling / g._VI_WORDS_PER_SEC + g._CREDIT_SLATE_SEC
        assert vo < src, f"src={src}: ceiling VO {vo:.1f}s not under source at budget pace"


def test_under_source_invariant_near_slowest_pace():
    """The whole point of the budget-pace choice: a ceiling-sized script rendered at the
    SLOWEST observed pace (2.23, pre-2026-07-26-slowdown) stays essentially at/under
    source — the budget words-per-second (safety*pace = 0.9*2.2 = 1.98) sits below the
    slowest pace with margin to spare (today's voice is slower still, which only widens
    this margin further). In the extreme worst case it overshoots by under ~1.5% (the
    residual the runtime gates backstop), so we assert it never exceeds source by more
    than that tiny margin — proving the formula is slow-pace-safe for all practical
    purposes, unlike the rejected 3.25 budget."""
    assert g._DURATION_SAFETY * g._VI_WORDS_PER_SEC == pytest.approx(1.98)
    for src in (120, 300, 499.9, 700, 1200):
        ceiling = g._auto_word_ceiling(src)
        vo = ceiling / _SLOWEST_OBSERVED_PACE + g._CREDIT_SLATE_SEC
        # within +1.5% of source even at the worst observed delivery pace.
        assert vo <= src * 1.015, f"src={src}: slow-end VO {vo:.1f}s exceeds source by >1.5%"


def test_ceiling_formula_arithmetic():
    """Pin the exact ceiling arithmetic so a future pace/safety change is caught.

    Mirrors _auto_word_ceiling exactly, including the outro-seconds subtraction (the
    outro CTA is appended AFTER generation, same reasoning as the credit slate, so the
    content budget must exclude it too)."""
    src = 499.9
    target_seconds = max(1.0, src - g._CREDIT_SLATE_SEC - g._outro_seconds())
    expected = max(1, math.floor(target_seconds * g._DURATION_SAFETY * g._VI_WORDS_PER_SEC))
    assert g._auto_word_ceiling(src) == expected
    # safety*pace product (the budget words/sec) is below the budget pace itself.
    assert g._DURATION_SAFETY * g._VI_WORDS_PER_SEC < g._VI_WORDS_PER_SEC


# ---------------------------------------------------------------------------
# Script-step duration gate (_enforce_script_duration).
# ---------------------------------------------------------------------------
def _scenes(words: int, n: int = 5) -> list:
    per = max(1, words // n)
    counts = [per] * n
    counts[-1] += words - per * n
    return [{"scene": i + 1, "narration": " ".join(["t"] * max(0, c)),
             "sourceStart": 0, "sourceEnd": 1} for i, c in enumerate(counts)]


def test_epsilon_zero():
    assert g._SCRIPT_DURATION_EPSILON_SEC == 0.0


def test_script_duration_gate_over_source_fails():
    src = 100.0
    # words s.t. words/pace + slate >= src → words >= (src-slate)*pace
    words = math.ceil((src - g._CREDIT_SLATE_SEC) * g._VI_WORDS_PER_SEC) + 5
    with pytest.raises(HTTPException) as ei:
        g._enforce_script_duration(_scenes(words), src)
    assert ei.value.status_code == 422
    assert "dài hơn hoặc bằng video gốc" in ei.value.detail


def test_script_duration_gate_under_source_passes():
    src = 100.0
    words = int((src - g._CREDIT_SLATE_SEC) * g._VI_WORDS_PER_SEC) - 10
    g._enforce_script_duration(_scenes(words), src)  # no raise


def test_script_duration_gate_exactly_equal_fails():
    """Zero tolerance: est == source must FAIL (output must be strictly shorter)."""
    src = 100.0
    # Pick words so est == src exactly: words = (src - slate) * pace.
    words = round((src - g._CREDIT_SLATE_SEC) * g._VI_WORDS_PER_SEC)
    est = words / g._VI_WORDS_PER_SEC + g._CREDIT_SLATE_SEC
    # Construct scenes with EXACTLY `words` tokens.
    scenes = [{"scene": 1, "narration": " ".join(["t"] * words),
               "sourceStart": 0, "sourceEnd": 1}]
    if est >= src:
        with pytest.raises(HTTPException):
            g._enforce_script_duration(scenes, src)
    else:
        # rounding landed just under; one extra token must tip it to >= and fail
        scenes[0]["narration"] += " t"
        with pytest.raises(HTTPException):
            g._enforce_script_duration(scenes, src)


def test_script_duration_gate_slate_toggle():
    """Without the slate the same word count that failed WITH the slate can pass."""
    src = 100.0
    # Choose words so that WITH slate it is >= src but WITHOUT slate it is < src.
    words = round((src - 1.0) * g._VI_WORDS_PER_SEC)  # est_noslate ≈ 99s, est_slate ≈ 102s
    scenes = [{"scene": 1, "narration": " ".join(["t"] * words),
               "sourceStart": 0, "sourceEnd": 1}]
    with pytest.raises(HTTPException):
        g._enforce_script_duration(scenes, src, with_slate=True)
    g._enforce_script_duration(scenes, src, with_slate=False)  # no raise


def test_script_duration_gate_skips_unknown_source():
    g._enforce_script_duration(_scenes(99999), 0.0)   # no raise
    g._enforce_script_duration(_scenes(99999), None)  # no raise


# ---------------------------------------------------------------------------
# Constant changes.
# ---------------------------------------------------------------------------
def test_constants_updated():
    assert g._KEEP_RATIO_BAND["summary"] == (0.75, 0.85)
    assert g._RATIO_REGEN_ATTEMPTS == 1
    # Chunk sizes raised +25% in the 2026-06-28 perf pass (fewer batches per job).
    assert g._DEFAULT_MODE_CHUNKS["summary"] == 19
    assert g._chunk_for_mode("summary") == 19
    # SCRIPT_GEN_CONCURRENCY default is 4 (the live value may be overridden by .env to 4).
    assert g.SCRIPT_GEN_CONCURRENCY >= 4


# ---------------------------------------------------------------------------
# Two-directional target-pace factor (_target_pace_factor), owner-approved 2026-07-03.
# A fast voice (current < target) is SLOWED (factor<1); a slow voice (current > target) is
# SPED UP (factor>1) but bounded by the ceil. Pure math — no ffmpeg/audio.
# ---------------------------------------------------------------------------
@pytest.fixture
def pace_knobs(monkeypatch):
    """Pin the pace knobs to known values so the factor math is deterministic regardless of
    the live .env (target=200, floor=0.5, ceil=1.15)."""
    monkeypatch.setattr(g, "GLOBAL_TARGET_MS_PER_SYL", 200.0)
    monkeypatch.setattr(g, "GLOBAL_TARGET_ATEMPO_FLOOR", 0.5)
    monkeypatch.setattr(g, "GLOBAL_TARGET_ATEMPO_CEIL", 1.15)


def test_target_pace_env_reenabled_and_balanced():
    """The live config re-enables the normalizer at the balanced middle target with a
    bounded speed-up (the owner's 2026-07-03 decision). Ceil raised 1.15 -> 1.25 later
    (owner-confirmed, .env GLOBAL_TARGET_ATEMPO_CEIL): a slow voice may now be sped up
    up to +25%, not just +15%."""
    assert g.GLOBAL_TARGET_PACE is True
    assert g.GLOBAL_TARGET_MS_PER_SYL == 200.0
    assert g.GLOBAL_TARGET_ATEMPO_CEIL == pytest.approx(1.25)
    assert g.GLOBAL_TARGET_ATEMPO_FLOOR == pytest.approx(0.5)


def test_target_pace_factor_slows_fast_voice(pace_knobs):
    """A fast voice (140 < 200) is slowed: factor = 140/200 = 0.7 (<1.0 = longer audio)."""
    assert g._target_pace_factor(140.0) == pytest.approx(0.70)


def test_target_pace_factor_speeds_up_slow_voice_within_bound(pace_knobs):
    """A mildly slow voice (215 > 200) is sped up: factor = 215/200 = 1.075 (<= ceil 1.15)."""
    assert g._target_pace_factor(215.0) == pytest.approx(1.075)


def test_target_pace_factor_speedup_is_capped_by_ceil(pace_knobs):
    """A very slow voice (300 > 200) would need factor 1.5, but the +15% ceil caps it at 1.15
    so it is only NUDGED toward target, never rushed."""
    assert g._target_pace_factor(300.0) == pytest.approx(1.15)


def test_target_pace_factor_slowdown_is_floored(pace_knobs):
    """An extremely fast measurement (80) would need factor 0.4, but the 0.5 floor caps the
    stretch at 2× so no artifact-prone extreme stretch is applied."""
    assert g._target_pace_factor(80.0) == pytest.approx(0.5)


def test_target_pace_factor_at_target_is_noop(pace_knobs):
    """A voice already at target gets factor ~1.0 (no retime)."""
    assert g._target_pace_factor(200.0) == pytest.approx(1.0)


def test_target_pace_factor_ceil_one_reverts_to_slow_only(pace_knobs, monkeypatch):
    """Setting the ceil to 1.0 restores pure slow-only: a slow voice is never sped up."""
    monkeypatch.setattr(g, "GLOBAL_TARGET_ATEMPO_CEIL", 1.0)
    assert g._target_pace_factor(300.0) == pytest.approx(1.0)  # capped at 1.0 = no speed-up
    assert g._target_pace_factor(140.0) == pytest.approx(0.70)  # slowing still works


if __name__ == "__main__":
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-q"]))
