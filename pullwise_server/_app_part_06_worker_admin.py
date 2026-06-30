from __future__ import annotations

# Loaded by app.py; keep definitions in that module's globals for compatibility.

from . import _app_part_05_worker_results as _previous_app_part
from ._app_imports import import_compat_globals as _import_compat_globals

_import_compat_globals(vars(_previous_app_part), globals())
del _import_compat_globals, _previous_app_part

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


def worker_record_provider_chain(worker: dict) -> list[str]:
    decoded = decoded_worker_json_payload(worker.get("provider_chain"), list)
    return worker_provider_chain(decoded or worker.get("provider_chain") or worker.get("provider") or worker.get("providerChain"))


def worker_record_ready_providers(worker: dict) -> list[str]:
    decoded = decoded_worker_json_payload(worker.get("ready_providers"), list)
    if decoded is not None:
        return db.normalize_provider_list(decoded)
    if worker.get("readyProviders") is not None:
        return db.normalize_provider_list(worker.get("readyProviders"))
    fallback = []
    if worker.get("codex_ready"):
        fallback.append("codex")
    if fallback:
        return fallback
    return worker_record_provider_chain(worker) if public_issue_text(worker.get("doctor_status")).lower() == "ok" else []


def worker_supported_provider(worker: dict) -> bool:
    provider_chain = worker_record_provider_chain(worker)
    allowed = {item.lower() for item in system_config.worker_allowed_providers()}
    return any(provider in allowed for provider in provider_chain)


def computed_worker_status(worker: dict, *, timestamp: int | None = None) -> str:
    current_time = int(timestamp if timestamp is not None else now())
    if not worker.get("enabled") or worker.get("deleted_at") is not None:
        return "disabled"
    last_heartbeat = pull_request_timestamp(worker.get("last_heartbeat_at"))
    if not last_heartbeat or last_heartbeat < current_time - worker_heartbeat_timeout_seconds():
        return "offline"
    running_jobs = public_scan_count(worker.get("running_jobs"))
    doctor_status = public_issue_text(worker.get("doctor_status")).lower()
    codex_ready = worker.get("codex_ready")
    ready_providers = worker_record_ready_providers(worker)
    provider_readiness_blocked = not ready_providers and (
        codex_ready == 0 or doctor_status in {"degraded", "failed", "not_ready"}
    )
    if (
        not worker_version_compatible(worker)
        or not worker_supported_provider(worker)
        or (provider_readiness_blocked and running_jobs <= 0)
    ):
        return "degraded"
    if provider_readiness_blocked and running_jobs > 0:
        return "busy"
    if running_jobs >= 1:
        return "busy"
    return "idle"


def worker_can_claim(worker: dict, *, timestamp: int | None = None) -> tuple[bool, str]:
    status = computed_worker_status(worker, timestamp=timestamp)
    if status in {"idle", "busy"}:
        return True, status
    return False, status


def user_has_active_private_worker(user_id: str, *, timestamp: int | None = None) -> bool:
    user_id = public_issue_text(user_id)
    if not user_id:
        return False
    for worker in db.list_private_workers_for_user(user_id):
        allowed, _status = worker_can_claim(worker, timestamp=timestamp)
        if allowed and worker_record_ready_providers(worker):
            return True
    return False


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


def annotate_worker_runtime_payloads(workers: list[dict], *, include_latest_commands: bool = False) -> list[dict]:
    worker_ids = [public_issue_text(worker.get("worker_id")) for worker in workers if public_issue_text(worker.get("worker_id"))]
    running_counts = db.worker_running_scan_job_counts(worker_ids)
    latest_commands = db.latest_worker_commands(worker_ids) if include_latest_commands else {}
    annotated = []
    for worker in workers:
        item = dict(worker)
        worker_id = public_issue_text(item.get("worker_id"))
        running_jobs = running_counts.get(worker_id, 0)
        item["running_jobs"] = running_jobs
        item["_running_jobs_count"] = running_jobs
        if include_latest_commands:
            item["_latest_command_loaded"] = True
        if worker_id in latest_commands:
            item["_latest_command"] = latest_commands[worker_id]
        annotated.append(item)
    return annotated


