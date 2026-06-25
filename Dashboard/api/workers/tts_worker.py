"""TTS worker — runs inside cf-venv (where `vieneu` AND `f5_tts` live), NOT the API venv.

Invoked by the FastAPI host as:
    cf-venv/python.exe tts_worker.py <input.json> <output.json>

input.json:  {"items":[{"scene":1,"text":"..."}], "voice":null, "emotion":"natural",
              "engine":"vieneu"|"f5-tts", "refAudio":"...wav", "refText":"...",
              "outDir":"E:/ContentFactory/<page>/audio"}
output.json: {"count":N, "results":[{"scene","text","audioPath","sampleRate","durationS"}]}

Two engines, dispatched by the `engine` field (default "vieneu" → unchanged behavior):

  vieneu  : VieNeu-TTS v3 Turbo (ONNX/CPU, torch-free, 48 kHz). Preset voices OR
            voice-clone via encode_reference (persisted speaker codes).
  f5-tts  : F5-TTS + Vietnamese ViVoice checkpoint (torch/GPU, 24 kHz). Voice-clone
            ONLY (needs a ref wav + its transcript). Output is resampled to 48 kHz
            so downstream (whisper timestamps / FFmpeg assembly) stays consistent.

The model loads once per invocation, then synthesizes every item.
"""

import json
import logging
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path

# Log to stderr (the host captures each worker's stderr to a file and surfaces its
# tail on failure), so root-cause signal lands in the logs immediately.
logging.basicConfig(
    level=logging.INFO, stream=sys.stderr,
    format="%(asctime)s %(levelname)s [tts_worker] %(message)s",
)
log = logging.getLogger("contentfactory.tts_worker")


def _enable_cuda_dlls() -> None:
    """Put torch's bundled cuDNN9 / cuBLAS12 DLLs on the DLL search path so
    CTranslate2 (faster-whisper on CUDA, used for F5 ref-text) can load them.
    os.add_dll_directory is the robust mechanism (PATH alone is not honored for
    native DLL search on modern Windows Python). Best-effort."""
    cand = os.getenv("CF_TORCH_LIB")
    if not (cand and os.path.isdir(cand)):
        try:
            import torch  # noqa
            cand = os.path.join(os.path.dirname(torch.__file__), "lib")
        except Exception:
            cand = None
    if cand and os.path.isdir(cand):
        try:
            os.add_dll_directory(cand)
        except Exception:
            pass
        os.environ["PATH"] = cand + os.pathsep + os.environ.get("PATH", "")

# Char-density floor (ref_text chars per second of the EFFECTIVE ref clip). Below
# this, F5's out_len = ref_len*(ref_text+gen_text)/ref_text ratio blows up and F5
# regenerates the reference text instead of gen_text — the low-density echo bug.
F5_DENSITY_FLOOR = float(os.getenv("F5_DENSITY_FLOOR", "12.0"))

# Adaptive-speed target (ref_text chars per second). F5 sizes the GENERATED segment
# as  gen_frames = ref_frames / ref_text_len * gen_text_len / speed, i.e. the term
# (ref_frames / ref_text_len) is the ref's seconds-per-char — its INVERSE density.
# A slow / vowel-dragging reference (low density) makes that ratio large, inflating
# the frame budget; F5 fills the surplus by STRETCHING a vowel (the "là..à..à" drag)
# or by hallucinating extra words. We can't make a slow reference denser, but we can
# divide the budget back down: setting speed = density_target / actual_density makes
# the budget identical to what a F5_SPEED_TARGET-density reference would produce, so
# the drag has no surplus to live in. speed is clamped to >=1.0 (never SLOW a clip —
# that would re-introduce drag) and to F5_SPEED_MAX.
#
# READABILITY OVER DRAG-AVOIDANCE (2026-06-23). `speed` does NOT only soak up the
# ref's idle vowel surplus — it compresses the WHOLE generated utterance (out_len is
# divided by speed). The owner's hard rule: a video must NEVER be made shorter by
# reading faster; reduce/curate content or let it run longer instead. The previous
# values (target 17 ch/s, max 1.6) sized the output as if a fast reader and let a
# low-density ref like Tourist (~11.3 ch/s) be read at ~1.5x — globally ~50% faster,
# the "đọc quá nhanh" the owner reported. We now aim only to remove DRAG, not to
# compress for fit:
#   * F5_SPEED_TARGET = 14.0 ch/s — a comfortable VN narration pace (~2.0-2.2 wps),
#     not the aggressive 17. The generated speech is sized toward this, so a low-
#     density ref is nudged just enough to deny the drag its surplus.
#   * F5_SPEED_MAX = 1.25 — a conservative ceiling so NO clip is ever read drastically
#     fast (was 1.6 = up to +60%). Worst case is now +25%, only on the sparsest refs.
# A ref already at/above the target gives factor <=1.0 → clamped to 1.0 (dense voices
# like Surface/Vision/Vision are untouched). Verified on Tourist (11.3 ch/s): at the
# new factor (~1.24) the "là" drag does NOT return and the output reads at ~14 ch/s /
# ~2.1 wps — see _workspace/bug_tts_overspeed_media.md for before/after measurements.
# Both knobs stay env-overridable for tuning a future checkpoint without a code change.
F5_SPEED_TARGET = float(os.getenv("F5_SPEED_TARGET", "14.0"))
F5_SPEED_MAX = float(os.getenv("F5_SPEED_MAX", "1.25"))

# Canonical pipeline sample rate. VieNeu v3turbo emits 48 kHz; F5-TTS emits 24 kHz.
# We normalize F5 output to this so whisper timestamps + FFmpeg concat see one rate.
CANONICAL_SR = 48000

# F5-TTS Vietnamese (ViVoice) checkpoint, per media-engineer's verified contract.
F5_MODEL = os.getenv("F5_MODEL", "F5TTS_Base")
F5_CKPT = os.getenv("F5_CKPT", r"E:\Installed\f5-vietnamese\ViVoice\model_last.pt")
F5_VOCAB = os.getenv("F5_VOCAB", r"E:\Installed\f5-vietnamese\ViVoice\vocab.txt")

