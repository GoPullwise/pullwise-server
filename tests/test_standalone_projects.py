"""Standalone financial projects retain actor, target and atomic write authority."""
import asyncio
import csv
import io
import json
from pathlib import Path

import pytest

from pullwise_server.cloudflare_api_key_write import create_api_key
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1
from pullwise_server.cloudflare_project_repositories import project_repository_eligibility
from pullwise_server.cloudflare_state_records import encode_record, record_name
from pullwise_server.ledger_plan_policy import default_policy
from test_cloudflare_github_identity_http import D1ShapedSQLite
from test_project_repositories import create, ledger, member, update


def blank(ledger, name="Standalone costs", **fields):
    status, project = ledger.call("POST", "/api/v1/projects", {"name": name, **fields})
    assert status == 201, project
    return project


def assert_standalone(project, *, active=True):
    assert project["githubRepoId"] is None and project["githubFullName"] is None
    assert project["githubRepoIds"] == [] and project["repositories"] == []
    assert project["githubOrganizationId"] is None and project["githubOrganization"] is None
    assert project["githubAccess"] == "not_linked" and project["canCreateExpense"] is active


def category(ledger):
    status, result = ledger.call("POST", "/api/v1/categories", {"name": "Hosting"})
    assert status == 201
    return result["id"]


def expense_body(category_id, project=None, **fields):
    return {"target": {"kind": "project", "projectId": project["id"]} if project else {"kind": "shared"},
            "categoryId": category_id, "occurredOn": "2026-10-06", "amount": "1.23",
            "currency": "USD", "purpose": "Financial history", **fields}


def save_user_with_unusable_github_token(ledger, user_id):
    with ledger.store.connect() as db:
        key = record_name("users", user_id)
        user = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?", (key,)).fetchone()[0])
        # Keep GitHub login authentication intact while making any attempt to
        # borrow an OAuth grant observable and unusable.
        user["githubAccessToken"] = "sealed:revoked-synthetic-token"
        db.execute("UPDATE app_state SET payload=? WHERE name=?", (encode_record("users", user_id, user), key))
    async def unavailable(_sealed):
        ledger.gateway.calls.append(("unseal", "revoked-synthetic-token"))
        raise RuntimeError("Synthetic OAuth grant cannot be used")
    ledger.gateway.unseal = unavailable


def add_usage_meter(ledger, binding=None, policy=None):
    root = Path(__file__).resolve().parents[1] / "cloudflare/server/migrations"
    with ledger.store.connect() as db:
        db.executescript((root / "0004_ledger_plan_usage.sql").read_text())
    ledger.binding = PlanLimitedD1(binding or ledger.binding, policy=policy, now=ledger.now + 3)


@pytest.mark.parametrize("fields", [{}, {"githubRepoIds": []}, {"githubRepoIds": [], "githubOrganizationId": None}])
def test_blank_creation_has_explicit_null_anchors_and_needs_no_github_grant(ledger, fields):
    save_user_with_unusable_github_token(ledger, "usr_github_77")
    project = blank(ledger, "  Non GitHub costs  ", **fields)
    assert project["name"] == "Non GitHub costs"
    assert_standalone(project)
    second = blank(ledger, "Another project")
    assert second["id"] != project["id"]
    for path in ("/api/v1/projects", "/api/v1/projects/" + project["id"]):
        status, result = ledger.call("GET", path)
        assert status == 200
        for item in result["items"] if "items" in result else [result]:
            assert_standalone(item)
    status, changed = update(ledger, project, {"description": "Budget without repository linkage"})
    assert status == 200 and changed["revision"] == 2
    assert_standalone(changed)
    assert ledger.gateway.calls == []
    with ledger.store.connect() as db:
        assert all(tuple(row) == (None, None, None) for row in db.execute(
            "SELECT github_repo_id,github_full_name,github_organization_id FROM ledger_projects"))
        assert db.execute("SELECT COUNT(*) FROM ledger_project_repositories").fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("body", [{}, {"description": "Missing name"}, {"name": ""}, {"name": " \t\n "},
    {"name": "x" * 121}, {"name": None}, {"name": "Costs", "githubOrganizationId": 1001},
    {"name": "Costs", "githubRepoIds": [], "githubOrganizationId": 1001},
    {"name": "Costs", "githubRepoIds": None}, {"name": "Costs", "githubRepoId": None}])
