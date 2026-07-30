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


import re as _re


# ---- CTC forced-alignment backbone (known text) --------------------------------
# Used for the KARAOKE word_map when the caller requests align="ctc" and supplies each
# item's `narration`. CTC aligns the KNOWN text, so it is accurate on English-heavy /
# loanword Vietnamese where whisper mis-transcribes + count-drifts (measured up to 1.27 s
# caption lag). Uses torchaudio's MMS_FA bundle + native forced_align (no C++ extension,
# CPU, offline from the torch-hub cache). Returns the SAME {segments:[{words:[...]}]} shape
# as the whisper path so assemble_footage consumes it unchanged. Per-item fallback: any
# failure returns None so the caller re-runs that item through whisper.
_CTC = {"model": None, "dict": None, "sr": None, "failed": False}


def _ctc_load():
    if _CTC["model"] is not None or _CTC["failed"]:
        return _CTC["model"] is not None
    try:
        import torch, torchaudio  # noqa
        b = torchaudio.pipelines.MMS_FA
        _CTC["model"] = b.get_model(with_star=False).to("cpu").eval()
        _CTC["dict"] = b.get_dict(star=None)
        _CTC["sr"] = int(b.sample_rate)
        return True
    except Exception as e:
        print(f"[ctc-align] unavailable ({e}); using whisper", file=sys.stderr)
        _CTC["failed"] = True
        return False


def _normalize_years_local(text: str) -> str:
    """Expand isolated 20xx years to digit-by-digit Vietnamese, space-joined (matches how
    F5 SPEAKS them), so CTC aligns THROUGH the year instead of stopping at 'năm'. Mirrors
    tts_worker._normalize_years' inline form (the only form the assembler audio contains)."""
    _YR = {0: "không", 1: "một", 2: "hai", 3: "ba", 4: "bốn",
           5: "năm", 6: "sáu", 7: "bảy", 8: "tám", 9: "chín"}
    return _re.sub(r"\b(20\d{2})\b", lambda m: " ".join(_YR[int(c)] for c in m.group(1)), text)


# Caption dash set — MUST mirror generate.py::_CAPTION_DASHES so the CTC word count
# equals the caption token count (both split each whitespace token on any hyphen/dash).
_CAPTION_DASHES = ("-", "‐", "‑", "‒", "–", "—", "―", "⁃", "−")
_CAPTION_DASH_RE = _re.compile("[" + "".join(_CAPTION_DASHES) + "]")


def _caption_tokens_local(text):
    """Flat caption token list mirroring generate.py::_tokenize_caption_glued: whitespace
    split, then split each token on any hyphen/dash variant (empties dropped). Returned
    count/order is IDENTICAL to the caption tokens generate.py lays the karaoke over, so the
    CTC word count below matches and the aligner takes its EXACT 1:1 branch."""
    toks = []
    for ws in _re.findall(r"\S+", (text or "").strip()):
        toks.extend(p for p in _CAPTION_DASH_RE.split(ws) if p)
    return toks


