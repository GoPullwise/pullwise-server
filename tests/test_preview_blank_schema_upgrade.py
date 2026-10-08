"""Atomic v6 behavior with real SQLite FKs; native cost evidence is separate."""
import asyncio
import sqlite3

import pytest

from test_d1_validation_budget import LocalSql
from test_preview_schema_upgrade import SQLiteD1
from pullwise_server.cloudflare_preview_schema import (
    V5_SCHEMA_SQL, V5_SCHEMA_FINGERPRINT, V6_SCHEMA_VERSION as SCHEMA_VERSION,
    V6_SCHEMA_FINGERPRINT as SCHEMA_FINGERPRINT, V6_SCHEMA_OBJECTS as SCHEMA_OBJECTS,
    V6_INDEX_COUNTS as INDEX_COUNTS, UPGRADE_V6_SQL,
)
from pullwise_server.cloudflare_preview_budget import (
    upgrade_product_schema_v6, _upgrade_v6_plan, _V6_COUNT_SQL as _COUNT_SQL,
)
from pullwise_server.cloudflare_state_records import record_name, encode_record
from pullwise_server.cloudflare_validation_budget import BudgetJournal, BudgetError


@pytest.fixture
def v5():
    db, storage = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    for sql in V5_SCHEMA_SQL:
        db.execute(sql)
    owner = {"id": "owner", "githubAccessToken": "opaque-encrypted-token",
             "billing": {"plan": "pro"}, "retained": "x" * 9000}
    db.execute("INSERT INTO app_state VALUES(?,?,?)", (record_name("users", "owner"),
        encode_record("users", "owner", owner), 100))
    db.execute("INSERT INTO app_state VALUES(?,?,?)", (record_name("sessions", "opaque-session"),
        encode_record("sessions", "opaque-session", {"userId": "owner", "expiresAt": 1000}), 100))
    db.execute("INSERT INTO ledger_projects VALUES('prj_old','owner',202,'org/private',"
               "'retain','archived',4,'old','old','',NULL)")
    db.execute("INSERT INTO ledger_project_repositories(owner_id,project_id,github_repo_id,"
               "github_full_name,created_at) VALUES('owner','prj_old',202,'org/private','old')")
    db.execute("INSERT INTO expense_categories VALUES('cat_old','owner','Hosting',NULL,NULL,1,'old','old')")
    db.execute("INSERT INTO expenses(id,owner_id,target_kind,project_id,category_id,occurred_on,"
               "amount_minor,currency,purpose,revision,created_at,updated_at,deleted_at) VALUES("
               "'exp_old','owner','project','prj_old','cat_old','2026-10-07',1250,'USD','retain',3,'old','old','old')")
    db.execute("INSERT INTO expense_events VALUES('evt_old','exp_old','owner','session','owner',"
               "'create',NULL,'{}','old')")
    db.execute("INSERT INTO expense_create_idempotency VALUES('owner','old-key','digest','exp_old','{}','old')")
    db.execute("INSERT INTO account_entitlement_authority VALUES('owner',7,'pro','month',1,1000,0)")
    db.execute("INSERT INTO billing_webhook_receipts VALUES('old-event','hash','{}',1,'applied')")
    db.commit()
    journal = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
    state = journal.snapshot()
    state.update(schema_ready=True, schema_version=5, schema_fingerprint=V5_SCHEMA_FINGERPRINT,
        product_data={"rows": dict(db.execute(_COUNT_SQL).fetchone()), "json": {}, "arrays": 0,
                      "records": {"users": 1, "sessions": 1}},
        product_data_verified=True, state_storage_version=1,
        state_record_migration={"version": 1, "request": 50, "complete": True, "copied_records": 2},
        schema_upgrade={"from": 4, "to": 5, "request": 49, "complete": True,
                        "write_execution": {"native_attempts": [None], "provenance": "old-proof"}},
        requests=89, cases={"product-schema": 1, "product-schema-v4-to-v5": 1,
                           "product-state-record-v1": 1, "product": 88},
        reserved_read=120_000, actual_read=500, reserved_written=1500, actual_written=300)
    journal._save(state)
    try:
        yield db, storage, journal
    finally:
        db.close()
        storage.close()


def run(raw, journal):
    asyncio.run(upgrade_product_schema_v6(raw, journal, clock=lambda: 10))


def facts(db):
    return {table: [tuple(row) for row in db.execute("SELECT * FROM " + table)]
            for table in INDEX_COUNTS}


