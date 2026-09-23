# CF1 isolated local probe — 2026-09-23

This is a **local experiment, not the Server deployment or a production adapter**.
The user approved the isolated validation dependencies in this session. No real
GitHub/Jev credentials, model requests, remote D1 resources, deployments, DNS
changes, or main-project dependency changes were made.

## Reproducible environment

- Node 22.22.0; Wrangler 4.136.3 (package-lock.json).
- workers-py 1.17.4; workers-runtime-sdk 1.9.0.
- typesafe-sdk **0.7.0**, httpx2 2.13.0.
- The CPython probe venv uses the existing Python 3.13 installation (uv.lock).
- Current local workerd uses **Python 3.14.2 / Pyodide v314.0.7**, and bundles
  pydantic 2.12.5 using pylock.toml. This is not Server's Python 3.10 runtime.
- The bundler required uv 0.12.3; it is installed only in this probe's venv.
  Global uv remains unchanged. Its downloaded host interpreter was relocated
  into .python; .venv-workers points there. All generated runtime/vendor/cache
  directories are ignored. Do not commit them.

Use the probe-local tools and cache; never a floating npx upgrade:

~~~powershell
$env:PATH="$PWD\.venv\Scripts;$env:PATH"
$env:UV_CACHE_DIR="$PWD\.uv-cache"
$env:UV_PYTHON_INSTALL_DIR="$PWD\.python"
$env:PYTHONUTF8="1"
.\.venv\Scripts\pywrangler.exe sync --no-allow-build
node node_modules/wrangler/wrangler-dist/cli.js dev --local --ip 127.0.0.1 --port 8794 --test-scheduled --persist-to .wrangler/probe-state --show-interactive-dev-session=false
~~~

The config has no routes, workers.dev or preview URL, and a placeholder **local**
D1 ID with remote bindings disabled. Do not deploy it. The HTTP handler also
rejects non-loopback hostnames. The finite test operations touch only probe_*
tables; there is no arbitrary SQL endpoint.

## Observed results

Run in a separate terminal:

~~~powershell
.\.venv\Scripts\python.exe verify_local.py
# Stop and restart wrangler using the same --persist-to directory, then:
.\.venv\Scripts\python.exe verify_local.py --after-restart
.\.venv\Scripts\python.exe sdk_transport_probe.py
~~~

Passed on local workerd:

- Python request handler and SDK/httpx2/pydantic imports.
- D1 batch rollback after a deliberate CHECK failure.
- Exactly one admission when two owners race for one global slot.
- A zero-row conditional update is converted into a CHECK failure via a
  changes() guard; the entire batch rolls back, including the other scope.
- Scheduled handler writes its tick to D1.
- After stopping/restarting workerd, the consumed budget and scheduled tick remain.

Current scheduled test URL is /cdn-cgi/local/scheduled?format=json, not the
older /__scheduled text still present in Wrangler's CLI help. The Python
scheduled signature is (self, controller, env, ctx).

SDK public-transport experiment (**CPython loopback, not Workers network**):

- A normal synthetic response returns jev-1.13.0.
- A 300ms header delay raises TypeSafeAPITimeoutError with a 100ms timeout.
- A response emitting bytes every 25ms does **not** exit within the 2s call
  deadline despite the 100ms timeout. The supervising test process terminates
  and reaps the child. The deadline starts after imports/server startup.
- This demonstrates socket inactivity timeout is not a total-exit guarantee.
  Production Jev stays disabled. No real model was contacted.

## What remains unproved

### Additional local domain protocol probe (2026-09-23)

`verify_domain.py` exercises finite `/domain/*` commands in the same local-only
Worker. Run it against port 8794, actually stop/restart Wrangler with the same
persist directory, then run `verify_domain.py --after-restart`.

Observed on local workerd/D1: concurrent claim exclusion; expired executor
rejection after successor claim; config/auth fencing with reservation release;
atomic assessment/usage/job publication; rollback of earlier job/result writes
when the final reservation guard fails; retry rejection before its deadline;
and persisted retry_wait/deadline after a real process stop/restart. Replaying
a publication after discarding its response leaves exactly one result and one
successful unit (the stale claim is rejected, not acknowledged as a new success).

This is a protocol-shaped experiment, not imported ProductStore transactions
or a production D1 adapter. It uses a deterministic clock, synthetic assessment,
one job/reservation, and random synthetic claim tokens. The response-loss check
discards a completed response. A second local test closes the client socket
before receiving the response, then retries; the result/usage still settles
exactly once. This is not upstream provider response-loss injection.
Multi-source fences and account/Creem integration remain unproved.
No dependencies changed.

### Additional local budget and SDK transport probes (2026-09-23)

`verify_attempt_budget.py` uses synthetic owner/global limits and an actual UTC
month boundary (2026-09-30 23:59:45). A D1 batch admits both scopes and the
attempt event together; a failed owner/global monthly or rolling gate rolls all
of them back. It passed before and after a real workerd stop/restart with the
same persisted D1. The previous month's attempt remains in the rolling window
after the month changes. This remains separate from the domain-job claim batch;
the complete Server admission mapping is still unproved.

