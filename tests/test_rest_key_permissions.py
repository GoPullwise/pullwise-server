"""Current REST key scopes intersect real SQL identity, role and target fences.

All identities, payment facts and model answers are synthetic. These tests call
canonical route handlers over isolated SQLite D1 transactions, with no network.
"""
import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from ledger_d1_fixture import D1ShapedSQLite, Store
from pullwise_server.cloudflare_api_key_write import create_api_key
from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_ledger_auth import ledger_principal
from pullwise_server.cloudflare_ledger_profile import read_ledger_me
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1
from pullwise_server.cloudflare_state_records import encode_record, record_name
from pullwise_server.ledger_plan_policy import JEV_RESERVATION_MICROUSD


NOW = 1_800_000_000
OWNER = "rest_owner"
SCOPES = ("profile:read", "projects:read", "categories:read", "expenses:read",
          "reports:read", "projects:write", "categories:write", "expenses:write",
          "suggestions:use")
# Product expectations are independent of ROLE_SCOPES in the implementation.
ROLE_SCOPES = {
    "owner": SCOPES,
    "admin": SCOPES,
    "editor": SCOPES[:5] + ("expenses:write", "suggestions:use"),
    "viewer": SCOPES[:5],
}
READ_ROUTES = (("profile:read", "/api/v1/me"),
               ("projects:read", "/api/v1/projects"),
               ("categories:read", "/api/v1/categories"),
               ("expenses:read", "/api/v1/expenses"),
               ("reports:read", "/api/v1/reports/summary"))


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class ClosingStore(Store):
    def connect(self):
        connection = sqlite3.connect(self.path, factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


class NoGitHub:
    def __getattr__(self, name):
        raise AssertionError("Standalone/key authorization called GitHub: " + name)


class SyntheticModel:
    enabled = True

    def __init__(self):
        self.calls = 0

    async def evaluate(self, request):
        self.calls += 1
        category = next(value for value in request["questions"]["category"]["criteria"]
                        if value != "uncertain")
        return json.dumps({"model": "jev-1.13.0", "answers": {
            "category": {"type": "choice", "choice": category, "confidence": 0.95,
                         "probabilities": {category: 0.95, "uncertain": 0.05}},
            "target": {"type": "choice", "choice": "shared", "confidence": 0.95,
                       "probabilities": {"shared": 0.95, "project": 0.03, "uncertain": 0.02}}},
            "usage": {"input_tokens": 10, "output_tokens": 5}}).encode()


@pytest.fixture
def rest_db(tmp_path):
    store = ClosingStore(tmp_path / "rest-permissions.sqlite")
    root = Path(__file__).resolve().parents[1] / "cloudflare/server/migrations"
    actors = {role: OWNER if role == "owner" else "rest_" + role for role in ROLE_SCOPES}
    with store.connect() as db:
        for migration in sorted(root.glob("*.sql")):
            db.executescript(migration.read_text())
        for role, actor in actors.items():
            account = {"id": actor, "createdAt": NOW - 1000, "name": role,
                       "billing": {"plan": "max" if role == "owner" else "free",
                                   "status": "active", "subscriptionId": "sub_synthetic_" + actor,
                                   "currentPeriodStart": NOW - 1000,
                                   "currentPeriodEnd": NOW + 1000}}
            session_id = "rest-session-" + role
            session = {"userId": actor, "expiresAt": NOW + 1000}
            for kind, identifier, record in (("users", actor, account),
                                              ("sessions", session_id, session)):
                db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
                           (record_name(kind, identifier), encode_record(kind, identifier, record), NOW))
            if role != "owner":
                db.execute("INSERT INTO workspace_members VALUES(?,?,?,1,'joined','updated',NULL,?)",
                           (OWNER, actor, role, OWNER))
        db.execute("INSERT INTO expense_categories(id,owner_id,name,created_at,updated_at) "
                   "VALUES('cat_rest',?,'Hosting','synthetic','synthetic')", (OWNER,))
    return SimpleNamespace(store=store, actors=actors,
                           binding=PlanLimitedD1(D1ShapedSQLite(store), now=NOW))


def cookie(role="owner"):
    return {"Cookie": "pw_session=rest-session-" + role,
            "X-Pullwise-Workspace": OWNER, "Origin": "https://app.example.test"}


def issue(app, scopes, *, role="owner", restrictions=None):
    return asyncio.run(create_api_key(binding=app.binding, headers=cookie(role),
        body={"scopes": list(scopes), "restrictions": {
            "workspaceId": OWNER, "shared": True, **(restrictions or {})}}, now=NOW))


def credentials(key, transport="bearer"):
    return ({"Authorization": "Bearer " + key["key"]} if transport == "bearer"
            else {"X-Pullwise-Api-Key": key["key"]})


