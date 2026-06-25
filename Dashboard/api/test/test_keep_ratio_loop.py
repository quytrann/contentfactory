"""Loop/regen tests for the keep-ratio bounded-regen LOOP (plan §B.2/B.3).

These pin the generate_script_footage() LOOP behavior WITHOUT spawning `claude -p`.
The loop's ONLY LLM touchpoint is generate._gen_footage_scenes(); we monkeypatch
THAT to return canned scene lists with known coverage ratios, and assert on:
  - how many times the LLM step was invoked (regen count),
  - which scene set is returned (in-band landing vs CLOSEST-on-exhaust),
  - whether/what ratio_nudge was forwarded on each call.

The scene lists are built so _check_keep_ratio computes the intended ratio:
window is pinned via TransformFootageRequest.windowSec; ratio = Σ(end-start)/window.
Scenes already lie inside [0, window] so _clamp_footage_scenes leaves them unchanged.

NOTE on attempt count: the code constant is `_RATIO_REGEN_ATTEMPTS = 2` (up to 2
EXTRA regens), so the EXHAUST case caps at 1 + 2 = 3 total calls. We assert against
the live constant, not a hardcoded 3, so the test tracks the source of truth.

Run: cd Dashboard/api && .venv/Scripts/python.exe -m pytest test/test_keep_ratio_loop.py -q
"""
import os
import sys
import logging

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate  # noqa: E402
from generate import (  # noqa: E402
    TransformFootageRequest,
    TimedSegment,
    _RATIO_REGEN_ATTEMPTS,
    _RATIO_REGEN_OVER,
    _RATIO_REGEN_UNDER,
)

WINDOW = 100.0


def _scenes_for_ratio(ratio: float) -> list:
    """One scene covering `ratio * WINDOW` seconds inside [0, WINDOW]. After
    _clamp_footage_scenes (which clamps to [0, WINDOW] and renormalizes) the kept
    span is unchanged, so _check_keep_ratio yields exactly `ratio` against WINDOW."""
    span = round(ratio * WINDOW, 6)
    return [{"scene": 1, "narration": "x", "sourceStart": 0.0, "sourceEnd": span}]


def _make_req(mode: str) -> TransformFootageRequest:
    # windowSec pins the ratio denominator to WINDOW; sceneCount pins the LLM call
    # to a single batch (1 <= SCRIPT_GEN_CHUNK_SCENES) so monkeypatching
    # _gen_footage_scenes captures exactly one invocation per attempt.
    return TransformFootageRequest(
        segments=[TimedSegment(start=0.0, end=WINDOW, text="source transcript here")],
        editMode=mode,
        durationSec=60,
        sceneCount=5,
        windowSec=WINDOW,
    )


class _SpyGen:
    """Stand-in for generate._gen_footage_scenes. Records every (call_index, kwargs)
    and returns the next scripted scene list. Mirrors the real signature exactly,
    INCLUDING the ratio_nudge kwarg the loop forwards on regen, so we can assert it."""

    def __init__(self, ratios):
        self._ratios = list(ratios)
        self.calls = []  # list of dicts: {"ratio_nudge": <str|None>}

    def __call__(self, req, scene_count, window, ratio_nudge=None):
        self.calls.append({"ratio_nudge": ratio_nudge,
                           "scene_count": scene_count, "window": window})
        idx = len(self.calls) - 1
        # If scripted ratios run out, repeat the last one (used by the exhaust test).
        ratio = self._ratios[idx] if idx < len(self._ratios) else self._ratios[-1]
        return _scenes_for_ratio(ratio)

    @property
    def count(self):
        return len(self.calls)


def _kept_fraction(result) -> float:
    return sum(s["sourceEnd"] - s["sourceStart"] for s in result["scenes"]) / WINDOW


# ---------------------------------------------------------------------------
# 1. recap OVER-band then IN-band → exactly 1 regen, returns the in-band set,
#    OVER-clause nudge forwarded on the 2nd call.
# ---------------------------------------------------------------------------
def test_recap_over_then_in_band(monkeypatch):
    spy = _SpyGen([0.90, 0.68])  # 0.90 > 0.75 (over), 0.68 in [0.60, 0.75]
    monkeypatch.setattr(generate, "_gen_footage_scenes", spy)

    result = generate.generate_script_footage(_make_req("recap"))

    assert spy.count == 2, f"expected 1 regen (2 calls), got {spy.count}"
    # First call: no nudge. Second call: nudge present, OVER clause.
    assert spy.calls[0]["ratio_nudge"] is None
    nudge = spy.calls[1]["ratio_nudge"]
    assert nudge is not None
    assert _RATIO_REGEN_OVER in nudge
    assert "~90%" in nudge and "60-75%" in nudge and "MODE recap" in nudge
    # Returned scenes are the in-band (0.68) set.
    assert abs(_kept_fraction(result) - 0.68) < 1e-9


