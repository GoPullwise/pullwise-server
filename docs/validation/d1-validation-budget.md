# S17/S18 bounded D1 validation

Updated 2026-10-06. The user authorized S17/S18 conditional on controlling
D1 usage, especially Rows Written. This is an execution candidate, not a
record of completed runtime acceptance or authorization to release production.

## Normal preview operation policy (2026-10-06)

The latest user request explicitly asks to remove unreasonable preview request
restrictions and deliver a usable product. Enabled preview product traffic now
uses `BudgetJournal(..., preview_product=True, product_operations=True)`. Its
ordinary SQL operations do not enforce the historical lifetime 100,000-read /
1,000-written reservation ceilings. These test ceilings made healthy use stop
permanently after a small number of edits or logins. Production access remains
paused, and generic finite validation plans retain their original request,
case and cumulative row limits.

The same Durable Object namespace/name, D1 database, journal and counters remain.
An immutable `preview_product_operation_policy` transition marker records the
old ceilings, request/reservation totals and any recovered pre-dispatch budget
stop. No reservations are refunded and no counters are reset. Only the exact
`BUDGET_EXHAUSTED` stop can close its old ticket, with schema readiness and
complete evidence matching observed totals. Native unknown outcomes, missing or
invalid metering, actual usage beyond a proven bound, incomplete requests,
timeouts, manual stops and schema mismatches remain blocked. The one existing
historical request/read grants retain their earlier rules.

Ordinary SQL continues to reserve its calculated worst-case rows before
dispatch, capture native actual rows and retain write reservations including
indexes. Native read attempts greater than one, or missing attempt metadata,
retain the whole read reservation and allow subsequent healthy traffic;
they do not imply an unknown SQL outcome. A native single attempt is still
required to settle unused read margin. Scalar/unique-key mutation fences,
64 statements per batch, 128 SQL groups per request, input envelopes, known
tables, schema fingerprints and cardinality checks remain. The shared request
lock remains necessary while the single active ticket and cardinality evidence
are authoritative; its bounded queue does not consume a lifetime allowance.
Current commercial project/record/write quotas, authorization, member roles,
owner billing and Jev dollar reservations remain independent and enforced.

The user's subsequent cost clarification sets about USD 200/month for the total
Cloudflare service as an operating target and explicitly rejects hard calendar
row caps that stop normal use. Abuse protection runs in the same DO SQLite store:
600 requests/minute per trusted Cloudflare IP, 240/minute per credential,
10 OAuth/installation authorize-or-callback requests/minute per IP, and after
existing authentication reads, 120 read attempts or 60 ordinary write attempts
per minute per actual actor across sessions/API keys. Emergency revocation has
a separate 120/minute actor bucket. Free's existing commercial 10 writes/minute
and paid plans' 60 writes/minute still apply. A rate rejection returns 429 with
`Retry-After`; it adds no D1 quota-counter writes and never stops the journal.
Only hashed subjects are stored, capped at 20,000 active buckets; their windows
expire within two minutes. These ephemeral buckets are separate from retained
accounting/evidence. The cost target is not an invoice ceiling. Remote acceptance
must stay finite and low frequency; no D1 polling or load testing is authorized.

Persisted `product_data_verified` cardinality avoids a full 18-table COUNT/state
scan before every read request. Fresh/previously unmarked initialization verifies
once; a successful schema upgrade already verifies its new counts. Every product
mutation still refreshes and persists counts before closing its ticket. A restart
may reuse a completed verified snapshot; an active/incomplete ticket still stops.
This relies on the existing exclusive preview-write premise: all D1 product
writes use this same singleton, and remote console/another Worker writes are
excluded. An operator migration must separately prove its schema/cardinality.

Known provider/business 403/404/409/413/422/429/502/503 responses finish the
current ticket normally and allow subsequent users to continue. The existing
30-second application deadline remains. If cancellation has fully awaited all
native IO, the journal is healthy and mutation cardinality was refreshed, a
current provider/application timeout or uncaught application error closes only
that ticket and returns a safe 503. Its numeric request ID/failure kind/time
append to `preview_request_failures`; no exception text is exposed. A canceled
native dispatch, incomplete cardinality refresh or any existing stop continues
to fail closed. Previous timeout/unknown stops are never resumed by this path.

New operation evidence appends to `preview_operation_evidence` in the same DO
SQLite storage. `preview_read_settlements` appends proven read-margin releases.
SQLite triggers atomically publish the corresponding cumulative counters.
Existing JSON evidence is retained unchanged; neither table has an application
update/delete/reset endpoint. Normal requests query only the latest evidence
row; the internal audit RPC returns at most 128 new rows and a truncation flag,
while all earlier history remains stored. This prevents ordinary operation from
rewriting an indefinitely growing evidence array on every SQL group.

