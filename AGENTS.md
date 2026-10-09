# Pullwise Server

## Optional oldest-expense replacement (2026-10-09)

The user's latest requirement is two capacity modes without overage fees.
Personal Owner Settings has autoRemoveOldestExpense, default false on all plans,
with a cookie-only independent revision/CAS endpoint. The setting belongs to the
actual account, independent of workspace selection, and governs its members,
API keys and recurring occurrences. Switching it must not retire data itself.
At exactly full capacity, enabled creates atomically remove one oldest visible
expense (occurredOn, createdAt, ID) and insert the new record. Disabled full
creates are denied; already-over-limit ledgers require manual cleanup before
replacement. The actual oldest target must be permitted to the current actor/key;
never silently select a later permitted record. Failed creates, model fallback,
stale credentials/preferences and idempotent/occurrence replays do not retire data.

Expense capacity is current undeleted shared and non-removed-project expenses,
including archived projects. Manual removal and project removal free expense
slots. Reconcile old cumulative usage in the original mutation transaction from
the same active predicate; GET never initializes or repairs counters. Project
capacity remains cumulative. Preserve original audits, replay and occurrence
identities, authorized 24-hour activity, financial/owner/membership/key fences,
commercial write budgets and native/global accounting. Active capacity is not a
physical storage ceiling. This supersedes earlier retained expense-slot guidance.

## REST API and browser parity (2026-10-09)

The user requires the Web ledger to use the REST API and external Bearer API
keys to cover project creation/editing/removal, ordinary and recurring expenses,
and member invitation/review/role/removal operations. This supersedes older
cookie-only project-removal, recurring-management and member-governance rules
below. Browser sessions and keys share resource DTOs and business validation.
Project removal still requires the actual ledger Owner plus projects:write and
the key's current target permission. Member governance uses explicit
members:read/write scopes; all roles can read members and only Owner/Admin can
write. Governance keys must omit projectIds and remain bound to one ledger;
global key workspace/inbox reads cannot reveal other ledgers. Preserve original
inviter approval, Admin boundaries, current membership revisions, credential
revocation and atomic CAS/audit/quota fences. Invitation preview/application
requires the applicant's independent cookie-account identity.

Recurring key grants store only the credential hash in internal template JSON,
never a token or public DTO field. Every execution rechecks the original key's
current expiry, revocation, scope, workspace/membership and target permission in
the occurrence's atomic write. A full edit or explicit resume adopts the current
actor and credential; a session edit/resume removes an old key grant. Expense
activity reads include recurring-rule changes under expenses:read; project-setting
activity also requires projects:read on a key. Apply filtering before pagination.
Existing preview/local verification and publication authority remains; production
D1 activation is still paused.

## Verification email language (2026-10-09)

The user requires verification-code emails to use English only in the subject,
plain-text body and HTML body, regardless of the interface language. Keep the
six-digit code (including leading zeros), ten-minute lifetime and ignore-email
hint. This applies to both email login and linking through the shared template.
Preserve recipient privacy, delivery configuration, no-retry behavior,
authentication, rate limits and storage. Complete this copy change after the
member-role refresh release; publish Server preview only after offline checks.
Do not send real verification emails as acceptance for this text-only change.

## Category removal (2026-10-09)

Categories keep Archive for historical use and add explicit Remove for unused
configuration, matching the Members confirmation flow. Preserve the existing
category DELETE/archive contract. Removal requires current category-management
authority and If-Match; atomically reject references from all expenses,
durable expense audits, creation replays and every recurring-rule state.
Never remove associated financial records or histories. Existing preview
publication policy applies; retain the original database, journal and production
pause. No schema migration or background cleanup is needed for this feature.

## Recent operation history (2026-10-09)

The user requests project and Shared pool operation logs with actual actor,
timestamp, affected record and changes for a rolling last 24 hours. Log successful
expense, project-setting and recurring-rule changes atomically with their current
authority, CAS/idempotency and commercial-plan fences; failed/replayed operations
must not produce extra entries. Keep immutable actor/record snapshots and target
scope authorization, including restricted keys, moves and removed users.
The recent activity projection has indexed bounded reads and bounded retirement
of expired rows through existing write/scheduled paths, never GET-side cleanup or
an added cron. Preserve financial records and the original durable audits.
This feature authorizes necessary schema/native checks, main pushes and preview
publication through the existing DB/coordinator/journal; production remains paused.

