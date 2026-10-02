# Pullwise Server

Cloudflare Python Worker modules for the [GitHub project expense ledger](docs/design/github-project-ledger/README.md).
The Server owns GitHub identity/repository authorization, Cookie/API-key
security, project/shared expenses, categories, exact money, reports, paginated
CSV and optional suggestions. Creem subscriptions remain separate from expenses.

`cloudflare/server/src/entry.py` is the Worker entry. `pullwise_server/` owns
the implementation; `cloudflare/server/sync_server_modules.py` generates the
ignored Worker mirror. The [ledger OpenAPI](openapi/ledger-v1.yaml) is the shared
business contract.

## Offline verification

Use Python 3.10.12 with the deployment/test tools available:

```bash
python -m pytest tests
python scripts/check-ledger-s01.py --allow-placeholders
python cloudflare/server/sync_server_modules.py --check
bash -n scripts/deploy-cloudflare.sh
```

Tests use synthetic SQLite D1 fixtures. Current results and unverified runtime
behavior are in [local acceptance](docs/validation/local-acceptance.md).

## Configuration and deployment

Preview and production have separate `cloudflare/server/wrangler.<environment>.jsonc`
configs and D1 databases. `cloudflare/server/.dev.vars.example` lists local
Worker variable names; credentials belong in Secrets and must not be committed.
`scripts/deploy-cloudflare.sh` defaults to dry-run and rejects placeholder
domains/database IDs. Jev stays disabled until real-provider quality/runtime
gates pass. The user authorized self-authored synthetic en/zh evaluation samples;
their results must be recorded as synthetic-data validation.

All Wrangler/workerd and D1 commands, including local probes, remain paused
until the user explicitly resumes them. Do not add cron triggers. Before any
remote validation, review the request row/operation budget, request frequency,
pagination/cache policy, cost guard, migration and rollback. S17 runtime
verification and S18 remote acceptance are recorded separately from publication.
Both Workers have been published; production D1 access remains paused. See the
current acceptance record for deployed versions and remaining provider gates.
