from __future__ import annotations

# Loaded by app.py; keep definitions in that module's globals for compatibility.


import argparse
import gzip
import hashlib
import io
import json
import logging
import math
import mimetypes
import os
import re
import secrets
import signal
import subprocess
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

from . import billing, checkout, db, fix_workflow, github_auth, logging_config, quota, review, scan_logging, system_config, system_metrics
from ._app_imports import sync_compat_globals as _sync_compat_globals

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


def admin_server_restart_mode() -> str:
    mode = os.environ.get("PULLWISE_ADMIN_RESTART_MODE", "").strip().lower()
    if mode in {"launcher", "self"}:
        return mode
    if os.environ.get("INVOCATION_ID", "").strip() or os.environ.get("JOURNAL_STREAM", "").strip():
        return "self"
    return "launcher"


def schedule_server_process_exit(delay_seconds: float = 0.5) -> threading.Timer:
    def terminate_current_process() -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    timer = threading.Timer(delay_seconds, terminate_current_process)
    timer.daemon = True
    timer.start()
    return timer


def start_server_restart_process() -> dict:
    if admin_server_restart_mode() == "self":
        schedule_server_process_exit()
        return {
            "ok": True,
            "message": "Pullwise server restart started.",
            "command": "self SIGTERM for systemd restart",
            "cwd": project_root(),
            "pid": os.getpid(),
            "startedAt": now(),
        }

    workdir = project_root()
    launcher = os.path.join(workdir, "launcher.sh")
    if not os.path.isfile(launcher):
        raise FileNotFoundError("launcher.sh not found.")

    popen_kwargs = {
        "cwd": workdir,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(["bash", "launcher.sh", "restart"], **popen_kwargs)
    return {
        "ok": True,
        "message": "Pullwise server restart started.",
        "command": "bash launcher.sh restart",
        "cwd": workdir,
        "pid": process.pid,
        "startedAt": now(),
    }


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
REVIEW_DECISION_EVENT_PROTOCOL_VERSION = "pullwise-review-decision/0.1"
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
SCAN_BY_ID: dict[str, dict] = {}
DEFAULT_WORKER_PACKAGE_VERSION = "0.8.0"
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
LEGACY_SCAN_ISSUE_IMPORT_STATE_KEY = "legacyScanIssueStateImported"


def index_memory_scan(scan: dict | None) -> None:
    if not isinstance(scan, dict):
        return
    scan_id = public_issue_text(scan.get("id"))
    if scan_id:
        SCAN_BY_ID[scan_id] = scan


def rebuild_scan_index_locked() -> None:
    SCAN_BY_ID.clear()
    for scan in SCANS:
        index_memory_scan(scan)


def memory_scan_by_id(scan_id: object) -> dict | None:
    target_scan_id = public_issue_text(scan_id)
    if not target_scan_id:
        return None
    if not SCANS:
        SCAN_BY_ID.clear()
        return None
    scan = SCAN_BY_ID.get(target_scan_id)
    if scan is not None:
        if any(item is scan for item in SCANS):
            return scan
        replacement = next((item for item in SCANS if public_issue_text(item.get("id")) == target_scan_id), None)
        if replacement is not None:
            SCAN_BY_ID[target_scan_id] = replacement
            return replacement
        SCAN_BY_ID.pop(target_scan_id, None)
        return None
    scan = next((item for item in SCANS if public_issue_text(item.get("id")) == target_scan_id), None)
    if scan is not None:
        SCAN_BY_ID[target_scan_id] = scan
    return scan


def remember_scan_snapshot_locked(scan: dict | None) -> dict | None:
    if not isinstance(scan, dict):
        return None
    scan_id = public_issue_text(scan.get("id"))
    if not scan_id:
        return scan
    existing = SCAN_BY_ID.get(scan_id)
    if existing is not None and not any(item is existing for item in SCANS):
        SCAN_BY_ID.pop(scan_id, None)
        existing = None
    if existing is None:
        existing = next((item for item in SCANS if public_issue_text(item.get("id")) == scan_id), None)
    if existing is not None and existing is not scan:
        existing.clear()
        existing.update(scan)
        scan = existing
    elif existing is None:
        SCANS.insert(0, scan)
    index_memory_scan(scan)
    return scan


def forget_memory_scans_locked(scan_ids: list[str] | set[str] | tuple[str, ...]) -> int:
    targets = {public_issue_text(scan_id) for scan_id in scan_ids if public_issue_text(scan_id)}
    if not targets:
        return 0
    before = len(SCANS)
    SCANS[:] = [
        scan
        for scan in SCANS
        if not (isinstance(scan, dict) and public_issue_text(scan.get("id")) in targets)
    ]
    for scan_id in targets:
        SCAN_BY_ID.pop(scan_id, None)
    return before - len(SCANS)


def forget_memory_scan_locked(scan_id: object) -> int:
    target = public_issue_text(scan_id)
    return forget_memory_scans_locked([target]) if target else 0


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


def max_decompressed_body_bytes() -> int:
    return max(max_body_bytes(), env_int("PULLWISE_MAX_DECOMPRESSED_BODY_BYTES", 50 * 1024 * 1024))


def decompress_gzip_body(raw_bytes: bytes, *, max_bytes: int) -> bytes:
    limit = max(0, int(max_bytes or 0))
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw_bytes)) as gzip_file:
            decompressed = gzip_file.read(limit + 1)
    except (OSError, EOFError):
        raise ValueError("Request body must be valid gzip-compressed JSON.") from None
    if len(decompressed) > limit:
        raise RequestBodyTooLarge("Request body is too large after decompression.")
    return decompressed


