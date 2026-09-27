# S15 — Retire PR/CI/Updates Server runtime

Status: **local cleanup complete** on 2026-09-28. Cloudflare runtime acceptance remains open for S17/S18.

## Changes and decisions

- Removed the old local VM HTTP entry, SQLite database layer, PR/CI/Updates collection, analysis, jobs, old product API, related scripts and historical tests. The earlier Worker entry had already stopped routing those APIs. `openapi/product-v1.yaml` was previously removed; `openapi/ledger-v1.yaml` is the active contract.
- Retained the Worker-owned GitHub OAuth/App identity and repository checks, hashed API keys, account/session rules, Creem catalog, checkout/subscription mutations, signed webhook receipts and payment history, plus ledger modules and tests. These live in the `cloudflare_*`, `billing_*`, `creem_*` and ledger files, independently of the removed VM modules. Existing billing/identity D1 tables and historical payment facts are not dropped.
- Added `cloudflare_d1_mapping.py` and `billing_account_rules.py` to `cloudflare/server/sync_server_modules.py`. Its relative-import closure check now rejects omitted Worker modules. The account projection keeps payment/account CAS metadata without restoring old processing quotas; PR/CI/Updates reservation and publication commands were removed. The Server README now names the Worker as the runtime.

## Local verification

- Explicitly ran every existing `tests/test_*.py` except the preexisting untracked `tests/test_jev_sdk_child_adapter.py`: **136 passed**. This includes Worker GitHub identity, Cookie/API-key, billing mutation, Creem webhook replay, account adapter and ledger tests.
- `python3 cloudflare/server/sync_server_modules.py --check`, `python3 scripts/check-ledger-s01.py --allow-placeholders`, packaged Worker-module import, and `git diff --check`: passed.
- CI status was unavailable locally; no GitHub Actions result was claimed. No real workerd, GitHub, Creem, D1 or Cloudflare remote test was run.

## Remaining gate and next entry

The removed VM payment and identity tests covered an obsolete runtime; the retained Worker tests are synthetic and cannot prove live OAuth/Creem or Python Worker FFI. In particular, Python CSV chunks bridged to `ReadableStream` still need local workerd verification. Do not alter or commit the unrelated untracked Jev child test.

Continue S12 Web locale completion, then S17 cross-project contract, permission, accounting, payment and deployment-script checks. Preview domains, D1 IDs and Wrangler credentials are absent, so S18 remote migration and preview acceptance are deferred.
