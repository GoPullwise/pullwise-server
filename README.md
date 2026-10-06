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

The current user request authorizes checks, fixes, main pushes and Cloudflare
publication. Preview is active behind the existing cumulative 100,000-read /
1,000-write journal; production D1 access remains paused. Do not reset that
journal, add cron triggers or copy preview credentials/data into production.
Keep remote validation finite and record its row reservations and actual results.
Publication is separate from authenticated runtime/provider acceptance. See the
[deployment guide](cloudflare/server/README.md) and current acceptance record for
release commands and remaining gates.
