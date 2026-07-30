"""TTS worker — runs inside cf-venv (where `vieneu` AND `f5_tts` live), NOT the API venv.

Invoked by the FastAPI host as:
    cf-venv/python.exe tts_worker.py <input.json> <output.json>

input.json:  {"items":[{"scene":1,"text":"..."}], "voice":null, "emotion":"natural",
              "engine":"vieneu"|"f5-tts", "refAudio":"...wav", "refText":"...",
              "outDir":"E:/ContentFactory/<page>/audio"}
output.json: {"count":N, "results":[{"scene","text","audioPath","sampleRate","durationS"}]}

Engines are dispatched by the `engine` field (default "f5" → F5-TTS is the project default):

  vieneu    : VieNeu-TTS v3 Turbo (ONNX/CPU, torch-free, 48 kHz). Preset voices OR
              voice-clone via encode_reference (persisted speaker codes).
  f5-tts    : F5-TTS + Vietnamese ViVoice checkpoint (torch/GPU, 24 kHz). Voice-clone
              ONLY (needs a ref wav + its transcript). Output is resampled to 48 kHz
              so downstream (whisper timestamps / FFmpeg assembly) stays consistent.
  omnivoice : k2-fsa/OmniVoice zero-shot clone — implemented ENTIRELY in the sibling
              module omnivoice_worker.py, imported lazily by main(). This file holds the
              F5/VieNeu paths only.

ENGINE ISOLATION RULE (owner decision, 2026-07-31): do NOT add an `if engine ==
"omnivoice"` branch to any function in this file. OmniVoice previously widened the
word_improve parser/compiler here, which leaked its own pronunciation rows into F5's
regex and loanword set and silently changed which chunks F5 re-drew. Engine-specific
logic belongs in that engine's own module; only engine-agnostic primitives are shared.

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
F5_SPEED_TARGET = float(os.getenv("F5_SPEED_TARGET", "13.0"))
F5_SPEED_MAX = float(os.getenv("F5_SPEED_MAX", "1.0"))

# ---- LEVER 2: reference-rate pace normalization (F5 only) -------------------
#
# F5's duration formula makes output pace INHERIT the reference clip's own speaking
# rate: gen_mel_frames = (ref_audio_len / ref_text_len) * gen_text_len / speed, so the
# OUTPUT byte-rate = speed × R_ref, where R_ref = ref_text_bytes / ref_audio_seconds
# (the ref's UTF-8 bytes of transcript per second of audio). Two refs of equal duration
# but different R_ref therefore read at different speeds at the SAME `speed=1.0` — the
# root cause of "India Review reads fast, Escbase reads right". (Derivation + sources:
# _workspace/12_research_f5_pace_rearchitecture.md.)
#
# To make pace INDEPENDENT of the reference, we solve output_rate = R_target for speed:
#     speed = R_target / R_ref
# so ANY ref auto-corrects to the same target byte-rate. R_target is calibrated to the
# CURRENT known-good voice (Escbase - F5-TTS) so that with THAT ref speed comes out ≈1.0
# — i.e. today's already-correct voiced pace is NOT changed. MEASURED 2026-07-05 (worker's
# own ref-prep: silence-trim + 24 kHz, then lowercased whisper transcript):
#     Escbase - F5-TTS   : 105 ref_text bytes over 5.610 s -> R_ref = 18.72 bytes/s
#     India Review - F5-TTS: 194 bytes over 5.670 s        -> R_ref = 34.21 bytes/s
# Default CF_TTS_TARGET_BYTE_RATE = 18.72 → Escbase speed = 18.72/18.72 = 1.00 (unchanged);
# India Review would need 18.72/34.21 = 0.55 → clamped to the 0.80 floor with a warning
# (that ref is too fast to fully normalize inside the safe band — it would need ref
# re-timing, out of scope). The whole point is FUTURE robustness: a different/faster/slower
# ref lands on the same pace automatically instead of requiring a per-voice F5_SPEED_SCALE.
#
# Clamped to [CF_TTS_SPEED_MIN, CF_TTS_SPEED_MAX] = [0.8, 1.3] (the researched safe band;
# beyond it F5 slurs or drags). A ref needing speed outside the band logs a warning.
# Gated behind CF_TTS_REF_RATE_NORM (default ON for the F5 path). An explicit caller
# `speed` still wins (manual override), and this only replaces the BASE speed — the
# existing density guard and F5_SPEED_SCALE still layer on top as before.
CF_TTS_REF_RATE_NORM = os.getenv("CF_TTS_REF_RATE_NORM", "1").strip().lower() not in ("0", "off", "false", "no")
CF_TTS_TARGET_BYTE_RATE = float(os.getenv("CF_TTS_TARGET_BYTE_RATE", "18.72"))
CF_TTS_SPEED_MIN = float(os.getenv("CF_TTS_SPEED_MIN", "0.8"))
CF_TTS_SPEED_MAX = float(os.getenv("CF_TTS_SPEED_MAX", "1.3"))

# F5-ONLY narration speed multiplier (owner: reduce reading speed). Applies to the F5-TTS
# synth path ONLY (VieNeu is untouched) — NOT a cross-engine global knob. F5's `speed`
# scalar sizes the generated frame budget: speed<1.0 = MORE frames = SLOWER speech
# (model-native, uniform, smoothest). This multiplies the per-scene speed uniformly across
# the WHOLE video so overall pace drops by a fixed amount, on TOP of any density
# compensation. Owner requirement: overall pace = previous-good baseline − 10% (i.e. 10%
# SLOWER). MEASURED (2026-07-02): the good baseline (v12/v82, Escbase voice) reads
# ~210 ms/syll; target = 231. The fast job (v84) used a DIFFERENT clone voice ("India
# Review") reading ~148 ms/syll at speed=1.0 — the "too fast" is a VOICE change, not the
# smoothing. Bringing India Review to target needs speed ~= 148/231 ~= 0.64; slowing the
# SAME baseline voice by the literal 10% needs ~= 0.90. The right factor is VOICE-DEPENDENT,
# so the default is the conservative literal 0.90; lower toward ~0.65 for a fast-cadence
# clone. Env-tunable. Clamped to [0.5, 1.0] — ONLY slows, never speeds up. 1.0 = disable.
F5_SPEED_SCALE = min(1.0, max(0.5, float(os.getenv("F5_SPEED_SCALE", "1.0"))))  # 1.0 = disabled; auto target-pace (generate.py) is now primary

# Canonical pipeline sample rate. VieNeu v3turbo emits 48 kHz; F5-TTS emits 24 kHz.
# We normalize F5 output to this so whisper timestamps + FFmpeg concat see one rate.
CANONICAL_SR = 48000

# Native F5-TTS output rate. Per-chunk post-processing (chính-fade, atempo) now stays
# at this rate; the 24→48 kHz resample runs ONCE per scene on the concatenated WAV.
F5_SOURCE_SR = 24000

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

# Head-protect filler (bug 1, ENABLED by default for F5). DISTINCT from F5_LEADIN:
# the old F5_LEADIN was never trimmed and leaked into every scene. This guard fixes
# the verified F5-ViVoice failure where a SHORT first token is dropped/garbled at the
# very start of a chunk (e.g. a list scene "Cursor... Windsurf..." → "Cursor" lost,
# whisper hears "Của/Winsorff" first). F5 is an infilling flow-matching model: the
# first token sits in the unstable ref→gen seam (SWivid issues #460/#85/#29), so a
# short leading fragment falls entirely inside that window. Prepending a throwaway
# 2-syllable word ENDING IN A PERIOD absorbs the seam; we then trim the filler back
# off the AUDIO using faster-whisper WORD timestamps (the only trim robust to the
# many short internal gaps of a comma/ellipsis list — naive "cut to first silence"
# ate the real items in testing). Applied ONLY to the first chunk of each scene.
# Verified remedy per researcher (Hungarian F5 model card + SWivid issues). Set
# F5_HEAD_PROTECT=0 to disable; F5_HEAD_FILLER overrides the filler word.
F5_HEAD_PROTECT = os.getenv("F5_HEAD_PROTECT", "1").strip().lower() not in ("0", "off", "false", "no")
# Period-terminated filler that gives F5 a clean internal seam to dump the onset garble
# into, then is trimmed back off by whisper word-timestamps (with an A5 silence-gap
# fallback). Lowercased to match ViVoice gen_text.
#
# Changed default "vâng rồi." → "này này." (A5b):
#  * The old 2-word "vâng rồi." had a MULTI-TOKEN partial-match failure: whisper often
#    transcribed only the first syllable ("vâng") and rendered "rồi" as "dồi"/"zồi" or
#    merged it forward, so the trim consumed 1 of 2 tokens and cut at the end of "vâng" —
#    leaving "rồi" audible (the owner's "âm dư vâng rồi" at ~31-33 s).
#  * "vâng"/"rồi" are also extremely common Vietnamese words, so real narration that
#    legitimately starts with them risked being over-trimmed by the prefix matcher.
#  * "này này." is two copies of ONE distinct token ("này" = "this/hey"), so the prefix
#    matcher only has to recognise a SINGLE wordform (no second wordform to mis-hear),
#    it is short with a hard period stop (clean post-filler silence gap for the A5
#    fallback), and whisper transcribes it reliably. NOTE: still subject to objective
#    real-audio verification (synthesize a head chunk → whisper the result → confirm the
#    filler is fully gone and the real first word is intact) before being declared good.
# Override with F5_HEAD_FILLER to revert/experiment without a code change.
F5_HEAD_FILLER = os.getenv("F5_HEAD_FILLER", "này này.")

# ---- Loanword rush repair (F5 only) ----------------------------------------
#
# F5 is alignment-free: it sizes each chunk's frame budget from ONE global ref/gen
# ratio and stochastically UNDER-allocates frames to long / foreign (English) tokens,
# rushing or clipping them — measured on video 81: "engineering" fell to ~127 ms/syll
# (vs ~285 ms/syll for surrounding Vietnamese) on an unlucky seed, and "prompt" clipped
# its /pt/ coda to "Prom". F5 exposes NO per-word duration control (only global speed),
# and respelling short English tokens reads WORSE on this VN checkpoint. So the only
# reliable, no-speedup remedy is a re-roll: after synthesising a chunk that CONTAINS a
# loanword, whisper-measure that word; if it rushed or mis-transcribed, re-render the
# chunk ONCE (F5 uses a fresh random seed each infer → a different draw) and keep the
# better attempt. This is the cheaper variant (loanword-only, 1 retry, resident whisper)
# approved by the owner. Pure-Vietnamese chunks are NEVER measured, so the cost stays at
# ~0.3 s (one CPU whisper pass) × only the chunks that contain a loanword.
#
# DISABLED by default (2026-07-04, BUG A). The "keep the better attempt" selection is
# "fewer failing loanwords wins; tie -> HIGHER worst-ms/syll wins" (see the repair loop):
# on a rushed/mis-heard first draw it re-renders and keeps the SLOWER draw, so that one
# loanword reads slower than its constant-pace neighbours -- exactly the owner's "harness
# engineering"/"agent" đột nhiên chậm. Worse, a respelled term (agent->"ây jừn") is heard
# by whisper as "ây dường", never prefix-matches "agent", so it is ALWAYS scored 0/flagged
# -> ALWAYS burns a re-render and keeps a non-deterministic (often slower) draw. That
# violates the owner's design rule: constant sentence pace, per-word speed ONLY on
# predefined say_as terms. So OFF by default. REGRESSION TRADEOFF: an unlucky F5 draw may
# occasionally rush/clip an English loanword (the original bug this caught) -- accepted in
# favor of constant pace. Re-enable with F5_LOANWORD_REPAIR=1 only if that regresses worse.
F5_LOANWORD_REPAIR = os.getenv("F5_LOANWORD_REPAIR", "0").strip().lower() not in ("0", "off", "false", "no")
# Rush threshold: a flagged loanword whose measured duration is BELOW this many ms per
# spoken syllable is considered rushed. Calibrated from measurement (video 81):
#   normal VN ~285 ms/syll; acceptable English re-renders 0.44-0.68 s ("engineering",
#   4 syll → ~110-170 ms/syll); the rushed shipped draw was 0.44 s / 4 = 110 ms/syll and
#   "prompt" clipped at 40-140 ms. 150 ms/syll sits above the clearly-rushed band and
#   below the comfortable band, so it catches clipped reads without firing on good ones.
# Env-overridable via F5_LOANWORD_MIN_MS_PER_SYL.
F5_LOANWORD_MIN_MS_PER_SYL = float(os.getenv("F5_LOANWORD_MIN_MS_PER_SYL", "150.0"))
# Max re-renders per flagged chunk (owner decision: 1). Kept env-tunable but capped.
F5_LOANWORD_MAX_RETRY = int(os.getenv("F5_LOANWORD_MAX_RETRY", "1"))

# ---- Slow-term stretch (F5 only) -------------------------------------------
#
# Owner request: the word "prompt" reads too fast even after the word_improve.md
# `prompt,` fix; slow it to ~50% speed (≈2× duration) and keep it smooth. F5 has NO
# per-word duration control, and a per-CHUNK slow (the removed F5_SLOW_TERMS path)
# slows the whole sentence AND F5 dropped the isolated short chunk. So we do it as a
# TARGETED POST-SYNTHESIS pass on the FINAL scene wav: whisper the scene (resident
# CPU model), locate each slow-term's WORD boundaries, time-stretch ONLY that slice
# (pitch-preserving atempo, chained below the 0.5 floor) and splice it back with an
# equal-power crossfade at each edge so there is no click. This slows ONLY the word,
# leaves all other narration at natural pace (so it does NOT worsen whole-video pace
# consistency), and auto-resyncs captions because the assembler's caption whisper runs
# on this same final wav.
#
# Gated behind F5_SLOW_TERMS_ENABLE (default on). F5_SLOW_TERMS = comma list of terms
# (lowercased, matched on the ORIGINAL English word whisper transcribes). F5_SLOW_FACTOR
# = playback-speed factor for the slice: 0.5 = half speed = 2× duration (owner target).
# NEVER above 1.0 (that would speed up). MEASURED TRADEOFF (video 82): atempo=0.5 on a
# short ~200-300 ms F5 "prompt" can smear the /t/ coda (whisper re-hears "Prom"); 0.6
# (≈1.7×) stays cleanly articulated. Default honors the owner's explicit 0.5; set
# F5_SLOW_FACTOR=0.6 for the cleaner-but-less-slow variant.
# DISABLED by default (2026-07-04, bug 2/3). This post-synth per-CHUNK slow located the
# ENGLISH word "prompt" in the whisper transcript and time-stretched that slice. It NEVER
# actually fired for "prompt" because the pron-map respells it to "pờ~rôm" and whisper
# hears "bờ rom", not "prompt" (verified). It is SUPERSEDED by the deterministic tilde (~)
# SLOW-join say_as (see _F5_SLOW_JOIN_FACTOR / word_improve.md), which slows ONLY the
# predefined term as one atempo chunk. Kept as an env escape hatch but OFF by default so it
# cannot introduce a second, whisper-dependent, non-predefined slow pass.
F5_SLOW_TERMS_ENABLE = os.getenv("F5_SLOW_TERMS_ENABLE", "0").strip().lower() not in ("0", "off", "false", "no")
F5_SLOW_TERMS = [t.strip().lower() for t in os.getenv("F5_SLOW_TERMS", "").split(",") if t.strip()]
F5_SLOW_FACTOR = min(1.0, float(os.getenv("F5_SLOW_FACTOR", "0.5")))
# Equal-power crossfade length (s) at each splice edge so the stretched slice blends
# with its neighbours without a click. Env-tunable via F5_SLOW_XFADE_S.
F5_SLOW_XFADE_S = float(os.getenv("F5_SLOW_XFADE_S", "0.020"))

# ---- Acronym tighten (F5 only) ---------------------------------------------
#
# Owner: abbreviations/acronyms must read as ONE tight/compact unit, not drawn out
# letter-by-letter. Root cause (video 83): the pron-map spells an acronym as SPACE-
# separated syllables ("ChatGPT"→"chát ji pi ti"), which F5 reads as discrete words;
# on a slow/unlucky draw that spans >1 s ("chát...dài...phai...ti" — the owner's
# "chai pi ti"). The space form is actually the most RELIABLE say_as (measured: hyphen-
# FAST-join is WORSE — it isolates the acronym into its own chunk with a lead-in seam;
# raw-letter "gpt" garbles). So we KEEP the say_as and instead TIGHTEN the acronym's
# AUDIO after synthesis: locate the acronym region via whisper and gently COMPRESS it
# (atempo>1) toward a compact unit. This is the inverse of the slow-term stretch and
# reuses the same splice infra. Measured (video 83 good draw): atempo 1.3 tightens
# "chát ji pi ti" 760→577 ms with "GPT" still cleanly transcribed; 1.5 → 495 ms.
#
# CAVEAT (honest): compression tightens a NORMAL/mildly-dragged draw well, but it cannot
# un-spell a SEVERELY dragged draw (compressing "chắt dài phai ti" just speeds up the
# garble). The residual driver is F5 per-draw variance; a hard guarantee would need a
# re-render (like loanword-repair). This pass handles the common case; see report.
#
# Gated behind F5_ACRONYM_TIGHTEN (default on). F5_ACRONYM_FACTOR = compression speed
# (>1 = faster/tighter; default 1.3 — gentle, keeps articulation). Only acronym regions
# (2+ uppercase letters in the ORIGINAL narration, incl. the caps of "chatGPT") are
# touched; the caption keeps the original text. NEVER slows (factor is clamped >=1.0).
# DISABLED by default (2026-07-04, bug 2). The acronym-tighten pass compresses (atempo>1)
# an acronym's audio region MID-SENTENCE. That is an AUTOMATIC, NON-predefined per-region
# speed change — it makes part of a sentence read faster than the rest, exactly the
# "intermittently speeds up then returns to normal" the owner reported. The owner's design
# rule (bug 2) is: normal sentence text = ONE constant pace, and ONLY predefined say_as
# terms may have their own speed. So this pass is OFF by default. REGRESSION TRADEOFF: a
# drawn-out spelled acronym ("chát ji pi ti") is no longer auto-compacted, so an unlucky
# F5 draw may read it slightly long/spelled-out. Re-enable with F5_ACRONYM_TIGHTEN=1 only
# if that regression is worse than the constant-pace requirement. (The pron-map say_as +
# the deterministic FAST-join already keep acronyms mostly compact without this pass.)
F5_ACRONYM_TIGHTEN = os.getenv("F5_ACRONYM_TIGHTEN", "0").strip().lower() not in ("0", "off", "false", "no")
F5_ACRONYM_FACTOR = max(1.0, float(os.getenv("F5_ACRONYM_FACTOR", "1.3")))
# Short-acronym coda guard (bug: MCP → "MC", the trailing "pi" clipped). A SHORT acronym
# (e.g. "em xi pi" for MCP, "ây pi ai" for API) already synthesizes as a compact ~0.42-
# 0.46 s unit; compressing that region at 1.3x shrinks its ALREADY-short final syllable
# to ~0.11 s, so on a dense/unlucky draw the last consonant is lost ("MCP"→"MC"). A
# genuinely DRAGGED acronym that tighten was designed for (ChatGPT → "chát ji pi ti")
# measures ~0.54 s — well above this floor — so it keeps being compacted. Measured
# 2026-07-04 (India Review F5 ref, whisper medium, anchor-to-anchor region):
#   MCP 0.42-0.46 s | API 3-syl short | ChatGPT 0.54 s (dragged, still tightened).
# A region SHORTER than this floor is left untouched (already compact — compressing it
# only risks the coda). Duration-driven, so no per-acronym list is needed and it
# automatically protects every short (2-3 letter) acronym while still tightening the
# long dragged ones. Set 0 to disable the floor (restore always-compress). Env-tunable.
F5_ACRONYM_MIN_REGION_S = float(os.getenv("F5_ACRONYM_MIN_REGION_S", "0.50"))
# An acronym in the ORIGINAL narration = a run of 2+ uppercase letters (also catches the
# caps part of a mixed-case token like "chatGPT" → "GPT"). Extend via env if needed; the
# base regex covers all-caps runs generically so no per-term list is required.
_ACRONYM_RE = re.compile(r"[A-Z]{2,}")

# Base loanword set (lowercase, whole-word). English tech terms that F5 rushes even
# when NOT in the pronunciation map (an unmapped raw English word is exactly the case
# that rushes). The pron-map English entries are ADDED to this at load time (see
# _LOANWORDS below) so any term the map already respells is also measured. Keep this
# list easy to extend; add plain English words that testing shows F5 rushes.
_LOANWORD_BASE = {
    "engineering", "engineer", "prompt", "prompting", "context", "harness",
    "system", "agent", "agents", "coding", "loop", "loops", "task", "tasks",
    "token", "tokens", "model", "clone", "website", "framework", "feature",
}
# Vietnamese diacritic vowels count toward syllable counting for VN tokens; for the
# ENGLISH loanwords we approximate spoken syllable count by vowel-group runs (a VN
# reader speaks each English vowel cluster as one syllable). This only needs to be
# roughly right — it just normalises duration so a long word is not falsely flagged.
_VOWELS = "aeiouyàáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵ"


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

# Speed/pause marker control chars — wrap segments of the processed TTS text that
# must be synthesised at a non-1.0 atempo, or that carry an inserted pause. ASCII
# SOH..ENQ never appear in Vietnamese narration, so they are safe in-band markers.
#
# These markers encode the NEW say_as separator semantics. The owner-decided rule
# (source of truth: word_improve.md "Separator semantics") is:
#   - syllables joined by a SPACE      → read NORMALLY (no marker, factor 1.0)
#   - syllables joined by ONE  hyphen  → read FAST  (atempo F5_FAST_FACTOR, ~1.3x)
#   - syllables joined by TWO  hyphens → insert a PAUSE beat (F5_PAUSE_BEAT_S) between
# The hyphen count is about CONSECUTIVE hyphens at one separator position, NOT the
# total number of hyphens in the value (so a year "hai-không-hai-sáu" = all FAST
# joins, while "ây--jừn" = one PAUSE). A no-separator say_as (e.g. "jipit") is a
# single token at normal speed. (No term is ever read SLOW — that path was removed.)
_SPD_FAST_ST = "\x01"   # start: synthesise this segment FAST (atempo F5_FAST_FACTOR)
_SPD_FAST_EN = "\x02"   # end of FAST segment
_SPD_SLOW_ST = "\x03"   # start: synthesise this segment SLOW (atempo F5_SLOW_JOIN_FACTOR)
_SPD_SLOW_EN = "\x04"   # end of SLOW segment
_SPD_PAUSE   = "\x05"   # insert a one-beat pause (F5_PAUSE_BEAT_S) at this position
# SLOW-join separator semantic (bug 3, 2026-07-04). The SLOW markers \x03/\x04 are
# RE-ENABLED for a single, TARGETED purpose: a `say_as` value may join syllables with a
# TILDE ("~") to mark a PREDEFINED per-word slowdown (owner wants "prompt"/"pờ-rôm" ~10%
# slower than the constant sentence pace). A tilde-join is the SLOW counterpart of the
# 1-hyphen FAST join: the joined syllables become ONE atempo chunk at F5_SLOW_JOIN_FACTOR
# (<1.0 = slower), so the slowdown is smooth (model-adjacent, single infer chunk), lands
# on ONLY that word, and auto-resyncs captions (the assembler's caption whisper reads the
# final wav). Tilde never appears in Vietnamese narration or in any other say_as, so it
# can never fire accidentally. This replaces the removed post-synth _slow_scene_terms path
# for "prompt": that path matched the ENGLISH token "prompt" in the whisper transcript, but
# whisper hears the RESPELLED "bờ rom" — so it never fired (verified). The tilde-join is
# applied at synth time to the say_as itself, so it is deterministic and does not depend on
# whisper re-finding the word. It is the ONLY slow path; no other term is ever slowed.

# Tunable atempo / pause factors (env-overridable). Defaults per owner decision.
#   F5_FAST_FACTOR       — atempo for single-hyphen joins so the syllables read as a
#                          tight unit with no audible mid-word gap (also used for years).
#   F5_SLOW_JOIN_FACTOR  — atempo for a TILDE ("~") join: 1.0 = normal speed (owner: prompt).
#   F5_PAUSE_BEAT_S      — silence inserted at a double-hyphen ("--") separator position.
_F5_FAST_FACTOR = float(os.getenv("F5_FAST_FACTOR", "1.05"))
# Clamp to (<=1.0, >=0.5]: a tilde-join ONLY ever slows (never speeds up); 0.5 floor keeps
# atempo in-range without chaining. Default 1.0 = NO slowdown (owner: prompt at normal pace).
# OWNER 2026-07-05 (3rd pass): 0.90 → 0.95. The GAP_TIGHTEN below already reduced the
# between-syllable dead air to ~0.00 s, so the residual "ngưng" the owner heard on
# "pờ~rôm" was the syllable-DRAG itself (10% stretch of "pờ"/"rôm"). Halving the slowdown
# to 5% shortens the drawn-out onset while keeping a slight, intentional slow feel.
# OWNER 2026-07-05 (4th pass): 0.95 → 1.0. Still too much separation at 5% slower, owner
# wants NO drag at all. At 1.0 the tilde adds NO time-stretch: the two syllables read at the
# normal sentence pace. IMPORTANT — the tilde is NOT reduced to a plain space-join: the
# marker still forces "pờ rôm" into its OWN standalone F5 inference chunk (a SLOW segment is
# always its own chunk). But because chunk_atempo == 1.0, the atempo AND the GAP_TIGHTEN
# junction handling are BOTH skipped (both are gated behind `chunk_atempo != 1.0`). So at 1.0
# the "pờ~rôm" chunk is simply F5 reading the two syllables at normal speed as an isolated
# 2-syllable inference, with whatever inter-syllable gap F5 renders natively (GAP_TIGHTEN 0.14
# no longer applies). No divide-by-zero, no broken chunk — the path stays sane, just a no-op
# time-wise. The 0.14 GAP_TIGHTEN below is retained (moot at 1.0, active again if factor < 1.0).
_F5_SLOW_JOIN_FACTOR = min(1.0, max(0.5, float(os.getenv("F5_SLOW_JOIN_FACTOR", "1.0"))))
_F5_PAUSE_BEAT_S = float(os.getenv("F5_PAUSE_BEAT_S", "0.10"))
# SLOW-join JUNCTION tighten (bug: "prompt" / "pờ~rôm" đột nhiên chậm, 2026-07-04). A
# tilde SLOW-join renders both syllables in ONE F5 inference with a real SPACE between
# them, then atempo-slows the WHOLE chunk — which STRETCHES the inter-syllable gap too, so
# "pờ" and "rôm" drift apart and sound dragged / broken into two beats. Owner refinement:
# keep the word's slowed FEEL but read the JUNCTION between the two syllables FASTER so
# they flow as one continuous word. This factor SCALES the internal silence run(s) of a SLOW
# chunk AFTER the atempo slow: it is the FRACTION of the gap that is KEPT (0.40 = keep 40% =
# the junction is ~60% tighter). The SYLLABLE audio (speech samples) is never touched, so each
# syllable stays at the slowed pace — only the dead air BETWEEN them is compressed. 1.0 =
# disable (no tighten). A SLOW chunk is a standalone inference, so its ONLY internal silence
# run is the syllable junction — this can never touch a real sentence pause.
# OWNER 2026-07-04: reduce the kept gap of the "pờ~rôm" tilde junction from 0.80 → 0.40 (keep
# only 40% of the inter-syllable gap, so "pờ" and "rôm" flow much more tightly as one word
# while each syllable stays at the 10%-slower pace).
# OWNER 2026-07-05: tightened further 0.40 → 0.20 (keep only 20% of the inter-syllable gap)
# — the "pờ~rôm" junction was still audibly split into two beats at 0.40; keeping 20% makes
# the two syllables flow as a single word while each still reads at the 10%-slower pace.
# _F5_SLOW_JOIN_FACTOR was 0.90 through these two passes (UNCHANGED then).
# OWNER 2026-07-05 (2nd pass): still perceived too long → reduce a further 30%: 0.20 × 0.70
# = 0.14 (keep only 14% of the inter-syllable gap). NOTE: an A/B probe already measured the
# residual pờ~rôm silence at ~0.00 s at 0.20 — the between-syllable GAP is effectively gone,
# so any residual "ngưng" the owner still hears is the syllable-DRAG from _F5_SLOW_JOIN_FACTOR
# stretching "pờ"/"rôm" themselves, NOT dead air. 0.14 shaves the last few ms of gap
# but cannot remove drag. GAP_TIGHTEN stays 0.14.
# OWNER 2026-07-05 (3rd pass): the drag lever was pulled instead — _F5_SLOW_JOIN_FACTOR
# 0.90 → 0.95 (see above) to shorten the drawn-out onset. GAP_TIGHTEN stays at 0.14.
_F5_SLOW_JOIN_GAP_TIGHTEN = min(1.0, max(0.05, float(os.getenv("F5_SLOW_JOIN_GAP_TIGHTEN", "0.14"))))

# A2: leading/trailing silence pad kept around a FAST (1-hyphen join / year) segment
# after its atempo time-stretch. Target: ~80% reduction from the original 0.12 s pad →
# 0.12 * 0.20 = 0.024 s. Trims air ONLY at the edges of a hyphenated unit (never
# clips speech), so "ây-jừn" / "hai-không-hai-sáu" sit tight against neighbours.
# Env-overridable via F5_FAST_TRIM_PAD_S.
_FAST_TRIM_PAD_S = float(os.getenv("F5_FAST_TRIM_PAD_S", "0.0240"))



def _parse_word_improve(md_path: str) -> list[tuple[str, str, str]]:
    """Parse word_improve.md's pipe table into (term, say_as, say_as_vieneu) triples.

    F5/VieNeu ONLY. Reads ONLY rows under a markdown table whose header contains
    'term' and 'say_as'. The DEFAULT say_as column (F5-tuned) is MANDATORY — a row
    with an empty say_as is skipped entirely, so it reaches neither the regexes, the
    replacement maps, nor the loanword set. One OPTIONAL override column may follow:

      • 'say_as_vieneu' — VieNeu-specific override (ONNX v3turbo). Non-empty →
        used for the vieneu engine; empty → FALL BACK to the default say_as.

    The OmniVoice column (say_as_omnivoice) is deliberately NOT read here: OmniVoice
    owns its own parser in omnivoice_worker.py. Keeping it out is what guarantees that
    an OmniVoice-only row (empty F5 say_as, e.g. `| RAG |  |  | Rát |`) can never leak
    into the F5-visible regex or into _LOANWORDS — which would silently change which
    chunks F5 re-rolls for loanword repair.

    Column order is discovered from the header, so the override column may be absent
    or in any position; extra columns are ignored.

    Skips the header, the |---| separator, blank say_as cells, and any non-table
    prose. Returns triples sorted LONGEST-term-first so multi-word terms match before
    their parts. say_as_vieneu is "" when not provided. Missing/corrupt file -> []
    (no-op, never fatal)."""
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
        # Header row: enables table parsing and locates the optional override column.
        if "term" in low and any("say_as" in c or "say as" in c for c in low):
            in_table = True
            vn_col = None
            for idx, c in enumerate(low):
                is_sayas = "say_as" in c or "say as" in c
                # Match the VieNeu override header (say_as_vieneu / say as vieneu).
                if is_sayas and "vieneu" in c and vn_col is None:
                    vn_col = idx
            continue
        if not in_table:
            continue
        # Separator row like |---|---|--- -> skip.
        if all(set(c) <= set("-: ") for c in cells if c):
            continue
        if len(cells) < 2:
            continue
        term, say_as = cells[0], cells[1]
        say_as_vieneu = ""
        if vn_col is not None and vn_col < len(cells):
            say_as_vieneu = cells[vn_col].strip()
        # A row without an F5 say_as is not an F5/VieNeu entry at all (it exists only
        # for another engine's column) — skip it so nothing downstream ever sees it.
        if not term or not say_as:
            continue
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
    longest-first regexes (case-insensitive for word terms, case-sensitive for all-caps
    acronyms) plus the F5 and VieNeu replacement maps.

    Returns (regex_ci, regex_cs, repl_default, repl_vieneu) or (None, None, {}, {}).
    Every term reaching this function has a non-empty F5 say_as (the parser drops the
    rest), so the regexes and repl_default hold exactly the same term set — the regex
    can never match a term the map cannot replace. repl_vieneu ONLY contains terms that
    declared an override; VieNeu lookups fall back to repl_default when absent.

    OmniVoice is NOT handled here — it compiles its own map from its own column in
    omnivoice_worker.py, so nothing about that engine can alter this term set.

    Acronym terms (see _is_acronym_term) go into regex_cs (case-sensitive, NO IGNORECASE)
    so the lowercase form of the token is left untouched; every other term goes into
    regex_ci (IGNORECASE) — preserving the previous behavior for them.

    Per-syllable reading SPEED is parsed at apply time from the say_as value's separators
    (space=normal / 1-hyphen=FAST / 2-hyphen=PAUSE); see word_improve.md "Separator
    semantics". There is NO SLOW path any more."""
    if not triples:
        return None, None, {}, {}
    repl_default: dict[str, str] = {}
    repl_vieneu: dict[str, str] = {}
    for term, say_as, say_as_vieneu in triples:
        key = term.lower()
        repl_default.setdefault(key, say_as)
        if say_as_vieneu:
            repl_vieneu.setdefault(key, say_as_vieneu)
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


