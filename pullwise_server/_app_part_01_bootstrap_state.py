from __future__ import annotations

# Loaded by app.py; keep definitions in that module's globals for compatibility.


import argparse
import gzip
import hashlib
import ipaddress
import io
import json
import logging
import math
import mimetypes
import os
import re
import secrets
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

from . import billing, db, deployment_status, github_auth, logging_config, system_config
from .billing_account_rules import MAX_BILLING_EVENT_RECORDS, MAX_BILLING_PENDING_UPDATES
from ._app_imports import sync_compat_globals as _sync_compat_globals

logger = logging.getLogger(__name__)
access_logger = logging.getLogger("pullwise_server.access")


def public_issue_text(value: object) -> str:
    """Normalize a short public identifier used by account and repo endpoints."""
    if not isinstance(value, str) or any(char in value for char in "\r\n\x00"):
        return ""
    return value.strip()

def project_root() -> str:
    return os.path.dirname(os.path.dirname(__file__))


SERVER_STARTED_AT = int(time.time())
SERVER_GIT_REVISION = deployment_status.current_git_revision(project_root())


def server_deployment_payload() -> dict[str, object]:
    status_file = env(
        "PULLWISE_GIT_WATCH_STATUS_FILE",
        os.path.join(project_root(), ".pullwise", "git-watch.status.json"),
    )
    return deployment_status.deployment_payload(
        status_file=status_file,
        running_revision=SERVER_GIT_REVISION,
        server_started_at=SERVER_STARTED_AT,
    )


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
REVIEW_DECISION_EVENT_PROTOCOL_VERSION = "pullwise-review-decision/0.1"
SCAN_STATUSES = {"queued", "running", "cancel_requested", "cancelling", "done", "failed", "cancelled", "partial_completed"}
SCAN_JOB_STATUSES = {"queued", "claimed", "running", "uploading_result", "cancel_requested", "cancelling", "done", "failed", "cancelled", "partial_completed", "lost"}
DEFAULT_SCAN_ISSUE_RETENTION_SECONDS = 90 * 24 * 60 * 60
TERMINAL_SCAN_RETENTION_STATUSES = {"done", "failed", "cancelled", "partial_completed", "lost"}
BILLING_PUBLIC_STATUSES = {"none", "active", "trialing", "canceling", "past_due", "unpaid", "paused", "canceled"}
API_KEY_PREFIX = "pwk_"
API_KEY_ALLOWED_SCOPES = {
    "profile:read",
    "repositories:read",
    "repositories:manage",
    "items:read",
    "items:write",
    "watches:read",
    "watches:write",
    "usage:read",
}
API_KEY_DEFAULT_SCOPES = [
    "profile:read",
    "repositories:read",
    "items:read",
    "watches:read",
    "usage:read",
]
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


def max_unauthenticated_decompressed_body_bytes() -> int:
    configured = env_int("PULLWISE_MAX_UNAUTHENTICATED_DECOMPRESSED_BODY_BYTES", max_body_bytes())
    return min(max_decompressed_body_bytes(), max(0, configured))


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


def decode_json_body(raw_bytes: bytes, content_encoding: str = "", *, max_decompressed_bytes: int | None = None) -> dict:
    if not raw_bytes:
        return {}
    encoding = str(content_encoding or "").strip().lower()
    if encoding == "gzip":
        limit = max_decompressed_body_bytes() if max_decompressed_bytes is None else max_decompressed_bytes
        raw_bytes = decompress_gzip_body(raw_bytes, max_bytes=limit)
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


def local_mock_loopback_host(value: str) -> bool:
    if not value:
        return False
    try:
        parsed = urlparse(value if "://" in value else f"http://{value}")
    except ValueError:
        return False
    host = (parsed.hostname or "").strip("[]").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def local_mock_origin_list_is_loopback(raw: str) -> bool:
    for item in raw.split(","):
        origin = item.strip()
        if not origin:
            continue
        if origin == "*" or not local_mock_loopback_host(origin):
            return False
    return True


def local_github_mocks_enabled(handler: BaseHTTPRequestHandler | None = None) -> bool:
    if not env_flag("PULLWISE_ENABLE_LOCAL_GITHUB_MOCKS"):
        return False
    mode = env("PULLWISE_MODE", "local").strip().lower()
    if mode not in {"", "local", "dev", "development", "test"}:
        return False
    if not local_mock_loopback_host(env("PULLWISE_APP_URL", "http://localhost:5173")):
        return False
    for name in ("PULLWISE_API_BASE_URL", "PULLWISE_ADMIN_APP_URL"):
        configured = os.environ.get(name, "").strip()
        if configured and not local_mock_loopback_host(configured):
            return False
    configured_origins = os.environ.get("PULLWISE_ALLOWED_ORIGINS", "").strip()
    if configured_origins and not local_mock_origin_list_is_loopback(configured_origins):
        return False
    if handler is not None and not local_mock_loopback_host(request_header(handler, "Host") or ""):
        return False
    return True


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
        SCAN_BY_ID = {}
        SCANS = []
        ISSUES = []
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
