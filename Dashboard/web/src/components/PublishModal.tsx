import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from 'react'
import { AlertCircle, CheckCircle2, ExternalLink, Loader2, UploadCloud, XCircle } from 'lucide-react'
import { api, ApiError } from '../api'
import { useRefresh } from '../data'
import type { AllLinkedChannelsPage, PublishResult } from '../types'
import { Modal, PLATFORM_META, useToast } from '../ui'

// ---- Shared publish flow ------------------------------------------------
// One modal host for EVERY "publish a video" affordance in the dashboard, in
// the spirit of useDeleteEntity. Any view calls usePublish()(video) to open the
// single global modal; behavior (channel tickboxes, validation, per-platform
// results, refresh) is therefore identical everywhere a publish button lives.

export interface PublishTarget {
  id: number // video id
  pageId: number
  title?: string
}

type Opener = (video: PublishTarget) => void

const PublishContext = createContext<Opener>(() => {
  // No provider mounted — a publish button was rendered outside <PublishProvider>.
  console.warn('usePublish() called without a <PublishProvider> ancestor')
})

/** Returns an `open(video)` that pops the shared publish modal. */
export const usePublish = () => useContext(PublishContext)

export function PublishProvider({ children }: { children: ReactNode }) {
  const [target, setTarget] = useState<PublishTarget | null>(null)
  const open = useCallback<Opener>((video) => setTarget(video), [])
  return (
    <PublishContext.Provider value={open}>
      {children}
      {target && <PublishModal target={target} onClose={() => setTarget(null)} />}
    </PublishContext.Provider>
  )
}

// Human message for the HTTP status codes the publish endpoint can return.
function statusMessage(e: ApiError): string {
  switch (e.status) {
    case 404:
      return 'Không tìm thấy video (có thể đã bị xoá).'
    case 409:
      return 'Video này đã được đăng trước đó.'
    case 422: {
      // The backend returns 422 for several DISTINCT reasons (missing video file
      // on disk, no connected account, AND sometimes an empty `accountIds` list).
      // Surface the server's ACTUAL detail rather than always blaming channel
      // selection, which misled users when a channel WAS picked.
      const detail = typeof e.detail === 'string' ? e.detail : ''
      const serverMsg = detail || e.message
      if (serverMsg) {
        // Nicety: keep the friendly Vietnamese line ONLY for the empty-selection case.
        if (/(account|channel|platform)/i.test(serverMsg) && /(empty|at least one|required|missing)/i.test(serverMsg)) {
          return 'Yêu cầu không hợp lệ — vui lòng chọn ít nhất một kênh.'
        }
        return serverMsg
      }
      return 'Yêu cầu không hợp lệ.'
    }
    case 429:
      return 'Facebook đang giới hạn tần suất đăng. Hãy thử lại sau ít phút.'
    case 400:
      return 'Tất cả kênh đăng đều thất bại — xem chi tiết bên dưới.'
    default:
      return e.message || 'Đăng thất bại.'
  }
}

// Derive the owning email for a page block from its channels (account labels
// sometimes carry the linked email). null when no channel label looks like one.
const emailOf = (pg: AllLinkedChannelsPage) =>
  pg.channels.find((c) => c.accountLabel.includes('@'))?.accountLabel ?? null

