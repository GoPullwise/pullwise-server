"""Actor GitHub grants and explicit multi-repository financial project behavior."""
import asyncio
import csv
import io
import json
import sqlite3
from pathlib import Path

import pytest

import test_ledger_routes as route_fixture
from pullwise_server.cloudflare_github_gateway import GitHubFailure
from pullwise_server.cloudflare_api_key_write import create_api_key
from pullwise_server.cloudflare_project_repositories import project_repository_eligibility
from test_cloudflare_github_identity_http import D1ShapedSQLite, GitHubStub


class RepositoryGateway(GitHubStub):
    def __init__(self):
        self.visible = {"synthetic-access-token": [101, 102, 103], "member-access-token": [102]}
        self.calls = []

    def installation(self, repo_id):
        return 501 if repo_id == 101 else 503 if repo_id == 103 else 502

    async def installations(self, token):
        self.calls.append(("installations", token))
        accounts = {501: {"id": 77, "login": "alice", "type": "User"},
                    502: {"id": 1001, "login": "alpha", "type": "Organization"},
                    503: {"id": 1002, "login": "beta", "type": "Organization"}}
        return [{"id": value, "account": accounts[value]}
                for value in sorted({self.installation(repo) for repo in self.visible[token]})]

    async def repositories(self, token, installation_id):
        self.calls.append(("repositories", token, installation_id))
        return [{"id": value, "full_name": f"private/repo-{value}"}
                for value in self.visible[token] if self.installation(value) == installation_id]


@pytest.fixture
def ledger():
    routes = route_fixture.LedgerRoutesTests()
    routes.setUp()
    connect = routes.store.connect

    def with_foreign_keys():
        connection = connect()
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    routes.store.connect = with_foreign_keys
    routes.gateway = RepositoryGateway()
    original_call = routes.call
    routes.call = lambda method, path, body=None, headers=None, params=None, gateway=None: original_call(
        method, path, body, headers, params, gateway or routes.gateway)
    try:
        yield routes
    finally:
        routes.tearDown()


def create(ledger, ids=(101, 102, 103), **fields):
    status, project = ledger.call("POST", "/api/v1/projects", {"githubRepoIds": list(ids), **fields})
    assert status == 201, project
    return project


def update(ledger, project, body, revision=None, headers=None):
    return ledger.call("PATCH", "/api/v1/projects/" + project["id"], body,
        {**(headers or ledger.headers), "If-Match": f'"{revision or project["revision"]}"'})


def member(ledger, role="admin"):
    with ledger.store.connect() as db:
        users = json.loads(db.execute("SELECT payload FROM app_state WHERE name='users'").fetchone()[0])
        users["usr_github_88"] = {"id": "usr_github_88", "name": "Bob", "githubId": "88",
            "githubLogin": "bob", "githubAccessToken": "sealed:member-access-token"}
        db.execute("UPDATE app_state SET payload=? WHERE name='users'", (json.dumps(users, separators=(",", ":")),))
        sessions = json.loads(db.execute("SELECT payload FROM app_state WHERE name='sessions'").fetchone()[0])
        sessions["session-member"] = {"userId": "usr_github_88", "expiresAt": ledger.now + 3600}
        db.execute("UPDATE app_state SET payload=? WHERE name='sessions'", (json.dumps(sessions),))
        db.execute("""INSERT INTO workspace_members(workspace_id,user_id,role,revision,joined_at,
            updated_at,invited_by_user_id) VALUES('usr_github_77','usr_github_88',?,1,'2026-10-06',
            '2026-10-06','usr_github_77')""", (role,))
    return {"Cookie": "pw_session=session-member", "X-Pullwise-Workspace": "usr_github_77",
            "Origin": "https://app.example.test"}


def test_explicit_cross_organization_links_and_current_account_metadata(ledger):
    project = create(ledger, name="Infrastructure", githubOrganizationId=1001)
    assert project["name"] == "Infrastructure" and project["githubRepoIds"] == [101, 102, 103]
    assert project["githubOrganization"] == {"id": 1001, "login": "alpha",
        "type": "Organization", "githubAccess": "authorized"}
    assert project["canCreateExpense"] is True
    assert [repo["account"]["type"] for repo in project["repositories"]] == ["User", "Organization", "Organization"]
    with ledger.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ledger_project_repositories").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 0


