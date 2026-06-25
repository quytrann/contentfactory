"""Warm the voice-preview cache — runs inside cf-venv.

Loads VieNeu ONCE, then synthesizes the fixed sample sentence for many voices,
writing each to an explicit output path the API chose. Used to pre-generate all
preset previews in a single model load (instead of one cold load per voice).

    cf-venv/python.exe prewarm_worker.py <input.json> <output.json>

input.json:  {"items":[{"voice":"Ngọc Linh","outPath":"…/preset_Ngọc Linh.wav"}],
              "text":"…", "applyWatermark":false}
output.json: {"count":N, "results":[{"voice","outPath"}]}
"""

import json
import sys
import traceback
from pathlib import Path


def main() -> None:
    in_path, out_path = sys.argv[1], sys.argv[2]
    cfg = json.loads(Path(in_path).read_text(encoding="utf-8"))

    from vieneu import Vieneu

    tts = Vieneu(mode="v3turbo")
    text = cfg.get("text") or "Xin chào, đây là giọng đọc mẫu cho kênh của bạn."
    apply_watermark = bool(cfg.get("applyWatermark", False))

    results = []
    for it in cfg["items"]:
        out = Path(it["outPath"])
        out.parent.mkdir(parents=True, exist_ok=True)
        wav = tts.infer(text, voice=it.get("voice"), ref_audio=it.get("refAudio"), apply_watermark=apply_watermark)
        tts.save(wav, out)
        results.append({"voice": it.get("voice"), "outPath": str(out)})

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
