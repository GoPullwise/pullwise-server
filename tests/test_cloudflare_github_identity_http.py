"""S03 synthetic OAuth/App/Session HTTP behavior, with no GitHub network calls."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pullwise_server.cloudflare_github_identity_http import handle_identity_request


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Store:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE app_state(name TEXT PRIMARY KEY,payload TEXT NOT NULL,updated_at INTEGER NOT NULL)")
            db.execute("CREATE TABLE d1_command_guard(ok INTEGER NOT NULL CHECK(ok=1))")
            db.execute("""CREATE TABLE account_entitlement_authority(
                owner_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,plan TEXT NOT NULL,
                period TEXT NOT NULL,period_start INTEGER NOT NULL,
                valid_until INTEGER NOT NULL,
                dirty INTEGER NOT NULL)""")

    def connect(self):
        db = sqlite3.connect(self.path, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        return db


class Statement:
    def __init__(self, store, sql, params=()):
        self.store, self.sql, self.params = store, sql, params

    def bind(self, *params):
        return Statement(self.store, self.sql, params)

    async def first(self):
        with self.store.connect() as db:
            row = db.execute(self.sql, self.params).fetchone()
        return dict(row) if row else None


class D1ShapedSQLite:
    def __init__(self, store):
        self.store = store

    def prepare(self, sql):
        return Statement(self.store, sql)

    async def batch(self, statements):
        with self.store.connect() as db:
            return [SimpleNamespace(results=[dict(row) for row in db.execute(item.sql, item.params).fetchall()])
                    for item in statements]


def seed(path):
    return SimpleNamespace(store=Store(path), now=1_800_000_000), None, None


class GitHubStub:
    client_id = "client-synthetic"
    app_slug = "pullwise-synthetic"

    async def exchange(self, code, redirect_uri, verifier):
        assert code == "synthetic-code"
        assert redirect_uri == "https://app.example.test/api/auth/github/callback"
        assert verifier
        return "synthetic-access-token"

    async def profile(self, token):
        assert token == "synthetic-access-token"
        return {"id": 77, "login": "alice", "name": "Alice"}

    async def seal(self, token):
        return "sealed:" + token

    async def unseal(self, token):
        assert token.startswith("sealed:")
        return token[len("sealed:"):]

    async def installations(self, token):
        assert token == "synthetic-access-token"
        return [{"id": 501, "account": {"login": "alice"}}]

    async def repositories(self, token, installation_id):
        assert installation_id == 501
        return [{"id": 202, "full_name": "alice/project"}]


def call(binding, gateway, now, method, path, params=None, headers=None):
    return asyncio.run(handle_identity_request(
        binding=binding, gateway=gateway, now=now, method=method, path=path,
        params=params or {}, headers=headers or {},
        app_url="https://app.example.test",
        callback_url="https://app.example.test/api/auth/github/callback",
        cookie_same_site="None",
        trusted_origins={"https://app.example.test"},
    ))


def login(binding, gateway, now):
    status, payload, _ = call(binding, gateway, now, "GET", "/auth/github/authorize",
        {"redirectTo": "/projects"})
    assert status == 200
    from urllib.parse import urlsplit, parse_qs
    state = parse_qs(urlsplit(payload["url"]).query)["state"][0]
    return call(binding, gateway, now + 1, "GET", "/auth/github/callback",
        {"state": state, "code": "synthetic-code"})


def test_login_state_cookie_and_callback_replay(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    binding, gateway = D1ShapedSQLite(fixture.store), GitHubStub()
    status, payload, response_headers = login(binding, gateway, fixture.now)
    assert status == 302 and payload["location"] == "https://app.example.test/projects"
    assert "HttpOnly" in response_headers["Set-Cookie"]
    assert "Secure" in response_headers["Set-Cookie"]
    assert "SameSite=None" in response_headers["Set-Cookie"]
    session_cookie = response_headers["Set-Cookie"].split(";", 1)[0]
    status, payload, _ = call(binding, gateway, fixture.now + 2, "GET", "/auth/session",
        headers={"Cookie": session_cookie})
    assert status == 200 and payload["authenticated"] is True
    assert payload["user"]["id"] == "usr_github_77"
    status, _, _ = call(binding, gateway, fixture.now + 2, "GET", "/auth/github/callback",
        {"state": "used-state", "code": "synthetic-code"})
    assert status == 400
    with fixture.store.connect() as db:
        users = json.loads(db.execute("SELECT payload FROM app_state WHERE name='users'").fetchone()[0])
        authority = db.execute("SELECT owner_id FROM account_entitlement_authority").fetchone()
    assert users["usr_github_77"]["githubAccessToken"] == "sealed:synthetic-access-token"
    assert authority["owner_id"] == "usr_github_77"


def test_installation_binding_and_lost_access_hides_repository_metadata(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    binding, gateway = D1ShapedSQLite(fixture.store), GitHubStub()
    _, _, login_headers = login(binding, gateway, fixture.now)
    cookie = login_headers["Set-Cookie"].split(";", 1)[0]
    status, payload, _ = call(binding, gateway, fixture.now + 2, "GET",
        "/integrations/github/authorize", {"redirectTo": "/projects"}, {"Cookie": cookie})
    assert status == 200
    from urllib.parse import urlsplit, parse_qs
    state = parse_qs(urlsplit(payload["url"]).query)["state"][0]
    status, payload, _ = call(binding, gateway, fixture.now + 3, "GET",
        "/integrations/github/callback", {"state": state, "installation_id": "501"}, {"Cookie": cookie})
    assert status == 302 and payload["location"] == "https://app.example.test/projects"
    status, payload, _ = call(binding, gateway, fixture.now + 4, "GET",
        "/repositories", headers={"Cookie": cookie})
    assert status == 200 and payload["items"][0]["githubRepoId"] == 202

    async def lost_repositories(_token, _installation_id):
        return []
    gateway.repositories = lost_repositories
    status, payload, _ = call(binding, gateway, fixture.now + 5, "GET",
        "/repositories", headers={"Cookie": cookie})
    assert status == 200 and payload["items"] == [] and payload["githubAccess"] == "lost"


def test_signout_requires_origin_and_revokes_session(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    binding, gateway = D1ShapedSQLite(fixture.store), GitHubStub()
    _, _, login_headers = login(binding, gateway, fixture.now)
    cookie = login_headers["Set-Cookie"].split(";", 1)[0]
    status, _, _ = call(binding, gateway, fixture.now + 2, "POST", "/auth/sign-out",
        headers={"Cookie": cookie, "Origin": "https://evil.example"})
    assert status == 403
    status, _, response_headers = call(binding, gateway, fixture.now + 3, "POST", "/auth/sign-out",
        headers={"Cookie": cookie, "Origin": "https://app.example.test"})
    assert status == 200 and "Max-Age=0" in response_headers["Set-Cookie"]
    status, payload, _ = call(binding, gateway, fixture.now + 4, "GET", "/auth/session",
        headers={"Cookie": cookie})
    assert status == 200 and payload["authenticated"] is False


class GitHubIdentityHttpTests(unittest.TestCase):
    def _run(self, check):
        with tempfile.TemporaryDirectory() as directory:
            check(Path(directory))

    def test_login_callback(self):
        self._run(test_login_state_cookie_and_callback_replay)

    def test_app_authorization_loss(self):
        self._run(test_installation_binding_and_lost_access_hides_repository_metadata)

    def test_signout(self):
        self._run(test_signout_requires_origin_and_revokes_session)