def _ctc_align_item(audio_path, narration):
    """Return whisper-shaped segments for `narration` aligned to `audio_path` via CTC, or
    None on failure. One segment carrying all per-word {start,end,word} (the assembler flat-
    tens words across segments, so a single segment is fine).

    Emits EXACTLY ONE span per CAPTION token (same tokenization as
    generate.py::_tokenize_caption_glued), so the caller's word count == caption token count
    and the karaoke aligner takes its EXACT 1:1 branch instead of the drifting interpolation
    branch. Number handling (job-145 karaoke-lag fix): a year ("2026") is spoken digit-by-
    digit, so it is EXPANDED for the acoustic alignment but its sub-word spans are COLLAPSED
    back into the single caption token "2026." (one visible highlight). A token with no
    alignable letters (e.g. "4.000") is a GAP token: skipped in the acoustic target, then
    assigned a span by linear interpolation between its nearest aligned neighbours so the 1:1
    count is preserved and it still gets a visible slot. Without this collapse, year expansion
    / dropped digit tokens made the CTC word count exceed the caption token count (e.g. 23 vs
    20/21), forcing the interpolation branch and a measured up-to-1.0 s highlight lag on the
    number/loanword-dense scenes 2-3."""
    if not narration or not _ctc_load():
        return None
    import torch, torchaudio
    import torchaudio.functional as AF
    import soundfile as sf
    from unidecode import unidecode
    d = _CTC["dict"]; model = _CTC["model"]; sr_model = _CTC["sr"]

    def _rom(w):
        r = _re.sub(r"[^a-z']", "", unidecode(w).lower())
        return "".join(c for c in r if c in d and d[c] != 0)

    # One entry per CAPTION token; each token's acoustic romanization is the concat of its
    # (year-expanded) sub-words. rom == "" marks a GAP token (spoken but non-alignable).
    ctoks = _caption_tokens_local(narration)
    if len(ctoks) < 2:
        return None
    tok_rom = []
    for ct in ctoks:
        subs = _re.findall(r"\S+", _normalize_years_local(ct))
        tok_rom.append("".join(_rom(s) for s in subs))
    if sum(1 for r in tok_rom if r) < 2:
        return None
    try:
        data, sr = sf.read(audio_path, dtype="float32", always_2d=False)
        if getattr(data, "ndim", 1) > 1:
            data = data.mean(axis=1)
        wav = torch.tensor(data, dtype=torch.float32).unsqueeze(0)
        if sr != sr_model:
            wav = torchaudio.functional.resample(wav, sr, sr_model)
        with torch.inference_mode():
            emission, _ = model(wav)
        tokens = [d[c] for r in tok_rom for c in r]
        targets = torch.tensor([tokens], dtype=torch.int32)
        aligned, scores = AF.forced_align(emission, targets, blank=0)
        spans = AF.merge_tokens(aligned[0], scores[0])
        ratio = wav.size(1) / emission.size(1)
        dur = wav.size(1) / sr_model
        # Pass 1: real span per alignable token; None placeholder for GAP tokens.
        words = []
        i = 0
        for ct, r in zip(ctoks, tok_rom):
            if not r:
                words.append({"start": None, "end": None, "word": " " + ct})
                continue
            ws = spans[i:i + len(r)]; i += len(r)
            if not ws:
                words.append({"start": None, "end": None, "word": " " + ct})
                continue
            st = round(float(ratio * ws[0].start / sr_model), 3)
            en = round(float(ratio * ws[-1].end / sr_model), 3)
            words.append({"start": st, "end": en, "word": " " + ct})
        real = [k for k, w in enumerate(words) if w["start"] is not None]
        if len(real) < 2:
            return None
        # Pass 2: fill each GAP token from its nearest aligned neighbours so every caption
        # token gets a monotonic, visible slot (generate.py enforces min slot width after).
        for k, w in enumerate(words):
            if w["start"] is not None:
                continue
            prev = max((r for r in real if r < k), default=None)
            nxt = min((r for r in real if r > k), default=None)
            if prev is not None and nxt is not None:
                lo = words[prev]["end"]; hi = words[nxt]["start"]
                st = lo + (hi - lo) * 0.34; en = lo + (hi - lo) * 0.66
            elif prev is not None:
                st = words[prev]["end"]; en = min(dur, st + 0.12)
            else:
                en = words[nxt]["start"]; st = max(0.0, en - 0.12)
            words[k]["start"] = round(float(st), 3)
            words[k]["end"] = round(float(en), 3)
        # PLAUSIBILITY / CLAMP: the last aligned word must end within the clip.
        # OmniVoice (and F5) can append a short trailing echo/hallucination after
        # the final real word; CTC then places the last token's span slightly past
        # the true audio end. Previously that returned None and dumped the WHOLE
        # scene to the whisper path -> word count != caption tokens -> the drifting
        # interpolation branch (the "runs ahead at the end" symptom). Instead, keep
        # the CTC alignment and CLAMP any word whose span runs past `dur` back into
        # the clip (start first, then end, keeping start < end). This preserves the
        # exact caption-token word count, so the scene stays on the EXACT 1:1 branch
        # and captions remain locked to the real onsets. Genuine failures (empty /
        # degenerate results, handled above) still return None.
        for w in words:
            if w["start"] > dur:
                w["start"] = round(max(0.0, dur - 0.12), 3)
            if w["end"] > dur:
                w["end"] = round(dur, 3)
            if w["end"] <= w["start"]:
                w["end"] = round(min(dur, w["start"] + 0.12), 3)
        return [{"start": words[0]["start"], "end": words[-1]["end"],
                 "text": " ".join(w["word"].strip() for w in words), "words": words}]
    except Exception as e:
        print(f"[ctc-align] {os.path.basename(audio_path)} failed ({e}); whisper fallback", file=sys.stderr)
        return None


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
    # KARAOKE alignment backbone: align="ctc" makes each item that carries a `narration`
    # use CTC forced alignment (known text) instead of whisper for its word timestamps.
    # Per-item fallback to whisper on any CTC failure. Default "whisper" (unchanged path).
    align_backend = (cfg.get("align") or "whisper").strip().lower()

    results = []
    for it in cfg["items"]:
        audio_path = it["audioPath"]
        if not Path(audio_path).exists():
            raise FileNotFoundError(f"audio not found: {audio_path}")

        seg_out = None
        used = "whisper"
        if align_backend == "ctc" and it.get("narration"):
            ctc_segs = _ctc_align_item(audio_path, it.get("narration"))
            if ctc_segs is not None:
                seg_out = ctc_segs
                used = "ctc"

        if seg_out is None:                        # whisper path (default or CTC fallback)
            segments, info = model.transcribe(
                audio_path, language=language, word_timestamps=word_ts,
            )
            seg_out = []
            for seg in segments:                  # generator -> realize it
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
            dur = round(info.duration, 3)
            lang = info.language
        else:
            dur = seg_out[-1]["end"] if seg_out else 0.0
            lang = "vi"
        print(f"[align] scene={it.get('scene')} backend={used}", file=sys.stderr)

        results.append({
            "scene": it.get("scene"),
            "audioPath": audio_path,
            "language": lang,
            "durationS": dur,
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