@pytest.mark.parametrize("ids", [[], list(range(101, 132)), [101, 101], [True], [-1],
                                     [9007199254740992], ["101"], None, {"id": 101}])
def test_invalid_explicit_selection_stops_before_provider_or_writes(ledger, ids):
    status, payload = ledger.call("POST", "/api/v1/projects", {"githubRepoIds": ids})
    assert status == 422 and payload["error"]["code"] == "INVALID_INPUT"
    assert ledger.gateway.calls == []
    with ledger.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ledger_projects").fetchone()[0] == 0


@pytest.mark.parametrize("body", [{"githubRepoId": 101, "githubRepoIds": [101]},
    {"githubRepoIds": [101], "name": "x" * 121}, {"githubRepoIds": [101], "owner_id": "other"},
    {"githubRepoIds": [101], "githubOrganizationId": True}, {"githubRepoIds": [101], "status": "active"}])
def test_project_fields_cannot_expand_authority(ledger, body):
    assert ledger.call("POST", "/api/v1/projects", body)[0] == 422
    assert ledger.gateway.calls == []


def test_thirty_explicit_repositories_are_the_maximum(ledger):
    ledger.gateway.visible["synthetic-access-token"] = list(range(101, 131))
    project = create(ledger, ids=range(101, 131))
    assert len(project["repositories"]) == 30
    assert project["githubAccess"] == "authorized"


def test_repository_unique_per_ledger_and_unbound_anchor_can_be_reused(ledger):
    project = create(ledger, ids=[101, 102])
    ledger.gateway.calls.clear()
    assert ledger.call("POST", "/api/v1/projects", {"githubRepoIds": [102, 103]})[0] == 409
    assert ledger.gateway.calls == []
    status, changed = update(ledger, project, {"githubRepoIds": [102]})
    assert status == 200 and changed["id"] == project["id"] and changed["githubRepoId"] == 102
    reused = create(ledger, ids=[101])
    assert reused["id"] != project["id"]
    assert update(ledger, changed, {"githubRepoIds": [101, 102]})[0] == 409
    assert update(ledger, changed, {"githubRepoIds": [103]}, revision=1)[0] == 412


def test_same_repository_can_be_bound_in_another_workspace(ledger):
    owner = create(ledger, ids=[102])
    headers = member(ledger)
    personal_headers = {key: value for key, value in headers.items() if key != "X-Pullwise-Workspace"}
    status, personal = ledger.call("POST", "/api/v1/projects", {"githubRepoIds": [102]}, personal_headers)
    assert status == 201 and personal["id"] != owner["id"]
    with ledger.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ledger_project_repositories WHERE github_repo_id=102").fetchone()[0] == 2


def test_partial_loss_hides_cached_names_but_preserves_project_financial_name(ledger):
    project = create(ledger, name="Stable financial label", githubOrganizationId=1002)
    ledger.gateway.visible["synthetic-access-token"] = [102]
    status, history = ledger.call("GET", "/api/v1/projects/" + project["id"])
    assert status == 200 and history["githubAccess"] == "partial" and history["canCreateExpense"]
    assert history["name"] == "Stable financial label" and history["githubFullName"] is None
    assert history["repositories"][0]["githubFullName"] is None
    assert history["repositories"][0]["account"] is None
    assert history["repositories"][1]["githubFullName"] == "private/repo-102"
    assert history["githubOrganization"]["login"] is None
    assert "private/repo-101" not in json.dumps(history) and "private/repo-103" not in json.dumps(history)
    assert update(ledger, project, {"description": "History remains editable"})[0] == 200
    assert update(ledger, project, {"githubRepoIds": [101, 102]}, revision=2)[0] == 403


