# Pullwise Server Worker

`src/entry.py` is the Cloudflare Python Worker for the project expense ledger. It routes GitHub sign-in and App authorization, account API keys, Creem subscription and webhook requests, `/api/v1` ledger resources, per-currency reports, paginated CSV export, and optional Jev suggestions.

## Source and database

`sync_server_modules.py` copies only Worker-reachable `pullwise_server` modules into `src/pullwise_server`; run it after changing Server source and run `--check` in CI. Do not edit the mirrored files directly.

Migrations apply in order: `0001_ledger.sql`, `0002_identity_billing_keys.sql`,
`0003_ledger_suggestions.sql`, `0004_ledger_plan_usage.sql`. They remain
unexecuted in production; preview has initialized the canonical schema behind
its existing journal. Preview and production use different D1 databases. Health
requires all 14 tables. The deploy script never applies remote migrations.

## Configuration

Preview targets `preview-api.pull-wise.com` and its isolated ledger database,
with test Creem product IDs/Secrets and the GoPullwise Preview GitHub App
(`gopullwise-preview`). Its OAuth and installation callbacks run through
`https://preview.pull-wise.com/api/`. Production uses
the approved `api.pull-wise.com` domain and its distinct, initially empty D1
database. Its GitHub App slug, required Secret names and product bindings have
been checked; provider validity and real flows remain unverified.
The production Worker `pullwise-server-production` uses the exact zone route
`api.pull-wise.com/*`; the existing proxied DNS record is retained. Web already
proxies to this origin. An empty database binding is not a completed migration.
Preview has `PULLWISE_D1_ACCESS_ENABLED=1` and
`PULLWISE_PREVIEW_PRODUCT_ENABLED=1`. Its fixed ValidationBudget journal
reserves SQL bounds before dispatch, with cumulative 100,000 reads / 1,000 writes
and no reset endpoint. The cumulative request-count gate was already removed.
Production retains `PULLWISE_D1_ACCESS_ENABLED=0`: requests receive
503 `D1_ACCESS_PAUSED` before DB/provider access. Missing/invalid switch values
also fail closed. This switch is a pause, not a metered quota.

Keep the nonsecret `PULLWISE_GITHUB_CLIENT_ID` in the environment config. Supply
`PULLWISE_GITHUB_CLIENT_SECRET`,
`PULLWISE_GITHUB_TOKEN_KEY`, `PULLWISE_CREEM_API_KEY` and
`PULLWISE_CREEM_WEBHOOK_SECRET` through Cloudflare Secrets. The GitHub token
key is 32 cryptographically random bytes encoded as unpadded base64url; reuse
it for existing encrypted data, or generate it for a fresh database. Never
paste Secrets into chat. The nonsecret `PULLWISE_GITHUB_APP_SLUG` identifies
`github.com/apps/<slug>`; the OAuth callback is
`https://pull-wise.com/api/auth/github/callback`. Configure the Creem webhook
as `https://api.pull-wise.com/webhooks/creem`. Preview acceptance uses Creem
test credentials/products, not live payments.

For this implementation, GitHub Client ID/Secret and App slug must identify
the same GitHub App: `/user/installations` consumes its user access token
([GitHub REST documentation](https://docs.github.com/en/rest/apps/installations#list-app-installations-accessible-to-the-user-access-token)).
Use the App's Client ID, not its numeric App ID. Set its installation Setup URL
to `https://pull-wise.com/api/integrations/github/callback`.

`PULLWISE_CREEM_PRODUCT_IDS_JSON` has this nonsecret shape:

```json
{"pro":{"month":"prod_pro_month","year":"prod_pro_year"},"max":{"month":"prod_max_month","year":"prod_max_year"}}
```

Use actual distinct product IDs; omit unavailable intervals rather than
inventing prices/products. Keep Jev enable/evaluated flags off; its optional
`TYPESAFE_API_KEY` is not needed while disabled.

The Web Worker removes its first `/api` prefix. The browser sends `/api/api/v1/*` on the production host, and this Worker receives `/api/v1/*`. The OAuth callback is the Web `/api/auth/github/callback` proxy to this Worker's `/auth/github/callback`.

## Checks and deployment

From the Server repository root:

```bash
python3 -m pytest tests
python3 scripts/check-ledger-s01.py --allow-placeholders
python3 cloudflare/server/sync_server_modules.py
python3 cloudflare/server/sync_server_modules.py --check
bash -n scripts/deploy-cloudflare.sh
cd cloudflare/server && npm ci && cd ../..
bash scripts/deploy-cloudflare.sh --environment preview --execute --local-checks-passed
```

The release script uses locked Python 3.14.2 / pywrangler packaging with the
locally pinned Wrangler, and preserves each environment's runtime switches.
It never runs migrations. The Worker uses the native bounded TypeSafe gateway;
the optional local SDK helper is not bundled as an unused runtime dependency.
The current request authorizes these checks and deployments. Do not add cron
triggers. The [acceptance record](../../docs/validation/local-acceptance.md)
separates local mocks, native runtime evidence, publication and real providers.

## Moving the verified release to production

Deploy the same reviewed source with `--environment production`. Keep production
paused until its schema, real GitHub App callbacks, live Creem products/Secrets,
signed webhook flow and end-to-end acceptance are verified. Review migration
counts and rollback before executing production SQL; the current database is
documented as empty/unmigrated, not as a copy of preview.

Use production's existing database, token-encryption key and provider identities.
Never promote preview sessions, test payment facts or test App credentials.
The Web production config binds `pullwise-server-production`; preview binds
`pullwise-server-preview`. Cookie callbacks and allowed origins must match each
Web host. Jev may be enabled only after the labeled en/zh and Worker transport
gates pass. Production activation remains separate from code publication.

Before each release, record the currently deployed version for rollback. If
runtime acceptance fails, restore that version without resetting the preview
journal or automatically retrying migrations, payments or uncertain model calls.
