# Server transaction mapping to local D1 — 2026-09-23

This is an executable storage-boundary experiment and a migration map. It is
not a deployed Server, a replacement ProductStore, or a CF2 pass. Production
Jev and live GitHub remain disabled. No account, credential, price, product ID,
callback or payment-provider configuration was changed.

## Tested boundary

`cloudflare/probe/src/server_mapping.py` emits finite prepared statements over
the actual Server tables. `tests/export_d1_server_fixture.py` creates a new
synthetic ProductStore and exports its schema/data into an ignored probe module;
it never accepts a database path or exports a user database. The same command
builders run under SQLite unit tests and local workerd D1 `batch()`.

Every command list is one batch. Conditional updates followed by CHECK guards
turn zero-row CAS into failure before commit. Cloudflare documents whole-batch
rollback on a statement failure in its [D1 binding API](https://developers.cloudflare.com/d1/worker-api/d1-database/#batch).
Separate awaits for the statements would invalidate the proof.

| Existing Server authority | D1 mapping | Current evidence / remaining work |
| --- | --- | --- |
| `background_jobs`, `source_records`, `source_contexts`, `processing_usage_ledger` | Frozen source/version/context/config/auth/owner and live reservation guard; current eligible job becomes running with a 120-second token | Local D1 competing claims admit one. Bounded due selection and stale-binding/cycle termination are separate guarded commands; full scheduler adaptation remains open |
| `provider_attempts` | Owner/global monthly and inclusive last-60-second counts plus the new attempt in the claim batch | SQLite tests show budget rejection rolls the job back; local D1 executes the combined command. Existing isolated monthly/rolling rollover tests remain separate evidence |
| `assessments`, `source_assessment_publications`, `items`, `item_versions` | Validate every source and context plus lease, then insert immutable result and CAS the current Item version | Local D1 rejects a changed secondary dependency and an expired/stale claim; current test result is synthetic, not model-quality evidence |
| `processing_usage_buckets`, `processing_usage_ledger`, `background_jobs` | Result, ItemVersion, reserved→used and succeeded state in the publication batch | Local D1 final reservation failure rolls back earlier result/version inserts. Replay after process restart cannot consume again |
| `app_state.users`, `account_entitlement_authority`, `d1_claim_authority` | Match the exact persisted account entry and monotonic entitlement revision, effective period and time limit before claim and publication; freeze the revision at claim | Local D1 rejects stale or dirty projection, expiry, A→B→A replay and a result from an older claim revision. Other account writers must participate in this protocol before production use |
| `app_state.billingEvents` | After the existing billing handler accepts a fact, the candidate batch updates that event record and the affected stored user, increments the entitlement revision and marks it dirty together | Synthetic event payload persists, duplicate event rejects atomically, and existing event/other-account records survive. This does not execute a Creem webhook in Workers |

## Account and Creem write boundary

Current `db.save_state`/`save_state_item` persist JSON state through
`state_for_storage`, which protects secret fields. `load_state`/`load_state_item`
restore it through `state_for_runtime`. A D1 adapter must preserve encrypted
records, account IDs, sessions, OAuth state, GitHub access, API-key hashes,
customer/subscription references and payment history. Loading empty product
tables is not an account migration.

The current Creem handler remains the authority for raw-body signature checking,
duplicate/late/pending event association and subscription lifecycle. Do not add
a second payment state machine to the Worker or to Web. Its accepted payment
fact must survive independently of a failed entitlement projection. Before an
analysis command can run, the projection must correspond to the current durable
payment/account revision and effective period; stale or absent projection must
deny admission, not reuse the old paid limit.

The existing account/history decision is now available from pure
`billing_account_rules.reduce_billing_update` with a fixed processedAt. The
legacy handler uses that same decision and keeps its local quota write. The
async D1 adapter can settle one persisted receipt for a matched owner using
the storage-form user JSON, retaining already encrypted token fields without
opening the key. It reads the receipt update, account revision and event/pending
snapshots, then fences receipt content, owner account snapshot, revision and
whole-map CAS in one D1 batch. A mismatched owner or changed receipt cannot
settle. This is local Worker evidence, not the real HTTP/Creem runtime.
An unmatched signed receipt can now be parked into the persisted pending list
with a receipt-content fence and whole-list CAS. A full list rejects rather
than dropping a signed payment update. A later trusted account association can
select at most 16 matching pending receipts by event-created order and settle
them one by one; each settlement removes its pending entry in the same batch
as account/event/receipt changes. Replays and concurrent pending writes are
tested. This is resumable event-driven mapping, not an enabled scheduler or a
complete account/Creem handler.

The Server package now has an async Creem composition function above these
transactions. It takes injected raw request bytes, signature, secret, product
ID bindings, D1 binding and time; applies the existing pure event rules;
persists the verified receipt; resolves a unique stored owner or parks the
event; then settles and refreshes the entitlement projection. It rejects
oversized input and ambiguous owners. If refresh fails after settlement, the
receipt remains applied with a dirty projection, and exact duplicate delivery
can finish the refresh without applying payment twice. A synthetic local
workerd HTTP route exercises this composition. The real Server HTTP route,
configured secret and production account writes remain unconnected.

An independent `cloudflare/server` Python Worker candidate now calls that
composition through an actual `/webhooks/creem` HTTP handler. The entry
retains raw bytes, validates length/signature before D1, returns the old
`{"received": true}` ACK only after a durable outcome, and returns a generic
503 on a retryable storage/settlement fault. `/health` is read-only and checks
for the account table. No reset/probe route or product-v1 substitute is
mounted. Local Wrangler 4.136.3 with the synthetic `remote: false` D1 verified
signature rejection, acceptance, replay and real process restart; account
revision remained 3 and projection clean. This is still not a remote Server
deployment or CF2 pass.
The candidate now also serves `/api/v1/me` and `/api/v1/usage` from persisted
Cookie sessions or hashed API-key rows with required read scopes. It rejects
mixed/expired/restricted credentials, projects usage from current D1 bucket,
ledger and owner-cycle provider attempts through the same pure entitlement
DTO builder as local Server, and performs no D1 batch write or model call on
GET. A synthetic local Workerd HTTP run and real process restart passed;
read-only D1 inspection found `last_used_at=NULL`, zero provider attempts and
unchanged payment revision on replay. This is a narrow read slice: full
session/API-key lifecycle, resource authorization and remaining product-v1
REST routes are not migrated.
It additionally serves owner-scoped `/api/v1/watches` using a shared pure
watch DTO function now called by both ProductStore and the D1 reader.
API-key watch restrictions are applied before returning rows; a key scoped
only to repositories sees no personal watches. A local workerd/D1 HTTP run
and process restart covered a full Cookie list and a one-watch restricted
list without a D1 write. Source/Item authorization and publication snapshots
remain separate CF2 work.
The next Source read mapping batches four SELECTs as one D1 read transaction:
currently authorized source/context rows, saved publications, source versions
and owner-context fences. It applies the same DTOs as ProductStore and drops
saved assessments when any secondary source or permission/context fence is
stale. Unit tests compare list/detail bytes with SQLite for PR and an
unclassified Release without an Item; local workerd/D1 also verified the
secondary-source invalidation. It is intentionally not routed as
`/api/v1/sources` yet: request identity and source rows must share the final
read snapshot to fence concurrent API-key/session revocation.

The local candidate now routes Source list/detail. Its seven-SELECT read batch
prepends current API-key, sessions and stored-user rows to the four Source
queries. A preliminary principal identifies the owner for bounded queries;
the final batch rechecks identity, scope, expiry and stored user before
returning rows, so a revocation between those steps cannot disclose content.
The same pure Source filters and API-key restrictions serve SQLite REST and
the candidate. The batch is read-only; local workerd HTTP passed before and
after process restart with synthetic D1. This does not cover all account
writers, GitHub authorization freshness, Item/handling, or remote CF2.

The candidate next routes Item list/detail through a six-SELECT read batch:
three current identity SELECTs plus current ItemVersion, readable source/context
and handling SELECTs. Shared SQLite/D1 projection requires a matching source
version/revision and context/authorization revision for every ItemVersion
dependency. A config-only analysis-off change retains the saved historical
judgment as required by design 02; publication writes still CAS config revision.
Missing or stale source/permission fences hide Item content.
Local workerd HTTP passed on synthetic D1 before and after process restart;
handling writes and overview remain unmapped.

The candidate Item handling PATCH now reads an authorized Item and latest
handling event, then performs one D1 write batch: guarded Item revision update,
`changes()=1` CHECK, handling event insert and guard cleanup. The UPDATE
rechecks the exact stored user, Cookie/session or API-key state, ItemVersion,
all source versions and context/authorization revisions. A config-only
analysis-off change does not block handling a saved Item; model publication
still CASes configuration revision.
Failure rolls back revision and event. Local tests revoked an API key before
the write batch and observed no partial handling; local workerd HTTP passed
valid and stale If-Match before and after process restart. This does not
establish remote Cloudflare transaction validation.

Item overview composes three principal, four Source and three Item SELECTs
in one read-only D1 batch. It validates identity first, projects each saved
resource with its publication/permission fences, then applies the shared
resource filters and counts. A separate first read of principal is used only
to bind the owner; revocation between it and the final batch is denied.
The synthetic local HTTP driver passed before and after workerd restart in a
fresh `server-http-overview-state`. Reusing an older local state returned an
empty Source list after its 300-second authorization lease expired; no lease
was extended for the probe.

The candidate sync-Job detail GET adds the Job row to the same D1 batch as
its current API-key/session/user proof. It returns only a Job requested by
that user with type `sync_repository` or `sync_watch`; analysis Jobs remain
inaccessible on this route. It performs no write or model work. The synthetic
local HTTP driver passed before and after restart in `server-http-jobs-state`.
The subsequent Job read also checks current watch archive/owner state or
active repository-service owner state in that batch. Archived watch status is
hidden. Member repository sync remains unmapped and is denied locally until
current repository authorization can be proven. This passed the synthetic
`server-http-job-fence-state` restart probe.

Profile, usage and watches now also append their response SELECTs to the
three current principal SELECTs in one D1 read batch. Usage's bucket, module
ledger counts and provider attempts no longer straddle separate awaits.
Tests revoked an API key immediately before each batch and a Cookie session
before the usage batch; all returned 401. A fresh local workerd HTTP run
passed before and after restart in `server-http-read-snapshot-state`. Direct
read-only inspection of that synthetic D1 file found zero provider attempts,
NULL API-key last-used and only the two expected handling events from the
HTTP driver's explicit PATCHes.

Watch detail GET uses the same batch-local principal and watch list snapshot
as the owner watch list, then selects the requested ID after API-key watchIds
restriction. Local REST now exposes the matching path. The synthetic workerd
HTTP driver passed before and after restart in `server-http-watch-detail-state`.

The next local watch write mapping adds a trusted resolved-public-upstream
create command. It reads the storage-form account and reuses
`entitlements_for_user` for the active-watch limit. One D1 batch checks the
exact user snapshot, live active count, unique scope and prior watch control,
then advances/inserts watch_controls, preserves processing_controls and
inserts update_watches. SQLite tests covered A→B→A monotonic versions,
duplicate active scope, concurrent last-slot and account-change rollback.
The isolated `/server-map/watch-create` local workerd probe passed create,
duplicate rejection and restart persistence in fresh
`server-map-watch-state`. This command does not verify GitHub public/private
status and is not mounted at product `POST /watches`.

Read-side archival fencing was tightened in both ProductStore and D1 readers.
An archived watch hides its Source and Item rows even before auth lease expiry;
a publication with an archived secondary watch dependency loses its saved
assessment. The write-side D1 archive transaction must still cancel queued
jobs and release reservations atomically before `DELETE /watches` is routed.
The trusted local `D1WatchTransactions.archive_watch` now performs that batch:
it guards owner/revision and reservation-bucket consistency, archives the
watch, decrements reserved buckets, releases ledger rows, cancels active
analysis/manual-watch Jobs, revokes contexts/targets and clears the guard.
An inconsistent bucket rolls back archival and all preceding writes; a running
Job's late result stays fenced and its provider attempt remains spent. The
product DELETE route remains unmounted while request/account authorization
composition is incomplete.
The SQLite reference archive path now mirrors the domain cascade within one
`BEGIN IMMEDIATE` transaction. Recreating the stable watch scope requires a
new authorization revision after archive; the discovery fixture renews proof
monotonically. D1 and SQLite still need target-runtime request-auth composition
before the product DELETE route can be exposed.

Trusted public-watch update now has a separate finite D1 batch. It checks the
persisted owner snapshot, current watch/control revisions and active limit
when enabling; updates semantic contextVersion only when interests change;
increments watch configuration revision; cancels queued/retry_wait analysis
Jobs with atomic ledger/bucket release; and updates watch-linked context and
discovery fences. Running Jobs keep their claim/attempt but cannot publish
under the new fence. SQLite `ProductStore.update_watch` now cascades to linked
contexts even when no discovery target was recorded. The isolated local
workerd probe passed A→B→A, queued release and restart under
`server-map-watch-update-state`. The product PATCH route remains unmounted.

The independent candidate now mounts PATCH and DELETE only for an owner's
public watch. Preflight reads current Cookie/API-key, stored user and watch
in one batch; the mutation passes that proof into the D1 write batch, which
rechecks the exact session/key and account snapshot with revision/limit and
domain guards. A revoked key between batches rolls back update/archive.
Cookie writes with SameSite=None require a trusted Origin before body read;
DELETE reads no body and returns 204. Local workerd/D1 exercised PATCH ETag,
stale If-Match, untrusted Origin and DELETE before and after process restart
under `server-http-watch-patch-state`. One cold-start timeout and a transient
local ProxyWorker connection loss occurred; retry after readiness passed.
Private/shared watch mutations and public create remain unmounted.
The next synthetic HTTP fixture bound the queued analysis Job and one Source
context to the first watch. Two watch-only PATCH/DELETE runs separated by a
real process restart passed in `server-http-watch-cascade-state`. Read-only
local D1 inspection found zero active watches, zero reserved units, cancelled
sync Job, inaccessible Source context, zero provider attempts and NULL API-key
last-used. The old Source lease expired during earlier cold-start trials; this
watch-only proof does not claim a full Source/Item HTTP rerun.
Successful-processing history now has shared `product_usage_events.py`
projection and keyset cursor rules. SQLite REST and candidate
`GET /api/v1/usage/events` list only the owner's consumed ledger; the Worker
adds current principal SELECTs to the ledger page SELECT in one read-only
D1 batch. The synthetic local workerd driver passed historical usage read
before and after restart in `server-http-usage-events-state`; direct D1
inspection found one consumed event, zero provider attempts and NULL key
last-used. A transient local ProxyWorker connection loss on the first PATCH
after restart cleared on retry; this did not alter the usage event read.
The later `server-http-usage-cursor-state` fixture had two historical consumed
rows. Real local workerd returned two stable pages, rejected the same cursor
under another module with 422, and passed again after a process restart.
Read-only D1 inspection found two consumed events, zero provider attempts and
NULL key last-used. One first PATCH hit transient local ProxyWorker connection
loss; the subsequent watch-only run passed.
The candidate's legacy account `GET /api-keys` now uses current session/user
SELECTs and owner API-key rows in one read-only D1 batch. A shared pure DTO
projects metadata without token or hash; revoked rows are excluded and the
HTTP entry emits the existing no-store headers. The local workerd watch-only
driver checked this before and after restart using synthetic credentials.
Creation/revocation, login/OAuth and full account migration are still open.
The candidate subsequently mapped Cookie-only API-key revocation. Its guarded
D1 batch checks the exact persisted user and sessions map before updating one
owner key, then forces a nonzero-row CHECK; a revoked Cookie between reads and
write rolls back. Local workerd/D1 passed untrusted Origin rejection, durable
revocation, bearer rejection and restart replay under
`server-http-key-delete-state`. Direct local D1 inspection found revoked=true,
lastUsed=NULL, zero provider attempts and unchanged entitlement revision 1.
Key creation and complete account/OAuth integration remain open.
The local Billing/Pricing read now uses product entitlements and product
processing ledger rather than scan quota, while retaining provider/customer/
subscription facts and payment history. The candidate Python Worker still
does not expose `/billing` or `/billing/plan`; equivalent account DTO reads
must join the same persisted entitlement/usage authority without a D1 write.
Checkout, subscription changes and all real payment writers remain separate
CF2 transaction work, not covered by the local Billing page tests.
The independent local Server Worker now routes Cookie-only `GET /billing`.
It batches current session/user, entitlement-cycle usage bucket, consumed
module counts, provider attempts and the latest 20 consumed events in one
D1 snapshot. The pure `product_billing_projection` keeps account/payment
fields and subscription history identical to local Server; no API key can
read Billing. Synthetic local workerd/D1 returned the expected Pro limits,
reserved usage and two historical processing events before and after a real
restart in `server-http-usage-cursor-state`. One cold-start request timed out
at 30 seconds, then the ready Worker returned the same result. Public plan,
checkout and production account writers are still unported.
The candidate now also reads a trusted `billing_public_catalog` D1 row for
`GET /billing/plan`. Its expiry/revision and complete three-plan shape gate
the response; current product entitlements are overlaid from the shared pure
rules. Anonymous requests receive only public pricing. A Cookie request adds
account/payment history using the same D1 batch, and a revoked Cookie loses
that private addition. No GET contacts Creem or writes D1. The local fixture
contains synthetic disabled prices only. Local workerd passed anonymous and
Cookie reads before/after restart in `server-http-catalog-state`. One first
restart read exceeded the 30-second local cold-start deadline, then passed
after readiness. A trusted provider catalog updater and real price/source
binding remain CF2 work.
Trusted local `D1BillingCatalogTransactions.stage_verified_catalog` now
publishes a complete three-plan public catalog under monotonic sourceRevision
with one D1 CAS batch, bounded expiry and no-op identical replay. Local
SQLite tests covered stale/concurrent revision rejection; the isolated
`server-map-catalog-state` workerd/D1 probe passed revision 1→2, stale
rejection and restart. The caller still must verify real Creem product IDs,
amounts/currency and configured status before staging; no refresh schedule,
secret or production price was connected.
An injected-product verifier now checks configured product ID equality,
active recurring `every-month`/`every-year`, positive integer cent prices,
unique plan/interval bindings and one currency before building the public
catalog. It follows Creem's [product entity fields](https://docs.creem.io/skills/creem-api/REFERENCE)
without making a live request. `D1BillingCatalogTransactions.stage_from_products`
composes that verifier with monotonic CAS. Four pure failure/success tests and
the isolated local workerd revision/restart driver passed using synthetic
products in `server-map-catalog-verified-state`. Real configured IDs,
credentials and provider fetching are still disconnected.
`product_public_catalog_rules` now supplies one pure completeness/expiry/
entitlement projection to both trusted writer and GET reader. It was checked
against local Billing's Pro/Max public price DTO for the same synthetic
product facts; the isolated candidate Worker still returned the expected
anonymous/Cookie catalog after exact module sync and a real restart under
`server-http-catalog-state`.
Trusted session issuance/revocation has a separate `D1SessionTransactions`
mapping. It reads the storage-form user and sessions map, then performs an
exact-snapshot CAS with a `changes()=1` guard so a concurrent session cannot
be lost. Revocation preserves unrelated sessions. Synthetic SQLite tests
covered usable and revoked Cookie reads and concurrent map mutation; local
workerd/D1 passed issue, duplicate rejection, real restart and revoke in
`server-map-session-state`. This is not the OAuth callback or public login
route; the caller must verify identity and generate a secure unique session ID.
`D1OAuthStates` adds a separate trusted map-CAS for GitHub authorization state.
It bounds expiry to ten minutes, permits only known state kinds, and consumes
one state exactly once across process restart/concurrent callbacks. Two local
SQLite tests and the combined `server-map-oauth-state` workerd probe passed
issue, duplicate rejection, restart, single consume and replay rejection.
Real OAuth code exchange, App installation authority, session cookie response
and production credentials remain unconnected.
The candidate now maps Cookie-only API-key creation too: trusted session/user
snapshot is validated in a read batch, then a D1 write batch guards those
exact persisted values before inserting one hashed `pwk_` token record. The
plaintext token appears only in the successful 201 response; metadata GET
and database rows never expose it. Local Server account routes now delegate
scope/restriction/public DTO decisions to the same pure rules. Synthetic
local workerd passed issue, Bearer use, revoke and restart under
`server-http-key-create-state`; local D1 inspection found two key records,
one revoked, 64-character hashes, zero provider attempts and entitlement
revision 1. Cookie/session issuance, OAuth/App and production migration remain.
The isolated local workerd probe also published a synthetic primary assessment
depending on a secondary watch context; archiving that watch withdrew the
assessment in the same D1 state. It passed after a real restart in
`server-map-watch-fence-state` without exercising write-side cancellation.
The next fresh `server-map-watch-archive-state` workerd run exercised the
actual D1 archive adapter on a queued reserved Job, then separately confirmed
secondary-watch assessment withdrawal. It passed duplicate/restart probes;
port 8796 was stopped. No cron or remote schedule was configured.

The candidate Cookie handling boundary now applies the local Server's
SameSite=None Origin rule before reading a PATCH body. Its local Worker uses
synthetic allowed origins; untrusted Origin returns 403 and trusted Origin
continues. `server-http-origin-state` passed the HTTP driver before and after
restart. No production Cookie/origin configuration was changed.

The finite local mapping now has `account_entitlement_authority` and
`d1_claim_authority`. A previously accepted synthetic event updates the matching
user entry and billingEvents entry, increments the owner revision, and marks the
projection dirty in one batch. The trusted projection command checks that same
persisted entry and revision, then records plan, period, limit and a strict
validUntil boundary. Claim additionally checks the ledger's period and freezes
the revision; publication checks it again. An old claim cannot publish after
event A→B→A even when the final user JSON equals the original. Expiry denies a
claim or publication at the boundary. Local SQLite and workerd/D1 tests cover
these transitions and a real restart.

The experiment still has **no actual Creem handler or account persistence
adapter on Workers**. The production caller must provide the existing handler's
validated/deduplicated event record and `state_for_storage` user payload; every
accepted account/checkout/Creem write must bump the durable revision. A missing
writer would break the A→B→A fence. Projection must calculate the current
effective plan and expiry from the persisted account using Server's entitlement
rules, then CAS the revision. Annual billing still releases monthly quota,
upgrades do not reset usage, and a checkout return URL never grants paid rights.
Keep those cases in the existing billing protection tests while adding target
runtime equivalents.

No model result, transaction rollback, or compensation may erase a valid Creem
event, change an amount/currency, or manufacture provider confirmation. A
provider response loss is not exactly-once model execution; attempt spend is
conservatively retained while successful processing remains charge-key guarded.

## Persisted-account projection boundary

The local projection now stores `period_start` as well as strict validUntil.
Claim uses the UTC month for global attempts and the billing owner's projected
cycle interval for owner monthly attempts. A fixed Jan-to-Feb paid-cycle test
failed under the old shared UTC-month count and passes after the mapping
change. The old local D1 state directory remains untouched; the revised
schema was exercised under `.wrangler/server-map-cycle-state` with a real
process restart.

`cloudflare_analysis_adapter.py` derives owner snapshot, revision and monthly
attempt limit from D1 rather than trusting caller values. The first-charge
reservation command updates a bucket's limit on an upgrade without clearing
used/reserved counts. Its current scope deliberately rejects replayed charge
keys; released-charge reuse still needs mapping. Failed execution commands
fence the active token/lease, persist retry_wait deadlines, and on terminal
failure release ledger/bucket and mark the job failed in one batch. Attempt
spend remains. These paths passed local SQLite and workerd/D1 restart probes;
no real scheduler or provider call uses them yet.

The adapter now also confirms an active charge key in a guarded D1 batch
before returning its existing reservation, and atomically reopens a released
charge key with a new reservation for the same owner/module. Replays cannot
increment reserved usage. This matches the local reference's charge-key
identity for this slice; scheduling generation and cancellation interactions
remain unconnected.

The next local boundary adds a verified webhook receipt table. The isolated
Worker reads actual raw HTTP bytes and a signature header, uses the same pure
HMAC check as the local billing module, and persists a normalized synthetic
update before acknowledging. Duplicate identical bytes keep one receipt;
invalid signatures and same-ID conflicting bytes cannot overwrite it.
The normalized event ID is checked against the signed raw event ID. The
event normalizer now lives in pure `creem_event_rules.py`, takes an explicit
plan-to-product-ID binding and is reused by `billing.py` with its existing
configured IDs. `record_signed_creem_event` accepts that callable; the local
Worker executes the same module with synthetic IDs and no production config.
Applying a pending receipt marks it applied in the same D1 batch as the
trusted user, billingEvents, billingPendingUpdates and owner-revision change.
This is local protocol evidence only: real Creem handler composition, secret
binding, checkout lifecycle and receipt retention are not wired into the Worker.
The probe Wrangler config declares no cron trigger; scheduled behavior is
invoked explicitly at localhost during local testing only.

A first-generation enqueue command now freezes the current source/context
revisions and trusted trigger into a queued job. It computes global and owner
active queue caps inside the batch. A denied admission releases the new
reservation and marks the context throttled in that same batch. Generation
  reuse, supersession, cancellation/revocation and stale eligibility still
  require target-runtime mapping.

The Server async adapter scans due analysis Jobs by durable owner claim order
and insertion order, then runs the guarded claim batch for one eligible Job.
The isolated Python Worker's actual `scheduled` handler can wake this
path behind a local probe flag. Local D1 observed one attempt on the first
wake, no duplicate attempt on a second wake, and state persistence after a
real process restart. It never calls Jev. The due scan now inspects at most 16
owner-fair candidates and atomically marks stale source/context/reservation
bindings superseded, cancelled or blocked, releasing any reserved usage in the
same D1 batch. Live running leases and temporarily dirty account projections
remain untouched. SQLite tests covered rollback on an inconsistent bucket; the
local scheduled workerd/D1 probe covered stale source release and restart.
The subsequent local D1 run also blocked an old-cycle Job and released its
reservation after the clean account authority moved to a different period;
a dirty authority remained pending in SQLite tests. A later local D1 run also
failed an expired third-attempt Job and released the reserved unit without
recording a fourth attempt. Selector races, complete scheduler fairness, batch bounds and response-loss
execution remain CF2 work.

The command builders now live in Server `cloudflare_d1_mapping.py`, with
`cloudflare_account_adapter.py` providing async snapshot reads and one D1
`batch()` call per guarded write. The isolated probe packages the same Server
source modules into its generated local Worker package. Its accepted-event,
non-billing account write, pending-list and pending-association paths passed
on local workerd/D1 and after an actual process restart. The fixture passes
through `state_for_storage` with synthetic GitHub tokens and a synthetic key;
the persisted fixture contains encrypted fields and no plaintext tokens.
The adapter accepts already encrypted account JSON from a trusted caller;
no real Worker account codec or Creem handler is connected.

The pending-association batch uses exact persisted snapshots of the affected
user, the whole billingEvents map and the whole billingPendingUpdates list.
It changes all three plus the owner revision together or rejects them all.
This coarse CAS matches today's app_state shape but can conflict across
unrelated billing events; a finer storage layout and retention protocol are
still needed before production traffic.

Initial and refresh commands now parse the frozen persisted users entry on the
Server side and call `entitlements_for_user(user, timestamp=now)`. They accept
no plan, period, limit or expiry arguments; resetAt is strict validUntil. The
generated workerd fixture includes the resulting refresh batch, so the
isolated Worker has no copy of the tariff or monthly-cycle rules. Local tests
cover paid expiry to free, anchored monthly cycles, an upgrade in the same
period without clearing used/reserved counts, dirty projection and A-B-A
revision fencing. This is still a fixture, not an account writer.
Refresh also updates an already existing current-period processing bucket's
limit only when it differs, leaving used/reserved intact; a new period bucket
is still created on first reservation. This avoids an extra no-op D1 write.

The real webhook verifies the signature and delegates to
`apply_billing_update`. It changes in-memory USERS, BILLING_EVENTS and
BILLING_PENDING_UPDATES under STATE_LOCK; `persist_state` later writes these
maps with `db.save_state` and `state_for_storage`. Its pending association,
late-event audit and encryption need a designed durable boundary before the
D1 revision commands can protect actual events. The probe does not prove
durable webhook acknowledgment or account CAS across Worker invocations.
The current handler sends ACK before `route` runs `persist_state` in `finally`,
and that persistence catches exceptions. The billing reducer also calls the
synchronous quota-bucket writer. Worker adaptation must retain the existing
payment decisions while moving receipt durability before ACK and including
the quota limit update in the guarded account transaction.
ProductStore's reservation bucket limit update also needs a D1 command when
a plan upgrades; projection alone does not alter existing bucket limits.

## Scope still requiring adaptation

- Full ProductStore async reads and consistent authorization-filtered list/count
  snapshots; no use of a Worker/Container local SQLite file as durable storage.
- Source-only and cached publication, thread membership CAS for projections
  computed outside D1, adjacent handling inheritance, atomic multi-source
  context links, and safe cache upsert. The current mapped publication is a
  first-result/one-Item case; it deliberately rejects duplicate inserts.
- Queue fairness selection, retry/backoff, cancellation/revocation releases,
  webhook receipts and cron execution over the same authority. Existing local
  protocol probes validate mechanisms, not all Server commands.
- Connect all real account/Creem writes, encrypted payload handling, pending and
  late event behavior, and trusted entitlement calculation to the mapped revision
  protocol. The local synthetic expiry/revision proof is not that integration.
- Real Server HTTP on Workers, real provider bounded exit and actual model
  quality. The new Web HTTP integration uses local CPython and the Web proxy
  function in Node, with synthetic cookies/keys; it is not a browser or remote
  Workers deployment test.

## Reproduce

From `pullwise-server`, with TEMP/TMP set to the workspace test directory:

```powershell
$env:PYTHONPATH=$PWD.Path
D:/Python313/python.exe -m pytest tests/test_cloudflare_server_mapping.py -q
D:/Python313/python.exe tests/export_d1_server_fixture.py
```

Use the probe README's existing tool/cache environment and start its pinned
Wrangler locally on **8796**, using `--persist-to .wrangler/server-map-state`.
Run `D:/Python313/python.exe verify_server_mapping.py` from the probe directory.
Stop that process, restart with the identical persist directory, then run
`D:/Python313/python.exe verify_server_mapping.py --after-restart`. The driver
bypasses environment proxies for loopback. No schema/SQL upload endpoint exists.