function PublishModal({ target, onClose }: { target: PublishTarget; onClose: () => void }) {
  const refresh = useRefresh()
  const { success, error: toastError } = useToast()
  // Connected channels across ALL pages, grouped by page (many-to-many publish).
  const [pages, setPages] = useState<AllLinkedChannelsPage[] | null>(null)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  // accountId → ticked. Default ALL ticked once channels load.
  const [picked, setPicked] = useState<Record<number, boolean>>({})
  const [fbState, setFbState] = useState<'PUBLISHED' | 'DRAFT'>('DRAFT')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [results, setResults] = useState<PublishResult[] | null>(null)

  // Fetch linked channels (all pages) on open. Not keyed to target.pageId — a
  // video can be published into channels belonging to any page.
  useEffect(() => {
    let alive = true
    setPages(null)
    setLoadErr(null)
    api
      .getAllLinkedChannels()
      .then((r) => {
        if (!alive) return
        setPages(r.pages)
        // ALL ticked by default, across every page's channels.
        const all = r.pages.flatMap((p) => p.channels.map((c) => [c.accountId, true] as const))
        setPicked(Object.fromEntries(all))
      })
      .catch((e) => {
        if (!alive) return
        setLoadErr(e instanceof Error ? e.message : String(e))
      })
    return () => {
      alive = false
    }
  }, [])

  // Flat list of every channel (with its owning page) + a pageId→pageName lookup
  // for rendering per-result rows.
  const allChannels = (pages ?? []).flatMap((p) => p.channels)
  const pageNameOf = (pageId: number) => (pages ?? []).find((p) => p.pageId === pageId)?.pageName ?? `Trang #${pageId}`
  const hasChannels = pages != null && allChannels.length > 0

  // Pages with at least one channel. Grouped by email so pages sharing an
  // account are shown under one email heading outside the block border.
  const channelPages = (pages ?? []).filter((pg) => pg.channels.length > 0)
  type EmailGroup = { email: string | null; pages: typeof channelPages }
  const emailGroups = channelPages.reduce<EmailGroup[]>((acc, pg) => {
    const email = emailOf(pg)
    const existing = acc.find((g) => g.email === email)
    if (existing) existing.pages.push(pg)
    else acc.push({ email, pages: [pg] })
    return acc
  }, [])
  // FB state toggle shows when any TICKED channel is facebook.
  const anyFacebookPicked = allChannels.some((c) => c.platform === 'facebook' && picked[c.accountId])
  const selectedAccountIds = allChannels.filter((c) => picked[c.accountId]).map((c) => c.accountId)
  const canConfirm = !busy && results == null && selectedAccountIds.length > 0

  const toggle = (accountId: number) =>
    setPicked((p) => ({ ...p, [accountId]: !p[accountId] }))

  const confirm = async () => {
    setBusy(true)
    setErr(null)
    try {
      const res = await api.publishVideo(target.id, {
        accountIds: selectedAccountIds,
        ...(anyFacebookPicked ? { state: fbState } : {}),
      })
      setResults(res.results)
      // Partial-success aware: all-ok → success; any failure → error toast.
      const failed = res.results.filter((r) => !r.ok).length
      if (failed === 0) success('Đã đăng video')
      else toastError(`Có ${failed} kênh đăng thất bại`)
      await refresh()
    } catch (e) {
      if (e instanceof ApiError) {
        setErr(statusMessage(e))
        // The all-failed 400 carries per-platform detail.results — show them.
        const detail = e.detail as { results?: PublishResult[] } | null
        if (detail && Array.isArray(detail.results)) setResults(detail.results)
      } else {
        setErr(e instanceof Error ? e.message : String(e))
      }
      toastError('Đăng video thất bại')
      await refresh()
    } finally {
      setBusy(false)
    }
  }

  const title = target.title ? `Đăng “${target.title}”` : 'Đăng video'

  return (
    <Modal open onClose={onClose} title={title} maxWidthClass="max-w-lg">
      <div className="space-y-4">
        {/* Loading channels */}
        {pages == null && loadErr == null && (
          <div className="flex items-center gap-2 py-6 text-sm text-muted">
            <Loader2 className="h-4 w-4 animate-spin" /> Đang tải danh sách kênh…
          </div>
        )}

        {/* Failed to load channels */}
        {loadErr != null && (
          <div className="flex items-start gap-2 rounded-lg border border-rose-500/40 bg-rose-500/10 p-3 text-sm text-rose-600 dark:text-rose-300">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>Không tải được danh sách kênh: {loadErr}</span>
          </div>
        )}

        {/* No channels linked on any page */}
        {pages != null && !hasChannels && (
          <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-700 dark:text-amber-300">
            Chưa có kênh nào liên kết ở bất kỳ trang nào — hãy liên kết ở mục Đăng tải.
          </div>
        )}

        {/* Channel checkboxes — grouped by email account, one block per group */}
        {hasChannels && results == null && (
          <>
            <div className="space-y-3">
              {emailGroups.map((group) => (
                <div key={group.email ?? '__no_email__'} className="space-y-1.5">
                  {group.email && (
                    <p className="truncate px-1 text-[11px] font-medium text-muted">{group.email}</p>
                  )}
                  <div className="space-y-2 rounded-xl border border-line bg-panel2/40 p-3">
                    {group.pages.flatMap((pg) =>
                      pg.channels.map((c) => {
                        const meta = PLATFORM_META[c.platform]
                        const Icon = meta.Icon
                        return (
                          <label
                            key={c.accountId}
                            className="flex cursor-pointer items-center gap-3 rounded-lg border border-line bg-panel px-3 py-2.5 transition hover:border-brand/40"
                          >
                            <input
                              type="checkbox"
                              checked={!!picked[c.accountId]}
                              onChange={() => toggle(c.accountId)}
                              className="h-4 w-4 shrink-0 accent-[var(--color-brand)]"
                            />
                            <Icon className={`h-4 w-4 shrink-0 ${meta.color}`} aria-hidden />
                            <span className="min-w-0 flex-1 truncate text-sm font-medium text-fg">
                              {pg.pageName}
                            </span>
                            <span className="shrink-0 text-xs text-muted">{meta.label}</span>
                          </label>
                        )
                      })
                    )}
                  </div>
                </div>
              ))}
            </div>

            {/* Facebook Reels publish state — shown when a facebook channel is ticked */}
            {anyFacebookPicked && (
              <div className="space-y-2">
                <p className="text-xs text-muted">Trạng thái đăng Facebook Reel:</p>
                <div className="grid grid-cols-2 gap-2">
                  {(
                    [
                      { value: 'DRAFT', label: 'Lưu nháp (Draft)' },
                      { value: 'PUBLISHED', label: 'Đăng công khai (Public)' },
                    ] as const
                  ).map((opt) => {
                    const active = fbState === opt.value
                    return (
                      <button
                        key={opt.value}
                        type="button"
                        onClick={() => setFbState(opt.value)}
                        aria-pressed={active}
                        className={`rounded-lg border px-3 py-2 text-sm font-medium transition ${
                          active
                            ? 'border-brand/60 bg-brand/15 text-fg'
                            : 'border-line bg-panel2 text-muted hover:border-brand/40 hover:text-fg'
                        }`}
                      >
                        {opt.label}
                      </button>
                    )
                  })}
                </div>
              </div>
            )}
          </>
        )}

        {/* Top-level error (e.g. 409 already published, 429 rate limit) */}
        {err && (
          <div className="flex items-start gap-2 rounded-lg border border-rose-500/40 bg-rose-500/10 p-3 text-sm text-rose-600 dark:text-rose-300">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{err}</span>
          </div>
        )}

        {/* Per-channel results: which page + platform each target was */}
        {results != null && (
          <div className="space-y-2">
            <p className="text-sm font-medium text-fg">Kết quả đăng:</p>
            {results.map((r) => {
              const meta = PLATFORM_META[r.platform]
              return (
                <div
                  key={r.accountId}
                  className={`flex items-start gap-2 rounded-lg border px-3 py-2 text-sm ${
                    r.ok
                      ? 'border-emerald-500/40 bg-emerald-500/10'
                      : 'border-rose-500/40 bg-rose-500/10'
                  }`}
                >
                  {r.ok ? (
                    <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" />
                  ) : (
                    <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-rose-500" />
                  )}
                  <div className="min-w-0 flex-1">
                    <span className="font-medium text-fg">{meta?.label ?? r.platform}</span>
                    <span className="ml-1 text-xs text-muted">· {pageNameOf(r.pageId)}</span>
                    {r.ok ? (
                      <span className="ml-1 text-muted">
                        {r.state === 'DRAFT' ? 'đã lưu nháp' : 'đã đăng'}
                      </span>
                    ) : (
                      <span className="ml-1 text-rose-600 dark:text-rose-300">
                        {r.error || 'thất bại'}
                      </span>
                    )}
                    {r.ok && r.url && (
                      <a
                        href={r.url}
                        target="_blank"
                        rel="noreferrer"
                        className="mt-0.5 inline-flex items-center gap-1 text-xs text-brand hover:underline"
                      >
                        Mở liên kết <ExternalLink className="h-3 w-3" />
                      </a>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )}

        {/* Footer */}
        <div className="flex justify-end gap-2 pt-1">
          {results != null ? (
            <button
              onClick={onClose}
              className="rounded-lg border border-brand/50 bg-brand px-4 py-2 text-sm font-medium text-white transition hover:bg-brand/90"
            >
              Đóng
            </button>
          ) : (
            <>
              <button
                onClick={onClose}
                disabled={busy}
                className="rounded-lg border border-line bg-panel2 px-4 py-2 text-sm text-fg transition hover:border-brand/40 disabled:opacity-50"
              >
                Huỷ
              </button>
              <button
                onClick={confirm}
                disabled={!canConfirm}
                title={!hasChannels ? 'Chưa có kênh nào liên kết' : undefined}
                className="inline-flex items-center justify-center gap-2 rounded-lg border border-brand bg-brand px-4 py-2 text-sm font-medium text-white transition hover:bg-brand/90 disabled:opacity-50"
              >
                {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <UploadCloud className="h-4 w-4" />}
                Đăng
              </button>
            </>
          )}
        </div>
      </div>
    </Modal>
  )
}