def test_invalid_blank_input_stops_before_provider_or_writes(ledger, body):
    with ledger.store.connect() as db:
        before = list(db.iterdump())
    assert ledger.call("POST", "/api/v1/projects", body) == (422, {"error": {"code": "INVALID_INPUT"}})
    assert ledger.gateway.calls == []
    with ledger.store.connect() as db:
        assert list(db.iterdump()) == before


def test_standalone_name_limit_applies_after_trimming_and_patch_requires_name(ledger):
    project = blank(ledger, " " * 200 + "x" * 120 + " " * 200)
    assert len(project["name"]) == 120
    for body in ({"name": "   "}, {"githubOrganizationId": 1001},
                 {"githubRepoIds": [], "name": ""}):
        assert update(ledger, project, body)[0] == 422
    assert ledger.call("GET", "/api/v1/projects/" + project["id"])[1]["revision"] == 1
    assert ledger.gateway.calls == []


def test_all_standalone_page_skips_github_even_when_next_page_has_a_bound_project(ledger):
    create(ledger, ids=[101], name="Linked")
    with ledger.store.connect() as db:
        db.execute("""INSERT INTO ledger_projects(id,owner_id,github_repo_id,github_full_name,name,
            created_at,updated_at) VALUES('prj_000_blank','usr_github_77',NULL,NULL,'First','local','local')""")
    ledger.gateway.calls.clear()
    status, first = ledger.call("GET", "/api/v1/projects", params={"limit": "1"})
    assert status == 200 and first["nextCursor"] == "prj_000_blank"
    assert_standalone(first["items"][0])
    assert ledger.gateway.calls == []
    status, second = ledger.call("GET", "/api/v1/projects", params={"cursor": first["nextCursor"], "limit": "1"})
    assert status == 200 and second["items"][0]["githubAccess"] == "authorized"
    assert ledger.gateway.calls


def test_explicit_unlink_clears_org_and_preserves_all_financial_history(ledger):
    project = create(ledger, ids=[101, 102], name="Costs", githubOrganizationId=1001)
    body = expense_body(category(ledger), project)
    status, expense = ledger.call("POST", "/api/v1/expenses", body,
        {**ledger.headers, "Idempotency-Key": "before-unlink"})
    assert status == 201
    with ledger.store.connect() as db:
        expenses_before = db.execute("SELECT * FROM expenses").fetchall()
        events_before = db.execute("SELECT * FROM expense_events").fetchall()
    ledger.gateway.calls.clear()
    assert update(ledger, project, {"githubRepoIds": [], "githubOrganizationId": 1001})[0] == 422
    status, unlinked = update(ledger, project, {"githubRepoIds": []})
    assert status == 200 and unlinked["id"] == project["id"] and unlinked["revision"] == 2
    assert_standalone(unlinked)
    assert unlinked["totals"] == [{"currency": "USD", "amountMinor": 123}]
    assert ledger.gateway.calls == []
    with ledger.store.connect() as db:
        assert db.execute("SELECT * FROM expenses").fetchall() == expenses_before
        assert db.execute("SELECT * FROM expense_events").fetchall() == events_before
        assert db.execute("SELECT COUNT(*) FROM ledger_project_repositories").fetchone()[0] == 0
        assert tuple(db.execute("SELECT github_repo_id,github_full_name,github_organization_id FROM ledger_projects").fetchone()) == (None, None, None)
    assert ledger.call("GET", "/api/v1/expenses/" + expense["id"])[1]["amountMinor"] == 123
    assert create(ledger, ids=[101])["id"] != project["id"]


def test_legacy_empty_github_name_requires_explicit_name_only_when_unlinking(ledger):
    project = create(ledger, ids=[101])
    assert project["name"] == ""
    status, retained = update(ledger, project, {"description": "Still linked"})
    assert status == 200 and retained["githubRepoIds"] == [101]
    ledger.gateway.calls.clear()
    assert update(ledger, retained, {"githubRepoIds": []})[0] == 422
    status, unlinked = update(ledger, retained, {"githubRepoIds": [], "name": "  Finance  "})
    assert status == 200 and unlinked["name"] == "Finance"
    assert_standalone(unlinked)
    assert ledger.gateway.calls == []