# Head-clip lead-in (DISABLED by default). The earlier theory was that F5 drops
# the first word of gen_text, so we prepended a throwaway "Vâng. " sentence. But
# the lead-in was NEVER trimmed off the output — it leaked a spurious "Vâng," into
# EVERY scene and F5 frequently dragged/garbled that prepended fragment, which is
# the verified cause of the "tiếng bị méo, kéo dãn" distortion. Re-tested on the
# ViVoice checkpoint: with NO lead-in the first word is preserved on every run
# (e.g. "Chữ…", "Harness…", "Ta…", "nhưng…" all intact), so the guard is pure harm.
# Kept as an env knob (empty default) in case a future checkpoint regresses; if you
# re-enable it you MUST also trim the lead-in audio back off after synthesis.
F5_LEADIN = os.getenv("F5_LEADIN", "")


def _mk_progress(prog_file):
    """Return a write(pct, msg) that atomically updates the progress JSON file.
    No-op when prog_file is None. The host polls this file and forwards it live."""
    if not prog_file:
        return lambda pct, msg: None

    def write(pct, msg):
        try:
            tmp = prog_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"pct": int(pct), "msg": msg}, f, ensure_ascii=False)
            os.replace(tmp, prog_file)
        except Exception:
            pass

    return write


# ---- Pronunciation map (word_improve.md) -----------------------------------
#
# F5-TTS (ViVoice VN checkpoint) mispronounces several English/tech terms (e.g.
# "agent" -> "ai-gen", "ChatGPT" -> garbled). word_improve.md is a human-editable
# table of  term | say_as | note  rows; before synthesis we replace each `term`
# (whole-word, case-insensitive) in the SPOKEN text with its `say_as` respelling
# so the VN model lands on the intended English-ish sound.
#
# CRITICAL: this is applied ONLY to the text fed to F5/VieNeu (the audio). The
# CAPTION is built upstream from the ORIGINAL narration, never from this mapped
# text, so viewers HEAR the fix but READ the correct term. See generate.py
# (_aligned_caption_words uses scene.caption / narration, not the TTS input).

# word_improve.md lives next to the API (one dir up from workers/). Override path
# with WORD_IMPROVE_MD if relocated.
WORD_IMPROVE_MD = os.getenv(
    "WORD_IMPROVE_MD",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "word_improve.md"),
)

# A "word character" for boundary purposes: any Unicode letter/digit (so Vietnamese
# diacritic letters count) plus underscore. We treat a term as whole-word when it
# is NOT flanked by such a character. \w under re.UNICODE already covers VN letters.
_PRON_WORDCHAR = r"[^\W]"  # one unicode word char (letters incl. VN, digits, _)


def _parse_word_improve(md_path: str) -> list[tuple[str, str, str]]:
    """Parse word_improve.md's pipe table into (term, say_as, say_as_vieneu) triples.

    Reads ONLY rows under a markdown table whose header contains 'term' and
    'say_as'. The DEFAULT say_as column (F5-tuned) is mandatory. An OPTIONAL
    'say_as_vieneu' column carries a VieNeu-specific override: VieNeu is a
    different model (ONNX v3turbo) and the F5-tuned respelling does not always
    land on it — verified cases (agent, JSON, prompt) read CORRECTLY raw on
    VieNeu but garble with the F5 respelling. When a VieNeu cell is present and
    non-empty it is used for the vieneu engine; otherwise the engine falls back
    to the default say_as (so adding the column never changes F5 behavior, and a
    term with no VieNeu cell keeps the shared respelling).

    Skips the header, the |---| separator, blank default say_as cells, and any
    non-table prose. Returns triples sorted LONGEST-term-first so multi-word
    terms match before their parts. say_as_vieneu is "" when not provided.
    Missing/corrupt file -> [] (no-op, never fatal)."""
    try:
        text = Path(md_path).read_text(encoding="utf-8")
    except OSError:
        return []
    triples: list[tuple[str, str, str]] = []
    in_table = False
    vn_col = None  # index of the say_as_vieneu column within a row, if present
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            in_table = False
            vn_col = None
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        low = [c.lower() for c in cells]
        # Header row: enables table parsing and locates the optional VieNeu column.
        if "term" in low and any("say_as" in c or "say as" in c for c in low):
            in_table = True
            vn_col = None
            for idx, c in enumerate(low):
                # Match the VieNeu override header (say_as_vieneu / say as vieneu).
                if "vieneu" in c and ("say_as" in c or "say as" in c):
                    vn_col = idx
                    break
            continue
        if not in_table:
            continue
        # Separator row like |---|---|--- -> skip.
        if all(set(c) <= set("-: ") for c in cells if c):
            continue
        if len(cells) < 2:
            continue
        term, say_as = cells[0], cells[1]
        if not term or not say_as:
            continue
        say_as_vieneu = ""
        if vn_col is not None and vn_col < len(cells):
            say_as_vieneu = cells[vn_col].strip()
        triples.append((term, say_as, say_as_vieneu))
    triples.sort(key=lambda t: len(t[0]), reverse=True)
    return triples


# An ACRONYM term is matched CASE-SENSITIVELY: an all-uppercase token of >=2 chars
# whose letters are all uppercase (digits allowed). Rationale (the "AI" homograph
# bug): "AI" the tech noun is written uppercase, but Vietnamese has a common
# lowercase pronoun "ai" (who / anyone). A blanket case-insensitive map turns every
# "ai" into the tech respelling "ây ai" — e.g. "ai làm gì, ai report cho ai" would be
# spoken "ây-ai làm gì…". Acronyms (AI, API, CPU, NLP, UX, UI, HTML, CSS, URL, HTTP,
# SQL, TTS, SDK, JSON, LLM, RAM, OCR, ID, GPT, BERT) are therefore matched ONLY in
# their uppercase form; word-like terms (agent, prompt, ChatGPT, Claude, debug, …)
# stay case-insensitive as before so "Agent"/"agent"/"AGENT" all map.
def _is_acronym_term(term: str) -> bool:
    letters = [c for c in term if c.isalpha()]
    return (
        len(term) >= 2
        and bool(letters)
        and all(c.isupper() for c in letters)
        and " " not in term
        and "-" not in term
    )