def decode_json_body(raw_bytes: bytes, content_encoding: str = "") -> dict:
    if not raw_bytes:
        return {}
    encoding = str(content_encoding or "").strip().lower()
    if encoding == "gzip":
        raw_bytes = decompress_gzip_body(raw_bytes, max_bytes=max_decompressed_body_bytes())
    elif encoding and encoding not in {"identity"}:
        raise ValueError("Unsupported Content-Encoding.")
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
    return system_config.rate_limit_enabled()


def rate_limit_requests() -> int:
    return system_config.rate_limit_requests()


def rate_limit_window_seconds() -> int:
    return system_config.rate_limit_window_seconds()


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


def worker_token_record(
    handler: BaseHTTPRequestHandler,
    *,
    allow_disabled: bool = False,
    include_deleted: bool = False,
) -> dict | None:
    token = bearer_token(handler)
    if not token:
        return None
    if allow_disabled:
        return db.get_worker_by_token(token, allow_disabled=True, include_deleted=include_deleted)
    return db.get_enabled_worker_token(token)


def admin_user_ids() -> set[str]:
    return {item.strip() for item in env("PULLWISE_ADMIN_USER_IDS", "").split(",") if item.strip()}


def normalized_admin_email(value: object) -> str:
    email = github_auth.clean_account_email_address(value)
    return email.lower() if email else ""


def admin_emails() -> set[str]:
    return {email for item in env("PULLWISE_ADMIN_EMAILS", "").split(",") if (email := normalized_admin_email(item))}


def user_admin_email_candidates(user: dict) -> set[str]:
    candidates = {email for email in [normalized_admin_email(user.get("email"))] if email}
    github_emails = user.get("githubVerifiedEmails")
    if isinstance(github_emails, list):
        candidates.update(email for item in github_emails if (email := normalized_admin_email(item)))
    return candidates


