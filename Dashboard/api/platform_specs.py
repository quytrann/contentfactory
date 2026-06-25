"""Per-platform upload-spec reference for the dashboard "Đăng tải" view.

This is a READ-ONLY reference surface: it tells creators each platform's upload
rules (container, aspect, resolution, duration, file size, codecs) so they can
fix a file before publishing. No credentials, no secrets, no network calls.

Single source of truth rule:
  - FACEBOOK is derived DIRECTLY from the real enforced constants in
    facebook_upload.py (the same numbers check_reel_spec() validates).
    enforced = True.
  - YOUTUBE / TIKTOK / INSTAGRAM are NOW validated before upload by the LENIENT
    generic validator in media_spec.py (publish_core runs it for each
    _publish_<platform>). So enforced = True, and the HARD-limit fields below are
    derived DIRECTLY from media_spec.RULES_* — what the dashboard shows can never
    drift from what the pipeline actually rejects.

HONESTY about recommended vs enforced (the reason for the new hard-limit fields):
  The human-readable `aspectRatio` / `resolution` / `maxDurationS` strings show a
  RECOMMENDED short profile (e.g. YouTube "9:16, 180s") — good production guidance.
  But the LENIENT validator only HARD-rejects the REAL platform ceilings (YouTube
  12h, any aspect). Showing only the recommended numbers next to an `enforced`
  badge would LIE about what is actually gated. So each spec now also carries
  explicit hard-limit fields the frontend can render as "truly rejected vs merely
  recommended":
    - hardMaxDurationS     : float  — duration over this is REJECTED (422).
    - hardMinDurationS     : float  — duration <= this is REJECTED.
    - enforceAspect        : bool   — true => non ~9:16 is REJECTED (Reels only).
    - gatedContainers      : [str]  — containers actually accepted at the gate.
    - gatedVcodecs         : [str]  — video codecs actually accepted at the gate.
    - requireAudioAac      : bool   — true => an AAC audio track is REQUIRED (FB only).
  Existing fields (aspectRatio, resolution, maxDurationS, minDurationS, containers,
  vcodecs, acodecs, maxFileMb, enforced, notes, label, platform) are kept for
  backward compatibility — the new fields are additive.

The route returns camelCase JSON to match the frontend TS types.
"""

from __future__ import annotations

import facebook_upload as _fb
import media_spec as _ms


