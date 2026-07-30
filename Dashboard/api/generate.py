"""Generation endpoints — the host-side service that n8n calls over HTTP.

These wrap the local tools that n8n (in Docker) cannot run directly. Stage 1 is
the script writer: Claude Code headless (the subscription, NOT the paid API)
turns a topic into a Vietnamese narration script split into scenes.

Claude Code is invoked headless and streamed: `claude -p <prompt> --model sonnet
--max-turns 1 --tools "" --strict-mcp-config --system-prompt <terse> --output-format
stream-json --verbose`; stdin is closed so it does not wait for piped input. We read
the newline-delimited event stream and reassemble the final `{"type":"result"}`
event's `result` field, which we parse into the scene list. Large scene counts are
CHUNKED into several batches (SCRIPT_GEN_CHUNK_SCENES) so each call's decode stays
well under the per-batch timeout and a failed batch retries on its own.
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

import cache_util
import llm_gate
import tts_cache
from worker_errors import (
    friendly_worker_error,
    gpu_flake_message,
    worker_timeout_message,
)

router = APIRouter()

log = logging.getLogger("contentfactory.generate")

# Full path to claude.exe avoids the PowerShell/cmd shim and its stdin wait.
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")

# PER-BATCH wall-clock ceiling for one `claude -p` script-gen call, enforced on OUR
# side by reading the stream incrementally with a wall-clock deadline (the claude CLI
# has NO reliable --timeout flag for headless -p — verified against the installed
# CLI's --help). Script gen is now CHUNKED (see SCRIPT_GEN_CHUNK_SCENES): each call
# emits at most ~18 scenes ≈ 1.5-3k Vietnamese output tokens ≈ 30-90s decode + ~3s
# per-call overhead. 300s gives ~3-5x headroom over a healthy batch so a momentarily
# loaded run / cold bootstrap won't trip it, while still bounding a genuine stall.
# (Was 600s as a SINGLE all-or-nothing ceiling for the whole 71-scene job; chunking
# makes a much smaller per-batch timeout both safe and tighter.) Override in .env.
# Lowered 300 -> 200 (2026-06-28 perf pass): a genuinely stalled batch now retries in
# a fresh process ~100s sooner, and the per-mode chunk sizes were raised in the same
# pass so each batch decodes fewer scenes — keeping a healthy batch well under 200s.
SCRIPT_GEN_TIMEOUT = int(os.getenv("SCRIPT_GEN_TIMEOUT", "200"))

# Wall-clock ceiling for the ONE multi-turn VISION script-gen call used by the
# no-speech visual-explain fallback (generate_script_visual_explain). This is a
# SINGLE `claude -p` call that must OPEN ~14 frame images with the Read tool (each a
# tool-use round trip) before emitting the JSON, so the per-batch 200s script-gen
# budget does NOT apply — the image round-trips alone can take a few minutes. 600s
# gives comfortable headroom for 14 Read calls + the JSON decode; a genuine stall
# still trips it and the caller retries once in a fresh process. Override in .env.
VISUAL_EXPLAIN_TIMEOUT = int(os.getenv("VISUAL_EXPLAIN_TIMEOUT", "600"))
# Turn budget for the vision call: one Read round-trip per frame (~14) plus planning
# turns plus the final JSON emission. 40 leaves generous headroom so the JSON step is
# never starved of a turn (an exhausted budget surfaces as error_max_turns → the
# caller's 504-retry re-runs it in a fresh process).
VISUAL_EXPLAIN_MAX_TURNS = int(os.getenv("VISUAL_EXPLAIN_MAX_TURNS", "40"))

# Max scenes generated per `claude -p` batch. Output-token decode is the dominant
# latency term (diag: ~50 tok/s; a 71-scene one-shot pushed past the old 600s wall),
# so we split a large scene_count into contiguous batches of this many scenes — each
# a small, fast, individually-retryable call. ~4 calls for 71 scenes. Don't shard to
# 1 scene/call: per-call overhead (~3s) + cache churn would dominate. When the total
# scene_count <= this, we do ONE call (no chunking overhead — the degenerate case).
SCRIPT_GEN_CHUNK_SCENES = int(os.getenv("SCRIPT_GEN_CHUNK_SCENES", "23"))

# Per-edit-mode chunk size. Decode density is NOT uniform across modes: summary keeps
# 76-90% of the source near-verbatim (the densest Vietnamese decode in the system), so
# it needs the SMALLEST chunk. Lighter modes (commentary/educational) write original,
# non-verbatim text, so they tolerate the largest chunk; recap sits in between.
# Each mode is overridable via .env
# (SCRIPT_GEN_CHUNK_SCENES_<MODE>); an unset mode override falls back to the global
# SCRIPT_GEN_CHUNK_SCENES default. _DEFAULT_MODE_CHUNKS holds the starting values.
# Raised +25% (2026-06-28 perf pass) to cut the batch COUNT per job — fewer waves of
# `claude -p` calls — now that the per-batch timeout (200s) and concurrency absorb a
# slightly denser batch. (summary 15->19, recap 12->15, commentary 16->20,
# educational 16->20.)
# translate_full keeps 100% of the content near-verbatim (like summary, the densest
# decode), so it gets summary's small chunk (19) to keep each batch well under the timeout.
_DEFAULT_MODE_CHUNKS = {"summary": 19, "recap": 15, "commentary": 20, "educational": 20,
                        "translate_full": 19}
SCRIPT_GEN_CHUNK_BY_MODE = {
    mode: int(os.getenv(f"SCRIPT_GEN_CHUNK_SCENES_{mode.upper()}", str(default)))
    for mode, default in _DEFAULT_MODE_CHUNKS.items()
}

# Script gen pins Sonnet so an account whose DEFAULT model is Opus (3-5× slower —
# the realistic way a long prompt creeps toward the timeout) can't silently make
# every job slow. Override with SCRIPT_GEN_MODEL=opus if the owner wants it.
SCRIPT_GEN_MODEL = os.getenv("SCRIPT_GEN_MODEL", "sonnet")

# Turn budget for the headless `claude -p` call. FINAL STATE: 1.
# RECONCILIATION (6 -> 1): an earlier fix had raised this to 6 because, with the OLD
# invocation (full tool schema auto-loaded, no `--tools ""`), the larger transform/
# footage prompts could spend a non-final turn (a tool-use attempt / planning turn)
# before emitting the JSON and hard-fail with `error_max_turns` at --max-turns 1.
# The new invocation disables tools (`--tools ""`) and ships a terse JSON-only
# `--system-prompt`, so the call is a single mechanical JSON emission — the diagnostics
# measured num_turns=1 even with a budget of 6. With no tools to call there is no
# non-final turn to spend, so 1 is correct and tighter. --max-turns is NOT a perf
# lever (it doesn't change decode time); it's just made correct + documented here.
# The error_max_turns -> 504-retry path is kept intact in case a batch ever hits it.
SCRIPT_GEN_MAX_TURNS = int(os.getenv("SCRIPT_GEN_MAX_TURNS", "1"))

# Terse, English, JSON-only system prompt for the headless script-gen call. Passed via
# `--system-prompt` to REPLACE Claude Code's large default system prompt (cuts prefill
# / context). Keep it minimal: the per-call user prompt carries all the real steering.
SCRIPT_GEN_SYSTEM_PROMPT = (
    "You output ONLY a valid JSON array of video script scenes. "
    "No prose, no markdown, no code fences, no explanation — just the JSON array."
)

# Retry the headless call once (configurable) on a TIMEOUT only. Script gen is a
# single, idempotent, side-effect-free prompt, so re-running in a fresh process is
# safe and clears a transient bootstrap/stream stall (the same fresh-process logic
# that fixes the cf-venv CUDA flake). 0 disables retries.
SCRIPT_GEN_RETRIES = int(os.getenv("SCRIPT_GEN_RETRIES", "1"))
SCRIPT_GEN_RETRY_BACKOFF = float(os.getenv("SCRIPT_GEN_RETRY_BACKOFF", "3.0"))

# Number of `claude -p` script-gen batches to run CONCURRENTLY (ThreadPoolExecutor).
# When scene_count chunks into >1 batch, the batches are independent idempotent calls,
# so running them in parallel removes the sequential decode-time stack-up that pushed
# long jobs toward repeated per-batch timeouts. Each batch keeps its OWN retry/backoff
# and per-batch SCRIPT_GEN_TIMEOUT; ordering is preserved (results land in a fixed slot,
# never by completion order) so the merged scene order == sequential order.
# CEILING NOTE: the real limit is the Anthropic SUBSCRIPTION rate-limit, NOT VRAM —
# `claude -p` is a CPU/network subprocess, it does not touch the GPU. Raise this
# GRADUALLY (e.g. 4 -> 6 -> 8) and watch for 429 / "overloaded" / "usage limit" errors
# or a sudden slowdown; back off if they appear. <=1 effectively runs sequentially.
SCRIPT_GEN_CONCURRENCY = int(os.getenv("SCRIPT_GEN_CONCURRENCY", "4"))

# Identifier of the DEFAULT LLM backend: the Claude Code headless CLI (`claude -p`,
# billed to the subscription). It is part of the script disk-cache key so a job on
# provider B can never be served provider A's cached scenes (the same class of bug
# already recorded for the TTS cache, which omitted its engine flags).
#
# The provider gate has now LANDED (llm_gate.py): a job may pick `gemini` / `openrouter`
# per job via jobs.llm_provider. This constant remains the value used when a job makes no
# choice (NULL), so an unset provider hashes exactly as it did before the gate existed.
LLM_PROVIDER_ID = llm_gate.PROVIDER_CLAUDE_CLI

# Version of the SCRIPT-GEN PROMPT TEXT, part of the script disk-cache key.
#
# !!! BUMP THIS WHENEVER ANY PROMPT TEXT THAT FEEDS SCRIPT GEN CHANGES !!!
# That means _build_prompt / _build_footage_prompt / _build_transform_prompt, the shared
# steering constants they embed (_VI_FULL_TRANSLATION_RULE, _KEEP_ENGLISH_TERMS,
# _PROPER_NOUN_DENSITY, _CUT_PROMO*, EDIT_MODE_GUIDE, the per-mode length blocks) and
# SCRIPT_GEN_SYSTEM_PROMPT. The cache key is built from the batch's IDENTITY (edit mode,
# window, budget), NOT from the prompt string, so a reworded prompt is otherwise
# invisible to the cache and a stale hit would silently mask the change for up to
# _SCRIPT_CACHE_TTL_HOURS. Same discipline as the TTS worker's VOICING_VERSION.
PROMPT_VERSION = 1

# Disk cache for per-batch script-gen results. A keep-ratio regen or a 504-retry can
# re-issue the SAME batch (identical edit_mode + source window + budget); caching the
# raw scenes from a successful `claude -p` call lets the repeat skip the whole decode.
# Keyed by the batch's identity (see _script_cache_key); entries older than
# _SCRIPT_CACHE_TTL_HOURS are treated as misses so a content/prompt tweak isn't masked
# by a stale hit. Failed/timed-out calls are NEVER cached.
_SCRIPT_CACHE_TTL_HOURS = 24
_SCRIPT_CACHE_DIR = os.path.join(os.path.dirname(__file__), "_script_cache")


def _script_cache_key(parts: dict, provider: str | None = None,
                      model: str | None = None) -> str:
    """Hash a batch-identity dict into a short cache key.

    The caller's `parts` (edit_mode / word_budget / ratio_nudge / source window / batch
    index) are AUGMENTED here with the three things that decide what the text actually
    looks like but that no call site passes: the provider, the model, and the prompt
    version. Doing it inside this function rather than at each call site means a caller
    cannot forget them and poison the cache across providers/models.

    `provider`/`model` are the job's per-job choice, ALREADY RESOLVED by
    `llm_gate.resolve` (which is pure and network-free precisely so this hash stays
    deterministic). Both default to the claude-cli pair, so a job that made no choice
    hashes byte-for-byte the same as it did before the provider gate existed — verified
    by test_script_cache_key_default_matches_pre_gate_hash.
    """
    keyed = {
        **parts,
        "provider": provider or LLM_PROVIDER_ID,
        "model": model or SCRIPT_GEN_MODEL,
        "prompt_version": PROMPT_VERSION,
    }
    return hashlib.sha256(json.dumps(keyed, sort_keys=True).encode()).hexdigest()[:16]


def _script_cache_path(key: str) -> str:
    return os.path.join(_SCRIPT_CACHE_DIR, f"{key}.json")


def _script_cache_get(key: str) -> list | None:
    """Return the cached scene list for `key` if a fresh (< TTL) entry exists, else
    None. Any read/parse error is a miss (the cache must never break generation)."""
    path = _script_cache_path(key)
    try:
        if not os.path.isfile(path):
            return None
        age_h = (time.time() - os.path.getmtime(path)) / 3600.0
        if age_h >= _SCRIPT_CACHE_TTL_HOURS:
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _script_cache_put(key: str, scenes: list) -> None:
    """Write `scenes` to the cache under `key`. Best-effort: a write failure is logged
    and ignored so caching never fails a generation."""
    try:
        os.makedirs(_SCRIPT_CACHE_DIR, exist_ok=True)
        with open(_script_cache_path(key), "w", encoding="utf-8") as f:
            json.dump(scenes, f, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001 — cache is advisory only
        log.warning("[script] cache write failed for %s: %s", key, e)


# ComfyUI runs on the host (the n8n container reaches it via host.docker.internal).
COMFY_URL = os.getenv("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
SDXL_CHECKPOINT = os.getenv("SDXL_CHECKPOINT", "sd_xl_base_1.0.safetensors")

# Studio "Model dựng" (render_model) -> ComfyUI checkpoint filename. Only sdxl-base
# is installed today; the rest are placeholders until their .safetensors are added.
RENDER_CHECKPOINTS = {
    "sdxl-base": "sd_xl_base_1.0.safetensors",
    "juggernaut-xl": "Juggernaut-XL_v9.safetensors",
    "realvisxl": "realvisxlV50.safetensors",
    "dreamshaper-xl": "dreamshaperXL_v21.safetensors",
    "sdxl-turbo": "sd_xl_turbo_1.0_fp16.safetensors",
    "sdxl-lightning": "sdxl_lightning_8step.safetensors",
    "sd35-medium": "sd3.5_medium.safetensors",
}

# VieNeu-TTS and faster-whisper live in the project venv (cf-venv), NOT this API's
# venv. We call them through small worker scripts run by that interpreter — same
# subprocess pattern as Claude above, so the heavy ML deps stay isolated.
CF_VENV_PYTHON = os.getenv("CF_VENV_PYTHON", r"E:\Installed\cf-venv\Scripts\python.exe")
WORKERS_DIR = os.path.join(os.path.dirname(__file__), "workers")


def _cf_torch_lib_dir() -> str:
    """Locate the cf-venv torch/lib dir (holds the bundled cuDNN9 / cuBLAS12 DLLs
    that CTranslate2 needs for faster-whisper on CUDA). Derived from CF_VENV_PYTHON:
    <venv>/Scripts/python.exe -> <venv>/Lib/site-packages/torch/lib. Returns "" if
    not found so the wiring degrades gracefully (CPU fallback / clear log)."""
    override = os.getenv("CF_TORCH_LIB")
    if override and os.path.isdir(override):
        return override
    try:
        venv_root = os.path.dirname(os.path.dirname(CF_VENV_PYTHON))  # .../<venv>
        cand = os.path.join(venv_root, "Lib", "site-packages", "torch", "lib")
        return cand if os.path.isdir(cand) else ""
    except Exception:
        return ""


# Resolved once at import. Prepended to each cf-venv worker's PATH so CTranslate2
# (faster-whisper) finds torch's cuDNN9/cuBLAS DLLs; the workers also call
# os.add_dll_directory on it (PATH alone is not honored for DLL search on modern
# Windows Python) before importing faster_whisper.
_CF_TORCH_LIB = _cf_torch_lib_dir()

# Finished/intermediate media live OUTSIDE the repo (large/regenerable).
CONTENT_OUTPUT_ROOT = os.getenv("CONTENT_OUTPUT_ROOT", r"E:\ContentFactory")

# faster-whisper defaults: CUDA/float16 — measured ~7x faster than CPU/int8 on the
# RTX 2070 Max-Q (154s -> 21.6s on a 30s clip) at ~3.3 GB peak VRAM. Models run
# sequentially, so the GPU is free during STT. Requires torch's bundled cuDNN9 /
# cuBLAS12 on the worker's DLL search path (wired below via _CF_TORCH_LIB +
# os.add_dll_directory in the CT2-loading workers). To run on a no-GPU box, set
# WHISPER_DEVICE=cpu and WHISPER_COMPUTE=int8 in .env (graceful documented fallback).
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "medium")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cuda")
WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE", "float16")

# ---- Per-scene pace normalization (Issue 2, owner-approved Option 1) --------
#
# F5-TTS has only a GLOBAL speed scalar and draws per-utterance prosody stochastically,
# so scenes vary in speaking pace (measured on video 82: 183-278 ms/syllable, ±22% about
# a 227 ms median) even though nothing in the pipeline intentionally stretches them. The
# owner wants a CONSISTENT, smooth narration pace. We SOFT-CLAMP outliers: a scene whose
# pace sits inside [BAND_LO, BAND_HI]×median is left UNTOUCHED; a scene slower than BAND_HI
# is gently sped up toward the band EDGE (not to the median — partial correction keeps
# natural variation), and a scene faster than BAND_LO is gently slowed toward the edge.
# The atempo is clamped to [ATEMPO_MIN, ATEMPO_MAX] so a correction can never introduce
# time-stretch artifacts. This is normalization (pull outliers to the band), NOT the
# forbidden "cram words to fit a length budget".
#
# ORDERING (critical): this runs FIRST in assemble_footage, BEFORE the caption-timing
# whisper — it rewrites each corrected scene's audio to a normalized wav and repoints
# the scene at it, so the caption whisper measures the FINAL (retimed) audio and karaoke
# stays in sync. Pace is measured as the MEDIAN of per-WORD ms/syllable in a scene, which
# is robust to the deliberately-slowed "prompt" (one 2× word is a single outlier the
# median ignores). Additionally, a scene that CONTAINS a slow-term is never SPED UP
# (only slowed if needed), so the Issue-1 "prompt" slowdown is never undone.
F5_PACE_NORMALIZE = os.getenv("F5_PACE_NORMALIZE", "1").strip().lower() not in ("0", "off", "false", "no")
F5_PACE_BAND_LO = float(os.getenv("F5_PACE_BAND_LO", "0.85"))   # below this ×median = too fast
F5_PACE_BAND_HI = float(os.getenv("F5_PACE_BAND_HI", "1.15"))   # above this ×median = too slow
# Correction target as a fraction of the way from the outlier to the band edge (1.0 =
# pull exactly to the edge; <1.0 = softer). Default full-to-edge (still only the edge,
# not the median, so variation is preserved).
F5_PACE_EDGE_PULL = float(os.getenv("F5_PACE_EDGE_PULL", "1.0"))
F5_PACE_ATEMPO_MIN = float(os.getenv("F5_PACE_ATEMPO_MIN", "0.85"))  # max slow-down (audio slower)
F5_PACE_ATEMPO_MAX = float(os.getenv("F5_PACE_ATEMPO_MAX", "1.25"))  # max speed-up
# Slow-terms to PROTECT from speed-up. A scene whose original narration contains one of
# these is never sped up (so the tilde-slowed "prompt" stays slow). NOTE: the tts_worker
# F5_SLOW_TERMS env is now empty by default (the old post-synth slow was retired for the
# deterministic tilde SLOW-join), so this list is now defined independently here; it still
# defaults to "prompt" so the pace passes keep protecting the owner's slowed term.
F5_PACE_PROTECT_TERMS = [t.strip().lower() for t in os.getenv("F5_PACE_PROTECT_TERMS", "prompt").split(",") if t.strip()]
_PACE_VOWELS = "aeiouyàáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵ"

# AUTO TARGET-PACE (owner-approved, voice-INDEPENDENT primary mechanism). After all scenes
# are synthesized (with the per-word fixes already baked in), we measure the WHOLE video's
# overall pace = MEDIAN of per-scene median-per-word ms/syllable, then SLOW every scene
# UNIFORMLY so the overall pace hits GLOBAL_TARGET_MS_PER_SYL. Because the slowdown is one
# uniform atempo applied to all scenes, ALL relative timing is preserved — the slowed
# "prompt" and tightened acronyms scale together and stay relatively slow/tight. SLOW-ONLY:
# if the video is already at/slower than target the factor is 1.0 (never sped up). This
# replaces the voice-dependent fixed F5_SPEED_SCALE as the primary pace control: it adapts
# to whatever cadence the chosen clone voice produced, so every video lands at ~the same
# reading pace regardless of voice. Target default 254 ms/syll = the previous-good baseline
# (v12/v82 Escbase ~210) + 10% + a further 10% slower (231 -> 254), per the owner's
# 2026-07-26 request to slow overall reading pace by 10% across all modes. Kept in sync
# with the 10% cut to _VI_WORDS_PER_SEC below so footage/translate still fit the source.
# All env-tunable; 0/off disables.
GLOBAL_TARGET_PACE = os.getenv("GLOBAL_TARGET_PACE", "1").strip().lower() not in ("0", "off", "false", "no")
GLOBAL_TARGET_MS_PER_SYL = float(os.getenv("GLOBAL_TARGET_MS_PER_SYL", "254.0"))
# Safe atempo floor: never slow more than this (0.5 = at most 2× longer). Guards against a
# pathological measurement forcing an extreme, artifact-prone stretch.
GLOBAL_TARGET_ATEMPO_FLOOR = float(os.getenv("GLOBAL_TARGET_ATEMPO_FLOOR", "0.5"))
# Speed-up CEILING for the TWO-DIRECTIONAL target-pace (owner-approved 2026-07-03). A voice
# SLOWER than target is sped up toward it, but by AT MOST this atempo (1.15 = +15% faster) so a
# very slow voice is nudged to the common pace without ever being rushed into a fast read. Set
# to 1.0 to disable speed-up entirely (reverts to the old slow-only behavior). Clamped >=1.0.
GLOBAL_TARGET_ATEMPO_CEIL = max(1.0, float(os.getenv("GLOBAL_TARGET_ATEMPO_CEIL", "1.15")))

# OMNIVOICE UNIFORM PACE knobs — consumed ONLY by the OMNIVOICE-ONLY section further down
# (_omnivoice_uniform_pace / _omnivoice_pace_verify). Kept next to the GLOBAL_TARGET_* knobs
# above for readability; no F5/VieNeu code reads them.
#
# (owner request 2026-07-28). OmniVoice narration reads too FAST and
# UNEVENLY scene-to-scene. The global _auto_target_pace applies ONE uniform atempo — it
# converges the OVERALL median to target but does NOT flatten scene-to-scene variance, so it
# was judged unhelpful for OmniVoice. It is SUPPLEMENTED (OmniVoice only) by
# a PER-SCENE pass (_omnivoice_uniform_pace): each scene is retimed INDIVIDUALLY toward a common
# OmniVoice target ms/syllable, so EVERY scene reads at ~the same pace (uniform) AND slower
# overall. per-scene atempo = scene_pace / OMNIVOICE_TARGET_MS_PER_SYL, clamped to
# [FLOOR, CEIL]. FLOOR (<1) caps the max SLOWDOWN of a very-fast scene (guards an extreme
# stretch); CEIL (slightly >1) only NUDGES a slower-than-target scene UP to the common pace —
# never a real speed-up/rush, honoring the owner's hard rule "never read faster to shorten a
# video" (narration-speed-rule). Because OmniVoice is faster (SMALLER ms/syll) than the target,
# the pass is almost entirely a SLOWDOWN; CEIL just equalizes the rare slow outlier. Default
# target ~200 ms/syll = the comfortable VN narration band F5 targets (~2.0-2.1 wps), slower
# than OmniVoice's measured native pace (see _workspace calibration). All env-tunable; set
# OMNIVOICE_TARGET_MS_PER_SYL=0 to disable the pass (falls back to no OmniVoice pace change).
# RETUNED 200 -> 220 (owner 2026-07-29: "word-metric nếu là 200 sẽ bị đọc nhanh hơn target ->
# kéo xuống gần target mong muốn"). At 200 a scene measuring exactly 200 got factor 1.00 and was
# left alone, yet it still read fast: with the interior gaps now controlled, a no-pause scene
# delivers a HEARD pace ≈ its word pace, so word 200 = heard 200 — noticeably faster than the
# rest of the video. The owner's own two reference points bracket the wanted pace: 191 ms/syll
# (video 281 scene 1 before the comma) was "quá nhanh" and 252 (after the comma) "quá chậm", so
# the target sits near their midpoint. Swept over video 281's 88 scenes (raw cached wavs,
# acronym+digit-aware metric, beat-discounted wall):
#   target  word med  wall med  min  max  spread  railed@0.80  railed@1.10  audio total
#     200      200      208     158  282    124        2           14         337s
#     210      210      216     166  282    116        7            8         351s
#     220      220      225     174  282    108       12            4         364s  <- chosen
#     230      225      234     181  282    100       35            3         376s
# 220 lands the heard median at 225 and nearly eliminates scenes still being sped up (14 -> 4).
# 230 is past the useful limit: 35 scenes can no longer reach target inside the 0.80 floor.
# COST, stated plainly: the VO grows ~11% (327s -> 364s of audio on this video).
# RECALIBRATED 210 -> 200 (owner 2026-08-03, job 315 / video 306). The owner nominated the
# 0:55-1:03 span ("Như các hãng luật…" + "Công ty luật dùng Claude Mythos…") as the reference
# pace; measured on the delivered mp4 with the corrected syllable counter it reads 200.0
# ms/syll word-level. Chosen KNOWINGLY with two caveats reported up front: (a) it does NOT
# fix the job-315 complaint — that scene already delivered 200.0, identical to the reference,
# because the problem there was the pause, not the speech rate (see _omnivoice_normalize_joins);
# (b) it shortens the VO ~4.3%. Measured over job 315's own 91 units, 210 -> 200 flips the
# correction direction on 16 units and moves 83 by >0.02 atempo.
#
# COMPANION KNOBS — checked, no change needed:
#   • OMNIVOICE_WALL_TARGET_RATIO is a RATIO of the target, so the perceived-pace ceiling
#     follows automatically (233 -> 222 ms/syll). NON-OBVIOUS CONSEQUENCE: that ceiling is now
#     TIGHTER in absolute terms, so more units will be limited by it rather than by their word
#     pace — i.e. more scenes that are "already slow as heard" get left alone.
#   • FLOOR/CEIL are dimensionless atempo bounds, independent of the target. A lower target
#     RAISES every factor (pace/200 > pace/210), so rails shift AWAY from FLOOR toward CEIL:
#     8 -> 12 railed units on job 315, in the direction measured safe (ASR intact to 1.35).
#
# 2026-08-04: REVERTED 200 -> 210 (owner). The 200 experiment lasted a day; the ~4.3% faster
# overall read was not wanted. Everything above stands as the record of why 200 was tried.
OMNIVOICE_TARGET_MS_PER_SYL = float(os.getenv("OMNIVOICE_TARGET_MS_PER_SYL", "210.0"))
# Max SLOWDOWN atempo. Guards a fast measurement from forcing an extreme, artifact-prone
# stretch — atempo time-stretches the WAVEFORM, so a large factor makes individual words
# sound audibly DRAGGED (vowels smeared), which the owner explicitly rejects: "giãn/nén chữ
# nhưng không gây đọc kéo dài làm người nghe cảm thấy từ bị đọc dài ra khó chịu" (2026-07-29).
# HISTORY: 0.75 -> 0.70 (2026-07-28) chased the last few fast scenes, but 0.70 = +43% word
# length, i.e. exactly the dragged-out read now rejected. 0.70 -> 0.85 (+18%), then LOOSENED
# 0.85 -> 0.80 (+25% max) on the owner's "nới trần 1 chút để về đúng target" (2026-07-29):
# 0.80 is the LEAST stretch at which the fast tail actually reaches target. Swept over video
# 278's 88 scenes (acronym-aware metric, delivered wall pace):
#   floor  median  min  spread  stdev  clamped  scene1  scenes >15% faster than median
#   0.85     214   180    120    20.0     11      190     1
#   0.80     217   185    115    19.3      4      202     0    <- chosen
#   0.70     217   185    115    19.1      0      202     0
# Below 0.80 buys nothing: scene 1 is then bounded by its own word target rather than the
# floor (0.78/0.75/0.70 all deliver 202) while the stretch keeps growing. 0.85 for reference
# is still the F5 pass's bound (F5_PACE_ATEMPO_MIN), documented there as artifact-free.
# CONSEQUENCE, stated plainly: 4 scenes still cannot reach target. That is NOT a stretch
# problem — an acronym is spoken as a fast blurred letter-run, cured by RESPELLING it in
# word_improve.md (the "pờ~rôm" mechanism), not by dragging the audio.
# WIDENED 0.80 -> 0.75 (owner "nới trần/sàn ra 1 chút để test", 2026-07-29) = at most +33% word
# length. Swept over video 281's 88 scenes at target 220 (see OMNIVOICE_TARGET_ATEMPO_CEIL for
# the full table): it releases the floor for 6 of the 12 clamped scenes at a negligible duration
# cost. 0.70 (+43%) is deliberately NOT used — that is the dragged-out read the owner rejected.
#
# TIGHTENED 0.75 -> 0.85 (owner 2026-08-04, after job 316 scene 5: a 155 ms/syll native draw
# took factor 0.775 = +29% duration and read as elongated). This caps the worst-case stretch at
# +18%. It is a deliberate GLOBAL tradeoff — the floor applies video-wide — and the SAME value
# was tried and reverted on 2026-07-30 when it was used as a single-scene fix; see the .env
# comment for that history and for the measured cost (26 units newly railed, 2 losing the
# +/-25 ms/syll convergence band, VO ~2.3% shorter on job 316).
OMNIVOICE_TARGET_ATEMPO_FLOOR = float(os.getenv("OMNIVOICE_TARGET_ATEMPO_FLOOR", "0.85"))
# Re-measure the RETIMED audio with one extra whisper pass and log the ACTUAL delivered
# pace, instead of only the analytic projection (pace * scale) the pass used to report.
# The projection claimed "spread 90 -> 22.4 ms/syll" on video 258 while the delivered mp4
# still measured 188-282 ms/syll, so the old log could report a uniformity it had not
# achieved. Costs ~1 extra whisper pass per job; set to 0 to skip.
OMNIVOICE_PACE_VERIFY = os.getenv("OMNIVOICE_PACE_VERIFY", "1").strip().lower() not in ("0", "off", "false", "no")
# Max NUDGE-UP atempo for a slower-than-target scene (1.10 = at most +10% faster) so a slow
# scene is brought toward the common pace without ever being rushed. Set to 1.0 for slow-only.
# WIDENED 1.10 -> 1.20 (same owner request). This is the ONLY knob that touches the slow tail:
# the slowest scene is limited purely by it. Swept over video 281's 88 scenes at target 220
# (raw cached wavs, acronym+digit-aware metric, beat-discounted wall):
#   floor  ceil  wall med  min  max  spread  clamped@floor  clamped@ceil  audio   max word stretch
#    0.80  1.10     225    174  282    108        12             4        +11.3%       25%
#    0.75  1.15     226    174  270     96         6             3        +11.8%       33%
#    0.75  1.20     226    174  258     85         6             2        +11.7%       33%  <- chosen
#    0.70  1.25     227    174  248     74         2             2        +11.9%       43%  (rejected: drag)
# Duration is nearly unaffected (+11.3% -> +11.7%) because the extra slowdowns and speed-ups
# offset. NOTE the `min` column: 174 ms/syll does NOT move at any setting — the fastest-reading
# scene is not clamp-limited at all, its factor comes from the WORD median while its span-based
# rate is much lower (a few long words lift the median). That residual is a metric disagreement,
# not a band problem, so widening further cannot fix it.
OMNIVOICE_TARGET_ATEMPO_CEIL = max(1.0, float(os.getenv("OMNIVOICE_TARGET_ATEMPO_CEIL", "1.20")))
# ---- Speed-up guards (job 334 / video 325 "cảnh đầu đọc nhanh") ----------------------
# Root cause measured on job 334 scene 1 ("Tỷ lệ thất nghiệp lại tăng, và nhiều người đổ
# lỗi cho AI"): the unit factor is a MEDIAN of per-word rates, and the clause boundary is
# inferred from the audio's own silences. That fresh OmniVoice draw (a) stretched the
# sentence-initial "Tỷ" to 600 ms (the known chunk lead-in artifact) and (b) dropped the
# comma pause after "tăng," to 0 ms, so the splitter cut after "và" instead. The resulting
# 7-word clause carried BOTH long bookends (600 ms "Tỷ", 280 ms "và") against 100-140 ms
# middle syllables; its median read 260 ms/syll = "too slow", so the pass sped the WHOLE
# clause up at the 1.20 CEIL and crushed the already-fast middles to 80-120 ms/syll —
# ~40% faster than the video's 215 ms/syll baseline, and the first thing the viewer hears.
# The PREVIOUS job (333/v324) spoke the SAME sentence, same code, same knobs, 30 min
# earlier: its draw kept the comma pause, split at 6 words, measured 190 ms/syll and was
# SLOWED 0.905. So the trigger is draw variance, but the fragility is here: one lost pause
# swings the same sentence 0.905 -> 1.20. These three guards make the factor robust to it.
#
# GUARD 1 — lead-in drop. A unit-initial word longer than this ratio x the unit's own
# median is the chunk lead-in artifact, not speech rate; it is excluded from BOTH the word
# median and the wall metric (the time is real, but it is not a rate the listener parses as
# tempo). On job 334 scene 1 this moves the clause median 260 -> ~200, i.e. factor 1.238
# (railed at 1.20) -> ~0.95, which is what v324's draw got. 0 disables.
OMNIVOICE_LEADIN_DROP_RATIO = float(os.getenv("OMNIVOICE_LEADIN_DROP_RATIO", "2.5"))
# GUARD 2 — per-word floor. Never apply a SPEED-UP that would push any word in the unit
# below this delivered rate. 150 ms/syll is the "clearly rushed" threshold already
# calibrated in tts_worker.py (normal VN ~285, acceptable English re-renders 110-170).
# Caps speed-ups only — never forces a slow-down, per the constant-pace rule. 0 disables.
OMNIVOICE_MIN_WORD_MS_PER_SYL = float(os.getenv("OMNIVOICE_MIN_WORD_MS_PER_SYL", "150.0"))
# GUARD 3 — variance skip. When a unit's slowest/fastest word-rate ratio still exceeds this
# AFTER the lead-in drop, the clause is internally uneven and ONE uniform atempo is the
# wrong tool: speeding it up only crushes the fast half. Speed-ups are skipped (factor
# pinned to 1.0); slow-downs are unaffected (they never rush anything). 0 disables.
OMNIVOICE_UNIT_VARIANCE_SKIP = float(os.getenv("OMNIVOICE_UNIT_VARIANCE_SKIP", "4.0"))
# PERCEIVED-pace ceiling (owner request 2026-07-29). OMNIVOICE_TARGET_MS_PER_SYL above is a
# WORD-metric target: a median of per-word rates that ignores the silence BETWEEN words. A
# scene can therefore be slow to the ear and still measure "fast", and then get stretched
# hard — which is the job-283 scene-3 bug (word 140 -> floor 0.70 -> +43% stretch -> the
# slowest-heard scene of the opening). _omnivoice_uniform_pace now also bounds the stretch by
# the WALL-CLOCK rate so no scene is ever slowed past this ceiling.
#
# The ceiling is expressed as a RATIO of the word target, not an absolute, so it tracks any
# retune of OMNIVOICE_TARGET_MS_PER_SYL. Default 1.11 = the measured word->wall gap ("wall-clock
# is 11% slower than the word metric on average", logged by the pace-verify pass). That default
# is deliberately the value that PRESERVES today's overall pace: word-target 200 has been
# landing at ~222 ms/syll perceived, so a 222 ceiling removes the outliers WITHOUT making the
# whole video faster. Simulated on job 283's 49 measured scenes: perceived median 222 -> 222
# (unchanged), spread 129 -> 86 ms/syll, and the reported scene 3 261 -> 222. LOWER the ratio
# to make everything slower-but-more-uniform; 0 disables the ceiling (pre-fix behavior).
OMNIVOICE_WALL_TARGET_RATIO = float(os.getenv("OMNIVOICE_WALL_TARGET_RATIO", "1.11"))
# Longest inter-word gap that still counts as natural rhythm when measuring the PERCEIVED
# (wall) pace. Anything above it is treated as a deliberate pause and discounted, because the
# OmniVoice path now inserts punctuation BEATS by construction — counting those made the pass
# read a scene as "already slow" and apply its maximum SPEED-UP, which is the job-290 report
# ("giây 9 / giây 25 bị tăng tốc so với tổng thể"): video 281 scene 4 wanted factor 1.00 from
# its word pace and got 1.10, delivering 180 ms/syll against a 200 median.
# 0.12 s sits above OmniVoice's natural word-to-word rhythm (measured: interior gaps are ~0 ms
# inside a clause) and below the shortest beat (OMNIVOICE_BEAT_MID_S 0.18 s, ~0.26-0.33 s as
# heard), so it separates the two cleanly. Set negative to disable the discount.
OMNIVOICE_WALL_MAX_GAP_S = float(os.getenv("OMNIVOICE_WALL_MAX_GAP_S", "0.12"))

# PER-UNIT (word-aware) PACE CORRECTION — owner decision 2026-08-02, replacing the
# whole-scene single-factor correction. A scene is split into CLAUSE UNITS at the
# measured inter-word gaps and each unit is retimed by its OWN factor.
#
# WHY (job 308 / video 299, owner: "13-15s đọc chậm hơn"). Scene 4 was
# "Đoạn mã dài 427 dòng, là runtime sinh ra hơn trăm agent." Measured on the DELIVERED
# audio (whisper on the finished mp4), its two clauses were:
#     "Đoạn mã dài 427 dòng,"            280 ms/syll word · 286 wall   <- the slow part
#     "là runtime sinh ra hơn trăm agent." 173 ms/syll word · 172 wall
# The SCENE aggregate was 187 word / 228 wall — comfortably on target, and below the
# perceived-pace ceiling — so the whole-scene pass saw nothing wrong and applied ONE
# +17.8% stretch uniformly, which dragged the already-slow first clause to 286 ms/syll
# against neighbours at 200-245. An aggregate cannot see that: a scene whose clauses sit
# at 280 and 173 averages to "fine". Per-unit correction speeds the 280 clause UP and
# slows the 173 clause DOWN, converging both on the target instead of preserving the
# 107 ms/syll split inside one scene.
#
# Split threshold reuses the OMNIVOICE_WALL_MAX_GAP_S logic (same physical boundary:
# above natural word-to-word rhythm ~0 ms, below the shortest inserted beat) so the two
# knobs stay consistent — a gap big enough to discount as a deliberate pause is exactly a
# gap big enough to cut a unit at. Splitting on the MEASURED gaps (not on narration
# punctuation) keeps the unit boundaries self-consistent with the very timings being
# measured, avoiding a text<->whisper alignment failure mode on loanword-heavy Vietnamese.
OMNIVOICE_UNIT_SPLIT_GAP_S = float(os.getenv("OMNIVOICE_UNIT_SPLIT_GAP_S", "0.12"))
# A unit below this many words is not independently measurable (_scene_pace_ms_per_syl
# needs >= 3 words for a median) and is merged into its neighbour. A scene that ends up
# with ONE unit falls back to the whole-scene single-factor path — same numbers as before.
OMNIVOICE_UNIT_MIN_WORDS = int(os.getenv("OMNIVOICE_UNIT_MIN_WORDS", "3"))

# UNIT BOUNDARIES FROM THE AUDIO ITSELF (owner request 2026-08-03), replacing whisper gaps
# as the PRIMARY signal. Whisper extends a word's end across short low-level audio and
# reports gap=0.00 even where a real pause exists: job 313 scene 23 has 0.129 s of quiet at
# its comma (0.104 s of it DIGITAL silence) yet whisper reported no gap at all, so the
# retiming pass saw one 'whole' unit and the per-clause correction silently degraded to the
# whole-scene behavior it was written to replace.
#
# The reliable signal is one WE write: the punctuation-beat concat inserts a run of TRUE
# ZERO samples at every clause join. Measured across all 59 cached scenes of job 313, that
# run is 0.090-0.104 s at essentially |sample| == 0, whereas OmniVoice's own clause-edge
# decay is quiet but NEVER digitally zero (it sits at -45..-55 dBFS, |sample| ~60-180).
# Scanning for near-zero runs therefore recovers exactly the clauses the TTS worker actually
# synthesized separately — verified 59/59 scenes against the worker's own split, with zero
# false positives and zero misses at amp<=2 / min 0.05-0.07 s.
#
# amp is in raw 16-bit units (2 ≈ -84 dBFS) to tolerate ±1-2 LSB from an ffmpeg re-encode;
# min length sits below the 0.09 s beat and far above any dither run.
OMNIVOICE_UNIT_SILENCE_AMP = int(os.getenv("OMNIVOICE_UNIT_SILENCE_AMP", "2"))
OMNIVOICE_UNIT_SILENCE_MIN_S = float(os.getenv("OMNIVOICE_UNIT_SILENCE_MIN_S", "0.05"))

# POST-CORRECTION CONVERGENCE BAND (owner request 2026-08-03: "every clause consistent with
# the overall pace"). The floor/ceil clamp exists to bound time-stretch artifacts, NOT to
# define an acceptable result — a unit needing factor 1.333 rails at CEIL 1.20 and lands at
# ~233 ms/syll against a 210 target (job 308 scene 4 unit 0). That is a real, audible miss,
# and accepting the rail silently makes the pass look successful when it is not. Any unit
# whose PROJECTED delivered pace is still further than this from the target is reported as
# UNCONVERGED with the amount, so structurally-unfixable clauses stay visible.
OMNIVOICE_MAX_PACE_DEVIATION_MS = float(os.getenv("OMNIVOICE_MAX_PACE_DEVIATION_MS", "25.0"))

# CLAUSE-JOIN QUIET NORMALIZATION (owner approved 2026-08-03, jobs 313 + 315).
# The heard pause at a clause join is (our fixed inserted beat) + (however much clause-edge
# decay OmniVoice happened to render), and the second term varies 0.025-0.193 s draw to draw.
# Identical settings therefore produce joins from 0.129 s (job 313 scene 23 — comma inaudible,
# "reads too fast") to 0.261 s raw / ~0.46 s delivered (job 315 scene 25 — "stretched out").
# Two owner complaints, one mechanism, opposite polarities. The per-unit atempo then AMPLIFIES
# it: the join silence is stretched by the adjacent unit's factor, so the units needing the
# biggest slowdown get the most inflated joins (job 315 unit2: factor 0.762 -> x1.31 on its
# half of the join).
#
# Fix: after retiming, drive the TOTAL quiet at each join to OMNIVOICE_JOIN_TARGET_S, adding
# or removing as needed.
#
# SAFETY — why this is not the reverted gap-shaper. That one re-cut finished audio at GUESSED
# whisper word boundaries and clipped Vietnamese soft-consonant tails. This one adds/removes
# samples ONLY inside the run of TRUE DIGITAL ZEROS that our own _concat_wavs_48k wrote
# (|sample| <= OMNIVOICE_UNIT_SILENCE_AMP). Those samples carry no signal whatsoever, so no
# speech sample can be touched even in principle — the model's decay, which IS signal, is
# measured but never modified. Verified empirically: stripping the near-zero samples from the
# before/after wavs yields byte-identical audible sample sequences.
#
# Consequence of that safety rule: when the model's own decay alone already exceeds the
# target, the join can only shrink to the decay length (all our zeros removed but no more).
# That case is logged rather than forced.
OMNIVOICE_JOIN_TARGET_S = float(os.getenv("OMNIVOICE_JOIN_TARGET_S", "0.22"))
# Never shrink our zero run below this — stops the two clauses' decay tails from butt-joining.
# Sized by MEASUREMENT, not by taste: it caps how much a long join can shrink, and 0.06 was
# too coarse — it left video 306 scene 25 (the reported "stretched out" case) stuck at 0.253 s
# instead of the 0.22 s target, salvaging only 2.3 of the 32 ms/syll it should have. 0.03
# leaves enough headroom to reach target there while still keeping a real gap between the two
# clauses' decay tails.
# NOTE: after a shrink the residual run can fall below OMNIVOICE_UNIT_SILENCE_MIN_S, i.e. the
# join stops being detectable. That is intentional and harmless: detection runs BEFORE
# normalization within a render (unit splitting), the normalized wav lives only in the temp
# work dir, and the TTS cache keeps the untouched original for every future render.
OMNIVOICE_JOIN_MIN_ZEROS_S = float(os.getenv("OMNIVOICE_JOIN_MIN_ZEROS_S", "0.03"))
# Absolute threshold (dBFS) for MEASURING the extent of the quiet around a join. Absolute, not
# peak-relative: perception works on absolute level, and the peak-relative -50 dB used by
# _trim_unit_edges is exactly why the residual varies with clip loudness in the first place.
OMNIVOICE_JOIN_QUIET_DB = float(os.getenv("OMNIVOICE_JOIN_QUIET_DB", "-45.0"))

# Acronym detection: a scene that CONTAINS an acronym is never SLOWED DOWN by pace-
# normalization — an acronym is SUPPOSED to read as a tight/fast unit, so a scene that
# looks "too fast" partly because of a compact acronym must not be dragged out (exactly
# the "chatGPT stretched to chai-pi-ti" complaint). An acronym = a run of 2+ uppercase
# letters, INCLUDING the caps part of a mixed-case token like "chatGPT" (→ "GPT"). The
# median-of-per-word pace metric already de-weights a single fast word, so this is a
# belt-and-suspenders guard. Speeding a scene UP is still allowed (that never stretches).
_ACRONYM_RE = re.compile(r"[A-Z]{2,}")


def _scene_has_acronym(text: str) -> bool:
    return bool(_ACRONYM_RE.search(text or ""))


# Optional per-call progress sink. The runner installs one (thread-local) around
# a worker call so the worker's intermediate progress — written to a JSON file —
# is forwarded live to the job row. Thread-local so concurrent HTTP worker calls
# (FastAPI threadpool) are unaffected.
_progress_local = threading.local()


def set_progress_cb(cb) -> None:
    """Install (cb) or clear (None) a progress callback for the CURRENT thread.

    cb receives (pct: int 0-100, msg: str) each time the worker reports progress.
    """
    _progress_local.cb = cb


# --- Active cf-venv worker PID registry --------------------------------------
# The /api/system monitor reports the footprint of what the RUNNING TASK is
# consuming, not the whole machine. Heavy pipeline steps (TTS / whisper / ingest
# / download) run as cf-venv python SUBPROCESSES spawned here; while one is alive
# we record its PID. main.py reads this set to scope RAM (RSS of the PID subtree)
# and VRAM (nvidia-smi compute-apps filtered to the PID subtree) to the worker.
# A set (not a single var) because, in principle, multiple worker calls can be in
# flight on the FastAPI threadpool; the lock guards concurrent add/discard.
_worker_pids: set[int] = set()
_worker_pids_lock = threading.Lock()


def _register_worker_pid(pid: int) -> None:
    with _worker_pids_lock:
        _worker_pids.add(pid)


def _unregister_worker_pid(pid: int) -> None:
    with _worker_pids_lock:
        _worker_pids.discard(pid)


def active_worker_pids() -> set[int]:
    """Snapshot of currently-running cf-venv worker PIDs (the active task's
    subprocesses). Empty when idle."""
    with _worker_pids_lock:
        return set(_worker_pids)


# --- Immediate-stop subprocess kill registry ---------------------------------
# The cooperative stop (runner._CANCEL_REQUESTED, checked at step boundaries) only
# aborts BETWEEN steps; a long child (yt-dlp / whisper / claude -p / TTS / FFmpeg)
# runs to completion first. To make POST /api/jobs/{id}/stop interrupt the ACTIVE
# child immediately, EVERY subprocess this module spawns for a pipeline step is
# registered here against the job that owns it, so the stop endpoint can hard-kill
# the whole process TREE (Windows: taskkill /F /T — children/grandchildren die too).
#
# Ownership model: the runner is a SINGLE worker thread (one job at a time), but a
# footage job runs TTS in a side thread CONCURRENTLY with FFmpeg cuts, so >1 child
# can be live for the same job at once. Hence job_id -> set[Popen]. The runner
# publishes the "current job id" via set_active_job() at the top of _process_job /
# _process_clone_job and clears it (set_active_job(None)) when the job finishes;
# the side TTS thread inherits that id because it is a plain module global, not a
# thread-local (the single worker only ever drives one job, so there is no ambiguity).
#
# HONEST LIMITATION: ComfyUI image generation is an HTTP request (generate_images,
# urllib), NOT a subprocess — there is nothing here to kill, so an in-flight SDXL
# render is NOT interrupted by stop; it aborts at the next _check_cancel boundary.
_active_job_id: int | None = None
_job_procs: dict[int, set[subprocess.Popen]] = {}
_job_procs_lock = threading.Lock()


def get_active_job_id() -> int | None:
    """Return the currently running job id (None when idle). Thread-safe read."""
    with _job_procs_lock:
        return _active_job_id


def set_active_job(job_id: int | None) -> None:
    """Publish which job the runner is currently processing, so every subprocess
    spawned for a pipeline step is attributed to it for immediate-kill on stop.
    Called by the runner around a job; clearing (None) also drops any leftover proc
    handles for the just-finished job (they should already be cleared on exit)."""
    global _active_job_id
    with _job_procs_lock:
        if job_id is None and _active_job_id is not None:
            _job_procs.pop(_active_job_id, None)
        _active_job_id = job_id


def _register_job_proc(proc: subprocess.Popen) -> int | None:
    """Attribute a freshly-spawned pipeline subprocess to the active job (if any).
    Returns the job_id it was registered under (None when no job is active — e.g. an
    on-demand preview/clone call, which stop never targets). Safe no-op when None."""
    with _job_procs_lock:
        jid = _active_job_id
        if jid is None:
            return None
        _job_procs.setdefault(jid, set()).add(proc)
        return jid


def _unregister_job_proc(job_id: int | None, proc: subprocess.Popen) -> None:
    """Drop a finished subprocess from its job's live set (best-effort)."""
    if job_id is None:
        return
    with _job_procs_lock:
        procs = _job_procs.get(job_id)
        if procs:
            procs.discard(proc)
            if not procs:
                _job_procs.pop(job_id, None)


def kill_job_processes(job_id: int) -> int:
    """Hard-kill EVERY live pipeline subprocess of `job_id` and their descendants.

    Called by POST /api/jobs/{id}/stop AFTER marking the row 'stopped', so a long
    in-flight child (yt-dlp/whisper/claude/TTS/FFmpeg) dies immediately instead of
    running to the next step boundary. Reuses the SAME tree-kill as the claude-p
    timeout path (_kill_proc_tree → Windows `taskkill /F /T`) so node/ffmpeg
    grandchildren are reaped too. Best-effort: a kill failure never raises. Returns
    the number of process trees it attempted to kill (0 = nothing live to kill, e.g.
    the job is between steps or waiting on the ComfyUI HTTP call).
    """
    with _job_procs_lock:
        procs = list(_job_procs.get(job_id, ()))
    for proc in procs:
        try:
            if proc.poll() is None:  # still running
                _kill_proc_tree(proc)
        except Exception:
            pass
    return len(procs)


# Set by the runner while a job is processing. Models run SEQUENTIALLY on the
# single 8GB GPU, so on-demand model calls (voice preview / clone warm / prewarm)
# must NOT run concurrently with a job — they'd thrash the GPU and make the job's
# TTS/whisper/SDXL time out. While busy, those endpoints return 409 instead.
_model_busy = False


def set_model_busy(v: bool) -> None:
    global _model_busy
    _model_busy = bool(v)


def model_busy() -> bool:
    return _model_busy


def _forward_progress(prog_path: str, cb, last):
    """Read the worker's progress file; forward to cb only when it changed."""
    try:
        with open(prog_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return last  # not written yet, or mid-write — try again next tick
    key = (data.get("pct"), data.get("msg"))
    if key != last:
        try:
            cb(int(data.get("pct") or 0), str(data.get("msg") or ""))
        except Exception:
            pass
    return key


# The F5/torch CUDA stack on this machine intermittently fails to load a GPU
# library at process start — e.g. "Could not load symbol cudnnGetLibConfig.
# Error code 127" — and the SAME invocation succeeds when re-run in a fresh
# process. It is a transient DLL/CUDA-init flake, not a real fault, so we retry
# the worker in a brand-new process (a fresh process re-initializes CUDA cleanly,
# which is exactly why the owner's manual retry worked).
#
# Match on the SIGNATURES of such load failures only. We deliberately do NOT match
# genuine input/data errors (missing ref file, empty ref_text, OOM, etc.) — those
# must fail fast with their real message instead of being retried 3x.
_TRANSIENT_LOAD_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"cudnngetlibconfig",                 # the exact observed cuDNN symbol
        r"could not load symbol",             # generic cuDNN/cuBLAS symbol-load miss
        r"error code 127",                    # the observed load error code
        r"could not load library",            # cudnn/cublas library load miss
        r"cudnn.*(load|init)",                # cuDNN load/init failures
        r"cublas.*(load|init)",               # cuBLAS load/init failures
        r"\bdll load failed\b",               # Windows native DLL load failure
        r"failed to load .*\.dll",            # ditto, explicit .dll
        r"cuda(?:_)? *(?:error)?.*initial",   # CUDA initialization errors
        r"cuda driver.*init",
        r"oserror: \[winerror 126\]",         # Windows: specified module not found
        r"importerror: dll load failed",
    )
]

# Substrings that mark a GENUINE (non-transient) error — if any appears, never
# retry even if a transient pattern also fuzzily matched. These are real,
# deterministic failures the caller must see immediately.
_NON_TRANSIENT_MARKERS = (
    "reference voice not found",
    "f5 model file not found",
    "requires a reference voice",
    "ref_text is empty",
    "could not transcribe reference voice",
)


def _is_transient_load_error(stderr: str) -> bool:
    """True if `stderr` looks like a transient GPU-library load flake (retry-worthy),
    and NOT a genuine input/data error (which must fail fast)."""
    if not stderr:
        return False
    low = stderr.lower()
    if any(m in low for m in _NON_TRANSIENT_MARKERS):
        return False
    return any(p.search(stderr) for p in _TRANSIENT_LOAD_PATTERNS)


# Well-known Windows NTSTATUS crash codes, for a readable log line. Anything else
# with the STATUS_SEVERITY_ERROR nibble (0xC…) is still recognized as a crash.
_NTSTATUS_NAMES = {
    0xC0000005: "ACCESS_VIOLATION",
    0xC0000017: "NO_MEMORY",
    0xC000001D: "ILLEGAL_INSTRUCTION",
    0xC0000094: "INTEGER_DIVIDE_BY_ZERO",
    0xC00000FD: "STACK_OVERFLOW",
    0xC0000135: "DLL_NOT_FOUND",
    0xC0000139: "ENTRYPOINT_NOT_FOUND",
    0xC0000142: "DLL_INIT_FAILED",
    0xC0000374: "HEAP_CORRUPTION",
    0xC0000409: "STACK_BUFFER_OVERRUN",
    0xC0000417: "INVALID_CRT_PARAMETER",
}


def _exit_code_label(returncode: int) -> str:
    """Human-readable form of a worker exit code, e.g.
    '-1073741819 (0xC0000005 ACCESS_VIOLATION)'. Windows reports native crashes as
    NTSTATUS values, which Python surfaces as a large negative int — unreadable in a
    log unless spelled out."""
    u = returncode & 0xFFFFFFFF
    name = _NTSTATUS_NAMES.get(u)
    if u >= 0xC0000000:
        return f"{returncode} (0x{u:08X}{' ' + name if name else ''})"
    return str(returncode)


def _is_native_crash(returncode: int, stderr: str) -> bool:
    """True if the worker died from a NATIVE crash (access violation, heap
    corruption, …) rather than raising a Python exception.

    Signature: an NTSTATUS-severity exit code (0xC……… — e.g. 0xC0000005) AND no
    Python traceback in stderr. Observed on the OmniVoice/F5 path: torch's CUDA/DLL
    init intermittently faults inside torch_cpu.dll, killing the process outright.
    Because the process never gets to raise, stderr holds only benign INFO lines, so
    `_is_transient_load_error` (text-matching) can't see it — yet it is the SAME
    transient flake and clears in a fresh process. Requiring "no traceback" keeps a
    genuine Python error that merely crashed on the way out from being retried.
    """
    if (returncode & 0xFFFFFFFF) < 0xC0000000:
        return False
    low = (stderr or "").lower()
    if "traceback (most recent call last)" in low:
        return False
    if any(m in low for m in _NON_TRANSIENT_MARKERS):
        return False
    return True


# NOTE: the DRM-block check that used to live here is now one rule ('src_drm') in
# worker_errors.RULES, alongside the other yt-dlp/source failure categories. The
# user-facing message is unchanged; see worker_errors.py for the full table.


# --- GPU contention: footage cuts in flight -----------------------------------
# The footage path runs TTS (a GPU model load) CONCURRENTLY with the source-clip cut
# pool (runner.py: _tts_fut + _run_cuts), and each cut is an h264_nvenc session holding
# VRAM. When the card is short, the TTS model load dies with a NATIVE fault, and the
# retry loop below used to fire all 3 attempts back-to-back INSIDE that same cut window
# (job 286: 17:47:44 / 17:48:21 / 17:48:51) — so every retry met the same contention and
# died identically. These counters let a retry WAIT for the cut pool to drain first,
# which is the state the retry actually needs. Only the FAILURE path waits: a healthy
# job never touches this and keeps the full cut/TTS overlap.
#
# No deadlock: cuts never wait on TTS (they run in the caller's foreground pool and
# _run_cuts drains every future before returning/raising), and the wait is bounded.
_cuts_in_flight = 0
_cuts_lock = threading.Lock()
_cuts_idle = threading.Event()
_cuts_idle.set()
# Ceiling on that wait (seconds). Sized for a long cut phase (job 286's ran ~4 min over
# 88 scenes); on expiry we retry anyway rather than hang. 0 = don't wait at all.
CF_RETRY_WAIT_CUTS_S = float(os.getenv("CF_RETRY_WAIT_CUTS_S", "900"))


def _cut_begin() -> None:
    """Mark one footage cut (one NVENC session) as in flight."""
    global _cuts_in_flight
    with _cuts_lock:
        _cuts_in_flight += 1
        _cuts_idle.clear()


def _cut_end() -> None:
    """Mark one footage cut as finished; wakes _wait_for_cuts_idle when the last
    one drains. Must run in a `finally` so a FAILED cut still releases its slot."""
    global _cuts_in_flight
    with _cuts_lock:
        _cuts_in_flight = max(0, _cuts_in_flight - 1)
        if _cuts_in_flight == 0:
            _cuts_idle.set()


def _wait_for_cuts_idle(script: str) -> None:
    """Block until no footage cut is in flight (bounded by CF_RETRY_WAIT_CUTS_S).
    No-op when nothing is cutting — the normal case for every non-footage job."""
    if CF_RETRY_WAIT_CUTS_S <= 0:
        return
    with _cuts_lock:
        pending = _cuts_in_flight
    if pending <= 0:
        return
    log.info("[generate] %s retry waiting for %d footage cut(s) to drain "
             "(GPU contention; max %.0fs)", script, pending, CF_RETRY_WAIT_CUTS_S)
    t0 = time.time()
    drained = _cuts_idle.wait(timeout=CF_RETRY_WAIT_CUTS_S)
    if drained:
        log.info("[generate] %s retry proceeding — cuts drained after %.1fs",
                 script, time.time() - t0)
    else:
        log.warning("[generate] %s retry proceeding WITHOUT drained cuts "
                    "(waited %.0fs, still %d in flight)",
                    script, time.time() - t0, _cuts_in_flight)


def _run_cf_worker(script: str, payload: dict, timeout: int, retries: int = 0,
                   retry_backoff: float = 2.0) -> dict:
    """Run a cf-venv worker on a JSON payload and return its JSON result.

    The payload and result are passed as temp files (not stdin/stdout) so library
    logging or HF download progress can never corrupt the parsed output. If a
    progress callback is installed for this thread (set_progress_cb), the worker
    is given a progress file path which we poll while it runs and forward live.

    `retries` (>0) re-runs the worker in a FRESH process when it fails with a
    TRANSIENT GPU-library load error (e.g. cuDNN "Error code 127") OR with a NATIVE
    CRASH (access violation in torch_cpu.dll etc. — see _is_native_crash). Each
    retry is a clean subprocess, which is what actually clears the flake. Genuine
    errors (bad input, timeout, etc.) are never retried — they fail fast. Used for
    the F5-TTS/OmniVoice paths, whose CUDA init is the source of the flake.
    """
    attempts = max(1, retries + 1)
    last_err = ""
    last_kind = "load"
    for attempt in range(1, attempts + 1):
        try:
            return _run_cf_worker_once(script, payload, timeout)
        except _TransientWorkerLoadError as e:
            last_err = str(e)
            last_kind = getattr(e, "kind", "load")
            if attempt < attempts:
                log.warning(
                    "[generate] %s transient %s (attempt %d/%d), "
                    "retrying in a fresh process: %s",
                    script,
                    "NATIVE CRASH" if last_kind == "crash" else "GPU-load failure",
                    attempt, attempts, (last_err or "").strip()[-300:],
                )
                # Both failure shapes are GPU-init failures, so give the retry a
                # quieter card: wait out any concurrent footage cuts (NVENC sessions)
                # before spending the attempt. No-op unless cuts are in flight.
                _wait_for_cuts_idle(script)
                time.sleep(retry_backoff)
                continue
            # Out of retries. Keep the technical detail in the log (this is the only
            # copy now) and hand the owner a friendly Vietnamese sentence — the text
            # below lands verbatim in jobs.error and in the dashboard.
            log.error(
                "[generate] %s GPU flake exhausted: kind=%s attempts=%d last_err=%s",
                script, last_kind, attempts, (last_err or "").strip()[-1500:] or "unknown",
            )
            raise HTTPException(503, gpu_flake_message(script, last_kind, attempts))
    # Unreachable, but keeps the type checker happy.
    raise HTTPException(500, f"{script}: failed ({last_err[-300:]})")


class _TransientWorkerLoadError(RuntimeError):
    """Raised internally when a worker subprocess fails with a transient GPU-library
    load error that is worth retrying in a fresh process.

    `kind` distinguishes the two observed shapes so the final message can name the
    right one: 'load' = a Python-level load error whose text matched
    _TRANSIENT_LOAD_PATTERNS; 'crash' = a native crash (NTSTATUS exit code, no
    traceback) detected by _is_native_crash."""

    def __init__(self, message: str, kind: str = "load"):
        super().__init__(message)
        self.kind = kind


def _run_cf_worker_once(script: str, payload: dict, timeout: int) -> dict:
    """One attempt at running a cf-venv worker (one subprocess). Raises
    _TransientWorkerLoadError on a retry-worthy GPU-load flake, HTTPException on
    everything else (genuine errors / timeout)."""
    worker = os.path.join(WORKERS_DIR, script)
    cb = getattr(_progress_local, "cb", None)
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, "in.json")
        out_path = os.path.join(td, "out.json")
        prog_path = os.path.join(td, "progress.json")
        if cb:
            payload = {**payload, "progressFile": prog_path}
        with open(in_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

        # Models are already downloaded, so skip the per-load HF Hub network
        # check (shaves seconds off each cold model load). Override by setting
        # HF_HUB_OFFLINE=0 in the environment if you need to fetch a new model.
        env = os.environ.copy()
        env.setdefault("HF_HUB_OFFLINE", "1")
        env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        # F5-TTS needs these: PYTHONUTF8 (Vietnamese crashes without it), ffmpeg on
        # PATH (for our 24k→48k resample), and the symlink-warning suppressed. Set
        # for every worker — harmless to VieNeu/whisper, required for F5.
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        env.setdefault("FFMPEG_BIN", os.getenv("FFMPEG_BIN", "ffmpeg"))
        env.setdefault("FFPROBE_BIN", os.getenv("FFPROBE_BIN", "ffprobe"))
        if _FFMPEG_DIR_ENV and os.path.isdir(_FFMPEG_DIR_ENV):
            env["PATH"] = _FFMPEG_DIR_ENV + os.pathsep + env.get("PATH", "")
        # CTranslate2 (faster-whisper on CUDA) needs torch's bundled cuDNN9/cuBLAS
        # DLLs discoverable. Prepend torch/lib to PATH and hand the worker the dir
        # via CF_TORCH_LIB so it can os.add_dll_directory() before importing CT2.
        if _CF_TORCH_LIB:
            env["PATH"] = _CF_TORCH_LIB + os.pathsep + env.get("PATH", "")
            env["CF_TORCH_LIB"] = _CF_TORCH_LIB

        # Redirect the worker's stdout/stderr to FILES (not PIPE). With PIPE we'd
        # have to drain it continuously; since we only read after the wait loop, a
        # long, log-heavy worker (e.g. 69-scene TTS) would fill the ~64KB OS pipe
        # buffer and DEADLOCK. Files have no such limit.
        err_path = os.path.join(td, "stderr.txt")
        f_err = open(err_path, "wb")
        worker_pid = None
        _kill_job = None
        proc = None
        try:
            try:
                proc = subprocess.Popen(
                    [CF_VENV_PYTHON, worker, in_path, out_path],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=f_err,
                    env=env,
                )
            except FileNotFoundError:
                raise HTTPException(500, f"cf-venv Python not found: {CF_VENV_PYTHON} (set CF_VENV_PYTHON in .env)")

            # Track this worker so /api/system can scope RAM/VRAM to it.
            worker_pid = proc.pid
            _register_worker_pid(worker_pid)
            # Register for immediate-kill on POST /stop (download/ingest/whisper/TTS
            # all run through here). The worker's own subprocesses (yt-dlp/ffmpeg) are
            # grandchildren of this Popen, so the tree-kill reaps them too.
            _kill_job = _register_job_proc(proc)

            deadline = time.time() + timeout
            last = None
            while True:
                try:
                    proc.wait(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    if cb:
                        last = _forward_progress(prog_path, cb, last)
                    if time.time() > deadline:
                        proc.kill()
                        proc.wait()
                        # Log the technical form; show the owner a friendly one.
                        log.error("[generate] %s timed out after %ss", script, timeout)
                        raise HTTPException(504, worker_timeout_message(script, timeout))
            if cb:
                _forward_progress(prog_path, cb, last)  # flush the final value
        finally:
            if worker_pid is not None:
                _unregister_worker_pid(worker_pid)
            if proc is not None:
                _unregister_job_proc(_kill_job, proc)
            f_err.close()

        if proc.returncode != 0 or not os.path.exists(out_path):
            try:
                with open(err_path, "rb") as f:
                    err_txt = f.read().decode("utf-8", "replace")
            except OSError:
                err_txt = ""
            rc = proc.returncode
            rc_label = _exit_code_label(rc)
            # ALWAYS log the exit code on failure. A native crash leaves NO traceback
            # in stderr, so without this line the api.log shows only benign INFO
            # output and the real cause (e.g. 0xC0000005 in torch_cpu.dll) is only
            # findable in the Windows Event Log. Also note when out.json is missing.
            # Keep a LONG tail here: this log line is now the ONLY place the raw
            # traceback survives (jobs.error gets a friendly Vietnamese sentence
            # instead), so it must stay diagnostic enough for a real bug report.
            log.error(
                "[generate] %s FAILED: exit=%s out.json=%s stderr_tail=%s",
                script, rc_label,
                "present" if os.path.exists(out_path) else "MISSING",
                (err_txt.strip()[-2000:] or "<empty>"),
            )
            # --- retry-worthy GPU flakes first (control flow, not messaging) ----
            # A transient GPU-library load flake (cuDNN "Error code 127" etc.) is
            # retry-worthy in a fresh process — signal the retry loop.
            if _is_transient_load_error(err_txt):
                raise _TransientWorkerLoadError(
                    err_txt.strip()[-800:] or "transient GPU-load failure", kind="load")
            # Same flake, harsher shape: the process was KILLED by a native fault
            # (NTSTATUS exit code, no traceback) so it never got to print anything
            # matchable. Retry it in a fresh process too — that is what clears it.
            if _is_native_crash(rc, err_txt):
                raise _TransientWorkerLoadError(
                    f"native crash, exit={rc_label}; "
                    f"stderr tail: {err_txt.strip()[-500:] or '<empty>'}",
                    kind="crash",
                )
            # --- everything else: friendly, user-facing Vietnamese --------------
            # worker_errors.py classifies the stderr (yt-dlp network/CDN timeouts,
            # DRM blocks, private/removed/geo-blocked sources, disk full, ...) into
            # a short actionable sentence. This exception's detail is what lands in
            # jobs.error and is rendered verbatim by the dashboard, so it must NEVER
            # be a raw traceback. The full stderr is in the log line above.
            status, friendly, code = friendly_worker_error(script, rc, err_txt)
            log.error("[generate] %s error category=%s -> %s", script, code, friendly)
            raise HTTPException(status, friendly)

        # Persist the worker's diagnostic stderr markers to the API log even on
        # SUCCESS. The whisper worker prints "[align] scene=N backend=ctc|whisper"
        # and "[ctc-align] ... failed (...)" to stderr; those lines are the record
        # of which caption-alignment path each scene took, so we surface them here
        # (they were previously only read on failure). Read UTF-8/replace — worker
        # stderr can contain Vietnamese and Windows' default cp1252 would raise.
        # Only marker lines are emitted; tqdm/model-load noise is skipped.
        try:
            with open(err_path, "rb") as f:
                _err_txt = f.read().decode("utf-8", "replace")
            for _ln in _err_txt.splitlines():
                _ln = _ln.strip()
                if _ln.startswith(("[align]", "[ctc-align]", "[gap-shape]")):
                    log.info("%s: %s", script, _ln)
        except OSError:
            pass

        with open(out_path, encoding="utf-8") as f:
            return json.load(f)


def _page_audio_dir(page: str | None) -> str:
    """Default audio output dir for a page: <CONTENT_OUTPUT_ROOT>/<page>/audio."""
    return os.path.join(CONTENT_OUTPUT_ROOT, page or "default", "audio")


def _page_voice_dir(page: str | None) -> str:
    """LEGACY per-page reference-voice dir: <CONTENT_OUTPUT_ROOT>/<page>/voice.

    Kept only for the ephemeral custom-text preview output dir (a non-cached,
    throwaway synth location). Cloned voices NO LONGER live here — they are shared
    across all pages in SHARED_VOICE_DIR. See _shared_voice_dir below.
    """
    return os.path.join(CONTENT_OUTPUT_ROOT, page or "default", "voice")


# Cloned reference voices are SHARED by every page (current and future). They live
# in one place — <CONTENT_OUTPUT_ROOT>/_voices — instead of per-page so any page
# can use any clone. Subcaches mirror the old per-page layout under this dir:
#   _voices/<name>.wav            the clone reference clips
#   _voices/_reftext/<name>.txt   F5 ref_text sidecars (fingerprinted)
#   _voices/_previews/clone_*.wav cached sample previews
# Preset previews stay page-independent in CONTENT_OUTPUT_ROOT/_voice_previews.
SHARED_VOICE_DIR = os.path.join(CONTENT_OUTPUT_ROOT, "_voices")


def _shared_voice_dir() -> str:
    """The one shared dir holding every page's cloned reference voices."""
    return SHARED_VOICE_DIR


# VieNeu ships its preset voices in this JSON; we read it directly (no model load).
VIENEU_VOICES_JSON = os.getenv(
    "VIENEU_VOICES_JSON",
    r"E:\Installed\cf-venv\Lib\site-packages\vieneu\assets\voices_v3_turbo.json",
)
_AUDIO_EXTS = (".wav", ".flac", ".ogg", ".mp3")
_PREVIEW_TEXT = "Xin chào, đây là giọng đọc mẫu cho kênh của bạn."

# Clone-engine key -> short display name, baked into a new clone's saved name as
# a suffix. Existing (suffix-less) clone files are never renamed.
_CLONE_MODEL_SHORT = {
    "f5-tts": "F5-TTS",
    "vieneu": "VieNeu",
    "omnivoice": "OmniVoice",
    "xtts-v2": "XTTS-v2",
    "openvoice-v2": "OpenVoice v2",
    "gpt-sovits": "GPT-SoVITS",
}

# Engine keys actually wired to a TTS code path in tts_worker.py. Anything else
# selected in the Studio is rejected with a clear message (not a 500).
_TTS_ENGINES_IMPLEMENTED = {"vieneu", "f5-tts", "omnivoice"}

# FFmpeg bin dir — F5-TTS (and our resample step) need ffmpeg on PATH inside the
# worker. Derived from FFMPEG_BIN so it stays in lockstep with the .env path.
_FFMPEG_DIR_ENV = os.path.dirname(os.getenv("FFMPEG_BIN", "")) if os.getenv("FFMPEG_BIN") else ""


def _engine_from_clone_name(ref_audio: str | None) -> str:
    """Derive the TTS engine from a cloned voice's baked filename suffix.

    upload_voice saves a clone as "<name> - <ShortName>" (e.g. "… - F5-TTS"),
    where ShortName comes from _CLONE_MODEL_SHORT. "… - VieNeu" maps to vieneu.
    Legacy clones (no suffix) also fall back to vieneu for backward compat —
    they were recorded before F5 became the project default.
    No ref_audio → VieNeu preset path (F5-TTS requires a reference wav).
    Returns a worker engine key (e.g. "f5-tts").
    """
    if not ref_audio:
        return "vieneu"
    stem = os.path.splitext(os.path.basename(ref_audio))[0]
    # Match the longest short-name suffix (case-insensitive) for robustness.
    low = stem.lower()
    best_key, best_len = "vieneu", -1
    for key, short in _CLONE_MODEL_SHORT.items():
        suffix = " - " + short.lower()
        if low.endswith(suffix) and len(suffix) > best_len:
            best_key, best_len = key, len(suffix)
    return best_key


def _normalize_engine(engine: str | None, ref_audio: str | None) -> str:
    """Resolve the effective engine: an explicit `engine` wins; otherwise derive
    it from the clone filename suffix. Reject not-yet-implemented engines."""
    eff = (engine or "").strip().lower() or _engine_from_clone_name(ref_audio)
    if eff not in _TTS_ENGINES_IMPLEMENTED:
        short = _CLONE_MODEL_SHORT.get(eff, eff)
        raise HTTPException(
            422,
            f"Voice engine '{short}' chưa được hỗ trợ — hiện chỉ có VieNeu, F5-TTS và OmniVoice.",
        )
    # Make the engine choice visible at the start of every synth (the first thing
    # you want when a clone misbehaves: which engine + which ref was actually used).
    log.info(
        "[generate] TTS engine=%s (requested=%r, ref=%s)",
        eff, engine, os.path.basename(ref_audio) if ref_audio else None,
    )
    return eff

# Reference voices are trimmed+normalized on upload. VieNeu clones best from a
# short, clean sample; long references make it ramble (uneven output length).
REF_TRIM_SEC = int(os.getenv("REF_TRIM_SEC", "12"))


def _preview_cache_path(page: str | None, voice: str | None, ref_audio: str | None) -> str:
    """Where a voice's sample preview is cached.

    Presets are page-independent → one shared dir (generate once, reuse for all
    pages). Cloned voices are now ALSO shared across pages → under the shared
    voice dir's _previews. The `page` arg is unused for clones (kept for the
    preset signature symmetry).
    """
    if ref_audio:
        d = os.path.join(_shared_voice_dir(), "_previews")
        key = "clone_" + os.path.splitext(os.path.basename(ref_audio))[0]
    else:
        d = os.path.join(CONTENT_OUTPUT_ROOT, "_voice_previews")
        key = "preset_" + (voice or "default")
    safe = "".join(c for c in key if c.isalnum() or c in (" ", "-", "_")).strip() or "voice"
    return os.path.join(d, safe + ".wav")


# Per-job LLM choice, carried on every TEXT script-gen request model. Both NULL = the
# claude-cli subscription path (today's only behavior). Mirrors jobs.llm_provider /
# jobs.llm_model, threaded exactly like `skipScriptCache` is. The two VISION calls do NOT
# get these fields — they stay hard-wired to Claude Code headless (see llm_gate's docstring).
class _LlmChoiceMixin(BaseModel):
    llmProvider: str | None = None   # claude-cli | gemini | openrouter (NULL = claude-cli)
    llmModel: str | None = None      # NULL = that provider's own default model


class ScriptRequest(_LlmChoiceMixin):
    topic: str
    durationSec: int = 60
    sceneCount: int | None = None


class Scene(BaseModel):
    scene: int
    narration: str
    image_prompt: str


def _build_prompt(topic: str, duration: int, scenes: int) -> str:
    # Instructions in English (cheaper tokens); the narration OUTPUT stays Vietnamese.
    return (
        "You are a scriptwriter for a short-form video channel that tells/explains "
        "game stories. Write in a natural, engaging Vietnamese narration voice with a "
        "strong hook in the opening line.\n"
        f"Topic: {topic}\n"
        f"Write a script about {duration} seconds long, split into exactly {scenes} scenes.\n"
        "Each scene has: 'narration' = a short spoken line — write it in VIETNAMESE (the "
        f"channel's own voice), do NOT output English narration ({_VI_FULL_TRANSLATION_RULE}); 'image_prompt' = a 9:16 "
        "vertical frame description in ENGLISH for the SDXL model (detailed, cinematic).\n"
        "Return ONLY a single valid JSON array, with no markdown or explanation. "
        'Each element: {"scene": <number starting at 1>, "narration": "<tiếng Việt>", '
        '"image_prompt": "<English prompt>"}.'
    )


def _extract_json_array(text: str) -> list:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end != -1:
        text = text[start : end + 1]
    return json.loads(text)


def _kill_proc_tree(proc: subprocess.Popen) -> None:
    """Hard-kill a claude headless subprocess AND its descendants.

    `claude.exe` is a Node launcher: subprocess.run(timeout=) / proc.kill() reaps
    only the direct child, so a stalled run can leave orphaned node grandchildren
    holding the API/stream connection. On Windows we use `taskkill /F /T` (kills the
    whole PID tree) — the same dependency-free, native approach main.py already uses
    (this API intentionally does NOT depend on psutil). Elsewhere we fall back to a
    plain kill. Best-effort: any failure here must not mask the timeout we report.
    """
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _read_stream_json_result(proc: subprocess.Popen, timeout: int) -> tuple[str | None, dict | None]:
    """Read a `--output-format stream-json --verbose` event stream from proc.stdout
    incrementally, enforcing a wall-clock `timeout` on OUR side, and return
    (result_text, result_event).

    stream-json in `-p` mode emits newline-delimited JSON events; the terminal
    `{"type":"result", ...}` event carries the final answer in `result` (and the
    `subtype`/`errors` on failure). We read it line by line in a background reader
    thread so a STALL (no bytes for `timeout` seconds) is detectable — proc.stdout
    is blocking, so we cannot just poll it with a deadline on the main thread without
    risking an indefinite hang on a wedged process.

    On timeout the caller hard-kills the process tree. Non-JSON lines are ignored
    (robustness against any stray banner). If the stream ends with NO result event,
    we return (None, None) and the caller treats it as an error.
    """
    result_event: dict | None = None
    reader_error: list[Exception] = []

    def _reader():
        try:
            for line in proc.stdout:  # blocking; ends when the pipe closes
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue  # ignore non-JSON noise
                if isinstance(evt, dict) and evt.get("type") == "result":
                    nonlocal result_event
                    result_event = evt
                    # keep draining so the child can flush/exit cleanly
        except Exception as e:  # pragma: no cover - defensive
            reader_error.append(e)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        # Stalled past the per-batch deadline — signal a timeout to the caller.
        raise HTTPException(504, f"Claude Code timed out after {timeout}s")
    if reader_error:
        raise HTTPException(500, f"Claude Code stream read failed: {reader_error[0]}")

    if result_event is None:
        return None, None
    return result_event.get("result"), result_event


def _claude_result(proc: subprocess.Popen, timeout: int, *,
                   label: str = "Claude Code", expect: str = "array"):
    """Read ONE headless `claude -p` process to completion and return its answer.

    This is the SINGLE read/reap/error-classify body shared by all three spawn sites
    (text script-gen, visual-explain vision, cover-prompt vision). It used to be copied
    three times, which meant any fix to the error taxonomy had to be made three times.
    The three call sites still build their OWN Popen argv (`--tools ""` vs
    `--tools "Read" --add-dir`, different --max-turns / timeouts) — only what happens
    AFTER the spawn is shared.

    Sequence (unchanged from the copies it replaces):
      1. stream the newline-delimited events with a wall-clock deadline
         (`_read_stream_json_result`); on its 504 stall, hard-kill the process TREE and
         reap it before re-raising, so no Node grandchild is orphaned;
      2. reap the process for the real exit code + stderr (stdout is already drained);
      3. classify the outcome from the result event's subtype/is_error plus the exit code;
      4. return the answer — parsed JSON array (`expect="array"`) or the raw result text
         (`expect="text"`, the vision sites parse it themselves).

    ERROR TAXONOMY (preserved EXACTLY — the retry path depends on it):
      - `error_max_turns`             -> HTTPException(504)  RETRYABLE: a fresh process
                                        gets a fresh turn budget, so `_run_claude_script`'s
                                        504-retry re-runs it (see `_is_retryable`).
      - any other non-success subtype
        / is_error / non-zero exit    -> HTTPException(500)  genuine error, fails fast.
      - stream ended, no result event -> HTTPException(502)
      - result text is not valid JSON -> HTTPException(502)  (`expect="array"` only)

    `label` only prefixes the messages ("Claude Code" for text, "Claude vision" for the
    vision sites) so the surfaced detail stays byte-identical to the copies.
    """
    try:
        result_text, result_event = _read_stream_json_result(proc, timeout)
    except HTTPException as e:
        if e.status_code == 504:
            _kill_proc_tree(proc)
            try:
                proc.wait(timeout=5)  # reap so no handle leaks
            except Exception:
                pass
        raise

    # The stream has ended (pipe closed). Reap the process to get the real exit code
    # and any stderr; the reader thread already drained stdout.
    try:
        _, stderr = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        _kill_proc_tree(proc)
        try:
            _, stderr = proc.communicate(timeout=5)
        except Exception:
            stderr = ""
    rc = proc.returncode

    # Surface a real error from either the result event's subtype OR a non-zero exit.
    subtype = result_event.get("subtype") if result_event else None
    is_error = (result_event.get("is_error") if result_event else False) or rc not in (0, None)
    if subtype not in (None, "success") or is_error:
        errs = result_event.get("errors") if result_event else None
        errors_joined = "; ".join(errs) if isinstance(errs, list) else (errs or "")
        if subtype == "error_max_turns":
            # Retryable via the existing mechanism: raise 504 so _run_claude_script's
            # 504-retry path re-runs in a fresh process; carry the real subtype so a
            # persisted failure stays diagnosable.
            raise HTTPException(
                504,
                f"{label} failed (exit {rc}, error_max_turns): "
                f"{errors_joined or 'Reached maximum number of turns'}",
            )
        raise HTTPException(
            500,
            f"{label} failed (exit {rc}, {subtype or 'no-result'}): "
            f"{errors_joined or (stderr or '')[:500]}",
        )

    if result_text is None:
        # Stream ended without a result event and without a non-zero exit — treat as
        # an error so we never silently return an empty/partial script.
        raise HTTPException(
            502, f"{label} stream ended with no result event (exit {rc}): {(stderr or '')[:300]}"
        )

    if expect == "text":
        return result_text
    try:
        return _extract_json_array(result_text)
    except json.JSONDecodeError:
        raise HTTPException(502, f"Could not parse script JSON: {result_text[:300]}")


def _run_claude_script_once(prompt: str, timeout: int, model: str | None = None) -> list:
    """One attempt at Claude Code headless on a prompt -> parsed scene JSON array.

    Billed to the subscription (NOT the paid API). stdin is closed so it does not
    wait for piped input.

    Invocation (validated in the diag for this exact environment; keeps subscription
    auth intact):
        claude -p <prompt> --model sonnet --max-turns 1 \
               --tools "" --strict-mcp-config \
               --system-prompt <terse JSON-only role> \
               --output-format stream-json --verbose
    - `--tools ""` drops the ~20k-token built-in tool schema (script gen needs none).
    - `--strict-mcp-config` ignores any machine-level MCP servers (cheap insurance).
    - `--system-prompt` replaces Claude Code's large default system prompt.
    - `--output-format stream-json` (REQUIRES `--verbose` in -p mode, else the CLI
      errors) streams newline-delimited events so a stall is observable; it does NOT
      reduce total decode time — it removes the all-or-nothing 600s block.
    - `--bare` is CONFIRMED BROKEN here (forces ANTHROPIC_API_KEY / breaks the
      borrowed subscription) — deliberately NOT used.

    The timeout is the PER-BATCH wall-clock ceiling, enforced HERE while reading the
    stream. On timeout we hard-kill the whole process tree (Node launcher +
    grandchildren) so nothing is orphaned, then raise HTTPException(504) — the
    caller's retry loop decides whether to re-run.

    `model` is the claude-cli leg's per-job model override, threaded in by
    `llm_gate.run_llm_json`. None (and the resolved default, which IS SCRIPT_GEN_MODEL)
    produce the IDENTICAL argv this function built before the provider gate existed —
    that equality is the whole point and is asserted by
    test_default_provider_argv_matches_pre_gate.
    """
    try:
        proc = subprocess.Popen(
            [CLAUDE_BIN, "-p", prompt, "--model", (model or SCRIPT_GEN_MODEL),
             "--max-turns", str(SCRIPT_GEN_MAX_TURNS),
             "--tools", "",
             "--strict-mcp-config",
             "--system-prompt", SCRIPT_GEN_SYSTEM_PROMPT,
             "--output-format", "stream-json", "--verbose"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        raise HTTPException(500, f"Claude binary not found: {CLAUDE_BIN} (set CLAUDE_BIN in .env)")

    # Register the claude -p process tree for immediate-kill on POST /stop. Without
    # this a stop during script-gen would wait the full per-batch timeout. Cleared in
    # the finally below (runs whether the call succeeds, errors, or times out).
    _kill_job = _register_job_proc(proc)
    try:
        # Shared read/reap/error-classify body (see _claude_result). Returns the parsed
        # JSON scene array; raises 504 (retryable) / 500 / 502 exactly as before.
        return _claude_result(proc, timeout, label="Claude Code", expect="array")
    finally:
        _unregister_job_proc(_kill_job, proc)


def _run_claude_vision_script(prompt: str, frames_dir: str, job_id: int | None,
                              timeout: int) -> str:
    """Run ONE MULTI-TURN Claude Code headless call that can SEE images, and return the
    final result TEXT (the caller extracts the JSON array from it).

    DIFFERS from _run_claude_script_once (which disables all tools for a single
    mechanical JSON emission): the no-speech visual-explain fallback needs Claude to
    actually LOOK at the sampled frames, so this ENABLES the Read tool and grants read
    access to `frames_dir` via --add-dir. The call is therefore multi-turn (one Read
    round-trip per frame, then the JSON emission), which is why --max-turns is generous
    and the timeout is VISUAL_EXPLAIN_TIMEOUT (not the per-batch script-gen budget).

    Flags (EMPIRICALLY VERIFIED against the installed CLI 2.1.200 on this machine —
    a minimal `-p` call with exactly these flags successfully Read a JPEG and returned
    a correct description, is_error=false, in the DEFAULT permission mode with no
    interactive prompt):
        claude -p <prompt> --model sonnet --max-turns 40 \
               --tools "Read" --add-dir <frames_dir> --strict-mcp-config \
               --system-prompt <terse JSON-only role> \
               --output-format stream-json --verbose
    - `--tools "Read"` grants ONLY the read-only Read tool (no Bash/Edit/Write/etc.) —
      the minimum needed to open the frame images; keeps the call non-destructive.
    - `--add-dir <frames_dir>` allows tool access to the per-job frames directory so the
      absolute frame paths in the prompt resolve without a permission prompt.
    - `--strict-mcp-config` ignores any machine-level MCP servers (cheap insurance).
    - stream-json (REQUIRES --verbose in -p mode) streams events so a stall is
      observable; the stream now also carries tool_use / tool_result events, but the
      terminal {"type":"result"} event still carries the final answer as before.

    On timeout we hard-kill the whole process tree and raise HTTPException(504) (the
    caller retries once in a fresh process). stdin is closed (DEVNULL) so the CLI does
    not wait for piped input; output is decoded utf-8/replace (Windows cp1252 default
    corrupts Vietnamese)."""
    try:
        proc = subprocess.Popen(
            [CLAUDE_BIN, "-p", prompt, "--model", SCRIPT_GEN_MODEL,
             "--max-turns", str(VISUAL_EXPLAIN_MAX_TURNS),
             "--tools", "Read",
             "--add-dir", frames_dir,
             "--strict-mcp-config",
             "--system-prompt", SCRIPT_GEN_SYSTEM_PROMPT,
             "--output-format", "stream-json", "--verbose"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        raise HTTPException(500, f"Claude binary not found: {CLAUDE_BIN} (set CLAUDE_BIN in .env)")

    # Register the claude -p process tree for immediate-kill on POST /stop, exactly like
    # the text script-gen path. Cleared in the finally (runs on success/error/timeout).
    _kill_job = _register_job_proc(proc)
    try:
        # Shared read/reap/error-classify body (see _claude_result). expect="text": the
        # caller (_ve_parse_and_clean) extracts + validates the JSON itself, and the
        # error taxonomy is identical to the text path (error_max_turns -> retryable 504).
        result_text = _claude_result(proc, timeout, label="Claude vision", expect="text")
        log.info("[generate] job %s visual-explain vision call OK (%d chars of result text)",
                 job_id, len(result_text))
        return result_text
    finally:
        _unregister_job_proc(_kill_job, proc)


# --- Vietnamese spelling fixes on Claude's script output ------------------------------
# Claude occasionally emits a MISSPELLED Vietnamese word. This is categorically different
# from a word_improve.md respelling: a say_as only changes the SPOKEN text and deliberately
# keeps the caption as written, whereas a misspelling is simply WRONG in both places — the
# viewer must not READ it either. So it is fixed on the RAW script text, right where Claude's
# output is parsed, before anything saves / captions / voices it.
#
# Fixing the raw narration also re-keys the TTS cache for free: the cache key hashes the
# narration itself, so a corrected line is a cache MISS and gets re-synthesized. No
# _*_VOICING_VERSION bump is needed for entries added here (unlike a worker text-prep change).
#
# Whole-word and case-insensitive; the replacement copies the ORIGINAL's leading capital so a
# sentence-initial occurrence stays capitalized. Internal whitespace in a multi-word term is
# matched flexibly (\s+), which also catches a line-wrapped occurrence.
_VI_SPELLING_FIXES = {
    # owner-reported: "chính muồi" is not a Vietnamese word — the correct form is "chín mùi"
    "chính muồi": "chín mùi",
}
_VI_SPELLING_RE = re.compile(
    "|".join(
        r"\b" + r"\s+".join(re.escape(w) for w in term.split()) + r"\b"
        # longest first, so an entry that is a prefix of another can never shadow it
        for term in sorted(_VI_SPELLING_FIXES, key=len, reverse=True)
    ),
    re.IGNORECASE,
) if _VI_SPELLING_FIXES else None


def _fix_vi_spelling(text: str) -> str:
    """Apply _VI_SPELLING_FIXES to one string, preserving the original capitalization."""
    if not text or _VI_SPELLING_RE is None:
        return text

    def _sub(m: "re.Match") -> str:
        matched = m.group(0)
        fixed = _VI_SPELLING_FIXES[" ".join(matched.split()).lower()]
        if matched.isupper():          # "CHÍNH MUỒI" (emphasis) -> "CHÍN MÙI"
            return fixed.upper()
        if matched[:1].isupper():      # sentence-initial -> keep the leading capital
            return fixed[:1].upper() + fixed[1:]
        return fixed

    return _VI_SPELLING_RE.sub(_sub, text)


def _fix_vi_spelling_deep(obj):
    """Recursively apply _fix_vi_spelling to every string in a parsed script structure.

    Claude's script JSON differs per mode (scene arrays with 'narration', translate_full
    arrays with 'text_vi', single-string title arrays), so we walk the whole structure
    rather than naming fields — a new mode is covered automatically. Non-string leaves
    (scene numbers, sourceStart/sourceEnd) are returned untouched."""
    if isinstance(obj, str):
        return _fix_vi_spelling(obj)
    if isinstance(obj, list):
        return [_fix_vi_spelling_deep(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _fix_vi_spelling_deep(v) for k, v in obj.items()}
    return obj


def _is_retryable(exc: BaseException) -> bool:
    """Should `_run_claude_script` re-run this failed call in a FRESH process?

    Single decision point for the retry policy. CURRENT policy — unchanged from the
    inline `if e.status_code != 504` it replaces: retry ONLY the 504 timeout. Script gen
    is one idempotent, side-effect-free prompt, so a transient stall (slow first-call
    bootstrap, wedged stream, exhausted turn budget remapped to 504 by `_claude_result`)
    is safely cleared by a fresh process. Everything else — bad binary, non-zero exit,
    unparseable JSON — is a GENUINE error that a retry cannot fix, so it fails fast with
    its real message.

    HTTP PROVIDERS (llm_gate): an `LLMError` carries its OWN `retryable` flag, decided by
    `llm_gate.is_retryable` from the real HTTP status — 429 / rate-limit, transient 5xx,
    connection reset and read timeout are retryable; 400/401/403/404/413 contract errors
    are not. That classification lives in llm_gate (next to the request that produced the
    status) rather than being re-derived here, so there is still exactly ONE decision per
    leg and no second parallel taxonomy. LLMError subclasses HTTPException, so it is
    checked FIRST — otherwise its status_code would be re-judged by the CLI's 504 rule.

    SCRIPT_GEN_RETRIES / SCRIPT_GEN_RETRY_BACKOFF are calibrated on measured `claude -p`
    runs and are deliberately NOT changed by the provider gate — do not tune them as a
    side effect of adding a provider.
    """
    if isinstance(exc, llm_gate.LLMError):
        return llm_gate.is_retryable(exc)
    return isinstance(exc, HTTPException) and exc.status_code == 504


def _llm_kwargs(provider: str | None, model: str | None) -> dict:
    """Provider/model kwargs for `_run_claude_script`, or {} when the job made NO choice.

    Why the conditional instead of always passing them: with no choice the call shape
    stays EXACTLY `_run_claude_script(prompt, timeout[, cache_parts, batch_idx,
    force_regen])` — the same arity every existing monkeypatch stub in the test-suite
    mirrors, and the same arity `_run_batches_parallel` submits. So "provider unset" is
    indistinguishable from before the gate existed at the call-shape level too, not just
    at the argv/cache-key level.
    """
    if not (provider or "").strip() and not (model or "").strip():
        return {}
    return {"provider": (provider or "").strip() or None,
            "model": (model or "").strip() or None}


def _llm_used(provider: str | None, model: str | None) -> dict:
    """{"llmProvider": ..., "llmModel": ...} — which backend ACTUALLY served this
    endpoint's script-gen calls, for the response body and for videos.llm_provider_used /
    videos.llm_model_used.

    Today this is simply the RESOLVED request choice, and that is exactly right BECAUSE
    there is no cross-provider fallback: whatever was resolved is what ran, or the call
    raised. `llm_gate.LLMResult` already reports the served leg per call; if a future
    phase adds a fallback ladder, this helper must be replaced by aggregating those
    per-call results instead of re-deriving them from the request.
    """
    prov, mod = llm_gate.resolve(provider, model)
    return {"llmProvider": prov, "llmModel": mod}


def _run_claude_script(prompt: str, timeout: int = 300,
                       cache_parts: dict | None = None, batch_idx: int = 0,
                       force_regen: bool = False,
                       provider: str | None = None, model: str | None = None) -> list:
    """Run ONE text script-gen prompt on the job's chosen LLM, with a bounded number of
    retries on a RETRYABLE failure only.

    This is still the single retry/cache/spell-fix/logging shell for every text feature;
    the provider gate was inserted BELOW it (`llm_gate.run_llm_json`) precisely so all of
    that stays shared between the claude-cli leg and the HTTP legs — one retry policy, one
    disk cache, one spell-fix pass, one set of `[claude]`/`[script]` log tags.

    Script gen is a single idempotent, side-effect-free prompt, so a transient stall (a
    slow first-call bootstrap, a stuck stream, a provider 429) is safely cleared by
    re-running. A genuine error (bad binary, non-zero exit, unparseable JSON, bad request,
    missing API key) fails fast with its real message. The retry/fail-fast decision lives
    in ONE place, `_is_retryable`. On the final timeout we raise a Vietnamese, user-facing
    message so the failed job row reads clearly in the dashboard.

    `provider`/`model` (per-job, from jobs.llm_provider / jobs.llm_model): None on both =
    today's claude-cli path, byte-for-byte. A non-default provider that fails is NEVER
    silently replaced by another provider — the job fails naming what failed and why.

    `cache_parts` (footage/transform batches only): the batch-identity dict
    {edit_mode, source_transcript_window, word_budget, batch_index} used as the disk
    cache key — `_script_cache_key` additionally folds in the RESOLVED provider, the
    RESOLVED model and PROMPT_VERSION, so switching provider cannot serve the other
    provider's cached scenes. Topic/image jobs pass None (no cache).
    """
    # Resolve BEFORE the cache key so the key carries the concrete ids that will actually
    # serve the call (e.g. "gemini"/"gemini-flash-latest"), never the claude-cli default.
    _prov, _model = llm_gate.resolve(provider, model)
    cache_key = (_script_cache_key(cache_parts, provider=_prov, model=_model)
                 if cache_parts is not None else None)
    if cache_key is not None and not force_regen:
        cached = _script_cache_get(cache_key)
        if cached is not None:
            log.info(f"[script] cache HIT batch {batch_idx}")
            # Spell-fix the cached array too: entries written BEFORE a _VI_SPELLING_FIXES
            # addition still carry the typo, and a cache hit must never serve it.
            return _fix_vi_spelling_deep(cached)

    attempts = max(1, SCRIPT_GEN_RETRIES + 1)
    for attempt in range(1, attempts + 1):
        # Per-call timing for the observability log: a slow/failed run must be
        # diagnosable from api.log without re-instrumenting. The first prompt line
        # carries the batch's sub-window (footage batch "covering source X-Ys"), so we
        # log a short prefix to correlate which batch this call served.
        _hint = (prompt.split("\n", 1)[0] or "")[:80]
        _t0 = time.monotonic()
        log.info("[claude] script-gen call start (attempt %d/%d, timeout %ss, %s/%s) :: %s",
                 attempt, attempts, timeout, _prov, _model, _hint)
        try:
            # THE provider gate. claude-cli (the default) goes straight back into
            # _run_claude_script_once with the same argv; gemini/openrouter take the
            # httpx OpenAI-compatible leg. No cross-provider fallback lives inside.
            _res = llm_gate.run_llm_json(
                prompt, timeout=timeout, system_prompt=SCRIPT_GEN_SYSTEM_PROMPT,
                provider=_prov, model=_model)
            _r = _fix_vi_spelling_deep(_res.data)
            log.info("[claude] script-gen call done in %.1fs (attempt %d/%d, %d scenes)",
                     time.monotonic() - _t0, attempt, attempts, len(_r) if _r else 0)
            if cache_key is not None:
                _script_cache_put(cache_key, _r)
                log.info(f"[script] cache WRITE batch {batch_idx}")
            return _r
        except HTTPException as e:
            log.warning("[claude] script-gen call FAILED in %.1fs (attempt %d/%d, status %s)",
                        time.monotonic() - _t0, attempt, attempts, e.status_code)
            if not _is_retryable(e):
                raise  # genuine error — fail fast, do not retry
            if attempt < attempts:
                log.warning(
                    "[generate] Claude script gen timed out after %ss "
                    "(attempt %d/%d) — retrying in a fresh process",
                    timeout, attempt, attempts,
                )
                time.sleep(SCRIPT_GEN_RETRY_BACKOFF)
                continue
            # Out of retries — surface a clear Vietnamese message to the dashboard.
            # The default (claude-cli) message is kept VERBATIM: it is what the owner
            # already recognises in a failed job row, and a test pins it. A non-default
            # provider gets its own message that NAMES the provider/model and carries the
            # real reason, so a gemini/openrouter failure can never be mistaken for a
            # Claude timeout — and so it is obvious nothing silently served it instead.
            if _prov != llm_gate.PROVIDER_CLAUDE_CLI:
                raise HTTPException(
                    getattr(e, "status_code", 504),
                    f"Viết kịch bản thất bại với {_prov}/{_model} sau {attempts} lần thử: "
                    f"{getattr(e, 'detail', '') or e}. Không tự động đổi sang model khác — "
                    f"chọn lại model trong Studio hoặc thử lại.",
                )
            raise HTTPException(
                504,
                f"Viết kịch bản quá thời gian chờ ({timeout}s) sau {attempts} lần thử. "
                f"Claude Code chạy quá lâu hoặc bị treo — thử lại, hoặc tăng "
                f"SCRIPT_GEN_TIMEOUT trong .env nếu prompt dài.",
            )
    # Unreachable, but keeps the type checker satisfied.
    raise HTTPException(500, "Claude script gen failed unexpectedly")


# --- Facebook hashtags ------------------------------------------------------
# Strip accents so Vietnamese words become bare ASCII tokens: FB hashtags do not
# match diacritics/spaces well, so "giải thích" -> "giaithich".
_VI_ACCENT_MAP = str.maketrans(
    "àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ"
    "ÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴĐ",
    "aaaaaaaaaaaaaaaaaeeeeeeeeeeeiiiiiooooooooooooooooouuuuuuuuuuuyyyyyd"
    "AAAAAAAAAAAAAAAAAEEEEEEEEEEEIIIIIOOOOOOOOOOOOOOOOOUUUUUUUUUUUYYYYYD",
)


def _fb_tag_clean(raw) -> str:
    """Normalize ONE hashtag into a Facebook-valid token: strip accents, drop any
    character that is not a letter/digit/underscore, collapse to a single leading
    '#'. Returns "" for anything that reduces to empty (dropped by the caller)."""
    if not isinstance(raw, str):
        return ""
    t = raw.strip().translate(_VI_ACCENT_MAP)
    # Keep only letters/digits (and underscore); this removes spaces, punctuation,
    # emojis and the '#' itself, which we re-add as exactly one leading char.
    t = re.sub(r"[^0-9A-Za-z_]", "", t)
    return f"#{t}" if t else ""


# Hard cap on the number of Facebook hashtags we ever emit (owner rule: MAX 8).
_FB_TAGS_MAX = 8


def _brand_tag(page: str | None) -> str | None:
    """Brand/channel hashtag derived from the PAGE name: strip Vietnamese accents,
    drop non-alphanumerics, lowercase, single leading '#'
    (e.g. "Giải Thích Mọi Thứ" -> "#giaithichmoithu"). Reuses _fb_tag_clean so the
    result is guaranteed FB-valid. Returns None when page is None/empty or reduces to
    nothing (caller then skips brand enforcement)."""
    if not page or not str(page).strip():
        return None
    tag = _fb_tag_clean(str(page))
    if not tag:
        return None
    return tag.lower()


def _finalize_fb_tags(raw_tags, page: str | None = None) -> list[str]:
    """Normalize + dedupe hashtags, FORCE the page brand tag into the result (FIRST
    when missing), then hard-cap at _FB_TAGS_MAX (8) WITHOUT ever dropping the brand:
    brand + up to (8-1) other tags = 8. When `page` is None/empty, brand enforcement
    is skipped and the list is simply capped at 8. Every token is FB-valid (single
    '#', no spaces, no diacritics) because each passes through _fb_tag_clean."""
    seen: set[str] = set()
    tags: list[str] = []
    for raw in raw_tags or []:
        tag = _fb_tag_clean(raw)
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
    brand = _brand_tag(page)
    if brand:
        # Brand is protected: place it first, cap the OTHER tags at _FB_TAGS_MAX-1.
        others = [t for t in tags if t.lower() != brand]
        tags = [brand] + others[: _FB_TAGS_MAX - 1]
    else:
        tags = tags[:_FB_TAGS_MAX]
    return tags


def _fb_tags_fallback(title: str, page: str | None = None) -> list[str]:
    """Deterministic fallback when Claude fails: derive tags from the title tokens
    plus the page's evergreen niche tags, so the FE always receives something.
    Runs through _finalize_fb_tags so the fallback ALSO includes the brand tag and is
    capped at 8 (brand-protected)."""
    toks = re.findall(r"[0-9A-Za-zÀ-ỹ]+", title or "")
    seen: list[str] = []
    for w in toks:
        tag = _fb_tag_clean(w)
        if tag and len(tag) > 2 and tag.lower() not in {s.lower() for s in seen}:
            seen.append(tag)
    # Evergreen niche + broad-reach tags for the "Giải Thích Mọi Thứ" style page.
    for base in ("#giaithichmoithu", "#kienthuc", "#khoahoc", "#kienthucthuvi",
                 "#hieubiet", "#facts", "#science", "#learnontiktok"):
        if base.lower() not in {s.lower() for s in seen}:
            seen.append(base)
    return _finalize_fb_tags(seen, page)


def _generate_fb_tags(title: str, edit_mode: str | None = None,
                      page: str | None = None, content: str | None = None,
                      llm_provider: str | None = None,
                      llm_model: str | None = None) -> list[str]:
    """Generate Facebook-appropriate hashtags for a Vietnamese short video via the
    SAME Claude-headless mechanism as the title translator (`_run_claude_script`,
    the JSON-array contract). English instructions, Vietnamese content awareness.

    When `content` (the Vietnamese narration text) is provided, a trimmed excerpt is
    included in the prompt so the hashtags reflect the ACTUAL spoken content, not just
    the title. This is what the runner's auto-tag step passes after script-gen.

    NEVER raises: on any failure (empty title, Claude timeout/error, unparseable or
    empty output) it returns a small sensible fallback set derived from the title so
    the FE always gets something. The caller normalizes/dedupes the result.
    """
    t = (title or "").strip()
    if not t:
        return []
    page_hint = (page or "").strip()
    mode_hint = (edit_mode or "").strip()
    # Trim the narration excerpt so the prompt stays cheap (billed to the subscription)
    # while still giving Claude the real content to draw specific hashtags from.
    content_excerpt = " ".join((content or "").split())[:1500].strip()
    prompt = (
        "You generate Facebook hashtags for a SHORT VIDEO whose spoken content is in "
        "Vietnamese. Given the video TITLE/topic (and, when present, an EXCERPT of the "
        "actual Vietnamese narration) below, produce hashtags that maximize "
        "Facebook discovery.\n"
        "Rules (follow EXACTLY):\n"
        "- Output AT MOST 8 hashtags, ORDERED from most relevant/specific to the "
        "content first, then broader reach.\n"
        "- Each hashtag is a SINGLE token starting with '#', with NO spaces and NO "
        "punctuation inside.\n"
        "- Vietnamese hashtags MUST be written WITHOUT diacritics and WITHOUT spaces "
        "(e.g. #giaithichmoithu #khoahoc #kienthuc), because Facebook hashtags do not "
        "match spaces or diacritics.\n"
        "- ALWAYS include the channel/brand hashtag derived from the Page/channel name "
        "below (accents stripped, no spaces, e.g. \"Giải Thích Mọi Thứ\" -> "
        "#giaithichmoithu) as one of the tags.\n"
        "- Mix three kinds: (1) content-specific tags derived from the title AND the "
        "narration excerpt (the real subject matter/keywords it mentions), "
        "(2) niche/page tags for a Vietnamese explainer/knowledge/science channel, "
        "(3) a few broad-reach tags, and a few broad global English tags where natural "
        "(e.g. #facts #science).\n"
        "- No spammy, banned, or irrelevant tags. No duplicates.\n"
        "- Output ONLY a JSON array of hashtag strings. Example: "
        "[\"#giaithichmoithu\", \"#khoahoc\", \"#facts\"]. No other text.\n\n"
        f"Title: {t}\n"
    )
    if content_excerpt:
        prompt += f"Narration excerpt (Vietnamese): {content_excerpt}\n"
    if mode_hint:
        prompt += f"Edit mode: {mode_hint}\n"
    if page_hint:
        prompt += f"Page/channel: {page_hint}\n"
    try:
        arr = _run_claude_script(prompt, timeout=SCRIPT_GEN_TIMEOUT,
                                 **_llm_kwargs(llm_provider, llm_model))
    except Exception as e:  # noqa: BLE001 — never let a tag-gen failure crash the caller
        log.warning("[fbtags] generation failed, using fallback: %s", str(e)[:200])
        return _fb_tags_fallback(t, page)
    tags: list[str] = []
    if isinstance(arr, list):
        for item in arr:
            val = item.get("tag") if isinstance(item, dict) else item
            if isinstance(val, str):
                tags.append(val)
    if not tags:
        log.warning("[fbtags] unexpected/empty shape %r, using fallback", arr)
        return _fb_tags_fallback(t, page)
    # Normalize/dedupe, force the brand tag, and hard-cap at 8 (brand-protected).
    return _finalize_fb_tags(tags, page)


def _chunk_for_mode(edit_mode: str | None) -> int:
    """Per-mode chunk size: normalize (lowercase) the edit_mode and return its
    SCRIPT_GEN_CHUNK_BY_MODE value, falling back to the global SCRIPT_GEN_CHUNK_SCENES
    for an unknown or None mode. The mode key is the exact editMode value
    ('summary'/'recap'/'commentary'/'educational' — note 'educational', not
    'education')."""
    key = (edit_mode or "").lower()
    return SCRIPT_GEN_CHUNK_BY_MODE.get(key, SCRIPT_GEN_CHUNK_SCENES)


def _batch_count(scene_count: int, chunk: int = SCRIPT_GEN_CHUNK_SCENES) -> int:
    """How many `claude -p` batches a scene_count splits into, given the chunk size
    (defaults to the global SCRIPT_GEN_CHUNK_SCENES; callers pass a mode-resolved chunk
    from _chunk_for_mode). <= chunk size -> 1 (the degenerate single-call case)."""
    chunk = max(1, chunk)
    return max(1, (scene_count + chunk - 1) // chunk)


def _split_counts(scene_count: int, batches: int) -> list[int]:
    """Split scene_count into `batches` contiguous per-batch scene counts, as even as
    possible (the first few batches get the +1 remainder). Sum == scene_count."""
    base, extra = divmod(scene_count, batches)
    return [base + (1 if i < extra else 0) for i in range(batches)]


def _merge_renumber(batch_arrays: list[list]) -> list:
    """Concatenate per-batch scene arrays in order and renumber 'scene' 1..N
    contiguously across the merged result (each batch numbered scenes from 1 locally;
    after merge the global order is what matters). Preserves every other field."""
    merged = []
    n = 0
    for arr in batch_arrays:
        for s in arr:
            n += 1
            if isinstance(s, dict):
                s = dict(s)
                s["scene"] = n
            merged.append(s)
    return merged


def _run_batches_parallel(prompts: list[str],
                          cache_parts: list[dict] | None = None,
                          force_regen: bool = False,
                          llm_kwargs: dict | None = None) -> list[list]:
    """Run an ORDERED list of per-batch script-gen prompts through a bounded
    ThreadPoolExecutor and return their parsed scene arrays IN INPUT ORDER.

    Ordering guarantee (the #1 invariant): each task carries its submit index `i` and
    writes its result into a fixed-position `results[i]` — NEVER appended by completion
    order. So `_merge_renumber(_run_batches_parallel(prompts))` numbers scenes exactly
    as the old sequential loop did, regardless of which batch finishes first.

    Concurrency = max(1, min(SCRIPT_GEN_CONCURRENCY, len(prompts))). Each task just calls
    `_run_claude_script(prompt, timeout=SCRIPT_GEN_TIMEOUT)`, which keeps its OWN
    retry/backoff and 504/error semantics unchanged — this helper adds nothing to a
    single batch's behavior, it only runs the independent batches at the same time.

    `cache_parts` (optional): a list parallel to `prompts` of per-batch cache-key dicts
    (or None entries). Each is forwarded to its batch's _run_claude_script so a repeat
    batch (regen / retry) hits the disk cache. Length must match `prompts`.

    `llm_kwargs` (optional): the job's {provider, model} choice, from `_llm_kwargs`. It is
    `{}`/None when the job made no choice, and in that case the submit below is the exact
    same call shape as before the provider gate — which is why every existing stub in the
    test-suite keeps matching.

    Fail-fast: if any batch raises (e.g. a 504 after its retries, carrying today's
    Vietnamese-facing timeout message), we drain the remaining futures and re-raise the
    FIRST exception — same all-or-nothing outcome as the sequential loop, never an
    ambiguous partial merge. Mirrors the errs/as_completed pattern used by the assemble
    progress executor below.
    """
    n = len(prompts)
    results: list = [None] * n
    if n == 0:
        return results

    max_workers = max(1, min(SCRIPT_GEN_CONCURRENCY, n))

    import concurrent.futures

    errs: list = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        # Pass the cache args ONLY when caching is requested, so the no-cache call shape
        # stays exactly _run_claude_script(prompt, timeout) (keeps existing 2-arg stubs
        # and callers working unchanged).
        _llm = llm_kwargs or {}
        if cache_parts:
            futs = {
                ex.submit(_run_claude_script, prompt, SCRIPT_GEN_TIMEOUT,
                          cache_parts[i], i, force_regen, **_llm): i
                for i, prompt in enumerate(prompts)
            }
        else:
            futs = {ex.submit(_run_claude_script, prompt, SCRIPT_GEN_TIMEOUT, **_llm): i
                    for i, prompt in enumerate(prompts)}
        for fut in concurrent.futures.as_completed(futs):
            i = futs[fut]
            try:
                results[i] = fut.result()  # fixed slot — order independent of completion
            except BaseException as e:  # noqa: BLE001 — drain the rest, then re-raise first
                errs.append(e)
    if errs:
        raise errs[0]
    return results


@router.post("/generate/script")
def generate_script(req: ScriptRequest):
    # Section A: scene count is script-driven, NOT source/7. The topic-only path has
    # no source, so derive the fallback hint from the (target-duration) word budget:
    # never request more scenes than the budget can fill (>= _MIN_WORDS_PER_SCENE each).
    scene_count = req.sceneCount or max(5, _word_budget(req.durationSec) // _MIN_WORDS_PER_SCENE)
    prompt = _build_prompt(req.topic, req.durationSec, scene_count)
    scenes = _run_claude_script(prompt, timeout=SCRIPT_GEN_TIMEOUT,
                                **_llm_kwargs(req.llmProvider, req.llmModel))
    return {
        "topic": req.topic,
        "durationSec": req.durationSec,
        "sceneCount": len(scenes),
        "scenes": scenes,
        **_llm_used(req.llmProvider, req.llmModel),
    }


# --- Script-gen for translate/reup pages (transform a source transcript) -
#
# Stage 1 for CTG Gaming et al.: take the SOURCE transcript from /generate/ingest
# and REWRITE it into a Vietnamese script per the chosen editing mode. This is the
# transformation step the "how to edit video" playbook mandates — the output is
# the creator's own voice (>60–80%), never a verbatim translation of the source.

# Per-mode steering, keyed to the three modes in "how to edit video.md".
EDIT_MODE_GUIDE = {
    "commentary": (
        "MODE: COMMENTARY (analysis + opinion). This is an ORIGINAL piece, NOT a "
        "translation: do NOT track the source line-by-line. EXPLAIN it, give a clear "
        "personal take/judgment, and analyze in depth. You MAY freely REORDER and "
        "restructure the material to serve your argument. Your analysis is the CORE "
        "content; the original footage is illustration ONLY (<=20-40%). Length is the "
        "creator's choice (the requested target), NOT derived from the source — the "
        "video may run shorter OR longer than the source. Write ALL narration in "
        "Vietnamese."
    ),
    "recap": (
        "MODE: RECAP (valuable condensed retell). RETELL the source faithfully but "
        "CONDENSED: KEEP roughly 60-75% of the source — drop the least-important beats, "
        "redundancy, and slow stretches, but PRESERVE the substantive story. You MUST "
        "REORDER the logic for clarity (do NOT just follow the source order) and ADD a "
        "LIGHT layer of analysis: open with a strong hook and close with a takeaway/"
        "lesson. This is more than a trim — it is a curated, re-sequenced retell in your "
        "own narrating voice, never a verbatim copy. The original footage is illustration "
        "only; your narration leads and is the main content. Length is SOURCE-DERIVED "
        "(it follows from how much you keep). Write ALL narration in Vietnamese."
    ),
    "educational": (
        "MODE: EDUCATIONAL (turn it into a lesson/knowledge). This is an ORIGINAL piece "
        "in LESSON form, NOT a translation: do NOT track the source line-by-line. Turn "
        "the source into a lesson, a how-to, the psychology behind a behavior, or why a "
        "phenomenon went viral. You MAY freely REORDER and restructure the material into "
        "a clear teaching arc, and you ADD analysis FRAMED AS THE LESSON (the point being "
        "taught is the core content). The original footage is illustration only; your "
        "explanation leads and is the main content. Length is the creator's choice (the "
        "requested target), NOT derived from the source. Write ALL narration in Vietnamese."
    ),
    "translate_full": (
        "MODE: TRANSLATE_FULL (full localization). Re-tell the ENTIRE video in natural "
        "spoken Vietnamese — this is NOT a 1:1 literal translation and is NOT time-locked to "
        "the source segments. KEEP 100% of the substantive content (every point, fact, "
        "example, and number) in the ORIGINAL chronological ORDER. The ONE difference from "
        "summary/recap is that you do NOT condense or drop anything — full retention is "
        "mandatory: cut ONLY junk (ads, sponsor reads, like/subscribe/follow prompts, channel "
        "self-promotion, source credit/attribution/watermark call-outs, bloated intros/outros, "
        "silent/dead gaps). Do NOT reorder. The original footage is shown MUTED while your "
        "Vietnamese narration leads and carries all the content; no verbatim word-for-word "
        "copying, natural spoken phrasing. Write ALL narration in Vietnamese."
    ),
    "summary": (
        "MODE: SUMMARY (near-full faithful retell). THIS IS NOT A RECAP — do NOT condense "
        "hard and do NOT add analysis. COVER AT LEAST 75% of the source timeline (aim for "
        "80%): advance your scenes through the FULL video from start to finish with only "
        "genuine filler cuts — do NOT skip blocks of real content. Retell ALL substantive "
        "content in the original's EXACT chronological ORDER (do NOT reorder), faithfully "
        "covering every argument, detail, example, and development in the channel's own "
        "Vietnamese narrating voice (NOT a verbatim translation). You MAY ONLY cut FILLER: "
        "like/subscribe/follow prompts, thank-yous, channel self-promotion, sponsor reads, "
        "bloated intros/outros, silent/dead gaps, and verbatim repetition. NEVER drop real "
        "content just to make it shorter, and do NOT inject your own take/opinion — this is "
        "a faithful retell, not a commentary. Output length tracks the source MINUS the "
        "filler. The original footage is illustration; your Vietnamese narration leads and "
        "is the main content; no verbatim copying, no reupload. Write ALL narration in "
        "Vietnamese."
    ),
}

# Appended to EVERY script/subtitle prompt (all edit modes + dubbed + visual-explain +
# topic). The Vietnamese output must be FULLY Vietnamese so each sentence reads clearly —
# no stray English words leaking through a translate/reup. Keeps only genuine proper nouns
# and standard units/symbols; technical terms are rendered by MEANING when no VN word fits.
_VI_FULL_TRANSLATION_RULE = (
    "FULL TRANSLATION IS MANDATORY — every sentence must read clearly in Vietnamese with NO "
    "stray English: translate ALL English words, verbs, adjectives, technical terms, and common "
    "loanwords into natural Vietnamese; do NOT leave any English word untranslated. Keep in the "
    "original ONLY genuine PROPER NOUNS (personal / place / brand / product / company names) and "
    "standard units/symbols. When a technical term has a normal Vietnamese equivalent, use it; "
    "otherwise render its MEANING naturally in Vietnamese (you may append the English term once in "
    "parentheses only if it genuinely aids understanding)."
)
_SOURCE_TRANSCRIPT_CAP = 14000  # keep the prompt well within Claude Code's input

# AUTO mode can hand us a source-based durationSec that may be long (the source
# could be several minutes). Scene count is still ~1 scene per 7s, but in AUTO we
# cap it so a long source doesn't explode into hundreds of scenes (the cut/render
# step would choke). FIXED keeps the original short-form-friendly derivation.
#
# The default cap (commentary/recap/educational) stays short-form-friendly: those
# modes condense, so 40 scenes is plenty. SUMMARY is different — its whole point is
# to track the SOURCE length (keep most of the content, only trim filler), so a 40-
# scene cap would throttle an 8-min source down to ~5 min. Cut+assemble are now
# parallelized, so more scenes is fine; summary gets a much higher ceiling that only
# guards against pathological inputs (e.g. a multi-hour source).
_AUTO_SCENE_CAP = 40
# SUMMARY scene cap (perf/stability fix — bug3). The flat 200 cap let a ~475s
# source explode into 119 scenes (tripling TTS chunks → ~54-min render). The cap is
# now TIED TO THE SOURCE: cap = round(hint * _SUMMARY_SCENE_CAP_FACTOR) where the
# hint is the source-derived sizing (~source/_SECONDS_PER_SCENE). For a 475s source
# hint=round(475/7)=68 → cap=round(68*1.3)=88, so the AUTO summary scene count can
# no longer reach 119. A floor keeps very short sources usable. An ABSOLUTE ceiling
# still guards pathological multi-hour inputs.
_SUMMARY_SCENE_CAP_FACTOR = 1.3   # summary cap = round(source-hint * 1.3)
_SUMMARY_SCENE_CAP_FLOOR = 10     # never cap a summary below this (short sources)
_SUMMARY_SCENE_CAP_ABS = 200      # hard ceiling for pathological (multi-hour) inputs


def _summary_scene_cap(duration_sec: int) -> int:
    """Source-tied AUTO scene cap for SUMMARY mode (bug3 perf fix).

    cap = clamp(round(hint * _SUMMARY_SCENE_CAP_FACTOR),
                _SUMMARY_SCENE_CAP_FLOOR, _SUMMARY_SCENE_CAP_ABS)
    where hint = round(duration / _SECONDS_PER_SCENE) is the same source-derived
    sizing the AUTO scene-count uses. This caps the scene count proportionally to
    the source instead of the old flat 200, so a short/medium source can't produce
    pathologically many scenes (the job-14 119-scene / 54-min-render regression)."""
    hint = max(1, round((duration_sec or 0) / _SECONDS_PER_SCENE))
    cap = round(hint * _SUMMARY_SCENE_CAP_FACTOR)
    return max(_SUMMARY_SCENE_CAP_FLOOR, min(cap, _SUMMARY_SCENE_CAP_ABS))


# Per-mode AUTO scene caps (flat constants). SUMMARY is NOT listed here anymore — it
# uses the source-tied _summary_scene_cap() above (bug3). Modes not listed fall back
# to _AUTO_SCENE_CAP. Kept as a dict so a future mode can opt into a flat cap.
_AUTO_SCENE_CAP_BY_MODE: dict[str, int] = {}

# --- Keep-ratio enforcement (plan §B, footage path only) -------------------
#
# Coverage metric (language-neutral): RATIO = Σ(sourceEnd − sourceStart) / window
# = fraction of source SECONDS the script covers. This works ACROSS languages
# (the narration is Vietnamese, the source transcript is often Chinese) because it
# measures source-time covered, never word counts. Bands are per-mode:
#   recap   → keep 60-75% (condensed + reorder + light analysis)
#   summary → keep 76-90% (near-full faithful retell, in order)
# Modes NOT listed (commentary/educational/dubbed/unknown) have NO band → the
# coverage ratio is never enforced for them (they are original/no-track pieces).
# This band is INDEPENDENT of _auto_word_ceiling (which bounds narration *words*
# below the source length) — the two guards measure different things.
#   translate_full → keep 85-100% (FULL retention; only junk is skipped)
_KEEP_RATIO_BAND = {"recap": (0.60, 0.75), "summary": (0.75, 0.85),
                    "translate_full": (0.85, 1.0)}
# Out-of-band → bounded REGEN: up to this many EXTRA claude -p calls (so up to 2
# total attempts). Each regen costs another subscription call — keep it bounded.
# (Lowered 2→1 in the 2026-06-27 tuning pass: a second regen rarely converges and
# doubles the worst-case extra cost; the closest attempt is kept either way.)
_RATIO_REGEN_ATTEMPTS = 1
# Minimum band-distance REDUCTION (as a fraction, i.e. ratio points) a regen must
# achieve over the best seen so far to count as "converging". A pass that does not
# beat best_dist by at least this much triggers an EARLY BREAK (perf guard — see the
# regen loop): a non-converging nudge would otherwise waste the remaining passes
# (the job-14 regression burned all passes stuck at 11.6%). 0.01 = 1 percentage
# point of source-coverage; smaller deltas are treated as no real progress.
_RATIO_REGEN_MIN_IMPROVE = 0.01

# Regen-correction nudge appended to _build_footage_prompt on a retry. ENGLISH
# instruction; the narration-output-in-Vietnamese mandate is preserved by the
# template itself. {pct}=previous coverage %, {mode}=edit mode, {lo}/{hi}=band as
# integer percents, {direction_clause}=_RATIO_REGEN_OVER / _RATIO_REGEN_UNDER.
_RATIO_REGEN_NUDGE = (
    "REGEN CORRECTION (keep-ratio out of band): your PREVIOUS attempt kept ~{pct}% of "
    "the source, but MODE {mode} requires keeping {lo}-{hi}% of the source. {direction_clause} "
    "Adjust which source beats you cover so the kept fraction lands inside {lo}-{hi}%. "
    "Keep all other rules (Vietnamese narration, your own voice as the main content, "
    "the chosen mode's structure) unchanged."
)

# Selected by code: if ratio > hi (kept too much) use _OVER; if ratio < lo use _UNDER.
_RATIO_REGEN_OVER = (
    "You kept TOO MUCH: cut more of the LEAST-IMPORTANT beats — redundancy, slow "
    "stretches, and minor asides — while preserving the substantive story."
)
_RATIO_REGEN_UNDER = (
    "You cut TOO MUCH: retain MORE substantive content — restore real arguments, "
    "details, and examples you dropped; trim only genuine filler."
)

# Section A: SIZING HINT only — NOT a forcing rule. Used as a divisor to turn a
# source span (seconds) into a rough scene-count HINT for SOURCE-TRACKING modes
# (summary/recap). It no longer dictates scene boundaries: the prompt frames the
# number as a SOFT TARGET and Claude owns the actual scene splits. The old
# `round(source/7)` leak (which forced ~72 scenes for any long source regardless of
# mode) is gone — only source-tracking modes still derive a hint from duration;
# original-length modes (commentary/educational) derive it from the word ceiling.
_SECONDS_PER_SCENE = 7.5

# Vietnamese narration speaking rate used to turn a target duration into a hard
# word budget — the SINGLE SOURCE OF TRUTH for pace, read by both _word_budget()
# (FIXED-mode word budget, used by both prompt builders) and _auto_scene_count().
#
# ~2.1 words/sec ≈ 126 words/min — a CALM, comfortable Vietnamese short-form pace.
# OWNER PRINCIPLE: a video is NEVER made shorter by reading faster. To hit a target
# duration we curate/reduce the CONTENT (fewer words) or let it run longer — we must
# NOT cram more words into the same seconds, which forces the TTS to read fast and
# loses the viewer. So this rate is deliberately on the calm side: lower words/sec =
# fewer words for the same target seconds = unhurried delivery.
#
# COMBINED-PACE NOTE (coordinated with media-engineer): the pace a viewer hears =
# script word-density (this constant) x TTS speed factor. Production TTS (F5) runs at
# natural 1.0x (no artificial over-speed), so the EFFECTIVE heard pace ≈ this density.
#
# MEASURED (2026-06-27): across 38 cached footage render manifests (E:\ContentFactory\
# _cache\renders\*\manifest.json), 33905 whitespace tokens over 10434.5s of per-scene
# VO → POOLED effective pace 3.249 tok/s (per-job MEAN 3.27, RANGE 2.23–4.17, n=38).
# This is the EFFECTIVE pace INCLUDING F5's delivery wobble — durationS is the probed
# per-scene VO length and the footage concat is a hard cut (no inter-scene gaps), so
# sum(durationS) is the true spoken time.
#
# BUDGET PACE = 2.2 (NOT the measured 3.25) — OWNER'S EXPLICIT CHOICE (reliability over
# hitting target length). The word budget deliberately uses a pace near the SLOW END of
# the observed range, not the pooled mean, so the under-source invariant holds for the
# slowest-rendering jobs WITHOUT relying on the runtime gates to catch overshoots. The
# trade: videos land ~0.69x of source at the mean pace (a sized-to-ceiling script of W
# words renders to W/3.27 s ≈ 0.69·(source-slate)) instead of near 1.0x — shorter than
# target, but with near-zero wasted/failed runs. (Still longer than the old 2.1 guess,
# which landed ~0.58x.) Setting the pace to the measured 3.25 would chase target length
# but make the budget formula rely on the gates to fail slow jobs — the owner rejected
# that in favor of reliability.
#
# UNDER-SOURCE INVARIANT (budget pace 2.2, safety 0.90). The AUTO word ceiling is
# (source - slate) * _DURATION_SAFETY * _VI_WORDS_PER_SEC = (source-slate)*0.9*2.2, so
# its words-per-second budget = 0.9*2.2 = 1.98. A script sized to that ceiling renders
# to ceiling/real_pace seconds, and the invariant holds as long as the budget rate stays
# at or below the SLOWEST pace the voice actually delivers.
# Owner 2026-07-26: overall reading pace slowed 10% across ALL modes. This word budget
# was dropped 10% (2.5 -> 2.2, so the budget rate 2.25 -> 1.98) in LOCKSTEP with
# GLOBAL_TARGET_MS_PER_SYL (231 -> 254): both the words and the delivery slowed by the
# same factor, so the margin proven at the old calibration is preserved proportionally
# and footage/translate output still fits strictly under the source.
# NOTE ON THE MEASURED FIGURES ABOVE (pooled 3.249 tok/s, per-job mean 3.27, range
# 2.23-4.17, n=38): they were measured on 2026-06-27, i.e. BEFORE that 10% slowdown, so
# they describe the FASTER voice. They are kept as the historical basis of the choice —
# do not compare them directly against today's 2.2 budget without applying the 10%.
# The runtime gates (script-step duration, post-TTS, post-assembly) remain the backstops
# for the slowest-case edge and for any genuine model overshoot.
_VI_WORDS_PER_SEC = 2.2
# DURATION GUARDRAIL (footage/translate mode). The owner's hard rule is that the
# finished video must be STRICTLY shorter than the source — and we must NEVER hit
# that by speeding up the voice or truncating narration. Instead, in AUTO mode we
# cap the SCRIPT word count so the natural-paced Vietnamese VO structurally fits
# under the source. The output also carries a fixed credit slate, and TTS pace
# wobbles ±10-15%, so the word ceiling targets (source - slate) * safety, never the
# raw source length. Both constants are reused by the runner-side hard backstop guard
# (_CREDIT_SLATE_SEC) so the prompt budget and the post-assemble assertion stay in sync.
_CREDIT_SLATE_SEC = 3.0    # fixed credit slate appended after the scenes (see assemble_footage)
_DURATION_SAFETY = 0.90    # extra margin below (source - slate) to absorb TTS pace wobble

# --- Fixed OUTRO call-to-action (owner request 2026-07-29) ---------------------------
# Every narration script ends with the SAME follow CTA. It is appended DETERMINISTICALLY
# after generation (see append_outro), NOT asked of `claude -p`: the owner wants a FIXED
# sentence, and an LLM would paraphrase it differently every run (and cost tokens per
# batch). Being real narration it is SPOKEN by TTS and shown in the karaoke caption.
#
# BUDGET: its spoken seconds are RESERVED out of the content budget (see _word_budget /
# _auto_word_ceiling) instead of being added on top. The footage duration gates are
# zero-tolerance (output must be strictly shorter than the source — see
# _enforce_fixed_source_fit and runner._enforce_post_tts_duration), so bolting ~5 s of
# extra VO onto a script that already filled its ceiling would start FAILING jobs that
# used to pass. Reserving instead makes Claude write proportionally less content, and
# every existing gate keeps working untouched.
CF_OUTRO_ENABLED = (os.getenv("CF_OUTRO", "1") or "").strip().lower() not in ("0", "off", "false", "no")
CF_OUTRO_TEXT = (os.getenv("CF_OUTRO_TEXT")
                 or "Nếu bạn thấy hữu ích, hãy follow để cập nhật những thông tin tiếp theo.").strip()
# Seconds the final black handle card lasts (see _finish_video's outro card). Counted by
# the runner's duration gate alongside the credit slate, so keep the two in sync.
CF_OUTRO_CARD_SEC = float(os.getenv("CF_OUTRO_CARD_SEC", "3"))


def _outro_words() -> int:
    """Word count of the fixed outro CTA (0 when disabled) — the amount reserved out of
    the content word budget so the outro never pushes a job past a duration gate."""
    return len(CF_OUTRO_TEXT.split()) if (CF_OUTRO_ENABLED and CF_OUTRO_TEXT) else 0


def _outro_seconds() -> float:
    """Spoken seconds the outro CTA is expected to take, at the shared narration pace."""
    return _outro_words() / _VI_WORDS_PER_SEC if _VI_WORDS_PER_SEC else 0.0


def append_outro(scenes: list) -> bool:
    """Append the fixed outro CTA to the LAST scene's narration, in place. Returns True
    when it actually appended.

    Appended to the last scene rather than added as a NEW scene on purpose: a new scene
    would need its own visual (an extra SDXL render in image mode, a source cut in footage
    mode) while this rides the last scene's existing visual — the last clip simply lasts
    a few seconds longer (image: Ken Burns continues; footage: the existing slow-down /
    freeze-last-frame logic covers it). The separate BLACK handle card the owner asked for
    is a different thing and is produced by the assembly (see _finish_video's outro card).

    Idempotent: a scene list whose last narration already ends with the CTA is left alone,
    so a retry / script-reuse run cannot stack the outro twice. No-op for an empty list,
    a non-scene shape (dubbed's dict), or when the caption differs from the narration
    (`caption` is kept in sync so the karaoke shows what is spoken)."""
    if not (CF_OUTRO_ENABLED and CF_OUTRO_TEXT) or not isinstance(scenes, list) or not scenes:
        return False
    last = scenes[-1]
    if not isinstance(last, dict):
        return False
    narration = (last.get("narration") or "").strip()
    if not narration or narration.endswith(CF_OUTRO_TEXT):
        return False
    joined = f"{narration} {CF_OUTRO_TEXT}" if narration[-1:] in ".!?…" else f"{narration}. {CF_OUTRO_TEXT}"
    last["narration"] = joined
    # `caption` is the DISPLAY text (falls back to narration when absent). Keep the two in
    # sync so the karaoke line shows the CTA it is speaking — but only when the caption was
    # merely mirroring the narration; a deliberately different caption is left untouched.
    cap = last.get("caption")
    if cap is None or (cap or "").strip() == narration:
        last["caption"] = joined
    return True
# POST-GEN word-ceiling ENFORCEMENT (bug3 root-cause fix). The word ceiling is
# injected into the AUTO prompt as a HARD per-script budget, but the LLM can still
# overshoot (job-14 produced an 8484-word script vs a ~892-word ceiling, 9.5x).
# After generation we re-count the TOTAL narration words and FAIL the job if it
# exceeds ceiling * _WORD_CEILING_TOLERANCE — we never auto-truncate the text and
# never speed up the voice (owner rule). A small tolerance absorbs honest word-count
# wobble (tokenization, the LLM landing slightly over) without letting a 9.5x blowout
# through. Enforced ONLY for SOURCE-TRACKING modes (summary/recap) — those are the
# modes whose length must fit the source; commentary/educational are original-length.
_WORD_CEILING_TOLERANCE = 1.15
# Modes whose AUTO output length must fit the source → subject to the post-gen word
# ceiling enforcement above (kept in sync with runner._SOURCE_TRACKING_MODES).
_WORD_CEILING_MODES = {"summary", "recap"}


def _count_narration_words(scenes: list) -> int:
    """Total whitespace-delimited word count across all scenes' narration. Used by the
    post-gen word-ceiling enforcement. Whitespace split matches _word_budget /
    _allocate_scene_budgets word counting, so the budget and the enforcement agree."""
    return sum(len((s.get("narration") or "").split()) for s in (scenes or []))


def _enforce_word_ceiling(mode: str, scenes: list, source_seconds: float) -> None:
    """HARD post-gen check (bug3): for SOURCE-TRACKING modes, if the generated
    narration exceeds the source-derived word ceiling (times a small tolerance),
    FAIL with a clear Vietnamese error. Does NOT mutate scenes (no truncation) and
    never speeds up the voice. No-ops for non-tracking modes or a non-positive source
    span (no honest ceiling to compare against)."""
    if (mode or "").lower() not in _WORD_CEILING_MODES:
        return
    if not source_seconds or source_seconds <= 0:
        return
    ceiling = _auto_word_ceiling(source_seconds)
    total = _count_narration_words(scenes)
    limit = math.floor(ceiling * _WORD_CEILING_TOLERANCE)
    if total > limit:
        log.warning(
            "[generate] word-ceiling FAILED: mode '%s' produced %d words > limit %d "
            "(ceiling %d x tol %.2f) for a %.0fs source — failing the job (no truncation).",
            mode, total, limit, ceiling, _WORD_CEILING_TOLERANCE, source_seconds,
        )
        raise HTTPException(
            422,
            f"Kịch bản vượt giới hạn từ cho độ dài nguồn ({total} từ > trần {ceiling} từ) "
            f"— hãy rút ngắn kịch bản.",
        )


def _enforce_fixed_source_fit(scenes: list, source_seconds: float) -> None:
    """FIXED-mode FAST-FAIL: a source-derived footage job's natural-paced VO must fit
    UNDER the source duration regardless of edit mode. If the total narration exceeds
    the source-length ceiling (the SAME source→words bound the AUTO path uses) times
    the shared tolerance, FAIL at script-gen with a clear Vietnamese error instead of
    letting it surface ~50 min later at the assembly duration gate. Mode-agnostic (the
    constraint is physical, not content-policy), so unlike _enforce_word_ceiling there
    is no mode allow-list. Does NOT mutate scenes and never speeds up/ truncates the
    voice. No-ops when the source span is non-positive (no honest ceiling)."""
    if not source_seconds or source_seconds <= 0:
        return
    ceiling = _auto_word_ceiling(source_seconds)
    total = _count_narration_words(scenes)
    limit = math.floor(ceiling * _WORD_CEILING_TOLERANCE)
    if total > limit:
        log.warning(
            "[generate] FIXED source-fit FAILED: %d words > limit %d (ceiling %d x tol %.2f) "
            "for a %.0fs source — failing fast at script-gen (no truncation).",
            total, limit, ceiling, _WORD_CEILING_TOLERANCE, source_seconds,
        )
        raise HTTPException(
            422,
            f"Kịch bản vượt giới hạn từ cho độ dài nguồn ({total} từ > trần {ceiling} từ) "
            f"— hãy rút ngắn kịch bản.",
        )


# Zero-tolerance epsilon for the script-step duration gate (mirrors
# runner._EARLY_DURATION_EPSILON_SEC). 0 == any overshoot fails.
_SCRIPT_DURATION_EPSILON_SEC = 0.0


def _enforce_script_duration(scenes: list, source_seconds: float, *,
                             with_slate: bool = True) -> None:
    """PRIMARY, CHEAPEST duration fail-fast — runs IN the script step, before TTS.

    Estimates the finished VO length straight from the script:
        est_seconds = total_tokens / _VI_WORDS_PER_SEC + (slate if appended)
    and FAILS immediately (zero tolerance) if est_seconds >= source_seconds. This is the
    earliest possible gate: it needs no audio, no whisper, no render — just the word
    count and the measured pace. It complements _enforce_fixed_source_fit /
    _enforce_word_ceiling (which bound the WORD COUNT against the source-derived ceiling);
    this one bounds the ESTIMATED SECONDS directly against the source, the same quantity
    the runtime gates (post-TTS + post-assembly) measure for real. Three layers, all the
    same invariant, failing progressively earlier and cheaper.

    `with_slate`: add _CREDIT_SLATE_SEC (a credit slate will be appended). The script
    endpoint doesn't know the job's addCredit flag, so we default True — the conservative
    (stricter) choice, consistent with _auto_word_ceiling already reserving the slate.
    Never mutates scenes / never speeds up the voice. No-ops when source is non-positive."""
    if not source_seconds or source_seconds <= 0:
        return
    tokens = _count_narration_words(scenes)
    slate = _CREDIT_SLATE_SEC if with_slate else 0.0
    est_seconds = tokens / _VI_WORDS_PER_SEC + slate
    if est_seconds >= source_seconds - _SCRIPT_DURATION_EPSILON_SEC:
        log.warning(
            "[generate] script duration gate FAILED (pre-TTS): est %d tokens / %.2f w/s "
            "+ slate %.1fs = %.1fs >= source %.1fs — failing fast at script-gen.",
            tokens, _VI_WORDS_PER_SEC, slate, est_seconds, source_seconds,
        )
        raise HTTPException(
            422,
            f"Video đầu ra (~{est_seconds:.1f}s) sẽ dài hơn hoặc bằng video gốc "
            f"({source_seconds:.1f}s) — dừng ngay, hãy giảm nội dung kịch bản.",
        )


# Minimum words a scene needs to be worth narrating. Used to keep scene_count
# consistent with the word budget — never request more scenes than the budget
# can fill (else each scene is a fragment, or the model pads to compensate).
_MIN_WORDS_PER_SCENE = 8

# Shared steering snippets appended to BOTH transform and footage prompts. They
# apply in AUTO and FIXED alike. Kept as constants so the two builders stay in
# sync.
_KEEP_ENGLISH_TERMS = (
    "KEEP ENGLISH TERMS AS-IS: technical terms, proper nouns, product/library/technology "
    "names (e.g. MCP, RAG, LLM, agent, context window, token, prompt, API, framework, "
    "GitHub...) MUST stay in English in the narration — do NOT translate, transliterate, "
    "or Vietnamize them. Embed them naturally inside the Vietnamese sentence. "
    "The TTS pronunciation for these terms is handled separately. "
    "EXCEPTION — common non-technical terms that have a natural Vietnamese equivalent "
    "SHOULD be translated: use \"tính năng\" for \"feature\", \"cơ sở dữ liệu\" for "
    "\"database\", \"triển khai\" for \"deploy\". When in doubt, keep the English term."
)
# Per-scene proper-noun DENSITY guard. A short scene crammed with many English proper
# nouns (a long comma-separated list of product/tool/library names) makes the TTS
# under-allocate frames and rush the line — it reads clipped and whisper mangles the
# names (observed: "Cursor, Windsurf, Klein, Roo, Ader" in one ~3s scene). Per the
# owner's hard rule the voice is NEVER sped up to fit, so the fix is at the SCRIPT
# level: keep each scene's spoken density comfortable. Appended to BOTH builders.
_PROPER_NOUN_DENSITY = (
    "ONE SCENE = ONE COMFORTABLE BREATH (avoid rushed proper-noun lists): do NOT cram "
    "more than ~3 English proper nouns (product/tool/library/company names) into a single "
    "short scene, and do NOT put a long comma-separated enumeration of names in one scene. "
    "When you must list several names, SPLIT them across consecutive scenes (a few names "
    "per scene) and/or PAD with connective Vietnamese words (e.g. \"cùng với\", \"và\", "
    "\"chẳng hạn như\", \"là những ví dụ\") so each scene's spoken line stays at a calm, "
    "natural speaking density and is never crammed. Never rely on speeding up the voice."
)
_CUT_PROMO = (
    "CUT THE NON-CONTENT SEGMENTS: completely drop ads, like/subscribe/follow prompts for "
    "the original channel, sponsor reads, channel self-promotion, bloated intros/outros, and "
    "silent/dead gaps that carry no information. Do NOT retell them and do NOT create scenes "
    "for them."
)
# Footage-prompt variant: also forbid selecting those windows as source footage.
_CUT_PROMO_FOOTAGE = (
    _CUT_PROMO
    + " Do NOT pick the ad/subscribe/sponsor/intro/outro time ranges as illustrative "
    "footage (sourceStart/sourceEnd)."
)


# --- Dubbed mode (Section F) — content artifacts (claude -p) ----------------
#
# Dubbed keeps the ORIGINAL audio+video and burns translated Vietnamese
# subtitles, trimming only filler. It produces NO narration scene array. Two
# claude -p calls supply its artifacts, both in SOURCE-timeline seconds:
#   1. _translate_subs_to_vi(segments)  -> [{start,end,text_vi}]  (faithful VN subs)
#   2. _detect_filler_ranges(segments)  -> [{start,end,reason}]   (cut-list; may be [])
# Both reuse the existing _run_claude_script headless runner (same path the
# footage script-gen uses): English prompt INSTRUCTIONS, Vietnamese subtitle
# OUTPUT (per the project token policy). Timings are NEVER invented by the LLM —
# the translate path maps output back onto the input segment timings by index.


def _segments_to_numbered_transcript(segments: list, cap: int = _SOURCE_TRANSCRIPT_CAP) -> tuple[str, int]:
    """Render timestamped source segments into a numbered, bounded transcript for a
    dubbed-mode claude -p prompt. Each line is `i. [start-end] text` (i is 1-based,
    matching the index the translate prompt must echo back). Truncated to `cap`
    characters so the prompt stays within Claude Code's input. Returns (text,
    n_lines_included)."""
    lines: list[str] = []
    total = 0
    for i, sg in enumerate(segments):
        try:
            st = float(sg["start"])
            en = float(sg["end"])
            txt = (sg.get("text") or "").strip()
        except (KeyError, TypeError, ValueError):
            continue
        line = f"{i + 1}. [{st:.1f}-{en:.1f}] {txt}"
        total += len(line) + 1
        if total > cap:
            break
        lines.append(line)
    return "\n".join(lines), len(lines)


def _translate_subs_to_vi(segments: list, llm_provider: str | None = None,
                          llm_model: str | None = None) -> list[dict]:
    """Translate the timestamped source segments into a Vietnamese subtitle list
    [{start,end,text_vi}], FAITHFULLY (closer-to-literal than narration — a subtitle
    must match the spoken line).

    Timings are NOT invented by the LLM: it returns one VN line per input segment,
    keyed by the segment index, and we map the translation back onto the ORIGINAL
    start/end of that segment. If the model drops/merges lines (count mismatch), we
    fall back to translating per-segment so timings stay anchored to the source.

    Raises HTTPException with a clear Vietnamese message if translation fails
    outright. Returns [] only if there are no source segments.
    """
    segs = [sg for sg in (segments or [])
            if isinstance(sg, dict) and sg.get("text") and str(sg.get("text")).strip()]
    if not segs:
        return []

    transcript, n_included = _segments_to_numbered_transcript(segs)
    prompt = (
        "You are a Vietnamese subtitle translator for a re-uploaded foreign video. "
        "Below is the source transcript with line indices and timestamps (seconds). "
        "Translate EACH line into natural, FAITHFUL Vietnamese — a subtitle must match "
        "what is spoken (closer-to-literal than a narration rewrite; do NOT summarize, "
        "add commentary, reorder, merge, or drop lines).\n"
        f"{_KEEP_ENGLISH_TERMS}\n\n"
        "Source transcript (one line per subtitle, prefixed by its index):\n"
        f"\"\"\"\n{transcript}\n\"\"\"\n\n"
        "Return ONLY a single valid JSON array, no markdown. EXACTLY one element per "
        "source line above, in the SAME order, each element: "
        '{"i": <the line index as given>, "text_vi": "<Vietnamese translation>"}. '
        "Do NOT output start/end times — only the index and the Vietnamese text. "
        f"Write all 'text_vi' in VIETNAMESE. {_VI_FULL_TRANSLATION_RULE}"
    )
    used = segs[:n_included]
    try:
        raw = _run_claude_script(prompt, timeout=SCRIPT_GEN_TIMEOUT,
                                 **_llm_kwargs(llm_provider, llm_model))
    except HTTPException as ex:
        # Surface a clear Vietnamese, user-facing failure (the runner persists it).
        raise HTTPException(
            getattr(ex, "status_code", 502),
            f"Dịch phụ đề tiếng Việt thất bại: {getattr(ex, 'detail', '') or 'Claude Code lỗi'}",
        )

    # Map the returned text back onto the ORIGINAL segment timings by index. The LLM
    # may echo the 1-based index ("i"); if it does we honour it, else fall back to
    # positional order. We NEVER trust an LLM-supplied time.
    by_index: dict[int, str] = {}
    positional: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            text_vi = (item.get("text_vi") or item.get("text") or "").strip()
            if not text_vi:
                continue
            positional.append(text_vi)
            idx = item.get("i", item.get("index"))
            try:
                if idx is not None:
                    by_index[int(idx)] = text_vi
            except (TypeError, ValueError):
                pass

    out: list[dict] = []
    if len(by_index) == len(used) and used:
        # Clean 1:1 indexed mapping — preferred.
        for i, sg in enumerate(used):
            out.append({"start": float(sg["start"]), "end": float(sg["end"]),
                        "text_vi": by_index.get(i + 1, "")})
    elif len(positional) == len(used) and used:
        # Count matches positionally even though indices were unusable.
        for sg, text_vi in zip(used, positional):
            out.append({"start": float(sg["start"]), "end": float(sg["end"]), "text_vi": text_vi})
    else:
        # Count mismatch (model dropped/merged lines). Anchor timings to the source by
        # using whatever indexed lines we DID get, leaving the rest blank rather than
        # mis-aligning text to the wrong timestamps. Log the mismatch honestly.
        log.warning(
            "[generate] dubbed translate: line-count mismatch (got %d indexed / %d "
            "positional, expected %d) — mapping by index where possible, blanks elsewhere",
            len(by_index), len(positional), len(used),
        )
        for i, sg in enumerate(used):
            out.append({"start": float(sg["start"]), "end": float(sg["end"]),
                        "text_vi": by_index.get(i + 1, "")})

    # Drop empty/degenerate subs (no text or non-positive duration).
    out = [s for s in out if s["text_vi"] and s["end"] > s["start"]]
    if not out:
        raise HTTPException(
            502,
            "Dịch phụ đề tiếng Việt không tạo được dòng nào hợp lệ — "
            "kiểm tra lại transcript nguồn.",
        )
    return out


def _detect_filler_ranges(segments: list, src_dur: float | None = None,
                          llm_provider: str | None = None,
                          llm_model: str | None = None) -> list[dict]:
    """Classify the source transcript and return a filler cut-list
    [{start,end,reason}] (SOURCE seconds) for ads / subscribe prompts / sponsor reads
    / channel self-promo / bloated intros+outros / dead-air. This extracts the
    _CUT_PROMO intent into a standalone detector.

    AN EMPTY LIST IS VALID — a dubbed source may have nothing to trim. Ranges are
    validated to be well-formed and within [0, src_dur] (when known); malformed
    ranges are dropped and LOGGED. A failed/empty LLM response yields [] (no cuts),
    NOT a hard failure — dubbed assembly then keeps the whole source.
    """
    segs = [sg for sg in (segments or []) if isinstance(sg, dict) and sg.get("text")]
    if not segs:
        return []
    transcript, _n = _segments_to_numbered_transcript(segs)
    prompt = (
        "You are a filler-segment detector for a re-uploaded foreign video. Below is "
        "the source transcript with timestamps (seconds). Identify ONLY the NON-CONTENT "
        "time ranges that should be CUT: advertisements, like/subscribe/follow prompts, "
        "sponsor reads, channel self-promotion, bloated intros/outros, and silent/dead "
        "gaps that carry no information. Do NOT cut substantive content. If there is "
        "NOTHING to cut, return an empty array.\n\n"
        "Source transcript (seconds):\n"
        f"\"\"\"\n{transcript}\n\"\"\"\n\n"
        "Return ONLY a single valid JSON array (possibly empty), no markdown. Each "
        'element: {"start": <seconds>, "end": <seconds>, "reason": "<short reason>"}. '
        "'reason' may be in English. Times are in SOURCE seconds and start < end."
    )
    try:
        raw = _run_claude_script(prompt, timeout=SCRIPT_GEN_TIMEOUT,
                                 **_llm_kwargs(llm_provider, llm_model))
    except HTTPException as ex:
        # Detector failure is non-fatal: log and proceed with NO cuts (keep the source).
        log.warning("[generate] dubbed filler detect failed (%s) — proceeding with no cuts",
                    getattr(ex, "detail", "") or ex)
        return []

    if not isinstance(raw, list):
        log.warning("[generate] dubbed filler detect: non-list result — proceeding with no cuts")
        return []

    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            st = float(item["start"])
            en = float(item["end"])
        except (KeyError, TypeError, ValueError):
            log.warning("[generate] dubbed filler: dropping malformed range %r", item)
            continue
        if src_dur and src_dur > 0:
            st = max(0.0, min(st, src_dur))
            en = max(0.0, min(en, src_dur))
        if en <= st:
            log.warning("[generate] dubbed filler: dropping non-positive range %r", item)
            continue
        out.append({"start": round(st, 2), "end": round(en, 2),
                    "reason": str(item.get("reason", "") or "")[:200]})
    return out


def _auto_scene_count(duration_sec: int, auto: bool, edit_mode: str | None = None) -> int:
    if auto:
        # Section A — re-base the AUTO scene-count HINT per mode (this was the
        # `round(source/7)` leak that forced ~72 scenes for any long source).
        #   * summary/recap are SOURCE-TRACKING: their length follows the source, so
        #     a duration-derived hint (duration / _SECONDS_PER_SCENE) is appropriate.
        #     _SECONDS_PER_SCENE is now a SIZING divisor, not a forcing rule — the
        #     prompt frames the number as a soft target.
        #   * commentary/educational are ORIGINAL-LENGTH (source-decoupled): derive
        #     the hint from the word ceiling, NOT from source duration, so a long
        #     source no longer inflates the scene count for these modes.
        mode = (edit_mode or "").lower()
        if mode in ("commentary", "educational"):
            ceiling = _auto_word_ceiling(duration_sec)
            n = max(5, ceiling // _MIN_WORDS_PER_SCENE)
        else:
            # summary / recap (and any unknown AUTO mode) → source-derived sizing hint.
            n = max(5, round((duration_sec or 0) / _SECONDS_PER_SCENE))
        # SUMMARY tracks the source length, so it gets its own SOURCE-TIED cap
        # (round(hint * 1.3), bug3 perf fix) instead of a flat constant; other modes
        # condense and keep the short-form-friendly default cap.
        # translate_full tracks the source length like summary (full retention), so it
        # gets the same source-tied cap — the flat _AUTO_SCENE_CAP (40) would throttle a
        # long full-retention source.
        if mode in ("summary", "translate_full"):
            cap = _summary_scene_cap(duration_sec)
        else:
            cap = _AUTO_SCENE_CAP_BY_MODE.get(mode, _AUTO_SCENE_CAP)
        return min(n, cap)
    n = max(5, round((duration_sec or 0) / _SECONDS_PER_SCENE))
    # FIXED: don't request more scenes than the word budget can realistically
    # fill (>= _MIN_WORDS_PER_SCENE words each), so per-scene narration stays
    # substantive and the model isn't pushed to pad.
    budget = _word_budget(duration_sec)
    max_scenes_for_budget = max(3, budget // _MIN_WORDS_PER_SCENE)
    return min(n, max_scenes_for_budget)


def _word_budget(duration_sec: int) -> int:
    """Max TOTAL narration words for a FIXED target duration, MINUS the fixed outro CTA's
    words (it is appended after generation, so the content must leave room for it)."""
    return max(1, round((duration_sec or 0) * _VI_WORDS_PER_SEC) - _outro_words())


def _auto_word_ceiling(source_seconds: float) -> int:
    """AUTO-mode HARD word ceiling tied to the SOURCE length so the natural-paced VO
    cannot structurally exceed the source. Targets (source - credit slate - outro) with an
    extra safety factor (TTS pace wobble), then converts to words via the shared pace.
    This is the script-side root-cause fix for the "output longer than source" bug —
    we generate LESS text, we never speed up the voice or truncate it. The outro CTA is
    subtracted for the same reason the slate is: both are appended AFTER generation, so
    the content budget must already exclude them."""
    target_seconds = max(1.0, (source_seconds or 0.0) - _CREDIT_SLATE_SEC - _outro_seconds())
    return max(1, math.floor(target_seconds * _DURATION_SAFETY * _VI_WORDS_PER_SEC))


class TransformRequest(_LlmChoiceMixin):
    transcript: str
    editMode: str = "commentary"       # commentary | recap | educational
    title: str | None = None           # source title, for context only
    durationSec: int = 60
    sceneCount: int | None = None
    sourceLang: str | None = None      # detected source language, for context
    auto: bool = False                 # AUTO: length follows the source, no fixed
                                       # target. durationSec is then only a source-
                                       # derived hint for how many scenes to make.


def _build_transform_prompt(req: TransformRequest, scenes: int) -> str:
    guide = EDIT_MODE_GUIDE.get(req.editMode.lower())
    if not guide:
        raise HTTPException(422, f"Unknown editMode '{req.editMode}' (use: {', '.join(EDIT_MODE_GUIDE)})")
    transcript = req.transcript.strip()
    truncated = ""
    if len(transcript) > _SOURCE_TRANSCRIPT_CAP:
        transcript = transcript[:_SOURCE_TRANSCRIPT_CAP]
        truncated = " (truncated — use only this portion as source material)"
    title_line = f"Source video title: {req.title}\n" if req.title else ""
    # AUTO: no fixed-seconds target. Length follows the source; summary keeps most
    # of the original and only trims redundancy/filler. FIXED: condense to ~N sec.
    if req.auto:
        if req.editMode.lower() == "summary":
            length_line = (
                "This is SUMMARY mode: CONDENSE the source. SELECT only the most important beats "
                "and summarize them tightly in the original's ORDER; DROP less-important detail, "
                "examples, asides, and all filler (subscribe/thanks/self-promo/sponsor/intro/"
                "outro/silent gaps/repetition). Do NOT try to cover everything. Do NOT pad. Do "
                "NOT cram. Keep each narration tight, 1-3 short Vietnamese sentences per scene.\n"
                f"The scene count is a SOFT TARGET (~{scenes}): you MAY use fewer or more "
                f"scenes as the content's natural structure requires. Do NOT pad or merge "
                f"real content just to hit the number.\n"
            )
        else:
            length_line = (
                "Retell the source in the channel's own voice, trimming rambling parts. If the "
                "source has more content than fits, SELECT the most important beats and CONDENSE "
                "them; DROP less-important detail. Do NOT pad. Do NOT cram. Keep each narration "
                "tight, 1-3 short Vietnamese sentences per scene.\n"
                f"The scene count is a SOFT TARGET (~{scenes}): you MAY use fewer or more "
                f"scenes as the content's natural structure requires. Do NOT pad or merge "
                f"real content just to hit the number.\n"
            )
    else:
        budget = _word_budget(req.durationSec)
        lo = round(budget * 0.9)
        hi = round(budget * 1.1)
        per_scene = max(1, budget // max(1, scenes))
        length_line = (
            f"Write a script ~{req.durationSec} seconds long, split into exactly {scenes} scenes.\n"
            f"WORD BUDGET (TARGET, must STICK to it): the TOTAL Vietnamese narration should be "
            f"about {budget} words (speaking pace ~{_VI_WORDS_PER_SEC} words/sec) — NOT under "
            f"{lo} words and NOT over {hi} words. On average ~{per_scene} words per scene.\n"
            f"ENOUGH LENGTH (important): if the source material is thinner than the budget, "
            f"EXPAND with analysis, context, meaning, examples, and the channel's own take (true "
            f"to 'your voice is the main content >60-80%') to hit the target — do NOT repeat "
            f"points, do NOT use empty hedging, do NOT pad with filler. If the material is longer: "
            f"condense, keep the most important parts. The goal is to match the length, not to "
            f"write short to play it safe.\n"
        )
    return (
        "You are a scriptwriter for a Vietnamese short-video channel that RE-EDITS foreign "
        "content (no plain reupload). Task: TRANSFORM the source material into a Vietnamese "
        "script in the channel's own writing voice — do NOT translate verbatim.\n\n"
        f"{guide}\n\n"
        "SAFETY RULES (mandatory):\n"
        "1. Real transformation — analyze, reorder, add a take; not just translation.\n"
        "2. Your voice is the main content (>60-80%); the source material is illustration only.\n"
        "3. The opening must have a strong hook in the first 3-5 seconds.\n"
        "4. Do not repeat the source dialogue verbatim.\n"
        f"5. {_KEEP_ENGLISH_TERMS}\n"
        f"6. {_CUT_PROMO}\n"
        f"7. {_PROPER_NOUN_DENSITY}\n\n"
        f"{title_line}"
        f"Source material (transcript{truncated}):\n\"\"\"\n{transcript}\n\"\"\"\n\n"
        f"{length_line}"
        "Each scene has: 'narration' = the spoken line, concise and natural; 'image_prompt' = a "
        "description of a vertical 9:16 frame, in ENGLISH, for the SDXL model (detailed, cinematic).\n"
        "Write all 'narration' in VIETNAMESE (the channel's own voice) — do NOT output English "
        f"narration. {_VI_FULL_TRANSLATION_RULE}\n"
        "Return ONLY a single valid JSON array, with no markdown or explanation. "
        'Each element: {"scene": <number starting at 1>, "narration": "<Vietnamese>", '
        '"image_prompt": "<English prompt>"}.'
    )


class TimedSegment(BaseModel):
    start: float
    end: float
    text: str


class TransformFootageRequest(_LlmChoiceMixin):
    segments: list[TimedSegment]       # timestamped source transcript (from ingest)
    editMode: str = "commentary"
    title: str | None = None
    durationSec: int = 60
    sceneCount: int | None = None
    windowSec: float | None = None     # clamp source ranges to [0, windowSec]
    auto: bool = False                 # AUTO: length follows the source, no fixed target
    skipScriptCache: bool = False      # bypass disk cache READ on every batch; WRITE still warms


def _build_footage_prompt(req: TransformFootageRequest, scenes: int, window: float,
                          window_start: float = 0.0, ratio_nudge: str | None = None,
                          total_window: float | None = None) -> str:
    """Build the footage script-gen prompt for the source sub-range
    [window_start, window] (window_start defaults to 0.0 = the whole window, the
    single-call case). When CHUNKING, each batch passes its own contiguous
    [window_start, window] sub-range so batches cover disjoint source time-windows
    and never overlap or duplicate footage. The model is told to keep sourceStart/
    sourceEnd INSIDE this sub-range.

    `total_window` is the WHOLE-video source span (all batches combined). It defaults
    to `window` (the single-call case, where the sub-range IS the whole video). In
    FIXED mode the word budget is the whole-video budget scaled by THIS batch's share
    of the timeline (sub-window seconds / total_window) — so B batches sum to ~the
    whole-video budget, NOT B× it (root-cause fix for the multi-batch fixed overshoot
    that blew job 45 to 4× words). AUTO mode is already per-batch correct (it derives
    its ceiling from this sub-window's `source_seconds`), so total_window is unused there.

    `ratio_nudge` (keep-ratio regen only): when set, the correction text is appended
    to the prompt so the retry steers the kept fraction back into the mode band."""
    guide = EDIT_MODE_GUIDE.get(req.editMode.lower())
    if not guide:
        raise HTTPException(422, f"Unknown editMode '{req.editMode}' (use: {', '.join(EDIT_MODE_GUIDE)})")
    # Hoisted to function scope: the return (intro/safety/closing override) and the AUTO
    # length_line branch both key off it, and FIXED mode reaches the return too.
    _mode_key = req.editMode.lower()
    lines, total = [], 0
    for s in req.segments:
        if s.start > window:
            break
        # Skip segments fully before this sub-window (chunked batches).
        if s.end < window_start:
            continue
        seg_lo = max(s.start, window_start)
        line = f"[{seg_lo:.1f}-{min(s.end, window):.1f}] {s.text.strip()}"
        total += len(line)
        if total > _SOURCE_TRANSCRIPT_CAP:
            break
        lines.append(line)
    transcript = "\n".join(lines)
    title_line = f"Source video title: {req.title}\n" if req.title else ""
    # AUTO: no fixed-seconds target — length follows the source; summary keeps most
    # of the original, trimming only redundant/filler segments. FIXED: ~N sec.
    if req.auto:
        # Source seconds available to THIS prompt (the whole window for a single call,
        # or this batch's contiguous sub-range when chunking). The HARD word ceiling is
        # derived from it so the natural-paced VO stays UNDER the source — the script-
        # side root-cause fix for "output longer than source". We cut content, never
        # speed up or truncate the voice.
        source_seconds = max(0.0, window - window_start)
        word_ceiling = _auto_word_ceiling(source_seconds)
        per_scene_cap = max(1, word_ceiling // max(1, scenes))
        _mode_key = req.editMode.lower()
        # Each mode gets its own isolated length_line — do NOT share the else branch
        # across modes. Adding a new mode = add a new elif here.
        if _mode_key == "summary":
            # SUMMARY: near-full faithful retell (76-90% source coverage).
            # Word ceiling is a PACING constraint only (VO under source duration) —
            # NOT a license to heavily condense. The previous wording ("source has
            # FAR MORE content → CONDENSE → select only important beats") was the
            # root cause of the 60% keep-ratio regression: the model interpreted it
            # as a condensing directive and skipped 40% of the source.
            length_line = (
                f"HARD WORD CAP (ABSOLUTE MAXIMUM): the TOTAL Vietnamese narration across ALL "
                f"scenes must be AT MOST {word_ceiling} words (keeps VO under source duration). "
                f"Aim CLOSE to the ceiling — writing significantly less means you have skipped "
                f"real content. (This source span is {source_seconds:.0f}s.)\n"
                f"COVERAGE MINIMUM (MANDATORY): the sourceStart/sourceEnd ranges of your scenes "
                f"MUST collectively span AT LEAST 75% of this window ({window_start:.0f}–{window:.0f}s). "
                f"Target 80%. Advance through the source CONTINUOUSLY — no large jumps; only "
                f"skip genuine filler sections (ads, subscribe/follow prompts, sponsor reads, "
                f"channel self-promo, bloated intros/outros, dead gaps, verbatim repetition).\n"
                f"NO REPETITION (MANDATORY): EVERY scene must advance NEW source material — never "
                f"repeat, restate, or re-summarize a point you already narrated in an earlier scene. "
                f"Re-narrating covered content burns the word cap WITHOUT increasing coverage, so the "
                f"script falls short of the coverage target. Move forward through the source only.\n"
                f"This is SUMMARY mode — near-full faithful retell: cover the ENTIRE window from "
                f"start to finish. Do NOT drop real arguments, examples, or details. Keep "
                f"chronological ORDER. Narrate each beat in 1-4 compact Vietnamese sentences.\n"
                f"PER-SCENE BUDGET: aim for about {per_scene_cap} words per scene. The scene "
                f"count is a SOFT TARGET (~{scenes}): use fewer or more as the content's natural "
                f"structure requires, but the total word cap above always wins.\n"
                f"REJECTION RULE: any script whose total narration EXCEEDS {word_ceiling} words "
                f"will be REJECTED outright. For EACH scene:\n"
            )
        elif _mode_key == "recap":
            # RECAP: condensed curated retell (60-75% source coverage).
            length_line = (
                f"HARD WORD CAP (ABSOLUTE MAXIMUM, NOT a target): the TOTAL Vietnamese narration "
                f"across ALL scenes must be AT MOST {word_ceiling} words. This is a strict ceiling, "
                f"not a goal — fewer is fine, more is FORBIDDEN. (This source span is "
                f"{source_seconds:.0f}s; staying under the cap keeps the finished video shorter than "
                f"the source.)\n"
                f"This is RECAP mode — condensed curated retell (60-75% of source): SELECT the "
                f"most important beats; DROP redundancy, slow stretches, and minor asides. You MAY "
                f"reorder for clarity and add a light layer of analysis, but preserve the "
                f"substantive story. Keep the source's time range ({window_start:.0f}–{window:.0f}s).\n"
                f"NO REPETITION (MANDATORY): EVERY scene must cover NEW source material — never "
                f"repeat, restate, or re-summarize a beat you already narrated in an earlier scene. "
                f"Re-narrating covered content burns the word cap WITHOUT increasing coverage, so the "
                f"kept-ratio falls short of the target. Keep moving forward through the source.\n"
                f"PER-SCENE BUDGET: aim for about {per_scene_cap} words per scene — keep each "
                f"narration tight, 1-3 short Vietnamese sentences. The scene count is a SOFT TARGET "
                f"(~{scenes}): use fewer or more as the content's natural structure requires, but the "
                f"total word cap above always wins.\n"
                f"REJECTION RULE: any script whose total narration EXCEEDS {word_ceiling} words will "
                f"be REJECTED outright. For EACH scene:\n"
            )
        elif _mode_key == "commentary":
            # COMMENTARY: analysis + opinion (original-length, not source-tracking).
            length_line = (
                f"HARD WORD CAP (ABSOLUTE MAXIMUM, NOT a target): the TOTAL Vietnamese narration "
                f"across ALL scenes must be AT MOST {word_ceiling} words. This is a strict ceiling, "
                f"not a goal — fewer is fine, more is FORBIDDEN. (This source span is "
                f"{source_seconds:.0f}s; staying under the cap keeps the finished video shorter than "
                f"the source.)\n"
                f"This is COMMENTARY mode — analysis + personal take (NOT source-tracking): do NOT "
                f"follow the source line-by-line. Explain it, give a clear judgment, and analyze in "
                f"depth. You MAY freely reorder the material to serve your argument. Your analysis "
                f"is the core content; original footage is illustration only (≤20-40%). Source "
                f"range: {window_start:.0f}–{window:.0f}s.\n"
                f"PER-SCENE BUDGET: aim for about {per_scene_cap} words per scene — keep each "
                f"narration tight, 1-3 short Vietnamese sentences. The scene count is a SOFT TARGET "
                f"(~{scenes}): use fewer or more as the content's natural structure requires, but the "
                f"total word cap above always wins.\n"
                f"REJECTION RULE: any script whose total narration EXCEEDS {word_ceiling} words will "
                f"be REJECTED outright. For EACH scene:\n"
            )
        elif _mode_key == "educational":
            # EDUCATIONAL: lesson/how-to/explanation (original-length, not source-tracking).
            length_line = (
                f"HARD WORD CAP (ABSOLUTE MAXIMUM, NOT a target): the TOTAL Vietnamese narration "
                f"across ALL scenes must be AT MOST {word_ceiling} words. This is a strict ceiling, "
                f"not a goal — fewer is fine, more is FORBIDDEN. (This source span is "
                f"{source_seconds:.0f}s; staying under the cap keeps the finished video shorter than "
                f"the source.)\n"
                f"This is EDUCATIONAL mode — turn it into a lesson (NOT source-tracking): do NOT "
                f"follow the source line-by-line. Restructure the material into a clear teaching "
                f"arc (lesson, how-to, psychology, why it matters). Add analysis framed as the "
                f"lesson being taught. Your explanation leads and is the main content; original "
                f"footage is illustration only. Source range: {window_start:.0f}–{window:.0f}s.\n"
                f"PER-SCENE BUDGET: aim for about {per_scene_cap} words per scene — keep each "
                f"narration tight, 1-3 short Vietnamese sentences. The scene count is a SOFT TARGET "
                f"(~{scenes}): use fewer or more as the content's natural structure requires, but the "
                f"total word cap above always wins.\n"
                f"REJECTION RULE: any script whose total narration EXCEEDS {word_ceiling} words will "
                f"be REJECTED outright. For EACH scene:\n"
            )
        elif _mode_key == "translate_full":
            # TRANSLATE_FULL: full localization (85-100% retention). NO hard cap and NO
            # rejection rule — full content retention OVERRIDES the soft word target, so a
            # dense source is never truncated. Coverage must tile the real content in order.
            length_line = (
                f"FULL CONTENT RETENTION (MANDATORY): re-tell EVERY point, fact, example, and "
                f"number in this window — never summarize, condense, or drop substantive content. "
                f"If you are unsure whether something matters, KEEP it. Cut ONLY junk (ads, sponsor "
                f"reads, like/subscribe/follow prompts, channel self-promo, source credit/"
                f"attribution/watermark call-outs, bloated intros/outros, silent/dead gaps).\n"
                f"NATURAL PACING: write natural spoken Vietnamese at a calm, normal speaking pace — "
                f"do NOT cram words, and NEVER assume the voice will be sped up to fit.\n"
                f"SOFT WORD TARGET (guidance ONLY, not a cap): about {word_ceiling} words total for "
                f"this {source_seconds:.0f}s window is a rough guide — but FULL RETENTION OVERRIDES "
                f"it: if keeping all the content needs more words, WRITE MORE. There is NO hard cap "
                f"and NO rejection for length.\n"
                f"COVERAGE (MANDATORY): your scenes' sourceStart/sourceEnd ranges MUST TILE the real "
                f"content of this window ({window_start:.0f}–{window:.0f}s) CONTIGUOUSLY — no gaps "
                f"except over junk. Advance through the source IN ORDER from start to finish.\n"
                f"NO REPETITION (MANDATORY): every scene advances NEW source material — never repeat, "
                f"restate, or re-summarize a point already narrated in an earlier scene.\n"
                f"PER-SCENE BUDGET: aim for about {per_scene_cap} words per scene, 1-4 compact "
                f"Vietnamese sentences. The scene count is a SOFT TARGET (~{scenes}): use MORE scenes "
                f"if full retention requires it — retention always wins over the target. For EACH scene:\n"
            )
        else:
            # Unknown/future mode — generic fallback. Add a new elif above for any new mode.
            length_line = (
                f"HARD WORD CAP (ABSOLUTE MAXIMUM, NOT a target): the TOTAL Vietnamese narration "
                f"across ALL scenes must be AT MOST {word_ceiling} words. This is a strict ceiling, "
                f"not a goal — fewer is fine, more is FORBIDDEN. (This source span is "
                f"{source_seconds:.0f}s; staying under the cap keeps the finished video shorter than "
                f"the source.)\n"
                f"If the source has more content than fits, SELECT the most important beats and "
                f"CONDENSE; DROP less-important detail and rambling. Do NOT pad. Do NOT cram. "
                f"Source range: {window_start:.0f}–{window:.0f}s.\n"
                f"PER-SCENE BUDGET: aim for about {per_scene_cap} words per scene — keep each "
                f"narration tight, 1-3 short Vietnamese sentences. The scene count is a SOFT TARGET "
                f"(~{scenes}): use fewer or more as the content's natural structure requires, but the "
                f"total word cap above always wins.\n"
                f"REJECTION RULE: any script whose total narration EXCEEDS {word_ceiling} words will "
                f"be REJECTED outright. For EACH scene:\n"
            )
    else:
        # FIXED mode: the word budget is for the WHOLE video. Each batch covers only a
        # contiguous slice [window_start, window] of the source, so this batch must only
        # target its SHARE of the budget — the whole-video budget scaled by this
        # sub-window's fraction of the total timeline. Without this division every batch
        # independently chased the full-video budget → B batches produced ~B× the words
        # (the job-45 overshoot: 4 batches × ~665 = 2661 words). `total_window` defaults
        # to `window` for the single-call case (fraction == 1.0, whole-video budget).
        _total = total_window if (total_window and total_window > 0) else window
        _sub_seconds = max(0.0, window - window_start)
        _fraction = (_sub_seconds / _total) if _total > 0 else 1.0
        # Clamp to (0, 1]: never below a tiny floor (avoid a 0-word batch) nor above the
        # whole budget (a single batch should never exceed the whole-video target).
        _fraction = min(1.0, max(1e-6, _fraction))
        whole_budget = _word_budget(req.durationSec)
        budget = max(1, round(whole_budget * _fraction))
        lo = round(budget * 0.9)
        hi = round(budget * 1.1)
        per_scene = max(1, budget // max(1, scenes))
        # `req.durationSec` is the WHOLE-video target; this batch's slice is only a part
        # of it, so frame the seconds/scene-count to THIS batch (its sub-window seconds)
        # and keep the "TOTAL across ALL scenes" wording accurate to the per-batch amount.
        length_line = (
            f"Write the narration for THIS batch covering source {window_start:.0f}-{window:.0f}s "
            f"(~{_sub_seconds:.0f}s of the video), split into exactly {scenes} scenes.\n"
            f"WORD BUDGET (TARGET, must STICK to it): the TOTAL Vietnamese narration for THESE "
            f"{scenes} scenes should be about {budget} words (speaking pace ~{_VI_WORDS_PER_SEC} "
            f"words/sec) — NOT under {lo} words and NOT over {hi} words, on average ~{per_scene} "
            f"words per scene.\n"
            f"ENOUGH LENGTH (important): if the source material is thinner than the budget, "
            f"EXPAND with analysis, context, meaning, examples, and the channel's own take (your "
            f"voice is the main content >60-80%) to hit the target — do NOT repeat points, do NOT "
            f"use empty phrasing, do NOT pad. If it is longer: condense, keep the most important "
            f"parts. The goal is to match the length. For EACH scene:\n"
        )
    # translate_full OVERRIDES the shared intro + safety rules 1/2 + the closing line: it is
    # a FULL localization (re-tell everything in order over muted footage), NOT a
    # transform/re-edit. Rules 3-6 (hook, KEEP_ENGLISH, CUT_PROMO, PROPER_NOUN) are shared.
    if _mode_key == "translate_full":
        intro = (
            "You are a scriptwriter who FULLY LOCALIZES a foreign video for a Vietnamese "
            "short-video channel. The source video is shown MUTED while your Vietnamese "
            "narration RE-TELLS ALL of its content in the channel's own natural voice. Below is "
            "the source transcript with timestamps (seconds).\n\n"
        )
        rule1 = ("1. FULL localization in a natural spoken voice — re-tell EVERY point, fact, and "
                 "number in the ORIGINAL order; do NOT drop, condense, or reorder content.\n")
        rule2 = ("2. This is a full re-voice of the whole video (owner-accepted): keep ALL "
                 "substantive content and cut ONLY junk (ads, sponsor, like/subscribe, self-promo, "
                 "source credit/attribution/watermark call-outs, bloated intro/outro, dead gaps).\n")
        closing = ("Cover the real content in the ORIGINAL order — do NOT reorder. Your scene "
                   "windows must TILE the content contiguously (skip only junk).\n")
    else:
        intro = (
            "You are a scriptwriter for a Vietnamese short-video channel that RE-EDITS foreign "
            "content. The source material (footage) is only ILLUSTRATION; your Vietnamese "
            "commentary is the main content. Below is the source transcript with timestamps (seconds).\n\n"
        )
        rule1 = "1. Real transformation — analyze, reorder, add a take; do NOT translate verbatim.\n"
        rule2 = "2. Your voice is the main content (>60-80%); the source footage is illustration only.\n"
        closing = "Pick the worthwhile moments; you may reorder them for a better narrative flow.\n"
    return (
        intro
        + f"{guide}\n\n"
        "SAFETY RULES (mandatory):\n"
        + rule1
        + rule2
        + "3. The opening has a strong hook in the first 3-5 seconds.\n"
        f"4. {_KEEP_ENGLISH_TERMS}\n"
        f"5. {_CUT_PROMO_FOOTAGE}\n"
        f"6. {_PROPER_NOUN_DENSITY}\n\n"
        f"{title_line}"
        f"Source transcript (seconds, only within {window_start:.0f}-{window:.0f}s):\n\"\"\"\n{transcript}\n\"\"\"\n\n"
        f"{length_line}"
        "- 'narration': the spoken Vietnamese line, concise and natural (your commentary/recap). "
        f"Write all 'narration' in VIETNAMESE (the channel's own voice) — do NOT output English narration. {_VI_FULL_TRANSLATION_RULE}\n"
        "- 'sourceStart','sourceEnd': the time range (seconds) in the transcript above to TAKE "
        f"illustrative footage for that scene — must lie within {window_start:.0f}-{window:.0f}s, each clip ~3-8 seconds.\n"
        + closing
        + (f"\n{ratio_nudge}\n" if ratio_nudge else "")
        + "Return ONLY a single valid JSON array, no markdown. Each element: "
        '{"scene": <number from 1>, "narration": "<Vietnamese>", "sourceStart": <seconds>, "sourceEnd": <seconds>}.'
    )


def _gen_footage_scenes(req: TransformFootageRequest, scene_count: int, window: float,
                        ratio_nudge: str | None = None) -> list:
    """Generate the footage scene array, CHUNKING into batches when scene_count exceeds
    SCRIPT_GEN_CHUNK_SCENES. Each batch maps to a CONTIGUOUS source time sub-window:
    the 0..window span is split into B equal sub-windows, batch i generates its slice's
    scenes with sourceStart/sourceEnd inside [subStart, subEnd]. Batches are merged in
    order and scene ids renumbered 1..N. A failed batch retries ON ITS OWN (the per-batch
    _run_claude_script 504-retry); already-completed batches are NOT discarded.

    Degenerate case (scene_count <= chunk size): ONE call over the whole window — the
    original single-call behavior, no chunking overhead.

    `ratio_nudge` (keep-ratio regen only): forwarded to every batch's prompt so a retry
    steers the kept fraction back into the mode band."""
    chunk = _chunk_for_mode(req.editMode)
    batches = _batch_count(scene_count, chunk)
    # Per-job force-fresh script-gen: skip the disk cache READ on every batch (the
    # cache WRITE inside _run_claude_script still fires on success). Default FALSE
    # keeps the cache-read path identical to today.
    force_regen = getattr(req, "skipScriptCache", False)
    # SOURCE-LENGTH BATCH CAP (owner mechanisms 1 & 2). `_batch_count` only bounds
    # batches by the DECODE-time chunk size (scenes per `claude -p` call); it does not
    # know how much source actually exists. Batches partition [0, window] into `batches`
    # equal sub-windows; a batch whose sub-window lies entirely past the real content
    # span carries NO transcript and would make the LLM hallucinate scenes for an empty
    # range. So clamp `batches` down to the MINIMUM the source length needs: the number
    # of equal sub-windows of width (window/batches) that the content span actually
    # reaches. This both (1) collapses to a single batch when the whole source fits one
    # sub-window and (2) keeps just-enough, contiguous, non-overlapping batches — never
    # excess. The clamp can only LOWER the count, so it never violates the per-batch
    # scene-chunk ceiling.
    if batches > 1 and req.segments:
        _actual_span = max((s.end for s in req.segments), default=0.0)
        _span = window / batches
        if _span > 0 and _actual_span > 0:
            # Smallest B in [1, batches] whose B equal sub-windows are all reached by the
            # content. With fixed sub-window width `_span`, content reaches sub-window i
            # (0-based) iff _actual_span > i*_span, so the count of non-empty sub-windows
            # is ceil(_actual_span / _span). A 10% tolerance absorbs trailing silence.
            needed = max(1, math.ceil((_actual_span * 1.1) / _span))
            needed = min(needed, batches)
            if needed < batches:
                log.info(
                    "[generate] source-length batch cap: content span %.1fs over window "
                    "%.1fs needs only %d of %d batches (sub-window %.1fs) — collapsing %d → %d",
                    _actual_span, window, needed, batches, _span, batches, needed,
                )
                batches = needed
    # Cache key parts shared by every batch of this job. ratio_nudge is included so a
    # keep-ratio regen (which passes a DIFFERENT nudge) never collides with the first
    # pass's cached entry; req.durationSec stands in for the word budget the prompt
    # derives. The per-batch source window + index are added per batch below.
    _ck_base = {
        "edit_mode": (req.editMode or "").lower(),
        "word_budget": _word_budget(req.durationSec),
        "ratio_nudge": ratio_nudge or "",
    }
    # The job's per-job LLM choice, forwarded to every batch. {} when unset -> the call
    # shape below is unchanged from before the provider gate.
    llm_kw = _llm_kwargs(getattr(req, "llmProvider", None), getattr(req, "llmModel", None))
    if batches == 1:
        # Single call over the whole window: total_window == window, fraction == 1.0,
        # so FIXED gets the full whole-video budget (no division).
        prompt = _build_footage_prompt(req, scene_count, window, ratio_nudge=ratio_nudge)
        cache_parts = {**_ck_base, "source_transcript_window": [0.0, window],
                       "batch_index": 0}
        return _run_claude_script(prompt, timeout=SCRIPT_GEN_TIMEOUT,
                                  cache_parts=cache_parts, batch_idx=0,
                                  force_regen=force_regen, **llm_kw)

    per_batch = _split_counts(scene_count, batches)
    span = window / batches
    # Build the ORDERED list of per-batch prompts (same sub-window math as before), then
    # run them CONCURRENTLY. _run_batches_parallel preserves input order, so the merged
    # scene order is identical to the old sequential loop — only the timing changes.
    # Sub-windows are [i*span, (i+1)*span) — contiguous, disjoint, covering [0, window]
    # exactly once. Each batch's FIXED budget is the whole-video budget scaled by its
    # sub-window fraction (total_window=window), so the B per-batch budgets SUM to ~the
    # whole-video budget instead of each chasing the full target (job-45 4× overshoot).
    prompts = []
    cache_parts: list[dict] = []
    for i, n_scenes in enumerate(per_batch):
        sub_start = i * span
        sub_end = window if i == batches - 1 else (i + 1) * span
        log.info(
            "[generate] footage batch %d/%d: %d scenes over %.0f-%.0fs",
            i + 1, batches, n_scenes, sub_start, sub_end,
        )
        # Build this batch's own footage prompt over its contiguous source sub-window
        # [sub_start, sub_end] so each batch covers a disjoint time slice (no overlap /
        # duplicate footage); forward ratio_nudge so a keep-ratio regen steers every batch.
        prompts.append(_build_footage_prompt(
            req, n_scenes, sub_end, window_start=sub_start, ratio_nudge=ratio_nudge,
            total_window=window,
        ))
        cache_parts.append({**_ck_base,
                            "source_transcript_window": [sub_start, sub_end],
                            "batch_index": i})
    # Each batch is independently retryable; if one fails (after its own retries) the
    # whole gen aborts fail-fast (the others are drained, the first error re-raised).
    return _merge_renumber(_run_batches_parallel(prompts, cache_parts=cache_parts,
                                                 force_regen=force_regen,
                                                 llm_kwargs=llm_kw))


def _clamp_footage_scenes(scenes: list, window: float) -> list:
    """Clamp raw LLM scene ranges into [0, window], drop hallucinated scenes whose
    start lies at/beyond the source window, and normalize each kept scene to
    {scene, narration, sourceStart, sourceEnd}. Pure (no LLM) — safe to re-run on
    every regen attempt."""
    clean = []
    for s in scenes:
        try:
            st = max(0.0, float(s.get("sourceStart", 0)))
            en = min(window, float(s.get("sourceEnd", st + 5)))
        except (TypeError, ValueError):
            st, en = 0.0, min(window, 5.0)
        # Drop hallucinated scenes whose start lies at/beyond the real source window
        # (e.g. the LLM drifting to fabricated timestamps like 1800s on a 500s video).
        # Such a scene carries no real footage and no recoverable timecode → dropping it
        # is more honest than emitting a degenerate start>=end clip, and keeps the saved
        # script JSONB clean for any future script-reuse job.
        if window > 0 and st >= window:
            log.warning("[generate] dropping hallucinated footage scene %s: sourceStart=%.2f >= window=%.2f",
                        s.get("scene"), st, window)
            continue
        if en <= st:
            en = min(window, st + 4.0)
        clean.append({"scene": s.get("scene"), "narration": s.get("narration", ""),
                      "sourceStart": round(st, 2), "sourceEnd": round(en, 2)})
    return clean


def _check_keep_ratio(mode: str, clean: list, window: float):
    """Coverage keep-ratio check (plan §B.1). Returns (ratio, in_band, hint|None).

    ratio = Σ(sourceEnd − sourceStart) / window over the cleaned scenes — the
    language-neutral fraction of source SECONDS covered. `mode`s without a band
    (commentary/educational/dubbed/unknown) are never enforced: in_band=True,
    hint=None. A non-positive window can't yield a fair denominator → ratio 0.0,
    in_band=True (never block on a bad window), and we log it.

    When out of band, `hint` is the verbatim regen-nudge string ready to append to
    the next prompt; the over/under clause is chosen by comparing ratio to the band."""
    band = _KEEP_RATIO_BAND.get((mode or "").lower())
    if band is None:
        return 0.0, True, None  # mode has no band → no enforcement
    if window <= 0:
        log.warning("[generate] keep-ratio: non-positive window=%.2f for mode '%s' — skipping enforcement",
                    window, mode)
        return 0.0, True, None
    total_kept = sum(s["sourceEnd"] - s["sourceStart"] for s in clean)
    ratio = total_kept / window
    lo, hi = band
    in_band = lo <= ratio <= hi
    if in_band:
        return ratio, True, None
    pct = round(ratio * 100)
    lo_pct, hi_pct = round(lo * 100), round(hi * 100)
    clause = _RATIO_REGEN_OVER if ratio > hi else _RATIO_REGEN_UNDER
    hint = _RATIO_REGEN_NUDGE.format(pct=pct, mode=(mode or "").lower(),
                                     lo=lo_pct, hi=hi_pct, direction_clause=clause)
    return ratio, False, hint


@router.post("/generate/script/footage")
def generate_script_footage(req: TransformFootageRequest):
    if not req.segments:
        raise HTTPException(422, "Provide source 'segments' (from /generate/ingest).")
    window = req.windowSec or max((s.end for s in req.segments), default=0)
    scene_count = req.sceneCount or _auto_scene_count(req.durationSec, req.auto, req.editMode)
    # NOTE: this runs ONLY on FRESH generation. When a job reuses a prior script
    # (reuse_script_video_id), runner.py bypasses this endpoint entirely, so the
    # keep-ratio loop below never fires on reuse — it only ever steers brand-new gen.
    mode = req.editMode.lower()

    # First attempt. Clamp ranges into the available window so downstream cuts never
    # seek past it, then measure source-coverage against the mode band (§B.1).
    scenes = _gen_footage_scenes(req, scene_count, window)
    clean = _clamp_footage_scenes(scenes, window)
    ratio, in_band, hint = _check_keep_ratio(mode, clean, window)

    # Bounded REGEN (§B.2) — footage path only, and only for modes that HAVE a band
    # (recap/summary). Out of band → re-generate up to _RATIO_REGEN_ATTEMPTS more
    # times, each passing the correction nudge into the prompt. Every regen is another
    # `claude -p` call (subscription cost), so this is hard-bounded. We keep the
    # CLOSEST-to-band attempt; if none lands in-band we proceed with the closest one
    # (owner Q4: log + proceed, NOT a hard fail — never waste a completed job over a
    # soft content target).
    # Regen is restricted to recap/summary ONLY. commentary/educational are
    # original-length modes with no keep-ratio band to chase, so an extra `claude -p`
    # pass would just burn a subscription call without a target to converge toward —
    # accept whatever coverage the first pass produced.
    _regen_modes = {"recap", "summary", "translate_full"}
    has_band = _KEEP_RATIO_BAND.get(mode) is not None and mode in _regen_modes
    if not in_band and mode not in _regen_modes:
        log.info(f"[script] skipping regen for edit_mode={mode} (not recap/summary/translate_full)")
    if not in_band and has_band:
        lo, hi = _KEEP_RATIO_BAND[mode]

        def _dist(r: float) -> float:
            # distance from the band [lo, hi]; 0.0 when inside.
            return 0.0 if lo <= r <= hi else (lo - r if r < lo else r - hi)

        best_clean, best_ratio, best_dist = clean, ratio, _dist(ratio)
        log.info("[generate] keep-ratio: mode '%s' attempt 1 kept %.1f%% (band %d-%d%%) — out of band, regenerating",
                 mode, ratio * 100, round(lo * 100), round(hi * 100))
        for attempt in range(1, _RATIO_REGEN_ATTEMPTS + 1):
            log.info("[generate] keep-ratio regen attempt %d/%d for mode '%s' (extra claude -p call)",
                     attempt, _RATIO_REGEN_ATTEMPTS, mode)
            scenes = _gen_footage_scenes(req, scene_count, window, ratio_nudge=hint)
            clean = _clamp_footage_scenes(scenes, window)
            ratio, in_band, hint = _check_keep_ratio(mode, clean, window)
            d = _dist(ratio)
            # PERF GUARD (regression: job 14 ran ALL regen passes stuck at 11.6%,
            # band 76-90%, wasting ~8 concurrent `claude -p` calls + 300s timeouts
            # per pass without ever getting closer). If a regen does NOT measurably
            # REDUCE the band-distance vs the best seen so far, the nudge isn't
            # converging — stop early and proceed with the closest attempt rather
            # than burning the remaining passes. _RATIO_REGEN_MIN_IMPROVE is a small
            # epsilon so negligible jitter still counts as "no progress".
            improved = d < best_dist - _RATIO_REGEN_MIN_IMPROVE
            if d < best_dist:
                best_clean, best_ratio, best_dist = clean, ratio, d
            if in_band:
                log.info("[generate] keep-ratio: mode '%s' landed in band at %.1f%% on regen %d/%d",
                         mode, ratio * 100, attempt, _RATIO_REGEN_ATTEMPTS)
                break
            if not improved:
                log.warning(
                    "[generate] keep-ratio: mode '%s' regen %d/%d did NOT improve "
                    "(kept %.1f%%, best %.1f%%, band %d-%d%%) — stopping early to avoid "
                    "wasting the remaining %d pass(es); proceeding with CLOSEST attempt.",
                    mode, attempt, _RATIO_REGEN_ATTEMPTS, ratio * 100, best_ratio * 100,
                    round(lo * 100), round(hi * 100), _RATIO_REGEN_ATTEMPTS - attempt,
                )
                clean = best_clean
                break
        else:
            # Attempts exhausted, still out of band → proceed with the closest attempt.
            log.warning(
                "[generate] keep-ratio: mode '%s' never reached band %d-%d%% after %d regens; "
                "proceeding with CLOSEST attempt at %.1f%% (owner Q4: log + proceed, no hard fail).",
                mode, round(lo * 100), round(hi * 100), _RATIO_REGEN_ATTEMPTS, best_ratio * 100,
            )
            clean = best_clean

    # POST-GEN WORD-CEILING ENFORCEMENT (bug3 root-cause fix). AUTO source-tracking
    # modes (summary/recap) must fit the source: the natural-paced VO of an over-long
    # script is what blew job-14 to 54 min and forced the hard VO trim. The ceiling is
    # derived from the SAME source span fed to the prompt (window), so prompt-budget
    # and enforcement agree. FIXED mode has its own word budget in the prompt and is
    # not source-tracked, so it is exempt here. On overshoot we FAIL (HTTPException),
    # never auto-truncate the narration or speed up the voice (owner rule).
    if req.auto:
        _enforce_word_ceiling(mode, clean, window)
    else:
        # FIXED-mode FAST-FAIL (owner mechanism 3 backstop). The per-batch budget
        # division above keeps the FIXED total near the whole-video target by
        # construction, but a model overshoot (or a target longer than the source can
        # physically hold) would otherwise only surface at the 99% assembly duration
        # gate (runner._enforce_duration_guard) after ~50 min of TTS/whisper/render.
        # Catch it HERE, cheaply, at script-gen: for a source-derived footage job the
        # natural-paced VO must still fit UNDER the source span, so reject when the
        # total narration exceeds the SAME source-length ceiling the AUTO path uses
        # (window seconds → words), times the shared tolerance. This never speeds up or
        # truncates the voice — it fails fast and asks for a shorter script. Skips when
        # the source span is unknown (no honest ceiling to compare against).
        _enforce_fixed_source_fit(clean, window)

    # PRIMARY duration fail-fast (owner item 2): the CHEAPEST gate of all three layers.
    # Estimate the finished VO seconds straight from the word count at the measured pace
    # (+ credit slate) and FAIL now (zero tolerance) if it would meet/exceed the source —
    # before a single TTS subprocess runs. Gated to SOURCE-TRACKING modes only, exactly
    # like the runtime gates (runner._mode_tracks_source) and the word-ceiling checks, so
    # original-length modes (commentary/educational) are never false-failed. Runs for both
    # AUTO and FIXED of those modes. The word-ceiling checks above bound the WORD COUNT;
    # this bounds the ESTIMATED SECONDS — the same quantity the post-TTS gate later
    # measures for real.
    if mode in _WORD_CEILING_MODES:
        _enforce_script_duration(clean, window)

    return {"editMode": mode, "sceneCount": len(clean), "scenes": clean,
            **_llm_used(req.llmProvider, req.llmModel)}


def _build_visual_explain_prompt(frames: list[dict], title: str, description: str,
                                 tags: list, scenes: int, source_dur: float,
                                 budget: int, per_scene: int, pace: float) -> str:
    """Build the single-call VISION prompt for the no-speech visual-explain fallback.

    The source has audio but NO speech, so there is no transcript to rewrite. Instead we
    give Claude the sampled frame IMAGES (absolute paths, timestamp-tagged, sorted) and
    the source metadata, and ask it to OPEN every frame with the Read tool, reconstruct
    the on-screen story, and write a Vietnamese explainer voiceover that TILES the whole
    0-source_dur timeline. Instructions are English (token discipline) but Vietnamese
    narration output is MANDATED; the shared KEEP_ENGLISH / PROPER_NOUN steering constants
    are reused verbatim. Metadata lines are empty strings when absent (never fabricated),
    and the CUT-promo clause is deliberately NOT appended (a no-speech source has no ad/
    subscribe reads to drop)."""
    # One line per frame: absolute path + source timestamp, in timestamp order (frames
    # arrive pre-sorted from frames_util.sample_frames, but sort defensively).
    frame_lines = "\n".join(
        f"- [{f['tsSec']:.1f}s] {f['path']}"
        for f in sorted(frames, key=lambda x: x["tsSec"])
    )
    # Metadata: empty string when absent so the prompt never fabricates context.
    title_line = f"Title: {title}\n" if title else ""
    desc_line = f"Description: {description}\n" if description else ""
    _tags = ", ".join(t for t in (tags or []) if t) if tags else ""
    tags_line = f"Tags: {_tags}\n" if _tags else ""
    return (
        "You are a scriptwriter for a Vietnamese short-video channel that produces "
        "EXPLANATORY COMMENTARY over foreign footage. This source video has audio but NO "
        "speech (music/SFX only), so there is no transcript. Your job: understand what is "
        "actually happening ON SCREEN and write a Vietnamese explainer voiceover about it.\n\n"
        "The source footage is ONLY ILLUSTRATION. Your Vietnamese commentary is the main "
        "content (>60-80%): you explain, interpret, and add insight — you never just label "
        "what is on screen. This keeps the video transformative and copyright-safe (no "
        "verbatim reupload; the footage supports YOUR narration, not the reverse).\n\n"
        "STEP 1 — LOOK AT THE VIDEO. Below is a list of sampled frame IMAGES, each tagged "
        "with its timestamp in the source. You MUST OPEN EVERY FRAME with the Read tool and "
        "actually look at it. Do NOT guess from the title alone. Read them in timestamp "
        "order and reconstruct the real visual narrative: what is shown, what changes "
        "between frames, cause and effect, the beginning -> middle -> end of what happens.\n\n"
        "Frames (real image files — open EACH one with the Read tool):\n"
        f"{frame_lines}\n\n"
        "Context (metadata — supporting only, NEVER a substitute for looking):\n"
        f"{title_line}{desc_line}{tags_line}\n"
        f"STEP 2 — WRITE THE SCRIPT. Produce {scenes} scenes (SOFT target) that TILE the "
        f"whole source timeline 0-{source_dur:.0f}s in order: scene 1 starts at 0, each "
        f"scene's sourceStart continues where the previous sourceEnd left off, and the last "
        f"scene's sourceEnd = {source_dur:.0f}. No gaps, no overlaps, strictly increasing. "
        "Anchor each scene's sourceStart/sourceEnd to the frame timestamps that show the "
        "beat it narrates.\n\n"
        "NARRATION RULES:\n"
        "1. Write every 'narration' in natural, engaging, spoken VIETNAMESE — the channel's "
        f"own voice. Do NOT output English narration. {_VI_FULL_TRANSLATION_RULE} Do NOT write frame-by-frame captions "
        "like \"frame 1 shows...\"; write a flowing explainer that tells and interprets the "
        "story unfolding on screen.\n"
        "2. Open with a strong hook in the first 3-5 seconds.\n"
        "3. Your explanation leads and carries the meaning; the footage merely illustrates it.\n"
        f"4. {_KEEP_ENGLISH_TERMS}\n"
        f"5. {_PROPER_NOUN_DENSITY}\n\n"
        f"HARD WORD CAP (ABSOLUTE MAXIMUM, NOT a target): the TOTAL Vietnamese narration "
        f"across ALL scenes must be AT MOST {budget} words (calm speaking pace ~{pace} "
        f"words/sec) — this keeps the finished voiceover shorter than the source. Aim for "
        f"about {per_scene} words per scene, 1-3 short sentences each. NEVER cram extra "
        f"words to fill time and NEVER assume the voice will be sped up — fewer words is "
        f"fine, more is FORBIDDEN. Any script whose total narration EXCEEDS {budget} words "
        f"will be REJECTED.\n\n"
        "For EACH scene output:\n"
        "- 'scene': the scene number starting at 1.\n"
        "- 'narration': the spoken Vietnamese explainer line for this scene.\n"
        f"- 'sourceStart','sourceEnd': the source time range (seconds, within 0-{source_dur:.0f}) "
        "whose footage illustrates this narration; contiguous with neighbours as described above.\n\n"
        "Return ONLY a single valid JSON array, no markdown. Each element: "
        '{"scene": <number from 1>, "narration": "<Vietnamese>", "sourceStart": <seconds>, '
        '"sourceEnd": <seconds>}.'
    )


def _tile_normalize_scenes(scenes: list, source_dur: float) -> list:
    """DETERMINISTIC timeline repair for visual-explain scenes: the LLM anchors
    sourceStart/sourceEnd to frame timestamps but can leave small gaps/overlaps. Sort by
    sourceStart, force a contiguous tiling of [0, source_dur] (scene 0 starts at 0, each
    start = previous end, last end = source_dur), and DROP any resulting non-positive
    slice. Pure — no LLM. Mutates copies in-place and returns the kept scenes."""
    if not scenes:
        return []
    ordered = sorted(scenes, key=lambda s: float(s.get("sourceStart", 0) or 0))
    out: list = []
    prev_end = 0.0
    for i, s in enumerate(ordered):
        st = 0.0 if i == 0 else prev_end
        try:
            en = float(s.get("sourceEnd", st) or st)
        except (TypeError, ValueError):
            en = st
        # The end never runs backwards past the tiled start; the last scene closes at
        # the full source duration so the footage covers the whole video.
        if i == len(ordered) - 1:
            en = source_dur
        en = max(en, st)
        if en <= st:
            # Non-positive slice (a scene the tiling collapsed to zero width) — drop it.
            continue
        s["sourceStart"] = round(st, 2)
        s["sourceEnd"] = round(en, 2)
        out.append(s)
        prev_end = en
    return out


def _ve_parse_and_clean(text: str, src: float, jobId: int | None) -> list:
    """Parse a visual-explain vision result into a validated footage scene list:
    extract the JSON array, clamp ranges into [0, src] + normalize shape
    (_clamp_footage_scenes), reject a thin/blank script (422), then deterministically
    tile-normalize the timeline. Shared by the initial call AND the shrink re-prompt so
    both drafts get identical validation. Raises the user-facing Vietnamese 422 on
    unparseable or too-thin output."""
    try:
        arr = _extract_json_array(text)
    except (json.JSONDecodeError, ValueError):
        log.warning("[generate] job %s visual-explain: could not parse JSON from vision "
                    "result: %s", jobId, (text or "")[:300])
        raise HTTPException(422, "Không tạo được kịch bản từ hình ảnh — thử lại")
    clean = _clamp_footage_scenes(arr, window=src)
    if len(clean) < 3 or any(not (s.get("narration") or "").strip() for s in clean):
        log.warning("[generate] job %s visual-explain: script too thin (%d scenes) — "
                    "failing honestly", jobId, len(clean))
        raise HTTPException(422, "Không tạo được kịch bản từ hình ảnh — thử lại")
    clean = _tile_normalize_scenes(clean, src)
    if len(clean) < 3:
        raise HTTPException(422, "Không tạo được kịch bản từ hình ảnh — thử lại")
    return clean


# Sentence boundary for the deterministic visual-explain trim (Vietnamese uses the same
# terminal punctuation as English). Split AFTER . ! ? … followed by whitespace.
_VE_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def _ve_shrink_to_budget(scenes: list, budget: int) -> None:
    """DETERMINISTIC last-resort trim so the TOTAL narration is <= `budget` words WITHOUT
    speeding up the voice or dropping a scene. Removes whole TRAILING sentences from the
    last scenes first (keeps >=1 sentence per scene), then — if still over — trims
    trailing WORDS from the last scenes (keeps >=1 word per scene). The scene tiling stays
    contiguous and every narration stays non-empty; a whole scene is never dropped (its
    source slice would then have no VO). Mutates `scenes` in place. Reachable only after
    the one shrink re-prompt already failed to get under budget."""
    def _total() -> int:
        return sum(len((s.get("narration") or "").split()) for s in scenes)

    # Pass 1 — drop trailing whole sentences, last scene backwards (keep >=1 sentence).
    i = len(scenes) - 1
    while _total() > budget and i >= 0:
        sents = [x for x in _VE_SENTENCE_SPLIT.split((scenes[i].get("narration") or "").strip()) if x]
        while len(sents) > 1 and _total() > budget:
            sents.pop()
            scenes[i]["narration"] = " ".join(sents).strip()
        i -= 1

    # Pass 2 — still over: trim trailing WORDS, last scene backwards (keep >=1 word).
    i = len(scenes) - 1
    while _total() > budget and i >= 0:
        words = (scenes[i].get("narration") or "").split()
        over = _total() - budget
        keep = max(1, len(words) - over)
        if keep < len(words):
            scenes[i]["narration"] = " ".join(words[:keep]).strip()
        i -= 1


def generate_script_visual_explain(frames: list[dict], *, title: str = "",
                                   description: str = "", tags: list | None = None,
                                   sourceDurationSec: float, targetDurationSec: int,
                                   sceneCount: int | None = None,
                                   jobId: int | None = None) -> dict:
    """NO-SPEECH FALLBACK generator: write a Vietnamese explainer script from sampled
    frames + source metadata (Claude VISION), shaped as the SAME footage scene array the
    footage assembler consumes ({scene, narration, sourceStart, sourceEnd}).

    Used by the runner when a footage/translate/dubbed LINK job ingests a source with
    audio but ZERO whisper transcript segments (e.g. an animation with only music/SFX):
    there is nothing to rewrite, so we show Claude the frames and ask for an on-screen
    explainer that TILES the whole source timeline. The narration IS TTS-generated
    downstream (this is NOT the dubbed no-TTS path).

    Raises HTTPException(422) with a user-facing Vietnamese message when the vision pass
    produces a too-thin/failed script (owner-approved: FAIL honestly rather than ship a
    title-only guess)."""
    tags = tags or []
    src = float(sourceDurationSec or 0.0)
    if not frames:
        raise HTTPException(422, "Không tạo được kịch bản từ hình ảnh — thử lại")

    # Scene count (respect an explicit override): source-duration derived, floored at 5,
    # capped by the AUTO scene cap — same sizing basis as the footage AUTO path.
    if sceneCount and sceneCount > 0:
        N = sceneCount
    else:
        N = min(_AUTO_SCENE_CAP, max(5, round(src / _SECONDS_PER_SCENE))) if src > 0 else 5

    # Word budget: the SMALLER of the target-duration budget and the source-length ceiling,
    # so the natural-paced VO is shorter than BOTH the requested length and the source.
    budget = min(_word_budget(targetDurationSec), _auto_word_ceiling(src)) if src > 0 \
        else _word_budget(targetDurationSec)
    pace = _VI_WORDS_PER_SEC
    # SAFETY MARGIN: show the vision model a budget ~10% BELOW the true ceiling so a
    # natural draft lands under the pre-TTS duration gate without needing a re-prompt.
    # The GATES below still check the TRUE `budget`/ceiling — only the number the model
    # is asked to hit is shrunk. LOCAL to this generator: no global pace/constant change,
    # so the footage/translate/image paths keep their exact pacing/behavior.
    prompt_budget = max(1, int(budget * 0.9))
    per_scene = max(1, prompt_budget // max(1, N))

    prompt = _build_visual_explain_prompt(frames, title, description, tags, N, src,
                                          prompt_budget, per_scene, pace)
    frames_dir = os.path.dirname(frames[0]["path"])

    def _vision_once(p: str) -> str:
        """One multi-turn vision call (open ~N frames with Read, then emit JSON), with a
        transient (timeout / 5xx) retry in a FRESH process — same rationale as the text
        script-gen retry: the call is idempotent and side-effect-free."""
        _err: HTTPException | None = None
        for attempt in range(2):  # 1 initial + 1 retry
            try:
                return _run_claude_vision_script(p, frames_dir, jobId, VISUAL_EXPLAIN_TIMEOUT)
            except HTTPException as e:
                _err = e
                if e.status_code in (500, 502, 504) and attempt == 0:
                    log.warning("[generate] job %s visual-explain vision call failed (%s) — "
                                "retrying once in a fresh process", jobId, e.status_code)
                    continue
                raise
        raise _err or HTTPException(500, "Claude vision failed")  # pragma: no cover

    # Initial draft (parse + clamp + reject-thin + tile-normalize).
    clean = _ve_parse_and_clean(_vision_once(prompt), src, jobId)
    total = _count_narration_words(clean)

    # BUDGET RECOVERY (the vision model overshot the word cap). The TEXT script-gen path
    # recovers from over-budget drafts; the visual path must too, WITHOUT cramming words
    # or speeding up the voice (hard project rule). Two graded steps, then the honest gate:
    #   1) RE-PROMPT ONCE (fresh vision call, SAME frames dir so it can still see the
    #      images) stating the previous word count + the hard maximum, asking it to rewrite
    #      the SAME tiling/meaning in fewer words. Keep whichever valid draft is SHORTER.
    #   2) If STILL over, DETERMINISTICALLY trim trailing narration down to <= budget
    #      (whole trailing sentences first, then trailing words), keeping the tiling
    #      contiguous and every narration non-empty — never dropping a scene.
    if total > budget:
        log.info("[generate] job %s visual-explain: draft %d words > budget %d — "
                 "re-prompting once to shrink", jobId, total, budget)
        _shrink = (
            f"\n\nYour previous draft was {total} words, which is TOO LONG. Rewrite the SAME "
            f"script in AT MOST {prompt_budget} words TOTAL — remove detail and merge/shorten "
            f"sentences, keep the SAME scene tiling (sourceStart/sourceEnd) and the same "
            f"meaning. Do NOT speed up delivery; fewer words only."
        )
        try:
            clean2 = _ve_parse_and_clean(_vision_once(prompt + _shrink), src, jobId)
            total2 = _count_narration_words(clean2)
            # Keep the re-prompt result only if it is valid AND shorter; otherwise the
            # deterministic trim below works on the original (it lost less content).
            if total2 < total:
                clean, total = clean2, total2
                log.info("[generate] job %s visual-explain: re-prompt produced %d words",
                         jobId, total)
        except HTTPException as e:
            log.warning("[generate] job %s visual-explain: shrink re-prompt failed (%s) — "
                        "falling back to deterministic trim", jobId, e.status_code)

    if total > budget:
        _ve_shrink_to_budget(clean, budget)
        total = _count_narration_words(clean)
        log.info("[generate] job %s visual-explain: deterministic trim → %d words (budget %d)",
                 jobId, total, budget)

    # FINAL honest gates. After recovery the total is <= budget (which is below BOTH the
    # duration and source-fit ceilings), so these pass. Only a DEGENERATE output that still
    # cannot fit (e.g. more scenes than the budget has words) raises the Vietnamese 422 —
    # the fail-honestly path. Neither gate truncates or speeds up the voice.
    _enforce_script_duration(clean, src, with_slate=True)
    _enforce_fixed_source_fit(clean, src)

    # renumber scenes 1..N in timeline order.
    for i, s in enumerate(clean, start=1):
        s["scene"] = i

    log.info("[generate] job %s visual-explain: %d scenes over %.0fs source (%d words, budget %d)",
             jobId, len(clean), src, total, budget)
    return {"scenes": clean}


def _split_transcript(transcript: str, batches: int) -> list[str]:
    """Split transcript text into `batches` CONTIGUOUS chunks for the no-timestamp
    transform path. Splits on whitespace boundaries (word-aligned) so no word is cut
    mid-token; chunks are roughly equal by word count and cover the text in order."""
    words = transcript.split()
    if batches <= 1 or len(words) <= batches:
        return [transcript]
    counts = _split_counts(len(words), batches)
    chunks, idx = [], 0
    for c in counts:
        chunks.append(" ".join(words[idx:idx + c]))
        idx += c
    return chunks


def _gen_transform_scenes(req: TransformRequest, scene_count: int) -> list:
    """Generate the transform scene array, CHUNKING into batches when scene_count
    exceeds SCRIPT_GEN_CHUNK_SCENES. The (no-timestamp) transcript is split into B
    contiguous text chunks; batch i rewrites its chunk into its slice of scenes.
    Batches merge in order with scene ids renumbered 1..N. A failed batch retries on
    its own; completed batches are not discarded. Degenerate case (<= chunk size):
    ONE call over the whole transcript."""
    chunk = _chunk_for_mode(req.editMode)
    batches = _batch_count(scene_count, chunk)
    # Per-job LLM choice; {} when unset -> unchanged call shape.
    llm_kw = _llm_kwargs(getattr(req, "llmProvider", None), getattr(req, "llmModel", None))
    if batches == 1:
        prompt = _build_transform_prompt(req, scene_count)
        return _run_claude_script(prompt, timeout=SCRIPT_GEN_TIMEOUT, **llm_kw)

    per_batch = _split_counts(scene_count, batches)
    chunks = _split_transcript(req.transcript.strip(), batches)
    # If the transcript is too short to split into B parts, _split_transcript returns
    # one chunk — fall back to a single call so we don't re-send the same text B times.
    if len(chunks) < batches:
        prompt = _build_transform_prompt(req, scene_count)
        return _run_claude_script(prompt, timeout=SCRIPT_GEN_TIMEOUT, **llm_kw)

    # Build the ORDERED list of per-batch prompts (same model_copy / chunk logic), then
    # run them CONCURRENTLY. _run_batches_parallel preserves input order, so the merged
    # scene order is identical to the old sequential loop — only the timing changes.
    prompts = []
    for i, (n_scenes, chunk) in enumerate(zip(per_batch, chunks)):
        log.info("[generate] transform batch %d/%d: %d scenes", i + 1, batches, n_scenes)
        sub_req = req.model_copy(update={"transcript": chunk, "sceneCount": n_scenes})
        prompts.append(_build_transform_prompt(sub_req, n_scenes))
    # Fail-fast on any batch error (others drained, first error re-raised).
    return _merge_renumber(_run_batches_parallel(prompts, llm_kwargs=llm_kw))


@router.post("/generate/script/transform")
def generate_script_transform(req: TransformRequest):
    if not req.transcript.strip():
        raise HTTPException(422, "Provide the source 'transcript' (from /generate/ingest).")
    scene_count = req.sceneCount or _auto_scene_count(req.durationSec, req.auto, req.editMode)
    scenes = _gen_transform_scenes(req, scene_count)
    return {
        "editMode": req.editMode.lower(),
        "durationSec": req.durationSec,
        "sceneCount": len(scenes),
        "scenes": scenes,
        **_llm_used(req.llmProvider, req.llmModel),
    }


# --- Image generation (ComfyUI / SDXL) ----------------------------------
#
# n8n cannot drive ComfyUI's graph itself, so we wrap it: build a txt2img
# workflow from each scene's English image_prompt, submit it to ComfyUI's
# /prompt API, poll /history until the render lands, and hand back the file
# paths. Portrait 9:16 (768x1344) matches the Shorts/Reels frame.

NEGATIVE_PROMPT = (
    "lowres, blurry, jpeg artifacts, watermark, text, signature, deformed, "
    "bad anatomy, extra limbs, disfigured, ugly, cropped, worst quality"
)


class ImageScene(BaseModel):
    scene: int
    image_prompt: str


# SDXL native resolution per output aspect. SDXL is trained on ~1MP buckets;
# generating at the bucket whose ratio matches the OUTPUT aspect avoids the old
# bug where every image was rendered PORTRAIT (768x1344) and then letterboxed into
# a landscape/square frame with blurred bars. Each bucket is ~1MP, so peak VRAM is
# the SAME as the previous portrait default (safe on the 8GB RTX 2070). Keys mirror
# runner.ASPECTS; the slight ratio gap (e.g. 4:5=0.80 vs the 896x1152=0.78 bucket)
# is absorbed by the assembler's scale+Ken-Burns crop. Unknown aspect → 9:16.
SDXL_RES = {
    "9:16": (768, 1344),
    "16:9": (1344, 768),
    "1:1": (1024, 1024),
    "4:5": (896, 1152),
}


def sdxl_dims_for_aspect(aspect: str | None) -> tuple[int, int]:
    """SDXL (width, height) for a job's output aspect; defaults to 9:16 portrait."""
    return SDXL_RES.get(aspect or "9:16", SDXL_RES["9:16"])


class ImagesRequest(BaseModel):
    # Either pass scenes (from /generate/script) or a single prompt for a test.
    scenes: list[ImageScene] | None = None
    prompt: str | None = None
    width: int = 768
    height: int = 1344
    steps: int = 28
    seed: int | None = None
    checkpoint: str | None = None   # ComfyUI ckpt_name; None falls back to SDXL_CHECKPOINT


def _comfy_post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{COMFY_URL}{path}", data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _comfy_get(path: str) -> dict:
    with urllib.request.urlopen(f"{COMFY_URL}{path}", timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --- ComfyUI VRAM reclaim before a GPU phase ---------------------------------
# An IDLE ComfyUI does NOT release the checkpoint it last rendered with: measured
# 4419 MiB of the 8192 MiB card still pinned with an empty queue. That is the whole
# problem, because the 8GB card must also hold the TTS model AND the concurrent
# NVENC cut sessions:
#   job 286 (2026-07-29) — 5 parallel h264_nvenc cuts of a 1440p60 source pushed VRAM
#   to 5935 MiB (2.25 GiB free); OmniVoice's model load then died with a NATIVE
#   0xC0000005 in arrow.dll/torch_cpu.dll instead of a clean CUDA OOM (no Python
#   traceback, so all 3 worker retries burned inside the same load window).
# Reproduced A/B/C: TTS alone PASSES; TTS + 5 NVENC cuts CRASHES; TTS + 5 libx264
# (CPU) cuts PASSES with even LESS free RAM — so it is VRAM, not host RAM.
# POST /free {"unload_models","free_memory"} reclaims it (measured 4419 -> 719 MiB).
# ComfyUI transparently re-loads the checkpoint on its next render (a few seconds,
# once), which matches the project's "models run sequentially, not concurrently" rule.
# Set CF_COMFY_FREE_BEFORE_GPU=0 to disable.
CF_COMFY_FREE_BEFORE_GPU = os.getenv("CF_COMFY_FREE_BEFORE_GPU", "1").strip().lower() \
    not in ("0", "off", "false", "no")


def _vram_used_mb() -> int | None:
    """Total VRAM in use on the card (MiB), or None if nvidia-smi is unavailable.
    Logging-only — never gates anything."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=10,
        )
        return int((proc.stdout or "").strip().splitlines()[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def _free_comfy_vram(reason: str = "") -> bool:
    """Ask ComfyUI to unload its models and release cached VRAM. Returns True if the
    call was made.

    Best-effort by design: ComfyUI being down/unreachable is NOT an error here (an
    image job would fail later with its own clear message), so every failure is logged
    and swallowed — this must never be the reason a job dies.

    SKIPPED while ComfyUI is actively executing a prompt (`/queue` -> queue_running):
    unloading models out from under a running render would break THAT render. Pending
    (not yet started) prompts are fine — they re-load the checkpoint themselves."""
    if not CF_COMFY_FREE_BEFORE_GPU:
        return False
    try:
        q = _comfy_get("/queue")
        if q.get("queue_running"):
            log.info("[comfy] VRAM reclaim SKIPPED (%s): a render is in flight", reason)
            return False
    except Exception as e:  # noqa: BLE001 — urllib/JSON/socket all mean "can't ask"
        log.info("[comfy] VRAM reclaim skipped (%s): queue unreachable (%s)", reason, e)
        return False
    before = _vram_used_mb()
    try:
        # /free answers 200 with an EMPTY body, so do NOT go through _comfy_post
        # (which json-decodes the response and would raise on "").
        req = urllib.request.Request(
            f"{COMFY_URL}/free",
            data=json.dumps({"unload_models": True, "free_memory": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception as e:  # noqa: BLE001
        log.warning("[comfy] VRAM reclaim FAILED (%s): %s", reason, e)
        return False
    after = _vram_used_mb()
    if before is not None and after is not None:
        log.info("[comfy] VRAM reclaim (%s): %d -> %d MiB used (freed %d MiB)",
                 reason, before, after, max(0, before - after))
    else:
        log.info("[comfy] VRAM reclaim (%s): requested (VRAM unmeasurable)", reason)
    return True


def _build_workflow(prompt: str, width: int, height: int, steps: int, seed: int,
                    checkpoint: str | None = None, cfg: float = 7.0,
                    negative: str | None = None) -> dict:
    # cfg defaults to 7.0 to preserve scene-image behavior; the cover path passes
    # COVER_CFG (8.0) for tighter prompt adherence on the poster. `negative` defaults to
    # the scene-image NEGATIVE_PROMPT; the cover path passes COVER_NEGATIVE (text-killing
    # + punchy) without changing scene-image behavior.
    return {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": "dpmpp_2m",
                "scheduler": "karras",
                "denoise": 1.0,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint or SDXL_CHECKPOINT}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": negative or NEGATIVE_PROMPT, "clip": ["4", 1]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "contentfactory/scene", "images": ["8", 0]},
        },
    }


def _render_one(prompt: str, width: int, height: int, steps: int, seed: int, timeout: int = 240,
                checkpoint: str | None = None) -> list[dict]:
    workflow = _build_workflow(prompt, width, height, steps, seed, checkpoint)
    try:
        submitted = _comfy_post("/prompt", {"prompt": workflow})
    except urllib.error.URLError as e:
        raise HTTPException(502, f"ComfyUI unreachable at {COMFY_URL} — is it running? ({e})")
    prompt_id = submitted.get("prompt_id")
    if not prompt_id:
        raise HTTPException(502, f"ComfyUI rejected the workflow: {submitted}")

    deadline = time.time() + timeout
    while time.time() < deadline:
        history = _comfy_get(f"/history/{prompt_id}")
        entry = history.get(prompt_id)
        if entry and entry.get("outputs"):
            images = []
            for node_out in entry["outputs"].values():
                for img in node_out.get("images", []):
                    q = urllib.parse.urlencode(
                        {"filename": img["filename"], "subfolder": img.get("subfolder", ""), "type": img.get("type", "output")}
                    )
                    images.append({
                        "filename": img["filename"],
                        "subfolder": img.get("subfolder", ""),
                        "url": f"{COMFY_URL}/view?{q}",
                    })
            return images
        # SDXL stays cached across prompts (the checkpoint node is unchanged), so
        # this loop only waits out the current render. Poll at 0.5s (was 2s) to
        # remove idle polling tax — ~1.5s/scene reclaimed across N scenes.
        time.sleep(0.5)
    raise HTTPException(504, f"ComfyUI render timed out after {timeout}s (prompt_id={prompt_id})")


def generate_images(req: ImagesRequest, progress=None):
    """Render one SDXL still per scene via ComfyUI.

    `progress` is an OPTIONAL callable(pct:int 0-100, msg:str). The runner passes one
    so the job bar advances per finished image. This is a COUNT-based step (N/M
    scenes) — kept in count form per the owner's preference, but now emitted live
    instead of sitting frozen at the band start. NOTE: this is the plain function,
    NOT the route handler — `progress` is never an HTTP param. The HTTP route below
    delegates here with progress=None.
    """
    items: list[tuple[int, str]] = []
    if req.scenes:
        items = [(s.scene, s.image_prompt) for s in req.scenes]
    elif req.prompt:
        items = [(1, req.prompt)]
    else:
        raise HTTPException(422, "Provide either 'scenes' or 'prompt'.")

    base_seed = req.seed if req.seed is not None else int(time.time())
    results = []
    total = len(items)
    for offset, (scene_no, prompt) in enumerate(items):
        images = _render_one(prompt, req.width, req.height, req.steps, base_seed + offset, checkpoint=req.checkpoint)
        results.append({"scene": scene_no, "prompt": prompt, "images": images})
        if progress:
            try:
                done = offset + 1
                progress(round(done / total * 100) if total else 100, f"Tạo ảnh {done}/{total}")
            except Exception:
                pass

    return {"count": len(results), "width": req.width, "height": req.height, "results": results}


@router.post("/generate/images")
def generate_images_route(req: ImagesRequest):
    return generate_images(req)


# --- Cover / thumbnail generation ---------------------------------------
#
# Generate a custom video COVER (poster/thumbnail) straight from the title via SDXL
# — NO Claude call (owner decision: the title IS the prompt). Rotating a visual
# STYLE suffix by styleIndex lets re-clicking yield a visibly different look. The
# result is persisted to a STABLE disk path (covers cache) so the Studio can preview
# it and a job can later apply it as the video's thumb_path.

# NOTE (2026-07-04): COVER_STYLES is NO LONGER APPLIED — the owner set the AUTO cover
# prompt to the TITLE ALONE (see _assemble_cover_prompt). Kept here so the rotating-style
# behavior can be re-enabled later without rewriting the list. English only (model-facing).
COVER_STYLES = [
    "cinematic dramatic lighting, high detail, film still, bold composition",
    "vibrant poster art, saturated colors, dynamic, eye-catching, illustration",
    "dark moody atmosphere, deep shadows, high contrast, mysterious tone",
    "minimal bold composition, clean negative space, strong focal subject",
    "photorealistic, sharp focus, professional photography, natural lighting",
    "epic fantasy digital painting, glowing highlights, intense mood, detailed",
]

# Cover renders use FEWER sampling steps than scene images (a thumbnail doesn't need
# scene-image quality) — faster "Tạo Cover". Env-tunable; scene image gen stays at 28.
COVER_STEPS = int(os.getenv("COVER_STEPS", "18"))
# Cover renders use a slightly higher CFG than scene images for tighter prompt
# adherence on the poster. Env-tunable; scene image gen stays at 7.0.
COVER_CFG = float(os.getenv("COVER_CFG", "8.0"))

# FIXED thumbmagic / Submagic ("MrBeast" high-CTR) style suffix appended in the BACKEND
# to whatever the vision call returns, so the look is enforced consistently across covers
# regardless of the LLM's exact wording. Env-overridable. English (CLIP is English-only).
COVER_STYLE_SUFFIX = os.getenv(
    "COVER_STYLE_SUFFIX",
    ", MrBeast-style YouTube thumbnail, bold high-CTR, dramatic cinematic lighting, "
    "strong rim light, high contrast, punchy saturated colors, vibrant HDR pop, subject "
    "separated from dark blurred background, bokeh, dark vignette, subtle glow around "
    "subject, depth of field, ultra sharp, professional studio color grade, 8k, eye-catching",
)

# Cover-specific NEGATIVE prompt (kills baked-in text + weak look). Used ONLY by the
# cover render path (_render_cover_image); the scene-image NEGATIVE_PROMPT is untouched.
COVER_NEGATIVE = os.getenv(
    "COVER_NEGATIVE",
    "text, letters, words, watermark, logo, caption, typography, subtitles, low contrast, "
    "flat lighting, dull, desaturated, washed out, cluttered background, blurry subject, "
    "deformed hands, deformed face, disfigured, plastic skin, low quality, jpeg artifacts, "
    "border, frame",
)

# NOTE: AUTO_COVER_ENABLED / the AUTO_COVER env knob were REMOVED (2026-07-28) together with
# generate_vision_cover_sync — the pipeline no longer generates a cover on its own, so there
# is nothing left to gate. Covers are MANUAL only (see the note above @router.post("/generate/cover")).


# ---- ComfyUI websocket progress + async cover tasks -------------------------
#
# ComfyUI emits real sampling progress over a websocket (ws://<host>/ws?clientId=..).
# We submit the /prompt with the SAME client_id and listen for:
#   {"type":"progress","data":{"value":N,"max":M}}          — KSampler step N/M
#   {"type":"executing","data":{"node":null,"prompt_id":X}} — node null => finished
# When execution finishes we STILL fetch /history/<prompt_id> to get the output
# image filename (ws tells WHEN done; /history gives the result to download+persist).

# Phase pct mapping (single image): submit=5, sampling 5->95 (from progress events),
# fetch+persist 95->100, done=100.
_COVER_PCT_SUBMIT = 5
_COVER_PCT_SAMPLE_LO = 5
_COVER_PCT_SAMPLE_HI = 95
_COVER_PCT_PERSIST = 95


def _comfy_ws_url(client_id: str) -> str:
    """ws(s):// URL for ComfyUI's progress socket, derived from COMFY_URL."""
    base = COMFY_URL
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return f"{base}/ws?clientId={client_id}"


def _history_images(prompt_id: str) -> list[dict]:
    """Read /history/<prompt_id> and map its outputs to [{filename,subfolder,url}].
    Returns [] when the entry has no outputs yet."""
    history = _comfy_get(f"/history/{prompt_id}")
    entry = history.get(prompt_id)
    if not (entry and entry.get("outputs")):
        return []
    images: list[dict] = []
    for node_out in entry["outputs"].values():
        for img in node_out.get("images", []):
            q = urllib.parse.urlencode(
                {"filename": img["filename"], "subfolder": img.get("subfolder", ""),
                 "type": img.get("type", "output")}
            )
            images.append({
                "filename": img["filename"],
                "subfolder": img.get("subfolder", ""),
                "url": f"{COMFY_URL}/view?{q}",
            })
    return images


async def _ws_wait_for_done(client_id: str, prompt_id: str, timeout: float,
                            progress_cb) -> None:
    """Listen on ComfyUI's websocket until this prompt_id finishes executing,
    forwarding sampling progress to progress_cb(pct, msg). Raises TimeoutError on
    deadline, or lets connection errors propagate to the caller's fallback."""
    import websockets  # local import — availability checked at call site
    url = _comfy_ws_url(client_id)
    deadline = time.time() + timeout
    async with websockets.connect(url, max_size=None) as ws:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError()
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            if not isinstance(raw, str):
                continue  # binary preview frames — ignore
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            mtype = msg.get("type")
            data = msg.get("data") or {}
            if mtype == "progress":
                mx = data.get("max") or 0
                val = data.get("value") or 0
                if mx:
                    span = _COVER_PCT_SAMPLE_HI - _COVER_PCT_SAMPLE_LO
                    pct = _COVER_PCT_SAMPLE_LO + round(val / mx * span)
                    progress_cb(min(pct, _COVER_PCT_SAMPLE_HI), f"Vẽ ảnh bìa {val}/{mx}")
            elif mtype == "executing":
                # node null AND our prompt_id => the whole prompt finished.
                if data.get("node") is None and data.get("prompt_id") == prompt_id:
                    return
            elif mtype == "execution_error" and data.get("prompt_id") == prompt_id:
                raise RuntimeError(f"ComfyUI execution error: {data}")


def _submit_cover_workflow_and_wait(workflow: dict, progress_cb, timeout: int = 240) -> list[dict]:
    """Submit ANY single-image ComfyUI workflow and return [{filename,subfolder,url}].

    This is the shared render engine for the cover paths: it submits `workflow`,
    tracks real sampling progress over the ComfyUI websocket (falling back to
    /history polling when the `websockets` lib is unavailable), and fetches the
    produced image(s) from /history. progress_cb(pct,msg) is called throughout.
    Raises HTTPException on unreachable/rejected/timeout. Extracted so both the
    text2img cover render and the reference-image (IP-Adapter/img2img) render reuse
    the identical ws-progress + persistence path."""
    client_id = uuid.uuid4().hex
    try:
        submitted = _comfy_post("/prompt", {"prompt": workflow, "client_id": client_id})
    except urllib.error.URLError as e:
        raise HTTPException(502, f"ComfyUI unreachable at {COMFY_URL} — is it running? ({e})")
    prompt_id = submitted.get("prompt_id")
    if not prompt_id:
        raise HTTPException(502, f"ComfyUI rejected the workflow: {submitted}")
    progress_cb(_COVER_PCT_SUBMIT, "Đã gửi yêu cầu dựng ảnh")

    # Prefer real ws progress; fall back to /history polling if ws is unavailable.
    ws_ok = False
    try:
        import websockets  # noqa: F401 — availability probe
        ws_ok = True
    except Exception:
        print("[cover] websockets lib unavailable — falling back to /history polling (coarse pct)")

    if ws_ok:
        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(
                    _ws_wait_for_done(client_id, prompt_id, float(timeout), progress_cb)
                )
            finally:
                loop.close()
            # Execution finished — fetch the produced image(s) from /history.
            progress_cb(_COVER_PCT_PERSIST, "Đang lưu ảnh bìa")
            imgs = _history_images(prompt_id)
            if imgs:
                return imgs
            # ws said done but /history has no outputs yet — poll briefly.
            end = time.time() + 10
            while time.time() < end:
                imgs = _history_images(prompt_id)
                if imgs:
                    return imgs
                time.sleep(0.5)
            raise HTTPException(502, "ComfyUI finished but produced no image")
        except TimeoutError:
            raise HTTPException(504, f"ComfyUI render timed out after {timeout}s (prompt_id={prompt_id})")
        except HTTPException:
            raise
        except Exception as e:
            # ws failed mid-run — degrade to /history polling rather than crashing.
            print(f"[cover] ws progress failed ({e}); falling back to /history polling")

    # FALLBACK: coarse pct — submit already reported 5; hold ~15 until /history returns.
    progress_cb(15, "Đang dựng ảnh bìa")
    deadline = time.time() + timeout
    while time.time() < deadline:
        imgs = _history_images(prompt_id)
        if imgs:
            progress_cb(_COVER_PCT_PERSIST, "Đang lưu ảnh bìa")
            return imgs
        time.sleep(0.5)
    raise HTTPException(504, f"ComfyUI render timed out after {timeout}s (prompt_id={prompt_id})")


def _render_cover_image(prompt: str, width: int, height: int, steps: int, seed: int,
                        progress_cb, timeout: int = 240, cfg: float = COVER_CFG) -> list[dict]:
    """Render a text2img SDXL cover (the AUTO/title path). Thin wrapper that builds
    the text2img workflow and delegates to the shared render engine. `cfg` defaults
    to COVER_CFG; scene image gen uses _render_one. Uses the cover-specific COVER_NEGATIVE
    (text-killing + punchy) so baked-in SDXL text / weak look is suppressed."""
    workflow = _build_workflow(prompt, width, height, steps, seed, cfg=cfg, negative=COVER_NEGATIVE)
    return _submit_cover_workflow_and_wait(workflow, progress_cb, timeout)


def warmup_comfyui() -> None:
    """Pre-load SDXL into ComfyUI at server start so the owner's FIRST cover render
    isn't slowed by the cold checkpoint-load. Runs once per boot on a daemon thread.

    Gated by COMFY_WARMUP (default on; '0' disables). FULLY best-effort — every step
    is wrapped so this never raises and never crashes the server. Loads SDXL into
    ComfyUI's VRAM at idle (acceptable: models are still used sequentially)."""
    if (os.getenv("COMFY_WARMUP", "1") or "").strip().lower() in ("0", "off", "false", "no"):
        return
    try:
        # 1) Wait for ComfyUI to be reachable (2s interval, ~120s cap).
        reachable = False
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                _comfy_get("/system_stats")
                reachable = True
                break
            except Exception:
                time.sleep(2)
        if not reachable:
            print("[warmup] ComfyUI not reachable — skipped")
            return

        # 2) Submit ONE tiny render just to force the checkpoint load into VRAM.
        workflow = _build_workflow("warmup", 256, 256, COVER_STEPS, 0)
        submitted = _comfy_post("/prompt", {"prompt": workflow})
        prompt_id = submitted.get("prompt_id")
        if not prompt_id:
            print(f"[warmup] failed: ComfyUI rejected warmup workflow: {submitted}")
            return

        # 3) Poll /history briefly to let it complete (we don't need the image).
        warm_deadline = time.time() + 120
        while time.time() < warm_deadline:
            try:
                history = _comfy_get(f"/history/{prompt_id}")
                entry = history.get(prompt_id)
                if entry and entry.get("outputs"):
                    print("[warmup] ComfyUI warmed")
                    return
            except Exception:
                pass
            time.sleep(2)
        # Timed out waiting for completion — the checkpoint likely still loaded.
        print("[warmup] ComfyUI warm render did not finish in time (checkpoint likely loaded)")
    except Exception as e:  # noqa: BLE001 — warmup must never crash the server
        print(f"[warmup] failed: {e}")


# In-memory async cover tasks. Entry: {status, pct, msg, result, error, ts}.
_COVER_TASKS: dict[str, dict] = {}
_COVER_TASKS_LOCK = threading.Lock()
_COVER_TASK_TTL = 600  # 10 min — prune older entries on each new POST to bound growth.


def _prune_cover_tasks() -> None:
    """Drop tasks older than the TTL (called under the lock)."""
    now = time.time()
    stale = [tid for tid, t in _COVER_TASKS.items() if now - t.get("ts", now) > _COVER_TASK_TTL]
    for tid in stale:
        _COVER_TASKS.pop(tid, None)


def _cover_task_update(task_id: str, **fields) -> None:
    """Merge fields into a task entry and bump its ts (thread-safe, best-effort)."""
    with _COVER_TASKS_LOCK:
        t = _COVER_TASKS.get(task_id)
        if t is None:
            return
        t.update(fields)
        t["ts"] = time.time()


class CoverRequest(BaseModel):
    page: str
    title: str = ""                     # required only for the AUTO path (empty OK when `prompt` given)
    aspect: str = "9:16"
    seed: int | None = None
    styleIndex: int = 0
    summary: str | None = None          # Vietnamese content summary — appended to the auto prompt
                                        # (truncated) so the cover is more on-topic. Ignored when `prompt` set.
    prompt: str | None = None           # manual user-typed cover prompt (EN or VI); when non-empty it
                                        # is used VERBATIM (NO style suffix) — the user's prompt drives the image.
    sourceLink: str | None = None       # optional source video URL. When set (and no manual `prompt`),
                                        # Claude VISION analyzes the source thumbnail + title to craft the
                                        # English SDXL prompt; falls back to the title-only prompt on failure.
    tiltDeg: float | None = None        # exact title tilt (deg) for the auto-baked title, clamped to
                                        # [-20,20]; null = seeded auto minority (see /generate/cover/title).


def _covers_dir(page: str) -> str:
    """Stable per-page covers cache dir: <root>/_cache/covers/<page>/. Created on demand."""
    return os.path.join(CONTENT_OUTPUT_ROOT, "_cache", "covers", page)


def _assemble_cover_prompt(req: CoverRequest) -> tuple[str, int, int]:
    """Validate the body and build (prompt, style_index, seed).

    Prompt source:
      - `prompt` (manual, EN/VI) non-empty  → used VERBATIM as the positive prompt.
        Re-clicking still varies the image via the changing seed (styleIndex advances
        the seed), just without swapping text.
      - else (AUTO path)                    → the TITLE ALONE.
    Owner decision (2026-07-04): the AUTO cover prompt is the TITLE ONLY — NO `summary`
    (script/content) and NO COVER_STYLES suffix are appended. Re-clicking still yields a
    different image via the changing seed (style_index advances the seed).
    Raises HTTPException(422) only on the AUTO path when title is empty."""
    title = (req.title or "").strip()
    manual = (req.prompt or "").strip()
    style_index = int(req.styleIndex or 0)
    if manual:
        prompt = manual  # verbatim — no style suffix
    else:
        if not title:
            raise HTTPException(422, "title is required")
        prompt = title  # title only — no summary, no style suffix (owner 2026-07-04)
    # Backend Python may use time for a varying seed (the no-Date rule is Workflow-only).
    seed = req.seed if req.seed is not None else (int(time.time()) + style_index)
    return prompt, style_index, seed


# Vision cover-prompt call is bounded: a few turns (open image + answer) and a short
# timeout. It must NEVER hard-fail the cover — on any failure the caller falls back to
# the title-only prompt. Env-tunable.
COVER_VISION_MAX_TURNS = int(os.getenv("COVER_VISION_MAX_TURNS", "6"))
COVER_VISION_TIMEOUT = int(os.getenv("COVER_VISION_TIMEOUT", "90"))
# Second-tier fallback when no thumbnail is available (DRM/private source, fetch error):
# a TEXT-ONLY reasoning call over the title. No image to open -> single turn, so it is
# meaningfully faster than the vision call and gets a shorter default timeout.
COVER_TITLE_ONLY_TIMEOUT = int(os.getenv("COVER_TITLE_ONLY_TIMEOUT", "45"))

# JSON-only system role for the vision cover call: the model returns the English SDXL
# prompt, the Vietnamese title, AND ordered title segments (with key-word flags), all in
# one object, nothing else.
_COVER_VISION_SYSTEM_PROMPT = (
    "You are an art director for video cover images. You output ONLY a single valid JSON "
    'object of the form {"prompt": "<English SDXL prompt>", "vi_title": "<Vietnamese '
    'title>", "title_segments": [{"text": "<word or short phrase>", "key": true|false}]} '
    "— no prose, no markdown, no code fences, no explanation."
)

# Same JSON-only contract as the vision role, for the TEXT-ONLY (no image) fallback call.
_COVER_TITLE_ONLY_SYSTEM_PROMPT = (
    "You are an art director for video covers, working from the title text alone. You "
    "output ONLY a single valid JSON object of the form "
    '{"prompt": "<English SDXL prompt>", "vi_title": "<Vietnamese '
    'title>", "title_segments": [{"text": "<word or short phrase>", "key": true|false}]} '
    "— no prose, no markdown, no code fences, no explanation."
)


# Safety-net regex for stray duration/time expressions the LLM may leave in the title
# (the main fix is the STEP 4 instruction). Strips VI "trong 8 phút" / "8 phút" and EN
# "in 8 minutes" / "8 min", plus dangling connectors, then collapses whitespace.
_DURATION_RE = re.compile(
    r"\b(?:trong|in)\s+\d+\s*(?:phút|phut|minutes?|mins?|min)\b"
    r"|\b\d+\s*(?:phút|phut|minutes?|mins?|min)\b",
    re.IGNORECASE,
)


def _strip_duration(title: str) -> str:
    """Remove duration/time expressions (e.g. 'trong 8 phút', '8 phút', 'in 8 minutes')
    and tidy the leftover punctuation/whitespace. Best-effort safety net behind the LLM
    instruction; returns the cleaned title (never raises)."""
    s = (title or "")
    s = _DURATION_RE.sub(" ", s)
    # Drop now-empty brackets/parens left around the removed duration, e.g. "(8 phút)".
    s = re.sub(r"[(\[\{]\s*[)\]\}]", " ", s)
    # Tidy connectors/punctuation left dangling by the removal, then collapse spaces.
    s = re.sub(r"\s*[-–—:|]\s*$", "", s)          # trailing separators
    s = re.sub(r"^\s*[-–—:|]\s*", "", s)          # leading separators
    s = re.sub(r"\s{2,}", " ", s).strip(" \t\r\n-–—:|")
    return s.strip()


def _parse_vision_cover_json(text: str) -> tuple[str, str, list]:
    """Parse Claude's vision answer into (english_prompt, vi_title, title_segments).
    Strips code fences, isolates the outermost JSON object, reads the string fields and
    the ordered `title_segments` (each {text, key}). English prompt is length-capped.
    Robust fallback: if title_segments is missing/invalid, synthesize it as the whole
    vi_title with key=False (single-color overlay still works). Returns ('','',[]) when
    nothing usable parses so the caller falls back to the title-only, no-overlay path."""
    s = (text or "").strip()
    if not s:
        return "", "", []
    s = re.sub(r"^```[a-zA-Z]*\s*", "", s).strip()
    s = re.sub(r"\s*```$", "", s).strip()
    # Isolate the outermost {...} so any stray prose around it is ignored.
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        s = s[i:j + 1]
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return "", "", []
    if not isinstance(obj, dict):
        return "", "", []
    prompt = obj.get("prompt")
    vi_title = obj.get("vi_title")
    prompt = prompt.strip() if isinstance(prompt, str) else ""
    vi_title = vi_title.strip().strip('"').strip("'").strip() if isinstance(vi_title, str) else ""
    if len(prompt) > 700:
        prompt = prompt[:700].rsplit(" ", 1)[0]
    vi_title = _strip_duration(vi_title)

    # Ordered title segments with key-word flags (each {text, key}). Each segment's text
    # is duration-stripped too, then empty segments dropped, so segments stay consistent
    # with the cleaned vi_title.
    segs_raw = obj.get("title_segments")
    title_segments: list[dict] = []
    if isinstance(segs_raw, list):
        for el in segs_raw:
            if isinstance(el, dict):
                tx = el.get("text")
                if isinstance(tx, str):
                    tx = _strip_duration(tx.strip())
                    if tx:
                        title_segments.append({"text": tx, "key": bool(el.get("key"))})
    if not title_segments and vi_title:
        # Fallback: whole title as one non-key segment -> single-color overlay still works.
        title_segments = [{"text": vi_title, "key": False}]
    return prompt, vi_title, title_segments


def _vision_cover_prompt(thumb_path: str, title: str) -> tuple[str, str, list]:
    """Ask Claude (headless VISION) to OPEN the source thumbnail (Read tool) and, with
    the title, return: an ENGLISH SDXL prompt for a DETAILED DIGITAL ILLUSTRATION that
    depicts a GROUNDED VISUAL METAPHOR for the title's SPECIFIC ANGLE/CLAIM — not the
    subject in general and NOT a literal redraw (the title, not the source thumbnail,
    decides the angle; sci-fi/glowing-AI clichés are explicitly ruled out unless the
    video really is about hardware/infra) — plus a natural Vietnamese title AND ordered
    title_segments marking the 1-2 MAIN-IDEA word(s) as key. English prompt (SDXL CLIP
    is English-only). Bounded turns + short timeout. NEVER raises: returns ('','',[]) on
    timeout/error/parse-fail so the caller falls back to the title-only, no-overlay path. Modeled on _run_claude_vision_script."""
    if not thumb_path or not os.path.isfile(thumb_path):
        return "", "", []
    frames_dir = os.path.dirname(os.path.realpath(thumb_path))
    t = (title or "").strip()
    user_prompt = (
        f"Open and look at the thumbnail image at this path using the Read tool:\n"
        f"{os.path.realpath(thumb_path)}\n\n"
        f"The video title is: \"{t}\"\n\n"
        "STEP 1 — Find the video's SPECIFIC CLAIM, not just its subject. Read the title as "
        "a sentence and separate (a) the SUBJECT — what thing it is about — from (b) the "
        "ANGLE — what the title actually ASSERTS, ASKS or PROMISES about that subject. The "
        "qualifying words carry the angle and change everything: limits / boundaries "
        "(giới hạn), risk / danger (rủi ro, nguy hiểm), why (vì sao, tại sao), compared to "
        "/ vs (so với), history (lịch sử), mistakes (sai lầm), future (tương lai), how-to "
        "(cách), the truth about (sự thật). 'What is X' and 'the LIMITS of X' share a "
        "subject but are DIFFERENT videos and MUST get different images. The TITLE decides "
        "the angle: the thumbnail is the SOURCE video's own artwork and is usually broader "
        "or more generic than this video — use it ONLY to recognise the subject and its "
        "visual context, NEVER to set the angle. State the angle to yourself in one short "
        "phrase (e.g. 'X hits a hard ceiling', 'X is more dangerous than people think') "
        "before continuing.\n"
        "STEP 2 — Choose ONE grounded VISUAL METAPHOR for THAT ANGLE (not for the subject "
        "in general): a real, physical, photographable object or scene that a viewer reads "
        "instantly as the claim you just stated, drawn from the everyday world — "
        "architecture, nature, weather, hands, animals, machinery, sport, household "
        "objects, and so on. TEST IT: if the very same image would fit just as well on a "
        "video about the subject's history, its risks, or how it works, it is too generic "
        "— discard it and pick a sharper one that only fits THIS angle. AVOID THE SCI-FI "
        "DEFAULT: do NOT use glowing neural networks, holographic brains, glowing cores or "
        "orbs, floating circuit boards, digital particle swarms, blue tech grids or falling "
        "code UNLESS the video is literally about hardware, chips, data centres or network "
        "infrastructure — those look identical on every tech video and kill CTR. Also NOT a "
        "flow diagram or infographic, and do NOT literally redraw or copy the thumbnail.\n"
        "STEP 3 — Write an ENGLISH prompt (one line, ~45 words) for an SDXL text-to-image "
        "model to generate a BOLD, HIGH-CTR YouTube THUMBNAIL SCENE (MrBeast / thumbmagic "
        "style) DEPICTING THAT METAPHOR. Make the metaphor the ONE strong FOCAL subject and "
        "place it on ONE side of the frame (say LEFT or RIGHT), leaving the OTHER side dark "
        "and low-detail for a big title. Make it dramatic, high-contrast and cinematic. If "
        "the angle is best carried by a person / character, use an EXPRESSIVE close-up "
        "(exaggerated emotion) reacting to the metaphor. Describe concrete physical detail "
        "— material, scale, lighting, camera angle — not abstract concepts. COMPOSITION: "
        "keep about 40% of the frame (the side OPPOSITE the subject) DARK and low-detail "
        "for a LARGE title. Do NOT describe any text, letters, captions, labels, logos or "
        "watermarks in the image (text is added separately).\n"
        "STEP 4 — Translate the title into natural, FAITHFUL Vietnamese. It MUST accurately "
        "preserve the ORIGINAL title's core subject AND the relationship between the "
        "subjects — do NOT loosely paraphrase, do NOT invent, do NOT drift to a different "
        "claim or a related-but-different idea. Keep the SAME MEANING, just concise and "
        "punchy. Example: an English title meaning 'How the Sun guides Voyager' must become "
        "'Cách Mặt Trời dẫn đường cho Voyager' — NOT 'Voyager rời hệ mặt trời' (that changes "
        "the meaning). STRIP ONLY duration / time expressions and filler — e.g. 'trong 8 "
        "phút', '8 phút', 'N phút', 'in N minutes', 'N minutes', episode / time tags — while "
        "keeping the actual topic and its relationships intact.\n"
        "STEP 5 — Split that Vietnamese title into ORDERED segments whose `text` values, "
        "joined by single spaces, reconstruct the title EXACTLY. Mark the 1-2 word(s) or "
        "one short phrase that carry the MAIN IDEA with \"key\": true and ALL other "
        "segments with \"key\": false.\n"
        'Output ONLY this JSON object: {"prompt": "<the English prompt>", "vi_title": '
        '"<the Vietnamese title>", "title_segments": [{"text": "...", "key": true|false}]}'
    )
    try:
        proc = subprocess.Popen(
            [CLAUDE_BIN, "-p", user_prompt, "--model", SCRIPT_GEN_MODEL,
             "--max-turns", str(COVER_VISION_MAX_TURNS),
             "--tools", "Read",
             "--add-dir", frames_dir,
             "--strict-mcp-config",
             "--system-prompt", _COVER_VISION_SYSTEM_PROMPT,
             "--output-format", "stream-json", "--verbose"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        log.warning("[cover] vision prompt: Claude binary not found (%s) — falling back", CLAUDE_BIN)
        return "", "", []

    _kill_job = _register_job_proc(proc)
    try:
        # Shared read/reap/error-classify body (see _claude_result). This site's contract
        # is NEVER-RAISE, so every classified failure (504 stall / 500 error / 502
        # no-result) is caught here and degraded to the title-only fallback — the same
        # outcome the three inline branches produced before, with the subtype/exit code
        # now carried in the logged detail instead of a separate log line.
        try:
            result_text = _claude_result(proc, COVER_VISION_TIMEOUT,
                                         label="Claude vision", expect="text")
        except HTTPException as e:
            log.warning("[cover] vision prompt failed (%s) — falling back to title-only", e.detail)
            return "", "", []
        if not result_text:
            # Empty (but present) result text: nothing to art-direct with.
            log.warning("[cover] vision prompt no usable result (empty result text) — falling back")
            return "", "", []
        prompt, vi_title, title_segments = _parse_vision_cover_json(result_text)
        if prompt:
            keyed = [s["text"] for s in title_segments if s.get("key")]
            log.info("[cover] vision-crafted SDXL prompt (%d chars): %s | vi_title=%r | key=%r",
                     len(prompt), prompt[:200], vi_title[:80], keyed)
        return prompt, vi_title, title_segments
    except Exception as e:  # noqa: BLE001 — vision must never crash the cover
        log.warning("[cover] vision prompt unexpected error (%s) — falling back", str(e)[:200])
        try:
            _kill_proc_tree(proc)
        except Exception:
            pass
        return "", "", []
    finally:
        _unregister_job_proc(_kill_job, proc)


def _title_only_cover_prompt(title: str) -> tuple[str, str, list]:
    """TEXT-ONLY twin of `_vision_cover_prompt`: no image, no Read tool, no --add-dir.

    Used when a `sourceLink` was given but no usable thumbnail/vision result came back
    (DRM-protected source, fetch error, vision timeout/parse-fail). Instead of silently
    degrading to the bare title, Claude reasons over the TITLE ALONE — using its own
    world knowledge of the subject to pick a concrete, photographable scene — and returns
    the same contract: (english_prompt, vi_title, title_segments). Single-shot
    (--max-turns 1) with a shorter timeout. NEVER raises: returns ('','',[]) on
    timeout/error/parse-fail so the caller falls back to the bare-title, no-overlay path."""
    t = (title or "").strip()
    if not t:
        return "", "", []
    user_prompt = (
        f"The video title is: \"{t}\"\n\n"
        "STEP 1 — Find the video's SPECIFIC CLAIM, not just its subject. Read the title as "
        "a sentence and separate (a) the SUBJECT — what thing it is about — from (b) the "
        "ANGLE — what the title actually ASSERTS, ASKS or PROMISES about that subject. The "
        "qualifying words carry the angle and change everything: limits / boundaries "
        "(giới hạn), risk / danger (rủi ro, nguy hiểm), why (vì sao, tại sao), compared to "
        "/ vs (so với), history (lịch sử), mistakes (sai lầm), future (tương lai), how-to "
        "(cách), the truth about (sự thật). 'What is X' and 'the LIMITS of X' share a "
        "subject but are DIFFERENT videos and MUST get different images. State the angle to "
        "yourself in one short phrase (e.g. 'X hits a hard ceiling', 'X is more dangerous "
        "than people think') before continuing.\n"
        "STEP 1b — You have NO image to look at, so use YOUR OWN KNOWLEDGE of the subject. "
        "Before choosing any metaphor, silently establish what this subject actually IS and "
        "how it really works in the world, and name at least one CONCRETE, RECOGNISABLE, "
        "PHOTOGRAPHABLE real-world object or scene that a general audience already "
        "associates with it. If the title names a technical or abstract concept, first "
        "answer 'what is this concept actually about, in physical terms' from general "
        "knowledge, THEN pick the visual metaphor for the SPECIFIC ANGLE you stated in "
        "STEP 1. Never invent facts about the video's content — reason only from the title "
        "and what you genuinely know about the subject.\n"
        "STEP 2 — Choose ONE grounded VISUAL METAPHOR for THAT ANGLE (not for the subject "
        "in general): a real, physical, photographable object or scene that a viewer reads "
        "instantly as the claim you just stated, drawn from the everyday world — "
        "architecture, nature, weather, hands, animals, machinery, sport, household "
        "objects, and so on. TEST IT: if the very same image would fit just as well on a "
        "video about the subject's history, its risks, or how it works, it is too generic "
        "— discard it and pick a sharper one that only fits THIS angle. AVOID THE SCI-FI "
        "DEFAULT: do NOT use glowing neural networks, holographic brains, glowing cores or "
        "orbs, floating circuit boards, digital particle swarms, blue tech grids or falling "
        "code UNLESS the video is literally about hardware, chips, data centres or network "
        "infrastructure — those look identical on every tech video and kill CTR. Also NOT a "
        "flow diagram or infographic.\n"
        "STEP 3 — Write an ENGLISH prompt (one line, ~45 words) for an SDXL text-to-image "
        "model to generate a BOLD, HIGH-CTR YouTube THUMBNAIL SCENE (MrBeast / thumbmagic "
        "style) DEPICTING THAT METAPHOR. Make the metaphor the ONE strong FOCAL subject and "
        "place it on ONE side of the frame (say LEFT or RIGHT), leaving the OTHER side dark "
        "and low-detail for a big title. Make it dramatic, high-contrast and cinematic. If "
        "the angle is best carried by a person / character, use an EXPRESSIVE close-up "
        "(exaggerated emotion) reacting to the metaphor. Describe concrete physical detail "
        "— material, scale, lighting, camera angle — not abstract concepts. COMPOSITION: "
        "keep about 40% of the frame (the side OPPOSITE the subject) DARK and low-detail "
        "for a LARGE title. Do NOT describe any text, letters, captions, labels, logos or "
        "watermarks in the image (text is added separately).\n"
        "STEP 4 — Translate the title into natural, FAITHFUL Vietnamese. It MUST accurately "
        "preserve the ORIGINAL title's core subject AND the relationship between the "
        "subjects — do NOT loosely paraphrase, do NOT invent, do NOT drift to a different "
        "claim or a related-but-different idea. Keep the SAME MEANING, just concise and "
        "punchy. Example: an English title meaning 'How the Sun guides Voyager' must become "
        "'Cách Mặt Trời dẫn đường cho Voyager' — NOT 'Voyager rời hệ mặt trời' (that changes "
        "the meaning). STRIP ONLY duration / time expressions and filler — e.g. 'trong 8 "
        "phút', '8 phút', 'N phút', 'in N minutes', 'N minutes', episode / time tags — while "
        "keeping the actual topic and its relationships intact.\n"
        "STEP 5 — Split that Vietnamese title into ORDERED segments whose `text` values, "
        "joined by single spaces, reconstruct the title EXACTLY. Mark the 1-2 word(s) or "
        "one short phrase that carry the MAIN IDEA with \"key\": true and ALL other "
        "segments with \"key\": false.\n"
        'Output ONLY this JSON object: {"prompt": "<the English prompt>", "vi_title": '
        '"<the Vietnamese title>", "title_segments": [{"text": "...", "key": true|false}]}'
    )
    try:
        proc = subprocess.Popen(
            [CLAUDE_BIN, "-p", user_prompt, "--model", SCRIPT_GEN_MODEL,
             "--max-turns", "1",
             "--strict-mcp-config",
             "--system-prompt", _COVER_TITLE_ONLY_SYSTEM_PROMPT,
             "--output-format", "stream-json", "--verbose"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        log.warning("[cover] title-only prompt: Claude binary not found (%s) — falling back to bare title",
                    CLAUDE_BIN)
        return "", "", []

    _kill_job = _register_job_proc(proc)
    try:
        try:
            result_text = _claude_result(proc, COVER_TITLE_ONLY_TIMEOUT,
                                         label="Claude title-only", expect="text")
        except HTTPException as e:
            log.warning("[cover] title-only prompt failed (%s) — falling back to bare title", e.detail)
            return "", "", []
        if not result_text:
            log.warning("[cover] title-only prompt no usable result (empty result text) "
                        "— falling back to bare title")
            return "", "", []
        prompt, vi_title, title_segments = _parse_vision_cover_json(result_text)
        if prompt:
            keyed = [s["text"] for s in title_segments if s.get("key")]
            log.info("[cover] title-only-crafted SDXL prompt (%d chars): %s | vi_title=%r | key=%r",
                     len(prompt), prompt[:200], vi_title[:80], keyed)
        else:
            log.warning("[cover] title-only prompt failed (unparseable JSON) — falling back to bare title")
        return prompt, vi_title, title_segments
    except Exception as e:  # noqa: BLE001 — this fallback must never crash the cover
        log.warning("[cover] title-only prompt failed (%s) — falling back to bare title", str(e)[:200])
        try:
            _kill_proc_tree(proc)
        except Exception:
            pass
        return "", "", []
    finally:
        _unregister_job_proc(_kill_job, proc)


# Vision-cover title overlay geometry (fractions of the cover height / width).
_COVER_TITLE_HEIGHT_FRAC = float(os.getenv("COVER_TITLE_HEIGHT_FRAC", "0.62"))  # target block height
_COVER_TITLE_MAX_W_FRAC = 0.95    # wrapped block width ceiling (fraction of image width)
_COVER_TITLE_SCRIM_ALPHA = 120    # peak scrim opacity behind the title band (feathered edges)
# FIXED thumbmagic title palette (env-overridable, parsed at draw time so a bad env value
# can't crash import): normal words WHITE, key words YELLOW, heavy BLACK outline on all.
# Used by the FLAT fallback overlay.
_COVER_TITLE_COLOR_HEX = os.getenv("COVER_TITLE_COLOR", "#FFFFFF")
_COVER_TITLE_ACCENT_HEX = os.getenv("COVER_TITLE_ACCENT", "#FDE919")
_COVER_TITLE_STROKE_HEX = os.getenv("COVER_TITLE_STROKE", "#000000")

# --- Fancy "text plates" overlay (default) knobs (all env-overridable) --------
# All-caps (Unicode/Vietnamese-safe) per-cluster colored rounded plates with a drop
# shadow + soft glow border. Exactly TWO plate backgrounds: KEY = yellow, NORMAL = dark.
_COVER_TITLE_UPPERCASE = (os.getenv("COVER_TITLE_UPPERCASE", "1") or "").strip().lower() not in ("0", "off", "false", "no", "")
# UNIFORM tilt magnitude + MODE. mode: auto | always | never.
#   auto (default) -> FLAT for the MAJORITY of covers; a tilted look is a deterministic
#                     MINORITY, selected from the cover `seed` (tilt when seed%100 < PCT).
#   always -> force tilt on every cover. never -> force flat on every cover.
# COVER_TITLE_TILT_PCT is the auto-mode share (0..100) of seeds that tilt (default 25 ->
# ~25% tilted, ~75% straight). Seed-driven so a given cover's tilt is reproducible.
_COVER_TITLE_TILT_DEG = float(os.getenv("COVER_TITLE_TILT_DEG", "5"))
_COVER_TITLE_TILT_MODE = (os.getenv("COVER_TITLE_TILT_MODE", "auto") or "auto").strip().lower()
_COVER_TITLE_TILT_PCT = int(os.getenv("COVER_TITLE_TILT_PCT", "25"))
# ONLY the KEY cluster gets a background plate; NORMAL clusters render text directly over
# the image (no rectangle). The KEY plate bg = the RENDERED image's DOMINANT color (auto),
# optionally a gradient. Override with COVER_TITLE_KEY_BG (hex) to force a fixed color.
_COVER_TITLE_KEY_BG = (os.getenv("COVER_TITLE_KEY_BG", "") or "").strip()      # "" = derive from image
_COVER_TITLE_KEY_GRADIENT = (os.getenv("COVER_TITLE_KEY_GRADIENT", "1") or "").strip().lower() not in ("0", "off", "false", "no", "")
_COVER_TITLE_KEY_SAT_BOOST = float(os.getenv("COVER_TITLE_KEY_SAT_BOOST", "1.35"))  # pop the plate hue
# NORMAL-cluster TEXT + STROKE (no plate behind them, so they lean on stroke+glow for
# legibility over the raw image). KEY text/stroke are AUTO-picked from the plate-bg
# luminance (see _composite_title_plates) so any dominant hue stays legible.
_COVER_TITLE_TEXT_NORMAL = os.getenv("COVER_TITLE_TEXT_NORMAL", "#FFFFFF")
_COVER_TITLE_STROKE_NORMAL = os.getenv("COVER_TITLE_STROKE_NORMAL", "#0A0A0A")
# Soft GLOW/blur border behind the crisp glyphs (a blurred silhouette in the glow color).
_COVER_TITLE_GLOW = (os.getenv("COVER_TITLE_GLOW", "1") or "").strip().lower() not in ("0", "off", "false", "no", "")
_COVER_TITLE_GLOW_RADIUS = float(os.getenv("COVER_TITLE_GLOW_RADIUS", "0.06"))  # fraction of font px
# KEY glow is AUTO (contrasts the auto-picked key text); only the NORMAL glow is env-set.
_COVER_TITLE_GLOW_NORMAL = os.getenv("COVER_TITLE_GLOW_NORMAL", "#000000")      # glow on normal text
# Supersample factor for crisp (non-smeared) rotated text: draw at N x, rotate BICUBIC,
# downscale LANCZOS. 1 = off. Env-tunable.
_COVER_TITLE_SS = max(1, int(os.getenv("COVER_TITLE_SS", "3")))
# Side-aware placement: the title block goes on the emptier half, opposite the detected
# subject. SIDE_W_FRAC = column width (<= this fraction of W); SIDE_H_FRAC = vertical
# extent allowed for the taller narrow stack; SIDE_MIN_DIFF = min relative left/right
# saliency gap to call a subject side (below it -> centered fallback).
_COVER_TITLE_SIDE_W_FRAC = float(os.getenv("COVER_TITLE_SIDE_W_FRAC", "0.50"))
_COVER_TITLE_SIDE_H_FRAC = float(os.getenv("COVER_TITLE_SIDE_H_FRAC", "0.86"))
_COVER_TITLE_SIDE_MIN_DIFF = float(os.getenv("COVER_TITLE_SIDE_MIN_DIFF", "0.08"))
# Center-fallback (near-symmetric image) block target — also enlarged so the title is big.
_COVER_TITLE_CENTER_W_FRAC = float(os.getenv("COVER_TITLE_CENTER_W_FRAC", "0.92"))
# PORTRAIT boost (owner request 2026-07-29): on a 9:16 cover the narrow column caps the
# font well below what the tall frame can carry, so the AUTO title reads too small.
# Multiply the AUTO target block height by this factor when the cover is PORTRAIT (H > W).
# Applies ONLY to the auto path — a manual fontScale (the Studio slider) is absolute and
# is never boosted. Landscape/square covers are unchanged (boost 1.0).
_COVER_TITLE_PORTRAIT_BOOST = float(os.getenv("COVER_TITLE_PORTRAIT_BOOST", "1.30"))
# PORTRAIT row gap: the VISIBLE (ink-to-ink) vertical space between two stacked title rows,
# in px at the cover's own resolution. Portrait only — landscape keeps the fill-to-target
# spacing. NOT a box-to-box gap: each rendered plate carries transparent slack (glow radius +
# pad_y) above and below its ink, so a 10px BOX gap measured 44px of visible space. We
# therefore subtract the measured slack (see _portrait_row_box_gap) — which also makes the
# knob independent of font size and glow radius. See the note at the gap computation.
_COVER_TITLE_ROW_GAP_PX = int(os.getenv("COVER_TITLE_ROW_GAP_PX", "10"))
# PORTRAIT title-column width as a fraction of the image width (owner request 2026-07-29:
# "tăng bề ngang text khi tạo cover dọc lên 100% ảnh"). 1.0 = the column spans the WHOLE frame
# instead of the ~half-frame side column. Portrait only (H > W); landscape keeps the side/center
# columns. Lower it (e.g. 0.94) to leave a side margin; 0 restores the old side-column behavior.
_COVER_TITLE_PORTRAIT_W_FRAC = float(os.getenv("COVER_TITLE_PORTRAIT_W_FRAC", "1.0"))


def _title_words_with_colors(vi_title: str, title_segments: list) -> list:
    """Build the per-word (word, is_key) list that EXACTLY reconstructs `vi_title` — no
    duplicated and no dropped words — assigning key=True to words covered by a key:true
    segment. Anchors on vi_title's word sequence (walked ONCE) and marks the KEY segments'
    word-runs found in order; non-key / non-matching / overlapping segments never add or
    remove words. Guard: if the result doesn't rejoin to vi_title, fall back to all-normal
    (single-color) so a cover is never wrong. Returns [] for an empty title."""
    vi_words = (vi_title or "").split()
    if not vi_words:
        return []
    key_flags = [False] * len(vi_words)
    pointer = 0
    for seg in (title_segments or []):
        if not isinstance(seg, dict) or not seg.get("key"):
            continue
        seg_words = str(seg.get("text") or "").split()
        if not seg_words:
            continue
        # Find seg_words as a contiguous run in vi_words at/after the pointer (in order).
        n = len(seg_words)
        found = -1
        for p in range(pointer, len(vi_words) - n + 1):
            if vi_words[p:p + n] == seg_words:
                found = p
                break
        if found >= 0:
            for k in range(found, found + n):
                key_flags[k] = True
            pointer = found + n
    words = list(zip(vi_words, key_flags))
    # Guard: drawn words MUST reconstruct vi_title exactly.
    if " ".join(w for w, _k in words) != " ".join(vi_words):
        log.warning("[cover] title word/color rebuild mismatch — falling back to single-color")
        return [(w, False) for w in vi_words]
    return words


def _composite_title_flat(img, vi_title: str, title_segments: list):
    """FALLBACK title overlay: flat white/yellow wrapped text over a feathered scrim.
    Kept as the safety net for _composite_title's fancy plates path — same word/color
    guarantees (exact vi_title reconstruction). Returns a NEW RGB image; on any failure
    returns the original."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import numpy as np

        # 1) Per-word tokens carrying their key flag — anchored to vi_title (no dup/drop).
        words = _title_words_with_colors(vi_title, title_segments)
        if not words:
            return img
        # Per-word typed-line index so owner '\n' hard breaks are honored in the wrap too
        # (fall back to a single line if it doesn't line up).
        line_idx = _word_line_indices(vi_title)
        if len(line_idx) != len(words):
            line_idx = [0] * len(words)

        base = img.convert("RGB")
        W, H = base.size
        draw0 = ImageDraw.Draw(base)  # measuring context
        font_path, _is_fallback = _cover_text_font_path()
        margin = round(0.045 * min(W, H))
        max_w = _COVER_TITLE_MAX_W_FRAC * W
        # Same portrait boost as the plates path (see _COVER_TITLE_PORTRAIT_BOOST).
        target_h = _COVER_TITLE_HEIGHT_FRAC * H * (_COVER_TITLE_PORTRAIT_BOOST if H > W else 1.0)

        def make_font(px):
            try:
                return ImageFont.truetype(font_path, px)
            except Exception:
                return ImageFont.truetype("DejaVuSans.ttf", px)

        def wrap_words(font):
            """Greedy word-wrap keeping per-word tokens; returns [[(w,key,width),...]].
            A change in the owner-typed line index forces a hard break (honors '\\n')."""
            space_w = draw0.textlength(" ", font=font)
            lines, cur, cur_w = [], [], 0.0
            prev_li = None
            for (w, key), li in zip(words, line_idx):
                ww = draw0.textlength(w, font=font)
                add = ww if not cur else space_w + ww
                force_break = prev_li is not None and li != prev_li
                if cur and (force_break or cur_w + add > max_w):
                    lines.append(cur)
                    cur, cur_w = [(w, key, ww)], ww
                else:
                    cur.append((w, key, ww))
                    cur_w += add
                prev_li = li
            if cur:
                lines.append(cur)
            return lines, space_w

        # 2) Largest font whose wrapped block fits BOTH the width and the ~40% target
        #    height (bigger font -> fewer words/line -> more lines -> taller block).
        chosen = None
        font_px = max(16, int(0.24 * H))
        while font_px >= 16:
            font = make_font(font_px)
            widest_word = max(draw0.textlength(w, font=font) for w, _ in words)
            if widest_word <= max_w:
                lines, space_w = wrap_words(font)
                ascent, descent = font.getmetrics()
                line_h = ascent + descent
                line_gap = round(font_px * 0.16)
                block_h = line_h * len(lines) + line_gap * max(0, len(lines) - 1)
                if block_h <= target_h:
                    chosen = (font, lines, space_w, line_h, line_gap, block_h, font_px)
                    break
            font_px -= max(2, int(font_px * 0.06))
        if chosen is None:  # extreme fallback: smallest usable font
            font_px = 16
            font = make_font(font_px)
            lines, space_w = wrap_words(font)
            ascent, descent = font.getmetrics()
            line_h = ascent + descent
            line_gap = round(font_px * 0.16)
            block_h = min(int(target_h), line_h * len(lines) + line_gap * max(0, len(lines) - 1))
            chosen = (font, lines, space_w, line_h, line_gap, block_h, font_px)
        font, lines, space_w, line_h, line_gap, block_h, font_px = chosen

        # 3) Adaptive vertical placement: pick the candidate band with the LOWEST edge
        #    energy (grayscale gradient magnitude) that fully fits the block.
        gray = np.asarray(base.convert("L"), dtype=np.int32)
        gx = np.abs(np.diff(gray, axis=1))
        gy = np.abs(np.diff(gray, axis=0))
        row_energy = np.zeros(H, dtype=np.float64)
        row_energy[:] += gx.sum(axis=1)
        row_energy[:-1] += gy.sum(axis=1)
        lo, hi = margin, H - margin - block_h
        if hi <= lo:
            y0 = max(0, (H - block_h) // 2)
        else:
            cands = np.linspace(lo, hi, 9).astype(int)
            y0 = int(min(cands, key=lambda yy: float(row_energy[yy:yy + block_h].sum())))

        # 4) FIXED thumbmagic title palette (env-overridable): normal WHITE, key YELLOW,
        #    heavy BLACK outline — no palette derivation.
        def _hexc(h, default):
            try:
                return _parse_hex_color(h)
            except Exception:
                return default
        normal = _hexc(_COVER_TITLE_COLOR_HEX, (255, 255, 255))
        key_col = _hexc(_COVER_TITLE_ACCENT_HEX, (253, 233, 25))
        stroke_rgb = _hexc(_COVER_TITLE_STROKE_HEX, (0, 0, 0))

        # Feathered dark scrim behind the title region for legibility.
        pad = int(round(0.03 * H))
        ry0, ry1 = max(0, y0 - pad), min(H, y0 + block_h + pad)
        bh = max(1, ry1 - ry0)
        feather = max(1, int(0.20 * bh))
        acol = np.full(bh, float(_COVER_TITLE_SCRIM_ALPHA))
        for k in range(min(feather, bh)):
            f = k / float(feather)
            acol[k] *= f
            acol[bh - 1 - k] *= f
        scrim = np.zeros((bh, W, 4), dtype=np.uint8)
        scrim[..., 3] = np.repeat(acol.astype(np.uint8)[:, None], W, axis=1)
        rgba = base.convert("RGBA")
        rgba.alpha_composite(Image.fromarray(scrim, "RGBA"), (0, ry0))
        out = rgba.convert("RGB")
        draw = ImageDraw.Draw(out)

        stroke_w = max(3, round(font_px * 0.07))  # heavy black outline
        y = y0
        for line in lines:
            line_w = sum(t[2] for t in line) + space_w * max(0, len(line) - 1)
            x = max(margin, (W - line_w) / 2.0)  # horizontal-center
            for (w, key, ww) in line:
                col = key_col if key else normal
                draw.text((x, y), w, font=font, fill=col,
                          stroke_width=stroke_w, stroke_fill=stroke_rgb)
                x += ww + space_w
            y += line_h + line_gap
        log.info("[cover] title overlay: font=%dpx lines=%d block=%.0f/%.0f y0=%d normal=%s key=%s stroke=%s",
                 font_px, len(lines), block_h, target_h, y0, _hex(normal), _hex(key_col), _hex(stroke_rgb))
        return out
    except Exception as e:  # noqa: BLE001 — overlay must never lose the cover
        log.warning("[cover] title overlay failed (%s) — keeping image-only cover", str(e)[:200])
        return img


def _word_line_indices(vi_title: str) -> list:
    r"""Per-word LINE index (0-based) for the words of `vi_title`, in the SAME order as
    vi_title.split(). Explicit '\n' (and '\r\n'/'\r') START a new line; other whitespace
    only separates words WITHIN a line. Used to honor owner-typed HARD line breaks in the
    title overlay (a newline forces a plate/row boundary). Returned length == the number
    of words (empty typed lines contribute no words and no index)."""
    out: list[int] = []
    text = (vi_title or "").replace("\r\n", "\n").replace("\r", "\n")
    for li, line in enumerate(text.split("\n")):
        for _w in line.split():
            out.append(li)
    return out


def _title_clusters(vi_title: str, title_segments: list) -> list:
    r"""Group the title into visual CLUSTERS (one plate each): consecutive words of the
    SAME key-flag AND the SAME owner-typed line merge into one cluster. Anchored on
    _title_words_with_colors so the clusters' words EXACTLY reconstruct vi_title (no
    dup/drop). Explicit '\n' in vi_title forces a cluster boundary even between same-key
    words, so each typed line renders as its own plate/row (a single typed line still
    auto-wraps INSIDE its plate when it's too wide for the column). If there's only ONE
    cluster, split it into <=2 word-balanced halves so there are >=2 plates to style.
    Returns [{text, key}]."""
    words = _title_words_with_colors(vi_title, title_segments)  # [(w, key)]
    if not words:
        return []
    # Per-word typed-line index; fall back to a single line if it doesn't line up (guard).
    line_idx = _word_line_indices(vi_title)
    if len(line_idx) != len(words):
        line_idx = [0] * len(words)
    clusters = []
    cur, cur_key, cur_line = [words[0][0]], words[0][1], line_idx[0]
    for (w, k), li in zip(words[1:], line_idx[1:]):
        if k == cur_key and li == cur_line:
            cur.append(w)
        else:
            clusters.append({"text": " ".join(cur), "key": cur_key})
            cur, cur_key, cur_line = [w], k, li
    clusters.append({"text": " ".join(cur), "key": cur_key})
    # Single-cluster balance-split fires only when NO newline and NO key transition split
    # the title — i.e. exactly the old behavior for a plain one-line, all-normal title.
    if len(clusters) == 1 and len(words) >= 2:
        ws = [w for w, _ in words]
        key = clusters[0]["key"]
        mid = (len(ws) + 1) // 2
        clusters = [{"text": " ".join(ws[:mid]), "key": key},
                    {"text": " ".join(ws[mid:]), "key": key}]
    return clusters


def _cover_palette(img, n: int = 6) -> tuple:
    """Return the RENDERED cover's DOMINANT color (the most-populous quantized color).
    Falls back to a neutral slate on any failure. Used for the key plate's background."""
    try:
        q = img.convert("RGB").quantize(colors=n)
        pal = q.getpalette() or []
        counts = sorted(q.getcolors() or [], reverse=True)  # [(count, idx), ...]
        if not counts:
            return (60, 60, 72)
        idx = counts[0][1]
        return (pal[idx * 3], pal[idx * 3 + 1], pal[idx * 3 + 2])
    except Exception:
        return (60, 60, 72)


def _boost_saturation(rgb: tuple, factor: float) -> tuple:
    """Multiply the color's HSV saturation by `factor` (clamped) so a derived plate hue
    pops. Also floors value a touch so a near-black dominant still reads as a color."""
    try:
        import colorsys
        r, g, b = [c / 255.0 for c in rgb]
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        s = max(0.0, min(1.0, s * factor))
        v = max(v, 0.22)
        r, g, b = colorsys.hsv_to_rgb(h, s, v)
        return (round(r * 255), round(g * 255), round(b * 255))
    except Exception:
        return rgb


def _jitter_hsv(rgb: tuple, rng, hue_deg: float = 12.0, sat: float = 0.12,
                val: float = 0.12) -> tuple:
    """Apply a small SEEDED HSV jitter to `rgb` (rng = a random.Random). BOUNDED so the
    result stays clearly in the SAME family as the input: hue ±hue_deg degrees, saturation
    ±sat, value ±val (clamped to [0,1]). Used to slightly shift the image's dominant color
    per apply-seed without drifting off the image's actual dominant hue. Best-effort:
    returns the input unchanged on any failure."""
    try:
        import colorsys
        r, g, b = [c / 255.0 for c in rgb]
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        h = (h + rng.uniform(-hue_deg, hue_deg) / 360.0) % 1.0
        s = max(0.0, min(1.0, s + rng.uniform(-sat, sat)))
        v = max(0.0, min(1.0, v + rng.uniform(-val, val)))
        r, g, b = colorsys.hsv_to_rgb(h, s, v)
        return (round(r * 255), round(g * 255), round(b * 255))
    except Exception:
        return rgb


def _stroke_contrast_guard(fill: tuple, stroke: tuple) -> tuple:
    """Never let the stroke/border equal or near-match the text fill (that is what made
    black-text/black-border SMEAR). If `stroke` doesn't contrast `fill` enough, flip it to
    a contrasting neutral — white when the fill is dark, near-black when the fill is light."""
    if _contrast_ratio(fill, stroke) >= 2.2:
        return stroke
    return (255, 255, 255) if _relative_luminance(fill) < 0.5 else (10, 10, 10)


def _render_plate(lines, font_path: str, font_px: int, plate_bg, plate_bg2, text_col: tuple,
                  stroke_col: tuple, glow_col: tuple, glow_on: bool, angle: float,
                  stroke_w: int, pad_x: int, pad_y: int, ss: int = None, grad_angle: float = 0.0,
                  align: str = "center", guard_stroke: bool = True):
    """Render ONE title plate (one or MORE stacked, centered lines) to a sharp RGBA image.

    `align` ("center" default | "left" | "right") aligns the plate's own lines against each
    other inside the block — only meaningful for a MULTI-line plate. `guard_stroke` False
    skips _stroke_contrast_guard so a MANUALLY chosen border color is honored verbatim
    (the guard would otherwise flip a low-contrast pick to white/near-black).

    `plate_bg` None  -> NO background rectangle: the text is drawn FREE over the image
                        (relies on its stroke + glow for legibility). Used for NORMAL clusters.
    `plate_bg` set   -> a rounded-rectangle plate (with a TIGHT drop shadow) behind the text;
                        if `plate_bg2` is also set the plate is filled with a vertical
                        GRADIENT plate_bg -> plate_bg2. Used for the KEY cluster.
    Text always has a soft GLOW/blur border (blurred silhouette in `glow_col`) + a thin crisp
    CONTRASTING stroke. Crispness: drawn at `ss`x, rotated (BICUBIC, expand), downscaled
    (LANCZOS). Text fill and stroke are INDEPENDENT and guarded to always contrast."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    if ss is None:
        ss = _COVER_TITLE_SS
    ss = max(1, int(ss))
    if isinstance(lines, str):
        lines = [lines]
    lines = [ln for ln in lines if ln != ""] or [" "]
    if guard_stroke:
        stroke_col = _stroke_contrast_guard(text_col, stroke_col)  # never fill==stroke

    def _mk(px):
        try:
            return ImageFont.truetype(font_path, px)
        except Exception:
            return ImageFont.truetype("DejaVuSans.ttf", px)

    font = _mk(max(1, font_px * ss))
    sw = max(1, stroke_w * ss)
    px_, py_ = pad_x * ss, pad_y * ss
    line_gap = round(0.08 * font_px * ss)
    glow_r = max(1, round(_COVER_TITLE_GLOW_RADIUS * font_px * ss))
    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    d0 = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    # Per-line INK bbox (includes the stroke); block width = widest line's real ink.
    lbb = [d0.textbbox((0, 0), ln, font=font, stroke_width=sw) for ln in lines]
    lw = [b[2] - b[0] for b in lbb]
    tw = max(lw) if lw else 1
    n = len(lines)
    # font.getmetrics() ascent/descent is ASYMMETRIC (empty headroom above caps + a fixed
    # descent slot below the baseline; VN diacritics add extra top height) — using it to
    # size/place the block is what floated the glyphs HIGH inside the plate. Use it ONLY as
    # the uniform baseline-to-baseline advance (even multiline spacing); size the plate from
    # the ACTUAL drawn-pixel INK bounds so the top margin == the bottom margin.
    step = line_h + line_gap
    ink_top = min(i * step + lbb[i][1] for i in range(n))
    ink_bottom = max(i * step + lbb[i][3] for i in range(n))
    block_ink_h = max(1, ink_bottom - ink_top)
    pw, ph = tw + 2 * px_, block_ink_h + 2 * py_
    radius = max(6, round(0.20 * min(pw, ph)))
    # TIGHT shadow: small offset + low blur so it never softens the letters.
    shadow_off = max(2, round(0.02 * ph))
    blur = max(1, round(0.010 * ph))
    margin = shadow_off + blur * 2 + glow_r * 2 + 4 * ss
    cw, chh = pw + 2 * margin, ph + 2 * margin
    canvas = Image.new("RGBA", (cw, chh), (0, 0, 0, 0))

    def _round_rect(drw, box, rad, **kw):
        try:
            drw.rounded_rectangle(box, radius=rad, **kw)
        except Exception:
            drw.rectangle(box, **kw)  # older Pillow fallback

    # Background PLATE only for the KEY cluster (plate_bg set); NORMAL clusters (plate_bg
    # None) draw text directly over the image — no rectangle, no shadow.
    if plate_bg is not None:
        # Drop shadow: small offset, low-blur dark rounded rect (crisp, no glyph bleed).
        sh = Image.new("RGBA", (cw, chh), (0, 0, 0, 0))
        _round_rect(ImageDraw.Draw(sh),
                    [margin + shadow_off, margin + shadow_off, margin + pw + shadow_off, margin + ph + shadow_off],
                    radius, fill=(0, 0, 0, 180))
        canvas = Image.alpha_composite(canvas, sh.filter(ImageFilter.GaussianBlur(blur)))
        if plate_bg2 is not None:
            # Vertical gradient plate_bg -> plate_bg2, clipped to the rounded-rect shape.
            grad = _plate_gradient((pw, ph), plate_bg, plate_bg2, grad_angle)
            mask = Image.new("L", (pw, ph), 0)
            _round_rect(ImageDraw.Draw(mask), [0, 0, pw - 1, ph - 1], radius, fill=255)
            canvas.paste(grad, (margin, margin), mask)
        else:
            _round_rect(ImageDraw.Draw(canvas), [margin, margin, margin + pw, margin + ph],
                        radius, fill=plate_bg + (255,))
    pd = ImageDraw.Draw(canvas)

    def _text_positions():
        # Align each line horizontally in the block (default CENTER; left/right flush the
        # lines to that edge); place the WHOLE ink block so its real ink-top lands exactly
        # `py_` below the inner top edge — with ph sized to the ink extent this yields top
        # margin == bottom margin (glyphs optically centered, not high).
        y_off = margin + py_ - ink_top
        for i, (ln, b) in enumerate(zip(lines, lbb)):
            line_w = b[2] - b[0]
            if align == "left":
                slack = 0.0
            elif align == "right":
                slack = tw - line_w
            else:
                slack = (tw - line_w) / 2.0
            x = margin + px_ + slack - b[0]
            yield ln, x, i * step + y_off

    # Soft GLOW/blur border: a blurred silhouette of the text in glow_col, behind the glyphs.
    if glow_on:
        glow = Image.new("RGBA", (cw, chh), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        for ln, x, y in _text_positions():
            gd.text((x, y), ln, font=font, fill=glow_col + (255,),
                    stroke_width=max(1, round(sw * 1.3)), stroke_fill=glow_col + (255,))
        canvas = Image.alpha_composite(canvas, glow.filter(ImageFilter.GaussianBlur(glow_r)))
        pd = ImageDraw.Draw(canvas)

    # Crisp glyphs: text fill + a thin CONTRASTING stroke (independent color) on top.
    for ln, x, y in _text_positions():
        pd.text((x, y), ln, font=font, fill=text_col + (255,),
                stroke_width=sw, stroke_fill=stroke_col + (255,))

    if abs(angle) >= 0.1:
        canvas = canvas.rotate(angle, expand=True, resample=Image.BICUBIC)
    if ss > 1:
        fw, fh = canvas.size
        canvas = canvas.resize((max(1, round(fw / ss)), max(1, round(fh / ss))), Image.LANCZOS)
    return canvas


def _detect_subject_side(base, emap, min_diff: float) -> str:
    """Compare LEFT vs RIGHT half saliency (edge energy + a brightness term — a subject
    usually has both more detail AND more light) on the rendered image. Returns 'left' or
    'right' for the busier (subject) half, or 'center' when the two halves are within
    `min_diff` relative gap (no clear subject side)."""
    import numpy as np
    W = emap.shape[1]
    mid = W // 2
    lum = np.asarray(base.convert("L"), dtype=np.float64)
    # Normalize the two saliency terms to comparable scale, then sum.
    e_l, e_r = float(emap[:, :mid].sum()), float(emap[:, mid:].sum())
    b_l, b_r = float(lum[:, :mid].sum()), float(lum[:, mid:].sum())
    e_tot, b_tot = (e_l + e_r) or 1.0, (b_l + b_r) or 1.0
    l = 0.7 * (e_l / e_tot) + 0.3 * (b_l / b_tot)
    r = 0.7 * (e_r / e_tot) + 0.3 * (b_r / b_tot)
    tot = l + r
    rel = abs(l - r) / tot if tot > 0 else 0.0
    if rel < min_diff:
        return "center"
    return "left" if l > r else "right"


# Manual-position anchors for the title overlay -> (vertical bucket, horizontal bucket).
# "center" is the middle-center single-word key; the rest are "<v>-<h>".
_COVER_TITLE_ANCHORS: dict[str, tuple[str, str]] = {
    "top-left": ("top", "left"), "top-center": ("top", "center"), "top-right": ("top", "right"),
    "center-left": ("middle", "left"), "center": ("middle", "center"), "center-right": ("middle", "right"),
    "bottom-left": ("bottom", "left"), "bottom-center": ("bottom", "center"), "bottom-right": ("bottom", "right"),
}


def _portrait_row_box_gap(rendered: list, want_visible_px: int) -> int:
    """Box-to-box gap that yields ~`want_visible_px` of VISIBLE space between stacked title
    rows. Returns an int (often NEGATIVE — see below).

    A rendered plate is a transparent canvas: its ink (text, or the KEY plate's background
    rectangle) sits inside padding + glow-blur headroom, so the box is taller than what the
    eye sees. Measured on a 768x1344 cover at font 64px: ~18px of empty slack below the
    upper row's ink and ~16px above the lower row's, i.e. a 10px BOX gap looks like 44px.
    We subtract that slack: box_gap = want_visible - (slack_below_upper + slack_above_lower),
    averaged over consecutive pairs so the single uniform `gap` the caller applies lands on
    the target. Deriving it from the actual alpha bboxes keeps the knob correct for any font
    size / glow radius instead of hard-coding a magic offset.

    A negative result simply overlaps the two boxes' TRANSPARENT slack — ink can never
    collide, because the returned gap always leaves `want_visible` px between the ink runs
    (want_visible > 0). Falls back to `want_visible` unchanged if the alpha bboxes cannot be
    read (no RGBA / empty plate), which is the conservative, wider spacing."""
    slacks = []
    for i in range(len(rendered) - 1):
        up, lo = rendered[i][0], rendered[i + 1][0]
        try:
            up_bb = up.split()[-1].getbbox() if up.mode == "RGBA" else None
            lo_bb = lo.split()[-1].getbbox() if lo.mode == "RGBA" else None
        except Exception:  # noqa: BLE001 — a measurement failure must not break the cover
            up_bb = lo_bb = None
        if not up_bb or not lo_bb:
            return int(want_visible_px)
        slacks.append((up.size[1] - up_bb[3]) + lo_bb[1])
    if not slacks:
        return int(want_visible_px)
    return int(round(want_visible_px - sum(slacks) / len(slacks)))


def _composite_title_plates(img, vi_title: str, title_segments: list, seed: int = 0,
                            overrides: dict | None = None):
    """FANCY "text plates" title overlay (default): split the title into colored plates
    (mostly FLAT; a seed-selected minority tilt — see the tilt mode below), stacked with a
    slight jitter/overlap, and placed on the emptier half OPPOSITE the detected subject (or
    centered when no clear subject side). Text is supersampled for crisp edges. KEY cluster
    = dominant-color plate; NORMAL clusters = free text. Returns a NEW RGB image. Raises on
    failure (caller falls back to the flat overlay).

    `seed` drives a small deterministic PRNG (random.Random(seed)) so the SAME seed always
    reproduces the SAME look, and a DIFFERENT seed re-rolls a fresh STYLE VARIATION — the
    owner clicks "Áp dụng" with a new seed to re-roll: (1) vertical POSITION (pick among the
    top-few calmest bands + a seeded sub-offset; flip left/center/right when no clear
    subject), (2) key-plate GRADIENT direction + tone stops, (3) a small BOUNDED HSV jitter
    of the image dominant color (stays in the same family), (4) the existing tilt minority.
    All bounded to stay tasteful + legible (contrast re-checked AFTER the color jitter)."""
    from PIL import Image, ImageDraw, ImageFont
    import numpy as np
    import math
    import random

    clusters = _title_clusters(vi_title, title_segments)
    if not clusters:
        return img
    base = img.convert("RGB")
    W, H = base.size
    font_path, _fb = _cover_text_font_path()
    margin = round(0.03 * min(W, H))
    # Deterministic PRNG: same seed -> same variation; a new seed re-rolls the look.
    rng = random.Random(int(seed) & 0x7FFFFFFF)
    # Manual overrides: a SET facet overrides; anything left None/auto still re-rolls off seed.
    # With NO overrides (the default) every branch below takes the exact current auto path,
    # so the all-omitted baseline stays byte-identical.
    ov = overrides or {}
    ov_pos = ov.get("position") or None           # None/"auto" -> seeded; else an anchor key
    ov_key_color = ov.get("key_color")            # None -> auto dominant(+jitter); else (r,g,b)
    ov_key_color2 = ov.get("key_color2")          # None -> auto-derived 2nd stop; else (r,g,b)
    ov_gradient = ov.get("gradient", True)        # False -> solid plate fill
    ov_stroke_color = ov.get("stroke_color")      # None -> auto contrast pick; else (r,g,b) BORDER color
    ov_align = ov.get("align") or None            # None/"auto" -> centered in the column; else l/c/r
    ov_font_scale = ov.get("font_scale")          # None -> auto fill; else block-height fraction
    ov_tilt = ov.get("tilt_deg")                  # None -> seeded minority; else exact degrees

    def _hexc(h, default):
        try:
            return _parse_hex_color(h)
        except Exception:
            return default

    # KEY plate background: forced hex (COVER_TITLE_KEY_BG) OR the RENDERED image's DOMINANT
    # color (saturation-boosted so it pops). Optional deterministic GRADIENT (dominant ->
    # a darker variant). Text/stroke/glow are AUTO-picked from the plate-bg luminance so
    # ANY dominant hue stays legible (black text on light bg, white on dark; contrast guard).
    if ov_key_color is not None:
        key_bg = ov_key_color            # MANUAL base color (verbatim; contrast guard still applies)
    elif _COVER_TITLE_KEY_BG:
        key_bg = _hexc(_COVER_TITLE_KEY_BG, (245, 197, 66))  # env-forced hex: no seeded jitter
    else:
        # Image dominant color + a small BOUNDED seeded HSV jitter, so each apply-seed shifts
        # the plate color a little WITHOUT drifting off the image's dominant family.
        dom = _jitter_hsv(_cover_palette(base), rng, hue_deg=12.0, sat=0.12, val=0.12)
        key_bg = _boost_saturation(dom, _COVER_TITLE_KEY_SAT_BOOST)
    # GRADIENT (default on) second stop: a MANUAL keyColor2 (only with a manual keyColor) is
    # used verbatim as the bottom stop (from key_bg -> key_bg2); else it varies by seed —
    # mostly DARKER, sometimes slightly LIGHTER, bounded so it stays tasteful and in-palette.
    # gradient=False -> SOLID plate. (Angle is resolved later; still seeded when auto.)
    if ov_gradient and _COVER_TITLE_KEY_GRADIENT:
        if ov_key_color is not None and ov_key_color2 is not None:
            key_bg2 = ov_key_color2                      # exact manual FROM->TO stops
        elif rng.random() < 0.75:
            key_bg2 = _blend(key_bg, (0, 0, 0), rng.uniform(0.28, 0.52))
        else:
            key_bg2 = _blend(key_bg, (255, 255, 255), rng.uniform(0.12, 0.28))
    else:
        key_bg2 = None
    # Slight diagonal gradient direction (from vertical) selected by seed; bounded to ±18°.
    grad_angle = rng.uniform(-18.0, 18.0) if key_bg2 is not None else 0.0
    # Legibility is re-checked AFTER the jitter: text/stroke/glow are derived from the FINAL
    # (jittered) plate luminance, and _stroke_contrast_guard enforces a contrasting outline.
    avg_bg = _blend(key_bg, key_bg2, 0.5) if key_bg2 else key_bg
    key_text = (10, 10, 10) if _relative_luminance(avg_bg) > 0.55 else (245, 245, 245)
    key_stroke = (255, 255, 255) if _relative_luminance(key_text) < 0.5 else (10, 10, 10)
    key_stroke = _stroke_contrast_guard(key_text, key_stroke)
    key_glow = key_stroke  # halo contrasts the text

    # NORMAL clusters have NO plate — text drawn directly over the image with stroke+glow.
    text_normal = _hexc(_COVER_TITLE_TEXT_NORMAL, (255, 255, 255))
    stroke_normal = _hexc(_COVER_TITLE_STROKE_NORMAL, (10, 10, 10))
    glow_normal = _hexc(_COVER_TITLE_GLOW_NORMAL, (0, 0, 0))

    # MANUAL text BORDER (stroke) color — applies to EVERY plate (key + normal) so the whole
    # title gets one consistent outline. When set we also DISABLE _stroke_contrast_guard for
    # the stroke: the guard exists to stop an auto-derived outline from matching the fill and
    # smearing, but here the owner picked the color deliberately, so silently flipping it to
    # white/black would read as "the picker does nothing". The GLOW stays on its auto value
    # (it is a separate blurred halo; forcing it to the border color would fatten the outline).
    if ov_stroke_color is not None:
        key_stroke = stroke_normal = ov_stroke_color

    # Per-cluster styling: ONLY the KEY cluster gets a bg plate; NORMAL clusters bg=None.
    # The tilt angle is resolved AFTER placement (tilt mode), then applied uniformly below.
    plates = []
    for c in clusters:
        text = c["text"].upper() if _COVER_TITLE_UPPERCASE else c["text"]
        if c["key"]:
            plates.append({"text": text, "key": True, "bg": key_bg, "bg2": key_bg2,
                           "tc": key_text, "sc": key_stroke, "gc": key_glow})
        else:
            plates.append({"text": text, "key": False, "bg": None, "bg2": None,
                           "tc": text_normal, "sc": stroke_normal, "gc": glow_normal})

    # 2D edge-magnitude map (reused for side detection AND the vertical band search).
    gray = np.asarray(base.convert("L"), dtype=np.int32)
    emap = np.zeros((H, W), dtype=np.float64)
    emap[:, :W - 1] += np.abs(np.diff(gray, axis=1))
    emap[:-1, :] += np.abs(np.diff(gray, axis=0))

    # Detect the subject side (also used for the log line). MANUAL position anchor overrides
    # both the horizontal column and the vertical band; else the AUTO seeded placement runs:
    # place OPPOSITE the detected subject, or — when NO clear subject (both halves empty) —
    # the seed may flip to a side or stay centered (never landing over a subject).
    subject_side = _detect_subject_side(base, emap, _COVER_TITLE_SIDE_MIN_DIFF)
    if ov_pos:
        vbucket, hbucket = _COVER_TITLE_ANCHORS[ov_pos]
        placement = "center" if hbucket == "center" else hbucket  # "left"/"right"/"center"
    else:
        vbucket = None  # auto vertical band search below
        if subject_side == "center":
            placement = rng.choice(["center", "left", "right"])
        else:
            placement = "right" if subject_side == "left" else "left"  # always OPPOSITE the subject
    # AUTO-size portrait boost. On a 9:16 frame the effective font cap is NOT the height
    # budget but the SIDE column's word-fit rule (the longest word must fit ~half the
    # narrow frame) — measured on a real 768x1344 cover the search picks the same px for
    # any target_h. So the boost must widen the COLUMN as well as the height budget:
    # column width scales by the boost (capped at the center width), which raises the
    # word-fit cap — the real +30%. Landscape/square (H <= W) is unchanged (boost 1.0).
    _boost = _COVER_TITLE_PORTRAIT_BOOST if H > W else 1.0
    # PORTRAIT FULL WIDTH (owner request 2026-07-29): on a 9:16 cover the title column spans
    # the WHOLE image width instead of the ~half-frame side column. Consequences, all intended:
    #   • the word-fit rule stops being the binding cap, so target_h (and therefore the manual
    #     fontScale slider) becomes what actually controls size — no column-widening hack needed;
    #   • the LEFT/RIGHT placement anchors no longer shift the text horizontally (there is no
    #     spare width left to shift into), so a full-width portrait title necessarily sits over
    #     the subject; the vertical band search still picks the calmest row.
    # Ink does NOT touch the frame edge: each plate box carries pad_x + glow headroom (~20-25 px
    # at font 64), so a full-bleed COLUMN still renders an inset title.
    # Landscape/square is untouched. Set COVER_TITLE_PORTRAIT_W_FRAC below 1.0 for a margin.
    _portrait_full = H > W and _COVER_TITLE_PORTRAIT_W_FRAC > 0
    side_w_frac = min(_COVER_TITLE_CENTER_W_FRAC, _COVER_TITLE_SIDE_W_FRAC * _boost)
    if ov_font_scale is not None and not _portrait_full:
        # MANUAL slider, LANDSCAPE path. Legacy range (<= 0.8) keeps the legacy narrow column so
        # existing slider values reproduce their old size exactly. The 0.8..1.5 range widens the
        # side column toward the center width, because on a narrow column height alone cannot
        # grow the text past the word-fit cap. Portrait no longer needs this: its column is
        # already the full width, so the slider works through target_h directly.
        if ov_font_scale > 0.8:
            t = (min(ov_font_scale, 1.5) - 0.8) / 0.7
            side_w_frac = (_COVER_TITLE_SIDE_W_FRAC
                           + (_COVER_TITLE_CENTER_W_FRAC - _COVER_TITLE_SIDE_W_FRAC) * t)
        else:
            side_w_frac = _COVER_TITLE_SIDE_W_FRAC
    if _portrait_full:
        half_w = min(1.0, _COVER_TITLE_PORTRAIT_W_FRAC) * W / 2.0
        col_x0 = max(0, int(round(W / 2.0 - half_w)))
        col_x1 = min(W, int(round(W / 2.0 + half_w)))
        max_w = col_x1 - col_x0
        target_h = _COVER_TITLE_SIDE_H_FRAC * H * 0.98 * _boost
    elif placement == "center":
        col_x0, col_x1 = margin, W - margin
        max_w = _COVER_TITLE_CENTER_W_FRAC * W
        target_h = _COVER_TITLE_HEIGHT_FRAC * H * 0.98 * _boost
    else:
        col_w = int(round(side_w_frac * W)) - margin  # ~half the frame (wider when boosted)
        if placement == "left":
            col_x0, col_x1 = margin, margin + col_w
        else:
            col_x0, col_x1 = W - margin - col_w, W - margin
        max_w = col_x1 - col_x0            # plates fill (up to) the side column width
        target_h = _COVER_TITLE_SIDE_H_FRAC * H * 0.98 * _boost
    # fontScale override: the title BLOCK height as a fraction of image height. ABSOLUTE —
    # the manual slider value replaces the auto target entirely (portrait boost included).
    if ov_font_scale is not None:
        target_h = ov_font_scale * H
    col_w = col_x1 - col_x0
    col_cx = (col_x0 + col_x1) / 2.0

    # TILT: never -> flat; always -> tilt; auto (default) -> FLAT for the MAJORITY, tilting
    # only a deterministic MINORITY selected from the cover seed (seed%100 < TILT_PCT). The
    # old "SIDE placement always tilts" rule is DROPPED — it tilted far too many covers.
    # Uniform magnitude (COVER_TITLE_TILT_DEG) across all plates in a tilted cover.
    if ov_tilt is not None:
        eff_tilt = max(-20.0, min(20.0, float(ov_tilt)))  # MANUAL exact tilt (clamped)
    elif _COVER_TITLE_TILT_MODE == "never":
        eff_tilt = 0.0
    elif _COVER_TITLE_TILT_MODE == "always":
        eff_tilt = _COVER_TITLE_TILT_DEG
    else:  # auto
        tilt_pct = max(0, min(100, _COVER_TITLE_TILT_PCT))
        eff_tilt = _COVER_TITLE_TILT_DEG if (int(seed) % 100) < tilt_pct else 0.0

    def make_font(px):
        try:
            return ImageFont.truetype(font_path, px)
        except Exception:
            return ImageFont.truetype("DejaVuSans.ttf", px)

    measure = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    overlap_frac = 0.08
    rad = math.radians(abs(eff_tilt))

    def wrap_and_measure(font, stroke_w, pad_x, pad_y, line_gap):
        """Per plate: wrap the (uppercased) cluster text to the column text-width at word
        boundaries, then compute the multi-line plate box + its rotated bbox. Returns
        (metas, lines_per, word_fits) — word_fits is False when ANY single word is wider
        than the column (would force an ugly mid-word char break), signalling the search
        to shrink the font. Wrapping is what lets a big font FILL a tall narrow column."""
        max_text_w = max(8, max_w * 0.94 - 2 * pad_x)  # leave headroom for tilt/rounding
        ascent, descent = font.getmetrics()
        line_h = ascent + descent
        word_fits = True
        for p in plates:
            for w in p["text"].split():
                if measure.textlength(w, font=font) > max_text_w:
                    word_fits = False
                    break
        metas, lines_per = [], []
        for p in plates:
            lines = _wrap_text_to_width(p["text"], font, max_text_w, measure) or [p["text"]]
            widths = [measure.textlength(ln, font=font) for ln in lines]
            tw = max(widths) if widths else 1
            n = len(lines)
            pw = tw + 2 * pad_x
            ph = n * line_h + (n - 1) * line_gap + 2 * pad_y
            rw = pw * math.cos(rad) + ph * math.sin(rad)
            rh = pw * math.sin(rad) + ph * math.cos(rad)
            metas.append((rw, rh))
            lines_per.append(lines)
        return metas, lines_per, word_fits

    # LARGEST font whose plates fit the column width (no mid-word breaks) AND whose stacked
    # block <= target. A bigger font wraps to MORE lines, so the stacked height GROWS with
    # the font; the winner sits just under target_h — i.e. it FILLS the (enlarged) column.
    chosen = None
    font_px = max(20, int(0.50 * H))  # generous start so wrapping can fill the tall column
    while font_px >= 20:
        font = make_font(font_px)
        stroke_w = max(3, round(font_px * 0.06))
        pad_x, pad_y = round(0.28 * font_px), round(0.16 * font_px)
        line_gap = round(0.08 * font_px)
        metas, lines_per, word_fits = wrap_and_measure(font, stroke_w, pad_x, pad_y, line_gap)
        if word_fits and all(rw <= max_w for rw, _ in metas):
            avg_rh = sum(rh for _, rh in metas) / len(metas)
            overlap = round(overlap_frac * avg_rh)
            stacked_h = sum(rh for _, rh in metas) - overlap * (len(metas) - 1)
            if stacked_h <= target_h:
                chosen = (font_px, stroke_w, pad_x, pad_y, lines_per)
                break
        font_px -= max(2, int(font_px * 0.05))
    if chosen is None:
        font_px = 20
        font = make_font(font_px)
        stroke_w = max(3, round(font_px * 0.06))
        pad_x, pad_y = round(0.28 * font_px), round(0.16 * font_px)
        _metas, lines_per, _wf = wrap_and_measure(font, stroke_w, pad_x, pad_y, round(0.08 * font_px))
        chosen = (font_px, stroke_w, pad_x, pad_y, lines_per)
    font_px, stroke_w, pad_x, pad_y, lines_per = chosen

    # Render every plate (supersampled -> crisp, multi-line, glow + contrast stroke) at the
    # chosen font, using the layout-resolved UNIFORM tilt.
    rendered = []
    for p, lines in zip(plates, lines_per):
        pim = _render_plate(lines, font_path, font_px, p["bg"], p["bg2"], p["tc"], p["sc"],
                            p["gc"], _COVER_TITLE_GLOW, eff_tilt, stroke_w, pad_x, pad_y,
                            grad_angle=grad_angle, align=ov_align or "center",
                            guard_stroke=ov_stroke_color is None)
        rendered.append((pim, p))
    heights = [pim.size[1] for pim, _ in rendered]
    n = len(heights)
    sum_h = sum(heights)
    avg_rh = sum_h / n

    # DISTRIBUTE the plates to FILL the target column height. The font is often capped by
    # the (narrow) column WIDTH, so a tight stack would leave vertical space empty; instead
    # pick a uniform inter-plate `gap` that spreads the block to ~target_h (capped so gaps
    # never get absurd), falling back to a small overlap when the natural stack is already
    # taller than target.
    #
    # PORTRAIT ROW GAP (owner request 2026-07-29): on a 9:16 cover the fill-to-target rule
    # produced huge inter-row gaps — the font is WIDTH-capped (see _COVER_TITLE_PORTRAIT_BOOST),
    # so ALL the leftover vertical budget went into the gaps, and raising target_h for the
    # +30% font boost made them wider still (measured 239 px between two rows). The title read
    # as two far-apart blocks. On portrait we therefore TIGHTEN to a fixed VISIBLE row gap
    # (_COVER_TITLE_ROW_GAP_PX) instead of filling the height. This does NOT shrink the text:
    # the font search bounds itself with `overlap_frac`, not this gap, and it is width-bound on
    # portrait anyway (measured — the same font_px comes back for target_h ×1.0 and ×1.3).
    # `min(gap, ...)` so the pre-existing OVERFLOW branch (stack taller than budget → negative
    # fill_gap → overlap) still wins when it is tighter; we only ever REMOVE space here.
    budget = min(target_h, H - 2 * margin)
    if n > 1:
        fill_gap = (budget - sum_h) / (n - 1)
        min_gap = -round(overlap_frac * avg_rh)          # allow a small overlap
        max_gap = round(1.1 * avg_rh)                    # but never an absurd gap
        gap = int(round(max(min_gap, min(fill_gap, max_gap))))
        if H > W:
            gap = min(gap, _portrait_row_box_gap(rendered, _COVER_TITLE_ROW_GAP_PX))
    else:
        gap = 0
    blk = int(round(sum_h + gap * (n - 1)))
    log.info("[cover-title] %dx%d font=%dpx plates=%d row_box_gap=%dpx block=%dpx (target_h=%dpx)",
             W, H, font_px, n, gap, blk, int(target_h))

    # Vertical placement varies by seed but STAYS calm: rank the candidate bands by edge
    # energy (within the title column) and pick among the TOP-FEW calmest by seed, then add
    # a small seeded sub-offset (<= half a band gap) — a fresh sensible position each apply,
    # never slammed over the busiest detail (the calmness ranking is the guard).
    col_energy = emap[:, col_x0:col_x1].sum(axis=1)
    lo, hi = margin, H - margin - blk
    if hi <= lo:
        y0 = max(0, (H - blk) // 2)
    elif vbucket is not None:
        # MANUAL vertical anchor (from the position override): top / middle / bottom.
        y0 = lo if vbucket == "top" else hi if vbucket == "bottom" else (lo + hi) // 2
    else:
        cands = np.linspace(lo, hi, 9).astype(int)
        ranked = sorted((int(c) for c in cands), key=lambda yy: float(col_energy[yy:yy + blk].sum()))
        topk = ranked[:min(3, len(ranked))]  # among the calmest few only
        y0 = rng.choice(topk)
        gap_band = (hi - lo) / 8.0             # spacing between adjacent candidate bands
        y0 = int(max(lo, min(hi, y0 + int(rng.uniform(-0.5, 0.5) * gap_band))))

    # Compose plates onto a transparent layer (paste clips off-canvas safely), then flatten.
    # Plates are centered within the TITLE COLUMN (col_cx), with a small deterministic
    # horizontal jitter scaled to the column width.
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    y = float(y0)
    log_bits = []
    for i, (pim, p) in enumerate(rendered):
        rw, rh = pim.size
        if ov_align in ("left", "right"):
            # MANUAL edge alignment: flush every plate to the SAME column edge. The seeded
            # per-plate jitter is dropped here — a ±jitter on an edge-aligned stack reads as
            # a misalignment bug, which is exactly what the owner is asking to control.
            x = col_x0 if ov_align == "left" else col_x1 - rw
            x = int(round(x))
        else:
            jitter = (1 if i % 2 == 0 else -1) * round(0.015 * col_w)  # deterministic per index
            x = int(round(col_cx - rw / 2.0 + jitter))
        x = max(0, min(x, W - rw)) if rw <= W else x
        layer.paste(pim, (x, int(round(y))), pim)
        sc_used = _stroke_contrast_guard(p["tc"], p["sc"])
        if p["bg"] is None:
            bg_desc = "NONE"
        elif p["bg2"] is not None:
            bg_desc = f"gradient({_hex(p['bg'])}->{_hex(p['bg2'])})"
        else:
            bg_desc = _hex(p["bg"])
        log_bits.append(f"'{p['text']}'{'*' if p['key'] else ''} bg={bg_desc} "
                        f"txt={_hex(p['tc'])} stroke={_hex(sc_used)} glow={_hex(p['gc'])}")
        y += rh + gap
    out = Image.alpha_composite(base.convert("RGBA"), layer).convert("RGB")
    log.info("[cover] title plates: %d plates font=%dpx tilt=%+.0f (mode=%s, seed=%d, pct=%d, placement=%s) "
             "subject=%s keybg=%s gradAngle=%+.1f glow=%s col=[%d,%d] stacked=%d/%.0f y0=%d | %s",
             len(rendered), font_px, eff_tilt, _COVER_TITLE_TILT_MODE, int(seed), _COVER_TITLE_TILT_PCT,
             placement, subject_side, _hex(key_bg), grad_angle, _COVER_TITLE_GLOW,
             col_x0, col_x1, blk, target_h, y0, " | ".join(log_bits))
    return out


def _composite_title(img, vi_title: str, title_segments: list, seed: int = 0,
                     overrides: dict | None = None):
    """Title overlay entry point: try the FANCY "text plates" treatment; on any failure
    fall back to the flat white/yellow text overlay, and if that also fails return the
    original image — a cover is never lost. `seed` drives the seeded/auto styling; `overrides`
    (optional dict) manually pins individual facets (position/key_color/gradient/font_scale/
    tilt_deg) — a set facet wins, the rest still re-roll off `seed`. The flat fallback is
    best-effort and ignores overrides."""
    try:
        return _composite_title_plates(img, vi_title, title_segments, seed, overrides)
    except Exception as e:  # noqa: BLE001
        log.warning("[cover] fancy title plates failed (%s) — falling back to flat text", str(e)[:200])
        return _composite_title_flat(img, vi_title, title_segments)


def _run_cover_task(task_id: str, req: CoverRequest, prompt: str, style_index: int, seed: int) -> None:
    """Worker (daemon thread): render the cover with live progress, persist as JPG,
    then set the task to 'done'+result or 'error'+error. Never raises out of the thread.

    Prompt source (decided here since the Claude call is slow — belongs in the async
    task): a manual `prompt` is used verbatim (image-only, no overlay); else if
    `sourceLink` is present, TWO tiers are tried in order — (1) Claude VISION analyzes the
    source thumbnail + title, (2) if no thumbnail could be fetched (DRM/private source) or
    vision returned nothing, Claude reasons over the TITLE ALONE (text-only, own world
    knowledge) — both crafting an English SDXL prompt (grounded visual metaphor) AND a
    Vietnamese title translation composited over a subtle scrim at the top. Only if BOTH
    tiers fail does it fall back to the passed bare-title `prompt` with no overlay. With
    no `sourceLink` at all, the bare title-only prompt is used."""
    try:
        width, height = sdxl_dims_for_aspect(req.aspect)

        def cb(pct: int, msg: str) -> None:
            _cover_task_update(task_id, pct=int(pct), msg=msg)

        # Crafted-prompt override: only when there is NO manual prompt and a source link
        # is given. Tier 1 = vision over the source thumbnail; tier 2 = text-only analysis
        # of the title (when the thumbnail is unavailable, e.g. DRM-protected source, or
        # vision came back empty) — so an unfetchable thumbnail no longer silently degrades
        # the cover to the bare title.
        manual = bool((req.prompt or "").strip())
        link = (req.sourceLink or "").strip()
        vi_title = ""            # non-empty only on a crafted path -> overlaid after render
        title_segments: list = []  # ordered {text,key} segments for the multi-color overlay
        if not manual and link:
            cb(1, "Đang phân tích thumbnail gốc…")
            thumb_path = ""
            try:
                thumb_path = _fetch_source_thumbnail(req.page, link).get("thumbPath", "")
            except HTTPException as e:
                log.warning("[cover] source thumbnail fetch failed (%s) — trying title-only analysis",
                            e.detail)
            except Exception as e:  # noqa: BLE001
                log.warning("[cover] source thumbnail fetch error (%s) — trying title-only analysis",
                            str(e)[:200])
            crafted, crafted_vi, crafted_segs = "", "", []
            if thumb_path:
                crafted, crafted_vi, crafted_segs = _vision_cover_prompt(thumb_path, req.title or "")
            if not crafted:
                # Tier 2 — no thumbnail (or vision produced nothing): reason over the TITLE
                # alone using Claude's own knowledge of the subject.
                cb(1, "Đang phân tích tiêu đề…")
                crafted, crafted_vi, crafted_segs = _title_only_cover_prompt(req.title or "")
            if crafted:
                # Replace the title-only base with the crafted prompt + the FIXED
                # thumbmagic style suffix so the high-CTR look is enforced consistently.
                prompt = crafted + COVER_STYLE_SUFFIX
                vi_title = crafted_vi
                title_segments = crafted_segs
                # Surface ONLY the ENGLISH prompt so the FE's coverPromptShown displays
                # it (vi_title is baked onto the image, never into this field).
                _cover_task_update(task_id, prompt=prompt)

        images = _render_cover_image(prompt, width, height, COVER_STEPS, seed, cb)
        if not images:
            _cover_task_update(task_id, status="error", error="ComfyUI returned no cover image", pct=0)
            return

        covers_dir = _covers_dir(req.page)
        os.makedirs(covers_dir, exist_ok=True)
        dest = os.path.join(covers_dir, f"cover_{seed}_{style_index}.jpg")

        # Fetch the ComfyUI /view bytes and persist as JPG (Pillow; raw-bytes fallback).
        try:
            with urllib.request.urlopen(images[0]["url"], timeout=60) as resp:
                raw = resp.read()
        except (urllib.error.URLError, OSError) as e:
            _cover_task_update(task_id, status="error",
                               error=f"failed to fetch cover bytes from ComfyUI: {e}", pct=0)
            return
        base_path = ""            # abs path of the CLEAN (title-less) base (vision path only)
        vi_title_out = ""         # the auto-translated Vietnamese title (vision path only)
        key_words_out: list = []  # the key word(s) Claude flagged (vision path only)
        try:
            from PIL import Image  # local import (Pillow already a dep)
            import io
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            # Vision path only: bake the LARGE, adaptively-placed, palette-synced multi-
            # color Vietnamese title onto the rendered illustration. SDXL can't render VN.
            if vi_title and title_segments:
                # 1) Persist the CLEAN (title-less) render as a reusable base FIRST, so the
                #    FE can re-render the title (owner-edited) on it via /generate/cover/title.
                base_path = os.path.join(covers_dir, f"cover_{seed}_{style_index}_base.jpg")
                img.save(base_path, format="JPEG", quality=90)
                vi_title_out = vi_title
                # keyWords = the word(s) Claude flagged as key (surfaced to the FE).
                key_words_out = [s["text"] for s in title_segments
                                 if isinstance(s, dict) and s.get("key")]
                # 2) Auto-bake the title through the EXACT SAME segment-building + tilt-seed
                #    path as POST /generate/cover/title, compositing on the RE-LOADED clean
                #    base — so the generated cover is pixel-identical to
                #    renderCoverTitle(viTitle, keyWords) and there's NO visual jump when the
                #    owner opens the editor and re-applies the prefilled title unchanged.
                cb(98, "Đang ghép tiêu đề tiếng Việt")
                render_segs = _title_segments_from_keywords(vi_title, key_words_out)
                tilt_seed = _basepath_tilt_seed(os.path.realpath(base_path))
                clean = Image.open(base_path).convert("RGB")
                # tiltDeg from the request pins the auto-bake's tilt (default 0 from the FE
                # unless the owner manually moved the slider); null falls back to the seeded
                # auto minority, same as /generate/cover/title.
                tilt_override = None if req.tiltDeg is None else max(-20.0, min(20.0, float(req.tiltDeg)))
                titled = _composite_title(clean, vi_title, render_segs, tilt_seed,
                                          {"tilt_deg": tilt_override})
                titled.save(dest, format="JPEG", quality=90)
            else:
                img.save(dest, format="JPEG", quality=90)
        except Exception:
            with open(dest, "wb") as f:
                f.write(raw)
            # The PIL path failed -> the clean base / title metadata are unreliable; drop them.
            base_path, vi_title_out, key_words_out = "", "", []

        result = {
            "path": dest,
            "url": f"/media?path={urllib.parse.quote(dest)}",
            "seed": seed,
            "styleIndex": style_index,
            # Vision path extras (empty on the manual / title-only paths): let the FE prefill
            # an editable title box and re-render on the clean base.
            "basePath": base_path,
            "viTitle": vi_title_out,
            "keyWords": key_words_out,
        }
        _cover_task_update(task_id, status="done", pct=100, msg="Hoàn tất", result=result)
    except HTTPException as e:
        _cover_task_update(task_id, status="error", error=str(e.detail), pct=0)
    except Exception as e:  # noqa: BLE001 — worker must never crash the thread
        _cover_task_update(task_id, status="error", error=f"cover render failed: {e}", pct=0)


# NOTE: generate_vision_cover_sync was REMOVED (2026-07-28, owner decision). It was the
# PIPELINE's auto-cover: when a job had no manual cover but did have a source link, the
# runner synthesized one (yt-dlp thumbnail -> Claude vision prompt -> SDXL text2img -> baked
# VN title) and baked it as the video's first frame. Clicking "Tạo video" no longer renders a
# cover on its own. The AUTO_COVER env gate went with it.
#
# Everything MANUAL is untouched and still uses the same building blocks this helper used to
# share (_fetch_source_thumbnail / _vision_cover_prompt / _render_cover_image /
# _composite_title): POST /generate/cover (the Studio "Tạo Cover" button, via
# _run_cover_task) and POST /api/videos/{id}/cover (change a cover after render).


@router.post("/generate/cover")
def generate_cover(req: CoverRequest):
    """Kick off an async SDXL cover render (no Claude) and return immediately.

    Body: { page, title="", aspect="9:16", seed=None, styleIndex=0, summary=None, prompt=None }
    Returns: { taskId }. Poll GET /generate/cover/progress/{taskId} for status/pct/result.
    The 422 'title is required' fires only on the AUTO path (no manual prompt)."""
    # Validate + assemble the prompt UP FRONT so a bad request 422s synchronously
    # (before spawning the thread), exactly like the old synchronous handler did.
    prompt, style_index, seed = _assemble_cover_prompt(req)
    task_id = uuid.uuid4().hex
    with _COVER_TASKS_LOCK:
        _prune_cover_tasks()
        _COVER_TASKS[task_id] = {
            "status": "running", "pct": 0, "msg": "Đang khởi tạo",
            # The final positive prompt sent to SDXL (manual verbatim, or auto
            # title+summary+style) — exposed so the FE can display it while rendering.
            "prompt": prompt,
            "result": None, "error": None, "ts": time.time(),
        }
    threading.Thread(
        target=_run_cover_task, args=(task_id, req, prompt, style_index, seed), daemon=True
    ).start()
    return {"taskId": task_id}


@router.get("/generate/cover/progress/{task_id}")
def generate_cover_progress(task_id: str):
    """Poll an async cover task. Returns { status, pct, msg, prompt, result, error }.
    `prompt` is the final positive prompt sent to SDXL (string; null if unset).
    `result` (only when status=='done') is
    { path, url, seed, styleIndex, basePath, viTitle, keyWords }. On the VISION path,
    basePath is the abs path of the CLEAN (title-less) base, viTitle is the auto-translated
    Vietnamese title, and keyWords is the list of key word(s); on the manual/title-only
    paths those three are '' / '' / []. 404 for an unknown/expired taskId."""
    with _COVER_TASKS_LOCK:
        t = _COVER_TASKS.get(task_id)
        if t is None:
            raise HTTPException(404, "cover task not found or expired")
        return {
            "status": t["status"],
            "pct": t["pct"],
            "msg": t["msg"],
            "prompt": t.get("prompt"),
            "result": t["result"],
            "error": t["error"],
        }


# --- Cover file helpers (path guard + entry builder) --------------------
#
# The covers CACHE dir (<root>/_cache/covers/<page>/) holds every generated cover
# (SDXL cover_*.jpg + _txt_* overlays). It is regenerable; there is no separate
# persistent "saved" library (removed 2026-07-19 — the created-covers listing
# already surfaces every cover). Old saved/ files on disk, if any, are harmless:
# the created listing is top-level only and never lists that subdir.


def _covers_tree_guard(path: str) -> str:
    """Resolve `path` and confirm it is inside the content output root, reusing the
    exact guard style as /media. Returns the realpath; raises HTTPException(403) on
    a path-traversal / outside-tree attempt. Callers additionally scope to the
    covers dir where relevant."""
    root = os.path.realpath(CONTENT_OUTPUT_ROOT)
    full = os.path.realpath(path)
    if full != root and not full.startswith(root + os.sep):
        raise HTTPException(403, "Path is outside the media root")
    return full


def _cover_base_sibling(abs_path: str) -> str:
    """Abs path of the CLEAN (title-less) base for a cover, or "" if there is none.

    Every titled cover is written as a PAIR by the vision path (_run_cover_task; the removed
    pipeline auto-cover used to write the same pair):
        cover_<seed>_<style>.jpg        <- the TITLE is baked into this one
        cover_<seed>_<style>_base.jpg   <- the clean render the title was baked onto
    Only the titled file is handed around (it is what jobs.cover_image_path stores), so a
    cover that came back from a LISTING had no way to point at its base — and re-rendering
    an edited title then composited on top of the already-baked one (the owner's "text
    dính luôn trên hình, bị overlay"). Resolving the sibling here fixes every consumer at
    once. A cover with no sibling (manual-prompt render, _txt_* overlay, hand-dropped file)
    returns "" and callers fall back to the image itself, which is correct — nothing was
    baked into it.

    Also unwinds the OUTPUT of /generate/cover/title, which writes
    "<base-stem>_title_<sha256[:12]>.jpg". Editing the title a SECOND time hands that file
    back as the base, so without this the second edit stacks on the first."""
    root, ext = os.path.splitext(abs_path)
    if root.endswith("_base"):
        return abs_path  # already the clean base
    # "<stem>_title_<12 hex>" -> back to "<stem>" (which is itself usually "..._base")
    m = re.match(r"^(?P<stem>.+)_title_[0-9a-f]{12}$", root)
    if m:
        orig = f"{m.group('stem')}{ext}"
        if os.path.isfile(orig):
            return orig
        # The clean base was deleted (covers dir is a CACHE) — no safe base to composite
        # onto. "" makes the caller fall back to this image, which WOULD stack, so the FE
        # must treat an empty basePath on a *_title_* file as "text no longer editable".
        return ""
    cand = f"{root}_base{ext}"
    return cand if os.path.isfile(cand) else ""


def _cover_entry(page: str, abs_path: str) -> dict:
    """Build the CoverResult-compatible dict for a cover file on disk.
    savedAt = the file's mtime as an ISO-8601 string (local time).
    basePath = the clean title-less base to composite an edited title onto ("" if none)."""
    from datetime import datetime  # local import — only used here
    try:
        mtime = os.path.getmtime(abs_path)
        saved_at = datetime.fromtimestamp(mtime).isoformat()
    except OSError:
        saved_at = ""
    return {
        "path": abs_path,
        "url": f"/media?path={urllib.parse.quote(abs_path)}",
        "filename": os.path.basename(abs_path),
        "savedAt": saved_at,
        "basePath": _cover_base_sibling(abs_path),
    }


class DeleteSavedCoverRequest(BaseModel):
    page: str
    path: str  # abs path of a cover to remove


# --- Cover text overlay (Pillow) ----------------------------------------
#
# SDXL cannot render legible text (see memory: sdxl-english-only-prompt), so the
# title is composited onto the CLEAN generated cover programmatically with Pillow.
# This powers the Studio "cover text" tool: the owner types the title, picks a
# 9-anchor position, a font size (fraction of image height) and a hex color, then
# re-applies until happy. Every apply re-composites FROM THE CLEAN BASE (the FE
# keeps the clean base path and always sends it) so text never stacks.

# The 9 fixed anchors → (vertical, horizontal) buckets.
_COVER_TEXT_ANCHORS: dict[str, tuple[str, str]] = {
    "top-left": ("top", "left"),
    "top-center": ("top", "center"),
    "top-right": ("top", "right"),
    "middle-left": ("middle", "left"),
    "middle-center": ("middle", "center"),
    "middle-right": ("middle", "right"),
    "bottom-left": ("bottom", "left"),
    "bottom-center": ("bottom", "center"),
    "bottom-right": ("bottom", "right"),
}

# fontScale (font height as a fraction of image height) is clamped to this range.
_COVER_TEXT_FONTSCALE_MIN = 0.04
_COVER_TEXT_FONTSCALE_MAX = 0.22
_COVER_TEXT_FONTSCALE_DEFAULT = 0.10


class CoverTextRequest(BaseModel):
    page: str
    basePath: str                       # abs path to the CLEAN (text-free) generated cover
    text: str                           # overlay text (Vietnamese with diacritics)
    position: str = "bottom-center"     # one of _COVER_TEXT_ANCHORS keys
    fontScale: float = _COVER_TEXT_FONTSCALE_DEFAULT  # font height / image height
    color: str = "#FFFFFF"              # hex "#RRGGBB" — solid fill, or gradient TOP color
    strokeColor: str | None = None      # hex outline; None/empty -> auto (black on light / white on dark)
    gradient: bool = False              # fill the text with a vertical gradient color -> color2
    color2: str | None = None           # hex gradient BOTTOM color (required when gradient=true)
    autoStyle: bool = False             # analyze the base image and auto-pick fill/stroke/gradient,
                                        # OVERRIDING color/strokeColor/gradient/color2


class CoverTitleRequest(BaseModel):
    """Re-render the FANCY multi-plate title with owner-edited text on a CLEAN base.
    All style fields default to AUTO/current behavior; a SET field OVERRIDES that facet
    while everything left auto still re-rolls off `seed`."""
    page: str
    basePath: str                       # abs path to the CLEAN (title-less) SDXL base
    text: str                           # the edited Vietnamese title
    keyWords: list[str] | None = None   # words to render on the KEY plate
    seed: int | None = None             # tilt/variation seed; else derived from basePath
    position: str = "auto"              # "auto" or one of the 9 anchors (below)
    keyColor: str | None = None         # "#RRGGBB" KEY plate base / gradient TOP stop; null = auto dominant(+jitter)
    keyColor2: str | None = None        # "#RRGGBB" gradient BOTTOM stop (needs keyColor+gradient); null = auto-derived
    gradient: bool = True               # True = gradient plate; False = solid fill
    strokeColor: str | None = None      # "#RRGGBB" text BORDER (outline) color, all plates; null = auto contrast pick
    align: str = "auto"                 # "auto" | "left" | "center" | "right" — text alignment inside the block
    fontScale: float | None = None      # title block HEIGHT fraction ~[0.2,1.5]; null = auto fill
    tiltDeg: float | None = None        # exact tilt degrees [-20,20] for ALL plates; null = seeded minority


def _parse_hex_color(value: str) -> tuple[int, int, int]:
    """Parse '#RRGGBB' (or 'RRGGBB', or '#RGB') into an (R,G,B) tuple.
    Raises HTTPException(422) on anything that isn't a valid hex color."""
    s = (value or "").strip().lstrip("#")
    if len(s) == 3:  # shorthand #RGB -> #RRGGBB
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", s):
        raise HTTPException(422, "Màu không hợp lệ — dùng dạng hex #RRGGBB")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    """Perceived luminance in [0,1] (Rec. 601 weights). Used to pick a contrasting
    outline: a light fill gets a black outline, a dark fill gets a white outline."""
    r, g, b = rgb
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def _hex(rgb: tuple[int, int, int]) -> str:
    """(R,G,B) -> '#RRGGBB' (uppercase)."""
    return "#%02X%02X%02X" % (int(rgb[0]), int(rgb[1]), int(rgb[2]))


def _blend(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    """Linear blend a->b by t in [0,1] (t=0 -> a, t=1 -> b)."""
    return (round(a[0] + (b[0] - a[0]) * t),
            round(a[1] + (b[1] - a[1]) * t),
            round(a[2] + (b[2] - a[2]) * t))


def _wcag_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 2.x relative luminance (linearized sRGB). Used for contrast ratios —
    more accurate than the Rec.601 approximation for the auto-style contrast gate."""
    def chan(c: int) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def _contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """WCAG contrast ratio in [1, 21] between two colors."""
    la, lb = _wcag_luminance(a), _wcag_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _vertical_gradient(size: tuple[int, int], top: tuple[int, int, int],
                       bottom: tuple[int, int, int], y_top: float, y_bottom: float):
    """Build an RGB image of `size` whose color runs vertically from `top` at y_top
    to `bottom` at y_bottom (clamped above/below that band). Cheap: one column of
    pixels then a horizontal stretch."""
    from PIL import Image
    w, h = size
    col = Image.new("RGB", (1, h))
    span = max(1.0, float(y_bottom - y_top))
    px = col.load()
    for yy in range(h):
        t = (yy - y_top) / span
        t = 0.0 if t < 0 else 1.0 if t > 1 else t
        px[0, yy] = _blend(top, bottom, t)
    return col.resize((w, h))


def _plate_gradient(size: tuple[int, int], c1: tuple[int, int, int],
                    c2: tuple[int, int, int], angle_deg: float = 0.0):
    """RGB gradient of `size` running c1 -> c2 along a direction `angle_deg` measured from
    the VERTICAL (0 = pure top->bottom, matching _vertical_gradient; small angles tilt it
    slightly diagonal). Used for the KEY plate so the gradient DIRECTION can vary per seed.
    numpy-vectorized; falls back to the plain vertical gradient on any failure."""
    try:
        from PIL import Image
        import numpy as np
        import math
        w, h = max(1, int(size[0])), max(1, int(size[1]))
        rad = math.radians(angle_deg)
        dx, dy = math.sin(rad), math.cos(rad)
        ys, xs = np.mgrid[0:h, 0:w].astype(np.float64)
        proj = xs * dx + ys * dy
        pmin, pmax = float(proj.min()), float(proj.max())
        t = (proj - pmin) / (pmax - pmin if pmax > pmin else 1.0)
        a = np.array(c1, dtype=np.float64)
        b = np.array(c2, dtype=np.float64)
        arr = a[None, None, :] * (1.0 - t)[..., None] + b[None, None, :] * t[..., None]
        return Image.fromarray(arr.round().clip(0, 255).astype(np.uint8), "RGB")
    except Exception:
        return _vertical_gradient(size, c1, c2, 0, int(size[1]))


def _auto_style_for_region(img, bbox: tuple[int, int, int, int]) -> dict:
    """Deterministically pick a legible text style for the image REGION the text will
    cover. Returns {color, color2, strokeColor, gradient} as hex strings / bool.

    Heuristic (no randomness — same base+bbox -> same result):
      - Crop the region, compute its mean color and its single dominant color
        (8-color quantize, most-populous palette entry).
      - Dark region  -> LIGHT fill (white, optionally a white-heavy tint of the
        dominant hue as the gradient top); STROKE black.
      - Light region -> DARK fill (near-black, optionally a black-heavy tint);
        STROKE white.
      - Use the tint->anchor gradient only if the tint keeps a strong contrast
        (>= 4.5) against the region mean; otherwise fall back to a solid
        pure-white / near-black fill (always high contrast)."""
    from PIL import Image, ImageStat  # local import (Pillow already a dep)
    region = img.crop(bbox).convert("RGB")
    if region.width < 1 or region.height < 1:
        region = img.convert("RGB")
    mean = tuple(int(c) for c in ImageStat.Stat(region).mean[:3])

    # Dominant color via an 8-color quantize (most-populous palette entry).
    try:
        q = region.quantize(colors=8)
        pal = q.getpalette() or []
        counts = sorted(q.getcolors() or [], reverse=True)  # [(count, idx), ...]
        idx = counts[0][1]
        dom = (pal[idx * 3], pal[idx * 3 + 1], pal[idx * 3 + 2])
    except Exception:
        dom = mean

    dark_region = _wcag_luminance(mean) < 0.5
    if dark_region:
        anchor = (255, 255, 255)            # light fill on a dark region
        tint = _blend(dom, (255, 255, 255), 0.80)
        stroke = (0, 0, 0)
    else:
        anchor = (18, 18, 18)               # dark fill on a light region
        tint = _blend(dom, (0, 0, 0), 0.80)
        stroke = (255, 255, 255)

    if _contrast_ratio(tint, mean) >= 4.5 and _contrast_ratio(tint, anchor) >= 1.15:
        # Styled gradient: tinted hue -> neutral anchor (both stay high-contrast).
        return {"color": _hex(tint), "color2": _hex(anchor),
                "strokeColor": _hex(stroke), "gradient": True}
    # Fallback: solid, guaranteed-legible neutral.
    return {"color": _hex(anchor), "color2": _hex(anchor),
            "strokeColor": _hex(stroke), "gradient": False}


def _wrap_text_to_width(text: str, font, max_width: float, draw) -> list[str]:
    """Greedy word-wrap `text` so each line's rendered width <= max_width.
    A single word longer than max_width is hard-broken character by character so
    it can never overflow the image. Uses the font's real glyph metrics."""
    def measure(s: str) -> float:
        # textlength is accurate for a single line at the font's nominal size.
        return float(draw.textlength(s, font=font))

    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        cur = ""
        for word in words:
            candidate = word if not cur else f"{cur} {word}"
            if measure(candidate) <= max_width or not cur:
                # Word (or first word) fits, OR line empty — but a lone word may
                # still be wider than max_width, so hard-break it below.
                if not cur and measure(word) > max_width:
                    piece = ""
                    for ch in word:
                        if measure(piece + ch) <= max_width or not piece:
                            piece += ch
                        else:
                            lines.append(piece)
                            piece = ch
                    cur = piece
                else:
                    cur = candidate
            else:
                lines.append(cur)
                cur = word
                if measure(cur) > max_width:
                    # The new word alone overflows — hard-break it.
                    piece = ""
                    for ch in word:
                        if measure(piece + ch) <= max_width or not piece:
                            piece += ch
                        else:
                            lines.append(piece)
                            piece = ch
                    cur = piece
        if cur or not lines:
            lines.append(cur)
    return lines or [""]


def _cover_text_font_path() -> tuple[str, bool]:
    """Return (font_path, is_fallback). Prefer the bundled Vietnamese-capable
    Be Vietnam Pro Bold used by the caption/karaoke pipeline (CAPTION_FONT). If it
    is missing, fall back to Pillow's bundled DejaVuSans and flag it so the caller
    can REPORT the fallback (DejaVuSans covers Vietnamese diacritics too)."""
    if os.path.isfile(CAPTION_FONT):
        return CAPTION_FONT, False
    return "DejaVuSans.ttf", True  # resolved by ImageFont.truetype's bundled search


def _infer_cover_seed_style(basename: str) -> tuple[int, int]:
    """Best-effort recover (seed, styleIndex) from a generated cover filename like
    'cover_<seed>_<style>.jpg'. Returns (0, 0) when it doesn't match."""
    m = re.match(r"cover_(\d+)_(\d+)\.", basename)
    if m:
        return int(m.group(1)), int(m.group(2))
    return 0, 0


@router.post("/generate/cover/text")
def generate_cover_text(req: CoverTextRequest):
    """Composite TEXT onto an already-generated (text-free) SDXL cover with Pillow.

    Body: { page, basePath, text, position, fontScale, color,
            strokeColor?, gradient?, color2?, autoStyle? }.
      - basePath    : abs path to the CLEAN cover (guarded to the media root; 404 if missing).
      - position    : one of the 9 anchors (top/middle/bottom × left/center/right); 422 otherwise.
      - fontScale   : font height as a fraction of image height, clamped to [0.04, 0.22].
      - color       : "#RRGGBB" solid fill, OR the gradient TOP color; 422 on a bad hex.
      - strokeColor : "#RRGGBB" outline. Omitted/empty -> auto (black on a light fill,
                      white on a dark fill by Rec.601 luminance).
      - gradient    : when true, fill the text with a vertical gradient color -> color2
                      (color2 REQUIRED, 422 if missing). The outline stays solid.
      - autoStyle   : when true, analyze the base image under the text and auto-pick
                      fill/stroke/gradient, OVERRIDING color/strokeColor/gradient/color2.

    Returns { path, url, seed, styleIndex, style } for a NEW composited file written
    into the per-page covers CACHE dir. `style` = { color, color2, strokeColor,
    gradient } is the styling ACTUALLY used (auto picks echoed back). The base is
    NEVER overwritten — each call composites onto a fresh copy of the clean base, so
    re-applying never stacks text. The output filename is STABLE per (basePath + all
    resolved style params) via a short hash. 403 on a path outside the media root."""
    from PIL import Image, ImageDraw, ImageFont  # local import (Pillow already a dep)

    # --- validate ---
    base = _covers_tree_guard(req.basePath)  # 403 on traversal / outside root
    if not os.path.isfile(base):
        raise HTTPException(404, "Không tìm thấy ảnh nền")
    position = (req.position or "").strip().lower()
    if position not in _COVER_TEXT_ANCHORS:
        raise HTTPException(422, f"Vị trí không hợp lệ: {req.position!r}")
    rgb = _parse_hex_color(req.color)  # 422 on bad hex (also validates the gradient top)
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(422, "Thiếu nội dung chữ")
    font_scale = max(_COVER_TEXT_FONTSCALE_MIN,
                     min(_COVER_TEXT_FONTSCALE_MAX, float(req.fontScale or 0)))

    # --- open the clean base (NEVER mutate it — composite on a copy) ---
    try:
        src = Image.open(base)
        src.load()
    except Exception as exc:
        raise HTTPException(422, f"Ảnh nền không đọc được: {exc}")
    ext = os.path.splitext(base)[1].lower()
    is_jpg = ext in (".jpg", ".jpeg")
    img = src.convert("RGB") if is_jpg else src.convert("RGBA")
    W, H = img.size
    draw = ImageDraw.Draw(img)

    # --- font (Vietnamese-capable; report a fallback) ---
    font_px = max(1, round(font_scale * H))
    font_path, is_fallback = _cover_text_font_path()
    try:
        font = ImageFont.truetype(font_path, font_px)
    except Exception:
        # Last-resort fallback so the endpoint never hard-fails on a font issue.
        font = ImageFont.truetype("DejaVuSans.ttf", font_px)
        font_path, is_fallback = "DejaVuSans.ttf", True

    # --- wrap to ~90% width, measure the block ---
    max_text_w = 0.90 * W
    lines = _wrap_text_to_width(text, font, max_text_w, draw)
    ascent, descent = font.getmetrics()
    line_gap = round(font_px * 0.15)
    line_h = ascent + descent
    block_h = line_h * len(lines) + line_gap * (len(lines) - 1)
    line_widths = [float(draw.textlength(ln, font=font)) for ln in lines]

    # --- anchor the block (margin = 4% of the smaller dimension) ---
    vbucket, hbucket = _COVER_TEXT_ANCHORS[position]
    margin = round(0.04 * min(W, H))
    stroke_w = max(2, round(font_px * 0.06))

    if vbucket == "top":
        y0 = margin
    elif vbucket == "bottom":
        y0 = H - margin - block_h
    else:  # middle
        y0 = (H - block_h) / 2
    # Keep the whole block inside the image vertically.
    y0 = max(margin, min(y0, H - margin - block_h)) if block_h <= H - 2 * margin else 0

    # --- lay out every line (x per column), collect the block bbox ---
    layout: list[tuple[str, float, float, float]] = []  # (line, x, y, width)
    y = y0
    for ln, lw in zip(lines, line_widths):
        if hbucket == "left":
            x = margin
        elif hbucket == "right":
            x = W - margin - lw
        else:  # center
            x = (W - lw) / 2
        x = max(margin, min(x, W - margin - lw)) if lw <= W - 2 * margin else (W - lw) / 2
        layout.append((ln, x, y, lw))
        y += line_h + line_gap

    x_min = min(p[1] for p in layout)
    x_max = max(p[1] + p[3] for p in layout)
    y_top = layout[0][2]
    y_bot = layout[-1][2] + line_h
    pad = round(0.02 * min(W, H)) + stroke_w
    region_bbox = (max(0, int(x_min - pad)), max(0, int(y_top - pad)),
                   min(W, int(x_max + pad)), min(H, int(y_bot + pad)))

    # --- resolve the styling actually used ---
    if req.autoStyle:
        # Auto picks OVERRIDE all manual color inputs (deterministic per base+bbox).
        st = _auto_style_for_region(img, region_bbox)
        fill_rgb = _parse_hex_color(st["color"])
        gradient = bool(st["gradient"])
        color2_rgb = _parse_hex_color(st["color2"]) if gradient else None
        stroke_rgb = _parse_hex_color(st["strokeColor"])
    else:
        fill_rgb = rgb
        gradient = bool(req.gradient)
        if gradient:
            if not (req.color2 or "").strip():
                raise HTTPException(422, "Thiếu color2 cho chế độ gradient")
            color2_rgb = _parse_hex_color(req.color2)  # 422 on bad hex
        else:
            color2_rgb = None
        sc = (req.strokeColor or "").strip()
        if sc:
            stroke_rgb = _parse_hex_color(sc)  # 422 on bad hex
        else:
            # Auto outline: light fill -> black outline, dark fill -> white outline.
            stroke_rgb = (0, 0, 0) if _relative_luminance(fill_rgb) > 0.5 else (255, 255, 255)

    # --- render ---
    if gradient:
        # 1) Solid stroke ring: draw the DILATED glyph entirely in the stroke color.
        for ln, x, ly, _lw in layout:
            draw.text((x, ly), ln, font=font, fill=stroke_rgb,
                      stroke_width=stroke_w, stroke_fill=stroke_rgb)
        # 2) Glyph-only mask (no stroke) to paste the gradient through the interior.
        mask = Image.new("L", (W, H), 0)
        mdraw = ImageDraw.Draw(mask)
        for ln, x, ly, _lw in layout:
            mdraw.text((x, ly), ln, font=font, fill=255)
        # 3) Vertical gradient spanning the text block; paste it over the interior,
        #    leaving the wider stroke ring showing around the glyphs.
        grad_img = _vertical_gradient((W, H), fill_rgb, color2_rgb, y_top, y_bot)
        img.paste(grad_img, (0, 0), mask)
    else:
        for ln, x, ly, _lw in layout:
            draw.text((x, ly), ln, font=font, fill=fill_rgb,
                      stroke_width=stroke_w, stroke_fill=stroke_rgb)

    # --- save a NEW file (stable per base + resolved style) in the covers CACHE dir ---
    covers_dir = _covers_dir(req.page)
    os.makedirs(covers_dir, exist_ok=True)
    key = "|".join([
        os.path.realpath(base), text, position, f"{font_scale:.4f}",
        _hex(fill_rgb), _hex(color2_rgb) if color2_rgb else "-",
        _hex(stroke_rgb), "g" if gradient else "s",
    ])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    base_stem = os.path.splitext(os.path.basename(base))[0]
    out_ext = ".jpg" if is_jpg else ".png"
    dest = os.path.join(covers_dir, f"{base_stem}_txt_{digest}{out_ext}")
    if is_jpg:
        img.convert("RGB").save(dest, format="JPEG", quality=92)
    else:
        img.save(dest, format="PNG")

    if is_fallback:
        print(f"[cover-text] Be Vietnam Pro not found at {CAPTION_FONT!r} — "
              f"fell back to DejaVuSans (Vietnamese glyphs still covered)")

    seed, style_index = _infer_cover_seed_style(os.path.basename(base))
    return {
        "path": dest,
        "url": f"/media?path={urllib.parse.quote(dest)}",
        "seed": seed,
        "styleIndex": style_index,
        "style": {
            "color": _hex(fill_rgb),
            "color2": _hex(color2_rgb) if color2_rgb else None,
            "strokeColor": _hex(stroke_rgb),
            "gradient": gradient,
        },
    }


def _basepath_tilt_seed(base_realpath: str) -> int:
    """Stable tilt seed derived from a clean base's realpath, so the auto-bake and every
    re-apply of the SAME base make the SAME flat/tilt decision. Shared by _run_cover_task
    and POST /generate/cover/title so `initial cover == renderCoverTitle(viTitle, keyWords)`."""
    return int(hashlib.sha256((base_realpath or "").encode("utf-8")).hexdigest()[:8], 16)


def _fold_vi(s: str) -> str:
    """Diacritic-fold + lowercase + keep alnum only (đ->d), for case/diacritic-insensitive
    word matching. Mirrors the caption romanization helper (unicodedata, stdlib)."""
    import unicodedata as _ud
    s = (s or "").replace("đ", "d").replace("Đ", "D")
    n = _ud.normalize("NFKD", s)
    n = "".join(c for c in n if not _ud.combining(c))
    return "".join(c for c in n.lower() if c.isalnum())


def _title_segments_from_keywords(text: str, key_words: list | None) -> list:
    """Build ordered {text, key} segments from an owner-edited title `text`, marking the
    words that match any `key_words` entry (case/diacritic-INSENSITIVE, CONTIGUOUS phrase
    match) as key=True and the rest normal. Segment `text` values are the ORIGINAL tokens
    of `text` so they reconstruct it EXACTLY (drives _title_words_with_colors correctly).
    NO fallback: when NO keyWord matches, ALL words stay normal (key=False) and the whole
    title renders WITHOUT any background plate. Returns [{text,key}]."""
    words = (text or "").split()
    if not words:
        return []
    folded = [_fold_vi(w) for w in words]
    key_flags = [False] * len(words)
    for kw in (key_words or []):
        kw_tokens = [tok for tok in (_fold_vi(t) for t in str(kw).split()) if tok]
        if not kw_tokens:
            continue
        n = len(kw_tokens)
        # Mark EVERY contiguous run in `words` whose folded tokens equal this keyword.
        for p in range(0, len(words) - n + 1):
            if folded[p:p + n] == kw_tokens:
                for k in range(p, p + n):
                    key_flags[k] = True
    # NO fallback heuristic: if nothing matched, key_flags stays all-False -> the merging
    # loop below yields ONE key=False segment (no background anywhere). Owner-edited titles
    # get a background ONLY via explicit quotes (_title_segments_from_quotes) or a real
    # keyWord match.
    # Merge consecutive same-flag words into runs -> one segment (one plate) each.
    segments: list[dict] = []
    cur, cur_key = [words[0]], key_flags[0]
    for w, kf in zip(words[1:], key_flags[1:]):
        if kf == cur_key:
            cur.append(w)
        else:
            segments.append({"text": " ".join(cur), "key": cur_key})
            cur, cur_key = [w], kf
    segments.append({"text": " ".join(cur), "key": cur_key})
    return segments


# Quote characters that mark a KEY span in the title: straight + smart, double + single.
_QUOTE_DOUBLE = ('"', "“", "”")   # "  “  ”
_QUOTE_SINGLE = ("'", "‘", "’")   # '  ‘  ’


def _title_segments_from_quotes(text: str):
    """Parse QUOTED spans in `text` (straight OR smart, single OR double) as KEY clusters.

    Returns (display_text, segments):
      - display_text = `text` with ALL quote delimiter chars STRIPPED (never shown on the
        image), newlines preserved.
      - segments = ordered [{text,key}] where each BALANCED quoted span is a key=True cluster
        and everything else is normal; the segment `text` values are the DE-QUOTED words so
        they reconstruct display_text exactly.
    If there is NO balanced quoted span, returns (display_text, None) so the caller falls
    back to keyWords matching — lone/unbalanced quotes are still stripped (no stray glyph)
    but define no key. Pairs are matched per family (double vs single), consecutively; a
    trailing odd quote is a lone char (stripped, no span). Never raises."""
    s = text or ""
    dbl, sgl = set(_QUOTE_DOUBLE), set(_QUOTE_SINGLE)
    pos_d = [i for i, c in enumerate(s) if c in dbl]
    pos_s = [i for i, c in enumerate(s) if c in sgl]
    quote_pos = set(pos_d) | set(pos_s)
    # Mark chars strictly BETWEEN each balanced pair as key content.
    key_char = [False] * len(s)
    for poss in (pos_d, pos_s):
        for k in range(0, len(poss) - 1, 2):
            for j in range(poss[k] + 1, poss[k + 1]):
                key_char[j] = True
    # De-quoted display text (drop EVERY quote char) + aligned per-char key flags.
    out, kflags = [], []
    for i, ch in enumerate(s):
        if i in quote_pos:
            continue
        out.append(ch)
        kflags.append(key_char[i])
    dequoted = "".join(out)
    if not any(kflags):
        return dequoted, None  # no balanced quoted span -> caller uses keyWords
    # Group into word spans (non-space runs); a word is key if it overlaps any key char.
    words, wkey = [], []
    i, n = 0, len(dequoted)
    while i < n:
        if dequoted[i].isspace():
            i += 1
            continue
        j = i
        while j < n and not dequoted[j].isspace():
            j += 1
        words.append(dequoted[i:j])
        wkey.append(any(kflags[i:j]))
        i = j
    if not words:
        return dequoted, None
    # Merge consecutive same-flag words into segments (space-joined de-quoted words).
    segments: list[dict] = []
    cur, cur_key = [words[0]], wkey[0]
    for w, kf in zip(words[1:], wkey[1:]):
        if kf == cur_key:
            cur.append(w)
        else:
            segments.append({"text": " ".join(cur), "key": cur_key})
            cur, cur_key = [w], kf
    segments.append({"text": " ".join(cur), "key": cur_key})
    return dequoted, segments


def _title_segments_for_render(text: str, key_words: list | None):
    """Shared title segment-building: a QUOTED span in `text` is the ONLY way an
    owner-edited title gets a background plate. `key_words` is accepted for API
    compatibility but IGNORED here — auto-matching it against freshly-typed/re-applied
    text kept showing a background on words the owner never marked (e.g. a leftover
    keyWord from the original auto-generated title still matching after the owner
    edited the text). With no quoted span, the WHOLE title renders normal (no
    background anywhere). Returns (display_text, segments) where display_text is
    `text` with quote delimiters stripped. Used by the /generate/cover/title path."""
    dequoted, segments = _title_segments_from_quotes(text)
    if segments is not None:
        return dequoted, segments          # quotes win
    return dequoted, _title_segments_from_keywords(dequoted, None)


@router.post("/generate/cover/title")
def generate_cover_title(req: CoverTitleRequest):
    """Re-render the FANCY multi-plate Vietnamese title with owner-EDITED text on a CLEAN
    (title-less) SDXL base — pure Pillow, SYNCHRONOUS (NO Claude call), so it's fast.

    Body: { page, basePath, text, keyWords?, seed?, position?, keyColor?, gradient?,
            fontScale?, tiltDeg? }. Every STYLE field defaults to AUTO/current behavior; a
    SET field OVERRIDES that facet while anything left auto still re-rolls off `seed`.
      - basePath : abs path to the CLEAN title-less cover (guarded to the media root).
      - text     : the edited Vietnamese title (baked via the SAME fancy overlay as AUTO).
                   EXPLICIT '\n' are honored as HARD line breaks — each typed line renders
                   as its own plate/row (a single line still auto-wraps if too wide).
                   QUOTED spans ('...' / "..." / smart quotes) become KEY (background-plate)
                   clusters and the quote chars are STRIPPED (never shown); when any quoted
                   span is present it OVERRIDES keyWords.
      - keyWords : words to render on the KEY plate (case/diacritic-insensitive, contiguous)
                   — used ONLY when `text` has no quoted span. Null/empty (or no match) ->
                   the single longest content word becomes key.
      - seed     : optional variation seed; else derived from basePath so re-applies are stable.
      - position : "auto" (default) OR one of the 9 anchors (top/center/bottom × left/center/
                   right, plus "center"). Non-auto pins the block at that anchor; 422 otherwise.
      - keyColor : "#RRGGBB" KEY plate base / gradient TOP stop; null = auto dominant(+seeded
                   jitter). 422 on bad hex. Contrast guard still applies.
      - keyColor2: "#RRGGBB" gradient BOTTOM stop; only used with keyColor set AND gradient
                   true (else ignored). null = auto-derived darker/lighter 2nd stop. 422 on
                   bad hex. The seeded gradient ANGLE still varies; text color/contrast are
                   picked against the AVERAGE of the two stops.
      - gradient : bool (default true). true = gradient plate; false = solid fill (keyColor).
      - strokeColor: "#RRGGBB" text BORDER (outline) color applied to EVERY plate; null = auto
                   contrast pick. When set, the auto contrast guard is BYPASSED so the exact
                   color is honored (the glow halo keeps its auto color). 422 on bad hex.
      - align    : "auto" (default, centered in the column + seeded jitter) | "left" | "center"
                   | "right". left/right flush every row to that column edge and drop the
                   jitter; it also aligns the lines INSIDE a multi-line row. 422 otherwise.
      - fontScale: title block HEIGHT fraction, clamped to [0.2,1.5]; null = auto fill.
      - tiltDeg  : exact tilt (deg) for ALL plates, clamped to [-20,20]; null = seeded minority.

    CONSISTENCY: to reproduce the freshly-generated cover EXACTLY, send the prefilled viTitle
    + result.keyWords and OMIT seed AND all style overrides (both the auto-bake and this
    endpoint then take the SAME basePath-seeded auto path -> byte-identical).

    Composites onto a FRESH copy of the clean base EVERY call (so re-applying never stacks),
    saves a NEW file in the per-page covers CACHE dir, and returns { path, url }.
    403 outside the media root, 404 if the base is missing, 422 on empty text/bad hex/bad position."""
    from PIL import Image  # local import (Pillow already a dep)

    base = _covers_tree_guard(req.basePath)  # 403 on traversal / outside root
    if not os.path.isfile(base):
        raise HTTPException(404, "Không tìm thấy ảnh nền")
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(422, "Thiếu tiêu đề")

    # --- validate + normalize the manual overrides (auto/null -> unset) ---
    pos_in = (req.position or "auto").strip().lower()
    if pos_in in ("", "auto"):
        pos_override = None
    elif pos_in in _COVER_TITLE_ANCHORS:
        pos_override = pos_in
    else:
        raise HTTPException(422, f"Vị trí không hợp lệ: {req.position!r}")
    key_color_rgb = _parse_hex_color(req.keyColor) if (req.keyColor or "").strip() else None    # 422 on bad hex
    key_color2_rgb = _parse_hex_color(req.keyColor2) if (req.keyColor2 or "").strip() else None  # 422 on bad hex
    stroke_rgb = _parse_hex_color(req.strokeColor) if (req.strokeColor or "").strip() else None  # 422 on bad hex
    align_in = (req.align or "auto").strip().lower()
    if align_in in ("", "auto"):
        align_override = None
    elif align_in in ("left", "center", "right"):
        align_override = align_in
    else:
        raise HTTPException(422, f"Căn chữ không hợp lệ: {req.align!r}")
    gradient_on = bool(req.gradient)
    # Upper clamp 1.5 (owner request 2026-07-29, was 0.8): >1.0 means "taller than the
    # frame" as a TARGET — the width/word-fit constraints of the font search then become
    # the effective cap, so the slider top = as big as physically fits. Safe by construction.
    font_scale = None if req.fontScale is None else max(0.2, min(1.5, float(req.fontScale)))  # clamp
    tilt_deg = None if req.tiltDeg is None else max(-20.0, min(20.0, float(req.tiltDeg)))      # clamp
    overrides = {
        "position": pos_override, "key_color": key_color_rgb, "key_color2": key_color2_rgb,
        "gradient": gradient_on, "font_scale": font_scale, "tilt_deg": tilt_deg,
        "stroke_color": stroke_rgb, "align": align_override,
    }

    try:
        src = Image.open(base)
        src.load()  # force-decode now so a corrupt base 422s here, not mid-composite
    except Exception as exc:
        raise HTTPException(422, f"Ảnh nền không đọc được: {exc}")
    img = src.convert("RGB")  # a FRESH RGB copy — the clean base file is never mutated

    # Quoted spans in `text` DEFINE the key clusters and OVERRIDE keyWords; quote delimiters
    # are stripped from the rendered title. With no quotes -> keyWords matching (unchanged).
    render_text, title_segments = _title_segments_for_render(text, req.keyWords)
    # Stable seed: caller-supplied, else derived from basePath (so a given base always makes
    # the SAME auto decisions on every re-apply).
    if req.seed is not None:
        seed = int(req.seed)
    else:
        seed = _basepath_tilt_seed(os.path.realpath(base))

    out = _composite_title(img, render_text, title_segments, seed, overrides)

    # Save a NEW file (stable per base + text + keyWords + seed + all overrides) in the covers CACHE dir.
    covers_dir = _covers_dir(req.page)
    os.makedirs(covers_dir, exist_ok=True)
    kw_norm = ",".join(str(k) for k in (req.keyWords or []))
    ov_key = "|".join([
        pos_override or "auto", _hex(key_color_rgb) if key_color_rgb else "-",
        _hex(key_color2_rgb) if key_color2_rgb else "-",
        "g" if gradient_on else "s",
        f"{font_scale:.3f}" if font_scale is not None else "-",
        f"{tilt_deg:.1f}" if tilt_deg is not None else "-",
        # New facets MUST be in the key or a border/align change would return the CACHED
        # file from the previous settings (same base+text+seed) and look like a no-op.
        _hex(stroke_rgb) if stroke_rgb else "-",
        align_override or "auto",
    ])
    key = "|".join([os.path.realpath(base), text, kw_norm, str(seed), ov_key])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    base_stem = os.path.splitext(os.path.basename(base))[0]
    dest = os.path.join(covers_dir, f"{base_stem}_title_{digest}.jpg")
    # quality=90 matches the auto-bake in _run_cover_task, so reapplying the prefilled
    # viTitle (unchanged) yields a cover byte-identical to the freshly-generated one.
    out.save(dest, format="JPEG", quality=90)
    return {
        "path": dest,
        "url": f"/media?path={urllib.parse.quote(dest)}",
    }


def _fetch_source_thumbnail(page: str, link: str) -> dict:
    """Fetch the source video's thumbnail into the page's covers cache dir via the
    download_worker THUMBNAIL mode (cf-venv yt-dlp). Returns the worker dict
    {thumbPath, thumbUrl, videoId}. Raises HTTPException on failure."""
    ffmpeg_dir = os.path.dirname(FFMPEG_BIN) if os.path.isfile(FFMPEG_BIN) else None
    covers_dir = _covers_dir(page)
    os.makedirs(covers_dir, exist_ok=True)
    payload = {
        "mode": "thumbnail",
        "link": link.strip(),
        "outDir": covers_dir,
        "ffmpegLocation": ffmpeg_dir,
    }
    res = _run_cf_worker("download_worker.py", payload, timeout=300)
    thumb = res.get("thumbPath")
    if not thumb or not os.path.isfile(thumb):
        raise HTTPException(502, "Không tải được ảnh thu nhỏ từ nguồn")
    return res


@router.get("/generate/cover/created")
def list_created_covers(page: str):
    """List ALL generated covers in a page's covers CACHE dir (top-level only).

    Returns { covers: [{ path, url, filename, savedAt }] } NEWEST FIRST by mtime.
    Reads _covers_dir(page) = <root>/_cache/covers/<page>/ and includes every
    top-level .jpg/.jpeg/.png (the SDXL cover_*.jpg AND the _txt_* overlays — all
    are valid covers to reuse). Top-level only: the isfile() filter skips any
    subdirectory (e.g. a leftover saved/ dir). Empty or missing dir ->
    { covers: [] } (never an error)."""
    covers_dir = _covers_dir(page)
    if not os.path.isdir(covers_dir):
        return {"covers": []}
    exts = (".jpg", ".jpeg", ".png")
    entries: list[tuple[float, dict]] = []
    for name in os.listdir(covers_dir):
        fp = os.path.join(covers_dir, name)
        if not os.path.isfile(fp):  # skips any subdir (e.g. a leftover saved/)
            continue
        if os.path.splitext(name)[1].lower() not in exts:
            continue
        try:
            mtime = os.path.getmtime(fp)
        except OSError:
            mtime = 0.0
        entries.append((mtime, _cover_entry(page, os.path.realpath(fp))))
    entries.sort(key=lambda e: e[0], reverse=True)
    return {"covers": [e[1] for e in entries]}


@router.delete("/generate/cover/created")
def delete_created_cover(req: DeleteSavedCoverRequest):
    """Remove ONE cover from a page's covers CACHE dir. Body: { page, path } -> { ok: true }.

    403 (same path-traversal guard as /media) if `path` resolves outside the content
    output root, or is NOT inside the page's covers cache dir. Missing file is
    tolerated (still returns ok:true — the end state is 'gone'). 500 on an OSError
    while removing."""
    full = _covers_tree_guard(req.path)
    covers_dir = os.path.realpath(_covers_dir(req.page))
    # Must be inside this page's cache dir.
    if not full.startswith(covers_dir + os.sep):
        raise HTTPException(403, "Đường dẫn nằm ngoài thư mục cover của trang")
    try:
        if os.path.isfile(full):
            os.remove(full)
    except OSError as exc:
        raise HTTPException(500, f"failed to remove cover: {exc}")
    return {"ok": True}


# --- Voiceover (VieNeu-TTS) ---------------------------------------------
#
# Turn each scene's Vietnamese narration into a wav. VieNeu-TTS does NOT emit
# timestamps — run /generate/timestamps on the output to get per-line timing.


class TtsScene(BaseModel):
    scene: int
    narration: str


class TtsRequest(BaseModel):
    # Either pass scenes (from /generate/script) or a single text for a test.
    scenes: list[TtsScene] | None = None
    text: str | None = None
    voice: str | None = None          # preset voice name; None = engine default (F5-TTS prefers ref audio)
    refAudio: str | None = None        # path to a reference wav -> clone that voice
    engine: str | None = None          # "vieneu" | "f5-tts"; None = derive from clone name
    refText: str | None = None         # F5 only: transcript of refAudio (else auto via whisper)
    emotion: str = "natural"
    applyWatermark: bool = False       # VieNeu's Perth watermark — off by default
    temperature: float | None = None       # ↓ = steadier; None = VieNeu default (0.8)
    repetitionPenalty: float | None = None  # ↑ = less rambling/looping (default 1.2)
    maxNewFrames: int | None = None         # hard cap on generated length (default 300)
    page: str | None = None           # used to derive the default output dir
    outDir: str | None = None         # explicit override of the audio output dir
    bypassTtsCache: bool = False      # force-fresh: skip the per-scene cache READ (every
                                      # scene treated as a MISS) but STILL WRITE results
                                      # back so the cache warms. Reuse-script "fresh voice".


# Canonical pipeline sample rate (VieNeu v3turbo = 48 kHz; F5 is resampled up to
# this — see tts_worker.CANONICAL_SR). A cache HIT only stores the wav, so we report
# this rate (the worker would have emitted the same) and re-probe the duration.
_TTS_CANONICAL_SR = 48000


def _tts_scene_wav_name(scene) -> str:
    """The output filename tts_worker.py uses for a scene (must stay in lockstep
    with _run_vieneu / _run_f5): scene_NNN.wav for an int scene, else audio.wav."""
    return f"scene_{scene:03d}.wav" if isinstance(scene, int) else "audio.wav"


# TTS-cache voicing salt (fixes the "tts-cache-key omits worker text-prep" bug for ALL
# engines). The base cache key hashes only the RAW narration, but every engine's SPOKEN
# text is derived in the worker from word_improve.md (say_as / say_as_vieneu /
# say_as_omnivoice respelling) plus engine-specific text-prep logic — NONE of which is in
# the key. So editing a say_as column, or changing an engine's text-prep, would NOT
# invalidate a cached clip: the owner re-renders and still hears the OLD pronunciation.
# We fold a per-engine voicing salt into the key: a manual VERSION bump (text-prep code
# changes) + a hash of word_improve.md (any say_as edit). We hash the WHOLE file for
# simplicity, so an edit to one engine's column busts all three engines' caches — safe
# (a one-time re-synth), never serves a stale pronunciation.
#
# The VERSION constants are per-engine and bumped MANUALLY when that engine's worker
# text-prep changes (parity with the OmniVoice design — we deliberately do NOT try to
# auto-hash F5's many env knobs):
#   F5:       year normalization / separator-marker semantics / per-sentence architecture
#   VieNeu:   its say_as (say_as_vieneu) application path
#   OmniVoice:_normalize_text_neutral (year/number) + _apply_omnivoice_pron (say_as_omnivoice)
#
# v2 (all three): _expand_vn_dates added to the worker's text-prep — a slash date is now
# SPOKEN as "24 tháng 2". F5/VieNeu get it via _apply_pron_map, OmniVoice via
# _normalize_text_neutral, so every engine's cached audio for a line containing a date is
# stale and must be re-synthesized.
# v3 (OmniVoice ONLY): punctuation beats by construction (OMNIVOICE_PUNCT_BEAT). A scene with
# internal punctuation is now synthesized one clause per generate() and concatenated with a
# fixed silence at each join, so every cached OmniVoice wav produced before this (one
# continuous run, no pause at a comma) is stale. F5/VieNeu are untouched -> versions unchanged,
# their caches survive.
# v3 (F5 ONLY, 2026-08-02): _count_syllables corrected (Vietnamese magnitude-based number
# reading + English silent-e) — changes which loanword chunks trigger F5's best-of-N reroll
# (3/49 word_improve.md loanwords affected: clone/feature/website, all fewer spurious
# rerolls) and shifts the vi-corrections verifier's per-draw target on ~2.3% of matched-window
# tokens. Cache was empty at bump time (24h TTL, 0 cached entries) so this cost nothing; bumped
# anyway so no render started in the prior 24h window can serve audio picked by the old policy.
# v4 (all three, 2026-08-16): _decimal_sep_for now also picks 'chấm' over 'phẩy' whenever a
# magnitude quantifier (nghìn/ngàn/triệu/tỷ/tỉ) immediately follows the fraction — OmniVoice was
# measured (job 340, "1.6 nghìn tỷ tham số") to SWALLOW 'phẩy' whole in that position ("một, sáu
# nghìn tỷ", separator word dropped entirely; 'tỷ' alone was fine, 'nghìn' anywhere after the
# fraction broke it; 'chấm' survived every tested case). Applied engine-neutrally since
# _expand_decimal_point is shared and 'chấm' is already a validated decimal word for all three,
# so every cached line with a "<digit>.<digit> nghìn/triệu/tỷ/tỉ" pattern is stale.
# v5 (all three, 2026-08-17): v4's "immediately follows" check was too narrow — job 341
# ("...14.8 LÊN 32 nghìn tỷ token,") proved OmniVoice swallows 'phẩy' if a quantifier occurs
# ANYWHERE later in the same clause, not only right after the fraction ('lên' sits between
# '8' and 'nghìn' here, so v4 still picked 'phẩy' and it was still dropped — confirmed by a
# true ~100ms digital-silence gap in the waveform, not just a suspicious transcript).
# _decimal_sep_for's suffix check now scans the whole clause (to the next punctuation mark)
# for a quantifier. Every cached line with a quantifier anywhere in the same clause as a
# decimal is stale again, including ones re-synthesized under v4 (job 341's "14.8" line was
# cached under v4 and still had the bug).
# v6 (all three, 2026-08-18): v4/v5's "quantifier in clause" rule chased a false pattern —
# job 345 ("24.7 đô", no quantifier, no version-term prefix; "GBD 5.6", an unrecognized
# version term) proved 'phẩy' fails even with NEITHER trigger present. Re-synthesizing the
# exact same "24.7 đô" text 3x with no code change lost the separator on 2/3 draws and kept
# it on 1/3 — OmniVoice's diffusion sampling has no fixed seed, so this was never a
# deterministic text-pattern bug, just draw variance that v4/v5's small samples happened to
# correlate with a quantifier. 'chấm' had not failed once across ~10 varied trials by this
# point. VI_DECIMAL_SEP_WORD's default is now 'chấm' (was 'phẩy') — every cached line
# containing ANY decimal number is stale, not just quantifier-adjacent ones.
_F5_VOICING_VERSION = "6"
_VIENEU_VOICING_VERSION = "5"
# v4 (OmniVoice ONLY): per-clause pace balance (OMNIVOICE_UNIT_PACE_BALANCE). Each clause of a
# scene is retimed to the scene's syllable-weighted mean pace, so v3's independent per-clause
# draws no longer land at different speeds (video 280 scene 1: 191 vs 252 ms/syllable).
# v6 (OmniVoice ONLY): short clauses are merged into a neighbour before synthesis
# (OMNIVOICE_MIN_UNIT_SYL) — a 2-3 syllable clause synthesized alone was mostly model
# lead-in/tail and produced an 837 ms join plus a 436 ms/syllable read on video 287 scene 6.
# v7 (OmniVoice ONLY): OMNIVOICE_BEAT_MID_S 0.10 -> 0.08 (owner on video 295: the comma pause at
# ~6 s is still too long). The beat is baked INTO the cached scene wav at synthesis time and
# the cache key does NOT see the knob, so without this bump every cached OmniVoice wav would keep
# replaying the old 100 ms comma silence and the .env change would look like a no-op.
# v8 (OmniVoice ONLY): OMNIVOICE_BEAT_MID_S 0.08 -> 0.09, the owner's chosen value (-10% from the
# original 0.10 rather than v7's -20%). Same cache reasoning as v7: the v7 wavs carry an 80 ms
# beat, so they are stale for the same reason.
# v9 (OmniVoice ONLY, 2026-08-02): _spoken_syllables' fallback _count_syllables corrected
# (magnitude-based numbers + silent-e), same fix as F5 v3 above. Traced against job 308's full
# 61 scenes / 113 clause units: 0 OMNIVOICE_MIN_UNIT_SYL merge decisions changed, but 13/61
# scenes get different per-clause _balance_unit_pace weights (same clause grouping, different
# intra-scene atempo split). Cache was empty at bump time, so free; bumped for the same
# mixed-policy-window reason as v3.
# v10 (OmniVoice ONLY, 2026-08-16): _merge_short_units now flags a merged unit and
# run_omnivoice skips _compress_unit_interior on it, so the internal punctuation mark it
# swallowed keeps whatever pause the model gave it (job 338: "sửa lỗi, kiểm chứng" was losing
# its comma pause — interior-silence compression assumed no unit ever has internal punctuation,
# which stopped being true once short-clause merging was added). Also carries the v4 decimal/
# 'chấm' fix above (shared function, same bump would be needed either way).
# v11 (OmniVoice ONLY, 2026-08-17): carries the v5 clause-wide decimal/quantifier fix above.
# v12 (OmniVoice ONLY, 2026-08-18): carries the v6 chấm-default fix above.
_OMNIVOICE_NORM_VERSION = "12"
_VOICING_VERSIONS = {
    "f5-tts": _F5_VOICING_VERSION,
    "vieneu": _VIENEU_VOICING_VERSION,
    "omnivoice": _OMNIVOICE_NORM_VERSION,
}
_WORD_IMPROVE_MD = os.getenv(
    "WORD_IMPROVE_MD", os.path.join(os.path.dirname(__file__), "word_improve.md")
)


def _word_improve_hash() -> str:
    """sha256[:12] of word_improve.md — busts every engine's cache on any say_as edit.
    Best-effort: an unreadable file degrades to a constant (still keyed by VERSION)."""
    try:
        with open(_WORD_IMPROVE_MD, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return "nofile"


def _voicing_salt(engine: str) -> str:
    """Short signature of an engine's voicing recipe (per-engine VERSION + word_improve.md
    content), folded into that engine's cache key. Shared by f5-tts / vieneu / omnivoice —
    single source of truth. Returns '' for an engine with no registered version (leaves the
    key unsalted, i.e. legacy raw-narration behavior)."""
    eng = (engine or "").strip().lower()
    version = _VOICING_VERSIONS.get(eng)
    if version is None:
        return ""
    return f"\x00{eng}-v{version}-{_word_improve_hash()}"


def _tts_key_for_item(req: "TtsRequest", engine: str, text: str) -> str | None:
    """Compute the per-scene TTS cache key for one narration line, or None if the
    key cannot be derived (never raises — a None key just disables caching for it)."""
    # Fold the per-engine voicing salt into the hashed narration so a say_as edit or a
    # text-prep VERSION bump busts the cache (the base key sees only the raw narration).
    narration_for_key = text + _voicing_salt(engine)
    try:
        return tts_cache.tts_cache_key(
            narration=narration_for_key,
            engine=engine,
            voice=req.voice,
            ref_audio_path=req.refAudio,
            emotion=req.emotion,
            temperature=req.temperature,
            rep_penalty=req.repetitionPenalty,
        )
    except Exception as e:  # noqa: BLE001 — cache must never break the pipeline
        log.warning("[tts_cache] key computation failed: %s", e)
        return None


@router.post("/generate/tts")
def generate_tts(req: TtsRequest):
    if req.scenes:
        items = [{"scene": s.scene, "text": s.narration} for s in req.scenes]
    elif req.text:
        items = [{"scene": 1, "text": req.text}]
    else:
        raise HTTPException(422, "Provide either 'scenes' or 'text'.")

    engine = _normalize_engine(req.engine, req.refAudio)
    out_dir = req.outDir or _page_audio_dir(req.page)

    # --- Per-scene TTS cache ------------------------------------------------
    # Each scene is keyed by a content hash of (narration, engine, voice, emotion,
    # temperature, rep_penalty). Scenes already in the cache (HITs) are copied into
    # out_dir and never sent to the worker; only the MISSes go to the GPU. Watermark
    # and maxNewFrames are intentionally NOT in the key: the watermark is an inaudible
    # overlay and maxNewFrames is only a safety cap (the audible content is identical),
    # so they would needlessly fragment the cache without changing what is heard.
    # Every cache op is best-effort and wrapped so a cache fault can never fail synth.
    keys: dict[int, str | None] = {}     # item index -> cache key (or None = uncacheable)
    hits: dict[int, str] = {}            # item index -> cached wav path
    # Per-request force-fresh: when bypassTtsCache is set we SKIP the cache READ
    # entirely (every scene is a forced MISS → re-synthesized), but we STILL compute
    # `keys` below so the post-synth store_tts WRITE keeps warming the cache. This is
    # the reuse-script "fresh voice" path. The global TTS_CACHE env still governs the
    # write (off => no write); this flag only suppresses the read.
    if req.bypassTtsCache:
        log.info("[tts_cache] BYPASS read (bypass_tts_cache) — %d scenes forced to synth", len(items))
    for idx, it in enumerate(items):
        key = _tts_key_for_item(req, engine, it["text"])
        keys[idx] = key
        if key is None:
            continue
        if req.bypassTtsCache:
            # Force-fresh: evict any stale entry for this key BEFORE the scene
            # falls through to MISS → worker synth → store_tts re-populates it
            # clean. Double-guarded (the cache layer is already best-effort) so
            # a delete fault can never break synth.
            try:
                tts_cache.delete_tts(key)
            except Exception as e:  # noqa: BLE001
                log.warning("[tts_cache] delete failed scene %s: %s", it.get("scene"), e)
            continue
        try:
            cached = tts_cache.find_cached_tts(key)
        except Exception as e:  # noqa: BLE001
            log.warning("[tts_cache] lookup failed scene %s: %s", it.get("scene"), e)
            cached = None
        if cached:
            log.info("[tts_cache] HIT %s scene %s", key[:8], it.get("scene"))
            hits[idx] = cached
        else:
            log.info("[tts_cache] MISS %s scene %s", key[:8], it.get("scene"))

    misses = [it for idx, it in enumerate(items) if idx not in hits]

    def _result_from_hit(it: dict, cached_path: str) -> dict:
        """Materialize a HIT into the same shape the worker returns: copy the cached
        wav into out_dir under the worker's naming, probe its duration, report the
        canonical sample rate. Falls back to None if the copy fails (caller re-synths)."""
        scene = it.get("scene")
        dest = os.path.join(out_dir, _tts_scene_wav_name(scene))
        try:
            os.makedirs(out_dir, exist_ok=True)
            if os.path.realpath(cached_path) != os.path.realpath(dest):
                tmp = dest + ".part"
                shutil.copyfile(cached_path, tmp)
                os.replace(tmp, dest)
        except OSError as e:
            log.warning("[tts_cache] hit copy failed scene %s: %s", scene, e)
            return None
        return {
            "scene": scene,
            "text": it["text"],
            "audioPath": dest,
            "sampleRate": _TTS_CANONICAL_SR,
            "durationS": _probe_duration(dest),
        }

    # Demote any HIT whose copy fails back to a MISS so the worker re-synthesizes it.
    hit_results: dict[int, dict] = {}
    for idx, cached_path in list(hits.items()):
        r = _result_from_hit(items[idx], cached_path)
        if r is None:
            misses.append(items[idx])
        else:
            hit_results[idx] = r

    # ALL scenes served from cache: skip the worker (and its model load) entirely.
    if not misses:
        results = [hit_results[idx] for idx in sorted(hit_results)]
        return {"count": len(results), "results": results}

    payload = {
        "items": [{"scene": it.get("scene"), "text": it["text"]} for it in misses],
        "engine": engine,
        "voice": req.voice,
        "refAudio": req.refAudio,
        "refText": req.refText,
        "emotion": req.emotion,
        "applyWatermark": req.applyWatermark,
        "temperature": req.temperature,
        "repetitionPenalty": req.repetitionPenalty,
        "maxNewFrames": req.maxNewFrames,
        "outDir": out_dir,
    }
    # TTS is sequential per scene, so scale the timeout with scene count (plus a
    # floor that covers the one-time cold model load/download on first run). F5 and
    # OmniVoice are heavier (per-scene GPU inference + resample), so give them a
    # larger per-item budget than the CPU/ONNX VieNeu.
    _gpu_engine = engine in ("f5-tts", "omnivoice")
    per_item = 90 if _gpu_engine else 35
    timeout = max(900, per_item * len(misses))
    # F5's and OmniVoice's CUDA init intermittently flakes ("Error code 127" /
    # WinError 6714) and clears on a fresh process — retry it. VieNeu is CPU/ONNX
    # (no such flake), so no retry there.
    retries = 2 if _gpu_engine else 0
    # Reclaim ComfyUI's pinned VRAM before loading a GPU TTS model. Placed HERE (not in
    # the runner) so it fires exactly when a real model load is about to happen: an
    # all-cache-HIT job returned above without ever reaching this line, so a cached run
    # never pays ComfyUI a needless re-load. CPU/ONNX VieNeu doesn't touch the card, so
    # it is left alone. See _free_comfy_vram for the job-286 evidence.
    if _gpu_engine:
        _free_comfy_vram(f"tts {engine}")
    worker_out = _run_cf_worker("tts_worker.py", payload, timeout=timeout, retries=retries)

    # Store each freshly-synthesized scene in the cache (best-effort), then merge the
    # worker results back with the cached hits in the ORIGINAL scene order. We map a
    # worker result back to its source item by scene number; the worker preserves it.
    worker_by_scene: dict = {}
    for r in (worker_out.get("results") or []):
        worker_by_scene[r.get("scene")] = r

    miss_idx_by_scene: dict = {}
    for idx, it in enumerate(items):
        if idx in hit_results:
            continue
        miss_idx_by_scene.setdefault(it.get("scene"), idx)

    miss_results: dict[int, dict] = {}
    for scene, r in worker_by_scene.items():
        idx = miss_idx_by_scene.get(scene)
        if idx is None:
            continue
        miss_results[idx] = r
        key = keys.get(idx)
        wav_path = r.get("audioPath")
        if key and wav_path:
            try:
                tts_cache.store_tts(key, wav_path)
            except Exception as e:  # noqa: BLE001
                log.warning("[tts_cache] store failed scene %s: %s", scene, e)

    # Reassemble full result in original item order (hits + freshly-synth'd misses).
    merged = {**hit_results, **miss_results}
    results = [merged[idx] for idx in sorted(merged)]
    return {"count": len(results), "results": results}


# --- Timestamps (faster-whisper) ----------------------------------------
#
# Transcribe the generated audio to per-line (and per-word) timestamps that
# drive scene length and caption sync.


class TimestampItem(BaseModel):
    scene: int | None = None
    audioPath: str


class TimestampsRequest(BaseModel):
    # Either a batch (e.g. the results of /generate/tts) or one audio file.
    items: list[TimestampItem] | None = None
    audioPath: str | None = None
    model: str | None = None
    language: str | None = "vi"       # None = autodetect
    wordTimestamps: bool = True


@router.post("/generate/timestamps")
def generate_timestamps(req: TimestampsRequest):
    if req.items:
        items = [{"scene": i.scene, "audioPath": i.audioPath} for i in req.items]
    elif req.audioPath:
        items = [{"scene": 1, "audioPath": req.audioPath}]
    else:
        raise HTTPException(422, "Provide either 'items' or 'audioPath'.")

    payload = {
        "items": items,
        "model": req.model or WHISPER_MODEL,
        "device": WHISPER_DEVICE,
        "compute": WHISPER_COMPUTE,
        "language": req.language,
        "wordTimestamps": req.wordTimestamps,
    }
    result = _run_cf_worker("whisper_worker.py", payload, timeout=1200)
    return result


# --- Ingest (yt-dlp + faster-whisper) -----------------------------------
#
# Stage 0 for translate/reup pages: download a source video's audio and
# transcribe it. The resulting transcript is what the script-gen stage rewrites
# into a Commentary/Recap/Educational script. Source language is autodetected
# (it is usually NOT Vietnamese), and we keep only a small 16 kHz mono wav.


# Cap the ingested length of a source (seconds). Hours-long sources can't be
# fully transcribed on CPU, so a cap keeps ingest tractable. 0 = no cap (full).
INGEST_MAX_SEC = int(os.getenv("INGEST_MAX_SEC", "0"))


class IngestRequest(BaseModel):
    link: str
    page: str | None = None
    model: str | None = None
    language: str | None = None        # None = autodetect the source language
    sampleRate: int = 16000
    clipSec: int | None = None         # only ingest the first N seconds; None = env default
    outDir: str | None = None
    localMedia: str | None = None      # footage de-dup: extract audio from this already-
                                       # downloaded source video instead of re-pulling it


def _page_source_dir(page: str | None) -> str:
    """Where ingested source audio lands: <CONTENT_OUTPUT_ROOT>/<page>/source."""
    return os.path.join(CONTENT_OUTPUT_ROOT, page or "default", "source")


class ProbeRequest(BaseModel):
    link: str


class TagsRequest(_LlmChoiceMixin):
    title: str
    editMode: str | None = None
    page: str | None = None


@router.post("/generate/tags")
def generate_tags(req: TagsRequest):
    """Generate copy-ready Facebook hashtags for a video's title/topic.

    Returns { "tags": [str, ...], "text": "#a\\n#b\\n..." }:
      - tags: at most 8, each starts with exactly one '#', no spaces/punctuation
        inside, deduped, non-empty; Vietnamese tokens are accent-stripped; the
        page brand tag is always present (first) when a page name is supplied.
      - text: the tags joined by NEWLINES (one hashtag per line) — Facebook treats a
        space-joined block as a single tag, so the paste-ready block is newline-split.
    NEVER raises — on any generation failure a title-derived fallback set is used
    (see _generate_fb_tags), so the FE always receives a usable result.
    """
    # _generate_fb_tags already normalizes/dedupes, forces the brand tag, and caps at 8.
    tags = _generate_fb_tags(req.title, req.editMode, req.page,
                             llm_provider=req.llmProvider, llm_model=req.llmModel)
    return {"tags": tags, "text": "\n".join(tags)}


@router.post("/generate/probe_link")
def probe_link(req: ProbeRequest):
    """Lightweight metadata for the Studio paste-link preview (no download)."""
    if not req.link.strip():
        raise HTTPException(422, "Provide a 'link'.")
    return _run_cf_worker("probe_worker.py", {"link": req.link.strip()}, timeout=90)


@router.post("/generate/ingest")
def generate_ingest(req: IngestRequest):
    if not req.link.strip():
        raise HTTPException(422, "Provide a source 'link'.")
    link = req.link.strip()

    # --- Cross-job REUSE (TASK 3): the transcript ("bóc lời") — text + segment/word
    #     timestamps the pipeline uses downstream as the source "script" — is cached
    #     under CONTENT_OUTPUT_ROOT/_cache/transcripts/<id>.json keyed by a STABLE id
    #     from the link. Cache HIT -> SKIP whisper (and the audio download/extract)
    #     entirely and return the loaded dict, which has the EXACT same shape the
    #     ingest worker emits (transparent to the runner). MISS -> transcribe, then
    #     store. INGEST_CACHE=0 disables reuse; a missing/empty/unparseable/wrong-
    #     shape file is treated as a MISS and re-transcribed. The cached transcript
    #     corresponds to the configured ingest window (clipSec); reuse is correct as
    #     long as that window is unchanged (it is, for a given INGEST_MAX_SEC).
    sid = cache_util.source_id(link)
    if cache_util.cache_reads_enabled():
        cached = cache_util.load_transcript(sid)
        if cached is not None:
            print(f"[cache] transcript HIT {sid} -> {cache_util.transcript_path(sid)}")
            return cached
        print(f"[cache] transcript MISS {sid} — transcribing")
    else:
        print(f"[cache] transcript bypass (INGEST_CACHE off) {sid} — transcribing")

    # yt-dlp's audio extraction needs ffmpeg; FFMPEG_BIN isn't on PATH, so hand
    # yt-dlp the directory it lives in.
    ffmpeg_dir = os.path.dirname(FFMPEG_BIN) if os.path.isfile(FFMPEG_BIN) else None
    clip_sec = req.clipSec if req.clipSec is not None else (INGEST_MAX_SEC or None)
    payload = {
        "link": link,
        "outDir": req.outDir or _page_source_dir(req.page),
        "model": req.model or WHISPER_MODEL,
        "device": WHISPER_DEVICE,
        "compute": WHISPER_COMPUTE,
        "language": req.language,
        "sampleRate": req.sampleRate,
        "clipSec": clip_sec,
        "ffmpegLocation": ffmpeg_dir,
        "localMedia": req.localMedia,
    }
    # Download + full-length transcription on CPU can run long for a 10–20 min
    # source; give it a generous ceiling.
    res = _run_cf_worker("ingest_worker.py", payload, timeout=3600)
    # Cache the transcript for the next job on this URL (best-effort; never fails).
    stored = cache_util.store_transcript(sid, res)
    if stored:
        print(f"[cache] transcript STORED {sid} -> {stored}")
    return res


# --- Voices: list / clone-upload / preview / media serving --------------


@router.get("/generate/voices")
def list_voices(page: str | None = None):
    """Built-in VieNeu preset voices + every SHARED cloned reference voice.

    Clones are shared across all pages, so the returned clone list is identical
    regardless of `page` (the param is kept for API compatibility / future
    page-scoped filtering, but no longer scopes clones)."""
    presets = []
    try:
        with open(VIENEU_VOICES_JSON, encoding="utf-8") as f:
            data = json.load(f)
        default = data.get("default_voice")
        for name, v in data.get("presets", {}).items():
            desc = v.get("description", "") if isinstance(v, dict) else ""
            presets.append({"name": name, "description": desc or "", "isDefault": name == default})
    except (OSError, json.JSONDecodeError):
        pass  # presets are optional; cloning still works

    cloned = []
    vdir = _shared_voice_dir()
    if os.path.isdir(vdir):
        for fn in sorted(os.listdir(vdir)):
            full = os.path.join(vdir, fn)
            if os.path.isfile(full) and fn.lower().endswith(_AUDIO_EXTS):
                cloned.append({"name": os.path.splitext(fn)[0], "path": full})

    return {"presets": presets, "cloned": cloned}


@router.post("/generate/voice")
def upload_voice(
    background: BackgroundTasks,
    page: str = Form(...),
    name: str = Form(...),
    model: str = Form("vieneu"),
    file: UploadFile = File(...),
):
    """Save a reference voice (auto-trimmed + normalized), then warm its preview.

    The chosen clone-engine's short name is baked into the saved voice name as a
    suffix (e.g. "My Voice - F5-TTS") so the engine is visible without renaming
    any existing (suffix-less) clone files on disk.

    Clones are SHARED across all pages, so the upload lands in SHARED_VOICE_DIR
    regardless of which page initiated it; every page will then see this clone.
    """
    vdir = _shared_voice_dir()
    os.makedirs(vdir, exist_ok=True)
    safe = "".join(c for c in name if c.isalnum() or c in (" ", "-", "_")).strip() or "voice"
    short = _CLONE_MODEL_SHORT.get((model or "").strip().lower()) or (model.strip() if model and model.strip() else "VieNeu")
    final = f"{safe} - {short}"
    ext = os.path.splitext(file.filename or "")[1].lower() or ".wav"
    dest = os.path.join(vdir, final + ".wav")  # always normalized to wav

    with tempfile.TemporaryDirectory() as td:
        raw = os.path.join(td, "raw" + ext)
        with open(raw, "wb") as f:
            f.write(file.file.read())
        # Trim to the first REF_TRIM_SEC seconds, normalize loudness, 48 kHz mono.
        _run_ffmpeg(
            ["-i", raw, "-t", str(REF_TRIM_SEC),
             "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
             "-ar", "48000", "-ac", "1", dest],
            step="trim reference",
        )

    # RE-CLONE invalidation: this overwrites any existing voice of the same name,
    # so the previous clip's derived caches are now stale. Delete the ref_text
    # sidecar and the cached preview so both regenerate from the NEW clip. Without
    # this, _resolve_ref_text would serve the OLD clip's transcript against the new
    # audio (text/audio mismatch → F5 echoes/garbles the reference), which is the
    # exact "re-cloned voice still broken" failure. (The worker's fingerprinted
    # sidecar also busts on its own; this is belt-and-suspenders and also clears
    # the preview wav whose mtime check could otherwise lag.)
    _invalidate_voice_caches(page, dest)

    # Pre-generate this clone's preview so the first Play is instant.
    background.add_task(_warm_clone_preview, page, dest)
    return {"name": final, "path": dest, "trimmedToSec": REF_TRIM_SEC}


def _invalidate_voice_caches(page: str | None, ref_path: str) -> None:
    """Remove a cloned voice's stale derived caches (ref_text sidecar + preview wav).

    Called on (re-)upload so a voice re-cloned under the SAME name never reuses the
    previous clip's transcript or preview. Best-effort: missing files are fine.

    Clones (and their _reftext/_previews caches) are shared across pages, so the
    sidecar lives in SHARED_VOICE_DIR/_reftext — the `page` arg is now unused."""
    stem = os.path.splitext(os.path.basename(ref_path))[0]
    vdir = _shared_voice_dir()
    sidecar = os.path.join(vdir, "_reftext", stem + ".txt")
    preview = _preview_cache_path(page, None, ref_path)
    for p in (sidecar, preview):
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass


@router.delete("/generate/voice")
def delete_voice(page: str, name: str):
    """Delete a cloned reference voice file from disk so it can be re-cloned.

    Guarded to the SHARED voice dir; presets (JSON-defined, not on disk) are
    unaffected. `name` is the clone display name (filename without extension),
    exactly as returned by list_voices (e.g. "My Voice - F5-TTS"). Clones are
    shared across pages, so deletion removes them for every page (the `page` arg
    is accepted for API compatibility but no longer scopes the target).
    """
    vdir = os.path.realpath(_shared_voice_dir())

    # Primary candidate is the normalized .wav we save on upload; fall back to
    # any audio file in the dir whose basename (sans ext) matches `name`.
    target = os.path.realpath(os.path.join(vdir, name + ".wav"))
    if not os.path.isfile(target):
        target = None
        if os.path.isdir(vdir):
            for fn in os.listdir(vdir):
                if fn.lower().endswith(_AUDIO_EXTS) and os.path.splitext(fn)[0] == name:
                    target = os.path.realpath(os.path.join(vdir, fn))
                    break

    # Containment check: the resolved target MUST live inside the page voice dir.
    # Guards against traversal (e.g. name="../../something"). Done before the
    # existence error so an out-of-dir path is rejected as 400, not leaked as 404.
    probe = target or os.path.realpath(os.path.join(vdir, name + ".wav"))
    try:
        contained = os.path.commonpath([probe, vdir]) == vdir
    except ValueError:
        contained = False  # different drive => definitely outside
    if not contained:
        raise HTTPException(400, "Tên giọng không hợp lệ")

    if not target or not os.path.isfile(target):
        raise HTTPException(404, f"Không tìm thấy giọng {name}")

    os.remove(target)

    # Best-effort: remove this clone's cached preview too (ignore if missing).
    try:
        preview = _preview_cache_path(page, None, target)
        if os.path.isfile(preview):
            os.remove(preview)
    except OSError:
        pass

    return {"deleted": True, "name": name}


def _media_url(path: str) -> str:
    return f"/media?path={urllib.parse.quote(path)}"


def _synth_preview_to_cache(page: str | None, voice: str | None, ref_audio: str | None,
                            engine: str | None = None) -> str:
    """Synthesize the fixed sample for a voice and store it at its cache path."""
    cache = _preview_cache_path(page, voice, ref_audio)
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    # Always use VieNeu for cached previews regardless of the clone's production engine.
    # VieNeu is CPU/ONNX (~3-5s) vs F5-TTS GPU cold-load (~60-90s). VieNeu still encodes
    # the same ref_audio so the preview voice resembles the reference. Production TTS
    # for actual video jobs still uses the correct engine (F5-TTS or VieNeu as stored).
    eng = _normalize_engine("vieneu", ref_audio)
    payload = {
        "items": [{"scene": 0, "text": _PREVIEW_TEXT}],
        "engine": eng,
        "voice": voice,
        "refAudio": ref_audio,
        "emotion": "natural",
        "applyWatermark": False,
        "outDir": os.path.dirname(cache),
    }
    result = _run_cf_worker("tts_worker.py", payload, timeout=300, retries=0)
    produced = result["results"][0]["audioPath"]
    if os.path.realpath(produced) != os.path.realpath(cache):
        os.replace(produced, cache)
    return cache


def _warm_clone_preview(page: str | None, ref_path: str) -> None:
    if model_busy():
        return  # a job owns the GPU; the preview will synth on demand later
    try:
        _synth_preview_to_cache(page, None, ref_path)
    except Exception as exc:
        # Best-effort: a failed warm must NOT break the upload — the on-demand
        # /generate/voice/preview endpoint is the fallback. But it must be VISIBLE
        # in the logs (silently swallowing hid the cuDNN-127 flake before), so log
        # it with the traceback for diagnosis.
        log.warning(
            "[generate] clone preview warm failed for page=%s ref=%s: %s "
            "(non-fatal; preview will synth on demand)",
            page, ref_path, exc, exc_info=True,
        )


class VoicePreviewRequest(BaseModel):
    page: str | None = None
    voice: str | None = None       # preset name
    refAudio: str | None = None    # path to a cloned reference voice
    engine: str | None = None      # "vieneu" | "f5-tts"; None = derive from clone name
    text: str | None = None


@router.post("/generate/voice/preview")
def voice_preview(req: VoicePreviewRequest):
    """Sample the chosen voice and return a playable URL.

    The sample sentence is fixed, so the result is cached per voice: the first
    play of a voice synthesizes (~8s, mostly cold model load); every replay is
    instant. A clone's cache is invalidated when its reference file changes.
    """
    if req.text:
        if model_busy():
            raise HTTPException(409, "Đang dựng video (model đang bận) — nghe thử lại sau khi job xong.")
        eng = _normalize_engine(req.engine, req.refAudio)
        # Custom text isn't cacheable — synth fresh into an EPHEMERAL throwaway dir.
        # This is the one place the per-page voice dir is still used: only as a
        # scratch output location for the non-cached custom-text preview wav. The
        # CLONE refs + their fingerprint/preview caches are shared (SHARED_VOICE_DIR);
        # only refAudio (an absolute shared path) drives the synth here.
        payload = {
            "items": [{"scene": 0, "text": req.text}],
            "engine": eng,
            "voice": req.voice,
            "refAudio": req.refAudio,
            "emotion": "natural",
            "applyWatermark": False,
            "outDir": os.path.join(_page_voice_dir(req.page), "_previews"),
        }
        _gpu_eng = eng in ("f5-tts", "omnivoice")
        timeout = 420 if _gpu_eng else 300
        retries = 2 if _gpu_eng else 0  # ride out the transient cuDNN-127 / WinError-6714 load flake
        produced = _run_cf_worker("tts_worker.py", payload, timeout=timeout, retries=retries)["results"][0]["audioPath"]
        return {"audioPath": produced, "url": _media_url(produced), "cached": False}

    cache = _preview_cache_path(req.page, req.voice, req.refAudio)
    fresh = os.path.isfile(cache)
    if fresh and req.refAudio and os.path.isfile(req.refAudio):
        fresh = os.path.getmtime(cache) >= os.path.getmtime(req.refAudio)
    if fresh:
        return {"audioPath": cache, "url": _media_url(cache), "cached": True}

    if model_busy():
        raise HTTPException(409, "Đang dựng video (model đang bận) — nghe thử lại sau khi job xong.")
    cache = _synth_preview_to_cache(req.page, req.voice, req.refAudio, req.engine)
    return {"audioPath": cache, "url": _media_url(cache), "cached": False}


@router.post("/generate/voices/prewarm")
def prewarm_voices():
    """Pre-generate all preset previews in one model load (skips cached ones)."""
    if model_busy():
        raise HTTPException(409, "Đang dựng video (model đang bận) — thử lại sau.")
    names: list[str] = []
    try:
        with open(VIENEU_VOICES_JSON, encoding="utf-8") as f:
            names = list(json.load(f).get("presets", {}).keys())
    except (OSError, json.JSONDecodeError):
        pass

    items = [
        {"voice": nm, "outPath": _preview_cache_path(None, nm, None)}
        for nm in names
        if not os.path.isfile(_preview_cache_path(None, nm, None))
    ]
    if not items:
        return {"warmed": 0, "alreadyCached": len(names)}

    result = _run_cf_worker(
        "prewarm_worker.py",
        {"items": items, "text": _PREVIEW_TEXT, "applyWatermark": False},
        timeout=600,
    )
    return {"warmed": result["count"], "alreadyCached": len(names) - len(items)}


@router.get("/media")
def media(path: str):
    """Serve a media file, restricted to the content output root."""
    root = os.path.realpath(CONTENT_OUTPUT_ROOT)
    full = os.path.realpath(path)
    if full != root and not full.startswith(root + os.sep):
        raise HTTPException(403, "Path is outside the media root")
    if not os.path.isfile(full):
        raise HTTPException(404, "File not found")
    return FileResponse(full)


# --- Assembly (FFmpeg) --------------------------------------------------
#
# The only hand-written render step: each scene = its image with a slow Ken
# Burns zoom + burned caption, lasting exactly its voiceover. Scenes concat in
# order; optional background music is mixed under; an optional source credit
# slate is appended. FFmpeg is a host binary, so we call it directly (no cf-venv).

FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe")
# Headless Blender stickman renderer (render_model = "stickman-blender").
BLENDER_BIN = os.getenv("BLENDER_BIN", r"E:\Installed\Blender\blender.exe")
STICKMAN_SCRIPT = os.path.join(os.path.dirname(__file__), "blender", "stickman_render.py")
# Bundled Be Vietnam Pro (OFL) — designed for Vietnamese, so diacritics always
# render. Shipping the font (not relying on a system font) makes libass
# deterministic: it can't fall back to a font without Vietnamese glyphs.
_FONTS_DIR = os.path.join(os.path.dirname(__file__), "assets", "fonts")
CAPTION_FONT = os.getenv("CAPTION_FONT", os.path.join(_FONTS_DIR, "BeVietnamPro-Bold.ttf"))
CAPTION_FONT_FAMILY = os.getenv("CAPTION_FONT_FAMILY", "Be Vietnam Pro")
CAPTION_FONTSDIR = os.getenv("CAPTION_FONTSDIR", os.path.dirname(CAPTION_FONT))


def _libass_width_factor(font_path: str) -> float:
    """Ratio between the width libass RENDERS a glyph run at and the width Pillow
    MEASURES for the same nominal fontsize. libass scales a font's point size by
    the OS/2 win-metrics, so its effective pixel size = fontsize * upm /
    (usWinAscent + usWinDescent). Fonts with inflated win-metrics — Be Vietnam Pro
    reserves vertical headroom for stacked Vietnamese diacritics — render NARROWER
    than Pillow's nominal-size getlength() reports (factor ~0.65 here).

    Per-word karaoke layout positions each word with \\pos() using Pillow widths;
    without this correction the reserved boxes are ~1.55x too wide and the unused
    slack appears as large, UNEVEN inter-word gaps. Multiplying every Pillow width
    by this factor makes the reserved box equal the actually-rendered glyph width,
    so the visible gap collapses to exactly `space_w` for every word pair.

    Parsed straight from the TTF (no fontTools dependency). Returns 1.0 on any
    parse failure (degrades to the old over-wide behavior rather than crashing).
    """
    try:
        with open(font_path, "rb") as fh:
            d = fh.read()
        n = struct.unpack(">H", d[4:6])[0]
        off = {}
        for i in range(n):
            o = 12 + i * 16
            off[d[o:o + 4]] = struct.unpack(">I", d[o + 8:o + 12])[0]
        upm = struct.unpack(">H", d[off[b"head"] + 18:off[b"head"] + 20])[0]
        oo = off[b"OS/2"]
        win_asc = struct.unpack(">H", d[oo + 74:oo + 76])[0]
        win_desc = struct.unpack(">H", d[oo + 76:oo + 78])[0]
        c = upm / float(win_asc + win_desc)
        if 0.3 < c <= 1.0:
            return c
    except Exception:
        pass
    return 1.0


CAPTION_LIBASS_WFACTOR = _libass_width_factor(CAPTION_FONT)


def _caption_fontsize(width: int) -> int:
    """Single source for the on-screen caption font size, shared by the karaoke
    (_build_karaoke_ass) and dubbed (_build_dubbed_ass) ASS builders so both modes
    render captions at the SAME size for a given frame width (was width//17*0.8 vs
    width//20 — inconsistent). ~0.047*width: 50px @1080w, 89px @1920w."""
    return max(36, int(width // 17 * 0.8))


# GLOBAL karaoke caption size boost for 9:16 (owner-confirmed, ALL modes). Portrait
# 9:16 shorts are watched on a phone where the base caption reads small; DOUBLE the
# karaoke font there. The libass width-factor (_word_px scales getlength at the actual
# fontsize) auto-compensates, so word spacing stays correct at the larger size.
KARAOKE_9X16_FONT_SCALE = float(os.getenv("KARAOKE_9X16_FONT_SCALE", "2.0"))


def _is_9x16(width: int, height: int) -> bool:
    """True when the output aspect is ~9:16 (portrait, height/width ≈ 16/9)."""
    return height > width and abs((height / float(max(1, width))) - (16.0 / 9.0)) < 0.06


def _karaoke_fontsize(width: int, height: int) -> int:
    """Karaoke caption font size = the shared caption size, DOUBLED at 9:16 (Section D,
    owner-confirmed global). Used by _build_karaoke_ass and for cover-band positioning."""
    fs = _caption_fontsize(width)
    if _is_9x16(width, height):
        fs = int(round(fs * KARAOKE_9X16_FONT_SCALE))
    return fs

# --- Assemble video encoder (NVENC by default; CPU fallback) -----------------
# The owner wants the (otherwise-idle) GPU used to MAXIMIZE assemble speed. The
# big structural win is the cheaper 1/4-res blur (see _bg_blur_chain); on top of
# that, the GPU H.264 encoder (h264_nvenc) shaves another ~25-40% off wall-clock.
#
# DEFAULT is now NVENC, auto-detected: if h264_nvenc is present AND a one-shot
# test encode succeeds, assemble uses the GPU; otherwise it transparently FALLS
# BACK to libx264 (CPU) so it never hard-fails on a machine without NVENC. The
# chosen encoder is logged once. Override with ASSEMBLE_VENC=nvenc|libx264|auto.
#
# NVENC is tuned for SPEED at acceptable Shorts quality: preset p1 (fastest) +
# -rc vbr -cq 25. Measured VMAF of p1/cq25 (96.7) is actually ABOVE the old CPU
# libx264 crf23 default (94.6) on real footage — NVENC spends bits generously, so
# the fastest preset still looks good. Both encoders emit h264/yuv420p, so the
# concat stream-copy and the final output contract (1080x1920 h264/yuv420p + aac)
# are UNCHANGED. x264 preset order fast→slow is ultrafast > superfast > veryfast …
# ("faster" is actually SLOWER than "veryfast" — measured — so veryfast stays).
ASSEMBLE_VENC = os.getenv("ASSEMBLE_VENC", "auto").strip().lower()
# Quality-first defaults (owner-approved). We treat the LOCAL file as a near-lossless
# MASTER because Facebook/YouTube re-encode every upload onto their own bitrate ladder;
# a high-bitrate source survives that re-encode far better. libx264 crf18/medium is
# visually ~transparent; NVENC gets a quality target PLUS a real bitrate floor/ceiling
# (see NVENC args) so static/Ken-Burns scenes stop collapsing to <1 Mbps.
X264_PRESET = os.getenv("ASSEMBLE_X264_PRESET", "medium")
X264_CRF = os.getenv("ASSEMBLE_X264_CRF", "18")
NVENC_PRESET = os.getenv("ASSEMBLE_NVENC_PRESET", "p5")
NVENC_CQ = os.getenv("ASSEMBLE_NVENC_CQ", "20")
# Bitrate floor/ceiling for the NVENC path. VERIFIED with ffprobe on near-flat content
# (worst case for bitrate collapse — a soft SDXL background with a slow Ken-Burns pan):
#   old  -rc vbr -cq 25 -b:v 0                  -> 0.12 Mbps  (collapse; = the v165 bug)
#   vbr  -rc vbr -cq 20 -b:v 12M -maxrate 16M   -> 0.51 Mbps  (cq is a quality CAP; -b:v
#                                                  is only a CEILING in VBR — floor DID
#                                                  NOT take, exactly the owner's warning)
#   cbr  -rc cbr -b:v 12M -maxrate 16M          -> 11.8 Mbps  (floor HELD)
# So only CBR provably enforces a bitrate floor on trivially compressible frames. We
# default to CBR so every scene ships a fat ~12 Mbps master that survives FB/YouTube
# re-encode. On busy content CBR measured 12.6 Mbps High profile; encode was only ~7%
# slower than the old p1 path (the CPU zoompan filter dominates, not the encoder).
# Trade-off: CBR pads flat frames with filler bits (bigger files). Set
# ASSEMBLE_NVENC_RC=vbr to switch to lean quality-targeted mode (uses NVENC_CQ; spends
# bits only where detail needs them — smaller files, but flat scenes drop below target).
NVENC_RC = os.getenv("ASSEMBLE_NVENC_RC", "cbr").strip().lower()
NVENC_TARGET_BV = os.getenv("ASSEMBLE_NVENC_BV", "12M")
NVENC_MAXRATE = os.getenv("ASSEMBLE_NVENC_MAXRATE", "16M")
NVENC_BUFSIZE = os.getenv("ASSEMBLE_NVENC_BUFSIZE", "24M")
# Audio master quality: stereo AAC at a comfortable bitrate (was mono / no explicit
# bitrate, which inherited F5's mono VO and a low default). Platforms prefer stereo >=128k.
ASSEMBLE_AUDIO_BR = os.getenv("ASSEMBLE_AUDIO_BR", "192k")

# --- H.264 stream hygiene (GOP / level) — applied UNIFORMLY to every encode ---
# These MUST be identical across scene / slate / footage-cut / dubbed / translate
# encodes, else the final concat demuxer refuses to stream-copy. They live in
# _video_encoder_args() so all call-sites inherit them.
#   * Closed GOP ~2s: platforms (FB/YouTube) re-segment on keyframes and prefer a
#     short, closed GOP. Default keyframe interval = round(GOP_SECONDS * fps) (60 @
#     30fps). x264 gets keyint_min + sc_threshold=0 for a FIXED closed GOP; NVENC
#     gets -g + -forced-idr 1 (NVENC H.264 GOPs are closed by default).
#   * Level 4.2: comfortably covers 1080x1920@30 High; profile is already 'high'.
ASSEMBLE_GOP_SECONDS = float(os.getenv("ASSEMBLE_GOP_SECONDS", "2"))
H264_LEVEL = os.getenv("ASSEMBLE_H264_LEVEL", "4.2")
# --- libx264 fallback bitrate FLOOR ------------------------------------------
# The old fallback was CRF18 with NO floor: a flat SDXL / slow Ken-Burns scene
# collapses well below FB's preferred bitrate (the exact regression the NVENC CBR
# path was added to fix). Give the CPU fallback a real floor mirroring NVENC CBR
# 12M. ASSEMBLE_X264_RC:
#   'cbr'   (default) — true CBR via -x264-params nal-hrd=cbr with minrate=maxrate=
#            bitrate: pads flat frames with filler so the rate CANNOT collapse.
#            (bitrate==maxrate is what makes nal-hrd=cbr actually hold a floor; a
#            minrate<maxrate spread does NOT pad in x264 → floor would not take,
#            same trap as NVENC VBR.)
#   'floor' — ABR target + VBV minrate/maxrate window (keeps a 16M ceiling; floor
#            is best-effort, not guaranteed on trivially-compressible frames).
#   'crf'   — revert to the old quality-only CRF18 (NO floor).
ASSEMBLE_X264_RC = os.getenv("ASSEMBLE_X264_RC", "cbr").strip().lower()
X264_BV = os.getenv("ASSEMBLE_X264_BV", "12M")
X264_MINRATE = os.getenv("ASSEMBLE_X264_MINRATE", "12M")
X264_MAXRATE = os.getenv("ASSEMBLE_X264_MAXRATE", "16M")
X264_BUFSIZE = os.getenv("ASSEMBLE_X264_BUFSIZE", "24M")

# --- Parallel scene encoding -------------------------------------------------
# The per-scene clip encodes are independent (each is image/clip + VO -> a self-
# contained mp4); only the CONCAT/bgm/slate tail is order-dependent. Encoding the
# scenes one-at-a-time left the CPU/GPU ~45% idle, so we run them through a bounded
# worker pool. The CONCAT step still consumes the clips in scene order, so output
# ordering is unaffected by completion order.
#
# Measured on the target box (RTX 2070 Max-Q 8GB, FFmpeg 8.1.1, current driver):
#   - h264_nvenc ran 5 concurrent 1080x1920 sessions cleanly (nvidia-smi
#     encoder.stats.sessionCount=5, NO "OpenEncodeSessionEx"/session-limit error;
#     the old consumer 3-session cap is lifted on this driver).
#   - VRAM cost ~155 MiB PER session (1359 MiB used at 5 sessions vs 580 idle) —
#     negligible against 8GB; VRAM is not the limiter.
#   - 4 parallel vs 4 serial pure-NVENC encodes: 2.32s vs 3.86s (~1.66x). The real
#     assemble does heavy CPU-side filtering (Ken Burns zoompan / boxblur / libass
#     drawtext) per scene, which parallelizes BETTER across cores than raw NVENC.
# Default = 3 (conservative: real speedup with headroom left for an adjacent
# ComfyUI/whisper step and well under the measured NVENC ceiling). 1 = kill-switch
# (exact previous serial behavior). NVENC is clamped to NVENC_MAX_PARALLEL; libx264
# (CPU) is bounded by cores instead (a stuck-low session cap doesn't apply on CPU).
NVENC_MAX_PARALLEL = int(os.getenv("ASSEMBLE_NVENC_MAX_PARALLEL", "5"))  # measured-safe ceiling
try:
    ASSEMBLE_PARALLEL = max(1, int(os.getenv("ASSEMBLE_PARALLEL", "3")))
except ValueError:
    ASSEMBLE_PARALLEL = 3

# Resolved once per process: "nvenc" or "libx264". None until first resolved.
_resolved_venc: str | None = None
_venc_lock = threading.Lock()


def _scene_encode_workers() -> int:
    """How many scene clips to encode concurrently, given the resolved encoder.

    NVENC: clamp to the measured-safe session ceiling (NVENC_MAX_PARALLEL) so we
    never trip the driver's encode-session limit. libx264 (CPU): the limiter is
    cores, not encode sessions, so allow up to the CPU count (each x264 at the
    'veryfast' preset is only lightly threaded, so several in parallel use idle
    cores). Either way honor the ASSEMBLE_PARALLEL request and the kill-switch (1).
    """
    want = max(1, ASSEMBLE_PARALLEL)
    if want == 1:
        return 1
    if _resolve_venc() == "nvenc":
        n = min(want, max(1, NVENC_MAX_PARALLEL))
        if want > NVENC_MAX_PARALLEL:
            log.warning("[assemble] ASSEMBLE_PARALLEL=%d > measured NVENC ceiling %d "
                        "— clamping to %d", want, NVENC_MAX_PARALLEL, n)
        return n
    # libx264 / CPU: bound by cores (leave at least one for the OS/decoder).
    cores = os.cpu_count() or 4
    return max(1, min(want, cores))


def _nvenc_usable() -> bool:
    """True if h264_nvenc both EXISTS in this ffmpeg AND can actually encode here.

    We don't trust `-encoders` alone (it lists nvenc even when the driver/GPU
    can't run it); we run a tiny test encode and require exit 0 + a non-empty file.
    This is what makes auto-detect safe: a driverless/GPU-less box silently uses
    CPU. NOTE: NVENC has a MINIMUM frame size and needs a few frames to open the
    encoder before EOF (a 1-frame / 64px test fails with "Frame Dimension less
    than the minimum" / "Could not open encoder before EOF" even on a working GPU),
    so the probe uses 256x256 @ 0.5s — verified to be the safe floor on this build.
    """
    try:
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "t.mp4")
            r = subprocess.run(
                [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.5:r=30",
                 "-c:v", "h264_nvenc", "-preset", NVENC_PRESET, "-pix_fmt", "yuv420p",
                 out],
                # Force UTF-8 to avoid cp1252 decode errors on captured ffmpeg output.
                capture_output=True, encoding="utf-8", errors="replace", timeout=30,
            )
            return r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0
    except (OSError, subprocess.SubprocessError):
        return False


def _resolve_venc() -> str:
    """Decide the assemble encoder once, with detection + fallback, and log it.

    ASSEMBLE_VENC: 'auto' (default) -> prefer NVENC if usable, else CPU;
    'nvenc'/'h264_nvenc' -> NVENC if usable, else fall back to CPU (warn);
    'libx264'/'cpu' -> always CPU.
    """
    global _resolved_venc
    if _resolved_venc is not None:
        return _resolved_venc
    with _venc_lock:
        if _resolved_venc is not None:
            return _resolved_venc
        want = ASSEMBLE_VENC
        if want in ("libx264", "cpu", "x264"):
            chosen = "libx264"
            log.info("[assemble] video encoder: libx264 (CPU) — ASSEMBLE_VENC=%s", want)
        elif want in ("nvenc", "h264_nvenc", "gpu"):
            if _nvenc_usable():
                chosen = "nvenc"
                log.info("[assemble] video encoder: h264_nvenc (GPU) — requested, test-encode OK")
            else:
                chosen = "libx264"
                log.warning("[assemble] h264_nvenc requested but NOT usable on this "
                            "machine — falling back to libx264 (CPU)")
        else:  # auto (default) or anything unrecognized
            if _nvenc_usable():
                chosen = "nvenc"
                log.info("[assemble] video encoder: h264_nvenc (GPU) — auto-detected, "
                         "preset=%s cq=%s", NVENC_PRESET, NVENC_CQ)
            else:
                chosen = "libx264"
                log.info("[assemble] video encoder: libx264 (CPU) — NVENC not usable "
                         "(auto)")
        _resolved_venc = chosen
        return chosen


# H.264 level limits (Annex A, Table A-1), low→high: (level, MaxFS, MaxMBPS).
# MaxFS = max frame size in MACROBLOCKS (16x16); MaxMBPS = max macroblocks PER SECOND
# (i.e. MaxFS-per-frame x frame rate). Exceeding EITHER makes the encoder REFUSE to
# open with error -22 — two distinct failures that look identical downstream:
#   * MaxFS  — a 1440p frame (2560x1440 = 14400 MB) at the default level 4.2
#     (MaxFS 8704) fails, while ≤1080p (8160 MB) fits.
#   * MaxMBPS — a 1440p source at 60fps needs 14400*60 = 864000 MB/s, which does NOT
#     fit level 5.0 (589824) even though its MaxFS does; NVENC reports
#     "InitializeEncoder failed: invalid param (8): Invalid Level." (job 284). It
#     needs 5.1 (983040). So the frame rate MUST be part of the choice, not just dims.
_H264_LEVEL_LIMITS = [
    ("3.0", 1620, 40500), ("3.1", 3600, 108000), ("3.2", 5120, 216000),
    ("4.0", 8192, 245760), ("4.1", 8192, 245760), ("4.2", 8704, 522240),
    ("5.0", 22080, 589824), ("5.1", 36864, 983040), ("5.2", 36864, 2073600),
    ("6.0", 139264, 4177920), ("6.1", 139264, 8355840), ("6.2", 139264, 16711680),
]


def _h264_level_for(width: int | None, height: int | None, base: str = H264_LEVEL,
                    fps: float | None = None) -> str:
    """Smallest H.264 level that is >= `base` AND fits a width*height frame at `fps`
    (both MaxFS and MaxMBPS — see _H264_LEVEL_LIMITS).

    Returns `base` unchanged when the dims are unknown or already fit — so every
    ≤1080p encode (all image/slate/assembly call-sites) keeps the exact old level and
    stays byte-identical for the `-c copy` concat. Only a larger/faster frame (a
    1440p/4K or high-fps passthrough-trim SOURCE cut) bumps the level up so NVENC/x264
    will open. `fps` is the clip's REAL frame cadence (for a passthrough-trim cut that
    is the SOURCE fps, not the pipeline's 30) — omit it to check MaxFS only. All cut
    clips of ONE render share the source resolution AND cadence, so they still get the
    SAME level (concat stays uniform). Caps at 6.2 (a >8K frame would fail NVENC's own
    limits)."""
    if not width or not height:
        return base
    need_fs = ((int(width) + 15) // 16) * ((int(height) + 15) // 16)
    # Unknown/degenerate fps -> MaxFS-only (the pre-fps behaviour), never a crash.
    need_mbps = need_fs * fps if fps and fps > 0 else 0
    base_idx = next((i for i, lim in enumerate(_H264_LEVEL_LIMITS) if lim[0] == base), 0)
    for i, (lv, maxfs, maxmbps) in enumerate(_H264_LEVEL_LIMITS):
        if i >= base_idx and maxfs >= need_fs and maxmbps >= need_mbps:
            return lv
    return _H264_LEVEL_LIMITS[-1][0]


def _video_encoder_args(fps: int = 30, width: int | None = None, height: int | None = None,
                        src_fps: float | None = None) -> list[str]:
    """Video-encoder ffmpeg args for an assemble clip. h264/yuv420p High profile,
    level 4.2, closed ~2s GOP either way, so EVERY encode call-site emits byte-for-
    byte-compatible stream params and the final concat stays a `-c copy` stream-copy.

    `fps` sets the keyframe interval (round(ASSEMBLE_GOP_SECONDS*fps)); pass the same
    fps used to encode the clip (the pipeline is a uniform 30fps, so all clips in one
    render share the GOP and stay concat-compatible).

    `src_fps` is the clip's REAL output cadence, used ONLY to pick the H.264 level
    (MaxMBPS). A passthrough-trim cut keeps the SOURCE cadence (e.g. 60fps) while `fps`
    stays the pipeline's 30 for the GOP, so the two are NOT interchangeable — see
    _h264_level_for. Omit it and the level check falls back to MaxFS only.

    Resolves the encoder lazily (NVENC by default with CPU fallback; see
    _resolve_venc). Quality-first master: NVENC p5, CBR 12M (default) which provably
    holds a ~12 Mbps floor even on flat content (ffprobe-verified); the libx264
    fallback now ALSO carries a real bitrate floor (CBR 12M by default) instead of the
    old floor-less CRF18."""
    gop = max(1, round(ASSEMBLE_GOP_SECONDS * (fps or 30)))
    # Level must fit the actual frame AND its rate (see _h264_level_for). Unknown/≤1080p
    # dims -> H264_LEVEL unchanged; a 1440p/4K or high-fps passthrough-trim cut bumps it
    # so the encoder opens.
    level = _h264_level_for(width, height, fps=src_fps)
    # SDR BT.709 output signalling. Untagged H.264 leaves the color space
    # unsignalled, so Facebook/players GUESS it on upload and re-encode with a
    # shifted/washed-out matrix. Two parts, both output-side and signalling-only (no
    # input -color* flags, no scaling/zoompan filter changes):
    #   (1) the -color* output options tag the container 'colr' atom + set the
    #       matrix (colorspace) and range.
    #   (2) IMPORTANT (ffmpeg 8.1 quirk, ffprobe-verified on this box): the -color*
    #       options ALONE only write matrix_coefficients + video_full_range_flag into
    #       the H.264 SPS VUI; colour_primaries + transfer_characteristics stay
    #       "unknown". The h264_metadata bitstream filter forces ALL FOUR VUI fields
    #       (1 = BT.709; video_full_range_flag=0 = tv/limited range). transfer
    #       (gamma) is a primary driver of the washed-out look, so it must be tagged.
    # Applied IDENTICALLY at EVERY call-site (all go through this function), so every
    # clip carries the same SPS/VUI and the final concat stays a byte-for-byte
    # `-c copy` stream-copy (verified: 2-clip concat -c copy preserves all four tags).
    color_vui = ["-colorspace", "bt709", "-color_primaries", "bt709",
                 "-color_trc", "bt709", "-color_range", "tv",
                 "-bsf:v", ("h264_metadata=colour_primaries=1:"
                            "transfer_characteristics=1:matrix_coefficients=1:"
                            "video_full_range_flag=0")]
    if _resolve_venc() == "nvenc":
        # -forced-idr 1 makes each -g keyframe a real IDR (closed GOP); NVENC H.264
        # does not emit open GOPs, so -g + forced-idr is a fixed closed GOP.
        args = ["-c:v", "h264_nvenc", "-profile:v", "high", "-level:v", level,
                "-pix_fmt", "yuv420p", "-preset", NVENC_PRESET,
                "-g", str(gop), "-forced-idr", "1"] + color_vui
        if NVENC_RC == "cbr":
            # CBR is the ONLY mode that enforces the floor on near-static content
            # (VBR under-spends on flat frames — ffprobe-verified). -maxrate/-bufsize
            # set the VBV window so the constant rate is honoured without spikes.
            args += ["-rc", "cbr", "-b:v", NVENC_TARGET_BV,
                     "-maxrate", NVENC_MAXRATE, "-bufsize", NVENC_BUFSIZE]
        else:
            # Lean quality-targeted VBR: -cq drives quality, -b:v/-maxrate cap the
            # ceiling. NOTE: NVENC VBR does NOT hold a bitrate floor even with -b:v
            # (documented + ffprobe-verified) — flat scenes still under-spend. If a
            # guaranteed floor is required, use the default RC=cbr. Empty
            # NVENC_TARGET_BV -> pure cq.
            args += ["-rc", "vbr", "-cq", NVENC_CQ]
            if NVENC_TARGET_BV.strip():
                args += ["-b:v", NVENC_TARGET_BV, "-maxrate", NVENC_MAXRATE, "-bufsize", NVENC_BUFSIZE]
            else:
                args += ["-b:v", "0"]
        return args
    # libx264 (CPU) fallback: closed GOP via keyint + keyint_min + scenecut off.
    # Same BT.709 output VUI tags as the NVENC branch — identical flags keep the
    # streams uniform for the `-c copy` concat and stop FB's guess-and-shift re-encode.
    args = ["-c:v", "libx264", "-profile:v", "high", "-level:v", level,
            "-pix_fmt", "yuv420p", "-preset", X264_PRESET,
            "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0"] + color_vui
    if ASSEMBLE_X264_RC == "crf":
        # Legacy quality-only mode: no bitrate floor (flat scenes can collapse).
        args += ["-crf", X264_CRF]
    elif ASSEMBLE_X264_RC == "floor":
        # ABR target + VBV window: keeps a real ceiling (X264_MAXRATE) with a best-
        # effort floor (X264_MINRATE). Not a guaranteed pad on flat frames.
        args += ["-b:v", X264_BV, "-minrate", X264_MINRATE,
                 "-maxrate", X264_MAXRATE, "-bufsize", X264_BUFSIZE]
    else:
        # 'cbr' (default): true CBR. nal-hrd=cbr only holds a floor when
        # bitrate==maxrate==minrate — it then pads trivially-compressible frames with
        # filler NAL so a flat scene CANNOT drop below X264_BV (mirrors NVENC CBR).
        args += ["-b:v", X264_BV, "-minrate", X264_BV, "-maxrate", X264_BV,
                 "-bufsize", X264_BUFSIZE, "-x264-params", "nal-hrd=cbr"]
    return args


def _audio_encoder_args() -> list[str]:
    """AAC audio args for an assemble clip: stereo at ASSEMBLE_AUDIO_BR, 48 kHz. Kept
    IDENTICAL across scene/slate/footage/cut encodes so the concat demuxer can still
    stream-copy (mismatched channel counts/bitrates would force a re-mux). F5's VO is
    mono; -ac 2 up-mixes it to stereo so the whole master is uniformly stereo."""
    return ["-c:a", "aac", "-b:a", ASSEMBLE_AUDIO_BR, "-ar", "48000", "-ac", "2"]


def _bg_blur_chain(w: int, h: int) -> str:
    """9:16 blurred-background fill chain for footage mode.

    The heavy decorative blur used to run at FULL output resolution
    (boxblur=40:8 on 1080x1920), which was BY FAR the dominant assemble cost
    (~6x the rest). A heavy blur destroys detail anyway, so we blur a 1/4-res
    copy and upscale — visually indistinguishable, a fraction of the per-pixel
    work. Measured: footage assemble dropped ~6x (e.g. 166s → 27s for 20 scenes).
    """
    bw, bh = max(2, w // 4), max(2, h // 4)
    return (
        f"[bg]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
        f"boxblur=10:2,scale={w}:{h},setsar=1[bgb]"
    )


def _ff_filter_path(p: str) -> str:
    """Escape a Windows path for use INSIDE a filtergraph (fontfile/textfile)."""
    return p.replace("\\", "/").replace(":", "\\:")


# --- Assemble progress plumbing ----------------------------------------------
# assemble()/assemble_footage() run IN-PROCESS (the runner calls them directly,
# not via a cf-venv worker), so the worker progress-file mechanism does not apply.
# Instead the runner installs an FFmpeg progress sink for the current thread; each
# _run_ffmpeg call parses `-progress pipe:1` (out_time_us) against the clip's known
# duration and reports a 0..1 fraction of THAT step. An "allocator" maps each
# sub-step's fraction onto an overall 0..100 band so the percent advances smoothly
# across all scenes + concat + bgm instead of sitting at 0 then jumping to 100.
_ff_progress_local = threading.local()


def set_ff_progress_cb(cb) -> None:
    """Install (cb) or clear (None) an FFmpeg progress callback for THIS thread.
    cb receives (pct: int 0-100, msg: str). Thread-local, like set_progress_cb."""
    _ff_progress_local.cb = cb


class _AssembleProgress:
    """Maps per-ffmpeg-call fractions onto one overall 0..100 percentage.

    The caller declares the total 'weight' (≈ total seconds of video to encode,
    plus a small constant for concat/bgm). Each step is run via .step(weight, dur,
    msg, fn): while fn's ffmpeg runs, its out_time/dur fraction is scaled into the
    [done, done+weight] slice of the whole. The emitted percent is mapped onto an
    optional outer band [lo, hi] (the runner's assemble band is [85, 99])."""

    def __init__(self, cb, total_weight: float, lo: int = 0, hi: int = 100, label: str = "Dựng video"):
        self.cb = cb
        self.total = max(1e-6, float(total_weight))
        self.done = 0.0
        self.lo, self.hi = lo, hi
        self.label = label
        # Optional phase suffix appended to every emitted message (e.g. " — dò phụ đề
        # gốc"). Callers that never set it (assemble_footage / assemble()) get exactly
        # the old "Dựng video NN%" text.
        self.phase = ""
        # Guards self.done / the live per-scene contributions when scenes encode in
        # PARALLEL (serial .step() never contends, but the lock is cheap and keeps the
        # accumulator correct either way).
        self._lock = threading.Lock()

    def _emit(self, frac_overall: float):
        if not self.cb:
            return
        frac = min(1.0, max(0.0, frac_overall))
        pct = self.lo + frac * (self.hi - self.lo)
        try:
            # Label shows the mapped pct (lo..hi), not the raw 0..100 frac, so when a
            # HEAD is reserved for pre-encode work (lo>0) the encode label CONTINUES
            # from the head (e.g. 12%..100%) instead of restarting at 0% — no backwards
            # jump in the FE chip. For lo=0/hi=100 this is identical to the old label.
            self.cb(int(round(pct)), f"{self.label} {int(round(pct))}%{self.phase}")
        except Exception:
            pass

    def begin(self):
        self._emit(0.0)

    def step(self, weight: float, dur: float, fn):
        """Run fn (which performs one _run_ffmpeg) while reporting its progress.
        `weight` is this step's share of the total; `dur` is its output seconds."""
        base = self.done
        w = max(0.0, float(weight))

        def on_frac(step_frac: float):
            self._emit((base + w * min(1.0, max(0.0, step_frac))) / self.total)

        _ff_progress_local.step = {"dur": max(1e-6, float(dur)), "cb": on_frac}
        try:
            fn()
        finally:
            _ff_progress_local.step = None
        with self._lock:
            self.done = base + w
            done = self.done
        self._emit(done / self.total)

    def step_manual(self, weight: float, fn, phase: str = ""):
        """Run fn while IT drives the fraction, for a sub-stage that is NOT one
        _run_ffmpeg call (a cf-venv worker, a Python/GPU pass, ...).

        fn is called as fn(report) where report(frac: 0..1) maps that fraction onto
        this step's [done, done+weight] slice — the same arithmetic .step() applies
        to an ffmpeg out_time fraction, just fed from another source. On return the
        accumulator is pinned to done+weight exactly like .step(), so a stage that
        never reports still leaves the bar at the right boundary.

        `phase` is appended to the emitted message while the step runs (so the FE chip
        names the sub-stage), and cleared afterwards.
        """
        base = self.done
        w = max(0.0, float(weight))

        def report(step_frac: float):
            self._emit((base + w * min(1.0, max(0.0, step_frac))) / self.total)

        prev_phase = self.phase
        self.phase = phase or prev_phase
        try:
            return fn(report)
        finally:
            self.phase = prev_phase
            with self._lock:
                self.done = base + w
                done = self.done
            self._emit(done / self.total)

    def run_parallel(self, items, fn, max_workers: int):
        """Run independent scene-encode tasks concurrently while keeping the overall
        percent monotonic and reaching the post-scene total.

        items: list of (weight, dur, payload) — `weight` is this scene's share of
        the total, `dur` its output seconds (for the live ffmpeg fraction), `payload`
        whatever fn needs. fn(payload) does the encode (one _run_ffmpeg under this
        thread's step). Results are returned IN INPUT ORDER regardless of completion
        order, so the caller's concat list stays deterministic.

        Each worker thread installs its OWN _ff_progress_local.step (thread-local, so
        the per-thread ffmpeg `-progress` callbacks never collide). Their live
        fractions feed a shared per-scene contribution map guarded by self._lock; the
        emitted percent = (already-done + sum of live contributions) / total, clamped
        monotonic. max_workers == 1 falls back to the plain serial path so the
        kill-switch reproduces the exact previous behavior.
        """
        n = len(items)
        results: list = [None] * n
        if n == 0:
            return results
        if max_workers <= 1:
            # Serial: identical to the old per-scene loop (step() advances done).
            for i, (w, dur, payload) in enumerate(items):
                self.step(w, dur, lambda payload=payload: results.__setitem__(i, fn(payload)))
            return results

        import concurrent.futures

        live = [0.0] * n           # current contribution of each in-flight scene (0..weight)
        base = self.done           # done before this batch (slate/tail not yet spent)

        def _emit_locked():
            # caller holds self._lock
            self.done = base + sum(live)
            return self.done

        def _on_frac(i, w, step_frac):
            sf = min(1.0, max(0.0, step_frac))
            with self._lock:
                live[i] = w * sf
                done = base + sum(live)
                if done < self.done:        # keep monotonic across threads
                    done = self.done
                self.done = done
            self._emit(done / self.total)

        def _work(i, w, dur, payload):
            # Each thread streams its clip's fraction into slot i. Reuse the existing
            # thread-local step mechanism that _run_ffmpeg reads.
            _ff_progress_local.step = {"dur": max(1e-6, float(dur)),
                                       "cb": lambda f, i=i, w=w: _on_frac(i, w, f)}
            try:
                r = fn(payload)
            finally:
                _ff_progress_local.step = None
            # Settle this scene's full weight on completion (covers any missed frames).
            with self._lock:
                live[i] = w
                done = base + sum(live)
                if done < self.done:
                    done = self.done
                self.done = done
            self._emit(done / self.total)
            return r

        errs: list = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(_work, i, w, dur, payload): i
                    for i, (w, dur, payload) in enumerate(items)}
            for fut in concurrent.futures.as_completed(futs):
                i = futs[fut]
                try:
                    results[i] = fut.result()
                except BaseException as e:  # noqa: BLE001 — re-raise after draining
                    errs.append(e)
        if errs:
            raise errs[0]
        # All scenes done: pin the accumulator to base + total scene weight so the
        # tail (slate/concat/bgm) starts from a clean, exact offset.
        with self._lock:
            self.done = base + sum(w for (w, _d, _p) in items)
            done = self.done
        self._emit(done / self.total)
        return results


def _ramped_step(prog, weight: float, expected_sec: float, phase: str, fn):
    """Spend `weight` of `prog` on a BLACK-BOX blocking call that emits no progress.

    Same honesty contract as runner._run_with_time_ramp: an asymptotic time ESTIMATE
    (1 - exp(-t/tau)) clamped to 92% of the step's own slice until fn actually returns,
    so the bar keeps moving through the stage but never claims the stage is (nearly)
    finished before it is. Used for the karaoke whisper pass, which gives no signal.

    With no progress sink (prog None, or its cb None on a direct HTTP call) fn() is run
    inline — no helper thread, i.e. byte-for-byte the previous behavior.
    """
    if prog is None or not getattr(prog, "cb", None):
        if prog is not None:
            prog.step_manual(weight, lambda report: None)
        return fn()

    def _run(report):
        result: dict = {}
        error: dict = {}

        def _target():
            try:
                result["value"] = fn()
            except BaseException as e:  # captured; re-raised on this thread below
                error["exc"] = e

        th = threading.Thread(target=_target, name="cf-assemble-ramp", daemon=True)
        th.start()
        t0 = time.time()
        tau = max(1.0, float(expected_sec))
        while th.is_alive():
            th.join(timeout=0.5)
            if not th.is_alive():
                break
            frac = 1.0 - math.exp(-(time.time() - t0) / tau)
            report(min(frac, 0.92))          # honest cap: never pin at the slice top
        if "exc" in error:
            raise error["exc"]
        return result.get("value")

    return prog.step_manual(weight, _run, phase=phase)


def _make_assemble_progress(scenes, durs, bgm_path, add_credit, logo_path, handle, source_name,
                            head: float = 0.0):
    """Build the assemble progress controller for the current thread's FFmpeg cb.

    Total weight ≈ total scene seconds (the encode cost) + a small slate share +
    small concat/bgm shares (those are stream-copy / audio-only, i.e. cheap, but
    given nonzero weight so the bar advances through them instead of stalling at
    the last scene's percent). Returns a controller whose .step() is a no-op when
    no FFmpeg progress cb is installed (direct HTTP calls), so callers stay simple.
    """
    cb = getattr(_ff_progress_local, "cb", None)
    scene_w = float(sum(durs)) or 1.0
    slate_w = 3.0 if (add_credit and (logo_path or handle or source_name)) else 0.0
    # concat copies all clips; bgm re-muxes audio over the concatenated video.
    # Both scale with total length but are ~50-100x faster than encoding, so a
    # small fraction of the scene weight is a fair, non-stalling allocation.
    concat_w = max(0.5, 0.02 * scene_w)
    bgm_w = (max(0.5, 0.02 * scene_w) if bgm_path else 0.0)
    total = scene_w + slate_w + concat_w + bgm_w
    # `head` (0..100) reserves the FRONT of the render band for un-instrumented
    # pre-encode work (whisper caption/pace passes) that the caller reports via
    # coarse milestones; the ffmpeg encode then spans [head, 100] so the FE chip
    # advances continuously instead of sitting frozen at 0% during the whisper phase.
    prog = _AssembleProgress(cb, total, lo=max(0.0, min(90.0, head)), hi=100)
    # Stash the sub-step weights so _finish_video can spend them without recomputing.
    prog.slate_w, prog.concat_w, prog.bgm_w = slate_w, concat_w, bgm_w
    if cb:
        prog.begin()
    return prog


def _parse_progress_us(line: str) -> int | None:
    """Pull microseconds-elapsed from an ffmpeg `-progress` line, or None."""
    if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
        # ffmpeg prints both keys; out_time_us is microseconds, out_time_ms is
        # ALSO microseconds in practice (a long-standing ffmpeg quirk), so treat
        # both the same and take whichever we see.
        val = line.split("=", 1)[1].strip()
        try:
            return int(val)
        except ValueError:
            return None
    return None


def _run_ffmpeg(args: list[str], step: str) -> None:
    """Run one ffmpeg job. If an assemble step is active for this thread, stream
    `-progress pipe:1` and report the out_time/dur fraction live; otherwise run
    plain (capture stderr for the error message)."""
    stepinfo = getattr(_ff_progress_local, "step", None)
    if not stepinfo:
        # Spawn via Popen (not subprocess.run) so the handle is registered for
        # immediate-kill on POST /stop. This is the path the footage per-scene CUT
        # (_cut_clip), the libx264 fallback, the thumbnail grab, and any non-assemble
        # ffmpeg take. Stop tree-kills it so a long cut/encode dies at once.
        proc = subprocess.Popen([FFMPEG_BIN, "-y", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace")
        _kill_job = _register_job_proc(proc)
        try:
            _, stderr = proc.communicate()
        finally:
            _unregister_job_proc(_kill_job, proc)
        if proc.returncode != 0:
            # A stop-kill makes returncode nonzero; surface a clear note so the failure
            # handler can recognise it (the runner also checks the cancel set).
            raise HTTPException(500, f"ffmpeg {step} failed: {(stderr or '')[-800:]}")
        return

    dur = stepinfo["dur"]
    on_frac = stepinfo["cb"]
    # -progress pipe:1 → machine-readable key=value on stdout; stderr carries the
    # human log we keep for error reporting. CRITICAL: stderr must go to a FILE,
    # not a PIPE. ffmpeg's stderr is verbose (filtergraph banner, per-second stats
    # even with -nostats some builds emit), and if we only drain stdout the ~64KB
    # stderr OS pipe buffer fills and ffmpeg DEADLOCKS. A file has no such limit
    # (same reasoning as _run_cf_worker_once).
    with tempfile.TemporaryDirectory() as td:
        err_path = os.path.join(td, "stderr.txt")
        f_err = open(err_path, "wb")
        _kill_job = None
        proc = None
        try:
            proc = subprocess.Popen(
                [FFMPEG_BIN, "-y", "-progress", "pipe:1", "-nostats", *args],
                stdout=subprocess.PIPE, stderr=f_err, text=True,
                encoding="utf-8", errors="replace",
            )
            # Register the assemble/encode ffmpeg for immediate-kill on POST /stop.
            _kill_job = _register_job_proc(proc)
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    us = _parse_progress_us(line.strip())
                    if us is not None:
                        on_frac(min(1.0, (us / 1e6) / dur))
            finally:
                if proc.stdout:
                    proc.stdout.close()
            proc.wait()
        finally:
            if proc is not None:
                _unregister_job_proc(_kill_job, proc)
            f_err.close()
        if proc.returncode != 0:
            try:
                with open(err_path, "rb") as f:
                    err = f.read().decode("utf-8", "replace")
            except OSError:
                err = ""
            raise HTTPException(500, f"ffmpeg {step} failed: {(err or '').strip()[-800:] or 'no stderr'}")


def render_stickman_clip(out_path: str, duration_s: float, width: int, height: int,
                         fps: int = 30, timeout: int = 600) -> str:
    """Render a stickman animation clip of `duration_s` seconds via headless Blender.

    Blender renders a PNG sequence then muxes to mp4 with FFMPEG_BIN (this build has
    no FFMPEG output format). Returns out_path. Used by render_model=stickman-blender.
    """
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    frames = max(1, round((duration_s or 3.0) * fps))
    env = {**os.environ, "FFMPEG_BIN": FFMPEG_BIN}
    # Popen (not subprocess.run) so the Blender process tree is registered for
    # immediate-kill on POST /stop; communicate(timeout=) preserves the wall-clock
    # ceiling. On stop the tree-kill ends Blender + its ffmpeg mux grandchild at once.
    try:
        proc = subprocess.Popen(
            [BLENDER_BIN, "-b", "-P", STICKMAN_SCRIPT, "--",
             out_path, str(frames), str(width), str(height), str(fps)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", env=env,
        )
    except FileNotFoundError:
        raise HTTPException(500, f"Blender not found: {BLENDER_BIN} (set BLENDER_BIN in .env)")
    _kill_job = _register_job_proc(proc)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_proc_tree(proc)
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            raise HTTPException(504, f"stickman render timed out after {timeout}s")
    finally:
        _unregister_job_proc(_kill_job, proc)
    if proc.returncode != 0 or not os.path.isfile(out_path):
        raise HTTPException(500, f"stickman render failed: {(stderr or stdout or '')[-800:]}")
    return out_path


def render_stickman_clip_procedural(out_path: str, duration_s: float, width: int, height: int,
                                    fps: int = 30) -> str:
    """Render a stickman clip via the CPU-only procedural 2D renderer (Pillow + FFmpeg).

    Used by render_model=stickman-procedural. Interchangeable with the Blender path:
    produces the same silent libx264/yuv420p clip at the pipeline resolution/fps, so the
    downstream footage assembler (VO + karaoke captions) treats it identically. Uses no
    GPU / VRAM. Returns out_path.
    """
    import importlib.util
    import sys

    mod_path = os.path.join(os.path.dirname(__file__), "workers", "stickman_procedural.py")
    mod = sys.modules.get("stickman_procedural")
    if mod is None:
        spec = importlib.util.spec_from_file_location("stickman_procedural", mod_path)
        if spec is None or spec.loader is None:
            raise HTTPException(500, f"stickman procedural module not found: {mod_path}")
        mod = importlib.util.module_from_spec(spec)
        # Register BEFORE exec so @dataclass annotation introspection finds the module.
        sys.modules["stickman_procedural"] = mod
        spec.loader.exec_module(mod)
    try:
        return mod.render_clip(out_path, duration_s, width, height, fps=fps, ffmpeg_bin=FFMPEG_BIN)
    except Exception as exc:  # noqa: BLE001 — surface as a clean API error
        raise HTTPException(500, f"procedural stickman render failed: {exc}")


def make_thumbnail(video_path: str, out_path: str | None = None, at: float = 1.0) -> str | None:
    """Grab a poster frame from a finished video for the Videos grid. Returns the
    jpg path, or None on failure. Default location: sibling '<name>.thumb.jpg'."""
    if not os.path.isfile(video_path):
        return None
    out_path = out_path or os.path.splitext(video_path)[0] + ".thumb.jpg"
    try:
        _run_ffmpeg(["-ss", f"{at:.2f}", "-i", video_path, "-frames:v", "1", "-q:v", "3", out_path],
                    step="thumbnail")
    except HTTPException:
        return None
    return out_path if os.path.isfile(out_path) else None


# Cover hold at the HEAD of the final video, in frames. The owner asked for the
# chosen cover to appear as the very FIRST FRAME (so the platform/player poster,
# derived from frame 0, shows it) — not an intro sequence. Default = 1 frame
# (~33ms @30fps). Bump via env ONLY if a platform's thumbnailer samples past frame 0
# and would skip a single-frame cover (see bake_cover_first_frame docstring).
_COVER_HOLD_FRAMES = max(1, int(os.getenv("CF_COVER_HOLD_FRAMES", "1")))


def bake_cover_first_frame(video_path: str, cover_image_path: str | None,
                           width: int, height: int, fps: int = 30) -> str:
    """Prepend the chosen cover image as the video's FIRST FRAME (in-stream) so the
    platform/player thumbnail — derived from frame 0 — shows the cover. No sibling
    .thumb.jpg is produced; the cover lives INSIDE the mp4 stream.

    Approach (no full re-encode, sync-preserving):
      1. Encode a tiny cover clip (_COVER_HOLD_FRAMES frames + silent stereo audio)
         with the SAME video/audio encoder args the assembler used for every scene
         (_video_encoder_args / _audio_encoder_args), scaled+padded to the exact
         WxH (no distortion, letterbox only if aspect mismatched) at SAR 1:1.
      2. Concat [cover, video] via the concat DEMUXER with `-c copy` (stream copy):
         the main video is NOT re-encoded, only the tiny head clip is. Identical
         codec params (h264/High/level/yuv420p/SAR 1:1 + aac/48k/stereo) make the
         stream copy valid.

    Sync: the cover carries its OWN silent audio, so video AND audio are both held by
    the cover duration — the narration is delayed as a whole, never drifts. Added
    duration = _COVER_HOLD_FRAMES video frames (~33ms @30fps for the default) plus
    AAC-quantized silence (~55ms of container time for a 1-frame default). The main
    body is a single stream-copied segment, so its internal A/V sync is unchanged.

    No-op (returns video_path unchanged) when no cover was chosen, the file is
    missing/invalid, or on any ffmpeg failure — never regresses the natural-first-
    frame path and never fails a finished render over a cosmetic poster.
    """
    src = (cover_image_path or "").strip()
    if not src or not os.path.isfile(src) or not os.path.isfile(video_path):
        return video_path
    try:
        hold = _COVER_HOLD_FRAMES / float(fps or 30)
        # Work dir on the SAME volume as the output so the final os.replace is an
        # atomic same-filesystem move (the output lives on E:, tempfile default is C:).
        work = tempfile.mkdtemp(prefix="cover_", dir=os.path.dirname(video_path) or ".")
        try:
            cover_clip = os.path.join(work, "cover_head.mp4")
            vf = (f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                  f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps}[v]")
            _run_ffmpeg(
                ["-loop", "1", "-framerate", str(fps), "-i", src,
                 "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                 "-filter_complex", vf, "-map", "[v]", "-map", "1:a",
                 "-t", f"{hold:.6f}", "-r", str(fps),
                 *_video_encoder_args(fps=fps), *_audio_encoder_args(), cover_clip],
                step="cover first frame",
            )
            list_path = os.path.join(work, "cover_concat.txt")
            with open(list_path, "w", encoding="utf-8") as fh:
                fh.write(f"file '{cover_clip.replace(chr(92), '/')}'\n")
                fh.write(f"file '{video_path.replace(chr(92), '/')}'\n")
            baked = os.path.join(work, "baked.mp4")
            _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy",
                         "-movflags", "+faststart", baked], step="cover concat")
            os.replace(baked, video_path)  # atomic swap into place
        finally:
            shutil.rmtree(work, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 — cosmetic poster; never fail a finished render
        print(f"[assemble] cover first-frame bake skipped ({src}): {exc}")
    return video_path


def _probe_duration(path: str) -> float:
    # Force UTF-8: ffprobe can echo the (Vietnamese) path in captured stderr; on
    # Windows text=True decodes with cp1252 and a bad byte raises UnicodeDecodeError.
    proc = subprocess.run(
        [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    try:
        return float(proc.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def _probe_media(path: str) -> dict:
    """Probe duration + the first video stream's width/height/fps (ffprobe). Used to
    rebuild the download_source_video result for a CACHE HIT so it matches a fresh
    download's contract, and to pick the H.264 level for a passthrough-trim cut (which
    needs the SOURCE cadence, not the pipeline's 30fps — see _h264_level_for).
    Best-effort: returns 0/None on any probe failure."""
    out = {"durationS": _probe_duration(path), "width": None, "height": None, "fps": None}
    # Force UTF-8 (see _probe_duration): Vietnamese path may appear in stderr.
    proc = subprocess.run(
        [FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate", "-of", "csv=p=0:s=x", path],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    parts = (proc.stdout or "").strip().split("x")
    try:
        out["width"], out["height"] = int(parts[0]), int(parts[1])
    except (ValueError, IndexError, AttributeError):
        pass
    # r_frame_rate is a rational ("60/1", "30000/1001"); "0/0" for a still/odd stream.
    try:
        num, den = parts[2].split("/")
        out["fps"] = (float(num) / float(den)) if float(den) else None
    except (ValueError, IndexError, ZeroDivisionError, AttributeError):
        pass
    return out


def _has_audio(path: str) -> bool:
    """True if the file has at least one audio stream (ffprobe)."""
    # Force UTF-8 (see _probe_duration): Vietnamese path may appear in stderr.
    proc = subprocess.run(
        [FFPROBE_BIN, "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", path],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    return bool((proc.stdout or "").strip())


def _has_video(path: str) -> bool:
    """True if the file has at least one video stream (ffprobe). A cut whose range
    overshot the source end can yield an audio-only clip with no video stream; the
    footage filtergraph then fails on '[0:v] matches no streams'. Probe up front."""
    # Force UTF-8 (see _probe_duration): Vietnamese path may appear in stderr.
    proc = subprocess.run(
        [FFPROBE_BIN, "-v", "error", "-select_streams", "v",
         "-show_entries", "stream=index", "-of", "csv=p=0", path],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    return bool((proc.stdout or "").strip())


def _scene_filter(width: int, height: int, fps: int, dur: float, caption: str | None, work: str, idx: int) -> str:
    """Build the per-scene video filter: cover-fit → Ken Burns zoom → caption.

    KEN BURNS STABILITY (I7 — "frame jumps 5 times"): zoompan is fed a SINGLE still
    image. The previous expression used the STATEFUL accumulator z='min(zoom+inc,..)'
    combined with d=frames, which is the documented zoompan stutter source on a single
    input frame: zoompan's `zoom` accumulator is per-INPUT-frame and the d=frames
    re-trigger interacts badly with the s= rescale, producing periodic position/zoom
    JUMPS (a handful of visible "snaps" across the scene). The robust fix is to drive
    the zoom DETERMINISTICALLY from the OUTPUT frame counter `on` — z = 1 + k*(on/N) —
    so every output frame's zoom is a pure function of its index with NO accumulator to
    drift or reset. We also UPSCALE the source ~4x before zoompan: zoompan rounds the
    crop origin to integer source pixels, so on a 1:1 source a slow zoom snaps between
    integer crop positions (the residual micro-jump); a large source makes each step
    sub-pixel and the pan/zoom continuous. d is set to exactly `frames` and the input
    is looped at `fps` (see the -loop/-framerate input args in _encode_image_scene) so
    zoompan receives a steady frame cadence instead of one frame stretched to d."""
    frames = round(dur * fps) + fps  # +1s buffer; -t trims to the audio length
    # Total zoom travel (1.00 -> 1.10) spread linearly over the output frames via `on`.
    # `on` is the 0-based output frame index; on/(frames-1) goes 0..1 across the scene.
    _denom = max(frames - 1, 1)
    f = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,"
        # Upscale 4x so the integer-pixel crop origin zoompan computes is sub-pixel
        # relative to the displayed frame → no integer-step snap during the slow zoom.
        f"scale={width*4}:{height*4},"
        f"zoompan=z='1+0.10*on/{_denom}'"
        f":d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps={fps}"
    )
    if caption and os.path.isfile(CAPTION_FONT):
        # #2: target a SINGLE line. Use a WIDER text area (safe-margin ~5% each side)
        # and pick the largest font (down to a sane floor) at which the caption fits
        # one line; only wrap if it still doesn't fit even at the min font.
        cap = " ".join((caption or "").split())  # collapse stray whitespace/newlines
        margin = max(24, int(width * 0.05))            # left+right safe margin
        usable_px = max(1, width - 2 * margin)
        # Be Vietnam Pro Bold averages ~0.56*fontsize px per glyph. Solve for the
        # font that makes the whole caption fit on one line, clamped to [floor, base].
        base_fs = max(28, width // 24)                 # previous size (45 @1080) = upper bound
        floor_fs = max(24, width // 34)                 # smallest readable size (~31 @1080)
        n = max(1, len(cap))
        fit_fs = int(usable_px / (0.56 * n)) if n else base_fs
        fs = max(floor_fs, min(base_fs, fit_fs))
        # Wrap only if even at this font the line overflows (rare, very long caption).
        max_chars = max(8, int(usable_px / (0.56 * fs)))
        if len(cap) <= max_chars:
            wrapped = cap                               # single line — the common case
        else:
            wrapped = "\n".join(textwrap.wrap(cap, width=max_chars) or [cap])
        txt_path = os.path.join(work, f"cap_{idx}.txt")
        with open(txt_path, "w", encoding="utf-8") as fh:
            fh.write(wrapped)
        f += (
            f",drawtext=fontfile='{_ff_filter_path(CAPTION_FONT)}'"
            f":textfile='{_ff_filter_path(txt_path)}'"
            f":fontcolor=white:fontsize={fs}:line_spacing=8"
            f":box=1:boxcolor=black@0.55:boxborderw=24"
            f":x=(w-text_w)/2:y=h-text_h-{max(80, height // 12)}"
        )
    return f + "[v]"


class AssembleScene(BaseModel):
    scene: int
    imagePath: str
    audioPath: str
    caption: str | None = None
    durationS: float | None = None


class AssembleRequest(BaseModel):
    page: str | None = None
    title: str = "video"
    scenes: list[AssembleScene]
    bgmPath: str | None = None
    bgmVolume: float = 0.18
    width: int = 1080
    height: int = 1920
    fps: int = 30
    captions: bool = True
    sourceName: str | None = None
    sourceLink: str | None = None
    sourceLogo: str | None = None       # path to source channel avatar (credit slate)
    sourceHandle: str | None = None      # e.g. "@KezzaZomboid"
    # OWN-page branding handle for the final black outro card (e.g. "@giaithichmoithu").
    # None/"" -> no outro card. Distinct from sourceHandle, which CREDITS the source.
    outroHandle: str | None = None
    addCredit: bool = True               # append the source-credit slate?
    # Accepted for contract symmetry with footage mode. Image mode has no source
    # audio (scenes are SDXL stills + VO), so this is a no-op here.
    srcAudioVolume: float = 0.0
    outDir: str | None = None
    videoId: int | None = None  # when set, the output filename is suffixed " (v<id>)" so re-renders don't overwrite each other


@router.post("/generate/assemble")
def assemble(req: AssembleRequest):
    if not req.scenes:
        raise HTTPException(422, "Provide at least one scene.")
    for s in req.scenes:
        if not os.path.isfile(s.imagePath):
            raise HTTPException(422, f"scene {s.scene}: image not found: {s.imagePath}")
        if not os.path.isfile(s.audioPath):
            raise HTTPException(422, f"scene {s.scene}: audio not found: {s.audioPath}")

    out_dir = req.outDir or os.path.join(CONTENT_OUTPUT_ROOT, req.page or "default", "video")
    os.makedirs(out_dir, exist_ok=True)
    safe_title = "".join(c for c in req.title if c.isalnum() or c in (" ", "-", "_")).strip() or "video"
    # Suffix the per-video id (unique per render) so re-rendering the SAME source
    # (same title) with different settings produces a DISTINCT file instead of
    # silently overwriting the previous output.
    suffix = f" (v{req.videoId})" if req.videoId else ""
    out_path = os.path.join(out_dir, f"{safe_title}{suffix}.mp4")

    with tempfile.TemporaryDirectory() as work:
        # 0) Scene-JOIN silence normalization — same pass as footage mode (see
        #    _normalize_scene_joins). Image scenes concat as the same hard cut, so the
        #    inter-scene pause is the wav edges here too. Runs before durs so the
        #    progress weights and clip lengths (-t dur) match the trimmed audio.
        _normalize_scene_joins(req.scenes, work)

        # Per-scene durations up front so progress can weight each step by its
        # output seconds (longer scenes take proportionally longer to encode).
        durs: list[float] = []
        for s in req.scenes:
            d = s.durationS or _probe_duration(s.audioPath)
            if d <= 0:
                raise HTTPException(422, f"scene {s.scene}: could not determine audio duration")
            durs.append(d)
        prog = _make_assemble_progress(req.scenes, durs, req.bgmPath, req.addCredit,
                                       req.sourceLogo, req.sourceHandle, req.sourceName)

        # 1) one clip per scene (image + Ken Burns + caption + its audio), encoded
        #    concurrently with a bounded pool (see _scene_encode_workers); the clips
        #    are returned IN SCENE ORDER so the concat list below stays deterministic.
        def _encode_image_scene(p):
            s, dur = p
            vf = _scene_filter(req.width, req.height, req.fps, dur, s.caption if req.captions else None, work, s.scene)
            clip = os.path.join(work, f"scene_{s.scene:03d}.mp4")
            _run_ffmpeg(
                [
                    # Loop the still at the target fps so zoompan receives a STEADY
                    # frame cadence (one fresh input frame per output frame) instead of
                    # a single frame stretched to d=frames. With the still fed once,
                    # zoompan's d= re-trigger is what made the Ken Burns "jump" (I7);
                    # a looped, framerate-explicit input removes that re-trigger.
                    "-loop", "1", "-framerate", str(req.fps), "-i", s.imagePath,
                    "-i", s.audioPath,
                    "-filter_complex", vf,
                    "-map", "[v]", "-map", "1:a",
                    "-t", f"{dur:.3f}",
                    "-r", str(req.fps),
                    *_video_encoder_args(fps=req.fps),
                    *_audio_encoder_args(),
                    clip,
                ],
                step=f"scene {s.scene}",
            )
            return clip

        items = [(dur, dur, (s, dur)) for s, dur in zip(req.scenes, durs)]
        clips: list[str] = prog.run_parallel(items, _encode_image_scene, _scene_encode_workers())

        # 2-4) credit slate + concat + optional bgm (shared with footage mode)
        _finish_video(
            work, clips, out_path,
            width=req.width, height=req.height, fps=req.fps,
            source_name=req.sourceName, source_link=req.sourceLink,
            bgm_path=req.bgmPath, bgm_volume=req.bgmVolume,
            logo_path=req.sourceLogo, handle=req.sourceHandle, add_credit=req.addCredit,
            outro_handle=req.outroHandle,
            prog=prog,
        )

    total = _probe_duration(out_path)
    return {
        "videoPath": out_path,
        "url": _media_url(out_path),
        "durationS": round(total, 2),
        "scenes": len(req.scenes),
        "width": req.width,
        "height": req.height,
    }


def _finish_video(work, clips, out_path, *, width, height, fps, source_name, source_link,
                  bgm_path, bgm_volume, logo_path=None, handle=None, add_credit=True, prog=None,
                  outro_handle=None):
    """Append a source-credit slate, concat all clips, mix optional bgm. Shared
    by image- and footage-mode assembly (clips must share codec params).

    Credit slate (3s, black): the source channel's LOGO centered on one row and
    "/@handle" centered on a second row below it. Skipped entirely when
    add_credit is False or there's nothing to credit.

    Outro card (owner request 2026-07-29): when `outro_handle` is set, a FINAL black card
    showing ONLY that handle in white, centered, is appended after the credit slate — the
    page's own branding sign-off, silent (anullsrc). It is INDEPENDENT of add_credit: the
    source credit is a copyright obligation, this is the owner's own handle, so turning the
    source credit off must not remove it. Empty/None -> no card at all (a page whose handle
    the owner has not provided stays unbranded rather than guessing one).

    `prog` (an _AssembleProgress) spends its slate/concat/bgm weights as these
    steps run, so the overall percent keeps advancing through the tail. When None
    (a no-op controller is always passed by the assemble fns), step() just runs fn.
    """
    # Run an ffmpeg sub-step under the progress controller (or plainly if none).
    def _do(weight, dur, fn):
        if prog is not None:
            prog.step(weight, dur, fn)
        else:
            fn()

    # Total video seconds = the controller's scene weight (= concat/bgm output len).
    # A controller whose total includes NON-encode stages (translate_full: OCR detect,
    # karaoke whisper) can't derive it by subtraction, so it states the real output
    # length on prog.video_secs and we use that instead.
    video_secs = 1.0
    if prog is not None:
        video_secs = getattr(prog, "video_secs", None) or max(
            1.0, prog.total - getattr(prog, "slate_w", 0.0)
            - getattr(prog, "concat_w", 0.0) - getattr(prog, "bgm_w", 0.0))
    slate_w = getattr(prog, "slate_w", 0.0) if prog is not None else 0.0
    concat_w = getattr(prog, "concat_w", 0.0) if prog is not None else 0.0
    bgm_w = getattr(prog, "bgm_w", 0.0) if prog is not None else 0.0

    if add_credit and (logo_path or handle or source_name):
        slate = os.path.join(work, "zzz_credit.mp4")
        htext = (f"/{handle}" if handle else f"Nguồn: {source_name or ''}").strip()
        ctxt = os.path.join(work, "credit.txt")
        with open(ctxt, "w", encoding="utf-8") as fh:
            fh.write(htext)
        fs = max(34, width // 22)
        has_logo = bool(logo_path and os.path.isfile(logo_path))
        if has_logo:
            logo_sz = 200  # fixed 200x200 (light + consistent across aspect ratios)
            fc = (
                f"[1:v]scale={logo_sz}:{logo_sz}:force_original_aspect_ratio=decrease[lg];"
                f"[0:v][lg]overlay=(W-w)/2:(H-h)/2-{height // 12}[bg];"
                f"[bg]drawtext=fontfile='{_ff_filter_path(CAPTION_FONT)}':textfile='{_ff_filter_path(ctxt)}'"
                f":fontcolor=white:fontsize={fs}:x=(w-text_w)/2:y=h/2+{height // 22}[v]"
            )
            _do(slate_w, 3.0, lambda: _run_ffmpeg(
                [
                    "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={fps}",
                    "-i", logo_path,
                    "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                    "-filter_complex", fc, "-map", "[v]", "-map", "2:a",
                    "-t", "3", *_video_encoder_args(fps=fps),
                    *_audio_encoder_args(), slate,
                ],
                step="credit slate",
            ))
        else:
            cf = (
                f"drawtext=fontfile='{_ff_filter_path(CAPTION_FONT)}':textfile='{_ff_filter_path(ctxt)}'"
                f":fontcolor=white:fontsize={fs}:x=(w-text_w)/2:y=(h-text_h)/2"
            )
            _do(slate_w, 3.0, lambda: _run_ffmpeg(
                [
                    "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={fps}",
                    "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                    "-vf", cf if os.path.isfile(CAPTION_FONT) else "null",
                    "-t", "3", *_video_encoder_args(fps=fps),
                    *_audio_encoder_args(), slate,
                ],
                step="credit slate",
            ))
        clips.append(slate)

    # OUTRO CARD — black frame, ONLY the page's own handle in white, centered. Same encoder
    # args as every other clip so the concat below stays a `-c copy` stream-copy.
    otext = (outro_handle or "").strip()
    if otext:
        outro = os.path.join(work, "zzzz_outro.mp4")
        otxt = os.path.join(work, "outro.txt")
        with open(otxt, "w", encoding="utf-8") as fh:
            fh.write(otext)
        ofs = max(40, width // 16)   # a touch larger than the credit slate: this is branding
        ovf = (
            f"drawtext=fontfile='{_ff_filter_path(CAPTION_FONT)}':textfile='{_ff_filter_path(otxt)}'"
            f":fontcolor=white:fontsize={ofs}:x=(w-text_w)/2:y=(h-text_h)/2"
        )
        _do(0.0, CF_OUTRO_CARD_SEC, lambda: _run_ffmpeg(
            [
                "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={fps}",
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-vf", ovf if os.path.isfile(CAPTION_FONT) else "null",
                "-t", f"{CF_OUTRO_CARD_SEC:g}", *_video_encoder_args(fps=fps),
                *_audio_encoder_args(), outro,
            ],
            step="outro card",
        ))
        clips.append(outro)

    # concat (same codec params → stream copy)
    list_path = os.path.join(work, "concat.txt")
    with open(list_path, "w", encoding="utf-8") as fh:
        for c in clips:
            fh.write(f"file '{c.replace(chr(92), '/')}'\n")
    concat_out = out_path if not bgm_path else os.path.join(work, "concat.mp4")
    # +faststart (moov atom to the front) belongs ONLY on the FINAL mux. When there's
    # no bgm the concat IS the final output, so faststart here; when bgm follows, the
    # concat is an intermediate (faststart is applied on the bgm re-mux below instead).
    # +faststart is compatible with `-c copy` (pure mux, no re-encode).
    _concat_final = ["-movflags", "+faststart"] if not bgm_path else []
    _do(concat_w, video_secs, lambda: _run_ffmpeg(
        ["-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy",
         *_concat_final, concat_out], step="concat"))

    # optional background music mixed under the whole thing
    if bgm_path:
        if not os.path.isfile(bgm_path):
            raise HTTPException(422, f"bgm not found: {bgm_path}")
        _do(bgm_w, video_secs, lambda: _run_ffmpeg(
            [
                "-i", concat_out, "-stream_loop", "-1", "-i", bgm_path,
                "-filter_complex",
                f"[1:a]volume={bgm_volume}[bg];[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0[a]",
                "-map", "0:v", "-map", "[a]",
                "-c:v", "copy", *_audio_encoder_args(),
                "-movflags", "+faststart",
                out_path,
            ],
            step="bgm mix",
        ))


# --- Footage mode: original clips + Vietnamese VO + karaoke captions ------
#
# For translate/reup pages, real footage beats static SDXL images. Each scene
# is a slice of the SOURCE video, fit to 9:16 (blurred bg + centered video),
# the original audio ducked to ~15% under a Vietnamese voiceover, with word-by-
# word karaoke captions synced to the VO (whisper word timestamps drive them).

ORIG_AUDIO_VOLUME = float(os.getenv("FOOTAGE_ORIG_VOLUME", "0.15"))  # duck originals


def _ass_time(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


# Active-word highlight: pop to yellow and scale up (TikTok-style), instead of
# a fill sweep. \c uses &HBBGGRR — yellow = &H0000FFFF... in \c form: &H00FFFF&.
HL_COLOR = "&H00FFFF&"   # yellow
# % size of the spoken word. The active word scales around its OWN center without
# reflowing neighbours. The inter-word gap auto-widens to absorb this growth (see
# _group_gap), so a long active word never overlaps its neighbours at any scale;
# a smaller scale just keeps those adaptive gaps tighter. 112 = clear pop with
# minimal gap widening; raise toward 124 for a punchier pop (wider gaps on
# long-word lines). Env-tunable.
HL_SCALE = int(os.getenv("KARAOKE_HL_SCALE", "110"))   # % size of the spoken word


# Vietnamese-aware word tokenizer for the CORRECT caption text (the script's own
# narration). Splits on whitespace, keeping each whitespace-delimited token (which
# carries its diacritics) intact. We deliberately keep attached punctuation on the
# token (e.g. "gì." / "rối,") so the rendered caption reads exactly like the script.
_WORD_RE = re.compile(r"\S+")


# DISPLAY-ONLY hyphen/dash stripping for karaoke captions (owner v47 review). The
# narration KEEPS its hyphens/em-dashes (they matter for F5 TTS pronunciation), but the
# on-screen caption must show NONE. We replace ASCII hyphen "-", non-breaking/figure
# dashes, en-dash "–", em-dash "—" (and the hyphen-bullet/minus variants) with a space,
# then collapse runs of whitespace and trim. So "sub-agent" -> "sub agent" and
# "riêng — mỗi" -> "riêng mỗi". Applied ONLY to the caption text inside
# _aligned_caption_words — never to s.narration (which TTS reads).
_CAPTION_DASHES = ("-", "‐", "‑", "‒", "–", "—", "―",
                   "⁃", "−")
_CAPTION_DASH_RE = re.compile("[" + "".join(_CAPTION_DASHES) + "]")
_MULTISPACE_RE = re.compile(r"\s+")


def _strip_caption_hyphens(text: str) -> str:
    """Replace every hyphen/dash variant with a space, then collapse whitespace and
    trim. DISPLAY-ONLY — keep the original narration (with hyphens) for TTS."""
    if not text:
        return text
    return _MULTISPACE_RE.sub(" ", _CAPTION_DASH_RE.sub(" ", text)).strip()


def _tokenize_narration(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").strip())


def _tokenize_caption_glued(text: str) -> tuple[list[str], list[bool]]:
    """Tokenize the CAPTION text into display tokens AND a parallel `glue_next` list
    that records which adjacent tokens came from a single hyphen-joined compound
    (e.g. "sub-agent" -> tokens ["sub","agent"], glue [True, False]).

    Why: the caption strips hyphens to spaces so "sub-agent" shows as two words and
    matches whisper's word count (TTS speaks it as two words). But the two halves are
    ONE atomic term and MUST NOT be split across two karaoke rows (owner: never wrap
    in the middle of a joined term). The glue flag lets the row-packer keep such a
    pair on the same row. glue_next[i] == True means token i is bound to token i+1.

    A token is produced per hyphen-separated sub-part of each whitespace token; every
    sub-part except the last of the SAME original token is glued to its successor.
    Whitespace-separated tokens are never glued to each other. Tokens with no internal
    hyphen produce a single token with glue_next=False. The returned token count and
    order are IDENTICAL to _tokenize_narration(_strip_caption_hyphens(text)), so the
    downstream whisper-count matching is unchanged."""
    tokens: list[str] = []
    glue: list[bool] = []
    for ws_tok in _tokenize_narration(text or ""):
        # Split this whitespace token on any hyphen/dash variant; drop empties from
        # leading/trailing/doubled dashes (matches the strip+collapse behavior).
        parts = [p for p in _CAPTION_DASH_RE.split(ws_tok) if p]
        if not parts:
            continue
        for k, part in enumerate(parts):
            tokens.append(part)
            # Glue every part except the last to the following part (same original word).
            glue.append(k < len(parts) - 1)
    return tokens, glue


# Years the pipeline reads DIGIT-BY-DIGIT. This must mirror the text-prep expansion regex
# exactly — tts_worker._normalize_years and omnivoice_worker._expand_years_spacejoined both
# match `\b(20\d{2})\b` and NOTHING else. A 19xx year is therefore never digit-exploded; the
# engine reads it by magnitude ("1736" → "một nghìn bảy trăm ba mươi sáu"), so it must NOT be
# treated as a year here. (Caught on video 299 scene 11, "Ý tưởng graph có từ năm 1736".)
_YEAR_TOKEN_RE = re.compile(r"^20\d{2}$")
_DIGIT_RUN_RE = re.compile(r"\d+")


def _vi_number_syllables(n: int) -> int:
    """Spoken-syllable count of a Vietnamese number read by MAGNITUDE (not digit by digit).

    This is how every TTS engine in the pipeline voices a plain numeral: 427 is read
    "bốn trăm hai mươi bảy" (5 syllables), not "bốn hai bảy" (3). Only YEAR-shaped tokens
    are read digit-by-digit, and those are handled by the caller.

        n < 10    → 1                    một … chín
        n < 20    → 1 + unit             mười / mười lăm
        n < 100   → 2 + unit             hai mươi / hai mươi lăm
        n < 1000  → 2 (trăm) + (0 | 2 for "linh"+unit | syllables of the 10-99 remainder)
        n ≥ 1000  → group + "nghìn"/"triệu"/"tỷ" + remainder (APPROXIMATE, see below)

    ≥1000 is deliberately approximate: it recurses on the thousands group and adds one for
    the magnitude word, skipping the "không trăm" filler Vietnamese inserts for some 4-6
    digit numbers (2024 → "hai nghìn không trăm hai mươi tư" is 7, we score 5). That is
    accepted because this feeds a DURATION PROXY for pace measurement, not user-facing
    text, and because numbers that large are rare in this project's narration (line counts,
    ages, percentages, model numbers are all < 1000, where the count is exact)."""
    n = abs(int(n))
    if n < 10:
        return 1
    if n < 20:
        return 1 + (1 if n % 10 else 0)          # mười [+ unit]
    if n < 100:
        return 2 + (1 if n % 10 else 0)          # H mươi [+ unit]
    if n < 1000:
        rem = n % 100
        if rem == 0:
            return 2                              # H trăm
        if rem < 10:
            return 4                              # H trăm linh U
        return 2 + _vi_number_syllables(rem)      # H trăm + (10-99)
    for div in (1_000_000_000, 1_000_000, 1_000):
        if n >= div:
            head = _vi_number_syllables(n // div) + 1   # group + tỷ/triệu/nghìn
            rem = n % div
            return head + (_vi_number_syllables(rem) if rem else 0)
    return 1


def _spoken_weight(tok: str) -> int:
    """Estimate spoken syllable count for proportional whisper-timestamp anchoring.

    All-caps acronyms (AI, API, GPU): each letter ≈ one spoken syllable.
    Mixed-case compound words (ChatGPT, GitHub): roughly len//2 syllables.
    Regular words (Vietnamese + English): VOWEL-GROUP syllable count.

    BUG FIX (symptom 1, v109 caption "stall then out of sync" at scene 6): the default
    branch previously returned raw CHAR LENGTH. That over-weights a long English loanword
    — "engineering" scored 11 vs "và" 2 — but SPOKEN, "engineering" is ~4 syllables and
    "và" is 1. In the interpolation path (token count != whisper word count), the char-
    weight decides each token's time slice, so an over-weighted long word stole ~1 s of the
    caption timeline and shoved every following token late (measured: "calling," highlighted
    at 5.03 s while the audio said it at 4.30 s; then MCP/và/RAG flashed by). Using the
    vowel-group syllable count (the same metric pace uses, ~duration-proportional) removes
    that bias so the char-weighted interpolation tracks the real audio far more closely.
    A pure-consonant token (rare, e.g. a stray symbol) still floors at 1."""
    letters = [c for c in tok if c.isalpha()]
    digits = [c for c in tok if c.isdigit()]
    # DIGITS (job-290 "câu NVIDIA bị tăng tốc"): a number has NO vowel, so the vowel-group
    # default scored "2024" as ONE syllable while it is SPOKEN as four ("hai không hai tư" —
    # the worker's own _normalize_text_neutral expands years digit-by-digit for exactly this
    # reason, but whisper transcribes them back as digits). Measured on video 281 scene 8:
    # "2024." ran 720 ms and "2022," 680 ms, each credited 1 syllable, so the scene read as
    # 276 ms/syllable "slow" and the pace pass applied its maximum 10% SPEED-UP to a scene
    # that was actually on pace. One syllable per digit (plus any letters, for "H100" →
    # "hát một trăm") tracks the spoken form closely enough for a pace measure.
    if digits:
        # Checked BEFORE the acronym rule so "B100" is not scored as a 1-letter acronym.
        #
        # MAGNITUDE FIX (owner 2026-08-02). Digit-by-digit is correct ONLY for years, which
        # the pipeline deliberately reads that way. Every OTHER numeral is voiced by the
        # engine's own number normalization as ordinary Vietnamese magnitude reading, so
        # one-syllable-per-digit under-counts it badly: job 308 scene 4's "427" is spoken
        # "bốn trăm hai mươi bảy" (5 syllables) but scored 3, which made that clause measure
        # ~25% FASTER than it reads and skewed the pace correction applied to it.
        #
        # YEARS stay digit-by-digit: the TTS text-prep expands an isolated 20xx into
        # "hai không hai tư" BEFORE synthesis, and whisper then re-collapses that spoken form
        # back into the numeral "2024" when it transcribes (the job-290 measurement below is
        # exactly that case), so the token we see here really was read one digit at a time.
        year_like = [d for d in _DIGIT_RUN_RE.findall(tok) if _YEAR_TOKEN_RE.match(d)]
        if year_like and len(year_like) == len(_DIGIT_RUN_RE.findall(tok)):
            return max(1, len(digits) + len(letters))
        # Sum each digit RUN by magnitude, so "H100" → 1 letter + "một trăm" ≈ 3 and a
        # decimal/date that whisper wrote with punctuation ("5.6", "24/2") scores each side
        # separately — the spoken connector ("chấm", "tháng") arrives as its own token.
        n_syl = sum(_vi_number_syllables(int(run)) for run in _DIGIT_RUN_RE.findall(tok))
        return max(1, n_syl + len(letters))
    # All-caps acronym (AI, API, CPU, GPT …): letter count ≈ syllable count
    if letters and all(c.isupper() for c in letters) and len(letters) >= 2:
        return max(1, len(letters))
    # Mixed-case with 2+ uppercase (ChatGPT, GitHub, MacBook …): roughly half chars
    uppers = sum(1 for c in tok if c.isupper())
    if uppers >= 2:
        return max(2, len(tok) // 2)
    # Default: spoken-syllable estimate via vowel groups (duration-proportional), NOT raw
    # char length — so a long loanword no longer dominates the interpolation weight.
    return _pace_syllables(tok)


def _aligned_caption_words(narration: str | None, whisper_words: list[dict],
                           audio_dur: float, scene: int | None = None) -> list[dict]:
    """Return per-word [{start,end,word}] for the karaoke caption whose TEXT is the
    CORRECT narration (the script that was fed to TTS) and whose TIMING comes from
    whisper's word timestamps.

    Why: rendering whisper's re-transcription of the TTS audio produced garbled
    Vietnamese captions (e.g. "Agent harness" -> "Dân vật A.A.A.Harnes"). Whisper is
    accurate at WHEN a word is spoken but unreliable at WHAT it is, so we keep its
    timing and substitute the known-correct narration tokens. This makes captions
    robust to whisper accuracy entirely.

    Alignment (forced-alignment-lite): take the spoken time span from whisper's first
    word start .. last word end, then lay the N narration tokens across that span,
    each token's slice weighted by its character length so longer words get more
    time. Falls back gracefully:
      - no narration  -> whisper words verbatim (old behavior; never worse than before)
      - no whisper words but we have narration + a duration -> even split over [0,dur]
      - neither        -> []
    """
    # DISPLAY-ONLY: strip hyphens/dashes from the caption text before tokenizing, so
    # "sub-agent" shows as two words "sub agent" and a stray "—" never appears on
    # screen. This does NOT touch the narration TTS reads (caller passes the script's
    # caption/display text here; s.narration is untouched). Splitting a hyphen compound
    # into separate tokens also tends to MATCH whisper's word count better (TTS speaks
    # "sub-agent" as two words), improving the exact-count timing path below.
    tokens, glue_flags = _tokenize_caption_glued(narration)
    if not tokens:
        # No script text to show — fall back to whisper words (previous behavior).
        return list(whisper_words or [])

    # Determine the spoken span. Prefer whisper's measured word boundaries; else
    # use the audio duration; else a tiny nonzero span so timestamps stay valid.
    if whisper_words:
        span_start = float(whisper_words[0].get("start", 0.0) or 0.0)
        span_end = float(whisper_words[-1].get("end", 0.0) or 0.0)
    else:
        span_start, span_end = 0.0, float(audio_dur or 0.0)
    if span_end <= span_start:
        span_end = span_start + max(0.5, float(audio_dur or len(tokens) * 0.35))

    # If whisper produced a comparable number of words, anchor each narration token
    # to the whisper word at the same proportional index — this keeps the per-word
    # pop tightly in sync with the actual audio. Otherwise distribute by char weight
    # across the whole span (still monotonic, never drifting off the audio).
    weights = [_spoken_weight(t) for t in tokens]
    total_w = sum(weights)
    out: list[dict] = []
    nw = len(whisper_words) if whisper_words else 0

    # Diagnostic: record which alignment branch this scene takes. EXACT (whisper
    # word count == caption tokens) is the perfectly-synced 1:1 map; INTERP snaps
    # each token to a REAL whisper onset via monotonic alignment (tracks the actual
    # voice speed); SPLIT is the last-resort even char-weight fallback when whisper
    # produced <2 words. Persisted so intermittent CTC failures are visible in the log.
    if whisper_words and nw == len(tokens):
        _branch = "EXACT"
    elif whisper_words and nw >= 2:
        _branch = "INTERP"
    else:
        _branch = "SPLIT"
    log.info("karaoke align: branch=%s cap_tokens=%d whisper_words=%d scene=%s",
             _branch, len(tokens), nw, scene if scene is not None else "?")

    if whisper_words and nw == len(tokens):
        # EXACT word-count match: whisper transcribed the same number of words the
        # script has (the common case for clean TTS narration), and in the same
        # order. Map token i DIRECTLY to whisper word i — exact per-word timing with
        # ZERO char-weight drift. The proportional branch below mis-anchors by ±1
        # word here (e.g. "harness"→"là", and the active word's end borrows the next
        # word's start → a ~1.2s held highlight). Direct mapping eliminates both.
        for i, tok in enumerate(tokens):
            st = float(whisper_words[i].get("start", span_start) or span_start)
            en = float(whisper_words[i].get("end", st) or st)
            if en <= st:
                en = st + 0.12
            out.append({"start": round(st, 3), "end": round(en, 3), "word": tok})
    elif whisper_words and nw >= 2:
        # Token count DIFFERS from whisper's word count (CTC failed for this scene and
        # whisper re-transcribed it, mis-segmenting loanwords/numbers), so token i has
        # no guaranteed 1:1 whisper word. Rather than a char-weight ESTIMATE — which
        # ignores the real audio and therefore drifts differently every render as the
        # OmniVoice speed changes (measured up to ~1.28 s lag on loanword scenes) — we
        # do a MONOTONIC token<->whisper-word alignment (Needleman-Wunsch) on a folded
        # romanization, then give each caption token the onset of the REAL whisper word
        # it aligns to. This tracks the actual voice speed regardless of how many words
        # whisper heard. Caption tokens with no whisper match (insertions) are filled by
        # linear interpolation between their nearest aligned neighbours. Romanization is
        # diacritic-folding (unicodedata, stdlib) mirroring the worker's unidecode step
        # — no extra dependency. Display-only; does NOT touch audio or the EXACT branch.
        import unicodedata as _ud
        import difflib as _difflib

        def _rom(s: str) -> str:
            s = (s or "").replace("đ", "d").replace("Đ", "D")
            n = _ud.normalize("NFKD", s)
            n = "".join(c for c in n if not _ud.combining(c))
            return "".join(c for c in n.lower() if c.isalnum())

        tok_r = [_rom(t) for t in tokens]
        wsp_r = [_rom(str(w.get("word", ""))) for w in whisper_words]
        wsp_start = [float(w.get("start", span_start) or span_start) for w in whisper_words]
        wsp_end = [float(w.get("end", s0) or s0) for w, s0 in zip(whisper_words, wsp_start)]

        def _sim(a: str, b: str) -> float:
            if not a or not b:
                return 0.0
            if a == b:
                return 1.0
            return _difflib.SequenceMatcher(None, a, b).ratio()

        # Needleman-Wunsch: maximize summed similarity of aligned pairs under a
        # monotonic (order-preserving) constraint. GAP is the cost of skipping a
        # caption token or a whisper word; MATCH_BIAS keeps weak matches preferable
        # to a double gap so tokens still anchor to their nearest real onset.
        n, m = len(tokens), nw
        GAP = -0.30
        MATCH_BIAS = -0.20
        NEG = float("-inf")
        dp = [[NEG] * (m + 1) for _ in range(n + 1)]
        bt = [[0] * (m + 1) for _ in range(n + 1)]  # 0=diag 1=up(tok gap) 2=left(wsp gap)
        dp[0][0] = 0.0
        for i in range(1, n + 1):
            dp[i][0] = dp[i - 1][0] + GAP; bt[i][0] = 1
        for j in range(1, m + 1):
            dp[0][j] = dp[0][j - 1] + GAP; bt[0][j] = 2
        for i in range(1, n + 1):
            for j in range(1, m + 1):
                diag = dp[i - 1][j - 1] + _sim(tok_r[i - 1], wsp_r[j - 1]) + MATCH_BIAS
                up = dp[i - 1][j] + GAP
                left = dp[i][j - 1] + GAP
                best = diag; move = 0
                if up > best:
                    best = up; move = 1
                if left > best:
                    best = left; move = 2
                dp[i][j] = best; bt[i][j] = move
        # Traceback -> for each caption token, the aligned whisper index or None.
        match_of: list[int | None] = [None] * n
        i, j = n, m
        while i > 0 or j > 0:
            mv = bt[i][j]
            if i > 0 and j > 0 and mv == 0:
                match_of[i - 1] = j - 1; i -= 1; j -= 1
            elif i > 0 and (j == 0 or mv == 1):
                i -= 1
            else:
                j -= 1
        # KARAOKE_WORD_OFFSET (default 0): nudge each matched anchor forward by whole
        # whisper words for manual correction; harmless no-op at 0.
        kwo = int(round(float(os.getenv("KARAOKE_WORD_OFFSET", "0"))))
        starts: list[float] = [0.0] * n
        for i in range(n):
            j = match_of[i]
            if j is not None:
                jj = min(nw - 1, max(0, j + kwo))
                starts[i] = wsp_start[jj]
        # Fill unmatched (insertion) tokens by linear interpolation between the
        # nearest aligned anchors so every token gets a monotonic, real-onset-derived
        # start (mirrors the worker's CTC gap-fill for a consistent, drift-free grid).
        anchors = [i for i in range(n) if match_of[i] is not None]
        for i in range(n):
            if match_of[i] is not None:
                continue
            prev = max((a for a in anchors if a < i), default=None)
            nxt = min((a for a in anchors if a > i), default=None)
            if prev is not None and nxt is not None and nxt > prev:
                frac = (i - prev) / (nxt - prev)
                starts[i] = starts[prev] + frac * (starts[nxt] - starts[prev])
            elif prev is not None:
                starts[i] = starts[prev]
            elif nxt is not None:
                starts[i] = span_start + (starts[nxt] - span_start) * (i / max(1, nxt))
            else:
                starts[i] = span_start + (span_end - span_start) * (i / max(1, n))
        # Enforce monotonic starts, then set each end to the next token's start (or the
        # aligned whisper word end / span_end for the last), so the active highlight
        # holds until the next word actually begins.
        for i in range(1, n):
            if starts[i] < starts[i - 1]:
                starts[i] = starts[i - 1]
        for i, tok in enumerate(tokens):
            st = starts[i]
            if i + 1 < n:
                en = max(st, starts[i + 1])
            else:
                j = match_of[i]
                en = wsp_end[j] if j is not None else span_end
            if en <= st:
                en = st + 0.12
            out.append({"start": round(st, 3), "end": round(en, 3), "word": tok})
    else:
        # Even-ish split by char weight across the span.
        dur = span_end - span_start
        cum = 0.0
        for i, tok in enumerate(tokens):
            st = span_start + dur * (cum / total_w)
            cum += weights[i]
            en = span_start + dur * (cum / total_w)
            if en <= st:
                en = st + 0.12
            out.append({"start": round(st, 3), "end": round(en, 3), "word": tok})

    # Attach the atomic-term glue flag onto each produced word (index-aligned with
    # `tokens`). glue_next[i] == True means word i is bound to word i+1 (they came from
    # one hyphen-joined compound like "sub-agent") and the row-packer must keep them on
    # the SAME karaoke row. Every branch above emits exactly one `out` entry per token
    # in order, so out[i] <-> glue_flags[i]. Defensive: only set when lengths align.
    if len(out) == len(glue_flags):
        for i in range(len(out)):
            out[i]["glue_next"] = bool(glue_flags[i])

    # Guarantee monotonic, non-overlapping starts so the karaoke pop advances cleanly.
    for i in range(1, len(out)):
        if out[i]["start"] < out[i - 1]["start"]:
            out[i]["start"] = out[i - 1]["start"]
        if out[i]["end"] < out[i]["start"]:
            out[i]["end"] = out[i]["start"] + 0.12

    # Enforce a minimum visible highlight slot per word. Whisper word timestamps
    # saturate at the last whisper word when the narration has more tokens than
    # whisper produced — all end-of-sentence tokens get the SAME start time and
    # _build_karaoke_ass's dedup step drops all but the last, causing the
    # karaoke to "flash" through the final words. Spreading them by MIN_WORD_S
    # ensures every token gets its own distinct, visible slot.
    MIN_WORD_S = float(os.getenv("KARAOKE_MIN_WORD_S", "0.08"))
    for i in range(1, len(out)):
        if out[i]["start"] - out[i - 1]["start"] < MIN_WORD_S:
            out[i]["start"] = round(
                min(span_end - 0.02, out[i - 1]["start"] + MIN_WORD_S), 3
            )
        if out[i]["end"] < out[i]["start"] + MIN_WORD_S:
            out[i]["end"] = round(
                min(span_end, out[i]["start"] + MIN_WORD_S), 3
            )
    # Compress large inter-word gaps to reduce the long held-highlight silences
    # that appear before year numbers ("hai không hai sáu") and after words with a
    # long TTS trailing pause (e.g. "chính"). The previous word stays highlighted
    # across the whole gap, so a 0.6-0.7s TTS pause reads as the karaoke "sticking".
    # Compress the excess so the next word pops sooner (it then leads the audio
    # slightly during the pause, which reads smoother than a long static hold).
    _GAP_THRESHOLD = float(os.getenv("KARAOKE_GAP_THRESHOLD", "0.25"))
    _GAP_FACTOR = float(os.getenv("KARAOKE_GAP_FACTOR", "0.30"))
    # Hard cap: no matter how long the TTS silence, the held gap never exceeds this.
    # The longest TTS pauses (before year numbers like "2026" / after clause-final
    # words like "chính:") all FLATLINE at this cap, so it is the single knob that
    # controls the residual "stick" the owner still feels there. Measured on job 41:
    # scene 4 has a real 0.64s pause before the year — at the old 0.30 cap that held
    # the previous word 0.256s after its audio ended; 0.18 trims that to ~0.18s
    # (snappier) while keeping a natural micro-beat (0 would look like a cut). Only
    # gaps > _GAP_THRESHOLD are touched, so normal word-to-word rhythm is unaffected
    # — this compresses ONLY the long-pause boundaries the owner pointed at.
    _GAP_MAX = float(os.getenv("KARAOKE_GAP_MAX", "0.18"))
    # RUSH GUARD (owner 2026-07-05: "will NOT accept rush"). The pull-forward below made the
    # NEXT word POP EARLY during a long TTS pause ("it then leads the audio slightly") — that
    # early lead IS the rush the owner rejects, and after our durable non-punct gap removal
    # (tts_worker) the long mid-phrase pauses are already gone, so the only remaining >0.25 s
    # gaps are REAL punctuation beats where the highlight SHOULD hold on the last word, not
    # jump ahead. So the pull-forward is DISABLED by default. Each caption word now stays on
    # whisper's measured onset of the FINAL (post-shape, post-pace) audio → no lead, no rush.
    # Set KARAOKE_GAP_RUSH=1 to restore the legacy pull-forward if ever wanted.
    _rush = os.getenv("KARAOKE_GAP_RUSH", "0").strip().lower() not in ("0", "off", "false", "no")
    if _rush:
        for i in range(1, len(out)):
            gap = out[i]["start"] - out[i - 1]["end"]
            if gap > _GAP_THRESHOLD:
                out[i]["start"] = round(out[i - 1]["end"] + min(gap * _GAP_FACTOR, _GAP_MAX), 3)
                if out[i]["end"] < out[i]["start"] + MIN_WORD_S:
                    out[i]["end"] = round(out[i]["start"] + MIN_WORD_S, 3)
    return out


# Single-line karaoke chunk size. The owner wants a FIXED single line that does
# not jump: as time advances the current line is CLEARED and the next short chunk
# shown in the SAME position (no stacking, no wrap to 2-3 lines).
#
# OWNER v47 (revised): pack a CONSISTENT 5 words per row, EVEN ACROSS punctuation. A
# line keeps packing until it hits KARAOKE_MAX_WORDS (5) OR the per-row pixel-width
# budget (max_px = usable_px * 100/HL_SCALE, in libass-rendered units via
# CAPTION_LIBASS_WFACTOR — which guarantees the zoomed active word still fits the
# margins). It does NOT break at a comma/period/clause mark anymore (that produced
# uneven "1 word then 4" lines). Punctuation stays attached to its word (still shown),
# it just no longer ends the line. Width is the HARD limit; if 5 wide words wouldn't
# fit the row, the width budget breaks earlier (fine). Env-tunable.
KARAOKE_MAX_WORDS = int(os.getenv("KARAOKE_MAX_WORDS", "5"))


def _karaoke_pin_top_y(width: int, height: int, clip_path: str) -> int | None:
    """PORTRAIT karaoke placement (owner request 2026-07-29): on a 9:16 output the source
    footage is scaled to the frame WIDTH and centered vertically over the blurred bg
    (see _footage_scene_clip: fg_h = width*srcH/srcW, overlay=(H-h)/2). The karaoke line
    should sit IMMEDIATELY BELOW that footage band: top of the text = footage bottom +
    KARAOKE_PIN_PAD_TOP px (default 10).

    Returns the absolute Y for the caption's TOP edge, or None to keep the legacy
    bottom-anchored position when pinning doesn't apply:
      - landscape/square output (height <= width) — there is no below-video band;
      - probe failure / no video stream;
      - the footage fills (nearly) the whole frame — e.g. a portrait source — so there is
        no room below it: pinning would push the line off-screen. The guard reserves the
        rendered line box (~fontsize, libass scales the font so the OS/2 win-height equals
        Fontsize) grown by the active-word pop (HL_SCALE) plus the outline.
    Env: KARAOKE_PIN_BELOW=0 restores the legacy position; KARAOKE_PIN_PAD_TOP tunes the
    padding. Only the footage path calls this — translate_full keeps its explicit
    cover-band margin_v and image mode has no footage band."""
    if height <= width:
        return None
    if os.getenv("KARAOKE_PIN_BELOW", "1").strip().lower() in ("0", "off", "false", "no"):
        return None
    try:
        pad = int(os.getenv("KARAOKE_PIN_PAD_TOP", "10"))
    except ValueError:
        pad = 10
    m = _probe_media(clip_path)
    if not m.get("width") or not m.get("height"):
        return None
    fg_h = width * m["height"] / m["width"]      # scale=width:-2 keeps AR (±1px irrelevant)
    if fg_h >= height:                            # source taller than the frame -> no band
        return None
    top = int(round((height + fg_h) / 2.0)) + pad
    # Overflow guard: the grown line box + outline must still fit above the frame edge.
    line_box = _karaoke_fontsize(width, height) * (HL_SCALE / 100.0) + 12
    if top + line_box > height - 8:
        return None
    return top


def _build_karaoke_ass(words: list[dict], width: int, height: int, work: str, idx: int,
                       margin_v: int | None = None, time_offset: float = 0.0,
                       return_events: bool = False,
                       clamp_window: tuple[float, float] | None = None,
                       pin_top_y: int | None = None):
    """Write an .ass that shows ONE fixed, centered single line at a time.

    Owner contract: a fixed single line that does NOT jump. As the VO advances, the
    current line is cleared and the next short chunk appears in the SAME position —
    never stacking to 2-3 lines, never wrapping, never moving vertically. Within a
    chunk the spoken word still POPS (yellow + grows) so it reads as karaoke, but
    every chunk is small enough to fit one row, and at any instant EXACTLY ONE
    Dialogue event is on screen (events are contiguous and non-overlapping), which
    is what prevents libass from stacking two captions vertically.

    Times are relative to 0 (each scene's caption file). Returns the .ass path, or
    None if there are no words.

    `clamp_window` (translate_full merge): an ABSOLUTE (start, end) window — after the
    time_offset shift, every emitted event is clamped into it and any event lying fully
    outside is dropped. This guarantees a scene's captions never bleed past its own
    [start, end) into the next scene (the trailing tail-hold on the last group could
    otherwise extend past the boundary and put TWO lines on screen at the seam). None =
    no clamp (footage/image single-scene path — behavior unchanged).

    `pin_top_y` (portrait footage, see _karaoke_pin_top_y): absolute Y for the caption's
    TOP edge. Words anchor \\an8 (top-center) at that Y instead of the legacy \\an2 bottom
    anchor — the top edge stays fixed even while the active word grows (the pop expands
    DOWNWARD into the empty blurred band, so the "padding top" is exact). None = legacy.
    """
    if not words:
        return None
    # time_offset (translate_full): shift every word onto the ABSOLUTE output timeline
    # (each soft-anchored VN clip is placed at plan[i].start). All internal timing is
    # relative-comparison only, so a constant shift of the inputs is safe.
    if time_offset:
        words = [{**w, "start": float(w["start"]) + time_offset,
                  "end": float(w["end"]) + time_offset} for w in words]
    # Section D: karaoke font is DOUBLED at 9:16 (all modes). _karaoke_fontsize handles
    # the aspect check; everything downstream (row budget, Pillow width, libass width-
    # factor, gaps) derives from this fontsize, so spacing stays correct at 2x.
    fontsize = _karaoke_fontsize(width, height)
    # Fixed vertical anchor. Alignment 2 = bottom-center; MarginV is the SAME for
    # every event, so the (single) line sits at one fixed Y for the whole scene.
    # `margin_v` override (translate_full): position the line OVER the cover band so
    # the running text sits where the source's own karaoke was (default = footage bottom).
    margin_v = max(120, height // 8) if margin_v is None else int(margin_v)
    margin = max(24, int(width * 0.037))
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        # WrapStyle 2 = never auto-wrap (only an explicit \\N breaks a line, and we
        # emit none). Combined with the width budget below, a line stays single-row.
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # White text, thick black outline; bottom-center. Font is bundled (deterministic).
        f"Style: Default,{CAPTION_FONT_FAMILY},{fontsize},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
        f"-1,0,0,0,100,100,0,0,1,5,2,2,{margin},{margin},{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    usable_px = max(1, width - 2 * margin)
    # Fallback estimate — used only when Pillow is unavailable.
    # 0.60 is a realistic average across Vietnamese + English uppercase; the old
    # 0.45 underestimated uppercase ASCII by ~64% (GPU: est 56px, actual 93px).
    avg_glyph = 0.60 * fontsize

    # Accurate per-word pixel width via Pillow + the real font file.
    # Replaces the flat len×ratio heuristic whose error ranged from ~5% (narrow
    # function words) to 64% (English uppercase like "GPU"), causing inconsistent
    # inter-word gaps: crowded technical terms, overly wide plain-Vietnamese groups.
    _font = None
    try:
        from PIL import ImageFont as _ImageFont
        if os.path.isfile(CAPTION_FONT):
            _font = _ImageFont.truetype(CAPTION_FONT, fontsize)
    except Exception:
        pass

    # Pillow measures at the NOMINAL fontsize; libass renders this font ~0.65x
    # narrower (inflated OS/2 win-metrics — see _libass_width_factor). Scale every
    # measured width by that factor so the reserved per-word box equals what libass
    # actually draws, collapsing the inter-word gap to exactly `space_w`.
    _wfac = CAPTION_LIBASS_WFACTOR

    def _word_px(tok: str) -> float:
        if _font is not None:
            try:
                return float(_font.getlength(tok)) * _wfac
            except Exception:
                pass
        return len(tok) * avg_glyph * _wfac

    # Natural inter-word gap = the font's real space-glyph advance, in the SAME
    # (libass-rendered) units as the word widths. Words are positioned by their own
    # bounding boxes (no rendered space char between them), so the visible gap is
    # purely `space_w`; using the rendered space advance makes karaoke spacing look
    # like normal typed text — uniform across every word, scaling with fontsize.
    _natural_space = 0.24 * fontsize * _wfac  # fallback when Pillow absent
    if _font is not None:
        try:
            _natural_space = float(_font.getlength(" ")) * _wfac
        except Exception:
            pass

    # Pixel budget per row: shrink by HL_SCALE so the grown active word never
    # overflows the safe area.
    max_px = usable_px * (100.0 / HL_SCALE)

    # Row-ending priority (owner v47 refined):
    #   PRIORITY 1 — end the row at the end of a SENTENCE. Only SENTENCE-TERMINAL marks
    #     break: "." "!" "?" "…" (and a literal "..." ellipsis). The next sentence starts
    #     on a fresh row, even if the current row had fewer than 5 words.
    #   PRIORITY 2 — else end by the per-row PIXEL-WIDTH budget or the KARAOKE_MAX_WORDS
    #     (5) cap, whichever hits first.
    # CLAUSE marks "," ";" ":" do NOT break — they keep packing onto the same row (fixes
    # the earlier "1 word then 4" comma orphans). Punctuation stays attached to its word
    # and is still shown; it just decides (sentence end) or doesn't (clause) end the row.
    _SENTENCE_END = (".", "!", "?", "…")

    def _ends_sentence(tok: str) -> bool:
        # A trailing closing quote/bracket after the terminal mark still counts as a
        # sentence end (e.g. 'rồi."' / 'thật?)'). Strip trailing quotes/brackets, then
        # test the terminal punctuation. "..." is covered by endswith(".").
        t = tok.rstrip("\"'”’)】»]")
        return t.endswith(_SENTENCE_END)

    # Inter-word gap (px between adjacent word bounding boxes). Default = the
    # font's natural space advance (looks like normal text). KARAOKE_SPACE_W
    # overrides: leave it empty/unset for natural, or set an absolute px value
    # (e.g. 8 for snug, 0 for touching).
    _env_space = os.getenv("KARAOKE_SPACE_W", "").strip()
    space_w = float(_env_space) if _env_space else _natural_space

    def _chunk_words(ws: list[dict]) -> list[list[dict]]:
        groups, cur, cur_px = [], [], 0.0
        for w in ws:
            tok = _ass_escape(w["word"]).strip()
            tok_px = _word_px(tok)
            add_px = tok_px + (space_w if cur else 0.0)
            # ATOMIC-TERM GUARD (owner bug C): never break a hyphen-joined compound
            # (e.g. "sub-agent" -> "sub" + "agent") across two rows. When the PREVIOUS
            # word on the current row is glued to THIS word, they are one term — the row
            # is allowed to TEMPORARILY exceed the width/word cap to finish the term
            # before starting a new row. `glued` suppresses the break below for this word.
            glued = bool(cur and cur[-1].get("glue_next"))
            # PRIORITY 2: word-count cap or pixel-width budget. Width is the hard limit —
            # if a 5th/over-wide word would overflow the safe row, break before it (so a
            # packed line never exceeds usable_px even when the active word zooms). But a
            # glued continuation of the current word is NEVER split off — complete it here.
            if cur and not glued and (cur_px + add_px > max_px or len(cur) >= KARAOKE_MAX_WORDS):
                groups.append(cur)
                cur, cur_px = [], 0.0
                add_px = tok_px
            cur.append(w)
            cur_px += add_px
            # PRIORITY 1: end the row at a SENTENCE boundary (NOT at clause marks) — but
            # not in the middle of a glued term (defensive: a hyphen part shouldn't carry
            # a sentence-terminal mark, but if it does, keep the term intact).
            if cur and not w.get("glue_next") and _ends_sentence(tok):
                groups.append(cur)
                cur, cur_px = [], 0.0
        if cur:
            groups.append(cur)
        return groups

    groups = _chunk_words(words)

    # ANTI-ORPHAN post-pass (owner bug B): a sentence-terminal short word left ALONE on
    # a fresh final row reads as the caption "jumping and freezing for a beat" — the
    # previous 5-word row clears, a lone word (e.g. "hơn?") pops onto an empty row, and
    # the last-group tail-hold (chunk_end = last_end + 0.40) then holds that single word
    # static for ~0.4s at the scene end. Fix: pull a trailing 1-word group back onto the
    # previous row so the sentence finishes on one line (no lone-word jump, the tail-hold
    # lands on a natural multi-word row). Allowed to slightly exceed KARAOKE_MAX_WORDS —
    # the same "overflow to complete the term" allowance as the atomic-term guard — but
    # NEVER past the row pixel-width budget (that would push text past the safe margins).
    if len(groups) >= 2 and len(groups[-1]) == 1:
        prev, last = groups[-2], groups[-1]
        merged = prev + last
        merged_px = sum(_word_px(_ass_escape(g["word"]).strip()) for g in merged) \
            + space_w * max(0, len(merged) - 1)
        # Only merge when the combined row still fits the width budget AND the previous
        # row is a normal packed row (don't grow an already-huge row unboundedly).
        if merged_px <= max_px and len(prev) < KARAOKE_MAX_WORDS + 2:
            groups[-2] = merged
            groups.pop()

    # Per-word absolute positioning: each word in a group is its own Dialogue event
    # with \an2\pos(x, pos_y). Every word is anchored independently at a fixed
    # horizontal center — scaling the active word expands it in-place around that
    # center without reflowing (and thus shifting) its neighbours.

    # Highlight-zoom headroom. The active word scales to HL_SCALE% around its OWN
    # centre, so each edge advances toward its neighbour by (HL_SCALE/100-1)/2 of
    # the word's width. With a tight gap a long active word overlaps the start/end
    # of its neighbours. Fix: widen the inter-word gap to that growth, sized from
    # the WIDEST word in the line so the gap stays UNIFORM (equal for every pair).
    # Short-word lines keep the tight natural space; only lines containing a long
    # word open up, and only as much as the zoom needs.
    _zoom_grow = max(0.0, (HL_SCALE / 100.0 - 1.0) / 2.0)

    def _group_gap(widths: list[float]) -> float:
        if len(widths) < 2:
            return space_w
        # Reserve the widest word's zoom growth ON TOP of the natural gap, so that
        # when that word is highlighted it grows into the reserved part and STILL
        # leaves a full `space_w` gap to its neighbour (never touches/overlaps).
        gap = space_w + _zoom_grow * max(widths)
        # Clamp so a line packed with long words still fits the safe area
        # (degrade toward space_w rather than overflow / push past the margins).
        max_gap = (usable_px - sum(widths)) / (len(widths) - 1)
        return max(space_w, min(gap, max_gap)) if max_gap > space_w else space_w

    def _group_x_centers(grp: list[dict]) -> tuple[list[float], float]:
        # Returns (per-word x-centers, total rendered line width). total_w is the
        # actual on-screen width of this single-row chunk in libass-rendered units
        # (Pillow widths already scaled by CAPTION_LIBASS_WFACTOR), so the caller can
        # size a cover box to exactly the VN text — see _build_translate_full_karaoke.
        toks = [_ass_escape(g["word"]) for g in grp]
        widths = [_word_px(t) for t in toks]
        gap = _group_gap(widths)
        total_w = sum(widths) + gap * max(0, len(grp) - 1)
        x = width / 2.0 - total_w / 2.0
        centers = []
        for w in widths:
            centers.append(x + w / 2.0)
            x += w + gap
        return centers, total_w

    # Anchor: legacy \an2 (bottom-center; bottom edge at height - margin_v) or, when
    # pin_top_y is given, \an8 (top-center; TOP edge pinned at pin_top_y — right under
    # the portrait footage band, pop grows downward so the top padding never moves).
    if pin_top_y is not None:
        pos_anchor, pos_y = 8, int(pin_top_y)
    else:
        pos_anchor, pos_y = 2, height - margin_v

    # (start, end, ass_text, layer). Each word in a group occupies its own layer so
    # libass renders them as independent positioned elements; stacking impossible.
    raw_events: list[tuple[float, float, str, int]] = []
    # Unique layer band per group prevents cross-group overlay. Size it from the ACTUAL
    # widest group (not KARAOKE_MAX_WORDS, now a high ceiling) so layer numbers stay
    # compact while still giving every word in the largest group its own layer.
    _max_group = max((len(g) for g in groups), default=0)
    layer_stride = _max_group + 2
    # Widest single-row chunk actually laid out (libass-rendered px). Since this builder
    # NEVER wraps (WrapStyle 2; exactly one single-row chunk on screen at any instant),
    # this is the full horizontal extent our VN caption ever occupies — used to size the
    # translate_full blur cover to OUR text (not the source box).
    max_line_w = 0.0
    for gi, group in enumerate(groups):
        n = len(group)
        if gi + 1 < len(groups):
            next_start = float(groups[gi + 1][0]["start"])
            last_end = float(group[-1]["end"])
            chunk_end = max(next_start, last_end)
            # Hard cap: all events for this group end at next_start so no
            # event from group gi can bleed onto the screen when group gi+1 starts.
            display_end = next_start
        else:
            chunk_end = float(group[-1]["end"]) + 0.40
            display_end = chunk_end
        x_centers, group_w = _group_x_centers(group)
        max_line_w = max(max_line_w, group_w)
        group_start = float(group[0]["start"])
        layer_base = gi * layer_stride
        for j, g in enumerate(group):
            tok = _ass_escape(g["word"])
            xc = int(x_centers[j])
            pos_tag = f"\\an{pos_anchor}\\pos({xc},{pos_y})"
            w_start = float(g["start"])
            w_end = min(float(g["end"]), display_end)
            w_end = max(w_end, w_start + 0.05)
            # Extend highlight to the next word's start (mid-group) or to the
            # group's display end (last word). The original bug: capping at w_end
            # left a blank window from w_end to next_word_start / display_end —
            # visible as an unhighlighted static period that reads as a karaoke gap.
            # Symmetric fix: last word of a group stays highlighted until the group
            # ends (= next group starts), just as mid-group words stay highlighted
            # until the next word within the group starts.
            if j + 1 < n:
                hl_end = min(float(group[j + 1]["start"]), display_end - 0.001)
            else:
                hl_end = display_end - 0.001
            hl_end = max(hl_end, w_start + 0.02)
            slot_ms = max(1, int((hl_end - w_start) * 1000))
            t_in = max(20, min(50, slot_ms // 4))
            t_out_start = max(t_in + 10, slot_ms - t_in)
            static_text = f"{{\\c&HFFFFFF&{pos_tag}}}{tok}"
            hl_text = (
                f"{{\\c&HFFFFFF&{pos_tag}\\fscx100\\fscy100"
                f"\\t(0,{t_in},\\c{HL_COLOR}\\fscx{HL_SCALE}\\fscy{HL_SCALE})"
                f"\\t({t_out_start},{slot_ms},\\c&HFFFFFF&\\fscx100\\fscy100)}}"
                f"{tok}"
            )
            layer = layer_base + j
            # Before this word's voice: show unhighlighted
            if w_start > group_start + 0.001:
                raw_events.append((group_start, w_start, static_text, layer))
            # Highlight: bounded to the spoken word window
            raw_events.append((w_start, hl_end, hl_text, layer))
            # After highlight: unhighlighted until group display ends
            if hl_end < display_end - 0.001:
                raw_events.append((hl_end, display_end, static_text, layer))

    # Per-layer dedup + clamp: within each layer exactly one event is on screen at
    # any instant (no overlap within a layer → libass never stacks two lines).
    by_layer: dict[int, list[tuple[float, float, str]]] = {}
    for start, end, text, layer in raw_events:
        if layer not in by_layer:
            by_layer[layer] = []
        by_layer[layer].append((start, end, text))

    events: list[str] = []
    for layer in sorted(by_layer.keys()):
        layer_evs = sorted(by_layer[layer], key=lambda e: e[0])
        dedup: list[tuple[float, float, str]] = []
        for ev in layer_evs:
            if dedup and abs(ev[0] - dedup[-1][0]) < 1e-3:
                dedup[-1] = ev
            else:
                dedup.append(ev)
        for i, (start, end, text) in enumerate(dedup):
            nxt = dedup[i + 1][0] if i + 1 < len(dedup) else end
            end = nxt if nxt > start else (end if end > start else start + 0.10)
            if clamp_window is not None:
                # Hard-cut this scene's caption to its own window so the previous line is
                # gone before the next scene's first line appears (no seam overlap).
                start = max(start, clamp_window[0])
                end = min(end, clamp_window[1])
                if end <= start:
                    continue   # event lies fully outside this scene -> drop it
            events.append(
                f"Dialogue: {layer},{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}"
            )

    # return_events (translate_full): hand back the header + event lines (so the caller
    # can MERGE the per-clip karaoke into ONE spanning .ass — clips are time-disjoint, so
    # reusing layer numbers across clips is safe) PLUS max_line_w (widest rendered chunk,
    # for sizing the blur cover to OUR VN text). Footage/image callers use the default
    # (write a per-scene file and return its path) — behavior unchanged.
    if return_events:
        return header, events, max_line_w
    path = os.path.join(work, f"cap_{idx}.ass")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header + "\n".join(events) + "\n")
    return path


# When the VO outruns the cut clip by MORE than this fraction, the footage scene is
# SLOWED (setpts) to fill the VO once instead of looping the raw clip (which visibly
# repeats). Below this tolerance the clip already ~matches the VO so we leave it at
# native speed (the trailing tpad/loop covers the sub-tolerance remainder invisibly).
FOOTAGE_SLOWDOWN_TOL = float(os.getenv("FOOTAGE_SLOWDOWN_TOL", "0.12"))


def _footage_scene_clip(src_clip: str, vo_audio: str, dur: float, ass_path: str | None,
                        width: int, height: int, fps: int, work: str, idx: int,
                        src_audio_volume: float = 0.0, zoom: float = 0.0) -> str:
    """Render one footage scene: 9:16 blurred-bg fit + karaoke captions, lasting
    exactly the VO duration.

    Audio: by DEFAULT (src_audio_volume == 0) the output carries ONLY the Vietnamese
    voiceover — the original English source audio is dropped entirely, so the video
    has a single, unified voice (no two-voices artifact). When src_audio_volume > 0
    the source audio is mixed UNDER the VO at that linear gain (the VO stays at full
    level and remains dominant); typical faint-bed values are 0.05–0.15.

    Video: the source clip is shorter than the VO often (the VO is the new, longer
    narration). The OLD behavior LOOPED the short clip to fill the VO — but a clip
    much shorter than the VO then visibly REPEATS several times (owner-reported on
    job 70 scene 20 / job 72 scene 21: a 1.3s clip vs a 5.756s VO = ~4.4x needed,
    reading as a stutter). Instead we SLOW the single clip with setpts so it plays
    ONCE across the whole VO (smooth slow-motion, no repeat), capped at
    FOOTAGE_MAX_SLOWDOWN so an extreme ratio doesn't judder into a hard near-freeze.

    When the needed slowdown EXCEEDS the cap, the slowed single pass still ends
    before the VO. The earlier fallback (slow by the cap + -stream_loop the
    remainder) left a RESIDUAL, still-visible loop — at the 3.0x cap the 1.3s clip
    spans only 3.9s of a 5.756s VO, then the source restarted and the image
    repeated (confirmed: loop-region frames matched the start-region frames at
    SSIM ~0.996, i.e. a re-start of the motion). We now FREEZE the LAST frame for
    the remainder instead of looping it: -stream_loop is removed and tpad=clone
    holds the final frame to fill `dur`. A held last frame reads as a deliberate
    pause, NOT a jarring restart of the clip. The cap default was also raised to
    4.0x (env FOOTAGE_MAX_SLOWDOWN) so the slowed motion covers more of the VO and
    the frozen tail is shorter; truly extreme ratios (> cap) still freeze the tail.
    The final tpad=clone + trim still guarantees the visual exactly fills `dur`.
    """
    # The filtergraph references [0:v]; if the cut clip lost its video stream (range
    # overshot the source end → audio-only slice), fail with a clear message instead
    # of the opaque ffmpeg "[0:v] matches no streams" crash.
    if not _has_video(src_clip):
        raise RuntimeError(f"footage scene {idx}: cut clip has no video stream ({src_clip}); "
                           "the scene's source range likely overshot the video end")
    mix_src = src_audio_volume > 0.0 and _has_audio(src_clip)
    # Decide the playback-speed factor that makes the (single) source clip span the
    # whole VO. clip_dur is the cut clip's real duration; pts_factor < 1 would be a
    # SPEED-UP (never wanted here — a longer clip is just trimmed), so we only slow
    # DOWN (factor > 1) when the VO meaningfully outruns the clip. A small tolerance
    # avoids retiming clips that already ~match the VO (no visible repeat there).
    clip_dur = _probe_duration(src_clip)
    # Default 4.0x (raised from 3.0): at 4.0x a clip covers more of the VO before the
    # frozen tail kicks in, so the held-last-frame remainder is shorter. Env-tunable.
    max_slowdown = float(os.getenv("FOOTAGE_MAX_SLOWDOWN", "4.0"))
    pts_factor = 1.0
    if clip_dur > 0.05 and dur > clip_dur * (1.0 + FOOTAGE_SLOWDOWN_TOL):
        # Slow just enough to fill the VO, but never beyond the cap (an extreme
        # slowdown reads as a hard freeze). Above the cap the single slowed pass still
        # ends before the VO; the tpad=clone below FREEZES the last frame for the
        # remainder (NOT a loop — looping reintroduced the visible repeat).
        pts_factor = min(max_slowdown, dur / clip_dur)
    # Retime the source ONCE at the head of the chain so both the blurred bg and the
    # sharp fg share the identical (slowed) motion. PTS-STARTPTS rebases to 0; the
    # multiply by pts_factor stretches each frame's timestamp → slow motion.
    _spd = (f"setpts={pts_factor:.4f}*(PTS-STARTPTS)" if pts_factor != 1.0
            else "setpts=PTS-STARTPTS")
    vf = (
        f"[0:v]{_spd},split[bg][fg];"
        f"{_bg_blur_chain(width, height)};"
        f"[fg]scale={width}:-2:force_original_aspect_ratio=decrease[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,"
        # Guarantee the visual fills the whole VO by HOLDING the last frame (clone)
        # for any remainder the slowed single pass didn't cover. stop_duration is set
        # to the full VO length so even an extreme slowdown-cap shortfall is filled by
        # a freeze (never a loop); the final trim cuts back to exactly dur.
        f"tpad=stop_mode=clone:stop_duration={dur:.3f},fps={fps},trim=duration={dur:.3f},setpts=PTS-STARTPTS[vbase]"
    )
    # Optional per-scene Ken Burns slow zoom (translate_full). Deterministic zoom driven
    # by the output frame counter `on` (no stateful accumulator to drift); d=1 => one
    # output frame per input frame (this is video, not a still). Resets each scene, like
    # summary's per-scene Ken Burns. zoom==0 (default, footage/stickman) is a no-op.
    _vlabel = "[vbase]"
    if zoom and zoom > 0:
        n_frames = max(1, round(dur * fps))
        vf += (
            f";[vbase]zoompan=z='min(1+{zoom:.4f}*on/{n_frames},{1 + zoom:.4f})'"
            f":d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":s={width}x{height}:fps={fps}[vz]"
        )
        _vlabel = "[vz]"
    if ass_path:
        vf += (f";{_vlabel}subtitles='{_ff_filter_path(ass_path)}'"
               f":fontsdir='{_ff_filter_path(CAPTION_FONTSDIR)}'[v]")
    else:
        vf += f";{_vlabel}null[v]"
    # Audio: the VO drives the length. Only mix the (faint) source audio when the
    # caller asked for it AND the source clip actually has an audio stream — some cut
    # segments are silent (the slice ran past the source end), and referencing [0:a]
    # then makes ffmpeg fail with "Stream specifier ':a' matches no streams".
    if mix_src:
        # VO at full gain + source attenuated to src_audio_volume, summed (NOT
        # averaged) so the VO stays at its original loudness and the source sits
        # faintly underneath. `normalize=0` keeps amix from halving each input.
        vf += (
            f";[1:a]aresample=48000[vo];[0:a]aresample=48000,volume={src_audio_volume:.4f}[oa];"
            f"[vo][oa]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
        )
    else:
        # Default: VO only — the original/source audio is dropped entirely.
        vf += ";[1:a]aresample=48000[a]"
    clip = os.path.join(work, f"fscene_{idx:03d}.mp4")
    _run_ffmpeg(
        [
            # No -stream_loop: a short clip is slowed (setpts) to span the VO, and any
            # remainder past the slowdown cap is filled by tpad=clone FREEZING the last
            # frame. Looping the source here reintroduced the owner-reported repeat.
            "-i", src_clip, "-i", vo_audio,
            "-filter_complex", vf,
            "-map", "[v]", "-map", "[a]",
            "-t", f"{dur:.3f}", "-r", str(fps),
            *_video_encoder_args(fps=fps),
            *_audio_encoder_args(),
            clip,
        ],
        step=f"footage scene {idx}",
    )
    return clip


class FootageScene(BaseModel):
    scene: int
    clipPath: str           # local source-video slice for this scene
    audioPath: str          # the Vietnamese VO for this scene
    caption: str | None = None  # text fallback (karaoke is built from VO words)
    durationS: float | None = None
    # Silence the TTS worker inserted ON PURPOSE in this scene (OmniVoice punctuation
    # beats), seconds. Discounted when measuring the perceived pace so a deliberate pause
    # is not read as a slow voice — see _scene_wall_ms_per_syl. 0.0 when unknown (e.g. a
    # scene served from the TTS cache), in which case the measured-gap discount covers it.
    beatSilenceS: float = 0.0


class FootageAssembleRequest(BaseModel):
    page: str | None = None
    title: str = "video"
    scenes: list[FootageScene]
    bgmPath: str | None = None
    bgmVolume: float = 0.12
    width: int = 1080
    height: int = 1920
    fps: int = 30
    captions: bool = True
    sourceName: str | None = None
    sourceLink: str | None = None
    sourceLogo: str | None = None
    sourceHandle: str | None = None
    outroHandle: str | None = None   # own-page handle for the black outro card; None = none
    addCredit: bool = True
    # Source (original) audio gain UNDER the Vietnamese voiceover. 0.0 (default) =
    # the source audio is dropped entirely, so the output has ONLY the unified VO.
    # 0.05/0.10/0.15 = mix the source faintly under the VO (VO stays dominant).
    srcAudioVolume: float = 0.0
    outDir: str | None = None
    videoId: int | None = None  # when set, the output filename is suffixed " (v<id>)" so re-renders don't overwrite each other
    # TTS engine that produced the scene audio (voice_clone_model). The shared pace flow
    # ignores it — _normalize_scene_pace / _auto_target_pace run for every engine. It is
    # read ONLY by _omnivoice_post_pace (the OMNIVOICE-ONLY section), which self-gates on
    # it to add OmniVoice's per-scene uniform-pace pass. None/other → nothing extra runs.
    engine: str | None = None


def _pace_syllables(word: str) -> int:
    """Spoken-syllable estimate = vowel-group runs (>=1), with an English silent-final-e
    correction. Called ONLY by _spoken_weight (its sole consumer in this module).

    SILENT-E FIX (owner 2026-08-02). Vowel groups over-count English loanwords whose final
    'e' is mute: "runtime" scores u/i/e = 3 but is spoken ~2 ("run-time"), "file" scores
    i/e = 2 but is 1. Over-counting makes the word look FASTER per syllable than it is,
    which biases both the pace correction and the caption interpolation weight.

    Rule: a bare ASCII final 'e' does not open its own syllable, EXCEPT
      • when the char before it is itself a vowel ("ee", "ye", "ae") — there is no separate
        group to remove anyway;
      • when the word ends in a SYLLABIC "-le", i.e. consonant + "le" ("table", "little"),
        where the -le genuinely is a syllable. A vowel before the l ("file", "while") is the
        mute case and IS corrected;
      • when removing it would leave zero groups — this is what protects Vietnamese
        one-syllable tokens ending in e ("xe", "che", "nghe", "the") and English "the".
    Accented Vietnamese e's (ê, è, é, ẻ, ẽ, ẹ …) are never touched: the rule matches only
    the bare ASCII letter. "engineering" (the job-v109 case this function exists for) does
    not end in 'e' and is unchanged at 4."""
    runs, inv = 0, False
    for c in word.lower():
        v = c in _PACE_VOWELS
        if v and not inv:
            runs += 1
        inv = v
    w = "".join(c for c in word.lower() if c.isalpha())
    if runs > 1 and len(w) >= 2 and w[-1] == "e":
        prev = w[-2]
        prev_is_vowel = prev in _PACE_VOWELS
        syllabic_le = (prev == "l" and len(w) >= 3 and w[-3] not in _PACE_VOWELS)
        if not prev_is_vowel and not syllabic_le:
            runs -= 1
    return max(1, runs)


def _scene_pace_ms_per_syl(words: list[dict]) -> float | None:
    """Robust per-scene pace = MEDIAN of individual words' ms/syllable. Using the median
    (not total-span/total-syllables) makes the measure insensitive to a single deliberately
    slowed word like 'prompt' (one 2× outlier word does not drag the scene's pace). Returns
    None if the scene has too few words to judge.

    Syllables come from _spoken_weight, NOT _pace_syllables (job-287 "cảnh đầu đọc quá
    nhanh"): _pace_syllables counts VOWEL GROUPS, so an acronym scores 1 ("GPU" has one
    vowel run) even though it is SPOKEN as one syllable per letter. Video 278 scene 1
    ("Nvidia và AMD thống trị GPU, …") therefore measured 12 syllables instead of 16 and
    its pace read 207 ms/syll when the real spoken rate was 155 ms/syll — the FASTEST
    scene of the video, yet it looked on-target and the uniform-pace pass slowed it by
    only 9%. _spoken_weight already had the correct rule (acronym = letter count,
    mixed-case = len//2, else vowel groups) for caption anchoring; the pace metric now
    shares that single source of truth. Scenes with no acronym/mixed-case token are
    numerically UNCHANGED (the acronym branches simply don't fire)."""
    per = []
    for w in words:
        dur_ms = (w["end"] - w["start"]) * 1000.0
        if dur_ms <= 0:
            continue
        per.append(dur_ms / _spoken_weight(w["word"]))
    if len(per) < 3:
        return None
    per.sort()
    n = len(per)
    return per[n // 2] if n % 2 else (per[n // 2 - 1] + per[n // 2]) / 2.0


def _atempo_filter(factor: float) -> str:
    """FFmpeg atempo filter string for `factor` (playback-speed multiplier). atempo's
    valid range is 0.5-2.0; chain for extremes. factor>1 = faster (shorter), <1 = slower."""
    if factor > 2.0:
        return f"atempo=2.0,atempo={factor/2.0:.4f}"
    if factor < 0.5:
        return f"atempo=0.5,atempo={factor/0.5:.4f}"
    return f"atempo={factor:.4f}"


def _normalize_scene_joins(scenes, work: str) -> None:
    """Scene-JOIN silence normalization (owner request 2026-07-28, video 269 0:11-0:16).

    The final video concats scene clips as a HARD CUT, and each clip lasts exactly its VO
    wav — so the audible pause at every scene boundary is exactly (trailing silence of
    scene N) + (leading silence of scene N+1), both baked inside the wavs by the TTS
    engine. Measured on video 269: ~0.30 s per join; when a scene then OPENS with a short
    clause + comma ("Tương tự,"), its in-wav comma pause (~0.39 s) lands right next to the
    join pause and the 2-pause cluster reads as "the voice slowed down".

    This pass trims each scene wav's EDGES so every join converges to ~2×KEEP seconds:
    leading/trailing silence longer than KEEP is cut back to KEEP; shorter edges are left
    alone. EDGE-ONLY by construction — silenceremove touches nothing past the first/last
    non-silent sample, so mid-wav pauses (commas) are never sliced (that is the exact
    mechanism the reverted OmniVoice gap-shaper got wrong: it re-cut FINISHED audio at
    whisper word boundaries and clipped Vietnamese soft-consonant tails).

    The -40 dB threshold is deliberately conservative: F5's breath-level lead-ins sit at
    speech-adjacent energy (~-28..-38 dB), so they are NOT classified as silence and are
    left untouched — same for natural trailing vowel decay (it stays above the threshold
    until it is genuinely inaudible). Runs FIRST in assembly — before the pace passes and
    the caption whisper — so karaoke timing and pace measurement see the FINAL audio.

    Repoints scene.audioPath / clears scene.durationS exactly like _normalize_scene_pace.
    Fail-safe per scene: any ffmpeg error, an implausibly short result (<0.3 s — e.g. the
    all-silence placeholder wav), or a negligible saving (<20 ms) keeps the original.
    Env: CF_SCENE_JOIN_TRIM=0 disables; CF_SCENE_JOIN_KEEP (default 0.075 s/edge, i.e.
    ~0.15 s per join); CF_SCENE_JOIN_DB (default -40)."""
    if os.getenv("CF_SCENE_JOIN_TRIM", "1").strip().lower() in ("0", "off", "false", "no"):
        return
    try:
        keep = float(os.getenv("CF_SCENE_JOIN_KEEP", "0.075"))
        thresh_db = float(os.getenv("CF_SCENE_JOIN_DB", "-40"))
    except ValueError:
        keep, thresh_db = 0.075, -40.0
    # One silenceremove per edge; areverse makes the same start-edge filter act on the tail.
    edge = f"silenceremove=start_periods=1:start_silence={keep}:start_threshold={thresh_db}dB"
    af = f"{edge},areverse,{edge},areverse"
    trimmed = 0
    saved_total = 0.0
    for s in scenes:
        out = os.path.join(work, f"join_{s.scene:03d}.wav")
        try:
            before = s.durationS or _probe_duration(s.audioPath)
            _run_ffmpeg(
                ["-i", s.audioPath, "-af", af,
                 "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", out],
                f"join-trim scene {s.scene}",
            )
            after = _probe_duration(out)
            # Guards: keep the original on an implausible result or a negligible saving.
            if after < 0.3 or (before - after) < 0.02:
                continue
            s.audioPath = out
            s.durationS = None  # force re-probe of the trimmed audio downstream
            trimmed += 1
            saved_total += before - after
        except Exception as e:  # noqa: BLE001 — a join trim must never fail the render
            log.warning("join-trim scene %s failed (%s); leaving unchanged", s.scene, e)
    if trimmed:
        log.info("join-trim: tightened %d/%d scene edge(s), %.2fs total (keep=%.3fs/edge, %.0fdB)",
                 trimmed, len(scenes), saved_total, keep, thresh_db)


def _normalize_scene_pace(scenes, work: str) -> dict:
    """Soft per-scene pace normalization (Issue 2, Option 1). Whispers all scenes ONCE,
    measures each scene's median ms/syllable pace, computes the video median, and for
    OUTLIERS (outside [BAND_LO,BAND_HI]×median) writes an atempo-corrected wav into `work`
    and repoints scene.audioPath / clears scene.durationS. In-band scenes are untouched.

    Runs BEFORE the caption whisper so captions derive from the retimed audio. Returns a
    debug dict {scene: (pace_before, factor)} for logging/validation. Fail-safe: on any
    whisper/ffmpeg error the scene is left unchanged.

    A scene whose narration contains a protected slow-term (F5_PACE_PROTECT_TERMS, e.g.
    'prompt') is NEVER sped up — only slowed if it is too fast — so the Issue-1 slowdown
    is preserved. NOTE: 'caption' is the closest per-scene text we have here for the
    protect check (assemble receives no separate narration field); it carries the original
    words, so the term match is reliable."""
    if not F5_PACE_NORMALIZE or len(scenes) < 3:
        return {}
    try:
        res = _run_cf_worker(
            "whisper_worker.py",
            {"items": [{"scene": s.scene, "audioPath": s.audioPath} for s in scenes],
             "model": WHISPER_MODEL, "device": WHISPER_DEVICE, "compute": WHISPER_COMPUTE,
             "language": "vi", "wordTimestamps": True},
            timeout=1200,
        )
    except Exception as e:
        log.warning("pace-normalize whisper failed (%s); skipping normalization", e)
        return {}
    words_by_scene = {r["scene"]: [w for seg in r.get("segments", []) for w in (seg.get("words") or [])]
                      for r in res["results"]}
    paces = {sc: _scene_pace_ms_per_syl(ws) for sc, ws in words_by_scene.items()}
    valid = [p for p in paces.values() if p]
    if len(valid) < 3:
        return {}
    valid.sort()
    n = len(valid)
    median = valid[n // 2] if n % 2 else (valid[n // 2 - 1] + valid[n // 2]) / 2.0
    hi_edge = F5_PACE_BAND_HI * median
    lo_edge = F5_PACE_BAND_LO * median
    debug = {}
    for s in scenes:
        pace = paces.get(s.scene)
        if not pace:
            continue
        # SLOW-ONLY normalization (owner: smoothing must NOT increase overall speed).
        # We ONLY correct too-FAST outlier scenes (pace < lo_edge) by SLOWING them toward
        # the low band edge. The too-SLOW branch (which sped scenes UP) was REMOVED so
        # smoothing can never raise the overall pace — it only trims the fast tail. Slow
        # scenes are left as-is (the global F5 slowdown already lowers the whole video).
        if pace >= lo_edge:
            continue  # in-band OR too-slow → untouched (never sped up)
        target = pace + (lo_edge - pace) * F5_PACE_EDGE_PULL  # pull up toward low edge
        # atempo(playback speed) = current_pace / target_pace. target>pace → factor<1.0 =
        # SLOWER audio (bigger ms/syll). This is always a slow-down here by construction.
        factor = pace / target
        # NOTE (bug: symptoms 1-2, v108 — "sentence đột nhiên nhanh hơn"). The acronym-scene
        # skip was REMOVED here. It previously left a scene containing ANY acronym (MCP, RAG,
        # …) UNCORRECTED even when that scene read far too fast — e.g. v108 scene 8 measured
        # 160 ms/syll (band lo=204) but was skipped because it contains "MCP", then the global
        # auto target-pace (+15%) sped it up FURTHER, so it read markedly faster than its
        # neighbours (the owner's non-uniform-pace complaint). This branch ONLY ever SLOWS a
        # too-fast scene toward the band edge; slowing brings the whole sentence — acronym
        # syllables included — to the CONSTANT pace the owner requires, which is exactly the
        # goal (an acronym spoken via its say_as respelling is ordinary syllables at sentence
        # pace, not a special "compact" speed). The old "acronyms must stay fast" rationale
        # belonged to the acronym-TIGHTEN pass (a per-region speed-up, now OFF by default) and
        # contradicts the constant-pace rule, so it no longer gates pace normalization. Only a
        # predefined say_as term (tilde SLOW-join) may deviate from the constant pace; the
        # median-based pace metric already ignores such single-word outliers.
        factor = max(F5_PACE_ATEMPO_MIN, min(F5_PACE_ATEMPO_MAX, factor))
        if abs(factor - 1.0) < 0.01:
            continue  # negligible
        norm_path = os.path.join(work, f"pace_{s.scene:03d}.wav")
        try:
            _run_ffmpeg(
                ["-i", s.audioPath, "-af", _atempo_filter(factor),
                 "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", norm_path],
                f"pace-normalize scene {s.scene}",
            )
            if os.path.isfile(norm_path):
                s.audioPath = norm_path
                s.durationS = None  # force re-probe of the retimed audio downstream
                debug[s.scene] = (round(pace, 1), round(factor, 3))
        except Exception as e:
            log.warning("pace-normalize scene %s ffmpeg failed (%s); leaving unchanged", s.scene, e)
    if debug:
        corrected = {k: v for k, v in debug.items() if v[1] != 1.0}
        log.info("pace-normalize: median=%.1f ms/syll band=[%.0f,%.0f]; corrected %d/%d scene(s): %s",
                 median, lo_edge, hi_edge, len(corrected), len(scenes), corrected)
    return debug


def _target_pace_factor(current: float) -> float:
    """Two-directional atempo factor to converge `current` (ms/syllable) toward
    GLOBAL_TARGET_MS_PER_SYL, clamped to [GLOBAL_TARGET_ATEMPO_FLOOR, GLOBAL_TARGET_ATEMPO_CEIL].
    factor = current/target: <1.0 SLOWS a fast voice (current<target), >1.0 SPEEDS UP a slow
    voice (current>target). The floor caps stretch of a fast voice; the ceil caps rush of a
    slow voice (the two-directional balance bound). Pure (no I/O) → unit-testable. Returns 1.0
    for a non-positive target."""
    if GLOBAL_TARGET_MS_PER_SYL <= 0:
        return 1.0
    factor = current / GLOBAL_TARGET_MS_PER_SYL
    return min(GLOBAL_TARGET_ATEMPO_CEIL, max(GLOBAL_TARGET_ATEMPO_FLOOR, factor))


def _auto_target_pace(scenes, word_map: dict, work: str, pace_word_map: dict | None = None) -> float:
    """AUTO TARGET-PACE (voice-independent): measure the whole video's overall pace from
    the already-computed caption `word_map`, then retime every scene UNIFORMLY so the overall
    pace CONVERGES to GLOBAL_TARGET_MS_PER_SYL. Returns the applied atempo factor (1.0 = no change).

    TWO-DIRECTIONAL (owner-approved 2026-07-03, supersedes the old slow-only behavior): the
    goal is a CONSISTENT reading pace regardless of the input clone voice. A FAST voice
    (current_pace < target) is SLOWED (atempo < 1.0, floored at GLOBAL_TARGET_ATEMPO_FLOOR so
    it is never over-stretched); a SLOW voice (current_pace > target) is SPED UP (atempo > 1.0)
    but BOUNDED by GLOBAL_TARGET_ATEMPO_CEIL (default 1.15 = at most +15%), so a very slow voice
    is only NUDGED toward the common pace, never rushed. This deliberately relaxes the earlier
    "never speed up" rule for the narrow, capped case of equalizing voices — speeding a slow VO
    up only makes the video SHORTER, so it never threatens the footage "VO fits under source"
    invariant. Set GLOBAL_TARGET_ATEMPO_CEIL=1.0 to restore pure slow-only.

    The SAME atempo is applied to EVERY scene wav (uniform → all relative timing, incl. the
    slowed 'prompt' and tightened acronyms, is preserved) and the caption `word_map` timestamps
    are SCALED by 1/factor in place (uniform atempo scales all times linearly), so captions stay
    in sync WITHOUT a second whisper pass. Each scene's durationS is cleared to force a re-probe
    of the retimed audio downstream. Fail-safe: a scene whose ffmpeg fails is left unchanged.

    Overall pace = MEDIAN across scenes of each scene's median-per-word ms/syllable (the
    same robust metric used elsewhere), so one slow/fast scene or word does not skew it.

    PACE-MEASUREMENT DECOUPLING (job-142 fix, 2026-07-07): the pace is MEASURED from
    `pace_word_map` when supplied, otherwise from `word_map` (the caption map). This matters
    because the caption `word_map` may be CTC-aligned (CF_KARAOKE_ALIGN=ctc): CTC marks the
    TIGHT acoustic extent of each word (shorter per-word spans than whisper, which include
    lead-in/trailing air), so measuring pace off a CTC map read the video as ~140 ms/syll
    when its true pace was ~200, triggering a spurious ×1.42 slowdown (+117 s on a 273 s
    video). Caption timing (display) and pace measurement (audio-duration control) are now
    independent: captions keep whatever backend CF_KARAOKE_ALIGN selects, while pace is
    always measured off a WHISPER word_map (word spans consistent with the pre-CTC baseline).
    The atempo is still APPLIED to every scene and the caption `word_map` timestamps are
    still scaled — only the SOURCE of the `current` measurement changes."""
    if not GLOBAL_TARGET_PACE or GLOBAL_TARGET_MS_PER_SYL <= 0 or not word_map:
        # Log the disabled case explicitly so a "why is the video fast?" investigation sees
        # the toggle in the log instead of a silent no-op (this was the exact blind spot that
        # masked the GLOBAL_TARGET_PACE=0 regression).
        if not GLOBAL_TARGET_PACE:
            log.info("auto target-pace: DISABLED (GLOBAL_TARGET_PACE=0) — no pace normalization applied")
        return 1.0
    # Measure pace from the whisper map when provided (backend-independent); else fall back
    # to the caption word_map (unchanged behavior when captions already use whisper).
    _measure_map = pace_word_map if pace_word_map else word_map
    scene_paces = []
    for sc, ws in _measure_map.items():
        p = _scene_pace_ms_per_syl(ws)
        if p:
            scene_paces.append(p)
    if len(scene_paces) < 1:
        return 1.0
    scene_paces.sort()
    n = len(scene_paces)
    current = scene_paces[n // 2] if n % 2 else (scene_paces[n // 2 - 1] + scene_paces[n // 2]) / 2.0
    # atempo = current/target, clamped to [floor, ceil]. <1.0 slows a FAST voice toward
    # target; >1.0 speeds up a SLOW voice toward target (the two-directional balance bound).
    factor = _target_pace_factor(current)
    if abs(factor - 1.0) < 0.005:
        log.info("auto target-pace: current %.1f ms/syll ~= target %.1f — no change",
                 current, GLOBAL_TARGET_MS_PER_SYL)
        return 1.0
    scale = 1.0 / factor  # audio (and every timestamp) stretches (>1) or compresses (<1)
    changed = 0
    for s in scenes:
        tgt = os.path.join(work, f"tpace_{s.scene:03d}.wav")
        try:
            _run_ffmpeg(
                ["-i", s.audioPath, "-af", _atempo_filter(factor),
                 "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", tgt],
                f"auto target-pace scene {s.scene}",
            )
            if os.path.isfile(tgt):
                s.audioPath = tgt
                s.durationS = None
                changed += 1
                # Scale this scene's caption word timestamps by `scale` (linear stretch).
                for w in word_map.get(s.scene, []):
                    w["start"] = w["start"] * scale
                    w["end"] = w["end"] * scale
        except Exception as e:
            log.warning("auto target-pace scene %s ffmpeg failed (%s); leaving unchanged", s.scene, e)
    direction = "SLOWER" if factor < 1.0 else "FASTER"
    log.info("auto target-pace: current %.1f -> target %.1f ms/syll; atempo=%.4f (%s, duration x%.4f) "
             "applied to %d/%d scene(s); caption timestamps scaled x%.4f",
             current, GLOBAL_TARGET_MS_PER_SYL, factor, direction, scale, changed, len(scenes), scale)
    return factor


# =============================================================================
# OMNIVOICE-ONLY SECTION — nothing below this banner is executed by an F5-TTS or
# VieNeu render, and nothing above it may branch on engine == "omnivoice".
#
# Owner decision 2026-07-31: OmniVoice was originally wired in by SKIPPING the shared
# pace flow from inside assemble_footage. That put engine knowledge in the middle of a
# function F5/VieNeu also run. The shared flow is now unconditional again (exactly its
# pre-OmniVoice form) and everything OmniVoice needs lives here, reached through the
# single entry point _omnivoice_post_pace().
#
# RULE: never add an `if engine == "omnivoice"` test outside this section. If OmniVoice
# needs different behavior, duplicate the logic into a function here.
# =============================================================================


def _scene_wall_ms_per_syl(words: list[dict], max_gap_s: float | None = None,
                           minus_s: float = 0.0) -> float | None:
    """PERCEIVED pace = (last word end - first word start) / total syllables, i.e. the
    scene's wall-clock rate INCLUDING the silences between words.

    Diagnostic counterpart to _scene_pace_ms_per_syl, which is a median of per-WORD
    rates and therefore blind to inter-word gaps. Measured on video 258 the two can
    diverge by >50% on a single scene (word 170 ms/syll vs wall 264 ms/syll), so a
    scene can sit exactly on target by the word metric and still sound slow. Logged
    side by side with the word metric by the pace-verify pass so the gap between
    'what we optimize' and 'what is heard' is visible in api.log instead of having to
    be re-derived from the finished mp4."""
    usable = [w for w in words if (w["end"] - w["start"]) > 0]
    if len(usable) < 3:
        return None
    span_ms = (usable[-1]["end"] - usable[0]["start"]) * 1000.0
    # DELIBERATE-SILENCE DISCOUNT (job-290 "giây 9 / giây 25 bị tăng tốc"). Since the
    # OmniVoice path inserts punctuation BEATS by construction, part of this span is silence
    # we ASKED for. Counting it made a scene look slow-as-heard, which drove the caller's
    # wall floor above the speed-up rail: video 281 scene 4 measured its word pace at exactly
    # the 200 target (factor 1.00 wanted) yet was sped up 10%, ending at 180 ms/syll against a
    # 200 median — i.e. the pass compressed the very beats it was told to insert AND
    # accelerated the speech. Scenes railed at max speed-up went 17/71 (no beats) -> 27/66.
    #
    # Two independent estimates of that silence, used together on purpose:
    #   • max_gap_s — MEASURED: the excess of every inter-word gap over max_gap_s. Works from
    #     whisper alone, so it also covers scenes served from the TTS CACHE (whose per-scene
    #     beat bookkeeping is not stored) and catches long TTS dead-air that was never a beat.
    #   • minus_s — EXACT: the silence the worker reports having inserted (beatSilenceS).
    # We apply whichever removes MORE (they measure the same physical silence, so adding both
    # would double-discount). Both default to off, so the metric is the plain wall-clock rate
    # unless a caller asks for the discount. (This function is OmniVoice-only — its callers
    # are _omnivoice_uniform_pace and _omnivoice_pace_verify. The F5 flow measures pace with
    # _scene_pace_ms_per_syl and never enters here.)
    reduction = 0.0
    if max_gap_s is not None and max_gap_s >= 0:
        reduction = sum(
            max(0.0, (b["start"] - a["end"]) - max_gap_s) * 1000.0
            for a, b in zip(usable, usable[1:])
        )
    if minus_s:
        reduction = max(reduction, float(minus_s) * 1000.0)
    if reduction:
        span_ms = max(0.0, span_ms - reduction)
    # _spoken_weight (not _pace_syllables) for the same acronym reason as the word metric
    # above — an under-counted acronym inflates ms/syllable and hides a fast scene.
    syls = sum(_spoken_weight(w["word"]) for w in usable)
    if span_ms <= 0 or syls <= 0:
        return None
    return span_ms / syls


def _omnivoice_beat_boundaries(audio_path: str) -> list[float] | None:
    """Midpoints (seconds) of the DIGITAL-SILENCE runs the TTS worker wrote at clause joins.

    Returns None when the wav cannot be read as 16-bit PCM, or an empty list when the scene
    carries no inserted beat (a single-clause scene, or audio from before this construction).
    The caller treats BOTH as "no audio signal" and falls back to the whisper-gap rule — for
    an empty list that fallback is still useful, since it can catch a long pause the MODEL
    rendered inside one generate() call, which leaves no digital-zero marker.

    Only near-zero amplitude counts (|sample| <= OMNIVOICE_UNIT_SILENCE_AMP). That is the
    whole point: the model's own clause-edge decay is QUIET but not zero, so it cannot
    produce a false boundary, while our inserted beat is exactly zero and always does."""
    try:
        import wave
        from array import array as _array

        with wave.open(audio_path, "rb") as w:
            if w.getsampwidth() != 2:
                return None
            n_ch, sr, n = w.getnchannels(), w.getframerate(), w.getnframes()
            if sr <= 0 or n <= 0:
                return None
            samples = _array("h")
            samples.frombytes(w.readframes(n))
    except Exception as e:  # noqa: BLE001 — detection must never break a render
        log.warning("omnivoice unit-pace: cannot read %s for beat detection (%s); "
                    "falling back to whisper gaps", os.path.basename(audio_path or ""), e)
        return None
    if n_ch > 1:                       # interleaved → take the first channel
        samples = samples[::n_ch]
    amp = OMNIVOICE_UNIT_SILENCE_AMP
    min_len = max(1, int(OMNIVOICE_UNIT_SILENCE_MIN_S * sr))
    total = len(samples)
    # A clause join has SPEECH ON BOTH SIDES. Anchoring on the wav's speech extent (rather
    # than a fixed edge margin) is what rejects LEADING/TRAILING silence: video 306 scene 42
    # opens with 0.052 s of digital zeros starting at 0.053 s, which a margin-based guard
    # counted as a join — it would have split a clause off the front of the scene.
    quiet_thr = int(32768 * (10 ** (OMNIVOICE_JOIN_QUIET_DB / 20.0)))
    first = next((i for i, v in enumerate(samples) if abs(v) >= quiet_thr), None)
    if first is None:
        return []
    last = next((i for i in range(total - 1, -1, -1) if abs(samples[i]) >= quiet_thr), first)
    out: list[float] = []
    start = None
    for i, v in enumerate(samples):
        if -amp <= v <= amp:
            if start is None:
                start = i
        else:
            if start is not None and (i - start) >= min_len and start > first and i <= last:
                out.append((start + i) / 2.0 / sr)
            start = None
    return out


def _omnivoice_split_word_units(words: list[dict],
                                audio_path: str | None = None) -> list[list[dict]]:
    """Split one scene's measured words into CLAUSE UNITS.

    PRIMARY signal = the scene's OWN AUDIO (`audio_path`): the digital-silence run our
    punctuation-beat concat wrote at each clause join (_omnivoice_beat_boundaries). This is
    ground truth — those runs mark exactly the clauses the TTS worker synthesized as separate
    generate() calls — and it is verified 59/59 against the worker's own split on job 313.

    FALLBACK = inter-word gaps >= OMNIVOICE_UNIT_SPLIT_GAP_S in the measured word map, used
    only when the wav cannot be read. Whisper is unreliable for this: it extends a word's end
    across short low-level audio, so a real 0.129 s pause (job 313 scene 23) is reported as
    gap 0.00 and the whole clause split is missed. That is why the audio signal leads.

    Either way the boundaries are compared against the SAME measured timings, so nothing can
    desync; and units shorter than OMNIVOICE_UNIT_MIN_WORDS are forward-merged into the next
    unit, with a too-short trailing remainder merged BACKWARD — the same accumulate-and-merge
    shape as the TTS-side clause merger, so a 1-2 word fragment never becomes its own
    independently-measured unit (its median would be one word's quirk).

    Returns [[w, ...], ...]; a scene with no qualifying boundary returns ONE unit."""
    if not words:
        return []
    ws = sorted(words, key=lambda w: w["start"])
    raw: list[list[dict]] = []
    cuts = _omnivoice_beat_boundaries(audio_path) if audio_path else None
    if cuts:
        # AUDIO-DRIVEN. Assign each word to the unit given by how many beat boundaries
        # precede its START, then group consecutive words with the same index.
        #
        # Word STARTS only — never word ends. Whisper habitually extends a word's `end`
        # PAST the following silence (that is the whole defect being worked around: on job
        # 313 scene 23 it reported end == next start across a 0.129 s pause). Any rule that
        # asks "does this cut fall after the previous word ended?" therefore rejects the very
        # boundaries the audio proves are real — measured: such a guard recovered only 8/18
        # true splits, while start-based assignment recovers 18/18. A start is safe because
        # the boundary lies in silence, which is always after the preceding word began.
        import bisect as _bisect

        cur = [ws[0]]
        idx0 = _bisect.bisect_right(cuts, ws[0]["start"])
        for b in ws[1:]:
            idx = _bisect.bisect_right(cuts, b["start"])
            if idx != idx0:
                raw.append(cur)
                cur = [b]
                idx0 = idx
            else:
                cur.append(b)
        raw.append(cur)
    else:
        cur = [ws[0]]
        for a, b in zip(ws, ws[1:]):
            if (b["start"] - a["end"]) >= OMNIVOICE_UNIT_SPLIT_GAP_S:
                raw.append(cur)
                cur = [b]
            else:
                cur.append(b)
        raw.append(cur)
    if len(raw) < 2:
        return raw
    out: list[list[dict]] = []
    buf: list[dict] = []
    for unit in raw:
        buf = buf + unit
        if len(buf) >= OMNIVOICE_UNIT_MIN_WORDS:
            out.append(buf)
            buf = []
    if buf:                       # trailing remainder too short → fold into the previous unit
        if out:
            out[-1] = out[-1] + buf
        else:
            out.append(buf)
    return out


def _omnivoice_pace_factor(words: list[dict], wall_target: float,
                           minus_s: float = 0.0) -> tuple[float | None, float | None, bool]:
    """The atempo factor for ONE span of words (a unit, or a whole scene on the fallback).

    Single source of truth for the correction formula, so the per-unit path and the
    whole-scene fallback can never drift apart. Returns (factor, pace, railed_by_wall);
    factor is None when the span is too short to measure.

      • factor = median word pace / OMNIVOICE_TARGET_MS_PER_SYL. <1 SLOWS a too-fast span;
        >1 NUDGES a slower-than-target span up toward the common pace (bounded by CEIL —
        never a real rush, per the owner's "never read faster to shorten a video" rule).
      • PERCEIVED-pace ceiling: stretching by 1/factor turns the wall-clock rate into
        wall/factor, so requiring wall/factor <= ceiling means factor >= wall/ceiling. A span
        already at/past the ceiling as HEARD is therefore never slowed further.
      • clamped to [OMNIVOICE_TARGET_ATEMPO_FLOOR, OMNIVOICE_TARGET_ATEMPO_CEIL].

    `minus_s` (deliberate beat silence to discount from the wall metric) is only meaningful
    for a WHOLE-SCENE span: on the per-unit path the beats sit BETWEEN units, so no unit's
    span contains one and the discount is 0."""
    pace = _scene_pace_ms_per_syl(words) if words else None
    if not pace:
        return None, None, False
    # GUARD 1 (lead-in drop): a unit-initial word far slower than the unit's own median is
    # the chunk lead-in artifact, not tempo. Measure everything else without it. Needs >=4
    # words left so the median stays meaningful; keeps the ORIGINAL pace if dropping makes
    # the span unmeasurable.
    span = words
    if OMNIVOICE_LEADIN_DROP_RATIO > 0 and len(words) >= 5:
        w0 = words[0]
        lead_ms = (float(w0["end"]) - float(w0["start"])) * 1000.0
        lead_rate = lead_ms / max(1, _spoken_weight(w0["word"]))
        if lead_rate > OMNIVOICE_LEADIN_DROP_RATIO * pace:
            p2 = _scene_pace_ms_per_syl(words[1:])
            if p2:
                span, pace = words[1:], p2
    factor = pace / OMNIVOICE_TARGET_MS_PER_SYL
    railed = False
    wall = _scene_wall_ms_per_syl(span, max_gap_s=OMNIVOICE_WALL_MAX_GAP_S, minus_s=minus_s)
    if wall_target > 0 and wall and wall > 0:
        wall_floor = wall / wall_target
        if wall_floor > factor:
            railed = True
            factor = wall_floor
    factor = min(OMNIVOICE_TARGET_ATEMPO_CEIL, max(OMNIVOICE_TARGET_ATEMPO_FLOOR, factor))
    # GUARDS 2-3 apply to SPEED-UPS ONLY (factor > 1.0). A slow-down can never rush a word,
    # so it is left exactly as computed above.
    if factor > 1.0:
        rates = [((float(w["end"]) - float(w["start"])) * 1000.0) / max(1, _spoken_weight(w["word"]))
                 for w in span]
        rates = [r for r in rates if r > 0]
        if rates:
            fastest = min(rates)
            # GUARD 3 (variance skip): still internally uneven after the lead-in drop ->
            # one uniform atempo would only crush the fast half. Do not speed up at all.
            if (OMNIVOICE_UNIT_VARIANCE_SKIP > 0
                    and max(rates) / fastest > OMNIVOICE_UNIT_VARIANCE_SKIP):
                factor = 1.0
            # GUARD 2 (per-word floor): cap the speed-up so the unit's FASTEST word does not
            # land below the "clearly rushed" rate. Never drops below 1.0 (cap, not slow-down).
            elif OMNIVOICE_MIN_WORD_MS_PER_SYL > 0:
                factor = max(1.0, min(factor, fastest / OMNIVOICE_MIN_WORD_MS_PER_SYL))
    return factor, pace, railed


def _omnivoice_unit_bounds(units: list[list[dict]]) -> list[float]:
    """Cut points (seconds, in the scene wav's own timeline) BETWEEN consecutive units.

    Each cut is the MIDPOINT of the silence between one unit's last word end and the next
    unit's first word start, so a cut can never land inside a word — the same "never clip a
    word" convention the TTS-side edge trimming uses, applied to the one piece of
    information we have here (whisper word boundaries). Returns len(units)-1 values."""
    cuts = []
    for a, b in zip(units, units[1:]):
        lo, hi = float(a[-1]["end"]), float(b[0]["start"])
        cuts.append((lo + hi) / 2.0 if hi > lo else hi)
    return cuts


def _omnivoice_remap_time(t: float, bounds: list[float], new_durs: list[float]) -> float:
    """Map a timestamp from the ORIGINAL scene timeline to the RETIMED one, piecewise.

    `bounds` has len(new_durs)+1 entries: [0.0, cut_1, ..., cut_n-1, original_total] — the
    segment EDGES in the original timeline, so every segment (including the last) has a
    known original length. `new_durs` = the MEASURED duration of each retimed segment.

    Segment i therefore starts at sum(new_durs[:i]) in the new timeline and runs at its own
    local scale new_durs[i] / (bounds[i+1] - bounds[i]). Using the MEASURED durations — not
    the theoretical 1/factor — is what keeps captions locked to the audio: atempo's output
    length is only approximately input/factor, and with several segments per scene those
    small errors would otherwise accumulate into exactly the kind of caption drift this
    project has hit before.

    The map is continuous at every cut (segment i's end maps to segment i+1's start) and
    monotonic, so a word never inverts. A timestamp past the end keeps the last segment's
    scale rather than clamping, so a caption word whose end slightly exceeds the probed
    duration still lands after its own start."""
    n = len(new_durs)
    if n == 0 or len(bounds) < n + 1:
        return t
    i = 0
    for k in range(n - 1, -1, -1):
        if t >= bounds[k]:
            i = k
            break
    seg_orig_len = bounds[i + 1] - bounds[i]
    scale = (new_durs[i] / seg_orig_len) if seg_orig_len > 0 else 1.0
    return sum(new_durs[:i]) + (t - bounds[i]) * scale


def _omnivoice_retime_scene(scene, units: list[list[dict]], factors: list[float],
                            work: str) -> tuple[str, list[float], list[float]] | None:
    """Cut the scene wav at the unit boundaries, atempo each piece by its OWN factor, and
    concatenate back into one wav.

    Returns (out_path, bounds, measured_new_durs) or None, where `bounds` are the segment
    EDGES in the ORIGINAL timeline — [0.0, cut_1, ..., cut_n-1, original_total], one more
    entry than there are segments — so _omnivoice_remap_time knows every segment's original
    length, including the last one.

    Cuts fall in the silence between clauses (_omnivoice_unit_bounds). Every sample is kept:
    segment 0 starts at 0.0 (so leading silence travels with the first clause) and the last
    segment runs to EOF. `-ss/-t` are output-side options (sample-accurate on PCM) and the
    pieces are re-joined with the concat DEMUXER + `-c copy` — all pieces are already
    48 kHz mono pcm_s16le, so the join is lossless and adds no re-encode artifacts.

    Fail-safe: any ffmpeg failure or a missing/implausible piece returns None and the caller
    leaves the scene completely untouched (never a half-retimed scene)."""
    orig_total = _probe_duration(scene.audioPath)
    if orig_total <= 0:
        return None
    cuts = [c for c in _omnivoice_unit_bounds(units) if 0.0 < c < orig_total]
    if len(cuts) != len(units) - 1:
        return None                         # a cut fell outside the wav — don't risk it
    bounds = [0.0] + cuts + [orig_total]    # segment EDGES: len == len(units) + 1
    parts, new_durs = [], []
    for i, f in enumerate(factors):
        a = bounds[i]
        # Last segment runs to EOF (no -t) so trailing silence is never dropped, but its
        # ORIGINAL length is still known (bounds[-1]) for the caption remap.
        d = (bounds[i + 1] - a) if i + 1 < len(bounds) - 1 else None
        if d is not None and d <= 0.02:
            return None                     # degenerate slice — abandon, keep the scene as-is
        seg = os.path.join(work, f"ovpace_{scene.scene:03d}_u{i}.wav")
        # Slice with the atrim FILTER, never with -ss/-t. As OUTPUT options those cap the
        # duration AFTER the filter chain, so `-t d -af atempo=1.2` writes d seconds of
        # OUTPUT (consuming d*1.2 of input) — measured: a 2.140 s slice at atempo 1.2 came
        # out 2.140 s instead of 1.783 s, i.e. the wrong audio in the wrong length, and the
        # caption remap then computed a local scale of 1.0. atrim cuts on the INPUT timeline
        # before atempo sees it; asetpts rebases the slice to t=0 so the encoder writes it
        # from the start.
        trim = f"atrim=start={a:.6f}" + (f":end={a + d:.6f}" if d is not None else "")
        args = ["-i", scene.audioPath,
                "-af", f"{trim},asetpts=N/SR/TB,{_atempo_filter(f)}",
                "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", seg]
        try:
            _run_ffmpeg(args, f"omnivoice unit-pace scene {scene.scene} unit {i}")
        except Exception as e:  # noqa: BLE001
            log.warning("omnivoice unit-pace scene %s unit %s ffmpeg failed (%s); "
                        "leaving the whole scene unchanged", scene.scene, i, e)
            return None
        nd = _probe_duration(seg)
        if not os.path.isfile(seg) or nd <= 0:
            return None
        parts.append(seg)
        new_durs.append(nd)
    out = os.path.join(work, f"ovpace_{scene.scene:03d}.wav")
    list_path = os.path.join(work, f"ovpace_{scene.scene:03d}_concat.txt")
    try:
        with open(list_path, "w", encoding="utf-8") as fh:
            for p in parts:
                fh.write(f"file '{p.replace(chr(92), '/')}'\n")
        _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", out],
                    f"omnivoice unit-pace concat scene {scene.scene}")
    except Exception as e:  # noqa: BLE001
        log.warning("omnivoice unit-pace scene %s concat failed (%s); leaving unchanged",
                    scene.scene, e)
        return None
    if not os.path.isfile(out) or _probe_duration(out) <= 0:
        return None
    return out, bounds, new_durs


def _omnivoice_normalize_joins(scene, work: str) -> list[tuple[float, float]]:
    """Drive the total quiet at every clause join in ONE scene wav to OMNIVOICE_JOIN_TARGET_S.

    Returns [(position_s, delta_s), ...] in the scene's PRE-edit timeline, ascending — the
    caller shifts caption timestamps by the cumulative delta. Empty list = nothing changed.

    Only the run of TRUE DIGITAL ZEROS that our own concat wrote is edited (see the knob
    block): samples are inserted into it, or deleted from it, and nothing else in the wav is
    read-modified-written. The model's clause-edge decay is MEASURED (to know the current
    total quiet) but never altered, so a soft consonant tail cannot be clipped.

    Always writes a NEW file under `work` and repoints scene.audioPath — never edits in place,
    because audioPath may still point into the shared TTS cache."""
    if OMNIVOICE_JOIN_TARGET_S <= 0:
        return []
    path = scene.audioPath
    try:
        import wave
        from array import array as _array

        with wave.open(path, "rb") as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                return []
            sr, n = w.getframerate(), w.getnframes()
            data = _array("h")
            data.frombytes(w.readframes(n))
    except Exception as e:  # noqa: BLE001 — never break a render over a cosmetic pause
        log.warning("omnivoice join-normalize: cannot read %s (%s); left unchanged",
                    os.path.basename(path or ""), e)
        return []

    amp = OMNIVOICE_UNIT_SILENCE_AMP
    quiet_thr = int(32768 * (10 ** (OMNIVOICE_JOIN_QUIET_DB / 20.0)))
    min_zeros = int(OMNIVOICE_JOIN_MIN_ZEROS_S * sr)
    target_n = int(round(OMNIVOICE_JOIN_TARGET_S * sr))
    edge = int(0.05 * sr)
    total = len(data)

    # Locate the interior digital-zero runs (one per clause join). Same SPEECH-ON-BOTH-SIDES
    # anchor as _omnivoice_beat_boundaries: leading/trailing silence is not a join, and
    # padding it would add a phantom pause to the START of a scene (video 306 scene 42).
    first = next((i for i, v in enumerate(data) if abs(v) >= quiet_thr), None)
    if first is None:
        return []
    last = next((i for i in range(total - 1, -1, -1) if abs(data[i]) >= quiet_thr), first)
    zruns, s0 = [], None
    min_run = max(1, int(OMNIVOICE_UNIT_SILENCE_MIN_S * sr))
    for i, v in enumerate(data):
        if -amp <= v <= amp:
            if s0 is None:
                s0 = i
        else:
            if s0 is not None and (i - s0) >= min_run and s0 > first and i <= last:
                zruns.append((s0, i))
            s0 = None
    if not zruns:
        return []

    edits: list[tuple[int, int, int]] = []   # (zero_run_start, zero_run_end, delta_samples)
    clipped = []
    for zs, ze in zruns:
        # measure the FULL quiet extent around this run (our zeros + the model's decay)
        qs = zs
        while qs > 0 and abs(data[qs - 1]) < quiet_thr:
            qs -= 1
        qe = ze
        while qe < total and abs(data[qe]) < quiet_thr:
            qe += 1
        cur = qe - qs
        delta = target_n - cur
        if delta < 0:
            # SHRINK, but only by consuming our own zeros — never the model's decay.
            removable = max(0, (ze - zs) - min_zeros)
            want = -delta
            delta = -min(want, removable)
            if want > removable:
                clipped.append((round(zs / sr, 3), round(cur / sr, 3),
                                round((cur - removable) / sr, 3)))
        if delta:
            edits.append((zs, ze, delta))

    if not edits:
        return []
    out = _array("h")
    prev = 0
    for zs, ze, delta in edits:
        mid = (zs + ze) // 2
        if delta > 0:
            out.extend(data[prev:mid])
            out.extend(_array("h", [0]) * delta)
            prev = mid
        else:
            k = -delta
            rs = min(max(zs, mid - k // 2), ze - k)   # removal window stays INSIDE the zeros
            out.extend(data[prev:rs])
            prev = rs + k
    out.extend(data[prev:])

    dst = os.path.join(work, f"ovjoin_{scene.scene:03d}.wav")
    try:
        with wave.open(dst, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(out.tobytes())
    except Exception as e:  # noqa: BLE001
        log.warning("omnivoice join-normalize: cannot write %s (%s); left unchanged", dst, e)
        return []
    if clipped:
        log.info("omnivoice join-normalize scene %s: %d join(s) could not reach the %.2fs "
                 "target — the model's own decay already exceeds it and only OUR silence may "
                 "be removed. [(pos, from, floor)]: %s",
                 scene.scene, len(clipped), OMNIVOICE_JOIN_TARGET_S, clipped)
    scene.audioPath = dst
    scene.durationS = None
    return [(((zs + ze) // 2) / sr, delta / sr) for zs, ze, delta in edits]


def _omnivoice_uniform_pace(scenes, word_map: dict, work: str,
                            pace_word_map: dict | None = None) -> dict:
    """OMNIVOICE PER-SCENE UNIFORM PACE (owner request 2026-07-28).

    ⚠ EXCLUSIVITY (2026-07-31 separation): this is the ONLY pace pass an OmniVoice render
    gets. assemble_footage dispatches on _omnivoice_owns_pace(req) — OmniVoice skips BOTH
    shared passes (_normalize_scene_pace, _auto_target_pace) and runs this instead. Never
    make them cumulative: each is a phase-vocoder stretch, and chaining them multiplies the
    factors (video 258: 0.915 then ≈0.765 → 0.70 total, +43% length, 282 ms/syll, smeared
    voice). The dispatch is a top-level engine check at the call sites; the shared passes
    themselves stay engine-blind.

    Where _auto_target_pace applies ONE global atempo
    (converges the OVERALL median to target but does NOT flatten scene-to-scene variance),
    this retimes EACH scene INDIVIDUALLY toward a COMMON OmniVoice target ms/syllable, so
    every scene reads at ~the SAME pace (uniform) AND slower overall — exactly the owner's
    "tốc độ đọc đồng đều toàn video" requirement. OmniVoice-only; F5/VieNeu/None keep
    _auto_target_pace unchanged.

    WORD-AWARE (PER-UNIT) CORRECTION — owner decision 2026-08-02, REPLACING the previous
    whole-scene single-factor correction (see OMNIVOICE_UNIT_SPLIT_GAP_S for the job-308
    evidence: a scene averaging a healthy 187 ms/syll while its two clauses sat at 280 and
    173, so one aggregate factor dragged the already-slow clause further out).

    Per scene:
      • the scene's WHISPER words (pace_word_map when supplied — always a whisper map — else
        the caption word_map) are split into clause UNITS at their own measured gaps
        (_omnivoice_split_word_units).
      • each unit gets its OWN factor from _omnivoice_pace_factor: median word pace vs
        target, bounded by the PERCEIVED-pace ceiling computed PER UNIT (an already-slow
        clause is no longer averaged away by a fast neighbour), clamped to
        [OMNIVOICE_TARGET_ATEMPO_FLOOR, OMNIVOICE_TARGET_ATEMPO_CEIL].
      • the scene wav is cut at the silences between units, each piece is atempo'd by its own
        factor, and the pieces are concatenated back (_omnivoice_retime_scene).
      • caption word_map timestamps are remapped through the resulting PIECEWISE-linear time
        map built from the MEASURED length of every retimed piece (_omnivoice_remap_time), so
        each caption word follows its own clause and later clauses carry the correct
        cumulative offset. durationS is cleared to force a re-probe.
      • FALLBACK: a scene with fewer than 2 measurable units takes the old whole-scene
        single-factor path unchanged. Fail-safe: any ffmpeg failure leaves the scene as-is.

    Measuring per unit still SELF-CORRECTS OmniVoice's loanword timing quirk (the original
    skip reason) — each span is judged on its own audio — and now also catches the case where
    the quirk affects only PART of a scene.

    Returns a debug dict {scene: (pace_before, effective_factor)} for logging/validation;
    the per-unit breakdown is logged separately."""
    if OMNIVOICE_TARGET_MS_PER_SYL <= 0 or not word_map:
        log.info("omnivoice uniform-pace: DISABLED (OMNIVOICE_TARGET_MS_PER_SYL<=0 or no word_map)"
                 " — no OmniVoice pace normalization applied")
        return {}
    _measure_map = pace_word_map if pace_word_map else word_map
    # PERCEIVED-pace ceiling: no scene is ever slowed past this heard rate (see the
    # OMNIVOICE_WALL_TARGET_RATIO note). 0 disables the ceiling (pre-2026-07-29 behavior).
    _wall_target = OMNIVOICE_TARGET_MS_PER_SYL * OMNIVOICE_WALL_TARGET_RATIO
    debug: dict = {}
    changed = 0
    before: list[float] = []
    after: list[float] = []
    railed_by_wall = 0
    unit_debug: dict = {}
    for s in scenes:
        ws = _measure_map.get(s.scene)
        if not ws:
            continue
        # Boundaries come from the scene's OWN AUDIO (the beat silences we wrote), with the
        # whisper-gap rule only as a fallback — see _omnivoice_split_word_units.
        units = _omnivoice_split_word_units(ws, audio_path=s.audioPath)
        # ---- measure every unit; a unit too short to measure inherits the scene's factor
        factors: list[float] = []
        paces: list[float | None] = []
        measurable = 0
        for u in units:
            f, p, railed = _omnivoice_pace_factor(u, _wall_target)
            factors.append(f if f else 1.0)
            paces.append(p)
            if f:
                measurable += 1
                railed_by_wall += 1 if railed else 0
        scene_pace = _scene_pace_ms_per_syl(ws)
        if not scene_pace:
            continue
        before.append(scene_pace)
        # ---- FALLBACK: one unit (or none measurable) → whole-scene single factor, i.e.
        # exactly the pre-2026-08-02 behavior for that scene. Expected and fine: a
        # single-clause scene has nothing to balance against.
        if len(units) < 2 or measurable < 2:
            f, p, railed = _omnivoice_pace_factor(
                ws, _wall_target, minus_s=float(getattr(s, "beatSilenceS", 0.0) or 0.0))
            if not f:
                continue
            if railed:
                railed_by_wall += 1
            if abs(f - 1.0) < 0.005:
                after.append(scene_pace)
                continue
            tgt = os.path.join(work, f"ovpace_{s.scene:03d}.wav")
            try:
                _run_ffmpeg(
                    ["-i", s.audioPath, "-af", _atempo_filter(f),
                     "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", tgt],
                    f"omnivoice uniform-pace scene {s.scene}",
                )
                if os.path.isfile(tgt):
                    scale = 1.0 / f
                    s.audioPath = tgt
                    s.durationS = None
                    changed += 1
                    for w in word_map.get(s.scene, []):
                        w["start"] = w["start"] * scale
                        w["end"] = w["end"] * scale
                    debug[s.scene] = (round(scene_pace, 1), round(f, 3))
                    unit_debug[s.scene] = [(round(p or 0, 1), round(f, 3), len(ws), "whole")]
                    after.append(scene_pace * scale)
                else:
                    after.append(scene_pace)
            except Exception as e:
                log.warning("omnivoice uniform-pace scene %s ffmpeg failed (%s); leaving unchanged",
                            s.scene, e)
                after.append(scene_pace)
            continue
        # ---- PER-UNIT path. Nothing to do when every unit is already on pace.
        if all(abs(f - 1.0) < 0.005 for f in factors):
            after.append(scene_pace)
            continue
        retimed = _omnivoice_retime_scene(s, units, factors, work)
        if not retimed:
            after.append(scene_pace)
            continue
        out, bounds, new_durs = retimed
        old_total = bounds[-1] - bounds[0]
        s.audioPath = out
        s.durationS = None  # force re-probe of the retimed audio downstream
        changed += 1
        # CAPTIONS: remap each caption word through the PIECEWISE map (its own unit's
        # measured scale + the cumulative measured length of every earlier unit). Mapping is
        # by TIME, not by index, so it is valid even when word_map is the CTC caption map
        # while the units were derived from the whisper measure map — the two describe the
        # same audio timeline but not necessarily the same tokens.
        for w in word_map.get(s.scene, []):
            w["start"] = _omnivoice_remap_time(w["start"], bounds, new_durs)
            w["end"] = _omnivoice_remap_time(w["end"], bounds, new_durs)
        eff = (old_total / sum(new_durs)) if sum(new_durs) > 0 else 1.0
        debug[s.scene] = (round(scene_pace, 1), round(eff, 3))
        unit_debug[s.scene] = [
            (round(p, 1) if p else None, round(f, 3), len(u), " ".join(x["word"] for x in u)[:28])
            for u, f, p in zip(units, factors, paces)
        ]
        after.append(scene_pace * (1.0 / eff) if eff else scene_pace)

    # ---- CLAUSE-JOIN QUIET NORMALIZATION -------------------------------------------------
    # Separate pass over EVERY scene, whichever pace path it took above (including scenes that
    # needed no retiming at all): join length must be consistent across the whole video, and a
    # scene already on pace can still carry a 0.46 s join. Runs AFTER retiming so it corrects
    # the atempo amplification too, rather than being re-stretched by it.
    join_scenes = 0
    join_delta_s = 0.0
    for s in scenes:
        edits = _omnivoice_normalize_joins(s, work)
        if not edits:
            continue
        join_scenes += 1
        join_delta_s += sum(d for _p, d in edits)
        # Captions: every word after an edit shifts by that edit's delta. The edited samples
        # are digital silence, so no word can lie inside the edited window.
        for w in word_map.get(s.scene, []):
            w["start"] = w["start"] + sum(d for p, d in edits if p <= w["start"])
            w["end"] = w["end"] + sum(d for p, d in edits if p <= w["end"])
    if join_scenes:
        log.info("omnivoice join-normalize: %d scene(s) adjusted to a %.2fs clause-join target; "
                 "net duration change %+.2fs (only digital-silence samples added/removed)",
                 join_scenes, OMNIVOICE_JOIN_TARGET_S, join_delta_s)

    # Variance BEFORE vs AFTER = the objective "did it become even?" signal. NOTE the
    # `after` list here is a PROJECTION (pace * scale), not a measurement — see the
    # verify pass below, which re-whispers the retimed audio for the real number.
    def _spread(xs: list[float]) -> float:
        return (max(xs) - min(xs)) if len(xs) >= 2 else 0.0
    log.info("omnivoice uniform-pace: target=%.0f ms/syll band=[%.2f,%.2f]; retimed %d/%d scene(s); "
             "pace spread %.1f -> %.1f ms/syll PROJECTED (min/max before %.0f/%.0f, after %.0f/%.0f); "
             "%d scene(s) limited by the PERCEIVED-pace ceiling %.0f ms/syll (already slow enough as heard)",
             OMNIVOICE_TARGET_MS_PER_SYL, OMNIVOICE_TARGET_ATEMPO_FLOOR, OMNIVOICE_TARGET_ATEMPO_CEIL,
             changed, len(scenes), _spread(before), _spread(after),
             min(before) if before else 0.0, max(before) if before else 0.0,
             min(after) if after else 0.0, max(after) if after else 0.0,
             railed_by_wall, _wall_target)

    # PER-SCENE factors. Previously the debug dict was built and then DISCARDED by the
    # caller, so api.log held no record of how hard any individual scene was stretched and
    # the numbers had to be re-derived from the finished mp4. Scenes that hit a clamp rail
    # are called out explicitly — those are the ones that end up audibly off-pace.
    if debug:
        log.info("omnivoice uniform-pace per-scene {scene: (pace_before, effective_atempo)}: %s",
                 debug)
        # PER-UNIT detail — the trace needed to diagnose "the voice slows at X-Ys". The
        # scene aggregate above CANNOT show an intra-scene split (job 308 scene 4 averaged
        # to a healthy 187 ms/syll while its two clauses sat at 280 and 173), so the unit
        # breakdown is logged with each clause's own pace, factor, word count and text.
        log.info("omnivoice unit-pace per-unit {scene: [(pace_before, atempo, n_words, text)]}: %s",
                 unit_debug)
        railed_units = {sc: [(p, f, n, t) for (p, f, n, t) in us
                             if abs(f - OMNIVOICE_TARGET_ATEMPO_FLOOR) < 1e-6
                             or abs(f - OMNIVOICE_TARGET_ATEMPO_CEIL) < 1e-6]
                        for sc, us in unit_debug.items()}
        railed_units = {sc: us for sc, us in railed_units.items() if us}
        if railed_units:
            log.warning("omnivoice unit-pace: %d scene(s) have unit(s) CLAMPED at a rail (could "
                        "not reach target; these will read off-pace vs their neighbours): %s",
                        len(railed_units), railed_units)
        # Intra-scene spread AFTER correction, projected per unit: the number that would
        # have exposed job 308 scene 4 (107 ms/syll split inside one scene) before shipping.
        worst = None
        for sc, us in unit_debug.items():
            proj = [p / f for (p, f, _n, _t) in us if p and f]
            if len(proj) >= 2:
                spread = max(proj) - min(proj)
                if worst is None or spread > worst[1]:
                    worst = (sc, spread)
        if worst:
            log.info("omnivoice unit-pace: worst remaining INTRA-scene spread = %.0f ms/syll "
                     "(scene %s) PROJECTED across its units", worst[1], worst[0])

        # CONVERGENCE REPORT (owner 2026-08-03). A clamped factor is NOT success: the rail
        # bounds time-stretch artifacts, it does not mean the clause reached the target. Any
        # unit whose PROJECTED delivered pace is still outside the acceptable band is listed
        # with its miss, so a structurally-unfixable clause is visible instead of silently
        # counted as corrected.
        unconverged = []
        for sc, us in unit_debug.items():
            for (p, f, n, t) in us:
                if not p or not f:
                    continue
                delivered = p / f
                dev = delivered - OMNIVOICE_TARGET_MS_PER_SYL
                if abs(dev) > OMNIVOICE_MAX_PACE_DEVIATION_MS:
                    at_rail = (abs(f - OMNIVOICE_TARGET_ATEMPO_FLOOR) < 1e-6
                               or abs(f - OMNIVOICE_TARGET_ATEMPO_CEIL) < 1e-6)
                    unconverged.append((sc, round(delivered), round(dev),
                                        "RAILED" if at_rail else "wall-ceiling", t))
        if unconverged:
            log.warning(
                "omnivoice unit-pace: %d unit(s) did NOT converge to within +/-%.0f ms/syll of "
                "the %.0f target — these will still read off-pace vs the rest of the video. "
                "{scene: (delivered_ms_syl, deviation, limited_by, text)}: %s",
                len(unconverged), OMNIVOICE_MAX_PACE_DEVIATION_MS,
                OMNIVOICE_TARGET_MS_PER_SYL, unconverged)
        else:
            log.info("omnivoice unit-pace: ALL units converged to within +/-%.0f ms/syll of the "
                     "%.0f target", OMNIVOICE_MAX_PACE_DEVIATION_MS, OMNIVOICE_TARGET_MS_PER_SYL)

    _omnivoice_pace_verify(scenes, debug)
    return debug


def _omnivoice_pace_verify(scenes, debug: dict) -> None:
    """Re-whisper the RETIMED audio and log the pace we ACTUALLY delivered.

    The pass above reports `pace * scale`, an analytic projection that assumes atempo
    landed perfectly and that the word metric is what a listener hears. Neither held on
    video 258: the projection said "spread 90 -> 22.4 ms/syll" while the delivered mp4
    measured 188-282 ms/syll. This pass costs one extra whisper over the retimed scene
    wavs and logs BOTH metrics — the word median (what the pass optimizes) and the
    wall-clock rate including inter-word silence (what is perceived). Purely
    observational: it never modifies audio. Disable with OMNIVOICE_PACE_VERIFY=0."""
    if not OMNIVOICE_PACE_VERIFY or not debug:
        return
    try:
        res = _run_cf_worker(
            "whisper_worker.py",
            {"items": [{"scene": s.scene, "audioPath": s.audioPath} for s in scenes],
             "model": WHISPER_MODEL, "device": WHISPER_DEVICE, "compute": WHISPER_COMPUTE,
             "language": "vi", "wordTimestamps": True},
            timeout=1200,
        )
    except Exception as e:
        log.warning("omnivoice pace-verify whisper failed (%s); skipping verification", e)
        return
    words_by_scene = {r["scene"]: [w for seg in r.get("segments", []) for w in (seg.get("words") or [])]
                      for r in res.get("results", [])}
    word_paces, wall_paces, per_scene = {}, {}, {}
    for sc, ws in words_by_scene.items():
        wp = _scene_pace_ms_per_syl(ws)
        ll = _scene_wall_ms_per_syl(ws)
        if wp:
            word_paces[sc] = wp
        if ll:
            wall_paces[sc] = ll
        if wp and ll:
            per_scene[sc] = (round(wp), round(ll))
    if not word_paces:
        log.warning("omnivoice pace-verify: no measurable scene; nothing to report")
        return
    w, l = list(word_paces.values()), list(wall_paces.values())
    log.info("omnivoice pace-verify (MEASURED on retimed audio, target=%.0f): "
             "word-metric min/max %.0f/%.0f spread %.0f ms/syll; "
             "wall-clock min/max %.0f/%.0f spread %.0f ms/syll",
             OMNIVOICE_TARGET_MS_PER_SYL, min(w), max(w), max(w) - min(w),
             min(l) if l else 0.0, max(l) if l else 0.0, (max(l) - min(l)) if l else 0.0)
    log.info("omnivoice pace-verify per-scene {scene: (word_ms_syl, wall_ms_syl)}: %s", per_scene)
    # The gap between the two metrics is the reason a scene can be "on target" yet sound
    # slow. Surfaced as a single number so it can be tracked job to job.
    if l:
        log.info("omnivoice pace-verify: wall-clock is %.0f%% slower than the word metric on "
                 "average (inter-word silence not counted by the optimized metric)",
                 100.0 * (sum(l) / len(l)) / (sum(w) / len(w)) - 100.0)


def _omnivoice_owns_pace(req) -> bool:
    """True when OmniVoice owns pace normalization for this request, i.e. the shared
    passes (_normalize_scene_pace / _auto_target_pace) must NOT run.

    The two pace flows are MUTUALLY EXCLUSIVE and must stay that way: each one is a
    phase-vocoder time-stretch, and running them in sequence multiplies the factors.
    Video 258 measured STEP 0 at 0.915 then the OmniVoice pass at ≈0.765 → 0.70 total
    (+43% length, 282 ms/syll, audibly smeared) — that is the defect this predicate
    prevents. With captions on, all three passes would otherwise stack.

    This is a TOP-LEVEL dispatch test, deliberately the only kind of engine check allowed
    outside this section: the caller picks a flow, it does not alter one. Nothing inside
    _normalize_scene_pace or _auto_target_pace knows an engine exists."""
    return (getattr(req, "engine", "") or "").strip().lower() == "omnivoice"


def _omnivoice_post_pace(req, word_map: dict, work: str,
                         pace_word_map: dict | None = None) -> None:
    """THE entry point into this section — the only OmniVoice reference in assemble_footage.

    Self-gating: a NO-OP for every engine except omnivoice, so the caller does not test the
    engine and the shared pace flow stays engine-blind. Requires the caption word_map (pace
    is measured from whisper word spans), so it is called from the same place in assembly
    where the shared pace step runs — after the caption whisper, before durations are read.

    Adding another OmniVoice-only assembly step later means calling it from HERE, not
    adding a branch upstream."""
    if (getattr(req, "engine", "") or "").strip().lower() != "omnivoice":
        return
    if not word_map:
        # No word_map (captions off) → nothing to measure; matches the shared flow, which
        # also skips target-pace when there are no timestamps to scale.
        log.info("omnivoice uniform-pace: SKIPPED (no caption word_map to measure pace from)")
        return
    _omnivoice_uniform_pace(req.scenes, word_map, work, pace_word_map=pace_word_map)


# =============================================================================
# END OMNIVOICE-ONLY SECTION
# =============================================================================


@router.post("/generate/assemble/footage")
def assemble_footage(req: FootageAssembleRequest):
    if not req.scenes:
        raise HTTPException(422, "Provide at least one scene.")
    for s in req.scenes:
        if not os.path.isfile(s.clipPath):
            raise HTTPException(422, f"scene {s.scene}: clip not found: {s.clipPath}")
        if not os.path.isfile(s.audioPath):
            raise HTTPException(422, f"scene {s.scene}: audio not found: {s.audioPath}")

    out_dir = req.outDir or os.path.join(CONTENT_OUTPUT_ROOT, req.page or "default", "video")
    os.makedirs(out_dir, exist_ok=True)
    safe_title = "".join(c for c in req.title if c.isalnum() or c in (" ", "-", "_")).strip() or "video"
    # Suffix the per-video id (unique per render) so re-rendering the SAME source
    # (same title) with different settings produces a DISTINCT file instead of
    # silently overwriting the previous output.
    suffix = f" (v{req.videoId})" if req.videoId else ""
    out_path = os.path.join(out_dir, f"{safe_title}{suffix}.mp4")

    # Pre-encode progress milestones (fix for the "Dựng video 0%" freeze). The render
    # step runs two whisper passes (caption alignment + pace) + pace normalization over
    # EVERY scene BEFORE any ffmpeg encode — minutes of work that emitted NO progress, so
    # the FE chip sat at "Dựng video 0%" the whole time. We can't cheaply get per-scene
    # progress from the whisper worker (that's the heavier option), so we emit coarse,
    # phase-named milestones across a reserved HEAD of the band; the encode then spans
    # [_RENDER_HEAD, 100]. Each msg carries "N%" so the FE chip stays monotonic.
    _pre_cb = getattr(_ff_progress_local, "cb", None)

    def _pre(pct: int, phase: str) -> None:
        if _pre_cb:
            try:
                _pre_cb(int(pct), f"Dựng video {int(pct)}% — {phase}")
            except Exception:
                pass

    with tempfile.TemporaryDirectory() as work:
        _pre(2, "chuẩn bị cảnh")
        # STEP 0a — scene-JOIN silence normalization. MUST run FIRST: it rewrites the wavs'
        # edges, and every downstream consumer (pace passes, caption whisper, durs) must see
        # the trimmed audio or karaoke timing would lead the voice by the leading-trim amount.
        _normalize_scene_joins(req.scenes, work)
        # STEP 0 — per-scene pace normalization (Issue 2, Option 1). MUST run before the
        # caption whisper: it retimes outlier scenes' audio (writing normalized wavs into
        # `work` and repointing scene.audioPath), so the caption-timing whisper below sees
        # the FINAL retimed audio and karaoke stays in sync. In-band scenes are untouched;
        # scenes with a protected slow-term ('prompt') are never sped up. No-op when
        # F5_PACE_NORMALIZE=0. See _normalize_scene_pace.
        #
        # ENGINE DISPATCH (not a branch inside the pace logic): OmniVoice runs its OWN
        # exclusive pace flow (_omnivoice_post_pace below), so the shared passes are skipped
        # for it — stacking both would time-stretch the same audio twice (see
        # _omnivoice_owns_pace for the measured defect). The predicate lives in the
        # OMNIVOICE-ONLY section; _normalize_scene_pace itself stays engine-blind.
        if not _omnivoice_owns_pace(req):
            _normalize_scene_pace(req.scenes, work)

        # Word timestamps for ALL scenes in ONE whisper load (drives karaoke captions).
        # Runs on the (possibly pace-normalized) audio so caption timing matches playback.
        word_map: dict[int, list] = {}
        if req.captions:
            # KARAOKE alignment backbone (item 2 durable fix). ctc makes the worker
            # forced-align the KNOWN caption text per scene instead of transcribing with
            # whisper — accurate on loanword-heavy Vietnamese where whisper mis-segments +
            # count-drifts (measured up to 1.27 s caption lag). Each scene falls back to
            # whisper inside the worker on any CTC failure. Passing the caption as
            # `narration` supplies the known text; audioPath is the FINAL (pace-normalized)
            # wav so timing matches playback.
            #
            # SPLIT KNOB (job-140 karaoke-drift fix, 2026-07-06): the karaoke word_map and the
            # TTS gap-shaper used to share ONE knob (CF_ALIGN_BACKEND). The owner's 2026-07-05
            # CTC revert set CF_ALIGN_BACKEND=whisper to disable the CTC GAP-SHAPER for AUDIO
            # reasons — but that also silently reverted the CTC KARAOKE word_map, re-introducing
            # the 0:37-0:42 "context engineering" caption drift (whisper heard 26 words for a
            # 22-token scene → interpolation branch → measured 1.28 s lag on "engineering"; CTC
            # forced-align produced exactly 22 words → the EXACT 1:1 branch → drift gone). These
            # are INDEPENDENT: caption timing is display-only and does not touch the reverted
            # audio path. CF_KARAOKE_ALIGN controls ONLY the caption word_map and DEFAULTS to
            # CF_ALIGN_BACKEND (so existing configs are unchanged); set it to ctc to get accurate
            # captions while keeping the gap-shaper on whisper.
            _align = os.getenv(
                "CF_KARAOKE_ALIGN",
                os.getenv("CF_ALIGN_BACKEND", "ctc"),
            ).strip().lower()
            _pre(4, "căn phụ đề")
            res = _run_cf_worker(
                "whisper_worker.py",
                {"items": [{"scene": s.scene, "audioPath": s.audioPath,
                            "narration": (s.caption or "")} for s in req.scenes],
                 "model": WHISPER_MODEL, "device": WHISPER_DEVICE, "compute": WHISPER_COMPUTE,
                 "language": "vi", "wordTimestamps": True, "align": _align},
                timeout=1200,
            )
            for r in res["results"]:
                word_map[r["scene"]] = [w for seg in r.get("segments", []) for w in (seg.get("words") or [])]
            _pre(8, "căn phụ đề xong")

        # PACE word_map (job-142 fix): pace MUST be measured from a WHISPER word_map, never a
        # CTC one — CTC's tight acoustic spans read ~140 ms/syll for a true ~200 ms/syll video
        # and cause a spurious ×1.42 stretch. If the caption map above was already whisper
        # (_align != "ctc"), reuse it (zero extra cost). Only when captions use CTC do we run a
        # dedicated whisper pass (no `narration` → plain transcribe → whisper word spans) whose
        # timestamps are used ONLY to measure pace, never rendered. This keeps captions on CTC
        # (drift fix intact) while pace stays on the pre-CTC baseline metric.
        pace_word_map: dict[int, list] = {}
        if req.captions and word_map:
            if _align == "ctc":
                try:
                    _pre(9, "đo nhịp đọc")
                    pres = _run_cf_worker(
                        "whisper_worker.py",
                        {"items": [{"scene": s.scene, "audioPath": s.audioPath}
                                   for s in req.scenes],
                         "model": WHISPER_MODEL, "device": WHISPER_DEVICE,
                         "compute": WHISPER_COMPUTE, "language": "vi",
                         "wordTimestamps": True, "align": "whisper"},
                        timeout=1200,
                    )
                    for r in pres["results"]:
                        pace_word_map[r["scene"]] = [w for seg in r.get("segments", [])
                                                     for w in (seg.get("words") or [])]
                except Exception as e:
                    log.warning("pace whisper pass failed (%s); measuring pace off the caption "
                                "map (may misread CTC pace)", e)
            else:
                pace_word_map = word_map  # captions already whisper → reuse, no extra pass

        # STEP 2 — AUTO TARGET-PACE (voice-independent, primary pace control). Runs AFTER
        # the caption whisper so it can (a) measure the whole video's pace and (b) SCALE the
        # caption timestamps by the uniform slowdown instead of re-whispering. Pace is measured
        # from `pace_word_map` (always whisper), the caption `word_map` is what gets scaled.
        # Ordering: per-scene slow-only normalize (STEP 0) trims the fast TAIL; this STEP 2
        # then slows the WHOLE video uniformly to the target floor. They compose cleanly —
        # normalize only ever slowed a few fast outliers; target-pace slows everything by one
        # factor computed from the post-normalize pace, so no confusing double-apply. When
        # req.captions is False there is no word_map to measure/scale, so target-pace is
        # skipped (captions off ⇒ pace-normalize STEP 0 still applies; a caption-less job is
        # rare and can use the manual F5_SPEED_SCALE knob if needed).
        #
        # Same ENGINE DISPATCH as STEP 0: OmniVoice takes the exclusive branch below instead.
        if req.captions and word_map and not _omnivoice_owns_pace(req):
            _auto_target_pace(req.scenes, word_map, work, pace_word_map=pace_word_map)

        # STEP 2 (OmniVoice) — the OTHER half of that dispatch, and the ONLY pace pass an
        # OmniVoice render gets. Unconditional CALL, self-gating callee: a no-op for
        # F5/VieNeu/None, which already took the shared branch above. The two are mutually
        # exclusive by construction, so scene audio is time-stretched by at most one pass.
        # All OmniVoice assembly behavior hangs off this one line (see OMNIVOICE-ONLY section).
        _omnivoice_post_pace(req, word_map, work, pace_word_map=pace_word_map)

        _pre(12, "chuẩn hoá nhịp")
        durs: list[float] = []
        for s in req.scenes:
            d = s.durationS or _probe_duration(s.audioPath)
            if d <= 0:
                raise HTTPException(422, f"scene {s.scene}: could not determine VO duration")
            durs.append(d)
        # head=12 → the encode spans [12,100]; the pre-encode milestones above filled 0..12
        # so the FE chip climbs 2%→12% through the whisper/pace phases, then 12%→100% while
        # encoding, instead of freezing at "Dựng video 0%" for the whole whisper stretch.
        prog = _make_assemble_progress(req.scenes, durs, req.bgmPath, req.addCredit,
                                       req.sourceLogo, req.sourceHandle, req.sourceName, head=12)

        # Caption TEXT = the CORRECT script narration; caption TIMING = whisper word
        # timestamps. Whisper mishears Vietnamese (e.g. "Agent harness" -> "Dân vật
        # A.A.A.Harnes"), so we never render its text — we lay the known narration
        # tokens onto whisper's measured timing (forced-alignment-lite). Falls back to
        # whisper words verbatim only when no narration is present. The .ass files are
        # built up front (cheap, single-threaded) so the parallel workers only do the
        # heavy ffmpeg encode; clips come back IN SCENE ORDER for a deterministic concat.
        items = []
        for s, dur in zip(req.scenes, durs):
            cap_words = _aligned_caption_words(s.caption, word_map.get(s.scene, []), dur, scene=s.scene)
            # Portrait output: pin the karaoke line right BELOW the footage band (top =
            # footage bottom + pad). Probed per scene from its own cut clip (cheap; all
            # cuts share the source dims anyway). None -> legacy bottom-anchored position.
            pin = _karaoke_pin_top_y(req.width, req.height, s.clipPath) if req.captions else None
            ass = (_build_karaoke_ass(cap_words, req.width, req.height, work, s.scene,
                                      pin_top_y=pin)
                   if req.captions else None)
            items.append((dur, dur, (s, dur, ass)))

        def _encode_footage_scene(p):
            s, dur, ass = p
            return _footage_scene_clip(s.clipPath, s.audioPath, dur, ass, req.width, req.height, req.fps,
                                       work, s.scene, src_audio_volume=req.srcAudioVolume)

        clips: list[str] = prog.run_parallel(items, _encode_footage_scene, _scene_encode_workers())
        _finish_video(
            work, clips, out_path,
            width=req.width, height=req.height, fps=req.fps,
            source_name=req.sourceName, source_link=req.sourceLink,
            bgm_path=req.bgmPath, bgm_volume=req.bgmVolume,
            logo_path=req.sourceLogo, handle=req.sourceHandle, add_credit=req.addCredit,
            outro_handle=req.outroHandle,
            prog=prog,
        )

    total = _probe_duration(out_path)
    return {
        "videoPath": out_path, "url": _media_url(out_path),
        "durationS": round(total, 2), "scenes": len(req.scenes),
        "width": req.width, "height": req.height,
    }


# --- Dubbed mode (Section F) — cut+concat keep-audio + burn VN subs --------
#
# Dubbed = keep the ORIGINAL audio+video, trim filler ranges, burn translated
# Vietnamese subtitles, append the existing source-credit slate. NO TTS, NO
# SDXL, NO stickman, NO Blender. ffmpeg + (already-run-at-ingest) whisper only.
# The media code below is the implementation spec
# (_workspace/phase6_media_dubbed_spec.md) pasted verbatim.


def _compute_keep_ranges(filler: list[dict], src_dur: float) -> list[tuple[float, float]]:
    """Complement of merged/clamped filler within [0, src_dur].

    filler: [{"start": float, "end": float, "reason": str}, ...] in SOURCE seconds.
    Returns keep-ranges [(s0, e0), (s1, e1), ...] sorted, non-overlapping, all
    inside [0, src_dur], each with e > s. Half-open [start, end) semantics.

    Raises ValueError (Vietnamese, user-facing) if nothing is left to keep.
    """
    if src_dur <= 0:
        raise ValueError("Không xác định được thời lượng nguồn để cắt.")

    # 1) Clamp each filler range to [0, src_dur] and drop degenerate/empty ones.
    clamped: list[tuple[float, float]] = []
    for f in (filler or []):
        s = max(0.0, min(float(f["start"]), src_dur))
        e = max(0.0, min(float(f["end"]), src_dur))
        if e > s:
            clamped.append((s, e))

    # 2) Empty filler => keep the whole source.
    if not clamped:
        return [(0.0, src_dur)]

    # 3) Sort + merge overlapping/adjacent filler ranges.
    clamped.sort()
    merged: list[list[float]] = [list(clamped[0])]
    for s, e in clamped[1:]:
        if s <= merged[-1][1]:            # overlap or touch -> extend
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    # 4) Complement within [0, src_dur].
    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in merged:
        if s > cursor:
            keep.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < src_dur:
        keep.append((cursor, src_dur))

    # 5) Drop sub-frame slivers (< 1 frame @ 30fps ~ 0.033s) that would produce
    #    empty trims; if everything was filler, keep is empty here.
    keep = [(s, e) for (s, e) in keep if (e - s) >= 0.04]

    if not keep:
        # User-facing Vietnamese: do NOT emit a 0-frame file.
        raise ValueError(
            "Toàn bộ nguồn bị đánh dấu là phần cần cắt — không còn nội dung nào "
            "để giữ lại. Vui lòng kiểm tra lại danh sách cắt (filler)."
        )
    return keep


def _remap_subs_post_trim(subs: list[dict],
                          keep_ranges: list[tuple[float, float]]) -> list[dict]:
    """Map subtitles from SOURCE seconds to OUTPUT seconds after filler is cut.

    subs: [{"start": float, "end": float, "text_vi": str}, ...] in SOURCE seconds,
          assumed sorted by start (sort defensively).
    keep_ranges: from _compute_keep_ranges (SOURCE seconds, sorted, non-overlapping).

    Returns remapped subs in OUTPUT seconds, sorted, each with end > start. A sub
    fully inside filler is dropped; a sub straddling keep/filler boundaries is
    clipped to the intersection with each keep-range it overlaps (so it may split
    into multiple output events). Half-open [start, end) avoids boundary
    double-counting.
    """
    out: list[dict] = []
    out_cursor = 0.0
    subs_sorted = sorted(subs or [], key=lambda s: (float(s["start"]), float(s["end"])))
    for ks, ke in keep_ranges:
        seg_len = ke - ks
        for sub in subs_sorted:
            ss = float(sub["start"])
            se = float(sub["end"])
            # Intersection of [ss, se) with the keep-range [ks, ke) (half-open).
            i_start = max(ss, ks)
            i_end = min(se, ke)
            if i_end <= i_start:
                continue  # no overlap with this keep-range (fully in filler or elsewhere)
            # Offset so this keep-range begins at out_cursor.
            o_start = out_cursor + (i_start - ks)
            o_end = out_cursor + (i_end - ks)
            out.append({
                "start": round(o_start, 3),
                "end": round(o_end, 3),
                "text_vi": sub["text_vi"],
            })
        out_cursor += seg_len
    out.sort(key=lambda s: (s["start"], s["end"]))
    return out


DUBBED_SUB_MAX_LINES = 2


def _wrap_two_lines(text: str, max_chars: int) -> str:
    """Greedy word-wrap into at most 2 rows joined by an ASS hard break (\\N).
    If the text exceeds two rows' worth, the remainder stays on row 2 (libass may
    shrink-to-fit visually, but we never emit a 3rd row)."""
    words = text.split()
    if not words:
        return ""
    lines, cur = [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        if cur and len(cand) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = cand
    if cur:
        lines.append(cur)
    if len(lines) > DUBBED_SUB_MAX_LINES:
        # collapse overflow into the 2nd line
        lines = [lines[0], " ".join(lines[1:])]
    return "\\N".join(lines)


def _build_dubbed_ass(subs: list[dict], width: int, height: int, work: str) -> str | None:
    """Write ONE .ass for the whole dubbed video: one Dialogue per remapped
    subtitle (OUTPUT-timeline seconds), bottom-center, up to 2 lines, no karaoke.

    `subs` MUST already be remapped to output seconds (_remap_subs_post_trim).
    Returns the .ass path, or None if there are no subtitles.
    """
    if not subs:
        return None
    fontsize = _caption_fontsize(width)
    margin_v = max(120, height // 8)
    margin = max(24, int(width * 0.037))
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        # WrapStyle 0 = smart auto-wrap allowed (we also pre-wrap to <=2 rows).
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # White text, thick black outline, bottom-center (Alignment 2). Bundled font.
        f"Style: Default,{CAPTION_FONT_FAMILY},{fontsize},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
        f"-1,0,0,0,100,100,0,0,1,5,2,2,{margin},{margin},{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    # ~0.5*fontsize px/glyph (Be Vietnam Pro Bold); budget 2 rows.
    usable_px = max(1, width - 2 * margin)
    max_chars = max(10, int(usable_px / (0.50 * fontsize)))

    events: list[str] = []
    # Defensive: sort + clamp overlaps so two events never stack (libass would
    # render both). End is clamped to the next event's start when they overlap.
    ss = sorted(subs, key=lambda s: (float(s["start"]), float(s["end"])))
    for i, sub in enumerate(ss):
        start = float(sub["start"])
        end = float(sub["end"])
        if i + 1 < len(ss):
            nxt = float(ss[i + 1]["start"])
            if end > nxt:
                end = nxt
        if end <= start:
            end = start + 0.10
        text = _wrap_two_lines(_ass_escape(sub.get("text_vi", "")), max_chars)
        if not text:
            continue
        events.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}"
        )
    path = os.path.join(work, "dubbed.ass")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header + "\n".join(events) + "\n")
    return path


DUBBED_FILTER_MAX_SEGMENTS = int(os.getenv("DUBBED_FILTER_MAX_SEGMENTS", "50"))


def _build_dubbed_filter(keep: list[tuple[float, float]], ass_path: str | None,
                         width: int, height: int, fps: int) -> str:
    """filter_complex for the single-pass cut+concat (video+audio kept), 9:16 fit,
    optional burned subtitles. `keep` is in SOURCE seconds; the .ass MUST already
    be remapped to OUTPUT seconds (see _remap_subs_post_trim)."""
    n = len(keep)
    parts: list[str] = []
    vlabels: list[str] = []
    alabels: list[str] = []
    for i, (s, e) in enumerate(keep):
        parts.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
        vlabels.append(f"[v{i}]")
        alabels.append(f"[a{i}]")
    interleaved = "".join(f"{vlabels[i]}{alabels[i]}" for i in range(n))
    parts.append(f"{interleaved}concat=n={n}:v=1:a=1[vc][ac]")

    # 9:16 fit reusing _bg_blur_chain (same splice as _footage_scene_clip).
    parts.append(
        f"[vc]split[bg][fg];{_bg_blur_chain(width, height)};"
        f"[fg]scale={width}:-2:force_original_aspect_ratio=decrease[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,fps={fps},setsar=1[vbase]"
    )
    if ass_path:
        parts.append(
            f"[vbase]subtitles='{_ff_filter_path(ass_path)}'"
            f":fontsdir='{_ff_filter_path(CAPTION_FONTSDIR)}'[v]"
        )
    else:
        parts.append("[vbase]null[v]")
    # async=1 absorbs cumulative A/V drift accumulated across the N joins.
    parts.append("[ac]aresample=async=1[a]")
    return ";".join(parts)


def _dubbed_cut_concat_largeN(src_mp4, keep, ass_path, width, height, fps, work, out_path):
    """Large-N (> DUBBED_FILTER_MAX_SEGMENTS) fallback: re-encode each keep-range to
    its own clip (identical codec params), join via the concat demuxer (stream copy),
    then a SECOND pass applies the 9:16 fit + subtitle burn (already on the output
    timeline) + audio resample."""
    # Pass 1: re-encode each keep-range to its own clip (frame-accurate; -ss AFTER
    # -i for accuracy; keep original audio). Same codec params for every clip so
    # the concat demuxer can stream-copy them.
    seg_paths: list[str] = []
    for i, (s, e) in enumerate(keep):
        seg = os.path.join(work, f"seg_{i:04d}.mp4")
        dur = e - s
        _run_ffmpeg(
            ["-i", src_mp4, "-ss", f"{s:.3f}", "-t", f"{dur:.3f}",
             *_video_encoder_args(fps=fps), *_audio_encoder_args(), seg],
            step=f"dubbed seg {i+1}/{len(keep)}",
        )
        seg_paths.append(seg)

    # Pass 1b: join (stream copy via concat demuxer).
    list_path = os.path.join(work, "dubbed_concat.txt")
    with open(list_path, "w", encoding="utf-8") as fh:
        for p in seg_paths:
            fh.write(f"file '{p.replace(chr(92), '/')}'\n")
    joined = os.path.join(work, "dubbed_joined.mp4")
    _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", joined],
                step="dubbed concat (demuxer)")

    # Pass 2: 9:16 fit + burn subs (already on output timeline) + audio resample.
    fc = (
        f"[0:v]split[bg][fg];{_bg_blur_chain(width, height)};"
        f"[fg]scale={width}:-2:force_original_aspect_ratio=decrease[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,fps={fps},setsar=1[vbase];"
    )
    fc += (f"[vbase]subtitles='{_ff_filter_path(ass_path)}'"
           f":fontsdir='{_ff_filter_path(CAPTION_FONTSDIR)}'[v];" if ass_path
           else "[vbase]null[v];")
    fc += "[0:a]aresample=async=1[a]"
    _run_ffmpeg(
        ["-i", joined, "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
         *_video_encoder_args(fps=fps), *_audio_encoder_args(), out_path],
        step="dubbed fit+subs",
    )


class DubbedAssembleRequest(BaseModel):
    page: str | None = None
    title: str = "video"
    srcVideoPath: str                       # cached 720p-capped source mp4 (src_video["videoPath"])
    srcDurationS: float | None = None       # source duration; probed if None
    subs: list[dict]                        # [{start,end,text_vi}] in SOURCE seconds (VN translation)
    filler: list[dict] = []                 # [{start,end,reason}] in SOURCE seconds (may be empty)
    width: int = 1080
    height: int = 1920
    fps: int = 30
    captions: bool = True
    # Source-credit slate fields (same names as FootageAssembleRequest):
    sourceName: str | None = None
    sourceLink: str | None = None
    sourceLogo: str | None = None
    sourceHandle: str | None = None
    addCredit: bool = True
    bgmPath: str | None = None
    bgmVolume: float = 0.12
    outDir: str | None = None
    videoId: int | None = None              # output filename suffix " (v<id>)"


@router.post("/generate/assemble/dubbed")
def assemble_dubbed(req: DubbedAssembleRequest) -> dict:
    if not os.path.isfile(req.srcVideoPath):
        raise HTTPException(422, f"Không tìm thấy video nguồn: {req.srcVideoPath}")

    src_dur = req.srcDurationS or _probe_duration(req.srcVideoPath)
    if src_dur <= 0:
        raise HTTPException(422, "Không xác định được thời lượng video nguồn.")

    # 1) keep-ranges (raises ValueError in Vietnamese if empty keep-set).
    try:
        keep = _compute_keep_ranges(req.filler, src_dur)
    except ValueError as ex:
        raise HTTPException(422, str(ex))

    # 2) remap subtitles SOURCE -> OUTPUT seconds, then build the .ass.
    out_dir = req.outDir or os.path.join(CONTENT_OUTPUT_ROOT, req.page or "default", "video")
    os.makedirs(out_dir, exist_ok=True)
    safe_title = "".join(c for c in req.title if c.isalnum() or c in (" ", "-", "_")).strip() or "video"
    suffix = f" (v{req.videoId})" if req.videoId else ""
    out_path = os.path.join(out_dir, f"{safe_title}{suffix}.mp4")

    with tempfile.TemporaryDirectory() as work:
        ass_path = None
        if req.captions and req.subs:
            remapped = _remap_subs_post_trim(req.subs, keep)
            ass_path = _build_dubbed_ass(remapped, req.width, req.height, work)

        # 3) cut+concat (keep original audio) + 9:16 + burn subs.
        body_path = os.path.join(work, "dubbed_body.mp4")
        if len(keep) > DUBBED_FILTER_MAX_SEGMENTS:
            _dubbed_cut_concat_largeN(req.srcVideoPath, keep, ass_path,
                                      req.width, req.height, req.fps, work, body_path)
        else:
            fc = _build_dubbed_filter(keep, ass_path, req.width, req.height, req.fps)
            _run_ffmpeg(
                ["-i", req.srcVideoPath, "-filter_complex", fc,
                 "-map", "[v]", "-map", "[a]",
                 *_video_encoder_args(fps=req.fps), *_audio_encoder_args(),
                 body_path],
                step="dubbed cut+concat",
            )

        # 4) credit slate + concat + optional bgm — reuse _finish_video.
        #    body_path already has the SAME codec params the slate uses
        #    (h264/yuv420p via _video_encoder_args; aac/48000 stereo), so the
        #    tail concat stays a stream copy.
        _finish_video(
            work, [body_path], out_path,
            width=req.width, height=req.height, fps=req.fps,
            source_name=req.sourceName, source_link=req.sourceLink,
            bgm_path=req.bgmPath, bgm_volume=req.bgmVolume,
            logo_path=req.sourceLogo, handle=req.sourceHandle, add_credit=req.addCredit,
            prog=None,   # dubbed has no per-scene progress allocator; run plainly
        )

    out_dur = _probe_duration(out_path)
    # F.5: DO NOT call _enforce_duration_guard here. Dubbed output = source - filler
    # (+ ~3s credit slate), structurally <= source; the guard's >= test would
    # false-fail an empty-cut dubbed. Log src vs out for transparency instead.
    filler_total = sum(e - s for s, e in zip(
        [f["start"] for f in req.filler], [f["end"] for f in req.filler])) if req.filler else 0.0
    print(f"[dubbed] src_dur={src_dur:.2f}s out_dur={out_dur:.2f}s "
          f"keep_ranges={len(keep)} (+credit slate ~{_CREDIT_SLATE_SEC:.0f}s)")

    return {
        "videoPath": out_path, "url": _media_url(out_path),
        "durationS": round(out_dur, 2), "scenes": len(keep),
        "width": req.width, "height": req.height,
    }


# --- translate_full mode: FULL source kept, muted, VN TTS + burned VN subs +
#     karaoke-cover over the source's own burned captions -----------------------
#
# translate_full (owner approach A — FOOTAGE-CUT). Natural spoken-Vietnamese narration
# (footage prompt), source RE-CUT and fit to the narration with junk removed, source
# audio MUTED, a subtle Ken Burns zoom, per-word VN karaoke positioned OVER the source's
# own burned captions, which are hidden by an EasyOCR caption-cover run on the CUT video.
# assemble_translate_full below reuses the footage per-scene cut/fit (_footage_scene_clip)
# then applies the cover + karaoke + credit treatment on the concatenated body.

def _caption_cover_intervals(video_path: str, band: tuple[float, float],
                             sample_fps: float = 3.0, pad_frac: float = 0.0) -> dict:
    """Run the caption-cover DETECTION pre-pass in the cf-venv (needs easyocr +
    opencv + CUDA) and return its result dict {srcW, srcH, intervals:[...]}.

    Runs BEFORE TTS so the GPU is not contended (detection is a short EasyOCR pass).
    "No captions -> 0 intervals" is VALID (empty cover, safe). Any worker failure is
    surfaced as an HTTPException by _run_cf_worker; the caller decides whether to
    proceed with an empty cover. The worker's CLI reads in.json/out.json exactly the
    way _run_cf_worker passes them (argv[1]=in, argv[2]=out)."""
    payload = {
        "videoPath": video_path,
        "sampleFps": sample_fps,
        "band": list(band),
        # Tight glyph boxes for the blur cover: padFrac 0 (the worker still floors the
        # dilation at ~2px) so the detected box hugs the text, giving a thin blur strip.
        "padFrac": pad_frac,
        "ffmpegBin": FFMPEG_BIN,
        "colorSample": False,
    }
    # 20 min ceiling: EasyOCR CRAFT on a few-fps sample of a short is fast, but the
    # cold model load on CPU-fallback can be slow.
    return _run_cf_worker("caption_cover.py", payload, timeout=1200)


def _build_cover_ass(intervals: list[dict], src_w: int, src_h: int, out_path: str,
                     play_res_x: int, play_res_y: int) -> str | None:
    """Thin wrapper that imports the stdlib-only ASS cover builder from the workers
    package and writes the cover .ass. Returns the path or None (no intervals)."""
    import importlib.util
    mod_path = os.path.join(WORKERS_DIR, "caption_cover.py")
    spec = importlib.util.spec_from_file_location("caption_cover", mod_path)
    cc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cc)
    return cc.build_cover_ass(intervals, src_w, src_h, out_path,
                              play_res_x=play_res_x, play_res_y=play_res_y, layer=0)


# translate_full slow-zoom (Ken Burns) travel: the source zooms 1.0 -> 1+ZOOM linearly
# over the whole video (subtle, like summary). 0 disables. Kept modest so the static
# cover box (which zooms WITH the frame) still fully hides the source's own karaoke.
TRANSLATE_FULL_ZOOM = float(os.getenv("TRANSLATE_FULL_ZOOM", "0.08"))


def _cover_caption_margin_v(intervals: list[dict], cover_src_h: int | None,
                            height: int, fontsize: int) -> int | None:
    """Vertical MarginV that puts the VN karaoke line's CENTER over the detected cover
    band (in OUTPUT px), so our running text sits where the source's own karaoke was.

    Returns None when there is nothing to cover (no intervals / unknown source height)
    -> caller uses the default footage bottom position. `\\an2` anchors the caption's
    BOTTOM edge at height - margin_v, so we solve margin_v from the band center + half
    the font height."""
    ys = []
    for iv in intervals or []:
        box = iv.get("box")
        if box and len(box) >= 4:
            ys.append((float(box[1]), float(box[1]) + float(box[3])))
    if not ys or not cover_src_h:
        return None
    ys.sort(key=lambda c: c[0])
    y0 = ys[len(ys) // 2][0]
    ys.sort(key=lambda c: c[1])
    y1 = ys[len(ys) // 2][1]
    sy = height / float(cover_src_h)
    band_center_out = ((y0 + y1) / 2.0) * sy
    pos_y = band_center_out + fontsize / 2.0            # an2 bottom edge
    return int(max(60, min(height - 40, height - pos_y)))


# translate_full caption-cover = SMOOTH GAUSSIAN BLUR of the region (owner-chosen; NOT
# mosaic, NOT flat color). Sigma scales with the region height so the strength tracks the
# text size; a small darken kills residual high-contrast text edges. Env-tunable.
TF_COVER_BLUR_SIGMA_FACTOR = float(os.getenv("TF_COVER_BLUR_SIGMA_FACTOR", "0.55"))
TF_COVER_BLUR_SIGMA_MIN = float(os.getenv("TF_COVER_BLUR_SIGMA_MIN", "14"))
TF_COVER_DARKEN = float(os.getenv("TF_COVER_DARKEN", "0.06"))   # slight darken under the patch


# Cap on per-interval blur overlays (perf). Above it, collapse to ONE persistent union
# blur (the detector rarely produces this many, but a pathological source is bounded).
#
# LOWERED 120 -> 60 (2026-08-19, owner call after job 349/350/351 all hung — same source
# video, ~33-34 intervals each attempt): isolated repro proved the per-interval chained
# split->crop->gblur->overlay filtergraph (_blur_cover_filtergraph) doesn't just get slow
# at high interval counts, it can flat-out STALL on frame 1 (90s+, zero output) well below
# the old 120 cap. NOTE: 34 intervals is still under 60, so this specific video's own
# retry will NOT be pushed onto the safe union-blur path by this change alone — the cap
# only helps sources with MORE intervals than this one. If translate_full hangs again on
# a source with a similar (30-40) interval count, the real fix is restructuring
# _blur_cover_filtergraph to not chain overlays sequentially, not a lower cap.
TF_COVER_MAX_INTERVALS = int(os.getenv("TF_COVER_MAX_INTERVALS", "60"))

# Extra pad (px, PER SIDE) added ON TOP of the measured VN karaoke extent when sizing the
# blur cover. The intrinsic outline/shadow of the karaoke style is always covered
# separately (see the box builder); these knobs add margin beyond that.
#   X: default 0 = tight to OUR VN text horizontally (owner: blur width == VN line).
#   Y: default 3 = extend the bar 3px UP (top) AND 3px DOWN (bottom), net height += 6px, so
#      the source's OLD burned text can't peek at the top/bottom edges. PAD_Y is applied
#      symmetrically: box_h gets 2*PAD_Y and the bottom anchor is pushed down by PAD_Y.
TF_COVER_PAD_X = float(os.getenv("TF_COVER_PAD_X", "0"))
TF_COVER_PAD_Y = float(os.getenv("TF_COVER_PAD_Y", "3"))
# Bottom-ONLY extra pad (px). Extends the bar DOWNWARD without moving the top, to cover a
# few px of the source's old burned text that peek just below the bar (the source caption
# often sits slightly lower than OUR VN line). Bumped 4 -> 8 because covers still leaked a
# few px at the bottom when the source caption was offset lower than the detected band.
# Applied only to the bottom edge: box_h += PAD_BOTTOM and the bottom anchor drops by
# PAD_BOTTOM; top (box_y) is unchanged.
TF_COVER_PAD_BOTTOM = float(os.getenv("TF_COVER_PAD_BOTTOM", "8"))
# Top-ONLY extra pad (px). Extends the bar UPWARD without moving the bottom, symmetric to
# PAD_BOTTOM. Default 4. Applied only to box_h (NOT to the bottom-anchor term), so the top
# rises by PAD_TOP and the bottom edge stays put.
TF_COVER_PAD_TOP = float(os.getenv("TF_COVER_PAD_TOP", "4"))

# Sentinel end (seconds) for the LAST cover box's between() time-gate. The concat body can
# run a hair longer than sum(scene durations) due to concat/fps rounding; a last box ending
# exactly at plan[-1].end lets the final frame(s) fall past the gate -> blur off -> the
# source text flashes back on the last frame. An effectively-unbounded end keeps between()
# true for every real body frame (harmless past body end: the body is finite and the credit
# slate is a separate later-concatenated clip). Only the LAST box gets this.
TF_COVER_TAIL_END = float(os.getenv("TF_COVER_TAIL_END", "1e9"))

# Karaoke style stroke geometry — MUST match the Outline/Shadow in the Style line built by
# _build_karaoke_ass (currently Outline=5, Shadow=2). The blur must cover the rendered
# stroke+shadow, not just the glyph ink, so the source caption can't peek through the edge.
_KARAOKE_OUTLINE_PX = 5
_KARAOKE_SHADOW_PX = 2

# Fine multiplier on the MEASURED VN ink height (below). Default 1.0 = hug the real text
# top. Nudge >1 if a render ever clips a tall diacritic, <1 to trim the top harder.
TF_COVER_INK_SCALE = float(os.getenv("TF_COVER_INK_SCALE", "1.0"))


def _karaoke_ink_height(text: str, fontsize: int) -> float:
    """Rendered VERTICAL ink extent (video px) of `text` in the karaoke style, measured
    from the \\an2 BOTTOM line UP to the top of the tallest glyph in the string (incl.
    stacked Vietnamese diacritics). This is well UNDER `fontsize`: (a) libass renders
    Be-Vietnam-Pro ~CAPTION_LIBASS_WFACTOR smaller than Pillow's nominal size (the same
    uniform scale the width uses), and (b) ordinary VN text does not fill the font's tall
    ascender headroom. The blur box height uses THIS (per scene, from the scene's own
    text) so its TOP hugs the visible text instead of the empty ascender space.

    = (font descent + text ink-ascent) * width-factor. VERIFIED against libass: predicts
    the on-screen ink-top-above-baseline within ~1px (probe over 5 VN strings). The box
    then adds the highlight-zoom headroom and the outline/shadow edge on top of this.
    Falls back to `fontsize` (old full-em behavior) if Pillow/the font is unavailable."""
    try:
        from PIL import ImageFont as _ImageFont
        if os.path.isfile(CAPTION_FONT):
            f = _ImageFont.truetype(CAPTION_FONT, fontsize)
            _asc, desc = f.getmetrics()
            bb = f.getbbox(text or "M", anchor="ls")   # origin at baseline
            ink_ascent = -bb[1]                          # px of tallest glyph above baseline
            h = (desc + ink_ascent) * CAPTION_LIBASS_WFACTOR
            if h > 0:
                return float(h)
    except Exception:
        pass
    return float(fontsize)


def _clamp_box(b, width: int, height: int, margin: int = 1):
    """Detected [x,y,w,h] -> even-aligned (x,y,w,h) clamped to frame, +margin px. None if
    degenerate."""
    x = max(0, int(float(b[0]) - margin)); y = max(0, int(float(b[1]) - margin))
    w = min(width - x, int(float(b[2]) + 2 * margin)); h = min(height - y, int(float(b[3]) + 2 * margin))
    x -= x % 2; y -= y % 2; w -= w % 2; h -= h % 2
    if w < 8 or h < 8:
        return None
    return (x, y, w, h)


def _blur_cover_filtergraph(cov_intervals: list[dict], width: int, height: int,
                            in_label: str, out_label: str) -> str | None:
    """Build the caption-cover filtergraph: a SMOOTH Gaussian blur (gblur) + slight darken
    applied ONLY inside the per-interval rectangles, each TIME-GATED to its interval.
    Blurred pixels are the real (blurred) background, so the patch blends per-frame on a
    changing scene; no border/stroke is drawn. Returns the fragment ending in `out_label`,
    or None if nothing to cover.

    Each interval is `{"box": [x, y, w, h], "start": s, "end": e}`. For translate_full the
    boxes are sized from OUR VN KARAOKE line (see _build_translate_full_karaoke): width =
    measured rendered VN text width, height = one karaoke line — NOT the EasyOCR source
    box. The source box is used only for the vertical band placement (margin_v), so if the
    source caption is wider/taller than the VN line a sliver may remain (accepted trade-off).

    SHAPE (rewritten 2026-08-19 — see the perf note below): the graph is O(1) in filters
    that need frame-sync, regardless of interval count.

        [in]split=3[base][src][msk]
        [src] crop(UNION) -> gblur -> eq(darken)          # ONE blur pass, whole union band
        [msk] crop(UNION) -> gray -> drawbox(fill black)  # binary MASK canvas
                            -> drawbox(fill white, enable=between(t,s,e)) x N
        [blur][mask] alphamerge                           # mask -> alpha of the blurred band
        [base][blurA] overlay=UNION.x:UNION.y             # ONE overlay

    N per-interval `drawbox` filters replace N per-interval `split+crop+gblur+overlay`
    stages. drawbox is single-input and in-place (no framesync, no branch to reconcile), so
    chaining many of them is linear and cheap; the only frame-syncing filters left are ONE
    alphamerge and ONE overlay. Time-gating is unchanged (`enable=between(t,start,end)`,
    now on the drawbox that paints the interval's rect into the mask).

    WHY (do not "simplify" this back): the previous shape chained one
    `split -> crop -> gblur -> overlay` stage PER interval, feeding stage N's output into
    stage N+1. Each overlay is a framesync filter reconciling two branches; ~30 of them
    deep hits an ffmpeg filter-graph scheduling cliff — measured on the job-352 body (33
    intervals, 1080x1920): the old graph could not emit even the FIRST frame in 120s
    (`-t 15 ... -f null -` stuck at frame=1), and it hung 4 consecutive production jobs
    (349/350/351/352, 25min–3.5h, zero output). The same body through this graph runs at a
    healthy multiple of realtime. The cost here does NOT scale with interval count: exactly
    one blur pass over the union band and one composite, whether there are 2 or 60 boxes.

    SIGMA: the old graph blurred each box separately with `sigma = box_h * FACTOR`, so
    sigma tracked that box's own height. One shared blur pass needs ONE sigma, so it is
    taken from the TALLEST box (`max(h) * FACTOR`, floored at TF_COVER_BLUR_SIGMA_MIN).
    Safe in practice: for translate_full every box is the same single karaoke line on the
    same band, so the heights differ only by the per-scene measured ink height (job 352:
    119.9–121.4px, i.e. sigma 66.0 vs 67.1 — under 2%). Erring toward the MAX also errs
    toward MORE obliteration of the source text, which is the point of the cover.

    Above TF_COVER_MAX_INTERVALS, still collapse to ONE persistent union blur (kept as a
    safety bound for an absurd interval count; the mask path no longer needs it for perf)."""
    ivs = [iv for iv in (cov_intervals or []) if iv.get("box") and len(iv["box"]) >= 4]
    if not ivs:
        return None

    def _stage(prev, box, enable, out):
        """Single blurred patch (one split+crop+gblur+overlay). Used only for the 1-box and
        the collapsed-union paths, where there is nothing to chain."""
        r = _clamp_box(box, width, height, margin=1)
        if r is None:
            return None, prev
        x, y, w, h = r
        sigma = max(TF_COVER_BLUR_SIGMA_MIN, h * TF_COVER_BLUR_SIGMA_FACTOR)
        b = out.strip("[]")
        en = f":enable='between(t,{enable[0]:.3f},{enable[1]:.3f})'" if enable else ""
        frag = (
            f"{prev}split=2[{b}_bs][{b}_rg];"
            f"[{b}_rg]crop={w}:{h}:{x}:{y},"
            f"gblur=sigma={sigma:.1f}:steps=3,eq=brightness=-{TF_COVER_DARKEN:.3f}[{b}_bl];"
            f"[{b}_bs][{b}_bl]overlay={x}:{y}{en}{out}"
        )
        return frag, out

    if len(ivs) > TF_COVER_MAX_INTERVALS:
        # Pathological interval count -> one persistent union blur (still tight in HEIGHT:
        # union of glyph-tight boxes; not time-gated).
        left = min(float(iv["box"][0]) for iv in ivs)
        top = min(float(iv["box"][1]) for iv in ivs)
        right = max(float(iv["box"][0]) + float(iv["box"][2]) for iv in ivs)
        bottom = max(float(iv["box"][1]) + float(iv["box"][3]) for iv in ivs)
        frag, _ = _stage(in_label, [left, top, right - left, bottom - top], None, out_label)
        return frag

    # Clamp every interval up front; degenerate boxes drop out here (same rule as before).
    rects: list[tuple[tuple[int, int, int, int], float, float, list]] = []
    for iv in ivs:
        r = _clamp_box(iv["box"], width, height, margin=1)
        if r is None:
            continue
        rects.append((r, float(iv.get("start", 0.0)), float(iv.get("end", 0.0)), iv["box"]))
    if not rects:
        return None
    if len(rects) == 1:
        # Nothing to chain — keep the plain single-patch graph (no mask machinery).
        _r, s, e, raw = rects[0]
        frag, _ = _stage(in_label, raw, (s, e), out_label)
        return frag

    # UNION band: the one region the blur pass has to cover (even-aligned for yuv420p).
    ux = min(r[0][0] for r in rects)
    uy = min(r[0][1] for r in rects)
    ux2 = max(r[0][0] + r[0][2] for r in rects)
    uy2 = max(r[0][1] + r[0][3] for r in rects)
    ux -= ux % 2
    uy -= uy % 2
    uw = min(ux2 - ux + ((ux2 - ux) % 2), width - ux)
    uh = min(uy2 - uy + ((uy2 - uy) % 2), height - uy)
    uw -= uw % 2
    uh -= uh % 2
    if uw < 8 or uh < 8:
        return None

    # One sigma for the shared pass, from the tallest box (see SIGMA note above).
    sigma = max(TF_COVER_BLUR_SIGMA_MIN,
                max(r[0][3] for r in rects) * TF_COVER_BLUR_SIGMA_FACTOR)

    # Binary mask: black canvas the size of the union band, one filled white drawbox per
    # interval, time-gated exactly as the old per-interval overlay was. drawbox on GRAY8
    # writes exact 0/255 (verified), which alphamerge turns into a hard 0/1 alpha — so the
    # composite is "original outside the rect, blurred inside", identical to the old
    # overlay semantics (hard edges, no feathering).
    mask_boxes = "".join(
        f",drawbox=x={bx - ux}:y={by - uy}:w={bw}:h={bh}"
        f":color=white:t=fill:enable='between(t,{s:.3f},{e:.3f})'"
        for (bx, by, bw, bh), s, e, _raw in rects
    )
    # TWO format pins below are LOAD-BEARING — do not drop them (both verified with
    # `ffmpeg -v verbose` graph dumps on the job-352 body):
    #
    #  (1) `format=yuv420p` BEFORE the mask branch's crop. libavfilter negotiates one
    #      shared pixel format across ALL outputs of a `split`, and it propagates the
    #      requirement BACKWARDS. Without this pin the mask branch's `format=gray`
    #      reaches back through crop -> split -> the source, so ffmpeg converts the WHOLE
    #      1080x1920 frame to gray and back (`auto_scale ... yuv420p -> gray` on the graph
    #      input): the entire video comes out BLACK AND WHITE, plus a tv->pc->tv range
    #      round-trip on every pixel. It only looked fine by accident when a `subtitles`
    #      filter happened to sit downstream and pin the format itself. With the pin, the
    #      gray conversion is confined to the cropped band.
    #  (2) `:format=yuv420` on the overlay. The overlay input carries alpha (yuva420p);
    #      left on `auto`, overlay converts the full-size MAIN frame to yuva420p too.
    #      Pinned, main stays yuv420p and only the small band carries alpha. Verified: with
    #      an all-black (alpha=0) mask the graph is a bit-exact no-op over the whole frame
    #      (maxdiff 0 on Y, U and V), i.e. nothing outside a cover rect is touched.
    return (
        f"{in_label}split=3[cvbase][cvsrc][cvmsk];"
        f"[cvsrc]crop={uw}:{uh}:{ux}:{uy},gblur=sigma={sigma:.1f}:steps=3,"
        f"eq=brightness=-{TF_COVER_DARKEN:.3f}[cvblur];"
        f"[cvmsk]format=yuv420p,crop={uw}:{uh}:{ux}:{uy},format=gray,"
        f"drawbox=x=0:y=0:w={uw}:h={uh}:color=black:t=fill{mask_boxes}[cvmask];"
        f"[cvblur][cvmask]alphamerge[cvblura];"
        f"[cvbase][cvblura]overlay={ux}:{uy}:format=yuv420{out_label}"
    )


def _build_translate_full_karaoke(plan: list[dict], width: int, height: int, fps: int,
                                  work: str, margin_v: int | None
                                  ) -> tuple[str | None, list[dict]]:
    """Build ONE per-word KARAOKE .ass spanning the whole translate_full video, reusing
    the SAME pipeline footage/summary use: whisper word timings per VN clip ->
    _aligned_caption_words (known text on measured timing) -> _build_karaoke_ass
    (per-word highlight, Be-Vietnam-Pro width-factor fix). Each clip's events are shifted
    to its soft-anchor placement (time_offset) and positioned over the cover band
    (margin_v), then merged into one file.

    Returns (ass_path, cover_boxes). cover_boxes is one blur-cover interval per scene,
    sized from OUR VN karaoke line (NOT the EasyOCR source box):
      - WIDTH  = the scene's widest rendered VN chunk (max_line_w from _build_karaoke_ass,
                 already in libass px via CAPTION_LIBASS_WFACTOR) * active-word zoom + pad.
      - HEIGHT = the scene's MEASURED VN ink height (_karaoke_ink_height: descender line
                 up to the tallest glyph, libass-scaled) * active-word zoom + edge. NOT the
                 full em/fontsize (that left dead space above the caps). This builder never
                 wraps (WrapStyle 2; exactly one single-row chunk on screen at any instant),
                 so the rendered text is always ONE line tall; only the TOP is trimmed —
                 the bottom edge stays on the karaoke band.
      - X      = centered (karaoke is \\an2 / center-anchored).
      - Y      = the karaoke band: \\an2 anchors the line BOTTOM at height - margin_v, so
                 the box's bottom edge sits there and it grows upward over the line.
    ass_path is None (and boxes []) when there are no captions at all."""
    align = os.getenv("CF_KARAOKE_ALIGN", os.getenv("CF_ALIGN_BACKEND", "ctc")).strip().lower()
    items = [{"scene": i, "audioPath": p["clip"], "narration": p["text_vi"]}
             for i, p in enumerate(plan)]
    word_map: dict[int, list] = {}
    try:
        res = _run_cf_worker(
            "whisper_worker.py",
            {"items": items, "model": WHISPER_MODEL, "device": WHISPER_DEVICE,
             "compute": WHISPER_COMPUTE, "language": "vi", "wordTimestamps": True,
             "align": align},
            timeout=1200,
        )
        for r in res.get("results", []):
            word_map[r["scene"]] = [w for seg in r.get("segments", [])
                                    for w in (seg.get("words") or [])]
    except Exception as e:  # noqa: BLE001 — captions must never fail the whole render
        log.warning("[translate_full] karaoke whisper failed (%s); falling back to "
                    "even-split word timing", e)

    # Cover-box geometry, shared across scenes. Mirror _build_karaoke_ass's own defaults so
    # the blur band lines up with where the karaoke is actually drawn.
    fontsize = _karaoke_fontsize(width, height)
    eff_margin_v = int(margin_v) if margin_v is not None else max(120, height // 8)
    pos_y = height - eff_margin_v                    # \an2 bottom edge of the karaoke line
    zoom = max(1.0, HL_SCALE / 100.0)                # active word grows to HL_SCALE% (also vertically)
    edge = float(_KARAOKE_OUTLINE_PX + _KARAOKE_SHADOW_PX)   # cover the rendered stroke+shadow

    header = None
    all_events: list[str] = []
    cover_boxes: list[dict] = []
    for i, p in enumerate(plan):
        dur = max(0.1, float(p["end"]) - float(p["start"]))
        cap_words = _aligned_caption_words(p["text_vi"], word_map.get(i, []), dur, scene=i)
        built = _build_karaoke_ass(cap_words, width, height, work, i,
                                   margin_v=margin_v, time_offset=float(p["start"]),
                                   return_events=True,
                                   clamp_window=(float(p["start"]), float(p["end"])))
        if not built:
            continue
        h, evs, line_w = built
        header = header or h
        all_events.extend(evs)
        # One cover box per scene sized to OUR VN text; time-gated to the scene window.
        if line_w and line_w > 0:
            # HEIGHT hugs the scene's actual VN ink (measured), NOT the full em — the top
            # was previously the full fontsize (tall ascender headroom = dead space above
            # the text). zoom covers the highlighted word's vertical growth (fscy scales
            # from the an2 bottom anchor, so the top grows by (zoom-1)); edge covers stroke.
            ink_h = _karaoke_ink_height(p["text_vi"], fontsize) * TF_COVER_INK_SCALE
            box_w = float(line_w) * zoom + 2.0 * edge + 2.0 * TF_COVER_PAD_X
            # PAD_BOTTOM extends ONLY the bottom edge downward, PAD_TOP ONLY the top edge
            # upward (covers source text peeking below/above the bar). Both add to box_h;
            # only PAD_BOTTOM enters the bottom-anchor term, so PAD_TOP grows the top alone.
            box_h = (ink_h * zoom + 2.0 * edge + 2.0 * TF_COVER_PAD_Y
                     + TF_COVER_PAD_BOTTOM + TF_COVER_PAD_TOP)
            box_x = width / 2.0 - box_w / 2.0                 # centered (an2/center) — unchanged
            box_y = (pos_y + edge + TF_COVER_PAD_Y + TF_COVER_PAD_BOTTOM) - box_h  # bottom fixed; top rises by PAD_TOP
            cover_boxes.append({
                "box": [box_x, box_y, box_w, box_h],
                "start": float(p["start"]), "end": float(p["end"]),
            })
    # TAIL HOLD: keep ONLY the last cover box on through the true end of the body. The
    # concat body can run a hair longer than sum(durs) (concat/fps rounding); a last box
    # ending exactly at plan[-1].end lets the final frame(s) fall past its between() gate,
    # so the blur switches off and the source text flashes back on the last frame. An
    # effectively-unbounded end keeps between() true for every real frame. Non-last boxes
    # keep their contiguous [start,end] (between() is inclusive; adjacent boxes touch, no
    # gap). This is the BLUR gate only — the caption event clamp (per-scene) is untouched.
    if cover_boxes:
        cover_boxes[-1]["end"] = TF_COVER_TAIL_END
    if header is None or not all_events:
        return None, cover_boxes
    path = os.path.join(work, "karaoke.ass")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header + "\n".join(all_events) + "\n")
    return path, cover_boxes


class TranslateFullAssembleRequest(BaseModel):
    page: str | None = None
    title: str = "video"
    scenes: list[FootageScene]              # cut source clip + VN VO + caption + durationS
    width: int = 1080
    height: int = 1920
    fps: int = 30
    captions: bool = True
    captionBand: list[float] = [0.72, 0.90]  # y-band (source-relative) to scan for burned captions
    zoom: float | None = None                # Ken Burns travel; None -> TRANSLATE_FULL_ZOOM
    engine: str | None = None                # reserved (parity with FootageAssembleRequest)
    # Source-credit slate fields (same names as FootageAssembleRequest):
    sourceName: str | None = None
    sourceLink: str | None = None
    sourceLogo: str | None = None
    sourceHandle: str | None = None
    addCredit: bool = True
    bgmPath: str | None = None
    bgmVolume: float = 0.12
    outDir: str | None = None
    videoId: int | None = None


# --- translate_full render-progress weights ------------------------------------
#
# The render step used to freeze at "Dựng video (dịch) 0%" for its whole 6-10 min:
# assemble_translate_full never fed the ffmpeg progress callback the runner installs.
# Each weight below is an ESTIMATED wall-clock cost expressed as a MULTIPLE OF THE BODY
# LENGTH (sum of the VO durations), so one split works for a 60s and a 300s video.
# Calibrated on this session's real jobs (~170s body, 30-40 scenes, ~7-10 min render):
#
#   scene cut + Ken Burns   ~30-60s    -> 0.30x   (per scene, weighted by its own dur)
#   concat body             ~1-3s      -> 0.02x   (stream copy)
#   EasyOCR cover detect     ~6-7 min   -> 2.30x   DOMINANT (3fps sample, ~0.7s/frame)
#   karaoke whisper pass    ~30-60s    -> 0.30x
#   blur cover + sub burn   ~30-400s   -> 0.80x   (full re-encode of the body)
#   credit slate/concat/bgm            -> the same small constants assemble_footage uses
#
# The weights only place the stage BOUNDARIES; INSIDE each stage the reported fraction is
# real (ffmpeg -progress, or the OCR worker's per-frame progress file), so a misestimate
# makes the bar uneven — never frozen, and never claiming done before it is.
TF_PROG_W_SCENES = float(os.getenv("TF_PROG_W_SCENES", "0.30"))
TF_PROG_W_CONCAT = float(os.getenv("TF_PROG_W_CONCAT", "0.02"))
TF_PROG_W_OCR = float(os.getenv("TF_PROG_W_OCR", "2.30"))
TF_PROG_W_KARAOKE = float(os.getenv("TF_PROG_W_KARAOKE", "0.30"))
TF_PROG_W_BURN = float(os.getenv("TF_PROG_W_BURN", "0.80"))


@router.post("/generate/assemble/translate_full")
def assemble_translate_full(req: TranslateFullAssembleRequest) -> dict:
    """translate_full assembler (owner approach A — footage-cut).

    Per scene: cut source window fitted to the VN VO length + a subtle Ken Burns zoom,
    MUTED source (VO only). Concat the scenes into a body, then — on that CUT body —
    run the EasyOCR caption-cover detector (so cover intervals align to the CUT
    timeline), build the opaque cover (layer 0) and the per-word VN karaoke (layer 1,
    positioned OVER the cover band), burn both, and append the credit slate. Junk was
    already excluded at scene-gen (no scenes over ad/intro/outro/source-credit ranges)."""
    if not req.scenes:
        raise HTTPException(422, "translate_full: không có cảnh nào để dựng.")
    for s in req.scenes:
        if not os.path.isfile(s.clipPath):
            raise HTTPException(422, f"scene {s.scene}: clip not found: {s.clipPath}")
        if not os.path.isfile(s.audioPath):
            raise HTTPException(422, f"scene {s.scene}: audio not found: {s.audioPath}")

    out_dir = req.outDir or os.path.join(CONTENT_OUTPUT_ROOT, req.page or "default", "video")
    os.makedirs(out_dir, exist_ok=True)
    safe_title = "".join(c for c in req.title if c.isalnum() or c in (" ", "-", "_")).strip() or "video"
    suffix = f" (v{req.videoId})" if req.videoId else ""
    out_path = os.path.join(out_dir, f"{safe_title}{suffix}.mp4")

    zoom = TRANSLATE_FULL_ZOOM if req.zoom is None else float(req.zoom)

    with tempfile.TemporaryDirectory() as work:
        # Per-scene VO durations (drive each fitted clip's length).
        durs: list[float] = []
        for s in req.scenes:
            d = s.durationS or _probe_duration(s.audioPath)
            if d <= 0:
                raise HTTPException(422, f"scene {s.scene}: could not determine VO duration")
            durs.append(d)

        # LIVE PROGRESS controller (see TF_PROG_W_* above). Same _AssembleProgress the
        # image/footage assemblers use; a no-op when no ffmpeg progress cb is installed
        # for this thread (direct HTTP call), so behavior outside the runner is unchanged.
        body_secs = max(1.0, float(sum(durs)))
        _cb = getattr(_ff_progress_local, "cb", None)
        scenes_w = TF_PROG_W_SCENES * body_secs
        body_concat_w = max(0.5, TF_PROG_W_CONCAT * body_secs)
        ocr_w = TF_PROG_W_OCR * body_secs
        karaoke_w = (TF_PROG_W_KARAOKE * body_secs) if req.captions else 0.0
        burn_w = TF_PROG_W_BURN * body_secs
        slate_w = 3.0 if (req.addCredit and (req.sourceLogo or req.sourceHandle
                                             or req.sourceName)) else 0.0
        # The _finish_video tail is a cheap stream copy, but its weight must still be a
        # VISIBLE slice: with a purely proportional weight it was ~0.5% of the total, so
        # the chip rounded to "100%" the instant the burn ended — while the concat (and
        # the output file) was still a second away. Floor it at ~1.5% of everything else
        # so the bar reads 98-99% through the tail and only hits 100% when the mp4 exists.
        _pre_tail_w = scenes_w + body_concat_w + ocr_w + karaoke_w + burn_w + slate_w
        tail_concat_w = max(0.5, 0.02 * body_secs, 0.015 * _pre_tail_w)
        tail_bgm_w = (max(0.5, 0.02 * body_secs) if req.bgmPath else 0.0)
        prog = _AssembleProgress(
            _cb,
            scenes_w + body_concat_w + ocr_w + karaoke_w + burn_w
            + slate_w + tail_concat_w + tail_bgm_w,
            lo=0, hi=100, label="Dựng video (dịch)",
        )
        # _finish_video spends these; video_secs is the REAL output length (the total
        # above is inflated by the non-encode OCR/whisper stages, so it can't subtract).
        prog.slate_w, prog.concat_w, prog.bgm_w = slate_w, tail_concat_w, tail_bgm_w
        prog.video_secs = body_secs
        if _cb:
            prog.begin()

        # 1) Per-scene fitted + zoomed clip (NO caption yet; source audio MUTED = VO only).
        scene_clips: list[str] = []
        prog.phase = " — cắt cảnh"
        for s, dur in zip(req.scenes, durs):
            def _cut_scene(s=s, dur=dur):
                scene_clips.append(_footage_scene_clip(
                    s.clipPath, s.audioPath, dur, None, req.width, req.height, req.fps,
                    work, s.scene, src_audio_volume=0.0, zoom=zoom))
            # Each scene's slice of the scene weight = its share of the body length.
            prog.step(scenes_w * (dur / body_secs), dur, _cut_scene)

        # 2) Concat the scene clips -> body (no captions yet). Same codec params across
        #    clips (via _footage_scene_clip) so the concat demuxer stream-copies.
        prog.phase = " — ghép cảnh"
        body_path = os.path.join(work, "tf_body.mp4")
        list_path = os.path.join(work, "tf_concat.txt")
        with open(list_path, "w", encoding="utf-8") as fh:
            for c in scene_clips:
                fh.write(f"file '{c.replace(chr(92), '/')}'\n")
        prog.step(body_concat_w, body_secs, lambda: _run_ffmpeg(
            ["-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", body_path],
            step="translate_full concat"))

        # 3) Caption-cover DETECTION on the CUT body (Section C): cover intervals must
        #    align to the CUT/zoomed timeline, not the original source. Detection on the
        #    SAME frames we burn onto means the (static, per-interval) cover box spans the
        #    karaoke's on-screen positions in that interval. Non-fatal on failure.
        #    PROGRESS: this is the DOMINANT sub-stage (~6-7 min). The worker reports a REAL
        #    per-frame fraction through the standard progressFile channel (_run_cf_worker
        #    polls it when a set_progress_cb is installed for this thread), so we install a
        #    cb that folds the worker's 0..100 into this step's slice. Saved/restored so the
        #    runner's own worker-progress wiring is untouched.
        cover: dict = {}

        def _detect_cover(report):
            _prev_cb = getattr(_progress_local, "cb", None)
            set_progress_cb(lambda pct, msg: report(min(100, max(0, int(pct))) / 100.0))
            try:
                return _caption_cover_intervals(body_path, tuple(req.captionBand))
            finally:
                set_progress_cb(_prev_cb)

        try:
            cover = prog.step_manual(ocr_w, _detect_cover, phase=" — dò phụ đề gốc")
        except Exception as e:  # noqa: BLE001 — cover must never fail the whole render
            log.warning("[translate_full] caption-cover detect failed (%s); proceeding "
                        "with empty cover (source karaoke may show through)", e)
        cov_iv = cover.get("intervals", []) if isinstance(cover, dict) else []

        # 4) VN karaoke goes OVER the cover band (2x-font-aware). None -> footage bottom.
        cap_margin_v = _cover_caption_margin_v(
            cov_iv, cover.get("srcH"), req.height, _karaoke_fontsize(req.width, req.height))

        # 6) Per-word VN karaoke spanning the body: each scene's whisper word timings
        #    offset by the cumulative concat position, positioned over the band. This ALSO
        #    yields the blur-cover boxes sized to OUR VN line (width = measured VN text
        #    width, height = one karaoke line), NOT the source-detected box.
        karaoke_ass = None
        cover_boxes: list[dict] = []
        if req.captions:
            plan: list[dict] = []
            cursor = 0.0
            for s, dur in zip(req.scenes, durs):
                plan.append({"scene": s.scene, "clip": s.audioPath,
                             "text_vi": s.caption or "",
                             "start": round(cursor, 3), "end": round(cursor + dur, 3)})
                cursor += dur
            # PROGRESS: this runs a whisper pass over every scene and emits NO signal, so
            # spend its weight on a capped time RAMP (same honesty contract as
            # runner._run_with_time_ramp — an estimate that never claims the stage is done).
            karaoke_ass, cover_boxes = _ramped_step(
                prog, karaoke_w, max(10.0, 0.30 * body_secs), " — căn phụ đề",
                lambda: _build_translate_full_karaoke(
                    plan, req.width, req.height, req.fps, work, cap_margin_v))

        # 7) Burn the blur cover (sized to OUR VN karaoke line, centered on the band) THEN
        #    the VN karaoke onto the body. Video re-encodes; audio (VO, already aac/48k/
        #    stereo) is stream-copied so the tail concat stays a stream copy in _finish_video.
        cover_fc = _blur_cover_filtergraph(cover_boxes, req.width, req.height, "[0:v]", "[vc]")
        captioned = body_path
        if cover_fc or karaoke_ass:
            parts: list[str] = []
            label = "[0:v]"
            if cover_fc:
                parts.append(cover_fc)
                label = "[vc]"
            if karaoke_ass:
                parts.append(f"{label}subtitles='{_ff_filter_path(karaoke_ass)}'"
                             f":fontsdir='{_ff_filter_path(CAPTION_FONTSDIR)}'[vk]")
                label = "[vk]"
            fc = ";".join(parts)
            captioned = os.path.join(work, "tf_captioned.mp4")
            prog.phase = " — chèn phụ đề"
            prog.step(burn_w, body_secs, lambda: _run_ffmpeg(
                ["-i", body_path, "-filter_complex", fc, "-map", label, "-map", "0:a",
                 *_video_encoder_args(fps=req.fps), "-c:a", "copy", captioned],
                step="translate_full captions",
            ))
        else:
            # Nothing to burn — spend the weight so the bar reaches the tail's boundary
            # instead of jumping when _finish_video starts.
            prog.step_manual(burn_w, lambda report: None)

        # 8) credit slate + concat + optional bgm — reuse _finish_video (captioned shares
        #    the codec params the slate uses, so the tail concat stays a stream copy).
        #    `prog` spends the slate/concat/bgm weights it was given above (was prog=None,
        #    which made the tail invisible to the FE chip).
        prog.phase = " — hoàn thiện"
        _finish_video(
            work, [captioned], out_path,
            width=req.width, height=req.height, fps=req.fps,
            source_name=req.sourceName, source_link=req.sourceLink,
            bgm_path=req.bgmPath, bgm_volume=req.bgmVolume,
            logo_path=req.sourceLogo, handle=req.sourceHandle, add_credit=req.addCredit,
            prog=prog,
        )

    out_dur = _probe_duration(out_path)
    body_secs = sum(durs)
    print(f"[translate_full] scenes={len(req.scenes)} body={body_secs:.2f}s "
          f"out_dur={out_dur:.2f}s zoom={zoom:.3f} src_detect={len(cov_iv)} "
          f"vn_cover_boxes={len(cover_boxes)} karaoke={'yes' if karaoke_ass else 'no'}")
    return {
        "videoPath": out_path, "url": _media_url(out_path),
        "durationS": round(out_dur, 2), "scenes": len(req.scenes),
        "width": req.width, "height": req.height,
    }


# --- Footage download: one start-anchored window, cut scenes locally -----

# Source-download height cap. Was 720, which forced an UPSCALE to the 1080 output
# (soft). 1080 matches the output height so 16:9 source fits 1:1 and 9:16 crop stays
# sharp; env-overridable to trade download size/speed for sharpness.
FOOTAGE_MAX_HEIGHT = int(os.getenv("FOOTAGE_MAX_HEIGHT", "1080"))


def _page_clips_dir(page: str | None) -> str:
    return os.path.join(CONTENT_OUTPUT_ROOT, page or "default", "clips")


class SourceVideoRequest(BaseModel):
    link: str
    page: str | None = None
    window: int | None = None     # seconds from 0 to fetch; None = env INGEST_MAX_SEC
    maxHeight: int | None = None
    outDir: str | None = None


@router.post("/generate/source_video")
def download_source_video(req: SourceVideoRequest):
    if not req.link.strip():
        raise HTTPException(422, "Provide a source 'link'.")
    link = req.link.strip()
    ffmpeg_dir = os.path.dirname(FFMPEG_BIN) if os.path.isfile(FFMPEG_BIN) else None
    window = req.window if req.window is not None else (INGEST_MAX_SEC or 0)

    # --- Cross-job REUSE (TASK 2): a downloaded source video is cached under
    #     CONTENT_OUTPUT_ROOT/_cache/sources/<id>.<ext> keyed by a STABLE id from
    #     the link. Cache HIT -> skip the yt-dlp download entirely; MISS -> download
    #     (preserving the download_worker 403 start-anchored-range workaround) then
    #     store. The cached file is the SAME start-anchored window used for cutting,
    #     so reuse is correct as long as the ingest window is unchanged (it is, for
    #     a given INGEST_MAX_SEC). INGEST_CACHE=0 disables reuse; a corrupt/zero-byte
    #     cache file is treated as a MISS. probe() gives width/height for the HIT
    #     result so the contract matches a fresh download.
    sid = cache_util.source_id(link)
    if cache_util.cache_reads_enabled():
        hit = cache_util.find_cached_source(sid)
        if hit:
            print(f"[cache] source HIT {sid} -> {hit}")
            meta = _probe_media(hit)
            return {
                "videoPath": hit, "videoId": sid,
                "durationS": meta.get("durationS", 0.0),
                "width": meta.get("width"), "height": meta.get("height"),
                "cached": True,
            }
        print(f"[cache] source MISS {sid} — will download")
    else:
        print(f"[cache] source bypass (INGEST_CACHE off) {sid} — will download")

    payload = {
        "link": link,
        "outDir": req.outDir or _page_clips_dir(req.page),
        "window": window,
        "maxHeight": req.maxHeight or FOOTAGE_MAX_HEIGHT,
        "ffmpegLocation": ffmpeg_dir,
    }
    res = _run_cf_worker("download_worker.py", payload, timeout=1800)
    # Populate the cache for the next job using this URL (best-effort; never fails
    # the job). store_source no-ops when writes are disabled or the file is invalid.
    cached = cache_util.store_source(sid, res.get("videoPath", ""))
    if cached:
        print(f"[cache] source STORED {sid} -> {cached}")
    return res


def _cut_clip(src_video: str, start: float, end: float, dest: str, fps: int = 30) -> float:
    """Cut [start,end] from a local video (re-encode for accurate cuts). Returns
    the clip duration. Falls back to a 2s minimum if the range is degenerate.

    `fps` only sets the encoder GOP (defaults to the pipeline's 30fps); the cut keeps
    the source's own frame cadence. Uniform encoder params keep the later concat a
    stream-copy."""
    dur = max(0.5, float(end) - float(start))
    # The cut re-encodes at the SOURCE resolution AND the SOURCE frame cadence
    # (passthrough-trim keeps source pixels; the assembly step later downscales to the
    # output aspect). A 1440p/4K source exceeds the default H.264 level 4.2 max frame
    # size, and a high-fps source additionally exceeds a level's macroblocks-per-second
    # budget (1440p60 does NOT fit level 5.0), so probe the source dims AND fps and let
    # _video_encoder_args pick a level that fits both — otherwise NVENC refuses to open
    # the encoder and the cut yields a 0-frame/streamless clip ("cut clip failed").
    # NOTE: `fps` (the GOP arg) is the pipeline's 30, NOT the clip's real cadence —
    # the level must be computed from the probed source fps.
    _sm = _probe_media(src_video)
    # Count this NVENC session while it runs so a concurrently-loading GPU worker can
    # wait it out on retry instead of re-crashing into the same contention
    # (see _wait_for_cuts_idle). finally: a failed cut must still release its slot.
    _cut_begin()
    try:
        _run_ffmpeg(
            ["-ss", f"{max(0.0, float(start)):.3f}", "-i", src_video, "-t", f"{dur:.3f}",
             *_video_encoder_args(fps=fps, width=_sm.get("width"), height=_sm.get("height"),
                                  src_fps=_sm.get("fps")),
             *_audio_encoder_args(), dest],
            step="cut clip",
        )
    finally:
        _cut_end()
    return _probe_duration(dest)


# --- Manual scene editor: image upload + one-shot render -----------------


@router.post("/generate/image")
async def upload_image(page: str = Form(...), file: UploadFile = File(...)):
    """Save an uploaded scene image and return its path + playable URL."""
    d = os.path.join(CONTENT_OUTPUT_ROOT, page or "default", "images")
    os.makedirs(d, exist_ok=True)
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        ext = ".png"
    dest = os.path.join(d, uuid.uuid4().hex + ext)
    with open(dest, "wb") as f:
        f.write(await file.read())
    return {"path": dest, "url": _media_url(dest)}


class VideoScene(BaseModel):
    scene: int
    caption: str
    imagePath: str


class VideoRequest(BaseModel):
    page: str | None = None
    title: str = "video"
    voice: str | None = None
    refAudio: str | None = None
    scenes: list[VideoScene]
    bgmPath: str | None = None
    sourceName: str | None = None
    sourceLink: str | None = None
    width: int = 1080
    height: int = 1920
    fps: int = 30
    captions: bool = True
    temperature: float | None = None
    repetitionPenalty: float | None = None
    maxNewFrames: int | None = None


@router.post("/generate/video")
def make_video(req: VideoRequest):
    """Manual build: voice each scene's caption, then assemble into a video.

    Used by the Studio's Edit-scenes panel. One TTS model load for all scenes.
    """
    if not req.scenes:
        raise HTTPException(422, "Provide at least one scene.")
    for s in req.scenes:
        if not s.caption.strip():
            raise HTTPException(422, f"scene {s.scene}: caption is empty")
        if not os.path.isfile(s.imagePath):
            raise HTTPException(422, f"scene {s.scene}: image not found: {s.imagePath}")

    tts = _run_cf_worker(
        "tts_worker.py",
        {
            "items": [{"scene": s.scene, "text": s.caption} for s in req.scenes],
            "voice": req.voice,
            "refAudio": req.refAudio,
            "emotion": "natural",
            "applyWatermark": False,
            "temperature": req.temperature,
            "repetitionPenalty": req.repetitionPenalty,
            "maxNewFrames": req.maxNewFrames,
            "outDir": _page_audio_dir(req.page),
        },
        timeout=900,
    )
    audio = {r["scene"]: r for r in tts["results"]}

    a_scenes = []
    for s in req.scenes:
        r = audio.get(s.scene)
        if not r:
            raise HTTPException(500, f"scene {s.scene}: TTS produced no audio")
        a_scenes.append(
            AssembleScene(scene=s.scene, imagePath=s.imagePath, audioPath=r["audioPath"], caption=s.caption, durationS=r.get("durationS"))
        )

    return assemble(
        AssembleRequest(
            page=req.page, title=req.title, scenes=a_scenes, bgmPath=req.bgmPath,
            sourceName=req.sourceName, sourceLink=req.sourceLink,
            width=req.width, height=req.height, fps=req.fps, captions=req.captions,
        )
    )
