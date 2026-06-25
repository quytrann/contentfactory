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
  - Validation raises HTTPException(422/404/409/400). The runner calls this
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

from fastapi import HTTPException

import facebook_upload
import media_spec
import youtube_upload
from db import get_conn

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
UPLOAD_PRIVACY = os.getenv("YOUTUBE_PRIVACY", "private")

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

def _build_description(v) -> str:
    """Description text: first narration line + an optional source credit."""
    scenes = v["script"] or []
    desc = (scenes[0].get("narration", "") + "\n\n") if scenes else ""
    if v["source_name"]:
        desc += f"Nguồn: {v['source_name']}"
        if v["source_link"]:
            desc += f"\n{v['source_link']}"
    return desc


def _publish_youtube(conn, v, acc, desc: str) -> dict:
    """Upload to YouTube. Returns a per-platform result dict.

    LENIENT pre-upload spec check (media_spec.RULES_YOUTUBE) runs BEFORE any
    bytes leave the machine. A spec failure raises HTTPException(422) with the
    SAME "spec check failed: <reason>" shape Facebook uses, so the manual
    endpoint surfaces a clean 422 (not a 500) and the runner's best-effort
    try/except records it as a per-account failure without aborting the others.
    """
    spec = media_spec.check_spec(v["video_path"], media_spec.RULES_YOUTUBE)
    if not spec["ok"]:
        raise HTTPException(422, f"spec check failed: {spec['reason']}")

    token = _abs_creds_ref(acc["credentials_ref"])
    pub = youtube_upload.upload_video(
        token, v["video_path"], v["title"] or "video", description=desc, privacy=UPLOAD_PRIVACY
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
    return {
        "accountId": acc["id"], "pageId": acc["page_id"],
        "platform": "youtube", "ok": True, "state": "PUBLISHED",
        "post_id": pub.get("videoId"), "url": pub.get("url"), "privacy": pub.get("privacy"),
    }


def _publish_facebook(conn, v, acc, desc: str, state: str) -> dict:
    """Publish a Facebook Page Reel. Enforces the 30/24h rate limit, surfaces a
    spec mismatch as a clear error, and records a posts row (even for DRAFT, so the
    rate-limit window + history stay accurate)."""
    # Rate limit: 30 Reels / rolling 24h is PER FACEBOOK PAGE — i.e. per
    # platform_account, which is keyed to acc["page_id"]. (The video's origin page
    # may differ now that cross-page publish is allowed, so count by the publishing
    # ACCOUNT's page, joining posts -> platform_accounts, NOT by the video's page.)
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

    # Pass the facebook account dict (incl. credentials_ref) so the worker loads
    # the right creds; never read/echo the token here. The Page name is the
    # publishing ACCOUNT's page name (may differ from the video's origin page).
    fb_page = {"credentials_ref": acc["credentials_ref"], "name": acc["page_name"]}
    res = facebook_upload.publish_reel(
        page=fb_page, video_path=v["video_path"], caption=desc, state=state,
    )
    if not res.get("ok"):
        reason = res.get("reason", "Facebook publish failed")
        # Spec mismatch -> surface as 422 with the reason (not a 500).
        if "spec check failed" in reason:
            raise HTTPException(422, reason)
        return {"accountId": acc["id"], "pageId": acc["page_id"],
                "platform": "facebook", "ok": False, "state": state, "error": reason}

    # posts.status: 'posted' when live; a DRAFT reel is recorded as 'draft' (it is
    # not public). posted_at is set so it counts toward the rate-limit window either
    # way — a draft still consumes the Reels API quota.
    post_status = "posted" if state == "PUBLISHED" else "draft"
    conn.execute(
        """
        INSERT INTO posts (video_id, platform_account_id, platform, platform_post_id, url, privacy, status, posted_at)
        VALUES (%s, %s, 'facebook', %s, %s, %s, %s, now())
        """,
        (
            v["id"], acc["id"], res.get("post_id") or res.get("video_id"), None,
            "public" if state == "PUBLISHED" else "private", post_status,
        ),
    )
    out = {
        "accountId": acc["id"], "pageId": acc["page_id"],
        "platform": "facebook", "ok": True, "state": state,
        "post_id": res.get("post_id"), "video_id": res.get("video_id"),
    }
    if state != "PUBLISHED":
        out["note"] = "recorded as 'draft' (non-public Facebook DRAFT reel)"
    return out


def _dispatch_publish(conn, v, platform: str, acc, desc: str, state: str) -> dict:
    """Single dispatch point for ONE platform → its uploader.

    Both the manual multi-platform endpoint and the runner's auto-publish go
    through this so there is exactly one publish code path per platform (same
    spirit as the shared delete remover). Adding a new platform = one branch here
    plus a `_publish_<platform>` helper.
    """
    if platform == "youtube":
        return _publish_youtube(conn, v, acc, desc)
    if platform == "facebook":
        # May raise HTTPException(429/422) before any publish — surfaces the
        # rate-limit / spec error clearly to the caller.
        return _publish_facebook(conn, v, acc, desc, state)
    return {"accountId": acc["id"], "pageId": acc["page_id"],
            "platform": platform, "ok": False, "error": f"unsupported platform '{platform}'"}


# ---- the shared core --------------------------------------------------------

def publish_video_core(video_id: int, account_ids: list[int] | None = None,
                       state: str = "DRAFT") -> dict:
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
      state       : "PUBLISHED" | "DRAFT" (Facebook Reels only; YouTube ignores it).

    Returns: {"ok": True, "published": <bool>, "results": [<per-account>...]} where
    each result carries accountId + pageId + platform + ok (+ url/post_id/error/state).

    Raises HTTPException on validation/all-failed (422/404/409/400). The runner
    wraps this in try/except (best-effort) so a publish failure never fails the job.

    Opens its own short-lived DB connection (works the same for the request-scoped
    endpoint and the runner's connection-per-step style).
    """
    state = (state or "DRAFT").upper()
    if state not in ("PUBLISHED", "DRAFT"):
        raise HTTPException(422, f"invalid state '{state}' (expected PUBLISHED or DRAFT)")

    with get_conn() as conn:
        v = conn.execute(
            """
            SELECT vi.id, vi.page_id, vi.title, vi.video_path, vi.script,
                   vi.source_name, vi.source_link, vi.status, p.name AS page_name
            FROM videos vi JOIN pages p ON p.id = vi.page_id
            WHERE vi.id = %s
            """,
            (video_id,),
        ).fetchone()
        if not v:
            raise HTTPException(404, "Video not found")
        if v["status"] == "published":
            raise HTTPException(409, "Video is already published")
        if not v["video_path"] or not os.path.isfile(v["video_path"]):
            raise HTTPException(422, "Video file not found on disk")

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

        desc = _build_description(v)
        results: list[dict] = []
        for acc in targets:
            results.append(_dispatch_publish(conn, v, acc["platform"], acc, desc, state))

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
