"""Bug3 perf-fix regression tests — word-ceiling enforcement + no VO hard-trim.

Context (proven by prior investigation): summary job 14 produced an 8484-word
script (9.5x the ~892-word ceiling for a ~475s source) and 119 scenes. The runner
then HARD-TRIMMED every scene's VO (_cap_voiceover), chopping ~78% of each sentence
(faded mid-sentence), and the over-long script tripled TTS chunks (91->256) → 54-min
render.

The fix (owner-approved):
  1a) The source-derived word ceiling is injected into the AUTO prompt AND enforced
      after generation: an over-budget summary/recap script FAILS the job (Vietnamese
      message) instead of being trimmed/sped up.
  1b) _cap_voiceover (the fade+atrim hard-trim) is no longer applied on the assemble
      path; _allocate_scene_budgets is no longer called, so no scene carries a
      `time_cap` and _scene_clip_duration returns the FULL VO duration.
  3)  _SUMMARY_SCENE_CAP is replaced by a SOURCE-TIED cap (_summary_scene_cap =
      round(hint*1.3)), so a ~475s source can't reach 119 scenes.

These tests pin those behaviors WITHOUT spawning `claude -p`, touching the DB, or
running ffmpeg. The LLM touchpoint generate._gen_footage_scenes is monkeypatched to
return canned scene lists with a known total narration word count.

Run: cd Dashboard/api && .venv/Scripts/python.exe -m pytest test/test_word_ceiling_bug3.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate  # noqa: E402
import runner  # noqa: E402
from generate import TransformFootageRequest, TimedSegment  # noqa: E402
from fastapi import HTTPException  # noqa: E402

# A 475s-ish source window (the job-14 case). ceiling(475) == 892.
WINDOW = 475.0


def _scenes_with_words(total_words: int, n_scenes: int = 5) -> list:
    """Build `n_scenes` footage scenes whose narration sums to exactly `total_words`
    whitespace-delimited words, all source ranges inside [0, WINDOW] (so
    _clamp_footage_scenes / keep-ratio don't drop or alter them). Each scene covers an
    equal slice of the window so coverage lands well inside the summary band (76-90%)
    and the keep-ratio loop does NOT regen (which would re-call our spy)."""
    per = max(1, total_words // n_scenes)
    counts = [per] * n_scenes
    counts[-1] += total_words - per * n_scenes  # remainder onto the last scene
    span = WINDOW / n_scenes
    scenes = []
    for i, c in enumerate(counts):
        st = i * span
        # cover ~85% of each slice → overall ratio ~0.85, inside summary band 0.76-0.90
        en = st + span * 0.85
        scenes.append({
            "scene": i + 1,
            "narration": " ".join(["từ"] * max(0, c)),
            "sourceStart": round(st, 2),
            "sourceEnd": round(en, 2),
        })
    return scenes


def _make_req(mode: str, auto: bool = True) -> TransformFootageRequest:
    return TransformFootageRequest(
        segments=[TimedSegment(start=0.0, end=WINDOW, text="source transcript")],
        editMode=mode,
        durationSec=int(WINDOW),
        sceneCount=5,          # single batch → exactly one _gen_footage_scenes call
        windowSec=WINDOW,
        auto=auto,
    )


# ---------------------------------------------------------------------------
# (i) over-budget AUTO summary script → job FAILS with the Vietnamese message,
#     scenes are NOT truncated.
# ---------------------------------------------------------------------------
def test_over_budget_summary_fails_with_vietnamese_message(monkeypatch):
    ceiling = generate._auto_word_ceiling(WINDOW)          # 892
    limit = int(ceiling * generate._WORD_CEILING_TOLERANCE)  # ~1025
    over = limit + 500                                      # comfortably over the limit
    monkeypatch.setattr(generate, "_gen_footage_scenes",
                        lambda *a, **k: _scenes_with_words(over))

    with pytest.raises(HTTPException) as ei:
        generate.generate_script_footage(_make_req("summary"))

    assert ei.value.status_code == 422
    # Vietnamese failure message (owner-mandated wording), NOT a silent truncation.
    assert "vượt giới hạn từ" in ei.value.detail
    assert "rút ngắn kịch bản" in ei.value.detail
    # The actual count and the ceiling both surface for diagnosis.
    assert str(ceiling) in ei.value.detail


def test_extreme_blowup_like_job14_fails(monkeypatch):
    """The literal job-14 magnitude (8484 words vs 892 ceiling) must FAIL."""
    monkeypatch.setattr(generate, "_gen_footage_scenes",
                        lambda *a, **k: _scenes_with_words(8484, n_scenes=20))
    with pytest.raises(HTTPException) as ei:
        generate.generate_script_footage(_make_req("summary"))
    assert ei.value.status_code == 422
    assert "8484" in ei.value.detail


# ---------------------------------------------------------------------------
# (ii) in-budget AUTO summary script → NO failure, scenes returned unchanged.
# ---------------------------------------------------------------------------
def test_in_budget_summary_passes(monkeypatch):
    ceiling = generate._auto_word_ceiling(WINDOW)  # 892
    in_budget = ceiling - 100                       # comfortably under the ceiling
    monkeypatch.setattr(generate, "_gen_footage_scenes",
                        lambda *a, **k: _scenes_with_words(in_budget))

    out = generate.generate_script_footage(_make_req("summary"))

    assert out["editMode"] == "summary"
    # Returned, not raised; word count preserved (no truncation).
    assert generate._count_narration_words(out["scenes"]) == in_budget


def test_at_word_ceiling_passes_but_duration_gate_binds_above(monkeypatch):
    """A script AT the word ceiling passes BOTH the word-ceiling check and the new
    pre-TTS duration gate (its estimated VO is comfortably under source). But the
    word-ceiling's 1.15x TOLERANCE no longer makes the UPPER boundary pass on its own:
    the zero-tolerance duration gate (added 2026-06-27) is the stricter, more accurate
    source-fit check, so a script at 1.15x the ceiling — whose estimated VO would EXCEED
    the source — is correctly rejected. This pins the intended precedence: the duration
    gate binds whenever the word-tolerance would otherwise admit an over-source script."""
    import math
    ceiling = generate._auto_word_ceiling(WINDOW)
    # (i) AT the ceiling → passes both gates.
    monkeypatch.setattr(generate, "_gen_footage_scenes",
                        lambda *a, **k: _scenes_with_words(ceiling))
    out = generate.generate_script_footage(_make_req("summary"))  # must NOT raise
    assert generate._count_narration_words(out["scenes"]) == ceiling
    # Sanity: the ceiling's estimated VO really is under source.
    est = ceiling / generate._VI_WORDS_PER_SEC + generate._CREDIT_SLATE_SEC
    assert est < WINDOW, (est, WINDOW)

    # (ii) AT 1.15x the ceiling → the word-ceiling tolerance would admit it, but its
    # estimated VO exceeds source → the duration gate FAILS it (zero tolerance).
    limit = math.floor(ceiling * generate._WORD_CEILING_TOLERANCE)
    est_over = limit / generate._VI_WORDS_PER_SEC + generate._CREDIT_SLATE_SEC
    assert est_over >= WINDOW, "premise: 1.15x ceiling must estimate OVER source"
    monkeypatch.setattr(generate, "_gen_footage_scenes",
                        lambda *a, **k: _scenes_with_words(limit))
    with pytest.raises(HTTPException) as ei:
        generate.generate_script_footage(_make_req("summary"))
    assert ei.value.status_code == 422
    assert "dài hơn hoặc bằng video gốc" in ei.value.detail


# ---------------------------------------------------------------------------
# Mode gating (Task 1c): commentary/educational are NOT source-tracking → the word
# ceiling must NOT be enforced even when way over (they may run original-length).
# ---------------------------------------------------------------------------
def test_commentary_over_budget_does_not_fail(monkeypatch):
    monkeypatch.setattr(generate, "_gen_footage_scenes",
                        lambda *a, **k: _scenes_with_words(8484, n_scenes=20))
    # commentary has NO keep-ratio band and NO word ceiling → returns, never raises.
    out = generate.generate_script_footage(_make_req("commentary"))
    assert generate._count_narration_words(out["scenes"]) == 8484


def test_fixed_mode_extreme_blowup_fails_fast(monkeypatch):
    """FIXED-mode FAST-FAIL (mechanism 3): a FIXED-target footage script whose total
    narration grossly exceeds what the SOURCE span can physically hold must FAIL at
    script-gen (422, Vietnamese message) instead of surfacing ~50 min later at the
    assembly duration gate. This is the multi-batch budget-overshoot backstop — the
    output VO must still fit under the source regardless of edit mode, so the FIXED
    path now applies the SAME source-length ceiling the AUTO path uses (via
    _enforce_fixed_source_fit). Replaces the old test that asserted FIXED was exempt:
    that exemption is exactly what let job-45's 2661-word overshoot slip through to a
    99% assembly failure."""
    monkeypatch.setattr(generate, "_gen_footage_scenes",
                        lambda *a, **k: _scenes_with_words(8484, n_scenes=20))
    with pytest.raises(HTTPException) as ei:
        generate.generate_script_footage(_make_req("summary", auto=False))
    assert ei.value.status_code == 422
    assert "vượt giới hạn từ" in ei.value.detail
    assert "rút ngắn kịch bản" in ei.value.detail


def test_fixed_mode_in_budget_passes(monkeypatch):
    """A FIXED-target footage script that fits the source-length ceiling is returned
    unchanged — the fast-fail only trips on a real overshoot, not on a normal script."""
    ceiling = generate._auto_word_ceiling(WINDOW)  # 892 for 475s
    in_budget = ceiling - 100
    monkeypatch.setattr(generate, "_gen_footage_scenes",
                        lambda *a, **k: _scenes_with_words(in_budget))
    out = generate.generate_script_footage(_make_req("summary", auto=False))
    assert generate._count_narration_words(out["scenes"]) == in_budget


def test_fixed_source_fit_helper_unit():
    """Direct unit of _enforce_fixed_source_fit: over → raise (mode-agnostic),
    under → no-op, non-positive source → no-op."""
    ceiling = generate._auto_word_ceiling(WINDOW)
    over = _scenes_with_words(int(ceiling * 2))
    under = _scenes_with_words(ceiling // 2)
    with pytest.raises(HTTPException):
        generate._enforce_fixed_source_fit(over, WINDOW)
    # under → no raise
    generate._enforce_fixed_source_fit(under, WINDOW)
    # non-positive source → no raise (no honest ceiling)
    generate._enforce_fixed_source_fit(over, 0)


def test_enforce_helper_unit():
    """Direct unit of _enforce_word_ceiling: over → raise, under → no-op,
    non-tracking mode → no-op, non-positive source → no-op."""
    ceiling = generate._auto_word_ceiling(WINDOW)
    over = _scenes_with_words(int(ceiling * 2))
    under = _scenes_with_words(ceiling // 2)
    # over + tracking mode → raises
    with pytest.raises(HTTPException):
        generate._enforce_word_ceiling("summary", over, WINDOW)
    with pytest.raises(HTTPException):
        generate._enforce_word_ceiling("recap", over, WINDOW)
    # under → no raise
    generate._enforce_word_ceiling("summary", under, WINDOW)
    # non-tracking mode → no raise even if over
    generate._enforce_word_ceiling("commentary", over, WINDOW)
    generate._enforce_word_ceiling("educational", over, WINDOW)
    # non-positive source → no raise (no honest ceiling)
    generate._enforce_word_ceiling("summary", over, 0)


# ---------------------------------------------------------------------------
# (iii) summary scene count is CLAMPED to the new source-tied cap for a 475s source.
# ---------------------------------------------------------------------------
def test_summary_scene_count_clamped_for_475s():
    # New cap = round(hint*1.3); hint = round(475/7.5)=63 → cap = round(81.9)=82.
    cap = generate._summary_scene_cap(475)
    assert cap == 82, cap
    # The old flat cap (200) would have let job-14's 119 scenes through; the new cap
    # (82) is BELOW 119, so 119 is no longer reachable for a 475s source.
    assert cap < 119
    # AUTO scene count for a 475s summary is min(hint, cap) = min(63, 82) = 63.
    n = generate._auto_scene_count(475, auto=True, edit_mode="summary")
    assert n == 63, n
    assert n <= cap


def test_summary_scene_cap_clamps_pathological_input():
    """A pathologically large source is clamped to the absolute ceiling, not unbounded."""
    cap = generate._summary_scene_cap(100000)
    assert cap == generate._SUMMARY_SCENE_CAP_ABS == 200
    # AUTO scene count for such a source lands on the cap.
    n = generate._auto_scene_count(100000, auto=True, edit_mode="summary")
    assert n == 200


def test_summary_scene_cap_floor_for_short_source():
    """A very short source still gets a usable floor cap."""
    cap = generate._summary_scene_cap(10)
    assert cap == generate._SUMMARY_SCENE_CAP_FLOOR == 10
    cap0 = generate._summary_scene_cap(0)
    assert cap0 == generate._SUMMARY_SCENE_CAP_FLOOR


def test_summary_cap_lower_than_old_flat_200_in_normal_range():
    """For any non-pathological source the new cap is below the old flat 200."""
    for dur in (120, 300, 475, 700, 1000):
        assert generate._summary_scene_cap(dur) < 200, dur


# ---------------------------------------------------------------------------
# (iv) _cap_voiceover is NO LONGER invoked on the assemble path, and
#      _scene_clip_duration returns the FULL VO duration (no time_cap clamp).
# ---------------------------------------------------------------------------
def test_scene_clip_duration_returns_full_vo_without_cap():
    """With no `time_cap` (the new default — _allocate_scene_budgets is not called),
    _scene_clip_duration returns the raw VO duration unchanged (no truncation)."""
    scene = {"scene": 1, "narration": "x"}            # no time_cap key
    audio = {"durationS": 12.34}
    assert runner._scene_clip_duration(scene, audio) == 12.34
    # Even a long VO is returned in full (the old min(vo, cap) clamp is gone).
    assert runner._scene_clip_duration({"scene": 2}, {"durationS": 99.0}) == 99.0


def test_cap_voiceover_not_referenced_on_assemble_path():
    """Static proof that the runner's assemble/TTS section no longer CALLS
    _cap_voiceover or _allocate_scene_budgets (they may remain DEFINED). We scan the
    runner source and assert neither symbol is INVOKED anywhere (only def/comment
    lines may mention them). This guards against a future re-introduction of the
    hard-trim on the assemble path."""
    src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runner.py")
    with open(src_path, encoding="utf-8") as fh:
        lines = fh.readlines()
    for fn in ("_cap_voiceover", "_allocate_scene_budgets"):
        call = fn + "("
        offenders = []
        for i, line in enumerate(lines, 1):
            if call not in line:
                continue
            stripped = line.lstrip()
            # Allow the function DEFINITION line and pure comment lines.
            if stripped.startswith("def " + fn):
                continue
            if stripped.startswith("#"):
                continue
            offenders.append((i, line.rstrip()))
        assert not offenders, (
            f"{fn} must not be CALLED on the assemble path (bug3); found: {offenders}"
        )


def test_scene_clip_duration_none_passthrough():
    """Unknown VO duration still passes through as None (unchanged contract)."""
    assert runner._scene_clip_duration({"scene": 1}, {"durationS": None}) is None
    assert runner._scene_clip_duration({"scene": 1}, {}) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-q"]))
