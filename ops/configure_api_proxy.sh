#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/server-pullwise}"
SERVICE_NAME="${SERVICE_NAME:-pullwise-server}"
SERVICE_USER="${SERVICE_USER:-pullwise}"
SERVICE_GROUP="${SERVICE_GROUP:-pullwise}"
API_DOMAIN="${API_DOMAIN:-api.pull-wise.com}"
ROOT_DOMAIN="${ROOT_DOMAIN:-pull-wise.com}"
APP_ORIGIN="${APP_ORIGIN:-https://pull-wise.com}"
ADMIN_ORIGIN="${ADMIN_ORIGIN:-https://admin.${ROOT_DOMAIN}}"
ALLOWED_ORIGINS="${ALLOWED_ORIGINS:-${APP_ORIGIN},${ADMIN_ORIGIN}}"
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-18080}"
CERT_NAME="${CERT_NAME:-pull-wise.com}"
ENV_FILE="${ENV_FILE:-/etc/pullwise/server.env}"
CLOUDFLARE_CREDENTIALS_FILE="${CLOUDFLARE_CREDENTIALS_FILE:-/etc/letsencrypt/cloudflare.ini}"
SYNC_CLOUDFLARE_DNS="${SYNC_CLOUDFLARE_DNS:-true}"
NGINX_SITE="/etc/nginx/sites-available/${API_DOMAIN}.conf"

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root." >&2
    exit 1
  fi
}

