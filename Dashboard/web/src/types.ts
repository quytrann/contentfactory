// Types mirror Dashboard/db/schema.sql so the UI matches the real data model.

export type Platform = 'youtube' | 'tiktok' | 'instagram' | 'facebook' | 'x' | 'threads'
// 'needs_input' = a Dubbed (no-TTS reup) job paused waiting on source-credit
// fields the user must fill (or explicitly Skip). The runner does NOT claim it.
// 'stopped' = a job the user deliberately stopped mid-run (NOT an error). It is a
// neutral, resumable terminal state: retry RESUMES it from where it left off.
export type JobStatus = 'queued' | 'running' | 'done' | 'failed' | 'needs_input' | 'stopped'
// The video row of a parked job also reads 'needs_input' (so a restart's stale
// recovery does not flip it to 'failed').
export type VideoStatus = 'rendering' | 'ready' | 'published' | 'failed' | 'needs_input'
export type PostStatus = 'pending' | 'draft' | 'posted' | 'failed'
export type AccountStatus = 'active' | 'blocked' | 'terminated'
// 'connected' is emitted by the API (main.py) when the OAuth token file exists
// — it overrides the stored DB approval state and means the channel is linked.
export type ApprovalStatus = 'not_started' | 'pending' | 'approved' | 'connected'
export type InputType = 'prompt' | 'link'

export interface Page {
  id: number
  name: string
  language: string
  status: 'active' | 'paused' | 'archived'
  creatorName: string | null
  channelUrl: string | null
  platforms: Platform[]
  config: {
    imageModel: string
    tts: string
    timestamp: string
    motion: string
  }
  videoCount: number
  publishedCount: number
}

export interface PlatformAccount {
  id: number
  pageId: number
  platform: Platform
  accountLabel: string | null
  accountType: 'personal' | 'business'
  status: AccountStatus
  approval: ApprovalStatus
}

// One published (or drafted) post on a Job, projected from the DB `posts` table
// joined to `platform_accounts` (for the page). A Job can be published into
// channels across MANY pages and platforms, so this is an array on the Job.
// `url` is null for drafts / not-yet-public posts; `status` mirrors PostStatus
// ('posted' = live & clickable, 'draft' = recorded but not public).
// `pageId`/`pageName` are null only when the post's platform_account row was
// deleted (posts.platform_account_id is ON DELETE SET NULL) — normally set.
export interface PublishedPost {
  platform: Platform
  url: string | null
  pageId: number | null
  pageName: string | null
  status: PostStatus
}

// A credit-field name the Dubbed pause can ask the user to fill. The backend
// emits a subset of these in needsInput.missingFields and keys prefill by them.
export type CreditField = 'sourceName' | 'sourceLink' | 'handle' | 'logo'

// Carried on a job parked at status 'needs_input' (Dubbed credit pause). Usually
// null on non-parked jobs — do NOT treat null as an error. Exception: a completed
// ('done') Dubbed job whose owner turned crediting OFF (add_credit=false) carries
// creditDecision: 'disabled' without ever parking. See the BE contract §2.
export interface NeedsInput {
  kind: 'credit' // discriminator (only "credit" today)
  missingFields: CreditField[] // which credit fields were empty at pause time
  // Every credit value ingest DID return (may be partial), so the modal can
  // pre-populate known fields. Each key is null when ingest found nothing.
  prefill: Record<CreditField, string | null>
  // null = unresolved (still needs input); 'provided' = user filled fields;
  // 'skipped' = user explicitly accepted shipping with NO credit (via resume);
  // 'disabled' = crediting was turned OFF on a fresh Dubbed job, which completes
  // ('done') without parking. null/'provided'/'skipped' reach the FE via the
  // resume response; 'disabled' is set on the fresh runner path.
  creditDecision: 'provided' | 'skipped' | 'disabled' | null
  videoId: number // the video row the resume patches (display only on FE)
}

export interface Job {
  id: number
  pageId: number
  inputType: InputType
  inputPayload: string
  status: JobStatus
  costUsd: number
  createdAt: string
  finishedAt: string | null // ISO when the job reached a terminal state (done/failed); null while queued/running
  editMode: string | null
  aspect: string | null
  renderModel: string | null
  voiceCloneModel: string | null
  progressStep: string | null
  progressPct: number
  progressMsg: string | null
  error: string | null
  uploadedUrl: string | null // DEPRECATED: YouTube-only scalar URL. Kept for backend compat; the UI now renders publishedPosts instead.
  // All posts (across every platform & page) produced by this job. [] when none.
  // Powers the "Trang" (distinct page names) and "Uploaded Link" (one icon per
  // post) columns in Jobs.tsx. Replaces the single-valued uploadedUrl.
  publishedPosts: PublishedPost[]
  // Set only when status === 'needs_input' (a paused Dubbed credit job); null on
  // every other job. Powers the "Chờ nhập nguồn" badge + source-credit modal.
  needsInput?: NeedsInput | null
}

