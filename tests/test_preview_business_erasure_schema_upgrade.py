"""Preserving v10-to-v11 DDL; native dispatch and cost proof are separate."""
import asyncio
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from pullwise_server.cloudflare_preview_schema import (
    V11_INDEX_COUNTS as INDEX_COUNTS, V11_MIGRATIONS as MIGRATIONS, V11_PRIMARY_KEYS as PRIMARY_KEYS,
    V11_SCHEMA_FINGERPRINT as SCHEMA_FINGERPRINT, V11_SCHEMA_OBJECTS as SCHEMA_OBJECTS,
    V11_SCHEMA_SQL as SCHEMA_SQL, V11_SCHEMA_VERSION as SCHEMA_VERSION, UPGRADE_V11_SQL, V10_INDEX_COUNTS,
    V10_SCHEMA_FINGERPRINT, V10_SCHEMA_OBJECTS, V10_SCHEMA_SQL, V10_SCHEMA_VERSION,
)
from pullwise_server.cloudflare_preview_budget import (
    ProductMeteredD1, STATE_RECORD_INTEGRITY_VERSION, _RECORD_COUNT_SQL,
    _STRICT_RECORD_SQL, _V10_COUNT_SQL, _upgrade_v11_plan,
    begin_product_schema_upgrade_v11, migrate_product_state_records,
    upgrade_product_schema, upgrade_product_schema_v6, upgrade_product_schema_v7,
    upgrade_product_schema_v8, upgrade_product_schema_v9, upgrade_product_schema_v10,
    upgrade_product_schema_v11,
)
from pullwise_server.cloudflare_validation_budget import BudgetError, BudgetJournal
from test_d1_validation_budget import LocalSql
from test_preview_schema_upgrade import SQLiteD1


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-10-09T12:34:56Z"


def objects(db):
    return tuple((kind, name, table, " ".join(sql.split()) if sql else None)
                 for kind, name, table, sql in db.execute(
                     "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"))


def insert(db, table, **values):
    db.execute("INSERT INTO " + table + "(" + ",".join(values) + ") VALUES(" +
               ",".join("?" for _ in values) + ")", tuple(values.values()))


