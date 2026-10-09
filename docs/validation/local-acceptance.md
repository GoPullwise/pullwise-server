# Current local acceptance

## English-only verification emails (2026-10-09)

After the member-role refresh preview release, the shared verification template
uses English only for its subject, plain text and HTML, regardless of interface
language. Both login and email linking keep their existing six-digit code,
leading zeros, ten-minute expiry, ignore-email hint and delivery behavior.
The HTML declares `lang="en"`. Authentication, schemas, bindings, rate limits
and no-retry delivery behavior are unchanged.

The existing mock-binding gateway assertion verifies English-only output in all
three fields, both code copies, expiry and ignore hints, recipient privacy and
one send. Python 3.10.12 collected the complete current suite: **1,736 tests and
60 subtests passed**, with one environment failure because the sandbox blocked
the existing loopback socket test. A narrowly approved rerun of that one test
passed: its server binds only `127.0.0.1`, makes four requests to the local mock
and bypasses system proxies. Thus all **1,737 tests and 60 subtests** are verified
across the two runs; no test was skipped or changed for the socket restriction.

Module generation and `--check`, `check-ledger-s01.py --allow-placeholders` and
`bash -n scripts/deploy-cloudflare.sh` pass. No real email, remote D1/provider
request or deployment was performed for this local acceptance. Publication is
recorded separately after the checked source is released to Server preview.

Publication preserves concurrent main's mailbox-ownership precheck (`b5682b1`)
at source `509d528`; all **139 tests in five email-related files** pass on the
merged source. Source mirror, static and script checks pass again. The net
English-copy patch does not modify that upstream authentication behavior.

[Preview receipt](english-verification-email-preview-release-2026-10-09.json)
records Server version `34b4dea7-efb2-4b88-baca-f98f0293dc55`. Management readback
confirms that EMAIL sender/configuration, original database/coordinator, Jev and
plan configuration, existing schedules and Web version are preserved. The
original schema-10 journal remains healthy; no schema migration, real email,
business API mutation or production activation was performed. Live budget
counter deltas include other traffic and are not measured deployment effects.

## Explicit category removal (2026-10-09)

`POST /api/v1/categories/{id}/remove` removes an unused active or archived
category with current category-management authority and If-Match. Existing
DELETE continues to archive. All expenses, durable audits, original creation
responses, recurring schedules and persisted actual Jev selections retain their
category definitions. Removal never deletes associated records or their history;
the permission, revision and absence fences commit atomically with commercial
usage, and failed races roll back. No category schema migration is introduced.

After merging concurrent project-removal schema 10, the full suite passes
**1,731 tests and 60 subtests**. Static preview checks and source mirror checks
pass. `scripts/check-category-removal-native-runtime.py` passes 20 finite local
HTTP calls through the canonical Python Worker, NativeD1 and original-scope
ValidationBudget with native schema 10. Nine independent historical references
return CATEGORY_IN_USE without writes; active/archived unused removal succeeds.
Native metadata reports 406 metered reads and 208 writes, including isolated
fixture initialization. Financial/history hashes, account proof and accounting
remain intact; provider/model calls are zero. This is local synthetic evidence.

[Preview receipt](category-removal-preview-release-2026-10-09.json) records
Server version `012c2be6-2e25-4c58-9e2d-996b33bd35ef`. The existing schema 10,
bindings, database, singleton, schedules and Jev activation were preserved.
This publication performs no remote business mutation or category migration.

Updated 2026-10-09. Companion: [Web acceptance](../../../pullwise-web/docs/validation/local-acceptance.md).

## Preview operator Max trial (2026-10-09)

Published source `1302fb8` to preview and applied one exact real-account Max
trial through the requested Shanghai month end. The temporary config was removed
and persisted authority, expiry, audit, Jev flags and healthy journal were read
back. Original payment facts and production D1 pause were preserved. Remote
interval observed 293 reads / 19 writes (including other preview traffic); no
model or payment was called. See `preview-plan-grant-release-2026-10-09.json`.

Preview administrators can issue a bounded complimentary Max grant through the
existing singleton coordinator or its trusted configuration binding. Exact user
ID, GitHub login and email must match. One atomic account/audit/authority batch
preserves provider facts and all ledger data; replay adds no writes. Expiry
restores the current billing plan without deleting account or ledger content.
There is no public grant endpoint and production remains paused.

The merged schema-v9 source passed 1,602 tests and 56 subtests. Seven isolated
native Worker requests verified grant application, zero-write replay/profile/
Billing reads and the exact expiry boundary. The original singleton journal,
15 business-table hashes and payment/account fields were preserved. Native
Miniflare omitted attempt metadata, so read reservations were retained; no
attempt value was fabricated. No payment or model provider was called.

## Recent operation history (2026-10-09)

Project and Shared pool history identifies the actual actor, full timestamp,
affected record and changed values for the Server-enforced rolling last 24 hours.
Successful expense, project-setting and recurring operations log immutable
snapshots in their existing guarded transaction. Replays, unchanged values,
failed CAS/authority and failed history inserts create no partial business or
audit result. Restricted keys see only authorized expense history; actor data
contains no private email, key hash or authentication token. Original financial
audits remain intact. Bounded 16-row retirement uses existing mutation and idle
hourly maintenance paths, without GET writes or a new trigger.

The complete Python 3.10.12 suite passes **1,569 tests**. Static contracts,
preview configuration, deployment syntax and the full Worker import closure
pass. The verified preview package contains the exact entry and 53 canonical
modules, plus the unchanged pinned SDK and tzdata. Older test fixtures were
upgraded without excluding their assistance, quota or replay regressions.

[Schema evidence](activity-schema-native-local-2026-10-09.json) proves v8-to-v9
in five local requests and two actual restarts, with all 21 prior table hashes
unchanged. Migration native accounting is 453 reads / 5 writes. An injected
failure rolls back the extension and retains its stop/reservation without replay.
The [reviewed plan](activity-schema-upgrade.md) records all finite bounds.

[Native workflow evidence](activity-log-native-runtime-local-2026-10-09.json)
passes 55 of at most 60 local calls, 2,442 metered reads / 627 writes and an actual
restart. Separate read-only integrity probes observe 1,763 reads. It verifies
cookie and email-only identities, key redaction, before/after money, strict
24-hour boundaries, recurring generation, unchanged commercial charges and
idle cleanup preserving finance/rule/usage hashes. Missing native attempts stay
null. One post-runtime assertion typo and its offline correction are retained;
they caused no additional runtime request or retry.

These fixtures use synthetic local accounts and providers are never invoked.
[Preview release evidence](activity-log-preview-release-2026-10-09.json) records
bounded publication checks separately from real-account acceptance. The new
projection begins with this release; historical audit facts are not backfilled.
Production D1 remains paused and publication is preview only.

Preview version `b931d45f-507a-4ca5-8f81-e0b1ed366f47` is published. The single
health GET returns 200; readback confirms schema9/storage1, no stop, the original
database/coordinator and cumulative accounting. Existing bindings, secret names
and the hourly schedule are preserved. Production management readback retains
D1 access `0` and no schedules. The release receipt records seven finite HTTP
publication checks; interval counters include any concurrent product traffic.

## Invitation links and inviter approval (2026-10-09)

The Members flow creates an invitation link without a GitHub username. A real
signed-in applicant submits a join request; the original inviter's durable
inbox identifies that account and provides Approve/Reject actions. Pending or
rejected requests grant no ledger access. One approval consumes the link and
grants its selected role; repeat requests and status recovery perform no extra
membership writes. Removed membership cannot recover through an old link.
The complete Python 3.10.12 suite passes **1,330 tests and 56 subtests**.

[Native schema proof](invite-approval-schema-native-local-2026-10-09.json)
passes four finite local requests and one actual process restart. The atomic
v7-to-v8 upgrade preserves full-column hashes for all 20 existing tables,
legacy pending/accepted/revoked targeted invitations, financial and recurring
history, original journal markers and cumulative evidence. Its four migration
groups observe **1,042 rows read and 90 written**, matching journal deltas.
Restart dispatches no migration SQL. An injected failure after the invitation
table drop rolls back the entire batch and retains the v7 unknown-outcome stop
and full reservation; reconstructed journal retry dispatches zero SQL.
Nullable recipient, unique applicant, foreign-key and review constraints pass.
The [reviewed upgrade plan](invite-approval-schema-upgrade.md) records bounds.

[Native workflow proof](invite-approval-native-runtime-local-2026-10-09.json)
passes **23 local HTTP requests within a 30-request cap**, using the canonical
application, native Python/D1, product meter, commercial limits and SQLite DO
journal. Creation, actual applicant identity in the notification inbox,
rejection, approval, access denial before approval, read-only duplicates/replay
and access denial after member removal pass. Native **506 rows read and 210
written** match journal accounting; five commercial writes and six audit events
are recorded. Missing native attempts remain null; metadata is not fabricated.
These isolated fixtures use synthetic local cookie accounts, with no remote
accounts, provider requests, payments or forced ticks.

[Preview publication](invite-approval-preview-release-2026-10-09.json) records
Server version `be3c2974-dcf3-4a7c-937f-07c2da956fec`. One successful finite health GET
returns 200 and admits the reviewed v8 upgrade under the original coordinator.
Readback confirms **schema8/storage1 with no stop**, the original preview D1
and DO namespace, and retained nondecreasing cumulative counters. Remote
publication creates no synthetic account or business fixture and issues no
provider request or forced tick. No manual production deployment or D1
activation was performed; management readback retains production access `0`
and no cron. These publication checks do not claim an authenticated real-user
A/B invitation journey.

## Project links and recurring expenses: local acceptance (2026-10-08)

[Current receipt](recurring-links-local-2026-10-08.json) records 1,266 CPython
3.10.12 tests and 56 subtests, static contract/configuration/mirror checks and
pinned preview packaging. The original production D1 pause remains; only
preview has the hourly recurring trigger. Project URLs preserve existing
role/key/CAS/actor metadata guards. Cookie-managed recurring rules use a real
saved execution grant, calendar selectors and permanent rule/period identity.

[Native schema proof](recurring-schema-native-local-2026-10-08.json) passes four
finite local requests, populated v6-to-v7 atomic upgrade, exact historical-field
preservation, one real process restart with no replay, and isolated failed-batch
rollback/stop. It observes 492 native statement results, 3,036 rows read and 472
written, including separately labeled seed/integrity work. It does not claim
that added-column SELECT-star hashes stay identical.

[Native API and scheduled proof](recurring-links-native-local-2026-10-08.json)
passes 49 local HTTP requests, 24 mutation attempts, four official native
`/cdn-cgi/local/scheduled` dispatches and one real restart. Exactly three costs,
three permanent occurrences and three schedule audit events are generated;
tick creation counts are `[3, 0, 0, 0]`. Paused, ended, excess-catch-up,
archived-category/project and permission/idempotency fences retain their facts.
Native packaged tzdata 2026.5 is proven with an empty system TZPATH, the exact
Shanghai TZif resource and seven IANA conversions including midnight gap/fold
and a skipped day. Business/initialization observes 553 statements and
2,219/368 rows; trusted metered setup separately observes 28 statements and
41/53 rows, against its closed 916/53 reservation. Missing native attempt fields
remain null, with conservative reservations; no native metadata is fabricated.

