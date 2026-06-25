"""Tests for the job-stop v2 backend changes (no DB required):

  1. The subprocess kill registry in generate.py: a child spawned while a job is
     active is attributed to it, tree-killed by kill_job_processes, and cleaned up.
  2. The runner's stop classification: JobStopped / _was_stopped → 'stopped'.

These exercise the pure in-memory machinery, not the full pipeline.
"""
import subprocess
import sys
import time

import generate
import runner


# --- 1) kill registry --------------------------------------------------------

def test_register_attributes_proc_to_active_job():
    generate.set_active_job(4242)
    try:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        jid = generate._register_job_proc(proc)
        assert jid == 4242
        # The job now has one live process registered.
        assert proc in generate._job_procs.get(4242, set())
        # kill_job_processes tree-kills it.
        n = generate.kill_job_processes(4242)
        assert n == 1
        proc.wait(timeout=10)
        assert proc.poll() is not None  # actually dead
        generate._unregister_job_proc(jid, proc)
        assert 4242 not in generate._job_procs
    finally:
        generate.set_active_job(None)


def test_no_active_job_means_no_attribution():
    generate.set_active_job(None)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        jid = generate._register_job_proc(proc)
        assert jid is None  # on-demand call (preview/clone) is never stop-targeted
        # kill_job_processes for an unknown id is a clean no-op.
        assert generate.kill_job_processes(999999) == 0
    finally:
        proc.wait(timeout=10)


def test_set_active_job_none_drops_leftover_handles():
    generate.set_active_job(77)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        generate._register_job_proc(proc)
        assert generate._job_procs.get(77)
        generate.set_active_job(None)  # clearing drops the job's leftover set
        assert 77 not in generate._job_procs
    finally:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:
            pass


def test_kill_job_processes_handles_multiple_concurrent_children():
    # A footage job runs TTS concurrently with ffmpeg cuts → >1 live child per job.
    generate.set_active_job(555)
    procs = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
             for _ in range(3)]
    try:
        for p in procs:
            generate._register_job_proc(p)
        assert generate.kill_job_processes(555) == 3
        for p in procs:
            p.wait(timeout=10)
            assert p.poll() is not None
    finally:
        generate.set_active_job(None)
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass


# --- 2) stop classification --------------------------------------------------

def test_check_cancel_raises_jobstopped():
    runner._CANCEL_REQUESTED.add(31337)
    try:
        raised = None
        try:
            runner._check_cancel(31337)
        except runner.JobStopped as e:
            raised = e
        assert isinstance(raised, runner.JobStopped)
        # _check_cancel consumes the request.
        assert 31337 not in runner._CANCEL_REQUESTED
    finally:
        runner._CANCEL_REQUESTED.discard(31337)


def test_was_stopped_flag():
    assert runner._was_stopped(8080) is False
    runner._STOPPED_JOBS.add(8080)
    try:
        assert runner._was_stopped(8080) is True
    finally:
        runner._STOPPED_JOBS.discard(8080)


def test_jobstopped_is_runtimeerror_subclass():
    # The failure handler catches Exception, then narrows on isinstance(JobStopped).
    # JobStopped must be an Exception so the generic handler still catches it.
    assert issubclass(runner.JobStopped, RuntimeError)