@pytest.mark.parametrize("role,project_status,expense_status", [("viewer", 403, 403), ("editor", 403, 201), ("admin", 201, 201)])
def test_shared_roles_keep_existing_authority_without_lending_github_credentials(ledger, role, project_status, expense_status):
    project = blank(ledger)
    category_id = category(ledger)
    headers = member(ledger, role)
    save_user_with_unusable_github_token(ledger, "usr_github_88")
    ledger.gateway.calls.clear()
    assert ledger.call("GET", "/api/v1/projects/" + project["id"], headers=headers)[0] == 200
    assert ledger.call("POST", "/api/v1/projects", {"name": "Member costs"}, headers)[0] == project_status
    assert update(ledger, project, {"description": "Member edit"}, headers=headers)[0] == (200 if role == "admin" else 403)
    status, _ = ledger.call("POST", "/api/v1/expenses", expense_body(category_id, project),
        {**headers, "Idempotency-Key": "member-standalone"})
    assert status == expense_status and ledger.gateway.calls == []
    with ledger.store.connect() as db:
        events = db.execute("SELECT owner_id,actor_id FROM expense_events").fetchall()
        assert [tuple(row) for row in events] == ([] if role == "viewer" else [("usr_github_77", "usr_github_88")])


def test_linking_a_blank_project_uses_actual_actor_grants_and_repository_uniqueness(ledger):
    project = blank(ledger)
    headers = member(ledger)
    assert update(ledger, project, {"githubRepoIds": [101]}, headers=headers)[0] == 403
    assert ledger.gateway.calls and all(row[1] == "member-access-token" for row in ledger.gateway.calls)
    status, linked = update(ledger, project, {"githubRepoIds": [102]}, headers=headers)
    assert status == 200 and linked["githubRepoIds"] == [102] and linked["githubAccess"] == "authorized"
    other = blank(ledger, "Another cost centre")
    assert update(ledger, other, {"githubRepoIds": [102]})[0] == 409
    assert update(ledger, linked, {"githubRepoIds": []}, revision=1)[0] == 412
    assert ledger.call("GET", "/api/v1/projects/" + other["id"])[1]["githubAccess"] == "not_linked"


def test_api_key_project_isolation_survives_standalone_access_and_shared_target(ledger):
    allowed, forbidden = blank(ledger), blank(ledger, "Private other project")
    category_id = category(ledger)
    status, key = asyncio.run(create_api_key(binding=ledger.binding, headers=ledger.headers,
        body={"name": "Standalone scope", "scopes": ["projects:read", "projects:write", "expenses:read", "expenses:write", "reports:read"],
              "restrictions": {"projectIds": [allowed["id"]], "shared": False}}, now=ledger.now + 3))
    assert status == 201
    headers = {"Authorization": "Bearer " + key["key"]}
    save_user_with_unusable_github_token(ledger, "usr_github_77")
    assert ledger.call("GET", "/api/v1/projects", headers=headers)[1]["items"][0]["id"] == allowed["id"]
    assert ledger.call("GET", "/api/v1/projects/" + forbidden["id"], headers=headers)[0] == 403
    assert update(ledger, forbidden, {"description": "Denied"}, headers=headers)[0] == 403
    for index, target in enumerate((allowed, forbidden, None)):
        status, _ = ledger.call("POST", "/api/v1/expenses", expense_body(category_id, target),
            {**headers, "Idempotency-Key": f"restricted-blank-{index}"})
        assert status == (201 if target is allowed else 403)
    status, page = ledger.call("GET", "/api/v1/expenses", headers=headers)
    assert status == 200 and len(page["items"]) == 1
    status, report = ledger.call("GET", "/api/v1/reports/summary", headers=headers)
    assert status == 200 and {row["amountMinor"] for row in report["groups"]} == {123}
    assert ledger.gateway.calls == []


