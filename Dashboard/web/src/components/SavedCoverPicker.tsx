import { useEffect, useState } from 'react'
import { CheckSquare, Loader2, Square, Trash2 } from 'lucide-react'
import { api } from '../api'
import { Modal, useToast } from '../ui'

// A generated cover in a page's cache dir (GET /generate/cover/created).
// CoverResult-compatible (path+url); filename/savedAt are display metadata.
export interface SavedCover {
  path: string
  url: string
  filename: string
  savedAt: string
  // The CLEAN (title-less) sibling this cover's title was baked onto, or "" when the
  // image has no baked title (manual-prompt render, _txt_* overlay, hand-dropped file).
  // Use it — NOT `path` — as the compositing base when re-rendering an edited title,
  // otherwise the new title stacks on top of the baked one.
  basePath?: string
}

// Modal that browses a page's generated covers and lets the owner pick one. Renders
// a responsive thumbnail grid (newest first); picking a thumbnail lifts the chosen
// cover up via onPick, and each thumbnail carries a delete affordance. Shared by the
// Studio (Tạo video) cover-browse flow and the Videos list "Đổi cover" action.
// (The old saved-cover library was removed; the created-cover cache already lists
// every generated cover, so there is a single source now.)
//
// A "Chọn nhiều" (multi-select) mode lets the owner tick several covers and delete
// them in one action. When select mode is OFF, clicking a thumbnail picks it (the
// normal onPick behavior) and each thumbnail keeps its single-delete trash button.
export function SavedCoverPicker({
  pageName,
  onClose,
  onPick,
}: {
  pageName: string
  onClose: () => void
  onPick: (cover: SavedCover) => void
}) {
  const [covers, setCovers] = useState<SavedCover[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Path of the cover currently being deleted, for the per-thumbnail spinner + to
  // disable double-clicks. null = none in flight.
  const [deletingPath, setDeletingPath] = useState<string | null>(null)
  // The cover pending delete-confirmation. null = no dialog.
  const [confirmDelete, setConfirmDelete] = useState<SavedCover | null>(null)
  // Multi-select mode: when true, thumbnail clicks toggle selection instead of
  // picking; a bulk-delete toolbar appears. `selected` holds the ticked paths.
  const [selectMode, setSelectMode] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  // Bulk-delete in flight (disables the grid + toolbar while paths are removed).
  const [bulkDeleting, setBulkDeleting] = useState(false)
  // Whether the bulk-delete confirm dialog is open.
  const [confirmBulk, setConfirmBulk] = useState(false)
  const { success, error: toastError } = useToast()

  useEffect(() => {
    let cancelled = false
    setCovers(null)
    setError(null)
    api.listCreatedCovers(pageName)
      .then((res) => !cancelled && setCovers(res.covers))
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : String(e)))
    return () => {
      cancelled = true
    }
  }, [pageName])

  // Delete a generated cover from the cache dir. Called by the confirm modal's
  // "Xoá" button; drops it from the local list on success (no full refetch) and
  // closes the dialog.
  const deleteCover = async (c: SavedCover) => {
    if (deletingPath) return
    setDeletingPath(c.path)
    try {
      await api.deleteCreatedCover({ page: pageName, path: c.path })
      setCovers((prev) => (prev ? prev.filter((x) => x.path !== c.path) : prev))
      success('Đã xoá cover')
    } catch (e) {
      toastError(e instanceof Error ? e.message : 'Xoá cover thất bại')
    } finally {
      setDeletingPath(null)
      setConfirmDelete(null)
    }
  }

  // Toggle one cover's selection (multi-select mode only).
  const toggleSelect = (path: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })

  // Leave select mode and drop any ticks.
  const exitSelectMode = () => {
    setSelectMode(false)
    setSelected(new Set())
  }

  // Bulk-delete every selected cover. Loops with Promise.allSettled so one failure
  // doesn't abort the rest; removes the successfully-deleted paths from the local
  // list (no refetch), toasts success/partial-failure, then clears + exits select
  // mode. Called by the bulk-confirm modal's "Xoá" button.
  const bulkDelete = async () => {
    if (bulkDeleting || selected.size === 0) return
    setBulkDeleting(true)
    const paths = [...selected]
    const results = await Promise.allSettled(
      paths.map((p) => api.deleteCreatedCover({ page: pageName, path: p })),
    )
    const okPaths = paths.filter((_, i) => results[i].status === 'fulfilled')
    const failCount = paths.length - okPaths.length
    const okSet = new Set(okPaths)
    setCovers((prev) => (prev ? prev.filter((x) => !okSet.has(x.path)) : prev))
    if (failCount === 0) success(`Đã xoá ${okPaths.length} cover`)
    else if (okPaths.length === 0) toastError('Xoá cover thất bại')
    else toastError(`Đã xoá ${okPaths.length} cover, lỗi ${failCount}`)
    setBulkDeleting(false)
    setConfirmBulk(false)
    exitSelectMode()
  }

  const allSelected = covers !== null && covers.length > 0 && selected.size === covers.length

  return (
    <Modal open onClose={onClose} title="Duyệt cover đã tạo" maxWidthClass="max-w-2xl">
      <div className="space-y-3">
        <p className="text-xs text-muted">
          {selectMode
            ? 'Chọn nhiều cover để xoá cùng lúc. Bấm vào ảnh để chọn/bỏ chọn.'
            : 'Chọn một cover đã tạo để dùng cho video này. Ảnh mới nhất nằm đầu danh sách.'}
        </p>

        {covers === null && !error && (
          <div className="flex items-center gap-2 py-6 text-sm text-muted">
            <Loader2 className="h-4 w-4 animate-spin" /> Đang tải cover đã tạo…
          </div>
        )}
        {error && <p className="py-4 text-sm text-rose-400">Không tải được cover đã tạo: {error}</p>}
        {covers !== null && !error && covers.length === 0 && (
          <div className="py-6 text-center">
            <p className="text-sm text-muted">Chưa có cover đã tạo nào.</p>
            <p className="mt-1 text-xs text-muted/70">Tạo cover từ Studio (Tạo video) để nó xuất hiện ở đây.</p>
          </div>
        )}

        {covers !== null && covers.length > 0 && (
          <>
            {/* Toolbar: multi-select toggle + (in select mode) select-all / clear /
                bulk-delete affordances. */}
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => (selectMode ? exitSelectMode() : setSelectMode(true))}
                disabled={bulkDeleting}
                aria-pressed={selectMode}
                className={`inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-medium transition disabled:opacity-50 ${
                  selectMode ? 'border-brand bg-brand/20 text-fg' : 'border-line bg-panel text-muted hover:border-brand/40'
                }`}
              >
                <CheckSquare className="h-3.5 w-3.5" /> {selectMode ? 'Xong chọn nhiều' : 'Chọn nhiều'}
              </button>

              {selectMode && (
                <>
                  <button
                    type="button"
                    onClick={() =>
                      setSelected(allSelected ? new Set() : new Set(covers.map((c) => c.path)))
                    }
                    disabled={bulkDeleting}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-panel px-3 py-1.5 text-xs font-medium text-muted transition hover:border-brand/40 hover:text-fg disabled:opacity-50"
                  >
                    {allSelected ? 'Bỏ chọn tất cả' : 'Chọn tất cả'}
                  </button>
                  <button
                    type="button"
                    onClick={() => setSelected(new Set())}
                    disabled={bulkDeleting || selected.size === 0}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-panel px-3 py-1.5 text-xs font-medium text-muted transition hover:border-brand/40 hover:text-fg disabled:opacity-50"
                  >
                    Bỏ chọn
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirmBulk(true)}
                    disabled={bulkDeleting || selected.size === 0}
                    className="ml-auto inline-flex items-center gap-1.5 rounded-lg border border-rose-500/50 bg-rose-500 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-rose-600 disabled:opacity-50"
                  >
                    {bulkDeleting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
                    Xoá đã chọn ({selected.size})
                  </button>
                </>
              )}
            </div>

            <div className="grid max-h-[60vh] grid-cols-2 gap-3 overflow-y-auto pr-0.5 sm:grid-cols-3">
              {covers.map((c) => {
                const isSelected = selected.has(c.path)
                return (
                  // Relative container (a div, not a button) so the delete button can be
                  // a real sibling <button> — nesting a button inside a button is invalid.
                  <div
                    key={c.path}
                    className={`group relative aspect-video overflow-hidden rounded-xl border bg-panel2 transition ${
                      isSelected ? 'border-brand ring-2 ring-brand/40' : 'border-line hover:border-brand/60'
                    }`}
                  >
                    <button
                      type="button"
                      // In select mode a click toggles selection; otherwise it picks.
                      onClick={() => (selectMode ? toggleSelect(c.path) : onPick(c))}
                      disabled={bulkDeleting}
                      aria-pressed={selectMode ? isSelected : undefined}
                      title={c.filename}
                      className="absolute inset-0 h-full w-full outline-none focus:ring-2 focus:ring-brand/30 disabled:cursor-not-allowed"
                    >
                      <img
                        src={c.url}
                        alt={c.filename}
                        loading="lazy"
                        className="h-full w-full object-cover transition group-hover:scale-105"
                      />
                      <span className="absolute inset-x-0 bottom-0 truncate bg-black/55 px-1.5 py-1 text-left text-[10px] text-white/90">
                        {c.filename}
                      </span>
                    </button>

                    {/* Selection checkbox overlay (multi-select mode). */}
                    {selectMode && (
                      <div
                        className={`pointer-events-none absolute left-1.5 top-1.5 grid h-7 w-7 place-items-center rounded-lg border backdrop-blur-sm ${
                          isSelected ? 'border-brand bg-brand text-white' : 'border-line bg-black/55 text-white/80'
                        }`}
                      >
                        {isSelected ? <CheckSquare className="h-4 w-4" /> : <Square className="h-4 w-4" />}
                      </div>
                    )}

                    {/* Single-delete affordance — only outside select mode. */}
                    {!selectMode && (
                      <button
                        type="button"
                        onClick={(e) => { e.stopPropagation(); setConfirmDelete(c) }}
                        disabled={deletingPath === c.path}
                        title="Xoá cover này"
                        aria-label="Xoá cover này"
                        className="absolute right-1.5 top-1.5 grid h-7 w-7 place-items-center rounded-lg border border-rose-500/50 bg-black/55 text-rose-300 backdrop-blur-sm transition hover:bg-rose-600 hover:text-white disabled:opacity-60"
                      >
                        {deletingPath === c.path ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
                      </button>
                    )}
                  </div>
                )
              })}
            </div>
          </>
        )}
      </div>

      {/* Delete confirm — an in-app Modal (portals to <body>) replaces window.confirm. */}
      {confirmDelete && (
        <Modal open onClose={() => setConfirmDelete(null)} title="Xoá cover?" maxWidthClass="max-w-md">
          <div className="space-y-4">
            <p className="text-sm text-muted">
              Xoá ảnh cover “<span className="font-medium text-fg">{confirmDelete.filename}</span>” khỏi thư mục đã tạo?
              Không thể hoàn tác.
            </p>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setConfirmDelete(null)}
                disabled={deletingPath === confirmDelete.path}
                className="rounded-lg border border-line bg-panel2 px-4 py-2 text-sm text-fg transition hover:border-brand/40 disabled:opacity-50"
              >
                Huỷ
              </button>
              <button
                type="button"
                onClick={() => void deleteCover(confirmDelete)}
                disabled={deletingPath === confirmDelete.path}
                className="inline-flex items-center gap-1.5 rounded-lg border border-rose-500/50 bg-rose-500 px-4 py-2 text-sm font-medium text-white transition hover:bg-rose-600 disabled:opacity-50"
              >
                {deletingPath === confirmDelete.path ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
                Xoá
              </button>
            </div>
          </div>
        </Modal>
      )}

      {/* Bulk-delete confirm. */}
      {confirmBulk && (
        <Modal
          open
          onClose={() => { if (!bulkDeleting) setConfirmBulk(false) }}
          title="Xoá các cover đã chọn?"
          maxWidthClass="max-w-md"
        >
          <div className="space-y-4">
            <p className="text-sm text-muted">
              Xoá <span className="font-medium text-fg">{selected.size}</span> ảnh cover khỏi thư mục đã tạo?
              Không thể hoàn tác.
            </p>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setConfirmBulk(false)}
                disabled={bulkDeleting}
                className="rounded-lg border border-line bg-panel2 px-4 py-2 text-sm text-fg transition hover:border-brand/40 disabled:opacity-50"
              >
                Huỷ
              </button>
              <button
                type="button"
                onClick={() => void bulkDelete()}
                disabled={bulkDeleting}
                className="inline-flex items-center gap-1.5 rounded-lg border border-rose-500/50 bg-rose-500 px-4 py-2 text-sm font-medium text-white transition hover:bg-rose-600 disabled:opacity-50"
              >
                {bulkDeleting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
                Xoá {selected.size} ảnh
              </button>
            </div>
          </div>
        </Modal>
      )}
    </Modal>
  )
}
