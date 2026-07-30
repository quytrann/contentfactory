"""Background runner — turns queued jobs into finished videos, end to end.

A single worker thread (the 8 GB GPU runs models sequentially, so concurrency
buys nothing) claims one queued job at a time and drives the whole pipeline,
committing each step so the dashboard reflects progress live:

    jobs.status:   queued -> running -> done | failed
    videos.status: rendering -> ready -> published | failed

Pipeline by page architecture:
    translate        : ingest -> script(transform) -> TTS -> images -> assemble -> upload
    story_voiceover  : script(topic) -> TTS -> images -> assemble -> upload

Image generation needs ComfyUI (:8188); if it's down the job fails with a clear
message. The YouTube upload is best-effort — without an OAuth token the video is
left 'ready' (produced but unpublished), never failed.
"""

import glob
import math
import os
import shutil
import frames_util
import sys
import threading
import time
import traceback
import urllib.request

# Vietnamese page names / titles flow into video PATHS that we print on the
# success path (e.g. "E:\ContentFactory\Giải Thích Mọi Thứ\..."). On a Windows
# console whose stdout is cp1252, print()-ing such a string raises
# UnicodeEncodeError — which, on the job's SUCCESS path, would be caught by the
# job's except and wrongly flip an already-finished job to 'failed'. Force UTF-8
# (with a safe fallback) so a non-ASCII path/title can never kill a job. Idempotent.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass

from psycopg.types.json import Json

import render_cache
from db import get_conn

# Job IDs whose cancellation was requested via POST /api/jobs/{id}/stop.
# Populated by main.py's stop endpoint; checked at step boundaries in _process_job.
_CANCEL_REQUESTED: set[int] = set()

# Job IDs the user explicitly STOPPED (POST /stop). This is SEPARATE from
# _CANCEL_REQUESTED, which _check_cancel CONSUMES (discards) the moment it fires.
# A stop also immediately tree-kills the active subprocess, so the job usually
# crashes mid-step with a generic ffmpeg/worker error BEFORE reaching the next
# _check_cancel boundary. The failure handler consults THIS set to classify such a
# crash as a deliberate user-stop ('stopped') rather than a real fault ('failed').
# Entries are cleared when the job finalizes (_job_stopped / _job_failed / _job_done).
_STOPPED_JOBS: set[int] = set()


class JobStopped(RuntimeError):
    """Raised by _check_cancel when the user stopped the job at a step boundary.
    Caught by the failure handler and finalized as status='stopped' (NOT 'failed').
    A genuine pipeline error raises a different exception type and stays 'failed'."""


def _check_cancel(job_id: int) -> None:
    """Raise JobStopped if a stop was requested for this job (consumes the request)."""
    if job_id in _CANCEL_REQUESTED:
        _CANCEL_REQUESTED.discard(job_id)
        raise JobStopped("Dừng bởi người dùng")


def _was_stopped(job_id: int) -> bool:
    """True if the user stopped this job (POST /stop marked it). Used by the failure
    handler to decide 'stopped' vs 'failed' when a kill-induced exception surfaces
    that did NOT come through _check_cancel."""
    return job_id in _STOPPED_JOBS
# Shared publish core — the SAME path the manual POST /api/videos/{id}/publish
# endpoint uses. Leaf module (imports only db + uploaders), so no import cycle
# with main.py (which imports this runner module).
from publish_core import API_UPLOAD_ENABLED, publish_video_core
# Provider gate for TEXT script-gen. Imported for `resolve()` only — the runner never
# calls an LLM itself, it just needs the concrete provider/model ids to record on the
# video row. Leaf module (no import cycle: llm_gate imports generate lazily).
import llm_gate
from generate import (
    CONTENT_OUTPUT_ROOT,
    INGEST_MAX_SEC,
    RENDER_CHECKPOINTS,
    AssembleRequest,
    AssembleScene,
    DubbedAssembleRequest,
    FootageAssembleRequest,
    FootageScene,
    ImageScene,
    ImagesRequest,
    IngestRequest,
    ScriptRequest,
    SourceVideoRequest,
    TimedSegment,
    TransformFootageRequest,
    sdxl_dims_for_aspect,
    TransformRequest,
    TtsRequest,
    TtsScene,
    _cut_clip,
    _detect_filler_ranges,
    _fb_tag_clean,
    _generate_fb_tags,
    _probe_duration,
    _run_ffmpeg,
    _scene_encode_workers,
    _page_clips_dir,
    _page_source_dir,
    _shared_voice_dir,
    _translate_subs_to_vi,
    assemble,
    assemble_dubbed,
    assemble_footage,
    assemble_translate_full,
    bake_cover_first_frame,
    TranslateFullAssembleRequest,
    download_source_video,
    generate_images,
    generate_ingest,
    generate_script,
    generate_script_footage,
    generate_script_transform,
    generate_script_visual_explain,
    generate_tts,
    append_outro,
    CF_OUTRO_CARD_SEC,
    kill_job_processes,
    make_thumbnail,
    render_stickman_clip,
    render_stickman_clip_procedural,
    set_active_job,
    set_ff_progress_cb,
    set_model_busy,
    set_progress_cb,
)
from worker_errors import friendly_job_error

POLL_SECONDS = float(os.getenv("RUNNER_POLL_SECONDS", "3"))
DEFAULT_DURATION = int(os.getenv("RUNNER_TARGET_SECONDS", "60"))

# Output frame size per chosen aspect ratio (width, height).
ASPECTS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "4:5": (1080, 1350)}

# translate_full: caption band (fractional y-range of the SOURCE frame) the
# caption-cover detector scans for the source's own burned-in captions. Default
# bottom band [0.72, 0.90] worked on the de-risk test short. Exposed as an env
# knob so it can be tuned per install; "no captions found -> 0 intervals" is valid.
def _translate_full_caption_band() -> tuple[float, float]:
    raw = os.getenv("TRANSLATE_FULL_CAPTION_BAND", "0.72,0.90")
    try:
        a, b = (float(x) for x in raw.split(","))
        if 0.0 <= a < b <= 1.0:
            return (a, b)
    except (ValueError, TypeError):
        pass
    return (0.72, 0.90)

# How many independent per-scene FFmpeg cuts to run concurrently in the footage
# CUT step. Each _cut_clip reads the SAME read-only source and writes a DISTINCT
# scene{NNN}.mp4 dest, so they are fully independent → safe to parallelize. We
# REUSE the assemble step's resolved worker count (_scene_encode_workers) so the
# cut step respects the exact same encoder-aware cap: _cut_clip uses libx264
# (CPU) like the assemble fallback, so the relevant limiter is CPU cores, and
# the same NVENC-session clamp is honored when that path is ever chosen. An
# explicit CUT_PARALLEL overrides only the *request* (still clamped by
# _scene_encode_workers); CUT_PARALLEL=1 is the serial kill-switch reproducing
# the exact previous loop. Default (unset) inherits ASSEMBLE_PARALLEL.
_CUT_PARALLEL_ENV = os.getenv("CUT_PARALLEL")


def _cut_workers() -> int:
    """Concurrency for the cut loop. Inherits the assemble worker count
    (_scene_encode_workers, which already honors ASSEMBLE_PARALLEL + the encoder
    cap + the kill-switch). If CUT_PARALLEL is set explicitly, it overrides the
    *requested* count but is still clamped to a safe value (>=1)."""
    base = max(1, _scene_encode_workers())
    if _CUT_PARALLEL_ENV is None:
        return base
    try:
        want = max(1, int(_CUT_PARALLEL_ENV))
    except (TypeError, ValueError):
        return base
    if want == 1:
        return 1  # explicit serial kill-switch
    # Never exceed the encoder-aware safe ceiling resolved for assemble.
    return min(want, base)


def _run_cuts(items, fn, max_workers: int):
    """Run independent per-scene cut tasks concurrently, returning results IN
    INPUT ORDER (so the visuals/f_scenes order is preserved regardless of which
    cut finishes first). max_workers <= 1 falls back to the plain serial path so
    the kill-switch reproduces the exact previous behavior. The caller drives the
    monotonic per-scene progress via on_done(i) as each cut completes."""
    n = len(items)
    results: list = [None] * n
    if n == 0:
        return results
    if max_workers <= 1:
        for i, payload in enumerate(items):
            results[i] = fn(payload)
        return results
    import concurrent.futures
    errs: list = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(fn, payload): i for i, payload in enumerate(items)}
        for fut in concurrent.futures.as_completed(futs):
            i = futs[fut]
            try:
                results[i] = fut.result()
            except BaseException as e:  # noqa: BLE001 — re-raise after draining
                errs.append(e)
    if errs:
        raise errs[0]
    return results


_started = False
_lock = threading.Lock()


# ---- small DB helpers (short-lived conn each → progress commits immediately) --

def _with_src_audio_volume(req, vol: float):
    """Set the source-audio volume on an assemble request, matching the agreed
    contract (the assemble fn accepts src_audio_volume; the request field is
    `srcAudioVolume`, mirroring bgmVolume). Tolerate generate.py not having added
    the field yet — never crash the runner over an interim signature mismatch."""
    if hasattr(req, "srcAudioVolume"):
        try:
            req.srcAudioVolume = vol
        except Exception:
            pass
    return req


def _claim_job() -> dict | None:
    """Atomically take the oldest queued job and mark it running."""
    with get_conn() as conn:
        return conn.execute(
            """
            UPDATE jobs SET status = 'running'
             WHERE id = (
                 SELECT id FROM jobs WHERE status = 'queued'
                  ORDER BY created_at, id LIMIT 1
                  FOR UPDATE SKIP LOCKED
             )
            RETURNING id, page_id, input_type, input_payload, voice, edit_mode, comment,
                      source_video_id, aspect, target_sec, add_credit, publish,
                      render_mode, render_model, voice_clone_model, src_audio_volume,
                      llm_provider, llm_model,
                      clone_of_video_id,
                      reuse_script_video_id, bypass_tts_cache, bypass_script_cache, title, publish_platform,
                      cover_image_path, facebook_tags
            """
        ).fetchone()


def _load_page(page_id: int) -> dict | None:
    with get_conn() as conn:
        # `credit` carries the page's OWN branding handle (e.g. "@giaithichmoithu") shown on
        # the final black outro card. It is one of the owner-owned identity fields (see the
        # pages table comment): NULL means the owner has not supplied one, and we then render
        # NO outro card rather than inventing a handle.
        return conn.execute(
            "SELECT id, name, creator_name, credit FROM pages WHERE id = %s",
            (page_id,),
        ).fetchone()


def _page_outro_handle(page: dict | None) -> str | None:
    """The page's own outro-card handle (pages.credit), or None when unset."""
    return ((page or {}).get("credit") or "").strip() or None


def _resolve_publish_account_id(page_id: int, platform: str) -> int | None:
    """Resolve the platform_accounts.id for a job's chosen auto-publish platform.

    Per-platform auto-publish: when jobs.publish_platform is set, publish ONLY to
    that platform's connected channel of the job's OWN page (borrowed-account rule:
    identity is always the page's own platform_accounts row, never inferred).
    Returns the account id, or None when the page has no row for that platform
    (caller skips auto-publish with a warning — never fails the job).
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM platform_accounts WHERE page_id = %s AND platform = %s",
            (page_id, platform),
        ).fetchone()
    return row["id"] if row else None


def _resolve_publish_account_ids(job: dict) -> list[int] | None:
    """Translate a job's publish target into the account_ids arg for publish_video_core.

    - publish_platform set  -> [account_id] for that page+platform, or None when no
      matching account exists (caller treats None-with-platform as "skip", logged).
    - publish_platform empty -> None (AUTO mode: ALL connected channels of the page).

    Returns a tuple-like marker via a sentinel is avoided; callers distinguish the
    two None cases by re-checking job['publish_platform'].
    """
    platform = (job.get("publish_platform") or "").strip().lower() or None
    if platform is None:
        return None  # AUTO mode: all channels
    account_id = _resolve_publish_account_id(job["page_id"], platform)
    return [account_id] if account_id is not None else None


def _video_for_job(job_id: int) -> dict | None:
    """The (single) video row created for this job — used to resolve a clone job's
    destination video that the clone endpoint already inserted."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT id FROM videos WHERE job_id = %s ORDER BY id LIMIT 1", (job_id,)
        ).fetchone()


def _create_video(job_id: int, page_id: int, facebook_tags: str | None = None) -> int:
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO videos (job_id, page_id, status, facebook_tags) "
            "VALUES (%s, %s, 'rendering', %s) RETURNING id",
            (job_id, page_id, facebook_tags),
        ).fetchone()["id"]


def _load_reusable_script(src_video_id: int) -> tuple[list | None, str | None, str | None, str | None, str | None]:
    """Load a source video's saved script for the PART-B script-reuse path.

    Returns (scenes, title, source_name, source_link, facebook_tags). `scenes` is the
    stored videos.script JSONB (a list of scene dicts) or None when the row is missing
    or its script is NULL/empty. The title/source_* are surfaced so the reusing job can
    carry the SAME credit/title hint as the source (the runner still lets a
    user-supplied job title override it, exactly like the fresh path). facebook_tags is
    surfaced so the caller can copy it onto the new video row and skip the redundant
    `claude -p` hashtag call in _auto_fill_fb_tags (the reused narration is identical,
    so regenerating tags from it is wasted subscription usage)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT script, title, source_name, source_link, facebook_tags FROM videos WHERE id = %s",
            (src_video_id,),
        ).fetchone()
    scenes = row["script"] if row else None
    # psycopg3 returns JSONB as a parsed Python object (list); guard non-list/empty.
    if isinstance(scenes, list) and scenes:
        return (scenes, row["title"], row["source_name"], row["source_link"],
                row["facebook_tags"])

    # MANIFEST FALLBACK — an orphaned-manifest script (the DB video row was deleted
    # with keepScript=true, or the script is otherwise NULL/empty). Rebuild the scene
    # list from _cache/renders/<id>/manifest.json so a script-only reuse still works.
    # Manifest scenes carry {scene, narration, caption, durationS} — the same shape
    # the reuse->TTS path reads (footage cut timecodes are absent here, so this is
    # for image/stickman reuse, which is exactly the "script-only" intent). The
    # manifest never carries facebook_tags, so this path leaves it None — the caller's
    # normal auto-generate step still fires, which is correct (no stored tags exist).
    manifest = render_cache.load_manifest(src_video_id)
    if manifest and isinstance(manifest.get("scenes"), list) and manifest["scenes"]:
        m_scenes = [
            {
                "scene": s.get("scene"),
                "narration": s.get("narration"),
                "caption": s.get("caption") if s.get("caption") is not None else s.get("narration"),
            }
            for s in manifest["scenes"]
            if isinstance(s, dict)
        ]
        print(f"[runner] _load_reusable_script: video {src_video_id} has no DB script "
              f"— rebuilt {len(m_scenes)} scenes from render-cache manifest")
        return (m_scenes, manifest.get("title"),
                manifest.get("sourceName"), manifest.get("sourceLink"), None)

    if not row:
        return None, None, None, None, None
    return None, row["title"], row["source_name"], row["source_link"], row["facebook_tags"]


def _save_script(video_id: int, title: str | None, scenes: list, source_name: str | None,
                 source_link: str | None,
                 llm_used: tuple[str, str | None] | None = None) -> None:
    """Persist the finished script onto the video row.

    `llm_used` = (provider, model) that ACTUALLY produced this script, or None when NO
    LLM ran (script reuse / retry reuse / dubbed resume). None leaves
    videos.llm_provider_used / llm_model_used UNTOUCHED on purpose: recording a provider
    for a run that made no call would be a lie, and on a resume it would also clobber the
    truthful value the original run wrote."""
    with get_conn() as conn:
        if llm_used is None:
            conn.execute(
                "UPDATE videos SET title = %s, script = %s, source_name = %s, source_link = %s WHERE id = %s",
                (title, Json(scenes), source_name, source_link, video_id),
            )
        else:
            conn.execute(
                "UPDATE videos SET title = %s, script = %s, source_name = %s, "
                "source_link = %s, llm_provider_used = %s, llm_model_used = %s "
                "WHERE id = %s",
                (title, Json(scenes), source_name, source_link,
                 llm_used[0], llm_used[1], video_id),
            )


# ---- Automatic Facebook hashtags (post-script) -------------------------------

def _narration_from_script(script) -> str:
    """Concatenate the Vietnamese narration text out of a saved script into one
    plain-text blob, used as the CONTENT signal for content-aware FB-tag generation.

    Handles BOTH stored shapes gracefully:
      - scene ARRAY (image/footage/stickman/translate_full/clone):
        [{ "scene": n, "narration": "...", ... }, ...] -> join the 'narration' text.
      - dubbed / translate_full DICT: { "mode": ..., "subs": [{ "text_vi": "..." }] }
        -> join the 'text_vi' subtitle text.
    Returns "" for anything it cannot read; the caller then falls back to a
    title-only prompt (still valid, just less content-specific)."""
    parts: list[str] = []
    if isinstance(script, list):
        for s in script:
            if isinstance(s, dict):
                txt = s.get("narration") or s.get("text") or s.get("caption") or ""
            else:
                txt = s if isinstance(s, str) else ""
            if isinstance(txt, str) and txt.strip():
                parts.append(txt.strip())
    elif isinstance(script, dict):
        subs = script.get("subs")
        if isinstance(subs, list):
            for sub in subs:
                if isinstance(sub, dict):
                    txt = sub.get("text_vi") or sub.get("text") or sub.get("vi") or ""
                else:
                    txt = sub if isinstance(sub, str) else ""
                if isinstance(txt, str) and txt.strip():
                    parts.append(txt.strip())
    return " ".join(parts).strip()


def _auto_fill_fb_tags(video_id: int, title: str | None, edit_mode: str | None,
                       page: str | None, script,
                       llm_provider: str | None = None,
                       llm_model: str | None = None) -> None:
    """After the script is finalized, auto-generate CONTENT-AWARE Facebook hashtags
    (from the real Vietnamese narration, not just the title) and store them on
    videos.facebook_tags — but ONLY when that column is still NULL/empty, so an
    owner-provided value copied from jobs.facebook_tags at create time is preserved.

    Non-fatal by contract: ANY failure (DB error, Claude timeout, empty output) is
    logged and swallowed so a tag problem can NEVER wedge or fail the pipeline. This
    is a single quick `claude -p` call, run right after the (already-completed)
    script step; _generate_fb_tags itself also never raises (title-derived fallback).
    """
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT facebook_tags FROM videos WHERE id = %s", (video_id,)
            ).fetchone()
        existing = (row or {}).get("facebook_tags") if row else None
        if existing and str(existing).strip():
            # Owner supplied tags at create time (jobs.facebook_tags -> videos) — keep them.
            return
        content = _narration_from_script(script)
        # _generate_fb_tags already normalizes/dedupes, forces the page brand tag, and
        # hard-caps at 8 (brand-protected) — so the returned list is copy-ready.
        tags = _generate_fb_tags(title=title or "", edit_mode=edit_mode,
                                 page=page, content=content,
                                 llm_provider=llm_provider, llm_model=llm_model)
        if not tags:
            print(f"[runner] auto FB-tags: no usable tags for video {video_id} (skipped)")
            return
        # Store the copy-ready NEWLINE-joined block (one hashtag per line): Facebook
        # treats a space-joined block as a single tag, so the Videos-list copy button
        # must copy a newline-separated block.
        text = "\n".join(tags)
        with get_conn() as conn:
            conn.execute(
                "UPDATE videos SET facebook_tags = %s WHERE id = %s",
                (text, video_id),
            )
        print(f"[runner] auto FB-tags for video {video_id}: {len(tags)} tags "
              f"(content-aware={'yes' if content else 'no'})")
    except Exception as e:  # noqa: BLE001 — tags must NEVER fail a job
        print(f"[runner] auto FB-tags SKIPPED for video {video_id} (non-fatal): {e}")


# ---- needs_input (Dubbed credit gate) ----------------------------------------

def _job_needs_input(job_id: int, video_id: int, payload: dict, pct: int) -> None:
    """Park a job in the 'needs_input' state instead of proceeding.

    Used by the Dubbed credit gate: when ingest produced no usable source-credit
    fields, the job pauses here so the owner can fill the credit (or explicitly
    Skip) via the dashboard, rather than silently shipping an un-credited reup.

    - jobs.status='needs_input' (NOT claimable: the worker only claims 'queued').
    - jobs.needs_input = payload (missingFields/prefill/creditDecision/videoId).
    - videos.status='needs_input' so the startup stale-recovery (which fails any
      video left 'rendering') does NOT orphan a legitimately-parked video.
    - progress_step/msg record the pause for the dashboard Workflow block.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'needs_input', needs_input = %s,"
            " progress_step = 'needs_input', progress_pct = %s,"
            " progress_msg = 'Chờ nhập thông tin nguồn' WHERE id = %s",
            (Json(payload), pct, job_id),
        )
        conn.execute("UPDATE videos SET status = 'needs_input' WHERE id = %s", (video_id,))
    set_active_job(None)  # parked: drop proc attribution (resume runs as a new job)