def test_shared_expense_can_move_into_blank_without_provider_and_keeps_exact_amount(ledger):
    project = blank(ledger)
    shared = expense_body(category(ledger))
    status, expense = ledger.call("POST", "/api/v1/expenses", shared,
        {**ledger.headers, "Idempotency-Key": "shared-to-blank"})
    assert status == 201
    save_user_with_unusable_github_token(ledger, "usr_github_77")
    status, moved = ledger.call("PATCH", "/api/v1/expenses/" + expense["id"],
        {**shared, "target": {"kind": "project", "projectId": project["id"]}},
        {**ledger.headers, "If-Match": '"1"'})
    assert status == 200 and moved["id"] == expense["id"] and moved["amountMinor"] == 123 and moved["revision"] == 2
    assert ledger.call("GET", "/api/v1/projects/" + project["id"])[1]["totals"] == [{"currency": "USD", "amountMinor": 123}]
    with ledger.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 1
        assert [row[0] for row in db.execute("SELECT action FROM expense_events ORDER BY action")] == ["create", "update"]
    assert ledger.gateway.calls == []


def test_blank_archive_blocks_new_targets_but_replay_history_and_reactivation_need_no_github(ledger):
    project = blank(ledger)
    category_id = category(ledger)
    body = expense_body(category_id, project)
    headers = {**ledger.headers, "Idempotency-Key": "blank-archive-original"}
    status, expense = ledger.call("POST", "/api/v1/expenses", body, headers)
    assert status == 201
    _, shared = ledger.call("POST", "/api/v1/expenses", expense_body(category_id),
        {**ledger.headers, "Idempotency-Key": "blank-archive-shared"})
    status, archived = update(ledger, project, {"status": "archived"})
    assert status == 200
    assert_standalone(archived, active=False)
    save_user_with_unusable_github_token(ledger, "usr_github_77")
    assert ledger.call("POST", "/api/v1/expenses", body, headers) == (201, expense)
    assert ledger.call("POST", "/api/v1/expenses", body,
        {**ledger.headers, "Idempotency-Key": "blank-archive-new"})[0] == 403
    assert ledger.call("PATCH", "/api/v1/expenses/" + shared["id"], body,
        {**ledger.headers, "If-Match": '"1"'})[0] == 403
    status, historical = ledger.call("PATCH", "/api/v1/expenses/" + expense["id"],
        {**body, "purpose": "Historical correction"}, {**ledger.headers, "If-Match": '"1"'})
    assert status == 200 and historical["revision"] == 2
    status, restored = update(ledger, archived, {"status": "active"})
    assert status == 200 and restored["revision"] == 3
    assert_standalone(restored)
    assert ledger.gateway.calls == []
    with ledger.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM expense_create_idempotency").fetchone()[0] == 2


@pytest.mark.parametrize("change", ["linked", "anchor_only", "binding_only", "archived"])
@pytest.mark.parametrize("moving", [False, True])
def test_atomic_blank_target_proof_rejects_concurrent_association_or_status_change(ledger, change, moving):
    project = blank(ledger)
    body = expense_body(category(ledger), project)
    expense = None
    if moving:
        _, expense = ledger.call("POST", "/api/v1/expenses", {**body, "target": {"kind": "shared"}},
            {**ledger.headers, "Idempotency-Key": "before-blank-race"})

    class Racing(D1ShapedSQLite):
        raced = False
        async def batch(self, statements):
            mutating_expense = any(item.sql.lstrip().startswith(("INSERT INTO expenses", "UPDATE expenses")) for item in statements)
            if not self.raced and mutating_expense:
                self.raced = True
                with self.store.connect() as db:
                    if change in {"linked", "anchor_only"}:
                        db.execute("UPDATE ledger_projects SET github_repo_id=101,github_full_name='private/repo-101',revision=revision+? WHERE id=?",
                                   (int(change == "linked"), project["id"]))
                    if change in {"linked", "binding_only"}:
                        db.execute("""INSERT INTO ledger_project_repositories(owner_id,project_id,github_repo_id,
                            github_full_name,created_at) VALUES('usr_github_77',?,101,'private/repo-101','local')""", (project["id"],))
                    if change == "archived":
                        db.execute("UPDATE ledger_projects SET status='archived' WHERE id=?", (project["id"],))
            return await super().batch(statements)

    add_usage_meter(ledger, Racing(ledger.store))
    ledger.gateway.calls.clear()
    if moving:
        status, _ = ledger.call("PATCH", "/api/v1/expenses/" + expense["id"], body,
            {**ledger.headers, "If-Match": '"1"'})
        assert status == 412
    else:
        status, _ = ledger.call("POST", "/api/v1/expenses", body,
            {**ledger.headers, "Idempotency-Key": "blank-raced"})
        assert status == 409
    assert ledger.gateway.calls == []
    with ledger.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == int(moving)
        assert db.execute("SELECT COUNT(*) FROM expense_events").fetchone()[0] == int(moving)
        assert db.execute("SELECT COUNT(*) FROM expense_create_idempotency").fetchone()[0] == int(moving)
        assert db.execute("SELECT COUNT(*) FROM ledger_plan_usage").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM d1_command_guard").fetchone()[0] == 0
        if moving:
            assert tuple(db.execute("SELECT target_kind,project_id,revision,amount_minor FROM expenses").fetchone()) == ("shared", None, 1, 123)


