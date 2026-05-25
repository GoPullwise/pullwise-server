from __future__ import annotations

import argparse
import json
import logging
import math
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
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from . import billing, checkout, db, fix_workflow, github_auth, logging_config, review, scan_logging, worker

logger = logging.getLogger(__name__)
access_logger = logging.getLogger("pullwise_server.access")

def project_root() -> str:
    return os.path.dirname(os.path.dirname(__file__))


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
SCAN_PHASES = {"clone", "index", "secrets", "deps", "ai", "report"}
BILLING_PUBLIC_STATUSES = {"none", "active", "trialing", "canceling", "past_due", "unpaid", "paused", "canceled"}

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
    with STATE_LOCK:
        for scan in SCANS:
            if scan.get("status") != "running":
                continue
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
            "maxConcurrentScans": max_scan_concurrency(),
            "maxConcurrentScansPerUser": max_scan_concurrency_per_user(),
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
    return f"{app_url}/?screen={screen}"


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


def pop_github_state(kind: str, state: str) -> dict:
    record = GITHUB_STATES.pop(state, None)
    if record:
        mark_state_dirty()
    if not record or record.get("kind") != kind or record.get("expiresAt", 0) < now():
        raise ValueError("GitHub authorization state is invalid or expired.")
    return record


def remember_github_repository_authorization(
    user: dict,
    redirect_to: str,
    requested_scope: str,
    *,
    manage: bool = False,
) -> str:
    state = remember_github_state("install", redirect_to, userId=user["id"], requestedScope=requested_scope)
    github_access = user.get("githubRepositoryAccess") or {}
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


def github_repository_authorization_pending(user: dict | None) -> dict | None:
    if not user:
        return None

    timestamp = now()
    pending = user.get("githubRepositoryAccessPending")
    if isinstance(pending, dict):
        if int(pending.get("expiresAt") or 0) >= timestamp:
            return pending
        user.pop("githubRepositoryAccessPending", None)
        mark_state_dirty()

    expired_states = []
    for state, record in GITHUB_STATES.items():
        if record.get("kind") != "install" or record.get("userId") != user.get("id"):
            continue
        if int(record.get("expiresAt") or 0) < timestamp:
            expired_states.append(state)
            continue
        return {
            "state": state,
            "startedAt": record.get("startedAt"),
            "expiresAt": record.get("expiresAt"),
            "previousInstallationId": (user.get("githubRepositoryAccess") or {}).get("installationId"),
            "manage": True,
        }

    for state in expired_states:
        GITHUB_STATES.pop(state, None)
    if expired_states:
        mark_state_dirty()
    return None


def clear_github_repository_authorization_pending(user: dict | None, state: str | None = None) -> None:
    if not user:
        return

    pending = user.get("githubRepositoryAccessPending")
    if isinstance(pending, dict) and (not state or pending.get("state") == state):
        user.pop("githubRepositoryAccessPending", None)
        mark_state_dirty()

    states_to_clear = [
        stored_state
        for stored_state, record in GITHUB_STATES.items()
        if record.get("kind") == "install"
        and record.get("userId") == user.get("id")
        and (not state or stored_state == state)
    ]
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
    return [scan for scan in SCANS if scan.get("userId") == session["userId"]]


def user_scan_by_request_id(user_id: str, request_id: str) -> dict | None:
    if not request_id:
        return None
    for scan in SCANS:
        if scan.get("userId") == user_id and scan.get("requestId") == request_id:
            return scan
    return None


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


def public_billing_text(value: object) -> str | None:
    return public_issue_text(value) or None


def public_billing_status(value: object) -> str:
    status = public_issue_text(value).lower()
    return status if status in BILLING_PUBLIC_STATUSES else "none"