def worker_public_payload(worker: dict, *, admin: bool = False, include_machine_metrics: bool = False) -> dict:
    if admin:
        worker = dict(worker)
        running_jobs = public_scan_count(worker.get("_running_jobs_count"))
        if "_running_jobs_count" not in worker:
            running_jobs = db.count_worker_running_scan_jobs(public_issue_text(worker.get("worker_id")))
        worker["running_jobs"] = running_jobs
    provider_chain = worker_record_provider_chain(worker)
    ready_providers = worker_record_ready_providers(worker)
    worker_scope = db.normalize_worker_scope(worker.get("worker_scope") or worker.get("scope"))
    owner_user_id = public_issue_text(worker.get("owner_user_id") or worker.get("ownerUserId"))
    payload = {
        "worker_id": public_issue_text(worker.get("worker_id")),
        "name": public_issue_text(worker.get("name")) or public_issue_text(worker.get("worker_id")),
        "scope": worker_scope,
        "private": worker_scope == db.WORKER_SCOPE_PRIVATE,
        "ownerUserId": owner_user_id,
        "provider": public_issue_text(worker.get("provider")) or (provider_chain[0] if provider_chain else "codex"),
        "providerChain": provider_chain,
        "readyProviders": ready_providers,
        "enabled": bool(worker.get("enabled")),
        "status": computed_worker_status(worker),
        "last_heartbeat_at": pull_request_timestamp(worker.get("last_heartbeat_at")),
        "running_jobs": public_scan_count(worker.get("running_jobs")),
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
        latest_command = worker.get("_latest_command") if isinstance(worker.get("_latest_command"), dict) else None
        if worker.get("_latest_command_loaded") and latest_command is None:
            payload["latest_command"] = None
        else:
            payload["latest_command"] = worker_command_payload(
                latest_command or db.get_latest_worker_command(public_issue_text(worker.get("worker_id"))),
                admin=True,
            )
        if include_machine_metrics:
            machine_metrics = worker_machine_metrics_payload(worker)
            if machine_metrics:
                payload["machineMetrics"] = machine_metrics
    return payload


LOG_STREAM_LOCK = threading.RLock()
LOG_STREAM_SESSIONS: dict[str, dict] = {}
LOG_STREAM_TOKEN_RE = re.compile(
    r"(?i)(x-access-token:)[^\s@]+|"
    r"(Bearer\s+)[A-Za-z0-9._~+/=-]+|"
    r"(pw[krs]_[A-Za-z0-9._~+/=-]+)|"
    r"(sk-[A-Za-z0-9._~+/=-]+)"
)


def log_stream_idle_timeout_seconds() -> int:
    return max(30, min(3600, env_int("PULLWISE_LOG_STREAM_IDLE_TIMEOUT_SECONDS", 300)))


def log_stream_max_lines() -> int:
    return max(100, min(2000, env_int("PULLWISE_LOG_STREAM_MAX_LINES", 500)))


def log_stream_max_sessions() -> int:
    return max(1, min(256, env_int("PULLWISE_LOG_STREAM_MAX_SESSIONS", 16)))


def log_stream_read_max_bytes() -> int:
    return max(4096, min(1024 * 1024, env_int("PULLWISE_LOG_STREAM_READ_MAX_BYTES", 128 * 1024)))


def redact_log_stream_text(value: object) -> str:
    text = public_issue_text(value)
    if not text:
        return ""
    redacted = LOG_STREAM_TOKEN_RE.sub(lambda match: f"{match.group(1) or match.group(2) or ''}[redacted]", text)
    return redacted[:4000]


def log_stream_cleanup_expired(timestamp: int | None = None) -> int:
    current_time = int(timestamp if timestamp is not None else now())
    removed = 0
    with LOG_STREAM_LOCK:
        for session_id, session in list(LOG_STREAM_SESSIONS.items()):
            if session.get("status") != "active" and session.get("updated_at", 0) < current_time - 60:
                LOG_STREAM_SESSIONS.pop(session_id, None)
                removed += 1
                continue
            if int(session.get("expires_at") or 0) < current_time:
                session["status"] = "paused"
                session["updated_at"] = current_time
        removed += log_stream_trim_sessions_locked(current_time)
    return removed


def log_stream_trim_sessions_locked(timestamp: int | None = None) -> int:
    del timestamp
    max_sessions = log_stream_max_sessions()
    overflow = len(LOG_STREAM_SESSIONS) - max_sessions
    if overflow <= 0:
        return 0
    ordered = sorted(
        LOG_STREAM_SESSIONS.items(),
        key=lambda item: (
            1 if item[1].get("status") == "active" else 0,
            int(item[1].get("updated_at") or 0),
            int(item[1].get("created_at") or 0),
            public_issue_text(item[0]),
        ),
    )
    for session_id, _session in ordered[:overflow]:
        LOG_STREAM_SESSIONS.pop(session_id, None)
    return overflow


def log_stream_session_payload(session: dict | None) -> dict | None:
    if not session:
        return None
    return {
        "id": public_issue_text(session.get("id")),
        "source": public_issue_text(session.get("source")),
        "worker_id": public_issue_text(session.get("worker_id")),
        "status": public_issue_text(session.get("status")) or "paused",
        "created_at": pull_request_timestamp(session.get("created_at")),
        "updated_at": pull_request_timestamp(session.get("updated_at")),
        "expires_at": pull_request_timestamp(session.get("expires_at")),
        "nextSequence": int(session.get("next_sequence") or 1),
        "earliestSequence": log_stream_earliest_sequence(session),
    }


def missing_log_stream_session_payload(session_id: object) -> dict:
    return {
        "id": public_issue_text(session_id),
        "source": "",
        "worker_id": "",
        "status": "paused",
        "created_at": 0,
        "updated_at": 0,
        "expires_at": 0,
        "nextSequence": 1,
        "earliestSequence": 1,
    }


def missing_log_stream_lines_payload(session_id: object) -> dict:
    return {
        "ok": True,
        "session": missing_log_stream_session_payload(session_id),
        "lines": [],
        "nextSequence": 1,
        "earliestSequence": 1,
        "truncated": False,
    }


def log_stream_earliest_sequence(session: dict) -> int:
    lines = session.get("lines") if isinstance(session.get("lines"), list) else []
    if not lines:
        return int(session.get("next_sequence") or 1)
    return int(lines[0].get("sequence") or 1)


def log_stream_append_locked(session: dict, entries: list[dict], *, timestamp: int | None = None) -> list[dict]:
    appended = []
    current_time = int(timestamp if timestamp is not None else now())
    lines = session.setdefault("lines", [])
    for entry in entries[:500]:
        line = redact_log_stream_text(entry.get("line"))
        if not line:
            continue
        item = {
            "sequence": int(session.get("next_sequence") or 1),
            "timestamp": pull_request_timestamp(entry.get("timestamp")) or current_time,
            "source": public_issue_text(entry.get("source")) or public_issue_text(session.get("source")),
            "stream": public_issue_text(entry.get("stream")) or "log",
            "line": line,
        }
        session["next_sequence"] = item["sequence"] + 1
        lines.append(item)
        appended.append(item)
    max_lines = log_stream_max_lines()
    if len(lines) > max_lines:
        del lines[: len(lines) - max_lines]
    session["updated_at"] = current_time
    return appended


def server_log_path_for_timestamp(timestamp: float | None = None) -> str:
    recorded_at = float(timestamp if timestamp is not None else time.time())
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging_config.DailyDatedFileHandler):
            return handler.path_for_date(handler.log_date_for_timestamp(recorded_at))
    log_dir = env("PULLWISE_LOG_DIR", "") or os.path.join(project_root(), ".pullwise", "logs")
    rotation_time = logging_config.parse_rotation_time(env("PULLWISE_LOG_ROTATION_TIME", "00:00"))
    helper = logging_config.DailyDatedFileHandler(log_dir, rotation_time=rotation_time)
    return helper.path_for_date(helper.log_date_for_timestamp(recorded_at))