## Invitation approval (2026-10-09)

New member links select a role without a GitHub recipient. Opening the hash
link restores/login-returns the actual account, and applying records a pending
request without ledger access. Only the original inviter with current original
management authority approves or rejects; approval atomically grants membership
and consumes the link. Keep legacy targeted/accepted invitations, idempotent
applications, creator/revision/session/Origin fences, single-use approval and
removed-member protection. Inbox reads remain cookie-only, bounded and scoped
to the issuing actor. Migration 0008/v8 preserves the existing database,
singleton journal, prior markers and accounting. Complete isolated native
schema/journey acceptance before preview publication; production D1 stays paused.

## Project links and recurring expenses (2026-10-08)

The latest user explicitly requests implementation of development/product links
and weekly/monthly/calendar-quarter/yearly recurring expenses. This authorizes
the necessary preview Server schema upgrade, bounded hourly background writes,
main pushes and preview-only publication, superseding older blanket no-cron and
Web-only follow-up instructions for these features. Keep production D1 paused
and production without cron. Preserve the original preview DB, coordinator,
namespace, cumulative journal and completed upgrade markers; never reset or
retry an unknown outcome. Local native acceptance precedes one reviewed v7
upgrade and preview publication; do not create remote synthetic users or issue
remote forced ticks.

Project URLs are optional absolute HTTP(S) destinations, validated and bounded
before persistence. Manual development links apply to truly unlinked projects;
GitHub links require each current actor's authorized metadata. Product links
apply to every project. Never fetch configured URLs on the Server or weaken
trusted authentication/payment redirect restrictions.

Recurring rules require a real cookie-session creator and current expense-write
permissions; API keys do not create permanent background grants. Recheck actor,
workspace/project/category access and owner plan at execution. Store timezone
and original day anchors, clamp short months, preserve calendar period identity
across revisions and expense deletion, and generate expense/audit/occurrence/
quota/next-date atomically. Pause/resume skips paused periods; explicit start
allows bounded catch-up. Future planned costs do not enter financial reports.
Use packaged pinned tzdata and prove IANA timezone behavior in native Pyodide.
Scheduled execution uses the existing singleton lock and metered D1 path, with
finite per-tick limits; no public scheduler or migration endpoint.

## Standalone projects follow-up (2026-10-07)

Projects may be created with a nonblank name and no GitHub repository or
Organization association. This supersedes the historical minimum-one-repository
product rule below. Use real nullable GitHub anchors and empty binding arrays,
never fabricated repository IDs. Standalone reads, writes and reactivation do
not require repository installation or provider calls; linked projects retain
actual-actor GitHub authorization and lost-access protection. Explicit attach
or detach preserves project ID, expense history, role/key/CAS fences and plan
limits. Preview schema upgrades must preserve the existing database, singleton
journal, completed prior upgrade markers and cumulative accounting. Prove the
finite v5-to-v6 migration locally before one preview upgrade; no production D1
activation, remote synthetic accounts or high-frequency testing.

## Current task authority (2026-10-06)

