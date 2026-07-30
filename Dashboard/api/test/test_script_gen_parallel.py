"""Tests for the parallel script-gen path in generate.py.

WHAT THIS COVERS
----------------
The new `_run_batches_parallel(prompts)` helper runs the independent per-batch
`claude -p` calls in a ThreadPoolExecutor and is wired into the multi-batch path
of `_gen_footage_scenes` / `_gen_transform_scenes`, then merged by
`_merge_renumber`. We prove three things WITHOUT ever calling `claude -p` for real
(`generate._run_claude_script` is monkeypatched in every test):

(a) CONCURRENCY  — batches genuinely overlap; peak in-flight == min(concurrency, N),
                   plus a generous wall-clock sanity check.
(b) ORDER        — results land in SUBMIT order even when completion order is the
                   REVERSE of submit order (the #1 risk of an as-completed bug). The
                   merged/renumbered scenes are byte-identical to a sequential run.
                   Driven through BOTH `_gen_footage_scenes` and
                   `_gen_transform_scenes` real multi-batch paths.
(c) FAIL-PATH    — one batch raising HTTPException(504) propagates (Vietnamese
                   message preserved), no silent partial merge; a generic Exception
                   also propagates.

Pure unit tests — no LLM, no DB, no network.

Run: cd Dashboard/api && .venv/Scripts/python.exe -m pytest test/test_script_gen_parallel.py -q
"""
import os
import sys
import threading
import time

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate  # noqa: E402
from generate import (  # noqa: E402
    TransformFootageRequest,
    TransformRequest,
    TimedSegment,
)


# --------------------------------------------------------------------------- #
# (a) CONCURRENCY                                                             #
# --------------------------------------------------------------------------- #
class _ConcurrencyTracker:
    """Stub for _run_claude_script that sleeps so calls overlap, and records the
    PEAK number of calls in flight at once (incremented on entry, decremented on
    exit, under a lock). The peak is the robust, non-timing-dependent signal that
    the pool actually ran tasks in parallel."""

    def __init__(self, sleep=0.3):
        self.sleep = sleep
        self._lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.calls = 0

    def __call__(self, prompt, timeout=None, cache_parts=None, batch_idx=0):
        with self._lock:
            self.in_flight += 1
            self.calls += 1
            if self.in_flight > self.peak:
                self.peak = self.in_flight
        try:
            time.sleep(self.sleep)
            # Return a one-scene array; the per-prompt content is irrelevant here.
            return [{"scene": 1, "narration": "x", "image_prompt": "y"}]
        finally:
            with self._lock:
                self.in_flight -= 1


def test_run_batches_parallel_peak_concurrency(monkeypatch):
    """Peak in-flight count == min(SCRIPT_GEN_CONCURRENCY, N). This is the most
    robust proof of parallelism (independent of CI timing)."""
    n = 6
    monkeypatch.setattr(generate, "SCRIPT_GEN_CONCURRENCY", 4)
    tracker = _ConcurrencyTracker(sleep=0.3)
    monkeypatch.setattr(generate, "_run_claude_script", tracker)

    prompts = [f"prompt-{i}" for i in range(n)]
    result = generate._run_batches_parallel(prompts)

    assert len(result) == n
    assert tracker.calls == n
    expected_peak = min(generate.SCRIPT_GEN_CONCURRENCY, n)  # == 4
    assert tracker.peak == expected_peak, (
        f"expected peak concurrency {expected_peak}, observed {tracker.peak} — "
        "the batches did not overlap as expected"
    )


def test_run_batches_parallel_peak_capped_by_prompt_count(monkeypatch):
    """When N < concurrency, max_workers = min(concurrency, N) caps the peak at N
    (no idle over-provisioning, and >1 so it is still genuinely parallel)."""
    n = 3
    monkeypatch.setattr(generate, "SCRIPT_GEN_CONCURRENCY", 8)
    tracker = _ConcurrencyTracker(sleep=0.3)
    monkeypatch.setattr(generate, "_run_claude_script", tracker)

    generate._run_batches_parallel([f"p{i}" for i in range(n)])
    assert tracker.peak == n, f"expected peak {n} (capped by prompt count), got {tracker.peak}"


