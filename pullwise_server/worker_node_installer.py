from __future__ import annotations


def render() -> str:
    return r'''#!/usr/bin/env bash
set -euo pipefail

NODE_VERSION="22.23.1"
SERVER_URL=""
WORKER_ID=""
WORKER_NAME="Pullwise Worker"
WORKER_PACKAGE=""
WORKER_TOKEN="${PULLWISE_WORKER_TOKEN:-}"
BOOTSTRAP_TOKEN="${PULLWISE_WORKER_BOOTSTRAP_TOKEN:-}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --server) SERVER_URL="${2:-}"; shift 2 ;;
    --worker-id) WORKER_ID="${2:-}"; shift 2 ;;
    --worker-name) WORKER_NAME="${2:-}"; shift 2 ;;
    --package) WORKER_PACKAGE="${2:-}"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "Run this installer as root." >&2; exit 1; }
[ -n "$SERVER_URL" ] || { echo "--server is required" >&2; exit 2; }
[ -n "$WORKER_ID" ] || { echo "--worker-id is required" >&2; exit 2; }
[ -n "$WORKER_PACKAGE" ] || { echo "--package is required" >&2; exit 2; }
if [ -n "$WORKER_TOKEN" ] && [ -n "$BOOTSTRAP_TOKEN" ]; then
  echo "Set only one of PULLWISE_WORKER_TOKEN or PULLWISE_WORKER_BOOTSTRAP_TOKEN." >&2
  exit 2
fi
[ -n "$WORKER_TOKEN" ] || [ -n "$BOOTSTRAP_TOKEN" ] || {
  echo "PULLWISE_WORKER_TOKEN or PULLWISE_WORKER_BOOTSTRAP_TOKEN is required" >&2
  exit 2
}
case "$SERVER_URL" in http://*|https://*) ;; *) echo "--server must be HTTP(S)" >&2; exit 2 ;; esac
case "$WORKER_ID" in *[!A-Za-z0-9_-]*|'') echo "worker id is unsafe" >&2; exit 2 ;; esac
case "$SERVER_URL" in *[!A-Za-z0-9._:/-]*) echo "server URL is unsafe" >&2; exit 2 ;; esac
case "$WORKER_TOKEN$BOOTSTRAP_TOKEN" in *[!A-Za-z0-9_-]*) echo "worker token is unsafe" >&2; exit 2 ;; esac

safe_worker_id() {
  local value="$1"
  if [ "${#value}" -le 48 ]; then
    printf '%s' "$value"
    return
  fi
  local digest
  digest="$(printf '%s' "$value" | sha256sum | cut -c1-10)"
  printf '%s-%s' "${value:0:37}" "$digest"
}

SAFE_ID="$(safe_worker_id "$WORKER_ID")"
SERVICE_USER="pww-$(printf '%s' "$WORKER_ID" | sha256sum | cut -c1-24)"
SERVICE_HOME="/var/lib/pullwise-worker/${SAFE_ID}"
RUNTIME_ROOT="${SERVICE_HOME}/workers/${SAFE_ID}"
APP_ROOT="${RUNTIME_ROOT}/app"
INITIAL_APP="${RUNTIME_ROOT}/versions/initial"
NODE_ROOT="${RUNTIME_ROOT}/node"
PROFILE_ROOT="${RUNTIME_ROOT}/pi-profiles"
STATE_ROOT="${RUNTIME_ROOT}/state"
CHECKOUT_ROOT="${SERVICE_HOME}/checkouts"
CONFIG_DIR="/etc/pullwise-worker/${SAFE_ID}"
ENV_FILE="${CONFIG_DIR}/worker.env"
BIN_PATH="/usr/local/bin/pullwise-worker-${SAFE_ID}"
WATCHER_SERVICE="pullwise-worker-watcher-${SAFE_ID}"
WATCHER_UNIT="/etc/systemd/system/${WATCHER_SERVICE}.service"
WORKER_SERVICE="pullwise-worker-${SAFE_ID}"
WORKER_UNIT="/etc/systemd/system/${WORKER_SERVICE}.service"
[ ! -e "$ENV_FILE" ] || { echo "Instance already installed; use its update command." >&2; exit 1; }
[ ! -e "$SERVICE_HOME" ] && [ ! -L "$SERVICE_HOME" ] || { echo "Instance home already exists; inspect it before installing." >&2; exit 1; }

if [ -r /etc/os-release ]; then
  . /etc/os-release
  [ "${ID:-}" = "ubuntu" ] && [ "${VERSION_ID:-}" = "22.04" ] || {
    echo "Pullwise Worker requires Ubuntu 22.04." >&2
    exit 1
  }
fi

command -v curl >/dev/null 2>&1 || { apt-get update; apt-get install -y curl ca-certificates xz-utils; }
command -v sha256sum >/dev/null 2>&1 || { apt-get update; apt-get install -y coreutils; }
command -v git >/dev/null 2>&1 || { apt-get update; apt-get install -y git; }

case "$(uname -m)" in
  x86_64) NODE_ARCH="x64" ;;
  aarch64|arm64) NODE_ARCH="arm64" ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

if id "$SERVICE_USER" >/dev/null 2>&1; then
  EXISTING_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
  [ "$EXISTING_HOME" = "$SERVICE_HOME" ] || {
    echo "Existing service user has a different home: $EXISTING_HOME" >&2
    exit 1
  }
else
  useradd --system --home-dir "$SERVICE_HOME" --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -m 0755 -o root -g root /var/lib/pullwise-worker /etc/pullwise-worker
install -d -m 0750 -o root -g "$SERVICE_USER" "$SERVICE_HOME" "$SERVICE_HOME/workers" "$RUNTIME_ROOT" "$RUNTIME_ROOT/versions" "$INITIAL_APP" "$PROFILE_ROOT"
install -d -m 0700 -o root -g root "$RUNTIME_ROOT/host-state" "$RUNTIME_ROOT/watcher-home"
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_ROOT" "$CHECKOUT_ROOT" "$RUNTIME_ROOT/worker-home"
install -d -m 0750 -o root -g "$SERVICE_USER" "$CONFIG_DIR"
ln -s "$INITIAL_APP" "$APP_ROOT"

if [ ! -x "$NODE_ROOT/bin/node" ]; then
  TEMP_DIR="$(mktemp -d)"
  trap 'rm -rf "$TEMP_DIR"' EXIT
  ARCHIVE="node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz"
  BASE_URL="https://nodejs.org/dist/v${NODE_VERSION}"
  curl -fsSL "${BASE_URL}/${ARCHIVE}" -o "${TEMP_DIR}/${ARCHIVE}"
  curl -fsSL "${BASE_URL}/SHASUMS256.txt" -o "${TEMP_DIR}/SHASUMS256.txt"
  (cd "$TEMP_DIR" && grep "  ${ARCHIVE}$" SHASUMS256.txt | sha256sum -c -)
  rm -rf "$NODE_ROOT"
  install -d -m 0755 "$NODE_ROOT"
  tar -xJf "${TEMP_DIR}/${ARCHIVE}" --no-same-owner --strip-components=1 -C "$NODE_ROOT"
fi

"$NODE_ROOT/bin/node" -e 'const [major,minor]=process.versions.node.split(".").map(Number);if(major<22||(major===22&&minor<19))process.exit(1)'
export PATH="$NODE_ROOT/bin:/usr/sbin:/usr/bin:/sbin:/bin"
"$NODE_ROOT/bin/npm" install --prefix "$APP_ROOT" --omit=dev --ignore-scripts --no-audit --no-fund "$WORKER_PACKAGE"
"$NODE_ROOT/bin/node" "$APP_ROOT/node_modules/pullwise-worker/dist/main.js" self-check

if [ -z "$WORKER_TOKEN" ]; then
  WORKER_TOKEN="$(
    PULLWISE_SERVER_URL="$SERVER_URL" \
    PULLWISE_WORKER_ID="$WORKER_ID" \
    PULLWISE_WORKER_BOOTSTRAP_TOKEN="$BOOTSTRAP_TOKEN" \
    "$NODE_ROOT/bin/node" "$APP_ROOT/node_modules/pullwise-worker/dist/main.js" bootstrap
  )"
  case "$WORKER_TOKEN" in pww_*) ;; *) echo "Worker bootstrap exchange failed." >&2; exit 1 ;; esac
fi
unset BOOTSTRAP_TOKEN PULLWISE_WORKER_BOOTSTRAP_TOKEN

cat >"$ENV_FILE" <<EOF
PULLWISE_SERVER_URL=$SERVER_URL
PULLWISE_WORKER_ID=$WORKER_ID
PULLWISE_WORKER_TOKEN=$WORKER_TOKEN
PULLWISE_MANAGED_HOST=1
PULLWISE_PI_PROFILE_ROOT=$PROFILE_ROOT
PULLWISE_WORKER_STATE_ROOT=$STATE_ROOT
PULLWISE_CHECKOUT_ROOT=$CHECKOUT_ROOT
PATH=$APP_ROOT/node_modules/.bin:$NODE_ROOT/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EOF
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

cat >"$BIN_PATH" <<EOF
#!/bin/sh
set -a
. "$ENV_FILE"
set +a
exec "$NODE_ROOT/bin/node" "$APP_ROOT/node_modules/pullwise-worker/dist/main.js" "\$@"
EOF
chmod 0755 "$BIN_PATH"

cat >"$WATCHER_UNIT" <<'EOF'
[Unit]
Description=Pullwise Worker Watcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=__SERVICE_USER__
UMask=0027
Environment=HOME=__RUNTIME_ROOT__/watcher-home
EnvironmentFile=__ENV_FILE__
ExecStart=__BIN_PATH__ watch
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
KillMode=control-group

[Install]
WantedBy=multi-user.target
EOF
sed -i \
  -e "s|__SERVICE_USER__|$SERVICE_USER|g" \
  -e "s|__ENV_FILE__|$ENV_FILE|g" \
  -e "s|__BIN_PATH__|$BIN_PATH|g" \
  -e "s|__RUNTIME_ROOT__|$RUNTIME_ROOT|g" \
  "$WATCHER_UNIT"

cat >"$WORKER_UNIT" <<'EOF'
[Unit]
Description=Pullwise Node Pi Worker
After=network-online.target __WATCHER_SERVICE__.service
Wants=network-online.target __WATCHER_SERVICE__.service

[Service]
Type=simple
User=__SERVICE_USER__
Group=__SERVICE_USER__
UMask=0077
Environment=HOME=__RUNTIME_ROOT__/worker-home
EnvironmentFile=__ENV_FILE__
ExecStart=__BIN_PATH__ serve
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=__RUNTIME_ROOT__/state __RUNTIME_ROOT__/worker-home __CHECKOUT_ROOT__
KillMode=control-group
TimeoutStopSec=90

[Install]
WantedBy=multi-user.target
EOF
sed -i \
  -e "s|__SERVICE_USER__|$SERVICE_USER|g" \
  -e "s|__ENV_FILE__|$ENV_FILE|g" \
  -e "s|__BIN_PATH__|$BIN_PATH|g" \
  -e "s|__RUNTIME_ROOT__|$RUNTIME_ROOT|g" \
  -e "s|__CHECKOUT_ROOT__|$CHECKOUT_ROOT|g" \
  -e "s|__WATCHER_SERVICE__|$WATCHER_SERVICE|g" \
  "$WORKER_UNIT"

systemctl daemon-reload
systemctl enable --now "$WATCHER_SERVICE"
systemctl enable --now "$WORKER_SERVICE"
echo "Installed $WORKER_NAME ($WORKER_ID). Profiles are managed by Pullwise Model Gateway."
'''
