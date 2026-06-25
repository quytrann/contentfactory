"""Dubbed credit `needs_input` PAUSE / RESUME cycle — REAL DB integration tests.

WHAT THIS PROVES (read the contract: _workspace/dubbed_credit_be_contract.md)
============================================================================
The Dubbed (no-TTS reup) credit gate must NOT silently ship an un-credited reup.
When ingest returns no source credit (no uploader/handle/logo), the runner PAUSES
the job into `needs_input` BEFORE assemble; the owner then resumes by PROVIDING
credit or explicitly SKIPPING; the resumed job re-assembles from the cached subs/
filler WITHOUT re-translating/re-detecting and WITHOUT re-pausing.

SEAMS (explicit per test)
-------------------------
- PAUSE / RESUME-drive / NO-RE-PAUSE: drive `runner._process_job` directly against
  the test DB, with every external side effect (download/ingest/translate/filler/
  assemble/render-cache) stubbed. `assemble_dubbed` is a SPY so we can assert it is
  (a) NOT reached at pause time, and (b) reached on resume with the credit threaded.
  The job dict is obtained via the REAL `runner._claim_job()` (queued -> running),
  exactly as the worker loop does, so the dict shape is authentic.
- RESUME endpoints (provide / skip / 404 / 409): driven through the REAL FastAPI
  route `POST /api/jobs/{id}/resume` and `GET /api/jobs/needs-input` via TestClient
  (no `with` block -> lifespan/worker NOT started), against the same test DB.

NOT PROVEN: real ffmpeg assembly output (assemble is stubbed). This verifies the
CONTROL FLOW + DB state + the credit values handed to assemble — not pixels.

Run (cwd = Dashboard/api):
  .venv/Scripts/python.exe -m pytest test/fixtures/test_dubbed_credit_needs_input.py -v
"""
import os
import sys
import tempfile

import pytest

# Throwaway media root (nothing writes media here; keep it off the real tree).
_SCRATCH = tempfile.mkdtemp(prefix="cf_dubbed_credit_test_")
os.environ.setdefault("CONTENT_OUTPUT_ROOT", _SCRATCH)

# api package importable (this file is under api/test/fixtures/). conftest.py at
# api/test/ already forced PGDATABASE=contentfactory_test + guarded db.DATABASE_URL.
_API_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _API_DIR not in sys.path:
    sys.path.insert(0, _API_DIR)

import db          # noqa: E402  (conftest already re-pointed db.DATABASE_URL)
import generate    # noqa: E402
import render_cache  # noqa: E402
import runner      # noqa: E402


# A non-empty fake translation/filler result so the dubbed branch has a real record
# to cache. The exact contents don't matter for the credit gate; what matters is that
# they are PERSISTED at pause time and REUSED (not regenerated) on resume.
FAKE_SUBS = [
    {"start": 0.0, "end": 2.0, "text_vi": "Cau phu de mot"},
    {"start": 2.0, "end": 4.0, "text_vi": "Cau phu de hai"},
]
FAKE_FILLER = [{"start": 1.0, "end": 1.2, "reason": "um"}]


# --------------------------------------------------------------------------- #
# DB seed / teardown helpers (test DB only — guarded by conftest).
# --------------------------------------------------------------------------- #
def _seed_page() -> int:
    """Insert a page; return its id. Reused across tests.

    NOTE: pages.architecture_type was dropped in the schema redesign (render_mode/
    edit_mode are now per-JOB). The seeded job below carries edit_mode='dubbed', which
    is what drives the credit gate — the page only needs to exist."""
    with db.get_conn() as conn:
        return conn.execute(
            "INSERT INTO pages (name, language, status)"
            " VALUES (%s, 'vi', 'active') RETURNING id",
            (f"dubbed_test_page_{os.getpid()}_{_uniq()}",),
        ).fetchone()["id"]


_COUNTER = [0]


def _uniq() -> int:
    _COUNTER[0] += 1
    return _COUNTER[0]


