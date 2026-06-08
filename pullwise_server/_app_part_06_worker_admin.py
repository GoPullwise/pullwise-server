from __future__ import annotations

# Loaded by app.py; keep definitions in that module's globals for compatibility.

def summarize_findings(findings: list[dict]) -> dict:
    summary = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for finding in findings:
        severity = review._safe_severity(finding.get("severity"))
        if severity in summary:
            summary[severity] += 1
    return summary


def worker_heartbeat_timeout_seconds() -> int:
    return max(60, env_int("PULLWISE_WORKER_HEARTBEAT_TIMEOUT_SECONDS", 120))


def parse_worker_version(value: object) -> tuple[int, ...] | None:
    version = public_issue_text(value).strip()
    if version.startswith("v"):
        version = version[1:]
    parts = version.split(".")
    if not parts or any(not part.isdecimal() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def compare_worker_versions(version: tuple[int, ...], minimum: tuple[int, ...]) -> int:
    length = max(len(version), len(minimum))
    padded_version = version + (0,) * (length - len(version))
    padded_minimum = minimum + (0,) * (length - len(minimum))
    if padded_version == padded_minimum:
        return 0
    return 1 if padded_version > padded_minimum else -1


def worker_version_compatible(worker: dict) -> bool:
    minimum = env("PULLWISE_MIN_WORKER_VERSION", "").strip()
    if not minimum:
        return True
    parsed_minimum = parse_worker_version(minimum)
    if parsed_minimum is None:
        return True
    parsed_version = parse_worker_version(worker.get("version"))
    if parsed_version is None:
        return False
    return compare_worker_versions(parsed_version, parsed_minimum) >= 0


def worker_supported_provider(worker: dict) -> bool:
    provider = public_issue_text(worker.get("provider")) or "codex"
    allowed = {item.strip() for item in env("PULLWISE_WORKER_PROVIDERS", "codex").split(",") if item.strip()}
    return provider in allowed


def computed_worker_status(worker: dict, *, timestamp: int | None = None) -> str:
    current_time = int(timestamp if timestamp is not None else now())
    if not worker.get("enabled") or worker.get("deleted_at") is not None:
        return "disabled"
    last_heartbeat = pull_request_timestamp(worker.get("last_heartbeat_at"))
    if not last_heartbeat or last_heartbeat < current_time - worker_heartbeat_timeout_seconds():
        return "offline"
    doctor_status = public_issue_text(worker.get("doctor_status")).lower()
    codex_ready = worker.get("codex_ready")
    if (
        clean_scan_error(worker.get("last_error"))
        or not worker_version_compatible(worker)
        or not worker_supported_provider(worker)
        or doctor_status in {"degraded", "failed", "not_ready"}
        or codex_ready == 0
    ):
        return "degraded"
    if public_scan_count(worker.get("running_jobs")) >= max(1, public_scan_count(worker.get("max_concurrent_jobs"))):
        return "busy"
    return "idle"


def worker_can_claim(worker: dict, *, timestamp: int | None = None) -> tuple[bool, str]:
    status = computed_worker_status(worker, timestamp=timestamp)
    if status in {"idle", "busy"}:
        return True, status
    return False, status


def worker_available_claim_slots(worker: dict) -> int:
    capacity = max(1, public_scan_count(worker.get("max_concurrent_jobs")) or 1)
    running = max(0, public_scan_count(worker.get("running_jobs")))
    reported_free = max(0, public_scan_count(worker.get("free_slots")))
    return max(0, min(reported_free, capacity - running))


def worker_command_payload(command: dict | None, *, admin: bool = False) -> dict | None:
    if not command:
        return None
    payload = {
        "id": public_issue_text(command.get("id")),
        "worker_id": public_issue_text(command.get("worker_id")),
        "command": public_issue_text(command.get("command")),
        "status": public_issue_text(command.get("status")),
        "created_at": pull_request_timestamp(command.get("created_at")),
        "started_at": pull_request_timestamp(command.get("started_at")),
        "completed_at": pull_request_timestamp(command.get("completed_at")),
        "updated_at": pull_request_timestamp(command.get("updated_at")),
        "error": clean_scan_error(command.get("error")),
    }
    if admin:
        payload["requested_by_user_id"] = public_issue_text(command.get("requested_by_user_id"))
        payload["request_id"] = public_issue_text(command.get("request_id"))
    return payload


def worker_public_payload(worker: dict, *, admin: bool = False) -> dict:
    payload = {
        "worker_id": public_issue_text(worker.get("worker_id")),
        "name": public_issue_text(worker.get("name")) or public_issue_text(worker.get("worker_id")),
        "provider": public_issue_text(worker.get("provider")) or "codex",
        "enabled": bool(worker.get("enabled")),
        "status": computed_worker_status(worker),
        "last_heartbeat_at": pull_request_timestamp(worker.get("last_heartbeat_at")),
        "max_concurrent_jobs": public_scan_count(worker.get("max_concurrent_jobs")) or 1,
        "running_jobs": public_scan_count(worker.get("running_jobs")),
        "free_slots": public_scan_count(worker.get("free_slots")),
        "version": public_issue_text(worker.get("version")),
        "region": public_issue_text(worker.get("region")),
        "created_at": pull_request_timestamp(worker.get("created_at")),
        "updated_at": pull_request_timestamp(worker.get("updated_at")),
        "disabled_at": pull_request_timestamp(worker.get("disabled_at")),
        "deleted_at": pull_request_timestamp(worker.get("deleted_at")),
    }
    if admin:
        payload["hostname"] = public_issue_text(worker.get("hostname"))
        payload["last_error"] = clean_scan_error(worker.get("last_error"))
        payload["doctor_status"] = public_issue_text(worker.get("doctor_status"))
        payload["codex_ready"] = bool(worker.get("codex_ready")) if worker.get("codex_ready") is not None else None
        payload["systemd_active"] = bool(worker.get("systemd_active")) if worker.get("systemd_active") is not None else None
        payload["doctor_checked_at"] = pull_request_timestamp(worker.get("doctor_checked_at"))
        payload["test"] = worker_test_payload(worker)
        payload["latest_command"] = worker_command_payload(
            db.get_latest_worker_command(public_issue_text(worker.get("worker_id"))),
            admin=True,
        )
    return payload


def worker_release_package(version: str) -> str:
    return (
        "https://github.com/GoPullwise/pullwise-worker/releases/download/"
        f"v{version}/pullwise_worker-{version}-py3-none-any.whl"
    )


def normalize_worker_release_version(value: object) -> str:
    version = public_issue_text(value)
    if version.startswith("v"):
        version = version[1:]
    return version if WORKER_PACKAGE_RELEASE_RE.fullmatch(version) else ""


def configured_worker_release_version() -> str:
    return normalize_worker_release_version(env("PULLWISE_DEFAULT_WORKER_VERSION", "")) or DEFAULT_WORKER_PACKAGE_VERSION


def fetch_latest_worker_release_version() -> str:
    api_url = env("PULLWISE_WORKER_RELEASES_API_URL", DEFAULT_WORKER_RELEASES_API_URL).strip()
    if not api_url:
        return ""
    request = urllib.request.Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Pullwise",
        },
    )
    timeout = max(1, env_int("PULLWISE_WORKER_RELEASE_FETCH_TIMEOUT_SECONDS", 3))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        return ""
    return normalize_worker_release_version(payload.get("tag_name") or payload.get("name"))