Latest user follow-up authorizes complete Projects acceptance, two real GitHub
accounts (DFerryman and SanChai20), payment/subscription success and failure
verification without real payment, repairs, main pushes and preview publication.
It explicitly requests removal of unreasonable preview request restrictions.
The user then clarified that their total Cloudflare target is about USD 200 per
month and requested reasonable abuse protection, not hard daily/monthly row
budgets that stop normal use. Do not deploy an unapproved calendar write cap or
restore lifetime product exhaustion. Keep remote acceptance low frequency and
finite; no rapid D1 polling, load tests or repeated provider calls. Preview
abuse admission uses the same DO SQLite storage with hashed IP/credential/actor
subjects, bounded expiring counters and HTTP 429 plus Retry-After. It adds no
D1 counter writes. Authenticated actor limits remain compatible with paid plan
write allowances; emergency revocation has a separate admission bucket. The
USD 200 value is an operating target, not a guaranteed invoice ceiling.
Product cardinalities are verified on first unmarked use/schema upgrade and
after every mutation, then persisted in the same journal. Healthy read requests
reuse that proof to avoid full-table COUNT scans. This relies on the existing
exclusive preview-write path: no console/other-Worker writes outside the same
singleton. Incomplete tickets and unknown native outcomes still stop.
Enabled preview product traffic now selects product_operations: lifetime
request/read/write test ceilings do not gate normal operation. The original
journal, namespace, numeric cumulative accounting and legacy evidence stay;
new evidence is append-only in the same DO SQLite store. Generic finite test
plans retain their ceilings. Complete native accounting, bounded SQL/input/CSV,
workspace authorization and commercial plan/model budgets remain required.
Do not reset/resume unknown-outcome or incomplete-accounting stops. Known
fully-accounted request/provider failures are isolated to their request.
The older lifetime product ceilings stated below are historical authority,
superseded by this follow-up; they must not reinstate ordinary preview gating.
Remote testing remains preview-only, production D1 stays paused. Real-account
acceptance requires the accounts' actual preview sessions, never fabricated
sessions from GitHub connector profiles. Test payment facts locally with signed
synthetic webhooks; do not make real charges or change live subscriptions.

The follow-up capacity repair uses exact `record:<kind>:<identity>` rows in the
existing `app_state` table. Preserve legacy user/session/token/billing fields,
finance history, webhook receipts, account revisions and all original journal
counters. Copy legal legacy facts with one reviewed atomic cutover under the
existing coordinator and persist its state-storage marker; no namespace or
database replacement, implicit GET migration, general reset or migration retry.
Trusted user-record SQL snapshots may use a typed 512 KiB envelope to retain
1,000 repos and 100 billing events; ordinary HTTP ingress remains 8 KiB.
Application reads use exact owner/record keys, and cardinality checks keep
bounded numeric facts rather than loading all account payloads on each write.
Native migration, restart, accounting and capacity checks precede publication.

Latest scope: the original release goals have their own dated evidence; the
multi-repository/Organization/shared-ledger version is now implemented and
released to preview with its own native/browser/publication evidence. Treat
`docs/planning/project-repositories.md` as its current product specification.
Existing `owner_id` is the immutable workspace ID; never rewrite expense history
or share GitHub credentials between members. Migration 0005 and the one-shot
legacy-v4 preview upgrade used the same cumulative journal; deployed schemaVersion
5 and healthy accounting were verified. See
`docs/validation/workspaces-preview-release-2026-10-06.json`.
Keep production D1 paused and remote work
preview-only. Historical original-version evidence is not new-version acceptance.

The user separately authorized editing the production GoPullwise GitHub App's
registered permissions. This does not authorize production D1 activation or
business testing. The existing connector cannot edit App registration settings;
the verified target and actual unchanged snapshot are recorded in
`docs/design/github-project-ledger/github-app-permissions.md`.

The user explicitly authorized the resumed Server/Web audit, repairs, local
verification, main pushes, Cloudflare publication and finite preview user/model
acceptance. This supersedes historical blanket test/Wrangler pauses in this
file. Continue within that scope without repeating deployment approval.
Preserve the existing preview journal and cumulative ceilings of 100,000 read /
1,000 written rows, no reset/retry/cron policy and environment isolation.
Production D1 activation and real payment acceptance remain separate gates;
publishing code does not prove those gates. Jev enablement requires actual
provider/runtime and labeled en/zh quality evidence, not synthetic gold labels.
Use `docs/validation/local-acceptance.md` for the latest release evidence.
The user's later 2026-10-06 scope is preview-only remote testing/verification.
They subsequently reaffirmed that every original goal, including an engineering
path to production, must be finished before the new multi-repository/team version.
Production-compatible source/configuration repairs and isolated local runtime
checks are authorized; do not activate production D1, migrate or perform remote
production acceptance. Main Builds retain the checked-in production pause.

## Product and authority

