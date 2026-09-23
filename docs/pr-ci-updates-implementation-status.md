# PR / CI / Updates 1.4 implementation status

Last updated: 2026-09-22. This file records implementation evidence; it does
not replace `../../docs/design/pr-ci-updates/README.md`, its 01–07 contracts,
or the Cloudflare deployment appendix 08.

## P0 baseline

The four repositories were clean before implementation started. Recovery
anchors and remotes were recorded before any local project removal:

| Repository | Baseline HEAD | Recovery remote |
| --- | --- | --- |
| pullwise-server | `0122367e27e42aafec54a5094e153cab63509c7f` | `https://github.com/GoPullwise/pullwise-server.git` |
| pullwise-web | `f5b1b2fb755aeb015ecb0edc4c5a12bec7731f9a` | `https://github.com/GoPullwise/pullwise-web.git` |
| pullwise-admin | `0760a8a0e66c2a964bab6151da758f7c8145be17` | `https://github.com/GoPullwise/pullwise-admin.git` |
| pullwise-worker | `d26fdffda93833b7dd1bd41d415adab4a856182b` | `https://github.com/GoPullwise/pullwise-worker.git` |

Only ignored local state was present: Server virtualenv/cache/log/CodeGraph
data, Web/Admin `node_modules`/build/log data, and Worker `node_modules`/build/
temporary data. No tracked or untracked user changes were present. The user has
authorized eventual deletion of the two local Admin/Worker directories and
will delete the corresponding GitHub repositories manually. No remote deletion,
deployment, production worker shutdown, credential change, or production data
operation is authorized.

The leading Server and Web `AGENTS.md` target blocks now mark the old Reviewer,
Worker fleet, Agent-first, and Model Gateway rules as historical cleanup
evidence. Their remaining references are still present until P4/P6 dependency
removal is verified.

Protected baseline command:

```text
D:\Python313\python.exe -m pytest \
  tests/test_github_auth_contracts.py \
  tests/test_billing_contracts.py tests/test_billing_routes.py \
  tests/test_billing_webhooks.py tests/test_security_contracts.py \
  tests/test_api_security_extensions.py -q
```

Result: `299 passed, 41 subtests passed`. This is evidence on Python 3.13 only.
The repository's `.venv` points at a missing/broken Python 3.10.12 installation
and fails during interpreter startup with `ModuleNotFoundError: encodings`.
Therefore target Python 3.10 runtime, SDK import, and full CI remain unverified.
No dependency was installed to work around this gate.

The retired hash-pinned Reviewer authority gates were removed from Server and
Web CI after the target blocks changed. Server retains pytest, pip check,
dependency audit and shell checks. Web retains lint, tests, build, production
audit and Cloudflare worker syntax checks. Current Web `npm run check` result:
46 files / 607 tests passed, lint passed, and the production build completed.

The read-only `legacy_product_inventory` fixture now counts protected account,
session, GitHub-state and billing-event collections plus active scan jobs,
reserved scan units and reservation-ledger rows without creating or mutating
legacy tables. It provides the settlement gate for eventual old-path removal.

P0 remains incomplete: the 1440px/390px light/dark non-Dashboard screenshot
baseline is not yet captured.
Admin/Worker must not be removed before Server/Web no longer depend on them and
the old active scan/reservation path is accounted for.

## P0.5 offline rule evidence

Implemented without a model key or network dependency:

- stable personal/shared `watchScopeKey` identity independent of public watch ID;
- normalized-interest `contextHash` reuse with monotonic audit
  `contextVersion`, including A→B→A;
- monotonic ItemVersion snapshots that include every source revision and never
  reuse an earlier occurrence after a real transition;
- adjacent, unique action-signature handling inheritance only; context changes,
  ambiguous matches, and new rule actions reopen handling;
- empty-body formal `CHANGES_REQUESTED` as a rule action;
- pending assessment keeps an existing item visible as `needs_confirmation`
  without advancing its attention timestamp;
- Updates same-unit joint projection, conflict-to-confirmation, and partial
  coverage that does not infer negative release-wide signals;
- SQLite watch/control persistence across delete/recreate and restart;
- atomic owner/global rolling and UTC-month provider-attempt admission with a
  mandatory global monthly limit and idempotent attempt IDs;
- atomic successful-processing reservation/consume/release with idempotent
  charge keys, module totals, restart persistence, and last-slot contention;
- separate manual fact-sync jobs and analysis jobs; manual sync can only create
  `sync_repository`/`sync_watch`, while analysis requires a server-owned
  `TrustedTrigger` enum and active logical jobs are deduplicated;
- a P1 `openapi/product-v1.yaml` list/overview/source/Item/handling/watch/sync/
  usage contract shared by cookie sessions and API keys. It deliberately omits
  user model-submission routes and unimplemented P5b endpoints;
- append-only Item handling events with actor/itemVersion, optional note,
  classification feedback, and simultaneous current-item/current-revision CAS;
- source-context publication fences for context, configuration and authorization
  revisions, including permission freshness/accessibility checks before an Item
  snapshot can become current;
- shared Cookie/API-key product routes for watches, sources, Items, overview,
  handling, usage, profile and sync-job status; dual identity is rejected,
  Cookie writes retain Origin checks, resource restrictions are enforced, and
  repeated reads/manual sync do not create analysis jobs;
- offline GitHub source normalization for PR review bodies/comments, CI failed
  job attempts/windows and Releases, with GitHub write operations absent;
- deterministic bounded Release-unit selection that never truncates a unit,
  reports partial/unavailable coverage, and makes no negative inference about
  omitted material;
- conditional CI successor projection that accepts only explicit verified job
  identity and, across runs, explicit lineage evidence; common names or SHAs
  alone remain unknown;
- versioned Jev question builders for all six PR semantic choices, nine CI
  symptoms per evidence window, and five Updates judgments per change unit.
  Candidate keyword ranking does not emit classifications or action labels;
- current-scope API keys default to read-only product scopes; RepositoryService
  and watch capacity use atomic SQLite last-slot admission, and Cookie/API-key
  writes share user-scoped idempotency records across transport aliases;
- initial backfill persists one fixed bounded source set plus its completed
  subset under the stable processing-control key. Restart and watch recreation
  replay the same set instead of expanding into later historical pages;
- processing eligibility now freezes the first authoritative `eligible_since`
  across restart and watch recreation. Discovery eligibility accepts only
  authoritative create/change times at or after that boundary or membership in
  the fixed backfill set; opaque cursor/high-watermark checkpoints advance by
  exact CAS and survive restart without lexical ordering guesses;
- immutable assessment storage caches only succeeded results by billing domain,
  SourceVersion, `contextHash`, question/extractor/model and complete input hash;
  audit-only `contextVersion` changes may rebind a result, while discarded
  responses never enter the reusable cache;
- every published assessment now persists a complete public projection with
  source/version, dependencies, question bindings, current evidence IDs,
  evaluated/publish context versions, answers and usage. Bindings must exactly
  cover answer keys and reference current snapshot evidence. Semantic cache
  replay reuses the assessment id while rebinding current context/evidence and
  appends `assessment_rebound` so prior handling is not revived;
- `publish_assessment_result` commits the validated assessment, current
  ItemVersion and successful-processing consumption atomically; stale source or
  publication fences roll back the assessment/Item writes and keep the original
  reservation. The focused success/rollback regressions both pass.
- trusted `analyze_source` execution now freezes source/context/config/auth and
  reservation bindings at enqueue, claims/reclaims with a random expiring token,
  and validates the token plus every binding inside the atomic assessment/Item/
  usage publication transaction. Expired executors cannot publish or consume;
  disabled, stale, unauthorized, or invalid-reservation jobs become terminal and
  release an active reservation in the claim transaction. Existing pre-claim
  product-v1 job tables receive the required nullable claim/binding columns at
  initialization.
- retryable failures persist `retry_wait` and `next_attempt_at` on the same job;
  claims before that time are rejected, attempt 3 is terminal, and final or
  non-retryable failure atomically releases the processing reservation. A stale
  claim token cannot alter a reclaimed job's retry state.
- analysis enqueue now enforces the default global 1000/per-billing-owner 100
  active-job limits atomically; rejection releases the new reservation and
  exposes throttled source status. Claims rotate across billing owners using a
  durable order with insertion-order tie breaking.
- a newer source/context binding supersedes the older active generation and
  releases its reservation. A successor to a running generation cannot claim
  before the old lease expires, while a failed attempt-3 input cannot create a
  fresh generation or retain a redundant reservation.
- Item handling current-state projection now uses append insertion order rather
  than random UUID order, eliminating same-second session/API-key update races.
- strict local Jev request/response validation for fixed model, exact question
  keys, enums, probabilities, confidence, usage, duplicate JSON keys and byte
  bounds. State is text-only (string, recursively textual JSON object, or text
  array) and rejects media/binary/non-text leaves. The SDK transport itself is
  not installed or enabled.

The current model is pinned exactly to `jev-1.13.0`; request construction
rejects `jev-latest` and other unvalidated model identifiers. Focused validation:
`tests/test_typesafe_client_contracts.py` reports 7 passed and 18 subtests.
The fixed-backfill restart/recreation regression also passes independently.

The supplied Jev account limit is 1,200 requests/minute and 250,000 input
tokens/second. Pullwise remains capped at one credential, concurrency 2, 60
actual attempts/rolling minute globally and 6 per billing owner. This leaves
roughly 20x request-rate headroom; retries consume the same admission budget.
No API-key pool or per-IP capacity assumption is used.

The current published Jev input price is `$42 / billion tokens`, equivalent to
`$0.042 / million tokens` (`$0.000042 / thousand tokens`). This is a cost
baseline, not permission to spend: the mandatory global monthly attempt limit
remains unset until P0.5 measures actual average/p95 input tokens and chooses an
explicit deployment budget. Output is not included in this input-price figure.

