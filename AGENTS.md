<!-- PULLWISE_PRODUCT_TARGET_START -->
## Current target — PR / CI / Updates design 1.4

Implement only the PR follow-up, GitHub Actions failure, and upstream Release
filtering product defined by `../docs/design/pr-ci-updates/README.md` and
01–07. The old full-repository Reviewer, Agent-first, Worker fleet, and Model
Gateway rules below are historical cleanup evidence, not implementation
authority. Do not restore their routes, runtime, entitlements, or contracts.

Keep GitHub access read-only. Web and external clients share product-v1 REST;
GET and manual fact sync never schedule model work. Production Jev remains
disabled until the P0.5 quality and bounded-transport gates pass. Preserve
account/session/GitHub authorization, Creem transaction facts, payment history,
and their security tests while replacing old scan entitlements.

Use stable `watchScopeKey` for domain identity and charging, `contextHash` for
semantic cache reuse, and monotonic `contextVersion` plus `ItemVersion` for
audit. A real A→B→A transition never reuses an earlier ItemVersion. Persist
multi-source dependencies and fence publication on every source/config/auth
revision; pending or incomplete assessment must not silently close an item.
<!-- PULLWISE_PRODUCT_TARGET_END -->

## Current product-v1 implementation invariants

- The workspace Cloudflare D1 cost pause applies here: do not run Wrangler,
  workerd or D1 commands, even local probes, until the user explicitly
  authorizes resuming. Use Python/SQLite and static checks meanwhile; keep
  remote CF2/CF3 validation pending. Do not expose candidate repository list
  remotely until its full-list row-read cost, call frequency and cache or
  pagination policy have been bounded and explicitly approved by the user.
- Candidate `GET /api/v1/repositories` reads a complete owner-scoped
  `repository_directory` manifest containing every per-repository GitHub App
  accessibility proof, owner services
  and Cookie/API-key/account in one D1 read batch. Trusted injected discovery
  requires a closed page chain and exact total before atomic publication;
  directory size is capped at 500, discovery at ten pages, and validity at
  300 seconds. Missing,
  expired, inconsistent or account-changed proof returns 503, never a partial
  success. API-key `repositoryIds` narrows the complete result; repositories
  without a service remain listed. Real GitHub App refresh is unconnected.
- Candidate owner-public `POST /api/v1/watches` requires a saved 300-second
  `public_upstream_proofs` row from a trusted resolver, an unrestricted
  `watches:write` principal, exact body, Idempotency-Key and Cookie Origin
  where SameSite=None. The write batch rechecks the proof, account,
  credential, stable watch control, capacity and idempotency key, then stores
  the watch plus completed 201 response atomically. Replay returns the saved
  response without another watch or usage charge. Public proof staging is
  synthetic/trusted only; real GitHub resolution and workerd verification of
  this route remain unconnected under the D1 cost pause. Personal private and
  shared watch creation remain closed.

- Derive the local D1 entitlement projection from the persisted storage-form
  users entry with `entitlements_for_user` at a fixed timestamp. Use its
  period, monthlyProcessingLimit and strict resetAt; do not copy plan rules
  into the Worker. Paid expiry changes to free; upgrades retain period/usage.
- `account_cycle_rules.py` and `product_entitlement_rules.py` own the pure
  monthly cycle and product entitlement rules. `quota.py` and
  `entitlements.py` re-export them for existing Server callers; the local
  Python Worker packages those same pure modules for live reprojection and
  the exact product-v1 usage DTO from saved bucket/attempt counts.
- `cloudflare_account_adapter.py` submits Server-owned D1 account commands
  through one async `batch()` after reading persisted snapshots. Every batch
  rechecks those snapshots; separate awaits do not form a transaction. The
  caller must pass `state_for_storage` account JSON and validated billing
  handler output. Pending association updates users, billingEvents and
  billingPendingUpdates in one guarded batch; this is not yet wired to Creem.
- In D1 claim, global monthly provider attempts use the UTC month, while
  billing-owner monthly attempts use the account entitlement's persisted
  `period_start`/strict `valid_until`. Do not count owner attempts by
  `provider_attempts.period_utc`: a paid cycle can cross a UTC month.
- `cloudflare_analysis_adapter.py` reads owner snapshot/revision/limit from D1
  for a first reservation or claim and reads the claim-frozen revision for
  first-result publication. Retry/terminal failure keeps attempt spend,
  persists the deadline or atomically releases reservation and job state.
  Replay/released charge keys, full job scheduling, source-only/cache replay
  and all cancellation/revocation paths still need mapping.
- D1 reservation reuse must confirm an active charge key under one batch
  before returning its prior reservation. A released charge key may reopen
  with a new reservation only for the same owner/module, while updating the
  current period bucket without resetting usage. Both paths remain local
  mapping evidence until the real scheduler is connected.
- The Server-owned local D1 webhook receipt verifies the raw request bytes
  with the same Creem HMAC helper as `billing.py`, then stores a normalized
  trusted update before ACK. Applying that receipt marks it applied in the
  same batch as users, billingEvents, pending updates and owner revision.
  `creem_event_rules.py` owns the pure Creem event normalization and requires
  an explicit plan-to-product-ID binding. `billing.py` supplies its existing
  configured IDs and retains its public entry; the local Python Worker packages
  the same pure module with synthetic IDs. `record_signed_creem_event` accepts
  that normalizer as a trusted callback, and saved eventId must match the
  signed raw event ID. Real handler/key binding, account settlement and
  receipt retention remain unconnected.
- First-generation D1 analysis enqueue computes owner/global active caps in
  one batch. On cap rejection, it releases the new reservation and marks the
  source context throttled without creating a job. Existing-generation reuse,
  supersession, stale-admission release and complete due-job lifecycle still
  need mapping before a real Cloudflare scheduler can run.
- `cloudflare_analysis_adapter.claim_due_analysis` scans at most 16 due Jobs
  in persisted owner-fair order. It atomically terminates an invalid
  source/context/reservation binding and releases any reserved usage before
  claiming an eligible Job; a live running lease and an account projection
  awaiting refresh stay untouched. The local scheduled probe calls no model.
  A clean projection with a changed or expired billing cycle also blocks the
  old Job and releases its old reservation. A third-attempt running Job whose
  lease expired becomes failed and releases its reservation without another
  provider attempt. Concurrent selector races and complete scheduler
  composition still require mapping.
- The real Creem handler mutates in-memory users, billingEvents and pending
  updates under `STATE_LOCK`; `persist_state` later flushes them through
  `db.save_state` and `state_for_storage`. The probe event batch is not this
  path. An async D1 adapter must cover every account writer with revision
  changes and preserve encrypted fields, pending and late events.
  `billing_account_rules.py` now owns the pure account/event/history decision;
  the existing handler applies its output and retains the local quota write.
  `D1AccountTransactions.settle_webhook_receipt` calculates from the stored
  encrypted account JSON without decrypting untouched token fields, checks
  receipt ID/owner/revision/content, and atomically stores account/event/receipt.
  `park_webhook_receipt` appends an unmatched signed update under receipt and
  pending-list CAS without dropping a full list; duplicate parking does no
  write. `reconcile_pending_for_owner` processes at most 16 matching receipts
  in event-created order, one atomic settlement at a time. Account association
  must invoke/retry this trusted path; there is no cron or user route. The real
  key/HTTP handler and every other account writer still need D1 composition.
  `cloudflare_creem_handler.accept_signed_creem_webhook` now composes the pure
  Creem parser, verified receipt, persisted-owner match, settlement/parking
  and dirty entitlement refresh from injected bindings and config. It rejects
  ambiguous owners and oversized bodies; duplicate delivery can repair a
  previously failed projection refresh. The isolated `/server-map/*` route
  exercises only synthetic values; no product webhook route is registered.
  ACK must follow durable receipt persistence, unlike the current HTTP
  response-before-finally flush order.
- On D1 entitlement refresh, update an existing current-period processing
  bucket's limit only when it changed; preserve used/reserved counts and do
  not write the whole backlog. A new period bucket remains created on first
  reservation. An upgrade's saved usage must not show the old limit.
- `product_api._store()` constructs a synchronous SQLite `ProductStore` for
  each product-v1 route; some reads also call `db` directly and authenticate
  through the in-memory users map. Cloudflare REST adaptation must map all
  three seams while preserving the same Cookie/API-key contract and DTOs.
- `product_dto_rules.watch_dto` is the shared watch projection for SQLite
  ProductStore and local async D1. The candidate `GET /api/v1/watches` lists
  only the authenticated billing owner's active watches, applies API-key
  `watchIds` restrictions and returns an empty list for a key restricted only
  by repositories. Its GET has no D1 batch write/model side effect. Source and
  Item reads still require multi-source, permission and publication fences.
- Public Pricing plans now take PR/CI/Updates capacities directly from
  `product_entitlement_rules.PLAN_ENTITLEMENTS`; no reviewLimit or checkout
  file/byte limits belong in the public product catalog. Local Billing keeps
  provider/customer/subscription facts and `subscriptionEvents`, while
  `billing_account_payload` initializes ProductStore and exposes product
  entitlements, intelligent-processing usage/runtime attempts and consumed
  `processingActivity`. Never label legacy scan quota ledger as new usage.
  `product_billing_projection.billing_account_dto` now owns the pure account/
  subscription-history shape for local and candidate. Candidate Cookie-only
  `/billing` batches current principal, product usage and recent consumed
  history in one D1 read snapshot, sends no-store, and rejects API keys.
  Candidate `/billing/plan` reads a fresh trusted `billing_public_catalog`
  D1 projection, strips old scan/model fields and overlays current
  `PLAN_ENTITLEMENTS`. Anonymous reads are public; a valid Cookie adds account
  data in the same batch, and a revoked Cookie cannot disclose it. Missing or
  expired catalog returns 503, never fake paid prices. Trusted local-only
  `D1BillingCatalogTransactions.stage_verified_catalog` requires a complete
  three-plan shape and monotonic sourceRevision, publishes atomically after
  caller-verified provider data, rejects stale prices and skips identical
  replay. Real Creem product fetch/binding, refresh trigger and provider
  checkout/account writes still require CF2 adaptation.
  `creem_public_catalog_rules.verified_public_catalog` validates an injected
  fetched-product set against configured IDs, active recurring month/year
  periods, positive integer cent prices and one currency before trusted D1
  staging. Never stage a provider response by product position or infer a
  missing/malformed product as configured. No live Creem fetch is wired.
  `product_public_catalog_rules.catalog_payload` is shared by the D1 catalog
  writer and GET reader for completeness/expiry and current entitlements;
  Pro/Max default public descriptions match local Billing projection.
- `product_dto_rules.source_context_dto` and `source_record_dto` own the
  exact SQLite/D1 Source projection. Candidate `/api/v1/sources` list/detail
  prepends API-key, session and user SELECTs to the four Source/publication/
  dependency/fence SELECTs in one D1 `batch()`. It denies a changed or revoked
  identity before projecting rows. Keep this same-snapshot auth proof when
  changing Source reads; an earlier `_principal` call alone is insufficient.
  `product_source_filters.py` is shared by local REST and Worker for module,
  context and API-key resource filters. An unclassified Release without an
  Item stays visible; stale secondary dependencies hide assessments. The
  local HTTP candidate and tests verify this narrow slice, not full CF2.
  Publication writes CAS every dependency's `configurationRevision`.
  Read-side config revision alone must not hide an already successful saved
  judgment when analysis is switched off; design 02 keeps historical judgment
  readable. Source/context semantic version, permission, source revision and
  active-watch fences still govern current read visibility.
  Require one matching saved context fence per publication dependency,
  including the primary Source/context; an empty or mismatched fence set must
  hide the assessment even if its rows still exist.
  `product_source_filters.filter_sources` also handles `releaseId` against
  saved Release `sourceFacts` on both local REST and Worker; do not filter
  release lists by Item presence or reconstruct source facts in Web.