def _seed_queued_dubbed_job(page_id: int, *, add_credit: bool = True,
                            link: str = "https://youtu.be/abc") -> int:
    """Insert a QUEUED dubbed job (edit_mode='dubbed', input_type='link'); return id."""
    with db.get_conn() as conn:
        return conn.execute(
            "INSERT INTO jobs (page_id, input_type, input_payload, status, edit_mode,"
            " aspect, add_credit, publish) "
            " VALUES (%s, 'link', %s, 'queued', 'dubbed', '9:16', %s, false) RETURNING id",
            (page_id, link, add_credit),
        ).fetchone()["id"]


def _job_row(job_id: int) -> dict:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT id, status, needs_input, reuse_script_video_id, progress_step,"
            " progress_pct FROM jobs WHERE id = %s", (job_id,)).fetchone()


def _video_row(video_id: int) -> dict:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT id, status, script, source_name, source_link FROM videos WHERE id = %s",
            (video_id,)).fetchone()


def _video_for_job(job_id: int) -> dict | None:
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT id, status, script, source_name, source_link FROM videos"
            " WHERE job_id = %s ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()


@pytest.fixture
def page_id():
    """A fresh translate page per test (rows cleaned up after)."""
    pid = _seed_page()
    yield pid
    # Teardown: delete this page's jobs/videos then the page (FK order).
    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM videos WHERE job_id IN (SELECT id FROM jobs WHERE page_id = %s)",
            (pid,))
        conn.execute("DELETE FROM jobs WHERE page_id = %s", (pid,))
        conn.execute("DELETE FROM pages WHERE id = %s", (pid,))