The healthy original local singleton ends at schema7/storage1 with no stop and
unchanged canonical source. Three known harness/tool failures are excluded and
retained separately, never resumed or treated as acceptance. Native upgrade
rollback is distinct from CPython generation revocation/atomic-fence tests;
this run does not claim an injected failed native generation batch. No remote
D1 test, provider request, real account, real payment or forced remote tick is
part of these local receipts. Preview publication is recorded separately below.

[Preview publication](recurring-links-preview-release-2026-10-08.json) deploys
Server source `200a987955c1545f0542a4e30424967c85f70d66` as version
`c402fee1-d2ea-4f97-836b-8fc6db5ee4f7` and Web source
`6ae2a6284dc70edb58cce65809f9d010ef19b175` as version
`cd31aba4-195a-40e5-b2cc-3222bf6cea13`, each at 100% preview traffic.
The original preview database, coordinator namespace and cumulative journal
remain in use. One eligible health GET admits the reviewed v6-to-v7 upgrade;
health and the separate DO-only status return 200, schema7/storage1, no stop
and nondecreasing reservations/observations. Interval deltas can include
concurrent product traffic and are not attributed exclusively to migration.
Management confirms exactly the hourly `0 * * * *` trigger. Scheduled generation
is proven natively locally; no remote forced tick or synthetic expense is created.

The companion Web acceptance records 700 tests, three complete native browser
contexts, 102 scenarios and eight visually reviewed captures, including one
retained capture from a separate partial context. Four unauthenticated static
GETs return 200, match the accepted entry/asset hashes and retain preview
noindex. These are publication checks, not authenticated customer/provider
acceptance. Production receives no deployment or D1 activation in this release,
and its checked-in D1 pause and no-cron configuration remain. Receipt follow-up
commits are documentation-only and need no redeployment.

## Product copy and contract documentation audit (2026-10-08)

Current product documentation uses Pullwise / pull-wise.com and describes a
project expense ledger for developers and teams. Named standalone projects,
optional repository associations, shared-ledger roles and Owner allowances,
separate currencies and conditional Max assistance agree with the implementation.
Current v6 and enabled Preview descriptions replace stale v5 and pending-release
summaries; dated historical evidence remains intact. Ledger expenses remain
separate from platform subscription payment facts.

The OpenAPI changes affect only its title and descriptions. Server runtime,
schema, migrations, deployment switches and provider settings are unchanged.
Python 3.10.12 passes the complete **1,015-test** suite. S01 static checks,
Worker-mirror verification, deployment shell syntax, diff whitespace checks and
the pinned Preview Python Worker packaging dry run all pass. The initial
sandbox test run lacked loopback socket permission; the complete suite passes
with that permission enabled. No remote business request or database operation
is part of this audit. Preview publication is recorded separately below.

Preview source commit `9349b48eb043d9b0dc566cae15a63265b7db510f` is published
as version `ead2b046-b7df-4869-b8d3-c5dbb84e33f1` at `preview-api.pull-wise.com`.
Management readback confirms 100% traffic, the original database/budget namespace,
existing runtime settings and `python_workers` compatibility. All 48 audited
Server source modules remain unchanged. An initial upload using an external
config directory omitted the managed SDK and was rejected before publication;
the identical overlay in the Python project directory packages the SDK and
publishes successfully. No dependency/SDK upgrade, schema migration or remote
business request was used; the temporary config is removed and pylock restored.
The companion [release evidence](../../../pullwise-web/docs/validation/copy-audit-preview-release-2026-10-08.json)
records both versions and the Cloudflare 1010 block on Web's finite static check.
Public-page and real-payment acceptance are not established by deployment
management readback. Production stays paused. These evidence updates are
documentation-only and require no further deployment.

## Final real REST and temporary-key acceptance (2026-10-07)

