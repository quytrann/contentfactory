---
name: qa-integration
description: >-
  How to QA ContentFactory by cross-checking integration boundaries (not just
  "does it exist"): API JSON response shape ↔ web TS types/hooks, DB columns ↔
  API fields ↔ web client, and pipeline step contracts (worker output ↔ next
  step input). Use to verify a change is wired correctly end-to-end. Triggers on
  "QA", "verify", "boundary", "contract", "integration", "kiểm tra tích hợp".
---

# QA — integration integrity

Used by **qa**.

## The boundary method (core)
Don't confirm existence — **read both sides and compare shape**:
- **API ↔ web**: the route's JSON (e.g. `fetch_jobs`, `/api/bootstrap`) vs `types.ts` + the consuming hook/view. Every field name/type/optional must match. API-returns-but-type-omits (or vice-versa) = bug.
- **DB ↔ API**: a new column is in schema/seed ALTER → selected in the query → camelCased in the return → present in the web type.
- **Pipeline**: a worker's output keys ↔ the next step's expected input keys (ingest→script→tts→assemble). A renamed key silently breaks downstream.

## How
- **Incremental** — verify right after each module, not once at the end.
- Real evidence: `curl` the live API, query Postgres, run the worker, `tsc --noEmit`. Compare actual values.
- For each boundary report: the two sides, the exact match/mismatch, `file:line`.

## Honesty
"Pass" only when you actually compared both sides and they matched. Couldn't verify → say why; never assume. Findings → `_workspace/NN_qa_*.md`. Escalate suspicious-but-out-of-scope to security-review / leader.