- Candidate `/api/v1/items` list/detail uses one D1 read batch for current
  ItemVersions, accessible source contexts and handling events, prepended by
  the same-snapshot Cookie/API-key/user proof. `product_dto_rules.py` shares
  Item and handling DTOs with SQLite; `product_item_filters.py` shares view
  and API-key restrictions. Both readers must check every ItemVersion source
  and context fence before returning it, including secondary source version
  and authorization revisions. Config-only analysis-off keeps historical
  judgment readable; a new model publication still CASes config, while
  handling writes CAS Item revision, source/context semantics and permission.
  Empty or stale source/permission fences do not
  authorize private Item content. Candidate Item PATCH checks `itemVersion`
  and `If-Match`, then atomically guards the same persisted identity and every
  dependency fence while incrementing Item revision and inserting a handling
  event. A failed guard rolls back both writes; GET/PATCH never charges model
  usage. Candidate Item overview joins the seven Source, three Item and three
  principal SELECTs into one read-only D1 batch before counting; never build
  dashboard counts from separate authority snapshots. This is local-only
  HTTP evidence.
  Candidate `GET /api/v1/jobs/{id}` uses the same batch-local identity proof
  and returns only the requester's manual sync Jobs, never an analysis Job.
  It also requires the associated watch to remain unarchived under that owner,
  or the repository service to remain active under that owner, in the same
  read snapshot. Member sync resource authorization remains unmapped and must
  fail closed. Job GET is read-only and does not schedule, renew or charge.
  Local REST and candidate Job GET share `product_job_filters` for API-key
  `repositoryIds` and `watchIds` restrictions. A shared-watch Job must meet
  every supplied dimension and its parent repository service must remain
  active under the owner; archived watches and disabled repository services
  hide sync status. Candidate identity, Job and resource checks share one
  D1 read snapshot.
  `cloudflare_repository_adapter.D1RepositoryTransactions.put_service` is a
  trusted local-only command, not an HTTP repository write. Its D1 batch
  rechecks the owner's entitlement, active repository count, revision and
  queued reservations, then updates the service, releases queued processing
  reservations and advances PR/CI Source configuration fences atomically.
  Running leases retain their state but cannot publish against the new fence.
  Shared-watch parent changes also advance Updates configuration fences,
  release queued work and, on installation change, revoke authorization and
  cancel running work. A real caller still needs current GitHub App repository
  authorization before this command can be mounted.
  Candidate `GET /api/v1/repositories/{id}/service` is owner-only and requires
  current stored GitHub App repositoryItems bound to the service installation,
  a non-expired accessible repository discovery proof, and any API-key
  `repositoryIds` restriction. Cookie/key/user, service and proof are read
  in one D1 batch; changing the account before that batch fails closed.
  This detail route does not imply the repository list or write routes are
  mapped, and GET performs no provider/model work or D1 write.
  `cloudflare_manual_sync.D1ManualSyncTransactions` is a trusted, unmounted
  owner-only command for a saved public watch or managed repository. Its D1
  guard rechecks the exact storage account, active resource and absence of an
  active logical Job before enqueue; repository sync also requires current
  account GitHub App item and matching fresh installation proof. An active Job
  is reused only for the same requester. It creates `sync_watch` or
  `sync_repository` with `manual_sync` trigger and no model reservation or
  attempt. Private/shared watches, member sync, request idempotency and HTTP
  authentication remain unmapped for this command.
  When a caller supplies a Cookie/API-key proof, the trusted command validates
  its expiry, required read plus `sync:write` scopes and resource restriction,
  then rechecks the exact key/session/user in the D1 enqueue batch. The
  authenticated read helper records the concrete session ID for that proof.
  These checks prepare HTTP composition but do not themselves mount POST sync.
  `request_idempotent` now commits a manual Job and completed
  `request_idempotency` response in one guarded D1 batch. Same-key replay
  returns the saved response; a second key can reuse one active Job and save
  its own response. Archive/authority races roll back both rows. Candidate
  `POST /api/v1/watches/{id}/sync` and
  `POST /api/v1/repositories/{id}/sync` now bind that transaction to a current
  Cookie or scoped API key. They require `{}` and `Idempotency-Key`, enforce
  SameSite=None Origin for Cookie requests, and return 202 with a fact-only
  Job. The Worker forwards `Idempotency-Key`. GET/manual sync never queues
  analysis or spends intelligent-processing units. Owner public watches and
  managed owner repositories are the routed scope; member/private/shared
  sync remains closed.
  Candidate `PUT /api/v1/repositories/{id}/service` is limited to an existing
  owner service. It derives installation ID from that saved row, requires
  If-Match, a matching current account GitHub App item and unexpired D1
  discovery proof, and rechecks the exact Cookie/key/user and proof in the
  write batch. It cannot create a service or switch installations through
  HTTP; those require a separate verified GitHub App authorization path.
  Configuration changes do not enqueue model work or increase usage.
  Candidate `/api/v1/*` responses use `Cache-Control: no-store` with private
  identity Vary headers; successful versioned detail/handling responses keep
  their ETag. The local workerd response-header driver verifies both.
  `GET /api/v1/watches/{id}` is now shared by local REST and the candidate;
  resolve only an unarchived owner watch, apply API-key watchIds restrictions,
  and keep the candidate's identity and watch row in one D1 read snapshot.
  The candidate HTTP entry emits an `ETag` from the saved revision on successful
  Item/watch detail or handling responses, matching local REST's If-Match
  contract. Keep ETag off list/overview payloads.
  `cloudflare_watch_adapter.D1WatchTransactions.create_public_watch` is a
  trusted local-only D1 command; it does not prove public upstream identity
  or expose `POST /watches`. A future caller must provide a resolved public
  GitHub repository. The command derives the active-watch limit from the
  persisted storage-form user, then guards account snapshot, owner active
  count, unique watchScopeKey and watch_controls revision in one D1 batch.
  Recreated A→B→A interests advance contextVersion 1→2→3; duplicate active
  creation and a raced last slot roll back.
  Source and Item reads must also fence current watch archival: archived
  watch contexts do not appear in lists/details even while their old GitHub
  authorization lease is still valid, and a saved assessment depending on
  any archived secondary watch context is stale. Keep the SQLite and D1
  readers aligned. For linked shared watches, a missing, paused, or
  owner-mismatched parent repository service hides their Source/Item content
  even if the watch proof and Source authorization lease remain valid. Item
  handling D1 writes recheck that parent in the guarded batch; the local
  handling transaction rejects a paused linked parent as stale.
  `D1WatchTransactions.archive_watch` maps a trusted local
  batch for archival, context/target revocation, active Job cancellation and
  reserved-usage release. It checks bucket consistency and leaves provider
  attempts spent; a late result cannot publish. Candidate product
  `DELETE /watches/{id}` now binds Cookie/API-key scope and resource
  restrictions inside the read and write batches for owner public watches.
  Private/shared watch deletion remains unmapped.
  SQLite `ProductStore.archive_watch` now performs the same context/target
  revocation, active Job cancellation and reservation release inside its
  immediate transaction. A recreated stable watch scope must renew GitHub
  proof with an authorization revision above the archive revision; stale
  pre-archive proof must not restore access.
  `D1WatchTransactions.update_public_watch` also backs candidate product
  `PATCH /watches/{id}` for owner public watches only. Its
  batch CASes owner account/watch/control and active limit when enabling,
  advances semantic contextVersion only for changed interests, cancels queued
  analysis with reservation release, and fences running claims without
  refunding attempts. SQLite `ProductStore.update_watch` cascades to linked
  contexts/jobs even when no discovery target exists. The HTTP PATCH/DELETE
  paths recheck credential and user snapshots inside the write batch; a key
  revoked after preflight rolls back. Analysis-off alone is
  config-only and preserves saved historical judgments for reads/handling.
  `GET /api/v1/usage/events` is shared local REST/Worker: only the
  authenticated billing owner's consumed processing ledger is visible, with
  module filter and stable finishedAt/reservationId cursor (default 20,
  maximum 50). The cursor includes a digest of owner+module and rejects
  cross-owner/filter reuse. Return opaque reservation ID, never chargeKey; the Worker
  batches current principal with event rows and performs no write/model call.
  Candidate legacy `GET /api-keys` is session-only. It batches current Cookie
  session/user with owner API-key rows, projects only public metadata through
  `api_key_dto_rules`, redacts token/hash, excludes revoked keys, and sends
  no-store headers. Bearer API keys cannot enumerate other keys. The local
  Server API-key response, requested scopes and restriction normalization now
  delegate to the same pure rules. Candidate Cookie-only `POST /api-keys`
  generates a one-time token, stores only its hash under a session/user-guarded
  D1 batch, and returns the token once with no-store headers. SameSite=None
  Origin is required. Python Workers token bytes come from Web Crypto
  `crypto.getRandomValues` through FFI; CPython tests use `secrets.token_bytes`.
  Do not depend on unverified Pyodide `os.urandom` behavior for production
  token entropy. Complete session/OAuth lifecycle remains unmapped. Candidate
  `DELETE /api-keys/{id}` is also Cookie-only, checks SameSite=None Origin,
  rechecks exact session/user in its guarded D1 batch, and preserves billing
  owner/revision and model attempt spend. A key revoked between preflight and
  write rolls back; duplicate revoke returns 404.
  `D1SessionTransactions` is trusted local-only mapping for issue/revoke after
  an OAuth identity is verified. It CASes the persisted sessions JSON map and
  storage-form user in one batch, preserves unrelated sessions, and rejects
  concurrent map changes. It does not expose login/logout HTTP or validate
  real GitHub OAuth; session IDs must be generated securely by the future caller.
  `D1OAuthStates` similarly maps trusted `app_state.githubStates` issue/consume
  with one D1 CAS batch and a 10-minute upper expiry bound. State consumption
  is single-use across concurrent callbacks/restarts; it does not exchange
  GitHub codes or expose `/auth/github/*` until runtime/credential gates pass.
  Consume a present OAuth state before validating its expected kind/expiry,
  matching local callback semantics: a wrong-kind or expired callback burns
  the state and cannot replay it through another route.
  The synthetic local HTTP fixture now binds the queued analysis Job and one
  Source context to the first watch. Candidate DELETE must atomically leave
  `reserved=0`, cancel that Job, revoke the context, and keep provider attempts
  and API-key last-used unchanged; the second watch remains deletable after a
  real process restart.
  Candidate Cookie Item PATCH must enforce a trusted Origin or Referer when
  `PULLWISE_COOKIE_SAME_SITE=None`, before reading the body or touching D1.
  The Worker reads `PULLWISE_ALLOWED_ORIGINS`/`PULLWISE_APP_URL`; absent trust
  configuration fails closed. Bearer API keys do not require browser Origin.
  A non-empty malformed `X-Pullwise-Api-Key` header combined with Cookie or
  bearer session is ambiguous authentication (400); never let preliminary
  Cookie resolution silently fall through to a different batch identity.
  `product_item_filters.filter_items` is shared by local REST and Worker for
  actionType, lifecycle, handling disposition, PR pullNumber and CI runId.
  Pull/run identity filters require matching module plus repositoryId and
  return 422 `INVALID_CONFIGURATION` when malformed; do not silently treat
  them as a global search or borrow facts across Items.
- `cloudflare/server` is a separate **local-only candidate** for the actual
  Server Python Worker HTTP entry. `src/entry.py` routes read-only `/health`,
  authenticated product GETs for profile, usage, watches, Sources, Items,
  overview and requester-owned sync Jobs; Item handling PATCH; and raw-byte
  `POST /webhooks/creem` through Server-owned modules. Unported routes return
  404. `cloudflare_product_read.py` checks persisted Cookie sessions and hashed
  API keys, rejects mixed/expired/restricted identities, and rechecks identity
  in the same D1 read batch as `/me`, `/usage`, `/watches` and protected
  product rows. Usage bucket, ledger and owner-cycle attempts share one
  read-only batch. The candidate does not update API-key
  last-used metadata on every GET; define a bounded policy before migration.
  Its read-only health checks presence of the 22 D1 tables required by
  currently routed endpoints; it is not a full schema/migration readiness gate.
  Its Wrangler config has no cron/public route, uses a synthetic `remote: false`
  D1 ID, and must not be deployed. Sync exact Server modules into its ignored
  `src/pullwise_server` before running. The local candidate passed a real
  process restart and webhook replay; this does not validate remote runtime,
  real secrets, full account/session lifecycle or all shared product-v1 REST.
- D1 charges by rows written, including indexed writes. Keep scheduled D1
  commands bounded to changed rows; never refresh an entire waiting backlog
  on each wake. Inspect D1 `meta.rows_written` and per-query analytics before
  expanding queue or cron frequency. The local probe uses `remote: false` and
  does not establish remote cost behavior.
- Do not configure Cloudflare cron triggers. The local probe Wrangler config
  has none; its handler remains available for explicit local tests only. No
  remote schedule is authorized. Never re-enable the unrelated
  `gamelens-jobs-staging` every-minute cron stopped by the user.

- `pr_followup.reconcile_thread` aggregates saved pr-followup/v3 answers inside
  the fact/result transaction. One verified thread/context has one Item; source
  publication and processing consumption still occur per comment. All six
  question bindings, reply dependencies and observed thread roster are required
  for reliable negatives and adjacent handling inheritance. Missing identities,
  partial material and stale parent dependencies cannot withdraw a request.
- Inline reader facts carry pullAuthor and the complete observed GraphQL
  threadCommentIds roster. A complete GraphQL identity page does not prove that
  every comment body has been fetched. Aggregation checks both; incremental
  traversal is not an atomic GitHub snapshot.
- Product API header handling must accept stdlib HTTPMessage as well as Mapping;
  dictionary-only route harnesses miss real HTTP If-Match/Idempotency-Key loss.
- Completion claims belong to model-derived evidence.progressType, never the
  GitHub sourceFacts block. They cannot close or hand off an Item. Unknown or
  conflicting thread facts remain null; incomplete membership cannot prove
  reliable negatives. Optional local validate_thread_contract.py checks actual
  DTOs against OpenAPI with the already available PyYAML/jsonschema environment.
- `docs/cloudflare-domain-transaction-map.md` maps actual domain tables to finite
  local D1 batches. The synthetic account CAS/payment-fact preservation tests
  are not a Creem runtime adapter or a CF2 pass; production account/entitlement
  integration and all migration gates remain required.
- The local D1 account mapping now freezes a monotonic owner entitlement
  revision at claim and rechecks it at publication. A trusted accepted-event
  batch stores the user and billing-event entry together and marks projection
  dirty; a separate trusted recalculation restores it with strict validUntil.
  A→B→A and expiry are locally tested. Every real account/Creem writer must
  participate before enabling this protocol; the existing handler and encrypted
  state have not yet been adapted to Workers.

- Server and Web both target Cloudflare; follow deployment appendix 08 alongside
  the PR/CI/Updates product design. P3 adapters and P5a continue in parallel with
  runtime/storage validation. CPython fixtures are not Workers compatibility or
  deployment evidence. Preserve the shared REST contract and account/Creem data.
- `github_authorization.py` checks current GitHub account, repository maintenance,
  App installation and module permissions using a server-resolved credential
  binding. Only a conclusive denial revokes; HTTP/credential/shape uncertainty
  does not extend proof validity. Tokens never belong to DTOs, logs or reprs.
  Shared watch App authority belongs to its target repository, not the public
  upstream. Include target installation, configuration and billing owner in
  renewal/fact publication fences and cascade target revocations to its watches.
- `github_release_reader.py` reads one bounded page per call via injected GET,
  validates stable repository identity around mutable name routes, and binds
  pagination to the target and high watermark. GitHub Release has no reliable
  top-level `updated_at`: use only known `published_at`, never local read time
  or `created_at` as edit evidence. Old edits may update facts without gaining
  new analysis eligibility. Page omission/404 never proves deletion; a multi-page
  scan is not a complete snapshot and the six-hour cadence adds scan latency.
- `github_transport.py` is the read-only CPython reference: fixed GitHub origin,
  no redirects/retries, bounded decoded JSON, socket inactivity timeouts, and
  sanitized failures. It does not prove total slow-stream exit or native
  Cloudflare I/O. Preserve `GitHubUnavailable.retry_at` through adapter layers;
  permission retries and `github_read_backoffs` retain retry deadlines across
  restart. Manual/event/scheduled fact reads share the target's cooldown. Live
  credential-scope rate coordination, conditional reads and runtime composition
  remain unimplemented; do not claim production ingestion readiness.
- `github_ingestion.build_github_fact_sync` explicitly composes PR, CI and
  Updates readers with `GitHubCredentialResolver` and the permission checker;
  constructing it performs no I/O and default startup stays unconfigured.
  Credential callbacks reuse account/identity/installation access metadata;
  cached `canAccess` selects an identity, never proves maintenance authority.
  Recheck the chosen identity and installation-access record after issuance,
  as they may change independently of the account/target rows. Honor known
  token expiries; existing OAuth storage lacks refresh/expiry lifecycle data.
- Pass readers the discovery snapshot after the sync transaction's own
  schedule/stamp writes; otherwise full-snapshot credential fencing rejects
  legitimate scheduled reads. Do not weaken permission fields to avoid this.
- PR receipts persist a validated `pull_number` locator only; review/comment
  bodies and state still come from authoritative refetch. `pr_state.updatedAt`
  fences stale PR snapshots. REST review pages alone do not prove an effective
  formal review or thread resolution. When a bounded GraphQL review reader is
  injected, use latestOpinionatedReviews with review/reviewer IDs and bracket
  it with the same PR snapshot; missing/partial evidence stays unknown. Direct
  dismissed review state removes only the formal rule. Preserve text requests.
  Scheduled discovery rotates through persisted open parents and refetches one
  PR detail before the open list. Mark the check only in the fenced fact write;
  manual sync never reads/advances that checkpoint. This compensates for missed
  closes of known PRs, not unseen closed PRs or atomic snapshot coverage.
  The reader scope changed; pre-reconciliation discovery cursors need an
  explicit checkpoint transition before live rollout, never a silent reset.
- CI `FactPage.run_states` persist separately in `github_run_states`, under the
  fact transaction's permission/config/generation fence. Retain each attempt;
  older timestamps and terminal-to-nonterminal observations cannot regress it.
  Snapshots contain the current jobs page, not a synthesized complete run.
  Success never creates a failure source or proves recovery. Direct job events
  with verified run/attempt identity need no first-page membership assumption.
  Missing attempt identity still requires explicit membership evidence.
- Native CI steps without downloaded logs have `unavailable` model evidence.
  Do not invent log symptoms or infer cross-run identity. Raw run snapshots
  remain internal; any future REST read must apply current repository authority.
- Fact sync publishes `product_projection` rule Items via `product_rule_items`
  in the same fenced transaction as sources/contexts, with no model or usage
  side effect. Current requests create one Item per GitHub user/team; CI failure
  creates one per job/attempt. An empty projection never proves withdrawal.
  Complete request lists can withdraw a request; explicit PR parent closure
  reconciles related Items while retaining child facts and all dependency fences.
  Parent reopen creates a new occurrence pending confirmation, never revives done.
- Rule handling inheritance requires adjacent versions, unchanged action/material
  and context/config, and complete rule evidence. Audit `handling_carried` with
  a system actor. Unknown semantic updates keep existing Items pending with
  expired historical evidence and no current assessment, not a fake new result.
  Repeated facts preserve ItemVersion/attention time; lastSyncedAt comes from
  source freshness and must not force a new ItemVersion.
