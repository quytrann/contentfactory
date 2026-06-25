# ContentFactory Dashboard (web)

Visual dashboard to review ContentFactory features before starting a channel.
**React 19 + TypeScript + Vite + Tailwind CSS v4.** Responsive and mobile-friendly
(sidebar on desktop, bottom navigation on mobile) — built to later port to a mobile app.

Currently runs on **mock data** that mirrors [../db/schema.sql](../db/schema.sql); no backend is connected yet.

## Commands

```bash
npm install     # install dependencies
npm run dev     # start dev server (http://localhost:5173)
npm run build   # type-check + production build to dist/
npm run preview # serve the production build locally
```

## Structure

```
src/
├── App.tsx          App shell: responsive nav + view routing
├── types.ts         Types mirroring the DB schema
├── mockData.ts      Sample pages / jobs / videos / accounts
├── ui.tsx           Shared pieces: Card, Pill, StatusBadge, platform metadata, formatters
├── components/      Reusable widgets (Pipeline)
└── views/           Overview · Pages · PageDetail · Jobs · Videos · Publishing
```

When the backend exists, replace `mockData.ts` with API calls; the views and types stay the same.