# ---------------------------------------------------------------------------
# 2. ALL out-of-band AND NOT IMPROVING → PERF GUARD (B3): the loop stops EARLY on
#    the first non-improving regen instead of burning all _RATIO_REGEN_ATTEMPTS
#    passes. With an identical 0.90 every time, the first regen gains nothing, so
#    we stop at 2 calls (1 initial + 1 regen), NOT 1 + _RATIO_REGEN_ATTEMPTS. Still
#    returns the CLOSEST attempt and warns (no exception, owner Q4: log+proceed).
#    This is the job-14 regression fix: a non-converging nudge no longer wastes the
#    remaining ~8-concurrent-`claude -p` passes (and their 300s timeouts).
# ---------------------------------------------------------------------------
def test_recap_no_improvement_breaks_early(monkeypatch, caplog):
    spy = _SpyGen([0.90])  # always over-band (0.90), no pass ever gets closer
    monkeypatch.setattr(generate, "_gen_footage_scenes", spy)

    with caplog.at_level(logging.WARNING, logger="contentfactory.generate"):
        result = generate.generate_script_footage(_make_req("recap"))

    # 1 initial + exactly 1 (non-improving) regen, then early break.
    assert spy.count == 2, (
        f"perf guard: a non-improving regen must break early at 2 calls, got {spy.count}"
    )
    # Sanity: the guard must actually save passes vs the old exhaust behavior.
    assert spy.count < 1 + _RATIO_REGEN_ATTEMPTS or _RATIO_REGEN_ATTEMPTS <= 1
    # No exception (implicit). Closest attempt == 0.90 (all equal).
    assert abs(_kept_fraction(result) - 0.90) < 1e-9
    # The one regen still forwarded an OVER-clause nudge before the break.
    assert spy.calls[0]["ratio_nudge"] is None
    assert spy.calls[1]["ratio_nudge"] is not None and _RATIO_REGEN_OVER in spy.calls[1]["ratio_nudge"]
    # An early-break WARNING was logged carrying the (closest) ratio.
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("did NOT improve" in m and "90.0%" in m for m in warnings), warnings


# ---------------------------------------------------------------------------
# 2b. OUT-of-band but STILL IMPROVING each pass → the guard must NOT fire: the
#     loop keeps regenerating up to the full 1 + _RATIO_REGEN_ATTEMPTS budget
#     because every pass measurably reduces the band-distance. Confirms B3 does
#     not regress the legitimate "converging slowly, never quite lands" case.
# ---------------------------------------------------------------------------
def test_recap_improving_uses_full_budget(monkeypatch, caplog):
    # recap band is [0.60, 0.75]. Start far over, get CLOSER each pass but never
    # land in-band: 1.00 (d=0.25) -> 0.90 (d=0.15) -> 0.80 (d=0.05). Each step
    # improves by 0.10 (> _RATIO_REGEN_MIN_IMPROVE), so no early break.
    spy = _SpyGen([1.00, 0.90, 0.80])
    monkeypatch.setattr(generate, "_gen_footage_scenes", spy)

    with caplog.at_level(logging.WARNING, logger="contentfactory.generate"):
        result = generate.generate_script_footage(_make_req("recap"))

    expected_calls = 1 + _RATIO_REGEN_ATTEMPTS
    assert spy.count == expected_calls, (
        f"a steadily-improving sequence must use the full budget ({expected_calls}), got {spy.count}"
    )
    # Returns the CLOSEST attempt (the last, 0.80 — smallest band-distance).
    assert abs(_kept_fraction(result) - 0.80) < 1e-9
    # Exhausted via the for/else path (never improved INTO band) → "never reached band".
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("never reached band" in m for m in warnings), warnings


# ---------------------------------------------------------------------------
# 3. no-band mode (commentary) → exactly ONE call, no regen, no enforcement,
#    no nudge ever forwarded.
# ---------------------------------------------------------------------------
def test_commentary_no_band_single_call(monkeypatch):
    spy = _SpyGen([0.95])  # would be way over any band, but commentary has no band
    monkeypatch.setattr(generate, "_gen_footage_scenes", spy)

    result = generate.generate_script_footage(_make_req("commentary"))

    assert spy.count == 1, f"no-band mode must not regen; got {spy.count} calls"
    assert spy.calls[0]["ratio_nudge"] is None
    assert abs(_kept_fraction(result) - 0.95) < 1e-9


# ---------------------------------------------------------------------------
# 4. summary UNDER-band then IN-band → 1 regen, UNDER-clause nudge on 2nd call.
# ---------------------------------------------------------------------------
def test_summary_under_then_in_band(monkeypatch):
    spy = _SpyGen([0.50, 0.82])  # 0.50 < 0.76 (under), 0.82 in [0.76, 0.90]
    monkeypatch.setattr(generate, "_gen_footage_scenes", spy)

    result = generate.generate_script_footage(_make_req("summary"))

    assert spy.count == 2, f"expected 1 regen (2 calls), got {spy.count}"
    assert spy.calls[0]["ratio_nudge"] is None
    nudge = spy.calls[1]["ratio_nudge"]
    assert nudge is not None
    assert _RATIO_REGEN_UNDER in nudge
    assert "~50%" in nudge and "76-90%" in nudge and "MODE summary" in nudge
    assert abs(_kept_fraction(result) - 0.82) < 1e-9


if __name__ == "__main__":
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-q"]))
