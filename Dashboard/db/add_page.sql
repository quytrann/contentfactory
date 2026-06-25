-- Add the real "CTG Gaming" page + its YouTube account.
-- Real data — persists across restarts; nothing in the repo wipes it.
-- Idempotent: re-running updates the same page/account instead of duplicating.
--   psql -d contentfactory -f Dashboard/db/add_page.sql

BEGIN;

WITH up AS (
    INSERT INTO pages (name, language, status,
                       creator_name, author, credit, channel_url)
    VALUES (
        'CTG Gaming',              -- name (unique)
        'vi',                      -- language
        'active',                  -- active | paused | archived
        'CTG Gaming',              -- creator_name (owner's)
        'CTG Gaming',              -- author       (owner's)
        NULL,                      -- credit       (owner's; not provided)
        'https://www.youtube.com/@CTG.GameStory'
    )
    ON CONFLICT (name) DO UPDATE SET
        creator_name = EXCLUDED.creator_name,
        author       = EXCLUDED.author,
        channel_url  = EXCLUDED.channel_url,
        updated_at   = now()
    RETURNING id
)
INSERT INTO platform_accounts (page_id, platform, account_label, account_type,
                               credentials_ref, status, approval)
SELECT id, 'youtube',
       'contentfactory.gamestory@gmail.com',          -- account_label (gmail; handle is in channel_url)
       'personal',                                    -- personal | business
       'Dashboard/secrets/ctg-gaming/youtube.json',   -- path to token file (NOT the secret)
       'active',                                      -- active | blocked | terminated
       'not_started'                                  -- not_started | pending | approved
FROM up
ON CONFLICT (page_id, platform) DO UPDATE SET
    account_label   = EXCLUDED.account_label,
    account_type    = EXCLUDED.account_type,
    credentials_ref = EXCLUDED.credentials_ref,
    status          = EXCLUDED.status,
    approval        = EXCLUDED.approval;

COMMIT;