def route(app, method, path, headers, body=None, model=None):
    if method == "GET" and path == "/api/v1/me":
        return asyncio.run(read_ledger_me(binding=app.binding, headers=headers, now=NOW))
    return asyncio.run(handle_ledger_request(binding=app.binding, gateway=NoGitHub(),
        suggestion_gateway=model, method=method, path=path, headers=headers,
        params={}, body=body, now=NOW))


def facts(app):
    with app.store.connect() as db:
        return tuple(db.execute("SELECT (SELECT COUNT(*) FROM ledger_projects),"
            "(SELECT COUNT(*) FROM expenses),(SELECT COUNT(*) FROM expense_events),"
            "(SELECT COUNT(*) FROM expense_suggestion_events),"
            "(SELECT COUNT(*) FROM expense_suggestion_budget),"
            "(SELECT COALESCE(SUM(writes),0) FROM ledger_plan_usage),"
            "(SELECT COUNT(*) FROM d1_command_guard)").fetchone())


@pytest.mark.parametrize("role", tuple(ROLE_SCOPES))
@pytest.mark.parametrize("scope", SCOPES)
def test_all_nine_key_scopes_intersect_four_issuing_roles(rest_db, role, scope):
    before = facts(rest_db)
    status, key = issue(rest_db, [scope], role=role)
    if scope not in ROLE_SCOPES[role]:
        assert (status, key["error"]["code"]) == (403, "ROLE_FORBIDDEN")
        assert facts(rest_db) == before
        with rest_db.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 0
        return
    assert status == 201 and key["scopes"] == [scope]
    assert key["userId"] == rest_db.actors[role]
    async def authorize():
        proof = {}
        user, _, commands, validate = await ledger_principal(binding=rest_db.binding,
            headers=credentials(key), scope=scope, now=NOW, target_kind="shared", proof=proof)
        validate([part.results for part in await rest_db.binding.batch(commands)])
        assert user["id"] == OWNER and user["_actor"]["id"] == rest_db.actors[role]
        assert user["_workspace"]["role"] == role and proof["key"]["user_id"] == rest_db.actors[role]
    after_issue = facts(rest_db)
    asyncio.run(authorize())
    assert facts(rest_db) == after_issue


@pytest.mark.parametrize("transport", ["bearer", "header"])
@pytest.mark.parametrize("scope,path", READ_ROUTES)
def test_each_read_route_requires_its_own_key_scope(rest_db, transport, scope, path):
    status, key = issue(rest_db, [value for value in SCOPES if value != scope])
    assert status == 201
    before = facts(rest_db)
    status, body = route(rest_db, "GET", path, credentials(key, transport))
    assert (status, body["error"]["code"]) == (403, "INSUFFICIENT_SCOPE")
    assert facts(rest_db) == before


@pytest.mark.parametrize("transport", ["bearer", "header"])
@pytest.mark.parametrize("scope,path", READ_ROUTES)
def test_each_read_route_accepts_only_its_own_key_scope(rest_db, transport, scope, path):
    status, key = issue(rest_db, [scope])
    assert status == 201
    before = facts(rest_db)
    assert route(rest_db, "GET", path, credentials(key, transport))[0] == 200
    assert facts(rest_db) == before


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_projects_write_key_creates_and_updates_standalone_without_read_scope(rest_db, role):
    status, key = issue(rest_db, ["projects:write"], role=role)
    assert status == 201
    headers = credentials(key)
    status, project = route(rest_db, "POST", "/api/v1/projects", headers, {"name": "Key costs"})
    assert status == 201 and project["githubRepoId"] is None and project["githubRepoIds"] == []
    status, updated = route(rest_db, "PATCH", "/api/v1/projects/" + project["id"],
                            {**headers, "If-Match": '"1"'}, {"description": "Key update"})
    assert status == 200 and updated["revision"] == 2 and updated["id"] == project["id"]
    with rest_db.store.connect() as db:
        assert tuple(db.execute("SELECT owner_id,name FROM ledger_projects").fetchone()) == (OWNER, "Key costs")
        assert [tuple(row) for row in db.execute("SELECT owner_id,writes FROM ledger_plan_usage")] == [(OWNER, 3)]


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_categories_write_key_creates_updates_and_archives_without_read_scope(rest_db, role):
    status, key = issue(rest_db, ["categories:write"], role=role)
    assert status == 201
    headers = credentials(key)
    status, category = route(rest_db, "POST", "/api/v1/categories", headers, {"name": "Key category"})
    assert status == 201
    path = "/api/v1/categories/" + category["id"]
    assert route(rest_db, "PATCH", path, {**headers, "If-Match": '"1"'}, {"name": "Renamed"})[0] == 200
    assert route(rest_db, "DELETE", path, {**headers, "If-Match": '"2"'})[0] == 204
    with rest_db.store.connect() as db:
        saved = db.execute("SELECT owner_id,name,archived_at FROM expense_categories WHERE id=?", (category["id"],)).fetchone()
        assert saved["owner_id"] == OWNER and saved["name"] == "Renamed" and saved["archived_at"] is not None


