"""Friendly, user-facing failure messages for pipeline/worker errors.

WHY
---
When a cf-venv worker (yt-dlp download/ingest, whisper, TTS, ...) dies, its raw
Python traceback used to be copied verbatim into ``jobs.error`` and rendered in the
dashboard. The owner saw things like::

    download_worker.py failed (exit=1): n <lambda>
        error=IDENTITY if not fatal else lambda e: self.report_error(...)
      File "E:\\Installed\\cf-venv\\Lib\\site-packages\\yt_dlp\\YoutubeDL.py", ...
    yt_dlp.utils.DownloadError: ERROR: [download] Got error: (<HTTPSConnection(
    host='rr2---sn-ojnpo5-c3.googlevideo.com', ...)>, 'Connection ... timed out.
    (connect timeout=20.0)'). Giving up after 10 retries

...which says nothing actionable to a non-programmer.

WHAT THIS DOES
--------------
A single, table-driven classifier: regex rules over the worker's stderr map a
failure to a CATEGORY, and each category carries a short Vietnamese sentence that
tells the owner what happened and what to do. Two entry points:

* :func:`friendly_worker_error` — used by ``generate._run_cf_worker_once`` at the
  moment a worker subprocess fails, so the HTTPException that propagates up into
  ``jobs.error`` is already friendly.
* :func:`friendly_job_error` — a last-resort sanitizer in ``runner._job_failed``
  for raw text arriving from anywhere else (ffmpeg, DB, unexpected exceptions).
  Text that already looks like a human sentence passes through untouched.

NOTHING IS SWALLOWED. Callers log the full raw stderr/exception to
``logs/api.log`` (``[generate] <worker> FAILED: ...`` / ``[runner] job N raw
error: ...``) BEFORE replacing it, and the unclassified fallback still carries a
one-line technical gist plus an explicit "chi tiết đã được ghi log" pointer.

Adding a category = append one rule to ``RULES``. No caller changes needed.

Language note (project convention): code/comments English, user-facing strings
Vietnamese — these messages are read by the owner in the dashboard.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Worker scripts that shell out to yt-dlp — network/source rules only apply here
# (a network timeout inside the TTS worker means something entirely different).
YTDLP_SCRIPTS = (
    "download_worker.py",
    "ingest_worker.py",
    "probe_worker.py",
)

# Vietnamese step label per worker script, used to say WHICH step failed in the
# generic fallback message. Unknown scripts fall back to "Bước xử lý".
STEP_LABELS = {
    "download_worker.py": "Tải video nguồn",
    "ingest_worker.py": "Tải và bóc lời video nguồn",
    "probe_worker.py": "Kiểm tra video nguồn",
    "whisper_worker.py": "Bóc lời / căn chữ",
    "tts_worker.py": "Lồng tiếng",
    "omnivoice_worker.py": "Lồng tiếng",
    "caption_cover.py": "Tạo ảnh bìa",
    "stickman_procedural.py": "Dựng hình stickman",
    "prewarm_worker.py": "Khởi động model",
}


def step_label(script: str | None) -> str:
    """Vietnamese name of the pipeline step a worker script implements."""
    return STEP_LABELS.get((script or "").strip(), "Bước xử lý")


@dataclass(frozen=True)
class ErrorRule:
    """One failure category.

    code    — stable identifier, logged so a category can be grepped in api.log.
    status  — HTTP status for the HTTPException raised at the worker boundary.
    message — Vietnamese, user-facing. May contain ``{step}``.
    patterns— regexes; ANY match (case-insensitive) selects this rule.
    scripts — restrict the rule to these worker scripts (empty = any worker).
    """

    code: str
    status: int
    message: str
    patterns: tuple[str, ...]
    scripts: tuple[str, ...] = ()

    def matches(self, text: str, script: str | None) -> bool:
        if self.scripts and (script or "") not in self.scripts:
            return False
        return any(rx.search(text) for rx in _compiled_for(self))

    def render(self, script: str | None = None) -> str:
        return self.message.format(step=step_label(script))


_RX_CACHE: dict[str, tuple[re.Pattern[str], ...]] = {}


def _compiled_for(rule: ErrorRule) -> tuple[re.Pattern[str], ...]:
    """Compile (once) and cache the rule's regexes."""
    got = _RX_CACHE.get(rule.code)
    if got is None:
        got = tuple(re.compile(p, re.IGNORECASE | re.DOTALL) for p in rule.patterns)
        _RX_CACHE[rule.code] = got
    return got


