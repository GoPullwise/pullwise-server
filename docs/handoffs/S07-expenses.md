# S07 — Project and shared expenses

Status: Local implementation complete; continued to S08 by the developer's explicit S10 instruction.

## Implemented

- `pullwise_server/cloudflare_ledger_expenses.py` provides owner-scoped create, detail, list, edit, and soft removal. `cloudflare_ledger_api.py` dispatches the routes and `cloudflare/server/src/entry.py` handles JSON, no-store responses, and Cookie Origin checks.
- Decimal amount strings convert to integer minor units using ISO 4217 exponents, with negative, excess-precision, unknown-code, invalid-date, and safe-integer overflow rejection.
- Creates use owner-scoped `Idempotency-Key`; updates/removals use quoted `If-Match` revisions. The D1 write batch records every mutation in `expense_events`. Moves check both old and new target restrictions; project-to-shared moves retain only one record. A lost repository blocks a new target while historical edits and removal remain available.
- `tests/test_ledger_routes.py` covers precision, replay/conflict, target move, stale revision, audit, removal, API-key target denial, and lost access.

## Verification and limitations

- Focused route tests and all `test_ledger*.py` suites passed locally. Python compile and Worker module sync checks passed after the final source sync.
- No real D1, Wrangler, Cloudflare, GitHub, or remote migration was run. CI status was unavailable because `gh` is absent. Worker transaction/FFI behavior remains for S18.

## Next entry

S08: share expense filters across list/report/export, group exact minor units by currency, date and category, paginate owner-visible rows, and guard CSV cells against formulas.
