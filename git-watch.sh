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
LOCK_FILE=${PULLWISE_WATCH_LOCK_FILE:-$LOCK_DIR.flock}
DEPLOYED_HEAD_FILE=${PULLWISE_WATCH_DEPLOYED_HEAD_FILE:-$APP_DIR/.pullwise/git-watch.deployed-head}
STATUS_FILE=${PULLWISE_WATCH_STATUS_FILE:-$APP_DIR/.pullwise/git-watch.status.json}

RUN_SETUP=${PULLWISE_WATCH_RUN_SETUP:-true}
RUN_TESTS=${PULLWISE_WATCH_RUN_TESTS:-true}
RUN_SYNC_ENV=${PULLWISE_WATCH_RUN_SYNC_ENV:-false}
RUN_DOCTOR=${PULLWISE_WATCH_RUN_DOCTOR:-false}
RUN_HEALTH=${PULLWISE_WATCH_RUN_HEALTH:-true}
ALLOW_DIRTY=${PULLWISE_WATCH_ALLOW_DIRTY:-false}
HEALTH_RETRIES=${PULLWISE_WATCH_HEALTH_RETRIES:-30}
HEALTH_RETRY_SECONDS=${PULLWISE_WATCH_HEALTH_RETRY_SECONDS:-2}

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
  PULLWISE_WATCH_HEALTH_RETRIES=30
  PULLWISE_WATCH_HEALTH_RETRY_SECONDS=2
  PULLWISE_WATCH_DEPLOYED_HEAD_FILE=.pullwise/git-watch.deployed-head
  PULLWISE_WATCH_STATUS_FILE=.pullwise/git-watch.status.json
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
  ensure_command_available "flock" flock util-linux || return 1
  ensure_command_available "tee" tee coreutils || return 1
  ensure_command_available "sed" sed sed || return 1
}

COMMAND_ARGV=()

parse_command_argv() {
  local command_text="$1"
  COMMAND_ARGV=()
  local token=""
  local quote=""
  local char
  local have_token=0
  local i
  local len=${#command_text}

  for ((i = 0; i < len; i++)); do
    char="${command_text:i:1}"
    if [ -n "$quote" ]; then
      if [ "$char" = "$quote" ]; then
        quote=""
        have_token=1
      elif [ "$char" = "\\" ]; then
        i=$((i + 1))
        if [ "$i" -ge "$len" ]; then
          log "ERROR: command has trailing escape: $command_text" >&2
          return 1
        fi
        token="${token}${command_text:i:1}"
        have_token=1
      else
        token="${token}${char}"
        have_token=1
      fi
      continue
    fi

    case "$char" in
      "'"|'"')
        quote="$char"
        have_token=1
        ;;
      "\\")
        i=$((i + 1))
        if [ "$i" -ge "$len" ]; then
          log "ERROR: command has trailing escape: $command_text" >&2
          return 1
        fi
        token="${token}${command_text:i:1}"
        have_token=1
        ;;
      " "|$'\t'|$'\n'|$'\r')
        if [ "$have_token" -eq 1 ]; then
          COMMAND_ARGV+=("$token")
          token=""
          have_token=0
        fi
        ;;
      *)
        token="${token}${char}"
        have_token=1
        ;;
    esac
  done

  if [ -n "$quote" ]; then
    log "ERROR: command has unterminated quote: $command_text" >&2
    return 1
  fi
  if [ "$have_token" -eq 1 ]; then
    COMMAND_ARGV+=("$token")
  fi
  if [ "${#COMMAND_ARGV[@]}" -eq 0 ]; then
    log "ERROR: command is empty" >&2
    return 1
  fi
}

run_command() {
  local label=$1
  local command_text=$2
  parse_command_argv "$command_text" || return 1
  log "running $label: $command_text"
  "${COMMAND_ARGV[@]}"
}

run_health_command() {
  attempts=$HEALTH_RETRIES
  delay=$HEALTH_RETRY_SECONDS
  case "$attempts" in
    ""|*[!0-9]*)
      attempts=1
      ;;
  esac
  [ "$attempts" -gt 0 ] || attempts=1

  attempt=1
  while [ "$attempt" -le "$attempts" ]; do
    if run_command "health" "$HEALTH_COMMAND"; then
      return 0
    fi
    if [ "$attempt" -lt "$attempts" ]; then
      log "health check attempt $attempt/$attempts failed; retrying in ${delay}s"
      sleep "$delay"
    fi
    attempt=$((attempt + 1))
  done

  log "ERROR: health check failed after $attempts attempt(s)" >&2
  return 1
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

write_success_status() {
  head=$1
  case "$head" in
    *[!0-9a-fA-F]*|"")
      log "ERROR: refusing to publish an invalid Git revision" >&2
      return 1
      ;;
  esac
  case "${#head}" in
    40|64)
      ;;
    *)
      log "ERROR: refusing to publish an invalid Git revision length" >&2
      return 1
      ;;
  esac

  completed_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ') || return 1
  mkdir -p "$(dirname -- "$STATUS_FILE")" || return 1
  status_tmp=$STATUS_FILE.tmp.$$
  if ! printf '{"schemaVersion":1,"status":"succeeded","revision":"%s","completedAt":"%s"}\n' \
    "$head" "$completed_at" > "$status_tmp"; then
    rm -f -- "$status_tmp"
    return 1
  fi
  chmod 0644 "$status_tmp" || {
    rm -f -- "$status_tmp"
    return 1
  }
  mv -f -- "$status_tmp" "$STATUS_FILE" || {
    rm -f -- "$status_tmp"
    return 1
  }
}

current_branch() {
  if [ -n "$BRANCH" ]; then
    checked_out_branch=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)
    if [ "$checked_out_branch" != "$BRANCH" ]; then
      [ -n "$checked_out_branch" ] || checked_out_branch="detached HEAD"
      die "checked out branch is $checked_out_branch, expected $BRANCH"
      return 1
    fi
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
    run_health_command || return 1
  fi
}

deploy_current_head() {
  reason=$1
  head=$(git rev-parse HEAD) || return 1
  log "deploying $reason at $head"
  deploy_after_pull || return 1
  write_success_status "$head" || return 1
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
  mkdir -p "$(dirname -- "$LOCK_FILE")" || return 1
  exec 9>"$LOCK_FILE" || return 1
  if flock -n 9; then
    # Remove an empty lock directory left by versions that predated flock.
    rmdir -- "$LOCK_DIR" 2>/dev/null || true
    check_once
    status=$?
    flock -u 9
    exec 9>&-
    return "$status"
  fi

  exec 9>&-
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