def _load_credit_decision(job_id: int) -> str | None:
    """Return the recorded creditDecision for a job, or None when none was ever
    recorded. Three deliberate states are distinguishable: 'provided' / 'skipped'
    (set by the resume endpoint) and 'disabled' (set by _record_credit_decision
    when the owner turned crediting OFF on a fresh Dubbed run)."""
    with get_conn() as conn:
        row = conn.execute("SELECT needs_input FROM jobs WHERE id = %s", (job_id,)).fetchone()
    ni = row["needs_input"] if row else None
    if isinstance(ni, dict):
        return ni.get("creditDecision")
    return None


def _record_credit_decision(job_id: int, decision: str) -> None:
    """Record a deliberate creditDecision on jobs.needs_input WITHOUT parking the
    job (status stays whatever it was; videos.status is untouched). This is an
    audit-only write so a no-credit Dubbed ship is traceable to a deliberate
    choice. The three distinguishable states read back by _load_credit_decision
    are: 'provided' / 'skipped' (set by the resume endpoint) and 'disabled' (set
    here when the owner turned crediting OFF). We MERGE into any existing
    needs_input object (COALESCE to '{}' when NULL) so we only set creditDecision
    and keep the kind tag, leaving other keys intact."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET needs_input = jsonb_set("
            " jsonb_set(COALESCE(needs_input, '{}'::jsonb), '{kind}', '\"credit\"'),"
            " '{creditDecision}', %s::jsonb) WHERE id = %s",
            (Json(decision), job_id),
        )


def _load_dubbed_record(video_id: int) -> dict | None:
    """Load a parked/resumed Dubbed video's saved {mode,subs,filler} record so the
    resume can RE-ASSEMBLE without re-running ingest translation / filler detection
    (both are `claude -p` calls). Returns the dict, or None if not a dubbed record."""
    with get_conn() as conn:
        row = conn.execute("SELECT script FROM videos WHERE id = %s", (video_id,)).fetchone()
    rec = row["script"] if row else None
    if isinstance(rec, dict) and rec.get("mode") == "dubbed":
        return rec
    return None


def _save_assets(video_id: int, scenes: list, visuals: dict, audio: dict, visual_kind: str = "image") -> None:
    with get_conn() as conn:
        for s in scenes:
            n = s["scene"]
            if visuals.get(n):
                conn.execute(
                    "INSERT INTO assets (video_id, kind, scene_index, path, prompt) VALUES (%s,%s,%s,%s,%s)",
                    (video_id, visual_kind, n, visuals[n], s.get("image_prompt")),
                )
            if audio.get(n):
                conn.execute(
                    "INSERT INTO assets (video_id, kind, scene_index, path) VALUES (%s,'audio',%s,%s)",
                    (video_id, n, audio[n]["audioPath"]),
                )


# NOTE: _thumb_from_cover_or_frame was removed (2026-07-22). Render/finalize no
# longer creates a sibling <video>.thumb.jpg; videos.thumb_path stays NULL and the
# chosen cover is baked into the mp4's first frame by the FFmpeg assembly step. The
# make_thumbnail import below is kept intentionally: tests monkeypatch
# runner.make_thumbnail, and generate.make_thumbnail still backs the manual cover
# feature (POST /api/videos/{id}/cover).


def _finalize_video(video_id: int, audio_path: str | None, video_path: str, duration_s: float,
                    width: int | None, height: int | None, thumb_path: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE videos SET audio_path = %s, video_path = %s, duration_s = %s,"
            " width = %s, height = %s, thumb_path = %s, status = 'ready' WHERE id = %s",
            (audio_path, video_path, duration_s, width, height, thumb_path, video_id),
        )


def _set_progress(job_id: int, step: str, pct: int, msg: str) -> None:
    """Record the current pipeline step so the dashboard's Workflow block can
    show where the job is and what it's doing."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET progress_step = %s, progress_pct = %s, progress_msg = %s WHERE id = %s",
            (step, pct, msg[:300], job_id),
        )


def _assemble_progress_cb(job_id: int, step: str):
    """A progress callback for the in-process FFmpeg assemble step.

    assemble()/assemble_footage() emit 0..100 across their scenes+concat+bgm; we
    map that onto the render band [85,99] (99→100 is left for thumbnail/finalize)
    and write it to the job row, so the FE chip shows 'Dựng video NN%' live instead
    of sitting at 85 then jumping to done."""
    LO, HI = 85, 99
    return lambda pct, msg: _set_progress(
        job_id, step, LO + round(min(100, max(0, pct)) / 100 * (HI - LO)), msg)


def _run_with_time_ramp(job_id: int, step: str, msg: str, lo: int, hi: int,
                        expected_sec: float, fn):
    """Run a BLACK-BOX blocking call `fn` while emitting a smooth, monotonic
    time-based progress ESTIMATE into the band [lo, hi].

    Used for steps that give NO real progress signal — currently script generation
    via Claude Code headless (`claude -p ... --output-format json`), which prints
    nothing until it returns. We cannot measure true progress, so this is an
    *estimate only* (honesty): an asymptotic ramp that approaches but never reaches
    `hi` until the call actually finishes, then snaps to `hi`. It will not hang
    misleadingly at 99% — it caps at ~92% of the band span until completion.

    Curve: pct = lo + span * (1 - exp(-t / tau)), tau chosen so the ramp reaches
    ~63% of the span at `expected_sec` and asymptotes toward (but never hits) `hi`.
    We additionally clamp the pre-completion value to <= lo + 0.92*span so a slow
    run doesn't sit pinned at the band top pretending it's nearly done.
    """
    span = max(0, hi - lo)
    tau = max(1.0, float(expected_sec))
    result: dict = {}
    error: dict = {}

    def _target():
        try:
            result["value"] = fn()
        except BaseException as e:  # capture; re-raised on the main thread below
            error["exc"] = e

    th = threading.Thread(target=_target, name=f"cf-ramp-{step}", daemon=True)
    _set_progress(job_id, step, lo, msg)
    th.start()
    t0 = time.time()
    last_pct = lo
    # Emit the estimate ~3x/sec while the call runs; cap pre-completion at 92% span.
    while th.is_alive():
        th.join(timeout=0.33)
        if not th.is_alive():
            break
        elapsed = time.time() - t0
        frac = 1.0 - math.exp(-elapsed / tau)          # 0 -> ~1 (never reaches 1)
        pct = lo + int(round(span * min(frac, 0.92)))  # honest cap: never pin at top
        if pct > last_pct:                              # monotonic within the band
            last_pct = pct
            _set_progress(job_id, step, pct, msg)
    if "exc" in error:
        raise error["exc"]
    _set_progress(job_id, step, hi, msg)               # snap to band top on success
    return result.get("value")


# Typical wall-clock for one Claude-headless script generation on this machine,
# used ONLY to shape the time-based estimate above (not a hard timeout — that lives
# in generate.py). Tune via env if script gen is consistently faster/slower.
SCRIPT_RAMP_EXPECTED_SEC = float(os.getenv("SCRIPT_RAMP_EXPECTED_SEC", "45"))


# --- Per-step timing instrumentation -----------------------------------------
# A single worker thread processes one job at a time, so a simple per-job dict keyed
# by job_id is sufficient (no concurrency). Each pipeline step is wrapped in the
# `_timed(job_id, step)` context manager, which (1) logs a greppable line
# `[timing] job <id> step=<name> secs=<n>` and (2) accumulates the elapsed seconds
# into _JOB_TIMINGS[job_id]. At finalize (_job_done / _job_failed) the accumulator is
# best-effort persisted to jobs.timings (JSONB) and then cleared. Purely diagnostic:
# every part is wrapped so a timing failure can NEVER affect job processing.
import contextlib  # noqa: E402  (local-style import kept beside the feature it serves)

_JOB_TIMINGS: dict[int, dict[str, float]] = {}


@contextlib.contextmanager
def _timed(job_id: int, step: str):
    """Time a pipeline step. Logs `[timing] job <id> step=<name> secs=<n.n>` and adds
    the elapsed seconds (summing if the same step runs more than once) to the per-job
    accumulator. NEVER raises on the timing path — the wrapped body's own exceptions
    propagate normally, but the elapsed time is still recorded in a finally block."""
    t0 = time.time()
    try:
        yield
    finally:
        try:
            dt = time.time() - t0
            bucket = _JOB_TIMINGS.setdefault(job_id, {})
            bucket[step] = round(bucket.get(step, 0.0) + dt, 2)
            print(f"[timing] job {job_id} step={step} secs={dt:.1f}", flush=True)
        except Exception:
            pass  # diagnostics must never break a job


def _persist_timings(job_id: int) -> None:
    """Best-effort write of the per-job timing accumulator into jobs.timings (JSONB),
    then drop it from the in-memory map. Uses its OWN connection/transaction so a
    failure here can NEVER poison the status-finalize transaction (a raised statement
    aborts the whole psycopg transaction — isolating this keeps 'done'/'failed' safe).
    Tolerates a DB that has not yet applied the `timings` column migration (seed.sql):
    the error is swallowed so a pre-migration deployment still finalizes jobs normally.
    The accumulator is always cleared (pop) so a missing column can't leak memory."""
    timings = _JOB_TIMINGS.pop(job_id, None)
    if not timings:
        return
    try:
        with get_conn() as conn:
            conn.execute("UPDATE jobs SET timings = %s WHERE id = %s", (Json(timings), job_id))
    except Exception as e:  # noqa: BLE001  (column may not exist yet, or any DB hiccup)
        print(f"[timing] job {job_id}: timings persist skipped ({e})", flush=True)


def _job_done(job_id: int) -> None:
    _CANCEL_REQUESTED.discard(job_id)
    _STOPPED_JOBS.discard(job_id)
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'done', progress_step = 'done', progress_pct = 100,"
            " progress_msg = 'Hoàn tất', finished_at = now() WHERE id = %s AND status = 'running'",
            (job_id,),
        )
    set_active_job(None)  # drop proc attribution on terminal finalize
    _persist_timings(job_id)  # separate txn — must not jeopardize the finalize above


def _job_failed(job_id: int, video_id: int | None, msg: str) -> None:
    """Finalize a job as 'failed'.

    LAST-RESORT SANITIZER: whatever reaches here is what the dashboard renders, so
    machine output (tracebacks, worker dumps) is translated into a friendly
    Vietnamese sentence by worker_errors.friendly_job_error. Messages already
    written for a human pass through untouched. The RAW text is printed first, so
    logs/api.log keeps the full record for debugging — nothing is swallowed.
    Most worker failures are already classified upstream in
    generate._run_cf_worker_once; this catches everything else (ffmpeg, DB, ...).
    """
    _CANCEL_REQUESTED.discard(job_id)
    _STOPPED_JOBS.discard(job_id)
    friendly = friendly_job_error(msg)
    if friendly != (msg or "").strip():
        print(f"[runner] job {job_id} raw error (sanitized for UI): {msg}", flush=True)
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'failed', error = %s, finished_at = now() WHERE id = %s",
            (friendly[:2000], job_id),
        )
        if video_id:
            conn.execute("UPDATE videos SET status = 'failed' WHERE id = %s", (video_id,))
    set_active_job(None)  # drop proc attribution on terminal finalize
    _persist_timings(job_id)  # separate txn — must not jeopardize the finalize above


def _job_stopped(job_id: int, video_id: int | None) -> None:
    """Finalize a job the user STOPPED: status='stopped' (distinct from 'failed' so
    the FE can offer Resume), error carries the reason, finished_at=now().

    videos.status is deliberately LEFT at 'rendering' (NOT 'failed'): a resume
    (retry of a 'stopped' job) reuses the SAME video row's persisted artifacts
    (script JSONB, etc.) to continue from the last completed step, and the resume
    endpoint re-queues with reuse_script_video_id pointing at it. Marking the video
    'failed' would not block resume, but 'rendering' keeps it semantically "still in
    progress, paused" and consistent with needs_input's parked-video handling.
    NOTE: the startup stale-recovery (_recover_stale_jobs) fails any video left
    'rendering' on restart — a stopped job's video would be swept there, which is
    acceptable (a server restart means the in-memory resume context is gone anyway;
    the row's saved script still allows a fresh retry to reuse it)."""
    _CANCEL_REQUESTED.discard(job_id)
    _STOPPED_JOBS.discard(job_id)
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'stopped', error = %s, progress_msg = %s,"
            " finished_at = now() WHERE id = %s",
            ("Dừng bởi người dùng", "Đã dừng", job_id),
        )
        # videos.status left 'rendering' on purpose (see docstring) — do not fail it.
    set_active_job(None)  # drop proc attribution on terminal finalize
    _persist_timings(job_id)  # separate txn — must not jeopardize the finalize above


# ---- pipeline ----------------------------------------------------------------

def _resolve_voice(page_name: str, voice_key: str | None) -> tuple[str | None, str | None]:
    """Map the Studio voice key to (presetVoice, refAudioPath)."""
    if not voice_key:
        return None, None
    if voice_key.startswith("clone:"):
        name = voice_key[len("clone:"):]
        # Clones are SHARED across pages (see generate.py SHARED_VOICE_DIR).
        # The legacy per-page dir (_page_voice_dir) is empty since the migration,
        # so resolve against the shared dir where upload_voice actually writes.
        return None, os.path.join(_shared_voice_dir(), name + ".wav")
    if voice_key.startswith("preset:"):
        return voice_key[len("preset:"):], None
    return voice_key, None


def _localize_images(page_name: str, job_id: int, image_results: list) -> dict:
    """Download each ComfyUI-rendered image to a local file (assemble needs paths)."""
    img_dir = os.path.join(CONTENT_OUTPUT_ROOT, page_name, "images")
    os.makedirs(img_dir, exist_ok=True)
    out: dict[int, str] = {}
    for r in image_results:
        scene = r["scene"]
        imgs = r.get("images") or []
        if not imgs:
            raise RuntimeError(f"scene {scene}: ComfyUI returned no image")
        dest = os.path.join(img_dir, f"job{job_id}_scene{scene:03d}.png")
        urllib.request.urlretrieve(imgs[0]["url"], dest)
        out[scene] = dest
    return out


