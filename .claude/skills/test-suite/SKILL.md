---
name: test-suite
description: >-
  How to test ContentFactory: write+run pytest for the FastAPI/pipeline/workers
  and tsc/vitest for the web, plus objective media probes (ffprobe, whisper
  transcript vs intended text). Use to add tests, run a suite, or validate a
  change before it's called done. Triggers on "test", "pytest", "vitest",
  "kiểm thử", "viết test", "chạy test", "coverage".
---

# Test suite

Used by **tester**.

## Full cycle
1. **Write** discriminating tests (happy + edge + regression), bound to the spec — never always-pass tests.
   - Backend: `pytest` (api `.venv`) for route contracts, `fetch_jobs` shape, param threading, migration helpers.
   - Web: `npx tsc --noEmit` as the gate; `vitest` when configured.
   - Media: objective probes — `ffprobe` (duration/codec/volume) and faster-whisper transcript compared to the intended text; not subjective listening.
2. **Run** — collect pass/fail/error, coverage if available.
3. **Report** — counts, which failed + why, suggested fix.

## How
- **Incremental** (after each module). Isolate test data under `_dummy_data/` or `test/fixtures/`; clean up DB rows you insert.
- Build repro cases when qa flags a boundary or security needs exploit proof.

## Honesty (critical)
pass/fail reflects the actual run — never mark a failing/un-run test as passing. Blocking core failure → tell `leader` before "done". Artifacts/notes → `_workspace/`.
