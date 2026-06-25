---
name: security-audit
description: >-
  How to security-review ContentFactory changes: OAuth/credential handling
  (path-only refs, never in DB/config/logs), per-page account isolation,
  the borrowed-account rule, secret leakage, API authz/IDOR, and the local-only
  constraint. Use when code touches auth, secrets, accounts, publishing, data
  exposure, or external/paid services. Triggers on "security", "bảo mật",
  "secret/token/credential", "auth", "account isolation", "lỗ hổng".
---

# Security audit

Used by **security-review**.

## Project-specific checklist
- **Secrets path-only**: `platform_accounts.credentials_ref` + config `accounts` block hold a PATH to `Dashboard/secrets/<page>/<platform>.json` (gitignored). No raw token/secret in DB, config JSON, code, logs, or `_workspace/`. Grep committed paths to confirm.
- **Account isolation**: `UNIQUE(page_id, platform)`; each page its OWN platform account (termination cascades across a shared Google account = finding).
- **Borrowed-account rule**: creator/author/credit/channel must be the project owner or `TODO_ASK_USER` — never auto-filled from the logged-in Claude account. Flag any such inference.
- **API surface**: mutating/exposing routes keyed by id (video/job/publish) → authz/IDOR, input validation, no token leak on the upload/publish path.
- **Local & free**: any new paid API / cloud call violates the constraint — flag it.

## How
- Incremental (after each module). Real checks: grep for secrets, inspect actual DB rows & config files, trace credential flow.
- Each finding: severity (critical/high/low/info), what & where (`file:line`), concrete fix.

## Escalation (risk-mode = ASK)
Send findings to `leader`. Critical/high → leader asks the user before proceeding; low/info → noted, non-blocking. **Outranks** other agents on conflict. Honesty: never downgrade/hide a real risk; couldn't verify → say so. Findings → `_workspace/NN_security_*.md`.