`GET /_preview/budget` remains DO-only and exposes numeric cumulative usage,
`productOperationMode: true`, unenforced lifetime `limits` as null and the
historical numeric ceilings separately. It exposes no account data, parameters,
tokens or provider payloads. Publication checks may compare counters across the
transition, rather than treating the old ceilings as the current product gate.

Oversized SQL parameters are rejected before that SQL group's dispatch as an
ordinary invalid-input request, without stopping all users. Preview CSV exports
still finish their metered D1 work before returning a response. The former
1 MiB restriction is replaced with a 24 MiB UTF-8 byte spool; this leaves room
within the [Workers 128 MB memory limit](https://developers.cloudflare.com/workers/platform/limits/).
Larger exports return 413 `EXPORT_TOO_LARGE` with filtering guidance and keep
the journal healthy; partial CSV files are never returned. Production/local
exports retain their existing streaming response path. This bounded spool is
an explicit compatibility choice while preview accounting requires all D1
pulls to finish inside the active ticket.

References checked for this change: [Workers best practices](https://developers.cloudflare.com/workers/best-practices/workers-best-practices/),
[Workers streams](https://developers.cloudflare.com/workers/runtime-apis/streams/),
[D1 automatic retries](https://developers.cloudflare.com/d1/observability/debug-d1/#automatic-retries).
The dated sections below describe prior finite validation policies and evidence;
their lifetime ceilings and 1 MiB product-export cap are historical for enabled
normal preview product traffic.

## Current workspace/repository schema plan (2026-10-06)

The new version is implemented and released to preview with its dedicated
local native/browser evidence and actual compiled v4-to-v5 runtime proof.
Its canonical schema freezes five migrations, 18 tables and 33 SQLite indexes.
0005 appends four member/invitation/event/repository-binding tables and two
project columns, then backfills the original single-repository associations to
the same project IDs. It does not rewrite expenses or financial history.

Fresh empty-schema initialization reserves 256 written / 4,000 read rows for
the 32 compiled DDL/seed statements, plus 384 read rows for the empty-schema
check. The already initialized preview must instead use the exact retained
legacy-v4 schema/fingerprint and its separate binding-only v4-to-v5 plan.
The versioned preview upgrade flag explicitly enables this plan before the
first eligible application request under the existing DO lock; ordinary product
SQL cannot dispatch migration DDL. The compiled plan reserves
all operations before dispatch, verifies the exact old schema and cardinalities,
executes all ten 0005 statements in one atomic batch and verifies the new schema.
For existing project count `P`, written reservation is `128 + 21P`; compiled
reads must fit the finite 10,000-read per-case ceiling. The whole plan must also
fit the same journal's remaining cumulative 100,000-read / 1,000-write allowance.
There is one upgrade admission and no reset, retry, new namespace/database or
release of prior write reservations, including after partial/unknown outcomes.

Local native SQL/budget measurement passed: four real D1 batches read 324 rows
and wrote 25, below the retained 4,852/170 reservation for two existing projects.
Miniflare omits `total_attempts`; its pinned D1/workerd source shows one native
transaction/fetch without a retry loop. The measurement preserves those nulls
and is not the deployed strict remote gate. The first failed local claim stays
stopped and unchanged. Remote read phases require native integer `1`; only the
exact pinned CREATE/ALTER/INSERT write group may use D1's documented nonretryable
write contract when attempts are absent. Provided non-1 attempts still reject.
No attempt value is invented and no deployed local bypass exists. The finite
preview upgrade passed the final runtime gate: schemaVersion=5, stopped=null,
retained additional write reserve 170 and native write delta 25. Final snapshot
is reserved 33,358/719 and observed 21,716/359. Production access stays 0.
Workspace mutations normally consume the ledger Owner's commercial allowance.
Emergency member removal, invitation revocation and own-key revocation are
commercial-quota exempt; current authorization/atomic guards and global D1
admission/accounting still apply.

The dated sections below retain original-version observations and grants.
Their four-migration/14-table/24-index measurements describe the historical v4
schema, rather than the current canonical target or native upgrade acceptance.

## Product-wide preview authorization (2026-09-29)

### Later preview-only read grant (2026-09-29)

After a new BUDGET_EXHAUSTED login failure, the user approved cumulative preview
Rows Read of 100,000, explicitly limited to preview. Rows Written stays 1,000.
Default/non-product plans stay at 10,000 reads; production remains paused. The
fixed namespace/database and all observed/reserved counters are preserved.

Only the enabled preview product selects this ceiling. One journal marker
records the approved expansion. The previous read-budget stop may close its
rejected pre-dispatch ticket only with a ready schema, in-budget counters and
complete evidence matching observed totals. Other stop reasons, incomplete
evidence and subsequent exhaustion never recover through this grant. No legacy
read margin or write reservation is released. Request/SQL/statement limits,
native accounting and input/cardinality/index bounds remain enforced.

Deployment acceptance has one case: one GET of `/_preview/budget`, no retries,
providers or D1 queries. Its initial DO access may record the one grant in the
existing DO SQLite journal, which is not D1. Expected limits: 100,000 read /
1,000 written, stopped=null, counters at least 9,898 / 218 reserved and
2,856 / 131 observed. Total validation D1 bounds: 0 Rows Read / 0 Rows Written.
Manual product traffic thereafter consumes the expanded cumulative ceiling;
there is no automatic polling, reset, new database or namespace.

The user subsequently explicitly requested all preview product functionality,
including deployment/initialization needed to make it usable now. Production
stays paused. The cumulative 1,000 written / 10,000 read limits remain active;
this is not authorization to remove/reset the budget or use production data.
This is the historical baseline; the later grant above supersedes only the
enabled preview product's read ceiling.

Product preview uses the same fixed ValidationBudget namespace/name. It replaces
HTTP case denial with serialized product requests and durable reservation before
each SQL group. The original 200 cumulative request cap was explicitly removed
by the user on 2026-09-29 for the enabled preview product. Request accounting
continues cumulatively; 128 SQL groups per request and 64 statements per group
remain enforced. No application retry, cron, polling,
new namespace or reset endpoint is introduced. Missing/ambiguous native D1 meta
stops persistently. Normal business/provider HTTP errors with complete D1 meta
retain their reservations; a subsequent manual request is separately capped.

The existing REQUEST_LIMIT stop may be cleared once only for an inactive,
schema-ready preview journal with at least 200 requests, complete matching
evidence and in-bound 100,000-read / 1,000-write counters. Preserve all usage,
requests, cases and prior grants. Other reasons, active tickets, incomplete meta
and row-budget exhaustion remain blocked. Default/generic request plans retain
their original finite request/case caps. Publication checks one DO-only budget
GET (zero D1 rows, zero provider/account requests, zero retries).

### Executable row-bound rules

Read settlement correction (2026-09-29): actual usage was 955 read / 86 written,
while reservations reached 9,969 / 164 and blocked the user. New product groups
still reserve the same worst-case bound before dispatch. Only complete, in-bound
results with native `total_attempts=1` for every statement settle unused READ
margin; retries, missing attempt metadata and ambiguous outcomes retain the full
reservation. WRITE reservations never decrease. Observed counters and evidence
remain cumulative. See [D1 result metadata](https://developers.cloudflare.com/d1/worker-api/return-object/).

The original successful initialization's non-read-only DDL/seed batch cannot
be automatically retried. Its complete operation-1 metadata can therefore settle
only its 2,000-read margin once, with the schema-ready flag, one initialization,
matching complete evidence totals and original request-1 operation order verified.
The empty-schema read retains all 384 reads, all old product reservations remain,
and the 128-write initialization reservation stays intact. Only BUDGET_EXHAUSTED
may recover; unknown outcomes/timeouts/incomplete metadata never recover. This
is a journal-only reconciliation with an audit marker, no D1 request, public reset,
budget reset, raised ceiling or replacement namespace/database.

Publication validation is finite: one unauthenticated session GET, followed by
one DO-only status GET. Session reads cannot write D1; its read bound uses existing
cardinality and the three-attempt reserve. No polling/provider call or write test.

- Current fresh initialization first reads sqlite_master with LIMIT 65,
  reserving 384 reads including native read retries. Nonempty application
  schemas are rejected. The frozen five migrations execute as one atomic D1
  batch: 18 tables, 33 indexes, four app_state seed rows and 32 finite SQL
  statements, with no Wrangler migration table or untracked bookkeeping.
  Reserve **256 written / 4,000 read** for that batch. Existing v4 preview uses
  the distinct one-shot upgrade described above, never fresh initialization.
  D1 write queries are not automatically retried. The original v4 initialization
  reserved 128 written / 2,000 read; its dated evidence and reconciliation remain
  preserved and are not recalculated as v5 initialization.
- Runtime INSERT must be scalar VALUES or scalar SELECT without a top-level
  FROM/compound SELECT. REPLACE and multi-row VALUES are rejected. UPDATE/DELETE
  require a top-level conjunctive primary-key equality fence. Current index
  counts are frozen with migration fingerprints. Scalar INSERT reserves one
  base row plus all indexes; UPDATE/UPSERT reserves one plus twice all indexes;
  scalar DELETE reserves one plus all indexes. Maximum is 9 for expense UPDATE.
- Guard inserts are scalar; guard DELETE reserves every preceding insert in
  the same atomic group. Every guard-bearing group must end with zero guards.
  D1 batch rollback prevents failed transactions leaving extra guard rows.
- Numeric cardinality snapshots contain no account/token data in the journal.
  They start at the exact seed shape, refresh before requests and after writes,
  and reserve COUNT/state reads using the previous counts plus scalar insert
  upper bounds. Unknown/excessive cardinality or nonempty guards stops the run.
  Runtime parameters are UTF-8/finite scalar bounded; state JSON is capped at
  8,192 bytes. Read reservations cover table traversals, indexed probes and
  JSON iteration using current cardinalities/parameter collection bounds.
- D1 can retry read-only queries twice, so each read-only group's reservation
  covers all three attempts. There is no application retry; proven unused single-
  attempt read margin may settle as specified above. Details:
  [automatic read retries](https://developers.cloudflare.com/d1/best-practices/retry-queries/).
- CSV is materialized inside the active budget ticket, capped at 1 MiB. No lazy
  D1 pull remains after releasing the response. Queue depth is capped at 16;
  concurrent page reads serialize rather than consuming overlapping tickets.

Historical native local Python Worker/D1/DO product run (2026-09-29): 15 finite checks including login,
installation, profile, category/expense creation, list/report/CSV and edit/delete
passed; two concurrent session requests also returned 200. Before those two
reads, observed usage was **464 read / 131 written**, reservations **7,847 /
232** and no stop. A failed earlier lazy-CSV buffering attempt retained all
reservations and stopped; its local state was preserved. The fixed fixture is
separate local-only persistence, never a replacement remote namespace/budget.
The subsequent SELECT traversal estimate removes an unnecessary fixed DML probe
allowance; its bounds still include indexed/JSON traversal and native retries.

Local artifacts: .agents/runtime/preview-product-local-20260929/ including
http-evidence-csv-fixed.json, csv-fixed-journal.json and concurrency-evidence.json.
GitHub in this run is synthetic. All prior source/secret/config readings and
guard HTTP checks used zero remote D1. Remote initialization and bounded product
smoke checks are pending publication of this reviewed product-preview mode.
Jev remains disabled without its separate real credentials/quality gate.

2026-09-29: MeteredD1 additionally checks per-statement parameter envelopes
before dispatch (count, scalar type, safe integer range and UTF-8 byte bound).
Missing envelopes allow no parameters. Violations stop without a D1 call;
the full reservation remains consumed and no parameter values enter evidence.
These input limits do not prove JSON cardinality, database cardinality, scans,
DDL/index effects or provider behavior. The remote allowlist remains empty.
Preview now has a separate GitHub App and all required Secret names; real
login is requested but cannot complete while the D1 pause remains enabled.

## Finite schema and identity local replay (2026-09-29)

`scripts/check-preview-identity-cost.py` prepares the canonical migration SQL
and captures SQL batches from the current Python identity handlers with
synthetic GitHub responses. The trace is replayed through an isolated local JS
Worker and native D1: this measures the same SQL/parameters/batch boundaries,
not the live Python FFI or real GitHub transport. A single no-proxy/no-redirect
POST executed 55 operations; all statement results supplied valid native meta.
The local process was stopped. No remote D1 or provider request was made.

| Local phase | SQL operations | Observed Rows Read | Observed Rows Written |
| --- | ---: | ---: | ---: |
| All four migrations and four app_state seed rows | 22 | 28 | 60 |
| Login authorize | 2 | 1 | 4 |
| New-user callback, authority and session | 11 | 13 | 13 |
| Session read | 2 | 2 | 0 |
| Used callback replay | 1 | 1 | 0 |
| Installation authorize | 4 | 4 | 3 |
| Installation callback | 6 | 6 | 6 |
| Repository read | 2 | 2 | 0 |
| Sign out | 4 | 4 | 3 |
| Signed-out session read | 1 | 1 | 0 |
| Total | **55** | **62** | **89** |

The reference fixture has 14 tables / 24 indexes (7 explicit, 17 implicit),
one user, at most one active state/session and one repository. Final user/state/
session cardinalities are 1/0/0. Migration names and SHA-256 fingerprints are
in the local manifest. Evidence is stored outside version control under
`F:/Pullwise/.agents/runtime/identity-cost-20260929/`: manifest.json, trace.json
and native-evidence.json. The reference SQLite database and synthetic parameter
trace are local fixtures, not remote seed data or credentials.

The observed 89 writes are **not** a remote ceiling. SQL grouping/schema/input
drift, D1 DDL/bookkeeping overhead, rejection/provider-failure cases and any
cleanup still need reviewed bounds. No arbitrary multiplier becomes a proof.
The trace bypasses no remote control: its generated config is local-only,
and `remote_admissible` is false. Remote totals remain 0/0.

JSON map parameter envelopes now enforce top-level item limits in addition to
UTF-8 bytes, rejecting duplicate keys, non-finite JSON and invalid Unicode
before dispatch. Reviewed plans must supply the appropriate field policies;
these policies do not automatically establish nested collection/row bounds.

Initialization now has a binding-only RPC using the existing singleton journal,
full upfront reservation and exactly one fixed execution across restarts.
Unknown/missing metadata stops the remaining SQL and retains all reservations.
The RPC accepts no SQL/params from callers; no public route, reset, retry or
cleanup was added. `REVIEWED_INITIALIZATION_PLAN` is None, so this capability
remains disabled pending concrete DDL/empty-schema proofs. Remote HTTP plans
also remain empty. Native Python RPC/FFI acceptance is still pending.

## Budget and execution status

2026-09-29 preview switch check: the user explicitly authorized setting only
the preview D1 access switch to 1. Metadata read-back confirmed preview mode,
the original isolated DB/coordinator and production switch 0. The deployed
preview source was read through the Worker code API: its remote plans remain
empty and UNREVIEWED_CASE returns before the journal, DB or provider handler.
This is not authorization to populate unknown-bound plans or initialize D1.
The checked-in deployment config deliberately retains its paused default 0;
a future preview deployment restores that default unless reviewed separately.

Finite ingress check plan: exactly one GET through preview Web to
`/api/auth/github/authorize`, no redirect following, no retry/polling. Expected
503 UNREVIEWED_CASE. The inspected pre-dispatch path has a bound of 0 Rows
Read / 0 Rows Written and no provider attempt. Stop at any unexpected result;
do not start a login, seed/schema operation or subsequent functional case.
Remote SQL allowance remains zero until those cases have proven bounds.

The one ingress request returned exactly 503 UNREVIEWED_CASE; no redirect or
retry occurred. The inspected deployed path rejects before all D1/provider
access, so the test's row usage is 0/0 by path proof, not a dashboard reading.
The browser connector inventory failed without opening any page. Public shell
fallback is limited to one GET each for /, /pricing and /login, at most three
requests with no redirects/retries; stop on the first unexpected result. These
do not execute browser JS. No SQL case is admitted, so upstream D1 usage stays
bounded at 0/0. HTTP shell checks are not visual/browser acceptance.

2026-09-28 continuation: the user waived real GitHub login/repository authorization
testing. Test Creem IDs and test Secrets are configured in a separately deployed
**paused** preview Server; preview Web proxies only that Server. Empty preview
DB creation, namespace/deployment and Secret configuration used metadata APIs,
not D1 SQL. Both switches remain 0 and the remote case allowlist is empty.
Cumulative remote D1 rows are still **0 read / 0 written**.

Migration 0004 adds `ledger_plan_usage` and an implicit primary-key index, making
the current target 14 tables / 24 SQLite indexes. Every protected resource write
now includes a usage UPSERT; initialization counts existing owner rows once.
API-key revocation is exempt from this commercial quota for security, but remains
inside global validation admission/accounting. Earlier base mutation counts
omit these new effects and must not be used as current remote upper bounds.
The deploy script no longer runs or proposes direct remote D1 migration commands.

New native local steps: schema/fixture setup measured 29 reads / 61 writes;
guarded creation measured 1 / 9. Expected over-cap failure provided no complete
meta; the run stopped with unknown actual counts for that batch. No retry or
remote probe was used. This shows why error-path reservations remain mandatory.

- Approved cumulative S18 ceiling: 1,000 Rows Written and 10,000 Rows Read.
  The user also authorized restoring pinned development dependencies and
  deploying Server separately at `https://api.pull-wise.com`.
- Remote database query/DDL/data operations performed so far: **zero**.
  A new empty `pullwise-ledger-production` database was created via the
  metadata API (ID `80a29a0d-5699-449f-9541-a01dc461ca9d`). No remote migrations
  or fixtures have been applied. Worker deployment status is recorded in
  `local-acceptance.md`.
- No cron, scheduled handler, monitoring queries against D1, load testing,
  automatic polling or automatic retries are permitted.
- S17 uses only local bindings (`remote: false`), an isolated local state
  directory, synthetic fixtures and no GitHub/Creem/Jev network calls.
- S18 uses a reviewed, distinct preview database with no production data.
  Preview isolation does not imply a separate included allowance.

## Accounting

### Implemented local admission boundary (2026-09-28)

`ValidationBudget` in `cloudflare/server/src/entry.py` now executes preview
HTTP handlers behind a single Durable Object. `cloudflare_validation_budget.py`
uses its synchronous SQLite storage to persist a full worst-case reservation
before any application/provider call. It does not spend D1 rows on budgeting.
The fixed scope is `pullwise-s17-s18-2026-09-28`: any future preview binding
must share ONE authoritative namespace across stages/databases, not provision
one per environment. The preview coordinator namespace is now configured;
no remote D1 migration or schema initialization has been executed.

- Hard ceilings: 1,000 written / 10,000 read rows, 40 admitted requests, finite
  per-case requests and at most 64 D1 batch operations per request.
- No refund on success, denial, failure or timeout. Observed actual metadata is
  tracked separately; incomplete metadata is explicitly marked incomplete.
- One durable active ticket excludes interleaving HTTP handlers across awaits.
  A restarted object with a pending ticket permanently stops. SQL operation
  slots are consumed before await, so overlapping calls cannot reuse a slot.
- The metered binding matches exact trusted SQL batch groups before dispatch,
  preserves domain transactions, and captures every result's native meta.
  `first()` uses the full batch result so it does not discard accounting.
- Missing/invalid metadata, unexpected response, SQL/case/request/row limit,
  ambiguous result or timeout stops persistently. In-flight returned metadata
  is retained even after manual stop. Internal stop/evidence RPCs use DO
  storage only; no public HTTP control or reset endpoint exists.
- Production cannot enter preview validation even if its access switch is
  mistakenly set to 1. Preview without the coordinator fails closed before
  reading DB/provider configuration. All OAuth, Session, API-key, webhook and
  unknown HTTP paths take this same ingress boundary.
- `REVIEWED_REMOTE_PLANS` is **empty**. No runtime path is remotely admitted.
  CSV's lazy D1 pulls are explicitly blocked until its entire stream lifetime
  is bounded. Migration DDL, fixtures and cleanup have no approved plan and
  must not be executed through Wrangler/REST outside this boundary.

These controls are implementation/local evidence, **not** proof of the numeric
SQL bounds needed to enable a case. An incorrect bound cannot be undone by
checking meta afterwards. Remote configs and deployment checks keep access at 0
and forbid cron. Provider presence does not relax these gates.

### Complete cost-path inventory

The packaged import closure and entry were inspected locally after CodeGraph's
bounded query did not return. Static SQL/I/O inventory was retained under the
ignored workspace runtime directory; generated mirrors are not authority.

| Path / modules | Possible side effects and bound prerequisite |
| --- | --- |
| Health; `cloudflare_http_contract` | sqlite_master/schema reads; returned COUNT is not billed Rows Read |
| Principal/profile; `cloudflare_principal`, `cloudflare_ledger_auth`, `cloudflare_ledger_profile` | Session/user JSON virtual-table and key reads; resource snapshot SELECTs are repeated in the authorization batch |
| OAuth/App; `cloudflare_github_identity_http`, `cloudflare_oauth_state_adapter`, `cloudflare_session_adapter` | GET authorize issues state; callback consumes state, writes user, may initializes authority, then creates session; installation callback consumes state and writes repository access; logout revokes session |
| Key management; `cloudflare_api_key_read`, `cloudflare_api_key_write` | Create/revoke writes key and guard rows; authentication alone does not update last_used_at in current source; include key's PK, unique hash and user/revocation indexes |
| Ledger; `cloudflare_ledger_api`, `cloudflare_ledger_expenses`, `cloudflare_ledger_reports` | Resource, guard, audit and idempotency writes; aggregate scans are not bounded by result count; CSV continues in 250-row pages after HTTP response |
| Billing reads/catalog; `cloudflare_billing_read`, `cloudflare_billing_catalog`, `cloudflare_billing_catalog_refresh`, `cloudflare_billing_catalog_write` | GET /billing/plan can refresh stale catalog: provider requests plus catalog/guard writes; paid/free plan projection is not a resource-quota definition |
| Billing mutations/account; `cloudflare_billing_mutations`, `cloudflare_account_adapter`, `cloudflare_d1_mapping` | Account CAS, pending updates/events and authority revisions; pending reconciliation can settle up to 16 receipts per call, each with its own writes |
| Creem ingress/replay; `cloudflare_creem_handler`, `cloudflare_webhook_receipts` | Receipt/guard writes occur even for a duplicate; an applied receipt can still refresh dirty authority; unknown owner may park a pending event; settlement writes account/events/pending/receipt/authority and guards |
| Jev; `cloudflare_ledger_suggestions` | Suggestion budget/event/guard writes are separate from expense writes; disabled and unadmitted |
| SQL helper; `cloudflare_d1_batch` | Trusted finite command lists preserve one transaction; statement count is not a row bound |
| Migration/seed/cleanup | Four migrations create 14 tables and 24 SQLite indexes (7 explicit, 17 implicit), plus four app_state seed rows; include DDL and migration-bookkeeping rows, and teardown/index effects; remote bounds unknown |

Logical success-path table writes below assume serial execution and an empty
guard table. They omit billed index/internal rows and failure paths and are
**not remote upper bounds**:

| Additional step | Logical base-table rows written |
| --- | ---: |
| OAuth state issue / consume; Session issue / revoke | 3 each |
| New-user OAuth callback | 12 (state consumption, user write, authority initialization, session issue) |
| Existing-user OAuth callback | 9, excluding any initialization/failure branch |
| App install authorize / successful callback | 3 / 6 |
| API-key create / revoke | 3 each |
| Catalog publication / refresh write | 3 |
| Webhook receipt insert / duplicate receipt check | 3 / 2 |
| Park unmatched receipt | 7 |
| Settle receipt | 19 |
| Refresh dirty authority | 5 |

The current `app_state` maps/arrays and `json_each` scans need explicit finite
cardinality/input constraints; a few physical JSON rows do not establish a
read bound. `DELETE FROM d1_command_guard` has no predicate. Any existing guard
rows or another database writer invalidate the serial base counts. Local
SQLite index inspection is structural evidence, not D1 billing calibration.

No remote step currently has a proven bound: migration, identity, key/payment
cases, fixtures and cleanup remain **not admissible**. Their current executable
remote request allowance is zero. The earlier 20-request ledger candidate and
32/500 reservations remain proposals only; do not use them as approved limits.

### Actual finite local cost-control evidence

An ignored, generated loopback-only fixture exercised the production coordinator
and adapter on real local Python Worker, SQLite-backed DO and D1 bindings. It
used no provider or remote binding and did not seed CSV fixtures remotely.

| Local step | D1 Rows Read | D1 Rows Written |
| --- | ---: | ---: |
| Request 1: CREATE table, INSERT, SELECT, metered first(COUNT) | 3 | 3 |
| Request 2: same finite case | 4 | 1 |
| Request 3: rejected with CASE_LIMIT before D1 | 0 | 0 |
| Total observed local D1 meta | **7** | **4** |

The persisted DO journal retained **220 reads / 40 writes reserved**, two
admitted requests, all four operation metadata totals and sticky CASE_LIMIT
after the process was stopped. No reservation was refunded. DO storage usage
is separate from these D1 totals and has its own billing if deployed remotely.
The local fixture bounds are test-only and do not admit remote DDL or data.

The first intended loopback client attempt used the machine's default proxy
and timed out; it was stopped without an automatic retry. The cause was
confirmed with proxy_bypass('127.0.0.1') == False. After the transport was
corrected, the explicit three-request finite run above passed. Both local
validation scripts now disable proxies/redirects; a regression test reproduces
the proxy failure first and passes with the fix.

Evidence files, outside version control:
`F:/Pullwise/.agents/runtime/budget-local-evidence-c102.json` (stopped attempt),
`budget-local-evidence-c102-proxy-fixed.json` and `budget-local-journal-c102.json`.
No extra remote D1 request was used for evidence/monitoring.
**Cumulative remote usage remains 0 Rows Read / 0 Rows Written**; the unused
approved ceiling remains 10,000 / 1,000. No remote migration or production
activation occurred.

[D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/) defines
Rows Written to include INSERT/UPDATE/DELETE and additional index writes;
DDL may also contribute reads and writes. Under the documented paid rate,
1,000 additional written rows cost $0.001 for this D1 usage component alone.
This does not cap Workers, storage, other services or unrelated account usage.

Each executed statement/batch must report `meta.rows_read` and
`meta.rows_written`. Aggregate these in a local evidence file, with no
extra D1 monitoring writes. Missing metadata, timeouts or unexpected values
stop the run; do not retry ambiguous requests automatically. A request cap
and verified worst-case reservation must prevent starting a step that could
exceed the remaining budget. Measuring after a request alone is not a hard cap.

## Source audit: ledger mutations

These counts describe application-table mutations on the success path, before
index effects. They are NOT Cloudflare billed-row measurements or proven
remote upper bounds. They assume an empty guard table and serial validation.

| Operation | Base table rows written | Source reasoning |
| --- | ---: | --- |
| Create project/category | 3 | One credential guard insert, one resource insert, one guard deletion |
| Update project/category or archive category | 5 | Two guard inserts, one resource update, two guard deletions |
| Create expense | 9 | Three guard inserts/deletions plus expense, audit and idempotency inserts |
| Update expense | 6–10 | Two to four guard inserts/deletions, one expense update, one audit insert |
| Remove expense | 6 | Two guard inserts/deletions, one expense update, one audit insert |
| Replay same expense creation / repeat removal | 0 | Existing response / already deleted early return |
| Expense list, reports and CSV | 0 | Principal/snapshot and resource SELECTs only |

Sources: `cloudflare_ledger_api.py`, `cloudflare_ledger_expenses.py`,
`cloudflare_ledger_reports.py`, `cloudflare_ledger_auth.py`,
`cloudflare_principal.py`. Guard DELETE has no WHERE clause; any unexpected
pre-existing guard rows invalidate these counts. Reject a nonempty guard
fixture locally and do not validate against an existing shared database.

Indexes on expenses, events, idempotency, projects and categories add writes.
For the small ledger-only candidate, reserve 32 written rows per mutation
and 500 read rows per request until local runtime measurements and schema
analysis establish conservative bounds. These reservations are proposals,
not verified guarantees. Do not execute remotely using an unverified bound.

## Finite case candidate

Use one test account, at most two projects, two categories and eight expenses.
Keep full-page CSV boundary fixtures (250/251 rows) exclusively local.
CSV remains paginated in chunks of 250; list requests use explicit small limits.
No cached protected response is shared between accounts; no refresh loop is
used to wait for provider state.

| Case group | Maximum ledger HTTP requests | Written-row reservation |
| --- | ---: | ---: |
| Two projects and two categories | 4 | 128 |
| Four expenses, including mixed currencies and shared pool | 4 | 128 |
| Creation replay, move, edit, stale revision, remove, repeat removal | 6 | 192 (reserve each request conservatively) |
| Filtered list, three reports, CSV, denied scope | 6 | 0 after read-only path verification |
| Ledger subtotal | 20 | 448 plus verified read bounds |

This ledger subset alone does not complete S18. Identity login/App callbacks,
API-key creation/revocation, Creem signed webhook/replay, migration DDL,
seeding and cleanup must have separately established bounds fitting the
remaining allowance before remote execution. Their bounds are currently
unknown. Real anonymized Jev quality samples are unavailable; keep Jev off.

## Required controls before S18

1. Numeric ceiling is confirmed. Review distinct preview targets before S18.
2. Complete S17 with pinned local tooling and capture actual local D1 metadata.
3. Bound migration/index/identity/payment/cleanup effects; revise the finite
   case list if they cannot fit. Never copy an existing account/database.
4. Implement and verify admission control covering EVERY preview D1 path,
   including OAuth/provider callbacks and concurrent requests. A client-side
   runner cap alone cannot constrain unsolicited traffic to a public Worker.
5. Reserve worst-case rows before each request, serially; stop on any error,
   missing accounting or divergence. Keep no background work running after
   the finite run. Include teardown writes in the reservation.
6. Recheck configurations contain no cron or remote bindings for local work.

## Current prerequisites

- Pinned Wrangler 4.136.3, workers-py 1.17.4, workers-runtime-sdk 1.9.0 and
  uv 0.12.3 were restored with permission. A separate healthy Python 3.10.12
  reference environment and Python 3.14.2 Worker tooling live under the
  ignored workspace `.agents/runtime/`; the broken original venv is preserved.
- Real local Worker/D1 migrations and 17 HTTP acceptance requests passed,
  including the 251-record paginated CSV/ReadableStream bridge, Cookie/API key,
  CRUD, idempotency, conflict and reports. They incurred no remote D1 usage.
- Preview public variables and all four Secret names are configured, including
  the separate gopullwise-preview App. Provider callbacks/installation and
  credential validity are not implied by configuration. Production stays on
  its separate App/domain/database; neither remote schema has been initialized.
- `PULLWISE_D1_ACCESS_ENABLED=0` rejects every route before D1/provider access;
  missing/invalid values also fail closed. Both remote configs keep it off.
  This is a verified zero-access pause, not an implemented metered quota for
  an active service. Do not turn it on until finite admission/accounting
  controls and required provider prerequisites have passed.
- No Cloudflare/GitHub/Creem credential variable names or local .dev.vars were
  available in the inspected task environment. This does not establish that
  no dashboard/CLI credentials exist; none were read or exposed.
- Cloudflare CLI/connector authentication was subsequently confirmed without
  reading its token. The user supplied GitHub App ID 3631508, Client ID
  Iv23lipOl05N1ZULZmYW and slug gopullwise. The user confirmed changing both
  callbacks to the Web proxy URLs. Public App configuration was deployed and
  verified while D1 remained disabled. A fresh 32-byte random Token encryption
  key was stored as `PULLWISE_GITHUB_TOKEN_KEY` in Cloudflare Secrets, without
  displaying or checking in its value. A later names/type-only check confirms
  the GitHub Client Secret and both Creem Secrets are now present. The four
  user-supplied Creem product IDs were synchronized as a plain_text JSON
  string and read back exactly; all existing bindings were inherited and D1
  remains off. No payment/provider or D1 request was made for this sync.
  Secret presence is not validation of its value, and product environment/
  prices and real OAuth/install/payment behavior remain unverified.
- CodeGraph did not return within the bounded lookup; source audit used the
  implementation files named in current checked-in documentation.

Web `npm run check` (33 files / 252 tests, lint/build) and
`npm run check:workers` passed in this continuation. The browser connector
remains unavailable; S17 browser integration and S18 provider acceptance
remain open. See the current Server acceptance record for reference/CI results.
