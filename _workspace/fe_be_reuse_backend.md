# Backend: `bypass_tts_cache` end-to-end wiring

Per-job `bypass_tts_cache` flag so the Studio "Dùng lại kịch bản" button can reuse a
saved script but force FRESH TTS: it skips the per-scene TTS cache **READ** (every scene
re-synthesized) while keeping the cache **WRITE** (so the cache still warms).

## Files changed

### `Dashboard/db/schema.sql` (jobs table, ~line 82)
Added `bypass_tts_cache BOOLEAN NOT NULL DEFAULT FALSE` right after `reuse_script_video_id`,
with an English comment describing the read-skip / write-keep semantics.

### `Dashboard/db/seed.sql` (ALTER TABLE jobs, ~line 38)
Added `ADD COLUMN IF NOT EXISTS bypass_tts_cache BOOLEAN NOT NULL DEFAULT FALSE` (idempotent
migration for the live DB), inserted after the `reuse_script_video_id` line.

### `Dashboard/api/main.py`
- `class NewJob` (~line 824): added `bypassTtsCache: bool = False` (camelCase API field) with
  a 3-line English comment.
- `create_job` INSERT (~line 869-880): added column `bypass_tts_cache` to the column list, a
  matching `%s` placeholder, and value `body.bypassTtsCache` to the VALUES tuple.
- Requeue/resume INSERT path (~line 1762-1774): added column `bypass_tts_cache` and bound it to a
  **literal `False`** (NOT carried from the original job). Documented in an English comment: a
  plain resume/retry should reuse cached TTS (the failure was downstream, not the voice), so it
  must not implicitly force-fresh synth — only an explicit `create_job` reuse-script sets the bypass.

### `Dashboard/api/runner.py`
- `_claim_job()` RETURNING list (~line 230): added `bypass_tts_cache` (after `reuse_script_video_id`)
  so the runner's job dict carries the flag.
- `_run_tts()` `TtsRequest(...)` build (~line 1634-1640): added
  `bypassTtsCache=bool(job.get("bypass_tts_cache"))` with a comment noting `.get()` tolerates
  older job dicts lacking the key (falsy → normal cached behavior).

### `Dashboard/api/generate.py`
- `class TtsRequest` (~line 2273): added `bypassTtsCache: bool = False` with an English comment.
- `generate_tts` cache-read loop (~line 2325-2348): when `req.bypassTtsCache` is true, emit
  `log.info("[tts_cache] BYPASS read (bypass_tts_cache) — %d scenes forced to synth", len(items))`
  and SKIP `find_cached_tts` (the per-scene loop `continue`s before the lookup, so `hits` stays
  empty → every scene is a forced MISS). `keys` is STILL computed for every scene, so the
  unchanged post-synth `store_tts` WRITE path keeps warming the cache. All existing
  best-effort / never-fatal semantics untouched. The global `TTS_CACHE` env still governs the
  write (`off` => no write); this flag only suppresses the read.

## Verification

- **psql migration (live DB):** Ran against local `contentfactory` (psql at
  `E:\Installed\PostgreSQL16\bin`, creds from `Dashboard/api/.env`). `ALTER TABLE ... ADD COLUMN
  IF NOT EXISTS` succeeded; `\d jobs` confirms:
  `bypass_tts_cache | boolean | not null | false`. **Applied and verified.**
- **Test:** `Dashboard/api/.venv/Scripts/python.exe -m pytest test/fixtures/test_script_reuse_bypass.py -v`
  (cwd=`Dashboard/api`) → **7 passed in 1.33s**. The `_claim_job` RETURNING change and the new
  `TtsRequest` field did NOT break it (`job.get("bypass_tts_cache")` tolerates the stubbed
  `_base_job` dict that lacks the key).
- **Syntax:** `ast.parse` on `generate.py`, `runner.py`, `main.py` → OK.

