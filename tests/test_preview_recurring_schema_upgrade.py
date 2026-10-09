"""Populated v6-to-v7 integrity checks; native D1 costs are a separate proof."""
import asyncio
import sqlite3

import pytest

from test_d1_validation_budget import LocalSql
from test_preview_blank_schema_upgrade import v5, facts
from test_preview_schema_upgrade import SQLiteD1
from pullwise_server.cloudflare_preview_schema import (
    V7_SCHEMA_VERSION as SCHEMA_VERSION, V7_SCHEMA_FINGERPRINT as SCHEMA_FINGERPRINT,
    V7_SCHEMA_OBJECTS as SCHEMA_OBJECTS, V7_INDEX_COUNTS as INDEX_COUNTS,
    V6_INDEX_COUNTS, UPGRADE_V7_SQL,
)
from pullwise_server.cloudflare_preview_budget import (
    upgrade_product_schema_v6, upgrade_product_schema_v7, _upgrade_v7_plan,
    begin_product_schema_upgrade_v7,
)
from pullwise_server.cloudflare_validation_budget import BudgetJournal, BudgetError


@pytest.fixture
def v6(v5):
    db, storage, journal = v5
    asyncio.run(upgrade_product_schema_v6(SQLiteD1(db, journal), journal, clock=lambda: 10))
    return db, storage, journal


def run(raw, journal):
    asyncio.run(upgrade_product_schema_v7(raw, journal, clock=lambda: 10))


def old_facts(db):
    values = facts(db)
    values["ledger_projects"] = [row[:11] for row in values["ledger_projects"]]
    return values


