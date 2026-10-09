"""Category history labels over isolated real SQLite transactions, not native D1."""
import asyncio

import pytest

from test_rest_key_permissions import NOW, OWNER, cookie, credentials, issue, rest_db, route
from test_ledger_recurring import (
    app as recurring_app, draft as recurring_draft, NoGitHub, NOW as RECURRING_NOW,
)
from pullwise_server.cloudflare_ledger_api import MAX_REVISION, handle_ledger_request
from pullwise_server import cloudflare_ledger_categories as categories
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1
from pullwise_server.cloudflare_preview_budget import sql_write_bound


@pytest.fixture
def app(rest_db):
    return rest_db


_EMPTY_BODY = object()


def remove(app, item, *, headers=None, revision=None, body=_EMPTY_BODY):
    chosen = {**(cookie() if headers is None else headers),
        "If-Match": f'"{item["revision"] if revision is None else revision}"'}
    return asyncio.run(categories.remove_category(binding=app.binding, item_id=item["id"],
        headers=chosen, body={} if body is _EMPTY_BODY else body, now=NOW))


def listing(app, params=None, headers=None):
    return asyncio.run(categories.list_categories(binding=app.binding,
        headers=cookie() if headers is None else headers, params=params or {}, now=NOW))


def category(app, name="Removable"):
    status, item = route(app, "POST", "/api/v1/categories", cookie(), {"name": name})
    assert status == 201
    return item


def row(app, identifier):
    with app.store.connect() as db:
        return dict(db.execute("SELECT * FROM expense_categories WHERE id=?", (identifier,)).fetchone())


