"""Owner policy and successful replacements use the real REST/SQLite boundary."""
import asyncio
import json

import pytest

from test_ledger_recurring import NOW, NoGitHub, app, draft
from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_ledger_recurring import generate_occurrence
from pullwise_server.cloudflare_ledger_profile import read_ledger_me
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1
from pullwise_server.cloudflare_state_records import MAX_SAFE_INTEGER, encode_record, record_name


def request(app, path="/api/v1/expenses", *, method="POST", body=None,
            key="new-expense", revision=None, actor="owner", headers=None, params=None):
    auth = {"Cookie": "pw_session=" + actor, "Origin": "https://app.example.test",
            "Idempotency-Key": key}
    if actor != "owner":
        auth["X-Pullwise-Workspace"] = "owner"
    if revision is not None:
        auth["If-Match"] = f'"{revision}"'
    auth.update(headers or {})
    return asyncio.run(handle_ledger_request(
        binding=PlanLimitedD1(app.raw, policy=app.policy, now=NOW), gateway=NoGitHub(),
        method=method, path=path, headers=auth, params=params or {}, body=body, now=NOW))


def preference(app, *, enabled=None, revision=None, actor="owner", headers=None):
    return request(app, "/api/v1/account/expense-retention", actor=actor, headers=headers,
        method="GET" if enabled is None else "PATCH",
        body=None if enabled is None else {"autoRemoveOldestExpense": enabled}, revision=revision)


def user_record(app, identifier="owner"):
    return next(row for row in app.rows("app_state") if row["name"] == record_name("users", identifier))


def patch_user(app, identifier="owner", **changes):
    row = user_record(app, identifier)
    with app.store._immediate() as db:
        db.execute("UPDATE app_state SET payload=? WHERE name=?", (
            encode_record("users", identifier, {**json.loads(row["payload"]), **changes}), row["name"]))


def expense(*, date="2026-09-01", project=False, **changes):
    return {"target": {"kind": "project", "projectId": "prj_1"} if project else {"kind": "shared"},
        "occurredOn": date, "amount": "1.00", "currency": "USD", "categoryId": "cat_1",
        "purpose": "Synthetic local retention fixture", **changes}


def create(app, body=None, **kwargs):
    result = request(app, body=body or expense(), **kwargs)
    assert result[0] == 201, result
    return result[1]


@pytest.mark.parametrize("plan", ["free", "pro", "max"])
def test_all_plans_read_default_off_without_writes_and_toggle_with_raw_cas(app, plan):
    patch_user(app, billing={"plan": plan, "status": "active"},
        jevEnabled=False, jevPreferenceRevision=9, preserved={"nested": [1, False]})
    before = user_record(app)
    assert preference(app) == (200, {"autoRemoveOldestExpense": False, "revision": 1})
    assert user_record(app) == before and app.rows("ledger_plan_usage") == []
    assert preference(app, enabled=True, revision=1) == (
        200, {"autoRemoveOldestExpense": True, "revision": 2})
    assert json.loads(user_record(app)["payload"]) == {
        **json.loads(before["payload"]), "autoRemoveOldestExpense": True, "expenseRetentionRevision": 2}
    after = user_record(app)
    assert preference(app, enabled=True, revision=2)[0] == 200
    assert user_record(app) == after and app.rows("ledger_plan_usage") == []
    assert preference(app, enabled=False, revision=1)[0] == 412
    assert preference(app, enabled=False, revision=2)[1]["revision"] == 3


def test_account_preference_ignores_selected_workspace_and_does_not_change_owner(app):
    owner = user_record(app)
    assert preference(app, actor="viewer", enabled=True, revision=1)[0] == 200
    assert user_record(app) == owner
    assert json.loads(user_record(app, "viewer")["payload"])["autoRemoveOldestExpense"] is True
    assert preference(app, actor="viewer")[1]["autoRemoveOldestExpense"] is True
    assert preference(app)[1]["autoRemoveOldestExpense"] is False


@pytest.mark.parametrize("body", [None, {}, {"autoRemoveOldestExpense": 1},
    {"autoRemoveOldestExpense": "true"}, {"autoRemoveOldestExpense": True, "ownerId": "other"}])
def test_preference_body_is_exact_boolean_without_writes(app, body):
    before = user_record(app)
    assert request(app, "/api/v1/account/expense-retention", method="PATCH", body=body, revision=1)[0] == 422
    assert user_record(app) == before


