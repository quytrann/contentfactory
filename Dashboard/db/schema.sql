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

    -- Input
    input_type    TEXT NOT NULL,        -- prompt | link
    input_payload TEXT NOT NULL,        -- the prompt text or URL

    -- Production options (all chosen at video creation time)
    render_mode        TEXT,            -- footage | image | stickman | clone
    edit_mode          TEXT,            -- commentary | recap | educational | summary | dubbed
    voice              TEXT,            -- TTS preset name or clone:<name>
    voice_clone_model  TEXT,            -- voice-clone engine (f5-tts | vivoice | ...)
    render_model       TEXT,            -- image/animation engine (sdxl | stickman_blender | ...)
    aspect             TEXT,            -- 9:16 | 16:9 | 1:1 | 4:5
    target_sec         INT,             -- target output length in seconds (whole source condensed to this)
    src_audio_volume   REAL NOT NULL DEFAULT 0,  -- source audio level in the final mix (0 = voiceover only)
    add_credit         BOOLEAN NOT NULL DEFAULT TRUE,  -- append source-credit slate?
    title              TEXT,            -- user-supplied output title for the new video
    comment            TEXT,            -- extra re-create instruction

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
    source_video_id       BIGINT,       -- reference video for clone/commentary source

    -- Orchestration & progress
    status           TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed | needs_input | stopped
                                        -- ^ 'stopped' = user hit POST /jobs/{id}/stop. Distinct from
                                        --   'failed' so the FE offers RESUME (retry of a stopped job
                                        --   continues from last_step). No CHECK constraint, so the new
                                        --   value is accepted without a migration.
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
