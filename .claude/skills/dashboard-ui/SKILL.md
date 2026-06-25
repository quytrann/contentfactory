---
name: dashboard-ui
description: >-
  How to build/change the ContentFactory React+TypeScript+Tailwind dashboard
  (Dashboard/web): views (PageDetail/Studio, Pages, Publishing, Overview,
  Videos, Jobs), components (OrgChart, Charts), ui.tsx primitives, data.tsx,
  api.ts, types.ts. Use for any dashboard UI/UX change, new form field, layout,
  styling, or data-wiring on the web side. Triggers on "dashboard", "Studio",
  "form", "dropdown", "view", "component", "UI", "Tạo video", "PageDetail".
---

# Dashboard UI (frontend)

Used by **frontend-engineer**.

## Stack & conventions
- Vite + React + TS + Tailwind. UI copy = **Vietnamese**; code/comments = English.
- Primitives in `ui.tsx`: `Field` (label + control + hint-below), `Select`, `Card`, `Pill`, `StatusBadge`, `PLATFORM_META`, `Button`, `Modal`, `TextInput`. Reuse these, don't reinvent.
- Tailwind tokens: `bg-panel/panel2`, `text-fg/muted`, `border-line`, brand via `accent-[var(--color-brand)]`. **Always provide `dark:` variants and check light-mode contrast** (amber/yellow on light needs `text-amber-700 dark:text-amber-200`).
- Data: `data.tsx` `DataProvider` fetches `/api/bootstrap`, falls back to `mockData` ("review mode"); `api.ts` holds mutations; `types.ts` mirrors the API JSON.

## How to add a form field / option (common task)
- Catalog-style options (render models, voice-clone models, edit modes) live as `const` arrays in `PageDetail.tsx` with `{value,label,desc}`; render `<Select>` + show `desc` via `Field hint`.
- A field that persists to a job → add to `NewJobBody` (`api.ts`) + `Job` (`types.ts`), pass in `createJob`, and confirm backend-engineer threaded it server-side.
- Nested interactive: never `<a>`/`<button>` inside `<button>` — use `role="button"` div + `onKeyDown` + `stopPropagation`.

## Verify
- `npx tsc --noEmit` after every change; report the real exit code. Dev server (`:5173`) hot-reloads.
- Hand boundary checks (API shape ↔ types) to qa.
- Management notes → `_workspace/`; mocks stay in `mockData.ts`.
