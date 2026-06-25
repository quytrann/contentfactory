# QA — `bypass_tts_cache` + reuse-script modal: boundary integrity

Date: 2026-06-25. Scope: per-job `bypass_tts_cache` flag (force-fresh TTS) and the two-button reuse-script modal. Method: cross-checked both sides of each boundary against live evidence (psql, tsc, pytest) — not "code exists".

## Live evidence (run, not inferred)

- **psql** (live `contentfactory` DB, `\d`-equivalent on `information_schema`):
  ```
  column_name      | data_type | is_nullable | column_default
  bypass_tts_cache | boolean   | NO          | false
  ```
  → Column EXISTS on the live `jobs` table, NOT NULL, DEFAULT false. Matches schema + seed intent.
- **tsc** (`Dashboard/web`, `npx tsc --noEmit`): `TSC_EXIT=0` — clean. Independently confirms the FE agent's exit-0 claim.
- **pytest** (`Dashboard/api`, `test/fixtures/test_script_reuse_bypass.py -v`): 7 passed in 1.20s, `PYTEST_EXIT=0`.

## Boundary verdicts

### 1. DB schema ↔ seed ↔ live DB — PASS
- `Dashboard/db/schema.sql:86` — `bypass_tts_cache BOOLEAN NOT NULL DEFAULT FALSE`.
- `Dashboard/db/seed.sql:38` — idempotent `ADD COLUMN IF NOT EXISTS bypass_tts_cache BOOLEAN NOT NULL DEFAULT FALSE`.
- Live DB query (above) confirms the column is actually present, NOT NULL, default false. All three sides agree.

### 2. API write paths (column/value/tuple alignment) — PASS
**create_job INSERT** (`main.py:869-880`): 19 columns, of which `input_type` and `status` are SQL literals (`'link'`, `'queued'`) → 17 `%s` placeholders. Counted VALUES placeholders = 17; tuple length = 17. `bypass_tts_cache` ← `body.bypassTtsCache` (last position, aligned). ALIGNED.

**resume/requeue INSERT** (`main.py:1760-1778`): same 19 columns, but here only `status` is a literal (`input_type` IS a placeholder, fed from `job["input_type"]`) → 18 `%s` placeholders. Counted VALUES placeholders = 18; tuple length = 18. `bypass_tts_cache` ← literal `False` (last position). ALIGNED.
- Resume correctly hardcodes `False` (`main.py:1770-1773` comment + `main.py:1778` value) — a resume must NOT implicitly force-fresh synth. PASS on the "resume must not bypass" requirement.

### 3. Runner read path — PASS
- `runner._claim_job()` RETURNING (`runner.py:230`) includes `bypass_tts_cache` → job dict carries the key.
- `_run_tts()` (`runner.py:1640`) passes `bypassTtsCache=bool(job.get("bypass_tts_cache"))` into `TtsRequest`. `.get()` tolerates older dicts (falsy → normal cached behavior). Field name on the Python side is `bypass_tts_cache` (DB/dict) → `bypassTtsCache` (Pydantic) — correctly translated at this hop.

### 4. generate.py read-skip / write-keep — PASS
- `TtsRequest.bypassTtsCache: bool = False` (`generate.py:2273`).
- READ skip: `generate.py:2335-2341` — when `req.bypassTtsCache`, logs BYPASS; the per-item loop sets `keys[idx] = key` FIRST (line 2339) and only THEN `continue`s on `key is None or req.bypassTtsCache` (line 2340). So `find_cached_tts` is never called (hits stays empty) but `keys` is still fully populated.
- WRITE kept: `generate.py:2434-2440` — `store_tts(key, wav_path)` is gated ONLY on `key and wav_path`, NOT on `bypassTtsCache`. Because `keys[idx]` was populated even on the bypass path, the WRITE still runs. The global `TTS_CACHE=off` env still governs the write independently (store_tts no-ops). CONFIRMED: bypass suppresses READ only, keeps WRITE — matches documented intent.

### 5. Web ↔ API contract (camelCase, no alias) — PASS
- `Dashboard/web/src/api.ts:72` — `NewJobBody` has `bypassTtsCache?: boolean` (optional, camelCase).
- `main.py:824` — `NewJob.bypassTtsCache: bool = False` (camelCase, no Pydantic alias → JSON key must be literally `bypassTtsCache`).
- `CreateVideo.tsx:1324` — `createJob({ ..., bypassTtsCache: reuseScriptVideoId != null ? bypassTtsCache : undefined })`. Key is literally `bypassTtsCache`. When no reuse → `undefined` (omitted, server default False). Name/case/optionality match exactly across the boundary.

## THE CRITICAL FLOW — Button 1 → generate_tts read-skip (every hop verified)

1. Modal Button 1 "Dùng lại kịch bản" (`CreateVideo.tsx:1414`) → `setReuseMode('fresh-audio')`.
2. `bypassTtsCache = reuseMode === 'fresh-audio'` (`CreateVideo.tsx:1190`) → `true`.
3. `createJob({ reuseScriptVideoId, bypassTtsCache: ... })` (`CreateVideo.tsx:1324`) → JSON key `bypassTtsCache: true` (reuse is set, so not undefined).
4. `NewJob.bypassTtsCache` (`main.py:824`) receives `true` (no alias — key matches).
5. `create_job` INSERT (`main.py:880`) → `jobs.bypass_tts_cache = TRUE`.
6. `_claim_job` RETURNING (`runner.py:230`) → job dict key `bypass_tts_cache = True`.
7. `_run_tts` (`runner.py:1640`) → `TtsRequest(bypassTtsCache=bool(job.get("bypass_tts_cache")))` = `True`.
8. `generate_tts` (`generate.py:2335`) → `req.bypassTtsCache` True → skips `find_cached_tts` (read), still computes `keys`, still writes via `store_tts`.

Every hop's field name lines up. Button 2 ("with-audio", `CreateVideo.tsx:1431`) drives the same chain with `bypassTtsCache=false` → cache READ served. NO MISMATCH FOUND on any hop.

## Known limitation (not a bug)
- **Session-only edited-detection** (`CreateVideo.tsx:1182-1184`, `reusedScriptEdited`): the "audio may not match" warning (shown for Button 2 on an edited script, `CreateVideo.tsx:1451`) is driven by an in-session edit flag. If a script was edited in a PRIOR session and reused after reload, the FE cannot know it was edited, so the Button-2 mismatch warning won't fire. This is a documented FE limitation, not a wiring bug — the safe default (Button 1, fresh-audio, bypass=true) is preselected (`CreateVideo.tsx:1189`), and reuse state intentionally does NOT persist across reloads (`CreateVideo.tsx:1175-1176`). Acceptable; flagged for awareness.

## Overall
All 5 boundaries PASS with live/static evidence. Critical Button-1 flow verified end-to-end, field names aligned at every hop. tsc clean, 7/7 pytest green, live DB column present. One known FE limitation (session-only edit detection) noted, not a defect.
