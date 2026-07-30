"""Tests for the friendly job-error mechanism (worker_errors.py) and its wiring.

THE CASE THAT PROMPTED THIS FILE: job 356 failed during footage ingest and the
dashboard showed the owner a raw yt-dlp traceback ending in

    yt_dlp.utils.DownloadError: ERROR: [download] Got error: (<HTTPSConnection(
    host='rr2---sn-ojnpo5-c3.googlevideo.com', port=443) ...>, 'Connection to
    rr2---sn-ojnpo5-c3.googlevideo.com timed out. (connect timeout=20.0)').
    Giving up after 10 retries

The real stderr of that failure is stored verbatim in
``test/_dummy_data/ytdlp_cdn_timeout_stderr.txt`` and drives the tests below.

What is asserted:
  1. That stderr classifies as ``net_timeout`` and yields a Vietnamese sentence
     with NO traceback fragments in it.
  2. ``generate._run_cf_worker_once`` puts that friendly sentence (not the raw
     tail) into the HTTPException detail — i.e. what lands in ``jobs.error``.
  3. The RAW stderr is NOT lost: it is written to the api logger before the
     exception is raised.
  4. Other yt-dlp categories (DRM, private, removed, geo, bot-check, broken
     stream, 403, no-space, ...) classify too.
  5. Unclassified failures still produce a friendly sentence + the "chi tiết đã
     được ghi log" pointer + a one-line technical gist.
  6. ``friendly_job_error`` (the runner's last-resort sanitizer) rewrites raw
     dumps but leaves human-written messages untouched.

NO subprocess, NO network, NO DB: the worker subprocess is stubbed at
subprocess.Popen.

Run:  cd Dashboard/api && .venv/Scripts/python -m pytest test/test_worker_error_messages.py -q
"""

import logging
import os
import subprocess
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate  # noqa: E402
import worker_errors as we  # noqa: E402

_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "_dummy_data", "ytdlp_cdn_timeout_stderr.txt")

# Fragments that must NEVER appear in a user-facing message.
_TRACEBACK_FRAGMENTS = (
    "Traceback",
    "yt_dlp.utils",
    ".py\", line",
    "^^^^",
    "HTTPSConnection",
    "download_worker.py failed",
)


def _cdn_timeout_stderr() -> str:
    with open(_FIXTURE, encoding="utf-8") as f:
        return f.read()


def _assert_friendly_vi(msg: str) -> None:
    """A user-facing message: Vietnamese, one short paragraph, no machine noise."""
    assert msg, "message must not be empty"
    for frag in _TRACEBACK_FRAGMENTS:
        assert frag not in msg, f"raw fragment leaked into the UI message: {frag!r}"
    assert "\n" not in msg, "user-facing message must be a single paragraph"
    assert len(msg) < 400, "user-facing message must stay short"
    # Vietnamese diacritics prove it is the localized string, not an English dump.
    assert any(ch in msg for ch in "ạảãáàềếệễểịỉĩíìọỏõóòụủũúùỳỹýỵ")


# ---------------------------------------------------------------------------
# 1. the CDN-timeout case classifies and reads like a human sentence
# ---------------------------------------------------------------------------
def test_cdn_timeout_classifies_as_net_timeout():
    stderr = _cdn_timeout_stderr()
    # Sanity: the fixture really is the raw traceback we are trying to hide.
    assert "yt_dlp.utils.DownloadError" in stderr
    assert "connect timeout=20.0" in stderr

    rule = we.classify(stderr, "download_worker.py")
    assert rule is not None and rule.code == "net_timeout"

    status, msg, code = we.friendly_worker_error("download_worker.py", 1, stderr)
    assert code == "net_timeout"
    assert status == 503
    _assert_friendly_vi(msg)
    # It must say what happened (network) and what to do (retry).
    assert "kết nối mạng" in msg
    assert "thử lại" in msg.lower()


# ---------------------------------------------------------------------------
# 2 + 3. end-to-end at the worker boundary: friendly detail, raw kept in the log
# ---------------------------------------------------------------------------
class _FakeProc:
    """Stand-in for a cf-venv worker that wrote `stderr` and exited non-zero."""

    def __init__(self, stderr_path: str, payload: str):
        self.pid = 424242
        self.returncode = 1
        with open(stderr_path, "wb") as f:
            f.write(payload.encode("utf-8"))

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


