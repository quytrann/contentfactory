import { useMemo, useState } from 'react'
import { Check, Copy, Link2, Loader2, Pencil, Play, SearchX, Square, Trash2, Type, X } from 'lucide-react'
import { useData, useDeleteEntity, useRefresh, useStopJob } from '../data'
import { api } from '../api'
import type { Job, JobStatus, PublishedPost, Video } from '../types'
import { Card, ChipGroup, EmptyState, FilterBar, Modal, PLATFORM_META, SearchInput, SectionTitle, StatusBadge, fmtClock, fmtDate, useToast } from '../ui'
import { SourceCreditButton } from '../components/SourceCreditModal'

// Distinct page names a job was actually published to (order-preserving).
// pageName can be null when the post's platform_account row was deleted; drop
// those so the cell only shows real page names.
function publishedPageNames(j: Job): string[] {
  return [...new Set(j.publishedPosts.map((p) => p.pageName).filter((n): n is string => !!n))]
}

// "Trang" cell content: comma-separated distinct page names from publishedPosts,
// or null (→ blank cell) when the job has no posts.
function PublishedPages({ job }: { job: Job }) {
  const names = publishedPageNames(job)
  if (names.length === 0) return null
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      {names.map((name) => (
        <span key={name} className="rounded-md bg-panel2 px-2 py-0.5 text-xs text-fg">
          {name}
        </span>
      ))}
    </span>
  )
}

// "Uploaded Link" cell content: one platform icon per published post. A 'posted'
// post with a URL is a clickable full-opacity link; a draft / urlless post shows
// the same icon grayed out and NOT wrapped in an anchor. Blank when no posts.
function PublishedLinks({ posts }: { posts: PublishedPost[] }) {
  if (posts.length === 0) return null
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      {posts.map((p, idx) => {
        const { Icon, color, label } = PLATFORM_META[p.platform]
        const live = p.status === 'posted' && !!p.url
        if (live) {
          return (
            <a
              key={`${p.platform}-${p.pageId}-${idx}`}
              href={p.url!}
              target="_blank"
              rel="noopener noreferrer"
              title={label}
              aria-label={label}
              className="inline-flex transition hover:opacity-80"
            >
              <Icon className={`h-4 w-4 ${color}`} />
            </a>
          )
        }
        return (
          <span key={`${p.platform}-${p.pageId}-${idx}`} title={`${label} (draft)`} className="inline-flex">
            <Icon className="h-4 w-4 text-muted opacity-50" aria-label={`${label} (draft)`} />
          </span>
        )
      })}
    </span>
  )
}

const STATUSES: JobStatus[] = ['held', 'queued', 'running', 'needs_input', 'done', 'failed', 'stopped']

// Workflow run time for the queue column. Matches the WorkflowProgress elapsed
// counter EXACTLY: (finishedAt − createdAt) once terminal, so the cell equals
// what the counter showed at completion. Queued (no finishedAt, not running) →
// "—"; running → live elapsed-so-far (no live tick here — refreshes on poll).
function runTime(j: Job): string {
  if (j.finishedAt) return fmtClock((Date.parse(j.finishedAt) - Date.parse(j.createdAt)) / 1000)
  if (j.status === 'running') return fmtClock((Date.now() - Date.parse(j.createdAt)) / 1000)
  return '—'
}

type SortDir = 'asc' | 'desc'