- `defaultAssigneeId`, handling assigneeId and nextActors.githubId are GitHub
  identities, not Pullwise user IDs. Match action views to the signed-in
  account's githubId; manual assignment takes priority. Unverified team
  membership stays unassigned. Current-version done/dismissed is a handling
  closure, not GitHub resolution; stale-version handling cannot silently apply.
- `github_pr_threads` uses fixed read-only GraphQL documents and validates
  repo/PR/thread/comment/reply identity. Each call advances bounded thread or
  comment pagination. Optional REST enrichment only joins matching returned
  comments; missing/partial pages leave unknown, never deleted/resolved.
  Scheduled reads persist its continuation in the REST discovery cursor;
  repeated traversal remains incremental, not an atomic complete snapshot.
- `github_ci_logs` has no default network transport. Its callback must enforce
  public peers/TLS and a hard deadline; late-result checks alone are not proof
  of bounded exit. Keep auth only on the GitHub API hop, redact before storage,
  and cap download at 5 MiB. Select one complete-line tail window at most 20 KiB,
  preserving line numbers and unknown stage/step; do not label symptoms by regex.
  Optional CI log mode uses one job per page to bound network work. Changing that
  reader page size invalidates existing scope-bound cursors and needs an explicit
  checkpoint transition before live rollout, not an automatic backfill reset.

- Product-v1 Cookie sessions and API keys share one router and DTOs. Reject a
  request carrying both identities. Cookie writes under `/api/v1` or `/v1`
  still require the trusted browser Origin check; path aliases never grant a
  CSRF exemption.
- Product-v1 scopes are `profile:read`, `repositories:read`,
  `repositories:manage`, `items:read`, `items:write`, `sync:write`,
  `watches:read`, `watches:write`, and `usage:read`. Retired scan scopes never
  authorize a new product operation.
- `product_store.py` owns new domain persistence. Source and Item publication
  must fence every source, context, configuration and authorization revision.
  Reads filter inaccessible or expired source contexts before producing rows or
  counts; never aggregate globally and hide rows afterward.
- Publish a successful model result only through
  `ProductStore.publish_assessment_result`: assessment insert/cache replay,
  ItemVersion publication, and processing `reserved -> used` must commit in one
  `BEGIN IMMEDIATE` transaction. Any stale source/context/config/auth/item or
  reservation mismatch rolls back all three and leaves the reservation intact.
- Treat assessment answers as semantic cache data, but question bindings and
  evidence IDs as publication-context data. Every answer key needs one local
  binding whose evidence IDs exist in the current Item snapshot. Persist the
  complete public assessment in ItemVersion; cache replay rebinds current
  context/evidence and appends `assessment_rebound` to reopen prior handling.
- `analyze_source` jobs freeze source/context/config/auth revisions and the
  processing reservation when queued. Claim with a random 120-second token in
  a short SQLite transaction; successful publication must validate that token
  and every frozen binding inside the same transaction that completes the job.
  An expired executor cannot publish or consume, and terminal eligibility
  rejection releases the still-reserved processing unit atomically.
- Retry an `analyze_source` job in the same generation with persisted
  `retry_wait`/`next_attempt_at`; claim increments its attempt and attempt 3 is
  terminal. Only the current unexpired claim token may record failure. A final
  or non-retryable failure releases the processing reservation atomically;
  user routes must never reset or create these retries.
- Enforce analysis queue admission under the same SQLite write transaction:
  at most 1000 active jobs globally and 100 per billing owner by default.
  Rejection releases the just-created processing reservation and marks the
  source context throttled. Claim rotates by durable billing-owner claim order;
  use SQLite insertion order to break same-second enqueue ties, never random ids.
- One analysis logical key retains only its newest input. A newer source/context
  binding supersedes the older active generation and releases its reservation;
  if the old generation was running, delay successor claim until that lease
  expires. Re-enqueueing the same input after attempt 3 returns the failed job
  and releases the redundant reservation instead of resetting attempts.
- `item_handling_events` are append-only and their effective order is SQLite
  insertion order (`rowid`), not random UUID order. Consecutive events may share
  one-second timestamps, so never select the current event with `created_at + id`.
- Manual sync accepts only an empty object plus `Idempotency-Key` and creates
  only `sync_repository` or `sync_watch`. Only a server-owned `TrustedTrigger`
  may create `analyze_source`; GET, handling writes and sync never do so.
- Freeze each stable processing control's initial-backfill source keys exactly
  once. Persist both the fixed set and completed subset; restart, disable/
  enable, API-key rotation, or watch delete/recreate must replay that set and
  must never select a new page of historical sources.
- Freeze `processing_controls.eligible_since` on the first valid analysis
  authorization. Daily discovery admits only authoritative create/change times
  at or after that boundary or sources in the fixed initial-backfill set.
  Persist opaque GitHub cursor/high-watermark pairs with exact compare-and-swap;
  Store must not infer ordering from cursor strings or local first-seen time.
- `ProductFactSync` is the trusted fact-sync orchestration seam. Its reader and
  processing-budget resolver are server dependencies, never request fields.
  `run_due`/`run_scheduled` use persisted 900-second PR/CI and 21600-second
  Updates schedules; `run_manual` neither reads discovery checkpoints nor
  establishes eligibility, advances checkpoints, reserves usage, or schedules
  analysis. `main(fact_sync=...)` / `PullwiseThreadingHTTPServer(fact_sync=...)`
  can host one background fact worker independently of HTTP requests; default
  startup leaves it unconfigured until live readers and permission refresh are
  assembled and validated. Do not claim production GitHub ingestion is running.
- `/webhooks/github` verifies the raw HMAC and matches persisted App,
  installation and repository bindings before storing minimal durable receipts.
  ACK does no GitHub/model I/O. Event processing waits 30 seconds and refetches
  authority through the reader. Signed installation deletion/suspension and
  repository removal immediately invalidate matching authorization and jobs.
- Discovery proofs expire within five minutes and are module-scoped. Refresh
  them only from trusted authorization checks; public visibility or webhook
  payload permissions are not proof. Recheck expiry after network reads and
  budget resolution. The first valid analysis-proof time may initialize
  `eligible_since` only in trusted discovery, so the stability delay cannot
  move that boundary past the first eligible event.
- `ProductFactSync.run_authorization_due` renews proofs independently of the
  15-minute/6-hour fact schedules, starting 60 seconds before expiry. Inject
  its checker only as a server dependency. The checker must verify the entire
  target's module/installation/repository/owner authority, including a shared
  watch's target repository; public visibility is not an authorization proof.
  Discovery target snapshots include watch `owner_id` and `target_repository_id`
  for this purpose. GitHub checker and Releases reader have injectable reference
  implementations; live credential and target-runtime composition is pending.
- Permission refresh claims and retry times persist in
  `discovery_authorization_refreshes`. Network checks run outside SQLite
  transactions; publication requires the same 120-second claim and unchanged
  target snapshot. Failed checks retry no sooner than 60 seconds without
  extending old proof validity. Ordinary renewal preserves auth revision;
  access transitions increment it and denial cancels analysis reservations.
  Refresh never reads facts, advances discovery, reserves usage, or admits
  analysis. Manual sync and GET never invoke the checker.
- Share durable parent sync generations across manual/event/scheduled reads
  and watches of the same upstream. Reject superseded responses and older
  authoritative source times; reject duplicate source IDs within one page.
  Unknown timestamps do not gain automatic eligibility. Page omission is
  never evidence of deletion. Context-only changes are not new-source
  discovery and remain stale until the separate bounded refresh path exists.
  Manual content edits mark the new version pending without clearing an
  existing context-stale flag or creating another reservation/job.
- Use `ProductStore.atomic()` to compose admission: facts commit first, then
  checkpoint CAS, fixed backfill, reservation and job admission commit together.
  Never perform network I/O inside it. Queue rejection commits visible
  throttling and releases the reservation; unexpected admission faults retain
  facts but roll back checkpoint and quota changes. Released reservations may
  re-enter the current billing period; reserved/consumed rows retain theirs.
- Discovery configuration epochs survive watch recreation independently of
  public watch revision. Configuration writes fence existing contexts and
  cancel unstarted jobs. Preserve the original running lease deadline through
  every successor, including queued successors and revoked jobs. Same-input
  discovery generations inherit the persisted attempt count; authorization or
  configuration revision changes cannot buy three new attempts.
- CI recovery remains unknown unless `ci_triage.verified_successor` receives a
  verified job identity proof; later-run recovery additionally requires an
  explicit verified lineage proof. Job name, workflow display name or SHA alone
  is insufficient.
- Use Jev for semantic choices whenever the decision depends on natural-language
  meaning: PR intent/blocking language, CI log symptoms, and Updates relevance
  plus migration/deprecation/breaking/security signals. Code may select bounded
  complete evidence units using versioned deterministic rules, but keywords
  must never directly emit those classifications or action labels. Keep hard
  rules only for verifiable GitHub facts, CI stage metadata, identity/lineage,
  permissions, dates, counts, state projection and answer-combination tables.
- Offline `typesafe_client.py` validates request/response contracts only. Do
  not claim SDK/runtime or model-quality readiness until the fixed SDK import,
  loopback bounded-exit and real evaluation gates pass.
- The current model ID is exactly `jev-1.13.0`. Reject `jev-latest` and any
  unvalidated alternative at request construction; a future model change must
  first update the fixed evaluation baseline and versioned templates.
- Jev input is text-only. `state` may be a string, a JSON object whose leaves
  are text, or an array of text values; reject numeric/boolean/null/binary
  leaves, image/audio/video fields, and media data URLs. Keep IDs, revisions,
  authorization, evidence bindings and billing facts in local store metadata.
- The observed Jev account limit is 1,200 requests/minute and 250,000 input
  tokens/second. Keep Pullwise's conservative single-key admission at global
  concurrency 2, 60 actual attempts/rolling minute, and 6 per billing owner;
  retries count. Use key rotation for credential lifecycle, not load striping
  or usage-limit circumvention.
- Use `$42 / billion input tokens` = `$0.042 / million input tokens` as the
  current Jev cost baseline. Choose the required global monthly attempt limit
  from measured input-token distributions and an explicit budget; never derive
  a spending budget from the much higher provider RPM ceiling.
- The old scan/finding/fix/Reviewer/Worker/Gateway/Agent-first implementation
  is scheduled for deletion, not compatibility. Do not add adapters, fallback,
  dual routes or shims while removing it. Preserve only account/session/GitHub
  authorization, API-key security, Creem transaction facts/payment history,
  and other explicitly retained infrastructure.

## Current Pi Worker runtime catalog

- Pool desired revision is a target, not proof that every membership has
  converged after a wave. Include membership state in immediate-rollout
  idempotency checks. Validate rotation candidate model dependencies at promote
  time because Pool/member revisions can change after staging.
- Route ids are unique within Profile revisions, not globally. Scope Gateway
  limiter identities accordingly; output admission accounts for aggregate
  output across every requested choice.

- A stored result can precede its public snapshot/quota projection. Do not show
  a terminal result with an active snapshot or reserved quota. Warm read repair
  must reload the reconciled snapshots in one batch before responding; otherwise
  a previously captured snapshot drops failure/preflight/refund evidence.

- Persist instance update commands with only the validated release version in
  payload_json; derive the package URL from the official release repository.
  The claim transaction excludes active update commands, but keeps the Worker
  enabled so its current attempt can renew credentials. Starting an update
  requires no active run; completion requires a target-version heartbeat.
- The Node installer keeps executable ancestors root-owned and uses a bounded
  hashed Linux account name. Extract Node with `--no-same-owner`; archive UIDs
  must not become owners of code executed by the root Watcher. The Unix-root
  regression in test_installer_archive_ownership exercises the rendered tar command.
  Watcher is root with the paired Worker group;
  the execution service writes only state/checkouts/worker-home. Host commands
  remain pollable with a disabled token. A succeeded uninstall ACK can be
  replayed only through its matching authenticated command-status endpoint.
- Disabling new leases must not block the owned attempt's terminal artifact and
  result publication during host stop/uninstall. Accept those two v1 routes
  with the disabled Worker token while retaining attempt ownership, checksum,
  cancellation-state and late-publication checks; do not allow new lease/events.

- Pullwise Model Gateway is a separate `pullwise-model-gateway` process in this
  repository. It must not import or open the Server business DB. It owns the
  encrypted secret store, upstream credential injection, official-origin route
  adapters, limits, streaming/cancellation, and body-free audit JSONL.
- Server stores only Provider Connection metadata/validated model ids and an
  opaque `secret_ref`; immutable Profile Set revisions/routes; Worker Pools and
  membership desired revisions; de-secreted observations; bootstrap hashes; and
  Gateway grant JTI/hash/scope/generation/expiry/revocation. Never add provider
  secret or usable token plaintext to SQLite.
- Admin Provider Connection writes go through the fixed HTTPS write-only secret
  broker. Supported official origins are OpenAI `api.openai.com`, DeepSeek
  `api.deepseek.com`, and MiniMax `api.minimax.io`/`api.minimaxi.com`; do not add
  arbitrary endpoint, path, header, or proxy support.
- Profile routes use stable `route_id`, Pi provider `pullwise-gateway`, a unique
  enabled model alias per revision, one exact upstream model, and the
  intersection of configured allowlist with Gateway-validated model discovery.
- Published manifest routes include `upstream_provider`, derived from the
  Provider Connection in the same publication transaction. Admin publish inputs
  do not author that field. Publish a new Profile Set revision after upgrading
  older manifests so Workers can load signed native Pi model metadata.
- Keep model request differences in Pi's native capability metadata. The
  Gateway's configured API transport forwards the resulting request; do not
  add provider-specific role, token-field, or thinking rewrites in Python.
- Gateway output limits reserve a request's ceiling while it runs, then settle
  once from valid upstream completion usage after a successful response. Missing
  or invalid usage retains the full reservation. A late response from an old
  minute must never refund a new minute's counters. SSE usage inspection must
  stay bounded and must not persist prompts or response bodies.
- Treat that intersection as a live fail-closed gate: profile issuance,
  introspection, readiness, lease eligibility, and route resolution all require
  every desired route's provider status, adapter, secret version, and validated
  model catalog to remain compatible. A rotation candidate must cover every
  active Profile/Pool/member route model before staging.
- A Pool/member desired revision plus matching Worker observation and current
  non-revoked Gateway grant is required before lease. Immediate rollouts update
  every membership; rollout waves update only explicit Worker ids. Both rotate
  the Pool Gateway-token generation and revoke older grants.
- Worker control-plane and Gateway credentials are different audiences. Worker
  profile/trust pulls require the matching Worker token; Gateway access tokens
  are Ed25519-signed, five-minute by default, Worker/Profile/revision/digest/
  route scoped, and checked through authenticated live introspection.
- Provider rotation is staged write -> validate/model discovery -> canary ->
  atomic promote -> explicit previous-version retirement. Normal removal blocks
  active Profile/Pool/member dependencies and requires confirmation that the
  upstream credential was revoked; emergency revoke stops route resolution
  immediately.
- If a candidate secret was written but validation or metadata persistence
  fails, attempt retirement immediately. A failed compensation must create a
  non-secret `model_gateway_secret_cleanup_tasks` record and surface a sanitized
  failure; retry only through the authenticated empty-body Admin cleanup route.
- Batch Pool creation uses one transaction to create independent Worker ids,
  membership, and ten-minute single-use bootstrap hashes. The exchange returns
  one Worker control-plane token once; installer input must use environment,
  never URL or argv.
