import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from 'react'
import { AlertCircle, CheckCircle2, ExternalLink, Film, Info, Loader2, Square, UploadCloud, XCircle } from 'lucide-react'
import { api, ApiError } from '../api'
import { useRefresh } from '../data'
import type { AllLinkedChannelsPage, Platform, PublishPreflight, PublishResult } from '../types'
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

// Column order for the per-platform layout (platforms not present are dropped).
const PLATFORM_ORDER: Platform[] = ['youtube', 'facebook', 'tiktok', 'instagram']

// Per-channel posted state (from a prior post OR a just-succeeded session result),
// normalized for the "Đã đăng / Đã lên lịch / Bản nháp" row display.
interface PostedInfo {
  status: 'posted' | 'scheduled' | 'draft'
  url?: string | null
  pageName: string
}

function PublishModal({ target, onClose }: { target: PublishTarget; onClose: () => void }) {
  const refresh = useRefresh()
  const { success, error: toastError } = useToast()
  // Connected channels across ALL pages, grouped by page (many-to-many publish).
  const [pages, setPages] = useState<AllLinkedChannelsPage[] | null>(null)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  // accountId → ticked. Default ALL ticked once channels load.
  const [picked, setPicked] = useState<Record<number, boolean>>({})
  // Facebook timing: publish now (public) or schedule for later.
  const [fbState, setFbState] = useState<'PUBLISHED' | 'SCHEDULED'>('PUBLISHED')
  // datetime-local value (local wall-clock string, "" until picked).
  const [scheduledAt, setScheduledAt] = useState<string>('')
  // Per-platform preflight for THIS video (Facebook Reel vs post, YouTube Short vs
  // video), decided by its aspect ratio / duration on the server. Feeds each
  // platform column's video-specific info line.
  const [preflight, setPreflight] = useState<PublishPreflight | null>(null)
  // Publishing is PER-PLATFORM now: which platform columns are mid-request (each
  // column publishes independently; others stay interactive), and the merged
  // per-channel results (successes flip the channel to a "Đã đăng" row; failures
  // show inline + stay re-tickable). Keyed by platform / accountId respectively.
  const [busyByPlatform, setBusyByPlatform] = useState<Record<string, boolean>>({})
  const [resultsByAccount, setResultsByAccount] = useState<Record<number, PublishResult>>({})
  // Live upload progress per platform (real %, polled while a column is busy).
  // Only platforms the backend tracks (facebook) ever populate this.
  const [progressByPlatform, setProgressByPlatform] = useState<
    Record<string, { pct?: number; phase?: string; bytesSent?: number; bytesTotal?: number }>
  >({})
  // Shared caption applied to EVERY platform. `description` is the body verbatim
  // (no credit line); `includeSource` appends the source credit server-side.
  // Description DEFAULTS to the video title (the owner can freely edit/clear it);
  // includeSource defaults off — the owner opts in by ticking.
  const [description, setDescription] = useState(target.title ?? '')
  const [includeSource, setIncludeSource] = useState(false)

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

  // Preflight per-platform handling for THIS video. Cheap path-only probe;
  // best-effort (a failure just hides the hints, never blocks publishing).
  useEffect(() => {
    let alive = true
    setPreflight(null)
    api
      .getPublishPreflight(target.id)
      .then((m) => alive && setPreflight(m))
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [target.id])

  // Poll live upload progress for every platform column that is currently
  // publishing. One shared 1s interval covers all busy columns; it re-establishes
  // whenever the busy set changes and is torn down when nothing is busy or on
  // unmount (cleanup clears the interval + guards against late writes). Best-effort.
  useEffect(() => {
    const busyPlatforms = Object.keys(busyByPlatform).filter((p) => busyByPlatform[p])
    if (busyPlatforms.length === 0) return
    let alive = true
    const poll = () => {
      busyPlatforms.forEach((platform) => {
        api
          .getPublishProgress(target.id, platform)
          .then((prog) => {
            if (!alive || !prog.active) return
            setProgressByPlatform((prev) => ({
              ...prev,
              [platform]: { pct: prog.pct, phase: prog.phase, bytesSent: prog.bytesSent, bytesTotal: prog.bytesTotal },
            }))
          })
          .catch(() => {})
      })
    }
    poll()
    const id = setInterval(poll, 1000)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [busyByPlatform, target.id])

  // Flat list of every channel, each tagged with its owning page's id + name (for
  // the column rows and synthetic error results, which need pageId/pageName).
  const flatChannels = (pages ?? []).flatMap((p) =>
    p.channels.map((c) => ({ ...c, pageName: p.pageName, pageId: p.pageId })),
  )
  const allChannels = (pages ?? []).flatMap((p) => p.channels)
  const hasChannels = pages != null && allChannels.length > 0

  // One COLUMN per platform (only platforms that actually have a connected
  // channel), in a stable order. Each column carries its own channels + info.
  const platformCols = PLATFORM_ORDER.map((platform) => ({
    platform,
    channels: flatChannels.filter((c) => c.platform === platform),
  })).filter((col) => col.channels.length > 0)

  // Friendly resolution/aspect summary for the header (e.g. "1920×1080 · 305s").
  const videoShape = preflight
    ? [
        preflight.width && preflight.height ? `${preflight.width}×${preflight.height}` : null,
        preflight.duration != null ? `${Math.round(preflight.duration)}s` : null,
      ]
        .filter(Boolean)
        .join(' · ')
    : ''

  // Prior publishes of this video, per channel (for the "already posted" rows).
  const priorPosts = preflight?.posts ?? []
  const postsOf = (accountId: number) => priorPosts.filter((p) => p.accountId === accountId)

  type FlatChannel = (typeof flatChannels)[number]

  // A channel's posted info: a just-succeeded session result wins over any prior
  // post; otherwise the best prior post (posted > scheduled > draft). null = the
  // channel is NOT posted (render a checkbox so a column publish can target it).
  const postedInfoOf = (c: FlatChannel): PostedInfo | null => {
    const r = resultsByAccount[c.accountId]
    if (r && r.ok) {
      const status: PostedInfo['status'] =
        r.state === 'SCHEDULED' ? 'scheduled' : r.state === 'DRAFT' ? 'draft' : 'posted'
      return { status, url: r.url, pageName: c.pageName }
    }
    const prior = postsOf(c.accountId)
    if (prior.length > 0) {
      const chosen =
        prior.find((p) => p.status === 'posted') ??
        prior.find((p) => p.status === 'scheduled') ??
        prior[0]
      const status: PostedInfo['status'] =
        chosen.status === 'scheduled' ? 'scheduled' : chosen.status === 'draft' ? 'draft' : 'posted'
      return { status, url: chosen.url, pageName: chosen.pageName ?? c.pageName }
    }
    return null
  }

  // Scheduling: earliest allowed time (now + 10 min) as a datetime-local string,
  // and the chosen time as unix seconds. When the Facebook column uses "Hẹn giờ",
  // the chosen time must be valid and ≥ 10 min out before its publish is allowed.
  const pad = (n: number) => String(n).padStart(2, '0')
  const toLocalInput = (d: Date) =>
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
  const minScheduleLocal = toLocalInput(new Date(Date.now() + 10 * 60 * 1000))
  const scheduledUnix = scheduledAt ? Math.floor(new Date(scheduledAt).getTime() / 1000) : null
  const scheduleValid = scheduledUnix != null && scheduledUnix >= Math.floor(Date.now() / 1000) + 600

  const toggle = (accountId: number) =>
    setPicked((p) => ({ ...p, [accountId]: !p[accountId] }))

  // The ticked, not-yet-posted accountIds of one platform column — the exact set a
  // column's "Đăng" button targets.
  const tickedIdsOf = (platform: Platform) =>
    flatChannels
      .filter((c) => c.platform === platform && !postedInfoOf(c) && picked[c.accountId])
      .map((c) => c.accountId)

  // Publish ONE platform's ticked channels. Facebook carries the timing state.
  const publishPlatform = async (platform: Platform) => {
    const ids = tickedIdsOf(platform)
    if (ids.length === 0 || busyByPlatform[platform]) return
    const label = PLATFORM_META[platform].label
    setBusyByPlatform((b) => ({ ...b, [platform]: true }))
    // Clear any stale progress from a prior attempt so no old % flashes.
    setProgressByPlatform((p) => {
      const n = { ...p }
      delete n[platform]
      return n
    })
    // Merge a batch of results into the per-channel map (successes flip to posted,
    // failures show inline).
    const merge = (rs: PublishResult[]) =>
      setResultsByAccount((prev) => {
        const next = { ...prev }
        for (const r of rs) next[r.accountId] = r
        return next
      })
    try {
      const res = await api.publishVideo(target.id, {
        accountIds: ids,
        // Shared caption + source credit apply to every platform.
        description,
        includeSource,
        ...(platform === 'facebook'
          ? {
              state: fbState,
              ...(fbState === 'SCHEDULED' && scheduledUnix ? { scheduledPublishTime: scheduledUnix } : {}),
            }
          : {}),
      })
      merge(res.results)
      const failed = res.results.filter((r) => !r.ok).length
      const ok = res.results.length - failed
      if (failed === 0) success(`Đã đăng lên ${label}`)
      else if (ok > 0) toastError(`${label}: ${failed} kênh lỗi`)
      else toastError(`${label}: đăng thất bại`)
      await refresh()
    } catch (e) {
      if (e instanceof ApiError) {
        const detail = e.detail as { results?: PublishResult[] } | null
        if (detail && Array.isArray(detail.results)) {
          // The all-failed 400 carries per-channel detail — show each inline.
          merge(detail.results)
        } else {
          // No per-channel detail: attach the mapped message to every targeted
          // channel so the error surfaces in-column (still re-tickable).
          const msg = statusMessage(e)
          merge(ids.map((id) => {
            const c = flatChannels.find((x) => x.accountId === id)
            return { accountId: id, pageId: c?.pageId ?? 0, platform, ok: false, error: msg }
          }))
        }
        toastError(`${label}: ${statusMessage(e)}`)
      } else {
        const msg = e instanceof Error ? e.message : String(e)
        merge(ids.map((id) => {
          const c = flatChannels.find((x) => x.accountId === id)
          return { accountId: id, pageId: c?.pageId ?? 0, platform, ok: false, error: msg }
        }))
        toastError(`${label}: đăng thất bại`)
      }
      await refresh()
    } finally {
      setBusyByPlatform((b) => ({ ...b, [platform]: false }))
    }
  }

  const title = target.title ? `Đăng “${target.title}”` : 'Đăng video'

  // Badge classes + Vietnamese label per posted status.
  const POSTED_BADGE: Record<PostedInfo['status'], { cls: string; label: string }> = {
    posted: { cls: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-300', label: 'Đã đăng' },
    scheduled: { cls: 'bg-sky-500/15 text-sky-600 dark:text-sky-300', label: 'Đã lên lịch' },
    draft: { cls: 'bg-amber-500/15 text-amber-600 dark:text-amber-300', label: 'Bản nháp' },
  }

  return (
    <Modal open onClose={onClose} title={title} maxWidthClass="max-w-2xl">
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

        {/* One column per platform; each publishes ITS OWN ticked channels. */}
        {hasChannels && (
          <>
            {/* Video shape summary (drives every column's per-platform hint) */}
            {videoShape && (
              <p className="flex items-center gap-1.5 text-xs text-muted">
                <Info className="h-3.5 w-3.5 shrink-0" />
                Video: <span className="font-medium text-fg">{videoShape}</span>
              </p>
            )}

            {/* Shared caption — one description + source-credit choice applied to
                EVERY platform (a single caption for all channels). */}
            <div className="space-y-2 rounded-xl border border-line bg-panel2/40 p-3">
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-muted">Mô tả (description)</span>
                <textarea
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  rows={4}
                  // Value defaults to the video title (see state init). Placeholder
                  // only shows when the owner clears it.
                  placeholder={preflight?.defaultDescription || 'Nhập mô tả (để trống nếu không cần)'}
                  className="w-full resize-y rounded-lg border border-line bg-panel px-3 py-2 text-sm text-fg outline-none transition placeholder:text-muted/70 focus:border-brand/50 focus:ring-2 focus:ring-brand/20"
                />
              </label>
              {/* Source credit — only when the video has a creditable source. */}
              {preflight?.sourceName && (
                <label className="flex cursor-pointer items-start gap-2.5">
                  <input
                    type="checkbox"
                    checked={includeSource}
                    onChange={(e) => setIncludeSource(e.target.checked)}
                    className="mt-0.5 h-4 w-4 shrink-0 accent-[var(--color-brand)]"
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm font-medium text-fg">Dẫn nguồn</span>
                    <span className="block text-[11px] text-muted">
                      Sẽ thêm: <span className="text-fg">Nguồn: {preflight.sourceName}</span>
                    </span>
                  </span>
                </label>
              )}
            </div>

            <div className={`grid gap-3 ${platformCols.length > 1 ? 'sm:grid-cols-2' : ''}`}>
              {platformCols.map((col) => {
                const meta = PLATFORM_META[col.platform]
                const Icon = meta.Icon
                const fb = preflight?.facebook
                const yt = preflight?.youtube
                const colBusy = !!busyByPlatform[col.platform]
                const tickedIds = tickedIdsOf(col.platform)
                // FB "Hẹn giờ" needs a valid schedule before this column can publish.
                const colNeedsSchedule = col.platform === 'facebook' && fbState === 'SCHEDULED'
                const canPublishCol =
                  !colBusy && tickedIds.length > 0 && (!colNeedsSchedule || scheduleValid)
                // Real upload progress (only shown once meaningful — a pct > 0 or the
                // transfer phase — so no stuck 0% bar appears for reels/youtube).
                const prog = progressByPlatform[col.platform]
                const progPct = typeof prog?.pct === 'number' ? Math.round(prog.pct) : 0
                const showProgress = colBusy && !!prog && (progPct > 0 || prog.phase === 'transfer')
                const progMB =
                  typeof prog?.bytesSent === 'number' && typeof prog?.bytesTotal === 'number' && prog.bytesTotal > 0
                    ? `${(prog.bytesSent / 1048576).toFixed(0)}/${(prog.bytesTotal / 1048576).toFixed(0)} MB`
                    : null
                return (
                  <div
                    key={col.platform}
                    className="flex flex-col gap-2 rounded-xl border border-line bg-panel2/40 p-3"
                  >
                    {/* Column header */}
                    <div className="flex items-center gap-2">
                      <Icon className={`h-4 w-4 shrink-0 ${meta.color}`} aria-hidden />
                      <span className="text-sm font-semibold text-fg">{meta.label}</span>
                    </div>

                    {/* Per-platform, video-specific info */}
                    {col.platform === 'facebook' && fb && (
                      <div className="flex items-start gap-2 rounded-lg border border-sky-500/40 bg-sky-500/10 p-2.5 text-xs text-sky-700 dark:text-sky-300">
                        {fb.mode === 'reel' ? (
                          <Film className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                        ) : (
                          <Square className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                        )}
                        <div className="min-w-0 flex-1">
                          {fb.mode === 'reel' ? (
                            <p>
                              Đăng dạng <b>Reel</b> (dọc, ≤ 90s).
                            </p>
                          ) : (
                            <p>
                              Đăng dạng <b>bài viết thường</b> — không đủ điều kiện Reel (Reel cần 9:16
                              dọc và ≤ 90s).
                            </p>
                          )}
                        </div>
                      </div>
                    )}
                    {col.platform === 'youtube' && yt && (
                      <div className="flex items-start gap-2 rounded-lg border border-sky-500/40 bg-sky-500/10 p-2.5 text-xs text-sky-700 dark:text-sky-300">
                        {yt.mode === 'short' ? (
                          <Film className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                        ) : (
                          <Square className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                        )}
                        <p>
                          {yt.mode === 'short' ? (
                            <>
                              Đăng dạng <b>Short</b> (dọc, ≤ 3 phút).
                            </>
                          ) : (
                            <>
                              Đăng dạng <b>video thường</b>.
                            </>
                          )}
                        </p>
                      </div>
                    )}

                    {/* Channels (pages) under this platform. A posted channel shows
                        a "Đã đăng/Đã lên lịch/Bản nháp" row (badge + Mở link, no
                        checkbox); a not-yet-posted one keeps its checkbox; a failed
                        session result stays checkable with the error inline. */}
                    <div className="space-y-2">
                      {col.channels.map((c) => {
                        const info = postedInfoOf(c)
                        if (info) {
                          const badge = POSTED_BADGE[info.status]
                          return (
                            <div
                              key={c.accountId}
                              className="flex items-center gap-2 rounded-lg border border-line bg-panel px-3 py-2.5"
                            >
                              <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-500" />
                              <span className="min-w-0 flex-1 truncate text-sm font-medium text-fg">
                                {info.pageName}
                              </span>
                              <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${badge.cls}`}>
                                {badge.label}
                              </span>
                              {info.url && (
                                <a
                                  href={info.url}
                                  target="_blank"
                                  rel="noreferrer"
                                  className="inline-flex shrink-0 items-center gap-1 text-xs text-brand hover:underline"
                                >
                                  Mở <ExternalLink className="h-3 w-3" />
                                </a>
                              )}
                            </div>
                          )
                        }
                        // Not posted (or a failed attempt): checkbox + optional error.
                        const failed = resultsByAccount[c.accountId]
                        return (
                          <div key={c.accountId} className="space-y-1">
                            <label className="flex cursor-pointer items-center gap-3 rounded-lg border border-line bg-panel px-3 py-2.5 transition hover:border-brand/40">
                              <input
                                type="checkbox"
                                checked={!!picked[c.accountId]}
                                onChange={() => toggle(c.accountId)}
                                disabled={colBusy}
                                className="h-4 w-4 shrink-0 accent-[var(--color-brand)] disabled:opacity-50"
                              />
                              <span className="min-w-0 flex-1 truncate text-sm font-medium text-fg">
                                {c.pageName}
                              </span>
                            </label>
                            {failed && !failed.ok && (
                              <p className="flex items-start gap-1.5 px-1 text-[11px] text-rose-600 dark:text-rose-300">
                                <XCircle className="mt-0.5 h-3 w-3 shrink-0" />
                                <span>{failed.error || 'Đăng thất bại'}</span>
                              </p>
                            )}
                          </div>
                        )
                      })}
                    </div>

                    {/* Facebook publish timing: now (public) vs scheduled */}
                    {col.platform === 'facebook' && (
                      <div className="space-y-1.5 pt-0.5">
                        <p className="text-[11px] text-muted">Thời điểm đăng:</p>
                        <div className="grid grid-cols-2 gap-2">
                          {(
                            [
                              { value: 'PUBLISHED', label: 'Đăng ngay' },
                              { value: 'SCHEDULED', label: 'Hẹn giờ' },
                            ] as const
                          ).map((opt) => {
                            const active = fbState === opt.value
                            return (
                              <button
                                key={opt.value}
                                type="button"
                                onClick={() => setFbState(opt.value)}
                                disabled={colBusy}
                                aria-pressed={active}
                                className={`rounded-lg border px-3 py-2 text-xs font-medium transition disabled:opacity-50 ${
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
                        {fbState === 'SCHEDULED' && (
                          <div className="space-y-1 pt-0.5">
                            <input
                              type="datetime-local"
                              value={scheduledAt}
                              min={minScheduleLocal}
                              onChange={(e) => setScheduledAt(e.target.value)}
                              disabled={colBusy}
                              className="w-full rounded-lg border border-line bg-panel px-2.5 py-1.5 text-xs text-fg outline-none focus:border-brand/60 disabled:opacity-50"
                            />
                            <p className="text-[10px] text-muted">
                              Tối thiểu 10 phút nữa. Bài sẽ tự đăng đúng giờ và hiện trong mục “Đã lên
                              lịch” của Trang.
                            </p>
                          </div>
                        )}
                      </div>
                    )}

                    {/* Real upload progress bar — shown while this column is
                        publishing AND the backend reports meaningful progress (feed
                        uploads stream per ~8MiB chunk). Coarse platforms (reels early,
                        youtube) show no bar — just the button spinner. */}
                    {showProgress && (
                      <div className="pt-0.5">
                        <p className="mb-1 flex items-center justify-between gap-2 text-[11px] text-muted">
                          <span className="inline-flex items-center gap-1.5">
                            <Loader2 className="h-3 w-3 animate-spin" /> Đang tải lên… {progPct}%
                          </span>
                          {progMB && <span className="tabular-nums">{progMB}</span>}
                        </p>
                        <div className="h-1.5 w-full overflow-hidden rounded-full bg-panel2">
                          <div
                            className="h-full rounded-full bg-brand transition-all duration-500"
                            style={{ width: `${Math.max(2, progPct)}%` }}
                          />
                        </div>
                      </div>
                    )}

                    {/* Per-column publish button — targets THIS platform's ticked,
                        not-yet-posted channels only. */}
                    <button
                      type="button"
                      onClick={() => void publishPlatform(col.platform)}
                      disabled={!canPublishCol}
                      title={
                        tickedIds.length === 0
                          ? 'Không còn kênh nào để đăng'
                          : colNeedsSchedule && !scheduleValid
                            ? 'Chọn thời điểm hợp lệ (tối thiểu 10 phút nữa)'
                            : undefined
                      }
                      className="mt-auto inline-flex items-center justify-center gap-2 rounded-lg border border-brand bg-brand px-3 py-2 text-sm font-medium text-white transition hover:bg-brand/90 disabled:opacity-50"
                    >
                      {colBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <UploadCloud className="h-4 w-4" />}
                      {colBusy
                        ? 'Đang đăng…'
                        : colNeedsSchedule
                          ? `Lên lịch ${meta.label}`
                          : `Đăng ${meta.label}`}
                    </button>
                  </div>
                )
              })}
            </div>
          </>
        )}

        {/* Footer — only Đóng now (publishing happens per-platform above). */}
        <div className="flex justify-end gap-2 pt-1">
          <button
            onClick={onClose}
            className="rounded-lg border border-brand/50 bg-brand px-4 py-2 text-sm font-medium text-white transition hover:bg-brand/90"
          >
            Đóng
          </button>
        </div>
      </div>
    </Modal>
  )
}