def seed(db):
    for owner in ("owner", "other"):
        insert(db, "expense_categories", id="cat_" + owner, owner_id=owner,
               name="Hosting " + owner, color="blue", created_at=NOW, updated_at=NOW)
        insert(db, "ledger_projects", id="prj_" + owner, owner_id=owner,
               name="Standalone " + owner, created_at=NOW, updated_at=NOW)
        insert(db, "expense_recurring_rules", id="rule_" + owner, owner_id=owner,
               actor_user_id=owner, target_kind="project", project_id="prj_" + owner,
               template_json='{"purpose":"周期🙂","categoryId":"cat_' + owner + '"}',
               schedule_json='{"frequency":"monthly","timezone":"Asia/Shanghai","day":31}',
               status="canceled", revision=7, created_at=NOW, updated_at=NOW,
               create_key="rule-create-" + owner, create_sha256="a" * 64,
               create_response_json='{"revision":7}')
        insert(db, "expenses", id="exp_" + owner, owner_id=owner, target_kind="project",
               project_id="prj_" + owner, category_id="cat_" + owner,
               occurred_on="2026-10-09", amount_minor=1234, currency="USD",
               purpose="History 🙂 " + owner, note="Immutable financial note",
               revision=3, deleted_at=NOW, created_at=NOW, updated_at=NOW)
        insert(db, "expense_recurring_occurrences", rule_id="rule_" + owner,
               owner_id=owner, period_key="2026-10", scheduled_on="2026-10-09",
               expense_id="exp_" + owner, rule_revision=6, created_at=NOW)
        insert(db, "expense_events", id="event_" + owner, expense_id="exp_" + owner,
               owner_id=owner, actor_kind="schedule", actor_id="rule_" + owner,
               action="create", after_json='{"purpose":"History 🙂"}', created_at=NOW)
        insert(db, "expense_create_idempotency", owner_id=owner, idempotency_key="key-" + owner,
               request_sha256="b" * 64, expense_id="exp_" + owner,
               response_json='{"id":"exp_' + owner + '","note":"Replay 🙂"}', created_at=NOW)
        insert(db, "expense_suggestion_events", id="sg_" + owner, owner_id=owner,
               created_at=NOW, question_version="review-1", draft_target_kind="project",
               draft_project_id="prj_" + owner, outcome="available",
               category_probabilities_json='{"cat_' + owner + '":0.9}')
        insert(db, "expense_suggestion_budget", owner_id=owner, day="2026-10-09", attempts=4)
        insert(db, "ledger_activity_events", id="activity_" + owner, operation_id="op-" + owner,
               owner_id=owner, target_kind="project", project_id="prj_" + owner,
               actor_json='{"kind":"session","userId":"' + owner + '"}',
               resource_kind="expense", resource_id="exp_" + owner, action="update",
               before_json='{"note":"Before"}', after_json='{"note":"After 🙂"}', created_at=NOW)
        insert(db, "ledger_plan_usage", owner_id=owner, projects=1, records=1,
               month="2026-10", writes=9, minute=1, minute_writes=2,
               jev_reserved_microusd=11012, project_cap=100, record_cap=20000,
               minute_cap=60, month_cap=10000, jev_cap=5000000,
               project_delta=0, record_delta=0, jev_delta=0,
               previous_month="2026-10", previous_minute=1)
        insert(db, "account_entitlement_authority", owner_id=owner, revision=8,
               plan="max", period="month", period_start=1, valid_until=999999, dirty=0)
        insert(db, "app_state", name="record:users:" + owner,
               payload='{"id":"' + owner + '","jevEnabled":false,"jevPreferenceRevision":8}', updated_at=1)
    insert(db, "api_keys", id="key_owner", user_id="owner", name="Retained key",
           key_prefix="test", key_hash="c" * 64, created_at=1)
    insert(db, "billing_webhook_receipts", event_id="retained-payment", raw_sha256="d" * 64,
           update_json='{"plan":"max"}', received_at=1, state="applied")
    db.commit()


@pytest.fixture
def v10():
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("PRAGMA foreign_keys=ON")
        for sql in V10_SCHEMA_SQL:
            db.execute(sql)
        seed(db)
        assert not list(db.execute("PRAGMA foreign_key_check"))
        yield db


@pytest.fixture
def v11(v10):
    with v10:
        for sql in UPGRADE_V11_SQL:
            v10.execute(sql)
    return v10


def facts(db, columns):
    return {table: sorted((tuple(row) for row in db.execute(
        "SELECT " + ",".join(names) + " FROM " + table)), key=repr)
            for table, names in columns.items()}


@pytest.fixture
def upgrade_journal(v10):
    v10.row_factory = sqlite3.Row
    with closing(sqlite3.connect(":memory:")) as storage:
        journal = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
        saved = journal.snapshot()
        old_markers = {"schema_upgrade": {"from": 4, "to": 5, "request": 41, "complete": True}}
        for version in range(6, 11):
            old_markers["schema_upgrade_v" + str(version)] = {
                "from": version - 1, "to": version, "request": 40 + version, "complete": True,
                "write_execution": {"native_attempts": [None], "provenance": "retained-old-proof"}}
        saved.update(schema_ready=True, schema_version=10, schema_fingerprint=V10_SCHEMA_FINGERPRINT,
            product_data={"rows": dict(v10.execute(_V10_COUNT_SQL).fetchone()), "json": {}, "arrays": 0,
                          "records": dict(v10.execute(_RECORD_COUNT_SQL).fetchone())},
            product_data_verified=True, state_storage_version=1,
            state_record_migration={"version": 1, "request": 50, "complete": True, "copied_records": 2},
            state_record_integrity_version=STATE_RECORD_INTEGRITY_VERSION, state_record_integrity_request=71,
            requests=89, cases={"product-schema": 1, "product": 88,
                "product-state-record-v1": 1, **{"product-schema-v" + str(n - 1) + "-to-v" + str(n): 1
                                                for n in range(5, 11)}},
            reserved_read=120000, actual_read=500, reserved_written=2000, actual_written=300,
            evidence=[{"request": 1, "operation": 1, "rows_read": 500, "rows_written": 300}], **old_markers)
        journal._save(saved)
        yield v10, storage, journal