def _compile_pron_map(triples: list[tuple[str, str, str]]):
    """Compile the (term, say_as, say_as_vieneu) triples into TWO whole-word,
    longest-first regexes (case-insensitive for word terms, case-sensitive for
    all-caps acronyms) plus per-engine replacement maps.

    Returns (regex_ci, regex_cs, {lower_term: say_as_default}, {lower_term: say_as_vieneu})
    or (None, None, {}, {}). The vieneu map ONLY contains terms that declared a
    non-empty VieNeu override; lookups fall back to the default map when a term is
    absent. Acronym terms (see _is_acronym_term) go into regex_cs (case-sensitive,
    NO IGNORECASE) so the lowercase form of the token is left untouched; every other
    term goes into regex_ci (IGNORECASE) — preserving the previous behavior for them."""
    if not triples:
        return None, None, {}, {}
    repl_default: dict[str, str] = {}
    repl_vieneu: dict[str, str] = {}
    for term, say_as, say_as_vieneu in triples:
        repl_default.setdefault(term.lower(), say_as)
        if say_as_vieneu:
            repl_vieneu.setdefault(term.lower(), say_as_vieneu)
    # Partition terms into case-insensitive (word-like) and case-sensitive (acronym)
    # alternations. triples are already sorted longest-first so multi-char terms win.
    alts_ci, alts_cs = [], []
    for term, _, _ in triples:
        (alts_cs if _is_acronym_term(term) else alts_ci).append(re.escape(term))

    def _mk(alts, flags):
        if not alts:
            return None
        pattern = (
            r"(?<!" + _PRON_WORDCHAR + r")(?:" + "|".join(alts) + r")(?!" + _PRON_WORDCHAR + r")"
        )
        return re.compile(pattern, flags)

    regex_ci = _mk(alts_ci, re.IGNORECASE | re.UNICODE)
    regex_cs = _mk(alts_cs, re.UNICODE)  # case-SENSITIVE: acronyms only in uppercase
    return regex_ci, regex_cs, repl_default, repl_vieneu


# Load + compile ONCE per worker invocation (workers are short-lived subprocesses,
# so this picks up word_improve.md edits on the very next run — no API restart).
_PRON_PAIRS = _parse_word_improve(WORD_IMPROVE_MD)
_PRON_RE_CI, _PRON_RE_CS, _PRON_REPL, _PRON_REPL_VIENEU = _compile_pron_map(_PRON_PAIRS)
if _PRON_PAIRS:
    log.info(
        "pronunciation map: %d term(s) from %s (%d vieneu override(s))",
        len(_PRON_PAIRS), WORD_IMPROVE_MD, len(_PRON_REPL_VIENEU),
    )
else:
    log.info("pronunciation map: none (no %s or empty)", WORD_IMPROVE_MD)


def _apply_pron_map(text: str, engine: str = "f5") -> str:
    """Replace mapped terms in SPOKEN text with their say_as respelling. Whole-word,
    case-insensitive. No-op when the map is empty. APPLIED ONLY to TTS input — never
    to caption/narration text.

    `engine` selects the say_as variant: for "vieneu" a term's VieNeu-specific
    override is used when one exists, else the default (F5) say_as. Any other engine
    value (incl. the default "f5") uses ONLY the default map, so the F5 code path is
    byte-identical to before the override column existed.

    Acronym terms (AI, API, …) are matched CASE-SENSITIVELY via _PRON_RE_CS so the
    Vietnamese lowercase pronoun "ai" (and any lowercase acronym spelling) is left
    untouched; all other terms are matched case-insensitively via _PRON_RE_CI."""
    if not text or (_PRON_RE_CI is None and _PRON_RE_CS is None):
        return text

    # Strip digit-unit hyphens: "4-nghìn" → "4 nghìn". A hyphen between a digit and a
    # Vietnamese number unit (or a byte unit) is read by TTS as a pause ("4 — nghìn");
    # removing it makes the magnitude read continuously. Runs before term replacement.
    text = re.sub(
        r'(\d+)-(nghìn|triệu|tỷ|byte|[KkMmGgTt][Bb])\b',
        r'\1 \2',
        text,
    )

    # Strip word-connecting hyphens in Vietnamese compound phrases.
    # A hyphen not preceded by an uppercase ASCII letter is a Vietnamese
    # syllable connector (e.g. "giải quyết-được", "chính-ít") that the TTS
    # reads as a hard pause or emits a stray phoneme. Replace with a space.
    # All-caps technical terms (DALL-E, USB-C) are safe: their preceding
    # letter is uppercase → regex doesn't match → they survive to be handled
    # by the pron map below. Runs before term replacement so no say_as
    # hyphens (injected below) are touched.
    text = re.sub(r'(?<![A-Z])-(?=\w)', ' ', text)

    use_vieneu = engine == "vieneu" and bool(_PRON_REPL_VIENEU)

    def _sub(m: "re.Match") -> str:
        key = m.group(0).lower()
        if use_vieneu and key in _PRON_REPL_VIENEU:
            return _PRON_REPL_VIENEU[key]
        return _PRON_REPL.get(key, m.group(0))

    # Apply the case-sensitive (acronym) pass first, then the case-insensitive pass.
    # The two alternations are disjoint by construction (a term is in exactly one),
    # and acronym say_as values contain no letters that would re-trigger a word match.
    if _PRON_RE_CS is not None:
        text = _PRON_RE_CS.sub(_sub, text)
    if _PRON_RE_CI is not None:
        text = _PRON_RE_CI.sub(_sub, text)
    return text


# ---- Shared text/audio helpers ---------------------------------------------