Use `main` for all work unless the user explicitly requests another branch.
Main pushes trigger production Builds, which must keep D1 access paused.
Select preview-only deployments with the explicit preview config; never use
branch separation as a substitute for runtime environment guards.

The current product is Pullwise, the project expense ledger for developers and
teams at pull-wise.com, described in
`docs/design/github-project-ledger/README.md`. Use repository state, current
user instructions, local documentation and tests as authority. Keep Web and
Server separate and share `openapi/ledger-v1.yaml`. Do not use Notion or
historical product plans to gate work.

Keep public copy aligned with standalone named projects, optional GitHub
repository/Organization associations, explicitly invited shared-ledger roles,
user-entered expenses and separate per-currency reports. A shared expense pool
is visible only to authorized ledger members/keys, not to the public. Pullwise
subscription payments remain separate from ledger expenses. Qualify AI
assistance by the ledger Owner's Max plan, current activation and usage budget;
do not promise bank/vendor imports, exchange-rate conversion, automatic
allocation, accounting/tax advice or permanent deletion through expense DELETE.

## Runtime and ownership

- The user removed the enabled preview product's cumulative 200-request gate
  on 2026-09-29. Keep counters/evidence cumulative and row/SQL/timeout/concurrency
  caps. One audited inactive REQUEST_LIMIT recovery is allowed; do not recover
  other stops or incomplete accounting. Default/generic finite plans keep their
  request/case limits. Deploy this policy through the existing preview config.
- User approved preview-only cumulative reads of 100,000 on 2026-09-29;
  writes remain 1,000. Default/non-product budgets remain 10,000 reads.
  The fixed preview journal applies one read-ceiling grant without resetting
  counts or reclaiming legacy reservations. Only proven BUDGET_EXHAUSTED state
  with complete accounting may resume; unsafe stops and later exhaustion
  remain blocked. The separately approved REQUEST_LIMIT policy above is the
  only additional recovery. Keep production access 0 and the same namespace/database.
  The user now requires main for all work; retain the explicit preview runtime
  guards even when this source is merged into main.
- Product read reservations settle only after complete, in-bound native meta
  reports total_attempts=1 for every result. Keep all write reservations and
  retry/missing/ambiguous read reservations; actual counters/evidence never reset.
  One audited schema-only read-margin reconciliation can recover BUDGET_EXHAUSTED
  with complete initialization evidence. Preserve every legacy product reservation,
  original namespace/name, ceilings and request count; no general reset endpoint.

- The user's latest 2026-09-29 requirement is usable, product-wide preview.
  PULLWISE_PREVIEW_PRODUCT_ENABLED=1 selects the product budget wrapper; it
  admits product paths with per-SQL reservations instead of the empty case
  list. Keep production access 0, cumulative writes at 1,000 and enabled
  preview-product reads at the later-approved 100,000. Preserve the same
  namespace/name and no-reset/no-retry/no-cron policy.
- Product preview initializes the frozen five-migration canonical schema only
  on an empty isolated DB, behind the same journal. cloudflare_preview_schema.py
  retains exact legacy-v4 fingerprints and one compiled, atomic 0005 upgrade.
  Verify every migration fingerprint; never silently rerun initialization or
  retry a partial/unknown upgrade against user data. Package both preview modules
  in the mirror. See docs/validation/d1-validation-budget.md for current bounds.
- ProductMeteredD1 validates scalar INSERT and unique-key UPDATE/DELETE,
  includes all index effects and guard cleanup, reserves before dispatch and
  validates native meta. Cardinality snapshots/journal evidence must never
  contain raw users, tokens, SQL parameters or provider payloads. Complete
  CSV pulls inside the active ticket; serialize concurrent product requests.
- Store expense audit/idempotency JSON as compact UTF-8. Only scalar INSERT
  expense_events.before_json/after_json and expense_create_idempotency.response_json
  accept a 16 KiB JSON-object envelope: 8 KiB ingress plus bounded DTO metadata
  and at most 30 categories' assistance. Other text and small typed state records
  retain 8 KiB bounds; exact user snapshots have the reviewed 512 KiB exception;
  reject malformed/non-finite/non-object JSON and retain row/index/global caps.
