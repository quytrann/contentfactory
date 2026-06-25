"""Regression tests for the "Claude Code timed out" fix in generate.py.

Prove the headless script-gen timeout is handled GRACEFULLY:
  - a slow/hanging `claude` subprocess does NOT hang past the timeout — it raises
    HTTPException(504) instead of blocking forever;
  - the whole process tree is hard-killed (taskkill /F /T) so no orphan is left;
  - the retry loop fires exactly SCRIPT_GEN_RETRIES+1 times on timeout, then fails
    with the user-facing Vietnamese message;
  - the happy path (valid claude JSON) still returns the parsed scene list (no
    regression);
  - the job-failure transition (runner._job_failed) marks job + video 'failed'.

These are PURE tests: subprocess.Popen / subprocess.run / time.sleep are mocked, so
the REAL `claude` CLI is never invoked and no real process is spawned. The test
asserts on generate.py's real control flow, not a re-implementation of it.

Run:  cd Dashboard/api && .venv/Scripts/python -m pytest test/test_claude_timeout.py -q
"""

import json
import os
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate  # noqa: E402
from fastapi import HTTPException  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
#                                                                              #
# The real runner (generate._read_stream_json_result) was refactored to STREAM #
# the claude CLI output: it spawns the proc with                               #
# `--output-format stream-json --verbose` and reads `proc.stdout` line-by-line #
# in a background reader thread. Each line is a newline-delimited JSON event;  #
# the terminal `{"type":"result","subtype":"success","result":"<text>", ...}`  #
# event carries the final answer. A STALL is detected by the reader thread     #
# staying alive past `timeout` (t.join(timeout=...)), which raises 504. On 504 #
# the caller hard-kills the tree then `proc.wait(timeout=5)`; on success it     #
# reaps via `proc.communicate(timeout=10)`.                                    #
#                                                                              #
# The doubles below therefore expose a `.stdout` that is an ITERABLE of        #
# stream-json lines (success) or one that BLOCKS forever (hang), matching the  #
# real contract so genuine behavior is exercised — not a re-implementation.    #
# --------------------------------------------------------------------------- #
def _stream_lines(result_text):
    """Build the newline-delimited stream-json events the real reader parses:
    a couple of benign 'assistant' events plus the terminal 'result' event whose
    `result` field carries the raw model text the runner extracts. Mirrors the
    exact event keys generate._read_stream_json_result keys off
    (type=='result', subtype, result, is_error)."""
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": result_text}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": result_text},
    ]
    return [json.dumps(e) + "\n" for e in events]


class _BlockingStdout:
    """A file-like stdout whose iteration BLOCKS until the proc is killed — models
    a wedged claude that produces no terminal 'result' line, so the reader thread
    stays alive and the runner's t.join(timeout) trips the 504 stall path.

    `kill()`/`wait()` release the block so the daemon reader thread can exit
    cleanly between tests (no lingering threads)."""

    def __init__(self):
        self._released = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        # Block indefinitely; the runner's reader thread will be join()'d with a
        # timeout and abandoned (daemon) on the stall path. Released on kill().
        self._released.wait()
        raise StopIteration

    def release(self):
        self._released.set()


class _HangingProc:
    """A fake Popen that hangs: its .stdout never yields a 'result' event, so the
    reader thread stays alive and the runner raises 504 after `timeout`.

    Mirrors the Popen surface the runner touches: .pid, .returncode, .stdout,
    .kill(), .wait(timeout=), .communicate(timeout=)."""

    _next_pid = 4242

    def __init__(self):
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.returncode = None
        self.killed = False
        self.wait_calls = 0
        self.stdout = _BlockingStdout()

    def kill(self):
        self.killed = True
        self.stdout.release()

    def wait(self, timeout=None):
        # The post-kill reap on the 504 path. Release the reader so it can exit.
        self.wait_calls += 1
        self.stdout.release()
        self.returncode = -9
        return self.returncode

    def communicate(self, timeout=None):
        # Defensive: the 504 path uses wait(), but keep this harmless if reached.
        self.stdout.release()
        return ("", "")