def user_is_admin(user: dict | None) -> bool:
    if not user:
        return False
    user_id = str(user.get("id") or "")
    github_id = str(user.get("githubId") or "")
    allowed_user_ids = admin_user_ids()
    allowed_emails = admin_emails()
    return (
        user_id in allowed_user_ids
        or github_id in allowed_user_ids
        or bool(user_admin_email_candidates(user) & allowed_emails)
    )


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
    global STATE_DIRTY, STATE_LOADED, USERS, SESSIONS, GITHUB_STATES, SETTINGS, BILLING_EVENTS, BILLING_PENDING_UPDATES, SCANS, ISSUES, SCAN_BY_ID
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
        legacy_scans = persisted_state_list(state, "scans")
        legacy_issues = persisted_state_list(state, "issues")
        legacy_marker = state.get(LEGACY_SCAN_ISSUE_IMPORT_STATE_KEY)
        legacy_imported = isinstance(legacy_marker, dict) and legacy_marker.get("imported") is True
        has_legacy_state = bool(legacy_scans or legacy_issues)
        if has_legacy_state:
            normalized_scans_exist = db.count_scan_snapshots() > 0 or db.count_scan_jobs() > 0
            normalized_issues_exist = db.count_issue_snapshots() > 0
            import_legacy_scans = bool(legacy_scans) and not legacy_imported and not normalized_scans_exist
            import_legacy_issues = bool(legacy_issues) and not legacy_imported and not normalized_issues_exist
            if import_legacy_scans:
                for scan in legacy_scans:
                    db.upsert_scan(scan)
            if import_legacy_issues:
                for issue in legacy_issues:
                    db.upsert_issue(issue)
            db.save_state_item(
                LEGACY_SCAN_ISSUE_IMPORT_STATE_KEY,
                {
                    "imported": True,
                    "importedAt": now(),
                    "scansImported": len(legacy_scans) if import_legacy_scans else 0,
                    "issuesImported": len(legacy_issues) if import_legacy_issues else 0,
                    "scansSkipped": 0 if import_legacy_scans else len(legacy_scans),
                    "issuesSkipped": 0 if import_legacy_issues else len(legacy_issues),
                },
            )
            db.delete_state_items(("scans", "issues"))
        SCAN_BY_ID = {}
        SCANS = []
        for scan in db.list_scan_snapshots(limit=env_int("PULLWISE_SCAN_MEMORY_CACHE_LIMIT", 1000)):
            remember_scan_snapshot_locked(scan)
        ISSUES = db.list_issue_snapshots(limit=env_int("PULLWISE_ISSUE_MEMORY_CACHE_LIMIT", 5000))
        STATE_LOADED = True
        STATE_DIRTY = False
        _sync_compat_globals(
            globals(),
            (
                "USERS",
                "SESSIONS",
                "GITHUB_STATES",
                "SETTINGS",
                "BILLING_EVENTS",
                "BILLING_PENDING_UPDATES",
                "SCANS",
                "ISSUES",
                "SCAN_BY_ID",
                "STATE_LOADED",
                "STATE_DIRTY",
            ),
        )


def mark_state_dirty() -> None:
    global STATE_DIRTY
    with STATE_LOCK:
        STATE_DIRTY = True
        _sync_compat_globals(globals(), ("STATE_DIRTY",))


def persist_state(*, force: bool = False) -> None:
    global STATE_DIRTY, SETTINGS
    with STATE_LOCK:
        if not STATE_LOADED or (not force and not STATE_DIRTY):
            return
        try:
            persisted_settings = db.load_state_item("settings")
            if isinstance(persisted_settings, dict):
                SETTINGS = persisted_settings
            db.save_state(
                {
                    "users": USERS,
                    "sessions": SESSIONS,
                    "githubStates": GITHUB_STATES,
                    "settings": SETTINGS,
                    "billingEvents": BILLING_EVENTS,
                    "billingPendingUpdates": BILLING_PENDING_UPDATES,
                }
            )
        except Exception:
            logger.exception("Failed to persist app state.")
            return
        STATE_DIRTY = False
        _sync_compat_globals(globals(), ("SETTINGS", "STATE_DIRTY"))


