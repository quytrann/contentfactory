-- QA fixture for Phase-2 "Video history table" boundary check.
-- Runs INSIDE a transaction that is ROLLED BACK — never persists to the DB.
-- It seeds: jobA with 2 videos (youtube posted + tiktok posted + facebook draft
-- across the two videos = 3 posts), jobB with zero posts, and one post whose
-- platform_account was deleted (pageId/pageName must come back null).
BEGIN;

-- Reuse an existing page (id taken dynamically) by inserting a fresh one to be deterministic.
-- architecture_type was dropped from pages (render_mode/edit_mode are per-JOB now).
INSERT INTO pages (name) VALUES ('QA_PAGE_X') RETURNING id \gset page_
INSERT INTO platform_accounts (page_id, platform, account_label) VALUES (:page_id, 'youtube', 'qa@x') RETURNING id \gset acc_

-- Job A: two videos, 3 posts total (yt posted, tiktok posted, fb draft url null)
INSERT INTO jobs (page_id, input_type, input_payload) VALUES (:page_id, 'prompt', 'jobA') RETURNING id \gset jobA_
INSERT INTO videos (job_id, page_id) VALUES (:jobA_id, :page_id) RETURNING id \gset vA1_
INSERT INTO videos (job_id, page_id) VALUES (:jobA_id, :page_id) RETURNING id \gset vA2_
INSERT INTO posts (video_id, platform_account_id, platform, url, status) VALUES (:vA1_id, :acc_id, 'youtube', 'https://yt/1', 'posted');
INSERT INTO posts (video_id, platform_account_id, platform, url, status) VALUES (:vA1_id, :acc_id, 'tiktok', 'https://tt/1', 'posted');
INSERT INTO posts (video_id, platform_account_id, platform, url, status) VALUES (:vA2_id, :acc_id, 'facebook', NULL, 'draft');

-- Post with NULL platform_account_id (simulates ON DELETE SET NULL) -> pageId/pageName null
INSERT INTO jobs (page_id, input_type, input_payload) VALUES (:page_id, 'prompt', 'jobC_orphan') RETURNING id \gset jobC_
INSERT INTO videos (job_id, page_id) VALUES (:jobC_id, :page_id) RETURNING id \gset vC1_
INSERT INTO posts (video_id, platform_account_id, platform, url, status) VALUES (:vC1_id, NULL, 'instagram', 'https://ig/1', 'posted');

-- Job B: zero posts
INSERT INTO jobs (page_id, input_type, input_payload) VALUES (:page_id, 'prompt', 'jobB_empty') RETURNING id \gset jobB_

-- ---- Exact published_posts column from fetch_jobs(), per job ----
\echo '=== JOB A (expect 3-element array: facebook draft url null + tiktok + youtube, ordered by platform,id) ==='
SELECT jobs.id, COALESCE(
  (SELECT jsonb_agg(jsonb_build_object(
            'platform', po.platform,'url',po.url,'pageId',pa.page_id,'pageName',pg.name,'status',po.status)
          ORDER BY po.platform, po.id)
     FROM posts po
     LEFT JOIN platform_accounts pa ON pa.id = po.platform_account_id
     LEFT JOIN pages pg ON pg.id = pa.page_id
    WHERE po.video_id IN (SELECT id FROM videos WHERE job_id = jobs.id)),
  '[]'::jsonb) AS published_posts
FROM jobs WHERE jobs.id = :jobA_id;

\echo '=== JOB C orphan (expect 1 element, pageId+pageName null) ==='
SELECT jobs.id, COALESCE(
  (SELECT jsonb_agg(jsonb_build_object(
            'platform', po.platform,'url',po.url,'pageId',pa.page_id,'pageName',pg.name,'status',po.status)
          ORDER BY po.platform, po.id)
     FROM posts po
     LEFT JOIN platform_accounts pa ON pa.id = po.platform_account_id
     LEFT JOIN pages pg ON pg.id = pa.page_id
    WHERE po.video_id IN (SELECT id FROM videos WHERE job_id = jobs.id)),
  '[]'::jsonb) AS published_posts
FROM jobs WHERE jobs.id = :jobC_id;

\echo '=== JOB B empty (expect literal [] not null) ==='
SELECT jobs.id, COALESCE(
  (SELECT jsonb_agg(jsonb_build_object(
            'platform', po.platform,'url',po.url,'pageId',pa.page_id,'pageName',pg.name,'status',po.status)
          ORDER BY po.platform, po.id)
     FROM posts po
     LEFT JOIN platform_accounts pa ON pa.id = po.platform_account_id
     LEFT JOIN pages pg ON pg.id = pa.page_id
    WHERE po.video_id IN (SELECT id FROM videos WHERE job_id = jobs.id)),
  '[]'::jsonb) AS published_posts
FROM jobs WHERE jobs.id = :jobB_id;

\echo '=== type check: pg_typeof of empty result ==='
SELECT pg_typeof(COALESCE((SELECT jsonb_agg(1) FROM posts WHERE false), '[]'::jsonb)) AS empty_type;

ROLLBACK;