# ---- post-success cleanup (TASK 3) -------------------------------------------
#
# After a job SUCCEEDS, free disk by deleting this job's intermediate artifacts,
# keeping ONLY the final video + its thumbnail and the shared reusable caches.
# Default ON; flip CLEANUP_AFTER_DONE=0/off/false/no to disable.
CLEANUP_AFTER_DONE = os.getenv("CLEANUP_AFTER_DONE", "1").strip().lower() not in ("0", "off", "false", "no")

# Dirs that are SHARED / reusable across jobs and pages — NEVER deleted by cleanup.
# (_cache: cross-job source+transcript reuse; _voices/_voice_previews: clone refs &
#  cached previews, page-independent.)
_CLEANUP_NEVER_DIRS = {"_cache", "_voices", "_voice_previews"}


def _within_root(path: str) -> bool:
    """Path-guard: True only if `path` resolves to a location strictly INSIDE
    CONTENT_OUTPUT_ROOT. Refuses anything outside (incl. the root itself), so a
    bad/spoofed path can never delete arbitrary files."""
    try:
        root = os.path.realpath(CONTENT_OUTPUT_ROOT)
        rp = os.path.realpath(path)
    except Exception:
        return False
    if rp == root:
        return False
    return os.path.commonpath([root, rp]) == root


def _safe_rm_file(path: str | None, removed: list, freed: list) -> None:
    """Best-effort delete one file, path-guarded. Records size freed. Never raises."""
    if not path:
        return
    try:
        if not os.path.isfile(path):
            return
        if not _within_root(path):
            print(f"[cleanup] REFUSED (outside root): {path}")
            return
        sz = os.path.getsize(path)
        os.remove(path)
        removed.append(path)
        freed.append(sz)
    except Exception as e:
        print(f"[cleanup] skip file {path}: {e}")


def _safe_rm_tree(path: str | None, removed: list, freed: list) -> None:
    """Best-effort delete one directory tree, path-guarded. Never raises."""
    if not path:
        return
    try:
        if not os.path.isdir(path):
            return
        if not _within_root(path):
            print(f"[cleanup] REFUSED (outside root): {path}")
            return
        # Never remove a shared/reusable dir, even if a caller passes one by mistake.
        if os.path.basename(os.path.normpath(path)) in _CLEANUP_NEVER_DIRS:
            print(f"[cleanup] REFUSED (protected dir): {path}")
            return
        for dirpath, _dirs, files in os.walk(path):
            for f in files:
                try:
                    freed.append(os.path.getsize(os.path.join(dirpath, f)))
                    removed.append(os.path.join(dirpath, f))
                except Exception:
                    pass
        shutil.rmtree(path, ignore_errors=True)
    except Exception as e:
        print(f"[cleanup] skip tree {path}: {e}")


def _cleanup_job_intermediates(job_id: int, page_name: str, video_path: str | None,
                               render_mode: str, *, visuals: dict | None = None,
                               audio: dict | None = None,
                               source_video_id: str | None = None) -> None:
    """Delete THIS job's intermediate artifacts after a successful render, keeping
    only the final video + thumbnail and the shared caches (_cache/_voices/...).

    Best-effort: every delete is wrapped and path-guarded to CONTENT_OUTPUT_ROOT, so
    a cleanup failure can NEVER fail or un-finalize the job. Logs a greppable summary.

    What gets removed (all this-job-only, all under CONTENT_OUTPUT_ROOT):
      - Per-scene clips dir <page>/clips/job<job_id>/ (footage/stickman scene clips).
      - The footage source DOWNLOAD copy <page>/clips/<vid>_src.mp4 and the extracted
        source audio <page>/source/<vid>.wav — both now redundant (the reusable copies
        live in _cache/sources + _cache/transcripts). Derived from the page clips/source
        dir + the yt-dlp video id, so we only ever touch the PAGE copy, never the cache.
      - This job's per-scene TTS wavs and SDXL images, taken from the in-memory
        `audio`/`visuals` maps (absolute paths the job just produced). Images are also
        job-prefixed (job<id>_scene*.png), so a glob backstops the map.

    Assets table: `_save_assets` rows point at the per-scene image/audio FILES we delete
    here. We delete the FILES but LEAVE the asset rows (simplest correct option): the
    Videos grid relies only on videos.video_path/thumb_path (both KEPT), never on assets,
    so leaving stale rows breaks nothing and avoids extra writes in a best-effort step.
    A re-render regenerates the (page-scoped) wavs — acceptable per the A3 de-dup note.

    NEVER touched: _cache, _voices, _voice_previews, the final video_path + thumbnail,
    or any other job/page.
    """
    if not CLEANUP_AFTER_DONE:
        print(f"[cleanup] job {job_id}: disabled (CLEANUP_AFTER_DONE off), kept all intermediates")
        return

    removed: list[str] = []
    freed: list[int] = []
    keep = set()
    if video_path:
        try:
            keep.add(os.path.realpath(video_path))
        except Exception:
            pass

    try:
        # 1) per-scene clips dir for this job (footage / stickman). image mode never
        #    creates it, so this is a no-op there.
        clips_job_dir = os.path.join(_page_clips_dir(page_name), f"job{job_id}")
        _safe_rm_tree(clips_job_dir, removed, freed)

        # 2) footage source download copy + extracted source audio (redundant vs cache).
        if source_video_id:
            _safe_rm_file(os.path.join(_page_clips_dir(page_name), f"{source_video_id}_src.mp4"),
                          removed, freed)
            _safe_rm_file(os.path.join(_page_source_dir(page_name), f"{source_video_id}.wav"),
                          removed, freed)

        # 3) per-scene TTS wavs this job produced (page-scoped scene_NNN.wav). Take the
        #    exact paths from the audio map so we never touch another job's/page's files.
        for r in (audio or {}).values():
            ap = r.get("audioPath") if isinstance(r, dict) else None
            if ap and os.path.realpath(ap) not in keep:
                _safe_rm_file(ap, removed, freed)

        # 4) per-scene SDXL images this job produced. Delete the exact paths from the
        #    visuals map (image mode only — footage/stickman visuals are the scene clips
        #    already removed in (1), which live under clips_job_dir).
        if render_mode == "image":
            for p in (visuals or {}).values():
                if p and os.path.realpath(p) not in keep:
                    _safe_rm_file(p, removed, freed)
            # Backstop: any job-prefixed image the map missed.
            img_dir = os.path.join(CONTENT_OUTPUT_ROOT, page_name, "images")
            for p in glob.glob(os.path.join(img_dir, f"job{job_id}_scene*.png")):
                if os.path.realpath(p) not in keep:
                    _safe_rm_file(p, removed, freed)
    except Exception as e:
        # Defensive: nothing above should raise (all helpers swallow), but guarantee it.
        print(f"[cleanup] job {job_id}: unexpected error, aborting cleanup: {e}")

    mb = sum(freed) / (1024 * 1024)
    print(f"[cleanup] job {job_id}: removed {len(removed)} files / {mb:.1f} MB, "
          f"kept final video + cache")


def _resolve_manual_cover(job: dict) -> str | None:
    """MAIN production path (image/footage): decide the cover to bake as the FIRST FRAME.

    MANUAL ONLY — the cover the owner explicitly generated in the Studio and ticked
    ("Sử dụng ảnh Cover"), which the FE sends as NewJobBody.coverImagePath and which is
    already persisted on the job row. No cover attached -> None (the video stays coverless
    and the FE just renders the mp4's first frame).

    AUTO-COVER REMOVED (2026-07-28, owner decision): creating a video used to auto-generate
    a vision cover here whenever the job had no manual cover but did have a source link
    (yt-dlp thumbnail -> Claude vision prompt -> SDXL -> baked VN title). That whole flow is
    gone — clicking "Tạo video" never renders a cover on its own any more. The MANUAL
    feature is untouched: POST /generate/cover (the "Tạo Cover" button) and POST
    /api/videos/{id}/cover (change the cover after render) both still work.

    Consistent with the convention: the chosen cover is BAKED into the mp4 (no sibling
    .thumb.jpg, videos.thumb_path stays NULL)."""
    return (job.get("cover_image_path") or "").strip() or None


def _resolve_clone_cover(job: dict, job_id: int) -> str | None:
    """CLONE path: reuse the SOURCE video's cover — NO heavy vision render (owner decision).

    - Manual cover set on the clone job -> use it (respect manual).
    - Else look up the SOURCE video's cover: the clone references clone_of_video_id; the
      video that produced it is videos.job_id, and that job carries the persisted
      cover_image_path. Reuse that exact file (bake_cover_first_frame fits/pads it to the
      clone's dims — accepted as-is).
    - Source has no cover (or file missing) -> None (clone stays coverless, as before).

    Cheap (a DB lookup), so it always runs. NEVER raises."""
    manual = (job.get("cover_image_path") or "").strip()
    if manual:
        return manual
    src_video_id = job.get("clone_of_video_id")
    if not src_video_id:
        return None
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT j.cover_image_path AS cover FROM videos v "
                "JOIN jobs j ON j.id = v.job_id WHERE v.id = %s",
                (src_video_id,),
            ).fetchone()
        cover = (str(row["cover"]).strip() if (row and row.get("cover")) else "")
        if cover and os.path.isfile(cover):
            _set_progress(job_id, "render", 92, "Dùng lại cover nguồn")
            print(f"[runner] clone job {job_id} reusing source video {src_video_id} cover -> {cover}")
            return cover
        print(f"[runner] clone job {job_id} source video {src_video_id} has no reusable cover -> coverless")
    except Exception as exc:  # noqa: BLE001 — never fail the clone on a cover lookup
        print(f"[runner] clone job {job_id} source-cover lookup failed (ignored): {exc}")
    return None


def _process_clone_job(job: dict) -> None:
    """ASSEMBLE-ONLY path for a CLONE job (re-render an existing finished video at a
    DIFFERENT aspect ratio). Reuses the cached content snapshot from
    _cache/renders/<clone_of_video_id>/ — NO ingest, NO Claude script, NO TTS, NO
    SDXL, NO model load. Only the FFmpeg ASSEMBLE step runs.

    The job carries:
      - clone_of_video_id : the SOURCE finished video to reuse content from.
      - aspect            : the TARGET aspect ratio for the new render.
    A NEW videos row was already created by the clone endpoint (status 'rendering');
    its id is job['_video_id'] (set by the worker dispatch). The new file keeps the
    existing " (v<id>)" suffix scheme so it never overwrites the source.
    """
    job_id = job["id"]
    video_id = job["_video_id"]
    src_video_id = job["clone_of_video_id"]
    # Attribute the clone's assemble ffmpeg to this job for immediate-kill on /stop.
    set_active_job(job_id)
    page = _load_page(job["page_id"])
    if not page:
        _job_failed(job_id, video_id, f"page {job['page_id']} not found")
        return
    page_name = page["name"]
    # Page's OWN branding handle for the final black outro card (None -> no card).
    outro_handle = _page_outro_handle(page)

    try:
        _set_progress(job_id, "render", 80, "Tải nội dung đã lưu")
        manifest = render_cache.load_manifest(src_video_id)
        if not manifest:
            raise RuntimeError(
                f"cached content for video {src_video_id} not available "
                f"(manifest missing or RENDER_CACHE off) — cannot clone")

        new_aspect = job.get("aspect") or "9:16"
        width, height = ASPECTS.get(new_aspect, ASPECTS["9:16"])
        render_mode = manifest.get("renderMode") or "image"
        title = manifest.get("title") or f"job_{job_id}"
        add_credit = bool(manifest.get("addCredit", True))
        src_audio_volume = float(manifest.get("srcAudioVolume") or 0.0)
        bgm_path = manifest.get("bgmPath")
        bgm_volume = manifest.get("bgmVolume")
        source_name = manifest.get("sourceName")
        source_link = manifest.get("sourceLink")
        source_logo = manifest.get("sourceLogo")
        source_handle = manifest.get("sourceHandle")

        # A clone must credit the SAME source as the SOURCE video (owner: "video clone
        # thì source link copy giống source video"). The manifest's sourceName/Link are
        # a snapshot taken when the source was FIRST rendered; if the owner edited the
        # source video's credit fields afterwards, the manifest is stale. So prefer the
        # SOURCE video row's CURRENT source_name/source_link and only fall back to the
        # manifest when the DB row is null. (sourceLogo/sourceHandle have no DB columns,
        # so they stay manifest-only.)
        src_row = None
        try:
            with get_conn() as conn:
                src_row = conn.execute(
                    "SELECT source_name, source_link FROM videos WHERE id = %s",
                    (src_video_id,),
                ).fetchone()
        except Exception as src_exc:
            # Non-fatal: keep the manifest values if the lookup fails.
            print(f"[runner] clone job {job_id} source-row lookup failed (using manifest): {src_exc}")
        if src_row is not None:
            if src_row["source_name"] is not None:
                source_name = src_row["source_name"]
            if src_row["source_link"] is not None:
                source_link = src_row["source_link"]

        m_scenes = manifest["scenes"]

        # Persist the reused script onto the new video row (so the Videos grid shows
        # scene count / title exactly like the source). Caption text is the script.
        script = [{"scene": s["scene"], "narration": s.get("narration")} for s in m_scenes]
        _save_script(video_id, title, script, source_name, source_link)

        set_model_busy(True)  # serialize against on-demand model calls, like a normal job
        _set_progress(job_id, "render", 85, "Dựng lại (đổi tỷ lệ) 0%")
        set_ff_progress_cb(_assemble_progress_cb(job_id, "render"))
        try:
            if render_mode in ("footage", "stickman"):
                # Cached visual = the per-scene CUT CLIP; the assembler re-frames it
                # to the new width/height (centered scale+crop). Karaoke captions are
                # rebuilt from the cached VO via whisper inside assemble_footage.
                # Caption fallback chain (consistent across footage + image modes):
                # display -> caption -> narration. TTS is not re-run on the clone path.
                f_scenes = [
                    FootageScene(scene=s["scene"], clipPath=s["visualPath"], audioPath=s["audioPath"],
                                 caption=s.get("display") or s.get("caption") or s.get("narration"),
                                 durationS=s.get("durationS"))
                    for s in m_scenes
                ]
                with _timed(job_id, "render"):
                    res = assemble_footage(
                        _with_src_audio_volume(
                            FootageAssembleRequest(page=page_name, title=title, scenes=f_scenes,
                                                   width=width, height=height, videoId=video_id,
                                                   bgmPath=bgm_path,
                                                   bgmVolume=(bgm_volume if bgm_volume is not None else 0.12),
                                                   sourceName=source_name, sourceLink=source_link,
                                                   sourceLogo=source_logo, sourceHandle=source_handle, outroHandle=outro_handle,
                                                   addCredit=add_credit,
                                                   # Best-effort engine tag so an OmniVoice clone also skips
                                                   # auto target-pace (cached audio is already as-drawn).
                                                   engine=(job.get("voice_clone_model") or "").strip().lower() or None),
                            src_audio_volume,
                        )
                    )
                visual_kind = "clip"
            else:
                # image mode: cached visual = the SDXL still; the assembler scales/crops
                # it to the new aspect (acceptable per owner: "chỉ đổi tỷ lệ").
                # Caption fallback chain (consistent across footage + image modes):
                # display -> caption -> narration. TTS is not re-run on the clone path.
                a_scenes = [
                    AssembleScene(scene=s["scene"], imagePath=s["visualPath"], audioPath=s["audioPath"],
                                  caption=s.get("display") or s.get("caption") or s.get("narration"),
                                  durationS=s.get("durationS"))
                    for s in m_scenes
                ]
                with _timed(job_id, "render"):
                    res = assemble(
                        AssembleRequest(page=page_name, title=title, scenes=a_scenes,
                                        width=width, height=height, videoId=video_id,
                                        bgmPath=bgm_path,
                                        bgmVolume=(bgm_volume if bgm_volume is not None else 0.18),
                                        sourceName=source_name, sourceLink=source_link,
                                        sourceLogo=source_logo, sourceHandle=source_handle, outroHandle=outro_handle,
                                        addCredit=add_credit)
                    )
                visual_kind = "image"
        finally:
            set_ff_progress_cb(None)
            set_model_busy(False)

        # Finalize the NEW video via the SHARED finalize path.
        # No sibling .thumb.jpg is created and thumb_path stays NULL: the chosen cover
        # (if any) is baked into the mp4 as its first frame by the FFmpeg assembly step.
        # The FE renders the <video> first frame when thumbUrl is null; POST
        # /api/videos/{id}/cover still sets a display-only poster after render.
        first_audio = m_scenes[0]["audioPath"] if m_scenes else None
        # Resolve the cover for a CLONE: manual if attached, else REUSE the SOURCE video's
        # cover (NO heavy vision render — owner decision), else coverless. Then bake it into
        # the mp4 as its FIRST FRAME (no-op when there's no cover). Must run BEFORE finalize
        # so videos.video_path points at the baked file. In-place, no full re-encode.
        cover_to_bake = _resolve_clone_cover(job, job_id)
        bake_cover_first_frame(res["videoPath"], cover_to_bake,
                               res.get("width") or width, res.get("height") or height)
        _finalize_video(video_id, first_audio, res["videoPath"], res["durationS"],
                        res.get("width"), res.get("height"), None)

        # Snapshot the CLONE's OWN content so a clone can itself be re-cloned. The
        # cached visuals/audio are the SAME files reused from the source render cache;
        # store_render copies them into _cache/renders/<new video id>/. Best-effort.
        try:
            visuals = {s["scene"]: s["visualPath"] for s in m_scenes}
            audio = {s["scene"]: {"audioPath": s["audioPath"], "durationS": s.get("durationS")}
                     for s in m_scenes}
            render_cache.store_render(
                video_id, page=page_name, render_mode=render_mode, visual_kind=visual_kind,
                title=title, aspect=new_aspect, width=res.get("width") or width,
                height=res.get("height") or height, source_name=source_name, source_link=source_link,
                source_logo=source_logo, source_handle=source_handle, add_credit=add_credit,
                bgm_path=bgm_path, bgm_volume=bgm_volume, src_audio_volume=src_audio_volume,
                scenes=script, visuals=visuals, audio=audio,
            )
        except Exception as snap_exc:
            print(f"[runner] clone job {job_id} render-cache snapshot error (ignored): {snap_exc}")

        # Publish — OPT-IN, via the SHARED publish core (same as a normal job).
        if job.get("publish") and not API_UPLOAD_ENABLED:
            # API upload kill-switch is ON: skip auto-publish, never fail the job.
            _set_progress(job_id, "publish", 95, "Hoàn tất (đăng qua API đang tạm tắt)")
            print(f"[runner] clone job {job_id} auto-publish SKIPPED (API_UPLOAD_ENABLED is off)")
        elif job.get("publish"):
            _set_progress(job_id, "publish", 95, "Đăng / hoàn tất")
            try:
                # Per-platform target: when publish_platform is set, publish ONLY to
                # that platform's connected channel of the job's OWN page; otherwise
                # account_ids=None => AUTO mode (ALL connected channels). One shared
                # publish core, no divergence.
                target_platform = (job.get("publish_platform") or "").strip().lower() or None
                account_ids = _resolve_publish_account_ids(job)
                if target_platform is not None and account_ids is None:
                    print(f"[runner] clone job {job_id} auto-publish skipped: page "
                          f"{job['page_id']} has no '{target_platform}' channel")
                else:
                    publish_video_core(video_id, account_ids=account_ids)
            except Exception as pub_exc:
                detail = getattr(pub_exc, "detail", None) or str(pub_exc) or pub_exc.__class__.__name__
                print(f"[runner] clone job {job_id} produced (auto-publish skipped/failed): {detail}")
        else:
            _set_progress(job_id, "publish", 95, "Hoàn tất (chưa đăng)")

        _job_done(job_id)
        print(f"[runner] clone job {job_id} done -> {res['videoPath']} "
              f"({res['durationS']}s, {len(m_scenes)} scenes, {new_aspect}, reused video {src_video_id})")

        # Cleanup: a clone reuses the SHARED render cache files (under _cache, never
        # touched by cleanup) and produced no NEW per-scene intermediates of its own,
        # so there is nothing job-specific to delete. Call the shared cleanup with
        # empty maps anyway (it is a no-op here and must never crash a clone).
        try:
            _cleanup_job_intermediates(job_id, page_name, res.get("videoPath"), render_mode,
                                       visuals={}, audio={}, source_video_id=None)
        except Exception as clean_exc:
            print(f"[runner] clone job {job_id} cleanup error (ignored): {clean_exc}")

    except Exception as exc:
        if isinstance(exc, JobStopped) or _was_stopped(job_id):
            _job_stopped(job_id, video_id)
            print(f"[runner] clone job {job_id} STOPPED by user")
        else:
            detail = getattr(exc, "detail", None) or str(exc) or exc.__class__.__name__
            traceback.print_exc()
            _job_failed(job_id, video_id, str(detail))
            print(f"[runner] clone job {job_id} FAILED: {detail}")


