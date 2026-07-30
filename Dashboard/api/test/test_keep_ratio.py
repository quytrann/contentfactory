"""Unit tests for the keep-ratio coverage metric (plan §B.1).

Pure-Python: exercises _check_keep_ratio with hand-built scene lists (no LLM,
no claude -p). Verifies the recap band, the summary band, no-band modes, the
bad-window guard, and the over/under nudge text selection.

Run: cd Dashboard/api && .venv/Scripts/python.exe -m pytest test/test_keep_ratio.py -q
Or:  .venv/Scripts/python.exe test/test_keep_ratio.py   (runs the asserts directly)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generate import (  # noqa: E402
    _check_keep_ratio,
    _KEEP_RATIO_BAND,
    _RATIO_REGEN_OVER,
    _RATIO_REGEN_UNDER,
)


def _scene(st, en):
    return {"scene": 1, "narration": "x", "sourceStart": st, "sourceEnd": en}


def test_recap_in_band():
    # window=100, kept 65s → 0.65, inside recap band [0.60, 0.75]
    clean = [_scene(0, 30), _scene(40, 75)]  # 30 + 35 = 65
    ratio, in_band, hint = _check_keep_ratio("recap", clean, 100.0)
    assert abs(ratio - 0.65) < 1e-9, ratio
    assert in_band is True
    assert hint is None


def test_recap_over_band():
    # window=100, kept 90s → 0.90 > 0.75 → OVER nudge
    clean = [_scene(0, 90)]
    ratio, in_band, hint = _check_keep_ratio("recap", clean, 100.0)
    assert abs(ratio - 0.90) < 1e-9, ratio
    assert in_band is False
    assert hint is not None
    assert _RATIO_REGEN_OVER in hint
    assert "~90%" in hint
    assert "60-75%" in hint
    assert "MODE recap" in hint


def test_recap_under_band():
    # window=100, kept 40s → 0.40 < 0.60 → UNDER nudge
    clean = [_scene(0, 40)]
    ratio, in_band, hint = _check_keep_ratio("recap", clean, 100.0)
    assert abs(ratio - 0.40) < 1e-9, ratio
    assert in_band is False
    assert _RATIO_REGEN_UNDER in hint
    assert "~40%" in hint


def test_summary_in_band():
    # window=100, kept 80s → 0.80, inside summary band [0.75, 0.85]
    clean = [_scene(0, 80)]
    ratio, in_band, hint = _check_keep_ratio("summary", clean, 100.0)
    assert abs(ratio - 0.80) < 1e-9, ratio
    assert in_band is True
    assert hint is None


def test_summary_under_band():
    # window=100, kept 50s → 0.50 < 0.75 → UNDER nudge, band shown as 75-85%
    clean = [_scene(0, 50)]
    ratio, in_band, hint = _check_keep_ratio("summary", clean, 100.0)
    assert in_band is False
    assert _RATIO_REGEN_UNDER in hint
    assert "75-85%" in hint
    assert "MODE summary" in hint


def test_no_band_modes_never_enforced():
    # commentary / educational / dubbed / unknown have NO band → in_band True, hint None
    clean = [_scene(0, 99)]  # would be 0.99, way over any band — must STILL pass
    for mode in ("commentary", "educational", "dubbed", "translate", "wtf"):
        ratio, in_band, hint = _check_keep_ratio(mode, clean, 100.0)
        assert in_band is True, mode
        assert hint is None, mode
        assert ratio == 0.0, mode  # no-band → ratio not computed


def test_mode_case_insensitive():
    clean = [_scene(0, 65)]
    ratio, in_band, hint = _check_keep_ratio("RECAP", clean, 100.0)
    assert in_band is True and abs(ratio - 0.65) < 1e-9


def test_bad_window_never_blocks():
    # window <= 0 → can't compute a fair denominator → in_band True, no hint
    clean = [_scene(0, 50)]
    for w in (0.0, -10.0):
        ratio, in_band, hint = _check_keep_ratio("recap", clean, w)
        assert ratio == 0.0
        assert in_band is True
        assert hint is None


def test_band_constants_match_guide():
    assert _KEEP_RATIO_BAND["recap"] == (0.60, 0.75)
    assert _KEEP_RATIO_BAND["summary"] == (0.75, 0.85)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} assertions passed.")
