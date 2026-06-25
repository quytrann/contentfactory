import { useMemo, useState } from 'react'
import { ArrowUpRight, ChevronRight, Mail, Plus } from 'lucide-react'
import OrgChart from '../components/OrgChart'
import { useData, useRefresh } from '../data'
import type { Page } from '../types'
import { Button, Card, ChipGroup, Field, FilterBar, Modal, PLATFORM_META, Pill, SearchInput, SectionTitle, StatusBadge, TextInput, useToast } from '../ui'
import { api } from '../api'

const STATUSES: Page['status'][] = ['active', 'paused', 'archived']

const PLATFORM_ORDER = ['youtube', 'tiktok', 'instagram', 'facebook', 'x', 'threads'] as const

function AddPageModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const refresh = useRefresh()
  const { success, error: toastError } = useToast()
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [selectedPlatform, setSelectedPlatform] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const reset = () => {
    setName(''); setEmail('')
    setSelectedPlatform(null); setError(null)
  }

  const submit = async () => {
    const n = name.trim()
    if (!n) return
    setSaving(true)
    setError(null)
    try {
      await api.createPage({
        name: n,
        accountEmail: email.trim() || undefined,
        platforms: selectedPlatform ? [selectedPlatform] : [],
      })
      await refresh()
      reset()
      onClose()
      success('Đã tạo trang mới')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Lỗi không xác định')
      toastError('Tạo trang thất bại')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal open={open} onClose={() => { if (!saving) { reset(); onClose() } }} title="Thêm trang mới" maxWidthClass="max-w-lg">
      <div className="space-y-4">
        <Field label="Tên trang *">
          <TextInput value={name} onChange={setName} placeholder="VD: GameStory" disabled={saving} />
        </Field>
        <Field label="Email tài khoản" hint="Gmail hoặc handle sở hữu các kênh dưới đây.">
          <TextInput value={email} onChange={setEmail} placeholder="example@gmail.com" type="text" disabled={saving} />
        </Field>
        <Field label="Nền tảng" hint="Chọn kênh sẽ đăng tải. Có thể thêm/bỏ sau ở trang chi tiết.">
          <div className="mt-1 flex gap-2">
            {PLATFORM_ORDER.map((p) => {
              const { Icon, color, label, soft } = PLATFORM_META[p]
              const active = selectedPlatform === p
              return (
                <button
                  key={p}
                  type="button"
                  disabled={saving}
                  onClick={() => setSelectedPlatform((prev) => (prev === p ? null : p))}
                  title={label}
                  className={`flex cursor-pointer flex-col items-center gap-1 rounded-lg border px-3 py-2 text-xs transition disabled:cursor-not-allowed disabled:opacity-50 ${
                    active
                      ? `border-transparent ${soft} font-medium ${color}`
                      : 'border-line bg-panel text-muted hover:border-brand/30'
                  }`}
                >
                  <Icon className={`h-5 w-5 ${active ? color : 'text-muted'}`} />
                  <span>{label}</span>
                </button>
              )
            })}
          </div>
        </Field>
        {error && <p className="text-xs text-red-500">{error}</p>}
        <div className="flex justify-end gap-2 pt-1">
          <Button variant="ghost" onClick={() => { reset(); onClose() }} disabled={saving}>Huỷ</Button>
          <Button onClick={submit} disabled={!name.trim() || saving}>
            {saving ? 'Đang tạo…' : 'Tạo trang'}
          </Button>
        </div>
      </div>
    </Modal>
  )
}

