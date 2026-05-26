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

run_command() {
  label=$1
  command_text=$2
  log "running $label: $command_text"
  sh -c "$command_text"
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
    log "no update; HEAD is $local_head"
    return 0
  fi

  if git merge-base --is-ancestor "$remote_head" HEAD; then
    log "local HEAD is ahead of $remote_ref; skipping deploy"
    return 0
  fi

  if ! git merge-base --is-ancestor HEAD "$remote_head"; then
    die "local HEAD and $remote_ref have diverged; refusing to pull"
    return 1
  fi

  log "updating $branch: $local_head -> $remote_head"
  git pull --ff-only "$REMOTE" "$branch" || return 1
  deploy_after_pull
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