def cleanup_server_resources_if_due(*, force: bool = False) -> dict[str, int]:
    global LAST_RESOURCE_CLEANUP_AT
    current = time.monotonic()
    interval = max(60, env_int("PULLWISE_SERVER_CLEANUP_INTERVAL_SECONDS", 3600))
    if not force and current - LAST_RESOURCE_CLEANUP_AT < interval:
        return {}
    LAST_RESOURCE_CLEANUP_AT = current
    _sync_compat_globals(globals(), ("LAST_RESOURCE_CLEANUP_AT",))
    try:
        return cleanup_server_resources()
    except Exception:
        logger.exception("Failed to clean up server resources.")
        return {}


def cleanup_server_resources(*, timestamp: int | None = None) -> dict[str, int]:
    current_time = int(timestamp if timestamp is not None else now())
    state_removed = cleanup_expired_state_records(current_time)
    log_stream_removed = 0
    log_stream_cleanup = globals().get("log_stream_cleanup_expired")
    if callable(log_stream_cleanup):
        log_stream_removed = int(log_stream_cleanup(current_time) or 0)
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
    return {**state_removed, "log_stream_sessions": log_stream_removed, **database_removed}


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
    progress_message = public_issue_text(job.get("progress_message"))
    if progress_message:
        scan["progressMessage"] = progress_message
    logs_summary = public_issue_text(job.get("logs_summary"))
    if logs_summary:
        scan["logsSummary"] = logs_summary
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
        remember_scan_snapshot_locked(scan)
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
        if public_issue_text(row.get("reason")) == "scan_reserved":
            result = quota.release_scan_quota_reservation(
                scan_id=scan_id,
                requested_by_user_id=requested_by_user_id,
                request_id=public_issue_text(row.get("request_id")) or None,
                record_ledger=False,
            )
        else:
            result = quota.rollback_scan_quota(
                scan_id=scan_id,
                requested_by_user_id=requested_by_user_id,
                request_id=public_issue_text(row.get("request_id")) or None,
            )
        if result.get("ledgerRows") or result.get("bucketRows"):
            rolled_back += 1
    return rolled_back


def scan_job_has_active_restart_lease(job: dict | None, timestamp: int) -> bool:
    if not isinstance(job, dict):
        return False
    status = public_issue_text(job.get("status")).lower()
    if status not in {"claimed", "running", "uploading_result"}:
        return False
    timeout_at = pull_request_timestamp(job.get("timeout_at")) or 0
    return timeout_at > int(timestamp)


def recover_interrupted_scans() -> int:
    recovered = 0
    timestamp = now()
    recovered_jobs = db.recover_expired_scan_jobs(
        timestamp,
        worker_heartbeat_timeout_seconds=system_config.worker_heartbeat_timeout_seconds(),
    )
    with STATE_LOCK:
        rebuild_scan_index_locked()
        recovered += reconstruct_orphan_scan_jobs_locked()
        recovered += rollback_orphan_scan_quota_locked()
        recovered += reconcile_completed_scan_job_results_locked()
        recovered += apply_recovered_scan_jobs_locked(recovered_jobs)
        for scan in SCANS:
            if reconcile_terminal_reserved_scan_quota_locked(scan):
                recovered += 1
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
                if scan_job_has_active_restart_lease(job, timestamp):
                    if reconcile_scan_job_state_locked(scan):
                        recovered += 1
                    continue
            db.requeue_interrupted_scan_job(str(scan.get("id") or ""), reason="server_restart", timestamp=timestamp)
            scan["status"] = "queued"
            scan["progress"] = 0
            scan["phase"] = None
            scan["recoveredAt"] = timestamp
            scan["recoveryReason"] = "server_restart"
            recovered += 1
        if recovered:
            mark_state_dirty()
            persist_state()
    return recovered


def graph_verified_report_required_error(exc: Exception) -> bool:
    return "GraphVerified worker result must include graphVerifiedReport" in str(exc)


