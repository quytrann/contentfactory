import { createContext, useContext, useRef, useState } from 'react'
import type { ComponentType, ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { CheckCircle2, Facebook, Instagram, Music2, Search, Twitter, X, XCircle, Youtube } from 'lucide-react'
import type { Platform } from './types'

// Custom brand icons not in lucide-react
function ThreadsIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className={className} aria-hidden="true">
      <path d="M12.186 24h-.007c-3.581-.024-6.334-1.205-8.184-3.509C2.35 18.44 1.5 15.587 1.473 12.01v-.017c.027-3.579.877-6.43 2.523-8.482C5.845 1.205 8.6.024 12.18 0h.014c2.746.018 5.143.771 6.928 2.01 1.698 1.178 2.886 2.82 3.253 4.81l-2.498.43c-.27-1.52-1.054-2.73-2.268-3.553-1.237-.84-2.886-1.284-4.77-1.297h-.01c-2.734.018-4.787.944-6.105 2.752-1.197 1.645-1.82 4.05-1.842 7.131v.012c.022 3.078.645 5.48 1.842 7.126 1.318 1.81 3.371 2.734 6.105 2.752h.01c2.516-.017 4.234-.714 5.616-2.17 1.502-1.58 1.928-3.912 1.213-6.97-.45-1.91-1.375-3.312-2.721-4.163-.42 3.037-1.654 5.213-3.672 6.38-1.548.888-3.378 1.03-5.3.41l-.46-.143c-1.748-.56-3.41-2.168-3.568-4.652-.048-.734-.021-1.471.082-2.188.298-2.087 1.493-3.668 3.368-4.457.848-.354 1.77-.53 2.73-.53.48 0 .96.042 1.435.127 1.69.31 3.065 1.086 3.987 2.232l.017.022-2.082 1.396-.01-.014c-.592-.76-1.485-1.204-2.56-1.397a5.34 5.34 0 00-.92-.083c-.61 0-1.197.115-1.745.34-1.13.47-1.83 1.43-1.98 2.556-.072.52-.091 1.042-.056 1.55.105 1.507.982 2.487 2.308 2.908l.312.098c1.27.398 2.5.313 3.564-.31 1.447-.829 2.412-2.61 2.681-5.01a8.13 8.13 0 00-1.89-.617 8.426 8.426 0 00-1.567-.134c-.674 0-1.358.088-2.031.263l-.652-2.418c.88-.237 1.79-.357 2.683-.357.724 0 1.448.078 2.158.23a10.6 10.6 0 012.43.791c1.82.904 3.1 2.508 3.584 4.603.859 3.685.284 6.56-1.662 8.626-1.66 1.766-4 2.67-6.95 2.685z" />
    </svg>
  )
}

function XBrandIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className={className} aria-hidden="true">
      <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-4.714-6.231-5.401 6.231H2.742l7.776-8.926L2.11 2.25h6.326l4.27 5.64 5.538-5.64zm-1.161 17.52h1.833L7.084 4.126H5.117L17.083 19.77z" />
    </svg>
  )
}

// ---- Filter controls ---------------------------------------------------

// A toolbar row that sits under a section title: search on the left,
// filter chips trailing. Wraps on small screens.
export function FilterBar({ children }: { children: ReactNode }) {
  return <div className="mb-4 flex flex-wrap items-center gap-2">{children}</div>
}

export function SearchInput({
  value,
  onChange,
  placeholder = 'Tìm…',
}: {
  value: string
  onChange: (v: string) => void
  placeholder?: string
}) {
  return (
    <div className="relative w-full sm:w-72">
      <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
      <input
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="h-9 w-full rounded-lg border border-line bg-panel pl-8 pr-3 text-sm text-fg outline-none transition placeholder:text-muted/70 focus:border-brand/50 focus:ring-2 focus:ring-brand/20"
      />
    </div>
  )
}

export interface ChipOption {
  value: string
  label: string
  count?: number
}

