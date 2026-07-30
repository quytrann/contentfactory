import { useMemo, useState } from 'react'
import { Check, CheckCircle2, CheckSquare, ExternalLink, Film, Hash, Image as ImageIcon, Loader2, Pencil, Play, Proportions, RectangleHorizontal, ScrollText, SearchX, Smartphone, Trash2, UploadCloud, X } from 'lucide-react'
import { api, ApiError } from '../api'
import type { VideoScriptDetail } from '../api'
import { useApiUploadEnabled, useData, useDeleteEntity, useRefresh } from '../data'
import { usePublish } from '../components/PublishModal'
import { SavedCoverPicker } from '../components/SavedCoverPicker'
import { ScriptView } from '../components/ScriptView'
import type { Video, VideoStatus } from '../types'
import { Card, ChipGroup, EmptyState, FilterBar, Modal, PLATFORM_META, SearchInput, SectionTitle, fmtDuration, useToast } from '../ui'
import { isKeepScript } from './CreateVideo'

// Compact status indicator (a dot) so dense cards stay readable.
const STATUS_DOT: Record<VideoStatus, string> = {
  published: '#10b981',
  ready: '#4d8bff',
  rendering: '#f59e0b',
  needs_input: '#f59e0b', // amber, like rendering — a waiting (paused) state
  failed: '#f43f5e',
}

const STATUSES: VideoStatus[] = ['published', 'ready', 'rendering', 'needs_input', 'failed']

// Real orientation from the produced frame size; falls back to the stored
// aspect string (e.g. "9:16" → "9 / 16") for videos still rendering, then to
// the caller's landscape flag for legacy rows with no dims or aspect.
export const aspectOf = (v: Video, landscape: boolean) => {
  if (v.width && v.height) return `${v.width} / ${v.height}`
  if (v.aspect) {
    const [w, h] = v.aspect.split(':')
    if (w && h) return `${w} / ${h}`
  }
  return landscape ? '16 / 9' : '9 / 16'
}

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
  omnivoice: 'OmniVoice',
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
  translate_full: 'Dịch đầy đủ (voice + phụ đề)',
}

// Read-back label for the LLM that ACTUALLY wrote a video's script (recorded on
// the video row, not the job). Provider keys mirror the backend gate:
// 'claude-cli' | 'gemini' | 'openrouter'. Unknown providers fall back to the raw
// key so a newly added backend provider still shows something meaningful.
const LLM_PROVIDER_LABEL: Record<string, string> = {
  'claude-cli': 'Claude',
  gemini: 'Gemini',
  openrouter: 'OpenRouter',
}

