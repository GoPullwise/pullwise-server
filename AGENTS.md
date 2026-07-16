# Pullwise Server Agent Notes

## Worker Host Platform

Pullwise worker installs target Ubuntu 22.04 hosts. Installer generation and
worker lifecycle changes may assume Linux/systemd behavior available on Ubuntu
22.04, including `useradd`, `chown`, `chmod`, `sudo`/`runuser`, logrotate, and
systemd unit management. Do not add macOS or Windows worker installer behavior.

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
- Public/API payloads should keep the existing account/repository vocabulary:
  `userQuota`, `repoQuota`, `billingUsage`, `repoUsage`, and quota scope values
  `user` and `repository`.
- Repository quota is scoped by repository, with forks sharing quota with their
  source repository when the source id is known.
- Repository scan quota uses a single global `quota.repositoryReviewLimit`
  value for all subscription plans and resets by UTC calendar month. Do not
  derive repository quota period, bucket plan, or limit from the requesting
  user billing cycle or subscription plan.
- A scan reserves both account and repository quota before queueing. Reserved
  quota becomes consumed only after a v1.2 core review phase starts, currently
  `repo_map`, `risk_routing`, `reviewer_fanout`, `clustering_and_voting`,
  `validator_disproof`, or `final_report_json`; do not use the legacy `ai`
  phase as a quota-consumption trigger. Release the reservation when a worker
  never reaches a billable core review phase. Keep idempotency and rollback
  paths aligned with both bucket ids.
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