@pytest.mark.parametrize("revision,status", [(None, 428), (0, 422), (2, 412)])
def test_preference_requires_its_own_current_revision(app, revision, status):
    before = user_record(app)
    assert preference(app, enabled=True, revision=revision)[0] == status
    assert user_record(app) == before


@pytest.mark.parametrize("changes", [{"autoRemoveOldestExpense": "false"},
    {"expenseRetentionRevision": True}, {"expenseRetentionRevision": 0},
    {"expenseRetentionRevision": MAX_SAFE_INTEGER + 1}])
def test_malformed_persisted_preference_fails_closed_without_reset(app, changes):
    patch_user(app, **changes)
    before = user_record(app)
    assert preference(app) == (503, {"error": {"code": "RETENTION_PREFERENCE_UNAVAILABLE"}})
    assert preference(app, enabled=True, revision=1)[0] == 503
    assert request(app, body=expense())[0] == 503
    assert user_record(app) == before and app.rows("expenses") == []


def test_max_revision_allows_noop_but_not_increment_and_rejects_bearer(app):
    patch_user(app, expenseRetentionRevision=MAX_SAFE_INTEGER)
    before = user_record(app)
    assert preference(app, enabled=False, revision=MAX_SAFE_INTEGER)[0] == 200
    assert preference(app, enabled=True, revision=MAX_SAFE_INTEGER)[0] == 409
    assert preference(app, headers={"Authorization": "Bearer synthetic"})[0] == 401
    assert user_record(app) == before


def test_off_blocks_at_capacity_manual_remove_frees_slot_and_replay_keeps_history(app):
    app.policy["pro"]["records"] = 1
    first = create(app, key="first")
    before = app.rows("expense_events")
    assert request(app, body=expense(), key="full") == (403, {"error": {"code": "RECORD_LIMIT"}})
    assert app.rows("expense_events") == before
    assert request(app, "/api/v1/expenses/" + first["id"], method="DELETE", revision=1) == (204, None)
    assert app.rows("ledger_plan_usage")[0]["records"] == 0
    second = create(app, key="second")
    usage = app.rows("ledger_plan_usage")
    assert request(app, body=expense(), key="first") == (201, first)
    assert app.rows("ledger_plan_usage") == usage
    assert len(app.rows("expenses")) == 2 and len(app.rows("expense_create_idempotency")) == 2
    assert request(app, "/api/v1/expenses/" + first["id"], method="GET")[0] == 404
    assert [item["id"] for item in request(app, method="GET")[1]["items"]] == [second["id"]]


@pytest.mark.parametrize("ordering", ["occurred_on", "created_at", "id"])
def test_on_replaces_true_oldest_with_stable_ties_and_one_commercial_write(app, ordering):
    app.policy["pro"]["records"] = 2
    first = create(app, expense(date="2026-08-01"), key="first")
    second = create(app, expense(date="2026-09-01", project=True, currency="EUR"), key="second")
    with app.store._immediate() as db:
        if ordering == "occurred_on":
            db.execute("UPDATE expenses SET created_at='a' WHERE id=?", (second["id"],))
            expected = first["id"]
        elif ordering == "created_at":
            db.execute("UPDATE expenses SET occurred_on='2026-08-01',created_at='a' WHERE id=?", (second["id"],))
            db.execute("UPDATE expenses SET created_at='b' WHERE id=?", (first["id"],))
            expected = second["id"]
        else:
            db.execute("UPDATE expenses SET occurred_on='2026-08-01',created_at='a'")
            expected = min(first["id"], second["id"])
    assert preference(app, enabled=True, revision=1)[0] == 200
    old_writes = app.rows("ledger_plan_usage")[0]["writes"]
    saved = create(app, expense(date="2026-10-01"), actor="editor", key="replacement")
    usage = app.rows("ledger_plan_usage")[0]
    assert (usage["records"], usage["writes"]) == (2, old_writes + 1)
    victim = next(row for row in app.rows("expenses") if row["id"] == expected)
    assert victim["deleted_at"] is not None and victim["revision"] == 2
    deletion = next(row for row in app.rows("expense_events") if row["action"] == "delete")
    assert (deletion["expense_id"], deletion["actor_kind"], deletion["actor_id"]) == (expected, "session", "editor")
    activity = next(row for row in app.rows("ledger_activity_events") if row["action"] == "delete")
    assert json.loads(activity["actor_json"])["userId"] == "editor" and activity["resource_id"] == expected
    assert json.loads(activity["before_json"])["amount"] == "1.00"
    assert "removedExpense" not in saved and "deletedExpense" not in saved
    assert request(app, "/api/v1/expenses/" + expected, method="GET")[0] == 404
    before = app.rows("ledger_plan_usage"), app.rows("expense_events"), app.rows("ledger_activity_events")
    assert request(app, body=expense(date="2026-10-01"), actor="editor", key="replacement") == (201, saved)
    assert (app.rows("ledger_plan_usage"), app.rows("expense_events"), app.rows("ledger_activity_events")) == before


