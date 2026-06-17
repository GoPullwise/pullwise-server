#!/usr/bin/env bash

# Poll the configured Git upstream and redeploy Pullwise when new commits land.
# Intended to run from the repository root, either manually with --once or as a
# long-running process managed by systemd/cron.

set -uo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
APP_DIR=${PULLWISE_WATCH_APP_DIR:-$SCRIPT_DIR}
INTERVAL_SECONDS=${PULLWISE_WATCH_INTERVAL_SECONDS:-60}
REMOTE=${PULLWISE_WATCH_REMOTE:-origin}
BRANCH=${PULLWISE_WATCH_BRANCH:-}
LOG_FILE=${PULLWISE_WATCH_LOG_FILE:-$APP_DIR/.pullwise/git-watch.log}
LOCK_DIR=${PULLWISE_WATCH_LOCK_DIR:-$APP_DIR/.pullwise/git-watch.lock}
DEPLOYED_HEAD_FILE=${PULLWISE_WATCH_DEPLOYED_HEAD_FILE:-$APP_DIR/.pullwise/git-watch.deployed-head}

RUN_SETUP=${PULLWISE_WATCH_RUN_SETUP:-true}
RUN_TESTS=${PULLWISE_WATCH_RUN_TESTS:-true}
RUN_SYNC_ENV=${PULLWISE_WATCH_RUN_SYNC_ENV:-false}
RUN_DOCTOR=${PULLWISE_WATCH_RUN_DOCTOR:-false}
RUN_HEALTH=${PULLWISE_WATCH_RUN_HEALTH:-true}
ALLOW_DIRTY=${PULLWISE_WATCH_ALLOW_DIRTY:-false}

SETUP_COMMAND=${PULLWISE_WATCH_SETUP_COMMAND:-./launcher.sh setup}
TEST_COMMAND=${PULLWISE_WATCH_TEST_COMMAND:-.venv/bin/python -m unittest discover -s tests}
SYNC_ENV_COMMAND=${PULLWISE_WATCH_SYNC_ENV_COMMAND:-./launcher.sh sync-env}
DOCTOR_COMMAND=${PULLWISE_WATCH_DOCTOR_COMMAND:-./launcher.sh doctor}
RESTART_COMMAND=${PULLWISE_WATCH_RESTART_COMMAND:-./launcher.sh restart}
HEALTH_COMMAND=${PULLWISE_WATCH_HEALTH_COMMAND:-./launcher.sh health}

ONCE=false

usage() {
  cat <<'USAGE'
Usage:
  ./git-watch.sh [--once]

Poll Git for updates. When the configured upstream has a new commit, the script
pulls with --ff-only, runs deployment checks, then restarts via launcher.sh.

Common environment overrides:
  PULLWISE_WATCH_INTERVAL_SECONDS=60
  PULLWISE_WATCH_REMOTE=origin
  PULLWISE_WATCH_BRANCH=main
  PULLWISE_WATCH_RUN_SYNC_ENV=false
  PULLWISE_WATCH_RUN_DOCTOR=false
  PULLWISE_WATCH_DEPLOYED_HEAD_FILE=.pullwise/git-watch.deployed-head
  PULLWISE_WATCH_RESTART_COMMAND='./launcher.sh restart'

Default update flow:
  git fetch
  git pull --ff-only
  ./launcher.sh setup
  .venv/bin/python -m unittest discover -s tests
  ./launcher.sh restart
  ./launcher.sh health
USAGE
}

timestamp() {
  date '+%Y-%m-%d %H:%M:%S'
}

log() {
  printf '[%s] %s\n' "$(timestamp)" "$*"
}

is_true() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

die() {
  log "ERROR: $*" >&2
  return 1
}

