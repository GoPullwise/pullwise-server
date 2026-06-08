from __future__ import annotations

# Loaded by app.py; keep definitions in that module's globals for compatibility.


import argparse
import hashlib
import io
import json
import logging
import math
import mimetypes
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse, urlunparse

from . import billing, checkout, db, fix_workflow, github_auth, logging_config, quota, review, scan_logging

logger = logging.getLogger(__name__)
access_logger = logging.getLogger("pullwise_server.access")

def project_root() -> str:
    return os.path.dirname(os.path.dirname(__file__))


def web_root() -> str:
    """Return the path to the built frontend assets."""
    custom = env("PULLWISE_WEB_DIR", "")
    if custom:
        return os.path.abspath(custom)
    # Default: ../pullwise-web/dist relative to this file
    return os.path.join(os.path.dirname(project_root()), "pullwise-web", "dist")


def load_env_file(path: str | None = None) -> None:
    env_path = path or os.path.join(project_root(), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

SESSION_COOKIE = "pw_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7
GITHUB_STATE_MAX_AGE = 60 * 10
ISSUE_STATUSES = {"open", "fixed", "snoozed"}
ISSUE_VERIFICATION_STATUSES = {"verified", "static_proof", "potential_risk", "unverified"}
ISSUE_EVIDENCE_TYPES = {
    "code",
    "path",
    "trigger",
    "runtime_log",
    "test",
    "environment",
    "tool",
    "documentation",
    "fix_verification",
}
AUDIT_SWARM_EVIDENCE_BLOCK_KINDS = {
    "summary",
    "claim",
    "code_location",
    "evidence",
    "command",
    "verifier_verdict",
    "false_positive_check",
    "invariant",
    "risk",
}
CONVERGENCE_PROTOCOL_VERSION = "pullwise-convergence/0.1"
SCAN_STATUSES = {"queued", "running", "done", "failed", "cancelled"}
SCAN_JOB_STATUSES = {"queued", "claimed", "running", "uploading_result", "done", "failed", "cancelled", "lost", "retrying"}
SCAN_PHASES = {"clone", "index", "secrets", "deps", "ai", "report"}
BILLING_PUBLIC_STATUSES = {"none", "active", "trialing", "canceling", "past_due", "unpaid", "paused", "canceled"}
API_KEY_PREFIX = "pwk_"
API_KEY_ALLOWED_SCOPES = {"repositories:read", "scans:read", "scans:write", "quota:read"}
API_KEY_DEFAULT_SCOPES = ["repositories:read", "scans:read", "scans:write", "quota:read"]
WINDOWS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[/\\]")
GIT_COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
SCAN_REQUEST_COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

USERS: dict[str, dict] = {}
SESSIONS: dict[str, dict] = {}
GITHUB_STATES: dict[str, dict] = {}
SETTINGS: dict[str, dict] = {}
BILLING_EVENTS: dict[str, dict] = {}
BILLING_PENDING_UPDATES: list[dict] = []
STATE_LOADED = False
STATE_DIRTY = False
LAST_RESOURCE_CLEANUP_AT = 0.0

DEFAULT_REPOSITORIES: list[dict] = [
    {
        "id": "repo_pullwise_web",
        "name": "pullwise-web",
        "fullName": "pullwise/pullwise-web",
        "desc": "Pullwise frontend",
        "description": "Pullwise frontend",
        "lang": "JavaScript",
        "private": True,
        "stars": "-",
        "branches": "-",
        "defaultBranch": "main",
        "updated": "",
        "htmlUrl": "https://github.com/pullwise/pullwise-web",
        "cloneUrl": "https://github.com/pullwise/pullwise-web.git",
        "permissions": {"pull": True},
    },
    {
        "id": "repo_pullwise_server",
        "name": "pullwise-server",
        "fullName": "pullwise/pullwise-server",
        "desc": "Pullwise local API server",
        "description": "Pullwise local API server",
        "lang": "Python",
        "private": True,
        "stars": "-",
        "branches": "-",
        "defaultBranch": "main",
        "updated": "",
        "htmlUrl": "https://github.com/pullwise/pullwise-server",
        "cloneUrl": "https://github.com/pullwise/pullwise-server.git",
        "permissions": {"pull": True},
    },
]

REPOSITORIES: list[dict] = [dict(repo) for repo in DEFAULT_REPOSITORIES]
ISSUES: list[dict] = []
SCANS: list[dict] = []
DEFAULT_WORKER_PACKAGE_VERSION = "0.1.8"
DEFAULT_WORKER_PACKAGE = (
    "https://github.com/GoPullwise/pullwise-worker/releases/download/"
    f"v{DEFAULT_WORKER_PACKAGE_VERSION}/pullwise_worker-{DEFAULT_WORKER_PACKAGE_VERSION}-py3-none-any.whl"
)
DEFAULT_WORKER_RELEASES_API_URL = "https://api.github.com/repos/GoPullwise/pullwise-worker/releases/latest"
WORKER_PACKAGE_RELEASE_RE = re.compile(r"^\d+\.\d+\.\d+$")
LATEST_WORKER_RELEASE_CACHE: dict[str, object] = {"version": "", "checked_at": 0.0}

# Re-entrant so worker mutations can call persist_state() while already holding
# the lock. Protects against worker/handler interleaving on SCANS and ISSUES.
STATE_LOCK = threading.RLock()


class PreviewScanLockEntry:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.refs = 0


class AuditBundleCacheLockEntry:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.refs = 0


PREVIEW_SCAN_LOCKS: dict[str, PreviewScanLockEntry] = {}
PREVIEW_SCAN_LOCKS_GUARD = threading.Lock()
AUDIT_BUNDLE_CACHE_LOCKS: dict[str, AuditBundleCacheLockEntry] = {}
AUDIT_BUNDLE_CACHE_LOCKS_GUARD = threading.Lock()
MAX_BILLING_EVENT_RECORDS = 5000
MAX_BILLING_PENDING_UPDATES = 1000


class RequestBodyTooLarge(ValueError):
    pass


class ClientDisconnected(ConnectionError):
    pass


_CLIENT_DISCONNECT_EXCEPTIONS = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)


