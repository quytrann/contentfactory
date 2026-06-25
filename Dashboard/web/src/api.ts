// Thin client for the dashboard's write/generation endpoints (proxied to :4000).
// Reads still flow through DataProvider/useData; these are the mutations.

import type { AllLinkedChannelsResponse, Job, LinkedChannelsResponse, PageAnalytics, PlatformSpecsResponse, PublishResponse } from './types'

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
  skippedFiles: string[]
  remaining: number
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
  editMode: string | null     // 'commentary' | 'recap' | 'educational' | 'summary' | 'dubbed'
  sceneCount: number
  preview: string | null
  createdAt: string
}

// GET /api/videos/{videoId}/script → VideoScriptDetail. The full saved script for
// the "Xem trước" expansion. Footage scenes carry sourceStart/sourceEnd; image/
// stickman scenes carry image_prompt — so a script is mode-specific.
export interface VideoScriptScene {
  scene: number
  narration: string
  image_prompt?: string
  sourceStart?: number
  sourceEnd?: number
}
export interface VideoScriptDetail {
  videoId: number
  title: string | null
  renderMode: string | null
  sceneCount: number
  scenes: VideoScriptScene[]
}

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

  // Delete a finished video: removes the DB row + cleans up its files on disk.
  // 404 if missing. See DeleteResult for the success shape.
  deleteVideo: (id: number): Promise<DeleteResult> =>
    fetch(`/api/videos/${id}`, { method: 'DELETE' }).then(jsonOrThrow),

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
    body: { accountIds: number[]; state?: 'PUBLISHED' | 'DRAFT' },
  ): Promise<PublishResponse> =>
    fetch(`/api/videos/${id}/publish`, {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }).then(jsonOrThrow),

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
  getPlatformSpecs: (): Promise<PlatformSpecsResponse> =>
    fetch('/api/platform-specs').then(jsonOrThrow),

  // Per-page traffic analytics (platform split + monthly views) for the
  // PageDetail charts. Empty arrays = no data yet (charts render empty states).
  getPageAnalytics: (pageId: number): Promise<PageAnalytics> =>
    fetch(`/api/pages/${pageId}/analytics`).then(jsonOrThrow),

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