def contents(app):
    with app.store.connect() as db:
        return {table: [tuple(item) for item in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                for table in ("expenses", "expense_events", "expense_create_idempotency",
                    "expense_recurring_rules", "expense_recurring_occurrences",
                    "expense_suggestion_events", "expense_suggestion_budget",
                    "ledger_activity_events", "ledger_plan_usage", "d1_command_guard")}


def financial(app, item):
    draft = {"target": {"kind": "shared"}, "occurredOn": "2026-10-09", "amount": "12.34",
        "currency": "USD", "categoryId": item["id"], "purpose": "Saved financial facts"}
    headers = {**cookie(), "Idempotency-Key": "category-history"}
    status, entry = route(app, "POST", "/api/v1/expenses", headers, draft)
    assert status == 201
    return draft, headers, entry


def preserved_business(before, after):
    for table in before:
        if table != "ledger_plan_usage":
            assert after[table] == before[table]
    # Capacity/paid model reservation columns must not be affected.
    assert after["ledger_plan_usage"][0][1:3] == before["ledger_plan_usage"][0][1:3]
    assert after["ledger_plan_usage"][0][7] == before["ledger_plan_usage"][0][7]


@pytest.mark.parametrize("archived", [False, True])
def test_active_or_archived_category_withdraws_but_keeps_original_identity_and_label(app, archived):
    item = category(app)
    if archived:
        assert route(app, "DELETE", "/api/v1/categories/" + item["id"],
            {**cookie(), "If-Match": '"1"'}) == (204, None)
        item = {**item, "revision": 2}
    original = row(app, item["id"])
    assert remove(app, item) == (204, None)
    saved = row(app, item["id"])
    for field in ("id", "owner_id", "name", "color", "created_at"):
        assert saved[field] == original[field]
    assert saved["revision"] == original["revision"] + 1
    assert saved["deleted_at"] is not None and saved["archived_at"] is not None
    if archived:
        assert saved["archived_at"] == original["archived_at"]
    assert item["id"] not in {value["id"] for value in listing(app)[1]}
    assert item["id"] not in {value["id"] for value in listing(app, {"includeRemoved": "false"})[1]}
    metadata = next(value for value in listing(app, {"includeRemoved": ["true"]})[1]
                    if value["id"] == item["id"])
    assert metadata["removedAt"] == saved["deleted_at"] and metadata["name"] == item["name"]
    assert "removedAt" not in next(value for value in listing(app)[1] if value["id"] == "cat_rest")
    assert category(app)["id"] != item["id"]


@pytest.mark.parametrize("deleted", [False, True])
def test_live_or_soft_deleted_shared_history_does_not_block_removal_or_change_history(app, deleted):
    item = category(app)
    draft, headers, entry = financial(app, item)
    if deleted:
        assert route(app, "DELETE", "/api/v1/expenses/" + entry["id"],
            {**cookie(), "If-Match": '"1"'}) == (204, None)
    before = contents(app)
    assert remove(app, item) == (204, None)
    preserved_business(before, contents(app))
    assert route(app, "POST", "/api/v1/expenses", headers, draft) == (201, entry)
    assert row(app, item["id"])["name"] == item["name"]


@pytest.mark.parametrize("status", ["active", "paused", "blocked", "completed", "canceled"])
def test_all_recurring_states_and_jev_references_remain_unchanged(app, status):
    item = category(app)
    draft = {"target": {"kind": "shared"}, "amount": "12.34", "currency": "USD",
        "categoryId": item["id"], "purpose": "Historical recurrence", "schedule": {
            "frequency": "monthly", "day": 1, "timezone": "UTC", "startOn": "2027-02-01"}}
    code, rule = route(app, "POST", "/api/v1/expense-recurring-rules",
        {**cookie(), "Idempotency-Key": "category-rule"}, draft)
    assert code == 201
    with app.store.connect() as db:
        db.execute("UPDATE expense_recurring_rules SET status=? WHERE id=?", (status, rule["id"]))
        db.execute("""INSERT INTO expense_suggestion_events(id,owner_id,created_at,question_version,
            draft_target_kind,outcome,category_id,accepted_category_id)
            VALUES('category-suggestion',?,'synthetic','synthetic','shared','available',?,?)""",
            (OWNER, item["id"], item["id"]))
    before = contents(app)
    assert remove(app, item) == (204, None)
    preserved_business(before, contents(app))
    assert row(app, item["id"])["archived_at"] is not None


def test_removed_category_blocks_next_due_once_until_schedule_is_replaced_and_resumed(recurring_app):
    app = recurring_app
    code, rule = app.call("POST", body=recurring_draft())
    assert code == 201
    original = app.rows("expense_recurring_rules")
    headers = {"Cookie": "pw_session=owner", "Origin": "https://app.example.test"}
    binding = PlanLimitedD1(app.raw, policy=app.policy, now=RECURRING_NOW)
    assert asyncio.run(categories.remove_category(binding=binding, item_id="cat_1",
        headers={**headers, "If-Match": '"1"'}, body={}, now=RECURRING_NOW)) == (204, None)
    assert app.rows("expense_recurring_rules") == original
    assert app.tick() == {"scanned": 1, "created": 0, "blocked": 1, "replayed": 0}
    blocked = app.call("GET", rule["id"])[1]
    assert blocked["status"] == "blocked" and blocked["blockedCode"] == "INVALID_CATEGORY"
    assert blocked["nextOccurrenceOn"] == rule["nextOccurrenceOn"]
    assert app.tick()["scanned"] == 0
    assert app.rows("expenses") == app.rows("expense_recurring_occurrences") == []

    code, replacement = asyncio.run(handle_ledger_request(
        binding=binding, gateway=NoGitHub(),
        method="POST", path="/api/v1/categories", headers=headers, params={},
        body={"name": "Replacement category"}, now=RECURRING_NOW))
    assert code == 201
    code, changed = app.call("PATCH", rule["id"],
        recurring_draft(categoryId=replacement["id"]), revision=blocked["revision"])
    assert code == 200 and changed["status"] == "blocked"
    code, resumed = app.call("PATCH", rule["id"], {"status": "active"}, revision=changed["revision"])
    assert code == 200 and resumed["status"] == "active"
    assert app.tick(now=RECURRING_NOW + 25 * 86400)["created"] == 1
    assert len(app.rows("expenses")) == 1
    assert app.rows("expenses")[0]["category_id"] == replacement["id"]


def test_original_removed_category_can_be_retained_on_edit_but_not_newly_assigned(app):
    item = category(app)
    draft, _, entry = financial(app, item)
    assert remove(app, item) == (204, None)
    assert route(app, "PATCH", "/api/v1/expenses/" + entry["id"],
        {**cookie(), "If-Match": '"1"'}, {**draft, "note": "Allowed original label"})[0] == 200
    assert route(app, "POST", "/api/v1/expenses",
        {**cookie(), "Idempotency-Key": "new-category"}, draft) == (
            422, {"error": {"code": "INVALID_CATEGORY"}})
    assert route(app, "PATCH", "/api/v1/categories/" + item["id"],
        {**cookie(), "If-Match": '"2"'}, {"name": "Attempt rename"})[0] == 409


def test_repeat_requires_current_revision_and_does_not_charge_again(app):
    item = category(app)
    assert remove(app, item) == (204, None)
    saved, before = row(app, item["id"]), contents(app)
    assert remove(app, item) == (412, {"error": {"code": "PRECONDITION_FAILED"}})
    assert remove(app, item, revision=2) == (204, None)
    assert row(app, item["id"]) == saved and contents(app) == before


@pytest.mark.parametrize("role", ["owner", "admin", "editor", "viewer"])
def test_cookie_management_requires_current_workspace_role(app, role):
    item = category(app)
    before = row(app, item["id"]), contents(app)
    code, _ = remove(app, item, headers=cookie(role))
    assert code == (204 if role in {"owner", "admin"} else 403)
    if role in {"editor", "viewer"}:
        assert (row(app, item["id"]), contents(app)) == before


@pytest.mark.parametrize("role", ["owner", "admin"])
@pytest.mark.parametrize("transport", ["bearer", "header"])
def test_write_only_key_keeps_existing_category_management_scope(app, role, transport):
    item = category(app)
    code, key = issue(app, ["categories:write"], role=role)
    assert code == 201
    headers = credentials(key, transport=transport)
    assert listing(app, {"includeRemoved": "true"}, headers)[0] == 403
    assert remove(app, item, headers=headers) == (204, None)


@pytest.mark.parametrize("role", ["owner", "admin", "editor", "viewer"])
def test_metadata_uses_existing_read_authority_without_writes(app, role):
    item = category(app)
    assert remove(app, item) == (204, None)
    before = contents(app)
    code, values = listing(app, {"includeRemoved": "true"}, cookie(role))
    assert code == 200 and any(value["id"] == item["id"] for value in values)
    assert contents(app) == before
    code, key = issue(app, ["categories:read"], role=role)
    assert code == 201 and listing(app, {"includeRemoved": "true"}, credentials(key))[0] == 200


@pytest.mark.parametrize("value", ["", "1", "True", "TRUE", 1, True, [], ["true", "false"]])
def test_invalid_metadata_parameter_is_rejected_before_sql(app, value):
    original = app.binding.prepare
    def reject_sql(_):
        raise AssertionError("invalid metadata input reached SQL")
    app.binding.prepare = reject_sql
    try:
        assert listing(app, {"includeRemoved": value}, {}) == (422, {"error": {"code": "INVALID_INPUT"}})
    finally:
        app.binding.prepare = original


def test_other_owner_and_mixed_credentials_cannot_access_current_category(app):
    item = category(app)
    assert remove(app, item, headers={**cookie("admin"),
        "X-Pullwise-Workspace": app.actors["admin"]}) == (404, {"error": {"code": "NOT_FOUND"}})
    assert item["id"] not in {value["id"] for value in listing(app,
        {"includeRemoved": "true"}, {**cookie("admin"), "X-Pullwise-Workspace": app.actors["admin"]})[1]}
    code, key = issue(app, ["categories:write"])
    assert code == 201
    assert remove(app, item, headers={**cookie(), **credentials(key)})[0] == 400
    assert row(app, item["id"])["deleted_at"] is None


@pytest.mark.parametrize("revision,status", [(None, 428), ('"0"', 422), ('1', 422), ('"2"', 412)])
def test_requires_current_bounded_if_match_without_mutation(app, revision, status):
    item = category(app)
    headers = cookie()
    if revision is not None:
        headers["If-Match"] = revision
    before = row(app, item["id"]), contents(app)
    assert asyncio.run(categories.remove_category(binding=app.binding, item_id=item["id"],
        headers=headers, body={}, now=NOW))[0] == status
    assert (row(app, item["id"]), contents(app)) == before


@pytest.mark.parametrize("body", [None, [], {"ownerId": OWNER}, {"force": True}])
def test_rejects_nonempty_or_nonobject_body_without_mutation(app, body):
    item = category(app)
    before = row(app, item["id"]), contents(app)
    assert remove(app, item, body=body)[0] == 422
    assert (row(app, item["id"]), contents(app)) == before


def test_max_revision_cannot_overflow_and_removed_max_revision_is_noop(app):
    item = category(app)
    with app.store.connect() as db:
        db.execute("UPDATE expense_categories SET revision=? WHERE id=?", (MAX_REVISION, item["id"]))
    before = contents(app)
    assert remove(app, item, revision=MAX_REVISION) == (409, {"error": {"code": "REVISION_LIMIT"}})
    assert contents(app) == before
    with app.store.connect() as db:
        db.execute("UPDATE expense_categories SET archived_at=?,deleted_at=? WHERE id=?",
            ("2026-10-09T00:00:00Z", "2026-10-09T00:00:00Z", item["id"]))
    assert remove(app, item, revision=MAX_REVISION) == (204, None)


@pytest.mark.parametrize("race", ["revision", "membership", "reference"])
def test_fences_roll_back_charge_or_allow_new_history_without_erasure(app, race):
    item = category(app)
    raw, raced = app.binding.binding, False
    original_batch = raw.batch
    async def racing_batch(statements):
        nonlocal raced
        statements = list(statements)
        if not raced and any(statement.sql.startswith("UPDATE expense_categories SET deleted_at") for statement in statements):
            raced = True
            with app.store.connect() as db:
                if race == "revision":
                    db.execute("UPDATE expense_categories SET revision=revision+1 WHERE id=?", (item["id"],))
                elif race == "membership":
                    db.execute("UPDATE workspace_members SET removed_at='synthetic',revision=revision+1 WHERE workspace_id=? AND user_id=?",
                        (OWNER, app.actors["admin"]))
                else:
                    db.execute("""INSERT INTO expenses(id,owner_id,target_kind,category_id,occurred_on,
                        amount_minor,currency,purpose,created_at,updated_at)
                        VALUES('concurrent-history',?,'shared',?,'2026-10-09',1234,'USD','Concurrent history','synthetic','synthetic')""",
                        (OWNER, item["id"]))
        return await original_batch(statements)
    raw.batch = racing_batch
    before = contents(app)
    assert remove(app, item, headers=cookie("admin" if race == "membership" else "owner"))[0] == {
        "revision": 412, "membership": 404, "reference": 204}[race]
    assert raced
    after = contents(app)
    if race != "reference":
        assert after["ledger_plan_usage"] == before["ledger_plan_usage"]
        assert row(app, item["id"])["deleted_at"] is None
    else:
        assert any(value[0] == "concurrent-history" for value in after["expenses"])
        assert row(app, item["id"])["deleted_at"] is not None
    assert after["d1_command_guard"] == []


def test_unknown_native_transport_failure_propagates_without_retry(app):
    item = category(app)
    raw, attempts = app.binding.binding, 0
    original_batch = raw.batch
    async def failing_batch(statements):
        nonlocal attempts
        statements = list(statements)
        if any(statement.sql.startswith("UPDATE expense_categories SET deleted_at") for statement in statements):
            attempts += 1
            raise RuntimeError("synthetic unknown transport outcome")
        return await original_batch(statements)
    raw.batch = failing_batch
    with pytest.raises(RuntimeError, match="unknown transport outcome"):
        remove(app, item)
    assert attempts == 1


def test_tombstone_update_keeps_existing_unique_scalar_sql_admission():
    assert sql_write_bound("""UPDATE expense_categories SET deleted_at=?,
        archived_at=COALESCE(archived_at,?),revision=revision+1,updated_at=?
        WHERE id=? AND owner_id=? AND revision=? AND deleted_at IS NULL""") == 7