# --- the rule table -----------------------------------------------------------
# ORDER MATTERS: first match wins. Put the SPECIFIC shapes above the generic ones
# (e.g. an incomplete-read is also "a download error", so it must precede the
# generic connection rules; a bot-check 403 must precede the plain HTTP-403 rule).
RULES: tuple[ErrorRule, ...] = (
    # ---- definitive source-side blocks (retrying can never help) -------------
    ErrorRule(
        code="src_drm",
        status=422,
        # Kept byte-identical to the pre-existing DRM message (it was already
        # surfaced to the owner) so nothing regresses for that case.
        message="Video nguồn bị khoá bản quyền (DRM) nên không tải được — "
                "hãy chọn video nguồn khác.",
        patterns=(r"drm[\s\-_]*protected",),
    ),
    ErrorRule(
        code="src_unavailable",
        status=404,
        message="Video nguồn không còn tồn tại hoặc đã bị gỡ khỏi nền tảng. "
                "Hãy kiểm tra lại đường dẫn hoặc chọn video nguồn khác.",
        patterns=(
            r"video unavailable",
            r"this video (?:is|has been) (?:no longer available|removed)",
            r"removed by the uploader",
            r"account associated with this video has been terminated",
            r"video has been removed",
        ),
    ),
    ErrorRule(
        code="src_private",
        status=403,
        message="Video nguồn ở chế độ riêng tư hoặc chỉ dành cho thành viên nên "
                "không tải được. Hãy chọn video nguồn khác.",
        patterns=(
            r"private video",
            r"this video is private",
            r"members[\s\-]?only",
            r"join this channel",
        ),
    ),
    ErrorRule(
        code="src_age_restricted",
        status=403,
        message="Video nguồn bị giới hạn độ tuổi nên không tải được nếu chưa "
                "đăng nhập. Hãy chọn video nguồn khác.",
        patterns=(
            r"age[\s\-]?restricted",
            r"sign in to confirm your age",
            r"inappropriate for some users",
        ),
    ),
    ErrorRule(
        code="src_geo_blocked",
        status=451,
        message="Video nguồn bị chặn ở khu vực này nên không tải được. "
                "Hãy chọn video nguồn khác.",
        patterns=(
            r"available in your country",
            r"geo[\s\-]?restricted",
            r"blocked it in your country",
            r"available in the following countries",
        ),
    ),
    ErrorRule(
        code="src_not_ready",
        status=422,
        message="Video nguồn đang phát trực tiếp hoặc chưa tới giờ công chiếu nên "
                "chưa tải được. Hãy đợi video kết thúc rồi thử lại.",
        patterns=(
            r"live event will begin",
            r"this live event",
            r"premieres in",
            r"is not yet available",
        ),
    ),
    ErrorRule(
        code="src_bad_link",
        status=422,
        message="Đường dẫn video nguồn không hợp lệ hoặc nền tảng này chưa được "
                "hỗ trợ. Hãy kiểm tra lại link nguồn.",
        patterns=(
            r"unsupported url",
            r"is not a valid url",
            r"incomplete youtube id",
        ),
    ),
    ErrorRule(
        code="src_no_format",
        status=422,
        message="Không tìm được định dạng tải phù hợp cho video nguồn này. "
                "Hãy thử video nguồn khác.",
        patterns=(
            r"requested format is not available",
            r"no video formats found",
        ),
    ),
    # ---- source-side blocks that MAY clear on a retry ------------------------
    ErrorRule(
        code="src_bot_check",
        status=429,
        message="Nền tảng nguồn đang chặn tải tự động (yêu cầu xác minh "
                "\"không phải bot\"). Hãy thử lại sau vài phút, hoặc chọn video "
                "nguồn khác.",
        patterns=(
            r"sign in to confirm you.{0,3}re not a bot",
            r"confirm you.{0,3}re not a bot",
        ),
    ),
    ErrorRule(
        code="src_incomplete_stream",
        status=502,
        # See download_worker.py: fmt 251 (webm/opus) is sometimes served broken
        # ("992 bytes read, N more expected") and every retry reproduces it.
        message="Luồng dữ liệu của video nguồn bị lỗi (tải về thiếu dữ liệu, "
                "đã thử lại nhiều lần vẫn hỏng). Hãy thử lại sau, hoặc chọn "
                "video nguồn khác.",
        patterns=(
            r"incompleteread",
            r"bytes read.{0,40}more expected",
            r"did not get any data blocks",
        ),
    ),
    ErrorRule(
        code="src_http_403",
        status=503,
        message="Máy chủ video từ chối lượt tải (HTTP 403) — thường do nền tảng "
                "chặn tạm thời. Hãy thử lại; nếu vẫn lỗi, cần cập nhật cấu hình "
                "tải video.",
        patterns=(
            r"http error 403",
            r"\b403:? forbidden",
        ),
    ),
    # ---- network / connectivity (yt-dlp family) ------------------------------
    # THE CASE THAT PROMPTED THIS MODULE: a connect timeout to a googlevideo CDN
    # host, after yt-dlp already burned its 10 retries.
    ErrorRule(
        code="net_timeout",
        status=503,
        message="Không tải được video nguồn do lỗi kết nối mạng tạm thời "
                "(hết thời gian chờ khi kết nối tới máy chủ video). Hãy thử lại.",
        patterns=(
            r"connect timeout",
            r"read timeout",
            r"connection to \S+ timed out",
            r"the read operation timed out",
            r"timeouterror",
            r"timed out\b.{0,80}(?:giving up|retries)",
        ),
        scripts=YTDLP_SCRIPTS,
    ),
    ErrorRule(
        code="net_reset",
        status=503,
        message="Không tải được video nguồn vì kết nối tới máy chủ video bị ngắt "
                "giữa chừng. Hãy thử lại.",
        patterns=(
            r"connection reset",
            r"connectionreseterror",
            r"connection aborted",
            r"remote end closed connection",
            r"winerror 10054",
            r"broken pipe",
            r"eof occurred in violation of protocol",
        ),
        scripts=YTDLP_SCRIPTS,
    ),
    ErrorRule(
        code="net_dns",
        status=503,
        message="Không kết nối được tới máy chủ video (không phân giải được tên "
                "miền — có thể mất mạng). Hãy kiểm tra kết nối Internet rồi thử lại.",
        patterns=(
            r"getaddrinfo failed",
            r"errno 11001",
            r"name or service not known",
            r"temporary failure in name resolution",
            r"nodename nor servname",
        ),
        scripts=YTDLP_SCRIPTS,
    ),
    ErrorRule(
        code="ytdlp_extractor",
        status=502,
        message="Trình tải video (yt-dlp) không đọc được trang nguồn — nhiều khả "
                "năng nền tảng vừa đổi cấu trúc và yt-dlp cần cập nhật. "
                "Chi tiết đã được ghi log.",
        patterns=(
            r"unable to extract",
            r"failed to extract any player response",
            r"nsig extraction failed",
            r"signature extraction failed",
        ),
        scripts=YTDLP_SCRIPTS,
    ),
    # Catch-all for yt-dlp's own download failure prefix. Deliberately LAST in the
    # yt-dlp block: the specific shapes above (timeout, reset, 403, incomplete
    # read) all arrive wrapped in this same sentence and must win over it.
    ErrorRule(
        code="src_download_failed",
        status=502,
        message="Không tải được dữ liệu của video nguồn (máy chủ nguồn trả về lỗi). "
                "Hãy thử lại; nếu vẫn lỗi, hãy chọn video nguồn khác.",
        patterns=(
            r"unable to download video data",
            r"unable to download webpage",
            r"giving up after \d+ retries",
        ),
        scripts=YTDLP_SCRIPTS,
    ),
    # ---- generic network fallback (any worker) -------------------------------
    ErrorRule(
        code="net_generic",
        status=503,
        message="{step} thất bại do lỗi kết nối mạng tạm thời. Hãy thử lại.",
        patterns=(
            r"connect timeout",
            r"connection reset",
            r"connection aborted",
            r"getaddrinfo failed",
            r"max retries exceeded with url",
            r"newconnectionerror",
        ),
    ),
    # ---- machine / environment ------------------------------------------------
    ErrorRule(
        code="disk_full",
        status=507,
        message="Ổ đĩa đã đầy nên không ghi được file. Hãy giải phóng dung lượng "
                "rồi thử lại.",
        patterns=(
            r"no space left on device",
            r"errno 28\b",
            r"winerror 112\b",
            r"not enough space on the disk",
        ),
    ),
    ErrorRule(
        code="gpu_oom",
        status=507,
        message="{step} thất bại vì GPU hết bộ nhớ. Hãy đóng bớt ứng dụng dùng "
                "GPU rồi thử lại, hoặc giảm độ dài / độ phân giải video.",
        patterns=(
            r"cuda out of memory",
            r"outofmemoryerror",
            r"failed to allocate .{0,40}memory",
        ),
    ),
    ErrorRule(
        code="ffmpeg_missing",
        status=500,
        message="Không tìm thấy FFmpeg — đường dẫn FFmpeg đang cấu hình sai. "
                "Chi tiết đã được ghi log.",
        patterns=(
            r"ffmpeg not found",
            r"ffprobe not found",
            r"you have not installed ffmpeg",
        ),
    ),
    ErrorRule(
        code="dep_missing",
        status=500,
        message="Môi trường xử lý (cf-venv) thiếu thư viện cần thiết. "
                "Chi tiết đã được ghi log.",
        patterns=(
            r"modulenotfounderror",
            r"no module named",
        ),
    ),
    # ---- script generation (claude -p headless / LLM gate) -------------------
    # These do not come from a cf-venv worker; they reach friendly_job_error via
    # the HTTPExceptions raised in _run_claude_script.
    ErrorRule(
        code="llm_script_gen",
        status=502,
        message="Bước viết kịch bản thất bại: mô hình ngôn ngữ không trả về kết quả "
                "hợp lệ. Hãy thử lại; chi tiết đã được ghi log.",
        patterns=(
            r"error_max_turns",
            r"\(exit .{0,6}, no-result\)",
            r"stream ended with no result event",
            r"could not parse script json",
            r"claude script gen failed",
        ),
    ),
)


