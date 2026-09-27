# S08 — Ledger reports, pagination and CSV export

Status: Local implementation complete with the export limit below; continued to S09 by the developer's explicit S10 instruction.

## Implemented

- `pullwise_server/cloudflare_ledger_reports.py` shares target/project/category/date/currency filters with the expense list, applies API-key target restrictions in SQL, and groups only active expenses by currency for summary, day/month time series, and category reports. Summary separates project, shared, and account totals; no currency conversion occurs.
- Project and expense lists use owner-scoped cursors and limits. The Worker returns CSV for `/api/v1/expenses/export`; it quotes CSV fields and prefixes formula-like cells. Export excludes soft-deleted and unauthorized rows.
- Cookie and Bearer Key use the same ledger authorization and DTO paths. `tests/test_ledger_routes.py` covers report totals, a date bucket, pagination, CSV escaping, and restricted-key behavior.

## Verification and limitations

- All `test_ledger*.py` suites, Python compilation, module sync, and S01 static checks passed locally. No remote Cloudflare/D1/Wrangler/GitHub calls were run. CI status was unavailable because `gh` is absent.
- CSV export is currently capped at 10,000 rows with a 413 error beyond the cap and is assembled in memory. The OpenAPI text describes a streamed export; implement and validate streaming before production use.
- Cursor values are owner-filtered stable IDs, not opaque signed cursors. Review cursor encoding and large-account costs in S17 before production.

## Next entry

S09 Web: use `src/api/ledger.js` to build project selection, descriptions, categories, and lost-GitHub-access history views. Keep platform billing separate from user expenses.
