import { useEffect, useState } from 'react'
import {
  Clapperboard,
  LayoutGrid,
  ListChecks,
  Moon,
  Send,
  Sun,
  Video as VideoIcon,
  Factory,
  Wand2,
} from 'lucide-react'
import Overview from './views/Overview'
import Pages from './views/Pages'
import PageDetail from './views/PageDetail'
import CreateVideo from './views/CreateVideo'
import Jobs from './views/Jobs'
import Videos from './views/Videos'
import Publishing from './views/Publishing'
import { PublishProvider } from './components/PublishModal'
import { ToastProvider } from './ui'
import { useData, useDataStatus } from './data'

type View = 'overview' | 'pages' | 'create-video' | 'jobs' | 'videos' | 'publishing' | 'page-detail'

const NAV = [
  { key: 'overview', label: 'Tổng quan', Icon: LayoutGrid },
  { key: 'create-video', label: 'Tạo Video', Icon: Wand2 },
  { key: 'videos', label: 'Video', Icon: VideoIcon },
  { key: 'pages', label: 'Trang', Icon: Clapperboard },
  { key: 'jobs', label: 'Lịch sử Video', Icon: ListChecks },
  { key: 'publishing', label: 'Thông tin Platform', Icon: Send },
] as const

// View key → breadcrumb label (Vietnamese).
const VIEW_LABEL: Record<string, string> = {
  overview: 'Tổng quan',
  pages: 'Trang',
  'create-video': 'Tạo Video',
  jobs: 'Lịch sử Video',
  videos: 'Video',
  publishing: 'Thông tin Platform',
  'page-detail': 'Chi tiết trang',
}

type Theme = 'light' | 'dark'

const VIEW_KEYS: View[] = ['overview', 'pages', 'create-video', 'jobs', 'videos', 'publishing', 'page-detail']

// Restore the last view on refresh (F5 stays put) instead of forcing Overview.
// The current view + selected page are persisted to localStorage on every
// change; on mount we read them back. A persisted 'page-detail' without a valid
// page id falls back to the Pages list so the body never renders blank.
function initialView(): View {
  try {
    const v = localStorage.getItem('cf-view') as View | null
    if (v && VIEW_KEYS.includes(v)) {
      if (v === 'page-detail' && !Number.isFinite(Number(localStorage.getItem('cf-page-id')))) {
        return 'pages'
      }
      return v
    }
  } catch {
    /* ignore */
  }
  return 'overview'
}

function initialPageId(): number | null {
  try {
    const raw = localStorage.getItem('cf-page-id')
    const n = Number(raw)
    return raw != null && Number.isFinite(n) ? n : null
  } catch {
    return null
  }
}

