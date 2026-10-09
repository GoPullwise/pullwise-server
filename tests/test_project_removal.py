"""Project removal requires the actual Owner and retains financial facts."""
import asyncio
import json

import pytest

from pullwise_server.cloudflare_api_key_write import create_api_key
from pullwise_server.cloudflare_state_records import encode_record, record_name
from test_cloudflare_github_identity_http import D1ShapedSQLite
from test_project_repositories import create, ledger, member, update
from test_standalone_projects import add_usage_meter, blank, category, expense_body

OWNER = "usr_github_77"


def remove(ledger, project, *, headers=None, revision=None):
    return ledger.call("DELETE", "/api/v1/projects/" + project["id"],
        headers={**(headers or ledger.headers), "If-Match": '"' + str(revision or project["revision"]) + '"'})


def rows(ledger):
    with ledger.store.connect() as db:
        return {table: [tuple(row) for row in db.execute("SELECT * FROM " + table)] for table in (
            "ledger_projects", "ledger_project_repositories", "expenses", "expense_events",
            "expense_create_idempotency", "expense_recurring_rules", "expense_recurring_occurrences",
            "ledger_activity_events", "d1_command_guard")}


def test_owner_removal_preserves_financial_rows_and_releases_repository_anchors(ledger):
    # Legacy linked projects legitimately have an empty financial name.
    project = create(ledger, ids=[101, 102])
    assert project["name"] == ""
    draft = expense_body(category(ledger), project)
    status, expense = ledger.call("POST", "/api/v1/expenses", draft,
        {**ledger.headers, "Idempotency-Key": "retained-removal"})
    assert status == 201
    before = rows(ledger)
    ledger.gateway.calls.clear()
    assert remove(ledger, project) == (204, None)
    assert ledger.gateway.calls == []
    after = rows(ledger)
    for table in ("expenses", "expense_events", "expense_create_idempotency", "expense_recurring_rules", "expense_recurring_occurrences"):
        assert after[table] == before[table]
    with ledger.store.connect() as db:
        saved = dict(db.execute("SELECT * FROM ledger_projects WHERE id=?", (project["id"],)).fetchone())
        assert saved["deleted_at"] and saved["status"] == "archived" and saved["revision"] == 2
        assert saved["name"] == "" and saved["description"] == project["description"]
        assert saved["github_repo_id"] is saved["github_full_name"] is saved["github_organization_id"] is None
        assert db.execute("SELECT COUNT(*) FROM ledger_project_repositories WHERE project_id=?", (project["id"],)).fetchone()[0] == 0
        events = list(db.execute("SELECT * FROM ledger_activity_events WHERE action='delete' AND resource_kind='project'"))
        assert len(events) == 1
        event = dict(events[0])
        assert json.loads(event["actor_json"])["userId"] == OWNER
        assert json.loads(event["before_json"])["githubRepoIds"] == [101, 102]
        assert event["after_json"] is None
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert ledger.call("GET", "/api/v1/projects/" + project["id"])[0] == 404
    assert update(ledger, {**project, "revision": 2}, {"status": "active"})[0] == 404
    assert all(item["id"] != project["id"] for item in ledger.call("GET", "/api/v1/projects")[1]["items"])
    assert ledger.call("GET", "/api/v1/expenses/" + expense["id"])[0] == 404
    assert ledger.call("GET", "/api/v1/activity", params={"target": "project", "projectId": project["id"]})[0] == 404
    replacement = create(ledger, ids=[101, 102], name="Replacement")
    assert replacement["id"] != project["id"]
    assert remove(ledger, project) == (204, None)
    with ledger.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ledger_activity_events WHERE action='delete'").fetchone()[0] == 1


@pytest.mark.parametrize("role", ["admin", "editor", "viewer"])
def test_only_selected_ledger_owner_can_remove_project(ledger, role):
    project = blank(ledger)
    headers = member(ledger, role)
    before = rows(ledger)
    assert remove(ledger, project, headers=headers)[0] == 403
    assert rows(ledger) == before
    assert ledger.gateway.calls == []


@pytest.mark.parametrize("transport", ["Authorization", "X-Pullwise-Api-Key"])
def test_owner_api_key_with_project_write_scope_removes_only_allowed_project(ledger, transport):
    project = blank(ledger)
    outside = blank(ledger, "Outside key allowlist")
    status, result = asyncio.run(create_api_key(binding=ledger.binding, headers=ledger.headers,
        body={"name": "Owner client", "scopes": ["projects:write"],
            "restrictions": {"projectIds": [project["id"]]}}, now=ledger.now + 3))
    assert status == 201
    value = "Bearer " + result["key"] if transport == "Authorization" else result["key"]
    headers = {transport: value}
    before = rows(ledger)
    assert remove(ledger, outside, headers=headers) == (403, {"error": {"code": "TARGET_FORBIDDEN"}})
    assert rows(ledger) == before
    assert remove(ledger, project, headers=headers) == (204, None)
    with ledger.store.connect() as db:
        assert db.execute("SELECT deleted_at FROM ledger_projects WHERE id=?", (project["id"],)).fetchone()[0]
        actor = json.loads(db.execute("SELECT actor_json FROM ledger_activity_events WHERE action='delete'").fetchone()[0])
        assert actor["userId"] == OWNER and actor["kind"] == "api_key"


