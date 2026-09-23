# Candidate Server Worker, local validation only

This is the first real Server HTTP entry candidate. It exposes `/health`,
authenticated `GET /api/v1/me`, `GET /api/v1/usage`, `GET /api/v1/watches`,
`GET /api/v1/usage/events` for owner successful-processing history,
session-only `GET /api-keys` for redacted owner API-key metadata,
session-only `DELETE /api-keys/{id}` for guarded revocation,
session-only `POST /api-keys` for one-time token issuance,
session-only `GET /billing` for product usage and saved payment history,
public `GET /billing/plan` from a fresh trusted D1 catalog projection,
`GET /api/v1/watches/{id}`,
`GET /api/v1/sources` and `GET /api/v1/sources/{id}`, and
`GET /api/v1/items`, `GET /api/v1/items/{id}`, `GET /api/v1/items/overview` and
`PATCH /api/v1/items/{id}` for handling, and
`PATCH /api/v1/watches/{id}` and `DELETE /api/v1/watches/{id}` for owner public
watch configuration/archive, and
`GET /api/v1/jobs/{id}` for requester-owned manual sync status, and
`POST /webhooks/creem`; other routes return 404 until the shared product-v1 REST
contract has been adapted. It has no probe/reset route,
no cron, no public route or Workers subdomain, and a synthetic `remote: false`
D1 binding. Do not deploy it or put real credentials or payment data into its
local state.