class ResourceNotFound(Exception):
    def __init__(self, label: str) -> None:
        safe_label = label if label in {"Issue", "Scan"} else "Resource"
        super().__init__(f"{safe_label} not found.")


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def env_flag(name: str, default: str = "false") -> bool:
    return env(name, default).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


def parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer.") from None
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535.")
    return port


def server_port() -> int:
    try:
        return parse_port(env("PULLWISE_PORT", "8080"))
    except argparse.ArgumentTypeError:
        return 8080


def max_body_bytes() -> int:
    return max(0, env_int("PULLWISE_MAX_BODY_BYTES", 1024 * 1024))


def decode_json_body(raw_bytes: bytes) -> dict:
    if not raw_bytes:
        return {}
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Request body must be valid JSON.") from None
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("Request body must be valid JSON.") from None


def rate_limit_enabled() -> bool:
    configured = os.environ.get("PULLWISE_RATE_LIMIT_ENABLED")
    if configured is not None:
        return env_flag("PULLWISE_RATE_LIMIT_ENABLED")
    return env("PULLWISE_MODE", "local").strip().lower() == "production"


def rate_limit_requests() -> int:
    return max(0, env_int("PULLWISE_RATE_LIMIT_REQUESTS", 600))


def rate_limit_window_seconds() -> int:
    return max(1, env_int("PULLWISE_RATE_LIMIT_WINDOW_SECONDS", 60))


def rate_limit_exempt_path(method: str, path: str) -> bool:
    return method == "OPTIONS" or path == "/health"


def request_header(handler: BaseHTTPRequestHandler, name: str) -> str | None:
    value = handler.headers.get(name)
    if value:
        return value
    target = name.lower()
    if isinstance(handler.headers, dict):
        for key, candidate in handler.headers.items():
            if key.lower() == target and candidate:
                return candidate
    return None


def bearer_token(handler: BaseHTTPRequestHandler) -> str | None:
    authorization = first_header_value(handler, "Authorization")
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    if not token or any(char in token for char in "\r\n"):
        return None
    return token


def api_key_token(handler: BaseHTTPRequestHandler) -> str | None:
    authorization_token = bearer_token(handler)
    if authorization_token and authorization_token.startswith(API_KEY_PREFIX):
        return authorization_token
    header_token = first_header_value(handler, "X-Pullwise-Api-Key")
    if header_token and header_token.startswith(API_KEY_PREFIX) and not any(char in header_token for char in "\r\n"):
        return header_token
    return None


def api_key_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def api_key_prefix(token: str) -> str:
    return token[:16]


def worker_token_record(handler: BaseHTTPRequestHandler, *, allow_disabled: bool = False) -> dict | None:
    token = bearer_token(handler)
    if not token:
        return None
    if allow_disabled:
        return db.get_worker_by_token(token, allow_disabled=True)
    return db.get_enabled_worker_token(token)