def schema(db):
    return tuple((row[0], row[1], row[2], " ".join(row[3].split()) if row[3] else None)
                 for row in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"))


def test_upgrade_preserves_all_history_prior_markers_and_cumulative_accounting(v6):
    db, storage, journal = v6
    before, historical = journal.snapshot(), old_facts(db)
    raw = SQLiteD1(db, journal, attempts=None)
    run(raw, journal)
    after = journal.snapshot()
    assert after["schema_version"] == SCHEMA_VERSION == 7
    assert after["schema_fingerprint"] == SCHEMA_FINGERPRINT
    assert old_facts(db) == historical
    assert db.execute("SELECT development_url,product_url FROM ledger_projects").fetchone()[:] == (None, None)
    for key in ("scope", "schema_upgrade", "schema_upgrade_v6", "state_record_migration", "state_storage_version"):
        assert after[key] == before[key]
    assert after["requests"] == before["requests"] + 1
    assert after["reserved_read"] > before["reserved_read"]
    assert after["reserved_written"] > before["reserved_written"]
    assert after["actual_read"] >= before["actual_read"]
    assert after["actual_written"] > before["actual_written"]
    assert after["evidence"][:len(before["evidence"])] == before["evidence"]
    assert after.get("read_margin_released", 0) == before.get("read_margin_released", 0)
    assert after["cases"]["product-schema-v6-to-v7"] == 1
    assert after["schema_upgrade_v7"]["complete"] is True
    assert after["schema_upgrade_v7"]["write_execution"] == {
        "native_attempts": [None] * len(UPGRADE_V7_SQL),
        "provenance": "d1-nonretryable-write-contract-v1"}
    assert after["product_data_verified"] is True and after["active"] is None and after["stopped"] is None
    assert set(after["product_data"]["rows"]) == set(INDEX_COUNTS)
    assert all(after["product_data"]["rows"][table] == 0 for table in (
        "expense_recurring_rules", "expense_recurring_occurrences"))
    assert schema(db) == SCHEMA_OBJECTS
    assert not list(db.execute("PRAGMA foreign_key_check"))
    assert raw.calls == 4 and len(set(raw.reservations)) == 1
    restarted = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
    run(raw, restarted)
    assert raw.calls == 4 and restarted.snapshot() == after


@pytest.mark.parametrize("call", [1, 2, 3, 4])
def test_missing_native_metadata_stops_and_never_replays_or_refunds(v6, call):
    db, storage, journal = v6
    raw = SQLiteD1(db, journal, missing_meta=call)
    with pytest.raises(BudgetError, match="METERING_MISSING"):
        run(raw, journal)
    stopped = journal.snapshot()
    assert stopped["schema_version"] == 6 and stopped["schema_upgrade_v7"]["complete"] is False
    restarted = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
    with pytest.raises(BudgetError, match="METERING_MISSING"):
        run(raw, restarted)
    assert raw.calls == call and restarted.snapshot() == stopped


@pytest.mark.parametrize("call", [1, 3, 4])
def test_unknown_native_outcome_is_never_retried(v6, call):
    db, storage, journal = v6
    raw = SQLiteD1(db, journal, fail_call=call)
    with pytest.raises(BudgetError, match="D1_OUTCOME_UNKNOWN"):
        run(raw, journal)
    stopped = journal.snapshot()
    restarted = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
    with pytest.raises(BudgetError, match="D1_OUTCOME_UNKNOWN"):
        run(raw, restarted)
    assert raw.calls == call and restarted.snapshot() == stopped


@pytest.mark.parametrize("call,attempts", [(1, 0), (2, 4), (3, 2), (3, True), (3, "1"), (4, 1.0)])
def test_invalid_native_attempts_never_publish_schema_version(v6, call, attempts):
    db, _, journal = v6
    with pytest.raises(BudgetError, match="MIGRATION_ATTEMPTS_UNPROVEN"):
        run(SQLiteD1(db, journal, attempts_by_call={call: attempts}), journal)
    assert journal.snapshot()["schema_version"] == 6
    assert journal.snapshot()["schema_upgrade_v7"]["complete"] is False


def test_three_read_attempts_retain_the_complete_reserved_margin(v6):
    db, _, journal = v6
    before = journal.snapshot()
    run(SQLiteD1(db, journal, attempts_by_call={1: 3, 2: 3, 4: 3}), journal)
    assert journal.snapshot().get("read_margin_released", 0) == before.get("read_margin_released", 0)


def test_failure_after_event_drop_rolls_back_columns_schema_and_history(v6):
    db, _, journal = v6
    historical, prior_schema = old_facts(db), schema(db)

    class Injected(SQLiteD1):
        async def batch(self, statements):
            if any(item.sql == UPGRADE_V7_SQL[4] for item in statements):
                statements[5].sql = "INSERT INTO d1_command_guard(ok) VALUES(0)"
            return await super().batch(statements)

    with pytest.raises(BudgetError, match="D1_OUTCOME_UNKNOWN"):
        run(Injected(db, journal), journal)
    assert old_facts(db) == historical and schema(db) == prior_schema
    assert not list(db.execute("PRAGMA foreign_key_check"))


@pytest.mark.parametrize("change", ["stopped", "schema6_incomplete", "cutover_incomplete",
    "schema7_incomplete", "unverified", "wrong_fingerprint", "unknown_table"])
def test_unsafe_or_unreviewed_prior_state_never_dispatches(v6, change):
    db, _, journal = v6
    state = journal.snapshot()
    if change == "stopped":
        state["stopped"] = "MANUAL_STOP"
    elif change == "schema6_incomplete":
        state["schema_upgrade_v6"]["complete"] = False
    elif change == "cutover_incomplete":
        state["state_record_migration"]["complete"] = False
    elif change == "schema7_incomplete":
        state["schema_upgrade_v7"] = {"from": 6, "to": 7, "complete": False}
    elif change == "unverified":
        state["product_data_verified"] = False
    elif change == "wrong_fingerprint":
        state["schema_fingerprint"] = "unknown"
    else:
        state["product_data"]["rows"]["unreviewed_table"] = 0
    journal._save(state)
    raw = SQLiteD1(db, journal)
    with pytest.raises(BudgetError):
        run(raw, journal)
    assert raw.calls == 0 and journal.snapshot() == state


def test_schema_drift_stops_before_any_mutation(v6):
    db, _, journal = v6
    db.execute("ALTER TABLE expenses ADD COLUMN unreviewed TEXT")
    db.commit()
    raw = SQLiteD1(db, journal)
    with pytest.raises(BudgetError, match="PREVIEW_SCHEMA_MISMATCH"):
        run(raw, journal)
    assert raw.calls == 1
    assert "development_url" not in {row[1] for row in db.execute("PRAGMA table_info(ledger_projects)")}


def test_closed_plan_covers_maximum_cardinality_and_rejects_forged_operations(v6):
    db, _, journal = v6
    state = journal.snapshot()
    state["product_data"]["rows"] = {table: 0 if table == "d1_command_guard" else 1_000_000
                                    for table in V6_INDEX_COUNTS}
    plan = _upgrade_v7_plan(state)
    assert 100_000 < plan.rows_read < 9007199254740991
    assert 1000 < plan.rows_written < 9007199254740991
    assert len(plan.operations[2].sql) == 13
    journal._save(state)
    with pytest.raises(BudgetError, match="SCHEMA_V7_UPGRADE_UNREVIEWED"):
        begin_product_schema_upgrade_v7(journal, None, now=10)
    assert journal.snapshot() == state


def test_new_links_actor_and_recurring_constraints_retain_financial_fks(v6):
    db, _, journal = v6
    run(SQLiteD1(db, journal), journal)
    db.execute("UPDATE ledger_projects SET development_url=?,product_url=? WHERE id='prj_old'",
               ("https://dev.example.test", "https://example.test"))
    db.execute("INSERT INTO expense_events VALUES('evt_schedule','exp_old','owner','schedule','rule',"
               "'create',NULL,'{}','new')")
    db.execute("INSERT INTO expense_recurring_rules(id,owner_id,actor_user_id,target_kind,project_id,"
               "template_json,schedule_json,status,created_at,updated_at,create_key,create_sha256,create_response_json) "
               "VALUES('rule','owner','owner','project','prj_old','{}','{}','active','new','new','key',?,'{}')",
               ("a" * 64,))
    db.execute("INSERT INTO expense_recurring_occurrences VALUES('rule','owner','2026-10','2026-10-08',"
               "'exp_old',1,'new')")
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO expense_recurring_occurrences VALUES('rule','owner','2026-10','2026-10-08',"
                   "'exp_old',1,'new')")
    db.rollback()
    for sql, params in (
        ("UPDATE ledger_projects SET development_url=? WHERE id='prj_old'", ("é" * 1025,)),
        ("UPDATE ledger_projects SET product_url=? WHERE id='prj_old'", ("",)),
        ("UPDATE expense_recurring_rules SET project_id='missing' WHERE id='rule'", ()),
        ("UPDATE expense_recurring_rules SET owner_id='another' WHERE id='rule'", ()),
        ("UPDATE expense_recurring_rules SET template_json='[]' WHERE id='rule'", ()),
        ("UPDATE expense_recurring_rules SET schedule_json=? WHERE id='rule'", ('{"x":"' + "x" * 2048 + '"}',)),
        ("UPDATE expense_recurring_rules SET create_response_json='[]' WHERE id='rule'", ()),
        ("UPDATE expense_recurring_rules SET revision=9007199254740992 WHERE id='rule'", ()),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql, params)
        db.rollback()
    assert db.execute("SELECT COUNT(*) FROM expense_recurring_occurrences").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM expenses WHERE id='exp_old'")
    db.rollback()
    assert not list(db.execute("PRAGMA foreign_key_check"))