def log_stream_file_offset(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def read_log_file_incremental(path: str, offset: int, partial: str, *, max_bytes: int) -> tuple[list[str], int, str]:
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset, partial
    start = offset if 0 <= offset <= size else 0
    try:
        with open(path, "rb") as stream:
            stream.seek(start)
            chunk = stream.read(max_bytes)
            next_offset = stream.tell()
    except OSError:
        return [], offset, partial
    if not chunk:
        return [], next_offset, partial
    text = partial + chunk.decode("utf-8", errors="replace")
    parts = text.splitlines(keepends=True)
    next_partial = ""
    if parts and not parts[-1].endswith(("\n", "\r")):
        next_partial = parts.pop()
    return [part.rstrip("\r\n") for part in parts], next_offset, next_partial


def collect_server_log_stream(session_id: str) -> None:
    session_id = public_issue_text(session_id)
    if not session_id:
        return
    with LOG_STREAM_LOCK:
        session = LOG_STREAM_SESSIONS.get(session_id)
        if not session or session.get("status") != "active" or session.get("source") != "server":
            return
        current_path = server_log_path_for_timestamp()
        path = public_issue_text(session.get("server_path")) or current_path
        if path != current_path and not os.path.exists(path):
            path = current_path
            session["server_offset"] = 0
            session["server_partial"] = ""
        lines, next_offset, next_partial = read_log_file_incremental(
            path,
            int(session.get("server_offset") or 0),
            str(session.get("server_partial") or ""),
            max_bytes=log_stream_read_max_bytes(),
        )
        session["server_path"] = path
        session["server_offset"] = next_offset
        session["server_partial"] = next_partial
        if path != current_path and not lines:
            session["server_path"] = current_path
            session["server_offset"] = 0
            session["server_partial"] = ""
        if lines:
            log_stream_append_locked(
                session,
                [{"source": "server", "stream": "app", "line": line, "timestamp": now()} for line in lines],
            )


def create_log_stream_session(source: str, *, worker_id: str = "") -> dict:
    source = public_issue_text(source).lower()
    if source not in {"server", "worker"}:
        raise ValueError("Log source must be server or worker.")
    worker_id = clean_github_access_text(worker_id) or ""
    if source == "worker":
        if not worker_id:
            raise ValueError("worker_id is required for worker log streams.")
        if not db.get_worker(worker_id, include_deleted=True):
            raise ResourceNotFound("Worker not found.")
    timestamp = now()
    session_id = make_id("log")
    session = {
        "id": session_id,
        "source": source,
        "worker_id": worker_id,
        "status": "active",
        "created_at": timestamp,
        "updated_at": timestamp,
        "expires_at": timestamp + log_stream_idle_timeout_seconds(),
        "next_sequence": 1,
        "lines": [],
    }
    if source == "server":
        path = server_log_path_for_timestamp(timestamp)
        session["server_path"] = path
        session["server_offset"] = log_stream_file_offset(path)
        session["server_partial"] = ""
    with LOG_STREAM_LOCK:
        log_stream_cleanup_expired(timestamp)
        for existing in LOG_STREAM_SESSIONS.values():
            if existing.get("source") == source and public_issue_text(existing.get("worker_id")) == worker_id:
                existing["status"] = "paused"
                existing["updated_at"] = timestamp
        LOG_STREAM_SESSIONS[session_id] = session
        log_stream_trim_sessions_locked(timestamp)
    return session


def pause_log_stream_session(session_id: str) -> dict | None:
    session_id = public_issue_text(session_id)
    if not session_id:
        return None
    timestamp = now()
    with LOG_STREAM_LOCK:
        session = LOG_STREAM_SESSIONS.get(session_id)
        if not session:
            return None
        session["status"] = "paused"
        session["updated_at"] = timestamp
        session["expires_at"] = timestamp
        return dict(session)


def log_stream_lines_payload(session_id: str, *, after: object = None, limit: object = None) -> dict | None:
    session_id = public_issue_text(session_id)
    if not session_id:
        return None
    collect_server_log_stream(session_id)
    timestamp = now()
    after_sequence = public_scan_count(after)
    safe_limit = max(1, min(500, public_scan_count(limit) or 200))
    with LOG_STREAM_LOCK:
        log_stream_cleanup_expired(timestamp)
        session = LOG_STREAM_SESSIONS.get(session_id)
        if not session:
            return None
        if session.get("status") == "active":
            session["expires_at"] = timestamp + log_stream_idle_timeout_seconds()
            session["updated_at"] = timestamp
        lines = session.get("lines") if isinstance(session.get("lines"), list) else []
        selected = [line for line in lines if int(line.get("sequence") or 0) > after_sequence][:safe_limit]
        earliest = log_stream_earliest_sequence(session)
        return {
            "ok": True,
            "session": log_stream_session_payload(session),
            "lines": selected,
            "nextSequence": int(session.get("next_sequence") or 1),
            "earliestSequence": earliest,
            "truncated": bool(lines and after_sequence and after_sequence < earliest - 1),
        }


def worker_log_stream_poll_payload(worker_id: str) -> dict | None:
    worker_id = clean_github_access_text(worker_id) or ""
    if not worker_id:
        return None
    timestamp = now()
    with LOG_STREAM_LOCK:
        log_stream_cleanup_expired(timestamp)
        active = [
            session
            for session in LOG_STREAM_SESSIONS.values()
            if session.get("source") == "worker"
            and session.get("status") == "active"
            and public_issue_text(session.get("worker_id")) == worker_id
            and int(session.get("expires_at") or 0) >= timestamp
        ]
        if not active:
            return None
        session = sorted(active, key=lambda item: int(item.get("created_at") or 0), reverse=True)[0]
        return log_stream_session_payload(session)


def append_worker_log_stream_lines(session_id: str, worker_id: str, entries: object) -> dict | None:
    session_id = public_issue_text(session_id)
    worker_id = clean_github_access_text(worker_id) or ""
    if not session_id or not worker_id:
        return None
    if not isinstance(entries, list):
        raise ValueError("lines must be a list.")
    timestamp = now()
    normalized = [entry for entry in entries if isinstance(entry, dict)]
    with LOG_STREAM_LOCK:
        session = LOG_STREAM_SESSIONS.get(session_id)
        if (
            not session
            or session.get("source") != "worker"
            or public_issue_text(session.get("worker_id")) != worker_id
        ):
            return None
        if session.get("status") != "active" or int(session.get("expires_at") or 0) < timestamp:
            session["status"] = "paused"
            session["updated_at"] = timestamp
            return {"accepted": False, "session": log_stream_session_payload(session), "nextSequence": session.get("next_sequence", 1)}
        appended = log_stream_append_locked(session, normalized, timestamp=timestamp)
        return {
            "accepted": True,
            "appended": len(appended),
            "session": log_stream_session_payload(session),
            "nextSequence": int(session.get("next_sequence") or 1),
        }


def worker_safe_service_id(worker_id: object) -> str:
    text = public_issue_text(worker_id)
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    safe = "".join(char if char in allowed else "-" for char in text)
    if len(safe) <= 48:
        return safe
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
    return f"{safe[:37]}-{digest}"


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
    provider_chain = default_worker_provider_chain()
    return {
        "workerVersion": version,
        "workerPackage": package,
        "latestWorkerVersion": latest_version,
        "latestWorkerPackage": latest_package,
        "configuredWorkerVersion": configured_version,
        "providerChain": list(provider_chain),
        "defaults": {
            "version": version,
            "package": package,
            "providerChain": list(provider_chain),
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


WORKER_INSTALL_PROVIDERS = ("codex",)


def default_worker_provider_chain() -> list[str]:
    providers: list[str] = []
    for plan in billing.PLAN_IDS:
        provider = billing.review_agent_provider(plan)
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
        raise ValueError("providerChain must include codex.")
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
    worker_package = default_worker_package(public.get("version"))
    codex_timeout_seconds = str(system_config.worker_codex_timeout_seconds())
    provider_chain = worker_record_provider_chain(worker)
    provider_chain_text = ",".join(provider_chain)
    safe_worker_id = worker_safe_service_id(public["worker_id"])
    service_home = f"/var/lib/pullwise-worker/{safe_worker_id}" if safe_worker_id else "/var/lib/pullwise-worker"
    service_log_dir = f"/var/log/pullwise-worker/{safe_worker_id}" if safe_worker_id else "/var/log/pullwise-worker"
    install_command = worker_install_command(
        install_url=install_url,
        server_url=server_url,
        worker_id=public["worker_id"],
        worker_name=public.get("name") or public["worker_id"],
        worker_package=worker_package,
        provider_chain=provider_chain_text,
    )
    local_install_command = worker_install_command(
        install_url=local_install_url,
        server_url=local_server_url,
        worker_id=public["worker_id"],
        worker_name=public.get("name") or public["worker_id"],
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
        "PULLWISE_CHECKOUT_ROOT": f"{service_home}/checkouts",
        "PULLWISE_LOG_DIR": service_log_dir,
        "PULLWISE_WORKER_PACKAGE": worker_package,
        "PULLWISE_SERVICE_HOME": service_home,
        "PULLWISE_ACTIVE_READINESS_CHECK_SECONDS": "60",
        "PULLWISE_DEGRADED_READINESS_CHECK_SECONDS": "600",
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
                "PULLWISE_CODEX_TIMEOUT_SECONDS": codex_timeout_seconds,
                "PULLWISE_CODEX_APP_SERVER_MAX_AGE_SECONDS": "1800",
                "PULLWISE_CODEX_APP_SERVER_MAX_TURNS": "8",
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
        f"--provider-chain {shell_quote(provider_chain)}"
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
PROVIDER="codex"
PROVIDER_CHAIN=""
WORKER_PACKAGE=""
CODEX_COMMAND="${PULLWISE_CODEX_COMMAND:-}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --server) SERVER_URL="${2:-}"; shift 2 ;;
    --worker-id) WORKER_ID="${2:-}"; shift 2 ;;
    --worker-token-file) WORKER_TOKEN="$(cat "${2:-}")"; shift 2 ;;
    --worker-name) WORKER_NAME="${2:-}"; shift 2 ;;
    --max-concurrent-jobs) shift 2 ;;
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

