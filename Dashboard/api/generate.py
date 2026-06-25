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

import json
import logging
import math
import os
import re
import shutil
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
import tts_cache

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
SCRIPT_GEN_TIMEOUT = int(os.getenv("SCRIPT_GEN_TIMEOUT", "300"))

# Max scenes generated per `claude -p` batch. Output-token decode is the dominant
# latency term (diag: ~50 tok/s; a 71-scene one-shot pushed past the old 600s wall),
# so we split a large scene_count into contiguous batches of this many scenes — each
# a small, fast, individually-retryable call. ~4 calls for 71 scenes. Don't shard to
# 1 scene/call: per-call overhead (~3s) + cache churn would dominate. When the total
# scene_count <= this, we do ONE call (no chunking overhead — the degenerate case).
SCRIPT_GEN_CHUNK_SCENES = int(os.getenv("SCRIPT_GEN_CHUNK_SCENES", "18"))

# Per-edit-mode chunk size. Decode density is NOT uniform across modes: summary keeps
# 76-90% of the source near-verbatim (the densest Vietnamese decode in the system), so
# an 18-scene summary batch can exceed the 300s per-batch timeout — it needs a SMALLER
# chunk. Lighter modes (commentary/educational) write original, non-verbatim text, so
# they tolerate more — but 18 is the EXACT scene count that timed out under summary, so
# we pull them back to 16 to keep a safety margin instead of sitting at the known-bad 18.
# recap sits in between (12). Each mode is overridable via .env
# (SCRIPT_GEN_CHUNK_SCENES_<MODE>); an unset mode override falls back to the global
# SCRIPT_GEN_CHUNK_SCENES default. _DEFAULT_MODE_CHUNKS holds the starting values.
_DEFAULT_MODE_CHUNKS = {"summary": 10, "recap": 12, "commentary": 16, "educational": 16}
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