# --------------------------------------------------------------------------- #
# Runner stub fixture: stub every external side effect; SPY on assemble_dubbed,
# _translate_subs_to_vi, _detect_filler_ranges, and the literal `claude -p` spawn.
# --------------------------------------------------------------------------- #
@pytest.fixture
def stubs(monkeypatch):
    rec = {
        "ingest_called": False,
        "download_called": False,
        "translate_calls": 0,
        "filler_calls": 0,
        "assemble_calls": [],        # each = the DubbedAssembleRequest the spy received
        "claude_popen": [],          # any `claude -p` spawn (must stay empty)
        # ingest credit fields to return (overridden per test):
        "ingest_uploader": None,
        "ingest_handle": None,
        "ingest_logo": None,
        "ingest_sourceUrl": None,
    }

    # --- source download: no network, return a fake local mp4 path ---
    def _spy_download(req, *a, **k):
        rec["download_called"] = True
        return {"videoPath": os.path.join(_SCRATCH, "src.mp4"), "durationS": 4.0}
    monkeypatch.setattr(runner, "download_source_video", _spy_download)

    # --- ingest: valid segments, but credit fields are test-controlled ---
    def _spy_ingest(req, *a, **k):
        rec["ingest_called"] = True
        return {
            "uploader": rec["ingest_uploader"],
            "sourceUrl": rec["ingest_sourceUrl"],
            "logoPath": rec["ingest_logo"],
            "handle": rec["ingest_handle"],
            "title": "Ingest Title",
            "segments": [{"start": 0.0, "end": 4.0, "text": "src transcript"}],
            "transcript": "src transcript", "language": "en", "durationS": 4.0,
        }
    monkeypatch.setattr(runner, "generate_ingest", _spy_ingest)

    # --- the two `claude -p` dubbed steps: spied; return canned data, count calls ---
    def _spy_translate(segments, *a, **k):
        rec["translate_calls"] += 1
        return [dict(s) for s in FAKE_SUBS]
    monkeypatch.setattr(runner, "_translate_subs_to_vi", _spy_translate)

    def _spy_filler(segments, *a, **k):
        rec["filler_calls"] += 1
        return [dict(f) for f in FAKE_FILLER]
    monkeypatch.setattr(runner, "_detect_filler_ranges", _spy_filler)

    # --- assemble_dubbed: SPY. Records the request; never touches ffmpeg. ---
    def _spy_assemble(req, *a, **k):
        rec["assemble_calls"].append(req)
        return {"videoPath": os.path.join(_SCRATCH, "out.mp4"), "durationS": 3.8,
                "width": 1080, "height": 1920}
    monkeypatch.setattr(runner, "assemble_dubbed", _spy_assemble)

    # --- guard the literal `claude -p` subprocess spawn (must never fire here) ---
    _real_popen = generate.subprocess.Popen

    def _guarded_popen(args, *a, **k):
        try:
            argv = [str(x) for x in args] if isinstance(args, (list, tuple)) else [str(args)]
            head = os.path.basename(argv[0]).lower() if argv else ""
            if (argv and (argv[0] == generate.CLAUDE_BIN or head.startswith("claude"))
                    and "-p" in argv):
                rec["claude_popen"].append(argv)
                raise AssertionError(f"claude -p spawned on dubbed path! argv={argv}")
        except AssertionError:
            raise
        except Exception:
            pass
        return _real_popen(args, *a, **k)
    monkeypatch.setattr(generate.subprocess, "Popen", _guarded_popen)

    # --- progress / model-busy / time-ramp: no-ops so we don't sleep or hit cb wiring.
    monkeypatch.setattr(runner, "set_model_busy", lambda *a, **k: None)
    monkeypatch.setattr(runner, "set_progress_cb", lambda *a, **k: None)
    monkeypatch.setattr(runner, "set_ff_progress_cb", lambda *a, **k: None)
    monkeypatch.setattr(runner, "make_thumbnail", lambda *a, **k: None)
    # _run_with_time_ramp wraps a thunk; just call it (no estimate thread / sleeps).
    monkeypatch.setattr(runner, "_run_with_time_ramp",
                        lambda jid, step, msg, lo, hi, exp, fn: fn())
    # No real probe (returns 0 -> no clamp / no budget on the dubbed path anyway).
    monkeypatch.setattr(runner, "_probe_duration", lambda *a, **k: 4.0)
    # render-cache snapshot + post-success cleanup -> no-op.
    monkeypatch.setattr(render_cache, "store_render", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_cleanup_job_intermediates", lambda *a, **k: None)
    # _finalize_video writes real DB row (status='ready') — KEEP it real (cheap UPDATE).
    return rec


def _claim_specific(job_id: int) -> dict | None:
    """Claim ONE SPECIFIC queued job (queued -> running) and return the worker's
    exact RETURNING dict shape. This mirrors runner._claim_job but targets a single
    id so a co-resident queued job (e.g. the 409 fixture) can't be grabbed instead —
    test isolation, not a behavioral change to the claim itself (same columns, same
    queued->running transition)."""
    with db.get_conn() as conn:
        return conn.execute(
            """
            UPDATE jobs SET status = 'running'
             WHERE id = (SELECT id FROM jobs WHERE id = %s AND status = 'queued'
                          FOR UPDATE SKIP LOCKED)
            RETURNING id, page_id, input_type, input_payload, voice, edit_mode, comment,
                      source_video_id, aspect, target_sec, add_credit, publish,
                      render_model, voice_clone_model, src_audio_volume, clone_of_video_id,
                      reuse_script_video_id, title, publish_platform
            """,
            (job_id,),
        ).fetchone()


def _drive_claimed(job_id: int):
    """Claim the seeded job (queued -> running) the way the worker does, then run
    _process_job on the real claimed dict. Returns the claimed dict."""
    job = _claim_specific(job_id)
    assert job is not None and job["id"] == job_id, (
        f"could not claim job {job_id} (is it queued?); got {job and job.get('id')}")
    runner._process_job(job)
    return job


# =========================================================================== #
# TEST 1 — PAUSE: dubbed job, no credit from ingest -> needs_input, NO assemble.
# =========================================================================== #
def test_pause_when_ingest_returns_no_credit(page_id, stubs):
    """SEAM: runner._process_job (claimed job) against the test DB.

    Ingest returns empty uploader/handle/logo (and no sourceUrl) but valid segments.
    Expect: the job parks in needs_input BEFORE assemble_dubbed is reached."""
    job_id = _seed_queued_dubbed_job(page_id)
    # ingest credit fields all empty (the default in `stubs`).

    _drive_claimed(job_id)

    j = _job_row(job_id)
    assert j["status"] == "needs_input", f"job not parked: status={j['status']!r}"
    ni = j["needs_input"]
    assert isinstance(ni, dict), f"needs_input not a dict: {ni!r}"
    assert ni["kind"] == "credit", f"kind != credit: {ni!r}"
    # All four fields were empty -> all four reported missing.
    assert set(ni["missingFields"]) == {"sourceName", "handle", "logo", "sourceLink"}, \
        f"missingFields wrong: {ni['missingFields']!r}"
    assert ni["creditDecision"] is None, f"creditDecision must be null at pause: {ni!r}"
    assert isinstance(ni.get("prefill"), dict), "prefill must be present"
    vid = ni.get("videoId")
    assert vid, f"needs_input must carry a videoId: {ni!r}"

    v = _video_row(vid)
    assert v["status"] == "needs_input", f"video not parked: {v['status']!r}"
    # The dubbed record (subs+filler) was persisted at pause time so resume can reuse it.
    assert isinstance(v["script"], dict) and v["script"].get("mode") == "dubbed", \
        f"dubbed record not saved on pause: {v['script']!r}"
    assert v["script"]["subs"] == FAKE_SUBS, "cached subs missing/altered at pause"

    # CRUCIAL: the pause intercepts BEFORE assemble.
    assert stubs["assemble_calls"] == [], \
        "assemble_dubbed WAS called despite the credit pause (gate did not intercept)"
    # translate/filler ran exactly once (this was a fresh, not-yet-resumed job).
    assert stubs["translate_calls"] == 1 and stubs["filler_calls"] == 1
    assert stubs["claude_popen"] == [], "no real claude -p should spawn (both steps spied)"


# =========================================================================== #
# TEST 2 — RESUME-PROVIDE: endpoint records 'provided' + writes credit; then the
# resumed run reuses the cache, does NOT re-pause, reaches assemble WITH credit.
# =========================================================================== #
def test_resume_provide_then_assemble_gets_credit(page_id, stubs):
    """SEAM A (endpoint): POST /api/jobs/{id}/resume via TestClient.
       SEAM B (runner): re-drive _process_job on the re-queued job."""
    from fastapi.testclient import TestClient
    import main

    job_id = _seed_queued_dubbed_job(page_id)
    _drive_claimed(job_id)  # -> parks in needs_input
    parked = _job_row(job_id)
    assert parked["status"] == "needs_input"
    video_id = parked["needs_input"]["videoId"]

    # --- SEAM A: resume with PROVIDE ---
    client = TestClient(main.app)  # no `with` => lifespan/worker NOT started
    resp = client.post(f"/api/jobs/{job_id}/resume",
                       json={"skip": False, "sourceName": "X",
                             "sourceLink": "http://src.example/x",
                             "handle": "@xchan", "logo": "E:/secrets/x/logo.png"})
    assert resp.status_code == 200, f"resume failed: {resp.status_code} {resp.text}"
    body = resp.json()
    assert body["status"] == "queued"
    assert body["creditDecision"] == "provided", f"resume body: {body}"

    # Endpoint side effects: job re-queued, reuse points at the parked video, credit
    # written onto the video row, decision persisted in needs_input.
    j2 = _job_row(job_id)
    assert j2["status"] == "queued", f"job not re-queued: {j2['status']!r}"
    assert j2["reuse_script_video_id"] == video_id, \
        f"reuse_script_video_id not set to parked video: {j2['reuse_script_video_id']}"
    assert j2["needs_input"]["creditDecision"] == "provided"
    v2 = _video_row(video_id)
    assert v2["source_name"] == "X", f"sourceName not written: {v2['source_name']!r}"
    assert v2["source_link"] == "http://src.example/x", \
        f"sourceLink not written: {v2['source_link']!r}"
    assert v2["status"] == "rendering", f"video not back to rendering: {v2['status']!r}"

    # --- SEAM B: drive the resumed job. ---
    _drive_claimed(job_id)

    # NO re-translate / NO re-detect (dubbed record reused from cache).
    assert stubs["translate_calls"] == 1, \
        f"resume re-translated! translate_calls={stubs['translate_calls']} (expected 1 from pause)"
    assert stubs["filler_calls"] == 1, \
        f"resume re-detected filler! filler_calls={stubs['filler_calls']}"
    assert stubs["claude_popen"] == [], "resume must not spawn claude -p"
    # NO re-pause.
    j3 = _job_row(job_id)
    assert j3["status"] != "needs_input", "resumed job re-parked into needs_input!"
    # assemble IS reached now, exactly once.
    assert len(stubs["assemble_calls"]) == 1, \
        f"assemble_dubbed not reached on resume (calls={len(stubs['assemble_calls'])})"
    # The credit values flowed into assemble.
    areq = stubs["assemble_calls"][0]
    assert areq.sourceName == "X", f"assemble sourceName wrong: {areq.sourceName!r}"
    assert areq.sourceLink == "http://src.example/x", \
        f"assemble sourceLink wrong: {areq.sourceLink!r}"
    assert areq.sourceHandle == "@xchan", f"assemble handle wrong: {areq.sourceHandle!r}"
    assert areq.sourceLogo == "E:/secrets/x/logo.png", \
        f"assemble logo wrong: {areq.sourceLogo!r}"
    # The cached subs/filler reached assemble unchanged (no regeneration).
    assert areq.subs == FAKE_SUBS, "assemble did not get the cached subs"
    assert areq.filler == FAKE_FILLER, "assemble did not get the cached filler"
    # Job finished cleanly.
    assert j3["status"] == "done", f"resumed job did not complete: {j3['status']!r}"


# =========================================================================== #
# TEST 3 — RESUME-SKIP: endpoint records 'skipped' (NOT null); resume reaches
# assemble; the source fields are whatever partial value existed (here: none).
# =========================================================================== #
def test_resume_skip_records_skipped_and_assembles(page_id, stubs):
    """SEAM A (endpoint) + SEAM B (runner). Skip = deliberate no-credit reup."""
    from fastapi.testclient import TestClient
    import main

    job_id = _seed_queued_dubbed_job(page_id)
    _drive_claimed(job_id)
    parked = _job_row(job_id)
    video_id = parked["needs_input"]["videoId"]

    client = TestClient(main.app)
    resp = client.post(f"/api/jobs/{job_id}/resume", json={"skip": True})
    assert resp.status_code == 200, f"resume(skip) failed: {resp.status_code} {resp.text}"
    body = resp.json()
    assert body["creditDecision"] == "skipped", \
        f"skip must record 'skipped' (NOT null/provided): {body}"

    j2 = _job_row(job_id)
    assert j2["status"] == "queued"
    # Persisted decision is the explicit 'skipped' discriminator, not null.
    assert j2["needs_input"]["creditDecision"] == "skipped", \
        f"persisted decision not 'skipped': {j2['needs_input']}"
    # Partial source values (none were provided) — not a bare error, just empty.
    v2 = _video_row(video_id)
    assert v2["source_name"] is None and v2["source_link"] is None, \
        f"skip kept partial values; got name={v2['source_name']!r} link={v2['source_link']!r}"

    # Drive the resumed (skipped) job: reaches assemble, no re-pause, no regen.
    _drive_claimed(job_id)
    assert stubs["translate_calls"] == 1 and stubs["filler_calls"] == 1, \
        "skip-resume re-ran translate/filler"
    assert len(stubs["assemble_calls"]) == 1, "skip-resume did not reach assemble"
    j3 = _job_row(job_id)
    assert j3["status"] != "needs_input", "skip-resume re-parked!"
    areq = stubs["assemble_calls"][0]
    # On skip, no credit fields are present, but addCredit may still be True — the
    # assembler's own slate-skip handles the empty case. The recorded DECISION is the
    # honesty witness, not the field contents.
    assert areq.sourceName is None and areq.sourceHandle is None and areq.sourceLogo is None, \
        f"skip should carry no credit fields: name={areq.sourceName!r}"


# =========================================================================== #
# TEST 4 — NO-RE-PAUSE GUARD: a resumed job with creditDecision set must NOT re-park
# even though credit is still missing (the skip case). This isolates the guard
# (`_load_credit_decision is not None`) from the cache-reuse path.
# =========================================================================== #
def test_resumed_job_with_decision_does_not_repause(page_id, stubs):
    """SEAM: endpoint (skip) + runner re-drive, PLUS a direct helper assertion.

    Credit is STILL missing after a SKIP resume, yet the runner must not return to
    needs_input. HOW the no-re-pause is achieved (verified, honest): a resumed dubbed
    job carries reuse_script_video_id, so the runner takes the `elif reuse_dubbed:`
    branch and `_do_ingest` is False — the credit GATE is never re-entered on resume.
    The `_decision is None` clause in the gate is a defensive belt-and-suspenders that
    only matters if ingest ever re-ran; on the normal resume it is not the mechanism.
    This test asserts BOTH: (1) the OUTCOME (no re-park, reaches assemble) and (2) the
    guard HELPER `_load_credit_decision` returns the recorded decision so the gate's
    guard clause WOULD also hold if reached."""
    from fastapi.testclient import TestClient
    import main

    job_id = _seed_queued_dubbed_job(page_id)
    _drive_claimed(job_id)  # park
    client = TestClient(main.app)
    r = client.post(f"/api/jobs/{job_id}/resume", json={"skip": True})
    assert r.status_code == 200 and r.json()["creditDecision"] == "skipped"

    # (2) Direct helper-level proof of the guard input: the runner reads the decision
    # via _load_credit_decision; after a skip it must be the explicit 'skipped' (not
    # None), which is exactly what the gate's `_decision is None` guard tests against.
    assert runner._load_credit_decision(job_id) == "skipped", \
        "_load_credit_decision must report 'skipped' so the re-pause guard would hold"

    # Sanity: credit is genuinely still missing on the video (the gate's "missing"
    # condition would be TRUE again) — so only the resolution state prevents re-pause.
    parked_vid = _job_row(job_id)["needs_input"]["videoId"]
    v = _video_row(parked_vid)
    assert not (v["source_name"]), "precondition: credit must still be missing for this guard test"

    # (1) OUTCOME: drive the resumed job; it must not re-park and must reach assemble.
    _drive_claimed(job_id)
    j = _job_row(job_id)
    assert j["status"] != "needs_input", \
        "GUARD FAILED: resumed job re-parked into needs_input despite creditDecision set"
    assert j["status"] == "done", f"resumed job should complete, got {j['status']!r}"
    assert len(stubs["assemble_calls"]) == 1, "resumed job must reach assemble exactly once"


# =========================================================================== #
# TEST 6 (POINT 2) — DISABLED CREDIT: a FRESH dubbed job seeded with add_credit
# =False must NOT park. It records creditDecision='disabled' as a non-pausing
# audit write, then ships normally (status -> 'done') with addCredit=False threaded
# all the way into assemble_dubbed. This pins the new `else: _record_credit_decision`
# branch (runner.py ~1281-1292) and the new helper `_record_credit_decision`.
# =========================================================================== #
def test_disabled_credit_records_decision_and_ships(page_id, stubs):
    """SEAM: runner._process_job (claimed fresh job) against the test DB.

    add_credit=False means the owner deliberately turned the source-credit slate OFF.
    The runner must NOT enter the credit pause gate (that lives under `if add_credit:`);
    instead the `else` branch records a deliberate 'disabled' decision WITHOUT parking,
    and the job runs to completion. Ingest credit fields are irrelevant on this path
    (the gate is never reached), so we leave them empty (the `stubs` default)."""
    job_id = _seed_queued_dubbed_job(page_id, add_credit=False)

    _drive_claimed(job_id)

    # OUTCOME: the job ships — it must NOT park into needs_input; it completes.
    j = _job_row(job_id)
    assert j["status"] != "needs_input", \
        f"disabled-credit job WRONGLY parked: status={j['status']!r}"
    assert j["status"] == "done", f"disabled-credit job did not complete: {j['status']!r}"

    # AUDIT FLAG: the deliberate 'disabled' decision is recorded and readable back.
    assert runner._load_credit_decision(job_id) == "disabled", \
        "_load_credit_decision must report the recorded 'disabled' audit flag"
    # The needs_input dict carries creditDecision='disabled' (kind tag preserved by the
    # MERGE in _record_credit_decision). This was a non-pausing write — verify it did
    # NOT use the parking payload shape (no missingFields/prefill were written).
    ni = j["needs_input"]
    assert isinstance(ni, dict), f"needs_input not a dict after disabled write: {ni!r}"
    assert ni.get("creditDecision") == "disabled", \
        f"creditDecision not 'disabled' in needs_input: {ni!r}"

    # The video was never parked (no videos.status='needs_input' was set on this path).
    v = _video_for_job(job_id)
    assert v is not None, "no video row created for the dubbed job"
    assert v["status"] != "needs_input", \
        f"video wrongly parked on the disabled path: {v['status']!r}"

    # assemble_dubbed WAS reached exactly once, and the request carries addCredit=False.
    assert len(stubs["assemble_calls"]) == 1, \
        f"assemble_dubbed not reached exactly once (calls={len(stubs['assemble_calls'])})"
    areq = stubs["assemble_calls"][0]
    assert areq.addCredit is False, \
        f"addCredit not threaded as False into assemble: {areq.addCredit!r}"
    # The cached subs/filler reached assemble unchanged.
    assert areq.subs == FAKE_SUBS, "assemble did not get the (fresh-path) subs"
    assert areq.filler == FAKE_FILLER, "assemble did not get the (fresh-path) filler"

    # FRESH path: translate/filler each ran exactly once; no real claude -p spawned.
    assert stubs["translate_calls"] == 1 and stubs["filler_calls"] == 1, \
        f"translate/filler call counts wrong: {stubs['translate_calls']}/{stubs['filler_calls']}"
    assert stubs["claude_popen"] == [], "no real claude -p should spawn (both steps spied)"


# =========================================================================== #
# TEST 5 — endpoint error paths (404 missing / 409 not parked) + needs-input list.
# =========================================================================== #
def test_resume_404_and_409_and_discovery_list(page_id, stubs):
    """SEAM: TestClient endpoints only.

    - 404 when the job id doesn't exist.
    - 409 when the job exists but is NOT in needs_input.
    - GET /api/jobs/needs-input returns the parked job with the contract shape."""
    from fastapi.testclient import TestClient
    import main
    client = TestClient(main.app)

    # 404: a job id that does not exist.
    r404 = client.post("/api/jobs/999999999/resume", json={"skip": True})
    assert r404.status_code == 404, f"expected 404 for missing job, got {r404.status_code}"

    # 409: a queued (not parked) job is not resumable.
    queued_id = _seed_queued_dubbed_job(page_id)
    r409 = client.post(f"/api/jobs/{queued_id}/resume", json={"skip": True})
    assert r409.status_code == 409, \
        f"expected 409 for a non-needs_input job, got {r409.status_code} {r409.text}"

    # Park a job, then it must appear in the discovery list with the right shape.
    parked_id = _seed_queued_dubbed_job(page_id)
    _drive_claimed(parked_id)
    rlist = client.get("/api/jobs/needs-input")
    assert rlist.status_code == 200
    jobs = rlist.json()["jobs"]
    ids = [j["id"] for j in jobs]
    assert parked_id in ids, f"parked job {parked_id} not in needs-input list: {ids}"
    parked = next(j for j in jobs if j["id"] == parked_id)
    assert parked["status"] == "needs_input"
    ni = parked["needsInput"]
    assert ni and ni["kind"] == "credit" and ni["creditDecision"] is None, \
        f"needsInput shape wrong in list: {ni!r}"
    # The queued (non-parked) job must NOT carry a needsInput payload.
    rall = client.get("/api/jobs/needs-input").json()["jobs"]
    assert queued_id not in [j["id"] for j in rall], \
        "a queued job leaked into the needs-input discovery list"