export default function App() {
  const [view, setView] = useState<View>(initialView)
  const [selectedPageId, setSelectedPageId] = useState<number | null>(initialPageId)
  const [theme, setTheme] = useState<Theme>(
    () => (document.documentElement.dataset.theme as Theme) || 'dark',
  )

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try {
      localStorage.setItem('cf-theme', theme)
    } catch {
      /* ignore */
    }
  }, [theme])

  // Persist the active view + selected page so a hard reload (F5) restores the
  // current location instead of jumping back to Overview.
  useEffect(() => {
    try {
      localStorage.setItem('cf-view', view)
      if (selectedPageId != null) localStorage.setItem('cf-page-id', String(selectedPageId))
      else localStorage.removeItem('cf-page-id')
    } catch {
      /* ignore */
    }
  }, [view, selectedPageId])

  const { loading, source } = useDataStatus()
  const { pages } = useData()

  const toggleTheme = () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))

  const openPage = (id: number) => {
    setSelectedPageId(id)
    setView('page-detail')
  }

  const navActive = view === 'page-detail' ? 'pages' : view

  return (
    <ToastProvider>
    <PublishProvider>
    <div className="min-h-full">
      {/* Sidebar — desktop only */}
      <aside className="fixed inset-y-0 left-0 hidden w-60 flex-col border-r border-line bg-panel/60 px-3 py-5 md:flex">
        <Brand />
        <nav className="mt-8 flex flex-col gap-1">
          {NAV.map(({ key, label, Icon }) => (
            <div key={key} className="flex flex-col">
              <button
                onClick={() => setView(key)}
                className={`flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition active:scale-[.98] ${
                  navActive === key ? 'bg-brand/10 text-brand ring-1 ring-inset ring-brand/20' : 'text-muted hover:bg-panel2 hover:text-fg'
                }`}
              >
                <Icon className="h-[18px] w-[18px]" />
                {label}
              </button>
              {/* Sub-items under "Trang": the current pages, for one-click navigation. */}
              {key === 'pages' && pages.length > 0 && (
                <div className="ml-4 mt-0.5 flex flex-col gap-0.5 border-l border-line pl-2">
                  {pages.map((p) => {
                    const active = view === 'page-detail' && selectedPageId === p.id
                    return (
                      <button
                        key={p.id}
                        onClick={() => openPage(p.id)}
                        title={p.name}
                        className={`truncate rounded-lg px-3 py-1.5 text-left text-[13px] transition ${
                          active ? 'bg-brand/10 font-medium text-brand' : 'text-muted hover:bg-panel2 hover:text-fg'
                        }`}
                      >
                        {p.name}
                      </button>
                    )
                  })}
                </div>
              )}
            </div>
          ))}
        </nav>
        <div
          className={`mt-auto rounded-xl border p-3 text-xs ${
            source === 'live'
              ? 'border-line bg-panel2 text-muted'
              : 'border-amber-500/40 bg-amber-500/10 text-amber-700 ring-1 ring-inset ring-amber-500/20 dark:text-amber-300'
          }`}
        >
          {source === 'live' ? (
            <div className="flex items-center gap-2">
              <span className="h-2 w-2 shrink-0 rounded-full bg-emerald-500" />
              <span>
                <span className="font-medium text-fg">Database connected</span> — local PostgreSQL.
              </span>
            </div>
          ) : (
            <div className="flex items-start gap-2">
              <span className="mt-1 h-2 w-2 shrink-0 rounded-full bg-amber-400" />
              <span>
                <span className="font-semibold text-amber-800 dark:text-amber-200">Mất kết nối</span> — không gọi được
                API. Dữ liệu sẽ hiện lại khi kết nối được khôi phục.
              </span>
            </div>
          )}
        </div>
      </aside>

      {/* Main column */}
      <div className="md:pl-60">
        {/* Top bar */}
        <header className="sticky top-0 z-10 flex items-center justify-between border-b border-line bg-ink/85 px-4 py-3 backdrop-blur md:px-8">
          <div className="md:hidden">
            <Brand compact />
          </div>
          <div className="hidden md:block">
            <h1 className="text-sm font-medium text-muted">
              Content Factory <span className="text-muted/50">/</span>{' '}
              <span className="text-fg">{VIEW_LABEL[view] ?? view}</span>
            </h1>
          </div>
          <div className="flex items-center gap-2">
            <span className="hidden items-center gap-2 rounded-full border border-line bg-panel px-3 py-1 text-xs text-muted sm:inline-flex">
              {loading ? (
                <>
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-400" />
                  Connecting…
                </>
              ) : source === 'live' ? (
                <>
                  <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
                  Live · PostgreSQL
                </>
              ) : (
                <>
                  <span className="h-1.5 w-1.5 rounded-full bg-amber-400" />
                  Mất kết nối
                </>
              )}
            </span>
            <button
              onClick={toggleTheme}
              aria-label={theme === 'dark' ? 'Chuyển sang nền sáng' : 'Chuyển sang nền tối'}
              className="grid h-8 w-8 place-items-center rounded-lg border border-line bg-panel text-muted transition hover:text-fg active:scale-95"
            >
              {theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            </button>
          </div>
        </header>

        {/* View body */}
        <main className="w-full px-4 pb-28 pt-6 md:px-8 md:pb-12">
          <div key={view} className="animate-rise">
            {view === 'overview' && <Overview onOpenPage={openPage} />}
            {view === 'pages' && <Pages onOpenPage={openPage} />}
            {view === 'page-detail' && selectedPageId != null && (
              <PageDetail pageId={selectedPageId} onBack={() => setView('pages')} />
            )}
            {view === 'create-video' && <CreateVideo />}
            {view === 'jobs' && <Jobs />}
            {view === 'videos' && <Videos />}
            {view === 'publishing' && <Publishing />}
          </div>
        </main>
      </div>

      {/* Bottom nav — mobile only */}
      <nav className="fixed inset-x-0 bottom-0 z-20 flex border-t border-line bg-panel/95 backdrop-blur md:hidden">
        {NAV.map(({ key, label, Icon }) => (
          <button
            key={key}
            onClick={() => setView(key)}
            className={`flex flex-1 flex-col items-center gap-1 py-2.5 text-[10px] font-medium transition ${
              navActive === key ? 'text-brand' : 'text-muted'
            }`}
          >
            <Icon className="h-5 w-5" />
            {label}
          </button>
        ))}
      </nav>
    </div>
    </PublishProvider>
    </ToastProvider>
  )
}

function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <div className="flex items-center gap-2.5">
      <div className="grid h-9 w-9 place-items-center rounded-xl bg-gradient-to-br from-brand to-brand2 text-white shadow-card">
        <Factory className="h-5 w-5" />
      </div>
      {!compact && (
        <div className="leading-tight">
          <div className="text-sm font-semibold">Content Factory</div>
          <div className="text-[11px] text-muted">Dashboard</div>
        </div>
      )}
      {compact && <div className="text-sm font-semibold">Content Factory</div>}
    </div>
  )
}
