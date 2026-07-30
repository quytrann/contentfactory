import { useEffect, useMemo, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import {
  AlignCenter, AlignLeft, AlignRight,
  Check, CheckCircle2, ChevronDown, ChevronRight as ChevronRightIcon, ChevronUp, Clock, FileText, Film, FolderOpen, Image as ImageIcon, Link2, ListPlus, Loader2, Mic,
  Pencil, Play, Plus, RefreshCw, RotateCcw, Save, Search, SlidersHorizontal, Square, Star, Trash2, Type, Wand2, X,
} from 'lucide-react'
import { useData, useRefresh, useStopJob } from '../data'
import { ApiError, api } from '../api'
import type { ClonedVoice, CoverResult, LlmModelOption, ReusableScript, VideoScriptDetail, VoicesResponse } from '../api'
import type { BatchCreateBody, PlatformSpec } from '../types'
import {
  Button, Card, EmptyState, Field, Modal, Pill, SectionTitle,
  Select, TextInput, fmtClock, getDefaultPref, setDefaultPref, clearDefaultPref, useToast,
} from '../ui'
// Reuse the SAME option-chip primitive + key→Vietnamese-label maps the Videos
// view uses, so the Workflow's "selected options" row reads identically to the
// chips on a finished video card (single source of truth, no label drift).
import { EDIT_MODE_LABEL, OptionChip, RENDER_MODEL_LABEL, VOICE_CLONE_MODEL_LABEL } from './Videos'
// Shared saved-cover browser, reused by the Videos list "Đổi cover" action.
import { SavedCoverPicker } from '../components/SavedCoverPicker'

// Three editing modes from `how to edit video.md`. The owner MUST pick one
// before a workflow runs (CLAUDE.md pre-workflow rule).
const EDIT_MODES = [
  { value: 'summary', label: 'Summary', desc: 'Rút gọn video gốc còn các ý chính theo đúng trình tự; lời kể của bạn, footage gốc chỉ minh hoạ. Xem "how to edit video.md".' },
  { value: 'commentary', label: 'Commentary', desc: 'Dịch + phân tích & đưa quan điểm; footage gốc ≤ 20–40%.' },
  { value: 'recap', label: 'Recap', desc: 'Tóm tắt & kể lại có chọn lọc; sắp xếp lại, thêm giải thích.' },
  { value: 'educational', label: 'Giáo dục / Education', desc: 'Biến nội dung thành bài học / how-to / giải thích.' },
  { value: 'dubbed', label: 'Lồng phụ đề (Dubbed)', desc: 'Giữ nguyên hình + tiếng gốc, chỉ cắt phần thừa, burn phụ đề tiếng Việt; KHÔNG TTS. Cảnh báo: rủi ro bản quyền cao — owner đã chấp nhận.' },
  { value: 'translate_full', label: 'Dịch đầy đủ (voice + phụ đề)', desc: 'Dịch nguyên bản toàn bộ nội dung sang giọng đọc tiếng Việt, giữ nguyên video gốc, che phụ đề gốc và chèn phụ đề tiếng Việt.' },
]

// Render/animation engines available in the project (model-choices.md). The
// Studio "Tạo video" form offers all of them in one dropdown; the Vietnamese
// description of the picked model shows right below the select.
const RENDER_MODELS = [
  // — Giữ nguyên hình ảnh video gốc (không sinh ảnh) —
  {
    value: 'passthrough-trim',
    group: 'Video gốc (giữ hình)',
    label: 'Giữ nguyên video gốc — chỉ cắt',
    desc: 'Dùng chính hình ảnh của video nguồn, chỉ cắt bỏ đoạn thừa cho khớp Độ dài video đích. Không sinh ảnh AI, không Ken Burns.',
  },
  // — Ảnh AI tĩnh + Ken Burns (kiến trúc story_voiceover / translate) —
  {
    value: 'sdxl-base',
    group: 'Ảnh AI + Ken Burns',
    label: 'SDXL base 1.0 — đang dùng',
    desc: 'Sinh ảnh tĩnh bằng SDXL base 1.0 rồi lia/zoom (Ken Burns) + caption. Vừa 8GB VRAM. Mặc định hiện tại.',
  },
  {
    value: 'juggernaut-xl',
    group: 'Ảnh AI + Ken Burns',
    label: 'Juggernaut XL',
    desc: 'Fine-tune SDXL, ảnh chân thực & chi tiết hơn base. Vẫn vừa 8GB VRAM.',
  },
  {
    value: 'realvisxl',
    group: 'Ảnh AI + Ken Burns',
    label: 'RealVisXL',
    desc: 'Chuyên ảnh thực (người/cảnh) độ chân thực cao. Vừa 8GB VRAM.',
  },
  {
    value: 'dreamshaper-xl',
    group: 'Ảnh AI + Ken Burns',
    label: 'DreamShaper XL',
    desc: 'Đa phong cách (tranh / anime / concept art). Vừa 8GB VRAM.',
  },
  {
    value: 'sdxl-turbo',
    group: 'Ảnh AI + Ken Burns',
    label: 'SDXL-Turbo',
    desc: 'Ít step, sinh ảnh rất nhanh; chất lượng thấp hơn base. Hợp khi cần tốc độ.',
  },
  {
    value: 'sdxl-lightning',
    group: 'Ảnh AI + Ken Burns',
    label: 'SDXL-Lightning',
    desc: 'Sinh ảnh 4–8 step, nhanh, cân bằng tốc độ/chất lượng tốt hơn Turbo.',
  },
  {
    value: 'sd35-medium',
    group: 'Ảnh AI + Ken Burns',
    label: 'SD3.5 Medium',
    desc: 'Model mới hơn, prompt sát hơn — nhưng cần kiểm tra VRAM, có thể chật 8GB.',
  },
  // — Stickman animation (kiến trúc mới) —
  {
    value: 'stickman-procedural',
    group: 'Stickman animation',
    label: 'Stickman — Procedural 2D',
    desc: 'Rig xương + keyframe tư thế, render từng frame bằng code rồi ghép FFmpeg. Chạy CPU, ~0 VRAM, nét que sạch, nhanh, miễn phí. Khuyến nghị cho stickman.',
  },
  {
    value: 'stickman-blender',
    group: 'Stickman animation',
    label: 'Stickman — Blender (headless)',
    desc: 'Stickman rig + Python keyframe, render headless qua GPU. Đẹp/3D hơn nhưng nặng và lâu dựng hơn Procedural.',
  },
]

// Render engines actually present/usable on this machine right now. The rest show
// "— chưa cài" in the dropdown and block job creation until installed + wired.
const INSTALLED_RENDER_MODELS = new Set(['passthrough-trim', 'sdxl-base', 'juggernaut-xl', 'stickman-blender', 'stickman-procedural'])

// PART B (script reuse): collapse a render-model key into the high-level script
// "mode" so a reused script can be cross-checked against the form's current model.
// A script authored for one mode carries mode-specific fields (footage →
// sourceStart/sourceEnd; image/stickman → image_prompt), so reusing across the
// footage↔image/stickman boundary can produce a broken render. The backend uses
// the SAME mapping; keep them in sync.
//   'passthrough-trim'  → 'footage'
//   starts-with 'stickman' → 'stickman'
//   otherwise (SDXL keys) → 'image'
function renderModelToMode(renderModel: string): 'footage' | 'stickman' | 'image' {
  if (renderModel === 'passthrough-trim') return 'footage'
  if (renderModel.startsWith('stickman')) return 'stickman'
  return 'image'
}

// Voice-clone engines available in the project. Shown in a dropdown next to the
// render-model one; the picked model's Vietnamese description shows below it.
// Labels carry a "- chưa cài" suffix for engines not yet installed on the machine.
const VOICE_CLONE_MODELS = [
  {
    value: 'f5-tts',
    installed: true,
    short: 'F5-TTS',
    label: 'F5-TTS',
    desc: 'Clone chất lượng cao, zero-shot. Đã cài (cf-venv, GPU) + checkpoint tiếng Việt ViVoice 1000h. Mặc định.',
  },
  {
    value: 'vieneu',
    installed: true,
    short: 'VieNeu',
    label: 'VieNeu-TTS',
    desc: 'TTS tiếng Việt, clone zero-shot từ 1 đoạn mẫu ngắn. Đã cài (cf-venv), nhẹ.',
  },
  {
    // NOTE: `short` MUST stay "OmniVoice" — the backend bakes it into cloned-voice
    // names ("<name> - OmniVoice"), and BAKED_MODEL_SUFFIX is derived from this.
    value: 'omnivoice',
    installed: true,
    short: 'OmniVoice',
    label: 'OmniVoice (đa ngôn ngữ, clone)',
    desc: 'Clone đa ngôn ngữ (gồm tiếng Việt) từ đoạn mẫu ngắn. Đã cài (cf-venv, GPU).',
  },
  {
    value: 'xtts-v2',
    installed: false,
    short: 'XTTS-v2',
    label: 'Coqui XTTS-v2 - chưa cài',
    desc: 'Clone đa ngôn ngữ (gồm tiếng Việt) chỉ từ ~6 giây mẫu. Tự nhiên, cần GPU. CHƯA CÀI.',
  },
  {
    value: 'openvoice-v2',
    installed: false,
    short: 'OpenVoice v2',
    label: 'OpenVoice v2 - chưa cài',
    desc: 'Clone giọng + điều khiển tông/cảm xúc/nhịp. Đa ngôn ngữ. CHƯA CÀI.',
  },
  {
    value: 'gpt-sovits',
    installed: false,
    short: 'GPT-SoVITS',
    label: 'GPT-SoVITS - chưa cài',
    desc: 'Clone rất giống chỉ với mẫu ngắn, hỗ trợ tiếng Việt; fine-tune nhanh. Cần GPU. CHƯA CÀI.',
  },
]

// Script-gen LLM ("Model AI viết kịch bản"). Unlike RENDER_MODELS /
// VOICE_CLONE_MODELS there is NO static list here: the offered providers depend on
// which API keys the backend has, so the options come from GET /api/llm/models at
// runtime. What lives here is only the packing of a {provider, model} pair into
// the single string value the shared <Select> primitive (and its localStorage
// "set as default" ★) works with. `model` is null for claude-cli, so the encoded
// key for it is simply "claude-cli|".
const LLM_KEY_SEP = '|'

// settingKey for the ★-pinned default LLM (`cf.default.studio.llmModel`). A const
// because BOTH the <Select> and the post-create reset read it — an inline string in
// two places is exactly how those two drift apart.
const LLM_SETTING_KEY = 'studio.llmModel'

function llmOptionKey(o: { provider: string; model: string | null }): string {
  return `${o.provider}${LLM_KEY_SEP}${o.model ?? ''}`
}

// Output aspect ratios offered in the Studio.
const ASPECT_OPTIONS = [
  { value: '9:16', label: '9:16 — Dọc (Shorts/Reels/TikTok)' },
  { value: '16:9', label: '16:9 — Ngang (YouTube)' },
  { value: '1:1', label: '1:1 — Vuông' },
  { value: '4:5', label: '4:5 — Dọc (Feed)' },
]

// Original/source-audio volume kept under the Vietnamese voiceover. Default OFF
// (voiceover only); the owner may keep a faint bed of the source's audio.
const SRC_AUDIO_OPTIONS = [
  { value: 0, label: 'Tắt — chỉ lồng tiếng' },
  { value: 0.05, label: 'Giữ 5%' },
  { value: 0.1, label: 'Giữ 10%' },
  { value: 0.15, label: 'Giữ 15%' },
]

// Target OUTPUT length is chosen via minutes+seconds inputs (1s–50min); stored as
// seconds on the job. The whole source is condensed into this length.
const MAX_TARGET_SEC = 50 * 60
const MIN_TARGET_SEC = 1

// Canonical pipeline steps for the Workflow progress diagram (footage mode).
// Keys match runner.py's progress_step values.
// `band: [lo, hi]` is each step's slice of the global progressPct, used to
// derive a per-chip percentage from the single global pct (footage pipeline).
const WORKFLOW_STEPS: { key: string; label: string; band: [number, number] }[] = [
  { key: 'ingest', label: 'Tải & bóc lời', band: [5, 25] },
  { key: 'script', label: 'Kịch bản', band: [25, 40] },
  { key: 'cut', label: 'Cắt cảnh', band: [40, 55] },
  { key: 'voice', label: 'Lồng tiếng', band: [55, 70] },
  { key: 'footage', label: 'Tải/Tạo hình', band: [70, 85] },
  { key: 'render', label: 'Dựng video', band: [85, 95] },
]

// Dubbed edit mode runs a DIFFERENT backend pipeline than the normal footage one:
// there is NO TTS, NO narration-script generation, and NO image/footage creation.
// Dubbed = download source + translate its subtitles to Vietnamese, then cut filler
// and concat the original clips while KEEPING the original audio and burning the VN
// subtitle in. So the chip set is intentionally smaller and worded differently from
// WORKFLOW_STEPS. Keys match the dubbed runner's progress_step values
// (ingest → script → render → publish → done, with an optional needs_input pause
// between script and render). `band: [lo, hi]` slices the global progressPct per chip.
const DUBBED_WORKFLOW_STEPS: { key: string; label: string; band: [number, number] }[] = [
  { key: 'ingest', label: 'Tải & bóc lời', band: [5, 25] },
  { key: 'script', label: 'Dịch phụ đề', band: [25, 40] },
  { key: 'render', label: 'Cắt & ghép giữ tiếng gốc', band: [40, 99] },
]

// Short display labels for platform buttons — just the platform name, no content-type qualifier.
const PLATFORM_SHORT: Record<string, string> = {
  youtube: 'YouTube',
  facebook: 'Facebook',
  tiktok: 'TikTok',
  instagram: 'Instagram',
}

const PLATFORM_STYLE: Record<string, { selected: string; idle: string; logo: React.ReactNode }> = {
  youtube: {
    selected: 'border-red-500/50 bg-red-500/10 text-red-600 dark:text-red-400',
    idle: 'border-line bg-panel text-fg hover:border-red-400/40',
    logo: (
      <svg viewBox="0 0 24 24" className="h-4 w-4 shrink-0 fill-current" aria-hidden>
        <path d="M23.5 6.19a3.02 3.02 0 0 0-2.12-2.14C19.54 3.5 12 3.5 12 3.5s-7.54 0-9.38.55A3.02 3.02 0 0 0 .5 6.19C0 8.03 0 12 0 12s0 3.97.5 5.81a3.02 3.02 0 0 0 2.12 2.14C4.46 20.5 12 20.5 12 20.5s7.54 0 9.38-.55a3.02 3.02 0 0 0 2.12-2.14C24 15.97 24 12 24 12s0-3.97-.5-5.81zM9.75 15.5v-7l6.5 3.5-6.5 3.5z" />
      </svg>
    ),
  },
  facebook: {
    selected: 'border-blue-500/50 bg-blue-500/10 text-blue-600 dark:text-blue-400',
    idle: 'border-line bg-panel text-fg hover:border-blue-400/40',
    logo: (
      <svg viewBox="0 0 24 24" className="h-4 w-4 shrink-0 fill-current" aria-hidden>
        <path d="M24 12.07C24 5.41 18.63 0 12 0S0 5.41 0 12.07C0 18.1 4.39 23.1 10.13 24v-8.44H7.08v-3.49h3.04V9.41c0-3.02 1.8-4.7 4.54-4.7 1.31 0 2.68.24 2.68.24v2.97h-1.51c-1.49 0-1.95.93-1.95 1.87v2.27h3.32l-.53 3.49h-2.79V24C19.61 23.1 24 18.1 24 12.07z" />
      </svg>
    ),
  },
  tiktok: {
    selected: 'border-neutral-500/40 bg-neutral-500/10 text-fg',
    idle: 'border-line bg-panel text-fg hover:border-neutral-400/40',
    logo: (
      <svg viewBox="0 0 24 24" className="h-4 w-4 shrink-0 fill-current" aria-hidden>
        <path d="M19.59 6.69a4.83 4.83 0 0 1-3.77-4.25V2h-3.45v13.67a2.89 2.89 0 0 1-2.88 2.5 2.89 2.89 0 0 1-2.89-2.89 2.89 2.89 0 0 1 2.89-2.89c.28 0 .54.04.79.1V9.01a6.34 6.34 0 0 0-.79-.05 6.34 6.34 0 0 0-6.34 6.34 6.34 6.34 0 0 0 6.34 6.34 6.34 6.34 0 0 0 6.33-6.34V8.69a8.18 8.18 0 0 0 4.78 1.52V6.74a4.84 4.84 0 0 1-1.01-.05z" />
      </svg>
    ),
  },
  instagram: {
    selected: 'border-pink-500/50 bg-pink-500/10 text-pink-600 dark:text-pink-400',
    idle: 'border-line bg-panel text-fg hover:border-pink-400/40',
    logo: (
      <svg viewBox="0 0 24 24" className="h-4 w-4 shrink-0 fill-current" aria-hidden>
        <path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zM12 0C8.741 0 8.333.014 7.053.072 2.695.272.273 2.69.073 7.052.014 8.333 0 8.741 0 12c0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98C8.333 23.986 8.741 24 12 24c3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98C15.668.014 15.259 0 12 0zm0 5.838a6.162 6.162 0 1 0 0 12.324 6.162 6.162 0 0 0 0-12.324zM12 16a4 4 0 1 1 0-8 4 4 0 0 1 0 8zm6.406-11.845a1.44 1.44 0 1 0 0 2.881 1.44 1.44 0 0 0 0-2.881z" />
      </svg>
    ),
  },
}

// Default values for the Studio "Tạo video" per-job creation fields. These are the
// initial state when there is no cached draft, and the values the form resets to
// after a job is successfully created.
const STUDIO_DEFAULTS = {
  link: '',
  title: '',
  editMode: 'summary',
  renderModel: 'passthrough-trim',
  voiceCloneModel: 'f5-tts',
  // Script-gen LLM, as an encoded "<provider>|<model>" key (see llmOptionKey).
  // Deliberately EMPTY: the default is whatever GET /api/llm/models reports as
  // is_default, resolved once the list arrives — never hardcoded here, so the
  // backend can move its default without the form fighting it.
  llmKey: '',
  aspect: '16:9',
  targetSec: 300,
  autoDuration: true,
  addCredit: false,
  srcAudioVolume: 0,
  // Cover draft fields (persist across refresh; cleared when the job completes).
  // `cover` is the generated CoverResult (its `url` is a /media?path=… src that
  // reloads the on-disk file after refresh); null = none generated yet.
  cover: null as CoverResult | null,
  useCover: false,
  coverPrompt: '',
  coverStyleIndex: 0,
  // Cover title fields. `coverBasePath` is the CLEAN (title-less) cover path used as
  // the compositing base for every renderCoverTitle call, so re-applying an edited
  // title never stacks. `coverText` is the (editable) Vietnamese title, prefilled
  // from the generate result's `viTitle`. `coverKeyWords` are the phrases the
  // backend highlights when it fancy-styles the title (from the generate result).
  coverBasePath: null as string | null,
  coverText: '',
  coverKeyWords: [] as string[],
  // Manual title-style knobs. Most default to "Auto" (the backend does its
  // seeded/auto thing); pinning a knob overrides it. Position "auto" = auto anchor;
  // the *Auto booleans send null for the color/font/tilt values while on.
  // EXCEPTION (owner request 2026-08-20): position / align / tilt are PINNED by
  // default so a first-time "Tạo Cover" always lands middle-center, centered text,
  // 0° tilt instead of the backend's seeded anchor/jitter. Re-generating an existing
  // cover still preserves whatever the owner moved them to (makeCover never resets
  // these) — the defaults only apply to a fresh form / after a job is submitted.
  coverPosition: 'center',
  coverAlign: 'center',
  coverKeyColor: '#FF6600',
  coverKeyColor2: '#0B3866',
  coverKeyColorAuto: true,
  coverGradient: true,
  // Text BORDER (outline) color. Auto = backend picks a contrasting outline.
  coverStrokeColor: '#000000',
  coverStrokeAuto: true,
  coverFontScale: 0.5,
  coverFontAuto: true,
  // tiltAuto=false + tilt=0 → the FE sends an explicit tiltDeg:0, which the backend
  // honors as "flat" (`if ov_tilt is not None`). Leaving tiltAuto ON would instead
  // let the seeded minority tilt the title, which is what the owner asked to stop.
  coverTilt: 0,
  coverTiltAuto: false,
  // In-flight cover-generation task id. Persisted so the percent poll can RESUME
  // after a page refresh (backend keeps the task ~600s). null = no task running.
  coverTaskId: null as string | null,
  createdJobId: null as number | null,
} as const

// The 9 title anchors (backend names), in reading order so a `grid-cols-3` lays
// them out spatially (row 1 = top … row 3 = bottom; col 1 = left … col 3 = right).
// The middle row is "center-left"/"center"/"center-right" (matches the API contract).
const COVER_TITLE_ANCHORS = [
  'top-left', 'top-center', 'top-right',
  'center-left', 'center', 'center-right',
  'bottom-left', 'bottom-center', 'bottom-right',
] as const

// localStorage key for the in-progress Studio draft. While the owner is filling in
// the form (before submitting) the fields are cached here so navigating away and
// back, or refreshing, does not lose what they typed. A successful create clears it.
const STUDIO_DRAFT_KEY = 'cf-studio'

// Marker for the one-time cover title position/align/tilt default migration
// (see loadStudioDraft). Presence = the saved draft has already been migrated.
const COVER_TITLE_DEFAULTS_MIGRATION_KEY = 'cf-studio.coverTitleDefaults.v2'

// settingKey for the pinned default VOICE (a voiceKey like `clone:<name>`), stored
// under `cf.default.studio.voice`. Shared by the VoicePicker's ★ affordance and the
// fresh-form auto-select effect so the key can never drift between the two.
const VOICE_DEFAULT_SETTING_KEY = 'studio.voice'

// Persisted PER-VIDEO keep-script preference (shared with Videos.tsx): the set of
// video ids whose saved script should be KEPT (media + audio still deleted) when
// the video is removed from the history. Stored as a JSON array of ids under
// `cf.keepScriptIds`. Exported helpers so both the reuse modal (which toggles a
// row's flag) and Videos.tsx (which reads it on delete) hit the same key with no
// drift.
export const KEEP_SCRIPT_IDS_KEY = 'cf.keepScriptIds'

export function getKeepScriptIds(): Set<number> {
  try {
    const raw = localStorage.getItem(KEEP_SCRIPT_IDS_KEY)
    if (!raw) return new Set()
    const arr = JSON.parse(raw)
    return Array.isArray(arr) ? new Set(arr.filter((x): x is number => typeof x === 'number')) : new Set()
  } catch {
    return new Set()
  }
}

export function isKeepScript(id: number): boolean {
  return getKeepScriptIds().has(id)
}

export function setKeepScript(id: number, keep: boolean): void {
  try {
    const ids = getKeepScriptIds()
    if (keep) ids.add(id)
    else ids.delete(id)
    localStorage.setItem(KEEP_SCRIPT_IDS_KEY, JSON.stringify([...ids]))
  } catch {
    /* storage unavailable (private mode/quota) — best-effort */
  }
}

// Shape of the persisted draft — exactly the per-job fields the form owns. Mirrors
// STUDIO_DEFAULTS so the typed useState generics line up (STUDIO_DEFAULTS is `as const`).
type StudioDraft = {
  link: string
  title: string
  editMode: string
  renderModel: string
  voiceCloneModel: string
  llmKey: string
  aspect: string
  targetSec: number
  autoDuration: boolean
  addCredit: boolean
  srcAudioVolume: number
  voiceKey: string
  // Cover state (persisted so a generated cover survives refresh; see STUDIO_DEFAULTS).
  cover: CoverResult | null
  useCover: boolean
  coverPrompt: string
  coverStyleIndex: number
  // Cover title draft fields (persist across refresh; see STUDIO_DEFAULTS).
  coverBasePath: string | null
  coverText: string
  coverKeyWords: string[]
  // Manual title-style knobs (persist across refresh; see STUDIO_DEFAULTS).
  coverPosition: string
  coverAlign: string
  coverKeyColor: string
  coverKeyColor2: string
  coverKeyColorAuto: boolean
  coverGradient: boolean
  coverStrokeColor: string
  coverStrokeAuto: boolean
  coverFontScale: number
  coverFontAuto: boolean
  coverTilt: number
  coverTiltAuto: boolean
  // In-flight cover task id (persisted so the poll resumes across a refresh).
  coverTaskId: string | null
  // The job whose completion should clear the cover. Persisted so that if the user
  // refreshes while the job is still running, the clear-on-done watch re-engages
  // after reload and still fires when the job finishes. null = nothing to watch.
  createdJobId: number | null
}

// Read the cached draft from localStorage, merged over the defaults so a partial or
// stale draft (missing keys after a schema change) still yields a complete object.
// Any parse/access failure (private mode, corrupt JSON) falls back to the defaults.
function loadStudioDraft(): StudioDraft {
  const base: StudioDraft = { ...STUDIO_DEFAULTS, voiceKey: '' }
  try {
    const raw = localStorage.getItem(STUDIO_DRAFT_KEY)
    if (!raw) return base
    const saved = JSON.parse(raw) as Partial<StudioDraft>
    // One-time migration for the position/align/tilt default flip. A draft saved
    // BEFORE the flip still carries the old auto values, and `{...base, ...saved}`
    // would keep overriding the new defaults until the next job submit — so the
    // first load after the flip drops just those keys and lets the new defaults
    // through. Every other draft field (link, title, voice…) is preserved.
    if (!localStorage.getItem(COVER_TITLE_DEFAULTS_MIGRATION_KEY)) {
      localStorage.setItem(COVER_TITLE_DEFAULTS_MIGRATION_KEY, '1')
      delete saved.coverPosition
      delete saved.coverAlign
      delete saved.coverTilt
      delete saved.coverTiltAuto
    }
    return { ...base, ...saved }
  } catch {
    return base
  }
}

// Pull a YouTube video id out of common URL shapes (watch / youtu.be / shorts / embed).
function ytId(url: string): string | null {
  try {
    const u = new URL(url.trim())
    if (u.hostname.includes('youtu.be')) return u.pathname.slice(1) || null
    const v = u.searchParams.get('v')
    if (v) return v
    const m = u.pathname.match(/\/(shorts|embed)\/([\w-]+)/)
    if (m) return m[2]
  } catch {
    /* not a URL yet */
  }
  return null
}

// Format a duration (seconds) as mm:ss; null = unbounded ("∞").
function fmtTier(s: number | null): string {
  if (s == null) return '∞'
  const m = Math.floor(s / 60)
  const sec = Math.round(s % 60)
  return `${m}:${String(sec).padStart(2, '0')}`
}

interface VoiceOption {
  key: string
  label: string
  voice?: string
  refAudio?: string
  model: string // clone engine (for grouping the dropdown)
  name?: string // clone's filename-without-ext (ClonedVoice.name) — used to delete on disk
}

// Matches a baked model suffix at the end of a clone name, e.g. " - F5-TTS".
// Derived from VOICE_CLONE_MODELS[].short so the list can never drift.
const BAKED_MODEL_SUFFIX = new RegExp(
  ` - (${VOICE_CLONE_MODELS.map((m) => m.short.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})$`,
)

// Only cloned voices are offered (preset sample voices are no longer shown).
// Each clone's model drives the dropdown's <optgroup>:
//   - NEW clone: the backend bakes the model into the saved name
//     (e.g. "Surface - F5-TTS"). BAKED_MODEL_SUFFIX extracts the model and the
//     suffix is stripped from the displayed label (the group already shows it).
//   - LEGACY clone: no suffix → grouped under "VieNeu" (all pre-existing clones
//     in this project were made with VieNeu). The key/path stay the full name.
function buildVoiceOptions(voices: VoicesResponse | null): VoiceOption[] {
  const opts: VoiceOption[] = []
  voices?.cloned.forEach((c: ClonedVoice) => {
    const m = c.name.match(BAKED_MODEL_SUFFIX)
    const model = m ? m[1] : 'VieNeu'
    const display = m ? c.name.slice(0, m.index) : c.name // strip " - <model>"
    opts.push({ key: `clone:${c.name}`, label: `🎙 ${display}`, refAudio: c.path, model, name: c.name })
  })
  return opts
}

// Group cloned voices by their clone model for the dropdown header. Groups keep
// first-seen order, except the F5-TTS group is always pinned to the front.
const PINNED_VOICE_MODEL = 'F5-TTS'
function groupVoiceOptionsByModel(opts: VoiceOption[]): { model: string; options: VoiceOption[] }[] {
  const order: string[] = []
  const byModel: Record<string, VoiceOption[]> = {}
  for (const o of opts) {
    if (!byModel[o.model]) {
      byModel[o.model] = []
      order.push(o.model)
    }
    byModel[o.model].push(o)
  }
  // Stable sort: pin F5-TTS first, leave every other group in its existing order.
  order.sort((a, b) => (a === PINNED_VOICE_MODEL ? -1 : 0) - (b === PINNED_VOICE_MODEL ? -1 : 0))
  return order.map((model) => ({ model, options: byModel[model] }))
}

function defaultVoiceKey(_voices: VoicesResponse | null, opts: VoiceOption[]): string {
  return opts[0]?.key ?? ''
}

// Custom voice dropdown: a button + an absolutely-positioned panel listing cloned
// voices grouped by clone model. Each row is selectable; on hover a trash button
// appears that deletes the voice FILE from disk (so the user can re-clone). Styled
// with the same Tailwind tokens as the native Select (bg-panel / border-line / etc).
export function VoicePicker({
  value,
  onChange,
  voices,
  page,
  onDeleted,
  className = '',
  settingKey,
}: {
  value: string
  onChange: (key: string) => void
  voices: VoicesResponse | null
  page: string
  onDeleted: () => void
  className?: string
  // When set, each voice row shows a ★ to pin that voice as the default (stored
  // under `cf.default.<settingKey>`). Mirrors the shared Select feature; the
  // fresh-form auto-select in the parent reads the same key. Omit → no ★.
  settingKey?: string
}) {
  const [open, setOpen] = useState(false)
  const [confirming, setConfirming] = useState<VoiceOption | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // The pinned-default voiceKey (filled ★), read once on mount and updated on pin.
  const [voiceDefault, setVoiceDefault] = useState<string | null>(() =>
    settingKey ? getDefaultPref(settingKey) : null,
  )
  const rootRef = useRef<HTMLDivElement>(null)
  const { success, error: toastError } = useToast()

  const options = useMemo(() => buildVoiceOptions(voices), [voices])
  const groups = useMemo(() => groupVoiceOptionsByModel(options), [options])
  const selected = options.find((o) => o.key === value)

  // Pin/unpin a voice as the default (distinct from selecting it): persists to
  // localStorage and flips that row's ★. Toggle — clicking the ★ of the CURRENT
  // default un-pins it; clicking any other voice's ★ pins that one instead.
  const pinDefault = (o: VoiceOption) => {
    if (!settingKey) return
    if (voiceDefault === o.key) {
      clearDefaultPref(settingKey)
      setVoiceDefault(null)
      success('Đã bỏ giọng mặc định')
    } else {
      setDefaultPref(settingKey, o.key)
      setVoiceDefault(o.key)
      success('Đã đặt giọng mặc định')
    }
  }

  // Close on outside-click and Escape (only while the panel is open).
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const select = (key: string) => {
    onChange(key)
    setOpen(false)
  }

  const doDelete = async () => {
    if (!confirming?.name) return
    setDeleting(true)
    setError(null)
    try {
      await api.deleteVoice(page, confirming.name)
      // If the deleted voice was selected, clear the selection — the parent's
      // default-selection effect will pick the first remaining option.
      if (confirming.key === value) onChange('')
      setConfirming(null)
      onDeleted()
      success('Đã xóa giọng')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      toastError('Xóa giọng thất bại')
    } finally {
      setDeleting(false)
    }
  }

  // Label shown on the trigger button. The selected voice shows its clone engine
  // inline ("🎙 Name - F5-TTS") so the engine is visible at a glance in the input;
  // the dropdown list keeps its own model-group headers unchanged.
  const triggerLabel =
    voices === null
      ? 'Đang tải giọng…'
      : options.length === 0
        ? 'Chưa có giọng — bấm + để clone'
        : selected
          ? `${selected.label} - ${selected.model}`
          : 'Chọn giọng…'

  // Whether the CURRENTLY-SELECTED voice is the pinned default (drives the field ★).
  const selectedIsDefault = !!settingKey && !!value && voiceDefault === value

  return (
    <div ref={rootRef} className={`group relative ${className}`}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="flex h-9 w-full items-center justify-between gap-2 rounded-lg border border-line bg-panel px-3 text-sm text-fg outline-none transition hover:border-brand/40 focus:border-brand/50 focus:ring-2 focus:ring-brand/20"
      >
        {/* pr reserves room so the field ★ sits left of the chevron, never over it. */}
        <span className={`truncate ${settingKey && selected ? 'pr-7' : ''} ${selected ? 'text-fg' : 'text-muted'}`}>
          {triggerLabel}
        </span>
        <ChevronDown className={`h-4 w-4 shrink-0 text-muted transition ${open ? 'rotate-180' : ''}`} />
      </button>
      {/* Field-level ★: mirrors the shared Select. Pins the CURRENTLY-SELECTED voice as
          the default. Hidden when nothing is selected. Filled amber (always visible)
          when the selected voice IS the default; outline on field hover otherwise. */}
      {settingKey && selected && (
        <button
          type="button"
          // preventDefault stops the wrapping <Field> <label> from forwarding the click
          // to the trigger; stopPropagation keeps the panel from toggling.
          onClick={(e) => {
            e.preventDefault()
            e.stopPropagation()
            pinDefault(selected)
          }}
          title={selectedIsDefault ? 'Đang là mặc định — bấm để bỏ' : 'Đặt làm mặc định'}
          aria-label={selectedIsDefault ? 'Giọng đang là mặc định' : 'Đặt giọng đang chọn làm mặc định'}
          aria-pressed={selectedIsDefault}
          className={`absolute right-9 top-1/2 z-10 grid h-6 w-6 -translate-y-1/2 place-items-center rounded transition ${
            selectedIsDefault
              ? 'text-amber-500 opacity-100 dark:text-amber-400'
              : 'text-muted opacity-0 hover:text-amber-500 focus:opacity-100 group-hover:opacity-100 dark:hover:text-amber-400'
          }`}
        >
          <Star className={`h-3.5 w-3.5 ${selectedIsDefault ? 'fill-current' : ''}`} />
        </button>
      )}

      {open && (
        <div className="absolute left-0 top-[calc(100%+4px)] z-30 max-h-72 min-w-[560px] max-w-[92vw] overflow-y-auto rounded-lg border border-line bg-panel p-1 shadow-card">
          {voices === null ? (
            <div className="px-3 py-2 text-sm text-muted">Đang tải giọng…</div>
          ) : (
            // Always render THREE side-by-side columns (F5-TTS / VieNeu / OmniVoice)
            // regardless of content, so the fixed-engine structure is always visible.
            // groupVoiceOptionsByModel already pins F5-TTS first and only returns
            // models that HAVE voices, so we merge it over a fixed base (empty columns
            // get a muted placeholder). REQUIRED_COLUMNS entries key on the engine's
            // `short` value (VOICE_CLONE_MODELS[].short) — the same value baked into
            // cloned-voice names and extracted by BAKED_MODEL_SUFFIX for grouping.
            // Any extra model beyond these three flows into additional cells (grid-cols-3 wraps).
            (() => {
              const REQUIRED_COLUMNS = [PINNED_VOICE_MODEL, 'VieNeu', 'OmniVoice']
              const byModel = new Map(groups.map((g) => [g.model, g.options]))
              const columnModels = [
                ...REQUIRED_COLUMNS,
                ...groups.map((g) => g.model).filter((m) => !REQUIRED_COLUMNS.includes(m)),
              ]
              return (
                <div className="grid grid-cols-3 gap-1">
                  {columnModels.map((model) => {
                    const opts = byModel.get(model) ?? []
                    return (
                      <div key={model} className="min-w-0">
                        <div className="px-2 pb-1 pt-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted/80">
                          {model}
                        </div>
                        {opts.length === 0 ? (
                          <div className="px-2 py-1.5 text-xs text-muted/60">(chưa có giọng)</div>
                        ) : (
                          opts.map((o) => (
                            <div
                              key={o.key}
                              role="button"
                              tabIndex={0}
                              // preventDefault stops the wrapping <label> (from <Field>) forwarding
                              // this click to the trigger button, which would reopen the panel.
                              onClick={(e) => {
                                e.preventDefault()
                                select(o.key)
                              }}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter' || e.key === ' ') {
                                  e.preventDefault()
                                  select(o.key)
                                }
                              }}
                              className={`group flex cursor-pointer items-center justify-between gap-2 rounded-md px-2 py-1.5 text-sm transition ${
                                o.key === value ? 'bg-brand/15 text-brand' : 'text-fg hover:bg-panel2'
                              }`}
                            >
                              <span className="flex min-w-0 items-center gap-1.5">
                                {o.key === value && <Check className="h-3.5 w-3.5 shrink-0" />}
                                <span className="truncate">{o.label}</span>
                              </span>
                              <span className="flex shrink-0 items-center gap-0.5">
                                {settingKey && (
                                  <button
                                    type="button"
                                    // Pin as default (NOT the same as selecting). stopPropagation
                                    // + preventDefault keep the row-select/label-forward from firing.
                                    aria-label={
                                      voiceDefault === o.key
                                        ? `${o.label} đang là mặc định`
                                        : `Đặt ${o.label} làm mặc định`
                                    }
                                    aria-pressed={voiceDefault === o.key}
                                    title={voiceDefault === o.key ? 'Đang là mặc định — bấm để bỏ' : 'Đặt làm mặc định'}
                                    onClick={(e) => {
                                      e.preventDefault()
                                      e.stopPropagation()
                                      pinDefault(o)
                                    }}
                                    className={`grid h-6 w-6 place-items-center rounded transition ${
                                      voiceDefault === o.key
                                        ? 'text-amber-500 opacity-100 dark:text-amber-400'
                                        : 'text-muted opacity-0 hover:bg-amber-500/10 hover:text-amber-500 focus:opacity-100 group-hover:opacity-100 dark:hover:text-amber-400'
                                    }`}
                                  >
                                    <Star className={`h-3.5 w-3.5 ${voiceDefault === o.key ? 'fill-current' : ''}`} />
                                  </button>
                                )}
                                {o.name && (
                                  <button
                                    type="button"
                                    aria-label={`Xoá giọng ${o.label}`}
                                    onClick={(e) => {
                                      e.preventDefault()
                                      e.stopPropagation()
                                      setError(null)
                                      setConfirming(o)
                                    }}
                                    className="grid h-6 w-6 place-items-center rounded text-muted opacity-0 transition hover:bg-rose-500/10 hover:text-rose-400 focus:opacity-100 group-hover:opacity-100"
                                  >
                                    <Trash2 className="h-3.5 w-3.5" />
                                  </button>
                                )}
                              </span>
                            </div>
                          ))
                        )}
                      </div>
                    )
                  })}
                </div>
              )
            })()
          )}
        </div>
      )}

      {confirming && (
        <Modal open onClose={() => !deleting && setConfirming(null)} title="Xoá giọng">
          <div className="space-y-3">
            <p className="text-sm text-fg">
              Xoá giọng "{confirming.label}"? File sẽ bị xoá khỏi ổ cứng để bạn clone lại.
            </p>
            {error && <p className="text-xs text-rose-400">{error}</p>}
            <div className="flex justify-end gap-2 pt-1">
              <Button variant="ghost" onClick={() => setConfirming(null)} disabled={deleting}>
                Huỷ
              </Button>
              <Button onClick={doDelete} disabled={deleting}>
                {deleting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
                {deleting ? 'Đang xoá…' : 'Xoá'}
              </Button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}

export default function CreateVideo() {
  const { pages: PAGES } = useData()
  const refresh = useRefresh()
  // Incremented each time a job is created; WorkflowProgress watches this to
  // trigger an immediate poll instead of waiting up to 1.5s. Especially useful
  // for reused-script jobs that skip ingest+script-gen and run fast.
  const [jobTrigger, setJobTrigger] = useState(0)

  // No page selector in this view: always operate on the first available page.
  const page = PAGES[0] ?? null
  const pageName = page?.name ?? ''

  // Per-page cloned voices for the Studio + SceneEditor.
  const [voices, setVoices] = useState<VoicesResponse | null>(null)
  useEffect(() => {
    if (!pageName) {
      setVoices(null)
      return
    }
    let cancelled = false
    setVoices(null)
    api.listVoices(pageName)
      .then((v) => !cancelled && setVoices(v))
      .catch(() => !cancelled && setVoices({ presets: [], cloned: [] }))
    return () => {
      cancelled = true
    }
  }, [pageName])

  const reloadVoices = () => {
    if (pageName) api.listVoices(pageName).then(setVoices).catch(() => undefined)
  }

  // Empty state: no pages at all.
  if (PAGES.length === 0) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold tracking-tight">Tạo Video</h1>
        <Card className="p-5">
          <EmptyState
            Icon={Wand2}
            title="Chưa có trang nào"
            hint="Tạo một trang trước ở mục Trang, rồi quay lại đây để tạo video."
          />
        </Card>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Tạo Video</h1>

      {page && (
        <>
          {/* Workflow — live pipeline progress (top of page) */}
          <WorkflowProgress pageId={page.id} trigger={jobTrigger} />

          {/* Studio — create a new video */}
          <Studio
            pageId={page.id}
            pageName={page.name}
            voices={voices}
            onVoicesChanged={reloadVoices}
            onCreated={async () => { await refresh(); setJobTrigger((t) => t + 1) }}
          />

          {/* Edit scenes — manual scene-by-scene build */}
          <SceneEditor pageName={page.name} voices={voices} onVoicesChanged={reloadVoices} />
        </>
      )}
    </div>
  )
}

// ---- Platform + format-tier picker -------------------------------------

// One duration tier on a PlatformSpec (tiers is optional → NonNullable element).
type Tier = NonNullable<PlatformSpec['tiers']>[number]

// Suggest a target duration (seconds) INSIDE a tier band, clamped to the tier's
// own [min,max] (when present) and to the form's allowed 1s..50min range.
function suggestTierSec(tier: Tier): number {
  const lo = tier.minDurationS ?? MIN_TARGET_SEC
  const hi = tier.maxDurationS
  let raw: number
  if (tier.key === 'short') {
    raw = hi != null ? Math.min(hi - 5, 85) : 85
  } else if (tier.key === 'mid') {
    raw = hi != null ? Math.round((lo + hi) / 2) : 120
  } else {
    // long
    raw = Math.max(lo, 300)
  }
  // Clamp into the tier band (when bounded) then into the form's hard range.
  let v = raw
  v = Math.max(lo, v)
  if (hi != null) v = Math.min(hi, v)
  v = Math.max(MIN_TARGET_SEC, Math.min(MAX_TARGET_SEC, v))
  return v
}

// Linked-platform + tier picker. Single-select platform → render its 3 tiers.
// Selecting a tier auto-applies aspect + duration on the parent form (decision:
// auto-apply). Fetch failures degrade gracefully: the picker just hides.
function PlatformTierPicker({
  specs,
  onApply,
  onPlatformChange,
}: {
  specs: PlatformSpec[] | null
  onApply: (a: { aspect?: string; targetSec?: number }) => void
  // Lifts the currently-picked platform up to Studio so auto-publish can target
  // exactly the platform chosen here (null = no platform picked → no auto-publish).
  onPlatformChange: (platform: string | null) => void
}) {
  const [channels, setChannels] = useState<{ platform: string; label: string }[] | null>(null)
  const [platform, setPlatform] = useState<string | null>(null)
  const [tierKey, setTierKey] = useState<string | null>(null)

  // The "Tạo Video" menu is now its own page (not nested under a page), so this
  // picker only suggests OUTPUT FORMAT — it lists EVERY linked platform across
  // ALL pages (dedupe by platform). Being linked anywhere is enough; no per-page
  // filter. Failures → empty list.
  useEffect(() => {
    let cancelled = false
    setChannels(null)
    setPlatform(null)
    onPlatformChange(null)
    setTierKey(null)
    api.getAllLinkedChannels()
      .then((res) => {
        if (cancelled) return
        const seen = new Set<string>()
        const list: { platform: string; label: string }[] = []
        for (const c of res.pages.flatMap((p) => p.channels)) {
          // Show every LINKED platform (a token/channel exists) for format
          // guidance — being publish-ready (canPublish) is NOT required here.
          // Guard on `linked` so a future linked-but-not-publishable channel
          // (e.g. a Facebook page mid-setup) still appears in the picker.
          if (c.linked === false) continue
          if (seen.has(c.platform)) continue
          seen.add(c.platform)
          const spec = specs?.find((s) => s.platform === c.platform)
          list.push({ platform: c.platform, label: spec?.label ?? c.platform })
        }
        setChannels(list)
      })
      .catch(() => !cancelled && setChannels([]))
    return () => {
      cancelled = true
    }
  }, [specs])

  if (channels === null) {
    return <p className="text-xs text-muted">Đang tải nền tảng đã liên kết…</p>
  }
  if (channels.length === 0) {
    return <p className="text-xs text-muted">Chưa có nền tảng nào được liên kết.</p>
  }

  const spec = platform ? specs?.find((s) => s.platform === platform) ?? null : null
  const tiers = spec?.tiers ?? []
  const selectedTier = tiers.find((t) => t.key === tierKey) ?? null

  const pickPlatform = (p: string) => {
    setPlatform(p)
    onPlatformChange(p)
    setTierKey(null)
  }

  const pickTier = (t: Tier) => {
    setTierKey(t.key)
    const aspect = t.key === 'short' ? '9:16' : '16:9'
    onApply({ aspect, targetSec: suggestTierSec(t) })
  }

  return (
    <div className="space-y-3 rounded-xl border border-line bg-panel2 p-3">
      <div>
        <span className="mb-1.5 block text-xs font-medium text-muted">Nền tảng đã liên kết</span>
        <div className="flex flex-wrap gap-1.5">
          {channels.map((c) => (
            <button
              key={c.platform}
              type="button"
              onClick={() => pickPlatform(c.platform)}
              className={`flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm transition ${
                platform === c.platform
                  ? (PLATFORM_STYLE[c.platform]?.selected ?? 'border-brand/50 bg-brand/15 text-brand')
                  : (PLATFORM_STYLE[c.platform]?.idle ?? 'border-line bg-panel text-fg hover:border-brand/40')
              }`}
            >
              {PLATFORM_STYLE[c.platform]?.logo}
              {PLATFORM_SHORT[c.platform] ?? c.label}
            </button>
          ))}
        </div>
      </div>

      {platform && tiers.length === 0 && (
        <p className="text-xs text-muted">Nền tảng này chưa có phân tầng định dạng.</p>
      )}

      {platform && tiers.length > 0 && (
        <div>
          <span className="mb-1.5 block text-xs font-medium text-muted">Loại video</span>
          <div className="grid gap-1.5 sm:grid-cols-3">
            {tiers.map((t) => {
              const range =
                t.maxDurationS == null
                  ? `≥ ${fmtTier(t.minDurationS)}`
                  : t.minDurationS == null || t.minDurationS === 0
                    ? `≤ ${fmtTier(t.maxDurationS)}`
                    : `${fmtTier(t.minDurationS)}–${fmtTier(t.maxDurationS)}`
              return (
                <button
                  key={t.key}
                  type="button"
                  onClick={() => pickTier(t)}
                  className={`flex flex-col items-start gap-0.5 rounded-lg border px-3 py-2 text-left transition ${
                    tierKey === t.key
                      ? 'border-brand/50 bg-brand/15'
                      : 'border-line bg-panel hover:border-brand/40'
                  }`}
                >
                  <span className={`text-sm font-medium ${tierKey === t.key ? 'text-brand' : 'text-fg'}`}>
                    {t.label}
                  </span>
                  <span className="text-[11px] tabular-nums text-muted">{range}</span>
                </button>
              )
            })}
          </div>
        </div>
      )}

      {selectedTier?.note && (
        <p className="text-[11px] text-muted">{selectedTier.note}</p>
      )}
    </div>
  )
}

// ---- Reusable-script picker (PART B) -----------------------------------

// Vietnamese label for a script's render mode, shown as a badge in the picker.
const SCRIPT_MODE_LABEL: Record<string, string> = {
  footage: 'Giữ footage gốc',
  image: 'Ảnh AI',
  stickman: 'Stickman',
}

// mm:ss (or h:mm:ss) timestamp for Dubbed subtitle rows. Unlike ui.tsx's fmtClock,
// a 0s start renders as "0:00" (a real timestamp), not an em-dash placeholder.
function fmtTs(total: number): string {
  const s = Math.max(0, Math.round(total || 0))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  const mm = h ? String(m).padStart(2, '0') : String(m)
  return `${h ? `${h}:` : ''}${mm}:${String(sec).padStart(2, '0')}`
}

// Escape a user-typed string for safe use inside a RegExp (search highlighting).
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

// Split `text` on case-insensitive occurrences of `query` and render each match
// as a <mark>. `matchStart` is the running match index across the whole script;
// the match whose global index === `activeMatch` gets the "current" highlight so
// Prev/Next can visually focus one match. Returns the rendered nodes plus the
// number of matches found in this text (so the caller can advance matchStart).
function highlightMatches(
  text: string,
  query: string,
  matchStart: number,
  activeMatch: number,
): { nodes: React.ReactNode[]; count: number } {
  if (!query.trim()) return { nodes: [text], count: 0 }
  const re = new RegExp(escapeRegExp(query), 'gi')
  const nodes: React.ReactNode[] = []
  let last = 0
  let count = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index))
    const globalIdx = matchStart + count
    const isActive = globalIdx === activeMatch
    nodes.push(
      <mark
        key={`${m.index}-${count}`}
        data-search-match={globalIdx}
        className={
          isActive
            ? 'rounded bg-amber-400 px-0.5 text-[#0a0c12]'
            : 'rounded bg-amber-400/30 px-0.5 text-fg'
        }
      >
        {m[0]}
      </mark>,
    )
    last = m.index + m[0].length
    count++
    // Guard against zero-length matches (shouldn't happen with escaped literals).
    if (m.index === re.lastIndex) re.lastIndex++
  }
  if (last < text.length) nodes.push(text.slice(last))
  return { nodes, count }
}