@pytest.fixture()
def _stub_worker(monkeypatch):
    """Make _run_cf_worker_once run a fake failing worker with the given stderr."""

    def _install(stderr_payload: str):
        def fake_popen(cmd, stdin=None, stdout=None, stderr=None, env=None):
            # `stderr` is the open file object _run_cf_worker_once created.
            return _FakeProc(stderr.name, stderr_payload)

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        # The cf-venv python never runs, but the path check must not trip first.
        monkeypatch.setattr(generate, "CF_VENV_PYTHON", sys.executable, raising=False)

    return _install


def test_worker_boundary_raises_friendly_and_logs_raw(_stub_worker, caplog):
    raw = _cdn_timeout_stderr()
    _stub_worker(raw)

    with caplog.at_level(logging.ERROR, logger="contentfactory.generate"):
        with pytest.raises(HTTPException) as ei:
            generate._run_cf_worker_once("download_worker.py", {"link": "x"}, timeout=30)

    # (2) what lands in jobs.error is the friendly sentence, not the traceback.
    detail = ei.value.detail
    _assert_friendly_vi(detail)
    assert ei.value.status_code == 503

    # (3) the raw error is NOT lost — it is in the log record (-> logs/api.log).
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "yt_dlp.utils.DownloadError" in logged, "raw traceback must survive in the log"
    assert "connect timeout=20.0" in logged
    assert "category=net_timeout" in logged


def test_worker_boundary_unclassified_still_friendly_and_logged(_stub_worker, caplog):
    raw = (
        'Traceback (most recent call last):\r\n'
        '  File "E:\\Installed\\cf-venv\\Lib\\site-packages\\weird\\thing.py", line 7, in go\r\n'
        '    boom()\r\n'
        'weird.thing.VeryNovelError: something nobody has seen before\r\n'
    )
    _stub_worker(raw)

    with caplog.at_level(logging.ERROR, logger="contentfactory.generate"):
        with pytest.raises(HTTPException) as ei:
            generate._run_cf_worker_once("download_worker.py", {"link": "x"}, timeout=30)

    detail = ei.value.detail
    assert "Tải video nguồn" in detail          # names the step that failed
    assert "logs/api.log" in detail             # tells the owner where to look
    assert "VeryNovelError" in detail           # keeps a usable one-line gist
    assert "Traceback" not in detail            # but not the stack itself
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "VeryNovelError" in logged and "thing.py" in logged


# ---------------------------------------------------------------------------
# 4. the other known yt-dlp / environment categories
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code,stderr", [
    ("src_drm",
     "ERROR: [youtube] abc123: This video is DRM protected"),
    ("src_unavailable",
     "ERROR: [youtube] abc123: Video unavailable. This video has been removed by the uploader"),
    ("src_private",
     "ERROR: [youtube] abc123: Private video. Sign in if you've been granted access"),
    ("src_age_restricted",
     "ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users."),
    ("src_geo_blocked",
     "ERROR: [youtube] abc: The uploader has not made this video available in your country"),
    ("src_not_ready",
     "ERROR: [youtube] abc: This live event will begin in 3 hours"),
    ("src_bad_link",
     "ERROR: Unsupported URL: https://example.invalid/whatever"),
    ("src_no_format",
     "ERROR: [youtube] abc: Requested format is not available"),
    ("src_bot_check",
     "ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies-from-browser"),
    ("src_incomplete_stream",
     "ERROR: unable to download video data: 992 bytes read, 4272 more expected. "
     "Giving up after 10 retries"),
    ("src_http_403",
     "ERROR: unable to download video data: HTTP Error 403: Forbidden"),
    ("ytdlp_extractor",
     "ERROR: [youtube] abc: Unable to extract player response; please report this issue"),
    ("src_download_failed",
     "ERROR: unable to download video data: <urlopen error [Errno 104]>"),
    ("disk_full",
     "OSError: [Errno 28] No space left on device"),
    ("dep_missing",
     "ModuleNotFoundError: No module named 'yt_dlp'"),
])
def test_known_categories_classify(code, stderr):
    rule = we.classify(stderr, "download_worker.py")
    assert rule is not None, f"expected {code}, got no match"
    assert rule.code == code
    _assert_friendly_vi(rule.render("download_worker.py"))