def latest_worker_release_version() -> str:
    configured = normalize_worker_release_version(env("PULLWISE_DEFAULT_WORKER_VERSION", ""))
    if configured:
        return configured

    ttl = max(0, env_int("PULLWISE_WORKER_RELEASE_CACHE_SECONDS", 300))
    current_time = now()
    cached_version = public_issue_text(LATEST_WORKER_RELEASE_CACHE.get("version"))
    checked_at = float(LATEST_WORKER_RELEASE_CACHE.get("checked_at") or 0)
    if cached_version and ttl and checked_at > current_time - ttl:
        return cached_version

    try:
        latest = fetch_latest_worker_release_version()
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError, urllib.error.URLError):
        latest = ""
    if latest:
        LATEST_WORKER_RELEASE_CACHE.update({"version": latest, "checked_at": current_time})
        return latest
    return configured_worker_release_version()


def worker_defaults_payload() -> dict:
    version = latest_worker_release_version()
    package = worker_release_package(version)
    return {
        "workerVersion": version,
        "workerPackage": package,
        "defaults": {
            "version": version,
            "package": package,
        },
    }


def default_worker_package(version: object = None) -> str:
    explicit_package = env("PULLWISE_DEFAULT_WORKER_PACKAGE", "").strip()
    if explicit_package:
        return explicit_package
    selected_version = public_issue_text(version) or env("PULLWISE_DEFAULT_WORKER_VERSION", "").strip() or DEFAULT_WORKER_PACKAGE_VERSION
    if not WORKER_PACKAGE_RELEASE_RE.fullmatch(selected_version):
        selected_version = DEFAULT_WORKER_PACKAGE_VERSION
    return worker_release_package(selected_version)


