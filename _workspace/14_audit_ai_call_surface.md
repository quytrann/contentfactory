# Audit 14 — AI call surface inventory (pre-design for a provider gate)

**Scope:** read-only audit. No project code was modified.
**Date:** 2026-07-30
**Repo root:** `d:\workspace\ContentFactory`

Bottom line up front: **there is exactly ONE LLM provider in the repo (Claude Code headless
CLI) and exactly THREE `subprocess.Popen` spawn sites**, all in
`Dashboard/api/generate.py`. Every text feature funnels through one helper
(`_run_claude_script`), which is the natural insertion point for a provider gate. The two
vision spawn sites are the hard part: they do not pass images as data at all — they pass
**absolute file paths and rely on Claude Code's `Read` tool** to open them, i.e. they
depend on agentic tool-use, not on a multimodal message payload.

No other provider exists anywhere: a repo-wide grep for
`openai|langchain_openai|google.generativeai|genai|mistralai|cohere|ollama|openrouter`
matches only `Dashboard/web/src/views/CreateVideo.tsx` (the string "openvoice-v2" in the
TTS-engine dropdown — a false positive, not an LLM provider).

---

## 1. Every LLM call site

### 1.1 The three real spawn sites (only places a model process is created)

| # | file:line | wrapper | powers | sync/async | invocation |
|---|-----------|---------|--------|-----------|------------|
| A | `Dashboard/api/generate.py:1247-1259` | `_run_claude_script_once` | ALL text generation (script gen, translation, hashtags, filler detection) | **sync**, blocking, called from a worker thread or a `ThreadPoolExecutor` | `Popen([CLAUDE_BIN, "-p", prompt, "--model", SCRIPT_GEN_MODEL, "--max-turns", "1", "--tools", "", "--strict-mcp-config", "--system-prompt", SCRIPT_GEN_SYSTEM_PROMPT, "--output-format", "stream-json", "--verbose"])`, `stdin=DEVNULL`, `stdout/stderr=PIPE`, `encoding="utf-8", errors="replace"`, **no `cwd`**, **no `env`** (inherits API process env), timeout enforced by the caller (not by the CLI) |
| B | `Dashboard/api/generate.py:1367-1380` | `_run_claude_vision_script` | no-speech "visual-explain" fallback script gen | **sync** | same as A **plus** `--tools "Read"`, `--add-dir <frames_dir>`, `--max-turns VISUAL_EXPLAIN_MAX_TURNS (40)`; timeout `VISUAL_EXPLAIN_TIMEOUT (600s)` |
| C | `Dashboard/api/generate.py:4455-4468` | `_vision_cover_prompt` | cover-image art direction (SDXL prompt + VN title + key-word segments) | **sync**, runs inside the cover daemon thread `_run_cover_task` (`generate.py:5460`) | same as B but `--max-turns COVER_VISION_MAX_TURNS (6)`, `--system-prompt _COVER_VISION_SYSTEM_PROMPT`, timeout `COVER_VISION_TIMEOUT (90s)` |

Shared plumbing for all three: `_read_stream_json_result` (`generate.py:1170`) reads the
newline-delimited `stream-json` events in a **daemon reader thread** and enforces the
wall-clock deadline with `thread.join(timeout=...)`; `_kill_proc_tree`
(`generate.py:1144`) hard-kills with `taskkill /F /T` on Windows (claude.exe is a Node
launcher, so `proc.kill()` orphans grandchildren); `_register_job_proc` /
`_unregister_job_proc` (`generate.py:520`, `:532`) register the PID tree so
`POST /api/jobs/{id}/stop` can kill it mid-flight (`kill_job_processes`, `generate.py:544`).

Notably: `FileNotFoundError` on the spawn is the only reason `CLAUDE_BIN` is validated —
there is no provider health check.

### 1.2 Feature-level call sites (all route into site A via `_run_claude_script`)

`_run_claude_script(prompt, timeout, cache_parts=None, batch_idx=0, force_regen=False)` —
`generate.py:1499` — is the single retry/cache/spell-fix wrapper. Its callers:

