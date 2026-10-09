"""Explicit category removal withdraws configuration and preserves recorded facts.

Canonical REST handlers and plan guards run over isolated SQLite transactions.
These tests do not establish Cloudflare native row metadata; native acceptance
uses the unchanged preview SQL meter separately.
"""
import asyncio
import json

import pytest

from test_rest_key_permissions import OWNER, cookie, credentials, issue, rest_db, route
from test_rest_key_permissions import NOW, NoGitHub
from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_preview_budget import sql_write_bound


def category(app, name="Unused"):
    status, item = route(app, "POST", "/api/v1/categories", cookie(), {"name": name})
    assert status == 201, item
    return item


def remove(app, item, *, headers=None, revision=None, body=None):
    return route(app, "POST", "/api/v1/categories/" + item["id"] + "/remove",
        {**(cookie() if headers is None else headers),
         "If-Match": f'"{item["revision"] if revision is None else revision}"'},
        {} if body is None else body)


def expense(app, category_id="cat_rest", key="synthetic-category-removal"):
    draft = {"target": {"kind": "shared"}, "occurredOn": "2026-10-09",
             "amount": "12.34", "currency": "USD", "categoryId": category_id,
             "purpose": "Synthetic preservation acceptance", "note": "Keep this note"}
    status, item = route(app, "POST", "/api/v1/expenses",
        {**cookie(), "Idempotency-Key": key}, draft)
    assert status == 201, item
    return item, draft


def recurring(app):
    draft = {"target": {"kind": "shared"}, "amount": "12.34", "currency": "USD",
             "categoryId": "cat_rest", "purpose": "Synthetic recurring acceptance",
             "schedule": {"frequency": "monthly", "day": 1, "timezone": "UTC", "startOn": "2027-02-01"}}
    status, item = route(app, "POST", "/api/v1/expense-recurring-rules",
        {**cookie(), "Idempotency-Key": "synthetic-category-rule"}, draft)
    assert status == 201, item
    return item


def contents(app):
    with app.store.connect() as db:
        return {table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
            for table in ("expense_categories", "expenses", "expense_events",
                          "expense_create_idempotency", "expense_recurring_rules",
                          "expense_suggestion_events", "ledger_plan_usage", "d1_command_guard")}


@pytest.mark.parametrize("archived", [False, True])
def test_unused_active_or_archived_category_is_removed_without_touching_other_records(rest_db, archived):
    preserved, _ = expense(rest_db)
    item = category(rest_db)
    if archived:
        assert route(rest_db, "DELETE", "/api/v1/categories/" + item["id"],
            {**cookie(), "If-Match": '"1"'}) == (204, None)
        item = {**item, "revision": 2}
    before = contents(rest_db)
    assert remove(rest_db, item) == (204, None)
    after = contents(rest_db)
    removed = next(row for row in after["expense_categories"] if row[0] == item["id"])
    assert removed[-1] is not None and removed[2] == item["name"]
    for table in ("expenses", "expense_events", "expense_create_idempotency", "expense_recurring_rules"):
        assert before[table] == after[table]
    assert route(rest_db, "GET", "/api/v1/expenses/" + preserved["id"], cookie())[0] == 200
    assert remove(rest_db, item) == (412, {"error": {"code": "PRECONDITION_FAILED"}})
    unchanged = contents(rest_db)
    assert remove(rest_db, item, revision=item["revision"] + 1) == (204, None)
    assert contents(rest_db) == unchanged
    assert category(rest_db)["id"] != item["id"]


@pytest.mark.parametrize("deleted", [False, True])
def test_recorded_expenses_do_not_prevent_removal_even_after_explicit_expense_removal(rest_db, deleted):
    item = category(rest_db)
    entry, _ = expense(rest_db, item["id"])
    if deleted:
        assert route(rest_db, "DELETE", "/api/v1/expenses/" + entry["id"],
            {**cookie(), "If-Match": '"1"'}) == (204, None)
    before = contents(rest_db)
    assert remove(rest_db, item) == (204, None)
    after = contents(rest_db)
    for table in ("expenses", "expense_events", "expense_create_idempotency", "expense_recurring_rules"):
        assert after[table] == before[table]
    assert item["id"] not in {row["id"] for row in route(rest_db, "GET", "/api/v1/categories", cookie())[1]}
    assert remove(rest_db, {**item, "revision": 2}) == (204, None)


def test_switching_expense_category_preserves_old_history_and_exact_creation_replay(rest_db):
    old = category(rest_db, "Old selection")
    new = category(rest_db, "Current selection")
    original, draft = expense(rest_db, old["id"])
    assert route(rest_db, "PATCH", "/api/v1/expenses/" + original["id"],
        {**cookie(), "If-Match": '"1"'}, {**draft, "categoryId": new["id"]})[0] == 200
    before = contents(rest_db)
    assert remove(rest_db, old) == (204, None)
    after = contents(rest_db)
    for table in ("expenses", "expense_events", "expense_create_idempotency"):
        assert after[table] == before[table]
    assert route(rest_db, "POST", "/api/v1/expenses",
        {**cookie(), "Idempotency-Key": "synthetic-category-removal"}, draft) == (201, original)