class _GoodProc:
    """A fake Popen that streams a valid stream-json event sequence ending in a
    `result` event carrying `result_text` — the real success contract."""

    def __init__(self, result_text):
        self.pid = 5555
        self.returncode = 0
        self.stdout = iter(_stream_lines(result_text))
        self.communicate_calls = 0

    def communicate(self, timeout=None):
        # The success-path reap: stdout already drained by the reader thread.
        self.communicate_calls += 1
        return ("", "")

    def wait(self, timeout=None):
        return self.returncode


_VALID_SCENES = [
    {"scene": 1, "narration": "Xin chào", "image_prompt": "a wide cinematic shot"},
    {"scene": 2, "narration": "Tiếp theo", "image_prompt": "a close-up shot"},
]


# --------------------------------------------------------------------------- #
# Test 1 — timeout handled: no hang, tree-kill fires, retries as configured    #
# --------------------------------------------------------------------------- #
def test_timeout_raises_504_and_kills_proc_tree(monkeypatch):
    """A hung claude → HTTPException(504); _kill_proc_tree is invoked for EVERY
    attempt (no orphan left); the call returns promptly (no real wait)."""
    procs = []

    def fake_popen(*args, **kwargs):
        p = _HangingProc()
        procs.append(p)
        return p

    kill_calls = []

    def spy_kill(proc):
        kill_calls.append(proc)
        # Defer to the real kill behavior would call taskkill; here we just record.

    monkeypatch.setattr(generate.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(generate, "_kill_proc_tree", spy_kill)
    # Don't actually sleep the retry backoff.
    monkeypatch.setattr(generate.time, "sleep", lambda *_a, **_k: None)
    # Pin retries so the assertion is deterministic regardless of .env.
    monkeypatch.setattr(generate, "SCRIPT_GEN_RETRIES", 1)

    with pytest.raises(HTTPException) as ei:
        generate._run_claude_script("dummy prompt", timeout=0.2)

    assert ei.value.status_code == 504
    # Retry loop fired SCRIPT_GEN_RETRIES + 1 = 2 attempts → 2 fresh procs spawned.
    assert len(procs) == 2, f"expected 2 attempts, got {len(procs)}"
    # Every attempt's process tree was killed → no orphan after either attempt.
    assert len(kill_calls) == 2, f"expected 2 tree-kills, got {len(kill_calls)}"
    assert kill_calls == procs, "the spawned procs were not all tree-killed"
    # The post-kill reap (proc.wait(timeout=5)) ran on each, so no handle leaks.
    assert all(p.wait_calls == 1 for p in procs)


def test_kill_proc_tree_runs_taskkill_on_windows(monkeypatch):
    """_kill_proc_tree must hard-kill the WHOLE tree via `taskkill /F /T /PID` on
    Windows — proving the Node launcher's grandchildren are reaped, not orphaned."""
    run_calls = []

    def fake_run(cmd, *args, **kwargs):
        run_calls.append(cmd)
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(generate.subprocess, "run", fake_run)
    monkeypatch.setattr(generate.os, "name", "nt")

    proc = _HangingProc()
    generate._kill_proc_tree(proc)

    assert len(run_calls) == 1, "taskkill was not invoked exactly once"
    cmd = run_calls[0]
    assert cmd[0] == "taskkill"
    assert "/F" in cmd and "/T" in cmd and "/PID" in cmd
    assert str(proc.pid) in cmd, "taskkill was not pointed at the proc's PID"


def test_kill_proc_tree_falls_back_to_kill_off_windows(monkeypatch):
    """On non-Windows, _kill_proc_tree falls back to proc.kill() (still no orphan
    from OUR direct child)."""
    monkeypatch.setattr(generate.os, "name", "posix")
    proc = _HangingProc()
    generate._kill_proc_tree(proc)
    assert proc.killed is True


def test_single_attempt_timeout_kills_and_raises_504(monkeypatch):
    """_run_claude_script_once (one attempt) tree-kills on timeout, reaps the pipes,
    and raises 504 — the building block the retry loop depends on."""
    proc = _HangingProc()
    monkeypatch.setattr(generate.subprocess, "Popen", lambda *a, **k: proc)
    kills = []
    monkeypatch.setattr(generate, "_kill_proc_tree", lambda p: kills.append(p))

    with pytest.raises(HTTPException) as ei:
        generate._run_claude_script_once("dummy", timeout=0.2)

    assert ei.value.status_code == 504
    assert kills == [proc], "the single attempt did not tree-kill the hung proc"
    assert proc.wait_calls == 1, "the post-kill reap (proc.wait) did not run"


def test_retries_disabled_means_single_attempt(monkeypatch):
    """SCRIPT_GEN_RETRIES=0 → exactly ONE attempt, then fail (no retry)."""
    procs = []
    monkeypatch.setattr(generate.subprocess, "Popen",
                        lambda *a, **k: procs.append(_HangingProc()) or procs[-1])
    monkeypatch.setattr(generate, "_kill_proc_tree", lambda p: None)
    monkeypatch.setattr(generate.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(generate, "SCRIPT_GEN_RETRIES", 0)

    with pytest.raises(HTTPException) as ei:
        generate._run_claude_script("dummy", timeout=0.2)
    assert ei.value.status_code == 504
    assert len(procs) == 1, f"retries=0 should give 1 attempt, got {len(procs)}"


def test_final_timeout_message_is_vietnamese(monkeypatch):
    """After exhausting retries, the surfaced 504 detail is the user-facing
    Vietnamese message (what the failed job row shows in the dashboard)."""
    monkeypatch.setattr(generate.subprocess, "Popen", lambda *a, **k: _HangingProc())
    monkeypatch.setattr(generate, "_kill_proc_tree", lambda p: None)
    monkeypatch.setattr(generate.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(generate, "SCRIPT_GEN_RETRIES", 1)

    with pytest.raises(HTTPException) as ei:
        generate._run_claude_script("dummy", timeout=0.2)

    detail = ei.value.detail
    assert "Viết kịch bản quá thời gian chờ" in detail
    assert "Claude Code" in detail


# --------------------------------------------------------------------------- #
# Test 2 — happy path still works (no regression)                              #
# --------------------------------------------------------------------------- #
def test_happy_path_returns_parsed_scene_list(monkeypatch):
    """Valid claude JSON → _run_claude_script returns the parsed scene list.
    Proves the timeout fix did not regress the success path."""
    import json
    good = _GoodProc(json.dumps(_VALID_SCENES))
    spawned = []

    def fake_popen(*a, **k):
        spawned.append(good)
        return good

    monkeypatch.setattr(generate.subprocess, "Popen", fake_popen)
    # _kill_proc_tree must NOT be called on the happy path.
    called = {"kill": False}
    monkeypatch.setattr(generate, "_kill_proc_tree",
                        lambda p: called.__setitem__("kill", True))

    scenes = generate._run_claude_script("dummy prompt", timeout=30)

    assert scenes == _VALID_SCENES
    assert len(spawned) == 1, "happy path should run exactly one attempt"
    assert called["kill"] is False, "tree-kill must not fire on the success path"


def test_happy_path_strips_markdown_fence(monkeypatch):
    """A claude result wrapped in a ```json fence is still parsed (real
    _extract_json_array path), guarding the common model-output shape."""
    import json
    fenced = "```json\n" + json.dumps(_VALID_SCENES) + "\n```"
    good = _GoodProc(fenced)
    monkeypatch.setattr(generate.subprocess, "Popen", lambda *a, **k: good)
    monkeypatch.setattr(generate, "_kill_proc_tree", lambda p: None)

    scenes = generate._run_claude_script("dummy", timeout=30)
    assert scenes == _VALID_SCENES


def test_nonzero_exit_fails_fast_without_retry(monkeypatch):
    """A genuine error (non-zero exit) must FAIL FAST (500), NOT be retried like a
    timeout — proves retry is scoped to 504 only."""
    class _BadExit:
        """Streams a clean result event but exits non-zero — the runner must fail
        fast with 500 (rc not in (0, None)), NOT retry like a timeout."""
        pid = 9
        returncode = 1
        def __init__(self):
            self.stdout = iter(_stream_lines(json.dumps(_VALID_SCENES)))
        def communicate(self, timeout=None):
            return ("", "boom: model error")
        def wait(self, timeout=None):
            return self.returncode

    procs = []
    monkeypatch.setattr(generate.subprocess, "Popen",
                        lambda *a, **k: procs.append(_BadExit()) or procs[-1])
    monkeypatch.setattr(generate, "_kill_proc_tree", lambda p: None)
    sleeps = []
    monkeypatch.setattr(generate.time, "sleep", lambda *a, **k: sleeps.append(1))
    monkeypatch.setattr(generate, "SCRIPT_GEN_RETRIES", 1)

    with pytest.raises(HTTPException) as ei:
        generate._run_claude_script("dummy", timeout=5)
    assert ei.value.status_code == 500
    assert len(procs) == 1, "a non-timeout error must not be retried"
    assert sleeps == [], "no retry backoff should run on a fail-fast error"


# --------------------------------------------------------------------------- #
# Test 3 — job-failure transition marks job + video failed (mocked DB)         #
# --------------------------------------------------------------------------- #
class _FakeConn:
    """Minimal stand-in for the psycopg dict_row connection used by _job_failed.
    Records every execute(sql, params) so the test can assert the failed-status
    UPDATEs ran with the expected message."""

    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return self


def test_job_failed_marks_job_and_video_failed(monkeypatch):
    """runner._job_failed marks BOTH the job and its video 'failed' and stores the
    (Vietnamese) timeout message — the transition the runner's except-block invokes
    when _run_claude_script finally raises 504."""
    import runner

    fake = _FakeConn()

    # get_conn is a contextmanager; replace it with one yielding our fake conn.
    from contextlib import contextmanager

    @contextmanager
    def fake_get_conn():
        yield fake

    monkeypatch.setattr(runner, "get_conn", fake_get_conn)

    msg = ("Viết kịch bản quá thời gian chờ (600s) sau 2 lần thử. "
           "Claude Code chạy quá lâu hoặc bị treo.")
    runner._job_failed(job_id=123, video_id=456, msg=msg)

    # Two UPDATEs: jobs -> failed (with the message), videos -> failed.
    assert len(fake.calls) == 2, f"expected 2 UPDATEs, got {len(fake.calls)}"
    job_sql, job_params = fake.calls[0]
    assert "UPDATE jobs SET status = 'failed'" in job_sql
    assert job_params[0].startswith("Viết kịch bản quá thời gian chờ")
    assert job_params[1] == 123
    vid_sql, vid_params = fake.calls[1]
    assert "UPDATE videos SET status = 'failed'" in vid_sql
    assert vid_params == (456,)


def test_job_failed_without_video_only_updates_job(monkeypatch):
    """When there is no video row yet, only the job is marked failed (no stray
    videos UPDATE)."""
    import runner
    from contextlib import contextmanager

    fake = _FakeConn()

    @contextmanager
    def fake_get_conn():
        yield fake

    monkeypatch.setattr(runner, "get_conn", fake_get_conn)
    runner._job_failed(job_id=7, video_id=None, msg="boom")

    assert len(fake.calls) == 1
    assert "UPDATE jobs SET status = 'failed'" in fake.calls[0][0]