def worker_create_payload(worker: dict) -> dict:
    public = worker_public_payload(worker, admin=True)
    token = public_issue_text(worker.get("worker_token"))
    server_url = (
        env("PULLWISE_WORKER_SERVER_URL", "").rstrip("/")
        or env("PULLWISE_SERVER_URL", "").rstrip("/")
        or env("PULLWISE_API_BASE_URL", "").rstrip("/")
        or "http://localhost:8080"
    )
    install_url = f"{server_url}/install-worker.sh"
    local_server_url = (
        env("PULLWISE_WORKER_LOCAL_SERVER_URL", "").rstrip("/")
        or env("PULLWISE_LOCAL_SERVER_URL", "").rstrip("/")
        or "http://127.0.0.1:18080"
    )
    local_install_url = f"{local_server_url}/install-worker.sh"
    max_concurrent_jobs = max(1, public_scan_count(public.get("max_concurrent_jobs")) or 1)
    worker_package = default_worker_package(public.get("version"))
    install_command = worker_install_command(
        install_url=install_url,
        server_url=server_url,
        worker_id=public["worker_id"],
        worker_name=public.get("name") or public["worker_id"],
        max_concurrent_jobs=max_concurrent_jobs,
        worker_package=worker_package,
    )
    local_install_command = worker_install_command(
        install_url=local_install_url,
        server_url=local_server_url,
        worker_id=public["worker_id"],
        worker_name=public.get("name") or public["worker_id"],
        max_concurrent_jobs=max_concurrent_jobs,
        worker_package=worker_package,
    )
    payload = {
        "worker": public,
        "worker_id": public["worker_id"],
        "worker_token": token,
        "server_url": server_url,
        "install_url": install_url,
        "install_command": install_command,
        "local_server_url": local_server_url,
        "local_install_url": local_install_url,
        "local_install_command": local_install_command,
        "install_commands": {
            "standard": install_command,
            "local": local_install_command,
        },
        "provider": public["provider"],
        "suggested_env": {
            "PULLWISE_SERVER_URL": server_url,
            "PULLWISE_LOCAL_SERVER_URL": local_server_url,
            "PULLWISE_WORKER_ID": public["worker_id"],
            "PULLWISE_WORKER_TOKEN": token,
            "PULLWISE_PROVIDER": public["provider"],
            "PULLWISE_PROVIDER_CHAIN": public["provider"],
            "PULLWISE_MAX_CONCURRENT_JOBS": str(max_concurrent_jobs),
            "PULLWISE_CHECKOUT_ROOT": "/var/lib/pullwise-worker/checkouts",
            "PULLWISE_LOG_DIR": "/var/log/pullwise-worker",
            "PULLWISE_WORKER_PACKAGE": worker_package,
            "PULLWISE_CODEX_PACKAGE": "@openai/codex@0.135.0",
            "PULLWISE_CODEX_MODEL": "gpt-5.5",
            "PULLWISE_CODEX_REASONING_EFFORT": "medium",
            "PULLWISE_OPENCODE_COMMAND": "opencode",
            "PULLWISE_OPENCODE_MODEL": "opencode/big-pickle",
            "PULLWISE_OPENCODE_VARIANT": "medium",
            "PULLWISE_WORKER_POLL_JITTER_SECONDS": "2",
            "PULLWISE_WORKER_MAX_BACKOFF_SECONDS": "60",
            "PULLWISE_WORKER_CLEANUP_INTERVAL_SECONDS": "3600",
            "PULLWISE_RETAIN_FAILED_CHECKOUT_SECONDS": "0",
            "PULLWISE_MAX_CHECKOUT_BYTES": "21474836480",
            "PULLWISE_MAX_REPO_FILES": "2000",
            "PULLWISE_MAX_REPO_BYTES": "52428800",
            "PULLWISE_LOG_RETENTION_SECONDS": "1209600",
            "PULLWISE_MAX_LOG_BYTES": "1073741824",
            "PULLWISE_SCAN_SUMMARY_LOG_MAX_BYTES": "10485760",
        },
    }
    return payload