def test_member_discovery_and_history_use_actor_token_not_owner_token(ledger):
    project = create(ledger, name="Team cost")
    headers = member(ledger, "viewer")
    ledger.gateway.calls.clear()
    status, page = ledger.call("GET", "/api/v1/repositories", headers=headers)
    assert status == 200 and [item["githubRepoId"] for item in page["items"]] == [102]
    status, history = ledger.call("GET", "/api/v1/projects/" + project["id"], headers=headers)
    assert status == 200 and history["name"] == "Team cost" and history["githubAccess"] == "partial"
    assert [repo["githubFullName"] for repo in history["repositories"]] == [None, "private/repo-102", None]
    assert all(item[1] == "member-access-token" for item in ledger.gateway.calls)
    assert update(ledger, project, {"name": "Forbidden"}, headers=headers)[0] == 403


def test_organization_is_validated_from_live_installation_metadata(ledger):
    assert ledger.call("POST", "/api/v1/projects", {"githubRepoIds": [101], "githubOrganizationId": 9999})[0] == 403
    project = create(ledger, ids=[101], githubOrganizationId=1001)
    # Organization association does not add any of its repository candidates.
    assert project["githubRepoIds"] == [101]
    status, cleared = update(ledger, project, {"githubOrganizationId": None})
    assert status == 200 and cleared["githubOrganization"] is None


def test_repository_pagination_is_ordered_bounded_and_does_not_write(ledger):
    with ledger.store.connect() as db:
        before = list(db.iterdump())
    status, first = ledger.call("GET", "/api/v1/repositories", params={"limit": "2"})
    assert status == 200 and [row["githubRepoId"] for row in first["items"]] == [101, 102]
    assert first["nextCursor"] == "102"
    status, second = ledger.call("GET", "/api/v1/repositories", params={"limit": "2", "cursor": "102"})
    assert status == 200 and [row["githubRepoId"] for row in second["items"]] == [103]
    assert second["nextCursor"] is None and second["organizations"] == first["organizations"]
    for params in ({"limit": "101"}, {"limit": "9" * 5000}, {"cursor": "-1"},
                   {"cursor": "9007199254740992"}, {"cursor": "１２"}):
        assert ledger.call("GET", "/api/v1/repositories", params=params)[0] == 422
    with ledger.store.connect() as db:
        assert list(db.iterdump()) == before


def test_repository_occupancy_includes_projects_beyond_the_loaded_project_page(ledger):
    project = create(ledger, ids=[103])
    with ledger.store.connect() as db:
        db.executemany("""INSERT INTO ledger_projects(id,owner_id,github_repo_id,github_full_name,
            created_at,updated_at) VALUES(?,'usr_github_77',?,'private/other','2026-10-06','2026-10-06')""",
            [(f"prj_000{index:03}", 400 + index) for index in range(50)])
        db.executemany("""INSERT INTO ledger_project_repositories(owner_id,project_id,github_repo_id,
            github_full_name,created_at) VALUES('usr_github_77',?,?,'private/other','2026-10-06')""",
            [(f"prj_000{index:03}", 400 + index) for index in range(50)])
    _, projects = ledger.call("GET", "/api/v1/projects")
    assert len(projects["items"]) == 50 and all(row["id"] != project["id"] for row in projects["items"])
    status, repositories = ledger.call("GET", "/api/v1/repositories")
    assert status == 200
    assert {row["githubRepoId"]: row["isBound"] for row in repositories["items"]} == {101: False, 102: False, 103: True}
    assert project["id"] not in json.dumps(repositories)


def test_key_occupancy_never_discloses_bindings_outside_allowed_projects(ledger):
    allowed = create(ledger, ids=[101])
    hidden = create(ledger, ids=[102])
    status, key = asyncio.run(create_api_key(binding=ledger.binding, headers=ledger.headers,
        body={"name": "One financial project", "scopes": ["projects:read"],
              "restrictions": {"projectIds": [allowed["id"]]}}, now=ledger.now + 3))
    assert status == 201
    status, page = ledger.call("GET", "/api/v1/repositories", headers={"Authorization": "Bearer " + key["key"]})
    assert status == 200
    assert {row["githubRepoId"]: row["isBound"] for row in page["items"]} == {101: True, 102: False, 103: False}
    assert allowed["id"] not in json.dumps(page) and hidden["id"] not in json.dumps(page)


