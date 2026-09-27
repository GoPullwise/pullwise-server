# S01 — Server contract and deployment layout

Status: **complete for S01**, 2026-09-27. The current Worker still serves the old product slice; this stage creates the target contract and deployable layout without routing ledger endpoints.

## Changed files and decisions

- `openapi/ledger-v1.yaml`: draft `/api/v1` ledger contract for projects, shared expenses, categories, reports, export and optional suggestions. Cookie and Bearer clients share DTOs; operation scopes are explicit. Existing `openapi/product-v1.yaml` remains current-runtime evidence until replacement.
- `cloudflare/server/migrations/0001_ledger.sql`: new owner-scoped project, category, expense, audit event and idempotency tables. Platform billing and identity tables stay separate. Composite foreign keys prevent expense references across owners; a CHECK enforces project versus shared target. Soft deletion is represented by `deleted_at`, with partial active-record indexes.
- `cloudflare/server/wrangler.preview.jsonc` and `wrangler.production.jsonc`: separate Worker names, custom domains and D1 names, common `DB` binding and `migrations` directory. Domains and database UUIDs are deliberate placeholders. Secrets belong in Cloudflare Secrets, never Wrangler `vars`.
- `scripts/check-ledger-s01.py`, `scripts/deploy-cloudflare.sh`: offline contract/config validation and a dry-run deployment wrapper. The wrapper rejects placeholders and missing local-check acknowledgement before remote migration or deploy. It applies `wrangler d1 migrations apply DB --remote --config ...` before `wrangler deploy --config ...` only with `--execute --local-checks-passed` after all checks and real values are supplied.
- `pyproject.toml`: `deployment` optional dependency installs PyYAML for the static OpenAPI parser (`python3 -m pip install -e '.[deployment]'`).
- `tests/test_ledger_deploy_contract.py`: guard and environment separation tests. `AGENTS.md` records the durable contract/deployment rule.

## Local verification

- Test-first evidence: the two deploy guard tests failed while the script was absent, then all three tests passed after implementation: `python3 -m unittest tests.test_ledger_deploy_contract -v`.
- `python3 scripts/check-ledger-s01.py --allow-placeholders`: passed, including YAML parse, component references, complete SQL statement and both config shapes.
- `bash -n scripts/deploy-cloudflare.sh` and `python3 -m py_compile scripts/check-ledger-s01.py`: passed.
- `sqlite3` in-memory `executescript` parsed the migration and created five intended tables. This was a local SQL syntax check, not a Wrangler/D1 test.
- `git diff --check`: passed at the time of handoff.
- CI: `gh` is not installed; the GitHub Actions page was unavailable from this environment. No CI result was available for these uncommitted changes.

## Remaining risks and next entry

Cloudflare real testing was **not run**. The configuration cannot deploy while placeholder IDs/domains remain. The current Worker lacks the full OAuth/Creem/ledger target endpoints; migration tables do not yet replace its existing identity/payment storage. Currency exponent, date validity, archived-category policy, GitHub access, transaction guards and API key target restrictions are application responsibilities in later stages. The draft OpenAPI is not yet wired to a generated client.

Proceed only when the developer requests S02 or another stage. S02 starts in `pullwise-web`: validate the Web Worker proxy and new API path assembly, add guarded deployment/local checks, then write `pullwise-web/docs/handoffs/S02-<name>.md`. Do not run Cloudflare commands before the S18 gate.