case "$(uname -s)" in Linux) ;; *) echo "Pullwise worker installer requires Linux" >&2; exit 1 ;; esac
case "$(uname -m)" in x86_64|aarch64|arm64) ;; *) echo "Unsupported CPU architecture: $(uname -m)" >&2; exit 1 ;; esac
if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root so the installer can create service users and systemd units." >&2
  exit 1
fi

read_os_value() {
  local key="$1"
  local os_file="${PULLWISE_WORKER_OS_RELEASE_FILE:-/etc/os-release}"
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
  case "${PULLWISE_WORKER_AUTO_INSTALL_DEPS:-1}" in
    0|false|FALSE|no|NO|off|OFF) return 1 ;;
    *) return 0 ;;
  esac
}
apt_get_bin() {
  if [ -n "${PULLWISE_WORKER_APT_GET_BIN:-}" ]; then
    printf '%s' "$PULLWISE_WORKER_APT_GET_BIN"
    return 0
  fi
  command -v apt-get 2>/dev/null
}
install_ubuntu_packages() {
  local packages=("$@")
  [ "${#packages[@]}" -gt 0 ] || return 0
  auto_install_enabled || {
    echo "Missing dependencies: ${packages[*]}. Dependency auto-install is disabled by PULLWISE_WORKER_AUTO_INSTALL_DEPS." >&2
    exit 1
  }
  is_ubuntu_2204 || {
    echo "Missing dependencies: ${packages[*]}. Automatic installation is supported on Ubuntu 22.04 hosts." >&2
    exit 1
  }
  local apt_get
  apt_get="$(apt_get_bin)"
  [ -n "$apt_get" ] || {
    echo "Missing dependencies: ${packages[*]}. apt-get is required for Ubuntu 22.04 dependency installation." >&2
    exit 1
  }
  echo "Installing Ubuntu packages: ${packages[*]}"
  DEBIAN_FRONTEND=noninteractive "$apt_get" update
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
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "$label is still unavailable after installing: $*" >&2
    exit 1
  }
}
ensure_python_runtime() {
  if ! command -v python3.10 >/dev/null 2>&1 || ! python3.10 -m pip --version >/dev/null 2>&1; then
    install_ubuntu_packages python3.10 python3.10-venv python3-pip
  fi
  command -v python3.10 >/dev/null 2>&1 || {
    echo "python3.10 is still unavailable after installing Ubuntu packages." >&2
    exit 1
  }
  python3.10 -m pip --version >/dev/null 2>&1 || {
    echo "python3.10 pip is still unavailable after installing Ubuntu packages." >&2
    exit 1
  }
  python3.10 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Pullwise worker requires Python 3.10 or newer.")
PY
  PYTHON_BIN="$(python3.10 -c 'import sys; print(sys.executable)')"
}
node20_available() {
  command -v node >/dev/null 2>&1 || return 1
  node --version 2>/dev/null | sed -n 's/^v\([0-9][0-9]*\).*/\1/p' | awk '{ exit ($1 >= 20 ? 0 : 1) }'
}
ensure_nodesource_nodejs() {
  if node20_available && command -v npm >/dev/null 2>&1; then
    return 0
  fi
  install_ubuntu_packages ca-certificates curl gnupg
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor --yes -o /etc/apt/keyrings/nodesource.gpg
  chmod 0644 /etc/apt/keyrings/nodesource.gpg
  printf '%s\n' 'deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main' >/etc/apt/sources.list.d/nodesource.list
  DEBIAN_FRONTEND=noninteractive "$(apt_get_bin)" update
  DEBIAN_FRONTEND=noninteractive "$(apt_get_bin)" install -y --no-install-recommends nodejs
  node20_available && command -v npm >/dev/null 2>&1 || {
    echo "Node.js 20+ and npm are still unavailable after NodeSource install." >&2
    exit 1
  }
}

