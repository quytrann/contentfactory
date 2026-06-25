#!/usr/bin/env python
r"""voice_doctor.py — fast voice/TTS diagnostic for ContentFactory.

ONE command to surface why a clone voice misbehaves, WITHOUT loading any model.
Built after two slow-to-diagnose Tourist bugs:
  1. a long, LOW-DENSITY F5 reference (few words spread over many seconds) makes
     F5 echo the reference text instead of speaking gen_text;
  2. a STALE ref_text sidecar (voice re-cloned under the same name, transcript of
     the OLD clip served against the NEW audio) → F5 garbles / echoes.
Both now show up instantly in the static audit below.

Run with cf-venv python (only needs stdlib + ffprobe on PATH; whisper is used by
the optional --synth active check):

  E:\Installed\cf-venv\Scripts\python.exe tools\voice_doctor.py            # shared voices (all pages)
  ...voice_doctor.py --synth "Tourist - F5-TTS"                            # active PASS/FAIL
  ...voice_doctor.py --synth-all                                            # synth every voice
  ...voice_doctor.py --page "CTG Gaming"   # --page only labels the table; clones are SHARED now

NOTE: cloned voices are now SHARED by every page in <CONTENT_OUTPUT_ROOT>\_voices
(not per-page <page>\voice), so the audit scans that one shared dir.

STATIC AUDIT (default): read-only, instant. For each clone voice prints a row with
ref-clip probe (dur / sr / channels), char-density, ref_text sidecar state
(present? fingerprint match?), preview-cache state, and an OK / SUSPECT verdict.

ACTIVE CHECK (--synth / --synth-all): synthesizes a fixed short sentence through the
REAL tts_worker.py (same code path the pipeline uses), whisper-transcribes the result,
and prints PASS (transcript ~= intended) / FAIL (echoes ref / garbled / error) + timing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

# Vietnamese voice names must print without UnicodeEncodeError on the Windows
# console (cp1252). Force UTF-8 on our streams (PYTHONUTF8=1 also does this when set).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_env_file(path: str) -> None:
    """Populate os.environ from a KEY=VALUE .env file WITHOUT overriding anything
    already set in the real environment. Lets the tool reuse the API's FFmpeg /
    cf-venv / output-root paths so it works out of the box (binaries aren't on PATH)."""
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except OSError:
        pass


_load_env_file(os.path.join(_REPO_ROOT, "Dashboard", "api", ".env"))

# --- Config mirrored from Dashboard/api/generate.py + workers/tts_worker.py ----
# Kept in lockstep with the pipeline so the audit reflects what synthesis actually
# sees. If those constants change, update here.

CONTENT_OUTPUT_ROOT = os.getenv("CONTENT_OUTPUT_ROOT", r"E:\ContentFactory")
# Cloned voices are now SHARED by every page in one dir (mirrors generate.py::
# SHARED_VOICE_DIR), not per-page <root>/<page>/voice. The audit scans this dir.
SHARED_VOICE_DIR = os.path.join(CONTENT_OUTPUT_ROOT, "_voices")
CF_VENV_PYTHON = os.getenv("CF_VENV_PYTHON", r"E:\Installed\cf-venv\Scripts\python.exe")
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe")
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
# tools/ is at the repo root; the cf-venv workers live at Dashboard/api/workers.
WORKERS_DIR = os.path.join(_REPO_ROOT, "Dashboard", "api", "workers")

_AUDIO_EXTS = (".wav", ".flac", ".ogg", ".mp3")

# F5 ref hard cap (tts_worker._prep_f5_ref): a ref over this is silence-trimmed +
# capped before F5 sees it. Density is judged against the EFFECTIVE (capped) clip.
F5_REF_MAX_SEC = float(os.getenv("F5_REF_MAX_SEC", "6.0"))

# Char-density floor (chars of ref_text per second of EFFECTIVE ref audio). Below
# this, the F5 ref/gen length ratio blows up and F5 regenerates the reference text
# instead of gen_text (the Tourist echo bug). ~12 ch/s ≈ a normal speaking pace; a
# clip well under that is mostly silence/slow speech carrying too few characters.
DENSITY_FLOOR = float(os.getenv("VOICE_DOCTOR_DENSITY_FLOOR", "12.0"))

# Clone-engine suffix -> short name, from generate.py::_CLONE_MODEL_SHORT.
_CLONE_MODEL_SHORT = {
    "f5-tts": "F5-TTS", "vieneu": "VieNeu", "xtts-v2": "XTTS-v2",
    "openvoice-v2": "OpenVoice v2", "gpt-sovits": "GPT-SoVITS",
}

_PREVIEW_TEXT = "Xin chào, đây là giọng đọc mẫu cho kênh của bạn."
# Short active-check sentence (kept short so --synth is quick). Distinct words so an
# echo of the ref text (which is different) is obvious in the transcript compare.
_SYNTH_SENTENCE = "Hôm nay trời nắng đẹp, tôi đi dạo trong công viên."


# --- engine derivation (mirror of generate.py::_engine_from_clone_name) --------

def engine_from_name(ref_audio: str) -> str:
    stem = os.path.splitext(os.path.basename(ref_audio))[0]
    low = stem.lower()
    best_key, best_len = "vieneu", -1
    for key, short in _CLONE_MODEL_SHORT.items():
        suffix = " - " + short.lower()
        if low.endswith(suffix) and len(suffix) > best_len:
            best_key, best_len = key, len(suffix)
    return best_key


def engine_label(engine_key: str, stem: str) -> str:
    """Display label: F5-TTS / VieNeu / 'VieNeu (legacy)' for suffix-less files."""
    has_suffix = any(stem.lower().endswith(" - " + s.lower())
                     for s in _CLONE_MODEL_SHORT.values())
    if engine_key == "f5-tts":
        return "F5-TTS"
    return "VieNeu" if has_suffix else "legacy"


# --- fingerprint + sidecar (mirror of tts_worker) -----------------------------

def ref_fingerprint(ref_audio: str) -> str:
    try:
        st = os.stat(ref_audio)
        return f"{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        return ""


def sidecar_path(voice_dir: str, stem: str) -> str:
    return os.path.join(voice_dir, "_reftext", stem + ".txt")


def read_sidecar(path: str) -> tuple[bool, str | None, str]:
    """Return (exists, stored_fp_or_None, ref_text). stored_fp is None for a legacy
    (unfingerprinted) sidecar — which the worker treats as a cache MISS."""
    if not os.path.isfile(path):
        return False, None, ""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return True, None, ""
    first, _, rest = raw.partition("\n")
    if first.startswith("# fp:"):
        return True, first[len("# fp:"):].strip(), rest.strip()
    return True, None, raw.strip()  # legacy: whole file is the transcript


def preview_cache_path(voice_dir: str, ref_audio: str) -> str:
    """Mirror of generate.py::_preview_cache_path for a CLONE voice."""
    key = "clone_" + os.path.splitext(os.path.basename(ref_audio))[0]
    safe = "".join(c for c in key if c.isalnum() or c in (" ", "-", "_")).strip() or "voice"
    return os.path.join(voice_dir, "_previews", safe + ".wav")


# --- ffprobe ------------------------------------------------------------------

def probe_audio(path: str) -> dict:
    """duration / sample_rate / channels via one ffprobe call. Zeros on failure."""
    out = {"duration": 0.0, "sr": 0, "ch": 0}
    try:
        proc = subprocess.run(
            [FFPROBE_BIN, "-v", "error",
             "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate,channels:format=duration",
             "-of", "json", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        data = json.loads(proc.stdout or "{}")
        st = (data.get("streams") or [{}])[0]
        out["sr"] = int(st.get("sample_rate") or 0)
        out["ch"] = int(st.get("channels") or 0)
        out["duration"] = round(float((data.get("format") or {}).get("duration") or 0.0), 2)
    except (ValueError, OSError, json.JSONDecodeError, IndexError):
        pass
    return out


# --- text compare (for the active --synth check) ------------------------------

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFC", s or "").lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def token_overlap(a: str, b: str) -> float:
    """Fraction of the INTENDED tokens (a) that appear in the transcript (b)."""
    ta, tb = _norm(a).split(), set(_norm(b).split())
    if not ta:
        return 0.0
    return sum(1 for t in ta if t in tb) / len(ta)


# --- static audit -------------------------------------------------------------

def audit_voice(voice_dir: str, ref_audio: str) -> dict:
    stem = os.path.splitext(os.path.basename(ref_audio))[0]
    eng = engine_from_name(ref_audio)
    probe = probe_audio(ref_audio)
    cur_fp = ref_fingerprint(ref_audio)

    # Effective duration F5 actually sees (capped). Density is judged against this.
    eff_dur = probe["duration"]
    capped = False
    if eng == "f5-tts" and eff_dur > F5_REF_MAX_SEC:
        eff_dur = F5_REF_MAX_SEC
        capped = True

    sc_path = sidecar_path(voice_dir, stem)
    sc_exists, sc_fp, ref_text = read_sidecar(sc_path)

    # Sidecar staleness: a fingerprinted sidecar whose fp != current clip is STALE
    # (re-clone with same name). A legacy sidecar (no fp) is treated by the worker
    # as a MISS, so it WILL be re-transcribed — flag it so it isn't trusted as-is.
    sidecar_state = "missing"
    if sc_exists:
        if sc_fp is None:
            sidecar_state = "legacy"            # worker re-transcribes; not trusted
        elif sc_fp == cur_fp:
            sidecar_state = "match"
        else:
            sidecar_state = "stale"             # re-clone busted it

    # Char-density = ref_text chars per second of the audio that ref_text DESCRIBES.
    # The current pipeline transcribes ref_text from the CAPPED clip (so judge it
    # against eff_dur); a LEGACY sidecar was transcribed from the FULL clip, so its
    # chars correspond to the full duration. Using the matching denominator keeps the
    # ratio honest — this is the F5 ref/gen length signal that caught the Tourist bug.
    # Only a fingerprint-MATCHING sidecar reflects what F5 will actually see; a legacy
    # one is informational (it will be re-transcribed at synth), so we still compute it
    # but don't fail on it (the legacy flag already fires).
    density_dur = eff_dur if sidecar_state == "match" else probe["duration"]
    density = (len(ref_text) / density_dur) if (ref_text and density_dur > 0) else None

    # Preview cache state.
    pv_path = preview_cache_path(voice_dir, ref_audio)
    pv_exists = os.path.isfile(pv_path)
    pv_stale = False
    if pv_exists:
        try:
            pv_stale = os.path.getmtime(pv_path) < os.path.getmtime(ref_audio)
        except OSError:
            pass

    # Verdict.
    reasons: list[str] = []
    if probe["duration"] <= 0:
        reasons.append("ref clip unreadable / empty")
    # Low-density echo risk is a hard fault only when the ref_text F5 will USE (a
    # fingerprint-matching sidecar) is too sparse for its clip. Legacy/missing get
    # their own flag and are re-transcribed at synth, so don't double-penalize them.
    if eng == "f5-tts" and sidecar_state == "match" and density is not None \
            and density < DENSITY_FLOOR:
        # tts_worker compensates a low-density ref at synth time by speeding up
        # generation (speed = F5_SPEED_TARGET/density, clamped) so the inflated
        # frame budget can't drag a vowel / echo the ref. The flag still fires
        # because the CLIP itself is sparse — re-recording a denser ref is the
        # cleaner long-term fix, but synthesis is auto-corrected meanwhile.
        reasons.append(
            f"LOW DENSITY {density:.1f}ch/s (<{DENSITY_FLOOR:g}) "
            f"-> F5 drag/echo risk (auto speed-compensated at synth)")
    if sidecar_state == "stale":
        reasons.append("STALE ref_text (re-clone; fp mismatch)")
    if sidecar_state == "legacy":
        reasons.append("legacy ref_text (no fingerprint; not trusted)")
    if pv_stale:
        reasons.append("STALE PREVIEW (older than ref)")
    # F5 with no usable ref_text at all = will whisper at synth time (slow, and the
    # transcript drives correctness) — worth noting but not a hard fault.
    if eng == "f5-tts" and not ref_text and sidecar_state in ("missing", "legacy"):
        reasons.append("no cached ref_text (will transcribe on synth)")

    verdict = "OK" if not reasons else "SUSPECT"
    return {
        "stem": stem, "ref": ref_audio, "engine_key": eng,
        "engine": engine_label(eng, stem),
        "duration": probe["duration"], "eff_dur": round(eff_dur, 2), "capped": capped,
        "sr": probe["sr"], "ch": probe["ch"],
        "ref_text": ref_text, "density": density,
        "sidecar_state": sidecar_state, "sidecar_path": sc_path,
        "preview_exists": pv_exists, "preview_stale": pv_stale, "preview_path": pv_path,
        "verdict": verdict, "reasons": reasons,
    }


def find_voices(voice_dir: str) -> list[str]:
    if not os.path.isdir(voice_dir):
        return []
    return [os.path.join(voice_dir, fn) for fn in sorted(os.listdir(voice_dir))
            if os.path.isfile(os.path.join(voice_dir, fn))
            and fn.lower().endswith(_AUDIO_EXTS)]


def pages_with_voice() -> list[tuple[str, str]]:
    """Voices are now SHARED across all pages in one dir, so there is a single
    scan target. Returns one (label, voice_dir) entry pointing at SHARED_VOICE_DIR.

    Kept named pages_with_voice for call-site stability; the label reflects that the
    list is shared by every page rather than naming a single page."""
    if os.path.isdir(SHARED_VOICE_DIR):
        return [("(shared — all pages)", SHARED_VOICE_DIR)]
    return []


def _trunc(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def print_table(page: str, rows: list[dict]) -> None:
    print(f"\n=== {page}  ({len(rows)} voice{'s' if len(rows) != 1 else ''}) ===")
    if not rows:
        print("  (no clone voices on disk)")
        return
    hdr = f"  {'VOICE':<26} {'ENGINE':<7} {'DUR':>6} {'SR':>6} {'CH':>2} {'DENSITY':>9} {'SIDECAR':<8} {'PREVIEW':<8} VERDICT"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in rows:
        dur = f"{r['duration']:.1f}s" + ("*" if r["capped"] else "")
        dens = "-" if r["density"] is None else f"{r['density']:.1f}ch/s"
        pv = "stale" if r["preview_stale"] else ("ok" if r["preview_exists"] else "none")
        verdict = r["verdict"]
        if r["reasons"]:
            verdict += " (" + "; ".join(r["reasons"]) + ")"
        print(f"  {_trunc(r['stem'], 26):<26} {r['engine']:<7} {dur:>6} "
              f"{(str(r['sr']) if r['sr'] else '-'):>6} {(str(r['ch']) if r['ch'] else '-'):>2} "
              f"{dens:>9} {r['sidecar_state']:<8} {pv:<8} {verdict}")
    print("  * = duration capped to F5_REF_MAX_SEC for F5; density judged on the capped clip.")


# --- active synth check -------------------------------------------------------

def _run_worker(script: str, payload: dict, timeout: int) -> dict:
    """Run a cf-venv worker on a JSON payload via temp files (same contract the API
    uses). Sets the F5 env (PYTHONUTF8, ffmpeg on PATH). Raises on non-zero exit."""
    worker = os.path.join(WORKERS_DIR, script)
    if not os.path.isfile(worker):
        raise FileNotFoundError(f"worker not found: {worker}")
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("HF_HUB_OFFLINE", "1")
    env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    env.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    env.setdefault("FFMPEG_BIN", FFMPEG_BIN)
    env.setdefault("FFPROBE_BIN", FFPROBE_BIN)
    ff_dir = os.path.dirname(FFMPEG_BIN) if os.path.isfile(FFMPEG_BIN) else ""
    if ff_dir and os.path.isdir(ff_dir):
        env["PATH"] = ff_dir + os.pathsep + env.get("PATH", "")
    with tempfile.TemporaryDirectory() as td:
        in_p = os.path.join(td, "in.json")
        out_p = os.path.join(td, "out.json")
        Path(in_p).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(
            [CF_VENV_PYTHON, worker, in_p, out_p],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=timeout,
        )
        if proc.returncode != 0 or not os.path.isfile(out_p):
            raise RuntimeError((proc.stderr or "").strip()[-600:] or "worker failed (no stderr)")
        return json.loads(Path(out_p).read_text(encoding="utf-8"))


def synth_check(row: dict, page: str) -> dict:
    """Synthesize the fixed sentence for one voice via tts_worker.py, transcribe it
    via whisper_worker.py, and judge PASS/FAIL. Returns a result dict."""
    eng = row["engine_key"]
    intended = _SYNTH_SENTENCE
    t0 = time.time()
    res = {"stem": row["stem"], "engine": row["engine"], "passed": False,
           "detail": "", "elapsed": 0.0}
    try:
        with tempfile.TemporaryDirectory() as out_dir:
            tts_payload = {
                "items": [{"scene": 0, "text": intended}],
                "engine": eng,
                "voice": None,
                "refAudio": row["ref"],
                "emotion": "natural",
                "applyWatermark": False,
                "outDir": out_dir,
            }
            # F5 cold-load + GPU inference is slower; give it room.
            tts_to = 420 if eng == "f5-tts" else 240
            tts_out = _run_worker("tts_worker.py", tts_payload, timeout=tts_to)
            wav = tts_out["results"][0]["audioPath"]

            wh_out = _run_worker("whisper_worker.py", {
                "items": [{"scene": 0, "audioPath": wav}],
                "model": os.getenv("WHISPER_MODEL", "medium"),
                "device": os.getenv("WHISPER_DEVICE", "cpu"),
                "compute": os.getenv("WHISPER_COMPUTE", "int8"),
                "language": "vi", "wordTimestamps": False,
            }, timeout=300)
            segs = wh_out["results"][0]["segments"]
            transcript = " ".join(s["text"].strip() for s in segs).strip()

        ov_intended = token_overlap(intended, transcript)
        ov_ref = token_overlap(row["ref_text"], transcript) if row["ref_text"] else 0.0
        res["elapsed"] = round(time.time() - t0, 1)
        res["transcript"] = transcript
        # PASS = transcript matches the intended sentence and does NOT look like the
        # reference text being echoed back.
        if ov_intended >= 0.6 and ov_intended >= ov_ref:
            res["passed"] = True
            res["detail"] = f"intended~{ov_intended:.0%}"
        else:
            why = "echoes ref" if (ov_ref > ov_intended and ov_ref >= 0.5) else "garbled/mismatch"
            res["detail"] = f"{why} (intended~{ov_intended:.0%}, ref~{ov_ref:.0%})"
    except Exception as exc:  # noqa: BLE001 — report, don't crash the run
        res["elapsed"] = round(time.time() - t0, 1)
        res["detail"] = f"ERROR: {str(exc)[-200:]}"
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="Fast voice/TTS diagnostic for ContentFactory.")
    ap.add_argument("--page", default=None,
                    help='Page label only. Clone voices are SHARED across all pages now, '
                         'so this no longer scopes which voices are scanned — it just '
                         'labels the table. Default label: "(shared — all pages)".')
    ap.add_argument("--synth", default=None, metavar="VOICE",
                    help="Active check: synth+transcribe this voice (display name / stem).")
    ap.add_argument("--synth-all", action="store_true",
                    help="Active check on every voice (slow; loads models).")
    args = ap.parse_args()

    # Clones are shared across all pages, so we always scan SHARED_VOICE_DIR. The
    # --page flag only changes the table label (back-compat; it no longer scopes).
    if args.page:
        if not os.path.isdir(SHARED_VOICE_DIR):
            print(f"No shared voice dir at {SHARED_VOICE_DIR}")
            return 1
        pages = [(f"{args.page}  (shared — all pages)", SHARED_VOICE_DIR)]
    else:
        pages = pages_with_voice()
        if not pages:
            print(f"No shared voice dir at {SHARED_VOICE_DIR}")
            return 1

    all_rows: dict[str, list[dict]] = {}
    suspect_total = 0
    for page, vdir in pages:
        rows = [audit_voice(vdir, ref) for ref in find_voices(vdir)]
        all_rows[page] = rows
        suspect_total += sum(1 for r in rows if r["verdict"] == "SUSPECT")
        print_table(page, rows)

    print(f"\nStatic audit: {sum(len(r) for r in all_rows.values())} voice(s), "
          f"{suspect_total} SUSPECT.")

    # Active check (opt-in).
    targets: list[tuple[str, dict]] = []
    if args.synth_all:
        for page, rows in all_rows.items():
            targets += [(page, r) for r in rows]
    elif args.synth:
        want = args.synth.lower()
        for page, rows in all_rows.items():
            for r in rows:
                if r["stem"].lower() == want or r["stem"].lower().startswith(want):
                    targets.append((page, r))
        if not targets:
            print(f"\n--synth: no voice matching '{args.synth}' found.")
            return 2

    if targets:
        print(f"\n=== ACTIVE SYNTH CHECK ({len(targets)} voice(s)) ===")
        print(f'  intended: "{_SYNTH_SENTENCE}"')
        n_fail = 0
        for page, r in targets:
            print(f"  synth {r['stem']} [{r['engine']}] ...", flush=True)
            sr = synth_check(r, page)
            status = "PASS" if sr["passed"] else "FAIL"
            if not sr["passed"]:
                n_fail += 1
            print(f"    {status} ({sr['elapsed']}s) — {sr['detail']}")
            if sr.get("transcript"):
                print(f"      transcript: {_trunc(sr['transcript'], 100)}")
        print(f"\nActive check: {len(targets) - n_fail} PASS / {n_fail} FAIL.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
