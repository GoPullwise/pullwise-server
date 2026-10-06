"""S03 synthetic OAuth/App/Session HTTP behavior, with no GitHub network calls."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
import pytest
from pathlib import Path
from types import SimpleNamespace

from pullwise_server.cloudflare_github_identity_http import (
    _repo_items, _user, _write_user, handle_identity_request,
)
from pullwise_server.cloudflare_state_records import record_name


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
        user = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?",
                                    (record_name("users", "usr_github_77"),)).fetchone()[0])
        authority = db.execute("SELECT owner_id FROM account_entitlement_authority").fetchone()
    assert user["githubAccessToken"] == "sealed:synthetic-access-token"
    assert authority["owner_id"] == "usr_github_77"


@pytest.mark.parametrize("with_query", [False, True])
@pytest.mark.parametrize("oauth_error", ["access_denied", None])
def test_oauth_failure_query_preserves_workspace_invitation_fragment(tmp_path, with_query, oauth_error):
    from urllib.parse import parse_qs, urlsplit
    fixture, _, _ = seed(tmp_path / "oauth-fragment.db")
    binding = D1ShapedSQLite(fixture.store)
    class NoExchange(GitHubStub):
        async def exchange(self, *args):
            raise AssertionError("Denied/missing-code callback must not call GitHub")
    gateway = NoExchange()
    fragment = "invite=pwi_" + "Q" * 43
    destination = "/members" + ("?tab=invites" if with_query else "") + "#" + fragment
    status, payload, _ = call(binding, gateway, fixture.now, "GET", "/auth/github/authorize",
                              {"redirectTo": destination})
    assert status == 200
    state = parse_qs(urlsplit(payload["url"]).query)["state"][0]
    params = {"state": state}
    if oauth_error:
        params["error"] = oauth_error
    status, payload, headers = call(binding, gateway, fixture.now + 1, "GET", "/auth/github/callback", params)
    assert status == 302 and "Set-Cookie" not in headers
    location = urlsplit(payload["location"])
    assert location.scheme == "https" and location.netloc == "app.example.test" and location.path == "/members"
    assert location.fragment == fragment
    query = parse_qs(location.query)
    assert query["github_error"] == [oauth_error or "missing_oauth_code"]
    assert query.get("tab") == (["invites"] if with_query else None)
    assert headers["Location"] == payload["location"]
    assert call(binding, gateway, fixture.now + 2, "GET", "/auth/github/callback", params)[0] == 400
    with fixture.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM account_entitlement_authority").fetchone()[0] == 0
        assert db.execute("SELECT payload FROM app_state WHERE name LIKE 'record:users:%' OR name LIKE 'record:sessions:%'").fetchall() == []


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


def test_preinstalled_authorized_repositories_are_visible_without_setup_callback(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    binding, gateway = D1ShapedSQLite(fixture.store), GitHubStub()
    _, _, login_headers = login(binding, gateway, fixture.now)
    cookie = login_headers["Set-Cookie"].split(";", 1)[0]
    with fixture.store.connect() as db:
        db.execute("""CREATE TABLE api_keys(id TEXT PRIMARY KEY,user_id TEXT,name TEXT,
            key_prefix TEXT,key_hash TEXT UNIQUE,scopes TEXT,expires_at INTEGER,
            restrictions TEXT,created_at INTEGER,last_used_at INTEGER,revoked_at INTEGER)""")
        before = db.execute("SELECT payload FROM app_state WHERE name=?",
                            (record_name("users", "usr_github_77"),)).fetchone()[0]
    status, payload, _ = call(binding, gateway, fixture.now + 2, "GET",
        "/api/v1/repositories", headers={"Cookie": cookie})
    assert status == 200 and payload["githubAccess"] == "authorized"
    assert payload["items"][0]["githubRepoId"] == 202
    with fixture.store.connect() as db:
        assert db.execute("SELECT payload FROM app_state WHERE name=?",
                          (record_name("users", "usr_github_77"),)).fetchone()[0] == before


def test_explicit_sync_uses_live_github_access_and_requires_trusted_cookie_origin(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    binding, gateway = D1ShapedSQLite(fixture.store), GitHubStub()
    _, _, login_headers = login(binding, gateway, fixture.now)
    cookie = login_headers["Set-Cookie"].split(";", 1)[0]
    status, payload, _ = call(binding, gateway, fixture.now + 2, "POST",
        "/repositories/sync", headers={"Cookie": cookie, "Origin": "https://evil.example"})
    assert status == 403
    status, payload, _ = call(binding, gateway, fixture.now + 2, "POST",
        "/repositories/sync", headers={"Cookie": cookie, "Origin": "https://app.example.test"})
    assert status == 200 and payload["items"][0]["githubRepoId"] == 202
    assert payload["needsAuthorization"] is False


def test_live_grants_cover_multiple_installations_with_bounded_provider_calls():
    from pullwise_server.cloudflare_github_identity_http import read_repository_access
    class Multiple(GitHubStub):
        async def installations(self, token):
            return [{"id": 501}, {"id": 502}]

        async def repositories(self, token, installation_id):
            return [{"id": installation_id, "full_name": f"alice/repo-{installation_id}"}]
    user = {"githubAccessToken": "sealed:synthetic-access-token"}
    access = asyncio.run(read_repository_access(user, Multiple()))
    assert [item["githubRepoId"] for item in access["items"]] == [501, 502]
    class Excessive(Multiple):
        async def installations(self, token):
            return [{"id": index + 1} for index in range(11)]

        async def repositories(self, *_args):
            raise AssertionError("Excessive installations must stop before repository calls")
    import pytest
    with pytest.raises(ValueError, match="excessive"):
        asyncio.run(read_repository_access(user, Excessive()))


def test_project_eligibility_uses_live_grants_without_cached_callback():
    from pullwise_server.cloudflare_ledger_api import _live_repos
    user = {"githubAccessToken": "sealed:synthetic-access-token"}
    assert asyncio.run(_live_repos(user, GitHubStub())) == {202: "alice/project"}
    class Revoked(GitHubStub):
        async def installations(self, token):
            return []
    assert asyncio.run(_live_repos(user, Revoked())) == {}


def test_repository_sync_does_not_change_user_or_session_state(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    binding, gateway = D1ShapedSQLite(fixture.store), GitHubStub()
    _, _, login_headers = login(binding, gateway, fixture.now)
    cookie = login_headers["Set-Cookie"].split(";", 1)[0]
    with fixture.store.connect() as db:
        before = [tuple(row) for row in db.execute("SELECT * FROM app_state ORDER BY name")]
    status, payload, _ = call(binding, gateway, fixture.now + 2, "POST", "/repositories/sync",
        headers={"Cookie": cookie, "Origin": "https://app.example.test"})
    assert status == 200 and len(payload["items"]) == 1
    with fixture.store.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM app_state ORDER BY name")] == before


def test_account_record_preserves_full_repository_cache_billing_history_and_opaque_token(tmp_path):
    from pullwise_server.billing_account_rules import (
        append_billing_subscription_event, upsert_billing_subscription_record,
    )
    fixture, _, _ = seed(tmp_path / "large-account.db")
    binding = D1ShapedSQLite(fixture.store)
    user = {"id": "usr_github_77", "githubId": "77", "githubLogin": "alice",
            "githubAccessToken": "sealed:synthetic-access-token", "createdAt": fixture.now}
    account = {"id": 77, "login": "a" * 100, "type": "Organization"}
    cache = _repo_items([{"id": index + 1, "full_name": "a" * 100 + "/" + "r" * 100}
                         for index in range(1000)], 501, account)
    user["githubRepositoryAccess"] = {"status": "authorized", "repositoryItems": cache}
    for index in range(100):
        update = {"provider": "creem", "subscriptionId": f"synthetic-subscription-{index % 25}",
                  "customerId": "synthetic-customer", "customerEmail": "synthetic@example.invalid",
                  "eventId": f"synthetic-event-{index}", "eventType": "subscription.updated",
                  "eventCreated": fixture.now + index, "status": "active", "plan": "pro",
                  "interval": "month", "currentPeriodStart": fixture.now,
                  "currentPeriodEnd": fixture.now + 86400}
        upsert_billing_subscription_record(user, update, processed_at=fixture.now + index)
        append_billing_subscription_event(user, update, update, processed_at=fixture.now + index)
    asyncio.run(_write_user(binding, user, fixture.now, expected_user={}))
    status, _, _ = login(binding, GitHubStub(), fixture.now + 100)
    assert status == 302
    current = asyncio.run(_user(binding, user["id"]))
    assert current["githubRepositoryAccess"] == user["githubRepositoryAccess"]
    assert current["billingSubscriptions"] == user["billingSubscriptions"]
    assert current["billingSubscriptionEvents"] == user["billingSubscriptionEvents"]
    assert current["githubAccessToken"] == "sealed:synthetic-access-token"
    assert len(current["billingSubscriptionEvents"]) == 100
    with fixture.store.connect() as db:
        raw = db.execute("SELECT payload FROM app_state WHERE name=?",
                         (record_name("users", user["id"]),)).fetchone()[0]
        assert 8192 < len(raw.encode("utf-8")) <= 512 * 1024
        assert db.execute("SELECT payload FROM app_state WHERE name='users'").fetchone() is None


def test_user_record_byte_bound_rejects_oversize_before_write(tmp_path):
    fixture, _, _ = seed(tmp_path / "oversize-account.db")
    binding = D1ShapedSQLite(fixture.store)
    user = {"id": "usr_github_77", "githubAccessToken": "sealed:synthetic-access-token"}
    asyncio.run(_write_user(binding, user, fixture.now, expected_user={}))
    with fixture.store.connect() as db:
        before = list(db.iterdump())
    with pytest.raises(ValueError, match="byte bound"):
        asyncio.run(_write_user(binding, {**user, "retained": "界" * 180000}, fixture.now + 1,
                                expected_user=user))
    with fixture.store.connect() as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("same_account", [False, True])
def test_user_write_cas_isolated_to_own_account_and_rejects_concurrent_own_change(tmp_path, same_account):
    fixture, _, _ = seed(tmp_path / "concurrent-account.db")
    binding = D1ShapedSQLite(fixture.store)
    user = {"id": "usr_github_77", "name": "Original"}
    other = {"id": "usr_github_88", "name": "Other"}
    asyncio.run(_write_user(binding, user, fixture.now, expected_user={}))
    asyncio.run(_write_user(binding, other, fixture.now, expected_user={}))
    target = user if same_account else other
    concurrent = {**target, "name": "Concurrent"}
    class Concurrent(D1ShapedSQLite):
        async def batch(self, statements):
            with self.store.connect() as db:
                db.execute("UPDATE app_state SET payload=? WHERE name=?",
                           (json.dumps(concurrent), record_name("users", target["id"])))
            return await super().batch(statements)
    write = _write_user(Concurrent(fixture.store), {**user, "name": "Updated"}, fixture.now + 1,
                       expected_user=user)
    if same_account:
        with pytest.raises(sqlite3.IntegrityError):
            asyncio.run(write)
    else:
        asyncio.run(write)
    assert asyncio.run(_user(binding, target["id"])) == concurrent
    assert asyncio.run(_user(binding, user["id"]))["name"] == ("Concurrent" if same_account else "Updated")
    with fixture.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM d1_command_guard").fetchone()[0] == 0


def test_explicit_legacy_cutover_preserves_identity_without_implicit_read_writes(tmp_path):
    from state_record_fixtures import normalize_legacy_state
    fixture, _, _ = seed(tmp_path / "legacy-account.db")
    user = {"id": "usr_github_77", "githubId": "77", "githubLogin": "alice", "name": "Alice",
            "providers": ["github"], "githubAccessToken": "sealed:synthetic-access-token"}
    session = {"id": "ses-synthetic", "userId": user["id"], "expiresAt": fixture.now + 600}
    with fixture.store.connect() as db:
        for name, value in (("users", {user["id"]: user}), ("sessions", {session["id"]: session})):
            db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
                       (name, json.dumps(value), fixture.now))
        before = list(db.iterdump())
    class ReadOnly(D1ShapedSQLite):
        def __init__(self, store):
            super().__init__(store)
            self.actors = []

        def observe_authenticated_actor(self, actor_id):
            self.actors.append(actor_id)

        def prepare(self, sql):
            assert sql.lstrip().upper().startswith("SELECT"), sql
            return super().prepare(sql)
    binding = ReadOnly(fixture.store)
    headers = {"Cookie": "pw_session=" + "x" * 4097 + "; pw_session=" + session["id"]}
    assert call(binding, GitHubStub(), fixture.now, "GET", "/auth/session", headers=headers)[1]["authenticated"] is False
    assert binding.actors == []
    with fixture.store.connect() as db:
        assert list(db.iterdump()) == before
        assert normalize_legacy_state(db, now=fixture.now) == 2
    with fixture.store.connect() as db:
        assert normalize_legacy_state(db, now=fixture.now) == 0
    with fixture.store.connect() as db:
        after = list(db.iterdump())
    assert call(binding, GitHubStub(), fixture.now, "GET", "/auth/session", headers=headers)[1]["authenticated"] is True
    assert binding.actors == [user["id"]]
    assert asyncio.run(_user(binding, user["id"])) == user
    with fixture.store.connect() as db:
        assert list(db.iterdump()) == after


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
