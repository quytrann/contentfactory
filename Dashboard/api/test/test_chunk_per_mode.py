"""Tests for the PER-MODE chunk-size change in generate.py (job-13 summary timeout fix).

WHAT THIS COVERS
----------------
The script-gen splitter now picks its per-batch chunk size by edit mode instead of
one global constant. Summary keeps near-verbatim Vietnamese text (densest decode) so
it gets a SMALLER chunk (10); recap 12; commentary/educational 16; anything unknown
or None falls back to the global SCRIPT_GEN_CHUNK_SCENES (18). The pieces under test:

  _chunk_for_mode(edit_mode) -> int      # mode -> chunk, case-insensitive, fallback 18
  _batch_count(scene_count, chunk)       # ceil(scene_count / chunk), >= 1
  _split_counts(scene_count, batches)    # even-ish contiguous split, sums to scene_count
  _merge_renumber(batch_arrays)          # concat in order, renumber scene 1..N
  _run_batches_parallel(prompts)         # fixed-slot ordering by submit index

(a) BATCH-SPLIT PER MODE — each mode's chunk drives the right batch count + split.
(b) SCENE ORDER AFTER MERGE — _merge_renumber gives contiguous 1..N in input order,
    preserving other fields; and _run_batches_parallel keeps INPUT order even when
    completion order is scrambled (deterministic via per-prompt sleep).
(c) ENV OVERRIDE — SCRIPT_GEN_CHUNK_SCENES_SUMMARY overrides the summary default.

Pure unit tests — `generate._run_claude_script` is monkeypatched in the one test that
needs it; no LLM, no DB, no network.

Run: cd Dashboard/api && .venv/Scripts/python.exe -m pytest test/test_chunk_per_mode.py -q
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate  # noqa: E402


# --------------------------------------------------------------------------- #
# (a) BATCH-SPLIT PER MODE                                                    #
# --------------------------------------------------------------------------- #
# Each tuple: (edit_mode, expected_chunk, expected_batches) for scene_count=71.
_SCENES = 71
_MODE_CASES = [
    ("summary", 10, 8),       # ceil(71/10) = 8
    ("recap", 12, 6),         # ceil(71/12) = 6
    ("commentary", 16, 5),    # ceil(71/16) = 5
    ("educational", 16, 5),   # ceil(71/16) = 5
]


@pytest.mark.parametrize("mode,expected_chunk,expected_batches", _MODE_CASES)
def test_chunk_and_batch_count_per_mode(mode, expected_chunk, expected_batches):
    """_chunk_for_mode returns the mode's configured chunk, and _batch_count with that
    chunk yields the expected number of batches for 71 scenes."""
    chunk = generate._chunk_for_mode(mode)
    assert chunk == expected_chunk, f"{mode}: chunk {chunk} != expected {expected_chunk}"
    assert generate._batch_count(_SCENES, chunk) == expected_batches


@pytest.mark.parametrize("mode,expected_chunk,expected_batches", _MODE_CASES)
def test_split_counts_sum_and_evenness_per_mode(mode, expected_chunk, expected_batches):
    """_split_counts(71, batches) sums to 71 and is an even-ish split (max-min <= 1)."""
    counts = generate._split_counts(_SCENES, expected_batches)
    assert sum(counts) == _SCENES, f"{mode}: split {counts} sums to {sum(counts)} != {_SCENES}"
    assert len(counts) == expected_batches
    assert max(counts) - min(counts) <= 1, f"{mode}: split not even-ish: {counts}"


def test_summary_split_exact_shape():
    """Spec spells out summary's split shape: 71 over 8 batches -> seven 9s then one 8.
    divmod(71, 8) = (8, 7) -> first 7 batches get +1 (=9), last gets 8."""
    counts = generate._split_counts(71, 8)
    assert counts == [9, 9, 9, 9, 9, 9, 9, 8]
    assert sum(counts) == 71


def test_unknown_mode_falls_back_to_global_chunk():
    """An unrecognized mode string ('xyz') falls back to the global SCRIPT_GEN_CHUNK_SCENES
    (18 by default) -> ceil(71/18) = 4 batches."""
    assert generate._chunk_for_mode("xyz") == generate.SCRIPT_GEN_CHUNK_SCENES
    assert generate._chunk_for_mode("xyz") == 18
    assert generate._batch_count(_SCENES, generate._chunk_for_mode("xyz")) == 4


def test_none_mode_falls_back_to_global_chunk():
    """None mode (topic-only / no editMode) falls back to the global chunk -> 4 batches."""
    assert generate._chunk_for_mode(None) == generate.SCRIPT_GEN_CHUNK_SCENES
    assert generate._chunk_for_mode(None) == 18
    assert generate._batch_count(_SCENES, generate._chunk_for_mode(None)) == 4


def test_empty_string_mode_falls_back_to_global_chunk():
    """Empty-string mode behaves like None (the `(edit_mode or "")` normalization)."""
    assert generate._chunk_for_mode("") == generate.SCRIPT_GEN_CHUNK_SCENES


@pytest.mark.parametrize("variant", ["SUMMARY", "Summary", "sUmMaRy", " summary "])
def test_chunk_for_mode_case_insensitivity(variant):
    """Mode lookup is case-insensitive (lowercased). NOTE: leading/trailing spaces are
    NOT stripped by the helper, so ' summary ' is expected to MISS and fall back to the
    global chunk — only the cased variants resolve to 10. This documents real behavior."""
    if variant.strip() != variant:
        # Whitespace is not stripped -> falls back to global chunk.
        assert generate._chunk_for_mode(variant) == generate.SCRIPT_GEN_CHUNK_SCENES
    else:
        assert generate._chunk_for_mode(variant) == 10


def test_batch_count_degenerate_single_call():
    """scene_count <= chunk -> exactly 1 batch (the no-chunking degenerate case)."""
    assert generate._batch_count(5, 10) == 1
    assert generate._batch_count(10, 10) == 1
    assert generate._batch_count(11, 10) == 2


# --------------------------------------------------------------------------- #
# (b) SCENE ORDER AFTER MERGE                                                 #
# --------------------------------------------------------------------------- #
def test_merge_renumber_contiguous_and_preserves_fields():
    """Build fake per-batch arrays each numbered LOCALLY from 1; after _merge_renumber
    the merged 'scene' ids are exactly 1..N contiguous in input order, and every other
    field is preserved untouched."""
    batch_arrays = [
        [  # batch 0, local scenes 1..3
            {"scene": 1, "narration": "b0s0", "image_prompt": "img-b0-0"},
            {"scene": 2, "narration": "b0s1", "image_prompt": "img-b0-1"},
            {"scene": 3, "narration": "b0s2", "image_prompt": "img-b0-2"},
        ],
        [  # batch 1, local scenes 1..2
            {"scene": 1, "narration": "b1s0", "image_prompt": "img-b1-0"},
            {"scene": 2, "narration": "b1s1", "image_prompt": "img-b1-1"},
        ],
        [  # batch 2, local scenes 1..2
            {"scene": 1, "narration": "b2s0", "image_prompt": "img-b2-0"},
            {"scene": 2, "narration": "b2s1", "image_prompt": "img-b2-1"},
        ],
    ]
    total = sum(len(b) for b in batch_arrays)  # 7

    merged = generate._merge_renumber(batch_arrays)

    # Scene ids exactly 1..N contiguous.
    assert [s["scene"] for s in merged] == list(range(1, total + 1))
    # Input order preserved (batch0 scenes, then batch1, then batch2).
    assert [s["narration"] for s in merged] == [
        "b0s0", "b0s1", "b0s2", "b1s0", "b1s1", "b2s0", "b2s1"
    ]
    # Other fields preserved unchanged.
    assert [s["image_prompt"] for s in merged] == [
        "img-b0-0", "img-b0-1", "img-b0-2", "img-b1-0", "img-b1-1", "img-b2-0", "img-b2-1"
    ]


def test_merge_renumber_does_not_mutate_input():
    """_merge_renumber copies dicts before renumbering — the caller's input arrays must
    keep their original local 'scene' numbers."""
    batch = [{"scene": 1, "narration": "a"}, {"scene": 2, "narration": "b"}]
    arrays = [batch, [{"scene": 1, "narration": "c"}]]
    generate._merge_renumber(arrays)
    # Original batch still locally numbered 1,2 (not bumped to 1,2 global / unchanged).
    assert [s["scene"] for s in batch] == [1, 2]
    assert arrays[1][0]["scene"] == 1


class _ScrambledFinishStub:
    """Stub for _run_claude_script whose COMPLETION order is the reverse of submit order.

    Each prompt carries a 0-based batch index via a `#IDX=<i>#` marker. Earlier indices
    sleep LONGER so the last-submitted batch finishes first. Each returns a uniquely
    tagged scene array. If results were collected by completion order, the merged output
    would be visibly scrambled — so this deterministically catches an as-completed bug.
    """

    def __init__(self, n_batches, scenes_per_batch, unit_sleep=0.02):
        self.n_batches = n_batches
        self.scenes_per_batch = scenes_per_batch
        self.unit_sleep = unit_sleep
        self._lock = threading.Lock()
        self.peak = 0
        self.in_flight = 0

    @staticmethod
    def _index_of(prompt):
        import re
        m = re.search(r"#IDX=(\d+)#", prompt)
        assert m is not None, "prompt missing #IDX=..# marker"
        return int(m.group(1))

    def __call__(self, prompt, timeout=None):
        with self._lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            i = self._index_of(prompt)
            # Earlier index -> longer sleep -> finishes LATER (reverse of submit order).
            time.sleep((self.n_batches - i) * self.unit_sleep)
            return [
                {"scene": k + 1, "narration": f"batch{i}-scene{k}", "image_prompt": f"i{i}-{k}"}
                for k in range(self.scenes_per_batch)
            ]
        finally:
            with self._lock:
                self.in_flight -= 1


def test_run_batches_parallel_order_invariant_then_merge(monkeypatch):
    """_run_batches_parallel returns arrays in SUBMIT order even when batches finish in
    REVERSE order; the subsequent _merge_renumber then yields contiguous 1..N with the
    narration tags in submit order. Deterministic via per-prompt sleeps."""
    n = 5
    spb = 3
    monkeypatch.setattr(generate, "SCRIPT_GEN_CONCURRENCY", n)  # all overlap
    stub = _ScrambledFinishStub(n_batches=n, scenes_per_batch=spb, unit_sleep=0.02)
    monkeypatch.setattr(generate, "_run_claude_script", stub)

    prompts = [f"#IDX={i}# body" for i in range(n)]
    arrays = generate._run_batches_parallel(prompts)

    # Sanity: batches genuinely overlapped (otherwise the order test is trivial).
    assert stub.peak > 1, f"batches did not overlap (peak={stub.peak}); order test would be trivial"

    # Arrays land in SUBMIT order, identical to a sequential call of the same stub.
    sequential = [stub(p) for p in prompts]
    assert arrays == sequential, "results not in submit order — as-completed ordering bug"

    merged = generate._merge_renumber(arrays)
    assert [s["scene"] for s in merged] == list(range(1, n * spb + 1))
    expected_tags = [f"batch{i}-scene{k}" for i in range(n) for k in range(spb)]
    assert [s["narration"] for s in merged] == expected_tags


# --------------------------------------------------------------------------- #
# (c) ENV OVERRIDE                                                            #
# --------------------------------------------------------------------------- #
def test_env_override_via_importlib_reload(monkeypatch):
    """TRUE env-reload test: set SCRIPT_GEN_CHUNK_SCENES_SUMMARY in os.environ, reload the
    generate module, and assert the rebuilt SCRIPT_GEN_CHUNK_BY_MODE / _chunk_for_mode
    pick up the override. The module is reloaded a second time at teardown WITHOUT the
    env var so the rest of the suite sees the pristine defaults again.

    This is the strongest form of the assertion: it exercises the real import-time map
    construction (the `os.getenv(... )` comprehension), not just a stand-in monkeypatch
    of the already-built dict. Reliable on Windows — importlib.reload is in-process, no
    subprocess.
    """
    import importlib

    monkeypatch.setenv("SCRIPT_GEN_CHUNK_SCENES_SUMMARY", "7")
    try:
        importlib.reload(generate)
        assert generate.SCRIPT_GEN_CHUNK_BY_MODE["summary"] == 7, (
            "env SCRIPT_GEN_CHUNK_SCENES_SUMMARY=7 not reflected in the rebuilt map"
        )
        assert generate._chunk_for_mode("summary") == 7
        # Other modes still at their defaults (env did not touch them).
        assert generate.SCRIPT_GEN_CHUNK_BY_MODE["recap"] == 12
        assert generate.SCRIPT_GEN_CHUNK_BY_MODE["commentary"] == 16
    finally:
        # Restore the pristine module so later tests/imports see default 10 for summary.
        monkeypatch.delenv("SCRIPT_GEN_CHUNK_SCENES_SUMMARY", raising=False)
        importlib.reload(generate)

    # After reload-without-env, the default is back.
    assert generate.SCRIPT_GEN_CHUNK_BY_MODE["summary"] == 10
    assert generate._chunk_for_mode("summary") == 10


def test_module_attr_override_is_picked_up(monkeypatch):
    """Complementary, lighter proof that _chunk_for_mode reads the module-level map at
    call time (not a value captured at def-time): monkeypatch the dict entry and assert
    the helper returns the patched value. Auto-reverted by monkeypatch at teardown."""
    patched = dict(generate.SCRIPT_GEN_CHUNK_BY_MODE)
    patched["summary"] = 9
    monkeypatch.setattr(generate, "SCRIPT_GEN_CHUNK_BY_MODE", patched)
    assert generate._chunk_for_mode("summary") == 9
    # Unknown still uses the (un-patched) global fallback.
    assert generate._chunk_for_mode("nope") == generate.SCRIPT_GEN_CHUNK_SCENES


if __name__ == "__main__":
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-q"]))