# VieNeu (and F5) degrade on long inputs: the model runs to its frame cap, then
# RAMBLES / LOOPS a phrase and pads the tail with silence (observed: a 24.0s clip
# carrying ~3s of garbled speech + ~20s of dead air). Synthesizing one SENTENCE at
# a time keeps every call well inside the model's reliable range, then we concat.
# A char ceiling also splits a single over-long sentence so no call is ever huge.
_SENT_SPLIT = re.compile(r"(?<=[.!?…;:])\s+|\n+")

# Chunk ceiling. PREVIOUS default 160 split many ordinary multi-clause Vietnamese
# sentences at a comma, inserting an audible pause MID-sentence (the job-31 bug).
# Raised to 320 so a normal narration sentence is ONE chunk (one infer call, no
# internal pause); only genuinely long sentences fall through to the comma split.
# Env-tunable via TTS_MAX_CHUNK_CHARS. The model still degrades on very long input,
# so a sentence above this ceiling is still comma/char-split as a safety net.
_MAX_CHUNK_CHARS = int(os.getenv("TTS_MAX_CHUNK_CHARS", "320"))

# F5-specific chunk ceiling (chars). F5-TTS sizes the generated segment from the
# ref/gen frame ratio and degrades on long gen_text: past roughly a couple hundred
# characters it starts DROPPING or garbling words (the documented long-input
# failure). The VieNeu ceiling of 320 is too high for F5 — at 320 chars a single
# infer call is well into F5's drop zone. We use a tighter 220-char budget for F5:
# comfortably below the ~200-token / ~250-char threshold where drops appear, while
# still keeping most ordinary Vietnamese narration sentences in ONE chunk (so we do
# not introduce a mid-sentence pause). Env-tunable via F5_MAX_CHUNK_CHARS.
_F5_MAX_CHUNK_CHARS = int(os.getenv("F5_MAX_CHUNK_CHARS", "220"))


def _split_for_tts(text: str, max_chars: int = _MAX_CHUNK_CHARS) -> list[tuple[str, bool]]:
    """Split narration into chunks for per-chunk synthesis, TAGGED by boundary type.

    Returns a list of (chunk, ends_sentence) tuples:
      - ends_sentence=True  → this chunk is the END of a sentence (or the whole
        text); the caller inserts the full inter-SENTENCE gap after it.
      - ends_sentence=False → this chunk was carved out of a longer sentence at a
        comma / hard char cut; the caller inserts the smaller intra-sentence gap
        (or none) so the listener does not hear a sentence-sized pause mid-sentence.

    Splitting still happens sentence-first; a sentence within the char ceiling is a
    single (chunk, True). A sentence ABOVE the ceiling is sub-split on commas then
    hard char count, each sub-piece tagged False except the LAST (which carries the
    sentence end). Returns non-empty, stripped chunks; empty input -> []."""
    text = (text or "").strip()
    if not text:
        return []
    pieces = [p.strip() for p in _SENT_SPLIT.split(text) if p.strip()]
    out: list[tuple[str, bool]] = []
    for p in pieces:
        if len(p) <= max_chars:
            out.append((p, True))
            continue
        # Sentence is too long for one reliable infer() call: break on commas,
        # accumulating up to the char ceiling. These sub-pieces are MID-sentence.
        subs: list[str] = []
        buf = ""
        for sub in re.split(r"(?<=,)\s+", p):
            if buf and len(buf) + 1 + len(sub) > max_chars:
                subs.append(buf.strip())
                buf = sub
            else:
                buf = (buf + " " + sub).strip() if buf else sub
        if buf.strip():
            subs.append(buf.strip())
        # Hard-wrap any sub-piece still over the ceiling (no punctuation to split on).
        wrapped: list[str] = []
        for sp in subs:
            while len(sp) > max_chars:
                cut = sp.rfind(" ", 0, max_chars) or max_chars
                cut = cut if cut > 0 else max_chars
                wrapped.append(sp[:cut].strip())
                sp = sp[cut:].strip()
            if sp:
                wrapped.append(sp)
        # Tag every sub-piece mid-sentence (False) except the last, which ends the
        # sentence (True) → full gap only at the real sentence boundary.
        for j, sp in enumerate(wrapped):
            out.append((sp, j == len(wrapped) - 1))
    return out or [(text, True)]


def _trim_silence_np(wav, sr: int, thresh_db: float = -40.0, pad_s: float = 0.12):
    """Trim leading/trailing silence from a float/int waveform (numpy 1-D array).

    Keeps a small pad of silence at each end. Guards against the model's trailing
    dead-air (the frame-cap failure mode) inflating scene duration and causing the
    assembler to hold a frozen video frame over silence. Returns the trimmed array
    (or the original if it would trim to nothing)."""
    import numpy as np

    a = np.asarray(wav)
    if a.ndim > 1:
        a = a.reshape(a.shape[0], -1).mean(axis=1)
    a = a.astype(np.float32)
    peak = float(np.max(np.abs(a))) or 1.0
    norm = a / peak
    thr = 10.0 ** (thresh_db / 20.0)
    loud = np.where(np.abs(norm) > thr)[0]
    if loud.size == 0:
        return wav  # all silence — leave as-is (caller decides)
    pad = int(pad_s * sr)
    lo = max(0, int(loud[0]) - pad)
    hi = min(a.size, int(loud[-1]) + pad)
    if hi - lo < int(0.1 * sr):  # would trim to <0.1s — keep original
        return wav
    return wav[lo:hi]


# ---- VieNeu engine ----------------------------------------------------------

