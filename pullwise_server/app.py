from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

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

USERS: dict[str, dict] = {}
SESSIONS: dict[str, dict] = {}
MAGIC_LINKS: dict[str, dict] = {}
SETTINGS: dict[str, dict] = {}

REPOSITORIES = [
    {
        "id": "r6",
        "name": "billing-service",
        "fullName": "yourname/billing-service",
        "desc": "Internal billing and invoicing service",
        "description": "Internal billing and invoicing service",
        "lang": "Go",
        "private": True,
        "stars": "-",
        "branches": 31,
        "defaultBranch": "main",
        "updated": "an hour ago",
    },
    {
        "id": "r2",
        "name": "frontend-app",
        "fullName": "acme-inc/frontend-app",
        "desc": "Customer-facing React application",
        "description": "Customer-facing React application",
        "lang": "TypeScript",
        "private": True,
        "stars": "42",
        "branches": 18,
        "defaultBranch": "main",
        "updated": "today",
    },
    {
        "id": "r3",
        "name": "api-gateway",
        "fullName": "acme-inc/api-gateway",
        "desc": "Public API gateway and auth edge",
        "description": "Public API gateway and auth edge",
        "lang": "TypeScript",
        "private": True,
        "stars": "18",
        "branches": 9,
        "defaultBranch": "main",
        "updated": "yesterday",
    },
    {
        "id": "r4",
        "name": "portfolio-2025",
        "fullName": "yourname/portfolio-2025",
        "desc": "Personal portfolio site",
        "description": "Personal portfolio site",
        "lang": "TypeScript",
        "private": False,
        "stars": "128",
        "branches": 6,
        "defaultBranch": "main",
        "updated": "2 days ago",
    },
]

ISSUES = [
    {
        "id": "PW-101",
        "repo": "yourname/billing-service",
        "title": "Hardcoded API key leaked into frontend bundle",
        "summary": "A secret-looking API key is bundled in client-side code.",
        "severity": "critical",
        "category": "security",
        "status": "open",
        "file": "lib/payments.ts",
        "line": 14,
        "confidence": 0.97,
        "autoFixable": True,
    },
    {
        "id": "PW-102",
        "repo": "yourname/billing-service",
        "title": "SQL string concatenation enables injection",
        "summary": "A query is built from unsanitized user input.",
        "severity": "critical",
        "category": "security",
        "status": "open",
        "file": "routes/search.ts",
        "line": 42,
        "confidence": 0.93,
        "autoFixable": True,
    },
    {
        "id": "PW-103",
        "repo": "acme-inc/frontend-app",
        "title": "Dashboard triggers N+1 data fetch",
        "summary": "The dashboard fetches child resources in a render loop.",
        "severity": "high",
        "category": "performance",
        "status": "open",
        "file": "src/screens/dashboard.jsx",
        "line": 88,
        "confidence": 0.84,
        "autoFixable": False,
    },
]

SCANS = [
    {
        "id": "sc_demo_1",
        "repo": "yourname/billing-service",
        "branch": "main",
        "commit": "a3f9c2",
        "status": "done",
        "createdAt": "2026-05-07T08:00:00Z",
        "durationMs": 72148,
        "issues": {"critical": 2, "high": 1, "medium": 0, "low": 0},
    }
]


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def allowed_origins() -> set[str]:
    raw = env(
        "PULLWISE_ALLOWED_ORIGINS",
        "http://localhost:5173,http://localhost:5174,http://127.0.0.1:5173,http://127.0.0.1:5174",
    )
    return {item.strip() for item in raw.split(",") if item.strip()}


def api_base_url(handler: BaseHTTPRequestHandler) -> str:
    configured = os.environ.get("PULLWISE_API_BASE_URL")
    if configured:
        return configured.rstrip("/")
    host = handler.headers.get("Host", "localhost:3000")
    return f"http://{host}"


def default_redirect(screen: str) -> str:
    app_url = env("PULLWISE_APP_URL", "http://localhost:5173").rstrip("/")
    return f"{app_url}/?screen={screen}"


def now() -> int:
    return int(time.time())


def make_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(8)}"


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
    elif "email" not in USERS[user_id]["providers"]:
        USERS[user_id]["providers"].append("email")
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
    elif "github" not in USERS[user_id]["providers"]:
        USERS[user_id]["providers"].append("github")
    return USERS[user_id]


def create_session(user: dict) -> dict:
    session_id = make_id("ses")
    session = {
        "id": session_id,
        "userId": user["id"],
        "createdAt": now(),
        "expiresAt": now() + SESSION_MAX_AGE,
    }
    SESSIONS[session_id] = session
    return session