`src/entry.py` calls Server-owned `cloudflare_http_contract.py`. The latter
requires raw request bytes, checks the 64 KiB bound and signature before D1,
and returns the existing `{"received": true}` webhook ACK only after the
trusted D1 composition has committed or parked the verified receipt. A
post-settlement projection failure returns 503; exact provider redelivery can
complete the refresh without charging or applying payment twice.
The product GETs resolve persisted `pw_session` cookies or hashed Pullwise
API keys with the required read scope. Mixed identities, revoked/expired keys,
missing GitHub session tokens and audit-bundle restrictions fail closed. They
read saved entitlements, buckets and attempts without invoking GitHub/Jev or
writing D1. Profile, usage and watch reads recheck current identity in the
same D1 batch as their response rows; usage counts share that batch. The
usage-events page also shares that identity snapshot and exposes only consumed
owner rows with module/cursor/limit filters; charge keys stay private. The
legacy API-key list also rechecks Cookie/user in one batch, excludes revoked
rows and sends no-store headers. DELETE rechecks the exact Cookie session and
stored user in its D1 write batch, applies the SameSite=None Origin rule,
and returns 404 for a duplicate. POST stores only SHA-256 hash/prefix and
metadata in a guarded D1 batch; the random `pwk_` token appears only in the
201 response. Local Server and Worker share scope/restriction/public DTO rules.
Python Workers obtains the token's 32 random bytes from
[`crypto.getRandomValues`](https://developers.cloudflare.com/workers/runtime-apis/web-crypto/)
through its JS FFI; local CPython tests use `secrets.token_bytes`.
The existing local Server updates API-key `last_used_at` on access;
this candidate deliberately leaves it untouched on GET to avoid write charges.
A bounded operational last-used policy is still needed before migration.
Billing GET rechecks Cookie/user with usage bucket, consumed module counts,
owner-cycle attempts and the newest 20 successful processing events in one
read-only D1 batch. It shares the pure account/payment DTO with local Server,
preserves subscription history, sends no-store and rejects API keys. Public
plan GET never calls Creem: it requires a non-expired verified catalog row,
overlays shared product capacities, and adds a Cookie account from the same
D1 snapshot only when authority remains current. Missing/stale catalog
returns 503. The local fixture contains only synthetic disabled pricing;
trusted live catalog refresh and payment-provider mutations remain unported.
The watch list uses the same Server-owned DTO projection as ProductStore,
filters to the current billing owner, and applies API-key `watchIds` scope.
Source list/detail rechecks the API key or Cookie session and stored user in
the same read-only D1 batch as Source rows and publication/dependency fences.
It shares Source DTO and filter rules with local REST, including watch and
repository key restrictions. GET never writes usage or schedules model work.
Item list/detail uses the same identity proof and reads current ItemVersion,
source/context fences and handling events in one D1 batch. A stale secondary
source or permission revision hides the old Item. Handling PATCH requires
`itemVersion` and `If-Match`; one guarded D1 write batch commits the event and
revision together while rechecking identity and all dependencies. Overview
combines principal, Source and Item SELECTs in one D1 snapshot before counting.
Job GET rechecks identity and current watch/repository-service ownership with
the row and excludes `analyze_source`.
Public watch PATCH/DELETE recheck Cookie/API-key, stored user, owner, resource
restriction and revision in the read snapshot and guarded write batch. They
do not enqueue analysis. Private/shared watch writes and public-watch creation
are still unported.
Successful Item/watch detail and handling responses include revision `ETag`
for the shared If-Match contract.
When `PULLWISE_COOKIE_SAME_SITE=None`, Cookie Item/watch writes require an Origin or
Referer matching `PULLWISE_ALLOWED_ORIGINS` or `PULLWISE_APP_URL` before the
request body is read. The local probe used synthetic loopback values.
`/health` checks that the D1 tables required by the currently routed HTTP
slice exist; this is a narrow readiness check, not a full schema or CF2 gate.

## Local reproduction

Use the already installed Wrangler 4.136.3 and Python Worker modules from the
sibling isolated probe. The copy below reuses its pinned local dependencies;
it does not install or upgrade packages. Keep the Server source package in the
ignored candidate directory byte-identical:

```powershell
# From F:/Pullwise/pullwise-server
D:/Python313/python.exe cloudflare/server/sync_server_modules.py
D:/Python313/python.exe cloudflare/server/sync_server_modules.py --check
if (-not (Test-Path cloudflare/server/python_modules)) {
  Copy-Item -LiteralPath cloudflare/probe/python_modules -Destination cloudflare/server/python_modules -Recurse
}
$env:TEMP='F:/Pullwise/.test-tmp/discovery'
$env:TMP=$env:TEMP
D:/Python313/python.exe cloudflare/server/export_local_fixture.py
node cloudflare/probe/node_modules/wrangler/wrangler-dist/cli.js d1 execute pullwise-cf1-local-only --config cloudflare/server/wrangler.jsonc --local --persist-to cloudflare/server/.wrangler/server-http-catalog-state --file cloudflare/server/.wrangler/local-seed.sql
```

Start the Worker with **synthetic** test values and run its local HTTP driver:

```powershell
node cloudflare/probe/node_modules/wrangler/wrangler-dist/cli.js dev --config cloudflare/server/wrangler.jsonc --local --ip 127.0.0.1 --port 8797 --persist-to cloudflare/server/.wrangler/server-http-catalog-state --var='PULLWISE_CREEM_WEBHOOK_SECRET:synthetic-secret' --var='PULLWISE_CREEM_PRODUCT_IDS_JSON:{}' --var='PULLWISE_COOKIE_SAME_SITE:None' --var='PULLWISE_ALLOWED_ORIGINS:http://127.0.0.1:5173'
D:/Python313/python.exe cloudflare/server/verify_local_http.py --watch-only --delete-watch --same-site-none
```

Stop Wrangler and restart it with the same local persist directory, then run
the watch-only driver again; the two runs archive the two synthetic watches.
Read-only D1 inspection should find zero active watches, `reserved=0`, a
cancelled synthetic sync Job, revoked Source context, no provider attempt and
no API-key last-used write. Use a fresh ignored state directory before repeating the
two-run sequence.
For the usage-event cursor probe, seed another fresh ignored directory such as
`.wrangler/server-http-usage-cursor-state` and run
`verify_local_http.py --watch-only --same-site-none` before and after restart.
Its two historical consumed rows must page in order, reject cursor reuse under
another module, and leave provider attempts and key last-used unchanged.
The same watch-only driver checks session-only `/api-keys` redaction and
no-store headers before and after restart.
To check revocation separately, seed a fresh ignored directory and run
`verify_local_http.py --key-delete-only --same-site-none`, restart workerd,
then run `verify_local_http.py --key-delete-only --after-restart --same-site-none`.
For synthetic issue/use/revoke, seed a fresh ignored directory and run
`verify_local_http.py --key-create-only --same-site-none`, restart, then
`verify_local_http.py --key-create-only --after-restart --same-site-none`.
The fixture exporter
opens only temporary synthetic SQLite
data; it never opens an account database. Preserve existing `.wrangler` state
directories as local evidence.

## Remaining gates

- Repository, public-watch creation, private/shared watch mutations,
  visualization, sync and other
  product-v1 REST paths are not routed yet. Session issuance, OAuth/App
  lifecycle, bounded API-key last-used/rotation policy and complete authorization still need D1
  adaptation. Web and external clients must share the eventual Server REST.
- Real Creem secret and product-ID binding, checkout/account writer coverage,
  pending reconciliation invocation, receipt retention, encrypted account
  runtime, migration and remote Cloudflare validation remain open.
- Production GitHub ingestion and Jev are disabled. No Cloudflare cron may be
  configured or enabled for this project.
