"""Unit tests for tts_worker._count_syllables / _vi_number_syllables.

This is the WORKER-side twin of test_spoken_weight.py. The two implementations are
deliberate duplicates (the worker runs in cf-venv as a standalone script and must not
import the FastAPI module), so these tests exist to keep them from drifting: the shared
cases are asserted against BOTH modules in test_counters_agree_with_generate below.

_count_syllables is audio-affecting — it feeds F5's loanword rush detection
(_measure_loanwords), the vi_corrections best-of-N verifier (_vi_verify_term), and
OmniVoice's clause merge / pace balance (omnivoice_worker._spoken_syllables).

Run:  cd Dashboard/api && .venv/Scripts/python -m pytest test/test_tts_worker_syllables.py -q
"""

import os
import sys

import pytest

_API = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _API)
sys.path.insert(0, os.path.join(_API, "workers"))

import tts_worker as tw  # noqa: E402


# --------------------------------------------------------------------------------------
# Numbers — magnitude reading. Previously EVERY numeral scored 1 (no vowel groups).
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("n, expected, spoken", [
    (0, 1, "không"), (1, 1, "một"), (10, 1, "mười"), (15, 2, "mười lăm"),
    (20, 2, "hai mươi"), (25, 3, "hai mươi lăm"), (99, 3, "chín mươi chín"),
    (100, 2, "một trăm"), (101, 4, "một trăm linh một"), (108, 4, "một trăm linh tám"),
    (110, 3, "một trăm mười"), (205, 4, "hai trăm linh năm"),
    (427, 5, "bốn trăm hai mươi bảy"), (999, 5, "chín trăm chín mươi chín"),
    (1000, 2, "một nghìn"), (1736, 7, "một nghìn bảy trăm ba mươi sáu"),
])
def test_vi_number_syllables(n, expected, spoken):
    assert tw._vi_number_syllables(n) == expected, f"{n} reads '{spoken}'"


@pytest.mark.parametrize("tok, expected, why", [
    ("427", 5, "video 299 scene 4"),
    ("427,", 5, "trailing punctuation ignored"),
    ("108", 4, "video 299 scene 36"),
    ("20.000", 3, "hai mươi nghìn — thousands separator splits into runs"),
    ("2024", 4, "20xx year: digit-by-digit (text-prep expands it that way)"),
    ("2023.", 4, "20xx year with punctuation"),
    ("1736", 7, "NOT a 20xx year -> magnitude (video 299 scene 11)"),
    ("H100", 3, "hát một trăm"),
])
def test_count_syllables_numeric_tokens(tok, expected, why):
    assert tw._count_syllables(tok) == expected, f"{tok!r}: {why}"


# --------------------------------------------------------------------------------------
# English silent final 'e'.
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("tok, expected", [
    ("runtime", 2), ("file", 1), ("code", 1), ("time", 1), ("node", 1),
    ("clone", 1), ("feature", 2), ("website", 2), ("pipeline", 3),
    ("table", 2), ("little", 2),      # syllabic -le keeps its syllable
    ("free", 1), ("bye", 1),          # vowel before the final e
])
def test_silent_final_e(tok, expected):
    assert tw._count_syllables(tok) == expected


@pytest.mark.parametrize("tok", ["the", "xe", "che", "nghe", "me"])
def test_one_syllable_tokens_ending_in_e_stay_one(tok):
    assert tw._count_syllables(tok) == 1


def test_engineering_still_four():
    """REGRESSION GUARD: the documented job-v109 value. Does not end in 'e'."""
    assert tw._count_syllables("engineering") == 4


@pytest.mark.parametrize("tok", ["kê", "tè", "mé", "trẻ", "lẽ", "mẹ"])
def test_accented_e_never_stripped(tok):
    assert tw._count_syllables(tok) == 1


@pytest.mark.parametrize("tok, expected", [
    ("đoạn", 1), ("mã", 1), ("dài", 1), ("dòng", 1), ("trăm", 1),
    ("nghìn", 1), ("triệu", 1), ("người", 1), ("những", 1),
])
def test_vietnamese_unchanged(tok, expected):
    assert tw._count_syllables(tok) == expected


# --------------------------------------------------------------------------------------
# Anti-drift: the duplicated implementations must agree.
# --------------------------------------------------------------------------------------
def test_counters_agree_with_generate():
    """tts_worker._vi_number_syllables must match generate._vi_number_syllables exactly,
    and _count_syllables must match generate._spoken_weight on every token where the two
    are defined to agree (i.e. anything that is not an all-caps acronym or a mixed-case
    compound — those branches live in _spoken_weight / _spoken_syllables, not here)."""
    import generate as g

    for n in range(0, 3000):
        assert tw._vi_number_syllables(n) == g._vi_number_syllables(n), f"number {n}"

    tokens = [
        "427", "427,", "100", "108", "20.000", "2024", "2023.", "1736", "H100",
        "runtime", "file", "code", "node", "clone", "feature", "website", "pipeline",
        "table", "little", "free", "bye", "the", "xe", "che", "nghe", "engineering",
        "đoạn", "mã", "dài", "dòng", "trăm", "nghìn", "triệu", "người", "kê", "mẹ",
    ]
    for t in tokens:
        assert tw._count_syllables(t) == g._spoken_weight(t), (
            f"{t!r}: worker={tw._count_syllables(t)} api={g._spoken_weight(t)}")