- Active preview config is allowed only with its original DB/hosts, existing
  ValidationBudget class and test Creem origin. This supersedes the old blanket
  preview-pause notes below; it does not permit production activation or Jev.
- Workers fetch supports follow/manual, not redirect='error'. GitHub/Creem
  and Jev gateways use manual and reject unsuccessful/3xx responses without following
  them. Preview diagnostics must redact all credentials and omit provider body.
- /_preview/budget is a preview-only, read-only numeric status, served from DO
  storage without D1. It is not a reset/stop/SQL endpoint and must never return
  raw journal product state, accounts, tokens or parameter evidence.

- `cloudflare/server/src/entry.py` is the Server Worker entry.
  `pullwise_server/` owns its modules; `sync_server_modules.py` generates the
  ignored Worker mirror. Never edit the mirror directly. Run the sync and
  its `--check` after changing packaged source. Preserve the import closure.
- OAuth/App authorization, sessions, API keys and Creem payment facts are
  required ledger infrastructure. Browser and external clients share the
  same resource authorization; Cookie writes require a trusted Origin.
- API keys use `api_key_dto_rules.py` ledger scopes, hash-only storage,
  one-time token display, immediate revocation, project ID restrictions and
  explicit shared-pool permission. A project allowlist grants no shared access.
- Resource reads append their SELECTs to the principal's D1 read batch.
  Guarded writes recheck user/session/key, revisions, categories and newly
  targeted projects in the same write batch. Do not split atomic guards,
  mutation, idempotency and expense audit publication.
- Money is integer minor units with fixed currency exponents. Shared
  expenses have no project ID; reports never combine currencies. Moves keep
  one record; soft deletion removes it from lists, reports and exports.
- Aggregate money uses `ledger_money_totals.AGGREGATE_SQL` split integer sums,
  then Python integer reconstruction; direct SQLite SUM(amount_minor) can
  overflow on valid records. Both D1 components stay JavaScript-safe through
  the maximum 1,000,000-record operator cap. Aggregate DTOs use numeric
  amountMinor through 9007199254740991 and a decimal integer string above it.
  Project PATCH returns its totals from the same guarded mutation batch.
- Wrap native env.DB in NativeD1 underneath quota/budget adapters. Keep logical
  parameters as Python integers for envelope checks; convert safe integers to
  exactly representable JavaScript Numbers only at native bind. Python FFI
  binding of 9007199254740991 can otherwise fail before transaction dispatch.
  Reject unsafe integers, preserve atomic batches and native result/meta.
- Lost repository access hides protected GitHub metadata and blocks new
  project targets; owners retain control of their historical expenses.
- GitHub 401 means `reauthorization_required`, never a revoked Pullwise session.
  Ordinary repository GET/sync must not refresh/persist tokens or cached grants.
  Discard partial/cached grants on credential/permission rejection. Rate limits,
  transport/5xx, invalid responses and crypto/config failures remain distinct
  errors. Project history hides GitHub names and uses `unavailable` for unknown
  grants; new targets fail closed while historical edits retain owner/key fences.
  Preview identity diagnostics contain only code, exception type/function/line
  and numeric provider status; never stringify exceptions or log credentials.
- Repository candidates and new project eligibility use `read_repository_access`
  with the current App user token; a missing installation Setup callback/cache
  is not evidence of missing GitHub grants. Bound discovery to ten installations
  and 1,000 repositories; preserve failure/lost-access handling and owner scopes.
  `/repositories/sync` is a cookie-session/trusted-origin, read-only verification
  route used by Web popup completion, with no user/session/grant writes.
- Creem checkout does not grant entitlement. Signed, idempotent webhooks
  own payment facts; keep account revision fences, pending updates and replay
  recovery atomic. Platform payments never create ledger expenses.
- Cookie-authenticated ledger and API-key writes require a trusted Origin
  regardless of SameSite mode. Malformed Authorization must never bypass
  this check. Preview diagnostics expose fixed codes/type/site and numeric
  status only; never return provider exception text or partially redacted bodies.
