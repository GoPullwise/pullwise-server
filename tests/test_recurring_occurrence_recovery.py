"""Posted starts, independent periods and bounded recoverable capacity failures."""
from datetime import datetime, timezone

import pytest

from test_ledger_recurring import NOW, api_auth, app, draft, legacy_due


def occupied(app):
    app.policy["pro"]["records"] = 1
    with app.store._immediate() as db:
        db.execute("""INSERT INTO expenses(id,owner_id,target_kind,category_id,occurred_on,
            amount_minor,currency,purpose,created_at,updated_at)
            VALUES('existing','owner','shared','cat_1','2026-01-01',999,'USD','Existing','local','local')""")


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("start", ["2024-01-01", "2026-09-05", "2026-10-08"])
def test_historical_or_today_start_posts_exactly_one_initial_expense_atomically(app, project, start):
    status, rule = app.call("POST", body=draft(project=project, start=start))
    assert status == 201
    assert rule["nextOccurrenceOn"] > "2026-10-08" and not rule["awaitingSync"]
    expenses, events, periods = (app.rows(name) for name in (
        "expenses", "expense_events", "expense_recurring_occurrences"))
    assert len(expenses) == len(events) == len(periods) == 1
    assert expenses[0]["occurred_on"] == start and expenses[0]["amount_minor"] == 1234
    assert expenses[0]["target_kind"] == ("project" if project else "shared")
    assert events[0]["expense_id"] == periods[0]["expense_id"] == expenses[0]["id"]
    assert app.rows("ledger_plan_usage")[0]["records"] == 1
    assert app.rows("ledger_plan_usage")[0]["writes"] == 1
    assert app.call("POST", body=draft(project=project, start=start)) == (201, rule)
    assert app.tick()["created"] == 0 and len(app.rows("expenses")) == 1


def test_future_start_waits_and_each_later_period_inserts_a_distinct_expense(app):
    status, rule = app.call("POST", body=draft(start="2026-10-31"))
    assert status == 201 and app.rows("expenses") == []
    october = int(datetime(2026, 10, 31, 12, tzinfo=timezone.utc).timestamp())
    november = int(datetime(2026, 11, 30, 12, tzinfo=timezone.utc).timestamp())
    assert app.tick(now=october)["created"] == 1
    assert app.tick(now=november)["created"] == 1
    expenses = app.rows("expenses")
    assert len({item["id"] for item in expenses}) == 2
    assert [item["occurred_on"] for item in expenses] == ["2026-10-31", "2026-11-30"]
    assert [item["amount_minor"] for item in expenses] == [1234, 1234]
    assert app.tick(now=november)["created"] == 0


def test_calendar_zone_decides_whether_start_is_posted_today(app):
    # This instant is October 8 in UTC, October 9 in Shanghai.
    now = int(datetime(2026, 10, 8, 17, tzinfo=timezone.utc).timestamp())
    body = draft(start="2026-10-09")
    from pullwise_server.cloudflare_state_records import encode_record, record_name
    with app.store._immediate() as db:
        db.execute("UPDATE app_state SET payload=? WHERE name=?", (
            encode_record("sessions", "owner", {"userId": "owner", "expiresAt": now + 1000}),
            record_name("sessions", "owner")))
    status, rule = app.call("POST", body=body, now=now)
    assert status == 201 and len(app.rows("expenses")) == 1
    assert app.rows("expenses")[0]["occurred_on"] == "2026-10-09"
    assert rule["nextOccurrenceOn"] == "2026-11-30"


