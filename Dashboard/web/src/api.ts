// Thin client for the dashboard's write/generation endpoints (proxied to :4000).
// Reads still flow through DataProvider/useData; these are the mutations.

import type { AllLinkedChannelsResponse, BatchCreateBody, BatchCreateResponse, BatchPreviewResponse, Job, LinkedChannelsResponse, MarkPostedResponse, PageAnalytics, PlatformSpecsResponse, PublishPreflight, PublishResponse, RemoveFromPageResult, SystemStats, TagsRequest, TagsResponse } from './types'

// An Error that also carries the HTTP status and the parsed `detail` body, so
// callers (e.g. the publish modal) can branch on 400/404/409/422/429 and read a
// structured `detail.results` (per-platform errors on the all-failed 400 case).
export class ApiError extends Error {
  status: number
  detail: unknown
  constructor(message: string, status: number, detail: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

async function jsonOrThrow(r: Response) {
  if (!r.ok) {
    let message = `HTTP ${r.status}`
    let detail: unknown = null
    try {
      const body = await r.json()
      detail = body.detail ?? body
      message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? body)
    } catch {
      message = (await r.text().catch(() => '')) || message
    }
    throw new ApiError(message, r.status, detail)
  }
  return r.json()
}

const JSON_HEADERS = { 'Content-Type': 'application/json' }

// Shared result shape for the DELETE endpoints (jobs + videos). The backend
// removes the DB row AND cleans up files on disk, reporting which paths were
// removed/skipped plus how many rows remain.
export interface DeleteResult {
  deletedId: number
  removedFiles: string[]
  // Deliberately NOT deleted: a SURVIVING row still points at these (the normal case for
  // page-scoped per-scene audio a sibling video also uses). Not a problem, never retried.
  keptFiles?: string[]
  // The real failure: the OS refused the unlink because a process holds the file open.
  // Queued and retried on the next delete / at API startup.
  lockedFiles?: string[]
  // Union of keptFiles + lockedFiles. Kept for compatibility — do NOT show it as
  // "locked": doing so reported 91 shared audio files as locked when none were.
  skippedFiles: string[]
  remaining: number
  // Files the backend could NOT unlink (almost always still open — e.g. this very
  // dashboard is streaming the mp4 into a <video> preview). The DB row is gone but the
  // file is queued and retried on the next delete and at API startup. Surface it, or
  // the owner never learns the disk kept growing.
  pendingDeletes?: number
}

// DELETE /api/videos/{id} success shape (differs from DeleteResult, which fits
// job deletes). removedRendersDir/keptManifest/purgedRenderCacheFiles reflect the
// keepScript branch: keepScript=true keeps manifest.json (keptManifest=true) and
// lists the per-scene media it purged; keepScript=false removes the whole renders dir.
export interface VideoDeleteResult {
  ok: boolean
  id: number
  removedFiles: string[]
  // Deliberately NOT deleted: a SURVIVING row still points at these (the normal case for
  // page-scoped per-scene audio a sibling video also uses). Not a problem, never retried.
  keptFiles?: string[]
  // The real failure: the OS refused the unlink because a process holds the file open.
  // Queued and retried on the next delete / at API startup.
  lockedFiles?: string[]
  // Union of keptFiles + lockedFiles. Kept for compatibility — do NOT show it as
  // "locked": doing so reported 91 shared audio files as locked when none were.
  skippedFiles: string[]
  removedRendersDir: boolean
  keptManifest: boolean
  purgedRenderCacheFiles: string[]
  // See DeleteResult.pendingDeletes.
  pendingDeletes?: number
}

export interface NewJobBody {
  pageId: number
  link: string
  title?: string | null         // output video title; empty falls back to the source video's title
  voice?: string | null
  editMode?: string | null
  comment?: string | null
  sourceVideoId?: number | null
  aspect?: string | null          // 9:16 | 16:9 | 1:1 | 4:5
  targetSec?: number | null       // target OUTPUT length (whole source condensed into this)
  addCredit?: boolean             // append the source-credit slate?
  srcAudioVolume?: number         // 0 (off, default) | 0.05 | 0.10 | 0.15 — keep a little of the source's original audio under the VN voiceover
  renderModel?: string | null     // render/animation engine key (see RENDER_MODELS)
  voiceCloneModel?: string | null  // voice-clone engine key (see VOICE_CLONE_MODELS)
  // Script-gen LLM routing (option list from GET /api/llm/models). `llmModel` is
  // null for providers that have no model id of their own (e.g. claude-cli).
  // BOTH are OMITTED when the owner keeps the backend's default option, so a
  // default job's payload stays identical to the pre-feature one.
  llmProvider?: string | null
  llmModel?: string | null
  publish?: boolean               // auto-upload after the job finishes (default false = manual publish later)
  publishPlatform?: string | null // when publish=true, the platform to auto-upload to ('youtube'|'tiktok'|'instagram'|'facebook'); null = don't auto-publish
  // PART B (script cache reuse): when set, the backend SKIPS script-generation and
  // reuses the saved script from this previously-produced video. Script-gen-related
  // inputs (editMode, targetSec) are then ignored on the bypass path. null = normal generate.
  reuseScriptVideoId?: number | null
  // PART B (TTS cache control): only meaningful when reuseScriptVideoId is set.
  //   true  → force TTS to re-synthesize fresh audio (bypass the WAV cache). Use when
  //           the reused script text was edited so cached audio no longer matches.
  //   false / omit → let the TTS cache serve existing audio (cache HIT skips the GPU).
  bypassTtsCache?: boolean
  // Script cache control: only meaningful on jobs that RUN script-gen (i.e. NOT the
  // reuseScriptVideoId path, which skips script-gen entirely).
  //   true  → force script-gen to skip the disk-cache READ (fresh Claude headless
  //           call). The fresh result is still WRITTEN back to the cache.
  //   false / omit → let the script cache serve an existing result if present.
  bypassScriptCache?: boolean
  // Cover image (thumbnail) control. When useCover is true, the assembly step
  // uses the AI-generated cover (at coverImagePath) as the video's poster/
  // thumbnail instead of an extracted frame (not burned into the video stream).
  // coverImagePath is the disk path returned by POST /generate/cover.
  useCover?: boolean
  coverImagePath?: string | null
  // Final, edited copy-ready Facebook hashtag string (generated + tweaked in the
  // Studio). null/omit = no tags. Persisted with the produced video.
  facebookTags?: string | null
}

// GET /api/pages/{pageId}/reusable-scripts?link=<optional> → ReusableScript[].
// One row per previously-produced video whose script can be reused (skip script-gen).
// `link` narrows the list to scripts derived from the SAME source link. All text
// fields are nullable on legacy rows. `preview` is a short narration excerpt.
export interface ReusableScript {
  videoId: number
  title: string | null
  sourceLink: string | null
  sourceName: string | null
  renderMode: string | null   // 'footage' | 'image' | 'stickman' — the mode the script was authored for
  editMode: string | null     // 'commentary' | 'recap' | 'educational' | 'summary' | 'dubbed' | 'translate_full'
  sceneCount: number
  preview: string | null
  createdAt: string | null    // null for 'manifest'-source items (no DB row to date it)
  // Reuse-audio hints (backend contract):
  //   audioCached — a cached WAV set exists → reusing WITH audio is possible.
  //   audioStale  — the script text was edited AFTER the cache was made (a prior
  //                 session), so reusing WITH audio would serve mismatched audio.
  //   source      — 'db' (script lives in the DB) | 'manifest' (only a render
  //                 manifest on disk; these never have a reusable audio cache).
  audioCached: boolean
  audioStale: boolean
  source: 'db' | 'manifest'
}

// POST /generate/cover → CoverResult. Generates an AI cover/thumbnail image for a
// title. `url` is a ready-to-use /media?path=… src; `path` is the disk path to
// pass back as NewJobBody.coverImagePath when the owner opts to use the cover.
export interface CoverResult {
  path: string
  url: string
  seed: number
  styleIndex: number
  // Present ONLY on the generate-progress `result` (GET /generate/cover/progress):
  //   basePath — the CLEAN (title-less) image; the compositing base every
  //              renderCoverTitle call re-renders from (so it never stacks).
  //   viTitle  — the auto-translated Vietnamese title; prefilled into the editable
  //              title input so the owner can tweak it before applying.
  //   keyWords — key phrases the backend highlights when it fancy-styles the title.
  // Omitted on the renderCoverTitle response (which only echoes path/url).
  basePath?: string
  viTitle?: string
  keyWords?: string[]
}

// GET /api/videos/{videoId}/script → VideoScriptDetail. The full saved script for
// the "Xem trước" expansion. The response is a discriminated union on `kind`:
//   - kind === 'scenes' → a scene-array script (image/footage/stickman). Footage
//     scenes carry sourceStart/sourceEnd; image/stickman scenes carry image_prompt.
//   - kind === 'dubbed' → a Dubbed job's transcript, stored as timestamped VN
//     subtitles (no scene array). 404 still means genuinely no script.
export interface VideoScriptScene {
  scene: number
  narration: string
  image_prompt?: string
  sourceStart?: number
  sourceEnd?: number
}

// One dubbed subtitle line (kind === 'dubbed'). start/end are seconds from the
// source; text_vi is the Vietnamese dubbed line for that span.
export interface DubbedSub {
  start: number
  end: number
  text_vi: string
}

// Common fields on every script-detail response, regardless of kind.
interface VideoScriptBase {
  videoId: number
  title: string | null
  renderMode: string | null
  editMode: string | null
}

export interface VideoScriptScenes extends VideoScriptBase {
  kind: 'scenes'
  sceneCount: number
  scenes: VideoScriptScene[]
}

export interface VideoScriptDubbed extends VideoScriptBase {
  kind: 'dubbed'
  subCount: number
  subs: DubbedSub[]
}

// Discriminated union: narrow on `kind` before touching scenes/subs. A legacy API
// response that omits `kind` fails the `kind === 'dubbed'` check at runtime and is
// treated as the scenes branch (backward-safe).
export type VideoScriptDetail = VideoScriptScenes | VideoScriptDubbed

export interface LinkProbe {
  title: string | null
  durationS: number
  thumbnail: string | null
  channel: string | null
  handle: string | null
}

export interface PresetVoice {
  name: string
  description: string
  isDefault: boolean
}
export interface ClonedVoice {
  name: string
  path: string
  model?: string // engine that produced this clone; backend may set it later
}
export interface VoicesResponse {
  presets: PresetVoice[]
  cloned: ClonedVoice[]
}

// Body for POST /api/jobs/{jobId}/resume — one of two intents (BE contract §3):
//   - { skip: true }                  → user accepts shipping with NO credit.
//   - { skip: false, ...credit }      → user provides any subset of credit fields.
// `logo` is a path string only (path-only; no upload).
export type ResumeJobBody =
  | { skip: true }
  | {
      skip: false
      sourceName?: string
      sourceLink?: string
      handle?: string
      logo?: string
    }

export interface ResumeJobResult {
  ok: boolean
  jobId: number
  status: string // 're-queued' job → 'queued'
  creditDecision: 'provided' | 'skipped'
}

// ---- Script-gen LLM options (GET /api/llm/models) ---------------------------
// The providers/models the backend can route script generation to. NOTE: this is
// the one endpoint whose payload is snake_case (`is_default` / `generated_at`) —
// mirrored verbatim here rather than renamed, so the type matches the wire.
//
// Contract notes that matter to the UI:
//   - `claude-cli` is always present and is normally the `is_default` row.
//   - gemini/openrouter rows appear ONLY when the backend has a key for them, so a
//     one-option list is a valid response, not an error state.
//   - `reliability: 'low'` = experimental free-tier model that often fails to
//     finish within budget; the dropdown flags it so it can't be picked blindly.
export interface LlmModelOption {
  provider: string
  model: string | null
  label: string
  is_default: boolean
  reliability: 'high' | 'low'
  notes?: string | null
}
export interface LlmModelsResponse {
  options: LlmModelOption[]
  generated_at?: string
}

export interface NewPageBody {
  name: string
  language?: string
  accountEmail?: string
  platforms?: string[]
}

export const api = {
  createPage: (body: NewPageBody): Promise<{ id: number; name: string }> =>
    fetch('/api/pages', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }).then(jsonOrThrow),