- Focused Gateway tests are `tests/test_model_gateway_*.py` plus
  `tests/test_worker_profile_observations.py`. Run the full `python -m pytest`
  suite before completion.
- `tests/test_model_gateway_end_to_end.py` is the safe TLS loopback plumbing
  smoke: it uses a fake provider adapter and temporary secrets, so it proves
  Server/Gateway/Worker boundaries but never claims real-provider review readiness.
- Reuse the authenticated `review-worker-protocol/v1` registration, heartbeat,
  and lease routes for Pi Workers. Worker `runtime_catalog` uses schema
  `pullwise-pi-runtime-catalog/v1`.
- Catalogs contain credential metadata and provider/model availability only.
  Reject unknown fields so API keys, bearer tokens, OAuth secrets, and other
  credential material cannot enter Server storage.
- Subscription plans own one global provider/model/thinking-level tuple.
  Snapshot that tuple when a scan job is created. During lease, match the
  requesting Worker's catalog, resolve the unique credential for that
  provider/model pair, and copy the exact selection into `runtime_selection`
  and `model_profile`.
- Public status exposes only the de-identified provider/model union as
  `availableReviewModels`. Credential ids and account labels are Admin-only.
- A cataloged Worker may advertise many providers and models. It claims only
  the oldest queued job compatible with one uniquely resolvable catalog route;
  an ambiguous provider/model pair is not routable. Never fall back to another
  plan tuple.
- Worker creation is provider-agnostic. Batch creation under a Worker Pool
  returns distinct short-lived bootstrap tokens; the Node 22.23.1/npm installer
  exchanges one token, starts the Watcher before the Worker service, and never
  prompts for or stores an upstream provider credential.
- The Watcher owns v1 registration/heartbeat. The Worker execution service owns
  one lease, checkout, Pi invocation, artifact uploads, and result submit, but
  never writes Server-owned fleet state directly.
- The existing v1 result envelope accepts `pi_agent_session` as the engine.
  Completed Pi runs retain the five standard artifact kinds; failed/cancelled
  runs retain the three terminal diagnostic kinds. Do not create a parallel
  result-ingest path.

## Four-project local debug loop

- Active v1 heartbeats require a closed execution proof with executor_id,
  run_id, lease_id and timezone-bearing updated_at. Match Server run/lease
  ownership; cap renewal at the earlier of proof time + 60 seconds and receipt
  time + 60 seconds, allowing at most 15 seconds of future clock skew. Missing,
  stale or expired ownership never renews. Business events never extend leases
  and expired active events must roll back their event/progress transaction.
- Queue admission rechecks capacity under BEGIN IMMEDIATE. Persist the scan
  job and public scan snapshot in that same transaction, then update memory;
  rejected admission or failed writes compensate the separate quota reservation.
- Gateway route limits use the Profile plus logical route id across revisions.
  Request n is an integer from 1 to 128 and reserves the aggregate choice output;
  an omitted output ceiling is forwarded as max_tokens=16384. Rotation dependency
  validation and promotion share the write transaction.

- Concurrency probes for queue admission must run distinct request ids through
  the real route and SQLite write boundary; a sequential full-queue test does
  not establish that capacity is reserved atomically.
- A live Watcher heartbeat does not by itself prove the execution process is
  alive. Test stale persisted busy state separately from an offline Watcher
  when verifying lease renewal, deadlines, and automatic task recovery.

- Pi flow acceptance must inspect `scan_jobs.started_at`, live progress, and
  final account/repository quota state in addition to terminal result artifacts.
  Pi emits `run_started` for preparing and `phase_started` for review/publishing.
  Only `review` is a billable running phase. Terminal ingest uses the same durable
  reconciliation as recovery: completion consumes, pre-review failure/cancel
  releases, and recorded core work remains consumed across later phases/replay.
- Gateway fleet readiness must agree with the lease gate after grant revocation,
  token-generation rotation, provider unavailability, and heartbeat expiry.
  A profile observation's unexpired timestamp alone is insufficient readiness.
- Verify exact source excerpts survive Worker finding serialization, the public
  issue projection, and exported issue Markdown. Worker `evidence[].text` and
  public `evidence[].summary` currently require explicit contract alignment.

- Use `ops/local_debug_loop.py` from this repository to start Server, Web,
  Admin, and the Worker's Watcher/service together. It owns only the child
  processes it starts, uses a fresh database, and writes a redacted
  `pullwise-local-debug-report/v1` under `.pullwise/local-debug/runs/`.
- Explicit local GitHub mocks remain loopback-only. Their repository sync must
  clear `repositoriesNeedSync` and return the seeded repository items without
  requiring GitHub App API credentials. Local scan branch validation accepts
  only the seeded/default branches and must not call GitHub.
- The local plumbing smoke flow creates and cancels a scan. Do not report a
  completed AI review unless the Worker has a reconciled managed Gateway
  profile/catalog, a current Worker-specific Gateway grant, and the scan reaches
  a terminal review result.
- `--hold` opens Web and Admin through the existing loopback-only local GitHub
  callback so the browser receives the fake session cookie before landing on
  Dashboard/Workers. Keep raw service URLs unauthenticated in a fresh browser;
  never add a Web/Admin production-code login bypass. `--no-open-browser`
  disables only automatic tab opening, and `entryUrls` remain in the report.
- Before port validation, replace only a prior run whose report binds the same
  Server/Web/Admin URLs. A live `local_debug_loop.py` supervisor may be
  terminated as one tree; orphan children may be terminated only when their
  parent PID and Server/Vite/Watcher/Service command shapes match that report.
  Never terminate an unknown process merely because it owns a requested port.

# Pullwise Server Agent Notes

## Python Dependency Audit

CI runs pip-audit . against project dependencies. Keep the cryptography range on a fixed line that excludes the August 2026 vulnerable 48/49 releases; do not lower it below 50.0.0 unless the advisory state is intentionally re-evaluated and CI audit still passes.

## SQLite test connection lifetime

- Wrap test-owned SQLite connections in `contextlib.closing`; a connection's
  context manager commits or rolls back but does not close the file handle.
  Keep a nested transaction context when writes must commit. Close connections
  created in helper threads before deleting temporary databases on Windows.

## Current CI target check

- CI uses current repository instructions, code, and tests directly. Do not
  restore a hash-pinned Reviewer authority script or Notion/external-authority
  gate. `test_product_ci_target.py` protects the retained pytest, pip-check,
  dependency-audit, and shell validation lanes during the 1.4 transition.

## Worker Host Platform

Pullwise worker installs target Ubuntu 22.04 hosts. Installer generation and
worker lifecycle changes may assume Linux/systemd behavior available on Ubuntu
22.04, including `useradd`, `chown`, `chmod`, `sudo`/`runuser`, logrotate, and
systemd unit management. Do not add macOS or Windows worker installer behavior.

Worker installer Python readiness must validate Python 3.10, pip, and
venv/ensurepip as separate capabilities. A host can have Python and pip without
the python3.10-venv package; dependency auto-install must repair that state
before creating any worker-instance resources.

## Worker Installer Provider Isolation

The server-generated worker installer must preserve per-worker Codex
isolation. A worker must never depend on global Codex config, root auth,
or another worker instance's auth state.

- Generated install commands and suggested env must point provider commands at
  the target worker home, for example:
  - `$WORKER_RUNTIME_ROOT/.local/bin/codex`
  - `$CODEX_HOME/bin/codex`
- The installer, saved auth commands, and systemd unit must use the same
  instance-scoped environment:
  - `HOME=$WORKER_RUNTIME_ROOT`
  - `USERPROFILE=$WORKER_RUNTIME_ROOT`
  - `CODEX_HOME=$WORKER_RUNTIME_ROOT/codex-home`
  - `XDG_CONFIG_HOME=$WORKER_RUNTIME_ROOT/.config`
  - `XDG_CACHE_HOME=$WORKER_RUNTIME_ROOT/.cache`
  - `XDG_DATA_HOME=$WORKER_RUNTIME_ROOT/.local/share`
  - `PATH` with this worker's `$WORKER_RUNTIME_ROOT/.venv/bin`, `$WORKER_RUNTIME_ROOT/.local/bin`, `$WORKER_RUNTIME_ROOT/.codex/bin`, `$CODEX_HOME/bin`,
    before the base service path
- The installer should create the per-worker config/cache/auth directories under
  `$WORKER_RUNTIME_ROOT`.
- The installer-time readiness output and a later `doctor` run with no
  intervening manual action must agree. `doctor` must not appear ready because
  it sees root/global auth or another worker's provider config.

When changing worker installer generation, keep multi-worker deployments in
mind: every worker on the same server must use only its own configured Codex
directories.
- Installer auth commands should call the worker SDK helper (`pullwise-worker codex-login` / `$BIN_PATH codex-login`), not `codex login --device-auth`, so device-code auth uses the same Python SDK path as worker automation.
- Server-generated installer scripts and admin suggested env must not emit old app-server lifecycle knobs (`PULLWISE_CODEX_APP_SERVER_MAX_AGE_SECONDS` or `PULLWISE_CODEX_APP_SERVER_MAX_TURNS`); the worker SDK owns that lifecycle.
- Admin-created worker payloads must not expose Codex CLI command/release pinning fields; admin UI/API should not send `codexVersion`, `codexUseLatest`, or plan-level CLI command policy.
- Default managed Codex automation uses the `openai-codex` Python SDK with OpenAI's official standalone CLI installed under the worker runtime root and passed through `PULLWISE_CODEX_COMMAND`; default `PULLWISE_CODEX_RELEASE` to `latest` so newly supported models do not remain blocked by the SDK-bundled CLI. Use `https://chatgpt.com/codex/install.sh`, never install `@openai/codex` directly, and preserve the worker-local path in installer env plus admin suggested env.
- Worker Python packages, including `pullwise-worker`, `openai-codex`, `openai-codex-cli-bin`, and transitive runtime dependencies, must be installed into the worker instance venv under `$WORKER_RUNTIME_ROOT/.venv`; do not install them into global/system Python. Host-level package installation is only for OS dependencies such as Python, git, bwrap, systemd helpers, and logrotate.
- Installer rollback may disable/remove only units created by the failed
  attempt. Preserve pre-existing active worker/watcher units and their enabled
  state when a replacement install fails.

## Worker Codex Runtime Concurrency

Never configure or schedule a single worker identity to run multiple Codex
SDK runtime/app-server processes concurrently.

- Treat worker capacity for Codex jobs as permanently fixed at `1`.
- Do not expose, persist, or route configurable worker job parallelism,
  max-claim, or worker-side job queue controls. The server owns the scan job
  queue; each worker claims a new job only after finishing the current job.
- Reviewer-turn concurrency is distinct from job/process concurrency. The
  server may set plan policy `reviewWorker.reviewerConcurrency` to `1` or `2`;
  the worker must realize it as fresh independent reviewer threads inside the
  one already-running SDK/App Server for the claimed job. Root semantic phases
  stay sequential, and no second Codex process may be launched.
- The failure mode is correctness, not just load: separate Codex agent CLI
  processes can refresh the same auth token/session at the same time and
  corrupt or invalidate shared `auth.json` or stored credential state. Every
  worker identity therefore keeps its own `CODEX_HOME` and auth store; never
  copy or share that credential file across worker roots.
- Do not change claim payloads, worker capacity, plan policy, or server-side
  scheduling in a way that lets one worker launch parallel Codex processes
  under the same auth identity. Bounded reviewer turns must reuse the single
  App Server/AuthManager and do not create an additional job slot.

## Worker Cancellation Slot Accounting

Cancelled jobs must release the worker's single execution slot immediately from
the server scheduler's point of view.

- The v1 heartbeat endpoint must use the fixed `review-worker-protocol/v1`
  shape and reject legacy `running_jobs` / `active_job_ids` fields. Use
  `active_run_id`, `concurrency`, `codex_app_server`, and active-run
  `progress` for v1 workers.
- Review workers must use `/v1/workers/...` and `/v1/review-runs/...` for
  registration, heartbeat, lease, progress, artifact upload, and result
  submission. Do not accept `/worker/heartbeat`, `/worker/agent-configs`, or
  `/worker/jobs/...` as review-protocol compatibility routes.
- `/worker/commands/...` and `/worker/log-streams/...` are lifecycle-control
  plumbing only and must not carry review job claim, progress, artifact, or
  result semantics.
- A job in `cancelled`, `done`, `failed`, or `lost` must not keep the worker
  busy, must not receive lease renewal, and must not block the same worker from
  claiming the next queued job.
- Keep regression coverage for this path. The important scenario is: worker
  claims a job through v1 lease, the scan/job is cancelled, the worker's next v1
  heartbeat no longer has an active run, and the same worker can still lease a
  new queued job.

Active user cancellation is a server-authoritative handshake, not an immediate
terminal write. Cancel a queued job directly, but move a claimed, running, or
uploading job through `cancel_requested` and `cancelling`; return a
`cancel_run` heartbeat command, accept cancellation progress and required
artifacts, and make only the matching `cancelled` result terminal. Pending
cancellation must not renew the lease or count the worker as running, but it
must block that same worker from claiming another job until cleanup reaches a
terminal result or the server cancellation timeout reaps it.
Reject a non-cancelled result in any cancellation state with HTTP 409 code
`JOB_CANCELLATION_AUTHORITATIVE` and canonical job, run, attempt, job-status,
and accepted-result-status bindings. A late `cancelled` receipt from that same
attempt may attach raw evidence after the timeout reaper has finalized the
job, but must preserve the reaper-owned job, attempt, review-run, and public
scan completion/error/cancellation metadata.

## Worker Install Secrets And Identity

- The public `/install-worker.sh` script must not embed worker tokens or other
  per-worker secrets.
- Admin-created install commands may prompt for the worker token or pass it via
  operator-controlled env/file, but the generated public script should remain
  reusable.
- Per-worker paths and names must be derived from the safe worker id:
  `CONFIG_DIR`, `ENV_FILE`, `AUTH_COMMANDS_FILE`, `BIN_PATH`, `DATA_DIR`,
  `CHECKOUT_ROOT`, `LOG_DIR`, systemd service name, and service user.
- Keep `/var/lib/pullwise-worker`, `/var/log/pullwise-worker`, and
  `/etc/pullwise-worker` as base directories only; mutable worker state belongs
  in the worker-specific subdirectory.
- Suggested env should include provider command variables only for providers in
  the worker's configured provider chain.
- Worker install packages are selected only from the configured/default worker
  version or the registration-time version and resolve to the official release
  wheel. Do not reintroduce an arbitrary admin `defaultPackage` override.
- Generated worker env must not carry reasoning-effort or turn-timeout policy;
  those values come from the server-owned plan policy on each claimed job.

## Worker Delete Lifecycle

Admin Delete instance is not complete when the worker disappears from the server
registry or admin list. Deleting a worker instance must also remove the
worker-host resources associated with that instance: service unit, wrapper,
logrotate entry, `/etc` config, service user when safe, instance `DATA_DIR` under
`/var/lib/pullwise-worker`, instance `LOG_DIR` under
`/var/log/pullwise-worker`, and any other instance-scoped runtime files.

Disabling a worker must atomically cancel any active telemetry command such as
`refresh_codex_quota`. Lifecycle commands may preempt telemetry commands, and a
late worker status report must not revive a command after it becomes terminal.

The server and worker may run on different hosts. Do not implement admin delete
by deleting paths on the Pullwise Server host or by assuming server-local
`/var/lib/pullwise-worker` and `/var/log/pullwise-worker` are the target worker
host. Server-side delete should express desired lifecycle state and track
pending/running/succeeded/failed cleanup status; worker-host cleanup must be
performed by a host-local worker manager, watcher, supervisor, or finalizer that
has authority over the installed worker instance.