export default function Pages({ onOpenPage }: { onOpenPage: (id: number) => void }) {
  const { pages: PAGES, org } = useData()
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState<'all' | Page['status']>('all')
  const [addOpen, setAddOpen] = useState(false)

  // Per-page org info from the account map: the affiliated account email and its
  // manage-console link. Resolve the primary channel generically so the top
  // Manage link works for any platform: prefer YouTube (→ studio.youtube.com),
  // else Facebook (→ the FB Page dashboard), else the first channel that exposes
  // a manageUrl.
  const orgByPage = useMemo(() => {
    const m: Record<number, { email: string; manageUrl?: string }> = {}
    // An email account now groups pages[]; channels live on each page. Flatten
    // to a per-page lookup, carrying the owning email down to each page.
    for (const acc of org.accounts) {
      for (const page of acc.pages) {
        const ch =
          page.channels.find((c) => c.platform === 'youtube' && c.manageUrl) ??
          page.channels.find((c) => c.platform === 'facebook' && c.manageUrl) ??
          page.channels.find((c) => c.manageUrl)
        m[page.pageId] = { email: acc.gmail, manageUrl: ch?.manageUrl }
      }
    }
    return m
  }, [org])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return PAGES.filter((p) => {
      if (status !== 'all' && p.status !== status) return false
      if (q && !`${p.name} ${p.language}`.toLowerCase().includes(q)) return false
      return true
    })
  }, [query, status])

  const STATUS_VI: Record<Page['status'], string> = { active: 'hoạt động', paused: 'tạm dừng', archived: 'lưu trữ' }
  const statusOptions = [
    { value: 'all', label: 'Tất cả', count: PAGES.length },
    ...STATUSES.map((s) => ({ value: s, label: STATUS_VI[s], count: PAGES.filter((p) => p.status === s).length })),
  ]

  return (
    <div className="space-y-6">
      <AddPageModal open={addOpen} onClose={() => setAddOpen(false)} />
      <div className="flex items-start justify-between gap-3">
        <SectionTitle sub="Mỗi trang là một kênh độc lập với tài khoản riêng. Thêm trang chỉ là thêm một dòng — không đổi schema.">
          Trang
        </SectionTitle>
        <Button onClick={() => setAddOpen(true)} className="shrink-0">
          <Plus className="h-4 w-4" />
          Thêm trang
        </Button>
      </div>

      <FilterBar>
        <SearchInput value={query} onChange={setQuery} placeholder="Tìm trang…" />
        <ChipGroup options={statusOptions} value={status} onChange={(v) => setStatus(v as 'all' | Page['status'])} />
      </FilterBar>

      <div className="grid gap-3 sm:grid-cols-2">
        {filtered.map((page) => (
          <div
            key={page.id}
            role="button"
            tabIndex={0}
            onClick={() => onOpenPage(page.id)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                onOpenPage(page.id)
              }
            }}
            className="group cursor-pointer text-left transition active:scale-[.99]"
          >
            <Card className="p-5 transition hover:border-brand/40 hover:bg-panel2">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h3 className="font-semibold">{page.name}</h3>
                  <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                    <Pill tone="brand">{page.language}</Pill>
                    <StatusBadge status={page.status} />
                  </div>
                </div>
                <ChevronRight className="h-5 w-5 shrink-0 text-muted transition group-hover:translate-x-0.5 group-hover:text-brand" />
              </div>

              <div className="mt-4 flex items-center gap-1.5">
                {page.platforms.map((p) => {
                  const { Icon, color, label } = PLATFORM_META[p]
                  return <Icon key={p} className={`h-4 w-4 ${color}`} aria-label={label} />
                })}
                {orgByPage[page.id]?.email && (
                  <span className="ml-1.5 inline-flex min-w-0 items-center gap-1 text-xs text-muted">
                    <Mail className="h-3.5 w-3.5 shrink-0" />
                    <span className="truncate">{orgByPage[page.id]!.email}</span>
                  </span>
                )}
              </div>

              <div className="mt-4 flex items-center gap-6 border-t border-line pt-3 text-sm">
                <span>
                  <span className="font-semibold">{page.videoCount}</span>{' '}
                  <span className="text-muted">video</span>
                </span>
                <span>
                  <span className="font-semibold">{page.publishedCount}</span>{' '}
                  <span className="text-muted">đã đăng</span>
                </span>
                {orgByPage[page.id]?.manageUrl && (
                  <a
                    href={orgByPage[page.id]!.manageUrl}
                    target="_blank"
                    rel="noreferrer"
                    onClick={(e) => e.stopPropagation()}
                    className="group/manage ml-auto inline-flex items-center gap-1.5 rounded-full bg-panel px-2.5 py-1 text-xs font-medium ring-1 ring-line transition hover:ring-brand/40 active:scale-95"
                  >
                    Quản lý
                    <span className="grid h-4 w-4 place-items-center rounded-full bg-brand/12 text-brand transition group-hover/manage:translate-x-0.5 group-hover/manage:-translate-y-px">
                      <ArrowUpRight className="h-3 w-3" />
                    </span>
                  </a>
                )}
              </div>
            </Card>
          </div>
        ))}
      </div>

      {/* Account & channel relationship map */}
      <div className="pt-2">
        <SectionTitle sub="Mỗi trang chạy trên tài khoản Google riêng để một lệnh cấm không lan ra. Bảng điều khiển → tài khoản → kênh, kèm link quản lý.">
          Sơ đồ tài khoản &amp; kênh
        </SectionTitle>
        <Card className="overflow-x-auto p-5">
          <OrgChart onOpenPage={onOpenPage} />
        </Card>
      </div>
    </div>
  )
}

