from __future__ import annotations

# Loaded by app.py; keep definitions in that module's globals for compatibility.

from . import _app_part_05_worker_results as _previous_app_part
from . import worker_runtime_catalog
from . import worker_node_installer
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
    catalog_providers = [
        item["provider"]
        for item in worker_runtime_catalog.available_models(
            worker.get("runtime_catalog"),
            include_credentials=False,
        )
    ]
    if catalog_providers:
        return list(dict.fromkeys(catalog_providers))
    decoded = decoded_worker_json_payload(worker.get("provider_chain"), list)
    raw = decoded or worker.get("provider_chain") or worker.get("providerChain")
    if raw:
        return worker_provider_chain(raw)
    provider = public_issue_text(worker.get("provider")).lower()
    return [] if provider in {"", "unconfigured"} else worker_provider_chain(provider)


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
    if worker_runtime_catalog.routable_selections(worker.get("runtime_catalog")):
        return True
    if worker_runtime_catalog.normalize_runtime_catalog(worker.get("runtime_catalog")) is not None:
        return False
    return bool(worker_record_provider_chain(worker))


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
    codex_quota = worker_codex_quota_payload(worker) or {}
    quota_readiness_blocked = (
        codex_quota.get("ready") is False
        or public_issue_text(codex_quota.get("status")).lower() == "exhausted"
        or public_issue_text(codex_quota.get("reason")).lower() == "codex_quota_exhausted"
    )
    provider_readiness_blocked = quota_readiness_blocked or (
        not ready_providers
        and (codex_ready == 0 or doctor_status in {"degraded", "failed", "not_ready"})
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



def worker_codex_quota_payload(worker: dict) -> dict | None:
    quota = decoded_worker_json_payload(worker.get("codex_quota"), dict)
    if not isinstance(quota, dict) and isinstance(worker.get("codexQuota"), dict):
        quota = dict(worker["codexQuota"])
    return quota if isinstance(quota, dict) else None

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
    worker_scope = db.WORKER_SCOPE_SHARED
    payload = {
        "worker_id": public_issue_text(worker.get("worker_id")),
        "name": public_issue_text(worker.get("name")) or public_issue_text(worker.get("worker_id")),
        "scope": worker_scope,
        "provider": public_issue_text(worker.get("provider")) or (provider_chain[0] if provider_chain else "unconfigured"),
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
        runtime_catalog = worker_runtime_catalog.public_runtime_catalog(worker.get("runtime_catalog"))
        if runtime_catalog is not None:
            payload["runtimeCatalog"] = runtime_catalog
        payload["availableModels"] = worker_runtime_catalog.available_models(
            worker.get("runtime_catalog"),
            include_credentials=True,
        )
        payload["hostname"] = public_issue_text(worker.get("hostname"))
        payload["last_error"] = clean_scan_error(worker.get("last_error"))
        payload["doctor_status"] = public_issue_text(worker.get("doctor_status"))
        payload["codex_ready"] = bool(worker.get("codex_ready")) if worker.get("codex_ready") is not None else None
        codex_quota = worker_codex_quota_payload(worker)
        if codex_quota:
            payload["codexQuota"] = codex_quota
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


def fleet_available_review_models(workers: list[dict]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for worker in workers:
        for item in worker_runtime_catalog.available_models(
            worker.get("runtime_catalog"),
            include_credentials=False,
        ):
            identity = (item["provider"], item["model"])
            if identity in seen:
                continue
            seen.add(identity)
            result.append(item)
    return sorted(result, key=lambda item: (item["provider"], item["model"]))


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
        f"v{version}/pullwise-worker-{version}.tgz"
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
    selected_version = public_issue_text(version) or system_config.worker_default_version().strip() or DEFAULT_WORKER_PACKAGE_VERSION
    if not WORKER_PACKAGE_RELEASE_RE.fullmatch(selected_version):
        selected_version = DEFAULT_WORKER_PACKAGE_VERSION
    return worker_release_package(selected_version)


def default_worker_provider_chain() -> list[str]:
    return []


def worker_provider_chain(value: object = None, *, strict: bool = False) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        raw_items = value.split(",")
    elif value is None:
        raw_items = default_worker_provider_chain()
    else:
        raw_items = []
    providers = db.normalize_provider_list(raw_items)
    if providers:
        return providers
    if strict:
        raise ValueError("providerChain is invalid.")
    return default_worker_provider_chain()


def worker_provider_chain_text(value: object = None, *, strict: bool = False) -> str:
    return ",".join(worker_provider_chain(value, strict=strict))


def request_bool(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = public_issue_text(value).lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


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
    provider_chain = worker_record_provider_chain(worker)
    provider_chain_text = ",".join(provider_chain)
    safe_worker_id = worker_safe_service_id(public["worker_id"])
    service_home = f"/var/lib/pullwise-worker/{safe_worker_id}" if safe_worker_id else "/var/lib/pullwise-worker"
    worker_runtime_root = f"{service_home}/workers/{safe_worker_id or 'worker'}"
    service_user = f"pullwise-worker-{safe_worker_id}" if safe_worker_id else "pullwise-worker"
    bin_path = f"/usr/local/bin/pullwise-worker-{safe_worker_id}" if safe_worker_id else "/usr/local/bin/pullwise-worker"
    env_file = f"/etc/pullwise-worker/{safe_worker_id}/worker.env" if safe_worker_id else "/etc/pullwise-worker/worker.env"
    profile_root = f"{worker_runtime_root}/pi-profiles"
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
        "PULLWISE_CHECKOUT_ROOT": f"{service_home}/checkouts",
        "PULLWISE_WORKER_ROOT": worker_runtime_root,
        "PULLWISE_PI_PROFILE_ROOT": profile_root,
        "PULLWISE_WORKER_STATE_ROOT": f"{worker_runtime_root}/state",
        "PULLWISE_WORKER_PACKAGE": worker_package,
        "PULLWISE_SERVICE_HOME": service_home,
    }
    script_hash = worker_install_script_sha256()
    sync_catalog_command = (
        f"sudo -u {shell_quote(service_user)} sh -lc "
        f"{shell_quote(f'. {env_file}; exec {bin_path} sync')}"
    )
    return {
        **public,
        "worker_id": public["worker_id"],
        "worker": public,
        "worker_token": token,
        "token": token,
        "install_script_url": install_url,
        "install_commands": {
            "standard": install_command,
            "local": local_install_command,
            "script_sha256": script_hash,
        },
        "suggested_env": suggested_env,
        "configuration": {
            "secretsStoredOnWorker": False,
            "profileRoot": profile_root,
            "selectionOwner": "pullwise-server",
            "profileMode": "server-managed",
            "upstreamSecretsOwner": "pullwise-model-gateway",
        },
        "configuration_commands": [
            {
                "key": "sync_managed_profile",
                "title": "Pull and apply assigned Profile Set",
                "command": sync_catalog_command,
            },
        ],
    }


def worker_install_command(
    *,
    install_url: str,
    server_url: str,
    worker_id: str,
    worker_name: str,
    worker_package: str,
    provider_chain: str,
) -> str:
    del provider_chain
    script_hash = worker_install_script_sha256()
    return (
        "read -rsp 'Pullwise worker token: ' PULLWISE_WORKER_TOKEN; echo; "
        "export PULLWISE_WORKER_TOKEN; "
        "install_script=\"$(mktemp)\"; "
        "trap 'rm -f \"$install_script\"' EXIT; "
        f"curl -fsSL {shell_quote(install_url)} -o \"$install_script\"; "
        f"printf '%s  %s\\n' {shell_quote(script_hash)} \"$install_script\" | sha256sum -c - >/dev/null; "
        "bash \"$install_script\" "
        f"--server {shell_quote(server_url)} "
        f"--worker-id {shell_quote(worker_id)} "
        f"--worker-name {shell_quote(worker_name)} "
        f"--package {shell_quote(worker_package)}"
    )

def shell_quote(value: object) -> str:
    text = public_issue_text(value)
    if not text:
        return "''"
    return "'" + text.replace("'", "'\"'\"'") + "'"


def worker_install_script_sha256() -> str:
    return hashlib.sha256(worker_install_script().encode("utf-8")).hexdigest()


def worker_install_script() -> str:
    return worker_node_installer.render()
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