Future lifecycle work should prefer a host-local watcher/supervisor managing the
worker process over relying on the managed worker process alone to delete
itself. A running worker may acknowledge admin delete, but the durable cleanup
responsibility belongs to the worker host manager so stopped, degraded, or
self-removing workers can still be cleaned up and reported accurately.

If a pending or running uninstall command exceeds its server cleanup timeout
before host cleanup is confirmed, the server must soft-delete only that
timed-out worker registry record so the admin list cannot retain a stuck cleanup
forever. Pending age is measured from command creation; running age is measured
from `started_at` so a command that waited in pending does not immediately time
out after a watcher starts it. Acquire the SQLite write transaction before
selecting timed-out commands so a concurrent terminal status report cannot land
between selection, command cancellation, and worker soft deletion. Keep the
terminal command row and timeout reason for auditability. Do not broaden this
cleanup to recent pending/running commands, stop commands, already deleted
workers, or other worker instances on the same host.

A single worker host may run multiple Pullwise worker instances. Do not reuse a
worker process, watcher process, systemd unit, service user, env file, config
directory, data directory, log directory, runtime directory, or lifecycle marker
across worker instances. Each worker instance must have its own paired watcher or
supervisor with instance-scoped names derived from the safe worker id.

The paired watcher is the host-local role that monitors and controls a worker
instance. Server-generated installers must make the watcher reliable by enabling
and starting it before the worker service and by ordering the watcher systemd
unit before the paired worker unit. The watcher may stop and remove the worker
service and instance-scoped resources while carrying out lifecycle cleanup.

Watcher identity is per worker instance. Multiple worker instances on the same
machine must never share a watcher id, watcher service name, runtime directory,
env/config path, or lifecycle marker.

Once a watcher service has successfully started, do not design any non-delete
path to stop, disable, or remove it, including update, restart, cleanup,
manual/local uninstall, and post-watcher-start install failures. Watcher
self-removal is valid only for an admin-initiated Delete instance lifecycle
flow, after the host-local watcher has confirmed the paired worker instance has
been successfully uninstalled.

Worker command polling for delete/uninstall must report the current worker
heartbeat slot state (`workers.running_jobs`) to the watcher. Do not use stale
`scan_jobs` running counts to decide whether an idle worker-host watcher may
execute cleanup, because an already-idle worker can otherwise keep an uninstall
command pending until server timeout cleanup.

## Agent Config Source Of Truth

The server owns subscription plan agent policy.

- Free/pro/max review agent configs are the source of truth for the plan
  provider, model names, reasoning effort/variant, and repository limits.
- Worker claim payloads must include canonical v1 `model_profile` and
  `review_request.policy` derived from server plan/business logic, plus
  `repositoryLimits`; workers should not infer those from local defaults.
- `agentConfig` may be included as server-derived backing data for admin/doctor
  consistency, but v1 workers should prefer `model_profile` and
  `review_request.policy` when driving review execution.
- The worker agent-config endpoint used by `doctor` must expose the same plan
  configs that job claims use.
- Keep the plan review-agent provider as a single `provider` field in
  worker-facing API responses.
- Persist only canonical plan policy that affects v1 jobs: Codex model,
  reasoning effort, bounded reviewer concurrency (`1..2`), turn timeout, scan
  deadline. Do not restore legacy `mode`, `scanMode`, reviewer-turn, discovery,
  bundle, or candidate limits to subscription plan config.
- Bundle and reviewer-assignment ceilings are global pipeline stage limits,
  independent of subscription plan. Store them only in database-backed system
  config as `reviewWorker.maxBundles` (`1..64`) and
  `reviewWorker.maxReviewerAssignments` (`1..128`), then forward them in every
  v1 claim as `max_bundles` and `max_reviewer_assignments`. They are admission
  checks, never permission to truncate eligible paths or tier-required
  reviewers.
- The plan-agent Admin payload owns the model-aware reasoning capability
  contract. Validate every explicit model/effort pair against it. Expose exact
  Codex `model/list` entries when available, with a declarative longest-prefix
  family/default fallback for offline operation; Admin must not need code
  changes for a newly catalogued effort.
- Mixed worker versions may temporarily differ in model support while operators
  replace old workers. Do not hide new plan capabilities by intersecting the
  catalog across the fleet, and do not add legacy worker compatibility adapters
  unless explicitly requested; worker replacement/routing is separate work.

## Whole-Scan ETA Contract

- The worker-provided estimate is for the whole running scan, not the current
  phase. The server must never derive, extrapolate, smooth, or replace it.
- Strictly validate and sanitize the estimate state, basis, timestamps, finite
  non-negative bounds, bound ordering, confidence, and reviewer parallelism
  metadata before persistence or exposure. Preserve only the newest accepted
  event sequence so a delayed event cannot overwrite a fresher estimate.
- Persist a valid running estimate in both review-run progress and the scan
  mirror, and expose the sanitized top-level estimate consistently from scan
  detail, history, batch status, and `/scans/status`. A nested progress estimate
  may remain as compatibility data, but it is not a second estimate source.
- Queued scans have no execution ETA. Terminal events and terminal result ingest
  must clear the forecast immediately; terminal UI duration comes from actual
  start/finish timestamps. Public scan/progress payloads must not expose Codex
  thread ids.

## Review Worker Protocol Semantics

Agent-First terminal transport may atomically advance an ACTIVE task head
through terminalization_requested into task_result_published; FINALIZING heads
still publish directly from their current task version.

`../codex_full_repo_review_worker_spec_v1_2_FULL_SELF_CONTAINED.md` is the
source of truth for worker-facing server behavior. The server owns the global
job queue, leases at most one job to a worker, and must not add worker-side
queue, prefetch, max-claim, or parallel job controls.

Repository materialization is a worker responsibility in v1. The server must
validate repository access, issue short-lived clone credentials, and include
`clone_url`, branch, commit, `clone_token`, and `repositoryLimits` in the lease
payload, but it must not assume the Pullwise Server host shares a checkout
filesystem with the worker. Workers clone or copy the repository into their own
isolated workspace and must reject empty checkouts before inventory/review
phases run.

Worker results use `review-worker-protocol/v1`: a stable result envelope plus a
versioned artifact manifest. Server ingest must validate protocol version,
worker/job/run/lease binding, execution status, summary, quality gate, required
artifacts, supported artifact kinds, `schema_version = v1`, `encoding = utf-8`,
`compression = none`, valid SHA-256, non-negative size, and v1
`server_artifact` storage URL shape before accepting a completed result. The
stable summary must include `overall_risk`, `result_status`, `finding_counts`,
`coverage`, and `top_findings`; do not accept top-findings-only summaries as
v1 terminal results. Store the raw envelope and artifacts; do not depend on
`report.agent.json` internals for core result acceptance.
Worker finding ingestion must normalize priority-style severity aliases consistently: `P0` maps to `critical`, `P1` to `high`, `P2` to `medium`, `P3` to `low`, and `P4` to `info`. Persisted issues, stable-summary counts, and public scan payloads must use the same canonical levels.
V1 terminal result status must preserve `completed`/`done`, `failed`,
`cancelled`, and `partial_completed` distinctly through job result rows,
`review_runs`, scan state, public scan payloads, and artifact/result retrieval;
do not collapse cancelled or partial results back to legacy `failed` or
`queued` states.
After a v1 terminal result is accepted, the matching terminal progress event
from the same worker (`run_completed`, `run_failed`, `run_cancelled`, or
`run_partial_completed`) may still arrive as the worker refreshes final logs.
Accept and store only the event that matches the terminal job status, update the
review run event/progress snapshot, and do not regress the terminal scan job,
scan state, quota state, or lease accounting back to running.

Treat worker-result receipt and worker-result convergence as separate durable
steps. Fresh submissions, checksum-identical duplicates, startup recovery, and
terminal read reconciliation must reload the stored raw result payload and
idempotently converge the resolved commit, decision events, quota, scan/issues,
and review run. Revalidate the stored v1 envelope and required artifacts during
convergence, but allow an explicitly replaceable final-log artifact to have a
new content hash and size after receipt. A non-cancelled result must never
overwrite an authoritative `cancel_requested`, `cancelling`, or `cancelled`
job/scan state. Exact duplicate convergence must be a database write no-op when
all derived review-run fields already match, while any corrupted or incomplete
derived field must still be repaired from the stored raw envelope.
Use the durable scan snapshot as the write base for result and recovery
projection, then synchronize any process-local `SCANS` mirror. A stale memory
object must never erase newer cancellation, quota, completion, or recovery
fields; the newest in-process progress counter may still be merged explicitly.

Expose the worker-facing v1 review routes explicitly: register under
`/v1/workers/register`, lease and heartbeat under `/v1/workers/{worker_id}/...`,
and run events, artifact upload, and terminal result submit under
`/v1/review-runs/{run_id}/...`. Register must be bearer-token authenticated,
store the raw registration JSON, and validate stable fields synchronously:
protocol version, worker identity binding, Linux/POSIX platform, one active job,
no local queue, and no prefetch. Progress events must validate the v1 envelope
(`run_id`, claimed `worker_id`, positive `sequence`, `timestamp`, `event_type`,
`phase`, `severity`, and `progress` with `overall_percent`,
`current_phase_percent`, and `status`) before they are durably inserted into the
review run event store with a strictly monotonic per-run `sequence` and before
they update scan progress. Preserve unknown event payload fields in the stored
raw JSON. V1 lease requests must validate `review-worker-protocol/v1`, idle
capacity (`active_jobs = 0`, `available_job_slots = 1`), no local queue, and
the required v1 capabilities before claiming any job. V1 artifact uploads must
validate `review-worker-protocol/v1`, supported artifact `kind`, `name`,
`media_type`, `schema_id`, `schema_version = v1`, `encoding = utf-8`,
`compression = none`, `sha256`, `size_bytes`, and `content_base64` before
storage; idempotency stays `run_id + artifact_id`. V1 heartbeats must validate
the fixed heartbeat shape and reject
malformed v1 payloads:
`protocol_version`, `status`, `active_run_id`, `concurrency`,
`codex_app_server`, and active-run `progress`. Idle heartbeats must report
`active_jobs = 0` and `available_job_slots = 1`; active heartbeats must report
`active_jobs = 1`, `available_job_slots = 0`, a non-null `active_run_id`, and a
progress snapshot whose `run_id` matches the active run. Resolve `active_run_id`
to the server-owned job for lease renewal, cancellation, and progress snapshots
instead of requiring worker-side queue state. Progress snapshots shown to the
product should be derived from accepted v1 run events, v1 heartbeat progress,
and stored scan state, not from raw worker-only artifact internals. The server
must not own or hardcode the jobscan detail flow definition. Workers report
their own full ordered progress steps with phase ids, labels, status, and
percent; the server sanitizes, stores, and exposes those steps as
`progressSteps` / `reviewRun.progress.steps` without assuming a fixed 30-step
pipeline or rejecting unknown safe phase ids. Quota and other business logic may
still key off known core phase ids, but display flow shape belongs to the
worker that is running the job. Existing `/worker/...` lifecycle routes are
operator plumbing; do not reintroduce `/worker/jobs/...`, `/worker/heartbeat`,
or `/worker/agent-configs` for review protocol behavior.
Active v1 heartbeat `progress` snapshots must include `message`, the full
counter set from the v1.2 spec (`source_like_files_*`, `bundles_*`,
`reviewer_runs_*`, `intent_tests_*`, `validator_candidates_*`, and
`artifacts_*`), and an `active_unit` object; malformed snapshots should be
rejected instead of accepted as partial progress.
Heartbeat `progress.updated_at` is a timezone-bearing RFC3339/ISO-8601 string,
not a numeric scan-storage timestamp. Reject non-finite percentages and
fractional event/heartbeat sequences at the protocol boundary before they can
enter progress persistence.
V1 heartbeats may also carry Codex app-server quota telemetry as `codex_quota`.
Persist the sanitized quota payload, expose it through worker/admin status, and
do not remove it while refactoring readiness, lease eligibility, or worker
details. Quota exhaustion should make the worker unable to claim jobs without
breaking the required idle heartbeat concurrency shape.

Admin manual quota refresh uses the durable `refresh_codex_quota` worker command. Queue it for any online worker (`idle`, `busy`, or `degraded`) but reject offline or disabled workers; never disable the worker when creating it, and let `stop` or `uninstall` cancel and supersede an active telemetry command. The worker must heartbeat the refreshed quota before reporting the command succeeded.

Operational worker alert emails are fleet incidents keyed by alert kind and
status, not by worker id. Keep Codex quota `low` and `exhausted` as distinct
groups, suppress the generic degraded alert when a quota alert explains it,
and persist the affected worker ids in the alert state. A worker recovery must
remove only that worker from every group; clear the incident and allow a new
email only after its last affected worker recovers. Full scan-system status
sync must replace group membership from the complete worker snapshot, while a
heartbeat sync must update only the reporting worker. Preserve migration of
legacy per-worker alert keys so an active incident is not resent on rollout.

Each leased v1 run must also have a first-class `review_runs` row. Create or
refresh it when a lease is issued, update its progress from accepted run events,
and finalize it from the terminal result envelope by storing summary,
quality-gate, usage, progress, error, and raw envelope JSON. Scan jobs run once
only, so each job has one terminal run namespace and recovery paths must not
create attempt-scoped replacement runs. Web/admin terminal views should read
server-owned run state and artifact metadata instead of parsing raw worker
artifact internals. Detailed scan payloads should expose this as a `reviewRun`
object with public terminal state and artifact metadata, never raw artifact
upload content or raw result envelopes.

Completed runs require uploaded `report.human`, `report.agent`, `coverage`,
`qa`, and `token_budget` artifacts. Failed and cancelled runs should accept a
valid terminal envelope only when it includes `qa`, `worker_log`, and either
`error_report` or partial `report.agent` diagnostics. Artifact upload must be
idempotent by run/artifact, and result submit must be idempotent by run/message
type. V1 artifact uploads must write first-class
`review_artifacts` rows keyed by `run_id + artifact_id`, preserving artifact
metadata, `storage_url`, storage metadata, optional small JSON `inline_json`,
sha256, size, and raw upload payload. The storage URL must resolve through an
owner-authenticated server GET route so web clients can read terminal run
artifacts without parsing worker internals. Do not store new v1 artifact uploads
as legacy `job_result_artifacts` compatibility entries.

Required artifact result validation must compare the uploaded artifact record's identity metadata (`kind`, `name`, media type, schema, required flag, and storage URL) as well as SHA-256 and size. Do not accept a reused `artifact_id` just because the bytes match.
Every mandatory artifact kind for the submitted terminal status must be represented by an entry whose `required` flag is exactly `true`; an optional entry of the same kind must never satisfy the mandatory-artifact gate.
For `failed`, `cancelled`, and `partial_completed` terminal envelopes, the
server may accept missing required artifact uploads only when the v1 envelope
records `extensions.worker_internal.artifact_upload_error`; completed results
must never use that exception.

Scan jobs run once only. Do not add job-level retry configuration, max-attempt controls, or recovery paths that return a claimed/running/lost scan job to `queued`; user retry means starting a new scan.