def run_upgrade(raw, journal):
    asyncio.run(upgrade_product_schema_v11(raw, journal, clock=lambda: 12))


async def completed_paths(raw, journal):
    for migration in (upgrade_product_schema, upgrade_product_schema_v6,
                      upgrade_product_schema_v7, upgrade_product_schema_v8,
                      upgrade_product_schema_v9, upgrade_product_schema_v10,
                      migrate_product_state_records):
        await migration(raw, journal, clock=lambda: 12)


def test_metered_upgrade_retains_business_identity_markers_counters_and_restarts_once(upgrade_journal):
    db, storage, journal = upgrade_journal
    columns = {table: [row[1] for row in db.execute("PRAGMA table_info(" + table + ")")]
               for table in V10_INDEX_COUNTS}
    original, before = facts(db, columns), journal.snapshot()
    raw = SQLiteD1(db, journal, attempts=None)
    asyncio.run(completed_paths(raw, journal))
    assert raw.calls == 0 and journal.snapshot() == before
    plan = _upgrade_v11_plan(before)
    run_upgrade(raw, journal)
    after = journal.snapshot()
    assert after["schema_version"] == 11 and after["schema_fingerprint"] == SCHEMA_FINGERPRINT
    assert objects(db) == SCHEMA_OBJECTS and facts(db, columns) == original
    assert after["product_data"]["rows"] == before["product_data"]["rows"]
    assert after["product_data"]["records"] == before["product_data"]["records"]
    for key in ("scope", "schema_upgrade", "schema_upgrade_v6", "schema_upgrade_v7",
                "schema_upgrade_v8", "schema_upgrade_v9", "schema_upgrade_v10",
                "state_record_migration", "state_storage_version",
                "state_record_integrity_version", "state_record_integrity_request"):
        assert after[key] == before[key]
    assert after["requests"] == before["requests"] + 1
    assert after["cases"] == {**before["cases"], "product": before["cases"]["product"] + 1,
                              "product-schema-v10-to-v11": 1}
    assert after["reserved_written"] == before["reserved_written"] + plan.rows_written
    assert after["reserved_read"] == before["reserved_read"] + plan.rows_read
    assert after["evidence"][:len(before["evidence"])] == before["evidence"]
    assert after["schema_upgrade_v11"]["complete"] is True
    assert after["schema_upgrade_v11"]["write_execution"] == {
        "native_attempts": [None] * 7, "provenance": "d1-nonretryable-write-contract-v1"}
    assert after["active"] is None and after["stopped"] is None and raw.calls == 4
    assert len(set(raw.reservations)) == 1
    restarted = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
    asyncio.run(completed_paths(raw, restarted))
    run_upgrade(raw, restarted)
    assert raw.calls == 4 and restarted.snapshot() == after
    ticket = restarted.begin_product(now=13)
    with pytest.raises(BudgetError, match="SCHEMA_UPGRADE_REQUIRED"):
        ProductMeteredD1(raw, restarted, ticket, clock=lambda: 14)
    assert raw.calls == 4
    restarted.finish(ticket, now=15)


@pytest.mark.parametrize("change", ["stopped", "unverified", "fingerprint", "legacy_storage",
    "schema_upgrade", "schema_upgrade_v6", "schema_upgrade_v7", "schema_upgrade_v8",
    "schema_upgrade_v9", "schema_upgrade_v10", "schema_upgrade_v11", "state_record_migration",
    "unknown_table", "nonempty_guard"])