@pytest.mark.parametrize("source", ["event_before", "event_after", "event_assistance", "creation", "assistance"])
def test_each_persistent_expense_reference_is_retained_without_blocking_removal(rest_db, source):
    item = category(rest_db)
    original, _ = expense(rest_db)
    with rest_db.store.connect() as db:
        if source.startswith("event_"):
            field = "before_json" if source == "event_before" else "after_json"
            snapshot = {**original, "categoryId": item["id"]}
            if source == "event_assistance":
                snapshot = {**original, "assistance": {"status": "available", "suggestions": {"categoryId": item["id"]}}}
            db.execute("UPDATE expense_events SET " + field + "=? WHERE expense_id=?",
                (json.dumps(snapshot), original["id"]))
        else:
            response = json.loads(db.execute("SELECT response_json FROM expense_create_idempotency").fetchone()[0])
            if source == "creation":
                response["categoryId"] = item["id"]
            else:
                response["assistance"] = {"status": "available", "suggestions": {"categoryId": item["id"]}}
            db.execute("UPDATE expense_create_idempotency SET response_json=?", (json.dumps(response),))
    before = contents(rest_db)
    assert remove(rest_db, item) == (204, None)
    after = contents(rest_db)
    for table in ("expenses", "expense_events", "expense_create_idempotency"):
        assert after[table] == before[table]


@pytest.mark.parametrize("status", ["active", "paused", "blocked", "completed", "canceled"])
@pytest.mark.parametrize("source", ["template", "creation"])
def test_recurring_current_and_original_category_references_preserve_every_rule_status(rest_db, status, source):
    item = category(rest_db)
    rule = recurring(rest_db)
    field, path = ("template_json", "category_id") if source == "template" else ("create_response_json", "categoryId")
    with rest_db.store.connect() as db:
        snapshot = json.loads(db.execute("SELECT " + field + " FROM expense_recurring_rules WHERE id=?", (rule["id"],)).fetchone()[0])
        snapshot[path] = item["id"]
        db.execute("UPDATE expense_recurring_rules SET " + field + "=?,status=? WHERE id=?",
            (json.dumps(snapshot), status, rule["id"]))
    before = contents(rest_db)
    assert remove(rest_db, item) == (204, None)
    assert contents(rest_db)["expense_recurring_rules"] == before["expense_recurring_rules"]


@pytest.mark.parametrize("source", ["suggested", "accepted", "candidate_only"])
def test_selected_and_candidate_jev_categories_do_not_block_removal_or_rewrite_model_history(rest_db, source):
    item = category(rest_db)
    with rest_db.store.connect() as db:
        db.execute("""INSERT INTO expense_suggestion_events(id,owner_id,created_at,question_version,
            draft_target_kind,outcome,category_id,accepted_category_id,category_probabilities_json)
            VALUES('suggestion_only',?,'synthetic','synthetic','shared','available',?,?,?)""",
            (OWNER, item["id"] if source == "suggested" else "cat_rest",
             item["id"] if source == "accepted" else None,
             json.dumps({item["id"]: 0.2, "cat_rest": 0.8})))
    before = contents(rest_db)
    assert remove(rest_db, item) == (204, None)
    after = contents(rest_db)
    assert before["expense_suggestion_events"] == after["expense_suggestion_events"]
    assert next(row for row in after["expense_categories"] if row[0] == item["id"])[-1] is not None


@pytest.mark.parametrize("revision", [None, "1", '"2"', '"0"', '"9007199254740992"'])
def test_removal_requires_exact_bounded_revision_without_writes(rest_db, revision):
    item = category(rest_db)
    headers = cookie()
    if revision is not None:
        headers["If-Match"] = revision
    before = contents(rest_db)
    expected = 428 if revision is None else 412 if revision == '"2"' else 422
    assert route(rest_db, "POST", "/api/v1/categories/" + item["id"] + "/remove", headers, {})[0] == expected
    assert contents(rest_db) == before


@pytest.mark.parametrize("role", ["owner", "admin", "editor", "viewer"])
def test_cookie_removal_uses_current_ledger_management_role(rest_db, role):
    item = category(rest_db)
    before = contents(rest_db)
    result = remove(rest_db, item, headers=cookie(role))
    if role in {"owner", "admin"}:
        assert result == (204, None)
    else:
        assert result == (403, {"error": {"code": "ROLE_FORBIDDEN"}})
        assert contents(rest_db) == before


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_categories_write_only_keys_can_remove_without_categories_read_scope(rest_db, role):
    item = category(rest_db)
    status, key = issue(rest_db, ["categories:write"], role=role)
    assert status == 201
    assert remove(rest_db, item, headers=credentials(key)) == (204, None)


def test_scope_and_workspace_fences_deny_removal_without_mutation(rest_db):
    item = category(rest_db)
    status, key = issue(rest_db, ["categories:read"])
    assert status == 201
    before = contents(rest_db)
    assert remove(rest_db, item, headers=credentials(key)) == (403, {"error": {"code": "INSUFFICIENT_SCOPE"}})
    assert remove(rest_db, item, headers={**cookie("admin"),
        "X-Pullwise-Workspace": rest_db.actors["admin"]}) == (404, {"error": {"code": "NOT_FOUND"}})
    assert contents(rest_db) == before