- Use json_input.validate_json_unicode for all decoded request JSON strings
  and object keys before route/auth/provider/write work. ASCII JSON escapes
  can contain unpaired surrogates that would otherwise stop the preview's
  UTF-8 parameter admission. Valid emoji and decoded surrogate pairs remain
  valid; preserve ordinary request accounting and global stop rules.
- Creem product bindings map explicit month/year keys, never positional lists.
  Claim subscription upgrades before provider dispatch; provider acknowledgement
  returns pendingChange but cannot grant the target plan. Signed target/terminal
  webhooks clear the claim. Unknown provider outcomes retain it to avoid another
  prorated charge; known rejected requests may release it.
  Terminal webhooks with no valid cadence preserve the existing subscription's
  annual/monthly history; never infer month from missing provider facts.
- `PULLWISE_CREEM_PRODUCT_IDS_JSON` is a plain_text binding containing a JSON
  string, because entry parses it with json.loads. Keep pro/max objects and
  distinct product IDs, with month/year keys as available. Mirror public IDs
  in the reviewed environment config. Settings-only binding updates must
  inherit existing bindings and verify Secret names, DB ID and D1 pause on
  read-back; never read or log Secret values.
- The fresh, unexecuted migrations contain current identity, payment, key
  and ledger tables. Account authority tracks billing revisions and cycles,
  not old processing quotas. Billing DTOs expose subscriptions and events,
  not historical model-processing usage. Plan defaults are Free 3 projects/500
  stored expenses, Pro/Max 100/20,000; override with PULLWISE_PLAN_LIMITS_JSON.
  Only Max has a $5 monthly UTC Jev reservation, with no rollover (annual too).
  Do not invent Creem prices; provider activation remains separately gated.
- `PlanLimitedD1` inserts one usage UPSERT inside the original credential,
  mutation, idempotency and audit batch. GETs do not initialize usage; capacity
  includes archived projects/soft-deleted expenses. Quota changes do not reset
  totals. Late requests cannot roll UTC minute/month counters backwards.
  Automatic Jev reservations and suggestion events do not consume commercial
  write/minute allowances; the expense operation counts once. Reserve Jev USD
  atomically with its budget mutation; event-only batches retain the original
  credential fence and global D1 accounting without an extra usage UPSERT.
  Check the owner/day attempt count and current UTC-month USD remainder before
  optional reservation. Deterministic exhaustion must return the manual fallback
  before a failing native batch or another USD reservation; retain atomic
  reservation fences and all unknown-outcome accounting.
  Key revocation is exempt from commercial quotas so a compromised key can
  always be revoked; the normal credential fence and global validation cap stay.
- Migration 0004 and quota initialization/index effects need new S18 bounds.
  Jev reserves a conservative whole-context cost before calling; it is not an
  actual-invoice meter. Keep quality/enable flags off. Free/Pro are ineligible.
- The user waived real GitHub login/repository authorization acceptance on
  2026-09-28, then requested real preview login on 2026-09-29. Real preview
  login is now a pending gate; preserve security regressions and do not report
  the earlier waiver as acceptance of the new test App.
- Preview uses preview-api.pull-wise.com and a separate empty database, with
  test Creem IDs/Secrets only. Production keeps its original providers/DNS.
  Production access stays 0. On 2026-09-29 the user explicitly authorized the
  preview switch at 1 while its deployed remote plans remain empty; the
  one-request ingress check returned UNREVIEWED_CASE before D1/provider access.
  This does not authorize unknown-bound SQL or initialization. Checked-in
  deployment defaults stay 0. Never send a test webhook to production.
- `deploy-cloudflare.sh` uploads only a paused Worker and never migrates D1.
  `build-trigger-plan.json` records the approved main-branch GitHub Builds setup.
  User explicitly approved it after automatic review initially rejected it.
  Keep its static pause check, pinned tools/lock, no-migration command and main
  branch restriction; this approval does not authorize activating D1.
