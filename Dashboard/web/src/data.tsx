import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import type { AppData } from './types'
import { api } from './api'
import type { ResumeJobBody } from './api'

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
 * Returns a `delete(kind, id)` that calls the matching DELETE endpoint and
 * refreshes the dataset on success. Errors (e.g. 409 running job, 404) bubble
 * up so the caller can surface a brief message; no refresh happens on failure.
 */
export function useDeleteEntity() {
  const refresh = useRefresh()
  return useCallback(
    async (kind: DeletableEntity, id: number) => {
      const result = kind === 'job' ? await api.deleteJob(id) : await api.deleteVideo(id)
      await refresh()
      return result
    },
    [refresh],
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