`sdk_workers_loopback.py` contacts only a synthetic service on local port 8795
through fixed `typesafe-sdk==0.7.0` running inside local Python workerd. The
original plain httpx2 transport accepted a byte every 25 ms and took 5.173 s
despite a 100 ms timeout. `src/bounded_transport.py` injects a public
`httpx2.BaseTransport`, streams decoded bytes, caps them at 1 MiB, checks a
synthetic 2-second total deadline, and closes the response on failure. Local
workerd observations: normal 0.059 s success, slow headers ~0.1 s timeout,
continuously slow body ~2.0 s timeout with peer disconnect confirmed, and
2 MiB body ~0.1 s rejection. The same Worker served a normal request after
each failure. No real SDK key or model call was used. This validates the local
mechanism; 90-second production configuration, real provider behavior, quality,
remote Cloudflare runtime and service integration remain gated.

No complete CF1/CF2 pass is claimed. Still required: account/Creem dependency and
runtime compatibility; real Server REST adaptation; D1 mapping of all domain
transactions; combined claim/admission with monthly and rolling two-scope
budgets; atomic publication/usage under cancellation and upstream response
loss; and production SDK network hard exit. These separate local experiments
do not prove the full mapping. Do not replace
ProductStore with a SQLite file in the Worker/Container filesystem.

The thread scan remains incremental: it traverses GraphQL for each REST comment
page, potentially repeating GraphQL pages. Known-open PR close reconciliation
and injected formal-review verification live in Server; semantic thread
aggregation and unseen-PR coverage remain separate work.

## Actual Server schema mapping continuation

The current generated Server mapping has an additional persisted
`period_start` column. Run the local actual-schema driver with
`--persist-to .wrangler/server-map-cycle-state` on port 8796; retain the
previous `.wrangler/server-map-state` directory as historical local evidence.
The driver now checks account-cycle owner attempt limits through Server tests,
first reservation, retry deadline, terminal release and replay after a real
restart. Fresh local Worker cold start can exceed 20 seconds; the driver uses
a 90-second request timeout. None of these results is remote validation.
It also checks active charge-key reuse and a released key's guarded
re-reservation. One local restart exited with a workerd disconnected error;
a subsequent restart with the same persisted D1 passed the replay check.
The local HTTP receipt route now reads request bytes/signature rather than
fabricating a signature inside the Worker. It uses a synthetic secret and
normalized update, verifies a 64 KiB Content-Length bound, and proves the
receipt/apply and first-generation queue-admission batches on local D1.
Do not treat this route as a production Creem endpoint or Server scheduler.
After `/server-map/schedule-enable`, the local Wrangler scheduled test URL
invokes the Server async due-job selector once, then disables that probe flag.
It claims one eligible synthetic Job with no model request; a second wake
does not spend another attempt. A separate stale-source wake terminates that
Job and releases its reservation without spending an attempt. This does not
integrate the real Server scheduler.

The synthetic fixture generator now copies Server-owned D1 mapping, async
batch/account adapter and pure entitlement-rule modules into an ignored local
Worker package. `/server-map/*` uses that adapter for account event, generic
account write, pending association and live entitlement refresh. Its account
snapshot contains encrypted synthetic GitHub tokens produced by Server
`state_for_storage`; no real key or account data enters the probe. Local D1
and restart checks are still isolated validation, not a real Creem/Server
runtime or CF2 completion.

See [the transaction map](../../docs/cloudflare-domain-transaction-map.md) for
the precise account/Creem boundary and remaining CF2 work. From Server, generate
the ignored synthetic `src/server_fixture.py` using
`tests/export_d1_server_fixture.py`; the generator never opens a user database.
Start the same pinned local Wrangler on port 8796 with
`--persist-to .wrangler/server-map-state`. Run `verify_server_mapping.py`, stop
and restart that process with the same persist directory, then run
`verify_server_mapping.py --after-restart`.

The finite `/server-map/*` routes execute prepared batches over actual Server
table definitions. Local D1 passed competing claim exclusion with combined
two-scope attempt admission, secondary-source and persisted-account fencing,
atomic assessment/ItemVersion/usage/job publication, final-guard rollback,
payment-fact preservation, and replay after a real workerd restart. The account
data and assessment are synthetic. Account encryption, Creem processing and
production entitlement integration remain unproved; this does not migrate
Server or pass CF2. No real account/payment data is loaded or changed.

Continuation: the synthetic mapping now also executes a finite accepted-event
batch over `app_state.users`, `app_state.billingEvents` and a monotonic owner
entitlement revision. A dirty or expired projection blocks claim/publication;
the claim freezes the revision. Local workerd/D1 proved duplicate-event rollback,
A→B→A rejection of an old revision, trusted reprojection, and restart persistence.
This tests the database boundary only: no real Creem webhook, encrypted user
payload or subscription runtime was run in workerd.

The original probe_* routes remain separate. Only this explicit local mapping
fixture uses product table names; do not mount any probe in the product router.

## Existing Web deployment, read-only inspection (prior session)

Cloudflare reported pullwise-web on pull-wise.com and www.pull-wise.com.
Active deployment: aa3d1f53-86a5-4a96-9f85-6060965d6af1, version
bc3fa061-c928-4314-bd47-6caa8f0416f9 at 100%, created 2026-09-08.
The local and remote API origin are https://api.pull-wise.com.
No pullwise-server Worker appeared in the inspected account. This session did
not publish the new Web implementation.

References:
[Python packages](https://developers.cloudflare.com/workers/languages/python/packages/),
[D1 Python binding](https://developers.cloudflare.com/d1/examples/query-d1-from-python-workers/),
[D1 batch semantics](https://developers.cloudflare.com/d1/worker-api/d1-database/),
[scheduled handler](https://developers.cloudflare.com/workers/runtime-apis/handlers/scheduled/).