| feature | call site | notes |
|---|---|---|
| Topic-only script gen (image/stickman, no source) | `generate.py:1881` in `generate_script` (route `POST /generate/script`, `:1874`) | prompt from `_build_prompt` (`:1115`) |
| Footage script gen (single batch) | `generate.py:3159` in `_gen_footage_scenes` | prompt from `_build_footage_prompt` (`:2820`); **disk-cached** |
| Footage script gen (multi-batch, concurrent) | `generate.py:1856` / `:1861` in `_run_batches_parallel` (`:1814`), driven from `_gen_footage_scenes:3193` | ordered results, `SCRIPT_GEN_CONCURRENCY` workers |
| Transform script gen (no-timestamp transcript) | `generate.py:3692`, `:3700` (single) and `:3711` via `_run_batches_parallel` | prompt from `_build_transform_prompt` (`:2726`); **no disk cache** |
| Dubbed VN subtitle translation | `generate.py:2435` in `_translate_subs_to_vi` (`:2399`) | index-echo contract |
| `translate_full` 1:1 translation | `generate.py:2543` in `_translate_full_to_vi` (`:2497`) | **DEAD CODE — no callers** (see §7.6) |
| Dubbed filler / cut-list detection | `generate.py:2626` in `_detect_filler_ranges` (`:2597`) | failure ⇒ `[]`, never fatal |
| Facebook hashtag generation | `generate.py:1756` in `_generate_fb_tags` (`:1701`) | never raises; deterministic fallback |
| Source-title → Vietnamese translation | `generate.py:1597` in `_translate_title_to_vi` (`:1567`) | **DEAD CODE — no callers** (see §7.6) |

### 1.3 Where the pipeline enters these (runner + API surface)

`Dashboard/api/runner.py` imports the generators directly (`runner.py:95-134`) and calls
them in `_process_job`:

- `runner.py:1743` → `generate_script_visual_explain(...)` (vision, site B) — no-speech source
- `runner.py:1771` → `_translate_subs_to_vi(...)` (dubbed)
- `runner.py:1774` → `_detect_filler_ranges(...)` (dubbed)
- `runner.py:1859` → `generate_script_footage(...)` (translate_full path)
- `runner.py:1931` → `generate_script_footage(...)` (normal footage path)
- `runner.py:1945` → `generate_script_transform(...)` (non-footage w/ source)
- `runner.py:2012` → `generate_script(...)` (topic-only)
- `runner.py:441` → `_generate_fb_tags(...)` inside `_auto_fill_fb_tags` (`:417`), non-fatal by contract

HTTP routes that can trigger an LLM call directly:
`POST /generate/script` (`generate.py:1874`), `POST /generate/script/footage` (`:3257`),
`POST /generate/script/transform` (`:3714`), `POST /generate/tags` (`main`-mounted,
`generate.py:6880`), `POST /generate/cover` (`:5586`, async task → vision site C).
`POST /api/jobs/batch/preview` (`main.py:1198`) deliberately makes **no** Claude call
(comment at `main.py:1226`: the owner types the VN title by hand to save subscription usage).

Every script-gen path is wrapped in `_run_with_time_ramp` because the CLI gives no progress
signal (`runner.py:1929-1940` and siblings); expected duration knob
`SCRIPT_RAMP_EXPECTED_SEC` (default `45`, `runner.py:639`).

---

## 2. Exact contract per call

### 2.1 Common output contract (sites A and B)

- **Format demanded:** a bare JSON **array**, no markdown/prose. Enforced twice: via
  `--system-prompt SCRIPT_GEN_SYSTEM_PROMPT` (`generate.py:126-129`) and again in every
  user prompt's closing line.