def test_archived_project_consumes_capacity_removed_project_releases_it_and_repairs_legacy_cache(app):
    app.policy["pro"]["records"] = 2
    project_expense = create(app, expense(project=True), key="project")
    shared = create(app, key="shared")
    with app.store._immediate() as db:
        db.execute("UPDATE ledger_projects SET status='archived' WHERE id='prj_1'")
        db.execute("UPDATE ledger_plan_usage SET record_delta=0,records=98765 WHERE owner_id='owner'")
    status, me = asyncio.run(read_ledger_me(binding=PlanLimitedD1(app.raw, policy=app.policy, now=NOW),
        headers={"Cookie": "pw_session=owner"}, now=NOW))
    assert status == 200 and me["ledgerUsage"]["expenseRecords"]["used"] == 2
    assert app.rows("ledger_plan_usage")[0]["records"] == 98765
    assert request(app, body=expense(), key="full")[0] == 403
    assert request(app, "/api/v1/projects/prj_1", method="DELETE", revision=1)[0] == 204
    usage = app.rows("ledger_plan_usage")[0]
    assert (usage["projects"], usage["records"]) == (1, 1)
    assert all(row["id"] != project_expense["id"] for row in app.rows("expenses"))
    assert all(row["expense_id"] != project_expense["id"] for row in app.rows("expense_events"))
    assert all(row["expense_id"] != project_expense["id"] for row in app.rows("expense_create_idempotency"))
    assert request(app, "/api/v1/expenses/" + project_expense["id"], method="GET")[0] == 404
    assert [item["id"] for item in request(app, method="GET")[1]["items"]] == [shared["id"]]
    create(app, key="available")
    assert app.rows("ledger_plan_usage")[0]["records"] == 2


def test_scheduled_replacement_and_removed_occurrence_replay_never_create_again(app):
    app.policy["pro"]["records"] = 1
    old = create(app, expense(date="2026-08-01"), key="old")
    assert preference(app, enabled=True, revision=1)[0] == 200
    status, rule = app.call("POST", body=draft(start="2026-11-01"))
    assert status == 201
    # Reproduce a persisted pre-release due pointer without posting at save.
    with app.store._immediate() as db:
        db.execute("UPDATE expense_recurring_rules SET schedule_json=?,next_occurrence_on='2026-09-30',next_period_key='M2026-09',next_run_at=0 WHERE id=?",
            (json.dumps(draft(start="2026-09-01")["schedule"]), rule["id"]))
    assert status == 201
    pre = app.rows("expense_recurring_rules")[0]
    writes = app.rows("ledger_plan_usage")[0]["writes"]
    assert app.tick()["created"] == 1
    occurrence = app.rows("expense_recurring_occurrences")[0]
    assert app.rows("ledger_plan_usage")[0]["writes"] == writes + 1
    assert app.rows("ledger_plan_usage")[0]["records"] == 1
    deletion = next(row for row in app.rows("expense_events") if row["action"] == "delete")
    assert (deletion["expense_id"], deletion["actor_kind"], deletion["actor_id"]) == (old["id"], "schedule", rule["id"] + ":owner")
    assert request(app, "/api/v1/expenses/" + occurrence["expense_id"], method="DELETE", revision=1)[0] == 204
    with app.store._immediate() as db:
        db.execute("UPDATE expense_recurring_rules SET next_occurrence_on=?,next_period_key=?,next_run_at=0 WHERE id=?",
            (pre["next_occurrence_on"], pre["next_period_key"], rule["id"]))
    current = app.rows("expense_recurring_rules")[0]
    before = app.rows("expenses"), app.rows("expense_events"), app.rows("ledger_plan_usage")
    outcome = asyncio.run(generate_occurrence(binding=PlanLimitedD1(app.raw, policy=app.policy, now=NOW),
        maintenance_binding=app.raw, gateway=NoGitHub(), row=current, now=NOW))
    assert outcome[1] == "replayed"
    assert (app.rows("expenses"), app.rows("expense_events"), app.rows("ledger_plan_usage")) == before