# --- Per-scene TIME-BUDGET allocation + VO capping (duration-bug v2 fix) ------
# The footage/translate output was = Σ(full VO durations) + slate, uncoupled from
# the source, so a natural-paced VO grew past the source. The mechanical fix (owner
# directive) FLIPS the order: allocate a per-scene TIME BUDGET from the source
# duration BEFORE TTS, then TRIM each scene's VO audio down to its cap (with a short
# fade-out) and drive the scene clip by min(vo_dur, cap). This bounds the output to
# the source by construction. NOTE: this trims the VO AUDIO only — the narration TEXT
# in the saved script stays full (owner rule: never truncate the text, never speed up
# the voice). Audio capping with a fade-out is the user-directed mechanism for v2.

_BUDGET_SLATE_SEC = 3.0    # fixed credit slate appended after the scenes (assemble_footage)
_BUDGET_SAFETY = 0.05      # reserve 5% of the source as headroom below the slate-adjusted budget

# Section D — mode-gate the source-duration budget machinery. The per-scene time
# budget (_allocate_scene_budgets) and the post-assembly duration guard
# (_enforce_duration_guard) force the output STRICTLY shorter than the source. That
# invariant only holds for SOURCE-TRACKING modes (summary/recap), whose length is
# meant to follow the source. ORIGINAL-LENGTH modes (commentary/educational) may
# legitimately run longer than the source, and dubbed has no TTS to cap — applying
# the cap/guard there would truncate real content or FALSE-FAIL the job.
_SOURCE_TRACKING_MODES = {"summary", "recap"}


def _mode_tracks_source(edit_mode: str | None) -> bool:
    """True only for modes whose output length is meant to track the source length
    (summary, recap) — those get the per-scene time budget + the duration guard.
    False for commentary/educational (original-length, may exceed source), dubbed
    (no TTS), and any unknown/None mode. The runner default edit_mode is "commentary"
    (see _process_job), so the default is False = no cap / no guard = the SAFE default
    (it can never false-fail a legitimately-longer original-length video)."""
    return (edit_mode or "").lower() in _SOURCE_TRACKING_MODES


def _allocate_scene_budgets(scenes: list, source_duration: float) -> None:
    """Attach a hard float `time_cap` (seconds) to each scene dict, distributing a
    source-derived total budget across scenes by their narration word count.

        total_budget = source_duration - slate - source_duration * 5%
        cap_i        = total_budget * (words_i / total_words)

    Mutates the scene dicts in place. SKIPS entirely (no `time_cap` set, behavior
    unchanged) when there is nothing to bound against: source_duration <= 0 (no
    source — image/stickman/story_voiceover topic-only paths) or total_words == 0.
    """
    if not scenes or source_duration is None or source_duration <= 0:
        return
    word_counts = [len((s.get("narration") or "").split()) for s in scenes]
    total_words = sum(word_counts)
    if total_words <= 0:
        return
    total_budget = source_duration - _BUDGET_SLATE_SEC - source_duration * _BUDGET_SAFETY
    if total_budget <= 0:
        # Source shorter than the slate+safety reserve — nothing left to allocate.
        # Leave caps unset so the post-assembly guard remains the only backstop.
        return
    for s, w in zip(scenes, word_counts):
        # Each scene gets a hard floor of 0.05s so a zero-word scene never gets a
        # 0s (or negative) cap that would later trip the very-short-cap guard.
        s["time_cap"] = max(0.05, total_budget * (w / total_words))


def _cap_voiceover(src_audio: str, cap: float, dest: str) -> float:
    """Trim a TTS voiceover WAV to a hard `cap` (seconds), writing a NEW file at
    `dest`. Returns the probed duration of the trimmed file. Uses the same FFmpeg
    helper/binary as the rest of the pipeline (_run_ffmpeg → [FFMPEG_BIN, -y, ...]).

    The cut is shaped so the scene-transition concat (a hard-cut stream-copy in
    generate.py) does NOT butt mid-word speech against the next scene's speech:

      * A LONGER fade-out so a severed syllable trails off rather than chopping:
            fade_dur = clamp(max(0.5, cap * 0.15)) into the audible window.
      * A trailing SILENCE pad so the scene ends on silence and the next scene
        starts clean across the hard-cut join:
            pad = min(0.15, cap * 0.1)

    Crucially this is LENGTH-PRESERVING: the audible content + fade occupies
    [0, cap-pad] and silence fills [cap-pad, cap], so the total stays = `cap`
    (≤ budget). This keeps the duration invariant intact so the post-assembly
    guard `_enforce_duration_guard` never fires on account of this step.

    Filter chain (single -af):
        afade=t=out:st={fade_st}:d={fade_dur}   # ramp speech to zero by cap-pad
        ,atrim=end={cap-pad}                     # cut speech at cap-pad
        ,apad                                    # pad with silence (indefinitely)
    plus a top-level `-t {cap}` to hard-stop the padded stream at exactly `cap`.

    Edge-case math (gate at the call site guarantees cap > 0.05):
        cap=10.0 → pad=0.15, fade [8.35,9.85], silence [9.85,10.0]
        cap=1.0  → pad=0.10, fade [0.40,0.90], silence [0.90,1.00]
        cap=0.2  → pad=0.02, fade [0.00,0.18], silence [0.18,0.20]
    fade_st is clamped to >= 0 and fade_dur is clamped to fit [0, cap-pad], so the
    fade never starts negative and never extends past where the speech is trimmed."""
    pad = min(0.15, cap * 0.1)
    audible_end = max(0.0, cap - pad)
    # Longer fade than before (was min(0.30, cap*0.5)); floor 0.5s, else 15% of cap.
    fade_dur = max(0.5, cap * 0.15)
    # Clamp the fade so it fits entirely inside the audible window [0, audible_end].
    fade_dur = min(fade_dur, audible_end)
    fade_st = max(0.0, audible_end - fade_dur)
    af = (
        f"afade=t=out:st={fade_st:.3f}:d={fade_dur:.3f}"
        f",atrim=end={audible_end:.3f}"
        f",apad"
    )
    _run_ffmpeg(
        ["-i", src_audio, "-t", f"{cap:.3f}",
         "-af", af,
         "-c:a", "pcm_s16le", dest],
        step="cap voiceover",
    )
    return _probe_duration(dest)


def _scene_clip_duration(scene: dict, audio_result: dict) -> float | None:
    """Duration that should drive a scene's visual clip = min(vo_dur, time_cap).

    `audio_result` is this scene's TTS result; after the 2b capping step its
    `durationS` already reflects the (possibly trimmed) audio file. We still take an
    explicit min against the scene's `time_cap` so the clip can never run LONGER than
    the budget even if the trimmed file probes a hair over the cap (rounding). When
    the scene has no cap (image/stickman/no-source paths, or a VO under budget) this
    returns the raw VO duration unchanged. Returns None only if the VO duration is
    unknown (preserves the prior `r.get("durationS")` None passthrough)."""
    try:
        vo_dur = float(audio_result.get("durationS")) if audio_result.get("durationS") is not None else None
    except (TypeError, ValueError):
        vo_dur = None
    cap = scene.get("time_cap")
    if vo_dur is None:
        return None
    if cap is None or cap <= 0.05:
        return vo_dur
    return min(vo_dur, cap)


def _enforce_duration_guard(job_id, res: dict, src_dur) -> None:
    """HARD GUARD (owner-requested backstop): a source-derived output video MUST be
    STRICTLY shorter than the source. The AUTO word-ceiling fix makes this hold by
    construction, but TTS pace wobble / a stale reused script could still push the
    output over. We never fix that by speeding up the voice or trimming audio, so the
    only honest recovery is to FAIL the job and ask for a shorter script.

    Self-skips cleanly when `src_dur` is falsy / <= 0 (story_voiceover or any path
    with no source to compare against) — the invariant only applies to source-derived
    paths. Shared by the footage and stickman assemble call sites so the check is
    identical on every source-derived path.
    """
    try:
        out_dur = float(res.get("durationS")) if res.get("durationS") is not None else None
    except (TypeError, ValueError):
        out_dur = None
    if out_dur is None:
        # res didn't carry a duration → re-probe the assembled output ourselves.
        out_dur = _probe_duration(res.get("videoPath") or "")
    if not src_dur or src_dur <= 0:
        # No source duration to compare against (can't be honest about the ratio).
        print(f"[runner] job {job_id} duration guard SKIPPED: source duration unknown")
    elif out_dur and out_dur >= src_dur:
        print(f"[runner] job {job_id} duration guard FAILED: "
              f"output={out_dur:.2f}s >= source={src_dur:.2f}s")
        raise RuntimeError(
            f"Video đầu ra ({out_dur:.1f}s) dài hơn hoặc bằng video gốc "
            f"({src_dur:.1f}s) — vi phạm ràng buộc độ dài. Job đã dừng. "
            f"Hãy giảm nội dung kịch bản."
        )


# Zero-tolerance epsilon for the post-TTS early duration gate. The owner's rule is
# STRICT: if the output would exceed the source by ANY amount (even a fraction of a
# millisecond) the job must fail. So the threshold is exactly 0 — `total >= src_dur`
# fails, with no slack. We catch it HERE, the EARLIEST point the REAL (measured, not
# estimated) output seconds are known — right after TTS, before whisper + per-scene
# encoding + concat — so an over-length job fails fast instead of burning the whole
# assembly stage only to die at the post-assembly _enforce_duration_guard (the final
# backstop, which still stays in place).
_EARLY_DURATION_EPSILON_SEC = 0.0


def _enforce_post_tts_duration(job_id, audio: dict, scenes: list, src_dur,
                               add_credit: bool, has_slate_content: bool,
                               outro_handle: str | None = None) -> None:
    """EARLY zero-tolerance duration gate (owner-requested). Sums the ACTUAL per-scene
    VO seconds (already probed by the TTS step → `audio[scene]["durationS"]`) plus the
    fixed credit-slate seconds that assemble_footage will append, and FAILS the job
    immediately if that total >= the source duration (strict, epsilon == 0).

    This is the SAME invariant the post-assembly _enforce_duration_guard enforces, but
    moved to the earliest point the real output length is known (right after TTS, before
    whisper/encode/concat). The caller MUST gate this exactly like the post-assembly
    guard (only for source-tracking modes) so we never false-fail an original-length
    mode. Never speeds up or truncates audio — the only honest recovery is to fail and
    ask for a shorter script.

    Self-skips when `src_dur` is falsy/<=0 (no source to compare against) — mirroring the
    post-assembly guard's skip-and-log behavior. The slate is added only when it will
    actually be appended (add_credit AND there is logo/handle/source_name to show),
    matching _make_assemble_progress's slate_w condition so the estimate is faithful.
    """
    if not src_dur or src_dur <= 0:
        print(f"[runner] job {job_id} early duration gate SKIPPED: source duration unknown")
        return
    total_vo = 0.0
    for s in scenes:
        r = audio.get(s["scene"]) or {}
        try:
            d = float(r.get("durationS")) if r.get("durationS") is not None else 0.0
        except (TypeError, ValueError):
            d = 0.0
        total_vo += max(0.0, d)
    slate = _BUDGET_SLATE_SEC if (add_credit and has_slate_content) else 0.0
    # The black outro handle card is appended too (independent of add_credit), so it must be
    # in this sum or the gate would under-estimate the output and let a video exceed the source.
    outro_card = CF_OUTRO_CARD_SEC if outro_handle else 0.0
    total = total_vo + slate + outro_card
    # Zero tolerance: any overshoot (total >= src_dur, no epsilon) fails immediately.
    if total >= src_dur - _EARLY_DURATION_EPSILON_SEC:
        print(f"[runner] job {job_id} EARLY duration gate FAILED (post-TTS, before "
              f"whisper/assembly): VO={total_vo:.3f}s + slate={slate:.1f}s "
              f"+ outro={outro_card:.1f}s = {total:.3f}s >= source={src_dur:.3f}s")
        raise RuntimeError(
            f"Video đầu ra ({total:.1f}s) sẽ dài hơn hoặc bằng video gốc "
            f"({src_dur:.1f}s) — dừng ngay, hãy giảm nội dung kịch bản."
        )


