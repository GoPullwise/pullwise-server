# S17 — Server local integration and acceptance boundary

Status: **local static and synthetic checks complete; S17 runtime acceptance remains open** on 2026-09-28. Companion: [Web S17 handoff](../../../pullwise-web/docs/handoffs/S17-local-integration.md). Do not use this handoff as S18 deployment approval.

## Contract, permissions, accounting and payment

- Compared `openapi/ledger-v1.yaml` with the Web API docs: all 21 method/path operations match. `src/api/ledger.js` and Web proxy tests cover `/api/api/v1/*` to Server `/api/v1/*`, Bearer forwarding, OAuth callback redirect/Cookie headers and CSV response streaming. Those Web tests use mocked upstream responses.
- Current Server tests cover owner and API-key project/shared restrictions, revoked keys, Cookie Origin protection, cross-owner hiding, stale `If-Match`, create idempotency, exact minor-unit money, project-to-shared moves, soft deletion and audit, per-currency reports, filters, pagination and CSV output. Creem tests cover signed webhook receipt replay, checkout/subscription mutation and payment projection. These use a synthetic SQLite-shaped D1 fixture, not a live Worker or provider.
- Removed retired Item/Watch response branches from `cloudflare/server/src/entry.py`. `/health` now checks active ledger/identity/billing tables without depending on historical processing-usage tables; the new test failed first, then passed.

## Local commands and results

- Explicit `pytest` over all existing `tests/test_*.py` except preexisting untracked `tests/test_jev_sdk_child_adapter.py`: **139 passed**.
- `python3 cloudflare/server/sync_server_modules.py --check`, packaged Worker-module import, `python3 scripts/check-ledger-s01.py --allow-placeholders`, `bash -n scripts/deploy-cloudflare.sh`, and `git diff --check`: passed. Fresh migrations and Worker health are exercised by `tests/test_ledger_deploy_contract.py` against SQLite.
- Both `bash scripts/deploy-cloudflare.sh --environment preview` and `--environment production` rejected placeholder domains/D1 IDs before invoking Wrangler. No `--execute` command was run.
- CI status was unavailable locally (`gh` absent); no GitHub Actions status is claimed.

## Open acceptance and next machine

The Python `CsvExport` async generator to JavaScript `ReadableStream` bridge has **not** run in real workerd. Cloudflare Python FFI, D1 transaction/batch behavior, OAuth/App, Creem and Jev provider behavior remain unverified. No remote migration, preview Worker or deployment was run. S17 must stay open until the local workerd check and any resulting fixes pass; the Web proxy CSV test proves only JavaScript response forwarding.

S18 preview is deferred at the developer's request. The next machine needs reviewed preview Server and Web domains/routes, distinct preview D1 ID, Wrangler credentials, GitHub OAuth/App callback configuration, Creem test products and secrets, and the reviewed migration/rollback plan. Keep Jev suggestions off unless the real anonymized evaluation gate is met. Start with the S17 workerd CSV check, then follow the design's S18 gate before any remote command. The unrelated untracked Jev child test must remain untouched.
