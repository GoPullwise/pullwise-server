# Current local acceptance

Updated 2026-09-29. Companion: [Web acceptance](../../../pullwise-web/docs/validation/local-acceptance.md).

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
