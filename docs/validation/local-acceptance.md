# Current local acceptance

Updated 2026-09-28. Companion: [Web acceptance](../../../pullwise-web/docs/validation/local-acceptance.md).

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