[DFerryman's consented real preview run](rest-final-real-preview-2026-10-07.json)
passes **28 business HTTP requests**, spaced at least five seconds, with six
mutation attempts: one temporary key issuance, four exact `INSUFFICIENT_SCOPE`
rejections and one issuer revocation. The key expires after 900 seconds and
contains only the five default read scopes, one existing project allowlist,
its owner's workspace and `shared: false`. No project, category or expense was
created or changed; no real payment or model request was made.

Profile, project list/detail, categories, one-day expense list, all three
reports and CSV return their expected success. Shared-pool reads/export,
workspace override, the known QA ID and a second existing personal project
outside the allowlist are forbidden. Key management requires the issuer's session; missing authentication
is rejected. Project/category/expense writes and standalone suggestions reject
the absent write/use scope before resource/model work. The known QA project ID
proves an allowlist rejection, not the continued existence of a foreign project.

Issuer revocation succeeds; the token disappears from the active metadata
list and immediately returns **401 UNAUTHENTICATED** on the next request.
The sole consented session/token existed only in process memory; the receiver
has exited and cleared them. Sanitized evidence retains no credential or raw
financial body. This one-key real run covers five present read scopes and four
absent write/use scopes; all nine-scope/four-role issuance/principal matrices
and additional write-success paths are separately local SQL/native evidence.

[Temporary helper cleanup](rest-final-helper-cleanup-2026-10-07.json) is verified:
the exact consent route, Worker and its own Durable Object namespace are absent;
the original product budget namespace remains present. The receiver has exited
and six local helper control files have been removed. One anonymous, DO-only
post-acceptance budget read confirms schema v6 and no permanent row cutoff.
The interval added 906 observed D1 rows read and 25 rows written (reserved
906/35); these counter deltas may include concurrent activity and are not a
per-request attribution. No budget reset or repeated test loop was used.

## Blank projects and REST permission coverage: local acceptance (2026-10-07)

Name-only project POST now creates a standalone project with true NULL GitHub
repository/full-name/Organization fields, empty repository bindings and
`githubAccess: not_linked`. Normal GitHub account login remains. Nonblank names
are required for standalone projects; explicit `githubRepoIds: []` detaches all
repositories and clears Organization association. Later explicit association
uses the acting member's real GitHub grant. A lost grant on a linked project
still fails closed. No extra compatibility route or fabricated repository ID
was added. OpenAPI is version 0.3.0 and matches these rules.

Standalone creation, reads, all-standalone pages, rename, archive/reactivation
and new/moved expenses avoid GitHub calls including token unseal. The atomic
expense target guard checks active project revision, NULL anchor and absence of
bindings alongside quota, audit and idempotency writes. Binding races roll back
the complete financial batch. Existing role/key target restrictions remain.

Final full checks pass **1,015 tests plus 56 subtests**, source-mirror verification,
static deployment checks and release shell syntax. A local socket-restricted
full run initially failed one transport test; the authorized local-network
rerun passed. [Pinned preview packaging](blank-projects-packaging-2026-10-07.json)
passes Wrangler 4.136.3 / pywrangler 1.17.4 dry-run, 78 modules/601,686 bytes, with
external Node networking blocked and uv offline. Production D1 remains paused.

[Native authenticated API flow](blank-projects-native-2026-10-07.json) passes 49
local HTTP requests/18 mutation attempts, 495 native statements/482 reads/116
writes. Four synthetic roles exercise blank creation, restricted key writes,
exact replay, shared/project moves, exact reports/CSV, archive/history edit,
reactivation, attach/lost-access/detach and token revocation followed by 401.
Every standalone provider assertion is zero; ten linked-case GitHub fixture
operations are deterministic and are not real provider calls. The earlier
bare-array fixture error and accounted Free-plan 429 are retained; cumulative
local API totals are 97 HTTP/35 mutation attempts/950 reads/219 writes, excluding
schema seeding, final SQL snapshots and one stopped-fixture cleanup row.

[Populated v5-to-v6 native upgrade](blank-projects-schema-native-2026-10-07.json)
passes under the original-style DO journal: 633 migration reads/49 writes,
unchanged full-field digests of all 18 tables, two coexisting NULL-anchor
projects, enforced foreign keys and an actual restart with zero upgrade replay.
Injected failure after parent DROP rolls back the entire atomic batch and
retains the unknown-outcome stop and full reservation. Local absent native
attempts stay null; no metadata value is fabricated. The one-shot preview flag
is independent of earlier migrations and preserves the existing DB, namespace,
state-record cutover and cumulative counters.

[REST permission coverage](rest-permissions-coverage-2026-10-07.md) adds 68 local
key tests: all nine scopes across four issuing roles, both credential headers,
individual read scope success/denial, project/category write success and
synthetic suggestions-use success with role/target denial. Native and ingress
checks separately exercise authorization race fences and cookie boundaries.
This closes local coverage gaps; it does not claim the single real-account key
will reproduce every role/scope combination.

[Actual preview publication](blank-projects-preview-release-2026-10-07.json)
serves Server source `38348d071071ec1a117e373efabd82b9e1974e93`, version
`71e18ae0-f7af-4988-b8d9-7955ae5171f1`, at 100% traffic, with the original
D1/ValidationBudget namespace, all prior variables and five inherited Secrets.
The only added runtime flag is the preview v6 upgrade. Production's management
read-back remains `PULLWISE_D1_ACCESS_ENABLED=0`. No production migration or
product namespace reset occurred.

One anonymous Chromium budget preflight reports v5. After deployment, exactly
two Chromium GETs report health200/ok and budget200/schema6, stateStorage1 and
no stop. Across that release window observed read/write counters increase by
1,109/60 and reservations by 38,820/296; these interval deltas may include
concurrent preview traffic and are not all attributed to the upgrade. Normal
product limits remain null and cumulative evidence is retained. Two preceding
Python transport failures and one successful Node transport preflight are
preserved, with no migration replay. Including Web's four static GETs, ten
public verification GET attempts were made. No payment or model call occurred.
The companion's seven built-browser cases are accepted; the consented DFerryman
final REST/token-revocation result remains separately recorded.

## Account storage published; real-user acceptance complete (2026-10-06)

Server source `58f78d11dbe60141175593e379cbb40c977d01e9` is on main and
preview `ab500595-328d-4fc2-b897-7c6137659c92` serves 100% traffic. Shared
identity and billing JSON maps now use exact individual records in the same
D1 table, with typed user snapshots up to 512 KiB and unchanged 8 KiB HTTP
ingress. Full Server checks pass **893 tests** plus mirror/static/shell/dry-run.
The Web runtime and its earlier 434-test artifact remain unchanged.

[Native acceptance](state-records-native-2026-10-06.md) passed legal legacy
cutover, actual workerd restart, old sessions/roles, 500 users/1,000 sessions,
1,000 repository entries/100 billing history events and signed subscription
success/failure/replay. It preserves both harness failures and their bounded
continuation; no bulk seed or cumulative counter was reset. These are local
synthetic accounts, with no remote D1/provider or real payment requests.

[Actual publication proof](state-records-preview-release-2026-10-06.json)
retains the original preview D1/DO namespace and Secret names, production
access 0, schema 5 and all cumulative counters. Six finite anonymous GETs
including pre-publication status observed stateStorageVersion 0→1 and healthy
ingress. The migration window added 63 observed/73 reserved writes; the
subsequent health window added zero writes. These are global snapshots, so the
migration-window delta is not presented as exclusive request attribution.
The [capacity review](preview-state-capacity-review-2026-10-06.md) retains
its original pre-repair blocker and separate published follow-up provenance.

The [first two-real-user run](projects-two-real-users-baseline-2026-10-06.json)
passed ten checks across Projects, reports/CSV, invitations and Viewer access.
It stopped before the Editor update because the runner kept an old Owner
Members view after recipient acceptance. The failed result remains unchanged;
all confirmed QA fixtures were cleaned up.

The [separate focused real run](projects-two-real-users-focused-2026-10-06.json)
passed all six checks after both accounts explicitly renewed consent. SanChai20
was Owner and DFerryman was the invited member. Actual UI checks covered Viewer
invitation acceptance, Owner reload and Editor role change, and shared-expense
creation/editing. REST checks verified Admin project reads, revision-bound key
invalidation and member removal blocking workspace sessions/keys while retaining
the personal ledger. The run used 89 business HTTP requests, 150 total network
requests and 12 mutation attempts/confirmed writes, with at least ten seconds
between mutations and no client retries. New QA expense, category, invitation,
membership and key cleanup completed. No real payment, model call, screenshot,
trace, session file or raw financial evidence was persisted.

The [temporary helper cleanup](real-account-helper-cleanup-2026-10-06.json)
removed the exact route, helper namespace and Worker. Management read-back found
only the original product coordinator namespace, preview D1 access 1 and
production D1 access 0. These finite checks cover the listed release workflows;
they are not a load test or an actual charged payment acceptance.

## Earlier Projects, collaboration and cost publication (2026-10-06)

This earlier publication used Server `80f398c3d1e332aa91eefe2d5ccf885c227b14cc`
and Web `4e72574c19daec85ba713dd2d14a40034f7c03e7`, pushed to main.
Explicit preview publication serves Server
`7882f562-8c5e-4c5a-a898-d52943dc63e6` and Web
`ea62c7a0-37e4-4693-b78a-bbb3c96c53b6`, both at 100%.
Management read-back retained the original preview D1 and fixed coordinator
namespace, service binding and secret names. Production D1 remains 0.

Final full checks pass 835 Server and 434 Web tests, lint/build, source mirror,
offline configuration/static checks and shell syntax. Repairs cover project
archive/reactivation and revision-safe settings drafts, completed-invite
recovery, member permission isolation, billing lifecycle/redirect/error
boundaries, input envelopes and larger bounded CSV exports. Native local
Worker/SQL fixtures and built Chromium fixtures are separate evidence from
real user sessions: see [Projects and subscriptions](projects-subscriptions-native-2026-10-06.json),
[CSV](bounded-csv-native-2026-10-06.json),
[journal, limiter and cardinality](preview-operation-journal-native-2026-10-06.json),
and [payment audit](billing-subscriptions-2026-10-06.md).

Normal preview product operations no longer use lifetime test request/row
ceilings. Original cumulative counters and all integrity stops remain;
short-window DO-only IP/credential/actual-user limits return 429 with
Retry-After. Verified reads skip repeated cardinality scans, writes still
refresh after dispatch, and Status uses initial/manual checks. Native DO
cursor cost measurement and the USD 200/month planning target are in
[cost plan](cloudflare-cost-plan-2026-10-06.md).

[One actual public Chromium run](preview-public-release-2026-10-06.json)
verified matching deployed asset hashes, healthy schema 5, preserved cumulative
counters, manual health refresh and a 390px mobile layout. It forwarded five
read-only API requests and no mutations. Concurrent user OAuth activity
overlapped the run: global writes increased by three, matching one guarded
OAuth state/session transaction. Source audit finds no writes in health or
session reads, but aggregate snapshots cannot prove attribution. The original
zero-write gate remains recorded as failed; it is not rewritten as a pass.

Two real-user acceptance was a separate bounded gate at this publication.
The subsequent baseline and focused results above preserve its actual outcome;
it is not established by any local fixture or the public smoke above.

## Shared ledger and multi-repository release (2026-10-06, final)

The original audit, real consented preview lifecycle and 36 actual Jev cases
were completed before this new version. Server/Web now implement stable Owner
workspaces, Admin/Editor/Viewer invitations and revisions, actual-actor GitHub
access, up to 30 repository bindings and optional Organization metadata. Owner
plans/limits/model budgets serve each ledger; account billing remains personal.
Old ownership, finance history, project IDs and unscoped personal keys remain.

Full Server tests passed **658**; Web passed **418**, lint and build. The last
layout repairs passed **159** relevant UI tests and a final build. Native local
Python Worker/D1 passed **35** finite HTTP requests across four roles, invitation
accept/replay, revision-bound keys, distinct actor idempotency, exact reports,
251-row CSV and workspace isolation. These role scenarios use synthetic local
accounts, not two real remote users. Native schema/budget measurement used four
real batches, **324 read / 25 written**, below its retained **4,852 / 170** plan;
Miniflare's missing attempts stay null with pinned no-retry-source provenance.
The first stopped local claim remains unchanged, with no deployed bypass.

Source commits `a799232f` (Server) and `775fabdc` (Web) were pushed to main.
[Server CI 37439922368](https://github.com/GoPullwise/pullwise-server/actions/runs/37439922368)
passed. Explicit preview deployments serve Server
`6a69026a-993e-4afd-849b-671804288211` and Web
`926cf9ce-9beb-4cec-80eb-02d233e16a6e`, both at **100%**. Management read-back
confirmed original D1/fixed DO, inherited Secret names, service binding/assets,
Preview mode and Jev 1/1. The first proxied health request returned **200**.
Its exact compiled v4→v5 upgrade passed the deployed strict read-attempt gate,
completed 0005 and the 18-table health check under the same cumulative journal.
It retained **170** additional write reservation and observed **25** new writes;
no expense/history rewrite, namespace replacement, counter reset or retry occurred.

The final DO-only snapshot is schemaVersion **5**, schemaReady=true, stopped=null:
reserved **33,358 read / 719 written**, observed **21,716 / 359**, limits
**100,000 / 1,000**. Subsequent manual traffic continues those same counters.
Final built Chromium cases and actual deployed anonymous home/Members→Login
checks loaded `index-D2iP-fc1.js`; the mobile entry is 390px with no page errors.
The home harness stopped only on a known blocked Cloudflare analytics script;
its verified home/layout/source result was retained and not replayed. The
remaining Members case then passed with that external resource intentionally
blocked. No OAuth button, additional model/payment/GitHub provider or remote
finance/member write was executed. Original real authenticated acceptance
remains in its separate dated record.

Production D1 and Jev remain 0. Main Builds may publish paused code; no remote
production activation, schema migration or business acceptance was performed.
Current release details are in [release JSON](workspaces-preview-release-2026-10-06.json),
[native application](workspaces-native-local-2026-10-06.json),
[native upgrade measurement](workspaces-upgrade-native-local-2026-10-06.json) and
[minimum GitHub App permissions](../design/github-project-ledger/github-app-permissions.md).


## Resumed product audit (2026-10-06)

The user's current request explicitly supersedes the historical stop-testing
instruction: inspect, repair, verify, push main and publish, then perform preview
user acceptance. The implementation and source checks below are complete;
publication and real-provider/browser results are recorded separately.

- Expected project/category conflicts and deterministic project, record and
  write limits return business errors before a failing native SQL dispatch.
  Preview wrapper regressions verify the journal stays healthy. Atomic credential,
  usage UPSERT and uniqueness fences remain authoritative for concurrency.
- NUL JSON, invalid/oversized decoded and raw URL paths/query values, invalid
  project filters and unsafe/noninteger API-key expiry fail before D1 admission.
  Raw percent-encoded paths cannot bypass the 8 KiB parameter envelope.
- Jev Choice option order is preserved; state plus the longest question has its
  own conservative byte envelope. The gateway revalidates requests, closes
  rejected/interrupted bodies and rejects missing/blank credentials. Versioned
  questions state the task and uncertainty boundaries more precisely.
- [TypeSafe review](typesafe-jev-review.md) documents official API/model sources,
  suitable classification tasks and 36 agent-authored en/zh evaluation inputs.
  All 36 actual pinned-model responses passed the independent quality gate;
  the review contains normalized provider evidence and reproducible metrics.
- The deployment script uses locked pywrangler/Python packaging, source mirror
  generation and the pinned local Wrangler. Unused TypeSafe SDK/httpx2 runtime
  dependencies were removed; the native gateway remains the Worker transport.
  Preview/prod isolation, database IDs, journal ceilings and runtime flags are
  unchanged. No migration is needed for these repairs.

Final Python 3.10.12 reference verification passed **476 tests**, including the
release-command fixture, actual preview-wrapper denials and new input boundaries.
Source sync/import closure, default/preview/production static checks and shell
syntax passed. Web passed **339 tests** and eight finite actual local Chromium
cases on desktop/phone; its companion distinguishes mocked APIs from providers.

One preview read-only journal GET before publication returned: reserved
**24,599 read / 416 written**, observed **17,557 read / 251 written**,
schema ready, no stop, ceilings **100,000 read / 1,000 written**. It did not use
D1 or reset accounting. The old cumulative request gate was already removed;
Shared Pool owner isolation and explicit key permission remain product security.
Production D1 stays paused. Preview Jev flags were initially off pending actual
quality/runtime evidence; the later preview-only activation is recorded below.
Cloudflare CLI browser/device OAuth completed without requiring the
user to configure an API token. The user's later scope is preview-only testing
and verification; no further production repair, publication or acceptance is
part of this continuation.

Four migrations passed against a fresh local D1 directory only. The first local
Python Worker startup failed fetching its runtime bundle because direct network
access is unavailable. Recovery downloaded the official 13,727,600-byte
`pyodide_314.0.6_2026-08-17_6.capnp.bin` through the inherited proxy into a
temporary runtime cache; SHA-256 matched the embedded Workerd integrity value.
No route/proxy policy or product source was changed for this recovery.

The final actual Python Worker/D1 fixture passed **17 finite loopback HTTP
requests**: Cookie/Bearer reads, 251-row paginated CSV and spreadsheet-formula
escaping, category/expense creation, exact replay, PATCH and stale 412,
three report endpoints, repeated soft deletion/404 and recalculated totals.
The fixture, its credentials and migrations stayed local; remote D1/provider
calls were zero. Preview and production locked packaging both passed, with
**430.59 KiB upload / 99.84 KiB gzip** after removing unused dependencies.
Source `2ed7271ca058e6778aee372e93acb8d8f4fe7b89` was pushed to Server main.
Its GitHub CI run [37422418979](https://github.com/GoPullwise/pullwise-server/actions/runs/37422418979)
completed successfully. Preview was published with version
`b385df9f-f7f9-40e9-90da-26e27b1bcb57`; the management deployment read-back
confirmed 100% traffic. A finite direct preview health GET returned HTTP 200,
`ok=true` and D1 configured. The unchanged fixed journal subsequently reported
reserved **24,663 read / 416 written**, observed **17,621 read / 251 written**,
schema ready and no stop. These are cumulative all-traffic snapshots.

Web main source `0ff502cdea177f4d17cf08aaf35e818dd4ecf27d` was published to
preview version `ef9edfbe-f881-4f7a-982a-8fc1d75ace71`. Its homepage and three
current hashed assets returned 200; asset SHA-256 matched the local build and
the preview HTML retained noindex. The Web companion records actual public
browser evidence and distinguishes it from the synthetic local ledger journey.
Authenticated browser and real model evidence are recorded separately below.

## Current preview release (2026-10-06)

Web code `ceb4424977193f6783a70e3e3cc5251100957e55` corrected the mobile
homepage cascade; the actual 390px preview browser showed readable 288px purpose
cells, a 386px ledger card and no horizontal overflow. Preview Web version
`d3b560ec-ba80-4bea-a428-3ad137cad571` serves that build. Its four finite
homepage/asset GETs passed content hashes; the follow-up phone batch had 13
requests, all HTTP 200. The companion records the screenshot tool's touch-reset
limitation and the restored native touch measurements separately.

The [real Jev evaluation](typesafe-jev-review.md) completed 36 actual requests,
all valid, with zero retries or false actionable hints. Each language/task
correctly suggested 16/16 clear cases and withheld 2/2 uncertain cases; all six
option-order pairs agreed. These small synthetic results support preview
advisory use and do not establish customer-data accuracy.

After independent offline recomputation, the unchanged canonical Server source
`2ed7271ca058e6778aee372e93acb8d8f4fe7b89` was published to preview with the two
Jev flags overridden to `1` using locked Pywrangler. Server version
`a7470c2d-55f7-4f23-a3e0-2f920b6ef4b6` received 100% traffic; management
read-back confirmed mode `preview`, both flags `1`, the original D1 database and
fixed validation namespace, and the TypeSafe Secret name. No migration, journal
reset, production deployment or provider call was part of this activation.

At the first activation, the checked-in config still defaulted both flags to
`0`, so that release used an explicit preview runtime overlay. The subsequent
migration-readiness repair records both reviewed Preview flags as `1` in source
to preserve enablement on ordinary deployments; production flags remain `0`.
At this deployment stage, authenticated end-user acceptance was pending. That
gate was subsequently completed in the finite user-consented run below.

## Authenticated preview acceptance (2026-10-06)

The user explicitly shared their existing Preview login through a temporary
first-party consent page. A private one-shot WebSocket receiver placed only the
canonical session Cookie in an in-memory Chromium context. It did not persist
the Cookie, copy GitHub credentials, create a remote user or alter entitlements.
The actual account was Free, so no additional model requests were made.

The actual published Shared page loaded authenticated, showed no desktop
overflow or page errors, switched Reports/Expenses with selected-tab and visible
panel assertions, and opened, filled and cancelled an unsaved expense form.
The cropped screenshot retained only the agent-authored draft; project/category
choices were masked. The five writes were separate real same-browser Cookie
REST requests, rather than UI Save clicks:

1. Create an isolated QA category and confirm its filtered ledger is empty.
2. Create one explicit Shared expense for USD 0.01; verify `amountMinor=1`.
3. Edit only that new expense with `If-Match: "1"`; verify revision 2.
4. Verify filtered Shared/account totals and exactly one CSV row, then delete
   with `If-Match: "2"`; verify detail 404, empty filtered list/totals/CSV.
5. Archive only the new QA category with its original revision.

The receiver admitted **36 network / 21 business requests, five writes**, below
the prewritten **95 network / 44 receiver-business / five-write** ceilings. The
helper made one additional read-only authenticated-session check, within the
combined 45-business ceiling. All 13 required REST results were asserted; the
response observer retained 20 business responses, so the count of admitted
requests is not presented as 21 completed HTTP responses. One admitted read had
no retained response and its completion status is unknown. Source shows the
draft form issues an abortable profile read cancelled on unmount, but the log
does not prove that caused the missing observation. Two third-party
font/analytics requests were deliberately blocked by the harness. There were
zero application page errors, retries, provider calls, payment operations,
GitHub mutations or changes to existing records.

The test expense is soft-deleted and the QA category archived. Normal audit and
idempotency records remain, one cumulative record slot and five commercial
write operations were consumed, and no counters were refunded or reset. The
test Cookie was cleared and the browser closed before the 15-minute deadline.
[Credential-free results and the original admission manifest](preview-authenticated-2026-10-06.json)
record the exact boundaries and results.

The first consent-page attempt was rejected before session validation/sharing:
the page's `no-referrer` policy made a native form send `Origin: null`. Actual
Chromium/native Workerd evidence reproduced that rejection and proved the
`strict-origin` repair; the exact Preview Origin check was retained. Sixteen
local helper cases and two built-UI receiver fixtures passed without outgoing
Preview/model traffic. A fresh consent capability and private runner were used
only after proving the first attempt made zero session checks or business writes.

After the successful run, the temporary path route, helper Worker and its
independent `SessionRelay` namespace were removed. Management read-back found
zero helper scripts/routes/namespaces and retained the original fixed budget
namespace. Generated private helper files were deleted. Canonical Server version
`a7470c2d-55f7-4f23-a3e0-2f920b6ef4b6` and both Jev flags `1` were read back;
the helper did not change either product Worker.

Final finite journal GET: reserved **28,042 read / 549 written**, observed
**21,000 read / 334 written**, schema ready and no stop. Before consent it was
reserved **26,865 / 469**, observed **19,823 / 284**. These are cumulative
all-traffic snapshots under the unchanged **100,000 / 1,000** ceilings, not
exclusive per-test D1 attribution. Authenticated Free Shared behavior and the
separate 36-case real Jev suite passed; paid checkout, fresh OAuth callback
assertions and production acceptance are outside this completed preview run.

## Migration-readiness follow-up

The user subsequently asked whether every original goal, including direct
engineering migration to production, was complete and instructed us to finish
that before the new version. That review found a real remaining gap: Default
rejected production even with D1 explicitly enabled, and the static checker
prevented any correct production activation configuration.

Both are now repaired while preserving checked-in production access `0`.
The [migration guide](production-migration-readiness.md) documents the separate
normal application path, isolated configuration checks and explicit future
activation option. Preview still uses its original budget and neither runtime
path loses authentication, owner/key restrictions or commercial quota fences.
Web production configuration now routes HTML through existing SEO/www middleware
with ASSETS; Preview assets and configuration remain unchanged. Jev Preview flags
are now versioned as `1` following the earlier real quality gate.

Server passed **497 tests**, Web **343 tests and build**, plus configuration,
source sync and shell checks. The initial full Server attempt lacked permission
for a single loopback socket; the properly enabled environment passed all 497.
The actual native production-mode Worker/D1 fixture then passed **22/22 local
HTTP requests**, including 251-row CSV/formula escaping, Cookie/Bearer, owner 404,
missing/foreign Origin 403, trusted Origin 201, stale 412, exact replay and the
monthly write-limit 429. It had no validation DO binding and made zero remote
D1/provider requests. Persisted read-only SQL confirmed five charged commercial
writes, no charges for denials/replays, 252 retained records/251 active, and zero
Jev reservations. Entry SHA-256 matched final source
`96badb49c227a886e2a5de9a07660e50a87443025a5e6d39c6ab40c42705a32b`.
The fixture stopped its runtime and released both ports; formal config hash was
unchanged. An initial offline inspector included Miniflare metadata.sqlite;
excluding that non-product database fixed the inspector with no HTTP rerun or
data reset. [Native local evidence](production-native-local-2026-10-06.json)
distinguishes that result from remote formal activation, which was not performed.

Locked Preview packaging passed at **430.99 KiB / 99.96 KiB gzip**. Final main
publication: Server `bccf80897206ff9e9ae5b9fb83bcf309504f7036`, Web
`26ed47ed9c375fdfd2c49ba86e906cebe5e9defa`. Server Preview version
`b9a372f7-fd79-4233-8c46-b97d08c9797e` received 100% traffic. Management
read-back confirmed the original D1/fixed DO, Preview mode, TypeSafe Secret name
and both reviewed Jev flags `1`. Web Preview remains
`d3b560ec-ba80-4bea-a428-3ad137cad571` at 100%; its actual browser/Worker source
and asset hashes are unchanged by the production-only config repair.

Two finite public Web-proxy GETs after the final Server publication returned
200: health `ok=true`, D1 configured, and anonymous session authenticated=false.
One subsequent journal GET returned schema ready/no stop, reserved
**28,144 read / 549 written**, observed **21,102 read / 334 written**. The fixed
100,000/1,000 ceilings were preserved. No remote schema migration, new model
request or repeat user consent was needed for this runtime-compatible release.
The earlier authenticated 13-step run tested the unchanged Preview application
path; the new normal-mode branch was additionally exercised by the native local
22-request fixture. No remote production business acceptance was performed.
The new multi-repository/Organization/team role requirements remain planning
only and have not altered this product release.

The historical evidence below describes earlier sources and authorization.

## Product audit and automatic Max assistance (2026-10-02)

The current audit covers platform checkout/subscription facts, REST authorization,
expense integrity and idempotency, exact reports, Web workflows, pricing and
public documentation. Every behavioral repair has a failing regression followed
by the same passing check; copy/documentation alignment follows the verified
behavior. No schema or database migration is required.

| Area | Verified repair |
| --- | --- |
| Max / REST | Ordinary expense POST/PATCH includes assistance for Max and expense-write keys; create can omit category. Confidence-gated existing categories only; explicit money, category and target stay authoritative. |
| Inference failures | Manual-category writes remain usable on provider failure, daily/monthly Jev exhaustion or absent configuration. Auxiliary model bookkeeping no longer consumes business-write slots; monthly provider reservation remains conservative. |
| Replay and authority | Caller intent is hashed before inference. Exact replay returns the saved result without provider/quota work, including after archival. Expired Max, restricted/revoked keys and concurrent category archival remain fenced. |
| Money | Split SQL integer aggregates reconstruct exact Python totals; totals exceeding JavaScript's safe integer use decimal strings. Project PATCH preserves totals. Native D1 converts safe Python integers only at the final JS bind, preserving exact MAX_SAFE_INTEGER and logical budget envelopes. |
| Payment | Claim upgrades before chargeable dispatch, retain unknown outcomes, wait for signed payment facts before granting Max. Verified product/subscription IDs and owner association are required; historical events cannot overwrite the current subscription. Missing terminal cadence preserves annual history. |
| Security | Trusted Origin is required for Cookie writes in all SameSite modes, including malformed Authorization. Jev rejects redirects and bounds timeout/body size. Provider exception text is excluded from preview diagnostics. |
| Preview admission | Daily 20-attempt and monthly USD exhaustion exit before optional D1 mutations. Long UTF-8 notes retain ordinary writes, compact audit snapshots and exact replay within narrowly reviewed 16 KiB expense-JSON bounds. |

Before the final Unicode repair, the full Python 3.10.12 suite passed **395 tests**, with cached PyYAML 6.0.3
available and one finite loopback fixture allowed by the execution sandbox.
The expiry/concurrent-archival cases are included in the same
17-case automatic-assistance suite. Default/preview/production static checks,
source mirror/import closure and shell syntax passed. Pinned Python 3.14.2 /
workers-py 1.17.4 / Wrangler 4.136.3 preview packaging passed; it includes the
new exact-money module.

Actual local Python Worker/D1 acceptance then exposed a native bind failure for
9007199254740991 that synthetic SQLite did not catch. The NativeD1 fix preserves
integer envelopes until the final FFI call; 11 regressions passed after their
failing baseline. A fresh isolated fixture used exactly **30 local HTTP requests**:
17 baseline cases, the failing maximum-value create, two bounded diagnostic
reads and ten post-fix cases. Maximum-value create/PATCH/detail, all three
reports, project list/detail/PATCH and a 1,025-row CSV passed. Shared total
`9232379236109515775` (above int64) and project KRW total `18014398509481982`
were exact decimal strings. Four migrations and fixture SQL were local only;
zero remote D1/provider calls ran. The temporary diagnostic was removed, source
hashes matched current source and the captured Worker was stopped.

Four bounded Cloudflare management GETs confirmed original databases, the fixed
preview budget namespace, preview product access 1, production access 0 and both
Jev flags 0. Both Workers lacked TYPESAFE_API_KEY at the initial inspection.
After user configuration, a names-only read-back confirmed the preview Secret
on 2026-10-02. Its value was not read back or logged. The user explicitly
authorized agent-authored test data for real-provider en/zh evaluation rather
than supplying customer samples; report that evaluation's provenance honestly.
Real Jev quality/runtime and real payment/login acceptance remain separate from
synthetic local tests. Do not claim the model is live or change production D1
pause based on this implementation evidence. Publication and CI evidence follow
after the authorized commit, push and deployment.

The enablement review added **12 actual-adapter-chain admission cases** and
**13 payload cases** using canonical SQLite schema under ProductMeteredD1 and
PlanLimitedD1. Monthly exhaustion and valid long notes initially caused a
persistent preview stop; daily-cap saves also incorrectly reserved more USD.
The same cases now pass without provider dispatch or USD consumption on known
exhaustion, while manual-category saves remain available. Timeout/uncertain
attempts retain their conservative reservation. Maximum 8 KiB ingress with
30 categories, long CJK/ASCII POST/PATCH snapshots and exact idempotency replay
pass; unrelated text/app_state retains 8 KiB limits, and invalid/oversized JSON
is rejected. These fixtures prove adapter composition and SQL admission, not
native billed row counts. No schema, migration or global stop policy changed.

A separate native Python Worker gateway proof passed **3/3** with unchanged
gateway/validator source and the full pinned SDK 1.9.0 inventory. One temporary
process served exactly three loopback POSTs against a closed outbound mock
that allowed only the fixed TypeSafe URL. Native to_js/Object.fromEntries,
headers, compact UTF-8 and AbortSignal construction passed. A 355-byte response
split inside a Chinese UTF-8 code point reconstructed exactly and passed the
strict validator; 302 was rejected without following, and a 65,537-byte stream
without Content-Length was rejected. No real provider, D1 or forbidden outbound
operation occurred; the process stopped and its ports closed. This proves
local native transport behavior, separately from remote model quality.

The final input repair rejects unpaired Unicode surrogates in every decoded
JSON string/key at ledger, API-key and Worker ingress. Eight failures reproduced
a persistent UNREVIEWED_SQL_BOUND before the fix; **21 new Unicode cases** and
**94 focused cases** then passed, including valid emoji round-trips. Schema,
SQL envelopes and global stop policy did not change. The user subsequently
requested stopping all testing and proceeding to commit, push and deployment;
no further test suite, runtime or model evaluation was run after that request.

The user authorized 36 self-authored en/zh provider cases under an approximately
$0.10 conservative ceiling. Temporary private Cloudflare previews never reached
provider readiness: full-SDK Python returned 503/1105, and JS isolation did not
complete authenticated startup. All captured processes stopped. Actual provider
calls and remote D1 operations were **0**; no model quality metric or gate was
claimed. Both Jev flags remain 0 for the requested repair publication.

### Product audit publication (2026-10-02)

Server source `630752fece96be1a5c0d5b8febddcf4734cdb689` was committed, pushed
and published after exact native-money acceptance. GitHub CI
[36973091088](https://github.com/GoPullwise/pullwise-server/actions/runs/36973091088)
completed successfully. Its preceding automatic-assistance commit `5f77941`
also passed CI
[36971220721](https://github.com/GoPullwise/pullwise-server/actions/runs/36971220721).

| Environment | Worker | Published version |
| --- | --- | --- |
| Preview | `pullwise-server-preview` | `5753a2c1-8798-4e70-876a-594c1b6c87cc` |
| Production, D1 paused | `pullwise-server-production` | `52f24bf6-ac8b-40f9-b7ec-86cf28243020` |

Management read-back confirmed each version at 100%, original database IDs,
the fixed preview namespace/migration tag and unchanged binding/configuration
baselines. The only additional production binding was the user's
TYPESAFE_API_KEY Secret; its name was verified, never its value. Production
D1 remained 0 and both environments' Jev flags remained 0. Publication involved
no D1 query/migration, business request or provider call. The companion Web
source `1bc0b57` was published to both environments and passed eight bounded
static GETs; its acceptance document records the versions and CI limitation.

## Preview repository identity failure (2026-10-02)

The user's existing browser record confirms GET
`https://preview.pull-wise.com/api/api/v1/repositories` returned 503 and the
Projects screen displayed `IDENTITY_UNAVAILABLE`. The double `/api` is the
documented proxy path. Source proves that GitHub non-success responses become
generic ValueError, the Worker masks all identity exceptions with that code,
and Web treats repository failure as a whole-page failure. These defects are
reproduced locally. The actual preview trigger (expired/revoked token, GitHub
outage/rate limit, crypto configuration, or identity exception) is **unproven**.

Read-only Cloudflare management settings confirm the original preview DB,
Client ID/slug, enabled preview switch and Secret **names only**. Observability
is absent, Logpush is false and there are no tail consumers. Historical logs
therefore provide no root-cause evidence. No logging configuration was changed,
runtime Secret value read, business request, login/sign-out, D1 query or budget request
was executed. Initial management connections failed; the bounded follow-up succeeded.
The previously supplied DO budget snapshot remains historical, not a fresh check.

The gateway now distinguishes credential rejection, permission denial, rate
limits, transport/5xx, invalid response and crypto/config failure. Rejected user
credentials return empty candidates with `reauthorization_required`, keep the
Pullwise session and never trust cached/partially loaded grants. Renewal uses
the existing explicit OAuth flow; ordinary GET/sync never refresh or write.
Unknown provider failures remain errors. Project reads hide GitHub names and
keep owner history available as `unavailable`; new targets fail closed, while
same-target historical edits/removal retain all owner/key/revision guards.
Preview diagnostics expose only fixed codes, exception type/function/line and
numeric provider status. Exception text, provider bodies and credentials are
excluded. No schema, migration, budget, namespace or runtime switch changed.

GitHub's [token documentation](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/refreshing-user-access-tokens)
states expiration is enabled by default for new Apps and expiring access tokens
last eight hours. That supports a hypothesis, not this user's token diagnosis.
The [REST troubleshooting documentation](https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api)
distinguishes rate-limit 403/429 from permission failures. No provider token was
obtained or tested against GitHub in this investigation.

Test-first evidence: 23 gateway/identity cases and the new history/target case
failed before implementation (24 failures / 8 existing passes); the first Web
batch failed five cases, and the unavailable-history UI regression also failed
before its notice was implemented. The completed Server suite passed **271 tests**.
Local synthetic SQLite
fixtures include setup/explicit-callback/write simulations; these are not
Cloudflare D1 operations or preview acceptance. Source mirror sync/`--check`,
default/preview/production static checks and shell syntax passed. Python 3.10.12
uses cached PyYAML 6.0.3 via PYTHONPATH; no dependency download was needed. The
full test run needs loopback socket permission for one finite HTTP fixture.

The user separately authorized publishing source `04c8797` to the preview Server
on 2026-10-02, then explicitly requested commit, push and deployment. Publication
uses these source modules and the safe Worker error handler, with the same
preview config/DB/journal and unchanged limits, without migrations or business
validation. Main pushes trigger the already authorized paused production Build;
`PULLWISE_D1_ACCESS_ENABLED=0` remains mandatory. A deployment or local mock pass
must never be described as real preview repository/login acceptance.

Source commit `04c87978c4e9bf17caa4cf4012fde83487200b7d` was pushed to main.
GitHub push CI [36961634876](https://github.com/GoPullwise/pullwise-server/actions/runs/36961634876)
completed successfully. The production settings read-back still has the original
DB ID and `PULLWISE_D1_ACCESS_ENABLED=0`. Cloudflare Builds status lookup returned
HTTP 403, so the automatic production Build outcome is unverified.

Locked Python 3.14.2 / uv 0.12.3 / workers-py 1.17.4 / Wrangler 4.136.3 preview
packaging passed. Missing local release tools and executable links were restored
without changing lockfiles or dependency versions. Preview publication succeeded
with version `ca964508-e9b5-45d6-8c5c-a373d3be8bdd`, tag `identity-04c8797`.
Management read-back confirms that version at **100%**, the same complete binding
set and environment variables, original DB
`e9dc3b89-f81f-4fce-87ef-d8797d879fb4`, budget namespace
`0e0ce4946c5e4d5f81c606273ba94b5d` and existing `validation-budget-v1` migration
tag. All four Secret names remain present; their values were not read.
Production management read-back again confirms its original DB and D1 access 0.
No runtime or budget endpoint was invoked after publication; current journal
counts were not sampled. Real preview identity/provider acceptance remains
pending, and the original upstream trigger remains unproven.

The companion Web source `28dfea0` was published to both environments and passed
eight finite static GETs. This does not establish the real identity trigger or
acceptance. This task performed zero Cloudflare D1 queries, writes or migrations,
zero remote OAuth/sign-out/provider/business requests and no budget changes.

## Preview cumulative request gate removal (2026-09-29)

The user explicitly requested removing the preview site's 200 cumulative
request cap, then committing/pushing/deploying. Enabled preview product requests
can now proceed beyond 200; request/case/row counters and evidence remain
cumulative. D1 ceilings remain 100,000 reads / 1,000 writes. Default/non-product
request limits and all SQL, deadline, concurrency and native-accounting guards
remain enforced. Only the existing inactive, schema-ready REQUEST_LIMIT stop
with matching complete evidence and in-budget counters receives one internal
policy marker and may resume. No counter, reservation or evidence is reset.
Unknown outcomes, active tickets, manual stops and row-budget stops stay closed.

Two regressions failed before the fix. The final focused suites passed 99 tests,
including 18 new request-policy cases: requests after 200, no-refund recovery,
restart persistence, environment boundaries, incomplete/invalid accounting and
both row ceilings. Source mirror, both environment static checks and shell
syntax passed. Full suite/preview packaging and publication evidence follow.
Remote verification is one DO-only status GET (zero D1 reads/writes, zero
provider/account requests and zero retries). Its first access may write the
approved marker in the existing DO journal, which is not D1.

Final full reference verification passed **241 tests / 22 subtests**, plus
source-mirror/static/shell checks and pinned Wrangler 4.136.3 preview packaging.
Commit `1be2353` was pushed to main; CI
[36551967847](https://github.com/GoPullwise/pullwise-server/actions/runs/36551967847)
passed the dependency audit, compatibility, shell checks and full test step.

Preview deployed version `722344d9-077c-4dfa-81af-4ac706ac2bc3`. One DO-only
status GET returned 200 and stopped=null. Limits remain 100,000 reads / 1,000
writes; reserved counts stayed exactly 11,605 / 243 and observed counts stayed
exactly 4,563 / 150. The request stop was cleared without refunding/resetting
usage or making D1/provider requests. Production access/config remains paused;
no production activation, database migration, cron or counter reset occurred.

## Preinstalled repository discovery and popup sync repair (2026-09-29)

The user has GitHub-authorized repositories but no candidates. A corrected
synthetic login/preinstalled-App regression failed against the previous committed
handler: its absent local Setup callback cache hid real provider grants. Web
popup completion also called an unimplemented POST `/repositories/sync` route.
Current discovery reads fresh App user-token grants from at most ten visible
installations, aggregates/deduplicates at most 1,000 repositories and reuses
those facts for project eligibility. Provider/lost-access failures remain
guarded, and GET/sync do not persist authorization cache or user/session updates.
Sync requires a valid cookie session and trusted Origin/Referer. API-key clients
still require projects:read. The prior scope test's stale empty fixture
expectation was updated to the now-visible synthetic authorized repository;
its insufficient-scope rejection is unchanged.

The full suite executed 223 tests / 22 subtests: 222 tests and all subtests
passed; the one old empty-fixture assertion was corrected and the corresponding
API-key, GitHub identity and product-budget suites then passed 28 tests.
Identity/project route checks passed 16 tests. Source mirror, both environment
static checks and preview packaging passed. No real provider or D1 validation
was performed. Publication will verify only one DO-only budget GET; its D1
bound is zero read/write, with no retries or authorization/provider requests.
The existing preview 100,000-read / 1,000-write caps remain unchanged.

Commit `dc6adf4` was pushed to main. CI
[36549930938](https://github.com/GoPullwise/pullwise-server/actions/runs/36549930938)
passed dependency audit/compatibility, shell/static checks and the entire current
test suite. Server preview deployed as `7001ca49-1c49-45b9-91f6-7d134e3d1324`.
One DO-only budget GET returned 200, stopped=null, limits 100,000 read / 1,000
written, reserved 10,332 / 223 and observed 3,290 / 134. No user/provider/D1
request was made by the publication checks; the counters include user activity.
User-specific real GitHub results remain manual acceptance, not synthetic proof.

## Main branch consolidation (2026-09-29)

The user explicitly requested merging the preview read grant into main and
using main for all subsequent work. The merge is a fast-forward of the already
tested/deployed `e9b8e0c` and `4a57a5a` commits; no runtime code or deployment
config changed during consolidation. Both repositories now use main. The
preview grant remains selected only by enabled preview-product mode; production
stays paused and default/non-product reads remain 10,000. No additional D1 or
provider validation is needed for this branch/documentation operation.

## Preview-only 100,000 read grant (2026-09-29)

The user approved raising cumulative Rows Read to 100,000, only for preview;
Rows Written remains 1,000. Default and non-product budgets remain 10,000 reads,
and production stays paused. One internal grant marker preserves the original
database/namespace, every observed/reserved counter, request count and evidence.
Only the existing budget stop with complete accounting can close its rejected
pre-dispatch ticket; unknown outcomes/timeouts/manual stops and later exhaustion
remain blocked. No read margin or write reservation is refunded.

Nineteen new regressions cover recovery without refunds, the original default,
all other stop reasons, incomplete/invalid/out-of-bound state, both hard ceilings,
repeat exhaustion and the actual Worker method's preview-only selection. The
first 15 tests failed before implementation; final focused budget suites passed
77 tests, and the new grant/deployment suites passed 27 tests with Git Bash.
The full suite ran all 218 tests and 22 subtests: 217 tests and all subtests
passed; one shell test failed because PATH selected WSL without python3. That
same deployment suite passed after selecting D:/Git/bin; no application fix was
needed. Python compilation, exact source mirror, both environment static checks,
shell syntax and pinned Wrangler 4.136.3 preview packaging passed.

Publication uses a separate preview branch because main triggers automatic
production Builds. The only post-deploy case is one DO-only budget GET, zero
retries, zero D1 Rows Read/Rows Written and no provider/account requests. The
first access may persist the authorized grant in DO SQLite, not D1. Deployment
and live limit/counter/stop evidence are recorded after publication.

Commit `e9b8e0c` was pushed on `preview/read-budget-100k-20260929`, preserving
main so its production Builds trigger did not run. Preview Server deployed as
`03cfe963-6888-4f19-ad39-79966656ff79`. One DO-only status GET returned 200,
limits 100,000 read / 1,000 written, schemaReady=true and stopped=null. Reserved
counts remained exactly 9,898 reads / 218 writes; observed counts remained
exactly 2,856 reads / 131 writes. No D1 query, migration, provider traffic, retry,
usage refund, counter reset or production deployment was performed. Preview
budget admission is restored; a real user OAuth journey remains user acceptance.
No CI run exists for this branch; the checked-in push workflow targets main.

## Preview login budget stop observed (2026-09-29)

The user reported 503 during GitHub login/authorization. Exactly one
`GET /_preview/budget` returned 200 from the preview Server's DO-only numeric
status path, before any D1/provider dispatch. It reported `BUDGET_EXHAUSTED`,
schemaReady=true, 9,898 / 10,000 reserved reads and 218 / 1,000 reserved writes;
native observations were 2,856 reads / 131 writes. Only 102 reserved reads
remain. These counters are application protection evidence, not a live
Cloudflare billing or CPU/memory quota measurement.

The 7,042-row reserved-minus-observed read margin matches the previous repaired
checkpoint (8,010 minus 968). That is consistent with retained legacy margins,
not proof that any of those reservations can safely be released. Source keeps
budget stops persistent and rejects subsequent product/login requests before
D1 access. Deployment/refresh does not reset the journal. No retry, account
request, provider call, D1 query, counter reset or ceiling change was performed.
Restoring preview access requires a separately reviewed and authorized budget
adjustment or an evidence-backed recovery allowed by the existing controls.

## Preview premature read exhaustion repaired (2026-09-29)

User testing stopped at 9,969 reserved reads / 164 reserved writes, while native
observations were only 955 reads / 86 writes. Commit 44f9fe5 preserves the same
database, coordinator, 10,000-read / 1,000-write ceilings, observed counters,
request counts and old product reservations. The complete original non-retried
schema batch releases its unused read margin once; new complete single-attempt
product batches settle unused read margin only. Retry/missing/ambiguous metadata
keeps full read reservations and all write reservations always stay charged.
There is no public reset endpoint or general recovery from unknown outcomes.

Test-first: five regressions failed before implementation; final focused suite
passed 62 tests. Full reference suite passed 198 tests / 22 subtests before the
final write-settlement replay test was added; that test passed in the focused
run. The earlier CI migration fingerprint failure was reproduced locally and
fixed by hashing canonical LF bytes on Windows and Linux. Application commit
44f9fe5 CI [36525624836](https://github.com/GoPullwise/pullwise-server/actions/runs/36525624836)
passed. Source mirror, production/preview static checks and preview packaging
passed; no additional native local product journey was run for this correction.

Preview Server published version 8863ad4d-dc41-4d31-b95c-75ac16eabc63. One
unauthenticated Web session GET returned 200; one subsequent DO-only budget GET
reported stopped=null, schemaReady=true, **968 observed reads / 86 writes** and
**8,010 reserved reads / 164 writes**. This finite check added 13 reads and zero
writes, with exact single-attempt settlement verified by the resulting counters.
Old ambiguous/retry margins were not reclaimed. Remaining conservative headroom
at that checkpoint is 1,990 reads and 836 writes, not an unlimited test allowance.
No provider call, data creation, cron or polling was part of this validation.
The earlier preview publication checkpoints below remain historical evidence.

## Product preview is active (2026-09-29)

The user explicitly requested all preview product paths to be usable now,
retaining the 1,000-written / 10,000-read cumulative cap. Product-wide mode
replaces the empty HTTP case list with per-SQL durable reservations, unique-key
write validation, index/guard bounds, cardinality checks and a serialized queue.
It automatically initializes only the frozen empty preview schema inside the
existing fixed budget journal. Production remains at access 0, verified through
Worker settings; the original preview DB and namespace were preserved.

Reference full suite passed **189 tests / 22 subtests**, 279.46 seconds. Later
targeted deployment/budget/provider regressions passed **74 tests**. One Windows
focused run chose WSL bash and failed to find python3; explicitly selecting Git
Bash passed all eight deployment-contract checks. Native local Python Worker/
D1/DO ran 15 product checks including login/install, profile, create/edit/delete,
list/report and CSV, plus two simultaneous session reads. Before those reads:
464 native reads / 131 writes, 7,847 / 232 reserved, no stop. CSV is eagerly
consumed inside the preview ticket (1 MiB cap), preventing late unmetered pulls.

Preview Web was deployed as 66ff72ed-cd94-4cf0-89f2-9c9af21d71cc. Preview Server
current version is 9f2fef3b-e4e8-4577-b899-87271df4c67a. A finite Web auth/session
check returned 200 authenticated=false; homepage returned 200 HTML and noindex;
GitHub authorize returned 200 with github.com target (state/URL not logged).
These checks do not claim a real browser/OAuth callback or completed payment.

The real test catalog initially failed before making a provider request:
Workers rejects fetch redirect='error'. GitHub and Creem gateways now use
manual redirect and reject non-success responses without following redirects.
The regression failed before repair and passed after. A metadata deployment
failed due to connectivity; its bounded retry succeeded without data/budget
reset. Creem test catalog then returned **200, provider=creem, enabled=true**.
No checkout or actual payment was started by the agent.

One final read-only /_preview/budget check reported **165 observed reads / 67
written**, **3,554 reads / 135 writes reserved**, schemaReady=true, stopped=null.
This status is read from DO storage, not an extra D1 monitoring query. It exposes
only budget numbers/readiness/stop reason, no account/token/SQL parameter data.
This is the recorded checkpoint, not a claim about future user/account traffic.
The same hard cap applies to subsequent manual requests and provider callbacks.
No cron, polling, application retries, reset or new namespace was added.

Jev remains unavailable without its separate real key/quality gate. Real user
GitHub callback/repository use, complete test payment/webhook and browser visual
acceptance remain to be exercised. Provider/HTTP business errors do not close
all product paths when native D1 accounting is complete; ambiguous D1 outcomes
still stop persistently. Do not reset stopped/exhausted budgets to regain quota.

## Preview switch and zero-SQL ingress check (2026-09-29)

Committed the earlier local initialization/build repair checkpoint as 0c2e2c6;
it has not been pushed. The user then explicitly requested preview access 1.
A settings-only PATCH changed that one preview variable, inheriting all other
bindings. Read-back verified preview mode, the original DB/coordinator, all
Secret names and unchanged production access 0. Deployment configs still
default to 0; a later preview upload restores the pause unless reviewed.

The deployed preview source was retrieved via the Worker code API. It has one
empty remote-plan assignment, the fixed singleton scope and UNREVIEWED_CASE
before journal/DB/provider access; the direct DB branch is restricted to local
mode. One no-redirect Web authorize GET returned **503 UNREVIEWED_CASE** with
no retry. Its D1 bound and usage are 0 read / 0 written by inspected path proof,
not a billing/dashboard measurement. No login/provider/DDL SQL was attempted.

Browser inventory failed without a page navigation. One public homepage GET
returned 200 HTML but no expected X-Robots-Tag; remaining shell cases stopped.
The Web configuration lets assets bypass its preview middleware. The companion
records the local repair; browser/functional acceptance remains pending.
Only two finite site requests ran. Cumulative recorded remote D1 usage remains
0/0, against the existing 10,000 read / 1,000 write limits. Turning on the
switch alone did not initialize the database or admit a functional case.

## Local initialization preparation and Builds diagnosis (2026-09-29)

Added test-first JSON map cardinality checks and a binding-only initialization
RPC using the same fixed budget journal, full upfront reservation and once-only
execution. Missing metadata/failure/restart retains reservations and stops;
no public initialization endpoint, caller SQL, reset or retry exists. Both
reviewed remote HTTP plans and the initialization plan remain disabled.

The finite synthetic identity SQL trace replay completed one local request and
55 native D1 operations: **62 reads / 89 writes**, including schema **28 / 60**
and identity **34 / 29**. All native metadata was complete, and the process was
stopped. Exact phases, local artifact locations and limits are in the
[budget record](d1-validation-budget.md). These are local SQL observations,
not remote row bounds, Python RPC/FFI or real GitHub acceptance.

Full reference suite: **184 tests / 22 subtests passed**, 161.10 seconds.
Module mirror, production/preview static checks and diff whitespace passed.
Locked Python 3.14.2 / Wrangler 4.136.3 production packaging **dry run passed**;
no Worker upload or D1 SQL was executed. Missing pinned cache wheels were
restored within the existing dependency authorization; no versions were changed.

Existing Server HEAD GitHub Actions run 36412958473 remains successful.
Cloudflare Builds run 7326ca8c-8284-40b2-b5d4-834fc298b7df failed at the same
4a55cd5 commit: the build host's asdf Python 3.10.12 has no _sqlite3 module,
so the static checker fails before the deploy stage. The local command repair
uses uv-managed Python 3.10.12 with pinned PyYAML 6.0.3 for that check; the
actual managed-interpreter check and regression test pass. Deploy command,
branch restriction, D1 pause and no-migration/no-cron policy are preserved.

Automatic approval review rejected the remote trigger command update as a
persistent production pipeline change lacking explicit approval for this
modification. The user then explicitly approved it on 2026-09-29. A scoped
PATCH updated only build_command on trigger 38570661-f72d-4ee1-a97c-83db00a3003b.
Read-back confirmed the exact repaired command, unchanged deploy_command,
main branch and root directory. Production settings confirmed D1 access 0.
`cloudflare/server/build-trigger-plan.json` matches the remote repair. This
approval is resolved; no retry/new build, push, remote schema operation or
activation was performed. Current changes have no new CI/build result, and
the failed historical build is not evidence that the repaired build passed.

Remaining before remote login: proven DDL/bookkeeping and failure-path bounds,
reviewed finite SQL/input/cardinality/provider plans, Python initializer RPC
acceptance and provider callback/selected-repository confirmation. Production
and preview stay paused; cumulative recorded remote D1 usage remains 0/0.

## Separate preview GitHub App and parameter admission (2026-09-29)

The user requested real preview login, superseding the earlier GitHub waiver
for this preview flow, and supplied the separate GoPullwise Preview App:
App ID 5116379, Client ID Iv23liUTSufy2U2Dc5l7, slug gopullwise-preview.
Its Client Secret was stored directly in pullwise-server-preview through the
Cloudflare Secret API; no value was written to source or this record.
Settings-only multipart PATCH updated just the preview Client ID and slug,
inheriting all other bindings. Read-back confirmed both public values, all
four Secret names, the existing preview DB and the unchanged validation
namespace. D1 and both Jev flags remain 0. Production was not modified.

The user reported D1_ACCESS_PAUSED after GitHub authorization. This is the
expected paused callback response, not a completed login. Provider callback
configuration and installation on a selected test repository remain to be
confirmed. The current gateway reads user identity, user App installations
and accessible repository metadata, requiring only Metadata read repository
permission; no code-content or account-email permission is needed.

Test-first parameter admission was added to MeteredD1: reviewed SQL groups
now constrain parameter counts, scalar types, safe integer ranges and UTF-8
text bytes before native dispatch. Undeclared bound parameters stop the
journal. Eight new cases failed before implementation; the focused budget,
pause and deployment-contract run subsequently passed **48 tests**. The
first deployment-contract attempt resolved Windows/WSL bash instead of Git
Bash and failed to find python3; selecting D:/Git/bin through PATH passed.
These checks are Python/SQLite and offline shell checks, not workerd evidence.

Row bounds for DDL/schema bookkeeping, initialization, OAuth/session and
provider operations remain unproven. Parameter envelopes are an additional
input guard, not remote row-bound proof. REVIEWED_REMOTE_PLANS stays empty.
No remote D1 SQL, provider request or access activation was performed by this
agent; cumulative recorded remote D1 usage remains 0 read / 0 written.
Existing Server HEAD CI 36412958473 was reviewed as successful at 4a55cd5;
this local checkpoint has no corresponding CI run until pushed. The Cloudflare
Builds failure and approved command repair are recorded above; a repaired cloud
build result remains pending.

## Preview variables handoff checkpoint (2026-09-28)

The user's latest instruction supersedes the earlier production-test request:
**do not open production; configure preview only**. Both D1 switches stay 0.
Preview settings-only multipart PATCH inherited DB, validation namespace,
product JSON and all Secret bindings. Read-back confirmed GitHub public Client
ID/slug/callback, App/origin/Cookie policy, test Creem API base and explicit
PULLWISE_PLAN_LIMITS_JSON. No D1/provider request was performed.

A fresh 32-byte preview PULLWISE_GITHUB_TOKEN_KEY was generated inside the
connector and written directly as a Secret without displaying its value.
The preview database remains empty/unmigrated. Its Secret inventory now contains
the two Creem test Secrets and this encryption key. **PULLWISE_GITHUB_CLIENT_SECRET
is still missing**; never read/copy the production value. Real preview login
would also require provider callback configuration; earlier GitHub acceptance
waiver is not evidence that preview login is ready. Jev remains disabled.

Local preview config matches these changes. Static config/contract and parsed
plan policy validation passed. Remote settings PATCH requires multipart/form-data
with a JSON `settings` part; application/json was rejected without mutation.
Handoff is requested by the user; resume with preview setup and finite cost
proofs, not an access-switch change. Cumulative remote D1 usage remains zero.

## Web-to-Server transport correction (2026-09-28)

Production Web's domain fetch to this zone-route Server returned 521 while
direct Server requests returned 503. Web now uses a matching HTTP service
binding; production DNS and Server code/bindings were retained. A finite
post-fix Web GitHub-authorize request returned **503 D1_ACCESS_PAUSED**, confirming
the intended pause is reached through the proxy. The Server switch remains 0;
no OAuth state, provider call or D1 SQL ran. Full details and Web regressions
are in the companion. This is not real GitHub flow acceptance or activation.

## Configurable plans and paused preview (2026-09-28)

- Implemented Free 3 projects / 500 stored expenses and Pro/Max 100 / 20,000.
  `PULLWISE_PLAN_LIMITS_JSON` exposes operator overrides. Added account-wide
  atomic minute/month write protection; metadata reads never initialize usage.
  Archived/soft-deleted rows count, replay does not spend again, downgrade
  preserves history, and API-key revocation remains available after quota use.
- Max alone has a $5 provider-cost reservation per UTC calendar month, including
  annual subscriptions, without rollover. Reservations use pinned Jev 1.13
  pricing and a conservative complete-context bound; successful small requests
  are not yet settled to actual invoiced tokens. Jev stays disabled.
- Full Python/SQLite run: **145 tests / 22 subtests passed**, 244.58 seconds.
  After reviewed preview configuration and removal of migrations from the deploy
  script, all **6 deployment contract tests** passed. A subsequent test-first
  policy check prevents Pro/Max record overrides from diverging and passed.
  Web companion: **257 tests**,
  lint/build and offline production/preview config checks passed.
- Native local quota run used three explicit requests. Schema/fixture setup
  measured **29 reads / 61 writes**; successful guarded creation **1 read / 9
  writes**. The over-cap batch returned PROJECT_LIMIT but supplied no complete
  native meta. Its actual billed row counts are **unknown**, not zero; the run
  stopped without retry. This is local-only evidence, not a proven remote bound.
  Evidence: workspace `.test-tmp/plan-quota-native-evidence-c102.json`.
- The user supplied four **test** Creem product IDs, a test API Key and test
  Webhook Secret. Only the two Secret names/types were verified after a bulk
  Secret write to `pullwise-server-preview`; values were not read back/logged
  or stored in source. Production provider bindings were not changed.
- `pullwise-ledger-preview` was created via metadata only, ID
  `e9dc3b89-f81f-4fce-87ef-d8797d879fb4`. No remote schema, SQL query, seed or
  cleanup was run. It is distinct from production. Replication is disabled.
- Full Server source was uploaded to `pullwise-server-preview`, with the single
  SQLite-backed validation coordinator namespace and access **0**. Initial
  upload version: `d8c0ff19-b747-4dbc-b538-364128498812`; Secret updates subsequently
  produced a settings version. Domain: `preview-api.pull-wise.com`.
- `pullwise-web-preview` was separately uploaded, version
  `3a12463b-32e1-4782-85e9-f5345eef3791`, at `preview.pull-wise.com`, proxying only
  the preview API. Production Web/DNS/backend bindings remain unchanged.
- A finite HTTP shell/pause check stopped at its first unexpected preview shell
  response. No polling/retry or SQL followed. Domain/deployment metadata and
  local noindex/proxy tests are evidence; browser/payment runtime is still pending.
- User waived real GitHub login/repository authorization testing. Preserve
  synthetic security regressions and report this gate **waived**, not passed.
- Server GitHub repository connection was created for GoPullwise/pullwise-server
  (repo ID 1231824559, connection 8cb85eea-4b6f-4f52-8176-4fd55ed596ff).
  Automatic approval review initially rejected the persistent auto-deploy
  trigger, because association alone did not authorize future main deployments.
  The user then explicitly approved the exact reviewed main-branch commands,
  keeping D1 at 0 and no migration/cron. Trigger
  `38570661-f72d-4ee1-a97c-83db00a3003b` was created successfully for the existing
  production Worker. `cloudflare/server/build-trigger-plan.json` records it.
  Worker dependency resolution is now pinned by its checked-in uv.lock; offline
  lock verification passed. The first build of these local changes is pending
  their publication; Git association is not a claim that a build succeeded.
- Production settings read-back still showed access **0** and its original DB.
  **Cumulative remote D1 usage remains 0 read / 0 written.** Deployment/config
  metadata, empty DB creation and Secret storage are not S18 runtime acceptance.

Remaining: publish/review the first approved GitHub build; complete finite S18 SQL bounds
including migration 0004 and quota initialization/index effects; establish all
accounting/control cases; verify test product prices/credentials and test payment
flows only after those gates. Neither paused Worker may be enabled just because
configuration is present. See [plan configuration](../design/github-project-ledger/plans-and-configuration.md)
and [language evaluation](../design/github-project-ledger/server-language-evaluation.md).

## Cost-control continuation (2026-09-28)

- Test-first persistent admission and metered D1 adapter implemented. Shared
  DO scope, cumulative no-refund reservations, request/case/SQL operation caps,
  concurrency exclusion, stop/restart semantics and missing/partial metadata
  are covered. Remote SQL/case plans remain empty and all such cases fail closed.
- Full Python/SQLite suite passed: **131 tests / 22 subtests**, 189.32 seconds.
  Subsequently added proxy/unknown-path and in-flight/partial-meta regressions
  passed in the focused **32-test** cost/pause/transport/deployment run. This is
  targeted verification after the full run, not a claim of another full suite.
- Real local Worker/D1/DO control fixture: three finite HTTP requests; two
  admitted, third rejected by CASE_LIMIT. Native D1 totals: **7 reads / 4 writes**
  including DDL. Persistent reservations: **220 reads / 40 writes**, no refunds.
  The fixture process was stopped; no background validation remains running.
- An earlier loopback client timed out because system proxies did not bypass
  127.0.0.1. That attempt stopped. Explicit no-proxy/no-redirect transport then
  passed the finite local run; both local check scripts now prevent this leak.
- Static contract/config and module checks and Git Bash syntax passed. Remote
  config check now rejects enabled D1 and cron; production/preview remain at 0.
- Latest existing Server CI [36392938720](https://github.com/GoPullwise/pullwise-server/actions/runs/36392938720)
  succeeded at `d11d1bf3b1ebaf46d6476fa88a1451f36b8f708b`. New local changes
  have no corresponding CI run yet.
- The browser connector's bounded getState attempt timed out (25.6 seconds);
  no browser page was navigated. S17 browser integration remains pending.
- **S18 remains blocked** by unproven per-step migration/index/identity/payment
  and cleanup bounds, missing reviewed shared preview coordinator binding and
  incomplete browser/provider acceptance. Provider price/environment/validity
  is still unverified. No remote DB/provider execution was attempted; **remote
  totals remain 0 read / 0 written**, against cumulative 10,000 / 1,000 ceilings.
  Budget implementation/local success does not authorize production activation.

See [cost-path inventory and accounting](d1-validation-budget.md) for the full
path audit, finite local steps, actual evidence locations and exact remaining
gates. Preserve the empty remote allowlist until each bound is established.

## Conditional S17/S18 continuation (2026-09-28)

The user approved pinned dependency restoration, a cumulative remote ceiling
of 1,000 Rows Written / 10,000 Rows Read, and a separate Server deployment at
`https://api.pull-wise.com`. See [budget and execution controls](d1-validation-budget.md).

- Healthy Python 3.10.12 reference tools and Python 3.14.2/pywrangler tools
  were restored under the ignored workspace `.agents/runtime/`; the original
  broken venv was preserved. Wrangler remains pinned to 4.136.3.
- Full reference run: **111 tests and 22 subtests passed**. A subsequent
  exact-hostname route guard was added test-first; its current five deployment
  tests passed. Module mirror, static contract/config and shell syntax passed.
- The real local Python Worker/D1 served **17 finite HTTP checks**, including
  the **251-record CSV / ReadableStream** bridge, Cookie/API key authentication,
  create/replay/edit/conflict/removal, reports and pagination. Local migrations
  passed; no real provider call or remote D1 query was used for these checks.
- The cost pause was verified locally on four routes; absent/invalid switch
  values are rejected before DB/provider access. The browser connector remains
  unavailable, so browser UI integration and provider flows are not accepted.
- The existing HEAD Server CI run [36373637693](https://github.com/GoPullwise/pullwise-server/actions/runs/36373637693)
  succeeded at `2cc9e55859c25e99b6230883ceb6c4e1b1d836ff`. Current working-tree
  changes have no separate CI result; local commits do not trigger CI.
- Pre-commit offline checks: cost-pause, deployment-contract and identity HTTP
  tests passed (**15 tests**); static contract/config, module mirror and Git
  Bash syntax checks passed. Latest existing Server CI remains successful;
  these local changes have no corresponding CI run until pushed.

## Independent Server deployment

`pullwise-server-production` was uploaded successfully and is routed by
`api.pull-wise.com/*` in zone `pull-wise.com`. The existing proxied A record
was preserved. Custom Domain attachment failed because that record already
existed; the reviewed zone route was then attached through the API. The
checked-in production config matches this route. The existing Web Worker was
not changed and already proxies to this origin.

The bound database `pullwise-ledger-production`
(`80a29a0d-5699-449f-9541-a01dc461ca9d`) is new and empty. **No remote D1
queries, migrations, seeds or cleanup have been executed.** Metadata creation
does not constitute schema/data migration. No original account/payment data
was copied or deleted.

Remote settings confirmed `PULLWISE_D1_ACCESS_ENABLED=0` and no cron schedules.
Four finite PowerShell HTTP checks returned exactly **503 D1_ACCESS_PAUSED**
on `/health`, `/api/v1/me`, `/api/v1/expenses`, `/auth/github/authorize`.
Earlier Python HTTP checks were blocked with 403 and stopped at their first
request; one PowerShell health diagnostic then returned Cloudflare 503. The
successful fixed four-request check followed this diagnosis; no polling,
automatic retry or D1 monitoring query was used.

This is a deployed **paused** Server, not an active ledger release or completed
S18 acceptance. The pause guarantees no application D1 access while disabled;
it is not a metered quota when enabled. Real provider verification,
bounded admission/accounting, reviewed migrations/rollback, a distinct preview
environment and browser/provider verification remain required before activation.

The sections below retain the earlier cleanup's evidence and limits; current
authorization and verification are stated above.

## Verified

- Python 3.10.12 local reference run: `python -m pytest tests -q` —
  **107 tests and 22 subtests passed**. The entire surviving suite is collected.
  WSL used the existing pytest tools; no dependencies were installed.
- Coverage includes ledger owner/key restrictions, revoked credentials,
  Cookie Origin checks, money precision, project/shared moves, audit,
  idempotency/revisions, currency reports, pagination, CSV, OAuth/App
  synthetic callbacks and Creem subscription/webhook replay.
- Billing no longer exposes usage/runtimeUsage/processingActivity. The fresh
  schema omits old processing tables and quotas. These regression assertions
  failed before cleanup and passed afterward; payment facts remain protected.
- Module mirror generation and `--check`, ledger static contract/config check,
  and offline `uv lock --check` passed. The local root needs no production
  Python dependencies; PyYAML remains the deployment-check extra.
- Web companion: 33 test files, 252 tests, lint/build and offline config checks
  passed. Its proxy tests use mocked upstream responses.

## CI

The Server run inspected before cleanup was [36334353793](https://github.com/GoPullwise/pullwise-server/actions/runs/36334353793),
at commit `efa57b77bd6e2b7af0e2c27522eadfbc06582b01`. It failed because the
static checker could not import YAML. CI now installs the deployment extra
and generates the ignored Worker mirror before checking it. This is the local acceptance record; CI for the cleanup commits must be
verified independently after push.

## Pending gates

S01–S16 local implementation is complete. The CSV bridge and finite ledger
journey now have real local workerd/D1 evidence above. **S17 browser integration
and S18 remote/provider acceptance remain open**. Local workerd is not proof
of remote D1/provider behavior. The browser connector remains unavailable.

The previous blanket tooling pause was superseded by the user's bounded
authorization above. Remote D1 application access remains disabled. No remote
migration or real GitHub/Creem/Jev call was run. S18 also needs reviewed preview
domains/routes, distinct D1 ID, credentials, OAuth/App callbacks, Creem test
products/Secrets and migration/rollback. Before remote validation, document
per-request row/operation bounds, frequency, pagination/cache and cost guard.
Keep Jev disabled until the real anonymized en/zh quality gate passes.

The 2026-09-28 user constraint prioritizes D1 **Rows Written**: the supplied
billing snapshot is 176.13M total / 126.13M billable rows, not a live budget.
Before resuming S17/S18, prepare finite cases, request/retry caps, conservative
per-case and total Rows Read/Rows Written bounds (including ancillary writes,
index effects, migrations and cleanup), and an enforceable user-approved
numeric write/cost ceiling. Unknown bounds block remote execution. Prefer
local-only S17 after authorization; S18 must be a small one-off preview run.
No cron, recurring writes, D1 monitoring polls or remote load tests. Dashboard
alerts alone do not enforce the cap. Keep remote D1 access paused until the
active validation controls and prerequisites pass.

The user supplied the `GoPullwise` App metadata: App ID `3631508`, Client ID
`Iv23lipOl05N1ZULZmYW`, slug `gopullwise`. The two nonsecret runtime variables
are recorded in the production config. The user confirmed updating both
App callbacks to match the host-only Cookie/Web proxy flow:
`https://pull-wise.com/api/auth/github/callback` and
`https://pull-wise.com/api/integrations/github/callback`. This is user-reported
provider configuration, not a completed OAuth/install journey. The public
Client ID/slug/callback configuration was then deployed and verified through
Worker settings. A new 32-byte random `PULLWISE_GITHUB_TOKEN_KEY` was generated
without displaying its value and stored as a Cloudflare Secret for the empty
database. A later names/type-only inventory confirms all four required Secrets:
`PULLWISE_GITHUB_TOKEN_KEY`, `PULLWISE_GITHUB_CLIENT_SECRET`,
`PULLWISE_CREEM_API_KEY`, `PULLWISE_CREEM_WEBHOOK_SECRET`. Secret values were
not read. This verifies presence, not provider validity. Settings still confirm
D1 access is off.
No remote D1 query or OAuth request was made for this configuration update.
Do not enable D1 or invoke OAuth just to
check partial configuration. The numeric App ID is not the OAuth Client ID.

The four user-supplied Creem product IDs were synchronized to
`pullwise-server-production` as a **plain_text** JSON string in
`PULLWISE_CREEM_PRODUCT_IDS_JSON`, and mirrored in the checked-in production
config. The settings-only API patch inherited all other bindings. Read-back
confirmed the exact Pro/Max monthly/yearly mapping, all four Secret names,
the unchanged DB ID, `python_workers` compatibility flag/date, and
`PULLWISE_D1_ACCESS_ENABLED=0`. Offline product parsing and the production
static check passed. No D1 query/migration, Creem API/payment call, backend
request, cron change or Web deployment was made for this synchronization.
Actual product environment/prices and provider behavior remain unverified.

The three migrations describe the fresh, unexecuted target schema. Cleanup
does not migrate or delete an existing remote database. Retired local state
was preserved outside both product projects rather than treated as disposable
expense or payment data.