- **Parse:** `_extract_json_array` (`generate.py:1132`) strips ``` fences, then slices from
  the first `[` to the last `]` and `json.loads`. There is **no JSON repair beyond that
  slice** — a malformed body raises `HTTPException(502, "Could not parse script JSON: ...")`
  (`:1329-1330`).
- **Post-parse mutation:** `_fix_vi_spelling_deep` (`:1483`) walks every string and applies
  `_VI_SPELLING_FIXES` (`:1452`, currently one entry: `chính muồi` → `chín mùi`).
- **Error mapping:** the `{"type":"result"}` event's `subtype`/`is_error` plus the process
  exit code decide the outcome (`:1300-1325`). `error_max_turns` is deliberately remapped to
  **504** so it re-enters the retry path; anything else → 500; missing result event → 502.

### 2.2 Retry / validation / gates

- **Retry:** `SCRIPT_GEN_RETRIES` (default 1 ⇒ 2 attempts) with
  `SCRIPT_GEN_RETRY_BACKOFF` (3.0 s), **only on 504** (`generate.py:1543-1562`). A genuine
  error fails fast. Final failure raises a Vietnamese user-facing 504.
- **Batching:** `_batch_count` / `_split_counts` / `_merge_renumber`
  (`:1783`, `:1791`, `:1798`). Chunk size per edit mode: `SCRIPT_GEN_CHUNK_BY_MODE`
  (`:100`), defaults `summary 19 / recap 15 / commentary 20 / educational 20 /
  translate_full 19`, global fallback `SCRIPT_GEN_CHUNK_SCENES = 23`.
- **Source-length batch cap:** `_gen_footage_scenes:3127-3143` collapses batch count when
  the real content span doesn't reach the later sub-windows (prevents hallucinated scenes
  over empty ranges).
- **Deterministic cleanup:** `_clamp_footage_scenes` (`:3197`) clamps `sourceStart/End`
  into `[0, window]` and DROPS scenes whose start ≥ window (hallucinated timestamps).
- **Keep-ratio regen loop:** `_check_keep_ratio` (`:3225`) + the loop in
  `generate_script_footage` (`:3274-3337`). Bands `_KEEP_RATIO_BAND` (`:2031`):
  `recap 0.60-0.75`, `summary 0.75-0.85`, `translate_full 0.85-1.0`. Out-of-band ⇒ up to
  `_RATIO_REGEN_ATTEMPTS = 1` extra full generation with `_RATIO_REGEN_NUDGE` (`:2050`)
  appended; early-break if the band distance doesn't improve by
  `_RATIO_REGEN_MIN_IMPROVE = 0.01`; never a hard fail (keeps the closest attempt).
- **Three duration gates, all post-generation, all fail rather than truncate:**
  `_enforce_word_ceiling` (`:2222`, modes `{summary, recap}`, tolerance 1.15),
  `_enforce_fixed_source_fit` (`:2248`, mode-agnostic),
  `_enforce_script_duration` (`:2280`, zero-tolerance estimated-seconds gate). All raise
  Vietnamese `HTTPException(422)`.
- **Vision-specific recovery:** `generate_script_visual_explain` (`:3545`) does
  parse+clamp+reject-thin+tile-normalize (`_ve_parse_and_clean`, `:3485`), then ONE shrink
  re-prompt (`:3625-3642`), then a deterministic trailing-sentence/word trim
  (`_ve_shrink_to_budget`, `:3514`).

### 2.3 Prompt sizes and what gets embedded

| prompt builder | line | embeds transcript? | rough size |
|---|---|---|---|
| `_build_prompt` (topic) | `:1115` | no | ~250 words |
| `_build_transform_prompt` | `:2726` | yes, capped `_SOURCE_TRANSCRIPT_CAP = 14000` chars (`:1973`) | up to ~14 KB + ~1.5 KB steering |
| `_build_footage_prompt` | `:2820` | yes, timestamped lines, same 14 000-char cap **per batch** | up to ~14 KB + ~2-3 KB mode-specific steering |
| `_segments_to_numbered_transcript` (dubbed / translate_full / filler) | `:2376` | yes, `i. [start-end] text`, same cap | up to ~14 KB |
| `_build_visual_explain_prompt` | `:3377` | no transcript; embeds ~14 **frame file paths** + metadata | ~1.2 KB text, but the model then reads 14 images (~500-970 vision tokens each) |
| `_vision_cover_prompt` user prompt | `:4418-4453` | no | ~600 words + 1 image |
| `_generate_fb_tags` | `:1723` | narration excerpt trimmed to 1500 chars (`:1722`) | ~1.5 KB |

Shared steering constants reused across builders (a provider layer must keep these
verbatim to avoid behavior drift): `_VI_FULL_TRANSLATION_RULE` (`:1964`),
`_KEEP_ENGLISH_TERMS` (`:2324`), `_PROPER_NOUN_DENSITY` (`:2340`), `_CUT_PROMO` (`:2349`),
`_CUT_PROMO_FOOTAGE` (`:2356`), `EDIT_MODE_GUIDE` (`:1898`).

### 2.4 The non-array contract (site C)

`_vision_cover_prompt` is the only call that expects a JSON **object**:
`{"prompt": "<English SDXL prompt>", "vi_title": "...", "title_segments":[{"text","key"}]}`.
Parsed by `_parse_vision_cover_json` (`:4357`) — fences stripped, outermost `{...}` sliced,
English prompt truncated to 700 chars, `_strip_duration` applied, missing
`title_segments` synthesized. **Never raises**: returns `("", "", [])` and the caller falls
back to the title-only prompt with no overlay (`:5490-5500`).

---

## 3. Shared plumbing a provider layer should reuse (or replace)

### 3.1 Reuse
- **One entry point for text:** `_run_claude_script` (`generate.py:1499`). Swapping the body
  of `_run_claude_script_once` is enough to redirect every text feature. The retry/cache/
  spell-fix/logging shell above it is provider-agnostic already.
- **Stream/deadline/kill trio:** `_read_stream_json_result` (`:1170`), `_kill_proc_tree`
  (`:1144`), `_register_job_proc`/`kill_job_processes` (`:520`, `:544`). A network-based
  provider needs an equivalent cancellation hook, otherwise `POST /api/jobs/{id}/stop`
  silently regresses to "aborts at the next step boundary".
- **Script disk cache:** `_script_cache_key/_path/_get/_put` (`:160-192`), dir
  `Dashboard/api/_script_cache/`, TTL `_SCRIPT_CACHE_TTL_HOURS = 24`. Key parts assembled at
  `_gen_footage_scenes:3148-3152` + per-batch window/index (`:3157`, `:3188`):
  `{edit_mode, word_budget, ratio_nudge, source_transcript_window, batch_index}`.
  **The key does NOT include the model, the provider, the prompt text, or a prompt version.**
  This is the same class of bug already recorded for TTS (`VOICING_VERSION` memory note):
  adding a provider/model choice **must** add it to the cache key, or a job that switches
  provider will silently serve the other provider's cached scenes.
- **Concurrency:** `_run_batches_parallel` (`:1814`) with `SCRIPT_GEN_CONCURRENCY` (4).
  Comment at `:144-147` states the ceiling is the Anthropic subscription rate-limit, not
  VRAM — a per-provider concurrency/rate-limit setting will be needed.
- **Logging:** `log_setup.py` installs one rotating file handler on the root logger AND tees
  `stdout`/`stderr` into `Dashboard/api/logs/api.log` (5 MB × 5). `generate.py` logs
  `[claude] script-gen call start/done/FAILED` with attempt + duration
  (`:1533`, `:1537`, `:1544`) and `[script] cache HIT/WRITE` (`:1520`, `:1541`). Keep these
  tags or the existing debugging workflow (documented in memory: "read the log first")
  breaks.

### 3.2 Duplicated code that should collapse into the provider layer
Sites B and C **duplicate** the whole read/reap/error-classify body of site A
(`:1388-1435` and `:4474-4514` vs `:1276-1330`). Three near-identical copies of
`communicate(timeout=10) → subtype/is_error check → 504 vs 500 vs 502` exist today. A
provider layer should expose one `invoke(prompt, *, tools, extra_dirs, max_turns, timeout,
expect="array"|"object"|"text")` and delete the duplication.

### 3.3 Every env knob (name — default — where read)

| knob | default | read at |
|---|---|---|
| `CLAUDE_BIN` | `"claude"` | `generate.py:47` (actual `.env` value: full path to `claude.exe`) |
| `SCRIPT_GEN_TIMEOUT` | `200` (**`.env` sets `350`**) | `generate.py:61` |
| `SCRIPT_GEN_MODEL` | `"sonnet"` (`.env`: `sonnet`) | `generate.py:108` |
| `SCRIPT_GEN_MAX_TURNS` | `1` | `generate.py:121` |
| `SCRIPT_GEN_RETRIES` | `1` | `generate.py:135` |
| `SCRIPT_GEN_RETRY_BACKOFF` | `3.0` | `generate.py:136` |
| `SCRIPT_GEN_CONCURRENCY` | `4` (`.env`: `4`) | `generate.py:148` |
| `SCRIPT_GEN_CHUNK_SCENES` | `23` (`.env`: `23`) | `generate.py:83` |
| `SCRIPT_GEN_CHUNK_SCENES_<MODE>` | per-mode 19/15/20/20/19 (`.env` sets `_SUMMARY=19`) | `generate.py:100-103` |
| `VISUAL_EXPLAIN_TIMEOUT` | `600` | `generate.py:70` |
| `VISUAL_EXPLAIN_MAX_TURNS` | `40` | `generate.py:75` |
| `COVER_VISION_MAX_TURNS` | `6` | `generate.py:4318` |
| `COVER_VISION_TIMEOUT` | `90` | `generate.py:4319` |
| `SCRIPT_RAMP_EXPECTED_SEC` | `45` | `runner.py:639` |
| `BATCH_MAX_LINKS` | `30` | `main.py:1171` |
| `CF_LOG_FILE` | `<api>/logs/api.log` | `log_setup.py:72` |

There is **no** `LLM_PROVIDER`, `OPENROUTER_*`, API-key, base-URL, or per-feature model knob
today. `SCRIPT_GEN_MODEL` is the only model selector and it is **global**, read once at
import, and shared by all three sites (including both vision calls).

---

## 4. Vision / multimodal specifics

This is the single biggest portability risk, because **no image bytes are ever sent by this
code**.

**How the image reaches the model:**
1. Frames are produced locally. `frames_util.sample_frames(video_path, out_dir, n=14,
   long_edge=576)` (`frames_util.py:71`) extracts 14 evenly-spaced JPEGs (q=3) via the
   project FFmpeg, long edge 576 px — the docstring at `:78` states this targets ~500-970
   vision tokens per image. Cover path instead fetches ONE thumbnail via yt-dlp:
   `_fetch_source_thumbnail` (`generate.py:6436`) → `download_worker.py` in `mode:
   "thumbnail"`.
2. The prompt embeds **absolute filesystem paths**, timestamp-tagged and sorted:
   `_build_visual_explain_prompt:3393-3396` (`- [12.3s] C:\...\frame_03_12.30s.jpg`) and
   `_vision_cover_prompt:4419-4420` (`os.path.realpath(thumb_path)`).
3. The prompt **instructs the model to open them itself**:
   "You MUST OPEN EVERY FRAME with the Read tool and actually look at it"
   (`:3411-3416`).
4. The CLI is granted exactly the capability to do that: `--tools "Read"` plus
   `--add-dir <frames_dir>` (`:1370-1371`, `:4458-4459`). `--add-dir` is what avoids an
   interactive permission prompt.
5. Because each `Read` is a tool round-trip, the calls are **multi-turn**: `--max-turns 40`
   for 14 frames (`:75`), `6` for the single cover thumbnail (`:4318`). An exhausted budget
   surfaces as `error_max_turns` → 504 → one fresh-process retry
   (`_vision_once`, `:3592-3607`).
6. Frames are deleted in a `finally` right after generation (`runner.py:1753-1756`).

**What an alternative provider must support:**
- **Inline multimodal input.** OpenRouter/OpenAI-style APIs take images as
  `image_url` parts with `data:image/jpeg;base64,...`. The provider layer must therefore
  (a) read each JPEG from disk, (b) base64-encode it, (c) build a multi-part user message,
  and (d) **rewrite the prompt text**, because the current text tells the model to use a
  Read tool that will not exist. That is a prompt change, not just a transport change.
- **14 images in one request.** Many free-tier OpenRouter models are text-only, and those
  that are multimodal often cap image count or total request size. 14 × ~576 px JPEG is a
  large single request; a fallback that batches frames across several calls would change
  the tiling contract (`_tile_normalize_scenes`, `:3453`, assumes one draft covering the
  whole timeline).
- **No tool-use needed after the rewrite** — which is actually a simplification, but it
  also removes the current natural rate-limiting: the model no longer "chooses" how many
  frames to look at.
- **Cover path is more forgiving:** one image, `never raises`, falls back to title-only
  (`:4506-4512`, `:5490`). This is the safest first candidate for a provider swap.
- **Visual-explain path is not forgiving:** it raises a Vietnamese 422 and fails the job
  rather than shipping a guess (`:3560-3562`, `:3497`).

---

## 5. Where a per-job model choice would live (DB → API → runner → worker)

### 5.1 Existing per-job option columns on `jobs` (`Dashboard/db/schema.sql:56-139`)

| column | line | meaning |
|---|---|---|
| `render_mode` | `schema.sql:70` | footage \| image \| stickman \| clone |
| `edit_mode` | `:71` | commentary \| recap \| educational \| summary \| dubbed (+ `translate_full` in practice) |
| `voice` | `:72` | TTS preset or `clone:<name>` |
| `voice_clone_model` | `:73` | **the closest precedent** — TTS engine key (`f5-tts` \| `vieneu` \| `omnivoice` \| …) |
| `render_model` | `:74` | image/animation engine key (`sdxl-base`, `stickman-blender`, `passthrough-trim`, …) |
| `aspect`, `target_sec`, `src_audio_volume`, `add_credit`, `title`, `comment` | `:75-80` | other per-job options |
| `bypass_script_cache` | `:102-105` | per-job force-fresh script-gen (skip cache READ, keep WRITE) |
| `reuse_script_video_id` | `:93-96` | script reuse (skips `claude -p` entirely) |
| `facebook_tags`, `cover_image_path` | `:81-86` | |

`videos` (`schema.sql:144-163`) does **not** store the engine keys; the Videos view reads
them by `LEFT JOIN jobs j ON j.id = v.job_id` (`main.py:422`) and echoes them
(`main.py:447-448`). So a new `llm_provider`/`llm_model` on `jobs` alone is automatically
visible on the video card — no `videos` change needed.

### 5.2 The flow to copy verbatim (use `voice_clone_model` / `render_model` as the template)

1. **Schema:** add the column to the `CREATE TABLE jobs` body in `schema.sql` **and** an
   `ADD COLUMN IF NOT EXISTS` line in `Dashboard/db/seed.sql` — that file already carries
   the full idempotent list at `seed.sql:25-45` (`render_mode`, `edit_mode`, `voice`,
   `voice_clone_model`, `render_model`, `aspect`, `target_sec`, … `bypass_tts_cache` at
   `:38`). Standalone one-liners are also used (`seed.sql:48`, `schema.sql:191-194`).
   Apply to the live DB with `psql`. **Migration convention: additive
   `ADD COLUMN IF NOT EXISTS` only, no CHECK constraints** (see the deliberate note at
   `schema.sql:116-117`: no CHECK on `status` "so new values are accepted without a
   migration").
2. **Pydantic model:** add the camelCase field to `NewJob` (`main.py:1062-1093`, e.g.
   next to `renderModel`/`voiceCloneModel` at `:1080-1081`).
3. **INSERT:** add to the column list and the value tuple in `_insert_job`
   (`main.py:1141-1157`) — one column name at `:1143` and one `body.X` at `:1153`.
4. **Batch path:** `BatchCreate` (`main.py:1183-1196`) mirrors the same fields and forwards
   them at `main.py:1273-1274`; add there too or batch jobs silently lose the choice.
5. **Runner read:** add to the `RETURNING` list of `_claim_job`
   (`runner.py:250-256` — `render_model, voice_clone_model` are on `:252`). Then read it as
   `job.get("llm_model")` exactly like `runner.py:2091` reads `voice_clone_model` and
   `runner.py:1455` reads `render_model`.
6. **Thread into the generator:** the script-gen requests are Pydantic models —
   `TransformFootageRequest` (`generate.py:2809`), `TransformRequest` (`:2714`),
   `ScriptRequest` (`:1103`). `skipScriptCache` (`:2817`) is the exact precedent for a
   per-job flag threaded request → `_gen_footage_scenes:3115` → `_run_claude_script`
   `force_regen`. A `provider`/`model` field would follow that same path down to
   `_run_claude_script_once`.
7. **API read-back:** `fetch_jobs` (`main.py:297`, mapping at `:351-352`) and
   `fetch_videos` (`main.py:372`, join `:422`, mapping `:447-448`).
8. **Retry path:** `main.py:2748-2758` re-inserts a job on retry and lists columns
   explicitly — a new column must be added there too or a retry loses it (note the existing
   deliberate exception: `bypass_script_cache` is intentionally reset to FALSE on retry,
   `main.py:2758`).

---

## 6. Frontend path for one more dropdown (`Dashboard/web/src`)

Adding a dropdown is genuinely mechanical — five files, all with a working precedent.

**Option list + dropdown (`views/CreateVideo.tsx`, 234 KB single file):**
- Option constants live at the top of the file: `EDIT_MODES` (`:25-32`), `RENDER_MODELS`
  (`:37-101`, grouped via `m.group`), `INSTALLED_RENDER_MODELS` (`:105`),
  `VOICE_CLONE_MODELS` (`:125-170`, each `{value, installed, short, label, desc}`),
  `ASPECT_OPTIONS` (`:173`).
- The rendered dropdowns are in one 2-column grid block: `Cách biên tập` (`:2881-2892`),
  `Model dựng (engine)` (`:2894-2906`), `Tỷ lệ khung hình` (`:2910-2918`),
  `Model lồng tiếng` (`:2920-2928`). Pattern:
  `<Field label hint={LIST.find(m=>m.value===state)?.desc}><Select value onChange settingKey="studio.X" autoApplyDefault>{LIST.map(...)}</Select></Field>`.
- `Field`, `Select` come from `../ui` (`ui.tsx:255`); `settingKey` persists the owner's
  chosen default in `localStorage` under `cf.default.<settingKey>` via
  `getDefaultPref`/`setDefaultPref` (`ui.tsx:236-300`), and `autoApplyDefault` re-applies it
  on mount.
- State + draft: `useState` at `:1953-1956`; defaults in `STUDIO_DEFAULTS`
  (`:275-276`); draft shape type at `:378-379`; the draft persist effect lists every field
  at `:2163` and its dep array at `:2176`; the reset handler at `:2665-2666`.

**Payload:** the single create call is one line — `api.createJob({... editMode, renderModel,
voiceCloneModel, aspect, ...})` at `CreateVideo.tsx:2616`. The batch modal mirrors the same
settings object at `:3602-3612`.

**Types / client:**
- `api.ts:85-125` `NewJobBody` (add the optional field next to `renderModel`/
  `voiceCloneModel` at `:97-98`); `api.createJob` at `api.ts:274`,
  `api.createJobsBatch` at `:295`.
- `types.ts:87-114` `Job` (engine keys at `:100-101`), `types.ts:116-143` `Video`
  (`:132-133`), plus the batch/job-options shape at `types.ts:458`, `:478-479`.

**Display chips (optional but expected for consistency):** `RENDER_MODEL_LABEL` /
`VOICE_CLONE_MODEL_LABEL` / `EDIT_MODE_LABEL` are exported from `views/Videos.tsx` and
imported by `CreateVideo.tsx:19`; chips are built at `CreateVideo.tsx:3975-3976` and
`Videos.tsx:968-970` (with `hasJobOptions` at `Videos.tsx:992`).

**Mandatory ops step:** the dashboard is served from `dist/` by the API — a `src/*.tsx`
change requires `npm run build` and a bundle-hash check on the live page, otherwise the
owner sees nothing (recorded in memory as `feedback-rebuild-web-after-fe-fix`).

---

## 7. Honest risk notes — what is hard to move off Claude Code headless

Ordered easiest → hardest.

**7.1 Easy (low risk).** `_generate_fb_tags` (`generate.py:1701`) and
`_detect_filler_ranges` (`:2597`). Small prompts, tolerant contracts, both already have
non-fatal fallbacks (`_fb_tags_fallback` `:1682`; `[]` = no cuts). Ideal first provider
targets.

**7.2 Medium.** Topic-only `_build_prompt` (`:1115`) and the dubbed/translate_full
translators (`:2399`, `:2497`). Contract is a strict indexed array with a **1:1 line-count
requirement**; the code already degrades on count mismatch (`:2477-2484`) but blank
subtitles are the observable failure. A weaker free-tier model will drop/merge lines far
more often than Sonnet — expect the mismatch branch to become the common path rather than
the exception.

**7.3 Hard — the footage/transform script generator.** Reasons:
- **Prompt size + batching.** Up to ~14 KB of transcript per batch × up to ~9 batches
  concurrently. Many free OpenRouter models have small context or tight free-tier
  rate/day limits; `SCRIPT_GEN_CONCURRENCY=4` would trip them immediately.
- **The mode steering is heavily tuned to Claude's behavior.** `EDIT_MODE_GUIDE`
  (`:1898`) and the per-mode `length_line` blocks (`:2875-3010`) contain explicit
  regression archaeology — e.g. the summary block's comment at `:2876-2881` says the
  previous wording "was the root cause of the 60% keep-ratio regression". "HARD WORD CAP …
  will be REJECTED outright" is instruction-following pressure, not a hard constraint; a
  weaker model will overshoot and hit `_enforce_word_ceiling` / `_enforce_script_duration`,
  which **fail the job** (by owner rule they must never truncate or speed the voice).
  Net effect of a naive swap: more failed jobs, not worse videos.
- **Retry loops are tuned to Claude's failure modes.** `SCRIPT_GEN_RETRIES=1` on 504 only,
  `_RATIO_REGEN_ATTEMPTS=1`, `_RATIO_REGEN_MIN_IMPROVE=0.01` — all calibrated on measured
  Claude runs (see the 38-manifest pace measurement note at `:2092-2097`). Another
  provider's error taxonomy (429/quota/overloaded/content-filter) has **no mapping** today:
  everything non-504 fails fast, so a 429 from OpenRouter would hard-fail a job instead of
  backing off.
- **Pacing constants are calibrated to Claude output.** `_VI_WORDS_PER_SEC = 2.2` (`:2124`)
  and the whole budget chain were fitted to measured renders. A model with different verbosity
  changes the effective pace, which the memory notes flag as the #1 cause of "video too
  fast/slow".

**7.4 Hardest — the two vision calls.** See §4. They depend on
`--tools Read` + `--add-dir` **agentic file access**, which has no equivalent in a plain
chat-completions API. Porting is not a transport change, it is: read+base64 the JPEGs,
build multi-part messages, and **rewrite the prompts** to drop the Read-tool instruction.
Also, `generate_script_visual_explain` fails the job on a thin script (`:3497`), so a
weaker multimodal model turns a rare fallback into a common job failure.

**7.5 Cross-cutting hazards.**
- **Cache poisoning.** `_script_cache_key` (`:160`, parts assembled at `:3148`) omits
  model/provider/prompt-version. Adding a provider choice without extending the key will
  serve provider-A scenes to a provider-B job for 24 h. This exact class of bug is already
  in the project memory for TTS (`tts-cache-key-omits-engine-flags`).
- **Cancellation.** `POST /api/jobs/{id}/stop` works only because the LLM is a killable
  process tree (`kill_job_processes`, `:544`). An HTTP-based provider needs an explicit
  cancel-token/abort path or "Stop" quietly stops interrupting the LLM step.
- **Prompt caching.** Nothing in the repo relies on Anthropic prompt caching (no
  `cache_control`, no system-prompt reuse across calls) — each `claude -p` is a cold process
  with `--tools ""` specifically to shrink prefill (`:1232`). So there is **no** prompt-cache
  assumption to break. That is one risk we do NOT have.
- **Cost/billing model.** The whole design (`--tools ""`, `--system-prompt` replacement,
  chunking, disk cache, `BATCH_MAX_LINKS`, "no auto-translation to save subscription usage"
  at `main.py:1226`) exists to minimize **subscription** usage. Those trade-offs are wrong
  for a per-token or per-request-quota provider and should be re-derived, not carried over.
- **Windows encoding.** Every spawn uses `encoding="utf-8", errors="replace"` because the
  cp1252 default silently corrupts Vietnamese (memory: `subprocess-utf8-windows`). An HTTP
  provider must decode UTF-8 explicitly too, and any PowerShell-based smoke test must use a
  Python client (memory: `powershell-utf8-http-client`).
- **Tests pin CLI behavior.** `Dashboard/api/test/test_claude_timeout.py` asserts
  tree-kill/timeout/parse semantics against `_run_claude_script_once`;
  `test_script_gen_parallel.py`, `test_chunk_per_mode.py`, `test_footage_fixed_budget.py`,
  `test_footage_multibatch_prompt.py`, `test_keep_ratio_loop.py`, `test_word_ceiling_bug3.py`
  all monkeypatch `generate._run_claude_script`, and
  `test/fixtures/test_script_reuse_bypass.py:136` +
  `test/fixtures/test_dubbed_credit_needs_input.py:198` assert on
  `argv[0] == generate.CLAUDE_BIN`. **Good news:** the monkeypatch seam is exactly
  `_run_claude_script`, so a provider layer inserted at or below that function keeps most
  tests valid; the two fixtures asserting `CLAUDE_BIN` argv will need updating.

**7.6 Two dead call sites (report honestly, do not assume they are live).**
`_translate_title_to_vi` (`generate.py:1567`) and `_translate_full_to_vi`
(`generate.py:2497`) have **no callers anywhere in the repo** (verified by repo-wide grep;
`translate_full` now rides the footage prompt path per `runner.py:1848-1868`, and batch
preview deliberately skips title translation per `main.py:1226`). They are complete,
working helpers that would need porting **if** they are ever re-enabled. Decision needed
from the owner: port them, or delete them before the provider work starts. Recommendation:
delete `_translate_title_to_vi` (its feature was explicitly removed to save usage) and keep
`_translate_full_to_vi` only if the owner still wants a literal-translation mode.

---

## 8. Suggested seam (for the design phase, not implemented)

One provider interface with three call shapes, inserted between `_run_claude_script`
(keeps retry/cache/spell-fix/logging) and the process spawn:

```
invoke_text(prompt, *, timeout, model, provider)            -> str   # replaces :1247 spawn
invoke_vision(prompt, image_paths, *, timeout, model, ...)   -> str   # replaces :1367 and :4455
```

with `expect="array"|"object"` handled by the existing `_extract_json_array` (`:1132`) /
`_parse_vision_cover_json` (`:4357`). Required companions: provider-aware cache key
(§7.5), a provider-aware error taxonomy mapping quota/rate-limit → retry-with-backoff
rather than hard-fail, a cancellation hook mirroring `kill_job_processes`, and a
`jobs.llm_provider` / `jobs.llm_model` pair added via the additive migration convention in
§5.2.
