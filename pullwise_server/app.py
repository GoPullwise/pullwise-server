from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import mimetypes
import os
import re
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse

from . import billing, checkout, db, fix_workflow, github_auth, logging_config, quota, review, scan_logging, worker

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
SCAN_STATUSES = {"queued", "running", "done", "failed", "cancelled"}
SCAN_JOB_STATUSES = {"queued", "claimed", "running", "uploading_result", "done", "failed", "cancelled", "lost", "retrying"}
SCAN_PHASES = {"clone", "index", "secrets", "deps", "ai", "report"}
BILLING_PUBLIC_STATUSES = {"none", "active", "trialing", "canceling", "past_due", "unpaid", "paused", "canceled"}
API_KEY_PREFIX = "pwk_"
API_KEY_ALLOWED_SCOPES = {"repositories:read", "scans:read", "scans:write", "quota:read"}
API_KEY_DEFAULT_SCOPES = ["repositories:read", "scans:read", "scans:write", "quota:read"]

USERS: dict[str, dict] = {}
SESSIONS: dict[str, dict] = {}
GITHUB_STATES: dict[str, dict] = {}
SETTINGS: dict[str, dict] = {}
BILLING_EVENTS: dict[str, dict] = {}
BILLING_PENDING_UPDATES: list[dict] = []
STATE_LOADED = False
STATE_DIRTY = False

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
WORKER_PACKAGE_RELEASE_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Re-entrant so worker mutations can call persist_state() while already holding
# the lock. Protects against worker/handler interleaving on SCANS and ISSUES.
STATE_LOCK = threading.RLock()


class PreviewScanLockEntry:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.refs = 0


PREVIEW_SCAN_LOCKS: dict[str, PreviewScanLockEntry] = {}
PREVIEW_SCAN_LOCKS_GUARD = threading.Lock()
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
    return method == "OPTIONS" or path == "/health" or path.startswith("/worker/")


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


def recover_interrupted_scans() -> int:
    recovered = 0
    recovered_jobs = db.recover_expired_scan_jobs(now())
    with STATE_LOCK:
        recovered += reconcile_completed_scan_job_results_locked()
        recovered += apply_recovered_scan_jobs_locked(recovered_jobs)
        for scan in SCANS:
            if scan.get("status") != "running":
                continue
            job_id = public_issue_text(scan.get("jobId"))
            if job_id:
                job = db.get_scan_job(job_id)
                if job and public_issue_text(job.get("status")) in {"done", "failed", "cancelled"}:
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


def readiness_payload() -> dict:
    try:
        billing_provider = billing.selected_provider()
    except (billing.BillingConfigurationError, billing.BillingProviderConflict):
        billing_provider = "error"
    return {
        "reviewProvider": review.selected_provider(),
        "github": {
            "oauthConfigured": github_auth.oauth_configured(),
            "appInstallConfigured": github_auth.app_install_configured(),
            "appApiConfigured": github_auth.app_api_configured(),
            "appVisibilityCheck": github_auth.app_visibility_check_enabled(),
        },
        "billing": {
            "provider": billing_provider,
            "enabled": billing_provider in {"stripe", "creem"},
        },
        "limits": {
            "maxConcurrentScansPerUser": max_scan_concurrency_per_user(),
            "maxQueuedScansGlobal": max_queued_scans_global(),
            "maxQueuedScansPerUser": max_queued_scans_per_user(),
            "rateLimitEnabled": rate_limit_enabled(),
        },
    }


def allowed_origins() -> set[str]:
    raw = env(
        "PULLWISE_ALLOWED_ORIGINS",
        "http://localhost:5173,http://localhost:5174,http://127.0.0.1:5173,http://127.0.0.1:5174",
    )
    return {item.strip() for item in raw.split(",") if item.strip() and item.strip() != "*"}


def api_base_url(handler: BaseHTTPRequestHandler) -> str:
    configured = os.environ.get("PULLWISE_API_BASE_URL")
    if configured:
        return configured.rstrip("/")
    if env_flag("PULLWISE_TRUST_PROXY_HEADERS"):
        forwarded = forwarded_api_base_url(handler)
        if forwarded:
            return forwarded
    host = trusted_host_header(handler)
    if host:
        return f"http://{host}"
    return "http://localhost:8080"