def admin_user_ids() -> set[str]:
    return {item.strip() for item in env("PULLWISE_ADMIN_USER_IDS", "").split(",") if item.strip()}


def admin_emails() -> set[str]:
    return {item.strip().lower() for item in env("PULLWISE_ADMIN_EMAILS", "").split(",") if item.strip()}


def user_is_admin(user: dict | None) -> bool:
    if not user:
        return False
    user_id = str(user.get("id") or "")
    email = str(user.get("email") or "").strip().lower()
    return user_id in admin_user_ids() or (email and email in admin_emails())


def request_id_from_handler(handler: BaseHTTPRequestHandler) -> str:
    return public_issue_text(first_header_value(handler, "X-Request-Id") or first_header_value(handler, "X-Correlation-Id"))


def local_github_mocks_enabled() -> bool:
    return env_flag("PULLWISE_ENABLE_LOCAL_GITHUB_MOCKS")


def persisted_state_dict(state: object, name: str) -> dict:
    if not isinstance(state, dict):
        return {}
    value = state.get(name)
    return dict(value) if isinstance(value, dict) else {}


def persisted_state_list(state: object, name: str) -> list:
    if not isinstance(state, dict):
        return []
    value = state.get(name)
    return list(value) if isinstance(value, list) else []


def ensure_state_loaded() -> None:
    global STATE_DIRTY, STATE_LOADED, USERS, SESSIONS, GITHUB_STATES, SETTINGS, BILLING_EVENTS, BILLING_PENDING_UPDATES, SCANS, ISSUES
    with STATE_LOCK:
        if STATE_LOADED:
            return

        state = db.load_state()
        USERS = persisted_state_dict(state, "users")
        SESSIONS = persisted_state_dict(state, "sessions")
        GITHUB_STATES = persisted_state_dict(state, "githubStates")
        SETTINGS = persisted_state_dict(state, "settings")
        BILLING_EVENTS = persisted_state_dict(state, "billingEvents")
        BILLING_PENDING_UPDATES = persisted_state_list(state, "billingPendingUpdates")
        SCANS = persisted_state_list(state, "scans")
        ISSUES = persisted_state_list(state, "issues")
        STATE_LOADED = True
        STATE_DIRTY = False


def mark_state_dirty() -> None:
    global STATE_DIRTY
    with STATE_LOCK:
        STATE_DIRTY = True


def persist_state(*, force: bool = False) -> None:
    global STATE_DIRTY
    with STATE_LOCK:
        if not STATE_LOADED or (not force and not STATE_DIRTY):
            return
        try:
            db.save_state(
                {
                    "users": USERS,
                    "sessions": SESSIONS,
                    "githubStates": GITHUB_STATES,
                    "settings": SETTINGS,
                    "billingEvents": BILLING_EVENTS,
                    "billingPendingUpdates": BILLING_PENDING_UPDATES,
                    "scans": SCANS,
                    "issues": ISSUES,
                }
            )
        except Exception:
            logger.exception("Failed to persist app state.")
            return
        STATE_DIRTY = False


def cleanup_server_resources_if_due(*, force: bool = False) -> dict[str, int]:
    global LAST_RESOURCE_CLEANUP_AT
    current = time.monotonic()
    interval = max(60, env_int("PULLWISE_SERVER_CLEANUP_INTERVAL_SECONDS", 3600))
    if not force and current - LAST_RESOURCE_CLEANUP_AT < interval:
        return {}
    LAST_RESOURCE_CLEANUP_AT = current
    try:
        return cleanup_server_resources()
    except Exception:
        logger.exception("Failed to clean up server resources.")
        return {}


def cleanup_server_resources(*, timestamp: int | None = None) -> dict[str, int]:
    current_time = int(timestamp if timestamp is not None else now())
    state_removed = cleanup_expired_state_records(current_time)
    database_removed = db.cleanup_operational_records(
        timestamp=current_time,
        worker_command_retention_seconds=max(
            0,
            env_int("PULLWISE_WORKER_COMMAND_RETENTION_SECONDS", 30 * 24 * 60 * 60),
        ),
        worker_audit_retention_seconds=max(
            0,
            env_int("PULLWISE_WORKER_AUDIT_RETENTION_SECONDS", 90 * 24 * 60 * 60),
        ),
        scan_job_retention_seconds=max(
            0,
            env_int("PULLWISE_SCAN_JOB_RETENTION_SECONDS", 30 * 24 * 60 * 60),
        ),
        removable_scan_ids=terminal_scan_ids_with_retained_results(),
    )
    return {**state_removed, **database_removed}


