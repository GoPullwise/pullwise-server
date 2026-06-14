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
    return system_config.worker_heartbeat_timeout_seconds()


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
    minimum = system_config.worker_min_version().strip()
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
    provider = public_issue_text(worker.get("provider")).lower() or "codex"
    allowed = {item.lower() for item in system_config.worker_allowed_providers()}
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
    codex_unready = codex_ready == 0 and doctor_status != "ok"
    if (
        clean_scan_error(worker.get("last_error"))
        or not worker_version_compatible(worker)
        or not worker_supported_provider(worker)
        or doctor_status in {"degraded", "failed", "not_ready"}
        or codex_unready
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


def decoded_worker_json_payload(value: object, expected_type: type) -> object | None:
    if isinstance(value, expected_type):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, expected_type) else None


def worker_machine_metrics_payload(worker: dict) -> dict | None:
    metrics = decoded_worker_json_payload(worker.get("machine_metrics"), dict)
    if not isinstance(metrics, dict):
        return None
    payload = dict(metrics)
    history = decoded_worker_json_payload(worker.get("machine_metrics_history"), list)
    payload["history"] = history if isinstance(history, list) else []
    payload["historyMeta"] = {
        "limit": system_metrics.SERVER_METRICS_HISTORY_LIMIT,
        "minIntervalSeconds": system_metrics.SERVER_METRICS_HISTORY_MIN_INTERVAL_SECONDS,
    }
    return payload


def worker_public_payload(worker: dict, *, admin: bool = False, include_machine_metrics: bool = False) -> dict:
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
        if include_machine_metrics:
            machine_metrics = worker_machine_metrics_payload(worker)
            if machine_metrics:
                payload["machineMetrics"] = machine_metrics
    return payload


def worker_safe_service_id(worker_id: object) -> str:
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    safe = "".join(char if char in allowed else "-" for char in public_issue_text(worker_id))
    return safe[:48]


def worker_release_package(version: str) -> str:
    return (
        "https://github.com/GoPullwise/pullwise-worker/releases/download/"
        f"v{version}/pullwise_worker-{version}-py3-none-any.whl"
    )


class WorkerReleaseConfigurationError(RuntimeError):
    pass


class WorkerReleaseDispatchError(RuntimeError):
    pass


class WorkerReleaseFetchError(RuntimeError):
    pass


def normalize_worker_release_version(value: object) -> str:
    version = public_issue_text(value)
    if version.startswith("v"):
        version = version[1:]
    return version if WORKER_PACKAGE_RELEASE_RE.fullmatch(version) else ""


def configured_worker_release_version() -> str:
    return normalize_worker_release_version(system_config.worker_default_version()) or DEFAULT_WORKER_PACKAGE_VERSION


def fetch_latest_worker_release_version(*, strict_list: bool = False) -> str:
    api_url = system_config.worker_release_api_url().strip() or DEFAULT_WORKER_RELEASES_API_URL
    if not api_url:
        return ""
    latest_payload = fetch_worker_release_api_payload(api_url)
    latest_version = worker_release_version_from_payload(latest_payload)
    list_url = worker_release_list_api_url(api_url)
    if list_url:
        try:
            list_version = worker_release_version_from_payload(fetch_worker_release_api_payload(list_url))
        except (OSError, TimeoutError, ValueError, json.JSONDecodeError, urllib.error.URLError, WorkerReleaseFetchError):
            if strict_list:
                raise
            list_version = ""
        if list_version and (
            not latest_version
            or compare_worker_versions(parse_worker_version(list_version) or (), parse_worker_version(latest_version) or ()) > 0
        ):
            return list_version
    return latest_version


def fetch_worker_release_api_payload(api_url: str) -> object:
    token = worker_release_github_token()
    headers = github_auth.github_api_headers(token) if token else github_auth.github_api_headers()
    try:
        return fetch_worker_release_api_payload_with_headers(api_url, headers)
    except urllib.error.HTTPError as exc:
        if not token or exc.code not in {401, 403, 404}:
            raise WorkerReleaseFetchError(worker_release_http_error_message(exc)) from exc
        try:
            return fetch_worker_release_api_payload_with_headers(api_url, github_auth.github_api_headers())
        except urllib.error.HTTPError as fallback_exc:
            message = (
                f"{worker_release_http_error_message(exc)} "
                f"Unauthenticated fallback also failed: {worker_release_http_error_message(fallback_exc)}"
            )
            raise WorkerReleaseFetchError(message) from fallback_exc


