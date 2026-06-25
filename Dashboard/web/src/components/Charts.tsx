// Dependency-free, theme-aware SVG charts.
// Color comes from `currentColor`, so wrapping in `text-brand` (or any text-*)
// makes a chart adapt to light/dark automatically.

let uid = 0
const nextId = () => `cf-grad-${uid++}`

function buildPoints(data: number[], w: number, h: number, pad: number) {
  const max = Math.max(...data)
  const min = Math.min(...data, 0)
  const span = max - min || 1
  const innerW = w - pad * 2
  const innerH = h - pad * 2
  return data.map((v, i) => {
    const x = pad + (data.length === 1 ? innerW / 2 : (i / (data.length - 1)) * innerW)
    const y = pad + innerH - ((v - min) / span) * innerH
    return [x, y] as const
  })
}

export function Sparkline({ data, className = 'text-brand' }: { data: number[]; className?: string }) {
  const w = 100
  const h = 32
  const pts = buildPoints(data, w, h, 3)
  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ')
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className={`h-8 w-full ${className}`} preserveAspectRatio="none" aria-hidden>
      <path
        d={line}
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  )
}

export function AreaChart({
  data,
  labels,
  className = 'text-brand',
}: {
  data: number[]
  labels?: string[]
  className?: string
}) {
  const w = 360
  const h = 150
  const pad = 14
  const id = nextId()
  if (data.length === 0) {
    return (
      <div className="flex h-44 items-center justify-center text-xs text-muted">
        No data yet — metrics appear once videos are published.
      </div>
    )
  }
  const pts = buildPoints(data, w, h, pad)
  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ')
  const area = `${line} L${pts[pts.length - 1][0].toFixed(1)} ${h - pad} L${pts[0][0].toFixed(1)} ${h - pad} Z`
  return (
    <div className={className}>
      <svg viewBox={`0 0 ${w} ${h}`} className="h-44 w-full" preserveAspectRatio="none" aria-hidden>
        <defs>
          <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="currentColor" stopOpacity={0.28} />
            <stop offset="100%" stopColor="currentColor" stopOpacity={0} />
          </linearGradient>
        </defs>
        {/* baseline grid */}
        {[0.25, 0.5, 0.75].map((f) => (
          <line
            key={f}
            x1={pad}
            x2={w - pad}
            y1={pad + (h - pad * 2) * f}
            y2={pad + (h - pad * 2) * f}
            stroke="currentColor"
            strokeOpacity={0.08}
            strokeWidth={1}
            vectorEffect="non-scaling-stroke"
          />
        ))}
        <path d={area} fill={`url(#${id})`} />
        <path
          d={line}
          fill="none"
          stroke="currentColor"
          strokeWidth={2.25}
          strokeLinecap="round"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      {labels && (
        <div className="mt-1 flex justify-between text-[10px] text-muted">
          {labels.map((l, i) => (
            <span key={i} className={i % 2 ? 'opacity-0 sm:opacity-100' : ''}>
              {l}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

export function BarChart({
  data,
  className = 'text-brand',
  format = (v) => `$${v.toFixed(0)}`,
}: {
  data: { month: string; value: number }[]
  className?: string
  format?: (value: number) => string
}) {
  const max = Math.max(...data.map((d) => d.value)) || 1
  if (data.length === 0) {
    return (
      <div className="flex h-44 items-center justify-center text-xs text-muted">
        No videos produced yet.
      </div>
    )
  }
  return (
    <div className={className}>
      <div className="flex h-44 items-end justify-center gap-3">
        {data.map((d) => (
          <div key={d.month} className="flex h-full max-w-[64px] flex-1 flex-col items-center gap-1.5">
            <span className="text-[10px] font-medium text-muted">{format(d.value)}</span>
            <div className="flex w-full flex-1 items-end justify-center">
              <div
                className="w-full max-w-[52px] rounded-t-md bg-current transition-all"
                style={{ height: `${Math.max(4, (d.value / max) * 100)}%` }}
              />
            </div>
            <span className="text-[10px] text-muted">{d.month}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

export function Donut({
  slices,
  centerValue,
  centerLabel,
}: {
  slices: { label: string; value: number; color: string }[]
  centerValue: string
  centerLabel: string
}) {
  const sum = slices.reduce((s, x) => s + x.value, 0) || 1
  let cumulative = 0
  const r = 15.9155 // circumference ~= 100, so dash values map directly to %
  return (
    <div className="flex flex-col items-center gap-5 sm:flex-row sm:gap-6">
      <div className="relative h-32 w-32 shrink-0">
        <svg viewBox="0 0 36 36" className="h-full w-full">
          <circle cx="18" cy="18" r={r} fill="none" stroke="var(--color-panel2)" strokeWidth="3.6" />
          {slices.map((s, i) => {
            const pct = (s.value / sum) * 100
            const el = (
              <circle
                key={i}
                cx="18"
                cy="18"
                r={r}
                fill="none"
                stroke={s.color}
                strokeWidth="3.6"
                strokeDasharray={`${pct.toFixed(2)} ${(100 - pct).toFixed(2)}`}
                strokeDashoffset={(25 - cumulative).toFixed(2)}
              />
            )
            cumulative += pct
            return el
          })}
        </svg>
        <div className="absolute inset-0 grid place-items-center text-center">
          <div>
            <div className="text-lg font-semibold leading-none tracking-tight">{centerValue}</div>
            <div className="mt-1 text-[10px] uppercase tracking-wider text-muted">{centerLabel}</div>
          </div>
        </div>
      </div>
      <ul className="w-full space-y-2 text-sm">
        {slices.map((s) => (
          <li key={s.label} className="flex items-center gap-2.5">
            <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: s.color }} />
            <span className="text-muted">{s.label}</span>
            <span className="ml-auto font-medium tabular-nums">{s.value.toLocaleString()}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

export function HBars({ items }: { items: { label: string; pct: number; colorClass: string }[] }) {
  return (
    <ul className="space-y-3">
      {items.map((it) => (
        <li key={it.label}>
          <div className="mb-1 flex items-center justify-between text-xs">
            <span className="font-medium">{it.label}</span>
            <span className="text-muted">{it.pct}%</span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-panel2">
            <div className={`h-full rounded-full ${it.colorClass}`} style={{ width: `${it.pct}%` }} />
          </div>
        </li>
      ))}
    </ul>
  )
}