def terminal_scan_ids_with_retained_results() -> set[str]:
    with STATE_LOCK:
        return {
            public_issue_text(scan.get("id"))
            for scan in SCANS
            if isinstance(scan, dict)
            and public_issue_text(scan.get("id"))
            and public_scan_status(scan.get("status")) in {"done", "failed", "cancelled"}
        }


def cleanup_expired_state_records(timestamp: int) -> dict[str, int]:
    removed_sessions = 0
    removed_github_states = 0
    removed_pending_github_authorizations = 0
    with STATE_LOCK:
        for session_id, session in list(SESSIONS.items()):
            expires_at = pull_request_timestamp(session.get("expiresAt")) if isinstance(session, dict) else None
            if expires_at is not None and expires_at < timestamp:
                SESSIONS.pop(session_id, None)
                removed_sessions += 1
        for state_id, record in list(GITHUB_STATES.items()):
            expires_at = pull_request_timestamp(record.get("expiresAt")) if isinstance(record, dict) else None
            if expires_at is not None and expires_at < timestamp:
                GITHUB_STATES.pop(state_id, None)
                removed_github_states += 1
        for user in USERS.values():
            if not isinstance(user, dict):
                continue
            pending = user.get("githubRepositoryAccessPending")
            expires_at = pull_request_timestamp(pending.get("expiresAt")) if isinstance(pending, dict) else None
            if expires_at is not None and expires_at < timestamp:
                user.pop("githubRepositoryAccessPending", None)
                removed_pending_github_authorizations += 1
        if removed_sessions or removed_github_states or removed_pending_github_authorizations:
            mark_state_dirty()
    return {
        "sessions": removed_sessions,
        "github_states": removed_github_states,
        "pending_github_authorizations": removed_pending_github_authorizations,
    }


def scan_status_from_recovered_job(job: dict) -> str:
    status = public_issue_text(job.get("status")).lower()
    if status in {"claimed", "running", "uploading_result"}:
        return "running"
    return public_scan_status(status)


def scan_from_recovered_job(job: dict) -> dict | None:
    scan_id = public_issue_text(job.get("scan_id"))
    user_id = public_issue_text(job.get("user_id"))
    repo = clean_repository_full_name(job.get("repo"))
    if not scan_id or not user_id or not repo or user_id not in USERS:
        return None
    created_at = pull_request_timestamp(job.get("created_at")) or now()
    completed_at = pull_request_timestamp(job.get("completed_at"))
    repo_id = public_issue_text(job.get("repo_id"))
    repository = db.get_repository(repo_id) if repo_id else None
    scan = {
        "id": scan_id,
        "repo": repo,
        "branch": clean_github_access_text(job.get("branch")) or "main",
        "commit": clean_github_access_text(job.get("commit")) or "pending",
        "status": scan_status_from_recovered_job(job),
        "userId": user_id,
        "createdAt": created_at,
        "queuedAt": created_at,
        "progress": public_scan_progress(job.get("progress")),
        "phase": public_scan_phase(job.get("progress_phase")) or None,
        "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        "jobId": public_issue_text(job.get("job_id")),
        "repoId": repo_id,
        "githubRepoId": public_issue_text(job.get("github_repo_id")),
        "installationId": clean_github_access_text(job.get("installation_id"), allow_int=True),
        "cloneUrl": trusted_github_web_url(job.get("clone_url")) or (repository or {}).get("clone_url"),
        "repositoryPrivate": bool((repository or {}).get("private")),
        "repoPath": None,
        "by": "you",
        "recoveredAt": now(),
        "recoveryReason": "orphan_scan_job",
    }
    if completed_at is not None:
        scan["completedAt"] = completed_at
    error = clean_scan_error(job.get("error"))
    if error:
        scan["error"] = error
    return scan


def reconstruct_orphan_scan_jobs_locked() -> int:
    existing_scan_ids = {public_issue_text(scan.get("id")) for scan in SCANS if public_issue_text(scan.get("id"))}
    reconstructed = 0
    for job in db.list_scan_jobs_missing_from_state(existing_scan_ids):
        scan_id = public_issue_text(job.get("scan_id"))
        if not scan_id or scan_id in existing_scan_ids:
            continue
        scan = scan_from_recovered_job(job)
        if not scan:
            continue
        SCANS.insert(0, scan)
        existing_scan_ids.add(scan_id)
        reconstructed += 1
    if reconstructed:
        mark_state_dirty()
    return reconstructed