Run continuous scan-lease recovery from
`PullwiseThreadingHTTPServer.service_actions`, never from the v1 claim hot
path. A recovery must finalize `scan_jobs`, `scan_job_attempts`, and
nonterminal `review_runs` in one SQLite transaction; it must never requeue a
run-once job. Expired `cancel_requested`/`cancelling` jobs converge to
`cancelled`, late heartbeats cannot renew terminal leases, and pending
scan/quota projection is replayed idempotently until `status`, `quotaState`,
and `recoveryReason` match the terminal job.
Set `scan_jobs.projection_pending = 1` in the same transaction that creates a
reaper-owned terminal state, select retries through the partial
`idx_scan_jobs_projection_pending` index, and clear the marker only after the
durable scan and quota projection succeeds with the same terminal status and
reason. Legacy databases may run a one-time mismatch backfill during schema
migration; the recurring maintenance loop must not rescan terminal history or
evaluate scan JSON to discover pending work.

Refundable worker-failure replay must derive `requestId`, repository identity,
quota state, and preflight evidence from the durable scan snapshot before any
process-local mirror. Replaying an existing release repairs the projection but
preserves the first `quotaReleasedAt` value.

Quota should be finalized when the worker reaches core semantic review work, not
for mechanical setup phases. Preserve subscription-plan controlled model,
timeout, repository limits, and core reasoning effort; non-core phases use the
same model with medium effort.

Worker progress and reports should include the v1.2 intent-test validation
stages. Intent-test artifacts are evidence for high-value P0/P1 candidates, not
a separate bug source, and generated test failures must not be treated as
confirmed findings without validator confirmation.

## Public REST API Rate Limits

The API rate limit is scoped to public REST API automation, not normal browser
web app session traffic.

- Apply the `rateLimit` system config to `/api/v1/*` and `/v1/*` public REST
  endpoints.
- Do not apply this narrow API limit to signed-in web app routes such as
  `/auth/session`, `/repositories`, `/scans`, `/issues`, `/settings`, or
  `/billing`.
- Authenticated worker endpoints remain exempt. Separate unauthenticated worker
  probe protection is allowed, but do not describe it as browser/web app rate
  limiting.
- User-facing docs should say public REST API rate limit or API-key automation
  rate limit, not a shared browser web app rate limit.
- The database-backed Admin `rateLimit` group is the only runtime source for
  enabled state, request count, and window. Do not add environment-variable,
  production-mode, launcher, or deployment-script overrides.

## Quota And Account Terminology

Pullwise does not have a workspace quota concept. Do not rename account/user
quota to workspace quota.

- Scan quota is enforced against two buckets: account/user scope and repository
  scope.
- Admin user deletion removes all user-scope quota buckets for the deleted user,
  including auto-created current-period reservation buckets; repository-scope
  buckets are preserved.
- Public/API payloads should keep the existing account/repository vocabulary:
  `userQuota`, `repoQuota`, `billingUsage`, `repoUsage`, and quota scope values
  `user` and `repository`.
- Repository quota is scoped by repository, with forks sharing quota with their
  source repository when the source id is known.
- Repository scan quota uses a single global `quota.repositoryReviewLimit`
  value for all subscription plans and resets by UTC calendar month. Do not
  derive repository quota period, bucket plan, or limit from the requesting
  user billing cycle or subscription plan.
- A scan reserves both account and repository quota before queueing. Pi's
  `review` phase consumes both reservations; preparing, publishing, and retired
  semantic-phase names do not. A valid completed result also settles consumption
  at terminal ingest, including when optional intermediate events were absent.
  Release pre-review failure/cancellation reservations, preserve validated
  refundable failures, and keep replay aligned with both bucket ids.
- Billable-phase evidence and refundable-reservation rollback must be derived
  from durable job/run/event storage, not only the process-local scan mirror,
  so cold-memory restart paths consume or refund both quota buckets correctly.
- Billable core work remains billable after the latest progress phase advances
  to cleanup or another non-core phase. Check durable review-run progress,
  started/completed core steps, and historical indexed review-run events before
  releasing a reservation during cancellation or recovery.
- A prior `scan_reservation_released` ledger is audit history, not a permanent
  veto on later consumption when durable core evidence arrives. Replay must
  deterministically repair stale scan quota projections from existing release
  or consumption ledgers without changing bucket totals twice.
- UI/API copy should say account, user, repository, or repo; avoid introducing
  workspace unless referring to a local checkout/worktree in the generic
  filesystem sense.

## Performance And State Source Of Truth

The server is being moved away from full in-memory scan/issue traversal. Keep
new read and write paths aligned with the normalized SQLite tables.

- Browser scan payloads must preserve non-secret scan `requestId` values in create, detail, history list, and `/scans/status` responses. Web uses that idempotency key to reconcile batch scan handoff with Scan history while newly created rows propagate through pagination and status refreshes.
- `/scans`, `/issues`, scan detail, issue detail, status, and admin worker APIs
  should use DB-side `user_id` filtering, sorting, counts, and pagination.
  Hydrate only the current page or requested object.
- Do not reintroduce `user_scans_for_read()` or `user_issues()` as a first step
  for paginated routes. Those helpers are older bridge paths, not the scale
  path.
- Issue detail bridges may still need runtime fields from the matching
  in-memory `ISSUES` item, especially `pullRequest` and `pullRequestPending` in
  older tests. Merge those fields only after matching both `userId` and issue
  id, and do not let list routes expose PR state.
- `SCANS` and `ISSUES` are in-memory mirrors only. `persist_state()` must
  not write bulk scan or issue business data into `app_state`; app state should
  remain lightweight configuration/session state.
- Worker result payloads may be large. Store full reports/log-heavy payloads in
  result artifacts and keep the main job/result transaction to status,
  checksum, summary, and artifact references.
- Worker result routes accept gzip-compressed JSON bodies. Keep JSON decoding,
  body-size checks, and decompressed-size limits in sync when changing request
  parsing.
- Authenticated v1 worker gzip result/artifact uploads may exceed the public
  REST API compressed body limit; gate them with the worker decompressed-size
  limit instead. Do not broaden this exception to unauthenticated requests,
  browser routes, or identity/uncompressed payloads.
- Startup/recovery should be incremental by cursor/timestamp/job id. Avoid
  full reverse synchronization from all completed results back into memory.
- Worker/admin/status pages should use aggregate queries and short TTL caches
  rather than per-worker or per-scan loops.
- Issue/scan mutation routes must resolve authorization and current state from SQLite before applying updates; in-memory `SCANS` and `ISSUES` entries are optional mirrors and must not be required for preview or pull-request actions.
- Replacing a review artifact must remove the superseded content file after the replacement row is committed, while keeping all path deletion constrained to the configured artifact storage root.
- Billing webhook ordering timestamps must preserve sub-second precision when providers send milliseconds; do not truncate fractional event creation times before stale-event comparison.
- User deletion must remove that user's review runs, events, artifact rows, and artifact content files in addition to scan/job/issue records.
- User deletion must remove only that identity's user-scoped quota buckets and associated ledger rows while preserving repository-scoped buckets. Build the returned admin user payload before deleting buckets so quota serialization cannot recreate the deleted bucket.
- Quota finalization must fall back to the durable user scan snapshot for `requestId`, consumed state, and the persisted consumed update when the `SCANS` mirror is cold or evicted.
- Issue status read/replace writes and pull-request metadata writes must share `STATE_LOCK`; both persist full issue payloads, so an unlocked stale status write can erase a concurrently stored PR.
- Terminal worker-result reconciliation may replay stored findings during `/scans` reads. Preserve the database-backed user issue status (`open`, `fixed`, or `snoozed`) and its update timestamp when replacing those findings, and rebuild the optional `ISSUES` mirror from the records actually stored; raw worker findings must not reopen user-triaged issues.
- Public scan-system status must stay redacted, but fleet alert synchronization must receive an internal quota-bearing worker projection so complete-snapshot refreshes preserve quota incident grouping.
- Parse trusted `X-Forwarded-For` chains from the right and use the first address outside `PULLWISE_TRUSTED_PROXY_CIDRS` as the client identity. Never use the client-controlled leftmost entry or a trusted proxy hop for rate-limit subjects.
## Worker Upload Load Testing

Use `python ops/worker_upload_load.py --workers <n> --uploads <m> --concurrency <c> --operation heartbeat|event|artifact|mixed|lease --artifact-kib <k> --event-kib <k>` from `pullwise-server` to measure v1 worker control-plane and artifact upload throughput against a real local `ThreadingHTTPServer` and temporary SQLite DB. This is a server control-plane load probe, not a worker execution benchmark: simulated workers do not run Codex, clone repositories, analyze files, or perform real review work, and production workers are expected to be distributed across many machines. Interpret slow local probe results as pressure on server HTTP handling, auth, worker routes, and database writes unless evidence proves a client-side harness bottleneck. New v1 artifact uploads store content bytes outside SQLite under `PULLWISE_REVIEW_ARTIFACT_STORAGE_DIR` or next to `PULLWISE_DB_PATH`; `review_artifacts.payload_json` must not contain `content_base64`, and `content_path` is server-internal. Review-run artifact uploads should reuse the job resolved from the run id instead of fetching it again, and unique non-replaceable artifact rows should insert before duplicate/conflict probing so the common path avoids an extra `review_artifacts` read. Progress event ingestion should use `db.store_review_run_event_and_progress(...)` with `scan_job_progress` so the durable event insert, `review_runs` progress upsert, and scan-job progress update share one SQLite transaction before any scan mirror update. Active heartbeat persistence should use `db.record_active_worker_heartbeat(...)` with heartbeat progress arguments when progress must be persisted, so job update classification, worker heartbeat upsert, missing-job recovery, lease renewal, `review_runs` progress, and scan-job progress share one SQLite transaction; heartbeat progress may update the in-memory scan mirror, but should not do a separate inline `db.upsert_scan(scan)` on the hot path. Heartbeat alert sync should pass known running-job/latest-command values instead of hydrating admin-only worker payload DB fields. The July 2026 300-worker local probes now meet the short-term SQLite/ThreadingHTTPServer target of stable 300/300 under the default request timeout with p95 below 60s: heartbeat p50/p95 roughly 9s/18s, event roughly 20s/20s, artifact 32 KiB roughly 9s/19s, mixed 16 KiB roughly 19s/19s, and lease roughly 29s/29s. The largest common late bottleneck was `read_json()`/body-size limit selection calling `worker_token_record(...)` with token last-used writes before worker routing; body-limit/auth-size checks for worker requests should use `update_last_used=False` and leave token usage writes off hot-path request parsing. Temporary short-circuit probes should be used before further hot-path changes: bypassing the lease claim transaction or removing the claim Python `_LOCK` produced the large lease improvement; removing the Python `_LOCK` around active heartbeat, event, and artifact write transactions also produced large improvements; pre-body heartbeat no-op p95 was about 0.18s and pure no-op `ThreadingHTTPServer` with heartbeat-sized gzip bodies was about 0.12s, proving earlier 40s floors came from Pullwise handler work rather than Windows/urllib. Short-circuiting heartbeat token read locking, scan-job read locking, post-DB alert/log-session response work, active-heartbeat command polling, active-heartbeat missing-job recovery scanning, gzip, implicit SQLite `IMMEDIATE` transactions, lease payload construction, scan mirror dirty marking, and scan mirror object updates did not produce useful gains. Lease requests should skip presence rewrites for already claim-ready workers, refresh only the requesting worker when it would otherwise be offline, create `review_runs` inside the claim transaction, never run full recovery sweeps inline on the claim hot path, and let SQLite transaction semantics rather than the process-wide DB lock serialize concurrent claims. Treat this probe as the regression/operational check before increasing worker fleet size, artifact size, heartbeat frequency, progress-event frequency, or lease claim rate.

## Debug Bundle Contract

A debug bundle is not the audit bundle and must never silently fall back to the audit bundle.

- A real debug bundle combines worker-side live evidence and server-side evidence for the same scan/job/run.
- Worker-side evidence should include run-local logs, Codex app-server events, progress logs, run-state, phase outputs, terminal QA/error reports, and the worker artifact manifest. It must not include repository source files, raw API keys, unredacted environment dumps, or unrelated worker-instance state.
- Server-side evidence should include only scoped records for the same scan/job/run: scan/job/attempt/run identifiers, phase/progress/error snapshots, review-run events, artifact metadata/storage references, quota state, and relevant timestamps. It must not include full database dumps, secrets, other users' data, or unrelated scans.
- `server-debug-evidence.json.pipeline_diagnostics` must reconcile worker envelope main/weak/disproven/suppressed counts with persisted scan issue counts. Use its disposition and blocker codes to distinguish no issue-eligible worker findings from a server ingestion gap without reading raw artifact payloads.
- The UI must disable or omit debug bundle actions when no real debug_bundle artifact/server debug bundle endpoint exists. Do not substitute /scans/{scanId}/audit-bundle.zip as a debug zip URL.
- Tests should protect this contract: missing debugBundleUrl must not produce an audit-bundle URL, and server/worker tests must verify failed runs still expose a real debug_bundle artifact or explicit absence.

## CI Test Harness Notes

- Server CI must check out Server and the exact tested Worker revision into
  sibling `pullwise-server` / `pullwise-worker` directories, provision Node
  22.23.1, and install the Worker's locked graph with scripts disabled before
  pytest. The Gateway TLS integration invokes the actual Node Worker; a local
  four-repository checkout does not prove standalone CI has that prerequisite.
- Gateway app test master-key fixtures must set mode 0600 explicitly. A normal
  POSIX umask creates 0644 files; preserve the production permission rejection
  and exercise the fixture under umask 022 on Linux.

- `tests/test_agent_first_source_fixture_global_gate.py` runs one large generated
  Node fixture process that can exceed ten minutes even when its semantic
  sub-gates are green. Keep the test's internal subprocess timeout at 1,800
  seconds or higher and run it without competing Node-heavy suites; increasing
  only an outer pytest/command timeout cannot override that internal limit.
- `app.main()` constructs `PullwiseThreadingHTTPServer`, not the stdlib `ThreadingHTTPServer` symbol. Tests that call `app.main()` must patch `app.PullwiseThreadingHTTPServer` so they do not start a real `serve_forever()` loop in CI.
- Scan request IDs are globally idempotent per requesting user, not per repository. Quota reservation must atomically detect the same user/request ID across repositories so concurrent requests cannot reserve twice; route code decides whether the existing repository is a dedupe or `IDEMPOTENCY_KEY_REUSED` conflict.
- Persisted issue row IDs must be globally collision-safe across scans; raw
  worker finding IDs are source identities and may repeat in different runs.
- `git-watch.sh` single-instance exclusion must use an OS-held lock such as
  `flock`; a stale directory left by a crash must not block updates forever.
- A Git watcher deployment is successful only after setup, tests, server
  restart, and health checks complete. Publish the full commit and completion
  time atomically to `.pullwise/git-watch.status.json` only then. The admin
  deployment endpoint must compare that commit with the server process's
  startup commit and report verified only when they match.
- Do not expose an authenticated Admin endpoint for restarting Pullwise Server.
  Production restart ownership belongs exclusively to the Git watcher and
  systemd deployment lifecycle.
- Ubuntu 22.04 production Git polling uses the optional
  `pullwise-server-git-watch` systemd unit installed by
  `./launcher.sh install-watch-service`. Keep its target branch pinned to
  `main`, reject a checkout on any other branch, and use journald rather than
  an unbounded watcher log file.
- V1 heartbeat payloads must contain `active_run_id` explicitly, including `null` while idle. Terminal wrapper status maps exactly to execution status: `done/completed`, `failed/failed`, `cancelled/cancelled`, and `partial_completed/partial_completed`.
- Preserve worker validator disposition when constructing issues: plausible stays `potential_risk`; confirmed static evidence is `static_proof`; only confirmed dynamic evidence with a command plus output/log may become `verified`. Audit bundles include redacted `intent_test_output` artifacts and localize Markdown to the scan output language.
- Rendered `observation/v1` facades must parse exact millisecond UTC instants
  with calendar validation and platform-independent epoch arithmetic; valid
  pre-1970 instants must behave identically in Python and Node. Actor semantics
  use the current `domain_reviewer` kind and must not restore its legacy alias.