export interface Video {
  id: number
  pageId: number
  jobId: number | null // the job that produced this video; null for legacy rows. Used to join a video back to its job (Overview "Video gần đây").
  title: string
  durationS: number
  scenes: number
  status: VideoStatus
  createdAt: string
  postedPlatforms: Platform[]
  width: number | null
  height: number | null
  videoUrl: string | null
  thumbUrl: string | null
  // Production options chosen for this video (echoed from its job). Null/absent
  // on legacy rows that predate the job-options contract.
  voiceCloneModel: string | null // voice-clone engine key (e.g. 'f5-tts')
  renderModel: string | null // render/animation engine key (e.g. 'sdxl-base')
  voice: string | null // picked voice, e.g. 'clone:Korea - F5-TTS'
  srcAudioVolume: number // source-audio bed volume, 0 = off
  editMode: string | null // 'commentary' | 'recap' | 'educational' | 'summary' | 'dubbed'
  aspect: string | null // '9:16' | '16:9' | '1:1' | '4:5'
  targetSec: number | null // target duration in seconds; null = auto
  addCredit: boolean // whether a source credit was appended
  // Page ids this video has been PUBLISHED to (posts-driven, distinct). A video
  // can be published into channels belonging to MANY pages, so it appears in
  // each of those pages' "Sản phẩm" blocks. Empty [] when never published
  // (never null). The origin `pageId` is included ONLY if the video was actually
  // published to one of that page's channels — so a page's product list is
  // `pageId === id || publishedPageIds.includes(id)`.
  publishedPageIds: number[]
}

export interface PipelineStage {
  key: string
  label: string
  tool: string
}

// ---- Analytics (Overview charts) ---------------------------------------

export interface Kpi {
  key: string
  label: string
  value: string
  delta: number // percent change vs the start of the window
  spark: number[]
}

export interface PlatformSplit {
  platform: Platform
  views: number
  pct: number
}

export interface MonthValue {
  month: string
  value: number
}

export interface Analytics {
  kpis: Kpi[]
  viewsDaily: number[]
  likesDaily: number[]
  dayLabels: string[]
  videosMonthly: MonthValue[]
  platformSplit: PlatformSplit[]
}

// Per-page traffic analytics — GET /api/pages/{pageId}/analytics. Drives the
// PageDetail charts (platform pie + monthly column). Empty arrays = no data yet
// (the chart components render their own empty states).
export interface PageAnalytics {
  platformSplit: PlatformSplit[]
  viewsMonthly: MonthValue[]
}

// ---- Org map (Dashboard → email account → pages → channels) -------------
// One Google email can own MULTIPLE pages across different platforms
// (e.g. contentfactory.gamestory@gmail.com owns both "CTG Gaming" on YouTube
// and "Giải Thích Mọi Thứ" on Facebook). So an account groups pages[], and
// each page carries its own channels[].

export interface OrgChannel {
  platform: Platform
  handle: string
  // Optional: absent when the channel's credentials aren't set yet. For
  // FACEBOOK the API also sends pageId (the FB Page id) and manageUrl
  // (https://www.facebook.com/<pageId>) — the canonical Page-dashboard link.
  manageUrl?: string
  pageId?: string
  status: ApprovalStatus
}

export interface OrgPage {
  pageId: number
  pageName: string
  channels: OrgChannel[]
}

export interface OrgAccount {
  gmail: string
  pages: OrgPage[]
  // Platforms that appear 2+ times within THIS email group (same email reused
  // for multiple channels of the same platform). Empty = no risk; different
  // platforms on one email is fine. Absent on older API responses → treat as [].
  riskPlatforms?: Platform[]
}

export interface Org {
  dashboard: string
  accounts: OrgAccount[]
}

// ---- Publish flow (linked channels + per-platform publish results) ------
// GET /api/pages/{pageId}/linked-channels → LinkedChannelsResponse.
// Every returned channel is already publishable (a token file exists); the
// client shows them ALL as tickable, ALL ticked by default. An empty channels[]
// is a valid "none linked yet" state, not an error.

