# how_to_install.md — Setting ContentFactory up on a new machine

Audience: an **AI agent (or engineer) on a different Windows machine** that has this repository
but none of the tooling. Read this top to bottom before installing anything. Every path,
version and command below was verified against a working install; where a value is
machine-specific it is called out explicitly in [Step 9](#step-9--configure-env-the-only-file-you-must-edit).

Related docs — do not duplicate them, read them when the section points you there:

| File | What it covers |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Hard project rules, architecture, the mandatory API restart procedure |
| [README.md](README.md) | Project overview |
| [project_define.md](project_define.md) | Product definition |
| [Dashboard/README.md](Dashboard/README.md) | Dashboard internals |
| [how to edit video.md](how%20to%20edit%20video.md) | Editing-mode playbook (content, not setup) |
| [video-production-lessons.md](video-production-lessons.md) | Accumulated production findings |

---

## 1. What this project is

ContentFactory is an **automated short-video factory** (YouTube Shorts / Reels / TikTok /
Facebook Reels) that runs **100% locally and free** on one Windows PC with one NVIDIA GPU.
It ingests a source (a link or a topic), writes a Vietnamese script, synthesizes a cloned
Vietnamese voice, builds the visuals, and assembles a finished vertical video.

Four constraints shape every install decision. Violating them breaks the project's premise:

1. **Local and free only.** No paid APIs, no cloud rendering. Everything below is
   self-hosted or free-tier.
2. **The LLM is Claude Code in headless mode** (`claude -p`), billed to a subscription —
   **not** the Anthropic API. Script generation shells out to the `claude` CLI.
3. **Target GPU: 8 GB VRAM** (reference machine: RTX 2070 Max-Q). Models are chosen to fit
   8 GB and they run **sequentially, never concurrently**. Disk/shared memory does not
   expand usable VRAM.
4. **Video content language is Vietnamese.** Code, comments and prompts are English; only
   what a human end-user reads or hears is Vietnamese.

**Borrowed-account rule (important for an AI agent):** the Claude account used for
development may not belong to the project owner. Never infer a creator name, channel,
billing detail or account identity from the logged-in account. Leave such fields as
`TODO_ASK_USER` and ask.

---

## 2. Repository structure

```
ContentFactory/
├─ CLAUDE.md                  Project rules an agent MUST follow
├─ how_to_install.md          This file
├─ ContentFactory.bat         One-click launcher (see Step 13)
├─ create-shortcut.ps1        Creates the Desktop shortcut (see Step 13)
├─ tools/
│  └─ voice_doctor.py         Voice/TTS diagnostic (no model load for the static audit)
├─ Dashboard/
│  ├─ app.py                  Launcher: starts API + ComfyUI, opens the browser tab
│  ├─ assets/contentfactory.ico   Shortcut icon (create-shortcut.ps1 requires it)
│  ├─ db/
│  │  ├─ schema.sql           Full PostgreSQL schema — run this once
│  │  ├─ add_page.sql         Add a page/channel = INSERT a row, never a schema change
│  │  └─ seed.sql             Optional sample rows
│  ├─ api/                    The backend (this is where almost all code lives)
│  │  ├─ main.py              FastAPI app + routes + delete/cleanup logic
│  │  ├─ runner.py            Job loop: picks a queued job and drives the pipeline
│  │  ├─ generate.py          Script-gen (claude -p), TTS/image endpoints, FFmpeg assembly
│  │  ├─ word_improve.md      Pronunciation map (say_as per engine) — affects TTS cache keys
│  │  ├─ requirements.txt     API venv dependencies
│  │  ├─ run-api.ps1          Canonical dev launcher for the API
│  │  ├─ .env                 ALL machine-specific configuration (see Step 9)
│  │  ├─ .venv/               API virtualenv (you create it in Step 4)
│  │  ├─ logs/api.log         Rotating log — first place to look when anything fails
│  │  ├─ secrets/<page>/<platform>.json   OAuth tokens, gitignored, path-only references
│  │  └─ workers/             Subprocesses run by the SECOND venv (cf-venv)
│  │     ├─ tts_worker.py     F5-TTS / VieNeu / OmniVoice synthesis
│  │     ├─ whisper_worker.py faster-whisper transcription + word timestamps
│  │     ├─ download_worker.py yt-dlp source download
│  │     └─ ingest/probe workers
│  └─ web/                    React + TypeScript + Tailwind dashboard
│     ├─ src/                 Views, components, api.ts, data.tsx, types.ts
│     └─ dist/                BUILT output — the API serves this, not Vite (see Step 11)
└─ <page dirs>                Per-page/sub-channel folders, e.g. GameStory/
   └─ config/page.json        Page identity + its output.video_dir
```

**Finished media never lives in the repo.** It goes to `CONTENT_OUTPUT_ROOT`
(default `E:\ContentFactory`), one subfolder per page, plus shared `_voices` and `_cache`
folders. Media is large and regenerable; the repo stays small.

---

## 3. Runtime architecture (understand this before installing)

```
                    ┌──────────────────────────────────────────────┐
  browser tab ─────▶│ API + built SPA on http://127.0.0.1:4000     │
                    │ (uvicorn, API venv — ONE process serves both)│
                    └───────┬──────────────────────────┬───────────┘
                            │                          │
                    PostgreSQL :5432            runner.py job loop
                    (Windows service)                  │
                                     ┌─────────────────┼──────────────────┐
                                     ▼                 ▼                  ▼
                            cf-venv workers      ComfyUI :8188        FFmpeg
                            (torch + CUDA)       (SDXL images)     (cut/assemble)
                            TTS / whisper / yt-dlp
                                     │
                            `claude -p` CLI (script generation)
```

Five facts that surprise people:

1. **One port, one process.** The API on `:4000` also serves the built frontend. There is
   no Vite dev server in normal use, and no second terminal for the API.
2. **Two separate Python environments, on purpose.**
   - `Dashboard/api/.venv` — FastAPI/uvicorn only. **No torch.** Small and fast to start.
   - `E:\Installed\cf-venv` — the heavy ML env (torch+CUDA, F5-TTS, OmniVoice,
     faster-whisper, yt-dlp). Workers are spawned as subprocesses using this interpreter,
     so a model crash can never take the API down.
   Keeping them apart is why `CF_VENV_PYTHON` exists in `.env`.
3. **ComfyUI is a separate portable app** on `:8188`, launched by `Dashboard/app.py` /
   `run-api.ps1`. It is not a Windows service and does not auto-start on boot.
4. **PostgreSQL is a Windows service** set to Automatic, so it starts on boot by itself.
5. **The FFmpeg assembly step is hand-written Python**, not a workflow tool. It is the one
   part of the pipeline that was always going to be code.

Pipeline, footage mode (the common case):

```
Studio UI → job row → yt-dlp download → faster-whisper transcript
        → claude -p (script JSON) → TTS (F5/VieNeu/OmniVoice) → faster-whisper timestamps
        → FFmpeg (cut source + karaoke captions + bgm) → upload → PostgreSQL
```

---

## 4. Prerequisites at a glance

Reference machine values — match the **major** versions, especially Python 3.11 and the
CUDA 12.1 torch wheels.

| Component | Verified version | Install location | Needed for |
|---|---|---|---|
| Windows | 11 Pro | — | everything |
| NVIDIA GPU + driver | RTX 2070 Max-Q 8 GB, driver 610.62 | — | TTS, whisper, SDXL, NVENC |
| Python | **3.11.9** | `E:\Installed\Python311` | API + workers |
| Node.js + npm | 24.16.0 / 11.13.0 | `E:\Installed\Node` | building the dashboard, yt-dlp JS runtime |
| Git | any recent | `E:\Installed\Git` | cloning F5-TTS |
| PostgreSQL | **16.14** | `E:\Installed\PostgreSQL16` | all data |
| FFmpeg | **8.1.1 full build (Gyan)** | `E:\Installed\FFmpeg\ffmpeg-8.1.1-full_build` | cutting, karaoke, encoding |
| ComfyUI | portable (windows standalone) | `E:\Installed\ComfyUI\ComfyUI_windows_portable` | SDXL images/covers |
| Claude Code CLI | current | npm global | script generation |
| Blender | 4.x (optional) | `E:\Installed\Blender\blender.exe` | `stickman` render mode only |

**Machine convention:** local tool binaries go under `E:\Installed\<Tool>`, **never**
`C:\Program Files` — this keeps the system drive small. If an installer defaults to `C:`,
move it afterwards and point `.env` at the new path.

**Disk budget:** ~25 GB. Two SDXL checkpoints ≈ 13 GB, the F5 ViVoice checkpoint ≈ 5 GB,
Hugging Face model cache ≈ 5 GB, torch+CUDA in cf-venv ≈ 3 GB. Plus your finished videos.

---

## 5. Install order (why it matters)

Follow the steps in order. Later steps assume earlier ones:
FFmpeg before the venvs (workers probe it at import), PostgreSQL before the first API start
(the dashboard's data calls fail without it), and the web build before the first launch
(the API serves `dist/`, so an unbuilt frontend shows nothing).

---

### Step 1 — Python, Node, Git

Install Python **3.11** (not 3.12+ — the pinned torch/CUDA wheels target 3.11) to
`E:\Installed\Python311`, ticking *Add to PATH*. Install Node.js LTS to `E:\Installed\Node`
and Git to `E:\Installed\Git`.

```powershell
& 'E:\Installed\Python311\python.exe' --version   # expect: Python 3.11.x
node --version ; npm --version
git --version
```

### Step 2 — PostgreSQL 16 + the schema

Install PostgreSQL 16 to `E:\Installed\PostgreSQL16`. Keep the service on **Automatic**.
Remember the `postgres` password — it goes into `.env` in Step 9.

```powershell
$env:PGPASSWORD='<your-postgres-password>'
& 'E:\Installed\PostgreSQL16\bin\createdb.exe' -U postgres contentfactory
& 'E:\Installed\PostgreSQL16\bin\psql.exe' -U postgres -d contentfactory -f Dashboard\db\schema.sql
# verify: 7 core tables
& 'E:\Installed\PostgreSQL16\bin\psql.exe' -U postgres -d contentfactory -c "\dt"
```

Expect `pages`, `platform_accounts`, `jobs`, `videos`, `assets`, `posts`, `metrics`.
Adding a page later is an `INSERT` (see `Dashboard/db/add_page.sql`) — never a schema change.

### Step 3 — FFmpeg (must be a **full** build)

Download the **Gyan full build** (`ffmpeg-*-full_build`) and extract to
`E:\Installed\FFmpeg\`. The full build is required: the pipeline uses **libass**
(karaoke subtitles), **drawtext**, and **h264_nvenc**. The "essentials" build lacks libass
and the assembly step will fail.

```powershell
$ff = 'E:\Installed\FFmpeg\ffmpeg-8.1.1-full_build\bin\ffmpeg.exe'
& $ff -hide_banner -version | Select-Object -First 1
& $ff -hide_banner -filters  | Select-String -Pattern '\bass\b|drawtext'   # must match
& $ff -hide_banner -encoders | Select-String -Pattern 'h264_nvenc'         # must match
```

### Step 4 — API virtualenv (light, no torch)

```powershell
cd Dashboard\api
& 'E:\Installed\Python311\python.exe' -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

This installs FastAPI, uvicorn, psycopg, python-dotenv, python-multipart, the Google API
client (YouTube publishing) and pywebview (desktop-window launcher). **Do not** add torch
here — that is cf-venv's job.

### Step 5 — cf-venv (the heavy ML environment)

Create it **outside** the repo, at `E:\Installed\cf-venv`:

```powershell
& 'E:\Installed\Python311\python.exe' -m venv E:\Installed\cf-venv
$py = 'E:\Installed\cf-venv\Scripts\python.exe'
& $py -m pip install --upgrade pip

# 1) torch trio — CUDA 12.1 wheels, PINNED. Install these FIRST so nothing pulls a CPU build.
& $py -m pip install torch==2.5.1+cu121 torchaudio==2.5.1+cu121 torchvision==0.20.1+cu121 `
      --index-url https://download.pytorch.org/whl/cu121

# 2) speech + media stack
& $py -m pip install faster-whisper==1.2.1 ctranslate2==4.8.0 omnivoice==0.2.1 `
      soundfile librosa pydub vocos einops transformers datasets `
      onnxruntime yt-dlp

# 3) F5-TTS — installed from a LOCAL CLONE (it is not a plain PyPI dependency here)
git clone https://github.com/SWivid/F5-TTS.git E:\Installed\F5-TTS-main
& $py -m pip install -e E:\Installed\F5-TTS-main
```

> **Gotcha carried over from the reference machine:** F5-TTS was installed editable from a
> clone that lived at `D:\workspace\Claude Plugins\F5-TTS-main`. An editable install stores
> that absolute path, so it will **not** exist on your machine. Clone it wherever you like
> and install `-e` from there, as above.

Verify CUDA reaches the GPU from cf-venv — this is the single most important check in the
whole setup:

```powershell
& 'E:\Installed\cf-venv\Scripts\python.exe' -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
# expect: 2.5.1+cu121 True NVIDIA GeForce ...
```

If it prints `False`, you installed a CPU wheel — uninstall the trio and redo step 1 with
the `cu121` index URL.

### Step 6 — ComfyUI + SDXL checkpoints

Download the **ComfyUI Windows portable** build and extract to
`E:\Installed\ComfyUI\ComfyUI_windows_portable`. Then place the checkpoints in
`...\ComfyUI\models\checkpoints\`:

| File | Size | Role |
|---|---|---|
| `Juggernaut-XL_v9.safetensors` | 6.6 GB | **Default** — stronger prompt adherence, photoreal |
| `sd_xl_base_1.0.safetensors` | 6.5 GB | Reversible fallback |

`SDXL_CHECKPOINT` in `.env` selects the default. Nothing else needs installing — ComfyUI
portable ships its own embedded Python.

**SDXL is English-only.** Its text encoder (CLIP) ignores Vietnamese prompts, so
image/cover prompts must be written in English. This is by design, not a bug.

### Step 7 — Models: automatic vs manual

**Downloaded automatically** on first use, into the Hugging Face cache
(`%USERPROFILE%\.cache\huggingface\hub`). Leave `HF_HUB_OFFLINE` unset for the first run,
then the pipeline sets it to `1` to skip per-load network checks:

| Model repo | Used by |
|---|---|
| `Systran/faster-whisper-medium` | transcripts + word timestamps (`WHISPER_MODEL=medium`) |
| `k2-fsa/OmniVoice` | OmniVoice TTS engine |
| `pnnbao-ump/VieNeu-TTS-v3-Turbo` | VieNeu TTS engine |
| `SWivid/F5-TTS` | F5-TTS base assets |
| `charactr/vocos-mel-24khz` | F5 vocoder |
| `OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano` | OmniVoice tokenizer |

**Must be placed manually:** the Vietnamese F5 fine-tune checkpoint, expected at

```
E:\Installed\f5-vietnamese\ViVoice\model_last.pt      (+ config.json alongside)
```

Override with `F5_CKPT` in the environment if you store it elsewhere. Without this file the
`f5-tts` engine cannot start (VieNeu and OmniVoice still work).

**Fonts:** nothing to install. **Be Vietnam Pro** (OFL) is bundled in the repo and used for
captions/karaoke and cover text — it was chosen because its Vietnamese diacritics always
render. `DejaVuSans.ttf` is only a last-resort fallback.

### Step 8 — Claude Code CLI (script generation)

Script generation shells out to the `claude` CLI in headless mode
(`claude -p "..." --output-format json`), billed to a **subscription**. There is no API key
in this project.

```powershell
npm install -g @anthropic-ai/claude-code
# then find the real exe path — .env wants the exe, not the shim:
(Get-Command claude).Source
Get-ChildItem "$env:APPDATA\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe"
```

Put that full path in `CLAUDE_BIN`. Using the full `.exe` avoids the npm shim waiting on
stdin. Sign in once interactively (`claude`) so headless calls are authenticated.

### Step 9 — Configure `.env` (the only file you must edit)

`Dashboard/api/.env` holds **all** machine-specific configuration and ~40 tuning knobs. The
tuning knobs are heavily commented with the measurements behind their values — leave them
alone.

**A) Keys that already exist in `.env` — review each one:**

| Key | Reference value | Change it if… |
|---|---|---|
| `PGHOST` / `PGPORT` / `PGUSER` / `PGPASSWORD` / `PGDATABASE` | `localhost` / `5432` / `postgres` / `postgres` / `contentfactory` | your Postgres credentials differ |
| `API_HOST` / `API_PORT` | `127.0.0.1` / **`4000`** | **do not change 4000** — see the port rule below |
| `CLAUDE_BIN` | full path to `claude.exe` | always (Step 8) |
| `FFMPEG_BIN` / `FFPROBE_BIN` | `E:\Installed\FFmpeg\ffmpeg-8.1.1-full_build\bin\...` | your FFmpeg version/path differs |
| `SDXL_CHECKPOINT` | `Juggernaut-XL_v9.safetensors` | you use a different checkpoint |
| `ASSEMBLE_PARALLEL` | `5` | fewer than ~12 logical CPU cores → lower it |
| `SCRIPT_GEN_CONCURRENCY` | `4` | you hit Claude rate limits → lower it |
| `INGEST_MAX_SEC` / `FOOTAGE_MAX_HEIGHT` | `7200` / `720` | you want a different source cap |

**B) Keys that are NOT in `.env` — they are code defaults.** Do not go looking for them in
the file; **add a line only if the default is wrong for your machine**:

| Key | Code default | Defined in | Add a line if… |
|---|---|---|---|
| `CF_VENV_PYTHON` | `E:\Installed\cf-venv\Scripts\python.exe` | `generate.py` | cf-venv is elsewhere |
| `CONTENT_OUTPUT_ROOT` | `E:\ContentFactory` | `generate.py` | media belongs on another drive |
| `COMFY_URL` | `http://127.0.0.1:8188` | `generate.py` | ComfyUI runs elsewhere |
| `WHISPER_MODEL` / `_DEVICE` / `_COMPUTE` | `medium` / `cuda` / `float16` | `generate.py` | less VRAM → `small`; no GPU → `cpu` + `int8` |
| `F5_CKPT` | `E:\Installed\f5-vietnamese\ViVoice\model_last.pt` | `workers/tts_worker.py` | the F5 checkpoint is elsewhere |
| `CF_SECRETS_ENV` | `E:\Installed\ContentFactory-secrets\.env` | `oauth_env.py` | secrets file is elsewhere (a commented example line is already in `.env`) |
| `RUNNER_ENABLED` | `1` | `runner.py` | set `0` to start the API **without** its job runner (test instances only) |

**Port 4000 is mandatory** and read from `.env`. Never hardcode `8000` anywhere, and never
check port 8000 when debugging — [CLAUDE.md](CLAUDE.md) makes this a hard rule.

**Secrets never live in the repo or the database.** Google OAuth *app* credentials
(`GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`) go in an external file **outside**
the repo, by default `E:\Installed\ContentFactory-secrets\.env`. Per-page *tokens* live at
`Dashboard/api/secrets/<page>/<platform>.json` and the database stores only a **path** to
them (`platform_accounts.credentials_ref`). Publishing is optional — everything up to
"finished mp4 on disk" works with no credentials at all.

### Step 10 — Voice reference clips

Cloned voices are **shared by every page** and live in
`<CONTENT_OUTPUT_ROOT>\_voices\`:

```
E:\ContentFactory\_voices\
├─ <Voice name> - F5-TTS.wav          reference clip per voice+engine
├─ <Voice name> - VieNeu.wav
├─ <Voice name> - OmniVoice.wav
├─ _reftext\<name>.txt                transcript sidecar (F5/VieNeu)
├─ _reftext_omni\<name>.txt           transcript sidecar (OmniVoice)
└─ _previews\                         generated previews
```

A reference clip is a clean ~10 s recording of the target voice. The sidecar holds its
transcript plus a fingerprint line (`# fp:<size>:<hash>`); a mismatch means the clip was
re-recorded and the transcript is stale. Voices are normally created through the dashboard,
which writes the sidecar for you.

Audit them any time — this needs no model load and is the fastest way to spot a broken voice:

```powershell
& 'E:\Installed\cf-venv\Scripts\python.exe' tools\voice_doctor.py
```

### Step 11 — Build the dashboard (do not skip)

The API serves `Dashboard/web/dist/`. **If you never build, the page is blank.** Equally: any
future change to `src/*.tsx` is invisible until you rebuild.

```powershell
cd Dashboard\web
npm ci
npm run build        # runs tsc --noEmit, then vite build
```

Expect `dist/index.html` plus `dist/assets/index-<hash>.js` / `.css`.

### Step 12 — Verify before launching

Run all of these. Every one should pass before you try a job.

```powershell
# 1) API venv imports and finds its config
cd Dashboard\api
.\.venv\Scripts\python.exe -c "import main, generate; print('API imports OK'); print('output root:', generate.CONTENT_OUTPUT_ROOT)"

# 2) cf-venv sees the GPU
& 'E:\Installed\cf-venv\Scripts\python.exe' -c "import torch; print('cuda:', torch.cuda.is_available())"

# 3) database reachable and populated
$env:PGPASSWORD='<pw>'; & 'E:\Installed\PostgreSQL16\bin\psql.exe' -U postgres -d contentfactory -c "SELECT count(*) FROM pages;"

# 4) FFmpeg has libass + NVENC   (see Step 3)

# 5) yt-dlp works — use the MODULE, not the exe
& 'E:\Installed\cf-venv\Scripts\python.exe' -m yt_dlp --version

# 6) start the API and check it answers
Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile -Command "& D:\workspace\ContentFactory\Dashboard\api\run-api.ps1"'
Invoke-WebRequest http://127.0.0.1:4000/api/pages -UseBasicParsing   # expect HTTP 200

# 7) the live page serves your fresh bundle
(Invoke-WebRequest http://127.0.0.1:4000/ -UseBasicParsing).Content -match 'assets/index-[A-Za-z0-9_\-]+\.js'
```

`yt-dlp.exe` on the reference machine is broken (it fails even on `--version`). Always call
it as `python -m yt_dlp`, which is what `download_worker.py` does.

### Step 13 — Launcher `.bat` + Desktop shortcut

Both already exist in the repo — you do **not** write them, you just run the shortcut
creator once everything above passes.

- [ContentFactory.bat](ContentFactory.bat) — double-click launcher. Runs
  `Dashboard/app.py` with the API venv's `pythonw.exe` (no console window).
- [Dashboard/app.py](Dashboard/app.py) — the actual logic: brings up the API on `:4000`
  **and** ComfyUI on `:8188` if they are not already running (idempotent), then opens
  `http://127.0.0.1:4000` as a browser tab. The API stays alive as a **detached** process
  after the launcher exits, so closing the tab does not stop the server.
- [create-shortcut.ps1](create-shortcut.ps1) — creates `ContentFactory.lnk` on the Desktop
  pointing at `pythonw.exe Dashboard\app.py`, with working directory `Dashboard\api` and the
  bundled icon `Dashboard\assets\contentfactory.ico`.

```powershell
cd <repo root>
powershell -NoProfile -ExecutionPolicy Bypass -File .\create-shortcut.ps1
```

It prints the created path and **throws if a required file is missing** — a missing
`.venv\Scripts\pythonw.exe` means Step 4 did not finish; a missing icon means the repo is
incomplete. Re-run it any time to refresh the shortcut.

PostgreSQL is deliberately **not** started by the launcher: it is an auto-start Windows
service already. ComfyUI **is** started, because a portable build does not auto-start and
image/cover generation would otherwise fail after a fresh reboot.

To **stop** the API, use the zombie sweep in the next section — there is no stop button.

### Step 14 — First smoke test

1. Open the dashboard (Desktop shortcut or `ContentFactory.bat`).
2. Create a page if none exists (`Dashboard/db/add_page.sql`, or the Pages view).
3. Create a voice in the dashboard and let it record/import a ~10 s reference clip.
4. In Studio, create a video from a short source link. **You must pick `render_mode` and
   `edit_mode` explicitly** — the pipeline has no default, and the mode changes the script,
   the allowed original-footage ratio and the assembly.
5. Watch `Dashboard/api/logs/api.log` while it runs. The finished mp4 lands in
   `<CONTENT_OUTPUT_ROOT>\<page>\video\`.

---

## 6. Operating rules that will bite you (learned the hard way)

**Restarting the API — always this exact procedure.** `uvicorn` spawns
`multiprocessing.spawn` children on Windows. The reloader parent can die while a child keeps
holding `:4000` and serving **stale code**. So a backend fix that "doesn't take effect" is
almost always an orphaned worker, not a bad fix — suspect zombies **before** re-editing code.
`Get-NetTCPConnection -LocalPort 4000` alone is not enough: it mis-attributes the socket to
already-dead PIDs and hides the live orphan. Sweep by command line:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'uvicorn|multiprocessing|spawn_main' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-NetTCPConnection -LocalPort 4000 -State Listen -ErrorAction SilentlyContinue |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile -Command "& <repo>\Dashboard\api\run-api.ps1"'
Invoke-WebRequest http://127.0.0.1:4000/api/pages -UseBasicParsing   # expect 200
```

Then confirm **exactly one** instance is listening. Tell-tale zombie symptom: `/api/system`
reports a non-null `cpu.percent` while the machine is idle with no running job.

**Never run a second API instance for testing.** A throwaway uvicorn starts its own job
runner, which will pick up and corrupt jobs from the shared queue. If you must, set
`RUNNER_ENABLED=0`.

**After any backend edit → restart the API. After any `src/*.tsx` edit → `npm run build`.**
The dashboard runs from `dist/`; skipping the build means the owner sees nothing change.

**Subprocess output must be decoded as UTF-8.** Vietnamese text in a subprocess pipe with
`text=True` uses cp1252 on Windows and dies silently. Always
`encoding="utf-8", errors="replace"`. For the same reason, do not test endpoints with
PowerShell's `Invoke-RestMethod` — it mangles Vietnamese into mojibake and sends you chasing
a bug that does not exist. Use a Python HTTP client.

**GPU contention is real.** All models share 8 GB and run sequentially. An idle ComfyUI
still pins its last checkpoint (measured 4.4 GB of 8 GB), and the footage-cut pool holds
NVENC sessions, so a TTS model load can die with a native `0xC0000005` instead of a clean
CUDA OOM. The pipeline already reclaims ComfyUI's VRAM before a GPU TTS load
(`CF_COMFY_FREE_BEFORE_GPU=1`) and makes a crashed worker wait for the cut pool to drain
(`CF_RETRY_WAIT_CUTS_S`). Keep both on.

**TTS cache keys include a voicing version and the hash of `word_improve.md`.** Change a
pronunciation or the voicing recipe and every cached scene is correctly invalidated — which
means a full re-synth (~6–8 minutes for ~90 scenes). That is intended; without it the cache
would serve the audio you just fixed.

**`uvicorn --reload` is flaky on Windows** (watchfiles misses edits). Prefer a clean restart,
or set `WATCHFILES_FORCE_POLLING=1`.

---

## 7. Troubleshooting quick table

| Symptom | Real cause | Fix |
|---|---|---|
| Dashboard page blank | `web/dist` missing | `npm run build` (Step 11) |
| Data calls fail, page loads | PostgreSQL not running | start the service; check `.env` credentials |
| `cuda: False` in cf-venv | CPU torch wheel installed | reinstall the pinned `+cu121` trio |
| Assembly fails on captions | FFmpeg "essentials" build (no libass) | install the **full** Gyan build |
| Backend fix has no effect | orphaned uvicorn worker serving stale code | command-line zombie sweep (§6) |
| `yt-dlp.exe` fails even on `--version` | broken exe shim | use `python -m yt_dlp` |
| Native crash `0xC0000005` during TTS load | VRAM exhausted by ComfyUI + NVENC | keep `CF_COMFY_FREE_BEFORE_GPU=1`; close GPU-heavy apps |
| F5 job dies at inference (`exit 127` / `0xC0000409`) | transient CUDA-init flake, **not** a broken venv | just retry the job |
| Image/cover ignores the prompt | prompt written in Vietnamese; SDXL CLIP is English-only | write image prompts in English |
| Delete reports "N files locked" | files a surviving row still references (shared per-scene audio) — not locked | ignore; only genuinely locked files are queued and retried at restart |

---

## 8. Checklist for the agent doing the install

- [ ] Hardware: NVIDIA GPU with ≥ 8 GB VRAM, current driver, ~25 GB free disk
- [ ] Python 3.11, Node LTS, Git installed under `E:\Installed\`
- [ ] PostgreSQL 16 running; `contentfactory` created; `schema.sql` applied; 7 tables present
- [ ] FFmpeg **full** build; `ass`/`drawtext` filters and `h264_nvenc` encoder present
- [ ] `Dashboard/api/.venv` created from `requirements.txt`
- [ ] `E:\Installed\cf-venv` created; `torch.cuda.is_available()` is **True**
- [ ] F5-TTS cloned and installed `-e` from a path that exists on **this** machine
- [ ] ComfyUI portable installed; SDXL checkpoint(s) in `models\checkpoints\`
- [ ] `E:\Installed\f5-vietnamese\ViVoice\model_last.pt` in place (only for the `f5-tts` engine)
- [ ] Claude Code CLI installed, signed in, full `claude.exe` path in `CLAUDE_BIN`
- [ ] `.env` reviewed: DB creds, `CLAUDE_BIN`, FFmpeg paths, `CF_VENV_PYTHON`, `CONTENT_OUTPUT_ROOT`; port still 4000
- [ ] At least one voice reference clip + sidecar in `<output root>\_voices\`
- [ ] `npm ci && npm run build` done; `dist/assets/index-<hash>.js` exists
- [ ] All seven Step-12 verifications pass
- [ ] `create-shortcut.ps1` run; Desktop shortcut opens the dashboard
- [ ] One end-to-end test video produced
- [ ] Owner asked about anything account-related (channels, credits, credentials) — never inferred
