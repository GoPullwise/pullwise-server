# Pullwise Server Worker

`src/entry.py` is the Cloudflare Python Worker for Pullwise, the project expense ledger for developers and teams at [pull-wise.com](https://pull-wise.com). It routes GitHub sign-in and App authorization, workspace membership and invitations, account API keys, Creem subscription and webhook requests, standalone and optionally repository-linked `/api/v1` ledger resources, per-currency reports, paginated CSV export, and conditional Jev assistance. User-entered ledger expenses are separate from Pullwise subscription payments; no bank or vendor transaction import or currency conversion is provided.

Named projects can operate without a GitHub repository or Organization.
GitHub account sign-in remains the application identity. Repository
authorization is required for explicit repository associations and for adding
or moving expenses into linked projects; standalone projects do not require it.
The blank-project implementation, native role/browser checks and controlled
preview schema upgrade are recorded in
[latest acceptance](../../docs/validation/local-acceptance.md).

## Source and database

`sync_server_modules.py` copies only Worker-reachable `pullwise_server` modules into `src/pullwise_server`; run it after changing Server source and run `--check` in CI. Do not edit the mirrored files directly.

Migrations apply in order: `0001_ledger.sql`, `0002_identity_billing_keys.sql`,
`0003_ledger_suggestions.sql`, `0004_ledger_plan_usage.sql`,
`0005_workspaces_repositories.sql`, `0006_blank_projects.sql`,
`0007_recurring_expenses_project_links.sql`.
Production remains unmigrated and paused. The current canonical v7
schema has 20 tables and 40 SQLite indexes. Coordinated initialization and
upgrade verify the full schema; health retains its 18-table compatibility probe.
The deploy script never applies remote migrations.

0007 adds optional development/product URLs and recurring rule/occurrence
records, and permits the `schedule` audit actor. Its preview-only one-shot v6-to-v7
upgrade uses 13 statements in one guarded atomic batch under the existing
database, coordinator and journal. It preserves actual expenses and cumulative
accounting. Scheduled execution refuses an unready schema rather than migrating
it. See the [feature contract](../../docs/planning/recurring-expenses-project-links.md)
and [native evidence](../../docs/validation/recurring-links-local-2026-10-08.json).

0006 makes the project GitHub anchor nullable. A standalone project has a
nonblank name, NULL repository/full-name/Organization and no repository rows.
The v5-to-v6 upgrade rebuilds only the project parent table in one atomic D1
batch with deferred foreign-key checks; financial and audit child rows retain
their IDs and contents. A separate preview-only versioned flag admits this
one-shot plan under the original database, coordinator namespace and journal.
Exact schema, typed-record counts and foreign-key integrity are checked before
and after dispatch. Reservations remain intact on unknown outcomes, with no
automatic replay. Current local proof covers populated upgrade, actual restart
without migration replay and injected rollback after dropping the old parent.

0005 appends `workspace_members`, `workspace_invites`, `workspace_events` and
`ledger_project_repositories`, plus project `name` and
`github_organization_id` columns. It backfills each original repository binding
to the same project ID. Existing ledger owner IDs become workspace IDs with an
implicit Owner; no expense, audit, idempotency or finance history is rewritten.
Do not modify migrations 0001–0004 or import preview data into production.

Fresh preview initialization and the exact legacy-v4-to-v5 upgrade are distinct
compiled plans. The existing preview must use its binding-only, one-shot upgrade
under the same ValidationBudget journal, database and counters. The completed
upgrade retained its historical 100,000-read / 1,000-write ceilings; the latest
ordinary product operation policy retires those lifetime test gates while
preserving all their evidence. The upgrade verifies the exact legacy schema and bounded project
backfill, then executes 0005 atomically. The versioned preview upgrade flag
explicitly enables this before the first eligible application request under
the existing DO lock; ordinary product SQL cannot submit migration DDL.
Partial, unknown or stopped outcomes retain reservations and cannot be retried
or reset. Local native SQL/budget measurement has passed with 324 reads/25 writes;
its Miniflare metadata has absent attempts and uses pinned no-retry source
provenance without inventing values. Deployed read phases still require native
attempts=1; the finite preview upgrade passed that runtime gate. Current bounds
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
reserves SQL bounds before dispatch, records native usage and retains all
cumulative counters. Enabled normal preview product traffic removes the
historical lifetime request/read/write test gates; generic finite validation
retains them. Schema, SQL, input, per-batch/request bounds and commercial owner
quotas remain enforced. New evidence appends within the same DO SQLite journal;
unknown/incomplete native outcomes remain stopped and there is no reset endpoint.
The DO-only `/_preview/budget` status distinguishes current product operation
mode from the retired numeric ceilings. Preview CSV uses a 24 MiB UTF-8 byte
spool; an oversized export returns 413 with filtering guidance and keeps the
journal healthy. See [D1 policy](../../docs/validation/d1-validation-budget.md)
for accounting and failure isolation.
Preview abuse admission also uses hashed, expiring DO-only IP/credential/actor
buckets and returns 429 plus `Retry-After`. Authenticated actor rates are 120
reads/minute and 60 ordinary write attempts/minute across sessions and keys;
commercial Free/paid allowances still apply. A verified cached cardinality
snapshot removes repeated table-count scans on healthy reads; all mutations
continue to verify and persist their new counts. The user's roughly USD 200/month
total Cloudflare target does not introduce a hard day/month row budget.
The capacity follow-up stores users, sessions, OAuth states and billing
event/pending facts at exact `record:<kind>:<id>` keys in the existing
`app_state` table. This storage cutover preserved the then-current v5 schema;
the later blank-project upgrade advances it to v6, followed by the current v7
links/recurrence upgrade. Under the
same coordinator lock, one bounded atomic batch copies legal legacy facts and
empties their former containers, retaining all account/session fields, receipts,
revisions and journal counters. A completed `stateStorageVersion: 1` status
prevents replay; partial/unknown outcomes stay blocked. Typed user snapshots
allow 512 KiB for retained repository/billing history; small records and HTTP
ingress remain 8 KiB. Account/auth readers use exact primary keys. Successful
mutations refresh numeric physical/kind counts, while strict typed writes and
cutover establish the record payload invariant. Direct console/import writes
bypassing these paths are unsupported. Finite native cutover, restart and
capacity acceptance precedes publication of this follow-up.
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
inventing prices/products. Jev enable/evaluated flags are enabled in Preview
after its recorded quality/runtime acceptance and remain off in production.
Its optional `TYPESAFE_API_KEY` is not needed while disabled. Max entitlement
alone does not establish assistance availability or remaining budget.

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
The current request authorizes these checks and deployments and exactly the
preview-only hourly `0 * * * *` recurring-expense trigger. Production retains
paused D1 access and no cron. The [acceptance record](../../docs/validation/local-acceptance.md)
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
