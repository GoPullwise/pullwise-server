# S05 — Ledger identity, billing and API keys

Status: S02–S05 local implementation complete. The developer asked to continue
through S05 without intermediate handoff documents. S01's earlier handoff remains;
this is the single final handoff for the combined run.

## Implemented

- S02 Web: `src/api/ledger.js` maps the browser `/api` proxy to Server
  `/api/v1/*`; Worker proxy tests cover Bearer forwarding, OAuth redirect and
  Cookie headers. `check:workers` validates local configuration before the
  existing deployment command.
- S03 Server: `cloudflare_github_identity_http.py` and
  `cloudflare_github_gateway.py` mount GitHub OAuth, App installation callback,
  Session and authorized repository reads. New accounts initialize the existing
  billing authority. Lost GitHub access hides repository metadata.
- S04 Server: `cloudflare_billing_mutations.py`,
  `cloudflare_creem_gateway.py` and `cloudflare_billing_catalog_refresh.py`
  mount Cookie-only checkout, upgrade, cancellation, resume and current pricing.
  Signed webhook facts continue through the existing receipt and settlement
  logic; checkout response alone does not grant a paid plan.
- S05 Server: `api_key_dto_rules.py` now accepts ledger scopes only and stores
  `projectIds` and `shared` restrictions. `cloudflare_ledger_auth.py` checks
  scope, target and current key state with the D1 read batch; revocation blocks
  the next request. `/api/v1/me` and `/api/v1/repositories` use the ledger
  authorization path. `0002_identity_billing_keys.sql` adds the runtime tables
  required by S03–S05 to a fresh D1 database.

## Local verification

- Server: 18 focused unittest cases passed; all six synthetic Creem webhook
  receipt tests passed, including duplicate delivery and dirty projection
  repair. Fresh migrations passed SQLite execution and Worker health check.
- Web: `npm run check` passed (lint, 370 tests, build);
  `npm run check:workers` passed.
- Server module sync check, Python compilation, S01 static deployment check,
  and `git diff --check` in both projects passed.
- No real GitHub, Creem, Cloudflare, Wrangler or remote D1 calls were run.

## Contract and next entry

The API Key default is read-only. Missing `projectIds` permits the owner's
projects; shared-pool access requires `shared: true`. Key restrictions never
grant another owner's resource. S06 should append its resource SELECT/guarded
write to the authorization batch and validate the saved key/user/session before
returning data. Project ownership and live GitHub authorization are separate
checks. Start with project, repository binding and category CRUD from
`openapi/ledger-v1.yaml` and `0001_ledger.sql`.

Cloudflare Python JS FFI for GitHub/Creem HTTP and WebCrypto, real OAuth/App
installation behavior, real Creem settlement, and remote migration remain
unverified until S18. Production/preview configs still contain placeholder
domains and D1 IDs. The old PR/CI/Updates runtime and UI remain during this
transition and are scheduled for S15–S16 removal; API-key UI copy is scheduled
for S12.

## Commit and regression audit

- Server ledger S01–S05: `1259494`; Web ledger client/proxy: `7444291`.
- Earlier completed Web scan/UI cleanup: `d9f7673`. Its full `npm run check`
  passed: lint, 370 tests and build.
- Earlier Server scan/Agent-first/Worker/Model Gateway retirement and local
  PR/CI fact work: `cf861f1`. Python compilation, application import, 109
  billing/database/product/GitHub tests and 52 focused CI/GitHub/Jev/local
  identity tests passed. Static import audit found no missing production module.
- The broader legacy product test run is not green: 209 tests produced 11
  failures and 4 errors. Old API-key expectations conflict with S05 ledger
  scopes; two representative failures reproduce on `1259494` before the
  cleanup commit. Other errors include unavailable `pytest` and sandbox socket
  permission. Migrate or remove old product tests with S15, and keep the new
  ledger authorization tests as the active contract.
- `tests/test_jev_sdk_child_adapter.py` remains untracked because its
  `pullwise_server.jev_sdk_child_adapter` implementation is absent. It is
  unrelated to S01–S05 and should be addressed with the later Jev stage.