export interface LinkedChannel {
  accountId: number
  platform: Platform
  accountLabel: string
  // Backend emits 'page' for Facebook Pages in addition to personal/business;
  // kept as a string union of the observed values (display metadata only).
  accountType: 'personal' | 'business' | 'page'
  linked: boolean
  canPublish: boolean
}

export interface LinkedChannelsResponse {
  pageId: number
  channels: LinkedChannel[]
}

// GET /api/linked-channels → AllLinkedChannelsResponse. Connected channels
// across ALL pages, grouped by page, for the many-to-many publish modal (publish
// one video into channels belonging to multiple pages). Every channel returned is
// already linked+publishable. A page with no connected channels is omitted (or
// carries an empty channels[]); an empty pages[] means nothing is linked anywhere.
export interface AllLinkedChannelsPage {
  pageId: number
  pageName: string
  channels: LinkedChannel[]
}

export interface AllLinkedChannelsResponse {
  pages: AllLinkedChannelsPage[]
}

// POST /api/videos/{videoId}/publish → PublishResponse. Partial success is
// possible: some results ok, some carrying an error. When EVERY platform fails
// the backend returns HTTP 400 whose detail.results holds the same shape.
export interface PublishResult {
  // accountId identifies WHICH linked channel this result is for; pageId is the
  // page that channel belongs to (a video can publish into channels across many
  // pages). The UI resolves pageId→pageName from the loaded linked-channels data.
  accountId: number
  pageId: number
  platform: Platform
  ok: boolean
  url?: string
  post_id?: string
  error?: string
  // Per-result publish state. Facebook-only: YouTube ignores the requested state
  // and always reports 'PUBLISHED' on success. Badge on THIS value, not the
  // value the client requested.
  state?: 'PUBLISHED' | 'DRAFT'
}

export interface PublishResponse {
  ok: boolean
  // True when at least one channel was published (DRAFT or PUBLISHED) — the API
  // emits this as a boolean, not a count.
  published: boolean
  results: PublishResult[]
}

// ---- Platform upload specs (Publishing reference panel) -----------------
// GET /api/platform-specs → PlatformSpecsResponse. Read-only reference of each
// platform's video upload rules so the owner knows the constraints before
// publishing. Two layers:
//   - The recommended short-form profile (aspectRatio/resolution/min+maxDurationS/
//     containers/vcodecs/acodecs): guidance only, NOT what the pipeline blocks on.
//   - The hard gate fields below (hardMax/MinDurationS, enforceAspect,
//     gatedContainers/Vcodecs, requireAudioAac): what the pre-upload validator
//     actually rejects (422) on. `enforced` is now true for all platforms (a
//     validator runs before upload), so it means "validated before publishing",
//     NOT that the recommended numbers are enforced.
// Nullable numerics mean "no fixed limit" / "not applicable".
export interface PlatformSpec {
  platform: Platform
  label: string
  containers: string[]       // accepted container formats, e.g. ['mp4', 'mov']
  aspectRatio: string        // human string, e.g. '9:16 (dọc)'
  resolution: string         // human string, e.g. '1080×1920'
  minDurationS: number | null
  maxDurationS: number | null
  maxFileMb: number | null
  vcodecs: string[]
  acodecs: string[]
  enforced: boolean
  notes: string | null
  // --- Pre-upload validator hard gates (additive; live response always sends
  // them, but kept optional so an older API can't break the panel). ---
  hardMaxDurationS?: number  // duration over this → rejected (422) at publish
  hardMinDurationS?: number  // duration at/under this → rejected
  enforceAspect?: boolean    // true ⇒ non ~9:16 rejected (facebook + instagram)
  gatedContainers?: string[] // containers actually accepted at the gate
  gatedVcodecs?: string[]    // video codecs accepted at the gate
  requireAudioAac?: boolean  // true ⇒ AAC audio required (facebook only)
  // Short/Mid/Long duration tiers for this platform (additive; optional so an
  // older API can't break the panel). Rendered as a "Phân tầng video" group.
  tiers?: {
    key: 'short' | 'mid' | 'long'
    label: string
    minDurationS: number | null
    maxDurationS: number | null
    note: string
  }[]
}

export interface PlatformSpecsResponse {
  specs: PlatformSpec[]
}

// The full dataset the dashboard renders (live from the API, or mock fallback).
export interface AppData {
  pages: Page[]
  accounts: PlatformAccount[]
  jobs: Job[]
  videos: Video[]
  analytics: Analytics
  org: Org
}