# ComfyUI runs on the host (the n8n container reaches it via host.docker.internal).
COMFY_URL = os.getenv("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
SDXL_CHECKPOINT = os.getenv("SDXL_CHECKPOINT", "sd_xl_base_1.0.safetensors")

# Studio "Model dựng" (render_model) -> ComfyUI checkpoint filename. Only sdxl-base
# is installed today; the rest are placeholders until their .safetensors are added.
RENDER_CHECKPOINTS = {
    "sdxl-base": "sd_xl_base_1.0.safetensors",
    "juggernaut-xl": "juggernautXL_v9.safetensors",
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


def _run_cf_worker(script: str, payload: dict, timeout: int, retries: int = 0,
                   retry_backoff: float = 2.0) -> dict:
    """Run a cf-venv worker on a JSON payload and return its JSON result.

    The payload and result are passed as temp files (not stdin/stdout) so library
    logging or HF download progress can never corrupt the parsed output. If a
    progress callback is installed for this thread (set_progress_cb), the worker
    is given a progress file path which we poll while it runs and forward live.

    `retries` (>0) re-runs the worker in a FRESH process when it fails with a
    TRANSIENT GPU-library load error (e.g. cuDNN "Error code 127"). Each retry is
    a clean subprocess, which is what actually clears the flake. Genuine errors
    (bad input, timeout, etc.) are never retried — they fail fast. Used for the
    F5-TTS path, whose CUDA init is the source of the flake.
    """
    attempts = max(1, retries + 1)
    last_err = ""
    for attempt in range(1, attempts + 1):
        try:
            return _run_cf_worker_once(script, payload, timeout)
        except _TransientWorkerLoadError as e:
            last_err = str(e)
            if attempt < attempts:
                log.warning(
                    "[generate] %s transient GPU-load failure (attempt %d/%d), "
                    "retrying in a fresh process: %s",
                    script, attempt, attempts, (last_err or "").strip()[-300:],
                )
                time.sleep(retry_backoff)
                continue
            # Out of retries — surface a clear, actionable message (not a raw stack).
            raise HTTPException(
                503,
                f"{script}: GPU library failed to load after {attempts} attempts "
                f"(transient cuDNN/CUDA load instability — e.g. 'Error code 127'). "
                f"This usually clears by itself; try again. Last error: "
                f"{(last_err or '').strip()[-400:] or 'unknown'}",
            )
    # Unreachable, but keeps the type checker happy.
    raise HTTPException(500, f"{script}: failed ({last_err[-300:]})")


class _TransientWorkerLoadError(RuntimeError):
    """Raised internally when a worker subprocess fails with a transient GPU-library
    load error that is worth retrying in a fresh process."""


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
                        raise HTTPException(504, f"{script} timed out after {timeout}s")
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
            # A transient GPU-library load flake (cuDNN "Error code 127" etc.) is
            # retry-worthy in a fresh process — signal the retry loop. Genuine
            # errors fall through to a fast HTTPException with their real message.
            if _is_transient_load_error(err_txt):
                raise _TransientWorkerLoadError(err_txt.strip()[-800:] or "transient GPU-load failure")
            # Keep the END of stderr — the real traceback is last; the start is
            # just benign model-load warnings/progress bars.
            raise HTTPException(500, f"{script} failed: {err_txt.strip()[-800:] or 'no stderr'}")

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
    "xtts-v2": "XTTS-v2",
    "openvoice-v2": "OpenVoice v2",
    "gpt-sovits": "GPT-SoVITS",
}

# Engine keys actually wired to a TTS code path in tts_worker.py. Anything else
# selected in the Studio is rejected with a clear message (not a 500).
_TTS_ENGINES_IMPLEMENTED = {"vieneu", "f5-tts"}

# FFmpeg bin dir — F5-TTS (and our resample step) need ffmpeg on PATH inside the
# worker. Derived from FFMPEG_BIN so it stays in lockstep with the .env path.
_FFMPEG_DIR_ENV = os.path.dirname(os.getenv("FFMPEG_BIN", "")) if os.getenv("FFMPEG_BIN") else ""


def _engine_from_clone_name(ref_audio: str | None) -> str:
    """Derive the TTS engine from a cloned voice's baked filename suffix.

    upload_voice saves a clone as "<name> - <ShortName>" (e.g. "… - F5-TTS"),
    where ShortName comes from _CLONE_MODEL_SHORT. Legacy clones (no suffix) and
    "… - VieNeu" map to vieneu. Returns a worker engine key (e.g. "f5-tts").
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
            f"Voice engine '{short}' chưa được hỗ trợ — hiện chỉ có VieNeu và F5-TTS.",
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


class ScriptRequest(BaseModel):
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
        "channel's own voice), do NOT output English narration; 'image_prompt' = a 9:16 "
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


def _run_claude_script_once(prompt: str, timeout: int) -> list:
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
    """
    try:
        proc = subprocess.Popen(
            [CLAUDE_BIN, "-p", prompt, "--model", SCRIPT_GEN_MODEL,
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
        return _run_claude_script_body(proc, timeout)
    finally:
        _unregister_job_proc(_kill_job, proc)


def _run_claude_script_body(proc: subprocess.Popen, timeout: int) -> list:
    """The read/parse body of _run_claude_script_once, split out so the proc can be
    registered for immediate-kill and reliably unregistered in a finally."""
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
                f"Claude Code failed (exit {rc}, error_max_turns): "
                f"{errors_joined or 'Reached maximum number of turns'}",
            )
        raise HTTPException(
            500,
            f"Claude Code failed (exit {rc}, {subtype or 'no-result'}): "
            f"{errors_joined or (stderr or '')[:500]}",
        )

    if result_text is None:
        # Stream ended without a result event and without a non-zero exit — treat as
        # an error so we never silently return an empty/partial script.
        raise HTTPException(
            502, f"Claude Code stream ended with no result event (exit {rc}): {(stderr or '')[:300]}"
        )

    try:
        return _extract_json_array(result_text)
    except json.JSONDecodeError:
        raise HTTPException(502, f"Could not parse script JSON: {result_text[:300]}")


def _run_claude_script(prompt: str, timeout: int = 300) -> list:
    """Run Claude Code headless with a bounded number of retries on TIMEOUT only.

    Script gen is a single idempotent, side-effect-free prompt, so a transient
    stall (a slow first-call bootstrap, or a stuck stream) is safely cleared by
    re-running in a FRESH process. We retry ONLY on the 504 timeout — a genuine
    error (bad binary, non-zero exit, unparseable JSON) fails fast with its real
    message. On the final timeout we raise a Vietnamese, user-facing message so the
    failed job row reads clearly in the dashboard.
    """
    attempts = max(1, SCRIPT_GEN_RETRIES + 1)
    for attempt in range(1, attempts + 1):
        try:
            return _run_claude_script_once(prompt, timeout)
        except HTTPException as e:
            if e.status_code != 504:
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
            raise HTTPException(
                504,
                f"Viết kịch bản quá thời gian chờ ({timeout}s) sau {attempts} lần thử. "
                f"Claude Code chạy quá lâu hoặc bị treo — thử lại, hoặc tăng "
                f"SCRIPT_GEN_TIMEOUT trong .env nếu prompt dài.",
            )
    # Unreachable, but keeps the type checker satisfied.
    raise HTTPException(500, "Claude script gen failed unexpectedly")


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


def _run_batches_parallel(prompts: list[str]) -> list[list]:
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
        futs = {ex.submit(_run_claude_script, prompt, SCRIPT_GEN_TIMEOUT): i
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
    scenes = _run_claude_script(prompt, timeout=SCRIPT_GEN_TIMEOUT)
    return {
        "topic": req.topic,
        "durationSec": req.durationSec,
        "sceneCount": len(scenes),
        "scenes": scenes,
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
    "summary": (
        "MODE: SUMMARY (near-full faithful retell). THIS IS NOT A RECAP — do NOT condense "
        "hard and do NOT add analysis. KEEP ~76-90% of the source: retell ALL the "
        "substantive content in the original's EXACT chronological ORDER (do NOT reorder), "
        "faithfully covering every argument, detail, example, and development in the "
        "channel's own Vietnamese narrating voice (NOT a verbatim translation). You MAY "
        "ONLY cut FILLER: like/subscribe/follow prompts, thank-yous, channel "
        "self-promotion, sponsor reads, bloated intros/outros, silent/dead gaps, and "
        "verbatim repetition. NEVER drop real content just to make it shorter, and do NOT "
        "inject your own take/opinion — this is a faithful retell, not a commentary. "
        "Output length tracks the source MINUS the filler. The original footage is "
        "illustration; your Vietnamese narration leads and is the main content; no "
        "verbatim copying, no reupload. Write ALL narration in Vietnamese."
    ),
}
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
_KEEP_RATIO_BAND = {"recap": (0.60, 0.75), "summary": (0.76, 0.90)}
# Out-of-band → bounded REGEN: up to this many EXTRA claude -p calls (so up to 3
# total attempts). Each regen costs another subscription call — keep it bounded.
_RATIO_REGEN_ATTEMPTS = 2
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
# We target ~2.0-2.2 w/s effective; 2.1 centers that band after F5's natural ±10-15%
# delivery wobble (which tends to land slightly under the script density). Do NOT
# also slow the TTS to compensate — that would double-count and run far too slow.
# (Was 2.5 w/s ≈ 150 wpm: too dense, packed words into the target seconds and pushed
# delivery fast / the audio over target — the root cause of the "reads too fast" bug.)
_VI_WORDS_PER_SEC = 2.1
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


def _translate_subs_to_vi(segments: list) -> list[dict]:
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
        "Write all 'text_vi' in VIETNAMESE."
    )
    used = segs[:n_included]
    try:
        raw = _run_claude_script(prompt, timeout=SCRIPT_GEN_TIMEOUT)
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


def _detect_filler_ranges(segments: list, src_dur: float | None = None) -> list[dict]:
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
        raw = _run_claude_script(prompt, timeout=SCRIPT_GEN_TIMEOUT)
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
        if mode == "summary":
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
    """Max TOTAL narration words for a FIXED target duration."""
    return max(1, round((duration_sec or 0) * _VI_WORDS_PER_SEC))


def _auto_word_ceiling(source_seconds: float) -> int:
    """AUTO-mode HARD word ceiling tied to the SOURCE length so the natural-paced VO
    cannot structurally exceed the source. Targets (source - credit slate) with an
    extra safety factor (TTS pace wobble), then converts to words via the shared pace.
    This is the script-side root-cause fix for the "output longer than source" bug —
    we generate LESS text, we never speed up the voice or truncate it."""
    target_seconds = max(1.0, (source_seconds or 0.0) - _CREDIT_SLATE_SEC)
    return max(1, math.floor(target_seconds * _DURATION_SAFETY * _VI_WORDS_PER_SEC))


class TransformRequest(BaseModel):
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
        f"6. {_CUT_PROMO}\n\n"
        f"{title_line}"
        f"Source material (transcript{truncated}):\n\"\"\"\n{transcript}\n\"\"\"\n\n"
        f"{length_line}"
        "Each scene has: 'narration' = the spoken line, concise and natural; 'image_prompt' = a "
        "description of a vertical 9:16 frame, in ENGLISH, for the SDXL model (detailed, cinematic).\n"
        "Write all 'narration' in VIETNAMESE (the channel's own voice) — do NOT output English "
        "narration.\n"
        "Return ONLY a single valid JSON array, with no markdown or explanation. "
        'Each element: {"scene": <number starting at 1>, "narration": "<Vietnamese>", '
        '"image_prompt": "<English prompt>"}.'
    )


class TimedSegment(BaseModel):
    start: float
    end: float
    text: str


class TransformFootageRequest(BaseModel):
    segments: list[TimedSegment]       # timestamped source transcript (from ingest)
    editMode: str = "commentary"
    title: str | None = None
    durationSec: int = 60
    sceneCount: int | None = None
    windowSec: float | None = None     # clamp source ranges to [0, windowSec]
    auto: bool = False                 # AUTO: length follows the source, no fixed target


def _build_footage_prompt(req: TransformFootageRequest, scenes: int, window: float,
                          window_start: float = 0.0, ratio_nudge: str | None = None) -> str:
    """Build the footage script-gen prompt for the source sub-range
    [window_start, window] (window_start defaults to 0.0 = the whole window, the
    single-call case). When CHUNKING, each batch passes its own contiguous
    [window_start, window] sub-range so batches cover disjoint source time-windows
    and never overlap or duplicate footage. The model is told to keep sourceStart/
    sourceEnd INSIDE this sub-range.

    `ratio_nudge` (keep-ratio regen only): when set, the correction text is appended
    to the prompt so the retry steers the kept fraction back into the mode band."""
    guide = EDIT_MODE_GUIDE.get(req.editMode.lower())
    if not guide:
        raise HTTPException(422, f"Unknown editMode '{req.editMode}' (use: {', '.join(EDIT_MODE_GUIDE)})")
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
                f"HARD WORD CAP (ABSOLUTE MAXIMUM, NOT a target): the TOTAL Vietnamese narration "
                f"across ALL scenes must be AT MOST {word_ceiling} words. This is a strict ceiling "
                f"for pacing only (keeps the VO under the source duration) — fewer is fine, more "
                f"is FORBIDDEN. (This source span is {source_seconds:.0f}s.)\n"
                f"This is SUMMARY mode — near-full faithful retell: COVER most of the source "
                f"content in this window ({window_start:.0f}–{window:.0f}s). Trim ONLY genuine "
                f"filler (ads, subscribe/follow prompts, sponsor reads, channel self-promo, "
                f"bloated intros/outros, dead gaps, verbatim repetition). Do NOT drop real "
                f"arguments, examples, or details. Keep the source's chronological ORDER — do "
                f"NOT condense hard, skip substantive beats, or reorder.\n"
                f"The word ceiling is a PACING CONSTRAINT, not a selection filter: cover 76-90% "
                f"of this source window by using footage clips that span most of the timeline; "
                f"narrate each beat concisely in 1-3 tight Vietnamese sentences.\n"
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
        budget = _word_budget(req.durationSec)
        lo = round(budget * 0.9)
        hi = round(budget * 1.1)
        per_scene = max(1, budget // max(1, scenes))
        length_line = (
            f"Write a script ~{req.durationSec}s, split into exactly {scenes} scenes.\n"
            f"WORD BUDGET (TARGET, must STICK to it): the TOTAL Vietnamese narration should be "
            f"about {budget} words (speaking pace ~{_VI_WORDS_PER_SEC} words/sec) — NOT under "
            f"{lo} words and NOT over {hi} words, on average ~{per_scene} words per scene.\n"
            f"ENOUGH LENGTH (important): if the source material is thinner than the budget, "
            f"EXPAND with analysis, context, meaning, examples, and the channel's own take (your "
            f"voice is the main content >60-80%) to hit the target — do NOT repeat points, do NOT "
            f"use empty phrasing, do NOT pad. If it is longer: condense, keep the most important "
            f"parts. The goal is to match the length. For EACH scene:\n"
        )
    return (
        "You are a scriptwriter for a Vietnamese short-video channel that RE-EDITS foreign "
        "content. The source material (footage) is only ILLUSTRATION; your Vietnamese "
        "commentary is the main content. Below is the source transcript with timestamps (seconds).\n\n"
        f"{guide}\n\n"
        "SAFETY RULES (mandatory):\n"
        "1. Real transformation — analyze, reorder, add a take; do NOT translate verbatim.\n"
        "2. Your voice is the main content (>60-80%); the source footage is illustration only.\n"
        "3. The opening has a strong hook in the first 3-5 seconds.\n"
        f"4. {_KEEP_ENGLISH_TERMS}\n"
        f"5. {_CUT_PROMO_FOOTAGE}\n\n"
        f"{title_line}"
        f"Source transcript (seconds, only within {window_start:.0f}-{window:.0f}s):\n\"\"\"\n{transcript}\n\"\"\"\n\n"
        f"{length_line}"
        "- 'narration': the spoken Vietnamese line, concise and natural (your commentary/recap). "
        "Write all 'narration' in VIETNAMESE (the channel's own voice) — do NOT output English narration.\n"
        "- 'sourceStart','sourceEnd': the time range (seconds) in the transcript above to TAKE "
        f"illustrative footage for that scene — must lie within {window_start:.0f}-{window:.0f}s, each clip ~3-8 seconds.\n"
        "Pick the worthwhile moments; you may reorder them for a better narrative flow.\n"
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
    # Guard: if all transcript content fits within the first batch sub-window, there
    # is no point splitting — subsequent sub-windows would contain no transcript and
    # the LLM would hallucinate scenes for empty ranges. Collapse to 1 batch so only
    # one PID opens and the prompt covers the full content span.
    if batches > 1 and req.segments:
        _actual_span = max((s.end for s in req.segments), default=0.0)
        _first_end = window / batches
        if _actual_span <= _first_end * 1.1:
            log.info(
                "[generate] batch guard: all transcript content (%.1fs) fits in "
                "first batch window (%.1fs); collapsing %d batches → 1",
                _actual_span, _first_end, batches,
            )
            batches = 1
    if batches == 1:
        prompt = _build_footage_prompt(req, scene_count, window, ratio_nudge=ratio_nudge)
        return _run_claude_script(prompt, timeout=SCRIPT_GEN_TIMEOUT)

    per_batch = _split_counts(scene_count, batches)
    span = window / batches
    # Build the ORDERED list of per-batch prompts (same sub-window math as before), then
    # run them CONCURRENTLY. _run_batches_parallel preserves input order, so the merged
    # scene order is identical to the old sequential loop — only the timing changes.
    prompts = []
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
            req, n_scenes, sub_end, window_start=sub_start, ratio_nudge=ratio_nudge
        ))
    # Each batch is independently retryable; if one fails (after its own retries) the
    # whole gen aborts fail-fast (the others are drained, the first error re-raised).
    return _merge_renumber(_run_batches_parallel(prompts))


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
    has_band = _KEEP_RATIO_BAND.get(mode) is not None
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

    return {"editMode": mode, "sceneCount": len(clean), "scenes": clean}


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
    if batches == 1:
        prompt = _build_transform_prompt(req, scene_count)
        return _run_claude_script(prompt, timeout=SCRIPT_GEN_TIMEOUT)

    per_batch = _split_counts(scene_count, batches)
    chunks = _split_transcript(req.transcript.strip(), batches)
    # If the transcript is too short to split into B parts, _split_transcript returns
    # one chunk — fall back to a single call so we don't re-send the same text B times.
    if len(chunks) < batches:
        prompt = _build_transform_prompt(req, scene_count)
        return _run_claude_script(prompt, timeout=SCRIPT_GEN_TIMEOUT)

    # Build the ORDERED list of per-batch prompts (same model_copy / chunk logic), then
    # run them CONCURRENTLY. _run_batches_parallel preserves input order, so the merged
    # scene order is identical to the old sequential loop — only the timing changes.
    prompts = []
    for i, (n_scenes, chunk) in enumerate(zip(per_batch, chunks)):
        log.info("[generate] transform batch %d/%d: %d scenes", i + 1, batches, n_scenes)
        sub_req = req.model_copy(update={"transcript": chunk, "sceneCount": n_scenes})
        prompts.append(_build_transform_prompt(sub_req, n_scenes))
    # Fail-fast on any batch error (others drained, first error re-raised).
    return _merge_renumber(_run_batches_parallel(prompts))


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


def _build_workflow(prompt: str, width: int, height: int, steps: int, seed: int,
                    checkpoint: str | None = None) -> dict:
    return {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": 7.0,
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
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": NEGATIVE_PROMPT, "clip": ["4", 1]}},
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
    voice: str | None = None          # preset voice name; None = VieNeu default
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


def _tts_key_for_item(req: "TtsRequest", engine: str, text: str) -> str | None:
    """Compute the per-scene TTS cache key for one narration line, or None if the
    key cannot be derived (never raises — a None key just disables caching for it)."""
    try:
        return tts_cache.tts_cache_key(
            narration=text,
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
    # floor that covers the one-time cold model load/download on first run). F5 is
    # heavier (per-scene GPU inference + resample), so give it a larger per-item budget.
    per_item = 90 if engine == "f5-tts" else 35
    timeout = max(900, per_item * len(misses))
    # F5's CUDA init intermittently flakes ("Error code 127") and clears on a fresh
    # process — retry it. VieNeu is CPU/ONNX (no such flake), so no retry there.
    retries = 2 if engine == "f5-tts" else 0
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
        timeout = 420 if eng == "f5-tts" else 300
        retries = 2 if eng == "f5-tts" else 0  # ride out the transient cuDNN-127 load flake
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
X264_PRESET = os.getenv("ASSEMBLE_X264_PRESET", "veryfast")
X264_CRF = os.getenv("ASSEMBLE_X264_CRF", "23")
NVENC_PRESET = os.getenv("ASSEMBLE_NVENC_PRESET", "p1")
NVENC_CQ = os.getenv("ASSEMBLE_NVENC_CQ", "25")

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


def _video_encoder_args() -> list[str]:
    """Video-encoder ffmpeg args for an assemble clip. h264/yuv420p either way, so
    the concat stays a stream-copy and the output format is unchanged.

    Resolves the encoder lazily (NVENC by default with CPU fallback; see
    _resolve_venc). NVENC is tuned for speed (p1/cq25); libx264 stays veryfast/crf23."""
    if _resolve_venc() == "nvenc":
        return ["-c:v", "h264_nvenc", "-pix_fmt", "yuv420p",
                "-preset", NVENC_PRESET, "-rc", "vbr", "-cq", NVENC_CQ, "-b:v", "0"]
    return ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", X264_PRESET, "-crf", X264_CRF]


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
            self.cb(int(round(pct)), f"{self.label} {int(round(frac * 100))}%")
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


def _make_assemble_progress(scenes, durs, bgm_path, add_credit, logo_path, handle, source_name):
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
    prog = _AssembleProgress(cb, total, lo=0, hi=100)
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
    """Probe duration + the first video stream's width/height (ffprobe). Used to
    rebuild the download_source_video result for a CACHE HIT so it matches a fresh
    download's contract. Best-effort: returns 0/None on any probe failure."""
    out = {"durationS": _probe_duration(path), "width": None, "height": None}
    # Force UTF-8 (see _probe_duration): Vietnamese path may appear in stderr.
    proc = subprocess.run(
        [FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    try:
        w, h = (proc.stdout or "").strip().split("x")[:2]
        out["width"], out["height"] = int(w), int(h)
    except (ValueError, AttributeError):
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
    """Build the per-scene video filter: cover-fit → Ken Burns zoom → caption."""
    frames = round(dur * fps) + fps  # +1s buffer; -t trims to the audio length
    f = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,"
        f"zoompan=z='min(zoom+{0.10 / max(frames, 1):.6f},1.10)'"
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

    with tempfile.TemporaryDirectory() as work:
        # 1) one clip per scene (image + Ken Burns + caption + its audio), encoded
        #    concurrently with a bounded pool (see _scene_encode_workers); the clips
        #    are returned IN SCENE ORDER so the concat list below stays deterministic.
        def _encode_image_scene(p):
            s, dur = p
            vf = _scene_filter(req.width, req.height, req.fps, dur, s.caption if req.captions else None, work, s.scene)
            clip = os.path.join(work, f"scene_{s.scene:03d}.mp4")
            _run_ffmpeg(
                [
                    "-i", s.imagePath, "-i", s.audioPath,
                    "-filter_complex", vf,
                    "-map", "[v]", "-map", "1:a",
                    "-t", f"{dur:.3f}",
                    "-r", str(req.fps),
                    *_video_encoder_args(),
                    "-c:a", "aac", "-ar", "48000",
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
                  bgm_path, bgm_volume, logo_path=None, handle=None, add_credit=True, prog=None):
    """Append a source-credit slate, concat all clips, mix optional bgm. Shared
    by image- and footage-mode assembly (clips must share codec params).

    Credit slate (3s, black): the source channel's LOGO centered on one row and
    "/@handle" centered on a second row below it. Skipped entirely when
    add_credit is False or there's nothing to credit.

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
    video_secs = 1.0
    if prog is not None:
        video_secs = max(1.0, prog.total - getattr(prog, "slate_w", 0.0)
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
                    "-t", "3", *_video_encoder_args(),
                    "-c:a", "aac", "-ar", "48000", slate,
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
                    "-t", "3", *_video_encoder_args(),
                    "-c:a", "aac", "-ar", "48000", slate,
                ],
                step="credit slate",
            ))
        clips.append(slate)

    # concat (same codec params → stream copy)
    list_path = os.path.join(work, "concat.txt")
    with open(list_path, "w", encoding="utf-8") as fh:
        for c in clips:
            fh.write(f"file '{c.replace(chr(92), '/')}'\n")
    concat_out = out_path if not bgm_path else os.path.join(work, "concat.mp4")
    _do(concat_w, video_secs, lambda: _run_ffmpeg(
        ["-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", concat_out], step="concat"))

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
                "-c:v", "copy", "-c:a", "aac", "-ar", "48000",
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
HL_SCALE = 124           # % size of the spoken word


# Vietnamese-aware word tokenizer for the CORRECT caption text (the script's own
# narration). Splits on whitespace, keeping each whitespace-delimited token (which
# carries its diacritics) intact. We deliberately keep attached punctuation on the
# token (e.g. "gì." / "rối,") so the rendered caption reads exactly like the script.
_WORD_RE = re.compile(r"\S+")


def _tokenize_narration(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").strip())


def _aligned_caption_words(narration: str | None, whisper_words: list[dict],
                           audio_dur: float) -> list[dict]:
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
    tokens = _tokenize_narration(narration)
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
    weights = [max(1, len(t)) for t in tokens]
    total_w = sum(weights)
    out: list[dict] = []

    if whisper_words and len(whisper_words) >= 2:
        nw = len(whisper_words)
        cum = 0.0
        for i, tok in enumerate(tokens):
            # Proportional position of this token within the narration (by char
            # weight) mapped onto whisper's word index. Anchor each token at its
            # CENTER of mass (cum + half its own weight), not its leading edge:
            # leading-edge + int()-floor anchoring borrowed the start of the
            # PRECEDING whisper word, making every caption pop ~half-a-word to a
            # full word BEFORE the audio (the "captions lead by ~0.5 words" bug).
            w = weights[i]
            frac_center = (cum + w / 2.0) / total_w
            cum += w
            frac_end = cum / total_w
            wi_start = min(nw - 1, max(0, int(frac_center * nw)))
            # End index still bounds the token's own span; keep it >= wi_start so en >= st holds.
            wi_end = min(nw - 1, max(wi_start, int(frac_end * nw - 1e-9)))
            st = float(whisper_words[wi_start].get("start", span_start) or span_start)
            en = float(whisper_words[wi_end].get("end", st) or st)
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

    # Guarantee monotonic, non-overlapping starts so the karaoke pop advances cleanly.
    for i in range(1, len(out)):
        if out[i]["start"] < out[i - 1]["start"]:
            out[i]["start"] = out[i - 1]["start"]
        if out[i]["end"] < out[i]["start"]:
            out[i]["end"] = out[i]["start"] + 0.12
    return out


# Single-line karaoke chunk size. The owner wants a FIXED single line that does
# not jump: as time advances the current line is CLEARED and the next short chunk
# shown in the SAME position (no stacking, no wrap to 2-3 lines). We therefore show
# SMALL chunks (a few words) one at a time. KARAOKE_MAX_WORDS caps the word count
# per line; the char budget below also caps width so a chunk always fits one row.
KARAOKE_MAX_WORDS = int(os.getenv("KARAOKE_MAX_WORDS", "5"))


def _build_karaoke_ass(words: list[dict], width: int, height: int, work: str, idx: int) -> str | None:
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
    """
    if not words:
        return None
    fontsize = max(36, int(width // 17 * 0.8))
    # Fixed vertical anchor. Alignment 2 = bottom-center; MarginV is the SAME for
    # every event, so the (single) line sits at one fixed Y for the whole scene.
    margin_v = max(120, height // 8)
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
    # Width budget for ONE row. Reserve headroom so even the enlarged (HL_SCALE)
    # spoken word never pushes the line past the safe area. Be Vietnam Pro Bold
    # averages ~0.56*fontsize px/glyph. We keep chunks SMALL (KARAOKE_MAX_WORDS),
    # which is the primary single-line guarantee; the char cap is a width backstop.
    usable_px = max(1, width - 2 * margin)
    avg_glyph = 0.56 * fontsize
    # Reserve for one word grown by (HL_SCALE-100)% (~a few extra glyphs of width).
    headroom_px = avg_glyph * ((HL_SCALE - 100) / 100.0) * 6
    max_chars = max(8, int((usable_px - headroom_px) / avg_glyph))

    # Punctuation that ENDS a caption line: a new chunk starts after any word whose
    # visible token ends with one of these. Sentence ends (. ? ! …) and clause ends
    # (, ; :) all force a break so each caption line is one sentence/clause. The
    # punctuation stays attached to the word (the caption still shows it).
    BREAK_PUNCT = (".", ",", "?", "!", ";", ":", "…")

    def _ends_with_break(tok: str) -> bool:
        # Treat a literal "..." ellipsis as a break too (covered by endswith "." but
        # kept explicit for clarity). Strip nothing else — punctuation is preserved.
        return tok.endswith(BREAK_PUNCT) or tok.endswith("...")

    def _chunk_words(ws: list[dict]) -> list[list[dict]]:
        groups, cur, cur_len = [], [], 0
        for w in ws:
            tok = _ass_escape(w["word"]).strip()
            add = len(tok) + (1 if cur else 0)
            # Width / word-count cap: a very long clause with NO internal punctuation
            # still falls back to these so a single row never overflows.
            if cur and (cur_len + add > max_chars or len(cur) >= KARAOKE_MAX_WORDS):
                groups.append(cur)
                cur, cur_len = [], 0
                add = len(tok)
            cur.append(w)
            cur_len += add
            # Punctuation-aware break: end the current chunk AFTER appending this word
            # if it terminates a sentence/clause, so the NEXT line starts fresh. The
            # punctuation remains on `tok` (kept attached, never dropped). Empty chunks
            # are impossible here because `cur` just received this word.
            if cur and _ends_with_break(tok):
                groups.append(cur)
                cur, cur_len = [], 0
        if cur:
            groups.append(cur)
        return groups

    groups = _chunk_words(words)
    # Each chunk owns a contiguous time span [chunk_start, next_chunk_start). Within
    # it, the highlight moves word by word, every word-event butting up against the
    # next so there is NEVER a gap (blank screen) and NEVER an overlap (two lines
    # stacked). The chunk's last word holds until the next chunk begins, so the line
    # is continuously present, then is REPLACED in place by the next chunk.
    #
    # Build (start, end, text) tuples first, THEN clamp each event's end to the next
    # event's start. Whisper word timestamps are only weakly monotonic (two words can
    # share a start across a chunk boundary), which can otherwise leave two events
    # overlapping by a few cs — and overlapping events with the same alignment make
    # libass STACK them into a 2nd line (the exact "jumping/lung tung" bug). The final
    # clamp guarantees AT MOST ONE event is on screen at any instant: one fixed line.
    raw_events: list[tuple[float, float, str]] = []
    for gi, group in enumerate(groups):
        n = len(group)
        # When does this chunk hand off to the next? At the next chunk's first word
        # start (so the current line clears exactly as the next appears). For the
        # final chunk, hold a touch past its last spoken word.
        if gi + 1 < len(groups):
            chunk_end = float(groups[gi + 1][0]["start"])
        else:
            chunk_end = float(group[-1]["end"]) + 0.40
        for k in range(n):
            start = float(group[k]["start"])
            # This word's slot runs until the NEXT word in the chunk starts; the last
            # word in the chunk holds until chunk_end (the handoff point).
            end = float(group[k + 1]["start"]) if k + 1 < n else chunk_end
            segs = []
            for j, g in enumerate(group):
                tok = _ass_escape(g["word"])
                if j == k:
                    segs.append(f"{{\\fscx{HL_SCALE}\\fscy{HL_SCALE}\\c{HL_COLOR}}}{tok}{{\\r}}")
                else:
                    segs.append(tok)
            raw_events.append((start, end, " ".join(segs)))

    # Enforce strictly single-line: monotonic starts, and each event ends no later
    # than the next event starts (no two events ever overlap → no vertical stacking).
    raw_events.sort(key=lambda e: (e[0], e[1]))
    # Collapse events that share a start time (weakly-monotonic whisper timestamps:
    # two words can report the same start). Among equal-start events keep only the
    # LAST one — it carries the most-advanced highlight — so we never emit two events
    # beginning at the same instant (which would stack two lines). This is the key to
    # a single fixed line.
    dedup: list[tuple[float, float, str]] = []
    for ev in raw_events:
        if dedup and abs(ev[0] - dedup[-1][0]) < 1e-3:
            dedup[-1] = ev  # supersede the same-start predecessor
        else:
            dedup.append(ev)
    # Now clamp each event's end to the NEXT event's start so exactly one event is on
    # screen at any instant (no overlap → no vertical stacking), with no time gaps.
    events = []
    for i, (start, end, text) in enumerate(dedup):
        nxt = dedup[i + 1][0] if i + 1 < len(dedup) else end
        end = nxt if nxt > start else (end if end > start else start + 0.10)
        events.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}")
    path = os.path.join(work, f"cap_{idx}.ass")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header + "\n".join(events) + "\n")
    return path


def _footage_scene_clip(src_clip: str, vo_audio: str, dur: float, ass_path: str | None,
                        width: int, height: int, fps: int, work: str, idx: int,
                        src_audio_volume: float = 0.0) -> str:
    """Render one footage scene: 9:16 blurred-bg fit + karaoke captions, lasting
    exactly the VO duration.

    Audio: by DEFAULT (src_audio_volume == 0) the output carries ONLY the Vietnamese
    voiceover — the original English source audio is dropped entirely, so the video
    has a single, unified voice (no two-voices artifact). When src_audio_volume > 0
    the source audio is mixed UNDER the VO at that linear gain (the VO stays at full
    level and remains dominant); typical faint-bed values are 0.05–0.15.

    Video: the source clip is shorter than the VO often (the VO is the new, longer
    narration). Rather than FREEZING the last frame (tpad clone) for many seconds,
    we LOOP the source clip so the visual keeps moving for the whole VO duration,
    then trim to `dur`. A still freeze for >1–2s reads as a broken render.
    """
    # The filtergraph references [0:v]; if the cut clip lost its video stream (range
    # overshot the source end → audio-only slice), fail with a clear message instead
    # of the opaque ffmpeg "[0:v] matches no streams" crash.
    if not _has_video(src_clip):
        raise RuntimeError(f"footage scene {idx}: cut clip has no video stream ({src_clip}); "
                           "the scene's source range likely overshot the video end")
    mix_src = src_audio_volume > 0.0 and _has_audio(src_clip)
    # Loop the source video so a short clip keeps moving under a longer VO instead
    # of holding a single frozen frame. -stream_loop on the input + trim to dur.
    # (loop is applied as an input option below, not in the filtergraph.)
    vf = (
        f"[0:v]setpts=PTS-STARTPTS,split[bg][fg];"
        f"{_bg_blur_chain(width, height)};"
        f"[fg]scale={width}:-2:force_original_aspect_ratio=decrease[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,"
        # Guarantee the visual fills the whole VO: pad (clone) only as a last resort,
        # but because we loop the input it virtually never triggers; then trim to dur.
        f"tpad=stop_mode=clone:stop_duration=2,fps={fps},trim=duration={dur:.3f},setpts=PTS-STARTPTS[vbase]"
    )
    if ass_path:
        vf += (f";[vbase]subtitles='{_ff_filter_path(ass_path)}'"
               f":fontsdir='{_ff_filter_path(CAPTION_FONTSDIR)}'[v]")
    else:
        vf += ";[vbase]null[v]"
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
            # Loop the source footage so a short clip never freezes under a longer VO.
            "-stream_loop", "-1", "-i", src_clip, "-i", vo_audio,
            "-filter_complex", vf,
            "-map", "[v]", "-map", "[a]",
            "-t", f"{dur:.3f}", "-r", str(fps),
            *_video_encoder_args(),
            "-c:a", "aac", "-ar", "48000",
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
    addCredit: bool = True
    # Source (original) audio gain UNDER the Vietnamese voiceover. 0.0 (default) =
    # the source audio is dropped entirely, so the output has ONLY the unified VO.
    # 0.05/0.10/0.15 = mix the source faintly under the VO (VO stays dominant).
    srcAudioVolume: float = 0.0
    outDir: str | None = None
    videoId: int | None = None  # when set, the output filename is suffixed " (v<id>)" so re-renders don't overwrite each other


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

    # Word timestamps for ALL scenes in ONE whisper load (drives karaoke captions).
    word_map: dict[int, list] = {}
    if req.captions:
        res = _run_cf_worker(
            "whisper_worker.py",
            {"items": [{"scene": s.scene, "audioPath": s.audioPath} for s in req.scenes],
             "model": WHISPER_MODEL, "device": WHISPER_DEVICE, "compute": WHISPER_COMPUTE,
             "language": "vi", "wordTimestamps": True},
            timeout=1200,
        )
        for r in res["results"]:
            word_map[r["scene"]] = [w for seg in r.get("segments", []) for w in (seg.get("words") or [])]

    durs: list[float] = []
    for s in req.scenes:
        d = s.durationS or _probe_duration(s.audioPath)
        if d <= 0:
            raise HTTPException(422, f"scene {s.scene}: could not determine VO duration")
        durs.append(d)
    prog = _make_assemble_progress(req.scenes, durs, req.bgmPath, req.addCredit,
                                   req.sourceLogo, req.sourceHandle, req.sourceName)

    with tempfile.TemporaryDirectory() as work:
        # Caption TEXT = the CORRECT script narration; caption TIMING = whisper word
        # timestamps. Whisper mishears Vietnamese (e.g. "Agent harness" -> "Dân vật
        # A.A.A.Harnes"), so we never render its text — we lay the known narration
        # tokens onto whisper's measured timing (forced-alignment-lite). Falls back to
        # whisper words verbatim only when no narration is present. The .ass files are
        # built up front (cheap, single-threaded) so the parallel workers only do the
        # heavy ffmpeg encode; clips come back IN SCENE ORDER for a deterministic concat.
        items = []
        for s, dur in zip(req.scenes, durs):
            cap_words = _aligned_caption_words(s.caption, word_map.get(s.scene, []), dur)
            ass = _build_karaoke_ass(cap_words, req.width, req.height, work, s.scene) if req.captions else None
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
    fontsize = max(36, int(width // 20))
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
             *_video_encoder_args(), "-c:a", "aac", "-ar", "48000", "-ac", "2", seg],
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
         *_video_encoder_args(), "-c:a", "aac", "-ar", "48000", "-ac", "2", out_path],
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
                 *_video_encoder_args(), "-c:a", "aac", "-ar", "48000", "-ac", "2",
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


# --- Footage download: one start-anchored window, cut scenes locally -----

FOOTAGE_MAX_HEIGHT = int(os.getenv("FOOTAGE_MAX_HEIGHT", "720"))


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


def _cut_clip(src_video: str, start: float, end: float, dest: str) -> float:
    """Cut [start,end] from a local video (re-encode for accurate cuts). Returns
    the clip duration. Falls back to a 2s minimum if the range is degenerate."""
    dur = max(0.5, float(end) - float(start))
    _run_ffmpeg(
        ["-ss", f"{max(0.0, float(start)):.3f}", "-i", src_video, "-t", f"{dur:.3f}",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ar", "48000", dest],
        step="cut clip",
    )
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
