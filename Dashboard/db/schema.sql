-- ContentFactory Dashboard — PostgreSQL schema
-- Central store for managing multiple content pages/channels in parallel.
-- Pages are identity containers (name, language, platform accounts, analytics anchor).
-- Production options (render engine, voice, editing mode, source type) are chosen
-- per-job at video creation time — NOT locked to the page.
-- Target: local PostgreSQL now; same schema migrates to cloud later.

-- ---------------------------------------------------------------------------
-- pages: one row per channel/page (e.g. "Giải Thích Mọi Thứ").
-- Stores identity and publishing info only. No fixed architecture type or
-- pipeline config — those decisions live in jobs.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pages (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE,
    language          TEXT NOT NULL DEFAULT 'vi',
    status            TEXT NOT NULL DEFAULT 'active',    -- active | paused | archived

    -- Creator/credit info: ALWAYS belongs to the project owner, never the
    -- borrowed Claude account. Fill only when the owner provides values.
    creator_name      TEXT,
    author            TEXT,
    credit            TEXT,
    channel_url       TEXT,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- platform_accounts: per-page, per-platform publishing identity.
-- ISOLATION RULE: each page should use its OWN Google/social account so that a
-- termination on one channel does NOT cascade to other pages' channels.
-- Do NOT store raw OAuth secrets here; credentials_ref points to a token file
-- kept outside the DB (e.g. Dashboard/secrets/<page>/<platform>.json, gitignored).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS platform_accounts (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    page_id         BIGINT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    platform        TEXT NOT NULL,                         -- youtube | tiktok | instagram | facebook
    account_label   TEXT,                                  -- gmail address / handle (identifier, not a secret)
    account_type    TEXT NOT NULL DEFAULT 'personal',      -- personal | business
    credentials_ref TEXT,                                  -- path to OAuth token file (NOT the secret itself)
    status          TEXT NOT NULL DEFAULT 'active',        -- active | blocked | terminated
    approval        TEXT NOT NULL DEFAULT 'not_started',   -- not_started | pending | approved
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (page_id, platform)
);

-- ---------------------------------------------------------------------------
-- jobs: one production request.
-- All production options are chosen at job creation time (Studio UI).
-- Forward-reference FKs to videos (clone_of_video_id, reuse_script_video_id,
-- source_video_id) are added after the videos table via DO block below.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    page_id       BIGINT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    page_seq      BIGINT,               -- per-page display sequence (1,2,3… in creation order),
                                        -- assigned at INSERT = MAX(page_seq for this page)+1. This is
                                        -- the human-friendly "Job #N" shown in the dashboard: the raw
                                        -- `id` grows GLOBALLY with gaps (deletes/retries), whereas
                                        -- page_seq counts per page and matches the job-history order.

    -- Input
    input_type    TEXT NOT NULL,        -- prompt | link
    input_payload TEXT NOT NULL,        -- the prompt text or URL

    -- Production options (all chosen at video creation time)
    render_mode        TEXT,            -- footage | image | stickman | clone
    edit_mode          TEXT,            -- commentary | recap | educational | summary | dubbed
    voice              TEXT,            -- TTS preset name or clone:<name>
    voice_clone_model  TEXT,            -- voice-clone engine (f5-tts | vivoice | ...)
    render_model       TEXT,            -- image/animation engine (sdxl | stickman_blender | ...)
    llm_provider       TEXT,            -- TEXT script-gen backend: claude-cli | gemini | openrouter
                                        -- NULL = 'claude-cli' (Claude Code headless on the owner's
                                        -- subscription) — the ONLY behavior that existed before the
                                        -- provider gate, and still the default. NULL is stored rather
                                        -- than a literal so an unset job is byte-identical to a
                                        -- pre-gate row. Vision calls ignore this (always claude-cli).
    llm_model          TEXT,            -- model id within that provider (e.g. gemini-flash-latest).
                                        -- NULL = the provider's own default (claude-cli -> SCRIPT_GEN_MODEL).
    aspect             TEXT,            -- 9:16 | 16:9 | 1:1 | 4:5
    target_sec         INT,             -- target output length in seconds (whole source condensed to this)
    src_audio_volume   REAL NOT NULL DEFAULT 0,  -- source audio level in the final mix (0 = voiceover only)
    add_credit         BOOLEAN NOT NULL DEFAULT TRUE,  -- append source-credit slate?
    title              TEXT,            -- user-supplied output title for the new video
    comment            TEXT,            -- extra re-create instruction
    cover_image_path   TEXT,            -- optional custom cover (SDXL-generated) used as the
                                        -- video poster/thumbnail INSTEAD of an extracted frame.
                                        -- Only set when the Studio's "use cover" option is on.
    facebook_tags      TEXT,            -- owner-edited Facebook hashtag block (space-joined "#a #b ...")
                                        -- generated at create-time; copied onto videos.facebook_tags by
                                        -- the runner so it can be copied at manual-upload time.

    -- Script/clip reuse shortcuts (FKs added below after videos is defined)
    clone_of_video_id     BIGINT,
    -- ^ CLONE: re-render an existing video at a different aspect. Runner uses
    --   cached assets (script/audio/visuals) from _cache/renders/<id>/ — skips
    --   ingest/script/TTS/SDXL. Target aspect comes from jobs.aspect.
    reuse_script_video_id BIGINT,
    -- ^ SCRIPT-REUSE: skip script-gen (no claude -p), load that video's
    --   videos.script JSONB straight into TTS. Footage-mode still ingests;
    --   image/stickman modes skip ingest when script is reused.
    bypass_tts_cache      BOOLEAN NOT NULL DEFAULT FALSE,
    -- ^ Per-job force-fresh TTS: when TRUE the /generate/tts read of the
    --   per-scene TTS cache is SKIPPED (every scene re-synthesized), while the
    --   post-synth cache WRITE still warms it. Used by "Dùng lại kịch bản" to
    --   reuse a saved script but regenerate the voice. Default FALSE.
    bypass_script_cache   BOOLEAN NOT NULL DEFAULT FALSE,
    -- ^ Per-job force-fresh script-gen: when TRUE the disk cache READ for each
    --   script-gen batch is skipped so Claude headless always re-generates. The
    --   post-gen cache WRITE still warms the entry for future retries. Default FALSE.
    source_video_id       BIGINT,       -- reference video for clone/commentary source

    -- Orchestration & progress
    status           TEXT NOT NULL DEFAULT 'queued',   -- held | queued | running | done | failed | needs_input | stopped
                                        -- ^ 'stopped' = user hit POST /jobs/{id}/stop. Distinct from
                                        --   'failed' so the FE offers RESUME (retry of a stopped job
                                        --   continues from last_step). 'held' = source-list "Save"
                                        --   persisted the job but it must NOT auto-run; the runner's
                                        --   _claim_job selects only 'queued', so 'held' sits until
                                        --   POST /api/jobs/release flips it → 'queued' on "Tạo video".
                                        --   No CHECK constraint, so new values are accepted without a
                                        --   migration.
    publish          BOOLEAN NOT NULL DEFAULT false,
    publish_platform TEXT,              -- youtube | tiktok | instagram | facebook
                                        -- NULL = no platform chosen → runner skips auto-publish
    progress_step    TEXT,              -- current pipeline step key
    progress_pct     INT NOT NULL DEFAULT 0,  -- 0..100
    progress_msg     TEXT,              -- human-readable current activity (Vietnamese, shown in dashboard)
    needs_input      JSONB,
    -- ^ Credit-gate payload (Dubbed path). When the runner cannot find source
    --   credit fields after ingest it parks the job (status='needs_input') and
    --   writes here: {kind, missingFields[], prefill{}, creditDecision, videoId}.
    --   creditDecision: null=unresolved | 'provided'=user entered | 'skipped'=explicit skip.

    -- Cost & diagnostics
    cost_usd      NUMERIC(10,4) NOT NULL DEFAULT 0,
    timings       JSONB,
    -- ^ Per-step wall-time: {step_name: seconds_float, ...}.
    --   Written best-effort at job finalize; NULL on older rows or pre-step crash.
    error         TEXT,

    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- videos: rendered output of a job. Also the analytics anchor for the page.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS videos (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id      BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    page_id     BIGINT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    title       TEXT,
    description TEXT,
    source_name TEXT,                   -- original creator/channel credited at video end
    source_link TEXT,                   -- URL of the source video (for reup/translate jobs)
    script      JSONB,                  -- [{scene, narration, image_prompt}]
    audio_path  TEXT,
    video_path  TEXT,
    thumb_path  TEXT,                   -- poster frame for the Videos grid
    width       INT,                    -- output frame width
    height      INT,                    -- output frame height
    duration_s  NUMERIC(8,2),
    status      TEXT NOT NULL DEFAULT 'rendering',   -- rendering | ready | published | failed
    facebook_tags TEXT,                 -- copied from jobs.facebook_tags by the runner; the copy-ready
                                        -- Facebook hashtag block shown in the Videos list for manual upload
    llm_provider_used TEXT,             -- which LLM ACTUALLY wrote this video's script (claude-cli |
                                        -- gemini | openrouter). Written by the runner right where the
    llm_model_used    TEXT,             -- script is saved. NULL on rows produced before the provider
                                        -- gate, and on script-REUSE runs (no LLM call was made — we
                                        -- record what ran, never what would have run).
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Forward-reference FKs: jobs → videos (circular, defined here after videos exists).
-- Idempotent: skipped if constraint already exists.
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'jobs_clone_of_video_id_fkey'
    ) THEN
        ALTER TABLE jobs ADD CONSTRAINT jobs_clone_of_video_id_fkey
            FOREIGN KEY (clone_of_video_id) REFERENCES videos(id) ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'jobs_reuse_script_video_id_fkey'
    ) THEN
        ALTER TABLE jobs ADD CONSTRAINT jobs_reuse_script_video_id_fkey
            FOREIGN KEY (reuse_script_video_id) REFERENCES videos(id) ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'jobs_source_video_id_fkey'
    ) THEN
        ALTER TABLE jobs ADD CONSTRAINT jobs_source_video_id_fkey
            FOREIGN KEY (source_video_id) REFERENCES videos(id) ON DELETE SET NULL;
    END IF;
