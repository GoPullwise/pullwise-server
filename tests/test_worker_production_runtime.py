"""Production ingress executes the real owner/auth/quota application locally."""
import asyncio
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from test_cloudflare_github_identity_http import D1ShapedSQLite, Store
from test_worker_application_security import Response, request
from state_record_fixtures import normalize_legacy_state
from pullwise_server.cloudflare_state_records import record_name, encode_record


ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000
TOKEN = "pwk_production_runtime_synthetic"
ORIGIN = "https://app.example.test"


def load_entry():
    spec = importlib.util.spec_from_file_location("production_runtime_entry",
        ROOT / "cloudflare/server/src/entry.py")
    entry = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"workers": SimpleNamespace(Response=Response,
            WorkerEntrypoint=object, DurableObject=object)}):
        spec.loader.exec_module(entry)
    return entry


class NoPreviewEnvironment(SimpleNamespace):
    def __getattr__(self, name):
        if name == "VALIDATION_BUDGET" or name.startswith("PULLWISE_PREVIEW_"):
            raise AssertionError("Production touched preview controls")
        raise AttributeError(name)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    entry = load_entry()
    monkeypatch.setattr(entry.time, "time", lambda: NOW)
    store = Store(tmp_path / "production-local.db")
    with store.connect() as db:
        for migration in sorted((ROOT / "cloudflare/server/migrations").glob("*.sql")):
            db.executescript(migration.read_text())
        users = {owner: {"id": owner, "createdAt": NOW, "billing": {"plan": "free"}}
                 for owner in ("owner", "other")}
        sessions = {"own-session": {"userId": "owner", "expiresAt": NOW+3600}}
        db.execute("UPDATE app_state SET payload=? WHERE name='users'", (json.dumps(users),))
        db.execute("UPDATE app_state SET payload=? WHERE name='sessions'", (json.dumps(sessions),))
        scopes = ["profile:read", "categories:read", "categories:write",
                  "expenses:read", "expenses:write", "reports:read"]
        db.execute("""INSERT INTO api_keys(id,user_id,name,key_prefix,key_hash,scopes,
            restrictions,created_at) VALUES(?,?,?,?,?,?,?,?)""", (
            "key", "owner", "Local synthetic", TOKEN[:16], hashlib.sha256(TOKEN.encode()).hexdigest(),
            json.dumps(scopes), '{"shared":true}', NOW))
        for owner in users:
            db.execute("""INSERT INTO expense_categories(id,owner_id,name,created_at,updated_at)
                VALUES(?,?,?,?,?)""", ("cat_"+owner, owner, owner+" category", "local", "local"))
        db.execute("""INSERT INTO expenses(id,owner_id,target_kind,category_id,occurred_on,
            amount_minor,currency,purpose,created_at,updated_at)
            VALUES('exp_other','other','shared','cat_other','2026-09-27',999,'USD',
                'Other private record','local','local')""")
        normalize_legacy_state(db, now=NOW)
    worker = entry.Default()
    worker.env = NoPreviewEnvironment(PULLWISE_MODE="production", PULLWISE_D1_ACCESS_ENABLED="1",
        PULLWISE_APP_URL=ORIGIN, PULLWISE_ALLOWED_ORIGINS=ORIGIN, DB=D1ShapedSQLite(store),
        PULLWISE_JEV_SUGGESTIONS_ENABLED="0", PULLWISE_JEV_SUGGESTIONS_EVALUATED="0")
    return SimpleNamespace(entry=entry, worker=worker, store=store)


def call(runtime, path, *, method="GET", body=None, headers=None):
    return asyncio.run(runtime.worker.fetch(request(path, method=method, headers=headers,
        raw=json.dumps(body).encode() if body is not None else b"")))


def cookie(**headers):
    return {"cookie": "pw_session=own-session", **headers}