## Action required (NOT done here)
- **Restart the API server** to pick up the backend edits (leader owns server lifecycle).
- **frontend-engineer:** add `bypassTtsCache: boolean` to the NewJob client payload/TS types and
  wire the "Dùng lại kịch bản" button to send `bypassTtsCache: true` (with `reuseScriptVideoId`).
- **qa:** verify the API ↔ web boundary for the new `bypassTtsCache` field.

## Honest caveats
- Not exercised end-to-end against a real running server / real TTS worker — verified via unit
  test (stubs `generate_tts`), AST parse, and the live-DB column check only.
- The new flag is NOT returned by `fetch_jobs` to the web (none was requested); if the FE needs to
  display it, that's a follow-up.

---

## Follow-up: evict stale TTS cache entry on the BYPASS path (2026-06-25)

On the bypass (force-fresh) path, before re-synthesizing we now DELETE the existing
cache entry for each scene's key, so stale/corrupt entries don't linger and a later
non-bypass job with the same text gets fresh audio.

### Files / lines changed
- `Dashboard/api/tts_cache.py` — added public `delete_tts(key)` immediately before
  `store_tts` (inserted ~line 162). Best-effort, never raises; resolves the on-disk
  path via existing `_cache_path(key)` (path-guard inside CONTENT_OUTPUT_ROOT); on the
  guard's `ValueError` returns False with no raise; removes `<key>.wav` and a stray
  `<key>.wav.part` if present; logs `print(f"[tts_cache] DELETE {key[:8]}")` on an
  actual delete; NOT gated on `cache_writes_enabled()` (eviction must work even when
  writes are toggled off). Returns True iff something was deleted (missing file = False).
