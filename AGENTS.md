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
- The fresh, unexecuted migrations contain current identity, payment, key
  and ledger tables. Account authority tracks billing revisions and cycles,
  not old processing quotas. Billing DTOs expose subscriptions and events,
  not historical model-processing usage. Operational plan limits remain
  unconfigured; do not invent prices or ledger allowances.
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
- Preview/production use separate configs and databases. Deploy guards must
  reject placeholders. Keep credentials in Cloudflare Secrets; never expose
  tokens, private keys or account snapshots in logs or documentation.
- `docs/validation/local-acceptance.md` records current evidence and pending
  S17/S18 gates. The Python CSV generator to `ReadableStream` bridge remains
  unverified in workerd; local success is not deployment authorization.

## Repository hygiene

Keep current source, tests, contracts and durable documentation. Do not retain
unused modules, phase-by-phase stale handoffs, prototype fixtures, local logs,
screenshots or generated caches in version control. Runtime/build dependencies
and CodeGraph indexes are local tooling, not product source. Preserve user
changes and data; retire local databases outside the product tree before any
approved cleanup. Record current acceptance in one document per project.
