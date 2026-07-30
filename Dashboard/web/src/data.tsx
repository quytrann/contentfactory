import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import type { AppData } from './types'
import { api } from './api'
import type { ResumeJobBody } from './api'
import { useToast } from './ui'

// The dashboard reads everything through this context. It fetches live data
// from the local API on mount and on a background interval. There is no
// sample/mock fallback: if the API is unreachable the data stays empty and the
// views render their own empty states (the app never crashes).

const EMPTY: AppData = {
  pages: [],
  accounts: [],
  jobs: [],
  videos: [],
  analytics: {
    kpis: [],
    viewsDaily: [],
    likesDaily: [],
    dayLabels: [],
    videosMonthly: [],
    platformSplit: [],
  },
  org: { dashboard: 'Content Factory', accounts: [] },
}

export type DataSource = 'live' | 'empty'

interface DataState {
  data: AppData
  loading: boolean
  error: string | null
  source: DataSource
}

interface DataContextValue extends DataState {
  refresh: () => Promise<void>
}

const DataContext = createContext<DataContextValue>({
  data: EMPTY,
  loading: false,
  error: null,
  source: 'empty',
  refresh: async () => {},
})

export function DataProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<DataState>({
    data: EMPTY,
    loading: true,
    error: null,
    source: 'empty',
  })

  const refresh = useCallback(async () => {
    try {
      const r = await fetch('/api/bootstrap')
      if (!r.ok) throw new Error(`API responded ${r.status}`)
      const data = (await r.json()) as AppData
      setState({ data, loading: false, error: null, source: 'live' })
    } catch (e: unknown) {
      setState((prev) => ({
        // Keep any live data already loaded; otherwise stay on empty data so
        // the views render their empty states instead of crashing.
        data: prev.source === 'live' ? prev.data : EMPTY,
        loading: false,
        error: e instanceof Error ? e.message : String(e),
        source: prev.source === 'live' ? 'live' : 'empty',
      }))
    }
  }, [])

  // Initial load, then a quiet background refresh so newly produced/published
  // videos and job progress appear without a manual reload.
  useEffect(() => {
    void refresh()
    const id = setInterval(() => void refresh(), 12000)
    return () => clearInterval(id)
  }, [refresh])

  return <DataContext.Provider value={{ ...state, refresh }}>{children}</DataContext.Provider>
}

/** The dataset to render (live or mock fallback). */
export const useData = () => useContext(DataContext).data

/** Connection status, for the header indicator. */
export function useDataStatus() {
  const { loading, error, source } = useContext(DataContext)
  return { loading, error, source }
}

/** Re-fetch everything from the API (call after a mutation). */
export const useRefresh = () => useContext(DataContext).refresh

// ---- Shared entity delete ----------------------------------------------
// One mechanism for every server-side "delete a list row" action (jobs +
// videos). Both endpoints behave identically: remove the DB row, clean up the
// files on disk server-side, then we re-fetch the whole dataset so every view
// (and any contiguous client-side index) renumbers automatically. Callers own
// their own confirm UI; this hook only runs the delete + refresh.

export type DeletableEntity = 'job' | 'video'

/**
 * Returns a `delete(kind, id, opts?)` that calls the matching DELETE endpoint and
 * refreshes the dataset on success. Errors (e.g. 409 running job, 404) bubble
 * up so the caller can surface a brief message; no refresh happens on failure.
 * `opts.keepScript` applies only to videos (forwarded as ?keepScript=true) so a
 * deleted video keeps its reusable script; ignored for jobs.
 */
export function useDeleteEntity() {
  const refresh = useRefresh()
  const { error: toastError } = useToast()
  return useCallback(
    async (kind: DeletableEntity, id: number, opts?: { keepScript?: boolean }) => {
      const result = kind === 'job' ? await api.deleteJob(id) : await api.deleteVideo(id, opts)
      // A delete can succeed in the DB while the media file stays on disk because
      // something still has it open — most often THIS dashboard streaming the mp4 into a
      // <video> preview. The backend retries and then queues the file, but the owner has
      // to be told: silently swallowing this is how 5.41 GB of orphans accumulated.
      // Warned here (the single shared delete path) so every caller is covered.
      //
      // Only lockedFiles is a problem. This used to warn on skippedFiles, which ALSO
      // counts files the backend deliberately KEPT because a surviving video still points
      // at them (page-scoped per-scene audio) — so deleting one job reported 91 files as
      // "locked" when none were, and promised a retry that must never happen for them.
      // Falls back to skippedFiles only when the API predates the split.
      const locked = result.lockedFiles?.length
        ?? (result.keptFiles ? 0 : result.skippedFiles?.length ?? 0)
      if (locked > 0) {
        toastError(
          `Đã xoá bản ghi, nhưng ${locked} tệp còn bị khoá trên đĩa (có thể đang mở xem). ` +
          `Hệ thống sẽ tự xoá lại khi khởi động lại API.`,
        )
      }
      await refresh()
      return result
    },
    [refresh, toastError],
  )
}

