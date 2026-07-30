# ContentFactory

Automated short-video production and publishing system (YouTube Shorts / Reels / TikTok / Facebook Reels), running **100% locally and free** on a personal machine.

## Repository layout

```
ContentFactory/
├── Dashboard/              Central manager for all pages/channels
│   ├── api/                FastAPI backend (Python)
│   │   ├── main.py         API entry point & routing
│   │   ├── runner.py       Job pipeline orchestrator
│   │   ├── generate.py     Claude-headless script generation
│   │   ├── media_spec.py   Per-mode media specs
│   │   ├── workers/        Pipeline step workers
│   │   │   ├── ingest_worker.py
│   │   │   ├── download_worker.py
│   │   │   ├── probe_worker.py
│   │   │   ├── tts_worker.py        VieNeu / F5-TTS
│   │   │   ├── whisper_worker.py    faster-whisper timestamps
│   │   │   ├── stickman_procedural.py
│   │   │   └── prewarm_worker.py
│   │   ├── blender/        Blender headless stickman renderer
│   │   ├── assets/fonts/   Caption fonts (BeVietnamPro)
│   │   └── test/           pytest suite
│   ├── web/                React + TypeScript + Tailwind dashboard
│   │   └── src/
│   │       ├── views/      Pages · Overview · Jobs · Videos · Publishing · PageDetail · CreateVideo
│   │       ├── components/ OrgChart · Charts
│   │       ├── api.ts      HTTP client
│   │       ├── data.tsx    Data hooks
│   │       ├── types.ts    Shared types
│   │       └── ui.tsx      Primitive UI components
│   ├── db/                 PostgreSQL schema & migrations
│   ├── config/             Page config examples
│   ├── n8n/                n8n workflow stubs (legacy / reference)
│   ├── secrets/            OAuth token files — gitignored
│   └── test/               Cross-boundary integration fixtures
├── tools/
│   └── voice_doctor.py     TTS/audio diagnostics tool
├── .claude/                Claude Code agent & skill definitions
├── CLAUDE.md               Primary project instructions for Claude Code
├── how to edit video.md    Editing-mode playbook (commentary/recap/educational/summary/dubbed)
├── project_define.md       Owner's original project definition
└── video-production-lessons.md  Lessons learned from real production runs
```

## Hard constraints

- **Local & free only.** Self-hosted tools: VieNeu-TTS, faster-whisper, ComfyUI+SDXL, FFmpeg. No paid APIs.
- **LLM = Claude Code headless.** Script generation runs via `claude -p "..." --output-format json` — billed to the subscription, not the API.
- **Hardware: RTX 2070 Max-Q, 8 GB VRAM.** Models run sequentially. SDXL fits; full Flux does not.
- **Borrowed account.** The logged-in Claude account is not the owner's. Creator/author/credit fields must always be confirmed with the owner — never inferred from the account.

## Production pipeline

**Image mode** (AI-generated visuals):
```
Studio UI → job created → [yt-dlp + faster-whisper] → Claude Code script
          → VieNeu-TTS → faster-whisper (timestamps) → ComfyUI+SDXL
          → FFmpeg (Ken Burns + captions + bgm) → publish → PostgreSQL
```

**Footage mode** (translate / reup):
```
Studio UI → job created → yt-dlp → faster-whisper → Claude Code script (edit_mode applied)
          → VieNeu-TTS → FFmpeg (cut source + captions + bgm) → publish → PostgreSQL
```

**Stickman mode:** swap ComfyUI+SDXL for procedural 2D or Blender headless renderer.

## Production modes (chosen per job at creation time)

| Axis | Options |
|---|---|
| `render_mode` | `image` · `footage` · `stickman` · `clone` |
| `edit_mode` | `commentary` · `recap` · `educational` · `summary` · `dubbed` |

See [how to edit video.md](how%20to%20edit%20video.md) for the full transformation playbook and copyright safety formula.

## Tech stack

| Stage | Tool |
|---|---|
| Backend API | FastAPI (Python) |
| Dashboard UI | React + TypeScript + Tailwind |
| Database | PostgreSQL (local → cloud later) |
| Script generation | Claude Code headless (`claude -p`) |
| Voiceover (VI) | VieNeu-TTS |
| Timestamps | faster-whisper |
| Image generation | ComfyUI + SDXL |
| Video assembly | FFmpeg (Python microservice) |
| Stickman render | Procedural 2D / Blender headless |
| Web research | yt-dlp + WebSearch/WebFetch |
| Publishing | YouTube Data API v3 → IG / TikTok / FB |

## Getting started

```bash
# 1. Initialize the database
createdb contentfactory
psql -d contentfactory -f Dashboard/db/schema.sql

# 2. Start the API
cd Dashboard/api
.\run-api.ps1

# 3. Start the dashboard (dev)
cd Dashboard/web
npm install
npm run dev
```

Binary paths (this machine): FFmpeg → `E:\Installed\FFmpeg\`, Blender → `E:\Installed\Blender\blender.exe`, psql → `E:\Installed\PostgreSQL16\bin`.

## Principles

- **All `.md` files are written in English.** Code comments and LLM prompt instructions are English (token efficiency). Vietnamese is reserved for user-facing dashboard text and video narration output.
- **Finished video output lives outside the repo** at `E:\ContentFactory\<page-name>`. Each page's `config/page.json` carries the concrete `output.video_dir`.
- **Secrets stay path-only.** `platform_accounts.credentials_ref` stores only a path to a token file under `Dashboard/secrets/<page>/<platform>.json` — never the token itself.
- **Documentation:** see `CLAUDE.md` for full architecture, constraints, and agent/skill configuration.
