"""Removed projects retain money records without exposing or mutating them."""
import asyncio
import json
from pathlib import Path

import pytest

from pullwise_server.cloudflare_api_key_write import create_api_key
from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1
from pullwise_server.cloudflare_state_records import encode_record, record_name
from test_cloudflare_github_identity_http import GitHubStub
import test_ledger_routes as route_fixture
from test_ledger_automatic_assistance import Provider


@pytest.fixture
def ledger():
    fixture = route_fixture.LedgerRoutesTests()
    fixture.setUp()
    try:
        migrations = Path(__file__).resolve().parents[1] / "cloudflare/server/migrations"
        with fixture.store.connect() as db:
            for name in ("0003_ledger_suggestions.sql", "0004_ledger_plan_usage.sql"):
                db.executescript((migrations / name).read_text())
            name = record_name("users", "usr_github_77")
            user = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?", (name,)).fetchone()[0])
            user["billing"] = {"plan": "max", "status": "active", "currentPeriodEnd": fixture.now + 86400}
            db.execute("UPDATE app_state SET payload=? WHERE name=?",
                (encode_record("users", user["id"], user), name))
        fixture.binding = PlanLimitedD1(fixture.binding, now=fixture.now + 3)
        yield fixture
    finally:
        fixture.tearDown()


def invoke(ledger, method, path, body=None, *, provider=None, headers=None, params=None):
    return asyncio.run(handle_ledger_request(binding=ledger.binding, gateway=GitHubStub(),
        suggestion_gateway=provider, method=method, path=path, body=body,
        headers=headers or ledger.headers, params=params or {}, now=ledger.now + 3))


def setup_money(ledger):
    status, project = invoke(ledger, "POST", "/api/v1/projects", {"name": "Removed later"})
    assert status == 201
    status, category = invoke(ledger, "POST", "/api/v1/categories", {"name": "Hosting"})
    assert status == 201
    draft = {"target": {"kind": "project", "projectId": project["id"]},
        "occurredOn": "2026-10-09", "amount": "12.50", "currency": "USD",
        "categoryId": category["id"], "purpose": "Retained private history"}
    status, expense = invoke(ledger, "POST", "/api/v1/expenses", draft,
        headers={**ledger.headers, "Idempotency-Key": "original-project"})
    assert status == 201
    return project, category, draft, expense


def remove(ledger, project):
    # Model another authorized operation committing between our read/write
    # batches; the project's endpoint and activity logging are tested separately.
    with ledger.store.connect() as db:
        db.execute("""UPDATE ledger_projects SET deleted_at='2026-10-09T00:00:00Z',status='archived',
            github_repo_id=NULL,github_full_name=NULL,github_organization_id=NULL,revision=revision+1 WHERE id=?""",
            (project["id"],))


def facts(ledger):
    with ledger.store.connect() as db:
        return {table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY 1")]
            for table in ("expenses", "expense_events", "expense_create_idempotency",
                "ledger_activity_events", "ledger_plan_usage", "expense_suggestion_budget",
                "expense_suggestion_events")}


def remove_before_batch(ledger, project, predicate):
    binding = ledger.binding.binding
    batch = binding.batch
    fired = []

    async def racing_batch(statements):
        if not fired and predicate(statements):
            fired.append(True)
            remove(ledger, project)
        return await batch(statements)

    binding.batch = racing_batch
    return fired


async def csv_text(export):
    return "".join([chunk async for chunk in export.chunks()])