def test_unreviewed_v11_admission_never_dispatches_or_changes_existing_journal(upgrade_journal, change):
    db, _, journal = upgrade_journal
    saved = journal.snapshot()
    if change == "stopped":
        saved["stopped"] = "D1_OUTCOME_UNKNOWN"
    elif change == "unverified":
        saved["product_data_verified"] = False
    elif change == "fingerprint":
        saved["schema_fingerprint"] = "unreviewed"
    elif change == "legacy_storage":
        saved["state_storage_version"] = 0
    elif change == "unknown_table":
        saved["product_data"]["rows"]["unreviewed_table"] = 0
    elif change == "nonempty_guard":
        saved["product_data"]["rows"]["d1_command_guard"] = 1
    else:
        saved[change] = {"complete": False}
    journal._save(saved)
    raw = SQLiteD1(db, journal)
    with pytest.raises(BudgetError):
        run_upgrade(raw, journal)
    assert raw.calls == 0 and journal.snapshot() == saved


@pytest.mark.parametrize("call", [1, 2, 3, 4])
@pytest.mark.parametrize("failure,code", [("fail_call", "D1_OUTCOME_UNKNOWN"),
    ("missing_meta", "METERING_MISSING")])
def test_unknown_or_missing_v11_accounting_retains_reservation_and_never_replays(upgrade_journal, call, failure, code):
    db, storage, journal = upgrade_journal
    before = journal.snapshot()
    raw = SQLiteD1(db, journal, **{failure: call})
    with pytest.raises(BudgetError, match=code):
        run_upgrade(raw, journal)
    stopped = journal.snapshot()
    assert stopped["schema_version"] == 10 and stopped["schema_upgrade_v11"]["complete"] is False
    assert stopped["reserved_written"] == before["reserved_written"] + _upgrade_v11_plan(before).rows_written
    restarted = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
    with pytest.raises(BudgetError, match=code):
        run_upgrade(raw, restarted)
    assert raw.calls == call and restarted.snapshot() == stopped


@pytest.mark.parametrize("call,attempts", [(1, 0), (2, 4), (3, 2), (3, True), (3, "1"), (4, 1.0)])
def test_unproven_v11_attempts_never_publish_new_schema(upgrade_journal, call, attempts):
    db, _, journal = upgrade_journal
    with pytest.raises(BudgetError, match="MIGRATION_ATTEMPTS_UNPROVEN"):
        run_upgrade(SQLiteD1(db, journal, attempts_by_call={call: attempts}), journal)
    assert journal.snapshot()["schema_version"] == 10
    assert journal.snapshot()["schema_upgrade_v11"]["complete"] is False


def test_known_three_attempt_v11_reads_retain_full_reserved_margin(upgrade_journal):
    db, _, journal = upgrade_journal
    before = journal.snapshot()
    run_upgrade(SQLiteD1(db, journal, attempts_by_call={1: 3, 2: 3, 4: 3}), journal)
    after = journal.snapshot()
    assert after["reserved_read"] == before["reserved_read"] + _upgrade_v11_plan(before).rows_read
    assert after.get("read_margin_released", 0) == before.get("read_margin_released", 0)


def test_late_v11_native_failure_rolls_back_all_business_rows_and_keeps_unknown_stop(upgrade_journal):
    db, storage, journal = upgrade_journal
    columns = {table: [row[1] for row in db.execute("PRAGMA table_info(" + table + ")")]
               for table in V10_INDEX_COUNTS}
    original, before = facts(db, columns), journal.snapshot()

    class Failure(SQLiteD1):
        async def batch(self, statements):
            statements = list(statements)
            if tuple(item.sql for item in statements) == UPGRADE_V11_SQL:
                statements.insert(-1, self.prepare("INSERT INTO d1_command_guard(ok) VALUES(0)").bind())
            return await super().batch(statements)

    raw = Failure(db, journal)
    with pytest.raises(BudgetError, match="D1_OUTCOME_UNKNOWN"):
        run_upgrade(raw, journal)
    stopped = journal.snapshot()
    assert raw.calls == 3 and objects(db) == V10_SCHEMA_OBJECTS and facts(db, columns) == original
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert not list(db.execute("PRAGMA foreign_key_check"))
    assert stopped["reserved_written"] == before["reserved_written"] + _upgrade_v11_plan(before).rows_written
    restarted = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
    with pytest.raises(BudgetError, match="D1_OUTCOME_UNKNOWN"):
        run_upgrade(raw, restarted)
    assert raw.calls == 3 and restarted.snapshot() == stopped


