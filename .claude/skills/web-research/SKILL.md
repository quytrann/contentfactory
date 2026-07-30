---
name: web-research
description: >-
  How to research external knowledge for ContentFactory deeply and cite sources:
  model availability/sizes (HuggingFace), library quirks/versions, error fixes,
  ComfyUI nodes, TTS engines, FFmpeg, GPU/VRAM fit, and YouTube/TikTok/Meta
  monetization rules. Use whenever the team needs facts from outside the repo.
  Triggers on "research", "tra cứu", "tìm hiểu", "model nào", "dung lượng",
  "best practice", "tại sao lỗi", "API/policy", "version".
---

# Web research

Used by **researcher** — the single entry point for all internet lookups.

## Method (deep, not shallow)
- Read multiple sources; cross-check. Prefer official docs > reputable blogs > community threads. Mind **dates** (stale answers) and **exact versions** in use (e.g. torch 2.5.1, F5-TTS 1.1.20, Blender 5.1, FFmpeg 8).
- Use `WebSearch` then `WebFetch` the promising sources. For HF file sizes, the HF API (`huggingface.co/api/models/<repo>`) lists files reliably.
- Report: (1) problem understood, (2) solution(s) + pros/cons + applicability, (3) **source URLs**, (4) recommendation. Mark "widely-verified" vs "experimental/uncertain".

## Recurring topics here
HF checkpoint sizes/availability, ComfyUI custom nodes, TTS clone engines (XTTS/OpenVoice/GPT-SoVITS/F5 finetunes), FFmpeg flags, fit on RTX 2070 8GB, platform monetization thresholds (region/time-dependent — never assert from memory).

## Reading real platforms (YouTube etc.)
For facts that live on a real video/page (not in `WebSearch` snippets) use `yt-dlp` directly — it is already configured for this project (`--js-runtimes node` + `--ffmpeg-location` to the project FFmpeg).

Primary use for ContentFactory = **"watch" a YouTube video to mine topics / pull its transcript** (feed the script-gen step):
```powershell
yt-dlp --dump-json "URL"                                   # metadata (title/duration/uploader/views)
yt-dlp --dump-json "ytsearch5:<query>"                     # search for topic ideas
yt-dlp --write-auto-sub --sub-lang "vi,en" --skip-download -o ".\_workspace\%(id)s" "URL"   # subtitles → mine content
```
**No-subtitle fallback:** use the project's LOCAL faster-whisper (`workers/whisper_worker.py`) — stays free/local. Cite the source video/page URL like any other source.

## Honesty
Separate verified facts from guesses; on conflicting sources, say so; if no solid solution, say "chưa tìm được giải pháp triệt để" with avenues tried (and collaborate: tester to trial, security to assess risk). Always cite. Report to `leader` (+ requester). Notes → `_workspace/NN_research_*.md`.
