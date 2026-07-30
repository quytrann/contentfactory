# `tools/llm_bench.py` — measure free-tier LLMs on Vietnamese script-gen

## What this is for

ContentFactory writes its Vietnamese narration with `claude -p` (Claude Code headless,
billed to a subscription). If that subscription ends we need a **$0** replacement. No
public benchmark answers *"which free model writes the best Vietnamese long-form
narration"* — VMLU only measures multiple-choice knowledge, and SEA-HELM's Vietnamese
generation scores could not be extracted (see `_workspace/13_research_openrouter_free_tier.md`
§3). So this harness runs **one fixed script-gen task across every candidate provider**
and puts the objective numbers side by side.

**It does not judge fluency. It cannot.** It measures the things a machine *can* measure
(valid JSON, correct shape, scene-count adherence, word budget, pace, latency, HTTP
errors) and then writes the raw Vietnamese to disk so **you** read it and rank it. That
final judgement is the whole point — the numbers only tell you which legs are worth
reading.

## Ground rules baked into the tool

- **No paid path, ever.** Every leg is a $0 tier. The tool has no notion of a balance or a
  top-up, and it will never fall back to a paid endpoint. If a leg has no key it is
  **skipped and reported as skipped** — it never invents a result.
- **Keys are read from the environment only.** The tool never creates an account, never
  creates a key, and never writes a key anywhere (not to artifacts, not to logs).
  The keys must be from **your own** accounts.
- **It does not touch the pipeline.** It deliberately does not import
  `Dashboard/api/generate.py`. The prompts and expected scene shape are *copies* under
  `tools/_dummy_data/llm_bench/`, captured 2026-07-30.
- **Prompt-secret guard.** Free tiers may train on and even publish what you send. The
  tool hard-refuses to send a prompt containing anything credential-shaped (`sk-…`,
  `AIza…`, `ghp_…`, `ya29.…`, `EAA…`, `refresh_token`, a path into `Dashboard/secrets/`).
  Verified working — do not remove that check.

---

## 1. Environment variables

### Keys (one per provider; a missing key just skips that leg)

| Provider | Env var (first one set wins) | Where to get a $0 key |
|---|---|---|
| `gemini` | `GEMINI_API_KEY` or `GOOGLE_AI_STUDIO_API_KEY` | <https://aistudio.google.com/apikey> — sign in with a Google account, "Create API key". |
| `openrouter` | `OPENROUTER_API_KEY` | <https://openrouter.ai/settings/keys> — sign up, create a key. Do **not** buy credits (see limits below). You may also need to enable the free-endpoint data toggles at <https://openrouter.ai/settings/privacy>. |
| `zai` | `ZAI_API_KEY` or `Z_AI_API_KEY` | <https://z.ai/model-api> — the GLM-*-Flash models are listed **Free** on <https://docs.z.ai/guides/overview/pricing>. |
| `ollama` | *(none — local)* | <https://ollama.com/download>, then e.g. `ollama pull qwen3:8b`. Skipped silently if the daemon is not running. |

> **These must be keys on your own accounts (quy.qtp@gmail.com or whichever you choose).**
> The Claude account this repo's tooling is logged into is borrowed — nothing here may use
> it, and no LLM key should be created under it.

Set them for one shell session:

```powershell
$env:GEMINI_API_KEY     = "..."
$env:OPENROUTER_API_KEY = "..."
$env:ZAI_API_KEY        = "..."
```

or persist them for your user (survives reboots; then open a NEW shell):

```powershell
[Environment]::SetEnvironmentVariable('GEMINI_API_KEY', '...', 'User')
```

Do **not** put them in `Dashboard/api/.env` — that file is read by the API server and is
not where LLM bench keys belong.

### Optional tuning

| Env var | Default | What it does |
|---|---|---|
| `LLM_BENCH_GEMINI_MODEL` | `gemini-flash-latest` | Which Gemini model to bench. **Measured 2026-07-30 on a fresh $0 key:** `gemini-2.5-flash` / `gemini-2.5-flash-lite` → 404 *"no longer available to new users"*, `gemini-2.0-flash` → 429 (no free quota), **even though `ListModels` still advertises them**. Verified callable: `gemini-flash-latest`, `gemini-3.6-flash`, `gemini-3.5-flash`, `gemini-3.1-flash-lite`. Pin a concrete id for a reproducible A/B. |
| `LLM_BENCH_OPENROUTER_MODEL` | *(discovered live)* | Pin an OpenRouter model instead of auto-picking the best free one. |
| `LLM_BENCH_ZAI_MODEL` | `glm-4.5-flash` | Which GLM Flash model to bench. |
| `LLM_BENCH_OLLAMA_MODEL` | *(first installed)* | Which local model to bench. |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Where the local daemon lives. |
| `LLM_BENCH_TEMPERATURE` | `0.7` | Sampling temperature, same for every leg. |
| `LLM_BENCH_TIMEOUT` | `300` | Per-request seconds. A congested free endpoint on the 10-minute-transcript task is genuinely slow. |
| `LLM_BENCH_WORDS_PER_SEC` | `2.2` | Pace used for the "EST s" column. Frozen copy of `generate.py::_VI_WORDS_PER_SEC`. |
| `LLM_BENCH_<PROVIDER>_BASE_URL` | — | Redirect a leg's OpenAI-compatible base URL (local proxy / stub). Must stay a $0 endpoint. |
| `CONTENT_OUTPUT_ROOT` | `E:\ContentFactory` | Artifact root: `<root>\_bench\llm\<run>`. Read from `Dashboard/api/.env`. |