def test_v11_compiled_plan_is_finite_linear_closed_and_requires_strict_records(upgrade_journal):
    _, _, journal = upgrade_journal
    saved = journal.snapshot()
    rows, plan = saved["product_data"]["rows"], _upgrade_v11_plan(saved)
    assert len(plan.operations) == 4 and sum(len(op.sql) for op in plan.operations) == 17
    assert plan.operations[2].sql == UPGRADE_V11_SQL
    assert _STRICT_RECORD_SQL in plan.operations[1].sql and _STRICT_RECORD_SQL in plan.operations[3].sql
    assert plan.rows_written == (256 + 7 * rows["expense_categories"] + 5 * rows["expense_suggestion_events"]
                                 + 6 * rows["expense_recurring_occurrences"] + rows["expense_create_idempotency"])
    assert plan.operations[2].rows_read == (384 * 7 + 32 * sum(rows.values()) + 64 * (
        rows["expense_categories"] + rows["expense_suggestion_events"] +
        rows["expense_recurring_occurrences"] + rows["expense_create_idempotency"]))
    high = journal.snapshot()
    high["product_data"]["rows"] = {table: 0 if table == "d1_command_guard" else 1000000 for table in V10_INDEX_COUNTS}
    upper = _upgrade_v11_plan(high)
    assert plan.rows_read < upper.rows_read < 9007199254740991
    assert plan.rows_written < upper.rows_written < 9007199254740991
    with pytest.raises(BudgetError, match="SCHEMA_V11_UPGRADE_UNREVIEWED"):
        begin_product_schema_upgrade_v11(journal, None, now=12)
    assert journal.snapshot() == saved


@pytest.mark.parametrize("corruption,code", [("schema", "PREVIEW_SCHEMA_MISMATCH"),
    ("cardinality", "PREVIEW_DATA_BOUND"), ("typed_record", "STATE_RECORD_INVALID")])
def test_current_v10_preproof_rejects_corrupt_database_before_v11_ddl(upgrade_journal, corruption, code):
    db, _, journal = upgrade_journal
    if corruption == "schema":
        db.execute("CREATE INDEX unreviewed_index ON expenses(purpose)")
    elif corruption == "cardinality":
        insert(db, "expense_suggestion_budget", owner_id="unreviewed", day="2026-10-09", attempts=1)
    else:
        db.execute("UPDATE app_state SET payload='{}' WHERE name='record:users:owner'")
    db.commit()
    raw = SQLiteD1(db, journal)
    with pytest.raises(BudgetError, match=code):
        run_upgrade(raw, journal)
    assert raw.calls <= 2
    assert "deleted_at" not in [row[1] for row in db.execute("PRAGMA table_info(expense_categories)")]
    assert journal.snapshot()["schema_version"] == 10
    assert journal.snapshot()["schema_upgrade_v11"]["complete"] is False


def test_incomplete_v11_marker_on_current_schema_never_silently_noops(upgrade_journal):
    db, _, journal = upgrade_journal
    saved = journal.snapshot()
    saved.update(schema_version=11, schema_fingerprint=SCHEMA_FINGERPRINT,
                 schema_upgrade_v11={"complete": False})
    journal._save(saved)
    raw = SQLiteD1(db, journal)
    with pytest.raises(BudgetError, match="SCHEMA_V11_UPGRADE_UNREVIEWED"):
        run_upgrade(raw, journal)
    assert raw.calls == 0 and journal.snapshot() == saved