// "<Provider> (<model>)", e.g. 'gemini' + 'gemini-flash-latest' → "Gemini
// (flash-latest)". claude-cli carries no model id, so it renders as just
// "Claude". Returns null when nothing was recorded (legacy rows and script-reuse
// runs) — the caller then skips the chip entirely rather than guessing Claude.
const llmUsedLabel = (provider: string | null | undefined, model: string | null | undefined): string | null => {
  if (!provider) return null
  const name = LLM_PROVIDER_LABEL[provider] ?? provider
  if (!model) return name
  // Trim the parts that carry no distinguishing info so the chip (which
  // truncates) still shows the model itself: vendor prefix and the `:free` tag
  // every OpenRouter option in the gate has. `gemini-flash-latest` →
  // `flash-latest`, `deepseek/deepseek-r1:free` → `deepseek-r1`.
  const short = model.replace(/^gemini-/, '').replace(/^[^/]+\//, '').replace(/:free$/, '')
  return `${name} (${short})`
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
  const refresh = useRefresh()
  const { success, error: toastError } = useToast()
  const pageOf = (id: number) => pages.find((p) => p.id === id)
  const pageName = (id: number) => pageOf(id)?.name ?? '—'
  // Prefer the real frame size; fall back to the stored aspect string (e.g.
  // "16:9" → landscape) for videos still rendering; default to portrait.
  const landscapeOf = (v: Video) => {
    if (v.width && v.height) return v.width > v.height
    if (v.aspect) {
      const [w, h] = v.aspect.split(':')
      if (w && h) return parseInt(w) > parseInt(h)
    }
    return false
  }

  const [query, setQuery] = useState('')
  const [status, setStatus] = useState<'all' | VideoStatus>('all')
  const [format, setFormat] = useState<'all' | 'portrait' | 'landscape'>('all')
  const [pageId, setPageId] = useState<'all' | number>('all')
  const [playing, setPlaying] = useState<Video | null>(null)

  // ---- Selection mode ("Chọn" → mark manually-posted to Facebook) ----------
  // Toggling select mode turns each card into a tickable checkbox; a sticky
  // toolbar shows the count + a Facebook toggle (only platform this round) and
  // Lưu/Huỷ. "Lưu" marks every ticked video as manually posted to Facebook on its
  // OWN page (POST /api/videos/mark-posted), then refreshes so the FB done-chip
  // appears on each card.
  const [selectMode, setSelectMode] = useState(false)
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [fbOn, setFbOn] = useState(true)
  const [saving, setSaving] = useState(false)

  const enterSelect = () => {
    setSelectMode(true)
    setSelectedIds(new Set())
    setFbOn(true)
  }
  const exitSelect = () => {
    setSelectMode(false)
    setSelectedIds(new Set())
  }
  const toggleSelect = (id: number) =>
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const saveMarkPosted = async () => {
    const ids = [...selectedIds]
    if (ids.length === 0 || !fbOn || saving) return
    setSaving(true)
    try {
      const res = await api.markPosted(ids, 'facebook')
      const okCount = res.results.filter((r) => r.ok).length
      const failed = res.results.filter((r) => !r.ok)
      await refresh()
      if (failed.length === 0) {
        success(`Đã đánh dấu ${okCount} video đã đăng lên Facebook`)
        exitSelect()
      } else {
        const firstErr = failed.find((f) => f.error)?.error
        toastError(
          `Đánh dấu ${okCount}/${res.results.length} video · ${failed.length} lỗi${firstErr ? `: ${firstErr}` : ''}`,
        )
        // Keep failures ticked so the user can see/retry; drop the succeeded ones.
        const failedIds = new Set(failed.map((f) => f.videoId))
        setSelectedIds((prev) => new Set([...prev].filter((id) => failedIds.has(id))))
      }
    } catch (e) {
      toastError(e instanceof Error ? e.message : 'Đánh dấu thất bại')
    } finally {
      setSaving(false)
    }
  }

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
      // Newest first: by creation time, falling back to id (higher = newer) as a
      // tiebreaker / guard for unparseable timestamps. The live API already sorts
      // this way; sorting here keeps the order explicit and independent of source.
      .sort((a, b) => {
        const dt = (Date.parse(b.createdAt) || 0) - (Date.parse(a.createdAt) || 0)
        return dt !== 0 ? dt : b.id - a.id
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
      <div className="flex items-start justify-between gap-3">
        <SectionTitle sub="Khung video khớp đúng tỷ lệ khi tạo. Bấm vào một video để xem ngay trên trình duyệt.">
          Video
        </SectionTitle>
        <button
          type="button"
          onClick={selectMode ? exitSelect : enterSelect}
          className={`inline-flex shrink-0 items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm font-medium transition ${
            selectMode
              ? 'border-brand/50 bg-brand/15 text-brand'
              : 'border-line bg-panel2 text-fg hover:border-brand/40'
          }`}
        >
          <CheckSquare className="h-4 w-4" />
          {selectMode ? 'Đang chọn' : 'Chọn'}
        </button>
      </div>

      <FilterBar>
        <SearchInput value={query} onChange={setQuery} placeholder="Tìm tiêu đề hoặc trang…" />
        <ChipGroup options={pageOptions} value={String(pageId)} onChange={(v) => setPageId(v === 'all' ? 'all' : Number(v))} />
        <ChipGroup options={statusOptions} value={status} onChange={(v) => setStatus(v as 'all' | VideoStatus)} />
        <ChipGroup options={formatOptions} value={format} onChange={(v) => setFormat(v as 'all' | 'portrait' | 'landscape')} />
      </FilterBar>

      {filtered.length === 0 ? (
        <EmptyState Icon={SearchX} title="Không có video phù hợp" hint="Thử từ khoá, trạng thái, định dạng hoặc kênh khác." />
      ) : (
        <div className="grid grid-cols-3 gap-2.5 sm:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
          {filtered.map((v) => (
            <VideoCard
              key={v.id}
              v={v}
              landscape={landscapeOf(v)}
              pageName={pageName(v.pageId)}
              onPlay={setPlaying}
              variant="library"
              selectMode={selectMode}
              selected={selectedIds.has(v.id)}
              onToggleSelect={toggleSelect}
            />
          ))}
        </div>
      )}

      {/* Sticky selection toolbar — count + Facebook toggle + Lưu/Huỷ. */}
      {selectMode && (
        <div className="sticky bottom-4 z-20 mx-auto flex max-w-2xl flex-wrap items-center justify-between gap-3 rounded-2xl border border-line bg-panel/95 px-4 py-3 shadow-card backdrop-blur">
          <span className="text-sm font-medium text-fg">
            Đã chọn <span className="text-brand">{selectedIds.size}</span> video
          </span>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-muted">Đánh dấu đã đăng lên:</span>
            <button
              type="button"
              onClick={() => setFbOn((v) => !v)}
              aria-pressed={fbOn}
              className={`inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm font-medium transition ${
                fbOn
                  ? 'border-blue-500/50 bg-blue-500/15 text-blue-600 dark:text-blue-400'
                  : 'border-line bg-panel2 text-muted hover:border-brand/40'
              }`}
            >
              {fbOn ? <CheckCircle2 className="h-4 w-4" /> : <PLATFORM_META.facebook.Icon className="h-4 w-4" />}
              Facebook
            </button>
            <button
              type="button"
              onClick={exitSelect}
              disabled={saving}
              className="rounded-lg border border-line bg-panel2 px-3 py-1.5 text-sm text-fg transition hover:border-brand/40 disabled:opacity-50"
            >
              Huỷ
            </button>
            <button
              type="button"
              onClick={saveMarkPosted}
              disabled={saving || selectedIds.size === 0 || !fbOn}
              className="inline-flex items-center gap-1.5 rounded-lg border border-brand bg-brand px-4 py-1.5 text-sm font-medium text-white transition hover:bg-brand/90 disabled:opacity-50"
            >
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
              Lưu
            </button>
          </div>
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
  variant = 'library',
  pageId,
  selectMode = false,
  selected = false,
  onToggleSelect,
}: {
  v: Video
  landscape: boolean
  pageName: string
  onPlay: (v: Video) => void
  // Where this card is rendered, which changes the footer action:
  //  - 'library' (Video menu): ALWAYS show the "Đăng" button — a video can be
  //    published many times to many platforms, so it must never disappear;
  //    posted info (chip + per-channel links) shows ALONGSIDE it when present.
  //  - 'page' (a page's "Sản phẩm" list): no "Đăng" button at all — only a
  //    "đã lên lịch" chip when a scheduled post exists, otherwise nothing.
  variant?: 'library' | 'page'
  // Page variant only: the PageDetail page whose channels this video was
  // published into (NOT necessarily v.pageId). Needed to remove it from that
  // page's Products. Undefined disables the page-variant remove action.
  pageId?: number
  // Selection mode (library only): when true the card becomes a tickable
  // checkbox; clicking the thumbnail toggles selection instead of playing.
  selectMode?: boolean
  selected?: boolean
  onToggleSelect?: (id: number) => void
}) {
  const removeEntity = useDeleteEntity()
  const refresh = useRefresh()
  const openPublish = usePublish()
  const apiUploadEnabled = useApiUploadEnabled()
  const { success, error: toastError } = useToast()
  const FormatIcon = landscape ? RectangleHorizontal : Smartphone
  const playable = !!v.videoUrl
  const [busy, setBusy] = useState<'delete' | 'clone' | 'cover' | 'unpublish' | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [confirmDel, setConfirmDel] = useState(false)
  // Page-variant only: confirm dialog for "remove this video from THIS page's
  // Sản phẩm" (drops the page's posts rows only; keeps the video + the platform post).
  const [confirmUnpub, setConfirmUnpub] = useState(false)
  const [cloneOpen, setCloneOpen] = useState(false)
  // "Đổi cover" — open the saved-cover picker scoped to THIS video's page.
  const [coverOpen, setCoverOpen] = useState(false)
  // "Xem kịch bản" — read-only script viewer. Fetches on open; a video still
  // rendering (or failed early) 404s → show a friendly "chưa có kịch bản" note
  // instead of an error crash.
  const [scriptOpen, setScriptOpen] = useState(false)
  const [scriptDetail, setScriptDetail] = useState<VideoScriptDetail | null>(null)
  const [scriptLoading, setScriptLoading] = useState(false)
  // null = a real fetch error; 'none' = 404 (video has no script yet).
  const [scriptError, setScriptError] = useState<string | 'none' | null>(null)
  // Inline title editing.
  const [editingTitle, setEditingTitle] = useState(false)
  const [titleDraft, setTitleDraft] = useState(v.title)
  const [savingTitle, setSavingTitle] = useState(false)
  // "Copy tags" — briefly flips the Hash icon to a checkmark after copying.
  const [tagsCopied, setTagsCopied] = useState(false)

  const copyTags = async () => {
    if (!v.facebookTags) return
    try {
      await navigator.clipboard.writeText(v.facebookTags)
      setTagsCopied(true)
      success('Đã copy tags')
      setTimeout(() => setTagsCopied(false), 1500)
    } catch {
      toastError('Không copy được tags')
    }
  }

  const saveTitle = async () => {
    const next = titleDraft.trim()
    if (!next || next === v.title) {
      setEditingTitle(false)
      return
    }
    setSavingTitle(true)
    setErr(null)
    try {
      await api.updateVideoTitle(v.id, next)
      await refresh()
      setEditingTitle(false)
      success('Đã đổi tiêu đề')
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
      toastError('Đổi tiêu đề thất bại')
    } finally {
      setSavingTitle(false)
    }
  }

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
      // so we no longer call onChanged() for the delete case. Honor the PER-VIDEO
      // keep-script flag (toggled per row in the reuse-script modal): when set for
      // this video, the backend keeps the reusable script and clears only media +
      // audio.
      const keep = isKeepScript(v.id)
      await removeEntity('video', v.id, { keepScript: keep })
      success(keep ? 'Đã xóa video (giữ kịch bản)' : 'Đã xóa video')
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
      toastError('Xóa video thất bại')
      setBusy(null)
    }
  }

  // Page variant: remove this video from THIS page's "Sản phẩm" block ONLY —
  // deletes that page's posts rows. It does NOT delete the video (it stays in the
  // Video menu) and does NOT touch the real platform. On success the page's
  // product list no longer includes this video (publishedPageIds drops pageId),
  // so the card unmounts after refresh. Handles the 404 (nothing to remove) case.
  const doUnpublish = async () => {
    if (pageId == null) return
    setConfirmUnpub(false)
    setBusy('unpublish')
    setErr(null)
    try {
      const res = await api.removeVideoFromPage(v.id, pageId)
      if (res.removed > 0) success('Đã gỡ khỏi Sản phẩm')
      else success('Video không còn trong Sản phẩm của trang')
      await refresh()
      // Re-enable if the card is still mounted.
      setBusy(null)
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        // No posts for this video on this page — nothing to remove (soft no-op).
        toastError('Video này không có trong Sản phẩm của trang để gỡ')
        setBusy(null)
        return
      }
      setErr(e instanceof Error ? e.message : String(e))
      toastError('Gỡ khỏi Sản phẩm thất bại')
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

  // Replace this video's cover/thumbnail with a saved cover from the page's
  // library. Mirrors the delete/rename refresh path (global data hook) so the
  // card's thumbUrl updates in place after the swap.
  const doSetCover = async (path: string) => {
    setCoverOpen(false)
    setBusy('cover')
    setErr(null)
    try {
      await api.setVideoCover(v.id, path)
      await refresh()
      setBusy(null)
      success('Đã đổi cover')
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
      toastError('Đổi cover thất bại')
      setBusy(null)
    }
  }

  // Open the script viewer and (re)fetch the full saved script. A 404 is the
  // expected "no script yet" case (still rendering / failed early) and maps to
  // the friendly empty state, not an error.
  const openScript = async () => {
    setScriptOpen(true)
    setScriptDetail(null)
    setScriptError(null)
    setScriptLoading(true)
    try {
      const detail = await api.getVideoScript(v.id)
      setScriptDetail(detail)
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setScriptError('none')
      else setScriptError(e instanceof Error ? e.message : String(e))
    } finally {
      setScriptLoading(false)
    }
  }

  return (
    <>
    <Card className={`flex flex-col overflow-hidden ${landscape ? 'col-span-2' : ''}`}>
      <button
        type="button"
        onClick={() => {
          if (selectMode) onToggleSelect?.(v.id)
          else if (playable) onPlay(v)
        }}
        disabled={!selectMode && !playable}
        aria-pressed={selectMode ? selected : undefined}
        className={`group relative flex items-center justify-center overflow-hidden bg-gradient-to-br from-brand/12 to-panel2 disabled:cursor-default ${selectMode ? 'cursor-pointer' : ''}`}
        style={{ aspectRatio: aspectOf(v, landscape) }}
      >
        {/* Selection checkbox overlay (top-left) — shown only in select mode. */}
        {selectMode && (
          <span
            className={`absolute left-1.5 top-1.5 z-10 grid h-5 w-5 place-items-center rounded-md border-2 backdrop-blur-sm transition ${
              selected ? 'border-brand bg-brand text-white' : 'border-white/80 bg-[#0a0c12]/50 text-transparent'
            }`}
          >
            <Check className="h-3 w-3" />
          </span>
        )}
        {/* Dim + brand ring on a selected card so the pick reads at a glance. */}
        {selectMode && selected && <span className="absolute inset-0 z-[5] rounded-sm ring-2 ring-inset ring-brand" />}
        {v.thumbUrl ? (
          <img src={v.thumbUrl} alt={v.title} className="h-full w-full object-cover" />
        ) : v.videoUrl ? (
          // No owner-set cover: show the video's FIRST FRAME as a static thumbnail.
          // #t=0.001 forces the frame to paint in browsers that don't render one
          // from preload="metadata". pointer-events-none so the card's play button
          // still owns the click/hover (matching how the <img> behaved).
          <video
            src={`${v.videoUrl}#t=0.001`}
            preload="metadata"
            muted
            playsInline
            className="pointer-events-none h-full w-full object-cover"
          />
        ) : (
          <Film className="h-6 w-6 text-brand/40" />
        )}
        {playable && !selectMode && (
          <span className="absolute inset-0 grid place-items-center bg-black/0 transition group-hover:bg-black/30">
            <span className="grid h-9 w-9 place-items-center rounded-full bg-white/85 text-[#0a0c12] opacity-0 transition group-hover:opacity-100">
              <Play className="h-4 w-4 translate-x-px" />
            </span>
          </span>
        )}
        {/* Format badge — hidden in select mode so it doesn't overlap the checkbox. */}
        {!selectMode && (
          <span className="absolute left-1.5 top-1.5 inline-flex items-center gap-0.5 rounded bg-[#0a0c12]/70 px-1 py-0.5 text-[9px] font-medium text-white backdrop-blur-sm">
            <FormatIcon className="h-2.5 w-2.5" /> {landscape ? '16:9' : '9:16'}
          </span>
        )}
        <span className="absolute right-1.5 top-1.5 h-2 w-2 rounded-full ring-2 ring-[#0a0c12]/40" style={{ background: STATUS_DOT[v.status] }} title={v.status} />
        {v.status === 'needs_input' && (
          <span className="absolute inset-x-1.5 top-7 inline-flex items-center justify-center gap-0.5 rounded bg-amber-500/90 px-1 py-0.5 text-[9px] font-medium text-white backdrop-blur-sm">
            Chờ nhập nguồn
          </span>
        )}
        <span className="absolute bottom-1.5 right-1.5 inline-flex items-center gap-0.5 rounded bg-[#0a0c12]/70 px-1 py-0.5 text-[10px] tabular-nums text-white backdrop-blur-sm">
          {fmtDuration(v.durationS)}
        </span>
        {/* "Đổi cover" overlay — a role="button" div (not a <button>) because it
            sits INSIDE the play <button>; stopPropagation keeps a click here from
            also triggering play. Opens the saved-cover picker scoped to this page.
            Hidden in select mode so the whole thumbnail toggles selection cleanly. */}
        {!selectMode && (
        <span
          role="button"
          tabIndex={0}
          onClick={(e) => { e.stopPropagation(); setCoverOpen(true) }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); setCoverOpen(true) }
          }}
          title="Đổi cover — chọn một cover đã tạo của trang"
          aria-label="Đổi cover"
          className="absolute bottom-1.5 left-1.5 inline-flex cursor-pointer items-center gap-0.5 rounded bg-[#0a0c12]/70 px-1 py-0.5 text-[10px] font-medium text-white backdrop-blur-sm transition hover:bg-[#0a0c12]/90"
        >
          {busy === 'cover' ? <Loader2 className="h-2.5 w-2.5 animate-spin" /> : <ImageIcon className="h-2.5 w-2.5" />}
          Đổi cover
        </span>
        )}
      </button>

      <div className="flex flex-1 flex-col p-2.5">
        {editingTitle ? (
          <div className="flex items-start gap-1">
            <textarea
              value={titleDraft}
              onChange={(e) => setTitleDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void saveTitle() }
                if (e.key === 'Escape') { setEditingTitle(false); setTitleDraft(v.title) }
              }}
              rows={2}
              autoFocus
              className="min-w-0 flex-1 rounded border border-line bg-panel px-1.5 py-1 text-xs leading-snug text-fg outline-none focus:border-brand/50 focus:ring-1 focus:ring-brand/30"
              aria-label="Sửa tiêu đề"
            />
            <div className="flex shrink-0 flex-col gap-0.5">
              <button
                type="button"
                onClick={() => void saveTitle()}
                disabled={savingTitle}
                title="Lưu"
                aria-label="Lưu tiêu đề"
                className="grid h-5 w-5 place-items-center rounded text-emerald-500 transition hover:bg-emerald-500/10 disabled:opacity-50"
              >
                {savingTitle ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}
              </button>
              <button
                type="button"
                onClick={() => { setEditingTitle(false); setTitleDraft(v.title) }}
                disabled={savingTitle}
                title="Hủy"
                aria-label="Hủy sửa tiêu đề"
                className="grid h-5 w-5 place-items-center rounded text-muted transition hover:bg-panel2 hover:text-fg disabled:opacity-50"
              >
                <X className="h-3 w-3" />
              </button>
            </div>
          </div>
        ) : (
          <div className="group/title flex items-start gap-1">
            <p className="line-clamp-2 min-w-0 flex-1 text-xs font-medium leading-snug">{v.title}</p>
            <button
              type="button"
              onClick={() => { setTitleDraft(v.title); setEditingTitle(true) }}
              title="Đổi tên video"
              aria-label="Đổi tên video"
              className="grid h-5 w-5 shrink-0 place-items-center rounded text-muted transition hover:bg-panel2 hover:text-fg"
            >
              <Pencil className="h-3 w-3" />
            </button>
          </div>
        )}
        <div className="mt-1 flex items-center justify-between gap-1">
          <span className="truncate text-[10px] text-muted">{pageName}</span>
          <div className="flex shrink-0 items-center gap-1">
            {/* "đã đăng (thủ công) lên Facebook" chip — an F badge + check, styled
                as a posted/approved state. Set when postedPlatforms includes
                'facebook' (e.g. after "Chọn" → Lưu). Shown in both variants. */}
            {v.postedPlatforms.includes('facebook') && (
              <span
                title="Đã đăng (thủ công) lên Facebook"
                aria-label="Đã đăng lên Facebook"
                className="inline-flex items-center gap-0.5 rounded bg-emerald-500/15 px-1 py-0.5 text-[9px] font-semibold text-emerald-600 dark:text-emerald-400"
              >
                <PLATFORM_META.facebook.Icon className="h-2.5 w-2.5 text-blue-600 dark:text-blue-400" />
                <CheckCircle2 className="h-2.5 w-2.5" />
              </span>
            )}
            {/* Other posted platforms as plain icons (facebook handled by the chip). */}
            {v.postedPlatforms
              .filter((p) => p !== 'facebook')
              .map((p) => {
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
          {variant === 'page' ? (
            // Page "Sản phẩm" list: no publish action here. Surface ONLY a
            // "đã lên lịch" chip when a scheduled post exists; render nothing
            // for already-live/posted or no-post videos.
            (() => {
              const anyScheduled = (v.posts ?? []).some((p) => p.status === 'scheduled')
              return anyScheduled ? (
                <span className="inline-flex min-w-0 flex-1 items-center gap-1 self-start text-[10px] font-medium">
                  <span className="inline-flex shrink-0 items-center gap-1 rounded bg-sky-500/15 px-1.5 py-0.5 text-sky-500">
                    <UploadCloud className="h-3 w-3" /> đã lên lịch
                  </span>
                </span>
              ) : (
                // Nothing in this footer slot; keep a flex-1 spacer so the
                // clone/delete buttons stay right-aligned like elsewhere.
                <span className="min-w-0 flex-1" />
              )
            })()
          ) : (
            // Library (Video menu): the "Đăng" button is ALWAYS present (a video
            // can be published multiple times). When posts exist, show the status
            // chip + per-channel links ALONGSIDE the button, not instead of it.
            <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">
              {(v.posts?.length ?? 0) > 0 && (
                <>
                  {(() => {
                    const anyLive = v.posts.some((p) => p.status === 'posted')
                    const anyScheduled = !anyLive && v.posts.some((p) => p.status === 'scheduled')
                    const cls = anyLive
                      ? 'bg-emerald-500/15 text-emerald-500'
                      : anyScheduled
                        ? 'bg-sky-500/15 text-sky-500'
                        : 'bg-amber-500/15 text-amber-500'
                    const label = anyLive ? 'đã đăng' : anyScheduled ? 'đã lên lịch' : 'bản nháp'
                    return (
                      <span className={`inline-flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium ${cls}`}>
                        <UploadCloud className="h-3 w-3" /> {label}
                      </span>
                    )
                  })()}
                  <div className="flex min-w-0 flex-wrap items-center gap-1">
                    {v.posts.map((p, i) => {
                      const meta = PLATFORM_META[p.platform]
                      const Icon = meta.Icon
                      const label = `Xem trên ${meta.label} · ${p.pageName}${p.status === 'draft' ? ' (nháp)' : ''}`
                      return p.url ? (
                        <a
                          key={i}
                          href={p.url}
                          target="_blank"
                          rel="noreferrer"
                          title={label}
                          className="inline-flex shrink-0 items-center gap-0.5 rounded-md border border-line bg-panel2 px-1.5 py-1 text-fg transition hover:border-brand/40"
                        >
                          <Icon className={`h-3.5 w-3.5 ${meta.color}`} aria-hidden />
                          <ExternalLink className="h-3 w-3 text-muted" />
                        </a>
                      ) : (
                        <span
                          key={i}
                          title={`${label} — chưa có link`}
                          className="inline-flex shrink-0 items-center rounded-md border border-line bg-panel2 px-1.5 py-1 opacity-60"
                        >
                          <Icon className={`h-3.5 w-3.5 ${meta.color}`} aria-hidden />
                        </span>
                      )
                    })}
                  </div>
                </>
              )}
              {/* "Đăng" (opens the shared PublishModal) — hidden while API upload
                  is disabled (apiUploadEnabled=false from GET /api/system). */}
              {apiUploadEnabled && (
                <button
                  onClick={() => openPublish({ id: v.id, pageId: v.pageId, title: v.title })}
                  title="Chọn kênh và đăng"
                  aria-label="Đăng video"
                  className="inline-flex min-w-0 flex-1 items-center justify-center gap-1 rounded-md border border-brand/30 bg-brand/15 px-2 py-1.5 text-xs font-medium text-brand transition hover:bg-brand/25 disabled:opacity-50"
                >
                  <UploadCloud className="h-3.5 w-3.5 shrink-0" />
                  <span className="truncate">Đăng</span>
                </button>
              )}
            </div>
          )}
          {v.facebookTags && (
            <button
              onClick={copyTags}
              title="Copy tags Facebook của video này"
              aria-label="Copy tags Facebook"
              className="inline-flex shrink-0 items-center justify-center rounded-md border border-line bg-panel2 px-1.5 py-1.5 text-fg transition hover:border-brand/40"
            >
              {tagsCopied ? <Check className="h-3.5 w-3.5 text-emerald-500" /> : <Hash className="h-3.5 w-3.5" />}
            </button>
          )}
          <button
            onClick={openScript}
            title="Xem kịch bản đầy đủ của video này"
            aria-label="Xem kịch bản"
            className="inline-flex shrink-0 items-center justify-center rounded-md border border-line bg-panel2 px-1.5 py-1.5 text-fg transition hover:border-brand/40"
          >
            <ScrollText className="h-3.5 w-3.5" />
          </button>
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
            onClick={() => (variant === 'page' ? setConfirmUnpub(true) : setConfirmDel(true))}
            disabled={busy !== null || (variant === 'page' && pageId == null)}
            title={
              variant === 'page'
                ? 'Gỡ khỏi Sản phẩm (giữ video trong menu Video)'
                : 'Xoá hẳn (cả file ổ cứng)'
            }
            aria-label={variant === 'page' ? 'Gỡ khỏi Sản phẩm' : 'Xoá video'}
            className="inline-flex shrink-0 items-center justify-center rounded-md border border-rose-500/50 bg-rose-500/10 px-1.5 py-1.5 text-rose-400 transition hover:bg-rose-500/20 hover:text-rose-300 disabled:opacity-50"
          >
            {busy === 'delete' || busy === 'unpublish' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
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
    {scriptOpen && (
      <Modal open onClose={() => setScriptOpen(false)} title={`Kịch bản — ${v.title}`} maxWidthClass="max-w-2xl">
        <div className="max-h-[70vh] overflow-y-auto pr-0.5">
          {scriptLoading && (
            <div className="flex items-center gap-2 py-6 text-sm text-muted">
              <Loader2 className="h-4 w-4 animate-spin" /> Đang tải kịch bản…
            </div>
          )}
          {!scriptLoading && scriptError === 'none' && (
            <p className="py-6 text-center text-sm text-muted">Video này chưa có kịch bản.</p>
          )}
          {!scriptLoading && scriptError && scriptError !== 'none' && (
            <p className="py-4 text-sm text-rose-400">Không tải được kịch bản: {scriptError}</p>
          )}
          {!scriptLoading && !scriptError && scriptDetail && <ScriptView detail={scriptDetail} />}
        </div>
      </Modal>
    )}
    {coverOpen && (
      <SavedCoverPicker
        pageName={pageName}
        onClose={() => setCoverOpen(false)}
        onPick={(picked) => doSetCover(picked.path)}
      />
    )}
    {confirmUnpub && (
      <Modal open onClose={() => setConfirmUnpub(false)} title="Gỡ khỏi Sản phẩm?">
        <div className="space-y-4">
          <p className="text-sm text-muted">
            Gỡ “<span className="font-medium text-fg">{v.title}</span>” khỏi mục Sản phẩm của trang này. Thao tác này{' '}
            <span className="font-medium text-fg">không xoá video</span> (vẫn còn trong menu Video) và{' '}
            <span className="font-medium text-fg">không đụng tới bài đăng trên nền tảng</span> (Facebook…). Tiếp tục?
          </p>
          <div className="flex justify-end gap-2">
            <button
              onClick={() => setConfirmUnpub(false)}
              className="rounded-lg border border-line bg-panel2 px-4 py-2 text-sm text-fg transition hover:border-brand/40"
            >
              Huỷ
            </button>
            <button
              onClick={doUnpublish}
              className="rounded-lg border border-rose-500/50 bg-rose-500 px-4 py-2 text-sm font-medium text-white transition hover:bg-rose-600"
            >
              Gỡ khỏi Sản phẩm
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
  // Which LLM actually produced the script. Skipped when unrecorded (legacy rows
  // + script-reuse runs) — an absent chip means "not recorded", never "Claude".
  const llmUsed = llmUsedLabel(v.llmProviderUsed, v.llmModelUsed)
  if (llmUsed) chips.push({ label: 'Model AI', value: llmUsed })
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
  const hasJobOptions = !!(v.voiceCloneModel || v.renderModel || v.voice || v.editMode || v.aspect || v.targetSec != null || v.addCredit || llmUsed)
  if (!hasJobOptions) return null

  return <div className="mt-1.5 flex flex-wrap gap-1">{chips.map((c, i) => <OptionChip key={i} label={c.label} value={c.value} />)}</div>
}