def classify(text: str, script: str | None = None) -> ErrorRule | None:
    """Return the first rule matching `text`, or None if unclassified."""
    if not text:
        return None
    for rule in RULES:
        if rule.matches(text, script):
            return rule
    return None


# --- technical gist -----------------------------------------------------------
# Lines that carry no information for a human reader: traceback frame headers,
# the ^^^^ caret markers Python 3.11 prints, and bare source-echo lines.
_NOISE_LINE = re.compile(
    r"^(?:\s*File\s+\".*?\",\s*line\s+\d+|\s*[\^~]+\s*|Traceback \(most recent call last\):)\s*$"
)


def technical_gist(text: str, limit: int = 200) -> str:
    """One short, single-line summary of a raw error — the LAST informative line.

    A Python traceback's final line ("yt_dlp.utils.DownloadError: ERROR: ...") is
    the one line worth keeping: it names the real exception. Frame headers, caret
    markers and echoed source lines are dropped. Used only in the UNCLASSIFIED
    fallback so a bug report still has a handle, while the full text stays in
    logs/api.log.
    """
    if not text:
        return ""
    for raw_line in reversed(text.replace("\r\n", "\n").replace("\r", "\n").splitlines()):
        line = raw_line.strip()
        if not line or _NOISE_LINE.match(raw_line):
            continue
        line = re.sub(r"\s+", " ", line)
        return line[:limit].strip()
    return ""


