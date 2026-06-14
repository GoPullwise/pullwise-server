# Pullwise Server Agent Notes

## Worker Installer Provider Isolation

The server-generated worker installer must preserve per-worker Codex/OpenCode
isolation. A worker must never depend on global Codex/OpenCode config, root auth,
or another worker instance's auth state.

- Generated install commands and suggested env must point provider commands at
  the target worker home, for example:
  - `$DATA_DIR/.local/bin/codex`
  - `$DATA_DIR/.codex/bin/codex`
  - `$DATA_DIR/.opencode/bin/opencode`
- The installer, saved auth commands, and systemd unit must use the same
  instance-scoped environment:
  - `HOME=$DATA_DIR`
  - `USERPROFILE=$DATA_DIR`
  - `CODEX_HOME=$DATA_DIR/.codex`
  - `XDG_CONFIG_HOME=$DATA_DIR/.config`
  - `XDG_CACHE_HOME=$DATA_DIR/.cache`
  - `PATH` including only the service path and this worker's local tool dirs
- The installer should create the per-worker config/cache/auth directories under
  `$DATA_DIR`.
- The installer-time readiness output and a later `doctor` run with no
  intervening manual action must agree. `doctor` must not appear ready because
  it sees root/global auth or another worker's provider config.

When changing worker installer generation, keep multi-worker deployments in
mind: every worker on the same server must use only its own configured Codex and
OpenCode directories.

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

## Agent Config Source Of Truth

The server owns subscription plan agent policy.

- Free/pro/max review agent configs are the source of truth for plan provider
  chains, model names, reasoning effort/variant, and repository limits.
- Worker claim payloads must include per-job `agentConfig` and
  `repositoryLimits`; workers should not infer those from local defaults.
- The worker agent-config endpoint used by `doctor` must expose the same plan
  configs that job claims use.
- Keep provider chain payloads in camelCase for worker-facing API responses
  (`providerChain`), not mixed with internal snake_case fields.

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
