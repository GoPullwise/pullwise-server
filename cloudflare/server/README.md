# Candidate Server Worker, local validation only

This is the first real Server HTTP entry candidate. It exposes `/health` and
`POST /webhooks/creem`; other routes return 404 until the shared product-v1 REST
contract and authentication have been adapted. It has no probe/reset route,
no cron, no public route or Workers subdomain, and a synthetic `remote: false`
D1 binding. Do not deploy it or put real credentials or payment data into its
local state.

`src/entry.py` calls Server-owned `cloudflare_http_contract.py`. The latter
requires raw request bytes, checks the 64 KiB bound and signature before D1,
and returns the existing `{"received": true}` webhook ACK only after the
trusted D1 composition has committed or parked the verified receipt. A
post-settlement projection failure returns 503; exact provider redelivery can
complete the refresh without charging or applying payment twice.

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
node cloudflare/probe/node_modules/wrangler/wrangler-dist/cli.js d1 execute pullwise-cf1-local-only --config cloudflare/server/wrangler.jsonc --local --persist-to cloudflare/server/.wrangler/server-http-state --file cloudflare/server/.wrangler/local-seed.sql
```

Start the Worker with **synthetic** test values and run its local HTTP driver:

```powershell
node cloudflare/probe/node_modules/wrangler/wrangler-dist/cli.js dev --config cloudflare/server/wrangler.jsonc --local --ip 127.0.0.1 --port 8797 --persist-to cloudflare/server/.wrangler/server-http-state --var='PULLWISE_CREEM_WEBHOOK_SECRET:synthetic-secret' --var='PULLWISE_CREEM_PRODUCT_IDS_JSON:{}'
D:/Python313/python.exe cloudflare/server/verify_local_http.py
```

Stop Wrangler and restart it with the same local persist directory, then run
the HTTP driver again. A local read-only D1 query should still show revision 3
and `dirty=0`. The fixture exporter opens only temporary synthetic SQLite
data; it never opens an account database. Preserve existing `.wrangler` state
directories as local evidence.

## Remaining gates

- No session/API-key authentication, product-v1 REST reads or handling writes
  are routed here yet. Web and external clients must continue to share the
  eventual Server REST contract.
- Real Creem secret and product-ID binding, checkout/account writer coverage,
  pending reconciliation invocation, receipt retention, encrypted account
  runtime, migration and remote Cloudflare validation remain open.
- Production GitHub ingestion and Jev are disabled. No Cloudflare cron may be
  configured or enabled for this project.
