"""Unit tests for the PURE dubbed-mode helpers (no ffmpeg / no LLM).

Covers _compute_keep_ranges and _remap_subs_post_trim against the spec tables in
_workspace/phase6_media_dubbed_spec.md (sections 1 and 3). Run:

    cd Dashboard/api && .venv/Scripts/python.exe -m pytest test/test_dubbed_helpers.py -q
    (or)              .venv/Scripts/python.exe test/test_dubbed_helpers.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generate import _compute_keep_ranges, _remap_subs_post_trim  # noqa: E402


def _f(start, end, reason="x"):
    return {"start": start, "end": end, "reason": reason}


def test_compute_keep_ranges():
    # spec section 1 table
    assert _compute_keep_ranges([], 100) == [(0.0, 100)]
    assert _compute_keep_ranges([_f(0, 5)], 100) == [(5, 100)]
    assert _compute_keep_ranges([_f(95, 100)], 100) == [(0.0, 95)]
    assert _compute_keep_ranges([_f(10, 20), _f(30, 40)], 100) == [(0.0, 10), (20, 30), (40, 100)]
    # overlap
    assert _compute_keep_ranges([_f(10, 20), _f(15, 25)], 100) == [(0.0, 10), (25, 100)]
    # touching
    assert _compute_keep_ranges([_f(10, 20), _f(20, 30)], 100) == [(0.0, 10), (30, 100)]
    # past end -> clamped
    assert _compute_keep_ranges([_f(90, 500)], 100) == [(0.0, 90)]

    # covers all -> raises
    for filler in ([_f(0, 100)], [_f(0, 50), _f(50, 100)]):
        try:
            _compute_keep_ranges(filler, 100)
            assert False, "expected ValueError (empty keep-set)"
        except ValueError:
            pass

    # src_dur <= 0 -> raises
    try:
        _compute_keep_ranges([], 0)
        assert False, "expected ValueError (bad src_dur)"
    except ValueError:
        pass

    # negative / degenerate filler dropped, then whole-source keep
    assert _compute_keep_ranges([_f(-5, -1), _f(50, 50)], 100) == [(0.0, 100)]
    print("test_compute_keep_ranges OK")


def _s(start, end, text="t"):
    return {"start": start, "end": end, "text_vi": text}


def test_remap_subs_post_trim():
    # keep_ranges = [(0,10),(20,30)] (filler is [10,20)). out timeline:
    #   seg0 [0,10)->out [0,10); seg1 [20,30)->out [10,20).
    keep = [(0, 10), (20, 30)]

    def remap_one(sub):
        return _remap_subs_post_trim([sub], keep)

    # inside keep0
    assert remap_one(_s(0, 5)) == [{"start": 0.0, "end": 5.0, "text_vi": "t"}]
    # fully inside filler -> dropped
    assert remap_one(_s(12, 18)) == []
    # straddles keep0->filler -> clipped to {8,10}
    assert remap_one(_s(8, 15)) == [{"start": 8.0, "end": 10.0, "text_vi": "t"}]
    # straddles filler->keep1 -> out {10,15}
    assert remap_one(_s(18, 25)) == [{"start": 10.0, "end": 15.0, "text_vi": "t"}]
    # spans keep0+filler+keep1 -> TWO events {8,10} and {10,15}
    assert remap_one(_s(8, 25)) == [
        {"start": 8.0, "end": 10.0, "text_vi": "t"},
        {"start": 10.0, "end": 15.0, "text_vi": "t"},
    ]
    # starts exactly at filler start (half-open) -> dropped
    assert remap_one(_s(10, 12)) == []
    # starts exactly at keep1 start -> out {10,12}
    assert remap_one(_s(20, 22)) == [{"start": 10.0, "end": 12.0, "text_vi": "t"}]
    # ends exactly at filler start -> kept {5,10}
    assert remap_one(_s(5, 10)) == [{"start": 5.0, "end": 10.0, "text_vi": "t"}]

    # empty input
    assert _remap_subs_post_trim([], keep) == []
    print("test_remap_subs_post_trim OK")


if __name__ == "__main__":
    test_compute_keep_ranges()
    test_remap_subs_post_trim()
    print("ALL DUBBED HELPER TESTS PASSED")
