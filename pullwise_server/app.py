from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from . import billing, db, email_delivery, github_auth, worker

logger = logging.getLogger(__name__)

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
SESSION_MAX_AGE = 60 * 60 * 24 * 30
MAGIC_LINK_MAX_AGE = 60 * 15
GITHUB_STATE_MAX_AGE = 60 * 10

USERS: dict[str, dict] = {}
SESSIONS: dict[str, dict] = {}
MAGIC_LINKS: dict[str, dict] = {}
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
MAX_BILLING_EVENT_RECORDS = 5000
MAX_BILLING_PENDING_UPDATES = 1000


class RequestBodyTooLarge(ValueError):
    pass


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def env_flag(name: str, default: str = "false") -> bool:
    return env(name, default).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


def max_body_bytes() -> int:
    return max(0, env_int("PULLWISE_MAX_BODY_BYTES", 1024 * 1024))


def local_github_mocks_enabled() -> bool:
    return env_flag("PULLWISE_ENABLE_LOCAL_GITHUB_MOCKS")


def dev_magic_links_enabled() -> bool:
    return env_flag("PULLWISE_ENABLE_DEV_MAGIC_LINKS")


def ensure_state_loaded() -> None:
    global STATE_DIRTY, STATE_LOADED, USERS, SESSIONS, MAGIC_LINKS, GITHUB_STATES, SETTINGS, BILLING_EVENTS, BILLING_PENDING_UPDATES, SCANS, ISSUES
    with STATE_LOCK:
        if STATE_LOADED:
            return

        state = db.load_state()
        USERS = dict(state.get("users") or {})
        SESSIONS = dict(state.get("sessions") or {})
        MAGIC_LINKS = dict(state.get("magicLinks") or {})
        GITHUB_STATES = dict(state.get("githubStates") or {})
        SETTINGS = dict(state.get("settings") or {})
        BILLING_EVENTS = dict(state.get("billingEvents") or {})
        BILLING_PENDING_UPDATES = list(state.get("billingPendingUpdates") or [])
        SCANS = list(state.get("scans") or [])
        ISSUES = list(state.get("issues") or [])
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
        db.save_state(
            {
                "users": USERS,
                "sessions": SESSIONS,
                "magicLinks": MAGIC_LINKS,
                "githubStates": GITHUB_STATES,
                "settings": SETTINGS,
                "billingEvents": BILLING_EVENTS,
                "billingPendingUpdates": BILLING_PENDING_UPDATES,
                "scans": SCANS,
                "issues": ISSUES,
            }
        )
        STATE_DIRTY = False


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
    return "http://localhost:3000"


def trusted_host_header(handler: BaseHTTPRequestHandler) -> str | None:
    host = first_header_value(handler, "Host") or "localhost:3000"
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
    value = handler.headers.get(name)
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


def url_origin(value: str) -> str | None:
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def safe_redirect_to(value: str | None, screen: str) -> str:
    fallback = default_redirect(screen)
    if not value:
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
        "id": user["id"],
        "name": user["name"],
        "email": user["email"],
        "avatarUrl": user.get("avatarUrl"),
        "createdAt": user["createdAt"],
        "providers": user.get("providers", []),
    }


