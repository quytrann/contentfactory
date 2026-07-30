import { ArrowDownRight, ArrowUpRight, Clapperboard, DollarSign, Loader2, Send } from 'lucide-react'
import type { ReactNode } from 'react'
import Pipeline from '../components/Pipeline'
import { AreaChart, BarChart, Donut, Sparkline } from '../components/Charts'
import { useData } from '../data'
import type { VideoStatus } from '../types'
import { Card, PLATFORM_META, SectionTitle, StatusBadge, fmtDate } from '../ui'

const STATUS_COLOR: Record<VideoStatus, string> = {
  published: '#10b981',
  ready: '#3b82f6',
  rendering: '#f59e0b',
  needs_input: '#f59e0b',
  failed: '#f43f5e',
}

const fmtCompact = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n))

export default function Overview({ onOpenPage }: { onOpenPage: (id: number) => void }) {
  const { pages: PAGES, videos: VIDEOS, jobs: JOBS, analytics: ANALYTICS } = useData()
  const activePages = PAGES.filter((p) => p.status === 'active').length
  const published = VIDEOS.filter((v) => v.status === 'published').length
  const pendingJobs = JOBS.filter((j) => j.status === 'queued' || j.status === 'running').length
  const totalViews = ANALYTICS.platformSplit.reduce((s, p) => s + p.views, 0)
  const statusSlices = (['published', 'ready', 'rendering', 'failed'] as VideoStatus[])
    .map((st) => ({ label: st, value: VIDEOS.filter((v) => v.status === st).length, color: STATUS_COLOR[st] }))
    .filter((s) => s.value > 0)
  // "Video gần đây": real produced videos, newest-first, top 5. Sort defensively
  // by createdAt desc in case the API doesn't already order them.
  const recentVideos = [...VIDEOS]
    .sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime())
    .slice(0, 5)

  return (
    <div className="space-y-8">
      {/* Hero */}
      <div className="relative overflow-hidden rounded-2xl border border-line bg-gradient-to-br from-panel to-panel2 p-6 shadow-card md:p-8">
        {/* Luminous blue wash echoing the reference's glowing orb */}
        <div
          aria-hidden
          className="pointer-events-none absolute -right-16 -top-24 h-72 w-72 rounded-full bg-brand/25 blur-3xl"
        />
        <div
          aria-hidden
          className="pointer-events-none absolute -bottom-28 right-24 h-56 w-56 rounded-full bg-brand2/15 blur-3xl"
        />
        <div className="relative">
          <p className="inline-flex items-center gap-2 text-sm font-medium text-brand">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-brand" />
            Feature review
          </p>
          <h1 className="mt-2 text-2xl font-semibold tracking-tight md:text-3xl">
            Your automated short-video factory
          </h1>
          <p className="mt-2 max-w-2xl text-sm text-muted">
            Send a prompt or a reference link from chat — the system writes a script, generates the
            voiceover and visuals, assembles the video, and publishes it. Review every feature here
            before starting a channel.
          </p>
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Trang hoạt động" value={activePages} hint={`${PAGES.length} tổng cộng`} Icon={Clapperboard} />
        <Stat label="Video đã tạo" value={VIDEOS.length} hint="trên tất cả các trang" Icon={Send} />
        <Stat label="Đã đăng" value={published} hint="đang chạy trên nền tảng" Icon={Send} />
        <Stat label="Chi phí biến đổi" value="$0" hint="tạo 100% nội bộ" Icon={DollarSign} />
      </div>

      {/* Performance analytics */}
      <div>
        <SectionTitle sub="Hiệu suất kênh trên tất cả các trang (14 ngày gần nhất).">Hiệu suất</SectionTitle>

        {/* KPI cards with sparklines */}
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {ANALYTICS.kpis.map((k) => {
            const up = k.delta >= 0
            return (
              <Card key={k.key} className="p-4">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-muted">{k.label}</span>
                  <span
                    className={`inline-flex items-center gap-0.5 text-[11px] font-medium ${
                      up ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'
                    }`}
                  >
                    {up ? <ArrowUpRight className="h-3 w-3" /> : <ArrowDownRight className="h-3 w-3" />}
                    {Math.abs(k.delta)}%
                  </span>
                </div>
                <div className="mt-1 text-2xl font-semibold tracking-tight">{k.value}</div>
                <div className="mt-2">
                  <Sparkline data={k.spark} className={up ? 'text-brand' : 'text-rose-500/70'} />
                </div>
              </Card>
            )
          })}
        </div>

        {/* Charts */}
        <div className="mt-3 grid gap-3 lg:grid-cols-3">
          <Card className="p-5 lg:col-span-2">
            <div className="mb-3 flex items-baseline justify-between">
              <h3 className="text-sm font-semibold">Lượt xem</h3>
              <span className="text-xs text-muted">14 ngày gần nhất</span>
            </div>
            <AreaChart data={ANALYTICS.viewsDaily} labels={ANALYTICS.dayLabels} className="text-brand" />
          </Card>

          <Card className="p-5">
            <div className="mb-3 flex items-baseline justify-between">
              <h3 className="text-sm font-semibold">Video đã tạo</h3>
              <span className="text-xs text-muted">6 tháng</span>
            </div>
            <BarChart data={ANALYTICS.videosMonthly} className="text-brand" format={(v) => String(v)} />
          </Card>
        </div>

        {/* Donuts — platform split + production status */}
        <div className="mt-3 grid gap-3 lg:grid-cols-2">
          <Card className="p-5">
            <h3 className="mb-4 text-sm font-semibold">Lượt xem theo nền tảng</h3>
            <Donut
              centerValue={fmtCompact(totalViews)}
              centerLabel="lượt xem"
              slices={ANALYTICS.platformSplit.map((p) => ({
                label: PLATFORM_META[p.platform].label,
                value: p.views,
                color: PLATFORM_META[p.platform].hex,
              }))}
            />
          </Card>

          <Card className="p-5">
            <h3 className="mb-4 text-sm font-semibold">Trạng thái sản xuất</h3>
            <Donut
              centerValue={String(VIDEOS.length)}
              centerLabel="video"
              slices={statusSlices}
            />
          </Card>
        </div>
      </div>

      {/* Pipeline */}
      <Card className="p-5">
        <SectionTitle sub="Mỗi video đi qua các bước này. Bước ghép FFmpeg là dịch vụ duy nhất viết tay.">
          Pipeline sản xuất
        </SectionTitle>
        <Pipeline />
        {pendingJobs > 0 ? (
          <div className="mt-3 flex items-center gap-2 text-xs text-amber-300/90">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            {pendingJobs} job đang trong hàng đợi
          </div>
        ) : (
          <div className="mt-3 flex items-center gap-2 text-xs text-muted">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500/70" />
            Hàng đợi trống — không có job nào chạy
          </div>
        )}
      </Card>

      {/* Recent activity — bound to the `videos` source (real produced videos),
          NOT the jobs queue. This intentionally differs in count from the queue
          (Hàng đợi): a `videos` row only exists once a job reaches assembly, so
          this list is shorter and shrinks when a video is deleted (refreshed via
          the shared refresh path). Newest-first by createdAt. Each row links back
          to its producing job (video.jobId) for traceability. */}
      <Card className="p-5">
        <SectionTitle sub="Các video đã được sản xuất gần đây">Video gần đây</SectionTitle>
        {recentVideos.length === 0 ? (
          <div className="flex flex-col items-center gap-2 py-8 text-center">
            <span className="text-2xl">🎬</span>
            <p className="text-sm text-muted">Chưa có video nào</p>
          </div>
        ) : (
          <ul className="divide-y divide-line">
            {recentVideos.map((v) => {
              const page = PAGES.find((p) => p.id === v.pageId)
              const title = v.title?.trim() || page?.name || 'Chưa đặt tên'
              return (
                <li key={v.id} className="flex items-center gap-3 py-3">
                  <div className="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-panel2 text-xs text-muted">
                    {v.scenes > 0 ? `${v.scenes}🎬` : '🎬'}
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium">{title}</p>
                    <div className="flex items-center gap-2 text-xs text-muted">
                      <button
                        onClick={() => page && onOpenPage(page.id)}
                        className="hover:text-brand"
                      >
                        {page?.name} · {fmtDate(v.createdAt)}
                      </button>
                      {v.jobId != null && (
                        <span className="rounded bg-panel2 px-1.5 py-0.5 font-mono text-[10px] text-muted">
                          job #{JOBS.find((j) => j.id === v.jobId)?.pageSeq ?? v.jobId}
                        </span>
                      )}
                    </div>
                  </div>
                  <StatusBadge status={v.status} />
                </li>
              )
            })}
          </ul>
        )}
      </Card>
    </div>
  )
}

function Stat({ label, value, hint, Icon }: { label: string; value: ReactNode; hint: string; Icon: typeof Send }) {
  return (
    <Card className="p-4">
      <div className="flex items-center justify-between">
        <span className="text-xs text-muted">{label}</span>
        <Icon className="h-4 w-4 text-brand" />
      </div>
      <div className="mt-2 text-2xl font-semibold tracking-tight">{value}</div>
      <div className="mt-0.5 text-[11px] text-muted">{hint}</div>
    </Card>
  )
}