@pytest.mark.parametrize("race", ["revision", "reference", "membership"])
def test_atomic_races_rollback_plan_charge_and_reauthenticate_denials(rest_db, race):
    item = category(rest_db)
    raw = rest_db.binding.binding
    original_batch = raw.batch
    raced = False

    async def racing_batch(statements):
        nonlocal raced
        statements = list(statements)
        if not raced and any(statement.sql.startswith("UPDATE expense_categories SET deleted_at") for statement in statements):
            raced = True
            with rest_db.store.connect() as db:
                if race == "revision":
                    db.execute("UPDATE expense_categories SET revision=revision+1 WHERE id=?", (item["id"],))
                elif race == "membership":
                    db.execute("UPDATE workspace_members SET removed_at='synthetic',revision=revision+1 WHERE workspace_id=? AND user_id=?",
                        (OWNER, rest_db.actors["admin"]))
                else:
                    db.execute("""INSERT INTO expenses(id,owner_id,target_kind,category_id,occurred_on,
                        amount_minor,currency,purpose,created_at,updated_at)
                        VALUES('racing_expense',?,'shared',?,'2026-10-09',1234,'USD','Concurrent fact','synthetic','synthetic')""",
                        (OWNER, item["id"]))
        return await original_batch(statements)

    raw.batch = racing_batch
    before = contents(rest_db)
    result = remove(rest_db, item, headers=cookie("admin" if race == "membership" else "owner"))
    assert raced
    assert result[0] == (412 if race == "revision" else 404 if race == "membership" else 204)
    after = contents(rest_db)
    if race != "reference":
        assert before["ledger_plan_usage"] == after["ledger_plan_usage"]
    else:
        assert any(row[0] == "racing_expense" for row in after["expenses"])
    assert before["d1_command_guard"] == after["d1_command_guard"] == []
    assert any(row[0] == item["id"] for row in after["expense_categories"])


def test_unknown_d1_failure_is_never_reclassified_as_category_conflict(rest_db):
    item = category(rest_db)
    raw = rest_db.binding.binding
    original_batch = raw.batch

    async def failing_batch(statements):
        statements = list(statements)
        if any(statement.sql.startswith("UPDATE expense_categories SET deleted_at") for statement in statements):
            raise RuntimeError("synthetic unknown transport outcome")
        return await original_batch(statements)

    raw.batch = failing_batch
    with pytest.raises(RuntimeError, match="unknown transport outcome"):
        remove(rest_db, item)


def test_explicit_metadata_route_preserves_original_removed_label_and_normal_list_contract(rest_db):
    item = category(rest_db, "Original historical label")
    assert remove(rest_db, item) == (204, None)
    before = contents(rest_db)
    normal_status, normal = route(rest_db, "GET", "/api/v1/categories", cookie())
    assert normal_status == 200 and all(value["id"] != item["id"] for value in normal)
    assert all("removedAt" not in value for value in normal)
    status, metadata = asyncio.run(handle_ledger_request(binding=rest_db.binding,
        gateway=NoGitHub(), method="GET", path="/api/v1/categories", headers=cookie("viewer"),
        params={"includeRemoved": "true"}, body=None, now=NOW))
    assert status == 200
    saved = next(value for value in metadata if value["id"] == item["id"])
    assert saved["name"] == item["name"] and saved["removedAt"] is not None
    assert set(saved) == {"id", "name", "color", "revision", "archivedAt", "removedAt"}
    assert contents(rest_db) == before


@pytest.mark.parametrize("body", [None, [], {"force": True}, {"ownerId": OWNER}])
def test_removal_route_requires_empty_object_without_mutation(rest_db, body):
    item = category(rest_db)
    before = contents(rest_db)
    assert route(rest_db, "POST", "/api/v1/categories/" + item["id"] + "/remove",
        {**cookie(), "If-Match": '"1"'}, body) == (422, {"error": {"code": "INVALID_INPUT"}})
    assert contents(rest_db) == before


def test_category_metadata_queries_use_owner_indexes_and_remove_has_a_metered_primary_key_fence(rest_db):
    item = category(rest_db)
    with rest_db.store.connect() as db:
        details = [row[3] for row in db.execute("EXPLAIN QUERY PLAN SELECT * FROM expense_categories "
            "WHERE owner_id=? AND deleted_at IS NULL ORDER BY name,id", (OWNER,))]
    assert any("SEARCH expense_categories" in detail and "owner_id=?" in detail for detail in details), details
    assert not any("SCAN expense_categories" in detail for detail in details), details
    assert sql_write_bound("""UPDATE expense_categories SET deleted_at=?,
        archived_at=COALESCE(archived_at,?),revision=revision+1,updated_at=?
        WHERE id=? AND owner_id=? AND revision=? AND deleted_at IS NULL""") == 7