# ---------------------------------------------------------------------------
# Video length TIERS (short / mid / long) per platform — GUIDANCE ONLY.
#
# Source: _workspace/research_video_tiers.md (researcher, June 2026, READY).
# These describe which DURATION BAND a video falls in and what that band means
# for UPLOAD classification + MONETIZATION eligibility (ad type / mid-roll /
# watch-hour counting). They are advisory length/monetization guidance — NOT
# new reject gates. The enforced validators (facebook_upload.py / media_spec.py)
# remain the ONLY source of truth for rejection; nothing here gates an upload.
#
# Contract per tier (camelCase, consumed by the frontend "Đăng tải" view):
#   { key: "short"|"mid"|"long", label: str,
#     minDurationS: float|None, maxDurationS: float|None, note: str }
#   - minDurationS / maxDurationS are SECONDS; null = no hard bound for the band
#     (use the platform's real upload ceiling from media_spec for actual limits).
#   - `note` and `label` are USER-FACING Vietnamese guidance (monetization / ad /
#     mid-roll meaning), per the project's Vietnamese-for-end-users rule.
#
# Confidence (from the research file): YouTube boundaries are HIGH (official
# support.google.com); TikTok >=60s and Facebook 60s/180s are MEDIUM (official
# rule, JS-only help pages, corroborated by 2026 aggregators); the TikTok 3600s
# and FB 3-min mid-roll boundaries are LOW — noted inline in the Vietnamese text.
_TIERS: dict[str, list[dict]] = {
    "youtube": [
        # Vertical/square + <=180s qualifies as a Short -> Shorts ad pool (~45%).
        {"key": "short", "label": "Shorts", "minDurationS": 1.0, "maxDurationS": 180.0,
         "note": "Dọc/vuông và <=180s => tính là Shorts. Ăn tiền từ quỹ quảng cáo Shorts "
                 "(~45% doanh thu). Tính cho lộ trình YPP 10 triệu view/90 ngày, NHƯNG "
                 "view Shorts KHÔNG tính vào mốc 4.000 giờ xem. Khung 9:16, <=1080p."},
        # Long-form under 8 min: counts toward watch hours but NO mid-roll.
        {"key": "mid", "label": "Long-form (chưa có mid-roll)", "minDurationS": 181.0, "maxDurationS": 479.0,
         "note": "Video dài 16:9. Doanh thu quảng cáo chuẩn (~55%) và giờ xem ĐƯỢC tính vào "
                 "mốc 4.000 giờ YPP. Nhưng dưới 8 phút chỉ chạy được quảng cáo đầu/cuối/overlay "
                 "— CHƯA có mid-roll."},
        # >=8 min unlocks mid-roll, the biggest YouTube revenue lever.
        {"key": "long", "label": "Long-form (đủ điều kiện mid-roll)", "minDurationS": 480.0, "maxDurationS": 43200.0,
         "note": ">=8 phút (480s) mở khóa quảng cáo MID-ROLL — đòn bẩy doanh thu lớn nhất của "
                 "YouTube. Chia ~55%; giờ xem tính vào mốc 4.000 giờ. Giới hạn upload 12 giờ/256GB "
                 "(15 phút cho tới khi tài khoản được xác minh). Nên dùng 16:9."},
    ],
    "tiktok": [
        # <1 min cannot earn under Creator Rewards no matter the views.
        {"key": "short", "label": "Ngắn (không kiếm tiền)", "minDurationS": 1.0, "maxDurationS": 59.0,
         "note": "Vẫn phát/viral bình thường nhưng KHÔNG đủ điều kiện nhận tiền Creator Rewards "
                 "(luật loại video <1 phút). Thuật toán thường ưu ái ~21-34s. Dùng cho tiếp cận/"
                 "phễu, không phải để kiếm tiền."},
        # >=1 min is the monetizable band under Creator Rewards.
        {"key": "mid", "label": "Đủ điều kiện Rewards", "minDurationS": 60.0, "maxDurationS": 600.0,
         "note": ">=1 phút => đủ điều kiện Creator Rewards (video gốc, chất lượng, không phải "
                 "Duet/Stitch). Đây là dải kiếm tiền; tỉ lệ xem hết + thời lượng xem càng cao trả "
                 "càng nhiều. Quay trong app tối đa ~10 phút."},
        # Web upload up to ~60 min; exact ceiling LOW confidence.
        {"key": "long", "label": "Upload dài", "minDurationS": 601.0, "maxDurationS": 3600.0,
         "note": "Video upload qua web có thể dài tới ~60 phút và vẫn đủ điều kiện Rewards; nội "
                 "dung dài xem hết cao đôi khi đạt RPM tốt nhất. Trần phụ thuộc khu vực (mốc 3600s "
                 "ĐỘ TIN THẤP — giá trị của trình upload web, chưa có nguồn chính thức chốt)."},
    ],
    "facebook": [
        # Reels: short-form surface; monetization is page-level, not per-video length.
        {"key": "short", "label": "Reels", "minDurationS": 3.0, "maxDurationS": 90.0,
         "note": "Reels dọc 9:16 ngắn. Bề mặt Reels tối ưu cho <=90s (\"đẹp nhất\" 15-60s). Kiếm "
                 "tiền qua Content Monetization / quảng cáo Reels (xét ở cấp Trang), không theo độ "
                 "dài từng video. Tối thiểu ~3s (pipeline loại thẳng nếu ngắn hơn)."},
        # In-stream >1 min: pre/post only, no mid-roll under 3 min.
        {"key": "mid", "label": "In-stream (chỉ đầu/cuối)", "minDurationS": 61.0, "maxDurationS": 179.0,
         "note": "Video feed/VOD >1 phút mới đủ điều kiện gắn quảng cáo in-stream, nhưng DƯỚI 3 phút "
                 "chỉ chạy được quảng cáo đầu/cuối — CHƯA có mid-roll. Trang còn phải đạt ngưỡng "
                 "follower/phút-xem (vd 5.000 follower + 60.000 phút/60 ngày)."},
        # >=3 min unlocks mid-roll ad breaks (LOW confidence on exact boundary).
        {"key": "long", "label": "In-stream (đủ điều kiện mid-roll)", "minDurationS": 180.0, "maxDurationS": None,
         "note": ">=3 phút (180s) mở khóa các điểm chèn quảng cáo MID-ROLL — đòn bẩy doanh thu video "
                 "chính của Facebook. Không có giới hạn độ dài cứng cho video thường (~4GB là giới hạn "
                 "thực tế). Mốc mid-roll 3 phút ĐỘ TIN THẤP; ngưỡng sàn in-stream 1 phút thì chắc chắn."},
    ],
    "instagram": [
        # Reels recommended surface; ad-share is page/invite-level, not per-video length.
        {"key": "short", "label": "Reels (khuyến nghị)", "minDurationS": 3.0, "maxDurationS": 90.0,
         "note": "Reels dọc 9:16 ngắn — bề mặt chia sẻ doanh thu quảng cáo (~55% cho creator). Tối ưu "
                 "<=90s; xem hết càng nhiều trả càng cao. Tối thiểu ~3s (loại thẳng nếu ngắn hơn)."},
        # Still a Reel but nearing the recommend-ability cliff at 3 min.
        {"key": "mid", "label": "Reels (mở rộng)", "minDurationS": 91.0, "maxDurationS": 180.0,
         "note": "Vẫn là Reel và vẫn đủ điều kiện chia doanh thu quảng cáo, nhưng đang tiến sát giới hạn "
                 "đề xuất. Reels >3 phút KHÔNG được chủ động đề xuất tới người chưa follow => tiếp cận "
                 "(và doanh thu quảng cáo) giảm dần."},
        # Up to the 20-min camera-roll ceiling; >3 min has weak organic reach.
        {"key": "long", "label": "Reel dài / video feed", "minDurationS": 181.0, "maxDurationS": 1200.0,
         "note": "Tới trần upload 20 phút từ camera-roll. Được chấp nhận, nhưng >3 phút không được đề "
                 "xuất tới người xem mới => tiếp cận tự nhiên yếu; coi như nội dung kho/feed, không phải "
                 "đòn bẩy tăng trưởng/kiếm tiền."},
    ],
}


