import { useEffect, useMemo, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import {
  Check, CheckCircle2, ChevronDown, ChevronRight as ChevronRightIcon, Clock, FileText, Film, Image as ImageIcon, Link2, Loader2, Mic,
  Pencil, Play, Plus, RefreshCw, RotateCcw, SlidersHorizontal, Square, Trash2, Wand2, X,
} from 'lucide-react'
import { useData, useRefresh, useStopJob } from '../data'
import { api } from '../api'
import type { ClonedVoice, ReusableScript, VideoScriptDetail, VoicesResponse } from '../api'
import type { PlatformSpec } from '../types'
import {
  Button, Card, EmptyState, Field, Modal, Pill, SectionTitle,
  Select, TextInput, fmtClock, useToast,
} from '../ui'
// Reuse the SAME option-chip primitive + key→Vietnamese-label maps the Videos
// view uses, so the Workflow's "selected options" row reads identically to the
// chips on a finished video card (single source of truth, no label drift).
import { EDIT_MODE_LABEL, OptionChip, RENDER_MODEL_LABEL, VOICE_CLONE_MODEL_LABEL } from './Videos'

// Three editing modes from `how to edit video.md`. The owner MUST pick one
// before a workflow runs (CLAUDE.md pre-workflow rule).
const EDIT_MODES = [
  { value: 'commentary', label: 'Commentary', desc: 'Dịch + phân tích & đưa quan điểm; footage gốc ≤ 20–40%.' },
  { value: 'recap', label: 'Recap', desc: 'Tóm tắt & kể lại có chọn lọc; sắp xếp lại, thêm giải thích.' },
  { value: 'educational', label: 'Giáo dục / Education', desc: 'Biến nội dung thành bài học / how-to / giải thích.' },
  { value: 'summary', label: 'Summary', desc: 'Rút gọn video gốc còn các ý chính theo đúng trình tự; lời kể của bạn, footage gốc chỉ minh hoạ. Xem "how to edit video.md".' },
  { value: 'dubbed', label: 'Lồng phụ đề (Dubbed)', desc: 'Giữ nguyên hình + tiếng gốc, chỉ cắt phần thừa, burn phụ đề tiếng Việt; KHÔNG TTS. Cảnh báo: rủi ro bản quyền cao — owner đã chấp nhận.' },
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
const INSTALLED_RENDER_MODELS = new Set(['passthrough-trim', 'sdxl-base', 'stickman-blender', 'stickman-procedural'])

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

// Studio form fields persisted to localStorage so navigating away and back doesn't reset them.
const CF_STUDIO_KEY = 'cf-studio'
type StudioSaved = {
  link: string; title: string; editMode: string; renderModel: string
  voiceCloneModel: string; aspect: string; targetSec: number
  autoDuration: boolean; addCredit: boolean; srcAudioVolume: number
}
function loadStudioSaved(): Partial<StudioSaved> {
  try { return JSON.parse(localStorage.getItem(CF_STUDIO_KEY) ?? 'null') ?? {} }
  catch { return {} }
}
function saveStudioSaved(s: StudioSaved) {
  try { localStorage.setItem(CF_STUDIO_KEY, JSON.stringify(s)) } catch { /* ignore */ }
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
}: {
  value: string
  onChange: (key: string) => void
  voices: VoicesResponse | null
  page: string
  onDeleted: () => void
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const [confirming, setConfirming] = useState<VoiceOption | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  const { success, error: toastError } = useToast()

  const options = useMemo(() => buildVoiceOptions(voices), [voices])
  const groups = useMemo(() => groupVoiceOptionsByModel(options), [options])
  const selected = options.find((o) => o.key === value)

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

  return (
    <div ref={rootRef} className={`relative ${className}`}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="flex h-9 w-full items-center justify-between gap-2 rounded-lg border border-line bg-panel px-3 text-sm text-fg outline-none transition hover:border-brand/40 focus:border-brand/50 focus:ring-2 focus:ring-brand/20"
      >
        <span className={`truncate ${selected ? 'text-fg' : 'text-muted'}`}>{triggerLabel}</span>
        <ChevronDown className={`h-4 w-4 shrink-0 text-muted transition ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && (
        <div className="absolute left-0 right-0 top-[calc(100%+4px)] z-30 max-h-72 overflow-y-auto rounded-lg border border-line bg-panel p-1 shadow-card">
          {voices === null ? (
            <div className="px-3 py-2 text-sm text-muted">Đang tải giọng…</div>
          ) : options.length === 0 ? (
            <div className="px-3 py-2 text-sm text-muted">Chưa có giọng — bấm + để clone</div>
          ) : (
            groups.map((g) => (
              <div key={g.model} className="mb-1 last:mb-0">
                <div className="px-2 pb-1 pt-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted/80">
                  {g.model}
                </div>
                {g.options.map((o) => (
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
                        className="grid h-6 w-6 shrink-0 place-items-center rounded text-muted opacity-0 transition hover:bg-rose-500/10 hover:text-rose-400 focus:opacity-100 group-hover:opacity-100"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    )}
                  </div>
                ))}
              </div>
            ))
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
      // Update local detail state so the UI reflects the change immediately
      setDetail(prev => {
        if (!prev) return prev
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
  useEffect(() => {
    stopAudio()
    setPlayingScene(null)
    setEditingScene(null)
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

  return (
    <Modal open onClose={onClose} title="Dùng kịch bản đã tạo trước đó" maxWidthClass="max-w-2xl">
      <div className="space-y-3">
        <p className="text-xs text-muted">
          Chọn một kịch bản đã tạo trước đó để dùng lại — pipeline sẽ BỎ QUA bước viết kịch bản (tiết kiệm thời gian & chi phí).
          {link.trim() ? ' Đang lọc theo link nguồn hiện tại.' : ' Hiện mọi kịch bản đã lưu của trang này.'}
        </p>

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

        {scripts !== null && scripts.length > 0 && (
          <ul className="max-h-[60vh] space-y-2 overflow-y-auto pr-0.5">
            {scripts.map((s) => {
              const heading = s.title?.trim() || s.sourceName?.trim() || `Video #${s.videoId}`
              const modeLabel = s.renderMode ? SCRIPT_MODE_LABEL[s.renderMode] ?? s.renderMode : null
              const expanded = expandedId === s.videoId
              return (
                <li key={s.videoId} className="rounded-xl border border-line bg-panel2 p-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium text-fg">{heading}</p>
                      <div className="mt-1 flex flex-wrap items-center gap-1.5">
                        <Pill tone="brand">{s.sceneCount} cảnh</Pill>
                        {modeLabel && <Pill tone="sky">{modeLabel}</Pill>}
                        {s.editMode && <Pill tone="slate">{s.editMode}</Pill>}
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
                      {detail && !detailLoading && !detailError && (
                        <ol className="space-y-2">
                          {detail.scenes.map((sc) => (
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
                                  <span className="text-fg">{sc.narration}</span>
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
                          ))}
                        </ol>
                      )}
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        )}

        <div className="flex justify-end pt-1">
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
  const _saved = useMemo(() => loadStudioSaved(), [])
  const [link, setLink] = useState(_saved.link ?? '')
  const [voiceKey, setVoiceKey] = useState('')
  const [editMode, setEditMode] = useState(_saved.editMode ?? 'commentary')
  const [renderModel, setRenderModel] = useState(_saved.renderModel ?? 'passthrough-trim')
  const [voiceCloneModel, setVoiceCloneModel] = useState(_saved.voiceCloneModel ?? 'f5-tts')
  const renderInstalled = INSTALLED_RENDER_MODELS.has(renderModel)
  const voiceModelInstalled = VOICE_CLONE_MODELS.find((m) => m.value === voiceCloneModel)?.installed ?? true
  const [aspect, setAspect] = useState(_saved.aspect ?? '16:9')
  const [targetSec, setTargetSec] = useState(_saved.targetSec ?? 300)
  const [autoDuration, setAutoDuration] = useState(_saved.autoDuration ?? true)
  const [addCredit, setAddCredit] = useState(_saved.addCredit ?? false)
  const [srcAudioVolume, setSrcAudioVolume] = useState(_saved.srcAudioVolume ?? 0)
  const [publish, setPublish] = useState(false) // opt-in auto-upload; default off = manual publish later
  // Platform picked in PlatformTierPicker, lifted up here. Auto-publish targets
  // ONLY this platform; null = no platform picked → nothing is auto-published.
  const [publishPlatform, setPublishPlatform] = useState<string | null>(null)
  const [showAddVoice, setShowAddVoice] = useState(false)

  // PART B (script reuse): when a saved script is picked, the job is created with
  // reuseScriptVideoId set and the backend SKIPS script-gen. This is intentionally
  // NOT persisted to localStorage (see StudioSaved) — a reuse selection must reset
  // on reload, never silently stick across sessions. `reusedScript` holds the full
  // picked row so the summary chip + the cross-mode warning can read its metadata.
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
  // Edited text → stale cache regardless of reuseMode; always bypass+delete.
  const bypassTtsCache = reuseMode === 'fresh-audio' || reusedScriptEdited
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

  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [creating, setCreating] = useState(false)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)

  // Output title for the video being created. Applied at create time; an empty
  // value falls back to the source video's title on the backend.
  const [title, setTitle] = useState(_saved.title ?? '')

  // Persist form state on every change so navigating away and back restores it.
  useEffect(() => {
    saveStudioSaved({ link, title, editMode, renderModel, voiceCloneModel, aspect, targetSec, autoDuration, addCredit, srcAudioVolume })
  }, [link, title, editMode, renderModel, voiceCloneModel, aspect, targetSec, autoDuration, addCredit, srcAudioVolume])

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

  useEffect(() => {
    if (!voiceKey && voiceOptions.length) setVoiceKey(voiceOptions[0].key)
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

  const create = async () => {
    if (!link.trim()) {
      setMsg({ kind: 'err', text: 'Dán link nguồn trước đã.' })
      return
    }
    setCreating(true)
    setMsg(null)
    try {
      await api.createJob({ pageId, link: link.trim(), title: title.trim() || undefined, voice: voiceKey || null, editMode, renderModel, voiceCloneModel, aspect, targetSec: autoDuration ? null : targetSec, addCredit, srcAudioVolume, publish, publishPlatform: publish ? publishPlatform : null, reuseScriptVideoId: reuseScriptVideoId ?? null, bypassTtsCache: reuseScriptVideoId != null ? bypassTtsCache : undefined })
      await onCreated()
      setMsg({ kind: 'ok', text: 'Đã thêm vào hàng đợi. Pipeline sẽ tự xử lý.' })
      setLink('')
      setTitle('')
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
                  <div className="min-w-0 flex-1">
                    <p className="flex items-center gap-1.5 text-sm font-medium text-brand">
                      <RotateCcw className="h-4 w-4 shrink-0" />
                      <span className="truncate">
                        Đang dùng lại kịch bản: {reusedScript.title?.trim() || reusedScript.sourceName?.trim() || `Video #${reusedScript.videoId}`}
                      </span>
                    </p>
                    <p className="mt-0.5 text-[11px] text-muted">
                      {reusedScript.sceneCount} cảnh
                      {reusedScript.renderMode ? ` · ${SCRIPT_MODE_LABEL[reusedScript.renderMode] ?? reusedScript.renderMode}` : ''}
                      {' '}· pipeline sẽ bỏ qua bước viết kịch bản.
                    </p>
                  </div>
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
                    onClick={() => { setReuseMode('fresh-audio'); setAudioMismatchAck(false) }}
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
                      Dùng lại nội dung kịch bản, tạo lại giọng đọc mới (bỏ qua cache audio).
                    </span>
                  </button>
                  <button
                    type="button"
                    onClick={() => setReuseMode('with-audio')}
                    className={`flex-1 rounded-lg border px-3 py-2 text-left text-xs transition ${
                      reuseMode === 'with-audio'
                        ? 'border-brand/60 bg-brand/10 text-fg'
                        : 'border-line bg-panel2 text-muted hover:border-brand/40 hover:text-fg'
                    }`}
                  >
                    <span className="flex items-center gap-1.5 font-medium">
                      {reuseMode === 'with-audio' && <Check className="h-3.5 w-3.5 text-brand" />}
                      Dùng lại kịch bản và audio
                    </span>
                    <span className="mt-0.5 block text-[10px] leading-relaxed text-muted">
                      Dùng lại giọng đọc đã lưu (cache audio — nhanh, không chạy GPU). Nếu kịch bản đã sửa, audio vẫn tạo lại.
                    </span>
                  </button>
                </div>

                {/* Audio-mismatch warning: only when reusing WITH audio on a script
                    that was edited this session. Dismissible (proceed anyway), or
                    switch back to fresh audio (Button 1). */}
                {reusedScriptEdited && (
                  <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] leading-relaxed text-amber-700 dark:text-amber-200">
                    Kịch bản đã sửa — cache audio cũ sẽ bị xóa, audio mới sẽ được tạo lại tự động.
                  </div>
                )}
              </div>
            )}
          </Field>

          <Field label="Giọng đọc" hint="Chỉ hiển thị các giọng bạn đã clone cho trang này. Bấm + để clone giọng mới.">
            <div className="flex items-center gap-2">
              <VoicePicker value={voiceKey} onChange={setVoiceKey} voices={voices} page={pageName} onDeleted={onVoicesChanged} className="flex-1" />
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
              hint={reusedScript ? 'Không áp dụng khi dùng lại kịch bản đã lưu — kịch bản đã cố định.' : EDIT_MODES.find((m) => m.value === editMode)?.desc}
            >
              <div className={reusedScript ? 'pointer-events-none opacity-40' : ''}>
                <Select value={editMode} onChange={setEditMode}>
                  {EDIT_MODES.map((m) => (
                    <option key={m.value} value={m.value}>
                      {m.label}
                    </option>
                  ))}
                </Select>
              </div>
            </Field>

            <Field label="Model dựng (engine)" hint={RENDER_MODELS.find((m) => m.value === renderModel)?.desc}>
              <Select value={renderModel} onChange={setRenderModel}>
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
              <Select value={aspect} onChange={setAspect}>
                {ASPECT_OPTIONS.map((a) => (
                  <option key={a.value} value={a.value}>
                    {a.label}
                  </option>
                ))}
              </Select>
            </Field>

            <Field label="Model lồng tiếng" hint={VOICE_CLONE_MODELS.find((m) => m.value === voiceCloneModel)?.desc}>
              <Select value={voiceCloneModel} onChange={setVoiceCloneModel}>
                {VOICE_CLONE_MODELS.map((m) => (
                  <option key={m.value} value={m.value}>
                    {m.label}
                  </option>
                ))}
              </Select>
            </Field>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Audio gốc" hint="Mặc định tắt audio gốc; chọn % nếu muốn giữ nhẹ tiếng nền của video nguồn.">
              <Select value={String(srcAudioVolume)} onChange={(v) => setSrcAudioVolume(Number(v))}>
                {SRC_AUDIO_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </Select>
            </Field>

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
              <div className={`flex h-9 items-center gap-2 ${autoDuration || reusedScript ? 'pointer-events-none opacity-40' : ''}`}>
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
            </Field>
          </div>

          <label className={`flex items-center gap-2.5 text-sm ${reusedScript ? 'cursor-not-allowed opacity-40' : 'cursor-pointer'}`}>
            <input
              type="checkbox"
              checked={autoDuration}
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

          <div className="flex items-center gap-3 pt-1">
            <Button onClick={create} disabled={creating || !renderInstalled || !voiceModelInstalled || !link.trim()}>
              {creating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Wand2 className="h-4 w-4" />}
              {creating ? 'Đang thêm…' : 'Tạo video'}
            </Button>
            {!renderInstalled && <span className="text-xs text-amber-500">Model dựng chưa cài — chọn cái khác.</span>}
            {renderInstalled && !voiceModelInstalled && <span className="text-xs text-amber-500">Model lồng tiếng chưa cài — chọn cái khác.</span>}
            {msg && <span className={`text-xs ${msg.kind === 'ok' ? 'text-emerald-400' : 'text-rose-400'}`}>{msg.text}</span>}
          </div>
        </div>

        {/* Right: source link input + link preview (+30% larger) + source info */}
        <div>
          <Field label="Link nguồn">
            <div className="relative">
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
  const [sys, setSys] = useState<SysStats | null>(null)
  // Live clock for the elapsed timer; only ticks while a job is in flight.
  const [now, setNow] = useState(() => Date.now())

  // Poll the page's most recent job so the diagram tracks it live.
  useEffect(() => {
    let cancelled = false
    const tick = async () => {
      try {
        const jobs = (await fetch('/api/jobs').then((r) => r.json())) as import('../types').Job[]
        const latest = jobs.filter((j) => j.pageId === pageId).sort((a, b) => b.id - a.id)[0] ?? null
        if (!cancelled) {
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
      }, 3000)
      return () => clearTimeout(t)
    }
  }, [job])

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
  // Base alias for the normal path: 'image' (story/SDXL) → 'footage' chip; 'publish'
  // → 'render' so the last chip stays highlighted while the runner auto-uploads.
  // For dubbed, also alias 'needs_input' → 'script': the dubbed pipeline pauses for
  // source info right after the translate step, so keep the (last-before-render)
  // 'script' = "Dịch phụ đề" chip highlighted during that pause. Scoped to dubbed
  // so the normal path's needs_input handling is untouched.
  const STEP_ALIAS: Record<string, string> = isDubbed
    ? { image: 'footage', publish: 'render', needs_input: 'script' }
    : { image: 'footage', publish: 'render' }
  const rawStep = job?.progressStep ?? ''
  const stepKey = STEP_ALIAS[rawStep] ?? rawStep
  // Chip set selection:
  // - dubbed → DUBBED_WORKFLOW_STEPS (ingest / Dịch phụ đề / Cắt & ghép giữ tiếng gốc).
  // - else passthrough-trim keeps the source footage as-is: drop the 'footage'
  //   ("Tải/Tạo hình") chip so the diagram has no gap; remaining chips renumber via
  //   their map index (i + 1) and arrows connect off the filtered length.
  // - else the full footage WORKFLOW_STEPS.
  const isPassthrough = job?.renderModel === 'passthrough-trim'
  const steps = isDubbed
    ? DUBBED_WORKFLOW_STEPS
    : isPassthrough
      ? WORKFLOW_STEPS.filter((s) => s.key !== 'footage')
      : WORKFLOW_STEPS
  const activeIdx = job ? steps.findIndex((s) => s.key === stepKey) : -1
  const running = job?.status === 'running'
  const done = job?.status === 'done'
  const failed = job?.status === 'failed'
  // 'stopped' = user stopped the job mid-run. A NEUTRAL terminal state: neither
  // failed (red) nor done (green). "Tiếp tục" (vs the cold "Chạy lại") enqueues a
  // NEW job that resumes the work from the furthest recoverable step (script
  // reuse when available) — the backend always mints a new job id, it does NOT
  // continue the same row.
  const stopped = job?.status === 'stopped'
  const pct = done ? 100 : (job?.progressPct ?? 0)
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
  const doneTitle = done && job ? (videos.find((v) => v.jobId === job.id)?.title?.trim() ?? '') : ''
  const doneLabel = doneTitle ? `Hoàn tất - ${doneTitle}` : 'Hoàn tất'
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
        <SectionTitle sub="Tiến trình pipeline của job mới nhất — đang ở bước nào, bao nhiêu phần trăm.">
          Workflow
        </SectionTitle>
        {job && (
          <div className="flex shrink-0 items-center gap-2">
            {/* Stop button — only while running. Sits to the LEFT of the timer.
                Destructive red treatment; busy/disabled + inline error. */}
            {running && (
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
          </div>
        )}
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
                      ? doneLabel
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
                style={{ width: `${running || done ? Math.max(2, pct) : stopped ? Math.max(2, pct) : 0}%` }}
              />
            </div>
            {(running || done || hasError || stopped) && (
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
          <Select value={cloneModel} onChange={setCloneModel}>
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