- `Dashboard/api/generate.py` `generate_tts` — split the combined bypass branch in the
  read loop (was `if key is None or req.bypassTtsCache: continue`). Now: `if key is None:
  continue`, then `if req.bypassTtsCache:` calls `tts_cache.delete_tts(key)` wrapped in
  try/except (double-guard so a delete fault can't break synth), keeps the existing
  BYPASS log behavior, then `continue` → scene falls through to MISS → worker synth →
  existing `store_tts` re-populates the key fresh.

### delete_tts signature
`def delete_tts(key: str) -> bool` — returns True iff a file was actually removed.

### Test result
`Dashboard/api/.venv/Scripts/python.exe -m pytest test/fixtures/test_script_reuse_bypass.py -v`
(cwd=`Dashboard/api`) → **7 passed, 1 warning in 1.22s**. The 1 warning is a PRE-EXISTING
`DeprecationWarning: invalid escape sequence '\C'` from the `tts_cache.py` line-1 docstring
(`E:\ContentFactory` path literal) — unrelated to this change; line 1 was not touched.
`ast.parse` of both edited files → OK (no syntax error).

### HONESTY NOTE
When the script text is UNCHANGED, the freshly-synthesized wav has the SAME key, so
delete + re-store is effectively a refresh of the SAME entry (store_tts already overwrites
atomically via `.part` + `os.replace`). The delete therefore mainly (a) guarantees a clean
slot and (b) evicts a corrupt/zero-byte cached wav for that key. It does NOT evict the OLD
key when the user CHANGED the narration text — that old text hashes to a DIFFERENT key and
is simply orphaned (it lingers under its own path). That orphan behavior is existing cache
behavior and OUT OF SCOPE for this change unless leader decides otherwise.

### Restart note
The API server must be restarted for these backend edits to take effect — leader will
handle the restart; I did not restart it.

---

## Follow-up: 24h TTL eviction for per-scene TTS cache (2026-06-25)

### Files changed
- `Dashboard/api/tts_cache.py`
  - Added imports `threading`, `time` (top of module).
  - `find_cached_tts(key)`: on a HIT (valid cached path about to be returned) now bumps mtime via `os.utime(path, None)` wrapped in try/except. An mtime-bump failure does NOT turn a HIT into a miss — the valid path is still returned. English comment explains the TTL last-used rationale (Windows atime unreliable; daily-reused wav synthesized >24h ago must not be evicted).
  - Added `evict_stale_tts(max_age_seconds: int = 86400) -> int`.
  - Added `start_eviction_async(max_age_seconds: int = 86400) -> None`.
  - `store_tts` unchanged (writes fresh => mtime naturally "now", as specified).
- `Dashboard/api/main.py`
  - Added `import tts_cache`.
  - Wired `tts_cache.start_eviction_async()` into the EXISTING `lifespan` async context manager (after `start_runner()`, before `yield`), wrapped in try/except so a failure never blocks startup. English comment added.

### Function signatures
- `evict_stale_tts(max_age_seconds: int = 86400) -> int`
- `start_eviction_async(max_age_seconds: int = 86400) -> None`

### evict_stale_tts behavior
- Walks `tts_dir()` recursively (`os.walk`); if dir doesn't exist returns 0 cleanly.
- For each `*.wav`: if `now - os.path.getmtime > max_age_seconds`, `_guard(path)` then `os.remove`, count++.
- Stray `*.wav.part` files are always removed (interrupted store cleanup), not counted in the deleted total.
- BEST-EFFORT / NEVER RAISES: whole walk wrapped in try/except; each per-file op wrapped in try/except (`OSError`/`ValueError` from guard) and continues the sweep on any bad file.
- After deletion, prunes now-empty `<key[:2]>` shard subdirs (best-effort, ignores errors).
- Logs a single summary line only when n>0: `[tts_cache] evicted {n} stale entry|entries (>{hours:g}h)` (grammatical singular/plural, threshold in hours).
- Returns the count of wav files deleted.

### start_eviction_async behavior
- Spawns `threading.Thread(target=evict_stale_tts, args=(max_age_seconds,), daemon=True).start()`, entire spawn wrapped in try/except so it can never raise into the caller. Non-blocking (daemon thread does the work).

### mtime-bump-on-HIT change (correctness)
Implemented exactly as required: HIT path calls `os.utime(p, None)` (set mtime=now) inside try/except, AFTER `_valid_file` confirms the file but BEFORE returning. mtime-bump failure is swallowed and the valid path is still returned — a bump failure can never demote a HIT to a miss.

### Startup wiring mechanism used
Hooked into the EXISTING FastAPI `lifespan` `@asynccontextmanager` in `main.py` (no new `@app.on_event` added — the app already uses `lifespan=lifespan`). Call placed after `start_runner()`, before `yield`, wrapped in try/except.

### Verification (real results)
- `ast.parse` on both edited files: OK.
- pytest `test/fixtures/test_script_reuse_bypass.py -v` (cwd=Dashboard/api, .venv python): **7 passed, 1 warning in 1.05s**. The 1 warning is a pre-existing `DeprecationWarning: invalid escape sequence '\C'` in the tts_cache.py module docstring (line 1, `E:\C...` path) — predates and is unrelated to this change; not introduced by my edits.
- Throwaway eviction sanity (inline `python -c`, temp CONTENT_OUTPUT_ROOT in scratchpad, NOT committed): created one fresh wav + one wav with mtime set 48h ago via `os.utime`, called `evict_stale_tts()`. Result: **returned 1; fresh wav kept; stale wav removed**; emitted `[tts_cache] evicted 1 stale entry (>24h)`. Throwaway temp dir deleted afterward (confirmed not present). PASS.

### Scoping note
Per the coordinator's "optionally", per-job-completion eviction in `runner.py` was deliberately NOT added — the startup-trigger is sufficient and lower-risk. `_CLEANUP_NEVER_DIRS` and all runner.py cleanup logic were left untouched.

### Operational note
API restart required for the startup eviction trigger to take effect. Leader will restart (not done here). Backend-only change; no API JSON contract change, so no frontend/qa contract impact.