def _fmt_resolution(reco: str, min_w: int, min_h: int,
                    max_w: int | None = None, max_h: int | None = None) -> str:
    """Human-readable resolution line, e.g. '1080x1920 (min 540x960, max 1920x1920)'."""
    parts = [f"min {min_w}x{min_h}"]
    if max_w and max_h:
        parts.append(f"max {max_w}x{max_h}")
    return f"{reco} ({', '.join(parts)})"


def _facebook_spec() -> dict:
    """Build the Facebook Reels spec from the LIVE facebook_upload constants.

    Facebook is UNCHANGED: it keeps its own stricter check_reel_spec (h264-only,
    9:16 enforced, AAC required, 3..90s). The hard-limit fields below are derived
    from the same constants check_reel_spec() enforces.
    """
    return {
        "platform": "facebook",
        "label": "Facebook Reels",
        "containers": ["mp4", "mov"],          # check_reel_spec(): ext in (.mp4,.mov)
        "aspectRatio": "9:16",                 # REELS_TARGET_AR = 9/16 (±REELS_AR_TOLERANCE)
        "resolution": _fmt_resolution(
            "1080x1920",
            _fb.REELS_MIN_WIDTH, _fb.REELS_MIN_HEIGHT,
            _fb.REELS_MAX_WIDTH, _fb.REELS_MAX_HEIGHT,
        ),
        "minDurationS": _fb.REELS_MIN_DURATION,   # 3.0
        "maxDurationS": _fb.REELS_MAX_DURATION,   # 90.0
        "maxFileMb": None,                        # not enforced by check_reel_spec()
        # ALLOWED_VCODECS = {h264, avc1} (avc1 == h264 tag) -> show single name.
        "vcodecs": ["h264"],
        "acodecs": sorted(_fb.ALLOWED_ACODECS),   # {aac} -> ["aac"]
        "enforced": True,
        # --- explicit hard-limit fields (what is REALLY rejected) ---
        "hardMaxDurationS": _fb.REELS_MAX_DURATION,   # >90s rejected
        "hardMinDurationS": _fb.REELS_MIN_DURATION,   # <3s rejected
        "enforceAspect": True,                        # Reels: non ~9:16 rejected
        "gatedContainers": ["mp4", "mov"],
        "gatedVcodecs": ["h264"],                     # h264/avc1 tag; FB stays h264-only
        "requireAudioAac": True,                      # FB needs an AAC track
        # Advisory length/monetization tiers (NOT a reject gate; see _TIERS).
        "tiers": _TIERS["facebook"],
        "notes": (
            f"Enforced by the pipeline before upload (check_reel_spec): values here "
            f"are the live facebook_upload constants, not generic guidance. "
            f"Aspect tolerance ±{_fb.REELS_AR_TOLERANCE}; needs an AAC audio track. "
            f">=30fps recommended."
        ),
    }


