# Current local acceptance

Updated 2026-09-28. Companion: [Web acceptance](../../../pullwise-web/docs/validation/local-acceptance.md).

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

S01–S16 local implementation is complete. **S17 runtime acceptance remains
open**: the Python CSV async generator to JavaScript ReadableStream bridge
has not run in workerd, and real Worker/browser integration remains unverified.
Synthetic SQLite is not proof of Cloudflare FFI, D1 batch/transaction behavior
or provider behavior. The browser connector was unavailable during cleanup.

All Wrangler/workerd and D1 commands, including local probes, remain paused
until explicit user authorization. No remote migration, preview/deployment,
cron or real GitHub/Creem/Jev call was run. S18 also needs reviewed preview
domains/routes, distinct D1 ID, credentials, OAuth/App callbacks, Creem test
products/Secrets and migration/rollback. Before remote validation, document
per-request row/operation bounds, frequency, pagination/cache and cost guard.
Keep Jev disabled until the real anonymized en/zh quality gate passes.

The three migrations describe the fresh, unexecuted target schema. Cleanup
does not migrate or delete an existing remote database. Retired local state
was preserved outside both product projects rather than treated as disposable
expense or payment data.