# --- entry point 1: a cf-venv worker subprocess failed ------------------------

def friendly_worker_error(
    script: str, returncode: int | None, stderr: str
) -> tuple[int, str, str]:
    """Map a failed worker run to (http_status, vietnamese_message, category_code).

    Unclassified failures get a generic Vietnamese sentence naming the step, the
    "chi tiết đã được ghi log" pointer and a one-line technical gist — enough for
    the owner to file a useful report without pasting a traceback.
    """
    rule = classify(stderr or "", script)
    if rule is not None:
        return rule.status, rule.render(script), rule.code

    gist = technical_gist(stderr or "")
    msg = (
        f"{step_label(script)} thất bại vì một lỗi không xác định. "
        f"Chi tiết đầy đủ đã được ghi log (logs/api.log)."
    )
    if gist:
        msg += f" Tóm tắt kỹ thuật: {gist}"
    return 500, msg, "unclassified"


def worker_timeout_message(script: str, timeout: int | float) -> str:
    """User-facing message for a worker killed by our own watchdog timeout."""
    return (
        f"{step_label(script)} chạy quá lâu và đã bị dừng (quá {int(timeout)} giây). "
        f"Hãy thử lại; nếu lặp lại, hãy chọn video nguồn ngắn hơn."
    )


def gpu_flake_message(script: str, kind: str, attempts: int) -> str:
    """User-facing message for a GPU-init flake that survived every retry.

    `kind`: 'crash' = native fault with no traceback, anything else = a cuDNN/CUDA
    library load failure. Both are transient GPU-init instability, not bad input.
    """
    what = (
        "tiến trình xử lý bị sập khi khởi tạo GPU"
        if kind == "crash"
        else "thư viện GPU (cuDNN/CUDA) nạp lỗi"
    )
    return (
        f"{step_label(script)} thất bại: {what} sau {attempts} lần thử. "
        f"Đây là sự cố tạm thời của GPU và thường tự hết — hãy thử lại. "
        f"Chi tiết đã được ghi log."
    )


