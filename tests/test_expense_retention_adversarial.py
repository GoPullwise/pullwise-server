"""Capacity replacement cannot retire money outside current, atomic authority."""
import asyncio
import json

import pytest

from test_ledger_recurring import NOW, NoGitHub, api_auth, app, draft
from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1
from pullwise_server.cloudflare_state_records import encode_record, record_name


FINANCIAL = (
    "expenses", "expense_events", "expense_create_idempotency",
    "expense_recurring_occurrences", "ledger_activity_events", "ledger_plan_usage",
    "d1_command_guard",
)


async def request_async(app, path="/api/v1/expenses", *, method="POST", body=None,
                        key="new-expense", revision=None, headers=None, actor="owner"):
    auth = {"Cookie": "pw_session=" + actor, "Origin": "https://app.example.test",
            "Idempotency-Key": key}
    if actor != "owner":
        auth["X-Pullwise-Workspace"] = "owner"
    if revision is not None:
        auth["If-Match"] = f'"{revision}"'
    auth.update(headers or {})
    return await handle_ledger_request(binding=PlanLimitedD1(app.raw, policy=app.policy, now=NOW),
        gateway=NoGitHub(), method=method, path=path, headers=auth, params={}, body=body, now=NOW)


def request(app, *args, **kwargs):
    return asyncio.run(request_async(app, *args, **kwargs))


def expense(*, date="2026-09-01", project=False, **changes):
    return {"target": {"kind": "project", "projectId": "prj_1"} if project else {"kind": "shared"},
            "occurredOn": date, "amount": "1.00", "currency": "USD", "categoryId": "cat_1",
            "purpose": "Synthetic retention fixture", **changes}


def state(app):
    return {table: app.rows(table) for table in FINANCIAL}


def patch_owner(app, **changes):
    with app.store._immediate() as db:
        key = record_name("users", "owner")
        before = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?", (key,)).fetchone()[0])
        after = {**before, **changes}
        db.execute("UPDATE app_state SET payload=? WHERE name=?", (encode_record("users", "owner", after), key))


def full(app, *, first_project=False):
    app.policy["pro"]["records"] = 2
    oldest = request(app, body=expense(date="2026-08-01", project=first_project), key="oldest")
    second = request(app, body=expense(date="2026-09-01", project=True), key="second")
    assert oldest[0] == second[0] == 201
    result = request(app, "/api/v1/account/expense-retention", method="PATCH",
        body={"autoRemoveOldestExpense": True}, revision=1)
    assert result[0] == 200, result
    return oldest[1], second[1]


def intercept_expense_batch(app, hook):
    original = app.raw.batch

    async def intercepted(statements):
        if any(statement.sql.lstrip().startswith("INSERT INTO expenses") for statement in statements):
            app.raw.batch = original
            await hook()
        return await original(statements)

    app.raw.batch = intercepted


def test_global_oldest_outside_key_grant_does_not_skip_to_a_later_allowed_expense(app):
    full(app)
    auth = api_auth(app, actor="editor", restrictions={"shared": False, "projectIds": ["prj_1"]})
    before = state(app)
    result = request(app, body=expense(project=True), headers=auth)
    assert result == (403, {"error": {"code": "RETENTION_TARGET_FORBIDDEN"}})
    assert state(app) == before


def test_replays_invalid_categories_and_failed_automatic_category_never_retire_expenses(app):
    oldest, _ = full(app)
    before = state(app)
    assert request(app, body=expense(date="2026-08-01"), key="oldest") == (201, oldest)
    assert request(app, body=expense(categoryId="cat_missing"), key="bad-category")[0] == 422
    missing_category = expense()
    del missing_category["categoryId"]
    assert request(app, body=missing_category, key="no-category")[0] == 422
    assert state(app) == before
    status, saved = request(app, body=expense(date="2026-10-01"))
    assert status == 201 and saved["id"] != oldest["id"]
    after = state(app)
    assert request(app, body=expense(date="2026-08-01"), key="oldest") == (201, oldest)
    assert state(app) == after
    retired = [row for row in app.rows("expenses") if row["id"] == oldest["id"]][0]
    assert retired["deleted_at"] is not None


@pytest.mark.parametrize("same_key", [False, True])
def test_concurrent_creates_retire_once_and_same_idempotency_returns_the_winning_response(app, same_key):
    full(app)
    body = expense(date="2026-10-01")
    winner, after_winner = [], []

    async def interleave():
        winner.append(await request_async(app, body=body, key="new-expense" if same_key else "other-create"))
        after_winner.append(state(app))

    intercept_expense_batch(app, interleave)
    result = request(app, body=body)
    assert winner[0][0] == 201
    if same_key:
        assert result == winner[0]
    else:
        assert result[0] == 409
    assert state(app) == after_winner[0]
    assert len([row for row in app.rows("expenses") if row["deleted_at"] is not None]) == 1
    assert len([row for row in app.rows("expenses") if row["deleted_at"] is None]) == 2


def test_concurrent_manual_delete_prevents_a_second_retirement_and_retry_uses_free_capacity(app):
    oldest, _ = full(app)
    after_delete = []

    async def interleave():
        result = await request_async(app, "/api/v1/expenses/" + oldest["id"], method="DELETE", revision=1)
        assert result == (204, None)
        after_delete.append(state(app))

    intercept_expense_batch(app, interleave)
    assert request(app, body=expense(date="2026-10-01"))[0] == 409
    assert state(app) == after_delete[0]
    assert request(app, body=expense(date="2026-10-01"))[0] == 201
    assert len([row for row in app.rows("expenses") if row["deleted_at"] is not None]) == 1