export default function Jobs() {
  const { jobs: JOBS, pages, videos } = useData()
  const pageName = (id: number) => pages.find((p) => p.id === id)?.name ?? '—'
  // Resolve a display title for a job. Fallback chain so the cell is never blank,
  // even right after creation (before any video row exists):
  //   1. the produced video's title (videos.jobId links back to its job), if non-empty;
  //   2. else for a 'prompt' job, the typed topic text (inputPayload);
  //   3. else (a 'link' job — inputPayload is a raw URL, ugly as a title) an id-based placeholder.
  const videoTitle = (job: Job) => {
    const produced = videos.find((v) => v.jobId === job.id)?.title?.trim()
    if (produced) return produced
    if (job.inputType === 'prompt' && job.inputPayload.trim()) return job.inputPayload
    return `Video #${job.pageSeq ?? job.id}`
  }
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState<'all' | JobStatus>('all')
  const [pageId, setPageId] = useState<'all' | number>('all')
  // Index-column sort. The backend returns jobs ordered by id (the stable base
  // order = ascending); the toggle just reverses that base order so the
  // contiguous 1..N index reads top-down or bottom-up.
  const [sortDir, setSortDir] = useState<SortDir>('asc')

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    const base = JOBS.filter((j) => {
      if (pageId !== 'all' && j.pageId !== pageId) return false
      if (status !== 'all' && j.status !== status) return false
      if (q && !`${j.inputPayload} ${pageName(j.pageId)}`.toLowerCase().includes(q)) return false
      return true
    })
    // Keep the API's id order as the base, then flip for desc. We don't mutate
    // JOBS (filter already returns a fresh array, so reverse() is safe here).
    return sortDir === 'asc' ? base : base.reverse()
  }, [query, status, pageId, JOBS, pages, sortDir])

  const pageOptions = [
    { value: 'all', label: 'Mọi kênh', count: JOBS.length },
    ...pages.map((p) => ({ value: String(p.id), label: p.name, count: JOBS.filter((j) => j.pageId === p.id).length })),
  ]

  const STATUS_VI: Record<JobStatus, string> = { held: 'đã lưu', queued: 'chờ xử lý', running: 'đang chạy', needs_input: 'chờ nhập nguồn', done: 'xong', failed: 'lỗi', stopped: 'đã dừng' }
  const statusOptions = [
    { value: 'all', label: 'Tất cả', count: JOBS.length },
    ...STATUSES.map((s) => ({ value: s, label: STATUS_VI[s], count: JOBS.filter((j) => j.status === s).length })),
  ]

  const toggleSort = () => setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))

  return (
    <div className="space-y-6">
      <SectionTitle sub="Các yêu cầu sản xuất từ chat. Chạy theo hàng đợi vì mỗi video mất vài phút.">
        Lịch sử Job
      </SectionTitle>

      <FilterBar>
        <SearchInput value={query} onChange={setQuery} placeholder="Tìm nội dung hoặc trang…" />
        <ChipGroup options={pageOptions} value={String(pageId)} onChange={(v) => setPageId(v === 'all' ? 'all' : Number(v))} />
        <ChipGroup options={statusOptions} value={status} onChange={(v) => setStatus(v as 'all' | JobStatus)} />
      </FilterBar>

      {filtered.length === 0 ? (
        <EmptyState Icon={SearchX} title="Không có job phù hợp" hint="Thử từ khoá hoặc bộ lọc trạng thái khác." />
      ) : (
      <>
      {/* Desktop table */}
      <Card className="hidden overflow-hidden md:block">
        <table className="w-full text-sm">
          <thead className="border-b border-line text-left text-xs uppercase tracking-wider text-muted">
            <tr>
              <th className="px-4 py-3 font-medium">
                <button
                  type="button"
                  onClick={toggleSort}
                  className="inline-flex items-center gap-1 uppercase tracking-wider transition hover:text-fg"
                  title="Sắp xếp theo thứ tự"
                  aria-label="Sắp xếp theo thứ tự"
                >
                  #<span className="text-[10px] leading-none">{sortDir === 'asc' ? '▲' : '▼'}</span>
                </button>
              </th>
              <th className="px-4 py-3 font-medium">Source link</th>
              <th className="px-4 py-3 font-medium">Tên video</th>
              <th className="px-4 py-3 font-medium">Uploaded Link</th>
              <th className="px-4 py-3 font-medium">Trang</th>
              <th className="px-4 py-3 font-medium">Tỷ lệ</th>
              <th className="px-4 py-3 font-medium">Tạo lúc</th>
              <th className="px-4 py-3 font-medium">Thời gian</th>
              <th className="px-4 py-3 font-medium">Trạng thái</th>
              <th className="px-4 py-3 font-medium text-right">Thao tác</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {filtered.map((j) => (
              <tr key={j.id} className="hover:bg-panel2/60">
                {/* Stable per-page job number (page_seq): matches creation/history order,
                    same value everywhere, never shifts on filter/sort/delete. Falls back
                    to the raw id only on legacy rows predating page_seq. */}
                <td className="px-4 py-3 tabular-nums text-muted">{j.pageSeq ?? j.id}</td>
                <td className="max-w-sm px-4 py-3">
                  <div className="flex items-center gap-2">
                    <InputIcon type={j.inputType} />
                    <span className="truncate">{j.inputPayload}</span>
                    {j.inputType === 'link' && j.inputPayload.trim() && (
                      <CopySourceButton url={j.inputPayload} />
                    )}
                  </div>
                </td>
                <td className="max-w-xs px-4 py-3">
                  {(() => {
                    // Editable only when the job has actually produced a video row
                    // (the title lives on videos.title). Otherwise show the derived
                    // fallback text (topic / placeholder), no edit affordance.
                    const produced = videos.find((v) => v.jobId === j.id)
                    return produced ? (
                      <EditVideoTitle video={produced} />
                    ) : (
                      <span className="block truncate" title={videoTitle(j)}>{videoTitle(j)}</span>
                    )
                  })()}
                </td>
                <td className="px-4 py-3">
                  <PublishedLinks posts={j.publishedPosts} />
                </td>
                <td className="px-4 py-3 text-muted">
                  <PublishedPages job={j} />
                </td>
                <td className="px-4 py-3 text-muted tabular-nums">{j.aspect ?? '—'}</td>
                <td className="px-4 py-3 text-muted">{fmtDate(j.createdAt)}</td>
                <td className="px-4 py-3 text-muted tabular-nums">{runTime(j)}</td>
                <td className="px-4 py-3">
                  <div className="flex flex-col items-start gap-1.5">
                    <StatusBadge status={j.status} />
                    {j.status === 'needs_input' && j.needsInput && <SourceCreditButton job={j} />}
                  </div>
                </td>
                <td className="px-4 py-3 text-right">
                  <div className="inline-flex items-center gap-1.5">
                    {j.status === 'running' && <StopJobButton job={j} />}
                    {j.status === 'stopped' && <ResumeJobButton job={j} />}
                    <DeleteJobButton job={j} />
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      {/* Mobile cards */}
      <div className="space-y-3 md:hidden">
        {filtered.map((j) => (
          <Card key={j.id} className="p-4">
            <div className="flex items-start justify-between gap-3">
              <div className="flex min-w-0 items-center gap-2">
                <InputIcon type={j.inputType} />
                <span className="truncate text-sm font-medium">{j.inputPayload}</span>
                {j.inputType === 'link' && j.inputPayload.trim() && (
                  <CopySourceButton url={j.inputPayload} />
                )}
              </div>
              <StatusBadge status={j.status} />
            </div>
            {(() => {
              const produced = videos.find((v) => v.jobId === j.id)
              return produced ? (
                <div className="mt-1 flex items-center gap-1 text-xs text-muted">
                  <span className="shrink-0">Tên video:</span>
                  <EditVideoTitle video={produced} />
                </div>
              ) : (
                <div className="mt-1 truncate text-xs text-muted" title={videoTitle(j)}>
                  Tên video: <span className="text-fg">{videoTitle(j)}</span>
                </div>
              )
            })()}
            <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
              <span>#{j.pageSeq ?? j.id}</span>
              <span className="tabular-nums">{j.aspect ?? '—'}</span>
              <span>{fmtDate(j.createdAt)}</span>
              <span className="tabular-nums">{runTime(j)}</span>
            </div>
            {publishedPageNames(j).length > 0 && (
              <div className="mt-1.5 flex items-center gap-2 text-xs text-muted">
                <span className="shrink-0">Trang:</span>
                <PublishedPages job={j} />
              </div>
            )}
            {j.status === 'needs_input' && j.needsInput && (
              <div className="mt-2">
                <SourceCreditButton job={j} />
              </div>
            )}
            <div className="mt-1.5 flex items-center justify-between gap-3 text-xs">
              <PublishedLinks posts={j.publishedPosts} />
              <div className="inline-flex items-center gap-1.5">
                {j.status === 'running' && <StopJobButton job={j} />}
                {j.status === 'stopped' && <ResumeJobButton job={j} />}
                <DeleteJobButton job={j} />
              </div>
            </div>
          </Card>
        ))}
      </div>
      </>
      )}
    </div>
  )
}

// Per-row stop: appears only when job.status === 'running'. No confirm modal —
// the button is destructive enough that a single deliberate click is sufficient.
function StopJobButton({ job }: { job: Job }) {
  const stop = useStopJob()
  const { success, error: toastError } = useToast()
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const doStop = async () => {
    setBusy(true)
    setErr(null)
    try {
      await stop(job.id)
      success('Đã dừng job')
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
      toastError('Không dừng được job')
      setBusy(false)
    }
  }

  return (
    <>
      <button
        onClick={doStop}
        disabled={busy}
        title="Dừng job đang chạy"
        aria-label="Dừng job"
        className="inline-flex items-center justify-center rounded-md border border-amber-500/50 bg-amber-500/10 px-2 py-1 text-amber-400 transition hover:bg-amber-500/20 hover:text-amber-300 disabled:opacity-50"
      >
        {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Square className="h-3.5 w-3.5 fill-current" />}
      </button>
      {err && <p className="mt-1 line-clamp-2 text-right text-[10px] text-amber-400">{err}</p>}
    </>
  )
}

// Per-row resume: appears only when job.status === 'stopped'. Hits the SAME
// retry endpoint (POST /api/jobs/{id}/retry); the backend RESUMES a stopped job
// from where it left off (vs. cold re-run for a failed job). Neutral gray
// treatment to match the 'stopped' badge — this is not an error action.
function ResumeJobButton({ job }: { job: Job }) {
  const refresh = useRefresh()
  const { success, error: toastError } = useToast()
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const doResume = async () => {
    setBusy(true)
    setErr(null)
    try {
      await api.retryJob(job.id)
      await refresh()
      success('Đã tiếp tục job')
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
      toastError('Không tiếp tục được job')
      setBusy(false)
    }
  }

  return (
    <>
      <button
        onClick={doResume}
        disabled={busy}
        title="Tiếp tục job đã dừng"
        aria-label="Tiếp tục job"
        className="inline-flex items-center gap-1 rounded-md border border-slate-400/50 bg-slate-400/10 px-2 py-1 text-xs font-medium text-slate-600 transition hover:bg-slate-400/20 disabled:opacity-50 dark:text-slate-300"
      >
        {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
        Tiếp tục
      </button>
      {err && <p className="mt-1 line-clamp-2 text-right text-[10px] text-slate-400">{err}</p>}
    </>
  )
}

// Per-row delete: a small icon button + a confirm modal (it also deletes files
// on disk, so we confirm). The actual delete + list refresh go through the
// shared useDeleteEntity hook so behavior matches the Videos delete exactly.
function DeleteJobButton({ job }: { job: Job }) {
  const remove = useDeleteEntity()
  const { success, error: toastError } = useToast()
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const doDelete = async () => {
    setConfirm(false)
    setBusy(true)
    setErr(null)
    try {
      await remove('job', job.id)
      // On success the dataset re-fetches and this row unmounts; nothing else
      // to do (no setBusy(false) needed because the component goes away).
      success('Đã xóa job')
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
      toastError('Xóa job thất bại')
      setBusy(false)
    }
  }

  return (
    <>
      <button
        onClick={() => setConfirm(true)}
        disabled={busy}
        title="Xoá job (cả file ổ cứng)"
        aria-label="Xoá job"
        className="inline-flex items-center justify-center rounded-md border border-rose-500/50 bg-rose-500/10 px-2 py-1 text-rose-400 transition hover:bg-rose-500/20 hover:text-rose-300 disabled:opacity-50"
      >
        {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
      </button>
      {err && <p className="mt-1 line-clamp-2 text-right text-[10px] text-rose-400">{err}</p>}
      {confirm && (
        <Modal open onClose={() => setConfirm(false)} title="Xoá job">
          <div className="space-y-4">
            <p className="text-sm text-muted">
              Xoá job “<span className="font-medium text-fg">{job.inputPayload}</span>” khỏi hàng đợi và xoá luôn file của nó trên ổ cứng? Không thể hoàn tác.
            </p>
            {job.status === 'running' && (
              <p className="text-xs text-amber-400">Job đang chạy có thể không xoá được — hãy đợi nó dừng.</p>
            )}
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setConfirm(false)}
                className="rounded-lg border border-line bg-panel2 px-4 py-2 text-sm text-fg transition hover:border-brand/40"
              >
                Huỷ
              </button>
              <button
                onClick={doDelete}
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

function InputIcon({ type }: { type: 'prompt' | 'link' }) {
  const Icon = type === 'link' ? Link2 : Type
  return <Icon className="h-4 w-4 shrink-0 text-brand" aria-label={type} />
}

// Inline edit of a PRODUCED video's title, right in the history list. Saving hits
// PATCH /api/videos/{id}, which updates videos.title — the SINGLE source of truth
// — then refreshes the shared dataset. Because every view (this list, the Video
// library, page "Sản phẩm", Overview, the publish modal's default caption) derives
// its title from that same store, the rename shows EVERYWHERE with no extra wiring.
function EditVideoTitle({ video }: { video: Video }) {
  const refresh = useRefresh()
  const { success, error: toastError } = useToast()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(video.title ?? '')
  const [saving, setSaving] = useState(false)
  const current = video.title ?? ''

  const save = async () => {
    const next = draft.trim()
    if (!next || next === current) {
      setEditing(false)
      setDraft(current)
      return
    }
    setSaving(true)
    try {
      await api.updateVideoTitle(video.id, next)
      await refresh() // re-fetch the shared store → all views reflect the new title
      setEditing(false)
      success('Đã đổi tiêu đề')
    } catch (e) {
      toastError(e instanceof Error ? e.message : 'Đổi tiêu đề thất bại')
    } finally {
      setSaving(false)
    }
  }

  if (editing) {
    return (
      <div className="flex items-center gap-1">
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') { e.preventDefault(); void save() }
            if (e.key === 'Escape') { setEditing(false); setDraft(current) }
          }}
          autoFocus
          className="min-w-0 flex-1 rounded border border-line bg-panel px-1.5 py-1 text-xs leading-snug text-fg outline-none focus:border-brand/50 focus:ring-1 focus:ring-brand/30"
          aria-label="Sửa tiêu đề video"
        />
        <button
          type="button"
          onClick={() => void save()}
          disabled={saving}
          title="Lưu"
          aria-label="Lưu tiêu đề"
          className="grid h-6 w-6 shrink-0 place-items-center rounded text-emerald-500 transition hover:bg-emerald-500/10 disabled:opacity-50"
        >
          {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
        </button>
        <button
          type="button"
          onClick={() => { setEditing(false); setDraft(current) }}
          disabled={saving}
          title="Hủy"
          aria-label="Hủy sửa tiêu đề"
          className="grid h-6 w-6 shrink-0 place-items-center rounded text-muted transition hover:bg-panel2 hover:text-fg disabled:opacity-50"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
    )
  }

  return (
    <div className="flex items-center gap-1">
      <span className="min-w-0 flex-1 truncate text-fg" title={current || undefined}>
        {current || <span className="italic text-muted">Chưa có tiêu đề</span>}
      </span>
      <button
        type="button"
        onClick={() => { setDraft(current); setEditing(true) }}
        title="Đổi tên video"
        aria-label="Đổi tên video"
        className="grid h-6 w-6 shrink-0 place-items-center rounded text-muted transition hover:bg-panel2 hover:text-fg"
      >
        <Pencil className="h-3.5 w-3.5" />
      </button>
    </div>
  )
}

// Copy the source link (a 'link' job's input URL) to the clipboard, with a brief
// checkmark. Rendered only for 'link' jobs — a 'prompt' job's payload is topic
// text, not a URL.
function CopySourceButton({ url }: { url: string }) {
  const { success, error: toastError } = useToast()
  const [copied, setCopied] = useState(false)
  const doCopy = async () => {
    try {
      await navigator.clipboard.writeText(url)
      setCopied(true)
      success('Đã copy source link')
      setTimeout(() => setCopied(false), 1500)
    } catch {
      toastError('Không copy được link')
    }
  }
  return (
    <button
      type="button"
      onClick={doCopy}
      title="Copy source link"
      aria-label="Copy source link"
      className="inline-flex shrink-0 items-center justify-center rounded-md border border-line bg-panel2 p-1 text-muted transition hover:border-brand/40 hover:text-fg"
    >
      {copied ? <Check className="h-3.5 w-3.5 text-emerald-500" /> : <Copy className="h-3.5 w-3.5" />}
    </button>
  )
}
