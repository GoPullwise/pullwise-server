# Pullwise Server Worker

`src/entry.py` is the Cloudflare Python Worker for the project expense ledger. It routes GitHub sign-in and App authorization, account API keys, Creem subscription and webhook requests, `/api/v1` ledger resources, per-currency reports, paginated CSV export, and optional Jev suggestions.

## Source and database

`sync_server_modules.py` copies only Worker-reachable `pullwise_server` modules into `src/pullwise_server`; run it after changing Server source and run `--check` in CI. Do not edit the mirrored files directly.

Migrations apply in order: `0001_ledger.sql`, `0002_identity_billing_keys.sql`, then `0003_ledger_suggestions.sql`. Preview and production use different D1 databases. The health check requires the ledger, identity, key, billing and suggestion tables.

## Configuration

`wrangler.preview.jsonc` and `wrangler.production.jsonc` intentionally contain placeholder database IDs, domains and GitHub App slugs. Replace these with reviewed environment-specific values. Supply `PULLWISE_GITHUB_CLIENT_ID`, `PULLWISE_GITHUB_CLIENT_SECRET`, `PULLWISE_GITHUB_TOKEN_KEY`, Creem credentials and `TYPESAFE_API_KEY` only through Cloudflare Secrets. Keep `PULLWISE_JEV_SUGGESTIONS_ENABLED=0` and `PULLWISE_JEV_SUGGESTIONS_EVALUATED=0` until a real anonymized offline sample meets the evaluation gate.

The Web Worker removes its first `/api` prefix. The browser sends `/api/api/v1/*` on the production host, and this Worker receives `/api/v1/*`. The OAuth callback is the Web `/api/auth/github/callback` proxy to this Worker's `/auth/github/callback`.

## Checks and deployment

From the Server repository root:

```bash
python3 scripts/check-ledger-s01.py --allow-placeholders
python3 cloudflare/server/sync_server_modules.py --check
bash -n scripts/deploy-cloudflare.sh
```

The [local acceptance record](../../docs/validation/local-acceptance.md) records all current tests and outstanding runtime checks. All Wrangler/workerd and D1 commands, including local probes, are paused until explicit user authorization. Do not add cron triggers. `scripts/deploy-cloudflare.sh` prints intended steps by default and requires explicit execution for remote D1 migration and Worker deployment. Production needs separate review of migration, config and rollback.