// Newest-first ordering for the reusable-script list. The backend DOES order its
// DB rows created_at DESC, but it then APPENDS orphaned-manifest items (createdAt
// null, os.listdir order) after them — and a future change to that endpoint's
// default order must not silently break the UI guarantee. So we sort here, at the
// point of render, using only fields the API already returns:
//   1. rows that have a createdAt come first, newest timestamp on top;
//   2. rows without one (source='manifest' — genuinely undated) go last;
//   3. videoId DESC breaks every tie (ids are monotonic → newest id first).
function compareScriptsNewestFirst(a: ReusableScript, b: ReusableScript): number {
  const ta = a.createdAt ? Date.parse(a.createdAt) : NaN
  const tb = b.createdAt ? Date.parse(b.createdAt) : NaN
  const va = Number.isNaN(ta), vb = Number.isNaN(tb)
  if (va !== vb) return va ? 1 : -1          // undated items sink to the bottom
  if (!va && ta !== tb) return tb - ta       // both dated → newest first
  return b.videoId - a.videoId               // tie / both undated → highest id first
}

// Modal that lists previously-produced videos whose script can be reused (skip
// script-gen). Fetches GET /api/pages/{pageId}/reusable-scripts on open, narrowed
// by the current source link when present. Each row shows title/source, scene
// count, render-mode badge, edit mode and a narration preview; a "Xem trước"
// affordance expands the full saved script via GET /api/videos/{videoId}/script.
// Selecting a row lifts its videoId + metadata up to the Studio. Styled with the
// same Tailwind tokens as VoicePicker / the rest of the form.
function ReusableScriptPicker({
  pageId,
  link,
  onClose,
  onPick,
}: {
  pageId: number
  link: string
  onClose: () => void
  // `edited` is true when this video's narration was edited inline during THIS
  // picker session (so its cached audio no longer matches the script text).
  onPick: (s: ReusableScript, edited: boolean) => void
}) {
  const [scripts, setScripts] = useState<ReusableScript[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Full-script expansion: which videoId is expanded + its loaded detail/state.
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [detail, setDetail] = useState<VideoScriptDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)
  // Index of the scene currently playing audio, or null when idle.
  const [playingScene, setPlayingScene] = useState<number | null>(null)
  // Holds the active HTMLAudioElement so we can stop it on demand.
  const audioRef = useRef<HTMLAudioElement | null>(null)
  // Per-video "Xóa audio" (clear cached WAVs) state.
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null)
  const [deletingAudioFor, setDeletingAudioFor] = useState<number | null>(null)
  const [audioDeletedFor, setAudioDeletedFor] = useState<Set<number>>(new Set())
  // Inline scene-narration editing state.
  const [editingScene, setEditingScene] = useState<number | null>(null)
  const [editText, setEditText] = useState('')
  const [editSaving, setEditSaving] = useState(false)
  // Video ids whose narration was edited inline during this session. Used to flag
  // "script edited since audio was synthesized" when the picked script is reused
  // WITH cached audio (Button 2) — a cache HIT would then serve stale audio.
  const [editedVideoIds, setEditedVideoIds] = useState<Set<number>>(new Set())
  // Per-video "keep script on delete" set (shared with Videos.tsx via the
  // KEEP_SCRIPT_IDS_KEY helpers). localStorage is the source of truth; this local
  // Set mirror re-renders the row toggles when the owner flips one.
  const [keepIds, setKeepIds] = useState<Set<number>>(getKeepScriptIds)
  const toggleKeepScript = (videoId: number, keep: boolean) => {
    setKeepScript(videoId, keep)
    setKeepIds(getKeepScriptIds())
  }

  // Search-within-script: query text + which match (0-based, across the whole
  // expanded script) is the "current" one for Prev/Next focus. Reset whenever the
  // expanded script changes (see the expandedId effect below).
  const [search, setSearch] = useState('')
  const [activeMatch, setActiveMatch] = useState(0)

  const toast = useToast()

  const handleDeleteAudioOnly = async (videoId: number) => {
    flushSync(() => setDeletingAudioFor(videoId))
    try {
      const result = await api.deleteVideoAudio(videoId)
      setAudioDeletedFor(prev => new Set(prev).add(videoId))
      toast.success(result.count > 0 ? `Đã xóa ${result.count} file audio` : 'Không có audio cache để xóa')
    } catch {
      toast.error('Không thể xóa audio cache')
    } finally {
      setDeletingAudioFor(null)
    }
  }

  const handleDeleteAll = async (videoId: number) => {
    flushSync(() => setDeletingAudioFor(videoId))
    try {
      await api.clearVideoScript(videoId)
      setScripts(prev => prev ? prev.filter(s => s.videoId !== videoId) : prev)
      toast.success('Đã xóa kịch bản')
    } catch {
      toast.error('Không thể xóa kịch bản')
    } finally {
      setDeletingAudioFor(null)
    }
  }

  const handleSaveEdit = async (videoId: number, sceneNum: number) => {
    if (!editText.trim()) return
    setEditSaving(true)
    try {
      await api.updateSceneNarration(videoId, sceneNum, editText.trim())
      // Update local detail state so the UI reflects the change immediately.
      // Dubbed scripts have no scenes and no inline edit path, so leave them as-is.
      setDetail(prev => {
        if (!prev || prev.kind === 'dubbed') return prev
        return {
          ...prev,
          scenes: prev.scenes.map(sc =>
            sc.scene === sceneNum ? { ...sc, narration: editText.trim() } : sc
          ),
        }
      })
      setEditingScene(null)
      // Record that this video's script was edited this session: cached audio is
      // now stale, so reusing WITH audio (Button 2) should warn about a mismatch.
      setEditedVideoIds(prev => new Set(prev).add(videoId))
      toast.success('Đã lưu nội dung cảnh')
    } catch {
      // keep edit open, user can retry
      toast.error('Không lưu được nội dung cảnh')
    } finally {
      setEditSaving(false)
    }
  }

  // Stop any in-flight audio (cached WAV or browser TTS fallback).
  const stopAudio = () => {
    if (audioRef.current) {
      audioRef.current.onerror = null   // prevent onerror firing when src = ''
      audioRef.current.onended = null
      audioRef.current.pause()
      audioRef.current.src = ''
      audioRef.current = null
    }
    if (typeof window !== 'undefined' && window.speechSynthesis) {
      window.speechSynthesis.cancel()
    }
  }

  // Play a scene: tries the cached WAV at /api/videos/{videoId}/scenes/{n}/audio
  // first; on 404 (cache miss) falls back to browser Web Speech API.
  const toggleSpeak = (sceneNum: number, videoId: number, narration: string) => {
    stopAudio()
    if (playingScene === sceneNum) {
      setPlayingScene(null)
      return
    }
    setPlayingScene(sceneNum)
    const audio = new Audio(`/api/videos/${videoId}/scenes/${sceneNum}/audio`)
    audioRef.current = audio
    audio.onended = () => setPlayingScene((cur) => (cur === sceneNum ? null : cur))
    audio.onerror = () => {
      // Cache miss or unsupported — fall back to Web Speech API.
      audioRef.current = null
      const synth = typeof window !== 'undefined' ? window.speechSynthesis : null
      if (synth && narration.trim()) {
        const utter = new SpeechSynthesisUtterance(narration)
        utter.lang = 'vi-VN'
        const viVoice = synth.getVoices().find((v) => v.lang?.toLowerCase().startsWith('vi')) ?? null
        if (viVoice) utter.voice = viVoice
        utter.onend = () => setPlayingScene((cur) => (cur === sceneNum ? null : cur))
        utter.onerror = () => setPlayingScene((cur) => (cur === sceneNum ? null : cur))
        synth.speak(utter)
      } else {
        setPlayingScene(null)
      }
    }
    audio.play().catch(() => { /* onerror handles it */ })
  }

  // Stop audio when the modal unmounts.
  useEffect(() => () => stopAudio(), [])

  // Stop audio when the user collapses or switches to a different script preview.
  // Also reset the in-script search so matches/indices don't carry across scripts.
  useEffect(() => {
    stopAudio()
    setPlayingScene(null)
    setEditingScene(null)
    setSearch('')
    setActiveMatch(0)
  }, [expandedId])

  useEffect(() => {
    let cancelled = false
    setScripts(null)
    setError(null)
    api.getReusableScripts(pageId, link.trim() || undefined)
      .then((rows) => !cancelled && setScripts(rows))
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : String(e)))
    return () => {
      cancelled = true
    }
  }, [pageId, link])

  const togglePreview = (videoId: number) => {
    if (expandedId === videoId) {
      setExpandedId(null)
      return
    }
    setExpandedId(videoId)
    setDetail(null)
    setDetailError(null)
    setDetailLoading(true)
    let cancelled = false
    api.getVideoScript(videoId)
      .then((d) => !cancelled && setDetail(d))
      .catch((e) => !cancelled && setDetailError(e instanceof Error ? e.message : String(e)))
      .finally(() => !cancelled && setDetailLoading(false))
  }

  // Always render newest-first, independent of the order the API happened to return.
  // Copy before sorting — never mutate the state array in place.
  const sortedScripts = useMemo(
    () => (scripts ? scripts.slice().sort(compareScriptsNewestFirst) : null),
    [scripts],
  )

  const expandedScript = expandedId !== null && scripts
    ? scripts.find(s => s.videoId === expandedId) ?? null
    : null

  // Total search matches across the expanded script's narrations (case-insensitive).
  const totalMatches = useMemo(() => {
    // Search applies to scene narrations only; dubbed transcripts have no scenes.
    if (!search.trim() || !detail || detail.kind === 'dubbed') return 0
    const re = new RegExp(escapeRegExp(search), 'gi')
    return detail.scenes.reduce((sum, sc) => sum + ((sc.narration ?? '').match(re)?.length ?? 0), 0)
  }, [search, detail])

  // Keep activeMatch in range whenever the query or match count changes.
  useEffect(() => {
    if (activeMatch >= totalMatches) setActiveMatch(totalMatches > 0 ? totalMatches - 1 : 0)
  }, [totalMatches, activeMatch])

  // Scroll the active <mark> into view when navigating between matches.
  useEffect(() => {
    if (!search.trim() || totalMatches === 0) return
    const el = document.querySelector(`[data-search-match="${activeMatch}"]`)
    el?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [activeMatch, search, totalMatches, detail])

  const gotoMatch = (delta: number) => {
    if (totalMatches === 0) return
    setActiveMatch((cur) => (cur + delta + totalMatches) % totalMatches)
  }

  return (
    <Modal open onClose={onClose} title="Dùng kịch bản đã tạo trước đó" maxWidthClass="max-w-2xl">
      <div className="space-y-3">
        <p className="text-xs text-muted">
          Chọn một kịch bản đã tạo trước đó để dùng lại — pipeline sẽ BỎ QUA bước viết kịch bản (tiết kiệm thời gian & chi phí).
          {link.trim() ? ' Đang lọc theo link nguồn hiện tại.' : ' Hiện mọi kịch bản đã lưu của trang này.'}
        </p>

        {/* Search-within-script — only when a script preview is expanded (that's
            where the script text lives). Highlights matches in the narration,
            shows match count, and Prev/Next jump between matches. Hidden for
            dubbed transcripts (no scene narration to search). */}
        {detail && detail.kind !== 'dubbed' && (
          <div className="flex items-center gap-1.5">
            <div className="relative flex-1">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
              <input
                type="text"
                value={search}
                onChange={(e) => { setSearch(e.target.value); setActiveMatch(0) }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') { e.preventDefault(); gotoMatch(e.shiftKey ? -1 : 1) }
                }}
                placeholder="Tìm trong kịch bản…"
                className="h-9 w-full rounded-lg border border-line bg-panel pl-8 pr-16 text-sm text-fg outline-none transition placeholder:text-muted/70 focus:border-brand/50 focus:ring-2 focus:ring-brand/20"
              />
              {search && (
                <div className="absolute right-1.5 top-1/2 flex -translate-y-1/2 items-center gap-1">
                  <span className="text-[11px] tabular-nums text-muted">
                    {totalMatches > 0 ? `${activeMatch + 1}/${totalMatches}` : '0/0'}
                  </span>
                  <button
                    type="button"
                    onClick={() => { setSearch(''); setActiveMatch(0) }}
                    aria-label="Xoá tìm kiếm"
                    className="grid h-5 w-5 place-items-center rounded text-muted transition hover:bg-panel2 hover:text-fg"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              )}
            </div>
            <button
              type="button"
              onClick={() => gotoMatch(-1)}
              disabled={totalMatches === 0}
              aria-label="Kết quả trước"
              className="grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-line bg-panel text-muted transition hover:text-fg disabled:opacity-40"
            >
              <ChevronUp className="h-4 w-4" />
            </button>
            <button
              type="button"
              onClick={() => gotoMatch(1)}
              disabled={totalMatches === 0}
              aria-label="Kết quả kế tiếp"
              className="grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-line bg-panel text-muted transition hover:text-fg disabled:opacity-40"
            >
              <ChevronDown className="h-4 w-4" />
            </button>
          </div>
        )}

        {scripts === null && !error && (
          <div className="flex items-center gap-2 py-6 text-sm text-muted">
            <Loader2 className="h-4 w-4 animate-spin" /> Đang tải kịch bản…
          </div>
        )}
        {error && <p className="py-4 text-sm text-rose-400">Không tải được kịch bản: {error}</p>}
        {scripts !== null && !error && scripts.length === 0 && (
          <p className="py-6 text-center text-sm text-muted">
            Chưa có kịch bản nào để dùng lại{link.trim() ? ' cho link này' : ''}.
          </p>
        )}

        {sortedScripts !== null && sortedScripts.length > 0 && (
          <ul className="max-h-[60vh] space-y-2 overflow-y-auto pr-0.5">
            {sortedScripts.map((s) => {
              const heading = s.title?.trim() || s.sourceName?.trim() || `Video #${s.videoId}`
              const modeLabel = s.renderMode ? SCRIPT_MODE_LABEL[s.renderMode] ?? s.renderMode : null
              const expanded = expandedId === s.videoId
              return (
                <li key={s.videoId} className="rounded-xl border border-line bg-panel2 p-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium text-fg">{heading}</p>
                      <div className="mt-1 flex flex-wrap items-center gap-1.5">
                        {/* Dubbed scripts carry no scene array (sceneCount is 0), so
                            "0 cảnh" would wrongly read as empty — show a mode badge. */}
                        {s.editMode === 'dubbed'
                          ? <Pill tone="amber">Lồng tiếng</Pill>
                          : <Pill tone="brand">{s.sceneCount} cảnh</Pill>}
                        {modeLabel && <Pill tone="sky">{modeLabel}</Pill>}
                        {s.editMode && <Pill tone="slate">{s.editMode}</Pill>}
                        {/* Manifest-only scripts live on disk (no DB row) and never
                            carry a reusable audio cache → only fresh-audio reuse. */}
                        {s.source === 'manifest' && <Pill tone="slate">Chỉ manifest</Pill>}
                      </div>
                      {s.preview && (
                        <p className="mt-1.5 line-clamp-2 text-xs text-muted">{s.preview}</p>
                      )}
                    </div>
                    <div className="flex shrink-0 flex-col items-end gap-1.5">
                      <Button onClick={() => onPick(s, editedVideoIds.has(s.videoId))} className="shrink-0">
                        <Check className="h-4 w-4" /> Dùng
                      </Button>
                      <button
                        type="button"
                        onClick={() => togglePreview(s.videoId)}
                        className="inline-flex items-center gap-1 text-[11px] text-muted transition hover:text-fg"
                      >
                        {expanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRightIcon className="h-3.5 w-3.5" />}
                        Xem trước
                      </button>
                      <button
                        type="button"
                        onClick={() => setConfirmDeleteId(s.videoId)}
                        disabled={deletingAudioFor === s.videoId}
                        title="Xóa audio cache để TTS tạo bản thu mới"
                        className="inline-flex items-center text-muted transition hover:text-rose-400 disabled:opacity-50"
                      >
                        {deletingAudioFor === s.videoId
                          ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                          : audioDeletedFor.has(s.videoId)
                            ? <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />
                            : <Trash2 className="h-3.5 w-3.5" />}
                      </button>
                      {/* Per-video keep-on-delete flag (shared with Videos.tsx). Only
                          for DB-backed scripts — a manifest-only row has no DB video
                          row to delete, so the flag is meaningless there. */}
                      {s.source === 'manifest' ? (
                        <span className="text-[10px] text-muted/60" title="Không có bản ghi video để xóa">
                          Giữ khi xóa —
                        </span>
                      ) : (
                        <label className="flex cursor-pointer items-center gap-1 text-[10px] text-muted" title="Khi xóa video ở lịch sử, giữ lại kịch bản để tái dùng (chỉ xóa media + audio)">
                          <input
                            type="checkbox"
                            checked={keepIds.has(s.videoId)}
                            onChange={(e) => toggleKeepScript(s.videoId, e.target.checked)}
                            className="h-3.5 w-3.5 rounded border-line bg-panel accent-[var(--color-brand)]"
                          />
                          Giữ khi xóa
                        </label>
                      )}
                    </div>
                  </div>

                  {expanded && (
                    <div className="mt-2.5 rounded-lg border border-line bg-panel p-2.5">
                      {detailLoading && (
                        <div className="flex items-center gap-2 text-xs text-muted">
                          <Loader2 className="h-3.5 w-3.5 animate-spin" /> Đang tải kịch bản đầy đủ…
                        </div>
                      )}
                      {detailError && <p className="text-xs text-rose-400">Không tải được: {detailError}</p>}
                      {/* Dubbed transcript: read-only timestamped VN subtitle list. */}
                      {detail && !detailLoading && !detailError && detail.kind === 'dubbed' && (
                        <div>
                          <div className="mb-2 flex items-center gap-2">
                            <Pill tone="amber">Phụ đề lồng tiếng</Pill>
                            <span className="text-[11px] text-muted">{detail.subs.length} dòng</span>
                          </div>
                          <ol className="space-y-1.5">
                            {detail.subs.map((sub, i) => (
                              <li key={i} className="flex gap-2 text-xs">
                                <span className="shrink-0 tabular-nums text-muted/80">
                                  {fmtTs(sub.start)} → {fmtTs(sub.end)}
                                </span>
                                <span className="text-fg">{sub.text_vi}</span>
                              </li>
                            ))}
                          </ol>
                        </div>
                      )}
                      {/* Scene-array script (image/footage/stickman): existing render,
                          kept unchanged. `kind !== 'dubbed'` also covers a legacy
                          response missing `kind` (backward-safe). */}
                      {detail && !detailLoading && !detailError && detail.kind !== 'dubbed' && (
                        <ol className="space-y-2">
                          {(() => {
                            // Running count of matches BEFORE each scene, so each
                            // scene's highlighted marks get a unique global index
                            // for Prev/Next focus. Computed once per render pass.
                            let offset = 0
                            const sceneOffsets = detail.scenes.map((sc) => {
                              const start = offset
                              if (search.trim()) {
                                const re = new RegExp(escapeRegExp(search), 'gi')
                                offset += (sc.narration ?? '').match(re)?.length ?? 0
                              }
                              return start
                            })
                            return detail.scenes.map((sc, sceneIdx) => (
                            <li key={sc.scene} className="text-xs">
                              <span className="font-semibold text-muted">Cảnh {sc.scene}.</span>{' '}
                              {editingScene === sc.scene ? (
                                <span className="mt-1 flex flex-col gap-1">
                                  <textarea
                                    className="w-full rounded border border-line bg-panel px-2 py-1 text-xs text-fg focus:outline-none focus:ring-1 focus:ring-brand/50"
                                    rows={3}
                                    value={editText}
                                    onChange={e => setEditText(e.target.value)}
                                    autoFocus
                                  />
                                  <span className="flex gap-1.5">
                                    <button
                                      type="button"
                                      disabled={editSaving}
                                      onClick={() => handleSaveEdit(expandedId!, sc.scene)}
                                      className="inline-flex items-center gap-1 rounded bg-brand/10 px-2 py-0.5 text-[10px] font-medium text-brand hover:bg-brand/20 disabled:opacity-50"
                                    >
                                      {editSaving ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}
                                      Lưu
                                    </button>
                                    <button
                                      type="button"
                                      onClick={() => setEditingScene(null)}
                                      className="inline-flex items-center gap-1 rounded px-2 py-0.5 text-[10px] text-muted hover:text-fg"
                                    >
                                      Hủy
                                    </button>
                                  </span>
                                </span>
                              ) : (
                                <span className="inline-flex items-start gap-1">
                                  <span className="text-fg">
                                    {search.trim()
                                      ? highlightMatches(sc.narration, search, sceneOffsets[sceneIdx], activeMatch).nodes
                                      : sc.narration}
                                  </span>
                                  <button
                                    type="button"
                                    title="Sửa nội dung cảnh này"
                                    onClick={() => { setEditingScene(sc.scene); setEditText(sc.narration) }}
                                    className="ml-1 shrink-0 rounded p-0.5 text-muted opacity-60 transition hover:bg-panel2 hover:text-fg hover:opacity-100"
                                  >
                                    <Pencil className="h-3 w-3" />
                                  </button>
                                </span>
                              )}
                              {sc.image_prompt && (
                                <p className="mt-0.5 text-[11px] italic text-muted/80">🖼 {sc.image_prompt}</p>
                              )}
                              {/* Timestamp row — play button sits to the right of the clock */}
                              {(sc.sourceStart != null || sc.sourceEnd != null) && (
                                <p className="mt-0.5 flex items-center gap-2 text-[11px] tabular-nums text-muted/80">
                                  <span>⏱ {fmtClock(sc.sourceStart ?? 0)} – {fmtClock(sc.sourceEnd ?? 0)}</span>
                                  {sc.narration?.trim() && (
                                    <button
                                      type="button"
                                      onClick={() => toggleSpeak(sc.scene, expandedId!, sc.narration)}
                                      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium transition ${
                                        playingScene === sc.scene
                                          ? 'bg-brand/15 text-brand hover:bg-brand/25'
                                          : 'text-muted hover:bg-panel2 hover:text-fg'
                                      }`}
                                    >
                                      {playingScene === sc.scene ? (
                                        <><Square className="h-3 w-3" /> Dừng</>
                                      ) : (
                                        <><Play className="h-3 w-3" /> Phát</>
                                      )}
                                    </button>
                                  )}
                                </p>
                              )}
                              {/* For non-footage scenes (no timestamp) show play button on its own row */}
                              {sc.sourceStart == null && sc.sourceEnd == null && sc.narration?.trim() && (
                                <p className="mt-0.5">
                                  <button
                                    type="button"
                                    onClick={() => toggleSpeak(sc.scene, expandedId!, sc.narration)}
                                    className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium transition ${
                                      playingScene === sc.scene
                                        ? 'bg-brand/15 text-brand hover:bg-brand/25'
                                        : 'text-muted hover:bg-panel2 hover:text-fg'
                                    }`}
                                  >
                                    {playingScene === sc.scene ? (
                                      <><Square className="h-3 w-3" /> Dừng</>
                                    ) : (
                                      <><Play className="h-3 w-3" /> Phát</>
                                    )}
                                  </button>
                                </p>
                              )}
                            </li>
                            ))
                          })()}
                        </ol>
                      )}
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        )}

        <div className="flex justify-end gap-2 pt-1">
          {expandedScript && (
            <Button onClick={() => onPick(expandedScript, editedVideoIds.has(expandedScript.videoId))}>
              <Check className="h-4 w-4" /> Dùng
            </Button>
          )}
          <Button variant="ghost" onClick={onClose}>Đóng</Button>
        </div>
      </div>

      {confirmDeleteId !== null && (
        <Modal open onClose={() => setConfirmDeleteId(null)} title="Xóa kịch bản" maxWidthClass="max-w-sm">
          <p className="text-sm text-muted">
            Chọn cách xóa cho bản ghi này:
          </p>
          <div className="mt-4 flex flex-col gap-2">
            <Button
              variant="outline"
              onClick={() => {
                const id = confirmDeleteId
                setConfirmDeleteId(null)
                handleDeleteAudioOnly(id)
              }}
            >
              Chỉ xóa audio — giữ kịch bản lại
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                const id = confirmDeleteId
                setConfirmDeleteId(null)
                handleDeleteAll(id)
              }}
            >
              <Trash2 className="h-4 w-4" /> Xóa luôn kịch bản này
            </Button>
            <Button variant="ghost" onClick={() => setConfirmDeleteId(null)}>Hủy</Button>
          </div>
        </Modal>
      )}
    </Modal>
  )
}

// ---- Batch "Add List" --------------------------------------------------

// Settings snapshot passed from the Studio: exactly the create-time fields a batch
// job shares with a single "Tạo video" (BatchCreateBody minus pageId + per-link
// items, which the modal supplies). Read from the SAME Studio state as create().
type BatchSettings = Omit<BatchCreateBody, 'pageId' | 'items'>

// One manual row in the modal's editable grid: the owner types BOTH the source
// link and the Vietnamese title. A row counts as valid (→ becomes a job) only
// when both fields are non-empty. `id` is a stable key so rows can be reordered/
// removed without React key collisions on empty/duplicate links.
interface BatchRow {
  id: number
  link: string
  title: string
}

// Modal (mirrors SavedCoverPicker's Modal usage): a manual row grid — no probe/
// preview/translate. The owner types each source link + its Vietnamese title,
// then Save enqueues one queued job per fully-filled row (POST /api/jobs/batch)
// with the CURRENT Studio settings. The runner processes them sequentially; the
// owner watches progress in "Lịch sử Job".
function BatchListModal({
  pageId,
  settings,
  hasOuterLink,
  onAdoptFirst,
  onClose,
  onCreated,
}: {
  pageId: number
  settings: BatchSettings
  // Whether the Studio's outer source-link input already has a link. When it is
  // EMPTY, the batch's first row is moved into that input (onAdoptFirst) instead of
  // being saved as a held job — otherwise every batch link would be 'held' with no
  // outer link, leaving "Tạo video" disabled (it requires a non-empty outer link)
  // and the held rows unrunnable. The remaining rows are still saved as held.
  hasOuterLink: boolean
  onAdoptFirst: (link: string, title: string) => void
  onClose: () => void
  onCreated: () => Promise<void>
}) {
  const { success, error: toastError } = useToast()
  // Monotonic row-id source (never reused), so removing a row never lets a later
  // new row reuse a stale key.
  const nextId = useRef(5)
  // Start with 5 empty rows.
  const [rows, setRows] = useState<BatchRow[]>(() =>
    Array.from({ length: 5 }, (_, i) => ({ id: i, link: '', title: '' })),
  )
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  // Per-link create failures, shown after a batch save (kept until re-save/close).
  const [failures, setFailures] = useState<{ link: string; error: string }[]>([])
  // Per-row title-probe in flight (row id → true), for the small spinner.
  const [probing, setProbing] = useState<Record<number, boolean>>({})

  // A row becomes a job only when BOTH link and title are non-empty (blanks skipped).
  const validRows = rows.filter((r) => r.link.trim() && r.title.trim())

  const addRow = () => setRows((prev) => [...prev, { id: nextId.current++, link: '', title: '' }])
  const removeRow = (id: number) =>
    setRows((prev) => (prev.length <= 1 ? prev : prev.filter((r) => r.id !== id)))
  const editRow = (id: number, patch: Partial<Pick<BatchRow, 'link' | 'title'>>) =>
    setRows((prev) => prev.map((r) => (r.id === id ? { ...r, ...patch } : r)))

  // Per-row debounce timers for the auto-title probe (cleared on unmount).
  const probeTimers = useRef<Record<number, ReturnType<typeof setTimeout>>>({})
  useEffect(() => () => Object.values(probeTimers.current).forEach(clearTimeout), [])

  // On link edit, debounce a lightweight metadata probe and, when it resolves,
  // auto-fill the SOURCE video's title into the still-empty title cell. We only
  // fill when the row still holds the same link AND its title is empty — so a
  // title the owner typed (or already auto-filled + edited) is never clobbered.
  const scheduleTitleProbe = (id: number, rawLink: string) => {
    const link = rawLink.trim()
    const timers = probeTimers.current
    if (timers[id]) clearTimeout(timers[id])
    if (!/^https?:\/\//i.test(link)) {
      setProbing((p) => { const n = { ...p }; delete n[id]; return n })
      return
    }
    setProbing((p) => ({ ...p, [id]: true }))
    timers[id] = setTimeout(() => {
      api.probeLink(link)
        .then((res) => {
          const t = res?.title?.trim()
          if (!t) return
          setRows((prev) =>
            prev.map((r) =>
              r.id === id && r.link.trim() === link && !r.title.trim() ? { ...r, title: t } : r,
            ),
          )
        })
        .catch(() => undefined)
        .finally(() => setProbing((p) => { const n = { ...p }; delete n[id]; return n }))
    }, 600)
  }

  const editLink = (id: number, v: string) => {
    editRow(id, { link: v })
    scheduleTitleProbe(id, v)
  }

  const doSave = async () => {
    if (validRows.length === 0) return
    setSaving(true)
    setErr(null)
    setFailures([])
    try {
      // When the Studio's outer link input is EMPTY, adopt the first row into it so
      // "Tạo video" enables (it requires a non-empty outer link) and pressing it runs
      // the first link + releases the held rest. The remaining rows are saved as held.
      // If the outer input already has a link, keep every row as held (old behavior).
      let toHold = validRows
      if (!hasOuterLink && validRows.length > 0) {
        const first = validRows[0]
        onAdoptFirst(first.link.trim(), first.title.trim())
        toHold = validRows.slice(1)
      }

      // Only the adopted link (nothing left to hold): the first link is now in the
      // Studio input — nudge the owner to press "Tạo video".
      if (toHold.length === 0) {
        success('Đã điền link đầu vào Studio — bấm "Tạo video" để chạy')
        onClose()
        return
      }

      const body: BatchCreateBody = {
        pageId,
        items: toHold.map((r) => ({ link: r.link.trim(), title: r.title.trim() })),
        ...settings,
      }
      const res = await api.batchCreateJobs(body)
      const created: number[] = []
      const failed: { link: string; error: string }[] = []
      for (const r of res.results) {
        if ('jobId' in r) created.push(r.jobId)
        else failed.push({ link: r.link, error: r.error })
      }
      await onCreated()
      if (created.length > 0) {
        success(
          hasOuterLink
            ? `Đã lưu ${created.length} nguồn (sẽ chạy khi bấm Tạo video)`
            : `Đã điền link đầu vào Studio + lưu ${created.length} nguồn còn lại — bấm "Tạo video" để chạy tất cả`,
        )
      }
      if (failed.length > 0) {
        setFailures(failed)
        toastError(`${failed.length} link lỗi`)
      }
      // Close only when every link succeeded; otherwise keep the modal open so the
      // owner can see which links failed.
      if (failed.length === 0) onClose()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
      toastError('Lưu hàng loạt thất bại')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal open onClose={onClose} title="Thêm danh sách video" maxWidthClass="max-w-3xl">
      <div className="space-y-3">
        <p className="text-xs text-muted">
          Nhập từng dòng: link nguồn (trái) và tiêu đề (phải). Dán link xong sẽ tự lấy tiêu đề của video nguồn điền
          vào ô tiêu đề (bạn có thể sửa lại). Chỉ những dòng có ĐỦ cả link lẫn tiêu đề mới được lưu. Các nguồn được lưu
          lại (chưa chạy) với đúng thiết lập hiện tại của Studio; chúng sẽ tự chạy khi bạn bấm "Tạo video".
        </p>

        {/* Column header */}
        <div className="grid grid-cols-[1fr_1fr_auto] items-center gap-2 px-0.5 text-[11px] font-medium text-muted">
          <span>Link nguồn</span>
          <span>Tiêu đề tiếng Việt</span>
          <span className="w-8" aria-hidden />
        </div>

        <div className="max-h-[46vh] space-y-2 overflow-y-auto pr-0.5">
          {rows.map((r) => (
            <div key={r.id} className="grid grid-cols-[1fr_1fr_auto] items-center gap-2">
              <TextInput
                value={r.link}
                onChange={(v) => editLink(r.id, v)}
                placeholder="https://youtube.com/watch?v=…"
                className="w-full"
              />
              <div className="relative">
                <TextInput
                  value={r.title}
                  onChange={(v) => editRow(r.id, { title: v })}
                  placeholder={probing[r.id] ? 'Đang lấy tiêu đề nguồn…' : 'Tiêu đề (tự động lấy từ link)…'}
                  className="w-full"
                />
                {probing[r.id] && (
                  <Loader2 className="pointer-events-none absolute right-2 top-1/2 h-4 w-4 -translate-y-1/2 animate-spin text-muted" />
                )}
              </div>
              <button
                type="button"
                onClick={() => removeRow(r.id)}
                disabled={rows.length <= 1}
                title="Xoá dòng"
                aria-label="Xoá dòng"
                className="flex h-8 w-8 items-center justify-center rounded-lg border border-line text-muted transition hover:border-rose-500/40 hover:text-rose-400 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:border-line disabled:hover:text-muted"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          ))}
        </div>

        <div className="flex items-center justify-between">
          <Button variant="outline" onClick={addRow} disabled={saving}>
            <Plus className="h-4 w-4" /> Thêm dòng
          </Button>
          <span className="text-[11px] text-muted">{validRows.length} dòng hợp lệ</span>
        </div>

        {err && <p className="text-xs text-rose-400">{err}</p>}

        {failures.length > 0 && (
          <div className="rounded-lg border border-rose-500/40 bg-rose-500/5 px-3 py-2">
            <p className="text-xs font-medium text-rose-400">{failures.length} link không tạo được:</p>
            <ul className="mt-1 space-y-0.5">
              {failures.map((f) => (
                <li key={f.link} className="truncate text-[11px] text-muted" title={`${f.link} — ${f.error}`}>
                  {f.link} — <span className="text-rose-400">{f.error}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="flex items-center justify-end gap-2 pt-1">
          <Button variant="ghost" onClick={onClose} disabled={saving}>Đóng</Button>
          <Button onClick={doSave} disabled={saving || validRows.length === 0}>
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            {saving ? 'Đang lưu…' : `Lưu (${validRows.length})`}
          </Button>
        </div>
      </div>
    </Modal>
  )
}

// ---- Studio ------------------------------------------------------------

function Studio({
  pageId,
  pageName,
  voices,
  onVoicesChanged,
  onCreated,
}: {
  pageId: number
  pageName: string
  voices: VoicesResponse | null
  onVoicesChanged: () => void
  onCreated: () => Promise<void>
}) {
  const { success, error: toastError } = useToast()
  // Shared jobs list — used to detect when the job THIS Studio created reaches a
  // completed state, so the cover can be cleared once the video is done (not on
  // submit). See the createdJobId + clear-on-done effect below.
  const { jobs } = useData()
  // Per-job creation fields are restored from the cached draft (localStorage) on
  // mount so an in-progress draft survives navigation/refresh; they fall back to
  // STUDIO_DEFAULTS when no draft exists, and are reset to defaults (and the cache
  // cleared) after a successful create. `initialDraft` is read once at mount.
  const [initialDraft] = useState<StudioDraft>(loadStudioDraft)
  const [link, setLink] = useState<string>(initialDraft.link)
  const [voiceKey, setVoiceKey] = useState(initialDraft.voiceKey)
  const [editMode, setEditMode] = useState<string>(initialDraft.editMode)
  const [renderModel, setRenderModel] = useState<string>(initialDraft.renderModel)
  const [voiceCloneModel, setVoiceCloneModel] = useState<string>(initialDraft.voiceCloneModel)
  const renderInstalled = INSTALLED_RENDER_MODELS.has(renderModel)
  const voiceModelInstalled = VOICE_CLONE_MODELS.find((m) => m.value === voiceCloneModel)?.installed ?? true
  // Script-gen LLM: the selection is an encoded "<provider>|<model>" key (llmOptionKey);
  // `llmOptions` is the runtime list from GET /api/llm/models (null = still loading).
  const [llmKey, setLlmKey] = useState<string>(initialDraft.llmKey)
  const [llmOptions, setLlmOptions] = useState<LlmModelOption[] | null>(null)
  const [aspect, setAspect] = useState<string>(initialDraft.aspect)
  const [targetSec, setTargetSec] = useState<number>(initialDraft.targetSec)
  const [autoDuration, setAutoDuration] = useState<boolean>(initialDraft.autoDuration)
  const [addCredit, setAddCredit] = useState<boolean>(initialDraft.addCredit)
  const [srcAudioVolume, setSrcAudioVolume] = useState<number>(initialDraft.srcAudioVolume)
  const [publish, setPublish] = useState(false) // opt-in auto-upload; default off = manual publish later
  // Platform picked in PlatformTierPicker, lifted up here. Auto-publish targets
  // ONLY this platform; null = no platform picked → nothing is auto-published.
  const [publishPlatform, setPublishPlatform] = useState<string | null>(null)
  const [showAddVoice, setShowAddVoice] = useState(false)

  // PART B (script reuse): when a saved script is picked, the job skips
  // script-gen. The selection is in-session only (not persisted across refreshes)
  // and resets to null after a successful create. `reusedScriptEdited` likewise
  // tracks in-session edits only.
  const [showScriptPicker, setShowScriptPicker] = useState(false)
  const [reusedScript, setReusedScript] = useState<ReusableScript | null>(null)
  const reuseScriptVideoId = reusedScript?.videoId ?? null
  // Whether the picked script had an inline narration edit this session — drives
  // the "audio mismatch" warning when the user chooses to reuse cached audio.
  const [reusedScriptEdited, setReusedScriptEdited] = useState(false)
  // Reuse-mode for the picked script (only meaningful when reusedScript is set):
  //   'fresh-audio' → Button 1: reuse script text only, force fresh TTS (bypass cache).
  //   'with-audio'  → Button 2: reuse script + let the TTS cache serve existing audio.
  // Defaults to 'fresh-audio' (the safe path) until the owner chooses.
  const [reuseMode, setReuseMode] = useState<'fresh-audio' | 'with-audio'>('fresh-audio')
  // A script's cached audio is STALE either because it was edited inline THIS
  // session (reusedScriptEdited) or because the backend flagged it edited in a
  // PREVIOUS session (reusedScript.audioStale). Both mean reusing WITH audio would
  // serve mismatched audio, so we force a fresh re-synth + warn.
  const audioStale = reusedScriptEdited || (reusedScript?.audioStale ?? false)
  // Whether reusing WITH cached audio is even possible for the picked script.
  const audioCached = reusedScript?.audioCached ?? false
  // Edited/stale text → stale cache regardless of reuseMode; always bypass+delete.
  const bypassTtsCache = reuseMode === 'fresh-audio' || audioStale
  // The "audio may not match" warning shows when the owner picks Button 2 on an
  // edited script; they can dismiss it to proceed anyway.
  const [audioMismatchAck, setAudioMismatchAck] = useState(false)

  // Platforms LINKED on the SELECTED page (publish targets this page's channels,
  // but the picker lists platforms linked across ALL pages). Used only for the
  // honest coherence note: if the picked platform isn't linked here, the backend
  // will skip publishing. Failures → empty set (we then fall back to a generic hint).
  const [pageLinkedPlatforms, setPageLinkedPlatforms] = useState<Set<string> | null>(null)
  useEffect(() => {
    let cancelled = false
    setPageLinkedPlatforms(null)
    api.getLinkedChannels(pageId)
      .then((res) => {
        if (cancelled) return
        const set = new Set(res.channels.filter((c) => c.linked).map((c) => c.platform))
        setPageLinkedPlatforms(set)
      })
      .catch(() => !cancelled && setPageLinkedPlatforms(new Set()))
    return () => {
      cancelled = true
    }
  }, [pageId])

  // Script-gen LLM options. Fetched ONCE on mount — the backend caches the list
  // server-side (~6h), so no client-side cache here. The list is key-dependent and
  // can legitimately contain a single entry (claude-cli alone); that is not an
  // error state. On failure we fall back to an EMPTY list: the dropdown then shows
  // one "backend default" row and create() omits the llm fields, so the job runs
  // exactly as it did before this feature existed.
  useEffect(() => {
    let cancelled = false
    api.getLlmModels()
      .then((res) => {
        if (cancelled) return
        const opts = res.options ?? []
        setLlmOptions(opts)
        // Resolve the selection ONCE the real list is known: keep the draft/pinned
        // choice when it is still offered, otherwise take whatever the API marks
        // is_default. Never hardcode 'claude-cli' — the default is the backend's
        // to move, and a pinned key for a provider that lost its API key must not
        // leave the field pointing at an option that no longer exists.
        setLlmKey((cur) => {
          if (cur && opts.some((o) => llmOptionKey(o) === cur)) return cur
          const def = opts.find((o) => o.is_default) ?? opts[0]
          return def ? llmOptionKey(def) : ''
        })
      })
      .catch(() => {
        if (!cancelled) setLlmOptions([])
      })
    return () => {
      cancelled = true
    }
  }, [])

  // The selected LLM row (null while the list is loading, or when it came back empty).
  const selectedLlm = llmOptions?.find((o) => llmOptionKey(o) === llmKey) ?? null
  // The llm half of the create payload. EMPTY when the owner is on the backend's
  // default option (or nothing is resolved yet): omitted keys are dropped by
  // JSON.stringify, so a default job's body stays byte-identical to the one this
  // form sent before the dropdown existed.
  const llmPayload: { llmProvider?: string | null; llmModel?: string | null } =
    selectedLlm && !selectedLlm.is_default
      ? { llmProvider: selectedLlm.provider, llmModel: selectedLlm.model }
      : {}

  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [creating, setCreating] = useState(false)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)

  // Output title for the video being created. Applied at create time; an empty
  // value falls back to the source video's title on the backend.
  const [title, setTitle] = useState<string>(initialDraft.title)

  // Adopt a pending (held) source into the Studio input when the outer link is EMPTY
  // and the pipeline is idle. Held rows (a saved source-list batch) can't run on their
  // own: "Tạo video" is disabled without an outer link, so a batch saved with no outer
  // link leaves every row stuck. Here we MOVE the oldest held row into the outer input
  // (link + title) and DELETE it from held — so pressing "Tạo video" creates it once and
  // releases the REST (no duplicate). Guarded so it adopts exactly one row, only while
  // nothing is running. Delete-then-fill order: on a delete failure the input stays empty
  // (no double-run risk) and it retries on the next jobs refresh.
  const adoptingHeldRef = useRef(false)
  useEffect(() => {
    if (link.trim() || adoptingHeldRef.current) return
    const pageJobs = jobs.filter((j) => j.pageId === pageId)
    if (pageJobs.some((j) => j.status === 'running')) return // pipeline busy — don't touch
    const held = pageJobs.filter((j) => j.status === 'held').sort((a, b) => a.id - b.id)
    if (held.length === 0) return
    const first = held[0]
    adoptingHeldRef.current = true
    void (async () => {
      try {
        await api.deleteJob(first.id)
        setLink(first.inputPayload)
        if (first.title) setTitle(first.title)
        await onCreated()
      } catch {
        /* leave input empty; retry on the next jobs refresh */
      } finally {
        adoptingHeldRef.current = false
      }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobs, link, pageId])

  // AI cover/thumbnail generation. `cover` holds the last generated cover;
  // `coverStyleIndex` increments each click so re-clicking varies the style;
  // `useCover` opts the cover into the job at create time. These are PERSISTED in
  // the Studio draft (so a generated cover, the use-cover choice, the manual
  // prompt, and the style index all survive a page refresh) and are cleared when
  // the created job COMPLETES (see the clear-on-done effect below) — NOT on submit,
  // so the cover stays visible while the job runs. `coverLoading`/`coverErr` are
  // transient (not persisted).
  const [cover, setCover] = useState<CoverResult | null>(initialDraft.cover)
  const [coverLoading, setCoverLoading] = useState(false)
  // Live progress percent (0..100) while a cover generates. Transient — never
  // persisted in the draft (like coverLoading).
  const [coverPct, setCoverPct] = useState(0)
  // The actual assembled prompt the backend is sending to SDXL, surfaced from the
  // progress poll and shown under the loading indicator. Transient — NOT persisted.
  const [coverPromptShown, setCoverPromptShown] = useState('')
  const [coverStyleIndex, setCoverStyleIndex] = useState(initialDraft.coverStyleIndex)
  const [useCover, setUseCover] = useState(initialDraft.useCover)
  const [coverErr, setCoverErr] = useState<string | null>(null)
  // Optional manual base prompt for the cover. When non-empty it overrides the
  // auto title+summary prompt on the backend (style still varies per click).
  const [coverPrompt, setCoverPrompt] = useState(initialDraft.coverPrompt)
  // Cover TITLE state. `coverBasePath` tracks the CLEAN (title-less) cover
  // separately from the displayed `cover`: it is set on a FRESH generate or a
  // saved-cover pick, but NOT when applying the title — so every renderCoverTitle
  // call re-composites from the clean base (no stacking). `coverText` is the
  // editable Vietnamese title (prefilled from the generate result's viTitle);
  // `coverKeyWords` are the phrases the backend highlights. `coverTextLoading` is
  // transient (apply-button spinner, not persisted).
  const [coverBasePath, setCoverBasePath] = useState<string | null>(initialDraft.coverBasePath)
  const [coverText, setCoverText] = useState(initialDraft.coverText)
  const [coverKeyWords, setCoverKeyWords] = useState<string[]>(initialDraft.coverKeyWords)
  // Manual title-style knobs (default Auto). Position 'auto' = auto anchor; the
  // *Auto booleans send null for the color/font/tilt values while on.
  const [coverPosition, setCoverPosition] = useState(initialDraft.coverPosition)
  const [coverAlign, setCoverAlign] = useState(initialDraft.coverAlign)
  const [coverStrokeColor, setCoverStrokeColor] = useState(initialDraft.coverStrokeColor)
  const [coverStrokeAuto, setCoverStrokeAuto] = useState(initialDraft.coverStrokeAuto)
  const [coverKeyColor, setCoverKeyColor] = useState(initialDraft.coverKeyColor)
  const [coverKeyColor2, setCoverKeyColor2] = useState(initialDraft.coverKeyColor2)
  const [coverKeyColorAuto, setCoverKeyColorAuto] = useState(initialDraft.coverKeyColorAuto)
  const [coverGradient, setCoverGradient] = useState(initialDraft.coverGradient)
  const [coverFontScale, setCoverFontScale] = useState(initialDraft.coverFontScale)
  const [coverFontAuto, setCoverFontAuto] = useState(initialDraft.coverFontAuto)
  const [coverTilt, setCoverTilt] = useState(initialDraft.coverTilt)
  const [coverTiltAuto, setCoverTiltAuto] = useState(initialDraft.coverTiltAuto)
  const [coverTextLoading, setCoverTextLoading] = useState(false)
  // In-flight cover task id, persisted so the percent poll RESUMES after a refresh
  // (see the resume effect + pollCover below). null = no active task.
  const [coverTaskId, setCoverTaskId] = useState<string | null>(initialDraft.coverTaskId)
  // Monotonic token that identifies the CURRENT cover-generation run. Each
  // makeCover() bumps it; the poll loop captures its own token and bails the moment
  // the ref no longer matches (a newer click started, or the component unmounted),
  // so a stale poll can never overwrite a newer render. Bumped to a sentinel on
  // unmount by the cleanup effect below.
  const coverRunRef = useRef(0)
  // Monotonic counter → a NEW seed on every "Áp dụng" click so the backend re-rolls
  // a fresh title style VARIATION (position/gradient/dominant-color) each time, even
  // when the text is unchanged. Not the SDXL render seed — just a changing integer.
  const coverTitleSeedRef = useRef(0)
  // Id of the job THIS Studio last created — the one whose completion should clear
  // the cover. Persisted in the draft so the watch survives a refresh (if the job
  // finishes after reload, the clear-on-done effect still fires). null = nothing
  // to watch.
  const [createdJobId, setCreatedJobId] = useState<number | null>(initialDraft.createdJobId)
  // Guards the clear-on-done so it fires exactly ONCE per completed job and never
  // wipes a cover the user is preparing for the NEXT video (a new create() sets a
  // fresh createdJobId; a job id can only be cleared once).
  const clearedCoverJobIdRef = useRef<number | null>(null)
  // The cover PATH submitted with the currently-tracked job. Clear-on-done only
  // wipes the editing cover if it STILL equals this snapshot — so if the owner
  // has already started a NEW cover for the next video, completion of the old job
  // won't wipe it. null = the tracked job had no cover, so there's nothing to
  // reconcile (clear-on-done then leaves the editing cover untouched).
  const submittedCoverPathRef = useRef<string | null>(null)

  // Browse-created-covers picker modal (mirrors the ReusableScriptPicker pattern).
  const [showSavedCovers, setShowSavedCovers] = useState(false)

  // Batch "Add List" modal (mirrors the SavedCoverPicker pattern): paste many
  // links, preview each link's auto-translated VN title, then create one job per
  // link using the CURRENT Studio settings.
  const [showBatch, setShowBatch] = useState(false)

  // Reuse-guard memory (submit-time safeguard against a silent reuse reset).
  // `reusedScript` is intentionally NOT persisted and is reset to null after every
  // successful create, so after creating a REUSE job the form keeps the same
  // link/voice/mode (looks identical) but the reuse selection is gone — the next
  // submit would silently regenerate a NEW script. To catch exactly that case we
  // remember, in a ref that is NOT cleared on create, the link of the LAST
  // successful create that used reuse, and which script videoId it reused. When a
  // new submit has the SAME (non-empty) link but reuse is now null, we know the
  // owner almost certainly expected reuse and warn before regenerating. These are
  // refs (not state) so they survive the post-create reset without being part of
  // the draft; a truly new/different/empty link never matches, so no nagging.
  const lastReuseLinkRef = useRef<string | null>(null)
  const lastReuseVideoIdRef = useRef<number | null>(null)
  // When the guard fires, this holds the videoId the last reuse used, and drives
  // the confirmation modal. null = modal closed.
  const [reuseGuard, setReuseGuard] = useState<{ lastVideoId: number } | null>(null)

  // Persist the in-progress draft on every field change so navigating away/back or
  // refreshing restores it. Only the per-job draft fields are cached (NOT in-session
  // reuse/publish selections). A successful create clears this key (see create()).
  useEffect(() => {
    const draft: StudioDraft = {
      link, title, editMode, renderModel, voiceCloneModel, llmKey,
      aspect, targetSec, autoDuration, addCredit, srcAudioVolume, voiceKey,
      cover, useCover, coverPrompt, coverStyleIndex, coverTaskId, createdJobId,
      coverBasePath, coverText, coverKeyWords,
      coverPosition, coverAlign, coverKeyColor, coverKeyColor2, coverKeyColorAuto, coverGradient,
      coverStrokeColor, coverStrokeAuto,
      coverFontScale, coverFontAuto, coverTilt, coverTiltAuto,
    }
    try {
      localStorage.setItem(STUDIO_DRAFT_KEY, JSON.stringify(draft))
    } catch {
      /* storage unavailable (private mode/quota) — draft caching is best-effort */
    }
  }, [link, title, editMode, renderModel, voiceCloneModel, llmKey, aspect, targetSec, autoDuration, addCredit, srcAudioVolume, voiceKey, cover, useCover, coverPrompt, coverStyleIndex, coverTaskId, createdJobId, coverBasePath, coverText, coverKeyWords, coverPosition, coverAlign, coverKeyColor, coverKeyColor2, coverKeyColorAuto, coverGradient, coverStrokeColor, coverStrokeAuto, coverFontScale, coverFontAuto, coverTilt, coverTiltAuto])

  // Clear the cover when the created job COMPLETES ('done') — not on submit — so
  // the cover stays visible while the pipeline runs, then disappears once the
  // video is ready. Scoped tightly so it can never wipe a cover the owner is
  // preparing for the NEXT video:
  //   - it only ever looks at the specific createdJobId this Studio last created
  //     (persisted, so a mid-run refresh re-engages the watch and it still fires
  //     when the job finishes afterwards);
  //   - clearedCoverJobIdRef ensures it fires at most ONCE per job id;
  //   - a new create() sets a fresh createdJobId, so the guard is per-job;
  //   - it only clears the editing cover state if that cover is STILL the one the
  //     job was submitted with (submittedCoverPathRef). If the owner has already
  //     started a NEW cover for the next video (a fresh generation in flight, or a
  //     different cover.path now), we skip the cover-state clear but still consume
  //     the job id so it doesn't retrigger.
  // Clearing the React state cascades into the draft-save effect above, which
  // re-persists the now-empty cover — so it doesn't come back on the next refresh.
  useEffect(() => {
    if (createdJobId == null) return
    if (clearedCoverJobIdRef.current === createdJobId) return
    const j = jobs.find((x) => x.id === createdJobId)
    if (j?.status === 'done') {
      clearedCoverJobIdRef.current = createdJobId
      // Only clear the editing cover if it's untouched since submit: the current
      // cover still matches the submitted path, and no new cover is being made.
      const coverUnchanged =
        !coverLoading &&
        (cover?.path ?? null) === submittedCoverPathRef.current
      if (coverUnchanged) {
        setCover(null)
        setUseCover(false)
        setCoverPrompt('')
        setCoverStyleIndex(0)
        setCoverErr(null)
        setCoverPct(0)
        setCoverTaskId(null)
        // Cover cleared → also clear its title base + editable title + keywords,
        // and reset the manual title-style knobs back to Auto.
        setCoverBasePath(null)
        setCoverText('')
        setCoverKeyWords([])
        setCoverPosition(STUDIO_DEFAULTS.coverPosition)
        setCoverAlign(STUDIO_DEFAULTS.coverAlign)
        setCoverStrokeColor(STUDIO_DEFAULTS.coverStrokeColor)
        setCoverStrokeAuto(STUDIO_DEFAULTS.coverStrokeAuto)
        setCoverKeyColor(STUDIO_DEFAULTS.coverKeyColor)
        setCoverKeyColor2(STUDIO_DEFAULTS.coverKeyColor2)
        setCoverKeyColorAuto(STUDIO_DEFAULTS.coverKeyColorAuto)
        setCoverGradient(STUDIO_DEFAULTS.coverGradient)
        setCoverFontScale(STUDIO_DEFAULTS.coverFontScale)
        setCoverFontAuto(STUDIO_DEFAULTS.coverFontAuto)
        setCoverTilt(STUDIO_DEFAULTS.coverTilt)
        setCoverTiltAuto(STUDIO_DEFAULTS.coverTiltAuto)
      }
      submittedCoverPathRef.current = null
      setCreatedJobId(null)
    }
  }, [jobs, createdJobId, cover, coverLoading])

  // Source link metadata (title/duration/thumbnail/handle) for the preview box.
  const [probe, setProbe] = useState<Awaited<ReturnType<typeof api.probeLink>> | null>(null)
  const [probing, setProbing] = useState(false)

  // Platform upload specs (incl. duration tiers) — fetched once, cached in state.
  // Null = not loaded yet / fetch failed (picker still renders without tiers).
  const [specs, setSpecs] = useState<PlatformSpec[] | null>(null)
  useEffect(() => {
    let cancelled = false
    api.getPlatformSpecs()
      .then((r) => !cancelled && setSpecs(r.specs))
      .catch(() => !cancelled && setSpecs(null))
    return () => {
      cancelled = true
    }
  }, [])

  // Apply a tier's chosen aspect + duration to the form (auto-apply decision).
  // Duration is clamped into the form's 1s..50min range and autoDuration is
  // turned off so the explicit target is sent.
  const applyTier = ({ aspect: a, targetSec: t }: { aspect?: string; targetSec?: number }) => {
    if (a) setAspect(a)
    if (t != null) {
      setAutoDuration(false)
      setTargetSec(Math.min(MAX_TARGET_SEC, Math.max(MIN_TARGET_SEC, t)))
    }
  }

  // Build the voice dropdown (cloned voices only; presets are no longer offered).
  const voiceOptions = useMemo(() => buildVoiceOptions(voices), [voices])

  // Initial voice selection. Mirrors the shared Select's "pinned default wins over a
  // draft-restored value" semantics — a PINNED voice (present in localStorage AND still
  // in the loaded list) is applied on the first load EVEN IF a draft restored a
  // different voiceKey. Runs against the async-loaded voiceOptions.
  //  - firstLoad (voiceInitRef): the one-time pass where the pinned default overrides a
  //    draft-restored voiceKey. Guarded by the ref so a later voices refetch (add/delete)
  //    can never clobber a manual selection.
  //  - empty voiceKey (any time: fresh form OR the post-create reset that clears it):
  //    fill it — prefer the pinned default when valid, else the first option.
  //  - stale/deleted pinned voice: `match` is undefined → keep the draft value, or fall
  //    back to the first option when empty (picker is never left empty).
  const voiceInitRef = useRef(false)
  useEffect(() => {
    if (!voiceOptions.length) return
    const firstLoad = !voiceInitRef.current
    voiceInitRef.current = true

    const pinned = getDefaultPref(VOICE_DEFAULT_SETTING_KEY)
    const match = pinned ? voiceOptions.find((o) => o.key === pinned) : undefined

    let pick: VoiceOption | undefined
    if (match && (firstLoad || !voiceKey)) pick = match
    else if (!voiceKey) pick = voiceOptions[0]
    if (!pick) return

    if (pick.key !== voiceKey) setVoiceKey(pick.key)
    // Engine coherence: when we auto-select the PINNED voice, make the voice-clone
    // engine follow it so the chosen clone is synth-able by the engine. The pinned
    // VOICE takes precedence over a pinned ENGINE default here.
    if (pick === match) {
      const engine = VOICE_CLONE_MODELS.find((m) => m.short === match.model)?.value
      if (engine && engine !== voiceCloneModel) setVoiceCloneModel(engine)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [voiceOptions, voiceKey])

  const selected = voiceOptions.find((o) => o.key === voiceKey)
  const vid = ytId(link)

  // PART B cross-mode warning. A reused script is mode-specific: a 'footage'
  // script carries sourceStart/sourceEnd, an 'image'/'stickman' script carries
  // image_prompt. Reusing across the footage ↔ image/stickman boundary can break
  // the render. We WARN (amber), never block — the owner may still proceed.
  const currentMode = renderModelToMode(renderModel)
  const reusedMode = reusedScript?.renderMode ?? null
  const crossModeWarning =
    reusedMode != null &&
    reusedMode !== currentMode &&
    // Only the footage ↔ (image|stickman) crossing is dangerous (different scene
    // fields). image ↔ stickman both use image_prompt → no warning.
    ((reusedMode === 'footage') !== (currentMode === 'footage'))

  // Debounced metadata probe whenever a valid link is present.
  useEffect(() => {
    if (!vid) {
      setProbe(null)
      setProbing(false)
      return
    }
    setProbing(true)
    const t = setTimeout(() => {
      let cancelled = false
      api.probeLink(link.trim())
        .then((p) => !cancelled && setProbe(p))
        .catch(() => !cancelled && setProbe(null))
        .finally(() => !cancelled && setProbing(false))
      return () => {
        cancelled = true
      }
    }, 600)
    return () => clearTimeout(t)
  }, [vid, link])

  const playPreview = async () => {
    if (!selected) return
    setPreviewing(true)
    setPreviewUrl(null)
    setMsg(null)
    try {
      const { url } = await api.previewVoice({ page: pageName, voice: selected.voice ?? null, refAudio: selected.refAudio ?? null })
      setPreviewUrl(url)
    } catch (e) {
      setMsg({ kind: 'err', text: e instanceof Error ? e.message : String(e) })
    } finally {
      setPreviewing(false)
    }
  }

  // The title used for cover generation: the SOURCE video's probed title (owner:
  // the cover should reflect the source, NOT the manually-typed Studio title field).
  // Empty (no link / probe pending) → the auto path is unavailable, but a manual
  // prompt still enables the button (the backend only needs title on the auto path).
  const coverTitle = (probe?.title?.trim() || '')

  // Generate (or regenerate) the AI cover. Each click bumps coverStyleIndex and
  // passes seed=null so the backend varies the style — re-clicking gives a
  // different look. A manual `coverPrompt` (when set) overrides the auto prompt;
  // otherwise the TITLE alone drives it (owner: cover prompt uses the title only,
  // NOT the script/content summary).
  //
  // The backend flow is ASYNC: POST returns { taskId } immediately; we then poll
  // GET /generate/cover/progress/{taskId} every ~500ms until 'done' (store result)
  // or 'error'. Overlap/stale guard: each run captures its own `token` from
  // coverRunRef; the loop bails whenever coverRunRef.current !== token (a newer
  // click, or unmount), so a stale poll can never clobber a newer render's state.
  // A ~180s safety timeout stops a runaway poll and surfaces an error.
  // Shared poll loop for a cover-generation task. Used by BOTH makeCover (fresh
  // click) and the resume effect (a taskId restored from the draft after refresh).
  // `token` is captured from coverRunRef by the caller; the loop bails whenever
  // coverRunRef.current !== token (a newer click superseded this run, or the
  // component unmounted → ref set to -1), so a stale poll can never overwrite a
  // newer render's state. `resumed` only tweaks copy on the timeout/network paths.
  //   done   → store result (if any), 100%, stop, clear task id.
  //   error  → surface backend error, stop, clear task id.
  //   404    → task expired/GC'd or server restarted: stop QUIETLY (keep any
  //            already-persisted cover image), no scary error, clear task id.
  //   other transient fetch errors → keep retrying until the safety timeout.
  const pollCover = async (taskId: string, token: number) => {
    const isStale = () => coverRunRef.current !== token
    const POLL_MS = 500
    const TIMEOUT_MS = 180_000
    const startedAt = Date.now()
    const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))
    // eslint-disable-next-line no-constant-condition
    while (true) {
      if (isStale()) return
      if (Date.now() - startedAt > TIMEOUT_MS) {
        if (isStale()) return
        setCoverErr('Tạo cover quá lâu — đã hết thời gian chờ.')
        setCoverLoading(false)
        setCoverPromptShown('')
        setCoverTaskId(null)
        toastError('Tạo cover quá lâu')
        return
      }
      let prog: Awaited<ReturnType<typeof api.getCoverProgress>>
      try {
        prog = await api.getCoverProgress(taskId)
      } catch (e) {
        // A 404 means the task is gone (expired past the backend's ~600s window,
        // GC'd, or the server restarted). Stop QUIETLY — keep any cover already
        // shown; do NOT surface an error. Any other error is treated as a
        // transient network blip and retried until the safety timeout.
        if (e instanceof ApiError && e.status === 404) {
          if (isStale()) return
          setCoverLoading(false)
          setCoverPromptShown('')
          setCoverTaskId(null)
          return
        }
        await sleep(POLL_MS)
        continue
      }
      if (isStale()) return
      // Surface the assembled prompt as soon as the backend reports it (on any
      // running/done tick). Also fill it into the EDITABLE prompt textarea so the
      // owner can tweak the vision-crafted prompt and re-click to reuse/edit it.
      if (prog.prompt && prog.prompt.trim()) {
        setCoverPromptShown(prog.prompt)
        setCoverPrompt(prog.prompt)
      }
      if (prog.status === 'done') {
        if (prog.result) {
          setCover(prog.result)
          // A successfully generated cover is opted into the job automatically —
          // the owner asked for one, so "Sử dụng ảnh Cover" turns itself on. They
          // can still untoggle it (or clear the cover) before creating the video.
          setUseCover(true)
          // Fresh clean cover → its `basePath` becomes the title compositing base.
          // The displayed cover stays this clean image until the owner edits the
          // auto-translated title (viTitle) and clicks "Áp dụng". Prefill the
          // editable title + the keywords the backend will highlight.
          // `||` (not `??`) so an EMPTY-STRING basePath (title-only/non-vision covers
          // return basePath="") falls back to the rendered path — otherwise the base
          // stays "" and the "Áp dụng" button never enables.
          setCoverBasePath(prog.result.basePath || prog.result.path)
          setCoverText(prog.result.viTitle ?? '')
          setCoverKeyWords(prog.result.keyWords ?? [])
        }
        setCoverPct(100)
        setCoverLoading(false)
        setCoverPromptShown('')
        setCoverTaskId(null)
        return
      }
      if (prog.status === 'error') {
        setCoverErr(prog.error ?? 'Tạo cover thất bại.')
        setCoverLoading(false)
        setCoverPromptShown('')
        setCoverTaskId(null)
        toastError('Tạo cover thất bại')
        return
      }
      // running
      setCoverPct(Math.max(0, Math.min(100, prog.pct)))
      await sleep(POLL_MS)
    }
  }

  const makeCover = async () => {
    const manual = coverPrompt.trim()
    // The button is disabled unless there's either a title (auto path) or a
    // manual prompt; guard defensively.
    if (!coverTitle && !manual) return
    const nextStyle = coverStyleIndex + 1
    setCoverStyleIndex(nextStyle)
    setCoverLoading(true)
    setCoverPct(0)
    setCoverErr(null)
    setCoverPromptShown('')
    // Clear the title editor for the NEW cover — it will be re-prefilled from the
    // fresh cover's viTitle when the render completes.
    setCoverText('')
    setCoverKeyWords([])
    setCoverBasePath(null)

    // New run token — invalidates any in-flight poll from a previous click/resume.
    const token = ++coverRunRef.current
    const isStale = () => coverRunRef.current !== token

    try {
      const { taskId } = await api.generateCover({
        page: pageName,
        title: coverTitle,
        aspect,
        seed: null,
        styleIndex: nextStyle,
        prompt: manual || undefined,
        // Pass the source video link (when present) so the backend can
        // vision-analyze the source thumbnail to craft the cover prompt. Omitted
        // when empty → title-only behavior unchanged.
        sourceLink: link.trim() || undefined,
        // Pin the auto-baked title's tilt to the current slider value (default 0 /
        // flat) so a fresh "Tạo Cover" never lands on the backend's seeded random
        // tilt unless the owner manually turned coverTiltAuto on or moved the slider.
        tiltDeg: coverTiltAuto ? null : coverTilt,
      })
      if (isStale()) return
      // Persist the task id so the poll resumes across a refresh.
      setCoverTaskId(taskId)
      await pollCover(taskId, token)
    } catch (e) {
      if (isStale()) return
      setCoverErr(e instanceof Error ? e.message : String(e))
      setCoverLoading(false)
      setCoverPromptShown('')
      setCoverTaskId(null)
      toastError('Tạo cover thất bại')
    }
  }

  // Apply (or re-apply) the (edited) Vietnamese title onto the CLEAN base cover. The
  // backend always composites from `coverBasePath` and owns ALL styling (position,
  // color, gradient, plates) — the FE only sends the title text + the keywords to
  // highlight. So editing the title + clicking again re-renders from the clean base,
  // never stacking. The returned CoverResult becomes the displayed `cover` (so the
  // preview + useCover use the titled version); `coverBasePath` stays untouched.
  const applyCoverText = async () => {
    // Trim only the leading/trailing whitespace — internal "\n"s are preserved so
    // the owner's typed line breaks reach the backend as hard line breaks.
    const text = coverText.trim()
    // Base = the clean title-less image when known, else the currently displayed
    // cover's path (e.g. a browsed cover). Bail only when there is no base at all.
    const base = coverBasePath || cover?.path
    if (coverTextLoading || !text || !base) return
    setCoverTextLoading(true)
    // Fresh seed per click → a different style variation each apply (so knobs left
    // on Auto still re-roll every time).
    const seed = ++coverTitleSeedRef.current
    try {
      const result = await api.renderCoverTitle({
        page: pageName,
        basePath: base,
        text,
        keyWords: coverKeyWords,
        seed,
        // Manual overrides — send "auto"/null for any knob left on Auto so the
        // backend does its seeded/auto thing for that dimension.
        position: coverPosition,
        keyColor: coverKeyColorAuto ? null : coverKeyColor,
        // Gradient END color: only meaningful when a manual key color + gradient
        // are both on; otherwise null (auto, or gradient off → single color).
        keyColor2: (coverKeyColorAuto || !coverGradient) ? null : coverKeyColor2,
        gradient: coverGradient,
        // Text BORDER color + alignment. null/"auto" -> backend auto behavior.
        strokeColor: coverStrokeAuto ? null : coverStrokeColor,
        align: coverAlign,
        fontScale: coverFontAuto ? null : coverFontScale,
        tiltDeg: coverTiltAuto ? null : coverTilt,
      })
      setCover(result)
      success('Đã áp dụng tiêu đề lên cover')
    } catch (e) {
      toastError(e instanceof Error ? e.message : 'Áp dụng tiêu đề thất bại')
    } finally {
      setCoverTextLoading(false)
    }
  }

  // Resume the percent poll on mount if a task id was restored from the draft
  // (the user refreshed while a cover was still generating). Runs ONCE; captures a
  // fresh run token so a later manual click supersedes it. A 404 inside pollCover
  // (task expired/gone) stops quietly and leaves any already-shown cover intact.
  useEffect(() => {
    if (!coverTaskId) return
    const token = ++coverRunRef.current
    setCoverLoading(true)
    setCoverErr(null)
    void pollCover(coverTaskId, token)
    // Mount-only: intentionally not re-run when coverTaskId changes later (fresh
    // clicks are driven by makeCover, not this effect).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // On unmount, invalidate any in-flight cover poll so it stops touching state.
  useEffect(() => {
    return () => { coverRunRef.current = -1 }
  }, [])

  // `bypassGuard` skips the reuse-regenerate confirmation (used by the modal's
  // "Sinh kịch bản mới" action to proceed after the owner has acknowledged it).
  const create = async (bypassGuard = false) => {
    const trimmedLink = link.trim()
    if (!trimmedLink) {
      setMsg({ kind: 'err', text: 'Dán link nguồn trước đã.' })
      return
    }
    // Safety guard (mirrors the disabled button): never submit while a cover is
    // still generating — otherwise the cover the owner intends may not yet be
    // captured, and the job would be created with no cover.
    if (coverLoading) {
      setMsg({ kind: 'err', text: 'Đang tạo cover — chờ xong rồi tạo video.' })
      return
    }
    // Reuse guard: the previous successful create for THIS exact link reused a
    // script, but reuse is now null (it silently reset after that create and the
    // form still looks identical). Warn before regenerating a brand-new script.
    // Only fires when the link matches the remembered reuse link — a new/different/
    // empty link never matches, so genuinely fresh videos are never prompted.
    if (
      !bypassGuard &&
      reuseScriptVideoId == null &&
      lastReuseVideoIdRef.current != null &&
      lastReuseLinkRef.current === trimmedLink
    ) {
      setReuseGuard({ lastVideoId: lastReuseVideoIdRef.current })
      return
    }
    setCreating(true)
    setMsg(null)
    try {
      // Snapshot the cover for THIS submission into a local const, so nothing the
      // owner does afterwards (e.g. generating a new cover for the next video)
      // can change what this job was created with. The backend stores the path
      // immutably; this just guarantees the value we send is a stable snapshot.
      const submittedCover = cover
      const useCoverNow = useCover && !!submittedCover
      const created = await api.createJob({ pageId, link: link.trim(), title: title.trim() || undefined, voice: voiceKey || null, editMode, renderModel, voiceCloneModel, ...llmPayload, aspect, targetSec: autoDuration ? null : targetSec, addCredit, srcAudioVolume, publish, publishPlatform: publish ? publishPlatform : null, reuseScriptVideoId: reuseScriptVideoId ?? null, bypassTtsCache: reuseScriptVideoId != null ? bypassTtsCache : undefined, bypassScriptCache: true, useCover: useCoverNow || undefined, coverImagePath: useCoverNow ? submittedCover!.path : undefined })
      // The cover for THIS job is already snapshotted (submittedCover) and sent to
      // the backend above, so the editing cover state is now safe to clear — the
      // owner wants a clean cover state for the NEXT video the moment they hit
      // "Tạo video", not a leftover preview from the last create. The actual reset
      // happens in the post-create field reset below (alongside the other per-job
      // fields). No clear-on-done watch is needed anymore; neutralize its trackers
      // so the (now defunct) done-effect can't fire against a stale/new cover.
      submittedCoverPathRef.current = null
      clearedCoverJobIdRef.current = null
      // Remember whether THIS create reused a script, keyed by the exact link, so
      // the next submit can detect a silent reuse reset (see the guard above).
      // Refs so they survive the post-create field reset without joining the draft.
      // If this create did NOT reuse, clear the memory so a later same-link regen
      // isn't wrongly flagged.
      if (reuseScriptVideoId != null) {
        lastReuseLinkRef.current = link.trim()
        lastReuseVideoIdRef.current = reuseScriptVideoId
      } else {
        lastReuseLinkRef.current = null
        lastReuseVideoIdRef.current = null
      }
      setCreatedJobId(created.id)
      await onCreated()
      // Flush the page's SAVED source-list rows (status 'held') into the queue so
      // they run behind this freshly-created job. Non-fatal: a release failure must
      // NOT break the main create success path — surface a soft toast and move on.
      try {
        const rel = await api.releaseJobs(pageId)
        if (rel.released > 0) {
          success(`Đã đưa ${rel.released} nguồn đã lưu vào hàng đợi`)
          await onCreated()
        }
      } catch (relErr) {
        toastError(`Không thể chạy các nguồn đã lưu: ${relErr instanceof Error ? relErr.message : String(relErr)}`)
      }
      setMsg({ kind: 'ok', text: 'Đã thêm vào hàng đợi. Pipeline sẽ tự xử lý.' })
      // Reset the per-job creation fields back to defaults after a successful
      // create, so the form is ready for the next video instead of carrying over
      // the last-used selection. Also wipe the cached draft so a later refresh
      // starts clean (the persist-on-change effect re-saves the defaults next tick).
      try {
        localStorage.removeItem(STUDIO_DRAFT_KEY)
      } catch {
        /* storage unavailable — nothing to clear */
      }
      setLink(STUDIO_DEFAULTS.link)
      setTitle(STUDIO_DEFAULTS.title)
      setEditMode(STUDIO_DEFAULTS.editMode)
      setRenderModel(STUDIO_DEFAULTS.renderModel)
      setVoiceCloneModel(STUDIO_DEFAULTS.voiceCloneModel)
      // Script-gen LLM: back to the owner's ★-pinned default when it is still on
      // offer, else the backend's is_default row. Resolved here instead of blanking
      // to STUDIO_DEFAULTS.llmKey ('') because the options fetch is mount-only and
      // would not re-resolve an empty key after this reset.
      {
        const pinnedLlmKey = getDefaultPref(LLM_SETTING_KEY)
        const fallbackLlm = llmOptions?.find((o) => o.is_default) ?? llmOptions?.[0]
        setLlmKey(
          pinnedLlmKey && llmOptions?.some((o) => llmOptionKey(o) === pinnedLlmKey)
            ? pinnedLlmKey
            : fallbackLlm
              ? llmOptionKey(fallbackLlm)
              : STUDIO_DEFAULTS.llmKey,
        )
      }
      setAspect(STUDIO_DEFAULTS.aspect)
      setTargetSec(STUDIO_DEFAULTS.targetSec)
      setAutoDuration(STUDIO_DEFAULTS.autoDuration)
      setAddCredit(STUDIO_DEFAULTS.addCredit)
      setSrcAudioVolume(STUDIO_DEFAULTS.srcAudioVolume)
      setVoiceKey('')
      setReusedScript(null)
      setReusedScriptEdited(false)
      setReuseMode('fresh-audio')
      setAudioMismatchAck(false)
      setPublish(false)
      setPublishPlatform(null)
      setProbe(null)
      setPreviewUrl(null)
      // Clear the cover state NOW that the job is queued — the owner wants each
      // new "Tạo video" to start from a clean cover (no leftover preview from the
      // last video). Safe because the submitted cover was already snapshotted and
      // sent to the backend above. Invalidate any in-flight cover poll first so it
      // can't write back onto the freshly-cleared state.
      coverRunRef.current = -1
      setCover(null)
      setUseCover(false)
      setCoverPrompt('')
      setCoverStyleIndex(STUDIO_DEFAULTS.coverStyleIndex)
      setCoverErr(null)
      setCoverPct(0)
      setCoverTaskId(null)
      setCoverPromptShown('')
      // Reset the title base + editable title + keywords + manual knobs (→ Auto)
      // for the next video.
      setCoverBasePath(null)
      setCoverText('')
      setCoverKeyWords([])
      setCoverPosition(STUDIO_DEFAULTS.coverPosition)
      setCoverAlign(STUDIO_DEFAULTS.coverAlign)
      setCoverStrokeColor(STUDIO_DEFAULTS.coverStrokeColor)
      setCoverStrokeAuto(STUDIO_DEFAULTS.coverStrokeAuto)
      setCoverKeyColor(STUDIO_DEFAULTS.coverKeyColor)
      setCoverKeyColor2(STUDIO_DEFAULTS.coverKeyColor2)
      setCoverKeyColorAuto(STUDIO_DEFAULTS.coverKeyColorAuto)
      setCoverGradient(STUDIO_DEFAULTS.coverGradient)
      setCoverFontScale(STUDIO_DEFAULTS.coverFontScale)
      setCoverFontAuto(STUDIO_DEFAULTS.coverFontAuto)
      setCoverTilt(STUDIO_DEFAULTS.coverTilt)
      setCoverTiltAuto(STUDIO_DEFAULTS.coverTiltAuto)
      success('Đã thêm job vào hàng đợi')
    } catch (e) {
      setMsg({ kind: 'err', text: e instanceof Error ? e.message : String(e) })
      toastError('Không tạo được job')
    } finally {
      setCreating(false)
    }
  }

  const thumbSrc = probe?.thumbnail ?? (vid ? `https://img.youtube.com/vi/${vid}/hqdefault.jpg` : null)

  return (
    <Card className="p-5">
      <SectionTitle sub="Dán link nguồn, chọn giọng đọc, cách biên tập và tỷ lệ khung hình, rồi thêm job vào hàng đợi.">
        Tạo video
      </SectionTitle>

      <div className="grid gap-4 lg:grid-cols-[1fr_810px]">
        {/* Left: controls */}
        <div className="space-y-4">
          <Field
            label="Tên video"
            hint="Tiêu đề video sẽ tạo. Để trống sẽ dùng tiêu đề video nguồn."
          >
            <TextInput
              value={title}
              onChange={setTitle}
              placeholder="Tiêu đề video sẽ tạo…"
              className="w-full"
            />
          </Field>

          {/* Platform + format-tier picker (auto-applies aspect + duration).
              Placed above "Kịch bản" so the owner picks the target platform/format first. */}
          <Field
            label="Nền tảng & loại video"
            hint="Chọn nền tảng đã liên kết rồi chọn loại video — tỷ lệ khung hình và độ dài sẽ tự điều chỉnh (vẫn chỉnh tay được)."
          >
            <PlatformTierPicker specs={specs} onApply={applyTier} onPlatformChange={setPublishPlatform} />
          </Field>

          {/* PART B — Reuse a previously-generated script (skip script-gen). */}
          <Field
            label="Kịch bản"
            hint="Mặc định pipeline tự viết kịch bản. Bạn có thể dùng lại một kịch bản đã tạo trước đó để bỏ qua bước này."
          >
            {!reusedScript ? (
              <Button variant="outline" onClick={() => setShowScriptPicker(true)} className="w-full justify-start">
                <FileText className="h-4 w-4" /> Dùng kịch bản đã tạo trước đó
              </Button>
            ) : (
              <div className="space-y-2">
                <div className="flex items-start justify-between gap-2 rounded-lg border border-brand/40 bg-brand/10 px-3 py-2">
                  <button
                    type="button"
                    onClick={() => setShowScriptPicker(true)}
                    className="min-w-0 flex-1 text-left"
                    title="Click để đổi kịch bản"
                  >
                    <p className="flex items-center gap-1.5 text-sm font-medium text-brand">
                      <RotateCcw className="h-4 w-4 shrink-0" />
                      <span className="truncate">
                        Đang dùng lại kịch bản: {reusedScript.title?.trim() || reusedScript.sourceName?.trim() || `Video #${reusedScript.videoId}`}
                      </span>
                    </p>
                    <p className="mt-0.5 text-[11px] text-muted">
                      {/* Dubbed scripts have sceneCount 0 — show the mode, not "0 cảnh". */}
                      {reusedScript.editMode === 'dubbed' ? 'Lồng tiếng (phụ đề)' : `${reusedScript.sceneCount} cảnh`}
                      {reusedScript.renderMode ? ` · ${SCRIPT_MODE_LABEL[reusedScript.renderMode] ?? reusedScript.renderMode}` : ''}
                      {' '}· pipeline sẽ bỏ qua bước viết kịch bản.
                    </p>
                  </button>
                  <button
                    type="button"
                    onClick={() => { setReusedScript(null); setReusedScriptEdited(false); setReuseMode('fresh-audio'); setAudioMismatchAck(false) }}
                    aria-label="Bỏ dùng lại kịch bản"
                    className="grid h-6 w-6 shrink-0 place-items-center rounded text-muted transition hover:bg-panel2 hover:text-fg"
                  >
                    <X className="h-4 w-4" />
                  </button>
                </div>
                {crossModeWarning && (
                  <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] leading-relaxed text-amber-600 dark:text-amber-300">
                    Cảnh báo: kịch bản này được tạo cho chế độ
                    {' '}<b>{reusedMode ? SCRIPT_MODE_LABEL[reusedMode] ?? reusedMode : reusedMode}</b>, khác với model dựng đang chọn
                    {' '}(<b>{SCRIPT_MODE_LABEL[currentMode]}</b>). Kịch bản giữ footage mang mốc thời gian (sourceStart/sourceEnd),
                    còn kịch bản ảnh AI / stickman mang mô tả ảnh (image_prompt) — dùng chéo có thể làm video dựng bị lỗi.
                    Chỉ tiếp tục nếu bạn chấp nhận rủi ro này.
                  </p>
                )}

                {/* Reuse-mode chooser: two distinct paths off the picked script.
                    Button 1 (fresh-audio) re-synthesizes TTS (bypass cache) — the
                    expected path after editing the script; no warning.
                    Button 2 (with-audio) lets the TTS cache serve existing audio;
                    if the script was edited this session, the cache would serve
                    stale audio, so we warn (dismissible). */}
                <div className="flex flex-col gap-1.5 sm:flex-row">
                  <button
                    type="button"
                    onClick={() => { setReuseMode('fresh-audio'); setAudioMismatchAck(false); }}
                    className={`flex-1 rounded-lg border px-3 py-2 text-left text-xs transition ${
                      reuseMode === 'fresh-audio'
                        ? 'border-brand/60 bg-brand/10 text-fg'
                        : 'border-line bg-panel2 text-muted hover:border-brand/40 hover:text-fg'
                    }`}
                  >
                    <span className="flex items-center gap-1.5 font-medium">
                      {reuseMode === 'fresh-audio' && <Check className="h-3.5 w-3.5 text-brand" />}
                      Dùng lại kịch bản
                    </span>
                    <span className="mt-0.5 block text-[10px] leading-relaxed text-muted">
                      Dùng lại nội dung kịch bản, tạo lại giọng đọc mới (xóa cache audio cũ).
                    </span>
                  </button>
                  <button
                    type="button"
                    disabled={!audioCached}
                    onClick={() => { if (audioCached) setReuseMode('with-audio') }}
                    className={`flex-1 rounded-lg border px-3 py-2 text-left text-xs transition ${
                      !audioCached
                        ? 'cursor-not-allowed border-line bg-panel2 text-muted opacity-50'
                        : reuseMode === 'with-audio'
                          ? 'border-brand/60 bg-brand/10 text-fg'
                          : 'border-line bg-panel2 text-muted hover:border-brand/40 hover:text-fg'
                    }`}
                  >
                    <span className="flex items-center gap-1.5 font-medium">
                      {audioCached && reuseMode === 'with-audio' && <Check className="h-3.5 w-3.5 text-brand" />}
                      Dùng lại kịch bản và audio
                      {!audioCached && <span className="font-normal text-muted">(không có cache audio)</span>}
                    </span>
                    <span className="mt-0.5 block text-[10px] leading-relaxed text-muted">
                      Dùng lại giọng đọc đã lưu (cache audio — nhanh, không chạy GPU). Nếu kịch bản đã sửa, audio vẫn tạo lại.
                    </span>
                  </button>
                </div>

                {/* Audio-mismatch warning: when the picked script's cached audio is
                    stale — either edited inline this session, or flagged edited in a
                    previous session (audioStale). The old cache is cleared and audio
                    re-synthesized automatically. */}
                {audioStale && (
                  <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] leading-relaxed text-amber-700 dark:text-amber-200">
                    Kịch bản đã sửa — cache audio cũ sẽ bị xóa, audio mới sẽ được tạo lại tự động.
                  </div>
                )}
              </div>
            )}
          </Field>

          {/* Script-gen LLM. Options are fetched at runtime (they depend on which
              provider keys the backend holds), so the list can be a single row —
              that is a valid state, not a broken dropdown. Options flagged
              reliability:'low' get a "— thử nghiệm" suffix plus a warning box under
              the field, so a free-tier model is never picked unknowingly. */}
          <Field
            label="Model AI viết kịch bản"
            hint={selectedLlm?.notes ?? 'Model sinh kịch bản cho video. Mặc định dùng Claude (gói thuê bao).'}
          >
            <Select value={llmKey} onChange={setLlmKey} settingKey={LLM_SETTING_KEY} autoApplyDefault>
              {llmOptions == null ? (
                <option value={llmKey}>Đang tải…</option>
              ) : llmOptions.length === 0 ? (
                <option value="">Mặc định của hệ thống</option>
              ) : (
                llmOptions.map((o) => (
                  <option key={llmOptionKey(o)} value={llmOptionKey(o)}>
                    {o.label}{o.reliability === 'low' ? ' — thử nghiệm' : ''}
                  </option>
                ))
              )}
            </Select>
            {selectedLlm?.reliability === 'low' && (
              <div className="mt-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] leading-relaxed text-amber-700 dark:text-amber-200">
                Model thử nghiệm (free-tier) — hay viết không xong kịch bản trong giới hạn cho phép và làm job lỗi. Chỉ dùng khi Claude không dùng được.
              </div>
            )}
          </Field>

          <Field label="Giọng đọc" hint="Chỉ hiển thị các giọng bạn đã clone cho trang này. Bấm + để clone giọng mới.">
            <div className="flex items-center gap-2">
              <VoicePicker value={voiceKey} onChange={setVoiceKey} voices={voices} page={pageName} onDeleted={onVoicesChanged} className="flex-1" settingKey={VOICE_DEFAULT_SETTING_KEY} />
              <Button variant="outline" onClick={playPreview} disabled={previewing || !selected} className="shrink-0">
                {previewing || (!!voiceKey && !selected) ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                Nghe thử
              </Button>
              <Button variant="outline" onClick={() => setShowAddVoice(true)} className="shrink-0">
                <Mic className="h-4 w-4" /> Thêm
              </Button>
            </div>
            {previewUrl && (
              // eslint-disable-next-line jsx-a11y/media-has-caption
              <audio src={previewUrl} controls autoPlay className="mt-2 h-9 w-full" />
            )}
          </Field>

          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              label="Cách biên tập"
              hint={EDIT_MODES.find((m) => m.value === editMode)?.desc}
            >
              <Select value={editMode} onChange={setEditMode} settingKey="studio.editMode" autoApplyDefault>
                {EDIT_MODES.map((m) => (
                  <option key={m.value} value={m.value}>
                    {m.label}
                  </option>
                ))}
              </Select>
            </Field>

            <Field label="Model dựng (engine)" hint={RENDER_MODELS.find((m) => m.value === renderModel)?.desc}>
              <Select value={renderModel} onChange={setRenderModel} settingKey="studio.renderModel" autoApplyDefault>
                {Array.from(new Set(RENDER_MODELS.map((m) => m.group))).map((g) => (
                  <optgroup key={g} label={g}>
                    {RENDER_MODELS.filter((m) => m.group === g).map((m) => (
                      <option key={m.value} value={m.value}>
                        {m.label}{INSTALLED_RENDER_MODELS.has(m.value) ? '' : ' — chưa cài'}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </Select>
            </Field>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Tỷ lệ khung hình">
              <Select value={aspect} onChange={setAspect} settingKey="studio.aspect" autoApplyDefault>
                {ASPECT_OPTIONS.map((a) => (
                  <option key={a.value} value={a.value}>
                    {a.label}
                  </option>
                ))}
              </Select>
            </Field>

            <Field label="Model lồng tiếng" hint={VOICE_CLONE_MODELS.find((m) => m.value === voiceCloneModel)?.desc}>
              <Select value={voiceCloneModel} onChange={setVoiceCloneModel} settingKey="studio.voiceCloneModel" autoApplyDefault>
                {VOICE_CLONE_MODELS.map((m) => (
                  <option key={m.value} value={m.value}>
                    {m.label}
                  </option>
                ))}
              </Select>
            </Field>
          </div>

          {/* Row 1: LEFT column stacks "Audio gốc" + "Độ dài video đích" so its
              combined height matches the tall cover-controls column on the RIGHT,
              avoiding the empty void that appeared when a lone short field sat next
              to the taller cover column. The prompt input's w-full is naturally
              narrowed by the grid cell so it aligns evenly. The Tạo Cover button is
              enabled when there is a usable title OR a manual prompt (the backend
              only requires a title on the auto path). Single column on mobile via
              sm:grid-cols-2. */}
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="flex min-w-0 flex-col gap-4">
              <Field label="Audio gốc" hint="Mặc định tắt audio gốc; chọn % nếu muốn giữ nhẹ tiếng nền của video nguồn.">
                <Select value={String(srcAudioVolume)} onChange={(v) => setSrcAudioVolume(Number(v))} settingKey="studio.srcAudioVolume" autoApplyDefault>
                  {SRC_AUDIO_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </Select>
              </Field>

              {/* Độ dài video đích inputs (the auto-duration toggle lives on the
                  checkbox group just below in this same left column, next to
                  "Thêm nguồn"). Kept in the left column to balance the grid row height
                  against the cover controls. */}
              <Field
                label={
                  autoDuration
                    ? 'Độ dài video đích — Tự động'
                    : `Độ dài video đích — ${Math.floor(targetSec / 60)}:${String(targetSec % 60).padStart(2, '0')}`
                }
                hint={
                  reusedScript
                    ? 'Không áp dụng khi dùng lại kịch bản đã lưu — độ dài theo kịch bản đã cố định.'
                    : autoDuration
                      ? 'Độ dài theo video gốc; nếu là chế độ Tóm tắt sẽ cắt bớt phần thừa.'
                      : 'Cô đọng nguồn vào độ dài này; chỉnh phút và giây (từ 1 giây đến 50 phút).'
                }
              >
                {/* In auto/reused states the inputs are non-editable; collapse the row
                    entirely so the block doesn't leave a large empty area. The Field
                    label + hint already convey the auto behavior. */}
                {autoDuration || reusedScript ? null : (
                  <div className="flex h-9 items-center gap-2">
                    <input
                      type="number"
                      min={0}
                      max={50}
                      step={1}
                      value={Math.floor(targetSec / 60)}
                      onChange={(e) => {
                        // Recombine minutes + current seconds, clamp to 1s..50min.
                        const mins = Number.isFinite(e.target.valueAsNumber) ? Math.max(0, Math.floor(e.target.valueAsNumber)) : 0
                        const secs = targetSec % 60
                        setTargetSec(Math.min(MAX_TARGET_SEC, Math.max(MIN_TARGET_SEC, mins * 60 + secs)))
                      }}
                      disabled={autoDuration || !!reusedScript}
                      className="h-9 w-16 rounded-lg border border-line bg-panel px-2 text-sm text-fg outline-none transition focus:border-brand/50 focus:ring-2 focus:ring-brand/20 disabled:cursor-not-allowed"
                      aria-label="Độ dài video đích (phút)"
                    />
                    <span className="shrink-0 text-[11px] text-muted">phút</span>
                    <input
                      type="number"
                      min={0}
                      max={59}
                      step={1}
                      value={targetSec % 60}
                      onChange={(e) => {
                        // Recombine current minutes + seconds, clamp to 1s..50min.
                        const mins = Math.floor(targetSec / 60)
                        const secs = Number.isFinite(e.target.valueAsNumber) ? Math.min(59, Math.max(0, Math.floor(e.target.valueAsNumber))) : 0
                        setTargetSec(Math.min(MAX_TARGET_SEC, Math.max(MIN_TARGET_SEC, mins * 60 + secs)))
                      }}
                      disabled={autoDuration || !!reusedScript}
                      className="h-9 w-16 rounded-lg border border-line bg-panel px-2 text-sm text-fg outline-none transition focus:border-brand/50 focus:ring-2 focus:ring-brand/20 disabled:cursor-not-allowed"
                      aria-label="Độ dài video đích (giây)"
                    />
                    <span className="shrink-0 text-[11px] text-muted">giây</span>
                  </div>
                )}
              </Field>

              {/* Checkboxes + auto-publish block live in the LEFT column (moved out of
                  the below-grid full-width flow) so a freely-growing cover textarea in
                  the RIGHT column never shifts them: the grid row grows but this
                  top-anchored column keeps its content in place. Extra height from a
                  tall textarea appears as empty space below the publish block. */}
              {/* Two checkboxes stacked vertically: auto-duration then add-source-credit. */}
              <div className="flex flex-col gap-2">
                <label className={`flex items-center gap-2.5 text-sm ${reusedScript ? 'cursor-not-allowed opacity-40' : 'cursor-pointer'}`}>
                  <input
                    type="checkbox"
                    checked={autoDuration || !!reusedScript}
                    onChange={(e) => setAutoDuration(e.target.checked)}
                    disabled={!!reusedScript}
                    className="h-4 w-4 rounded border-line bg-panel2 accent-[var(--color-brand)] disabled:cursor-not-allowed"
                  />
                  <span>Tự động độ dài (theo video gốc)</span>
                </label>

                <label className="flex cursor-pointer items-center gap-2.5 text-sm">
                  <input
                    type="checkbox"
                    checked={addCredit}
                    onChange={(e) => setAddCredit(e.target.checked)}
                    className="h-4 w-4 rounded border-line bg-panel2 accent-[var(--color-brand)]"
                  />
                  <span>Thêm nguồn (logo + @handle) ở cuối video</span>
                </label>
              </div>

              <div>
                <label className="flex cursor-pointer items-center gap-2.5 text-sm">
                  <input
                    type="checkbox"
                    checked={publish}
                    onChange={(e) => setPublish(e.target.checked)}
                    className="h-4 w-4 rounded border-line bg-panel2 accent-[var(--color-brand)]"
                  />
                  <span>Tự động publish</span>
                </label>
                {(() => {
                  // Off → static manual-publish hint.
                  if (!publish) {
                    return <p className="ml-[26px] mt-1 text-[11px] text-muted">Tắt: chỉ tạo video, đăng thủ công sau ở mục Video.</p>
                  }
                  // On but no platform picked → warning tone, won't auto-publish.
                  if (!publishPlatform) {
                    return (
                      <p className="ml-[26px] mt-1 text-[11px] text-amber-500">
                        Chưa chọn nền tảng ở trên — sẽ KHÔNG tự đăng. Hãy chọn nền tảng để bật tự đăng.
                      </p>
                    )
                  }
                  // On + platform picked, but that platform isn't linked on THIS page →
                  // backend will skip publishing. Surface it honestly.
                  if (pageLinkedPlatforms && !pageLinkedPlatforms.has(publishPlatform)) {
                    return (
                      <p className="ml-[26px] mt-1 text-[11px] text-amber-500">
                        Nền tảng này chưa liên kết ở trang đang chọn — sẽ không tự đăng.
                      </p>
                    )
                  }
                  // On + platform picked (and linked here, or link state unknown) → will publish.
                  const platformLabel =
                    specs?.find((s) => s.platform === publishPlatform)?.label ?? publishPlatform
                  return (
                    <p className="ml-[26px] mt-1 text-[11px] text-muted">
                      Sẽ tự đăng lên: {platformLabel} (kênh của trang đang chọn).
                    </p>
                  )
                })()}
              </div>
            </div>

            {/* Cover column. On mobile (single column) render it FIRST via order-first
                so cover controls stay near the top; sm:order-none restores the natural
                left-then-right order on desktop (where anchoring the checkboxes in the
                left column is what matters). */}
            <div className="order-first flex min-w-0 flex-col gap-2 sm:order-none">
              {/* Column title, aligned horizontally with the "Audio gốc" Field label
                  on the left. Uses the same label typography (mb-1.5 text-xs font-medium
                  text-muted) so the two column headers line up cleanly. Always shown so
                  the layout never jumps. */}
              <span className="mb-1.5 block text-xs font-medium text-muted">Nhập tiêu đề hoặc dán link trước</span>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  variant="outline"
                  onClick={makeCover}
                  disabled={coverLoading || (!coverTitle && !coverPrompt.trim())}
                  className="shrink-0"
                >
                  {coverLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <ImageIcon className="h-4 w-4" />}
                  {coverLoading
                    ? 'Đang tạo cover…'
                    : 'Tạo Cover'}
                </Button>
                <Button
                  variant="outline"
                  onClick={() => setShowSavedCovers(true)}
                  disabled={coverLoading || !pageName}
                  className="shrink-0"
                >
                  <FolderOpen className="h-4 w-4" /> Duyệt
                </Button>
              </div>
              {/* Thin progress bar while a cover generates. */}
              {coverLoading && (
                <div className="h-1 w-full overflow-hidden rounded-full bg-panel2">
                  <div
                    className="h-full rounded-full bg-brand transition-all duration-300"
                    style={{ width: `${Math.max(2, coverPct)}%` }}
                  />
                </div>
              )}
              {/* The actual prompt sent to SDXL, shown while generating. */}
              {coverLoading && coverPromptShown && (
                <p className="line-clamp-3 whitespace-pre-wrap break-words text-[11px] text-muted">
                  <span className="font-medium">Prompt:</span> {coverPromptShown}
                </p>
              )}
              {/* Expandable multi-line prompt. No Textarea primitive exists in ui.tsx,
                  so this native <textarea> reuses the same field styling as TextInput
                  (border/bg/text/focus tokens) minus the fixed h-9, and starts at ~2
                  rows but is user-resizable vertically (resize-y).

                  NO HEIGHT CAP: the textarea grows freely to any size the user drags.
                  The 3 checkboxes are NOT affected because they now live in the LEFT
                  grid column (with "Audio gốc" + "Độ dài"). Growing this RIGHT cell only
                  grows the shared grid-row height; the left column stays top-anchored, so
                  the checkboxes keep their position (extra height is empty space at the
                  bottom of the left column). */}
              <textarea
                value={coverPrompt}
                onChange={(e) => setCoverPrompt(e.target.value)}
                placeholder="Để trống sẽ tự tạo từ tiêu đề + nội dung"
                rows={2}
                disabled={coverLoading}
                className="w-full min-h-[4.5rem] resize-y rounded-lg border border-line bg-panel px-3 py-2 text-sm text-fg outline-none transition placeholder:text-muted/70 focus:border-brand/50 focus:ring-2 focus:ring-brand/20 disabled:cursor-not-allowed disabled:opacity-50"
              />
              <div className="flex items-center justify-between">
                <span className="text-[11px] text-muted/80">Prompt ảnh Cover (tuỳ chọn)</span>
                {coverPrompt.trim() && !coverLoading && (
                  <button
                    type="button"
                    onClick={() => setCoverPrompt('')}
                    className="inline-flex items-center gap-1 text-[11px] text-muted/80 transition hover:text-rose-400"
                  >
                    <X className="h-3 w-3" /> Xoá nội dung
                  </button>
                )}
              </div>

              {/* Title panel — only once a cover exists. The owner edits the
                  auto-translated Vietnamese title; the backend re-renders it (owning
                  ALL styling — position/color/gradient/plates) from the CLEAN base
                  (coverBasePath), so re-applying an edited title never stacks. The
                  result replaces the displayed `cover`. */}
              {cover && (
                <div className="mt-1 space-y-2.5 rounded-lg border border-line bg-panel2/40 p-3">
                  <div className="flex items-center gap-1.5 text-xs font-medium text-fg">
                    <Type className="h-3.5 w-3.5 text-muted" /> Chữ trên ảnh cover
                  </div>
                  <textarea
                    value={coverText}
                    onChange={(e) => setCoverText(e.target.value)}
                    placeholder="Tiêu đề tiếng Việt hiển thị trên cover (xuống dòng để ngắt dòng)"
                    rows={2}
                    className="w-full resize-y rounded-lg border border-line bg-panel px-3 py-2 text-sm text-fg outline-none transition placeholder:text-muted/70 focus:border-brand/50 focus:ring-2 focus:ring-brand/20"
                  />
                  <p className="text-[11px] text-muted/80">Mẹo: bao chữ trong "..." hoặc '...' để chữ đó có nền.</p>
                  <div className="flex flex-wrap items-center gap-2">
                    <Button
                      variant="outline"
                      onClick={applyCoverText}
                      disabled={coverTextLoading || !coverText.trim() || !cover}
                      className="shrink-0"
                    >
                      {coverTextLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Type className="h-4 w-4" />}
                      {coverTextLoading ? 'Đang áp dụng…' : 'Áp dụng'}
                    </Button>
                    <span className="text-[11px] text-muted/80">Sửa tiêu đề rồi bấm lại để render lại từ ảnh gốc.</span>
                  </div>

                  {/* Manual title-style knobs. Each defaults to Auto (backend does its
                      seeded/auto thing); pinning a knob overrides only that dimension.
                      A fresh seed is sent each "Áp dụng", so knobs left on Auto still
                      re-roll a new variation every click. */}
                  <div className="space-y-3 border-t border-line/60 pt-2.5">
                    {/* Position ("Auto" toggle + 3×3 anchor grid) and Căn chữ, side by side.
                        items-START (not items-end): the anchor grid is ~3x taller than the
                        align row, so bottom-aligning dropped "Căn chữ" to the grid's bottom
                        edge and it read as a separate row. Top-aligning puts both LABELS on
                        the same line, which is what the owner asked for. */}
                    <div className="flex flex-wrap items-start gap-4">
                      <div>
                        <span className="mb-1 block text-[11px] text-muted">Vị trí</span>
                        <div className="flex items-start gap-2">
                          <button
                            type="button"
                            onClick={() => setCoverPosition('auto')}
                            aria-pressed={coverPosition === 'auto'}
                            className={`h-6 rounded border px-2 text-[11px] font-medium transition ${
                              coverPosition === 'auto' ? 'border-brand bg-brand/20 text-fg' : 'border-line bg-panel text-muted hover:border-brand/40'
                            }`}
                          >
                            Auto
                          </button>
                          <div className="grid grid-cols-3 gap-1">
                            {COVER_TITLE_ANCHORS.map((pos) => {
                              const active = coverPosition === pos
                              const [vert, horiz] = pos.split('-')
                              const vAlign = vert === 'top' ? 'items-start' : vert === 'center' ? 'items-center' : 'items-end'
                              const hAlign = horiz === 'left' ? 'justify-start' : horiz === 'right' ? 'justify-end' : 'justify-center'
                              return (
                                <button
                                  key={pos}
                                  type="button"
                                  onClick={() => setCoverPosition(pos)}
                                  aria-label={pos}
                                  aria-pressed={active}
                                  title={pos}
                                  className={`flex h-6 w-6 rounded border p-1 transition ${vAlign} ${hAlign} ${
                                    active ? 'border-brand bg-brand/20' : 'border-line bg-panel hover:border-brand/40'
                                  }`}
                                >
                                  <span className={`block h-1 w-2.5 rounded-sm ${active ? 'bg-brand' : 'bg-muted/60'}`} />
                                </button>
                              )
                            })}
                          </div>
                        </div>
                      </div>

                      {/* Căn chữ: how the rows align inside the title column. Auto =
                          backend's centered + seeded jitter; left/right flush every row
                          to that edge (and align the lines inside a multi-line row). */}
                      <div>
                        <span className="mb-1 block text-[11px] text-muted">Căn chữ</span>
                        <div className="flex items-center gap-1">
                          {([
                            ['auto', 'Auto', null],
                            ['left', 'Trái', AlignLeft],
                            ['center', 'Giữa', AlignCenter],
                            ['right', 'Phải', AlignRight],
                          ] as const).map(([val, label, Icon]) => {
                            const active = coverAlign === val
                            return (
                              <button
                                key={val}
                                type="button"
                                onClick={() => setCoverAlign(val)}
                                aria-pressed={active}
                                title={label}
                                aria-label={label}
                                className={`flex h-6 items-center gap-1 rounded border px-2 text-[11px] font-medium transition ${
                                  active ? 'border-brand bg-brand/20 text-fg' : 'border-line bg-panel text-muted hover:border-brand/40'
                                }`}
                              >
                                {Icon ? <Icon className="h-3.5 w-3.5" /> : label}
                              </button>
                            )
                          })}
                        </div>
                      </div>
                    </div>

                    {/* Plate màu (gradient): key color (start) + gradient-end color +
                        its Auto toggle + gradient checkbox. The END color only matters
                        when NOT Auto and Gradient is ON, so it greys out otherwise. */}
                    <div className="flex flex-wrap items-end gap-4">
                      <div>
                        <span className="mb-1 block text-[11px] text-muted">Plate màu gradient</span>
                        <div className="flex items-end gap-1.5">
                          {/* Màu 1 (gradient START) */}
                          <div>
                            <span className="mb-0.5 block text-[10px] text-muted/70">Từ</span>
                            <input
                              type="color"
                              value={coverKeyColor}
                              onChange={(e) => setCoverKeyColor(e.target.value)}
                              disabled={coverKeyColorAuto}
                              aria-label="Màu plate 1 (từ)"
                              className="h-8 w-10 cursor-pointer rounded border border-line bg-panel disabled:cursor-not-allowed disabled:opacity-40"
                            />
                          </div>
                          {/* Màu 2 (gradient END) — only relevant when manual + gradient on. */}
                          <div className={coverKeyColorAuto || !coverGradient ? 'pointer-events-none opacity-40' : ''}>
                            <span className="mb-0.5 block text-[10px] text-muted/70">Đến</span>
                            <input
                              type="color"
                              value={coverKeyColor2}
                              onChange={(e) => setCoverKeyColor2(e.target.value)}
                              disabled={coverKeyColorAuto || !coverGradient}
                              aria-label="Màu plate 2 (đến)"
                              className="h-8 w-10 cursor-pointer rounded border border-line bg-panel disabled:cursor-not-allowed disabled:opacity-40"
                            />
                          </div>
                          <button
                            type="button"
                            onClick={() => setCoverKeyColorAuto((a) => !a)}
                            aria-pressed={coverKeyColorAuto}
                            className={`h-8 rounded border px-2 text-xs font-medium transition ${
                              coverKeyColorAuto ? 'border-brand bg-brand/20 text-fg' : 'border-line bg-panel text-muted hover:border-brand/40'
                            }`}
                          >
                            Auto
                          </button>
                        </div>
                      </div>
                      <div>
                        <span className="mb-1 block text-[11px] text-muted">Gradient</span>
                        <button
                          type="button"
                          onClick={() => setCoverGradient((g) => !g)}
                          aria-pressed={coverGradient}
                          className={`h-8 rounded border px-3 text-xs font-medium transition ${
                            coverGradient ? 'border-brand bg-brand/20 text-fg' : 'border-line bg-panel text-muted hover:border-brand/40'
                          }`}
                        >
                          {coverGradient ? 'Bật' : 'Tắt'}
                        </button>
                      </div>

                      {/* Plate màu border: the text OUTLINE color for every row. Auto lets
                          the backend pick a contrasting outline; a manual pick is honored
                          verbatim (the backend skips its contrast guard). */}
                      <div>
                        <span className="mb-1 block text-[11px] text-muted">Plate màu border</span>
                        <div className="flex items-end gap-1.5">
                          <input
                            type="color"
                            value={coverStrokeColor}
                            onChange={(e) => setCoverStrokeColor(e.target.value)}
                            disabled={coverStrokeAuto}
                            aria-label="Màu border chữ"
                            className="h-8 w-10 cursor-pointer rounded border border-line bg-panel disabled:cursor-not-allowed disabled:opacity-40"
                          />
                          <button
                            type="button"
                            onClick={() => setCoverStrokeAuto((a) => !a)}
                            aria-pressed={coverStrokeAuto}
                            className={`h-8 rounded border px-2 text-xs font-medium transition ${
                              coverStrokeAuto ? 'border-brand bg-brand/20 text-fg' : 'border-line bg-panel text-muted hover:border-brand/40'
                            }`}
                          >
                            Auto
                          </button>
                        </div>
                      </div>
                    </div>

                    {/* Font size + tilt sliders, each with an Auto toggle. */}
                    <div className="flex flex-wrap items-end gap-4">
                      <div className="min-w-[10rem] flex-1">
                        <div className="mb-1 flex items-center justify-between gap-2">
                          <span className="text-[11px] text-muted">
                            Cỡ chữ · {coverFontAuto ? 'Auto' : `${Math.round(coverFontScale * 100)}%`}
                          </span>
                          <button
                            type="button"
                            onClick={() => setCoverFontAuto((a) => !a)}
                            aria-pressed={coverFontAuto}
                            className={`h-6 rounded border px-2 text-[11px] font-medium transition ${
                              coverFontAuto ? 'border-brand bg-brand/20 text-fg' : 'border-line bg-panel text-muted hover:border-brand/40'
                            }`}
                          >
                            Auto
                          </button>
                        </div>
                        <input
                          type="range"
                          min={0.2}
                          max={1.5}
                          step={0.05}
                          value={coverFontScale}
                          onChange={(e) => setCoverFontScale(Number(e.target.value))}
                          disabled={coverFontAuto}
                          aria-label="Cỡ chữ"
                          className="w-full accent-[var(--color-brand)] disabled:opacity-40"
                        />
                      </div>
                      <div className="min-w-[10rem] flex-1">
                        <div className="mb-1 flex items-center justify-between gap-2">
                          <span className="text-[11px] text-muted">
                            Độ nghiêng · {coverTiltAuto ? 'Auto' : `${coverTilt}°`}
                          </span>
                          <button
                            type="button"
                            onClick={() => setCoverTiltAuto((a) => !a)}
                            aria-pressed={coverTiltAuto}
                            className={`h-6 rounded border px-2 text-[11px] font-medium transition ${
                              coverTiltAuto ? 'border-brand bg-brand/20 text-fg' : 'border-line bg-panel text-muted hover:border-brand/40'
                            }`}
                          >
                            Auto
                          </button>
                        </div>
                        <input
                          type="range"
                          min={-20}
                          max={20}
                          step={1}
                          value={coverTilt}
                          onChange={(e) => setCoverTilt(Number(e.target.value))}
                          disabled={coverTiltAuto}
                          aria-label="Độ nghiêng"
                          className="w-full accent-[var(--color-brand)] disabled:opacity-40"
                        />
                      </div>
                    </div>
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* Facebook hashtags are now generated automatically by the pipeline
              after the script is produced; they are no longer set here. */}
          <p className="text-xs text-muted">
            Hashtag Facebook sẽ tự tạo sau khi có kịch bản — xem & copy ở danh sách Video.
          </p>

          <div className="flex items-center gap-3 pt-1">
            <Button onClick={create} disabled={creating || coverLoading || !renderInstalled || !voiceModelInstalled || !link.trim()}>
              {creating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Wand2 className="h-4 w-4" />}
              {creating ? 'Đang thêm…' : 'Tạo video'}
            </Button>
            {/* Block submit while a cover is still generating so the owner can't
                create the video before the cover they intend to use is captured
                (root cause of "cover doesn't show" — submitting mid-generation). */}
            {coverLoading && <span className="text-xs text-amber-500">Đang tạo cover — chờ xong rồi tạo video.</span>}
            {!renderInstalled && <span className="text-xs text-amber-500">Model dựng chưa cài — chọn cái khác.</span>}
            {renderInstalled && !voiceModelInstalled && <span className="text-xs text-amber-500">Model lồng tiếng chưa cài — chọn cái khác.</span>}
            {msg && <span className={`text-xs ${msg.kind === 'ok' ? 'text-emerald-400' : 'text-rose-400'}`}>{msg.text}</span>}
          </div>
        </div>

        {/* Right: source link input + link preview (+30% larger) + source info */}
        <div>
          <Field label="Link nguồn">
            <div className="flex items-center gap-2">
              <div className="relative flex-1">
                <Link2 className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
                <TextInput value={link} onChange={setLink} placeholder="https://youtube.com/watch?v=…" className="pl-8 pr-9" />
                {link && (
                  <button
                    type="button"
                    onClick={() => setLink('')}
                    aria-label="Xoá link"
                    className="absolute right-2 top-1/2 grid h-5 w-5 -translate-y-1/2 place-items-center rounded text-muted transition hover:bg-panel2 hover:text-fg"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
              {/* Batch create: paste many links → one job per link with the CURRENT
                  Studio settings. Same install-gate as single-create so a batch can't
                  queue unrunnable jobs. Sits inline with the source-link input. */}
              <Button
                variant="outline"
                onClick={() => setShowBatch(true)}
                disabled={creating || coverLoading || !renderInstalled || !voiceModelInstalled}
                className="shrink-0"
              >
                <ListPlus className="h-4 w-4" /> Thêm danh sách
              </Button>
            </div>
          </Field>
          <span className="mb-1.5 mt-4 block text-xs font-medium text-muted">Xem trước nguồn</span>
          <div className="grid aspect-video place-items-center overflow-hidden rounded-xl border border-line bg-panel2">
            {thumbSrc ? (
              <img src={thumbSrc} alt="Ảnh thu nhỏ nguồn" className="h-full w-full object-cover" />
            ) : (
              <div className="px-4 text-center text-xs text-muted">Dán link YouTube để tải ảnh thu nhỏ</div>
            )}
          </div>
          {vid && (
            <div className="mt-2.5 space-y-1.5">
              {probe?.title && <p className="line-clamp-2 text-lg font-medium text-fg">{probe.title}</p>}
              <div className="flex items-center gap-2 text-base text-muted">
                {probing ? (
                  <span className="inline-flex items-center gap-1.5"><Loader2 className="h-4 w-4 animate-spin" /> Đang đọc thông tin…</span>
                ) : probe ? (
                  <>
                    <span className="inline-flex items-center gap-1.5 tabular-nums">
                      <Clock className="h-4 w-4" /> {fmtClock(probe.durationS)}
                    </span>
                    {probe.handle && <span className="truncate">· {probe.handle}</span>}
                  </>
                ) : null}
              </div>
            </div>
          )}

          {/* AI cover preview — shown once a cover has been generated (or while it
              is generating). Sits right below the source preview. */}
          {(cover || coverLoading) && (
            <div className="mt-4">
              <span className="mb-1.5 block text-xs font-medium text-muted">Ảnh Cover</span>
              <div className="grid min-h-[9rem] place-items-center overflow-hidden rounded-xl border border-line bg-panel2 p-2">
                {coverLoading ? (
                  <span className="inline-flex items-center gap-1.5 text-xs text-muted">
                    <Loader2 className="h-4 w-4 animate-spin" /> Đang tạo cover… {coverPct}%
                  </span>
                ) : cover ? (
                  <img src={cover.url} alt="Ảnh cover" className="h-auto w-auto max-h-[60vh] max-w-full rounded-lg object-contain" />
                ) : null}
              </div>
              {coverErr && <p className="mt-1.5 text-[11px] text-rose-400">{coverErr}</p>}
              {/* Use-cover toggle — feeds the `useCover` boolean into the job. */}
              <div className="mt-2.5 flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => cover && setUseCover((u) => !u)}
                  disabled={!cover}
                  aria-pressed={useCover && !!cover}
                  className={`inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-40 ${
                    useCover && !!cover ? 'border-brand bg-brand/20 text-fg' : 'border-line bg-panel text-muted hover:border-brand/40'
                  }`}
                >
                  {useCover && !!cover ? <CheckCircle2 className="h-4 w-4 text-brand" /> : <Square className="h-4 w-4" />}
                  Sử dụng ảnh Cover
                </button>
                {/* Clear the cover from the PREVIEW only — in-memory selection reset,
                    no file/cache delete. The video then creates with no cover
                    (useCover=false path). Keeps coverPrompt so the owner can regen. */}
                {cover && (
                  <button
                    type="button"
                    onClick={() => {
                      setCover(null)
                      setUseCover(false)
                      setCoverBasePath(null)
                      setCoverPromptShown('')
                    }}
                    className="inline-flex items-center gap-2 rounded-lg border border-line bg-panel px-3 py-2 text-sm font-medium text-muted transition hover:border-brand/40 hover:text-fg"
                  >
                    <X className="h-4 w-4" /> Bỏ cover
                  </button>
                )}
              </div>
            </div>
          )}
        </div>
      </div>

      {showScriptPicker && (
        <ReusableScriptPicker
          pageId={pageId}
          link={link}
          onClose={() => setShowScriptPicker(false)}
          onPick={(s, edited) => {
            setReusedScript(s)
            setReusedScriptEdited(edited)
            // New selection resets to the safe default (fresh audio) and clears any
            // previously-dismissed mismatch warning.
            setReuseMode('fresh-audio')
            setAudioMismatchAck(false)
            setShowScriptPicker(false)
          }}
        />
      )}

      {showSavedCovers && (
        <SavedCoverPicker
          pageName={pageName}
          onClose={() => setShowSavedCovers(false)}
          onPick={(picked) => {
            // A browsed cover is an existing image, not a fresh generation: it has no
            // seed/styleIndex, so use 0/0 (the assembly only needs path+url).
            setCover({ path: picked.path, url: picked.url, seed: 0, styleIndex: 0 })
            // Compositing base for an edited title. Prefer the CLEAN title-less sibling
            // the backend resolves (`basePath`); only fall back to the picked image when
            // there is none. Using `picked.path` unconditionally (the old behavior) meant
            // that picking a cover which ALREADY had a title baked in composited the new
            // title ON TOP of the old one instead of replacing it.
            setCoverBasePath(picked.basePath || picked.path)
            setUseCover(true)
            setShowSavedCovers(false)
            success('Đã chọn cover')
          }}
        />
      )}

      {showBatch && (
        <BatchListModal
          pageId={pageId}
          // Mirror EXACTLY the single-create body: read the SAME state the normal
          // "Tạo video" uses so a batch job == a normal job but for many links.
          // useCover is only meaningful when a cover was actually generated (same
          // useCoverNow guard as create()).
          settings={{
            editMode,
            voice: voiceKey || null,
            aspect,
            renderModel,
            voiceCloneModel,
            ...llmPayload,
            srcAudioVolume,
            addCredit,
            useCover: useCover && !!cover,
            coverImagePath: useCover && !!cover ? cover.path : null,
          }}
          hasOuterLink={!!link.trim()}
          onAdoptFirst={(l, t) => {
            // Fill the Studio's outer source-link (+ title) so "Tạo video" enables;
            // its own paste-probe/preview re-engages off the new link.
            setLink(l)
            if (t) setTitle(t)
          }}
          onClose={() => setShowBatch(false)}
          onCreated={onCreated}
        />
      )}

      {reuseGuard && (
        <Modal
          open
          onClose={() => setReuseGuard(null)}
          title="Sẽ SINH KỊCH BẢN MỚI (không dùng lại kịch bản cũ)"
          maxWidthClass="max-w-lg"
        >
          <div className="space-y-4">
            <p className="text-sm text-fg">
              Video gần nhất cho nguồn này đã{' '}
              <span className="font-medium text-brand">dùng lại kịch bản #{reuseGuard.lastVideoId}</span>.
              Lần này lựa chọn dùng lại đã bị bỏ, nên pipeline sẽ{' '}
              <span className="font-medium">viết một kịch bản HOÀN TOÀN MỚI</span> thay vì dùng lại.
            </p>
            <p className="text-[13px] text-muted">
              Nếu bạn muốn dùng lại kịch bản cũ, hãy chọn lại kịch bản. Nếu thực sự muốn tạo kịch bản mới, cứ tiếp tục.
            </p>
            <div className="flex flex-wrap items-center justify-end gap-2">
              <Button variant="ghost" onClick={() => setReuseGuard(null)}>
                Huỷ
              </Button>
              <Button
                variant="outline"
                onClick={() => {
                  setReuseGuard(null)
                  setShowScriptPicker(true)
                }}
              >
                <RotateCcw className="h-4 w-4" /> Chọn lại kịch bản để dùng lại
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  setReuseGuard(null)
                  void create(true)
                }}
              >
                <FileText className="h-4 w-4" /> Sinh kịch bản mới
              </Button>
            </div>
          </div>
        </Modal>
      )}

      {showAddVoice && (
        <AddVoiceModal
          pageName={pageName}
          cloneModel={voiceCloneModel}
          setCloneModel={setVoiceCloneModel}
          onClose={() => setShowAddVoice(false)}
          onAdded={(name) => {
            setShowAddVoice(false)
            onVoicesChanged()
            setVoiceKey(`clone:${name}`)
          }}
        />
      )}
    </Card>
  )
}

// ---- Workflow progress (live step-by-step for the page's latest job) ----

interface SysStats {
  ram: { taskMB: number | null }
  vram: { taskMB: number | null; gpuUtil: number | null; note: string | null }
  cpu: { percent: number | null }
  activeJobId: number | null
  activeStep: string | null
  activeModel: string | null
}

function WorkflowProgress({ pageId, trigger }: { pageId: number; trigger?: number }) {
  const refresh = useRefresh()
  const { success, error: toastError } = useToast()
  const { videos } = useData()
  const [job, setJob] = useState<import('../types').Job | null>(null)
  // All pending (not-yet-done, not-running) jobs waiting BEHIND the displayed one:
  // both 'held' (saved source-list rows, not released) and 'queued' (released).
  // These are the sources shown as "đang chờ"; the chip lists them on click.
  const [pendingJobs, setPendingJobs] = useState<import('../types').Job[]>([])
  // Whether the pending-jobs popover/modal is open.
  const [pendingOpen, setPendingOpen] = useState(false)
  // Id of the pending job currently being deleted (row spinner + disable).
  const [deletingId, setDeletingId] = useState<number | null>(null)

  // Delete one pending (held/queued) source from the review list. Optimistically
  // drops it from local state for instant feedback; the next poll tick reconciles.
  const deletePending = async (id: number) => {
    setDeletingId(id)
    try {
      await api.deleteJob(id)
      setPendingJobs((prev) => prev.filter((j) => j.id !== id))
      success('Đã xoá nguồn khỏi hàng chờ')
    } catch (e) {
      toastError(e instanceof Error ? e.message : 'Không xoá được nguồn')
    } finally {
      setDeletingId(null)
    }
  }
  const [clearing, setClearing] = useState(false)
  const [retrying, setRetrying] = useState(false)
  const [retryErr, setRetryErr] = useState<string | null>(null)
  // Stop the currently-running job — reuses the shared useStopJob hook (POST
  // /api/jobs/{id}/stop + refresh), same as Jobs.tsx's StopJobButton.
  const stopJob = useStopJob()
  const [stopping, setStopping] = useState(false)
  const [stopErr, setStopErr] = useState<string | null>(null)
  // Once a finished job has been shown at 100%, we reset the diagram to idle and
  // remember its id so polling doesn't re-display the completed job.
  const dismissedDoneIdRef = useRef<number | null>(null)
  // Id of the job the diagram is currently tracking. `job` captured in a poll
  // closure is stale across ticks, so we compare against this ref (kept in sync
  // on every setJob) to detect when the poll selects a DIFFERENT job — a newly
  // created or retried job always has a higher id. On that transition we clear
  // the dismissed-id so the fresh job cleanly re-engages the diagram instead of
  // being suppressed by a leftover dismissed id from the previous run.
  const trackedIdRef = useRef<number | null>(null)
  // Monotonically-increasing chip index + progress pct: prevents the active chip
  // and the numeric percentage from jumping backwards during per-scene loops or
  // the reuse-script path (which jumps from script 40% directly to voice 55%+).
  const maxActiveIdxRef = useRef<number>(-1)
  const maxPctRef = useRef<number>(0)
  const prevJobIdForChipsRef = useRef<number | null>(null)
  const [sys, setSys] = useState<SysStats | null>(null)
  // Live clock for the elapsed timer; only ticks while a job is in flight.
  const [now, setNow] = useState(() => Date.now())

  // Poll the page's active jobs so the diagram tracks the running one live.
  // When multiple jobs are in flight (one running + N queued), show only the
  // running job's progress and expose the queued count as a notification chip.
  useEffect(() => {
    let cancelled = false
    const tick = async () => {
      try {
        const jobs = (await fetch('/api/jobs').then((r) => r.json())) as import('../types').Job[]
        const pageJobs = jobs.filter((j) => j.pageId === pageId).sort((a, b) => b.id - a.id)
        // Prefer the running job; fall back to the latest queued one (next up).
        const running = pageJobs.find((j) => j.status === 'running') ?? null
        // NEVER track a 'held' job as the displayed job: held = saved-but-not-released
        // source-list rows (a batch add) that are NOT running, so treating one as the
        // tracked job made the elapsed clock tick for a job that hadn't started ("counter
        // tự chạy" after adding links). Fall back to the newest NON-held job (running/
        // queued/done/failed/stopped); when only held jobs exist, show idle — the "N đang
        // chờ" chip already surfaces them.
        const latest = running ?? pageJobs.find((j) => j.status !== 'held') ?? null
        // Pending jobs waiting BEHIND the displayed one (exclude it): both 'held'
        // (saved, not released) and 'queued' (released, next up).
        const waiting = pageJobs.filter(
          (j) => (j.status === 'queued' || j.status === 'held') && j.id !== latest?.id,
        )
        if (!cancelled) {
          setPendingJobs(waiting)
          // The poll selected a DIFFERENT job than we were tracking (a newly
          // created/retried job — always a higher id). Clear any leftover
          // dismissed-id BEFORE the done-guard so the fresh job re-engages the
          // diagram instead of being blanked. (The done-guard below only
          // suppresses the SAME id that was actually dismissed.)
          if (latest && latest.id !== trackedIdRef.current) {
            dismissedDoneIdRef.current = null
          }
          // Keep idle if this finished job was already shown to 100% and reset.
          if (latest && latest.status === 'done' && latest.id === dismissedDoneIdRef.current) {
            trackedIdRef.current = null
            setJob(null)
          } else {
            trackedIdRef.current = latest?.id ?? null
            setJob(latest)
          }
        }
      } catch {
        /* keep last known */
      }
      try {
        const s = (await fetch('/api/system').then((r) => r.json())) as SysStats
        if (!cancelled) setSys(s)
      } catch {
        /* ignore resource-stat errors */
      }
    }
    void tick()
    const id = setInterval(tick, 1500)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [pageId, trigger])

  // Reach 100% (done) → hold briefly so the user sees it full → reset to idle.
  useEffect(() => {
    if (job?.status === 'done' && job.id !== dismissedDoneIdRef.current) {
      const id = job.id
      const t = setTimeout(() => {
        dismissedDoneIdRef.current = id
        trackedIdRef.current = null
        setJob(null)
        setPendingJobs([])
      }, 3000)
      return () => clearTimeout(t)
    }
  }, [job])

  // When a job reaches ANY terminal state (done / failed / stopped), reset the
  // monotonic chip-index + pct floors so the last run's progress can never bleed
  // forward into the next job's diagram (which would otherwise show steps as
  // already-"done" before the new job actually reaches them). The visible
  // failed/stopped panel (with its retry button) is unaffected — those refs only
  // gate forward-motion of the chips while a job is RUNNING.
  const terminalStatus = job?.status === 'done' || job?.status === 'failed' || job?.status === 'stopped'
  useEffect(() => {
    if (terminalStatus) {
      maxActiveIdxRef.current = -1
      maxPctRef.current = 0
    }
  }, [terminalStatus, job?.id])

  // Elapsed timer: count up from createdAt while the job is in flight; freeze at
  // (finishedAt − createdAt) once the job reaches a terminal state. We only run
  // the 1s interval while finishedAt is null, and clear it as soon as it's set.
  const inFlight = !!job && !job.finishedAt
  useEffect(() => {
    if (!inFlight) return
    setNow(Date.now())
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [inFlight, job?.id])

  const elapsedS = job
    ? Math.max(0, ((job.finishedAt ? Date.parse(job.finishedAt) : now) - Date.parse(job.createdAt)) / 1000)
    : 0

  // Backend emits some progress_step values that don't have their own chip
  // (e.g. story/SDXL mode emits 'image'). Alias those onto the closest chip so
  // an in-flight step always highlights one. 'image' → 'footage' (the 'footage'
  // chip is "Tải/Tạo hình" = Download/Create image, which covers image creation).
  // 'publish' → 'render': publish is no longer its own chip, so keep the last
  // chip (Dựng video) highlighted while the runner auto-uploads.
  // Dubbed jobs run a different pipeline (see DUBBED_WORKFLOW_STEPS). Detect them
  // by editMode === 'dubbed' ALONE — renderModel is decoupled from editMode in the
  // backend and is NOT guaranteed to be 'passthrough-trim' for dubbed.
  const isDubbed = job?.editMode === 'dubbed'
  const isPassthrough = job?.renderModel === 'passthrough-trim'
  // Base alias for the normal path: 'image' (story/SDXL) → 'footage' chip; 'publish'
  // → 'render' so the last chip stays highlighted while the runner auto-uploads.
  // For dubbed, also alias 'needs_input' → 'script': the dubbed pipeline pauses for
  // source info right after the translate step, so keep the (last-before-render)
  // 'script' = "Dịch phụ đề" chip highlighted during that pause. Scoped to dubbed
  // so the normal path's needs_input handling is untouched.
  // passthrough-trim removes the 'footage' chip from steps, so any runner step
  // that emits 'footage' (clip-cut phase) or 'image' (shouldn't happen for
  // passthrough, but defensive) must alias to 'cut' — otherwise stepKey='footage'
  // lands on activeIdx=-1 and ALL chips go gray for the entire cut phase.
  const STEP_ALIAS: Record<string, string> = isDubbed
    ? { image: 'footage', publish: 'render', needs_input: 'script' }
    : isPassthrough
      ? { footage: 'cut', image: 'cut', publish: 'render' }
      : { image: 'footage', publish: 'render' }
  const rawStep = job?.progressStep ?? ''
  const stepKey = STEP_ALIAS[rawStep] ?? rawStep
  // Chip set selection:
  // - dubbed → DUBBED_WORKFLOW_STEPS (ingest / Dịch phụ đề / Cắt & ghép giữ tiếng gốc).
  // - else passthrough-trim keeps the source footage as-is: drop the 'footage'
  //   ("Tải/Tạo hình") chip so the diagram has no gap; remaining chips renumber via
  //   their map index (i + 1) and arrows connect off the filtered length.
  // - else the full footage WORKFLOW_STEPS.
  const steps = isDubbed
    ? DUBBED_WORKFLOW_STEPS
    : isPassthrough
      ? WORKFLOW_STEPS.filter((s) => s.key !== 'footage')
      : WORKFLOW_STEPS
  // Reset monotonic floors when a different job takes over (retry / new job).
  if ((job?.id ?? null) !== prevJobIdForChipsRef.current) {
    maxActiveIdxRef.current = -1
    maxPctRef.current = 0
    prevJobIdForChipsRef.current = job?.id ?? null
  }
  const running = job?.status === 'running'
  const rawActiveIdx = job ? steps.findIndex((s) => s.key === stepKey) : -1
  // While running, chips only move forward — never jump back to an earlier step.
  if (running && rawActiveIdx >= 0) maxActiveIdxRef.current = Math.max(maxActiveIdxRef.current, rawActiveIdx)
  const activeIdx = running ? maxActiveIdxRef.current : rawActiveIdx
  const done = job?.status === 'done'
  const failed = job?.status === 'failed'
  // 'stopped' = user stopped the job mid-run. A NEUTRAL terminal state: neither
  // failed (red) nor done (green). "Tiếp tục" (vs the cold "Chạy lại") enqueues a
  // NEW job that resumes the work from the furthest recoverable step (script
  // reuse when available) — the backend always mints a new job id, it does NOT
  // continue the same row.
  const stopped = job?.status === 'stopped'
  // Monotonic pct floor: reuse-script path jumps from script (40%) directly to
  // voice (55%+) which can momentarily poll at a lower value. Keep the max seen.
  const rawPct = done ? 100 : (job?.progressPct ?? 0)
  if (running) maxPctRef.current = Math.max(maxPctRef.current, rawPct)
  const pct = running ? maxPctRef.current : rawPct
  // The real-time value shown under the active chip: extract just the number
  // ("56%", "1/49", "12/42") from progressMsg, falling back to overall pct%.
  const progressValue = (() => {
    const msg = job?.progressMsg ?? ''
    const m = msg.match(/(\d+\s*\/\s*\d+|\d+%)/)
    if (m) return m[1].replace(/\s+/g, '')
    return `${pct}%`
  })()
  // When done, append the finished video's title to "Hoàn tất". Join via the
  // video's jobId; if no video / empty title, fall back to plain "Hoàn tất".
  const doneVideo = done && job ? (videos.find((v) => v.jobId === job.id) ?? null) : null
  const doneTitle = doneVideo?.title?.trim() ?? ''
  const doneLabel = doneTitle ? `Hoàn tất - ${doneTitle}` : 'Hoàn tất'
  // Inline title editing for the just-finished video (mirrors Videos.tsx).
  const [editingDoneTitle, setEditingDoneTitle] = useState(false)
  const [doneTitleDraft, setDoneTitleDraft] = useState('')
  const [savingDoneTitle, setSavingDoneTitle] = useState(false)
  const saveDoneTitle = async () => {
    if (!doneVideo) return
    const next = doneTitleDraft.trim()
    if (!next || next === doneVideo.title) {
      setEditingDoneTitle(false)
      return
    }
    setSavingDoneTitle(true)
    try {
      await api.updateVideoTitle(doneVideo.id, next)
      await refresh()
      setEditingDoneTitle(false)
      success('Đã đổi tiêu đề')
    } catch (e) {
      toastError(e instanceof Error ? e.message : 'Đổi tiêu đề thất bại')
    } finally {
      setSavingDoneTitle(false)
    }
  }
  // The error message is driven by the DATA field, not status: once the backend
  // nulls jobs.error (via clear-error), this becomes false and the block hides
  // — even if the job row keeps status='failed'. The 1.5s poll re-reads the row.
  // A 'stopped' job is NOT an error: even if the backend leaves a note on
  // jobs.error (e.g. "Đã dừng bởi người dùng"), we never paint it red — the
  // neutral 'stopped' branch renders that message instead.
  const hasError = !!job?.error && !stopped

  // "Selected options" the latest job was created with — surfaced as the SAME
  // chips Videos.tsx renders on a finished card, so the user can see the picked
  // edit mode / render model / voice engine / aspect at a glance while it runs.
  // Built only from fields that actually exist on the Job (editMode, renderModel,
  // voiceCloneModel, aspect); each chip is skipped when its value is null/empty.
  // Aspect IS a user-chosen option (the Studio "Tỷ lệ khung hình" select), so it
  // belongs here — this is where the ratio now lives after the preview card chip
  // was dropped.
  const optionChips = useMemo(() => {
    if (!job) return [] as { label: string; value: string }[]
    const chips: { label: string; value: string }[] = []
    if (job.editMode) chips.push({ label: 'Biên tập', value: EDIT_MODE_LABEL[job.editMode] ?? job.editMode })
    if (job.renderModel) chips.push({ label: 'Model dựng', value: RENDER_MODEL_LABEL[job.renderModel] ?? job.renderModel })
    if (job.voiceCloneModel) chips.push({ label: 'Model lồng tiếng', value: VOICE_CLONE_MODEL_LABEL[job.voiceCloneModel] ?? job.voiceCloneModel })
    if (job.aspect) chips.push({ label: 'Tỷ lệ', value: job.aspect })
    return chips
  }, [job])

  const clearError = async () => {
    if (!job || clearing) return
    setClearing(true)
    try {
      await api.clearJobError(job.id)
      // Reflect the cleared state immediately on this component's own job copy,
      // then trigger the shared refresh so the rest of the app re-fetches too.
      setJob((prev) => (prev && prev.id === job.id ? { ...prev, error: null } : prev))
      await refresh()
      success('Đã xóa lỗi job')
    } catch {
      /* leave the error visible if the backend rejected the clear */
      toastError('Không xóa được lỗi')
    } finally {
      setClearing(false)
    }
  }

  // Re-run / resume via POST /api/jobs/{id}/retry. The backend ALWAYS enqueues a
  // NEW job (a higher id) that copies the old job's params — a 'failed' retry is a
  // full re-run ("Chạy lại"); a 'stopped' retry resumes from the furthest
  // recoverable step ("Tiếp tục", script reuse when available). It never continues
  // the same row. We capture the returned newJobId and point the diagram at it
  // OPTIMISTICALLY (a minimal 'queued' placeholder) so the chips re-engage at once
  // instead of waiting up to 1.5s for the next poll — which then takes over with
  // the real row. 409 (job not in a retriable state) → show the message inline.
  const retry = async () => {
    if (!job || retrying) return
    const wasStopped = job.status === 'stopped'
    const prev = job
    setRetrying(true)
    setRetryErr(null)
    try {
      const { newJobId } = await api.retryJob(prev.id)
      // The new job is fresh — never suppress it as a leftover dismissed id.
      dismissedDoneIdRef.current = null
      trackedIdRef.current = newJobId
      // Optimistic queued placeholder for the new job: carry over the fields the
      // chip diagram + option chips read (pageId/editMode/renderModel/aspect/
      // voiceCloneModel), reset progress, and stamp createdAt now so the elapsed
      // timer starts. The 1.5s poll replaces this with the real row shortly.
      setJob({
        ...prev,
        id: newJobId,
        status: 'queued',
        progressStep: null,
        progressPct: 0,
        progressMsg: null,
        error: null,
        finishedAt: null,
        createdAt: new Date().toISOString(),
      })
      await refresh()
      success(wasStopped ? 'Đã tiếp tục job' : 'Đã tạo lại job')
    } catch (e) {
      setRetryErr(e instanceof Error ? e.message : String(e))
      toastError(wasStopped ? 'Không tiếp tục được job' : 'Không chạy lại được job')
    } finally {
      setRetrying(false)
    }
  }

  // Stop the running job. No confirm modal (matches Jobs.tsx) — a single
  // deliberate click is enough. The shared hook posts the stop + refreshes.
  const stop = async () => {
    if (!job || stopping) return
    setStopping(true)
    setStopErr(null)
    try {
      await stopJob(job.id)
      success('Đã dừng job')
    } catch (e) {
      setStopErr(e instanceof Error ? e.message : String(e))
      toastError('Không dừng được job')
    } finally {
      setStopping(false)
    }
  }

  return (
    <Card className="p-5">
      <div className="flex items-start justify-between gap-3">
        <SectionTitle sub="Tiến trình pipeline của job mới nhất">
          Workflow
        </SectionTitle>
        <div className="flex shrink-0 items-center gap-2">
          {/* Queue notification chip — shown when 1+ jobs are waiting behind
              the currently displayed one. Click to see the list of pending
              sources (held + queued). Collapses the details into a compact badge
              so the progress card stays uncluttered. */}
          {pendingJobs.length > 0 && (
            <button
              type="button"
              onClick={() => setPendingOpen(true)}
              aria-label={`${pendingJobs.length} nguồn đang chờ — xem danh sách`}
              title={`${pendingJobs.length} nguồn đang chờ — bấm để xem danh sách`}
              className="inline-flex shrink-0 cursor-pointer items-center gap-1.5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-2.5 py-1 text-sm font-semibold text-amber-600 transition hover:bg-amber-500/20 dark:text-amber-300"
            >
              {pendingJobs.length} đang chờ
            </button>
          )}
          <Modal open={pendingOpen} onClose={() => setPendingOpen(false)} title="Nguồn đang chờ tạo video" maxWidthClass="max-w-3xl">
            {pendingJobs.length === 0 ? (
              <p className="text-sm text-muted">Không có nguồn nào đang chờ.</p>
            ) : (
              <div className="space-y-3">
                <p className="text-xs text-muted">
                  Danh sách nguồn đã lưu, sẽ chạy khi bấm "Tạo video". Chỉ xem — không sửa được ở đây; muốn bỏ nguồn nào
                  thì xoá.
                </p>

                {/* Column header — mirrors the "Thêm danh sách video" modal grid. */}
                <div className="grid grid-cols-[1fr_1fr_auto] items-center gap-2 px-0.5 text-[11px] font-medium text-muted">
                  <span>Link nguồn</span>
                  <span>Tiêu đề</span>
                  <span className="w-8" aria-hidden />
                </div>

                <div className="max-h-[60vh] space-y-2 overflow-y-auto pr-0.5">
                  {pendingJobs.map((j) => (
                    <div key={j.id} className="grid grid-cols-[1fr_1fr_auto] items-center gap-2">
                      {/* Read-only cells styled like disabled TextInputs. */}
                      <div
                        className="truncate rounded-lg border border-line bg-panel2 px-3 py-2 text-sm text-fg"
                        title={j.inputPayload}
                      >
                        {j.inputPayload}
                      </div>
                      <div
                        className="truncate rounded-lg border border-line bg-panel2 px-3 py-2 text-sm text-fg"
                        title={j.title ?? ''}
                      >
                        {j.title || <span className="text-muted">(chưa có tiêu đề)</span>}
                      </div>
                      <button
                        type="button"
                        onClick={() => deletePending(j.id)}
                        disabled={deletingId !== null}
                        title="Xoá nguồn khỏi hàng chờ"
                        aria-label="Xoá nguồn khỏi hàng chờ"
                        className="flex h-8 w-8 items-center justify-center rounded-lg border border-line text-muted transition hover:border-rose-500/40 hover:text-rose-400 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:border-line disabled:hover:text-muted"
                      >
                        {deletingId === j.id ? (
                          <Loader2 className="h-4 w-4 animate-spin" />
                        ) : (
                          <X className="h-4 w-4" />
                        )}
                      </button>
                    </div>
                  ))}
                </div>

                <p className="text-[11px] text-muted">{pendingJobs.length} nguồn đang chờ</p>
              </div>
            )}
          </Modal>
          {/* Stop button — only while running. Destructive red treatment. */}
          {job && running && (
            <div className="flex flex-col items-end">
              <button
                onClick={stop}
                disabled={stopping}
                title="Dừng workflow đang chạy"
                aria-label="Dừng workflow"
                className="inline-flex items-center gap-1.5 rounded-lg border border-rose-500/50 bg-rose-500/10 px-2.5 py-1 text-sm font-semibold text-rose-600 transition hover:bg-rose-500/20 hover:text-rose-700 disabled:opacity-50 dark:text-rose-300 dark:hover:text-rose-200"
              >
                {stopping
                  ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  : <Square className="h-3.5 w-3.5 fill-current" />}
                Dừng
              </button>
              {stopErr && <p className="mt-1 line-clamp-2 text-right text-[10px] text-rose-500">{stopErr}</p>}
            </div>
          )}
          {job && (
            <span
              className={`inline-flex shrink-0 items-center gap-1.5 rounded-lg border px-2.5 py-1 text-sm font-semibold tabular-nums ${
                hasError
                  ? 'border-rose-500/40 bg-rose-500/10 text-rose-600 dark:text-rose-300'
                  : done
                    ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-300'
                    : stopped
                      ? 'border-slate-400/40 bg-slate-400/10 text-slate-600 dark:text-slate-300'
                      : 'border-brand/40 bg-brand/10 text-brand'
              }`}
              title={job.finishedAt ? 'Tổng thời gian chạy' : 'Thời gian đã trôi qua'}
            >
              <Clock className="h-3.5 w-3.5" />
              {fmtClock(elapsedS)}
            </span>
          )}
        </div>
      </div>

      {/* Selected-options chips for the tracked job (what the user picked):
          edit mode / render model / voice engine / aspect. Reuses Videos.tsx's
          OptionChip so the styling matches the finished-video cards exactly. */}
      {optionChips.length > 0 && (
        <div className="mb-4 flex flex-wrap gap-1">
          {optionChips.map((c, i) => <OptionChip key={i} label={c.label} value={c.value} />)}
        </div>
      )}

      <div className="space-y-4">
        {/* Step diagram — colored only while a job is running; gray when idle.
            Outer flex centers the chip group in the card frame (căn giữa khung);
            inner inline-flex keeps the chips' own spacing intact and scrolls when
            the row is too wide for the frame. */}
        <div className="flex justify-center overflow-x-auto pb-1">
          <div className="inline-flex items-center gap-1.5">
          {steps.map((s, i) => {
            const state = running
              ? i < activeIdx
                ? 'done'
                : i === activeIdx
                  ? 'active'
                  : 'pending'
              : hasError && i === activeIdx
                ? 'failed'
                : stopped
                  ? i < activeIdx
                    ? 'done' // steps completed before the user stopped
                    : i === activeIdx
                      ? 'stopped' // the step it halted on — neutral gray
                      : 'pending'
                  : 'pending'
            return (
              <div key={s.key} className="flex items-start gap-1.5">
                <div className="flex flex-col items-center gap-1">
                  <div
                    className={`grid h-[70px] w-[70px] place-items-center rounded-full border text-lg font-semibold ${
                      state === 'done'
                        ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300'
                        : state === 'active'
                          ? 'border-brand/50 bg-brand/15 text-brand'
                          : state === 'failed'
                            ? 'border-rose-500/40 bg-rose-500/15 text-rose-300'
                            : state === 'stopped'
                              ? 'border-slate-400/40 bg-slate-400/15 text-slate-300'
                              : 'border-line bg-panel2 text-muted'
                    }`}
                  >
                    {state === 'done' ? <Check className="h-8 w-8" /> : state === 'active' ? <Loader2 className="h-8 w-8 animate-spin" /> : state === 'stopped' ? <Square className="h-7 w-7 fill-current" /> : i + 1}
                  </div>
                  {/* Step name under the circle. */}
                  <span className={`whitespace-nowrap text-[11px] ${state === 'active' ? 'font-bold' : ''} ${state === 'pending' ? 'text-muted' : 'text-fg'}`}>{s.label}</span>
                  {/* Fixed-height value slot present on EVERY chip so all columns are
                      equal height (row stays centered); real-time value only on active. */}
                  <span className={`h-[18px] max-w-16 truncate text-[12px] tabular-nums text-brand ${state === 'active' ? 'font-bold' : ''}`}>{state === 'active' ? progressValue : ''}</span>
                </div>
                {i < steps.length - 1 && (
                  <div className={`mt-8 h-0.5 w-9 rounded ${state === 'done' ? 'bg-emerald-500/40' : 'bg-line'}`} />
                )}
              </div>
            )
          })}
          </div>
        </div>

        {/* Progress bar */}
        <div>
          {/* Status message (enlarged ~50%); the % now sits next to the bar, not above it. */}
          <div className={`mb-1.5 flex items-start justify-between gap-3 text-lg font-medium ${hasError ? 'text-rose-400' : 'text-fg'}`}>
            <span className="min-w-0 break-words">
              {!job
                ? 'Chưa có job nào — tạo video ở trên'
                : hasError
                  ? `Lỗi: ${job.error}`
                  : stopped
                    ? 'Đã dừng bởi người dùng' /* neutral, not an error */
                    : done
                      ? (
                        editingDoneTitle && doneVideo ? (
                          <span className="inline-flex items-center gap-1.5 align-middle">
                            <input
                              type="text"
                              value={doneTitleDraft}
                              onChange={(e) => setDoneTitleDraft(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter') { e.preventDefault(); void saveDoneTitle() }
                                if (e.key === 'Escape') setEditingDoneTitle(false)
                              }}
                              autoFocus
                              aria-label="Sửa tiêu đề"
                              className="min-w-0 rounded-lg border border-line bg-panel px-2 py-1 text-base text-fg outline-none focus:border-brand/50 focus:ring-2 focus:ring-brand/20"
                            />
                            <button
                              type="button"
                              onClick={() => void saveDoneTitle()}
                              disabled={savingDoneTitle}
                              title="Lưu"
                              aria-label="Lưu tiêu đề"
                              className="grid h-7 w-7 place-items-center rounded-lg text-emerald-500 transition hover:bg-emerald-500/10 disabled:opacity-50"
                            >
                              {savingDoneTitle ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
                            </button>
                            <button
                              type="button"
                              onClick={() => setEditingDoneTitle(false)}
                              disabled={savingDoneTitle}
                              title="Hủy"
                              aria-label="Hủy"
                              className="grid h-7 w-7 place-items-center rounded-lg text-muted transition hover:bg-panel2 hover:text-fg disabled:opacity-50"
                            >
                              <X className="h-4 w-4" />
                            </button>
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1.5 align-middle">
                            {doneLabel}
                            {doneVideo && (
                              <button
                                type="button"
                                onClick={() => { setDoneTitleDraft(doneVideo.title); setEditingDoneTitle(true) }}
                                title="Sửa tiêu đề"
                                aria-label="Sửa tiêu đề"
                                className="grid h-6 w-6 place-items-center rounded-lg text-muted transition hover:bg-panel2 hover:text-fg"
                              >
                                <Pencil className="h-3.5 w-3.5" />
                              </button>
                            )}
                          </span>
                        )
                      )
                      : running
                        ? '' /* progress now shown under the active chip + on the bar */
                        : job.finishedAt
                          ? '' /* terminal (incl. failed-but-error-cleared) → no status message */
                          : 'Đang chờ…'}
            </span>
            {/* Actions for a failed/errored job. "Chạy lại" re-runs the job (a new
                queued job is enqueued by the backend, same inputs); "Xoá lỗi"
                clears the stored error. Both shrink-0 so they don't squeeze the
                status text. */}
            {(failed || stopped || hasError) && (
              <div className="flex shrink-0 items-center gap-1.5">
                {/* Failed → cold "Chạy lại" (re-run from scratch). Stopped →
                    "Tiếp tục" (resume from where it left off). SAME endpoint
                    (POST /api/jobs/{id}/retry); the backend branches on the
                    job's status server-side. */}
                {failed && (
                  <Button variant="ghost" onClick={retry} disabled={retrying}>
                    {retrying ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                    {retrying ? 'Đang tạo…' : 'Chạy lại'}
                  </Button>
                )}
                {stopped && (
                  <Button variant="ghost" onClick={retry} disabled={retrying}>
                    {retrying ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
                    {retrying ? 'Đang tiếp tục…' : 'Tiếp tục'}
                  </Button>
                )}
                {/* Only when the displayed job actually carries an error. Goes through
                    the backend (POST /clear-error) so the cleared state survives the
                    12s refetch — a client-only hide would reappear. */}
                {hasError && (
                  <button
                    type="button"
                    onClick={clearError}
                    disabled={clearing}
                    title="Xoá lỗi của job này"
                    className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-rose-500/40 bg-rose-500/10 px-2.5 py-1 text-xs font-medium text-rose-600 hover:bg-rose-500/20 disabled:opacity-50 dark:text-rose-300"
                  >
                    {clearing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
                    Xoá lỗi
                  </button>
                )}
              </div>
            )}
          </div>
          {/* Inline retry error (e.g. 409 if the job is no longer failed). */}
          {retryErr && <p className="mb-1.5 text-xs text-rose-400">Không chạy lại được: {retryErr}</p>}
          <div className="flex items-center gap-3">
            <div className="h-3 flex-1 overflow-hidden rounded-full bg-panel2">
              <div
                className={`h-full rounded-full transition-all duration-500 ${hasError ? 'bg-rose-500' : done ? 'bg-emerald-500' : stopped ? 'bg-slate-400 dark:bg-slate-500' : 'bg-brand'}`}
                // Idle (nothing running) → 0%; otherwise the live percentage.
                // A stopped job freezes the bar at how far it got (neutral gray).
                style={{ width: `${running ? Math.max(2, pct) : stopped ? Math.max(2, pct) : 0}%` }}
              />
            </div>
            {(running || hasError || stopped) && (
              <span className="w-14 shrink-0 text-right text-lg font-semibold tabular-nums text-fg">{pct}%</span>
            )}
          </div>
          {/* Live resource monitor — the running task's own footprint. RAM is the
              worker process's resident memory; we show whole-GPU utilization since
              per-process VRAM is not available on this GPU. Shown as small pill chips. */}
          {sys && running && (
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              {sys.activeModel && (
                <span className="rounded-full border border-line bg-panel2 px-2.5 py-1 text-[12px] text-muted">
                  Model: <span className="text-fg">{sys.activeModel}</span>
                </span>
              )}
              <span className="rounded-full border border-line bg-panel2 px-2.5 py-1 text-[12px] text-muted">
                RAM: <span className="text-fg">{sys.ram?.taskMB != null ? `${sys.ram.taskMB} MB` : '—'}</span>
              </span>
              {sys.cpu?.percent != null && (
                <span className="rounded-full border border-line bg-panel2 px-2.5 py-1 text-[12px] text-muted">
                  CPU: <span className="text-fg">{sys.cpu.percent}%</span>
                </span>
              )}
              {sys.vram?.gpuUtil != null && (
                <span className="rounded-full border border-line bg-panel2 px-2.5 py-1 text-[12px] text-muted">
                  GPU: <span className="text-fg">{sys.vram.gpuUtil}%</span>
                </span>
              )}
            </div>
          )}
        </div>
      </div>
    </Card>
  )
}

// ---- Edit scenes (manual build) ----------------------------------------

interface SceneRow {
  id: number
  caption: string
  imagePath?: string
  imageUrl?: string
  uploading?: boolean
}
let SCENE_SEQ = 0

function SceneEditor({ pageName, voices, onVoicesChanged }: { pageName: string; voices: VoicesResponse | null; onVoicesChanged: () => void }) {
  const { success, error: toastError } = useToast()
  const [title, setTitle] = useState('')
  const [voiceKey, setVoiceKey] = useState('')
  const [scenes, setScenes] = useState<SceneRow[]>(() => [{ id: ++SCENE_SEQ, caption: '' }])
  const [rendering, setRendering] = useState(false)
  const [videoUrl, setVideoUrl] = useState<string | null>(null)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)

  // Advanced TTS tuning (hidden behind a toggle). Defaults lean steadier than
  // VieNeu's stock 0.8/1.2 to keep cloned-voice length consistent.
  const [adv, setAdv] = useState(false)
  const [temp, setTemp] = useState(0.7)
  const [rep, setRep] = useState(1.3)
  const [maxFrames, setMaxFrames] = useState(300)

  const voiceOptions = useMemo(() => buildVoiceOptions(voices), [voices])
  useEffect(() => {
    if (!voiceKey && voiceOptions.length) setVoiceKey(defaultVoiceKey(voices, voiceOptions))
  }, [voiceOptions, voiceKey, voices])
  const selected = voiceOptions.find((o) => o.key === voiceKey)

  const patch = (id: number, p: Partial<SceneRow>) => setScenes((s) => s.map((x) => (x.id === id ? { ...x, ...p } : x)))
  const addScene = () => setScenes((s) => [...s, { id: ++SCENE_SEQ, caption: '' }])
  const removeScene = (id: number) => setScenes((s) => (s.length > 1 ? s.filter((x) => x.id !== id) : s))

  const upload = async (id: number, file: File) => {
    patch(id, { uploading: true })
    try {
      const res = await api.uploadImage(pageName, file)
      patch(id, { imagePath: res.path, imageUrl: res.url, uploading: false })
      success('Đã tải ảnh lên')
    } catch (e) {
      patch(id, { uploading: false })
      setMsg({ kind: 'err', text: e instanceof Error ? e.message : String(e) })
      toastError('Tải ảnh thất bại')
    }
  }

  const render = async () => {
    if (scenes.some((s) => !s.caption.trim() || !s.imagePath)) {
      setMsg({ kind: 'err', text: 'Mỗi cảnh cần có lời thoại và một ảnh.' })
      return
    }
    setRendering(true)
    setMsg(null)
    setVideoUrl(null)
    try {
      const res = await api.makeVideo({
        page: pageName,
        title: title.trim() || 'video',
        voice: selected?.voice ?? null,
        refAudio: selected?.refAudio ?? null,
        scenes: scenes.map((s, i) => ({ scene: i + 1, caption: s.caption.trim(), imagePath: s.imagePath as string })),
        temperature: temp,
        repetitionPenalty: rep,
        maxNewFrames: Math.round(maxFrames),
      })
      setVideoUrl(res.url)
      setMsg({ kind: 'ok', text: `Đã dựng ${res.scenes} cảnh · ${res.durationS}s` })
      success('Đã dựng video')
    } catch (e) {
      setMsg({ kind: 'err', text: e instanceof Error ? e.message : String(e) })
      toastError('Dựng video thất bại')
    } finally {
      setRendering(false)
    }
  }

  return (
    <Card className="p-5">
      <SectionTitle sub="Dựng video theo từng cảnh — mỗi cảnh một ảnh + lời thoại, lồng tiếng rồi ghép. Sửa và dựng lại bất cứ lúc nào.">
        Sửa cảnh thủ công
      </SectionTitle>

      <div className="grid gap-3 sm:grid-cols-[1fr_240px]">
        <Field label="Tiêu đề video">
          <TextInput value={title} onChange={setTitle} placeholder="vd: Top 5 bí mật trong Elden Ring" />
        </Field>
        <Field label="Giọng đọc">
          <VoicePicker value={voiceKey} onChange={setVoiceKey} voices={voices} page={pageName} onDeleted={onVoicesChanged} />
        </Field>
      </div>

      <ul className="mt-4 space-y-3">
        {scenes.map((s, i) => (
          <li key={s.id} className="flex gap-3 rounded-xl border border-line bg-panel2 p-3">
            <div className="flex flex-col items-center gap-1 pt-1">
              <span className="text-xs font-semibold text-muted">#{i + 1}</span>
              {scenes.length > 1 && (
                <button onClick={() => removeScene(s.id)} aria-label="Remove scene" className="text-muted transition hover:text-rose-400">
                  <Trash2 className="h-4 w-4" />
                </button>
              )}
            </div>
            <label className="group relative grid h-24 w-16 shrink-0 cursor-pointer place-items-center overflow-hidden rounded-lg border border-line bg-panel transition hover:border-brand/40">
              {s.imageUrl ? (
                <img src={s.imageUrl} alt="" className="h-full w-full object-cover" />
              ) : s.uploading ? (
                <Loader2 className="h-4 w-4 animate-spin text-muted" />
              ) : (
                <ImageIcon className="h-5 w-5 text-muted" />
              )}
              <input
                type="file"
                accept="image/*"
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0]
                  if (f) void upload(s.id, f)
                }}
              />
            </label>
            <textarea
              value={s.caption}
              onChange={(e) => patch(s.id, { caption: e.target.value })}
              placeholder="Lời thoại tiếng Việt cho cảnh này…"
              rows={3}
              className="flex-1 rounded-lg border border-line bg-panel px-3 py-2 text-sm text-fg outline-none transition placeholder:text-muted/70 focus:border-brand/50 focus:ring-2 focus:ring-brand/20"
            />
          </li>
        ))}
      </ul>

      {/* Advanced TTS — collapsed by default */}
      <div className="mt-4">
        <button
          onClick={() => setAdv((a) => !a)}
          className="inline-flex items-center gap-1.5 text-xs font-medium text-muted transition hover:text-fg"
        >
          {adv ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRightIcon className="h-3.5 w-3.5" />}
          <SlidersHorizontal className="h-3.5 w-3.5" /> Advanced TTS
        </button>
        {adv && (
          <div className="mt-3 grid gap-3 rounded-xl border border-line bg-panel2 p-3 sm:grid-cols-3">
            <NumField label="Temperature" hint="↓ ổn định hơn (0.5–0.8)" value={temp} onChange={setTemp} min={0.1} max={1.2} step={0.05} />
            <NumField label="Repetition penalty" hint="↑ bớt lặp/lan man (1.0–1.6)" value={rep} onChange={setRep} min={1} max={2} step={0.05} />
            <NumField label="Max length (frames)" hint="Giới hạn độ dài (≈ chặn 24s)" value={maxFrames} onChange={setMaxFrames} min={100} max={600} step={10} />
          </div>
        )}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-3">
        <Button variant="outline" onClick={addScene}>
          <Plus className="h-4 w-4" /> Thêm cảnh
        </Button>
        <Button
          onClick={render}
          disabled={rendering || !voiceKey || scenes.some((s) => !s.caption.trim() || !s.imagePath)}
        >
          {rendering ? <Loader2 className="h-4 w-4 animate-spin" /> : <Film className="h-4 w-4" />}
          {rendering ? 'Đang dựng…' : 'Dựng video'}
        </Button>
        {msg && <span className={`text-xs ${msg.kind === 'ok' ? 'text-emerald-400' : 'text-rose-400'}`}>{msg.text}</span>}
      </div>

      {videoUrl && (
        // eslint-disable-next-line jsx-a11y/media-has-caption
        <video src={videoUrl} controls className="mt-4 w-full max-w-[280px] rounded-xl border border-line" />
      )}
    </Card>
  )
}

function AddVoiceModal({
  pageName,
  cloneModel,
  setCloneModel,
  onClose,
  onAdded,
}: {
  pageName: string
  cloneModel: string
  setCloneModel: (v: string) => void
  onClose: () => void
  onAdded: (name: string) => void
}) {
  const { success, error: toastError } = useToast()
  const [name, setName] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  // The backend bakes this suffix into the saved voice name; show it read-only
  // so the user sees the final name before submitting.
  const short = VOICE_CLONE_MODELS.find((m) => m.value === cloneModel)?.short ?? 'VieNeu'
  // Mirror the backend sanitizer (generate.py upload_voice): keep only unicode
  // letters/digits + space/dash/underscore, trim, fall back to "voice" if empty.
  // \p{L}\p{N} (with /u) preserves Vietnamese diacritics like "Hà Nội", matching
  // Python's str.isalnum() which returns True for those letters.
  const sanitizedName = name.replace(/[^\p{L}\p{N} _-]/gu, '').trim()
  // Show a placeholder before the user has typed; otherwise mirror the saved base.
  const previewBase = name.trim() ? sanitizedName || 'voice' : '…'
  const finalName = `${previewBase} - ${short}`
  const modelInstalled = VOICE_CLONE_MODELS.find((m) => m.value === cloneModel)?.installed ?? true

  const submit = async () => {
    if (!name.trim() || !file) {
      setError('Cần nhập tên và chọn file âm thanh.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const res = await api.uploadVoice(pageName, name.trim(), file, cloneModel)
      success('Đã clone giọng thành công')
      onAdded(res.name)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      toastError('Clone giọng thất bại')
      setBusy(false)
    }
  }

  return (
    <Modal open onClose={onClose} title="Clone giọng đọc">
      <div className="space-y-3">
        <p className="text-xs text-muted">
          Tải lên mẫu tiếng Việt một người nói, sạch (~5–15s, wav/flac/ogg). Sample rate nào cũng được; không nhạc/tạp âm.
        </p>
        <Field label="Voice clone model" hint={VOICE_CLONE_MODELS.find((m) => m.value === cloneModel)?.desc}>
          <Select value={cloneModel} onChange={setCloneModel} settingKey="voiceUpload.cloneModel" autoApplyDefault>
            {VOICE_CLONE_MODELS.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Tên giọng">
          <TextInput value={name} onChange={setName} placeholder="vd: Host nam" />
        </Field>
        <Field label="Tên cuối cùng" hint="Tên lưu thực tế — đã gắn sẵn tên model.">
          <div className="w-full cursor-not-allowed rounded-lg border border-line bg-panel2 px-3 py-2 text-sm text-muted">
            {finalName}
          </div>
        </Field>
        <Field label="Âm thanh tham chiếu">
          <input
            ref={fileRef}
            type="file"
            accept="audio/*,.wav,.flac,.ogg,.mp3"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="block w-full text-xs text-muted file:mr-3 file:rounded-lg file:border file:border-line file:bg-panel2 file:px-3 file:py-1.5 file:text-sm file:text-fg hover:file:border-brand/40"
          />
        </Field>
        {error && <p className="text-xs text-rose-400">{error}</p>}
        <div className="flex justify-end gap-2 pt-1">
          <Button variant="ghost" onClick={onClose}>
            Huỷ
          </Button>
          <Button onClick={submit} disabled={busy || !modelInstalled}>
            {busy ? 'Đang tải lên…' : 'Thêm giọng'}
          </Button>
        </div>
        {!modelInstalled && (
          <p className="text-right text-[11px] text-amber-500">Model này chưa cài — không thể clone giọng.</p>
        )}
      </div>
    </Modal>
  )
}

function NumField({
  label, hint, value, onChange, min, max, step,
}: {
  label: string
  hint?: string
  value: number
  onChange: (n: number) => void
  min: number
  max: number
  step: number
}) {
  return (
    <label className="block">
      <span className="mb-1 flex items-center justify-between text-xs font-medium text-muted">
        <span>{label}</span>
        <span className="tabular-nums text-fg">{value}</span>
      </span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        className="w-full"
        style={{ accentColor: 'var(--color-brand)' }}
      />
      {hint && <span className="mt-0.5 block text-[10px] text-muted/80">{hint}</span>}
    </label>
  )
}
