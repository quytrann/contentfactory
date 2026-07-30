"""Burned-in caption COVER generator for the `translate_full` edit_mode.

The `translate_full` mode keeps the FULL source video (no scene cutting), mutes
its audio, narrates a Vietnamese translation over it, and burns VN subtitles.
Many source shorts already carry their OWN burned-in (karaoke) captions at the
bottom. This module DETECTS those captions and emits an ASS "cover" track of
opaque rectangles that hide the original text so the new VN caption reads cleanly.

Two pieces, deliberately split by dependency so the backend can call each from
the right venv:

  1. detect_caption_boxes(...)   -- needs easyocr + opencv (cf-venv). Runs
     EasyOCR DETECTION-ONLY (reader.detect(), no recognition) on frames sampled
     at a low fps, keeps only detections inside the caption BAND (so animated
     graphic text elsewhere in the frame is NOT covered), and aggregates them
     into time INTERVALS each carrying ONE tight union box (absorbing the
     karaoke highlight sweep + small motion). Temporal hysteresis bridges 1-frame
     misses. Also samples a blend color per interval.

  2. build_cover_ass(...)        -- stdlib only (safe to import from the API
     venv / generate.py). Turns the intervals into an ASS file of \\p1 vector
     rectangles with fully-opaque fill on a LOW layer. Coordinates are scaled
     from detection resolution to the target render resolution; libass maps
     PlayResX/Y onto the frame, so 1:1 px when PlayRes == render res.

CLI (cf-venv worker convention, mirrors the other workers):
    cf-venv/python.exe caption_cover.py <input.json> <output.json>

input.json:  {"videoPath": "...", "sampleFps": 3.0, "band": [0.60, 0.99],
              "xMargin": 0.02, "minSamples": 1, "gapTolerance": 2,
              "padPx": null, "padFrac": 0.18, "ffmpegBin": "...",
              "colorSample": true}
output.json: {"videoW": W, "videoH": H, "srcW": W, "srcH": H,
              "sampleFps": f, "nSamples": N, "band": [...],
              "intervals": [{"start": s, "end": e, "box": [x, y, w, h],
                             "fill": "&Hbbggrr&", "nFrames": k}, ...]}

Notes / hard-won:
- ONE Reader instance is reused across all frames (EasyOCR's CRAFT detector
  leaks memory if a Reader is constructed per frame).
- Frames are extracted with the PROJECT ffmpeg (the source may be AV1, which
  opencv's VideoCapture cannot decode on this box) into a temp dir, then read
  with opencv from PNG.
- All Vietnamese-safe I/O: JSON is read/written utf-8; subprocess text uses
  encoding='utf-8', errors='replace'.
"""

import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path


# --------------------------------------------------------------------------- #
# Geometry helpers (pure, stdlib)                                              #
# --------------------------------------------------------------------------- #
def _union(boxes):
    """Union of [x, y, w, h] boxes -> single [x, y, w, h]. Empty -> None."""
    if not boxes:
        return None
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return [x0, y0, x1 - x0, y1 - y0]


def _iou(a, b):
    """Intersection-over-union of two [x, y, w, h] boxes."""
    ax0, ay0, ax1, ay1 = a[0], a[1], a[0] + a[2], a[1] + a[3]
    bx0, by0, bx1, by1 = b[0], b[1], b[0] + b[2], b[1] + b[3]
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ua = a[2] * a[3] + b[2] * b[3] - inter
    return inter / ua if ua > 0 else 0.0


# --------------------------------------------------------------------------- #
# DETECTION (cf-venv: easyocr + opencv)                                        #
# --------------------------------------------------------------------------- #
def _extract_frames(video_path, out_dir, sample_fps, ffmpeg_bin):
    """Extract frames at `sample_fps` into out_dir as f%06d.png via project ffmpeg.
    Returns the sorted list of PNG paths. Frame k (1-based) ~ time (k-1)/sample_fps."""
    os.makedirs(out_dir, exist_ok=True)
    pattern = os.path.join(out_dir, "f%06d.png")
    cmd = [
        ffmpeg_bin, "-v", "error", "-i", video_path,
        "-vf", f"fps={sample_fps}", "-fps_mode", "passthrough",
        pattern, "-y",
    ]
    subprocess.run(cmd, check=True, capture_output=True,
                   text=True, encoding="utf-8", errors="replace")
    return sorted(Path(out_dir).glob("f*.png"))