def _process_job(job: dict) -> None:
    job_id = job["id"]
    # Publish this as the active job so every subprocess a pipeline step spawns
    # (download/whisper/claude/TTS/ffmpeg/blender) is attributed to it and can be
    # tree-killed immediately by POST /stop. Cleared in the finally at the bottom.
    set_active_job(job_id)
    page = _load_page(job["page_id"])
    if not page:
        _job_failed(job_id, None, f"page {job['page_id']} not found")
        set_active_job(None)
        return
    page_name = page["name"]
    # Page's OWN branding handle for the final black outro card (None -> no card).
    outro_handle = _page_outro_handle(page)
    # jobs.render_mode is now the AUTHORITATIVE source for the render mode (the
    # Studio writes it at job creation; pages.architecture_type/config were dropped
    # in the 2026-06-25 schema redesign). Prefer it when present.
    render_mode = job.get("render_mode")
    if not render_mode:
        # Legacy fallback for rows created before jobs.render_mode existed: derive
        # from render_model. passthrough-trim → footage (keep source video); an SDXL
        # checkpoint → image (AI stills); stickman-* → stickman.
        rm = job.get("render_model")
        if rm == "passthrough-trim":
            render_mode = "footage"
        elif rm and rm.startswith("stickman"):
            render_mode = "stickman"
        elif rm in RENDER_CHECKPOINTS:
            render_mode = "image"
        else:
            # No render_model either: key the default off whether there is a SOURCE
            # LINK to ingest (input_type=="link") rather than any page label.
            render_mode = "footage" if job["input_type"] == "link" else "image"
    payload = job["input_payload"]
    width, height = ASPECTS.get(job.get("aspect") or "9:16", ASPECTS["9:16"])
    add_credit = job.get("add_credit", True)
    # Original/source-audio volume in the final mix (0 = off, voiceover only).
    # Threaded into the footage assembler (media-engineer adds the assemble param).
    src_audio_volume = job.get("src_audio_volume") or 0.0
    # Whole source is ingested (capped only by the 2h safety ceiling).
    window = INGEST_MAX_SEC or 0
    # Target OUTPUT length. target_sec IS NULL  => AUTO mode: the output length
    # follows the SOURCE (no fixed condense; summary trims only redundancy).
    # target_sec is a number => existing fixed-condense behavior. We must detect
    # None EXPLICITLY: `or` would also coerce a legitimate 0 to the default.
    target_sec = job.get("target_sec")
    auto = target_sec is None
    target = DEFAULT_DURATION if (target_sec is None or target_sec <= 0) else int(target_sec)
    # Resolve the editing mode ONCE, before the scene-source branch dispatch below.
    # It must be bound on EVERY path (ingest / script-reuse / topic-only) because the
    # Section D duration-budget gate (_mode_tracks_source) is referenced unconditionally
    # after the branches. Default "commentary" => non-source-tracking => no duration
    # guard, which is the safe default (won't false-fail). NULL only happens for a
    # non-Studio caller that omitted it (which by definition isn't a summary/recap job).
    edit_mode = job["edit_mode"] or "commentary"
    # Section F — DUBBED mode. The Studio sends edit_mode=="dubbed" as the trigger:
    # keep the ORIGINAL audio+video, trim filler, burn translated VN subtitles, NO
    # TTS / SDXL / stickman. is_dubbed is computed HERE (before any prompt builder) so
    # a dubbed job is intercepted and NEVER reaches generate_script_footage/_transform
    # — those raise 422 on an unknown editMode against EDIT_MODE_GUIDE, and "dubbed" is
    # deliberately not in that dict (it produces no narration scene array).
    is_dubbed = (edit_mode or "").lower() == "dubbed"
    # translate_full — FULL re-upload: keep the ENTIRE source video, MUTE its audio,
    # narrate a VN F5-TTS translation of a full faithful translation, burn VN subtitles,
    # and COVER the source's own burned-in karaoke. Closest to dubbed mechanics but it
    # DOES run TTS (+ a caption-cover pre-pass) and mutes the original audio. Like
    # is_dubbed, it is detected HERE (before any prompt builder) so it is intercepted and
    # never reaches generate_script_footage/_transform (those 422 on an unknown editMode
    # against EDIT_MODE_GUIDE, and "translate_full" is deliberately not in that dict —
    # it produces per-line translated subs, not a narration scene array).
    is_translate_full = (edit_mode or "").lower() == "translate_full"
    # PER-JOB LLM CHOICE for TEXT script-gen (the provider gate, generate/llm_gate.py).
    # Read exactly like voice_clone_model above: normalized, empty -> None, and None means
    # "claude-cli with its default model" — i.e. the ONLY behavior that existed before the
    # gate. Threaded into every generate_script_* / dubbed-translate / filler / FB-tag call
    # below. The two VISION calls deliberately ignore it (they need Claude Code's Read-tool
    # agentic behavior, which no chat-completions provider has).
    llm_provider = (job.get("llm_provider") or "").strip().lower() or None
    llm_model = (job.get("llm_model") or "").strip() or None
    # The concrete (provider, model) this job's script-gen will use. Correct as an
    # "actually served" record today BECAUSE llm_gate never falls back across providers:
    # whatever resolves here is what ran, or the job failed. (If a fallback ladder is ever
    # added, this must switch to reading the served provider back out of the
    # generate_script_* response, which already returns llmProvider/llmModel.)
    _job_llm = llm_gate.resolve(llm_provider, llm_model)
    # Set to _job_llm ONLY on a branch that really made an LLM call; stays None on every
    # reuse/resume branch so _save_script does not claim a provider that never ran.
    _llm_used: tuple[str, str | None] | None = None
    if llm_provider:
        print(f"[runner] job {job_id} LLM provider override: {_job_llm[0]}/{_job_llm[1]}")
    # RETRY SCRIPT REUSE (A3). On a retry the SAME job_id may already have a prior
    # video row whose videos.script was saved by a run that got through script-gen
    # before failing later (TTS/cut/assemble). Detect that BEFORE creating the fresh
    # video row and reuse its scenes so the retry skips `claude -p` entirely. This is
    # the AUTOMATIC counterpart to PART B (explicit reuse_script_video_id) — the
    # explicit user-chosen reuse below always wins (guarded with `not reuse_src_id`).
    _retry_scenes = None
    _prior_video = _video_for_job(job_id)
    if _prior_video:
        _ps, _pt, _psn, _psl, _pfbt = _load_reusable_script(_prior_video["id"])
        if _ps:
            _retry_scenes = _ps
            print(f"[runner] job {job_id} retry: reusing script from prior video "
                  f"{_prior_video['id']} ({len(_retry_scenes)} scenes)")
    video_id = _create_video(job_id, page["id"], job.get("facebook_tags"))

    # Dubbed requires a SOURCE link (it dubs an existing video). A dubbed job with no
    # link has nothing to dub — fail fast and clearly rather than NameError later.
    if is_dubbed and job["input_type"] != "link":
        _job_failed(job_id, video_id,
                    "Chế độ Dubbed cần một video nguồn (link) để lồng phụ đề — "
                    "job này không có link nguồn.")
        print(f"[runner] job {job_id} DUBBED FAILED: no source link to dub")
        return
    # translate_full also requires a SOURCE link (it re-uploads an existing video).
    if is_translate_full and job["input_type"] != "link":
        _job_failed(job_id, video_id,
                    "Chế độ Dịch nguyên video (translate_full) cần một video nguồn "
                    "(link) — job này không có link nguồn.")
        print(f"[runner] job {job_id} TRANSLATE_FULL FAILED: no source link")
        return

    # PART B — SCRIPT REUSE. When the job carries reuse_script_video_id, the runner
    # BYPASSES script generation entirely (NO `claude -p`) and loads that source
    # video's saved videos.script JSONB to feed TTS. We pre-load it here (before the
    # model-busy block) so a missing/empty source script fails fast with a clear
    # Vietnamese error before any model work begins. For FOOTAGE mode we STILL ingest
    # (the cut step needs the downloaded source mp4 + each scene's sourceStart/
    # sourceEnd cut timecodes); for image/stickman we skip ingest too, since nothing
    # on those paths needs the source transcript once the script is reused.
    reuse_src_id = job.get("reuse_script_video_id")
    reuse_scenes = reuse_title = reuse_source_name = reuse_source_link = None
    reuse_facebook_tags = None
    # DUBBED RESUME: a Dubbed job resumed from the needs_input pause carries
    # reuse_script_video_id pointing at its OWN parked video, whose videos.script is a
    # {mode:'dubbed', subs, filler} DICT (not a scene list). _load_reusable_script
    # rejects non-list scripts, so we must detect the dubbed record FIRST and load it
    # via _load_dubbed_record — this lets the resume re-assemble from the cached subs/
    # filler WITHOUT re-running _translate_subs_to_vi / _detect_filler_ranges (both
    # `claude -p`). The credit fields entered (or the deliberate skip) live on the
    # video row + jobs.needs_input and are read back below.
    reuse_dubbed = _load_dubbed_record(reuse_src_id) if (reuse_src_id and is_dubbed) else None
    # translate_full saves a {mode:'translate_full', subs, filler} DICT (not a scene
    # list), which _load_reusable_script rejects. A translate_full RESUME (from the
    # credit-gate pause) therefore ignores reuse_src_id here and re-ingests +
    # re-translates on the fresh path below (an extra claude -p, but correct — a full
    # 1:1 translation is deterministic enough to reproduce). Guard with
    # `not is_translate_full` so we never mis-route its dict through the scene-reuse path.
    if reuse_src_id and not reuse_dubbed and not is_translate_full:
        (reuse_scenes, reuse_title, reuse_source_name, reuse_source_link,
         reuse_facebook_tags) = _load_reusable_script(reuse_src_id)
        if not reuse_scenes:
            _job_failed(job_id, video_id, "Video nguồn không có kịch bản để dùng lại")
            print(f"[runner] job {job_id} reuse FAILED: source video {reuse_src_id} "
                  f"has no script to reuse")
            return
        print(f"[runner] job {job_id} REUSING script from video {reuse_src_id} — "
              f"script-gen BYPASSED (no claude -p), {len(reuse_scenes)} scenes")
        # Reused narration is IDENTICAL to the source video's, so its hashtags are
        # still valid — copy them onto the new video row now (before _auto_fill_fb_tags
        # runs) so its NULL-check skips the redundant `claude -p` hashtag call. Owner-
        # supplied job.facebook_tags (already applied by _create_video above) always
        # wins and is never overwritten here.
        if reuse_facebook_tags and str(reuse_facebook_tags).strip() and not job.get("facebook_tags"):
            with get_conn() as conn:
                conn.execute(
                    "UPDATE videos SET facebook_tags = %s WHERE id = %s AND "
                    "(facebook_tags IS NULL OR facebook_tags = '')",
                    (reuse_facebook_tags, video_id),
                )
            print(f"[runner] job {job_id} reused facebook_tags from video {reuse_src_id} "
                  f"— auto FB-tags call skipped")
    elif reuse_dubbed:
        print(f"[runner] job {job_id} DUBBED RESUME from video {reuse_src_id} — "
              f"translate/filler BYPASSED (no claude -p), "
              f"{len(reuse_dubbed.get('subs') or [])} subs, "
              f"{len(reuse_dubbed.get('filler') or [])} filler ranges")

    set_model_busy(True)  # block on-demand model calls (preview/clone/prewarm) while the job runs
    try:
        # 1) source material -> scenes
        source_name = source_link = source_logo = source_handle = title = None
        ing = None
        # De-dup the source download (footage mode): a footage job needs BOTH a
        # 16 kHz wav (for whisper) and a height-capped start-anchored mp4 (for
        # clip-cutting) of the SAME first-N-seconds of the SAME URL. Historically
        # ingest pulled bestaudio AND download_source_video pulled the video again.
        # Now, for footage+link we download the video ONCE here (preserving the
        # download_worker 403 start-anchored-range workaround) and have ingest
        # extract its audio from that local mp4 instead of a second network pull.
        # The same `src_video` is reused by the cut stage below.
        src_video = None
        # Footage AND dubbed both need the cached 720p source mp4 (footage cuts scene
        # clips from it; dubbed cuts+concats keep-ranges from it). Gate both modes here.
        if (render_mode == "footage" or is_dubbed or is_translate_full) and job["input_type"] == "link":
            _set_progress(job_id, "ingest", 5, "Tải video nguồn")
            set_progress_cb(lambda pct, msg: _set_progress(job_id, "ingest", 5 + round(pct / 100 * 8), msg))
            try:
                src_video = download_source_video(
                    SourceVideoRequest(link=payload, page=page_name, window=window or None)
                )
            finally:
                set_progress_cb(None)
        # Ingest (download + whisper transcript) gate. On the SCRIPT-REUSE path we
        # only need ingest for FOOTAGE mode (the cut step uses the downloaded source
        # mp4 + each reused scene's sourceStart/sourceEnd); image/stickman reuse needs
        # no source transcript, so we skip the whole ingest+script branch and go
        # straight to TTS with the loaded scenes. A fresh (non-reuse) job ingests
        # whenever there is a source LINK to ingest (H2: keyed off input_type, not the
        # page's "translate" label — a translate page can also run no-source jobs).
        _is_link_job = job["input_type"] == "link"
        # Dubbed normally ingests (it needs the whisper transcript segments for sub
        # timing + the source mp4); footage ingests even on reuse (cut needs the mp4).
        # DUBBED RESUME (reuse_dubbed) is the exception: the subs + filler are already
        # cached on the parked video, so we SKIP the whisper ingest (and thus the
        # translate/filler `claude -p` calls); the source mp4 was already re-fetched
        # by the download_source_video block above (gated on is_dubbed).
        _do_ingest = _is_link_job and not reuse_dubbed and (
            not reuse_scenes or render_mode == "footage" or is_dubbed or is_translate_full)
        if _do_ingest:
            _set_progress(job_id, "ingest", 13 if src_video else 5, "Bóc lời nguồn")
            # Forward the worker's live download/transcribe progress (0-100) into
            # the ingest band [5,25] so the FE percent actually moves in real time.
            set_progress_cb(lambda pct, msg: _set_progress(job_id, "ingest", 5 + round(pct / 100 * 20), msg))
            try:
                with _timed(job_id, "ingest"):
                    ing = generate_ingest(IngestRequest(
                        link=payload, page=page_name, clipSec=window or None,
                        localMedia=(src_video["videoPath"] if src_video else None),
                    ))
            finally:
                set_progress_cb(None)
            source_name = ing.get("uploader")
            source_link = ing.get("sourceUrl")
            source_logo = ing.get("logoPath")
            source_handle = ing.get("handle")
            title = ing.get("title")
            _check_cancel(job_id)  # abort before expensive script-gen if stop was requested
            # edit_mode already resolved above (hoisted before the branch dispatch).
            # Script-gen window = actual spoken-content span, NOT the ingest ceiling.
            # INGEST_MAX_SEC (window) is a download/transcription cap and must NOT be
            # used as the script window — a video shorter than that cap would produce
            # batch sub-windows wider than the content, making every batch beyond the
            # first receive an empty transcript and cause LLM hallucination.
            # Prefer the whisper-probed file duration (durationS) over max segment end:
            # whisper's last segment can end 1-2s past the real video end; using it
            # as the window lets the LLM emit sourceEnd values that overshoot the video
            # → a cut clip with no video stream → assemble crash. durationS is safer.
            _file_dur = float(ing.get("durationS") or 0)
            _max_seg_end = max((float(sg["end"]) for sg in ing.get("segments", [])), default=0)
            # Backstop for the "doubled source duration" bug: if the file/container
            # duration is wildly larger than the transcript's last segment end, the
            # file length is bogus (a ~2x inflated container). Trust the transcript
            # span — that's the real spoken-content extent — so the script window
            # (and "0-Ns" prompt) reflects the actual video, not the doubled length.
            if _file_dur and _max_seg_end and _file_dur > _max_seg_end * 1.3:
                print(f"[runner] job {job_id} file_dur {_file_dur:.1f}s >> "
                      f"max_seg_end {_max_seg_end:.1f}s — clamping window to transcript span")
                win = _max_seg_end
            else:
                win = _file_dur or _max_seg_end
            # translate_full BUG-1 fix: the source's trailing outro/credit is usually a
            # VISUAL block AFTER the spoken content ends (little/no transcript). If the
            # script window is the FILE duration, the footage tiling extends the final
            # scene over that outro and the reup shows the ORIGINAL channel's credit at
            # the end. Cap the window to the last spoken segment's end so no scene window
            # can reach the post-speech outro. (Spoken outro junk is separately excluded
            # by the footage prompt's CUT-junk rule.)
            if is_translate_full and _max_seg_end > 0:
                if win > _max_seg_end:
                    print(f"[runner] job {job_id} TRANSLATE_FULL: capping window "
                          f"{win:.1f}s -> content end {_max_seg_end:.1f}s (exclude source outro)")
                win = min(win, _max_seg_end)
            # In AUTO mode the output length follows the SOURCE. Use the ingested
            # source duration (durationS, from the probe/whisper step) to drive the
            # script length; if it's somehow missing, fall back to the transcript
            # span (segment ends) and finally to the fixed default, logging it.
            # Use the (possibly clamped) `win`, not the raw _file_dur — otherwise
            # the doubled-duration backstop above would be bypassed for AUTO mode.
            src_dur = win or _file_dur or 0.0
            if auto:
                if src_dur <= 0:
                    src_dur = float(DEFAULT_DURATION)
                    print(f"[runner] job {job_id} AUTO: source duration missing, "
                          f"falling back to {src_dur:.0f}s")
                # Script length tracks the source. For retell modes the natural
                # length is ~source; for `summary` the prompt itself drives the
                # trim (keep most, cut redundancy) so durationSec is only a hint.
                gen_duration = max(1, round(src_dur))
                print(f"[runner] job {job_id} AUTO mode: duration follows source "
                      f"= {gen_duration}s (edit_mode={edit_mode})")
            else:
                gen_duration = target
                print(f"[runner] job {job_id} FIXED mode: condense to {gen_duration}s")
            # NO-SPEECH visual-explain fallback. When ingest returns ZERO whisper
            # transcript segments (a source that has audio but no speech — e.g. an
            # animation with only music/SFX), there is nothing to rewrite or translate.
            # Instead we sample frames, show them to Claude (vision), and generate a
            # Vietnamese explainer that TILES the whole source, then run the NORMAL
            # footage assembly (Ken Burns + karaoke captions + bgm + F5/VieNeu TTS).
            # This MUST precede the is_dubbed / is_translate_full / render_mode=="footage"
            # branches below — all of them consume ing["segments"] and would 422 (footage:
            # "Provide source 'segments'") or produce empty output. Skipped when the job
            # already carries a reusable/retry script (that saved script wins over a fresh,
            # expensive vision pass).
            _no_speech = not ing.get("segments")
            _will_reuse_script = bool(reuse_scenes) or bool(_retry_scenes and not reuse_src_id)
            if _no_speech and not _will_reuse_script:
                # Both the frame sampling and the downstream footage cut need the source
                # mp4. It is normally downloaded up-front (footage/dubbed/translate_full
                # link jobs); re-fetch defensively if a different render_mode skipped that.
                if src_video is None:
                    _set_progress(job_id, "footage", 20, "Tải video nguồn")
                    src_video = download_source_video(
                        SourceVideoRequest(link=payload, page=page_name, window=window or None))
                _set_progress(job_id, "script", 26, "Xem hình ảnh video nguồn")
                # Per-job frames dir under the shared media cache; removed after gen.
                frames_dir = os.path.join(CONTENT_OUTPUT_ROOT, "_cache", "frames", f"job{job_id}")
                frames = frames_util.sample_frames(src_video["videoPath"], frames_dir)
                # Metadata: prefer what ingest emitted; OLD cached transcripts (incl. job
                # 232's) lack the description/tags keys, so fall back to a yt-dlp metadata
                # fetch (best-effort, never raises, never fabricated).
                _ve_desc = ing.get("description")
                _ve_tags = ing.get("tags")
                if not _ve_desc:
                    _ve_meta = frames_util.fetch_source_metadata(payload)
                    _ve_desc = _ve_meta.get("description") or ""
                    if not _ve_tags:
                        _ve_tags = _ve_meta.get("tags") or []
                # FORCE the footage assembly path for the rest of this job (TTS narration +
                # real-clip cut + karaoke), regardless of the original render_mode/edit_mode
                # or the dubbed/translate_full flags. Clearing is_dubbed/is_translate_full
                # ensures neither the no-TTS dubbed path nor the translate_full caption-cover
                # path is taken. edit_mode -> "commentary": visual-explain is explanatory /
                # original-length, so the source-tracking duration guards (_mode_tracks_source)
                # correctly self-skip instead of false-failing.
                render_mode = "footage"
                is_dubbed = False
                is_translate_full = False
                edit_mode = "commentary"
                try:
                    with _timed(job_id, "script"):
                        scenes = _run_with_time_ramp(
                            job_id, "script", "Viết kịch bản (từ hình ảnh)", 26, 40,
                            SCRIPT_RAMP_EXPECTED_SEC,
                            lambda: generate_script_visual_explain(
                                frames,
                                title=title or "",
                                description=_ve_desc or "",
                                tags=_ve_tags or [],
                                sourceDurationSec=src_dur,
                                targetDurationSec=gen_duration,
                                jobId=job_id,
                            )["scenes"],
                        )
                    # VISION path: this script came from Claude Code headless with the
                    # Read tool, NOT from the job's chosen text provider — that choice is
                    # deliberately not honored here (see llm_gate's docstring). Record
                    # what truly ran: the claude-cli default pair.
                    _llm_used = llm_gate.resolve(None, None)
                finally:
                    # Frames are only needed for the vision call; drop them whether it
                    # succeeded or failed (a 422 thin-script failure still cleans up).
                    shutil.rmtree(frames_dir, ignore_errors=True)
                # source_name/source_link were set from ingest above and flow into the
                # footage credit slate; owner-owned credit fields stay TODO_ASK_USER.
                print(f"[runner] job {job_id} NO-SPEECH source → visual-explain fallback "
                      f"({len(scenes)} scenes from {len(frames)} frames)")
            # DUBBED (Section F): no narration script. Translate the source segments
            # into faithful VN subtitles and detect filler ranges (both claude -p),
            # then assemble below from the cached source mp4 — NO TTS / SDXL / stickman.
            # We set `scenes = []` (dubbed has no narration scene array); the TTS/
            # script-gen steps are wrapped in `if not is_dubbed:` and the dubbed
            # assemble branch runs in the visuals section.
            elif is_dubbed:
                _set_progress(job_id, "script", 30, "Dịch phụ đề tiếng Việt")
                dubbed_subs = _run_with_time_ramp(
                    job_id, "script", "Dịch phụ đề tiếng Việt", 30, 38, SCRIPT_RAMP_EXPECTED_SEC,
                    lambda: _translate_subs_to_vi(ing["segments"],
                                                  llm_provider=llm_provider,
                                                  llm_model=llm_model)
                )
                _set_progress(job_id, "script", 38, "Phát hiện đoạn cần cắt")
                dubbed_filler = _detect_filler_ranges(ing["segments"], src_dur=src_dur,
                                                      llm_provider=llm_provider,
                                                      llm_model=llm_model)
                _llm_used = _job_llm
                # Persist a minimal, mode-specific record (subs + filler) as the script
                # so the row is inspectable; this does NOT feed reuse for other modes.
                scenes = []
                print(f"[runner] job {job_id} DUBBED: {len(dubbed_subs)} subtitles, "
                      f"{len(dubbed_filler)} filler ranges (script-gen / TTS BYPASSED)")

                # ---- DUBBED CREDIT GATE (security MEDIUM fix) -------------------
                # A Dubbed reup MUST credit its source. The credit slate in
                # _finish_video is silently SKIPPED unless at least one of
                # (logo / handle / source_name) is truthy. When ingest returned
                # NONE of those, shipping would silently omit attribution. Instead
                # of silently skipping, PAUSE the job (needs_input) so the owner
                # fills the credit OR explicitly Skips (a recorded, deliberate
                # choice — never a silent gap). Borrowed-account rule: the credit
                # belongs to the project OWNER, so we ASK rather than infer.
                #
                # "Missing" rule (documented): the slate would be skipped, i.e.
                # NONE of (source_name, source_handle, source_logo) is present.
                # source_link is tracked separately (it is the attribution URL the
                # owner may also want to set) and reported in missingFields when
                # empty, but the gating condition is the slate-skip condition above.
                #
                # RE-PAUSE PREVENTION (note): this gate lives inside the
                # `if _do_ingest:` / `if is_dubbed:` ingest block, which is entered
                # ONLY on a FRESH dubbed run. A RESUMED dubbed job carries
                # reuse_script_video_id -> _load_dubbed_record() is truthy ->
                # _do_ingest is False -> it takes the `elif reuse_dubbed:` branch
                # below and NEVER re-enters here. So the resume cannot loop; the
                # ingest-bypass on resume (not a creditDecision check) is what
                # prevents a re-pause. On the fresh path creditDecision is always
                # unset, so gating only on the slate-skip condition is sufficient.
                if add_credit:
                    _has_slate_credit = bool(source_logo or source_handle or source_name)
                    if not _has_slate_credit:
                        # Save the dubbed record FIRST so the resume can re-assemble
                        # from the cached subs/filler WITHOUT re-translating.
                        _dub_title = (job.get("title") or "").strip() or title or f"job_{job_id}"
                        _save_script(video_id, _dub_title,
                                     {"mode": "dubbed", "subs": dubbed_subs, "filler": dubbed_filler},
                                     source_name, source_link, llm_used=_llm_used)
                        _missing = [name for present, name in (
                            (source_name, "sourceName"),
                            (source_handle, "handle"),
                            (source_logo, "logo"),
                            (source_link, "sourceLink"),
                        ) if not present]
                        _job_needs_input(job_id, video_id, {
                            "kind": "credit",
                            "missingFields": _missing,
                            "prefill": {
                                "sourceName": source_name,
                                "sourceLink": source_link,
                                "handle": source_handle,
                                "logo": source_logo,
                            },
                            "creditDecision": None,
                            "videoId": video_id,
                        }, pct=28)
                        print(f"[runner] job {job_id} DUBBED PAUSED (needs_input): no "
                              f"source credit after ingest; missing={_missing}")
                        return
                else:
                    # Crediting was deliberately turned OFF for this Dubbed job.
                    # The video ships with no slate, but record the choice so the
                    # no-credit ship is auditable as a deliberate 'disabled' (distinct
                    # from 'provided'/'skipped'). This is a non-pausing audit write —
                    # the job continues normally. Safe here because this block only
                    # runs on the FRESH dubbed path (resume bypasses the whole ingest
                    # block per the note above), so it cannot clobber a later
                    # 'provided'/'skipped' decision set on resume.
                    _record_credit_decision(job_id, "disabled")
                    print(f"[runner] job {job_id} DUBBED: crediting disabled by owner; "
                          f"recorded creditDecision='disabled' (no pause)")
            # translate_full (full localization). Owner-chosen approach A: ride the FOOTAGE
            # prompt path (natural narration + sourceStart/End, junk removed) — NOT a 1:1
            # literal translation. Scene-gen is identical to footage; the translate_full
            # TREATMENT (mute source, EasyOCR caption-cover on the CUT video, karaoke over
            # the cover band, Ken Burns zoom) is applied in assemble_translate_full below.
            # The dubbed-style credit gate still applies (a full reupload must credit source).
            elif is_translate_full:
                with _timed(job_id, "script"):
                    scenes = _run_with_time_ramp(
                        job_id, "script", "Viết kịch bản (dịch nguyên video)", 25, 40,
                        SCRIPT_RAMP_EXPECTED_SEC,
                        lambda: generate_script_footage(
                            TransformFootageRequest(
                                segments=[TimedSegment(start=sg["start"], end=sg["end"], text=sg["text"])
                                          for sg in ing["segments"]],
                                editMode=edit_mode, title=title, durationSec=gen_duration, windowSec=win,
                                auto=auto,
                                skipScriptCache=bool(job.get("bypass_script_cache")),
                                llmProvider=llm_provider, llmModel=llm_model,
                            )
                        )["scenes"]
                    )
                _llm_used = _job_llm
                print(f"[runner] job {job_id} TRANSLATE_FULL: {len(scenes)} footage scenes "
                      f"(full-localization prompt; junk excluded)")

                # ---- CREDIT GATE (same policy as dubbed; a full reupload MUST credit its
                #      source). Pause for owner input when the slate would be silently
                #      skipped (none of logo/handle/source_name present).
                if add_credit:
                    _has_slate_credit = bool(source_logo or source_handle or source_name)
                    if not _has_slate_credit:
                        _tf_title = (job.get("title") or "").strip() or title or f"job_{job_id}"
                        _save_script(video_id, _tf_title, scenes, source_name, source_link,
                                     llm_used=_llm_used)
                        _missing = [name for present, name in (
                            (source_name, "sourceName"),
                            (source_handle, "handle"),
                            (source_logo, "logo"),
                            (source_link, "sourceLink"),
                        ) if not present]
                        _job_needs_input(job_id, video_id, {
                            "kind": "credit",
                            "missingFields": _missing,
                            "prefill": {
                                "sourceName": source_name,
                                "sourceLink": source_link,
                                "handle": source_handle,
                                "logo": source_logo,
                            },
                            "creditDecision": None,
                            "videoId": video_id,
                        }, pct=28)
                        print(f"[runner] job {job_id} TRANSLATE_FULL PAUSED (needs_input): "
                              f"no source credit after ingest; missing={_missing}")
                        return
                else:
                    _record_credit_decision(job_id, "disabled")
                    print(f"[runner] job {job_id} TRANSLATE_FULL: crediting disabled by "
                          f"owner; recorded creditDecision='disabled' (no pause)")
            # SCRIPT REUSE (footage): ingest ran (cut needs the source + cut
            # timecodes), but script generation is BYPASSED — use the loaded scenes
            # straight from the source video. No `claude -p` on this path.
            elif reuse_scenes:
                scenes = reuse_scenes
                _set_progress(job_id, "script", 40, "Dùng kịch bản đã lưu")
                print(f"[runner] job {job_id} footage reuse: using {len(scenes)} saved "
                      f"scenes from video {reuse_src_id} (script-gen BYPASSED)")
            # A3 RETRY SCRIPT REUSE (ingest path): a retry of this same job_id whose
            # prior video already saved a script (failed AFTER script-gen) reuses it
            # and skips `claude -p`. Ingest STILL ran above (footage cuts need the
            # source mp4 + each scene's cut timecodes), only the LLM script-gen is
            # bypassed. Explicit user reuse (reuse_src_id) takes precedence and was
            # handled by the `elif reuse_scenes` branch above, so guard with
            # `not reuse_src_id` to be explicit.
            elif _retry_scenes and not reuse_src_id:
                scenes = _retry_scenes
                _set_progress(job_id, "script", 40, f"Dùng lại kịch bản ({len(scenes)} cảnh)")
                print(f"[runner] job {job_id} script-gen SKIPPED (retry reuse)")
            # Script gen (Claude headless) is a BLACK BOX — no progress signal. Wrap
            # it in a time-based ESTIMATE ramp over the script band [25,40] so the
            # bar moves instead of freezing at 25 (see _run_with_time_ramp).
            elif render_mode == "footage":
                with _timed(job_id, "script"):
                    scenes = _run_with_time_ramp(
                        job_id, "script", "Viết kịch bản", 25, 40, SCRIPT_RAMP_EXPECTED_SEC,
                        lambda: generate_script_footage(
                            TransformFootageRequest(
                                segments=[TimedSegment(start=sg["start"], end=sg["end"], text=sg["text"])
                                          for sg in ing["segments"]],
                                editMode=edit_mode, title=title, durationSec=gen_duration, windowSec=win,
                                auto=auto,
                                skipScriptCache=bool(job.get("bypass_script_cache")),
                                llmProvider=llm_provider, llmModel=llm_model,
                            )
                        )["scenes"]
                    )
                _llm_used = _job_llm
            else:
                with _timed(job_id, "script"):
                    scenes = _run_with_time_ramp(
                        job_id, "script", "Viết kịch bản", 25, 40, SCRIPT_RAMP_EXPECTED_SEC,
                        lambda: generate_script_transform(
                            TransformRequest(
                                transcript=ing["transcript"], editMode=edit_mode, title=title,
                                durationSec=gen_duration, sourceLang=ing.get("language"), auto=auto,
                                llmProvider=llm_provider, llmModel=llm_model,
                            )
                        )["scenes"]
                    )
                _llm_used = _job_llm
        elif reuse_dubbed:
            # DUBBED RESUME (from the needs_input credit pause): ingest was SKIPPED
            # (subs + filler are cached). Re-hydrate them and the credit fields the
            # owner entered (or the deliberate skip). The credit values live on the
            # video row (source_name/source_link, written by the resume endpoint) and
            # in jobs.needs_input.prefill (handle/logo). No `claude -p` on this path.
            dubbed_subs = reuse_dubbed.get("subs") or []
            dubbed_filler = reuse_dubbed.get("filler") or []
            scenes = []
            with get_conn() as conn:
                _vrow = conn.execute(
                    "SELECT source_name, source_link FROM videos WHERE id = %s",
                    (reuse_src_id,)).fetchone()
                _jrow = conn.execute(
                    "SELECT needs_input FROM jobs WHERE id = %s", (job_id,)).fetchone()
            source_name = (_vrow or {}).get("source_name")
            source_link = (_vrow or {}).get("source_link")
            _ni = (_jrow or {}).get("needs_input") if _jrow else None
            _prefill = _ni.get("prefill") if isinstance(_ni, dict) else None
            if isinstance(_prefill, dict):
                source_handle = _prefill.get("handle")
                source_logo = _prefill.get("logo")
            title = (job.get("title") or "").strip() or None
            _set_progress(job_id, "script", 40, "Dùng phụ đề đã lưu")
            print(f"[runner] job {job_id} DUBBED RESUME: {len(dubbed_subs)} subs, "
                  f"{len(dubbed_filler)} filler (ingest/translate/filler BYPASSED); "
                  f"creditDecision={_load_credit_decision(job_id)}")
        elif reuse_scenes:
            # SCRIPT REUSE (image/stickman, or any non-ingest path): no ingest, no
            # script-gen. Load the saved scenes straight into TTS. source_name/link
            # are carried from the source video so the credit slate stays consistent.
            scenes = reuse_scenes
            source_name = reuse_source_name
            source_link = reuse_source_link
            title = reuse_title
            _set_progress(job_id, "script", 40, "Dùng kịch bản đã lưu")
            print(f"[runner] job {job_id} image/stickman reuse: using {len(scenes)} saved "
                  f"scenes from video {reuse_src_id} (ingest + script-gen BYPASSED)")
        else:
            # No source video: footage needs one, so fall back to image; stickman
            # makes its own visuals, so keep it.
            if render_mode == "footage":
                render_mode = "image"
            # No source video: there is nothing for AUTO to follow, so a
            # topic-only script uses the default length (logged when AUTO).
            if auto:
                print(f"[runner] job {job_id} AUTO mode but no source (topic-only); "
                      f"using default {target}s")
            # A3 RETRY SCRIPT REUSE: if a prior video for this job already carried a
            # saved script (a previous run failed AFTER script-gen), reuse it and skip
            # `claude -p` entirely. Explicit user reuse (reuse_src_id) always wins, so
            # this only fires on the fresh path with no explicit reuse selected.
            if _retry_scenes and not reuse_src_id:
                scenes = _retry_scenes
                _set_progress(job_id, "script", 40, f"Dùng lại kịch bản ({len(scenes)} cảnh)")
                print(f"[runner] job {job_id} script-gen SKIPPED (retry reuse)")
            else:
                # Black-box script gen — same time-based estimate ramp over [25,40].
                scenes = _run_with_time_ramp(
                    job_id, "script", "Viết kịch bản", 25, 40, SCRIPT_RAMP_EXPECTED_SEC,
                    lambda: generate_script(ScriptRequest(
                        topic=payload, durationSec=target,
                        llmProvider=llm_provider, llmModel=llm_model))["scenes"]
                )
                _llm_used = _job_llm
            title = payload[:80]

        # Dubbed legitimately has NO narration scenes (scenes == []); every other
        # mode must have produced scenes by now.
        if not scenes and not is_dubbed:
            raise RuntimeError("script stage produced no scenes")
        _check_cancel(job_id)  # abort before TTS if stop was requested
        # OUTPUT title: the user-supplied job title (set in Studio at create time)
        # takes precedence; otherwise fall back to the source title, then job_<id>.
        # NOTE: the source `title` above is still used as the script-gen HINT
        # (generate_script_*); only the finished-video/output title is overridden.
        job_title = (job.get("title") or "").strip() or title or f"job_{job_id}"

        if is_dubbed:
            # Dubbed: no narration script and no TTS. Persist a minimal record (the
            # subs + filler) under videos.script so the row is inspectable, and set an
            # empty audio map so the shared tail (which reads `audio`) is satisfied.
            # Steps 1b/2/2b (budget/TTS/cap) are skipped entirely.
            # _llm_used is None on a DUBBED RESUME (subs/filler came from the cached
            # record, no claude -p ran) — that leaves the original run's recorded
            # provider intact instead of re-asserting it.
            _save_script(video_id, job_title,
                         {"mode": "dubbed", "subs": dubbed_subs, "filler": dubbed_filler},
                         source_name, source_link, llm_used=_llm_used)
            audio = {}
        else:
            # FIXED OUTRO CTA — appended to the LAST scene's narration before the script is
            # persisted, so TTS speaks it, the karaoke shows it, and a script REUSE of this
            # video already carries it (append_outro is idempotent, so a reuse/retry cannot
            # stack it twice). Its seconds were already reserved out of the word budget (see
            # generate._auto_word_ceiling), so the duration gates below stay honest.
            #
            # translate_full is EXCLUDED: it is a strict 1:1 per-source-line translation whose
            # scenes are pinned to source timecodes, so appending narration to the last line
            # would desync it from the footage it must sit over. (Dubbed can't reach here — it
            # takes the branch above and runs no TTS at all.)
            if not is_translate_full and append_outro(scenes):
                print(f"[runner] job {job_id} appended fixed outro CTA to scene "
                      f"{scenes[-1].get('scene')}")
            # translate_full also lands here: it now rides the footage prompt, so its
            # script is a normal scene ARRAY (viewable in the /script preview like footage).
            # _llm_used is None on every reuse branch (explicit script reuse, retry
            # reuse) — no LLM produced these scenes, so nothing is recorded.
            _save_script(video_id, job_title, scenes, source_name, source_link,
                         llm_used=_llm_used)

        # AUTO FB-TAGS. Now that the script is finalized and saved on the video row,
        # generate content-aware Facebook hashtags from the real Vietnamese narration
        # and store them on videos.facebook_tags — ONLY if the owner did not already
        # supply tags at create time (jobs.facebook_tags -> videos, copied in
        # _create_video). Non-fatal: _auto_fill_fb_tags swallows every error, so a tag
        # failure/slowness can never wedge or fail the pipeline. Runs after the script
        # step, before render — a single quick `claude -p`.
        _auto_fill_fb_tags(
            video_id, job_title, edit_mode, page_name,
            {"mode": "dubbed", "subs": dubbed_subs} if is_dubbed else scenes,
            llm_provider=llm_provider, llm_model=llm_model,
        )

        # Steps 1b (time-budget), 2 (TTS), 2b (VO cap) are NARRATION-only. Dubbed has
        # no narration scenes and no TTS, so skip them entirely; `audio` is already
        # set to {} above for dubbed.
        if not is_dubbed:
            # 1b) NO per-scene VO time-budget / hard-trim (bug3 root-cause fix).
            # The previous mechanical fix (duration-bug v2) allocated a per-scene
            # `time_cap` from the source and TRIMMED each scene's VO down to it with a
            # fade — which, on the job-14 over-long script, chopped ~78% off every
            # sentence (faded mid-sentence). The owner directive flips the strategy:
            # bound the SCRIPT at generation (the source-derived word ceiling, enforced
            # in generate_script_footage — it FAILS an over-long script instead of
            # cramming/trimming), and let the VO play at its NATURAL FULL length here.
            # We therefore NO LONGER call _allocate_scene_budgets, so no scene carries a
            # `time_cap`, and _scene_clip_duration returns the full VO duration. The
            # honest backstop is _enforce_duration_guard after assembly: if the output
            # still exceeds the source, the job FAILS (we never trim to hide it). With
            # the word ceiling enforced, that guard should rarely fire.
            _set_progress(job_id, "cut", 40, f"Chuẩn bị {len(scenes)} cảnh")

            # 2) voiceover (Vietnamese) — forward per-scene TTS progress into the
            # voice band [55,70] so the chip percent moves in real time.
            voice, ref = _resolve_voice(page_name, job["voice"])
            # The Studio "Model lồng tiếng" (voice_clone_model) picks the TTS engine.
            # F5-TTS is the project default: vieneu → VieNeu; everything else (unset/f5-tts)
            # → F5. When no explicit engine is set, generate_tts derives it from the clone's name.
            vcm = (job.get("voice_clone_model") or "").strip().lower() or None

            # TTS is wrapped in a closure so the FOOTAGE path can run it CONCURRENTLY
            # with the source-clip cuts (A1): _do_cut only reads each scene's
            # sourceStart/sourceEnd/scene — it does NOT need `audio` (audio is consumed
            # only at assemble time). For every other render_mode TTS runs SERIALLY
            # here, exactly as before. set_progress_cb must be registered BEFORE the
            # work starts; the cut path advances the footage band [72,84] via
            # _set_progress directly (never set_progress_cb), so the two writers never
            # touch the same global and their progress bands don't overlap.
            def _run_tts():
                _set_progress(job_id, "voice", 55, f"Lồng tiếng {len(scenes)} cảnh")
                set_progress_cb(lambda pct, msg: _set_progress(job_id, "voice", 55 + round(pct / 100 * 15), msg))
                try:
                    with _timed(job_id, "tts"):
                        tts = generate_tts(
                            TtsRequest(
                                scenes=[TtsScene(scene=s["scene"], narration=s["narration"]) for s in scenes],
                                voice=voice, refAudio=ref, engine=vcm, page=page_name,
                                # Force-fresh TTS when this job opted to bypass the cache READ
                                # (reuse-script path). .get() tolerates older job dicts lacking
                                # the key (falsy → normal cached behavior).
                                bypassTtsCache=bool(job.get("bypass_tts_cache")),
                            )
                        )
                finally:
                    set_progress_cb(None)
                return {r["scene"]: r for r in tts["results"]}

            if render_mode == "footage" or is_translate_full:
                # Defer: the footage branch below starts TTS in a thread and runs the
                # cuts concurrently, then joins both before assemble. `audio` is filled
                # there. We DON'T run TTS here so the GPU TTS work and the CPU/IO ffmpeg
                # cuts overlap end-to-end. translate_full now also uses footage-cut (owner
                # approach A), so it takes the same concurrent cut+TTS path.
                _tts_thunk = _run_tts
                audio = None
            else:
                _tts_thunk = None
                audio = _run_tts()

            # 2b) NO VO hard-trim (bug3 root-cause fix — _cap_voiceover removed from the
            # assemble path). The VO plays at its NATURAL full length: no `time_cap` is
            # set (step 1b no longer allocates one), so there is nothing to trim and
            # nothing fades mid-sentence. Downstream, _scene_clip_duration(s, r) returns
            # the full VO duration (cap is None) and the scene clip is cut to the full
            # VO. The output length is bounded by the GENERATION-side word ceiling, with
            # _enforce_duration_guard as the honest post-assembly backstop. (The
            # _cap_voiceover function is kept DEFINED but UNUSED for reference/history.)

        # 3) visuals + assemble — dubbed (cut+subs), footage (real clips), or image (SDXL stills)
        if is_dubbed:
            # DUBBED: cut filler + concat keeping original audio, 9:16 fit, burn VN subs,
            # credit slate. Uses the cached 720p source mp4 (src_video). No TTS/SDXL.
            if src_video is None:
                # Defensive: dubbed needs the cached source mp4; re-fetch if missing.
                _set_progress(job_id, "footage", 71, "Tải video nguồn")
                src_video = download_source_video(
                    SourceVideoRequest(link=payload, page=page_name, window=window or None))
            _set_progress(job_id, "render", 85, "Dựng video (dubbed) 0%")
            set_ff_progress_cb(_assemble_progress_cb(job_id, "render"))
            try:
                with _timed(job_id, "render"):
                    res = assemble_dubbed(
                        DubbedAssembleRequest(
                            page=page_name, title=job_title,
                            srcVideoPath=src_video["videoPath"],
                            subs=dubbed_subs, filler=dubbed_filler,
                            width=width, height=height, fps=30, videoId=video_id,
                            sourceName=source_name, sourceLink=source_link,
                            sourceLogo=source_logo, sourceHandle=source_handle, addCredit=add_credit,
                        )
                    )
            finally:
                set_ff_progress_cb(None)
            # Section F.5: NO _enforce_duration_guard for dubbed (output = source -
            # filler + slate, structurally <= source; the guard's >= test would
            # false-fail an empty-cut dubbed). assemble_dubbed already logs src vs out.
            visuals = {}
            visual_kind = "clip"
        elif render_mode == "footage" or is_translate_full:
            # Reuse the source video already downloaded up-front for the audio
            # de-dup; only fall back to a fetch if it's somehow missing (defensive).
            if src_video is not None:
                src = src_video
                _set_progress(job_id, "footage", 71, "Dùng footage đã tải")
            else:
                _set_progress(job_id, "footage", 71, "Tải footage nguồn")
                src = download_source_video(SourceVideoRequest(link=payload, page=page_name, window=window or None))
            clips_dir = os.path.join(_page_clips_dir(page_name), f"job{job_id}")
            os.makedirs(clips_dir, exist_ok=True)
            _set_progress(job_id, "footage", 72, f"Cắt {len(scenes)} cảnh")
            # A1 — PARALLEL TTS + footage cut. The cut step reads ONLY each scene's
            # sourceStart/sourceEnd/scene (NOT `audio`), so it can run CONCURRENTLY with
            # TTS; audio is consumed only later, at assemble time. We therefore:
            #   1) start TTS in a background thread (_tts_thunk, deferred above),
            #   2) run the per-scene cuts IMMEDIATELY using scene data only,
            #   3) join both, then validate audio for every scene before assemble.
            # Each scene's cut is an independent ffmpeg read from the SAME read-only
            # source into a DISTINCT dest → safe to run concurrently (bounded by
            # _cut_workers()). cut_items hold only (scene, dest); audio is matched in
            # after the join. Progress stays monotonic via a thread-safe completion
            # counter (less smooth than serial, but never goes backwards). The TTS
            # thread writes the voice band [55,70] via set_progress_cb; the cuts write
            # the footage band [72,84] via _set_progress directly — disjoint bands, no
            # shared global, so both can update concurrently without conflict.
            cut_items = [(s, os.path.join(clips_dir, f"scene{s['scene']:03d}.mp4")) for s in scenes]

            n_cuts = len(cut_items)
            _cut_done = [0]
            _cut_done_lock = threading.Lock()

            # Probe the ACTUAL source duration once: defense-in-depth against a scene
            # whose sourceEnd overshoots the video end (e.g. a stale reused script, or
            # the LLM still drifting past the window). Clamping here guarantees no cut
            # ever seeks past the last decodable frame → no audio-only/video-less clip.
            _src_dur = _probe_duration(src["videoPath"])

            def _do_cut(item):
                s, dest = item
                raw_start = max(0.0, float(s.get("sourceStart", 0)))
                raw_end = float(s.get("sourceEnd", raw_start + 5))
                if _src_dur > 0:
                    # Clamp the START inside the real source FIRST. A hallucinated start
                    # past EOF must be pulled back so the cut never seeks past the last
                    # decodable frame (otherwise the raw_end = max(raw_start+0.5, ...)
                    # below would re-inflate the end past the source → empty/streamless clip).
                    raw_start = min(raw_start, _src_dur - 0.5)
                    raw_start = max(0.0, raw_start)
                    raw_end = min(raw_end, _src_dur - 0.05)
                raw_end = max(raw_start + 0.5, raw_end)  # keep a minimum clip length
                _cut_clip(src["videoPath"], raw_start, raw_end, dest)
                # footage step occupies band [70,85]; advance per completed scene
                # (monotonic counter, order-independent), capped at 84.
                with _cut_done_lock:
                    _cut_done[0] += 1
                    done = _cut_done[0]
                pct = 72 + round(done / max(1, n_cuts) * (84 - 72))
                _set_progress(job_id, "footage", min(pct, 84), f"Cắt cảnh {done}/{n_cuts}")
                return dest

            # Run TTS (background thread) and the source-clip cuts concurrently.
            # _tts_thunk was set above only for footage mode; it must be present here.
            import concurrent.futures
            if _tts_thunk is None:
                # Defensive: footage must always defer TTS to this point (set above).
                raise RuntimeError("footage: TTS thunk missing (deferral not set up)")
            _tts_ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            _tts_fut = _tts_ex.submit(_tts_thunk)
            try:
                # Cuts run in the foreground (they manage their own bounded pool). If a
                # cut raises, we still drain the TTS future below so its thread and the
                # progress-cb it registered are cleaned up — then re-raise the cut error.
                _run_cuts(cut_items, _do_cut, _cut_workers())
            except BaseException as _cut_err:
                # A cut failed: TTS may still be running. We cannot force-cancel the TTS
                # subprocess cleanly, so wait for it to finish (draining any exception so
                # it isn't lost), shut the pool down, then re-raise the ORIGINAL cut error.
                try:
                    _tts_fut.result()
                except BaseException:
                    pass
                _tts_ex.shutdown(wait=True)
                raise _cut_err
            # Cuts finished — now block on TTS. .result() re-raises any TTS exception
            # (which then propagates to the job-failed handler, exactly as a serial TTS
            # error would have).
            try:
                audio = _tts_fut.result()
            finally:
                _tts_ex.shutdown(wait=True)

            # Both stages done. Validate that audio was produced for EVERY scene before
            # assemble (fail fast and clearly if any is missing), and abort if a stop
            # was requested while the two ran.
            for s in scenes:
                if not audio.get(s["scene"]):
                    raise RuntimeError(f"scene {s['scene']}: no audio produced")
            _check_cancel(job_id)

            # EARLY zero-tolerance duration gate (owner-requested). TTS has just produced
            # every scene's VO with REAL probed durations; this is the EARLIEST point the
            # actual output length is known. Fail NOW if VO + credit slate would meet or
            # exceed the source — before whisper/encode/concat — instead of waiting for the
            # post-assembly _enforce_duration_guard (kept below as the final backstop).
            # Gated EXACTLY like that guard (source-tracking modes only) so original-length
            # modes are never false-failed. has_slate_content mirrors assemble_footage's
            # slate condition (logo/handle/source_name present).
            if _mode_tracks_source(edit_mode):
                _enforce_post_tts_duration(
                    job_id, audio, scenes, _src_dur, add_credit,
                    bool(source_logo or source_handle or source_name),
                    outro_handle=outro_handle,
                )

            visuals, f_scenes = {}, []
            for s, dest in cut_items:
                r = audio[s["scene"]]
                visuals[s["scene"]] = dest
                # Drive the clip by the FULL VO duration (bug3): no `time_cap` is set
                # anymore, so _scene_clip_duration returns the natural VO length — the
                # clip is never truncated mid-sentence. Output length is bounded at the
                # GENERATION word ceiling; _enforce_duration_guard is the backstop.
                # Caption TEXT uses the clean `display` string when the script carries
                # one (no phonetic respellings like MCP->"em-xê-pê"), else falls back to
                # `narration`. TTS still reads `narration` (above) so pronunciation is
                # unaffected. Fallback chain: display -> narration.
                f_scenes.append(FootageScene(scene=s["scene"], clipPath=dest, audioPath=r["audioPath"],
                                             caption=s.get("display") or s["narration"],
                                             durationS=_scene_clip_duration(s, r),
                                             # Deliberate beat silence (OmniVoice punctuation
                                             # beats) so the pace pass can discount it; absent
                                             # on cache hits -> 0.0, handled by the gap discount.
                                             beatSilenceS=float(r.get("beatSilenceS") or 0.0)))
            _set_progress(job_id, "render", 85,
                          "Dựng video (dịch) 0%" if is_translate_full else "Dựng video 0%")
            set_ff_progress_cb(_assemble_progress_cb(job_id, "render"))
            try:
                with _timed(job_id, "render"):
                    if is_translate_full:
                        # translate_full: footage-cut body (fit clips to VN VO, MUTED source,
                        # Ken Burns zoom) + EasyOCR caption-cover run on the CUT video +
                        # per-word karaoke over the cover band + credit slate. src audio is
                        # always muted (no _with_src_audio_volume).
                        res = assemble_translate_full(
                            TranslateFullAssembleRequest(
                                page=page_name, title=job_title, scenes=f_scenes,
                                width=width, height=height, fps=30, videoId=video_id,
                                sourceName=source_name, sourceLink=source_link,
                                sourceLogo=source_logo, sourceHandle=source_handle,
                                addCredit=add_credit, engine=vcm,
                                captionBand=list(_translate_full_caption_band()),
                            )
                        )
                    else:
                        res = assemble_footage(
                            _with_src_audio_volume(
                                FootageAssembleRequest(page=page_name, title=job_title, scenes=f_scenes,
                                                       width=width, height=height, videoId=video_id,
                                                       sourceName=source_name, sourceLink=source_link,
                                                       sourceLogo=source_logo, sourceHandle=source_handle, outroHandle=outro_handle, addCredit=add_credit,
                                                       engine=vcm),
                                src_audio_volume,
                            )
                        )
            finally:
                set_ff_progress_cb(None)
            # HARD GUARD (owner-requested backstop): the finished footage video MUST be
            # STRICTLY shorter than the source. Shared with the stickman path so every
            # source-derived assemble enforces the same invariant (self-skips when no
            # source duration is known).
            # Section D: only enforce for SOURCE-TRACKING modes (summary/recap).
            # commentary/educational may legitimately exceed the source, so the guard
            # would false-fail them — skip it there. translate_full keeps ~100% of the
            # content so its output ~= source (not strictly shorter); it is NOT source-
            # tracking, so the guard self-skips (would else false-fail it).
            if _mode_tracks_source(edit_mode):
                _enforce_duration_guard(job_id, res, _src_dur)
            visual_kind = "clip"
        elif render_mode == "stickman":
            # Render a stickman animation clip per scene (length = its VO), then
            # reuse the footage assembler (VO + karaoke captions over the clip).
            _set_progress(job_id, "footage", 60, f"Dựng stickman {len(scenes)} cảnh")
            clips_dir = os.path.join(_page_clips_dir(page_name), f"job{job_id}")
            os.makedirs(clips_dir, exist_ok=True)
            # render_model picks the stickman engine for the WHOLE job:
            #   "stickman-procedural" → CPU-only Pillow+FFmpeg renderer (no GPU/VRAM,
            #     distinct dest per scene) → safe to PARALLELIZE like the cut step.
            #   anything else stickman-* (incl. "stickman-blender") → headless Blender,
            #     a GPU/VRAM-heavy subprocess per scene. On the single 8GB GPU, running
            #     several Blender renders concurrently would contend for one GPU and
            #     could exhaust VRAM, so the Blender path is kept SERIAL on purpose.
            # Both engines emit the same silent libx264/yuv420p clip, so the footage
            # assembler is engine-agnostic.
            stick_items = []
            for s in scenes:
                r = audio.get(s["scene"])
                if not r:
                    raise RuntimeError(f"scene {s['scene']}: no audio produced")
                dest = os.path.join(clips_dir, f"scene{s['scene']:03d}.mp4")
                stick_items.append((s, r, dest))

            n_stick = len(stick_items)
            _stick_done = [0]
            _stick_done_lock = threading.Lock()

            def _do_stickman(item):
                s, _r, dest = item
                render_stickman_clip_procedural(dest, _r.get("durationS") or 3.0, width, height)
                with _stick_done_lock:
                    _stick_done[0] += 1
                    done = _stick_done[0]
                pct = 60 + round(done / max(1, n_stick) * (85 - 60))
                _set_progress(job_id, "footage", min(pct, 84), f"Stickman cảnh {done}/{n_stick}")
                return dest

            if rm == "stickman-procedural":
                # CPU-only → bounded parallel pool, results IN SCENE ORDER.
                _run_cuts(stick_items, _do_stickman, _cut_workers())
            else:
                # Blender (GPU) → SERIAL (one GPU, 8GB VRAM; concurrent renders risk OOM).
                for i, (s, r, dest) in enumerate(stick_items):
                    render_stickman_clip(dest, r.get("durationS") or 3.0, width, height)
                    pct = 60 + round((i + 1) / max(1, n_stick) * (85 - 60))
                    _set_progress(job_id, "footage", min(pct, 84), f"Stickman cảnh {i+1}/{n_stick}")

            visuals, f_scenes = {}, []
            for s, r, dest in stick_items:
                visuals[s["scene"]] = dest
                # min(vo_dur, time_cap) for consistency; stickman has no source today
                # so no cap is ever set → this returns the raw VO unchanged. Future-
                # proof if stickman is ever wired to a source-derived budget.
                # Caption uses clean `display` when present, else `narration` (TTS still
                # reads `narration`). Fallback chain: display -> narration.
                f_scenes.append(FootageScene(scene=s["scene"], clipPath=dest, audioPath=r["audioPath"],
                                             caption=s.get("display") or s["narration"],
                                             durationS=_scene_clip_duration(s, r),
                                             # Deliberate beat silence (OmniVoice punctuation
                                             # beats) so the pace pass can discount it; absent
                                             # on cache hits -> 0.0, handled by the gap discount.
                                             beatSilenceS=float(r.get("beatSilenceS") or 0.0)))
            _set_progress(job_id, "render", 85, "Dựng video 0%")
            set_ff_progress_cb(_assemble_progress_cb(job_id, "render"))
            try:
                with _timed(job_id, "render"):
                    res = assemble_footage(
                        _with_src_audio_volume(
                            FootageAssembleRequest(page=page_name, title=job_title, scenes=f_scenes,
                                                   width=width, height=height, videoId=video_id,
                                                   sourceName=source_name, sourceLink=source_link,
                                                   sourceLogo=source_logo, sourceHandle=source_handle, outroHandle=outro_handle, addCredit=add_credit,
                                                   engine=vcm),
                            src_audio_volume,
                        )
                    )
            finally:
                set_ff_progress_cb(None)
            # Same source-output duration invariant as the footage path. NOTE: the
            # footage branch's local `_src_dur` is NOT in scope here (mutually exclusive
            # elif), so derive the source duration from the function-level `src_video`.
            # For story_voiceover / no-source stickman jobs `src_video` is None → the
            # guard self-skips. (Today stickman never carries a local source video, so
            # this is a structural backstop that activates if/when one ever does.)
            # Section D: gate identically to the footage path — only source-tracking
            # modes (summary/recap) enforce the guard. Stickman today carries no source
            # video (src_video None → guard self-skips anyway), so this is a structural
            # backstop; the mode gate keeps it consistent if a source is ever attached.
            _stick_src_dur = _probe_duration(src_video["videoPath"]) if src_video else None
            if _mode_tracks_source(edit_mode):
                _enforce_duration_guard(job_id, res, _stick_src_dur)
            visual_kind = "clip"
        else:
            _set_progress(job_id, "image", 60, f"Tạo ảnh 0/{len(scenes)}")
            # Per-scene SDXL render is a COUNT-based step (kept N/M, not converted to
            # %); forward each finished image into the image band [60,85] so the bar
            # advances live instead of freezing at 60 until assemble.
            LO_IMG, HI_IMG = 60, 85
            # SDXL renders at the bucket matching the OUTPUT aspect (not the old
            # hardcoded 768x1344 portrait), so a 16:9 / 1:1 / 4:5 job gets natively
            # framed stills instead of a portrait image letterboxed with blur bars.
            _sdxl_w, _sdxl_h = sdxl_dims_for_aspect(job.get("aspect"))
            imgs = generate_images(
                ImagesRequest(
                    scenes=[ImageScene(scene=s["scene"], image_prompt=s["image_prompt"]) for s in scenes],
                    width=_sdxl_w, height=_sdxl_h,
                    checkpoint=RENDER_CHECKPOINTS.get(rm),
                ),
                progress=lambda pct, msg: _set_progress(
                    job_id, "image", LO_IMG + round(min(100, max(0, pct)) / 100 * (HI_IMG - LO_IMG)), msg),
            )
            visuals = _localize_images(page_name, job_id, imgs["results"])
            a_scenes = []
            for s in scenes:
                r = audio.get(s["scene"])
                if not r:
                    raise RuntimeError(f"scene {s['scene']}: no audio produced")
                # Caption uses clean `display` when present, else `narration` (TTS still
                # reads `narration`). Fallback chain: display -> narration.
                a_scenes.append(
                    AssembleScene(scene=s["scene"], imagePath=visuals[s["scene"]], audioPath=r["audioPath"],
                                  caption=s.get("display") or s["narration"], durationS=r.get("durationS"))
                )
            _set_progress(job_id, "render", 85, "Dựng video 0%")
            set_ff_progress_cb(_assemble_progress_cb(job_id, "render"))
            try:
                with _timed(job_id, "render"):
                    res = assemble(
                        AssembleRequest(page=page_name, title=job_title, scenes=a_scenes,
                                        width=width, height=height, videoId=video_id,
                                        sourceName=source_name, sourceLink=source_link,
                                        sourceLogo=source_logo, sourceHandle=source_handle, outroHandle=outro_handle, addCredit=add_credit)
                    )
            finally:
                set_ff_progress_cb(None)
            visual_kind = "image"

        # 4) persist assets + finalize (poster frame for the Videos grid)
        # Dubbed has no narration scenes/audio → no per-scene audio_path (the original
        # source audio is carried inside the assembled mp4, not as a separate asset).
        first_audio = (audio.get(scenes[0]["scene"], {}).get("audioPath")
                       if scenes else None)
        # No sibling .thumb.jpg is created and thumb_path stays NULL: the chosen cover
        # (if any) is baked into the mp4 as its first frame by the FFmpeg assembly step.
        # The FE renders the <video> first frame when thumbUrl is null; POST
        # /api/videos/{id}/cover still sets a display-only poster after render.
        _save_assets(video_id, scenes, visuals, audio, visual_kind=visual_kind)
        # Resolve the cover: the owner's MANUAL cover if one is attached, else coverless
        # (auto-cover generation was removed — see _resolve_manual_cover). Then bake it into
        # the mp4 as its FIRST FRAME (no-op when there's no cover). Must run BEFORE finalize
        # so videos.video_path points at the baked file. In-place, no re-encode.
        cover_to_bake = _resolve_manual_cover(job)
        bake_cover_first_frame(res["videoPath"], cover_to_bake,
                               res.get("width") or width, res.get("height") or height)
        _finalize_video(video_id, first_audio, res["videoPath"], res["durationS"],
                        res.get("width"), res.get("height"), None)

        # 5) RENDER-CONTENT SNAPSHOT (must run BEFORE cleanup deletes intermediates).
        #    Copy this video's reusable content (script text + per-scene VISUAL +
        #    per-scene VO) into _cache/renders/<video_id>/ so it can later be
        #    re-assembled at a DIFFERENT aspect ratio via POST /api/videos/{id}/clone
        #    (assemble-only — no ingest/script/TTS/SDXL). For footage/stickman the
        #    cached visual is the per-scene CUT CLIP (aspect-agnostic; re-framed on
        #    clone); for image mode it's the SDXL still (scaled/cropped to the new
        #    aspect). Best-effort + env-guarded (RENDER_CACHE, default ON); a failure
        #    here must NEVER fail the already-finalized video. Lives under _cache,
        #    which cleanup never touches — so it survives _cleanup_job_intermediates.
        try:
            render_cache.store_render(
                video_id,
                page=page_name,
                render_mode=render_mode,
                visual_kind=visual_kind,
                title=job_title,
                aspect=job.get("aspect"),
                width=res.get("width") or width,
                height=res.get("height") or height,
                source_name=source_name,
                source_link=source_link,
                source_logo=source_logo,
                source_handle=source_handle,
                add_credit=add_credit,
                bgm_path=None,        # pipeline does not set bgm today; recorded as the value used
                bgm_volume=None,
                src_audio_volume=src_audio_volume,
                scenes=scenes,
                visuals=visuals,
                audio=audio,
            )
        except Exception as snap_exc:
            print(f"[runner] job {job_id} render-cache snapshot error (ignored): {snap_exc}")

        # 6) publish — OPT-IN. Only auto-publish when the job was created with publish=true.
        #    Otherwise the video stays 'ready' (set by _finalize_video) for manual publish
        #    via POST /api/videos/{id}/publish.
        #
        #    Auto-publish goes through the SHARED publish core (publish_video_core),
        #    the exact same code path as the manual endpoint — account_ids=None => AUTO
        #    mode: publish to ALL currently-connected channels of the JOB's OWN page
        #    (youtube + facebook, whichever have a token on disk), records posts rows,
        #    flips the video to 'published' on a real public go-live, and keeps
        #    partial-success semantics. Identity is taken ONLY from the page's own
        #    platform_accounts tokens (borrowed-account rule), never inferred.
        #
        #    BEST-EFFORT: a publish failure must NOT fail the whole job. The core raises
        #    HTTPException on validation/all-failed (e.g. no linked account, or every
        #    platform failed); we catch EVERYTHING here, log it, and leave the video
        #    'ready'. The video is already finalized at this point, so the job is a
        #    success regardless of the publish outcome.
        if job.get("publish") and not API_UPLOAD_ENABLED:
            # API upload kill-switch is ON: record the video as produced but skip the
            # auto-publish entirely (never fail the job). The video stays 'ready'.
            _set_progress(job_id, "publish", 95, "Hoàn tất (đăng qua API đang tạm tắt)")
            print(f"[runner] job {job_id} auto-publish SKIPPED (API_UPLOAD_ENABLED is off)")
        elif job.get("publish"):
            _set_progress(job_id, "publish", 95, "Đăng / hoàn tất")
            try:
                # state defaults to DRAFT (safe) — matches the manual endpoint default;
                # a Facebook DRAFT reel is recorded but not made public.
                # Per-platform target: when publish_platform is set, publish ONLY to
                # that platform's connected channel of the job's OWN page; otherwise
                # account_ids=None => AUTO mode (ALL connected channels). Same single
                # publish core as the manual endpoint, keyed on accountIds internally.
                target_platform = (job.get("publish_platform") or "").strip().lower() or None
                account_ids = _resolve_publish_account_ids(job)
                if target_platform is not None and account_ids is None:
                    print(f"[runner] job {job_id} auto-publish skipped: page "
                          f"{job['page_id']} has no '{target_platform}' channel")
                else:
                    result = publish_video_core(video_id, account_ids=account_ids)
                    oks = [r for r in result.get("results", []) if r.get("ok")]
                    fails = [r for r in result.get("results", []) if not r.get("ok")]
                    print(f"[runner] job {job_id} auto-publish: published={result.get('published')} "
                          f"ok={[r.get('platform') for r in oks]} failed={[r.get('platform') for r in fails]}")
            except Exception as pub_exc:
                # e.g. no connected account, all platforms failed, rate limit, spec
                # mismatch. Log + continue — the video stays 'ready' (produced, not
                # published), never failing the job.
                detail = getattr(pub_exc, "detail", None) or str(pub_exc) or pub_exc.__class__.__name__
                print(f"[runner] job {job_id} produced (auto-publish skipped/failed): {detail}")
        else:
            _set_progress(job_id, "publish", 95, "Hoàn tất (chưa đăng)")
            print(f"[runner] job {job_id} produced (publish disabled, left as ready)")

        _job_done(job_id)
        print(f"[runner] job {job_id} done -> {res['videoPath']} ({res['durationS']}s, {len(scenes)} scenes)")

        # Post-success cleanup — free disk by removing THIS job's intermediates,
        # keeping only the final video + thumbnail and the shared caches. Models run
        # sequentially (one job at a time), so nothing else needs these files now.
        # Best-effort and fully wrapped: a cleanup failure must NEVER fail the job —
        # the video is already finalized and the job already marked done above.
        try:
            _cleanup_job_intermediates(
                job_id, page_name, res.get("videoPath"), render_mode,
                visuals=visuals, audio=audio,
                source_video_id=(src_video.get("videoId") if isinstance(src_video, dict) else None),
            )
        except Exception as clean_exc:
            print(f"[runner] job {job_id} cleanup error (ignored): {clean_exc}")

    except Exception as exc:
        # Classify: a user STOP (explicit _check_cancel JobStopped, OR a generic
        # exception that surfaced because /stop tree-killed the active subprocess
        # mid-step — detected via _was_stopped) finalizes as 'stopped', NOT 'failed'.
        # A genuine pipeline fault stays 'failed' so the truth is preserved.
        if isinstance(exc, JobStopped) or _was_stopped(job_id):
            _job_stopped(job_id, video_id)
            print(f"[runner] job {job_id} STOPPED by user")
        else:
            detail = getattr(exc, "detail", None) or str(exc) or exc.__class__.__name__
            traceback.print_exc()
            _job_failed(job_id, video_id, str(detail))
            print(f"[runner] job {job_id} FAILED: {detail}")
    finally:
        set_model_busy(False)
        set_active_job(None)  # drop this job's proc attribution