- Rendered `change-set-patch/v1` facades must check canonical padded base64,
  decoded size, and byte SHA-256 before `patch_digest`. Compute that digest over
  `pullwise:change-set-patch/v1`, a NUL separator, and canonical unsigned
  document bytes, with identical Python/Node error code, detail, and path.
- Generated GatePreparation facades expose
  `verify_terminalization_fact_context(fact, task_id, current_task_version,
  lifecycle_state, existing_fact=None)`; Node also exports
  `verifyTerminalizationFactContext` plus the snake alias. It verifies the
  fact digest, exact task/version binding, a nonterminal request lifecycle, and
  canonical equality for a reused idempotency key.

## Agent-First Current Implementation Boundary

- D31 (`d6fe7e5184e410aa6d034be1b593c8bf83126d5af300ea489a3d077642b42254`)
  makes Server acceptance the sole deadline authority. Persist `accepted_at`,
  compute `absolute_deadline_at` exactly once as
  `accepted_at + effective_policy.budgets.wall_ms`, and persist the accepted
  `terminalization_reserve_ms`. Claim must copy both values verbatim into
  `agent-worker-grant/v1` and `server-authority-envelope/v1`; neither Server
  claim time nor Worker recovery may recompute or extend them.
- D32 (`11794116e7db5fdb330e001fa1ab7b7039ff1f1f04bc3283b9cddbc30bf3995e`)
  requires an independent `transport-abandonment-record/v1`. Its canonical
  bytes/digest are distinct from `agent-claim-abandon-response/v1`, which stays
  the fenced successor authority response. The same idempotent transaction
  seals both; abandonment never terminalizes a Task/TaskResult or binds a
  transport receipt.
- D33 (`8bf9ed4ac35fdd2f0bfd790c1a8f8879776a44711683152921c9ae330e105fb4`)
  requires one mechanical terminal selector over exactly `profile`,
  `gate_mode`, `cancel_state`, `effect_state`, `cause_family`, and
  `delivery_state`. Reject caller-selected outcomes/reasons and tombstoned or
  delete-fenced inputs. Unknown effects stay `RECONCILING` before deadline and
  become `TERMINATED_WITH_UNKNOWN_EFFECTS` at/after it; cancel plus committed
  effects becomes `CANCELLED_WITH_EFFECTS`. TaskResult CAS binds the complete
  selector-input digest.
- D34 (`2be5b5752b65714204fa6f41a0a126eb30e82bafcdeb38b5ece426938561158c`)
  limited the original work to one unactivated candidate. Its exactly-once
  Generate allowance was consumed, and D35 withdraws that old candidate tuple;
  never restore or fall back to those prior package bytes.
- D35 (`8cde7af149db8e6051f0342bd9490c4be31fce7b1868270ce7206350ee252a9e`)
  supersedes only D34's Generate limit. It authorized exactly one replacement
  after every source, fixture, semantic closure, DAG, registry, digest, and
  Python/Node parity pre-generation gate was green. That allowance is consumed
  by package `@pullwise/agent-task-contract@0.1.0`, content
  `11ced3caa5333f5d841a5f5d0ca33e9a91522f9809cd23943f56d1f371409564`,
  and root `e6dc056cb1b61c2a47c28d3e02117352bae35c7fecb07d10bad6afd65b9e194e`.
  D37 withdraws this historical tuple; never restore or repin these bytes.
- D36 (`cb40a540cff9af1d350bf1a413aa3aeaee0ca1ddce65afabec7443f294944a1b`)
  supersedes D35's candidate-only implementation boundary. It authorizes local
  repository and CI implementation/verification of S3-S7, including the
  authenticated current-task/operator HTTP seam, but no contract source
  change or Generate. It does not authorize production activation, D24
  implementation or enablement, deployment, changes to a deployed Worker,
  production traffic, canary, legacy deletion, or S8 release/cutover/rollback.
- D37 (`ae16d63b19bcd6ec81c65daf1668a3bf8878210aed137a59761ca9b36f96aa70`)
  is resolved to `bounded_s4_contract_closure_one_generate_no_activation`.
  Server-owned source now contains versioned `agent-task-accept-request/v1`,
  atomic `agent-task-runtime-bootstrap/v1`, `machine-checkpoint/v1`,
  `semantic-checkpoint/v1`, and `committed-checkpoint-manifest/v1`, with
  source fixtures, document semantics, contextual chain verification, registry,
  DAG, digest, and focused Python/Node parity coverage. Every required
  pre-generation gate passed. The D37 Generate allowance is consumed exactly
  once (count `1`) by package `@pullwise/agent-task-contract@0.1.0`, content
  `9dfa928d1a2d139036701b7d69354e6e4ceb16b9fa5d913fc77cd6fd823454fb`,
  root `4a37e789495b8b22d102ef1e87110b8e28abf555fa30bcd5baa1a2568d4b22ef`,
  and generated-artifact commit
  `a223f1ffdee345da366ab7c3bf8ca230ad7f39cb`. Server, Worker, and Web must
  exact-pin these bytes. Do not Generate again without another append-only
  superseding decision, and do not hand-edit generated files, activate
  production, implement or enable D24, deploy, canary, delete legacy, or begin
  S8 cutover/rollback.
- D38 (`d1cbc20e4220c6d073d01a060cce1ae2f109459e0c110d4e403c41ecd0303368`)
  is resolved to
  `bounded_s5_terminal_control_and_selector_closure_one_generate_no_activation`.
  It supersedes only D37's consumed Generate boundary and authorizes the
  bounded S5 contract closure: versionedly bridge a passed Success Gate into
  the sole mechanical six-axis terminal selector without requiring or
  fabricating a terminalization fact; bind immutable Server grant/authority to
  the local checkpoint/control-event Task-version chain; and define one real
  `FINALIZING -> TERMINAL` TaskResult CAS that atomically binds
  `published_from_version=N`, `terminal_task_version=N+1`, the selected result,
  transport/result/version/fence closure, and exact replay. Every required
  pre-generation gate passed, and the D38 Generate allowance is consumed
  exactly once (count `1`) by package `@pullwise/agent-task-contract@0.1.0`,
  content `51445b46d40b1c61387edfa3a4bd68e18fa388e7ac2139c45e870a3a6a3cc29d`,
  root `76b6c450fecacc5209cfc426c337134c0f8a7361c830d9d17103160d746233d9`,
  wrapper SHA-256 `c88455efd633746a34c8833e015d26ca4cd1beb5add4eb2cdd711ffeb7ce48d0`,
  package-manifest SHA-256
  `926b673652924591adb85ed7dbf72495ab113f0e9aa8b2381b5e28bf470e65df`,
  and Server generated-artifact commit `5048af9`. Server, Worker, and Web exact
  pins are synchronized. Do not Generate again without another append-only
  superseding decision, and do not hand-edit generated files.
  D24 implementation or activation, deployment, deployed-Worker changes,
  production traffic, canary, legacy deletion, S8, fallback, dual path,
  compatibility/downgrade shims, and a second authority/store/runner remain
  forbidden.
- D39 (`85365d344a6bc0d36d5d11dbc088278722083bf51e98c9cd518dd3d57ac90f9c`)
  is resolved to
  `bounded_s7_transport_attempt_binding_one_generate_no_activation`. It
  supersedes only the pre-D39 S7 `SPEC_GAP` and authorizes bounded local S7
  closure: authenticate the Server-issued outer `transport_attempt_id` claim,
  capture the worker debug fragment/descriptor variants, enforce replay,
  conflict, concurrency, redaction, and bounded extraction behavior, and add
  migration 9 with schema-v9 fingerprint
  `028cc25005ce33dd7b16017fe7e5324774205b0b603f2e2582e9930511065e6a`.
  Exactly one Generate was consumed (count `1`) for
  `@pullwise/agent-task-contract@0.1.0`: content
  `35468e289dd08a2b9a91b5c7ffb589f844c4373cfedd8f3846cc40dd1e8f6105`, root
  `ff6fce2d8a0d28adeb880b97ebfaa6037fa0503eb7c1accd68e840994add43b1`,
  Python wrapper `bd099dd825c2b2340061b67500bc02f1bb4fee0a1ce7ff44138b36b8821a59fd`,
  npm wrapper `4027cf1383772871efa293a1c55338e96e17d5c0387efd84d059585cdce6c0ef`,
  and package manifest
  `161c7d7bef846de963a491f2d9f07f9cbc1ced039a3c017467d6f02f14b1925e`;
  Server producer commit is `06ed22299e324a8a39f9030c653aef34044c3d3e`.
  Server, Worker, and Web exact pins match. This remains local candidate-only
  with no activation; do not Generate again, activate D24, deploy, send
  production traffic/canary, delete legacy paths, add fallback/dual/
  compatibility/downgrade shims, create a second authority/store/runner, or
  begin S8.
- D40 (`ce023be3c467370077f967bb7e17e9dee2064ea1fbe213a7a97b6815aec6cdc9`)
  is bound to `bounded_s8_offline_candidate_no_generate_no_activation`. Its
  audit proved that aggregate reports and caller-supplied observations could
  not reproduce raw samples, exclusions, rates, or the Wilson bound; D40
  recorded that package gap and stopped with zero contract changes or Generate.
- D41 (`ccfa987beb48b1158a0122b13ed6bab40bd955e5142fbd1e0fc56f7151b1cca5`)
  is bound to
  `bounded_s8_raw_evidence_contract_one_generate_no_activation`. The approved
  public closure adds canonical `release-gate-sample-set/v1`, exact report
  ContentRef/digest binding, and
  `derive_release_gate_evaluation(benchmark_bundle, policy, sample_set)`.
  Deterministic sample identity/order, exclusions, denominators, integer
  rounding, Wilson 95%, profile consumption, p95, and tri-state derivation are
  Server-owned with Python/Node parity. All source, fixture, semantic closure,
  DAG, registry, digest, signature, and parity pre-generation gates passed.
  The exactly-one Generate allowance is consumed at count `1` by package
  `@pullwise/agent-task-contract@0.1.0`, content
  `501ed1be77f96f5a00f1fb9cfd59da64ca46b317f117e8661af7d7948a9374e4`,
  root
  `1a79eb16f24c0390b9916255db251e7f607f5c23cc3a1afec5de4a63fb155ed9`,
  Python wrapper
  `9404c18b39afdb0ee6bd9d15fdbb3b24d9b85f1972a597a5919a868afe480697`,
  npm wrapper
  `76c92a690b35f8c676e391925dd8377ac731a880326fc729360cb122cdeca959`,
  package manifest
  `11f3400110578cbd2e5716f18c41318cc1db7ed6ce728160c5335ee160bddc94`,
  and Server producer commit
  `69c5fcc8964398b2ad156c6d8219230fce908715`. Server/Worker/Web exact
  generated bytes and pins are synchronized; do not Generate again without a
  new append-only superseding decision. Continue only local/offline
  report-builder TDD. D24 implementation or
  activation, deployment, deployed-Worker changes, production traffic, real
  benchmark/signing/attestation/release, canary, cutover, legacy deletion,
  fallback/dual/compatibility/downgrade paths, second authority/store/runner,
  and codegraph remain prohibited.
- D42 (`b2710ca7cbfd19cd70721a54cf18e5674dd4eadb10f2625728eb79fec4a422c3`)
  is bound to `bounded_python_facade_cache_one_generate_no_activation`. It
  supersedes only D41's consumed Generate boundary to cache the decoded bundle
  and internal schema/fixture lookup in the generated Python facade while
  keeping public bundle, root-manifest, schema, and fixture values detached and
  safe for caller mutation. The exactly-one Generate allowance is consumed at
  count `1`; package `@pullwise/agent-task-contract@0.1.0`, content
  `501ed1be77f96f5a00f1fb9cfd59da64ca46b317f117e8661af7d7948a9374e4`,
  and root
  `1a79eb16f24c0390b9916255db251e7f607f5c23cc3a1afec5de4a63fb155ed9`
  remain unchanged. The Server and Worker generated Python wrappers are
  byte-identical, LF-only, and SHA-256
  `b80384b663667fb041819f5c94d83e7f018264f9732569d394e8289512b3299f`;
  published bundle, root manifest, npm wrapper, and npm package bytes remain
  unchanged. Do not Generate again, hand-edit generated outputs, change
  contract sources or schemas, activate D24, deploy, send production traffic,
  canary, cut over, delete legacy paths, add fallback/dual/compatibility/
  downgrade paths, or create a second authority/store/runner.
- D43 (`703631e3aa2e1b05dfc2f4ba1d3f9e6607e1d53116f39969abe7f705f31a202c`)
  is the append-only
  `bounded_public_error_code_oneof_cache_one_generate_no_activation` repair.
  `_public_error_code` must read `error_golden_current_registry` from the
  module-owned parsed cache so expected failures of a valid `oneOf` never
  decode or parse the embedded bundle again. D42's Generate remains consumed
  at count `1`; D43's exactly-one Generate is also consumed at count `1`.
  The Server and Worker generated Python wrappers are byte-identical, LF-only,
  and SHA-256
  `956e2db9ea8fb81bce50e549656b44e6a28793597133693c0073210b171f2a8d`.
  Package `@pullwise/agent-task-contract@0.1.0`, content
  `501ed1be77f96f5a00f1fb9cfd59da64ca46b317f117e8661af7d7948a9374e4`,
  root `1a79eb16f24c0390b9916255db251e7f607f5c23cc3a1afec5de4a63fb155ed9`,
  and the published bundle, root manifest, npm wrapper, and npm package bytes
  remain unchanged. Do not Generate again or hand-edit either wrapper.
- D44 (`ac083eb8c4ef4f7e98dc7590d197d9499544eaaf762a34fc0c2ff18495690d1d`)
  is the append-only
  `bounded_public_error_code_missing_registry_default_one_generate_no_activation`
  repair. A rendered minimal bundle without
  `error_golden_current_registry` must retain the public
  `CONTRACT_DOCUMENT_INVALID` default without decoding or parsing the embedded
  bundle again; catch only the missing internal registry lookup and preserve
  all other validation failures. D42, D43, and D44 Generate counts are each
  consumed at exactly `1`. The Server and Worker generated Python wrappers are
  byte-identical, LF-only, and SHA-256
  `5142895ac6d9b2a6933ad1b5e9abdaf8efcebc453d502adae87bd55aaf3f8329`.
  Package `@pullwise/agent-task-contract@0.1.0`, content
  `501ed1be77f96f5a00f1fb9cfd59da64ca46b317f117e8661af7d7948a9374e4`,
  root `1a79eb16f24c0390b9916255db251e7f607f5c23cc3a1afec5de4a63fb155ed9`,
  and the published bundle, root manifest, npm wrapper, and npm package bytes
  remain unchanged. Do not Generate again or hand-edit either wrapper.
- The preceding S7 `SPEC_GAP` text is historical pre-D39 guidance. Native
  Attempt ID substitution remains forbidden, and all D39 work stays within
  the bounded no-activation boundary.
- Python nested semantic helpers must preserve Node's single-dispatch behavior.
  For an already nested document, call `validate_document` once and verify its
  embedded digest directly; do not call public `verify_document_digest` from a
  semantic helper because its public validate-plus-digest path dispatches
  document semantics twice. Effective-policy derivation likewise validates its
  nested policy once. Keep the global fixture semantic-hit trace equal across
  Python and Node.