def reject_non_graph_verified_completed_result_locked(row: dict, *, checksum: str) -> bool:
    scan_id = public_issue_text(row.get("scan_id"))
    scan = next((item for item in SCANS if public_issue_text(item.get("id")) == scan_id), None)
    if not scan:
        return False
    before = json.dumps(db.to_jsonable(scan), sort_keys=True)
    completed_at = pull_request_timestamp(row.get("completed_at")) or now()
    job_id = public_issue_text(row.get("job_id"))
    message = "Worker result is missing GraphVerified report; legacy result was rejected."
    scan.update(
        {
            "status": "failed",
            "phase": "report",
            "progress": public_scan_progress(scan.get("progress")),
            "completedAt": completed_at,
            "error": message,
            "errorCode": "GRAPH_VERIFIED_REPORT_MISSING",
            "resultChecksum": public_issue_text(checksum),
            "graphVerifiedReport": public_graph_verified_report(
                {
                    "version": "graph-verified-code-review/1",
                    "runId": f"rejected-{job_id or scan_id}",
                    "mode": "standard",
                    "confirmedCount": 0,
                    "rejectedCount": 0,
                    "blockedCount": 1,
                    "finalJson": {"confirmed": []},
                },
                include_markdown=True,
                include_debug=True,
            ),
        }
    )
    for key in (
        "auditSwarm",
        "completionAudit",
        "convergenceState",
        "impactGraph",
        "repositoryGraph",
        "semanticGraph",
        "verificationAudit",
    ):
        scan.pop(key, None)
    changed = before != json.dumps(db.to_jsonable(scan), sort_keys=True)
    if changed:
        db.upsert_scan(scan)
        mark_state_dirty()
    return changed


def reconcile_completed_scan_job_results_locked() -> int:
    reconciled = 0
    cursor = db.load_state_item("completedScanResultReconcileCursor")
    if not isinstance(cursor, dict):
        cursor = {}
    cursor_created_at = pull_request_timestamp(cursor.get("createdAt")) or 0
    cursor_job_id = public_issue_text(cursor.get("jobId"))
    last_cursor: dict | None = None
    rows = db.list_completed_scan_job_results(
        after_created_at=cursor_created_at,
        after_job_id=cursor_job_id,
        limit=500,
    )
    for row in rows:
        payload = row.get("result_payload") if isinstance(row.get("result_payload"), dict) else {}
        status = public_issue_text(row.get("result_status") or row.get("status")).lower()
        if status not in {"done", "failed"}:
            continue
        checksum = clean_github_access_text(row.get("result_result_checksum") or row.get("result_checksum"))
        try:
            changed = apply_worker_job_result_to_state_locked(row, payload, status=status, checksum=checksum)
        except ValueError as exc:
            if not graph_verified_report_required_error(exc):
                raise
            changed = reject_non_graph_verified_completed_result_locked(row, checksum=checksum)
        if changed:
            reconciled += 1
        rollback_scan_quota_for_refundable_worker_failure(row, payload, status=status)
        last_cursor = {
            "createdAt": pull_request_timestamp(row.get("result_created_at")) or cursor_created_at,
            "jobId": public_issue_text(row.get("job_id")) or cursor_job_id,
        }
    if last_cursor:
        db.save_state_item("completedScanResultReconcileCursor", last_cursor)
    return reconciled


def reconcile_terminal_scan_quota_locked(scan: dict, job: dict, *, status: str, reason: str = "") -> None:
    normalized_status = public_issue_text(status).lower()
    if normalized_status == "done" or (
        normalized_status == "failed" and public_scan_phase(job.get("progress_phase")) in {"ai", "report"}
    ):
        trigger = public_issue_text(reason) or f"terminal_{normalized_status}"
        finalize_scan_quota_for_job(job, trigger=trigger)
        return
    if normalized_status in {"failed", "cancelled"}:
        release_scan_quota_reservation_for_scan(scan, reason=public_issue_text(reason) or f"scan_{normalized_status}")