def _mk_progress(prog_file):
    """Return a write(pct, msg) that atomically updates the progress JSON file.

    Same contract as the ingest/tts workers: no-op when prog_file is None, and the
    host (generate._run_cf_worker) polls the file and forwards it to the job row.
    Detection is the DOMINANT cost of a translate_full render (minutes of EasyOCR),
    so without this the render bar has nothing real to report for that stretch."""
    if not prog_file:
        return lambda pct, msg: None

    def write(pct, msg):
        try:
            tmp = prog_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"pct": int(pct), "msg": msg}, f, ensure_ascii=False)
            os.replace(tmp, prog_file)  # atomic — host never reads a half-written file
        except Exception:
            pass

    return write


def _band_filter(det_boxes, h, band, x_margin, w, max_line_h_frac=0.055):
    """Keep detections that look like a burned CAPTION LINE inside the band.

    A detection is kept iff its vertical CENTER is inside `band` AND its height is
    within `max_line_h_frac` of the frame height. The height cap is the key
    graphic-text rejector: burned captions are a small, consistent font (~20-28px
    at 640h ≈ 0.03-0.044); animated graphic headlines that momentarily dip into
    the caption band are much taller (60px+), so the cap drops them while keeping
    real caption lines (incl. a 2-line caption, since each LINE is a separate
    detection here). `band` = [y0_frac, y1_frac]. det_boxes: [x_min,x_max,y_min,
    y_max] (EasyOCR .detect format). Returns [x, y, w, h]."""
    y0 = band[0] * h
    y1 = band[1] * h
    max_h = max_line_h_frac * h
    keep = []
    for b in det_boxes:
        xmin, xmax, ymin, ymax = float(b[0]), float(b[1]), float(b[2]), float(b[3])
        bw, bh = xmax - xmin, ymax - ymin
        if bw <= 1 or bh <= 1:
            continue
        yc = (ymin + ymax) / 2.0
        if not (y0 <= yc <= y1):
            continue
        if bh > max_h:            # too tall for a caption line -> graphic element
            continue
        keep.append([xmin, ymin, bw, bh])
    return keep


def _sample_fill_color(img, box, ring=6):
    """Median BGR of a thin ring just OUTSIDE the text box -> ASS '&Hbbggrr&'.
    Blends the opaque cover with the local video background. Falls back to black.
    img is a numpy HxWx3 BGR array."""
    try:
        import numpy as np
        H, W = img.shape[:2]
        x, y, w, h = [int(round(v)) for v in box]
        ox0, oy0 = max(0, x - ring), max(0, y - ring)
        ox1, oy1 = min(W, x + w + ring), min(H, y + h + ring)
        outer = img[oy0:oy1, ox0:ox1].reshape(-1, 3)
        # Inner (text) region mask removed by sampling only the border rows/cols.
        top = img[oy0:max(oy0 + ring, oy0 + 1), ox0:ox1].reshape(-1, 3)
        bot = img[max(0, oy1 - ring):oy1, ox0:ox1].reshape(-1, 3)
        left = img[oy0:oy1, ox0:max(ox0 + ring, ox0 + 1)].reshape(-1, 3)
        right = img[oy0:oy1, max(0, ox1 - ring):ox1].reshape(-1, 3)
        ring_px = np.concatenate([top, bot, left, right], axis=0) if outer.size else outer
        if ring_px.size == 0:
            return "&H000000&"
        med = np.median(ring_px, axis=0).astype(int)  # BGR
        b, g, r = int(med[0]), int(med[1]), int(med[2])
        return f"&H{b:02X}{g:02X}{r:02X}&"
    except Exception:
        return "&H000000&"


