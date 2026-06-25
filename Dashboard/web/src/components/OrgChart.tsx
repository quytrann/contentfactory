import { AlertTriangle, Mail } from 'lucide-react'
import { useData } from '../data'
import { PLATFORM_META, StatusBadge } from '../ui'
import type { Platform } from '../types'

// Relationship map: Dashboard -> Google EMAIL -> pages -> channels.
//
// One Google email can own MULTIPLE pages across different platforms
// (e.g. contentfactory.gamestory@gmail.com owns both "CTG Gaming" on YouTube
// and "Giải Thích Mọi Thứ" on Facebook). So one block = one email, and inside
// it each page is its own clickable nested card listing that page's channels.
//
// Layout: root node on top, a stem, then a responsive grid of EMAIL blocks.
// Each block: email header (avatar initial + Mail + address) over a stack of
// per-page sub-cards. Each page sub-card opens its own Studio via onOpenPage.
export default function OrgChart({ onOpenPage }: { onOpenPage?: (id: number) => void }) {
  const { org: ORG } = useData()
  const ease = 'ease-[cubic-bezier(0.32,0.72,0,1)]'
  return (
    <div className="min-w-[300px]">
      {/* Root */}
      <div className="flex justify-center">
        <div className="inline-flex items-center gap-3 rounded-2xl border border-brand/25 bg-brand/8 px-5 py-3 shadow-card">
          <span className="grid h-9 w-9 place-items-center rounded-xl bg-gradient-to-br from-brand to-brand2 text-white">
            <Mail className="h-5 w-5" />
          </span>
          <div className="leading-tight">
            <div className="text-sm font-semibold">{ORG.dashboard}</div>
            <div className="text-[11px] text-muted">Dashboard · {ORG.accounts.length} email cô lập</div>
          </div>
        </div>
      </div>

      {/* Stem + accounts share ONE centered column so the 1px connectors
          compute identical sub-pixel positions (no shimmer / no offset). */}
      <div className="mx-auto flex w-fit max-w-full flex-col items-center">
        {/* Stem — centered on the shared column axis so it matches the upward
            connector tick exactly (same bg-line color, same w-px width, same
            sub-pixel position; no offset). */}
        <div className="h-8 w-px bg-line" aria-hidden />

        {/* Accounts (one block per email) */}
        <div className="relative w-full pt-4">
          {/* Horizontal bus linking sibling email blocks (multi-account).
              Cards are a fixed 360px wide on md+, so insetting each end by
              half a card (180px) lands the bus exactly on the outer card
              centers instead of overshooting to the container edges. */}
          {ORG.accounts.length > 1 && (
            <span
              className="pointer-events-none absolute left-[180px] right-[180px] top-0 hidden h-px rounded-full bg-line md:block"
              aria-hidden
            />
          )}
          <div className="flex flex-wrap items-start justify-center gap-4">
            {ORG.accounts.map((acc) => {
              const initial = (acc.gmail[0] ?? '?').toUpperCase()
              const pageCount = acc.pages.length
              // Risk: this email is reused for 2+ channels of the same platform.
              // Guard with ?.length so older API responses (no field) render as before.
              const riskPlatforms = acc.riskPlatforms ?? []
              // Count how many channels of each risky platform share this email,
              // so the warning can state the concrete number of affected channels.
              const channelCountByPlatform = (platform: Platform) =>
                acc.pages.reduce(
                  (n, page) => n + page.channels.filter((ch) => ch.platform === platform).length,
                  0,
                )
              return (
                <div key={acc.gmail} className="relative w-full sm:w-[360px]">
                  {/* connector tick up to the bus / stem */}
                  <span className="absolute -top-4 left-1/2 h-4 w-px -translate-x-1/2 bg-line" aria-hidden />

                  {/* outer shell (double-bezel) — frames the whole email account */}
                  <div className="rounded-[1.4rem] border border-line bg-panel2/50 p-1.5">
                    <div className="rounded-[1.05rem] border border-line bg-panel p-4 shadow-card">
                      {/* EMAIL header — the shared identity that owns everything below */}
                      <div className="flex items-center gap-3">
                        <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-brand/25 to-brand2/10 text-sm font-semibold text-brand ring-1 ring-brand/15">
                          {initial}
                        </span>
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-1.5 text-sm font-semibold">
                            <Mail className="h-3.5 w-3.5 shrink-0 text-muted" />
                            <span className="truncate" title={acc.gmail}>
                              {acc.gmail}
                            </span>
                          </div>
                          <div className="mt-0.5 text-[11px] font-medium uppercase tracking-wide text-muted">
                            {pageCount} trang
                          </div>
                        </div>
                      </div>

                      {/* RISK WARNING — one email reused for 2+ channels of the
                          same platform. If one channel is banned the strike can
                          cascade to its siblings under the same account. */}
                      {riskPlatforms.length > 0 && (
                        <div className="mt-3 flex items-start gap-2 rounded-xl border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-amber-700 dark:text-amber-300">
                          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                          <div className="min-w-0 text-[12px] leading-snug">
                            {riskPlatforms.map((platform) => (
                              <p key={platform}>
                                Rủi ro: 1 email dùng cho {channelCountByPlatform(platform)} kênh{' '}
                                {PLATFORM_META[platform].label} — nếu 1 kênh bị khoá có thể lan sang
                                các kênh còn lại.
                              </p>
                            ))}
                          </div>
                        </div>
                      )}

                      {/* PAGES — each its own clickable nested card opening its Studio */}
                      <div className="mt-4 space-y-2.5">
                        {acc.pages.map((page, i) => (
                          <div
                            key={page.pageId}
                            role={onOpenPage ? 'button' : undefined}
                            tabIndex={onOpenPage ? 0 : undefined}
                            onClick={onOpenPage ? () => onOpenPage(page.pageId) : undefined}
                            onKeyDown={
                              onOpenPage
                                ? (e) => {
                                    if (e.key === 'Enter' || e.key === ' ') {
                                      e.preventDefault()
                                      onOpenPage(page.pageId)
                                    }
                                  }
                                : undefined
                            }
                            className={`rounded-2xl border border-line bg-panel2/60 p-3 transition ${ease} ${
                              onOpenPage
                                ? 'cursor-pointer hover:border-brand/40 hover:bg-panel2 active:scale-[.99]'
                                : ''
                            }`}
                          >
                            {/* page sub-header: brand index marker + page name */}
                            <div className="flex items-center gap-2">
                              <span className="grid h-5 w-5 shrink-0 place-items-center rounded-md bg-brand/12 text-[10px] font-semibold tabular-nums text-brand">
                                {i + 1}
                              </span>
                              <span className="truncate text-sm font-semibold" title={page.pageName}>
                                {page.pageName}
                              </span>
                            </div>

                            {/* this page's channels */}
                            <div className="mt-2.5 space-y-1.5">
                              {page.channels.map((ch) => {
                                const { Icon, color, soft, label } = PLATFORM_META[ch.platform]
                                return (
                                  <div
                                    key={ch.platform + ch.handle}
                                    className="flex items-center gap-2.5 rounded-xl bg-panel px-2.5 py-2 ring-1 ring-line/60"
                                  >
                                    <span className={`grid h-8 w-8 shrink-0 place-items-center rounded-lg ${soft}`}>
                                      <Icon className={`h-[17px] w-[17px] ${color}`} />
                                    </span>
                                    <span className="truncate text-sm font-medium">{label}</span>
                                    {/* status chip pushed to the right edge of the row */}
                                    <span className="ml-auto shrink-0">
                                      <StatusBadge status={ch.status} />
                                    </span>
                                  </div>
                                )
                              })}
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </div>
  )
}
