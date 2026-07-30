"""Shared publish core — ONE code path that publishes a finished video to its
page's linked social channels, used by BOTH the manual endpoint
(POST /api/videos/{id}/publish in main.py) and the runner's auto-publish step
(runner.py step 6).

Why a separate module (not a function in main.py)?
  main.py imports runner (`from runner import start_runner`), so if runner also
  imported main at module load we'd have an import cycle. Putting the publish
  core in its own leaf module — which imports only db + the uploaders (none of
  which import main/runner) — lets both main.py and runner.py import it with no
  cycle. Same spirit as the shared delete remover.

Design contract (MANY-TO-MANY publish, owner-approved redesign):
  - publish_video_core(video_id, account_ids=None, state="DRAFT")
      account_ids=None  => AUTO mode (runner/auto-publish): publish to ALL
                           currently-connected channels of the VIDEO'S OWN page
                           (its origin/production page).
      account_ids=[...] => explicit selection by platform_accounts.id. These may
                           span MULTIPLE pages — cross-page publish is ALLOWED BY
                           DESIGN (not an IDOR). Each id must resolve to a real,
                           connected, publishable account or the call is rejected
                           (422). Identity for each comes ONLY from THAT account's
                           own credentials_ref (borrowed-account rule).
      state             => Facebook Reels only ("PUBLISHED" | "DRAFT"); YouTube
                           ignores it. Default DRAFT = safe.
  - Why account_ids, not platforms: multiple pages can each own a 'youtube'
    account, so a bare platform string is ambiguous now. An account id pins the
    exact (page, platform, credentials_ref) target. A `posts` row tying the video
    to that account is what makes the video appear under that account's page
    "Products" block.
  - Validation raises HTTPException(422/404/400). The runner calls this
    best-effort and catches everything (a publish failure never fails the job).
  - Partial success: one account failing does not abort the others; each yields
    {accountId, pageId, platform, ok, url?/post_id?/error?, state?}. Only
    ALL-failed raises 400. Result recording (posts rows, video → 'published' on a
    real public go-live) is done here so both callers stay consistent.

Borrowed-account rule: identity comes ONLY from each account's own
platform_accounts.credentials_ref (a path to a token file); nothing is ever
inferred from the logged-in account. Token CONTENTS are never read/stored/logged
here — only the path ref.
"""

import os
import time

from fastapi import HTTPException

import facebook_upload
import media_spec
import youtube_upload
from db import get_conn

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
UPLOAD_PRIVACY = os.getenv("YOUTUBE_PRIVACY", "private")

# API-upload kill-switch. While Facebook API publishing is blocked on auth, real
# uploads are DISABLED by default. Read once at import (startup); this leaf module is
# the single source of truth consulted by BOTH the manual publish endpoint (403) and
# the runner's auto-publish (skip). Set API_UPLOAD_ENABLED=1 (or true/yes/on) to enable.
API_UPLOAD_ENABLED = os.getenv("API_UPLOAD_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")

# Platforms this backend can actually publish to (an uploader exists).
PUBLISHABLE_PLATFORMS = ("youtube", "facebook")

# Facebook Page Reels rate limit: max 30 Reels per rolling 24h per Page.
FACEBOOK_REELS_24H_LIMIT = 30


# ---- credential path helpers (path-only refs, never the secret) -------------

def _is_connected(credentials_ref: str | None) -> bool:
    """An account is 'connected' once its OAuth token file exists (API ready)."""
    if not credentials_ref:
        return False
    path = credentials_ref if os.path.isabs(credentials_ref) else os.path.join(_REPO_ROOT, credentials_ref)
    return os.path.isfile(path)


def _abs_creds_ref(ref: str | None) -> str | None:
    if not ref:
        return None
    return ref if os.path.isabs(ref) else os.path.join(_REPO_ROOT, ref)


# ---- description + per-platform publishers ----------------------------------