// Single-select segmented control — mirrors the reference's pill toggles.
export function ChipGroup({
  options,
  value,
  onChange,
}: {
  options: ChipOption[]
  value: string
  onChange: (v: string) => void
}) {
  return (
    <div className="inline-flex items-center gap-1 rounded-lg border border-line bg-panel p-1">
      {options.map((opt) => (
        <button
          key={opt.value}
          onClick={() => onChange(opt.value)}
          className={`rounded-md px-2.5 py-1 text-xs font-medium capitalize transition active:scale-95 ${
            value === opt.value
              ? 'bg-brand/15 text-brand ring-1 ring-inset ring-brand/25'
              : 'text-muted hover:bg-panel2 hover:text-fg'
          }`}
        >
          {opt.label}
          {opt.count != null && <span className="ml-1 tabular-nums opacity-60">{opt.count}</span>}
        </button>
      ))}
    </div>
  )
}

// ---- Shared presentational helpers -------------------------------------

export function Card({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-2xl border border-line bg-panel shadow-card ${className}`}>
      {children}
    </div>
  )
}

export function EmptyState({ Icon, title, hint }: { Icon: typeof Youtube; title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-line bg-panel/50 px-6 py-14 text-center">
      <div className="grid h-11 w-11 place-items-center rounded-xl bg-panel2 text-brand">
        <Icon className="h-5 w-5" />
      </div>
      <p className="mt-3 text-sm font-medium">{title}</p>
      {hint && <p className="mt-1 max-w-xs text-xs text-muted">{hint}</p>}
    </div>
  )
}

export function SectionTitle({ children, sub }: { children: ReactNode; sub?: string }) {
  return (
    <div className="mb-4">
      <h2 className="text-lg font-semibold tracking-tight">{children}</h2>
      {sub && <p className="mt-0.5 text-sm text-muted">{sub}</p>}
    </div>
  )
}

const TONES: Record<string, string> = {
  green: 'bg-emerald-600/10 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-400/12 dark:text-emerald-300 dark:ring-emerald-400/25',
  amber: 'bg-amber-600/10 text-amber-700 ring-amber-600/20 dark:bg-amber-400/12 dark:text-amber-300 dark:ring-amber-400/25',
  rose: 'bg-rose-600/10 text-rose-700 ring-rose-600/20 dark:bg-rose-400/12 dark:text-rose-300 dark:ring-rose-400/25',
  sky: 'bg-sky-600/10 text-sky-700 ring-sky-600/20 dark:bg-sky-400/12 dark:text-sky-300 dark:ring-sky-400/25',
  slate: 'bg-[#6b7280]/12 text-[#525a6b] ring-[#6b7280]/20 dark:bg-white/8 dark:text-[#aeb6c2] dark:ring-white/15',
  brand: 'bg-brand/10 text-brand ring-brand/25',
}

export function Pill({ tone = 'slate', children }: { tone?: keyof typeof TONES | string; children: ReactNode }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset ${TONES[tone] ?? TONES.slate}`}>
      {children}
    </span>
  )
}

// Map any domain status string to a colored pill.
const STATUS_TONE: Record<string, string> = {
  done: 'green', ready: 'green', published: 'green', posted: 'green', approved: 'green', active: 'green', connected: 'green',
  running: 'amber', queued: 'amber', rendering: 'amber', pending: 'amber', paused: 'amber', needs_input: 'amber',
  failed: 'rose', blocked: 'rose', terminated: 'rose',
  // 'stopped' = user-stopped job: a NEUTRAL terminal state, never red. Gray.
  not_started: 'slate', archived: 'slate', draft: 'slate', stopped: 'slate',
}

// Domain status → Vietnamese label for display.
const STATUS_LABEL: Record<string, string> = {
  queued: 'chờ xử lý', running: 'đang chạy', done: 'xong', failed: 'lỗi',
  needs_input: 'Chờ nhập nguồn', stopped: 'Đã dừng',
  rendering: 'đang dựng', ready: 'sẵn sàng', published: 'đã đăng',
  pending: 'chờ duyệt', draft: 'bản nháp', posted: 'đã đăng', approved: 'đã duyệt',
  not_started: 'Chưa liên kết', active: 'đã liên kết', paused: 'tạm dừng',
  archived: 'lưu trữ', blocked: 'bị chặn', terminated: 'bị khoá',
  connected: 'đã liên kết',
}

export function StatusBadge({ status }: { status: string }) {
  const tone = STATUS_TONE[status] ?? 'slate'
  return <Pill tone={tone}>{STATUS_LABEL[status] ?? status.replace(/_/g, ' ')}</Pill>
}