def _build_loanwords() -> set[str]:
    """Loanword set = _LOANWORD_BASE ∪ the ENGLISH terms from the pronunciation map.
    A pron-map term is treated as a loanword when it contains an ASCII letter (i.e. it
    is an English/acronym term, not a Vietnamese-diacritic respelling). Multi-word terms
    contribute each ASCII word. All lowercased. This means any term the map already
    respells is ALSO measured for rushing, and the base list catches the ones the map
    leaves raw (like the previously-unmapped "engineering").

    Scope note: _PRON_PAIRS holds F5-VISIBLE rows only (the parser drops any row with an
    empty say_as), so a term that exists purely for another engine's column never enters
    this set. That matters — this set gates the loanword re-roll in _run_f5, so leaking
    foreign rows in here changes which chunks F5 re-draws."""
    words = set(_LOANWORD_BASE)
    for term, _say, _vn in _PRON_PAIRS:
        for part in re.split(r"[\s\-]+", term):
            p = part.strip().lower()
            if p and re.search(r"[a-z]", p):
                words.add(p)
    return words


_LOANWORDS = _build_loanwords()


# Years the pipeline reads DIGIT-BY-DIGIT. Mirrors the expansion regex in
# _normalize_years / omnivoice_worker._expand_years_spacejoined, which both match
# `\b(20\d{2})\b` and nothing else — so a 19xx/17xx year is NOT digit-exploded and must be
# counted by magnitude ("1736" → "một nghìn bảy trăm ba mươi sáu").
_YEAR_TOKEN_RE = re.compile(r"^20\d{2}$")
_DIGIT_RUN_RE = re.compile(r"\d+")


def _vi_number_syllables(n: int) -> int:
    """Spoken-syllable count of a Vietnamese number read by MAGNITUDE.

    DUPLICATED from generate._vi_number_syllables ON PURPOSE — identical algorithm, shared
    test cases (test/test_spoken_weight.py ↔ test/test_tts_worker_syllables.py). It is not
    imported because this worker runs in cf-venv as a standalone script: importing
    generate.py would add ~0.8 s to every worker spawn and pull the whole FastAPI/DB layer
    into a TTS subprocess. Twenty lines of arithmetic with no dependencies is the cheaper
    trade. KEEP THE TWO IN SYNC.

        n < 10   → 1                 n < 20  → 1 + unit        n < 100 → 2 + unit
        n < 1000 → 2 (trăm) + (0 | 2 for "linh"+unit | syllables of the 10-99 remainder)
        n ≥ 1000 → group + magnitude word + remainder (APPROXIMATE: skips the "không trăm"
                   filler; this is a duration proxy, and <1000 is where narration lives)."""
    n = abs(int(n))
    if n < 10:
        return 1
    if n < 20:
        return 1 + (1 if n % 10 else 0)
    if n < 100:
        return 2 + (1 if n % 10 else 0)
    if n < 1000:
        rem = n % 100
        if rem == 0:
            return 2
        if rem < 10:
            return 4
        return 2 + _vi_number_syllables(rem)
    for div in (1_000_000_000, 1_000_000, 1_000):
        if n >= div:
            head = _vi_number_syllables(n // div) + 1
            rem = n % div
            return head + (_vi_number_syllables(rem) if rem else 0)
    return 1


def _count_syllables(word: str) -> int:
    """Spoken-syllable count, used to normalise a measured duration to ms/syllable so a
    long word isn't falsely flagged as rushed vs a short one.

    Base = vowel-group runs, plus the two corrections mirrored from generate.py
    (owner 2026-08-02) so the worker and the API measure the same word the same way:

      • NUMBERS read by MAGNITUDE. A numeral has no vowel, so the plain vowel-group count
        scored EVERY number as 1 syllable — "427" ("bốn trăm hai mươi bảy", 5) and "20.000"
        ("hai mươi nghìn", 3) both counted 1, making any word measured against them look
        wildly slow. Isolated 20xx years stay digit-by-digit because the text-prep expands
        them that way before synthesis ("2024" → "hai không hai tư" = 4).
      • English SILENT FINAL E. "runtime" is spoken ~2 syllables but scored 3 (u/i/e);
        "file"/"code" score 2 but are 1. Exceptions: a vowel before the final e ("free"),
        a syllabic consonant+"le" ("table"), and never reducing below one group — which is
        what protects Vietnamese "xe"/"che"/"nghe" and English "the". Only the bare ASCII
        'e' is affected; Vietnamese ê/è/é/ẻ/ẽ/ẹ are real vowels and are never stripped."""
    w = word.strip().lower()
    letters = [c for c in w if c.isalpha()]
    digits = [c for c in w if c.isdigit()]
    if digits:
        runs = _DIGIT_RUN_RE.findall(w)
        if runs and all(_YEAR_TOKEN_RE.match(r) for r in runs):
            return max(1, len(digits) + len(letters))   # year: one syllable per digit
        return max(1, sum(_vi_number_syllables(int(r)) for r in runs) + len(letters))
    runs_n, in_v = 0, False
    for ch in w:
        v = ch in _VOWELS
        if v and not in_v:
            runs_n += 1
        in_v = v
    alpha = "".join(letters)
    if runs_n > 1 and len(alpha) >= 2 and alpha[-1] == "e":
        prev = alpha[-2]
        syllabic_le = (prev == "l" and len(alpha) >= 3 and alpha[-3] not in _VOWELS)
        if prev not in _VOWELS and not syllabic_le:
            runs_n -= 1
    return max(1, runs_n)


def _norm_token(s: str) -> str:
    """Lowercase + strip surrounding punctuation for token compares (whisper adds a
    leading space and trailing '.,?'). Keeps interior letters/digits."""
    return re.sub(r"^[^\w]+|[^\w]+$", "", s.strip().lower())


def _loanwords_in_chunk(chunk_text: str) -> list[str]:
    """Return the loanwords present in a chunk's SPOKEN text (lowercased), in order.
    Empty list ⇒ the chunk is pure-Vietnamese and must NOT be measured (cost guard)."""
    found = []
    for tok in re.findall(r"[^\W]+", chunk_text.lower(), flags=re.UNICODE):
        if tok in _LOANWORDS:
            found.append(tok)
    return found


def _mapped_loanwords_in_chunk(chunk_text: str) -> list[str]:
    """Loanwords whose PRON-MAP RESPELLING is present in a chunk's spoken text.

    Bug ("mắt" dropped at the ChatGPT|year seam): the chunk is built from the ALREADY-
    respelled spoken text, so a loanword the pron-map rewrote (e.g. ChatGPT → "chát ji
    pi ti", API → "ây pi ai") no longer appears as its English token — `_loanwords_in_chunk`
    misses it, so the loanword-repair re-roll NEVER fires for those chunks even when F5
    garbles the acronym AND swallows an adjacent short word ("ra mắt"). This finds the
    ORIGINAL loanword by matching its say_as respelling (stripped of speed/pause markers)
    inside the chunk. A spelled-acronym say_as like "ji pi ti" is a run of short syllables,
    exactly the dense region where F5 drops words — flagging it re-enables the existing
    best-of-N re-roll for those chunks. Returns the ORIGINAL English tokens (what whisper
    should hear), so _measure_loanwords can score them. Whole-say_as substring match on the
    marker-stripped chunk; deduped, order-preserving."""
    if not _PRON_REPL:
        return []
    # Strip in-band speed/pause markers so the say_as text matches the raw chunk.
    bare = chunk_text
    for mk in (_SPD_FAST_ST, _SPD_FAST_EN, _SPD_PAUSE):
        bare = bare.replace(mk, " ")
    bare_low = re.sub(r"\s+", " ", bare.lower())
    found: list[str] = []
    seen: set[str] = set()
    for term, say_as in _PRON_REPL.items():
        # Only ENGLISH/acronym terms are loanwords F5 may rush (say_as containing a
        # Vietnamese respelling of an English term). Skip pure-diacritic non-English.
        if not re.search(r"[a-z]", term):
            continue
        say_bare = re.sub(r"[-]+", " ", say_as)  # say_as separators → spaces for match
        say_bare = re.sub(r"\s+", " ", say_bare.strip().lower())
        if not say_bare:
            continue
        # whole-token boundary match of the say_as run inside the chunk
        if re.search(r"(?<![^\W])" + re.escape(say_bare) + r"(?![^\W])", bare_low):
            # emit each ENGLISH sub-token of the ORIGINAL term (what whisper hears)
            for part in re.split(r"[\s\-]+", term):
                p = part.strip().lower()
                if p and re.search(r"[a-z]", p) and p not in seen:
                    seen.add(p)
                    found.append(p)
    return found


_YEAR_DIGIT_VN = ['không', 'một', 'hai', 'ba', 'bốn', 'năm', 'sáu', 'bảy', 'tám', 'chín']


def _encode_separators(say_as: str) -> str:
    """Translate a say_as value's syllable separators into in-band speed/pause
    markers per the owner's separator semantics (source of truth: word_improve.md
    "Separator semantics"). Counts CONSECUTIVE hyphens at each separator position:

      space        → keep the space, read NORMALLY (no marker).
      ONE hyphen   → join the two parts FAST: both parts go inside ONE _SPD_FAST_*
                     run, joined by a real space (atempo F5_FAST_FACTOR removes the
                     gap so they read as a tight unit, no audible mid-word break).
      TWO+ hyphens → insert a _SPD_PAUSE marker between the parts (one beat of
                     silence, F5_PAUSE_BEAT_S), then continue reading normally.
      TILDE "~"    → join the two parts into ONE _SPD_SLOW_* run read at speed
                     F5_SLOW_JOIN_FACTOR (def 1.0 = NORMAL speed; < 1.0 = slower). This is
                     the PREDEFINED per-word speed slot (owner: "prompt"/"pờ-rôm"). It is
                     the counterpart of the 1-hyphen FAST join and, even at 1.0, still
                     isolates the term into its own inference chunk.

    Examples:
      "ji pi ti"         → "ji pi ti"                       (all normal)
      "ây-jừn"           → "\x01ây jừn\x02"                 (one FAST run)
      "ây--jừn"          → "ây\x05jừn"                      (one PAUSE)
      "pờ~rôm"           → "\x03pờ rôm\x04"                 (one SLOW run; normal at factor 1.0)
      "hai-không-hai-sáu"→ "\x01hai không hai sáu\x02"      (year, all FAST)
      "ji-pit ka"        → "\x01ji pit\x02 ka"             (FAST run + normal)

    Adjacent single-hyphen joins coalesce into ONE contiguous FAST run (so a year's
    digits are one sped-up unit, not many tiny ones); adjacent tildes coalesce into one
    SLOW run. A double-hyphen breaks the run. A no-separator value (e.g. "jipit") is
    returned unchanged (single token, normal). Pre-existing markers in the value are
    left as-is (callers never pass them)."""
    if "-" not in say_as and "~" not in say_as:
        return say_as
    # Split keeping the separators so we can inspect each run. A "separator" here is
    # a run of spaces, a run of hyphens, or a run of tildes (possibly padded by spaces
    # we collapse — a say_as never intentionally mixes " - "). Tokenize on those runs.
    # Strategy: walk the string, emitting syllable text and, between syllables, deciding
    # FAST-join (1 hyphen) vs PAUSE (2+ hyphens) vs SLOW-join (tilde) vs normal (space).
    import re as _re
    # Split into syllables and the separator that FOLLOWS each (last has none). A
    # separator token is one or more hyphens/tildes optionally padded by spaces, OR
    # pure spaces. We capture the separator so we can classify it.
    parts = _re.split(r'(\s*[-~]+\s*|\s+)', say_as)
    # parts = [syl, sep, syl, sep, ..., syl]; even idx = syllable, odd = separator.
    #
    # Classify each separator into a JOIN KIND, then group syllables into maximal runs
    # of the same kind. A syllable's run kind is set by the separator that FOLLOWS it
    # (FAST/SLOW join) — the LAST syllable of a run has a NON-join separator after it
    # (space/pause/end) which closes the run.
    #   kind: "fast" (1 hyphen), "slow" (tilde), else the syllable is a lone NORMAL token.
    syllables = [parts[i] for i in range(0, len(parts), 2)]
    seps = [parts[i] for i in range(1, len(parts), 2)]  # len == len(syllables)-1

    def _kind(sep: str) -> str:
        if "~" in sep:
            return "slow"
        if sep.count("-") >= 2:
            return "pause"
        if sep.count("-") == 1:
            return "fast"
        return "space"

    out: list[str] = []
    run: list[str] = []       # syllables accumulated in the current join run
    run_kind = "space"        # "fast" | "slow" for an open join run

    def _flush():
        nonlocal run, run_kind
        if run:
            if run_kind == "slow":
                # A tilde-join ALWAYS wraps (even one syllable): its whole purpose is to
                # slow THIS word, so a lone tilde-joined syllable must still be slowed.
                out.append(_SPD_SLOW_ST + " ".join(run) + _SPD_SLOW_EN)
            elif run_kind == "fast" and len(run) > 1:
                out.append(_SPD_FAST_ST + " ".join(run) + _SPD_FAST_EN)
            else:
                out.append(" ".join(run))  # single FAST syllable → no wrap needed
            run = []
            run_kind = "space"

    for idx, syl in enumerate(syllables):
        following = _kind(seps[idx]) if idx < len(seps) else "space"
        if syl:
            # If this syllable is join-bound to the NEXT one, it belongs to a join run of
            # that kind. Open/switch the run kind when a join begins.
            if following in ("fast", "slow"):
                if run and run_kind != following:
                    _flush()
                run_kind = following if not run else run_kind
                if not run:
                    run_kind = following
                run.append(syl)
                continue
            # Non-join separator follows: this syllable ends the current run (if any),
            # else it is a standalone NORMAL token.
            if run and run_kind in ("fast", "slow"):
                run.append(syl)  # last syllable of the join run
                _flush()
            else:
                out.append(syl)
        # Emit the separator effect AFTER placing the syllable.
        if idx < len(seps):
            if following == "pause":
                _flush()
                out.append(_SPD_PAUSE)
            elif following == "space":
                _flush()
                out.append(" ")
            # "fast"/"slow" separators are consumed by the run accumulation above.
    _flush()
    return "".join(out)


def _normalize_thousands_sep(text: str) -> str:
    """Rewrite a digit-GROUPING comma ('4,000', '1,000,000') to a period ('4.000') so
    the number reads as ONE grouped quantity instead of breaking at the comma. A comma
    with a digit on BOTH sides AND followed by exactly 3 digits then a non-digit is a
    thousands separator; a decimal comma ('3,14') or a list comma after a number
    ('5, sau đó') is left untouched. Repeats to catch multiple groups ('1,000,000').

    ENGINE-NEUTRAL text normalization — no speed markers, no say_as. Shared by the F5
    pron-map path (step 0) and the OmniVoice neutral normalizer so both handle grouped
    numbers identically."""
    for _ in range(4):
        new = re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))', '.', text)
        if new == text:
            break
        text = new
    return text


# ---- Decimal / version point (owner request 2026-07-28, "option A") ---------
# NO engine (OmniVoice, F5, VieNeu) verbalizes a decimal point: "GPT 5.6" was handed
# through verbatim and every engine SWALLOWED the "." — the delivered video reads
# "GPT năm sáu" (confirmed by whisper on video 258). Vietnamese also uses "." as the
# THOUSANDS separator (and "," as the decimal one), so the dot carries no decimal
# meaning to a Vietnamese reader either. We therefore spell the separator out as a
# WORD and leave the digits alone (the engines already read bare digits correctly).
#
# Two readings existed because both occur in the same script:
#   • VERSION number after a product/model name  -> "chấm"  ("GPT 5.6" -> "GPT 5 chấm 6")
#   • ordinary decimal                            -> "phẩy"  ("2.5 đô"  -> "2 phẩy 5 đô")
# The term list decides which; it is env-tunable because model names keep appearing.
#
# DEFAULT CHANGED 2026-08-18 (job 340/341/345, OmniVoice): "phẩy" turned out to be an
# unreliable draw for OmniVoice regardless of context — the SAME text, re-synthesized with
# no change, dropped "phẩy" entirely on some draws and kept it on others (measured: 2/3 fresh
# draws of "24.7 đô" — no quantifier, no version term nearby — lost the separator; a plain
# "GBD 5.6" that doesn't match any VI_VERSION_TERMS entry hit the same default and failed the
# same way). "chấm" never failed once across ~10 varied trials (with/without a quantifier
# later in the clause, with/without a version-term prefix). So VI_DECIMAL_SEP_WORD now
# defaults to the SAME word as VI_VERSION_SEP_WORD — the version-vs-decimal branch below is
# kept (not deleted) so the two can still be told apart via env if a future need justifies it.
VI_DECIMAL_SEP_WORD = os.getenv("VI_DECIMAL_SEP_WORD", "chấm")
VI_VERSION_SEP_WORD = os.getenv("VI_VERSION_SEP_WORD", "chấm")
_VI_VERSION_TERMS_DEFAULT = (
    "gpt,chatgpt,claude,grok,gemini,llama,mistral,qwen,deepseek,kimi,copilot,"
    "mythos,fable,opus,sonnet,haiku,sole,terra,luna,"
    "bản,phiên bản,version,ver,v"
)
VI_VERSION_TERMS = {
    t.strip().lower()
    for t in os.getenv("VI_VERSION_TERMS", _VI_VERSION_TERMS_DEFAULT).split(",")
    if t.strip()
}
# An isolated "<int>.<frac>": no digit/dot may touch either side, so a grouped number
# ("1.000.000") never matches even partially, while a sentence-final "5.6." still does.
_VI_DECIMAL_RE = re.compile(r'(?<![\d.])(\d+)\.(\d+)(?!\d)(?!\.\d)')


# A magnitude quantifier ANYWHERE later in the same clause ("1.6 nghìn tỷ", "...14.8 lên 32
# nghìn tỷ token,") forces 'chấm' regardless of the prefix check below (job-340/job-341
# findings, see _decimal_sep_for). Clause = up to the next punctuation mark, matching the
# span OmniVoice actually synthesizes in one generate() call (_split_punct_units).
_VI_MAGNITUDE_QUANTIFIERS = {"nghìn", "ngàn", "triệu", "tỷ", "tỉ"}
_VI_CLAUSE_END_RE = re.compile(r'[.,;:!?…]')


def _decimal_sep_for(prefix: str, suffix: str = "") -> str:
    """Pick 'chấm' (version) or 'phẩy' (decimal) from the words around the number.

    'GPT 5.6' / 'GPT-5.6' -> version; '2.5 đô' -> decimal. The hyphen form is stripped
    so 'GPT-5.6' resolves to the same term as 'GPT 5.6'.

    CLAUSE-SUFFIX check: OmniVoice SWALLOWS 'phẩy' whole whenever a magnitude quantifier
    occurs anywhere later in the SAME clause (bounded by the next punctuation mark) — not
    only when it immediately follows the fraction. First measured job-340 ("1.6 nghìn tỷ
    tham số" -> immediate adjacency: "1 phẩy 6 tỷ" survives as "1,6 tỷ" but "1 phẩy 6 NGHÌN
    tỷ" collapses to "một, sáu nghìn tỷ", separator dropped). Then job-341 disproved
    "immediate adjacency" as the real boundary: "...14.8 LÊN 32 nghìn tỷ token," has "lên"
    (not a quantifier) directly after the fraction, yet waveform inspection (not just the
    transcript — whisper's LM can reconstruct a decimal from context even when nothing was
    said) showed a true ~100ms digital-silence gap exactly where 'phẩy' belongs, matching
    the job-340 failure signature; the SAME sentence with 'chấm' showed real (if brief)
    voiced energy in that gap instead of a hard drop to noise floor. So the trigger is
    "quantifier present anywhere in this clause", and 'chấm' survives at any position
    tested so far. This check runs BEFORE the version-term one below so it wins even for a
    version number that happens to share a clause with a quantifier (no realistic case
    where that combination would occur with different intent)."""
    clause_end = _VI_CLAUSE_END_RE.search(suffix)
    clause_tail = suffix[:clause_end.start()] if clause_end else suffix
    tail_tokens = {
        t.strip(".,;:!?\"'()[]").lower()
        for t in re.findall(r"\S+", clause_tail)
    }
    if tail_tokens & _VI_MAGNITUDE_QUANTIFIERS:
        return VI_VERSION_SEP_WORD
    tail = prefix.rstrip().rstrip("-–—").rstrip()
    tokens = re.findall(r"[^\s\-–—]+", tail)[-2:]
    tokens = [t.strip(".,;:!?\"'()[]").lower() for t in tokens]
    if not tokens:
        return VI_DECIMAL_SEP_WORD
    if tokens[-1] in VI_VERSION_TERMS or " ".join(tokens) in VI_VERSION_TERMS:
        return VI_VERSION_SEP_WORD
    return VI_DECIMAL_SEP_WORD


def _expand_decimal_point(text: str) -> str:
    """Spell a decimal/version point as a Vietnamese WORD so the engine cannot drop it.

    Digits are left as digits (every engine reads those correctly); only the '.' becomes
    'chấm'/'phẩy'. A 3-digit fraction is SKIPPED — '4.000' is the Vietnamese thousands
    grouping (and is exactly what _normalize_thousands_sep produces from '4,000'), not a
    decimal. ENGINE-NEUTRAL: no speed markers, no say_as, so both the OmniVoice neutral
    normalizer and the F5/VieNeu pron-map path can share it."""
    if not text:
        return text

    def _sub(m: "re.Match") -> str:
        whole, frac = m.group(1), m.group(2)
        if len(frac) == 3:
            return m.group(0)  # thousands group ("4.000"), not a decimal
        sep = _decimal_sep_for(text[:m.start()], text[m.end():])
        return f"{whole} {sep} {frac}"

    return _VI_DECIMAL_RE.sub(_sub, text)


# --- Slash dates ("24/2" -> "24 tháng 2") --------------------------------------------
# Owner rule: a slash date must be SPOKEN in words ("24 tháng 2") while the CAPTION keeps
# the compact "24/2" — the same display-vs-spoken split as a word_improve.md respelling or
# the digit-by-digit year rule. Engine-neutral: the digits stay digits (every engine reads
# bare digits correctly), only the '/' becomes "tháng"/"năm", so there are no speed markers
# and both the OmniVoice neutral path and the F5/VieNeu pron-map path share this function.
#
# A bare "d/m" is genuinely AMBIGUOUS in Vietnamese narration — it reads just as plausibly
# as a fraction ("1/2"), a ratio ("5/10"), an aspect ratio ("16/9") or the idiom "24/7". So
# we expand ONLY the shapes that are confidently a date:
#   1. three-part "d/m/yyyy"      -> always a date ("24/2/2026")
#   2. date cue word + "d/m"      -> "ngày|mùng|mồng|hôm|sáng|trưa|chiều|tối|đêm 1/2"
#   3. bare "d/m" with day >= 13  -> 13-31 cannot be a month, so the fraction/ratio
#                                    reading is implausible ("24/2" — the owner's example)
# ...minus _NON_DATE_PAIRS, the well-known non-dates that would otherwise pass rule 3.
# The asymmetry is deliberate: a MISSED date is merely read as "hai mươi tư gạch chéo hai",
# while a FALSE POSITIVE turns the idiom "24/7" into "24 tháng 7", which is plainly wrong.
# A bare small-number date ("1/2") therefore needs its cue word ("ngày 1/2") to be spoken
# as a date. Env VI_DATE_EXPAND=0 disables the whole step.
_VI_DATE_CUES = ("ngày", "mùng", "mồng", "hôm", "sáng", "trưa", "chiều", "tối", "đêm")
# Non-dates that satisfy "day >= 13 and month <= 12": the round-the-clock idiom and the
# wide aspect ratios. Written as (day, month) pairs exactly as they appear before the '/'.
_NON_DATE_PAIRS = {(24, 7), (16, 9), (16, 10), (21, 9)}
# Guards: no digit or '/' may touch either side, so a longer run ("1/2/3/4", "100/2") or an
# already-expanded fragment can never match partially. The year group is optional.
_VI_DATE_RE = re.compile(r'(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(\d{4}))?(?![\d/])')


def _expand_vn_dates(text: str) -> str:
    """Expand a slash date to its spoken Vietnamese form: '24/2' -> '24 tháng 2',
    '24/2/2026' -> '24 tháng 2 năm 2026'. SPOKEN text only — the caption keeps '24/2'.

    Leading zeros are dropped ('04/02' -> '4 tháng 2') so the engine cannot read the '0'
    as "không". The year is emitted as DIGITS on purpose: the year rule that runs after
    this (_normalize_years here, or omnivoice_worker._expand_years_spacejoined on the
    OmniVoice path) then expands it digit-by-digit per
    the owner's year rule, giving '24 tháng 2 năm hai không hai sáu'. See the block comment
    above for why only confident date shapes are expanded."""
    if not text or os.getenv("VI_DATE_EXPAND", "1").strip().lower() in ("0", "off", "false", "no"):
        return text

    def _sub(m: "re.Match") -> str:
        day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
        if not (1 <= day <= 31 and 1 <= month <= 12):
            return m.group(0)  # not a calendar date at all (e.g. "50/50", "20/80")
        if year is None:
            # Rule 2 — a date cue word immediately before the number.
            prefix = text[:m.start()].rstrip()
            last = re.findall(r"[^\s]+", prefix)[-1:]
            cued = bool(last) and last[0].strip(".,;:!?\"'()[]").lower() in _VI_DATE_CUES
            # Rule 3 — day 13-31 is unambiguous, unless it is a known non-date.
            if not cued and (day < 13 or (day, month) in _NON_DATE_PAIRS):
                return m.group(0)
        spoken = f"{day} tháng {month}"
        return f"{spoken} năm {int(year)}" if year is not None else spoken

    return _VI_DATE_RE.sub(_sub, text)


def _normalize_years(text: str) -> str:
    """Expand 4-digit years (20xx) to digit-by-digit Vietnamese, joined with single
    hyphens so the NEW separator rule reads them FAST (digit-by-digit, no gap).

    Owner rule: years are read digit by digit, not as full number words.
      2022 → "hai-không-hai-hai"   2026 → "hai-không-hai-sáu"
    The hyphen-joined string is later passed through _encode_separators (step 4 of
    _apply_pron_map), which turns the single-hyphen joins into ONE contiguous FAST
    run (atempo F5_FAST_FACTOR). This keeps the year atomic (one sped-up segment,
    no chunk-boundary gap between digits) AND read fast. We do NOT wrap the year in
    markers here — _encode_separators handles the separator semantics uniformly so
    the year path and the say_as path can never double-wrap or fight each other.
    Only matches isolated 20xx tokens (word boundaries) to avoid touching larger
    numbers like "20000 người". Range covers 2000-2099.

    F5_YEAR_INLINE (default on): join the digit-words with SPACES instead of single
    hyphens. A hyphen-joined year becomes its OWN FAST atempo chunk (separate F5
    inference), and F5 prepends a long breath-level LEAD-IN to that chunk — the audible
    BREAK heard before a number ("...vào đầu [break] 2026"). That lead-in sits at
    speech-adjacent energy (~-28..-38 dB), so it cannot be removed by silence
    trimming/compression without risking soft consonants elsewhere. Reading the year
    INLINE (space-joined) keeps it in the SAME inference as the surrounding words, so
    F5 reads "...vào đầu hai không hai sáu" continuously with no inter-chunk break —
    while still honouring the owner's "digit by digit" rule (each digit is its own
    word, just at normal narration speed, not sped up). Set F5_YEAR_INLINE=0 to restore
    the FAST hyphen-joined year (its own sped-up chunk)."""
    inline = os.getenv("F5_YEAR_INLINE", "1").strip().lower() not in ("0", "off", "false", "no")
    join = " " if inline else "-"
    def _yr(m: "re.Match") -> str:
        return join.join(_YEAR_DIGIT_VN[int(c)] for c in m.group(1))
    return re.sub(r'\b(20\d{2})\b', _yr, text)