// ---- Stop a running job ------------------------------------------------
export function useStopJob() {
  const refresh = useRefresh()
  return useCallback(
    async (jobId: number) => {
      const result = await api.stopJob(jobId)
      await refresh()
      return result
    },
    [refresh],
  )
}

// ---- Resume a paused (needs_input) Dubbed job --------------------------
// Returns a `resume(jobId, body)` that hits POST /api/jobs/{jobId}/resume and
// refreshes the dataset on success, so the job leaves the 'needs_input' state in
// every view. Errors (404/409) bubble up for the caller to surface; no refresh
// happens on failure. Same pattern as useDeleteEntity.
export function useResumeJob() {
  const refresh = useRefresh()
  return useCallback(
    async (jobId: number, body: ResumeJobBody) => {
      const result = await api.resumeJob(jobId, body)
      await refresh()
      return result
    },
    [refresh],
  )
}

// ---- New-video notification badge --------------------------------------
// Tracks how many videos have appeared since the user last visited the Videos
// view. The video set comes from the polled bootstrap data (data.tsx refreshes
// every 12s), so when a job completes and produces a new video row, its id shows
// up here and the count grows. `markSeen()` (called when the user opens Videos or
// clicks the badge) snapshots the current ids as the new baseline and zeroes the
// badge. The Videos view also calls refresh() on open via its own wiring.

interface NewVideosContextValue {
  count: number
  markSeen: () => void
}

const NewVideosContext = createContext<NewVideosContextValue>({
  count: 0,
  markSeen: () => {},
})

export function NewVideosProvider({ children }: { children: ReactNode }) {
  const { videos } = useData()
  const { source } = useDataStatus()
  // Track only IDs of successfully-produced videos (ready/published). This
  // prevents the badge from firing when a job merely starts (rendering) or
  // fails — it only fires when a video becomes ready to watch.
  const seenIdsRef = useRef<Set<number> | null>(null)
  const [count, setCount] = useState(0)

  useEffect(() => {
    // Ignore empty/error state (API down or still starting). Only process live
    // data so a restart gap (source='empty', videos=[]) can't consume the null
    // baseline with an empty Set and then treat all real videos as unseen.
    if (source !== 'live') return
    const readyIds = videos.filter((v) => v.status === 'ready' || v.status === 'published').map((v) => v.id)
    if (seenIdsRef.current === null) {
      // First live dataset: treat everything as already seen (no badge on cold start).
      seenIdsRef.current = new Set(readyIds)
      return
    }
    // Count ready/published ids not in the seen-baseline = genuinely new done videos.
    const unseen = readyIds.filter((id) => !seenIdsRef.current!.has(id)).length
    setCount(unseen)
  }, [videos, source])

  const markSeen = useCallback(() => {
    seenIdsRef.current = new Set(videos.filter((v) => v.status === 'ready' || v.status === 'published').map((v) => v.id))
    setCount(0)
  }, [videos])

  return (
    <NewVideosContext.Provider value={{ count, markSeen }}>{children}</NewVideosContext.Provider>
  )
}

/** New-video badge count + a markSeen() to clear it (call when opening Videos). */
export const useNewVideos = () => useContext(NewVideosContext)

// ---- System feature flags (GET /api/system) ----------------------------
// Polls the system endpoint for feature flags shared across the app — currently
// only `apiUploadEnabled`, which gates every publish (Đăng) affordance. Kept in a
// single provider so the many VideoCards read one shared value instead of each
// polling /api/system. Absent flag / unreachable API → treated as ENABLED (true)
// so a transient error never hides working publish buttons.

interface SystemContextValue {
  apiUploadEnabled: boolean
}

const SystemContext = createContext<SystemContextValue>({ apiUploadEnabled: true })

export function SystemProvider({ children }: { children: ReactNode }) {
  // Default true: absence of the flag (older API) or a failed fetch must not hide
  // the publish buttons; only an explicit `false` disables them.
  const [apiUploadEnabled, setApiUploadEnabled] = useState(true)

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const s = await api.getSystem()
        if (alive) setApiUploadEnabled(s.apiUploadEnabled !== false)
      } catch {
        // Leave the last known value; a transient error shouldn't flip the UI.
      }
    }
    void tick()
    const id = setInterval(() => void tick(), 15000)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [])

  return <SystemContext.Provider value={{ apiUploadEnabled }}>{children}</SystemContext.Provider>
}

/** Whether publishing via the platform APIs is currently enabled (gates Đăng UI). */
export const useApiUploadEnabled = () => useContext(SystemContext).apiUploadEnabled