def _build_description(v, description: str | None = None, include_source: bool = True) -> str:
    """Publish caption text: a body + an optional source credit.

    Args:
      description   : None => use the legacy default body (first scene narration).
                      A string => use it VERBATIM as the body, including "" which
                      means an intentionally blank body (user cleared the field).
      include_source: append the "Nguồn: <name>[\\n<link>]" credit only when True
                      AND v["source_name"] is set. When False, no credit at all.

    Called with defaults (description=None, include_source=True) the output is
    byte-identical to the previous behavior, so the runner's AUTO publish is
    unchanged.
    """
    if description is None:
        # Legacy default body: first scene narration, with the exact legacy shape
        # (trailing blank line after the narration) preserved for byte-identity.
        scenes = v["script"] or []
        body = (scenes[0].get("narration", "") + "\n\n") if scenes else ""
        if include_source and v["source_name"]:
            credit = f"Nguồn: {v['source_name']}"
            if v["source_link"]:
                credit += f"\n{v['source_link']}"
            body += credit
        return body

    # Custom body: use VERBATIM (an empty string stays empty). Append the source
    # credit cleanly — a blank-line separator ONLY when the body is non-empty, so
    # an empty body + credit does not start with a leading blank line.
    body = description
    if not (include_source and v["source_name"]):
        return body
    credit = f"Nguồn: {v['source_name']}"
    if v["source_link"]:
        credit += f"\n{v['source_link']}"
    return f"{body}\n\n{credit}" if body else credit


def _publish_youtube(conn, v, acc, desc: str, thumb_path: str | None = None) -> dict:
    """Upload to YouTube. Returns a per-platform result dict.

    LENIENT pre-upload spec check (media_spec.RULES_YOUTUBE) runs BEFORE any
    bytes leave the machine. A spec failure raises HTTPException(422) with the
    SAME "spec check failed: <reason>" shape Facebook uses, so the manual
    endpoint surfaces a clean 422 (not a 500) and the runner's best-effort
    try/except records it as a per-account failure without aborting the others.

    `thumb_path` (the video's custom cover, already resolved to an existing file
    or None by the caller) is passed to the uploader, which sets it via
    thumbnails.set BEST-EFFORT after the upload — a thumbnail failure (e.g. an
    unverified channel) never fails the publish.
    """
    spec = media_spec.check_spec(v["video_path"], media_spec.RULES_YOUTUBE)
    if not spec["ok"]:
        raise HTTPException(422, f"spec check failed: {spec['reason']}")

    token = _abs_creds_ref(acc["credentials_ref"])
    pub = youtube_upload.upload_video(
        token, v["video_path"], v["title"] or "video", description=desc,
        privacy=UPLOAD_PRIVACY, thumb_path=thumb_path,
    )
    if not pub.get("ok"):
        return {"accountId": acc["id"], "pageId": acc["page_id"],
                "platform": "youtube", "ok": False, "error": pub.get("reason", "Upload failed")}

    conn.execute(
        """
        INSERT INTO posts (video_id, platform_account_id, platform, platform_post_id, url, privacy, status, posted_at)
        VALUES (%s, %s, 'youtube', %s, %s, %s, 'posted', now())
        """,
        (v["id"], acc["id"], pub.get("videoId"), pub.get("url"), pub.get("privacy")),
    )
    out = {
        "accountId": acc["id"], "pageId": acc["page_id"],
        "platform": "youtube", "ok": True, "state": "PUBLISHED",
        "post_id": pub.get("videoId"), "url": pub.get("url"), "privacy": pub.get("privacy"),
    }
    # Soft note on the custom-thumbnail outcome (never affects publish success).
    if pub.get("thumb") is not None:
        out["thumb"] = pub.get("thumb")
    return out