def default_settings(user_id: str) -> dict:
    if user_id not in SETTINGS:
        SETTINGS[user_id] = {
            "profile": {"name": USERS[user_id]["name"], "email": USERS[user_id]["email"], "role": "Engineering Lead"},
            "notifications": {"email": True, "slack": False, "criticalOnly": False},
            "scan": {"defaultBranch": "main", "autoScan": True, "autoFixConfidence": 0.8},
        }
    return SETTINGS[user_id]


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
    providers = user.get("providers", []) if user else []
    repositories_connected = bool(repo_access)
    return {
        "authenticated": True,
        "user": user_public(user),
        "github": {
            "identityConnected": "github" in providers,
            "login": user.get("githubLogin"),
            "repositoriesConnected": repositories_connected,
            "repositoryScope": repo_access.get("scope") if repo_access else None,
            "authorizedAt": repo_access.get("authorizedAt") if repo_access else None,
        },
        "nextStep": "choose_repositories" if repositories_connected else "connect_github_repositories",
    }


def cookie_header(session_id: str) -> str:
    return f"{SESSION_COOKIE}={session_id}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_MAX_AGE}"


def clear_cookie_header() -> str:
    return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


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
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        segments = [part for part in path.split("/") if part]

        try:
            if method == "GET":
                return self.handle_get(path, params, segments)
            if method == "POST":
                return self.handle_post(path, params, segments)
            if method == "PATCH":
                return self.handle_patch(segments)
            if method == "DELETE":
                return self.handle_delete(segments)
            return self.error(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed")
        except ValueError as exc:
            return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            return self.error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Server error: {exc}")

    def handle_get(self, path: str, params: dict, segments: list[str]) -> None:
        if path == "/health":
            return self.json({"ok": True, "service": "pullwise-server", "time": now(), "mode": env("PULLWISE_MODE", "local")})
        if path == "/dev/magic-links":
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
            redirect_to = params.get("redirectTo") or default_redirect("oauth")
            callback = f"{api_base_url(self)}/auth/github/callback?{urlencode({'redirectTo': redirect_to})}"
            return self.json({"url": callback})
        if path == "/auth/github/callback":
            user = get_or_create_github_user()
            session = create_session(user)
            return self.redirect(params.get("redirectTo") or default_redirect("oauth"), cookie_header(session["id"]))
        if path == "/auth/email/callback":
            return self.handle_magic_callback(params)
        if path == "/integrations":
            return self.json(self.integrations_payload())
        if path == "/integrations/github/authorize":
            scope = params.get("scope") or "all"
            redirect_to = params.get("redirectTo") or default_redirect("repos")
            callback = f"{api_base_url(self)}/integrations/github/callback?{urlencode({'scope': scope, 'redirectTo': redirect_to})}"
            return self.json({"url": callback})
        if path == "/integrations/github/callback":
            return self.handle_github_repository_callback(params)
        if path == "/repositories":
            return self.json({"items": REPOSITORIES, "repositories": REPOSITORIES, "needsAuthorization": not self.repositories_connected()})
        if path == "/scans":
            return self.json({"items": SCANS, "scans": SCANS})
        if len(segments) == 2 and segments[0] == "scans":
            return self.json(self.find_or_404(SCANS, segments[1], "Scan"))
        if path == "/issues":
            return self.json({"items": ISSUES, "issues": ISSUES})
        if len(segments) == 2 and segments[0] == "issues":
            return self.json(self.find_or_404(ISSUES, segments[1], "Issue"))
        if path == "/settings":
            session = self.current_or_demo_session()
            return self.json(default_settings(session["userId"]))
        if path == "/billing/plan":
            return self.json({"plan": "free", "status": "active", "scansUsed": 12, "scansLimit": 100})
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_post(self, path: str, params: dict, segments: list[str]) -> None:
        body = self.read_json()
        if path == "/auth/email/magic-link":
            return self.handle_magic_link(body)
        if path == "/auth/sign-out":
            return self.json({"ok": True}, headers={"Set-Cookie": clear_cookie_header()})
        if path == "/repositories/sync":
            return self.json({"ok": True, "items": REPOSITORIES, "repositories": REPOSITORIES, "syncedAt": now()})
        if path == "/scans":
            scan = {
                "id": make_id("sc"),
                "repo": body.get("repo") or body.get("repository") or "yourname/billing-service",
                "branch": body.get("branch") or "main",
                "commit": body.get("commit") or "pending",
                "status": "queued",
                "createdAt": now(),
                "issues": None,
            }
            SCANS.insert(0, scan)
            return self.json(scan, HTTPStatus.CREATED)
        if len(segments) == 3 and segments[0] == "scans" and segments[2] == "cancel":
            scan = self.find_or_404(SCANS, segments[1], "Scan")
            scan["status"] = "cancelled"
            return self.json(scan)
        if len(segments) == 4 and segments[0] == "issues" and segments[2] == "fixes" and segments[3] == "apply":
            issue = self.find_or_404(ISSUES, segments[1], "Issue")
            return self.json({"ok": True, "issue": issue, "branch": body.get("branch") or f"fix/{issue['id'].lower()}"})
        if len(segments) == 3 and segments[0] == "issues" and segments[2] == "pull-requests":
            issue = self.find_or_404(ISSUES, segments[1], "Issue")
            return self.json({"ok": True, "issue": issue, "url": f"https://github.com/{issue['repo']}/pull/482"}, HTTPStatus.CREATED)
        if len(segments) == 2 and segments[0] == "integrations":
            return self.json({"ok": True, "provider": segments[1], "connected": True, "payload": body})
        if path == "/billing/checkout-sessions":
            return self.json({"url": "https://billing.stripe.test/checkout/pullwise"}, HTTPStatus.CREATED)
        if path == "/billing/portal-sessions":
            return self.json({"url": "https://billing.stripe.test/portal/pullwise"}, HTTPStatus.CREATED)
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_patch(self, segments: list[str]) -> None:
        body = self.read_json()
        if len(segments) == 3 and segments[0] == "issues" and segments[2] == "status":
            issue = self.find_or_404(ISSUES, segments[1], "Issue")
            issue["status"] = body.get("status") or issue["status"]
            return self.json(issue)
        if len(segments) == 1 and segments[0] == "settings":
            session = self.current_or_demo_session()
            settings = default_settings(session["userId"])
            settings.update(body)
            return self.json(settings)
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_delete(self, segments: list[str]) -> None:
        if len(segments) == 2 and segments[0] == "integrations":
            session = self.current_session()
            if session and segments[1] == "github":
                USERS[session["userId"]]["githubRepositoryAccess"] = None
            return self.json({"ok": True, "provider": segments[1], "connected": False})
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_magic_link(self, body: dict) -> None:
        email = str(body.get("email") or "").strip().lower()
        if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
            return self.error(HTTPStatus.BAD_REQUEST, "A valid email is required.")
        redirect_to = body.get("redirectTo") or default_redirect("oauth")
        token = secrets.token_urlsafe(24)
        MAGIC_LINKS[token] = {"email": email, "redirectTo": redirect_to, "expiresAt": now() + MAGIC_LINK_MAX_AGE}
        magic_link = f"{api_base_url(self)}/auth/email/callback?{urlencode({'token': token})}"
        return self.json({"ok": True, "email": email, "devMagicLink": magic_link, "magicLink": magic_link, "expiresInSeconds": MAGIC_LINK_MAX_AGE})

    def handle_magic_callback(self, params: dict) -> None:
        token = params.get("token") or ""
        record = MAGIC_LINKS.pop(token, None)
        if not record or record["expiresAt"] < now():
            return self.error(HTTPStatus.BAD_REQUEST, "Magic link is invalid or expired.")
        user = get_or_create_email_user(record["email"])
        session = create_session(user)
        return self.redirect(record["redirectTo"], cookie_header(session["id"]))

    def handle_github_repository_callback(self, params: dict) -> None:
        session = self.current_or_demo_session()
        scope = params.get("scope") or "all"
        USERS[session["userId"]]["githubRepositoryAccess"] = {
            "scope": scope,
            "authorizedAt": now(),
            "installationId": "dev_installation_1",
            "repositories": [repo["fullName"] for repo in REPOSITORIES] if scope == "all" else [REPOSITORIES[0]["fullName"]],
        }
        return self.redirect(params.get("redirectTo") or default_redirect("repos"), cookie_header(session["id"]))

    def integrations_payload(self) -> dict:
        session = self.current_session()
        user = USERS.get(session["userId"]) if session else None
        github_access = user.get("githubRepositoryAccess") if user else None
        github = {
            "provider": "github",
            "connected": bool(github_access),
            "scope": github_access.get("scope") if github_access else None,
            "repositories": github_access.get("repositories") if github_access else [],
        }
        items = [github, {"provider": "slack", "connected": False}, {"provider": "linear", "connected": False}]
        return {"items": items, "github": github}

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
        raw_cookie = self.headers.get("Cookie") or ""
        cookie = SimpleCookie(raw_cookie)
        morsel = cookie.get(SESSION_COOKIE)
        if not morsel:
            return None
        session = SESSIONS.get(morsel.value)
        if not session or session["expiresAt"] < now():
            return None
        return session

    def find_or_404(self, collection: list[dict], item_id: str, label: str) -> dict:
        for item in collection:
            if item.get("id") == item_id:
                return item
        raise ValueError(f"{label} not found: {item_id}")

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        if not raw.strip():
            return {}
        return json.loads(raw)

    def send_cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        allowed = allowed_origins()
        if origin and (origin in allowed or "*" in allowed):
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

