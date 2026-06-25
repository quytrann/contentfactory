import { ChevronRight } from 'lucide-react'
import { PIPELINE } from '../mockData'

// Horizontal, scrollable production pipeline. Scrolls on small screens
// instead of overflowing the page body.
export default function Pipeline() {
  return (
    <div className="-mx-1 overflow-x-auto pb-2">
      {/* w-max + mx-auto centers the track within the card when it fits, and lets
          the parent scroll (without clipping the leading edge) when it overflows. */}
      <div className="mx-auto flex w-max items-stretch gap-1 px-1">
        {PIPELINE.map((stage, i) => (
          <div key={stage.key} className="flex items-center">
            <div className="flex min-w-[112px] flex-col rounded-xl border border-line bg-panel2 px-3 py-2.5">
              <span className="text-[10px] font-semibold uppercase tracking-wider text-brand">
                {String(i + 1).padStart(2, '0')}
              </span>
              <span className="mt-0.5 text-sm font-medium leading-tight">{stage.label}</span>
              <span className="mt-1 text-[11px] text-muted">{stage.tool}</span>
            </div>
            {i < PIPELINE.length - 1 && (
              <ChevronRight className="mx-0.5 h-4 w-4 shrink-0 text-muted/40" />
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