def _publish_facebook(conn, v, acc, desc: str, state: str,
                      scheduled_publish_time: int | None = None,
                      thumb_path: str | None = None) -> dict:
    """Publish to a Facebook Page, auto-choosing the surface by the video's shape:
    portrait ~9:16 & 3..90s → Reels; everything else (16:9 / square / long) → a
    regular Page video post. Enforces the 30/24h Reels rate limit ONLY on the Reels
    path (a normal video post is not subject to it), surfaces a spec mismatch as a
    clear error, and records a posts row (PUBLISHED live / SCHEDULED for later /
    DRAFT), so history stays accurate.

    `thumb_path` (the video's custom cover, already resolved to an existing file or
    None by the caller) is set as the platform thumbnail BEST-EFFORT by the uploader
    after the video is created; a thumbnail failure never fails the publish (the soft
    outcome comes back in res["thumb"] and is echoed on the result)."""
    # Recommend the surface from the file's aspect ratio / duration (mirrors how
    # YouTube auto-handles any orientation through one pipeline).
    decision = facebook_upload.decide_mode(v["video_path"])
    mode = decision["mode"]  # "reel" | "feed"

    # Pass the facebook account dict (incl. credentials_ref) so the worker loads
    # the right creds; never read/echo the token here. The Page name is the
    # publishing ACCOUNT's page name (may differ from the video's origin page).
    fb_page = {"credentials_ref": acc["credentials_ref"], "name": acc["page_name"]}

    # Progress key the FE polls via GET /api/videos/{id}/publish-progress?platform=facebook.
    # Keyed by the VIDEO id (not the account) — the FE tracks progress per video.
    progress_key = f"{v['id']}:facebook"

    if mode == "reel":
        # Rate limit: 30 Reels / rolling 24h is PER FACEBOOK PAGE — i.e. per
        # platform_account, which is keyed to acc["page_id"]. (The video's origin
        # page may differ now that cross-page publish is allowed, so count by the
        # publishing ACCOUNT's page, joining posts -> platform_accounts.) A regular
        # video post is NOT a Reel, so the limit only applies on this branch.
        recent = conn.execute(
            """
            SELECT count(*) AS n
            FROM posts po
            JOIN platform_accounts pa ON pa.id = po.platform_account_id
            WHERE pa.page_id = %s AND po.platform = 'facebook'
              AND po.posted_at >= now() - interval '24 hours'
            """,
            (acc["page_id"],),
        ).fetchone()
        if recent and recent["n"] >= FACEBOOK_REELS_24H_LIMIT:
            raise HTTPException(
                429,
                f"Facebook Reels rate limit reached: {recent['n']}/{FACEBOOK_REELS_24H_LIMIT} "
                f"in the last 24h for this Page. Try again later.",
            )
        res = facebook_upload.publish_reel(
            page=fb_page, video_path=v["video_path"], caption=desc, state=state,
            scheduled_publish_time=scheduled_publish_time, thumb_path=thumb_path,
            progress_key=progress_key,
        )
    else:
        res = facebook_upload.publish_feed_video(
            page=fb_page, video_path=v["video_path"], caption=desc, state=state,
            scheduled_publish_time=scheduled_publish_time, thumb_path=thumb_path,
            progress_key=progress_key,
        )

    if not res.get("ok"):
        reason = res.get("reason", "Facebook publish failed")
        # Spec mismatch -> surface as 422 with the reason (not a 500).
        if "spec check failed" in reason:
            raise HTTPException(422, reason)
        return {"accountId": acc["id"], "pageId": acc["page_id"],
                "platform": "facebook", "ok": False, "state": state, "error": reason}

    # posts.status: 'posted' when live, 'scheduled' when it will auto-publish later,
    # 'draft' otherwise. posted_at is set so it counts toward the rate-limit window
    # in every case (a scheduled/draft upload still consumes the Reels API quota).
    post_status = {"PUBLISHED": "posted", "SCHEDULED": "scheduled"}.get(state, "draft")
    # Permalink so the UI can open/verify the upload. Store Facebook's DECLARED
    # canonical link (permalink_url), which reflects the video/reels MERGE: FB may
    # return "/reel/{id}/" even for a landscape Page video. Falling back to a guessed
    # "/watch/?v=" would NOT match what FB uses. Fallback (permalink unavailable, e.g.
    # a not-yet-public DRAFT/SCHEDULED post) is a surface-specific id URL.
    # NOTE: a correct URL does NOT guarantee public playback — a landscape video
    # filed as a reel, or a copyright-restricted reupload, can still show
    # "This page isn't available" to non-admins. That is a CONTENT issue, not a link one.
    fb_video_id = res.get("video_id") or res.get("post_id")
    if res.get("permalink_url"):
        fb_url = res["permalink_url"]
    elif fb_video_id:
        fb_url = (f"https://www.facebook.com/reel/{fb_video_id}" if mode == "reel"
                  else f"https://www.facebook.com/watch/?v={fb_video_id}")
    else:
        fb_url = None
    conn.execute(
        """
        INSERT INTO posts (video_id, platform_account_id, platform, platform_post_id, url, privacy, status, posted_at)
        VALUES (%s, %s, 'facebook', %s, %s, %s, %s, now())
        """,
        (
            v["id"], acc["id"], res.get("post_id") or res.get("video_id"), fb_url,
            "public" if state == "PUBLISHED" else "private", post_status,
        ),
    )
    surface = "Reel" if mode == "reel" else "video post"
    out = {
        "accountId": acc["id"], "pageId": acc["page_id"],
        "platform": "facebook", "ok": True, "state": state, "fbMode": mode,
        "post_id": res.get("post_id"), "video_id": res.get("video_id"),
        "url": fb_url,
    }
    # Soft note on the custom-thumbnail outcome (never affects publish success).
    if res.get("thumb") is not None:
        out["thumb"] = res.get("thumb")
    if state == "SCHEDULED":
        out["note"] = (
            f"scheduled Facebook {surface} — will auto-publish at the chosen time; "
            f"visible under the Page's scheduled content until then"
        )
    elif state != "PUBLISHED":
        out["note"] = (
            f"recorded as 'draft' (non-public Facebook {surface}) — visible only in "
            f"Meta Business Suite → Content, not on the Page feed"
        )
    return out