read_os_value() {
  local key="$1"
  local os_file="${PULLWISE_WATCH_OS_RELEASE_FILE:-/etc/os-release}"
  local line value
  [ -f "$os_file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      "$key"=*)
        value="${line#*=}"
        value="${value%\"}"
        value="${value#\"}"
        printf '%s' "$value"
        return 0
        ;;
    esac
  done < "$os_file"
}

is_ubuntu_2204() {
  [ "$(read_os_value ID)" = "ubuntu" ] && [ "$(read_os_value VERSION_ID)" = "22.04" ]
}

auto_install_enabled() {
  is_true "${PULLWISE_WATCH_SKIP_DEPENDENCY_INSTALL:-false}" && return 1
  return 0
}

apt_get_bin() {
  if [ -n "${PULLWISE_WATCH_APT_GET_BIN:-}" ]; then
    printf '%s' "$PULLWISE_WATCH_APT_GET_BIN"
    return 0
  fi
  command -v apt-get 2>/dev/null
}

sudo_bin() {
  if [ -n "${PULLWISE_WATCH_SUDO_BIN:-}" ]; then
    printf '%s' "$PULLWISE_WATCH_SUDO_BIN"
    return 0
  fi
  command -v sudo 2>/dev/null
}

run_apt_get() {
  local apt_get="$1"
  shift
  if [ "$(id -u 2>/dev/null || printf 1)" = "0" ]; then
    DEBIAN_FRONTEND=noninteractive "$apt_get" "$@"
    return $?
  fi
  local sudo_cmd
  sudo_cmd="$(sudo_bin)"
  [ -n "$sudo_cmd" ] || {
    log "ERROR: Missing dependencies and sudo is not available to install them." >&2
    return 1
  }
  "$sudo_cmd" env DEBIAN_FRONTEND=noninteractive "$apt_get" "$@"
}

install_ubuntu_packages() {
  local packages=("$@")
  [ "${#packages[@]}" -gt 0 ] || return 0
  auto_install_enabled || {
    log "ERROR: Missing dependencies: ${packages[*]}. Dependency auto-install is disabled by PULLWISE_WATCH_SKIP_DEPENDENCY_INSTALL." >&2
    return 1
  }
  is_ubuntu_2204 || {
    log "ERROR: Missing dependencies: ${packages[*]}. Automatic installation is supported on Ubuntu 22.04 hosts." >&2
    return 1
  }
  local apt_get
  apt_get="$(apt_get_bin)"
  [ -n "$apt_get" ] || {
    log "ERROR: Missing dependencies: ${packages[*]}. apt-get is required for Ubuntu 22.04 dependency installation." >&2
    return 1
  }
  log "installing Ubuntu packages: ${packages[*]}"
  run_apt_get "$apt_get" update || return 1
  run_apt_get "$apt_get" install -y --no-install-recommends "${packages[@]}" || return 1
}

ensure_command_available() {
  local label="$1"
  local command_name="$2"
  shift 2
  if command -v "$command_name" >/dev/null 2>&1; then
    return 0
  fi
  install_ubuntu_packages "$@" || return 1
  command -v "$command_name" >/dev/null 2>&1 || {
    log "ERROR: $label is still unavailable after installing: $*" >&2
    return 1
  }
}

ensure_host_dependencies() {
  ensure_command_available "git" git git || return 1
  ensure_command_available "tee" tee coreutils || return 1
  ensure_command_available "sed" sed sed || return 1
}

run_command() {
  label=$1
  command_text=$2
  log "running $label: $command_text"
  sh -c "$command_text"
}

read_deployed_head() {
  [ -f "$DEPLOYED_HEAD_FILE" ] || return 1
  sed -n '1p' "$DEPLOYED_HEAD_FILE" | tr -cd '0-9a-fA-F'
}

write_deployed_head() {
  head=$1
  mkdir -p "$(dirname -- "$DEPLOYED_HEAD_FILE")" || return 1
  printf '%s\n' "$head" > "$DEPLOYED_HEAD_FILE"
}

current_branch() {
  if [ -n "$BRANCH" ]; then
    printf '%s' "$BRANCH"
    return 0
  fi
  git symbolic-ref --quiet --short HEAD
}

remote_ref_for_branch() {
  branch=$1
  if [ -n "$BRANCH" ]; then
    printf '%s/%s' "$REMOTE" "$branch"
  elif upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null); then
    printf '%s' "$upstream"
  else
    printf '%s/%s' "$REMOTE" "$branch"
  fi
}