def _from_rules(rules: _ms.SpecRules, *, label: str, aspect_reco: str,
                resolution_reco: str, reco_max_duration_s: float,
                file_mb: float | None, acodecs: list[str], notes: str) -> dict:
    """Build a spec dict for a media_spec-validated platform.

    The RECOMMENDED display strings (aspectRatio/resolution/maxDurationS) are
    curated guidance; the HARD-limit fields are pulled straight from `rules` so
    the enforced badge is honest. h264/avc1 are the same codec -> shown once as
    'h264' in the gated list, but the recommended `vcodecs` keeps the full set.
    """
    gated_vcodecs = sorted({("h264" if v in ("h264", "avc1") else v) for v in rules.vcodecs})
    return {
        "platform": rules.platform,
        "label": label,
        "containers": list(rules.containers),
        "aspectRatio": aspect_reco,
        "resolution": resolution_reco,
        "minDurationS": 1.0,                       # recommended floor (display)
        "maxDurationS": reco_max_duration_s,       # RECOMMENDED short length (display)
        "maxFileMb": file_mb,                      # guidance; not gated by media_spec
        "vcodecs": list(rules.vcodecs),
        "acodecs": acodecs,
        "enforced": True,
        # --- explicit hard-limit fields (what is REALLY rejected by media_spec) ---
        "hardMaxDurationS": rules.hard_max_duration_s,
        "hardMinDurationS": rules.min_duration_s,  # duration <= this is rejected
        "enforceAspect": rules.enforce_aspect,     # non ~9:16 rejected only if true
        "gatedContainers": list(rules.containers),
        "gatedVcodecs": gated_vcodecs,
        "requireAudioAac": False,                  # audio NOT required (lenient)
        # Advisory length/monetization tiers (NOT a reject gate; see _TIERS).
        # Falls back to [] for any platform without a curated tier table.
        "tiers": _TIERS.get(rules.platform, []),
        "notes": notes,
    }


def get_platform_specs() -> dict:
    """Assemble the full spec list. Facebook first, then the media_spec-validated
    platforms. All four are now enforced=True."""
    youtube = _from_rules(
        _ms.RULES_YOUTUBE,
        label="YouTube Shorts",
        aspect_reco="9:16",                        # recommended for the Shorts shelf
        resolution_reco="1080x1920 (max 1080p for Shorts)",
        reco_max_duration_s=180.0,                 # recommended Shorts length
        file_mb=262144.0,                          # 256 GB account cap (guidance)
        acodecs=["aac"],
        notes=(
            "Enforced (lenient): rejects only the REAL ceilings — container, video "
            "codec, and duration > 12h (43200s). Aspect is NOT gated (any orientation "
            "uploads; vertical+<=180s is just what qualifies as a Short). MP4+H.264+AAC, "
            "9:16, <=180s recommended for Shorts."
        ),
    )
    tiktok = _from_rules(
        _ms.RULES_TIKTOK,
        label="TikTok",
        aspect_reco="9:16",
        resolution_reco="1080x1920 (min 720x1280)",
        reco_max_duration_s=60.0,                  # recommended/sweet length (display)
        file_mb=4096.0,                            # web uploader cap (guidance)
        acodecs=["aac"],
        notes=(
            "Enforced (lenient): rejects only container, video codec, and duration > 60min "
            "(3600s). Aspect is NOT gated (9:16/1:1/16:9 all play). File cap shown is the "
            "web uploader's (~4GB); app caps are far smaller. Algorithm favors ~21-34s."
        ),
    )
    instagram = _from_rules(
        _ms.RULES_INSTAGRAM,
        label="Instagram Reels",
        aspect_reco="9:16",
        resolution_reco="1080x1920 (min 720x1280)",
        reco_max_duration_s=180.0,                 # >180s not recommended to new audiences
        file_mb=4096.0,
        acodecs=["aac"],
        notes=(
            "Enforced (lenient + Reels aspect): rejects container, video codec, duration > "
            "20min (1200s, the documented camera-roll ceiling), AND non ~9:16 portrait "
            "(Reels are a portrait-only surface). >180s is not recommended to new audiences; "
            "30fps standard."
        ),
    )
    return {"specs": [_facebook_spec(), youtube, tiktok, instagram]}