def test_removed_project_disappears_from_all_financial_views_but_shared_survives(ledger):
    project, category, draft, expense = setup_money(ledger)
    status, shared = invoke(ledger, "POST", "/api/v1/expenses",
        {**draft, "target": {"kind": "shared"}, "purpose": "Shared remains", "amount": "2.00"},
        headers={**ledger.headers, "Idempotency-Key": "shared"})
    assert status == 201
    before = facts(ledger)
    remove(ledger, project)
    for params in ({}, {"target": "shared"}, {"projectId": project["id"]},
                   {"target": "project"}, {"categoryId": category["id"]}, {"limit": "1"}):
        status, page = invoke(ledger, "GET", "/api/v1/expenses", params=params)
        assert status == 200
        assert expense["id"] not in {item["id"] for item in page["items"]}
        assert page["nextCursor"] is None
    for name in ("summary", "timeseries", "categories"):
        status, report = invoke(ledger, "GET", "/api/v1/reports/" + name)
        assert status == 200
        assert all(group["projectId"] is None and group["amountMinor"] == 200
            for group in report["groups"])
        status, report = invoke(ledger, "GET", "/api/v1/reports/" + name,
            params={"projectId": project["id"]})
        assert (status, report["groups"]) == (200, [])
    status, export = invoke(ledger, "GET", "/api/v1/expenses/export")
    assert status == 200
    text = asyncio.run(csv_text(export))
    assert expense["id"] not in text and shared["id"] in text
    assert facts(ledger) == before


@pytest.mark.parametrize("operation", ["get", "new", "replay", "edit", "move", "delete"])
def test_removed_project_direct_operations_are_hidden_and_preserve_records_and_quota(ledger, operation):
    project, _, draft, expense = setup_money(ledger)
    remove(ledger, project)
    before = facts(ledger)
    if operation == "get":
        status, _ = invoke(ledger, "GET", "/api/v1/expenses/" + expense["id"])
    elif operation in {"new", "replay"}:
        status, _ = invoke(ledger, "POST", "/api/v1/expenses", draft,
            headers={**ledger.headers, "Idempotency-Key": "original-project" if operation == "replay" else "new"})
    elif operation in {"edit", "move"}:
        body = {**draft, "purpose": "Changed", **({"target": {"kind": "shared"}} if operation == "move" else {})}
        status, _ = invoke(ledger, "PATCH", "/api/v1/expenses/" + expense["id"], body,
            headers={**ledger.headers, "If-Match": '"1"'})
    else:
        status, _ = invoke(ledger, "DELETE", "/api/v1/expenses/" + expense["id"],
            headers={**ledger.headers, "If-Match": '"1"'})
    assert status == 404
    assert facts(ledger) == before


def test_replay_checks_original_stored_target_after_expense_moves_to_shared(ledger):
    project, _, draft, expense = setup_money(ledger)
    status, moved = invoke(ledger, "PATCH", "/api/v1/expenses/" + expense["id"],
        {**draft, "target": {"kind": "shared"}}, headers={**ledger.headers, "If-Match": '"1"'})
    assert status == 200
    remove(ledger, project)
    before = facts(ledger)
    status, result = invoke(ledger, "POST", "/api/v1/expenses", draft,
        headers={**ledger.headers, "Idempotency-Key": "original-project"})
    assert (status, result["error"]["code"]) == (404, "NOT_FOUND")
    status, visible = invoke(ledger, "GET", "/api/v1/expenses/" + expense["id"])
    assert status == 200 and visible["target"] == moved["target"]
    assert facts(ledger) == before


def test_replay_checks_current_project_after_shared_expense_moves_into_it(ledger):
    project, _, draft, _ = setup_money(ledger)
    shared_draft = {**draft, "target": {"kind": "shared"}}
    status, expense = invoke(ledger, "POST", "/api/v1/expenses", shared_draft,
        headers={**ledger.headers, "Idempotency-Key": "shared-original"})
    assert status == 201
    status, _ = invoke(ledger, "PATCH", "/api/v1/expenses/" + expense["id"], draft,
        headers={**ledger.headers, "If-Match": '"1"'})
    assert status == 200
    remove(ledger, project)
    before = facts(ledger)
    status, result = invoke(ledger, "POST", "/api/v1/expenses", shared_draft,
        headers={**ledger.headers, "Idempotency-Key": "shared-original"})
    assert (status, result["error"]["code"]) == (404, "NOT_FOUND")
    assert facts(ledger) == before


