"""Regression test for the multi-batch prompt UnboundLocalError in
generate._gen_footage_scenes (generate.py ~L1485).

THE BUG (now fixed)
-------------------
When scene_count exceeds SCRIPT_GEN_CHUNK_SCENES the function takes the
multi-batch branch (`batches > 1`) and loops over per-batch scene counts. The
broken version called `_run_claude_script(prompt, ...)` inside that loop WITHOUT
ever assigning `prompt` per batch — `prompt` was only bound in the single-batch
(`batches == 1`) branch, which the multi-batch path never executes. So the very
first batch iteration raised:

    UnboundLocalError: cannot access local variable 'prompt'
    where it is not associated with a value

The fix builds the per-batch prompt INSIDE the loop:

    prompt = _build_footage_prompt(req, n_scenes, sub_end,
                                   window_start=sub_start, ratio_nudge=ratio_nudge)

WHAT THIS TEST PROVES
---------------------
- Picks scene_count=40 which, at the live SCRIPT_GEN_CHUNK_SCENES (18), yields
  _batch_count(40) == 3 batches → forces the multi-batch branch (asserted, so the
  test stays honest if the constant changes).
- Monkeypatches _run_claude_script so NO real `claude -p` subprocess runs
  (local/free, deterministic, fast). The fake returns a tiny one-scene list and
  CAPTURES the `prompt` positional arg of every call.
- Asserts the call SUCCEEDS (no UnboundLocalError), is invoked once per batch,
  and every captured prompt is a NON-EMPTY str — exactly what the unbound version
  could not do (it raised before the first call completed with a real prompt).
- Asserts the merged result is renumbered 1..N contiguously across batches.

On the OLD (broken) code this test RAISES UnboundLocalError in the first batch
iteration (red); on the fixed code it PASSES (green). The red side was confirmed
out-of-band by reverting only the in-loop `prompt = ...` line in a scratch copy
(see the tester's report) — the real generate.py is never modified here.

Pure unit test — no LLM, no DB, no network.

Run: cd Dashboard/api && .venv/Scripts/python.exe -m pytest test/test_footage_multibatch_prompt.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate  # noqa: E402
from generate import TransformFootageRequest, TimedSegment  # noqa: E402

WINDOW = 120.0


class _SpyClaude:
    """Stand-in for generate._run_claude_script. Captures the `prompt` positional
    arg of every call and returns a tiny canned scene list (one scene per batch),
    so _gen_footage_scenes merges them and we can assert on call count + prompts."""

    def __init__(self):
        self.prompts = []  # captured prompt arg, one per batch call

    def __call__(self, prompt, timeout=None):
        self.prompts.append(prompt)
        # Minimal valid scene list — the merge/renumber path only needs dicts with
        # a 'scene' key plus the standard footage fields.
        return [{"scene": 1, "narration": "x", "sourceStart": 0, "sourceEnd": 4}]

    @property
    def count(self):
        return len(self.prompts)


def _make_req(scene_count: int) -> TransformFootageRequest:
    # A couple of segments whose [start, end] span the whole window so each batch's
    # contiguous sub-window has transcript text to slice. commentary = valid mode
    # with an EDIT_MODE_GUIDE entry so _build_footage_prompt does not 422.
    return TransformFootageRequest(
        segments=[
            TimedSegment(start=0.0, end=60.0, text="first half of the source transcript"),
            TimedSegment(start=60.0, end=WINDOW, text="second half of the source transcript"),
        ],
        editMode="commentary",
        title="Sample source",
        durationSec=60,
        sceneCount=scene_count,
        windowSec=WINDOW,
    )


def test_multibatch_scene_count_forces_more_than_one_batch():
    """Guard the premise: scene_count=40 must split into >1 batch at the live chunk
    size, otherwise the test would silently exercise the single-call branch instead
    of the multi-batch branch that carried the bug."""
    scene_count = 40
    batches = generate._batch_count(scene_count)
    assert batches > 1, (
        f"premise broken: scene_count={scene_count} yields {batches} batch(es) at "
        f"SCRIPT_GEN_CHUNK_SCENES={generate.SCRIPT_GEN_CHUNK_SCENES}; pick a larger count"
    )


def test_gen_footage_scenes_multibatch_does_not_raise_unbound_prompt(monkeypatch):
    """RED on the old code (UnboundLocalError in batch loop), GREEN on the fix.

    Drives _gen_footage_scenes directly through the multi-batch branch with a
    monkeypatched _run_claude_script, then asserts:
      * the call returned (no UnboundLocalError),
      * _run_claude_script was called once per batch,
      * every captured prompt is a non-empty str (the fix builds one per batch),
      * the merged scenes are renumbered 1..N contiguously.
    """
    scene_count = 40
    batches = generate._batch_count(scene_count)
    assert batches > 1  # premise (also covered by the dedicated test above)

    spy = _SpyClaude()
    monkeypatch.setattr(generate, "_run_claude_script", spy)

    req = _make_req(scene_count)

    # This call RAISED UnboundLocalError on the broken code; it must succeed now.
    result = generate._gen_footage_scenes(req, scene_count, WINDOW)

    # One claude call per batch.
    assert spy.count == batches, f"expected {batches} batch calls, got {spy.count}"

    # Every batch was handed a real, non-empty prompt string — the exact thing the
    # unbound version could not produce (it raised before binding `prompt`).
    for i, p in enumerate(spy.prompts):
        assert isinstance(p, str), f"batch {i}: prompt is {type(p).__name__}, expected str"
        assert p.strip(), f"batch {i}: prompt is empty/blank"

    # Merge/renumber: one scene per batch → N == batches, ids 1..N contiguous.
    assert len(result) == batches
    assert [s["scene"] for s in result] == list(range(1, batches + 1))


def test_gen_footage_scenes_multibatch_prompts_cover_disjoint_subwindows(monkeypatch):
    """Stronger check that each batch's prompt is built for its OWN contiguous
    source sub-window (the reason the in-loop _build_footage_prompt exists). The
    prompts must differ between batches — a single shared prompt (or a crash) would
    fail this. Confirms the fix rebuilds the prompt per batch rather than reusing
    one bound value."""
    scene_count = 40
    spy = _SpyClaude()
    monkeypatch.setattr(generate, "_run_claude_script", spy)

    generate._gen_footage_scenes(_make_req(scene_count), scene_count, WINDOW)

    batches = generate._batch_count(scene_count)
    assert spy.count == batches
    # Each batch covers a distinct [sub_start, sub_end] sub-window, so the prompts'
    # window-range text differs → no two batch prompts are identical.
    assert len(set(spy.prompts)) == batches, "batch prompts should be per-sub-window distinct"


if __name__ == "__main__":
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-q"]))
