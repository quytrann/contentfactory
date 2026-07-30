"""Regression tests for the FIXED-mode multi-batch word-budget overshoot (job 45)
and the source-length batch cap.

THE BUG (now fixed)
-------------------
_build_footage_prompt's FIXED branch computed `budget = _word_budget(req.durationSec)`
using the WHOLE-video target duration, but _gen_footage_scenes passed the SAME req
(same durationSec) to every batch. So each of B batches independently targeted the
full-video budget → B× overshoot. Job 45 (source 499.9s, 40 scenes, summary chunk 10
→ 4 batches) produced ~4 × 665 = 2661 words → 738s VO > 499.9s source → FAILED at the
99% assembly duration gate after ~50 min.

THE FIX
-------
1. Source-length batch cap: _gen_footage_scenes clamps the batch count DOWN to the
   minimum the source length actually needs (no batch over an empty sub-window).
2. Per-batch FIXED budget: each batch's budget is the whole-video budget scaled by its
   sub-window fraction (sub-seconds / total_window), so B batches SUM to ~the whole
   budget, not B×.
3. FIXED fast-fail: an overshoot is caught at script-gen (_enforce_fixed_source_fit),
   not 50 min later at assembly.

These tests assert the three owner-required properties WITHOUT spawning `claude -p`:
  (a) a short source collapses to 1 batch;
  (b) the SUM of per-batch FIXED budgets ≈ the whole-video budget (NOT B×);
  (c) batch sub-windows are contiguous, non-overlapping, covering [0, window] once.

Run: cd Dashboard/api && .venv/Scripts/python.exe -m pytest test/test_footage_fixed_budget.py -q
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate  # noqa: E402
from generate import TransformFootageRequest, TimedSegment  # noqa: E402

# Job-45 settings: source 499.9s, 40 scenes, summary (chunk 10 → 4 batches).
WINDOW = 499.9
SCENE_COUNT = 40
MODE = "summary"


def _make_req(window: float, scene_count: int, *, content_span: float | None = None,
              auto: bool = False) -> TransformFootageRequest:
    """A footage req whose transcript content spans [0, content_span] (defaults to the
    full window). FIXED by default (auto=False) so the per-batch budget path is exercised."""
    span = window if content_span is None else content_span
    # A couple of contiguous segments covering [0, span] so every reached sub-window has text.
    return TransformFootageRequest(
        segments=[
            TimedSegment(start=0.0, end=span / 2, text="first half of the source transcript"),
            TimedSegment(start=span / 2, end=span, text="second half of the source transcript"),
        ],
        editMode=MODE,
        title="Sample source",
        durationSec=int(window),
        sceneCount=scene_count,
        windowSec=window,
        auto=auto,
    )


class _Spy:
    """Captures every prompt _run_claude_script is handed (one per batch)."""

    def __init__(self):
        self.prompts: list[str] = []

    # Signature must mirror generate._run_claude_script, INCLUDING force_regen:
    # _run_batches_parallel submits it POSITIONALLY
    # (ex.submit(_run_claude_script, prompt, SCRIPT_GEN_TIMEOUT, cache_parts[i], i,
    # force_regen)), so a stub missing it raises TypeError inside the executor.
    def __call__(self, prompt, timeout=None, cache_parts=None, batch_idx=0,
                 force_regen=False):
        self.prompts.append(prompt)
        return [{"scene": 1, "narration": "x", "sourceStart": 0, "sourceEnd": 4}]


def _budget_from_prompt(prompt: str) -> int:
    """Extract the per-batch word budget the FIXED branch printed
    ('...should be about {budget} words...')."""
    m = re.search(r"should be about (\d+) words", prompt)
    assert m, f"no FIXED word-budget line in prompt:\n{prompt[:400]}"
    return int(m.group(1))


def _subwindow_from_prompt(prompt: str) -> tuple[float, float]:
    """Extract THIS batch's [start, end] sub-window from the FIXED length line
    ('...covering source {start}-{end}s...')."""
    m = re.search(r"covering source (\d+)-(\d+)s", prompt)
    assert m, f"no FIXED sub-window line in prompt:\n{prompt[:400]}"
    return float(m.group(1)), float(m.group(2))


# ---------------------------------------------------------------------------
# (a) Short source → exactly ONE batch (mechanism 1).
# ---------------------------------------------------------------------------
def test_short_source_collapses_to_one_batch(monkeypatch):
    """A many-scene request whose CONTENT only spans a short slice of the window must
    collapse to a single batch — no redundant extra batches over empty sub-windows."""
    spy = _Spy()
    monkeypatch.setattr(generate, "_run_claude_script", spy)
    # 40 scenes would split into multiple batches by scene-count alone, but the content
    # only spans ~40s of a 499.9s window → the source-length cap collapses to 1 batch.
    req = _make_req(WINDOW, SCENE_COUNT, content_span=40.0)
    # Premise: scene-count alone wants >1 batch.
    assert generate._batch_count(SCENE_COUNT, generate._chunk_for_mode(MODE)) > 1
    generate._gen_footage_scenes(req, SCENE_COUNT, WINDOW)
    assert len(spy.prompts) == 1, f"expected 1 batch for a short source, got {len(spy.prompts)}"


def test_single_batch_fixed_budget_is_whole_video_budget(monkeypatch):
    """The single-batch FIXED budget equals the whole-video budget (fraction 1.0)."""
    spy = _Spy()
    monkeypatch.setattr(generate, "_run_claude_script", spy)
    req = _make_req(WINDOW, SCENE_COUNT, content_span=40.0)
    generate._gen_footage_scenes(req, SCENE_COUNT, WINDOW)
    assert len(spy.prompts) == 1
    whole = generate._word_budget(int(WINDOW))
    assert _budget_from_prompt(spy.prompts[0]) == whole


# ---------------------------------------------------------------------------
# (b) Multi-batch: SUM of per-batch FIXED budgets ≈ whole-video budget (NOT B×).
# ---------------------------------------------------------------------------
def test_multibatch_fixed_budgets_sum_to_whole_not_times_batches(monkeypatch):
    """Job-45 reproduction: full-source multi-batch. The per-batch FIXED budgets must
    SUM to ~the whole-video budget (within rounding), NOT B× it — that 4× sum is what
    produced 2661 words and the 99% assembly failure."""
    spy = _Spy()
    monkeypatch.setattr(generate, "_run_claude_script", spy)
    req = _make_req(WINDOW, SCENE_COUNT)  # content spans full window → genuinely multi-batch
    batches = generate._batch_count(SCENE_COUNT, generate._chunk_for_mode(MODE))
    assert batches > 1, "premise: job-45 settings must split into >1 batch"
    generate._gen_footage_scenes(req, SCENE_COUNT, WINDOW)
    assert len(spy.prompts) == batches, f"expected {batches} batches, got {len(spy.prompts)}"

    whole = generate._word_budget(int(WINDOW))
    per_batch = [_budget_from_prompt(p) for p in spy.prompts]
    total = sum(per_batch)

    # SUM ≈ whole budget (allow a few words of per-batch rounding slack).
    assert abs(total - whole) <= len(per_batch) + 1, (
        f"per-batch budgets {per_batch} sum to {total}, expected ≈ whole-video {whole}"
    )
    # Hard proof it is NOT the old B× behavior: the sum is far below B× the whole budget.
    assert total < whole * 1.5, f"sum {total} looks like a B× overshoot vs whole {whole}"
    # And no single batch carries the whole-video budget.
    assert max(per_batch) < whole, f"a batch budget {max(per_batch)} == whole {whole} (not divided)"


# ---------------------------------------------------------------------------
# (c) Sub-windows are contiguous, non-overlapping, covering [0, window] once.
# ---------------------------------------------------------------------------
def test_multibatch_subwindows_are_contiguous_and_cover_window_once(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(generate, "_run_claude_script", spy)
    req = _make_req(WINDOW, SCENE_COUNT)
    batches = generate._batch_count(SCENE_COUNT, generate._chunk_for_mode(MODE))
    assert batches > 1
    generate._gen_footage_scenes(req, SCENE_COUNT, WINDOW)

    windows = [_subwindow_from_prompt(p) for p in spy.prompts]
    # First starts at 0; last ends at window (rounded to int seconds in the prompt text).
    assert windows[0][0] == 0.0
    assert abs(windows[-1][1] - round(WINDOW)) <= 1
    # Contiguous and non-overlapping: each batch's start == previous batch's end.
    for (s0, e0), (s1, e1) in zip(windows, windows[1:]):
        assert s1 == e0, f"sub-windows not contiguous: {(s0, e0)} then {(s1, e1)}"
        assert e0 > s0 and e1 > s1, "degenerate (empty) sub-window"


# ---------------------------------------------------------------------------
# Source-length cap math is bounded by content, not blindly by scene_count/chunk.
# ---------------------------------------------------------------------------
def test_partial_source_caps_batch_count_between_one_and_full(monkeypatch):
    """Content spanning ~half the window needs ~half the full batches — fewer than the
    scene-count split, more than 1. Uses a LOCAL scene count high enough that the
    scene-count split alone wants >=4 batches at the current summary chunk (15), so the
    partial cap is visible (module SCENE_COUNT=40 → only 3 batches at chunk 15)."""
    spy = _Spy()
    monkeypatch.setattr(generate, "_run_claude_script", spy)
    local_sc = 70  # ceil(70/15) = 5 batches at the summary chunk
    full_batches = generate._batch_count(local_sc, generate._chunk_for_mode(MODE))
    assert full_batches >= 4, "premise needs enough batches to show a partial cap"
    # Content spans only ~55% of the window.
    req = _make_req(WINDOW, local_sc, content_span=WINDOW * 0.55)
    generate._gen_footage_scenes(req, local_sc, WINDOW)
    used = len(spy.prompts)
    assert 1 < used < full_batches, (
        f"partial source should use between 2 and {full_batches - 1} batches, got {used}"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-q"]))