def test_partial_anchor_loss_allows_creation_and_total_loss_preserves_historical_edits(ledger):
    project = create(ledger, ids=[101, 102])
    _, category = ledger.call("POST", "/api/v1/categories", {"name": "Partial grants"})
    body = {"target": {"kind": "project", "projectId": project["id"]}, "categoryId": category["id"],
            "occurredOn": "2026-10-06", "amount": "1.00", "currency": "USD", "purpose": "History"}
    ledger.gateway.visible["synthetic-access-token"] = [102]
    status, expense = ledger.call("POST", "/api/v1/expenses", body,
        {**ledger.headers, "Idempotency-Key": "partial-project"})
    assert status == 201
    ledger.gateway.visible["synthetic-access-token"] = []
    assert ledger.call("POST", "/api/v1/expenses", body,
        {**ledger.headers, "Idempotency-Key": "all-lost-project"})[0] == 403
    ledger.gateway.calls.clear()
    status, edited = ledger.call("PATCH", "/api/v1/expenses/" + expense["id"],
        {**body, "purpose": "Retained control"}, {**ledger.headers, "If-Match": '"1"'})
    assert status == 200 and edited["purpose"] == "Retained control"
    assert ledger.call("DELETE", "/api/v1/expenses/" + expense["id"], headers={**ledger.headers, "If-Match": '"2"'})[0] == 204
    assert ledger.gateway.calls == []


def test_member_project_expense_uses_the_member_bound_grant_and_audits_real_actor(ledger):
    project = create(ledger, ids=[101, 102])
    _, category = ledger.call("POST", "/api/v1/categories", {"name": "Member"})
    headers = member(ledger, "editor")
    ledger.gateway.visible["synthetic-access-token"] = [101]
    ledger.gateway.calls.clear()
    status, _ = ledger.call("POST", "/api/v1/expenses", {"target": {"kind": "project", "projectId": project["id"]},
        "categoryId": category["id"], "occurredOn": "2026-10-06", "amount": "1.00", "currency": "USD",
        "purpose": "Member-authorized repository"}, {**headers, "Idempotency-Key": "member-bound-project"})
    assert status == 201 and all(call[1] == "member-access-token" for call in ledger.gateway.calls)
    with ledger.store.connect() as db:
        assert tuple(db.execute("SELECT owner_id,actor_id FROM expense_events").fetchone()) == ("usr_github_77", "usr_github_88")


def test_expense_eligibility_uses_any_actor_authorized_link_and_returns_revision(ledger):
    project = create(ledger)
    user = {"id": "usr_github_77", "_actor": {"githubAccessToken": "sealed:member-access-token"}}
    proof = asyncio.run(project_repository_eligibility(ledger.binding, user, project["id"], ledger.gateway))
    assert proof == {"revision": 1, "githubRepoId": 102}
    ledger.gateway.visible["member-access-token"] = []
    assert asyncio.run(project_repository_eligibility(ledger.binding, user, project["id"], ledger.gateway)) is None


def test_archive_restore_preserves_finance_and_repository_occupancy(ledger):
    project = create(ledger, ids=[101, 102], name="Archive QA")
    _, category = ledger.call("POST", "/api/v1/categories", {"name": "Archive QA"})
    body = {"target": {"kind": "project", "projectId": project["id"]},
        "categoryId": category["id"], "occurredOn": "2026-10-06",
        "amount": "1.23", "currency": "USD", "purpose": "Retained finance"}
    status, expense = ledger.call("POST", "/api/v1/expenses", body,
        {**ledger.headers, "Idempotency-Key": "archive-initial"})
    assert status == 201
    status, archived = update(ledger, project, {"status": "archived"})
    assert status == 200 and archived["status"] == "archived" and not archived["canCreateExpense"]
    assert archived["totals"] == [{"currency": "USD", "amountMinor": 123}]
    assert ledger.call("POST", "/api/v1/expenses", body,
        {**ledger.headers, "Idempotency-Key": "archive-new"})[0] == 403
    assert ledger.call("POST", "/api/v1/projects", {"githubRepoIds": [101]})[0] == 409
    status, repositories = ledger.call("GET", "/api/v1/repositories")
    assert status == 200
    assert {row["githubRepoId"]: row["isBound"] for row in repositories["items"]} == {
        101: True, 102: True, 103: False}
    ledger.gateway.visible["synthetic-access-token"] = []
    status, edited = ledger.call("PATCH", "/api/v1/expenses/" + expense["id"],
        {**body, "purpose": "Historical edit"}, {**ledger.headers, "If-Match": '"1"'})
    assert status == 200 and edited["revision"] == 2
    assert update(ledger, archived, {"status": "active"})[0] == 403
    ledger.gateway.visible["synthetic-access-token"] = [102]
    status, restored = update(ledger, archived, {"status": "active"})
    assert status == 200 and restored["canCreateExpense"] and restored["githubAccess"] == "partial"
    assert restored["githubRepoIds"] == [101, 102] and restored["revision"] == 3
    assert restored["totals"] == [{"currency": "USD", "amountMinor": 123}]
    with ledger.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM expense_events").fetchone()[0] == 2