read_os_value() {
  local key="$1"
  local os_file="${PULLWISE_API_PROXY_OS_RELEASE_FILE:-/etc/os-release}"
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

is_ubuntu_2204() {
  [ "$(read_os_value ID)" = "ubuntu" ] && [ "$(read_os_value VERSION_ID)" = "22.04" ]
}

apt_get_bin() {
  if [ -n "${PULLWISE_API_PROXY_APT_GET_BIN:-}" ]; then
    printf '%s' "$PULLWISE_API_PROXY_APT_GET_BIN"
    return 0
  fi
  command -v apt-get 2>/dev/null
}

install_ubuntu_packages() {
  local packages=("$@")
  [ "${#packages[@]}" -gt 0 ] || return 0
  if is_true "${PULLWISE_API_PROXY_SKIP_DEPENDENCY_INSTALL:-false}"; then
    echo "Missing dependencies: ${packages[*]}. Dependency auto-install is disabled by PULLWISE_API_PROXY_SKIP_DEPENDENCY_INSTALL." >&2
    exit 1
  fi
  if ! is_ubuntu_2204; then
    echo "Missing dependencies: ${packages[*]}. Automatic installation is supported on Ubuntu 22.04 hosts." >&2
    exit 1
  fi
  local apt_get
  apt_get="$(apt_get_bin)"
  if [ -z "$apt_get" ]; then
    echo "Missing dependencies: ${packages[*]}. apt-get is required for Ubuntu 22.04 dependency installation." >&2
    exit 1
  fi
  echo "Installing Ubuntu packages: ${packages[*]}"
  "$apt_get" update
  DEBIAN_FRONTEND=noninteractive "$apt_get" install -y --no-install-recommends "${packages[@]}"
}

ensure_command_available() {
  local label="$1"
  local command_name="$2"
  shift 2
  if command -v "$command_name" >/dev/null 2>&1; then
    return 0
  fi
  install_ubuntu_packages "$@"
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "${label} is still unavailable after installing: $*" >&2
    exit 1
  fi
}

ensure_host_dependencies() {
  ensure_command_available "getent" getent libc-bin
  ensure_command_available "groupadd" groupadd passwd
  ensure_command_available "useradd" useradd passwd
  ensure_command_available "install" install coreutils
  ensure_command_available "curl" curl curl
  ensure_command_available "python3.10" python3.10 python3.10 python3.10-venv python3-pip
  ensure_command_available "systemctl" systemctl systemd
  ensure_command_available "nginx" nginx nginx
  ensure_command_available "certbot" certbot certbot python3-certbot-dns-cloudflare
}

ensure_user_and_dirs() {
  if ! getent group "$SERVICE_GROUP" >/dev/null; then
    groupadd --system "$SERVICE_GROUP"
  fi
  if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin --gid "$SERVICE_GROUP" "$SERVICE_USER"
  fi

  install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" /var/lib/pullwise /var/lib/pullwise/checkouts
  install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" /var/log/pullwise
  install -d -m 0750 -o root -g "$SERVICE_GROUP" /etc/pullwise /etc/pullwise/secrets
}

upsert_env() {
  local key="$1"
  local value="$2"
  touch "$ENV_FILE"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

write_env_defaults() {
  touch "$ENV_FILE"
  chmod 0640 "$ENV_FILE"
  chown root:"$SERVICE_GROUP" "$ENV_FILE"

  upsert_env PULLWISE_MODE production
  upsert_env PULLWISE_HOST "$BACKEND_HOST"
  upsert_env PULLWISE_PORT "$BACKEND_PORT"
  upsert_env PULLWISE_APP_URL "$APP_ORIGIN"
  upsert_env PULLWISE_ALLOWED_ORIGINS "$ALLOWED_ORIGINS"
  upsert_env PULLWISE_API_BASE_URL "https://${API_DOMAIN}"
  upsert_env PULLWISE_TRUST_PROXY_HEADERS true
  upsert_env PULLWISE_COOKIE_SECURE true
  upsert_env PULLWISE_DB_PATH /var/lib/pullwise/pullwise.sqlite3
  upsert_env PULLWISE_LOG_DIR /var/log/pullwise
  upsert_env PULLWISE_CHECKOUT_ROOT /var/lib/pullwise/checkouts
  upsert_env PULLWISE_LOG_LEVEL INFO
  upsert_env PULLWISE_LOG_ROTATION_TIME "00:00"
  upsert_env PULLWISE_RATE_LIMIT_ENABLED true
  upsert_env PULLWISE_RATE_LIMIT_REQUESTS 600
  upsert_env PULLWISE_RATE_LIMIT_WINDOW_SECONDS 60
  upsert_env PULLWISE_MAX_RUNNING_SCANS_PER_USER 1

  if ! grep -qE '^PULLWISE_REVIEW_PROVIDER=' "$ENV_FILE"; then
    upsert_env PULLWISE_REVIEW_PROVIDER disabled
  fi
}

write_systemd_service() {
  cat >"/etc/systemd/system/${SERVICE_NAME}.service" <<SERVICE
[Unit]
Description=Pullwise Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/.venv/bin/python -m pullwise_server
Restart=always
RestartSec=5
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
UMask=007
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE

  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME" >/dev/null
}

write_cloudflare_credentials_if_present() {
  if [ -z "${CLOUDFLARE_API_TOKEN:-}" ]; then
    return 0
  fi
  install -d -m 0700 /etc/letsencrypt
  cat >"$CLOUDFLARE_CREDENTIALS_FILE" <<EOF
dns_cloudflare_api_token = ${CLOUDFLARE_API_TOKEN}
EOF
  chmod 0600 "$CLOUDFLARE_CREDENTIALS_FILE"
}

sync_cloudflare_dns_if_possible() {
  if [ "$SYNC_CLOUDFLARE_DNS" != "true" ] || [ -z "${CLOUDFLARE_API_TOKEN:-}" ]; then
    return 0
  fi

  local server_ip="${SERVER_IP:-}"
  if [ -z "$server_ip" ]; then
    server_ip="$(curl -fsS https://api.ipify.org)"
  fi

  CLOUDFLARE_API_TOKEN="$CLOUDFLARE_API_TOKEN" \
  ROOT_DOMAIN="$ROOT_DOMAIN" \
  API_DOMAIN="$API_DOMAIN" \
  SERVER_IP="$server_ip" \
  python3.10 <<'PY'
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

token = os.environ["CLOUDFLARE_API_TOKEN"]
root_domain = os.environ["ROOT_DOMAIN"]
api_domain = os.environ["API_DOMAIN"]
server_ip = os.environ["SERVER_IP"]
base = "https://api.cloudflare.com/client/v4"
headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
}


def request(method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Cloudflare API {method} {path} failed: {exc.code} {detail}") from exc
    result = json.loads(body)
    if not result.get("success"):
        raise SystemExit(f"Cloudflare API {method} {path} failed: {body}")
    return result["result"]


zone_query = urllib.parse.urlencode({"name": root_domain})
zones = request("GET", f"/zones?{zone_query}")
if not zones:
    raise SystemExit(f"Cloudflare zone not found: {root_domain}")
zone_id = zones[0]["id"]

record_query = urllib.parse.urlencode({"type": "A", "name": api_domain})
records = request("GET", f"/zones/{zone_id}/dns_records?{record_query}")
payload = {
    "type": "A",
    "name": api_domain,
    "content": server_ip,
    "ttl": 1,
    "proxied": False,
}
if records:
    record_id = records[0]["id"]
    request("PUT", f"/zones/{zone_id}/dns_records/{record_id}", payload)
    print(f"Updated Cloudflare DNS A {api_domain} -> {server_ip}")
else:
    request("POST", f"/zones/{zone_id}/dns_records", payload)
    print(f"Created Cloudflare DNS A {api_domain} -> {server_ip}")
PY
}

issue_certificate_if_possible() {
  if [ -z "${CLOUDFLARE_API_TOKEN:-}" ] || [ -z "${LETSENCRYPT_EMAIL:-}" ]; then
    echo "Skipping certificate issuance: set CLOUDFLARE_API_TOKEN and LETSENCRYPT_EMAIL to request/renew the wildcard certificate."
    return 0
  fi

  certbot certonly \
    --non-interactive \
    --agree-tos \
    --email "$LETSENCRYPT_EMAIL" \
    --cert-name "$CERT_NAME" \
    --dns-cloudflare \
    --dns-cloudflare-credentials "$CLOUDFLARE_CREDENTIALS_FILE" \
    --dns-cloudflare-propagation-seconds "${DNS_PROPAGATION_SECONDS:-60}" \
    -d "$ROOT_DOMAIN" \
    -d "*.${ROOT_DOMAIN}"
}

write_renewal_hook() {
  install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
  cat >/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh <<'HOOK'
#!/usr/bin/env sh
set -eu
nginx -t >/dev/null
systemctl reload nginx
HOOK
  chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
}

write_nginx_site() {
  if [ -f "/etc/letsencrypt/live/${CERT_NAME}/fullchain.pem" ]; then
    cat >"$NGINX_SITE" <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name ${API_DOMAIN};
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name ${API_DOMAIN};

    ssl_certificate /etc/letsencrypt/live/${CERT_NAME}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${CERT_NAME}/privkey.pem;
    ssl_session_cache shared:pullwise_api_ssl:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;

    client_max_body_size 10m;

    location / {
        proxy_pass http://${BACKEND_HOST}:${BACKEND_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 600s;
    }
}
NGINX
  else
    cat >"$NGINX_SITE" <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name ${API_DOMAIN};

    client_max_body_size 10m;

    location / {
        proxy_pass http://${BACKEND_HOST}:${BACKEND_PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 600s;
    }
}
NGINX
  fi

  ln -sfn "$NGINX_SITE" "/etc/nginx/sites-enabled/${API_DOMAIN}.conf"
  rm -f /etc/nginx/sites-enabled/default
  nginx -t
  systemctl reload nginx
}

start_backend() {
  systemctl restart "$SERVICE_NAME"
}

main() {
  require_root
  ensure_host_dependencies
  ensure_user_and_dirs
  write_env_defaults
  write_systemd_service
  write_cloudflare_credentials_if_present
  sync_cloudflare_dns_if_possible
  issue_certificate_if_possible
  write_renewal_hook
  write_nginx_site
  start_backend
  echo "Configured ${SERVICE_NAME} on ${BACKEND_HOST}:${BACKEND_PORT} behind ${API_DOMAIN}."
}

main "$@"