def fetch_worker_release_api_payload_with_headers(api_url: str, headers: dict[str, str]) -> object:
    request = urllib.request.Request(
        api_url,
        headers=headers,
    )
    timeout = system_config.worker_release_fetch_timeout_seconds()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def worker_release_list_api_url(api_url: str) -> str:
    parsed = urlparse(api_url)
    if not parsed.path.endswith("/releases/latest"):
        return ""
    query = urlencode({"per_page": "50"})
    return urlunparse(parsed._replace(path=parsed.path[: -len("/latest")], query=query))


def worker_release_version_from_payload(payload: object) -> str:
    if not isinstance(payload, dict):
        if isinstance(payload, list):
            return newest_worker_release_version(payload)
        return ""
    return normalize_worker_release_version(payload.get("tag_name") or payload.get("name"))


def newest_worker_release_version(releases: list) -> str:
    versions: list[str] = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") is True or release.get("prerelease") is True:
            continue
        version = normalize_worker_release_version(release.get("tag_name") or release.get("name"))
        if version:
            versions.append(version)
    if not versions:
        return ""
    return max(versions, key=lambda value: parse_worker_version(value) or ())


def github_latest_worker_release_version(*, force: bool = False) -> str:
    ttl = system_config.worker_release_cache_seconds()
    current_time = now()
    cached_version = public_issue_text(LATEST_WORKER_RELEASE_CACHE.get("version"))
    checked_at = float(LATEST_WORKER_RELEASE_CACHE.get("checked_at") or 0)
    if not force and cached_version and ttl and checked_at > current_time - ttl:
        return cached_version

    try:
        latest = fetch_latest_worker_release_version(strict_list=force)
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError, urllib.error.URLError, WorkerReleaseFetchError):
        if force:
            raise
        latest = ""
    if latest:
        LATEST_WORKER_RELEASE_CACHE.update({"version": latest, "checked_at": current_time})
        return latest
    return cached_version or ""


def latest_worker_release_version(*, force: bool = False) -> str:
    configured = normalize_worker_release_version(system_config.worker_default_version())
    if configured:
        return configured
    return github_latest_worker_release_version(force=force) or configured_worker_release_version()


def worker_defaults_payload(*, force_refresh: bool = False) -> dict:
    configured_version = normalize_worker_release_version(system_config.worker_default_version())
    release_error = ""
    try:
        latest_version = github_latest_worker_release_version(force=force_refresh)
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError, urllib.error.URLError, WorkerReleaseFetchError) as exc:
        latest_version = ""
        release_error = str(exc)
    version = configured_version or latest_version or configured_worker_release_version()
    package = worker_release_package(version)
    latest_package = worker_release_package(latest_version) if latest_version else ""
    return {
        "workerVersion": version,
        "workerPackage": package,
        "latestWorkerVersion": latest_version,
        "latestWorkerPackage": latest_package,
        "configuredWorkerVersion": configured_version,
        "defaults": {
            "version": version,
            "package": package,
            "source": "configured" if configured_version else "latest" if latest_version else "fallback",
        },
        "release": {
            "latestVersion": latest_version,
            "latestPackage": latest_package,
            "cacheSeconds": system_config.worker_release_cache_seconds(),
            "error": release_error,
        },
    }


WORKER_RELEASE_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
WORKER_RELEASE_WORKFLOW_RE = re.compile(r"^[A-Za-z0-9_.-]+\.ya?ml$")
WORKER_RELEASE_REF_RE = re.compile(r"^[A-Za-z0-9._/\-]{1,200}$")


