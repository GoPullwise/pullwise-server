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
  - `$DATA_DIR/.local/bin/codex`
  - `$DATA_DIR/.codex/bin/codex`
- The installer, saved auth commands, and systemd unit must use the same
  instance-scoped environment:
  - `HOME=$DATA_DIR`
  - `USERPROFILE=$DATA_DIR`
  - `CODEX_HOME=$DATA_DIR/.codex`
  - `XDG_CONFIG_HOME=$DATA_DIR/.config`
  - `XDG_CACHE_HOME=$DATA_DIR/.cache`
  - `XDG_DATA_HOME=$DATA_DIR/.local/share`
  - `PATH` with this worker's `$DATA_DIR/.local/bin`, `$DATA_DIR/.codex/bin`,
    before the base service path
- The installer should create the per-worker config/cache/auth directories under
  `$DATA_DIR`.
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

- Do not trust heartbeat `running_jobs` alone for worker slot accounting when
  `active_job_ids` is present. Reconcile reported active job ids against
  `scan_jobs` and count only jobs still accepting worker updates:
  `claimed`, `running`, or `uploading_result`.
- A job in `cancelled`, `done`, `failed`, or `lost` must not keep the worker
  busy, must not receive lease renewal, and must not block the same worker from
  claiming the next queued job, even if a stale heartbeat still reports that job
  as active.
- Keep regression coverage for this path. The important scenario is: worker
  claims a job, the scan/job is cancelled, the worker heartbeat reports the old
  job in `active_job_ids`, and the same worker can still claim a new queued job.

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
- Worker claim payloads must include per-job `agentConfig` and
  `repositoryLimits`; workers should not infer those from local defaults.
- The worker agent-config endpoint used by `doctor` must expose the same plan
  configs that job claims use.
- Keep the plan review-agent provider as a single `provider` field in
  worker-facing API responses.

## Graph-Verified Result Semantics

Worker GraphVerified results are full-repository snapshot reviews. Server
claim/result APIs, artifacts, and copy should preserve that scope. Preserve
review-unit coverage metadata in stored artifacts even when the confirmed
finding list is empty.

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
- A scan consumes both account and repository quota before queueing. Keep
  idempotency and rollback paths aligned with both bucket ids.
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
