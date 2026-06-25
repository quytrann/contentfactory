import { useEffect, useState } from 'react'
import { ArrowLeft, Film, Loader2 } from 'lucide-react'
import { useData } from '../data'
import { api } from '../api'
import type { PageAnalytics, Video, VideoStatus } from '../types'
import { BarChart, Donut } from '../components/Charts'
import { VideoCard, aspectOf } from './Videos'
import { Card, EmptyState, Modal, SectionTitle, Select, StatusBadge } from '../ui'

// Brand colors per platform for the traffic-split donut; default for any unknown.
const PLATFORM_COLOR: Record<string, string> = {
  youtube: '#ff4444',
  facebook: '#1877f2',
  tiktok: '#69c9d0',
}
const platformColor = (p: string) => PLATFORM_COLOR[p] ?? '#888'

export default function PageDetail({ pageId, onBack }: { pageId: number; onBack: () => void }) {
  const { pages: PAGES, videos: VIDEOS } = useData()
  const page = PAGES.find((p) => p.id === pageId)

  // Per-page traffic analytics (platform split + monthly views). null = loading.
  const [analytics, setAnalytics] = useState<PageAnalytics | null>(null)
  useEffect(() => {
    let alive = true
    setAnalytics(null)
    api.getPageAnalytics(pageId)
      .then((d) => { if (alive) setAnalytics(d) })
      .catch(() => {})
    return () => { alive = false }
  }, [pageId])

  if (!page) return null
  // Posts-driven products: a video belongs to this page's "Sản phẩm" ONLY if it
  // was PUBLISHED into one of this page's channels (a video can be published into
  // channels across many pages — backend computes publishedPageIds). Videos that
  // merely ORIGINATED here but were never published do NOT appear here.
  // publishedPageIds is always an array (never null).
  const videos = VIDEOS.filter((v) => (v.publishedPageIds ?? []).includes(pageId))

  return (
    <div className="space-y-6">
      <button onClick={onBack} className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-fg">
        <ArrowLeft className="h-4 w-4" /> Trang
      </button>

      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-2xl font-semibold tracking-tight">{page.name}</h1>
        <StatusBadge status={page.status} />
      </div>

      {/* Traffic analytics — platform split + monthly views */}
      <PageTraffic analytics={analytics} />

      {/* Products — videos published into this page's channels */}
      <Products pageName={page.name} videos={videos} />
    </div>
  )
}

// ---- Traffic analytics -------------------------------------------------

function PageTraffic({ analytics }: { analytics: PageAnalytics | null }) {
  const totalViews = analytics?.platformSplit.reduce((s, p) => s + p.views, 0) ?? 0
  const slices = (analytics?.platformSplit ?? []).map((p) => ({
    label: p.platform,
    value: p.views,
    color: platformColor(p.platform),
  }))
  const monthly = (analytics?.viewsMonthly ?? []).map((d) => ({ month: d.month.slice(5), value: d.value }))
  const isEmpty = analytics !== null && slices.length === 0 && monthly.length === 0

  return (
    <Card className="p-5">
      <SectionTitle sub="Lưu lượng của kênh này — phân bổ theo nền tảng và lượt xem theo tháng.">
        Phân tích lưu lượng
      </SectionTitle>
      {analytics === null ? (
        <div className="flex h-44 items-center justify-center text-muted">
          <Loader2 className="h-5 w-5 animate-spin" />
        </div>
      ) : isEmpty ? (
        <p className="py-8 text-center text-sm text-muted">Chưa có dữ liệu lưu lượng.</p>
      ) : (
        <div className="grid gap-6 lg:grid-cols-2">
          <div>
            <span className="mb-3 block text-xs font-medium text-muted">Nền tảng</span>
            <Donut
              slices={slices}
              centerValue={totalViews.toLocaleString()}
              centerLabel="lượt xem"
            />
          </div>
          <div>
            <span className="mb-3 block text-xs font-medium text-muted">Lượt xem theo tháng</span>
            <BarChart data={monthly} format={(v) => v.toLocaleString()} />
          </div>
        </div>
      )}
    </Card>
  )
}

// ---- Products ----------------------------------------------------------

// Finished products = preview grid (like the Videos menu), PUBLISHED-only for this channel.
function Products({ pageName, videos }: { pageName: string; videos: Video[] }) {
  const [status, setStatus] = useState<'all' | VideoStatus>('all')
  const [playing, setPlaying] = useState<Video | null>(null)
  // Match Videos.tsx landscapeOf: prefer the real frame size; when dims are
  // unknown (e.g. failed videos) default to portrait — shorts are portrait by
  // default, and render_mode is now a per-job property no longer on the page.
  const land = (v: Video) => (v.width && v.height ? v.width > v.height : false)
  const count = (s: VideoStatus) => videos.filter((v) => v.status === s).length
  const filtered = status === 'all' ? videos : videos.filter((v) => v.status === status)
  return (
    <Card className="p-5">
      <SectionTitle sub="Video của kênh này. Lọc theo trạng thái; bấm để xem ngay trên trình duyệt.">
        Sản phẩm ({filtered.length})
      </SectionTitle>
      <div className="mb-3 flex items-center gap-2">
        <span className="shrink-0 text-xs text-muted">Lọc trạng thái:</span>
        <Select value={status} onChange={(v) => setStatus(v as 'all' | VideoStatus)} className="w-52">
          <option value="all">Tất cả ({videos.length})</option>
          <option value="ready">Sẵn sàng ({count('ready')})</option>
          <option value="published">Đã đăng ({count('published')})</option>
          <option value="rendering">Đang dựng ({count('rendering')})</option>
          <option value="failed">Lỗi ({count('failed')})</option>
        </Select>
      </div>
      {filtered.length === 0 ? (
        <EmptyState Icon={Film} title="Chưa có video phù hợp" hint="Tạo video ở mục Tạo Video rồi đăng lên kênh này, hoặc đổi bộ lọc trạng thái." />
      ) : (
        <div className="grid grid-cols-3 gap-2.5 sm:grid-cols-4 lg:grid-cols-7 2xl:grid-cols-8">
          {filtered.map((v) => (
            <VideoCard key={v.id} v={v} landscape={land(v)} pageName={pageName} onPlay={setPlaying} />
          ))}
        </div>
      )}
      {playing && playing.videoUrl && (
        <Modal open onClose={() => setPlaying(null)} title={playing.title} maxWidthClass="max-w-none" variant="media">
          {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
          {/* Cap to ~70% of the viewport; w-auto/h-auto preserves the real aspect ratio. */}
          <video
            src={playing.videoUrl}
            controls
            autoPlay
            className="mx-auto h-auto w-auto max-h-[70vh] max-w-[70vw] rounded-xl border border-line"
            style={{ aspectRatio: aspectOf(playing, land(playing)) }}
          />
        </Modal>
      )}
    </Card>
  )
}