---

## 2. How to run it

It needs `httpx`, so use the **API venv** python (not cf-venv):

```powershell
$PY = "D:\workspace\ContentFactory\Dashboard\api\.venv\Scripts\python.exe"
```

### Step 1 — see what OpenRouter actually offers today (no key needed)

```powershell
& $PY tools\llm_bench.py --list-models
```

`GET /api/v1/models` and `/models/<id>/endpoints` are both unauthenticated, so this costs
nothing and consumes none of your daily quota. **Free model IDs are never hardcoded** —
they churn weekly, and the catalogue today shares almost nothing with 2025-era lists.

### Step 2 — verify the scorer without keys

```powershell
& $PY tools\llm_bench.py --dry-run
```

Scores six saved fixture responses (clean pass, fenced+wrapped output, truncated JSON,
English narration, missing keys, over-budget) and checks each produces its expected
verdict. Run this after any edit to the tool.

### Step 3 — the real bench

```powershell
# everything with a key set, both tasks, one sample each
& $PY tools\llm_bench.py

# the decisive task only, 3 samples, two legs
& $PY tools\llm_bench.py --tasks footage_recap_600s --repeat 3 --providers gemini,openrouter

# pin models explicitly
& $PY tools\llm_bench.py --models "gemini=gemini-2.5-flash,openrouter=openai/gpt-oss-20b:free"

# measure prompt-only JSON instead of strict structured output
& $PY tools\llm_bench.py --no-structured

# bench a REAL prompt captured from a past job
& $PY tools\llm_bench.py --prompt-file C:\tmp\job_281_prompt.txt --shape footage `
      --expect-scenes 40 --expect-words 1161 --expect-duration 600
```

**Recommended first real run** (cheap, 8 requests at most, leaves plenty of daily quota):

```powershell
& $PY tools\llm_bench.py --repeat 1
```

Then, once you know which legs work, spend the quota where it matters:

```powershell
& $PY tools\llm_bench.py --tasks footage_recap_600s --repeat 3
```

### The two tasks

| Task | What it stresses |
|---|---|
| `topic_gamestory_60s` | Cheap smoke test: topic in, 9 scenes out, ~117 Vietnamese words. |
| `footage_recap_600s` | **The decisive one.** A 10-minute source transcript (~9k chars) into a RECAP script: long-context ingest, a *hard* word ceiling, ~40 scenes, and per-scene source ranges that must stay inside 0–600 s. |

---

## 3. How to read the output

```
  PROVIDER     MODEL                     HTTP  LAT s JSON SHAPE  SCN    WORDS  EST s  w/s VN% STRUCT      VERDICT
  gemini       gemini-2.5-flash           200    6.4 ok   ok     9/9  120 +3%     54  2.0  94 json_schema OK
  openrouter   openai/gpt-oss-20b:free    200   18.9 ok   ok     7/9   63 -46%    29  1.1  92 json_schema WARN
      ! scene count 7 vs requested 9 (tolerance ±0)
      ! word count 63 vs budget 117 (-46%, tolerance ±10%)
```

| Column | Meaning |
|---|---|
| `HTTP` | Status. `429` = rate limited. `401` = bad/missing key. `200` with a `!` line = it answered but off-spec. |
| `LAT s` | Wall-clock seconds for that one request. |
| `JSON` | Did the body parse into a scene array at all (after tolerating markdown fences and a `{"scenes": …}` wrapper). |
| `SHAPE` | Does **every** scene carry the keys the pipeline needs (`narration`, `sourceStart`/`sourceEnd` or `image_prompt`). `BAD` = the pipeline would crash on it. |
| `SCN` | Scenes returned / scenes requested. |
| `WORDS` | Total Vietnamese narration words, and % off the prompt's word budget. |
| `EST s` | Seconds that word count would take to speak at 2.2 words/sec. Compare against the task's target duration. |
| `w/s` | Words per second of *target* duration. Far below 2.2 = the model under-filled the video. Far above = the finished video would overrun the source. |
| `VN%` | **Heuristic** share of narration tokens that look Vietnamese. Below ~55% is treated as "it answered in English" and hard-fails. **75–95% is normal** for good output that correctly keeps English proper nouns — do not read 100% as better. |
| `STRUCT` | `json_schema` = strict structured output was accepted. `fallback:none` = the provider rejected `response_format`, so the request was re-sent prompt-only (same $0 endpoint). `none` = not requested. |
| `VERDICT` | `OK` · `WARN` (usable but off-spec) · `FAIL` (unusable as-is) · `ERROR` (HTTP/network) · `SKIP` (no key / not reachable). |

`FAIL` is reserved for things production would genuinely reject: malformed shape, output
over a **hard** word ceiling, or narration that is not Vietnamese. Being under budget is a
`WARN` — it means the model skipped source content, which is bad but salvageable.

### Then do the part the tool can't

```
E:\ContentFactory\_bench\llm\<run>\
  <task>__<provider>__r<n>.narration.txt   <- READ THIS. Narration lines only.
  <task>__<provider>__r<n>.raw.txt         exactly what the model returned
  <task>__<provider>__r<n>.scenes.json     parsed scene array
  <task>__<provider>__r<n>.meta.json       status / latency / usage / metrics
  summary.txt / summary.json               the table + every metric, machine-readable