def worker_release_repository() -> str:
    repository = public_issue_text(
        github_auth.env_any(
            ["PULLWISE_WORKER_RELEASE_REPOSITORY", "PULLWISE_WORKER_RELEASE_REPO"],
            "GoPullwise/pullwise-worker",
        )
    )
    if not WORKER_RELEASE_REPOSITORY_RE.fullmatch(repository):
        raise WorkerReleaseConfigurationError("PULLWISE_WORKER_RELEASE_REPOSITORY must be owner/repo.")
    return repository


def worker_release_workflow() -> str:
    workflow = public_issue_text(
        github_auth.env_any(
            ["PULLWISE_WORKER_RELEASE_WORKFLOW", "PULLWISE_WORKER_RELEASE_WORKFLOW_ID"],
            "release.yml",
        )
    )
    if not WORKER_RELEASE_WORKFLOW_RE.fullmatch(workflow):
        raise WorkerReleaseConfigurationError("PULLWISE_WORKER_RELEASE_WORKFLOW must be a workflow YAML filename.")
    return workflow


def worker_release_ref() -> str:
    ref = public_issue_text(github_auth.env_any(["PULLWISE_WORKER_RELEASE_REF"], "main"))
    if not WORKER_RELEASE_REF_RE.fullmatch(ref) or ref.startswith("/") or ref.endswith("/") or ".." in ref:
        raise WorkerReleaseConfigurationError("PULLWISE_WORKER_RELEASE_REF must be a safe branch or tag ref.")
    return ref


def worker_release_github_token() -> str:
    return public_issue_text(
        github_auth.env_any(
            [
                "PULLWISE_WORKER_RELEASE_TOKEN",
                "PULLWISE_WORKER_RELEASE_GITHUB_TOKEN",
                "PULLWISE_GITHUB_WORKFLOW_TOKEN",
            ]
        )
    )


def worker_release_dispatch_url(repository: str, workflow: str) -> str:
    owner, repo = repository.split("/", 1)
    return (
        f"{github_auth.github_api_url()}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/actions/workflows/{quote(workflow, safe='')}/dispatches"
    )


def worker_release_http_error_message(exc: urllib.error.HTTPError) -> str:
    detail = ""
    try:
        detail = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:
        detail = ""
    message = f"GitHub workflow dispatch failed with status {exc.code}."
    if detail:
        message = f"{message} {detail[:500]}"
    return message


def dispatch_worker_release_workflow(version_value: object) -> dict:
    version = normalize_worker_release_version(version_value)
    if not version:
        raise ValueError("Worker release version must use x.y.z format.")

    token = worker_release_github_token()
    if not token:
        raise WorkerReleaseConfigurationError("PULLWISE_WORKER_RELEASE_TOKEN is required to dispatch worker releases.")

    repository = worker_release_repository()
    workflow = worker_release_workflow()
    ref = worker_release_ref()
    body = {"ref": ref, "inputs": {"version": version}}
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        worker_release_dispatch_url(repository, workflow),
        data=data,
        headers={
            **github_auth.github_api_headers(token),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=github_auth.request_timeout()) as response:
            status = int(getattr(response, "status", 0) or response.getcode())
    except urllib.error.HTTPError as exc:
        raise WorkerReleaseDispatchError(worker_release_http_error_message(exc)) from exc
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise WorkerReleaseDispatchError(f"GitHub workflow dispatch failed: {exc}") from exc

    if status < 200 or status >= 300:
        raise WorkerReleaseDispatchError(f"GitHub workflow dispatch failed with status {status}.")

    LATEST_WORKER_RELEASE_CACHE.update({"version": "", "checked_at": 0.0})
    return {
        "ok": True,
        "version": version,
        "tag": f"v{version}",
        "repository": repository,
        "workflow": workflow,
        "ref": ref,
        "workflowDispatch": {
            "repository": repository,
            "workflow": workflow,
            "ref": ref,
            "inputs": {"version": version},
        },
    }


def default_worker_package(version: object = None) -> str:
    explicit_package = system_config.worker_default_package().strip()
    if explicit_package:
        return explicit_package
    selected_version = public_issue_text(version) or system_config.worker_default_version().strip() or DEFAULT_WORKER_PACKAGE_VERSION
    if not WORKER_PACKAGE_RELEASE_RE.fullmatch(selected_version):
        selected_version = DEFAULT_WORKER_PACKAGE_VERSION
    return worker_release_package(selected_version)