- The tri-state release evaluator candidate consumes
  `benchmark-bundle/v1`, `release-gate-policy/v1`,
  `release-gate-sample-set/v1`, and `release-gate-report/v1`.
  `derive_release_gate_evaluation` derives every reported metric, result,
  verdict, and exit code from exact-bound canonical samples;
  `evaluate_release_gate` re-derives and compares the report. FAIL takes
  precedence over INDETERMINATE, and caller-selected observations or result
  status are rejected with stable fail-closed errors. It is
  source-only logic: no evaluator storage, signing/trust, organization
  principal, baseline/canary runtime state, external evidence, production
  HTTP/auth, Worker-loop activation, D24, deployment, or canary is implied.
- Durable release-evaluator storage is Server-only and current-package-only.
  `db.initialize()` installs exactly four normalized append-only tables for
  benchmark bundles, release-gate policies, release-gate sample sets, and
  release-gate reports. Before a report can be persisted, one
  `BEGIN IMMEDIATE` transaction must insert or exactly replay the canonical
  benchmark/policy pair. The later evaluation transaction must require that
  exact pair and atomically insert or replay the exact-bound sample set and
  report; it must never backfill missing inputs. Input-stage faults roll back
  both frozen rows, while evaluation-stage faults preserve the already-frozen
  pair and roll back both sample set and report. A freeze is fresh only when
  neither row exists and is an exact replay only when both rows match. Any
  one-sided pair state is
  `AUTHORITY_RELOAD_REQUIRED`; neither freeze nor report persistence may repair
  or reinterpret it as caller input.
- Keep each document's domain-separated digest distinct from the SHA-256 of
  its canonical bytes. Persist and verify both, plus exact byte size, runtime
  package tuple, normalized link digests, ContentRef SHA/size, and report
  verdict/exit-code pairing. A valid new document with a reused stable id is
  `IDEMPOTENCY_CONFLICT`; a same-digest mismatch or corrupt/missing linked row
  is `AUTHORITY_RELOAD_REQUIRED`.
- Evaluator storage does not persist attestations, signatures or trust state,
  principals or organizations, mutable baseline/canary state, external
  evidence, HTTP/auth state, or Worker/Web activation.
- The separate Server-only release trust candidate validates real Ed25519
  signatures for benchmark bundles, release-gate policies, and release-gate
  attestations. Trust roots are accepted only through an externally supplied,
  Server-local durable organization-to-root-digest pin set; stored root
  documents never bootstrap their own trust.
- Root-pin enrollment accepts only the explicit `(organization_id,
  root_digest)` tuple and must happen before authority registration. Pin rows
  are immutable, exact-replay idempotent, and serialized with `BEGIN
  IMMEDIATE`; every chain validation rereads the durable pin. A missing pin is
  `AUTHORITY_INPUT_UNTRUSTED` for fresh registration and
  `AUTHORITY_RELOAD_REQUIRED` when replaying stored authority state.
  Transient SQLite `BUSY`/`LOCKED` failures remain operational errors and must
  not be mislabeled as storage corruption.
- `db.initialize()` also installs normalized append-only tables for release
  root pins, trust roots, organization principals, signing keys, key
  revocations, and verified release-gate attestations.
- Root-to-principal-to-key registration is one `BEGIN IMMEDIATE` transaction
  with exact replay, stable-id conflict, immutable-row, corruption,
  missing-link, and normalized-metadata closure.
- Principal roles bind exact purposes: `benchmark_owner` to
  `benchmark_signing`, and `release_operator` to `release_signing`. Key
  rotation appends a new root-signed key; root-signed revocations block a key
  only at or after `effective_at`. Organization, principal, purpose, package,
  ContentRef, domain digest, canonical SHA/size, validity window, and signature
  bindings all fail closed.
- A `release-gate-policy/v1` validity window must satisfy
  `0 < expires_at - issued_at <= 30 days`. Exactly 30 days is valid; any longer
  window fails document validation and the public input-freeze boundary as
  `AUTHORITY_INPUT_UNTRUSTED` without persisting evaluator inputs.
- `AgentFirstReleaseAttestor` composes, but does not change, the deterministic
  evaluator. Its input-freeze facade verifies benchmark and policy signatures
  at one Server time, requires the same organization and distinct benchmark
  owner/release-operator principals, then atomically freezes their exact
  canonical pair. Attestation later rechecks benchmark/policy/attestation
  signatures at one time, accepts only an exact PASS/0 report binding against
  that pre-existing pair, persists the attestation, and reloads by rerunning
  evaluator, context, canonical-byte, link, and signature checks at the stored
  `verified_at`.
- This candidate does not provide private-key custody, authenticated or remote
  root-pin enrollment orchestration, unpin or automatic root rollover,
  external key distribution, HTTP/auth exposure, mutable baseline/canary
  state, external evidence, Worker-loop activation, D24, deployment, or
  canary.

## Agent-First Gate Decision Semantics

- Generated GateDecision facades treat the sealed
  `gate-predicate-registry/v1` fixture as the exact ordered contract. Every
  predicate result occurs in branch order, uses only that predicate's failure
  codes and input-schema evidence, satisfies
  `passed == (failure_code is null)`, and contributes to the decision-wide
  conjunction.
- `evaluate_success_gate(snapshot, context)` and
  `evaluate_terminalization_gate(snapshot, context)` are closed-world,
  deterministic aggregators. They bind an exact validated snapshot ContentRef
  and seal supplied predicate results; they must not invent live predicate
  booleans. Full live evaluation requires a typed predicate-evaluation input or
  resolver contract carrying resolved reference content plus clock, lease,
  writer, and tool facts, which the current source schemas do not define.

## Agent-First Quality Policy Semantics

- Generated QualityPolicy facades enforce the exact six-field input digest,
  fixed Q1/Q2/Q3 slot table, strict `plan_digest`, and opaque rationale. The
  context helper binds a plan to proposal, policy, request, ledger, and change
  set; enforces the risk floor; permits only active ledger requirement ids; and
  requires every active mandatory id in every Q1/Q2 slot while Q3 remains
  zero-slot unsupported evidence.
- Public `document_digest` / `documentDigest` helpers compute the digest first
  and validate the real sealed document. They must never validate a synthetic
  all-zero digest.

## Agent-First Pre-Gate Input Semantics

- Rendered pre-gate/gate-input helpers use direct-document APIs:
  `(root_set, task_id, outcome_candidate)`, `(manifest, root_set)`,
  `(snapshot, root_set, pre_gate_manifest)`, and the terminal variant plus
  `terminalization_facts`. They verify canonical references, digests, and
  projections; do not invent a resolver callback. Full transitive PreGate CAS
  byte proof requires a future explicit ref-to-content binding contract.
- `pre-gate-root-set/v1` currently has no `quality_policy_plan` field even
  though gate input requires `quality_policy_plan_ref`. Until the schema adds
  a direct projection, helpers can require that reference's closure membership
  but cannot bind it to a root-set field.

## Agent-First Bundle DAG

- The root DAG records `task-record/v1 -> task-result/v1` at
  `$.properties.result_ref.oneOf[0]` but excludes that optional post-gate edge
  from cycle traversal. GateDecision may cite the pre-terminal TaskRecord while
  final EvidenceClosure cites GateDecision; this exception must never admit a
  TaskResult, TaskResultCore, transport, or debug artifact into either evidence
  closure.

## Agent-First Generated Artifact Semantics

- Generated Agent-First publication artifacts are exact-byte test fixtures. Keep contracts/agent-first/current/published/*.json, generated/agent-task-contract-npm/index.js, generated/agent-task-contract-npm/package.json, and pullwise_server/_generated_agent_task_contract.py pinned to LF line endings; CRLF working-tree rewrites make write_generated(..., check=True) and wrapper lock tests fail.
- The generated NPM facade must cache decoded bundle bytes and the parsed bundle object. Schema/$ref lookup must not decode and parse the embedded bundle for every validation call; expose byte copies and a deeply frozen parsed cache so public helpers remain mutation-isolated; keep the direct repeated-helper regression in tests/test_agent_first_contract_bundle_npm_ordering.py.

## Agent-First Result, Debug, And Transport Semantics

- `TaskResult.diagnostics.worker_debug_fragment.ref` targets the
  `worker-debug-fragment-descriptor/v1` document, never the fragment document.
- Derive a worker debug fragment id from the validated file manifest document's
  `manifest_digest`; the manifest ContentRef's `sha256` is not that identity.
- Keep exact descriptor and receipt contextual binding behind storage/replay
  ordering so `IDEMPOTENCY_CONFLICT` and receipt-binding error precedence remain
  stable.
- The TaskResultCore projection removes only
  `diagnostics.worker_debug_fragment`; preserve every other TaskResult field.
- Semantic closure derives source-declared document rules/contextual helpers
  dynamically (currently 79/40) and checks them against the supported registry
  superset (currently 85/41). Never hard-code closure counts.
- Closure is two-layer: each positive schema hits exactly its declared rule
  sequence in Python and Node, and test-only direct probes invoke every rule
  handler because structural validation can preempt semantic dispatch. Never
  publish probes as exports.
- Test parity may recursively normalize Python bytes and Node `Uint8Array` to a
  hex marker only in the harness.
- Source-fixture `expected_code` is the raw-document expectation only when raw
  validation rejects. Explicitly enumerate schema-valid contextual negatives
  that are raw/digest-valid; specialized authority/context entrypoints own
  their stable codes. For example,
  `receipt_negative_transport_as_local` is raw `CONTRACT_DOCUMENT_INVALID` but
  authority validation returns `TRANSPORT_RECEIPT_TYPE_INVALID`.
- Internal-constraint fixtures must reject exactly through the internal schema
  ID, and every golden document must also execute successfully through its
  public parent schema.

## Agent-First Task Control Semantics

- Compose the specialized task-control rule/helper fragments after legacy
  control fragments so their complete handlers own overlapping rule names.
  Every declared rule must execute through both generated facades, and every
  contextual helper must expose Python snake case plus Node camel and snake
  names.
- JSON Schema object keywords remain active when a subschema omits
  `type: object`. The generated Python and Node evaluators must process
  `properties`, `required`, and `additionalProperties` for object values;
  otherwise conditional `oneOf` branches match vacuously.
- A TaskRecord may carry terminal result fields only in `TERMINAL`; preterminal
  result refs are invalid. Context helpers validate their owned TaskRecord
  documents and bind bounded external documents directly without claiming the
  external family's still-unimplemented semantic rules.
- `waiver-event/v1` has a sealed `signature_contract` in addition to its
  rule/helper lists. Both generated facades must require the exact Ed25519,
  NUL-separated, base64url-no-padding contract before authority evaluation.
- `authorized_waiver_issuers` is currently empty and no keyring/authority
  material is modeled. Keep waiver authority fail-closed: use
  `WAIVER_ISSUER_NOT_AUTHORIZED` for an otherwise current event and
  `WAIVER_TIME_INVALID` for an out-of-window event; do not invent an issuer.

## Agent-First Source, Execution, And Observation Semantics

- Generated facades execute `change_set`, `execution_state_manifest`,
  `source_selection_policy`, `source_tree_manifest`,
  `observation_manifest`, and `pre_verifier_observation_manifest` in both
  runtimes. Keep their embedded digest, ordering, identity, and shape checks in
  parity.
- The public direct-document helpers are
  `verify_change_set_context`, `verify_execution_state_context`,
  `verify_source_tree_context`, and
  `verify_observation_manifest_extension`; Node also exposes camel-case names.
  They bind exact document bytes and ContentRefs rather than accepting opaque
  resolver callbacks.
- A final observation manifest must strictly extend the exact pre-verifier
  entry prefix, bind its pre-manifest ContentRef, and add only
  `quality_verifier` entries. The source golden final fixture carries a
  placeholder pre-manifest ref, so positive contextual tests must rebind that
  ref to the actual pre-manifest bytes and reseal the final document.
- Actor semantics accept the current `domain_reviewer` kind and must not
  restore `legacy_domain_reviewer`.

## Agent-First Verification Semantics

- The five verification context APIs accept supplied immutable direct documents and exact canonical refs/digests. They cannot prove live CAS presence, current lease/fence/authorization/model use, in-flight tools, or future source stability. Proposal/work lack transitive cryptographic manifest closure, and the final observation manifest lacks `created_at`.
- With `oneOf` object roots and `additionalProperties: false`, declare the union of branch properties at the root while keeping every branch closed.

## P5a detail reads and incremental thread continuation

- `publish_assessment_result` also accepts `item_id=None` with no Item revision
  or action labels. Source-context publication, frozen coverage/evidence,
  assessment cache, reservation consumption and claimed-job completion remain
  one transaction; never create an Item solely to persist an irrelevant result.
- Source detail exposes assessments/evidence inside each authorized context.
  Source lists expose only relevance/updateSignals; never infer them from Items.
  Recheck every saved source and authorization/context dependency in one read
  snapshot. Analysis disable alone preserves unchanged published results.
- Updates release projection uses complete v3 question groups and the existing
  offline candidate threshold (0.8), not a validated model-quality claim.
  Entirely irrelevant releases have null signals; partial coverage cannot give
  negative release conclusions. Freeze coverage with the published assessment.
- Restrict and filter Sources at context granularity. A watch restriction must
  not expose sibling watch assessments. Shared contexts use targetRepositoryId
  and satisfy both target and watch restrictions when supplied.
- `tests/export_source_contract.py` emits fresh SQLite/shared-handler DTOs for
  the sibling Web test. This is local contract integration, not HTTP/Cloudflare
  end-to-end or a real model evaluation. Use the existing Python test environment.
- The CF probe's `/domain/*` routes use a deterministic clock and synthetic
  single-job protocol. Its local D1 lease/fencing/publication/retry checks are
  not a production ProductStore mapping or upstream provider response-loss
  injection. The protocol domains remain separate.
- The isolated `/attempt/*` D1 probe validates UTC month rollover plus
  owner/global monthly and rolling admission after actual local workerd restart.
  Its counters are separate from the domain-job probe, so full production
  admission and D1 mapping remain unproved.
- A public httpx2 BaseTransport injected into fixed SDK 0.7.0 locally bounded
  decoded response bytes and slow-body wall time in the Python Worker. Local
  loopback showed connection close and a successful next request. This uses a
  synthetic 2-second limit and does not pass the production 90-second,
  real-model-quality, account/payment, or remote-platform gates.

- Product-v1 Source detail exposes the current saved content; Source lists do
  not load/return bodies. Item detail exposes handlingHistory in insertion
  order; lists omit it. Filter current authority before returning either.
  Item/current handling/history reads share one SQLite read snapshot.
- GitHubPRReader thread-enabled scheduled scans persist nested GraphQL
  continuation and bounded matched-comment IDs in the normal discovery cursor.
  Drain the GraphQL connection before advancing that REST inline-comment page.
  Do not republish unknown state over already-matched comments on later pages.
  GraphQL unavailability preserves the checkpoint and Retry-After.
- Thread-enabled cursor scope differs from REST-only scope; never reuse or
  silently reset a live checkpoint when toggling the adapter. This remains
  incremental coverage, with GraphQL read amplification per REST page, not an
  atomic full snapshot, deletion proof, formal-review history, or semantic
  thread aggregation.
- cloudflare/probe is an isolated, approved local experiment, not an application
  entrypoint. Keep its venvs/runtime/vendor/cache ignored and main dependency
  manifests unchanged. It proves local workerd imports, a small D1 two-scope
  admission/rollback case, scheduled ticks and restart persistence only.
- Fixed SDK 0.7.0 passed imports on local Python Workers; its CPython loopback
  100ms inactivity timeout did not stop a continuous slow body within 2s.
  Preserve the separate target-runtime hard-exit gate and keep Jev disabled.