def _dispatch_publish(conn, v, platform: str, acc, desc: str, state: str,
                      scheduled_publish_time: int | None = None,
                      thumb_path: str | None = None) -> dict:
    """Single dispatch point for ONE platform → its uploader.

    Both the manual multi-platform endpoint and the runner's auto-publish go
    through this so there is exactly one publish code path per platform (same
    spirit as the shared delete remover). Adding a new platform = one branch here
    plus a `_publish_<platform>` helper.

    `thumb_path` is the video's custom cover, already resolved to an existing file
    (or None) by publish_video_core — each uploader sets it best-effort.
    """
    if platform == "youtube":
        # YouTube ignores state/schedule here (uploads at UPLOAD_PRIVACY); scheduling
        # is a Facebook-only feature for now.
        return _publish_youtube(conn, v, acc, desc, thumb_path=thumb_path)
    if platform == "facebook":
        # May raise HTTPException(429/422) before any publish — surfaces the
        # rate-limit / spec error clearly to the caller.
        return _publish_facebook(conn, v, acc, desc, state, scheduled_publish_time,
                                 thumb_path=thumb_path)
    return {"accountId": acc["id"], "pageId": acc["page_id"],
            "platform": platform, "ok": False, "error": f"unsupported platform '{platform}'"}


# ---- the shared core --------------------------------------------------------

