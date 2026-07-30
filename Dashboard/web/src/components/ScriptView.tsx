// Read-only renderer for a produced video's full saved script. Handles both
// arms of the GET /api/videos/{id}/script discriminated union:
//   - kind === 'scenes' → a scene-array script (image/footage/stickman). Footage
//     scenes carry sourceStart/sourceEnd; image/stickman scenes carry image_prompt.
//   - kind === 'dubbed' → a Dubbed job's timestamped VN subtitle list.
// This lives in ONE place so any surface that only needs to DISPLAY a script
// (e.g. the Videos-library "Xem kịch bản" modal) shares the same layout. Note:
// CreateVideo's ReusableScriptPicker keeps its OWN inline renderer because that
// one is interactive (inline scene editing, per-scene TTS play, search
// highlighting) and tightly bound to picker state — extracting it here would
// either drop those features or require threading a lot of callbacks, risking
// regressions. This component is deliberately read-only.

import { Pill } from '../ui'
import type { VideoScriptDetail } from '../api'

// mm:ss (or h:mm:ss) for a timestamp in seconds. Unlike ui.tsx's fmtClock, a 0s
// value renders "0:00" (not an em-dash) — a dubbed sub legitimately starts at 0.
function fmtTs(total: number): string {
  const s = Math.max(0, Math.round(total || 0))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  const mm = h ? String(m).padStart(2, '0') : String(m)
  return `${h ? `${h}:` : ''}${mm}:${String(sec).padStart(2, '0')}`
}

export function ScriptView({ detail }: { detail: VideoScriptDetail }) {
  // Dubbed transcript: read-only timestamped VN subtitle list. `kind !== 'dubbed'`
  // (below) also covers a legacy response missing `kind` (backward-safe).
  if (detail.kind === 'dubbed') {
    return (
      <div>
        <div className="mb-3 flex items-center gap-2">
          <Pill tone="amber">Phụ đề lồng tiếng</Pill>
          <span className="text-xs text-muted">{detail.subCount} dòng</span>
        </div>
        <ol className="space-y-1.5">
          {detail.subs.map((sub, i) => (
            <li key={i} className="flex gap-2 text-xs">
              <span className="shrink-0 tabular-nums text-muted/80">
                {fmtTs(sub.start)} → {fmtTs(sub.end)}
              </span>
              <span className="text-fg">{sub.text_vi}</span>
            </li>
          ))}
        </ol>
      </div>
    )
  }

  // Scene-array script (image/footage/stickman).
  return (
    <div>
      <div className="mb-3">
        <Pill tone="brand">{detail.sceneCount} cảnh</Pill>
      </div>
      <ol className="space-y-2.5">
        {detail.scenes.map((sc) => {
          const hasSourceSpan = sc.sourceStart != null || sc.sourceEnd != null
          return (
            <li key={sc.scene} className="text-xs">
              <span className="font-semibold text-muted">Cảnh {sc.scene}.</span>{' '}
              <span className="text-fg">{sc.narration}</span>
              {/* Footage scenes carry a source time span (mm:ss → mm:ss). */}
              {hasSourceSpan && (
                <p className="mt-0.5 flex items-center gap-1 text-[11px] tabular-nums text-muted/80">
                  ⏱ {fmtTs(sc.sourceStart ?? 0)} → {fmtTs(sc.sourceEnd ?? 0)}
                </p>
              )}
              {/* Image / stickman scenes carry the SDXL image prompt. */}
              {sc.image_prompt && (
                <p className="mt-0.5 text-[11px] italic text-muted/80">🖼 {sc.image_prompt}</p>
              )}
            </li>
          )
        })}
      </ol>
    </div>
  )
}