- Max Jev assistance runs inside ordinary POST/PATCH expense workflows,
  including expenses:write keys without suggestions:use. POST can omit a
  category; select only a confident active account category or return 422
  CATEGORY_REQUIRED with assistance. PATCH requires an explicit category.
  Preserve every explicit field; target and duplicate results are advisory.
  Authenticate and check target restrictions before model calls. Hash the
  normalized caller intent before inference; replay stored responses before
  archived-target/category validation, without another provider/budget attempt.
  GETs never call Jev. Provider failures or exhausted Jev budget do not block
  ordinary writes with an explicit category. The advanced draft endpoint
  shares the 20-attempt UTC daily cap and Max monthly reservation. Keep the
  runtime bound within the canonical attempts<=20 schema; raising only code
  would reject the next batch and stop the preview journal.
  Keep enable/evaluated flags off until real provider/Worker validation and
  labeled en/zh quality gates pass. The user authorized agent-authored synthetic
  samples for preview evaluation on 2026-10-02; record that provenance and actual
  model results, never substitute fabricated predictions or claim customer-data
  validation. This does not authorize production D1 activation.
  `typesafe_client.py` is the shared bounded input/response validator;
  Worker transport is `cloudflare_jev_gateway.py`, not a local child process.

## Verification and deployment

- Run every current test with `python -m pytest tests`. CI must discover the
  whole suite; do not hide obsolete code behind a test allowlist.
- Use Python 3.10.12 for local reference tests. Synthetic SQLite D1 fixtures
  verify application behavior, not Cloudflare FFI or real transaction behavior.
  Close test connections explicitly when a temporary database is removed.
- Offline checks: `python scripts/check-ledger-s01.py --allow-placeholders`,
  `python cloudflare/server/sync_server_modules.py --check`, and
  `bash -n scripts/deploy-cloudflare.sh`.
- All Wrangler/workerd and D1 commands, including local probes, remain paused
  until explicit user authorization. Do not enable cron triggers or remote
  schedules. Before remote validation, document request row/operation bounds,
  frequency, pagination/cache policy and cost guard, then obtain approval.
- Follow the workspace `D1 Rows Written budget guard`: S17/S18 need finite
  cases, request/retry caps and conservative total read/write bounds under a
  user-approved numeric ceiling. Count all guard, audit, idempotency, identity,
  payment replay, migration/cleanup and index effects, not just expense rows.
  Unknown bounds block remote work. No cron, recurring writes or D1 polling;
  enforce caps before execution rather than relying on delayed billing alerts.
- Current authorization allows pinned tool restoration, local S17 and an
  independent Server deployment at api.pull-wise.com. Remote validation has
  cumulative ceilings of 1,000 written / 10,000 read rows. Keep remote
  `PULLWISE_D1_ACCESS_ENABLED=0` until active-service admission and accounting
  controls pass; it blocks all routes before any DB/provider access and fails
  closed when absent. It is a pause switch, not a metered quota.
- Preview ingress must use the one fixed `ValidationBudget` Durable Object
  name `pullwise-s17-s18-2026-09-28`, sharing the same namespace across phases
  and databases. Never derive a budget ID from a run/user/DB or create another
  namespace to regain quota. Production remains blocked. Only the explicitly
  local config uses `PULLWISE_MODE=local` for direct local D1 access.
- `cloudflare_validation_budget.py` reserves worst-case totals before side
  effects and never refunds them. Its DO SQLite journal is not D1. The hard
  ceilings are 1,000 written / 10,000 read rows and 40 requests; exact SQL batch
  groups and operation counts require reviewed bounds. The remote plan list
  is deliberately empty until schema/index/input/cardinality bounds are proven.
  Stop is persistent, without a reset/resume API; interrupted requests retain
  their reservations. Capture native meta through the metered batch adapter,
  including for `first()`. CSV streaming, migrations and cleanup are unadmitted.
- Every metered SQL group must declare per-statement parameter envelopes:
  bounded safe integers, UTF-8 byte-bounded text or explicit null. Omitted
  envelopes permit zero bound parameters. Reject type/count/range/encoding
  violations before dispatch and never put parameter values in budget evidence.
  Input envelopes supplement, but do not prove, row/cardinality/index bounds.
- Use json_object parameter bounds for reviewed app_state maps that feed
  json_each: cap top-level entries and UTF-8 bytes; reject duplicate keys,
  malformed/non-finite JSON and invalid Unicode. Nested collection bounds
  still require separate review for each SQL path.