# ---- worker loop -------------------------------------------------------------

def _worker_loop() -> None:
    print("[runner] worker started")
    while True:
        try:
            job = _claim_job()
        except Exception as exc:  # DB hiccup → back off and retry
            print(f"[runner] claim error: {exc}")
            job = None
        if not job:
            time.sleep(POLL_SECONDS)
            continue
        print(f"[runner] picked job {job['id']} (page {job['page_id']})")
        # CLONE job: re-render an existing video at a new aspect (assemble-only).
        # The clone endpoint already created the destination videos row linked to
        # this job; resolve it and dispatch the assemble-only path. A clone job
        # with no destination video row is a bug in the endpoint — fail loudly.
        if job.get("clone_of_video_id"):
            vrow = _video_for_job(job["id"])
            if not vrow:
                _job_failed(job["id"], None,
                            "clone job has no destination video row (endpoint bug)")
                continue
            job["_video_id"] = vrow["id"]
            _process_clone_job(job)
            continue
        _process_job(job)


def _recover_stale_jobs() -> None:
    """Fail jobs orphaned by a server crash/restart.

    This is a single-process runner: a job left in status='running' at startup
    has no live worker behind it, so it can never progress. Mark such jobs
    'failed' and fail any video still 'rendering'. Safe to run once at startup.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "UPDATE jobs SET status = 'failed',"
            " error = 'Công việc bị gián đoạn do máy chủ khởi động lại. Hãy chạy lại.',"
            " finished_at = now()"
            " WHERE status = 'running' RETURNING id"
        ).fetchall()
        conn.execute("UPDATE videos SET status = 'failed' WHERE status = 'rendering'")
    n = len(rows)
    if n:
        ids = ", ".join(str(r["id"]) for r in rows)
        print(f"[runner] recovered {n} stale running job(s) -> failed: {ids}")


def start_runner() -> None:
    """Launch the worker thread once (idempotent across reload/import)."""
    global _started
    with _lock:
        if _started:
            return
        if os.getenv("RUNNER_ENABLED", "1") not in ("1", "true", "True"):
            print("[runner] disabled via RUNNER_ENABLED")
            return
        _recover_stale_jobs()
        _started = True
        threading.Thread(target=_worker_loop, name="cf-runner", daemon=True).start()