END $$;

-- Idempotent column migrations (safe to re-run over an existing DB).
-- page_seq was added to the CREATE TABLE block above after jobs already existed on
-- some DBs; CREATE TABLE IF NOT EXISTS silently skips those, so it needs its own
-- migration line like the others below.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS page_seq BIGINT;
-- Same class of gap as page_seq above: bypass_script_cache was added to CREATE TABLE
-- after jobs already existed on some DBs (e.g. contentfactory_test), so it never
-- landed there without its own migration line.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS bypass_script_cache BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cover_image_path TEXT;
-- Facebook hashtags: carried from create-time on the job, copied onto the video row.
ALTER TABLE jobs   ADD COLUMN IF NOT EXISTS facebook_tags TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS facebook_tags TEXT;
-- Per-job LLM choice for TEXT script-gen (provider gate, llm_gate.py). Additive and
-- nullable on purpose: NULL means 'claude-cli' with its default model, i.e. exactly the
-- behavior every existing row already had. No CHECK constraint, so a new provider id is
-- accepted without a migration (same rule as jobs.status).
ALTER TABLE jobs   ADD COLUMN IF NOT EXISTS llm_provider TEXT;
ALTER TABLE jobs   ADD COLUMN IF NOT EXISTS llm_model    TEXT;
-- What ACTUALLY served the script-gen call for a finished video (audit trail: the owner
-- must be able to see which model wrote a script, especially once a non-default provider
-- is selectable). NULL on pre-gate rows and on script-reuse runs.
ALTER TABLE videos ADD COLUMN IF NOT EXISTS llm_provider_used TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS llm_model_used    TEXT;