```

Open the `.narration.txt` files side by side and rank them on **fluency, register, and
faithfulness to the transcript**. Watch specifically for the failure the research flagged:
*fluent Vietnamese that has quietly drifted from the source*. It reads as correct and is
the hardest thing to catch — and no metric in the table can see it.

---

## 4. Rate limits — read this before you spend quota

The tool is sequential and paced to `--rpm` (default 20). On repeated 429s it **aborts the
whole run** rather than retrying, and tells you why.

| Provider | Limits | Confidence |
|---|---|---|
| **OpenRouter** | **20 requests/minute and only 50 requests/DAY**, reset on the UTC day. | **Official docs, verified.** Applies to any model ID ending `:free`. Extra accounts/keys do **not** help — capacity is governed globally. |
| **Gemini (AI Studio)** | **Unknown — Google no longer publishes them.** The rate-limits page now just points at <https://aistudio.google.com/rate-limit>. | Third-party figures circulating for mid-2026 (≈10 RPM / 1,500 RPD for 2.5 Flash) are **unconfirmed**. You must observe them empirically: run the bench and watch for 429s, and read the real numbers off AI Studio with your own key. |
| **Z.ai GLM Flash** | Not stated on the pricing page. Third-party estimate ≈1 req/sec, ≈1,000 RPD. | **Unverified.** This bench is how it gets verified. |
| **Ollama** | Unlimited (electricity only). | Shares the 8 GB GPU with SDXL/TTS, so expect a model-load penalty per call and a small usable context. |

### The 50-requests-per-day trap

Each leg × task × `--repeat` is one request. The full default run (2 tasks × 4 providers ×
1 repeat) is 8 requests, of which 2 hit OpenRouter. But `--repeat 5` across both tasks is
**10 OpenRouter requests**, and a couple of exploratory runs will eat a meaningful slice of
50. Budget it: use `--providers` and `--tasks` to spend requests only where you need them.

If you see the abort message: **wait for the UTC-day reset.** Do not buy credits — this
project is $0-only, and the $10 top-up that would raise the cap to 1,000 RPD is a paid
action that is explicitly out of scope.

### Privacy

Both Gemini's and OpenRouter's free tiers reserve the right to train on what you send, and
OpenRouter free endpoints may require enabling a "may publish prompts" toggle. You have
accepted this. It is fine here because everything sent is a public-source transcript plus
our prompt engineering — and the tool refuses to send anything credential-shaped. Note
that OpenRouter's `data_collection: "deny"` and "free" are effectively mutually exclusive:
you cannot have both.

---

## 5. Known limitations (stated plainly)

- **Fluency is not measured.** By design. Read the artifacts.
- **`VN%` is a heuristic**, not a language detector. It reliably catches "answered in
  English"; it does not grade Vietnamese quality.
- **Word counts are whitespace tokens**, the same rule `generate.py`'s word budget uses —
  so the two are comparable, but note that Vietnamese writes syllables separately, so a
  "word" here is closer to a syllable.
- **The scene-count and word-budget numbers are frozen copies** from `generate.py` as of
  2026-07-30. If those prompts change materially, re-capture the fixtures
  (`tools/_dummy_data/llm_bench/README.md` documents how the numbers were derived).
- **`--repeat` samples the same prompt** and does not vary temperature or seed. Free
  models vary run to run, so treat a single sample as indicative only; 3 is a reasonable
  sample without eating the daily cap.
- **Default model IDs for gemini/zai are documented defaults, not discovered.** Only
  OpenRouter's catalogue is discoverable via an API. If a default 404s, the leg reports
  the error — override it with `--models`.

## 6. Exit codes

| Code | Meaning |
|---|---|
| `0` | Ran, nothing failed. |
| `1` | Ran, but at least one leg was `FAIL`/`ERROR` (or a dry-run fixture mismatched). |
| `2` | OpenRouter model discovery failed. |
| `3` | `httpx` missing — you used the wrong python. |
| `4` | Aborted on repeated 429s. |