def test_run_batches_parallel_wallclock_overlaps(monkeypatch):
    """Secondary sanity check: wall-clock is clearly LESS than a sequential run
    (N * sleep). Generous margin (< N*sleep*0.6) so it does not flake on a loaded CI."""
    n = 6
    sleep = 0.3
    monkeypatch.setattr(generate, "SCRIPT_GEN_CONCURRENCY", 6)
    tracker = _ConcurrencyTracker(sleep=sleep)
    monkeypatch.setattr(generate, "_run_claude_script", tracker)

    t0 = time.perf_counter()
    generate._run_batches_parallel([f"p{i}" for i in range(n)])
    elapsed = time.perf_counter() - t0

    sequential = n * sleep
    assert elapsed < sequential * 0.6, (
        f"elapsed {elapsed:.2f}s not clearly under sequential {sequential:.2f}s — "
        "batches do not appear to run in parallel"
    )


def test_run_batches_parallel_empty(monkeypatch):
    """Empty list -> [] without spawning anything."""
    called = []
    monkeypatch.setattr(generate, "_run_claude_script",
                        lambda *a, **k: called.append(1) or [])
    assert generate._run_batches_parallel([]) == []
    assert called == []


# --------------------------------------------------------------------------- #
# (b) ORDER PRESERVED — the #1 risk                                          #
# --------------------------------------------------------------------------- #
class _ReverseFinishStub:
    """Stub whose COMPLETION order is the REVERSE of submit order.

    Each prompt carries a 0-based batch index (parsed from a `#<i>` tag in the
    prompt text). Earlier-index batches sleep LONGER, so the LAST-submitted batch
    finishes FIRST. Each returns a scene array uniquely tagged with its batch index
    (narration "batch<i>-sceneK"). If results were collected by completion order,
    the merge would be visibly scrambled — so this stub genuinely catches an
    as-completed ordering bug.
    """

    def __init__(self, n_batches, scenes_per_batch, unit_sleep=0.05):
        self.n_batches = n_batches
        self.scenes_per_batch = scenes_per_batch
        self.unit_sleep = unit_sleep

    @staticmethod
    def _index_of(prompt):
        # Prompts are tagged with a "#<i>#" marker the test injects (footage path)
        # OR we fall back to matching unique transcript content. For robustness we
        # require the marker to be present.
        import re
        m = re.search(r"#BATCHIDX=(\d+)#", prompt)
        assert m is not None, "prompt missing #BATCHIDX=..# marker"
        return int(m.group(1))

    def __call__(self, prompt, timeout=None, cache_parts=None, batch_idx=0):
        i = self._index_of(prompt)
        # Earlier index -> longer sleep -> finishes later in REVERSE of submit order.
        time.sleep((self.n_batches - i) * self.unit_sleep)
        return [
            {"scene": k + 1, "narration": f"batch{i}-scene{k}",
             "sourceStart": 0, "sourceEnd": 4, "image_prompt": f"img{i}-{k}"}
            for k in range(self.scenes_per_batch)
        ]


def test_run_batches_parallel_preserves_order_under_reverse_completion(monkeypatch):
    """Direct _run_batches_parallel + _merge_renumber order proof.

    The stub finishes batches in REVERSE submit order; the result MUST still be in
    submit order. We compare against the exact sequential equivalent."""
    n = 5
    spb = 2
    monkeypatch.setattr(generate, "SCRIPT_GEN_CONCURRENCY", 5)
    stub = _ReverseFinishStub(n_batches=n, scenes_per_batch=spb, unit_sleep=0.05)
    monkeypatch.setattr(generate, "_run_claude_script", stub)

    prompts = [f"#BATCHIDX={i}# prompt body" for i in range(n)]

    parallel_arrays = generate._run_batches_parallel(prompts)
    # Sequential reference: call the SAME stub in submit order.
    sequential_arrays = [stub(p) for p in prompts]

    assert parallel_arrays == sequential_arrays, "batch arrays not in submit order"

    merged = generate._merge_renumber(parallel_arrays)
    seq_merged = generate._merge_renumber(sequential_arrays)
    assert merged == seq_merged

    # Scenes renumbered 1..N contiguously...
    assert [s["scene"] for s in merged] == list(range(1, n * spb + 1))
    # ...and narration tags appear in submit order: batch0 scenes, then batch1, ...
    expected_tags = [f"batch{i}-scene{k}" for i in range(n) for k in range(spb)]
    assert [s["narration"] for s in merged] == expected_tags


