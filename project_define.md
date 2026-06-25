# ContentFactory — Architecture Definition

> Master architecture document. Records the overall system design and the decisions behind it.
> See [CLAUDE.md](CLAUDE.md) for working rules, [Dashboard/db/schema.sql](Dashboard/db/schema.sql) for the data model.

## 1. Goal

Automatically produce and publish short-form videos (YouTube Shorts / Reels / TikTok / Facebook Reels) from a single chat command. The owner sends a prompt or a reference link via chat; the system analyzes it, writes a script, generates the video, publishes it, and records everything in a central dashboard. The whole pipeline runs **locally and free** on the owner's machine.

## 2. Hard constraints

| Constraint | Decision |
|---|---|
| Cost | 100% local & free — no paid APIs unless the owner explicitly approves |
| LLM | Claude Code headless (`claude -p ... --output-format json`), not the Anthropic API |
| Account | Claude runs on a borrowed account; all creator/credit/author/billing info belongs to the **owner** (`TODO_ASK_USER`) |
| Hardware | RTX 2070 Max-Q, 8GB VRAM → SDXL-class models, run sequentially |
| Database | Local PostgreSQL now; cloud later (same schema) |
| Video language | Vietnamese |
| Docs | All `.md` files in English |

## 3. System layers

```
[1] CHAT INPUT      Telegram bot (later: Messenger) receives a prompt or reference link
        │
[2] ORCHESTRATOR    n8n (self-hosted, Docker) drives the flow + a job queue
        │
[3] BRAIN           link → yt-dlp + faster-whisper → reference content
                    Claude Code → script JSON [{scene, narration, image_prompt}]
        │
[4] GENERATION      VieNeu-TTS → audio; faster-whisper → timestamps;
                    ComfyUI+SDXL → one image per scene
        │
[5] ASSEMBLY        FFmpeg (Python microservice): Ken Burns + audio + synced captions + free bgm
        │
[6] PUBLISH         YouTube Data API v3 (MVP) → IG / TikTok / Facebook Reels later
        │
[7] DASHBOARD       PostgreSQL: metadata, cost, post IDs, view metrics
```

### Non-obvious design points

- **TTS has no timestamps.** VieNeu-TTS does not emit timing. Run faster-whisper on the generated audio to get per-line timestamps; those timestamps drive each scene's duration and caption sync.
- **Voiceover is the backbone.** Images and captions follow the narration timeline, not the reverse.
- **FFmpeg assembly is the only hand-written code.** It exceeds plain n8n, so it lives as a small Python microservice called over HTTP. Everything else is n8n nodes + external local services.
- **Sequential VRAM use.** SDXL, Whisper, and TTS never load at the same time on 8GB; ComfyUI unloads models between steps.

## 4. Multi-page model

The Dashboard manages **many pages/channels in parallel**. A page is an identity
container only — it carries no fixed production architecture:

- A page = one row in `pages` (name, language, platform accounts). No per-page
  render/architecture config.
- Production options are chosen **per-job at video creation time** in the Studio UI,
  not locked to the page: each job carries a `render_mode` (`image` | `footage` |
  `stickman` | `clone`) and, for footage jobs, an `edit_mode`
  (summary | commentary | recap | educational | dubbed).
- Everything downstream is keyed by `page_id`: `jobs` → `videos` → `assets`, and `posts` → `metrics`.
- **Adding a page = inserting a row, never a schema change.**

## 5. Account isolation & secrets

- A YouTube channel **termination** can cascade to all channels under the same Google account. Therefore **each page uses its own account per platform** — stored in `platform_accounts` with `UNIQUE (page_id, platform)`.
- Personal Gmail is sufficient for YouTube (no Workspace/Business). Instagram requires a Business/Creator account; Meta Business Verification applies only to Facebook/Instagram, not Google.
- **Secrets are never in the DB or config.** `credentials_ref` holds only a path to a token file under `Dashboard/secrets/<page>/<platform>.json` (gitignored).

## 6. Data model (PostgreSQL)

| Table | Purpose |
|---|---|
| `pages` | One row per channel/page; identity only (name, language, accounts) |
| `platform_accounts` | Per-page, per-platform publishing identity (isolation) |
| `jobs` | One production request (prompt or link) + cost/status |
| `videos` | Rendered output of a job + script JSON |
| `assets` | Per-scene images / audio / music |
| `posts` | One row per platform upload of a video |
| `metrics` | Periodic view/like/comment snapshots |

Full DDL: [Dashboard/db/schema.sql](Dashboard/db/schema.sql).

## 7. Locked toolchain

| Stage | Tool | Notes |
|---|---|---|
| Chat input | Telegram (BotFather) | Free, no app review |
| Orchestrator | n8n (Docker, self-host) | Built-in queue + nodes |
| Reference extraction | yt-dlp + faster-whisper | For link inputs |
| Script | Claude Code headless | Vietnamese script JSON |
| Voiceover | VieNeu-TTS (Apache 2.0) | Vietnamese, voice cloning from 3–5s |
| Timestamps | faster-whisper (medium) | Drives scene/caption sync |
| Images | ComfyUI + SDXL | Fits 8GB VRAM, ~15–30s/image |
| Assembly | FFmpeg (Python microservice) | Ken Burns + captions + bgm |
| Music | Free libraries (YouTube Audio Library / Pixabay) | Avoid copyright |
| Publishing | YouTube Data API v3 | MVP target |
| Storage | Local PostgreSQL | Dashboard |

## 8. Publishing platforms — cost & approval

All platform APIs are **free of monetary fees**; the cost is time and approval requirements.

| Platform | Fee | Requirement |
|---|---|---|
| YouTube Shorts | $0 | OAuth verification for public uploads; ~6 uploads/day quota |
| TikTok | $0 | Separate audit (1–4 weeks); SELF_ONLY until approved |
| Instagram Reels | $0 | IG Business account + App Review; 25 posts/24h |
| Facebook Reels | $0 | Facebook Page + App Review; may need Business Verification |

Instagram and Facebook share Meta's Graph API and App Review — one submission covers both.

## 9. Cost model (per ~60s video, all-local plan)

Variable cost ≈ **$0** (all generation is local). Only electricity + one-time hardware. Production time ≈ **5–10 min/video** on the target machine — fine for batch.

## 10. Roadmap

| Phase | Scope |
|---|---|
| **MVP** | Telegram → n8n → Claude Code script → VieNeu-TTS → faster-whisper → ComfyUI+SDXL → FFmpeg → upload **YouTube only** → PostgreSQL. Single page: GameStory. |
| **V2** | Reference-link analysis (yt-dlp + whisper). Dashboard reporting (Metabase or similar). |
| **V3** | Add Instagram Reels + TikTok + Facebook Reels (after approvals). Pull view metrics back into `metrics`. Additional per-job render modes (`clone`, `stickman`) and footage edit modes. |

### MVP build order

1. Install ComfyUI + SDXL; render one 1080×1920 test image.
2. Install VieNeu-TTS; generate one Vietnamese voiceover sample.
3. Write the FFmpeg Python microservice (images + audio + synced captions).
4. Install PostgreSQL; load `Dashboard/db/schema.sql`.
5. Stand up n8n + Telegram bot; wire end-to-end for one video → YouTube upload.

## 11. Open items (need owner input)

- GameStory creator info: channel name, dedicated Google account, displayed credit.
- Preferred game topics and script tone.
- A 3–5s voice sample if a custom cloned voice is wanted.
