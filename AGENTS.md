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
  - `PATH` with this worker's `$WORKER_RUNTIME_ROOT/.local/bin`, `$WORKER_RUNTIME_ROOT/.codex/bin`, `$CODEX_HOME/bin`,
    before the base service path
- The installer should create the per-worker config/cache/auth directories under
  `$WORKER_RUNTIME_ROOT`.
- The installer-time readiness output and a later `doctor` run with no
  intervening manual action must agree. `doctor` must not appear ready because
  it sees root/global auth or another worker's provider config.

When changing worker installer generation, keep multi-worker deployments in
mind: every worker on the same server must use only its own configured Codex
directories.

## Worker Codex CLI Concurrency

Never configure or schedule a single worker identity to run multiple Codex
agent CLI processes concurrently.

- Treat worker capacity for Codex jobs as permanently fixed at `1`.
- Do not expose, persist, or route configurable worker job parallelism,
  max-claim, or worker-side job queue controls. The server owns the scan job
  queue; each worker claims a new job only after finishing the current job.
- The failure mode is correctness, not just load: concurrent Codex agent CLI
  processes can refresh the same auth token/session at the same time and
  invalidate `auth.json` or stored credential state.
- Do not change claim payloads, worker capacity, plan policy, or server-side
  scheduling in a way that lets one worker launch parallel Codex agent CLI runs
  under the same auth identity.

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

## Worker Delete Lifecycle

Admin Delete instance is not complete when the worker disappears from the server
registry or admin list. Deleting a worker instance must also remove the
worker-host resources associated with that instance: service unit, wrapper,
logrotate entry, `/etc` config, service user when safe, instance `DATA_DIR` under
`/var/lib/pullwise-worker`, instance `LOG_DIR` under
`/var/log/pullwise-worker`, and any other instance-scoped runtime files.

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

A single worker host may run multiple Pullwise worker instances. Do not reuse a
worker process, watcher process, systemd unit, service user, env file, config
directory, data directory, log directory, runtime directory, or lifecycle marker
across worker instances. Each worker instance must have its own paired watcher or
supervisor with instance-scoped names derived from the safe worker id.

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
V1 terminal result status must preserve `completed`/`done`, `failed`,
`cancelled`, and `partial_completed` distinctly through job result rows,
`review_runs`, scan state, public scan payloads, and artifact/result retrieval;
do not collapse cancelled or partial results back to legacy `failed` or
`queued` states.

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
and stored scan state, not from raw worker-only artifact internals. Existing `/worker/...`
lifecycle routes are operator plumbing; do not reintroduce `/worker/jobs/...`,
`/worker/heartbeat`, or `/worker/agent-configs` for review protocol behavior.
Active v1 heartbeat `progress` snapshots must include `message`, the full
counter set from the v1.2 spec (`source_like_files_*`, `bundles_*`,
`reviewer_runs_*`, `intent_tests_*`, `validator_candidates_*`, and
`artifacts_*`), and an `active_unit` object; malformed snapshots should be
rejected instead of accepted as partial progress.
V1 heartbeats may also carry Codex app-server quota telemetry as `codex_quota`.
Persist the sanitized quota payload, expose it through worker/admin status, and
do not remove it while refactoring readiness, lease eligibility, or worker
details. Quota exhaustion should make the worker unable to claim jobs without
breaking the required idle heartbeat concurrency shape.

Each leased v1 run must also have a first-class `review_runs` row. Create or
refresh it when a lease is issued, update its progress from accepted run events,
and finalize it from the terminal result envelope by storing summary,
quality-gate, usage, progress, error, and raw envelope JSON. Retry attempts must
not reuse a prior terminal run namespace: attempt 1 may use `run_<job_id>` for
backward compatibility, while attempt N must use an attempt-scoped run id such
as `run_<job_id>_attempt_<N>` so progress event sequences, artifacts, and result
idempotency are isolated per attempt. Web/admin terminal views should read
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
For `failed`, `cancelled`, and `partial_completed` terminal envelopes, the
server may accept missing required artifact uploads only when the v1 envelope
records `extensions.worker_internal.artifact_upload_error`; completed results
must never use that exception.

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
- A scan reserves both account and repository quota before queueing. Reserved
  quota becomes consumed only after a v1.2 core review phase starts, currently
  `repo_map`, `risk_routing`, `reviewer_fanout`, `clustering_and_voting`,
  `validator_disproof`, or `final_report_json`; do not use the legacy `ai`
  phase as a quota-consumption trigger. Release the reservation when a worker
  never reaches a billable core review phase. Keep idempotency and rollback
  paths aligned with both bucket ids.
- UI/API copy should say account, user, repository, or repo; avoid introducing
  workspace unless referring to a local checkout/worktree in the generic
  filesystem sense.

## Performance And State Source Of Truth

The server is being moved away from full in-memory scan/issue traversal. Keep
new read and write paths aligned with the normalized SQLite tables.

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
- Startup/recovery should be incremental by cursor/timestamp/job id. Avoid
  full reverse synchronization from all completed results back into memory.
- Worker/admin/status pages should use aggregate queries and short TTL caches
  rather than per-worker or per-scan loops.