// ---- Form controls -----------------------------------------------------

export function Button({
  children,
  onClick,
  variant = 'primary',
  type = 'button',
  disabled = false,
  className = '',
}: {
  children: ReactNode
  onClick?: () => void
  variant?: 'primary' | 'outline' | 'ghost' | 'danger'
  type?: 'button' | 'submit'
  disabled?: boolean
  className?: string
}) {
  const v =
    variant === 'primary'
      ? 'bg-brand text-white hover:bg-brand2'
      : variant === 'ghost'
        ? 'text-muted hover:bg-panel2 hover:text-fg'
        : variant === 'danger'
          ? 'bg-rose-600 text-white hover:bg-rose-500'
          : 'border border-line bg-panel text-fg hover:border-brand/40'
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`inline-flex items-center justify-center gap-2 rounded-lg px-3.5 py-2 text-sm font-medium transition active:scale-[.98] disabled:pointer-events-none disabled:opacity-50 ${v} ${className}`}
    >
      {children}
    </button>
  )
}

const FIELD_CLS =
  'h-9 w-full rounded-lg border border-line bg-panel px-3 text-sm text-fg outline-none transition placeholder:text-muted/70 focus:border-brand/50 focus:ring-2 focus:ring-brand/20'

export function TextInput({
  value,
  onChange,
  placeholder,
  type = 'text',
  className = '',
  disabled = false,
}: {
  value: string
  onChange: (v: string) => void
  placeholder?: string
  type?: string
  className?: string
  disabled?: boolean
}) {
  return (
    <input
      type={type}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      disabled={disabled}
      className={`${FIELD_CLS} ${className} disabled:cursor-not-allowed disabled:opacity-50`}
    />
  )
}

export function Select({
  value,
  onChange,
  children,
  className = '',
}: {
  value: string
  onChange: (v: string) => void
  children: ReactNode
  className?: string
}) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} className={`${FIELD_CLS} ${className}`}>
      {children}
    </select>
  )
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-xs font-medium text-muted">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-[11px] text-muted/80">{hint}</span>}
    </label>
  )
}

export function Modal({
  open,
  onClose,
  title,
  children,
  maxWidthClass = 'max-w-md',
  variant = 'default',
}: {
  open: boolean
  onClose: () => void
  title: string
  children: ReactNode
  maxWidthClass?: string
  // 'media' trims the outer/inner padding and header margin to a minimum so the
  // content (e.g. a portrait video) can fill nearly the whole viewport. For a
  // portrait video the binding constraint is viewport HEIGHT, so every pixel of
  // chrome above/below the video matters — this reclaims it.
  variant?: 'default' | 'media'
}) {
  if (!open) return null
  const media = variant === 'media'
  // Portal to <body> so the overlay is fixed to the viewport, not trapped inside
  // an ancestor that establishes a containing block (e.g. the page's animate-rise
  // transform) — which would otherwise center the modal mid-page, needing a scroll.
  return createPortal(
    <div className={`fixed inset-0 z-50 flex items-center justify-center ${media ? 'p-2' : 'p-4'}`}>
      <div className="absolute inset-0 cursor-pointer bg-black/50 backdrop-blur-sm" onClick={onClose} aria-hidden />
      <div className={`relative z-10 animate-rise rounded-2xl border border-line bg-panel shadow-card ${media ? 'w-auto p-2' : `w-full ${maxWidthClass} p-5`}`}>
        <div className={`flex items-center justify-between gap-3 ${media ? 'mb-1.5' : 'mb-4'}`}>
          <h3 className="text-base font-semibold">{title}</h3>
          <button
            onClick={onClose}
            aria-label="Đóng"
            className="grid h-7 w-7 place-items-center rounded-lg text-muted transition hover:bg-panel2 hover:text-fg"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        {children}
      </div>
    </div>,
    document.body,
  )
}

// ---- Platform & architecture metadata ----------------------------------

export const PLATFORM_META: Record<
  Platform,
  { label: string; Icon: ComponentType<{ className?: string }>; color: string; soft: string; hex: string }
