# n8n — orchestration

n8n runs in Docker (`http://localhost:5678`) and **orchestrates over HTTP/SQL only**.
The container cannot run host CLIs (Claude Code, VieNeu-TTS, ffmpeg, ComfyUI), so the
heavy steps live on the host and n8n reaches them via **`host.docker.internal`**:

| Host service | URL from inside n8n |
|---|---|
| Dashboard API (FastAPI) | `http://host.docker.internal:4000` |
| ComfyUI | `http://host.docker.internal:8188` |
| PostgreSQL | `host.docker.internal:5432` |

Connectivity to API + PostgreSQL is verified working.

## DB access: through the host API, not an n8n DB credential

Chosen approach: **n8n never talks to PostgreSQL directly.** The host FastAPI
(`Dashboard/api`, port 4000) owns all database access; n8n only makes HTTP calls to it.
This keeps one place responsible for SQL, and avoids n8n's finicky public-API credential
schema. So n8n needs **no Postgres credential** for the normal flow.

Active starter workflow (created via the n8n API): **"GameStory — orchestration starter
(HTTP to host API)"** = Manual Trigger → HTTP GET `http://host.docker.internal:4000/api/pages`.
Open it in n8n and click **Test workflow** — it returns the live GameStory row.

> `gamestory-starter.workflow.json` (the Postgres-node version) is kept only as a reference
> for talking to the DB directly; the HTTP version above is the one to use.

## Planned full workflow (next)

```
Telegram Trigger  (pending — needs bot token)
  → HTTP POST host:4000/generate/script   (Claude Code headless, returns scene JSON)
  → HTTP POST host:4000/generate/tts       (VieNeu-TTS → audio)
  → HTTP POST host:4000/generate/timestamps(faster-whisper → per-line timing)
  → HTTP POST host:8188 (ComfyUI)          (SDXL image per scene)
  → HTTP POST host:4000/assemble           (ffmpeg: Ken Burns + captions + bgm)
  → HTTP POST host:4000/publish/youtube    (pending — needs OAuth)
  → Postgres                               (write jobs / videos / posts rows)
```

The `host:4000/generate/*` and `/assemble` endpoints are the host generation service
to be built next (the only hand-written code in the pipeline).