def test_lost_bound_project_is_never_interpreted_as_standalone(ledger):
    project = create(ledger, ids=[101], name="Bound costs")
    ledger.gateway.visible["synthetic-access-token"] = []
    assert ledger.call("GET", "/api/v1/projects/" + project["id"])[1]["githubAccess"] == "lost"
    with ledger.store.connect() as db:
        db.execute("DELETE FROM ledger_project_repositories WHERE project_id=?", (project["id"],))
    user = {"id": "usr_github_77"}
    ledger.gateway.calls.clear()
    assert asyncio.run(project_repository_eligibility(ledger.binding, user, project["id"], ledger.gateway)) is None
    status, unchanged = ledger.call("GET", "/api/v1/projects/" + project["id"])
    assert status == 200 and unchanged["githubAccess"] == "lost" and not unchanged["canCreateExpense"]
    assert unchanged["githubRepoId"] == 101


def test_standalone_totals_reports_and_csv_keep_one_record_per_expense(ledger):
    project = blank(ledger)
    category_id = category(ledger)
    for index, (amount, currency) in enumerate((("1.23", "USD"), ("7", "JPY"))):
        status, _ = ledger.call("POST", "/api/v1/expenses", expense_body(category_id, project, amount=amount, currency=currency),
            {**ledger.headers, "Idempotency-Key": f"blank-currency-{index}"})
        assert status == 201
    expected = [{"currency": "JPY", "amountMinor": 7}, {"currency": "USD", "amountMinor": 123}]
    assert ledger.call("GET", "/api/v1/projects/" + project["id"])[1]["totals"] == expected
    assert ledger.call("GET", "/api/v1/projects")[1]["items"][0]["totals"] == expected
    _, summary = ledger.call("GET", "/api/v1/reports/summary", params={"projectId": project["id"]})
    assert {(row["currency"], row["amountMinor"]) for row in summary["groups"]} == {("JPY", 7), ("USD", 123)}
    _, export = ledger.call("GET", "/api/v1/expenses/export", params={"projectId": project["id"]})
    async def collect():
        return "".join([chunk async for chunk in export.chunks()])
    rows = list(csv.DictReader(io.StringIO(asyncio.run(collect()))))
    assert len(rows) == 2 and {row["projectId"] for row in rows} == {project["id"]}
    assert ledger.gateway.calls == []


def test_blank_projects_count_toward_quota_and_archiving_does_not_reclaim_capacity(ledger):
    policy = default_policy()
    policy["free"]["projects"] = 1
    add_usage_meter(ledger, policy=policy)
    project = blank(ledger)
    assert update(ledger, project, {"status": "archived"})[0] == 200
    assert ledger.call("POST", "/api/v1/projects", {"name": "Over capacity"}) == (403, {"error": {"code": "PROJECT_LIMIT"}})
    with ledger.store.connect() as db:
        assert tuple(db.execute("SELECT projects,writes FROM ledger_plan_usage").fetchone()) == (1, 2)
        assert db.execute("SELECT COUNT(*) FROM ledger_projects").fetchone()[0] == 1
    assert ledger.gateway.calls == []
