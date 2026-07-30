"""Unit tests for _omnivoice_normalize_joins — the clause-join quiet normalizer.

This code EDITS RENDERED AUDIO, and its predecessor (the whisper-word-boundary gap shaper)
was reverted for clipping Vietnamese soft-consonant tails. The contract that makes this one
safe is narrow and must stay enforced:

    ONLY samples inside the run of true digital zeros that our own concat wrote may be
    added or removed. Never the model's clause-edge decay, never speech.

test_never_touches_audible_samples is the guard for exactly that: it strips the near-zero
samples from the before/after wavs and requires the remaining AUDIBLE sequences to be
byte-identical. If a future change starts trimming "quiet-ish" audio, that test fails.

Synthetic audio only — no ffmpeg, no GPU, no cache.

Run:  cd Dashboard/api && .venv/Scripts/python -m pytest test/test_omnivoice_join_normalize.py -q
"""

import array
import math
import os
import sys
import types
import wave

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate as g  # noqa: E402

SR = 48000


def _tone(n, amp=8000, f=180.0, phase=0.0):
    return [int(amp * math.sin(2 * math.pi * f * (i / SR) + phase)) for i in range(n)]


def _decay(n, start_amp=150):
    """Quiet-but-NOT-zero tail: this is the model's clause-edge decay. Sits below the
    -45 dBFS measuring threshold but far above the near-zero edit threshold."""
    return [int(start_amp * (1 - i / max(1, n))) for i in range(n)]


def _write(path, samples):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(array.array("h", samples).tobytes())


def _read(path):
    with wave.open(path, "rb") as w:
        a = array.array("h")
        a.frombytes(w.readframes(w.getnframes()))
    return a


def _scene_with_join(tmp_path, zeros_s, decay_s, speech_s=1.0, name="in.wav"):
    """speech | decay | ZEROS | decay | speech  — the real shape of a clause join."""
    p = str(tmp_path / name)
    d = int(decay_s * SR)
    samples = (_tone(int(speech_s * SR))
               + _decay(d)
               + [0] * int(zeros_s * SR)
               + list(reversed(_decay(d)))
               + _tone(int(speech_s * SR), phase=1.0))
    _write(p, samples)
    return p


def _join_extent(path):
    """Total quiet (below the measuring threshold) around the interior zero run, seconds.

    Measures the QUIET directly rather than going via the zero-run detector: after a shrink
    the residual zero run can legitimately fall below the detection floor, and the thing under
    test is the total quiet at the join, not whether the marker survived."""
    a = _read(path)
    n = len(a)
    thr = int(32768 * (10 ** (g.OMNIVOICE_JOIN_QUIET_DB / 20.0)))
    first = next((i for i, v in enumerate(a) if abs(v) >= thr), None)
    if first is None:
        return None
    last = next((i for i in range(n - 1, -1, -1) if abs(a[i]) >= thr), first)
    best, run = None, None
    for i in range(first, last + 1):
        if abs(a[i]) < thr:
            if run is None:
                run = i
        else:
            if run is not None:
                span = i - run
                if best is None or span > best:
                    best = span
            run = None
    return (best / SR) if best else None


def _run(tmp_path, path):
    scene = types.SimpleNamespace(scene=7, audioPath=path, durationS=1.23)
    work = str(tmp_path / "work")
    os.makedirs(work, exist_ok=True)
    edits = g._omnivoice_normalize_joins(scene, work)
    return scene, edits


# ---------------------------------------------------------------------------- safety
def test_never_touches_audible_samples(tmp_path):
    """THE safety invariant. Both directions exercised (grow and shrink)."""
    amp = g.OMNIVOICE_UNIT_SILENCE_AMP
    for zeros_s, decay_s in ((0.09, 0.02), (0.09, 0.25), (0.12, 0.005)):
        src = _scene_with_join(tmp_path, zeros_s, decay_s, name=f"z{zeros_s}_{decay_s}.wav")
        before = _read(src)
        scene, edits = _run(tmp_path, src)
        after = _read(scene.audioPath)
        aud_b = [v for v in before if abs(v) > amp]
        aud_a = [v for v in after if abs(v) > amp]
        assert aud_b == aud_a, (
            f"audible samples changed for zeros={zeros_s}s decay={decay_s}s "
            f"({len(aud_b)} -> {len(aud_a)})")


def test_never_edits_the_input_file_in_place(tmp_path):
    """audioPath may point into the shared TTS cache — the input must never be rewritten."""
    src = _scene_with_join(tmp_path, 0.09, 0.02)
    original = bytes(_read(src).tobytes())
    scene, _ = _run(tmp_path, src)
    assert scene.audioPath != src
    assert bytes(_read(src).tobytes()) == original


# ---------------------------------------------------------------------------- behaviour
def test_grows_a_short_join_to_target(tmp_path):
    """job 313 scene 23 polarity: join far below target must be padded up."""
    src = _scene_with_join(tmp_path, 0.09, 0.01)
    assert _join_extent(src) < g.OMNIVOICE_JOIN_TARGET_S
    scene, edits = _run(tmp_path, src)
    assert edits and edits[0][1] > 0
    assert _join_extent(scene.audioPath) == pytest.approx(g.OMNIVOICE_JOIN_TARGET_S, abs=0.005)