-- ---------------------------------------------------------------------------
-- assets: per-scene generated images / audio / music for a video.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assets (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    video_id    BIGINT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,          -- image | audio | music
    scene_index INT,
    path        TEXT NOT NULL,
    prompt      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- posts: one row per platform upload of a video.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS posts (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    video_id            BIGINT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    platform_account_id BIGINT REFERENCES platform_accounts(id) ON DELETE SET NULL,
    platform            TEXT NOT NULL,  -- youtube | tiktok | instagram | facebook
    platform_post_id    TEXT,
    url                 TEXT,
    privacy             TEXT,           -- public | private | self_only
    status              TEXT NOT NULL DEFAULT 'pending',
    -- pending | draft | posted | failed
    -- draft = Facebook DRAFT Reel (recorded, not public; counts toward rate-limit window)
    manual              BOOLEAN NOT NULL DEFAULT FALSE,
    -- TRUE = user hand-marked as uploaded off-platform (manual upload); there is no
    -- real platform_post_id/url and nothing was uploaded via the API.
    posted_at           TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- metrics: periodic snapshots of post performance.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metrics (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    post_id    BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    views      BIGINT,
    likes      BIGINT,
    comments   BIGINT,
    shares     BIGINT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_accounts_page ON platform_accounts(page_id, platform);
CREATE INDEX IF NOT EXISTS idx_jobs_page     ON jobs(page_id, status);
CREATE INDEX IF NOT EXISTS idx_videos_page   ON videos(page_id, status);
CREATE INDEX IF NOT EXISTS idx_posts_video   ON posts(video_id);
CREATE INDEX IF NOT EXISTS idx_metrics_post  ON metrics(post_id, fetched_at);