def worker_install_command(
    *,
    install_url: str,
    server_url: str,
    worker_id: str,
    worker_name: str,
    max_concurrent_jobs: int,
    worker_package: str,
) -> str:
    return (
        "read -rsp 'Pullwise worker token: ' PULLWISE_WORKER_TOKEN; echo; "
        "export PULLWISE_WORKER_TOKEN; "
        f"curl -fsSL {shell_quote(install_url)} | bash -s -- "
        f"--server {shell_quote(server_url)} "
        f"--worker-id {shell_quote(worker_id)} "
        f"--worker-name {shell_quote(worker_name)} "
        f"--package {shell_quote(worker_package)} "
        f"--max-concurrent-jobs {max_concurrent_jobs}"
    )


def shell_quote(value: object) -> str:
    text = public_issue_text(value)
    if not text:
        return "''"
    return "'" + text.replace("'", "'\"'\"'") + "'"


def worker_install_script() -> str:
    script = """#!/usr/bin/env bash
set -euo pipefail

SERVICE_USER="pullwise-worker"
SERVICE_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
CONFIG_DIR="/etc/pullwise-worker"
ENV_FILE="$CONFIG_DIR/worker.env"
BIN_PATH="/usr/local/bin/pullwise-worker"
DATA_DIR="/var/lib/pullwise-worker"
CHECKOUT_ROOT="$DATA_DIR/checkouts"
LOG_DIR="/var/log/pullwise-worker"
SERVER_URL=""
WORKER_ID=""
WORKER_TOKEN=""
WORKER_NAME="pullwise-worker"
MAX_CONCURRENT_JOBS="1"
PROVIDER="codex"
PROVIDER_CHAIN=""
WORKER_PACKAGE=""
CODEX_PACKAGE="${PULLWISE_CODEX_PACKAGE:-@openai/codex@0.135.0}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --server) SERVER_URL="${2:-}"; shift 2 ;;
    --worker-id) WORKER_ID="${2:-}"; shift 2 ;;
    --worker-token-file) WORKER_TOKEN="$(cat "${2:-}")"; shift 2 ;;
    --worker-name) WORKER_NAME="${2:-}"; shift 2 ;;
    --max-concurrent-jobs) MAX_CONCURRENT_JOBS="${2:-1}"; shift 2 ;;
    --provider) PROVIDER="${2:-codex}"; shift 2 ;;
    --provider-chain) PROVIDER_CHAIN="${2:-codex}"; shift 2 ;;
    --package) WORKER_PACKAGE="${2:-}"; shift 2 ;;
    --codex-package) CODEX_PACKAGE="${2:-@openai/codex@0.135.0}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$WORKER_TOKEN" ] && [ -n "${PULLWISE_WORKER_TOKEN:-}" ]; then
  WORKER_TOKEN="$PULLWISE_WORKER_TOKEN"
fi

if [ -z "$SERVER_URL" ] || [ -z "$WORKER_ID" ] || [ -z "$WORKER_TOKEN" ]; then
  echo "missing --server, --worker-id, or worker token env/file" >&2
  exit 2
fi
if [ -z "$WORKER_PACKAGE" ]; then
  WORKER_PACKAGE="${PULLWISE_WORKER_PACKAGE:-}"
fi
if [ -z "$WORKER_PACKAGE" ]; then
  WORKER_PACKAGE="__DEFAULT_WORKER_PACKAGE__"
fi
if [ -z "$PROVIDER_CHAIN" ]; then
  PROVIDER_CHAIN="${PULLWISE_PROVIDER_CHAIN:-$PROVIDER}"
fi

case "$(uname -s)" in Linux) ;; *) echo "Pullwise worker installer requires Linux" >&2; exit 1 ;; esac
case "$(uname -m)" in x86_64|aarch64|arm64) ;; *) echo "Unsupported CPU architecture: $(uname -m)" >&2; exit 1 ;; esac

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root so the installer can create service users and systemd units." >&2
  exit 1
fi

need_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "missing required command: $1" >&2; exit 1; }; }
run_as_service_user() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$SERVICE_USER" -- env PATH="$SERVICE_PATH" "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo -u "$SERVICE_USER" env PATH="$SERVICE_PATH" "$@"
  else
    echo "missing runuser or sudo; cannot validate worker service user runtime" >&2
    return 127
  fi
}
need_cmd python3
need_cmd git
python3 - <<'PY'
import sys
if sys.version_info < (3, 9):
    raise SystemExit("Pullwise worker requires Python 3.9 or newer.")
PY
PYTHON_BIN="$(python3 -c 'import sys; print(sys.executable)')"
if ! command -v node >/dev/null 2>&1; then
  echo "node is required for Codex CLI; install Node.js 20+ then rerun." >&2
  exit 1
fi
NODE_MAJOR="$(node -e 'process.stdout.write(String(process.versions.node.split(".")[0]))')"
if [ "${NODE_MAJOR:-0}" -lt 20 ]; then
  echo "Node.js 20+ is required for Codex CLI. Found $(node --version)." >&2
  exit 1
fi
if ! command -v codex >/dev/null 2>&1; then
  if command -v npm >/dev/null 2>&1; then
    npm install -g "$CODEX_PACKAGE"
  else
    echo "npm is required to install Codex CLI. Install codex manually and rerun." >&2
    exit 1
  fi
fi

id "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CONFIG_DIR" "$DATA_DIR" "$CHECKOUT_ROOT" "$LOG_DIR"

SERVICE_NODE_MAJOR="$(run_as_service_user node -e 'process.stdout.write(String(process.versions.node.split(".")[0]))' 2>/dev/null || true)"
SERVICE_NODE_VERSION="$(run_as_service_user node --version 2>/dev/null || true)"
if [ "${SERVICE_NODE_MAJOR:-0}" -lt 20 ]; then
  echo "Node.js 20+ must be available to $SERVICE_USER. Found ${SERVICE_NODE_VERSION:-not found}." >&2
  exit 1
fi

python3 -m pip install --upgrade "$WORKER_PACKAGE"

write_env_value() {
  local key="$1"
  local value="$2"
  if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
    echo "environment value for $key must be single-line" >&2
    exit 2
  fi
  printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
}

: > "$ENV_FILE"
write_env_value PULLWISE_SERVER_URL "$SERVER_URL"
write_env_value PULLWISE_WORKER_ID "$WORKER_ID"
write_env_value PULLWISE_WORKER_TOKEN "$WORKER_TOKEN"
write_env_value PULLWISE_PROVIDER "$PROVIDER"
write_env_value PULLWISE_PROVIDER_CHAIN "$PROVIDER_CHAIN"
write_env_value PULLWISE_MAX_CONCURRENT_JOBS "$MAX_CONCURRENT_JOBS"
write_env_value PULLWISE_CHECKOUT_ROOT "$CHECKOUT_ROOT"
write_env_value PULLWISE_LOG_DIR "$LOG_DIR"
write_env_value PULLWISE_WORKER_PACKAGE "$WORKER_PACKAGE"
write_env_value PULLWISE_CODEX_PACKAGE "$CODEX_PACKAGE"
write_env_value PULLWISE_CODEX_MODEL "${PULLWISE_CODEX_MODEL:-gpt-5.5}"
write_env_value PULLWISE_CODEX_REASONING_EFFORT "${PULLWISE_CODEX_REASONING_EFFORT:-medium}"
write_env_value PULLWISE_OPENCODE_COMMAND "${PULLWISE_OPENCODE_COMMAND:-opencode}"
write_env_value PULLWISE_OPENCODE_MODEL "${PULLWISE_OPENCODE_MODEL:-opencode/big-pickle}"
write_env_value PULLWISE_OPENCODE_VARIANT "${PULLWISE_OPENCODE_VARIANT:-medium}"
write_env_value PULLWISE_PYTHON_BIN "$PYTHON_BIN"
write_env_value PULLWISE_SERVICE_PATH "$SERVICE_PATH"
write_env_value PULLWISE_WORKER_POLL_JITTER_SECONDS "2"
write_env_value PULLWISE_WORKER_MAX_BACKOFF_SECONDS "60"
write_env_value PULLWISE_WORKER_CLEANUP_INTERVAL_SECONDS "3600"
write_env_value PULLWISE_RETAIN_FAILED_CHECKOUT_SECONDS "0"
write_env_value PULLWISE_MAX_CHECKOUT_BYTES "21474836480"
write_env_value PULLWISE_MAX_REPO_FILES "2000"
write_env_value PULLWISE_MAX_REPO_BYTES "52428800"
write_env_value PULLWISE_LOG_RETENTION_SECONDS "1209600"
write_env_value PULLWISE_MAX_LOG_BYTES "1073741824"
write_env_value PULLWISE_SCAN_SUMMARY_LOG_MAX_BYTES "10485760"
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

cat > "$BIN_PATH" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
load_worker_env() {
  local env_file="$1"
  local key value
  [ -f "$env_file" ] || return 0
  while IFS="=" read -r key value || [ -n "$key" ]; do
    [[ -z "$key" || "$key" == \\#* ]] && continue
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    export "$key=$value"
  done < "$env_file"
}
load_worker_env /etc/pullwise-worker/worker.env
export PATH="${PULLWISE_SERVICE_PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
PYTHON_BIN="${PULLWISE_PYTHON_BIN:-python3}"
exec "$PYTHON_BIN" -m pullwise_worker.main "$@"
EOF
chmod 0755 "$BIN_PATH"

cat > /etc/systemd/system/pullwise-worker.service <<EOF
[Unit]
Description=Pullwise Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$DATA_DIR
EnvironmentFile=$ENV_FILE
Environment=PATH=$SERVICE_PATH
ExecStart=$BIN_PATH run
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$DATA_DIR $LOG_DIR

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/logrotate.d/pullwise-worker <<EOF
$LOG_DIR/*.log {
  daily
  rotate 14
  compress
  missingok
  notifempty
  create 0640 $SERVICE_USER $SERVICE_USER
}
EOF

systemctl daemon-reload
systemctl enable pullwise-worker >/dev/null
systemctl restart pullwise-worker
run_as_service_user "$BIN_PATH" doctor || true

echo "Pullwise worker installed as $WORKER_NAME ($WORKER_ID)."
echo "If Codex is not logged in, run: sudo -u $SERVICE_USER env HOME=$DATA_DIR PATH=$SERVICE_PATH codex login --device-auth"
"""
    return script.replace("__DEFAULT_WORKER_PACKAGE__", default_worker_package()).replace("\r\n", "\n")


def worker_test_payload(worker: dict) -> dict:
    token_used_at = pull_request_timestamp(worker.get("token_last_used_at"))
    checks = {
        "exists": bool(worker and not worker.get("deleted_at")),
        "enabled": bool(worker.get("enabled")),
        "recentHeartbeat": bool(
            pull_request_timestamp(worker.get("last_heartbeat_at"))
            and pull_request_timestamp(worker.get("last_heartbeat_at")) >= now() - worker_heartbeat_timeout_seconds()
        ),
        "tokenRecentlyUsed": bool(token_used_at),
        "versionCompatible": worker_version_compatible(worker),
        "providerSupported": worker_supported_provider(worker),
        "freeSlotsNormal": public_scan_count(worker.get("free_slots")) <= max(1, public_scan_count(worker.get("max_concurrent_jobs"))),
        "noRecentError": not bool(clean_scan_error(worker.get("last_error"))),
    }
    return {"ok": all(checks.values()), "checks": checks}