def test_admin_api_key_cannot_remove_project(ledger):
    project = blank(ledger)
    admin = member(ledger, "admin")
    status, result = asyncio.run(create_api_key(binding=ledger.binding,
        headers={**admin, "X-Pullwise-Workspace": OWNER}, body={"scopes": ["projects:write"]}, now=ledger.now + 3))
    assert status == 201
    before = rows(ledger)
    assert remove(ledger, project, headers={"Authorization": "Bearer " + result["key"]}) == (
        403, {"error": {"code": "PROJECT_OWNER_REQUIRED"}})
    assert rows(ledger) == before


@pytest.mark.parametrize("race", ["revoke", "expire", "restriction", "scope"])
def test_owner_key_removal_rechecks_credential_in_atomic_write(ledger, race):
    project = blank(ledger)
    status, result = asyncio.run(create_api_key(binding=ledger.binding, headers=ledger.headers,
        body={"scopes": ["projects:write"], "restrictions": {"projectIds": [project["id"]]}}, now=ledger.now + 3))
    assert status == 201
    before = rows(ledger)

    class Racing(D1ShapedSQLite):
        raced = False
        async def batch(self, statements):
            statements = list(statements)
            if not self.raced and any(item.sql.lstrip().startswith("UPDATE ledger_projects SET deleted_at=") for item in statements):
                self.raced = True
                with self.store.connect() as db:
                    if race == "revoke":
                        db.execute("UPDATE api_keys SET revoked_at=? WHERE id=?", (ledger.now, result["id"]))
                    elif race == "expire":
                        db.execute("UPDATE api_keys SET expires_at=? WHERE id=?", (ledger.now - 1, result["id"]))
                    elif race == "restriction":
                        db.execute("UPDATE api_keys SET restrictions=? WHERE id=?", (json.dumps({"shared": False, "projectIds": []}), result["id"]))
                    else:
                        db.execute("UPDATE api_keys SET scopes='[]' WHERE id=?", (result["id"],))
            return await super().batch(statements)

    binding = Racing(ledger.store)
    add_usage_meter(ledger, binding)
    assert remove(ledger, project, headers={"Authorization": "Bearer " + result["key"]})[0] == 412
    assert binding.raced
    assert rows(ledger) == before
    with ledger.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ledger_plan_usage").fetchone()[0] == 0


def test_bearer_session_without_actual_cookie_cannot_remove(ledger):
    project = blank(ledger)
    token = ledger.headers["Cookie"].split("=", 1)[1]
    before = rows(ledger)
    assert remove(ledger, project, headers={"Authorization": "Bearer " + token,
        "Origin": "https://app.example.test"})[0] == 403
    assert rows(ledger) == before


@pytest.mark.parametrize("cookie", ["same", "different"])
def test_bearer_session_and_cookie_mixture_does_not_authorize_removal(ledger, cookie):
    project = blank(ledger)
    token = ledger.headers["Cookie"].split("=", 1)[1]
    before = rows(ledger)
    headers = {**ledger.headers, "Authorization": "Bearer " + token,
        "Cookie": ledger.headers["Cookie"] if cookie == "same" else "pw_session=unrelated"}
    assert remove(ledger, project, headers=headers)[0] == 403
    assert rows(ledger) == before


@pytest.mark.parametrize("header,status", [(None, 428), ('"2"', 412), ("1", 422), ('"0"', 422)])
def test_removal_requires_current_quoted_revision(ledger, header, status):
    project = blank(ledger)
    headers = dict(ledger.headers)
    if header is not None:
        headers["If-Match"] = header
    before = rows(ledger)
    assert ledger.call("DELETE", "/api/v1/projects/" + project["id"], headers=headers)[0] == status
    assert rows(ledger) == before


def test_removal_checks_maximum_revision_before_native_dispatch(ledger):
    project = blank(ledger)
    with ledger.store.connect() as db:
        db.execute("UPDATE ledger_projects SET revision=9007199254740991 WHERE id=?", (project["id"],))
    before = rows(ledger)
    assert remove(ledger, project, revision=9007199254740991) == (409, {"error": {"code": "REVISION_LIMIT"}})
    assert rows(ledger) == before


