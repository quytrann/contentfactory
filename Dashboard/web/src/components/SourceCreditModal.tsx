import { useState } from 'react'
import { Loader2, PencilLine } from 'lucide-react'
import { useResumeJob } from '../data'
import type { CreditField, Job } from '../types'
import { Button, Modal, TextInput, useToast } from '../ui'

// The four credit fields a Dubbed pause can ask for, with Vietnamese labels and
// per-field input hints. The backend keys both missingFields and prefill by
// these exact names (see BE contract §2).
const FIELD_META: Record<CreditField, { label: string; placeholder: string }> = {
  sourceName: { label: 'Tên nguồn', placeholder: 'VD: Tên kênh gốc' },
  sourceLink: { label: 'Link nguồn', placeholder: 'https://…' },
  handle: { label: 'Handle', placeholder: '@kênh' },
  logo: { label: 'Logo (đường dẫn)', placeholder: 'E:/path/to/logo.png' },
}

// All four fields, in display order — we show known prefill values for every
// field (editable) and highlight the ones the backend flagged as missing.
const ALL_FIELDS: CreditField[] = ['sourceName', 'sourceLink', 'handle', 'logo']

// Modal that resolves a Dubbed job parked at status 'needs_input'. It lists the
// source-credit fields prefilled from needsInput.prefill, lets the user fill the
// empty ones, and offers two deliberate actions:
//   - "Lưu & tiếp tục" → resume(jobId, { skip:false, ...entered values })
//   - "Bỏ qua (không ghi nguồn)" → resume(jobId, { skip:true }) — confirmed
// All UI copy is Vietnamese; identifiers/comments English.
export function SourceCreditModal({ job, open, onClose }: { job: Job; open: boolean; onClose: () => void }) {
  const resumeJob = useResumeJob()
  const { success, error: toastError } = useToast()
  const ni = job.needsInput

  // One controlled value per field, seeded from prefill (null → '').
  const [values, setValues] = useState<Record<CreditField, string>>(() => ({
    sourceName: ni?.prefill.sourceName ?? '',
    sourceLink: ni?.prefill.sourceLink ?? '',
    handle: ni?.prefill.handle ?? '',
    logo: ni?.prefill.logo ?? '',
  }))
  const [busy, setBusy] = useState<'save' | 'skip' | null>(null)
  const [confirmSkip, setConfirmSkip] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  // Guard: nothing to resolve without a needsInput payload (shouldn't render).
  if (!ni) return null

  const missing = new Set<CreditField>(ni.missingFields)
  const set = (f: CreditField, v: string) => setValues((p) => ({ ...p, [f]: v }))

  const doSave = async () => {
    setBusy('save')
    setErr(null)
    try {
      // Send only non-empty fields; trim so a whitespace-only entry counts as empty.
      const body: { skip: false; sourceName?: string; sourceLink?: string; handle?: string; logo?: string } = { skip: false }
      for (const f of ALL_FIELDS) {
        const v = values[f].trim()
        if (v) body[f] = v
      }
      await resumeJob(job.id, body)
      success('Đã lưu nguồn và tiếp tục')
      onClose() // on success the dataset refreshes; the job leaves needs_input.
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
      toastError('Không tiếp tục được job')
      setBusy(null)
    }
  }

  const doSkip = async () => {
    setBusy('skip')
    setErr(null)
    try {
      await resumeJob(job.id, { skip: true })
      success('Đã tiếp tục (không ghi nguồn)')
      onClose()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
      toastError('Không tiếp tục được job')
      setBusy(null)
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="Nhập thông tin nguồn">
      <div className="space-y-4">
        <p className="text-sm text-muted">
          Video lồng phụ đề này thiếu thông tin nguồn nên đã tạm dừng. Hãy điền thông tin để ghi nguồn ở cuối video,
          hoặc bỏ qua nếu chấp nhận đăng mà không ghi nguồn.
        </p>

        <div className="space-y-3">
          {ALL_FIELDS.map((f) => {
            const meta = FIELD_META[f]
            const isMissing = missing.has(f)
            return (
              <label key={f} className="block">
                <span className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-muted">
                  {meta.label}
                  {isMissing && (
                    <span className="rounded px-1 py-0.5 text-[10px] font-medium text-amber-700 ring-1 ring-inset ring-amber-600/30 dark:text-amber-200 dark:ring-amber-400/30">
                      thiếu
                    </span>
                  )}
                </span>
                <TextInput value={values[f]} onChange={(v) => set(f, v)} placeholder={meta.placeholder} />
              </label>
            )
          })}
        </div>

        {err && <p className="line-clamp-3 text-xs text-rose-600 dark:text-rose-400">{err}</p>}

        {confirmSkip ? (
          <div className="space-y-3 rounded-lg border border-amber-600/30 bg-amber-500/10 p-3">
            <p className="text-xs text-amber-700 dark:text-amber-200">
              Bạn chắc chắn muốn đăng video mà KHÔNG ghi nguồn? Đây là lựa chọn có chủ đích và sẽ được ghi nhận.
            </p>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setConfirmSkip(false)} disabled={busy !== null}>
                Quay lại
              </Button>
              <button
                onClick={doSkip}
                disabled={busy !== null}
                className="inline-flex items-center justify-center gap-2 rounded-lg border border-amber-600/50 bg-amber-500 px-3.5 py-2 text-sm font-medium text-white transition hover:bg-amber-600 active:scale-[.98] disabled:pointer-events-none disabled:opacity-50"
              >
                {busy === 'skip' && <Loader2 className="h-4 w-4 animate-spin" />}
                Xác nhận bỏ qua
              </button>
            </div>
          </div>
        ) : (
          <div className="flex items-center justify-between gap-2">
            <button
              onClick={() => setConfirmSkip(true)}
              disabled={busy !== null}
              className="text-xs font-medium text-muted underline-offset-2 transition hover:text-fg hover:underline disabled:pointer-events-none disabled:opacity-50"
            >
              Bỏ qua (không ghi nguồn)
            </button>
            <Button onClick={doSave} disabled={busy !== null}>
              {busy === 'save' && <Loader2 className="h-4 w-4 animate-spin" />}
              Lưu &amp; tiếp tục
            </Button>
          </div>
        )}
      </div>
    </Modal>
  )
}

// The small per-row trigger used in the Jobs view: opens the modal for a parked
// job. Co-located with the modal so any view can drop it onto a needs_input job.
export function SourceCreditButton({ job }: { job: Job }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button
        onClick={() => setOpen(true)}
        title="Nhập thông tin nguồn để tiếp tục"
        aria-label="Nhập thông tin nguồn"
        className="inline-flex items-center justify-center gap-1 rounded-md border border-amber-600/40 bg-amber-500/10 px-2 py-1 text-xs font-medium text-amber-700 transition hover:bg-amber-500/20 dark:text-amber-200"
      >
        <PencilLine className="h-3.5 w-3.5" />
        Nhập nguồn
      </button>
      {open && <SourceCreditModal job={job} open onClose={() => setOpen(false)} />}
    </>
  )
}