def test_net_rules_are_scoped_to_ytdlp_workers():
    """A 'connect timeout' in the TTS worker must not claim the video source failed."""
    stderr = "TimeoutError: connect timeout=20.0 to huggingface.co"
    dl = we.classify(stderr, "download_worker.py")
    tts = we.classify(stderr, "tts_worker.py")
    assert dl is not None and dl.code == "net_timeout"
    assert tts is not None and tts.code == "net_generic"
    assert "video nguồn" not in tts.render("tts_worker.py")
    assert "Lồng tiếng" in tts.render("tts_worker.py")


def test_gpu_flake_and_timeout_messages_are_vietnamese():
    _assert_friendly_vi(we.gpu_flake_message("tts_worker.py", "crash", 3))
    _assert_friendly_vi(we.gpu_flake_message("tts_worker.py", "load", 3))
    msg = we.worker_timeout_message("ingest_worker.py", 3600)
    _assert_friendly_vi(msg)
    assert "3600" in msg


# ---------------------------------------------------------------------------
# 5 + 6. the runner's last-resort sanitizer
# ---------------------------------------------------------------------------
def test_friendly_job_error_rewrites_the_real_job_356_error():
    """The exact string the dashboard showed for job 356 (worker dump prefix +
    traceback) must become a friendly sentence, even without a script hint."""
    raw = ("download_worker.py failed (exit=1): n <lambda>\r\n"
           "    error=IDENTITY if not fatal else lambda e: self.report_error(...),\r\n"
           "  File \"E:\\Installed\\cf-venv\\Lib\\site-packages\\yt_dlp\\YoutubeDL.py\", "
           "line 1104, in trouble\r\n    raise DownloadError(message, exc_info)\r\n"
           "yt_dlp.utils.DownloadError: ERROR: \r[download] Got error: "
           "(<HTTPSConnection(host='rr2---sn-ojnpo5-c3.googlevideo.com', port=443) "
           "at 0x24a96ddc390>, 'Connection to rr2---sn-ojnpo5-c3.googlevideo.com "
           "timed out. (connect timeout=20.0)'). Giving up after 10 retries")
    assert we.looks_raw(raw)
    assert we.infer_script(raw) == "download_worker.py"
    out = we.friendly_job_error(raw)
    _assert_friendly_vi(out)
    assert "kết nối mạng" in out


def test_friendly_job_error_passes_human_messages_through():
    """Already-friendly job errors (and short internal ones) must not be rewritten."""
    for msg in (
        "Video nguồn không có kịch bản để dùng lại",
        "Dừng bởi người dùng",
        "page 7 not found",
        "clone job has no destination video row (endpoint bug)",
        "Video nguồn bị khoá bản quyền (DRM) nên không tải được — hãy chọn video nguồn khác.",
    ):
        assert we.friendly_job_error(msg) == msg


def test_friendly_job_error_classifies_script_gen_failures():
    """`claude -p` failures reach jobs.error as a worker-style dump too."""
    raw = ("claude -p (footage batch 2/3) failed (exit 1, no-result): "
           + "some very long provider dump " * 20)
    out = we.friendly_job_error(raw)
    _assert_friendly_vi(out)
    assert "kịch bản" in out


def test_friendly_job_error_unclassified_keeps_a_gist():
    raw = ("Traceback (most recent call last):\n"
           "  File \"D:\\x\\runner.py\", line 10, in go\n"
           "    ffmpeg()\n"
           "RuntimeError: concat demuxer produced 0 frames\n")
    out = we.friendly_job_error(raw)
    assert "logs/api.log" in out
    assert "concat demuxer produced 0 frames" in out
    assert "Traceback" not in out


def test_empty_error_still_yields_a_message():
    assert we.friendly_job_error("").strip()
    _assert_friendly_vi(we.friendly_job_error(""))
