# Pullwise Server

The target Server is a Cloudflare Python Worker for the [GitHub project expense ledger](docs/design/github-project-ledger/README.md). It keeps GitHub identity and repository authorization, API keys, and Creem subscription facts. Ledger expenses, reports and CSV exports are account-owned and separate from platform billing.

The deployed entry is `cloudflare/server/src/entry.py`. Its dependency mirror is generated from `pullwise_server/` by `cloudflare/server/sync_server_modules.py`. The Worker exposes GitHub OAuth and App callbacks, billing and webhook routes, `/api-keys`, and `/api/v1` projects, categories, expenses, reports and optional suggestions. The old local VM runtime, PR/CI/Updates modules and their historical tests have been removed. Local verification uses the Worker entry and its synthetic D1 test fixture; Cloudflare runtime behavior still needs separate acceptance.

## Local checks

Use Python 3.10.12 with the project installed in `.venv`. The focused Cloudflare ledger suite is configured in `.github/workflows/ci.yml`; run it locally with the same test list. Also run:

```bash
python3 scripts/check-ledger-s01.py --allow-placeholders
python3 cloudflare/server/sync_server_modules.py --check
bash -n scripts/deploy-cloudflare.sh
```

The untracked local test `tests/test_jev_sdk_child_adapter.py`, if present, imports a module outside the target Worker and prevents an unrestricted `pytest` collection. The target CI suite names current ledger, identity, key and payment tests explicitly.

## Deployment

`cloudflare/server/migrations/0001_ledger.sql`, `0002_identity_billing_keys.sql`, and `0003_ledger_suggestions.sql` define the target D1 schema. Preview and production use separate `wrangler.<environment>.jsonc` files and D1 bindings. `scripts/deploy-cloudflare.sh` is dry-run by default and rejects placeholder IDs/domains before executing any remote migration or deployment. Configure credentials as Cloudflare Secrets, never in JSONC or logs. Jev suggestions remain off until the offline evaluation gate passes.

The [ledger OpenAPI](openapi/ledger-v1.yaml) is the current contract. See `docs/handoffs/` for local verification and remote acceptance status.