def test_archival_retains_reads_historical_edits_and_original_replay(ledger):
    project, _, draft, expense = setup_money(ledger)
    with ledger.store.connect() as db:
        db.execute("UPDATE ledger_projects SET status='archived',revision=revision+1 WHERE id=?", (project["id"],))
        db.execute("UPDATE expense_categories SET archived_at='now' WHERE id=?", (draft["categoryId"],))
    before = facts(ledger)
    provider = Provider()
    status, replay = invoke(ledger, "POST", "/api/v1/expenses", draft, provider=provider,
        headers={**ledger.headers, "Idempotency-Key": "original-project"})
    assert status == 201 and replay["id"] == expense["id"]
    assert not provider.calls and facts(ledger) == before
    status, edited = invoke(ledger, "PATCH", "/api/v1/expenses/" + expense["id"],
        {**draft, "purpose": "Archived history edited"}, headers={**ledger.headers, "If-Match": '"1"'})
    assert status == 200 and edited["purpose"] == "Archived history edited"
    assert invoke(ledger, "GET", "/api/v1/expenses/" + expense["id"])[0] == 200


def test_deleted_expense_replay_is_preserved_until_its_project_is_removed(ledger):
    project, _, draft, expense = setup_money(ledger)
    assert invoke(ledger, "DELETE", "/api/v1/expenses/" + expense["id"],
        headers={**ledger.headers, "If-Match": '"1"'})[0] == 204
    status, replay = invoke(ledger, "POST", "/api/v1/expenses", draft,
        headers={**ledger.headers, "Idempotency-Key": "original-project"})
    assert status == 201 and replay["id"] == expense["id"]
    remove(ledger, project)
    before = facts(ledger)
    assert invoke(ledger, "DELETE", "/api/v1/expenses/" + expense["id"],
        headers={**ledger.headers, "If-Match": '"2"'})[0] == 404
    assert invoke(ledger, "POST", "/api/v1/expenses", draft,
        headers={**ledger.headers, "Idempotency-Key": "original-project"})[0] == 404
    assert facts(ledger) == before


@pytest.mark.parametrize("operation", ["edit", "move", "delete"])
def test_old_target_removal_between_read_and_mutation_rolls_back_all_expense_effects(ledger, operation):
    project, _, draft, expense = setup_money(ledger)
    before = facts(ledger)
    fired = remove_before_batch(ledger, project, lambda statements: any(
        statement.sql.lstrip().startswith("UPDATE expenses") for statement in statements))
    method = "DELETE" if operation == "delete" else "PATCH"
    body = None if method == "DELETE" else {**draft,
        **({"target": {"kind": "shared"}} if operation == "move" else {"purpose": "Changed"})}
    status, _ = invoke(ledger, method, "/api/v1/expenses/" + expense["id"], body,
        headers={**ledger.headers, "If-Match": '"1"'})
    assert fired and status == 412
    assert facts(ledger) == before


@pytest.mark.parametrize("operation", ["new", "move"])
def test_new_target_removal_before_atomic_batch_rolls_back_creation_or_move(ledger, operation):
    project, _, draft, expense = setup_money(ledger)
    if operation == "move":
        status, expense = invoke(ledger, "PATCH", "/api/v1/expenses/" + expense["id"],
            {**draft, "target": {"kind": "shared"}}, headers={**ledger.headers, "If-Match": '"1"'})
        assert status == 200
    before = facts(ledger)
    fired = remove_before_batch(ledger, project, lambda statements: any(
        statement.sql.lstrip().startswith("INSERT INTO expenses" if operation == "new" else "UPDATE expenses")
        for statement in statements))
    status, _ = invoke(ledger, "POST" if operation == "new" else "PATCH",
        "/api/v1/expenses" if operation == "new" else "/api/v1/expenses/" + expense["id"], draft,
        headers={**ledger.headers, "Idempotency-Key": "new-race", "If-Match": '"2"'})
    assert fired and status == (409 if operation == "new" else 412)
    assert facts(ledger) == before


