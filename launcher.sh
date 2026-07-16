#!/usr/bin/env sh

# Pullwise server launcher for Ubuntu 22.04 production hosts.
# The production path is:
#   project .env.local -> /etc/pullwise/server.env -> systemd EnvironmentFile.

APP_NAME="pullwise-server"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
APP_DIR=${PULLWISE_APP_DIR:-$SCRIPT_DIR}
LOCAL_ENV_FILE=${PULLWISE_LOCAL_ENV_FILE:-$APP_DIR/.env.local}
SYSTEM_ENV_FILE=${PULLWISE_SYSTEM_ENV_FILE:-/etc/pullwise/server.env}
SERVICE_NAME=${PULLWISE_SERVICE_NAME:-pullwise-server}
SYSTEMD_DIR=${PULLWISE_SYSTEMD_DIR:-/etc/systemd/system}
SERVICE_FILE=${PULLWISE_SERVICE_FILE:-$SYSTEMD_DIR/$SERVICE_NAME.service}
WATCH_SERVICE_NAME=${PULLWISE_WATCH_SERVICE_NAME:-$SERVICE_NAME-git-watch}
WATCH_SERVICE_FILE=${PULLWISE_WATCH_SERVICE_FILE:-$SYSTEMD_DIR/$WATCH_SERVICE_NAME.service}
WATCH_BRANCH=${PULLWISE_WATCH_BRANCH:-main}
WATCH_INTERVAL_SECONDS=${PULLWISE_WATCH_INTERVAL_SECONDS:-60}
SERVICE_USER=${PULLWISE_SERVICE_USER:-pullwise}
SERVICE_GROUP=${PULLWISE_SERVICE_GROUP:-pullwise}
VENV_DIR=${PULLWISE_VENV_DIR:-$APP_DIR/.venv}
RUN_DIR=${PULLWISE_RUN_DIR:-$APP_DIR/.pullwise/run}
PID_FILE=${PULLWISE_PID_FILE:-$RUN_DIR/$APP_NAME.pid}
SERVER_OUT_LOG=${PULLWISE_SERVER_OUT_LOG:-$RUN_DIR/server.out.log}
SERVER_ERR_LOG=${PULLWISE_SERVER_ERR_LOG:-$RUN_DIR/server.err.log}
STOP_TIMEOUT=${PULLWISE_STOP_TIMEOUT_SECONDS:-20}

if [ -n "${PULLWISE_ENV_FILE-}" ]; then
  ENV_FILE=$PULLWISE_ENV_FILE
elif [ -f "$SYSTEM_ENV_FILE" ]; then
  ENV_FILE=$SYSTEM_ENV_FILE
elif [ -f "$LOCAL_ENV_FILE" ]; then
  ENV_FILE=$LOCAL_ENV_FILE
else
  ENV_FILE=$APP_DIR/.env
fi

fail_count=0
warn_count=0

usage() {
  cat <<'USAGE'
Usage:
  ./launcher.sh <command> [options]

Commands:
  init-env [--force]        Create .env.local from .env.example and list next steps
  setup                     Create .venv and install the server package
  sync-env                  Copy .env.local to /etc/pullwise/server.env
  render-service            Print the systemd service unit
  install-service [--dry-run] Sync env, install unit, daemon-reload, enable
  render-watch-service      Print the Git watcher systemd service unit
  install-watch-service [--dry-run]
                            Install, enable, and start the Git watcher
  start [--dry-run]         Start via systemd when installed, else direct
  run                       Run the server in the foreground
  stop [--force]            Stop via systemd when installed, else direct
  restart                   Restart via systemd when installed, else direct
  status                    Show process/service and health status
  health                    Query GET /health
  logs [target]             Tail logs; target: journal, server, error, app, all
  doctor                    Audit Ubuntu 22.04 production readiness
  audit                     Alias for doctor
  config                    Print effective non-secret launcher configuration
  export [--include-secrets] <archive.tar.gz>
                            Package db, logs, checkouts, and state; env/PEM only with --include-secrets
  import <archive.tar.gz>   Restore a migration package and render service
  help                      Show this help

Environment overrides:
  PULLWISE_LOCAL_ENV_FILE, PULLWISE_SYSTEM_ENV_FILE, PULLWISE_SYSTEMD_DIR
  PULLWISE_SERVICE_NAME, PULLWISE_SERVICE_USER, PULLWISE_SERVICE_GROUP
  PULLWISE_VENV_DIR, PULLWISE_PYTHON_BIN, PULLWISE_MANAGER

Recommended production flow:
  ./launcher.sh init-env
  ./launcher.sh setup
  ./launcher.sh sync-env
  ./launcher.sh install-service
  ./launcher.sh doctor
  ./launcher.sh start
USAGE
}

say() {
  printf '%s\n' "$*"
}

info() {
  printf '[info] %s\n' "$*"
}

ok() {
  printf '[ok] %s\n' "$*"
}

warn() {
  warn_count=$((warn_count + 1))
  printf '[warn] %s\n' "$*" >&2
}

fail() {
  fail_count=$((fail_count + 1))
  printf '[fail] %s\n' "$*" >&2
}

die() {
  printf '[fail] %s\n' "$*" >&2
  exit 1
}

trim() {
  printf '%s' "$1" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

is_valid_env_name() {
  case "$1" in
    ""|[0-9]*|*[!A-Za-z0-9_]*)
      return 1
      ;;
    *)
      return 0
      ;;
  esac
}

is_env_set() {
  eval '[ "${'"$1"'+x}" = x ]'
}