> = {
  youtube:  { label: 'YouTube',  Icon: Youtube,      color: 'text-red-600 dark:text-red-400',   soft: 'bg-red-500/10',    hex: '#ef4444' },
  tiktok:   { label: 'TikTok',   Icon: Music2,        color: 'text-teal-600 dark:text-teal-300', soft: 'bg-teal-500/10',   hex: '#14b8a6' },
  instagram:{ label: 'Instagram',Icon: Instagram,     color: 'text-pink-600 dark:text-pink-400', soft: 'bg-pink-500/10',   hex: '#ec4899' },
  facebook: { label: 'Facebook', Icon: Facebook,      color: 'text-blue-600 dark:text-blue-400', soft: 'bg-blue-500/10',   hex: '#3b82f6' },
  x:        { label: 'X',        Icon: XBrandIcon,    color: 'text-fg',                          soft: 'bg-fg/10',         hex: '#000000' },
  threads:  { label: 'Threads',  Icon: ThreadsIcon,   color: 'text-purple-600 dark:text-purple-400', soft: 'bg-purple-500/10', hex: '#7c3aed' },
}

// ---- Formatting --------------------------------------------------------

export function fmtDate(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleString('vi-VN', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

export function fmtDuration(s: number): string {
  const m = Math.floor(s / 60)
  const sec = s % 60
  return `${m}:${sec.toString().padStart(2, '0')}`
}

// mm:ss or h:mm:ss for a duration in seconds (source clips / workflow runs can be
// hours long). Shared by the Workflow elapsed timer (PageDetail) and the queue
// run-time column (Jobs) so both render the exact same basis. Empty/negative → "—".
export function fmtClock(total: number): string {
  if (!total || total < 0) return '—'
  const s = Math.round(total)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  const mm = h ? String(m).padStart(2, '0') : String(m)
  return `${h ? `${h}:` : ''}${mm}:${String(sec).padStart(2, '0')}`
}

// ---- Toast notification system -----------------------------------------

export type ToastEntry = { id: number; type: 'success' | 'error'; message: string }

// Global toast singleton. <ToastProvider> (mounted once at the app root) owns the
// toast array and renders the single <Toaster> portal; components call useToast()
// to get { success, error } and fire into the shared context — no local state.

// Internal context: a single `add(type, msg)` fn. Default is a no-op so a stray
// useToast() outside the provider degrades gracefully (no crash).
const ToastContext = createContext<(type: ToastEntry['type'], msg: string) => void>(() => {})

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastEntry[]>([])
  const nextId = useRef(0)
  const add = (type: ToastEntry['type'], message: string) => {
    const id = ++nextId.current
    setToasts(prev => [...prev, { id, type, message }])
    setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), 3500)
  }
  const dismiss = (id: number) => setToasts(prev => prev.filter(t => t.id !== id))
  return (
    <ToastContext.Provider value={add}>
      {children}
      <Toaster toasts={toasts} dismiss={dismiss} />
    </ToastContext.Provider>
  )
}

// Components call this — no state needed; it fires into the provider's context.
export function useToast(): { success: (msg: string) => void; error: (msg: string) => void } {
  const add = useContext(ToastContext)
  return {
    success: (msg: string) => add('success', msg),
    error: (msg: string) => add('error', msg),
  }
}

// Internal: rendered once inside ToastProvider. Not exported.
function Toaster({ toasts, dismiss }: { toasts: ToastEntry[]; dismiss: (id: number) => void }) {
  if (!toasts.length) return null
  return createPortal(
    <div className="pointer-events-none fixed right-4 top-4 z-[200] flex flex-col gap-2">
      {toasts.map(t => (
        <div
          key={t.id}
          className={`pointer-events-auto flex animate-rise items-center gap-2.5 rounded-xl border px-4 py-2.5 text-sm shadow-card ${
            t.type === 'success'
              ? 'border-emerald-500/40 bg-emerald-800/90 text-white'
              : 'border-rose-500/30 bg-rose-950/95 text-white'
          }`}
        >
          {t.type === 'success'
            ? <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-300" />
            : <XCircle className="h-4 w-4 shrink-0 text-rose-400" />}
          <span className="flex-1">{t.message}</span>
          <button
            type="button"
            onClick={() => dismiss(t.id)}
            className="ml-1 shrink-0 opacity-50 transition hover:opacity-100"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      ))}
    </div>,
    document.body,
  )
}
