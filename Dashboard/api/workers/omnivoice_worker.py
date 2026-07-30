"""OmniVoice TTS engine — k2-fsa/OmniVoice zero-shot voice clone.

Runs inside cf-venv, dispatched from tts_worker.py's `main()`:

    from omnivoice_worker import run_omnivoice
    results = run_omnivoice(cfg, items, out_dir, progress)

WHY THIS IS A SEPARATE MODULE (owner decision, 2026-07-31)
-----------------------------------------------------------
OmniVoice was originally added by widening functions that F5-TTS and VieNeu also
execute (the word_improve parser/compiler, the loanword set, the assembly pace gate).
That leaked OmniVoice-only pronunciation rows into F5's regex and loanword set and
changed which chunks F5 re-drew. Everything OmniVoice needs now lives HERE, so no
OmniVoice tuning can ever alter an F5/VieNeu render again.

The rule going forward: **never add an `if engine == "omnivoice"` branch to a function
that F5 or VieNeu also calls.** Duplicate the logic into this module instead. Only
genuinely engine-agnostic primitives (ffmpeg wrappers, resampling, syllable counting,
the canonical sample rate) are imported from tts_worker — none of them branch on engine.

OmniVoice contract:
  • clone-only (requires refAudio); RAW Vietnamese in — no F5 say_as respelling and no
    vi_corrections. Only the dedicated `say_as_omnivoice` column applies, and a term
    without one is spoken RAW (NO fallback to F5's say_as).
  • ref_text MUST describe the EXACT prepped clip (see _resolve_omni_ref_text); it uses
    its OWN `_reftext_omni/` sidecar dir so it can never collide with F5's `_reftext/`.
  • emits 24 kHz and NO timestamps → each scene is resampled to CANONICAL_SR and
    downstream faster-whisper handles caption/scene alignment exactly as for F5.
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path

# Shared, ENGINE-AGNOSTIC primitives. None of these branch on the engine; they are
# imported (not duplicated) on purpose so a genuine ffmpeg/resample fix is not
# maintained twice. Anything that IS engine-specific is defined below instead.
from tts_worker import (
    CANONICAL_SR,
    WORD_IMPROVE_MD,
    _PRON_WORDCHAR,
    _YEAR_DIGIT_VN,
    _atempo_stretch_np,
    _concat_wavs_48k,
    _count_syllables,
    _enable_cuda_dlls,
    _expand_decimal_point,
    _expand_vn_dates,
    _ffmpeg_bin,
    _is_acronym_term,
    _normalize_thousands_sep,
    _probe_duration,
    _ref_fingerprint,
    _resample_to_canonical,
    _split_sentences_for_ps,
    log,
)

# ---------------------------------------------------------------------------
# KNOBS (all OmniVoice-only; dedicated names so tuning these can never move F5)
# ---------------------------------------------------------------------------

# OmniVoice (k2-fsa/OmniVoice) — 0.6B diffusion-LM zero-shot clone, 24 kHz native,
# genuine Vietnamese. Runtime VRAM ~2.1-2.4 GB (fits 8 GB alongside sequential SDXL).
# Downloaded once to the HF cache (already on E:); ~3.1 GB repo (LM + Higgs audio
# tokenizer). Emits NO timestamps → downstream faster-whisper alignment is unchanged.
#
# Phase-2 contract (media-engineer): RAW Vietnamese is fed to OmniVoice — NO
# word_improve say_as / vi_corrections respelling (Phase-1 A/B showed it needs far less
# than F5; only the dedicated say_as_omnivoice column applies). The reference transcript
# MUST match the EXACT ref clip passed (see _resolve_omni_ref_text): F5's 6 s-capped
# _reftext sidecar is INVALID for OmniVoice and caused leading hallucination in Phase-1
# testing. OmniVoice ref/text agreement is therefore resolved independently.
OMNIVOICE_MODEL = os.getenv("OMNIVOICE_MODEL", "k2-fsa/OmniVoice")
OMNIVOICE_NUM_STEP = int(os.getenv("OMNIVOICE_NUM_STEP", "28"))
OMNIVOICE_GUIDANCE = float(os.getenv("OMNIVOICE_GUIDANCE", "2.0"))
OMNIVOICE_SOURCE_SR = 24000
# OmniVoice handles a full multi-sentence scene in ONE generate() call cleanly
# (verified: a 264-char / 3-sentence scene synthesized with zero drops/garbling).
# Default path is therefore per-scene (no concat seams). As a safety net, a scene
# longer than this char budget is split into sentences and concatenated like F5.
OMNIVOICE_MAX_CHARS = int(os.getenv("OMNIVOICE_MAX_CHARS", "400"))
# Reference clip cap for OmniVoice. Unlike F5 (6 s), OmniVoice cloned cleanly from the
# full ~10 s EscBase ref in Phase-1 testing, so the cap is generous — it only guards a
# pathologically long upload. The ref_text is transcribed from the PREPPED (trimmed +
# capped) clip so text and audio always agree.
OMNIVOICE_REF_MAX_SEC = float(os.getenv("OMNIVOICE_REF_MAX_SEC", "15.0"))
# PUNCTUATION BEATS (owner 2026-07-29: "sau dấu câu như chấm, phẩy phải ngưng 1 nhịp").
# OmniVoice synthesizes a short scene in ONE generate() and simply does not render a pause at
# an internal comma — measured on video 278 scene 1 ("… thống trị GPU, bỏ xa mọi đối thủ."),
# whisper reports a 0 ms gap between EVERY pair of words, and silencedetect finds no interior
# silence at all (only 0.119 s lead / 0.121 s tail). So the scene reads as one continuous run.
#
# The pause is inserted BY CONSTRUCTION: the scene is split at pause punctuation, each unit is
# synthesized separately, and a fixed silence is placed at each join (sentence-final -> the
# longer PARA gap, comma/;/: -> the shorter MID gap). This is exactly the route the reverted
# gap-shaper's post-mortem prescribed (see the note at the end of run_omnivoice): "do it by
# CONSTRUCTION … rather than by cutting finished audio" — slicing finished audio at whisper
# word boundaries clipped Vietnamese soft-consonant tails and produced audible cuts.
# COST: one generate() per unit instead of per scene (~2× the TTS inferences for typical
# 2-clause narration). Set OMNIVOICE_PUNCT_BEAT=0 to restore the single-inference behavior.
OMNIVOICE_PUNCT_BEAT = os.getenv("OMNIVOICE_PUNCT_BEAT", "1").strip().lower() not in ("0", "off", "false", "no")
OMNIVOICE_LANGUAGE = os.getenv("OMNIVOICE_LANGUAGE", "Vietnamese")
# OmniVoice's OWN text normalizer (numbers / dates / currency), added upstream in 0.2.1
# and OFF by default there. Enabled here so the model's first-party normalizer handles
# bare digits instead of a hand-rolled rule — it knows how the model was trained.
# ORDERING: this runs INSIDE generate(), i.e. AFTER the project's own passes, so the
# text it sees already has years spelled out digit-by-digit ("hai không hai sáu") and
# the decimal point as a word ("5 chấm 6"). It therefore cannot undo the owner's
# digit-by-digit year rule — the year is no longer a number by the time it gets there.
# Set OMNIVOICE_NORMALIZE_TEXT=0 to fall back to the upstream default (off).
OMNIVOICE_NORMALIZE_TEXT = os.getenv("OMNIVOICE_NORMALIZE_TEXT", "1").strip().lower() not in ("0", "off", "false", "no")

_PUNCT_BEAT_SENTENCE = ".!?…"
_PUNCT_BEAT_MID = ",;:"
# MINIMUM CLAUSE SIZE for the punctuation-beat split (owner-approved 2026-07-30). A clause
# short enough to be its own inference is dominated by the model's own lead-in and tail
# rather than by speech, and that produced two separate defects on video 287 scene 6
# ("… nguyên liệu, thiết kế, nhà máy, và mô hình." → four clauses, two of them 2 syllables):
#   * an 837 ms silence at the "thiết kế," → "nhà máy," join — 6x the 138 ms median. Measured
#     frame by frame, that gap is 160 ms of true silence, then a 120 ms blip at -41..-46 dB,
#     then 600 ms of silence (down to -84 dB, including frames of digital zero). Both cleanup
#     passes read the blip as SPEECH (edge trim at -50 dB, interior compressor at -45 dB), so
#     neither could see one long gap to remove — and the compressor's "reject if >40% removed"
#     guard fires on a fragment that is mostly silence, keeping the original.
#   * a standalone 3-syllable clause measured 436 ms/syllable, i.e. read as a whole sentence
#     with its own slow onset and falling close.
# Raising the threshold to fix the blip detection was rejected: -38 dB is inside Vietnamese
# breath and vowel-tail territory, which is exactly what made the old gap-shaper clip word
# endings. Merging removes the cause instead of chasing the symptom.
# TRADE-OFF: the beat at a merged clause's comma is LOST (the comma stays in the text so the
# model still gets the intonation, it just may not pause there). That cost is why the
# threshold is 4 and not the 6 originally proposed — measured over video 287's 88 narrations:
#   threshold  total clauses  scenes losing ALL beats  remaining 2-3 syllable clauses
#     off           160                 0                        22
#     4             139                 9                         0    <- chosen
#     5             133                13                         0
#     6             117                28                         0
#     8              97                48                         0
# 4 already removes EVERY pathological 2-3 syllable clause (the measured defect class) while
# costing 3x fewer beats than 6. 6 additionally silenced the commas in "… GPU, bỏ xa mọi đối
# thủ." and "… chip, mà thuê ngoài gia công." — both 5-syllable second clauses, and both are
# beats the owner had explicitly asked for, so 6 would have undone an earlier request.
OMNIVOICE_MIN_UNIT_SYL = int(os.getenv("OMNIVOICE_MIN_UNIT_SYL", "4"))

# Beat lengths for the OmniVoice punctuation-beat path. DEDICATED knobs (not the F5
# CF_PS_GAP_* ones) so tuning OmniVoice's pauses can never shift F5's per-sentence timing.
# The values are the AUDIBLE pause: each unit's facing edge is trimmed to
# OMNIVOICE_BEAT_EDGE_PAD_S first, so the heard gap ≈ beat + 2×pad instead of being
# dominated by whatever silence OmniVoice happened to pad each inference with (measured
# before trimming: a 0.12 s configured gap came out as a 353 ms pause, and a 4-unit scene
# inflated +64%).
OMNIVOICE_BEAT_MID_S = float(os.getenv("OMNIVOICE_BEAT_MID_S", "0.10"))
OMNIVOICE_BEAT_PARA_S = float(os.getenv("OMNIVOICE_BEAT_PARA_S", "0.28"))
OMNIVOICE_BEAT_EDGE_PAD_S = float(os.getenv("OMNIVOICE_BEAT_EDGE_PAD_S", "0.02"))

# PER-UNIT PACE BALANCE (owner 2026-07-29 "cân pace theo từng cụm"). Splitting a scene at
# punctuation gives each clause its OWN OmniVoice draw, so the clauses land at DIFFERENT
# paces — measured on video 280 scene 1: "Nvidia và AMD thống trị GPU," read 191 ms/syllable
# (14% faster than the video median) while "bỏ xa mọi đối thủ." read 252 (14% slower), a 32%
# split inside ONE scene. The assembly pace pass cannot fix that: it applies ONE atempo per
# SCENE, scaling both clauses equally and preserving the imbalance, and its own measurement
# reported the harmless-looking scene average (220/220).
#
# Here each clause is retimed to the scene's syllable-WEIGHTED MEAN pace, so the clauses
# converge on each other while the scene's total speech duration is preserved (a weighted
# mean redistributes time, it does not add any) — the downstream global pace pass and the
# video length are therefore untouched. Bounds keep a correction from dragging or rushing a
# clause. Runs on the still-separate unit files BEFORE the beats are inserted, which is also
# why captions need no remap: assemble derives the caption word_map by whispering the FINAL
# scene wav, so it never sees these intermediate timings.
OMNIVOICE_UNIT_PACE_BALANCE = os.getenv("OMNIVOICE_UNIT_PACE_BALANCE", "1").strip().lower() not in ("0", "off", "false", "no")
OMNIVOICE_UNIT_PACE_FLOOR = float(os.getenv("OMNIVOICE_UNIT_PACE_FLOOR", "0.85"))
OMNIVOICE_UNIT_PACE_CEIL = float(os.getenv("OMNIVOICE_UNIT_PACE_CEIL", "1.15"))
# A clause shorter than this is not measurable enough to trust (one drawn-out word dominates
# its ms/syllable), so it is neither corrected nor counted toward the scene target.
OMNIVOICE_UNIT_PACE_MIN_SYL = int(os.getenv("OMNIVOICE_UNIT_PACE_MIN_SYL", "3"))

# MID-SENTENCE SILENCE inside one clause (owner 2026-07-30: "các từ chung 1 câu phải đọc liên
# tục"). OmniVoice invents pauses where the text has no punctuation — video 282 scene 3
# ("… chip, mà thuê ngoài gia công.") carries a 148 ms silence after "thuê", which breaks the
# phrase. Compressing such gaps used to be unsafe: the reverted gap-shaper cut finished audio at
# whisper word boundaries and clipped soft Vietnamese consonant tails, and an energy pass could
# not tell a comma pause from an invented one.
#
# The punctuation-beat architecture removes that obstacle: a unit is split at EVERY , ; : . ! ?
# so it contains NO punctuation internally — every interior silence in a unit is therefore
# mid-sentence by construction, and the real punctuation pauses are added afterwards as beats.
# So the compression can be plain energy-based silenceremove, whose cut points sit inside actual
# silence rather than on a guessed word boundary. Threshold is deliberately conservative
# (-45 dB): F5/OmniVoice breath and vowel decay sit well above it and survive.
OMNIVOICE_INTRA_SILENCE = os.getenv("OMNIVOICE_INTRA_SILENCE", "1").strip().lower() not in ("0", "off", "false", "no")
OMNIVOICE_INTRA_KEEP_S = float(os.getenv("OMNIVOICE_INTRA_KEEP_S", "0.05"))
OMNIVOICE_INTRA_DB = float(os.getenv("OMNIVOICE_INTRA_DB", "-45"))


# ---------------------------------------------------------------------------
# PRONUNCIATION — OmniVoice's OWN word_improve.md column
# ---------------------------------------------------------------------------

def _parse_omni_column(md_path: str) -> list[tuple[str, str]]:
    """Parse word_improve.md's `say_as_omnivoice` column into (term, say_as) pairs.

    DELIBERATELY INDEPENDENT of tts_worker._parse_word_improve. That parser reads the
    F5/VieNeu columns and requires a non-empty F5 `say_as`; this one requires a non-empty
    `say_as_omnivoice` and ignores the F5 cell entirely. Keeping the two parsers separate
    is what allows a row like `| RAG |  |  | Rát |` to exist for OmniVoice WITHOUT that
    term entering F5's regex or F5's loanword set (the bug this split fixes).

    The header is located by name, so column order/position is free and extra columns are
    ignored. Rows without an omnivoice cell are skipped — a term absent from this map is
    spoken RAW (there is NO fallback to F5's say_as; owner decision).

    Returns pairs sorted LONGEST-term-first so multi-word terms match before their parts.
    Missing/corrupt file -> [] (no-op, never fatal)."""
    try:
        text = Path(md_path).read_text(encoding="utf-8")
    except OSError:
        return []
    pairs: list[tuple[str, str]] = []
    in_table = False
    ov_col = None
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            in_table = False
            ov_col = None
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        low = [c.lower() for c in cells]
        if "term" in low and any("say_as" in c or "say as" in c for c in low):
            in_table = True
            ov_col = None
            for idx, c in enumerate(low):
                if ("say_as" in c or "say as" in c) and "omnivoice" in c and ov_col is None:
                    ov_col = idx
            continue
        if not in_table or ov_col is None:
            continue
        # Separator row like |---|---|--- -> skip.
        if all(set(c) <= set("-: ") for c in cells if c):
            continue
        if not cells or ov_col >= len(cells):
            continue
        term = cells[0]
        say_as = cells[ov_col].strip()
        if not term or not say_as:
            continue
        pairs.append((term, say_as))
    pairs.sort(key=lambda t: len(t[0]), reverse=True)
    return pairs


def _compile_omni_map(pairs: list[tuple[str, str]]):
    """Compile the OmniVoice pairs into (regex_ci, regex_cs, repl) or (None, None, {}).

    Same whole-word / longest-first / acronym-case rules as the F5 map (shared
    _is_acronym_term + _PRON_WORDCHAR so both engines treat "AI" vs "ai" identically),
    but built ONLY from OmniVoice's own terms. The regexes therefore match exactly the
    terms this map can replace."""
    if not pairs:
        return None, None, {}
    repl: dict[str, str] = {}
    for term, say_as in pairs:
        repl.setdefault(term.lower(), say_as)
    alts_ci, alts_cs = [], []
    for term, _say in pairs:
        (alts_cs if _is_acronym_term(term) else alts_ci).append(re.escape(term))

    def _mk(alts, flags):
        if not alts:
            return None
        pattern = (
            r"(?<!" + _PRON_WORDCHAR + r")(?:" + "|".join(alts) + r")(?!" + _PRON_WORDCHAR + r")"
        )
        return re.compile(pattern, flags)

    return _mk(alts_ci, re.IGNORECASE | re.UNICODE), _mk(alts_cs, re.UNICODE), repl


# Load + compile ONCE per worker invocation (workers are short-lived subprocesses, so
# this picks up word_improve.md edits on the very next run — no API restart).
_OMNI_PAIRS = _parse_omni_column(WORD_IMPROVE_MD)
_OMNI_RE_CI, _OMNI_RE_CS, _OMNI_REPL = _compile_omni_map(_OMNI_PAIRS)
log.info("OmniVoice pronunciation map: %d term(s) from %s", len(_OMNI_PAIRS), WORD_IMPROVE_MD)


def _apply_omnivoice_pron(text: str) -> str:
    """OmniVoice-only pronunciation substitution — PLAIN whole-word replacement.

    Substitutes ONLY terms that declared a non-empty ``say_as_omnivoice`` cell. A term
    with no omnivoice cell is left RAW — there is NO fallback to the F5 ``say_as``
    (owner decision: OmniVoice must not inherit F5's respellings).

    Output is PLAIN text: unlike tts_worker._apply_pron_map this does NOT run
    ``_encode_separators`` / the \\x01-\\x05 speed markers — OmniVoice has no
    atempo-marker path. Acronyms match case-sensitively so the lowercase form is
    untouched; other terms match case-insensitively.

    With an all-empty ``say_as_omnivoice`` column the map is empty, so this returns
    ``text`` unchanged (byte-identical no-op)."""
    if not text or not _OMNI_REPL:
        return text

    def _sub(m: "re.Match") -> str:
        return _OMNI_REPL.get(m.group(0).lower(), m.group(0))

    # Case-sensitive (acronyms) first, then case-insensitive (word terms) — same order
    # as the F5 map so overlapping acronym/word terms resolve identically.
    if _OMNI_RE_CS is not None:
        text = _OMNI_RE_CS.sub(_sub, text)
    if _OMNI_RE_CI is not None:
        text = _OMNI_RE_CI.sub(_sub, text)
    return text


# ---------------------------------------------------------------------------
# TEXT NORMALIZATION (engine-neutral reading rules, applied on the OmniVoice path)
# ---------------------------------------------------------------------------

# FORCED DIGIT-BY-DIGIT YEARS — OFF by default since 2026-08-04 (owner). We used to expand an
# isolated 20xx into "hai không hai sáu" before synthesis so years obeyed a digit-by-digit rule.
# The owner now wants OmniVoice to read years its OWN way, letting the model's upstream
# normalizer (OMNIVOICE_NORMALIZE_TEXT) decide. Set OMNIVOICE_EXPAND_YEARS=1 to restore.
#
# F5/VieNeu are UNAFFECTED: they never call this: they force digit-by-digit through their own
# tts_worker._normalize_years, which is untouched.
#
# CROSS-DEPENDENCY (checked, see the note on generate._spoken_weight's year branch): the pace
# metric special-cases a 20xx token as one syllable per digit BECAUSE of this expansion. With
# the expansion off, that assumption only holds if the model also reads years digit-by-digit —
# verified by transcribing a real render before relying on it.
OMNIVOICE_EXPAND_YEARS = os.getenv("OMNIVOICE_EXPAND_YEARS", "0").strip().lower() not in ("0", "off", "false", "no")


def _expand_years_spacejoined(text: str) -> str:
    """Expand isolated 4-digit years (20xx) to digit-by-digit Vietnamese, SPACE-joined
    plain words: NO hyphens and NO F5 speed markers, because OmniVoice has no
    atempo/separator path. Owner rule — years read digit by digit: 2026 -> 'hai không
    hai sáu'. Only isolated 20xx tokens (word boundaries) so larger numbers ('20000
    người') are untouched. Distinct from tts_worker._normalize_years (the F5 variant,
    which may hyphen-join + FAST-encode the year via F5_YEAR_INLINE)."""
    def _yr(m: "re.Match") -> str:
        return " ".join(_YEAR_DIGIT_VN[int(c)] for c in m.group(1))
    return re.sub(r'\b(20\d{2})\b', _yr, text)


def _normalize_text_neutral(text: str) -> str:
    """ENGINE-NEUTRAL reading normalizations ONLY — returns PLAIN text with NO in-band
    speed markers (\\x01-\\x05) and NO word_improve say_as respellings.

    The OmniVoice entry point for text prep. Applies exactly four things, in order:
      1. thousands-separator comma -> period  ('4,000' -> '4.000'), so grouped numbers
         read as one quantity;
      2. decimal/version point -> a spoken WORD ('GPT 5.6' -> 'GPT 5 chấm 6', '2.5 đô' ->
         '2 phẩy 5 đô'), because every engine otherwise SWALLOWS the '.'. Runs AFTER (1)
         so the '4.000' it just produced is seen — and skipped — by the 3-digit rule;
      3. slash dates -> spoken words ('24/2' -> '24 tháng 2'). Runs BEFORE the year step
         so the '2026' it emits for a '24/2/2026' is picked up and expanded by (4);
      4. year expansion, digit-by-digit Vietnamese ('2026' -> 'hai không hai sáu') —
         DISABLED BY DEFAULT since 2026-08-04, see OMNIVOICE_EXPAND_YEARS.

    Steps 1-3 are the shared engine-agnostic helpers from tts_worker (F5/VieNeu apply the
    same three inside _apply_pron_map — they are reading rules, not engine tuning).
    Ordinary words (e.g. 'ChatGPT', 'agent') pass through untouched — no F5 respelling."""
    if not text:
        return text
    text = _normalize_thousands_sep(text)
    text = _expand_decimal_point(text)
    text = _expand_vn_dates(text)
    if OMNIVOICE_EXPAND_YEARS:
        text = _expand_years_spacejoined(text)
    return text


# ---------------------------------------------------------------------------
# CLAUSE UNITS, PACE BALANCE, SILENCE SHAPING (punctuation-beat architecture)
# ---------------------------------------------------------------------------

def _spoken_syllables(text: str) -> int:
    """Spoken-syllable count for a whole clause, acronym-aware.

    Mirrors generate._spoken_weight per token (all-caps run = one syllable per LETTER,
    mixed-case like ChatGPT ≈ len//2, otherwise vowel groups via _count_syllables) and sums.
    The acronym rule matters here for the same reason it did in the pace metric: "GPU" is
    ONE vowel group but THREE spoken syllables, so counting vowel groups would make an
    acronym-bearing clause look ~2× slower than it reads and invert the correction."""
    total = 0
    for tok in re.findall(r"\S+", text or ""):
        letters = [c for c in tok if c.isalpha()]
        if letters and all(c.isupper() for c in letters) and len(letters) >= 2:
            total += max(1, len(letters))
            continue
        if sum(1 for c in tok if c.isupper()) >= 2:
            total += max(2, len(tok) // 2)
            continue
        total += _count_syllables(tok)
    return max(1, total)


def _split_punct_units(text: str) -> list[tuple[str, bool]]:
    """Split narration at EVERY pause-inducing punctuation mark for the OmniVoice
    punctuation-beat path. Returns [(unit_text, ends_sentence)] where ends_sentence=True
    means the unit closed on . ! ? … (take the longer PARA gap) and False means it closed
    on , ; : (take the shorter MID gap).

    Differs from tts_worker._split_sentences_for_ps deliberately: that one is a
    CHAR-BUDGET chunker (sentence-first, comma sub-split only when a sentence is too long
    for one F5 infer), so a short sentence with an internal comma comes back as ONE unit
    and gets no beat — which is precisely the video-278 scene-1 symptom. Here the split is
    driven by punctuation alone, regardless of length.

    The punctuation STAYS attached to its unit so the model still sees the comma/period and
    renders the matching intonation (a clause fed without its comma reads flat).

    Guards, so a beat is never inserted mid-token:
      • the mark must be followed by whitespace or end-of-text — protects '4.000' and
        '3,5' (thousands/decimal separators produced by _normalize_text_neutral) and
        'claude.ai';
      • a digit-to-digit mark never splits even if spaced oddly;
      • units that end up empty/whitespace are dropped.
    A text with no internal punctuation returns a single unit, so the caller keeps the
    exact single-inference path (no concat, no added silence)."""
    s = (text or "").strip()
    if not s:
        return []
    units: list[tuple[str, bool]] = []
    start = 0
    for i, ch in enumerate(s):
        if ch not in _PUNCT_BEAT_SENTENCE and ch not in _PUNCT_BEAT_MID:
            continue
        # Must be at end-of-text or followed by whitespace (else it is intra-token: 4.000).
        if i + 1 < len(s) and not s[i + 1].isspace():
            continue
        # Digit on both sides (e.g. "3, 5" from a malformed number) — not a clause break.
        prev_c = s[i - 1] if i else ""
        nxt = s[i + 1:].lstrip()
        if prev_c.isdigit() and nxt[:1].isdigit():
            continue
        unit = s[start:i + 1].strip()
        if unit:
            units.append((unit, ch in _PUNCT_BEAT_SENTENCE))
        start = i + 1
    tail = s[start:].strip()
    if tail:
        # Trailing fragment with no closing mark — treat as sentence-final.
        units.append((tail, True))
    return units or [(s, True)]


def _merge_short_units(units: list[tuple[str, bool]]) -> list[tuple[str, bool, bool]]:
    """Merge clauses below OMNIVOICE_MIN_UNIT_SYL spoken syllables into a neighbour.

    Forward-accumulating: a too-short clause absorbs the following one (and keeps absorbing
    until it is long enough or the text runs out). The merged unit inherits the gap class of
    the LAST clause it swallowed, because the join now sits at that clause's punctuation.
    A trailing remainder that is still too short is merged BACKWARD into the previous unit —
    otherwise the tail of the scene would keep exactly the tiny fragment this is meant to
    avoid. Punctuation is preserved inside the merged text.

    Returns [(unit_text, ends_sentence, merged)] where merged=True means this unit swallowed
    2+ original punctuation clauses, so it still carries an INTERNAL punctuation mark that
    never got its own join/beat. Callers must skip interior-silence compression on such a
    unit — otherwise the model's own pause at that internal mark gets erased along with
    genuine hallucinated dead air (job-338 "sửa lỗi, kiểm chứng" losing its comma pause).
    merged=False (unchanged list) when the threshold is <= 1 or nothing is short."""
    if OMNIVOICE_MIN_UNIT_SYL <= 1 or len(units) < 2:
        return [(t, e, False) for t, e in units]
    out: list[tuple[str, bool, bool]] = []
    buf_text: str | None = None
    buf_ends = True
    buf_n = 0
    for text, ends in units:
        if buf_text is None:
            buf_text, buf_ends = text, ends
        else:
            buf_text, buf_ends = f"{buf_text} {text}", ends
        buf_n += 1
        if _spoken_syllables(buf_text) >= OMNIVOICE_MIN_UNIT_SYL:
            out.append((buf_text, buf_ends, buf_n > 1))
            buf_text = None
            buf_n = 0
    if buf_text is not None:
        # Leftover shorter than the threshold: fold it into the previous unit if there is
        # one (its own gap class wins — it now ends the merged unit), else keep it alone.
        # Either way this join gains an internal mark, so merged=True from here on.
        if out:
            prev_text, _prev_ends, _prev_merged = out[-1]
            out[-1] = (f"{prev_text} {buf_text}", buf_ends, True)
        else:
            out.append((buf_text, buf_ends, buf_n > 1))
    return out


def _atempo_file(path: str, factor: float) -> bool:
    """Time-stretch a unit wav in place by `factor` (pitch-preserving; <1 lengthens).
    Returns True if the file was rewritten. Fail-safe: any error leaves it untouched."""
    import numpy as np
    import soundfile as sf

    try:
        wav, sr = sf.read(path, dtype="float32", always_2d=False)
    except Exception as e:  # noqa: BLE001
        log.warning("unit-pace: cannot read %s (%s); left unchanged", path, e)
        return False
    a = wav if getattr(wav, "ndim", 1) == 1 else wav.reshape(wav.shape[0], -1).mean(axis=1)
    out = _atempo_stretch_np(a, sr, factor)
    if out is None or len(out) == 0 or len(out) == len(a):
        return False
    try:
        sf.write(path, np.asarray(out, dtype=np.float32), sr, subtype="PCM_16")
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("unit-pace: cannot write %s (%s); left unchanged", path, e)
        return False


def _balance_unit_pace(scene, parts: list[str], syls: list[int]) -> None:
    """Retime each clause of one scene to the scene's syllable-weighted mean pace, in place.

    parts/syls are 1:1 (unit wav path, its spoken-syllable count). Units below
    OMNIVOICE_UNIT_PACE_MIN_SYL syllables are excluded from the target AND left unchanged.
    No-op when fewer than 2 units qualify (nothing to balance against)."""
    if not OMNIVOICE_UNIT_PACE_BALANCE or len(parts) < 2:
        return
    durs = [_probe_duration(p) for p in parts]
    idx = [i for i, (d, s) in enumerate(zip(durs, syls))
           if d > 0 and s >= OMNIVOICE_UNIT_PACE_MIN_SYL]
    if len(idx) < 2:
        return
    # Syllable-weighted mean = total speech time / total syllables of the MEASURABLE units.
    target = sum(durs[i] for i in idx) * 1000.0 / sum(syls[i] for i in idx)
    before = [durs[i] * 1000.0 / syls[i] for i in idx]
    if max(before) - min(before) < 10.0:      # already even (<10 ms/syll spread)
        return
    applied = []
    for i in idx:
        pace = durs[i] * 1000.0 / syls[i]
        factor = min(OMNIVOICE_UNIT_PACE_CEIL, max(OMNIVOICE_UNIT_PACE_FLOOR, pace / target))
        if abs(factor - 1.0) < 0.02:
            applied.append((round(pace), 1.0))
            continue
        ok = _atempo_file(parts[i], factor)
        applied.append((round(pace), round(factor, 3) if ok else 1.0))
    after = []
    for i, (_p, f) in zip(idx, applied):
        d = _probe_duration(parts[i])
        after.append(d * 1000.0 / syls[i] if d > 0 else 0.0)
    log.info("OmniVoice scene %s unit-pace: target %.0f ms/syll; per-unit (pace,atempo)=%s; "
             "spread %.0f -> %.0f ms/syll", scene, target, applied,
             max(before) - min(before), (max(after) - min(after)) if len(after) >= 2 else 0.0)


def _compress_unit_interior(path: str) -> float:
    """Shrink mid-sentence silences INSIDE one clause wav to OMNIVOICE_INTRA_KEEP_S, in place.
    Returns the seconds removed (0.0 if nothing changed).

    Interior-only by construction: silenceremove's stop_* mode scans the whole stream, but a
    unit's own edges are handled separately (_trim_unit_edges for join-facing edges, and
    generate._normalize_scene_joins downstream for the scene's outer edges), so shrinking edge
    silence here too is harmless — the beat that follows is inserted afterwards at a fixed size.
    Fail-safe: any ffmpeg error, a missing output, or an implausible result (shorter than 0.1 s,
    or more than 40% of the clip removed — a sign the threshold ate speech) keeps the original."""
    if not OMNIVOICE_INTRA_SILENCE:
        return 0.0
    before = _probe_duration(path)
    if before <= 0:
        return 0.0
    tmp = path + ".intra.wav"
    af = (f"silenceremove=stop_periods=-1:stop_duration={OMNIVOICE_INTRA_KEEP_S:.3f}"
          f":stop_threshold={OMNIVOICE_INTRA_DB:.0f}dB:stop_silence={OMNIVOICE_INTRA_KEEP_S:.3f}")
    proc = subprocess.run(
        [_ffmpeg_bin(), "-y", "-i", path, "-af", af, "-ar", str(CANONICAL_SR),
         "-ac", "1", "-c:a", "pcm_s16le", tmp],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0 or not os.path.isfile(tmp):
        log.warning("intra-silence: ffmpeg failed on %s; left unchanged", os.path.basename(path))
        return 0.0
    after = _probe_duration(tmp)
    if after < 0.1 or after < before * 0.6:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return 0.0
    try:
        os.replace(tmp, path)
    except OSError as e:
        log.warning("intra-silence: cannot replace %s (%s); left unchanged", path, e)
        return 0.0
    return max(0.0, before - after)


def _trim_unit_edges(path: str, pad_s: float, lead: bool, tail: bool) -> None:
    """Trim silence from ONE or BOTH edges of a unit wav, in place, keeping `pad_s`.

    Same detection as tts_worker._trim_silence_np (peak-normalized -50 dB by default, low
    enough that a soft Vietnamese tone-3 final like "ngữ" is NOT eaten — see that function's
    note), but side-selectable: the punctuation-beat concat trims only the edges that FACE a
    join, so the scene's own first/last edges stay exactly as OmniVoice produced them and the
    downstream scene-join normalization keeps seeing what it always has. No-op on an
    all-silence or too-short result."""
    if not (lead or tail):
        return
    import numpy as np
    import soundfile as sf

    try:
        wav, sr = sf.read(path, dtype="float32", always_2d=False)
    except Exception as e:  # noqa: BLE001 — a trim must never break synthesis
        log.warning("beat edge-trim: cannot read %s (%s); left unchanged", path, e)
        return
    a = wav if getattr(wav, "ndim", 1) == 1 else wav.reshape(wav.shape[0], -1).mean(axis=1)
    peak = float(np.max(np.abs(a))) or 1.0
    thr = 10.0 ** (float(os.getenv("CF_TTS_TRIM_DB", "-50.0")) / 20.0)
    loud = np.where(np.abs(a / peak) > thr)[0]
    if loud.size == 0:
        return
    pad = max(0, int(pad_s * sr))
    lo = max(0, int(loud[0]) - pad) if lead else 0
    hi = min(a.size, int(loud[-1]) + pad) if tail else a.size
    if hi - lo < int(0.05 * sr):
        return
    try:
        sf.write(path, wav[lo:hi], sr, subtype="PCM_16")
    except Exception as e:  # noqa: BLE001
        log.warning("beat edge-trim: cannot write %s (%s); left unchanged", path, e)


# ---------------------------------------------------------------------------
# REFERENCE CLIP + REF TEXT (own sidecar dir — must never collide with F5's)
# ---------------------------------------------------------------------------

def _prep_omni_ref(src: str, dst: str, max_sec: float = OMNIVOICE_REF_MAX_SEC) -> str:
    """Produce an OmniVoice-safe reference clip: trim silence edges, cap duration,
    24 kHz mono pcm_s16le. Returns dst on success, or the original src if ffmpeg fails
    or the result would be empty (so we never break a working short ref).

    Deliberately a SEPARATE function from tts_worker._prep_f5_ref even though the ffmpeg
    recipe is currently identical: the two engines' ref caps and future ref tuning are
    independent (F5 caps at 6 s for out_len ratio reasons that do not apply here), and
    sharing it would put an F5-owned function back on the OmniVoice path."""
    af = (
        "silenceremove=start_periods=1:start_silence=0.1:start_threshold=-40dB,"
        "areverse,"
        "silenceremove=start_periods=1:start_silence=0.1:start_threshold=-40dB,"
        "areverse"
    )
    proc = subprocess.run(
        [_ffmpeg_bin(), "-y", "-i", src, "-af", af, "-t", f"{max_sec:g}",
         "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", dst],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0 or not os.path.isfile(dst) or os.path.getsize(dst) < 1024:
        return src  # fall back to the original ref rather than fail the clone
    return dst


def _resolve_omni_ref_text(cfg, ref_audio: str, transcribe_audio: str) -> str:
    """Return the transcript of the OmniVoice reference clip, matched to `transcribe_audio`.

    Separate from tts_worker._resolve_ref_text (F5's) for two reasons the Phase-1 A/B proved
    matter:

      1. **Casing.** OmniVoice cloned cleanly with the NATURAL-CASE transcript; F5
         lowercases for ViVoice. We keep natural case here.
      2. **Clip identity.** The transcript MUST describe the EXACT clip OmniVoice sees.
         F5 caps its ref at 6 s and caches the transcript of THAT capped clip in
         ``_reftext/<name>.txt``. Feeding that 6 s transcript against a longer OmniVoice
         ref made OmniVoice hallucinate a leading phrase (Phase-1 finding). We therefore
         transcribe ``transcribe_audio`` (the OmniVoice-prepped clip) and cache it in a
         DISTINCT sidecar dir ``_reftext_omni/<name>.txt`` so the two engines' sidecars can
         never collide — even if a caller points OmniVoice at an F5 clone file.

    Priority: explicit refText from the caller → matched sidecar (fingerprint of the
    ORIGINAL clip + prepped-clip duration) → transcribe once with faster-whisper and cache.
    Whisper VRAM is released before returning so the OmniVoice model load has the headroom
    on the 8 GB card.
    """
    explicit = (cfg.get("refText") or "").strip()
    if explicit:
        return explicit  # natural case — do NOT lowercase for OmniVoice

    # Fingerprint = original-clip identity (busts on re-clone) + the prepped clip's
    # duration when prep changed the audio (busts a stale full-clip transcript).
    fp = _ref_fingerprint(ref_audio)
    if transcribe_audio and os.path.abspath(transcribe_audio) != os.path.abspath(ref_audio):
        cap_dur = _probe_duration(transcribe_audio)
        orig_dur = _probe_duration(ref_audio)
        if cap_dur > 0 and orig_dur - cap_dur > 0.1:
            fp = f"{fp}|cap:{cap_dur:.2f}"

    ref_path = Path(ref_audio)
    cache_dir = ref_path.parent / "_reftext_omni"
    cache_file = cache_dir / (ref_path.stem + ".txt")
    if fp and cache_file.is_file():
        raw = cache_file.read_text(encoding="utf-8")
        first, _, rest = raw.partition("\n")
        if first.startswith("# fp:"):
            stored = first[len("# fp:"):].strip()
            if stored == fp:
                cached = rest.strip()
                if cached:
                    return cached  # natural case
            else:
                log.warning(
                    "OmniVoice ref_text sidecar STALE for %s — fingerprint mismatch "
                    "(stored=%s current=%s); re-transcribing the new clip",
                    cache_file.name, stored, fp,
                )

    _enable_cuda_dlls()
    from faster_whisper import WhisperModel

    model_name = os.getenv("WHISPER_MODEL", "medium")
    device = os.getenv("WHISPER_DEVICE", "cuda")
    compute = os.getenv("WHISPER_COMPUTE", "float16")
    try:
        wm = WhisperModel(model_name, device=device, compute_type=compute)
    except Exception as e:
        if device == "cuda":
            log.warning("OmniVoice ref-text whisper CUDA init failed (%s); falling back to cpu/int8", e)
            device, compute = "cpu", "int8"
            wm = WhisperModel(model_name, device=device, compute_type=compute)
        else:
            raise
    log.info("OmniVoice ref-text whisper device=%s compute=%s", device, compute)
    segments, _info = wm.transcribe(transcribe_audio or ref_audio, language="vi")
    text = " ".join(s.text.strip() for s in segments).strip()
    # Release whisper VRAM BEFORE the OmniVoice model loads (~2.4 GB) on the 8 GB GPU.
    del wm
    if device == "cuda":
        try:
            import gc
            gc.collect()
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass
    if not text:
        raise RuntimeError(
            f"could not transcribe reference voice for OmniVoice ref_text: {ref_audio}"
        )
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text((f"# fp:{fp}\n" if fp else "") + text, encoding="utf-8")
    except OSError:
        pass
    return text  # natural case


# ---------------------------------------------------------------------------
# SYNTHESIS
# ---------------------------------------------------------------------------

def run_omnivoice(cfg, items, out_dir, progress):
    """Synthesize each item with OmniVoice (k2-fsa/OmniVoice), cloning refAudio.

    RAW Vietnamese in — no F5 say_as / vi_corrections respelling (Phase-2 contract);
    only the say_as_omnivoice column applies. Per-scene: the scene is split at pause
    punctuation into clause units (OMNIVOICE_PUNCT_BEAT), each unit gets its own
    generate(), and the units are concatenated with a fixed beat of silence at each join;
    a unit longer than OMNIVOICE_MAX_CHARS is sub-split by the shared char-budget chunker
    as a safety net. OmniVoice emits 24 kHz and NO timestamps, so each scene is resampled
    to the 48 kHz canonical rate (matching F5/VieNeu) and downstream faster-whisper
    handles caption/scene alignment exactly as before.

    Returns the standard worker result list; each entry additionally carries
    "beatSilenceS" (seconds of silence inserted ON PURPOSE) so the assembly pace pass can
    discount it. F5/VieNeu results simply omit that key."""
    ref_audio = cfg.get("refAudio")
    if not ref_audio:
        raise RuntimeError("OmniVoice requires a reference voice (refAudio); it is clone-only.")
    if not Path(ref_audio).exists():
        raise FileNotFoundError(f"reference voice not found: {ref_audio}")

    # Prep an OmniVoice-safe ref clip (silence-trim + generous cap, 24 kHz mono) and
    # transcribe THAT exact clip for a matched ref_text.
    ref_tmp = tempfile.TemporaryDirectory()
    safe_ref = _prep_omni_ref(ref_audio, os.path.join(ref_tmp.name, "omni_ref.wav"),
                              max_sec=OMNIVOICE_REF_MAX_SEC)
    orig_dur = _probe_duration(ref_audio)
    safe_dur = _probe_duration(safe_ref)
    if safe_ref != ref_audio and orig_dur > OMNIVOICE_REF_MAX_SEC + 0.05:
        log.warning("OmniVoice ref capped %.1fs -> %.1fs (OMNIVOICE_REF_MAX_SEC=%.1f) for %s",
                    orig_dur, safe_dur, OMNIVOICE_REF_MAX_SEC, os.path.basename(ref_audio))

    # CRITICAL: ref_text must describe the EXACT prepped clip (see _resolve_omni_ref_text).
    ref_text = _resolve_omni_ref_text(cfg, ref_audio, transcribe_audio=safe_ref)
    if not ref_text:
        raise RuntimeError("OmniVoice ref_text is empty — provide refText or a transcribable ref voice.")
    log.info("OmniVoice ref_text (%d chars): %s", len(ref_text), ref_text[:120])

    speed = float(cfg.get("speed") or 1.0)

    progress(0, "Nạp model OmniVoice")
    _enable_cuda_dlls()
    import torch
    from omnivoice.models.omnivoice import OmniVoice
    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    model = OmniVoice.from_pretrained(OMNIVOICE_MODEL, device_map=device,
                                      dtype=dtype, load_asr=False)
    src_sr = int(getattr(model, "sampling_rate", OMNIVOICE_SOURCE_SR))
    log.info("OmniVoice loaded on %s (num_step=%d guidance=%.1f speed=%.2f sr=%d)",
             device, OMNIVOICE_NUM_STEP, OMNIVOICE_GUIDANCE, speed, src_sr)

    import numpy as np
    import soundfile as sf

    def _synth_one(gen_text: str, dst_24k: str) -> None:
        """One OmniVoice generate() -> a 24 kHz mono wav at dst_24k. RAW text in."""
        audios = model.generate(
            text=gen_text, language=(OMNIVOICE_LANGUAGE or None), ref_text=ref_text,
            ref_audio=safe_ref, num_step=OMNIVOICE_NUM_STEP,
            guidance_scale=OMNIVOICE_GUIDANCE, speed=speed,
            normalize_text=OMNIVOICE_NORMALIZE_TEXT,
        )
        wav = np.asarray(audios[0], dtype=np.float32).squeeze()
        sf.write(dst_24k, wav, src_sr, subtype="PCM_16")

    results = []
    total = len(items)
    try:
        for i, it in enumerate(items):
            scene = it.get("scene")
            text = (it.get("text") or "").strip()
            # OmniVoice text prep, two independent passes (numbers vs words):
            #   1. ENGINE-NEUTRAL number/year normalization — years digit-by-digit
            #      space-joined ('2026' -> 'hai không hai sáu'), thousands fix
            #      ('4,000' -> '4.000').
            #   2. OmniVoice-only PLAIN pronunciation substitution (say_as_omnivoice);
            #      a term WITHOUT an omnivoice cell stays RAW (NO F5 fallback). No
            #      \x01-\x05 speed markers.
            # Order is independent (pass 1 = numbers, pass 2 = word terms); neutral runs
            # first so number normalization sees the original digits. The ORIGINAL `text`
            # is kept for the caption/record; `spoken` drives synthesis.
            spoken = _apply_omnivoice_pron(_normalize_text_neutral(text))
            name = f"scene_{scene:03d}.wav" if isinstance(scene, int) else "audio.wav"
            path = out_dir / name
            out_dir.mkdir(parents=True, exist_ok=True)  # shared page audio dir may be removed mid-run by a sibling job's cleanup
            beat_silence_s = 0.0  # deliberate beat silence for THIS scene (see results.append)

            with tempfile.TemporaryDirectory() as td:
                if not spoken:
                    # Nothing to say -> short silent placeholder so the scene has a VO file.
                    sf.write(str(path), np.zeros(int(0.2 * CANONICAL_SR), dtype=np.float32),
                             CANONICAL_SR, subtype="PCM_16")
                else:
                    # UNIT SELECTION. Two independent reasons to split a scene:
                    #   1. PUNCTUATION BEATS (OMNIVOICE_PUNCT_BEAT) — split at every , ; : . ! ?
                    #      so a fixed silence can be placed at each join. This is the only way
                    #      OmniVoice pauses at an internal comma at all (see the knob's note).
                    #   2. CHAR BUDGET (safety net, pre-existing) — a unit still longer than
                    #      OMNIVOICE_MAX_CHARS is sub-split by the shared char-budget chunker.
                    # With the beat knob OFF and a short scene this yields exactly ONE unit, and
                    # _concat_wavs_48k's single-chunk fast path copies it through untouched — so
                    # the legacy single-inference behavior is preserved byte-for-byte.
                    if OMNIVOICE_PUNCT_BEAT:
                        # Split at punctuation, then fold clauses too short to survive as
                        # their own inference back into a neighbour (see _merge_short_units:
                        # a 2-syllable clause is mostly model lead-in/tail, which is what
                        # produced the 837 ms join on video 287 scene 6).
                        units = _merge_short_units(_split_punct_units(spoken))
                    else:
                        units = [(spoken, True, False)]
                    expanded: list[tuple[str, bool, bool]] = []
                    for unit_text, ends, merged in units:
                        if len(unit_text) <= OMNIVOICE_MAX_CHARS:
                            expanded.append((unit_text, ends, merged))
                            continue
                        subs = _split_sentences_for_ps(unit_text)
                        log.info("OmniVoice scene %s unit is %d chars (>%d) -> %d sub-chunk(s)",
                                 scene, len(unit_text), OMNIVOICE_MAX_CHARS, len(subs))
                        # The LAST sub-chunk inherits this unit's own gap class; earlier
                        # sub-chunks are mid-clause splits, so they take the shorter MID gap.
                        # `merged` propagates to every sub-chunk conservatively — the internal
                        # mark that made the parent unit `merged` could land in any of them.
                        for k, (sub_text, _sub_ends) in enumerate(subs):
                            expanded.append((sub_text, ends if k == len(subs) - 1 else False, merged))
                    usable = [(t, e, m) for t, e, m in expanded if t.strip()]
                    parts: list[str] = []
                    gaps: list[float] = []
                    unit_syls: list[int] = []
                    _intra_removed = 0.0   # seconds of mid-sentence dead air removed
                    prev_ends = True  # gap class of the previous emitted unit
                    for j, (unit_text, ends, merged) in enumerate(usable):
                        raw24 = os.path.join(td, f"s{j}.wav")
                        part48 = os.path.join(td, f"p{j}.wav")
                        _synth_one(unit_text.strip(), raw24)
                        _resample_to_canonical(raw24, part48)
                        # Remove model-invented MID-SENTENCE pauses first: inside one unit there
                        # is no punctuation, so any interior silence is a phrase break the text
                        # never asked for (job-291 "mà thuê … ngoài gia công"). Runs before the
                        # pace balance so the balance measures speech, not dead air. SKIPPED for
                        # a `merged` unit — it DOES have an internal punctuation mark (from
                        # _merge_short_units swallowing a too-short clause), so any interior
                        # silence there may be the model's own pause for that mark, not
                        # hallucinated dead air (job-338: this was erasing comma pauses).
                        if merged:
                            log.info("OmniVoice scene %s unit %d: merged clause, skipping "
                                     "interior-silence compression to keep its internal pause",
                                     scene, j)
                        else:
                            _intra_removed += _compress_unit_interior(part48)
                        # Trim ONLY the edges that face a join, so the heard pause is the
                        # configured beat rather than OmniVoice's per-inference padding. The
                        # scene's outer edges are left untouched (first unit's lead, last
                        # unit's tail) — those belong to _normalize_scene_joins downstream.
                        if len(usable) > 1:
                            _trim_unit_edges(part48, OMNIVOICE_BEAT_EDGE_PAD_S,
                                             lead=(j > 0), tail=(j < len(usable) - 1))
                        if parts:
                            # Gap BEFORE this part = the gap class of the PREVIOUS unit, i.e.
                            # the mark that unit closed on. gap_s carries one float per join.
                            gaps.append(OMNIVOICE_BEAT_PARA_S if prev_ends else OMNIVOICE_BEAT_MID_S)
                        parts.append(part48)
                        unit_syls.append(_spoken_syllables(unit_text))
                        prev_ends = ends
                    # Even out the clause-to-clause pace BEFORE the beats go in, while the
                    # units are still separate files (each clause was an independent draw, so
                    # they arrive at different paces — see _balance_unit_pace).
                    if len(parts) > 1:
                        _balance_unit_pace(scene, parts, unit_syls)
                    # EXACT deliberate-silence bookkeeping: report how much beat silence this
                    # scene received so the assembly pace pass can discount it instead of
                    # reading it as "the voice is slow here" (job-290 speed-up bug). Only the
                    # multi-unit branch inserts any; the silent-placeholder and single-unit
                    # paths leave the 0.0 initialized before this scene's temp dir.
                    beat_silence_s = float(sum(gaps)) if len(parts) > 1 else 0.0
                    if not parts:
                        sf.write(str(path), np.zeros(int(0.2 * CANONICAL_SR), dtype=np.float32),
                                 CANONICAL_SR, subtype="PCM_16")
                    elif len(parts) == 1:
                        # Single unit → straight copy (no concat, no added silence).
                        _concat_wavs_48k(parts, str(path))
                    else:
                        log.info("OmniVoice scene %s: %d punctuation unit(s), gaps=%s, "
                                 "mid-sentence dead air removed %.2fs",
                                 scene, len(parts), [round(x, 3) for x in gaps], _intra_removed)
                        _concat_wavs_48k(parts, str(path), gap_s=gaps)

            # (beat_silence_s is set in every branch above; see the bookkeeping note there.)
            # NO gap shaping on the OmniVoice path — REVERTED 2026-07-28 (owner: the job it
            # produced had audible CUTS at every comma/period). _shape_gaps_by_alignment
            # resizes silence by SLICING the wav at whisper word boundaries, and those
            # boundaries land early on Vietnamese soft consonant / vowel tails, so forcing a
            # fixed pause clips word tails and reads mechanically — the exact failure the
            # CF_TTS_PER_SENTENCE note in tts_worker.py already documents for F5. It also
            # never earned its keep here: measured across 59 scenes it left the wall-clock
            # spread unchanged (91 -> 100 ms/syll) while adding ~183 s per job. If gap control
            # is revisited for OmniVoice, do it by CONSTRUCTION (synthesize per sentence and
            # concatenate with fixed padding) rather than by cutting finished audio.
            duration_s = _probe_duration(str(path))
            results.append({
                "scene": scene, "text": text, "audioPath": str(path),
                "sampleRate": CANONICAL_SR, "durationS": duration_s,
                # Silence inserted ON PURPOSE (punctuation beats), seconds. The assembly pace
                # pass discounts it so a deliberate pause is never mistaken for a slow read
                # (job-290 speed-up bug). 0.0 when the scene was one single inference.
                "beatSilenceS": round(beat_silence_s, 4),
            })
            progress(round((i + 1) / max(1, total) * 100), f"Lồng tiếng {i + 1}/{total}")
    finally:
        # Free VRAM promptly — SDXL image gen may run next in the pipeline (sequential).
        try:
            del model
            import gc
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
    return results
