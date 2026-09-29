# Pullwise Server

## Product and authority

The current product is the GitHub project expense ledger in
`docs/design/github-project-ledger/README.md`. Use repository state, current
user instructions, local documentation and tests as authority. Keep Web and
Server separate and share `openapi/ledger-v1.yaml`. Do not use Notion or
historical product plans to gate work.

## Runtime and ownership

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
- Lost repository access hides protected GitHub metadata and blocks new
  project targets; owners retain control of their historical expenses.
- Creem checkout does not grant entitlement. Signed, idempotent webhooks
  own payment facts; keep account revision fences, pending updates and replay
  recovery atomic. Platform payments never create ledger expenses.
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
- Jev suggestions are optional and never save expenses. Keep enable/evaluated
  flags off until real anonymized en/zh quality and runtime gates pass.
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
