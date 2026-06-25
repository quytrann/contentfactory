# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

ContentFactory is an automated short-video production system (YouTube Shorts / Reels / TikTok / Facebook Reels) designed to run **100% locally and free** on the owner's machine. It is in **early scaffolding stage** — current contents are docs, the PostgreSQL schema, and per-page config. There is no application code, build, or test suite yet; do not invent commands that don't exist.

## Hard constraints (read before designing anything)

These are project rules that override default assumptions:

- **Local & free only.** Prefer self-hosted/local tools (n8n self-host, VieNeu-TTS, faster-whisper, ComfyUI+SDXL, FFmpeg). Do **not** introduce paid APIs (no Anthropic API, no fal.ai, no cloud rendering) unless the owner explicitly approves a cost.
- **LLM = Claude Code headless, not the API.** Script/text generation runs via `claude -p "..." --output-format json` invoked from a command node — billed to the subscription, not per-token API.
- **Borrowed account.** The logged-in Claude account is borrowed. Any creator / author / credit / channel / billing field belongs to the **project owner**, never the account holder. Leave such fields as `TODO_ASK_USER` and ask the owner — never infer them from the logged-in account.
- **Target hardware: RTX 2070 Max-Q, 8GB VRAM.** Choose models that fit 8GB (SDXL, not full Flux). Models run **sequentially**, not concurrently. Disk/shared memory does not expand usable VRAM.
- **Video content language is Vietnamese.**

## Conventions

- **All `.md` files MUST be written in English** — this README, every page/sub-channel README, and any future docs. Applies to all sub-channels (`GameStory/` and any page added later). If a Markdown file is found in another language, translate it to English. (Chat with the owner stays in the owner's language; only the repo's Markdown is English.)
- **Code language = English; Vietnamese ONLY for user-facing output (token discipline).** Write all **code comments, docstrings, and any model-facing prompt strings** (text sent to `claude -p` / an LLM) in **English** — Vietnamese tokenizes ~2–3× heavier (multi-byte diacritics), and script-gen prompts are billed to the Claude subscription. When adding a new LLM prompt, write the *instructions* in English but **explicitly mandate Vietnamese narration output** where the content language applies. Keep **Vietnamese ONLY** for genuinely user-facing text: dashboard UI (labels, hints, model/edit-mode descriptions, buttons, `SectionTitle` subs, status badges), progress messages written to the DB and shown in the dashboard (e.g. "Dựng video", "Lồng tiếng", "Viết kịch bản"), API error details surfaced to the user, the video **narration output**, and the Vietnamese voice-sample sentence. Do **not** "translate to save tokens" anything in that user-facing list — that would break the owner's Vietnamese dashboard. (Net: instructions/comments → English; what a human end-user reads or hears → Vietnamese.)
- **Finished-video output lives OUTSIDE the repo** (media is large/regenerable). Convention: `E:\ContentFactory\<page name>` (e.g. GameStory → `E:\ContentFactory\GameStory`). Each page's `config/page.json` carries the concrete `output.video_dir`; the assembly step and `videos.video_path` should write there, not into the repo.
- **Local tool binaries live under `E:\Installed\<Tool>`, never `C:\Program Files`** (machine convention — keeps the system drive small). When installing a tool that defaults to `C:` (e.g. winget/MSI), **move it to `E:\Installed\<Tool>` and reference that path.** Key binaries: Blender → `E:\Installed\Blender\blender.exe` (headless render: `blender -b -P <script.py>`; Blender is **Z-up**), FFmpeg/FFprobe → `E:\Installed\FFmpeg\...` (see `Dashboard/api/.env`), Python venv → `Dashboard/api/.venv`, psql → `E:\Installed\PostgreSQL16\bin`.
- **Internet reading (Agent Reach).** To pull facts/topics/transcripts from real platforms (YouTube, RSS, any webpage, V2EX, Bilibili) use **Agent Reach** via the per-project wrapper `.\areach.ps1` at repo root (loads `.env` identity; `.env` is gitignored). The CLI itself only does `doctor`/`transcribe`/`setup` — actual reading = calling upstream tools directly (e.g. `yt-dlp --dump-json "URL"`, `yt-dlp --write-auto-sub …`), as documented in the `web-research` skill. Primary use = "watch" a YouTube video to mine topics / get its transcript to feed script-gen. No-subtitle fallback uses the project's **local faster-whisper**, not `agent-reach transcribe` (needs a paid key — left unset to stay local/free). **Never** run `agent-reach configure` (writes global creds, breaks per-project identity). Owned by the `researcher` agent; any agent or an on-demand request can call it.

