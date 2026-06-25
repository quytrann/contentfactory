"""faster-whisper worker — runs inside cf-venv (where `faster_whisper` lives).

Invoked by the FastAPI host as:
    cf-venv/python.exe whisper_worker.py <input.json> <output.json>

input.json:  {"items":[{"scene":1,"audioPath":"..."}], "model":"medium",
              "device":"cpu", "compute":"int8", "language":"vi", "wordTimestamps":true}
output.json: {"count":N, "results":[{"scene","audioPath","language","durationS",
              "segments":[{"start","end","text","words":[{"start","end","word"}]}]}]}

The model loads once per invocation, then transcribes every item. These timestamps
drive scene length and caption sync (VieNeu-TTS itself emits no timing).
"""

import json
import os
import sys
import traceback
from pathlib import Path


def _enable_cuda_dlls() -> None:
    """Put torch's bundled cuDNN9 / cuBLAS12 DLLs on the DLL search path so
    CTranslate2 (faster-whisper on CUDA) can load them. On modern Windows Python,
    PATH alone is NOT honored for native DLL resolution — os.add_dll_directory is
    the robust mechanism. The host passes the dir via CF_TORCH_LIB; we also try to
    locate torch's lib dir directly as a fallback. No-op / best-effort on failure
    so CPU runs (or a graceful CUDA->error) are unaffected."""
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


def _load_whisper(WhisperModel, model_name, device, compute):
    """Construct a WhisperModel; if a CUDA load fails (driver/cuDNN hiccup), log
    clearly and fall back to CPU/int8 instead of hard-crashing STT."""
    try:
        m = WhisperModel(model_name, device=device, compute_type=compute)
        print(f"[whisper] device={device} compute={compute} model={model_name}", file=sys.stderr)
        return m
    except Exception as e:
        if device == "cuda":
            print(f"[whisper] CUDA init failed ({e}); falling back to cpu/int8", file=sys.stderr)
            m = WhisperModel(model_name, device="cpu", compute_type="int8")
            print(f"[whisper] device=cpu compute=int8 model={model_name} (fallback)", file=sys.stderr)
            return m
        raise


def main() -> None:
    in_path, out_path = sys.argv[1], sys.argv[2]
    cfg = json.loads(Path(in_path).read_text(encoding="utf-8"))

    _enable_cuda_dlls()
    from faster_whisper import WhisperModel

    model = _load_whisper(
        WhisperModel,
        cfg.get("model", "medium"),
        cfg.get("device", "cpu"),
        cfg.get("compute", "int8"),
    )
    language = cfg.get("language") or None        # None -> autodetect
    word_ts = bool(cfg.get("wordTimestamps", True))

    results = []
    for it in cfg["items"]:
        audio_path = it["audioPath"]
        if not Path(audio_path).exists():
            raise FileNotFoundError(f"audio not found: {audio_path}")

        segments, info = model.transcribe(
            audio_path, language=language, word_timestamps=word_ts,
        )

        seg_out = []
        for seg in segments:                      # generator -> realize it
            words = None
            if word_ts and seg.words:
                words = [
                    {"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word}
                    for w in seg.words
                ]
            seg_out.append({
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "text": seg.text,
                "words": words,
            })

        results.append({
            "scene": it.get("scene"),
            "audioPath": audio_path,
            "language": info.language,
            "durationS": round(info.duration, 3),
            "segments": seg_out,
        })

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
