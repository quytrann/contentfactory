"""Centralized file logging for the ContentFactory API process.

WHY
---
Before this, runner/script-gen diagnostics went only to uvicorn's stdout — and the API
is launched with `-WindowStyle Hidden`, so that stdout is discarded. A slow or failed
run left NOTHING on disk to diagnose. This module installs a single rotating FILE
handler on the ROOT logger (so every `logging.getLogger(...)` child, e.g.
`contentfactory.generate`, persists with timestamps) AND tees the process stdout/stderr
to the same file so the runner's many `print(...)` diagnostics (`[timing] ...`,
`[runner] ...`, gate decisions) are captured too — without rewriting 55 call sites.

Idempotent: calling setup() more than once (e.g. uvicorn --reload re-import, or the
spawn worker) does not duplicate handlers. Safe to import early in main.py.

Log path: <api dir>/logs/api.log (override with CF_LOG_FILE). Rotates at ~5 MB, keeps
5 backups, so the file never grows unbounded.
"""
import logging
import logging.handlers
import os
import sys

_API_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_LOG = os.path.join(_API_DIR, "logs", "api.log")
_MARKER = "_cf_file_handler"  # tag so we never add the handler twice


class _StreamToLogger:
    """File-like wrapper that mirrors writes to a logger (so plain `print(...)` from the
    runner lands in api.log) while STILL writing through to the original stream. Buffers
    partial lines and only emits on newline so multi-write prints stay on one log line."""

    def __init__(self, orig, logger, level):
        self._orig = orig
        self._logger = logger
        self._level = level
        self._buf = ""

    def write(self, s):
        try:
            if self._orig is not None:
                self._orig.write(s)
        except Exception:
            pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.rstrip():
                try:
                    self._logger.log(self._level, line.rstrip())
                except Exception:
                    pass

    def flush(self):
        try:
            if self._orig is not None:
                self._orig.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return bool(self._orig) and self._orig.isatty()
        except Exception:
            return False


def setup(log_file: str | None = None, level: int = logging.INFO) -> str:
    """Install the rotating file handler on the root logger + tee stdout/stderr. Returns
    the resolved log-file path. Idempotent."""
    path = log_file or os.getenv("CF_LOG_FILE") or _DEFAULT_LOG
    os.makedirs(os.path.dirname(path), exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)

    # Don't add a second file handler on re-import / reload.
    already = any(getattr(h, _MARKER, False) for h in root.handlers)
    if not already:
        fh = logging.handlers.RotatingFileHandler(
            path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        setattr(fh, _MARKER, True)
        root.addHandler(fh)

        # Tee stdout/stderr exactly once (guard via the same marker on the streams).
        if not getattr(sys.stdout, "_cf_teed", False):
            out_logger = logging.getLogger("contentfactory.stdout")
            err_logger = logging.getLogger("contentfactory.stderr")
            sys.stdout = _StreamToLogger(sys.stdout, out_logger, logging.INFO)
            sys.stderr = _StreamToLogger(sys.stderr, err_logger, logging.WARNING)
            setattr(sys.stdout, "_cf_teed", True)
            setattr(sys.stderr, "_cf_teed", True)

        logging.getLogger("contentfactory.log_setup").info(
            "[log] file logging initialized -> %s (level=%s, rotate 5MB x5)",
            path, logging.getLevelName(level),
        )
    return path