Exact 14-file current-target verification (PowerShell, from `pullwise-server`):

```powershell
D:\Python313\python.exe -m pytest `
  tests/test_product_domain_contracts.py `
  tests/test_product_store_contracts.py `
  tests/test_product_jobs_contracts.py `
  tests/test_product_openapi_contract.py `
  tests/test_product_ci_target.py `
  tests/test_legacy_product_inventory.py `
  tests/test_product_api_security.py `
  tests/test_product_api_routes.py `
  tests/test_product_entitlements.py `
  tests/test_github_source_contracts.py `
  tests/test_update_filter_contracts.py `
  tests/test_ci_triage_contracts.py `
  tests/test_typesafe_client_contracts.py `
  tests/test_jev_questions.py -q
```

Current result after trusted analysis claim/publication wiring:
`115 passed, 24 subtests passed` in 252.89 seconds. The handling ordering
regression also passed once and then five consecutive focused reruns. An earlier
current-target run plus protected API-key, CSRF/security, GitHub authorization and
Creem billing suites produced `392 passed, 50 subtests passed` in 351.17
seconds. These are Python 3.13 engineering fixtures, not target-runtime, Jev quality,
GitHub field-coverage, transport-exit, token-cost, or user-benefit evidence.

Remote CI review on 2026-09-21 found no run for the uncommitted product-v1
changes. The latest `main` run for baseline HEAD
`0122367e27e42aafec54a5094e153cab63509c7f` failed on 2026-09-08 in two
retired Pi/Worker publication tests (`test_pi_result_publication.py`), after
1,431 tests passed. That historical failure does not exercise the new job
executor, but full target-runtime CI remains unverified until these changes are
committed and run by the current workflow.

## Trusted discovery integration slice — 2026-09-21

Added `product_discovery.py`, `github_webhooks.py` and
`tests/test_product_discovery_contracts.py`. The HTTP receiver is wired to
`/webhooks/github`; the executable orchestration uses injected server-owned
read-only fact readers and budget resolvers. It imports no model transport.
`main(fact_sync=...)` and `PullwiseThreadingHTTPServer(fact_sync=...)` can host
one background fact worker; a real loopback-server test proves normal scheduling
works without any HTTP request. Default startup has no live reader configured.

Implemented and exercised against temporary SQLite databases:

- raw-byte HMAC, event/action allowlists, durable App/delivery deduplication,
  stored installation/repository binding and minimal identifier-only receipts;
- 30-second event stability/refetch and persistent normal schedule cadence
  (PR/CI 15 minutes, Updates 6 hours); manual sync does not advance that clock;
- facts committed before eligibility/admission, with shared parent read
  generations and authoritative-time checks preventing stale responses from
  replacing newer snapshots; conflicting IDs in one page are rejected;
- stable eligibility, fixed initial-backfill selection, exact discovery CAS,
  and atomic reservation/queue admission through `ProductStore.atomic()`;
- old/pagination-late sources remain `not_scheduled`; new eligible versions
  reserve once; duplicates, concurrency, restart and watch recreation retain
  the same control, cursor and billing identities;
- expired or revoked authorization blocks ingestion/admission; signed
  installation deletion/suspension or repository removal hides existing
  contexts and cancels jobs; configuration writes also fence pending work;
- queue rejection keeps facts and commits throttling with released usage;
  unexpected admission failure rolls back quota/checkpoint but retains facts;
- all consecutive coalescings retain the first running lease deadline;
  same-input generations carry attempt counts across authorization/configuration
  changes; watch configuration epochs do not regress when public IDs change;
- released queue-rejection reservations can re-enter the current billing
  period without changing active or consumed reservations from earlier periods;
- CI normalization now retains `completedAt` and the actual `timed_out`
  conclusion, allowing authoritative-time eligibility without local first-seen
  inference. PR/CI/Updates scheduling fixtures all exercise the same pipeline.

Test-first evidence: the initial 11 integration tests failed on the missing
authorization/ingestion seam. Subsequent focused red tests exposed watch
configuration regression after recreation, queued work after disabling analysis,
missing webhook routing, CI timeout omission, authorization expiry during read,
the first-event stability boundary, cross-period reservation, duplicate-page
charge binding, signed revocation, repeated coalescing lease loss, and attempt
reset through authorization revisions, missing background-loop assembly, and
manual edits retaining an obsolete assessed status/clearing context staleness.
The final suite contains 33 passing
integration tests.

Final verification on `D:/Python313/python.exe`:

- The exact 14-file command above: **115 passed, 24 subtests passed in 23.33s**.
- `tests/test_product_discovery_contracts.py`: **33 passed in 8.79s**.
- `tests/test_product_discovery_contracts.py tests/test_security_contracts.py
  tests/test_api_security_extensions.py tests/test_github_auth_contracts.py
  tests/test_billing_webhooks.py`: **245 passed, 31 subtests passed in 24.41s**.
- Changed Python modules/tests passed `py_compile`; `git diff --check` passed
  with the existing LF-to-CRLF warnings.

The default temporary directory/cache encountered Windows access errors in one
run. Successful final commands set `TEMP` and `TMP` to
`F:/Pullwise/.test-tmp/discovery` and pytest cache directories under
`F:/Pullwise/.test-tmp`. No interpreter, dependency or permission installation
was used. Python 3.10 remains unverified.

An additional broad run including the historical `test_api_key_routes.py`
reported **10 failed, 249 passed, 31 subtests passed**. The failures expect
retired scan repository DTOs/scan operations or the old flat `message` error
shape. They remain unresolved and are not included in the green current-target
claim; neither their assertions nor the existing API-key changes were removed
to manufacture a pass. Current product API-key/session/security tests pass.

