# S17/S18 bounded D1 validation

Updated 2026-09-28. The user authorized S17/S18 conditional on controlling
D1 usage, especially Rows Written. This is an execution candidate, not a
record of completed runtime acceptance or authorization to release production.

## Budget and execution status

- Approved cumulative S18 ceiling: 1,000 Rows Written and 10,000 Rows Read.
  The user also authorized restoring pinned development dependencies and
  deploying Server separately at `https://api.pull-wise.com`.
- Remote database query/DDL/data operations performed so far: **zero**.
  A new empty `pullwise-ledger-production` database was created via the
  metadata API (ID `80a29a0d-5699-449f-9541-a01dc461ca9d`). No remote migrations
  or fixtures have been applied. Worker deployment status is recorded in
  `local-acceptance.md`.
- No cron, scheduled handler, monitoring queries against D1, load testing,
  automatic polling or automatic retries are permitted.
- S17 uses only local bindings (`remote: false`), an isolated local state
  directory, synthetic fixtures and no GitHub/Creem/Jev network calls.
- S18 uses a reviewed, distinct preview database with no production data.
  Preview isolation does not imply a separate included allowance.

## Accounting

[D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/) defines
Rows Written to include INSERT/UPDATE/DELETE and additional index writes;
DDL may also contribute reads and writes. Under the documented paid rate,
1,000 additional written rows cost $0.001 for this D1 usage component alone.
This does not cap Workers, storage, other services or unrelated account usage.

Each executed statement/batch must report `meta.rows_read` and
`meta.rows_written`. Aggregate these in a local evidence file, with no
extra D1 monitoring writes. Missing metadata, timeouts or unexpected values
stop the run; do not retry ambiguous requests automatically. A request cap
and verified worst-case reservation must prevent starting a step that could
exceed the remaining budget. Measuring after a request alone is not a hard cap.

## Source audit: ledger mutations

These counts describe application-table mutations on the success path, before
index effects. They are NOT Cloudflare billed-row measurements or proven
remote upper bounds. They assume an empty guard table and serial validation.

| Operation | Base table rows written | Source reasoning |
| --- | ---: | --- |
| Create project/category | 3 | One credential guard insert, one resource insert, one guard deletion |
| Update project/category or archive category | 5 | Two guard inserts, one resource update, two guard deletions |
| Create expense | 9 | Three guard inserts/deletions plus expense, audit and idempotency inserts |
| Update expense | 6–10 | Two to four guard inserts/deletions, one expense update, one audit insert |
| Remove expense | 6 | Two guard inserts/deletions, one expense update, one audit insert |
| Replay same expense creation / repeat removal | 0 | Existing response / already deleted early return |
| Expense list, reports and CSV | 0 | Principal/snapshot and resource SELECTs only |

Sources: `cloudflare_ledger_api.py`, `cloudflare_ledger_expenses.py`,
`cloudflare_ledger_reports.py`, `cloudflare_ledger_auth.py`,
`cloudflare_principal.py`. Guard DELETE has no WHERE clause; any unexpected
pre-existing guard rows invalidate these counts. Reject a nonempty guard
fixture locally and do not validate against an existing shared database.

Indexes on expenses, events, idempotency, projects and categories add writes.
For the small ledger-only candidate, reserve 32 written rows per mutation
and 500 read rows per request until local runtime measurements and schema
analysis establish conservative bounds. These reservations are proposals,
not verified guarantees. Do not execute remotely using an unverified bound.

## Finite case candidate

Use one test account, at most two projects, two categories and eight expenses.
Keep full-page CSV boundary fixtures (250/251 rows) exclusively local.
CSV remains paginated in chunks of 250; list requests use explicit small limits.
No cached protected response is shared between accounts; no refresh loop is
used to wait for provider state.