def test_gen_footage_scenes_multibatch_preserves_order(monkeypatch):
    """Drive the REAL _gen_footage_scenes multi-batch path. We inject a per-batch
    #BATCHIDX# marker by intercepting _build_footage_prompt (recording call order),
    then assert the merged scene narrations are in submit order despite reverse
    completion."""
    scene_count = 40
    batches = generate._batch_count(scene_count)
    assert batches > 1, f"premise broken: {batches} batch(es) at chunk={generate.SCRIPT_GEN_CHUNK_SCENES}"

    monkeypatch.setattr(generate, "SCRIPT_GEN_CONCURRENCY", batches)

    # Replace the prompt builder with one that tags each prompt with its submit
    # index in build order (footage builds prompts sequentially before submitting).
    counter = {"i": 0}

    def _fake_build(req, n_scenes, window, window_start=0.0, ratio_nudge=None, total_window=None):
        i = counter["i"]
        counter["i"] += 1
        return f"#BATCHIDX={i}# footage n={n_scenes} ws={window_start} we={window}"

    monkeypatch.setattr(generate, "_build_footage_prompt", _fake_build)
    stub = _ReverseFinishStub(n_batches=batches, scenes_per_batch=3, unit_sleep=0.05)
    monkeypatch.setattr(generate, "_run_claude_script", stub)

    window = 120.0
    req = TransformFootageRequest(
        segments=[
            TimedSegment(start=0.0, end=60.0, text="first half source transcript words"),
            TimedSegment(start=60.0, end=window, text="second half source transcript words"),
        ],
        editMode="commentary",
        durationSec=60,
        sceneCount=scene_count,
        windowSec=window,
    )

    merged = generate._gen_footage_scenes(req, scene_count, window)

    assert counter["i"] == batches, f"expected {batches} prompts built, got {counter['i']}"
    # Scenes renumbered 1..N.
    assert [s["scene"] for s in merged] == list(range(1, len(merged) + 1))
    # Narration order == submit order (batch0..batch{B-1}), NOT completion order.
    expected_tags = [f"batch{i}-scene{k}" for i in range(batches) for k in range(3)]
    assert [s["narration"] for s in merged] == expected_tags, (
        "footage multi-batch order scrambled — looks like results were taken by "
        "completion order, not submit order"
    )


def test_gen_transform_scenes_multibatch_preserves_order(monkeypatch):
    """Drive the REAL _gen_transform_scenes multi-batch path with reverse-completion
    stub and assert submit-order preservation."""
    scene_count = 40
    batches = generate._batch_count(scene_count)
    assert batches > 1

    monkeypatch.setattr(generate, "SCRIPT_GEN_CONCURRENCY", batches)

    counter = {"i": 0}

    def _fake_build(req, n_scenes):
        i = counter["i"]
        counter["i"] += 1
        return f"#BATCHIDX={i}# transform n={n_scenes}"

    monkeypatch.setattr(generate, "_build_transform_prompt", _fake_build)
    stub = _ReverseFinishStub(n_batches=batches, scenes_per_batch=3, unit_sleep=0.05)
    monkeypatch.setattr(generate, "_run_claude_script", stub)

    # Transcript long enough that _split_transcript yields >= batches chunks
    # (one word per slot is enough; make it comfortably long).
    transcript = " ".join(f"word{i}" for i in range(400))
    req = TransformRequest(
        transcript=transcript,
        editMode="commentary",
        durationSec=60,
        sceneCount=scene_count,
    )

    merged = generate._gen_transform_scenes(req, scene_count)

    assert counter["i"] == batches, f"expected {batches} prompts built, got {counter['i']}"
    assert [s["scene"] for s in merged] == list(range(1, len(merged) + 1))
    expected_tags = [f"batch{i}-scene{k}" for i in range(batches) for k in range(3)]
    assert [s["narration"] for s in merged] == expected_tags, (
        "transform multi-batch order scrambled — completion-order bug"
    )


