import { useMemo, useState } from 'react'
import { Film, Loader2, Play, Proportions, RectangleHorizontal, SearchX, Smartphone, Trash2, UploadCloud } from 'lucide-react'
import { api } from '../api'
import { useData, useDeleteEntity, useRefresh } from '../data'
import { usePublish } from '../components/PublishModal'
import type { Video, VideoStatus } from '../types'
import { Card, ChipGroup, EmptyState, FilterBar, Modal, PLATFORM_META, SearchInput, SectionTitle, fmtDuration, useToast } from '../ui'

// Compact status indicator (a dot) so dense cards stay readable.
const STATUS_DOT: Record<VideoStatus, string> = {
  published: '#10b981',
  ready: '#4d8bff',
  rendering: '#f59e0b',
  needs_input: '#f59e0b', // amber, like rendering — a waiting (paused) state
  failed: '#f43f5e',
}

const STATUSES: VideoStatus[] = ['published', 'ready', 'rendering', 'needs_input', 'failed']

// Real orientation from the produced frame size; the `landscape` flag is the
// caller's fallback (portrait-default) for older videos with no width/height.
export const aspectOf = (v: Video, landscape: boolean) =>
  v.width && v.height ? `${v.width} / ${v.height}` : landscape ? '16 / 9' : '9 / 16'

// Key→label lookups for the production-option chips. Source of truth is
// PageDetail.tsx (RENDER_MODELS / VOICE_CLONE_MODELS / EDIT_MODES); those consts
// are not exported there, so we mirror only the key→label mapping here to avoid
// an export-refactor / import cycle. Unknown keys fall back to the raw value.
export const RENDER_MODEL_LABEL: Record<string, string> = {
  'passthrough-trim': 'Giữ video gốc',
  'sdxl-base': 'SDXL base',
  'juggernaut-xl': 'Juggernaut XL',
  realvisxl: 'RealVisXL',
  'dreamshaper-xl': 'DreamShaper XL',
  'sdxl-turbo': 'SDXL-Turbo',
  'sdxl-lightning': 'SDXL-Lightning',
  'sd35-medium': 'SD3.5 Medium',
  'stickman-procedural': 'Stickman 2D',
  'stickman-blender': 'Stickman Blender',
}
export const VOICE_CLONE_MODEL_LABEL: Record<string, string> = {
  'f5-tts': 'F5-TTS',
  vieneu: 'VieNeu-TTS',
  'xtts-v2': 'Coqui XTTS-v2',
  'openvoice-v2': 'OpenVoice v2',
  'gpt-sovits': 'GPT-SoVITS',
}
export const EDIT_MODE_LABEL: Record<string, string> = {
  commentary: 'Commentary',
  recap: 'Recap',
  educational: 'Giáo dục / Education',
  summary: 'Summary',
  dubbed: 'Lồng phụ đề (Dubbed)',
}

// Clean a stored voice value for display: drop a leading `clone:` and a trailing
// ` - <Model>` suffix, e.g. `clone:Korea - F5-TTS` → `Korea`.
const cleanVoice = (voice: string) =>
  voice
    .replace(/^clone:/, '')
    .replace(/ - [^-]+$/, '')
    .trim()

// The clone model baked into a voice name's ` - <Model>` suffix (e.g.
// `clone:Korea - F5-TTS` → `F5-TTS`), or null for legacy clones with no suffix.
const cloneModelOf = (voice: string): string | null => {
  const m = voice.replace(/^clone:/, '').match(/ - ([^-]+)$/)
  return m ? m[1].trim() : null
}

// One production-option chip: faint label + value, tiny + truncating so the
// dense grid never overflows.
export function OptionChip({ label, value }: { label: string; value: string }) {
  return (
    <span className="inline-flex max-w-full items-center gap-1 rounded border border-line bg-panel2 px-1.5 py-0.5 text-[10px] leading-none">
      <span className="shrink-0 text-muted">{label}:</span>
      <span className="truncate text-fg">{value}</span>
    </span>
  )
}