| Case group | Maximum ledger HTTP requests | Written-row reservation |
| --- | ---: | ---: |
| Two projects and two categories | 4 | 128 |
| Four expenses, including mixed currencies and shared pool | 4 | 128 |
| Creation replay, move, edit, stale revision, remove, repeat removal | 6 | 192 (reserve each request conservatively) |
| Filtered list, three reports, CSV, denied scope | 6 | 0 after read-only path verification |
| Ledger subtotal | 20 | 448 plus verified read bounds |

This ledger subset alone does not complete S18. Identity login/App callbacks,
API-key creation/revocation, Creem signed webhook/replay, migration DDL,
seeding and cleanup must have separately established bounds fitting the
remaining allowance before remote execution. Their bounds are currently
unknown. Real anonymized Jev quality samples are unavailable; keep Jev off.

## Required controls before S18

1. Numeric ceiling is confirmed. Review distinct preview targets before S18.
2. Complete S17 with pinned local tooling and capture actual local D1 metadata.
3. Bound migration/index/identity/payment/cleanup effects; revise the finite
   case list if they cannot fit. Never copy an existing account/database.
4. Implement and verify admission control covering EVERY preview D1 path,
   including OAuth/provider callbacks and concurrent requests. A client-side
   runner cap alone cannot constrain unsolicited traffic to a public Worker.
5. Reserve worst-case rows before each request, serially; stop on any error,
   missing accounting or divergence. Keep no background work running after
   the finite run. Include teardown writes in the reservation.
6. Recheck configurations contain no cron or remote bindings for local work.

## Current prerequisites

- Pinned Wrangler 4.136.3, workers-py 1.17.4, workers-runtime-sdk 1.9.0 and
  uv 0.12.3 were restored with permission. A separate healthy Python 3.10.12
  reference environment and Python 3.14.2 Worker tooling live under the
  ignored workspace `.agents/runtime/`; the broken original venv is preserved.
- Real local Worker/D1 migrations and 17 HTTP acceptance requests passed,
  including the 251-record paginated CSV/ReadableStream bridge, Cookie/API key,
  CRUD, idempotency, conflict and reports. They incurred no remote D1 usage.
- Server preview configuration still has placeholder values. Production points
  to the user's approved API domain and empty production database. Web's
  existing remote origin is already `https://api.pull-wise.com`.
- `PULLWISE_D1_ACCESS_ENABLED=0` rejects every route before D1/provider access;
  missing/invalid values also fail closed. Both remote configs keep it off.
  This is a verified zero-access pause, not an implemented metered quota for
  an active service. Do not turn it on until finite admission/accounting
  controls and required provider prerequisites have passed.
- No Cloudflare/GitHub/Creem credential variable names or local .dev.vars were
  available in the inspected task environment. This does not establish that
  no dashboard/CLI credentials exist; none were read or exposed.
- Cloudflare CLI/connector authentication was subsequently confirmed without
  reading its token. The user supplied GitHub App ID 3631508, Client ID
  Iv23lipOl05N1ZULZmYW and slug gopullwise. The user confirmed changing both
  callbacks to the Web proxy URLs. Public App configuration was deployed and
  verified while D1 remained disabled. A fresh 32-byte random Token encryption
  key was stored as `PULLWISE_GITHUB_TOKEN_KEY` in Cloudflare Secrets, without
  displaying or checking in its value. A later names/type-only check confirms
  the GitHub Client Secret and both Creem Secrets are now present. The four
  user-supplied Creem product IDs were synchronized as a plain_text JSON
  string and read back exactly; all existing bindings were inherited and D1
  remains off. No payment/provider or D1 request was made for this sync.
  Secret presence is not validation of its value, and product environment/
  prices and real OAuth/install/payment behavior remain unverified.
- CodeGraph did not return within the bounded lookup; source audit used the
  implementation files named in current checked-in documentation.

Web `npm run check` (33 files / 252 tests, lint/build) and
`npm run check:workers` passed in this continuation. The browser connector
remains unavailable; S17 browser integration and S18 provider acceptance
remain open. See the current Server acceptance record for reference/CI results.
