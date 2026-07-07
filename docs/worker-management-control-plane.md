# Worker Management Control Plane

Date: 2026-05-31

## Goals

Pullwise separates worker visibility from worker control:

- Public users may see read-only, sanitized worker status and fixed single-job capacity on the web status page.
- Administrators may manage worker registry state and worker credentials through `/admin/workers/*`.
- The server must not become a remote shell, SSH orchestrator, or root-level host manager for worker machines.

## Public Read-Only Status

`GET /status/system` is the public status surface. It exposes aggregate worker
counts and scan queue health, not per-worker records:

- `onlineWorkerCount`
- `offlineWorkerCount`
- `busyWorkerCount`
- `idleWorkerCount`
- `degradedWorkerCount`
- `totalWorkerCount`
- `queuedJobs`
- `runningJobs`
- `scanSystemStatus`

The public status payload must not include:

- per-worker ids, names, hostnames, regions, or versions
- `hostname`
- `last_error`
- `doctor_status`
- `codex_ready`
- `systemd_active`
- `running_jobs`
- `free_slots`
- `max_concurrent_jobs`
- `token_hash`
- `worker_token`
- worker audit events
- install commands, local paths, internal logs, or host-specific diagnostics

The web status page should render only aggregate public worker health for
ordinary visitors. Admin users may see richer worker details through admin-only
APIs.

## Admin Registry Control

`/admin/workers/*` is the worker registry control plane. These endpoints are
admin-only and are limited to registry state, desired state, and credentials:

- `GET /admin/workers`: list admin worker details with pagination
- `GET /admin/workers/{id}`: worker detail plus audit events
- `POST /admin/workers`: create a worker and return the worker token once
- `GET /admin/workers/defaults`: read worker install defaults and latest release metadata
- `POST /admin/workers/releases`: trigger the worker release workflow
- `PATCH /admin/workers/{id}`: update metadata such as name, provider, region, and version
- `POST /admin/workers/{id}/enable`: allow a worker to claim new jobs
- `POST /admin/workers/{id}/disable`: prevent a worker from claiming new jobs
- `POST /admin/workers/{id}/rotate-token`: rotate the worker credential and return the new token once
- `POST /admin/workers/{id}/test`: evaluate server-side registry and heartbeat diagnostics
- `POST /admin/workers/{id}/commands`: queue lifecycle commands such as `stop` or `uninstall`
- `DELETE /admin/workers/{id}`: queue worker uninstall and keep cleanup status visible until completion
- `DELETE /worker/registry`: worker-token authenticated self-unregister used by local uninstall

All admin writes must:

- require an admin session
- record a worker audit event with actor, action, worker id, request id, changed fields, success or failure, timestamp, and error when present
- keep worker token plaintext out of persisted storage
- return worker token plaintext only on create or rotate
- avoid returning worker tokens in nested worker payloads

## Stable Host Operations Model

Host lifecycle operations such as restart, update, cleanup, and uninstall should
not be implemented as server-side remote execution. The compliant model is a
pull-based command queue:

1. An admin creates a worker command through an admin-only endpoint.
2. The server persists the command with desired action, target worker id, actor, request id, status, attempts, and timestamps.
3. The worker watcher receives pending commands through `/worker/commands/poll`.
4. The worker validates that the command applies to its own authenticated worker id.
5. The worker executes the action locally with its existing least-privilege service account.
6. The worker reports progress, final status, output summary, and error details back to the server.
7. The server records the result and exposes it only to admins.

Review execution does not use this control plane. Review workers use
`review-worker-protocol/v1` under `/v1/workers/...` and `/v1/review-runs/...`.
The v1 `POST /v1/workers/{worker_id}/agent-configs` route is only for
server-owned plan config/readiness data used by `pullwise-worker doctor`; it is
not a job claim, heartbeat, progress, artifact, or result route. Do not
reintroduce `/worker/heartbeat`, `/worker/agent-configs`, or `/worker/jobs/...`
for review heartbeat, lease, progress, artifact, or result traffic.

`stop` commands disable job claiming but keep the worker in the registry.
`DELETE /admin/workers/{id}` creates an `uninstall` command instead of only
soft-deleting registry state. `uninstall` commands disable job claiming and keep
the worker in admin lists while cleanup is pending, running, or failed so
operators can see the watcher status and retry context. The worker registry row
is soft-deleted only after cleanup reports success. Current installs create one
host-local watcher service per worker instance. The watcher polls lifecycle
commands without mutating heartbeat state, stops the paired worker service,
writes an uninstall marker, reports command status, and removes the worker
service unit, watcher unit, wrapper binary, logrotate file, `/etc`
configuration directory, instance home, and instance log directory. A locally
run `pullwise-worker uninstall` calls
`DELETE /worker/registry` before removing the local service when a worker token
is configured.

This model keeps root, SSH, and host-specific privileges off the server. It also
makes operations retryable, auditable, and compatible with workers behind NAT or
private networks.

## Non-Goals

The worker management control plane must not:

- expose public hostnames or local paths
- expose worker tokens or token hashes
- expose internal errors to ordinary users
- let ordinary users mutate worker registry state
- let the server directly SSH into worker hosts
- provide arbitrary command execution from the server to a worker