@pytest.mark.parametrize("headers", [cookie(), {"authorization": "Bearer "+TOKEN}])
def test_production_reaches_authenticated_owner_application_without_preview(runtime, headers):
    profile = call(runtime, "/api/v1/me", headers=headers)
    assert profile.status == 200 and profile.payload["id"] == "owner"
    assert profile.payload["entitlements"]["plan"] == "free"
    assert profile.payload["entitlements"]["jev"]["available"] is False
    categories = call(runtime, "/api/v1/categories", headers=headers)
    assert categories.status == 200
    assert [item["id"] for item in categories.payload] == ["cat_owner"]
    expenses = call(runtime, "/api/v1/expenses", headers=headers)
    assert expenses.status == 200 and expenses.payload == {"items": [], "nextCursor": None}
    other = call(runtime, "/api/v1/expenses/exp_other", headers=headers)
    assert other.status == 404 and other.payload == {"error": {"code": "NOT_FOUND"}}
    budget = call(runtime, "/_preview/budget", headers=headers)
    assert budget.status == 404


@pytest.mark.parametrize("headers", [{}, {"cookie": "pw_session=expired"},
    {"authorization": "Bearer pwk_unknown"}])
def test_production_authentication_is_required(runtime, headers):
    response = call(runtime, "/api/v1/me", headers=headers)
    assert response.status == 401
    assert response.payload["error"]["code"] == "UNAUTHENTICATED"


def test_production_cookie_origin_and_native_business_quota_remain_atomic(runtime):
    runtime.worker.env.PULLWISE_PLAN_LIMITS_JSON = '{"free":{"records":1}}'
    draft = {"target": {"kind": "shared"}, "categoryId": "cat_owner",
        "occurredOn": "2026-09-27", "currency": "USD", "amount": "0.01", "purpose": "Local QA"}
    denied = call(runtime, "/api/v1/expenses", method="POST", body=draft,
        headers=cookie(**{"idempotency-key": "bad-origin", "origin": "https://hostile.test"}))
    assert denied.status == 403 and denied.payload["error"]["code"] == "UNTRUSTED_ORIGIN"
    own = call(runtime, "/api/v1/expenses", method="POST", body=draft,
        headers=cookie(**{"idempotency-key": "first", "origin": ORIGIN}))
    assert own.status == 201 and own.payload["amountMinor"] == 1
    assert own.headers["ETag"] == '"1"'
    limited = call(runtime, "/api/v1/expenses", method="POST", body=draft,
        headers={"authorization": "Bearer "+TOKEN, "idempotency-key": "second"})
    assert limited.status == 403 and limited.payload["error"]["code"] == "RECORD_LIMIT"
    replay = call(runtime, "/api/v1/expenses", method="POST", body=draft,
        headers={"authorization": "Bearer "+TOKEN, "idempotency-key": "first"})
    assert replay.status == 201 and replay.payload["id"] == own.payload["id"]
    report = call(runtime, "/api/v1/reports/summary", headers=cookie())
    assert report.status == 200
    assert {(group["target"], group["amountMinor"]) for group in report.payload["groups"]} == {
        ("account", 1), ("shared", 1)}
    with runtime.store.connect() as db:
        assert tuple(db.execute("SELECT records,writes FROM ledger_plan_usage WHERE owner_id='owner'").fetchone()) == (1, 1)
        assert db.execute("SELECT COUNT(*) FROM expense_events WHERE owner_id='owner'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM expense_create_idempotency WHERE owner_id='owner'").fetchone()[0] == 1


def test_production_key_shared_restrictions_remain_enforced(runtime):
    with runtime.store.connect() as db:
        db.execute("UPDATE api_keys SET restrictions=?", ('{"projectIds":[],"shared":false}',))
    response = call(runtime, "/api/v1/reports/summary?target=shared",
        headers={"authorization": "Bearer "+TOKEN})
    assert response.status == 403 and response.payload["error"]["code"] == "TARGET_FORBIDDEN"