  createJob: (body: NewJobBody): Promise<{ id: number; status: string }> =>
    fetch('/api/jobs', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }).then(jsonOrThrow),

  // Generate Facebook hashtags for a video title. Returns the individual tags plus
  // a copy-ready `text` (tags joined by spaces). The owner edits `text` in the
  // Studio and sends the final string back on create as NewJobBody.facebookTags.
  generateTags: (body: TagsRequest): Promise<TagsResponse> =>
    fetch('/generate/tags', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }).then(jsonOrThrow),

  // Batch "Add List" — side-effect-free preview: auto-translate each link's title
  // to Vietnamese so the owner can review/edit before creating jobs. Order matches
  // the input links; each row is either {originalTitle, viTitle} or {error}. Empty
  // links → 422; capped at 30 links → 422 (both with a Vietnamese detail).
  batchPreview: (links: string[]): Promise<BatchPreviewResponse> =>
    fetch('/api/jobs/batch/preview', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ links }) }).then(jsonOrThrow),

  // Batch "Add List" — create one queued job per item (runner processes them
  // sequentially). `items` carry the (possibly edited) VN title; the rest of the
  // body mirrors the single "Tạo video" create. Order-preserved results are each
  // {jobId} or {error}.
  batchCreateJobs: (body: BatchCreateBody): Promise<BatchCreateResponse> =>
    fetch('/api/jobs/batch', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }).then(jsonOrThrow),

  // Release all of a page's 'held' jobs (saved source-list rows) into the queue so
  // the runner starts processing them. Returns how many rows were flipped to
  // 'queued'. Called after the main "Tạo video" create so saved sources flush in
  // behind the freshly-created job.
  releaseJobs: (pageId: number): Promise<{ released: number }> =>
    fetch('/api/jobs/release', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ pageId }) }).then(jsonOrThrow),

  // Delete a finished video: removes the DB row + cleans up its files on disk.
  // 404 if missing. See VideoDeleteResult for the success shape. When
  // opts.keepScript is true, appends ?keepScript=true so the backend keeps the
  // manifest (and clears only media + audio) — the script stays reusable.
  deleteVideo: (id: number, opts?: { keepScript?: boolean }): Promise<VideoDeleteResult> =>
    fetch(`/api/videos/${id}${opts?.keepScript ? '?keepScript=true' : ''}`, { method: 'DELETE' }).then(jsonOrThrow),

  // DELETE /api/videos/{videoId}/posts?pageId={pageId} — remove a video from ONE
  // page's "Sản phẩm" block ONLY: deletes that page's posts rows. Does NOT delete
  // the video (it stays in the Video menu) and does NOT touch the real platform.
  // Returns { ok, removed }. 404 (no posts on that page) throws ApiError.
  removeVideoFromPage: (videoId: number, pageId: number): Promise<RemoveFromPageResult> =>
    fetch(`/api/videos/${videoId}/posts?pageId=${pageId}`, { method: 'DELETE' }).then(jsonOrThrow),

  // PATCH /api/videos/{id} — rename a video. Body { title } (null clears back to
  // the source-derived title). Returns { ok, videoId, title }.
  updateVideoTitle: (id: number, title: string | null): Promise<{ ok: boolean; videoId: number; title: string | null }> =>
    fetch(`/api/videos/${id}`, {
      method: 'PATCH',
      headers: JSON_HEADERS,
      body: JSON.stringify({ title }),
    }).then(jsonOrThrow),

  // POST /api/videos/{id}/cover — replace a produced video's cover/thumbnail with
  // a cover from the page's generated-cover cache. Body { path } is the cover's
  // disk path (from listCreatedCovers). Returns the new thumbUrl so the caller can
  // refresh the card. 404 if the video/cover is missing.
  setVideoCover: (id: number, path: string): Promise<{ ok: boolean; thumbUrl: string }> =>
    fetch(`/api/videos/${id}/cover`, {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ path }),
    }).then(jsonOrThrow),

  // POST /generate/cover — kick off ASYNC cover generation for a title. Pass a new
  // styleIndex (and seed=null) each call to vary the style. `summary` is a short
  // content synopsis so the image is on-topic; `prompt` (when set) is a manual
  // base prompt that overrides the auto title/summary prompt (style still varies
  // per click). Returns { taskId } immediately; poll getCoverProgress(taskId)
  // until status is 'done' (then read `result`) or 'error'.
  // tiltDeg pins the auto-baked title's tilt (null = backend's seeded auto minority);
  // the Studio form sends 0 by default so a fresh cover is flat unless the owner moves
  // the "Độ nghiêng" slider manually.
  generateCover: (body: { page: string; title: string; aspect: string; seed?: number | null; styleIndex?: number; summary?: string | null; prompt?: string | null; sourceLink?: string; tiltDeg?: number | null }): Promise<{ taskId: string }> =>
    fetch('/generate/cover', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }).then(jsonOrThrow),

  // GET /generate/cover/progress/{taskId} — poll a cover generation task.
  // `pct` is 0..100; `result` is the finished CoverResult only when status==='done';
  // `error` is set only when status==='error'. `prompt` is the actual assembled
  // prompt string sent to SDXL (manual verbatim, or auto title+summary+style);
  // null when not yet available.
  getCoverProgress: (taskId: string): Promise<{ status: 'running' | 'done' | 'error'; pct: number; msg: string; result: CoverResult | null; error: string | null; prompt: string | null }> =>
    fetch(`/generate/cover/progress/${encodeURIComponent(taskId)}`).then(jsonOrThrow),

  // POST /generate/cover/title — re-render the fancy Vietnamese title onto a clean
  // cover. Always composites from `basePath` (the title-less image), so re-sending
  // an edited `text` re-renders from the clean base (never stacks). The backend
  // owns ALL styling (position, color, gradient, plates) — the FE only supplies the
  // title text and the `keyWords` to highlight. SYNC: returns the finished
  // CoverResult ({ path, url }) directly. `keyWords` comes from the generate result.
  renderCoverTitle: (body: {
    page: string
    basePath: string
    text: string
    keyWords: string[]
    // Optional changing integer — each click sends a NEW seed so the backend
    // re-rolls a fresh style VARIATION (position/gradient/dominant-color shift).
    seed?: number
    // Optional manual overrides. Each knob left on "auto" (position) / null
    // (keyColor/fontScale/tiltDeg) lets the backend do its seeded/auto thing;
    // pinning a value overrides it.
    //   position — "auto" (default) or one of the 9 anchors (top/center/bottom
    //              × left/center/right).
    //   keyColor — "#RRGGBB" plate color (gradient START); null = auto dominant color.
    //   keyColor2 — "#RRGGBB" gradient END color; null = auto (or gradient off).
    //   gradient — fill the plate with a gradient (default true).
    //   strokeColor — "#RRGGBB" text BORDER (outline) color for every row; null = auto
    //              contrast pick. When set the backend skips its contrast guard, so the
    //              exact color is honored.
    //   align — "auto" (centered + seeded jitter) | "left" | "center" | "right";
    //              left/right flush every row to that edge of the title column.
    //   fontScale — ~0.2–1.5 block-height fraction; null = auto.
    //   tiltDeg  — title tilt in degrees (e.g. -20..20); null = auto seeded tilt.
    position?: string
    keyColor?: string | null
    keyColor2?: string | null
    gradient?: boolean
    strokeColor?: string | null
    align?: string
    fontScale?: number | null
    tiltDeg?: number | null
  }): Promise<CoverResult> =>
    fetch('/generate/cover/title', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }).then(jsonOrThrow),

  // GET /generate/cover/created?page=<page> — ALL generated covers in the page's
  // cache dir (newest-first). Empty covers[] = none generated yet (valid).
  listCreatedCovers: (page: string): Promise<{ covers: { path: string; url: string; filename: string; savedAt: string }[] }> =>
    fetch(`/generate/cover/created?page=${encodeURIComponent(page)}`).then(jsonOrThrow),

  // DELETE /generate/cover/created — remove ONE generated cover from the cache dir.
  // Idempotent; the backend only deletes cache-level covers (refuses the saved/
  // subdir and path traversal). Body carries the page + the cover's disk path.
  deleteCreatedCover: (body: { page: string; path: string }): Promise<{ ok: boolean }> =>
    fetch('/generate/cover/created', { method: 'DELETE', headers: JSON_HEADERS, body: JSON.stringify(body) }).then(jsonOrThrow),

  // Clear a video's saved script (set script = NULL). The video row and files
  // are kept; the video disappears from the reusable-scripts picker.
  clearVideoScript: (id: number): Promise<{ ok: boolean; id: number }> =>
    fetch(`/api/videos/${id}/script`, { method: 'DELETE' }).then(jsonOrThrow),

  // Delete a queued/finished job: removes the DB row + cleans up its files on
  // disk. 404 if missing, 409 if the job is currently running (cannot delete a
  // job mid-pipeline). See DeleteResult for the success shape.
  deleteJob: (id: number): Promise<DeleteResult> =>
    fetch(`/api/jobs/${id}`, { method: 'DELETE' }).then(jsonOrThrow),

  // Clear a failed job's error so the workflow stops showing it forever. The
  // backend nulls jobs.error (and progress_msg) and returns the updated row.
  // 404 if the job is missing.
  clearJobError: (id: number): Promise<{ id: number; error: string | null; status: string }> =>
    fetch(`/api/jobs/${id}/clear-error`, { method: 'POST' }).then(jsonOrThrow),

  // Stop a RUNNING job: marks it as failed immediately and signals the runner to
  // abort at its next step boundary. 404 if missing, 409 if not running.
  stopJob: (id: number): Promise<{ id: number; status: string }> =>
    fetch(`/api/jobs/${id}/stop`, { method: 'POST' }).then(jsonOrThrow),

  // Re-run a FAILED job: the backend enqueues a NEW job with the same inputs and
  // returns its id (plus reuseScriptVideoId when a cached script is reused, so
  // the new run can skip script-gen). 409 if the job is not in a failed state.
  retryJob: (jobId: number): Promise<{ newJobId: number; reuseScriptVideoId: number | null }> =>
    fetch(`/api/jobs/${jobId}/retry`, { method: 'POST' }).then(jsonOrThrow),

  // Resume a Dubbed job parked at status 'needs_input': either provide source
  // credit (skip:false + any subset of fields) or explicitly ship with no credit
  // (skip:true). Re-queues the SAME job (no re-translate / no claude -p). 404 if
  // the job is missing, 409 if it is not in needs_input. See BE contract §3.
  resumeJob: (jobId: number, body: ResumeJobBody): Promise<ResumeJobResult> =>
    fetch(`/api/jobs/${jobId}/resume`, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }).then(jsonOrThrow),

  // Convenience: only the jobs currently parked at 'needs_input' (same object
  // shape as in the bootstrap jobs list). Not required — the needsInput field
  // also rides on the normal jobs list — but handy for a dedicated queue/banner.
  getNeedsInputJobs: (): Promise<{ jobs: Job[] }> =>
    fetch('/api/jobs/needs-input').then(jsonOrThrow),

  // Channels already linked (token file exists) for a page — the publish modal's
  // tickbox source. Every returned channel is publishable; channels:[] means
  // none linked yet (valid). 404 if the page is missing.
  getLinkedChannels: (pageId: number): Promise<LinkedChannelsResponse> =>
    fetch(`/api/pages/${pageId}/linked-channels`).then(jsonOrThrow),

  // Connected channels across ALL pages, grouped by page — the many-to-many
  // publish modal's tickbox source. Every returned channel is publishable; an
  // empty pages[] (or all-empty channels[]) means nothing linked anywhere (valid).
  getAllLinkedChannels: (): Promise<AllLinkedChannelsResponse> =>
    fetch('/api/linked-channels').then(jsonOrThrow),

  // Publish a ready video to the chosen channels. `accountIds` are the
  // `accountId` values from getAllLinkedChannels (a channel can live on any
  // page — many-to-many). `state` (PUBLISHED|DRAFT) applies to Facebook Reels
  // only. Partial success is possible — inspect results[] (each carries
  // accountId+pageId). An all-failed run throws an ApiError(status 400) whose
  // detail.results holds the per-channel errors; 404 (no video) / 409 (already
  // published) / 422 (bad input) / 429 (FB rate limit) throw ApiError with the
  // matching status.
  publishVideo: (
    id: number,
    body: {
      accountIds: number[]
      state?: 'PUBLISHED' | 'SCHEDULED' | 'DRAFT'
      // Unix seconds; required when state === 'SCHEDULED' (Facebook only).
      scheduledPublishTime?: number
      // Caption BODY verbatim (WITHOUT the "Nguồn:" credit line). null/omit → the
      // backend uses its default. Applies to all targeted platforms.
      description?: string | null
      // Append the source credit server-side (default true). Set false to omit it.
      includeSource?: boolean
    },
  ): Promise<PublishResponse> =>
    fetch(`/api/videos/${id}/publish`, {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(jsonOrThrow),

  // Per-platform publish preflight for the modal's platform columns: how each
  // platform will treat THIS video (Facebook Reel vs post, YouTube Short vs
  // video), from a path-only ffprobe on the server. 404 (no video) / 422 (file
  // missing on disk) throw ApiError.
  getPublishPreflight: (id: number): Promise<PublishPreflight> =>
    fetch(`/api/videos/${id}/publish-preflight`).then(jsonOrThrow),

  // Live upload progress for a video's in-flight publish to ONE platform (feed
  // uploads take minutes). Progress is keyed per {videoId:platform}; currently
  // only facebook is tracked. `active:false` = no entry / evicted (finished > ~30s
  // ago). While active: phase (start|transfer|finish|done|error) + pct 0..100 +
  // bytesSent/bytesTotal. Best-effort — poll while a column is publishing.
  getPublishProgress: (
    videoId: number,
    platform: string,
  ): Promise<{ active: boolean; phase?: string; pct?: number; bytesSent?: number; bytesTotal?: number }> =>
    fetch(`/api/videos/${videoId}/publish-progress?platform=${encodeURIComponent(platform)}`).then(jsonOrThrow),

  // Clone a finished video at a DIFFERENT aspect ratio. The backend enqueues a
  // fast re-assemble of the SAME content (reuses cached script/audio/images — no
  // GPU/Claude) and returns the new video id plus the job id (jobId may be null
  // if no async job was needed). `aspect` is "16:9" | "9:16". 404 (video/cached
  // content missing) / 409 (already that aspect, nothing to do) / 422 (bad
  // aspect) throw ApiError with the matching status + message.
  cloneVideo: (id: number, aspect: string): Promise<{ ok: boolean; videoId: number; jobId: number | null }> =>
    fetch(`/api/videos/${id}/clone`, {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ aspect }),
    }).then(jsonOrThrow),

  // Per-platform video upload requirements (format/aspect/resolution/duration/
  // size/codecs) for the Publishing reference panel. Read-only reference data.
  // GET /api/llm/models — script-gen LLM options (provider + model). The backend
  // caches the list server-side (~6h), so the Studio just fetches it on mount; no
  // client-side cache. See LlmModelsResponse for the shape.
  getLlmModels: (): Promise<LlmModelsResponse> =>
    fetch('/api/llm/models').then(jsonOrThrow),

  getPlatformSpecs: (): Promise<PlatformSpecsResponse> =>
    fetch('/api/platform-specs').then(jsonOrThrow),

  // Per-page traffic analytics (platform split + monthly views + follower counts)
  // for the PageDetail charts. Empty arrays = no data yet (charts render empty
  // states); followers/fanCount are null when the count is unavailable.
  getPageAnalytics: (pageId: number): Promise<PageAnalytics> =>
    fetch(`/api/pages/${pageId}/analytics`).then(jsonOrThrow),

  // GET /api/system — live resource footprint + feature flags. The dashboard reads
  // `apiUploadEnabled` to gate every publish (Đăng) affordance.
  getSystem: (): Promise<SystemStats> =>
    fetch('/api/system').then(jsonOrThrow),

  // POST /api/videos/mark-posted — mark videos as MANUALLY posted (uploaded by hand)
  // to the given platform on each video's OWN page. One result per video; on success
  // the video's postedPlatforms gains the platform after a data refresh. Facebook is
  // the only supported platform this round.
  markPosted: (videoIds: number[], platform: 'facebook'): Promise<MarkPostedResponse> =>
    fetch('/api/videos/mark-posted', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ videoIds, platform }),
    }).then(jsonOrThrow),

  // PART B: previously-produced videos whose script can be REUSED for this page
  // (skip script-gen). Pass `link` to narrow to scripts derived from the same
  // source. Empty array = nothing reusable yet (valid). 404 if the page is missing.
  getReusableScripts: (pageId: number, link?: string): Promise<ReusableScript[]> =>
    fetch(
      `/api/pages/${pageId}/reusable-scripts${link ? `?link=${encodeURIComponent(link)}` : ''}`,
    ).then(jsonOrThrow),

  // PART B: the full saved script for a video — drives the "Xem trước" expansion.
  // 404 if the video / its script is missing.
  getVideoScript: (videoId: number): Promise<VideoScriptDetail> =>
    fetch(`/api/videos/${videoId}/script`).then(jsonOrThrow),

  // DELETE /api/videos/{videoId}/audio — clear cached WAVs so TTS regenerates fresh recordings.
  // Keeps manifest.json + visual files. 404 if no cache (treat as already gone).
  deleteVideoAudio: (videoId: number): Promise<{ deleted: string[]; count: number }> =>
    fetch(`/api/videos/${videoId}/audio`, { method: 'DELETE' }).then(jsonOrThrow),

  // PATCH /api/videos/{videoId}/script/scene/{sceneNum} — update one scene's narration
  // in both the DB script and the render cache manifest.
  updateSceneNarration: (videoId: number, sceneNum: number, narration: string): Promise<{ videoId: number; scene: number; narration: string }> =>
    fetch(`/api/videos/${videoId}/script/scene/${sceneNum}`, {
      method: 'PATCH',
      headers: JSON_HEADERS,
      body: JSON.stringify({ narration }),
    }).then(jsonOrThrow),

  // Lightweight metadata for the paste-link preview (no download).
  probeLink: (link: string): Promise<LinkProbe> =>
    fetch('/generate/probe_link', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ link }) }).then(jsonOrThrow),

  listVoices: (page: string): Promise<VoicesResponse> =>
    fetch(`/generate/voices?page=${encodeURIComponent(page)}`).then(jsonOrThrow),

  // `model` is the clone engine key (e.g. "f5-tts"). The backend bakes a
  // ` - <ShortName>` suffix into the saved voice name and returns the final
  // name in `name`, so the frontend must NOT append any model suffix itself.
  uploadVoice: (page: string, name: string, file: File, model: string): Promise<ClonedVoice> => {
    const fd = new FormData()
    fd.append('page', page)
    fd.append('name', name)
    fd.append('file', file)
    fd.append('model', model)
    return fetch('/generate/voice', { method: 'POST', body: fd }).then(jsonOrThrow)
  },

  // Delete a cloned voice file from disk so it can be re-cloned. `name` is the
  // clone's filename-without-ext (the ClonedVoice.name, e.g. "Host nam - F5-TTS").
  deleteVoice: (page: string, name: string): Promise<{ deleted: boolean; name: string }> =>
    fetch(`/generate/voice?page=${encodeURIComponent(page)}&name=${encodeURIComponent(name)}`, { method: 'DELETE' }).then(jsonOrThrow),

  previewVoice: (body: { page: string; voice?: string | null; refAudio?: string | null; text?: string }): Promise<{ audioPath: string; url: string; cached: boolean }> =>
    fetch('/generate/voice/preview', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }).then(jsonOrThrow),

  uploadImage: (page: string, file: File): Promise<{ path: string; url: string }> => {
    const fd = new FormData()
    fd.append('page', page)
    fd.append('file', file)
    return fetch('/generate/image', { method: 'POST', body: fd }).then(jsonOrThrow)
  },

  // POST /api/shutdown — kill the whole local stack (API, ComfyUI, vite, workers).
  // The backend responds { ok: true } immediately, then detaches a killer process,
  // so the server dies ~1.5s later. This fetch may reject with a network error if
  // the process is torn down before the response is read — callers must treat a
  // post-fire rejection as EXPECTED (the shutdown still happened).
  shutdownProject: (): Promise<{ ok: boolean }> =>
    fetch('/api/shutdown', { method: 'POST' }).then(jsonOrThrow),

  makeVideo: (body: {
    page: string
    title: string
    voice?: string | null
    refAudio?: string | null
    scenes: { scene: number; caption: string; imagePath: string }[]
    temperature?: number
    repetitionPenalty?: number
    maxNewFrames?: number
  }): Promise<{ videoPath: string; url: string; durationS: number; scenes: number }> =>
    fetch('/generate/video', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }).then(jsonOrThrow),
}