def _run_vieneu(cfg, items, out_dir, progress):
    import numpy as np
    from vieneu import Vieneu

    # mode="v3turbo": ONNX on CPU (torch-free), 48 kHz, built-in preset voices.
    tts = Vieneu(mode="v3turbo")
    voice = cfg.get("voice")          # None -> default preset voice
    emotion = cfg.get("emotion", "natural")
    apply_watermark = bool(cfg.get("applyWatermark", False))  # off by default

    # Voice cloning: encode the reference wav ONCE (torch-free MOSS ONNX encoder),
    # then reuse its codes for every scene. ref_codes takes precedence over `voice`.
    ref_audio = cfg.get("refAudio")
    ref_codes = None
    if ref_audio:
        if not Path(ref_audio).exists():
            raise FileNotFoundError(f"reference voice not found: {ref_audio}")
        ref_codes = tts.encode_reference(ref_audio)

    # Optional generation tuning — only pass keys the caller actually set, so
    # everything else keeps VieNeu's own defaults.
    tune = {}
    for cfg_key, infer_key in (
        ("temperature", "temperature"),
        ("repetitionPenalty", "repetition_penalty"),
        ("maxNewFrames", "max_new_frames"),
        ("topK", "top_k"),
        ("topP", "top_p"),
    ):
        if cfg.get(cfg_key) is not None:
            tune[infer_key] = cfg[cfg_key]

    sr = int(tts.sample_rate)
    # Two distinct silences (env-tunable). The SENTENCE gap is a real, audible pause
    # at a sentence boundary (natural breathing point). The intra-sentence gap is the
    # silence inserted between sub-chunks carved out of ONE long sentence at a comma /
    # char cut — kept short (0.06s default) so a split sentence does not develop an
    # audible mid-sentence pause (the job-31 bug). With the raised char ceiling most
    # sentences are one chunk and never see the intra-sentence gap at all.
    sent_gap_s = float(os.getenv("TTS_SENT_GAP_S", "0.18"))
    intra_gap_s = float(os.getenv("TTS_INTRA_GAP_S", "0.06"))
    sent_gap = np.zeros(int(sent_gap_s * sr), dtype=np.float32)
    intra_gap = np.zeros(int(intra_gap_s * sr), dtype=np.float32)

    total = len(items)
    results_by_idx = {}
    completed = 0

    # VieNeu is ONNX/CPU — the runtime is thread-safe and carries no GPU lock, so
    # scenes can be synthesized concurrently. Tune TTS_VIENEU_WORKERS (default 3)
    # if CPU becomes saturated; set to 1 to revert to sequential.
    def _synth_scene(i, it):
        scene = it.get("scene")
        text = it["text"]
        # Pronunciation map: SPOKEN text only; captions keep original upstream.
        # engine="vieneu" picks VieNeu-specific say_as overrides from word_improve.md.
        spoken = _apply_pron_map(text, engine="vieneu")
        # Synthesize sentence-by-sentence to avoid the frame-cap garble failure.
        chunks = _split_for_tts(spoken)
        parts = []
        prev_ends_sentence = True  # gap BEFORE a chunk reflects the PREVIOUS boundary
        for _ci, (chunk, ends_sentence) in enumerate(chunks):
            w = tts.infer(
                chunk, ref_codes=ref_codes, voice=voice,
                emotion=emotion, apply_watermark=apply_watermark, **tune,
            )
            w = np.asarray(w, dtype=np.float32).reshape(-1)
            w = np.asarray(_trim_silence_np(w, sr), dtype=np.float32).reshape(-1)
            if w.size:
                if parts:
                    # Full pause only when the PREVIOUS chunk ended a sentence; a
                    # mid-sentence (comma/char) split gets the short intra gap.
                    parts.append(sent_gap if prev_ends_sentence else intra_gap)
                parts.append(w)
                prev_ends_sentence = ends_sentence
        wav = np.concatenate(parts) if parts else np.zeros(int(0.2 * sr), dtype=np.float32)
        name = f"scene_{scene:03d}.wav" if isinstance(scene, int) else "audio.wav"
        path = out_dir / name
        tts.save(wav, path)  # writes at tts.sample_rate (48 kHz for v3turbo)
        duration_s = round(len(wav) / float(sr), 3)
        return {
            "scene": scene,
            "text": text,
            "audioPath": str(path),
            "sampleRate": int(tts.sample_rate),
            "durationS": duration_s,
        }

    from concurrent.futures import ThreadPoolExecutor, as_completed
    vieneu_workers = int(os.getenv("TTS_VIENEU_WORKERS", "3"))
    with ThreadPoolExecutor(max_workers=vieneu_workers) as ex:
        futs = {ex.submit(_synth_scene, i, it): i for i, it in enumerate(items)}
        for fut in as_completed(futs):
            idx = futs[fut]
            results_by_idx[idx] = fut.result()  # re-raises any exception from _synth_scene
            completed += 1
            progress(round(completed / max(1, total) * 100), f"Lồng tiếng {completed}/{total}")

    return [results_by_idx[i] for i in range(len(items))]


# ---- F5-TTS engine ----------------------------------------------------------

def _ffmpeg_bin():
    """Locate ffmpeg: explicit env first, else assume it is on PATH (the host
    adds the FFmpeg build dir to PATH before launching this worker)."""
    return os.getenv("FFMPEG_BIN", "ffmpeg")


# F5 reproduces the REFERENCE text (instead of gen_text) when the reference clip
# is too long for the text it carries: F5 sets output length from
#   out_len = ref_len * (ref_text_len + gen_text_len) / ref_text_len
# so a long, low-density ref (e.g. 11s carrying one short sentence) blows up the
# frame budget and the model just regenerates the reference. F5's own clipper only
# fires ABOVE 12s and prefers silence cuts, so a sub-12s low-density ref slips
# through untouched. We defensively cap the ref to F5_REF_MAX_SEC (after trimming
# silence edges) so the ref/gen ratio stays in F5's reliable range. 6s is the
# verified-reliable ceiling: an 11s low-density ref echoed the reference text, and
# the same clip at 8s was flaky (clean on some runs, full ref-echo on others),
# whereas at 6s it produced the requested text on every run. Dense refs still carry
# ample timbre+text inside 6s, so this does not regress normal voices.
F5_REF_MAX_SEC = float(os.getenv("F5_REF_MAX_SEC", "6.0"))


def _prep_f5_ref(src: str, dst: str, max_sec: float = F5_REF_MAX_SEC) -> str:
    """Produce an F5-safe reference clip: trim silence edges, hard-cap duration,
    24 kHz mono pcm_s16le. Returns dst on success, or the original src if ffmpeg
    fails or the result would be empty (so we never break a working short ref)."""
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


