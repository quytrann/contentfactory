import type { Analytics, Job, Org, Page, PipelineStage, PlatformAccount, Video } from './types'

// Fallback data shown when the API is unreachable.
// Intentionally EMPTY: no sample/dummy pages, accounts, jobs, videos, or
// metrics. Only the static pipeline diagram below is kept, since it describes
// the architecture itself rather than seeded content.

export const PIPELINE: PipelineStage[] = [
  { key: 'input', label: 'Nhập từ chat', tool: 'Telegram' },
  { key: 'orchestrate', label: 'Điều phối', tool: 'n8n' },
  { key: 'script', label: 'Kịch bản', tool: 'Claude Code' },
  { key: 'voice', label: 'Lồng tiếng', tool: 'VieNeu-TTS' },
  { key: 'timing', label: 'Timestamps', tool: 'faster-whisper' },
  { key: 'images', label: 'Hình ảnh', tool: 'ComfyUI + SDXL' },
  { key: 'assembly', label: 'Ghép video', tool: 'FFmpeg' },
  { key: 'publish', label: 'Đăng tải', tool: 'YouTube API' },
  { key: 'store', label: 'Lưu trữ', tool: 'PostgreSQL' },
]

export const PAGES: Page[] = []

export const ACCOUNTS: PlatformAccount[] = []

export const JOBS: Job[] = []

export const VIDEOS: Video[] = []

// ---- Analytics (empty fallback for the Overview charts) -----------------

export const ANALYTICS: Analytics = {
  kpis: [],
  viewsDaily: [],
  likesDaily: [],
  dayLabels: [],
  videosMonthly: [],
  platformSplit: [],
}

// ---- Org map (Dashboard -> Google account -> channels) ------------------

export const ORG: Org = {
  dashboard: 'Content Factory',
  accounts: [],
}