def test_concurrent_backdated_create_changes_global_oldest_even_when_count_and_old_victim_revision_match(app):
    oldest, second = full(app)
    after_interleave = []

    async def interleave():
        assert await request_async(app, "/api/v1/expenses/" + second["id"], method="DELETE", revision=1) == (204, None)
        assert (await request_async(app, body=expense(date="2026-07-01"), key="backdated"))[0] == 201
        after_interleave.append(state(app))

    intercept_expense_batch(app, interleave)
    assert request(app, body=expense(date="2026-10-01"))[0] == 409
    assert state(app) == after_interleave[0]
    original = [row for row in app.rows("expenses") if row["id"] == oldest["id"]][0]
    assert original["deleted_at"] is None and original["revision"] == 1


@pytest.mark.parametrize("change", [
    {"autoRemoveOldestExpense": False, "expenseRetentionRevision": 3},
    {"billing": {"plan": "free", "status": "active"}},
])
def test_owner_preference_or_plan_change_at_commit_rolls_back_retirement_and_new_financial_state(app, change):
    full(app)
    before = state(app)

    async def interleave():
        patch_owner(app, **change)

    intercept_expense_batch(app, interleave)
    assert request(app, body=expense(date="2026-10-01"))[0] == 409
    assert state(app) == before


@pytest.mark.parametrize("mutation", [
    "UPDATE api_keys SET revoked_at=1",
    "UPDATE api_keys SET expires_at=0",
    "UPDATE api_keys SET scopes='[]'",
    "UPDATE api_keys SET restrictions='{\"shared\":false,\"projectIds\":[]}'",
])
def test_key_change_at_commit_cannot_retire_the_oldest_record(app, mutation):
    full(app)
    auth = api_auth(app)
    before = state(app)

    async def interleave():
        with app.store._immediate() as db:
            db.execute(mutation)

    intercept_expense_batch(app, interleave)
    assert request(app, body=expense(date="2026-10-01"), headers=auth)[0] in {401, 403, 409}
    assert state(app) == before


@pytest.mark.parametrize("credential", ["session", "key"])
def test_current_member_demotion_at_commit_rolls_back_every_financial_side_effect(app, credential):
    full(app)
    auth = api_auth(app, actor="editor") if credential == "key" else None
    before = state(app)

    async def interleave():
        with app.store._immediate() as db:
            db.execute("UPDATE workspace_members SET role='viewer',revision=revision+1 WHERE user_id='editor'")

    intercept_expense_batch(app, interleave)
    assert request(app, body=expense(date="2026-10-01"), actor="editor", headers=auth)[0] in {401, 403, 409}
    assert state(app) == before


@pytest.mark.parametrize("quota,column", [("writesPerMinute", "minute_writes"), ("writesPerMonth", "writes")])
def test_exhausted_commercial_write_allowance_never_retires_a_record(app, quota, column):
    full(app)
    app.policy["pro"][quota] = app.rows("ledger_plan_usage")[0][column]
    before = state(app)
    assert request(app, body=expense(date="2026-10-01"))[0] == 429
    assert state(app) == before


def test_downgrade_above_capacity_requires_manual_cleanup_without_bulk_retirement(app):
    oldest, second = full(app)
    app.policy["pro"]["records"] = 1
    before = state(app)
    result = request(app, body=expense(date="2026-10-01"))
    assert result[1]["error"]["code"] == "RETENTION_CLEANUP_REQUIRED"
    assert state(app) == before
    assert request(app, "/api/v1/expenses/" + oldest["id"], method="DELETE", revision=1) == (204, None)
    assert request(app, body=expense(date="2026-10-01"))[0] == 201
    retired = {row["id"] for row in app.rows("expenses") if row["deleted_at"] is not None}
    assert retired == {oldest["id"], second["id"]}


@pytest.mark.parametrize("mutation", ["UPDATE api_keys SET revoked_at=1", "UPDATE api_keys SET expires_at=0"])
def test_expired_or_revoked_original_recurring_key_cannot_retire_expenses_at_capacity(app, mutation):
    full(app)
    auth = api_auth(app)
    assert app.call("POST", body=draft(start="2026-09-01"), headers=auth)[0] == 201
    before = state(app)
    with app.store._immediate() as db:
        db.execute(mutation)
    result = app.tick()
    assert result["created"] == 0 and result["blocked"] == 1
    assert state(app) == before
    assert app.rows("expense_recurring_rules")[0]["blocked_code"] == "SCHEDULE_AUTHORIZATION_CHANGED"


def test_recurring_key_cannot_replace_an_oldest_expense_outside_its_current_target_grant(app):
    full(app)
    auth = api_auth(app, actor="editor", restrictions={"shared": False, "projectIds": ["prj_1"]})
    assert app.call("POST", body=draft(project=True, start="2026-09-01"), actor="editor", headers=auth)[0] == 201
    before = state(app)
    result = app.tick()
    assert result["created"] == 0 and result["blocked"] == 1
    assert state(app) == before
    assert app.rows("expense_recurring_rules")[0]["blocked_code"] == "RETENTION_TARGET_FORBIDDEN"


@pytest.mark.parametrize("mutation", ["UPDATE api_keys SET revoked_at=1", "UPDATE api_keys SET expires_at=0"])
def test_recurring_key_revocation_during_commit_rolls_back_prepared_retirement(app, mutation):
    full(app)
    auth = api_auth(app)
    assert app.call("POST", body=draft(start="2026-09-01"), headers=auth)[0] == 201
    before, before_rules = state(app), app.rows("expense_recurring_rules")

    async def interleave():
        with app.store._immediate() as db:
            db.execute(mutation)

    intercept_expense_batch(app, interleave)
    with pytest.raises(Exception, match="CHECK constraint failed"):
        app.tick()
    assert state(app) == before and app.rows("expense_recurring_rules") == before_rules
