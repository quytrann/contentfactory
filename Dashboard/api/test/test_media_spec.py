"""Unit tests for the generic lenient pre-upload validator (media_spec.check_spec_info).

Pure tests: synthetic ffprobe-info dicts fed straight into check_spec_info() — no
ffprobe, no files, no network. Covers portrait-ok, landscape-on-reels-reject,
over-duration-reject, bad-container-reject, plus the lenient YT/TikTok behavior.

Run:  cd Dashboard/api && .venv/Scripts/python -m pytest test/test_media_spec.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import media_spec as ms  # noqa: E402


def _info(width, height, vcodec="h264", duration=30.0, acodec="aac", has_video=True):
    ar = (width / height) if height else 0.0
    return {
        "has_video": has_video, "width": width, "height": height,
        "vcodec": vcodec, "duration": duration, "acodec": acodec,
        "aspect_ratio": round(ar, 4),
    }


# --- portrait-ok: a 1080x1920 9:16 h264 mp4 passes ALL four rule sets ---------
def test_portrait_passes_all_platforms():
    info = _info(1080, 1920, duration=30.0)
    for rules in (ms.RULES_YOUTUBE, ms.RULES_TIKTOK, ms.RULES_INSTAGRAM):
        r = ms.check_spec_info(info, "mp4", rules)
        assert r["ok"], f"{rules.platform}: {r.get('reason')}"


# --- landscape-on-reels-reject: 1920x1080 rejected by IG (Reels), ok on YT/TikTok ---
def test_landscape_rejected_on_reels_only():
    info = _info(1920, 1080, duration=400.0)
    # Instagram = Reels surface -> aspect gated -> reject.
    ig = ms.check_spec_info(info, "mp4", ms.RULES_INSTAGRAM)
    assert not ig["ok"]
    assert "portrait" in ig["reason"] or "9:16" in ig["reason"]
    # YouTube + TikTok accept any orientation -> pass (within their hard maxes).
    assert ms.check_spec_info(info, "mp4", ms.RULES_YOUTUBE)["ok"]
    assert ms.check_spec_info(info, "mp4", ms.RULES_TIKTOK)["ok"]


# --- over-duration-reject: each platform's REAL hard ceiling --------------------
def test_over_duration_rejected_per_platform():
    # TikTok hard max = 3600s. 3601s rejected; 3599s ok.
    over_tt = _info(1080, 1920, duration=3601.0)
    assert not ms.check_spec_info(over_tt, "mp4", ms.RULES_TIKTOK)["ok"]
    ok_tt = _info(1080, 1920, duration=3599.0)
    assert ms.check_spec_info(ok_tt, "mp4", ms.RULES_TIKTOK)["ok"]

    # Instagram hard max = 1200s.
    over_ig = _info(1080, 1920, duration=1201.0)
    assert not ms.check_spec_info(over_ig, "mp4", ms.RULES_INSTAGRAM)["ok"]

    # YouTube hard max = 43200s (12h) — a 2h file is fine.
    long_yt = _info(1920, 1080, duration=7200.0)
    assert ms.check_spec_info(long_yt, "mp4", ms.RULES_YOUTUBE)["ok"]
    # but 12h + 1s is rejected.
    over_yt = _info(1920, 1080, duration=43201.0)
    assert not ms.check_spec_info(over_yt, "mp4", ms.RULES_YOUTUBE)["ok"]


# --- bad-container-reject -------------------------------------------------------
def test_bad_container_rejected():
    info = _info(1080, 1920)
    r = ms.check_spec_info(info, "mkv", ms.RULES_YOUTUBE)
    assert not r["ok"]
    assert "container" in r["reason"]
    # webm ok on YouTube/TikTok, NOT on Instagram (mp4/mov only).
    assert ms.check_spec_info(info, "webm", ms.RULES_YOUTUBE)["ok"]
    assert ms.check_spec_info(info, "webm", ms.RULES_TIKTOK)["ok"]
    assert not ms.check_spec_info(info, "webm", ms.RULES_INSTAGRAM)["ok"]


# --- bad video codec rejected; hevc accepted (lenient) --------------------------
def test_codec_policy():
    bad = _info(1080, 1920, vcodec="prores")
    assert not ms.check_spec_info(bad, "mov", ms.RULES_YOUTUBE)["ok"]
    # hevc is accepted on all three (lenient, avoid false rejects).
    hevc = _info(1080, 1920, vcodec="hevc")
    for rules in (ms.RULES_YOUTUBE, ms.RULES_TIKTOK, ms.RULES_INSTAGRAM):
        assert ms.check_spec_info(hevc, "mp4", rules)["ok"], rules.platform


# --- audio NOT required for YT/TikTok/IG (lenient) ------------------------------
def test_audio_not_required():
    no_audio = _info(1080, 1920, acodec=None)
    no_audio["acodec"] = None
    for rules in (ms.RULES_YOUTUBE, ms.RULES_TIKTOK, ms.RULES_INSTAGRAM):
        assert ms.check_spec_info(no_audio, "mp4", rules)["ok"], rules.platform


# --- missing video stream rejected ----------------------------------------------
def test_no_video_stream_rejected():
    info = {"has_video": False}
    for rules in (ms.RULES_YOUTUBE, ms.RULES_TIKTOK, ms.RULES_INSTAGRAM):
        r = ms.check_spec_info(info, "mp4", rules)
        assert not r["ok"]
        assert "video stream" in r["reason"]


# --- zero/negative duration rejected (sane floor) -------------------------------
def test_zero_duration_rejected():
    info = _info(1080, 1920, duration=0.0)
    for rules in (ms.RULES_YOUTUBE, ms.RULES_TIKTOK, ms.RULES_INSTAGRAM):
        assert not ms.check_spec_info(info, "mp4", rules)["ok"], rules.platform
