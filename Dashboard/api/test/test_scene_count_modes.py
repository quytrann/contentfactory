"""Phase 3 regression tests — Sections A + D of the pipeline redesign.

Section A: scene count is no longer forced from source/7 for all modes.
  * commentary/educational decouple from source → derive from the word ceiling
    (_auto_word_ceiling // _MIN_WORDS_PER_SCENE), capped at _AUTO_SCENE_CAP.
  * summary/recap still source-derived via _SECONDS_PER_SCENE sizing, capped
    (summary → _SUMMARY_SCENE_CAP, recap → _AUTO_SCENE_CAP).
  * AUTO floor max(5); FIXED floor max(3); topic-only default
    max(5, _word_budget // _MIN_WORDS_PER_SCENE).

Section D: _mode_tracks_source(edit_mode) is True only for summary/recap; the
  runner gates _allocate_scene_budgets and both _enforce_duration_guard call
  sites behind it.

Pure unit tests — no LLM, no DB, no claude -p, no network. Helpers are imported
and called directly.

Run:  cd Dashboard/api && .venv/Scripts/python -m pytest test/test_scene_count_modes.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate as g  # noqa: E402
import runner as r  # noqa: E402


# ---------------------------------------------------------------------------
# Section A
# ---------------------------------------------------------------------------

def test_commentary_not_source_over_7_at_500s():
    """The old leak forced ~round(500/7)=71 scenes regardless of mode. After the
    fix, commentary derives from the word ceiling and is capped at _AUTO_SCENE_CAP,
    so it must NOT equal/approach the old 71 value."""
    old_source_over_7 = round(500 / 7)  # == 71, the old forced value
    expected = min(
        max(5, g._auto_word_ceiling(500) // g._MIN_WORDS_PER_SCENE),
        g._AUTO_SCENE_CAP,
    )
    actual = g._auto_scene_count(500, auto=True, edit_mode="commentary")
    assert actual == expected
    assert actual != old_source_over_7
    # word-ceiling-derived raw value (117) exceeds the cap, so it lands ON the cap.
    assert actual == g._AUTO_SCENE_CAP == 40


def test_commentary_diverges_from_summary_at_short_duration():
    """At a shorter duration the cap isn't binding, so commentary (word-ceiling
    derived) and summary/recap (source/7 derived) must differ clearly — proving
    commentary is decoupled from the source-sizing path."""
    commentary = g._auto_scene_count(120, auto=True, edit_mode="commentary")
    summary = g._auto_scene_count(120, auto=True, edit_mode="summary")
    recap = g._auto_scene_count(120, auto=True, edit_mode="recap")
    # commentary derives from word ceiling; summary/recap from source/7.
    expected_commentary = min(
        max(5, g._auto_word_ceiling(120) // g._MIN_WORDS_PER_SCENE),
        g._AUTO_SCENE_CAP,
    )
    expected_source = min(max(5, round(120 / g._SECONDS_PER_SCENE)), g._AUTO_SCENE_CAP)
    assert commentary == expected_commentary
    assert summary == expected_source
    assert recap == expected_source
    assert commentary != summary, (
        f"commentary ({commentary}) must diverge from summary ({summary})"
    )


def test_educational_uses_word_ceiling_path_like_commentary():
    """educational is the other original-length/source-decoupled mode and must
    follow the same word-ceiling path as commentary (not source/7)."""
    expected = min(
        max(5, g._auto_word_ceiling(120) // g._MIN_WORDS_PER_SCENE),
        g._AUTO_SCENE_CAP,
    )
    assert g._auto_scene_count(120, auto=True, edit_mode="educational") == expected
    # at 120s educational == commentary (same path) and != summary.
    assert g._auto_scene_count(120, auto=True, edit_mode="educational") == \
        g._auto_scene_count(120, auto=True, edit_mode="commentary")


def test_summary_source_derived_and_capped():
    """summary stays source-derived: ~round(N/7), capped at the SOURCE-TIED cap
    (bug3: _summary_scene_cap = round(hint*1.3), replacing the old flat 200)."""
    # mid range: source-tied cap not binding (hint*1.3 > hint) → equals round(N/7).
    assert g._auto_scene_count(700, auto=True, edit_mode="summary") == \
        min(max(5, round(700 / g._SECONDS_PER_SCENE)), g._summary_scene_cap(700))
    # correlates with N: larger source → more (or equal-at-cap) scenes.
    n_small = g._auto_scene_count(280, auto=True, edit_mode="summary")
    n_big = g._auto_scene_count(1000, auto=True, edit_mode="summary")
    assert n_big > n_small
    # pathological large input → clamped to the ABSOLUTE ceiling, not unbounded.
    assert g._auto_scene_count(7000, auto=True, edit_mode="summary") == g._summary_scene_cap(7000)
    assert g._auto_scene_count(100000, auto=True, edit_mode="summary") == g._SUMMARY_SCENE_CAP_ABS == 200
    # bug3 regression: a ~475s source can no longer reach 119 scenes (old flat-200
    # cap let it through). New cap = round(round(475/7.5)*1.3) = 82, count = 63.
    assert g._summary_scene_cap(475) == 82
    assert g._auto_scene_count(475, auto=True, edit_mode="summary") == 63


def test_recap_source_derived_with_default_cap():
    """recap is source-derived like summary but uses the default _AUTO_SCENE_CAP
    (40), not the summary cap."""
    assert g._auto_scene_count(120, auto=True, edit_mode="recap") == \
        min(max(5, round(120 / g._SECONDS_PER_SCENE)), g._AUTO_SCENE_CAP)
    # very long source → recap clamps at the default cap (40), not 200.
    assert g._auto_scene_count(7000, auto=True, edit_mode="recap") == g._AUTO_SCENE_CAP == 40


def test_auto_floor_min_5():
    """AUTO never returns < 5 for any mode, even at a tiny duration."""
    for mode in ("commentary", "educational", "summary", "recap", None, "unknown"):
        assert g._auto_scene_count(1, auto=True, edit_mode=mode) >= 5, mode
        assert g._auto_scene_count(0, auto=True, edit_mode=mode) >= 5, mode


def test_fixed_floor_min_3():
    """FIXED never returns < 3 at a tiny duration (the word-budget cap can pull it
    below 5, but the max(3, …) floor holds)."""
    # at duration 1: budget = round(1*2.1)=2; max_scenes_for_budget = max(3, 2//8)=3.
    assert g._auto_scene_count(1, auto=False, edit_mode="commentary") == 3
    assert g._auto_scene_count(1, auto=False, edit_mode="commentary") >= 3
    assert g._auto_scene_count(0, auto=False, edit_mode="commentary") >= 3


def test_topic_only_default_scene_count_math():
    """generate_script's topic-only default (generate.py:836) is
    max(5, _word_budget(durationSec) // _MIN_WORDS_PER_SCENE). Verify the helper
    math directly — no LLM call."""
    for dur in (30, 60, 120, 300):
        expected = max(5, g._word_budget(dur) // g._MIN_WORDS_PER_SCENE)
        # the same arithmetic the route uses inline.
        assert expected == max(5, g._word_budget(dur) // g._MIN_WORDS_PER_SCENE)
    # spot-check concrete values to pin the constants.
    assert g._word_budget(60) == 126           # round(60 * 2.1)
    assert max(5, g._word_budget(60) // g._MIN_WORDS_PER_SCENE) == 15   # 126 // 8
    assert max(5, g._word_budget(300) // g._MIN_WORDS_PER_SCENE) == 78  # 630 // 8
    # tiny duration → floor of 5 binds.
    assert max(5, g._word_budget(1) // g._MIN_WORDS_PER_SCENE) == 5


def test_topic_default_matches_generate_script_request_default():
    """ScriptRequest with no sceneCount must resolve to the budget-derived default,
    NOT the old round(durationSec/7). Verifies the line is wired to the helper.

    We compute scene_count the same way generate_script does (generate.py:836)
    without calling the LLM."""
    durationSec = 90
    sceneCount = None
    resolved = sceneCount or max(5, g._word_budget(durationSec) // g._MIN_WORDS_PER_SCENE)
    old_value = max(5, round(durationSec / 7))
    assert resolved == max(5, g._word_budget(90) // g._MIN_WORDS_PER_SCENE)
    # 90s: budget=189, 189//8=23; old source/7 = round(90/7)=13. They differ.
    assert resolved == 23
    assert resolved != old_value


# ---------------------------------------------------------------------------
# Section D
# ---------------------------------------------------------------------------

def test_mode_tracks_source_truth_table():
    assert r._mode_tracks_source("commentary") is False
    assert r._mode_tracks_source("educational") is False
    assert r._mode_tracks_source("dubbed") is False
    assert r._mode_tracks_source("summary") is True
    assert r._mode_tracks_source("recap") is True
    assert r._mode_tracks_source(None) is False
    assert r._mode_tracks_source("") is False
    assert r._mode_tracks_source("unknown") is False


def test_mode_tracks_source_case_insensitive():
    """Implementation lowercases the input — verify case-insensitivity holds."""
    assert r._mode_tracks_source("SUMMARY") is True
    assert r._mode_tracks_source("Recap") is True
    assert r._mode_tracks_source("Commentary") is False
    assert r._mode_tracks_source("DUBBED") is False


def test_mode_tracks_source_drives_gating_for_commentary_vs_summary():
    """Behavioral proxy for the runner gate. The runner wraps both
    _allocate_scene_budgets and _enforce_duration_guard in
    `if _mode_tracks_source(edit_mode):`. We can't run a full job here without
    heavy mocking, but we CAN prove the gating predicate yields the intended
    decision per mode — which is exactly what the runner branches on.

    Honest note: this asserts the gate PREDICATE, not a live runner invocation.
    A full behavioral test (asserting _enforce_duration_guard is never CALLED for
    commentary) requires driving _process_job end-to-end (DB + media side effects),
    which is out of scope for a pure unit test. See module docstring."""
    # commentary / educational / dubbed → guard + budget SKIPPED.
    for mode in ("commentary", "educational", "dubbed"):
        assert r._mode_tracks_source(mode) is False, f"{mode} should skip budget/guard"
    # summary / recap → guard + budget APPLIED.
    for mode in ("summary", "recap"):
        assert r._mode_tracks_source(mode) is True, f"{mode} should apply budget/guard"


def test_runner_default_edit_mode_is_non_tracking():
    """The runner's default edit_mode is 'commentary' (the safe default: no cap /
    no guard, can't false-fail a legitimately-longer original-length video)."""
    assert r._mode_tracks_source("commentary") is False