export default function Videos() {
  const { videos: VIDEOS, pages } = useData()
  const pageOf = (id: number) => pages.find((p) => p.id === id)
  const pageName = (id: number) => pageOf(id)?.name ?? '—'
  // Prefer the real frame size; default to portrait (shorts) when dims are
  // unknown. render_mode is now per-job, no longer a page property.
  const landscapeOf = (v: Video) =>
    v.width && v.height ? v.width > v.height : false

  const [query, setQuery] = useState('')
  const [status, setStatus] = useState<'all' | VideoStatus>('all')
  const [format, setFormat] = useState<'all' | 'portrait' | 'landscape'>('all')
  const [pageId, setPageId] = useState<'all' | number>('all')
  const [playing, setPlaying] = useState<Video | null>(null)

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return VIDEOS.filter((v) => {
      if (pageId !== 'all' && v.pageId !== pageId) return false
      if (status !== 'all' && v.status !== status) return false
      if (format !== 'all') {
        const land = landscapeOf(v)
        if (format === 'landscape' && !land) return false
        if (format === 'portrait' && land) return false
      }
      if (q && !`${v.title} ${pageName(v.pageId)}`.toLowerCase().includes(q)) return false
      return true
    })
  }, [query, status, format, pageId, VIDEOS, pages])

  const STATUS_VI: Record<VideoStatus, string> = { published: 'đã đăng', ready: 'sẵn sàng', rendering: 'đang dựng', needs_input: 'chờ nhập nguồn', failed: 'lỗi' }
  const statusOptions = [
    { value: 'all', label: 'Tất cả', count: VIDEOS.length },
    ...STATUSES.map((s) => ({ value: s, label: STATUS_VI[s], count: VIDEOS.filter((v) => v.status === s).length })),
  ]
  const formatOptions = [
    { value: 'all', label: 'Tất cả' },
    { value: 'portrait', label: 'Dọc 9:16', count: VIDEOS.filter((v) => !landscapeOf(v)).length },
    { value: 'landscape', label: 'Ngang 16:9', count: VIDEOS.filter((v) => landscapeOf(v)).length },
  ]
  const pageOptions = [
    { value: 'all', label: 'Mọi kênh', count: VIDEOS.length },
    ...pages.map((p) => ({ value: String(p.id), label: p.name, count: VIDEOS.filter((v) => v.pageId === p.id).length })),
  ]

  return (
    <div className="space-y-6">
      <SectionTitle sub="Khung video khớp đúng tỷ lệ khi tạo. Bấm vào một video để xem ngay trên trình duyệt.">
        Video
      </SectionTitle>

      <FilterBar>
        <SearchInput value={query} onChange={setQuery} placeholder="Tìm tiêu đề hoặc trang…" />
        <ChipGroup options={pageOptions} value={String(pageId)} onChange={(v) => setPageId(v === 'all' ? 'all' : Number(v))} />
        <ChipGroup options={statusOptions} value={status} onChange={(v) => setStatus(v as 'all' | VideoStatus)} />
        <ChipGroup options={formatOptions} value={format} onChange={(v) => setFormat(v as 'all' | 'portrait' | 'landscape')} />
      </FilterBar>

      {filtered.length === 0 ? (
        <EmptyState Icon={SearchX} title="Không có video phù hợp" hint="Thử từ khoá, trạng thái, định dạng hoặc kênh khác." />
      ) : (
        <div className="grid grid-cols-3 gap-2.5 sm:grid-cols-4 lg:grid-cols-7 2xl:grid-cols-8">
          {filtered.map((v) => (
            <VideoCard key={v.id} v={v} landscape={landscapeOf(v)} pageName={pageName(v.pageId)} onPlay={setPlaying} />
          ))}
        </div>
      )}

      {playing && playing.videoUrl && (
        <Modal open onClose={() => setPlaying(null)} title={playing.title} maxWidthClass="max-w-none" variant="media">
          {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
          {/* Cap to ~70% of the viewport; w-auto/h-auto keeps the real aspect
              ratio so the video shrinks proportionally for portrait and landscape. */}
          <video
            src={playing.videoUrl}
            controls
            autoPlay
            className="mx-auto h-auto w-auto max-h-[70vh] max-w-[70vw] rounded-xl border border-line"
            style={{ aspectRatio: aspectOf(playing, playing.width && playing.height ? playing.width > playing.height : false) }}
          />
        </Modal>
      )}
    </div>
  )
}