def test_project_write_scope_retains_allowlist_and_workspace_fences(rest_db):
    projects = [route(rest_db, "POST", "/api/v1/projects", cookie(), {"name": name})[1]
                for name in ("Allowed", "Forbidden")]
    status, key = issue(rest_db, ["projects:write"], role="admin",
                        restrictions={"projectIds": [projects[0]["id"]], "shared": False})
    assert status == 201
    headers = {**credentials(key), "If-Match": '"1"'}
    assert route(rest_db, "PATCH", "/api/v1/projects/" + projects[0]["id"], headers,
                 {"description": "Allowed"})[0] == 200
    before = facts(rest_db)
    for method, path, body, expected in (
        ("PATCH", "/api/v1/projects/" + projects[1]["id"], {"description": "Denied"}, "TARGET_FORBIDDEN"),
        ("POST", "/api/v1/projects", {"name": "Outside fixed allowlist"}, "TARGET_FORBIDDEN")):
        status, payload = route(rest_db, method, path, headers, body)
        assert (status, payload["error"]["code"]) == (403, expected)
    status, payload = route(rest_db, "POST", "/api/v1/projects",
        {**headers, "X-Pullwise-Workspace": rest_db.actors["admin"]}, {"name": "Other ledger"})
    assert (status, payload["error"]["code"]) == (403, "WORKSPACE_FORBIDDEN")
    assert facts(rest_db) == before


@pytest.mark.parametrize("role", ["owner", "admin", "editor"])
def test_suggestions_use_key_can_get_and_confirm_synthetic_answer_without_financial_write(rest_db, role):
    status, key = issue(rest_db, ["suggestions:use"], role=role)
    assert status == 201
    model = SyntheticModel()
    headers = credentials(key)
    before = facts(rest_db)
    status, answer = route(rest_db, "POST", "/api/v1/expense-suggestions", headers,
                           {"target": {"kind": "shared"}, "purpose": "Hosting"}, model)
    assert status == 200 and answer["status"] == "available" and model.calls == 1
    assert answer["suggestions"] == {"categoryId": "cat_rest", "targetKind": "shared"}
    status, _ = route(rest_db, "POST", "/api/v1/expense-suggestions/" + answer["suggestionId"] + "/decision",
                      headers, {"target": {"kind": "shared"}, "categoryId": "cat_rest"}, model)
    assert status == 204 and model.calls == 1
    after = facts(rest_db)
    assert after[:3] == before[:3] and after[5:] == before[5:]
    assert after[3:5] == (1, 0)
    with rest_db.store.connect() as db:
        assert tuple(db.execute("SELECT accepted_category_id,accepted_target_kind FROM expense_suggestion_events").fetchone()) == ("cat_rest", "shared")
        assert db.execute("SELECT jev_reserved_microusd FROM ledger_plan_usage").fetchone()[0] == JEV_RESERVATION_MICROUSD


def test_expense_write_key_cannot_use_advanced_suggestions_without_use_scope(rest_db):
    status, key = issue(rest_db, ["expenses:write"])
    assert status == 201
    model, before = SyntheticModel(), facts(rest_db)
    status, payload = route(rest_db, "POST", "/api/v1/expense-suggestions", credentials(key),
                            {"target": {"kind": "shared"}, "purpose": "Hosting"}, model)
    assert (status, payload["error"]["code"]) == (403, "INSUFFICIENT_SCOPE")
    assert model.calls == 0 and facts(rest_db) == before


@pytest.mark.parametrize("target", [{"kind": "shared"}, {"kind": "project", "projectId": "prj_outside"}])
def test_suggestions_key_target_restrictions_precede_model_and_writes(rest_db, target):
    status, key = issue(rest_db, ["suggestions:use"], role="editor",
                        restrictions={"projectIds": ["prj_allowed"], "shared": False})
    assert status == 201
    model, before = SyntheticModel(), facts(rest_db)
    status, payload = route(rest_db, "POST", "/api/v1/expense-suggestions", credentials(key),
                            {"target": target, "purpose": "Hosting"}, model)
    assert (status, payload["error"]["code"]) == (403, "TARGET_FORBIDDEN")
    assert model.calls == 0 and facts(rest_db) == before


def test_alternate_key_header_write_and_mixed_credentials_fail_closed(rest_db):
    status, key = issue(rest_db, ["projects:write"])
    assert status == 201
    assert route(rest_db, "POST", "/api/v1/projects", credentials(key, "header"), {"name": "Header key"})[0] == 201
    before = facts(rest_db)
    status, payload = route(rest_db, "POST", "/api/v1/projects",
                            {**credentials(key, "header"), **cookie()}, {"name": "Ambiguous"})
    assert (status, payload["error"]["code"]) == (400, "AMBIGUOUS_AUTH")
    assert facts(rest_db) == before