def _apply_pron_map(text: str, engine: str = "f5") -> str:
    """Replace mapped terms in SPOKEN text with their say_as respelling. Whole-word,
    case-insensitive. APPLIED ONLY to TTS input — never to caption/narration text.

    `engine` selects the say_as variant: for "vieneu" a term's VieNeu-specific
    override is used when one exists, else the default (F5) say_as. Any other engine
    value (incl. the default "f5") uses ONLY the default map.

    Acronym terms (AI, API, …) are matched CASE-SENSITIVELY via _PRON_RE_CS so the
    Vietnamese lowercase pronoun "ai" is left untouched; all other terms are matched
    case-insensitively via _PRON_RE_CI.

    Processing order (important):
      1. Digit-unit hyphens → space  ("4-nghìn" → "4 nghìn")
      1a. Slash dates → words        ("24/2" → "24 tháng 2"); before the year step so a
                                      "24/2/2026" hands its year to step 2
      2. Year normalization          (2022 → "hai-không-hai-hai", hyphen-joined)
      3. Pronunciation map           (each matched term's say_as is separator-encoded
                                      IN PLACE — its space/1-hyphen/2-hyphen separators
                                      become normal/FAST/PAUSE markers via
                                      _encode_separators).
      4. Year separator-encoding     (the hyphen-joined year from step 2 is encoded the
                                      same way → ONE FAST run; digit-by-digit, no gap).

    The separator semantics (space=normal, 1 hyphen=FAST, 2 hyphens=PAUSE) are the
    SINGLE source of truth in word_improve.md. _encode_separators implements them and
    is applied PER say_as value (and per year), NOT over the whole sentence, so a
    space between two ordinary narration words is never misread as a say_as separator.
    (No SLOW path exists — no term is ever slowed.)"""
    if not text:
        return text

    # 0. Thousands-separator comma → period (read continuously, no clause pause).
    #    A comma BETWEEN digits ("4,000", "1,000,000") is a thousands separator, but F5
    #    reads the comma as a clause PAUSE — so "4,000 tokens" breaks at the comma while
    #    "4.000 tokens" (period separator) reads continuously. We rewrite a digit-grouping
    #    comma to a period so it gets the smooth, no-pause reading. We ONLY touch a comma
    #    that has a digit on BOTH sides AND is followed by exactly 3 digits then a
    #    non-digit (the thousands-group shape), so a genuine decimal comma like "3,14" or a
    #    list comma after a number ("5, sau đó") is left untouched. Env F5_NUM_COMMA_FIX=0
    #    disables. Applied BEFORE year normalization so years (no comma) are unaffected.
    if os.getenv("F5_NUM_COMMA_FIX", "1").strip().lower() not in ("0", "off", "false", "no"):
        # Shared engine-neutral thousands-separator fix (catches "1,000,000" too).
        text = _normalize_thousands_sep(text)

    # 0a. Decimal / version point → a spoken Vietnamese word. Shared with the OmniVoice
    #     neutral normalizer (_expand_decimal_point): F5 and VieNeu drop a bare "." the
    #     same way OmniVoice does, so "GPT 5.6" must become "GPT 5 chấm 6" here too.
    #     AFTER the thousands fix so a just-created "4.000" is recognized and skipped.
    text = _expand_decimal_point(text)

    # 0b. Parenthetical em-dash → comma pause. F5 vocalizes a literal SPACED " - " as a
    #    spurious voiced vowel AT the dash and destabilizes the adjacent clip boundary.
    #    CONFIRMED (raw whisper, scene "...scope lớn - ... một website - prompt..."): a
    #    phantom "ớ"/"ở" syllable appears right after "lớn" and an "AperZoom" blurt right
    #    after "website" — both sitting exactly ON the " - " dashes, with NO silence gap
    #    (so the trailing-halluc trim keeps them, classified as the word's own tail).
    #    Rewriting the dash to a comma removes BOTH artifacts (re-verified: clean "lớn,
    #    chẳng" and "website, ... engineering") and gives the parenthetical the natural
    #    short comma pause it wants — inside ONE clip, so no clip-end hallucination. Only a
    #    SPACED hyphen (a parenthetical dash) or a real en/em dash is rewritten; a
    #    word-internal hyphen ("sub-agent", "front-end") or digit range ("2-3") has no
    #    surrounding spaces and is left for the compound/year handling below. SPOKEN text
    #    only — the caption keeps the original dash. Env F5_EMDASH_COMMA=0 to disable.
    if os.getenv("F5_EMDASH_COMMA", "1").strip().lower() not in ("0", "off", "false", "no"):
        text = re.sub(r'\s+-+\s+|\s*[–—]\s*', ', ', text)

    # 1. Digit-unit hyphens → space ("4-nghìn" → "4 nghìn").
    text = re.sub(r'(\d+)-(nghìn|triệu|tỷ|byte|[KkMmGgTt][Bb])\b', r'\1 \2', text)

    # 1a. Slash dates → spoken words ("24/2" → "24 tháng 2"). SPOKEN only — the caption
    #     keeps "24/2". MUST run before step 2: a "24/2/2026" becomes "... năm 2026" here,
    #     and the year rule below is what turns that 2026 into digit-by-digit Vietnamese.
    text = _expand_vn_dates(text)

    # 2. Year normalization (emits hyphen-joined digit words; encoded in step 4).
    text = _normalize_years(text)

    # 3. Pronunciation map. Each say_as value is separator-encoded IN PLACE so its
    #    own space/hyphen separators drive the reading speed (NOT the surrounding
    #    sentence). A no-separator say_as (jipit) passes through as one token.
    if _PRON_RE_CI is not None or _PRON_RE_CS is not None:
        use_vieneu = engine == "vieneu" and bool(_PRON_REPL_VIENEU)

        def _sub(m: "re.Match") -> str:
            key = m.group(0).lower()
            if use_vieneu and key in _PRON_REPL_VIENEU:
                say = _PRON_REPL_VIENEU[key]
            else:
                say = _PRON_REPL.get(key, m.group(0))
            # Separator-encode the say_as (space=normal / 1-hyphen=FAST / 2-hyphen=PAUSE).
            # There is no SLOW path any more — nothing is ever slowed. (Formerly "prompt"/
            # "prompting" were wrapped in SLOW markers here; that dragged them into two
            # beats, so they now read at normal speed via their say_as. See word_improve.md.)
            return _encode_separators(say)

        # Case-sensitive (acronyms) first, then case-insensitive (word terms).
        if _PRON_RE_CS is not None:
            text = _PRON_RE_CS.sub(_sub, text)
        if _PRON_RE_CI is not None:
            text = _PRON_RE_CI.sub(_sub, text)

    # 4. Encode any REMAINING hyphen-joined tokens (the year digits from step 2, plus
    #    any literal hyphen the writer typed in the narration, e.g. "front-end",
    #    "2-3") into the same separator semantics: a maximal run of word-chars joined
    #    by hyphens becomes ONE FAST run (1 hyphen) or splits on a PAUSE (2+ hyphens).
    #    say_as values were already encoded in step 3 (their hyphens are gone), so this
    #    only catches text the map did not touch — they can never double-encode.
    #    ACRONYM GUARD: a hyphen immediately PRECEDED by an uppercase letter (DALL-E,
    #    ABC-X) is left literal — those are acronym compounds, not syllable joins;
    #    speeding them up would garble. We skip any token containing such a hyphen.
    #
    #    ENGLISH-COMPOUND GUARD (bug: symptom 2, v109 "vendor-specific" → "specific" dropped).
    #    A literal narration hyphen joining two MULTI-LETTER ENGLISH words ("vendor-specific",
    #    "front-end", "open-source", "vendor-lock") is a COMPOUND WORD, not a syllable/digit
    #    join. FAST-encoding it makes "vendor specific" its OWN isolated FAST atempo chunk; F5
    #    then rushes/garbles the second English word EVERY draw (measured 3/3: "specific" →
    #    "Shift"/"Speed"/"Civic"). A compound must read as ordinary continuous text INSIDE the
    #    surrounding sentence chunk (no isolation, no atempo). So a hyphen whose BOTH sides are
    #    an ASCII-letter run of >= 3 chars is rewritten to a SPACE here (normal reading) and
    #    NOT separator-encoded. The year/syllable FAST path is untouched: year digit-words and
    #    say_as syllables are short (1-2 chars) or already encoded in step 3. Env-guarded via
    #    F5_COMPOUND_HYPHEN_SPACE=0 to restore the old FAST-join for compounds if ever needed.
    _tok_re = re.compile(r'\w+(?:-+\w+)+')
    _acr_hyphen = re.compile(r'[A-Z]-')
    _eng_compound_hyphen = re.compile(r'(?<=[A-Za-z]{3})-(?=[A-Za-z]{3})')
    _compound_to_space = os.getenv("F5_COMPOUND_HYPHEN_SPACE", "1").strip().lower() not in ("0", "off", "false", "no")

    def _enc_tok(m: "re.Match") -> str:
        tok = m.group(0)
        if _acr_hyphen.search(tok):
            return tok  # uppercase-before-hyphen → acronym compound, leave literal
        # English compound ("vendor-specific"): read as normal words, not a FAST chunk.
        if _compound_to_space and _eng_compound_hyphen.search(tok):
            return _eng_compound_hyphen.sub(" ", tok)
        return _encode_separators(tok)

    text = _tok_re.sub(_enc_tok, text)

    return text


# Sentinel atempo factor meaning "this is not speech to time-stretch, it is a PAUSE
# beat to insert" (F5_PAUSE_BEAT_S of silence). 0.0 is never a valid atempo, so it
# cannot collide with a real speed factor. The synth loops special-case it.
_PAUSE_FACTOR = 0.0


def _split_by_speed(text: str) -> list[tuple[str, float]]:
    """Split text at speed/pause markers into [(segment_text, atempo_factor), ...].

    Implements the NEW separator semantics (source of truth: word_improve.md). The
    markers are produced upstream by _encode_separators / _apply_pron_map:
      - _SPD_FAST_ST..EN  → segment read FAST   (factor F5_FAST_FACTOR, ~1.05x)
      - _SPD_SLOW_ST..EN  → segment read at factor F5_SLOW_JOIN_FACTOR (def 1.0 = NORMAL
                            speed; < 1.0 = slower). The PREDEFINED per-word speed (owner: prompt).
      - _SPD_PAUSE        → a ("", _PAUSE_FACTOR) segment: insert one beat of silence
                            (F5_PAUSE_BEAT_S) here, then continue. Carries NO text.
      - everything else   → factor 1.0 (normal speed).

    Returns REAL per-segment factors. A FAST or SLOW segment becomes its own chunk → a
    speed-transition boundary; the per-boundary gap at a speed transition is the tiny
    TTS_SPEED_GAP_S (5 ms by default) so the retimed word blends with no audible "break".
    Adjacent same-speed normal text is coalesced so a year/term does not fragment the
    surrounding sentence into extra infer calls. Pause segments are kept as standalone
    ("", _PAUSE_FACTOR) entries.

    Pause is NOT a speed; it does not start an atempo chunk. The synth loops translate
    a _PAUSE_FACTOR segment into inserted silence between the neighbouring chunks."""
    import re as _re
    SEP = _re.compile(r"(\x01|\x02|\x03|\x04|\x05)")
    result: list[tuple[str, float]] = []
    cur_speed = 1.0
    buf = ""

    def _flush():
        nonlocal buf
        if buf:
            result.append((buf, cur_speed))
            buf = ""

    for part in SEP.split(text):
        if part == _SPD_FAST_ST:
            _flush()
            cur_speed = _F5_FAST_FACTOR
        elif part == _SPD_FAST_EN:
            _flush()
            cur_speed = 1.0
        elif part == _SPD_SLOW_ST:
            _flush()
            cur_speed = _F5_SLOW_JOIN_FACTOR
        elif part == _SPD_SLOW_EN:
            _flush()
            cur_speed = 1.0
        elif part == _SPD_PAUSE:
            _flush()
            result.append(("", _PAUSE_FACTOR))  # standalone pause beat
        else:
            buf += part
    _flush()
    if not result:
        return [(text, 1.0)]
    # Coalesce consecutive same-speed SPEECH segments (factor != _PAUSE_FACTOR) so a
    # normal-text run is one segment (one infer call) — only a real FAST/SLOW/PAUSE
    # boundary introduces a chunk split. Pause segments are never merged into speech.
    merged: list[tuple[str, float]] = []
    for seg_text, spd in result:
        if (merged and spd != _PAUSE_FACTOR and merged[-1][1] == spd
                and merged[-1][1] != _PAUSE_FACTOR):
            merged[-1] = (merged[-1][0] + seg_text, spd)
        else:
            merged.append((seg_text, spd))
    # Drop empty SPEECH segments (a "" with factor 1.0 can appear if markers abut);
    # keep empty PAUSE segments (their emptiness is the point).
    merged = [(t, s) for (t, s) in merged if t or s == _PAUSE_FACTOR]
    return merged or [(text, 1.0)]


def _apply_atempo(src: str, dst: str, speed: float, out_sr: int = CANONICAL_SR) -> None:
    """Apply FFmpeg atempo to change playback speed (pitch-preserving).

    atempo's valid range is 0.5–2.0, so a target outside it is reached by CHAINING
    filters whose product equals `speed`:
      - speed > 2.0  (FAST) → atempo=2.0,atempo=speed/2.0   (e.g. 2.6 = 2.0 * 1.3)
      - speed < 0.5  (SLOW) → atempo=0.5,atempo=speed/0.5   (e.g. 0.4 = 0.5 * 0.8)
      - 0.5 ≤ speed ≤ 2.0   → a single atempo=speed
    A 10 ms linear fade-in is appended so the segment does not start abruptly
    (reduces the "preceding word sounds cut off" artifact). Falls back to copy on
    ffmpeg failure so synthesis never crashes.

    `out_sr` lets the F5 path keep its per-chunk atempo at the 24 kHz source rate (the
    single 24→48 kHz resample now happens once per scene); other callers keep 48 kHz."""
    fade_in = "afade=t=in:st=0:d=0.01"
    if speed > 2.0:
        af = f"atempo=2.0,atempo={speed/2.0:.4f},{fade_in}"
    elif speed < 0.5:
        # atempo floor is 0.5; reach lower targets by chaining (0.5 * factor = speed).
        af = f"atempo=0.5,atempo={speed/0.5:.4f},{fade_in}"
    else:
        af = f"atempo={speed:.4f},{fade_in}"
    proc = subprocess.run(
        [_ffmpeg_bin(), "-y", "-i", src, "-af", af,
         "-ar", str(out_sr), "-ac", "1", "-c:a", "pcm_s16le", dst],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0 or not os.path.isfile(dst):
        import shutil
        shutil.copy2(src, dst)


# ---- Shared text/audio helpers ---------------------------------------------

# VieNeu (and F5) degrade on long inputs: the model runs to its frame cap, then
# RAMBLES / LOOPS a phrase and pads the tail with silence (observed: a 24.0s clip
# carrying ~3s of garbled speech + ~20s of dead air). Synthesizing one SENTENCE at
# a time keeps every call well inside the model's reliable range, then we concat.
# A char ceiling also splits a single over-long sentence so no call is ever huge.
_SENT_SPLIT = re.compile(r"(?<=[.!?…;:])\s+|\n+")

# Narration lists sometimes arrive as short period/semicolon-separated items
# ("cursor. windsurf. Cline."). _SENT_SPLIT would split them into many tiny
# sentence-chunks each carrying a full inter-sentence gap — sounds choppy.
# Consecutive pieces ≤ this char count are merged back as one comma-separated
# list item so the reader hears them as a flowing enumeration.
_LIST_ITEM_THRESHOLD = 20

# Words whose F5-ViVoice synthesis produces a trailing -í artifact when they
# appear mid-chunk (the phoneme prediction bleeds into the next syllable).
# Placing them at a chunk boundary lets the inter-chunk silence absorb the
# artifact, and the F5 fade-out pass (in _run_f5) removes its tail.
#
# COMPOUND GUARD (bug 2): "chính" is also the first syllable of many common
# Vietnamese COMPOUND words — chính thức (official), chính xác (exact), chính phủ
# (government), chính sách (policy), chính trị (politics)… In a compound, "chính"
# is NOT phrase-final and carries NO trailing -í artifact, so splitting between it
# and the next syllable (the old `chính(?=\s+\S)` rule) cut a word in half and
# inserted an audible mid-word gap (the "chính[pause]thức" break the owner heard).
# The -í artifact only appears when "chính" genuinely ENDS a phrase/clause, i.e.
# it is followed by punctuation or end-of-text — NOT by a compound-forming syllable.
# We therefore only force the split when "chính" is the LAST word of its chunk
# (handled by _CHINH_END_RE downstream); the mid-chunk forced split is removed.
# The compound set below is kept for the negative-lookahead belt-and-suspenders so
# a future change to the splitter still cannot break these compounds.
_CHINH_COMPOUNDS = (
    "thức", "xác", "phủ", "sách", "trị", "quyền", "nghĩa", "đáng",
    "thống", "yếu", "diện", "kiến", "danh", "tả", "khách", "quy",
)
# Mid-chunk match: "chính" followed by whitespace + a word that is NOT one of the
# known compound-forming syllables. With every common compound excluded, this now
# effectively never fires inside a real Vietnamese sentence (a bare "chính X" where
# X is not a compound syllable is rare), so the artifact-absorbing split happens
# only at a genuine phrase end via _CHINH_END_RE. Kept as a guard, not a driver.
_CHINH_MID_RE = re.compile(
    r'\bchính\b(?=\s+(?!(?:' + "|".join(_CHINH_COMPOUNDS) + r')\b)\S)',
    re.IGNORECASE,
)
# Detects when "chính" is at the end of the gen_text fed to F5 — triggers the
# per-chunk tail fade-out that makes the -í artifact inaudible.
_CHINH_END_RE = re.compile(r'\bchính\b\s*$', re.IGNORECASE)

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
#
# A6 TUNING ("từ bị kéo dài như giật cụt"): lowered 220→200. Above F5's INTERNAL
# max_chars the model batches one infer call into sub-segments and overlap-adds them,
# whose seam is the stretched/stuttered artifact. 200 keeps more ordinary sentences
# inside F5's single-segment range (so no internal seam at all) while still rarely
# forcing our OWN chunk split (which only adds a clean inter-chunk gap, not a stutter).
_F5_MAX_CHUNK_CHARS = int(os.getenv("F5_MAX_CHUNK_CHARS", "200"))

# F5-specific chunk FLOOR (chars). A very short gen_text makes F5 over-size its
# frame budget — out_len = ref_len*(ref_text+gen_text)/ref_text is dominated by
# ref_len when gen_text is tiny, so F5 has far more frames than the words need and
# fills the surplus by REPEATING the text (a short comma-list "cursor, windsurf,
# cline." gets spoken twice). We merge consecutive same-speed chunks until each
# reaches this floor so no infer call gets a tiny gen_text. Env-tunable.
_F5_MIN_CHUNK_CHARS = int(os.getenv("F5_MIN_CHUNK_CHARS", "45"))


def _consolidate_f5_short(chunks: list[tuple[str, bool, float]]) -> list[tuple[str, bool, float]]:
    """Merge consecutive same-speed F5 chunks so none falls below _F5_MIN_CHUNK_CHARS.

    Prevents F5's short-gen-text repetition (see _F5_MIN_CHUNK_CHARS). Greedy
    forward merge bounded by _F5_MAX_CHUNK_CHARS*1.1 (so merged chunks stay within
    F5's reliable range); the merged chunk keeps the LAST piece's ends_sentence
    flag. Pieces are joined with ". " across a sentence end, else ", " so F5 still
    gets a prosody cue. A trailing short chunk with no successor is merged backward
    into its predecessor. Speed boundaries are never crossed (atempo-correctness)."""
    if not chunks:
        return chunks
    cap = int(_F5_MAX_CHUNK_CHARS * 1.1)

    def _join(left: str, left_ends: bool, right: str) -> str:
        # Strip any trailing sentence punctuation off the left piece first so we
        # never emit a doubled delimiter ("xong.. tiep") if a piece already carries
        # one; then add ". " across a sentence end, else ", " as a prosody cue.
        l = left.rstrip(" .,;:!?")
        return l + (". " if left_ends else ", ") + right

    out: list[tuple[str, bool, float]] = []
    i = 0
    while i < len(chunks):
        txt, ends, spd = chunks[i]
        j = i + 1
        while len(txt) < _F5_MIN_CHUNK_CHARS and j < len(chunks):
            ntxt, nends, nspd = chunks[j]
            if nspd != spd:
                break
            joined = _join(txt, ends, ntxt)
            if len(joined) > cap:
                break
            txt, ends = joined, nends
            j += 1
        out.append((txt, ends, spd))
        i = j
    # Trailing short chunk (no successor to absorb it) → merge back into the prev.
    if (
        len(out) >= 2
        and len(out[-1][0]) < _F5_MIN_CHUNK_CHARS
        and out[-1][2] == out[-2][2]
    ):
        ptxt, pends, pspd = out[-2]
        ltxt, lends, _ = out[-1]
        joined = _join(ptxt, pends, ltxt)
        if len(joined) <= cap:
            out[-2:] = [(joined, lends, pspd)]
    return out


def _split_for_tts(text: str, max_chars: int = _MAX_CHUNK_CHARS,
                   split_chinh: bool = True) -> list[tuple[str, bool]]:
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
    sentence end). Returns non-empty, stripped chunks; empty input -> [].

    Extra passes applied in order:
      1. Pre-merge: consecutive short pieces (≤ _LIST_ITEM_THRESHOLD) are joined
         with ", " so enumeration items ("cursor. windsurf. Cline.") become one
         comma-list chunk instead of many tiny sentence-chunks with full gaps.
      2. Post-"chính": any chunk containing "chính" not at its end is re-split so
         "chính" ends the chunk — the inter-chunk silence absorbs F5's -í artifact."""
    text = (text or "").strip()
    if not text:
        return []
    pieces = [p.strip() for p in _SENT_SPLIT.split(text) if p.strip()]

    # Pass 1: merge consecutive short pieces into a single comma-list chunk.
    merged: list[str] = []
    short_buf = ""
    for piece in pieces:
        if len(piece) <= _LIST_ITEM_THRESHOLD:
            short_buf = (short_buf + ", " + piece) if short_buf else piece
        else:
            if short_buf:
                merged.append(short_buf)
                short_buf = ""
            merged.append(piece)
    if short_buf:
        merged.append(short_buf)
    pieces = merged

    out: list[tuple[str, bool]] = []
    for p in pieces:
        if len(p) <= max_chars:
            out.append((p, True))
            continue
        # Sentence is too long for one reliable infer() call: break on commas,
        # accumulating up to the char ceiling. These sub-pieces are MID-sentence.
        # Flush only when the next sub is long enough to standalone (≥ 30 chars)
        # or we'd be hard-capping even with a short item — this keeps short list
        # items (e.g. "cursor,") together with their preceding context.
        subs: list[str] = []
        buf = ""
        for sub in re.split(r"(?<=,)\s+", p):
            candidate = (buf + " " + sub).strip() if buf else sub
            if buf and len(candidate) > max_chars:
                if len(sub) >= 30 or len(candidate) > int(max_chars * 1.25):
                    subs.append(buf.strip())
                    buf = sub
                else:
                    buf = candidate  # short list item: allow slight overflow
            else:
                buf = candidate
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

    # Pass 2 (REMOVED): there used to be a forced mid-chunk split after "chính" to
    # push the F5-ViVoice -í trailing artifact into an inter-chunk silence. That split
    # cut real phrases mid-stream — it broke the compound "chính thức" AND the phrase
    # "chính là" ("…xuất hiện chính | là khái niệm…"), inserting an audible cut the
    # owner heard. The -í artifact only occurs when "chính" GENUINELY ENDS a phrase/
    # clause; that case is already handled, with NO chunk split, by the _CHINH_END_RE
    # tail-fade in _run_f5 (it fades the last ~80 ms of any chunk that ends in "chính").
    # So the mid-chunk forced split was pure harm and is gone. `split_chinh` and
    # `_CHINH_MID_RE` are retained only as an env/behavior switch for a hypothetical
    # future checkpoint; with the split removed they currently have no effect here.
    return out or [(text, True)]


def _trim_silence_np(wav, sr: int, thresh_db: float = -50.0, pad_s: float = 0.12):
    """Trim leading/trailing silence from a float/int waveform (numpy 1-D array).

    Keeps a small pad of silence at each end. Guards against the model's trailing
    dead-air (the frame-cap failure mode) inflating scene duration and causing the
    assembler to hold a frozen video frame over silence. Returns the trimmed array
    (or the original if it would trim to nothing).

    A1 ("thuật ngữ" → "ngữ" dropped): the threshold was -40 dB, which is loud enough
    to clip a QUIET low-tone final syllable (Vietnamese tone-3 "ngữ" trails off creaky
    and soft, dipping near -40 dB) — the trailing trim ate it as if it were silence.
    Lowered to -50 dB (was -45, still clipped) so a soft word-final syllable survives
    while genuine dead air (well below -50 dB) is still removed. Env: CF_TTS_TRIM_DB."""
    thresh_db = float(os.getenv("CF_TTS_TRIM_DB", str(thresh_db)))
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


def _tighten_slow_join_gap(wav, sr: int, factor: float = _F5_SLOW_JOIN_GAP_TIGHTEN,
                           thresh_db: float = -33.0, edge_guard_s: float = 0.03,
                           min_gap_ms: float = 10.0):
    """Shorten the INTERNAL silence run(s) of a SLOW-join chunk to `factor` of their
    length, leaving all speech samples untouched (bug: "pờ~rôm" đột nhiên chậm).

    A tilde SLOW-join chunk is one F5 inference of two syllables joined by a space
    ("pờ rôm"); atempo-slowing the whole chunk stretches the inter-syllable gap too, so
    the syllables drift apart and sound dragged. This scales ONLY the silence BETWEEN the
    syllables (its ONLY internal silence run, since the chunk is a standalone inference)
    to `factor` (0.80 = 20% tighter junction). Speech is never time-stretched, so each
    syllable KEEPS the slowed pace — only the dead air between them is compressed. This is
    the "slow syllables, fast junction" hybrid the owner asked for.

    Detection mirrors _compress_internal_silence_np (5 ms RMS frames vs the clip peak). A
    run is INTERNAL when it does not touch the leading/trailing edge_guard_s (so a
    word-final tail or lead-in is never clipped). Runs shorter than min_gap_ms are left
    (nothing to tighten). factor>=1.0 → no-op. Returns the (possibly shorter) 1-D float32
    array; degenerate input → original."""
    import numpy as np
    if factor >= 1.0:
        return wav
    a = np.asarray(wav, dtype=np.float32).reshape(-1)
    n = a.size
    if n < int(0.1 * sr):
        return a
    fl = max(1, int(sr * 0.005))  # 5 ms frames
    nf = n // fl
    if nf == 0:
        return a
    fr = a[:nf * fl].reshape(nf, fl)
    rms = np.sqrt((fr ** 2).mean(axis=1) + 1e-12)
    peak = float(rms.max()) or 1.0
    db = 20.0 * np.log10(rms / peak + 1e-12)
    sil = db < thresh_db
    guard = int(edge_guard_s * sr)
    min_gap = int(min_gap_ms / 1000.0 * sr)
    kept: list[tuple[int, int]] = []
    cursor = 0
    i = 0
    changed = False
    while i < nf:
        if sil[i]:
            j = i
            while j < nf and sil[j]:
                j += 1
            s, e = i * fl, j * fl
            run = e - s
            # Internal only (skip leading/trailing silence) and worth tightening.
            if s > guard and e < n - guard and run >= min_gap:
                keep = int(run * factor)      # samples of the gap to RETAIN (factor=0.80 → 80%)
                drop = run - keep             # samples to REMOVE from the MIDDLE (~20%)
                # Split the retained gap evenly at each edge of the run and cut the middle,
                # so both syllable boundaries keep a little air (no click) and the junction
                # shrinks to `keep`. The KEPT-segment boundary ends after keep//2 of the
                # gap; the cursor resumes drop samples later (leaving keep-keep//2 at the
                # trailing edge).
                head = s + keep // 2
                tail = head + drop
                kept.append((cursor, head))
                cursor = tail
                changed = True
            i = j
        else:
            i += 1
    if not changed:
        return a
    kept.append((cursor, n))
    out = np.concatenate([a[s:e] for s, e in kept if e > s])
    return out.astype(np.float32) if out.size >= int(0.05 * sr) else a


# Leading-edge trim threshold for a FAST (year / 1-hyphen-join) chunk. A FAST chunk is
# its own infer call whose gen_text is a YEAR ("hai không hai sáu") or a mapped term
# ("ây-jừn") — and F5 prepends a long, breath-level LEAD-IN before the first phoneme.
# That lead-in is the audible BREAK heard before a number ("...vào đầu [break] 2026"): it
# is the FAST chunk's own leading silence, NOT a prosody pause inside another inference,
# so it survives the whole-scene silence compressor. It sits at ~-36..-43 dB (breath/room
# tone), ABOVE the gentle -50 dB CF_TTS_TRIM_DB used for the trailing edge, so the
# symmetric trim left it intact. Because the region BEFORE the first phoneme of a
# year/term carries no speech to protect (it starts on a strong onset; "hai"'s /h/
# survives a 20 ms pad), we trim the LEADING edge at this higher threshold to delete the
# pre-speech lead-in and seat the year flush against the preceding word. The TRAILING
# edge keeps the gentle threshold so a soft word-final tail is never clipped.
#
# NOTE (v73 variance finding): a SYMMETRIC (aggressive trailing) trim plus a FAST-boundary
# neighbour-edge trim were tried to also close the post-FAST-term seam ("Agent [seam]
# harness"). A 5-run-per-condition variance study on the REAL path showed the post-FAST
# seam is dominated by F5's stochastic inter-word pause (range ~90-345 ms across identical
# inputs); the extra trimming only moved the MEDIAN modestly and never controlled the
# spread, so it was reverted to keep the baseline simple. Only the LEADING trim (which
# fixes the deterministic pre-number break) is kept. Env: F5_FAST_LEAD_TRIM_DB.
_F5_FAST_LEAD_TRIM_DB = float(os.getenv("F5_FAST_LEAD_TRIM_DB", "-34.0"))


def _trim_fast_chunk_edges(wav, sr: int, lead_db: float = _F5_FAST_LEAD_TRIM_DB,
                           lead_pad_s: float = 0.020, trail_pad_s: float = _FAST_TRIM_PAD_S):
    """Asymmetric edge-trim for a FAST (year / 1-hyphen-join) chunk.

    Trims the LEADING pre-speech lead-in aggressively (at lead_db, keeping lead_pad_s)
    because everything before a year/term's first phoneme is F5 padding — this closes
    the deterministic break before a number. Trims the TRAILING edge gently (at the
    standard CF_TTS_TRIM_DB, keeping trail_pad_s) so a soft word-final tail is never
    clipped. (The post-FAST-term TRAILING seam is F5-stochastic and not reliably
    controllable by trimming — see the note above — so the trailing edge stays gentle.)
    Fail-safe: returns the original on any degenerate result (would trim to <50 ms)."""
    import numpy as np

    a = np.asarray(wav, dtype=np.float32).reshape(-1)
    if a.size < int(0.05 * sr):
        return a
    peak = float(np.max(np.abs(a))) or 1.0
    norm = np.abs(a / peak)
    trail_db = float(os.getenv("CF_TTS_TRIM_DB", "-50"))
    lead_loud = np.where(norm > 10.0 ** (lead_db / 20.0))[0]
    trail_loud = np.where(norm > 10.0 ** (trail_db / 20.0))[0]
    if lead_loud.size == 0 or trail_loud.size == 0:
        return a
    lo = max(0, int(lead_loud[0]) - int(lead_pad_s * sr))
    hi = min(a.size, int(trail_loud[-1]) + int(trail_pad_s * sr))
    if hi - lo < int(0.05 * sr):
        return a
    return a[lo:hi]


# ---- Internal-silence compression ------------------------------------------
#
# The TTS produces audible "laggy" dead-air at two kinds of spots:
#   (a) the F5-ViVoice "chính [pause] thức" inter-chunk gap (the Pass-2 split's
#       residual silence: per-chunk trim pad + tail fade + gap constant), and
#   (b) the model's PROSODY pause inside a single inference, before a year/number
#       (e.g. "...đầu [pause] 2026", "ra mắt năm [pause] 2022").
# A gap-constant tweak can only touch (a); (b) lives inside one infer call. So we
# run a conservative compression pass on the FINAL concatenated scene WAV that caps
# any INTERNAL silence longer than CF_TTS_SIL_CAP_S down to CF_TTS_SIL_KEEP_S,
# removing only the MIDDLE of the run (samples below the threshold). The edges of
# each silence run are kept intact, so no consonant onset/decay is clipped and the
# speaking RATE is never changed (we delete silence, we do not time-stretch — the
# owner's "never read faster" rule is respected). Leading/trailing silence is left
# to the existing edge-trim. Genuine short pauses (< cap) and sentence boundaries
# stay natural. Env-tunable; set CF_TTS_SIL_CAP_S=0 to disable entirely.
# CAP/KEEP tightened (owner asked ~20% more reduction of the intra-year digit-join gap,
# e.g. the small "hai—không" tail inside "2026"). The residual intra-year silence measured
# ~55-70 ms — BELOW the old 0.07 s cap, so it was NOT being compressed. Lowering the cap to
# 0.055 s lets those runs qualify, and KEEP 0.05→0.04 s trims ~20% more from every
# compressed run. Still conservative: only the MIDDLE of a detected (<-40 dB) silence run is
# removed, edges kept, rate unchanged — no consonant/digit is clipped, natural sub-55 ms
# micro-gaps stay. Env-tunable.
#
# REACHABILITY (audited 2026-07-28): this CF_TTS_SIL_* group is INERT as configured today.
# It is the FALLBACK inside the legacy tail of _run_f5 (used only when gap shaping did not
# run), and that tail itself executes only when CF_TTS_PER_SENTENCE=0 — which .env currently
# sets to 1. Kept as the other half of the owner's A/B, not dead code.
CF_TTS_SIL_CAP_S = float(os.getenv("CF_TTS_SIL_CAP_S", "0.055"))
CF_TTS_SIL_KEEP_S = float(os.getenv("CF_TTS_SIL_KEEP_S", "0.04"))
CF_TTS_SIL_THRESH_DB = float(os.getenv("CF_TTS_SIL_THRESH_DB", "-40.0"))
# Glide-dip PROTECT length (bug: v108 tail-clip at "tái"/"tùy" vs v109 rhythm-break regression).
# A F5 diphthong/glide-final syllable ("tái","tùy","nào") renders a brief low-energy NOTCH
# inside/adjacent to its own body; a rhythm-breaking INTER-WORD PAUSE is a longer low-energy
# run BETWEEN two words. Both floor in the SAME -40..-56 dB band (measured v108+v109), so DEPTH
# cannot tell them apart — an earlier depth gate (removed) protected glide dips but ALSO the
# long breath pauses, leaving v109's audible rhythm breaks uncompressed. DURATION separates
# them cleanly: after the morphological bridge, a glide-dip cluster spans <= ~95 ms while a real
# inter-word pause spans ~110..310 ms (measured v109 "tùy chỉnh" 305 ms, "truy cập" 110 ms; clean
# draws 175..310 ms). So the compressor now PROTECTS any bridged run SHORTER than this length
# (the glide dip is kept intact → no tail clip) and compresses only LONGER runs (the pause middle
# is removed → even rhythm). 0.10 s (100 ms) sits above the largest glide cluster (~95 ms) and
# below the shortest real pause (~110 ms). Set 0 to disable the length floor (compress any run
# > cap_s = pre-gate behaviour). Env: CF_TTS_SIL_PROTECT_MAX_S.
CF_TTS_SIL_PROTECT_MAX_S = float(os.getenv("CF_TTS_SIL_PROTECT_MAX_S", "0.10"))
# Morphological CLOSE bridge (seconds). The model's prosody pause before a
# year/number (e.g. "...vào đầu [breath] 2026") is NOT clean silence: it carries
# breath / room tone that hovers right around the -40 dB threshold, so the raw
# silence mask is broken into many sub-cap fragments and the long gap is never
# detected as one removable run (the "break before 2026" bug). Before measuring run
# length we CLOSE the mask: any NON-silent "blip" shorter than this bridge that sits
# strictly BETWEEN two silence runs is treated as silence, so a breath-level gap
# coalesces into ONE run whose middle is then removed. Edge non-silent runs (real
# speech at the head/tail) are never filled, so no word onset/tail is ever clipped.
# Set CF_TTS_SIL_BRIDGE_S=0 to disable the close (exact pre-fix behaviour).
CF_TTS_SIL_BRIDGE_S = float(os.getenv("CF_TTS_SIL_BRIDGE_S", "0.06"))