strip_outer_quotes() {
  value=$1
  case "$value" in
    \"*\")
      value=${value#\"}
      value=${value%\"}
      ;;
    \'*\')
      value=${value#\'}
      value=${value%\'}
      ;;
  esac
  printf '%s' "$value"
}

read_env_file_value() {
  file=$1
  wanted=$2
  [ -f "$file" ] || return 1
  while IFS= read -r raw_line || [ -n "$raw_line" ]; do
    line=$(printf '%s' "$raw_line" | tr -d '\r')
    case "$line" in
      ""|\#*)
        continue
        ;;
      *=*)
        key=$(trim "${line%%=*}")
        if [ "$key" = "$wanted" ]; then
          strip_outer_quotes "$(trim "${line#*=}")"
          return 0
        fi
        ;;
    esac
  done < "$file"
  return 1
}

load_env_file() {
  [ -f "$ENV_FILE" ] || return 0
  while IFS= read -r raw_line || [ -n "$raw_line" ]; do
    line=$(printf '%s' "$raw_line" | tr -d '\r')
    case "$line" in
      ""|\#*)
        continue
        ;;
      *=*)
        key=$(trim "${line%%=*}")
        value=$(strip_outer_quotes "$(trim "${line#*=}")")
        if is_valid_env_name "$key" && ! is_env_set "$key"; then
          export "$key=$value"
        fi
        ;;
    esac
  done < "$ENV_FILE"
}

env_value() {
  name=$1
  default=${2-}
  eval 'value=${'"$name"'-}'
  if [ -n "$value" ]; then
    printf '%s' "$value"
  else
    printf '%s' "$default"
  fi
}

env_file_value() {
  file=$1
  name=$2
  default=${3-}
  value=$(read_env_file_value "$file" "$name" 2>/dev/null || true)
  if [ -n "$value" ]; then
    printf '%s' "$value"
  else
    printf '%s' "$default"
  fi
}

is_abs_path() {
  case "$1" in
    /*|[A-Za-z]:/*|[A-Za-z]:\\*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

abs_path() {
  path=$1
  if [ -z "$path" ]; then
    return 0
  fi
  if is_abs_path "$path"; then
    printf '%s' "$path"
  else
    printf '%s/%s' "$APP_DIR" "$path"
  fi
}

path_from_env_file() {
  file=$1
  name=$2
  default=$3
  abs_path "$(env_file_value "$file" "$name" "$default")"
}

db_path() {
  abs_path "$(env_value PULLWISE_DB_PATH "$APP_DIR/.pullwise/pullwise.sqlite3")"
}

state_encryption_key_path() {
  raw=$(env_value PULLWISE_STATE_ENCRYPTION_KEY_PATH "/etc/pullwise/secrets/state-encryption-key")
  [ -n "$raw" ] || return 0
  abs_path "$raw"
}

log_dir() {
  abs_path "$(env_value PULLWISE_LOG_DIR "$APP_DIR/.pullwise/logs")"
}

checkout_root() {
  abs_path "$(env_value PULLWISE_CHECKOUT_ROOT "$APP_DIR/.pullwise/checkouts")"
}

host_value() {
  env_value PULLWISE_HOST "0.0.0.0"
}

port_value() {
  env_value PULLWISE_PORT "8080"
}

python_bin() {
  if [ -n "${PULLWISE_PYTHON_BIN-}" ]; then
    printf '%s' "$PULLWISE_PYTHON_BIN"
    return 0
  fi
  if [ -x "$VENV_DIR/bin/python" ]; then
    printf '%s' "$VENV_DIR/bin/python"
    return 0
  fi
  if command -v python3.10 >/dev/null 2>&1; then
    command -v python3.10
    return 0
  fi
  return 1
}

tool_bin() {
  override_name=$1
  default_name=$2
  eval 'override=${'"$override_name"'-}'
  if [ -n "$override" ]; then
    printf '%s' "$override"
    return 0
  fi
  command -v "$default_name" 2>/dev/null
}

systemctl_bin() {
  tool_bin PULLWISE_SYSTEMCTL_BIN systemctl || printf '%s' systemctl
}

journalctl_bin() {
  tool_bin PULLWISE_JOURNALCTL_BIN journalctl || printf '%s' journalctl
}

tar_bin() {
  tool_bin PULLWISE_TAR_BIN tar || printf '%s' tar
}

apt_get_bin() {
  tool_bin PULLWISE_APT_GET_BIN apt-get || true
}

sudo_bin() {
  tool_bin PULLWISE_SUDO_BIN sudo || true
}

chgrp_bin() {
  tool_bin PULLWISE_CHGRP_BIN chgrp || true
}

chown_bin() {
  tool_bin PULLWISE_CHOWN_BIN chown || true
}

read_os_value() {
  key=$1
  os_file=${PULLWISE_OS_RELEASE_FILE:-/etc/os-release}
  read_env_file_value "$os_file" "$key" 2>/dev/null || true
}

auto_install_enabled() {
  ! is_true "${PULLWISE_SKIP_DEPENDENCY_INSTALL:-false}"
}

is_ubuntu_2204() {
  [ "$(read_os_value ID)" = "ubuntu" ] && [ "$(read_os_value VERSION_ID)" = "22.04" ]
}

run_apt_get() {
  apt_get=$1
  shift
  if [ "$(id -u 2>/dev/null || printf 1)" = "0" ]; then
    DEBIAN_FRONTEND=noninteractive "$apt_get" "$@"
    return $?
  fi
  sudo_cmd=$(sudo_bin)
  [ -n "$sudo_cmd" ] || die "Missing dependencies and sudo is not available to install them."
  "$sudo_cmd" env DEBIAN_FRONTEND=noninteractive "$apt_get" "$@"
}

install_ubuntu_packages() {
  packages=$*
  [ -n "$packages" ] || return 0
  auto_install_enabled || die "Missing dependencies: $packages. Dependency auto-install is disabled by PULLWISE_SKIP_DEPENDENCY_INSTALL."
  is_ubuntu_2204 || die "Missing dependencies: $packages. Automatic installation is supported on Ubuntu 22.04 hosts."
  apt_get=$(apt_get_bin)
  [ -n "$apt_get" ] || die "Missing dependencies: $packages. apt-get is required for Ubuntu 22.04 dependency installation."
  info "installing Ubuntu packages: $packages"
  run_apt_get "$apt_get" update || die "apt-get update failed."
  run_apt_get "$apt_get" install -y --no-install-recommends "$@" || die "apt-get install failed: $packages"
}

ensure_command_available() {
  label=$1
  override_name=$2
  command_name=$3
  shift 3
  bin=$(tool_bin "$override_name" "$command_name" || true)
  if [ -n "$bin" ]; then
    return 0
  fi
  install_ubuntu_packages "$@"
  bin=$(tool_bin "$override_name" "$command_name" || true)
  [ -n "$bin" ] || die "$label is still unavailable after installing: $*"
}

ensure_python_setup_dependencies() {
  if [ -n "${PULLWISE_PYTHON_BIN-}" ]; then
    return 0
  fi
  if command -v python3.10 >/dev/null 2>&1 && python3.10 -m venv --help >/dev/null 2>&1 && python3.10 -m pip --version >/dev/null 2>&1; then
    return 0
  fi
  install_ubuntu_packages python3.10 python3.10-venv python3-pip
  command -v python3.10 >/dev/null 2>&1 || die "python3.10 is still unavailable after installing Ubuntu packages."
  python3.10 -m venv --help >/dev/null 2>&1 || die "python3.10 venv is still unavailable after installing Ubuntu packages."
  python3.10 -m pip --version >/dev/null 2>&1 || die "python3.10 pip is still unavailable after installing Ubuntu packages."
}

set_service_group_readable_file() {
  file=$1
  chgrp_cmd=$(chgrp_bin)
  if [ -n "$chgrp_cmd" ]; then
    "$chgrp_cmd" "$SERVICE_GROUP" "$file" 2>/dev/null || warn "could not set group $SERVICE_GROUP on $file"
  else
    warn "chgrp not found; could not set group $SERVICE_GROUP on $file"
  fi
  chmod 640 "$file" 2>/dev/null || warn "could not chmod 640 $file"
}

set_service_owned_dir() {
  dir=$1
  [ -d "$dir" ] || return 0
  [ "${PULLWISE_LAUNCHER_TESTING:-}" = "1" ] && return 0
  [ "$(id -u 2>/dev/null || printf 1)" = "0" ] || return 0
  id "$SERVICE_USER" >/dev/null 2>&1 || return 0
  chown_cmd=$(chown_bin)
  if [ -n "$chown_cmd" ]; then
    "$chown_cmd" "$SERVICE_USER:$SERVICE_GROUP" "$dir" 2>/dev/null || warn "could not set owner $SERVICE_USER:$SERVICE_GROUP on $dir"
  else
    warn "chown not found; could not set owner $SERVICE_USER:$SERVICE_GROUP on $dir"
  fi
}

ensure_runtime_dirs() {
  mkdir -p "$RUN_DIR" || die "Unable to create run directory: $RUN_DIR"
  mkdir -p "$(dirname -- "$(db_path)")" || die "Unable to create database directory: $(dirname -- "$(db_path)")"
  mkdir -p "$(log_dir)" || die "Unable to create log directory: $(log_dir)"
  mkdir -p "$(checkout_root)" || die "Unable to create checkout directory: $(checkout_root)"
  set_service_owned_dir "$RUN_DIR"
  set_service_owned_dir "$(dirname -- "$(db_path)")"
  set_service_owned_dir "$(log_dir)"
  set_service_owned_dir "$(checkout_root)"
}

server_url_host() {
  host=$(host_value)
  case "$host" in
    ""|"0.0.0.0"|"::")
      printf '%s' "127.0.0.1"
      ;;
    *)
      printf '%s' "$host"
      ;;
  esac
}

health_url() {
  if [ -n "${PULLWISE_HEALTH_URL-}" ]; then
    printf '%s' "$PULLWISE_HEALTH_URL"
  else
    printf 'http://%s:%s/health' "$(server_url_host)" "$(port_value)"
  fi
}

read_pid() {
  [ -f "$PID_FILE" ] || return 1
  pid=$(sed -n '1p' "$PID_FILE" | tr -cd '0-9')
  [ -n "$pid" ] || return 1
  printf '%s' "$pid"
}

process_alive() {
  pid=$1
  [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1
}

process_command() {
  pid=$1
  ps -p "$pid" -o command= 2>/dev/null || true
}

is_pullwise_process() {
  pid=$1
  command_text=$(process_command "$pid")
  case "$command_text" in
    *pullwise_server*|*pullwise-server*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

running_pid() {
  pid=$(read_pid 2>/dev/null || true)
  if [ -n "$pid" ] && process_alive "$pid" && is_pullwise_process "$pid"; then
    printf '%s' "$pid"
    return 0
  fi
  return 1
}

service_installed() {
  [ -f "$SERVICE_FILE" ]
}

manager_mode() {
  case "${PULLWISE_MANAGER:-auto}" in
    systemd|direct)
      printf '%s' "$PULLWISE_MANAGER"
      ;;
    auto|"")
      if service_installed; then
        printf '%s' systemd
      else
        printf '%s' direct
      fi
      ;;
    *)
      die "Unknown PULLWISE_MANAGER: $PULLWISE_MANAGER"
      ;;
  esac
}

print_direct_server_command() {
  py=$1
  printf 'cd %s && nohup %s -m pullwise_server --host %s --port %s >> %s 2>> %s &\n' \
    "$APP_DIR" "$py" "$(host_value)" "$(port_value)" "$SERVER_OUT_LOG" "$SERVER_ERR_LOG"
}

print_systemctl_command() {
  action=$1
  printf '%s %s %s\n' "$(systemctl_bin)" "$action" "$SERVICE_NAME"
}

cmd_setup() {
  ensure_python_setup_dependencies
  if [ -n "${PULLWISE_PYTHON_BIN-}" ]; then
    bootstrap_python=$PULLWISE_PYTHON_BIN
  elif command -v python3.10 >/dev/null 2>&1; then
    bootstrap_python=$(command -v python3.10)
  else
    die "python3.10 is required. Install Python 3.10.12 on Ubuntu 22.04, or set PULLWISE_PYTHON_BIN."
  fi

  if [ ! -x "$VENV_DIR/bin/python" ]; then
    info "creating virtual environment at $VENV_DIR"
    "$bootstrap_python" -m venv "$VENV_DIR" || die "Unable to create virtual environment."
  fi

  py=$(python_bin) || die "Unable to find virtual environment Python."
  "$py" -m pip install --upgrade pip || die "Unable to upgrade pip."
  "$py" -m pip install -e "$APP_DIR" || die "Unable to install $APP_NAME."
  ok "setup complete"
}

cmd_init_env() {
  force=false
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --force)
        force=true
        ;;
      *)
        die "Unknown init-env option: $1"
        ;;
    esac
    shift
  done

  example_file=$APP_DIR/.env.example
  [ -f "$example_file" ] || die "template env file not found: $example_file"
  if [ -f "$LOCAL_ENV_FILE" ] && [ "$force" != true ]; then
    warn "local env file already exists: $LOCAL_ENV_FILE"
    say "Re-run with --force to overwrite it from .env.example."
  else
    mkdir -p "$(dirname -- "$LOCAL_ENV_FILE")" || die "Unable to create $(dirname -- "$LOCAL_ENV_FILE")"
    cp "$example_file" "$LOCAL_ENV_FILE" || die "Unable to create $LOCAL_ENV_FILE"
    chmod 600 "$LOCAL_ENV_FILE" 2>/dev/null || warn "could not chmod 600 $LOCAL_ENV_FILE"
    ok "created local env template: $LOCAL_ENV_FILE"
  fi

  say "Edit these required production groups in $LOCAL_ENV_FILE:"
  say "  - HTTP/runtime: PULLWISE_APP_URL, PULLWISE_ALLOWED_ORIGINS, PULLWISE_API_BASE_URL"
  say "  - Storage: PULLWISE_DB_PATH, PULLWISE_LOG_DIR, PULLWISE_CHECKOUT_ROOT"
  say "  - Secrets: GitHub OAuth/App credentials and PULLWISE_STATE_ENCRYPTION_KEY_PATH"
  say "  - External workers: create at least one worker from the admin API after deploy"
  say "Then run: ./launcher.sh sync-env && ./launcher.sh doctor"
}

cmd_sync_env() {
  dry_run=false
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dry-run)
        dry_run=true
        ;;
      *)
        die "Unknown sync-env option: $1"
        ;;
    esac
    shift
  done

  [ -f "$LOCAL_ENV_FILE" ] || die "local env file not found: $LOCAL_ENV_FILE"
  if [ "$dry_run" = true ]; then
    say "dry-run: copy $LOCAL_ENV_FILE -> $SYSTEM_ENV_FILE"
    return 0
  fi

  mkdir -p "$(dirname -- "$SYSTEM_ENV_FILE")" || die "Unable to create $(dirname -- "$SYSTEM_ENV_FILE")"
  cp "$LOCAL_ENV_FILE" "$SYSTEM_ENV_FILE" || die "Unable to copy env to $SYSTEM_ENV_FILE"
  set_service_group_readable_file "$SYSTEM_ENV_FILE"
  ok "synced $LOCAL_ENV_FILE -> $SYSTEM_ENV_FILE"
}

render_service_content() {
  py=$(python_bin) || die "Unable to find Python. Run ./launcher.sh setup first."
  cat <<SERVICE
[Unit]
Description=Pullwise Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$SYSTEM_ENV_FILE
ExecStart=$py -m pullwise_server
Restart=always
RestartSec=5
User=$SERVICE_USER
Group=$SERVICE_GROUP
UMask=007
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE
}

cmd_render_service() {
  render_service_content
}

render_watch_service_content() {
  cat <<SERVICE
[Unit]
Description=Pullwise Server Git Watcher
After=network-online.target $SERVICE_NAME.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=PULLWISE_WATCH_BRANCH=$WATCH_BRANCH
Environment=PULLWISE_WATCH_INTERVAL_SECONDS=$WATCH_INTERVAL_SECONDS
Environment=PULLWISE_WATCH_LOG_FILE=/dev/null
Environment="PULLWISE_WATCH_RESTART_COMMAND=/usr/bin/systemctl restart $SERVICE_NAME"
ExecStart=/usr/bin/bash $APP_DIR/git-watch.sh
Restart=always
RestartSec=10
User=root
UMask=0022
NoNewPrivileges=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE
}

cmd_render_watch_service() {
  render_watch_service_content
}

write_watch_service_file() {
  mkdir -p "$(dirname -- "$WATCH_SERVICE_FILE")" || die "Unable to create $(dirname -- "$WATCH_SERVICE_FILE")"
  tmp_file=$WATCH_SERVICE_FILE.tmp.$$
  render_watch_service_content > "$tmp_file" || die "Unable to render watcher service file."
  mv "$tmp_file" "$WATCH_SERVICE_FILE" || die "Unable to install watcher service file: $WATCH_SERVICE_FILE"
  chmod 644 "$WATCH_SERVICE_FILE" 2>/dev/null || warn "could not chmod 644 $WATCH_SERVICE_FILE"
  ok "watcher service file written: $WATCH_SERVICE_FILE"
}

cmd_install_watch_service() {
  dry_run=false
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dry-run)
        dry_run=true
        ;;
      *)
        die "Unknown install-watch-service option: $1"
        ;;
    esac
    shift
  done

  if [ "$dry_run" = true ]; then
    say "dry-run: write watcher service file $WATCH_SERVICE_FILE"
    say "dry-run: systemctl daemon-reload"
    say "dry-run: systemctl enable --now $WATCH_SERVICE_NAME"
    return 0
  fi

  service_installed || die "Install the server service first with ./launcher.sh install-service."
  [ -f "$APP_DIR/git-watch.sh" ] || die "git-watch.sh not found: $APP_DIR/git-watch.sh"
  write_watch_service_file
  ensure_command_available "systemctl" PULLWISE_SYSTEMCTL_BIN systemctl systemd
  "$(systemctl_bin)" daemon-reload || die "systemctl daemon-reload failed"
  "$(systemctl_bin)" enable --now "$WATCH_SERVICE_NAME" || die "systemctl enable --now failed"
  ok "$WATCH_SERVICE_NAME enabled and started"
}

write_service_file() {
  mkdir -p "$(dirname -- "$SERVICE_FILE")" || die "Unable to create $(dirname -- "$SERVICE_FILE")"
  tmp_file=$SERVICE_FILE.tmp.$$
  render_service_content > "$tmp_file" || die "Unable to render service file."
  mv "$tmp_file" "$SERVICE_FILE" || die "Unable to install service file: $SERVICE_FILE"
  chmod 644 "$SERVICE_FILE" 2>/dev/null || warn "could not chmod 644 $SERVICE_FILE"
  ok "service file written: $SERVICE_FILE"
}

cmd_install_service() {
  dry_run=false
  enable_service=true
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dry-run)
        dry_run=true
        ;;
      --no-enable)
        enable_service=false
        ;;
      *)
        die "Unknown install-service option: $1"
        ;;
    esac
    shift
  done

  if [ "$dry_run" = true ]; then
    say "dry-run: ./launcher.sh sync-env"
    say "dry-run: write service file $SERVICE_FILE"
    say "dry-run: systemctl daemon-reload"
    if [ "$enable_service" = true ]; then
      say "dry-run: systemctl enable $SERVICE_NAME"
    fi
    return 0
  fi

  cmd_sync_env
  write_service_file
  ensure_command_available "systemctl" PULLWISE_SYSTEMCTL_BIN systemctl systemd
  if command -v "$(systemctl_bin)" >/dev/null 2>&1; then
    "$(systemctl_bin)" daemon-reload || die "systemctl daemon-reload failed"
    if [ "$enable_service" = true ]; then
      "$(systemctl_bin)" enable "$SERVICE_NAME" || die "systemctl enable failed"
    fi
  else
    warn "systemctl not found; service file rendered but daemon-reload was skipped"
  fi
}

cmd_start() {
  dry_run=false
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dry-run)
        dry_run=true
        ;;
      *)
        die "Unknown start option: $1"
        ;;
    esac
    shift
  done

  mode=$(manager_mode)
  if [ "$mode" = systemd ]; then
    if [ "$dry_run" = true ]; then
      print_systemctl_command start
      return 0
    fi
    "$(systemctl_bin)" start "$SERVICE_NAME" || die "systemctl start failed"
    ok "$SERVICE_NAME started"
    return 0
  fi

  py=$(python_bin) || die "Unable to find Python. Run ./launcher.sh setup first."
  if [ "$dry_run" = true ]; then
    say "dry-run: server would start with:"
    print_direct_server_command "$py"
    return 0
  fi

  if pid=$(running_pid 2>/dev/null); then
    ok "$APP_NAME already running with pid $pid"
    return 0
  fi

  ensure_runtime_dirs
  cd "$APP_DIR" || die "Unable to enter app directory: $APP_DIR"
  nohup "$py" -m pullwise_server --host "$(host_value)" --port "$(port_value)" >> "$SERVER_OUT_LOG" 2>> "$SERVER_ERR_LOG" &
  pid=$!
  printf '%s\n' "$pid" > "$PID_FILE" || die "Unable to write pid file: $PID_FILE"
  sleep 1
  if ! process_alive "$pid"; then
    fail "Server exited during startup. Check $SERVER_ERR_LOG"
    rm -f "$PID_FILE"
    return 1
  fi
  ok "$APP_NAME started with pid $pid"
  say "health: $(health_url)"
}

cmd_run() {
  py=$(python_bin) || die "Unable to find Python. Run ./launcher.sh setup first."
  ensure_runtime_dirs
  cd "$APP_DIR" || die "Unable to enter app directory: $APP_DIR"
  exec "$py" -m pullwise_server
}

cmd_stop() {
  force=false
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --force|-f)
        force=true
        ;;
      *)
        die "Unknown stop option: $1"
        ;;
    esac
    shift
  done

  if [ "$(manager_mode)" = systemd ]; then
    "$(systemctl_bin)" stop "$SERVICE_NAME" || die "systemctl stop failed"
    ok "$SERVICE_NAME stopped"
    return 0
  fi

  pid=$(read_pid 2>/dev/null || true)
  if [ -z "$pid" ]; then
    ok "$APP_NAME is not running; no pid file found"
    return 0
  fi
  if ! process_alive "$pid"; then
    warn "stale pid file removed: $PID_FILE"
    rm -f "$PID_FILE"
    return 0
  fi
  if ! is_pullwise_process "$pid"; then
    die "PID $pid does not look like a Pullwise server process; refusing to stop it."
  fi

  info "stopping $APP_NAME pid $pid"
  kill "$pid" >/dev/null 2>&1 || die "Unable to send TERM to pid $pid"
  elapsed=0
  while process_alive "$pid"; do
    if [ "$elapsed" -ge "$STOP_TIMEOUT" ]; then
      if [ "$force" = true ]; then
        warn "force killing pid $pid"
        kill -KILL "$pid" >/dev/null 2>&1 || true
        break
      fi
      die "Server did not stop within ${STOP_TIMEOUT}s. Re-run stop --force if needed."
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  rm -f "$PID_FILE"
  ok "$APP_NAME stopped"
}

cmd_restart() {
  if [ "$(manager_mode)" = systemd ]; then
    "$(systemctl_bin)" restart "$SERVICE_NAME" || die "systemctl restart failed"
    ok "$SERVICE_NAME restarted"
    wait_for_restart_health
    return 0
  fi
  cmd_stop "$@"
  cmd_start
}

wait_for_restart_health() {
  is_true "$(env_value PULLWISE_RESTART_WAIT_HEALTH "true")" || return 0

  attempts=$(env_value PULLWISE_RESTART_HEALTH_RETRIES "30")
  delay=$(env_value PULLWISE_RESTART_HEALTH_RETRY_SECONDS "2")
  case "$attempts" in
    ""|*[!0-9]*)
      attempts=30
      ;;
  esac
  [ "$attempts" -gt 0 ] || attempts=30
  case "$delay" in
    ""|*[!0-9]*)
      delay=2
      ;;
  esac

  attempt=1
  while [ "$attempt" -le "$attempts" ]; do
    if run_health_request >/dev/null 2>&1; then
      ok "health endpoint responded after restart"
      return 0
    fi
    if [ "$attempt" -lt "$attempts" ]; then
      info "health not ready after restart; retrying in ${delay}s ($attempt/$attempts)"
      sleep "$delay"
    fi
    attempt=$((attempt + 1))
  done

  run_health_request || die "health check failed after restart: $(health_url)"
  printf '\n'
}

run_health_request() {
  url=$(health_url)
  ensure_command_available "curl" PULLWISE_CURL_BIN curl curl
  curl_bin=$(tool_bin PULLWISE_CURL_BIN curl || true)
  "$curl_bin" -fsS --max-time "${PULLWISE_HEALTH_TIMEOUT_SECONDS:-5}" "$url"
}

cmd_health() {
  run_health_request || die "health check failed: $(health_url)"
  printf '\n'
}

cmd_status() {
  if [ "$(manager_mode)" = systemd ]; then
    if command -v "$(systemctl_bin)" >/dev/null 2>&1; then
      "$(systemctl_bin)" status "$SERVICE_NAME" --no-pager
    else
      say "$SERVICE_NAME: systemd service installed at $SERVICE_FILE"
    fi
    return 0
  fi

  pid=$(read_pid 2>/dev/null || true)
  if [ -z "$pid" ]; then
    say "$APP_NAME: stopped"
    return 0
  fi
  if ! process_alive "$pid"; then
    say "$APP_NAME: stopped (stale pid file: $PID_FILE)"
    return 1
  fi
  if ! is_pullwise_process "$pid"; then
    say "$APP_NAME: unknown process in pid file ($pid)"
    return 2
  fi

  say "$APP_NAME: running"
  say "pid: $pid"
  say "command: $(process_command "$pid")"
  say "health: $(health_url)"
}

latest_app_log() {
  dir=$(log_dir)
  [ -d "$dir" ] || return 1
  find "$dir" -maxdepth 1 -type f -name 'pullwise-*.log' -print 2>/dev/null | sort | tail -n 1
}

tail_file() {
  file=$1
  follow=$2
  if [ ! -f "$file" ]; then
    warn "log file not found: $file"
    return 0
  fi
  if [ "$follow" = true ]; then
    tail -n "${PULLWISE_LOG_LINES:-120}" -f "$file"
  else
    tail -n "${PULLWISE_LOG_LINES:-120}" "$file"
  fi
}

cmd_logs() {
  target=${1:-app}
  follow=false
  if [ "${2:-}" = "--follow" ] || [ "${2:-}" = "-f" ]; then
    follow=true
  fi

  case "$target" in
    journal)
      if [ "$follow" = true ]; then
        "$(journalctl_bin)" -u "$SERVICE_NAME" -n "${PULLWISE_LOG_LINES:-120}" -f --no-pager
      else
        "$(journalctl_bin)" -u "$SERVICE_NAME" -n "${PULLWISE_LOG_LINES:-120}" --no-pager
      fi
      ;;
    server|out)
      tail_file "$SERVER_OUT_LOG" "$follow"
      ;;
    error|err)
      tail_file "$SERVER_ERR_LOG" "$follow"
      ;;
    app)
      app_log=$(latest_app_log || true)
      if [ -z "$app_log" ]; then
        warn "no app log found in $(log_dir)"
        return 0
      fi
      tail_file "$app_log" "$follow"
      ;;
    all)
      say "== server out =="
      tail_file "$SERVER_OUT_LOG" false
      say "== server error =="
      tail_file "$SERVER_ERR_LOG" false
      say "== app =="
      app_log=$(latest_app_log || true)
      if [ -n "$app_log" ]; then
        tail_file "$app_log" false
      else
        warn "no app log found in $(log_dir)"
      fi
      ;;
    *)
      die "Unknown log target: $target"
      ;;
  esac
}

redacted_value() {
  key=$1
  value=$2
  case "$key" in
    *_SECRET|*_SECRET_*|*_PRIVATE_KEY|*_PRIVATE_KEY_*|*_API_KEY|*_TOKEN|*_TOKEN_*|*PASSWORD*)
      if [ -n "$value" ]; then
        printf '<set>'
      else
        printf '<empty>'
      fi
      ;;
    *)
      printf '%s' "$value"
      ;;
  esac
}

print_config_key() {
  key=$1
  default=${2-}
  value=$(env_value "$key" "$default")
  printf '%s=%s\n' "$key" "$(redacted_value "$key" "$value")"
}

cmd_config() {
  say "APP_DIR=$APP_DIR"
  say "LOCAL_ENV_FILE=$LOCAL_ENV_FILE"
  say "SYSTEM_ENV_FILE=$SYSTEM_ENV_FILE"
  say "ENV_FILE=$ENV_FILE"
  say "SERVICE_FILE=$SERVICE_FILE"
  say "SERVICE_NAME=$SERVICE_NAME"
  say "VENV_DIR=$VENV_DIR"
  say "RUN_DIR=$RUN_DIR"
  say "PID_FILE=$PID_FILE"
  py=$(python_bin 2>/dev/null || true)
  say "PYTHON=${py:-<missing>}"
  say "MANAGER=$(manager_mode)"
  print_config_key PULLWISE_MODE "local"
  print_config_key PULLWISE_HOST "0.0.0.0"
  print_config_key PULLWISE_PORT "8080"
  print_config_key PULLWISE_APP_URL "http://localhost:5173"
  print_config_key PULLWISE_ALLOWED_ORIGINS ""
  print_config_key PULLWISE_API_BASE_URL ""
  print_config_key PULLWISE_TRUST_PROXY_HEADERS "false"
  print_config_key PULLWISE_COOKIE_SECURE ""
  print_config_key PULLWISE_DB_PATH "$(db_path)"
  print_config_key PULLWISE_STATE_ENCRYPTION_KEY_PATH "$(state_encryption_key_path)"
  print_config_key PULLWISE_LOG_DIR "$(log_dir)"
  print_config_key PULLWISE_CHECKOUT_ROOT "$(checkout_root)"
  print_config_key PULLWISE_GITHUB_OAUTH_SCOPE "read:user user:email"
  print_config_key PULLWISE_GITHUB_CLIENT_ID ""
  print_config_key PULLWISE_GITHUB_CLIENT_SECRET ""
  print_config_key PULLWISE_GITHUB_APP_SLUG ""
  print_config_key PULLWISE_GITHUB_APP_ID ""
  print_config_key PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH ""
  print_config_key PULLWISE_GITHUB_APP_PRIVATE_KEY_BASE64 ""
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

is_positive_int() {
  case "$1" in
    ""|*[!0-9]*|0)
      return 1
      ;;
    *)
      return 0
      ;;
  esac
}

check_writable_dir() {
  label=$1
  dir=$2
  if mkdir -p "$dir" >/dev/null 2>&1 && [ -w "$dir" ]; then
    ok "$label writable: $dir"
  else
    fail "$label is not writable: $dir"
  fi
}

check_writable_parent() {
  label=$1
  path=$2
  parent=$(dirname -- "$path")
  check_writable_dir "$label parent" "$parent"
}

python_version_ok() {
  version_text=$1
  printf '%s\n' "$version_text" | awk '
    /^Python / {
      split($2, v, ".")
      if (v[1] == 3 && v[2] == 10 && v[3] >= 12) {
        exit 0
      }
    }
    { exit 1 }
  '
}

check_os() {
  os_id=$(read_os_value ID)
  os_version=$(read_os_value VERSION_ID)
  if [ "$os_id" = "ubuntu" ] && [ "$os_version" = "22.04" ]; then
    ok "Ubuntu 22.04 detected"
  elif [ -n "$os_id" ] || [ -n "$os_version" ]; then
    warn "target is Ubuntu 22.04; detected ${os_id:-unknown} ${os_version:-unknown}"
  else
    warn "could not detect OS; target production host should be Ubuntu 22.04"
  fi
}

check_python() {
  py=$(python_bin 2>/dev/null || true)
  if [ -z "$py" ]; then
    fail "Python 3.10.12+ is required; run setup or set PULLWISE_PYTHON_BIN."
    return
  fi
  version=$("$py" --version 2>&1 || true)
  if python_version_ok "$version"; then
    ok "$version at $py"
  else
    fail "Python must be >=3.10.12 and <3.11; found '${version:-unknown}' at $py"
  fi
}

check_required_tool() {
  label=$1
  override=$2
  command_name=$3
  bin=$(tool_bin "$override" "$command_name" || true)
  if [ -z "$bin" ]; then
    fail "$label is required on PATH, or set $override."
    return 1
  fi
  version=$("$bin" --version 2>&1 | sed -n '1p' || true)
  ok "$label available: ${version:-$bin}"
  return 0
}

contains_local_origin() {
  case "$1" in
    *localhost*|*127.0.0.1*|*0.0.0.0*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

contains_wildcard_origin() {
  origins=$1
  old_ifs=$IFS
  IFS=,
  set -f
  for item in $origins; do
    cleaned=$(trim "$item")
    if [ "$cleaned" = "*" ]; then
      set +f
      IFS=$old_ifs
      return 0
    fi
  done
  set +f
  IFS=$old_ifs
  return 1
}

check_production_env() {
  mode=$(env_value PULLWISE_MODE "")
  if [ "$mode" = "production" ]; then
    ok "PULLWISE_MODE=production"
  else
    fail "PULLWISE_MODE must be production for this launcher production audit."
  fi

  port=$(port_value)
  if is_positive_int "$port"; then
    ok "PULLWISE_PORT=$port"
  else
    fail "PULLWISE_PORT must be a positive integer."
  fi

  app_url=$(env_value PULLWISE_APP_URL "")
  case "$app_url" in
    https://*)
      ok "PULLWISE_APP_URL uses HTTPS"
      ;;
    *)
      fail "PULLWISE_APP_URL must be an https:// URL in production."
      ;;
  esac

  origins=$(env_value PULLWISE_ALLOWED_ORIGINS "")
  if [ -z "$origins" ]; then
    fail "PULLWISE_ALLOWED_ORIGINS must list exact trusted HTTPS origins."
  elif contains_wildcard_origin "$origins"; then
    fail "PULLWISE_ALLOWED_ORIGINS must not contain wildcard '*'."
  elif contains_local_origin "$origins"; then
    fail "PULLWISE_ALLOWED_ORIGINS must not contain localhost or local IP origins in production."
  else
    ok "PULLWISE_ALLOWED_ORIGINS is restricted"
  fi

  api_base=$(env_value PULLWISE_API_BASE_URL "")
  trust_proxy=$(env_value PULLWISE_TRUST_PROXY_HEADERS "false")
  if [ -n "$api_base" ]; then
    case "$api_base" in
      https://*)
        ok "PULLWISE_API_BASE_URL uses HTTPS"
        ;;
      *)
        fail "PULLWISE_API_BASE_URL must be an https:// URL in production."
        ;;
    esac
  elif is_true "$trust_proxy"; then
    ok "PULLWISE_TRUST_PROXY_HEADERS enabled; API base can come from proxy headers"
  else
    fail "Set PULLWISE_API_BASE_URL or enable PULLWISE_TRUST_PROXY_HEADERS behind a trusted proxy."
  fi

  if is_true "$(env_value PULLWISE_COOKIE_SECURE "")"; then
    ok "PULLWISE_COOKIE_SECURE=true"
  else
    fail "PULLWISE_COOKIE_SECURE must be true in production."
  fi

}

check_storage() {
  check_writable_parent "SQLite database" "$(db_path)"
  check_writable_dir "Log directory" "$(log_dir)"
  check_writable_dir "Checkout root" "$(checkout_root)"
  check_writable_dir "Run directory" "$RUN_DIR"
}

check_secret_path() {
  key_path=$1
  [ -n "$key_path" ] || return 0
  resolved=$(abs_path "$key_path")
  if [ -r "$resolved" ]; then
    ok "GitHub App private key file is readable"
  else
    fail "PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH is not readable: $resolved"
  fi
  case "$resolved" in
    /etc/pullwise/secrets/*.pem)
      ok "GitHub App private key is under /etc/pullwise/secrets"
      ;;
    "$APP_DIR"/*)
      warn "GitHub App private key is inside the project tree; prefer /etc/pullwise/secrets/github-app-private-key.pem"
      ;;
    *)
      warn "recommended GitHub App private key path: /etc/pullwise/secrets/github-app-private-key.pem"
      ;;
  esac
}

check_state_encryption_key() {
  key_path=$(state_encryption_key_path)
  if [ -z "$key_path" ]; then
    fail "PULLWISE_STATE_ENCRYPTION_KEY_PATH is required in production."
    return
  fi
  if [ -r "$key_path" ]; then
    ok "State encryption key file is readable"
  else
    fail "PULLWISE_STATE_ENCRYPTION_KEY_PATH is not readable: $key_path"
    return
  fi
  case "$key_path" in
    /etc/pullwise/secrets/*)
      ok "State encryption key is under /etc/pullwise/secrets"
      ;;
    "$APP_DIR"/*)
      warn "State encryption key is inside the project tree; prefer /etc/pullwise/secrets/state-encryption-key"
      ;;
    *)
      warn "recommended state encryption key path: /etc/pullwise/secrets/state-encryption-key"
      ;;
  esac

  mode=$(stat -c '%a' "$key_path" 2>/dev/null || true)
  case "$mode" in
    400|440)
      ok "State encryption key mode is $mode"
      ;;
    "")
      warn "could not inspect state encryption key mode; use chmod 400 or chmod 440"
      ;;
    *)
      fail "State encryption key mode must be 400 or 440, found $mode"
      ;;
  esac
}

check_github_config() {
  [ -n "$(env_value PULLWISE_GITHUB_CLIENT_ID "")" ] || fail "PULLWISE_GITHUB_CLIENT_ID is required for real GitHub sign-in."
  [ -n "$(env_value PULLWISE_GITHUB_CLIENT_SECRET "")" ] || fail "PULLWISE_GITHUB_CLIENT_SECRET is required for real GitHub sign-in."
  [ -n "$(env_value PULLWISE_GITHUB_APP_SLUG "")" ] || fail "PULLWISE_GITHUB_APP_SLUG is required for repository installs."
  [ -n "$(env_value PULLWISE_GITHUB_APP_ID "")" ] || fail "PULLWISE_GITHUB_APP_ID is required for installation tokens."

  key_path=$(env_value PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH "")
  key_base64=$(env_value PULLWISE_GITHUB_APP_PRIVATE_KEY_BASE64 "")
  key_direct=$(env_value PULLWISE_GITHUB_APP_PRIVATE_KEY "")
  if [ -n "$key_base64" ] || [ -n "$key_direct" ]; then
    ok "GitHub App private key is configured through environment"
  elif [ -n "$key_path" ]; then
    check_secret_path "$key_path"
  else
    fail "Set PULLWISE_GITHUB_APP_PRIVATE_KEY_BASE64 or PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH."
  fi
}

check_scan_limits() {
  ok "scan limits are managed by server system configuration"
}

check_billing_config() {
  creem=false
  if [ -n "$(env_value PULLWISE_CREEM_API_KEY "")" ] || [ -n "$(env_value PULLWISE_CREEM_WEBHOOK_SECRET "")" ]; then
    creem=true
  fi
  provider=$(env_value PULLWISE_BILLING_PROVIDER "")

  if [ -n "$provider" ]; then
    case "$provider" in
      creem)
        ok "PULLWISE_BILLING_PROVIDER=creem"
        ;;
      *)
        fail "PULLWISE_BILLING_PROVIDER must be creem."
        ;;
    esac
  elif [ "$creem" = true ]; then
    ok "billing provider inferred from Creem environment"
  else
    warn "billing provider is not configured; billing routes will not create checkout sessions"
  fi

  validate_creem=false
  if [ "$provider" = "creem" ]; then
    validate_creem=true
  elif [ -z "$provider" ] && [ "$creem" = true ]; then
    validate_creem=true
  fi

  if [ "$validate_creem" = true ]; then
    [ -n "$(env_value PULLWISE_CREEM_API_KEY "")" ] || fail "PULLWISE_CREEM_API_KEY is required for Creem billing."
    [ -n "$(env_value PULLWISE_CREEM_WEBHOOK_SECRET "")" ] || warn "PULLWISE_CREEM_WEBHOOK_SECRET is not configured; Creem webhooks will be rejected"
    ok "Creem secrets are configured; product catalog is managed by server system configuration"
  fi
}

check_service_config() {
  if service_installed; then
    if grep -F "EnvironmentFile=$SYSTEM_ENV_FILE" "$SERVICE_FILE" >/dev/null 2>&1; then
      ok "systemd service reads $SYSTEM_ENV_FILE"
    else
      fail "systemd service must use EnvironmentFile=$SYSTEM_ENV_FILE"
    fi
  else
    fail "systemd service is not installed; run ./launcher.sh install-service"
  fi
}

check_health_if_running() {
  if pid=$(running_pid 2>/dev/null); then
    if cmd_health >/dev/null 2>&1; then
      ok "health endpoint responded for pid $pid"
    else
      fail "server pid $pid is running but health endpoint did not respond: $(health_url)"
    fi
  else
    warn "server is not currently running; start it after doctor passes"
  fi
}

cmd_doctor() {
  fail_count=0
  warn_count=0

  say "Pullwise production doctor"
  say "app: $APP_DIR"
  say "env: $ENV_FILE"
  say "system env: $SYSTEM_ENV_FILE"
  say "service: $SERVICE_FILE"

  if [ -f "$ENV_FILE" ]; then
    ok "env file found"
  else
    fail "env file not found: $ENV_FILE"
  fi

  check_os
  check_python
  check_required_tool "git" PULLWISE_GIT_BIN git >/dev/null
  check_production_env
  check_storage
  check_state_encryption_key
  check_github_config
  check_scan_limits
  check_billing_config
  check_service_config
  check_health_if_running

  if [ "$fail_count" -gt 0 ]; then
    say "doctor checks failed: $fail_count failure(s), $warn_count warning(s)"
    return 1
  fi
  say "doctor checks passed: $warn_count warning(s)"
}

copy_dir_contents() {
  src=$1
  dst=$2
  [ -d "$src" ] || return 0
  mkdir -p "$dst" || die "Unable to create directory: $dst"
  cp -R "$src"/. "$dst"/ || die "Unable to copy $src -> $dst"
}

copy_file_if_exists() {
  src=$1
  dst=$2
  [ -f "$src" ] || return 0
  mkdir -p "$(dirname -- "$dst")" || die "Unable to create $(dirname -- "$dst")"
  cp "$src" "$dst" || die "Unable to copy $src -> $dst"
}

remove_staged_state_encryption_key() {
  stage_state_dir=$1
  key_path=$(state_encryption_key_path)
  [ -n "$key_path" ] || return 0
  app_state_dir=$(abs_path "$APP_DIR/.pullwise")
  case "$key_path" in
    "$app_state_dir"/*)
      rel=${key_path#"$app_state_dir"/}
      rm -f "$stage_state_dir/$rel" 2>/dev/null || true
      ;;
  esac
}

cmd_export() {
  archive=
  include_secrets=0
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --include-secrets)
        include_secrets=1
        ;;
      -h|--help)
        die "Usage: ./launcher.sh export [--include-secrets] <archive.tar.gz>"
        ;;
      -*)
        die "Unknown export option: $1"
        ;;
      *)
        if [ -n "$archive" ]; then
          die "Usage: ./launcher.sh export [--include-secrets] <archive.tar.gz>"
        fi
        archive=$1
        ;;
    esac
    shift
  done
  [ -n "$archive" ] || die "Usage: ./launcher.sh export [--include-secrets] <archive.tar.gz>"
  archive=$(abs_path "$archive")
  if [ "$include_secrets" = "1" ] && [ ! -f "$ENV_FILE" ]; then
    die "env file not found: $ENV_FILE"
  fi
  mkdir -p "$(dirname -- "$archive")" || die "Unable to create archive directory"

  stage=$(mktemp -d "${TMPDIR:-/tmp}/pullwise-export.XXXXXX") || die "Unable to create temp directory"
  if [ "$include_secrets" = "1" ]; then
    mkdir -p "$stage/config"
    cp "$ENV_FILE" "$stage/config/server.env" || die "Unable to stage env file"
  else
    warn "export excludes server.env and GitHub App private key; pass --include-secrets for a restorable migration package"
  fi
  cat > "$stage/manifest.env" <<MANIFEST
PULLWISE_EXPORT_VERSION=1
APP_NAME=$APP_NAME
EXPORTED_AT=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)
PULLWISE_EXPORT_SECRETS_INCLUDED=$include_secrets
MANIFEST

  db=$(db_path)
  mkdir -p "$stage/data"
  copy_file_if_exists "$db" "$stage/data/$(basename -- "$db")"
  copy_file_if_exists "$db-wal" "$stage/data/$(basename -- "$db")-wal"
  copy_file_if_exists "$db-shm" "$stage/data/$(basename -- "$db")-shm"

  mkdir -p "$stage/logs"
  copy_dir_contents "$(log_dir)" "$stage/logs"

  mkdir -p "$stage/checkouts"
  copy_dir_contents "$(checkout_root)" "$stage/checkouts"

  if [ "$include_secrets" = "1" ]; then
    key_path=$(env_value PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH "")
    if [ -n "$key_path" ]; then
      resolved_key=$(abs_path "$key_path")
      if [ -r "$resolved_key" ]; then
        mkdir -p "$stage/secrets"
        cp "$resolved_key" "$stage/secrets/$(basename -- "$resolved_key")" || die "Unable to stage private key"
      else
        warn "private key path configured but not readable, skipped: $resolved_key"
      fi
    fi
  fi

  if [ -d "$APP_DIR/.pullwise" ]; then
    mkdir -p "$stage/pullwise-state"
    copy_dir_contents "$APP_DIR/.pullwise" "$stage/pullwise-state"
    remove_staged_state_encryption_key "$stage/pullwise-state"
    find "$stage/pullwise-state/run" -type f -name '*.pid' -delete 2>/dev/null || true
  fi

  items="manifest.env"
  [ -d "$stage/config" ] && items="$items config"
  [ -d "$stage/data" ] && items="$items data"
  [ -d "$stage/logs" ] && items="$items logs"
  [ -d "$stage/checkouts" ] && items="$items checkouts"
  [ -d "$stage/secrets" ] && items="$items secrets"
  [ -d "$stage/pullwise-state" ] && items="$items pullwise-state"
  (cd "$stage" && "$(tar_bin)" -czf "$archive" $items) || die "Unable to create archive: $archive"
  rm -rf "$stage"
  ok "exported migration package: $archive"
}

first_file_in_dir() {
  dir=$1
  [ -d "$dir" ] || return 1
  find "$dir" -type f -print 2>/dev/null | sort | sed -n '1p'
}

restore_data_files() {
  extracted=$1
  imported_env=$2
  db_dest=$(path_from_env_file "$imported_env" PULLWISE_DB_PATH "$APP_DIR/.pullwise/pullwise.sqlite3")
  db_base=$(basename -- "$db_dest")
  if [ -f "$extracted/data/$db_base" ]; then
    copy_file_if_exists "$extracted/data/$db_base" "$db_dest"
    copy_file_if_exists "$extracted/data/$db_base-wal" "$db_dest-wal"
    copy_file_if_exists "$extracted/data/$db_base-shm" "$db_dest-shm"
  else
    copy_dir_contents "$extracted/data" "$(dirname -- "$db_dest")"
  fi

  log_dest=$(path_from_env_file "$imported_env" PULLWISE_LOG_DIR "$APP_DIR/.pullwise/logs")
  copy_dir_contents "$extracted/logs" "$log_dest"

  checkout_dest=$(path_from_env_file "$imported_env" PULLWISE_CHECKOUT_ROOT "$APP_DIR/.pullwise/checkouts")
  copy_dir_contents "$extracted/checkouts" "$checkout_dest"

  copy_dir_contents "$extracted/pullwise-state" "$APP_DIR/.pullwise"
  find "$APP_DIR/.pullwise/run" -type f -name '*.pid' -delete 2>/dev/null || true
}

restore_secret_file() {
  extracted=$1
  imported_env=$2
  key_path=$(env_file_value "$imported_env" PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH "")
  [ -n "$key_path" ] || return 0
  key_dest=$(abs_path "$key_path")
  key_base=$(basename -- "$key_dest")
  if [ -f "$extracted/secrets/$key_base" ]; then
    copy_file_if_exists "$extracted/secrets/$key_base" "$key_dest"
  else
    first_secret=$(first_file_in_dir "$extracted/secrets" || true)
    if [ -n "$first_secret" ]; then
      copy_file_if_exists "$first_secret" "$key_dest"
    fi
  fi
  if [ -f "$key_dest" ]; then
    set_service_group_readable_file "$key_dest"
  fi
}

unsafe_archive_member() {
  member=$1
  case "$member" in
    ""|/*|..|../*|*/../*|*/..)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

validate_archive_members() {
  archive=$1
  list_file=$(mktemp "${TMPDIR:-/tmp}/pullwise-archive-list.XXXXXX") || die "Unable to create temp file"
  error_file=$(mktemp "${TMPDIR:-/tmp}/pullwise-archive-list-error.XXXXXX") || die "Unable to create temp file"
  verbose_file=$(mktemp "${TMPDIR:-/tmp}/pullwise-archive-verbose.XXXXXX") || die "Unable to create temp file"

  if ! "$(tar_bin)" -tzf "$archive" > "$list_file" 2> "$error_file"; then
    if grep -F "Member name contains '..'" "$error_file" >/dev/null 2>&1 || grep -F "Removing leading" "$error_file" >/dev/null 2>&1; then
      rm -f "$list_file" "$error_file" "$verbose_file"
      die "unsafe archive member in migration package: $archive"
    fi
    detail=$(sed -n '1p' "$error_file")
    rm -f "$list_file" "$error_file" "$verbose_file"
    die "Unable to inspect archive: ${detail:-$archive}"
  fi

  while IFS= read -r member || [ -n "$member" ]; do
    if unsafe_archive_member "$member"; then
      rm -f "$list_file" "$error_file" "$verbose_file"
      die "unsafe archive member in migration package: $member"
    fi
  done < "$list_file"

  if ! "$(tar_bin)" -tvzf "$archive" > "$verbose_file" 2> "$error_file"; then
    detail=$(sed -n '1p' "$error_file")
    rm -f "$list_file" "$error_file" "$verbose_file"
    die "Unable to inspect archive member types: ${detail:-$archive}"
  fi

  while IFS= read -r line || [ -n "$line" ]; do
    type_char=$(printf '%s' "$line" | cut -c 1)
    case "$type_char" in
      -|d)
        ;;
      *)
        rm -f "$list_file" "$error_file" "$verbose_file"
        die "unsafe archive member type in migration package: $line"
        ;;
    esac
  done < "$verbose_file"

  rm -f "$list_file" "$error_file" "$verbose_file"
}

cmd_import() {
  archive=${1:-}
  [ -n "$archive" ] || die "Usage: ./launcher.sh import <archive.tar.gz>"
  [ -f "$archive" ] || die "archive not found: $archive"

  validate_archive_members "$archive"
  extracted=$(mktemp -d "${TMPDIR:-/tmp}/pullwise-import.XXXXXX") || die "Unable to create temp directory"
  "$(tar_bin)" -xzf "$archive" -C "$extracted" || die "Unable to extract archive"
  imported_env=$extracted/config/server.env
  [ -f "$imported_env" ] || die "archive does not contain config/server.env"

  mkdir -p "$(dirname -- "$SYSTEM_ENV_FILE")" || die "Unable to create $(dirname -- "$SYSTEM_ENV_FILE")"
  cp "$imported_env" "$SYSTEM_ENV_FILE" || die "Unable to restore env file to $SYSTEM_ENV_FILE"
  set_service_group_readable_file "$SYSTEM_ENV_FILE"

  restore_data_files "$extracted" "$imported_env"
  restore_secret_file "$extracted" "$imported_env"
  write_service_file

  if command -v "$(systemctl_bin)" >/dev/null 2>&1; then
    "$(systemctl_bin)" daemon-reload || warn "systemctl daemon-reload failed; run it manually"
  fi

  rm -rf "$extracted"
  ok "imported migration package: $archive"
}

main() {
  load_env_file
  command=${1:-help}
  if [ "$#" -gt 0 ]; then
    shift
  fi

  case "$command" in
    help|-h|--help)
      usage
      ;;
    setup|install)
      cmd_setup "$@"
      ;;
    init-env|env-template)
      cmd_init_env "$@"
      ;;
    sync-env)
      cmd_sync_env "$@"
      ;;
    render-service)
      cmd_render_service "$@"
      ;;
    install-service)
      cmd_install_service "$@"
      ;;
    render-watch-service)
      cmd_render_watch_service "$@"
      ;;
    install-watch-service)
      cmd_install_watch_service "$@"
      ;;
    start)
      cmd_start "$@"
      ;;
    run|foreground)
      cmd_run "$@"
      ;;
    stop)
      cmd_stop "$@"
      ;;
    restart)
      cmd_restart "$@"
      ;;
    status)
      cmd_status "$@"
      ;;
    health)
      cmd_health "$@"
      ;;
    logs|log)
      cmd_logs "$@"
      ;;
    doctor|audit)
      cmd_doctor "$@"
      ;;
    config|env)
      cmd_config "$@"
      ;;
    export)
      cmd_export "$@"
      ;;
    import)
      cmd_import "$@"
      ;;
    *)
      usage >&2
      die "Unknown command: $command"
      ;;
  esac
}

main "$@"