def test_capacity_failed_start_keeps_frozen_manual_retry_and_preserves_replay_identity(app):
    occupied(app)
    status, rule = app.call("POST", body=draft(start="2026-09-05"))
    assert status == 201 and rule["status"] == "active"
    assert rule["nextOccurrenceOn"] == "2026-10-31"
    pending = rule["pendingOccurrences"][0]
    assert pending["periodKey"] == "M2026-09" and pending["scheduledOn"] == "2026-09-05"
    assert pending["blockedCode"] == "RECORD_LIMIT"
    assert len(app.rows("expenses")) == 1 and app.rows("expense_recurring_occurrences") == []
    assert app.rows("expense_recurring_pending")[0]["notification_state"] == "inbox"
    status, edited = app.call("PATCH", rule["id"], draft(amount="88.00", purpose="Changed"), revision=1)
    assert status == 200 and edited["pendingOccurrences"] == [pending]
    before = {name: app.rows(name) for name in ("expenses", "expense_recurring_pending", "ledger_plan_usage")}
    assert app.call("PATCH", rule["id"], {"retryPeriodKey": pending["periodKey"]}, revision=2)[1]["error"]["code"] == "RECORD_LIMIT"
    assert {name: app.rows(name) for name in before} == before
    with app.store._immediate() as db:
        db.execute("UPDATE expenses SET deleted_at='deleted' WHERE id='existing'")
    status, recovered = app.call("PATCH", rule["id"], {"retryPeriodKey": pending["periodKey"]}, revision=2)
    assert status == 200 and recovered["pendingOccurrences"] == []
    expense = app.rows("expenses")[-1]
    assert expense["amount_minor"] == 1234 and expense["purpose"] == "Hosting"
    assert expense["occurred_on"] == "2026-09-05"
    assert recovered["nextOccurrenceOn"] == edited["nextOccurrenceOn"]
    assert app.call("PATCH", rule["id"], {"retryPeriodKey": pending["periodKey"]}, revision=2)[0] == 412
    with app.store._immediate() as db:
        db.execute("UPDATE expenses SET deleted_at='deleted'")
    assert app.call("PATCH", rule["id"], {"retryPeriodKey": pending["periodKey"]}, revision=3) == (200, recovered)
    assert len(app.rows("expense_recurring_occurrences")) == 1 and len(app.rows("expenses")) == 2


def test_first_ten_failures_are_kept_and_overflow_does_not_replace_or_block_future_dates(app):
    status, rule = app.call("POST", body=draft())
    assert status == 201
    occupied(app)
    legacy_due(app, start="2025-01-01")
    for _ in range(4):
        result = app.tick()
        assert result["created"] == 0 and result["blocked"] == 3
    retained = app.rows("expense_recurring_pending")
    assert len(retained) == 10
    assert [item["period_key"] for item in retained] == [f"M2025-{month:02d}" for month in range(1, 11)]
    saved = app.call("GET", rule["id"])[1]
    assert saved["status"] == "active" and saved["blockedCode"] is None
    assert saved["awaitingSync"] and saved["nextOccurrenceOn"] == "2026-10-31"
    # November/December 2025 failures overflowed. Removing capacity pressure
    # creates new January 2026 expense instead of replaying discarded failures.
    with app.store._immediate() as db:
        db.execute("UPDATE expenses SET deleted_at='deleted' WHERE id='existing'")
    result = app.tick()
    assert result["created"] == 1 and result["blocked"] == 2
    assert app.rows("expenses")[-1]["occurred_on"] == "2026-01-31"
    assert app.rows("expense_recurring_pending") == retained


def test_retained_failure_can_be_retried_with_a_current_key_and_not_by_viewer(app):
    occupied(app)
    status, rule = app.call("POST", body=draft(start="2026-09-05"))
    assert status == 201
    body = {"retryPeriodKey": "M2026-09"}
    assert app.call("PATCH", rule["id"], body, revision=1, actor="viewer")[0] == 403
    auth = api_auth(app, actor="editor")
    with app.store._immediate() as db:
        db.execute("UPDATE expenses SET deleted_at='deleted'")
    status, saved = app.call("PATCH", rule["id"], body, revision=1, actor="editor", headers=auth)
    assert status == 200 and saved["pendingOccurrences"] == []
    event = app.rows("expense_events")[0]
    assert event["actor_kind"] == "api_key" and event["actor_id"].startswith("editor:sha256:")


def test_retry_current_key_revocation_before_batch_rolls_back_every_financial_change(app):
    occupied(app)
    status, rule = app.call("POST", body=draft(start="2026-09-05"))
    assert status == 201
    auth = api_auth(app)
    with app.store._immediate() as db:
        db.execute("UPDATE expenses SET deleted_at='deleted'")
    before = {name: app.rows(name) for name in (
        "expenses", "expense_events", "expense_recurring_occurrences", "expense_recurring_pending",
        "expense_recurring_rules", "ledger_plan_usage")}
    original_batch = app.raw.batch
    async def raced_batch(statements):
        if any("INSERT INTO expenses(" in statement.sql for statement in statements):
            app.raw.batch = original_batch
            with app.store._immediate() as db:
                db.execute("UPDATE api_keys SET revoked_at=1")
        return await original_batch(statements)
    app.raw.batch = raced_batch
    with pytest.raises(Exception, match="CHECK constraint failed"):
        app.call("PATCH", rule["id"], {"retryPeriodKey": "M2026-09"}, revision=1, headers=auth)
    assert {name: app.rows(name) for name in before} == before