def publish_video_core(video_id: int, account_ids: list[int] | None = None,
                       state: str = "PUBLISHED",
                       scheduled_publish_time: int | None = None,
                       description: str | None = None,
                       include_source: bool = True) -> dict:
    """Publish a finished video to selected platform accounts — the ONE shared path.

    MANY-TO-MANY: a video can be published to channels across MULTIPLE pages and
    will then appear in EACH published-to page's "Products" block (via posts). The
    video's own page_id (its origin/production page) no longer constrains where it
    may be published.

    Args:
      video_id    : the video to publish.
      account_ids : None => AUTO mode (runner auto-publish): publish to ALL
                    currently-connected channels of the VIDEO'S OWN page.
                    a list => explicit platform_accounts.id selection; may span
                    multiple pages (cross-page publish allowed by design). Each id
                    must resolve to a real, connected, publishable account.
      state       : "PUBLISHED" | "SCHEDULED" | "DRAFT" (Facebook only; YouTube
                    ignores it). SCHEDULED requires scheduled_publish_time.
      scheduled_publish_time : unix seconds; required when state == "SCHEDULED".
                    Facebook enforces ~10 min .. ~6 months in the future.
      description : None => legacy default caption body (first scene narration).
                    A string => that exact text as the caption body ("" = blank
                    body). See _build_description.
      include_source : append the "Nguồn: ..." source credit only when True AND the
                    video has a source_name. Defaults True (legacy behavior).

    Returns: {"ok": True, "published": <bool>, "results": [<per-account>...]} where
    each result carries accountId + pageId + platform + ok (+ url/post_id/error/state).

    Raises HTTPException on validation/all-failed (422/404/400). The runner
    wraps this in try/except (best-effort) so a publish failure never fails the job.

    Opens its own short-lived DB connection (works the same for the request-scoped
    endpoint and the runner's connection-per-step style).
    """
    state = (state or "PUBLISHED").upper()
    if state not in ("PUBLISHED", "DRAFT", "SCHEDULED"):
        raise HTTPException(422, f"invalid state '{state}' (expected PUBLISHED, SCHEDULED or DRAFT)")
    if state == "SCHEDULED":
        now = int(time.time())
        if not scheduled_publish_time:
            raise HTTPException(422, "scheduledPublishTime is required when state is SCHEDULED")
        # Facebook rejects times under ~10 min or beyond ~6 months; validate a
        # slightly-inside band so the user gets a clear 422 instead of a Graph error.
        if scheduled_publish_time < now + 600:
            raise HTTPException(422, "scheduledPublishTime must be at least 10 minutes in the future")
        if scheduled_publish_time > now + 180 * 24 * 3600:
            raise HTTPException(422, "scheduledPublishTime must be within ~6 months")

    with get_conn() as conn:
        v = conn.execute(
            """
            SELECT vi.id, vi.page_id, vi.title, vi.video_path, vi.script,
                   vi.source_name, vi.source_link, vi.status, vi.thumb_path,
                   p.name AS page_name
            FROM videos vi JOIN pages p ON p.id = vi.page_id
            WHERE vi.id = %s
            """,
            (video_id,),
        ).fetchone()
        if not v:
            raise HTTPException(404, "Video not found")
        # NOTE: no global "already published" (409) gate. videos.status='published'
        # is only a status LABEL — it never reverts when the video is unpublished/
        # deleted from the platform, so blocking on it permanently prevented
        # re-publishing. De-dup is handled per-channel by the publish modal (it
        # hides "Đã đăng" channels that still have a posts row) + the "gỡ khỏi trang"
        # action which deletes the posts row, making that channel publishable again.
        if not v["video_path"] or not os.path.isfile(v["video_path"]):
            raise HTTPException(422, "Video file not found on disk")

        # Resolve the custom cover ONCE: use it only when it's a non-empty path to an
        # existing image on disk, else None. Never fail publish over a missing cover —
        # each uploader sets it best-effort (see _publish_facebook / _publish_youtube).
        thumb_path = v["thumb_path"] if (v["thumb_path"] and os.path.isfile(v["thumb_path"])) else None

        # Resolve the target accounts. Each carries its OWN page_id/page_name so a
        # cross-page publish credits + counts against the right page.
        if account_ids is None:
            # AUTO mode: every connected, publishable account of the video's page.
            rows = conn.execute(
                """
                SELECT pa.id, pa.platform, pa.credentials_ref, pa.page_id, p.name AS page_name
                  FROM platform_accounts pa JOIN pages p ON p.id = pa.page_id
                 WHERE pa.page_id = %s
                 ORDER BY pa.platform
                """,
                (v["page_id"],),
            ).fetchall()
            targets = [
                a for a in rows
                if a["platform"] in PUBLISHABLE_PLATFORMS and _is_connected(a["credentials_ref"])
            ]
            if not targets:
                raise HTTPException(
                    422,
                    "This page has no connected youtube/facebook account to publish to "
                    "(no credentials file found).",
                )
        else:
            # Explicit selection by account id. Dedupe, preserve request order.
            seen: set[int] = set()
            ordered_ids: list[int] = []
            for aid in account_ids:
                if aid not in seen:
                    seen.add(aid)
                    ordered_ids.append(aid)
            if not ordered_ids:
                raise HTTPException(422, "accountIds was empty — tick at least one channel.")

            rows = conn.execute(
                """
                SELECT pa.id, pa.platform, pa.credentials_ref, pa.page_id, p.name AS page_name
                  FROM platform_accounts pa JOIN pages p ON p.id = pa.page_id
                 WHERE pa.id = ANY(%s)
                """,
                (ordered_ids,),
            ).fetchall()
            by_id = {a["id"]: a for a in rows}

            # Validate EVERY requested id resolves to a real, connected, publishable
            # account. Cross-page is allowed by design (not an IDOR), but a bad id is
            # rejected with a clear 422 — never silently dropped.
            bad: list[str] = []
            for aid in ordered_ids:
                a = by_id.get(aid)
                if a is None:
                    bad.append(f"{aid} (unknown account)")
                elif a["platform"] not in PUBLISHABLE_PLATFORMS:
                    bad.append(f"{aid} (platform '{a['platform']}' not publishable)")
                elif not _is_connected(a["credentials_ref"]):
                    bad.append(f"{aid} (not connected — no OAuth token on disk)")
            if bad:
                raise HTTPException(
                    422,
                    "cannot publish — these accountIds are unknown or not connected: "
                    + ", ".join(bad),
                )
            # Order targets by the request's order.
            targets = [by_id[aid] for aid in ordered_ids]

        desc = _build_description(v, description, include_source)
        results: list[dict] = []
        for acc in targets:
            results.append(
                _dispatch_publish(conn, v, acc["platform"], acc, desc, state,
                                  scheduled_publish_time, thumb_path=thumb_path)
            )

        # Did anything actually go LIVE? A facebook DRAFT is not live, so it does
        # not flip the video to 'published'. Mark published only on a real, public
        # publish (youtube upload, or a facebook PUBLISHED reel).
        went_live = any(
            r.get("ok") and r.get("state") == "PUBLISHED" for r in results
        )
        any_ok = any(r.get("ok") for r in results)
        if went_live:
            conn.execute("UPDATE videos SET status = 'published' WHERE id = %s", (video_id,))

    if not any_ok:
        # Every selected platform failed (or none were linked).
        raise HTTPException(400, {"message": "All publish attempts failed", "results": results})

    return {"ok": True, "published": went_live, "results": results}