def rollback_orphan_scan_quota_locked() -> int:
    existing_scan_ids = {public_issue_text(scan.get("id")) for scan in SCANS if public_issue_text(scan.get("id"))}
    rolled_back = 0
    for row in db.list_orphan_scan_quota_consumptions(existing_scan_ids):
        scan_id = public_issue_text(row.get("scan_id"))
        requested_by_user_id = public_issue_text(row.get("requested_by_user_id"))
        if not scan_id or not requested_by_user_id:
            continue
        result = quota.rollback_scan_quota(
            scan_id=scan_id,
            requested_by_user_id=requested_by_user_id,
            request_id=public_issue_text(row.get("request_id")) or None,
        )
        if result.get("ledgerRows"):
            rolled_back += 1
    return rolled_back


def recover_interrupted_scans() -> int:
    recovered = 0
    recovered_jobs = db.recover_expired_scan_jobs(now())
    with STATE_LOCK:
        recovered += reconstruct_orphan_scan_jobs_locked()
        recovered += rollback_orphan_scan_quota_locked()
        recovered += reconcile_completed_scan_job_results_locked()
        recovered += apply_recovered_scan_jobs_locked(recovered_jobs)
        for scan in SCANS:
            if scan.get("status") != "running":
                continue
            job_id = public_issue_text(scan.get("jobId"))
            if job_id:
                job = db.get_scan_job(job_id)
                if job and public_issue_text(job.get("status")) in {"done", "failed", "cancelled"}:
                    if reconcile_terminal_scan_job_locked(scan, job):
                        recovered += 1
                    continue
            db.requeue_interrupted_scan_job(str(scan.get("id") or ""), reason="server_restart", timestamp=now())
            scan["status"] = "queued"
            scan["progress"] = 0
            scan["phase"] = None
            scan["recoveredAt"] = now()
            scan["recoveryReason"] = "server_restart"
            recovered += 1
        if recovered:
            mark_state_dirty()
            persist_state()
    return recovered


def reconcile_completed_scan_job_results_locked() -> int:
    reconciled = 0
    for row in db.list_completed_scan_job_results():
        payload = row.get("result_payload") if isinstance(row.get("result_payload"), dict) else {}
        status = public_issue_text(row.get("result_status") or row.get("status")).lower()
        if status not in {"done", "failed"}:
            continue
        checksum = clean_github_access_text(row.get("result_result_checksum") or row.get("result_checksum"))
        if apply_worker_job_result_to_state_locked(row, payload, status=status, checksum=checksum):
            reconciled += 1
    return reconciled


def reconcile_terminal_scan_job_locked(scan: dict, job: dict) -> bool:
    status = public_issue_text(job.get("status")).lower()
    if status not in {"done", "failed", "cancelled"}:
        return False
    before = json.dumps(db.to_jsonable(scan), sort_keys=True)
    completed_at = pull_request_timestamp(job.get("completed_at")) or now()
    update = {
        "status": status,
        "completedAt": completed_at,
        "error": clean_scan_error(job.get("error")),
        "resultChecksum": public_issue_text(job.get("result_checksum")),
    }
    if status == "done":
        update["phase"] = "report"
        update["progress"] = 100
        update["error"] = ""
    elif status == "failed":
        update["phase"] = "report"
    else:
        update["phase"] = None
    scan.update(update)
    changed = before != json.dumps(db.to_jsonable(scan), sort_keys=True)
    if changed:
        mark_state_dirty()
    return changed


def apply_recovered_scan_jobs_locked(recovered_jobs: list[dict]) -> int:
    recovered = 0
    timestamp = now()
    for job in recovered_jobs:
        scan_id = public_issue_text(job.get("scan_id"))
        if not scan_id:
            continue
        scan = next((item for item in SCANS if item.get("id") == scan_id), None)
        if not scan:
            continue
        if job.get("status") == "queued":
            scan.update(
                {
                    "status": "queued",
                    "progress": 0,
                    "phase": None,
                    "claimedAt": None,
                    "claimedByWorkerId": None,
                    "recoveredAt": timestamp,
                    "recoveryReason": public_issue_text(job.get("reason")) or "timed_out",
                }
            )
        elif job.get("status") == "failed":
            scan.update(
                {
                    "status": "failed",
                    "completedAt": timestamp,
                    "error": "Scan worker timed out before completing the job.",
                    "recoveredAt": timestamp,
                    "recoveryReason": public_issue_text(job.get("reason")) or "timed_out",
                }
            )
        else:
            continue
        recovered += 1
    if recovered:
        mark_state_dirty()
    return recovered