@pytest.mark.parametrize("code,state", [("GITHUB_REAUTHORIZATION_REQUIRED", "reauthorization_required"),
    ("GITHUB_PERMISSION_DENIED", "lost"), ("GITHUB_UNAVAILABLE", "unavailable")])
def test_known_rejection_and_unknown_outages_hide_all_repository_metadata(ledger, code, state):
    project = create(ledger, name="Costs")
    class Broken(RepositoryGateway):
        async def installations(self, _token):
            raise GitHubFailure(code)
    broken = Broken()
    status, history = ledger.call("GET", "/api/v1/projects/" + project["id"], gateway=broken)
    assert status == 200 and history["githubAccess"] == state and not history["canCreateExpense"]
    assert all(row["githubFullName"] is None for row in history["repositories"])
    assert update(ledger, project, {"name": "Retained"})[0] == 200
    assert ledger.call("POST", "/api/v1/projects", {"githubRepoIds": [104]}, gateway=broken)[0] in {403, 503}


def test_multi_repository_totals_and_export_count_each_expense_once(ledger):
    project = create(ledger)
    _, category = ledger.call("POST", "/api/v1/categories", {"name": "Usage"})
    for index, (amount, currency) in enumerate((("1.23", "USD"), ("7", "JPY"))):
        status, _ = ledger.call("POST", "/api/v1/expenses", {"target": {"kind": "project", "projectId": project["id"]},
            "occurredOn": "2026-10-06", "amount": amount, "currency": currency,
            "categoryId": category["id"], "purpose": "One financial record"},
            {**ledger.headers, "Idempotency-Key": f"multi-total-{index}"})
        assert status == 201
    expected = [{"currency": "JPY", "amountMinor": 7}, {"currency": "USD", "amountMinor": 123}]
    assert ledger.call("GET", "/api/v1/projects/" + project["id"])[1]["totals"] == expected
    assert ledger.call("GET", "/api/v1/projects")[1]["items"][0]["totals"] == expected
    status, changed = update(ledger, project, {"githubRepoIds": [102, 103]})
    assert status == 200 and changed["totals"] == expected
    _, summary = ledger.call("GET", "/api/v1/reports/summary", params={"projectId": project["id"]})
    assert {(row["currency"], row["amountMinor"]) for row in summary["groups"]} == {("JPY", 7), ("USD", 123)}
    _, export = ledger.call("GET", "/api/v1/expenses/export", params={"projectId": project["id"]})
    async def collect():
        return "".join([chunk async for chunk in export.chunks()])
    assert len(list(csv.DictReader(io.StringIO(asyncio.run(collect()))))) == 2
    with ledger.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM expense_events").fetchone()[0] == 2


def test_concurrent_revision_change_rolls_back_anchor_and_all_links(ledger):
    project = create(ledger, ids=[101, 102])
    class Racing(D1ShapedSQLite):
        raced = False
        async def batch(self, statements):
            if not self.raced and any(item.sql.lstrip().startswith("UPDATE ledger_projects") for item in statements):
                self.raced = True
                with self.store.connect() as db:
                    db.execute("UPDATE ledger_projects SET revision=revision+1 WHERE id=?", (project["id"],))
            return await super().batch(statements)
    ledger.binding = Racing(ledger.store)
    status, _ = update(ledger, project, {"githubRepoIds": [103]})
    assert status == 412
    with ledger.store.connect() as db:
        row = db.execute("SELECT github_repo_id,revision FROM ledger_projects WHERE id=?", (project["id"],)).fetchone()
        assert tuple(row) == (101, 2)
        assert [row[0] for row in db.execute("SELECT github_repo_id FROM ledger_project_repositories ORDER BY github_repo_id")] == [101, 102]
        assert db.execute("SELECT COUNT(*) FROM d1_command_guard").fetchone()[0] == 0