export function VideoCard({
  v,
  landscape,
  pageName,
  onPlay,
}: {
  v: Video
  landscape: boolean
  pageName: string
  onPlay: (v: Video) => void
}) {
  const removeEntity = useDeleteEntity()
  const refresh = useRefresh()
  const openPublish = usePublish()
  const { success, error: toastError } = useToast()
  const FormatIcon = landscape ? RectangleHorizontal : Smartphone
  const playable = !!v.videoUrl
  const [busy, setBusy] = useState<'delete' | 'clone' | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [confirmDel, setConfirmDel] = useState(false)
  const [cloneOpen, setCloneOpen] = useState(false)

  // All supported ratios offered in the clone dropdown (backend now accepts
  // every one). Re-assembling the same content at a new ratio reuses cached
  // script/audio/images (no TTS/script re-run). The same-aspect choice stays
  // selectable but the backend 409s it (surfaced as an error).
  const ASPECT_CHOICES: { value: string; label: string }[] = [
    { value: '9:16', label: '9:16 — Dọc' },
    { value: '16:9', label: '16:9 — Ngang' },
    { value: '1:1', label: '1:1 — Vuông' },
    { value: '4:5', label: '4:5 — Dọc nhẹ' },
  ]
  // Default to a ratio DIFFERENT from the video's current one.
  const defaultAspect = v.aspect === '9:16' ? '16:9' : '9:16'
  const [cloneAspect, setCloneAspect] = useState(defaultAspect)

  // Publish now flows through the shared PublishModal (channel tickboxes +
  // per-platform results), so every publish button in the dashboard behaves
  // identically — same mechanism the delete path uses via useDeleteEntity.

  const doRemove = async () => {
    setConfirmDel(false)
    setBusy('delete')
    setErr(null)
    try {
      // Shared delete path (DELETE /api/videos/{id} + global refresh) — same
      // mechanism the Jobs queue uses. The hook refreshes the dataset itself,
      // so we no longer call onChanged() for the delete case.
      await removeEntity('video', v.id)
      success('Đã xóa video')
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
      toastError('Xóa video thất bại')
      setBusy(null)
    }
  }

  // Clone the video at a new aspect: enqueues a fast re-assemble of the same
  // content. The new video shows up as 'rendering' in the grid after refresh,
  // exactly like a normal job. On error surface the backend message.
  const doClone = async (aspect: string) => {
    setCloneOpen(false)
    setBusy('clone')
    setErr(null)
    try {
      await api.cloneVideo(v.id, aspect)
      await refresh()
      setBusy(null)
      success('Đã tạo bản clone tỷ lệ mới')
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
      toastError('Clone tỷ lệ thất bại')
      setBusy(null)
    }
  }

  return (
    <>
    <Card className={`flex flex-col overflow-hidden ${landscape ? 'col-span-2' : ''}`}>
      <button
        type="button"
        onClick={() => playable && onPlay(v)}
        disabled={!playable}
        className="group relative flex items-center justify-center overflow-hidden bg-gradient-to-br from-brand/12 to-panel2 disabled:cursor-default"
        style={{ aspectRatio: aspectOf(v, landscape) }}
      >
        {v.thumbUrl ? (
          <img src={v.thumbUrl} alt={v.title} className="h-full w-full object-cover" />
        ) : (
          <Film className="h-6 w-6 text-brand/40" />
        )}
        {playable && (
          <span className="absolute inset-0 grid place-items-center bg-black/0 transition group-hover:bg-black/30">
            <span className="grid h-9 w-9 place-items-center rounded-full bg-white/85 text-[#0a0c12] opacity-0 transition group-hover:opacity-100">
              <Play className="h-4 w-4 translate-x-px" />
            </span>
          </span>
        )}
        <span className="absolute left-1.5 top-1.5 inline-flex items-center gap-0.5 rounded bg-[#0a0c12]/70 px-1 py-0.5 text-[9px] font-medium text-white backdrop-blur-sm">
          <FormatIcon className="h-2.5 w-2.5" /> {landscape ? '16:9' : '9:16'}
        </span>
        <span className="absolute right-1.5 top-1.5 h-2 w-2 rounded-full ring-2 ring-[#0a0c12]/40" style={{ background: STATUS_DOT[v.status] }} title={v.status} />
        {v.status === 'needs_input' && (
          <span className="absolute inset-x-1.5 top-7 inline-flex items-center justify-center gap-0.5 rounded bg-amber-500/90 px-1 py-0.5 text-[9px] font-medium text-white backdrop-blur-sm">
            Chờ nhập nguồn
          </span>
        )}
        <span className="absolute bottom-1.5 right-1.5 inline-flex items-center gap-0.5 rounded bg-[#0a0c12]/70 px-1 py-0.5 text-[10px] tabular-nums text-white backdrop-blur-sm">
          {fmtDuration(v.durationS)}
        </span>
      </button>

      <div className="flex flex-1 flex-col p-2.5">
        <p className="line-clamp-2 text-xs font-medium leading-snug">{v.title}</p>
        <div className="mt-1 flex items-center justify-between gap-1">
          <span className="truncate text-[10px] text-muted">{pageName}</span>
          <div className="flex shrink-0 items-center gap-1">
            {v.postedPlatforms.map((p) => {
              const { Icon, color, label } = PLATFORM_META[p]
              return <Icon key={p} className={`h-3 w-3 ${color}`} aria-label={label} />
            })}
          </div>
        </div>

        {/* Production options chosen for this video (skip blanks so legacy /
            jobless rows don't render empty chips). */}
        <OptionChips v={v} />

        {/* Actions — mt-auto pins this row to the BOTTOM of the flex column so it
            lines up across cards regardless of title/chip height or aspect. The
            Publish button stays compact (small padding + icon) so the 3-item row
            never overflows the narrowest portrait card. */}
        <div className="mt-auto flex items-center gap-1.5 border-t border-line pt-2">
          {v.status === 'published' ? (
            <span className="inline-flex items-center gap-1 text-[10px] font-medium text-emerald-400">
              <UploadCloud className="h-3 w-3" /> đã đăng
            </span>
          ) : (
            <button
              onClick={() => openPublish({ id: v.id, pageId: v.pageId, title: v.title })}
              disabled={busy !== null || v.status !== 'ready'}
              title={v.status !== 'ready' ? 'Chỉ đăng được video đã sẵn sàng' : 'Chọn kênh và đăng'}
              aria-label="Đăng video"
              className="inline-flex min-w-0 flex-1 items-center justify-center gap-1 rounded-md border border-brand/30 bg-brand/15 px-2 py-1.5 text-xs font-medium text-brand transition hover:bg-brand/25 disabled:opacity-50"
            >
              <UploadCloud className="h-3.5 w-3.5 shrink-0" />
              <span className="truncate">Đăng</span>
            </button>
          )}
          <button
            onClick={() => setCloneOpen(true)}
            disabled={busy !== null}
            title="Clone tỷ lệ: dựng lại đúng nội dung này ở tỷ lệ khác (tái dùng kịch bản/giọng đọc/ảnh đã cache, không chạy lại TTS hay tạo kịch bản)"
            aria-label="Clone tỷ lệ"
            className="inline-flex shrink-0 items-center justify-center rounded-md border border-line bg-panel2 px-1.5 py-1.5 text-fg transition hover:border-brand/40 disabled:opacity-50"
          >
            {busy === 'clone' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Proportions className="h-3.5 w-3.5" />}
          </button>
          <button
            onClick={() => setConfirmDel(true)}
            disabled={busy !== null}
            title="Xoá hẳn (cả file ổ cứng)"
            aria-label="Xoá video"
            className="inline-flex shrink-0 items-center justify-center rounded-md border border-rose-500/50 bg-rose-500/10 px-1.5 py-1.5 text-rose-400 transition hover:bg-rose-500/20 hover:text-rose-300 disabled:opacity-50"
          >
            {busy === 'delete' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
          </button>
        </div>
        {err && <p className="mt-1 line-clamp-2 text-[10px] text-rose-400">{err}</p>}
      </div>
    </Card>
    {cloneOpen && (
      <Modal open onClose={() => setCloneOpen(false)} title="Clone tỷ lệ">
        <div className="space-y-4">
          <p className="text-sm text-muted">
            Dựng lại “<span className="font-medium text-fg">{v.title}</span>” ở tỷ lệ khác. Tái dùng kịch bản, giọng đọc
            và ảnh đã cache — không chạy lại TTS hay tạo kịch bản. Video mới sẽ xuất hiện ở trạng thái “đang dựng”.
          </p>
          <div className="flex flex-col gap-2">
            <label className="text-xs font-medium text-muted" htmlFor={`clone-aspect-${v.id}`}>
              Tỷ lệ khung hình
            </label>
            <select
              id={`clone-aspect-${v.id}`}
              value={cloneAspect}
              onChange={(e) => setCloneAspect(e.target.value)}
              className="h-9 rounded-lg border border-line bg-panel px-2 text-sm text-fg outline-none transition focus:border-brand/50 focus:ring-2 focus:ring-brand/20"
            >
              {ASPECT_CHOICES.map((a) => (
                <option key={a.value} value={a.value}>
                  {a.label}{a.value === v.aspect ? ' (hiện tại)' : ''}
                </option>
              ))}
            </select>
          </div>
          <div className="flex justify-end gap-2">
            <button
              onClick={() => setCloneOpen(false)}
              className="rounded-lg border border-line bg-panel2 px-4 py-2 text-sm text-fg transition hover:border-brand/40"
            >
              Huỷ
            </button>
            <button
              onClick={() => doClone(cloneAspect)}
              className="rounded-lg border border-brand/50 bg-brand px-4 py-2 text-sm font-medium text-white transition hover:bg-brand/90"
            >
              Clone tỷ lệ
            </button>
          </div>
        </div>
      </Modal>
    )}
    {confirmDel && (
      <Modal open onClose={() => setConfirmDel(false)} title="Xoá video">
        <div className="space-y-4">
          <p className="text-sm text-muted">
            Xoá hẳn “<span className="font-medium text-fg">{v.title}</span>” khỏi ổ cứng? Không thể hoàn tác.
          </p>
          <div className="flex justify-end gap-2">
            <button
              onClick={() => setConfirmDel(false)}
              className="rounded-lg border border-line bg-panel2 px-4 py-2 text-sm text-fg transition hover:border-brand/40"
            >
              Huỷ
            </button>
            <button
              onClick={doRemove}
              className="rounded-lg border border-rose-500/50 bg-rose-500 px-4 py-2 text-sm font-medium text-white transition hover:bg-rose-600"
            >
              Xoá hẳn
            </button>
          </div>
        </div>
      </Modal>
    )}
    </>
  )
}

// Wrapping row of tiny chips listing the production options chosen for a video.
// Primary options first (voice-clone model, render model, voice, source audio),
// then secondary ones (edit mode, aspect, target duration, credit). Each chip is
// skipped when its value is null/empty so legacy/jobless rows render nothing.
function OptionChips({ v }: { v: Video }) {
  const chips: { label: string; value: string }[] = []

  if (v.voiceCloneModel)
    chips.push({ label: 'Model lồng tiếng', value: VOICE_CLONE_MODEL_LABEL[v.voiceCloneModel] ?? v.voiceCloneModel })
  if (v.renderModel) chips.push({ label: 'Model dựng', value: RENDER_MODEL_LABEL[v.renderModel] ?? v.renderModel })
  if (v.voice) {
    const name = cleanVoice(v.voice)
    // Append the clone model that produced this voice, e.g. "Korea (F5-TTS)".
    const cm = cloneModelOf(v.voice)
    if (name) chips.push({ label: 'Giọng đọc', value: cm ? `${name} (${cm})` : name })
  }
  // srcAudioVolume is always present (number); show it whenever a job produced
  // the video — i.e. when any other option is set or the value is meaningful.
  chips.push({ label: 'Audio gốc', value: v.srcAudioVolume === 0 ? 'tắt' : `${Math.round(v.srcAudioVolume * 100)}%` })

  // Secondary, kept last and visually identical but appended after the primary set.
  if (v.editMode) chips.push({ label: 'Biên tập', value: EDIT_MODE_LABEL[v.editMode] ?? v.editMode })
  // No aspect-ratio chip here: the preview frame already shows the ratio via its
  // top-left FormatIcon badge (16:9 / 9:16), so a "Tỷ lệ" chip would be redundant.
  // (The ratio is still surfaced in the Studio Workflow's selected-options chips.)
  if (v.targetSec != null) chips.push({ label: 'Độ dài', value: fmtDuration(v.targetSec) })
  if (v.addCredit) chips.push({ label: 'Credit', value: 'có' })

  // Skip the whole row only when nothing meaningful was produced. The Audio gốc
  // chip alone (with no other options) means the row predates the job contract,
  // so require at least one job-derived option before showing chips.
  const hasJobOptions = !!(v.voiceCloneModel || v.renderModel || v.voice || v.editMode || v.aspect || v.targetSec != null || v.addCredit)
  if (!hasJobOptions) return null

  return <div className="mt-1.5 flex flex-wrap gap-1">{chips.map((c, i) => <OptionChip key={i} label={c.label} value={c.value} />)}</div>
}