# --------------------------------------------------------------------------- #
# (c) FAIL-PATH                                                               #
# --------------------------------------------------------------------------- #
VI_504_MSG = "Viết kịch bản quá thời gian chờ (300s) sau 2 lần thử."


def _make_failing_stub(fail_index, exc):
    """Return a stub where the batch tagged #BATCHIDX=<fail_index># raises `exc`;
    all others sleep briefly and return a valid one-scene array. Used to prove
    fail-fast propagation with no partial merge."""
    import re

    def _stub(prompt, timeout=None, cache_parts=None, batch_idx=0):
        m = re.search(r"#BATCHIDX=(\d+)#", prompt)
        i = int(m.group(1)) if m else 0
        if i == fail_index:
            raise exc
        time.sleep(0.05)
        return [{"scene": 1, "narration": f"ok{i}", "image_prompt": "p"}]

    return _stub


def test_run_batches_parallel_propagates_http_504_with_vietnamese_message(monkeypatch):
    """A batch raising HTTPException(504) (as _run_claude_script does after its
    retries) must propagate out of _run_batches_parallel WITH its Vietnamese,
    user-facing message intact — and no partial result is returned."""
    monkeypatch.setattr(generate, "SCRIPT_GEN_CONCURRENCY", 4)
    exc = HTTPException(504, VI_504_MSG)
    monkeypatch.setattr(generate, "_run_claude_script",
                        _make_failing_stub(fail_index=2, exc=exc))

    prompts = [f"#BATCHIDX={i}# body" for i in range(4)]
    with pytest.raises(HTTPException) as ei:
        generate._run_batches_parallel(prompts)

    assert ei.value.status_code == 504
    assert ei.value.detail == VI_504_MSG, "Vietnamese 504 message was not preserved"


def test_run_batches_parallel_propagates_generic_exception(monkeypatch):
    """A batch raising a plain Exception also propagates (no swallowing)."""
    monkeypatch.setattr(generate, "SCRIPT_GEN_CONCURRENCY", 4)
    boom = RuntimeError("boom in batch 1")
    monkeypatch.setattr(generate, "_run_claude_script",
                        _make_failing_stub(fail_index=1, exc=boom))

    with pytest.raises(RuntimeError, match="boom in batch 1"):
        generate._run_batches_parallel([f"#BATCHIDX={i}# body" for i in range(4)])


def test_gen_footage_scenes_multibatch_fails_fast_no_partial_merge(monkeypatch):
    """End-to-end through _gen_footage_scenes: one failing batch makes the WHOLE
    gen raise (504), never returning a partial merged scene list."""
    scene_count = 40
    batches = generate._batch_count(scene_count)
    assert batches > 1
    monkeypatch.setattr(generate, "SCRIPT_GEN_CONCURRENCY", batches)

    counter = {"i": 0}

    def _fake_build(req, n_scenes, window, window_start=0.0, ratio_nudge=None, total_window=None):
        i = counter["i"]
        counter["i"] += 1
        return f"#BATCHIDX={i}# footage"

    monkeypatch.setattr(generate, "_build_footage_prompt", _fake_build)
    exc = HTTPException(504, VI_504_MSG)
    monkeypatch.setattr(generate, "_run_claude_script",
                        _make_failing_stub(fail_index=1, exc=exc))

    window = 120.0
    req = TransformFootageRequest(
        segments=[
            TimedSegment(start=0.0, end=60.0, text="first half words here"),
            TimedSegment(start=60.0, end=window, text="second half words here"),
        ],
        editMode="commentary",
        durationSec=60,
        sceneCount=scene_count,
        windowSec=window,
    )

    with pytest.raises(HTTPException) as ei:
        generate._gen_footage_scenes(req, scene_count, window)
    assert ei.value.status_code == 504
    assert ei.value.detail == VI_504_MSG


if __name__ == "__main__":
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-q"]))