def test_upgrade_preserves_every_column_and_all_commercial_security_history(v10):
    columns = {table: [row[1] for row in v10.execute("PRAGMA table_info(" + table + ")")]
               for table in V10_INDEX_COUNTS}
    original = facts(v10, columns)
    assert objects(v10) == V10_SCHEMA_OBJECTS
    with v10:
        for sql in UPGRADE_V11_SQL:
            v10.execute(sql)
    assert facts(v10, columns) == original
    assert objects(v10) == SCHEMA_OBJECTS
    assert v10.execute("SELECT COUNT(*) FROM expense_categories WHERE deleted_at IS NOT NULL").fetchone()[0] == 0
    assert v10.execute("SELECT COUNT(*) FROM expense_suggestion_events WHERE recorded_expense_id IS NOT NULL").fetchone()[0] == 0
    assert v10.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert not list(v10.execute("PRAGMA foreign_key_check"))
    assert v10.execute("SELECT COUNT(*) FROM d1_command_guard").fetchone()[0] == 0


def test_late_atomic_failure_rolls_back_child_replacement_and_additive_changes(v10):
    columns = {table: [row[1] for row in v10.execute("PRAGMA table_info(" + table + ")")]
               for table in V10_INDEX_COUNTS}
    original = facts(v10, columns)
    with pytest.raises(sqlite3.IntegrityError):
        with v10:
            # A late failure after the old child has been dropped must retain it.
            # SQLite is a local transaction proof, not native dispatch acceptance.
            v10.execute("BEGIN")
            for sql in UPGRADE_V11_SQL[:-1]:
                v10.execute(sql)
            v10.execute("INSERT INTO d1_command_guard(ok) VALUES(0)")
    assert objects(v10) == V10_SCHEMA_OBJECTS
    assert facts(v10, columns) == original
    assert not list(v10.execute("PRAGMA foreign_key_check"))


@pytest.mark.parametrize("timestamp", ["", "2026-10-09", "2026-10-09T12:34:56+00:00",
    "2026/10/09T12:34:56Z", "2026-10-09 12:34:56Z", "2026-10-09T12-34-56Z",
    "2026-10-09T12:34:56z", "2026-10-09T12:34:5🙂", b"2026-10-09T12:34:56Z", 123])
def test_category_tombstone_rejects_noncanonical_timestamps(v11, timestamp):
    with pytest.raises(sqlite3.IntegrityError):
        v11.execute("UPDATE expense_categories SET archived_at=?,deleted_at=? WHERE id='cat_owner'", (NOW, timestamp))
    assert v11.execute("SELECT deleted_at FROM expense_categories WHERE id='cat_owner'").fetchone() == (None,)


def test_category_tombstone_requires_archive_but_preserves_existing_financial_fks(v11):
    with pytest.raises(sqlite3.IntegrityError):
        v11.execute("UPDATE expense_categories SET deleted_at=? WHERE id='cat_owner'", (NOW,))
    v11.execute("UPDATE expense_categories SET archived_at=?,deleted_at=? WHERE id='cat_owner'", (NOW, NOW))
    with pytest.raises(sqlite3.IntegrityError):
        v11.execute("UPDATE expense_categories SET archived_at=NULL WHERE id='cat_owner'")
    with pytest.raises(sqlite3.IntegrityError):
        v11.execute("DELETE FROM expense_categories WHERE id='cat_owner'")
    assert v11.execute("SELECT category_id FROM expenses WHERE id='exp_owner'").fetchone() == ("cat_owner",)
    assert not list(v11.execute("PRAGMA foreign_key_check"))


@pytest.mark.parametrize("identity", ["", "a" * 121, "exp/owner", "exp.owner", "exp owner",
    "exp_é", "exp_🙂", "exp_owner\n", b"exp_owner"])
def test_suggestion_attribution_rejects_noncanonical_ids(v11, identity):
    with pytest.raises(sqlite3.IntegrityError):
        v11.execute("UPDATE expense_suggestion_events SET recorded_expense_id=? WHERE id='sg_owner'", (identity,))