def test_upgrade_preserves_every_fact_prior_marker_and_budget_across_restart(v5):
    db, storage, journal = v5
    before, prior_facts = journal.snapshot(), facts(db)
    raw = SQLiteD1(db, journal, attempts=None)
    run(raw, journal)
    after = journal.snapshot()
    assert after["schema_version"] == SCHEMA_VERSION == 6
    assert after["schema_fingerprint"] == SCHEMA_FINGERPRINT
    assert facts(db) == prior_facts
    assert after["schema_upgrade"] == before["schema_upgrade"]
    assert after["state_record_migration"] == before["state_record_migration"]
    assert after["state_storage_version"] == 1
    assert after["cases"]["product-schema-v5-to-v6"] == 1
    assert after["requests"] == before["requests"] + 1
    assert after["reserved_read"] > before["reserved_read"]
    assert after["reserved_written"] > before["reserved_written"]
    assert after.get("read_margin_released", 0) == before.get("read_margin_released", 0)
    assert after["active"] is None and after["stopped"] is None
    assert after["schema_upgrade_v6"]["complete"] is True
    assert after["schema_upgrade_v6"]["write_execution"] == {
        "native_attempts": [None] * 9, "provenance": "d1-atomic-write-batch-with-fk-pragma-v1"}
    assert raw.calls == 4 and len(set(raw.reservations)) == 1
    assert not list(db.execute("PRAGMA foreign_key_check"))
    objects = tuple((row[0], row[1], row[2], " ".join(row[3].split()) if row[3] else None)
        for row in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"))
    assert objects == SCHEMA_OBJECTS
    restarted = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
    run(raw, restarted)
    assert raw.calls == 4 and restarted.snapshot() == after


def test_two_blank_projects_use_real_nulls_and_preserve_foreign_key_enforcement(v5):
    db, _, journal = v5
    run(SQLiteD1(db, journal), journal)
    for number in (1, 2):
        db.execute("INSERT INTO ledger_projects(id,owner_id,github_repo_id,github_full_name,created_at,"
                   "updated_at,name) VALUES(?,?,NULL,NULL,'new','new',?)",
                   ("blank-" + str(number), "owner", "Blank " + str(number)))
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM ledger_projects WHERE github_repo_id IS NULL").fetchone()[0] == 2
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    for values in ((None, "fake/name", "Blank", None), (None, None, " ", None),
                   (None, None, "Blank", 1), (0, "fake/name", "Linked", None)):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO ledger_projects(id,owner_id,github_repo_id,github_full_name,created_at,"
                       "updated_at,name,github_organization_id) VALUES('bad','owner',?,?,'n','n',?,?)", values)
        db.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM ledger_projects WHERE id='prj_old'")
    db.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE expenses SET project_id='missing' WHERE id='exp_old'")


@pytest.mark.parametrize("call", [1, 2, 3, 4])
def test_unknown_outcome_or_missing_native_meta_never_replays_or_refunds(v5, call):
    db, storage, journal = v5
    raw = SQLiteD1(db, journal, missing_meta=call)
    with pytest.raises(BudgetError, match="METERING_MISSING"):
        run(raw, journal)
    stopped = journal.snapshot()
    assert stopped["schema_version"] == 5
    assert stopped["schema_upgrade_v6"]["complete"] is False
    restarted = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
    with pytest.raises(BudgetError, match="METERING_MISSING"):
        run(raw, restarted)
    assert raw.calls == call and restarted.snapshot()["reserved_written"] == stopped["reserved_written"]


@pytest.mark.parametrize("call,attempts", [(1, 0), (2, 4), (3, 2), (3, True), (3, "1"), (4, 1.0)])
def test_invalid_explicit_attempts_stop_without_committing_version(v5, call, attempts):
    db, _, journal = v5
    with pytest.raises(BudgetError, match="MIGRATION_ATTEMPTS_UNPROVEN"):
        run(SQLiteD1(db, journal, attempts_by_call={call: attempts}), journal)
    assert journal.snapshot()["schema_version"] == 5
    assert journal.snapshot()["schema_upgrade_v6"]["complete"] is False


def test_three_read_attempts_are_accounted_but_never_release_margin(v5):
    db, _, journal = v5
    before = journal.snapshot()
    run(SQLiteD1(db, journal, attempts_by_call={1: 3, 2: 3, 4: 3}), journal)
    after = journal.snapshot()
    assert after["schema_version"] == 6
    assert after.get("read_margin_released", 0) == before.get("read_margin_released", 0)


def test_failure_after_parent_drop_rolls_back_entire_schema_and_all_facts(v5):
    db, _, journal = v5
    before = facts(db)
    class Injected(SQLiteD1):
        async def batch(self, statements):
            if any(item.sql == UPGRADE_V6_SQL[3] for item in statements):
                statements[4].sql = "SELECT no_such_function()"
            return await super().batch(statements)
    with pytest.raises(BudgetError, match="D1_OUTCOME_UNKNOWN"):
        run(Injected(db, journal), journal)
    assert facts(db) == before
    assert db.execute("PRAGMA table_info(ledger_projects)").fetchall()[2][3] == 1
    assert not db.execute("SELECT 1 FROM sqlite_schema WHERE name='ledger_projects_v6'").fetchone()
    assert not list(db.execute("PRAGMA foreign_key_check"))


def test_closed_product_plan_allows_current_capacity_above_old_test_ceilings(v5):
    db, _, journal = v5
    state = journal.snapshot()
    state["product_data"]["rows"]["app_state"] = 2006
    state["product_data"]["rows"]["expenses"] = 20_000
    state["product_data"]["rows"]["ledger_projects"] = 150
    plan = _upgrade_v6_plan(state)
    assert plan.rows_read > 100_000 and plan.rows_written > 1000
    assert len(plan.operations[2].sql) == 9


@pytest.mark.parametrize("change", ["stopped", "prior_incomplete", "cutover_incomplete", "v6_incomplete"])
def test_existing_unsafe_marker_never_dispatches_or_resets(v5, change):
    db, _, journal = v5
    state = journal.snapshot()
    if change == "stopped":
        state["stopped"] = "MANUAL_STOP"
    elif change == "prior_incomplete":
        state["schema_upgrade"]["complete"] = False
    elif change == "cutover_incomplete":
        state["state_record_migration"]["complete"] = False
    else:
        state["schema_upgrade_v6"] = {"from": 5, "to": 6, "complete": False}
    journal._save(state)
    raw = SQLiteD1(db, journal)
    with pytest.raises(BudgetError):
        run(raw, journal)
    assert raw.calls == 0 and journal.snapshot() == state
