"""Unit tests for the spoken-syllable estimator (_spoken_weight / _vi_number_syllables /
_pace_syllables) in generate.py.

Pure tests: plain string tokens in, integer syllable counts out — no audio, no ffmpeg,
no DB. This function is SHARED and load-bearing:

  * _scene_pace_ms_per_syl  -> F5/VieNeu's _auto_target_pace AND OmniVoice's
    _omnivoice_pace_factor (how hard a clause gets time-stretched);
  * _scene_wall_ms_per_syl  -> the OmniVoice perceived-pace ceiling;
  * _aligned_caption_words  -> karaoke caption interpolation weights (all engines).

A wrong count here silently mis-times audio or captions, so the expectations below are
written against the ACTUAL Vietnamese spoken form (the word count of how a human reads
the token aloud), not against the old formula.

Run:  cd Dashboard/api && .venv/Scripts/python -m pytest test/test_spoken_weight.py -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate as g  # noqa: E402


# --------------------------------------------------------------------------------------
# Numbers: magnitude reading ("bốn trăm hai mươi bảy"), NOT digit-by-digit.
# `spoken` is the Vietnamese reading; the expected count is its word count.
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("n, expected, spoken", [
    (0, 1, "không"),
    (1, 1, "một"),
    (5, 1, "năm"),
    (10, 1, "mười"),
    (11, 2, "mười một"),
    (15, 2, "mười lăm"),
    (19, 2, "mười chín"),
    (20, 2, "hai mươi"),
    (21, 3, "hai mươi mốt"),
    (25, 3, "hai mươi lăm"),
    (30, 2, "ba mươi"),
    (99, 3, "chín mươi chín"),
    (100, 2, "một trăm"),
    (101, 4, "một trăm linh một"),
    (110, 3, "một trăm mười"),
    (115, 4, "một trăm mười lăm"),
    (200, 2, "hai trăm"),
    (205, 4, "hai trăm linh năm"),
    (427, 5, "bốn trăm hai mươi bảy"),
    (999, 5, "chín trăm chín mươi chín"),
    (1000, 2, "một nghìn"),
    (1500, 4, "một nghìn năm trăm"),
    (1000000, 2, "một triệu"),
])
def test_vi_number_syllables(n, expected, spoken):
    got = g._vi_number_syllables(n)
    assert got == expected, f"{n} reads '{spoken}' = {expected} syllable(s), got {got}"


def test_number_syllables_never_zero():
    for n in range(0, 2000):
        assert g._vi_number_syllables(n) >= 1


# --------------------------------------------------------------------------------------
# Token-level weights, in the shape whisper actually emits them (trailing punctuation).
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("tok, expected, why", [
    ("427", 5, "bốn trăm hai mươi bảy — job 308 scene 4"),
    ("427,", 5, "trailing comma must not change the count"),
    ("100", 2, "một trăm"),
    ("100.", 2, "trailing period must not change the count"),
    # YEARS stay digit-by-digit: the TTS text-prep expands 20xx into "hai không hai tư"
    # before synthesis, so the token really was read one digit at a time.
    ("2024", 4, "year: hai không hai tư (digit-by-digit)"),
    ("2024.", 4, "year with punctuation"),
    ("2022,", 4, "year with punctuation"),
    # ONLY 20xx is digit-exploded by the text-prep (`\b(20\d{2})\b`). A 19xx year is read
    # by magnitude like any other number — video 299 scene 11 says "từ năm 1736".
    ("1999", 7, "một nghìn chín trăm chín mươi chín — NOT digit-by-digit"),
    ("1736", 7, "một nghìn bảy trăm ba mươi sáu"),
    # Alphanumeric part codes: letters + magnitude of the digit run.
    ("H100", 3, "hát một trăm"),
    ("B200", 3, "bê hai trăm"),
    # Non-digit branches must be untouched by the number fix.
    ("GPT", 3, "acronym: one syllable per letter"),
    ("AI", 2, "acronym"),
    ("ChatGPT", 3, "mixed-case: len//2"),
])
def test_spoken_weight_tokens(tok, expected, why):
    assert g._spoken_weight(tok) == expected, f"{tok!r}: {why}"


# --------------------------------------------------------------------------------------
# English silent final 'e'.
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("tok, expected", [
    ("runtime", 2),     # run-time, not run-ti-me
    ("file", 1),
    ("code", 1),
    ("time", 1),
    ("while", 1),
    ("made", 1),
    ("style", 1),
    ("pipeline", 3),
    ("database", 3),
    ("module", 2),
    ("release", 2),
    # Syllabic "-le" (consonant + le) keeps its final syllable.
    ("table", 2),
    ("little", 2),
    # Vowel before the final e — nothing to remove.
    ("free", 1),
    ("bye", 1),
])
def test_silent_final_e(tok, expected):
    assert g._spoken_weight(tok) == expected


@pytest.mark.parametrize("tok", ["the", "xe", "che", "nghe", "me", "de"])
def test_single_syllable_tokens_ending_in_e_stay_one(tok):
    """The silent-e rule must never zero out a token whose ONLY vowel group is that 'e'.
    Protects Vietnamese one-syllable words (xe, che, nghe) and English 'the'."""
    assert g._spoken_weight(tok) == 1


def test_engineering_still_four():
    """REGRESSION GUARD (job-v109). _spoken_weight exists because char-length weighting
    made 'engineering' steal ~1 s of the caption timeline; its documented correct value is
    the 4-syllable vowel-group count. It does not end in 'e', so the silent-e rule must
    leave it alone."""
    assert g._spoken_weight("engineering") == 4


# --------------------------------------------------------------------------------------
# Vietnamese narration must be completely unaffected by both fixes.
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("tok, expected", [
    ("đoạn", 1), ("mã", 1), ("dài", 1), ("dòng", 1), ("là", 1), ("sinh", 1),
    ("ra", 1), ("hơn", 1), ("trăm", 1), ("của", 1), ("những", 1), ("người", 1),
    ("nghìn", 1), ("triệu", 1), ("mười", 1),
])
def test_vietnamese_tokens_unchanged(tok, expected):
    assert g._spoken_weight(tok) == expected


def test_accented_e_is_not_treated_as_silent():
    """Only the BARE ascii 'e' is mute in English. Vietnamese ê/è/é/ẻ/ẽ/ẹ are real
    vowels and must never be stripped."""
    for tok in ("kê", "tè", "mé", "trẻ", "lẽ", "mẹ"):
        assert g._spoken_weight(tok) == 1