def test_email_only_owner_removes_without_github_and_lost_bound_owner_also_can_remove(ledger):
    bound = create(ledger, ids=[101], name="Lost grant")
    with ledger.store.connect() as db:
        key = record_name("users", OWNER)
        user = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?", (key,)).fetchone()[0])
        for field in ("githubId", "githubLogin", "githubAccessToken"):
            user.pop(field, None)
        user.update(providers=["email"], email="owner@example.invalid", emailVerifiedAt=ledger.now)
        db.execute("UPDATE app_state SET payload=? WHERE name=?", (encode_record("users", OWNER, user), key))
    ledger.gateway.calls.clear()
    standalone = blank(ledger)
    assert remove(ledger, standalone) == (204, None)
    assert remove(ledger, bound) == (204, None)
    assert ledger.gateway.calls == []


@pytest.mark.parametrize("race", ["session", "user", "project", "binding"])
def test_same_batch_removal_fences_roll_back_tombstone_unlink_activity_and_usage(ledger, race):
    project = create(ledger, ids=[101], name="Race proof")
    before = rows(ledger)
    session_id = ledger.headers["Cookie"].split("=", 1)[1]

    class Racing(D1ShapedSQLite):
        raced = False
        async def batch(self, statements):
            statements = list(statements)
            if not self.raced and any(item.sql.lstrip().startswith("UPDATE ledger_projects SET deleted_at=") for item in statements):
                self.raced = True
                with self.store.connect() as db:
                    if race == "session":
                        db.execute("DELETE FROM app_state WHERE name=?", (record_name("sessions", session_id),))
                    elif race == "user":
                        key = record_name("users", OWNER)
                        user = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?", (key,)).fetchone()[0])
                        user["name"] = "Concurrent account change"
                        db.execute("UPDATE app_state SET payload=? WHERE name=?", (encode_record("users", OWNER, user), key))
                    elif race == "project":
                        db.execute("UPDATE ledger_projects SET revision=revision+1 WHERE id=?", (project["id"],))
                    else:
                        db.execute("""INSERT INTO ledger_project_repositories(owner_id,project_id,github_repo_id,github_full_name,created_at)
                            VALUES(?,?,102,'private/new-binding','race')""", (OWNER, project["id"]))
            return await super().batch(statements)

    binding = Racing(ledger.store)
    add_usage_meter(ledger, binding)
    ledger.gateway.calls.clear()
    assert remove(ledger, project)[0] == 412
    assert binding.raced and ledger.gateway.calls == []
    after = rows(ledger)
    assert after["ledger_activity_events"] == before["ledger_activity_events"]
    assert after["expenses"] == before["expenses"]
    assert after["d1_command_guard"] == []
    with ledger.store.connect() as db:
        row = db.execute("SELECT deleted_at,github_repo_id,revision FROM ledger_projects WHERE id=?", (project["id"],)).fetchone()
        assert tuple(row) == (None, 101, 2 if race == "project" else 1)
        assert db.execute("SELECT COUNT(*) FROM ledger_project_repositories WHERE project_id=?", (project["id"],)).fetchone()[0] == (2 if race == "binding" else 1)
        assert db.execute("SELECT COUNT(*) FROM ledger_plan_usage").fetchone()[0] == 0


def test_removal_uses_bounded_scalar_unlinks_and_never_reclaims_stored_capacity(ledger):
    ledger.gateway.visible["synthetic-access-token"] = list(range(101, 131))
    project = create(ledger, ids=range(101, 131), name="Thirty repositories")

    class Observe(D1ShapedSQLite):
        mutations = []
        async def batch(self, statements):
            statements = list(statements)
            if any(item.sql.lstrip().startswith("UPDATE ledger_projects SET deleted_at=") for item in statements):
                self.mutations.append(statements)
            return await super().batch(statements)

    binding = Observe(ledger.store)
    add_usage_meter(ledger, binding)
    ledger.gateway.calls.clear()
    assert remove(ledger, project) == (204, None)
    assert ledger.gateway.calls == [] and len(binding.mutations) == 1
    batch = binding.mutations[0]
    unlinks = [item for item in batch if item.sql.lstrip().startswith("DELETE FROM ledger_project_repositories")]
    assert len(unlinks) == 30 and len(batch) <= 64
    assert all("github_repo_id=?" in item.sql for item in unlinks)
    assert not any(item.sql.lstrip().startswith(("UPDATE expenses", "DELETE FROM expenses", "UPDATE expense_recurring_rules")) for item in batch)
    with ledger.store.connect() as db:
        assert db.execute("SELECT projects,writes FROM ledger_plan_usage").fetchone()["writes"] == 1
        # The retained project still counts in storage capacity.
        assert db.execute("SELECT COUNT(*) FROM ledger_projects").fetchone()[0] == 1
