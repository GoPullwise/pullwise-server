# Pullwise Server Worker

`src/entry.py` is the Cloudflare Python Worker for the project expense ledger. It routes GitHub sign-in and App authorization, workspace membership and invitations, account API keys, Creem subscription and webhook requests, multi-repository `/api/v1` ledger resources, per-currency reports, paginated CSV export, and optional Jev suggestions.

The new workspace/Organization/multi-repository version is implemented locally
as of 2026-10-06. Final verification, native schema-upgrade proof and preview
publication are pending. Original-version preview evidence does not validate
this release.

## Source and database

`sync_server_modules.py` copies only Worker-reachable `pullwise_server` modules into `src/pullwise_server`; run it after changing Server source and run `--check` in CI. Do not edit the mirrored files directly.

Migrations apply in order: `0001_ledger.sql`, `0002_identity_billing_keys.sql`,
`0003_ledger_suggestions.sql`, `0004_ledger_plan_usage.sql`,
`0005_workspaces_repositories.sql`. Production remains unmigrated and paused;
the existing preview uses the original four-migration schema. The new canonical
schema has 18 tables and 33 SQLite indexes. Health requires all 18 tables.
The deploy script never applies remote migrations.

0005 appends `workspace_members`, `workspace_invites`, `workspace_events` and
`ledger_project_repositories`, plus project `name` and
`github_organization_id` columns. It backfills each original repository binding
to the same project ID. Existing ledger owner IDs become workspace IDs with an
implicit Owner; no expense, audit, idempotency or finance history is rewritten.
Do not modify migrations 0001–0004 or import preview data into production.

Fresh preview initialization and the exact legacy-v4-to-v5 upgrade are distinct
compiled plans. The existing preview must use its binding-only, one-shot upgrade
under the same ValidationBudget journal, database, counters and 100,000-read /
1,000-write ceilings. It verifies the exact legacy schema and bounded project
backfill, then executes 0005 atomically. The versioned preview upgrade flag
explicitly enables this before the first eligible application request under
the existing DO lock; ordinary product SQL cannot submit migration DDL.
Partial, unknown or stopped outcomes retain reservations and cannot be retried
or reset. Local native SQL/budget measurement has passed with 324 reads/25 writes;
its Miniflare metadata has absent attempts and uses pinned no-retry source
provenance without inventing values. Deployed read phases still require native
attempts=1; the finite preview upgrade is the final runtime gate. Current bounds
are in [D1 validation](../../docs/validation/d1-validation-budget.md).

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