def detect_caption_boxes(video_path, sample_fps=3.0, band=(0.70, 0.92),
                         x_margin=0.02, gap_tolerance=2, min_samples=1,
                         pad_px=None, pad_frac=0.18, iou_merge=0.60,
                         max_line_h_frac=0.055, color_period_s=0.7,
                         color_sample=True, ffmpeg_bin=None, work_dir=None,
                         progress=None):
    """Detect burned-in caption boxes and aggregate them into time intervals.

    Returns dict: {videoW, videoH, srcW, srcH, sampleFps, nSamples, band,
                   intervals:[{start, end, box:[x,y,w,h], fill, nFrames}]}.
    Coordinates are in SOURCE pixels (srcW x srcH). `box` is already dilated by
    the pad. Intervals are contiguous, non-overlapping, sorted by start.

    - Reuses ONE easyocr.Reader (avoids the CRAFT per-instance memory leak).
    - detect-only (reader.detect); recognition is never run (faster, we only need
      geometry).
    - gap_tolerance: allow this many consecutive EMPTY samples inside a run
      before the interval is closed (temporal hysteresis).
    - pad_px overrides pad_frac; pad_frac dilates by that fraction of the box
      HEIGHT (default 0.18*h absorbs outline/glow leak). Verify empirically and
      bump if the re-OCR still finds text.
    """
    import cv2  # noqa
    import easyocr  # noqa

    progress = progress or (lambda pct, msg: None)
    ffmpeg_bin = ffmpeg_bin or os.getenv("FFMPEG_BIN", "ffmpeg")
    band = (float(band[0]), float(band[1]))

    owns_work = work_dir is None
    work_dir = work_dir or tempfile.mkdtemp(prefix="capcover_")
    frames_dir = os.path.join(work_dir, "frames")
    try:
        # Progress budget: 0-4% frame extraction, 4-6% model load, 6-99% the OCR loop
        # (the real cost, ~0.7s/frame), 100% on return.
        progress(1, "trích khung hình")
        frames = _extract_frames(video_path, frames_dir, sample_fps, ffmpeg_bin)
        if not frames:
            raise RuntimeError("no frames extracted")
        progress(4, "nạp mô hình OCR")

        # GPU if torch/CUDA present; EasyOCR falls back to CPU automatically.
        use_gpu = False
        try:
            import torch
            use_gpu = bool(torch.cuda.is_available())
        except Exception:
            pass
        reader = easyocr.Reader(["en", "vi"], gpu=use_gpu, verbose=False)
        progress(6, "dò phụ đề gốc")

        srcW = srcH = None
        n_frames = len(frames)
        last_pct = 6
        # Per-sample caption box (union of band-filtered detections) or None.
        per_sample = []   # list of (t, box_or_None, frame_path)
        for k, fp in enumerate(frames):
            # Real per-frame progress (the loop IS the cost) — emit only on change so
            # the host's poll/forward stays cheap.
            pct = 6 + int(93 * k / max(1, n_frames))
            if pct > last_pct:
                last_pct = pct
                progress(pct, f"dò phụ đề gốc {k}/{n_frames}")
            img = cv2.imread(str(fp))
            if img is None:
                per_sample.append(((k) / sample_fps, None, str(fp)))
                continue
            if srcW is None:
                srcH, srcW = img.shape[0], img.shape[1]
            # detect-only. EasyOCR returns (horizontal_list, free_list); with one
            # image each is nested one level.
            hlist, _free = reader.detect(
                img, min_size=10, text_threshold=0.5, low_text=0.3,
                link_threshold=0.4,
            )
            det = hlist[0] if hlist else []
            boxes = _band_filter(det, srcH, band, x_margin, srcW, max_line_h_frac)
            ub = _union(boxes)
            per_sample.append((k / sample_fps, ub, str(fp)))

        # Aggregate contiguous runs with hysteresis. A run is BROKEN not only by
        # too many empty samples but also when the caption box JUMPS (IoU vs the
        # last present box < split_iou) — i.e. the caption text/position changed.
        # This keeps each interval's union TIGHT (one caption line) instead of
        # ballooning to the max extent over the whole video.
        intervals = []
        run = []          # list of (t, box, frame_path) with box not None
        misses = 0
        step = 1.0 / sample_fps
        split_iou = 0.30

        def _flush(run):
            if not run or len(run) < min_samples:
                return
            boxes = [r[1] for r in run]
            ub = _union(boxes)
            # Dilate.
            pad = pad_px if pad_px is not None else max(2.0, pad_frac * ub[3])
            x = max(0.0, ub[0] - pad)
            y = max(0.0, ub[1] - pad)
            w = min(srcW - x, ub[2] + 2 * pad)
            h = min(srcH - y, ub[3] + 2 * pad)
            # Interval time: extend half a sample step on each side so coverage
            # does not flicker between samples.
            t_start = max(0.0, run[0][0] - step / 2.0)
            t_end = run[-1][0] + step / 2.0
            box = [round(x, 1), round(y, 1), round(w, 1), round(h, 1)]
            # Per-SLICE background color so the opaque bar TRACKS a changing
            # background (a single color over a long interval reads as a flat bar
            # when the scene behind it changes). Sample every ~color_period_s.
            slices = []
            if color_sample:
                n = len(run)
                per = max(1, int(round(color_period_s * sample_fps)))
                i = 0
                while i < n:
                    grp = run[i:i + per]
                    mid = grp[len(grp) // 2]
                    fill = "&H000000&"
                    img = cv2.imread(mid[2])
                    if img is not None:
                        fill = _sample_fill_color(img, box)
                    s0 = max(0.0, grp[0][0] - step / 2.0)
                    s1 = grp[-1][0] + step / 2.0
                    slices.append({"start": round(s0, 3), "end": round(s1, 3),
                                   "fill": fill})
                    i += per
                # stitch slice boundaries so there is no seam
                for j in range(1, len(slices)):
                    slices[j]["start"] = slices[j - 1]["end"]
                if slices:
                    slices[0]["start"] = round(t_start, 3)
                    slices[-1]["end"] = round(t_end, 3)
            intervals.append({
                "start": round(t_start, 3), "end": round(t_end, 3),
                "box": box, "fill": (slices[0]["fill"] if slices else "&H000000&"),
                "slices": slices, "nFrames": len(run),
            })

        for (t, box, fp) in per_sample:
            if box is not None:
                # Split the interval when the caption box jumps (new caption text
                # or moved position) so the union stays tight to one caption line.
                if run and _iou(run[-1][1], box) < split_iou:
                    _flush(run)
                    run = []
                run.append((t, box, fp))
                misses = 0
            else:
                if run:
                    misses += 1
                    if misses > gap_tolerance:
                        _flush(run)
                        run = []
                        misses = 0
        _flush(run)

        # Merge intervals whose boxes strongly overlap AND are time-adjacent
        # (bridges any residual fragmentation). Non-overlapping in time already.
        merged = []
        for iv in intervals:
            if merged and iv["start"] - merged[-1]["end"] <= step * (gap_tolerance + 1) \
               and _iou(merged[-1]["box"], iv["box"]) >= iou_merge:
                prev = merged[-1]
                ub = _union([prev["box"], iv["box"]])
                prev["box"] = [round(v, 1) for v in ub]
                prev["end"] = iv["end"]
                prev["nFrames"] += iv["nFrames"]
            else:
                merged.append(iv)

        # Absorb MICRO-intervals (transition fragments — a caption changing mid-
        # sample gives a tiny run with a narrow box that can let a word poke past
        # its edge). Union each such fragment into its temporally-nearest neighbor
        # so it inherits that (wider) box, and stitch the times so there is no
        # uncovered instant. Keeps the steady-state boxes tight while eliminating
        # the transition leak.
        micro_max = 2
        out2 = []
        for iv in merged:
            if iv["nFrames"] <= micro_max and (out2 or True):
                prev = out2[-1] if out2 else None
                # nearest neighbor: prefer the previous (extend its box+end),
                # else fold into the next by carrying the box forward.
                if prev is not None and iv["start"] - prev["end"] <= step * (gap_tolerance + 2):
                    ub = _union([prev["box"], iv["box"]])
                    prev["box"] = [round(v, 1) for v in ub]
                    prev["end"] = iv["end"]
                    prev["nFrames"] += iv["nFrames"]
                    continue
                # else carry forward: widen the NEXT interval to also cover this
                # fragment's box + start time (done lazily below by keeping it and
                # letting the next real interval absorb it).
                out2.append(iv)
            else:
                # If the immediately-previous kept item was a carried-forward micro
                # fragment, union it into this real interval and pull its start back.
                if out2 and out2[-1]["nFrames"] <= micro_max \
                   and iv["start"] - out2[-1]["end"] <= step * (gap_tolerance + 2):
                    frag = out2.pop()
                    ub = _union([frag["box"], iv["box"]])
                    iv["box"] = [round(v, 1) for v in ub]
                    iv["start"] = frag["start"]
                    iv["nFrames"] += frag["nFrames"]
                out2.append(iv)
        merged = out2

        return {
            "videoW": srcW, "videoH": srcH, "srcW": srcW, "srcH": srcH,
            "sampleFps": sample_fps, "nSamples": len(per_sample),
            "band": list(band), "intervals": merged,
        }
    finally:
        if owns_work:
            import shutil
            shutil.rmtree(work_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# ASS COVER BUILDER (stdlib only -- safe to import from the API venv)          #
# --------------------------------------------------------------------------- #
def _ass_time(t):
    if t < 0:
        t = 0.0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def build_cover_ass(intervals, src_w, src_h, out_path,
                    play_res_x=None, play_res_y=None, layer=0,
                    fixed_fill=None):
    """Write an ASS file of opaque cover rectangles from `intervals`.

    intervals: [{start, end, box:[x,y,w,h], fill:"&Hbbggrr&"(optional)}, ...] in
               SOURCE (src_w x src_h) pixel coordinates.
    play_res_x/y: PlayResX/Y for the file. Default = src_w/src_h (1:1). If the
               final render is a different size, pass the render W/H and the boxes
               are scaled accordingly (libass then maps PlayRes onto the frame).
    layer:     ASS layer for the cover. Keep LOW (0) so VN captions on a higher
               layer draw on top.
    fixed_fill: force one fill color for every box (e.g. "&H000000&"); otherwise
               each interval's own sampled 'fill' is used (fallback black).

    Returns out_path, or None if there are no intervals.
    Each rectangle is a \\p1 vector drawing with fully-opaque fill (\\1a&H00&) and
    no border/shadow. \\an7 + \\pos(x,y) places the drawing's (0,0) origin at the
    top-left corner so the m/l coordinates are the box size in px.
    """
    if not intervals:
        return None
    prx = int(play_res_x or src_w)
    pry = int(play_res_y or src_h)
    sx = prx / float(src_w)
    sy = pry / float(src_h)

    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {prx}\nPlayResY: {pry}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        # BorderStyle 1, Outline 0, Shadow 0 -> the fill is the only ink.
        "Style: Cover,Arial,20,&H00000000,&H00000000,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )

    def _rect_event(start, end, iw, ih, px, py, fill):
        # \an7 = top-left origin; \pos places (0,0) of the drawing; \1a&H00& fully
        # opaque; \1c fill color; \bord0\shad0 no edge; \p1 drawing mode.
        draw = f"m 0 0 l {iw} 0 l {iw} {ih} l 0 {ih}"
        text = (
            f"{{\\an7\\pos({px},{py})\\1c{fill}\\1a&H00&\\3a&HFF&\\4a&HFF&"
            f"\\bord0\\shad0\\p1}}{draw}{{\\p0}}"
        )
        return (f"Dialogue: {layer},{_ass_time(start)},{_ass_time(end)},"
                f"Cover,,0,0,0,,{text}")

    events = []
    for iv in intervals:
        x, y, w, h = iv["box"]
        x, y, w, h = x * sx, y * sy, w * sx, h * sy
        iw, ih = int(round(w)), int(round(h))
        px, py = round(x, 1), round(y, 1)
        base_fill = fixed_fill or iv.get("fill") or "&H000000&"
        # BASE event spans the whole interval (guarantees no uncovered instant even
        # after merges); per-slice events refine the color on top (later events on
        # the same layer draw over earlier ones in libass).
        events.append(_rect_event(iv["start"], iv["end"], iw, ih, px, py, base_fill))
        if not fixed_fill:
            for sl in iv.get("slices", []):
                events.append(_rect_event(sl["start"], sl["end"], iw, ih, px, py,
                                          sl.get("fill") or base_fill))

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(header + "\n".join(events) + "\n")
    return out_path


# --------------------------------------------------------------------------- #
# Worker entrypoint                                                            #
# --------------------------------------------------------------------------- #
def main():
    in_path, out_path = sys.argv[1], sys.argv[2]
    cfg = json.loads(Path(in_path).read_text(encoding="utf-8"))
    progress = _mk_progress(cfg.get("progressFile"))
    res = detect_caption_boxes(
        cfg["videoPath"],
        sample_fps=float(cfg.get("sampleFps", 3.0)),
        band=tuple(cfg.get("band", (0.60, 0.99))),
        x_margin=float(cfg.get("xMargin", 0.02)),
        gap_tolerance=int(cfg.get("gapTolerance", 2)),
        min_samples=int(cfg.get("minSamples", 1)),
        pad_px=cfg.get("padPx"),
        pad_frac=float(cfg.get("padFrac", 0.18)),
        max_line_h_frac=float(cfg.get("maxLineHFrac", 0.055)),
        color_period_s=float(cfg.get("colorPeriodS", 0.7)),
        color_sample=bool(cfg.get("colorSample", True)),
        ffmpeg_bin=cfg.get("ffmpegBin") or os.getenv("FFMPEG_BIN", "ffmpeg"),
        progress=progress,
    )
    Path(out_path).write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    progress(100, "dò phụ đề gốc xong")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