def test_removed_project_key_scope_does_not_expose_history_or_grant_shared_access(ledger):
    project, _, _, expense = setup_money(ledger)
    status, key = asyncio.run(create_api_key(binding=ledger.binding, headers=ledger.headers,
        body={"scopes": ["expenses:read", "reports:read", "expenses:write"],
            "restrictions": {"projectIds": [project["id"]], "shared": False}}, now=ledger.now + 3))
    assert status == 201
    headers = {"Authorization": "Bearer " + key["key"]}
    remove(ledger, project)
    assert invoke(ledger, "GET", "/api/v1/expenses", headers=headers)[1]["items"] == []
    assert invoke(ledger, "GET", "/api/v1/reports/summary", headers=headers)[1]["groups"] == []
    assert invoke(ledger, "GET", "/api/v1/expenses/" + expense["id"], headers=headers)[0] == 404
    assert invoke(ledger, "GET", "/api/v1/expenses", headers=headers, params={"target": "shared"})[0] == 403


def test_each_csv_page_rechecks_project_visibility(ledger):
    project, category, _, expense = setup_money(ledger)
    with ledger.store.connect() as db:
        owner = db.execute("SELECT owner_id FROM ledger_projects WHERE id=?", (project["id"],)).fetchone()[0]
        db.executemany("""INSERT INTO expenses(id,owner_id,target_kind,category_id,occurred_on,
            amount_minor,currency,purpose,created_at,updated_at)
            VALUES(?,?,'shared',?,'2026-10-08',1,'USD','First page','now','now')""",
            [(f"exp_page_{index:04d}", owner, category["id"]) for index in range(250)])
    status, export = invoke(ledger, "GET", "/api/v1/expenses/export")
    assert status == 200
    remove(ledger, project)
    text = asyncio.run(csv_text(export))
    assert text.count("exp_page_") == 250 and expense["id"] not in text


@pytest.mark.parametrize("stage", ["initial", "reservation", "provider"])
def test_suggestion_removal_fences_prevent_removed_project_results(ledger, stage):
    project, _, draft, _ = setup_money(ledger)
    provider = Provider(on_call=(lambda: remove(ledger, project)) if stage == "provider" else None)
    if stage == "initial":
        remove(ledger, project)
    elif stage == "reservation":
        fired = remove_before_batch(ledger, project, lambda statements: any(
            "INSERT INTO expense_suggestion_budget" in statement.sql for statement in statements))
    before = facts(ledger)
    status, result = invoke(ledger, "POST", "/api/v1/expense-suggestions",
        {"target": draft["target"], "purpose": draft["purpose"]}, provider=provider)
    assert status == (404 if stage == "initial" else 409)
    assert "suggestions" not in result
    if stage == "reservation":
        assert fired
    after = facts(ledger)
    assert len(provider.calls) == (1 if stage == "provider" else 0)
    if stage != "provider":
        assert after == before
    else:
        assert after["expense_suggestion_events"] == before["expense_suggestion_events"]
        assert len(after["expense_suggestion_budget"]) == 1
        for table in ("expenses", "expense_events", "expense_create_idempotency", "ledger_activity_events"):
            assert after[table] == before[table]


@pytest.mark.parametrize("stage", ["initial", "write"])
def test_suggestion_decision_checks_removed_original_draft_even_when_new_target_shared(ledger, stage):
    project, category, draft, _ = setup_money(ledger)
    status, suggestion = invoke(ledger, "POST", "/api/v1/expense-suggestions",
        {"target": draft["target"], "purpose": draft["purpose"]}, provider=Provider())
    assert status == 200
    if stage == "initial":
        remove(ledger, project)
    else:
        fired = remove_before_batch(ledger, project, lambda statements: any(
            "UPDATE expense_suggestion_events" in statement.sql for statement in statements))
    before = facts(ledger)
    status, _ = invoke(ledger, "POST", "/api/v1/expense-suggestions/" + suggestion["suggestionId"] + "/decision",
        {"target": {"kind": "shared"}, "categoryId": category["id"]})
    assert status == (404 if stage == "initial" else 409)
    if stage == "write":
        assert fired
    assert facts(ledger) == before
