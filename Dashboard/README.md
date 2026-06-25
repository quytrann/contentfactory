# Dashboard — ContentFactory control center

Manages **multiple pages (channels) in parallel**. Each page is an identity container (name, language, platform accounts, analytics anchor). Production options — render engine, voice model, editing mode, source type — are chosen **per-job at video creation time**, not locked to the page.

## Database

- **Now:** PostgreSQL running **locally**.
- **Later:** move to cloud (Supabase/RDS) when budget allows — the schema stays the same.
- Schema: [db/schema.sql](db/schema.sql)

Multi-page design: each page is one row in the `pages` table. All jobs / videos / assets / posts / metrics are keyed by `page_id`. Adding a new page = inserting a row, not changing the schema.

## Initialize the DB (local)

```bash
# Install PostgreSQL locally, create the database, then load the schema:
createdb contentfactory
psql -d contentfactory -f db/schema.sql
```

## Registering a new page

Insert a row into the `pages` table (see the example in [config/pages.example.json](config/pages.example.json)).
Each page's creator/author/credit fields are **provided by the owner** — leave them as `TODO_ASK_USER` until the owner fills them in.

## Account isolation

Each page uses its **own** account per platform (`platform_accounts` table) so that a termination on one channel does not cascade to other pages' channels. Credentials are never stored in the DB — `credentials_ref` points to a token file under `secrets/<page>/<platform>.json`, which is gitignored.