def _resample_to_canonical(src: str, dst: str, sr: int = CANONICAL_SR) -> None:
    """Resample F5's 24 kHz mono wav up to the canonical pipeline rate (48 kHz),
    mono, pcm_s16le — so whisper timestamps and FFmpeg concat see one rate, same
    as VieNeu output. Uses soxr (high-quality) resampler.

    Also trims leading/trailing silence: F5 can pad the tail with dead air, which
    would inflate the scene's VO duration and make the assembler hold a frozen
    video frame over silence. silenceremove on both ends (via reverse) leaves a
    short, natural pad. -40 dB / 0.2 s matches the VieNeu-side trim threshold."""
    af = (
        "aresample=resampler=soxr,"
        "silenceremove=start_periods=1:start_silence=0.12:start_threshold=-40dB,"
        "areverse,"
        "silenceremove=start_periods=1:start_silence=0.12:start_threshold=-40dB,"
        "areverse"
    )
    proc = subprocess.run(
        [_ffmpeg_bin(), "-y", "-i", src, "-ar", str(sr), "-ac", "1",
         "-af", af, "-c:a", "pcm_s16le", dst],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0 or not os.path.isfile(dst):
        raise RuntimeError(f"F5 resample failed: {(proc.stderr or '')[-500:]}")


def _probe_duration(path: str) -> float:
    ffprobe = os.getenv("FFPROBE_BIN", "ffprobe")
    # Force UTF-8: ffprobe may echo the (Vietnamese) path in captured stderr; on
    # Windows text=True decodes with cp1252 and a bad byte raises UnicodeDecodeError.
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    try:
        return round(float((proc.stdout or "").strip()), 3)
    except (ValueError, AttributeError):
        return 0.0


def _ref_fingerprint(ref_audio: str) -> str:
    """A cheap content fingerprint for the ORIGINAL voice clip: size + mtime_ns.

    Used to bust the ref_text sidecar when a voice is RE-CLONED under the same
    name (so the filename — and thus the sidecar path — is unchanged but the clip
    content differs). Re-uploading a new clip changes its size and/or mtime, so a
    fingerprint mismatch forces a re-transcription instead of serving the stale
    transcript of the previous clip (which would mismatch the new audio and make
    F5 echo/garble the reference)."""
    try:
        st = os.stat(ref_audio)
        return f"{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        return ""


def _resolve_ref_text(cfg, ref_audio: str, transcribe_audio: str | None = None) -> str:
    """Return the LOWERCASED transcript of the reference wav.

    Priority: explicit refText from the caller → cached sidecar (only if it still
    matches the current clip) → transcribe once with faster-whisper and cache. F5
    REQUIRES ref_text; we never let it auto-ASR.

    `transcribe_audio` (when given) is the actual clip whisper should transcribe —
    i.e. the F5-safe (silence-trimmed, duration-capped) version — so the transcript
    matches the audio F5 will see.

    The sidecar is keyed by the voice filename (<voiceDir>/_reftext/<name>.txt) but
    its FIRST line stores a content fingerprint (size:mtime_ns) of the ORIGINAL clip.
    On re-clone the filename is reused yet the clip differs, so the fingerprint no
    longer matches and we re-transcribe — never serving the previous clip's text
    against the new audio. Legacy sidecars (no fingerprint line) are treated as a
    miss so they get rewritten in the new format keyed to the current clip.

    NOTE (ref_text ↔ capped clip): when the ref is duration-capped, the cached
    transcript can describe a LONGER clip than F5 actually sees. We fold the capped
    clip's DURATION into the fingerprint so a capped ref transcribes (and caches) from
    the capped clip — keeping ref_text and the audio F5 sees in agreement. (This is a
    correctness hygiene measure; on the ViVoice checkpoint it does NOT by itself remove
    the leading ref-echo artifact — that echo is intrinsic to the model echoing the
    ref clip's TAIL regardless of ref_text length; see _workspace/tts_bugs_findings.md.)
    """
    explicit = (cfg.get("refText") or "").strip()
    if explicit:
        return explicit.lower()

    # Fingerprint = original-clip identity (busts on re-clone) PLUS the CAPPED clip's
    # DURATION when capping changed the audio (busts a stale full-clip transcript when
    # F5 will only see the capped clip). We key on the capped clip's DURATION, not its
    # size/mtime: the capped clip is a per-run temp file (its size/mtime would change
    # every run and never cache), but its duration is DETERMINISTIC given the original
    # clip + F5_REF_MAX_SEC, so the sidecar stays reusable across runs while still
    # being distinct from the (longer) original's transcript. When transcribe_audio is
    # the original (no cap fired) the suffix is empty, so existing sidecars stay valid.
    fp = _ref_fingerprint(ref_audio)
    if transcribe_audio and os.path.abspath(transcribe_audio) != os.path.abspath(ref_audio):
        cap_dur = _probe_duration(transcribe_audio)
        orig_dur = _probe_duration(ref_audio)
        # Only bust when the capped clip is meaningfully SHORTER than the original
        # (i.e. the cap actually removed audio); equal-length means no surplus tail.
        if cap_dur > 0 and orig_dur - cap_dur > 0.1:
            fp = f"{fp}|cap:{cap_dur:.2f}"

    # Sidecar cache next to the voice: <voiceDir>/_reftext/<name>.txt
    # Format: line 1 = "# fp:<size>:<mtime_ns>"; remaining lines = the transcript.
    ref_path = Path(ref_audio)
    cache_dir = ref_path.parent / "_reftext"
    cache_file = cache_dir / (ref_path.stem + ".txt")
    if fp and cache_file.is_file():
        raw = cache_file.read_text(encoding="utf-8")
        first, _, rest = raw.partition("\n")
        if first.startswith("# fp:"):
            stored = first[len("# fp:"):].strip()
            if stored == fp:
                cached = rest.strip()
                if cached:
                    return cached.lower()
            else:
                # Re-clone under the same name: the cached transcript belongs to the
                # PREVIOUS clip. Warn (this is the "re-cloned voice still broken" bug)
                # — we re-transcribe below so text matches the new audio.
                log.warning(
                    "ref_text sidecar STALE for %s — fingerprint mismatch "
                    "(stored=%s current=%s); re-transcribing the new clip",
                    cache_file.name, stored, fp,
                )
        else:
            # Legacy / unfingerprinted sidecar → ignore (re-transcribe + rewrite).
            log.info(
                "ref_text sidecar %s has no fingerprint (legacy) — re-transcribing",
                cache_file.name,
            )

    # Transcribe once with faster-whisper. Default is now CUDA/float16 (~3.3 GB)
    # for speed; F5 (~6 GB) loads AFTER this in the SAME process, so on the 8 GB
    # card BOTH cannot be resident at once. We therefore RELEASE whisper (del +
    # ctranslate2 unload + torch.cuda.empty_cache) BEFORE returning, so F5's load
    # below has the VRAM. cuDNN DLLs must be discoverable for the CUDA load.
    _enable_cuda_dlls()
    from faster_whisper import WhisperModel

    model_name = os.getenv("WHISPER_MODEL", "medium")
    device = os.getenv("WHISPER_DEVICE", "cuda")
    compute = os.getenv("WHISPER_COMPUTE", "float16")
    try:
        wm = WhisperModel(model_name, device=device, compute_type=compute)
    except Exception as e:
        if device == "cuda":
            log.warning("ref-text whisper CUDA init failed (%s); falling back to cpu/int8", e)
            device, compute = "cpu", "int8"
            wm = WhisperModel(model_name, device=device, compute_type=compute)
        else:
            raise
    log.info("ref-text whisper device=%s compute=%s", device, compute)
    segments, _info = wm.transcribe(transcribe_audio or ref_audio, language="vi")
    text = " ".join(s.text.strip() for s in segments).strip()
    # Release whisper VRAM BEFORE F5 loads (~6 GB) — critical on the 8 GB GPU.
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
            f"could not transcribe reference voice for ref_text: {ref_audio}"
        )
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Persist the fingerprint of the ORIGINAL clip so a future re-clone (same
        # name, different content) busts this cache automatically.
        cache_file.write_text((f"# fp:{fp}\n" if fp else "") + text, encoding="utf-8")
    except OSError:
        pass
    return text.lower()


def _concat_wavs_48k(part_paths: list[str], out_path: str, gap_s: float = 0.10) -> None:
    """Concatenate canonical-rate (48 kHz mono) WAV chunks into one WAV, inserting a
    short silence between consecutive chunks so split sentences do not run together.

    Single chunk → copied straight through (NO extra leading/trailing silence and no
    re-encode artifacts: behaves exactly like the old single-infer path). Reads/writes
    with soundfile (available in cf-venv alongside f5_tts). gap_s defaults to 100 ms —
    a natural inter-sentence breath, matching the VieNeu sentence-gap range."""
    import numpy as np
    import soundfile as sf

    if len(part_paths) == 1:
        # Fast path: no concatenation needed — preserve the exact bytes/duration the
        # old single-call path produced (no added silence at the ends).
        if part_paths[0] != out_path:
            data, sr = sf.read(part_paths[0], dtype="float32", always_2d=False)
            sf.write(out_path, data, sr, subtype="PCM_16")
        return

    gap = None
    parts: list = []
    sr_seen = CANONICAL_SR
    for p in part_paths:
        data, sr = sf.read(p, dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.reshape(data.shape[0], -1).mean(axis=1)
        sr_seen = sr
        if parts:
            if gap is None:
                gap = np.zeros(int(gap_s * sr), dtype=np.float32)
            parts.append(gap)
        parts.append(data.astype(np.float32))
    wav = np.concatenate(parts) if parts else np.zeros(int(0.2 * sr_seen), dtype=np.float32)
    sf.write(out_path, wav, sr_seen, subtype="PCM_16")


def _run_f5(cfg, items, out_dir, progress):
    import tempfile

    ref_audio = cfg.get("refAudio")
    if not ref_audio:
        raise RuntimeError("F5-TTS requires a reference voice (refAudio); it is clone-only.")
    if not Path(ref_audio).exists():
        raise FileNotFoundError(f"reference voice not found: {ref_audio}")

    for f in (F5_CKPT, F5_VOCAB):
        if not Path(f).exists():
            raise FileNotFoundError(f"F5 model file not found: {f}")

    # Build the F5-safe ref (silence-trimmed + duration-capped) and keep it for the
    # whole run. Capping BEFORE transcription is essential: F5 derives output length
    # from the ref/gen ratio, so an over-long, low-density ref makes it regenerate
    # the reference instead of gen_text (the "voice doesn't work" bug). ref_text is
    # transcribed from the SAME capped clip so text and audio agree.
    ref_tmp = tempfile.TemporaryDirectory()
    safe_ref = _prep_f5_ref(ref_audio, os.path.join(ref_tmp.name, "f5_ref.wav"))

    # Surface whether the defensive duration cap fired (a long ref got trimmed to
    # F5_REF_MAX_SEC). When it fires, the ref was over-long for F5's reliable range.
    orig_dur = _probe_duration(cfg.get("refAudio") or ref_audio)
    safe_dur = _probe_duration(safe_ref)
    if safe_ref != (cfg.get("refAudio") or ref_audio) and orig_dur > F5_REF_MAX_SEC + 0.05:
        log.warning(
            "F5 ref capped %.1fs -> %.1fs (F5_REF_MAX_SEC=%.1f) for %s",
            orig_dur, safe_dur, F5_REF_MAX_SEC, os.path.basename(cfg.get("refAudio") or ref_audio),
        )

    ref_text = _resolve_ref_text(cfg, ref_audio, transcribe_audio=safe_ref)
    if not ref_text:
        raise RuntimeError("F5-TTS ref_text is empty — provide refText or a transcribable ref voice.")

    # Low-density echo / vowel-drag guard. Too few ref_text chars per second of the
    # (capped) ref makes F5 over-size the generated segment; it fills the surplus by
    # echoing the reference, hallucinating words, or DRAGGING a vowel ("là..à..à").
    # We compensate by speeding up generation in exact proportion to the density
    # shortfall (see F5_SPEED_TARGET) so the frame budget lands where a normal-pace
    # reference would put it. An explicit caller speed wins (manual override).
    adaptive_speed = float(cfg.get("speed", 1.0))
    if cfg.get("speed") is None and safe_dur > 0 and ref_text:
        density = len(ref_text) / safe_dur
        if density < F5_DENSITY_FLOOR:
            adaptive_speed = max(1.0, min(F5_SPEED_MAX, F5_SPEED_TARGET / density))
            log.warning(
                "F5 LOW-DENSITY ref %.1f ch/s (<%.1f) for %s — compensating with "
                "speed=%.2f (target %.0f ch/s; ref_text=%d chars over %.1fs)",
                density, F5_DENSITY_FLOOR,
                os.path.basename(cfg.get("refAudio") or ref_audio),
                adaptive_speed, F5_SPEED_TARGET, len(ref_text), safe_dur,
            )

    ref_audio = safe_ref  # F5 inference uses the capped clip from here on

    progress(0, "Nạp model F5-TTS")
    from f5_tts.api import F5TTS

    tts = F5TTS(model=F5_MODEL, ckpt_file=F5_CKPT, vocab_file=F5_VOCAB)

    results = []
    total = len(items)
    for i, it in enumerate(items):
        scene = it.get("scene")
        text = (it["text"] or "").strip()
        # Pronunciation map applies to the SPOKEN text ONLY — the caption upstream
        # keeps the original `text`. ViVoice lowercases gen_text. F5_LEADIN is empty
        # by default (the old "Vâng. " lead-in leaked into every scene and garbled
        # the head — see its definition above); kept only as an env escape hatch.
        # engine="f5" uses ONLY the default say_as column (VieNeu overrides ignored).
        spoken = _apply_pron_map(text, engine="f5")
        # Split the scene narration into chunks BELOW F5's drop threshold and synth
        # each separately, then concat — a single infer call on a long scene drops /
        # garbles words past ~220 chars (issue 3). Splitting is sentence-first via the
        # shared _split_for_tts (F5 budget), so an ordinary short scene yields exactly
        # ONE chunk and behaves identically to the old single-infer path. The leadin
        # (empty by default) is prepended ONCE to the first chunk, not per chunk.
        chunks = _split_for_tts(spoken, max_chars=_F5_MAX_CHUNK_CHARS)
        if not chunks:
            # Empty / whitespace-only narration: nothing to say. Emit a short silent
            # WAV so the scene still has a (tiny) VO file and downstream does not break.
            chunks = []

        name = f"scene_{scene:03d}.wav" if isinstance(scene, int) else "audio.wav"
        path = out_dir / name

        if not chunks:
            import numpy as np
            import soundfile as sf
            sf.write(str(path), np.zeros(int(0.2 * CANONICAL_SR), dtype=np.float32),
                     CANONICAL_SR, subtype="PCM_16")
        else:
            with tempfile.TemporaryDirectory() as td:
                part_paths: list[str] = []
                for ci, (chunk, _ends_sentence) in enumerate(chunks):
                    gen_text = ((F5_LEADIN + chunk) if ci == 0 else chunk).lower()
                    raw = os.path.join(td, f"f5_raw_{ci:03d}.wav")
                    tts.infer(
                        ref_file=ref_audio,
                        ref_text=ref_text,
                        gen_text=gen_text,
                        file_wave=raw,
                        nfe_step=int(cfg.get("nfeStep", 32)),
                        cfg_strength=float(cfg.get("cfgStrength", 2.0)),
                        speed=adaptive_speed,
                        sway_sampling_coef=float(cfg.get("swaySamplingCoef", -1.0)),
                        target_rms=float(cfg.get("targetRms", 0.1)),
                        cross_fade_duration=float(cfg.get("crossFadeDuration", 0.15)),
                        remove_silence=False,
                    )
                    if not os.path.isfile(raw):
                        raise RuntimeError(f"scene {scene}: F5 produced no audio (chunk {ci})")
                    # Normalize each chunk 24kHz → 48kHz mono (+ silence-trim its ends)
                    # BEFORE concatenation so downstream matches VieNeu and F5's per-
                    # chunk tail dead-air is removed; then concat the 48 kHz chunks.
                    norm = os.path.join(td, f"f5_48k_{ci:03d}.wav")
                    _resample_to_canonical(raw, norm)
                    part_paths.append(norm)
                # One chunk → straight copy (no added silence); many → joined with a
                # short inter-chunk gap so split sentences do not run together.
                _concat_wavs_48k(part_paths, str(path))

        duration_s = _probe_duration(str(path))
        results.append({
            "scene": scene,
            "text": text,
            "audioPath": str(path),
            "sampleRate": CANONICAL_SR,
            "durationS": duration_s,
        })
        progress(round((i + 1) / max(1, total) * 100), f"Lồng tiếng {i + 1}/{total}")

    # Release the model so its VRAM frees before any SDXL stage runs.
    try:
        del tts
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    return results


# ---- entry ------------------------------------------------------------------

def main() -> None:
    in_path, out_path = sys.argv[1], sys.argv[2]
    cfg = json.loads(Path(in_path).read_text(encoding="utf-8"))

    out_dir = Path(cfg["outDir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    progress = _mk_progress(cfg.get("progressFile"))

    engine = (cfg.get("engine") or "vieneu").strip().lower()
    items = cfg["items"]

    if engine in ("f5", "f5-tts", "f5tts"):
        results = _run_f5(cfg, items, out_dir, progress)
    elif engine in ("vieneu", "", None):
        results = _run_vieneu(cfg, items, out_dir, progress)
    else:
        raise RuntimeError(
            f"TTS engine '{engine}' is not implemented (supported: vieneu, f5-tts)."
        )

    Path(out_path).write_text(
        json.dumps({"count": len(results), "results": results}, ensure_ascii=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
