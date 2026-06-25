"""Link probe worker (cf-venv) — metadata only, no download.

Powers the Studio's paste-link preview: title, duration, thumbnail, channel
name and @handle. Fast (no media fetch).

Invoked as: cf-venv/python.exe probe_worker.py <input.json> <output.json>
input.json:  {"link": "https://..."}
output.json: {"title","durationS","thumbnail","channel","handle"}
"""

import json
import sys
import traceback
from pathlib import Path


def main() -> None:
    in_path, out_path = sys.argv[1], sys.argv[2]
    cfg = json.loads(Path(in_path).read_text(encoding="utf-8"))

    import yt_dlp

    with yt_dlp.YoutubeDL({"skip_download": True, "quiet": True, "no_warnings": True}) as ydl:
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