ensure_clean_worktree() {
  if is_true "$ALLOW_DIRTY"; then
    return 0
  fi
  if ! git diff --quiet --ignore-submodules -- || ! git diff --cached --quiet --ignore-submodules --; then
    die "working tree has local changes; set PULLWISE_WATCH_ALLOW_DIRTY=true to allow updates anyway"
    return 1
  fi
  if [ -n "$(git ls-files --others --exclude-standard)" ]; then
    die "working tree has untracked files; set PULLWISE_WATCH_ALLOW_DIRTY=true to allow updates anyway"
    return 1
  fi
}

deploy_after_pull() {
  if is_true "$RUN_SETUP"; then
    run_command "setup" "$SETUP_COMMAND" || return 1
  fi
  if is_true "$RUN_TESTS"; then
    run_command "tests" "$TEST_COMMAND" || return 1
  fi
  if is_true "$RUN_SYNC_ENV"; then
    run_command "sync-env" "$SYNC_ENV_COMMAND" || return 1
  fi
  if is_true "$RUN_DOCTOR"; then
    run_command "doctor" "$DOCTOR_COMMAND" || return 1
  fi
  run_command "restart" "$RESTART_COMMAND" || return 1
  if is_true "$RUN_HEALTH"; then
    run_command "health" "$HEALTH_COMMAND" || return 1
  fi
}

deploy_current_head() {
  reason=$1
  head=$(git rev-parse HEAD) || return 1
  log "deploying $reason at $head"
  deploy_after_pull || return 1
  write_deployed_head "$head" || return 1
  log "deployed HEAD $head"
}

current_head_needs_deploy() {
  current_head=$(git rev-parse HEAD) || return 1
  deployed_head=$(read_deployed_head 2>/dev/null || true)
  [ "$current_head" != "$deployed_head" ]
}

check_once() {
  cd "$APP_DIR" || return 1
  mkdir -p "$(dirname -- "$LOG_FILE")" || return 1

  branch=$(current_branch) || {
    die "unable to determine current branch; set PULLWISE_WATCH_BRANCH"
    return 1
  }
  remote_ref=$(remote_ref_for_branch "$branch")

  ensure_clean_worktree || return 1

  log "fetching $REMOTE $branch"
  git fetch "$REMOTE" "$branch" || return 1

  local_head=$(git rev-parse HEAD) || return 1
  remote_head=$(git rev-parse FETCH_HEAD) || return 1

  if [ "$local_head" = "$remote_head" ]; then
    if current_head_needs_deploy; then
      deploy_current_head "current HEAD has not completed deployment"
      return $?
    fi
    log "no update; HEAD is $local_head"
    return 0
  fi

  if git merge-base --is-ancestor "$remote_head" HEAD; then
    if current_head_needs_deploy; then
      deploy_current_head "local HEAD is ahead of $remote_ref and has not completed deployment"
      return $?
    fi
    log "local HEAD is ahead of $remote_ref; skipping deploy"
    return 0
  fi

  if ! git merge-base --is-ancestor HEAD "$remote_head"; then
    die "local HEAD and $remote_ref have diverged; refusing to pull"
    return 1
  fi

  log "updating $branch: $local_head -> $remote_head"
  git pull --ff-only "$REMOTE" "$branch" || return 1
  deploy_current_head "updated $branch"
}

with_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM
    check_once
    status=$?
    rmdir "$LOCK_DIR" 2>/dev/null || true
    trap - EXIT INT TERM
    return "$status"
  fi

  log "another git-watch run is active; skipping"
  return 0
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --once)
      ONCE=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
  shift
done

ensure_host_dependencies || exit 1
mkdir -p "$(dirname -- "$LOG_FILE")" || exit 1

if [ "$ONCE" = true ]; then
  with_lock 2>&1 | tee -a "$LOG_FILE"
  exit "${PIPESTATUS[0]}"
fi

log "git watcher started for $APP_DIR; interval ${INTERVAL_SECONDS}s" | tee -a "$LOG_FILE"
while :; do
  with_lock 2>&1 | tee -a "$LOG_FILE"
  sleep "$INTERVAL_SECONDS"
done
