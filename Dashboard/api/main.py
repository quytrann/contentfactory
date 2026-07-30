"""ContentFactory Dashboard API.

A thin read layer over the local PostgreSQL store. Endpoints return JSON shaped
to match the frontend's TypeScript types (camelCase), so the React app can swap
from mock data to live data without reshaping. Everything is read-only for now.

Run:  uvicorn main:app --host 127.0.0.1 --port 4000
"""

import asyncio
import ctypes
import json
import os
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from urllib.parse import quote

import psycopg
from psycopg.types.json import Json
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Load .env BEFORE importing project modules. Several of them read env vars at IMPORT
# time (notably generate.py's `CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")`), so if
# .env is not already in os.environ they silently freeze to their defaults — and a
# later load_dotenv() cannot fix an already-evaluated module global. That exact bug
# made every script-gen fail with "Claude binary not found: claude".
load_dotenv()

import generate  # module handle for batch probe + VI title translation reuse
import llm_gate  # provider gate: backs GET /api/llm/models (catalogue + 6h disk cache)
import render_cache
import tts_cache
from platform_specs import get_platform_specs
from db import get_conn
from generate import CONTENT_OUTPUT_ROOT, RENDER_CHECKPOINTS, active_worker_pids, get_active_job_id
from generate import _covers_tree_guard
from generate import warmup_comfyui
from generate import router as generate_router
from runner import ASPECTS as _RUNNER_ASPECTS
from runner import _CANCEL_REQUESTED, _STOPPED_JOBS
from runner import start_runner
from generate import kill_job_processes
import facebook_upload  # decide_mode preflight (path-only probe; no creds/network)
# Shared publish core — ONE code path used by BOTH this endpoint and the runner's
# auto-publish step. Constants/helpers below are re-imported (not redefined) so the
# rest of main.py keeps working and there is a single source of truth.
from publish_core import (  # noqa: E402  (leaf module, no import cycle)
    API_UPLOAD_ENABLED,
    FACEBOOK_REELS_24H_LIMIT,
    PUBLISHABLE_PLATFORMS,
    UPLOAD_PRIVACY,
    _abs_creds_ref,
    _build_description,
    _dispatch_publish,
    _is_connected,
    _publish_facebook,
    _publish_youtube,
    _REPO_ROOT,
    publish_video_core,
)

# .env is already loaded above (before the project imports). Re-affirm idempotently so
# any late-imported module still sees it; harmless (override=False).
load_dotenv()

# Persist runner/script-gen diagnostics to a rotating file (the API runs with a hidden
# window, so stdout is otherwise lost). Installed right after env load so CF_LOG_FILE is
# honored and before anything emits. Idempotent under uvicorn --reload.
import log_setup  # noqa: E402
log_setup.setup()

# Also load the external secrets .env (Google OAuth app creds), kept OUTSIDE the repo.
# Best-effort: if absent, the clear error is raised lazily only when a publish needs it.
from oauth_env import load_oauth_env  # noqa: E402
# Reuse the SAME slug logic the OAuth flow uses to write token files, so the
# conventional credentials_ref we compute always matches what's on disk.
from youtube_auth import _slug  # noqa: E402

load_oauth_env()


def _conventional_creds_ref(page_name: str, platform: str) -> str:
    """The conventional path-only credentials_ref for a (page, platform).

    Mirrors youtube_auth.py's on-disk layout exactly (same `_slug`):
        Dashboard/secrets/<slug>/<platform>.json
    Relative to the repo root, matching how existing refs are stored. This is a
    PATH only — never the token contents (borrowed-account rule).
    """
    return f"Dashboard/secrets/{_slug(page_name)}/{platform}.json"


def _backfill_null_creds_refs() -> None:
    """One-time startup self-heal: any platform_accounts row whose credentials_ref
    is NULL gets the conventional path for its (page, platform). This makes
    pre-existing rows uploadable + visible in the Publish modal once their token
    file exists on disk. `_is_connected` still gates on the file actually being
    present, so this never fabricates a connection."""
    healed = 0
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT pa.id, pa.platform, p.name AS page_name
              FROM platform_accounts pa
              JOIN pages p ON p.id = pa.page_id
             WHERE pa.credentials_ref IS NULL
            """
        ).fetchall()
        for r in rows:
            ref = _conventional_creds_ref(r["page_name"], r["platform"])
            conn.execute(
                "UPDATE platform_accounts SET credentials_ref = %s WHERE id = %s",
                (ref, r["id"]),
            )
            healed += 1
    print(f"[startup] credentials_ref backfill: healed {healed} NULL row(s).")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One-time self-heal: backfill any NULL credentials_ref to the conventional path.
    _backfill_null_creds_refs()
    # Start the background job runner (queued jobs → finished videos).
    start_runner()
    # Sweep stale per-scene TTS cache entries (>24h since last use) in a daemon
    # thread — non-blocking and best-effort, so a failure never blocks startup.
    try:
        tts_cache.start_eviction_async()
    except Exception as e:  # noqa: BLE001 — startup must never fail on this
        print(f"[startup] tts cache eviction trigger failed: {e}")
    # Pre-load SDXL into ComfyUI so the owner's FIRST cover render isn't cold. Daemon
    # thread — non-blocking, runs once per boot, best-effort (never fails startup).
    try:
        threading.Thread(target=warmup_comfyui, daemon=True).start()
    except Exception as e:  # noqa: BLE001 — startup must never fail on this
        print(f"[startup] comfyui warmup trigger failed: {e}")
    # Retry media files a previous delete could not unlink because they were locked
    # (typically our own /media stream). A fresh boot holds no handles, so this is the
    # single best moment to catch them. Best-effort — never fails startup.
    try:
        res = sweep_pending_deletes()
        if res["removed"] or res["stillPending"]:
            print(f"[startup] pending deletes: removed {res['removed']}, "
                  f"still locked {res['stillPending']}")
    except Exception as e:  # noqa: BLE001
        print(f"[startup] pending-delete sweep failed: {e}")
    yield
    # Graceful shutdown: wait for any active job to finish before exiting (up to
    # 5 min). This ensures uvicorn --reload file-change restarts don't kill a
    # mid-run TTS or render job. If the deadline is exceeded we proceed anyway.
    _SHUTDOWN_WAIT_S = 300
    deadline = time.monotonic() + _SHUTDOWN_WAIT_S
    while True:
        jid = get_active_job_id()
        if jid is None:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"[shutdown] timeout after {_SHUTDOWN_WAIT_S}s — job {jid} still running, exiting anyway")
            break
        print(f"[shutdown] job {jid} in progress — waiting (up to {round(remaining)}s remaining)…")
        await asyncio.sleep(5)


app = FastAPI(title="ContentFactory Dashboard API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")],
    allow_methods=["GET", "POST", "DELETE", "PATCH"],
    allow_headers=["*"],
)

# Host generation endpoints (called by n8n over HTTP): /generate/*
app.include_router(generate_router)

# Accepted values for a job's auto-publish target platform (jobs.publish_platform).
# Mirrors platform_accounts.platform values the dashboard offers. NOTE: only youtube
# and facebook are actually publishable today (PUBLISHABLE_PLATFORMS in publish_core);
# tiktok/instagram are accepted here for forward-compatibility but the runner will
# find no connected channel and skip auto-publish gracefully until an uploader exists.
PUBLISH_PLATFORM_CHOICES = {"youtube", "tiktok", "instagram", "facebook"}

# Per-platform management console links (not stored in the DB).
MANAGE_URL = {
    "youtube": "https://studio.youtube.com",
    "tiktok": "https://www.tiktok.com/tiktokstudio",
    "instagram": "https://business.facebook.com",
    "facebook": "https://business.facebook.com",
}


# ---- helpers -----------------------------------------------------------

def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


def _num(value: Any) -> float:
    return float(value) if value is not None else 0.0


def _facebook_page_id(credentials_ref: str | None) -> str | None:
    """Read ONLY the public `page_id` from a facebook.json creds file.

    A Facebook Page ID is public, so it is safe to expose. The page access token
    lives in the same file but is NEVER read into the response. Defensive: any
    problem (missing/unreadable/malformed file, absent key) returns None so the
    caller simply omits the field instead of erroring.
    """
    if not credentials_ref:
        return None
    path = credentials_ref if os.path.isabs(credentials_ref) else os.path.join(_REPO_ROOT, credentials_ref)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    page_id = data.get("page_id")
    return str(page_id) if page_id else None


# ---- data fetchers -----------------------------------------------------

def fetch_pages(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT p.id, p.name, p.language, p.status,
               p.creator_name, p.channel_url,
               (SELECT count(*) FROM videos v WHERE v.page_id = p.id) AS video_count,
               (SELECT count(*) FROM videos v WHERE v.page_id = p.id AND v.status = 'published') AS published_count,
               (SELECT array_agg(DISTINCT pa.platform ORDER BY pa.platform)
                  FROM platform_accounts pa WHERE pa.page_id = p.id) AS platforms
          FROM pages p
         ORDER BY p.id
        """
    ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "name": r["name"],
                "language": r["language"],
                "status": r["status"],
                "creatorName": r["creator_name"],
                "channelUrl": r["channel_url"],
                "platforms": r["platforms"] or [],
                # pages.config was dropped (2026-06-25 redesign): production options
                # now live on the job, not the page. These were always the same fixed
                # defaults the old create_page INSERTed, so emit them as static labels
                # to keep the response shape stable for the dashboard.
                "config": {
                    "imageModel": "SDXL",
                    "tts": "VieNeu-TTS",
                    "timestamp": "faster-whisper",
                    "motion": "ken_burns",
                },
                "videoCount": r["video_count"],
                "publishedCount": r["published_count"],
            }
        )
    return out


def fetch_accounts(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, page_id, platform, account_label, account_type, status, approval, credentials_ref
          FROM platform_accounts
         ORDER BY id
        """
    ).fetchall()
    return [
        {
            "id": r["id"],
            "pageId": r["page_id"],
            "platform": r["platform"],
            "accountLabel": r["account_label"],
            "accountType": r["account_type"],
            "status": r["status"],
            # Once the OAuth token exists, surface it as 'connected' (API ready).
            "approval": "connected" if _is_connected(r["credentials_ref"]) else r["approval"],
        }
        for r in rows
    ]


def fetch_jobs(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, page_id, page_seq, input_type, input_payload, title, status, cost_usd, created_at, finished_at,
               edit_mode, aspect, render_model, voice_clone_model, progress_step, progress_pct, progress_msg, error,
               needs_input, llm_provider, llm_model,
               (SELECT p.url FROM posts p JOIN videos v ON p.video_id = v.id
                 WHERE v.job_id = jobs.id AND p.platform = 'youtube' AND p.url IS NOT NULL
                 ORDER BY p.id DESC LIMIT 1) AS uploaded_url,
               -- ALL posts (every platform/page, posted + draft) of every video under
               -- this job. Drives the Jobs-table multi-platform link/page columns.
               -- LEFT JOIN: posts.platform_account_id is nullable (ON DELETE SET NULL),
               -- so a post whose account row was deleted still surfaces with null
               -- pageId/pageName rather than being dropped. url is null for drafts.
               -- COALESCE → always a JSON array ('[]' when the job has no posts).
               COALESCE(
                 (SELECT jsonb_agg(
                           jsonb_build_object(
                             'platform', po.platform,
                             'url',      po.url,
                             'pageId',   pa.page_id,
                             'pageName', pg.name,
                             'status',   po.status
                           )
                           ORDER BY po.platform, po.id
                         )
                    FROM posts po
                    LEFT JOIN platform_accounts pa ON pa.id = po.platform_account_id
                    LEFT JOIN pages pg ON pg.id = pa.page_id
                   WHERE po.video_id IN (SELECT id FROM videos WHERE job_id = jobs.id)),
                 '[]'::jsonb
               ) AS published_posts
          FROM jobs
         ORDER BY created_at DESC, id DESC
        """
    ).fetchall()
    return [
        {
            "id": r["id"],
            "pageId": r["page_id"],
            # Per-page display sequence ("Job #N"): stable, gap-free-at-creation,
            # matches the job-history order. NULL only on rows predating the column.
            "pageSeq": r["page_seq"],
            "inputType": r["input_type"],
            "inputPayload": r["input_payload"],
            # Output title (owner-typed / auto-filled at batch-create). NULL for
            # single-create jobs that never carried one. Powers the held-list review.
            "title": r["title"],
            "status": r["status"],
            "costUsd": _num(r["cost_usd"]),
            "createdAt": _iso(r["created_at"]),
            "finishedAt": _iso(r["finished_at"]),
            "editMode": r["edit_mode"],
            "aspect": r["aspect"],
            "renderModel": r["render_model"],
            "voiceCloneModel": r["voice_clone_model"],
            # Per-job LLM choice. null = claude-cli (the subscription default) — the FE
            # should render null as "Claude (subscription)", not as "unknown".
            "llmProvider": r["llm_provider"],
            "llmModel": r["llm_model"],
            "progressStep": r["progress_step"],
            "progressPct": r["progress_pct"],
            "progressMsg": r["progress_msg"],
            "error": r["error"],
            "uploadedUrl": r["uploaded_url"],
            # All posts of all videos under this job (multi-platform, multi-page,
            # posted + draft). psycopg3 parses the jsonb_agg result into a Python
            # list of dicts; COALESCE guarantees [] (never None) when no posts.
            "publishedPosts": r["published_posts"] or [],
            # NEEDS-INPUT payload (Dubbed credit gate). NULL for every job except one
            # currently parked in status='needs_input'. Shape:
            # {kind, missingFields[], prefill{}, creditDecision, videoId}. psycopg3
            # parses the JSONB into a Python dict; FE treats null as "no input needed".
            "needsInput": r["needs_input"],
        }
        for r in rows
    ]