## Architecture

Two top-level units:

- **`Dashboard/`** — central manager for **multiple pages/channels in parallel**. A page is one row in `pages`; it stores identity info (name, language, platform accounts) only — **no fixed production architecture**. Production options (render engine, voice model, editing mode, source type) are chosen **per-job at video creation time** in the Studio UI. Everything downstream (`jobs` → `videos` → `assets`, `posts` → `metrics`) is keyed by `page_id`. **Adding a new page = inserting a row, not changing the schema.** Schema: [Dashboard/db/schema.sql](Dashboard/db/schema.sql).
- **`GameStory/`** — the first page/sub-project: a Vietnamese story-narration channel. Its [config/page.json](GameStory/config/page.json) carries output path and account info. Subfolders `scripts/ assets/ output/` hold generated artifacts (gitignored).

### Per-page account isolation (critical)

Revised risk model: one email/Google (or Meta) account linking **many channels across different platforms is normal and fine** — e.g. one email owning a YouTube channel + a Facebook Page + a TikTok account, or the same email reused across several pages. That is **not** a warning.

The real cascade risk is **same-platform duplication under one email**: using one email for **2+ channels of the SAME platform** (e.g. one email → two Facebook Pages, or one email → two YouTube channels). A termination/ban of one same-platform channel can cascade to its sibling under the same Google/Meta account. The dashboard **WARNS** about this (it does not block it): `fetch_org()` computes a `riskPlatforms` list per email group (platforms appearing 2+ times across that group's channels) for the OrgChart to flag.

Each page still has its **own** row per platform in `platform_accounts` with `UNIQUE (page_id, platform)` (a single page can't double-link one platform). The new cross-page, same-email, same-platform risk is detected only in `fetch_org()`, not enforced as a DB constraint. Secrets stay path-only (see Secrets below): nothing here reads or stores tokens, only `account_label`/platform.

### Secrets

OAuth tokens / credentials are **never** stored in the DB or in config — `platform_accounts.credentials_ref` (and the `accounts` block in page configs) only holds a **path** to a token file under `Dashboard/secrets/<page>/<platform>.json`, which is gitignored.

### Production pipeline

The pipeline is the same entry/exit for all jobs; which steps run depends on `render_mode` and `edit_mode` chosen at creation.

**Image mode** (AI-generated visuals):
```
Studio UI → job created → (link? yt-dlp + faster-whisper) → Claude Code (script JSON)
         → VieNeu-TTS (audio) → faster-whisper (timestamps) → ComfyUI+SDXL (images)
         → FFmpeg (Ken Burns + captions + bgm) → upload → PostgreSQL
```

**Footage mode** (translate/reup):
```
Studio UI → job created → yt-dlp (download source) → faster-whisper (transcript)
         → Claude Code (script JSON, edit_mode applied) → VieNeu-TTS (audio)
         → FFmpeg (cut source footage + captions + bgm) → upload → PostgreSQL
```

Two non-obvious points:
- **VieNeu-TTS does not emit timestamps.** Run faster-whisper on the generated audio to get per-line timestamps; those timestamps drive scene length and caption sync.
- The **FFmpeg assembly step exceeds plain n8n** and is intended to be a small Python microservice called over HTTP — it is the only part expected to be hand-written code.

### Production modes (chosen per-job at creation time)

Every job carries a `render_mode` and an `edit_mode` chosen in the Studio UI. There is
no page-level default — the owner picks both when creating the video.

**`render_mode`** — the visual production engine:
- `image` — AI-generated images (ComfyUI+SDXL) per scene.
- `footage` — source video cut into scenes (translate/reup).
- `stickman` — procedural 2D/Blender stickman animation.
- `clone` — re-render an existing video at a different aspect ratio.

**`edit_mode`** — how the source is transformed (required for `footage` jobs):
- **Summary** — condense the full source into a shorter narration-led video.
- **Commentary** — translate + explain, give a take, analyze; original footage ≤ 20–40%.
- **Recap** — condense and retell selectively (films, anime, podcasts, long news); re-order logic and add explanation.
- **Educational** — turn the content into a lesson / how-to / explanation.
- **Dubbed** — voiceover-replace the source audio (no TTS re-generation); credit gate applies.

All footage modes obey the same safety formula: real transformation, *your* voice as
the main content (> 60–80%), original footage as illustration only, no verbatim reupload.
Full playbook: [how to edit video.md](how%20to%20edit%20video.md).

**Per-video working rule.** When working on any footage job, follow the selected
`edit_mode` and the safety formula above. Record the source on the row:
`videos.source_name` / `videos.source_link` (credited at the video's end).

**Pre-workflow rule (mandatory).** Before kicking off the production workflow for a
footage/translate job, you MUST confirm `render_mode` and `edit_mode` with the owner.
Do not assume a default — the mode changes the script, the original-footage ratio, and
the assembly. Only proceed once the owner has chosen.

## Database

PostgreSQL, local now (cloud later, same schema). Initialize:

```bash
createdb contentfactory
psql -d contentfactory -f Dashboard/db/schema.sql
```

Tables: `pages` (identity only), `platform_accounts`, `jobs` (all production options here), `videos`, `assets`, `posts`, `metrics`.

## Harness: ContentFactory

**Mục tiêu:** Vận hành dự án bằng đội agent chuyên trách cho mọi việc đụng tới pipeline sản xuất, dashboard, model/media stack, schema, nội dung, kiểm thử, bảo mật, nghiên cứu.

**Main session = leader.** Main session không spawn thêm một "leader agent" — main session chính là leader, đóng vai trò điều phối trực tiếp. Khi có task ContentFactory: main session tự decompose, chọn specialist agents phù hợp (3–5 agent từ pool), spawn chúng trực tiếp, theo dõi kết quả, tổng hợp và báo cáo user. Không có tầng trung gian nào giữa main session và specialist agents.

**Pool specialist agents:** backend-engineer, frontend-engineer, media-engineer, content-strategist, qa, security-review, tester, researcher. Vai trò từng agent xem trong `.claude/agents/`. File `leader.md` là tham chiếu mô tả vai trò và policy của main session khi điều phối — không dùng để spawn subagent.

**Trigger:** Khi có yêu cầu công việc thực chất liên quan đến ContentFactory (pipeline, FastAPI, dashboard React, model/media: ComfyUI/SDXL · TTS F5-TTS/ViVoice/VieNeu · faster-whisper · FFmpeg · stickman, schema, script/editing-mode, QA/security/test/research) — kể cả follow-up ("chạy lại", "re-run", "cập nhật", "sửa", "bổ sung", "thêm agent", "chỉ chạy lại phần X", "dựa trên kết quả trước", "cải thiện") — main session tự điều phối. Câu hỏi đơn giản có thể trả lời trực tiếp.

**Lịch sử thay đổi:**

| Phiên bản | Ngày | Thay đổi |
|------|------|------|
| 1.0.0 | 2026-06-21 | Khởi tạo harness (9 agent: leader, backend/frontend/media/content, qa, security-review, tester, researcher; orchestrator + 8 skill capability) |
| 1.1.0 | 2026-06-25 | Bỏ skill `contentfactory-orchestrator` (duplicated leader logic) |
| 1.2.0 | 2026-06-25 | Main session = leader; không spawn leader subagent; `leader.md` là policy reference cho main session |

**Config:** risk-mode=hỏi · git-policy=N/A (dự án không phải git repo)

**Token Saver Policy:** Xử lý task nội bộ bằng tiếng Anh (mọi model, mọi agent, artifact `_workspace/`, và narration mô tả bước đang làm — gồm cả câu dẫn một dòng ngay trước một tool call). Dùng **tiếng Việt** chỉ khi: (a) hỏi/xác nhận/yêu cầu input/giải thích lý do cho người dùng; (b) kết quả cuối cùng (gồm tóm tắt cuối trình cho người dùng). Task ngắn/đơn giản (sửa UI/text/nhãn): bỏ narration chi tiết, chỉ dùng placeholder cực ngắn (`doing...`).

**Honesty policy:** Mọi agent phải trung thực về kết quả. Gặp điều đáng nghi/mâu thuẫn/mơ hồ nằm ngoài quyền tự quyết → hỏi lại người dùng kèm đề xuất phương án, không tự đoán đi tiếp. KHÔNG tự đánh dấu pass/hoàn thành khi chưa thực sự đạt — fail hoặc chưa kiểm chứng được thì báo cáo đúng hiện trạng.

**Dummy data isolation:** File dữ liệu dummy/giả/mẫu dùng để test local (seed, fixture, mock JSON, sample CSV, DB seed...) lưu riêng trong thư mục chuyên dụng (vd `_dummy_data/` hoặc `test/fixtures/`), không rải lẫn vào code/nguồn dự án.
