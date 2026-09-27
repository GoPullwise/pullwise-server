# S15 — Retire PR/CI/Updates Server runtime

Status: Incomplete. The developer explicitly requested progress through S18; this handoff records the remaining gate rather than declaring S15 accepted.

## Completed

`cloudflare/server/src/entry.py`, `cloudflare_http_contract.py` and `sync_server_modules.py` now route and package only ledger, GitHub identity, API keys, Creem billing and optional Jev modules. Old public watches, sources, items and service routes return 404 in the target Worker. Shared principal and billing projections were split from old product modules; old entitlement fields are removed from the new public catalog. `openapi/product-v1.yaml` was removed and the ledger OpenAPI remains. Target CI now checks current Worker modules and 84 focused ledger/identity/payment tests. The root and Worker READMEs describe the target runtime.

## Verification and blockers

The focused Server suite passed 86 tests locally, module sync and S01 static checks passed. A full pytest run excluding the preexisting untracked `tests/test_jev_sdk_child_adapter.py` produced 58 failures and 1197 passes because the old local VM runtime and retired product tests remain in the repository. Unrestricted collection also fails on that untracked test's missing `jev_sdk_child_adapter` module; it was not modified or committed. Old collection/analysis modules and their historical tests still need physical removal or refactoring of identity/payment fixtures. The old `launcher.sh`, `git-watch.sh`, reverse-proxy script, isolated `cloudflare/probe/` and stale local Worker verification scripts were removed in follow-up cleanup. Wrangler is now pinned in `cloudflare/server/package.json`, with its lockfile and config schema colocated. S15 is therefore not complete. No Cloudflare remote test ran; CI status unavailable locally.

## Next entry

Finish removing or replacing the remaining local VM PR/CI/Updates runtime and its scripts/tests without losing GitHub identity or Creem payment coverage. Re-run the current CI suite and an unrestricted collection excluding only explicitly retired tests. Then S16 Web cleanup and S17 integration can be accepted.