WORKER_INSTALL_PROVIDERS = ("codex", "opencode")


def default_worker_provider_chain() -> list[str]:
    providers: list[str] = []
    for plan in billing.PLAN_IDS:
        for provider in billing.review_agent_provider_chain(plan):
            if provider in WORKER_INSTALL_PROVIDERS and provider not in providers:
                providers.append(provider)
    return providers


def worker_provider_chain(value: object = None, *, strict: bool = False) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        raw_items = value.split(",")
    elif value is None:
        raw_items = default_worker_provider_chain()
    else:
        raw_items = []
    providers: list[str] = []
    for item in raw_items:
        provider = public_issue_text(item).lower()
        if provider in WORKER_INSTALL_PROVIDERS and provider not in providers:
            providers.append(provider)
    if providers:
        return providers
    if strict:
        raise ValueError("providerChain must include codex or opencode.")
    return default_worker_provider_chain()


def worker_provider_chain_text(value: object = None, *, strict: bool = False) -> str:
    return ",".join(worker_provider_chain(value, strict=strict))


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
    provider_chain = worker_provider_chain(worker.get("provider_chain") or worker.get("providerChain"))
    provider_chain_text = ",".join(provider_chain)
    safe_worker_id = worker_safe_service_id(public["worker_id"])
    service_home = f"/var/lib/pullwise-worker/{safe_worker_id}" if safe_worker_id else "/var/lib/pullwise-worker"
    service_log_dir = f"/var/log/pullwise-worker/{safe_worker_id}" if safe_worker_id else "/var/log/pullwise-worker"
    install_command = worker_install_command(
        install_url=install_url,
        server_url=server_url,
        worker_id=public["worker_id"],
        worker_name=public.get("name") or public["worker_id"],
        max_concurrent_jobs=max_concurrent_jobs,
        worker_package=worker_package,
        provider_chain=provider_chain_text,
    )
    local_install_command = worker_install_command(
        install_url=local_install_url,
        server_url=local_server_url,
        worker_id=public["worker_id"],
        worker_name=public.get("name") or public["worker_id"],
        max_concurrent_jobs=max_concurrent_jobs,
        worker_package=worker_package,
        provider_chain=provider_chain_text,
    )
    suggested_env = {
        "PULLWISE_SERVER_URL": server_url,
        "PULLWISE_LOCAL_SERVER_URL": local_server_url,
        "PULLWISE_WORKER_ID": public["worker_id"],
        "PULLWISE_WORKER_TOKEN": token,
        "PULLWISE_PROVIDER": provider_chain[0],
        "PULLWISE_PROVIDER_CHAIN": provider_chain_text,
        "PULLWISE_MAX_CONCURRENT_JOBS": str(max_concurrent_jobs),
        "PULLWISE_CHECKOUT_ROOT": f"{service_home}/checkouts",
        "PULLWISE_LOG_DIR": service_log_dir,
        "PULLWISE_WORKER_PACKAGE": worker_package,
        "PULLWISE_SERVICE_HOME": service_home,
        "PULLWISE_WORKER_POLL_JITTER_SECONDS": "2",
        "PULLWISE_WORKER_MAX_BACKOFF_SECONDS": "60",
        "PULLWISE_WORKER_CLEANUP_INTERVAL_SECONDS": "3600",
        "PULLWISE_RETAIN_FAILED_CHECKOUT_SECONDS": "0",
        "PULLWISE_MAX_CHECKOUT_BYTES": "21474836480",
        "PULLWISE_LOG_RETENTION_SECONDS": "1209600",
        "PULLWISE_MAX_LOG_BYTES": "1073741824",
        "PULLWISE_SCAN_SUMMARY_LOG_MAX_BYTES": "10485760",
    }
    if "codex" in provider_chain:
        suggested_env.update(
            {
                "PULLWISE_CODEX_COMMAND": f"{service_home}/.codex/bin/codex",
                "PULLWISE_CODEX_MODEL": "gpt-5.5",
                "PULLWISE_CODEX_REASONING_EFFORT": "medium",
            }
        )
    if "opencode" in provider_chain:
        suggested_env.update(
            {
                "PULLWISE_OPENCODE_COMMAND": f"{service_home}/.opencode/bin/opencode",
                "PULLWISE_OPENCODE_VARIANT": "medium",
            }
        )
    payload = {
        "worker": public,
        "worker_id": public["worker_id"],
        "worker_token": token,
        "server_url": server_url,
        "install_url": install_url,
        "local_server_url": local_server_url,
        "local_install_url": local_install_url,
        "install_commands": {
            "standard": install_command,
            "local": local_install_command,
        },
        "provider": provider_chain[0],
        "providerChain": list(provider_chain),
        "suggested_env": suggested_env,
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
    provider_chain: str,
) -> str:
    return (
        "read -rsp 'Pullwise worker token: ' PULLWISE_WORKER_TOKEN; echo; "
        "export PULLWISE_WORKER_TOKEN; "
        f"curl -fsSL {shell_quote(install_url)} | bash -s -- "
        f"--server {shell_quote(server_url)} "
        f"--worker-id {shell_quote(worker_id)} "
        f"--worker-name {shell_quote(worker_name)} "
        f"--package {shell_quote(worker_package)} "
        f"--provider-chain {shell_quote(provider_chain)} "
        f"--max-concurrent-jobs {max_concurrent_jobs}"
    )


