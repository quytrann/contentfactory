import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { Facebook, Instagram, Music2, Youtube } from 'lucide-react'
import { useData } from '../data'
import { api, ApiError } from '../api'
import type { PlatformSpec } from '../types'
import { Card, Pill, PLATFORM_META, SectionTitle, StatusBadge } from '../ui'

// Compact number: millions → "10tr", thousands stay grouped (1.000), small literal.
const fmtN = (n: number) =>
  n >= 1_000_000 ? `${+(n / 1_000_000).toFixed(1)}tr` : n.toLocaleString('vi-VN')

// A progress meter chip: "label current/target" + a thin fill bar. Turns green
// (and shows a check) once the milestone is reached.
function MeterChip({ label, current, target }: { label: string; current: number; target: number }) {
  const pct = target > 0 ? Math.min(100, (current / target) * 100) : 0
  const done = current >= target
  return (
    <span
      title={`${label}: ${fmtN(current)} / ${fmtN(target)}`}
      className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-1 text-[11px] font-medium ${
        done
          ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
          : 'border-line bg-panel2 text-muted'
      }`}
    >
      <span className="tabular-nums">
        {fmtN(current)}/{fmtN(target)}
      </span>
      <span className="text-muted/80">{label}</span>
      <span className="relative h-1 w-8 shrink-0 overflow-hidden rounded-full bg-line">
        <span
          className={`absolute inset-y-0 left-0 rounded-full ${done ? 'bg-emerald-500' : 'bg-brand'}`}
          style={{ width: `${pct}%` }}
        />
      </span>
    </span>
  )
}

// X and Threads aren't in lucide — small inline brand glyphs.
function XGlyph({ className = '' }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} fill="currentColor" aria-hidden>
      <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24h-6.66l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z" />
    </svg>
  )
}
function ThreadsGlyph({ className = '' }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} fill="currentColor" aria-hidden>
      <path d="M12.186 24h-.007c-3.581-.024-6.334-1.205-8.184-3.509C2.35 18.44 1.5 15.586 1.472 12.01v-.017c.03-3.579.879-6.43 2.525-8.482C5.845 1.205 8.6.024 12.18 0h.014c2.746.02 5.043.725 6.826 2.098 1.677 1.29 2.858 3.13 3.509 5.467l-2.04.569c-1.104-3.96-3.898-5.984-8.304-6.015-2.91.022-5.11.936-6.54 2.717C4.307 6.504 3.616 8.914 3.589 12c.027 3.086.718 5.496 2.057 7.164 1.43 1.781 3.631 2.695 6.54 2.717 2.623-.02 4.358-.631 5.8-2.045 1.647-1.613 1.618-3.593 1.09-4.798-.31-.71-.873-1.3-1.634-1.75-.192 1.352-.622 2.446-1.284 3.272-.886 1.102-2.14 1.704-3.73 1.79-1.202.065-2.361-.218-3.259-.801-1.063-.689-1.685-1.74-1.752-2.964-.065-1.19.408-2.285 1.33-3.082.88-.76 2.119-1.207 3.583-1.291a13.853 13.853 0 0 1 3.02.142c-.126-.742-.375-1.332-.741-1.757-.495-.572-1.27-.866-2.31-.866h-.043c-.785 0-1.84.218-2.513 1.331l-1.726-1.16C7.66 6.952 9.366 6.184 11.46 6.184h.045c3.501 0 5.586 2.143 5.793 5.903.118.05.234.103.348.158 1.612.756 2.79 1.9 3.409 3.31.86 1.961.94 5.157-1.659 7.703C17.523 23.058 15.376 23.997 12.186 24z" />
    </svg>
  )
}

// A monetization milestone rendered as a meter chip. `metric` says where the
// current value comes from: views (from analytics.platformSplit), videos posted
// to this platform, or subscribers/followers (not tracked yet → shows 0).
interface Gate {
  label: string
  target: number
  metric: 'subs' | 'views' | 'videos'
}

interface PlatformInfo {
  key: string
  label: string
  Icon: (p: { className?: string }) => ReactNode
  color: string
  approval: string       // publishing/approval requirement
  monetize: string[]     // eligibility notes to turn on monetization
  gates: Gate[]          // numeric milestones toward enabling monetization
  rateLimit?: string     // platform-specific publishing rate cap (optional)
}

// Eligibility figures are general guidance (Jan 2026) and vary by region / change
// over time — always verify the platform's current terms.
const PLATFORMS: PlatformInfo[] = [
  {
    key: 'youtube',
    label: 'YouTube',
    Icon: (p) => <Youtube {...p} />,
    color: 'text-red-600 dark:text-red-400',
    approval: 'Xác minh OAuth để đăng công khai · hạn mức ~6 video/ngày',
    monetize: [
      'Bật YPP: 1.000 sub + 4.000 giờ xem công khai (12 tháng) HOẶC 1.000 sub + 10 triệu view Shorts (90 ngày).',
      'Tier sớm (Super Thanks…): 500 sub + 3 video/90 ngày + 3.000 giờ HOẶC 3 triệu view Shorts.',
      'Short ≤ 3 phút (dọc) ăn pool quảng cáo Shorts; video dài cần ≥ 8 phút mới gắn được mid-roll.',
      'Up cả Short lẫn video dài đều được — nhưng "giờ xem" chỉ tính từ video dài, không tính Shorts.',
      'BẮT BUỘC nội dung gốc/biến đổi: reup/biên dịch không biến đổi bị từ chối (reused content). Cần 2FA + AdSense, không gậy bản quyền.',
    ],
    gates: [
      { label: 'sub', target: 1000, metric: 'subs' },
      { label: 'view Shorts', target: 10_000_000, metric: 'views' },
    ],
  },
  {
    key: 'tiktok',
    label: 'TikTok',
    Icon: (p) => <Music2 {...p} />,
    color: 'text-teal-600 dark:text-teal-300',
    approval: 'Kiểm duyệt riêng (1–4 tuần) · chỉ SELF_ONLY đến khi được duyệt',
    monetize: [
      'Creator Rewards Program: 10.000 follower + 100.000 view/30 ngày, 18+.',
      'BẮT BUỘC video DÀI HƠN 1 phút mới đủ điều kiện ăn tiền (TikTok ưu tiên video dài, không tính video <1 phút).',
      'Nội dung gốc, chất lượng, theo nguyên tắc cộng đồng; không tái sử dụng/đăng lại thuần.',
    ],
    gates: [
      { label: 'follower', target: 10_000, metric: 'subs' },
      { label: 'view/30n', target: 100_000, metric: 'views' },
    ],
  },
  {
    key: 'instagram',
    label: 'Instagram',
    Icon: (p) => <Instagram {...p} />,
    color: 'text-pink-600 dark:text-pink-400',
    approval: 'Tài khoản IG Business + App Review · 25 bài / 24h',
    monetize: [
      'Kiếm tiền (bonus/ads) chủ yếu theo LỜI MỜI và giới hạn khu vực (VN hạn chế).',
      'Tài khoản Professional, tuân Content Monetization Policies + Partner Monetization Policies.',
      'Gifts/quà ở Reels cần đủ điều kiện; phụ thuộc nặng vào khu vực & lượng follower.',
    ],
    // Chủ yếu theo lời mời + giới hạn khu vực — không có ngưỡng số công khai.
    gates: [{ label: 'follower', target: 10_000, metric: 'subs' }],
  },
  {
    key: 'facebook',
    label: 'Facebook',
    Icon: (p) => <Facebook {...p} />,
    color: 'text-blue-600 dark:text-blue-400',
    approval: 'Facebook Page + App Review · có thể cần xác minh Business',
    rateLimit: 'Giới hạn đăng Reels: 30 Reels / 24 giờ / trang',
    monetize: [
      'In-stream ads: 10.000 follower + 600.000 phút xem/60 ngày + ≥ 5 video đang hoạt động.',
      'Video cần ≥ 1 phút để gắn quảng cáo; mid-roll cần ≥ 3 phút.',
      'Reels qua chương trình bonus (tùy khu vực). Phải đủ điều kiện monetization + ở quốc gia hỗ trợ.',
    ],
    gates: [
      { label: 'follower', target: 10_000, metric: 'subs' },
      { label: 'video', target: 5, metric: 'videos' },
    ],
  },
  {
    key: 'x',
    label: 'X (Twitter)',
    Icon: (p) => <XGlyph {...p} />,
    color: 'text-fg',
    approval: 'API v2 (gói trả phí cho upload video lớn) · đăng qua OAuth 2.0',
    monetize: [
      'Creator Ads Revenue Sharing: phải là X Premium (trả phí), ≥ 500 follower, ≥ 5 triệu impression/3 tháng.',
      'Tiền đến từ quảng cáo hiển thị trong phần trả lời bài đăng của bạn.',
      'Không quy định độ dài video cụ thể — tính theo impression/tương tác.',
    ],
    gates: [
      { label: 'follower', target: 500, metric: 'subs' },
      { label: 'impression', target: 5_000_000, metric: 'views' },
    ],
  },
  {
    key: 'threads',
    label: 'Threads',
    Icon: (p) => <ThreadsGlyph {...p} />,
    color: 'text-fg',
    approval: 'Chưa có API đăng chính thức rộng rãi (Meta đang mở dần)',
    monetize: [
      'CHƯA có chương trình kiếm tiền chính thức cho creator (Meta mới thử nghiệm bonus ở vài khu vực).',
      'Hiện chủ yếu để kéo tương tác / điều hướng về kênh khác, không monetize trực tiếp.',
    ],
    gates: [],
  },
]

// ---- Platform upload specs (reference panel) ---------------------------

// Human-friendly Vietnamese duration from whole seconds. Tiers tuned for how
// short-form caps actually read: under 2 minutes stays in raw seconds (a 60s/90s
// cap reads naturally as "60 giây"/"90 giây"); up to and including 60 minutes →
// phút (+ giây remainder); above 1h → giờ (+ phút remainder). The minute tier is
// inclusive of exactly 3600s so a 1-hour cap reads "60 phút", while larger caps
// roll up to hours.
// Examples: 60 → "60 giây", 90 → "90 giây", 180 → "3 phút", 1200 → "20 phút",
// 3600 → "60 phút", 43200 → "12 giờ", 5400 → "1 giờ 30 phút".
function fmtSpecDuration(s: number): string {
  if (s < 120) return `${Math.round(s)} giây`
  if (s <= 3600) {
    const m = Math.floor(s / 60)
    const sec = Math.round(s % 60)
    return sec === 0 ? `${m} phút` : `${m} phút ${sec} giây`
  }
  const h = Math.floor(s / 3600)
  const min = Math.round((s % 3600) / 60)
  return min === 0 ? `${h} giờ` : `${h} giờ ${min} phút`
}

// Combine min/max duration into one readable range, tolerant of nulls.
function durationRange(min: number | null, max: number | null): string {
  if (min != null && max != null) return `${fmtSpecDuration(min)} – ${fmtSpecDuration(max)}`
  if (max != null) return `tối đa ${fmtSpecDuration(max)}`
  if (min != null) return `tối thiểu ${fmtSpecDuration(min)}`
  return 'Không giới hạn'
}

// Max file size: MB under 1024 stays MB, otherwise GB. Null = no fixed limit.
function fmtFileSize(mb: number | null): string {
  if (mb == null) return 'Không quy định'
  if (mb < 1024) return `${mb} MB`
  return `${+(mb / 1024).toFixed(mb % 1024 === 0 ? 0 : 1)} GB`
}

// A single labeled row inside a spec card. `value` is rendered as-is; empty
// strings fall back to a dash so the layout stays aligned.
function SpecRow({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex gap-2 text-xs leading-relaxed">
      <span className="w-28 shrink-0 text-muted">{label}</span>
      <span className="flex-1 text-fg">{value || '—'}</span>
    </div>
  )
}

// A small uppercase group heading inside a spec card.
function SpecGroupLabel({ children }: { children: ReactNode }) {
  return (
    <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted">{children}</p>
  )
}

// The enforced-side max duration. The validator rejects strictly ABOVE
// hardMaxDurationS, so render it as a "≤" ceiling. Falls back to the
// recommended maxDurationS if the gate field is absent (older API).
function hardMaxLabel(spec: PlatformSpec): string {
  const cap = spec.hardMaxDurationS ?? spec.maxDurationS
  return cap != null ? `≤ ${fmtSpecDuration(cap)}` : 'Không giới hạn'
}

function SpecCard({ spec }: { spec: PlatformSpec }) {
  const meta = PLATFORM_META[spec.platform]
  const Icon = meta?.Icon
  // Prefer the real gate lists; fall back to the recommended ones if absent.
  const gateContainers = spec.gatedContainers ?? spec.containers
  const gateVcodecs = spec.gatedVcodecs ?? spec.vcodecs
  const aspectGate =
    spec.enforceAspect === true ? 'bắt buộc 9:16 (dọc)' : 'mọi tỉ lệ'
  return (
    <Card className="flex flex-col p-5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          {Icon && <Icon className={`h-6 w-6 ${meta.color}`} />}
          <span className="font-semibold">{meta?.label ?? spec.label}</span>
        </div>
        {/* `enforced` now means a validator runs before upload — the card's
            "Hệ thống chặn khi đăng" group spells out exactly what it rejects, so
            this badge no longer implies the recommended numbers are enforced. */}
        {spec.enforced ? (
          <Pill tone="green">Có kiểm trước khi đăng</Pill>
        ) : (
          <span className="text-[11px] text-muted">Theo nền tảng</span>
        )}
      </div>

      {/* Recommended short-form profile — guidance only, not enforced. */}
      <div className="mt-3">
        <SpecGroupLabel>Khuyến nghị (short-form)</SpecGroupLabel>
        <div className="space-y-1.5">
          <SpecRow label="Định dạng" value={spec.containers.join(', ')} />
          <SpecRow label="Tỉ lệ" value={spec.aspectRatio} />
          <SpecRow label="Độ phân giải" value={spec.resolution} />
          <SpecRow label="Thời lượng" value={durationRange(spec.minDurationS, spec.maxDurationS)} />
          <SpecRow label="Dung lượng tối đa" value={fmtFileSize(spec.maxFileMb)} />
          <SpecRow label="Codec hình" value={spec.vcodecs.join(', ')} />
          <SpecRow label="Codec tiếng" value={spec.acodecs.join(', ')} />
        </div>
      </div>

      {/* Hard gates the pre-upload validator actually rejects on (422). */}
      <div className="mt-3 rounded-xl border border-amber-500/30 bg-amber-500/[0.06] p-3 dark:border-amber-400/25 dark:bg-amber-400/[0.07]">
        <SpecGroupLabel>Hệ thống chặn khi đăng</SpecGroupLabel>
        <div className="space-y-1.5">
          <SpecRow label="Thời lượng" value={hardMaxLabel(spec)} />
          <SpecRow label="Tỉ lệ" value={aspectGate} />
          <SpecRow label="Định dạng" value={gateContainers.join(', ')} />
          <SpecRow label="Codec hình" value={gateVcodecs.join(', ')} />
          {spec.requireAudioAac && <SpecRow label="Âm thanh" value="cần audio AAC" />}
        </div>
      </div>

      {/* Short / Mid / Long duration tiers (optional; render only when present). */}
      {spec.tiers && spec.tiers.length > 0 && (
        <div className="mt-3">
          <SpecGroupLabel>Phân tầng video (Short · Mid · Long)</SpecGroupLabel>
          <div className="space-y-1.5">
            {spec.tiers.map((t) => (
              <div key={t.key} className="flex gap-2 text-xs leading-relaxed">
                <span className="w-16 shrink-0 font-medium text-fg">{t.label}</span>
                <span className="flex-1 text-muted">
                  <span className="text-fg">{durationRange(t.minDurationS, t.maxDurationS)}</span>
                  {t.note && <span className="block text-muted">{t.note}</span>}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {spec.notes && (
        <p className="mt-3 border-t border-line pt-3 text-xs leading-relaxed text-muted">{spec.notes}</p>
      )}
    </Card>
  )
}

function PlatformSpecsPanel() {
  const [specs, setSpecs] = useState<PlatformSpec[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    api
      .getPlatformSpecs()
      .then((res) => {
        if (alive) setSpecs(res.specs)
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof ApiError ? e.message : 'Không tải được quy định nền tảng')
      })
    return () => {
      alive = false
    }
  }, [])

  return (
    <div className="space-y-3">
      <SectionTitle sub="Mỗi thẻ tách 2 phần: “Khuyến nghị” là cấu hình short-form gợi ý (không bắt buộc), còn “Hệ thống chặn khi đăng” là giới hạn cứng mà bộ kiểm trước khi upload sẽ từ chối (422). Lưu ý các số khuyến nghị KHÔNG phải thứ bị chặn — ví dụ YouTube không ép 9:16 hay 180 giây.">
        Quy định đăng tải theo nền tảng
      </SectionTitle>

      {error ? (
        <Card className="p-5 text-sm text-rose-600 dark:text-rose-400">
          Không tải được quy định nền tảng: {error}
        </Card>
      ) : specs === null ? (
        <Card className="p-5 text-sm text-muted">Đang tải quy định nền tảng…</Card>
      ) : specs.length === 0 ? (
        <Card className="p-5 text-sm text-muted">Chưa có dữ liệu quy định nền tảng.</Card>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {specs.map((s) => (
            <SpecCard key={s.platform} spec={s} />
          ))}
        </div>
      )}
    </div>
  )
}

export default function Publishing() {
  const { accounts: ACCOUNTS, videos: VIDEOS, analytics: ANALYTICS } = useData()
  const viewsFor = (key: string) =>
    ANALYTICS.platformSplit.find((s) => s.platform === key)?.views ?? 0

  // A platform counts as "đã liên kết" when at least one of its accounts has an
  // OAuth token (the API surfaces that as approval === 'connected'). Linked
  // platforms float to the top; the original PLATFORMS order is kept as a stable
  // secondary sort within each group so cards don't jump around on refresh.
  const isLinked = (key: string) =>
    ACCOUNTS.some((a) => a.platform === key && a.approval === 'connected')
  const orderedPlatforms = PLATFORMS.map((p, i) => ({ p, i }))
    .sort((a, b) => Number(isLinked(b.p.key)) - Number(isLinked(a.p.key)) || a.i - b.i)
    .map((x) => x.p)

  return (
    <div className="space-y-6">
      <SectionTitle sub="API các nền tảng đều miễn phí — chi phí là thời gian duyệt. Số liệu điều kiện kiếm tiền chỉ mang tính tham khảo (đổi theo thời gian & khu vực) — luôn kiểm tra điều khoản hiện hành.">
        Đăng tải
      </SectionTitle>

      <div className="grid gap-3 sm:grid-cols-2">
        {orderedPlatforms.map((p) => {
          const accounts = ACCOUNTS.filter((a) => a.platform === p.key)
          const posts = VIDEOS.filter((v) => (v.postedPlatforms as string[]).includes(p.key)).length
          const meterValue = (g: Gate) =>
            g.metric === 'videos' ? posts : g.metric === 'views' ? viewsFor(p.key) : 0
          return (
            <Card key={p.key} className="flex flex-col p-5">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2.5">
                  <p.Icon className={`h-6 w-6 ${p.color}`} />
                  <span className="font-semibold">{p.label}</span>
                </div>
                <StatusBadge status={accounts[0]?.approval ?? 'not_started'} />
              </div>

              <p className="mt-3 text-xs leading-relaxed text-muted">{p.approval}</p>
              {p.rateLimit && (
                <p className="mt-1.5 text-xs leading-relaxed text-muted">{p.rateLimit}</p>
              )}

              <div className="mt-3 rounded-xl border border-line bg-panel2 p-3">
                <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-brand">Điều kiện bật kiếm tiền</p>
                <ul className="space-y-1.5 text-xs leading-relaxed text-muted">
                  {p.monetize.map((m, i) => (
                    <li key={i} className="flex gap-1.5">
                      <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-brand/60" />
                      <span>{m}</span>
                    </li>
                  ))}
                </ul>
              </div>

              <div className="mt-auto border-t border-line pt-3">
                <span className="text-sm text-muted">
                  {accounts.length} tài khoản · {posts} đã đăng
                </span>
                {/* Progress toward enabling monetization on this channel. */}
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                  {p.gates.length > 0 ? (
                    p.gates.map((g) => (
                      <MeterChip key={g.label} label={g.label} current={meterValue(g)} target={g.target} />
                    ))
                  ) : (
                    <Pill tone="slate">Chưa có chương trình kiếm tiền</Pill>
                  )}
                </div>
              </div>
            </Card>
          )
        })}
      </div>

      <PlatformSpecsPanel />
    </div>
  )
}
