# S17/S18 bounded D1 validation

Updated 2026-09-28. The user authorized S17/S18 conditional on controlling
D1 usage, especially Rows Written. This is an execution candidate, not a
record of completed runtime acceptance or authorization to release production.

## Budget and execution status

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
one per environment. No coordinator binding or migration was deployed remotely.

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
| Migration/seed/cleanup | Three migrations create 13 tables and 23 SQLite indexes (7 explicit, 16 implicit), plus four app_state seed rows; include DDL and migration-bookkeeping rows, and teardown/index effects; remote bounds unknown |

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
- Server preview configuration still has placeholder values. Production points
  to the user's approved API domain and empty production database. Web's
  existing remote origin is already `https://api.pull-wise.com`.
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