def test_capacity_race_after_initial_admission_retains_plan_without_partial_expense(app):
    app.policy["pro"]["records"] = 1
    original_batch = app.raw.batch
    async def raced_batch(statements):
        # The first batch after expense admission is an activity label read;
        # filling capacity here makes PlanLimitedD1 reject the financial batch.
        if any("SELECT id,name FROM expense_categories" in statement.sql for statement in statements):
            app.raw.batch = original_batch
            occupied(app)
        return await original_batch(statements)
    app.raw.batch = raced_batch
    status, rule = app.call("POST", body=draft(start="2026-09-05"))
    assert status == 201 and rule["pendingOccurrences"][0]["blockedCode"] == "RECORD_LIMIT"
    assert len(app.rows("expenses")) == 1
    assert app.rows("expense_events") == app.rows("expense_recurring_occurrences") == []
    assert len(app.rows("expense_recurring_rules")) == len(app.rows("expense_recurring_pending")) == 1
    assert app.call("POST", body=draft(start="2026-09-05")) == (201, rule)


@pytest.mark.parametrize("code", ["RECORD_LIMIT", "RETENTION_CLEANUP_REQUIRED", "RETENTION_TARGET_FORBIDDEN", "CATCHUP_REVIEW_REQUIRED"])
def test_existing_capacity_or_catchup_block_is_recovered_and_retained_without_get_writes(app, code):
    status, rule = app.call("POST", body=draft())
    assert status == 201
    occupied(app)
    legacy_due(app, start="2026-07-01")
    with app.store._immediate() as db:
        db.execute("UPDATE expense_recurring_rules SET status='blocked',blocked_code=?", (code,))
    original = app.rows("expense_recurring_rules")
    stored = app.call("GET", rule["id"])[1]
    assert stored["nextOccurrenceOn"] == "2026-10-31" and app.rows("expense_recurring_rules") == original
    assert app.tick() == {"scanned": 1, "created": 0, "blocked": 3, "replayed": 0}
    saved = app.call("GET", rule["id"])[1]
    assert saved["status"] == "active" and saved["blockedCode"] is None
    assert [item["periodKey"] for item in saved["pendingOccurrences"]] == ["M2026-07", "M2026-08", "M2026-09"]
    assert saved["nextOccurrenceOn"] == "2026-10-31" and not saved["awaitingSync"]
    assert len(app.rows("expenses")) == 1 and app.rows("expense_recurring_occurrences") == []


def test_legacy_capacity_recovery_rechecks_revoked_original_key_and_does_not_resume_other_blocks(app):
    auth = api_auth(app)
    status, rule = app.call("POST", body=draft(), headers=auth)
    assert status == 201
    legacy_due(app)
    with app.store._immediate() as db:
        db.execute("UPDATE expense_recurring_rules SET status='blocked',blocked_code='RECORD_LIMIT'")
        db.execute("UPDATE api_keys SET revoked_at=1")
    assert app.tick() == {"scanned": 1, "created": 0, "blocked": 1, "replayed": 0}
    assert app.rows("expense_recurring_rules")[0]["blocked_code"] == "SCHEDULE_AUTHORIZATION_CHANGED"
    assert app.rows("expenses") == app.rows("expense_recurring_pending") == []
    assert app.tick()["scanned"] == 0
    with app.store._immediate() as db:
        db.execute("UPDATE expense_recurring_rules SET blocked_code='INVALID_CATEGORY'")
    assert app.tick()["scanned"] == 0
    with app.store._immediate() as db:
        db.execute("UPDATE expense_recurring_rules SET status='paused',blocked_code='RECORD_LIMIT'")
    assert app.tick()["scanned"] == 0


def test_future_exact_start_date_does_not_present_second_charge_in_its_calendar_period(app):
    body = draft(start="2026-10-10")
    assert app.call("POST", body=body)[0] == 201
    october = int(datetime(2026, 10, 10, 12, tzinfo=timezone.utc).timestamp())
    assert app.tick(now=october)["created"] == 1
    row = app.rows("expense_recurring_rules")[0]
    assert row["next_occurrence_on"] == "2026-11-30"
    assert app.rows("expenses")[0]["occurred_on"] == "2026-10-10"