def trusted_host_header(handler: BaseHTTPRequestHandler) -> str | None:
    host = first_header_value(handler, "Host") or "localhost:8080"
    if any(char in host for char in "/\r\n") or not re.match(r"^[A-Za-z0-9.:-]+$", host):
        return None
    if is_local_host(host):
        return host
    explicit_hosts = {
        item.strip().lower()
        for item in env("PULLWISE_API_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    }
    if host.lower() in explicit_hosts:
        return host
    allowed = allowed_origins()
    app_origin = url_origin(env("PULLWISE_APP_URL", "http://localhost:5173"))
    if app_origin:
        allowed.add(app_origin)
    if f"http://{host}" in allowed or f"https://{host}" in allowed:
        return host
    return None


def is_local_host(host: str) -> bool:
    name = host.rsplit(":", 1)[0].lower()
    return name in {"localhost", "127.0.0.1"}


def forwarded_api_base_url(handler: BaseHTTPRequestHandler) -> str | None:
    proto = first_header_value(handler, "X-Forwarded-Proto")
    host = first_header_value(handler, "X-Forwarded-Host")
    prefix = first_header_value(handler, "X-Forwarded-Prefix") or ""

    if proto not in {"http", "https"} or not host:
        return None
    if any(char in host for char in "/\r\n") or not re.match(r"^[A-Za-z0-9.:-]+$", host):
        return None
    if prefix and (not prefix.startswith("/") or prefix.startswith("//") or any(char in prefix for char in "\r\n")):
        return None

    return f"{proto}://{host}{prefix.rstrip('/')}"


def first_header_value(handler: BaseHTTPRequestHandler, name: str) -> str | None:
    value = request_header(handler, name)
    if not value:
        return None
    return value.split(",", 1)[0].strip()


def default_redirect(screen: str) -> str:
    app_url = env("PULLWISE_APP_URL", "http://localhost:5173").rstrip("/")
    # Use path-based URLs that match the frontend's client-side routing (e.g. /dashboard, /repos).
    # The "landing" screen maps to the root path "/".
    path = "/" if screen == "landing" else f"/{screen}"
    return f"{app_url}{path}"


def now() -> int:
    return int(time.time())


def make_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(8)}"


def remember_github_state(kind: str, redirect_to: str, **extra: object) -> str:
    state = secrets.token_urlsafe(32)
    GITHUB_STATES[state] = {
        "kind": kind,
        "redirectTo": redirect_to,
        "expiresAt": now() + GITHUB_STATE_MAX_AGE,
        **extra,
    }
    mark_state_dirty()
    return state


def github_state_record(state: str, *, consume: bool, expected_kind: str | None = None) -> dict:
    record = GITHUB_STATES.pop(state, None) if consume else GITHUB_STATES.get(state)
    if consume and record is not None:
        mark_state_dirty()
    if not isinstance(record, dict):
        raise ValueError("GitHub authorization state is invalid or expired.")
    expires_at = pull_request_timestamp(record.get("expiresAt"))
    kind = record.get("kind")
    if expires_at is None or expires_at < now() or (expected_kind is not None and kind != expected_kind):
        if not consume and (expires_at is None or expires_at < now()):
            GITHUB_STATES.pop(state, None)
            mark_state_dirty()
        raise ValueError("GitHub authorization state is invalid or expired.")
    return record


def peek_github_state(kind: str, state: str) -> dict:
    return github_state_record(state, consume=False, expected_kind=kind)


def pop_any_github_state(state: str) -> dict:
    return github_state_record(state, consume=True)


def pop_github_state(kind: str, state: str) -> dict:
    return github_state_record(state, consume=True, expected_kind=kind)


def remember_github_repository_authorization(
    user: dict,
    redirect_to: str,
    requested_scope: str,
    *,
    manage: bool = False,
    selected_github_identity_id: str | None = None,
) -> str:
    state = remember_github_state(
        "install",
        redirect_to,
        userId=user["id"],
        requestedScope=requested_scope,
        selectedGithubIdentityId=selected_github_identity_id,
    )
    github_access = user.get("githubRepositoryAccess")
    if not isinstance(github_access, dict):
        github_access = {}
    timestamp = now()
    user["githubRepositoryAccessPending"] = {
        "state": state,
        "startedAt": timestamp,
        "expiresAt": timestamp + GITHUB_STATE_MAX_AGE,
        "previousInstallationId": github_access.get("installationId"),
        "manage": bool(manage),
    }
    mark_state_dirty()
    return state


def remember_github_repository_identity_authorization(
    user: dict,
    redirect_to: str,
    requested_scope: str,
    *,
    add: bool = False,
    manage: bool = False,
) -> str:
    state = remember_github_state(
        "install_identity",
        redirect_to,
        userId=user["id"],
        requestedScope=requested_scope,
        add=bool(add),
        manage=bool(manage),
    )
    github_access = user.get("githubRepositoryAccess")
    if not isinstance(github_access, dict):
        github_access = {}
    timestamp = now()
    user["githubRepositoryAccessPending"] = {
        "state": state,
        "startedAt": timestamp,
        "expiresAt": timestamp + GITHUB_STATE_MAX_AGE,
        "previousInstallationId": github_access.get("installationId"),
        "add": bool(add),
        "manage": bool(manage),
        "needsIdentitySelection": True,
    }
    mark_state_dirty()
    return state


def remember_github_installation_manage_state(
    user: dict,
    installation: dict,
    redirect_to: str,
    *,
    expected_github_identity_id: str | None = None,
) -> str:
    return remember_github_state(
        "manage_installation",
        redirect_to,
        purpose="manage_installation",
        userId=user["id"],
        expectedInstallationId=clean_installation_summary_text(installation.get("installationId")),
        expectedAccountLogin=clean_installation_summary_text(installation.get("installationAccount")),
        expectedInstallationTargetType=clean_installation_summary_text(installation.get("installationTargetType")),
        expectedInstallationHtmlUrl=trusted_github_web_url(installation.get("installationHtmlUrl")),
        expectedGithubIdentityId=expected_github_identity_id,
    )


def github_repository_authorization_pending(user: dict | None) -> dict | None:
    if not user:
        return None

    timestamp = now()
    pending = user.get("githubRepositoryAccessPending")
    if isinstance(pending, dict):
        pending_expires_at = pull_request_timestamp(pending.get("expiresAt"))
        if pending_expires_at is not None and pending_expires_at >= timestamp:
            return pending
        user.pop("githubRepositoryAccessPending", None)
        mark_state_dirty()

    return None


def clear_github_repository_authorization_pending(user: dict | None, state: str | None = None) -> None:
    if not user:
        return

    pending = user.get("githubRepositoryAccessPending")
    if isinstance(pending, dict) and (not state or pending.get("state") == state):
        user.pop("githubRepositoryAccessPending", None)
        mark_state_dirty()

    states_to_clear = []
    for stored_state, record in GITHUB_STATES.items():
        if not isinstance(record, dict):
            if not state or stored_state == state:
                states_to_clear.append(stored_state)
            continue
        if (
            record.get("kind") == "install"
            and record.get("userId") == user.get("id")
            and (not state or stored_state == state)
        ):
            states_to_clear.append(stored_state)
    for stored_state in states_to_clear:
        GITHUB_STATES.pop(stored_state, None)
    if states_to_clear:
        mark_state_dirty()


def url_origin(value: str) -> str | None:
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def trusted_github_web_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or any(char in raw for char in "\r\n"):
        return None
    parsed = urlparse(raw)
    allowed = urlparse(github_auth.github_web_url())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if allowed.netloc and parsed.netloc.lower() != allowed.netloc.lower():
        return None
    return raw


def safe_redirect_to(value: object, screen: str) -> str:
    fallback = default_redirect(screen)
    if not isinstance(value, str) or not value:
        return fallback
    if any(char in value for char in "\r\n"):
        return fallback
    if value.startswith("/") and not value.startswith("//"):
        return env("PULLWISE_APP_URL", "http://localhost:5173").rstrip("/") + value

    origin = url_origin(value)
    allowed = allowed_origins()
    app_origin = url_origin(env("PULLWISE_APP_URL", "http://localhost:5173"))
    if app_origin:
        allowed.add(app_origin)
    if origin and origin in allowed:
        return value
    return fallback


def redirect_with_params(location: str, params: dict[str, str]) -> str:
    parsed = urlparse(location)
    query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
    query.update({key: value for key, value in params.items() if value})
    return urlunparse(parsed._replace(query=urlencode(query)))


def user_public(user: dict) -> dict:
    return {
        "id": public_issue_text(user.get("id")),
        "name": public_issue_text(user.get("name")) or "User",
        "email": public_issue_text(user.get("email")),
        "avatarUrl": trusted_public_url(user.get("avatarUrl")),
        "createdAt": pull_request_timestamp(user.get("createdAt")) or 0,
        "providers": review._safe_text_list(user.get("providers")),
    }


def get_or_create_github_user() -> dict:
    login = env("PULLWISE_DEV_GITHUB_LOGIN", "taylor-dev")
    email = env("PULLWISE_DEV_EMAIL", "taylor@acme.io")
    user_id = "usr_github_" + re.sub(r"[^a-z0-9]+", "_", login.lower()).strip("_")
    if user_id not in USERS:
        USERS[user_id] = {
            "id": user_id,
            "name": login,
            "email": email,
            "avatarUrl": None,
            "createdAt": now(),
            "providers": ["github"],
            "githubLogin": login,
            "githubRepositoryAccess": None,
        }
        mark_state_dirty()
    elif "github" not in USERS[user_id]["providers"]:
        USERS[user_id]["providers"].append("github")
        mark_state_dirty()
    return USERS[user_id]


def get_or_create_real_github_user(profile: dict, token_payload: dict) -> dict:
    login = profile["login"]
    github_id = github_profile_id(profile, login)
    user_id = "usr_github_" + github_id
    profile_name = clean_user_profile_text(profile.get("name"))
    email = (
        clean_user_profile_text(profile.get("primaryEmail"))
        or clean_user_profile_text(profile.get("email"))
        or f"{login}@users.noreply.github.com"
    )
    avatar_url = trusted_public_url(profile.get("avatar_url"))
    github_html_url = trusted_github_web_url(profile.get("html_url"))
    if user_id not in USERS:
        USERS[user_id] = {
            "id": user_id,
            "name": profile_name or login,
            "email": email,
            "avatarUrl": avatar_url,
            "createdAt": now(),
            "providers": ["github"],
            "githubRepositoryAccess": None,
        }
        mark_state_dirty()

    user = USERS[user_id]
    user.update(
        {
            "name": profile_name or clean_user_profile_text(user.get("name")) or login,
            "email": email,
            "avatarUrl": avatar_url,
            "githubId": github_id,
            "githubLogin": login,
            "githubHtmlUrl": github_html_url,
            "githubAccessToken": token_payload.get("access_token"),
            "githubTokenType": token_payload.get("token_type"),
            "githubOAuthScope": token_payload.get("scope"),
            "githubAccessTokenUpdatedAt": now(),
        }
    )
    if "github" not in user["providers"]:
        user["providers"].append("github")
    upsert_github_identity(user, profile, token_payload)
    mark_state_dirty()
    return user


def github_profile_id(profile: dict, login: str) -> str:
    raw_id = profile.get("id")
    if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id >= 0:
        return str(raw_id)
    if isinstance(raw_id, str):
        candidate = raw_id.strip()
        if re.fullmatch(r"[A-Za-z0-9_-]+", candidate):
            return candidate
    return re.sub(r"[^a-z0-9]+", "_", login.lower()).strip("_")


def github_identity_record_id(github_user_id: object, login: object) -> str:
    source = str(github_user_id or login or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", source).strip("_")
    return f"ghi_{slug or secrets.token_urlsafe(6)}"


def github_identity_list(user: dict | None) -> list[dict]:
    if not user:
        return []
    identities = user.get("githubIdentities")
    if not isinstance(identities, list):
        identities = []
        user["githubIdentities"] = identities
    return identities


def upsert_github_identity(user: dict, profile: dict, token_payload: dict) -> dict:
    login = public_issue_text(profile.get("login")) or "github-user"
    github_user_id = github_profile_id(profile, login)
    identities = github_identity_list(user)
    identity = next(
        (
            item
            for item in identities
            if isinstance(item, dict) and str(item.get("githubUserId") or "") == str(github_user_id)
        ),
        None,
    )
    if identity is None:
        identity = {
            "id": github_identity_record_id(github_user_id, login),
            "userId": user.get("id"),
            "githubUserId": str(github_user_id),
        }
        identities.append(identity)

    timestamp = now()
    identity.update({
        "githubLogin": login,
        "login": login,
        "githubHtmlUrl": trusted_github_web_url(profile.get("html_url")),
        "avatarUrl": trusted_public_url(profile.get("avatar_url")),
        "accessToken": token_payload.get("access_token"),
        "oauthScope": token_payload.get("scope"),
        "tokenUpdatedAt": timestamp,
        "lastVerifiedAt": timestamp,
        "status": "active",
    })
    mark_state_dirty()
    return identity


def synthesized_current_github_identity(user: dict | None) -> dict | None:
    if not user or not user.get("githubAccessToken") or not user.get("githubLogin"):
        return None
    github_user_id = str(user.get("githubId") or user.get("githubLogin") or "")
    login = public_issue_text(user.get("githubLogin")) or "github-user"
    return {
        "id": github_identity_record_id(github_user_id, login),
        "userId": user.get("id"),
        "githubUserId": github_user_id,
        "githubLogin": login,
        "login": login,
        "githubHtmlUrl": trusted_github_web_url(user.get("githubHtmlUrl")),
        "avatarUrl": trusted_public_url(user.get("avatarUrl")),
        "accessToken": user.get("githubAccessToken"),
        "oauthScope": user.get("githubOAuthScope"),
        "tokenUpdatedAt": user.get("githubAccessTokenUpdatedAt"),
        "lastVerifiedAt": user.get("githubAccessTokenUpdatedAt") or user.get("createdAt"),
        "status": "active",
    }


def github_identities_for_user(user: dict | None) -> list[dict]:
    if not user:
        return []
    identities = [identity for identity in github_identity_list(user) if isinstance(identity, dict)]
    current_identity = synthesized_current_github_identity(user)
    if current_identity and not any(identity.get("id") == current_identity["id"] for identity in identities):
        identities = [*identities, current_identity]
    return identities


def public_github_identity(identity: dict) -> dict:
    return {
        "id": clean_github_access_text(identity.get("id")),
        "githubUserId": clean_github_access_text(identity.get("githubUserId"), allow_int=True),
        "login": clean_github_access_text(identity.get("githubLogin") or identity.get("login")),
        "githubHtmlUrl": trusted_github_web_url(identity.get("githubHtmlUrl")),
        "avatarUrl": trusted_public_url(identity.get("avatarUrl")),
        "status": clean_github_access_text(identity.get("status")) or "active",
        "lastVerifiedAt": pull_request_timestamp(identity.get("lastVerifiedAt")),
    }


def public_github_identities(user: dict | None) -> list[dict]:
    identities = []
    for identity in github_identities_for_user(user):
        public_identity = public_github_identity(identity)
        if public_identity["id"] and public_identity["login"]:
            identities.append(public_identity)
    return identities


def github_identity_by_id(user: dict | None, identity_id: str | None) -> dict | None:
    if not identity_id:
        return None
    for identity in github_identities_for_user(user):
        if identity.get("id") == identity_id:
            return identity
    return None


def github_identity_access_list(user: dict | None) -> list[dict]:
    if not user:
        return []
    records = user.get("githubIdentityInstallationAccess")
    if not isinstance(records, list):
        records = []
        user["githubIdentityInstallationAccess"] = records
    return records


def upsert_github_identity_installation_access(
    user: dict,
    identity: dict,
    installation_id: str,
    *,
    can_access: bool,
    last_error_code: str | None = None,
    verification_method: str = "user_installations_api",
) -> dict:
    records = github_identity_access_list(user)
    identity_id = clean_github_access_text(identity.get("id"))
    record = next(
        (
            item
            for item in records
            if isinstance(item, dict)
            and item.get("githubIdentityId") == identity_id
            and str(item.get("githubAppInstallationId") or "") == str(installation_id)
        ),
        None,
    )
    if record is None:
        record = {
            "githubIdentityId": identity_id,
            "githubAppInstallationId": str(installation_id),
        }
        records.append(record)
    record.update({
        "canAccess": bool(can_access),
        "canManage": "unknown" if can_access else False,
        "verifiedAt": now(),
        "verificationMethod": verification_method,
        "lastErrorCode": last_error_code,
    })
    mark_state_dirty()
    return record


def latest_installation_access_record(user: dict | None, installation_id: str | None) -> dict | None:
    if not installation_id:
        return None
    candidates = [
        record
        for record in github_identity_access_list(user)
        if isinstance(record, dict)
        and str(record.get("githubAppInstallationId") or "") == str(installation_id)
    ]
    candidates.sort(key=lambda record: pull_request_timestamp(record.get("verifiedAt")) or 0, reverse=True)
    return candidates[0] if candidates else None


def clean_user_profile_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def trusted_public_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if any(char in raw for char in "\r\n"):
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return raw


def create_session(user: dict) -> dict:
    session_id = make_id("ses")
    session = {
        "id": session_id,
        "userId": user["id"],
        "createdAt": now(),
        "expiresAt": now() + SESSION_MAX_AGE,
    }
    SESSIONS[session_id] = session
    mark_state_dirty()
    return session


def default_settings_payload(user_id: str) -> dict:
    user = USERS.get(user_id) or {}
    return {
        "profile": {
            "name": public_issue_text(user.get("name")) or "User",
            "email": public_issue_text(user.get("email")),
        },
    }


def settings_payload(user_id: str) -> dict:
    return clean_settings_payload(user_id, SETTINGS.get(user_id))


def default_settings(user_id: str) -> dict:
    if not isinstance(SETTINGS.get(user_id), dict):
        SETTINGS[user_id] = default_settings_payload(user_id)
        mark_state_dirty()
    return SETTINGS[user_id]


def clean_settings_payload(user_id: str, value: object) -> dict:
    base = default_settings_payload(user_id)
    settings = value if isinstance(value, dict) else {}
    profile = settings.get("profile") if isinstance(settings.get("profile"), dict) else {}
    return {
        "profile": {
            "name": public_issue_text(profile.get("name")) or base["profile"]["name"],
            "email": public_issue_text(profile.get("email")) or base["profile"]["email"],
        },
    }


def apply_settings_update(user_id: str, body: dict) -> dict:
    settings = settings_payload(user_id)
    profile = body.get("profile") if isinstance(body.get("profile"), dict) else {}
    name = public_issue_text(profile.get("name"))
    email = public_issue_text(profile.get("email"))
    if name:
        settings["profile"]["name"] = name
    if email:
        settings["profile"]["email"] = email
    SETTINGS[user_id] = settings
    mark_state_dirty()
    return settings


def user_scans(session: dict | None) -> list[dict]:
    if not session:
        return []
    return [
        scan
        for scan in SCANS
        if scan.get("userId") == session["userId"]
    ]


def user_scan_by_request_id(user_id: str, request_id: str) -> dict | None:
    if not request_id:
        return None
    for scan in SCANS:
        if scan.get("userId") == user_id and scan.get("requestId") == request_id:
            return scan
    return None


IDEMPOTENCY_KEY_REUSED_MESSAGE = "This idempotency key is already attached to a different repository scan."


def scan_matches_requested_repository(scan: dict, *, requested_repo_id: str | None = None, requested_repository: str | None = None) -> bool:
    if requested_repo_id:
        scan_repo_ids = {
            clean_github_access_text(scan.get("repoId"), allow_int=True),
            clean_github_access_text(scan.get("githubRepoId"), allow_int=True),
        }
        if requested_repo_id in scan_repo_ids:
            return True
    if requested_repository and clean_repository_full_name(scan.get("repo")) == requested_repository:
        return True
    return False


def idempotency_key_reused_payload(scan: dict | None) -> dict:
    payload = {"message": IDEMPOTENCY_KEY_REUSED_MESSAGE, "code": "IDEMPOTENCY_KEY_REUSED"}
    if isinstance(scan, dict):
        if repo_id := clean_github_access_text(scan.get("repoId"), allow_int=True):
            payload["repoId"] = repo_id
    return payload


def current_review_usage_period(timestamp: int | None = None) -> str:
    return time.strftime("%Y-%m", time.gmtime(timestamp or now()))


def effective_billing_plan(user: dict | None) -> str:
    if not user:
        return "free"
    current = user_billing_state(user)
    status = str(current.get("status") or "").lower()
    plan = billing.normalize_plan(current.get("plan"))
    if plan == "pro" and status in {"active", "trialing", "canceling"}:
        return "pro"
    return "free"


def user_billing_state(user: dict) -> dict:
    return user.get("billing") if isinstance(user.get("billing"), dict) else {}


def non_negative_int(value: object) -> int:
    try:
        candidate = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return max(0, candidate)


def billing_usage_for_user(user: dict, plan_id: str, *, timestamp: int | None = None, mutate: bool = False) -> dict:
    period = current_review_usage_period(timestamp)
    current = user.get("billingUsage") if isinstance(user.get("billingUsage"), dict) else {}
    used = non_negative_int(current.get("used"))
    if current.get("period") != period or current.get("plan") != plan_id:
        usage = {"period": period, "plan": plan_id, "used": 0}
    else:
        usage = {"period": period, "plan": plan_id, "used": used}
    if mutate:
        user["billingUsage"] = usage
        mark_state_dirty()
        return user["billingUsage"]
    return usage


def billing_entitlement_for_user(user: dict | None, *, timestamp: int | None = None, mutate: bool = False) -> dict:
    plan_id = effective_billing_plan(user)
    limit = billing.review_limit(plan_id)
    usage = billing_usage_for_user(user or {}, plan_id, timestamp=timestamp, mutate=mutate) if user else {"period": current_review_usage_period(timestamp), "plan": plan_id, "used": 0}
    used = non_negative_int(usage.get("used"))
    current = user_billing_state(user) if user else {}
    return {
        "plan": plan_id,
        "interval": current.get("interval") if plan_id == "pro" else "month",
        "period": usage["period"],
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
    }


def consume_review_quota(user: dict) -> tuple[bool, dict]:
    entitlement = billing_entitlement_for_user(user, mutate=True)
    if entitlement["remaining"] <= 0:
        return False, entitlement
    usage = billing_usage_for_user(user, entitlement["plan"], mutate=True)
    usage["used"] = int(usage.get("used") or 0) + 1
    mark_state_dirty()
    return True, billing_entitlement_for_user(user)


def billing_account_payload(user: dict) -> dict:
    current = user_billing_state(user)
    entitlement = billing_entitlement_for_user(user)
    return {
        "provider": public_billing_text(current.get("provider")),
        "status": public_billing_status(current.get("status")),
        "plan": entitlement["plan"],
        "interval": billing.normalize_interval(entitlement["interval"]),
        "customerId": public_billing_text(current.get("customerId")),
        "subscriptionId": public_billing_text(current.get("subscriptionId")),
        "subscriptionItemId": public_billing_text(current.get("subscriptionItemId")),
        "customerEmail": public_billing_text(current.get("customerEmail")),
        "currentPeriodStart": pull_request_timestamp(current.get("currentPeriodStart")),
        "currentPeriodEnd": pull_request_timestamp(current.get("currentPeriodEnd")),
        "cancelAtPeriodEnd": current.get("cancelAtPeriodEnd") if isinstance(current.get("cancelAtPeriodEnd"), bool) else None,
        "canceledAt": pull_request_timestamp(current.get("canceledAt")),
        "lastEventId": public_billing_text(current.get("lastEventId")),
        "lastEventType": public_billing_text(current.get("lastEventType")),
        "lastEventCreated": pull_request_timestamp(current.get("lastEventCreated")),
        "updatedAt": pull_request_timestamp(current.get("updatedAt")),
        "reviewLimit": entitlement["limit"],
        "usage": {
            "period": entitlement["period"],
            "used": entitlement["used"],
            "limit": entitlement["limit"],
            "remaining": entitlement["remaining"],
            "plan": entitlement["plan"],
        },
    }


def clean_api_key_scopes(value: object) -> list[str]:
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = [item for item in value if isinstance(item, str)]
    else:
        candidates = API_KEY_DEFAULT_SCOPES
    scopes: list[str] = []
    for scope in candidates:
        normalized = scope.strip().lower()
        if normalized in API_KEY_ALLOWED_SCOPES and normalized not in scopes:
            scopes.append(normalized)
    return scopes or list(API_KEY_DEFAULT_SCOPES)


def requested_api_key_scopes(value: object, *, provided: bool) -> tuple[list[str], str | None]:
    if not provided or value is None:
        return list(API_KEY_DEFAULT_SCOPES), None
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        candidates = value
    else:
        return [], "API key scopes must be a string or a list of strings."

    scopes: list[str] = []
    invalid: list[str] = []
    for candidate in candidates:
        normalized = candidate.strip().lower()
        if normalized in API_KEY_ALLOWED_SCOPES:
            if normalized not in scopes:
                scopes.append(normalized)
        else:
            invalid.append(candidate)
    if invalid or not scopes:
        allowed = ", ".join(sorted(API_KEY_ALLOWED_SCOPES))
        return [], f"API key scopes must include only: {allowed}."
    return scopes, None


def scan_request_id_from_body(body: dict) -> str:
    for key in ("requestId", "idempotencyKey"):
        value = clean_github_access_text(body.get(key), allow_int=True)
        if value and "\x00" not in value:
            return value[:128]
    return ""


def parse_api_key_scopes(value: object) -> list[str]:
    if isinstance(value, list):
        return clean_api_key_scopes(value)
    if not isinstance(value, str):
        return list(API_KEY_DEFAULT_SCOPES)
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return clean_api_key_scopes(decoded)


def api_key_public_payload(record: dict, *, token: str | None = None) -> dict:
    payload = {
        "id": public_issue_text(record.get("id")),
        "name": public_issue_text(record.get("name")) or "API key",
        "userId": public_issue_text(record.get("user_id")),
        "prefix": public_issue_text(record.get("key_prefix")),
        "scopes": parse_api_key_scopes(record.get("scopes")),
        "createdAt": pull_request_timestamp(record.get("created_at")) or 0,
        "lastUsedAt": pull_request_timestamp(record.get("last_used_at")),
        "revokedAt": pull_request_timestamp(record.get("revoked_at")),
    }
    if token:
        payload["key"] = token
    return payload


def navigation_payload() -> dict:
    return {
        "top": [
            {"id": "product", "label": "Product", "href": "/"},
            {"id": "pricing", "label": "Pricing", "href": "/pricing"},
            {"id": "api", "label": "API", "href": "/api-docs"},
        ],
        "dashboard": [
            {"id": "overview", "label": "Overview", "href": "/dashboard/overview"},
            {"id": "repositories", "label": "Repositories", "href": "/repositories"},
            {"id": "api-keys", "label": "API Keys", "href": "/api-keys"},
            {"id": "billing", "label": "Billing", "href": "/billing"},
        ],
    }


def pricing_payload(user: dict | None = None) -> dict:
    payload = billing.public_plan()
    payload["page"] = {
        "id": "pricing",
        "checkoutAction": {"method": "POST", "href": "/billing/checkout-sessions"},
        "billingRoute": "/billing",
    }
    if user:
        payload["account"] = billing_account_payload(user)
    return payload


def billing_page_payload(user: dict) -> dict:
    return {
        "page": {
            "id": "billing",
            "subscriptionAction": {"label": "View pricing", "href": "/pricing"},
            "checkoutAction": None,
        },
        "account": billing_account_payload(user),
    }


def api_docs_payload() -> dict:
    return {
        "page": {"id": "api", "title": "Pullwise API"},
        "baseUrl": "https://api.pull-wise.com",
        "website": "https://pull-wise.com",
        "contact": "contact@pull-wise.com",
        "authentication": {
            "type": "apiKey",
            "headers": ["Authorization: Bearer <api_key>", "X-Pullwise-Api-Key: <api_key>"],
            "createKey": {"method": "POST", "href": "/api-keys"},
            "scopes": API_KEY_DEFAULT_SCOPES,
        },
        "endpoints": [
            {
                "method": "GET",
                "path": "/api/v1/repositories",
                "scope": "repositories:read",
                "description": "List authorized repositories for the API key, including repoId.",
            },
            {
                "method": "POST",
                "path": "/api/v1/repositories/{repoId}/scans",
                "scope": "scans:write",
                "description": "Start a scan for an authorized repository.",
            },
            {
                "method": "POST",
                "path": "/api/v1/repositories/{repoId}/scans/stop",
                "scope": "scans:write",
                "description": "Cancel the latest queued or running scan for the repository.",
            },
            {
                "method": "GET",
                "path": "/api/v1/repositories/{repoId}/scans/current",
                "scope": "scans:read",
                "description": "Read the latest scan status for the repository.",
            },
            {
                "method": "GET",
                "path": "/api/v1/repositories/{repoId}/quota",
                "scope": "quota:read",
                "description": "Read remaining account and repository scan quota.",
            },
        ],
        "errors": [
            {"status": 400, "description": "Malformed JSON, invalid scope, invalid repoId, or invalid request body."},
            {"status": 401, "description": "Missing or invalid Pullwise API key."},
            {"status": 403, "description": "API key is valid but lacks the required scope."},
            {"status": 404, "description": "Route not found, repository not authorized, or no active scan exists."},
            {"status": 409, "description": "requestId was reused for a different repository."},
            {"status": 402, "description": "Scan quota is exhausted."},
            {"status": 413, "description": "Request body is too large."},
            {"status": 429, "description": "Rate limit exceeded when rate limiting is enabled."},
            {"status": 503, "description": "Review provider is not configured."},
        ],
    }


def dashboard_overview_payload(session: dict) -> dict:
    user = USERS.get(session["userId"])
    scans = [scan_payload(scan) for scan in user_scans(session)]
    repositories = repository_items_for_response(user, user.get("githubRepositoryAccess") if user else None) if user else []
    status_counts: dict[str, int] = {}
    for scan in scans:
        status = scan.get("status") or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "breadcrumbs": [{"label": "Overview", "href": "/dashboard/overview"}],
        "scanTotals": {
            "total": len(scans),
            "byStatus": status_counts,
        },
        "authorizedRepositories": {
            "count": len(repositories),
            "href": "/repositories",
            "items": repositories,
        },
        "recentScans": scans[:10],
    }


def public_billing_text(value: object) -> str | None:
    return public_issue_text(value) or None


def public_billing_status(value: object) -> str:
    status = public_issue_text(value).lower()
    return status if status in BILLING_PUBLIC_STATUSES else "none"


def safe_billing_redirect_response(result: dict, label: str, *, require_url: bool = False) -> dict:
    if not isinstance(result, dict):
        raise billing.BillingProviderResponseError("Billing provider returned an invalid response.")
    payload = dict(result)
    provider = public_billing_text(payload.get("provider")) or "Billing provider"
    if "url" not in payload:
        if require_url:
            billing.provider_redirect_url(None, provider, label)
        return payload
    payload["url"] = billing.provider_redirect_url(payload.get("url"), provider, label)
    return payload


def scan_payload(scan: dict) -> dict:
    payload = {
        "id": public_issue_text(scan.get("id")),
        "userId": public_issue_text(scan.get("userId")),
        "repo": clean_repository_full_name(scan.get("repo")),
        "branch": clean_github_access_text(scan.get("branch")) or "main",
        "commit": clean_github_access_text(scan.get("commit")) or "pending",
        "status": public_scan_status(scan.get("status")),
        "phase": public_scan_phase(scan.get("phase")),
        "progress": public_scan_progress(scan.get("progress")),
        "issues": public_scan_issue_counts(scan.get("issues")),
        "createdAt": pull_request_timestamp(scan.get("createdAt")) or 0,
    }
    for key in ("queuedAt", "startedAt", "completedAt", "updatedAt", "recoveredAt"):
        if key in scan:
            payload[key] = pull_request_timestamp(scan.get(key)) or 0
    if "error" in scan:
        payload["error"] = clean_scan_error(scan.get("error"))
    if "time" in scan:
        payload["time"] = public_issue_text(scan.get("time"))
    if "by" in scan:
        payload["by"] = public_issue_text(scan.get("by"))
    if "installationId" in scan:
        payload["installationId"] = clean_github_access_text(scan.get("installationId"), allow_int=True)
    for key in ("repoId", "githubRepoId"):
        if key in scan:
            payload[key] = clean_github_access_text(scan.get(key), allow_int=True)
    if isinstance(scan.get("quotaBucketIds"), dict):
        payload["quotaBucketIds"] = {
            key: clean_github_access_text(value, allow_int=True)
            for key, value in scan["quotaBucketIds"].items()
            if clean_github_access_text(value, allow_int=True)
        }
    if isinstance(scan.get("billingUsage"), dict):
        payload["billingUsage"] = safe_quota_usage_payload(scan.get("billingUsage"), default_scope="user")
    if isinstance(scan.get("repoUsage"), dict):
        payload["repoUsage"] = safe_quota_usage_payload(scan.get("repoUsage"), default_scope="repository")
    if isinstance(scan.get("riskDecision"), dict):
        decision = public_issue_text(scan["riskDecision"].get("decision"))
        reason = public_issue_text(scan["riskDecision"].get("reason"))
        risk_payload = {}
        if decision:
            risk_payload["decision"] = decision
        if reason:
            risk_payload["reason"] = reason
        matched_repository_id = clean_github_access_text(scan["riskDecision"].get("matchedRepositoryId"), allow_int=True)
        if matched_repository_id:
            risk_payload["matchedRepositoryId"] = matched_repository_id
        if risk_payload:
            payload["riskDecision"] = risk_payload
    if isinstance(scan.get("repoFingerprint"), dict):
        fingerprint_payload = {}
        for source_key, target_key in (
            ("headSha", "headSha"),
            ("treeSha", "treeSha"),
            ("lockfileHash", "lockfileHash"),
            ("manifestHash", "manifestHash"),
            ("sourceFingerprint", "sourceFingerprint"),
        ):
            value = clean_github_access_text(scan["repoFingerprint"].get(source_key))
            if value:
                fingerprint_payload[target_key] = value
        if fingerprint_payload:
            payload["repoFingerprint"] = fingerprint_payload
    if "installationAccount" in scan:
        payload["installationAccount"] = clean_github_access_text(scan.get("installationAccount"))
    if "installationTargetType" in scan:
        payload["installationTargetType"] = clean_github_access_text(scan.get("installationTargetType"))
    if "repositorySelection" in scan:
        payload["repositorySelection"] = clean_github_access_text(scan.get("repositorySelection"))
    if "cloneUrl" in scan:
        payload["cloneUrl"] = trusted_github_web_url(scan.get("cloneUrl"))
    if "jobId" in scan:
        payload["jobId"] = public_issue_text(scan.get("jobId"))
    claimed_by_worker_id = public_issue_text(scan.get("claimedByWorkerId"))
    if claimed_by_worker_id:
        payload["worker"] = {"id": claimed_by_worker_id}
    if pull_request_timestamp(scan.get("claimedAt")):
        payload["claimedAt"] = pull_request_timestamp(scan.get("claimedAt")) or 0
    queue = scan_queue_payload(scan)
    if queue:
        payload["queue"] = queue
    return payload


def public_scan_status(value: object) -> str:
    status = public_issue_text(value).lower()
    return status if status in SCAN_STATUSES else "queued"


def public_scan_phase(value: object) -> str:
    phase = public_issue_text(value)
    return phase if phase in SCAN_PHASES else ""


def public_scan_progress(value: object) -> float:
    if isinstance(value, bool):
        return 0
    try:
        progress = float(value or 0)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(progress):
        return 0
    return min(100, max(0, progress))


def public_scan_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        count = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return max(0, count)


def worker_max_concurrency_cap() -> int:
    return max(1, env_int("PULLWISE_WORKER_MAX_CONCURRENCY_CAP", 32))


def worker_admin_capacity(value: object) -> int:
    capacity = public_scan_count(value) or 1
    cap = worker_max_concurrency_cap()
    if capacity > cap:
        raise ValueError(f"max_concurrent_jobs cannot exceed {cap}.")
    return capacity


def worker_heartbeat_capacity(value: object) -> int:
    return min(public_scan_count(value) or 1, worker_max_concurrency_cap())


def public_scan_issue_counts(value: object) -> dict:
    counts = value if isinstance(value, dict) else {}
    return {
        "critical": public_scan_count(counts.get("critical")),
        "high": public_scan_count(counts.get("high")),
        "medium": public_scan_count(counts.get("medium")),
        "low": public_scan_count(counts.get("low")),
        "info": public_scan_count(counts.get("info")),
    }


def create_scan_job_for_scan(scan: dict) -> dict:
    job = db.create_scan_job(
        {
            "job_id": make_id("job"),
            "scan_id": scan.get("id"),
            "repo": scan.get("repo"),
            "branch": scan.get("branch"),
            "commit": scan.get("commit"),
            "status": "queued",
            "created_at": scan.get("queuedAt") or scan.get("createdAt") or now(),
            "user_id": scan.get("userId"),
            "repo_id": scan.get("repoId"),
            "github_repo_id": scan.get("githubRepoId"),
            "installation_id": scan.get("installationId"),
            "clone_url": scan.get("cloneUrl"),
            "max_attempts": env_int("PULLWISE_SCAN_JOB_MAX_ATTEMPTS", 3),
        }
    )
    scan["jobId"] = job.get("job_id")
    return job


def scan_queue_limit_error(user_id: str) -> tuple[int, str, str] | None:
    queued = [scan for scan in SCANS if scan.get("status") == "queued"]
    queued_for_user = [scan for scan in queued if str(scan.get("userId") or "") == user_id]
    running_for_user = [
        scan
        for scan in SCANS
        if scan.get("status") == "running" and str(scan.get("userId") or "") == user_id
    ]
    if len(queued) >= max_queued_scans_global():
        return HTTPStatus.TOO_MANY_REQUESTS, "The global scan queue is full. Try again after queued scans start.", "QUEUE_FULL_GLOBAL"
    if len(queued_for_user) >= max_queued_scans_per_user():
        return HTTPStatus.TOO_MANY_REQUESTS, "You have too many queued scans. Wait for one to start before adding another.", "QUEUE_FULL_USER"
    if len(running_for_user) >= max_scan_concurrency_per_user() and len(queued_for_user) >= max_queued_scans_per_user():
        return HTTPStatus.TOO_MANY_REQUESTS, "You have too many active scans. Wait for one to finish before adding another.", "ACTIVE_LIMIT_USER"
    return None


def scan_job_payload(job: dict, *, include_clone_token: bool = False) -> dict:
    payload = {
        "job_id": public_issue_text(job.get("job_id")),
        "scan_id": public_issue_text(job.get("scan_id")),
        "repo": clean_repository_full_name(job.get("repo")),
        "branch": clean_github_access_text(job.get("branch")) or "main",
        "commit": clean_github_access_text(job.get("commit")) or "pending",
        "status": public_issue_text(job.get("status")) if job.get("status") in SCAN_JOB_STATUSES else "queued",
        "attempt": public_scan_count(job.get("attempt")),
        "claimed_by_worker_id": public_issue_text(job.get("claimed_by_worker_id")),
        "claimed_at": pull_request_timestamp(job.get("claimed_at")),
        "started_at": pull_request_timestamp(job.get("started_at")),
        "completed_at": pull_request_timestamp(job.get("completed_at")),
        "timeout_at": pull_request_timestamp(job.get("timeout_at")),
        "error": clean_scan_error(job.get("error")),
        "result_checksum": public_issue_text(job.get("result_checksum")),
        "repo_id": clean_github_access_text(job.get("repo_id"), allow_int=True),
        "github_repo_id": clean_github_access_text(job.get("github_repo_id"), allow_int=True),
        "installation_id": clean_github_access_text(job.get("installation_id"), allow_int=True),
        "clone_url": trusted_github_web_url(job.get("clone_url")),
    }
    if include_clone_token:
        payload["clone_token"] = installation_clone_token_payload(job)
    return payload


def installation_clone_token_payload(job: dict) -> dict | None:
    installation_id = clean_github_access_text(job.get("installation_id"), allow_int=True)
    if not installation_id or not github_auth.app_api_configured():
        return None
    token_payload = github_auth.create_installation_access_token(installation_id)
    token = token_payload.get("token")
    if not token:
        raise github_auth.GitHubError("GitHub did not return an installation access token.")
    return {
        "token": token,
        "expires_at": public_issue_text(token_payload.get("expires_at")),
        "repo": clean_repository_full_name(job.get("repo")),
    }


def worker_result_checksum(body: dict) -> str:
    provided = clean_github_access_text(body.get("result_checksum"))
    if provided:
        return provided
    digest_payload = {
        "status": body.get("status"),
        "findings": body.get("findings") if isinstance(body.get("findings"), list) else [],
        "summary": body.get("summary") if isinstance(body.get("summary"), dict) else {},
        "duration_ms": body.get("duration_ms"),
        "error": body.get("error"),
    }
    data = json.dumps(db.to_jsonable(digest_payload), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def expected_worker_attempt_id(job: dict) -> str:
    worker_id = public_issue_text(job.get("claimed_by_worker_id"))
    attempt = public_scan_count(job.get("attempt"))
    if worker_id and attempt:
        return f"{worker_id}-{attempt}"
    return f"attempt_{attempt}"


def apply_worker_job_result_to_state_locked(job: dict, body: dict, *, status: str, checksum: str) -> bool:
    findings = body.get("findings") if isinstance(body.get("findings"), list) else []
    normalized_findings = [worker_finding_payload(job, item, index) for index, item in enumerate(findings)]
    summary = public_scan_issue_counts(body.get("summary") if isinstance(body.get("summary"), dict) else summarize_findings(normalized_findings))
    completed_at = pull_request_timestamp(job.get("completed_at")) or now()
    scan = next((item for item in SCANS if item.get("id") == job.get("scan_id")), None)
    changed = False
    if scan:
        before = json.dumps(db.to_jsonable(scan), sort_keys=True)
        scan.update(
            {
                "status": status,
                "phase": "report",
                "progress": 100 if status == "done" else public_scan_progress(scan.get("progress")),
                "completedAt": completed_at,
                "durationMs": public_scan_count(body.get("duration_ms")),
                "issues": summary,
                "error": clean_scan_error(body.get("error")) if status == "failed" else "",
                "resultChecksum": checksum,
            }
        )
        changed = before != json.dumps(db.to_jsonable(scan), sort_keys=True)
        if status == "done":
            before_issues = json.dumps(
                db.to_jsonable([issue for issue in ISSUES if issue.get("scanId") == scan.get("id") and issue.get("jobId") == job.get("job_id")]),
                sort_keys=True,
            )
            ISSUES[:] = [
                issue
                for issue in ISSUES
                if not (issue.get("scanId") == scan.get("id") and issue.get("jobId") == job.get("job_id"))
            ]
            ISSUES.extend(normalized_findings)
            after_issues = json.dumps(db.to_jsonable(normalized_findings), sort_keys=True)
            changed = changed or before_issues != after_issues
    if changed:
        mark_state_dirty()
    return changed


def apply_worker_job_result(job: dict, body: dict) -> dict:
    status = public_issue_text(body.get("status")).lower()
    if status not in {"done", "failed"}:
        raise ValueError("status must be done or failed")
    expected_attempt_id = expected_worker_attempt_id(job)
    attempt_id = clean_github_access_text(body.get("attempt_id")) or expected_attempt_id
    if attempt_id != expected_attempt_id:
        return {"accepted": False, "conflict": True}
    checksum = worker_result_checksum(body)
    record_result = db.record_scan_job_result(
        str(job["job_id"]),
        attempt_id=attempt_id,
        status=status,
        result_checksum=checksum,
        payload=body,
    )
    if record_result.get("conflict"):
        return {"accepted": False, "conflict": True}
    duplicate = bool(record_result.get("duplicate"))
    with STATE_LOCK:
        apply_worker_job_result_to_state_locked(job, body, status=status, checksum=checksum)
    findings = body.get("findings") if isinstance(body.get("findings"), list) else []
    return {"accepted": True, "duplicate": duplicate, "conflict": False, "issueCount": len(findings)}


def worker_finding_payload(job: dict, finding: object, index: int) -> dict:
    source = finding if isinstance(finding, dict) else {}
    scan_id = public_issue_text(job.get("scan_id"))
    repo = clean_repository_full_name(job.get("repo"))
    issue = dict(source)
    issue.setdefault("id", make_id("iss"))
    issue.update(
        {
            "userId": public_issue_text(job.get("user_id")),
            "scanId": scan_id,
            "jobId": public_issue_text(job.get("job_id")),
            "repo": repo,
            "branch": clean_github_access_text(job.get("branch")) or "main",
            "status": public_issue_status(issue.get("status")),
            "createdAt": now(),
        }
    )
    if not public_issue_text(issue.get("title")):
        issue["title"] = f"Finding {index + 1}"
    return issue


def summarize_findings(findings: list[dict]) -> dict:
    summary = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for finding in findings:
        severity = review._safe_severity(finding.get("severity"))
        if severity in summary:
            summary[severity] += 1
    return summary


def worker_heartbeat_timeout_seconds() -> int:
    return max(60, env_int("PULLWISE_WORKER_HEARTBEAT_TIMEOUT_SECONDS", 120))


def worker_version_compatible(worker: dict) -> bool:
    minimum = env("PULLWISE_MIN_WORKER_VERSION", "").strip()
    version = public_issue_text(worker.get("version"))
    if not minimum or not version:
        return True
    return version >= minimum


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
            "PULLWISE_CODEX_MODEL": "",
            "PULLWISE_CODEX_REASONING_EFFORT": "xhigh",
            "PULLWISE_OPENCODE_COMMAND": "opencode",
            "PULLWISE_OPENCODE_MODEL": "",
            "PULLWISE_OPENCODE_VARIANT": "",
            "PULLWISE_WORKER_POLL_JITTER_SECONDS": "2",
            "PULLWISE_WORKER_MAX_BACKOFF_SECONDS": "60",
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

cat > "$ENV_FILE" <<EOF
PULLWISE_SERVER_URL=$SERVER_URL
PULLWISE_WORKER_ID=$WORKER_ID
PULLWISE_WORKER_TOKEN=$WORKER_TOKEN
PULLWISE_PROVIDER=$PROVIDER
PULLWISE_PROVIDER_CHAIN=$PROVIDER_CHAIN
PULLWISE_MAX_CONCURRENT_JOBS=$MAX_CONCURRENT_JOBS
PULLWISE_CHECKOUT_ROOT=$CHECKOUT_ROOT
PULLWISE_LOG_DIR=$LOG_DIR
PULLWISE_WORKER_PACKAGE=$WORKER_PACKAGE
PULLWISE_CODEX_PACKAGE=$CODEX_PACKAGE
PULLWISE_CODEX_MODEL=${PULLWISE_CODEX_MODEL:-}
PULLWISE_CODEX_REASONING_EFFORT=${PULLWISE_CODEX_REASONING_EFFORT:-xhigh}
PULLWISE_OPENCODE_COMMAND=${PULLWISE_OPENCODE_COMMAND:-opencode}
PULLWISE_OPENCODE_MODEL=${PULLWISE_OPENCODE_MODEL:-}
PULLWISE_OPENCODE_VARIANT=${PULLWISE_OPENCODE_VARIANT:-}
PULLWISE_PYTHON_BIN=$PYTHON_BIN
PULLWISE_SERVICE_PATH=$SERVICE_PATH
PULLWISE_WORKER_POLL_JITTER_SECONDS=2
PULLWISE_WORKER_MAX_BACKOFF_SECONDS=60
EOF
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

cat > "$BIN_PATH" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [ -f /etc/pullwise-worker/worker.env ]; then
  set -a
  . /etc/pullwise-worker/worker.env
  set +a
fi
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
echo "If Codex is not logged in, run: sudo -u $SERVICE_USER codex login"
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


def scan_system_status_payload(*, admin: bool = False) -> dict:
    workers = [worker_public_payload(worker, admin=True) for worker in db.list_workers()]
    queued_jobs = len([scan for scan in SCANS if scan.get("status") == "queued"])
    running_jobs = len([scan for scan in SCANS if scan.get("status") == "running"])
    online = [worker for worker in workers if worker["status"] in {"idle", "busy"}]
    degraded = [worker for worker in workers if worker["status"] == "degraded"]
    offline = [worker for worker in workers if worker["status"] == "offline"]
    total_capacity = sum(public_scan_count(worker.get("max_concurrent_jobs")) for worker in online + degraded)
    available_capacity = sum(public_scan_count(worker.get("free_slots")) for worker in online + degraded)
    if not workers or (not online and not degraded):
        system_status = "down"
    elif degraded or offline:
        system_status = "degraded"
    else:
        system_status = "ok"
    payload = {
        "scanSystemStatus": system_status,
        "onlineWorkerCount": len(online),
        "totalWorkerCount": len(workers),
        "totalCapacity": total_capacity,
        "availableCapacity": available_capacity,
        "runningJobs": running_jobs,
        "queuedJobs": queued_jobs,
        "degradedWorkerCount": len(degraded),
        "offlineWorkerCount": len(offline),
    }
    if admin:
        payload["workers"] = workers
    return payload


def clean_scan_error(value: object) -> str:
    if not isinstance(value, str):
        return ""
    lines = value.replace("\x00", "").splitlines()
    return (lines[0] if lines else "").strip()[:500]


def issue_payload(issue: dict) -> dict:
    issue_id = public_issue_text(issue.get("id")) or clean_pull_request_issue_id(issue.get("id"))
    auto_fix = issue_auto_fix_contract_ok(issue)
    auto_fixable = auto_fix
    payload = {
        "id": issue_id,
        "userId": public_issue_text(issue.get("userId")),
        "scanId": public_issue_text(issue.get("scanId")),
        "repo": clean_repository_full_name(issue.get("repo")),
        "branch": public_issue_text(issue.get("branch")),
        "status": public_issue_status(issue.get("status")),
        "severity": review._safe_severity(issue.get("severity")),
        "category": review._safe_category(issue.get("category")),
        "title": review._safe_text(issue.get("title"), "Untitled finding"),
        "summary": review._safe_text_lenient(issue.get("summary")) or public_issue_text(issue.get("description")),
        "impact": review._safe_text_lenient(issue.get("impact")),
        "detectionReasoning": review._safe_text_lenient(issue.get("detectionReasoning")),
        "reproductionPath": review._safe_text_lenient(issue.get("reproductionPath")),
        "file": public_issue_text(issue.get("file")),
        "line": review._safe_non_negative_int(issue.get("line")),
        "confidence": review._safe_confidence(issue.get("confidence")),
        "confidenceRationale": review._safe_text_lenient(issue.get("confidenceRationale")),
        "autoFix": auto_fix,
        "autoFixable": auto_fixable,
        "effort": review._safe_text(issue.get("effort"), "-"),
        "fixBenefits": review._safe_text_lenient(issue.get("fixBenefits")),
        "fixRisks": review._safe_text_lenient(issue.get("fixRisks")),
        "tags": review._safe_text_list(issue.get("tags")),
        "steps": review._safe_text_list(issue.get("steps")),
        "badCode": review._safe_code_lines(issue.get("badCode")),
        "goodCode": review._safe_code_lines(issue.get("goodCode")),
        "references": review._safe_references(issue.get("references")),
        "createdAt": pull_request_timestamp(issue.get("createdAt")) or 0,
    }
    updated_at = pull_request_timestamp(issue.get("updatedAt"))
    if updated_at is not None:
        payload["updatedAt"] = updated_at
    age = public_issue_text(issue.get("age"))
    if age:
        payload["age"] = age
    pull_request = issue.get("pullRequest")
    if isinstance(pull_request, dict):
        payload["pullRequest"] = safe_existing_pull_request(
            pull_request,
            issue_id=issue_id,
            fallback_title=pull_request_title(issue, issue_id),
        )
    pending = issue.get("pullRequestPending")
    if isinstance(pending, dict):
        payload["pullRequestPending"] = safe_pending_pull_request(pending, issue_id=issue_id)
    return payload


def safe_quota_usage_payload(value: object, *, default_scope: str) -> dict:
    usage = value if isinstance(value, dict) else {}
    used = non_negative_int(usage.get("used"))
    limit = non_negative_int(usage.get("limit"))
    return {
        "scope": clean_github_access_text(usage.get("scope")) or default_scope,
        "period": clean_github_access_text(usage.get("period")) or current_review_usage_period(),
        "plan": clean_github_access_text(usage.get("plan")) or "free",
        "used": used,
        "limit": limit,
        "remaining": max(0, non_negative_int(usage.get("remaining")) if "remaining" in usage else limit - used),
        "resetAt": non_negative_int(usage.get("resetAt")),
        "bucketId": clean_github_access_text(usage.get("bucketId"), allow_int=True),
    }


def issue_auto_fix_contract_ok(issue: dict) -> bool:
    if issue.get("autoFix") is not True and issue.get("autoFixable") is not True:
        return False
    if not fix_workflow.safe_issue_file(issue.get("file")):
        return False
    if not fix_workflow.code_lines(issue.get("badCode")) or not fix_workflow.code_lines(issue.get("goodCode")):
        return False

    scan_id = public_issue_text(issue.get("scanId"))
    if not scan_id:
        return True
    scan = next((item for item in SCANS if item.get("id") == scan_id), None)
    if not scan:
        return True
    repo_path = scan.get("repoPath")
    user_id = public_issue_text(issue.get("userId"))
    if not isinstance(repo_path, str) or not repo_path or not user_id:
        return True
    if not checkout.path_in_scan_workspace(repo_path, user_id, scan_id) or not os.path.exists(repo_path):
        return True

    try:
        return fix_workflow.preview_issue_fix(repo_path, issue).get("valid") is True
    except (OSError, UnicodeError, ValueError):
        return False


def public_issue_text(value: object) -> str:
    return review._safe_text(value)


def public_issue_status(value: object) -> str:
    status = public_issue_text(value).lower()
    return status if status in ISSUE_STATUSES else "open"


def scan_queue_payload(scan: dict) -> dict | None:
    status = scan.get("status")
    if status not in {"queued", "running"}:
        return None

    user_id = str(scan.get("userId") or "")
    limits = {
        "perUser": max_scan_concurrency_per_user(),
        "queuedGlobal": max_queued_scans_global(),
        "queuedPerUser": max_queued_scans_per_user(),
    }
    running = [item for item in SCANS if item.get("status") == "running"]
    running_for_user = [item for item in running if str(item.get("userId") or "") == user_id]
    running_counts = {
        "global": len(running),
        "user": len(running_for_user),
    }

    if status == "running":
        return {
            "position": 0,
            "ahead": 0,
            "userPosition": 0,
            "userAhead": 0,
            "reason": "running",
            "message": "Your scan is running now.",
            "limits": limits,
            "running": running_counts,
        }

    queued = sorted(
        [item for item in SCANS if item.get("status") == "queued"],
        key=scan_queue_sort_key,
    )
    queue_index = next((index for index, item in enumerate(queued) if item.get("id") == scan.get("id")), -1)
    position = queue_index + 1 if queue_index >= 0 else 0
    ahead = max(0, position - 1)

    user_queued = [item for item in queued if str(item.get("userId") or "") == user_id]
    user_index = next((index for index, item in enumerate(user_queued) if item.get("id") == scan.get("id")), -1)
    user_position = user_index + 1 if user_index >= 0 else 0
    user_ahead = max(0, user_position - 1)

    if running_counts["user"] >= limits["perUser"]:
        reason = "user_limit"
        message = (
            f"You already have {plural(running_counts['user'], 'scan')} running; "
            "this scan is queued and will start when one finishes."
        )
    elif ahead > 0:
        reason = "waiting_for_turn"
        message = f"Queued with {plural(ahead, 'scan')} ahead."
    else:
        reason = "ready"
        message = "Queued and waiting for the next available worker."

    return {
        "position": position,
        "ahead": ahead,
        "userPosition": user_position,
        "userAhead": user_ahead,
        "reason": reason,
        "message": message,
        "limits": limits,
        "running": running_counts,
    }


def scan_queue_sort_key(scan: dict) -> tuple[int, str]:
    return (
        int(scan.get("queuedAt") or scan.get("createdAt") or 0),
        str(scan.get("id") or ""),
    )


def max_scan_concurrency_per_user() -> int:
    return max(1, env_int("PULLWISE_MAX_RUNNING_SCANS_PER_USER", env_int("PULLWISE_MAX_CONCURRENT_SCANS_PER_USER", 1)))


def max_queued_scans_global() -> int:
    return max(1, env_int("PULLWISE_MAX_QUEUED_SCANS_GLOBAL", 1000))


def max_queued_scans_per_user() -> int:
    return max(1, env_int("PULLWISE_MAX_QUEUED_SCANS_PER_USER", 20))


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def user_issues(session: dict | None) -> list[dict]:
    if not session:
        return []
    return [issue for issue in ISSUES if issue.get("userId") == session["userId"]]


def pagination_params(params: dict, *, default_limit: int = 50, max_limit: int = 200) -> tuple[int, int]:
    try:
        limit = int(params.get("limit") or default_limit)
    except (TypeError, ValueError):
        limit = default_limit
    try:
        offset = int(params.get("offset") or 0)
    except (TypeError, ValueError):
        offset = 0
    return max(1, min(max_limit, limit)), max(0, offset)


def paginated_response(items: list[dict], *, keys: tuple[str, ...], params: dict) -> dict:
    limit, offset = pagination_params(params)
    total = len(items)
    page = items[offset : offset + limit]
    next_offset = offset + len(page)
    payload = {
        "items": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "hasMore": next_offset < total,
        "nextOffset": next_offset if next_offset < total else None,
    }
    for key in keys:
        payload[key] = page
    return payload


def filter_user_scan_payloads(scans: list[dict], params: dict) -> list[dict]:
    raw_status = public_issue_text(params.get("status")).lower()
    status = public_scan_status(raw_status) if raw_status and raw_status != "all" else ""
    repo = clean_repository_full_name(params.get("repo"))
    if status:
        scans = [scan for scan in scans if scan.get("status") == status]
    if repo:
        scans = [scan for scan in scans if scan.get("repo") == repo]
    return sorted(scans, key=lambda scan: (pull_request_timestamp(scan.get("createdAt")) or 0, public_issue_text(scan.get("id"))), reverse=True)


def filter_user_issue_payloads(issues: list[dict], params: dict) -> list[dict]:
    raw_status = public_issue_text(params.get("status")).lower()
    raw_severity = public_issue_text(params.get("severity")).lower()
    status = public_issue_status(raw_status) if raw_status and raw_status != "all" else ""
    severity = review._safe_severity(raw_severity) if raw_severity and raw_severity != "all" else ""
    scan_id = public_issue_text(params.get("scanId"))
    query = public_issue_text(params.get("q")).lower()
    if status:
        issues = [issue for issue in issues if issue.get("status") == status]
    if severity:
        issues = [issue for issue in issues if issue.get("severity") == severity]
    if scan_id:
        issues = [issue for issue in issues if issue.get("scanId") == scan_id]
    if query:
        issues = [
            issue
            for issue in issues
            if any(
                query in public_issue_text(value).lower()
                for value in (issue.get("title"), issue.get("file"), issue.get("repo"), issue.get("category"), issue.get("id"))
            )
        ]
    return issues


@contextmanager
def preview_scan_lock(scan_id: str) -> Iterator[None]:
    with PREVIEW_SCAN_LOCKS_GUARD:
        entry = PREVIEW_SCAN_LOCKS.get(scan_id)
        if entry is None:
            entry = PreviewScanLockEntry()
            PREVIEW_SCAN_LOCKS[scan_id] = entry
        entry.refs += 1

    entry.lock.acquire()
    try:
        yield
    finally:
        entry.lock.release()
        with PREVIEW_SCAN_LOCKS_GUARD:
            entry.refs -= 1
            if entry.refs == 0 and PREVIEW_SCAN_LOCKS.get(scan_id) is entry:
                PREVIEW_SCAN_LOCKS.pop(scan_id, None)


def preview_issue_fix_for_user(user: dict, issue: dict) -> dict:
    scan_id = issue.get("scanId")
    scan = next((item for item in SCANS if item.get("id") == scan_id), None)
    if not scan:
        raise ValueError("Scan not found for issue.")
    user_id = str(user.get("id") or "")
    scan_id = str(scan.get("id") or scan_id or "")
    if str(scan.get("userId") or "") != user_id:
        raise ValueError("Scan does not belong to the signed-in user.")
    if scan.get("status") != "done":
        raise ValueError("Scan must be completed before previewing fixes.")

    with preview_scan_lock(scan_id):
        repo_path = scan.get("repoPath")
        if repo_path:
            repo_path = str(repo_path)
            if not checkout.path_in_scan_workspace(repo_path, user_id, scan_id):
                raise ValueError("Scan checkout path is outside the scan workspace.")
            if os.path.exists(repo_path):
                return fix_workflow.preview_issue_fix(repo_path, issue)

        try:
            repo_path = checkout.prepare_checkout(scan_id, scan, lambda: False)
        except (RuntimeError, OSError, checkout.CheckoutCancelled) as exc:
            try:
                checkout.cleanup_scan_workspace(user_id, scan_id)
            except (RuntimeError, OSError) as cleanup_exc:
                raise ValueError(f"Unable to clean up failed preview checkout: {cleanup_exc}") from cleanup_exc
            raise ValueError(str(exc)) from exc

        try:
            repo_path = str(repo_path)
            if not checkout.path_in_scan_workspace(repo_path, user_id, scan_id):
                raise ValueError("Prepared checkout path is outside the scan workspace.")
            return fix_workflow.preview_issue_fix(repo_path, issue)
        finally:
            try:
                checkout.cleanup_scan_workspace(user_id, scan_id)
            except (RuntimeError, OSError) as exc:
                raise ValueError(f"Unable to clean up preview checkout: {exc}") from exc


def create_issue_pull_request(user: dict, issue: dict) -> dict:
    user_id = str(user.get("id") or "")
    if not user_id or str(issue.get("userId") or "") != user_id:
        raise ValueError("Issue does not belong to the signed-in user.")

    scan_id = str(issue.get("scanId") or "")
    scan = next((item for item in SCANS if item.get("id") == scan_id), None)
    if not scan:
        raise ValueError("Scan not found for issue.")
    if str(scan.get("userId") or "") != user_id:
        raise ValueError("Scan does not belong to the signed-in user.")

    issue_id = clean_pull_request_issue_id(issue.get("id"))
    issue_slug = issue_id
    pr_scan_id = f"pr_{issue_slug}"

    with preview_scan_lock(f"pull-request:{issue_slug}"):
        if github_repository_authorization_pending(user):
            raise ValueError("Complete GitHub repository authorization before creating a pull request.")
        if scan.get("status") != "done":
            raise ValueError("Scan must be completed before creating a pull request.")

        github_access = user.get("githubRepositoryAccess")
        if not github_repository_access_authorized_for_user(user, github_access):
            raise ValueError("Authorize GitHub repositories before creating a pull request.")
        if github_repositories_need_sync(github_access):
            raise ValueError("Sync GitHub repositories before creating a pull request.")
        existing = issue.get("pullRequest")
        pending = issue.get("pullRequestPending") if not isinstance(existing, dict) else None
        recovering_pending = isinstance(pending, dict) and pull_request_pending_is_stale(pending)
        if isinstance(pending, dict) and not recovering_pending:
            raise ValueError("Pull request creation is already in progress for this issue.")
        if not github_auth.app_api_configured():
            raise ValueError("GitHub App API is not configured for pull request creation.")
        repo = clean_repository_full_name(issue.get("repo"), scan.get("repo"))
        if not repo:
            raise ValueError("Repository must be a GitHub full name like owner/repo.")
        if not repository_is_authorized(github_access, repo):
            raise ValueError("Repository is not authorized for this GitHub App installation.")

        repo_meta = repository_item(github_access, repo) or {}
        installation_permissions = repo_meta.get("installationPermissions") or github_access.get("installationPermissions")
        if not isinstance(installation_permissions, dict) or not installation_supports_pull_request_creation({"permissions": installation_permissions}):
            raise ValueError(github_app_write_permissions_message())
        title = pull_request_title(issue, issue_id)

        if isinstance(existing, dict):
            safe_existing = safe_existing_pull_request(existing, issue_id=issue_id, fallback_title=title)
            if safe_existing != existing:
                store_issue_pull_request(issue, safe_existing)
                return safe_existing
            return existing

        base_branch = (
            clean_github_access_text(issue.get("branch"))
            or clean_github_access_text(scan.get("branch"))
            or clean_github_access_text(repo_meta.get("defaultBranch"))
            or clean_github_access_text(github_access.get("defaultBranch"))
            or "main"
        )
        installation_id = (
            clean_github_access_text(repo_meta.get("installationId"), allow_int=True)
            or clean_github_access_text(scan.get("installationId"), allow_int=True)
            or clean_github_access_text(github_access.get("installationId"), allow_int=True)
            or ""
        )
        if not installation_id:
            raise ValueError("Repository is missing a GitHub App installation id.")
        clone_url = trusted_github_web_url(repo_meta.get("cloneUrl"))
        if not clone_url:
            clone_url = trusted_github_web_url(scan.get("cloneUrl"))

        recovery_token = ""
        if recovering_pending:
            branch = valid_stored_pull_request_branch(pending.get("branch"))
            if not branch:
                clear_pull_request_pending(issue)
                raise ValueError("Stored pull request branch is invalid.")
            recovery_token = installation_token(installation_id)
            recovered = github_auth.find_pull_request_by_head(recovery_token, repo, head=branch)
            if recovered:
                pull_request = {
                    "issueId": issue_id,
                    "branch": branch,
                    "url": recovered.get("url"),
                    "number": recovered.get("number"),
                    "title": recovered.get("title") or title,
                }
                store_issue_pull_request(issue, pull_request)
                return pull_request

            if github_auth.branch_exists(recovery_token, repo, branch):
                body = (
                    f"Automated deterministic fix for Pullwise issue {issue_id}.\n\n"
                    f"Repository: {repo}\n"
                    "Recovered from an existing Pullwise fix branch."
                )
                try:
                    created = github_auth.create_pull_request(
                        recovery_token,
                        repo,
                        title=title,
                        head=branch,
                        base=base_branch,
                        body=body,
                    )
                except github_auth.GitHubError as exc:
                    record_pull_request_pending_failure(issue, str(exc))
                    raise
                pull_request = {
                    "issueId": issue_id,
                    "branch": branch,
                    "url": created.get("url"),
                    "number": created.get("number"),
                    "title": created.get("title") or title,
                }
                store_issue_pull_request(issue, pull_request)
                return pull_request

        if not recovering_pending:
            random_token = safe_git_ref_component(make_id("fix").split("_", 1)[-1], "branch")[:16]
            branch = f"pullwise/fix-{issue_slug}-{random_token}"
        store_pull_request_pending(issue, issue_id, branch)

        scan_payload = dict(scan)
        scan_payload.update({
            "id": pr_scan_id,
            "userId": user_id,
            "repo": repo,
            "branch": base_branch,
            "installationId": installation_id,
            "cloneUrl": clone_url,
        })

        checkout_started = False
        irreversible_started = False
        try:
            checkout_started = True
            repo_path = checkout.prepare_checkout(pr_scan_id, scan_payload, lambda: False)
            repo_path = str(repo_path)
            if not checkout.path_in_scan_workspace(repo_path, user_id, pr_scan_id):
                raise ValueError("Prepared checkout path is outside the pull request workspace.")

            preview = fix_workflow.apply_issue_fix(repo_path, issue)
            if not preview.get("valid"):
                raise ValueError(str(preview.get("message") or "Issue fix could not be applied."))
            fix_file = str(preview.get("file") or "")
            if not fix_file:
                raise ValueError("Issue fix did not report a file to commit.")

            token = recovery_token or installation_token(installation_id)

            body = (
                f"Automated deterministic fix for Pullwise issue {issue_id}.\n\n"
                f"Repository: {repo}\n"
                f"File: {fix_file}"
            )
            git_env = checkout.git_auth_env(token)
            git_env.update({
                "GIT_AUTHOR_NAME": "Pullwise",
                "GIT_AUTHOR_EMAIL": "pullwise@example.invalid",
                "GIT_COMMITTER_NAME": "Pullwise",
                "GIT_COMMITTER_EMAIL": "pullwise@example.invalid",
            })
            checkout.run_git(
                ["git", "checkout", "-B", branch],
                cwd=repo_path,
                extra_env=git_env,
                is_cancelled=lambda: False,
                action="create fix branch",
            )
            checkout.run_git(
                ["git", "add", "--", fix_file],
                cwd=repo_path,
                extra_env=git_env,
                is_cancelled=lambda: False,
                action="stage issue fix",
            )
            checkout.run_git(
                ["git", "commit", "-m", title],
                cwd=repo_path,
                extra_env=git_env,
                is_cancelled=lambda: False,
                action="commit issue fix",
            )
            irreversible_started = True
            checkout.run_git(
                ["git", "push", "origin", f"HEAD:{branch}"],
                cwd=repo_path,
                extra_env=git_env,
                is_cancelled=lambda: False,
                action="push issue fix",
            )
            irreversible_started = True
            created = github_auth.create_pull_request(
                token,
                repo,
                title=title,
                head=branch,
                base=base_branch,
                body=body,
            )
            pull_request = {
                "issueId": issue_id,
                "branch": branch,
                "url": created.get("url"),
                "number": created.get("number"),
                "title": created.get("title") or title,
            }
            store_issue_pull_request(issue, pull_request)
            return pull_request
        except (RuntimeError, OSError, checkout.CheckoutCancelled) as exc:
            if irreversible_started:
                record_pull_request_pending_failure(issue, str(exc))
                raise github_auth.GitHubError(str(exc)) from exc
            clear_pull_request_pending(issue)
            if github_service_error(exc):
                raise github_auth.GitHubError(str(exc)) from exc
            raise ValueError(str(exc)) from exc
        except github_auth.GitHubError as exc:
            if irreversible_started:
                record_pull_request_pending_failure(issue, str(exc))
            else:
                clear_pull_request_pending(issue)
            raise
        except Exception:
            clear_pull_request_pending(issue)
            raise
        finally:
            if checkout_started:
                try:
                    checkout.cleanup_scan_workspace(user_id, pr_scan_id)
                except (RuntimeError, OSError) as exc:
                    logger.warning("Unable to clean up pull request checkout workspace %s: %s", pr_scan_id, exc)


def installation_token(installation_id: str) -> str:
    token_payload = github_auth.create_installation_access_token(installation_id)
    token = str(token_payload.get("token") or "")
    if not token:
        raise github_auth.GitHubError("GitHub did not return an installation access token.")
    return token


def pull_request_pending_is_stale(pending: dict) -> bool:
    try:
        started_at = int(pending.get("startedAt") or 0)
    except (TypeError, ValueError):
        started_at = 0
    return started_at <= now() - pull_request_pending_stale_seconds()


def pull_request_pending_stale_seconds() -> int:
    return max(60, env_int("PULLWISE_PR_PENDING_STALE_SECONDS", 15 * 60))


def valid_stored_pull_request_branch(branch: object) -> str | None:
    value = str(branch or "")
    if not value.startswith("pullwise/fix-"):
        return None
    if value.endswith("/") or value.endswith(".") or ".." in value or "//" in value or " " in value:
        return None
    if not re.match(r"^[A-Za-z0-9._/-]+$", value):
        return None
    parts = value.split("/")
    if any(not part or part.startswith(".") or part.casefold().endswith(".lock") for part in parts):
        return None
    return value


def store_pull_request_pending(issue: dict, issue_id: str, branch: str) -> None:
    with STATE_LOCK:
        issue["pullRequestPending"] = {
            "issueId": issue_id,
            "branch": branch,
            "startedAt": now(),
        }
        mark_state_dirty()
        persist_state()


def store_issue_pull_request(issue: dict, pull_request: dict) -> None:
    with STATE_LOCK:
        issue.pop("pullRequestPending", None)
        issue["pullRequest"] = pull_request
        mark_state_dirty()
        persist_state()


def safe_existing_pull_request(value: dict, *, issue_id: str, fallback_title: str) -> dict:
    number = value.get("number")
    return {
        "issueId": issue_id,
        "branch": valid_stored_pull_request_branch(value.get("branch")) or "",
        "url": trusted_github_web_url(value.get("url")),
        "number": number if isinstance(number, int) and not isinstance(number, bool) else None,
        "title": clean_pull_request_text(value.get("title")) or fallback_title,
    }


def safe_pending_pull_request(value: dict, *, issue_id: str) -> dict:
    payload = {
        "issueId": issue_id,
        "branch": valid_stored_pull_request_branch(value.get("branch")) or "",
        "startedAt": pull_request_timestamp(value.get("startedAt")) or 0,
    }
    if "lastError" in value:
        payload["lastError"] = clean_pull_request_error(value.get("lastError"))
    failed_at = pull_request_timestamp(value.get("failedAt"))
    if failed_at is not None:
        payload["failedAt"] = failed_at
    return payload


def pull_request_timestamp(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        if not math.isfinite(value):
            return None
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def record_pull_request_pending_failure(issue: dict, message: str) -> None:
    with STATE_LOCK:
        pending = issue.get("pullRequestPending")
        if isinstance(pending, dict):
            pending["lastError"] = clean_pull_request_error(message)
            pending["failedAt"] = now()
        mark_state_dirty()
        persist_state()


def clear_pull_request_pending(issue: dict) -> None:
    with STATE_LOCK:
        issue.pop("pullRequestPending", None)
        mark_state_dirty()
        persist_state()


def remote_git_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return message.startswith("git clone") or message.startswith("git fetch") or message.startswith("git push")


def github_service_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return remote_git_error(exc) or "installation access token" in message


def github_app_write_permissions_message() -> str:
    return "GitHub App installation must grant Contents: write and Pull requests: write for Pullwise to push fix branches and open pull requests."


def clean_pull_request_error(value: object) -> str:
    if not isinstance(value, str):
        return "Pull request creation failed."
    text = value.replace("\x00", "").splitlines()[0].strip()
    return (text or "Pull request creation failed.")[:500]


def installation_supports_pull_request_creation(installation: dict) -> bool:
    permissions = installation.get("permissions") or {}
    return permissions.get("contents") == "write" and permissions.get("pull_requests") == "write"


def clean_repository_full_name(*values: object) -> str:
    for value in values:
        candidate = clean_github_access_text(value)
        if not candidate:
            continue
        try:
            return checkout.validate_repo_full_name(candidate)
        except RuntimeError:
            continue
    return ""


def pull_request_title(issue: dict, issue_id: str) -> str:
    title = clean_pull_request_text(issue.get("title"))
    fallback = clean_pull_request_text(issue_id) or safe_git_ref_component(issue_id, "issue")
    return f"Fix {title or fallback}"


def clean_pull_request_issue_id(value: object) -> str:
    if not isinstance(value, str):
        return "issue"
    text = value.replace("\x00", "").splitlines()[0].strip()
    return safe_git_ref_component(text, "issue")


def clean_pull_request_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    if any(char in value for char in "\r\n\x00"):
        return ""
    return value.strip()


def safe_git_ref_component(value: object, fallback: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "")).strip("-_")
    slug = re.sub(r"-+", "-", slug).strip("-_")
    return slug or fallback


def repository_item(github_access: dict | None, full_name: str) -> dict | None:
    if not github_access:
        return None
    for item in repository_items_for_payload(github_access):
        if item.get("fullName") == full_name:
            return item
    return None


def repository_item_by_repo_id(github_access: dict | None, repo_id: str) -> dict | None:
    if not github_access or not repo_id:
        return None
    for item in repository_items_for_payload(github_access):
        if repo_id in {
            str(item.get("repoId") or ""),
            str(item.get("id") or ""),
            str(item.get("githubRepoId") or ""),
        }:
            return item
    return None


def repository_item_for_scan_request(github_access: dict | None, body: dict) -> tuple[dict | None, str | None]:
    repo_id = clean_github_access_text(body.get("repoId"), allow_int=True)
    if repo_id:
        return repository_item_by_repo_id(github_access, repo_id), "repoId"
    full_name = clean_repository_full_name(body.get("repo"))
    if full_name:
        return repository_item(github_access, full_name), "repo"
    return None, None


def repository_is_authorized(github_access: dict | None, full_name: str) -> bool:
    if not github_access:
        return False
    repositories = clean_github_access_text_list(github_access.get("repositories"))
    if repositories:
        return full_name in repositories
    return repository_item(github_access, full_name) is not None


def api_repository_authorized_for_user(user: dict | None, repository: dict | None) -> bool:
    if not user or not repository:
        return False
    github_access = user.get("githubRepositoryAccess")
    if not isinstance(github_access, dict):
        return False

    repository_id = clean_github_access_text(repository.get("id"), allow_int=True)
    github_repo_id = clean_github_access_text(repository.get("github_repo_id"), allow_int=True)
    full_name = clean_repository_full_name(repository.get("full_name"))
    for candidate in (repository_id, github_repo_id):
        if candidate and repository_item_by_repo_id(github_access, candidate):
            return True
    return bool(full_name and repository_is_authorized(github_access, full_name))


def sync_repository_access_for_user(user: dict | None, github_access: dict | None) -> None:
    if not user or not isinstance(github_access, dict):
        return
    try:
        from . import repository_access
        repository_access.sync_access_for_user(user, github_access)
    except Exception:
        logger.exception("Unable to sync repository access for user %s", user.get("id"))


def repository_item_with_quota(item: dict, user: dict | None = None) -> dict:
    payload = dict(item)
    repo_id = clean_github_access_text(payload.get("repoId"), allow_int=True)
    if not repo_id:
        github_repo_id = clean_github_access_text(payload.get("githubRepoId"), allow_int=True)
        if github_repo_id:
            repository = db.get_repository_by_github_repo_id(github_repo_id)
            if repository:
                repo_id = repository.get("id")
                payload["repoId"] = repo_id
    if repo_id and user:
        repository = db.get_repository(repo_id)
        if repository:
            payload["quota"] = quota.quota_payload_for_repository(repository, user)
    link_repo_id = clean_github_access_text(payload.get("repoId"), allow_int=True)
    if link_repo_id:
        payload["href"] = f"/repositories/{link_repo_id}"
        payload["scanAction"] = {"method": "POST", "href": f"/api/v1/repositories/{link_repo_id}/scans"}
    return payload


def repository_items_for_response(user: dict | None, github_access: dict | None) -> list[dict]:
    if user and isinstance(github_access, dict):
        sync_repository_access_for_user(user, github_access)
    return [repository_item_with_quota(item, user) for item in repository_items_for_payload(github_access)]


def scan_resource_context(user: dict, github_access: dict, repo_meta: dict) -> tuple[dict, dict]:
    sync_repository_access_for_user(user, github_access)
    from . import repository_access
    repo_record = repository_access.repository_record_from_item(repo_meta)
    if not repo_record:
        raise ValueError("REPOSITORY_SYNC_REQUIRED")
    repository = db.upsert_repository(repo_record)
    return user, repository


def repository_item_from_full_name(full_name: str) -> dict:
    name = full_name.split("/", 1)[1] if "/" in full_name else full_name
    web_url = github_auth.github_web_url().rstrip("/")
    return {
        "id": full_name,
        "name": name,
        "fullName": full_name,
        "desc": "",
        "description": "",
        "lang": "-",
        "private": False,
        "stars": "-",
        "branches": "-",
        "defaultBranch": "main",
        "updated": "",
        "htmlUrl": f"{web_url}/{full_name}",
        "cloneUrl": f"{web_url}/{full_name}.git",
        "permissions": {},
    }


def repository_items_for_payload(github_access: dict | None) -> list[dict]:
    if not github_access:
        return []
    repository_items = github_access.get("repositoryItems") or []
    if isinstance(repository_items, list):
        safe_items = [
            item
            for repository_item in repository_items
            if (item := safe_repository_item_for_payload(repository_item))
        ]
        if safe_items:
            return safe_items
    if (
        github_access.get("mode") != "github-app"
        and not github_access.get("installationId")
        and not github_access.get("installationIds")
    ):
        return []
    return [
        repository_item_from_full_name(str(full_name))
        for full_name in clean_github_access_text_list(github_access.get("repositories"))
    ]


def safe_repository_item_for_payload(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    full_name = clean_github_access_text(value.get("fullName"))
    if not full_name or "/" not in full_name:
        return None

    base_item = repository_item_from_full_name(full_name)
    description = clean_github_access_text(value.get("description")) or clean_github_access_text(value.get("desc")) or ""
    raw_repo_id = clean_github_access_text(value.get("id"), allow_int=True)
    github_repo_id = (
        clean_github_access_text(value.get("githubRepoId"), allow_int=True)
        or raw_repo_id
    )
    owner = value.get("owner") if isinstance(value.get("owner"), dict) else {}
    parent = value.get("parent") if isinstance(value.get("parent"), dict) else {}
    source = value.get("source") if isinstance(value.get("source"), dict) else {}
    return {
        "id": raw_repo_id or full_name,
        "repoId": clean_github_access_text(value.get("repoId"), allow_int=True),
        "githubRepoId": github_repo_id,
        "githubNodeId": clean_github_access_text(value.get("githubNodeId")) or clean_github_access_text(value.get("nodeId")),
        "name": clean_github_access_text(value.get("name")) or base_item["name"],
        "fullName": full_name,
        "desc": description,
        "description": description,
        "owner": {
            key: clean_github_access_text(owner.get(key), allow_int=key == "id")
            for key in ("login", "id", "type")
            if clean_github_access_text(owner.get(key), allow_int=key == "id")
        },
        "ownerLogin": clean_github_access_text(value.get("ownerLogin")) or clean_github_access_text(owner.get("login")),
        "ownerId": clean_github_access_text(value.get("ownerId"), allow_int=True) or clean_github_access_text(owner.get("id"), allow_int=True),
        "lang": clean_github_access_text(value.get("lang")) or clean_github_access_text(value.get("language")) or "-",
        "private": value.get("private") is True,
        "fork": value.get("fork") is True,
        "parentGithubRepoId": clean_github_access_text(value.get("parentGithubRepoId"), allow_int=True) or clean_github_access_text(parent.get("id"), allow_int=True),
        "sourceGithubRepoId": clean_github_access_text(value.get("sourceGithubRepoId"), allow_int=True) or clean_github_access_text(source.get("id"), allow_int=True),
        "stars": clean_github_access_text(value.get("stars")) or "-",
        "branches": clean_github_access_text(value.get("branches")) or "-",
        "defaultBranch": clean_github_access_text(value.get("defaultBranch")) or "main",
        "updated": clean_github_access_text(value.get("updated")) or "",
        "htmlUrl": trusted_github_web_url(value.get("htmlUrl")),
        "cloneUrl": trusted_github_web_url(value.get("cloneUrl")),
        "permissions": github_auth.permissions_to_dict(value.get("permissions") or {}),
        "installationId": clean_github_access_text(value.get("installationId"), allow_int=True),
        "installationAccount": clean_github_access_text(value.get("installationAccount")),
        "installationTargetType": clean_github_access_text(value.get("installationTargetType")),
        "repositorySelection": clean_github_access_text(value.get("repositorySelection")),
        "quota": safe_quota_usage_payload(value.get("quota"), default_scope="repository") if isinstance(value.get("quota"), dict) else None,
    }


def github_repository_access_connected(github_access: dict | None) -> bool:
    if not github_access or github_repositories_need_sync(github_access):
        return False
    return bool(repository_items_for_payload(github_access))


def github_repositories_need_sync(github_access: dict | None) -> bool:
    return bool(github_access and github_access.get("repositoriesNeedSync") is True)


def github_repository_access_authorized_for_user(user: dict | None, github_access: dict | None) -> bool:
    if not user or not isinstance(github_access, dict):
        return False
    if github_access.get("mode") == "local":
        return True
    if github_access.get("mode") != "github-app":
        return False

    authorized_user_id = github_access.get("authorizedUserId")
    if authorized_user_id and authorized_user_id != user.get("id"):
        return False

    authorized_github_id = str(github_access.get("authorizedGithubId") or "")
    current_github_id = str(user.get("githubId") or "")
    if authorized_github_id and current_github_id and authorized_github_id != current_github_id:
        return False

    authorized_login = str(github_access.get("authorizedGithubLogin") or "").casefold()
    current_login = str(user.get("githubLogin") or "").casefold()
    if authorized_login and current_login and authorized_login != current_login:
        return False

    if str(github_access.get("installationTargetType") or "").casefold() == "user":
        installation_account = str(github_access.get("installationAccount") or "").casefold()
        installation_id = clean_github_access_text(github_access.get("installationId"), allow_int=True)
        if (
            installation_account
            and current_login
            and installation_account != current_login
            and not verified_identity_can_access_user_installation(user, installation_id, installation_account)
        ):
            return False

    installations = github_access.get("installations") or []
    if not isinstance(installations, list):
        installations = []
    for installation in installations:
        if not isinstance(installation, dict):
            continue
        if str(installation.get("installationTargetType") or "").casefold() != "user":
            continue
        installation_account = str(installation.get("installationAccount") or "").casefold()
        installation_id = clean_github_access_text(installation.get("installationId"), allow_int=True)
        if (
            installation_account
            and current_login
            and installation_account != current_login
            and not verified_identity_can_access_user_installation(user, installation_id, installation_account)
        ):
            return False

    return bool(authorized_user_id)


def verified_identity_can_access_user_installation(
    user: dict | None,
    installation_id: str | None,
    installation_account: str,
) -> bool:
    if not user or not installation_id or not installation_account:
        return False
    access_record = latest_installation_access_record(user, installation_id)
    if not access_record or access_record.get("canAccess") is not True:
        return False
    identity = github_identity_by_id(user, clean_github_access_text(access_record.get("githubIdentityId")))
    identity_login = str((identity or {}).get("githubLogin") or (identity or {}).get("login") or "").casefold()
    return bool(identity_login and identity_login == installation_account.casefold())


def repository_sync_should_refresh(user: dict | None, github_access: dict | None, body: dict) -> bool:
    if body.get("force") is True:
        return True
    if not user:
        return False
    if github_repository_authorization_pending(user):
        return True
    if not github_access:
        return True
    if not github_repository_access_authorized_for_user(user, github_access):
        return True
    if github_repositories_need_sync(github_access):
        return True
    return not github_repository_access_connected(github_access)


def github_repositories_connected_for_user(user: dict | None) -> bool:
    if not user or github_repository_authorization_pending(user):
        return False
    github_access = user.get("githubRepositoryAccess")
    return github_repository_access_authorized_for_user(user, github_access) and github_repository_access_connected(github_access)


def pending_repositories_payload() -> dict:
    return {
        "items": [],
        "repositories": [],
        "needsAuthorization": True,
        "authorizationPending": True,
        "authorizationIssue": "github_authorization_pending",
        "message": (
            "GitHub repository authorization is still pending. "
            "Complete the GitHub App setup window, then sync repositories again."
        ),
    }


def unavailable_repositories_payload(github_access: dict) -> dict:
    repositories_need_sync = github_repositories_need_sync(github_access)
    payload = {
        "items": [],
        "repositories": [],
        "needsAuthorization": True,
        "installationId": clean_github_access_text(github_access.get("installationId"), allow_int=True),
        "installationIds": clean_github_access_text_list(github_access.get("installationIds"), allow_int=True),
        "repositorySelection": clean_github_access_text(github_access.get("repositorySelection")),
        "installationAccount": clean_github_access_text(github_access.get("installationAccount")),
        "installationAccounts": clean_github_access_text_list(github_access.get("installationAccounts")),
        "installations": safe_installation_summaries(github_access.get("installations") or []),
        "repositoriesNeedSync": repositories_need_sync,
    }
    if repositories_need_sync and not github_auth.app_api_configured():
        payload.update({
            "authorizationIssue": "github_app_api_unconfigured",
            "message": (
                "GitHub App API is not configured, so Pullwise cannot sync authorized repositories. "
                "Set PULLWISE_GITHUB_APP_ID and a valid GitHub App private key path or base64 private key, then restart the server."
            ),
        })
    return payload


def installation_summary_from_access(github_access: dict) -> dict:
    repositories = github_access.get("repositories")
    return safe_installation_summary({
        "installationId": github_access.get("installationId"),
        "installationAccount": github_access.get("installationAccount"),
        "installationTargetType": github_access.get("installationTargetType"),
        "installationAppSlug": github_access.get("installationAppSlug"),
        "installationHtmlUrl": github_access.get("installationHtmlUrl"),
        "repositorySelection": github_access.get("repositorySelection"),
        "scope": github_access.get("scope"),
        "repositoryCount": len(repositories) if isinstance(repositories, list) else 0,
        "repositoriesNeedSync": github_repositories_need_sync(github_access),
    })


def safe_installation_summaries(installations: list[dict]) -> list[dict]:
    if not isinstance(installations, list):
        return []
    return [
        safe_installation_summary(installation)
        for installation in installations
        if isinstance(installation, dict)
    ]


def safe_installation_summary(installation: dict) -> dict:
    safe_url = trusted_github_web_url(installation.get("installationHtmlUrl"))
    return {
        "installationId": clean_installation_summary_text(installation.get("installationId")),
        "installationAccount": clean_installation_summary_text(installation.get("installationAccount")),
        "installationTargetType": clean_installation_summary_text(installation.get("installationTargetType")),
        "installationAppSlug": clean_installation_summary_text(installation.get("installationAppSlug")),
        "installationHtmlUrl": safe_url,
        "repositorySelection": clean_installation_summary_text(installation.get("repositorySelection")),
        "scope": clean_installation_summary_text(installation.get("scope")),
        "repositoryCount": safe_installation_repository_count(installation.get("repositoryCount")),
        "repositoriesNeedSync": installation.get("repositoriesNeedSync") is True,
    }


def public_installation_summary(user: dict | None, installation: dict) -> dict:
    item = safe_installation_summary(installation)
    installation_id = clean_installation_summary_text(item.get("installationId"))
    item["installationHtmlUrl"] = None
    item["manage"] = github_installation_manage_status(user, installation_id)
    return item


def public_installation_summaries(user: dict | None, github_access: dict | None) -> list[dict]:
    return [
        public_installation_summary(user, installation)
        for installation in installation_summaries_for_access(github_access)
    ]


def installation_summaries_for_access(github_access: dict | None) -> list[dict]:
    if not isinstance(github_access, dict):
        return []
    installations = github_access.get("installations")
    if isinstance(installations, list) and installations:
        return [
            safe_installation_summary(installation)
            for installation in installations
            if isinstance(installation, dict)
        ]
    if clean_github_access_text(github_access.get("installationId"), allow_int=True):
        return [installation_summary_from_access(github_access)]
    return []


def installation_summary_by_id(github_access: dict | None, installation_id: str) -> dict | None:
    target = str(installation_id)
    for installation in installation_summaries_for_access(github_access):
        if str(installation.get("installationId") or "") == target:
            return installation
    return None


def github_installation_manage_status(user: dict | None, installation_id: str | None) -> dict:
    access_record = latest_installation_access_record(user, installation_id)
    if not access_record:
        return {"mode": "needs_identity"}
    identity = github_identity_by_id(user, clean_github_access_text(access_record.get("githubIdentityId")))
    if access_record.get("canAccess") is True and identity:
        public_identity = public_github_identity(identity)
        if public_identity["status"] == "needs_reauth":
            return {
                "mode": "needs_reauth",
                "githubIdentityId": public_identity["id"],
                "githubLogin": public_identity["login"],
                "lastVerifiedAt": public_identity["lastVerifiedAt"],
            }
        return {
            "mode": "verified_identity",
            "githubIdentityId": public_identity["id"],
            "githubLogin": public_identity["login"],
            "lastVerifiedAt": pull_request_timestamp(access_record.get("verifiedAt")),
        }
    if access_record.get("lastErrorCode") == "github_identity_reauth_required":
        return {"mode": "needs_reauth"}
    return {"mode": "needs_identity", "lastErrorCode": clean_github_access_text(access_record.get("lastErrorCode"))}


def clean_installation_summary_text(value: object) -> str | None:
    return clean_github_access_text(value, allow_int=True)


def clean_github_access_text(value: object, *, allow_int: bool = False) -> str | None:
    if allow_int and isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or any(char in value for char in "\r\n"):
        return None
    return value


def clean_github_access_text_list(value: object, *, allow_int: bool = False) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        text
        for item in value
        if (text := clean_github_access_text(item, allow_int=allow_int))
    ]


def safe_installation_repository_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        count = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return max(0, count)


def repository_item_with_installation_context(repository_item: dict, github_access: dict) -> dict:
    item = dict(repository_item)
    item["installationId"] = clean_github_access_text(github_access.get("installationId"), allow_int=True)
    item["installationAccount"] = clean_github_access_text(github_access.get("installationAccount"))
    item["installationTargetType"] = clean_github_access_text(github_access.get("installationTargetType"))
    item["repositorySelection"] = clean_github_access_text(github_access.get("repositorySelection"))
    item["installationPermissions"] = github_access.get("installationPermissions") or {}
    return item


def aggregate_repository_scope(values: list[str]) -> str | None:
    clean_values = [value for value in values if value]
    if not clean_values:
        return None
    first = clean_values[0]
    if all(value == first for value in clean_values):
        return first
    return "mixed"


def github_repository_access_for_installation(
    installation_id: str,
    requested_scope: str = "selected",
    user_access_token: str | None = None,
    installation_hint: dict | None = None,
) -> dict:
    installation = dict(installation_hint or {})
    repository_items = []
    app_api_configured = github_auth.app_api_configured()
    if app_api_configured:
        installation = github_auth.fetch_installation(installation_id)
        if not installation_supports_pull_request_creation(installation):
            raise ValueError(github_app_write_permissions_message())
        repository_items = github_auth.list_installation_repositories(installation_id)
    elif user_access_token:
        if installation.get("permissions") and not installation_supports_pull_request_creation(installation):
            raise ValueError(github_app_write_permissions_message())
        try:
            repository_items = github_auth.list_user_installation_repositories(user_access_token, installation_id)
        except Exception:
            repository_items = []

    repository_selection = installation.get("repository_selection") or requested_scope or "selected"
    account = installation.get("account") or {}
    github_access = {
        "mode": "github-app",
        "scope": "all" if repository_selection == "all" else "selected",
        "repositorySelection": repository_selection,
        "authorizedAt": now(),
        "installationId": installation_id,
        "installationAccount": account.get("login"),
        "installationTargetType": installation.get("target_type"),
        "installationAppSlug": installation.get("app_slug"),
        "installationHtmlUrl": trusted_github_web_url(installation.get("html_url")),
        "installationPermissions": installation.get("permissions") or {},
        "repositories": [repo["fullName"] for repo in repository_items],
        "repositoriesNeedSync": not app_api_configured and not repository_items,
    }
    github_access["repositoryItems"] = [
        repository_item_with_installation_context(repo, github_access)
        for repo in repository_items
    ]
    return github_access


def aggregate_github_repository_access(user: dict, installation_accesses: list[dict]) -> dict | None:
    if not installation_accesses:
        return None

    timestamp = now()
    repository_items_by_name: dict[str, dict] = {}
    for access in installation_accesses:
        for item in access.get("repositoryItems") or []:
            full_name = str(item.get("fullName") or "")
            if full_name and full_name not in repository_items_by_name:
                repository_items_by_name[full_name] = item

    repository_items = list(repository_items_by_name.values())
    installation_summaries = [installation_summary_from_access(access) for access in installation_accesses]
    installation_ids = [str(access.get("installationId")) for access in installation_accesses if access.get("installationId")]
    installation_accounts = [
        str(access.get("installationAccount"))
        for access in installation_accesses
        if access.get("installationAccount")
    ]
    unique_accounts = list(dict.fromkeys(installation_accounts))
    repository_selections = [str(access.get("repositorySelection") or "") for access in installation_accesses]
    scopes = [str(access.get("scope") or "") for access in installation_accesses]
    single_access = installation_accesses[0] if len(installation_accesses) == 1 else None

    return {
        "mode": "github-app",
        "scope": aggregate_repository_scope(scopes) or "selected",
        "repositorySelection": aggregate_repository_scope(repository_selections) or "selected",
        "authorizedAt": timestamp,
        "authorizedUserId": user.get("id"),
        "authorizedGithubId": user.get("githubId"),
        "authorizedGithubLogin": user.get("githubLogin"),
        "validatedAt": timestamp,
        "repositoriesSyncedAt": timestamp,
        "installationId": single_access.get("installationId") if single_access else None,
        "installationIds": installation_ids,
        "installationAccount": unique_accounts[0] if len(unique_accounts) == 1 else None,
        "installationAccounts": unique_accounts,
        "installationTargetType": single_access.get("installationTargetType") if single_access else None,
        "installationAppSlug": single_access.get("installationAppSlug") if single_access else None,
        "installationHtmlUrl": trusted_github_web_url(single_access.get("installationHtmlUrl")) if single_access else None,
        "installationPermissions": single_access.get("installationPermissions") if single_access else {},
        "installations": installation_summaries,
        "repositories": [item["fullName"] for item in repository_items],
        "repositoryItems": repository_items,
        "repositoriesNeedSync": not repository_items,
    }


def installation_accesses_from_github_access(github_access: dict | None) -> list[dict]:
    if not isinstance(github_access, dict) or github_access.get("mode") != "github-app":
        return []
    if clean_github_access_text(github_access.get("installationId"), allow_int=True):
        return [dict(github_access)]

    repository_items = [
        item
        for item in github_access.get("repositoryItems") or []
        if isinstance(item, dict)
    ]
    accesses = []
    for summary in installation_summaries_for_access(github_access):
        installation_id = clean_github_access_text(summary.get("installationId"), allow_int=True)
        if not installation_id:
            continue
        items = [
            item
            for item in repository_items
            if str(item.get("installationId") or "") == installation_id
        ]
        accesses.append({
            "mode": "github-app",
            "scope": summary.get("scope") or github_access.get("scope") or "selected",
            "repositorySelection": summary.get("repositorySelection") or github_access.get("repositorySelection") or "selected",
            "authorizedAt": github_access.get("authorizedAt") or now(),
            "installationId": installation_id,
            "installationAccount": summary.get("installationAccount"),
            "installationTargetType": summary.get("installationTargetType"),
            "installationAppSlug": summary.get("installationAppSlug"),
            "installationHtmlUrl": trusted_github_web_url(summary.get("installationHtmlUrl")),
            "installationPermissions": github_access.get("installationPermissions") or {},
            "repositories": [item["fullName"] for item in items if item.get("fullName")],
            "repositoryItems": items,
            "repositoriesNeedSync": summary.get("repositoriesNeedSync") is True,
        })
    return accesses


def installation_allowed_for_identity(identity: dict, installation: dict) -> bool:
    if str(installation.get("target_type") or "").casefold() != "user":
        return True
    login = str(identity.get("githubLogin") or identity.get("login") or "").casefold()
    return bool(login) and installation_account_login(installation).casefold() == login


def sync_github_repository_installation_scope(
    user: dict,
    installation_id: str,
    *,
    github_identity_id: str | None = None,
) -> dict | None:
    identity = github_identity_by_id(user, github_identity_id) if github_identity_id else None
    if github_identity_id and not identity:
        raise ValueError("GitHub identity is not linked to this Pullwise account.")
    token = identity.get("accessToken") if identity else user.get("githubAccessToken")
    if not token:
        raise ValueError("Sign in with GitHub before syncing repositories.")

    installations = github_auth.list_current_app_installations_for_user(token)
    if identity:
        installations = [
            installation
            for installation in installations
            if installation_allowed_for_identity(identity, installation)
        ]
    else:
        installations = [
            installation
            for installation in installations
            if installation_allowed_for_user(user, installation)
        ]
    target = next(
        (
            installation
            for installation in installations
            if str(installation.get("id") or "") == str(installation_id)
        ),
        None,
    )
    if not target:
        if identity:
            upsert_github_identity_installation_access(
                user,
                identity,
                installation_id,
                can_access=False,
                last_error_code="github_installation_not_visible",
            )
        raise ValueError("GitHub installation is not visible to the selected GitHub identity.")

    refreshed_access = github_repository_access_for_installation(
        installation_id,
        target.get("repository_selection") or "selected",
        token,
        target,
    )
    existing_accesses = [
        access
        for access in installation_accesses_from_github_access(user.get("githubRepositoryAccess"))
        if str(access.get("installationId") or "") != str(installation_id)
    ]
    github_access = aggregate_github_repository_access(user, [*existing_accesses, refreshed_access])
    if github_access:
        user["githubRepositoryAccess"] = github_access
        mark_state_dirty()
    if identity:
        upsert_github_identity_installation_access(
            user,
            identity,
            installation_id,
            can_access=True,
        )
    return github_access


def bind_pending_selected_github_identity_access(user: dict | None) -> dict | None:
    pending = github_repository_authorization_pending(user)
    if not user or not isinstance(pending, dict):
        return None
    state = clean_github_access_text(pending.get("state"))
    if not state:
        return None
    try:
        record = peek_github_state("install", state)
    except ValueError:
        return None
    identity = github_identity_by_id(
        user,
        clean_github_access_text(record.get("selectedGithubIdentityId")),
    )
    if not identity:
        return None
    token = identity.get("accessToken")
    if not token:
        return None

    installations = [
        installation
        for installation in github_auth.list_current_app_installations_for_user(token)
        if installation_allowed_for_identity(identity, installation)
    ]
    github_access = user.get("githubRepositoryAccess")
    for installation in installations:
        installation_id = clean_github_access_text(installation.get("id"), allow_int=True)
        if not installation_id:
            continue
        github_access = bind_github_repository_installation_for_identity(
            user,
            installation,
            token,
            str(record.get("requestedScope") or "selected"),
        )
        upsert_github_identity_installation_access(
            user,
            identity,
            installation_id,
            can_access=True,
            verification_method="pending_sync",
        )
    return github_access if isinstance(github_access, dict) else None


def installation_allowed_for_user(user: dict, installation: dict) -> bool:
    if str(installation.get("target_type") or "").casefold() != "user":
        return True
    return installation_matches_user_login(user, installation)


def current_user_github_app_installations(user: dict) -> list[dict]:
    return [
        installation
        for installation in github_auth.list_current_app_installations_for_user(user.get("githubAccessToken"))
        if installation_allowed_for_user(user, installation)
    ]


def bind_github_repository_installations(
    user: dict,
    installations: list[dict],
    requested_scope: str = "selected",
) -> dict | None:
    installation_accesses = []
    for installation in installations:
        installation_id = str(installation.get("id") or "")
        if not installation_id:
            continue
        installation_accesses.append(
            github_repository_access_for_installation(
                installation_id,
                installation.get("repository_selection") or requested_scope,
                user.get("githubAccessToken"),
                installation,
            )
        )

    github_access = aggregate_github_repository_access(user, installation_accesses)
    if github_access:
        user["githubRepositoryAccess"] = github_access
        sync_repository_access_for_user(user, github_access)
        mark_state_dirty()
    return github_access


def bind_github_repository_installation_for_identity(
    user: dict,
    installation: dict,
    token: str | None,
    requested_scope: str = "selected",
) -> dict | None:
    installation_id = str(installation.get("id") or "")
    if not installation_id:
        return None
    refreshed_access = github_repository_access_for_installation(
        installation_id,
        installation.get("repository_selection") or requested_scope,
        token,
        installation,
    )
    existing_accesses = [
        access
        for access in installation_accesses_from_github_access(user.get("githubRepositoryAccess"))
        if str(access.get("installationId") or "") != installation_id
    ]
    github_access = aggregate_github_repository_access(user, [*existing_accesses, refreshed_access])
    if github_access:
        user["githubRepositoryAccess"] = github_access
        sync_repository_access_for_user(user, github_access)
        mark_state_dirty()
    return github_access


def installation_account_login(installation: dict) -> str:
    account = installation.get("account") or {}
    return str(account.get("login") or "")


def installation_matches_user_login(user: dict, installation: dict) -> bool:
    login = str(user.get("githubLogin") or "").casefold()
    if not login:
        return False
    if str(installation.get("target_type") or "").casefold() != "user":
        return False
    return installation_account_login(installation).casefold() == login


def try_bind_existing_github_repository_access(user: dict | None, *, force_refresh: bool = False) -> dict | None:
    if not user:
        return None
    existing_access = user.get("githubRepositoryAccess")
    if existing_access and not force_refresh and github_repository_access_authorized_for_user(user, existing_access):
        return existing_access
    if not github_auth.app_install_configured():
        return existing_access if existing_access and github_repository_access_authorized_for_user(user, existing_access) else None
    if not has_real_github_identity(user):
        return existing_access if existing_access and github_repository_access_authorized_for_user(user, existing_access) else None

    installations = current_user_github_app_installations(user)
    return bind_github_repository_installations(user, installations)


def has_real_github_identity(user: dict | None) -> bool:
    if not user:
        return False
    if not github_auth.oauth_configured():
        return "github" in user.get("providers", [])
    return bool(user.get("githubAccessToken"))


def has_github_repository_authorization_identity(user: dict | None) -> bool:
    if not user:
        return False
    if github_auth.oauth_configured():
        return bool(user.get("githubAccessToken") and user.get("githubLogin"))
    return "github" in user.get("providers", [])


def session_payload(session: dict | None) -> dict:
    if not session:
        return {
            "authenticated": False,
            "user": None,
            "github": {"identityConnected": False, "repositoriesConnected": False, "repositoryScope": None},
            "navigation": navigation_payload(),
            "nextStep": "sign_in",
        }

    user = USERS.get(session["userId"])
    repo_access = user.get("githubRepositoryAccess") if user else None
    repositories_pending = bool(github_repository_authorization_pending(user))
    repositories_authorized = github_repository_access_authorized_for_user(user, repo_access)
    visible_access = repo_access if repositories_authorized and not repositories_pending else None
    repositories_connected = repositories_authorized and github_repository_access_connected(repo_access) and not repositories_pending
    return {
        "authenticated": True,
        "user": user_public(user),
        "admin": user_is_admin(user),
        "github": {
            "identityConnected": has_real_github_identity(user),
            "login": public_issue_text(user.get("githubLogin")) or None,
            "repositoriesConnected": repositories_connected,
            "repositoriesAuthorizationPending": repositories_pending,
            "repositoryScope": clean_github_access_text(visible_access.get("scope")) if visible_access else None,
            "authorizedAt": pull_request_timestamp(visible_access.get("authorizedAt")) if visible_access else None,
            "installationId": clean_github_access_text(visible_access.get("installationId"), allow_int=True) if visible_access else None,
            "installationIds": clean_github_access_text_list(visible_access.get("installationIds"), allow_int=True) if visible_access else [],
            "repositorySelection": clean_github_access_text(visible_access.get("repositorySelection")) if visible_access else None,
            "repositoryCount": len(clean_github_access_text_list(visible_access.get("repositories"))) if visible_access else 0,
        },
        "navigation": navigation_payload(),
        "nextStep": "choose_repositories" if repositories_connected else "connect_github_repositories",
    }


def billing_event_id(update: dict) -> str:
    return billing_update_text(update.get("eventId"))


def billing_update_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or any(ord(char) < 32 or ord(char) == 127 for char in text):
        return ""
    return text


def billing_update_scalar(value: object) -> object | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, int | float):
        return value if math.isfinite(value) else None
    return None


def billing_update_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def billing_event_created(update: dict) -> int | None:
    value = update.get("eventCreated")
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        if not math.isfinite(value):
            return None
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def billing_event_processed(update: dict) -> bool:
    event_id = billing_event_id(update)
    return bool(event_id and event_id in BILLING_EVENTS)


def remember_billing_event(update: dict, *, applied: bool, stale: bool = False) -> None:
    event_id = billing_event_id(update)
    if not event_id:
        return
    BILLING_EVENTS[event_id] = {
        "eventType": billing_update_text(update.get("eventType")) or None,
        "eventCreated": billing_event_created(update),
        "processedAt": now(),
        "applied": applied,
        "stale": stale,
    }
    prune_billing_events()
    mark_state_dirty()


def prune_billing_events() -> None:
    if len(BILLING_EVENTS) <= MAX_BILLING_EVENT_RECORDS:
        return
    ordered = sorted(BILLING_EVENTS.items(), key=lambda item: item[1].get("processedAt") or 0)
    for event_id, _record in ordered[: len(BILLING_EVENTS) - MAX_BILLING_EVENT_RECORDS]:
        BILLING_EVENTS.pop(event_id, None)


def remember_pending_billing_update(update: dict) -> None:
    if not (billing_update_text(update.get("customerId")) or billing_update_text(update.get("subscriptionId"))):
        return
    event_id = billing_event_id(update)
    if event_id and any(billing_event_id(candidate) == event_id for candidate in BILLING_PENDING_UPDATES):
        return
    if billing_event_processed(update):
        return
    BILLING_PENDING_UPDATES.append(dict(update))
    if len(BILLING_PENDING_UPDATES) > MAX_BILLING_PENDING_UPDATES:
        del BILLING_PENDING_UPDATES[: len(BILLING_PENDING_UPDATES) - MAX_BILLING_PENDING_UPDATES]
    mark_state_dirty()


def billing_user_for_update(update: dict) -> dict | None:
    user = USERS.get(billing_update_text(update.get("userId")))
    if user:
        return user
    for candidate in USERS.values():
        if billing_update_matches_user(update, candidate):
            return candidate
    return None


def billing_update_matches_user(update: dict, user: dict) -> bool:
    current = user.get("billing") or {}
    customer_id = billing_update_text(update.get("customerId"))
    subscription_id = billing_update_text(update.get("subscriptionId"))
    user_id = billing_update_text(update.get("userId"))
    if customer_id and current.get("customerId") == customer_id:
        return True
    if subscription_id and current.get("subscriptionId") == subscription_id:
        return True
    return bool(user_id and user_id == user.get("id"))


def apply_billing_update_to_user(user: dict, update: dict) -> bool:
    current = user.get("billing") or {}
    incoming_created = billing_event_created(update)
    current_created = billing_event_created({"eventCreated": current.get("lastEventCreated")})
    if incoming_created is not None and current_created is not None and incoming_created < current_created:
        remember_billing_event(update, applied=False, stale=True)
        return False

    customer_id = billing_update_text(update.get("customerId"))
    customer_email = billing_update_text(update.get("customerEmail"))
    subscription_id = billing_update_text(update.get("subscriptionId"))
    subscription_item_id = billing_update_text(update.get("subscriptionItemId"))
    status = billing_update_text(update.get("status"))
    plan = billing_update_text(update.get("plan"))
    interval = billing_update_text(update.get("interval"))
    current_period_start = billing_update_scalar(update.get("currentPeriodStart"))
    current_period_end = billing_update_scalar(update.get("currentPeriodEnd"))
    cancel_at_period_end = billing_update_bool(update.get("cancelAtPeriodEnd"))
    canceled_at = billing_update_scalar(update.get("canceledAt"))
    provider = billing_update_text(update.get("provider"))
    event_type = billing_update_text(update.get("eventType"))
    event_id = billing_event_id(update)

    user["billing"] = {
        **current,
        "provider": provider or current.get("provider"),
        "customerId": customer_id or current.get("customerId"),
        "customerEmail": customer_email or current.get("customerEmail"),
        "subscriptionId": subscription_id or current.get("subscriptionId"),
        "subscriptionItemId": subscription_item_id or current.get("subscriptionItemId"),
        "status": status or current.get("status") or "active",
        "plan": plan or current.get("plan") or "pro",
        "interval": interval or current.get("interval") or "month",
        "currentPeriodStart": current_period_start if current_period_start is not None else current.get("currentPeriodStart"),
        "currentPeriodEnd": current_period_end if current_period_end is not None else current.get("currentPeriodEnd"),
        "cancelAtPeriodEnd": cancel_at_period_end if cancel_at_period_end is not None else current.get("cancelAtPeriodEnd"),
        "canceledAt": canceled_at if canceled_at is not None else current.get("canceledAt"),
        "updatedAt": now(),
        "lastEventType": event_type or current.get("lastEventType"),
        "lastEventId": event_id or current.get("lastEventId"),
        "lastEventCreated": incoming_created if incoming_created is not None else current.get("lastEventCreated"),
    }
    remember_billing_event(update, applied=True)
    mark_state_dirty()
    return True


def apply_pending_billing_updates_for_user(user: dict) -> None:
    matching = []
    remaining = []
    for update in BILLING_PENDING_UPDATES:
        if billing_update_matches_user(update, user):
            matching.append(update)
        else:
            remaining.append(update)
    if not matching:
        return

    BILLING_PENDING_UPDATES[:] = remaining
    mark_state_dirty()
    for update in sorted(matching, key=lambda item: billing_event_created(item) or 0):
        if not billing_event_processed(update):
            apply_billing_update_to_user(user, update)


def cookie_header(session_id: str) -> str:
    return f"{SESSION_COOKIE}={session_id}; {cookie_attributes()}; Max-Age={SESSION_MAX_AGE}"


def clear_cookie_header() -> str:
    return f"{SESSION_COOKIE}=; {cookie_attributes()}; Max-Age=0"


def cookie_attributes() -> str:
    attributes = ["Path=/", "HttpOnly", f"SameSite={cookie_same_site()}"]
    if cookie_secure_enabled():
        attributes.append("Secure")
    domain = cookie_domain()
    if domain:
        attributes.append(f"Domain={domain}")
    return "; ".join(attributes)


def cookie_same_site() -> str:
    configured = os.environ.get("PULLWISE_COOKIE_SAME_SITE", "").strip().lower()
    if configured == "none":
        return "None"
    if configured == "strict":
        return "Strict"
    return "Lax"


def cookie_domain() -> str:
    """Extract the registrable domain from the API base URL for cross-subdomain cookie sharing.

    For example, if the API is at api.pull-wise.com, set Domain=.pull-wise.com so the session
    cookie is shared with the frontend at pull-wise.com. Returns empty string for localhost,
    IP addresses, or when no suitable domain can be extracted.
    """
    configured = os.environ.get("PULLWISE_COOKIE_DOMAIN", "").strip()
    if configured:
        return configured if configured.startswith(".") else f".{configured}"
    public_base = os.environ.get("PULLWISE_API_BASE_URL") or ""
    if not public_base.startswith("https://"):
        return ""
    try:
        from urllib.parse import urlparse
        host = urlparse(public_base).hostname or ""
    except Exception:
        return ""
    # Skip localhost, IP addresses, and single-label names.
    if not host or host in {"localhost", "127.0.0.1", "::1"}:
        return ""
    parts = host.split(".")
    # Need at least two labels (e.g. pull-wise.com) to set a domain cookie.
    if len(parts) < 2:
        return ""
    # Skip IP addresses (all numeric labels).
    if all(part.isdigit() for part in parts):
        return ""
    # Use the last two labels as the registrable domain (e.g. api.pull-wise.com → .pull-wise.com).
    # This is a simplification — for multi-level TLDs like .co.uk, set PULLWISE_COOKIE_DOMAIN explicitly.
    return f".{parts[-2]}.{parts[-1]}"


def cookie_secure_enabled() -> bool:
    if cookie_same_site() == "None":
        return True
    if os.environ.get("PULLWISE_COOKIE_SECURE", "").strip():
        return env_flag("PULLWISE_COOKIE_SECURE")
    public_base = os.environ.get("PULLWISE_API_BASE_URL") or os.environ.get("PULLWISE_APP_URL") or ""
    return public_base.startswith("https://")


def external_api_segments(segments: list[str]) -> list[str] | None:
    if len(segments) >= 2 and segments[0] == "v1":
        return segments[1:]
    if len(segments) >= 3 and segments[0] == "api" and segments[1] == "v1":
        return segments[2:]
    return None


def decode_permissions(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def api_repository_payload(row: dict, user: dict | None = None) -> dict:
    repository = db.get_repository(str(row.get("id") or "")) if not row.get("github_repo_id") else row
    repository = repository or row
    payload = {
        "id": public_issue_text(repository.get("id")),
        "repoId": public_issue_text(repository.get("id")),
        "githubRepoId": public_issue_text(repository.get("github_repo_id")),
        "fullName": public_issue_text(repository.get("full_name")),
        "ownerLogin": public_issue_text(repository.get("owner_login")),
        "defaultBranch": public_issue_text(repository.get("default_branch")) or "main",
        "private": bool(repository.get("private")),
        "fork": bool(repository.get("fork")),
        "htmlUrl": trusted_public_url(repository.get("html_url")),
        "cloneUrl": trusted_public_url(repository.get("clone_url")),
        "installationId": clean_github_access_text(row.get("github_app_installation_id"), allow_int=True),
        "installationAccount": public_issue_text(row.get("installation_account")),
        "repositorySelection": public_issue_text(row.get("repository_selection")),
        "lastAuthorizedAt": pull_request_timestamp(row.get("last_authorized_at")),
        "permissions": decode_permissions(row.get("permissions")),
    }
    if user and repository.get("id"):
        payload["quota"] = quota.quota_payload_for_repository(repository, user)
    return payload


def latest_scan_for_user_repo(user_id: str, repo_id: str) -> dict | None:
    for scan in SCANS:
        if scan.get("userId") == user_id and scan.get("repoId") == repo_id:
            return scan
    return None


def active_scan_for_user_repo(user_id: str, repo_id: str) -> dict | None:
    for scan in SCANS:
        if (
            scan.get("userId") == user_id
            and scan.get("repoId") == repo_id
            and scan.get("status") in {"queued", "running"}
        ):
            return scan
    return None


class PullwiseHandler(BaseHTTPRequestHandler):
    server_version = "PullwiseDevAPI/0.1"

    def log_message(self, fmt: str, *args) -> None:
        access_logger.info("%s - %s", self.address_string(), fmt % args)

    def apply_rate_limit(self, method: str, path: str) -> bool:
        if not rate_limit_enabled() or rate_limit_exempt_path(method, path):
            self._rate_limit_headers = {}
            return False
        limit = rate_limit_requests()
        if limit <= 0:
            self._rate_limit_headers = {}
            return False

        try:
            rate = db.record_rate_limit_hit(
                self.rate_limit_subject(),
                limit=limit,
                window_seconds=rate_limit_window_seconds(),
            )
        except Exception:
            logger.exception("Failed to apply API rate limit.")
            self._rate_limit_headers = {}
            return False
        headers = {
            "X-RateLimit-Limit": str(rate["limit"]),
            "X-RateLimit-Remaining": str(rate["remaining"]),
            "X-RateLimit-Reset": str(rate["resetAt"]),
        }
        self._rate_limit_headers = headers
        if rate["allowed"]:
            return False

        retry_after = str(rate["retryAfter"])
        self.json(
            {"message": "API rate limit exceeded. Try again later."},
            HTTPStatus.TOO_MANY_REQUESTS,
            headers={**headers, "Retry-After": retry_after},
        )
        return True

    def rate_limit_subject(self) -> str:
        session = self.current_session()
        if session:
            return f"user:{session['userId']}"
        return f"ip:{self.client_ip_address()}"

    def client_ip_address(self) -> str:
        if env_flag("PULLWISE_TRUST_PROXY_HEADERS"):
            forwarded = first_header_value(self, "X-Forwarded-For")
            if forwarded:
                candidate = forwarded.split(",", 1)[0].strip()
                if candidate and not any(char in candidate for char in "\r\n"):
                    return candidate[:128]
        address = getattr(self, "client_address", None)
        if isinstance(address, tuple | list) and address:
            return str(address[0])[:128]
        return "unknown"

    def do_OPTIONS(self) -> None:
        try:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_cors_headers()
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization,X-Pullwise-Api-Key")
            self.end_headers()
        except _CLIENT_DISCONNECT_EXCEPTIONS:
            logger.debug("Client disconnected while handling OPTIONS %s", self.path)

    def do_GET(self) -> None:
        self.route("GET")

    def do_POST(self) -> None:
        self.route("POST")

    def do_PATCH(self) -> None:
        self.route("PATCH")

    def do_DELETE(self) -> None:
        self.route("DELETE")

    def route(self, method: str) -> None:
        ensure_state_loaded()
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        segments = [unquote(part) for part in path.split("/") if part]
        self._rate_limit_headers = {}

        try:
            try:
                if self.apply_rate_limit(method, path):
                    return
                self.enforce_body_size_limit(method)
                if method == "GET":
                    return self.handle_get(path, params, segments)
                if method == "POST":
                    return self.handle_post(path, params, segments)
                if method == "PATCH":
                    return self.handle_patch(segments)
                if method == "DELETE":
                    return self.handle_delete(segments)
                return self.error(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed")
            except ClientDisconnected:
                raise
            except RequestBodyTooLarge as exc:
                return self.error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(exc))
            except ResourceNotFound as exc:
                return self.error(HTTPStatus.NOT_FOUND, str(exc))
            except ValueError as exc:
                return self.error(HTTPStatus.BAD_REQUEST, str(exc))
            except billing.BillingProviderResponseError as exc:
                return self.error(HTTPStatus.BAD_GATEWAY, str(exc))
            except billing.BillingProviderConflict as exc:
                return self.error(HTTPStatus.BAD_REQUEST, str(exc))
            except billing.BillingConfigurationError as exc:
                return self.error(HTTPStatus.NOT_IMPLEMENTED, str(exc))
            except Exception as exc:
                logger.exception("Unhandled server error while handling %s %s", method, self.path)
                return self.error(HTTPStatus.INTERNAL_SERVER_ERROR, "Server error.")
        except ClientDisconnected:
            logger.debug("Client disconnected while handling %s %s", method, self.path)
            return
        finally:
            persist_state()

    def handle_get(self, path: str, params: dict, segments: list[str]) -> None:
        if path == "/health":
            return self.json({
                "ok": True,
                "service": "pullwise-server",
                "time": now(),
                "mode": env("PULLWISE_MODE", "local"),
                "database": {"type": "sqlite", "path": db.database_path()},
                "scanSystem": scan_system_status_payload(),
                **readiness_payload(),
            })
        if path == "/install-worker.sh":
            return self.text(worker_install_script(), content_type="text/x-shellscript; charset=utf-8")
        if path == "/status/system":
            return self.json(scan_system_status_payload())
        if segments and segments[0] == "admin":
            return self.handle_admin_get(segments, params)
        if path == "/pricing":
            session = self.current_session()
            user = USERS.get(session["userId"]) if session else None
            return self.json(pricing_payload(user))
        if path in {"/api-docs", "/api/docs"}:
            return self.json(api_docs_payload())
        api_segments = external_api_segments(segments)
        if api_segments is not None:
            return self.handle_external_api_get(api_segments, params)
        if path == "/auth/session":
            return self.json(session_payload(self.current_session()))
        if path == "/dashboard/overview":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing the dashboard.")
            return self.json(dashboard_overview_payload(session))
        if path == "/api-keys":
            return self.handle_api_keys_get(params)
        if path == "/auth/github/authorize":
            return self.handle_github_authorize(params)
        if path == "/auth/github/callback":
            return self.handle_github_callback(params)
        if path == "/integrations":
            return self.json(self.integrations_payload())
        if path == "/integrations/github/authorize":
            return self.handle_github_repository_authorize(params)
        if path == "/integrations/github/callback":
            return self.handle_github_repository_callback(params)
        if path == "/integrations/github/install/start":
            return self.handle_github_install_start(params)
        if path == "/integrations/github/manage/start":
            return self.handle_github_manage_start(params)
        if path == "/dev/magic-links" or path == "/auth/email/callback":
            return self.error(HTTPStatus.NOT_FOUND, "Route not found")
        if path == "/repositories":
            return self.json(self.repositories_payload())
        if path == "/scans":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing scans.")
            scans = filter_user_scan_payloads([scan_payload(scan) for scan in user_scans(session)], params)
            return self.json(paginated_response(scans, keys=("scans",), params=params))
        if len(segments) == 2 and segments[0] == "scans":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing scans.")
            return self.json(scan_payload(self.find_or_404(user_scans(session), segments[1], "Scan")))
        if path == "/issues":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing issues.")
            issue_payloads = filter_user_issue_payloads([issue_payload(issue) for issue in user_issues(session)], params)
            return self.json(paginated_response(issue_payloads, keys=("issues",), params=params))
        if len(segments) == 2 and segments[0] == "issues":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing issues.")
            return self.json(issue_payload(self.find_or_404(user_issues(session), segments[1], "Issue")))
        if path == "/settings":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing settings.")
            return self.json(settings_payload(session["userId"]))
        if path == "/billing/plan":
            session = self.current_session()
            user = USERS.get(session["userId"]) if session else None
            return self.json(pricing_payload(user))
        if path == "/billing":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing billing.")
            return self.json(billing_page_payload(USERS[session["userId"]]))
        # Static file serving + SPA fallback for client-side routing
        root = web_root()
        if os.path.isdir(root):
            # Try to serve the exact file
            rel = path.lstrip("/")
            candidate = os.path.normpath(os.path.join(root, rel))
            # Prevent path traversal
            if candidate.startswith(os.path.normpath(root)) and os.path.isfile(candidate):
                return self.serve_static_file(candidate)
            # SPA fallback: serve index.html for any other GET
            return self.serve_spa()
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_post(self, path: str, params: dict, segments: list[str]) -> None:
        if path == "/webhooks/stripe":
            return self.handle_stripe_webhook()
        if path == "/webhooks/creem":
            return self.handle_creem_webhook()
        body = self.read_json()
        api_segments = external_api_segments(segments)
        if api_segments is not None:
            return self.handle_external_api_post(api_segments, body)
        if segments and segments[0] == "worker":
            return self.handle_worker_post(segments, body)
        if segments and segments[0] == "admin":
            return self.handle_admin_post(segments, body)
        if path == "/auth/sign-out":
            self.clear_current_session()
            return self.json({"ok": True}, headers={"Set-Cookie": clear_cookie_header()})
        if path == "/api-keys":
            return self.handle_api_keys_post(body)
        if (
            len(segments) == 5
            and segments[0] == "integrations"
            and segments[1] == "github"
            and segments[2] == "installations"
            and segments[4] == "manage-sessions"
        ):
            return self.handle_github_installation_manage_session(segments[3], body)
        if path == "/repositories/sync":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before syncing repositories.")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            installation_id = clean_github_access_text(body.get("installationId"), allow_int=True)
            github_identity_id = clean_github_access_text(body.get("githubIdentityId"))
            user = USERS.get(session["userId"])
            if not user:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before syncing repositories.")
            if installation_id or github_identity_id:
                if not installation_id:
                    return self.error(HTTPStatus.BAD_REQUEST, "installationId is required for scoped repository sync.")
                sync_github_repository_installation_scope(
                    user,
                    installation_id,
                    github_identity_id=github_identity_id,
                )
                payload = self.repositories_payload(refresh=False)
            else:
                payload = self.repositories_payload(
                    refresh=repository_sync_should_refresh(
                        user,
                        user.get("githubRepositoryAccess"),
                        body,
                    )
                )
            payload.update({"ok": True, "syncedAt": now()})
            return self.json(payload)
        if path == "/scans":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before starting a scan.")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            requested_repo_id = clean_github_access_text(body.get("repoId"), allow_int=True)
            requested_repository = clean_repository_full_name(body.get("repo"))
            if not requested_repo_id and not requested_repository:
                return self.error(HTTPStatus.BAD_REQUEST, "A repository is required to start a scan.")
            repository = requested_repository or requested_repo_id or ""
            if review.selected_provider() == "disabled":
                return self.error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "Code review provider is not configured. Set PULLWISE_REVIEW_PROVIDER to claude_code or codex for real scans. Use mock only for explicit local wire-up.",
                )
            request_id = scan_request_id_from_body(body)
            scan_error: tuple[int, str] | None = None
            scan_error_code: str | None = None
            scan_error_repo_id: str | None = None
            scan = None
            scan_created = False
            with STATE_LOCK:
                user = USERS.get(session["userId"]) or {}
                github_access = user.get("githubRepositoryAccess")
                if not github_access:
                    scan_error = (HTTPStatus.FORBIDDEN, "Authorize GitHub repositories before starting a scan.")
                elif github_repository_authorization_pending(user):
                    scan_error = (HTTPStatus.FORBIDDEN, "Complete GitHub repository authorization before starting a scan.")
                elif not github_repository_access_authorized_for_user(user, github_access):
                    scan_error = (HTTPStatus.FORBIDDEN, "Authorize GitHub repositories before starting a scan.")
                elif github_repositories_need_sync(github_access):
                    scan_error = (HTTPStatus.FORBIDDEN, "Sync GitHub repositories before starting a scan.")
                    scan_error_code = "REPOSITORY_SYNC_REQUIRED"
                else:
                    scan = user_scan_by_request_id(session["userId"], request_id)
                    if scan is not None and not scan_matches_requested_repository(
                        scan,
                        requested_repo_id=requested_repo_id,
                        requested_repository=requested_repository,
                    ):
                        scan_error = (HTTPStatus.CONFLICT, IDEMPOTENCY_KEY_REUSED_MESSAGE)
                        scan_error_code = "IDEMPOTENCY_KEY_REUSED"
                        scan_error_repo_id = clean_github_access_text(scan.get("repoId"), allow_int=True)
                        scan = None
                    elif scan is None:
                        limit_error = scan_queue_limit_error(session["userId"])
                        if limit_error:
                            scan_error = (limit_error[0], limit_error[1])
                            scan_error_code = limit_error[2]
                        if scan_error is not None:
                            pass
                        else:
                            repo_meta, request_key = repository_item_for_scan_request(github_access, body)
                            if not repo_meta:
                                scan_error = (HTTPStatus.FORBIDDEN, "Repository is not authorized for this GitHub App installation.")
                                scan_error_code = "REPOSITORY_NOT_AUTHORIZED"
                            else:
                                repository = clean_repository_full_name(repo_meta.get("fullName"), requested_repository)
                                if not repository:
                                    scan_error = (HTTPStatus.FORBIDDEN, "Repository is not authorized for this GitHub App installation.")
                                    scan_error_code = "REPOSITORY_NOT_AUTHORIZED"
                                elif request_key != "repoId" and not repository_is_authorized(github_access, repository):
                                    scan_error = (HTTPStatus.FORBIDDEN, "Repository is not authorized for this GitHub App installation.")
                                    scan_error_code = "REPOSITORY_NOT_AUTHORIZED"
                        if scan_error is None:
                            scan_id = make_id("sc")
                            try:
                                scan_user, repository_record = scan_resource_context(user, github_access, repo_meta)
                            except ValueError as exc:
                                code = str(exc)
                                if code == "REPOSITORY_SYNC_REQUIRED":
                                    scan_error = (
                                        HTTPStatus.CONFLICT,
                                        "Sync GitHub repositories before starting a scan so Pullwise can verify the stable repository ID.",
                                    )
                                    scan_error_code = "REPOSITORY_SYNC_REQUIRED"
                                else:
                                    scan_error = (HTTPStatus.BAD_REQUEST, "Unable to resolve repository context.")
                            else:
                                try:
                                    quota_result = quota.consume_scan_quota(
                                        user=scan_user,
                                        repository=repository_record,
                                        requested_by_user_id=session["userId"],
                                        scan_id=scan_id,
                                        request_id=request_id or None,
                                    )
                                except quota.QuotaExceeded as exc:
                                    scan_error = (HTTPStatus.PAYMENT_REQUIRED, exc.message)
                                    scan_error_code = exc.code
                                    scan_error_repo_id = exc.repo_id
                                else:
                                    entitlement = quota_result["user"]
                        if scan_error:
                            pass
                        else:
                            branch = (
                                clean_github_access_text(body.get("branch"))
                                or clean_github_access_text(repo_meta.get("defaultBranch"))
                                or "main"
                            )
                            scan = {
                                "id": scan_id,
                                "repo": repository,
                                "branch": branch,
                                "commit": clean_github_access_text(body.get("commit")) or "pending",
                                "status": "queued",
                                "userId": session["userId"],
                                "createdAt": now(),
                                "queuedAt": now(),
                                "progress": 0,
                                "phase": None,
                                "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                                "installationId": (
                                    clean_github_access_text(repo_meta.get("installationId"), allow_int=True)
                                    or clean_github_access_text(github_access.get("installationId"), allow_int=True)
                                ),
                                "installationAccount": (
                                    clean_github_access_text(repo_meta.get("installationAccount"))
                                    or clean_github_access_text(github_access.get("installationAccount"))
                                ),
                                "repositorySelection": (
                                    clean_github_access_text(repo_meta.get("repositorySelection"))
                                    or clean_github_access_text(github_access.get("repositorySelection"))
                                ),
                                "repoId": repository_record["id"],
                                "githubRepoId": repository_record["github_repo_id"],
                                "quotaBucketIds": quota_result["bucketIds"],
                                "cloneUrl": repo_meta.get("cloneUrl"),
                                "repositoryPrivate": bool(repo_meta.get("private")),
                                "repoPath": None,
                                "billingUsage": quota_result["user"],
                                "repoUsage": quota_result["repository"],
                                "by": "you",
                            }
                            if request_id:
                                scan["requestId"] = request_id
                            create_scan_job_for_scan(scan)
                            SCANS.insert(0, scan)
                            scan_created = True
                            mark_state_dirty()

            if scan_error:
                scan_logging.log_event(
                    "scan_create_rejected",
                    userId=session["userId"],
                    repo=repository,
                    provider=review.selected_provider(),
                    httpStatus=int(scan_error[0]),
                    reason=scan_error[1],
                    requestId=request_id or None,
                    code=scan_error_code,
                    repoId=scan_error_repo_id,
                )
                payload = {"message": scan_error[1]}
                if scan_error_code:
                    payload["code"] = scan_error_code
                if scan_error_repo_id:
                    payload["repoId"] = scan_error_repo_id
                return self.json(payload, scan_error[0])
            if scan is None:
                return self.error(HTTPStatus.INTERNAL_SERVER_ERROR, "Unable to create scan.")
            if scan_created:
                scan_logging.log_event(
                    "scan_queued",
                    scanId=scan["id"],
                    userId=scan.get("userId"),
                    repo=scan.get("repo"),
                    branch=scan.get("branch"),
                    commit=scan.get("commit"),
                    provider=review.selected_provider(),
                    requestId=scan.get("requestId"),
                    installationId=scan.get("installationId"),
                    repoId=scan.get("repoId"),
                    githubRepoId=scan.get("githubRepoId"),
                    quotaBucketIds=scan.get("quotaBucketIds"),
                )
                worker.notify_queue_changed()
            else:
                scan_logging.log_event(
                    "scan_request_reused",
                    scanId=scan.get("id"),
                    userId=scan.get("userId"),
                    repo=scan.get("repo"),
                    branch=scan.get("branch"),
                    commit=scan.get("commit"),
                    provider=review.selected_provider(),
                    requestId=request_id or None,
                    status=scan.get("status"),
                )
            return self.json(scan_payload(scan), HTTPStatus.CREATED if scan_created else HTTPStatus.OK)
        if len(segments) == 3 and segments[0] == "scans" and segments[2] == "cancel":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before cancelling a scan.")
            with STATE_LOCK:
                scan = self.find_or_404(user_scans(session), segments[1], "Scan")
                if scan.get("status") not in {"queued", "running"}:
                    return self.error(HTTPStatus.CONFLICT, "Only queued or running scans can be cancelled.")
                scan["status"] = "cancelled"
                scan["completedAt"] = now()
                mark_state_dirty()
                db.cancel_scan_job_for_scan(str(scan.get("id") or ""))
            worker.notify_queue_changed()
            return self.json(scan_payload(scan))
        if len(segments) == 4 and segments[0] == "issues" and segments[2] == "fixes" and segments[3] == "preview":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before previewing fixes.")
            issue = self.find_or_404(user_issues(session), segments[1], "Issue")
            try:
                preview = preview_issue_fix_for_user(USERS[session["userId"]], issue)
            except ValueError as exc:
                return self.error(HTTPStatus.BAD_REQUEST, str(exc))
            return self.json(preview, HTTPStatus.OK if preview.get("valid") else HTTPStatus.BAD_REQUEST)
        if len(segments) == 4 and segments[0] == "issues" and segments[2] == "fixes" and segments[3] == "apply":
            return self.error(HTTPStatus.NOT_IMPLEMENTED, "Applying fixes is not implemented on this backend.")
        if len(segments) == 3 and segments[0] == "issues" and segments[2] == "pull-requests":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before creating pull requests.")
            issue = next((item for item in user_issues(session) if item.get("id") == segments[1]), None)
            if not issue:
                return self.error(HTTPStatus.NOT_FOUND, "Issue not found.")
            try:
                pull_request = create_issue_pull_request(USERS[session["userId"]], issue)
            except github_auth.GitHubError as exc:
                return self.error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
            except ValueError as exc:
                return self.error(HTTPStatus.BAD_REQUEST, str(exc))
            return self.json(pull_request)
        if len(segments) == 2 and segments[0] == "integrations":
            return self.error(HTTPStatus.NOT_IMPLEMENTED, f"{segments[1]} integration writes are not implemented on this backend.")
        if path == "/billing/checkout-sessions":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before starting checkout.")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            user = USERS[session["userId"]]
            checkout = billing.create_checkout_session(
                user,
                success_url=safe_redirect_to(body.get("successUrl"), "settings"),
                cancel_url=safe_redirect_to(body.get("cancelUrl"), "settings"),
                plan=str(body.get("plan") or "pro"),
                interval=str(body.get("interval") or "month"),
            )
            checkout = safe_billing_redirect_response(checkout, "Checkout", require_url=True)
            if checkout.get("customerId"):
                current_billing = user.get("billing") or {}
                user["billing"] = {
                    **current_billing,
                    "provider": checkout.get("provider") or current_billing.get("provider"),
                    "customerId": checkout.get("customerId"),
                }
            user["billingCheckout"] = {
                "provider": checkout.get("provider"),
                "id": checkout.get("id"),
                "requestId": checkout.get("requestId"),
                "plan": checkout.get("plan"),
                "interval": checkout.get("interval"),
                "createdAt": now(),
            }
            mark_state_dirty()
            return self.json(checkout)
        if path == "/billing/portal-sessions":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before opening the billing portal.")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            user = USERS[session["userId"]]
            portal = billing.create_portal_session(
                user,
                return_url=safe_redirect_to(body.get("returnUrl"), "settings"),
            )
            portal = safe_billing_redirect_response(portal, "portal", require_url=True)
            return self.json(portal)
        if path == "/billing/change-interval":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before changing your subscription.")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            user = USERS[session["userId"]]
            result = billing.change_subscription_interval(
                user,
                interval=str(body.get("interval") or "year"),
                return_url=safe_redirect_to(body.get("returnUrl"), "billing"),
            )
            result = safe_billing_redirect_response(result, "portal")
            if result.get("alreadyActive"):
                return self.json(result)
            if result.get("provider") == "creem" and result.get("interval") == "year":
                current_billing = user.get("billing") or {}
                user["billing"] = {
                    **current_billing,
                    "provider": "creem",
                    "subscriptionId": result.get("subscriptionId") or current_billing.get("subscriptionId"),
                    "status": result.get("status") or current_billing.get("status") or "active",
                    "plan": "pro",
                    "interval": "year",
                }
                mark_state_dirty()
            return self.json(result)
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_patch(self, segments: list[str]) -> None:
        body = self.read_json()
        if segments and segments[0] == "admin":
            return self.handle_admin_patch(segments, body)
        if len(segments) == 3 and segments[0] == "issues" and segments[2] == "status":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before updating issue status.")
            issue = self.find_or_404(user_issues(session), segments[1], "Issue")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            next_status = str(body.get("status") or issue["status"]).strip().lower()
            if next_status not in ISSUE_STATUSES:
                return self.error(HTTPStatus.BAD_REQUEST, "Issue status must be open, fixed, or snoozed.")
            issue["status"] = next_status
            mark_state_dirty()
            return self.json(issue_payload(issue))
        if len(segments) == 1 and segments[0] == "settings":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before updating settings.")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            return self.json(apply_settings_update(session["userId"], body))
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_delete(self, segments: list[str]) -> None:
        if segments and segments[0] == "admin":
            return self.handle_admin_delete(segments)
        if len(segments) == 2 and segments[0] == "api-keys":
            return self.handle_api_key_delete(segments[1])
        if len(segments) == 2 and segments[0] == "integrations":
            session = self.current_session()
            if segments[1] != "github":
                return self.error(HTTPStatus.NOT_IMPLEMENTED, f"{segments[1]} integration disconnect is not implemented on this backend.")
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before disconnecting GitHub.")
            USERS[session["userId"]]["githubRepositoryAccess"] = None
            USERS[session["userId"]].pop("githubRepositoryAccessPending", None)
            mark_state_dirty()
            return self.json({"ok": True, "provider": "github", "connected": False})
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_github_authorize(self, params: dict) -> None:
        redirect_to = safe_redirect_to(params.get("redirectTo"), "dashboard")
        if not github_auth.oauth_configured():
            if not local_github_mocks_enabled():
                return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub OAuth is not configured. Set PULLWISE_GITHUB_CLIENT_ID and PULLWISE_GITHUB_CLIENT_SECRET.")
            callback = f"{api_base_url(self)}/auth/github/callback?{urlencode({'redirectTo': redirect_to})}"
            return self.json({"url": callback, "mode": "local"})

        verifier = github_auth.make_code_verifier()
        state = remember_github_state("login", redirect_to, codeVerifier=verifier)
        callback_url = f"{api_base_url(self)}/auth/github/callback"
        authorize_url = github_auth.build_oauth_authorize_url(
            callback_url,
            state,
            verifier,
        )
        return self.json({"url": authorize_url, "mode": "github"})

    def handle_github_callback(self, params: dict) -> None:
        if not github_auth.oauth_configured():
            if not local_github_mocks_enabled():
                return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub OAuth is not configured.")
            user = get_or_create_github_user()
            session = create_session(user)
            return self.redirect(safe_redirect_to(params.get("redirectTo"), "dashboard"), cookie_header(session["id"]))

        state = params.get("state") or ""
        record = pop_any_github_state(state)
        if record.get("kind") == "manage_installation":
            return self.handle_github_manage_callback(params, record, state)
        if record.get("kind") == "install_identity":
            return self.handle_github_install_identity_callback(params, record, state)
        if record.get("kind") != "login":
            raise ValueError("GitHub authorization state is invalid or expired.")
        redirect_to = str(record["redirectTo"])
        if params.get("error"):
            return self.redirect(redirect_with_params(redirect_to, {"github_error": params.get("error_description") or params["error"]}))
        if not params.get("code"):
            return self.redirect(redirect_with_params(redirect_to, {"github_error": "missing_oauth_code"}))

        token_payload = github_auth.exchange_oauth_code(
            params["code"],
            f"{api_base_url(self)}/auth/github/callback",
            str(record.get("codeVerifier") or ""),
            state,
        )
        profile = github_auth.fetch_user_profile(token_payload["access_token"])
        user = get_or_create_real_github_user(profile, token_payload)
        session = create_session(user)
        return self.redirect(redirect_to, cookie_header(session["id"]))

    def handle_github_installation_manage_session(self, installation_id: str, body: dict) -> None:
        session = self.current_session()
        if not session:
            return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before managing GitHub installations.")
        if not isinstance(body, dict):
            return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        if not github_auth.oauth_configured():
            return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub OAuth is not configured. Set PULLWISE_GITHUB_CLIENT_ID and PULLWISE_GITHUB_CLIENT_SECRET.")
        if not github_auth.app_install_configured():
            return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub App installation is not configured. Set PULLWISE_GITHUB_APP_SLUG or PULLWISE_GITHUB_APP_INSTALL_URL.")

        user = USERS.get(session["userId"])
        github_access = user.get("githubRepositoryAccess") if user else None
        if not github_repository_access_authorized_for_user(user, github_access) or not github_repository_access_connected(github_access):
            return self.error(HTTPStatus.FORBIDDEN, "Connect GitHub repositories before managing an installation.")

        clean_installation_id = clean_github_access_text(installation_id, allow_int=True)
        if not clean_installation_id:
            return self.error(HTTPStatus.BAD_REQUEST, "A GitHub App installation id is required.")
        installation = installation_summary_by_id(github_access, clean_installation_id)
        if not installation:
            return self.error(HTTPStatus.NOT_FOUND, "GitHub App installation is not connected to this Pullwise account.")

        identity_id = clean_github_access_text(body.get("githubIdentityId"))
        if identity_id and not github_identity_by_id(user, identity_id):
            return self.error(HTTPStatus.BAD_REQUEST, "GitHub identity is not linked to this Pullwise account.")

        redirect_to = safe_redirect_to(body.get("returnUrl") or body.get("redirectTo"), "repos")
        state = remember_github_installation_manage_state(
            user,
            installation,
            redirect_to,
            expected_github_identity_id=identity_id,
        )
        url = f"{api_base_url(self)}/integrations/github/manage/start?{urlencode({'state': state})}"
        return self.json({
            "mode": "github-installation-manage",
            "url": url,
            "installationId": clean_installation_id,
        })

    def handle_github_install_start(self, params: dict) -> None:
        state = params.get("state") or ""
        record = peek_github_state("install_identity", state)
        if not github_auth.oauth_configured():
            return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub OAuth is not configured. Set PULLWISE_GITHUB_CLIENT_ID and PULLWISE_GITHUB_CLIENT_SECRET.")
        verifier = github_auth.make_code_verifier()
        record["codeVerifier"] = verifier
        record["oauthStartedAt"] = now()
        mark_state_dirty()
        authorize_url = github_auth.build_oauth_authorize_url(
            f"{api_base_url(self)}/auth/github/callback",
            state,
            verifier,
            prompt="select_account",
        )
        return self.redirect(authorize_url)

    def handle_github_install_identity_callback(self, params: dict, record: dict, state: str) -> None:
        redirect_to = str(record["redirectTo"])
        user = USERS.get(str(record.get("userId") or ""))
        if not user:
            raise ValueError("The GitHub installation identity session belongs to a user session that no longer exists.")
        if params.get("error"):
            clear_github_repository_authorization_pending(user, state)
            return self.redirect(redirect_with_params(redirect_to, {"github_error": params.get("error_description") or params["error"]}))
        if not params.get("code"):
            clear_github_repository_authorization_pending(user, state)
            return self.redirect(redirect_with_params(redirect_to, {"github_error": "missing_oauth_code"}))

        token_payload = github_auth.exchange_oauth_code(
            params["code"],
            f"{api_base_url(self)}/auth/github/callback",
            str(record.get("codeVerifier") or ""),
            state,
        )
        profile = github_auth.fetch_user_profile(token_payload["access_token"])
        identity = upsert_github_identity(user, profile, token_payload)
        install_state = remember_github_repository_authorization(
            user,
            redirect_to,
            str(record.get("requestedScope") or "selected"),
            manage=record.get("manage") is True,
            selected_github_identity_id=clean_github_access_text(identity.get("id")),
        )
        return self.redirect(github_auth.build_app_install_url(install_state))

    def handle_github_manage_start(self, params: dict) -> None:
        state = params.get("state") or ""
        record = peek_github_state("manage_installation", state)
        if not github_auth.oauth_configured():
            return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub OAuth is not configured. Set PULLWISE_GITHUB_CLIENT_ID and PULLWISE_GITHUB_CLIENT_SECRET.")
        verifier = github_auth.make_code_verifier()
        record["codeVerifier"] = verifier
        record["oauthStartedAt"] = now()
        mark_state_dirty()
        authorize_url = github_auth.build_oauth_authorize_url(
            f"{api_base_url(self)}/auth/github/callback",
            state,
            verifier,
            prompt="select_account",
        )
        return self.redirect(authorize_url)

    def handle_github_manage_callback(self, params: dict, record: dict, state: str) -> None:
        redirect_to = str(record["redirectTo"])
        if params.get("error"):
            return self.redirect(redirect_with_params(redirect_to, {"github_error": params.get("error_description") or params["error"]}))
        if not params.get("code"):
            return self.redirect(redirect_with_params(redirect_to, {"github_error": "missing_oauth_code"}))

        user = USERS.get(str(record.get("userId") or ""))
        if not user:
            raise ValueError("The GitHub manage session belongs to a user session that no longer exists.")
        token_payload = github_auth.exchange_oauth_code(
            params["code"],
            f"{api_base_url(self)}/auth/github/callback",
            str(record.get("codeVerifier") or ""),
            state,
        )
        profile = github_auth.fetch_user_profile(token_payload["access_token"])
        identity = upsert_github_identity(user, profile, token_payload)
        expected_installation_id = clean_github_access_text(record.get("expectedInstallationId"), allow_int=True)
        if not expected_installation_id:
            return self.redirect(redirect_with_params(redirect_to, {"github_error": "github_installation_not_visible"}))

        if self.github_manage_identity_mismatch(identity, record):
            upsert_github_identity_installation_access(
                user,
                identity,
                expected_installation_id,
                can_access=False,
                last_error_code="github_account_mismatch",
            )
            return self.redirect(self.github_manage_error_redirect(redirect_to, "github_account_mismatch", identity, record))

        try:
            installations = github_auth.list_current_app_installations_for_user(identity.get("accessToken"))
        except github_auth.GitHubError:
            identity["status"] = "needs_reauth"
            upsert_github_identity_installation_access(
                user,
                identity,
                expected_installation_id,
                can_access=False,
                last_error_code="github_identity_reauth_required",
            )
            return self.redirect(self.github_manage_error_redirect(redirect_to, "github_identity_reauth_required", identity, record))

        installation = next(
            (
                item
                for item in installations
                if str(item.get("id") or "") == str(expected_installation_id)
            ),
            None,
        )
        if not installation:
            upsert_github_identity_installation_access(
                user,
                identity,
                expected_installation_id,
                can_access=False,
                last_error_code="github_installation_not_visible",
            )
            return self.redirect(self.github_manage_error_redirect(redirect_to, "github_installation_not_visible", identity, record))
        if installation.get("suspended_at"):
            upsert_github_identity_installation_access(
                user,
                identity,
                expected_installation_id,
                can_access=False,
                last_error_code="github_installation_deleted",
            )
            return self.redirect(self.github_manage_error_redirect(redirect_to, "github_installation_deleted", identity, record))

        html_url = trusted_github_web_url(installation.get("html_url") or record.get("expectedInstallationHtmlUrl"))
        if not html_url:
            upsert_github_identity_installation_access(
                user,
                identity,
                expected_installation_id,
                can_access=False,
                last_error_code="github_installation_not_visible",
            )
            return self.redirect(self.github_manage_error_redirect(redirect_to, "github_installation_not_visible", identity, record))

        upsert_github_identity_installation_access(
            user,
            identity,
            expected_installation_id,
            can_access=True,
        )
        return self.redirect(html_url)

    def github_manage_identity_mismatch(self, identity: dict, record: dict) -> bool:
        expected_identity_id = clean_github_access_text(record.get("expectedGithubIdentityId"))
        if expected_identity_id and identity.get("id") != expected_identity_id:
            return True
        expected_target_type = str(record.get("expectedInstallationTargetType") or "").casefold()
        expected_account = str(record.get("expectedAccountLogin") or "").casefold()
        selected_login = str(identity.get("githubLogin") or identity.get("login") or "").casefold()
        return expected_target_type == "user" and expected_account and selected_login and selected_login != expected_account

    def github_manage_error_redirect(self, redirect_to: str, code: str, identity: dict, record: dict) -> str:
        return redirect_with_params(
            redirect_to,
            {
                "github_error": code,
                "github_login": clean_github_access_text(identity.get("githubLogin") or identity.get("login")) or "",
                "installation_account": clean_github_access_text(record.get("expectedAccountLogin")) or "",
            },
        )

    def handle_github_repository_authorize(self, params: dict) -> None:
        scope = params.get("scope") if params.get("scope") in {"all", "selected"} else "all"
        manage = str(params.get("manage") or "").lower() in {"1", "true", "yes", "on"}
        add_installation = str(params.get("add") or "").lower() in {"1", "true", "yes", "on"}
        redirect_to = safe_redirect_to(params.get("redirectTo"), "repos")
        if not github_auth.app_install_configured():
            if not local_github_mocks_enabled():
                return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub App installation is not configured. Set PULLWISE_GITHUB_APP_SLUG or PULLWISE_GITHUB_APP_INSTALL_URL.")
            callback = f"{api_base_url(self)}/integrations/github/callback?{urlencode({'scope': scope, 'redirectTo': redirect_to})}"
            return self.json({"url": callback, "mode": "local"})

        session = self.current_session()
        if not session:
            return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before authorizing GitHub repositories.")
        user = USERS.get(session["userId"])
        if not has_github_repository_authorization_identity(user):
            return self.error(HTTPStatus.UNAUTHORIZED, "Sign in with GitHub before authorizing repositories.")
        if github_auth.app_visibility_check_enabled():
            if not github_auth.app_slug():
                return self.error(
                    HTTPStatus.NOT_IMPLEMENTED,
                    "PULLWISE_GITHUB_APP_SLUG is required for user repository installs so Pullwise can verify the GitHub App is public.",
                )
            public_installable = github_auth.app_slug_publicly_installable()
            if public_installable is False:
                return self.error(
                    HTTPStatus.CONFLICT,
                    (
                        f"GitHub App '{github_auth.app_slug()}' is private or not publicly visible. "
                        "Make the GitHub App public before connecting repositories from user accounts, "
                        "and keep PULLWISE_GITHUB_APP_VISIBILITY_CHECK enabled for user repository installs."
                    ),
                )
            if public_installable is None:
                return self.error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    (
                        f"Unable to verify GitHub App '{github_auth.app_slug()}' is public before repository authorization. "
                        "Try again after GitHub API access is available, and keep PULLWISE_GITHUB_APP_VISIBILITY_CHECK enabled for user repository installs."
                    ),
                )

        existing_access = try_bind_existing_github_repository_access(user)
        if add_installation:
            state = remember_github_repository_identity_authorization(user, redirect_to, scope, add=True)
            url = f"{api_base_url(self)}/integrations/github/install/start?{urlencode({'state': state})}"
            return self.json({"url": url, "mode": "github-app-add"})

        if manage:
            existing_installations = installation_summaries_for_access(existing_access)
            if github_repository_access_connected(existing_access) and len(existing_installations) == 1:
                installation = existing_installations[0]
                installation_id = clean_github_access_text(installation.get("installationId"), allow_int=True)
                state = remember_github_installation_manage_state(user, installation, redirect_to)
                url = f"{api_base_url(self)}/integrations/github/manage/start?{urlencode({'state': state})}"
                return self.json({
                    "ok": True,
                    "connected": True,
                    "url": url,
                    "mode": "github-installation-manage",
                    "installationId": installation_id,
                })
            if github_repository_access_connected(existing_access) and existing_installations:
                return self.json({
                    "ok": True,
                    "connected": True,
                    "mode": "github-app-existing-manage-list",
                    "installationId": clean_github_access_text(existing_access.get("installationId"), allow_int=True),
                    "installationIds": clean_github_access_text_list(existing_access.get("installationIds"), allow_int=True),
                    "installationAccount": clean_github_access_text(existing_access.get("installationAccount")),
                    "installationAccounts": clean_github_access_text_list(existing_access.get("installationAccounts")),
                    "installations": public_installation_summaries(user, existing_access),
                    "identities": public_github_identities(user),
                })
            state = remember_github_repository_authorization(user, redirect_to, scope, manage=True)
            return self.json({"url": github_auth.build_app_install_url(state), "mode": "github-app"})

        if github_repository_access_connected(existing_access):
            payload = {
                "ok": True,
                "connected": True,
                "mode": "github-app-existing",
                "installationId": clean_github_access_text(existing_access.get("installationId"), allow_int=True),
            }
            return self.json(payload)
        existing_url = trusted_github_web_url(existing_access.get("installationHtmlUrl") if existing_access else None)
        if existing_access and existing_url:
            return self.json({
                "ok": True,
                "url": existing_url,
                "mode": "github-app-existing-pending",
                "installationId": existing_access.get("installationId"),
            })

        state = remember_github_repository_authorization(user, redirect_to, scope)
        return self.json({"url": github_auth.build_app_install_url(state), "mode": "github-app"})

    def handle_github_repository_callback(self, params: dict) -> None:
        if not github_auth.app_install_configured():
            if not local_github_mocks_enabled():
                return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub App installation is not configured.")
            session = self.current_or_demo_session()
            scope = params.get("scope") or "all"
            repository_items = REPOSITORIES if scope == "all" else REPOSITORIES[:1]
            USERS[session["userId"]]["githubRepositoryAccess"] = {
                "mode": "local",
                "scope": scope,
                "authorizedAt": now(),
                "installationId": "dev_installation_1",
                "repositories": [repo["fullName"] for repo in repository_items],
                "repositoryItems": repository_items,
                "repositoriesNeedSync": True,
            }
            mark_state_dirty()
            return self.redirect(safe_redirect_to(params.get("redirectTo"), "repos"), cookie_header(session["id"]))

        record = self.github_install_record_from_callback(params)
        user = USERS.get(str(record["userId"]))
        if not user:
            raise ValueError("The GitHub installation belongs to a user session that no longer exists.")
        if not has_github_repository_authorization_identity(user):
            raise ValueError("Sign in with GitHub before authorizing repositories.")
        state = params.get("state") or None
        if params.get("setup_action") == "request":
            clear_github_repository_authorization_pending(user, state)
            return self.redirect(
                redirect_with_params(str(record["redirectTo"]), {"github_error": "github_app_installation_not_completed"})
            )
        if not params.get("installation_id"):
            clear_github_repository_authorization_pending(user, state)
            return self.redirect(
                redirect_with_params(str(record["redirectTo"]), {"github_error": "missing_installation_id"})
            )

        installation_id = str(params["installation_id"])
        selected_identity = github_identity_by_id(
            user,
            clean_github_access_text(record.get("selectedGithubIdentityId")),
        )
        selected_token = selected_identity.get("accessToken") if selected_identity else user.get("githubAccessToken")
        installations = (
            [
                installation
                for installation in github_auth.list_current_app_installations_for_user(selected_token)
                if installation_allowed_for_identity(selected_identity, installation)
            ]
            if selected_identity
            else current_user_github_app_installations(user)
        )
        target_installation = next(
            (
                installation
                for installation in installations
                if str(installation.get("id") or "") == installation_id
            ),
            None,
        )
        if not target_installation:
            if selected_identity:
                upsert_github_identity_installation_access(
                    user,
                    selected_identity,
                    installation_id,
                    can_access=False,
                    last_error_code="github_installation_not_visible",
                )
            raise ValueError("Unable to verify this GitHub App installation belongs to the signed-in GitHub user.")

        requested_scope = params.get("scope") or record.get("requestedScope") or "selected"
        if selected_identity:
            bind_github_repository_installation_for_identity(
                user,
                target_installation,
                selected_token,
                requested_scope,
            )
            identity = selected_identity
        else:
            bind_github_repository_installations(
                user,
                installations,
                requested_scope,
            )
            identity = upsert_github_identity(
                user,
                {
                    "id": user.get("githubId"),
                    "login": user.get("githubLogin"),
                    "html_url": user.get("githubHtmlUrl"),
                    "avatar_url": user.get("avatarUrl"),
                },
                {
                    "access_token": user.get("githubAccessToken"),
                    "scope": user.get("githubOAuthScope"),
                },
            )
        upsert_github_identity_installation_access(
            user,
            identity,
            installation_id,
            can_access=True,
            verification_method="setup_callback",
        )
        clear_github_repository_authorization_pending(user, state)
        session = create_session(user)
        return self.redirect(str(record["redirectTo"]), cookie_header(session["id"]))

    def github_install_record_from_callback(self, params: dict) -> dict:
        state = params.get("state") or ""
        if not state:
            raise ValueError("GitHub authorization state is invalid or expired.")
        return pop_github_state("install", state)

    def integrations_payload(self) -> dict:
        session = self.current_session()
        user = USERS.get(session["userId"]) if session else None
        github_access = user.get("githubRepositoryAccess") if user else None
        pending = bool(github_repository_authorization_pending(user))
        visible_access = None if pending or not github_repository_access_authorized_for_user(user, github_access) else github_access
        github = {
            "provider": "github",
            "connected": github_repository_access_authorized_for_user(user, github_access)
            and github_repository_access_connected(github_access)
            and not pending,
            "authorizationPending": pending,
            "mode": clean_github_access_text(visible_access.get("mode")) if visible_access else None,
            "scope": clean_github_access_text(visible_access.get("scope")) if visible_access else None,
            "repositorySelection": clean_github_access_text(visible_access.get("repositorySelection")) if visible_access else None,
            "installationId": clean_github_access_text(visible_access.get("installationId"), allow_int=True) if visible_access else None,
            "installationIds": clean_github_access_text_list(visible_access.get("installationIds"), allow_int=True) if visible_access else [],
            "installationAccount": clean_github_access_text(visible_access.get("installationAccount")) if visible_access else None,
            "installationAccounts": clean_github_access_text_list(visible_access.get("installationAccounts")) if visible_access else [],
            "installationHtmlUrl": None,
            "identities": public_github_identities(user),
            "installations": public_installation_summaries(user, visible_access),
            "repositories": clean_github_access_text_list(visible_access.get("repositories")) if visible_access else [],
            "repositoriesNeedSync": github_repositories_need_sync(visible_access),
        }
        items = [github]
        return {"items": items, "github": github}

    def repositories_payload(self, refresh: bool = False) -> dict:
        session = self.current_session()
        if not session:
            return {"items": [], "repositories": [], "needsAuthorization": True}

        user = USERS.get(session["userId"])
        github_access = user.get("githubRepositoryAccess") if user else None
        bound_existing_access = False
        pending = bool(github_repository_authorization_pending(user))
        if pending:
            if not refresh:
                return pending_repositories_payload()
            github_access = (
                bind_pending_selected_github_identity_access(user)
                or try_bind_existing_github_repository_access(user, force_refresh=True)
            )
            if github_repository_access_connected(github_access):
                clear_github_repository_authorization_pending(user)
                pending = False
                bound_existing_access = True
            else:
                return pending_repositories_payload()

        if github_access and not github_repository_access_authorized_for_user(user, github_access):
            github_access = try_bind_existing_github_repository_access(user, force_refresh=True)
            bound_existing_access = bool(github_access)

        if not github_access:
            github_access = try_bind_existing_github_repository_access(user)
            bound_existing_access = bool(github_access)
        if not github_access:
            return {"items": [], "repositories": [], "needsAuthorization": True}

        if refresh and not bound_existing_access and github_access.get("mode") == "github-app":
            refreshed_access = try_bind_existing_github_repository_access(user, force_refresh=True)
            if refreshed_access:
                github_access = refreshed_access
                bound_existing_access = True

        repository_items = repository_items_for_response(user, github_access)
        if not github_repository_access_connected(github_access):
            return unavailable_repositories_payload(github_access)

        return {
            "items": repository_items,
            "repositories": repository_items,
            "needsAuthorization": False,
            "installationId": clean_github_access_text(github_access.get("installationId"), allow_int=True),
            "installationIds": clean_github_access_text_list(github_access.get("installationIds"), allow_int=True),
            "repositorySelection": clean_github_access_text(github_access.get("repositorySelection")),
            "installationAccount": clean_github_access_text(github_access.get("installationAccount")),
            "installationAccounts": clean_github_access_text_list(github_access.get("installationAccounts")),
            "installations": public_installation_summaries(user, github_access),
            "repositoriesNeedSync": github_repositories_need_sync(github_access),
        }

    def repositories_connected(self) -> bool:
        session = self.current_session()
        if not session:
            return False
        return github_repositories_connected_for_user(USERS.get(session["userId"]))

    def current_or_demo_session(self) -> dict:
        session = self.current_session()
        if session:
            return session
        user = get_or_create_github_user()
        return create_session(user)

    def current_session(self) -> dict | None:
        session_id = self.current_session_id()
        if not session_id:
            return None
        session = SESSIONS.get(session_id)
        if not session:
            return None
        if not isinstance(session, dict):
            SESSIONS.pop(session_id, None)
            mark_state_dirty()
            return None
        expires_at = pull_request_timestamp(session.get("expiresAt"))
        user_id = session.get("userId")
        if expires_at is None or not isinstance(user_id, str) or not user_id:
            SESSIONS.pop(session_id, None)
            mark_state_dirty()
            return None
        if expires_at < now():
            SESSIONS.pop(session_id, None)
            mark_state_dirty()
            return None
        user = USERS.get(user_id)
        if not user:
            SESSIONS.pop(session_id, None)
            mark_state_dirty()
            return None
        if github_auth.oauth_configured() and user and "github" in user.get("providers", []) and not user.get("githubAccessToken"):
            SESSIONS.pop(session_id, None)
            mark_state_dirty()
            return None
        return session

    def current_session_id(self) -> str | None:
        authorization_token = bearer_token(self)
        if authorization_token and not authorization_token.startswith(API_KEY_PREFIX):
            return authorization_token
        raw_cookie = request_header(self, "Cookie") or ""
        cookie = SimpleCookie(raw_cookie)
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def current_api_key_context(self) -> dict | None:
        cached = getattr(self, "_api_key_context", None)
        if cached is not None:
            return cached
        token = api_key_token(self)
        if not token:
            self._api_key_context = None
            return None
        token_hash = api_key_hash(token)
        record = db.get_api_key_by_hash(token_hash)
        if not record:
            self._api_key_context = None
            return None
        user = USERS.get(str(record.get("user_id") or ""))
        if not user:
            self._api_key_context = None
            return None
        db.mark_api_key_used(record["id"])
        context = {"apiKey": record, "user": user, "scopes": parse_api_key_scopes(record.get("scopes"))}
        self._api_key_context = context
        return context

    def require_api_key_context(self, scope: str) -> dict | None:
        context = self.current_api_key_context()
        if not context:
            self.error(HTTPStatus.UNAUTHORIZED, "A valid Pullwise API key is required.")
            return None
        if scope not in context.get("scopes", []):
            self.error(HTTPStatus.FORBIDDEN, f"API key scope {scope} is required.")
            return None
        return context

    def api_repository_context(self, context: dict, repo_id: str) -> tuple[dict, dict] | None:
        repo_id = clean_github_access_text(repo_id, allow_int=True) or ""
        if not repo_id:
            self.error(HTTPStatus.BAD_REQUEST, "repoId is required.")
            return None
        user = context.get("user") if isinstance(context.get("user"), dict) else None
        github_access = user.get("githubRepositoryAccess") if user else None
        if user and isinstance(github_access, dict):
            sync_repository_access_for_user(user, github_access)
        repository = db.get_repository(repo_id)
        if not repository or not api_repository_authorized_for_user(user, repository):
            self.error(HTTPStatus.NOT_FOUND, "Repository is not authorized for this account.")
            return None
        repository_item_meta = (
            repository_item_by_repo_id(github_access, repo_id)
            or repository_item_by_repo_id(github_access, str(repository.get("id") or ""))
            or repository_item_by_repo_id(github_access, str(repository.get("github_repo_id") or ""))
            or repository_item(github_access, str(repository.get("full_name") or ""))
            or {}
        )
        return repository, repository_item_meta

    def handle_api_keys_get(self, params: dict) -> None:
        session = self.current_session()
        if not session:
            return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing API keys.")
        user = USERS[session["userId"]]
        keys = [api_key_public_payload(item) for item in db.list_api_keys_for_user(user["id"])]
        return self.json({"items": keys, "apiKeys": keys})

    def handle_api_keys_post(self, body: dict) -> None:
        session = self.current_session()
        if not session:
            return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before creating API keys.")
        if not isinstance(body, dict):
            return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        user = USERS[session["userId"]]
        scopes, scopes_error = requested_api_key_scopes(body.get("scopes"), provided="scopes" in body)
        if scopes_error:
            return self.error(HTTPStatus.BAD_REQUEST, scopes_error)
        token = API_KEY_PREFIX + secrets.token_urlsafe(32)
        record = db.create_api_key(
            {
                "id": make_id("ak"),
                "user_id": user["id"],
                "name": public_issue_text(body.get("name")) or "API key",
                "key_prefix": api_key_prefix(token),
                "key_hash": api_key_hash(token),
                "scopes": scopes,
            }
        )
        return self.json(api_key_public_payload(record, token=token), HTTPStatus.CREATED)

    def handle_api_key_delete(self, key_id: str) -> None:
        session = self.current_session()
        if not session:
            return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before revoking API keys.")
        if not db.revoke_api_key(key_id, session["userId"]):
            return self.error(HTTPStatus.NOT_FOUND, "API key not found.")
        return self.json({"ok": True, "id": public_issue_text(key_id), "revoked": True})

    def require_admin_session(self) -> dict | None:
        session = self.current_session()
        if not session:
            self.error(HTTPStatus.UNAUTHORIZED, "Sign in before using admin APIs.")
            return None
        user = USERS.get(session["userId"])
        if not user_is_admin(user):
            self.error(HTTPStatus.FORBIDDEN, "Admin access is required.")
            return None
        return session

    def audit_worker_action(
        self,
        session: dict,
        action: str,
        *,
        worker_id: str | None = None,
        changed_fields: dict | None = None,
        success: bool = True,
        error: str | None = None,
    ) -> None:
        db.record_worker_audit_event(
            {
                "actor_user_id": session.get("userId"),
                "action": action,
                "worker_id": worker_id,
                "changed_fields": changed_fields or {},
                "request_id": request_id_from_handler(self),
                "created_at": now(),
                "success": success,
                "error": clean_scan_error(error),
            }
        )

    def handle_admin_get(self, segments: list[str], params: dict) -> None:
        session = self.require_admin_session()
        if not session:
            return
        if segments == ["admin", "status"]:
            return self.json(scan_system_status_payload(admin=True))
        if segments == ["admin", "workers"]:
            workers = [worker_public_payload(worker, admin=True) for worker in db.list_workers()]
            return self.json({"items": workers, "workers": workers})
        if len(segments) == 3 and segments[:2] == ["admin", "workers"]:
            worker = db.get_worker(segments[2], include_deleted=True)
            if not worker:
                return self.error(HTTPStatus.NOT_FOUND, "Worker not found.")
            audit = db.list_worker_audit_events(segments[2], limit=50)
            return self.json({"worker": worker_public_payload(worker, admin=True), "auditEvents": audit})
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_admin_post(self, segments: list[str], body: dict) -> None:
        session = self.require_admin_session()
        if not session:
            return
        if not isinstance(body, dict):
            return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        if segments == ["admin", "workers"]:
            return self.handle_admin_worker_create(session, body)
        if len(segments) == 4 and segments[:2] == ["admin", "workers"]:
            worker_id = clean_github_access_text(segments[2]) or ""
            action = segments[3]
            if action == "commands":
                return self.handle_admin_worker_command(session, worker_id, body)
            if action == "enable":
                worker = db.set_worker_enabled(worker_id, True)
                if not worker:
                    self.audit_worker_action(session, "enable_worker", worker_id=worker_id, success=False, error="Worker not found.")
                    return self.error(HTTPStatus.NOT_FOUND, "Worker not found.")
                self.audit_worker_action(session, "enable_worker", worker_id=worker_id, changed_fields={"enabled": True})
                return self.json({"worker": worker_public_payload(worker, admin=True)})
            if action == "disable":
                worker = db.set_worker_enabled(worker_id, False)
                if not worker:
                    self.audit_worker_action(session, "disable_worker", worker_id=worker_id, success=False, error="Worker not found.")
                    return self.error(HTTPStatus.NOT_FOUND, "Worker not found.")
                self.audit_worker_action(session, "disable_worker", worker_id=worker_id, changed_fields={"enabled": False})
                return self.json({"worker": worker_public_payload(worker, admin=True)})
            if action == "rotate-token":
                worker = db.rotate_worker_token(worker_id)
                if not worker:
                    self.audit_worker_action(session, "rotate_worker_token", worker_id=worker_id, success=False, error="Worker not found.")
                    return self.error(HTTPStatus.NOT_FOUND, "Worker not found.")
                self.audit_worker_action(session, "rotate_worker_token", worker_id=worker_id, changed_fields={"tokenHash": "rotated"})
                return self.json(worker_create_payload(worker))
            if action == "test":
                worker = db.get_worker(worker_id, include_deleted=True)
                if not worker:
                    self.audit_worker_action(session, "test_worker", worker_id=worker_id, success=False, error="Worker not found.")
                    return self.error(HTTPStatus.NOT_FOUND, "Worker not found.")
                result = worker_test_payload(worker)
                self.audit_worker_action(session, "test_worker", worker_id=worker_id, changed_fields={"result": result.get("ok")})
                return self.json({"worker": worker_public_payload(worker, admin=True), "result": result})
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_admin_worker_create(self, session: dict, body: dict) -> None:
        try:
            max_concurrent_jobs = worker_admin_capacity(body.get("max_concurrent_jobs"))
        except ValueError as exc:
            self.audit_worker_action(session, "create_worker", success=False, error=str(exc))
            return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        worker = db.create_worker(
            {
                "name": public_issue_text(body.get("name")) or "Worker",
                "provider": public_issue_text(body.get("provider")) or "codex",
                "region": public_issue_text(body.get("region")),
                "version": public_issue_text(body.get("version")),
                "max_concurrent_jobs": max_concurrent_jobs,
            }
        )
        self.audit_worker_action(
            session,
            "create_worker",
            worker_id=worker.get("worker_id"),
            changed_fields={"name": worker.get("name"), "provider": worker.get("provider"), "region": worker.get("region")},
        )
        return self.json(worker_create_payload(worker), HTTPStatus.CREATED)

    def handle_admin_worker_command(self, session: dict, worker_id: str, body: dict) -> None:
        command = public_issue_text(body.get("command")).lower()
        action_name = "delete_worker_service" if command == "uninstall" else "stop_worker_service"
        try:
            command = db.normalize_worker_lifecycle_command(command)
        except ValueError as exc:
            self.audit_worker_action(session, action_name, worker_id=worker_id, success=False, error=str(exc))
            return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        action_name = "delete_worker_service" if command == "uninstall" else "stop_worker_service"
        try:
            worker_command = db.create_worker_command(
                {
                    "worker_id": worker_id,
                    "command": command,
                    "requested_by_user_id": session.get("userId"),
                    "request_id": request_id_from_handler(self),
                    "created_at": now(),
                }
            )
        except ValueError as exc:
            self.audit_worker_action(session, action_name, worker_id=worker_id, success=False, error=str(exc))
            return self.error(HTTPStatus.CONFLICT, str(exc))
        if not worker_command:
            self.audit_worker_action(session, action_name, worker_id=worker_id, success=False, error="Worker not found.")
            return self.error(HTTPStatus.NOT_FOUND, "Worker not found.")
        self.audit_worker_action(
            session,
            action_name,
            worker_id=worker_id,
            changed_fields={"command": command, "commandId": worker_command.get("id")},
        )
        worker = db.get_worker(worker_id, include_deleted=True) or {}
        return self.json(
            {
                "ok": True,
                "worker": worker_public_payload(worker, admin=True),
                "command": worker_command_payload(worker_command, admin=True),
            },
            HTTPStatus.ACCEPTED,
        )

    def handle_admin_patch(self, segments: list[str], body: dict) -> None:
        session = self.require_admin_session()
        if not session:
            return
        if not isinstance(body, dict):
            return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        if len(segments) == 3 and segments[:2] == ["admin", "workers"]:
            worker_id = clean_github_access_text(segments[2]) or ""
            changed = {
                key: body.get(key)
                for key in ("name", "provider", "region", "version", "max_concurrent_jobs")
                if key in body
            }
            if "max_concurrent_jobs" in changed:
                try:
                    changed["max_concurrent_jobs"] = worker_admin_capacity(changed["max_concurrent_jobs"])
                except ValueError as exc:
                    self.audit_worker_action(session, "update_worker", worker_id=worker_id, success=False, error=str(exc))
                    return self.error(HTTPStatus.BAD_REQUEST, str(exc))
            worker = db.update_worker(worker_id, changed)
            if not worker:
                self.audit_worker_action(session, "update_worker", worker_id=worker_id, success=False, error="Worker not found.")
                return self.error(HTTPStatus.NOT_FOUND, "Worker not found.")
            self.audit_worker_action(session, "update_worker", worker_id=worker_id, changed_fields=changed)
            return self.json({"worker": worker_public_payload(worker, admin=True)})
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_admin_delete(self, segments: list[str]) -> None:
        session = self.require_admin_session()
        if not session:
            return
        if len(segments) == 3 and segments[:2] == ["admin", "workers"]:
            worker_id = clean_github_access_text(segments[2]) or ""
            worker = db.soft_delete_worker(worker_id)
            if not worker:
                self.audit_worker_action(session, "delete_worker", worker_id=worker_id, success=False, error="Worker not found.")
                return self.error(HTTPStatus.NOT_FOUND, "Worker not found.")
            self.audit_worker_action(session, "delete_worker", worker_id=worker_id, changed_fields={"deleted": True})
            return self.json({"worker": worker_public_payload(worker, admin=True), "deleted": True})
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def require_worker(self, *, allow_disabled: bool = False) -> dict | None:
        record = worker_token_record(self, allow_disabled=allow_disabled)
        if not record:
            self.error(HTTPStatus.UNAUTHORIZED, "A valid worker token is required.")
            return None
        return record

    def handle_worker_post(self, segments: list[str], body: dict) -> None:
        allow_disabled = segments == ["worker", "heartbeat"] or (
            len(segments) == 4 and segments[:2] == ["worker", "jobs"] and segments[3] in {"progress", "result"}
        ) or (
            len(segments) == 4 and segments[:2] == ["worker", "commands"] and segments[3] == "status"
        )
        worker_record = self.require_worker(allow_disabled=allow_disabled)
        if not worker_record:
            return
        if not isinstance(body, dict):
            return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        if segments == ["worker", "heartbeat"]:
            return self.handle_worker_heartbeat(body, worker_record)
        if segments == ["worker", "jobs", "claim"]:
            return self.handle_worker_job_claim(body, worker_record)
        if len(segments) == 4 and segments[:2] == ["worker", "jobs"] and segments[3] == "progress":
            return self.handle_worker_job_progress(segments[2], body, worker_record)
        if len(segments) == 4 and segments[:2] == ["worker", "jobs"] and segments[3] == "result":
            return self.handle_worker_job_result(segments[2], body, worker_record)
        if len(segments) == 4 and segments[:2] == ["worker", "commands"] and segments[3] == "status":
            return self.handle_worker_command_status(segments[2], body, worker_record)
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def authenticated_worker_id_matches(self, worker_record: dict, worker_id: str) -> bool:
        authenticated_worker_id = public_issue_text(worker_record.get("worker_id"))
        return bool(authenticated_worker_id and worker_id and authenticated_worker_id == worker_id)

    def handle_worker_heartbeat(self, body: dict, worker_record: dict) -> None:
        worker_id = clean_github_access_text(body.get("worker_id")) or ""
        if not self.authenticated_worker_id_matches(worker_record, worker_id):
            return self.error(HTTPStatus.FORBIDDEN, "Worker token does not match worker_id.")
        reported_capacity = public_scan_count(body.get("max_concurrent_jobs")) or 1
        heartbeat_capacity = worker_heartbeat_capacity(body.get("max_concurrent_jobs"))
        last_error = clean_scan_error(body.get("last_error"))
        if reported_capacity > heartbeat_capacity:
            clamp_error = f"max_concurrent_jobs clamped to {heartbeat_capacity}"
            last_error = f"{last_error}; {clamp_error}" if last_error else clamp_error
        heartbeat_region = public_issue_text(body.get("region")) if "region" in body else ""
        try:
            record = db.upsert_worker_heartbeat(
                {
                    "worker_id": worker_id,
                    "version": public_issue_text(body.get("version")),
                    "provider": public_issue_text(body.get("provider")) or "codex",
                    "max_concurrent_jobs": heartbeat_capacity,
                    "running_jobs": public_scan_count(body.get("running_jobs")),
                    "free_slots": public_scan_count(body.get("free_slots")),
                    "hostname": public_issue_text(body.get("hostname")),
                    "region": heartbeat_region or None,
                    "last_error": last_error,
                    "doctor_status": public_issue_text(body.get("doctor_status")),
                    "codex_ready": 1 if body.get("codex_ready") is True else 0 if body.get("codex_ready") is False else None,
                    "systemd_active": 1 if body.get("systemd_active") is True else 0 if body.get("systemd_active") is False else None,
                    "doctor_checked_at": pull_request_timestamp(body.get("doctor_checked_at")),
                    "timestamp": now(),
                }
            )
        except ValueError as exc:
            return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        command = db.get_next_worker_command(worker_id)
        return self.json(
            {
                "ok": True,
                "worker": {
                    "worker_id": record.get("worker_id"),
                    "status": record.get("status"),
                    "last_heartbeat_at": record.get("last_heartbeat_at"),
                },
                "command": worker_command_payload(command),
            }
        )

    def handle_worker_command_status(self, command_id: str, body: dict, worker_record: dict) -> None:
        command_id = clean_github_access_text(command_id) or ""
        if not command_id:
            return self.error(HTTPStatus.BAD_REQUEST, "command id is required.")
        worker_id = clean_github_access_text(body.get("worker_id")) or ""
        if not worker_id:
            return self.error(HTTPStatus.BAD_REQUEST, "worker_id is required.")
        if not self.authenticated_worker_id_matches(worker_record, worker_id):
            return self.error(HTTPStatus.FORBIDDEN, "Worker token does not match worker_id.")
        try:
            command = db.update_worker_command_status(
                {
                    "id": command_id,
                    "worker_id": worker_id,
                    "status": public_issue_text(body.get("status")),
                    "error": clean_scan_error(body.get("error")),
                    "timestamp": now(),
                }
            )
        except ValueError as exc:
            return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        if not command:
            return self.error(HTTPStatus.NOT_FOUND, "Worker command not found.")
        return self.json({"ok": True, "command": worker_command_payload(command)})

    def handle_worker_job_claim(self, body: dict, worker_record: dict) -> None:
        worker_id = clean_github_access_text(body.get("worker_id")) or ""
        if not worker_id:
            return self.error(HTTPStatus.BAD_REQUEST, "worker_id is required.")
        if not self.authenticated_worker_id_matches(worker_record, worker_id):
            return self.error(HTTPStatus.FORBIDDEN, "Worker token does not match worker_id.")
        allowed, worker_status = worker_can_claim(worker_record)
        if not allowed:
            return self.error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                f"Worker is not ready to claim jobs: {worker_status}.",
            )
        if "max_jobs" in body:
            max_jobs = public_scan_count(body.get("max_jobs"))
        elif "free_slots" in body:
            max_jobs = public_scan_count(body.get("free_slots"))
        else:
            max_jobs = 1
        if max_jobs <= 0:
            return self.json({"job": None, "jobs": []})
        max_jobs = min(
            max_jobs,
            worker_available_claim_slots(worker_record),
            max(1, env_int("PULLWISE_WORKER_MAX_CLAIM_JOBS", 32)),
        )
        if max_jobs <= 0:
            return self.json({"job": None, "jobs": []})
        try:
            recovered_jobs = db.recover_expired_scan_jobs(now())
            if recovered_jobs:
                with STATE_LOCK:
                    apply_recovered_scan_jobs_locked(recovered_jobs)
            jobs = db.claim_next_scan_jobs(
                worker_id,
                max_jobs=max_jobs,
                lease_seconds=max(60, env_int("PULLWISE_SCAN_JOB_LEASE_SECONDS", 3600)),
                per_user_running_limit=max_scan_concurrency_per_user(),
            )
        except ValueError as exc:
            return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        if not jobs:
            return self.json({"job": None, "jobs": []})
        try:
            payloads = [scan_job_payload(job, include_clone_token=True) for job in jobs]
        except github_auth.GitHubError as exc:
            for job in jobs:
                db.requeue_interrupted_scan_job(
                    str(job.get("scan_id") or ""),
                    reason="clone_token_unavailable",
                    timestamp=now(),
                )
            return self.error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
        with STATE_LOCK:
            for job in jobs:
                scan = next((item for item in SCANS if item.get("id") == job.get("scan_id")), None)
                if scan and scan.get("status") == "queued":
                    scan.update(
                        {
                            "status": "running",
                            "claimedAt": job.get("claimed_at"),
                            "claimedByWorkerId": worker_id,
                            "progress": 0,
                            "phase": "clone",
                            "jobId": job.get("job_id"),
                        }
                    )
                    mark_state_dirty()
                scan_logging.log_event(
                    "worker_job_claimed",
                    scanId=job.get("scan_id"),
                    repo=job.get("repo"),
                    repoId=job.get("repo_id"),
                    githubRepoId=job.get("github_repo_id"),
                    branch=job.get("branch"),
                    commit=job.get("commit"),
                    workerId=worker_id,
                    jobId=job.get("job_id"),
                    attempt=job.get("attempt"),
                )
        return self.json({"job": payloads[0], "jobs": payloads})

    def handle_worker_job_progress(self, job_id: str, body: dict, worker_record: dict) -> None:
        job_id = clean_github_access_text(job_id) or ""
        if not job_id:
            return self.error(HTTPStatus.BAD_REQUEST, "job_id is required.")
        current_job = db.get_scan_job(job_id)
        if not current_job:
            return self.error(HTTPStatus.NOT_FOUND, "Job not found.")
        if not self.authenticated_worker_id_matches(worker_record, public_issue_text(current_job.get("claimed_by_worker_id"))):
            return self.error(HTTPStatus.FORBIDDEN, "Worker token does not match claimed job.")
        job = db.update_scan_job_progress(
            job_id,
            {
                "phase": public_scan_phase(body.get("phase")),
                "progress": public_scan_progress(body.get("progress")),
                "message": public_issue_text(body.get("message")),
                "started_at": pull_request_timestamp(body.get("started_at")) or now(),
                "logs_summary": public_issue_text(body.get("logs_summary")),
            },
        )
        if not job:
            return self.error(HTTPStatus.NOT_FOUND, "Job not found.")
        with STATE_LOCK:
            scan = next((item for item in SCANS if item.get("id") == job.get("scan_id")), None)
            if scan and scan.get("status") == "running":
                scan.update(
                    {
                        "phase": public_scan_phase(body.get("phase")),
                        "progress": public_scan_progress(body.get("progress")),
                        "startedAt": job.get("started_at"),
                        "updatedAt": now(),
                    }
                )
                mark_state_dirty()
        return self.json({"ok": True, "job": scan_job_payload(job)})

    def handle_worker_job_result(self, job_id: str, body: dict, worker_record: dict) -> None:
        job_id = clean_github_access_text(job_id) or ""
        if not job_id:
            return self.error(HTTPStatus.BAD_REQUEST, "job_id is required.")
        job = db.get_scan_job(job_id)
        if not job:
            return self.error(HTTPStatus.NOT_FOUND, "Job not found.")
        if not self.authenticated_worker_id_matches(worker_record, public_issue_text(job.get("claimed_by_worker_id"))):
            return self.error(HTTPStatus.FORBIDDEN, "Worker token does not match claimed job.")
        try:
            result = apply_worker_job_result(job, body)
        except ValueError as exc:
            return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        if result.get("conflict"):
            return self.json({"message": "Result checksum conflicts with an existing attempt result."}, HTTPStatus.CONFLICT)
        scan_logging.log_event(
            "worker_job_result",
            scanId=job.get("scan_id"),
            repo=job.get("repo"),
            repoId=job.get("repo_id"),
            githubRepoId=job.get("github_repo_id"),
            branch=job.get("branch"),
            commit=job.get("commit"),
            jobId=job.get("job_id"),
            status=body.get("status"),
            duplicate=result.get("duplicate"),
            issueCount=result.get("issueCount"),
        )
        return self.json({"ok": True, **result})

    def handle_external_api_get(self, segments: list[str], params: dict) -> None:
        if segments == ["repositories"]:
            context = self.require_api_key_context("repositories:read")
            if not context:
                return
            user = context["user"]
            github_access = user.get("githubRepositoryAccess")
            items = repository_items_for_response(user, github_access)
            return self.json(
                {
                    "items": items,
                    "repositories": items,
                    "apiKey": api_key_public_payload(context["apiKey"]),
                }
            )
        if len(segments) == 4 and segments[0] == "repositories" and segments[2] == "scans" and segments[3] == "current":
            context = self.require_api_key_context("scans:read")
            if not context:
                return
            repo_context = self.api_repository_context(context, segments[1])
            if not repo_context:
                return
            repository = repo_context[0]
            scan = latest_scan_for_user_repo(context["user"]["id"], repository["id"])
            return self.json(
                {
                    "repoId": repository["id"],
                    "scan": scan_payload(scan) if scan else None,
                    "status": public_scan_status(scan.get("status")) if scan else "idle",
                }
            )
        if len(segments) == 3 and segments[0] == "repositories" and segments[2] == "quota":
            context = self.require_api_key_context("quota:read")
            if not context:
                return
            repo_context = self.api_repository_context(context, segments[1])
            if not repo_context:
                return
            user = context["user"]
            repository = repo_context[0]
            return self.json(
                {
                    "repoId": repository["id"],
                    "user": quota.quota_payload_for_user(user),
                    "repository": quota.quota_payload_for_repository(repository, user),
                }
            )
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_external_api_post(self, segments: list[str], body: dict) -> None:
        if len(segments) == 3 and segments[0] == "repositories" and segments[2] == "scans":
            return self.handle_external_api_scan_start(segments[1], body)
        if len(segments) == 4 and segments[0] == "repositories" and segments[2] == "scans" and segments[3] == "stop":
            return self.handle_external_api_scan_stop(segments[1])
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_external_api_scan_start(self, repo_id: str, body: dict) -> None:
        context = self.require_api_key_context("scans:write")
        if not context:
            return
        if not isinstance(body, dict):
            return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        repo_context = self.api_repository_context(context, repo_id)
        if not repo_context:
            return
        repository = repo_context[0]
        if review.selected_provider() == "disabled":
            return self.error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Code review provider is not configured. Set PULLWISE_REVIEW_PROVIDER to claude_code or codex for real scans. Use mock only for explicit local wire-up.",
            )
        request_id = scan_request_id_from_body(body)
        existing = user_scan_by_request_id(context["user"]["id"], request_id)
        if existing and existing.get("repoId") == repository["id"]:
            return self.json(scan_payload(existing))
        if existing:
            return self.json(idempotency_key_reused_payload(existing), HTTPStatus.CONFLICT)
        limit_error = scan_queue_limit_error(context["user"]["id"])
        if limit_error:
            return self.json({"message": limit_error[1], "code": limit_error[2]}, limit_error[0])
        scan_id = make_id("sc")
        try:
            quota_result = quota.consume_scan_quota(
                user=context["user"],
                repository=repository,
                requested_by_user_id=context["user"]["id"],
                scan_id=scan_id,
                request_id=request_id or None,
            )
        except quota.QuotaExceeded as exc:
            payload = {"message": exc.message, "code": exc.code}
            if exc.repo_id:
                payload["repoId"] = exc.repo_id
            return self.json(payload, HTTPStatus.PAYMENT_REQUIRED)

        github_access = context["user"].get("githubRepositoryAccess") or {}
        repository_item_meta = repo_context[1] if isinstance(repo_context[1], dict) else {}
        branch = clean_github_access_text(body.get("branch")) or clean_github_access_text(repository.get("default_branch")) or "main"
        scan = {
            "id": scan_id,
            "repo": repository["full_name"],
            "branch": branch,
            "commit": clean_github_access_text(body.get("commit")) or "pending",
            "status": "queued",
            "userId": context["user"]["id"],
            "apiKeyId": context["apiKey"]["id"],
            "createdAt": now(),
            "queuedAt": now(),
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "installationId": clean_github_access_text(repository_item_meta.get("installationId"), allow_int=True)
            or clean_github_access_text(github_access.get("installationId"), allow_int=True),
            "installationAccount": clean_github_access_text(repository_item_meta.get("installationAccount"))
            or clean_github_access_text(github_access.get("installationAccount")),
            "repositorySelection": clean_github_access_text(repository_item_meta.get("repositorySelection"))
            or clean_github_access_text(github_access.get("repositorySelection")),
            "repoId": repository["id"],
            "githubRepoId": repository["github_repo_id"],
            "quotaBucketIds": quota_result["bucketIds"],
            "cloneUrl": trusted_github_web_url(repository_item_meta.get("cloneUrl")) or repository.get("clone_url"),
            "repositoryPrivate": bool(repository.get("private")),
            "repoPath": None,
            "billingUsage": quota_result["user"],
            "repoUsage": quota_result["repository"],
            "by": "api key",
        }
        if request_id:
            scan["requestId"] = request_id
        with STATE_LOCK:
            create_scan_job_for_scan(scan)
            SCANS.insert(0, scan)
            mark_state_dirty()
        scan_logging.log_event(
            "scan_queued",
            scanId=scan["id"],
            userId=scan.get("userId"),
            repo=scan.get("repo"),
            branch=scan.get("branch"),
            commit=scan.get("commit"),
            provider=review.selected_provider(),
            requestId=scan.get("requestId"),
            installationId=scan.get("installationId"),
            repoId=scan.get("repoId"),
            githubRepoId=scan.get("githubRepoId"),
            quotaBucketIds=scan.get("quotaBucketIds"),
            apiKeyId=scan.get("apiKeyId"),
        )
        worker.notify_queue_changed()
        return self.json(scan_payload(scan), HTTPStatus.CREATED)

    def handle_external_api_scan_stop(self, repo_id: str) -> None:
        context = self.require_api_key_context("scans:write")
        if not context:
            return
        repo_context = self.api_repository_context(context, repo_id)
        if not repo_context:
            return
        with STATE_LOCK:
            repository = repo_context[0]
            scan = active_scan_for_user_repo(context["user"]["id"], repository["id"])
            if not scan:
                return self.error(HTTPStatus.NOT_FOUND, "No queued or running scan exists for this repository.")
            scan["status"] = "cancelled"
            scan["completedAt"] = now()
            mark_state_dirty()
            db.cancel_scan_job_for_scan(str(scan.get("id") or ""))
        worker.notify_queue_changed()
        return self.json(scan_payload(scan))

    def clear_current_session(self) -> None:
        session_id = self.current_session_id()
        if session_id and SESSIONS.pop(session_id, None):
            mark_state_dirty()

    def find_or_404(self, collection: list[dict], item_id: str, label: str) -> dict:
        for item in collection:
            if item.get("id") == item_id:
                return item
        raise ResourceNotFound(label)

    def read_json(self) -> dict:
        return decode_json_body(self.read_raw_body())

    def read_raw_body(self) -> bytes:
        length = self.request_content_length()
        if length == 0:
            return b""
        if length > max_body_bytes():
            raise RequestBodyTooLarge("Request body is too large.")
        return self.rfile.read(length)

    def enforce_body_size_limit(self, method: str) -> None:
        if method not in {"POST", "PATCH"}:
            return
        length = self.request_content_length()
        if length > max_body_bytes():
            raise RequestBodyTooLarge("Request body is too large.")

    def request_content_length(self) -> int:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return 0
        raw_text = str(raw_length).strip()
        if not raw_text:
            return 0
        if not raw_text.isdigit():
            raise ValueError("Invalid Content-Length header.")
        return int(raw_text)

    def handle_creem_webhook(self) -> None:
        raw = self.read_raw_body()
        if not billing.verify_creem_webhook(raw, self.headers.get("creem-signature")):
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid Creem webhook signature.")
        event = decode_json_body(raw)
        if not isinstance(event, dict):
            return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        update = billing.billing_update_from_creem_event(event)
        if update:
            self.apply_billing_update(update)
        return self.json({"received": True})

    def handle_stripe_webhook(self) -> None:
        raw = self.read_raw_body()
        if not billing.verify_stripe_webhook(raw, self.headers.get("Stripe-Signature")):
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid Stripe webhook signature.")
        event = decode_json_body(raw)
        if not isinstance(event, dict):
            return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        update = billing.billing_update_from_stripe_event(event)
        if update:
            self.apply_billing_update(update)
        return self.json({"received": True})

    def apply_billing_update(self, update: dict) -> None:
        if billing_event_processed(update):
            return
        user = billing_user_for_update(update)
        if user:
            apply_billing_update_to_user(user, update)
            apply_pending_billing_updates_for_user(user)
            return
        remember_pending_billing_update(update)

    def send_cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        allowed = allowed_origins()
        if origin and origin in allowed:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")

    def json(self, payload: dict, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            response_headers = {**getattr(self, "_rate_limit_headers", {}), **(headers or {})}
            for key, value in response_headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_DISCONNECT_EXCEPTIONS as exc:
            raise ClientDisconnected("Client disconnected before the response was sent.") from exc

    def text(self, payload: str, status: int = HTTPStatus.OK, *, content_type: str = "text/plain; charset=utf-8") -> None:
        body = payload.encode("utf-8")
        try:
            self.send_response(status)
            self.send_cors_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in getattr(self, "_rate_limit_headers", {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_DISCONNECT_EXCEPTIONS as exc:
            raise ClientDisconnected("Client disconnected before the response was sent.") from exc

    def redirect(self, location: str, set_cookie: str | None = None) -> None:
        try:
            self.send_response(HTTPStatus.FOUND)
            self.send_cors_headers()
            self.send_header("Location", location)
            if set_cookie:
                self.send_header("Set-Cookie", set_cookie)
            self.end_headers()
        except _CLIENT_DISCONNECT_EXCEPTIONS as exc:
            raise ClientDisconnected("Client disconnected before the response was sent.") from exc

    def serve_static_file(self, file_path: str) -> None:
        """Serve a static file from disk with appropriate headers."""
        try:
            stat = os.stat(file_path)
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            return self.error(HTTPStatus.NOT_FOUND, "File not found")
        content_type, _ = mimetypes.guess_type(file_path)
        content_type = content_type or "application/octet-stream"
        try:
            with open(file_path, "rb") as f:
                body = f.read()
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            return self.error(HTTPStatus.NOT_FOUND, "File not found")
        try:
            self.send_response(HTTPStatus.OK)
            self.send_cors_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(stat.st_size))
            # Cache static assets for 1 year (hashed filenames), don't cache index.html
            if os.path.basename(file_path) == "index.html":
                self.send_header("Cache-Control", "no-cache")
            elif "/assets/" in file_path.replace("\\", "/"):
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_DISCONNECT_EXCEPTIONS as exc:
            raise ClientDisconnected("Client disconnected before the response was sent.") from exc

    def serve_spa(self) -> None:
        """Serve the SPA index.html for client-side routing."""
        root = web_root()
        index = os.path.join(root, "index.html")
        if os.path.isfile(index):
            self.serve_static_file(index)
        else:
            self.error(HTTPStatus.NOT_FOUND, "Frontend not built. Run 'npm run build' in pullwise-web.")

    def error(self, status: int, message: str) -> None:
        self.json({"message": message}, status)


def main() -> None:
    load_env_file()
    logging_config.configure_logging(project_root=project_root())
    parser = argparse.ArgumentParser(description="Run the Pullwise local API server.")
    parser.add_argument("--host", default=env("PULLWISE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=parse_port, default=server_port())
    args = parser.parse_args()

    ensure_state_loaded()
    recovered_scans = recover_interrupted_scans()
    if recovered_scans:
        logger.info("Recovered %s interrupted scan(s).", recovered_scans)
    httpd = ThreadingHTTPServer((args.host, args.port), PullwiseHandler)
    logger.info("Pullwise API listening on http://%s:%s", args.host, args.port)
    logger.info("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
