# S06 — Projects, repository binding, categories

Status: Local implementation complete. The developer explicitly asked to continue through S10, superseding the design's one-stage pause rule.

## Implemented

- `pullwise_server/cloudflare_ledger_api.py` implements owner-scoped project list/detail/create/update and category list/create/update/archive. `cloudflare/server/src/entry.py` mounts ledger routes.
- Project creation uses a live GitHub App installation/repository read and the stable numeric repository ID. Reads hide the repository name after lost access, while keeping the owner's historical ledger visible. Reactivating an archived project requires live access.
- Cookie/API-key authorization and resource SELECTs share one D1 read batch. Writes fence the saved user and credential, expected revision, and unique category/project constraints in a D1 write batch. Owner mismatch returns 404; stale `If-Match` returns 412.
- `tests/test_ledger_routes.py` covers project/category CRUD, revision conflicts, cross-owner hiding, and lost access.

## Verification and limitations

- Focused SQLite-shaped D1 route tests passed. Worker module synchronization and Python compilation passed in the combined S06–S08 run; `python3 scripts/check-ledger-s01.py --allow-placeholders` passed.
- No real D1, Wrangler, Cloudflare, GitHub OAuth/App, or remote migration test was run. CI status could not be read because `gh` is not installed here.
- The SQLite harness approximates D1 batches; Cloudflare Python FFI and live GitHub behavior remain for S18. The inherited placeholder domains and D1 IDs remain.

## Next entry

S07: use the existing ledger auth/write fence for exact-money expenses, project/shared target checks, idempotency, revision updates, soft deletion, and audit events. Schema is `0001_ledger.sql`; API contract is `openapi/ledger-v1.yaml`.
