---
name: video-pipeline
description: >-
  How to work on the ContentFactory FastAPI backend & production pipeline:
  runner.py job loop, generate.py (Claude-headless script gen + TTS/image
  endpoints), cf-venv workers (ingest/download/probe/tts/whisper), main.py
  routes, and the PostgreSQL schema/migrations. Use when changing job
  processing, generation endpoints, worker invocation, API contracts, or the DB
  schema for ContentFactory. Triggers on "pipeline", "runner", "generate.py",
  "worker", "job", "endpoint", "schema/migration".
---

# Video pipeline (backend)

Used mainly by **backend-engineer** (and media-engineer for worker contracts).

## Architecture facts
- Production options are chosen **per-job at creation time**: `render_mode` (footage | image | stickman | clone) + `edit_mode` (commentary | recap | educational | summary | dubbed). Pages are identity containers only — no fixed architecture type.
- Pipeline flows by `render_mode`: `footage` = ingest → script(transform, edit_mode applied) → TTS → assemble → upload; `image` = script(topic) → TTS → SDXL images → assemble → upload; `stickman` = script → TTS → Blender render → assemble → upload.
- `runner.py` polls jobs (`_claim_job`), runs `_process_job` step-by-step, writes progress (`_set_progress`) and rows (videos/assets/posts). Reads `job["render_mode"]` (not `page["architecture_type"]`) to branch.
- `generate.py`: `_run_claude_script` calls `claude -p "<prompt>" --output-format json` (subscription, NOT the API). `_run_cf_worker(script, payload, timeout)` runs a cf-venv worker via subprocess (heavy ML deps isolated from the API venv).
- Workers in `Dashboard/api/workers/`: yt-dlp (ingest/download), faster-whisper, tts. They read JSON on stdin, print JSON result.

## How to make a change safely
- **New job parameter** (e.g. `render_model`): thread it end-to-end — `seed.sql` `ADD COLUMN IF NOT EXISTS` (+ apply to live DB) → `NewJob` Pydantic field → INSERT column+value → `fetch_jobs` SELECT + camelCase return → notify frontend-engineer to add it to `api.ts`/`types.ts`. Then qa verifies the boundary.
- **Migration with id/PK change**: GENERATED ALWAYS IDENTITY + FKs without ON UPDATE CASCADE → use copy-row(`OVERRIDING SYSTEM VALUE`) → repoint children → delete old → `setval`, all in one transaction.
- **New worker / model invocation**: get the exact CLI/env/payload from media-engineer; keep heavy deps in cf-venv, not the api venv.
- Restart the API after backend edits (uvicorn `--reload` may not catch all changes — restart to be sure) and verify with `curl`.

## Constraints
Local & free (no paid APIs, `claude -p` headless). Secrets = path refs only. Output dirs outside the repo. One 8GB GPU, models sequential. Adding a page = inserting a `pages` row, not changing the schema.

## Verify
- `curl` the changed route; round-trip a test job through create→`fetch_jobs`; clean up test rows after.
- Report real results; never assume. Management notes → `_workspace/`.