def shell_quote(value: object) -> str:
    text = public_issue_text(value)
    if not text:
        return "''"
    return "'" + text.replace("'", "'\"'\"'") + "'"


def worker_install_script() -> str:
    script = r"""#!/usr/bin/env bash
set -euo pipefail

SERVICE_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SERVER_URL=""
WORKER_ID=""
WORKER_TOKEN=""
WORKER_NAME="pullwise-worker"
MAX_CONCURRENT_JOBS="1"
PROVIDER="codex"
PROVIDER_CHAIN=""
WORKER_PACKAGE=""
CODEX_COMMAND="${PULLWISE_CODEX_COMMAND:-}"
OPENCODE_COMMAND="${PULLWISE_OPENCODE_COMMAND:-}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --server) SERVER_URL="${2:-}"; shift 2 ;;
    --worker-id) WORKER_ID="${2:-}"; shift 2 ;;
    --worker-token-file) WORKER_TOKEN="$(cat "${2:-}")"; shift 2 ;;
    --worker-name) WORKER_NAME="${2:-}"; shift 2 ;;
    --max-concurrent-jobs) MAX_CONCURRENT_JOBS="${2:-1}"; shift 2 ;;
    --provider) PROVIDER="${2:-codex}"; shift 2 ;;
    --provider-chain) PROVIDER_CHAIN="${2:-}"; shift 2 ;;
    --package) WORKER_PACKAGE="${2:-}"; shift 2 ;;
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
safe_worker_id() {
  printf '%s' "$1" | tr -c 'A-Za-z0-9_-' '-' | cut -c1-48
}
service_user_name() {
  local suffix
  suffix="$(printf '%s' "$1" | tr 'A-Z_' 'a-z-' | tr -cd 'a-z0-9-' | cut -c1-16)"
  if [ -z "$suffix" ]; then
    suffix="worker"
  fi
  printf 'pw-worker-%s\n' "$suffix"
}
SAFE_WORKER_ID="$(safe_worker_id "$WORKER_ID")"
if [ -z "$SAFE_WORKER_ID" ]; then
  echo "worker id does not contain any safe service-name characters" >&2
  exit 2
fi
SERVICE_USER="$(service_user_name "$SAFE_WORKER_ID")"
SERVICE_GROUP="pullwise-worker"
BASE_CONFIG_DIR="/etc/pullwise-worker"
BASE_DATA_DIR="/var/lib/pullwise-worker"
BASE_LOG_DIR="/var/log/pullwise-worker"
CONFIG_DIR="$BASE_CONFIG_DIR/$SAFE_WORKER_ID"
ENV_FILE="$CONFIG_DIR/worker.env"
AUTH_COMMANDS_FILE="$CONFIG_DIR/auth-commands.txt"
BIN_PATH="/usr/local/bin/pullwise-worker-$SAFE_WORKER_ID"
DATA_DIR="$BASE_DATA_DIR/$SAFE_WORKER_ID"
CHECKOUT_ROOT="$DATA_DIR/checkouts"
LOG_DIR="$BASE_LOG_DIR/$SAFE_WORKER_ID"
SERVICE_FILE="/etc/systemd/system/pullwise-worker-$SAFE_WORKER_ID.service"
LOGROTATE_FILE="/etc/logrotate.d/pullwise-worker-$SAFE_WORKER_ID"
if [ -z "$WORKER_PACKAGE" ]; then
  WORKER_PACKAGE="${PULLWISE_WORKER_PACKAGE:-}"
fi
if [ -z "$WORKER_PACKAGE" ]; then
  WORKER_PACKAGE="__DEFAULT_WORKER_PACKAGE__"
fi
normalize_provider_chain() {
  local raw="${1:-}"
  local next=""
  local item
  raw="${raw//[[:space:]]/}"
  IFS=',' read -ra items <<< "$raw"
  for item in "${items[@]}"; do
    case "$item" in
      codex|opencode)
        case ",$next," in *",$item,"*) ;; *) next="${next:+$next,}$item" ;; esac
        ;;
    esac
  done
  printf '%s\n' "$next"
}
provider_chain_has() {
  case ",$PROVIDER_CHAIN," in *",$1,"*) return 0 ;; *) return 1 ;; esac
}
if [ -z "$PROVIDER_CHAIN" ]; then
  PROVIDER_CHAIN="${PULLWISE_PROVIDER_CHAIN:-}"
fi
PROVIDER_CHAIN="$(normalize_provider_chain "$PROVIDER_CHAIN")"
if [ -z "$PROVIDER_CHAIN" ]; then
  echo "provider chain is required; install from the admin-generated command." >&2
  exit 2
fi
PROVIDER="${PROVIDER_CHAIN%%,*}"
SERVICE_TOOL_PATH="$SERVICE_PATH:$DATA_DIR/.local/bin:$DATA_DIR/.codex/bin:$DATA_DIR/.opencode/bin"

case "$(uname -s)" in Linux) ;; *) echo "Pullwise worker installer requires Linux" >&2; exit 1 ;; esac
case "$(uname -m)" in x86_64|aarch64|arm64) ;; *) echo "Unsupported CPU architecture: $(uname -m)" >&2; exit 1 ;; esac

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root so the installer can create service users and systemd units." >&2
  exit 1
fi

need_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "missing required command: $1" >&2; exit 1; }; }
run_as_service_user() {
  (
    cd "$DATA_DIR"
    if command -v runuser >/dev/null 2>&1; then
      runuser -u "$SERVICE_USER" -- env HOME="$DATA_DIR" PATH="$SERVICE_TOOL_PATH" "$@"
    elif command -v sudo >/dev/null 2>&1; then
      sudo -u "$SERVICE_USER" env HOME="$DATA_DIR" PATH="$SERVICE_TOOL_PATH" "$@"
    else
      echo "missing runuser or sudo; cannot validate worker service user runtime" >&2
      return 127
    fi
  )
}
service_user_auth_command() {
  local command_line=""
  local part
  for part in "$@"; do
    command_line="${command_line:+$command_line }$(printf '%q' "$part")"
  done
  printf 'sudo -u %q env HOME=%q PATH=%q sh -lc %q\n' \
    "$SERVICE_USER" "$DATA_DIR" "$SERVICE_TOOL_PATH" "cd \"\$HOME\" && exec $command_line"
}
scoped_command_path() {
  local fallback_one="${1:-}"
  local fallback_two="${2:-}"
  if [ -n "$fallback_one" ] && [ -x "$fallback_one" ]; then
    printf '%s\n' "$fallback_one"
  elif [ -n "$fallback_two" ] && [ -x "$fallback_two" ]; then
    printf '%s\n' "$fallback_two"
  else
    return 1
  fi
}
ensure_scoped_command_path() {
  local command_path="${1:-}"
  local label="${2:-provider}"
  local resolved_home resolved_command
  [ -n "$command_path" ] || return 0
  resolved_home="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$DATA_DIR")"
  resolved_command="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$command_path")"
  case "$resolved_command/" in
    "$resolved_home"/*) ;;
    *)
      echo "$label command must be inside worker home $DATA_DIR: $command_path" >&2
      exit 1
      ;;
  esac
}
need_cmd python3
need_cmd git
need_cmd curl
python3 - <<'PY'
import sys
if sys.version_info < (3, 9):
    raise SystemExit("Pullwise worker requires Python 3.9 or newer.")
PY
PYTHON_BIN="$(python3 -c 'import sys; print(sys.executable)')"

getent group "$SERVICE_GROUP" >/dev/null 2>&1 || groupadd --system "$SERVICE_GROUP"
id "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
usermod -a -G "$SERVICE_GROUP" "$SERVICE_USER"
install -d -m 0755 -o root -g root "$BASE_CONFIG_DIR"
install -d -m 1770 -o root -g "$SERVICE_GROUP" "$BASE_DATA_DIR" "$BASE_LOG_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CONFIG_DIR" "$DATA_DIR" "$CHECKOUT_ROOT" "$LOG_DIR"

if provider_chain_has codex; then
  if [ -n "$CODEX_COMMAND" ]; then
    ensure_scoped_command_path "$CODEX_COMMAND" "Codex"
  elif ! CODEX_COMMAND="$(scoped_command_path "$DATA_DIR/.local/bin/codex" "$DATA_DIR/.codex/bin/codex")"; then
    run_as_service_user sh -lc 'curl -fsSL https://chatgpt.com/codex/install.sh | sh'
    CODEX_COMMAND="$(scoped_command_path "$DATA_DIR/.local/bin/codex" "$DATA_DIR/.codex/bin/codex")" || {
      echo "Codex installer completed, but codex is not executable for $SERVICE_USER." >&2
      exit 1
    }
  fi
  ensure_scoped_command_path "$CODEX_COMMAND" "Codex"
fi

if provider_chain_has opencode; then
  if [ -n "$OPENCODE_COMMAND" ]; then
    ensure_scoped_command_path "$OPENCODE_COMMAND" "OpenCode"
  elif ! OPENCODE_COMMAND="$(scoped_command_path "$DATA_DIR/.local/bin/opencode" "$DATA_DIR/.opencode/bin/opencode")"; then
    run_as_service_user sh -lc 'curl -fsSL https://opencode.ai/install | bash'
    OPENCODE_COMMAND="$(scoped_command_path "$DATA_DIR/.local/bin/opencode" "$DATA_DIR/.opencode/bin/opencode")" || {
      echo "OpenCode installer completed, but opencode is not executable for $SERVICE_USER." >&2
      exit 1
    }
  fi
  ensure_scoped_command_path "$OPENCODE_COMMAND" "OpenCode"
fi

python3 -m pip install --upgrade --force-reinstall --no-cache-dir "$WORKER_PACKAGE"

write_env_value() {
  local key="$1"
  local value="$2"
  if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
    echo "environment value for $key must be single-line" >&2
    exit 2
  fi
  printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
}
write_auth_commands() {
  {
    echo "Pullwise worker manual authorization commands"
    echo "Worker: $WORKER_NAME ($WORKER_ID)"
    echo "Home: $DATA_DIR"
    echo "Provider chain: $PROVIDER_CHAIN"
    if provider_chain_has codex; then
      echo "Codex device login:"
      service_user_auth_command "$CODEX_COMMAND" login --device-auth
    fi
    if provider_chain_has opencode; then
      echo "OpenCode interactive provider selection:"
      echo "Select the providers used by the Pullwise subscription plan agent configs."
      service_user_auth_command "$OPENCODE_COMMAND" auth login
      echo "OpenCode auth status:"
      service_user_auth_command "$OPENCODE_COMMAND" auth list
    fi
  } > "$AUTH_COMMANDS_FILE"
  chown root:"$SERVICE_USER" "$AUTH_COMMANDS_FILE"
  chmod 0640 "$AUTH_COMMANDS_FILE"
}
print_auth_commands() {
  echo
  echo "Authorization commands saved to $AUTH_COMMANDS_FILE"
  echo
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
if provider_chain_has codex; then
  write_env_value PULLWISE_CODEX_COMMAND "$CODEX_COMMAND"
  write_env_value PULLWISE_CODEX_MODEL "${PULLWISE_CODEX_MODEL:-gpt-5.5}"
  write_env_value PULLWISE_CODEX_REASONING_EFFORT "${PULLWISE_CODEX_REASONING_EFFORT:-medium}"
fi
if provider_chain_has opencode; then
  write_env_value PULLWISE_OPENCODE_COMMAND "$OPENCODE_COMMAND"
  write_env_value PULLWISE_OPENCODE_VARIANT "${PULLWISE_OPENCODE_VARIANT:-medium}"
fi
write_env_value PULLWISE_PYTHON_BIN "$PYTHON_BIN"
write_env_value PULLWISE_SERVICE_PATH "$SERVICE_PATH"
write_env_value PULLWISE_SERVICE_USER "$SERVICE_USER"
write_env_value PULLWISE_SERVICE_HOME "$DATA_DIR"
write_env_value PULLWISE_WORKER_POLL_JITTER_SECONDS "2"
write_env_value PULLWISE_WORKER_MAX_BACKOFF_SECONDS "60"
write_env_value PULLWISE_WORKER_CLEANUP_INTERVAL_SECONDS "3600"
write_env_value PULLWISE_RETAIN_FAILED_CHECKOUT_SECONDS "0"
write_env_value PULLWISE_MAX_CHECKOUT_BYTES "21474836480"
write_env_value PULLWISE_LOG_RETENTION_SECONDS "1209600"
write_env_value PULLWISE_MAX_LOG_BYTES "1073741824"
write_env_value PULLWISE_SCAN_SUMMARY_LOG_MAX_BYTES "10485760"
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"
write_auth_commands

cat > "$BIN_PATH" <<EOF
#!/usr/bin/env bash
set -euo pipefail
load_worker_env() {
  local env_file="\$1"
  local key value
  [ -f "\$env_file" ] || return 0
  while IFS="=" read -r key value || [ -n "\$key" ]; do
    [[ -z "\$key" || "\$key" == \\#* ]] && continue
    [[ "\$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    export "\$key=\$value"
  done < "\$env_file"
}
load_worker_env "\${PULLWISE_WORKER_ENV_FILE:-$ENV_FILE}"
export PATH="\${PULLWISE_SERVICE_PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
PYTHON_BIN="\${PULLWISE_PYTHON_BIN:-python3}"
exec "\$PYTHON_BIN" -m pullwise_worker.main "\$@"
EOF
chmod 0755 "$BIN_PATH"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Pullwise Worker $WORKER_NAME
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
SupplementaryGroups=$SERVICE_GROUP
WorkingDirectory=$DATA_DIR
EnvironmentFile=$ENV_FILE
Environment=PATH=$SERVICE_PATH
ExecStart=$BIN_PATH run
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$BASE_DATA_DIR $BASE_LOG_DIR $DATA_DIR $LOG_DIR

[Install]
WantedBy=multi-user.target
EOF

cat > "$LOGROTATE_FILE" <<EOF
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
systemctl enable "pullwise-worker-$SAFE_WORKER_ID" >/dev/null
systemctl restart "pullwise-worker-$SAFE_WORKER_ID"
echo "Pullwise worker installed as $WORKER_NAME ($WORKER_ID)."
echo "Systemd service: pullwise-worker-$SAFE_WORKER_ID"
echo "Worker home: $DATA_DIR"
print_auth_commands
run_as_service_user "$BIN_PATH" doctor || true
"""
    return (
        script.replace("__DEFAULT_WORKER_PACKAGE__", default_worker_package())
        .replace("\r\n", "\n")
    )


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