ensure_command_available "sha256sum" sha256sum coreutils
safe_worker_id() {
  local raw safe digest prefix
  raw="$1"
  safe="$(printf '%s' "$raw" | tr -c 'A-Za-z0-9_-' '-')"
  if [ "${#safe}" -le 48 ]; then
    printf '%s\n' "$safe"
    return 0
  fi
  digest="$(printf '%s' "$raw" | sha256sum)"
  digest="${digest%% *}"
  digest="$(printf '%s' "$digest" | cut -c1-10)"
  prefix="$(printf '%s' "$safe" | cut -c1-37)"
  printf '%s-%s\n' "$prefix" "$digest"
}
service_user_name() {
  local prefix digest
  prefix="$(printf '%s' "$1" | tr 'A-Z_' 'a-z-' | tr -cd 'a-z0-9-' | cut -c1-10)"
  if [ -z "$prefix" ]; then
    prefix="worker"
  fi
  digest="$(printf '%s' "$1" | sha256sum)"
  digest="${digest%% *}"
  digest="$(printf '%s' "$digest" | cut -c1-10)"
  printf 'pw-worker-%s-%s\n' "$prefix" "$digest"
}
SAFE_WORKER_ID="$(safe_worker_id "$WORKER_ID")"
if [ -z "$SAFE_WORKER_ID" ]; then
  echo "worker id does not contain any safe service-name characters" >&2
  exit 2
