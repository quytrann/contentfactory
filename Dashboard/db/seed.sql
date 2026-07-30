-- ContentFactory Dashboard — schema migration (NO data, NO truncation).
-- Brings an existing database up to the current schema without recreating tables.
-- Safe to run repeatedly (all statements are idempotent).
--
--   psql -d contentfactory -f Dashboard/db/seed.sql
--
-- To add a real page, use add_page.sql instead.

-- ---------------------------------------------------------------------------
-- MIGRATION: remove fixed architecture from pages
-- pages.architecture_type and pages.config were removed in the architecture
-- redesign (2026-06-25). Production options now live in jobs. Drop them if
-- they still exist on an older database.
-- ---------------------------------------------------------------------------
ALTER TABLE pages DROP COLUMN IF EXISTS architecture_type;
ALTER TABLE pages DROP COLUMN IF EXISTS config;

-- ---------------------------------------------------------------------------
-- MIGRATION: jobs — production option columns
-- These were previously added here via ALTER TABLE; they are now declared
-- directly in schema.sql for fresh installs. The IF NOT EXISTS guards make
-- these no-ops on a new DB and safe migrations on an older one.
-- ---------------------------------------------------------------------------
ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS render_mode       TEXT,           -- footage | image | stickman | clone
    ADD COLUMN IF NOT EXISTS edit_mode         TEXT,           -- commentary | recap | educational | summary | dubbed
    ADD COLUMN IF NOT EXISTS voice             TEXT,           -- TTS preset or clone:<name>
    ADD COLUMN IF NOT EXISTS voice_clone_model TEXT,           -- f5-tts | vivoice | ...
    ADD COLUMN IF NOT EXISTS render_model      TEXT,           -- sdxl | stickman_blender | ...
    ADD COLUMN IF NOT EXISTS aspect            TEXT,           -- 9:16 | 16:9 | 1:1 | 4:5
    ADD COLUMN IF NOT EXISTS target_sec        INT,
    ADD COLUMN IF NOT EXISTS src_audio_volume  REAL NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS add_credit        BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS title             TEXT,
    ADD COLUMN IF NOT EXISTS comment           TEXT,
    ADD COLUMN IF NOT EXISTS clone_of_video_id     BIGINT,
    ADD COLUMN IF NOT EXISTS reuse_script_video_id BIGINT,
    ADD COLUMN IF NOT EXISTS bypass_tts_cache  BOOLEAN NOT NULL DEFAULT FALSE,  -- per-job force-fresh TTS (skip cache READ, keep WRITE)
    ADD COLUMN IF NOT EXISTS source_video_id       BIGINT,
    ADD COLUMN IF NOT EXISTS progress_step    TEXT,
    ADD COLUMN IF NOT EXISTS progress_pct     INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS progress_msg     TEXT,
    ADD COLUMN IF NOT EXISTS needs_input      JSONB,
    ADD COLUMN IF NOT EXISTS timings          JSONB,
    ADD COLUMN IF NOT EXISTS publish_platform TEXT;

-- Legacy column kept for data preservation; superseded by target_sec.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cap_minutes INT;

-- Forward-reference FK constraints (jobs → videos). Idempotent.
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
-- MIGRATION: videos — extra columns
-- ---------------------------------------------------------------------------
ALTER TABLE videos
    ADD COLUMN IF NOT EXISTS source_name TEXT,
    ADD COLUMN IF NOT EXISTS source_link TEXT,
    ADD COLUMN IF NOT EXISTS width       INT,
    ADD COLUMN IF NOT EXISTS height      INT,
    ADD COLUMN IF NOT EXISTS thumb_path  TEXT;

-- ---------------------------------------------------------------------------
-- MIGRATION: platform_accounts — approval column
-- ---------------------------------------------------------------------------
ALTER TABLE platform_accounts
    ADD COLUMN IF NOT EXISTS approval TEXT NOT NULL DEFAULT 'not_started';

-- ---------------------------------------------------------------------------
-- MIGRATION: metrics — shares column
-- ---------------------------------------------------------------------------
ALTER TABLE metrics
    ADD COLUMN IF NOT EXISTS shares BIGINT;

-- ---------------------------------------------------------------------------
-- MIGRATION: posts — manual (hand-marked off-platform upload) column
-- TRUE = user hand-marked the video as uploaded to the platform OUTSIDE the API
-- (no real platform_post_id/url). Set by POST /api/videos/mark-posted.
-- ---------------------------------------------------------------------------
ALTER TABLE posts
    ADD COLUMN IF NOT EXISTS manual BOOLEAN NOT NULL DEFAULT FALSE;

-- No seed/sample data. Insert real pages via Dashboard/db/add_page.sql.
