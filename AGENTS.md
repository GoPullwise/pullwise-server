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