def _compress_internal_silence_np(wav, sr: int,
                                  cap_s: float = CF_TTS_SIL_CAP_S,
                                  keep_s: float = CF_TTS_SIL_KEEP_S,
                                  thresh_db: float = CF_TTS_SIL_THRESH_DB,
                                  edge_guard_s: float = 0.05,
                                  bridge_s: float = CF_TTS_SIL_BRIDGE_S,
                                  protect_max_s: float = CF_TTS_SIL_PROTECT_MAX_S):
    """Shorten internal silence runs longer than cap_s down to keep_s.

    Detects silence with 5 ms RMS frames (relative to the clip peak). A morphological
    CLOSE then bridges brief NON-silent blips (shorter than bridge_s) that sit
    strictly BETWEEN two silence runs, so a breath-level low-energy gap (e.g. the
    prosody pause before a year/number, which hovers near the threshold and would
    otherwise fragment into sub-cap pieces) coalesces into ONE run. Any run that is
    wholly INTERNAL (not touching the leading/trailing edge_guard_s) and exceeds
    cap_s has its MIDDLE removed, keeping keep_s/2 of original samples at each edge
    so word boundaries are preserved. Speech samples are never time-stretched and the
    time base of speech is unchanged. The close never fills the leading or trailing
    non-silent run (real speech at the clip head/tail), so no word is clipped.

    DURATION GATE (protect_max_s): a bridged run is compressed ONLY if it is at least
    protect_max_s long. A brief glide dip inside a diphthong-final syllable ("tái"/"tùy")
    bridges to <= ~95 ms and is thus LEFT intact (fixing the v108 tail clip), while a real
    inter-word pause (~110..310 ms) exceeds the floor and is still compressed (fixing the
    v109 rhythm break). Depth is NOT used to gate (both cases share the -40..-56 dB band).
    protect_max_s<=0 disables the length floor (compress any run > cap_s).
    Returns the (possibly shorter) 1-D float32 array; degenerate input -> original."""
    import numpy as np

    if cap_s <= 0 or keep_s >= cap_s:
        return wav
    a = np.asarray(wav, dtype=np.float32).reshape(-1)
    n = a.size
    if n < int(0.2 * sr):
        return a
    fl = max(1, int(sr * 0.005))  # 5 ms frames
    nf = n // fl
    if nf == 0:
        return a
    fr = a[:nf * fl].reshape(nf, fl)
    rms = np.sqrt((fr ** 2).mean(axis=1) + 1e-12)
    peak = float(rms.max()) or 1.0
    db = 20.0 * np.log10(rms / peak + 1e-12)
    sil = db < thresh_db
    # Morphological CLOSE: fill short NON-silent gaps that sit BETWEEN two silence
    # runs so a breath-broken pause becomes one contiguous removable run. Bounded to
    # the interior between the first and last silent frame, so the leading/trailing
    # non-silent run (real speech at the edges) is never touched. bridge_s<=0 → skip.
    if bridge_s > 0 and sil.any():
        bridge = max(1, int(bridge_s * sr) // fl)
        first_sil = int(np.argmax(sil))
        last_sil = int(nf - 1 - np.argmax(sil[::-1]))
        i = first_sil
        while i <= last_sil:
            if not sil[i]:
                j = i
                while j <= last_sil and not sil[j]:
                    j += 1
                # [i, j) is a non-silent blip strictly inside the silence span.
                if i > first_sil and j <= last_sil and (j - i) <= bridge:
                    sil[i:j] = True
                i = j
            else:
                i += 1
    guard = int(edge_guard_s * sr)
    cap = int(cap_s * sr)
    half = int(keep_s * sr) // 2
    # Build kept-segment ranges, dropping the middle of each long internal run.
    kept: list[tuple[int, int]] = []
    cursor = 0
    i = 0
    while i < nf:
        if sil[i]:
            j = i
            while j < nf and sil[j]:
                j += 1
            s, e = i * fl, j * fl
            # DURATION gate (bug: symptoms 3-4 v108 tail-clip vs v109 rhythm-break regression).
            # Distinguish a GLIDE DIP (a brief low-energy notch inside/adjacent to a diphthong-
            # final syllable's own body — "tái"/"tùy") from a rhythm-breaking INTER-WORD PAUSE.
            # DEPTH cannot separate them (both floor at ~-40..-56 dB — measured v108+v109), but
            # DURATION does: after the morphological bridge, a glide-dip cluster spans <= ~95 ms
            # while a real inter-word pause spans ~110..310 ms (measured on v109 "tùy chỉnh" 305 ms,
            # "truy cập" 110 ms, and clean-draw pauses 175..310 ms). So we protect a SHORT bridged
            # run (below protect_max_s) — the glide dip is left INTACT (no tail clip) — and compress
            # only a LONG run (>= protect_max_s AND > cap_s) — the inter-word pause middle is removed
            # so the rhythm stays even. This REPLACES the earlier depth gate (min_depth_db), which
            # protected glide dips correctly but ALSO protected these long breath pauses, leaving the
            # v109 rhythm breaks uncompressed. The compressor's original job (shrink long pauses) is
            # thus restored, while the glide tail is protected by the length floor. protect_max_s<=0
            # disables the length floor (compress any run > cap_s, i.e. pre-gate behaviour).
            run_len = e - s
            long_enough = (protect_max_s <= 0.0) or (run_len >= int(protect_max_s * sr))
            # Internal only (skip leading/trailing silence — handled by edge trim).
            if s > guard and e < n - guard and run_len > cap and long_enough:
                kept.append((cursor, s + half))
                cursor = e - half
            i = j
        else:
            i += 1
    kept.append((cursor, n))
    pieces = [a[s:e] for (s, e) in kept if e > s]
    return np.concatenate(pieces).astype(np.float32) if pieces else a


# ---- Alignment-aware gap shaping (punctuation-aware) ------------------------
#
# The energy-only compressor above cannot satisfy the owner's spec: a COMMA/PERIOD
# pause and a mid-clause F5 pause are acoustically IDENTICAL (same -40..-56 dB breath
# level, same length range), so any threshold/duration rule either squashes real
# punctuation beats (v110: "punctuation pauses gone") or leaves mid-clause breaks
# (v109: "tùy chỉnh" break). The ONLY signal that separates them is the NARRATION
# PUNCTUATION. So we align whisper word timestamps (of the FINAL scene wav) to the
# narration tokens (which carry punctuation) and shape EACH inter-word gap by class:
#
#   * gap at a PUNCTUATION boundary (token ends with , . ; : ! ? …) → enforce a
#     TARGET one-beat pause: pad up to CF_GAP_PUNCT_S if shorter, keep if already
#     >= it (never compress a punctuation pause below the beat).
#   * gap MID-CLAUSE (no punctuation between the two words) → compress the low-energy
#     silence down to CF_GAP_CLAUSE_S (a small, even inter-word gap) — this removes
#     the F5 stochastic rhythm break.
#   * a dip INSIDE a whisper word span (glide dip like "tái"/"tùy"/"kỷ") is NEVER
#     touched: we only ever operate on the silence run that sits BETWEEN two whisper
#     words, so word interiors are structurally protected (no depth/duration guessing).
#
# This directly encodes "one beat at punctuation, smooth flow elsewhere, interiors
# untouched". Speech samples are never time-stretched (narration-speed rule): we only
# delete or insert SILENCE in the inter-word gap. Runtime: one CPU whisper pass per
# scene (the resident _head_whisper small/int8 model already used for filler trim), so
# no extra VRAM. Fail-safe: any whisper/alignment failure → return the input unchanged.
#
# REACHABILITY (audited 2026-07-28): this whole CF_GAP_* group is INERT as configured today.
# Its only caller is the legacy tail of _run_f5, which runs only when CF_TTS_PER_SENTENCE=0,
# and Dashboard/api/.env ships that flag as 1. The OmniVoice path called it briefly on
# 2026-07-28 and was reverted the same day (it cut word tails at commas/periods). So the flag
# below being "1" does NOT mean gap shaping is running — check CF_TTS_PER_SENTENCE first.
CF_GAP_SHAPE = os.getenv("CF_GAP_SHAPE", "1").strip().lower() not in ("0", "off", "false", "no")
# One-beat target pause kept at a punctuation boundary (seconds). Measured from good
# scenes' comma/period beats (~0.15-0.22 s). 0.18 s reads as one clear, uniform beat.
CF_GAP_PUNCT_S = float(os.getenv("CF_GAP_PUNCT_S", "0.18"))
# Target inter-word gap MID-CLAUSE (seconds). Small enough to flow continuously, big
# enough not to fuse two words. ~0.05 s matches the natural intra-sentence gap.
CF_GAP_CLAUSE_S = float(os.getenv("CF_GAP_CLAUSE_S", "0.05"))
# Only RESHAPE a gap when the change is at least this many seconds (avoid churning tiny
# differences / re-writing near-identical audio).
CF_GAP_MIN_DELTA_S = float(os.getenv("CF_GAP_MIN_DELTA_S", "0.02"))
# Silence threshold (dB below clip peak) for locating the low-energy gap between two
# whisper words. Matches the compressor's -40 dB so breath-level gaps are seen.
CF_GAP_THRESH_DB = float(os.getenv("CF_GAP_THRESH_DB", "-40.0"))
# Speech-edge guard (seconds) kept on each side of the gap so a soft word onset/tail is
# never clipped when we resize the silence between two words.
CF_GAP_EDGE_KEEP_S = float(os.getenv("CF_GAP_EDGE_KEEP_S", "0.03"))


# ---- Per-sentence architecture (CF_TTS_PER_SENTENCE) ------------------------
#
# Root cause of the "định nghĩa" tail-cut / "tùy chỉnh" mid-word break / global
# choppiness class: _shape_gaps_by_alignment (CF_GAP_SHAPE) uses whisper WORD
# timestamps to CUT audio into fixed-length pauses. Whisper boundaries land early on
# Vietnamese soft consonant / vowel tails, so forcing every gap to a fixed value
# clips word tails and makes the rhythm mechanical.
#
# The rearchitecture STOPS cutting audio to make pauses. Instead:
#   1. split the narration into SENTENCES/clauses (F5 pause boundaries are punctuation-
#      followed-by-space, so we normalize to exactly one space after . ! ? ; :),
#   2. run ONE F5 infer per sentence (short gen_text → no F5 duration-formula speed
#      drift, no internal cross-fade seam → no chunk-boundary stutter/echo),
#   3. lightly trim ONLY the leading/trailing SILENCE of each clip (energy-based —
#      silero-vad is not installed in cf-venv, so we reuse _trim_silence_np) plus a
#      short linear fade-in on the onset to kill F5 ref-tail bleed at sentence start,
#   4. concatenate with FIXED, CONSISTENT silence padding (short mid-sentence beat,
#      longer at a sentence-final period) — consistency is the key to natural prosody.
# Whisper is then used ONLY for caption timing downstream, NEVER to cut narration.
#
# Behind a flag so we can A/B safely. When ON, the whisper narration gap-shaping
# (_shape_gaps_by_alignment / CF_GAP_SHAPE path) is DISABLED for narration so the two
# mechanisms do not fight. Pronunciation overrides (pờ~rôm, ây jừn, years, acronyms)
# still apply — they run inside the same _apply_pron_map + _split_by_speed path per
# sentence, so a tilde/hyphen say_as still produces its FAST/SLOW/PAUSE atempo segments.
CF_TTS_PER_SENTENCE = os.getenv("CF_TTS_PER_SENTENCE", "0").strip().lower() not in ("0", "off", "false", "no")
# Fixed inter-clip silence padding (seconds). Consistency matters more than the exact
# value; start from the researched band and tune by ear via env.
#   MID  = between clauses / a non-sentence-final boundary (comma-split sub-clause).
#   PARA = after a sentence-final . ! ? … (a fuller beat).
# NOTE: each clip already retains a small trailing pad (CF_PS_TRIM_PAD_S) plus F5's own
# word-final tail, so the AUDIBLE inter-clip pause is this value PLUS ~0.15-0.25 s of
# residual. Measured (A/B scene1): PARA=0.55 → ~0.80 s audible period beat (too long).
# Defaults chosen so the audible period beat lands ~0.35-0.45 s (near the old gap-shape
# 0.34 s) and the mid beat ~0.25 s. Tune by ear via env.
CF_PS_GAP_MID_S = float(os.getenv("CF_PS_GAP_MID_S", "0.12"))
# ITEM 1 (owner job-122: even the sentence-period beat is too long/complete). Lowered
# 0.22 -> 0.14 so a sentence-final "." beat reads as a shorter, less "complete" pause.
CF_PS_GAP_PARA_S = float(os.getenv("CF_PS_GAP_PARA_S", "0.14"))
# Onset fade-in (seconds) applied to the FIRST ~15-20 ms of every per-sentence clip to
# kill F5 reference-tail bleed / the onset "bụp" at sentence start. Trims happen in the
# silence region, so this never clips a consonant. 0 disables.
CF_PS_HEAD_FADE_S = float(os.getenv("CF_PS_HEAD_FADE_S", "0.018"))
# Per-clip silence-trim pad (seconds) kept at each end after energy trim. A small pad keeps
# the clip flush while never eating a soft onset/tail (the trim threshold below is
# conservative so true dead air is removed but soft word tails are not).
CF_PS_TRIM_PAD_S = float(os.getenv("CF_PS_TRIM_PAD_S", "0.04"))
# Silence threshold (dB) for the per-clip leading/trailing trim. -50 dB matches the
# resample-stage trim; true dead air is well below, soft word tails sit above.
CF_PS_TRIM_DB = float(os.getenv("CF_PS_TRIM_DB", "-50.0"))

# ---- LEVER 1: compress-ONLY intra-clip silence pass (per-sentence path) ------
#
# Diagnosis (job 114): the per-sentence rearchitecture DISABLED the legacy whisper
# gap-shaping (_shape_gaps_by_alignment / CF_GAP_SHAPE) so it could not clip soft
# Vietnamese tails again. But that also removed ALL silence compression from the F5
# output, EXPOSING F5's raw over-long INTRA-clip silences: the "--"-like mid-phrase
# beats ("định nghĩa | nó", "giải thích | rõ"), a ~15% longer total, and much of the
# perceived choppiness are F5's own native pauses left un-shortened.
#
# This lever adds back ONLY the SAFE half: a compress-ONLY pass over EACH per-sentence
# clip (before concat) that shortens low-energy silence runs EXCEEDING a natural ceiling
# down toward that ceiling. It deliberately does NOT reintroduce the tail-clipping bug:
#   (a) silence is detected by ENERGY (5 ms RMS frames vs the clip peak) — NOT by whisper
#       word boundaries (whisper landed early on soft consonant tails, the old bug);
#   (b) a CONSERVATIVE threshold (~-52 dB) so soft nasal/vowel tails ("định nghĩa",
#       "tùy chỉnh") sit ABOVE it and count as SPEECH, never as silence;
#   (c) a generous ~45 ms edge guard on each side of every compressed run (speech
#       boundaries kept);
#   (d) ONLY runs LONGER than the ceiling are shortened — natural short inter-word gaps
#       (below the cap) are left untouched;
#   (e) never a FIXED gap value (the old mechanical choppiness) and never a cut into
#       voiced samples — only the MIDDLE of a detected silence run is removed.
# It reuses _compress_internal_silence_np (same audited algorithm the legacy path used)
# but with these SAFER, per-sentence-specific defaults so it can never behave like the
# old -40 dB whole-scene pass.
#
# Ceiling = CF_PS_MAX_INTRA_GAP_S (the "natural gap ceiling"): a run longer than this is
# a genuine over-long F5 pause and is compressed toward CF_PS_INTRA_KEEP_S; runs at/under
# it are natural and kept. Since each clip is ONE sentence/clause, its internal silences
# are mid-phrase F5 beats (the "--" symptom) — the inter-CLIP sentence/clause pauses
# (CF_PS_GAP_MID_S / CF_PS_GAP_PARA_S) are added at concat and are NOT touched here.
CF_PS_INTRA_COMPRESS = os.getenv("CF_PS_INTRA_COMPRESS", "1").strip().lower() not in ("0", "off", "false", "no")
# Natural gap ceiling (seconds): the longest INTERNAL silence a clip may keep. F5 mid-
# phrase beats measured ~0.20-0.45 s on job 114 (the "định nghĩa | nó" beat); a natural
# continuous mid-phrase gap is ~0.05-0.10 s. 0.08 s = the ceiling; runs above it are the
# over-long beats and get shortened. Env-tunable.
CF_PS_MAX_INTRA_GAP_S = float(os.getenv("CF_PS_MAX_INTRA_GAP_S", "0.08"))
# Length a compressed run is shortened TO (seconds). Must be < the ceiling. 0.06 s leaves
# a small, natural, CONSISTENT mid-phrase gap (not a fixed mechanical value forced onto
# every gap — only over-long runs are touched, and short ones keep their own length).
CF_PS_INTRA_KEEP_S = float(os.getenv("CF_PS_INTRA_KEEP_S", "0.06"))
# Conservative silence threshold (dB below clip peak) for this pass. -52 dB (vs the
# legacy -40 dB) so soft Vietnamese nasal/vowel tails count as SPEECH, protecting the
# "định nghĩa"/"tùy chỉnh" tails the whole rearchitecture was meant to preserve.
CF_PS_INTRA_THRESH_DB = float(os.getenv("CF_PS_INTRA_THRESH_DB", "-52.0"))
# Edge guard (seconds) kept on each side of every compressed run — generous ~45 ms so a
# soft onset/tail adjacent to the gap is never clipped.
CF_PS_INTRA_EDGE_GUARD_S = float(os.getenv("CF_PS_INTRA_EDGE_GUARD_S", "0.045"))


def _split_sentences_for_ps(text: str) -> list[tuple[str, bool]]:
    """Split narration into per-sentence (clause) units for the per-sentence path.

    Returns [(unit_text, ends_sentence)]:
      ends_sentence=True  → the unit ended at a sentence-final . ! ? … (use PARA gap).
      ends_sentence=False → a mid-sentence sub-clause (a comma split of a long sentence)
                            → use the shorter MID gap.

    Reuses _split_for_tts (sentence-first, comma sub-split for long sentences, F5 char
    budget) so behavior stays consistent with the existing chunker; we just interpret
    the ends_sentence flag as the gap class. Every unit is short enough for ONE F5 infer,
    which is exactly what removes the internal cross-fade seam + duration drift."""
    return _split_for_tts((text or "").strip(), max_chars=_F5_MAX_CHUNK_CHARS)


def _norm_align(s: str) -> str:
    """Lowercase + strip all non-word chars for loose token/whisper-word compares."""
    return re.sub(r"[^\w]", "", (s or "").strip().lower())


def _narration_punct_after(narration: str) -> list[bool]:
    """For each whitespace token of `narration`, True iff it ENDS with a pause-inducing
    punctuation mark (, . ; : ! ? … or a trailing ')' after one). Returns one flag per
    token, in order. Used to decide which inter-token gaps are punctuation beats."""
    toks = re.findall(r"\S+", narration or "")
    out: list[bool] = []
    for t in toks:
        # strip trailing brackets/quotes so "model.)" still counts on its '.'
        stripped = t.rstrip(")]}\"'»”’")
        out.append(bool(stripped) and stripped[-1] in ",.;:!?…")
    return out


def _shape_gaps_by_alignment(wav_path: str, narration: str,
                             punct_s: float = CF_GAP_PUNCT_S,
                             clause_s: float = CF_GAP_CLAUSE_S,
                             thresh_db: float = CF_GAP_THRESH_DB,
                             edge_keep_s: float = CF_GAP_EDGE_KEEP_S,
                             min_delta_s: float = CF_GAP_MIN_DELTA_S,
                             scene=None) -> bool:
    """Punctuation-aware inter-word gap shaping on a FINAL scene wav, IN PLACE.

    Whispers the wav (resident CPU model) for word timestamps, maps each whisper word
    to a narration token BY ORDER (monotonic proportional index — robust to whisper's
    English mis-transcription because we need only the POSITION, not the identity), then
    for every gap between consecutive whisper words sets the silence length to:
      punct_s  if the narration token at/left of the gap ends with punctuation,
      clause_s otherwise (mid-clause).
    The silence is resized by trimming or padding ONLY within the low-energy run between
    the two words, keeping edge_keep_s of original samples on each side (no onset/tail
    clip). Word interiors are never touched (we only edit BETWEEN whisper words).

    Returns True if the file was modified. Fail-safe: whisper unavailable / no words /
    any error → returns False and leaves the file untouched.

    OBSERVABILITY (owner request 2026-07-28). Every exit path emits a `[gap-shape]` marker
    to STDERR. The marker prefix matters: _run_cf_worker_once forwards ONLY lines starting
    with a known marker prefix from the worker's stderr into api.log, so a plain log.*
    call (which gets a timestamp prefix) is invisible to the API log. Before this, the
    pass failed SILENTLY — it just returned False — which is why we could not tell whether
    it was even running on the scenes that were measured as 20-40% silence."""
    def _mark(msg: str) -> None:
        tag = scene if scene is not None else os.path.basename(wav_path)
        print(f"[gap-shape] scene={tag} {msg}", file=sys.stderr, flush=True)

    wm = _head_whisper()
    if wm is None:
        _mark("SKIPPED reason=whisper-unavailable")
        return False
    import numpy as _np
    import soundfile as _sf
    try:
        segs, _info = wm.transcribe(wav_path, language="vi", word_timestamps=True)
        _wlist = [w for s in segs for w in (s.words or [])]
        words = [(float(w.start), float(w.end)) for w in _wlist]
        data, sr = _sf.read(wav_path, dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.reshape(data.shape[0], -1).mean(axis=1)
        data = _np.asarray(data, dtype=_np.float32).reshape(-1)
    except Exception as e:
        log.warning("gap-shape whisper failed for %s (%s)", os.path.basename(wav_path), e)
        _mark(f"SKIPPED reason=whisper-error err={type(e).__name__}")
        return False
    if len(words) < 2:
        _mark(f"SKIPPED reason=too-few-words words={len(words)}")
        return False

    ntoks = re.findall(r"\S+", narration or "")
    punct_flags = _narration_punct_after(narration)
    n_tok = len(punct_flags)
    n_w = len(words)
    # Whisper word texts for alignment (identity is unreliable for English, but the
    # normalized prefix still anchors most Vietnamese tokens, which is enough to keep the
    # monotonic map on track between the rarer English mismatches).
    wtexts = [_norm_align(w.word) for w in _wlist]
    # Build a MONOTONIC token->whisper index alignment by DP (longest common subsequence
    # on normalized-prefix match), then invert to "whisper word index -> narration token
    # index". This anchors punctuation to the CORRECT gap even when whisper split/merged a
    # few words (proportional mapping drifted on such scenes). Falls back to proportional
    # for any whisper word left unmatched.
    ntoks_norm = [_norm_align(t) for t in ntoks]

    def _tok_match(a: str, b: str) -> bool:
        if not a or not b:
            return False
        k = min(len(a), len(b))
        if k < 3:
            return a == b
        return a[:3] == b[:3]

    # DP LCS over (token, whisper) with prefix match.
    w2t = {}
    if wtexts and ntoks_norm:
        T, W = len(ntoks_norm), len(wtexts)
        dp = [[0] * (W + 1) for _ in range(T + 1)]
        for ti in range(T - 1, -1, -1):
            for wi in range(W - 1, -1, -1):
                if _tok_match(ntoks_norm[ti], wtexts[wi]):
                    dp[ti][wi] = 1 + dp[ti + 1][wi + 1]
                else:
                    dp[ti][wi] = max(dp[ti + 1][wi], dp[ti][wi + 1])
        ti = wi = 0
        while ti < T and wi < W:
            if _tok_match(ntoks_norm[ti], wtexts[wi]):
                w2t[wi] = ti
                ti += 1
                wi += 1
            elif dp[ti + 1][wi] >= dp[ti][wi + 1]:
                ti += 1
            else:
                wi += 1

    def _is_punct_gap(wi: int) -> bool:
        # The gap AFTER whisper word wi is a punctuation beat iff the narration token that
        # word maps to ends with punctuation. Use the DP alignment when available, else a
        # proportional fallback. Also treat a mapped token as punct if ANY unmatched token
        # BETWEEN this matched token and the next matched one carries punctuation (a comma
        # that whisper dropped as its own word still belongs in this gap).
        if n_tok == 0:
            return False
        ti = w2t.get(wi)
        if ti is None:
            ti = int(round(wi * (n_tok - 1) / max(1, n_w - 1))) if n_w > 1 else 0
            ti = min(n_tok - 1, max(0, ti))
            return punct_flags[ti]
        # next matched token index for this whisper position
        nxt = None
        for k in range(wi + 1, n_w):
            if k in w2t:
                nxt = w2t[k]
                break
        end_ti = (nxt - 1) if nxt is not None else ti
        end_ti = min(n_tok - 1, max(ti, end_ti))
        return any(punct_flags[ti:end_ti + 1])

    # Speech-edge guard kept on each side of every gap (samples), so a soft word onset/
    # tail ramp is never clipped when the silence between two words is resized.
    edge = int(edge_keep_s * sr)

    # Build the output by walking words and rewriting each inter-word gap.
    out_parts: list = []
    # head: everything up to the end of the first word (keep leading edge intact)
    prev_end_s = words[0][1]
    out_parts.append(data[: int(prev_end_s * sr)])
    changed = False
    # Diagnostics for the [gap-shape] marker: how much silence existed vs how much we
    # left, and how many gaps we actually touched. `punct_gaps` + `matched` together tell
    # whether the punctuation was anchored to the right gaps or the alignment drifted.
    resized = 0
    punct_gaps = 0
    sil_before_n = 0
    sil_after_n = 0
    for wi in range(1, n_w):
        gap_lo = int(words[wi - 1][1] * sr)   # end of previous word
        gap_hi = int(words[wi][0] * sr)       # start of current word
        # Locate the true low-energy span inside [gap_lo, gap_hi]: from the last loud
        # sample after gap_lo to the first loud sample before gap_hi (so we keep the
        # word tail/onset ramps and only resize the SILENCE between them).
        seg = data[gap_lo:gap_hi]
        is_punct = _is_punct_gap(wi - 1)
        punct_gaps += 1 if is_punct else 0
        target = punct_s if is_punct else clause_s
        target_n = int(target * sr)
        if seg.size <= 0:
            # whisper says words abut; INSERT silence if a punctuation beat is required.
            if is_punct and target_n > int(min_delta_s * sr):
                out_parts.append(_np.zeros(target_n, dtype=_np.float32))
                sil_after_n += target_n
                resized += 1
                changed = True
            out_parts.append(data[gap_hi: int(words[wi][1] * sr)])
            continue
        # measured silence length = whole gap minus the kept edges
        keep_each = min(edge, seg.size // 2)
        sil_len = seg.size - 2 * keep_each
        new_sil = max(0, target_n - 2 * keep_each) if target_n > 0 else 0
        sil_before_n += sil_len
        if abs(new_sil - sil_len) < int(min_delta_s * sr):
            out_parts.append(seg)  # already close enough
            sil_after_n += sil_len
        else:
            left = seg[:keep_each]
            right = seg[seg.size - keep_each:]
            mid = _np.zeros(new_sil, dtype=_np.float32)
            out_parts.append(_np.concatenate([left, mid, right]))
            sil_after_n += new_sil
            resized += 1
            changed = True
        out_parts.append(data[gap_hi: int(words[wi][1] * sr)])
    # tail after the last word
    out_parts.append(data[int(words[-1][1] * sr):])
    # `matched` = whisper words the LCS anchored to a narration token. A LOW ratio means
    # the punctuation was placed by the proportional fallback, i.e. the beats may be on
    # the wrong gaps — the prime suspect on number/loanword-heavy scenes.
    stats = (f"words={n_w} tokens={n_tok} matched={len(w2t)}/{n_w} "
             f"punct_gaps={punct_gaps} resized={resized} "
             f"silence={sil_before_n / sr:.2f}s->{sil_after_n / sr:.2f}s")
    if not changed:
        _mark(f"NOCHANGE {stats}")
        return False
    out = _np.concatenate([p for p in out_parts if p.size]).astype(_np.float32)
    if out.size < int(0.1 * sr):
        _mark(f"SKIPPED reason=output-too-short {stats}")
        return False
    _sf.write(wav_path, out, sr, subtype="PCM_16")
    _mark(f"OK {stats}")
    return True


# ---- DURABLE non-punctuation gap removal (energy + whisper-word + punctuation) --
#
# The core recurring problem: F5 is alignment-free and STOCHASTIC per draw, so its
# hesitation pauses land at DIFFERENT words on every job. Point-fixes never generalize.
# LEVER 1 (_compress_internal_silence_np) is energy-ONLY: it cannot tell a mid-phrase
# beat between two BOUND words ("kết|nối", "định nghĩa|nó", "năm|2026", "pờ|rôm") from a
# real punctuation beat, so it only compresses to a ~0.06 s floor — still audible.
#
# This pass removes that residual GENERALLY. On the FINAL scene wav it:
#   1. whispers the wav (resident CPU small model) for word timestamps + text,
#   2. detects low-energy silence RUNS (5 ms RMS frames, -40 dB) — whisper reports
#      adjacent words FLUSH (0 ms) so it cannot localize these beats itself; ENERGY can,
#   3. for each run, finds the whisper words that BRACKET it (last word ending before the
#      run, first word starting after it), maps them to NARRATION tokens by DP alignment
#      (same monotonic LCS as _shape_gaps_by_alignment), and reads the punctuation between
#      those tokens from the KNOWN narration text,
#   4. if there is NO punctuation between the two bracketing words (a bound lexical unit /
#      pre-year junction that must flow) → BUTT-JOIN: remove the run's middle down to
#      ~CF_PS_NONPUNCT_GAP_S (~0.02 s), keeping a small energy edge-guard on each side so a
#      soft voiced consonant/vowel tail is never clipped,
#   5. if there IS punctuation (comma/period/colon/…) → LEAVE the beat (only clamp a very
#      long one down to CF_PS_PUNCT_MAX_S so a runaway pause stays natural).
# Word interiors are never touched (we only resize the silence BETWEEN bracketing words).
# Fail-safe: whisper unavailable / <2 words / any error → return False, file untouched.
#
# This is the ONE mechanism the owner asked for: it makes bound pairs flow REGARDLESS of
# where F5 randomly puts the pause, while keeping the natural beat at real punctuation.
CF_PS_NONPUNCT_SHAPE = os.getenv("CF_PS_NONPUNCT_SHAPE", "1").strip().lower() not in ("0", "off", "false", "no")
# Target length a NO-PUNCTUATION inter-word gap is butt-joined to (seconds). ~0.02 s leaves
# only a zero-crossing micro-seam so the two bound words flow as one continuous unit.
CF_PS_NONPUNCT_GAP_S = float(os.getenv("CF_PS_NONPUNCT_GAP_S", "0.02"))
# A gap shorter than this is already tight — do not touch it (nothing to remove).
CF_PS_NONPUNCT_MIN_RUN_S = float(os.getenv("CF_PS_NONPUNCT_MIN_RUN_S", "0.055"))
# Longest beat kept at a PUNCTUATION boundary (seconds). A real comma/period beat is left
# as-is up to this; a runaway pause above it is clamped down (keeps punctuation natural
# without ever eliminating the beat). 0 disables the clamp (keep any punctuation beat).
CF_PS_PUNCT_MAX_S = float(os.getenv("CF_PS_PUNCT_MAX_S", "0.24"))
# Silence threshold (dB below clip peak) for locating the low-energy run. -40 dB matches
# the probe used to diagnose these beats; soft voiced tails sit above it (kept as speech).
CF_PS_NONPUNCT_THRESH_DB = float(os.getenv("CF_PS_NONPUNCT_THRESH_DB", "-40.0"))
# Energy edge-guard (seconds) kept on each side of a removed run so a soft word onset/tail
# ramp is never clipped when the silence between two words is shrunk.
CF_PS_NONPUNCT_EDGE_S = float(os.getenv("CF_PS_NONPUNCT_EDGE_S", "0.025"))

# ---- CTC-BACKBONE alignment (items 2/4/6 durable fix, owner-approved 2026-07-05) --------
#
# Diagnosis (job 122): the energy-only gap shaper STRUCTURALLY leaves the mid-phrase beat,
# because F5's inter-word "beat" between two bound words is NOT sub-(-40 dB) silence — it is
# a VOICED vowel-tail glide + breath-level tone at -9..-40 dB (measured: "tùy→chỉnh" 201 ms =
# ~120 ms voiced glide + ~80 ms silence; "năm→2026" 280 ms breath at -40..-52 dB fragmented
# by the threshold). Only a WORD-BOUNDARY-aware method can compress that inter-word region.
# whisper gives boundaries too, but mis-transcribes/mis-segments loanword-heavy Vietnamese
# ("prompt"→"Perum", count 13 vs 12) so its boundaries + the karaoke word_map DRIFT (measured
# up to 1.27 s lag). CTC forced alignment of the KNOWN text (already installed, CPU, offline)
# is accurate regardless of transcription, and precise at word boundaries — so we use it as
# the alignment backbone for BOTH the gap shaper and the karaoke word_map.
#
# CF_ALIGN_BACKEND: "ctc" (default) uses CTC word boundaries; anything else keeps the legacy
# whisper/energy path. Per-scene FAIL-SAFE: if CTC alignment fails or returns an implausible
# result for a scene, that scene falls back to the whisper/energy path (never hard-fails the
# job). Each scene logs which path it used.
CF_ALIGN_BACKEND = os.getenv("CF_ALIGN_BACKEND", "ctc").strip().lower()
# Edge guard for the CTC path (seconds). CTC word boundaries are PRECISE (word-END /
# word-START exclude the phonemes), so a smaller guard than the energy path (0.025) is safe
# and lets the compressed junction reach the <60 ms target instead of flooring at ~80 ms.
CF_CTC_EDGE_S = float(os.getenv("CF_CTC_EDGE_S", "0.012"))


def _shape_nonpunct_gaps_by_ctc(wav_path: str, narration: str,
                                gap_s: float = CF_PS_NONPUNCT_GAP_S,
                                punct_max_s: float = CF_PS_PUNCT_MAX_S,
                                edge_s: float = CF_CTC_EDGE_S) -> bool | None:
    """CTC-backbone inter-word gap shaper on a FINAL scene wav, IN PLACE.

    Uses CTC forced alignment of the KNOWN narration for precise per-word [start,end], then
    for each consecutive word pair rewrites the region STRICTLY BETWEEN word-END(prev) and
    word-START(next):
      * NO punctuation between them → compress that whole inter-word region (voiced glide-tail
        + breath, which the energy-only pass structurally missed) down to gap_s. Cutting only
        BETWEEN the two CTC word boundaries never touches a phoneme (CTC bounds are precise).
      * punctuation between them → keep the beat, clamped to punct_max_s (item 1 lowered it).
    A small edge_s guard is kept on each side so a soft onset/tail ramp is preserved.

    Returns True if modified, False if nothing needed changing, or None if CTC was unusable /
    implausible (caller then falls back to the energy path for this scene). Never raises."""
    import numpy as _np
    import soundfile as _sf
    words = None
    try:
        words = _ctc_align_words(wav_path, narration)
    except Exception as e:
        log.warning("CTC gap-shape align raised for %s (%s)", os.path.basename(wav_path), e)
        return None
    if not words or len(words) < 2:
        return None
    try:
        data, sr = _sf.read(wav_path, dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.reshape(data.shape[0], -1).mean(axis=1)
        data = _np.asarray(data, dtype=_np.float32).reshape(-1)
    except Exception:
        return None
    n = data.size
    dur = n / float(sr)
    # PLAUSIBILITY: the last CTC word must end within the clip (a small pad ok). If CTC ran
    # far past/short of the audio, the alignment is untrustworthy → fall back.
    if words[-1][2] > dur + 0.20 or words[-1][2] < 0.2 * dur:
        return None
    # Punctuation per SPOKEN token. The CTC word list is the year-expanded spoken sequence;
    # _normalize_years only rewrites the year digits (no punctuation lost), so punctuation on
    # a token maps positionally. Build a punct flag per spoken token from the spoken text.
    spoken = _normalize_years(narration or "")
    spoken_toks = re.findall(r"\S+", spoken)
    # keep only tokens that romanize (same filter _ctc_align_words applied) so indices align
    from unidecode import unidecode as _uni
    ali = _ctc_aligner()
    _d = ali[1] if ali else {}

    def _rom(w: str) -> str:
        r = re.sub(r"[^a-z']", "", _uni(w).lower())
        return "".join(ch for ch in r if ch in _d and _d[ch] != 0)

    punct_flags = []
    for t in spoken_toks:
        if not _rom(t):
            continue
        stripped = t.rstrip(")]}\"'»”’")
        punct_flags.append(bool(stripped) and stripped[-1] in ",.;:!?…")
    # punct_flags now parallels `words` (both are the romanizable spoken tokens, in order).
    if len(punct_flags) != len(words):
        # token/word count mismatch (rare) → cannot trust the punctuation map → fall back.
        return None

    edge_n = int(edge_s * sr)
    gap_n = int(gap_s * sr)
    punct_cap_n = int(punct_max_s * sr) if punct_max_s > 0 else 0
    kept: list = []          # ("KEEP", a, b) slices and ("ZERO", nsamp) inserts, in order
    cursor = 0
    changed = False
    for wi in range(1, len(words)):
        prev_end = words[wi - 1][2]   # (word, start, end) → end
        cur_start = words[wi][1]      # → start
        glo = int(prev_end * sr)
        ghi = int(cur_start * sr)
        # gap AFTER token (wi-1): punctuation on that token → keep a (capped) beat.
        is_punct = punct_flags[wi - 1]
        region = ghi - glo
        if ghi <= glo or region <= 0:
            continue  # words abut / overlap per CTC — nothing between them
        if is_punct:
            target = punct_cap_n if (punct_cap_n and region > punct_cap_n) else region
        else:
            target = gap_n
        if target >= region:
            continue  # already at/under target — leave this region intact
        # keep [.., glo+edge) + target-2*edge zeros + [ghi-edge, ..)
        keep_each = min(edge_n, (region - target) // 2, region // 2)
        new_mid = max(0, target - 2 * keep_each)
        kept.append(("KEEP", cursor, glo + keep_each))
        kept.append(("ZERO", new_mid))
        cursor = ghi - keep_each
        changed = True
    if not changed:
        return False
    kept.append(("KEEP", cursor, n))
    parts = []
    for item in kept:
        if item[0] == "ZERO":
            if item[1] > 0:
                parts.append(_np.zeros(int(item[1]), dtype=_np.float32))
        else:
            a, b = item[1], item[2]
            if b > a:
                parts.append(data[int(a):int(b)])
    out = _np.concatenate(parts).astype(_np.float32) if parts else data
    if out.size < int(0.1 * sr):
        return None
    _sf.write(wav_path, out, sr, subtype="PCM_16")
    return True


def _shape_nonpunct_gaps_by_energy(wav_path: str, narration: str,
                                   gap_s: float = CF_PS_NONPUNCT_GAP_S,
                                   min_run_s: float = CF_PS_NONPUNCT_MIN_RUN_S,
                                   punct_max_s: float = CF_PS_PUNCT_MAX_S,
                                   thresh_db: float = CF_PS_NONPUNCT_THRESH_DB,
                                   edge_s: float = CF_PS_NONPUNCT_EDGE_S) -> bool:
    """Remove NO-PUNCTUATION inter-word silence gaps on a FINAL scene wav, IN PLACE.

    See the block comment above. Returns True if the file was modified; False (file
    untouched) on any whisper/alignment failure or when no gap qualified."""
    wm = _head_whisper()
    if wm is None:
        return False
    import numpy as _np
    import soundfile as _sf
    try:
        segs, _info = wm.transcribe(wav_path, language="vi", word_timestamps=True)
        _wlist = [w for s in segs for w in (s.words or [])]
        words = [(float(w.start), float(w.end)) for w in _wlist]
        data, sr = _sf.read(wav_path, dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.reshape(data.shape[0], -1).mean(axis=1)
        data = _np.asarray(data, dtype=_np.float32).reshape(-1)
    except Exception as e:
        log.warning("nonpunct-gap whisper failed for %s (%s)", os.path.basename(wav_path), e)
        return False
    if len(words) < 2 or data.size < int(0.2 * sr):
        return False

    ntoks = re.findall(r"\S+", narration or "")
    punct_flags = _narration_punct_after(narration)
    n_tok = len(punct_flags)
    if n_tok == 0:
        return False
    wtexts = [_norm_align(w.word) for w in _wlist]
    ntoks_norm = [_norm_align(t) for t in ntoks]

    def _tok_match(a: str, b: str) -> bool:
        if not a or not b:
            return False
        k = min(len(a), len(b))
        if k < 3:
            return a == b
        return a[:3] == b[:3]

    # Monotonic DP LCS map: whisper-word-index -> narration-token-index (same as the
    # legacy shaper). We need only POSITION, so whisper's English mis-transcription is fine.
    w2t: dict[int, int] = {}
    W = len(wtexts)
    if wtexts and ntoks_norm:
        T = len(ntoks_norm)
        dp = [[0] * (W + 1) for _ in range(T + 1)]
        for ti in range(T - 1, -1, -1):
            for wi in range(W - 1, -1, -1):
                if _tok_match(ntoks_norm[ti], wtexts[wi]):
                    dp[ti][wi] = 1 + dp[ti + 1][wi + 1]
                else:
                    dp[ti][wi] = max(dp[ti + 1][wi], dp[ti][wi + 1])
        ti = wi = 0
        while ti < T and wi < W:
            if _tok_match(ntoks_norm[ti], wtexts[wi]):
                w2t[wi] = ti
                ti += 1
                wi += 1
            elif dp[ti + 1][wi] >= dp[ti][wi + 1]:
                ti += 1
            else:
                wi += 1

    def _tok_of(wi: int) -> int:
        ti = w2t.get(wi)
        if ti is not None:
            return ti
        # proportional fallback for an unmatched whisper word
        return min(n_tok - 1, max(0, int(round(wi * (n_tok - 1) / max(1, W - 1)))))

    def _punct_between(left_wi: int, right_wi: int) -> bool:
        # True if ANY narration token from the LEFT word's token up to (but not incl.)
        # the RIGHT word's token ends with punctuation. Inclusive of unmatched tokens in
        # the span (a comma whisper dropped still belongs to this gap).
        lt = _tok_of(left_wi)
        rt = _tok_of(right_wi)
        lo = min(lt, rt)
        hi = max(lt, rt)
        # the gap sits AFTER token lo; punctuation on any token in [lo, hi-1] is a beat.
        end = max(lo, hi - 1)
        return any(punct_flags[lo:end + 1])

    # Energy silence runs (5 ms RMS frames vs clip peak).
    fl = max(1, int(sr * 0.005))
    nf = data.size // fl
    if nf == 0:
        return False
    fr = data[: nf * fl].reshape(nf, fl)
    rms = _np.sqrt((fr ** 2).mean(axis=1) + 1e-12)
    peak = float(rms.max()) or 1.0
    db = 20.0 * _np.log10(rms / peak + 1e-12)
    sil = db < thresh_db

    first_start = words[0][0]
    last_end = words[-1][1]
    edge_n = int(edge_s * sr)
    gap_n = int(gap_s * sr)
    punct_cap_n = int(punct_max_s * sr) if punct_max_s > 0 else 0

    # Collect internal silence runs and decide each one's target length.
    kept: list[tuple[int, int]] = []  # sample ranges to KEEP
    cursor = 0
    i = 0
    changed = False
    while i < nf:
        if sil[i]:
            j = i
            while j < nf and sil[j]:
                j += 1
            s, e = i * fl, j * fl
            run = e - s
            run_start_t = s / float(sr)
            run_end_t = e / float(sr)
            # Only INTERNAL runs strictly between the first and last spoken word, and
            # long enough to be an audible beat rather than a natural micro-gap.
            internal = run_start_t > first_start + 0.01 and run_end_t < last_end - 0.01
            if internal and run >= int(min_run_s * sr):
                # bracket: last whisper word ending at/before the run start; first word
                # starting at/after the run end.
                left_wi = None
                right_wi = None
                for wi in range(W):
                    if words[wi][1] <= run_start_t + 0.06:
                        left_wi = wi
                    if right_wi is None and words[wi][0] >= run_end_t - 0.06:
                        right_wi = wi
                if left_wi is not None and right_wi is not None and right_wi > left_wi:
                    is_punct = _punct_between(left_wi, right_wi)
                    if is_punct:
                        # keep the beat; only clamp a runaway pause.
                        target = punct_cap_n if (punct_cap_n and run > punct_cap_n) else run
                    else:
                        # bound pair -> butt-join to ~gap_n.
                        target = gap_n
                    if target < run:
                        keep_each = min(edge_n, (run - target) // 2, run // 2)
                        new_mid = max(0, target - 2 * keep_each)
                        # keep [s, s+keep_each) + new_mid zeros + [e-keep_each, e)
                        kept.append((cursor, s + keep_each))
                        # emit the shrunk middle by leaving a zero-fill: represent by
                        # jumping the cursor and appending a synthetic short silence.
                        kept.append(("ZERO", new_mid))  # sentinel handled below
                        cursor = e - keep_each
                        changed = True
                        i = j
                        continue
            i = j
        else:
            i += 1
    if not changed:
        return False
    kept.append((cursor, data.size))
    parts: list = []
    for a_, b_ in kept:
        if a_ == "ZERO":
            if b_ > 0:
                parts.append(_np.zeros(int(b_), dtype=_np.float32))
        else:
            if b_ > a_:
                parts.append(data[int(a_):int(b_)])
    out = _np.concatenate(parts).astype(_np.float32) if parts else data
    if out.size < int(0.1 * sr):
        return False
    _sf.write(wav_path, out, sr, subtype="PCM_16")
    return True


# ---- Trailing F5 end-hallucination trim (spurious "ờ"/"ổn" fragment) ---------
#
# F5 sometimes APPENDS a spurious short voiced syllable at the END of a clip's inference
# ("kết quả không lý tưởng: [ổn]" — job 116 scene016). It is VOICED, so the leading/
# trailing SILENCE trim (energy-based) never removes it, and it is not dead air the gap
# shaper touches. Signature (verified on the real clip): the REAL last word ("tưởng",
# ~0.20 s) is followed by a clear SILENCE GAP (>= min_gap_s), then a SHORT voiced blip
# (<= max_frag_s) that runs to the clip end. We whisper the CLIP, and iff whisper found
# MORE words than the clip's narration has (an extra token), the last spoken run is a
# short blip after a gap, and the clip narration has >1 word (never trim a legitimately
# short single-word clip), we cut at the gap. Conservative + fail-safe: any ambiguity →
# leave the clip untouched (better to leak a faint "ờ" than clip a real final word).
#
# SAFE FIX via CTC FORCED ALIGNMENT (owner-approved 2026-07-05, ctc-forced-aligner /
# torchaudio MMS_FA). The old whisper-word-count heuristic was UNSAFE on loanword-dense
# clips (it clipped the real word "lặp" of scene46). CTC forced alignment of the EXACT
# KNOWN narration text gives the TRUE end timestamp of the last REAL word — it aligns to
# the known text, so a mis-toned real word ("lặp") still maps to its own position and is
# never mistaken for a hallucination. We then trim ONLY a SUSTAINED HIGH-ENERGY voiced
# region AFTER that end (a full hallucinated syllable, e.g. "ổn"/"ợt" at ~-9 dB), while a
# real word's natural decay tail (always low-level, ~-31..-44 dB — measured scene46) is
# LEFT intact. Measured separation is huge (hallucination ~-9 dB vs real tail ~-33 dB), so
# CF_PS_TRAIL_VOICED_DB=-20 cleanly divides them.
#
# DEFAULT ON after the safety sweep passed (0 real-word clips across 49 scenes). The CTC
# model (MMS_FA, ~1.18 GB) runs on CPU (never GPU — must not contend with F5's VRAM) and
# loads OFFLINE from the torch-hub cache. Fail-safe: any align/load error → clip untouched.
CF_PS_TRAIL_HALLUC = os.getenv("CF_PS_TRAIL_HALLUC", "1").strip().lower() not in ("0", "off", "false", "no")
# A voiced region after the last real word louder than this (dB below clip peak) for at
# least CF_PS_TRAIL_VOICED_MIN_S is a hallucinated syllable → trim. A real word's decay
# tail sits well below this. Measured: hallucination ~-9 dB, real "lặp" tail ~-33 dB.
CF_PS_TRAIL_VOICED_DB = float(os.getenv("CF_PS_TRAIL_VOICED_DB", "-20.0"))
CF_PS_TRAIL_VOICED_MIN_S = float(os.getenv("CF_PS_TRAIL_VOICED_MIN_S", "0.06"))
# --- Decay-vs-hallucination discriminator + soft cut (job-138 tail-clip fix) --------
# BUG (job 138, "thể"/"gì"/"cũ" chopped): the "sustained voiced region after the CTC
# word-end" test above ALSO fires on the NATURAL trailing decay of an OPEN/soft clause-
# final Vietnamese syllable. Measured on the "cụ thể." clip: CTC ends "thể" at 3.19s, but
# the word's own release ramps -11.5 -> -13 -> -16 -> -20 -> -25 -> -33 -> -48 dB over the
# next ~130 ms — a monotonic decay that STARTS loud (well above the -20 dB threshold) and
# glides to silence. The old code saw ">= 60 ms above -20 dB" and hard-cut at word_end+20 ms,
# removing 201 ms of that decay and leaving an abrupt digital-silence edge (the reported
# clipped/cut-off tail). The design assumption "hallucination ~-9 dB vs real tail ~-33 dB,
# cleanly split at -20 dB" is FALSE for these open syllables.
#
# The real, robust difference: a genuine F5 end-hallucination ("ờ"/"ổn") is a SEPARATE
# blurt — the word finishes and decays to (near) silence, THEN after a short gap a new
# voiced region appears. A natural decay is CONTINUOUS from the word-end (no intervening
# silence). So we only treat trailing voiced energy as a hallucination when it is preceded
# by a SILENCE GAP after the word; voiced energy continuous with the word-end is its own
# decay and is KEPT. CF_PS_TRAIL_GAP_MIN_S = the minimum silence gap (s) that must separate
# the word decay from a candidate hallucination. Set 0 to disable the gap requirement and
# restore the old behaviour (not recommended).
# NOTE (job-142): this tail-keep adds ~4 s across the whole video (decay retained on every
# clause-final clip). The owner ACCEPTS that (natural tails > the ~4 s). The earlier "video
# too slow" was a SEPARATE pace-measurement bug — the CTC caption word_map was read into
# generate.py::_auto_target_pace and misread the pace (~140 vs ~200 ms/syll → spurious
# ×1.42) — fixed independently there, NOT by touching this trim.
CF_PS_TRAIL_GAP_MIN_S = float(os.getenv("CF_PS_TRAIL_GAP_MIN_S", "0.08"))
# Silence threshold (dB below clip peak) used to detect the gap AND the end of the decay.
# A frame below this is "silence". -38 dB sits below the loud head of a decay but above true
# dead air, so the decay's own low tail (-30..-48 dB) reads as speech and is preserved.
CF_PS_TRAIL_GAP_DB = float(os.getenv("CF_PS_TRAIL_GAP_DB", "-38.0"))
# When a hallucination IS trimmed, keep this much of the CLIP's own audio after the word's
# natural-decay end before cutting (a small natural pad, never a hard cut at the word edge),
# and fade the last CF_PS_TRAIL_FADE_S of the kept audio to zero so the edge is never abrupt.
CF_PS_TRAIL_PAD_S = float(os.getenv("CF_PS_TRAIL_PAD_S", "0.06"))
CF_PS_TRAIL_FADE_S = float(os.getenv("CF_PS_TRAIL_FADE_S", "0.03"))
# CTC aligner forced to CPU (F5 owns the 8 GB GPU). torch-hub cache path is machine-local.
CF_CTC_DEVICE = os.getenv("CF_CTC_DEVICE", "cpu")

# Lazily-loaded, process-global CTC forced-alignment handle (torchaudio MMS_FA on CPU).
_CTC_ALIGN = None          # (model, dict) tuple once loaded
_CTC_ALIGN_FAILED = False


def _ctc_aligner():
    """Load the MMS_FA CTC forced-alignment model on CPU (once). Offline: the weights are
    in the torch-hub cache after a one-time download. Returns (model, char->idx dict) or
    None on failure (caller falls back to leaving the clip untouched)."""
    global _CTC_ALIGN, _CTC_ALIGN_FAILED
    if _CTC_ALIGN is not None or _CTC_ALIGN_FAILED:
        return _CTC_ALIGN
    try:
        import torch, torchaudio
        bundle = torchaudio.pipelines.MMS_FA
        d = bundle.get_dict(star=None)               # {char: idx}, blank ('-') at 0
        model = bundle.get_model(with_star=False).to(CF_CTC_DEVICE).eval()
        _CTC_ALIGN = (model, d, int(bundle.sample_rate))
    except Exception as e:  # pragma: no cover - environmental
        log.warning("CTC aligner unavailable (%s); trailing-halluc trim disabled", e)
        _CTC_ALIGN_FAILED = True
    return _CTC_ALIGN


def _ctc_align_words(clip_path: str, narration: str):
    """Forced-align the KNOWN narration to `clip_path` and return per-word timings:
    [(word_text, start_s, end_s), ...] for every SPOKEN word, in order — or None on any
    failure. This is the shared CTC primitive behind the gap shaper (items 4/6), the
    karaoke word_map, and the trailing-halluc trim.

    Robust to English loanwords / mis-toned words because it aligns the KNOWN text (each
    word gets its own precise span at its own position), unlike whisper which mis-
    transcribes + mis-segments loanword-heavy Vietnamese ("prompt"→"Perum", count drift).
    Years/numbers are expanded via _normalize_years FIRST so F5's digit-by-digit reading
    ("2026" → "hai không hai sáu") aligns THROUGH the year instead of stopping at "năm".
    The returned word list corresponds to the SPOKEN (year-expanded) token sequence."""
    ali = _ctc_aligner()
    if ali is None:
        return None
    model, d, sr_model = ali
    import torch, torchaudio
    import torchaudio.functional as _AF
    import soundfile as _sf
    from unidecode import unidecode

    def _rom(w: str) -> str:
        r = re.sub(r"[^a-z']", "", unidecode(w).lower())
        return "".join(ch for ch in r if ch in d and d[ch] != 0)

    spoken = _normalize_years(narration or "")
    words = re.findall(r"\S+", spoken)
    rom = [_rom(w) for w in words]
    kept = [(w, r) for w, r in zip(words, rom) if r]
    if not kept:
        return None
    w2 = [w for w, _r in kept]
    rom2 = [r for _w, r in kept]
    try:
        data, sr = _sf.read(clip_path, dtype="float32", always_2d=False)
        if getattr(data, "ndim", 1) > 1:
            data = data.mean(axis=1)
        wav = torch.tensor(data, dtype=torch.float32).unsqueeze(0)
        if sr != sr_model:
            wav = torchaudio.functional.resample(wav, sr, sr_model)
        with torch.inference_mode():
            emission, _ = model(wav.to(CF_CTC_DEVICE))
        tokens = [d[c] for r in rom2 for c in r]
        targets = torch.tensor([tokens], dtype=torch.int32, device=CF_CTC_DEVICE)
        aligned, scores = _AF.forced_align(emission, targets, blank=0)
        spans = _AF.merge_tokens(aligned[0], scores[0])
        ratio = wav.size(1) / emission.size(1)
        out = []
        i = 0
        for w, r in zip(w2, rom2):
            L = len(r)
            wspans = spans[i:i + L]
            i += L
            if not wspans:
                continue
            st = float(ratio * wspans[0].start / sr_model)
            en = float(ratio * wspans[-1].end / sr_model)
            out.append((w, st, en))
        return out or None
    except Exception as e:
        log.warning("CTC align failed for %s (%s)", os.path.basename(clip_path), e)
        return None


def _ctc_last_word_end(clip_path: str, narration: str):
    """Return the end time (seconds) of the LAST real narration word via CTC alignment of
    the KNOWN text, or None on any failure. Thin wrapper over _ctc_align_words. Aborts
    (returns None) if the last SPOKEN word did not romanize (a bare number/symbol the
    aligner can't represent) — better to keep the clip than trust an unreliable end."""
    words = _ctc_align_words(clip_path, narration)
    if not words:
        return None
    return words[-1][2]


def _trim_trailing_halluc(clip_path: str, narration: str) -> bool:
    """Trim a spurious trailing F5 hallucination ("ờ"/"ổn"/"ợt") off a clip END, IN PLACE.

    CTC-aligns the KNOWN narration to get the last REAL word's end, then trims ONLY a
    SUSTAINED HIGH-ENERGY voiced region after it (a hallucinated syllable). A real word's
    low-level decay tail is left intact, so a mis-toned/loanword final word ("lặp") is never
    clipped. Returns True if modified, False otherwise. Fail-safe on any error."""
    import numpy as _np
    import soundfile as _sf
    if len([t for t in re.findall(r"\S+", narration or "")]) < 2:
        return False  # never touch a legitimately-short single-word clip
    t_end = _ctc_last_word_end(clip_path, narration)
    if t_end is None or t_end <= 0.0:
        return False
    try:
        data, sr = _sf.read(clip_path, dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.reshape(data.shape[0], -1).mean(axis=1)
        data = _np.asarray(data, dtype=_np.float32).reshape(-1)
    except Exception:
        return False
    n = data.size
    st = int(t_end * sr)
    if st <= 0 or st >= n:
        return False
    tail = data[st:]
    if tail.size < int(CF_PS_TRAIL_VOICED_MIN_S * sr):
        return False  # nothing meaningful after the last word
    # Frame the region after the CTC word-end (10 ms RMS vs the clip peak).
    peak = float(_np.max(_np.abs(data))) or 1.0
    fl = max(1, int(sr * 0.01))  # 10 ms frames
    nf = tail.size // fl
    if nf == 0:
        return False
    fr = tail[: nf * fl].reshape(nf, fl)
    rms = _np.sqrt((fr ** 2).mean(axis=1) + 1e-12)
    db = 20.0 * _np.log10(rms / peak + 1e-12)
    voiced = db > CF_PS_TRAIL_VOICED_DB
    need = max(1, int(CF_PS_TRAIL_VOICED_MIN_S / 0.01))
    # longest run of consecutive voiced frames (still required — a decay alone is short-lived
    # and would never sustain, so this stays as the "there IS energy here" precondition).
    best = cur = 0
    for v in voiced:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    if best < need:
        return False  # only low-level decay/silence after the last word → keep intact
    #
    # DECAY-vs-HALLUCINATION (job-138 fix): the word's OWN natural release decays CONTINUOUSLY
    # from the word-end down to silence with no intervening gap. A genuine F5 end-hallucination
    # is a SEPARATE blurt: the word decays to (near) silence, THEN after a short silence gap a
    # new voiced region appears. So (1) find where the word's decay reaches silence (the first
    # sustained sub-CF_PS_TRAIL_GAP_DB run of >= CF_PS_TRAIL_GAP_MIN_S), then (2) only if there
    # is voiced energy AFTER that gap do we trim — cutting at the decay-end (+ a natural pad and
    # a short fade), never into the word's own decay. If the tail is one continuous decay (no
    # gap, i.e. the "thể"/"gì"/"cũ" open-syllable case), we KEEP the clip intact.
    if CF_PS_TRAIL_GAP_MIN_S > 0.0:
        sil = db < CF_PS_TRAIL_GAP_DB
        gap_need = max(1, int(CF_PS_TRAIL_GAP_MIN_S / 0.01))
        # Scan for: [some voiced (the decay)] -> [>= gap_need silence] -> [voiced again].
        decay_end_f = None      # frame index (in tail) where the word decay reaches silence
        i = 0
        # advance past the initial voiced decay region
        while i < nf and not sil[i]:
            i += 1
        # count the first silence run
        run = 0
        j = i
        while j < nf and sil[j]:
            run += 1
            j += 1
        if run >= gap_need and j < nf:
            # a real gap, then more audio -> the post-gap region is the hallucination.
            decay_end_f = i
        if decay_end_f is None:
            # No separating gap: the trailing energy is the word's own continuous decay
            # (or there is no post-gap blurt). Keep the clip intact — never chop the decay.
            return False
        # Cut at the decay-end + a small natural pad; the fade below removes any hard edge.
        cut_n = min(n, st + decay_end_f * fl + int(CF_PS_TRAIL_PAD_S * sr))
    else:
        # Legacy behaviour (gap requirement disabled): hard cut just after the word-end.
        cut_n = min(n, st + int(0.02 * sr))
    if cut_n <= 0 or cut_n >= n or cut_n < int(0.1 * sr):
        return False
    out = data[:cut_n].astype(_np.float32).copy()
    # Fade the last CF_PS_TRAIL_FADE_S of the kept audio to zero so the new END is a natural
    # ramp, not an abrupt cut (belt-and-braces even though we cut in a low-energy region).
    fn = min(int(CF_PS_TRAIL_FADE_S * sr), out.size // 4)
    if fn > 0:
        out[-fn:] *= _np.linspace(1.0, 0.0, fn, dtype=_np.float32)
    if out.size < int(0.1 * sr):
        return False
    _sf.write(clip_path, out, sr, subtype="PCM_16")
    log.info("trail-halluc(CTC): trimmed %.0f ms of trailing hallucination from %s "
             "(gap-gated, faded)",
             (n - cut_n) / float(sr) * 1000.0, os.path.basename(clip_path))
    return True


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
    intra_gap_s = float(os.getenv("TTS_INTRA_GAP_S", "0.05"))  # ~20% less than old 0.06
    speed_gap_s = float(os.getenv("TTS_SPEED_GAP_S", "0.005"))  # tight gap at speed transitions
    sent_gap = np.zeros(int(sent_gap_s * sr), dtype=np.float32)
    intra_gap = np.zeros(int(intra_gap_s * sr), dtype=np.float32)
    speed_gap = np.zeros(int(speed_gap_s * sr), dtype=np.float32)
    # One-beat pause inserted at a "--" (double-hyphen) separator (F5_PAUSE_BEAT_S).
    pause_beat = np.zeros(int(_F5_PAUSE_BEAT_S * sr), dtype=np.float32)

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
        # FIRST split at speed/pause markers (\x01-\x05) so a single-hyphen say_as / a
        # year carries its FAST atempo (F5_FAST_FACTOR), a slow term carries SLOW
        # (F5_SLOW_FACTOR), and a "--" separator becomes a PAUSE beat. Each speech
        # segment is then chunked sentence-by-sentence to avoid the frame-cap garble
        # failure. Flatten into (chunk_text, ends_sentence, atempo) triples; a PAUSE
        # segment becomes an empty-text chunk tagged with _PAUSE_FACTOR (no inference,
        # just inserts pause_beat at concat). Source of truth: word_improve.md.
        speed_segs = _split_by_speed(spoken)
        all_chunks: list[tuple[str, bool, float]] = []
        for seg_text, seg_speed in speed_segs:
            if seg_speed == _PAUSE_FACTOR:
                all_chunks.append(("", False, _PAUSE_FACTOR))  # pause beat placeholder
                continue
            # split_chinh=False: the "chính" forced-split guards an F5-ViVoice -í
            # artifact that VieNeu does not produce, so VieNeu skips it (no wasted gap).
            for chunk_text, ends_sent in _split_for_tts(seg_text.replace('\xa0', ' '), split_chinh=False):
                all_chunks.append((chunk_text, ends_sent, seg_speed))
        parts = []
        prev_ends_sentence = True  # gap BEFORE a chunk reflects the PREVIOUS boundary
        prev_atempo = 1.0           # track speed of last chunk to detect transitions
        just_paused = False         # skip the next chunk's leading gap after a pause beat
        for _ci, (chunk_text, ends_sentence, chunk_atempo) in enumerate(all_chunks):
            # PAUSE placeholder: insert one beat of silence, no inference, no gap logic.
            if chunk_atempo == _PAUSE_FACTOR:
                if parts:
                    parts.append(pause_beat)
                    just_paused = True  # the beat IS the separation; don't add a gap too
                continue
            w = tts.infer(
                chunk_text.replace('\xa0', ' '), ref_codes=ref_codes, voice=voice,
                emotion=emotion, apply_watermark=apply_watermark, **tune,
            )
            w = np.asarray(w, dtype=np.float32).reshape(-1)
            w = np.asarray(_trim_silence_np(w, sr), dtype=np.float32).reshape(-1)
            # Apply atempo to FAST (years / 1-hyphen joins) or SLOW (prompt) segments
            # via FFmpeg (round-trip through temp wavs since VieNeu returns a numpy
            # array, not a file). Pause placeholders never reach here (handled above).
            if chunk_atempo != 1.0 and w.size:
                import tempfile as _tf, soundfile as _sf
                with _tf.NamedTemporaryFile(suffix=".wav", delete=False) as _ft:
                    _tmp_src = _ft.name
                with _tf.NamedTemporaryFile(suffix=".wav", delete=False) as _ft:
                    _tmp_dst = _ft.name
                try:
                    _sf.write(_tmp_src, w, sr, subtype="PCM_16")
                    _apply_atempo(_tmp_src, _tmp_dst, chunk_atempo)
                    w_fast, _ = _sf.read(_tmp_dst, dtype="float32", always_2d=False)
                    # A2: 30%-tighter edge pad on FAST (1-hyphen / year) segments so the
                    # hyphenated unit sits flush against its neighbours; SLOW keeps 0.12.
                    _pad = (_FAST_TRIM_PAD_S if chunk_atempo > 1.0 else 0.12)
                    w = np.asarray(_trim_silence_np(w_fast, sr, pad_s=_pad), dtype=np.float32).reshape(-1)
                finally:
                    for _p in (_tmp_src, _tmp_dst):
                        try:
                            os.unlink(_p)
                        except OSError:
                            pass
            if w.size:
                if parts and not just_paused:
                    if chunk_atempo != prev_atempo:
                        parts.append(speed_gap)
                    elif prev_ends_sentence:
                        parts.append(sent_gap)
                    else:
                        parts.append(intra_gap)
                parts.append(w)
                prev_ends_sentence = ends_sentence
                prev_atempo = chunk_atempo
                just_paused = False
        wav = np.concatenate(parts) if parts else np.zeros(int(0.2 * sr), dtype=np.float32)
        # Conservative internal-silence compression: cap any long internal dead-air
        # (model prosody pause before numbers, residual inter-chunk gaps) to keep_s
        # without changing the speaking rate. No-op when CF_TTS_SIL_CAP_S=0.
        wav = np.asarray(_compress_internal_silence_np(wav, sr), dtype=np.float32).reshape(-1)
        # De-click edge fade (bug 4): 5 ms fade-in/out so the scene never starts/ends
        # with an abrupt amplitude step (a "bụp" pop). CF_TTS_EDGE_FADE_S=0 disables.
        _ef = float(os.getenv("CF_TTS_EDGE_FADE_S", "0.005"))
        _efn = min(int(_ef * sr), wav.size // 4)
        if _efn > 0:
            wav = wav.copy()
            wav[:_efn] *= np.linspace(0.0, 1.0, _efn, dtype=np.float32)
            wav[-_efn:] *= np.linspace(1.0, 0.0, _efn, dtype=np.float32)
        name = f"scene_{scene:03d}.wav" if isinstance(scene, int) else "audio.wav"
        path = out_dir / name
        out_dir.mkdir(parents=True, exist_ok=True)  # shared page audio dir may be removed mid-run by a sibling job's cleanup
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
    # A1: -50 dB (was -40, then -45 — still clipped) so a soft word-final syllable ("ngữ")
    # at the scene head/tail is not clipped; true dead air is still well below -50 dB.
    _trim_db = os.getenv("CF_TTS_TRIM_DB", "-50").rstrip("dB") or "-50"
    af = (
        "aresample=resampler=soxr,"
        f"silenceremove=start_periods=1:start_silence=0.12:start_threshold={_trim_db}dB,"
        "areverse,"
        f"silenceremove=start_periods=1:start_silence=0.12:start_threshold={_trim_db}dB,"
        "areverse"
    )
    # Ensure the destination directory exists right before writing. The page's shared
    # `audio` dir is normally created once in main(), but it can be removed mid-run by a
    # sibling job's cleanup (the jobs share one page audio dir), which made ffmpeg fail
    # with "Error opening output ... No such file or directory". A write must always
    # guarantee its own dir — cheap and idempotent.
    _dst_dir = os.path.dirname(dst)
    if _dst_dir:
        os.makedirs(_dst_dir, exist_ok=True)
    proc = subprocess.run(
        [_ffmpeg_bin(), "-y", "-i", src, "-ar", str(sr), "-ac", "1",
         "-af", af, "-c:a", "pcm_s16le", dst],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0 or not os.path.isfile(dst):
        raise RuntimeError(f"F5 resample failed: {(proc.stderr or '')[-500:]}")


# Lazily-loaded, process-global whisper handle used ONLY to locate the head-protect
# filler for trimming. Runs on CPU/int8 with a SMALL model: F5 (~6 GB) is already
# resident on the 8 GB GPU when this fires, so we must NOT take GPU VRAM here; the
# transcription target is a ~1 s head clip, trivial on CPU. Loaded once, reused for
# every scene's first chunk.
_HEAD_WHISPER = None
_HEAD_WHISPER_FAILED = False


def _head_whisper():
    global _HEAD_WHISPER, _HEAD_WHISPER_FAILED
    if _HEAD_WHISPER is not None or _HEAD_WHISPER_FAILED:
        return _HEAD_WHISPER
    try:
        from faster_whisper import WhisperModel
        model_name = os.getenv("F5_HEAD_WHISPER_MODEL", "small")
        _HEAD_WHISPER = WhisperModel(model_name, device="cpu", compute_type="int8")
    except Exception as e:  # pragma: no cover - environmental
        log.warning("head-protect whisper unavailable (%s); lead-in trim disabled", e)
        _HEAD_WHISPER_FAILED = True
    return _HEAD_WHISPER


# A5 fallback bounds: the head filler "vâng rồi." renders in roughly this window and
# is followed by a clear silence gap (it is period-terminated). When whisper fails to
# transcribe the filler we cut at that gap instead of leaking the filler. Conservative
# so a filler-free clip is never harmed. All env-overridable.
# Window where the period-terminated filler "vâng rồi." plausibly ENDS. Widened from
# the original [0.25, 1.10]: at F5's normal pace "vâng rồi." renders in ~0.4-0.7 s, but
# F5/ViVoice frequently prepends a leading ref-tail ECHO artifact (see memory:
# f5-tts-leading-echo) of ~0.3-0.9 s BEFORE the filler, pushing the filler's end-gap out
# toward ~1.3-1.6 s. The old 1.10 s cap landed BEFORE that gap, so the scanner found no
# qualifying silence and the whole filler leaked. Cap raised to 1.70 s and MIN lowered to
# 0.20 s so a fast render is still caught. The window stays bounded + gap-length-gated, so
# a filler-free clip (no long silence in this range) is still never mangled.
_LEADIN_FILLER_MIN_S = float(os.getenv("F5_LEADIN_FILLER_MIN_S", "0.20"))
_LEADIN_FILLER_MAX_S = float(os.getenv("F5_LEADIN_FILLER_MAX_S", "1.70"))
_LEADIN_GAP_MIN_S = float(os.getenv("F5_LEADIN_GAP_MIN_S", "0.07"))


def _find_leadin_gap_cut(data, sr: int, thresh_db: float = -38.0) -> float:
    """Locate the first SILENCE gap that plausibly separates the head filler from the
    real content, returning its MIDPOINT time (seconds) — or 0.0 if none qualifies.

    Used as the A5 fallback when whisper cannot transcribe the "vâng rồi." filler: the
    filler is period-terminated so F5 renders a clear gap after it. We scan 5 ms RMS
    frames for the first run of silence that (a) STARTS within [_LEADIN_FILLER_MIN_S,
    _LEADIN_FILLER_MAX_S] (where the short filler plausibly ends), (b) lasts at least
    _LEADIN_GAP_MIN_S, and (c) leaves >=0.3 s of audio after it. The midpoint of that
    gap is returned (a clean cut point between near-zero samples). Conservative by
    design: returns 0.0 (no cut) unless all bounds are satisfied, so a clip that has
    NO filler (e.g. a non-head chunk, or a filler-free render) is never mangled."""
    import numpy as _np

    a = _np.asarray(data, dtype=_np.float32).reshape(-1)
    n = a.size
    if n < int(0.5 * sr):
        return 0.0
    fl = max(1, int(sr * 0.005))
    nf = n // fl
    if nf == 0:
        return 0.0
    fr = a[: nf * fl].reshape(nf, fl)
    rms = _np.sqrt((fr ** 2).mean(axis=1) + 1e-12)
    peak = float(rms.max()) or 1.0
    db = 20.0 * _np.log10(rms / peak + 1e-12)
    sil = db < thresh_db
    f_min = int(_LEADIN_FILLER_MIN_S * sr) // fl
    f_max = int(_LEADIN_FILLER_MAX_S * sr) // fl
    gap_min = max(1, int(_LEADIN_GAP_MIN_S * sr) // fl)
    i = max(1, f_min)  # never cut at the very first frame (leading edge)
    while i < min(nf, f_max):
        if sil[i]:
            j = i
            while j < nf and sil[j]:
                j += 1
            if (j - i) >= gap_min and i >= f_min:
                mid = ((i + j) // 2) * fl
                if n - mid >= int(0.3 * sr):
                    return mid / float(sr)
            i = j
        else:
            i += 1
    return 0.0


def _trim_leadin_by_whisper(wav_path: str, filler_text: str) -> None:
    """Trim the head-protect filler off the START of a freshly-synthesised chunk WAV.

    Transcribes the clip with faster-whisper WORD timestamps (CPU), finds the END
    time of the LAST filler syllable, and rewrites the file with everything up to a
    hair before that boundary removed. Robust to comma/ellipsis lists (it keys on the
    filler's own tokens, not on silence gaps, which a list has many of).

    Conservative & fail-safe: if whisper is unavailable, the filler tokens are not
    found, or the computed cut would remove the whole clip / almost nothing, the file
    is left UNCHANGED (better to leak a short filler than to clip the real first word).
    Operates in-place; expects a mono PCM wav at any sample rate."""
    import re as _re
    import numpy as _np
    import soundfile as _sf

    wm = _head_whisper()
    if wm is None:
        return
    # Filler word stems (strip punctuation) — match by accent-insensitive prefix so
    # whisper's casing/diacritic jitter ("Vâng"/"vang"/"Vầng") still aligns.
    def _norm(s: str) -> str:
        return _re.sub(r"[^\w]", "", s.strip().lower())
    filler_tokens = [_norm(t) for t in filler_text.split() if _norm(t)]
    if not filler_tokens:
        return
    try:
        segments, _info = wm.transcribe(wav_path, language="vi", word_timestamps=True)
        words = []
        for seg in segments:
            for w in (seg.words or []):
                words.append(w)
        if not words:
            return
        # Walk the leading words; consume those that look like the filler. Stop at the
        # first word that does NOT match a remaining filler token — that is the real
        # content onset. cut = end time of the last consumed filler word.
        cut_t = 0.0
        consumed = 0
        for w in words[: len(filler_tokens) + 1]:
            wn = _norm(w.word)
            if consumed < len(filler_tokens) and wn and (
                wn.startswith(filler_tokens[consumed][:3]) or filler_tokens[consumed].startswith(wn[:3])
            ):
                cut_t = float(w.end)
                consumed += 1
            else:
                break
        data, sr = _sf.read(wav_path, dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.reshape(data.shape[0], -1).mean(axis=1)
        if consumed < len(filler_tokens) or cut_t <= 0.0:
            # FALLBACK (A5 fix, hardened A5b): whisper did NOT recognise the WHOLE filler
            # in its transcript. The OLD behaviour only fired this fallback when NOTHING
            # was consumed (consumed == 0); a PARTIAL match (e.g. it heard "vâng" but
            # transcribed "rồi" as "dồi"/"zồi" or merged it into the next word) left
            # consumed == 1 and cut at the end of "vâng" only — so "rồi" still leaked
            # (the owner's "âm dư vâng rồi" at ~31-33 s). Now ANY partial match
            # (consumed < len(filler_tokens)) ALSO routes to the silence-gap scanner,
            # which cuts past the WHOLE filler at the period-terminated gap. The filler is
            # period-terminated, so F5 always renders a clear SILENCE GAP after it before
            # the real content. We locate that first gap and cut there. Bounded tightly so
            # a genuinely filler-free clip is never mauled: the gap must START within the
            # window where the filler plausibly ends (FILLER_MIN..MAX seconds), be at
            # least GAP_MIN long, and leave substantial audio after it.
            gap_cut = _find_leadin_gap_cut(data, sr)
            if gap_cut <= 0.0:
                # No confident filler-gap found. If we had a PARTIAL whisper match, the
                # cut at the last recognised filler syllable is still better than leaking
                # the whole filler — keep it. Only bail (leave audio intact) when we have
                # nothing at all (consumed == 0), so a filler-free clip is never clipped.
                if consumed == 0 or cut_t <= 0.0:
                    return
                # else: fall through using the partial cut_t (best-effort).
            else:
                cut_t = gap_cut
                consumed = -1  # sentinel: cut came from the silence-gap fallback
        # Cut AFTER the filler's end. Whisper's word-end sits in the inter-word gap,
        # but the filler's release/breath can still ring for a few ms past it and the
        # next word's onset is close — so instead of a fixed offset we snap the cut to
        # the QUIETEST 5 ms inside a search window just after cut_t. Landing in the
        # true silence trough (a) drops the loud filler-tail fragment that used to leak
        # as a "bụp" at scene start, and (b) puts the cut between near-zero samples so
        # there is no waveform step. We then fade-in the new head to guarantee no click.
        base = cut_t + 0.005
        win0 = int(base * sr)
        win1 = min(len(data), int((base + 0.10) * sr))  # search up to 100 ms forward
        cut = int((cut_t + 0.015) * sr)  # fallback
        if win1 - win0 > int(0.01 * sr):
            fl = max(1, int(sr * 0.005))
            seg = data[win0:win1]
            nf = len(seg) // fl
            if nf > 0:
                fr = seg[: nf * fl].reshape(nf, fl)
                rms = _np.sqrt((fr ** 2).mean(axis=1) + 1e-12)
                quiet = int(_np.argmin(rms))
                cut = win0 + quiet * fl
        # Refine to the nearest zero-crossing within the next 3 ms so the very first
        # sample is ~0 (belt-and-suspenders against a click), then fade-in.
        zwin = min(len(data) - 1, cut + int(0.003 * sr))
        for k in range(cut, zwin):
            if data[k] == 0.0 or (data[k] <= 0.0 < data[k + 1]) or (data[k] >= 0.0 > data[k + 1]):
                cut = k
                break
        if cut <= 0 or len(data) - cut < int(0.3 * sr):
            return
        out = data[cut:].astype(_np.float32).copy()
        fade_n = min(int(0.005 * sr), len(out) // 4)  # 5 ms fade-in
        if fade_n > 0:
            out[:fade_n] *= _np.linspace(0.0, 1.0, fade_n, dtype=_np.float32)
        _sf.write(wav_path, out, sr, subtype="PCM_16")
    except Exception as e:
        log.warning("lead-in trim skipped for %s (%s)", os.path.basename(wav_path), e)


def _measure_loanwords(wav_path: str, loanwords: list[str]) -> tuple[float, list[str]]:
    """Whisper a freshly-synthesised chunk wav and score how well its LOANWORDS came out.

    Returns (score, problems):
      * score  — the WORST (lowest) ms-per-syllable across the loanwords that whisper
                 DID find, in [0, +inf). A rushed/clipped read has a low score. Used to
                 pick the better of two attempts (higher score = better articulated).
                 If no loanword is found at all, score = 0.0 (worst) so a re-render wins.
      * problems — the loanwords that FAILED the check: either not transcribed at all
                 (mis-heard/clipped, e.g. "prompt"→"Prom" so whisper has no "prompt"
                 token; the token match is prefix-based so "Prom" still matches "prompt"
                 by its first syllables — see below) OR below F5_LOANWORD_MIN_MS_PER_SYL.

    Matching: a loanword is matched to a whisper word when either normalised form is a
    prefix of the other for >=3 chars (so "prompt"↔"prom", "engineering"↔"engineer" still
    align to the intended token; a hard clip that drops the whole word finds NO match →
    counted as a problem). Uses the resident CPU whisper singleton (_head_whisper); if
    whisper is unavailable it returns (inf, []) so repair is a safe no-op.

    NOTE: `loanwords` are the ORIGINAL English tokens (from the pre-pron-map chunk text).
    Even when the SPOKEN text respelled them, whisper hears the intended sound, so a good
    render still transcribes close to the English word; a rushed one clips it."""
    wm = _head_whisper()
    if wm is None:
        return float("inf"), []
    try:
        segments, _info = wm.transcribe(wav_path, language="vi", word_timestamps=True)
        wwords = []
        for seg in segments:
            for w in (seg.words or []):
                wwords.append((_norm_token(w.word), float(w.start), float(w.end)))
    except Exception as e:
        log.warning("loanword measure whisper failed for %s (%s)", os.path.basename(wav_path), e)
        return float("inf"), []

    def _matches(loan: str, heard: str) -> bool:
        if not heard:
            return False
        n = min(len(loan), len(heard))
        if n < 3:
            return loan == heard
        return loan[:3] == heard[:3] and (loan.startswith(heard) or heard.startswith(loan) or loan[:n] == heard[:n])

    worst = float("inf")
    problems: list[str] = []
    for loan in loanwords:
        cand = None
        for heard, st, en in wwords:
            if _matches(loan, heard):
                cand = (heard, st, en)
                break
        if cand is None:
            # Whisper did not transcribe this loanword near-correctly → clipped/mis-heard.
            problems.append(loan)
            worst = min(worst, 0.0)
            continue
        dur_ms = (cand[2] - cand[1]) * 1000.0
        ms_per_syl = dur_ms / _count_syllables(loan)
        worst = min(worst, ms_per_syl)
        if ms_per_syl < F5_LOANWORD_MIN_MS_PER_SYL:
            problems.append(loan)
    if worst == float("inf"):
        worst = 0.0
    return worst, problems


def _atempo_stretch_np(seg, sr: int, factor: float):
    """Time-stretch a mono float slice, pitch-preserving, via FFmpeg atempo. factor<1
    LENGTHENS (0.5 → 2× longer). atempo's valid range is 0.5-2.0, so a factor below 0.5
    is reached by chaining. Returns the stretched float32 array (or the original on any
    ffmpeg failure — fail-safe)."""
    import tempfile as _tf
    import numpy as _np
    import soundfile as _sf
    with _tf.TemporaryDirectory() as _d:
        si = os.path.join(_d, "si.wav"); so = os.path.join(_d, "so.wav")
        _sf.write(si, _np.asarray(seg, dtype=_np.float32).reshape(-1), sr, subtype="PCM_16")
        af = f"atempo=0.5,atempo={factor/0.5:.4f}" if factor < 0.5 else f"atempo={factor:.4f}"
        proc = subprocess.run(
            [_ffmpeg_bin(), "-y", "-i", si, "-af", af, "-ar", str(sr), "-ac", "1",
             "-c:a", "pcm_s16le", so],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0 or not os.path.isfile(so):
            return _np.asarray(seg, dtype=_np.float32).reshape(-1)
        out, _ = _sf.read(so, dtype="float32", always_2d=False)
        return _np.asarray(out, dtype=_np.float32).reshape(-1)


def _slow_scene_terms(wav_path: str, terms: list[str], factor: float) -> bool:
    """Slow each occurrence of `terms` in a FINAL scene wav to `factor` speed (0.5 =
    2× duration), IN PLACE, keeping everything else at natural pace. Returns True if the
    file was modified.

    Mechanism: whisper (resident CPU model) the scene for word timestamps; for each word
    whose normalised form matches a slow-term (prefix match, so a clipped "prom" still
    catches "prompt"), extract that slice PLUS an F5_SLOW_XFADE_S guard on each side,
    time-stretch it with _atempo_stretch_np, and overlap-add it back with an equal-power
    crossfade so the splice is click-free. Processes matches RIGHT-TO-LEFT so earlier
    word indices stay valid as later slices grow. Fail-safe: any error leaves the file
    unchanged (better a fast word than a corrupted scene)."""
    if factor >= 1.0 or not terms:
        return False
    wm = _head_whisper()
    if wm is None:
        return False
    import numpy as _np
    import soundfile as _sf
    try:
        segments, _info = wm.transcribe(wav_path, language="vi", word_timestamps=True)
        wwords = [w for seg in segments for w in (seg.words or [])]
        data, sr = _sf.read(wav_path, dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.reshape(data.shape[0], -1).mean(axis=1)
        data = _np.asarray(data, dtype=_np.float32).reshape(-1)
    except Exception as e:
        log.warning("slow-term whisper failed for %s (%s)", os.path.basename(wav_path), e)
        return False

    def _match(w) -> bool:
        n = _norm_token(w.word)
        for t in terms:
            k = min(len(t), len(n))
            if k >= 3 and t[:3] == n[:3] and (t.startswith(n) or n.startswith(t)):
                return True
            if k < 3 and t == n:
                return True
        return False

    hits = [(float(w.start), float(w.end)) for w in wwords if _match(w)]
    if not hits:
        return False
    xf = max(1, int(F5_SLOW_XFADE_S * sr))
    changed = False
    for start_s, end_s in sorted(hits, key=lambda h: -h[0]):  # right-to-left
        i0 = max(0, int(start_s * sr)); i1 = min(len(data), int(end_s * sr))
        if i1 - i0 < int(0.03 * sr):
            continue
        a = max(0, i0 - xf); b = min(len(data), i1 + xf)
        slowed = _atempo_stretch_np(data[a:b], sr, factor)
        head = data[:a].copy()
        tail = data[b:].copy()
        if len(slowed) > 2 * xf and len(head) >= xf and len(tail) >= xf:
            fin = _np.sqrt(_np.linspace(0, 1, xf, dtype=_np.float32))
            fout = _np.sqrt(_np.linspace(1, 0, xf, dtype=_np.float32))
            slowed[:xf] *= fin
            slowed[-xf:] *= fout
            head[-xf:] *= fout
            tail[:xf] *= fin
            data = _np.concatenate([
                head[:-xf],
                head[-xf:] + slowed[:xf],
                slowed[xf:-xf],
                slowed[-xf:] + tail[:xf],
                tail[xf:],
            ]).astype(_np.float32)
        else:
            data = _np.concatenate([head, slowed, tail]).astype(_np.float32)
        changed = True
    if changed:
        _sf.write(wav_path, data, sr, subtype="PCM_16")
    return changed


def _tighten_scene_acronyms(wav_path: str, narration: str, factor: float) -> bool:
    """Compress each ACRONYM's audio region in a FINAL scene wav toward a tight unit
    (atempo `factor` > 1), IN PLACE. Returns True if modified. Owner: acronyms must read
    as ONE compact unit, not spelled out slowly.

    Region finding (robust to the unpredictable multi-syllable respelling of an acronym):
    an acronym in `narration` (a run of 2+ uppercase letters, incl. the caps of "chatGPT")
    is bounded by its PRECEDING and FOLLOWING ordinary narration words, which whisper
    transcribes reliably. We locate those anchor words in the whisper transcript (accent-
    insensitive prefix match) and compress the audio span BETWEEN them — that span is the
    acronym's spelled-out syllables. If an anchor can't be found confidently the acronym
    is SKIPPED (fail-safe: never compress an unbounded/ambiguous span). Never SLOWS
    (factor clamped >= 1.0 by the caller). Caption keeps the original text (assembler
    builds captions from narration, not this audio)."""
    if factor <= 1.0 or not narration:
        return False
    # narration word list (keep order); find acronym positions.
    ntoks = re.findall(r"\S+", narration)
    acr_idx = [i for i, t in enumerate(ntoks) if _ACRONYM_RE.search(t)]
    if not acr_idx:
        return False
    wm = _head_whisper()
    if wm is None:
        return False
    import numpy as _np
    import soundfile as _sf

    def _norm(s: str) -> str:
        return re.sub(r"[^\w]", "", s.strip().lower())

    try:
        segments, _info = wm.transcribe(wav_path, language="vi", word_timestamps=True)
        wwords = [w for seg in segments for w in (seg.words or [])]
        data, sr = _sf.read(wav_path, dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.reshape(data.shape[0], -1).mean(axis=1)
        data = _np.asarray(data, dtype=_np.float32).reshape(-1)
    except Exception as e:
        log.warning("acronym-tighten whisper failed for %s (%s)", os.path.basename(wav_path), e)
        return False

    wnorm = [_norm(w.word) for w in wwords]

    def _find_anchor(word: str, after_idx: int) -> int | None:
        """Index in wwords of the first whisper word matching `word` at/after after_idx."""
        wn = _norm(word)
        if len(wn) < 2:
            return None
        for j in range(max(0, after_idx), len(wwords)):
            h = wnorm[j]
            if h and (h.startswith(wn[:3]) or wn.startswith(h[:3])) and abs(len(h) - len(wn)) <= 3:
                return j
        return None

    # Build compress regions (start_s, end_s) between the anchor words around each acronym.
    regions = []
    w_cursor = 0
    for ai in acr_idx:
        prev_word = ntoks[ai - 1] if ai > 0 else None
        next_word = ntoks[ai + 1] if ai + 1 < len(ntoks) else None
        # locate the acronym's audio span:
        start_s = end_s = None
        if prev_word:
            pj = _find_anchor(prev_word, w_cursor)
            if pj is not None:
                start_s = float(wwords[pj].end)
                w_cursor = pj + 1
        if next_word:
            nj = _find_anchor(next_word, w_cursor)
            if nj is not None:
                end_s = float(wwords[nj].start)
        # If we only found one side, skip (ambiguous). Require BOTH anchors for safety,
        # unless the acronym is the FIRST/LAST word (then use scene edge conservatively).
        if start_s is None or end_s is None or end_s - start_s < 0.15 or end_s - start_s > 2.5:
            continue
        # Short-acronym coda guard: a region already compact (below the floor) is a short
        # 2-3 letter acronym whose final syllable would clip if compressed (MCP→"MC"). Skip
        # it — only DRAGGED acronyms (region >= floor, e.g. ChatGPT ~0.54 s) are tightened.
        if F5_ACRONYM_MIN_REGION_S > 0 and (end_s - start_s) < F5_ACRONYM_MIN_REGION_S:
            log.info("acronym-tighten: skip short region %.0f ms (< %.0f ms floor) — coda guard",
                     (end_s - start_s) * 1000, F5_ACRONYM_MIN_REGION_S * 1000)
            continue
        regions.append((start_s, end_s))

    if not regions:
        return False
    xf = max(1, int(F5_SLOW_XFADE_S * sr))
    changed = False
    for start_s, end_s in sorted(regions, key=lambda r: -r[0]):  # right-to-left
        i0 = max(0, int(start_s * sr)); i1 = min(len(data), int(end_s * sr))
        if i1 - i0 < int(0.1 * sr):
            continue
        a = max(0, i0 - xf); b = min(len(data), i1 + xf)
        comp = _atempo_stretch_np(data[a:b], sr, factor)
        head = data[:a].copy(); tail = data[b:].copy()
        if len(comp) > 2 * xf and len(head) >= xf and len(tail) >= xf:
            fin = _np.sqrt(_np.linspace(0, 1, xf, dtype=_np.float32))
            fout = _np.sqrt(_np.linspace(1, 0, xf, dtype=_np.float32))
            comp[:xf] *= fin
            comp[-xf:] *= fout
            head[-xf:] *= fout
            tail[:xf] *= fin
            data = _np.concatenate([
                head[:-xf], head[-xf:] + comp[:xf], comp[xf:-xf], comp[-xf:] + tail[:xf], tail[xf:],
            ]).astype(_np.float32)
        else:
            data = _np.concatenate([head, comp, tail]).astype(_np.float32)
        changed = True
    if changed:
        _sf.write(wav_path, data, sr, subtype="PCM_16")
    return changed


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


def _concat_wavs_48k(part_paths: list[str], out_path: str,
                     gap_s: float | list[float] = 0.10) -> None:
    """Concatenate canonical-rate (48 kHz mono) WAV chunks into one WAV, inserting a
    short silence between consecutive chunks so split sentences do not run together.

    Single chunk → copied straight through (NO extra leading/trailing silence and no
    re-encode artifacts: behaves exactly like the old single-infer path). Reads/writes
    with soundfile (available in cf-venv alongside f5_tts). gap_s defaults to 100 ms —
    a natural inter-sentence breath, matching the VieNeu sentence-gap range. gap_s may
    also be a list of N-1 floats (one gap per boundary) so the caller can shrink the
    gap after a "chính"-ending chunk (whose tail is already faded) — see bug 2."""
    import numpy as np
    import soundfile as sf

    if len(part_paths) == 1:
        # Fast path: no concatenation needed — preserve the exact bytes/duration the
        # old single-call path produced (no added silence at the ends).
        if part_paths[0] != out_path:
            data, sr = sf.read(part_paths[0], dtype="float32", always_2d=False)
            sf.write(out_path, data, sr, subtype="PCM_16")
        return

    parts: list = []
    sr_seen = CANONICAL_SR
    for i, p in enumerate(part_paths):
        data, sr = sf.read(p, dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.reshape(data.shape[0], -1).mean(axis=1)
        sr_seen = sr
        if parts:
            g = float(gap_s[i - 1]) if isinstance(gap_s, list) else float(gap_s)
            if g > 0:
                parts.append(np.zeros(int(g * sr), dtype=np.float32))
        parts.append(data.astype(np.float32))
    wav = np.concatenate(parts) if parts else np.zeros(int(0.2 * sr_seen), dtype=np.float32)
    sf.write(out_path, wav, sr_seen, subtype="PCM_16")


# ======================================================================================
# Vietnamese correction memory (CF_VI_CORRECTIONS) — fixes known problem terms so videos
# render them correctly, for ANY clone voice. SINGLE mechanism (no cache, nothing frozen):
#   * best-of-N reselect + whisper verify (ALL problem terms) — synthesize the term FRESH;
#     if a draw is bad (split/slur/rush/drag) re-infer the WHOLE clip up to N times and
#     keep the draw whose term scores CLOSEST to the video's own pace (two-sided) with a
#     correct transcription. Whole-clip selection ONLY (no per-region atempo) → constant
#     pace preserved. The kept clip is ALWAYS a fresh synth, so a repeated term (e.g.
#     'agent', the most frequent word) varies naturally per occurrence and per run.
# Voice-INDEPENDENT: no per-voice data is stored or read anywhere, so it covers any voice
# with zero warm-up. Extra work fires ONLY on a clip that CONTAINS a problem term; a clean
# clip runs exactly as before (no whisper / no reroll). Store: Dashboard/api/
# vi_corrections.json (human-editable). NOTE: an earlier design cached a per-voice
# "golden clip" and spliced it in; that was REMOVED — it made a frequent term play the
# same recording every time (robotic). Nothing is cached now.
# ======================================================================================
CF_VI_CORRECTIONS = os.getenv("CF_VI_CORRECTIONS", "1").strip().lower() not in ("0", "off", "false", "no")
CF_VI_CORRECTIONS_PATH = os.getenv(
    "CF_VI_CORRECTIONS_PATH",
    str(Path(__file__).resolve().parent.parent / "vi_corrections.json"),
)
CF_VI_BEST_OF_N = max(1, int(os.getenv("CF_VI_BEST_OF_N", "3")))
CF_VI_RUSH_FRAC = float(os.getenv("CF_VI_RUSH_FRAC", "0.60"))       # term_mps < target*frac => rushed
CF_VI_MIN_MS_PER_SYL = float(os.getenv("CF_VI_MIN_MS_PER_SYL", "110.0"))  # absolute rushed floor
CF_VI_GAP_MAX_S = float(os.getenv("CF_VI_GAP_MAX_S", "0.30"))      # abnormal internal gap (split/drag), inline terms
CF_VI_MATCH_MIN = float(os.getenv("CF_VI_MATCH_MIN", "0.75"))      # fraction of expected tokens that must match (inline)
# Isolatable terms (agent/agents) render as their OWN F5 chunk. The verify whisper (small
# CPU model) romanizes a loanword respelling unreliably ('ây-jừn' -> 'Ây chừng', 'agent',
# 'ê dừn'...), so we do NOT text-match an English token for these. Instead we verify the
# ISOLATED SEGMENT wav directly: it must read as a tight, un-split, un-clipped, un-dragged
# unit — exactly the split/drag/clip signal. Syllable count comes from the say_as.
CF_VI_ISO_GAP_MAX_S = float(os.getenv("CF_VI_ISO_GAP_MAX_S", "0.16"))   # mid-word split gap
CF_VI_ISO_MIN_MS = float(os.getenv("CF_VI_ISO_MIN_MS", "90.0"))         # per-syll clip/rush floor
CF_VI_ISO_MAX_MS = float(os.getenv("CF_VI_ISO_MAX_MS", "560.0"))        # per-syll drag ceiling
CF_VI_ISO_TARGET_MS = float(os.getenv("CF_VI_ISO_TARGET_MS", "220.0"))  # per-syll best-of-N target


def _vi_fold(s: str) -> str:
    """Accent-fold + lowercase + strip non-alnum for token compares. whisper drops/varies
    diacritics on loanword-heavy VN, so we compare on the romanized skeleton."""
    try:
        from unidecode import unidecode
        s = unidecode(s)
    except Exception:
        pass
    return re.sub(r"[^a-z0-9]", "", s.strip().lower())


def _vi_seg_form(say_as: str) -> str:
    """Decoded FAST/SLOW segment text for a say_as value, as _split_by_speed yields it:
    a 1-hyphen/tilde join becomes space-joined syllables ('ây-jừn' -> 'ây jừn'). Used to
    match a rendered speech segment back to its correction entry."""
    return re.sub(r"\s+", " ", say_as.replace("-", " ").replace("~", " ")).strip()


_VI_CORR_CACHE = None


def _vi_load_corrections() -> list[dict]:
    """Load + resolve vi_corrections.json once per worker run. Each returned entry:
      {term, term_fold, expected(list), expected_fold(list), isolatable(bool),
       say_as(str|None), seg_fold(str|None)}
    For isolatable terms the say_as is resolved from the loaded pron map (_PRON_REPL);
    an isolatable term with no say_as (no speed marker) is DEMOTED to inline (it cannot be
    a standalone chunk, so it can't be spliced). Missing/corrupt file or gate off -> []."""
    global _VI_CORR_CACHE
    if _VI_CORR_CACHE is not None:
        return _VI_CORR_CACHE
    if not CF_VI_CORRECTIONS:
        _VI_CORR_CACHE = []
        return _VI_CORR_CACHE
    out: list[dict] = []
    try:
        raw = json.loads(Path(CF_VI_CORRECTIONS_PATH).read_text(encoding="utf-8"))
        for e in raw.get("corrections", []):
            term = (e.get("term") or "").strip()
            if not term:
                continue
            expected = [str(t).strip() for t in (e.get("expected") or []) if str(t).strip()]
            isolatable = bool(e.get("isolatable"))
            say_as = None
            seg_fold = None
            if isolatable:
                say_as = _PRON_REPL.get(term.lower())
                # A say_as with a FAST/SLOW marker (- or ~) renders as its own chunk. Without
                # one it is NOT actually isolatable -> demote to inline (verify only).
                if say_as and ("-" in say_as or "~" in say_as):
                    seg_fold = _vi_fold(_vi_seg_form(say_as))
                else:
                    isolatable = False
            out.append({
                "term": term,
                "term_fold": _vi_fold(term),
                "term_re": re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE | re.UNICODE),
                "expected": expected,
                "expected_fold": [_vi_fold(t) for t in expected],
                "isolatable": isolatable,
                "say_as": say_as,
                "seg_fold": seg_fold,
            })
    except Exception as e:  # never fatal — feature degrades to off
        log.warning("vi_corrections load failed (%s); feature disabled this run", e)
        out = []
    if out:
        log.info("vi_corrections: %d term(s) from %s (gate on)", len(out), CF_VI_CORRECTIONS_PATH)
    _VI_CORR_CACHE = out
    return out


def _vi_problem_terms(sentence_text: str) -> list[dict]:
    """Corrections whose term occurs (whole-word, case-insensitive) in the ORIGINAL
    sentence text. Empty list ⇒ clean clip ⇒ NO extra whisper/reroll (cost guard)."""
    if not sentence_text:
        return []
    hits: list[dict] = []
    for c in _vi_load_corrections():
        if c["term_re"].search(sentence_text):
            hits.append(c)
    return hits


def _vi_verify_term(words, corr: dict) -> dict:
    """Score ONE correction against a whisper word list [(text, start, end), ...].
    Returns {ok, transcribed, matched, mps, target, gap, penalty, detail}. The window is
    located by the best contiguous run matching the term's expected tokens (accent-folded
    prefix). target = median ms/syll of the clip's OTHER words (self-calibrating per voice
    /draw). penalty is used by best-of-N (lower = better): a two-sided distance to target,
    with big constants for a transcription miss or abnormal internal gap so the net always
    prefers a CORRECT, closest-to-target draw (never 'keep the slower one')."""
    exp = corr["expected_fold"]
    n_exp = len(exp)
    folded = [(_vi_fold(w[0]), w[1], w[2]) for w in words]

    def _tok_match(a: str, b: str) -> bool:
        if not a or not b:
            return False
        k = min(len(a), len(b))
        if k < 3:
            return a == b
        return a[:3] == b[:3] and (a.startswith(b) or b.startswith(a))

    # Best contiguous window: slide over whisper words, count how many expected tokens
    # align in order starting at each position.
    best = {"matched": 0, "i0": -1, "i1": -1}
    for start in range(len(folded)):
        wi = start
        ei = 0
        matched = 0
        last = start - 1
        while wi < len(folded) and ei < n_exp:
            if _tok_match(folded[wi][0], exp[ei]):
                matched += 1
                last = wi
                wi += 1
                ei += 1
            else:
                # allow a spurious extra whisper token inside the window (small skew)
                if ei > 0 and (wi - last) <= 1:
                    wi += 1
                else:
                    break
        if matched > best["matched"]:
            best = {"matched": matched, "i0": start, "i1": last}
        if matched == n_exp:
            break

    matched = best["matched"]
    ratio = matched / max(1, n_exp)
    transcribed = ratio >= CF_VI_MATCH_MIN

    # clip reference pace = median ms/syll of words OUTSIDE the matched window, len>=2.
    others = []
    for idx, (t, st, en) in enumerate(folded):
        if best["i0"] <= idx <= best["i1"]:
            continue
        if len(t) >= 2:
            sy = _count_syllables(words[idx][0])
            if sy > 0 and en > st:
                others.append((en - st) * 1000.0 / sy)
    others.sort()
    target = others[len(others) // 2] if others else 200.0

    mps = None
    gap = 0.0
    if best["i0"] >= 0 and best["i1"] >= best["i0"]:
        st = words[best["i0"]][1]
        en = words[best["i1"]][2]
        syl = sum(_count_syllables(words[j][0]) for j in range(best["i0"], best["i1"] + 1)) or n_exp
        if en > st and syl > 0:
            mps = (en - st) * 1000.0 / syl
        # max internal gap between consecutive matched words (split / drag signal)
        for j in range(best["i0"], best["i1"]):
            gap = max(gap, words[j + 1][1] - words[j][2])

    # verdict + penalty
    rushed = (mps is not None) and (mps < max(CF_VI_MIN_MS_PER_SYL, target * CF_VI_RUSH_FRAC))
    gap_bad = gap > CF_VI_GAP_MAX_S
    ok = transcribed and not rushed and not gap_bad

    if not transcribed:
        penalty = 10000.0 + (n_exp - matched) * 1000.0
    elif gap_bad:
        penalty = 5000.0 + gap * 1000.0
    else:
        penalty = abs((mps if mps is not None else target) - target)

    return {
        "ok": ok, "transcribed": transcribed, "matched": matched, "n_exp": n_exp,
        "mps": mps, "target": target, "gap": gap, "rushed": rushed, "gap_bad": gap_bad,
        "penalty": penalty,
        "detail": (f"term='{corr['term']}' matched={matched}/{n_exp} "
                   f"mps={('%.0f' % mps) if mps is not None else 'NA'} target={target:.0f} "
                   f"gap={gap*1000:.0f}ms -> {'OK' if ok else 'BAD'}"
                   f"{' (rushed)' if rushed else ''}{' (gap)' if gap_bad else ''}"
                   f"{' (mis-transcribed)' if not transcribed else ''}"),
    }


def _vi_verify_clip(clip_path: str, corrs: list[dict]) -> tuple[bool, float, list[dict]]:
    """Whisper the clip ONCE (resident CPU model) and score every problem term on it.
    Returns (all_ok, total_penalty, per_term_results). If whisper is unavailable the clip
    is treated as OK (fail-safe: never worse than the pre-feature behaviour)."""
    wm = _head_whisper()
    if wm is None:
        return True, 0.0, []
    try:
        segments, _info = wm.transcribe(clip_path, language="vi", word_timestamps=True)
        words = [(w.word.strip(), float(w.start), float(w.end))
                 for seg in segments for w in (seg.words or [])]
    except Exception as e:
        log.warning("vi_corrections verify whisper failed for %s (%s)", os.path.basename(clip_path), e)
        return True, 0.0, []
    if not words:
        return True, 0.0, []
    results = [_vi_verify_term(words, c) for c in corrs]
    all_ok = all(r["ok"] for r in results)
    total = sum(r["penalty"] for r in results)
    return all_ok, total, results


def _vi_verify_isolatable_segment(seg_wav: str, corr: dict) -> dict:
    """Verify an ISOLATED term chunk (agent/agents = 'ây jừn') on its OWN segment wav.

    The segment is already the term alone, so we do NOT text-match an English token
    (whisper romanizes loanword respellings unreliably). We whisper the short segment for
    word timestamps and check it reads as ONE tight unit: no abnormal mid-word gap (split),
    per-syllable duration inside a plausible band (not clipped-rushed, not dragged). n_syll
    is derived from the say_as. Fail-safe: whisper unavailable / empty → treated OK (never
    worse than pre-feature). Returns the same result dict shape as _vi_verify_term."""
    n_syll = max(1, len(_vi_seg_form(corr.get("say_as") or "").split()))
    wm = _head_whisper()
    if wm is None or not seg_wav or not os.path.isfile(seg_wav):
        return {"ok": True, "transcribed": True, "matched": n_syll, "n_exp": n_syll,
                "mps": None, "target": CF_VI_ISO_TARGET_MS, "gap": 0.0, "rushed": False,
                "gap_bad": False, "penalty": 0.0,
                "detail": f"term='{corr['term']}' (isolatable) whisper NA -> OK(fail-safe)"}
    try:
        segments, _info = wm.transcribe(seg_wav, language="vi", word_timestamps=True)
        ws = [(w.word.strip(), float(w.start), float(w.end))
              for seg in segments for w in (seg.words or [])]
    except Exception as e:
        log.warning("vi_corrections iso-verify whisper failed for %s (%s)", os.path.basename(seg_wav), e)
        return {"ok": True, "transcribed": True, "matched": n_syll, "n_exp": n_syll,
                "mps": None, "target": CF_VI_ISO_TARGET_MS, "gap": 0.0, "rushed": False,
                "gap_bad": False, "penalty": 0.0,
                "detail": f"term='{corr['term']}' (isolatable) whisper error -> OK(fail-safe)"}
    tokens = len(ws)
    if tokens == 0:  # nothing heard = clipped / silent
        return {"ok": False, "transcribed": False, "matched": 0, "n_exp": n_syll,
                "mps": None, "target": CF_VI_ISO_TARGET_MS, "gap": 0.0, "rushed": True,
                "gap_bad": False, "penalty": 12000.0,
                "detail": f"term='{corr['term']}' (isolatable) NO tokens -> BAD (clipped)"}
    st = ws[0][1]
    en = ws[-1][2]
    gap = max((ws[j + 1][1] - ws[j][2]) for j in range(len(ws) - 1)) if len(ws) > 1 else 0.0
    dur = max(0.0, en - st)
    mps = (dur * 1000.0 / n_syll) if dur > 0 else None
    gap_bad = gap > CF_VI_ISO_GAP_MAX_S
    rushed = (mps is not None) and (mps < CF_VI_ISO_MIN_MS)
    dragged = (mps is not None) and (mps > CF_VI_ISO_MAX_MS)
    ok = (not gap_bad) and (not rushed) and (not dragged)
    if gap_bad:
        penalty = 5000.0 + gap * 1000.0
    else:
        penalty = abs((mps if mps is not None else CF_VI_ISO_TARGET_MS) - CF_VI_ISO_TARGET_MS)
    return {
        "ok": ok, "transcribed": True, "matched": tokens, "n_exp": n_syll,
        "mps": mps, "target": CF_VI_ISO_TARGET_MS, "gap": gap, "rushed": rushed,
        "gap_bad": gap_bad, "penalty": penalty,
        "detail": (f"term='{corr['term']}' (isolatable) heard={tokens}tok "
                   f"mps={('%.0f' % mps) if mps is not None else 'NA'} "
                   f"gap={gap*1000:.0f}ms -> {'OK' if ok else 'BAD'}"
                   f"{' (split/gap)' if gap_bad else ''}{' (clipped)' if rushed else ''}"
                   f"{' (dragged)' if dragged else ''}"),
    }


def _synth_sentence_clip(tts, sentence_text: str, ref_audio: str, ref_text: str,
                         adaptive_speed: float, cfg, td: str, tag: str,
                         head_protect: bool, corr_ctx: dict | None = None) -> str | None:
    """Wrapper around the single-draw clip builder that layers the Vietnamese correction
    memory (CF_VI_CORRECTIONS) on top. A clip with NO problem term takes the plain path
    (one build, no whisper, no reroll) — identical to before. A clip WITH a problem term
    goes through best-of-N + whisper-verify (see _synth_sentence_clip_corrected): build,
    verify the term(s); if bad, re-infer the WHOLE clip up to CF_VI_BEST_OF_N fresh random
    draws and keep the one with the lowest term penalty (closest-to-target pace, correct
    transcription). Whole-clip selection only — constant pace preserved, term stays fresh
    (nothing cached/frozen). Fail-safe: any error falls back to a plain single build."""
    corrs = _vi_problem_terms(sentence_text) if (corr_ctx and corr_ctx.get("enabled")) else []
    if not corrs:
        # Clean clip (or feature off): exact original behaviour, no extra cost.
        return _build_sentence_clip_once(tts, sentence_text, ref_audio, ref_text,
                                         adaptive_speed, cfg, td, tag, head_protect)
    try:
        return _synth_sentence_clip_corrected(
            tts, sentence_text, ref_audio, ref_text, adaptive_speed, cfg, td, tag,
            head_protect, corr_ctx, corrs)
    except Exception as _e:  # never let the correction layer break a clip
        log.warning("vi_corrections layer failed for %s (%s); plain single build", tag, _e)
        return _build_sentence_clip_once(tts, sentence_text, ref_audio, ref_text,
                                         adaptive_speed, cfg, td, f"{tag}fb", head_protect)


def _synth_sentence_clip_corrected(tts, sentence_text, ref_audio, ref_text, adaptive_speed,
                                   cfg, td, tag, head_protect, corr_ctx, corrs):
    """Correction-layer body — best-of-N + whisper-verify ONLY (no golden-clip cache).

    Every occurrence of every problem term is synthesized FRESH here: a bad draw
    (split/slur/rush/drag) triggers a whole-clip re-roll (up to CF_VI_BEST_OF_N fresh
    random draws) and we keep the draw whose term scores best toward the target pace
    (never simply the slower one). The KEPT clip is always a fresh synth, so the same term
    (e.g. 'agent') varies naturally per occurrence and per run. This is voice-independent:
    no per-voice data is stored or read, so it covers ANY clone voice with no warm-up.
    Split out from _synth_sentence_clip so the caller can wrap it in a plain-build fallback."""
    inline_corrs = [c for c in corrs if not (c["isolatable"] and c["seg_fold"])]
    iso_corrs = [c for c in corrs if c["isolatable"] and c["seg_fold"]]
    n = CF_VI_BEST_OF_N
    best = None  # (penalty, clip_path)
    for attempt in range(n):
        seg_out: dict[str, str] = {}
        clip = _build_sentence_clip_once(
            tts, sentence_text, ref_audio, ref_text, adaptive_speed, cfg, td,
            f"{tag}a{attempt}", head_protect, seg_out=seg_out,
        )
        if clip is None:
            continue
        # Inline VN phrases (cho phép / tùy chỉnh) transcribe reliably → verify on the whole
        # clip by whisper token match + pace/gap.
        inline_ok, inline_pen, inline_res = _vi_verify_clip(clip, inline_corrs)
        res_by_term = {c["term"]: r for c, r in zip(inline_corrs, inline_res)}
        # Isolatable terms (agent = 'ây jừn') render as their own F5 chunk. The hyphen
        # respell already makes the intra-word split ~0, so this is a light safety net that
        # re-rolls the rare split/drag — verified on the term's OWN freshly-synth'd segment.
        iso_ok = True
        iso_pen = 0.0
        for c in iso_corrs:
            seg_wav = seg_out.get(c["seg_fold"])
            if not seg_wav:  # segment not produced this draw (shouldn't happen) → skip
                continue
            r = _vi_verify_isolatable_segment(seg_wav, c)
            res_by_term[c["term"]] = r
            iso_ok = iso_ok and r["ok"]
            iso_pen += r["penalty"]
        all_ok = inline_ok and iso_ok
        penalty = inline_pen + iso_pen
        for c in corrs:
            r = res_by_term.get(c["term"])
            if r:
                log.info("vi_corrections %s attempt %d/%d: %s", tag, attempt + 1, n, r["detail"])
        if best is None or penalty < best[0]:
            best = (penalty, clip)
        if all_ok:
            break  # good draw — stop rolling
    if best is None:
        return None
    penalty, clip = best
    if n > 1:
        log.info("vi_corrections %s: kept best FRESH draw (penalty=%.0f)", tag, penalty)
    return clip


def _build_sentence_clip_once(tts, sentence_text: str, ref_audio: str, ref_text: str,
                              adaptive_speed: float, cfg, td: str, tag: str,
                              head_protect: bool, seg_out: dict | None = None) -> str | None:
    """Synthesize ONE sentence/clause as a single clean clip (per-sentence path).

    Applies the pronunciation map + separator encoding to `sentence_text`, then splits
    it at speed markers so a say_as FAST/SLOW/PAUSE segment (pờ~rôm, ây-jừn, a year, a
    "--" pause) still gets its own atempo/pause handling — the overrides keep working.
    Each speech segment is a single F5 infer (short gen_text → no duration drift, no
    internal cross-fade seam); segments are concatenated tight (their own edges are
    trimmed) into ONE sentence clip. The clip's LEADING/TRAILING SILENCE is then trimmed
    (energy-based, silero-vad absent) and a short onset fade-in kills F5 ref-tail bleed.
    Returns the clip wav path (24 kHz) or None if the sentence produced no audio.

    head_protect: when True this is the FIRST clip of the scene; a period-terminated
    filler is prepended to absorb F5's unstable ref→gen onset seam, then trimmed back off
    by whisper (same mechanism as the legacy path) — kept because the onset artifact is
    confined to the very start of the scene."""
    import numpy as _np
    import soundfile as _sf

    spoken = _apply_pron_map(sentence_text, engine="f5")
    speed_segments = _split_by_speed(spoken)

    # Build (seg_text, atempo) speech units + pause markers, in order.
    units: list[tuple[str, float]] = []
    for seg_text, seg_speed in speed_segments:
        if seg_speed == _PAUSE_FACTOR:
            units.append(("", _PAUSE_FACTOR))
            continue
        t = seg_text.replace('\xa0', ' ').strip()
        if t:
            units.append((t, seg_speed))

    part_paths: list[str] = []
    per_gap: list[float] = []
    first_seg = True
    for si, (seg_text, seg_speed) in enumerate(units):
        if seg_speed == _PAUSE_FACTOR:
            # a "--" pause between two speech segments of THIS sentence
            if part_paths:
                per_gap.append(_F5_PAUSE_BEAT_S)
            continue
        head = ((F5_LEADIN + (F5_HEAD_FILLER + " " if (head_protect and first_seg) else ""))
                if first_seg else "")
        # A FAST/SLOW say_as segment (e.g. 'ây jừn' for agent) is its own standalone chunk;
        # the fold of its text lets the caller verify THIS occurrence on its own segment wav
        # (via seg_out) — it is always freshly inferred (no cache / no splice), so the term
        # varies naturally per occurrence.
        seg_fold = _vi_fold(seg_text) if seg_speed != 1.0 else None
        gen_text = (head + seg_text).lower().replace('\xa0', ' ')
        raw = os.path.join(td, f"ps_{tag}_{si:02d}_raw.wav")
        tts.infer(
            ref_file=ref_audio, ref_text=ref_text, gen_text=gen_text, file_wave=raw,
            # nfe_step default raised 16 -> 32 (F5-TTS reference default). More ODE
            # denoising steps = more stable draws (fewer rushed function words / vowel
            # drags, clearer consonants). Global QUALITY knob, NOT a per-region pace
            # change, so the constant-pace design is untouched. ~2x slower per infer.
            nfe_step=int(cfg.get("nfeStep", 32)),
            cfg_strength=float(cfg.get("cfgStrength", 2.0)),
            speed=adaptive_speed,
            sway_sampling_coef=float(cfg.get("swaySamplingCoef", -1.0)),
            target_rms=float(cfg.get("targetRms", 0.1)),
            cross_fade_duration=float(cfg.get("crossFadeDuration", os.getenv("F5_XFADE_S", "0.06"))),
            remove_silence=False,
        )
        if not os.path.isfile(raw):
            continue
        # Trim head-protect filler off the FIRST segment (whisper word-timestamps).
        if head_protect and first_seg:
            _trim_leadin_by_whisper(raw, F5_HEAD_FILLER)
        first_seg = False
        cur = raw
        # atempo for FAST/SLOW say_as segments (year / hyphen / tilde), 24 kHz.
        if seg_speed != 1.0:
            tempo = os.path.join(td, f"ps_{tag}_{si:02d}_tempo.wav")
            _apply_atempo(cur, tempo, seg_speed, out_sr=F5_SOURCE_SR)
            _w, _sr = _sf.read(tempo, dtype="float32", always_2d=False)
            if seg_speed > 1.0:
                _w = _np.asarray(_trim_fast_chunk_edges(_w, _sr), dtype=_np.float32).reshape(-1)
            else:
                _w = _np.asarray(_trim_silence_np(_w, _sr, pad_s=0.12), dtype=_np.float32).reshape(-1)
                _w = _np.asarray(_tighten_slow_join_gap(_w, _sr), dtype=_np.float32).reshape(-1)
            _sf.write(tempo, _w, _sr, subtype="PCM_16")
            cur = tempo
            # Expose the freshly-synth'd, fully-processed segment wav so the caller can
            # verify THIS occurrence's isolated chunk (gap/duration). Not cached — used
            # only to score the current draw for best-of-N.
            if seg_out is not None and seg_fold:
                seg_out[seg_fold] = cur
        part_paths.append(cur)
        if len(part_paths) > 1:
            # intra-sentence gap between two speech segments (a say_as boundary). Keep
            # tight — a speed transition blends at ~5 ms, else a small ~5 ms seam.
            per_gap.append(float(os.getenv("TTS_SPEED_GAP_S", "0.005")))

    if not part_paths:
        return None

    # Concat this sentence's segments (tight) into ONE 24 kHz clip.
    clip = os.path.join(td, f"ps_{tag}_clip.wav")
    _concat_wavs_48k(part_paths, clip, gap_s=per_gap if per_gap else 0.005)

    # Light per-clip cleanup: trim leading/trailing SILENCE (energy-based, never a
    # consonant because we only cut the silence region), then a short onset fade-in to
    # kill F5 ref-tail bleed at sentence start.
    _d, _sr = _sf.read(clip, dtype="float32", always_2d=False)
    if _d.ndim > 1:
        _d = _d.reshape(_d.shape[0], -1).mean(axis=1)
    _d = _np.asarray(_d, dtype=_np.float32).reshape(-1)
    _d = _np.asarray(_trim_silence_np(_d, _sr, thresh_db=CF_PS_TRIM_DB, pad_s=CF_PS_TRIM_PAD_S),
                     dtype=_np.float32).reshape(-1)
    # LEVER 1: compress-ONLY intra-clip silence. AFTER the edge trim (so leading/trailing
    # silence is out of the way and only genuine INTERNAL runs remain), shorten any low-
    # energy run LONGER than the natural ceiling down to CF_PS_INTRA_KEEP_S. Energy-based
    # (RMS), conservative -52 dB threshold, ~45 ms edge guard, MIDDLE-only removal — this
    # kills F5's over-long mid-phrase beats ("định nghĩa | nó") WITHOUT the old whisper
    # tail-clip. protect_max_s == cap_s so every run above the ceiling qualifies (a glide
    # dip inside a syllable body sits ABOVE the -52 dB threshold, so it is not even seen as
    # silence — the threshold, not a length floor, protects the tails here). bridge coalesces
    # a breath-broken beat into one run.
    if CF_PS_INTRA_COMPRESS and CF_PS_MAX_INTRA_GAP_S > CF_PS_INTRA_KEEP_S:
        _d = _np.asarray(
            _compress_internal_silence_np(
                _d, _sr,
                cap_s=CF_PS_MAX_INTRA_GAP_S,
                keep_s=CF_PS_INTRA_KEEP_S,
                thresh_db=CF_PS_INTRA_THRESH_DB,
                edge_guard_s=CF_PS_INTRA_EDGE_GUARD_S,
                bridge_s=CF_TTS_SIL_BRIDGE_S,
                protect_max_s=CF_PS_MAX_INTRA_GAP_S,
            ),
            dtype=_np.float32,
        ).reshape(-1)
    fn = min(int(CF_PS_HEAD_FADE_S * _sr), _d.size // 4)
    if fn > 0:
        _d = _d.copy()
        _d[:fn] *= _np.linspace(0.0, 1.0, fn, dtype=_np.float32)
    _sf.write(clip, _d, _sr, subtype="PCM_16")
    # Trim a spurious trailing F5 end-hallucination ("ờ"/"ổn") off THIS clip. Only fires
    # on a clear whisper-vs-narration word-count overproduction with a short trailing blip
    # after a gap (see _trim_trailing_halluc), so a clean clip is never touched. Runs on the
    # 24 kHz clip so the fragment is gone BEFORE concat/resample (and before the caption
    # whisper), keeping captions in sync. Fail-safe: any error leaves the clip unchanged.
    if CF_PS_TRAIL_HALLUC:
        try:
            _trim_trailing_halluc(clip, sentence_text)
        except Exception as _e:
            log.warning("trail-halluc trim failed (%s); leaving clip unchanged", _e)
    return clip


def _synth_scene_per_sentence(tts, text: str, ref_audio: str, ref_text: str,
                              adaptive_speed: float, cfg, out_path: str,
                              corr_ctx: dict | None = None) -> bool:
    """Per-sentence F5 synthesis for one scene (CF_TTS_PER_SENTENCE path).

    Splits `text` into sentences/clauses, synthesizes each as one clean clip via
    _synth_sentence_clip, then concatenates the clips with FIXED, CONSISTENT silence
    padding (CF_PS_GAP_MID_S mid-sentence, CF_PS_GAP_PARA_S at a sentence-final period),
    resamples the concatenated 24 kHz scene to canonical 48 kHz, and writes out_path.
    Whisper is NOT used to cut narration audio anywhere in this path (only the optional
    head-filler trim on the first clip, which trims the throwaway filler, not narration).

    Returns True on success (out_path written). Returns False if there is nothing to say
    (caller writes a silent placeholder)."""
    import tempfile
    units = _split_sentences_for_ps(text)
    if not units:
        return False

    with tempfile.TemporaryDirectory() as td:
        clips: list[str] = []
        gaps: list[float] = []  # one gap per clip boundary (len == len(clips)-1)
        # Head-protect: the "aagent" doubled onset was CONFIRMED (probe: scene 1 re-synth
        # with F5_HEAD_PROTECT=0 → clean single "ây jừn" onset, no doubled onset / no 0.06s
        # "người" clip) to be the filler-trim SEAM, not a chunk-onset artifact. In the
        # per-sentence path the onset artifact is confined to the sentence START (pre-speech),
        # so the small onset fade-in (CF_PS_HEAD_FADE_S) + leading-silence trim replace the
        # filler hack — we do NOT prepend the "này này." filler here. Env escape hatch:
        # CF_PS_HEAD_FILLER=1 restores the legacy filler for the first clip if ever needed.
        _ps_use_filler = os.getenv("CF_PS_HEAD_FILLER", "0").strip().lower() not in ("0", "off", "false", "no")
        first = True
        for ui, (unit_text, ends_sentence) in enumerate(units):
            clip = _synth_sentence_clip(
                tts, unit_text, ref_audio, ref_text, adaptive_speed, cfg, td,
                tag=f"u{ui:02d}", head_protect=(first and _ps_use_filler),
                corr_ctx=corr_ctx,
            )
            first = False
            if clip is None:
                continue
            if clips:
                gaps.append(CF_PS_GAP_PARA_S if ends_sentence else CF_PS_GAP_MID_S)
            clips.append(clip)
        if not clips:
            return False
        scene_24k = os.path.join(td, "scene_ps_24k.wav")
        _concat_wavs_48k(clips, scene_24k, gap_s=gaps if gaps else CF_PS_GAP_PARA_S)
        _resample_to_canonical(scene_24k, out_path)
    # DURABLE non-punct gap removal on the FINAL scene wav: butt-join bound word pairs
    # ("kết|nối", "định nghĩa|nó", "năm|2026") to a micro-seam, keep the (item-1-lowered)
    # beat at real punctuation. Runs LAST so it operates on the same 48 kHz audio the
    # assembler's caption aligner reads → caption timing derives from the shaped audio.
    #
    # BACKBONE (item 2/4/6 durable fix): prefer CTC word boundaries — they compress the
    # VOICED glide-tail + breath region between bound words that the energy-only pass
    # structurally left behind (that residual is why "tùy|chỉnh"/"năm|year" still beat).
    # PER-SCENE FAIL-SAFE: if CTC is unusable/implausible for this scene, fall back to the
    # legacy energy path. Log which path each scene used.
    if CF_PS_NONPUNCT_SHAPE:
        used = "none"
        try:
            if CF_ALIGN_BACKEND == "ctc":
                r = _shape_nonpunct_gaps_by_ctc(out_path, text)
                if r is None:  # CTC unusable for this scene → energy fallback
                    _shape_nonpunct_gaps_by_energy(out_path, text)
                    used = "energy(fallback)"
                else:
                    used = "ctc"
            else:
                _shape_nonpunct_gaps_by_energy(out_path, text)
                used = "energy"
        except Exception as _e:  # never fail the scene on a shaping error
            log.warning("nonpunct-gap shaping failed for scene wav (%s); leaving unchanged", _e)
            used = "error"
        log.info("nonpunct-gap shaping: %s used %s backend", os.path.basename(out_path), used)
    return True


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
    caller_speed = cfg.get("speed") is not None
    # LEVER 2: reference-rate pace normalization. Replace the BASE speed with
    # R_target / R_ref so output pace is independent of the ref's own speaking rate.
    # R_ref = ref_text UTF-8 bytes / prepped-ref seconds (the exact terms of F5's
    # duration formula). Calibrated so the current good voice (Escbase, R_ref≈18.72)
    # yields speed≈1.0 → today's voiced pace is unchanged. An explicit caller speed
    # wins. Clamped to [CF_TTS_SPEED_MIN, CF_TTS_SPEED_MAX]; out-of-band → warn (ref
    # too fast/slow to normalize inside the safe band; would need ref re-timing).
    if CF_TTS_REF_RATE_NORM and not caller_speed and safe_dur > 0 and ref_text:
        r_ref = len(ref_text.encode("utf-8")) / safe_dur
        if r_ref > 0:
            want = CF_TTS_TARGET_BYTE_RATE / r_ref
            clamped = max(CF_TTS_SPEED_MIN, min(CF_TTS_SPEED_MAX, want))
            if abs(clamped - want) > 1e-6:
                log.warning(
                    "F5 REF-RATE NORM: ref %s is too %s to normalize inside the safe "
                    "band — R_ref=%.2f B/s, R_target=%.2f, ideal speed=%.3f clamped to "
                    "%.3f (would need ref re-timing; out of scope)",
                    os.path.basename(cfg.get("refAudio") or ref_audio),
                    "fast" if want < clamped else "slow",
                    r_ref, CF_TTS_TARGET_BYTE_RATE, want, clamped,
                )
            else:
                log.info(
                    "F5 REF-RATE NORM: R_ref=%.2f B/s R_target=%.2f -> speed=%.3f "
                    "(ref_text=%d bytes over %.2fs)",
                    r_ref, CF_TTS_TARGET_BYTE_RATE, clamped,
                    len(ref_text.encode("utf-8")), safe_dur,
                )
            adaptive_speed = clamped
    # Legacy low-density guard — a FALLBACK only when ref-rate normalization is OFF.
    # LEVER 2 subsumes it (it normalizes ALL refs, not just low-density ones, and by
    # byte-rate rather than a char-density heuristic), so when CF_TTS_REF_RATE_NORM is
    # on we do NOT let this overwrite the principled normalized speed.
    if not CF_TTS_REF_RATE_NORM and not caller_speed and safe_dur > 0 and ref_text:
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
    # GLOBAL slowdown: apply the uniform overall-speed multiplier on top of the density
    # compensation, UNLESS the caller passed an explicit speed (manual override wins). This
    # is what reduces the whole video's reading pace by a fixed amount (owner: prev − 10%).
    # F5 speed<1.0 = slower. Only applied on the default path so a manual speed test is exact.
    if not caller_speed and F5_SPEED_SCALE < 1.0:
        before = adaptive_speed
        adaptive_speed = adaptive_speed * F5_SPEED_SCALE
        log.info("F5 global slowdown: speed %.3f -> %.3f (F5_SPEED_SCALE=%.3f)",
                 before, adaptive_speed, F5_SPEED_SCALE)

    ref_audio = safe_ref  # F5 inference uses the capped clip from here on

    progress(0, "Nạp model F5-TTS")
    from f5_tts.api import F5TTS

    tts = F5TTS(model=F5_MODEL, ckpt_file=F5_CKPT, vocab_file=F5_VOCAB)

    # Vietnamese correction memory context (per-run). Voice-INDEPENDENT: no per-voice data
    # is stored or read — the best-of-N + verify net covers ANY clone voice with no warm-up.
    # `enabled` is the only field the correction layer consults.
    corr_ctx = {
        "enabled": bool(CF_VI_CORRECTIONS) and bool(_vi_load_corrections()),
    }
    if corr_ctx["enabled"]:
        log.info("vi_corrections: ON (best-of-N=%d, no cache — fresh synth per occurrence)",
                 CF_VI_BEST_OF_N)

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
        name = f"scene_{scene:03d}.wav" if isinstance(scene, int) else "audio.wav"
        path = out_dir / name
        out_dir.mkdir(parents=True, exist_ok=True)  # shared page audio dir may be removed mid-run by a sibling job's cleanup

        # -------- Per-sentence architecture (CF_TTS_PER_SENTENCE) --------
        # Synthesize one clip per sentence/clause, concat with FIXED silence padding,
        # NO whisper audio-cutting. When on, the whole legacy chunk/gap-shape block below
        # is skipped for this scene. Pronunciation overrides still apply (inside
        # _synth_sentence_clip).
        #
        # FAILURE BEHAVIOUR — corrected 2026-07-28. This comment used to claim "if it
        # raises, fall through to the legacy path". It does NOT: on failure the `if not ok`
        # branch below writes a 0.2 s SILENT placeholder and `continue`s (a deliberate
        # choice, see the note there — falling back mid-run would muddy an A/B). Net effect
        # to be aware of: a scene whose per-sentence synth fails ships as 0.2 s of SILENCE
        # and the job still completes as `done`. The warning logged just below is the only
        # signal, so check for it when a finished video has a mute scene.
        if CF_TTS_PER_SENTENCE:
            ok = False
            try:
                ok = _synth_scene_per_sentence(tts, text, ref_audio, ref_text,
                                               adaptive_speed, cfg, str(path),
                                               corr_ctx=corr_ctx)
            except Exception as _e:
                log.warning("per-sentence synth scene %s failed (%s); falling back to legacy", scene, _e)
                ok = False
            if not ok:
                # Nothing to say (or failed) → emit a short silent placeholder so the
                # scene still has a VO file. (A hard failure logs above; we do not
                # silently switch to the legacy chunker mid-run to keep the A/B clean.)
                import numpy as np
                import soundfile as sf
                sf.write(str(path), np.zeros(int(0.2 * CANONICAL_SR), dtype=np.float32),
                         CANONICAL_SR, subtype="PCM_16")
            else:
                log.info("per-sentence: scene %s synthesized (gap mid=%.2fs para=%.2fs)",
                         scene, CF_PS_GAP_MID_S, CF_PS_GAP_PARA_S)
            duration_s = _probe_duration(str(path))
            results.append({
                "scene": scene, "text": text, "audioPath": str(path),
                "sampleRate": CANONICAL_SR, "durationS": duration_s,
            })
            progress(round((i + 1) / max(1, total) * 100), f"Lồng tiếng {i + 1}/{total}")
            continue
        # -------- Legacy chunk + whisper gap-shape architecture --------
        # REACHABILITY (audited 2026-07-28): everything from here to the end of _run_f5 runs
        # ONLY when CF_TTS_PER_SENTENCE=0. Dashboard/api/.env currently ships CF_TTS_PER_SENTENCE=1
        # and every branch of the per-sentence block above ends in `continue`, so as configured
        # today this whole tail is NOT executed — and neither are the constants it owns:
        # CF_GAP_* (gap shaping) and CF_TTS_SIL_* (legacy silence compressor).
        # KEPT ON PURPOSE, not dead code: it is the other half of an A/B the owner toggles via
        # that env flag. Do not delete it without also removing the flag. Corollary: the
        # `[gap-shape]` stderr markers never appear while the flag is 1.
        spoken = _apply_pron_map(text, engine="f5")
        # Split the scene narration into chunks BELOW F5's drop threshold and synth
        # each separately, then concat — a single infer call on a long scene drops /
        # garbles words past ~220 chars (issue 3). Splitting is sentence-first via the
        # shared _split_for_tts (F5 budget), so an ordinary short scene yields exactly
        # ONE chunk and behaves identically to the old single-infer path. The leadin
        # (empty by default) is prepended ONCE to the first chunk, not per chunk.
        #
        # FIRST split the spoken text at speed/pause markers (\x01-\x05): a single-hyphen
        # say_as / a year carries FAST (F5_FAST_FACTOR), a slow term carries SLOW
        # (F5_SLOW_FACTOR), and a "--" separator becomes a PAUSE beat segment. Each
        # SPEECH segment is then chunked independently; a PAUSE segment is carried as a
        # ("", _PAUSE_FACTOR) placeholder (no inference, just a pause_beat gap at concat).
        # We flatten into (chunk_text, ends_sentence, atempo) triples so a single index
        # space covers atempo, pauses, and per-boundary gaps. Source: word_improve.md.
        speed_segments = _split_by_speed(spoken)
        all_chunks: list[tuple[str, bool, float]] = []
        for seg_text, seg_speed in speed_segments:
            if seg_speed == _PAUSE_FACTOR:
                all_chunks.append(("", False, _PAUSE_FACTOR))  # pause placeholder
                continue
            seg_chunks = _split_for_tts(seg_text.replace('\xa0', ' '), max_chars=_F5_MAX_CHUNK_CHARS)
            for chunk_text, ends_sent in seg_chunks:
                all_chunks.append((chunk_text, ends_sent, seg_speed))
        # Merge any tiny chunk up to a length floor so F5 never over-generates and
        # repeats a short gen_text (e.g. a short comma-list spoken twice). See
        # _consolidate_f5_short / _F5_MIN_CHUNK_CHARS. Pause placeholders carry the
        # unique _PAUSE_FACTOR speed so consolidation never merges across/into them.
        all_chunks = _consolidate_f5_short(all_chunks)

        name = f"scene_{scene:03d}.wav" if isinstance(scene, int) else "audio.wav"
        path = out_dir / name
        out_dir.mkdir(parents=True, exist_ok=True)  # shared page audio dir may be removed mid-run by a sibling job's cleanup

        if not all_chunks:
            # Empty / whitespace-only narration: nothing to say. Emit a short silent
            # WAV so the scene still has a (tiny) VO file and downstream does not break.
            import numpy as np
            import soundfile as sf
            sf.write(str(path), np.zeros(int(0.2 * CANONICAL_SR), dtype=np.float32),
                     CANONICAL_SR, subtype="PCM_16")
        else:
            with tempfile.TemporaryDirectory() as td:
                # Per-chunk non-GPU post-processing (chính tail-fade + atempo) is
                # pipelined onto a small thread pool so it overlaps the NEXT chunk's GPU
                # infer instead of blocking it. The GPU infer loop itself stays on the
                # main thread (F5 is not thread-safe and the 8 GB GPU runs one infer at
                # a time); only the ffmpeg/numpy post-work moves off-thread. All work is
                # done at the 24 kHz source rate — the single 24→48 kHz resample happens
                # ONCE per scene below, on the concatenated WAV.
                from concurrent.futures import ThreadPoolExecutor

                def _post_process_chunk(raw_path: str, out_path: str, gen_text: str,
                                        chunk_atempo: float) -> tuple[str, bool]:
                    # Tail fade-out for chunks ending with "chính": F5-ViVoice adds a
                    # trailing -í vowel artifact after the palatal-nasal /ŋ/ in "chính".
                    # Fade the last 80 ms down to 0.05 (−26 dB), capped at one third of
                    # the chunk so a very short chunk is never over-faded.
                    chinh_end = bool(_CHINH_END_RE.search(gen_text))
                    cur = raw_path
                    if chinh_end:
                        import numpy as _np
                        import soundfile as _sf
                        _d, _sr = _sf.read(cur, dtype="float32", always_2d=False)
                        _fade_n = min(int(0.08 * _sr), len(_d) // 3)
                        if _fade_n > 0:
                            _d[-_fade_n:] *= _np.linspace(1.0, 0.05, _fade_n, dtype=_np.float32)
                            _sf.write(cur, _d, _sr, subtype="PCM_16")
                    # Apply atempo for FAST/SLOW segments, at the 24 kHz source rate.
                    # After time-stretch, trim leading/trailing gaps so the sped-up chunk
                    # sits flush against its neighbours the same way a normal chunk does.
                    if chunk_atempo != 1.0:
                        _apply_atempo(cur, out_path, chunk_atempo, out_sr=F5_SOURCE_SR)
                        import numpy as _np
                        import soundfile as _sf
                        _w, _sr = _sf.read(out_path, dtype="float32", always_2d=False)
                        # FAST (year / 1-hyphen join) chunk: asymmetric edge-trim. F5
                        # prepends a long breath-level LEAD-IN (~0.5 s before a year's
                        # first digit) that is the audible BREAK heard before a number
                        # ("...vào đầu [break] 2026"). It is the FAST chunk's OWN leading
                        # silence (a separate infer), not a prosody pause inside another
                        # inference, so the whole-scene compressor cannot reach it; and it
                        # sits ABOVE the gentle -50 dB trailing threshold so the old
                        # symmetric trim left it intact. _trim_fast_chunk_edges trims the
                        # LEADING edge aggressively (everything before the year/term onset
                        # is padding) while keeping the trailing edge gentle so a soft
                        # word-final tail is never clipped — seating the year flush. SLOW
                        # (prompt) chunks keep the symmetric gentle trim (pad 0.12).
                        if chunk_atempo > 1.0:
                            _w = _np.asarray(_trim_fast_chunk_edges(_w, _sr),
                                             dtype=_np.float32).reshape(-1)
                        else:
                            _w = _np.asarray(_trim_silence_np(_w, _sr, pad_s=0.12),
                                             dtype=_np.float32).reshape(-1)
                            # SLOW-join (tilde, e.g. "pờ~rôm") chunk: keep the syllables at
                            # the slowed pace but TIGHTEN the junction between them ~20%, so
                            # the two syllables flow as one word instead of a dragged
                            # "pờ … rôm". Compresses ONLY the internal silence run (the
                            # chunk is a standalone inference, so that run IS the syllable
                            # junction); speech samples are untouched. Owner 2026-07-04.
                            _w = _np.asarray(_tighten_slow_join_gap(_w, _sr),
                                             dtype=_np.float32).reshape(-1)
                        _sf.write(out_path, _w, _sr, subtype="PCM_16")
                        cur = out_path
                    return cur, chinh_end

                executor = ThreadPoolExecutor(max_workers=2)
                # Futures are kept in submission order so part metadata (speeds,
                # pause-after) stays aligned to chunk order when results are collected.
                pp_futures: list = []
                part_speeds: list[float] = []
                # Whether a PAUSE placeholder immediately FOLLOWS each produced part
                # (→ insert a pause beat after it at concat). Parallels the futures list.
                part_pause_after: list[bool] = []
                first_infer = True  # head-protect attaches to the first REAL chunk
                try:
                    for ci, (chunk_text, _ends_sentence, chunk_atempo) in enumerate(all_chunks):
                        # PAUSE placeholder: no inference. Flag the PRECEDING part to
                        # carry a pause beat after it. (A leading/trailing pause with no
                        # neighbour is simply dropped.)
                        if chunk_atempo == _PAUSE_FACTOR:
                            if part_pause_after:
                                part_pause_after[-1] = True
                            continue
                        # Head-protect (bug 1): on the FIRST real chunk only, prepend a
                        # throwaway filler so F5's unstable ref→gen seam corrupts the
                        # FILLER, not the real first word; the filler audio is trimmed
                        # back off below using whisper word timestamps. F5_LEADIN
                        # (legacy, empty by default) stacks before the filler if set.
                        is_first_real = first_infer
                        use_head_filler = F5_HEAD_PROTECT and is_first_real
                        first_infer = False
                        head = ((F5_LEADIN + (F5_HEAD_FILLER + " " if use_head_filler else ""))
                                if is_first_real else "")
                        gen_text = (head + chunk_text).lower().replace('\xa0', ' ')
                        raw = os.path.join(td, f"f5_raw_{ci:03d}.wav")

                        def _infer_to(dst: str) -> None:
                            tts.infer(
                                ref_file=ref_audio,
                                ref_text=ref_text,
                                gen_text=gen_text,
                                file_wave=dst,
                                # nfe_step 16 -> 32 (F5 default; steadier draws). See the
                                # per-sentence infer site above for rationale.
                                nfe_step=int(cfg.get("nfeStep", 32)),
                                cfg_strength=float(cfg.get("cfgStrength", 2.0)),
                                speed=adaptive_speed,
                                sway_sampling_coef=float(cfg.get("swaySamplingCoef", -1.0)),
                                target_rms=float(cfg.get("targetRms", 0.1)),
                                # A6 ("từ bị kéo dài như giật cụt"): F5 batches gen_text that
                                # exceeds its internal max_chars into sub-segments and OVERLAP-
                                # ADDs them over cross_fade_duration. A long 0.15 s overlap is
                                # the documented source of the stretched/stuttered word at the
                                # internal seam (SWivid issues). Our chunks already bound length
                                # (_F5_MAX_CHUNK_CHARS) so F5 rarely needs to batch; when it
                                # does, a SHORTER cross-fade (0.06 s) makes the seam far less
                                # audible without a hard click. Env-overridable via F5_XFADE_S.
                                cross_fade_duration=float(cfg.get("crossFadeDuration",
                                                                  os.getenv("F5_XFADE_S", "0.06"))),
                                remove_silence=False,
                            )

                        _infer_to(raw)
                        if not os.path.isfile(raw):
                            raise RuntimeError(f"scene {scene}: F5 produced no audio (chunk {ci})")
                        # Trim the head-protect filler back off (bug 1) using whisper word
                        # timestamps — kept on the MAIN thread (right after infer, before
                        # hand-off), fail-safe: leaves audio intact if the filler is not
                        # found, so it never clips the real first word.
                        if use_head_filler:
                            _trim_leadin_by_whisper(raw, F5_HEAD_FILLER)

                        # Loanword rush repair (F5 only, cheaper variant): ONLY for a chunk
                        # that CONTAINS a loanword (pure-Vietnamese chunks are never measured
                        # → zero added cost). Whisper the chunk, measure the loanword(s); if
                        # rushed/clipped and retries remain, re-render ONCE (F5 draws a fresh
                        # random seed) and KEEP the better attempt (higher worst-ms/syll,
                        # fewer problems). Never speeds anything up — a re-roll only. Skips
                        # a FAST/SLOW atempo chunk (chunk_atempo != 1.0): those are years /
                        # mapped terms with their own timing path, not raw loanword reads.
                        if F5_LOANWORD_REPAIR and chunk_atempo == 1.0:
                            # Raw English loanwords still in the chunk (engineering, prompt,
                            # …) PLUS loanwords the pron-map RESPELLED away (ChatGPT →
                            # "chát ji pi ti") — the latter re-enables the re-roll for the
                            # dense spelled-acronym chunk where F5 drops an adjacent short
                            # word ("ra mắt"). Deduped, order preserved.
                            loans = _loanwords_in_chunk(chunk_text)
                            for lw in _mapped_loanwords_in_chunk(chunk_text):
                                if lw not in loans:
                                    loans.append(lw)
                            if loans:
                                best_path = raw
                                best_score, best_problems = _measure_loanwords(raw, loans)
                                attempts = 1
                                while best_problems and attempts <= F5_LOANWORD_MAX_RETRY:
                                    alt = os.path.join(td, f"f5_raw_{ci:03d}_r{attempts}.wav")
                                    _infer_to(alt)
                                    if os.path.isfile(alt):
                                        if use_head_filler:
                                            _trim_leadin_by_whisper(alt, F5_HEAD_FILLER)
                                        alt_score, alt_problems = _measure_loanwords(alt, loans)
                                        # Keep the better: fewer failing loanwords wins; tie →
                                        # higher worst-ms/syll (better articulation) wins.
                                        better = (len(alt_problems) < len(best_problems) or
                                                  (len(alt_problems) == len(best_problems)
                                                   and alt_score > best_score))
                                        if better:
                                            best_path, best_score, best_problems = alt, alt_score, alt_problems
                                    attempts += 1
                                if best_path != raw:
                                    import shutil as _shutil
                                    _shutil.copy2(best_path, raw)
                                log.info(
                                    "loanword-repair scene %s chunk %d %s: kept score=%.0f ms/syll, "
                                    "problems=%s, renders=%d",
                                    scene, ci, loans, best_score, best_problems, attempts,
                                )

                        tempo_wav = os.path.join(td, f"f5_tempo_{ci:03d}.wav")
                        # Submit non-GPU post-processing; it overlaps the next infer.
                        fut = executor.submit(_post_process_chunk, raw, tempo_wav,
                                              gen_text, chunk_atempo)
                        pp_futures.append(fut)
                        part_speeds.append(chunk_atempo)
                        part_pause_after.append(False)
                    # Collect post-processing results IN SUBMISSION ORDER.
                    part_paths: list[str] = []
                    chinh_flags: list[bool] = []
                    for fut in pp_futures:
                        p, chinh_end = fut.result()
                        part_paths.append(p)
                        chinh_flags.append(chinh_end)
                finally:
                    executor.shutdown(wait=True)
                # One part → straight copy (no added silence); many → joined with a
                # per-boundary gap. After a PAUSE placeholder → one beat (F5_PAUSE_BEAT_S);
                # after a faded "chính" chunk → ~15 ms; at a speed transition → ~5 ms so
                # the sped-up word blends (no "break at the year"); else the default
                # ~50 ms intra-chunk gap so split sentences do not run together. These
                # lists parallel part_paths (pauses produce no part), so indexing is safe.
                #
                # NOTE (v73 variance finding): a FAST-boundary neighbour-edge THRESHOLD trim
                # was tried to close the post-FAST-term seam ("Agent [seam] harness", "với
                # [seam] agent"). A 5-run study showed that seam is F5's STOCHASTIC inter-word
                # pause (~90-345 ms across identical inputs, -30 dB); threshold trimming only
                # nudged the median and never tamed the spread. The reliable fix (below) is a
                # DETERMINISTIC clamp of the COMBINED gap at the code-known FAST↔NORMAL concat
                # boundary: it targets a FIXED OUTPUT gap, so F5's variable padding is absorbed.
                per_gap: list[float] = []
                for idx in range(len(part_paths) - 1):
                    if part_pause_after[idx]:
                        per_gap.append(_F5_PAUSE_BEAT_S)
                    elif chinh_flags[idx]:
                        per_gap.append(float(os.getenv("TTS_CHINH_GAP_S", "0.015")))
                    elif part_speeds[idx] != part_speeds[idx + 1]:  # speed transition
                        per_gap.append(float(os.getenv("TTS_SPEED_GAP_S", "0.005")))
                    else:
                        per_gap.append(float(os.getenv("TTS_INTRA_GAP_S", "0.05")))

                # --- Deterministic FAST↔NORMAL boundary clamp (v73 seam fix, owner-approved) ---
                # At EVERY concat boundary where a FAST part is adjacent to a NORMAL part across
                # the speed transition, clamp the COMBINED low-energy span (left part trailing
                # silence + join gap + right part leading silence) to a FIXED target. We MEASURE
                # each side's silence and DELETE only the EXCESS from the MIDDLE of that span,
                # keeping ~edge_keep_ms of original samples on each side so NO onset/tail phoneme
                # is ever clipped, then set the join gap so the OUTPUT boundary silence == target.
                # Because the target is fixed and the boundary index is known in code, F5's
                # per-render padding variance is absorbed (it does NOT depend on a threshold
                # catching the right run). It fires ONLY at FAST↔NORMAL boundaries (never a
                # natural normal↔normal inter-word gap), removes ONLY low-energy silence (cannot
                # touch the digit-by-digit year content, the head filler, or split a word), and
                # is fail-safe (any error/short result leaves the part untouched). Env knob
                # F5_FAST_BOUNDARY_TARGET_MS (default 45; 0 = off).
                _clamp_target_ms = float(os.getenv("F5_FAST_BOUNDARY_TARGET_MS", "45"))
                if _clamp_target_ms > 0 and len(part_paths) > 1:
                    import numpy as _np
                    import soundfile as _sf

                    _edge_keep_ms = 20.0   # original samples kept at each side of the seam
                    # Silence threshold for the seam. F5 fills the inter-chunk gap with a
                    # low-level breath/exhale that can sit at ~-30..-38 dB (above a true
                    # -40 dB silence floor); a -30 dB threshold leaves a stochastic breath
                    # tail (measured: -30 dB seam still varied 80-235 ms while the true
                    # -38 dB silence was a tight 65-95 ms). -34 dB catches that breath while
                    # staying above the soft-consonant danger zone — and the clamp only
                    # deletes the MIDDLE of the run (keeps _edge_keep_ms each side) ONLY at a
                    # FAST↔NORMAL boundary, so a term/word phoneme is never clipped. Env knob.
                    _thr_db = float(os.getenv("F5_FAST_BOUNDARY_THR_DB", "-34.0"))

                    def _trail_sil_len(d, sr):
                        # samples of trailing low-energy (below _thr_db) at the END of d.
                        peak = float(_np.max(_np.abs(d))) or 1.0
                        loud = _np.where(_np.abs(d / peak) > 10.0 ** (_thr_db / 20.0))[0]
                        return 0 if loud.size == 0 else int(d.size - 1 - int(loud[-1]))

                    def _lead_sil_len(d, sr):
                        # samples of leading low-energy at the START of d.
                        peak = float(_np.max(_np.abs(d))) or 1.0
                        loud = _np.where(_np.abs(d / peak) > 10.0 ** (_thr_db / 20.0))[0]
                        return 0 if loud.size == 0 else int(loud[0])

                    for idx in range(len(part_paths) - 1):
                        # only FAST↔NORMAL transitions; skip pause/chính boundaries.
                        if part_pause_after[idx] or chinh_flags[idx]:
                            continue
                        sa, sb = part_speeds[idx], part_speeds[idx + 1]
                        is_fast_norm = (sa > 1.0 and sb <= 1.0) or (sa <= 1.0 and sb > 1.0)
                        if not is_fast_norm:
                            continue
                        try:
                            ld, lsr = _sf.read(part_paths[idx], dtype="float32", always_2d=False)
                            rd, rsr = _sf.read(part_paths[idx + 1], dtype="float32", always_2d=False)
                            if ld.ndim > 1:
                                ld = ld.reshape(ld.shape[0], -1).mean(axis=1)
                            if rd.ndim > 1:
                                rd = rd.reshape(rd.shape[0], -1).mean(axis=1)
                            ld = _np.asarray(ld, dtype=_np.float32).reshape(-1)
                            rd = _np.asarray(rd, dtype=_np.float32).reshape(-1)
                            sr = lsr
                            keep = int(_edge_keep_ms / 1000.0 * sr)
                            target = int(_clamp_target_ms / 1000.0 * sr)
                            cur_gap = int(per_gap[idx] * sr)
                            ltail = _trail_sil_len(ld, sr)
                            rlead = _lead_sil_len(rd, sr)
                            combined = ltail + cur_gap + rlead
                            if combined <= target:
                                continue  # already tight enough — leave as-is
                            # Keep up to `keep` of silence on each side; the join gap carries
                            # the remainder up to `target`. Trim the rest off the two parts.
                            keep_each = min(keep, target // 2)
                            new_ltail = min(ltail, keep_each)
                            new_rlead = min(rlead, keep_each)
                            new_gap = max(0, target - new_ltail - new_rlead)
                            # never trim into speech: only cut within the measured silence.
                            cut_l = ltail - new_ltail
                            cut_r = rlead - new_rlead
                            if cut_l > 0 and ld.size - cut_l > int(0.02 * sr):
                                ld = ld[: ld.size - cut_l]
                                _sf.write(part_paths[idx], ld.astype(_np.float32), sr, subtype="PCM_16")
                            if cut_r > 0 and rd.size - cut_r > int(0.02 * sr):
                                rd = rd[cut_r:]
                                _sf.write(part_paths[idx + 1], rd.astype(_np.float32), sr, subtype="PCM_16")
                            per_gap[idx] = new_gap / float(sr)
                        except Exception:
                            pass  # fail-safe: leave this boundary untouched
                if not part_paths:
                    # Degenerate scene (only pause placeholders, no real audio): emit the
                    # canonical-rate silent placeholder directly, skipping the resample.
                    import numpy as np
                    import soundfile as sf
                    sf.write(str(path), np.zeros(int(0.2 * CANONICAL_SR), dtype=np.float32),
                             CANONICAL_SR, subtype="PCM_16")
                else:
                    # Concat the 24 kHz parts (the gap-silence is generated at each part's
                    # own rate, so this is correct at 24 kHz), then do the SINGLE 24→48 kHz
                    # resample (+ edge silence-trim) once on the concatenated scene WAV.
                    scene_24k = os.path.join(td, "scene_24k.wav")
                    _concat_wavs_48k(
                        part_paths, scene_24k,
                        gap_s=per_gap if per_gap else float(os.getenv("TTS_INTRA_GAP_S", "0.05")),
                    )
                    _resample_to_canonical(scene_24k, str(path))
                    # Slow-term stretch (owner: "prompt" at ~50% speed). Targeted post-
                    # synthesis time-stretch of ONLY the slow-term word(s) on the FINAL
                    # scene wav — everything else stays at natural pace, and the caption
                    # whisper pass downstream re-measures this same wav so captions resync.
                    # Skipped entirely for a scene with no slow-term (no whisper cost).
                    if F5_SLOW_TERMS_ENABLE and F5_SLOW_TERMS and F5_SLOW_FACTOR < 1.0:
                        # Cheap pre-filter: only whisper the scene if a slow-term appears
                        # in the ORIGINAL narration (avoids a whisper pass on every scene).
                        low_text = (text or "").lower()
                        if any(re.search(r"(?<![^\W])" + re.escape(t) + r"(?![^\W])", low_text)
                               for t in F5_SLOW_TERMS):
                            try:
                                if _slow_scene_terms(str(path), F5_SLOW_TERMS, F5_SLOW_FACTOR):
                                    log.info("slow-term: scene %s slowed %s to %.2fx",
                                             scene, F5_SLOW_TERMS, F5_SLOW_FACTOR)
                            except Exception as _e:
                                log.warning("slow-term pass skipped scene %s (%s)", scene, _e)

                    # Acronym tighten (owner: acronyms read as ONE compact unit). Runs
                    # AFTER slow-term (independent regions — a slow-term is a word, an
                    # acronym is an uppercase run; they don't overlap). Pre-filtered on an
                    # uppercase run in the ORIGINAL narration so pure-Vietnamese scenes are
                    # never whispered. Compresses ONLY the acronym's audio span; caption
                    # keeps the original text. Never slows.
                    if F5_ACRONYM_TIGHTEN and F5_ACRONYM_FACTOR > 1.0 and _ACRONYM_RE.search(text or ""):
                        try:
                            if _tighten_scene_acronyms(str(path), text, F5_ACRONYM_FACTOR):
                                log.info("acronym-tighten: scene %s compressed acronym(s) at %.2fx",
                                         scene, F5_ACRONYM_FACTOR)
                        except Exception as _e:
                            log.warning("acronym-tighten pass skipped scene %s (%s)", scene, _e)

        # Post-pass on the FINAL scene WAV: (1) PUNCTUATION-AWARE gap shaping (the primary
        # rhythm control — replaces the old energy-only compressor), (2) a universal short
        # edge fade-in/out to de-click the scene onset/offset (the "bụp" pop).
        #
        # (1) _shape_gaps_by_alignment whispers the scene, aligns whisper words to the
        # narration tokens (which carry punctuation), and sets EACH inter-word gap to a
        # one-beat pause at a comma/period/semicolon and a small even gap mid-clause, never
        # touching word interiors (glide dips are structurally protected). This satisfies the
        # owner's spec — one beat at punctuation, smooth flow elsewhere — which the punctuation-
        # BLIND compressor could not (it squashed real beats AND left mid-clause breaks). It
        # only ever edits SILENCE between words (no speech time-stretch → narration-speed rule
        # honoured). Gated by CF_GAP_SHAPE (default on); if disabled OR whisper is unavailable
        # we FALL BACK to the legacy energy+duration compressor so behaviour never regresses to
        # "no shaping at all". The fade is a catch-all so the file never starts/ends with an
        # abrupt amplitude step. Fail-safe: any error skips the pass.
        if all_chunks:
            try:
                import numpy as _np
                import soundfile as _sf
                changed = False
                shaped = False
                if CF_GAP_SHAPE:
                    try:
                        shaped = _shape_gaps_by_alignment(str(path), text or "", scene=scene)
                        if shaped:
                            log.info("gap-shape: scene %s punctuation-aware gaps applied", scene)
                    except Exception as _e:
                        log.warning("gap-shape scene %s failed (%s); falling back to compressor", scene, _e)
                _d, _sr = _sf.read(str(path), dtype="float32", always_2d=False)
                if _d.ndim > 1:
                    _d = _d.reshape(_d.shape[0], -1).mean(axis=1)
                _d = _np.asarray(_d, dtype=_np.float32).reshape(-1)
                # Legacy energy+duration compressor: only as a FALLBACK when gap-shaping did
                # not run (disabled or whisper unavailable), so a scene is never left with raw
                # F5 pauses. When gap-shaping succeeded it already set every gap, so skip this.
                if not shaped and CF_TTS_SIL_CAP_S > 0:
                    _c = _np.asarray(_compress_internal_silence_np(_d, _sr), dtype=_np.float32).reshape(-1)
                    if _c.size and _c.size < _d.size:
                        _d = _c
                        changed = True
                # De-click edge fades (env-tunable; CF_TTS_EDGE_FADE_S=0 disables).
                fade_s = float(os.getenv("CF_TTS_EDGE_FADE_S", "0.005"))
                fn = min(int(fade_s * _sr), _d.size // 4)
                if fn > 0:
                    _d = _d.copy()
                    _d[:fn] *= _np.linspace(0.0, 1.0, fn, dtype=_np.float32)
                    _d[-fn:] *= _np.linspace(1.0, 0.0, fn, dtype=_np.float32)
                    changed = True
                if changed:
                    _sf.write(str(path), _d, _sr, subtype="PCM_16")
            except Exception as _e:
                log.warning("scene %s: post-pass (compress/de-click) skipped (%s)", scene, _e)

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

    engine = (cfg.get("engine") or "f5").strip().lower()
    items = cfg["items"]

    if engine in ("f5", "f5-tts", "f5tts"):
        results = _run_f5(cfg, items, out_dir, progress)
    elif engine in ("vieneu",):
        results = _run_vieneu(cfg, items, out_dir, progress)
    elif engine in ("omnivoice", "omni-voice", "omni"):
        # OmniVoice lives in its OWN module — no OmniVoice code path runs through the
        # F5/VieNeu functions in this file. Imported lazily so an F5 or VieNeu job never
        # even loads it (and can never be affected by anything defined there).
        from omnivoice_worker import run_omnivoice
        results = run_omnivoice(cfg, items, out_dir, progress)
    else:
        raise RuntimeError(
            f"TTS engine '{engine}' is not implemented (supported: f5-tts, vieneu, omnivoice)."
        )

    Path(out_path).write_text(
        json.dumps({"count": len(results), "results": results}, ensure_ascii=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    # This worker is always executed as a SCRIPT, so its module name is "__main__".
    # omnivoice_worker.py imports shared primitives from "tts_worker"; alias the running
    # module under that name BEFORE dispatch so that import resolves to THIS already
    # loaded module instead of re-executing this file (which would re-parse
    # word_improve.md, rebuild the regexes and duplicate every startup log line).
    sys.modules.setdefault("tts_worker", sys.modules["__main__"])
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
