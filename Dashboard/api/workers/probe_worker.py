"""Link probe worker (cf-venv) — metadata only, no download.

Powers the Studio's paste-link preview: title, duration, thumbnail, channel
name and @handle. Fast (no media fetch).

Invoked as: cf-venv/python.exe probe_worker.py <input.json> <output.json>
input.json:  {"link": "https://..."}
output.json: {"title","durationS","thumbnail","channel","handle"}
"""

import json
import os
import sys
import traceback
from pathlib import Path


def _apply_yt_hardening(ydl_opts: dict) -> None:
    """Apply the shared YouTube bot-check / throttling hardening (player-client
    ladder, node JS runtime for nsig, retries). Mutates `ydl_opts` in place.

    A METADATA-ONLY fetch needs this just as much as a download: yt-dlp's default
    "web" client intermittently gets served "Sign in to confirm you're not a bot"
    on plain extract_info(download=False), which surfaced in the Studio as a
    paste-link preview that silently failed to load the title. The android_vr /
    ios clients don't trigger that challenge and need no cookies or login.

    Kept as a local copy rather than a shared import on purpose: every file in
    this directory is a STANDALONE script invoked as
    `cf-venv/python.exe <worker>.py in.json out.json` with stdlib-only top-level
    imports and no cross-worker dependencies. Mirrors _apply_yt_hardening in
    download_worker.py — keep the three copies in sync."""
    player_client = (os.getenv("YTDLP_PLAYER_CLIENT") or "").strip()
    clients = ([c.strip() for c in player_client.split(",") if c.strip()]
               if player_client else ["android_vr", "ios", "web_safari", "tv"])
    ydl_opts["extractor_args"] = {"youtube": {"player_client": clients}}

    js_rt = (os.getenv("YTDLP_JS_RUNTIME") or "node").strip()
    rt_name, _, rt_path = js_rt.partition(":")
    ydl_opts["js_runtimes"] = {rt_name.lower(): {"path": rt_path or None}}

    ydl_opts.update({
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "sleep_interval_requests": 1,
    })


def main() -> None:
    in_path, out_path = sys.argv[1], sys.argv[2]
    cfg = json.loads(Path(in_path).read_text(encoding="utf-8"))

    import yt_dlp

    # `ignore_no_formats_error` is what makes this a TRUE metadata-only probe.
    # The client ladder alone is necessary but not sufficient: it gets us past the
    # bot check, but the ladder then dies on FORMAT-selection errors ("Requested
    # format is not available" / "This video is DRM protected") for videos this
    # preview has no intention of downloading. We only read title/duration/
    # thumbnail/channel/handle — all present in the metadata regardless of whether
    # a playable format exists — so a format failure must not fail the preview.
    ydl_opts = {"skip_download": True, "quiet": True, "no_warnings": True,
                "ignore_no_formats_error": True}
    _apply_yt_hardening(ydl_opts)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(cfg["link"], download=False)

    out = {
        "title": info.get("title"),
        "durationS": round(float(info.get("duration") or 0), 1),
        "thumbnail": info.get("thumbnail"),
        "channel": info.get("channel") or info.get("uploader"),
        "handle": info.get("uploader_id"),
    }
    Path(out_path).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