@pytest.mark.parametrize("field,value", [("revoked_at", NOW), ("expires_at", NOW-1)])
def test_production_revoked_or_expired_key_cannot_read_business_data(runtime, field, value):
    with runtime.store.connect() as db:
        db.execute(f"UPDATE api_keys SET {field}=?", (value,))
    response = call(runtime, "/api/v1/categories", headers={"authorization": "Bearer "+TOKEN})
    assert response.status == 401 and response.payload["error"]["code"] == "UNAUTHENTICATED"


def test_production_provider_failure_does_not_expose_preview_diagnostics(runtime, monkeypatch):
    from pullwise_server.cloudflare_github_gateway import GitHubFailure
    class FailedGitHub:
        def __init__(self, env):
            pass
        async def unseal(self, token):
            failure = GitHubFailure("GITHUB_UNAVAILABLE", 503)
            failure.args = ("private-provider-response-secret",)
            raise failure
    with runtime.store.connect() as db:
        name = record_name("users", "owner")
        user = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?", (name,)).fetchone()[0])
        user["githubAccessToken"] = "synthetic-sealed-token"
        db.execute("UPDATE app_state SET payload=? WHERE name=?", (encode_record("users", "owner", user), name))
    monkeypatch.setattr(runtime.entry, "WorkerGitHubGateway", FailedGitHub)
    response = call(runtime, "/api/v1/repositories", headers=cookie())
    assert response.status == 503 and response.payload == {"error": {"code": "GITHUB_UNAVAILABLE"}}


def test_production_never_initializes_a_missing_ledger_schema(tmp_path):
    entry = load_entry()
    store = Store(tmp_path / "unmigrated-local.db")
    with store.connect() as db:
        before = [tuple(row) for row in db.execute("SELECT name,sql FROM sqlite_master ORDER BY name")]
    worker = entry.Default()
    worker.env = NoPreviewEnvironment(PULLWISE_MODE="production", PULLWISE_D1_ACCESS_ENABLED="1",
        DB=D1ShapedSQLite(store))
    response = asyncio.run(worker.fetch(request("/health", method="GET")))
    assert response.status == 503 and response.payload == {"ok": False, "service": "pullwise-server"}
    with store.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT name,sql FROM sqlite_master ORDER BY name")] == before


def test_production_invalid_policy_fails_closed_without_secret_details(runtime):
    runtime.worker.env.PULLWISE_PLAN_LIMITS_JSON = "private-invalid-policy-secret"
    response = call(runtime, "/api/v1/me", headers=cookie())
    assert response.status == 503 and response.payload == {"error": {"code": "PLAN_POLICY_INVALID"}}


def test_production_broken_database_configuration_returns_safe_unavailable():
    entry = load_entry()
    class BrokenEnvironment(NoPreviewEnvironment):
        @property
        def DB(self):
            raise RuntimeError("private-database-configuration-secret")
    worker = entry.Default()
    worker.env = BrokenEnvironment(PULLWISE_MODE="production", PULLWISE_D1_ACCESS_ENABLED="1")
    response = asyncio.run(worker.fetch(request("/api/v1/me", method="GET")))
    assert response.status == 503 and response.payload == {"error": {"code": "SERVER_UNAVAILABLE"}}


@pytest.mark.parametrize("mode", ["", "unknown", "Production", None, [], {}])
def test_unknown_modes_never_access_database_or_preview(mode):
    entry = load_entry()
    class UnknownEnvironment:
        PULLWISE_D1_ACCESS_ENABLED = "1"
        PULLWISE_MODE = mode
        def __getattr__(self, name):
            raise AssertionError("Unknown mode accessed "+name)
    worker = entry.Default()
    worker.env = UnknownEnvironment()
    response = asyncio.run(worker.fetch(request("/api/v1/me", method="GET")))
    assert response.status == 503 and response.payload["error"]["code"] == "VALIDATION_CONTROL_REQUIRED"