- ValidationBudget.initialize is a binding-only RPC with no caller-supplied
  SQL or public HTTP route. REVIEWED_INITIALIZATION_PLAN remains None until
  DDL/empty-schema bounds pass. Reserve the full fixed plan before executing,
  allow it once across restarts, and preserve partial initialization on failure
  without retry/reset/unreviewed cleanup. It uses the existing fixed journal.
- scripts/check-preview-identity-cost.py prepares a local-only, synthetic
  identity SQL replay in ignored .agents/runtime. Never deploy its generated
  fixture or copy synthetic users/tokens to remote D1. Its native SQL metrics
  do not prove Python FFI, real providers or remote worst-case bounds.
- Loopback validation clients must disable system proxies and redirects.
  This machine's proxy does not bypass 127.0.0.1; a default urllib opener can
  forward an intended local check outside loopback and time out.
- Run Windows shell tests through a PATH-resolved Git Bash and use forward
  slash relative script paths. `PULLWISE_PYTHON` selects the deployment-check
  interpreter. SQLite test contexts must commit/rollback AND close connections.
- Cloudflare Builds' asdf Python 3.10.12 lacks _sqlite3. The checked-in build
  repair runs the static SQLite check through uv-managed Python 3.10.12 with
  pinned PyYAML 6.0.3; do not rely on the host Python for that check. The user
  explicitly approved the build-command repair on 2026-09-29; the remote
  trigger was updated and read back. Keep deploy_command unchanged. Do not
  repeat that approval or treat the resolved automatic rejection as a blocker.
- `scripts/check-ledger-local-runtime.py` seeds an isolated local database and
  checks a finite loopback-only HTTP journey; migrate first and use a fresh
  persistence directory for each run. Never seed its fixture remotely.
- Production uses `api.pull-wise.com/*` as an exact zone route over the
  existing proxied DNS record. Keep DNS and Web intact. The newly bound remote
  database is empty/unmigrated; paused deployment is not active-service acceptance.
- Web must use its PULLWISE_SERVER HTTP service binding to this Worker.
  Same-zone fetch cannot target a Route and may reach the retained DNS origin
  instead (521); direct API checks alone do not validate Web-to-Server transport.
- GitHub Client ID/Secret must belong to the same GitHub App as its slug,
  because repository access uses `/user/installations` App user tokens.
  Distinguish the Client ID from the numeric App ID. Configure OAuth and
  installation Setup callbacks separately through the Web API proxy.
- Preview uses the separate GoPullwise Preview App (ID 5116379, Client ID
  Iv23liUTSufy2U2Dc5l7, slug gopullwise-preview). Its current gateway needs
  only Repository Metadata read permission; organization/account permissions
  are unnecessary. Keep production App configuration independent. Provider
  callbacks and installation are user-configured prerequisites, not implied
  by Worker settings or the presence of a Client Secret.
- Preview/production use separate configs and databases. Deploy guards must
  reject placeholders. Keep credentials in Cloudflare Secrets; never expose
  tokens, private keys or account snapshots in logs or documentation.
- `docs/validation/local-acceptance.md` records current evidence and pending
  S17/S18 gates. The Python CSV generator to `ReadableStream` bridge has local
  workerd evidence; browser/provider and remote acceptance remain pending.

## Repository hygiene

Keep current source, tests, contracts and durable documentation. Do not retain
unused modules, phase-by-phase stale handoffs, prototype fixtures, local logs,
screenshots or generated caches in version control. Runtime/build dependencies
and CodeGraph indexes are local tooling, not product source. Preserve user
changes and data; retire local databases outside the product tree before any
approved cleanup. Record current acceptance in one document per project.

## CodeGraph indexing

The installed CodeGraph scanner uses Git visibility and .gitignore rules.
Keep current source, tests and schema/contracts available; exclude dependency
folders, generated mirrors, build/cache output, local tools and data backups.
The workspace .gitignore protects whole-workspace scans; each repository
keeps its own rules because Git boundaries do not inherit workspace rules.
After changing exclusions, force-reindex an already initialized project to
remove previously indexed paths; do not initialize another project implicitly.