def scan_payload(scan: dict) -> dict:
    payload = {
        "id": public_issue_text(scan.get("id")),
        "userId": public_issue_text(scan.get("userId")),
        "repo": clean_repository_full_name(scan.get("repo"), scan.get("repository")),
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
    if "installationAccount" in scan:
        payload["installationAccount"] = clean_github_access_text(scan.get("installationAccount"))
    if "installationTargetType" in scan:
        payload["installationTargetType"] = clean_github_access_text(scan.get("installationTargetType"))
    if "repositorySelection" in scan:
        payload["repositorySelection"] = clean_github_access_text(scan.get("repositorySelection"))
    if "cloneUrl" in scan:
        payload["cloneUrl"] = trusted_github_web_url(scan.get("cloneUrl"))
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


def public_scan_issue_counts(value: object) -> dict:
    counts = value if isinstance(value, dict) else {}
    return {
        "critical": public_scan_count(counts.get("critical")),
        "high": public_scan_count(counts.get("high")),
        "medium": public_scan_count(counts.get("medium")),
        "low": public_scan_count(counts.get("low")),
        "info": public_scan_count(counts.get("info")),
    }


def clean_scan_error(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "").splitlines()[0].strip()[:500]


def issue_payload(issue: dict) -> dict:
    issue_id = public_issue_text(issue.get("id")) or clean_pull_request_issue_id(issue.get("id"))
    auto_fix = issue.get("autoFix") is True
    auto_fixable = issue.get("autoFixable") is True or auto_fix
    payload = {
        "id": issue_id,
        "userId": public_issue_text(issue.get("userId")),
        "scanId": public_issue_text(issue.get("scanId")),
        "repo": clean_repository_full_name(issue.get("repo"), issue.get("repository")),
        "branch": public_issue_text(issue.get("branch")),
        "status": public_issue_status(issue.get("status")),
        "severity": review._safe_severity(issue.get("severity")),
        "category": review._safe_category(issue.get("category")),
        "title": review._safe_text(issue.get("title"), "Untitled finding"),
        "summary": public_issue_text(issue.get("summary")) or public_issue_text(issue.get("description")),
        "impact": public_issue_text(issue.get("impact")),
        "file": public_issue_text(issue.get("file")),
        "line": review._safe_non_negative_int(issue.get("line")),
        "confidence": review._safe_confidence(issue.get("confidence")),
        "autoFix": auto_fix,
        "autoFixable": auto_fixable,
        "effort": review._safe_text(issue.get("effort"), "-"),
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
        "global": max_scan_concurrency(),
        "perUser": max_scan_concurrency_per_user(),
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
    elif running_counts["global"] >= limits["global"]:
        reason = "global_limit"
        message = (
            f"Server is running {running_counts['global']} of {limits['global']} scans; "
            f"{plural(ahead, 'scan')} ahead."
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


def max_scan_concurrency() -> int:
    return max(1, env_int("PULLWISE_MAX_CONCURRENT_SCANS", 1))


def max_scan_concurrency_per_user() -> int:
    return max(1, env_int("PULLWISE_MAX_CONCURRENT_SCANS_PER_USER", 1))


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def user_issues(session: dict | None) -> list[dict]:
    if not session:
        return []
    return [issue for issue in ISSUES if issue.get("userId") == session["userId"]]


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
        if github_repositories_need_sync(github_access):
            raise ValueError("Sync GitHub repositories before creating a pull request.")
        existing = issue.get("pullRequest")
        pending = issue.get("pullRequestPending") if not isinstance(existing, dict) else None
        recovering_pending = isinstance(pending, dict) and pull_request_pending_is_stale(pending)
        if isinstance(pending, dict) and not recovering_pending:
            raise ValueError("Pull request creation is already in progress for this issue.")
        if not github_auth.app_api_configured():
            raise ValueError("GitHub App API is not configured for pull request creation.")
        repo = clean_repository_full_name(issue.get("repo"), issue.get("repository"), scan.get("repo"))
        if not repo:
            raise ValueError("Repository must be a GitHub full name like owner/repo.")
        if not github_repository_access_authorized_for_user(user, github_access):
            raise ValueError("Authorize GitHub repositories before creating a pull request.")
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
        clone_url = trusted_github_web_url(repo_meta.get("cloneUrl") or repo_meta.get("clone_url"))
        if not clone_url:
            clone_url = trusted_github_web_url(scan.get("cloneUrl") or scan.get("clone_url"))

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
        if item.get("fullName") == full_name or item.get("full_name") == full_name:
            return item
    return None


def repository_is_authorized(github_access: dict | None, full_name: str) -> bool:
    if not github_access:
        return False
    repositories = clean_github_access_text_list(github_access.get("repositories"))
    if repositories:
        return full_name in repositories
    return repository_item(github_access, full_name) is not None


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
    full_name = clean_github_access_text(value.get("fullName")) or clean_github_access_text(value.get("full_name"))
    if not full_name or "/" not in full_name:
        return None

    base_item = repository_item_from_full_name(full_name)
    description = clean_github_access_text(value.get("description")) or clean_github_access_text(value.get("desc")) or ""
    return {
        "id": clean_github_access_text(value.get("id"), allow_int=True) or full_name,
        "name": clean_github_access_text(value.get("name")) or base_item["name"],
        "fullName": full_name,
        "desc": description,
        "description": description,
        "lang": clean_github_access_text(value.get("lang")) or clean_github_access_text(value.get("language")) or "-",
        "private": value.get("private") is True,
        "stars": clean_github_access_text(value.get("stars")) or "-",
        "branches": clean_github_access_text(value.get("branches")) or "-",
        "defaultBranch": clean_github_access_text(value.get("defaultBranch")) or clean_github_access_text(value.get("default_branch")) or "main",
        "updated": clean_github_access_text(value.get("updated")) or "",
        "htmlUrl": trusted_github_web_url(value.get("htmlUrl") or value.get("html_url")),
        "cloneUrl": trusted_github_web_url(value.get("cloneUrl") or value.get("clone_url")),
        "permissions": github_auth.permissions_to_dict(value.get("permissions") or {}),
        "installationId": clean_github_access_text(value.get("installationId"), allow_int=True),
        "installationAccount": clean_github_access_text(value.get("installationAccount")),
        "installationTargetType": clean_github_access_text(value.get("installationTargetType")),
        "repositorySelection": clean_github_access_text(value.get("repositorySelection")),
    }


def github_repository_access_connected(github_access: dict | None) -> bool:
    if not github_access or github_repositories_need_sync(github_access):
        return False
    return bool(repository_items_for_payload(github_access))


def github_repositories_need_sync(github_access: dict | None) -> bool:
    return bool(github_access and github_access.get("repositoriesNeedSync") is True)


def github_repository_access_authorized_for_user(user: dict | None, github_access: dict | None) -> bool:
    if not user or not github_access:
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
        if installation_account and current_login and installation_account != current_login:
            return False

    for installation in github_access.get("installations") or []:
        if str(installation.get("installationTargetType") or "").casefold() != "user":
            continue
        installation_account = str(installation.get("installationAccount") or "").casefold()
        if installation_account and current_login and installation_account != current_login:
            return False

    return bool(authorized_user_id)


def github_repository_access_needs_aggregation_migration(user: dict | None, github_access: dict | None) -> bool:
    if not user or not github_access or github_access.get("mode") != "github-app":
        return False
    if not github_repository_access_authorized_for_user(user, github_access):
        return False
    return not bool(github_access.get("installations"))


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
        safe_installation_summary(installation, include_url_aliases=True)
        for installation in installations
        if isinstance(installation, dict)
    ]


def safe_installation_summary(installation: dict, *, include_url_aliases: bool = False) -> dict:
    safe_url = trusted_github_web_url(
        installation.get("installationHtmlUrl") or installation.get("htmlUrl") or installation.get("html_url")
    )
    item = {
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
    if include_url_aliases:
        item["htmlUrl"] = safe_url
        item["html_url"] = safe_url
    return item


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

    repository_items_by_name: dict[str, dict] = {}
    for access in installation_accesses:
        for item in access.get("repositoryItems") or []:
            full_name = str(item.get("fullName") or item.get("full_name") or "")
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
        "authorizedAt": now(),
        "authorizedUserId": user.get("id"),
        "authorizedGithubId": user.get("githubId"),
        "authorizedGithubLogin": user.get("githubLogin"),
        "validatedAt": now(),
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
        "nextStep": "choose_repositories" if repositories_connected else "connect_github_repositories",
    }


def billing_event_id(update: dict) -> str:
    return billing_update_text(update.get("eventId"))


def billing_update_text(value: object) -> str:
    return value if isinstance(value, str) else ""


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
    attributes = ["Path=/", "HttpOnly", "SameSite=Lax"]
    if cookie_secure_enabled():
        attributes.append("Secure")
    return "; ".join(attributes)


def cookie_secure_enabled() -> bool:
    if os.environ.get("PULLWISE_COOKIE_SECURE", "").strip():
        return env_flag("PULLWISE_COOKIE_SECURE")
    public_base = os.environ.get("PULLWISE_API_BASE_URL") or os.environ.get("PULLWISE_APP_URL") or ""
    return public_base.startswith("https://")


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
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")
        self.end_headers()

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
        segments = [part for part in path.split("/") if part]
        self._rate_limit_headers = {}

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
        except RequestBodyTooLarge as exc:
            return self.error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(exc))
        except ResourceNotFound as exc:
            return self.error(HTTPStatus.NOT_FOUND, str(exc))
        except ValueError as exc:
            return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        except billing.BillingProviderConflict as exc:
            return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        except billing.BillingConfigurationError as exc:
            return self.error(HTTPStatus.NOT_IMPLEMENTED, str(exc))
        except Exception as exc:
            logger.exception("Unhandled server error while handling %s %s", method, self.path)
            return self.error(HTTPStatus.INTERNAL_SERVER_ERROR, "Server error.")
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
                **readiness_payload(),
            })
        if path == "/auth/session":
            return self.json(session_payload(self.current_session()))
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
        if path == "/repositories":
            return self.json(self.repositories_payload())
        if path == "/scans":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing scans.")
            scans = [scan_payload(scan) for scan in user_scans(session)]
            return self.json({"items": scans, "scans": scans})
        if len(segments) == 2 and segments[0] == "scans":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing scans.")
            return self.json(scan_payload(self.find_or_404(user_scans(session), segments[1], "Scan")))
        if path == "/issues":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing issues.")
            issues = user_issues(session)
            scan_id = params.get("scanId")
            if scan_id:
                issues = [issue for issue in issues if issue.get("scanId") == scan_id]
            issue_payloads = [issue_payload(issue) for issue in issues]
            return self.json({"items": issue_payloads, "issues": issue_payloads})
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
            payload = billing.public_plan()
            session = self.current_session()
            user = USERS.get(session["userId"]) if session else None
            if user:
                payload["account"] = billing_account_payload(user)
            return self.json(payload)
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_post(self, path: str, params: dict, segments: list[str]) -> None:
        if path == "/webhooks/stripe":
            return self.handle_stripe_webhook()
        if path == "/webhooks/creem":
            return self.handle_creem_webhook()
        body = self.read_json()
        if path == "/auth/sign-out":
            self.clear_current_session()
            return self.json({"ok": True}, headers={"Set-Cookie": clear_cookie_header()})
        if path == "/repositories/sync":
            if not self.current_session():
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before syncing repositories.")
            payload = self.repositories_payload(refresh=True)
            payload.update({"ok": True, "syncedAt": now()})
            return self.json(payload)
        if path == "/scans":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before starting a scan.")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            repository = str(body.get("repo") or body.get("repository") or "").strip()
            if not repository:
                return self.error(HTTPStatus.BAD_REQUEST, "A repository is required to start a scan.")
            if review.selected_provider() == "disabled":
                return self.error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "Code review provider is not configured. Set PULLWISE_REVIEW_PROVIDER to claude_code or codex for real scans. Use mock only for explicit local wire-up.",
                )
            request_id = str(body.get("requestId") or body.get("idempotencyKey") or "").strip()[:128]
            scan_error: tuple[int, str] | None = None
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
                elif not repository_is_authorized(github_access, repository):
                    scan_error = (HTTPStatus.FORBIDDEN, "Repository is not authorized for this GitHub App installation.")
                else:
                    scan = user_scan_by_request_id(session["userId"], request_id)
                    if scan is None:
                        quota_allowed, entitlement = consume_review_quota(user)
                        if not quota_allowed:
                            scan_error = (
                                HTTPStatus.PAYMENT_REQUIRED,
                                (
                                    f"Monthly review limit reached for the {entitlement['plan']} plan "
                                    f"({entitlement['used']}/{entitlement['limit']} reviews used)."
                                ),
                            )
                        else:
                            repo_meta = repository_item(github_access, repository) or {}
                            branch = (
                                clean_github_access_text(body.get("branch"))
                                or clean_github_access_text(repo_meta.get("defaultBranch"))
                                or "main"
                            )
                            scan = {
                                "id": make_id("sc"),
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
                                "cloneUrl": repo_meta.get("cloneUrl") or repo_meta.get("clone_url"),
                                "repositoryPrivate": bool(repo_meta.get("private")),
                                "repoPath": None,
                                "billingUsage": {
                                    "period": entitlement["period"],
                                    "plan": entitlement["plan"],
                                    "used": entitlement["used"],
                                    "limit": entitlement["limit"],
                                },
                                "by": "you",
                            }
                            if request_id:
                                scan["requestId"] = request_id
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
                )
                return self.error(scan_error[0], scan_error[1])
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
                )
                worker.start_scan(scan["id"])
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
                scan["status"] = "cancelled"
                scan["completedAt"] = now()
                mark_state_dirty()
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
            if checkout.get("customerId"):
                current_billing = user.get("billing") or {}
                user["billing"] = {
                    **current_billing,
                    "provider": checkout.get("provider") or current_billing.get("provider"),
                    "customerId": checkout.get("customerId"),
                    "updatedAt": now(),
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
            portal = billing.create_portal_session(
                USERS[session["userId"]],
                return_url=safe_redirect_to(body.get("returnUrl"), "settings"),
            )
            return self.json(portal)
        if path == "/billing/change-interval":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before changing your subscription.")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            result = billing.change_subscription_interval(
                USERS[session["userId"]],
                interval=str(body.get("interval") or "year"),
                return_url=safe_redirect_to(body.get("returnUrl"), "billing"),
            )
            if result.get("alreadyActive"):
                return self.json(result)
            if result.get("provider") == "creem" and result.get("interval") == "year":
                user = USERS[session["userId"]]
                current_billing = user.get("billing") or {}
                user["billing"] = {
                    **current_billing,
                    "provider": "creem",
                    "subscriptionId": result.get("subscriptionId") or current_billing.get("subscriptionId"),
                    "status": result.get("status") or current_billing.get("status") or "active",
                    "plan": "pro",
                    "interval": "year",
                    "currentPeriodStart": result.get("currentPeriodStart") or current_billing.get("currentPeriodStart"),
                    "currentPeriodEnd": result.get("currentPeriodEnd") or current_billing.get("currentPeriodEnd"),
                    "updatedAt": now(),
                }
                mark_state_dirty()
            return self.json(result)
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_patch(self, segments: list[str]) -> None:
        body = self.read_json()
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
        record = pop_github_state("login", state)
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
            state = remember_github_repository_authorization(user, redirect_to, scope)
            return self.json({"url": github_auth.build_app_install_url(state), "mode": "github-app-add"})

        if manage:
            existing_url = trusted_github_web_url(existing_access.get("installationHtmlUrl") if existing_access else None)
            if github_repository_access_connected(existing_access) and existing_url:
                return self.json({
                    "ok": True,
                    "connected": True,
                    "url": existing_url,
                    "mode": "github-app-existing-manage",
                    "installationId": clean_github_access_text(existing_access.get("installationId"), allow_int=True),
                })
            existing_installations = existing_access.get("installations") if existing_access else []
            safe_existing_installations = safe_installation_summaries(existing_installations or [])
            if github_repository_access_connected(existing_access) and any(
                installation.get("installationHtmlUrl") for installation in safe_existing_installations
            ):
                return self.json({
                    "ok": True,
                    "connected": True,
                    "mode": "github-app-existing-manage-list",
                    "installationId": clean_github_access_text(existing_access.get("installationId"), allow_int=True),
                    "installationIds": clean_github_access_text_list(existing_access.get("installationIds"), allow_int=True),
                    "installationAccount": clean_github_access_text(existing_access.get("installationAccount")),
                    "installationAccounts": clean_github_access_text_list(existing_access.get("installationAccounts")),
                    "installations": safe_existing_installations,
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
        if record.get("stateFallback"):
            user_can_access = github_auth.user_can_access_installation(user.get("githubAccessToken"), installation_id)
            if user_can_access is not True:
                raise ValueError("Unable to verify access to this GitHub App installation.")

        installations = current_user_github_app_installations(user)
        if not any(str(installation.get("id") or "") == installation_id for installation in installations):
            raise ValueError("Unable to verify this GitHub App installation belongs to the signed-in GitHub user.")

        bind_github_repository_installations(
            user,
            installations,
            params.get("scope") or record.get("requestedScope") or "selected",
        )
        clear_github_repository_authorization_pending(user, state)
        session = create_session(user)
        return self.redirect(str(record["redirectTo"]), cookie_header(session["id"]))

    def github_install_record_from_callback(self, params: dict) -> dict:
        state = params.get("state") or ""
        if state:
            return pop_github_state("install", state)

        session = self.current_session()
        if not session:
            raise ValueError("GitHub authorization state is invalid or expired.")
        return {
            "kind": "install",
            "redirectTo": safe_redirect_to(params.get("redirectTo"), "repos"),
            "userId": session["userId"],
            "requestedScope": params.get("scope") or "selected",
            "stateFallback": True,
        }

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
            "installationHtmlUrl": trusted_github_web_url(visible_access.get("installationHtmlUrl")) if visible_access else None,
            "installations": safe_installation_summaries(visible_access.get("installations") if visible_access else []),
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
            github_access = try_bind_existing_github_repository_access(user, force_refresh=True)
            if github_repository_access_connected(github_access):
                clear_github_repository_authorization_pending(user)
                pending = False
                bound_existing_access = True
            else:
                return pending_repositories_payload()

        if github_access and not github_repository_access_authorized_for_user(user, github_access):
            github_access = try_bind_existing_github_repository_access(user, force_refresh=True)
            bound_existing_access = bool(github_access)

        if github_repository_access_needs_aggregation_migration(user, github_access):
            migrated_access = try_bind_existing_github_repository_access(user, force_refresh=True)
            if migrated_access:
                github_access = migrated_access
                bound_existing_access = True

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

        repository_items = repository_items_for_payload(github_access)
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
            "installations": safe_installation_summaries(github_access.get("installations") or []),
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
        if authorization_token:
            return authorization_token
        raw_cookie = request_header(self, "Cookie") or ""
        cookie = SimpleCookie(raw_cookie)
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

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
        if not user:
            remember_pending_billing_update(update)
            return
        apply_billing_update_to_user(user, update)
        apply_pending_billing_updates_for_user(user)

    def send_cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        allowed = allowed_origins()
        if origin and origin in allowed:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")

    def json(self, payload: dict, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        response_headers = {**getattr(self, "_rate_limit_headers", {}), **(headers or {})}
        for key, value in response_headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str, set_cookie: str | None = None) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_cors_headers()
        self.send_header("Location", location)
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()

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
    worker.ensure_workers()
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