def get_or_create_email_user(email: str) -> dict:
    user_id = "usr_email_" + re.sub(r"[^a-z0-9]+", "_", email.lower()).strip("_")
    if user_id not in USERS:
        USERS[user_id] = {
            "id": user_id,
            "name": email.split("@")[0].replace(".", " ").title(),
            "email": email,
            "avatarUrl": None,
            "createdAt": now(),
            "providers": ["email"],
            "githubRepositoryAccess": None,
        }
        mark_state_dirty()
    elif "email" not in USERS[user_id]["providers"]:
        USERS[user_id]["providers"].append("email")
        mark_state_dirty()
    return USERS[user_id]


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
    github_id = str(profile.get("id") or re.sub(r"[^a-z0-9]+", "_", login.lower()).strip("_"))
    user_id = "usr_github_" + github_id
    email = profile.get("primaryEmail") or profile.get("email") or f"{login}@users.noreply.github.com"
    if user_id not in USERS:
        USERS[user_id] = {
            "id": user_id,
            "name": profile.get("name") or login,
            "email": email,
            "avatarUrl": profile.get("avatar_url"),
            "createdAt": now(),
            "providers": ["github"],
            "githubRepositoryAccess": None,
        }
        mark_state_dirty()

    user = USERS[user_id]
    user.update(
        {
            "name": profile.get("name") or user.get("name") or login,
            "email": email,
            "avatarUrl": profile.get("avatar_url"),
            "githubId": github_id,
            "githubLogin": login,
            "githubHtmlUrl": profile.get("html_url"),
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
    user = USERS[user_id]
    return {
        "profile": {"name": user["name"], "email": user["email"]},
    }


def settings_payload(user_id: str) -> dict:
    return SETTINGS.get(user_id) or default_settings_payload(user_id)


def default_settings(user_id: str) -> dict:
    if user_id not in SETTINGS:
        SETTINGS[user_id] = default_settings_payload(user_id)
        mark_state_dirty()
    return SETTINGS[user_id]


def user_scans(session: dict | None) -> list[dict]:
    if not session:
        return []
    return [scan for scan in SCANS if scan.get("userId") == session["userId"]]


def user_issues(session: dict | None) -> list[dict]:
    if not session:
        return []
    return [issue for issue in ISSUES if issue.get("userId") == session["userId"]]


def repository_item(github_access: dict | None, full_name: str) -> dict | None:
    if not github_access:
        return None
    for item in github_access.get("repositoryItems") or []:
        if item.get("fullName") == full_name or item.get("full_name") == full_name:
            return item
    return None


def repository_is_authorized(github_access: dict | None, full_name: str) -> bool:
    if not github_access:
        return False
    repositories = github_access.get("repositories") or []
    if repositories:
        return full_name in repositories
    return repository_item(github_access, full_name) is not None


def has_real_github_identity(user: dict | None) -> bool:
    if not user:
        return False
    if not github_auth.oauth_configured():
        return "github" in user.get("providers", [])
    return bool(user.get("githubAccessToken"))


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
    repositories_connected = bool(repo_access)
    return {
        "authenticated": True,
        "user": user_public(user),
        "github": {
            "identityConnected": has_real_github_identity(user),
            "login": user.get("githubLogin"),
            "repositoriesConnected": repositories_connected,
            "repositoryScope": repo_access.get("scope") if repo_access else None,
            "authorizedAt": repo_access.get("authorizedAt") if repo_access else None,
            "installationId": repo_access.get("installationId") if repo_access else None,
            "repositorySelection": repo_access.get("repositorySelection") if repo_access else None,
            "repositoryCount": len(repo_access.get("repositories", [])) if repo_access else 0,
        },
        "nextStep": "choose_repositories" if repositories_connected else "connect_github_repositories",
    }


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
        print(f"{self.address_string()} - {fmt % args}")

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

        try:
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
            })
        if path == "/dev/magic-links":
            if not dev_magic_links_enabled():
                return self.error(HTTPStatus.NOT_IMPLEMENTED, "Local magic links are disabled. Set PULLWISE_ENABLE_DEV_MAGIC_LINKS=true for explicit local development.")
            links = []
            for token, record in MAGIC_LINKS.items():
                if record["expiresAt"] >= now():
                    links.append({
                        "email": record["email"],
                        "expiresAt": record["expiresAt"],
                        "url": f"{api_base_url(self)}/auth/email/callback?{urlencode({'token': token})}",
                    })
            return self.json({"items": links, "magicLinks": links})
        if path == "/auth/session":
            return self.json(session_payload(self.current_session()))
        if path == "/auth/github/authorize":
            return self.handle_github_authorize(params)
        if path == "/auth/github/callback":
            return self.handle_github_callback(params)
        if path == "/auth/email/callback":
            return self.handle_magic_callback(params)
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
            scans = user_scans(session)
            return self.json({"items": scans, "scans": scans})
        if len(segments) == 2 and segments[0] == "scans":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing scans.")
            return self.json(self.find_or_404(user_scans(session), segments[1], "Scan"))
        if path == "/issues":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing issues.")
            issues = user_issues(session)
            scan_id = params.get("scanId")
            if scan_id:
                issues = [issue for issue in issues if issue.get("scanId") == scan_id]
            return self.json({"items": issues, "issues": issues})
        if len(segments) == 2 and segments[0] == "issues":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing issues.")
            return self.json(self.find_or_404(user_issues(session), segments[1], "Issue"))
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
                payload["account"] = user.get("billing") or {"status": "none"}
            return self.json(payload)
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_post(self, path: str, params: dict, segments: list[str]) -> None:
        if path == "/webhooks/stripe":
            return self.handle_stripe_webhook()
        if path == "/webhooks/creem":
            return self.handle_creem_webhook()
        body = self.read_json()
        if path == "/auth/email/magic-link":
            return self.handle_magic_link(body)
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
            repository = str(body.get("repo") or body.get("repository") or "").strip()
            if not repository:
                return self.error(HTTPStatus.BAD_REQUEST, "A repository is required to start a scan.")
            scan_error: tuple[int, str] | None = None
            scan = None
            with STATE_LOCK:
                user = USERS.get(session["userId"]) or {}
                github_access = user.get("githubRepositoryAccess")
                if not github_access:
                    scan_error = (HTTPStatus.FORBIDDEN, "Authorize GitHub repositories before starting a scan.")
                elif not repository_is_authorized(github_access, repository):
                    scan_error = (HTTPStatus.FORBIDDEN, "Repository is not authorized for this GitHub App installation.")
                else:
                    repo_meta = repository_item(github_access, repository) or {}
                    scan = {
                        "id": make_id("sc"),
                        "repo": repository,
                        "branch": body.get("branch") or repo_meta.get("defaultBranch") or "main",
                        "commit": body.get("commit") or "pending",
                        "status": "queued",
                        "userId": session["userId"],
                        "createdAt": now(),
                        "progress": 0,
                        "phase": None,
                        "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                        "installationId": github_access.get("installationId"),
                        "installationAccount": github_access.get("installationAccount"),
                        "repositorySelection": github_access.get("repositorySelection"),
                        "cloneUrl": repo_meta.get("cloneUrl") or repo_meta.get("clone_url"),
                        "repositoryPrivate": bool(repo_meta.get("private")),
                        "repoPath": None,
                        "by": "you",
                    }
                    SCANS.insert(0, scan)
                    mark_state_dirty()

            if scan_error:
                return self.error(scan_error[0], scan_error[1])
            if scan is None:
                return self.error(HTTPStatus.INTERNAL_SERVER_ERROR, "Unable to create scan.")
            worker.start_scan(scan["id"])
            return self.json(scan, HTTPStatus.CREATED)
        if len(segments) == 3 and segments[0] == "scans" and segments[2] == "cancel":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before cancelling a scan.")
            with STATE_LOCK:
                scan = self.find_or_404(user_scans(session), segments[1], "Scan")
                scan["status"] = "cancelled"
                mark_state_dirty()
            return self.json(scan)
        if len(segments) == 4 and segments[0] == "issues" and segments[2] == "fixes" and segments[3] == "apply":
            return self.error(HTTPStatus.NOT_IMPLEMENTED, "Applying fixes is not implemented on this backend.")
        if len(segments) == 3 and segments[0] == "issues" and segments[2] == "pull-requests":
            return self.error(HTTPStatus.NOT_IMPLEMENTED, "Pull request creation is not implemented on this backend.")
        if len(segments) == 2 and segments[0] == "integrations":
            return self.error(HTTPStatus.NOT_IMPLEMENTED, f"{segments[1]} integration writes are not implemented on this backend.")
        if path == "/billing/checkout-sessions":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before starting checkout.")
            user = USERS[session["userId"]]
            checkout = billing.create_checkout_session(
                user,
                success_url=safe_redirect_to(body.get("successUrl"), "settings"),
                cancel_url=safe_redirect_to(body.get("cancelUrl"), "settings"),
            )
            user["billingCheckout"] = {
                "provider": checkout.get("provider"),
                "id": checkout.get("id"),
                "requestId": checkout.get("requestId"),
                "createdAt": now(),
            }
            mark_state_dirty()
            return self.json(checkout)
        if path == "/billing/portal-sessions":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before opening the billing portal.")
            portal = billing.create_portal_session(
                USERS[session["userId"]],
                return_url=safe_redirect_to(body.get("returnUrl"), "settings"),
            )
            return self.json(portal)
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_patch(self, segments: list[str]) -> None:
        body = self.read_json()
        if len(segments) == 3 and segments[0] == "issues" and segments[2] == "status":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before updating issue status.")
            issue = self.find_or_404(user_issues(session), segments[1], "Issue")
            issue["status"] = body.get("status") or issue["status"]
            mark_state_dirty()
            return self.json(issue)
        if len(segments) == 1 and segments[0] == "settings":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before updating settings.")
            settings = default_settings(session["userId"])
            settings.update(body)
            mark_state_dirty()
            return self.json(settings)
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_delete(self, segments: list[str]) -> None:
        if len(segments) == 2 and segments[0] == "integrations":
            session = self.current_session()
            if segments[1] != "github":
                return self.error(HTTPStatus.NOT_IMPLEMENTED, f"{segments[1]} integration disconnect is not implemented on this backend.")
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before disconnecting GitHub.")
            USERS[session["userId"]]["githubRepositoryAccess"] = None
            mark_state_dirty()
            return self.json({"ok": True, "provider": "github", "connected": False})
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_github_authorize(self, params: dict) -> None:
        redirect_to = safe_redirect_to(params.get("redirectTo"), "oauth")
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
            return self.redirect(safe_redirect_to(params.get("redirectTo"), "oauth"), cookie_header(session["id"]))

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
        if github_auth.oauth_configured() and not has_real_github_identity(user):
            return self.error(HTTPStatus.UNAUTHORIZED, "Sign in with GitHub before authorizing repositories.")

        state = remember_github_state("install", redirect_to, userId=session["userId"], requestedScope=scope)
        return self.json({"url": github_auth.build_app_install_url(state), "mode": "github-app"})

    def handle_magic_link(self, body: dict) -> None:
        dev_mode = dev_magic_links_enabled()
        if not dev_mode and not email_delivery.email_delivery_configured():
            return self.error(HTTPStatus.NOT_IMPLEMENTED, "Email magic links require SMTP configuration. Set PULLWISE_EMAIL_PROVIDER=smtp, PULLWISE_SMTP_HOST, and PULLWISE_EMAIL_FROM.")
        email = str(body.get("email") or "").strip().lower()
        if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
            return self.error(HTTPStatus.BAD_REQUEST, "A valid email is required.")
        redirect_to = safe_redirect_to(body.get("redirectTo"), "oauth")
        token = secrets.token_urlsafe(24)
        MAGIC_LINKS[token] = {"email": email, "redirectTo": redirect_to, "expiresAt": now() + MAGIC_LINK_MAX_AGE}
        mark_state_dirty()
        magic_link = f"{api_base_url(self)}/auth/email/callback?{urlencode({'token': token})}"
        if dev_mode:
            return self.json({"ok": True, "email": email, "devMagicLink": magic_link, "magicLink": magic_link, "expiresInSeconds": MAGIC_LINK_MAX_AGE})

        try:
            email_delivery.send_magic_link_email(email, magic_link)
        except Exception:
            MAGIC_LINKS.pop(token, None)
            mark_state_dirty()
            raise
        return self.json({"ok": True, "email": email, "sent": True, "expiresInSeconds": MAGIC_LINK_MAX_AGE})

    def handle_magic_callback(self, params: dict) -> None:
        token = params.get("token") or ""
        record = MAGIC_LINKS.pop(token, None)
        if record:
            mark_state_dirty()
        if not record or record["expiresAt"] < now():
            return self.error(HTTPStatus.BAD_REQUEST, "Magic link is invalid or expired.")
        user = get_or_create_email_user(record["email"])
        session = create_session(user)
        return self.redirect(record["redirectTo"], cookie_header(session["id"]))

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
        if not params.get("installation_id"):
            return self.redirect(
                redirect_with_params(str(record["redirectTo"]), {"github_error": "missing_installation_id"})
            )
        user = USERS.get(str(record["userId"]))
        if not user:
            raise ValueError("The GitHub installation belongs to a user session that no longer exists.")

        if params.get("setup_action") == "request":
            return self.redirect(
                redirect_with_params(str(record["redirectTo"]), {"github_error": "github_app_installation_not_completed"})
            )

        installation_id = str(params["installation_id"])
        user_can_access = github_auth.user_can_access_installation(user.get("githubAccessToken"), installation_id)
        if user_can_access is False:
            raise ValueError("The signed-in GitHub user cannot access this GitHub App installation.")
        if github_auth.oauth_configured() and user_can_access is not True:
            raise ValueError("Unable to verify access to this GitHub App installation. Try signing in with GitHub again.")

        installation = {}
        repository_items = []
        if github_auth.app_api_configured():
            installation = github_auth.fetch_installation(installation_id)
            repository_items = github_auth.list_installation_repositories(installation_id)

        repository_selection = installation.get("repository_selection") or params.get("scope") or record.get("requestedScope") or "selected"
        account = installation.get("account") or {}
        user["githubRepositoryAccess"] = {
            "mode": "github-app",
            "scope": "all" if repository_selection == "all" else "selected",
            "repositorySelection": repository_selection,
            "authorizedAt": now(),
            "installationId": installation_id,
            "installationAccount": account.get("login"),
            "installationTargetType": installation.get("target_type"),
            "repositories": [repo["fullName"] for repo in repository_items],
            "repositoryItems": repository_items,
            "repositoriesNeedSync": not github_auth.app_api_configured(),
        }
        mark_state_dirty()
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
        github = {
            "provider": "github",
            "connected": bool(github_access),
            "mode": github_access.get("mode") if github_access else None,
            "scope": github_access.get("scope") if github_access else None,
            "repositorySelection": github_access.get("repositorySelection") if github_access else None,
            "installationId": github_access.get("installationId") if github_access else None,
            "installationAccount": github_access.get("installationAccount") if github_access else None,
            "repositories": github_access.get("repositories") if github_access else [],
            "repositoriesNeedSync": github_access.get("repositoriesNeedSync") if github_access else False,
        }
        items = [github]
        return {"items": items, "github": github}

    def repositories_payload(self, refresh: bool = False) -> dict:
        session = self.current_session()
        if not session:
            return {"items": [], "repositories": [], "needsAuthorization": True}

        user = USERS.get(session["userId"])
        github_access = user.get("githubRepositoryAccess") if user else None
        if not github_access:
            return {"items": [], "repositories": [], "needsAuthorization": True}

        if refresh and github_access.get("mode") == "github-app" and github_auth.app_api_configured():
            repository_items = github_auth.list_installation_repositories(str(github_access["installationId"]))
            github_access["repositoryItems"] = repository_items
            github_access["repositories"] = [repo["fullName"] for repo in repository_items]
            github_access["repositoriesNeedSync"] = False
            github_access["syncedAt"] = now()
            mark_state_dirty()

        repository_items = github_access.get("repositoryItems") or []
        return {
            "items": repository_items,
            "repositories": repository_items,
            "needsAuthorization": False,
            "installationId": github_access.get("installationId"),
            "repositorySelection": github_access.get("repositorySelection"),
            "installationAccount": github_access.get("installationAccount"),
            "repositoriesNeedSync": github_access.get("repositoriesNeedSync", False),
        }

    def repositories_connected(self) -> bool:
        session = self.current_session()
        if not session:
            return False
        return bool(USERS[session["userId"]].get("githubRepositoryAccess"))

    def current_or_demo_session(self) -> dict:
        session = self.current_session()
        if session:
            return session
        user = get_or_create_email_user(env("PULLWISE_DEV_EMAIL", "taylor@acme.io"))
        return create_session(user)

    def current_session(self) -> dict | None:
        session_id = self.current_session_id()
        if not session_id:
            return None
        session = SESSIONS.get(session_id)
        if not session:
            return None
        if session["expiresAt"] < now():
            SESSIONS.pop(session_id, None)
            mark_state_dirty()
            return None
        user = USERS.get(session["userId"])
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
        raw_cookie = self.headers.get("Cookie") or ""
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
        raise ValueError(f"{label} not found: {item_id}")

    def read_json(self) -> dict:
        raw_bytes = self.read_raw_body()
        if not raw_bytes:
            return {}
        raw = raw_bytes.decode("utf-8")
        if not raw.strip():
            return {}
        return json.loads(raw)

    def read_raw_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return b""
        if length > max_body_bytes():
            raise RequestBodyTooLarge("Request body is too large.")
        return self.rfile.read(length)

    def enforce_body_size_limit(self, method: str) -> None:
        if method not in {"POST", "PATCH"}:
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > max_body_bytes():
            raise RequestBodyTooLarge("Request body is too large.")

    def handle_creem_webhook(self) -> None:
        raw = self.read_raw_body()
        if not billing.verify_creem_webhook(raw, self.headers.get("creem-signature")):
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid Creem webhook signature.")
        event = json.loads(raw.decode("utf-8") or "{}")
        update = billing.billing_update_from_creem_event(event)
        if update:
            self.apply_billing_update(update)
        return self.json({"received": True})

    def handle_stripe_webhook(self) -> None:
        raw = self.read_raw_body()
        if not billing.verify_stripe_webhook(raw, self.headers.get("Stripe-Signature")):
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid Stripe webhook signature.")
        event = json.loads(raw.decode("utf-8") or "{}")
        update = billing.billing_update_from_stripe_event(event)
        if update:
            self.apply_billing_update(update)
        return self.json({"received": True})

    def apply_billing_update(self, update: dict) -> None:
        user = USERS.get(update.get("userId") or "")
        if not user and update.get("customerId"):
            for candidate in USERS.values():
                if (candidate.get("billing") or {}).get("customerId") == update.get("customerId"):
                    user = candidate
                    break
        if not user:
            return
        current = user.get("billing") or {}
        user["billing"] = {
            **current,
            "provider": update.get("provider") or current.get("provider"),
            "customerId": update.get("customerId") or current.get("customerId"),
            "customerEmail": update.get("customerEmail") or current.get("customerEmail"),
            "subscriptionId": update.get("subscriptionId") or current.get("subscriptionId"),
            "status": update.get("status") or current.get("status") or "active",
            "updatedAt": now(),
            "lastEventType": update.get("eventType"),
        }
        mark_state_dirty()

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
        for key, value in (headers or {}).items():
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
    parser = argparse.ArgumentParser(description="Run the Pullwise local API server.")
    parser.add_argument("--host", default=env("PULLWISE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(env("PULLWISE_PORT", "3000")))
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), PullwiseHandler)
    print(f"Pullwise API listening on http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