def test_concurrent_repository_claim_returns_conflict_without_partial_replace(ledger):
    project = create(ledger, ids=[101, 102])
    class Racing(D1ShapedSQLite):
        raced = False
        async def batch(self, statements):
            if not self.raced and any(item.sql.lstrip().startswith("UPDATE ledger_projects") for item in statements):
                self.raced = True
                with self.store.connect() as db:
                    db.execute("""INSERT INTO ledger_projects(id,owner_id,github_repo_id,github_full_name,
                        created_at,updated_at) VALUES('prj_racer','usr_github_77',103,'private/repo-103',
                        '2026-10-06','2026-10-06')""")
                    db.execute("""INSERT INTO ledger_project_repositories(owner_id,project_id,github_repo_id,
                        github_full_name,created_at) VALUES('usr_github_77','prj_racer',103,'private/repo-103',
                        '2026-10-06')""")
            return await super().batch(statements)
    ledger.binding = Racing(ledger.store)
    status, result = update(ledger, project, {"githubRepoIds": [103]})
    assert status == 409 and result["error"]["code"] == "PROJECT_CONFLICT"
    with ledger.store.connect() as db:
        row = db.execute("SELECT github_repo_id,revision FROM ledger_projects WHERE id=?", (project["id"],)).fetchone()
        assert tuple(row) == (101, 1)
        assert [row[0] for row in db.execute("SELECT github_repo_id FROM ledger_project_repositories WHERE project_id=? ORDER BY github_repo_id",
                                           (project["id"],))] == [101, 102]
        assert db.execute("SELECT COUNT(*) FROM d1_command_guard").fetchone()[0] == 0


def test_malformed_provider_result_is_a_safe_upstream_error(ledger):
    class Invalid(RepositoryGateway):
        async def repositories(self, _token, _installation_id):
            return [{"id": 9007199254740992, "full_name": "private/unsafe"}]
    status, result = ledger.call("GET", "/api/v1/repositories", gateway=Invalid())
    assert status == 502 and result == {"error": {"code": "GITHUB_RESPONSE_INVALID"}}
    assert ledger.call("POST", "/api/v1/projects", {"githubRepoIds": [101]}, gateway=Invalid())[0] == 502


def test_legacy_migration_backfills_links_without_rewriting_financial_history(tmp_path):
    root = Path(__file__).resolve().parents[1] / "cloudflare/server/migrations"
    with sqlite3.connect(tmp_path / "legacy.db") as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript((root / "0001_ledger.sql").read_text())
        db.execute("""INSERT INTO ledger_projects(id,owner_id,github_repo_id,github_full_name,created_at,updated_at)
            VALUES('prj_legacy','owner',101,'private/old','2026-10-01','2026-10-01')""")
        db.execute("""INSERT INTO expense_categories(id,owner_id,name,created_at,updated_at)
            VALUES('cat_legacy','owner','Hosting','2026-10-01','2026-10-01')""")
        db.execute("""INSERT INTO expenses(id,owner_id,target_kind,project_id,category_id,occurred_on,
            amount_minor,currency,purpose,created_at,updated_at)
            VALUES('exp_legacy','owner','project','prj_legacy','cat_legacy','2026-10-01',123,'USD','Hosting',
            '2026-10-01','2026-10-01')""")
        before = db.execute("SELECT * FROM expenses").fetchall()
        db.executescript((root / "0005_workspaces_repositories.sql").read_text())
        assert db.execute("SELECT * FROM expenses").fetchall() == before
        assert db.execute("SELECT name,github_organization_id FROM ledger_projects").fetchone() == ("", None)
        assert db.execute("SELECT owner_id,project_id,github_repo_id,github_full_name FROM ledger_project_repositories").fetchone() == (
            "owner", "prj_legacy", 101, "private/old")
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