def terminal_scan_quota_job(scan: dict, job: dict | None = None) -> dict:
    if isinstance(job, dict) and job:
        return job
    return {
        "scan_id": public_issue_text(scan.get("id")),
        "user_id": public_issue_text(scan.get("userId")),
        "repo_id": public_issue_text(scan.get("repoId")),
        "progress_phase": public_scan_phase(scan.get("phase")),
    }


def reconcile_terminal_reserved_scan_quota_locked(scan: dict) -> bool:
    if public_issue_text(scan.get("quotaState")) != "reserved":
        return False
    status = public_issue_text(scan.get("status")).lower()
    if status not in {"done", "failed", "cancelled"}:
        return False
    scan_id = public_issue_text(scan.get("id"))
    job_id = public_issue_text(scan.get("jobId"))
    job = db.get_scan_job(job_id) if job_id else None
    if not job and scan_id:
        job = db.get_scan_job_for_scan(scan_id)
    before = json.dumps(db.to_jsonable(scan), sort_keys=True)
    reason = public_issue_text(scan.get("recoveryReason")) or clean_scan_error(scan.get("error")) or status
    reconcile_terminal_scan_quota_locked(scan, terminal_scan_quota_job(scan, job), status=status, reason=reason)
    changed = before != json.dumps(db.to_jsonable(scan), sort_keys=True)
    if changed:
        db.upsert_scan(scan)
        mark_state_dirty()
    return changed


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
    reconcile_terminal_scan_quota_locked(scan, job, status=status, reason=clean_scan_error(job.get("error")) or status)
    changed = before != json.dumps(db.to_jsonable(scan), sort_keys=True)
    if changed:
        db.upsert_scan(scan)
        mark_state_dirty()
    return changed


def scan_status_from_job_status(status: object) -> str:
    normalized = public_issue_text(status).lower()
    if normalized in {"claimed", "running", "uploading_result"}:
        return "running"
    if normalized == "retrying":
        return "queued"
    if normalized == "lost":
        return "failed"
    return normalized if normalized in {"queued", "done", "failed", "cancelled"} else ""


def scan_retry_summary_for_job(job: dict | None, *, reason: str = "") -> dict:
    if not job:
        return {}
    state = db.scan_job_retry_state(job)
    payload = {
        "attempt": public_scan_count(state.get("attempt")),
        "maxAttempts": max(1, public_scan_count(state.get("maxAttempts"))),
        "retryAttempts": public_scan_count(state.get("retryAttempts")),
        "remainingAttempts": public_scan_count(state.get("remainingAttempts")),
        "attemptedWorkers": public_scan_count(state.get("attemptedWorkers")),
    }
    retry_reason = public_issue_text(reason)
    if retry_reason:
        payload["reason"] = retry_reason
    return payload


