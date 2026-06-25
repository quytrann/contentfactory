# Dashboard API

A small read-only FastAPI service that serves the dashboard's data from the
local PostgreSQL database. The browser cannot talk to PostgreSQL directly, so
this sits between the React app and the database.

## Setup (Windows, PowerShell)

The machine's `python`/`python3` are Microsoft Store stubs; use the `py`
launcher, which resolves to Python 3.11.

```powershell
cd Dashboard/api
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt

# Configure the database connection:
copy .env.example .env
#   then edit .env and set PGPASSWORD (or a full DATABASE_URL)
```

## Seed the database

From the repo root, with the schema already applied:

```powershell
& "E:\Installed\PostgreSQL16\bin\psql.exe" -U postgres -d contentfactory -f Dashboard/db/seed.sql
```

## Run

```powershell
cd Dashboard/api
.\run-api.ps1
```

`run-api.ps1` is the canonical dev launcher: it runs uvicorn with `--reload` and
sets `WATCHFILES_FORCE_POLLING=1` so file changes (edits AND new files) are
reliably detected on Windows — plain `--reload` here intermittently misses them.
The polling env var lives in the launcher (not `.env`), because it must be set in
the reloader process before the app loads. It only applies while the server runs;
nothing is left running in the background after you stop it.

Raw equivalent (no reliable reload on Windows):
`.\.venv\Scripts\python -m uvicorn main:app --host 127.0.0.1 --port 4000 --reload`

The Vite dev server proxies `/api` to `http://127.0.0.1:4000`, so start this
before (or alongside) `npm run dev` in `Dashboard/web`.

## Endpoints

All return JSON shaped to match the frontend's TypeScript types (camelCase).

| Route             | Returns                                            |
| ----------------- | -------------------------------------------------- |
| `GET /api/health` | `{ ok: true }` — also checks the DB connection     |
| `GET /api/pages`  | pages with platforms, config, video counts         |
| `GET /api/accounts` | platform accounts (incl. approval state)         |
| `GET /api/jobs`   | production jobs                                    |
| `GET /api/videos` | rendered videos with scene count + posted platforms |
| `GET /api/analytics` | KPIs, daily views/likes, monthly output, platform split |
| `GET /api/org`    | account → channel map                              |
| `GET /api/bootstrap` | everything above in one response                |

Notes:

- The schema has no revenue field, so the Overview's monthly chart reports
  **videos produced per month** (real) instead of revenue.
- KPIs (Views, Likes, Comments) are computed from the `metrics` snapshots.