def test_shrinks_a_long_join_to_target(tmp_path):
    """job 315 scene 25 polarity: join above target must be cut back. The add-only design
    could NOT do this, which is why the fix is bidirectional."""
    # Enough zeros that the shrink is not limited by OMNIVOICE_JOIN_MIN_ZEROS_S
    # (the floor-limited case is covered by test_cannot_shrink_past_the_model_decay).
    src = _scene_with_join(tmp_path, 0.18, 0.06)
    assert _join_extent(src) > g.OMNIVOICE_JOIN_TARGET_S
    scene, edits = _run(tmp_path, src)
    assert edits and edits[0][1] < 0
    assert _join_extent(scene.audioPath) == pytest.approx(g.OMNIVOICE_JOIN_TARGET_S, abs=0.005)


def test_cannot_shrink_past_the_model_decay(tmp_path):
    """When the decay ALONE exceeds the target, only our zeros may go — the join stays long
    rather than eating signal. Documented limitation, must not silently become a clip."""
    src = _scene_with_join(tmp_path, 0.09, 0.30)   # decay 0.60s total >> 0.22 target
    scene, edits = _run(tmp_path, src)
    after = _join_extent(scene.audioPath)
    assert after > g.OMNIVOICE_JOIN_TARGET_S
    # our zeros were reduced to the configured floor, not below
    a = _read(scene.audioPath)
    amp = g.OMNIVOICE_UNIT_SILENCE_AMP
    runs, run, best = [], None, 0
    for i, v in enumerate(a):
        if -amp <= v <= amp:
            if run is None:
                run = i
        else:
            if run is not None:
                best = max(best, i - run)
            run = None
    assert best / SR >= g.OMNIVOICE_JOIN_MIN_ZEROS_S - 1e-6


def test_leading_silence_is_not_a_join(tmp_path):
    """REGRESSION (video 306 scene 42): the wav opens with 0.052 s of digital zeros at
    0.053 s. A fixed edge-margin guard counted that as a clause join and would have padded
    the START of the scene by the full target. Leading/trailing silence has no speech on one
    side and must be ignored."""
    p = str(tmp_path / "lead.wav")
    _write(p, [0] * int(0.053 * SR) + [0] * int(0.052 * SR) + _tone(int(1.5 * SR)))
    before = _read(p)
    scene, edits = _run(tmp_path, p)
    assert edits == [], f"leading silence treated as a join: {edits}"
    assert scene.audioPath == p
    assert _read(p) == before


def test_trailing_silence_is_not_a_join(tmp_path):
    p = str(tmp_path / "trail.wav")
    _write(p, _tone(int(1.5 * SR)) + [0] * int(0.20 * SR))
    scene, edits = _run(tmp_path, p)
    assert edits == []


def test_shrink_floor_leaves_headroom_for_a_real_join(tmp_path):
    """The shrink floor caps how far a long join can come down. Sized against the real case
    (video 306 scene 25: ~0.09 s of our zeros, ~0.27 s total quiet, 0.22 s target): the floor
    must leave enough removable silence to actually reach target, or the reported bug is only
    half-fixed. At 0.06 it was not — the join stalled at 0.253 s."""
    src = _scene_with_join(tmp_path, 0.093, 0.09)
    assert _join_extent(src) > g.OMNIVOICE_JOIN_TARGET_S
    scene, _ = _run(tmp_path, src)
    assert _join_extent(scene.audioPath) == pytest.approx(
        g.OMNIVOICE_JOIN_TARGET_S, abs=0.005), "shrink floor too coarse to reach target"


def test_noop_when_no_interior_zero_run(tmp_path):
    """A single-clause scene has no inserted beat and must be left completely alone."""
    p = str(tmp_path / "plain.wav")
    _write(p, _tone(SR))
    scene, edits = _run(tmp_path, p)
    assert edits == []
    assert scene.audioPath == p


def test_disabled_by_zero_target(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "OMNIVOICE_JOIN_TARGET_S", 0.0)
    src = _scene_with_join(tmp_path, 0.09, 0.02)
    scene, edits = _run(tmp_path, src)
    assert edits == []
    assert scene.audioPath == src


def test_multiple_joins_all_normalized(tmp_path):
    """A 3-clause scene has two joins; both must land on target."""
    p = str(tmp_path / "two.wav")
    d = int(0.02 * SR)
    seg = (_decay(d) + [0] * int(0.09 * SR) + list(reversed(_decay(d))))
    _write(p, _tone(SR) + seg + _tone(SR, phase=1.0) + seg + _tone(SR, phase=2.0))
    scene, edits = _run(tmp_path, p)
    assert len(edits) == 2
    a = _read(scene.audioPath)
    amp = g.OMNIVOICE_UNIT_SILENCE_AMP
    thr = int(32768 * (10 ** (g.OMNIVOICE_JOIN_QUIET_DB / 20.0)))
    n = len(a)
    quiet, run, spans = None, None, []
    for i, v in enumerate(a):
        if abs(v) < thr:
            if run is None:
                run = i
        else:
            if run is not None and (i - run) > int(0.05 * SR):
                spans.append((i - run) / SR)
            run = None
    assert len(spans) == 2
    for s in spans:
        assert s == pytest.approx(g.OMNIVOICE_JOIN_TARGET_S, abs=0.01)


def test_edit_positions_are_ascending_and_inside_the_wav(tmp_path):
    src = _scene_with_join(tmp_path, 0.09, 0.02)
    scene, edits = _run(tmp_path, src)
    pos = [p for p, _d in edits]
    assert pos == sorted(pos)
    dur = len(_read(scene.audioPath)) / SR
    assert all(0 < p < dur for p in pos)