def reconcile_scan_job_state_locked(
    scan: dict,
    *,
    job_lookup: dict[tuple[str, str], dict] | None = None,
    result_lookup: dict[str, dict] | None = None,
) -> bool:
    scan_id = public_issue_text(scan.get("id"))
    if not scan_id:
        return False
    job_id = public_issue_text(scan.get("jobId"))
    job = job_lookup.get(("job", job_id)) if job_lookup is not None and job_id else None
    if not job and job_lookup is None:
        job = db.get_scan_job(job_id) if job_id else None
    if not job:
        job = job_lookup.get(("scan", scan_id)) if job_lookup is not None else None
    if not job and job_lookup is None:
        job = db.get_scan_job_for_scan(scan_id)
    if not job:
        return False

    status = scan_status_from_job_status(job.get("status"))
    if not status:
        return False
    if status in {"done", "failed"}:
        job_id = public_issue_text(job.get("job_id"))
        result = result_lookup.get(job_id) if result_lookup is not None else None
        if result is None:
            result = db.get_completed_scan_job_result(job_id)
        if result:
            payload = result.get("result_payload") if isinstance(result.get("result_payload"), dict) else {}
            result_status = public_issue_text(result.get("result_status") or result.get("status")).lower()
            checksum = clean_github_access_text(result.get("result_result_checksum") or result.get("result_checksum"))
            if result_status in {"done", "failed"}:
                try:
                    changed = apply_worker_job_result_to_state_locked(
                        result,
                        payload,
                        status=result_status,
                        checksum=checksum,
                    )
                except ValueError as exc:
                    if not graph_verified_report_required_error(exc):
                        raise
                    changed = reject_non_graph_verified_completed_result_locked(result, checksum=checksum)
                rollback_scan_quota_for_refundable_worker_failure(result, payload, status=result_status)
                if changed:
                    return True
        return reconcile_terminal_scan_job_locked(scan, job)
    if status == "cancelled":
        return reconcile_terminal_scan_job_locked(scan, job)

    before = json.dumps(db.to_jsonable(scan), sort_keys=True)
    update = {
        "jobId": public_issue_text(job.get("job_id")) or scan.get("jobId"),
        "status": status,
        "progress": 0 if status == "queued" else public_scan_progress(job.get("progress")),
        "phase": None if status == "queued" else public_scan_phase(job.get("progress_phase")) or None,
        "error": clean_scan_error(job.get("error")),
        "retry": scan_retry_summary_for_job(job),
    }
    progress_message = "" if status == "queued" else public_issue_text(job.get("progress_message"))
    if progress_message:
        update["progressMessage"] = progress_message
    else:
        scan.pop("progressMessage", None)
        scan.pop("progress_message", None)
    logs_summary = "" if status == "queued" else public_issue_text(job.get("logs_summary"))
    if logs_summary:
        update["logsSummary"] = logs_summary
    else:
        scan.pop("logsSummary", None)
        scan.pop("logs_summary", None)
    commit = clean_github_access_text(job.get("commit"))
    if commit:
        update["commit"] = commit
    if status == "running":
        update["claimedByWorkerId"] = public_issue_text(job.get("claimed_by_worker_id"))
        claimed_at = pull_request_timestamp(job.get("claimed_at"))
        if claimed_at is not None:
            update["claimedAt"] = claimed_at
    else:
        update["claimedByWorkerId"] = None
        update["claimedAt"] = None
    scan.update(update)
    changed = before != json.dumps(db.to_jsonable(scan), sort_keys=True)
    if changed:
        db.upsert_scan(scan)
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
            stored_job = db.get_scan_job(public_issue_text(job.get("job_id"))) if public_issue_text(job.get("job_id")) else None
            scan.update(
                {
                    "status": "queued",
                    "progress": 0,
                    "phase": None,
                    "claimedAt": None,
                    "claimedByWorkerId": None,
                    "recoveredAt": timestamp,
                    "recoveryReason": public_issue_text(job.get("reason")) or "timed_out",
                    "retry": scan_retry_summary_for_job(stored_job or job, reason=public_issue_text(job.get("reason"))),
                }
            )
        elif job.get("status") == "failed":
            reason = public_issue_text(job.get("reason")) or "timed_out"
            stored_job = db.get_scan_job(public_issue_text(job.get("job_id"))) if public_issue_text(job.get("job_id")) else None
            error = (
                "Scan exceeded the configured retry attempts before completing."
                if reason == "retry_attempts_exhausted"
                else "Scan worker timed out before completing the job."
            )
            scan.update(
                {
                    "status": "failed",
                    "completedAt": timestamp,
                    "error": error,
                    "recoveredAt": timestamp,
                    "recoveryReason": reason,
                    "retry": scan_retry_summary_for_job(stored_job or job, reason=reason),
                }
            )
            reconcile_terminal_scan_quota_locked(scan, stored_job or job, status="failed", reason=reason)
        else:
            continue
        db.upsert_scan(scan)
        recovered += 1
    if recovered:
        mark_state_dirty()
    return recovered