def fetch_videos(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT v.id, v.page_id, v.job_id, v.title, v.duration_s, v.status, v.created_at,
               v.width, v.height, v.thumb_path, v.video_path, v.facebook_tags,
               v.llm_provider_used, v.llm_model_used,
               j.voice_clone_model, j.render_model, j.voice, j.src_audio_volume,
               j.edit_mode, j.aspect, j.target_sec, j.add_credit,
               -- Guard jsonb_array_length: Dubbed-mode videos store v.script as a JSON
               -- OBJECT ({mode,subs,filler}), not a scene array. jsonb_array_length on a
               -- non-array RAISES (COALESCE only catches NULL, not the exception), which
               -- would 500 the whole videos list / bootstrap once any dubbed video exists.
               COALESCE(CASE WHEN jsonb_typeof(v.script) = 'array'
                             THEN jsonb_array_length(v.script) END, 0) AS scenes,
               COALESCE(
                 (SELECT array_agg(DISTINCT po.platform ORDER BY po.platform)
                    FROM posts po
                   WHERE po.video_id = v.id AND po.status = 'posted'),
                 '{}'
               ) AS posted_platforms,
               -- Pages this video is PUBLISHED TO (many-to-many), derived from posts
               -- → platform_accounts.page_id. Drives the per-page "Products" block:
               -- a video appears under every page it has a post on (origin or not).
               -- Includes draft+posted posts so a recorded publish still surfaces.
               COALESCE(
                 (SELECT array_agg(DISTINCT pa.page_id)
                    FROM posts po
                    JOIN platform_accounts pa ON pa.id = po.platform_account_id
                   WHERE po.video_id = v.id),
                 '{}'
               ) AS published_page_ids,
               -- Full per-post detail (platform + status + permalink + which page)
               -- so a card can, once posted, swap the "Publish" button for a link
               -- to the real video + a status chip. Includes drafts (a draft is a
               -- real upload, just not public) with its own status so the UI can
               -- distinguish 'posted' (live) from 'draft'.
               COALESCE(
                 (SELECT jsonb_agg(jsonb_build_object(
                            'platform', po.platform,
                            'status', po.status,
                            'url', po.url,
                            'pageId', pa.page_id,
                            'pageName', p2.name
                          ) ORDER BY po.posted_at DESC)
                    FROM posts po
                    JOIN platform_accounts pa ON pa.id = po.platform_account_id
                    JOIN pages p2 ON p2.id = pa.page_id
                   WHERE po.video_id = v.id),
                 '[]'::jsonb
               ) AS posts
          FROM videos v
          LEFT JOIN jobs j ON j.id = v.job_id
         ORDER BY v.created_at DESC, v.id DESC
        """
    ).fetchall()
    media = lambda p: f"/media?path={quote(p)}" if p else None
    return [
        {
            "id": r["id"],
            "pageId": r["page_id"],
            "jobId": r["job_id"],
            "title": r["title"],
            "durationS": int(_num(r["duration_s"])),
            "scenes": r["scenes"],
            "status": r["status"],
            "createdAt": _iso(r["created_at"]),
            "postedPlatforms": r["posted_platforms"] or [],
            # Pages this video is published to (posts-driven membership). The
            # per-page Products block shows a video if pageId is its origin
            # (v.pageId) OR appears here.
            "publishedPageIds": r["published_page_ids"] or [],
            "posts": r["posts"] or [],
            "width": r["width"],
            "height": r["height"],
            "videoUrl": media(r["video_path"]),
            "thumbUrl": media(r["thumb_path"]),
            "voiceCloneModel": r["voice_clone_model"],
            "renderModel": r["render_model"],
            "voice": r["voice"],
            "srcAudioVolume": _num(r["src_audio_volume"]),
            "editMode": r["edit_mode"],
            "aspect": r["aspect"],
            "targetSec": int(r["target_sec"]) if r["target_sec"] is not None else None,
            "addCredit": bool(r["add_credit"]) if r["add_credit"] is not None else False,
            "facebookTags": r["facebook_tags"],
            # Which LLM ACTUALLY wrote this video's script. null on rows produced before
            # the provider gate AND on script-reuse runs (no LLM ran) — null means
            # "not recorded", NOT "claude-cli"; don't infer a provider that never ran.
            "llmProviderUsed": r["llm_provider_used"],
            "llmModelUsed": r["llm_model_used"],
        }
        for r in rows
    ]


def fetch_org(conn) -> dict:
    # One Google account/email can own MULTIPLE pages across different social
    # platforms, so the OrgChart groups by EMAIL: each block = one distinct
    # owning email, holding the pages under it, each page with its channels.
    pages = conn.execute("SELECT id, name FROM pages ORDER BY id").fetchall()
    accounts = conn.execute(
        """
        SELECT page_id, platform, account_label, approval, credentials_ref
          FROM platform_accounts
         ORDER BY page_id, platform
        """
    ).fetchall()
    by_page: dict[int, list[dict]] = {}
    for a in accounts:
        by_page.setdefault(a["page_id"], []).append(a)

    # Group pages by owning email, preserving first-seen order (pages are
    # iterated by ascending id, so groups order deterministically by the id of
    # their first page).
    groups: dict[str, list[dict]] = {}
    group_order: list[str] = []
    for p in pages:
        accs = by_page.get(p["id"], [])
        if not accs:
            continue
        # The page's owning email, by precedence:
        #   (a) the YouTube account's label (existing behavior for youtube pages)
        #   (b) else the first account_label
        #   (c) else fall back to the page name (its own singleton group)
        # NOTE: pages.config was dropped (2026-06-25 redesign), so the former
        # config.accountEmail precedence step is gone; the owning email is now
        # taken purely from platform_accounts labels.
        gmail = next((a["account_label"] for a in accs if a["platform"] == "youtube" and a["account_label"]), None)
        gmail = gmail or next((a["account_label"] for a in accs if a["account_label"]), None)
        gmail = gmail or p["name"]

        channels = []
        for a in accs:
            ch = {
                "platform": a["platform"],
                "handle": a["account_label"] or "—",
                "manageUrl": MANAGE_URL.get(a["platform"], "#"),
                "status": "connected" if _is_connected(a["credentials_ref"]) else a["approval"],
            }
            # Facebook: point "Manage" at the actual Page (by its public page_id),
            # read from the creds json. The page access token is never read/exposed.
            # https://www.facebook.com/<page_id> reliably opens the Page; an admin
            # lands there with the Page's management tools.
            if a["platform"] == "facebook":
                page_id = _facebook_page_id(a["credentials_ref"])
                if page_id:
                    ch["pageId"] = page_id
                    ch["manageUrl"] = f"https://www.facebook.com/{page_id}"
            channels.append(ch)

        page_block = {"pageId": p["id"], "pageName": p["name"], "channels": channels}
        if gmail not in groups:
            groups[gmail] = []
            group_order.append(gmail)
        groups[gmail].append(page_block)

    out_accounts = []
    for gmail in group_order:
        # Stable order within a group: by pageId ascending.
        pages_in_group = sorted(groups[gmail], key=lambda pb: pb["pageId"])
        # Account-isolation RISK (revised model): one email owning MANY channels
        # across DIFFERENT platforms is normal and fine. The cascade risk is ONLY
        # when one email is used for 2+ channels on the SAME platform (e.g. two
        # Facebook Pages, or two YouTube channels) — a same-platform ban/termination
        # under one Google/Meta account can cascade to the sibling same-platform
        # channel. `riskPlatforms` = the platforms appearing 2+ times across ALL
        # channels of every page in this email group (dedup-count by platform). It
        # is a WARNING surfaced to the dashboard, NOT a block: the per-page UNIQUE
        # (page_id, platform) constraint still stands, but this cross-page,
        # same-email duplication is detected only here, not enforced as a constraint.
        platform_counts: dict[str, int] = {}
        for pb in pages_in_group:
            for ch in pb["channels"]:
                plat = ch.get("platform")
                if plat:
                    platform_counts[plat] = platform_counts.get(plat, 0) + 1
        risk_platforms = sorted(p for p, c in platform_counts.items() if c >= 2)
        out_accounts.append({"gmail": gmail, "pages": pages_in_group, "riskPlatforms": risk_platforms})

    return {"dashboard": "Content Factory", "accounts": out_accounts}


def _kpi(key: str, label: str, series: list[int]) -> dict:
    last = series[-1] if series else 0
    first = series[0] if series else 0
    delta = round((last - first) / first * 100, 1) if first else 0.0
    return {"key": key, "label": label, "value": f"{last:,}", "delta": delta, "spark": series[-8:]}


def fetch_analytics(conn) -> dict:
    # Daily cumulative snapshots summed across all posts (last 14 days present).
    daily = conn.execute(
        """
        SELECT date_trunc('day', fetched_at)::date AS d,
               sum(views)::bigint    AS views,
               sum(likes)::bigint    AS likes,
               sum(comments)::bigint AS comments,
               sum(shares)::bigint   AS shares
          FROM metrics
         GROUP BY d
         ORDER BY d
        """
    ).fetchall()
    daily = daily[-14:]
    day_labels = [d["d"].strftime("%d") for d in daily]
    views_daily = [int(d["views"] or 0) for d in daily]
    likes_daily = [int(d["likes"] or 0) for d in daily]
    comments_daily = [int(d["comments"] or 0) for d in daily]
    shares_daily = [int(d["shares"] or 0) for d in daily]

    # Videos produced per month (replaces the old mock "revenue" series — the
    # schema has no revenue, but video output per month is real and meaningful).
    monthly = conn.execute(
        """
        SELECT date_trunc('month', created_at) AS m, count(*) AS value
          FROM videos
         GROUP BY m
         ORDER BY m
        """
    ).fetchall()
    videos_monthly = [{"month": r["m"].strftime("%b"), "value": int(r["value"])} for r in monthly]

    # Views by platform — latest snapshot per post, grouped by platform.
    split_rows = conn.execute(
        """
        WITH latest AS (
            SELECT DISTINCT ON (m.post_id) m.post_id, m.views, po.platform
              FROM metrics m
              JOIN posts po ON po.id = m.post_id
             ORDER BY m.post_id, m.fetched_at DESC
        )
        SELECT platform, sum(views)::bigint AS views
          FROM latest
         GROUP BY platform
         ORDER BY views DESC
        """
    ).fetchall()
    total_views = sum(int(r["views"] or 0) for r in split_rows) or 1
    platform_split = [
        {
            "platform": r["platform"],
            "views": int(r["views"] or 0),
            "pct": round(int(r["views"] or 0) / total_views * 100),
        }
        for r in split_rows
    ]

    kpis = [
        _kpi("views", "Views", views_daily),
        _kpi("likes", "Likes", likes_daily),
        _kpi("comments", "Comments", comments_daily),
        _kpi("shares", "Shares", shares_daily),
    ]

    return {
        "kpis": kpis,
        "viewsDaily": views_daily,
        "likesDaily": likes_daily,
        "dayLabels": day_labels,
        "videosMonthly": videos_monthly,
        "platformSplit": platform_split,
    }


def fetch_page_analytics(conn, page_id: int) -> dict:
    """Per-page analytics: views split by platform + monthly views (last 12 months).

    Page linkage is via videos.page_id (the `posts` table has no page_id column; a
    post belongs to a page through its video). The platform comes from the post's
    platform_accounts row (joined on posts.platform_account_id), per the contract.

    Views are summed over the LATEST metrics snapshot per post (DISTINCT ON, like
    fetch_analytics) so re-snapshotting a post does not double-count its views.
    Returns empty arrays (not 404) when the page has no metrics yet; the route
    itself 404s only when the page row does not exist.
    """
    # platformSplit: latest snapshot per post -> platform (via platform_accounts).
    split_rows = conn.execute(
        """
        WITH latest AS (
            SELECT DISTINCT ON (m.post_id)
                   m.post_id, m.views, pa.platform
              FROM metrics m
              JOIN posts po              ON po.id = m.post_id
              JOIN videos v              ON v.id  = po.video_id
              JOIN platform_accounts pa  ON pa.id = po.platform_account_id
             WHERE v.page_id = %s
             ORDER BY m.post_id, m.fetched_at DESC
        )
        SELECT platform, sum(views)::bigint AS views
          FROM latest
         GROUP BY platform
         ORDER BY views DESC
        """,
        (page_id,),
    ).fetchall()
    total_views = sum(int(r["views"] or 0) for r in split_rows)
    platform_split = [
        {
            "platform": r["platform"],
            "views": int(r["views"] or 0),
            "pct": round(int(r["views"] or 0) / total_views * 100, 1) if total_views else 0.0,
        }
        for r in split_rows
    ]

    # viewsMonthly: latest snapshot per post, bucketed by snapshot month, last 12.
    monthly_rows = conn.execute(
        """
        WITH latest AS (
            SELECT DISTINCT ON (m.post_id)
                   m.post_id, m.views, m.fetched_at
              FROM metrics m
              JOIN posts po  ON po.id = m.post_id
              JOIN videos v  ON v.id  = po.video_id
             WHERE v.page_id = %s
             ORDER BY m.post_id, m.fetched_at DESC
        )
        SELECT to_char(date_trunc('month', fetched_at), 'YYYY-MM') AS month,
               sum(views)::bigint AS value
          FROM latest
         GROUP BY date_trunc('month', fetched_at)
         ORDER BY date_trunc('month', fetched_at)
        """,
        (page_id,),
    ).fetchall()
    # Monthly buckets from the metrics table (per-post snapshots, e.g. YouTube).
    monthly_map: dict[str, int] = {r["month"]: int(r["value"] or 0) for r in monthly_rows}

    # --- LIVE Facebook Page Insights (page-level "Views") --------------------
    # The metrics table is per-post; Facebook page views come straight from the
    # Business-Suite "Views" metric (page_media_view) via the page's FB token —
    # matching what the owner sees in Insights, no per-post tracking needed.
    # Cached briefly so navigating the dashboard doesn't hammer the Graph API.
    fb_acc = conn.execute(
        """
        SELECT credentials_ref, page_id
          FROM platform_accounts
         WHERE page_id = %s AND platform = 'facebook'
         LIMIT 1
        """,
        (page_id,),
    ).fetchone()
    # Live Facebook Page follower/likes counts (best-effort, cached). Null when the
    # page has no connected FB channel or the Graph call fails.
    fb_followers: int | None = None
    fb_fan_count: int | None = None
    if fb_acc and _is_connected(fb_acc["credentials_ref"]):
        fb = _facebook_page_views_cached(page_id, fb_acc["credentials_ref"])
        if fb["total"] > 0 or fb["monthly"]:
            # Facebook slice comes from live insights; drop any stale metrics-table
            # facebook row so we don't double-count.
            platform_split = [s for s in platform_split if s["platform"] != "facebook"]
            platform_split.append({"platform": "facebook", "views": fb["total"], "pct": 0.0})
            for m, v in fb["monthly"].items():
                monthly_map[m] = monthly_map.get(m, 0) + v
        foll = _facebook_page_followers_cached(page_id, fb_acc["credentials_ref"])
        fb_followers = foll.get("followers")
        fb_fan_count = foll.get("fanCount")

    # Recompute percentages over the merged split.
    grand_total = sum(int(s["views"] or 0) for s in platform_split)
    for s in platform_split:
        s["pct"] = round(int(s["views"] or 0) / grand_total * 100, 1) if grand_total else 0.0
    platform_split.sort(key=lambda s: -int(s["views"] or 0))

    views_monthly = [
        {"month": m, "value": monthly_map[m]} for m in sorted(monthly_map)
    ][-12:]

    return {"platformSplit": platform_split, "viewsMonthly": views_monthly,
            "followers": fb_followers, "fanCount": fb_fan_count}


# Short-lived cache for live Facebook page-view insights (page_id -> (ts, payload)).
# Keeps dashboard navigation from calling the Graph API on every render.
_FB_VIEWS_CACHE: dict[int, tuple[float, dict]] = {}
_FB_VIEWS_TTL_S = 300


def _facebook_page_views_cached(page_id: int, credentials_ref: str) -> dict:
    """Live Facebook page 'views' for the last ~12 months, aggregated to months.

    Returns {"total": int, "monthly": {"YYYY-MM": int}}. Best-effort: on any
    Graph error/timeout returns zeros (so the chart just stays empty, never 500s).
    """
    now = time.time()
    hit = _FB_VIEWS_CACHE.get(page_id)
    if hit and now - hit[0] < _FB_VIEWS_TTL_S:
        return hit[1]

    # Each Graph insights call carries a fixed ~20s latency for this Page regardless
    # of range, and day-period caps the window at ~90 days ("Invalid parameter"
    # beyond that). So make a SINGLE 90-day day-period call (covers the recent
    # months the traffic panel shows) and cache the result — one slow call per TTL,
    # not one per render.
    until = int(now)
    since = until - 89 * 24 * 3600
    res = facebook_upload.page_insights_views(
        {"credentials_ref": credentials_ref}, since=since, until=until, period="day"
    )
    monthly: dict[str, int] = {}
    total = 0
    if res.get("ok"):
        for p in res["points"]:
            month = (p["date"] or "")[:7]  # YYYY-MM
            if not month:
                continue
            monthly[month] = monthly.get(month, 0) + p["value"]
            total += p["value"]

    payload = {"total": total, "monthly": monthly}
    _FB_VIEWS_CACHE[page_id] = (now, payload)
    return payload


# Short-lived cache for live Facebook Page follower/likes counts (page_id -> (ts,
# payload)). Same TTL as the views cache: one slow Graph call per TTL, not per render.
_FB_FOLLOWERS_CACHE: dict[int, tuple[float, dict]] = {}


def _facebook_page_followers_cached(page_id: int, credentials_ref: str) -> dict:
    """Live Facebook Page follower + likes counts, cached with the SAME TTL as the
    views cache. Returns {"followers": int|None, "fanCount": int|None}. Best-effort:
    on any Graph error/timeout returns nulls (so the panel just omits the number,
    never 500s)."""
    now = time.time()
    hit = _FB_FOLLOWERS_CACHE.get(page_id)
    if hit and now - hit[0] < _FB_VIEWS_TTL_S:
        return hit[1]
    res = facebook_upload.page_followers({"credentials_ref": credentials_ref})
    payload = ({"followers": res.get("followers"), "fanCount": res.get("fanCount")}
               if res.get("ok") else {"followers": None, "fanCount": None})
    _FB_FOLLOWERS_CACHE[page_id] = (now, payload)
    return payload


# ---- routes ------------------------------------------------------------

@app.get("/api/health")
def health():
    with get_conn() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


@app.get("/api/pages")
def pages():
    with get_conn() as conn:
        return fetch_pages(conn)


@app.get("/api/accounts")
def accounts():
    with get_conn() as conn:
        return fetch_accounts(conn)


def fetch_linked_channels(conn, page_id: int) -> list[dict]:
    """The page's platform accounts that are LINKED (a real OAuth token exists on
    disk → `_is_connected`) AND that this backend can publish to.

    Drives the Publish modal's checkboxes: every row returned is tick-able. An
    account configured in the DB but without a token file is NOT returned (it
    cannot publish), so the frontend does not need to filter — it ticks all by
    default. `canPublish` is always true here (it is the linked+publishable set);
    it is included so the contract is explicit if the filter ever loosens.
    """
    rows = conn.execute(
        """
        SELECT id, platform, account_label, account_type, credentials_ref
          FROM platform_accounts
         WHERE page_id = %s
         ORDER BY platform
        """,
        (page_id,),
    ).fetchall()
    out = []
    for r in rows:
        if r["platform"] not in PUBLISHABLE_PLATFORMS:
            continue
        if not _is_connected(r["credentials_ref"]):
            continue
        out.append(
            {
                "accountId": r["id"],
                "platform": r["platform"],
                "accountLabel": r["account_label"] or "—",
                "accountType": r["account_type"],
                "linked": True,
                "canPublish": True,
            }
        )
    return out


@app.get("/api/pages/{page_id}/linked-channels")
def linked_channels(page_id: int):
    """List the page's linked, publishable channels (for the Publish modal).

    Returns ONLY accounts that are truly linked (token file present) and that the
    backend has an uploader for. May be an empty list when the page has accounts
    configured but no OAuth token yet — that is expected, not an error.
    """
    with get_conn() as conn:
        page = conn.execute("SELECT id FROM pages WHERE id = %s", (page_id,)).fetchone()
        if not page:
            raise HTTPException(404, "Page not found")
        return {"pageId": page_id, "channels": fetch_linked_channels(conn, page_id)}


@app.get("/api/linked-channels")
def all_linked_channels():
    """List EVERY page's connected, publishable channels (all-pages Publish modal).

    Many-to-many publish: a video can be published to channels across MULTIPLE
    pages, so the Publish modal needs the full cross-page set. Same connection gate
    as the per-page endpoint (a token file must exist on disk → `_is_connected`);
    each channel keeps the LinkedChannel fields (accountId, platform, accountLabel,
    accountType, linked, canPublish). Pages with ZERO connected channels are
    omitted, so the modal only ever shows tickable targets.

    Shape: { "pages": [ { "pageId", "pageName", "channels": [LinkedChannel...] } ] }
    ordered by page id.
    """
    with get_conn() as conn:
        pages = conn.execute("SELECT id, name FROM pages ORDER BY id").fetchall()
        out_pages = []
        for p in pages:
            channels = fetch_linked_channels(conn, p["id"])
            if not channels:
                continue  # omit pages with no connected channel
            out_pages.append({"pageId": p["id"], "pageName": p["name"], "channels": channels})
    return {"pages": out_pages}


@app.get("/api/jobs")
def jobs():
    with get_conn() as conn:
        return fetch_jobs(conn)


@app.get("/api/videos")
def videos():
    with get_conn() as conn:
        return fetch_videos(conn)


@app.get("/api/analytics")
def analytics():
    with get_conn() as conn:
        return fetch_analytics(conn)


@app.get("/api/pages/{page_id}/analytics")
def page_analytics(page_id: int):
    """Per-page analytics: { platformSplit, viewsMonthly, followers, fanCount }.

    followers/fanCount are the live Facebook Page follower + likes counts (cached,
    best-effort; null when the page has no connected FB channel or Graph fails).
    404 when the page does not exist; empty arrays when it exists but has no
    metrics yet (a page with no posts/metrics is valid, not an error).
    """
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM pages WHERE id = %s", (page_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(404, f"page {page_id} not found")
        return fetch_page_analytics(conn, page_id)


@app.get("/api/org")
def org():
    with get_conn() as conn:
        return fetch_org(conn)


@app.get("/api/platform-specs")
def platform_specs():
    """Per-platform upload-spec reference for the Publishing view.

    Read-only, no DB, no secrets. Facebook is derived from the live
    facebook_upload constants (enforced=True); the others are curated guidance
    (enforced=False) because the pipeline does not validate them.
    """
    return get_platform_specs()


# ---- Exit project (whole-stack shutdown, EXCEPT PostgreSQL) ------------

# Windows process-creation flags: run the killer FULLY DETACHED + windowless so
# it OUTLIVES the API. Mirrors Dashboard/app.py's detached-launch pattern
# (_start_comfyui_if_down / _start_api_detached).
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


@app.post("/api/shutdown")
def shutdown():
    """Tear down the whole ContentFactory stack EXCEPT PostgreSQL, then let this
    API process be killed by the detached killer.

    Returns {"ok": true} IMMEDIATELY (before anything is killed) so the browser
    can react/close its tab. The actual teardown runs in exit_project.ps1, spawned
    as a DETACHED, windowless PowerShell that is NOT a child of this API — so when
    it kills the API/uvicorn LAST, it is not killing its own parent mid-run. The
    script sleeps 1.5s first, giving this HTTP 200 time to flush.

    PostgreSQL (Windows service postgresql-x64-16) is never touched.
    """
    api_dir = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(api_dir, "exit_project.ps1")
    api_port = int(os.getenv("API_PORT", "4000"))
    comfy_port = int(os.getenv("CF_COMFY_PORT", "8188"))

    # CREATE_NO_WINDOW ONLY — mirrors app.py's _start_api_detached PowerShell launch.
    # Do NOT add DETACHED_PROCESS: with it set, `powershell -File` is created (so
    # Popen does not raise) but SILENTLY never executes the script — verified by a
    # spawn-flag isolation test. CREATE_NO_WINDOW alone runs the killer, and it still
    # outlives this API because on Windows killing a parent does not cascade to its
    # child (and the killer kills this API LAST anyway).
    creationflags = 0
    if os.name == "nt":
        creationflags = _CREATE_NO_WINDOW

    subprocess.Popen(
        [
            "powershell", "-NoProfile", "-WindowStyle", "Hidden",
            "-ExecutionPolicy", "Bypass", "-File", script,
            "-ApiPort", str(api_port), "-ComfyPort", str(comfy_port),
        ],
        cwd=api_dir,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )
    return {"ok": True}


# ---- writes (Studio) ---------------------------------------------------

_VALID_PLATFORMS = {"youtube", "tiktok", "instagram", "facebook", "x", "threads"}


class NewPage(BaseModel):
    name: str
    language: str = "vi"
    creatorName: str | None = None
    channelUrl: str | None = None
    accountEmail: str | None = None   # owning email/handle for all selected platforms
    platforms: list[str] = []         # subset of youtube|tiktok|instagram|facebook
    # legacy compat — if old callers pass youtubeAccount, treat as accountEmail + youtube
    youtubeAccount: str | None = None


@app.post("/api/pages")
def create_page(body: NewPage):
    # Normalise legacy youtubeAccount field
    email = body.accountEmail or body.youtubeAccount or None
    plats = [p for p in body.platforms if p in _VALID_PLATFORMS]
    if not plats and body.youtubeAccount:
        plats = ["youtube"]

    with get_conn() as conn:
        try:
            row = conn.execute(
                """
                INSERT INTO pages (name, language, status, creator_name, channel_url)
                VALUES (%s, %s, 'active', %s, %s)
                RETURNING id
                """,
                (body.name, body.language, body.creatorName, body.channelUrl),
            ).fetchone()
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, f"A page named '{body.name}' already exists.")
        page_id = row["id"]
        for plat in plats:
            # Seed credentials_ref to the conventional token path so the account
            # becomes visible/uploadable the moment the OAuth token file exists on disk.
            # Path only — never a secret (borrowed-account rule).
            conn.execute(
                """
                INSERT INTO platform_accounts
                    (page_id, platform, account_label, account_type, status, approval, credentials_ref)
                VALUES (%s, %s, %s, 'personal', 'active', 'not_started', %s)
                ON CONFLICT (page_id, platform) DO NOTHING
                """,
                (page_id, plat, email, _conventional_creds_ref(body.name, plat)),
            )
    return {"id": page_id, "name": body.name}


class NewJob(BaseModel):
    pageId: int
    link: str
    title: str | None = None        # OUTPUT title for the NEW video (applied when the job runs)
    voice: str | None = None        # preset name or clone:<name>
    editMode: str | None = None     # commentary | recap | educational
    comment: str | None = None      # re-create instruction
    sourceVideoId: int | None = None
    reuseScriptVideoId: int | None = None  # PART B: reuse an existing video's saved
                                            # script (videos.script) — the runner BYPASSES
                                            # script-gen (no claude -p) and feeds it to TTS.
    bypassTtsCache: bool = False     # force-fresh TTS: skip the per-scene TTS cache READ
                                     # (every scene re-synthesized) but keep the WRITE.
                                     # Used by "Dùng lại kịch bản" (reuse script, fresh voice).
    bypassScriptCache: bool = False  # force-fresh script-gen: skip per-batch cache READ
    aspect: str | None = None        # 9:16 | 16:9 | 1:1 | 4:5
    targetSec: int | None = None     # target OUTPUT length (whole source condensed into this)
    addCredit: bool = True           # append the source-credit slate at the end?
    renderModel: str | None = None   # render/animation engine key (see Studio model dropdown)
    voiceCloneModel: str | None = None  # voice-clone engine key (see Studio clone-model dropdown)
    llmProvider: str | None = None   # TEXT script-gen backend (see GET /api/llm/models).
                                     # NULL = 'claude-cli' — the subscription path, the default,
                                     # and the only behavior that existed before the provider gate.
    llmModel: str | None = None      # model id within that provider; NULL = its own default
    srcAudioVolume: float = 0.0      # original/source-audio volume in the final mix (0 = off, voiceover only)
    publish: bool = False            # opt-in: auto-publish when the job finishes
    publishPlatform: str | None = None  # when publish=true, auto-publish ONLY to this platform's connected
                                         # channel(s) of the job's own page. One of youtube|tiktok|instagram|
                                         # facebook, or null (null ⇒ no platform chosen ⇒ no auto-publish).
    useCover: bool = False               # when true, apply coverImagePath as the video poster/thumbnail
                                         # INSTEAD of an extracted frame.
    coverImagePath: str | None = None    # abs path to an SDXL cover (from POST /generate/cover). Persisted
                                         # to jobs.cover_image_path ONLY when useCover is true.
    facebookTags: str | None = None      # owner-edited Facebook hashtag block ("#a #b ..."), generated via
                                         # POST /generate/tags. Persisted to jobs.facebook_tags and copied
                                         # onto the video row by the runner for copy-at-upload-time.


def _insert_job(body: NewJob, status: str = "queued") -> dict:
    """Validate + INSERT a single production job row and return {"id", "status"}.
    This is the ONE authoritative create path — both the single POST /api/jobs route
    and the batch endpoint call it, so the validation and the INSERT never diverge.

    `status` defaults to 'queued' (the normal single-create path: the runner claims
    it immediately). The batch/source-list endpoint passes status='held' so the rows
    are only PERSISTED — the runner's _claim_job selects only 'queued', so 'held'
    jobs sit until POST /api/jobs/release flips them to 'queued'.
    """
    # Clamp source-audio volume to [0, 1] (UI offers 0.0 / 0.05 / 0.10 / 0.15;
    # accept anything but never let it leave the valid mix range).
    src_audio_volume = min(1.0, max(0.0, body.srcAudioVolume))
    # publish_platform: only meaningful when publish=true. Normalize to a known
    # platform string or NULL. An unknown value is rejected (422) so a typo never
    # silently becomes "no platform" (= no auto-publish) and confuses the owner.
    publish_platform = (body.publishPlatform or "").strip().lower() or None
    if publish_platform is not None and publish_platform not in PUBLISH_PLATFORM_CHOICES:
        raise HTTPException(
            422,
            f"publishPlatform must be one of {sorted(PUBLISH_PLATFORM_CHOICES)} or null",
        )
    # PART B — script reuse. When set, the runner BYPASSES script-gen and loads this
    # video's saved videos.script. Validate the video EXISTS (404 if not) so a stale
    # id never silently produces a job that fails deep in the runner. (Cross
    # render-mode reuse is allowed with a frontend warning — not blocked here.)
    reuse_script_video_id = body.reuseScriptVideoId
    if reuse_script_video_id is not None:
        with get_conn() as conn:
            v = conn.execute(
                "SELECT id FROM videos WHERE id = %s", (reuse_script_video_id,)
            ).fetchone()
        # Accept either a surviving DB video OR an orphaned render-cache manifest
        # (deleted-with-keepScript scripts are reusable script-only — the runner's
        # _load_reusable_script falls back to load_manifest). Reject only when NEITHER
        # exists, so a truly stale id still fails fast at creation instead of deep in
        # the runner.
        if not v and not render_cache.has_cached_render(reuse_script_video_id):
            raise HTTPException(404, f"reuseScriptVideoId {reuse_script_video_id}: video not found")
    # Cover: only persist the cover path when the owner opted in (useCover). A path
    # without useCover is ignored (the runner extracts a frame as usual).
    cover_image_path = (body.coverImagePath or "").strip() or None if body.useCover else None
    with get_conn() as conn:
        row = conn.execute(
            """
            INSERT INTO jobs (page_id, page_seq, input_type, input_payload, status,
                              voice, edit_mode, comment, source_video_id,
                              aspect, target_sec, add_credit, render_model, voice_clone_model,
                              src_audio_volume, publish, title, publish_platform,
                              reuse_script_video_id, bypass_tts_cache, bypass_script_cache,
                              cover_image_path, facebook_tags, llm_provider, llm_model)
            VALUES (%s,
                    (SELECT COALESCE(MAX(page_seq), 0) + 1 FROM jobs WHERE page_id = %s),
                    'link', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, page_seq
            """,
            (body.pageId, body.pageId, body.link, status, body.voice, body.editMode, body.comment, body.sourceVideoId,
             body.aspect, body.targetSec, body.addCredit, body.renderModel, body.voiceCloneModel,
             src_audio_volume, body.publish, (body.title or "").strip() or None, publish_platform,
             reuse_script_video_id, body.bypassTtsCache, body.bypassScriptCache,
             cover_image_path, (body.facebookTags or "").strip() or None,
             # Empty string -> NULL so "no choice" is stored as NULL, never as "", and the
             # runner's `or None` normalization has nothing to paper over.
             (body.llmProvider or "").strip().lower() or None,
             (body.llmModel or "").strip() or None),
        ).fetchone()
    return {"id": row["id"], "status": status, "pageSeq": row["page_seq"]}


@app.post("/api/jobs")
def create_job(body: NewJob):
    """Enqueue a production job. The pipeline (ingest → assemble) picks it up."""
    return _insert_job(body)


# ---- BATCH ("Add List"): preview many links, then create one job per link ----

# Bound the batch so a paste of hundreds of links can't tie up the probe/translate
# path (each item probes the source + runs a Claude-headless translation).
BATCH_MAX_LINKS = int(os.getenv("BATCH_MAX_LINKS", "30"))


class BatchPreviewRequest(BaseModel):
    links: list[str] = []


class BatchItem(BaseModel):
    link: str
    title: str | None = None   # FINAL output title (FE may have edited the VN suggestion)


class BatchCreate(BaseModel):
    pageId: int
    items: list[BatchItem] = []
    # Shared settings applied to EVERY job in the batch (same knobs as NewJob).
    editMode: str | None = None
    voice: str | None = None
    aspect: str | None = None
    renderModel: str | None = None
    voiceCloneModel: str | None = None
    llmProvider: str | None = None   # NULL = claude-cli (see NewJob)
    llmModel: str | None = None
    srcAudioVolume: float = 0.0
    addCredit: bool = True
    useCover: bool = False
    coverImagePath: str | None = None


@app.post("/api/jobs/batch/preview")
def batch_preview(body: BatchPreviewRequest):
    """SIDE-EFFECT-FREE: for each link, probe its ORIGINAL title (same probe path as
    POST /generate/probe_link). Creates NO jobs and does NOT call Claude — the owner
    types the Vietnamese title manually in the modal, so `viTitle` is returned empty
    (no auto-translation, saves subscription usage).

    Order is preserved. One bad link yields {link, error} and does NOT abort the
    batch. Capped at BATCH_MAX_LINKS to bound the probe work.
    """
    links = [l.strip() for l in (body.links or []) if l and l.strip()]
    if not links:
        raise HTTPException(422, "Chưa có link nào để xem trước.")
    if len(links) > BATCH_MAX_LINKS:
        raise HTTPException(
            422,
            f"Quá nhiều link ({len(links)}). Tối đa {BATCH_MAX_LINKS} link mỗi lần.",
        )

    results = []
    for link in links:
        try:
            probed = generate._run_cf_worker("probe_worker.py", {"link": link}, timeout=90)
            original_title = (probed or {}).get("title") or ""
            if not original_title:
                results.append({"link": link, "error": "Không lấy được tiêu đề nguồn."})
                continue
            # No auto-translation: the owner fills the Vietnamese title by hand in
            # the modal. Return an empty viTitle (probe-only, no Claude call).
            results.append({
                "link": link,
                "originalTitle": original_title,
                "viTitle": "",
            })
        except HTTPException as e:
            results.append({"link": link, "error": str(e.detail)})
        except Exception as e:  # noqa: BLE001 — one bad link never fails the batch
            results.append({"link": link, "error": str(e)[:300] or "Probe thất bại."})
    return {"results": results}


@app.post("/api/jobs/batch")
def batch_create(body: BatchCreate):
    """Create one HELD job per item (sequentially), reusing the exact single-job
    insert path (_insert_job). Titles are FINAL (already edited by the FE). Shared
    settings apply to every job. Order preserved; per-item errors are non-fatal.

    The jobs are PERSISTED with status='held' — the runner's _claim_job selects only
    'queued', so these rows do NOT start rendering on save. They flush into the queue
    when the owner clicks the main "Tạo video" button, which calls POST
    /api/jobs/release (flips this page's 'held' rows → 'queued'). Nothing is run
    inline here.
    """
    if not body.items:
        raise HTTPException(422, "Chưa có mục nào để tạo.")
    if len(body.items) > BATCH_MAX_LINKS:
        raise HTTPException(
            422,
            f"Quá nhiều mục ({len(body.items)}). Tối đa {BATCH_MAX_LINKS} job mỗi lần.",
        )

    results = []
    for item in body.items:
        link = (item.link or "").strip()
        if not link:
            results.append({"link": item.link or "", "error": "Thiếu link."})
            continue
        try:
            job = _insert_job(NewJob(
                pageId=body.pageId,
                link=link,
                title=item.title,
                editMode=body.editMode,
                voice=body.voice,
                aspect=body.aspect,
                renderModel=body.renderModel,
                voiceCloneModel=body.voiceCloneModel,
                llmProvider=body.llmProvider,
                llmModel=body.llmModel,
                srcAudioVolume=body.srcAudioVolume,
                addCredit=body.addCredit,
                useCover=body.useCover,
                coverImagePath=body.coverImagePath,
            ), status="held")
            results.append({"link": link, "jobId": job["id"]})
        except HTTPException as e:
            results.append({"link": link, "error": str(e.detail)})
        except Exception as e:  # noqa: BLE001 — one bad item never fails the batch
            results.append({"link": link, "error": str(e)[:300] or "Tạo job thất bại."})
    return {"results": results}


class ReleaseJobsRequest(BaseModel):
    pageId: int


@app.post("/api/jobs/release")
def release_jobs(body: ReleaseJobsRequest):
    """Flush a page's HELD jobs into the queue.

    The source-list "Save" (POST /api/jobs/batch) persists jobs with status='held'
    so they do NOT auto-run. When the owner clicks the main "Tạo video" button the
    frontend calls this to flip ALL of that page's 'held' jobs to 'queued', so the
    in-process runner (which claims only 'queued') picks them up. Returns the count
    released. A page with no held jobs releases 0 (not an error).
    """
    # Guard pageId: must reference an existing page (404 otherwise), so a stale/bad
    # id can't silently no-op and hide a frontend bug.
    with get_conn() as conn:
        page = conn.execute("SELECT id FROM pages WHERE id = %s", (body.pageId,)).fetchone()
        if not page:
            raise HTTPException(404, f"Page {body.pageId} not found")
        rows = conn.execute(
            "UPDATE jobs SET status = 'queued'"
            " WHERE page_id = %s AND status = 'held' RETURNING id",
            (body.pageId,),
        ).fetchall()
    return {"released": len(rows)}


# ---- PART B: reusable-script picker + script preview ------------------------

# RENDER_CHECKPOINTS (the set of SDXL checkpoint keys, imported at the top) lets us
# mirror runner.py's render-mode derivation so the picker reports the SAME mode the
# runner used.
def _derive_render_mode(render_model: str | None, render_mode: str | None) -> str | None:
    """Derive a video's render mode the SAME way runner._process_job does.

    jobs.render_mode is now AUTHORITATIVE (set by the Studio at job creation;
    pages.architecture_type/config were dropped in the 2026-06-25 redesign), so
    prefer it when present. Otherwise fall back to the render_model rule for legacy
    rows: passthrough-trim → 'footage'; stickman-* → 'stickman'; an SDXL checkpoint
    key → 'image'. Returns None only when nothing at all is known."""
    if render_mode:
        return render_mode
    rm = (render_model or "").strip()
    if rm == "passthrough-trim":
        return "footage"
    if rm.startswith("stickman"):
        return "stickman"
    if rm in RENDER_CHECKPOINTS:
        return "image"
    return None


def _norm_narr(text: str | None) -> str:
    """Normalize a scene narration for stale-vs-cached comparison (whitespace-
    collapsed, stripped). Two narrations that differ only in surrounding/inner
    whitespace are considered the SAME line."""
    return " ".join((text or "").split()).strip()


def _manifest_scene_wavs_present(video_id: int, manifest: dict) -> bool:
    """True iff EVERY scene in the manifest has its cached wav present on disk under
    _cache/renders/<video_id>/. This is exactly what the reuse-with-audio path
    (load_manifest → assemble) consumes, so it's the honest 'audioCached' signal.
    Empty manifest scenes → False."""
    scenes = manifest.get("scenes") if isinstance(manifest, dict) else None
    if not isinstance(scenes, list) or not scenes:
        return False
    rdir = render_cache.render_dir(video_id)
    for sc in scenes:
        if not isinstance(sc, dict):
            return False
        aname = sc.get("audio") or f"scene{int(sc.get('scene', 0)):03d}.wav"
        ap = os.path.join(rdir, aname)
        try:
            if not (os.path.isfile(ap) and os.path.getsize(ap) > 0):
                return False
        except OSError:
            return False
    return True


def _audio_status_for_script(video_id: int, db_scenes) -> tuple[bool, bool]:
    """Compute (audioCached, audioStale) for a DB video's reusable script.

    - audioCached: the manifest's per-scene wavs are ALL present on disk (that's
      what the reuse-with-audio flow reads).
    - audioStale: audio exists BUT the current videos.script narrations no longer
      match the narrations snapshotted in the manifest (script edited after the
      audio was generated). Compared per-scene, whitespace-normalized. If the scene
      COUNT differs, or any narration differs, it's stale. Fixes the cross-session
      'edited but no warning' bug.
    Returns (False, False) when no manifest / no cached audio."""
    # Read the manifest raw (no per-scene file-existence validation) so an edited
    # script whose wavs are still present can be flagged stale; we check wav
    # presence explicitly below via _manifest_scene_wavs_present.
    raw = _read_manifest_raw(video_id)
    if not raw:
        return False, False
    audio_cached = _manifest_scene_wavs_present(video_id, raw)
    if not audio_cached:
        return False, False
    # Staleness: compare current DB script narrations vs manifest snapshot.
    m_scenes = raw.get("scenes") or []
    db_list = db_scenes if isinstance(db_scenes, list) else []
    if len(m_scenes) != len(db_list):
        return True, True
    for m, d in zip(m_scenes, db_list):
        m_narr = _norm_narr(m.get("narration") if isinstance(m, dict) else None)
        d_narr = _norm_narr(d.get("narration") if isinstance(d, dict) else None)
        if m_narr != d_narr:
            return True, True
    return True, False


def _read_manifest_raw(video_id: int) -> dict | None:
    """Read manifest.json for a video_id WITHOUT the per-scene file-existence
    validation load_manifest does (so an edited script with partially-missing wavs
    can still be inspected). Returns the parsed dict or None. Path-guarded, never
    raises."""
    if not render_cache.render_cache_enabled():
        return None
    try:
        mp = render_cache.manifest_path(video_id)
    except ValueError:
        return None
    try:
        if not (os.path.isfile(mp) and os.path.getsize(mp) > 0):
            return None
        with open(mp, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _orphaned_manifest_items(page_id: int, db_video_ids: set[int]) -> list[dict]:
    """Scan _cache/renders/*/manifest.json for manifests whose videoId has NO
    surviving videos row for THIS page, and map each to the reusable-scripts item
    shape with source='manifest'. The reuse id is the manifest's videoId.

    A manifest carries `page` (name), not page_id, so we match by the page's NAME.
    Best-effort: unreadable/mismatched manifests are skipped. Returns newest-first
    is not guaranteed (no timestamp in the manifest) — appended after DB rows."""
    if not render_cache.render_cache_enabled():
        return []
    with get_conn() as conn:
        prow = conn.execute("SELECT name FROM pages WHERE id = %s", (page_id,)).fetchone()
    if not prow:
        return []
    page_name = prow["name"]
    root = render_cache.renders_root()
    if not os.path.isdir(root):
        return []
    out: list[dict] = []
    for entry in os.listdir(root):
        sub = os.path.join(root, entry)
        if not os.path.isdir(sub):
            continue
        try:
            vid = int(entry)
        except ValueError:
            continue
        if vid in db_video_ids:
            continue  # a surviving DB row already covers this id
        data = _read_manifest_raw(vid)
        if not data:
            continue
        # Only manifests belonging to THIS page (matched by page name).
        if (data.get("page") or None) != page_name:
            continue
        scenes = data.get("scenes") if isinstance(data.get("scenes"), list) else []
        first_narr = None
        if scenes and isinstance(scenes[0], dict):
            first_narr = scenes[0].get("narration")
        preview = (first_narr[:120] + "…") if (first_narr and len(first_narr) > 120) else first_narr
        out.append({
            "videoId": int(data.get("videoId") or vid),
            "title": data.get("title"),
            "sourceLink": data.get("sourceLink"),
            "sourceName": data.get("sourceName"),
            "renderMode": data.get("renderMode"),
            "editMode": None,
            "sceneCount": len(scenes),
            "preview": preview,
            "createdAt": None,
            "audioCached": False,
            "audioStale": False,
            "source": "manifest",
        })
    return out


@app.get("/api/pages/{page_id}/reusable-scripts")
def reusable_scripts(page_id: int, link: str | None = None):
    """List a page's videos that have a saved script reusable by a new job.

    Each item (frontend depends on this EXACT shape):
      { videoId, title, sourceLink, sourceName, renderMode, editMode, sceneCount,
        preview, createdAt }

    Only videos whose script IS NOT NULL and has >0 scenes, for this page_id, newest
    first. renderMode is derived from the originating job's render_model exactly like
    runner.py (cross render-mode reuse is allowed; the WARNING is a frontend concern).

    `link` (optional): when provided we FILTER to videos whose source_link matches the
    given link (exact match), so the picker can prefer scripts produced from the same
    source. When omitted (or no match exists) the frontend may show all page scripts;
    we chose to FILTER server-side when link is given (simpler client, honest contract).
    404 if the page does not exist.
    """
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM pages WHERE id = %s", (page_id,)).fetchone():
            raise HTTPException(404, f"page {page_id} not found")
        params: list = [page_id]
        link_clause = ""
        norm_link = (link or "").strip()
        if norm_link:
            link_clause = " AND v.source_link = %s"
            params.append(norm_link)
        rows = conn.execute(
            f"""
            SELECT v.id, v.title, v.source_link, v.source_name, v.created_at,
                   v.script,
                   CASE WHEN jsonb_typeof(v.script) = 'array'
                        THEN jsonb_array_length(v.script) ELSE 0 END AS scene_count,
                   v.script->0->>'narration' AS first_narration,
                   j.render_model, j.render_mode, j.edit_mode
              FROM videos v
              LEFT JOIN jobs j  ON j.id = v.job_id
             WHERE v.page_id = %s
               AND v.script IS NOT NULL
               -- Only array-shaped scripts are reusable. Dubbed-mode scripts are JSON
               -- objects (not scene arrays), so we EXCLUDE them via jsonb_typeof='array'.
               -- jsonb_array_length must NOT be called bare on v.script: Postgres does not
               -- guarantee the typeof filter runs (nor the AND short-circuits) before the
               -- length expression, so a dubbed object row would raise
               -- "cannot get array length of a non-array" and 500 the query. Wrap every
               -- jsonb_array_length in a CASE typeof guard (same pattern as fetch_videos).
               AND jsonb_typeof(v.script) = 'array'
               AND (CASE WHEN jsonb_typeof(v.script) = 'array'
                         THEN jsonb_array_length(v.script) ELSE 0 END) > 0
               {link_clause}
             ORDER BY v.created_at DESC, v.id DESC
            """,
            tuple(params),
        ).fetchall()
    out = []
    db_video_ids: set[int] = set()
    for r in rows:
        db_video_ids.add(r["id"])
        narr = r["first_narration"]
        preview = (narr[:120] + "…") if (narr and len(narr) > 120) else narr
        audio_cached, audio_stale = _audio_status_for_script(r["id"], r["script"])
        out.append(
            {
                "videoId": r["id"],
                "title": r["title"],
                "sourceLink": r["source_link"],
                "sourceName": r["source_name"],
                "renderMode": _derive_render_mode(r["render_model"], r["render_mode"]),
                "editMode": r["edit_mode"],
                "sceneCount": r["scene_count"],
                "preview": preview,
                "createdAt": _iso(r["created_at"]),
                "audioCached": audio_cached,
                "audioStale": audio_stale,
                "source": "db",
            }
        )
    # UNION in orphaned manifests: cached scripts whose DB video row is gone but the
    # manifest survives (e.g. deleted with keepScript=true). These are script-only.
    out.extend(_orphaned_manifest_items(page_id, db_video_ids))
    return out


@app.get("/api/videos/{video_id}/script")
def video_script(video_id: int):
    """Full saved script of a video, for the reuse-picker / script-preview.

    videos.script is stored in TWO shapes and this endpoint returns BOTH via a
    `kind` discriminator so the frontend can render each correctly:

      - Scene-array jobs (image/footage/stickman): script is a JSON ARRAY of scene
        objects → kind:"scenes", plus sceneCount + scenes (the array).
      - Dubbed jobs (edit_mode='dubbed'): script is a JSON OBJECT
        {mode:'dubbed', subs:[{start,end,text_vi}], filler:[]} → kind:"dubbed",
        plus subCount + subs (and `mode` passed through).

    Common fields on every response: videoId, title, renderMode, editMode, kind.

    404 ONLY when the video is missing, or the script is NULL / an empty array /
    an unrecognized-or-empty object with no `subs`. A dict without a non-empty
    `subs` list has nothing to show → 404."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT v.id, v.title, v.script,
                   j.render_model, j.render_mode, j.edit_mode
              FROM videos v
              LEFT JOIN jobs j ON j.id = v.job_id
             WHERE v.id = %s
            """,
            (video_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Video not found")
    script = row["script"]
    common = {
        "videoId": row["id"],
        "title": row["title"],
        "renderMode": _derive_render_mode(row["render_model"], row["render_mode"]),
        "editMode": row["edit_mode"],
    }
    # Scene-array shape (image/footage/stickman).
    if isinstance(script, list) and script:
        return {**common, "kind": "scenes", "sceneCount": len(script), "scenes": script}
    # Dubbed shape: {mode:'dubbed', subs:[...], filler:[...]}. Only show it when it
    # actually carries a non-empty subs list — an empty/degenerate object has nothing
    # to preview.
    if isinstance(script, dict):
        subs = script.get("subs")
        if isinstance(subs, list) and subs:
            out = {**common, "kind": "dubbed", "subCount": len(subs), "subs": subs}
            if script.get("mode") is not None:
                out["mode"] = script.get("mode")
            return out
    raise HTTPException(404, "Video has no script to preview")


@app.get("/api/videos/{video_id}/scenes/{scene_num}/audio")
def scene_audio(video_id: int, scene_num: int):
    """Serve the cached WAV for one scene (sceneNNN.wav under the render-cache dir).

    Used by the script-preview modal play button so the browser plays the real
    F5-TTS audio instead of browser TTS.  Returns 404 when the render cache is
    absent or disabled."""
    if not render_cache.render_cache_enabled():
        raise HTTPException(404, "Render cache disabled")
    wav_name = f"scene{scene_num:03d}.wav"
    wav_path = os.path.join(render_cache.render_dir(video_id), wav_name)
    if not os.path.isfile(wav_path):
        raise HTTPException(404, f"{wav_name} not in render cache for video {video_id}")
    return FileResponse(wav_path, media_type="audio/wav")


@app.delete("/api/videos/{video_id}/audio")
def delete_video_audio(video_id: int):
    """Delete all cached `.wav` files for a video so TTS regenerates them on the
    next render. Keeps manifest.json and visual files (.mp4/.jpg/.png/...) intact.

    Best-effort: failures to remove individual files are skipped silently."""
    if not render_cache.render_cache_enabled():
        raise HTTPException(404, "Render cache disabled")
    rdir = render_cache.render_dir(video_id)
    if not os.path.isdir(rdir):
        raise HTTPException(404, f"No render cache for video {video_id}")
    deleted: list[str] = []
    for fname in os.listdir(rdir):
        if not fname.lower().endswith(".wav"):
            continue
        fp = os.path.join(rdir, fname)
        try:
            os.remove(fp)
            deleted.append(fname)
        except OSError:
            # Best-effort cleanup; skip files we cannot remove.
            pass
    return {"deleted": deleted, "count": len(deleted)}


@app.delete("/api/videos/{video_id}/script")
def clear_video_script(video_id: int):
    """Clear a video's saved script (set videos.script = NULL).

    The video row and all files are kept intact. After this call the video
    no longer appears in the reusable-scripts picker (which filters on
    script IS NOT NULL). 404 if the video does not exist.
    """
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM videos WHERE id = %s", (video_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Video {video_id} not found")
        conn.execute("UPDATE videos SET script = NULL WHERE id = %s", (video_id,))
    return {"ok": True, "id": video_id}


class SetCoverBody(BaseModel):
    path: str  # abs path of the image to use as this video's cover/thumbnail


@app.post("/api/videos/{video_id}/cover")
def set_video_cover(video_id: int, body: SetCoverBody):
    """Change a produced video's cover/thumbnail (videos.thumb_path only).

    Body: { path }. Sets videos.thumb_path to `path` and returns
      { ok: True, thumbUrl: "/media?path=<urlencoded path>" }
    using the same media() shape fetch_videos exposes.

    This is a display/metadata change ONLY — the mp4 is NOT re-encoded/re-muxed,
    and the OLD thumb file is NOT deleted (it may be shared or the owner may
    revert). The frontend picks an image (typically a saved cover) and calls this.

    Validation:
      - 404 if the video does not exist.
      - 404 if the file at `path` does not exist on disk.
      - 403 if the resolved realpath is outside CONTENT_OUTPUT_ROOT
        (path-traversal guard — same _covers_tree_guard used by /media and the
        cover-save endpoint). Any image inside that tree is accepted.
    """
    # Path-traversal guard first (403 before touching the DB); returns the realpath.
    full = _covers_tree_guard(body.path)
    if not os.path.isfile(full):
        raise HTTPException(404, "File not found")
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM videos WHERE id = %s", (video_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Video {video_id} not found")
        # Store the caller-supplied path (same value the client passed / that the
        # saved-covers list returns). Single UPDATE, no file/mp4 side effects.
        conn.execute(
            "UPDATE videos SET thumb_path = %s WHERE id = %s", (body.path, video_id)
        )
    return {"ok": True, "thumbUrl": f"/media?path={quote(body.path)}"}


class UpdateSceneBody(BaseModel):
    narration: str


@app.patch("/api/videos/{video_id}/script/scene/{scene_num}")
def update_scene_narration(video_id: int, scene_num: int, body: UpdateSceneBody):
    """Edit the narration text of one scene in `videos.script` (JSONB list).

    Also mirrors the change into the render-cache manifest (narration + caption)
    so a subsequent render picks up the new text. The manifest write is
    best-effort and never fails the request once the DB is updated."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT script FROM videos WHERE id = %s", (video_id,))
            row = cur.fetchone()
            # get_conn() uses row_factory=dict_row, so `row` is a dict keyed by
            # column name (e.g. {"script": [...]}), NOT a positional tuple.
            # Indexing row[0] raises KeyError(0) → 500 → the Studio shows
            # "Không lưu được nội dung cảnh". Read by column name instead.
            if row is None or row["script"] is None:
                raise HTTPException(404, f"No script for video {video_id}")
            scenes = row["script"]
            # Per-scene narration edit only applies to scene-array scripts. A Dubbed
            # video stores script as a {mode,subs,filler} OBJECT; iterating it would
            # walk its string keys and crash (AttributeError → 500). Fail cleanly so
            # the UI (which should not offer per-scene edit for dubbed) gets a clear
            # 400 instead of a server error.
            if not isinstance(scenes, list):
                raise HTTPException(
                    400,
                    "Chỉnh sửa lời thoại theo cảnh không áp dụng cho video lồng tiếng (dubbed).",
                )
            target = None
            for sc in scenes:
                if sc.get("scene") == scene_num:
                    target = sc
                    break
            if target is None:
                raise HTTPException(404, f"Scene {scene_num} not found in video {video_id}")
            target["narration"] = body.narration
            cur.execute(
                "UPDATE videos SET script = %s WHERE id = %s",
                (Json(scenes), video_id),
            )

    # Best-effort manifest sync (never raises): keep cached render in step.
    try:
        if render_cache.render_cache_enabled():
            mpath = render_cache.manifest_path(video_id)
            if os.path.isfile(mpath):
                with open(mpath, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                for sc in manifest.get("scenes", []):
                    if sc.get("scene") == scene_num:
                        sc["narration"] = body.narration
                        sc["caption"] = body.narration
                        break
                tmp = mpath + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(manifest, f, ensure_ascii=False, indent=2)
                os.replace(tmp, mpath)
    except Exception:
        # DB is already committed; manifest mirror is non-critical.
        pass

    return {"videoId": video_id, "scene": scene_num, "narration": body.narration}


# ---- System resource monitor (shown live in the Workflow block) -------------

_STEP_MODEL = {
    "ingest": "yt-dlp + faster-whisper",
    "script": "Claude Code (headless)",
    "voice": "VieNeu-TTS",
    "footage": "yt-dlp / SDXL",
    "cut": "FFmpeg",
    "render": "FFmpeg",
    "publish": "YouTube upload",
}


def _resolve_active_model(step: str | None, voice_clone_model: str | None,
                          render_model: str | None) -> str | None:
    """Which model the running job ACTUALLY uses for `step`.

    The plain `_STEP_MODEL` map is a per-step default; for steps whose model is
    chosen per-job it would lie (e.g. it always says "VieNeu-TTS" even when the
    job picked F5-TTS). Resolve those from the job's own config so the live
    "Model" chip matches what was selected in Studio.
    """
    if not step:
        return None
    if step == "voice":
        # F5-TTS is the project default. Only show VieNeu when explicitly set to "vieneu".
        return "VieNeu-TTS" if (voice_clone_model or "").strip().lower() == "vieneu" else "F5-TTS"
    if step in ("footage", "image"):
        rm = (render_model or "").strip().lower()
        if rm.startswith("stickman"):
            return "Stickman"
        if rm == "passthrough-trim":
            return "yt-dlp (footage)"
        if rm:  # an SDXL checkpoint key
            return "SDXL"
        return _STEP_MODEL.get(step)
    return _STEP_MODEL.get(step)


class _PROCESSENTRY32(ctypes.Structure):
    """Subset of PROCESSENTRY32W we read from a Toolhelp snapshot."""
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _process_snapshot() -> tuple[dict[int, int], dict[int, str]]:
    """Whole-system (PID -> parent PID, PID -> exe name) via the Win32 Toolhelp
    snapshot (CreateToolhelp32Snapshot). No psutil and no `wmic` — the latter is
    removed on recent Windows 11 builds, which is why we use the API directly.

    Used to expand a tracked worker PID into its whole subtree, so a worker that
    re-spawns children (an ffmpeg resample, a separate-python F5 helper, a CUDA
    helper) is counted in the task footprint.
    """
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID = ctypes.c_void_p(-1).value
    k = ctypes.windll.kernel32
    ppid: dict[int, int] = {}
    names: dict[int, str] = {}
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID:
        return ppid, names
    try:
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        if not k.Process32FirstW(snap, ctypes.byref(entry)):
            return ppid, names
        while True:
            pid = int(entry.th32ProcessID)
            ppid[pid] = int(entry.th32ParentProcessID)
            names[pid] = entry.szExeFile or ""
            if not k.Process32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        k.CloseHandle(snap)
    return ppid, names


def _ppid_map() -> dict[int, int]:
    return _process_snapshot()[0]


def _descendants(roots: set[int], ppid: dict[int, int]) -> set[int]:
    """All PIDs in `roots` plus every transitive child, using a PID->parent map."""
    if not roots:
        return set()
    # Invert to parent -> [children] for a quick BFS.
    children: dict[int, list[int]] = {}
    for pid, parent in ppid.items():
        children.setdefault(parent, []).append(pid)
    seen = set(roots)
    stack = list(roots)
    while stack:
        cur = stack.pop()
        for child in children.get(cur, []):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


def _rss_for_pids(pids: set[int]) -> int | None:
    """Sum the working-set (RSS) of the given PIDs, in bytes, via tasklist.

    tasklist reports "Mem Usage" (private working set) per PID — no psutil
    needed. Returns None if nothing could be read for any PID.
    """
    if not pids:
        return None
    total = 0
    found = False
    for pid in pids:
        try:
            # errors="replace": a process image name may carry bytes the locale
            # codec can't decode; never let that raise and drop the whole stat.
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, encoding="utf-8", errors="replace", timeout=8,
            ).stdout.strip()
        except Exception:
            continue
        if not out or out.upper().startswith("INFO:"):
            continue  # "INFO: No tasks ..." — the PID already exited
        # CSV row: "image","PID","Session","Session#","Mem Usage"
        cols = [c.strip().strip('"') for c in out.splitlines()[0].split('","')]
        if len(cols) < 5:
            continue
        mem = cols[-1].replace("\xa0", " ")  # e.g. "1,234,567 K"
        digits = "".join(ch for ch in mem if ch.isdigit())
        if not digits:
            continue
        total += int(digits) * 1024  # the value is in KB
        found = True
    return total if found else None


def _ram_stats(pids: set[int]) -> dict:
    """RAM consumed by the ACTIVE TASK: summed RSS of the tracked worker PID
    subtree. Null when idle (no worker running)."""
    if not pids:
        return {"taskMB": None}
    rss = _rss_for_pids(pids)
    return {"taskMB": (rss // (1024 * 1024)) if rss is not None else None}


# pid → kernel+user ticks (100-ns units) from the previous poll
_cpu_prev_samples: dict[int, int] = {}
# GetSystemTimes ker+usr snapshot (sum across all logical CPUs) — used as the
# denominator so the task % is bounded by the real system CPU, not wall×n_cores.
_cpu_prev_sys_ticks: list[int | None] = [None]
_cpu_prev_lock = threading.Lock()


def _cpu_percent(pids: set[int]) -> int | None:
    """Task-scoped CPU % using GetSystemTimes + GetProcessTimes deltas.

    Denominator = Δ(kernel+user) from GetSystemTimes (summed across all logical
    CPUs by Windows) — the real total CPU budget for the interval.

        % = Σ Δprocess_ticks / Δsystem_ticks × 100

    This prevents the chip from ever exceeding the actual system-wide CPU %,
    even when os.cpu_count() under-reports or PID-reuse inflates the task tree.
    """
    k32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    if not pids:
        with _cpu_prev_lock:
            _cpu_prev_samples.clear()
            _cpu_prev_sys_ticks[0] = None
        return None

    # IMPORTANT — sampling ORDER fixes the historical "chip > Task Manager" skew.
    # The numerator (Σ Δ per-process ticks) is read endpoint-to-endpoint across
    # this GetProcessTimes loop; the denominator (Δ GetSystemTimes) MUST span the
    # SAME interval. If GetSystemTimes were sampled BEFORE the loop, the stored
    # "prev" system endpoint would mark the loop START while the process endpoints
    # mark the loop END — offsetting the two windows by the loop duration L. When
    # a poll's loop is slower than the prior poll's (L_t > L_{t-1}), the task
    # accrues ticks over a LONGER window than the denominator covers, so the ratio
    # inflates (measured: true 16% reads as 32-42% on slow polls) and clamps at
    # 100. Sampling system ticks AFTER the loop — and storing THAT as prev for the
    # next poll — aligns both endpoints, so the windows are identical-length every
    # poll. (Empirically verified: NEW reads a steady 16% vs OLD's 7-42% swing.)
    current: dict[int, int] = {}
    for pid in pids:
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            continue
        try:
            ct = ctypes.c_uint64()
            et = ctypes.c_uint64()
            kt = ctypes.c_uint64()
            ut = ctypes.c_uint64()
            if k32.GetProcessTimes(h,
                                   ctypes.byref(ct), ctypes.byref(et),
                                   ctypes.byref(kt), ctypes.byref(ut)):
                current[pid] = kt.value + ut.value
        finally:
            k32.CloseHandle(h)

    # System-wide CPU total: GetSystemTimes sums across ALL logical CPUs.
    # lpKernelTime includes idle time, so ker+usr covers the full CPU budget.
    # Sampled AFTER the per-PID loop so its endpoint aligns with the process
    # endpoints above (see the timing-window note before the loop).
    si_idle = ctypes.c_uint64()
    si_ker  = ctypes.c_uint64()
    si_usr  = ctypes.c_uint64()
    sys_now: int | None = None
    if k32.GetSystemTimes(ctypes.byref(si_idle), ctypes.byref(si_ker), ctypes.byref(si_usr)):
        sys_now = si_ker.value + si_usr.value

    with _cpu_prev_lock:
        prev     = dict(_cpu_prev_samples)
        prev_sys = _cpu_prev_sys_ticks[0]
        _cpu_prev_samples.clear()
        _cpu_prev_samples.update(current)
        _cpu_prev_sys_ticks[0] = sys_now

    if not prev or not current or prev_sys is None or sys_now is None:
        return None

    delta_sys = sys_now - prev_sys
    if delta_sys <= 0:
        return None

    delta_task = sum(
        ticks - prev[pid]
        for pid, ticks in current.items()
        if pid in prev and ticks >= prev[pid]
    )

    return max(0, min(100, round(delta_task / delta_sys * 100)))


def _gpu_util() -> int | None:
    """Whole-GPU utilization % (kept for the chip; not task-scoped)."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip().splitlines()[0]
        return int(out.strip())
    except Exception:
        return None


def _vram_stats(pids: set[int]) -> dict:
    """VRAM consumed by the ACTIVE TASK: nvidia-smi compute-apps filtered to the
    tracked worker PID subtree. gpuUtil stays whole-GPU.

    taskMB resolves to one of:
      - None  -> idle (no worker), no GPU, or per-process VRAM UNAVAILABLE.
      - int   -> MB used by the tracked worker subtree's GPU process(es).

    `note` carries the reason when taskMB is null but a worker IS active, so the
    monitor can show "n/a" instead of a misleading 0.

    LIMITATIONS (honest):
      1. Consumer GeForce GPUs in WDDM mode (this RTX 2070 Max-Q) report
         per-process `used_memory` as [N/A] — the driver/OS does not expose it.
         There is no nvidia-smi/ctypes way around this without TCC mode (not
         available on GeForce). In that case taskMB is null + note.
      2. SDXL image gen runs inside the separate, long-lived ComfyUI SERVER
         process, NOT a tracked cf-venv worker, so even when per-process VRAM is
         available its memory is not attributed here.
    """
    util = _gpu_util()
    if not pids:
        return {"taskMB": None, "gpuUtil": util, "note": None}
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return {"taskMB": None, "gpuUtil": util, "note": "nvidia-smi unavailable"}
    if not out:
        # GPU present, no compute process at all for the task.
        return {"taskMB": 0, "gpuUtil": util, "note": None}

    task_mb = 0
    matched = False          # a tracked PID appeared in compute-apps
    mem_unavailable = False   # but its used_memory was [N/A]
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            gpid = int(parts[0])
        except ValueError:
            continue
        if gpid not in pids:
            continue
        matched = True
        try:
            task_mb += int(parts[1])
        except ValueError:
            mem_unavailable = True  # "[N/A]" — per-process VRAM not exposed

    if matched and mem_unavailable:
        return {"taskMB": None, "gpuUtil": util,
                "note": "per-process VRAM not available on this GPU (GeForce/WDDM)"}
    if not matched:
        # No tracked PID is a GPU compute app. Could be a CPU-only step (e.g.
        # whisper on CPU) — report 0, not null.
        return {"taskMB": 0, "gpuUtil": util, "note": None}
    return {"taskMB": task_mb, "gpuUtil": util, "note": None}


@app.get("/api/llm/models")
def llm_models(refresh: bool = Query(False, description="bypass the 6h disk cache")):
    """The TEXT script-gen backends this machine can actually use right now.

    Shape:
      {"options": [{provider, model, label, is_default, reliability, notes}, ...],
       "generated_at": "<iso8601>", "cached": <bool>}

    Contract the Studio can rely on:
      * `claude-cli` (model null) is ALWAYS present and is the ONLY `is_default: true`
        entry — it is the existing subscription path and needs no key check.
      * A provider with no API key in the environment is simply ABSENT. It is never
        listed-but-broken, and a job that somehow still names it fails with a clear
        message rather than silently running on a different provider.
      * Every `openrouter` entry is `reliability: "low"`, unconditionally: Phase 1
        measured its free models burning their token budget on chain-of-thought and
        under-delivering. Present because the owner wants the option; never a default.

    Discovery hits Gemini's ListModels and OpenRouter's public /models, so the result is
    disk-cached for 6h (LLM_MODELS_CACHE_TTL_HOURS) — a dashboard page load must not cost
    a round of provider calls. `?refresh=1` forces a re-discovery.
    """
    return llm_gate.list_model_options(force_refresh=refresh)


@app.get("/api/system")
def system_stats():
    """Task-scoped resource usage + which model the running job is using.

    RAM/VRAM reflect what the ACTIVE generation worker (the cf-venv subprocess
    subtree) is consuming, NOT the whole machine. Idle -> nulls.
    """
    step = job_id = None
    row = None
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT id, progress_step, voice_clone_model, render_model "
                "FROM jobs WHERE status = 'running' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row:
            job_id, step = row["id"], row["progress_step"]
    except Exception:
        row = None

    # Expand tracked worker PIDs into their full subtree so re-spawned children
    # (ffmpeg resample, a separate-python F5 helper, CUDA helpers) are counted in
    # the task footprint. One Toolhelp snapshot serves both the RAM and VRAM scope.
    roots = active_worker_pids()
    pids = _descendants(roots, _ppid_map()) if roots else set()

    return {
        "ram": _ram_stats(pids),
        "vram": _vram_stats(pids),
        "cpu": {"percent": _cpu_percent(pids)},
        "activeJobId": job_id,
        "activeStep": step,
        "activeModel": _resolve_active_model(
            step,
            row["voice_clone_model"] if row else None,
            row["render_model"] if row else None,
        ),
        # Whether API publishing is currently enabled (env kill-switch). The FE polls
        # /api/system and uses this to hide/disable the "Đăng qua API" actions.
        "apiUploadEnabled": API_UPLOAD_ENABLED,
    }


# ---- Shared disk cleanup (one path-guarded remover for ALL delete buttons) ----
#
# Owner rule: "all delete buttons must be linked" → job-delete and video-delete go
# through ONE cleanup path. Safety is layered:
#   1. CONTENT_OUTPUT_ROOT guard: a file/dir is touched ONLY if its realpath is
#      strictly inside E:\ContentFactory. The root itself, the repo, and anything
#      outside are rejected.
#   2. Shared-dir guard: never touch the cross-page shared dirs (_voices,
#      _voice_previews) — those clones/previews are owned by no single job/video.
#   3. Reference guard: a candidate file is removed ONLY if no SURVIVING DB row
#      still points at it. This matters because per-scene audio (scene_NNN.wav)
#      is page-scoped, NOT job-scoped — two jobs on the same page reuse the same
#      audio filenames, so blindly unlinking would corrupt a sibling job.
#   4. Missing files are tolerated (treated as already-removed, not an error).

# Cross-page shared trees that no single job/video owns — never delete from these.
_SHARED_DIR_NAMES = ("_voices", "_voice_previews")


def _is_inside_root(full: str, root: str) -> bool:
    """True if realpath `full` is STRICTLY inside `root` (not the root itself)."""
    return full != root and full.startswith(root + os.sep)


def _is_shared_path(full: str, root: str) -> bool:
    """True if `full` lives under one of the cross-page shared dirs."""
    for name in _SHARED_DIR_NAMES:
        shared = os.path.join(root, name)
        if full == shared or full.startswith(shared + os.sep):
            return True
    return False


def _guard_path(path: str | None) -> tuple[str | None, str | None]:
    """Resolve `path` and decide if it is safe to remove.

    Returns (full_realpath, skip_reason). skip_reason is None when the path passes
    every guard; otherwise it is a short string explaining why it was skipped.
    """
    if not path:
        return None, "empty"
    root = os.path.realpath(CONTENT_OUTPUT_ROOT)
    full = os.path.realpath(path)
    if not _is_inside_root(full, root):
        return full, "outside CONTENT_OUTPUT_ROOT"
    if _is_shared_path(full, root):
        return full, "shared dir (never delete)"
    return full, None


# ---- Deferred deletion of LOCKED media ---------------------------------------
#
# Windows refuses os.remove on a file another process still holds open. The common case
# here is our OWN /media endpoint streaming an mp4 into the dashboard's <video> preview:
# the owner watches a video, deletes its job, and the unlink fails with
#   [WinError 32] The process cannot access the file because it is being used by another
# Before 2026-07-28 that error was logged and SWALLOWED while the DB row was deleted
# anyway, so the file became a permanent orphan with nothing left pointing at it (audit
# found 1486 such files / 5.41 GB). Now: retry briefly (the stream lock clears in
# seconds), and if it still fails, remember the path so a later delete or the next boot
# finishes the job.
_UNLINK_RETRIES = int(os.getenv("DELETE_UNLINK_RETRIES", "4"))
_UNLINK_BACKOFF_S = float(os.getenv("DELETE_UNLINK_BACKOFF_S", "0.35"))
# WinError 32 = sharing violation, 33 = lock violation. Only these are worth retrying;
# anything else (missing, permission, read-only) will not fix itself.
_RETRYABLE_WINERR = (32, 33)


def _pending_deletes_path() -> str:
    return os.path.join(CONTENT_OUTPUT_ROOT, "_cache", "pending_deletes.json")


def _pending_load() -> list[str]:
    try:
        with open(_pending_deletes_path(), encoding="utf-8") as f:
            data = json.load(f)
        return [str(p) for p in data] if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _pending_save(paths) -> None:
    """Persist the pending list (deduped, order-stable). Best-effort: never raises."""
    try:
        fp = _pending_deletes_path()
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        seen, out = set(), []
        for p in paths:
            if p and p not in seen:
                seen.add(p)
                out.append(p)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    except OSError as exc:
        print(f"[delete] could not persist pending-delete list: {exc}")


def _try_unlink(full: str) -> str | None:
    """os.remove with a short retry on a Windows sharing/lock violation.
    Returns None on success, else the last error string."""
    last = ""
    for attempt in range(1, max(1, _UNLINK_RETRIES) + 1):
        try:
            os.remove(full)
            return None
        except OSError as exc:
            last = str(exc)
            if getattr(exc, "winerror", None) not in _RETRYABLE_WINERR:
                break
            if attempt >= max(1, _UNLINK_RETRIES):
                break
            time.sleep(_UNLINK_BACKOFF_S * attempt)
    return last or "unknown error"


def sweep_pending_deletes() -> dict:
    """Retry every path parked by an earlier failed delete. Files that are now unlocked
    are removed; the rest stay queued. Re-checks the path guard each time (the content
    root may have moved) but NOT the DB — a queued path's row is already gone by
    construction. Best-effort: never raises. Returns {removed, stillPending}."""
    queued = _pending_load()
    if not queued:
        return {"removed": 0, "stillPending": 0}
    removed, remaining = 0, []
    for p in queued:
        full, reason = _guard_path(p)
        if full is None or reason is not None:
            continue  # unsafe or empty -> drop from the queue, never delete
        if not os.path.isfile(full):
            continue  # already gone -> drop
        err = _try_unlink(full)
        if err is None:
            removed += 1
        else:
            remaining.append(full)
    _pending_save(remaining)
    if removed or remaining:
        print(f"[delete] pending sweep: removed {removed}, still locked {len(remaining)}")
    return {"removed": removed, "stillPending": len(remaining)}


def _remove_files(paths, *, protected: set[str] | None = None) -> tuple[list[str], list[str], list[str]]:
    """Unlink each path that passes the guards and is not still referenced.

    `protected` is the set of realpaths still pointed at by SURVIVING DB rows; any
    candidate matching one is skipped (kept) so a sibling job/video is not corrupted.

    Returns (removed, kept, locked) as lists of realpaths — three OUTCOMES, kept apart
    on purpose because they mean opposite things to the owner:
      • removed — gone from disk.
      • kept    — deliberately NOT deleted: a surviving row still points at the file (the
                  normal case for page-scoped per-scene audio shared by another video), or
                  the path failed a guard. Nothing is wrong and nothing will be retried.
      • locked  — a REAL failure: Windows refused the unlink (WinError 32/33) because some
                  process holds the file open, so it was queued for a later sweep.
    They used to be merged into one `skipped` list, and the dashboard reported the total as
    "N tệp còn bị khoá trên đĩa" — so deleting a job whose audio a sibling video still uses
    warned about 91 "locked" files while nothing was locked at all (job-291 report), and it
    promised a retry-on-restart that must never happen for those files.
    """
    protected = protected or set()
    removed: list[str] = []
    kept: list[str] = []
    locked: list[str] = []
    seen: set[str] = set()
    for p in paths:
        full, reason = _guard_path(p)
        if full is None:
            continue  # empty/None path: nothing to do, not reported
        if full in seen:
            continue
        seen.add(full)
        if reason is not None:
            print(f"[delete] KEEP {full} ({reason})")
            kept.append(full)
            continue
        if full in protected:
            print(f"[delete] KEEP {full} (still referenced by a surviving row)")
            kept.append(full)
            continue
        if not os.path.isfile(full):
            continue  # already gone — tolerated, not reported as removed/kept/locked
        err = _try_unlink(full)
        if err is None:
            removed.append(full)
        else:
            # Could not unlink even after the retries — QUEUE it instead of losing it.
            # The DB row still goes away (the owner asked for the delete), but the file
            # is no longer forgotten: the next delete call and the next boot retry it.
            print(f"[delete] LOCKED {full} (unlink failed after {_UNLINK_RETRIES} tries: {err})")
            locked.append(full)
    if locked:
        _pending_save(_pending_load() + locked)
    return removed, kept, locked


def _remove_empty_dirs(dirs) -> None:
    """Best-effort: drop now-empty per-job clip/image dirs, guarded to the root.

    Only removes a directory that is (a) strictly inside the content root, (b) not
    a shared dir, and (c) actually empty — so a page-level dir (e.g. <page>/audio
    that still holds a sibling job's files) is never blown away.
    """
    root = os.path.realpath(CONTENT_OUTPUT_ROOT)
    for d in dirs:
        if not d:
            continue
        full = os.path.realpath(d)
        if not _is_inside_root(full, root) or _is_shared_path(full, root):
            continue
        try:
            if os.path.isdir(full) and not os.listdir(full):
                os.rmdir(full)
        except OSError:
            pass


def _surviving_asset_paths(conn, *, exclude_video_ids: set[int]) -> set[str]:
    """Realpaths of every asset/video file still owned by rows we are KEEPING.

    Used as the reference guard: collected over all videos EXCEPT the ones being
    deleted (which are passed in `exclude_video_ids`). Page-scoped files (shared
    audio) referenced by a surviving sibling end up here and are preserved.
    """
    protected: set[str] = set()
    rows = conn.execute(
        "SELECT video_path, thumb_path, audio_path FROM videos WHERE id <> ALL(%s)",
        (list(exclude_video_ids) or [0],),
    ).fetchall()
    for r in rows:
        for p in (r["video_path"], r["thumb_path"], r["audio_path"]):
            if p:
                protected.add(os.path.realpath(p))
    arows = conn.execute(
        "SELECT path FROM assets WHERE video_id <> ALL(%s)",
        (list(exclude_video_ids) or [0],),
    ).fetchall()
    for a in arows:
        if a["path"]:
            protected.add(os.path.realpath(a["path"]))
    return protected


def _remove_render_cache_dir(video_id: int) -> bool:
    """Delete the ENTIRE render-cache dir _cache/renders/<video_id>/ (manifest +
    all scene wav/visual files). Guarded to stay inside CONTENT_OUTPUT_ROOT.
    Best-effort: NEVER raises. Returns True iff a directory was actually removed."""
    try:
        rdir = os.path.realpath(render_cache.render_dir(video_id))
    except (ValueError, OSError):
        return False
    root = os.path.realpath(CONTENT_OUTPUT_ROOT)
    renders_root = os.path.realpath(render_cache.renders_root())
    # Only ever touch a path strictly inside <root>/_cache/renders/.
    if not (rdir.startswith(renders_root + os.sep) and rdir.startswith(root + os.sep)):
        return False
    if not os.path.isdir(rdir):
        return False
    try:
        shutil.rmtree(rdir)
        return True
    except OSError as exc:
        print(f"[delete] SKIP render-cache dir {rdir} (rmtree failed: {exc})")
        return False


def _purge_render_cache_media(video_id: int) -> list[str]:
    """Delete every scene wav/visual file INSIDE _cache/renders/<video_id>/ but KEEP
    manifest.json, so the script stays reusable (script-only, no cached audio).
    Guarded to stay inside the render-cache dir. Best-effort: NEVER raises.
    Returns the basenames actually removed."""
    try:
        rdir = os.path.realpath(render_cache.render_dir(video_id))
    except (ValueError, OSError):
        return []
    renders_root = os.path.realpath(render_cache.renders_root())
    if not rdir.startswith(renders_root + os.sep) or not os.path.isdir(rdir):
        return []
    removed: list[str] = []
    for name in os.listdir(rdir):
        if name == render_cache.MANIFEST_NAME:
            continue  # keep the manifest so the script survives
        fp = os.path.join(rdir, name)
        try:
            if os.path.isfile(fp):
                os.remove(fp)
                removed.append(name)
        except OSError:
            pass  # best-effort
    return removed


@app.delete("/api/videos/{video_id}")
def delete_video(video_id: int, keep_script: bool = Query(False, alias="keepScript")):
    """Delete a video: its DB row (cascades assets/posts) AND its files on disk.

    Shares the path-guarded remover with DELETE /api/jobs/{id}. A file referenced
    by another (surviving) video — e.g. page-scoped scene audio — is preserved.

    Render-cache handling (_cache/renders/<video_id>/):
      - keepScript FALSE (default): the ENTIRE render-cache dir is removed (manifest
        + scene wav/visual files) — a full "delete everything".
      - keepScript TRUE: the per-scene wav/visual files are removed but manifest.json
        is KEPT, so the script stays reusable (script-only, no cached audio). The DB
        video row is deleted either way.
    """
    with get_conn() as conn:
        v = conn.execute(
            "SELECT video_path, thumb_path, audio_path FROM videos WHERE id = %s", (video_id,)
        ).fetchone()
        if not v:
            raise HTTPException(404, "Video not found")
        assets = conn.execute("SELECT path FROM assets WHERE video_id = %s", (video_id,)).fetchall()
        protected = _surviving_asset_paths(conn, exclude_video_ids={video_id})

    candidates = [v["video_path"], v["thumb_path"], v["audio_path"]] + [a["path"] for a in assets]
    clip_dirs = {os.path.dirname(a["path"]) for a in assets if a["path"]}
    removed, kept, locked = _remove_files(candidates, protected=protected)
    _remove_empty_dirs(clip_dirs)

    # Render cache: keep the manifest (script) or blow the whole dir away.
    if keep_script:
        purged = _purge_render_cache_media(video_id)
        removed_renders_dir = False
        kept_manifest = True
    else:
        purged = []
        removed_renders_dir = _remove_render_cache_dir(video_id)
        kept_manifest = False

    with get_conn() as conn:
        conn.execute("DELETE FROM videos WHERE id = %s", (video_id,))
    # Opportunistic: retry anything an EARLIER delete had to defer (the lock has usually
    # cleared by now). Cheap — a no-op when the queue is empty.
    pending = sweep_pending_deletes()
    return {
        "ok": True,
        "id": video_id,
        "removedFiles": removed,
        # THREE distinct outcomes — see _remove_files. `keptFiles` is NOT a problem (a
        # surviving row still uses those files); only `lockedFiles` needs the owner's
        # attention. `skippedFiles` is retained as their union for backward compatibility.
        "keptFiles": kept,
        "lockedFiles": locked,
        "skippedFiles": kept + locked,
        "removedRendersDir": removed_renders_dir,
        "keptManifest": kept_manifest,
        "purgedRenderCacheFiles": purged,
        "pendingDeletes": pending["stillPending"],
    }


class VideoPatch(BaseModel):
    title: str | None = None


@app.patch("/api/videos/{video_id}")
def patch_video(video_id: int, body: VideoPatch):
    """Rename a video. Trims the title; an empty/whitespace title clears it (→ NULL).
    Does NOT touch the render-cache manifest (the manifest title is the historical
    snapshot; only videos.title is user-editable). 404 if the video is missing."""
    new_title = (body.title or "").strip() or None
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM videos WHERE id = %s", (video_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Video not found")
        conn.execute("UPDATE videos SET title = %s WHERE id = %s", (new_title, video_id))
    return {"ok": True, "videoId": video_id, "title": new_title}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: int):
    """Delete a queue job: its DB row (cascades videos → assets/posts/metrics) AND
    its on-disk artifacts, via the shared path-guarded remover.

    Running-job decision: a job in status='running' is REFUSED with 409. It has a
    live worker behind it writing files this very moment; deleting its row/files
    mid-flight would race the runner and could orphan or corrupt output. The owner
    can let it finish (or it will fail) and then delete it.

    FK behavior (no orphans): videos.job_id → jobs ON DELETE CASCADE, and
    assets/posts/metrics cascade from videos. jobs.source_video_id → videos
    ON DELETE SET NULL, so any OTHER job that used a now-deleted video as its source
    simply has that pointer nulled — never a dangling reference.
    """
    with get_conn() as conn:
        job = conn.execute("SELECT id, status FROM jobs WHERE id = %s", (job_id,)).fetchone()
        if not job:
            raise HTTPException(404, "Job not found")
        if job["status"] == "running":
            raise HTTPException(409, "Job is running; cannot delete an in-flight job. "
                                     "Wait for it to finish or fail, then delete.")

        # Collect this job's video rows + their files and per-scene assets.
        videos = conn.execute(
            "SELECT id, video_path, thumb_path, audio_path FROM videos WHERE job_id = %s", (job_id,)
        ).fetchall()
        video_ids = {v["id"] for v in videos}
        assets = []
        if video_ids:
            assets = conn.execute(
                "SELECT path FROM assets WHERE video_id = ANY(%s)", (list(video_ids),)
            ).fetchall()
        # Protect anything still owned by OTHER jobs' videos (esp. page-scoped audio).
        protected = _surviving_asset_paths(conn, exclude_video_ids=video_ids)

    candidates: list[str] = []
    clip_dirs: set[str] = set()
    for v in videos:
        candidates += [v["video_path"], v["thumb_path"], v["audio_path"]]
    for a in assets:
        candidates.append(a["path"])
        if a["path"]:
            clip_dirs.add(os.path.dirname(a["path"]))

    removed, kept, locked = _remove_files(candidates, protected=protected)
    _remove_empty_dirs(clip_dirs)

    # Render cache: a job has NO per-script keep flag, so deleting a job FULLY purges
    # each of its videos' _cache/renders/<video_id>/ dirs (manifest + scene files).
    # Without this the manifests survive and resurface in reusable-scripts as
    # source:'manifest' orphans (the bug this fixes).
    removed_renders_dirs: list[int] = []
    for vid in video_ids:
        if _remove_render_cache_dir(vid):
            removed_renders_dirs.append(vid)

    with get_conn() as conn:
        conn.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
        remaining = conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"]

    # Opportunistic: retry anything an EARLIER delete had to defer (see above).
    pending = sweep_pending_deletes()
    return {
        "deletedId": job_id,
        "removedFiles": removed,
        # THREE distinct outcomes — see _remove_files. `keptFiles` is NOT a problem (a
        # surviving row still uses those files); only `lockedFiles` needs the owner's
        # attention. `skippedFiles` is retained as their union for backward compatibility.
        "keptFiles": kept,
        "lockedFiles": locked,
        "skippedFiles": kept + locked,
        "removedRendersDirs": removed_renders_dirs,
        "remaining": int(remaining),
        "pendingDeletes": pending["stillPending"],
    }


@app.post("/api/jobs/{job_id}/clear-error")
def clear_job_error(job_id: int):
    """Dismiss a job's error so it stops surfacing in the workflow UI forever.

    The owner asked to clear the ERROR, not the job, so the row is KEPT. The dashboard
    refetches /api/jobs every ~12s, so the dismissal must persist in the DB (not be a
    client-only state) — this endpoint zeroes the error/progress fields on the row.

    Fields mutated and WHY:
      - error        -> NULL   (the error text shown as "Lỗi: …" in WorkflowProgress)
      - progress_msg -> NULL   (the in-flight status line; a failure often leaves the
                                last step's message here, so it is cleared too)
      - progress_step-> NULL   (drives the red "failed" step circle in the diagram;
                                cleared so no step is highlighted as failed)
      - progress_pct -> 0      (reset the bar)
    status is LEFT AS-IS (stays 'failed'). Rationale: the job genuinely failed — that is
    the truth, and Jobs.tsx filters/counts on status; flipping it to 'done' would lie and
    inflate the done count. NOTE for frontend: WorkflowProgress renders the stuck failed
    state purely from status==='failed', so clearing fields alone changes the text to
    "Lỗi: không rõ" but does NOT hide it. WorkflowProgress needs a companion dismissal
    (a dismissedFailedIdRef mirroring dismissedDoneIdRef) keyed on (status==='failed' &&
    error===null) to fully hide a cleared-error job. That web change is tracked separately.

    Idempotent: clearing an already-clear job is a no-op success. Returns 404 if missing.
    Does NOT delete the row.
    """
    with get_conn() as conn:
        job = conn.execute("SELECT id, status FROM jobs WHERE id = %s", (job_id,)).fetchone()
        if not job:
            raise HTTPException(404, "Job not found")
        conn.execute(
            "UPDATE jobs SET error = NULL, progress_msg = NULL, progress_step = NULL, "
            "progress_pct = 0 WHERE id = %s",
            (job_id,),
        )
    return {
        "id": job_id,
        "error": None,
        "status": job["status"],
        "progressMsg": None,
        "progressStep": None,
        "progressPct": 0,
    }


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: int):
    """Stop a running job immediately and mark it 'stopped' (NOT 'failed').

    Three-layer stop:
      1) DB: status='stopped', error='Dừng bởi người dùng', finished_at=now() so the
         FE can distinguish a deliberate stop (offer Resume) from a real failure.
      2) Immediate kill: tree-kill the job's ACTIVE subprocess (yt-dlp / whisper /
         claude -p / TTS / FFmpeg / Blender) via kill_job_processes — so a long child
         dies AT ONCE instead of running to the next step boundary.
      3) Cooperative cancel: still add to _CANCEL_REQUESTED (checked at step
         boundaries) AND _STOPPED_JOBS (so the runner classifies the resulting
         mid-step crash as 'stopped', not 'failed'). The cooperative path also covers
         the ONE step that has no subprocess to kill: ComfyUI image generation is an
         HTTP call (urllib), so an in-flight SDXL render is NOT interrupted and only
         aborts at the next _check_cancel boundary. (Honest limitation.)

    'stopped' is a NEW status value (no DB CHECK constraint exists, so it is accepted).
    Returns 404 if missing, 409 if the job is not currently running.
    """
    with get_conn() as conn:
        job = conn.execute("SELECT id, status FROM jobs WHERE id = %s", (job_id,)).fetchone()
        if not job:
            raise HTTPException(404, "Job not found")
        if job["status"] != "running":
            raise HTTPException(409, f"Job is '{job['status']}', not running — only a running job can be stopped.")
        conn.execute(
            "UPDATE jobs SET status = 'stopped', error = %s, progress_msg = %s,"
            " finished_at = now() WHERE id = %s",
            ("Dừng bởi người dùng", "Đã dừng", job_id),
        )
    # Mark BEFORE killing so a mid-step crash from the kill is classified as 'stopped'.
    _STOPPED_JOBS.add(job_id)
    _CANCEL_REQUESTED.add(job_id)
    killed = kill_job_processes(job_id)
    print(f"[api] stop job {job_id}: marked stopped, tree-killed {killed} active process(es)")
    return {"id": job_id, "status": "stopped", "killedProcesses": killed}


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: int):
    """Re-queue a FAILED or STOPPED job from the furthest recoverable step (RESUME).

    The original row is KEPT (its error/stopped state stays the truth); a NEW queued
    job is inserted that copies every production parameter from the old one. The only
    field that differs is the recovery shortcut:

      - If the old job's video already has a saved videos.script JSONB (persisted by
        _save_script BEFORE TTS, i.e. ingest + script-gen succeeded), the new job sets
        reuse_script_video_id = <that video id>. The runner then BYPASSES ingest and
        script-gen (no `claude -p`) and resumes straight from TTS. This is the
        "furthest recoverable point".
      - Otherwise (no video / NULL / empty script — stopped/failed at or before
        script-gen), the new job carries the SAME params with reuse_script_video_id =
        NULL, i.e. a clean full re-run from scratch.

    RESUME SCOPE (honest limitation): the ONLY pipeline artifact persisted re-loadably
    mid-flight is videos.script (saved right before TTS). The per-scene TTS wavs and
    SDXL images are written to disk but are NOT indexed in a re-loadable way until the
    SUCCESSFUL finalize (_save_assets/_finalize_video at the last step). So resume can
    skip ingest+script-gen (continue from TTS) but CANNOT continue from after-TTS or
    after-images — those steps re-run. The old job's progress_step records where it was
    for the FE, but it does not unlock finer-grained resume than script-reuse.

    Both 'failed' and 'stopped' jobs resume identically (a stop is just a user-induced
    interruption; the same artifacts survive). A queued/running/done/needs_input job
    must not be cloned.

    Returns {"newJobId": <int>}. 404 if the job is missing, 409 if not retryable.
    """
    with get_conn() as conn:
        job = conn.execute(
            """
            SELECT id, page_id, status, input_type, input_payload, voice, edit_mode,
                   comment, source_video_id, aspect, target_sec, add_credit,
                   render_model, voice_clone_model, src_audio_volume, publish,
                   publish_platform, title, progress_step, llm_provider, llm_model
              FROM jobs WHERE id = %s
            """,
            (job_id,),
        ).fetchone()
        if not job:
            raise HTTPException(404, "Job not found")
        if job["status"] not in ("failed", "stopped"):
            raise HTTPException(
                409,
                f"Job is {job['status']}, not failed/stopped; only a failed or stopped "
                f"job can be retried/resumed.",
            )

        # Resolve the recovery shortcut: find this job's video (videos.job_id FK) and
        # check whether it holds a reusable saved script. psycopg3 parses JSONB into a
        # Python object, so an empty/NULL/non-list script means "no reuse" → full re-run.
        vid = conn.execute(
            "SELECT id, script FROM videos WHERE job_id = %s ORDER BY id LIMIT 1",
            (job_id,),
        ).fetchone()
        reuse_script_video_id = None
        if vid and isinstance(vid["script"], list) and vid["script"]:
            reuse_script_video_id = vid["id"]

        # Clone the job: same params, fresh 'queued' row. Mirrors create_job's INSERT.
        row = conn.execute(
            """
            INSERT INTO jobs (page_id, page_seq, input_type, input_payload, status,
                              voice, edit_mode, comment, source_video_id,
                              aspect, target_sec, add_credit, render_model, voice_clone_model,
                              src_audio_volume, publish, title, publish_platform,
                              reuse_script_video_id, bypass_tts_cache, bypass_script_cache,
                              llm_provider, llm_model)
            VALUES (%s,
                    (SELECT COALESCE(MAX(page_seq), 0) + 1 FROM jobs WHERE page_id = %s),
                    %s, %s, 'queued', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            # bypass_tts_cache is intentionally hardcoded FALSE on a resume/retry: a
            # plain resume should reuse cached TTS (the failure was downstream, not the
            # voice), so it must NOT implicitly force-fresh synth. Only an explicit
            # "Dùng lại kịch bản" create_job sets the bypass.
            # bypass_script_cache intentionally FALSE on retry — fresh script not desired
            # (a resume should reuse the cached script-gen; only an explicit create sets it).
            (job["page_id"], job["page_id"], job["input_type"], job["input_payload"], job["voice"],
             job["edit_mode"], job["comment"], job["source_video_id"], job["aspect"],
             job["target_sec"], job["add_credit"], job["render_model"],
             job["voice_clone_model"], job["src_audio_volume"], job["publish"],
             job["title"], job["publish_platform"], reuse_script_video_id, False, False,
             # The LLM choice IS carried over (unlike the two bypass flags above): a retry
             # must re-run on the SAME backend the owner picked, otherwise a retry could
             # silently switch providers — exactly the surprise the no-fallback rule exists
             # to prevent. NULL stays NULL = claude-cli.
             job["llm_provider"], job["llm_model"]),
        ).fetchone()
    return {
        "newJobId": row["id"],
        "reuseScriptVideoId": reuse_script_video_id,
        # Honest resume hint for the FE: where the old job stopped, and whether the
        # new job will skip ingest+script-gen (resume from TTS) or re-run from scratch.
        "resumedFrom": job["progress_step"],
        "resume": reuse_script_video_id is not None,
    }


# ---- needs_input (Dubbed credit gate) ---------------------------------------

@app.get("/api/jobs/needs-input")
def list_needs_input_jobs():
    """Convenience discovery endpoint: ONLY the jobs currently parked in the
    'needs_input' state, each as the SAME object shape as in GET /api/jobs (so the
    FE can drive a banner/queue without scanning the full list). The full list also
    carries `needsInput`, so this is optional sugar."""
    with get_conn() as conn:
        all_jobs = fetch_jobs(conn)
    return {"jobs": [j for j in all_jobs if j["status"] == "needs_input"]}


class ResumeJobBody(BaseModel):
    # Resume a job parked in 'needs_input' (Dubbed credit gate).
    # skip=True  -> the owner DELIBERATELY accepts shipping with no credit (recorded
    #               as creditDecision='skipped' — NOT a silent gap).
    # skip=False -> the owner provided credit; any subset of the fields below is
    #               written onto the video row / carried into assemble.
    skip: bool = False
    sourceName: str | None = None
    sourceLink: str | None = None
    handle: str | None = None
    logo: str | None = None  # path-only (project secret/asset path); never an upload


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: int, body: ResumeJobBody):
    """Resume a Dubbed job parked in 'needs_input' after the owner fills the source
    credit OR explicitly Skips.

    - 404 if the job is missing; 409 if it is not in 'needs_input'.
    - On PROVIDE: writes sourceName/sourceLink onto the video row and stores
      handle/logo into the needs_input.prefill (the runner reads them on resume to
      pass into the credit slate). Records creditDecision='provided'.
    - On SKIP: records creditDecision='skipped' — the explicit "user accepted no
      credit" flag (distinguishable from an unhandled NULL). Partial values are kept.
    - Either way: re-queues the SAME job (status='queued', video back to 'rendering')
      with reuse_script_video_id = the parked video, so the runner re-assembles from
      the cached dubbed subs/filler WITHOUT re-translating/re-detecting (no claude -p).
      The runner's pause is guarded on creditDecision being set, so it never re-pauses.
    """
    with get_conn() as conn:
        job = conn.execute(
            "SELECT id, status, needs_input FROM jobs WHERE id = %s", (job_id,)
        ).fetchone()
        if not job:
            raise HTTPException(404, "Job not found")
        if job["status"] != "needs_input":
            raise HTTPException(
                409, f"Job is {job['status']}, not needs_input; only a parked job can be resumed.")
        ni = job["needs_input"] if isinstance(job["needs_input"], dict) else {}
        video_id = ni.get("videoId")
        if not video_id:
            # The parked payload must carry its destination video; absence is a bug
            # in the pause site, not a user error.
            raise HTTPException(500, "Parked job has no videoId in needs_input payload")

        # Normalize provided credit (empty string -> None so it doesn't masquerade
        # as a real value and re-arm the slate with blank text).
        def _clean(v):
            return v.strip() if isinstance(v, str) and v.strip() else None

        prefill = dict(ni.get("prefill") or {})
        if body.skip:
            credit_decision = "skipped"
            # Keep whatever partial values already exist; do not overwrite.
            new_source_name = prefill.get("sourceName")
            new_source_link = prefill.get("sourceLink")
        else:
            credit_decision = "provided"
            new_source_name = _clean(body.sourceName) or prefill.get("sourceName")
            new_source_link = _clean(body.sourceLink) or prefill.get("sourceLink")
            # Carry handle/logo into the prefill so the runner passes them to assemble.
            prefill["sourceName"] = new_source_name
            prefill["sourceLink"] = new_source_link
            prefill["handle"] = _clean(body.handle) or prefill.get("handle")
            prefill["logo"] = _clean(body.logo) or prefill.get("logo")

        # Persist the resolved credit onto the video row (so a partial prefill /
        # provided value survives and feeds the credit slate on resume).
        conn.execute(
            "UPDATE videos SET source_name = %s, source_link = %s, status = 'rendering'"
            " WHERE id = %s",
            (new_source_name, new_source_link, video_id),
        )
        # Re-queue the SAME job and record the decision. reuse_script_video_id points
        # at this job's own parked video so the runner re-hydrates the cached dubbed
        # record (subs/filler) without re-running ingest/translate/filler.
        resolved = {**ni, "prefill": prefill, "creditDecision": credit_decision,
                    "videoId": video_id}
        conn.execute(
            "UPDATE jobs SET status = 'queued', needs_input = %s,"
            " reuse_script_video_id = %s, progress_step = NULL, progress_pct = 0,"
            " progress_msg = NULL, error = NULL, finished_at = NULL WHERE id = %s",
            (Json(resolved), video_id, job_id),
        )
    return {"ok": True, "jobId": job_id, "status": "queued", "creditDecision": credit_decision}


class PublishBody(BaseModel):
    # The ticked channels from the Publish modal, by platform_accounts.id. These
    # may span MULTIPLE pages — many-to-many publish is allowed by design. REQUIRED
    # (an explicit selection); an empty list is rejected (422) by the core. Why
    # accountIds, not platforms: multiple pages can each own a 'youtube', so a bare
    # platform string is ambiguous now — an account id pins the exact target.
    accountIds: list[int] = []
    # Applies to Facebook only; YouTube ignores it. "PUBLISHED" = live now,
    # "SCHEDULED" = auto-publish at scheduledPublishTime, "DRAFT" = unpublished.
    state: str | None = None  # "PUBLISHED" | "SCHEDULED" | "DRAFT"
    # Unix seconds; REQUIRED when state == "SCHEDULED" (Facebook only).
    scheduledPublishTime: int | None = None
    # Editable caption body. None => the server builds the legacy default (first
    # scene narration). A string => used VERBATIM as the caption body, including
    # "" (an intentionally blank body). The source credit is controlled SEPARATELY
    # by includeSource — do NOT bake the "Nguồn: ..." line into this field.
    description: str | None = None
    # Whether to append the "Nguồn: <source>" credit (only has an effect when the
    # video has a source_name). Default True = legacy behavior.
    includeSource: bool = True


@app.post("/api/videos/{video_id}/publish")
def publish_video(video_id: int, body: PublishBody):
    """Publish a finished video to the SELECTED (ticked) channels, by account id.

    Thin wrapper over publish_core.publish_video_core — the SAME code path the
    runner's auto-publish uses (one shared core, like the shared delete remover).

    Body:
      - accountIds: [int, ...]  the ticked channels' platform_accounts.id. May span
        multiple pages (cross-page publish allowed by design). Empty => 422.
      - state: "PUBLISHED" | "DRAFT"  Facebook Reels only; default DRAFT = safe.

    Response (PublishResponse): each result carries accountId + pageId (plus
    platform/ok/url/error/state) so the UI can show which page each result is for.

    Validation/all-failed paths raise 422/404/400 (raised inside the core).
    Unknown/disconnected accountIds => 422 with a clear detail. Partial success is
    preserved: one account failing does NOT abort the others. There is NO global
    "already published" 409 gate — re-publishing is allowed; per-channel de-dup is
    handled by the modal (hides channels that still have a posts row).

    Gated by API_UPLOAD_ENABLED: while API publishing is disabled (auth blocked),
    this returns 403 BEFORE touching the publish core so no upload is attempted.
    """
    if not API_UPLOAD_ENABLED:
        raise HTTPException(403, "Đăng qua API đang tạm tắt do vướng xác thực.")
    return publish_video_core(
        video_id,
        account_ids=body.accountIds,
        state=(body.state or "PUBLISHED"),
        scheduled_publish_time=body.scheduledPublishTime,
        description=body.description,
        include_source=body.includeSource,
    )


class MarkPostedBody(BaseModel):
    # Video ids the user is hand-marking as ALREADY uploaded to the platform OUTSIDE
    # the API (manual upload). Only 'facebook' is supported for now.
    videoIds: list[int] = []
    platform: str = "facebook"


@app.post("/api/videos/mark-posted")
def mark_videos_posted(body: MarkPostedBody):
    """Mark videos as MANUALLY posted to a platform (uploaded by hand, off-API).

    For EACH videoId: resolve the video's OWN page (videos.page_id) → that page's
    platform_accounts row for the platform → record a `posts` row with manual=TRUE,
    status='posted', posted_at=now() and NULL platform_post_id/url (there is no API
    object). Mirrors the real publish path's DB effect by flipping
    videos.status='published'. Idempotent: a video that already has a posted post on
    that account is treated as ok WITHOUT inserting a duplicate.

    Only 'facebook' is supported now; any other platform is rejected (422).

    Body:     { videoIds: [int, ...], platform: "facebook" }
    Response:  { results: [ {videoId: int, ok: bool, error?: string} ... ] }
    Per-video: one video's failure never fails the others (never global-fail).
    """
    platform = (body.platform or "").strip().lower()
    if platform != "facebook":
        raise HTTPException(
            422, f"unsupported platform '{body.platform}' (only 'facebook' is supported)"
        )

    results: list[dict] = []
    with get_conn() as conn:
        for vid in body.videoIds:
            v = conn.execute(
                "SELECT id, page_id FROM videos WHERE id = %s", (vid,)
            ).fetchone()
            if not v:
                results.append({"videoId": vid, "ok": False, "error": "Không tìm thấy video."})
                continue
            # The video's OWN page's Facebook channel (per-page account isolation).
            acc = conn.execute(
                """
                SELECT id FROM platform_accounts
                 WHERE page_id = %s AND platform = 'facebook'
                 LIMIT 1
                """,
                (v["page_id"],),
            ).fetchone()
            if not acc:
                results.append({
                    "videoId": vid, "ok": False,
                    "error": "Trang của video chưa liên kết kênh Facebook.",
                })
                continue
            # Idempotent: skip the INSERT if this video already has a posted facebook
            # post on that account (manual or real) — still report ok.
            existing = conn.execute(
                """
                SELECT 1 FROM posts
                 WHERE video_id = %s AND platform_account_id = %s
                   AND platform = 'facebook' AND status = 'posted'
                 LIMIT 1
                """,
                (vid, acc["id"]),
            ).fetchone()
            if not existing:
                conn.execute(
                    """
                    INSERT INTO posts (video_id, platform_account_id, platform,
                                       platform_post_id, url, status, manual, posted_at)
                    VALUES (%s, %s, 'facebook', NULL, NULL, 'posted', TRUE, now())
                    """,
                    (vid, acc["id"]),
                )
            # Mirror the real publish path: a successful (manual) post publishes the video.
            conn.execute("UPDATE videos SET status = 'published' WHERE id = %s", (vid,))
            results.append({"videoId": vid, "ok": True})

    return {"results": results}


@app.get("/api/videos/{video_id}/publish-progress")
def publish_progress(video_id: int, platform: str = "facebook"):
    """Poll the live upload progress for a video's in-flight publish (cheap; dict
    lookup, no DB). Powers the FE % bar during the slow chunked Facebook feed upload.

    Query: platform (default "facebook"). Key = f"{video_id}:{platform}".
    Returns:
      - in flight / recently finished:
        { active: true, phase: "start|transfer|finish|done|error",
          pct: 0..100, bytesSent: int, bytesTotal: int }
      - no entry (never started, or evicted ~30s after done/error):
        { active: false }
    Note: feed progress advances per ~8MiB chunk (~60 updates for a 296MB file);
    reels are coarse (start -> done)."""
    entry = facebook_upload.get_upload_progress(f"{video_id}:{platform}")
    if not entry:
        return {"active": False}
    return {
        "active": True,
        "phase": entry.get("phase"),
        "pct": entry.get("pct", 0.0),
        "bytesSent": entry.get("bytesSent", 0),
        "bytesTotal": entry.get("bytesTotal", 0),
    }


@app.delete("/api/videos/{video_id}/posts")
def unpublish_video_from_page(video_id: int, page_id: int = Query(..., alias="pageId")):
    """Remove a video FROM one page's "Products" block by deleting ONLY the local
    `posts` rows that tie this video to that page's platform_account(s).

    This does NOT call any platform delete API — nothing is removed on Facebook (or
    anywhere else). It only detaches the video from that page locally. It is also
    DISTINCT from DELETE /api/videos/{id} (which deletes the local file + the videos
    row and cascades posts): here the local file, the `videos` row, assets, and the
    render cache are all left untouched, so the video stays reusable.

    A post's page is derived via platform_account_id -> platform_accounts.page_id
    (posts has NO page_id). So we target exactly the posts of THIS video whose
    account belongs to `pageId` — those are the ones that make the video appear in
    that page's Products block — regardless of platform.

    After removal, if the video has NO remaining posted posts anywhere, its status is
    reset from 'published' back to 'ready' so it does not linger as published with no
    posts (the origin page keeps showing it via its own page_id, now as 'ready').

    Response: { ok: true, videoId, pageId, removed: <n> }  (n = rows removed).
    """
    with get_conn() as conn:
        removed_rows = conn.execute(
            """
            DELETE FROM posts
             WHERE id IN (
               SELECT po.id
                 FROM posts po
                 JOIN platform_accounts pa ON pa.id = po.platform_account_id
                WHERE po.video_id = %s AND pa.page_id = %s
             )
            RETURNING id
            """,
            (video_id, page_id),
        ).fetchall()
        removed = len(removed_rows)
        if removed == 0:
            # Vietnamese detail (surfaces in the dashboard).
            raise HTTPException(
                404,
                "Không có bài đăng nào của video này trên trang đó (no posts for this video on that page).",
            )
        # If nothing posted remains anywhere, drop the 'published' label back to
        # 'ready' so the video doesn't linger as published with no posts.
        remaining = conn.execute(
            "SELECT count(*) AS n FROM posts WHERE video_id = %s AND status = 'posted'",
            (video_id,),
        ).fetchone()
        if remaining and remaining["n"] == 0:
            conn.execute(
                "UPDATE videos SET status = 'ready' WHERE id = %s AND status = 'published'",
                (video_id,),
            )

    return {"ok": True, "videoId": video_id, "pageId": page_id, "removed": removed}


@app.get("/api/videos/{video_id}/publish-preflight")
def publish_preflight(video_id: int):
    """Per-platform preflight for the publish modal: given THIS video's shape
    (aspect ratio / duration), tell how each platform will treat it, so the modal
    can show a column per platform with video-specific info. Path-only ffprobe on
    the local file — no creds, no network. The Facebook decision is the exact one
    publish uses (facebook_upload.decide_mode); YouTube's Short-vs-video hint
    mirrors the Shorts rule (vertical/square AND <= 180s)."""
    with get_conn() as conn:
        v = conn.execute(
            "SELECT video_path, script, source_name, source_link FROM videos WHERE id = %s",
            (video_id,),
        ).fetchone()
    if not v:
        raise HTTPException(404, "Video not found")
    if not v["video_path"] or not os.path.isfile(v["video_path"]):
        raise HTTPException(422, "Video file not found on disk")

    # Default caption BODY only (first scene narration) — WITHOUT the "Nguồn:"
    # credit, which the modal toggles separately via includeSource. Lets the modal
    # prefill an editable field and decide whether to show the source toggle.
    scenes = v["script"] or []
    default_description = scenes[0].get("narration", "") if scenes else ""

    d = facebook_upload.decide_mode(v["video_path"])
    info = d.get("info", {})
    w = int(info.get("width") or 0)
    h = int(info.get("height") or 0)
    dur = info.get("duration")
    # YouTube Short: vertical or square AND <= 180s; otherwise a normal video.
    vertical_or_square = h > 0 and h >= w
    yt_short = bool(vertical_or_square and dur is not None and dur <= 180)

    # Where this video has ALREADY been posted (draft or live), so the modal can
    # flag "already on <platform>" up front. Source of truth is the posts table
    # (videos.status only flips to 'published' on a real public go-live, so a
    # facebook DRAFT would not show there — posts does).
    with get_conn() as conn:
        post_rows = conn.execute(
            """
            SELECT po.platform, po.status, po.platform_post_id, po.url, po.posted_at,
                   pa.page_id, pa.id AS account_id, p.name AS page_name
              FROM posts po
              JOIN platform_accounts pa ON pa.id = po.platform_account_id
              JOIN pages p ON p.id = pa.page_id
             WHERE po.video_id = %s
             ORDER BY po.posted_at DESC
            """,
            (video_id,),
        ).fetchall()
    posts = [
        {
            "platform": r["platform"],
            "status": r["status"],                    # 'posted' | 'draft'
            "postId": r["platform_post_id"],
            "url": r["url"],
            "postedAt": r["posted_at"].isoformat() if r["posted_at"] else None,
            "pageId": r["page_id"],
            "accountId": r["account_id"],
            "pageName": r["page_name"],
        }
        for r in post_rows
    ]

    return {
        "width": w or None,
        "height": h or None,
        "duration": dur,
        "aspectRatio": info.get("aspect_ratio"),
        "facebook": {
            "mode": d["mode"],                   # "reel" | "feed"
            "reelOk": d.get("reel_ok", False),
            "reelReason": d.get("reel_reason"),  # why NOT a reel (when mode == feed)
        },
        "youtube": {
            "mode": "short" if yt_short else "video",
        },
        "posts": posts,
        # Editable-caption support for the publish modal:
        "defaultDescription": default_description,   # body only, no "Nguồn:" credit
        "sourceName": v["source_name"] or None,
        "sourceLink": v["source_link"] or None,
    }


# Aspect ratios the clone re-assemble accepts. Derived from the runner's ASPECTS
# map (the single source of truth for supported frame sizes) so the two never
# drift: the clone job re-assembles via ASPECTS.get(new_aspect), so every key
# here is guaranteed renderable (9:16, 16:9, 1:1, 4:5).
CLONE_ASPECTS = set(_RUNNER_ASPECTS.keys())


class CloneBody(BaseModel):
    aspect: str  # target aspect ratio — must be one of CLONE_ASPECTS


@app.post("/api/videos/{video_id}/clone")
def clone_video(video_id: int, body: CloneBody):
    """Re-render an EXISTING finished video at a DIFFERENT aspect ratio (any of the
    pipeline's supported ratios: 9:16, 16:9, 1:1, 4:5),
    reusing ALL already-generated content (script, per-scene audio, per-scene visuals).
    The runner runs an ASSEMBLE-ONLY job — NO ingest, script, TTS, or SDXL.

    Requires a cached content snapshot at _cache/renders/<video_id>/manifest.json
    (written on the source video's first render when RENDER_CACHE is on). Videos that
    predate this feature — or were rendered with RENDER_CACHE off — have no cache and
    CANNOT be cloned (404).

    Contract:
      404  the video doesn't exist, OR it has no cached content (can't clone).
      422  aspect not in {9:16, 16:9, 1:1, 4:5}.
      409  requested aspect equals the source video's aspect (nothing to do).
      200  {"ok": true, "videoId": <new id>, "jobId": <id>}
    """
    aspect = (body.aspect or "").strip()
    if aspect not in CLONE_ASPECTS:
        raise HTTPException(422, f"aspect must be one of {sorted(CLONE_ASPECTS)}")

    with get_conn() as conn:
        v = conn.execute(
            "SELECT id, page_id, width, height FROM videos WHERE id = %s", (video_id,)
        ).fetchone()
        if not v:
            raise HTTPException(404, "Video not found")
        # The source aspect that was actually rendered: prefer the cached manifest's
        # recorded aspect, else derive from the stored frame size.
        src_aspect = conn.execute(
            "SELECT aspect FROM jobs WHERE id = (SELECT job_id FROM videos WHERE id = %s)",
            (video_id,),
        ).fetchone()

    # Cached content is REQUIRED — without it there is nothing to re-assemble.
    manifest = render_cache.load_manifest(video_id)
    if not manifest:
        raise HTTPException(
            404,
            "No cached content for this video — it cannot be cloned. "
            "(Rendered before this feature, or RENDER_CACHE was off.)",
        )

    # 409 if the requested aspect equals the source aspect (nothing to do). Use the
    # manifest's recorded aspect first (authoritative), then the job aspect, then
    # infer from width/height.
    source_aspect = manifest.get("aspect") or (src_aspect["aspect"] if src_aspect else None)
    if not source_aspect and v["width"] and v["height"]:
        source_aspect = "16:9" if v["width"] >= v["height"] else "9:16"
    if source_aspect == aspect:
        raise HTTPException(409, f"video is already {aspect}; nothing to clone")

    # Enqueue a CLONE job (assemble-only) + create its destination video row up front
    # so the runner can resolve it by job_id. clone_of_video_id marks the job; the
    # TARGET aspect rides the existing aspect field.
    with get_conn() as conn:
        job = conn.execute(
            """
            INSERT INTO jobs (page_id, page_seq, input_type, input_payload, status, aspect, clone_of_video_id)
            VALUES (%s,
                    (SELECT COALESCE(MAX(page_seq), 0) + 1 FROM jobs WHERE page_id = %s),
                    'clone', %s, 'queued', %s, %s)
            RETURNING id
            """,
            (v["page_id"], v["page_id"], f"clone of video {video_id}", aspect, video_id),
        ).fetchone()
        new_video = conn.execute(
            "INSERT INTO videos (job_id, page_id, status) VALUES (%s, %s, 'rendering') RETURNING id",
            (job["id"], v["page_id"]),
        ).fetchone()
    return {"ok": True, "videoId": new_video["id"], "jobId": job["id"]}


@app.get("/api/bootstrap")
def bootstrap():
    """Everything the dashboard needs in one round trip."""
    with get_conn() as conn:
        return {
            "pages": fetch_pages(conn),
            "accounts": fetch_accounts(conn),
            "jobs": fetch_jobs(conn),
            "videos": fetch_videos(conn),
            "analytics": fetch_analytics(conn),
            "org": fetch_org(conn),
        }


# ===========================================================================
# Static SPA hosting — serve the built frontend same-origin so runtime Vite is
# gone. FE fetches use RELATIVE paths (/api, /generate, /media), so hosting the
# built bundle from this origin needs NO base-URL change on the web side.
#
# Registration order matters: this block runs at import time AFTER every router
# and every @app route above, so FastAPI matches all real API routes FIRST and
# the catch-all only answers what nothing else claimed. The catch-all also
# explicitly refuses the API prefixes so an unknown /api/* path 404s as an API
# path (not as a silently-served index.html).
#
# Dev flow is untouched: if dist/ is absent (no build yet) this whole block is
# skipped, so run-api.ps1 + Vite :5173 still boot exactly as before. The dist
# location is resolved relative to this file and overridable via WEB_DIST_DIR.
# ===========================================================================
from pathlib import Path as _Path  # noqa: E402
from fastapi.staticfiles import StaticFiles as _StaticFiles  # noqa: E402

_WEB_DIST_DIR = _Path(
    os.getenv("WEB_DIST_DIR") or (_Path(__file__).resolve().parent.parent / "web" / "dist")
).resolve()

# Path prefixes owned by the API/server — the SPA catch-all must never answer
# these (an unknown one should 404 as an API path, not fall back to index.html).
_API_PREFIXES = ("/api", "/generate", "/media", "/docs", "/redoc", "/openapi.json")

if (_WEB_DIST_DIR / "index.html").is_file():
    _INDEX_HTML = str(_WEB_DIST_DIR / "index.html")

    # Hashed, content-addressed build assets (JS/CSS/fonts). Mounted (not routed)
    # so it is matched ahead of the catch-all and 404s cleanly on a missing hash.
    _assets_dir = _WEB_DIST_DIR / "assets"
    if _assets_dir.is_dir():
        app.mount("/assets", _StaticFiles(directory=str(_assets_dir)), name="spa-assets")

    # index.html must NEVER be cached. It is the only file whose NAME is stable while its
    # CONTENT changes on every build (it carries the hashed asset URLs), and the response
    # used to ship with just ETag/Last-Modified and no Cache-Control — which lets a browser
    # reuse it heuristically without revalidating. The tab then keeps loading the OLD
    # index.html, whose OLD hashed bundle is also still in the browser cache, so a rebuilt
    # dashboard silently keeps running the previous code: the "I rebuilt and my fix still
    # isn't there" trap (hit on the delete-toast fix). The hashed assets under /assets are
    # content-addressed and stay cacheable — only the shell is forced to revalidate.
    _NO_STORE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}

    @app.get("/{full_path:path}")
    def _spa_catch_all(full_path: str):
        """SPA fallback: serve a real dist file if the path maps to one, else
        index.html for client-side routing. Refuses API prefixes so they 404
        as API paths instead of being masked by index.html."""
        p = "/" + full_path
        for pre in _API_PREFIXES:
            if p == pre or p.startswith(pre + "/"):
                raise HTTPException(status_code=404, detail="Not found")
        # Real static file under dist (favicon, logos/, robots.txt, …)?
        if full_path:
            candidate = (_WEB_DIST_DIR / full_path).resolve()
            # Traversal guard: candidate must stay inside dist.
            if candidate.is_file() and candidate.is_relative_to(_WEB_DIST_DIR):
                # An unhashed dist file (favicon, logo, robots.txt) can also be replaced by
                # a rebuild under the same name, so it gets the same no-store treatment.
                return FileResponse(str(candidate), headers=_NO_STORE)
        # Otherwise it's an SPA client route → hand back the shell.
        return FileResponse(_INDEX_HTML, headers=_NO_STORE)

    print(f"[web] Serving built SPA from {_WEB_DIST_DIR}")
else:
    print(
        f"[web] dist not found at {_WEB_DIST_DIR} — SPA not served "
        f"(dev-only mode; run Vite on :5173). Build with `npm run build` in Dashboard/web."
    )

