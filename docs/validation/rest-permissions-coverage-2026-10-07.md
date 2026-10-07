# REST permissions coverage — 2026-10-07

The current Server enforces nine ledger scopes, four workspace roles, and the
intersection of credential, workspace membership and target restrictions. This
review found no permission bypass in the supported application write path.
The new isolated local key tests pass **68 cases**; no network, real accounts,
payment or provider service was used by this review.

## Scope and role contract

`Y` means the role may hold/use the scope. A key also needs that scope explicitly;
the default key contains only the first five read scopes.

| Scope | Canonical operation | Owner | Admin | Editor | Viewer | Local key route evidence |
| --- | --- | --- | --- | --- | --- | --- |
| `profile:read` | GET `/api/v1/me` | Y | Y | Y | Y | Own-scope success; absent-scope 403 |
| `projects:read` | GET projects/detail/repositories | Y | Y | Y | Y | Own-scope list success/absence; detail/repository isolation in existing tests |
| `categories:read` | GET categories | Y | Y | Y | Y | Own-scope success; absent-scope 403 |
| `expenses:read` | GET expenses/detail/export CSV | Y | Y | Y | Y | Own-scope list success/absence; restricted detail/export in native evidence |
| `reports:read` | GET summary/timeseries/categories reports | Y | Y | Y | Y | Own-scope summary success/absence; native all-three reports |
| `projects:write` | POST project; PATCH settings/archive/links | Y | Y | — | — | Owner/Admin standalone create + PATCH; target/workspace denials |
| `categories:write` | POST/PATCH category; DELETE archives category | Y | Y | — | — | Owner/Admin create + rename + archive |
| `expenses:write` | POST/PATCH/DELETE expense | Y | Y | Y | — | Existing restricted-key create/replay; native exact financial/audit checks |
| `suggestions:use` | POST draft suggestion and decision | Y | Y | Y | — | Owner/Admin/Editor synthetic answer + decision; missing scope/target denial before model |

CSV requires `expenses:read`, not `reports:read`. Advanced draft suggestions
require `suggestions:use`; automatic assistance inside an authorized expense
write intentionally requires only `expenses:write`. The latter distinction has
its own regression in `test_ledger_automatic_assistance.py`.

The new test's expected role table is independent of the implementation table.
Its **36 scope × role cases** issue each allowed single-scope key and validate the
persisted key principal, actor, workspace and current role; forbidden issuance
returns `ROLE_FORBIDDEN` with no key or commercial write. The existing
`test_role_matrix_is_enforced_before_mutations` separately covers all 36 cookie
principal cells. These are complete issuance/principal matrices, not a claim
that every endpoint/method is executed with every role/credential combination.

## Credential and target intersections

| Boundary | Enforced behavior | Inspectable coverage |
| --- | --- | --- |
| Cookie versus external key | Cookie writes require a trusted Origin for Lax/Strict/None, including malformed Authorization; external key writes remain supported | `test_worker_application_security.py`, `test_cloudflare_api_key_routes.py` |
| Bearer versus alternate key header | Both `Authorization: Bearer pwk_…` and `X-Pullwise-Api-Key` execute each five read routes with only its required scope; deleting that scope yields 403 even when all eight others remain | 20 cases in `test_rest_key_permissions.py` |
| Mixed credentials | Key plus cookie, or conflicting keys, fails closed; key-header project write succeeds without a cookie | New header-write/mixed-cookie test; principal source rejects conflicts |
| Session-only management | Key list/create/revoke require the issuing cookie session; workspace governance also rejects keys and bearer sessions | Key route tests; `test_api_keys_and_bearer_sessions_cannot_govern` |
| Project allowlist/shared pool | An allowlist grants no shared access. Lists, detail, report/CSV and both existing/new expense targets remain restricted; fixed project allowlists cannot create a fresh project ID | New project/suggestion target tests; ledger/standalone/native tests |
| Workspace/current member | Bound keys reject workspace override. Membership revision/removed state and current role apply; demotion/rejoin cannot revive an old key | Workspace authorization and workspace lifecycle tests |
| Actor versus owner | Actual issuer/member supplies credentials and GitHub grant; immutable workspace owner scopes finance and pays quota. No owner-token lending | Workspace/project repository tests and native SQL assertions |
| Atomic recheck | Resource read batches recheck key/session, exact actor/owner snapshots and membership. Write batches recheck those facts plus revisions/targets/categories; CSV rechecks on later pages | Snapshot/revocation races, stale owner/actor/session/member fences, provider-time key revocation tests |
| Secret/revocation/expiry | Key creation returns its token once; storage is hash-only and list DTOs omit secrets. Revoked/expired keys cannot read; issuer revocation remains available under commercial quota exhaustion | Key route tests; production-ingress tests; immediate native revocation |

Categories belong to the selected workspace, so `categories:read` exposes that
workspace's category vocabulary even for a project-restricted key. Repository
discovery uses the actual actor's GitHub grants; its occupancy metadata is
filtered by the key's project visibility. Project `canCreateExpense` describes
financial target eligibility; a Viewer still has `writeExpenses=false` and no
write scope.

## Evidence and limits

- [New scope/key tests](../../tests/test_rest_key_permissions.py): **68 passed
  in 2.57 s** with Python 3.10.12 and isolated SQLite D1 transaction fixtures.
  Suggestions use one deterministic in-process synthetic answer and create no
  expenses, expense audit or extra commercial writes.
- Read-only collection confirmed **301 existing tests** across the 11 ledger,
  workspace, key, security, assistance and repository/standalone files plus
  `test_worker_production_runtime.py`. These files also contain business tests;
  301 is a collection count, not 301 independent permission assertions.
  The final release-controller full-suite run records **1,015 passed + 56
  subtests in 21.15 s**, including the 68 new cases. Its private numeric/log
  proof is `/workspace/qa-private/blank-projects-final-server-with-rest-check.log`;
  the preceding 947-case result is preserved separately.
- [Current standalone native acceptance](blank-projects-native-2026-10-07.json)
  passed 49 local HTTP requests/18 mutation attempts, four synthetic roles,
  a restricted Editor key, exact-once expense/replay, allowlist/shared/missing
  scope denial, owner quota/actual-actor audit and immediate revocation. It uses
  actual Worker/NativeD1 locally, with no real provider or remote requests.
- [Earlier workspace native acceptance](workspaces-native-local-2026-10-06.json)
  and [two-real-account focused acceptance](projects-two-real-users-focused-2026-10-06.json)
  provide separate role, membership-generation and removed-member evidence.
  They remain dated prior evidence, not an execution of this new scope matrix.
- The final online REST controller is deliberately bounded to **one temporary
  default-five-read key**, 40 business HTTP requests, six mutation attempts and
  two confirmed key writes. Its planned success path is 28 HTTP requests when
  the optional second project exists; it reads existing data, checks all four
  absent write/use scopes, then revokes and verifies rejection/metadata absence.
  At this review's completion its 11 mock-controller tests passed; a completed
  real run has not been audited here. Its result must remain separate from
  local coverage, including empty-data/date-filter and optional-project limits.

This review added only a test file and this document. It did not change product
authorization, expand legacy-state handling, run remote requests, create online
keys, deploy, or perform real payment/model calls.