Remote CI was rechecked with `gh run list` and `gh run view`: the latest run is
still [34212039198](https://github.com/GoPullwise/pullwise-server/actions/runs/34212039198),
failed at the test step on unchanged baseline HEAD `0122367`. No run covers
these uncommitted changes. Fetching the old failed log timed out during TLS;
the earlier historical failure diagnosis above is not a new log-validation claim.

Boundary: this is an offline trusted-ingestion integration slice, not a live
GitHub or model rollout. `read_page`, actual permission-proof refresh and the
live adapter composition are still required for production fact sync; the
server-owned background-loop assembly is tested with an injected reader.
The seam consumes locally normalized authoritative-reader fixtures; no
actual GitHub field-coverage result is claimed. Context refresh remains a
separate bounded/cooldown path; discovery refuses to disguise it as new source
work. Real log transport, complete three-module source loops, source-to-Item
rule projection and live permission coverage remain P3 work. Production Jev
is disabled, and no SDK, key, paid model call, deployment or production change
was made.

## Independent permission renewal slice (2026-09-21)

The next integration prerequisite is implemented: permission proofs expire in
at most five minutes, so they cannot depend on the 15-minute PR/CI or six-hour
Updates fact schedule. `ProductFactSync` now accepts a server-owned
`refresh_authorization` checker and runs `run_authorization_due` before event
and scheduled fact processing. The default checker remains unconfigured.

- Renewal starts 60 seconds before expiry. Durable per-control claims use a
  random token and a 120-second lease; failures retain a 60-second retry delay
  across restart. Network I/O runs outside SQLite write transactions.
- A typed `DiscoveryAuthorizationProof` validates fresh observation time,
  strict boolean/integer fields and the five-minute maximum lifetime. A late
  result cannot publish after lease expiry, target/configuration changes,
  another proof update or signed installation revocation.
- Ordinary renewal preserves authorization revision and the original analysis
  authorization boundary. A verified access transition increments the revision;
  denial hides source contexts, cancels queued/running analysis and releases
  reservations through the existing atomic store path.
- The checker receives the watch owner and target-repository scope as well as
  module/installation/upstream/billing bindings. It must verify all relevant
  authority; public repository visibility alone is insufficient.
- Renewal does not read source facts, change discovery checkpoints/backfill,
  establish processing eligibility, reserve processing or enqueue analysis.
  GET and manual sync never invoke the checker. One failed target does not
  prevent later permission targets from being checked.

Test-first evidence: nine initial permission tests failed on the absent proof
type/renewal entrypoint; all then passed. An additional owner-scope test failed
on the missing `owner_id` before adding the scope fields. The suite now has
48 discovery tests (15 added), including concurrent workers using separate
SQLite connections, expired lease reclamation, restart/backoff, invalid proof
boundaries, revocation and archive races. The real HTTP-server background-loop
test now starts with expired authority and verifies renewal precedes fact sync.

Final verification used `D:/Python313/python.exe`, with `TEMP` and `TMP` set to
`F:/Pullwise/.test-tmp/discovery` and pytest cache under `F:/Pullwise/.test-tmp`:

- The 14-file current-target command above plus
  `tests/test_product_discovery_contracts.py tests/test_security_contracts.py
  tests/test_api_security_extensions.py tests/test_github_auth_contracts.py
  tests/test_billing_webhooks.py`: **375 passed, 60 subtests passed in 58.83s**.
- `py_compile` passed for `product_discovery.py`, `product_store.py` and
  `test_product_discovery_contracts.py`; `git diff --check` passed.
- `gh run list` and `gh run view 34212039198` still show the old baseline
  `0122367` failing at the test step. No remote run covers this working tree.
  Python 3.10 remains unverified. Previously recorded old API failures were
  not modified or included in the green current-target claim.

This completes the renewal scheduling prerequisite only. Actual read-only
GitHub permission checks, credentials-to-checker composition, live readers and
field-coverage verification remain pending; the injected test checker is not
evidence of live GitHub authorization. Production Jev remains disabled. No
dependencies were installed, credentials used, deployment made or remote
changes published.

## P3 GitHub reference adapters and deployment design (2026-09-22)

The main delivery remains PR / CI / Updates through the shared REST contract
and P5a source/evidence/handling views. Cloudflare deployment is now explicit
for both Server and Web in design appendix
[`08-cloudflare-deployment.md`](../../docs/design/pr-ci-updates/08-cloudflare-deployment.md).
It defines runtime/storage candidates and CF0-CF3 validation alongside P3/P5a;
it does not freeze an untested runtime or block injected-adapter development.

This slice adds executable read-only adapters, tested against HTTP response
fixtures, without configuring default live startup:

- `GitHubRESTTransport` restricts requests to GitHub GET, rejects redirects and
  credential-bearing paths, bounds decoded JSON to 1 MiB, closes responses and
  sanitizes errors. It has socket inactivity timeouts, not a proven overall
  slow-stream deadline. No internal retry or model transport is present.
- `GitHubAuthorizationChecker` binds the current GitHub user to a server-owned
  account credential, verifies maintain/admin and App installation/module
  permissions, and checks shared watch target authority plus public upstream
  visibility. HTTP errors and malformed/uncertain checks never become positive
  proof. Resolver tokens are omitted from dataclass reprs. The checker preserves
  rate-limit retry time without exposing upstream exception messages.
- `GitHubReleaseReader.read_page` resolves stable repository ID, reads one page
  and rechecks the mutable repository name route. It validates scope-bound
  cursors and official next-page links, including `/repositories/{id}/releases`
  canonical links for the same verified ID. One call has at most three GETs;
  no artificial whole-scan page cap traps the persisted cursor.
- Releases have no reliable top-level `updated_at`. The reader uses known
  `published_at` only, ignores unverified edit fields and never treats local
  read time as authoritative change time. Old edits can update facts without
  new analysis eligibility. Missing pages/404 never infer deletion. Pages are
  not a complete snapshot; the current six-hour cadence implies a potentially
  long full scan for large upstreams.
- GitHub `Retry-After`/rate-reset metadata now survives permission refresh and
  event retry persistence. Fact-read cooldowns in `github_read_backoffs` apply
  across manual, event and scheduled paths and restarts. One unavailable due
  target no longer aborts later targets. These are per-target cooldowns, not a
  claim of completed account/installation-wide rate coordination.

Independent review found and repaired two integration defects. Actual public
GitHub pagination for `cli/cli` used the canonical ID route rather than the
name route; the reader now accepts that exact same-ID form and still rejects
other origins/repositories. Shared watch target-service installation/config
changes originally fenced renewal snapshots only: late facts and existing
analysis contexts could still use the old authority. The target-service
identity/revision now participates in the watch configuration stamp; service
writes cascade context invalidation and cancellation. Installation replacement
also invalidates the old proof and hides sources immediately. Running lease
deadlines and attempts survive cancellation; only independent fresh renewal can
restore access. Signed target-installation revocation covers shared watches
without requiring an App on the public upstream.

Test-first evidence includes missing-adapter import failures, three failing
retry/failure-isolation cases, a real canonical-pagination fixture failure,
shared-target mismatch cases, and five second-pass race/cancellation failures.
All corresponding focused tests passed after implementation. Composition tests
use the actual transport/checker/reader with HTTP fixtures and SQLite discovery:
expired proof renewal precedes fact sync, manual release edits do not refresh
permission or establish eligibility, and transport retry time persists through
the checker. They are integration evidence, not live credential/runtime proof.

Verification command: the 19-file previous renewal regression plus
`tests/test_github_transport_contracts.py`,
`tests/test_github_authorization_contracts.py`,
`tests/test_github_release_reader_contracts.py`,
`tests/test_shared_watch_authorization_fence.py` and
`tests/test_github_ingestion_contracts.py`, using `D:/Python313/python.exe`,
`TEMP=TMP=F:/Pullwise/.test-tmp/discovery`, `-q -p no:cacheprovider`.

Final result: **463 passed, 111 subtests passed in 66.75s** (88 additional
tests compared with the previous 375-test renewal regression). All changed
Python modules and new/changed tests passed `py_compile`; `git diff --check`
passed with the existing LF-to-CRLF warnings. The workspace-root design files
are outside the separate project Git repositories; their local links, trailing
whitespace and conflict markers were checked separately.

`gh run list` and `gh run view 34212039198` were rechecked: the latest Server
run is still [the failed old baseline](https://github.com/GoPullwise/pullwise-server/actions/runs/34212039198)
at `0122367`, with failure in the test step. No remote run covers these local
changes. The broken Python 3.10 environment remains unverified; the passing
count above is a current-target regression on Python 3.13, not the full
historical suite or a Cloudflare-runtime result.

Remaining P3 work includes server credential/account-to-binding composition,
PR/CI readers and log transport, source-to-Item rule projection, conditional
reads and credential-scope rate scheduling, and actual GitHub field/permission
coverage. Cloudflare CF1/CF2, Python/SDK runtime compatibility and P5a Web remain
separate unfinished mainline slices. No dependency was installed, Jev invoked,
credentials provisioned, remote change published or deployment performed.

## P3 PR/CI readers, native run state and credential composition (2026-09-22)

Parallel implementation continued the same PR / CI / Updates mainline and
shared REST target. No Web business logic, SDK, Cloudflare resources or default
live startup was changed in this slice.

- `GitHubPRReader` supports PR state, review bodies, PR discussion comments and
  inline review comments through bounded REST pages and event refetch. A cursor
  retains the open-PR page and collection position; canonical same-repository
  Link URLs are checked. Repository identity is verified around mutable name
  routes, and child pages refetch the parent before applying its closed state.
  One call emits at most 100 sources with at most four GETs.
- PR delivery receipts now carry a validated numeric `pull_number`, because a
  review/global PR identifier alone cannot locate every REST endpoint. It is a
  locator, not an authority claim. Existing receipts survive the nullable-column
  schema upgrade; no old payload body is reconstructed. PR state now uses its
  authoritative `updatedAt` to reject an older snapshot reopening a closed PR.
- `GitHubCIReader` reads run/attempt/jobs/steps, traverses attempt history and
  paginates jobs. Success and other nonfailure states travel in
  `FactPage.run_states`, not synthetic failure sources. Normal calls use at most
  five GETs; job events use five when their attempt is known, or at most six for
  bounded membership verification when the job API omits it.
- `github_run_states` preserves native state per repository/run/attempt under
  the same authority/config/generation-fenced fact transaction. Older upstream
  times and same-attempt terminal-to-nonterminal regressions are rejected.
  Each snapshot identifies its current jobs-page coverage; a last page does
  not manufacture a complete run or an execution recovery relation.
- `GitHubCredentialResolver` composes existing account/identity/installation
  access helpers through server-owned callbacks. It verifies owner and target
  identity, chooses the bound GitHub identity, and checks known token expiry.
  Issuance completion rechecks target, account, selected identity and installation
  access because unlink/revoke can occur independently. Exceptions/reprs do not
  expose tokens, and GitHub retry deadlines are retained. Reader token access
  requires an already valid proof and never invokes the permission checker.
- `build_github_fact_sync` now assembles all three readers and that resolver
  with the checker, rejects mismatched App IDs and unsupported modules, and
  performs no I/O during construction. An integration failure exposed that the
  scheduled path passed a snapshot preceding its own next-schedule write; it
  now passes the post-write snapshot rather than weakening credential fencing.

Independent review reproduced two further defects and both were fixed with
red/green tests: a completed CI attempt could regress to in-progress at the
same authoritative second, and a direct job event with known attempt could
retry indefinitely when its job was outside page one. Known-attempt events now
use the verified direct job plus matching attempt response as a partial snapshot.

Test-first evidence includes absent-reader/resolver/factory failures, missing
native-run persistence, missing PR locator/upgrade handling, closed-parent
projection, expired or revoked credentials during issuance, and the integration
and review failures above. Three-module composition tests verify that existing
proofs permit manual fact reads without permission refresh, budget resolution or model
jobs; CI success persistence remains separate from failure classification.

Verification: the previous 24-file 463-test command plus
`tests/test_github_pr_reader_contracts.py`, `tests/test_github_ci_reader_contracts.py`,
`tests/test_github_credentials_contracts.py`, `tests/test_ci_run_persistence_contracts.py`
and `tests/test_pr_delivery_contracts.py`, using `D:/Python313/python.exe`,
`TEMP=TMP=F:/Pullwise/.test-tmp/discovery`, and `-q -p no:cacheprovider`.

Final result: **569 passed, 123 subtests passed in 59.08s**, adding 106 tests
over the previous 463-test slice. Changed modules and tests passed `py_compile`;
`git diff --check` passed with the existing LF-to-CRLF warnings. The latest
remote Server CI still reports failure at baseline `0122367` in
[run 34212039198](https://github.com/GoPullwise/pullwise-server/actions/runs/34212039198);
no remote run covers these local changes. This is Python 3.13 fixture/integration
evidence, not Python 3.10, Cloudflare runtime, full historical-suite or live
GitHub/model coverage evidence.

Explicit remaining P3 limits: PR thread IDs/resolved/outdated still require
GraphQL verification; paged historical reviews do not establish the currently
effective formal review, so raw `CHANGES_REQUESTED` is not promoted to a verified
action. The open-PR scan cannot yet compensate for every missed close event.
CI logs are not downloaded, evidence remains unavailable and no symptom or
cross-run recovery is inferred. Job events without attempt identity can still
need normal scheduled scanning beyond the bounded first membership page.
Collection-by-collection PR and attempt/job CI scanning adds latency at the
15-minute cadence; it is not a complete snapshot per invocation.

Account callbacks are implemented and explicitly composable, but default app
startup has not been given production credential bindings. Existing OAuth
identity storage does not persist expiry/refresh-token lifecycle data. Full
credential-scope rate coordination and conditional requests, source-to-Item
rule projection, P5a Web, and Cloudflare CF1/CF2 remain unfinished. No dependency
installation, model call, real credential use, deployment or remote publication
was performed.

## P3 rule Items, evidence adapters and REST handling loop (2026-09-22)

The Server now composes source persistence with deterministic Item publication.
This advances the P5a product loop rather than adding only ingestion scaffolding:
an actual fact-sync fixture produces a CI Item visible through the shared REST
API, PATCH marks it done, and subsequent list/overview reads remove it from
action views without a model call or processing charge. Web UI migration is
still pending; no browser or Cloudflare completion claim is implied.

- `product_projection.project_rule_source` maps explicit CI failure/timed_out,
  current review-request users/teams, and explicitly verified effective formal
  Request changes into stable rule units. Empty bodies do not erase a verified
  formal request. Unknown historical reviews and unclassified Updates do not
  create invented Items. Each projection carries an action signature and its
  rule-evidence completeness; it does no network/store/model work.
- `product_rule_items.publish_rule_items` runs inside the fact transaction,
  reuses context/unit identity, publishes through existing source/context fences,
  and retains distinct requested actors. Unchanged polls retain ItemVersion and
  attention time. Adjacent complete, materially unchanged actions can carry
  handling through an explicit system audit event. Withdrawal and A-to-B-to-A
  requests reopen appropriately; changed evidence cannot silently inherit done.
  Valid identical semantic snapshots are revalidated and retained; changed or
  uncertain semantic material retains the existing Item pending confirmation
  with expired historical evidence and no false current assessment.
- `reconcile_pr_items` handles explicit parent closure after each fact page in
  the same transaction. It retains child facts, adds parent authority to Item
  dependencies and fences all participating contexts. Late old-open child facts
  do not reopen a known closed PR or churn versions on repeated polls. Explicit
  parent reopening only reopens Items that parent had closed, with a new version
  and pending confirmation, never by restoring an old done record. Other PRs and
  inaccessible/expired parent evidence are not guessed from.
- REST read projection now applies current-version done/dismissed as handling
  closures while preserving native GitHub lifecycle. It does not reuse stale
  handling across versions. `mine` uses GitHub IDs, as required by design 03;
  Pullwise billing/account IDs are not interchangeable. Manual assignee takes
  priority, and unverified team membership remains unassigned. lastSyncedAt uses
  the oldest participating source's latest sync time, independent of ItemVersion
  and attentionUpdatedAt.
- `github_pr_threads` provides two fixed read-only GraphQL queries and bounded
  opaque continuation across both thread and comment pages. It validates stable
  repository/PR identity, comment database identity, reply association and cycles.
  Optional `GitHubPRReader.thread_reader` enrichment joins only actual matching
  comments; partial or missing pages remain unknown and never imply deletion.
- `github_ci_logs` provides an injected single-job log boundary: GitHub-only
  initial authorization, restricted credential-free download redirects, 5 MiB
  streaming bound, sanitized failures and known-pattern local redaction. It
  selects at most one complete-line 20 KiB tail window with stable line numbers,
  versioned selection and unknown stage/step. It does not infer symptoms.
  Optional `GitHubCIReader.log_reader` preserves failure facts on unavailable
  evidence and propagates rate limits. Log-enabled mode uses one job per page;
  the composition factory accepts optional CI logs and PR thread dependencies.

Test-first evidence covers the absent modules; wrong handling closure/view-ID
semantics; actual fact-sync to REST Item creation; source freshness without
version churn; same-input semantic retention; expired historical evidence;
request withdrawal/carry/ABA; explicit parent close/reopen; cross-thread reply
cycles; secret redaction and line numbers; credential-free redirects; and optional
reader/factory wiring. Independent review reported no additional confirmed P0/P1
and requested the late-child-after-parent-close scenario, which was covered by
both helper and fact-sync integration tests.

Final verification used the previous 29-file command plus
`tests/test_product_projection_contracts.py`, `tests/test_product_rule_items_contracts.py`,
`tests/test_github_ci_logs_contracts.py` and `tests/test_github_pr_threads_contracts.py`:
**706 passed, 123 subtests passed in 56.31s**, adding 137 tests. Interpreter:
`D:/Python313/python.exe`; `TEMP=TMP=F:/Pullwise/.test-tmp/discovery`;
`-q -p no:cacheprovider`. All changed Python modules/tests passed `py_compile`;
`git diff --check` passed with the existing LF-to-CRLF warnings. Latest remote
Server CI remains [run 34212039198](https://github.com/GoPullwise/pullwise-server/actions/runs/34212039198)
on old `0122367`, failed; it does not cover this working tree.

Explicit remaining boundaries: GraphQL has an injected query callback, and its
continuation is not yet integrated into the REST scan's persistent cursor;
enrichment only covers the returned matching comments. Effective formal-review
history verification, thread-level semantic aggregation and missed-close
discovery still need completion. Log transport has no default network executor;
cooperative deadline checks cannot interrupt a blocked callback, so public-peer
DNS/TLS enforcement and actual hard total exit remain platform gates. Known
pattern redaction is not a promise of complete secret removal. Enabling one-job
log pages changes cursor scope; live rollout needs an explicit safe checkpoint
transition without resetting backfill/usage. Assignee membership validation and
team resolution, credential lifecycle/rate coordination, model gates, P5a Web
and Cloudflare CF1/CF2 remain unfinished. No new dependency, real credential,
Jev call, deployment or remote publication was used.

## Explicit no-go gates

- Cloudflare production ingestion: **NO-GO** until the selected runtime,
  persistent transaction authority, credentials and scheduled execution have
  been validated. This does not block P3 injected adapters or P5a product work.

- Jev production calls: **NO-GO**. No authorized key, no fixed SDK install/import
  verification, no slow-stream/oversize-body bounded-exit proof, and no real
  PR/CI/Updates quality evaluation.
- General CI cross-run recovery: **NO-GO**. No actual job/matrix identity and
  lineage coverage study has been completed; unsupported relations must remain
  `unknown`.
- Updates automated quality claim: **NO-GO**. The joint-answer and partial-
  coverage rules are implemented only as deterministic skeletons; no real model
  precision/recall/coverage gate has passed.
- P5b matrices/full timeline: **DEFERRED** until paired usability testing proves
  a location-time benefit over P5a lists and labels.

## Next implementation slice

1. Complete the blocked P0 visual baselines when browser tooling is authorized.
2. Finish RepositoryService and watch mutation/idempotency endpoints, source
   assessment persistence, and the scheduled eligibility/cooldown state machine.
3. Finish P3 coverage after the persisted GraphQL continuation slice:
   effective-review verification, missed-close discovery, thread-level
   semantic projection, hard-bounded live log transport, credential lifecycle/
   rate coordination and actual field/permission coverage. Advance Cloudflare
   CF1/CF2 alongside P5a, using appendix 08; keep Jev production disabled.
4. Complete P5a source-assessment/release-label persistence and live Server/Web
   integration on top of the product-v1 Dashboard delivered below.
5. Remove the entire old scan/finding/fix/Reviewer/Worker/Gateway/Agent-first
   dependency closure and its tests/config/routes. Old physical scan tables may
   exist only for settlement/backup dry-run and must not be read by the new
   product; no compatibility adapter or dual runtime is an accepted endpoint.
6. After Server/Web no longer depend on them, remove local Admin and Worker
   directories using the recorded recovery commits. Remote deletion remains a
   manual user action.

## P5a Web, persisted thread continuation and local CF1 probe (2026-09-23)

Dashboard now consumes the shared product-v1 list/overview/detail/handling
contract. PR, CI and Updates share module/scope/view/attention controls, distinct
Server counts, evidence drawers, optional-note handling, GitHub-ID assignment
and classification feedback. Updates additionally shows all discovered sources
by watch context, including sources without Items; it displays partial coverage
without deriving negative labels. Actual saved release relevance/signal
projection still requires source-assessment persistence; it is not invented in
the browser. P5b matrices/full timelines remain deferred.

The new Web client retains nested product errors and uses If-Match plus the
displayed itemVersion. 409/412 disables mutation until an explicit reload;
no write is automatically replayed. A synchronous lock prevents duplicate
submissions. Read keys isolate filters/pages, stale requests abort, old results
are discarded, failed counts never become zero, and access loss clears rows
and evidence. The modal traps focus, Escape restores the opener, and expired
evidence text/unsafe source URLs are not displayed. Dashboard no longer issues
old scan/finding requests or opens the old issue search. Existing non-Dashboard
routes are not claimed to be fully migrated.

Server details now return saved Source content and append-only Item
handlingHistory. Lists omit these heavy fields. Authority is checked before
return, and Item/current handling/history share one database read snapshot.
Both additions are documented in product-v1 OpenAPI and tested through session
and API-key routes.

Thread-enabled scheduled PR reads now persist the nested GraphQL cursor plus
bounded matched-comment IDs in discovery checkpoints. They drain that cursor
before advancing the REST inline-comment page and avoid overwriting earlier
verified matches with unknown fields. Tests rebuild the reader, scheduler and
ProductStore between ticks and prove continuation survives restart without model
work or usage. Scope changes reject old REST-only cursors. This is still
incremental: GraphQL may be revisited per REST page; a completed traversal is not
an atomic snapshot or deletion proof. Direct-event enrichment remains bounded
to one page. Effective formal reviews, missed-close discovery and semantic
thread aggregation remain open.

With explicit user approval, dependencies were installed only for
cloudflare/probe; Server/Web main manifests and locks remain unchanged. Local
workerd (Python 3.14.2) imported typesafe-sdk 0.7.0. The local D1 probe passed
SQL-error rollback, zero-row guard rollback, two-owner/global-slot contention,
scheduled writes and persistence after an actual runtime stop/restart.
See cloudflare/probe/README.md for pinned tools, commands and limitations.
No full CF1/CF2 or remote-platform validation claim is made.

The SDK CPython loopback probe observed a successful synthetic normal response,
a timeout for slow headers, and no total exit within 2s for a continuously slow
body despite a 100ms timeout. The parent killed/reaped the test child. This is
not Workers network validation and does not pass the hard-exit gate. No real
GitHub/Jev credential or model service was used; production Jev stays disabled.

Verification:

- Test-first failures: missing Web product client/old Dashboard; absent detail
  history/content; missing nested persistent thread continuation.
- Web npm run check: **47 files, 612 tests passed**, lint and Vite build passed.
  Worker and worker-entry syntax checks passed.
- Server current-target selection: **536 passed, 95 subtests passed** (product*
  suites, github*contracts suites, CI persistence/triage, PR delivery, shared
  watch authorization, fixed SDK, Updates, question and legacy-inventory suites).
  This is a selected regression, not the prior 706-test command.
- Final directly affected seven-suite regression: **157 passed, 5 subtests
  passed**. Changed Python modules/tests and isolated probe scripts compiled.
- Browser fixture checks: 1440px light, dark detail, 390px list/Updates/detail;
  document scrollWidth/clientWidth both 390px; Escape/focus return; zero browser
  console errors. Screenshots are under Web output/playwright. This was a local
  fixture, not authenticated live Server/Cloudflare end-to-end traffic.
- git diff --check passed (existing line-ending warnings only).

Remote checks were reviewed again, not assumed from the previous baseline.
Server run 35808171823 on 2eeccb6 fails while checking out the retired Worker,
before tests run. Web's workflow-run API returned an empty list, so no remote
passing result is claimed. Local changes remain unpublished.

Read-only Cloudflare inspection confirmed the existing pullwise-web deployment
on pull-wise.com and www.pull-wise.com (2026-09-08 version
bc3fa061-c928-4314-bd47-6caa8f0416f9 at 100%) and matching API origin
https://api.pull-wise.com. No pullwise-server Worker appeared. No deployment,
DNS, secret, payment configuration or production activation was changed.

## Source assessment persistence and domain D1 probe (2026-09-23)

Continued from the dirty two-project workspace; all earlier changes remain.
`publish_assessment_result` now supports source-only results with no Item,
keeping the public assessment/evidence, frozen coverage and dependency fences
in `source_assessment_publications`. Cache insertion, optional ItemVersion,
processing consumption and claimed-job completion remain one transaction.
Configuration/auth/owner/analysis changes reject publication. Source detail
checks all dependencies in one read snapshot; changing source/context removes
the current assessment, while disabling analysis alone preserves valid results.
Repeated stale-claim publication cannot consume twice.

Source lists project saved complete updates-filter/v3 question groups with
the provisional 0.8 confidence threshold. Unknown/incomplete groups never
become negative classifications. Release relevance and signals follow the
same-unit joint table: partial negative evidence stays unclear; entirely
irrelevant releases have null signals; conflicting unrelated signals cannot
be borrowed by another relevant unit. No text keyword or Item-label inference
was added. Current raw answers/evidence are detail-only and context-scoped.
Coverage is frozen with the assessment so later fact coverage cannot expand
what the model actually saw. This does not pass the real quality gate.

The shared REST source path now filters relevance/updateSignal in one context
and trims other contexts from restricted API-key results. Shared watches use
their targetRepositoryId, not upstream identity, for repository restrictions.
Web renders these saved source labels and the clicked context's assessments,
including releases without Items. OpenAPI and both project AGENTS were updated.

The new optional Web contract test invokes `tests/export_source_contract.py`
against a fresh real SQLite store and shared product REST handler, verifies
Cookie/API-key DTO equality and zero GET usage/job changes, then renders the
returned DTO in Web. It is process/DTO integration, not HTTP/proxy/browser
end-to-end, real GitHub/Jev, or Cloudflare validation. Set
`PULLWISE_CONTRACT_PYTHON=D:/Python313/python.exe` for the local check; Web-only
CI explicitly skips this sibling-project test when the variable is absent.

Cloudflare: extended the existing isolated local probe with concurrent claims,
lease expiry/successor fencing, config/auth cancellation, retry deadlines,
atomic result/usage/job publication, final-guard rollback, and response replay.
`verify_domain.py` passed on local workerd; an actual stop/restart followed by
`verify_domain.py --after-restart` preserved the retry deadline. Deterministic
clock and synthetic single-job SQL protocol only; not ProductStore adaptation.
Discarding a completed response tests replay but does not inject network loss.
The existing `verify_local.py` import/budget/rollback/scheduled checks also passed.
No installation, dependency upgrade, deployment, credential or payment change.

Test-first evidence: source-only publication rejected None item_id; missing
release projection import/fields; context restrictions leaked sibling contexts;
Web lacked source labels; CF domain routes returned 404; missing final-reservation
fault injection. Each targeted failure was rerun after implementation.

Verification (existing Python 3.13; TEMP/TMP=F:/Pullwise/.test-tmp/discovery):

- Direct Server seven-suite regression: 98 passed. Final source persistence
  suite after one additional invalidation test: 11 passed.
- Broader selected regression: 531 passed, 69 subtests passed. Selection was
  `test_product*.py`, `test_source*.py`, `test_saved_updates_projection.py`,
  `test_update_filter_contracts.py`, `test_github*contracts.py`,
  `test_pr_delivery_contracts.py`, `test_shared_watch_authorization.py`,
  `test_ci*contracts.py`, `test_jev*contracts.py` as matched by rg --files.
  This differs from prior 536/706 commands and is not a full legacy suite.
- Web npm run check with the cross-project test enabled: 48 files, 614 tests
  passed; lint and build passed. After adding API-key parity to the exporter,
  its cross-project test separately passed again.
- Changed Python files compiled; git diff --check passed (line-ending notices).
- Remote status rechecked: Server 35808171823 still failed at retired Worker
  checkout before tests; Web run list empty. No remote passing CI claim.
- No new real-browser visual pass in this continuation; prior screenshots
  remain the earlier fixture baseline, not evidence for these new labels.
- Both Web hosting entry files passed node --check. Local workerd was stopped;
  port 8794 had no remaining listener at the end of this increment.

Remaining: PR effective-review verification, missed-close discovery and thread
semantic aggregation; full source-result production orchestration and broader
HTTP/proxy integration; D1 monthly/rolling budgets and full multi-source/account/
Creem mapping; fixed SDK target-runtime bounded network exit. Jev production
remains disabled. P5b and production operations remain outside this increment.

## Known PR close reconciliation, formal-review proof, and local CF bounds (2026-09-23 continuation)

Scheduled PR discovery now refetches one persisted open parent by number before
the normal open-list page. `pr_parent_checks` rotates candidates across restarts;
its timestamp advances only with the permission/config/generation-fenced fact
write. A real SQLite/scheduler rebuild fixture closes an existing empty-body
Request changes Item after a missed PR-close webhook, without model work. The
manual fact-sync path neither queries nor advances this checkpoint. GitHub
list omission still proves nothing; unseen closed PRs and an atomic full
snapshot remain outside this bounded compensation.
The reader scope changed; older saved PR discovery cursors require an explicit
transition before live rollout. This run did not silently reset checkpoints.

An injected `GitHubPRReviewReader` checks GitHub GraphQL
`latestOpinionatedReviews` against REST review/reviewer IDs, repository and PR
identity. A matching current CHANGES_REQUESTED confirms its formal rule;
later opinionated review marks the older one superseded. Missing reviewer,
partial page, uncertain transport or changed parent during the REST/GraphQL
reads cannot confirm an action. A directly refetched DISMISSED review is
conclusive for its own formal state. Empty-body formal Items close under a
verified supersession/dismissal; nonempty text remains pending because its
request meaning is independent. ItemVersion stays monotonic. The GraphQL
reader uses an injected read-only callback. When the already injected PR
thread reader exposes that callback, normal composition enables this proof;
no live GitHub credential or production reader was activated. GitHub's
[GraphQL PullRequest fields](https://docs.github.com/en/graphql/reference/pulls)
and [REST review list](https://docs.github.com/en/rest/pulls/reviews) were
checked for the field and list semantics. Thread-level semantic aggregation
remains unimplemented.

The isolated local Python Worker/D1 probe now also tests owner/global monthly
and 60-second rolling attempt budgets with one D1 batch and a real UTC month
boundary. A denied attempt rolls both scopes and the event back. A real
workerd stop/restart kept prior-month attempts in the rolling window. It is
still separate from the job lease/claim batch, not a Server store adapter.
The domain probe additionally sent a request then closed the local client
socket before its response; retry settled exactly one assessment and usage
unit. This does not simulate losing an upstream Jev response.

The fixed SDK 0.7.0 ran inside local Python workerd against a fake local
provider. A baseline plain transport took 5.173 s for bytes arriving every
25 ms despite a 100 ms timeout. The probe's injected public httpx2
`BaseTransport` bounds decoded body size at 1 MiB and checks a synthetic
2-second total deadline. Normal response succeeded; slow headers timed out;
continuous slow body exited at ~2 seconds and the local peer observed the
disconnect; a 2 MiB body was rejected at ~0.1 seconds. The same Worker
responded normally after each failure. This is a local mechanism test only:
90-second production setup, real provider behavior, model quality, remote
Cloudflare runtime and Server/Creem integration have not passed. Jev production
remains disabled.

Test-first failures included missing `FactPage.reconciled_source_id`, absent
durable rotation, missing GraphQL review reader and formal-rule withdrawal,
404 budget/SDK probe routes, and the plain Worker slow stream exceeding the
test deadline. After implementation, PR/review/rule targeted suites passed,
then a broader Server selection reported **551 passed, 69 subtests passed**.
The selection used `test_product*.py`, `test_source*.py`,
`test_saved_updates_projection.py`, `test_update_filter_contracts.py`,
`test_github*contracts.py`, `test_github_pr_review_verification.py`,
`test_pr_delivery_contracts.py`, `test_shared_watch_authorization.py`,
`test_ci*contracts.py`, and `test_jev*contracts.py`. This is not a full legacy
suite. Web code did not change in this continuation; its previous `npm run
check` result remains 48 files/614 tests with lint/build passed. All work is
local and unpublished.

## PR thread semantics, real Web/HTTP contract and Server-table D1 mapping (2026-09-23)

Continued in both dirty repositories and preserved the pre-existing work. The
new `pr_followup.reconcile_thread` publishes one stable `pr_thread` Item per
verified thread/context from saved per-comment pr-followup/v3 answers. Multiple
labels and source/assessment references are retained; processing remains per
source. Result publication invokes aggregation inside the same transaction as
assessment persistence, usage consumption and claimed-job completion. Fact
sync invokes the same projection without scheduling models or consuming usage.

Replies require a verified same-thread parent and a frozen parent dependency.
Declaring a dependency without its publication fence is rejected atomically.
Parent edits invalidate dependent answers even when the child body is unchanged.
The inline reader now carries the authoritative PR author and complete observed
GraphQL comment roster; missing bodies/roster, partial traversal, unknown answers
and lost thread proof remain pending. These changes retain the existing bounded
thread continuation and do not claim an atomic GitHub snapshot.

Complete unchanged action evidence may inherit only adjacent current handling,
with a system `handling_carried` audit. No-action progress does not advance
attention time; new action material and A→B→A cannot revive historical done.
Uncertain/incomplete transitions deliberately do not inherit completion. PR
closure and thread resolution remain GitHub facts; comment deletion removes
only its own contribution and an entirely deleted thread keeps source_deleted.
Multiple unresolved roles remain visible rather than inferring a handoff from
completion language. Pure claims/thanks or unknown thread identity create no
new Item. The candidate 0.8 threshold remains unvalidated by real model data.

Completion claims are model-derived `evidence.progressType=completion_claim`,
not GitHub facts or action labels. Web displays a neutral explicit distinction
from verified completion and does not close/reassign on a claim. Existing layout
and styling remain intact. OpenAPI documents this optional evidence field and
the existing Item title/facts/actors/timestamps which were missing despite
`additionalProperties:false`; a real generated Item now validates locally.

`product-http-contract.test.jsx` starts a fresh loopback CPython HTTP server via
`tests/serve_product_contract.py`. It runs the actual Web client and existing
Worker proxy function, renders the actual Item DTO, handles it, checks stale
revision rejection, compares Cookie/API-key GETs, verifies SameSite=None Origin
rejection, reads a no-Item release and submits manual sync. Successful usage is
unchanged and no analyze_source jobs appear. It uses a synthetic cookie jar/key
and Node/React test environment: not real-browser or Cloudflare HTTP execution.

That HTTP test exposed a real defect: `product_api._header` rejected stdlib
HTTPMessage because it only accepted Mapping, losing If-Match and idempotency
headers. The minimal HTTPMessage test failed first and now passes. The existing
source exporter test exceeded Vitest's default 5 seconds during concurrent full
regression; its test deadline is now 30 seconds, while the subprocess still has
its own 20-second cap. Both cross-project tests run when
PULLWISE_CONTRACT_PYTHON is set and explicitly skip in a Web-only environment.

The D1 work is an executable mapping, not a Server migration. See
`docs/cloudflare-domain-transaction-map.md`. A generated synthetic fixture uses
actual ProductStore tables and column definitions, then
`cloudflare/probe/src/server_mapping.py` executes finite claim/admission and
publication batches. Local workerd/D1 passed competing claims, combined two-scope
attempt admission, secondary-source and persisted-account CAS, final-reservation
rollback, atomic assessment/ItemVersion/usage/job writes, preserved synthetic
billingEvents and replay rejection. An actual stop/restart using
`.wrangler/server-map-state` retained one attempt/result/used unit. Probe ran on
loopback port 8796 and was stopped afterward.

The account guard compares a frozen persisted users entry; it does not implement
monotonic entitlement revisions, paid-period expiry or Creem event writes.
No payment/credential configuration changed. Existing account encryption,
OAuth/session, Creem signature/dedup/late/pending behavior must still be adapted
and validated in the target runtime. The mapped publication is a first-result,
one-Item case: cached/source-only publication, full thread membership CAS and
handling, revocation/retry release, reads and scheduler remain CF2 work. No remote
Cloudflare validation, real GitHub/Jev calls, deployment or production migration
was performed. Production Jev remains disabled.

Test-first evidence included zero Items for two classified thread comments,
incorrect handling reset after harmless progress, stale visibility after lost
thread proof, absent pullAuthor/roster, unfenced parent dependency acceptance,
wrong entire-deletion closure reason, missing real HTTP headers, completion
claims rendered under GitHub facts, missing Item schema fields, and absent D1
mapping commands. Each targeted behavior was rerun after implementation.

Local verification used D:/Python313/python.exe and
TEMP=TMP=F:/Pullwise/.test-tmp/discovery:

- Broader selected Server regression: **578 passed, 69 subtests**. Selection:
  `rg --files tests -g 'test_product*.py' -g 'test_source*.py'
  -g 'test_saved_updates_projection.py' -g 'test_update_filter_contracts.py'
  -g 'test_github*contracts.py' -g 'test_github_pr_review_verification.py'
  -g 'test_pr*.py' -g 'test_shared_watch_authorization.py'
  -g 'test_ci*contracts.py' -g 'test_jev*contracts.py'
  -g 'test_cloudflare_server_mapping.py'`, passed to `python -m pytest ... -q`.
  This is not the full legacy suite.
- After the final deletion-reason regression and OpenAPI additions,
  `python -m pytest tests/test_pr_thread_semantics.py
  tests/test_product_openapi_contract.py -q`: **24 passed** (18 thread tests).
- Separate `tests/test_pr_thread_semantics.py tests/test_billing_contracts.py
  tests/test_billing_routes.py tests/test_billing_webhooks.py
  tests/test_cookie_contracts.py`: **128 passed, 15 subtests** at that checkpoint.
  Payment code was unchanged afterward; this overlaps the thread selection.
- `tests/validate_thread_contract.py`: actual thread Item conforms to OpenAPI,
  using existing local PyYAML/jsonschema. It is an optional local check, not a
  newly installed dependency or a claim about remote CI dependencies.
- Web `npm run check` with PULLWISE_CONTRACT_PYTHON=D:/Python313/python.exe:
  **49 files / 615 tests passed**, lint and build passed, including both sibling
  contract tests. No new real-browser visual pass was performed.
- `verify_server_mapping.py` and `--after-restart`: passed on local workerd/D1;
  eight actual-schema SQLite mapping tests also passed. Remote platform gates
  and real model quality remain unpassed.
- Remote CI rechecked through gh: Server run 35808171823 remains failed at
  “Check out the Worker used by Gateway integration”; tests were skipped.
  Web Actions list is empty. No passing remote CI claim or old Worker restoration.

Next boundary: turn the proven finite persistence commands into an async Server
adapter with account/entitlement revision and Creem protection, then run the same
REST/proxy cases on local Workers. Full source-result production orchestration,
real provider quality/bounded-exit and live-ingestion readiness remain separate
gates; P5b remains deferred.

## D1 due-job wake continuation (2026-09-23)

`D1AnalysisTransactions.claim_due_analysis` selects one due eligible Job from
persisted D1 state using owner fairness and insertion order, then claims it
through the existing guarded transaction. A local probe flag lets the Python
Worker's actual `scheduled` handler exercise the path without calling Jev.
The first wake created one attempt, a second wake created none, and state
survived an actual process restart. A local test verifies retry_wait is not
eligible before its persisted deadline. This is bounded selection for an
eligible Job only: stale queued jobs are skipped, not yet terminally released;
no real Server cron composition, provider execution or HTTP runtime is wired.
Targeted mapping, adapter and billing-webhook verification after this change:
**54 passed, 5 subtests**. Local workerd/D1 scheduled wake and actual process
restart replay passed with the owned process stopped afterward.

## D1 signed receipt and first enqueue continuation (2026-09-23)

The local Worker now reads raw HTTP request bytes and a signature header for
a synthetic Creem event. Server `billing.py` and Worker code share the same
pure HMAC verifier. An accepted normalized update is stored as a D1 receipt
before ACK; exact duplicates retain one record, invalid signatures and
same-ID changed bodies reject. Applying a pending receipt atomically writes
the trusted account/event/pending state, increments the owner revision and
marks the receipt applied. Tests cover rollback on missing receipt and replay.
The real Creem mapper/handler, production secret binding, payment lifecycle,
encrypted account codec and receipt retention are still not connected.

Added a first-generation trusted analysis enqueue command. It validates
source/context, current permission/entitlement and reservation, then freezes
revisions in a queued job. Owner/global active caps are computed inside the
same D1 batch. Cap denial creates no job and atomically releases the new
reservation and marks the context throttled. Local SQLite and workerd/D1
admission/denial plus restart replay passed. It does not handle existing
logical-key generations, supersession or stale-admission release; the real
scheduler remains unwired. The later due-job section above covers only one
eligible candidate wake.

Targeted Server verification for D1 mapping/adapters and billing contracts,
routes and webhooks reported **145 passed, 15 subtests**. Server remote CI still
shows run 35824307016 failing at the retired Gateway Worker checkout before
tests; this local commit is not pushed. Web remains unchanged with only its
untracked `output/` screenshots.

## D1 owner-cycle budget, reservation and failure continuation (2026-09-23)

Added `period_start` to the local entitlement authority. The owner monthly
provider-attempt budget now spans the actual billing cycle, including attempts
from the preceding UTC month, while the global monthly budget stays UTC.
The old count admitted a fixed Jan-to-Feb counterexample; the corrected test
rejects it without partial claim or attempt writes.

Added an async `D1AnalysisTransactions` adapter that reads persisted owner
snapshot/revision/limit before a first reservation or claim, and the frozen
claim revision before first-result publication. A first reservation upserts
the current bucket limit without resetting usage and atomically inserts its
ledger entry; duplicate first-charge attempts, quota exhaustion, dirty
projection and stale snapshots reject as a whole. Retryable job failure
persists a deadline and retains reservation/attempt spend. Terminal failure
or attempt 3 releases the reservation and marks job/context failed in one
batch; expired tokens cannot release newer work.

The same Server modules ran in local workerd/D1 and after an actual process
restart under the new ignored `.wrangler/server-map-cycle-state` directory.
Fresh local Python Worker startup exceeded the old 20-second probe timeout,
so the probe driver now allows 90 seconds; warm requests stayed fast. The
process was stopped. This is not remote Cloudflare validation, a full async
ProductStore, or CF2. Real Creem, scheduling, REST, source-only/cache replay,
charge-key generation and revocation/cancellation remain open.

Charge-key continuation: active reserved/consumed keys now receive a guarded
read confirmation and return their original reservation; a released key can
reserve again with a new ID for the same owner/module, updating the current
bucket limit without clearing counts. Unit tests covered duplicate calls and
owner/module conflict; the local workerd driver covered active reuse,
terminal release, reopened reservation and replay. One workerd restart
attempt exited with a local runtime disconnected error before readiness;
restarting again against the same persisted directory passed
`verify_server_mapping.py --after-restart`. This is a local runtime anomaly,
not a remote CI result or a CF2 pass.

After this slice, the broader selected Server regression reported **764 passed,
84 subtests** with Python 3.13 and workspace TEMP/TMP. Server CI remained at
run 35824307016, failed before tests while checking out the retired Gateway
Worker; Web Actions remained empty. No push or remote validation occurred.

## Async D1 account boundary continuation (2026-09-23)

Moved finite mapping commands into Server `cloudflare_d1_mapping.py` and
introduced `cloudflare_d1_batch.py` plus `cloudflare_account_adapter.py`.
The adapter reads persisted account/event/pending snapshots through D1, then
submits each guarded write as one prepared async batch; concurrent changes
between read and batch reject the entire write. Added a non-billing account
write that increments the owner revision and dirties the projection, and a
pending-event association batch that atomically changes the affected user,
billingEvents, billingPendingUpdates and revision. It consumes trusted output
from the existing handler; it does not reinterpret Creem events.

Extracted the existing monthly cycle and product entitlement rules into pure
Server modules re-exported by `quota.py` and `entitlements.py`. The generated
local Python Worker package uses the same source for live entitlement refresh,
without a fixture-provided plan/period/limit/expiry or a second tariff table.
The synthetic fixture now passes through `state_for_storage`, encrypting two
synthetic GitHub token fields with a temporary synthetic key. The new account
adapter paths, pending association, claim/publication and restart replay passed
on local workerd/D1. The workerd process was stopped.

Test-first evidence includes failed tests for account identity swapping,
missing non-billing writer, missing async batch/adapter and pending commands;
the live Worker refresh initially failed before pure-rule packaging. A broad
selected Server regression after the pure-rule extraction passed **708 tests,
84 subtests**. Follow-up encrypted-fixture mapping tests passed **17 tests**.
No remote Server runtime, real Creem handler, encryption-key access on Workers,
all account writers, full ProductStore, scheduler or Server REST adapter is
connected. This remains below CF2; production Jev and live GitHub stay off.

## Persisted-account entitlement projection (2026-09-23 continuation)

The local mapping's initial and refresh commands now derive plan, processing
limit, anchored monthly period and strict validity from the frozen persisted
users entry with Server `entitlements_for_user`/`quota_cycle_for_user`. The
caller no longer passes those fields. The initial batch checks that its
snapshot still matches app_state.users. `export_d1_server_fixture.py` exports
the Server-calculated refresh batch for the isolated Worker; no tariff rules
were copied into the probe. The processing reservation uses that account
period and limit instead of synthetic `"period"` and 100.

Test first: the changed fixture failed with the old `initialize_account`
signature, and a missing-persisted-account test failed before the guard was
added. Final targeted tests: 15 passed, including product entitlements.
Local workerd/D1 `verify_server_mapping.py` and `--after-restart` passed after
regenerating the synthetic fixture and actually stopping/restarting Wrangler;
the process was stopped. These are local tests, not remote validation.

The actual webhook mutates in-memory USERS, BILLING_EVENTS and pending
updates under STATE_LOCK, then `persist_state` flushes them through
`db.save_state`/`state_for_storage`. The probe does not execute that handler,
its encryption, pending/late association or all account writers. An async
D1 adapter still needs durable accepted-event handling, complete writer
revision coverage, reservation bucket limit updates on upgrade and the
remaining ProductStore transaction, REST and scheduled paths. CF2,
production Jev and live GitHub remain gated.

CI recheck: Server run 35824307016 at 300bffe failed while checking out the
retired Gateway Worker; tests were skipped. Web Actions list was empty. No
remote passing check is claimed.

## D1 account revision and accepted-event boundary (2026-09-23 continuation)

The previous product and probe work was committed in the two independent
repositories as Server `1b7a65d` and Web `02c45ed`; the Web `output/` screenshots
remain untracked. This continuation changes only the Server's local D1 mapping,
tests and documentation. No changes were pushed or deployed.

The finite mapping now includes `account_entitlement_authority` and
`d1_claim_authority`. An already accepted synthetic billing event updates the
matching `app_state.users` entry and `billingEvents` record while incrementing
the owner's revision and marking its entitlement projection dirty, in one D1
batch. A trusted projection command checks the persisted user and revision,
then records the period, limit and strict validUntil. Claim checks that authority
and records the revision; publication checks it again. The mapping rejects
duplicate events without partial writes, a stale/dirty account, expiry at the
boundary and old claims after A→B→A. It preserves the original payment event.

Test-first evidence: new tests initially failed because the event/reprojection
commands were absent and expired accounts could still publish. After the
mapping change, `D:/Python313/python.exe -m pytest
tests/test_cloudflare_server_mapping.py -q` reported **10 passed** with
TEMP/TMP=F:/Pullwise/.test-tmp/discovery. Generated a fresh synthetic fixture,
ran `verify_server_mapping.py` against local workerd/D1 on 127.0.0.1:8796, then
stopped/restarted Wrangler using `.wrangler/server-map-state` and passed
`verify_server_mapping.py --after-restart`. The owned workerd process was
stopped afterward. These are local platform-simulation results, not a remote
Cloudflare deployment or real Creem/SDK test.

The actual Creem handler, encrypted account persistence, pending/late event
semantics, current effective-plan calculation, every account writer and Server
REST runtime remain to be adapted. A single bypassing account writer would
invalidate this revision guarantee, so production Jev and live GitHub ingestion
remain disabled. The mapped publication still covers one first-result Item;
source-only/cache replay, full thread projection and release/retry paths remain
CF2 work. No credentials, payment settings or provider transactions changed.

## D1 stale due-job termination (2026-09-23 continuation)

The async due scan now examines at most 16 owner-fair candidates. A due Job
whose source, context, authorization or reservation binding is invalid is
terminally marked superseded, cancelled or blocked; any reserved processing
unit is released in the same D1 batch. An unexpired running lease is not
cleaned up. A dirty account projection remains pending refresh rather than
being treated as a revoked source. The guarded batch rechecks the invalidity,
due state and bucket balance, so an inconsistent bucket rolls back the Job
change. The scan can then claim an eligible candidate without a model call.

Test first: two async tests failed while stale Jobs remained queued/running.
After implementation, `tests/test_cloudflare_analysis_adapter.py` reported
**11 passed**, and combined with actual-schema mapping tests **39 passed**
under Python 3.13 with workspace TEMP/TMP. The generated synthetic package
ran through local Wrangler 4.136.3/workerd and D1 at 127.0.0.1:8796 using
`.wrangler/server-map-cycle-state`; `verify_server_mapping.py` and its
`--after-restart` replay passed after a real stop/restart. The process was
stopped. These are local tests only; no remote Cloudflare runtime, Server
REST, Creem handler, live GitHub or Jev path was exercised.

The scan limit, exhausted attempts and concurrent selector races need further
mapping before calling this a full scheduler.
Remote Server CI still shows run 35824307016 failing before tests at the
retired Worker checkout; Web Actions remain empty. Neither repository was
pushed or deployed in this continuation.

The next local increment handled a clean account projection whose billing
period no longer matches the Job's reserved ledger, or whose strict validity
has expired. The due command now blocks that Job and releases its old-period
reservation atomically; it leaves a dirty projection queued for refresh.
The old-cycle test failed before implementation, then the combined adapter and
actual-schema mapping tests reported **41 passed**. Local workerd/D1
`verify_server_mapping.py` and `--after-restart` passed again with an added
old-cycle scheduled wake under the same local persistence directory. The
process was stopped. Remote Cloudflare, real account/Creem and REST wiring
remain unverified.

Real handler assessment: `_app_part_10_handler_main.py` verifies Creem and
calls `apply_billing_update`, which mutates in-memory account/event/pending
maps. Its response precedes the `route` finally block's `persist_state` call;
that persistence catches errors. `db.state_for_storage` encrypts secret fields
using a local key file. A Workers adapter must durably record an accepted
receipt before ACK, run the existing billing decisions over a revision-fenced
account snapshot, and apply account/event/pending changes in one async D1
batch. The REST router still depends on synchronous global state and SQLite
ProductStore; mapping its authenticated reads/writes and encrypted state is a
separate implementation slice. No product route or payment configuration was
changed here.

An expired third-attempt running Job previously stayed active because the due
selector excluded `attempt=3`. A failing regression covered that restart-like
case. The due scan now terminates it as failed, clears its stale claim and
releases the reservation in one guarded batch without a fourth provider
attempt. The combined async adapter and actual-schema tests reported **42
passed**. The added scheduled local workerd/D1 case passed in
`verify_server_mapping.py`; no remote runtime or model request was involved.

The real wiring audit found two additional effects behind the existing
billing handler: `apply_billing_update_to_user` writes a synchronous quota
bucket, and `product_api._store()` constructs a SQLite ProductStore for every
product-v1 route while some GETs call `db` directly. D1 REST adaptation must
replace those storage seams under the same authenticated DTO contract. The
Creem lifecycle needs an injected durable quota write alongside its account,
event and pending-state decisions; merely copying its in-memory maps into D1
would still leave a split transaction. These are implementation findings, not
completed Cloudflare integration.

## Signed Creem event-to-receipt binding (2026-09-23 continuation)

The local D1 receipt adapter now rejects a normalized eventId that differs
from the ID in the signed raw JSON. A new trusted entry accepts the existing
`billing.billing_update_from_creem_event` as its normalization callback before
writing the receipt; unsupported events create no receipt. The isolated
Worker uses a synthetic normalizer through that same entry and does not
package or duplicate the payment product rules.

The mismatched-ID and missing-entry tests failed first. Server receipt,
account/mapping and existing billing-route verification reported **90 passed,
2 subtests**. The
regenerated local Worker passed `verify_server_mapping.py` against local
workerd/D1 (`remote: false`); the process was stopped. No remote D1 write,
production key, payment configuration or real Creem handler was used.

Cloudflare cost inspection in the connected account found a separate staging
database's repeated whole-backlog UPDATE had dominated Rows Written. Pullwise
has no remote D1 binding yet. For its future D1 runtime, bound scheduled
updates to changed rows and inspect `meta.rows_written` plus per-query
analytics before raising cadence or queue limits. The external staging fix
and deployment are outside this repository and are not Pullwise validation.

## Pure Creem event normalization and no-cron probe (2026-09-23 continuation)

Moved the existing Creem event parser/status/product-ID checks into
`creem_event_rules.py`, a pure module with explicit configured product IDs.
`billing.py` reuses those functions and supplies the existing Server product
configuration, so payment decisions and public callers retain one authority.
The generated local Python Worker packages the same module and passes only
synthetic IDs through the signed receipt path. Tests first failed because the
module did not exist; lifecycle fixtures then compared the pure and existing
Server entries, including paid, canceled, unknown-product and unsupported
events. The expanded billing, webhook, entitlement and D1 mapping regression
passed **144 tests, 15 subtests**.

The probe's `wrangler.jsonc` now has **no cron trigger**. The local
`verify_server_mapping.py` driver passed with explicit localhost scheduled
invocations and the same local `remote: false` D1, then workerd was stopped.
The connected account's ten listed Workers were checked read-only and all
remote schedules were empty, including `pullwise-web`, `pullwise-admin` and
the separately stopped `gamelens-jobs-staging`. No remote schedule was changed
or enabled. Real Creem handler/key binding, account settlement and Server REST
remain unconnected.

## Pure account decision and guarded receipt settlement (2026-09-23 continuation)

Extracted the existing Creem account, subscription-history and stale-event
decision into `billing_account_rules.py`. It takes a stored account snapshot,
normalized update and fixed processedAt; it returns a new account, event
record and quota-refresh instruction without touching globals or SQLite.
The existing handler now applies this same decision and performs its local
quota write. A concurrency test was retargeted to the pure decision seam;
the old/new event ordering remains protected by `STATE_LOCK`.

`D1AccountTransactions.settle_webhook_receipt` reads a pending signed receipt,
stored user JSON, owner revision, event map and pending list. It rejects an
unrelated owner and duplicate event, computes the account decision without
decrypting untouched token fields, and commits user/event/pending snapshots,
owner revision and receipt state in one guarded batch. The receipt's exact
`update_json` is rechecked inside that batch. A changed receipt or account
snapshot rolls back all writes. The local Worker probe now executes this
settlement using a synthetic signed event, not a fabricated next-account
payload. Pending event association and the real HTTP/secret boundary remain.

Entitlement refresh now updates an existing current-period processing bucket
only when its limit changes; used/reserved counts remain intact. The upgrade
test failed on the old 5,000 limit and passes with 25,000. Local workerd/D1
`verify_server_mapping.py` passed with a separate upgrade case, followed by
a real process restart and passing `--after-restart` replay. No cron is
configured, no remote D1 was written, and the owned process was stopped.
The selected billing, entitlement and D1 regression passed **150 tests,
15 subtests**. Server remote CI remains at the retired Worker checkout
failure before tests; this continuation was not pushed or deployed.

## Pending signed receipt association (2026-09-23 continuation)

The local D1 account adapter can now park one unmatched signed receipt into
`billingPendingUpdates` under exact receipt-content and whole-list CAS.
Duplicate parking returns without another D1 write. At the 1,000-entry cap it
rejects rather than silently deleting an older signed payment fact. A trusted
account association can invoke `reconcile_pending_for_owner` with a 1–16
event bound; it selects matching pending receipts by event-created order and
settles each through the existing account/event/receipt batch. A failed or
interrupted run leaves the remaining entries durable for explicit retry;
there is no cron or user-facing reprocess route.

The early-event test failed before the adapter existed, then passed after
implementation. Tests cover replay without duplicate pending entries,
customer association, ordered one-at-a-time resume, and rollback on a
concurrent pending-list write. The no-cron local workerd/D1 driver now sends
a second synthetic signed event before the account has its customer binding,
parks it, links the account and reconciles it. `verify_server_mapping.py`
passed. Selected billing/entitlement/D1 regression: **154 passed, 15
subtests**. Server CI remains at run 35824307016, failing before tests at
the retired Worker checkout; Web Actions remain empty. Real Creem HTTP,
secret binding, every account writer, and automatic
trusted invocation after account association remain unconnected. No remote
D1, cron, GitHub or Jev behavior was enabled.

## Injected Creem webhook composition (2026-09-23 continuation)

Added `cloudflare_creem_handler.accept_signed_creem_webhook` as a trusted
async composition, without registering an HTTP product route. It accepts
raw bytes, signature, secret, configured product IDs, D1 binding and time;
enforces the 64 KiB body bound; uses the existing pure Creem normalizer;
persists the receipt; resolves a unique stored owner or parks an unmatched
event; settles the owner and refreshes the entitlement projection. An
ambiguous owner leaves the receipt pending rather than modifying an arbitrary
account. Exact replay returns duplicate without another account revision.
If projection refresh fails after receipt settlement, the receipt stays
applied and dirty; duplicate delivery finishes refresh without reapplying
payment.

Tests failed first because the composition module did not exist. Expanded
billing/entitlement/D1 regression: **160 passed, 15 subtests**. The isolated
local Python Worker `/server-map/creem-compose` path then accepted a synthetic
signed paid upgrade through the same function, refreshed the existing D1
bucket limit, and rejected duplicate reapplication in `verify_server_mapping.py`.
The probe has no cron, uses `remote: false`, and was stopped. Real Creem
secret binding, checkout/account writer coverage, product HTTP routing,
actual Cloudflare runtime and migration gates remain open. No push or deploy.

## Candidate Server Worker HTTP entry (2026-09-23 continuation)

Added `cloudflare/server` as a separate local-only Python Worker entry with
no cron, public route or remote D1 binding. `src/entry.py` delegates to
Server-owned `cloudflare_http_contract.py`, which implements read-only
`/health` and raw-byte `POST /webhooks/creem`; unported product paths return
404. The webhook keeps the existing `{"received": true}` success body,
rejects invalid signatures or malformed/oversized bodies before D1, hides
internal errors, and returns 503 so provider redelivery can repair a receipt
settled before a projection-refresh failure. No probe/reset route is mounted
in this candidate.

Test first: the HTTP contract module was absent. Unit tests then covered
the ACK shape, read-only health, unported path, missing configuration,
malformed signed JSON and a durable-receipt/retry transition. The candidate
initially failed to start because its own local Python modules lacked the
existing pinned Workers SDK; copying the probe's already installed, ignored
`python_modules` bytes fixed local packaging without installing a dependency.
`sync_server_modules.py --check` confirms the candidate's ignored Server
package matches checked-in source.

The fixture exporter wrote only synthetic account tables to a new local D1
directory using `wrangler d1 execute --local`; no real user DB was opened.
The actual local Worker returned 503 on empty-schema health, then after
seeding returned 200 health, 404 for `/api/v1/items`, 400 for an invalid
signature and 200 for signed acceptance and replay. A read-only local D1
query found receipt `applied`, revision 3, `dirty=0`, and canceled status.
After stopping and restarting workerd against the same directory, the HTTP
driver passed again and revision stayed 3. This is local workerd/D1 evidence,
not remote Cloudflare, full Server REST or production Creem integration.
The selected HTTP, billing, entitlement and D1 regression reported **165
passed, 15 subtests**. `sync_server_modules.py --check` passed. Server remote
CI still shows run 35824307016 failing at the retired Worker checkout before
tests; Web Actions are empty. No push, deployment, cron or remote D1 use.