# --- entry point 2: last-resort sanitizer before writing jobs.error -----------
# Markers that prove a string is machine output, not a sentence written for a human.
_RAW_MARKERS = (
    "traceback (most recent call last)",
    '  file "',
    '.py", line ',
    "^^^^",
    "yt_dlp.utils.",
    "failed (exit=",
    "failed (exit ",
    "stderr tail:",
)


def looks_raw(text: str) -> bool:
    """True if `text` looks like machine output (traceback / worker dump) rather
    than a message already written for the owner.

    Deliberately conservative: short, single-line, marker-free strings pass
    through untouched, so the many hand-written Vietnamese messages already stored
    in jobs.error (e.g. "Dừng bởi người dùng") are never rewritten.
    """
    if not text:
        return False
    low = text.lower()
    if any(m in low for m in _RAW_MARKERS):
        return True
    if len(text) > 400:
        return True
    return text.count("\n") + text.count("\r") >= 3


# A worker dump written by generate._run_cf_worker_once carries its script name in
# the prefix ("download_worker.py failed (exit=1): ..."). Recover it so script-scoped
# rules (the yt-dlp network family) still apply at this outer layer.
_SCRIPT_PREFIX = re.compile(r"\b([a-z0-9_]+\.py)\s+failed\s+\(exit", re.IGNORECASE)


def infer_script(text: str) -> str | None:
    """Best-effort worker-script name recovered from a raw worker-failure dump."""
    m = _SCRIPT_PREFIX.search(text or "")
    return m.group(1) if m else None


def friendly_job_error(raw: str, script: str | None = None) -> str:
    """Sanitize any error text on its way into ``jobs.error``.

    Pass-through when the text already reads like a human message; otherwise
    classify it, or fall back to the generic "chi tiết đã được ghi log" sentence.
    The caller MUST have logged `raw` first — this function is the only place the
    raw text stops being propagated.
    """
    text = (raw or "").strip()
    if not text:
        return "Công việc thất bại vì một lỗi không xác định. Chi tiết đã được ghi log."
    if not looks_raw(text):
        return text

    script = script or infer_script(text)
    rule = classify(text, script)
    if rule is not None:
        return rule.render(script)

    gist = technical_gist(text)
    msg = ("Công việc thất bại vì một lỗi không xác định. "
           "Chi tiết đầy đủ đã được ghi log (logs/api.log).")
    if gist:
        msg += f" Tóm tắt kỹ thuật: {gist}"
    return msg