fi
SERVICE_USER="$(service_user_name "$SAFE_WORKER_ID")"
BASE_CONFIG_DIR="/etc/pullwise-worker"
BASE_DATA_DIR="/var/lib/pullwise-worker"
BASE_LOG_DIR="/var/log/pullwise-worker"
SERVICE_NAME="pullwise-worker-$SAFE_WORKER_ID"
WATCHER_SERVICE_NAME="$SERVICE_NAME-watcher"
CONFIG_DIR="$BASE_CONFIG_DIR/$SAFE_WORKER_ID"
ENV_FILE="$CONFIG_DIR/worker.env"
AUTH_COMMANDS_FILE="$CONFIG_DIR/auth-commands.txt"
BIN_PATH="/usr/local/bin/$SERVICE_NAME"
DATA_DIR="$BASE_DATA_DIR/$SAFE_WORKER_ID"
CHECKOUT_ROOT="$DATA_DIR/checkouts"
LOG_DIR="$BASE_LOG_DIR/$SAFE_WORKER_ID"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"
WATCHER_SERVICE_FILE="/etc/systemd/system/$WATCHER_SERVICE_NAME.service"
LOGROTATE_FILE="/etc/logrotate.d/$SERVICE_NAME"
UNINSTALL_MARKER_FILE="/run/$SERVICE_NAME/uninstall-requested"
INSTALL_COMPLETED=0
ROLLBACK_ENABLED=0
HAD_SERVICE_USER=0
HAD_CONFIG_DIR=0
HAD_DATA_DIR=0
HAD_LOG_DIR=0
HAD_BIN_PATH=0
HAD_SERVICE_FILE=0
HAD_WATCHER_SERVICE_FILE=0
HAD_LOGROTATE_FILE=0
id "$SERVICE_USER" >/dev/null 2>&1 && HAD_SERVICE_USER=1
[ -e "$CONFIG_DIR" ] && HAD_CONFIG_DIR=1
[ -e "$DATA_DIR" ] && HAD_DATA_DIR=1
[ -e "$LOG_DIR" ] && HAD_LOG_DIR=1
[ -e "$BIN_PATH" ] && HAD_BIN_PATH=1
[ -e "$SERVICE_FILE" ] && HAD_SERVICE_FILE=1
[ -e "$WATCHER_SERVICE_FILE" ] && HAD_WATCHER_SERVICE_FILE=1
[ -e "$LOGROTATE_FILE" ] && HAD_LOGROTATE_FILE=1
rollback_dir() {
  local path="$1"
  local base="$2"
  local existed="$3"
  [ "$existed" = "0" ] || return 0
  [ -n "$path" ] || return 0
  case "$path" in
    "$base"/*) rm -rf -- "$path" >/dev/null 2>&1 || true ;;
    *) echo "Refusing to roll back unexpected directory: $path" >&2 ;;
  esac
}
rollback_file() {
  local path="$1"
  local base="$2"
  local existed="$3"
  [ "$existed" = "0" ] || return 0
  [ -n "$path" ] || return 0
  case "$path" in
    "$base"/*) rm -f -- "$path" >/dev/null 2>&1 || true ;;
    *) echo "Refusing to roll back unexpected file: $path" >&2 ;;
  esac
}
rollback_failed_install() {
  local status=$?
  trap - EXIT
  if [ "$status" -eq 0 ] || [ "$INSTALL_COMPLETED" = "1" ] || [ "$ROLLBACK_ENABLED" != "1" ]; then
    exit "$status"
  fi
  if [ "${PULLWISE_KEEP_FAILED_INSTALL:-}" = "1" ]; then
    echo "Pullwise worker install failed; preserving partial instance because PULLWISE_KEEP_FAILED_INSTALL=1." >&2
    exit "$status"
  fi
  echo "Pullwise worker install failed; rolling back partial instance $SAFE_WORKER_ID." >&2
  systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
  systemctl stop "$WATCHER_SERVICE_NAME" >/dev/null 2>&1 || true
  systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
  systemctl disable "$WATCHER_SERVICE_NAME" >/dev/null 2>&1 || true
  rollback_file "$SERVICE_FILE" "/etc/systemd/system" "$HAD_SERVICE_FILE"
  rollback_file "$WATCHER_SERVICE_FILE" "/etc/systemd/system" "$HAD_WATCHER_SERVICE_FILE"
  rollback_file "$LOGROTATE_FILE" "/etc/logrotate.d" "$HAD_LOGROTATE_FILE"
  rollback_file "$BIN_PATH" "/usr/local/bin" "$HAD_BIN_PATH"
  rollback_dir "$CONFIG_DIR" "$BASE_CONFIG_DIR" "$HAD_CONFIG_DIR"
  rollback_dir "$DATA_DIR" "$BASE_DATA_DIR" "$HAD_DATA_DIR"
  rollback_dir "$LOG_DIR" "$BASE_LOG_DIR" "$HAD_LOG_DIR"
  if [ "$HAD_SERVICE_USER" = "0" ]; then
    userdel "$SERVICE_USER" >/dev/null 2>&1 || true
  fi
  systemctl daemon-reload >/dev/null 2>&1 || true
  exit "$status"
}
trap rollback_failed_install EXIT
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
      codex)
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
SERVICE_TOOL_PATH="$DATA_DIR/.local/bin:$DATA_DIR/.codex/bin:$SERVICE_PATH"
CODEX_HOME="$DATA_DIR/.codex"
CODEX_SQLITE_HOME="$DATA_DIR/.codex-sqlite"
XDG_CONFIG_HOME="$DATA_DIR/.config"
XDG_CACHE_HOME="$DATA_DIR/.cache"
XDG_DATA_HOME="$DATA_DIR/.local/share"

run_as_service_user() {
  (
    cd "$DATA_DIR"
    if command -v runuser >/dev/null 2>&1; then
      runuser -u "$SERVICE_USER" -- env HOME="$DATA_DIR" USERPROFILE="$DATA_DIR" CODEX_HOME="$CODEX_HOME" CODEX_SQLITE_HOME="$CODEX_SQLITE_HOME" XDG_CONFIG_HOME="$XDG_CONFIG_HOME" XDG_CACHE_HOME="$XDG_CACHE_HOME" XDG_DATA_HOME="$XDG_DATA_HOME" PATH="$SERVICE_TOOL_PATH" "$@"
    elif command -v sudo >/dev/null 2>&1; then
      sudo -u "$SERVICE_USER" env HOME="$DATA_DIR" USERPROFILE="$DATA_DIR" CODEX_HOME="$CODEX_HOME" CODEX_SQLITE_HOME="$CODEX_SQLITE_HOME" XDG_CONFIG_HOME="$XDG_CONFIG_HOME" XDG_CACHE_HOME="$XDG_CACHE_HOME" XDG_DATA_HOME="$XDG_DATA_HOME" PATH="$SERVICE_TOOL_PATH" "$@"
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
  printf 'sudo -u %q env HOME=%q USERPROFILE=%q CODEX_HOME=%q CODEX_SQLITE_HOME=%q XDG_CONFIG_HOME=%q XDG_CACHE_HOME=%q XDG_DATA_HOME=%q PATH=%q sh -lc %q\n' \
    "$SERVICE_USER" "$DATA_DIR" "$DATA_DIR" "$CODEX_HOME" "$CODEX_SQLITE_HOME" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$SERVICE_TOOL_PATH" "cd \"\$HOME\" && exec $command_line"
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
  resolved_home="$("${PYTHON_BIN:-python3.10}" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$DATA_DIR")"
  resolved_command="$("${PYTHON_BIN:-python3.10}" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$command_path")"
  case "$resolved_command/" in
    "$resolved_home"/*) ;;
    *)
      echo "$label command must be inside worker home $DATA_DIR: $command_path" >&2
      exit 1
      ;;
  esac
}
ensure_python_runtime
ensure_command_available "git" git git
ensure_command_available "curl" curl curl ca-certificates
ensure_command_available "getent" getent libc-bin
ensure_command_available "install" install coreutils
ensure_command_available "runuser" runuser util-linux
ensure_command_available "systemctl" systemctl systemd
ensure_command_available "tar" tar tar
ensure_command_available "useradd" useradd passwd
ensure_command_available "userdel" userdel passwd
if provider_chain_has codex; then
  ensure_nodesource_nodejs
fi

ROLLBACK_ENABLED=1
if id "$SERVICE_USER" >/dev/null 2>&1; then
  existing_home="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
  if [ "$existing_home" != "$DATA_DIR" ]; then
    echo "service user $SERVICE_USER already exists with home $existing_home; expected $DATA_DIR" >&2
    exit 1
  fi
else
  useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -m 0755 -o root -g root "$BASE_CONFIG_DIR"
install -d -m 0755 -o root -g root "$BASE_DATA_DIR" "$BASE_LOG_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CONFIG_DIR" "$DATA_DIR" "$CHECKOUT_ROOT" "$LOG_DIR" "$CODEX_HOME" "$CODEX_SQLITE_HOME" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR/.local" "$DATA_DIR/.local/bin" "$DATA_DIR/.codex/bin"
if [ ! -f "$CODEX_HOME/config.toml" ]; then
  install -m 0640 -o "$SERVICE_USER" -g "$SERVICE_USER" /dev/null "$CODEX_HOME/config.toml"
fi

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

"$PYTHON_BIN" -m pip install --upgrade --force-reinstall --no-cache-dir "$WORKER_PACKAGE"

write_env_value() {
  local key="$1"
  local value="$2"
  if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
    echo "environment value for $key must be single-line" >&2
    exit 2
  fi
  printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
}
codex_device_auth_command() {
  service_user_auth_command "$CODEX_COMMAND" login --device-auth
}
write_auth_commands() {
  {
    echo "Pullwise worker manual authorization commands"
    echo "Worker: $WORKER_NAME ($WORKER_ID)"
    echo "Home: $DATA_DIR"
    echo "Provider chain: $PROVIDER_CHAIN"
    if provider_chain_has codex; then
      echo "Codex device login:"
      codex_device_auth_command
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
run_default_auth_commands() {
  provider_chain_has codex || return 0
  local auth_command
  auth_command="$(codex_device_auth_command)"
  echo "Starting Codex device login. The same command is saved in $AUTH_COMMANDS_FILE."
  if ! eval "$auth_command"; then
    echo "Codex device login did not complete. Re-run the saved command to authorize later." >&2
  fi
}

: > "$ENV_FILE"
write_env_value PULLWISE_SERVER_URL "$SERVER_URL"
write_env_value PULLWISE_WORKER_ID "$WORKER_ID"
write_env_value PULLWISE_WORKER_TOKEN "$WORKER_TOKEN"
write_env_value PULLWISE_PROVIDER "$PROVIDER"
write_env_value PULLWISE_PROVIDER_CHAIN "$PROVIDER_CHAIN"
write_env_value PULLWISE_CHECKOUT_ROOT "$CHECKOUT_ROOT"
write_env_value PULLWISE_LOG_DIR "$LOG_DIR"
write_env_value PULLWISE_WORKER_PACKAGE "$WORKER_PACKAGE"
if provider_chain_has codex; then
  write_env_value PULLWISE_CODEX_COMMAND "$CODEX_COMMAND"
  write_env_value PULLWISE_CODEX_SQLITE_HOME "$CODEX_SQLITE_HOME"
  write_env_value PULLWISE_CODEX_MODEL "${PULLWISE_CODEX_MODEL:-gpt-5.5}"
  write_env_value PULLWISE_CODEX_REASONING_EFFORT "${PULLWISE_CODEX_REASONING_EFFORT:-medium}"
  write_env_value PULLWISE_CODEX_TIMEOUT_SECONDS "${PULLWISE_CODEX_TIMEOUT_SECONDS:-__PULLWISE_CODEX_TIMEOUT_SECONDS__}"
  write_env_value PULLWISE_CODEX_APP_SERVER_MAX_AGE_SECONDS "${PULLWISE_CODEX_APP_SERVER_MAX_AGE_SECONDS:-1800}"
  write_env_value PULLWISE_CODEX_APP_SERVER_MAX_TURNS "${PULLWISE_CODEX_APP_SERVER_MAX_TURNS:-8}"
fi
write_env_value PULLWISE_PYTHON_BIN "$PYTHON_BIN"
write_env_value PULLWISE_SERVICE_PATH "$SERVICE_PATH"
write_env_value PULLWISE_SERVICE_USER "$SERVICE_USER"
write_env_value PULLWISE_SERVICE_HOME "$DATA_DIR"
write_env_value PULLWISE_SERVICE_NAME "$SERVICE_NAME"
write_env_value PULLWISE_SERVICE_FILE "$SERVICE_FILE"
write_env_value PULLWISE_LIFECYCLE_WATCHER_ENABLED "1"
write_env_value PULLWISE_WATCHER_SERVICE_NAME "$WATCHER_SERVICE_NAME"
write_env_value PULLWISE_WATCHER_SERVICE_FILE "$WATCHER_SERVICE_FILE"
write_env_value PULLWISE_WATCHER_POLL_SECONDS "5"
write_env_value PULLWISE_WORKER_ENV_FILE "$ENV_FILE"
write_env_value PULLWISE_WORKER_ENV_BACKUP_FILE "$ENV_FILE.bak"
write_env_value PULLWISE_WORKER_BIN_PATH "$BIN_PATH"
write_env_value PULLWISE_LOGROTATE_FILE "$LOGROTATE_FILE"
write_env_value PULLWISE_REMOTE_UNINSTALL_FINALIZER "1"
write_env_value PULLWISE_UNINSTALL_MARKER_FILE "$UNINSTALL_MARKER_FILE"
write_env_value PULLWISE_ACTIVE_READINESS_CHECK_SECONDS "60"
write_env_value PULLWISE_DEGRADED_READINESS_CHECK_SECONDS "600"
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
SERVICE_HOME="\${PULLWISE_SERVICE_HOME:-/var/lib/pullwise-worker}"
export HOME="\$SERVICE_HOME"
export USERPROFILE="\$SERVICE_HOME"
export CODEX_HOME="\$SERVICE_HOME/.codex"
export CODEX_SQLITE_HOME="\$SERVICE_HOME/.codex-sqlite"
export XDG_CONFIG_HOME="\$SERVICE_HOME/.config"
export XDG_CACHE_HOME="\$SERVICE_HOME/.cache"
export XDG_DATA_HOME="\$SERVICE_HOME/.local/share"
SERVICE_PATH="\${PULLWISE_SERVICE_PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
export PATH="\$SERVICE_HOME/.local/bin:\$SERVICE_HOME/.codex/bin:\$SERVICE_PATH"
PYTHON_BIN="\${PULLWISE_PYTHON_BIN:-python3.10}"
exec "\$PYTHON_BIN" -m pullwise_worker.main "\$@"
EOF
chmod 0755 "$BIN_PATH"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Pullwise Worker $WORKER_NAME
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$DATA_DIR
EnvironmentFile=$ENV_FILE
Environment=PATH=$SERVICE_TOOL_PATH
Environment=HOME=$DATA_DIR
Environment=USERPROFILE=$DATA_DIR
Environment=CODEX_HOME=$DATA_DIR/.codex
Environment=CODEX_SQLITE_HOME=$DATA_DIR/.codex-sqlite
Environment=XDG_CONFIG_HOME=$DATA_DIR/.config
Environment=XDG_CACHE_HOME=$DATA_DIR/.cache
Environment=XDG_DATA_HOME=$DATA_DIR/.local/share
ExecStart=$BIN_PATH run
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$DATA_DIR $LOG_DIR
RuntimeDirectory=$SERVICE_NAME
RuntimeDirectoryMode=0750

[Install]
WantedBy=multi-user.target
EOF

cat > "$WATCHER_SERVICE_FILE" <<EOF
[Unit]
Description=Pullwise Worker Watcher $WORKER_NAME
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=/
EnvironmentFile=$ENV_FILE
ExecStart=$BIN_PATH watch
Restart=on-failure
RestartSec=5
NoNewPrivileges=false
RuntimeDirectory=$WATCHER_SERVICE_NAME
RuntimeDirectoryMode=0750

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
print_auth_commands
run_default_auth_commands
systemctl restart "$SERVICE_NAME"
if ! run_as_service_user "$BIN_PATH" doctor; then
  echo "Pullwise worker doctor failed; leaving service stopped and rolling back install." >&2
  systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
  exit 1
fi
systemctl enable "$SERVICE_NAME" >/dev/null
systemctl enable "$WATCHER_SERVICE_NAME" >/dev/null
systemctl restart "$WATCHER_SERVICE_NAME"
INSTALL_COMPLETED=1
echo "Pullwise worker installed as $WORKER_NAME ($WORKER_ID)."
echo "Systemd service: $SERVICE_NAME"
echo "Watcher service: $WATCHER_SERVICE_NAME"
echo "Worker home: $DATA_DIR"
"""
    return (
        script.replace("__DEFAULT_WORKER_PACKAGE__", default_worker_package())
        .replace("__PULLWISE_CODEX_TIMEOUT_SECONDS__", str(system_config.worker_codex_timeout_seconds()))
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
        "noRecentError": not bool(clean_scan_error(worker.get("last_error"))),
    }
    return {"ok": all(checks.values()), "checks": checks}