@pytest.mark.parametrize("identity", [None, "a", "exp_owner-123", "A" * 120, "erased-expense-without-fk"])
def test_suggestion_attribution_accepts_nullable_bounded_server_ids_without_new_fk(v11, identity):
    v11.execute("UPDATE expense_suggestion_events SET recorded_expense_id=? WHERE id='sg_owner'", (identity,))
    assert v11.execute("SELECT recorded_expense_id FROM expense_suggestion_events WHERE id='sg_owner'").fetchone() == (identity,)
    assert not list(v11.execute("PRAGMA foreign_key_list(expense_suggestion_events)"))
    assert len(list(v11.execute("PRAGMA index_list(expense_suggestion_events)"))) == 2


def test_nullable_occurrence_keeps_consumed_period_and_rule_scope_fence(v11):
    v11.execute("UPDATE expense_recurring_occurrences SET expense_id=NULL")
    insert(v11, "expense_recurring_occurrences", rule_id="rule_owner", owner_id="owner",
           period_key="2026-11", scheduled_on="2026-11-09", expense_id=None,
           rule_revision=7, created_at=NOW)
    assert v11.execute("SELECT COUNT(*) FROM expense_recurring_occurrences WHERE expense_id IS NULL").fetchone()[0] == 3
    with pytest.raises(sqlite3.IntegrityError):
        insert(v11, "expense_recurring_occurrences", rule_id="rule_owner", owner_id="owner",
               period_key="2026-10", scheduled_on="2026-10-09", expense_id=None,
               rule_revision=7, created_at=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        v11.execute("DELETE FROM expense_recurring_rules WHERE id='rule_owner'")
    with pytest.raises(sqlite3.IntegrityError):
        v11.execute("UPDATE expense_recurring_occurrences SET owner_id='other' WHERE rule_id='rule_owner'")
    assert not list(v11.execute("PRAGMA foreign_key_check"))


def test_nonnull_occurrence_keeps_unique_expense_and_composite_fk(v11):
    with pytest.raises(sqlite3.IntegrityError):
        insert(v11, "expense_recurring_occurrences", rule_id="rule_owner", owner_id="owner",
               period_key="2026-11", scheduled_on="2026-11-09", expense_id="exp_owner",
               rule_revision=7, created_at=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        v11.execute("UPDATE expense_recurring_occurrences SET expense_id='exp_other' WHERE rule_id='rule_owner'")
    with pytest.raises(sqlite3.IntegrityError):
        v11.execute("UPDATE expense_recurring_occurrences SET expense_id='missing' WHERE rule_id='rule_owner'")
    assert v11.execute("SELECT expense_id FROM expense_recurring_occurrences WHERE rule_id='rule_owner'").fetchone() == ("exp_owner",)


def test_erased_generated_expense_retains_external_rules_consumed_period(v11):
    insert(v11, "expenses", id="exp_moved", owner_id="owner", target_kind="shared",
           category_id="cat_owner", occurred_on="2026-11-09", amount_minor=1234,
           currency="USD", purpose="Moved generation", created_at=NOW, updated_at=NOW)
    insert(v11, "expense_recurring_occurrences", rule_id="rule_owner", owner_id="owner",
           period_key="2026-11", scheduled_on="2026-11-09", expense_id="exp_moved",
           rule_revision=7, created_at=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        v11.execute("DELETE FROM expenses WHERE id='exp_moved'")
    v11.execute("UPDATE expense_recurring_occurrences SET expense_id=NULL "
                "WHERE rule_id='rule_owner' AND period_key='2026-11'")
    v11.execute("DELETE FROM expenses WHERE id='exp_moved'")
    assert v11.execute("SELECT rule_id,period_key,expense_id,rule_revision FROM "
                       "expense_recurring_occurrences WHERE period_key='2026-11'").fetchone() == (
                           "rule_owner", "2026-11", None, 7)
    with pytest.raises(sqlite3.IntegrityError):
        insert(v11, "expense_recurring_occurrences", rule_id="rule_owner", owner_id="owner",
               period_key="2026-11", scheduled_on="2026-11-09", expense_id=None,
               rule_revision=8, created_at=NOW)
    assert not list(v11.execute("PRAGMA foreign_key_check"))


@pytest.mark.parametrize("column,value", [("period_key", ""), ("period_key", "a" * 17),
    ("scheduled_on", "2026/10/09"), ("rule_revision", 0), ("rule_revision", 9007199254740992),
    ("created_at", None), ("owner_id", None), ("rule_id", None)])
def test_other_occurrence_constraints_survive_rebuild(v11, column, value):
    with pytest.raises(sqlite3.IntegrityError):
        v11.execute("UPDATE expense_recurring_occurrences SET " + column + "=? WHERE rule_id='rule_owner'", (value,))


def test_replay_index_bounds_incoming_expense_fk_probe_and_preserves_primary_keys(v11):
    assert [row[2] for row in v11.execute("PRAGMA index_info(expense_create_idempotency_owner_expense)")] == ["owner_id", "expense_id"]
    details = [row[3] for row in v11.execute("EXPLAIN QUERY PLAN DELETE FROM expenses WHERE owner_id=? AND id=?", ("owner", "exp_owner"))]
    assert any("expense_create_idempotency_owner_expense (owner_id=? AND expense_id=?)" in detail for detail in details)
    assert any("expense_recurring_occurrences_1 (expense_id=?)" in detail for detail in details)
    assert PRIMARY_KEYS["expense_recurring_occurrences"] == ["rule_id", "period_key"]
    assert INDEX_COUNTS == {**V10_INDEX_COUNTS, "expense_create_idempotency": 2}
    assert INDEX_COUNTS["expense_recurring_occurrences"] == 2


def test_fresh_schema_canonical_migrations_fingerprint_and_frozen_v10_authority_agree():
    migrations = [path for path in sorted((ROOT / "cloudflare/server/migrations").glob("*.sql"))
                  if int(path.name[:4]) <= 11]
    assert migrations[-1].name == "0011_business_erasure.sql"
    assert SCHEMA_VERSION == 11 and V10_SCHEMA_VERSION == 10
    assert V10_SCHEMA_FINGERPRINT == "4bee32e1b140db6ae7f36f24874f33ec059b15ba5707074a950d597405f07f4e"
    assert len(V10_SCHEMA_SQL) == 41 and len(UPGRADE_V11_SQL) == 7 and len(SCHEMA_SQL) == 48 <= 64
    assert sum(V10_INDEX_COUNTS.values()) == 49 and sum(INDEX_COUNTS.values()) == 50
    assert {item["name"]: item["sha256"] for item in MIGRATIONS} == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in migrations}
    with closing(sqlite3.connect(":memory:")) as canonical, closing(sqlite3.connect(":memory:")) as compiled:
        for migration in migrations:
            canonical.executescript(migration.read_text())
        for sql in SCHEMA_SQL:
            compiled.execute(sql)
        assert objects(canonical) == objects(compiled) == SCHEMA_OBJECTS
        assert hashlib.sha256(json.dumps(SCHEMA_OBJECTS, separators=(",", ":")).encode()).hexdigest() == SCHEMA_FINGERPRINT
    old = {(kind, name): sql for kind, name, _, sql in V10_SCHEMA_OBJECTS}
    current = {(kind, name): sql for kind, name, _, sql in SCHEMA_OBJECTS}
    changed = {key for key in old if old[key] != current[key]}
    assert changed == {("table", "expense_categories"), ("table", "expense_suggestion_events"),
                       ("table", "expense_recurring_occurrences")}
    assert current.keys() - old.keys() == {("index", "expense_create_idempotency_owner_expense")}
    for pattern in re.findall(r"\bGLOB\s+'([^']*)'", " ".join(UPGRADE_V11_SQL), re.I):
        assert len(pattern.encode()) <= 50
