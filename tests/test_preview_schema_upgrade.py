"""The compiled workspace migration shares the original persistent budget.

SQLite metadata here is synthetic; native D1 measurements remain a separate
gate. These tests cover admission, transaction outcomes and journal integrity.
"""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from test_d1_validation_budget import LocalSql
from pullwise_server.cloudflare_preview_schema import (
    SCHEMA_SQL, SCHEMA_OBJECTS, SCHEMA_VERSION, SCHEMA_FINGERPRINT,
    LEGACY_SCHEMA_SQL, LEGACY_SCHEMA_OBJECTS, LEGACY_INDEX_COUNTS, INDEX_COUNTS,
    V5_SCHEMA_FINGERPRINT,
)
from pullwise_server.cloudflare_preview_budget import (
    ProductMeteredD1, initialize_product, upgrade_product_schema,
    begin_product_schema_upgrade, _upgrade_plan,
)
from pullwise_server.cloudflare_validation_budget import BudgetError, BudgetJournal


class SQLiteD1:
    def __init__(self, db, journal, *, fail_call=None, missing_meta=None, attempts=1,
                 attempts_by_call=None):
        self.db, self.journal = db, journal
        self.fail_call, self.missing_meta, self.attempts = fail_call, missing_meta, attempts
        self.attempts_by_call = attempts_by_call or {}
        self.calls, self.reservations = 0, []

    def prepare(self, sql):
        return SimpleNamespace(sql=sql, bind=lambda *values: SimpleNamespace(sql=sql, params=values))

    async def batch(self, statements):
        self.calls += 1
        state = self.journal.snapshot()
        self.reservations.append((state["reserved_read"], state["reserved_written"]))
        if self.calls == self.fail_call:
            raise TimeoutError("synthetic unknown native outcome")
        self.db.execute("BEGIN")
        try:
            results = []
            for statement in statements:
                before = self.db.total_changes
                rows = [dict(row) for row in self.db.execute(statement.sql, statement.params).fetchall()]
                result = SimpleNamespace(success=True, results=rows)
                if self.calls != self.missing_meta:
                    result.meta = SimpleNamespace(rows_read=len(rows),
                        rows_written=self.db.total_changes - before,
                        total_attempts=self.attempts_by_call.get(self.calls, self.attempts))
                results.append(result)
            self.db.commit()
            return results
        except BaseException:
            self.db.rollback()
            raise


@pytest.fixture
def legacy():
    db, storage = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        for sql in LEGACY_SCHEMA_SQL:
            db.execute(sql)
        db.execute("UPDATE app_state SET payload=? WHERE name='users'", (
            json.dumps({"owner": {"id": "owner", "billing": {"plan": "free"}}}),))
        db.execute("INSERT INTO ledger_projects VALUES('prj_history','owner',202,'org/private',"
                   "'Keep this description','active',4,'old','old')")
        db.execute("INSERT INTO expense_categories VALUES('cat_history','owner','Hosting',NULL,NULL,1,'old','old')")
        db.execute("INSERT INTO expenses(id,owner_id,target_kind,project_id,category_id,occurred_on,"
                   "amount_minor,currency,purpose,revision,created_at,updated_at) VALUES("
                   "'exp_history','owner','project','prj_history','cat_history','2026-10-06',1250,'USD',"
                   "'Historical hosting',3,'old','old')")
        db.commit()
        journal = BudgetJournal(LocalSql(storage), preview_product=True)
        state = journal.snapshot()
        state.update(schema_ready=True, requests=89, cases={"product-schema": 1, "product": 88},
            reserved_read=1000, reserved_written=549, actual_read=800, actual_written=200,
            evidence=[{"request": 1, "operation": 1, "rows_read": 800, "rows_written": 200}],
            product_data={"rows": {table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                                   for table in LEGACY_INDEX_COUNTS},
                          "json": {"users": 1, "sessions": 0, "billingEvents": 0,
                                   "billingPendingUpdates": 0}, "arrays": 0})
        journal._save(state)
        yield db, storage, journal
    finally:
        db.close()
        storage.close()


def run_upgrade(raw, journal):
    return asyncio.run(upgrade_product_schema(raw, journal, clock=lambda: 10))


def test_populated_v4_upgrades_once_without_resetting_history_or_budget(legacy):
    db, _, journal = legacy
    before = journal.snapshot()
    raw = SQLiteD1(db, journal)
    run_upgrade(raw, journal)
    state = journal.snapshot()
    assert state["schema_version"] == 5
    assert state["schema_fingerprint"] == V5_SCHEMA_FINGERPRINT
    assert state["schema_upgrade"]["complete"] is True
    assert state["scope"] == before["scope"]
    assert state["requests"] == before["requests"] + 1
    assert state["cases"]["product-schema"] == 1
    assert state["cases"]["product"] == before["cases"]["product"] + 1
    assert state["reserved_written"] > before["reserved_written"]
    assert state["reserved_read"] > before["reserved_read"]
    assert state["evidence"][:1] == before["evidence"]
    assert state["active"] is None and state["stopped"] is None
    assert raw.calls == 4 and len(set(raw.reservations)) == 1
    assert raw.reservations[0][1] == state["reserved_written"]
    assert tuple(db.execute("SELECT id,owner_id,revision,description FROM ledger_projects").fetchone()) == (
        "prj_history", "owner", 4, "Keep this description")
    assert tuple(db.execute("SELECT id,project_id,amount_minor,revision FROM expenses").fetchone()) == (
        "exp_history", "prj_history", 1250, 3)
    assert tuple(db.execute("SELECT owner_id,project_id,github_repo_id FROM ledger_project_repositories").fetchone()) == (
        "owner", "prj_history", 202)
    assert state["product_data"]["rows"]["ledger_project_repositories"] == 1
    assert all(state["product_data"]["rows"][table] == 0 for table in (
        "workspace_members", "workspace_invites", "workspace_events"))
    completed = journal.snapshot()
    run_upgrade(raw, journal)
    assert raw.calls == 4 and journal.snapshot() == completed


def test_old_schema_regular_request_does_not_query_new_tables_or_stop_journal(legacy):
    db, _, journal = legacy
    raw = SQLiteD1(db, journal)
    before = journal.snapshot()
    with pytest.raises(BudgetError, match="SCHEMA_UPGRADE_REQUIRED"):
        asyncio.run(initialize_product(raw, journal, clock=lambda: 10))
    assert raw.calls == 0 and journal.snapshot() == before
    ticket = journal.begin_product(now=10)
    with pytest.raises(BudgetError, match="SCHEMA_UPGRADE_REQUIRED"):
        ProductMeteredD1(raw, journal, ticket, clock=lambda: 11)


def test_upgrade_reserves_all_phases_before_dispatch_and_preserves_insufficient_budget(legacy):
    db, _, journal = legacy
    state = journal.snapshot()
    state["reserved_written"] = 999
    journal._save(state)
    raw = SQLiteD1(db, journal)
    with pytest.raises(BudgetError, match="BUDGET_EXHAUSTED"):
        run_upgrade(raw, journal)
    assert raw.calls == 0
    assert journal.snapshot()["reserved_written"] == 999
    assert journal.snapshot()["requests"] == 89


@pytest.mark.parametrize("drift", ["table", "column"])
def test_mismatched_schema_stops_before_any_upgrade_sql(legacy, drift):
    db, _, journal = legacy
    if drift == "table":
        db.execute("CREATE TABLE unreviewed_data(id TEXT)")
    else:
        db.execute("ALTER TABLE expenses ADD COLUMN unreviewed TEXT")
    db.commit()
    raw = SQLiteD1(db, journal)
    with pytest.raises(BudgetError, match="PREVIEW_SCHEMA_MISMATCH"):
        run_upgrade(raw, journal)
    assert raw.calls == 1
    assert journal.snapshot()["stopped"] == "PREVIEW_SCHEMA_MISMATCH"
    assert journal.snapshot()["reserved_written"] > 549
    assert not db.execute("SELECT 1 FROM sqlite_schema WHERE name='workspace_members'").fetchall()


@pytest.mark.parametrize("fail_call", [1, 3, 4])
def test_unknown_outcome_stops_without_retry_or_refund_after_restart(legacy, fail_call):
    db, storage, journal = legacy
    raw = SQLiteD1(db, journal, fail_call=fail_call)
    with pytest.raises(BudgetError, match="D1_OUTCOME_UNKNOWN"):
        run_upgrade(raw, journal)
    stopped = journal.snapshot()
    assert stopped["reserved_written"] > 549
    restarted = BudgetJournal(LocalSql(storage), preview_product=True)
    assert restarted.snapshot()["stopped"] == "D1_OUTCOME_UNKNOWN"
    with pytest.raises(BudgetError):
        run_upgrade(raw, restarted)
    assert raw.calls == fail_call
    assert restarted.snapshot()["reserved_written"] == stopped["reserved_written"]


@pytest.mark.parametrize("missing_meta,attempts,code", [(3, 1, "METERING_MISSING"),
    (None, 2, "MIGRATION_ATTEMPTS_UNPROVEN"), (None, None, "MIGRATION_ATTEMPTS_UNPROVEN")])
def test_missing_or_retried_native_metadata_never_commits_schema_version(legacy, missing_meta, attempts, code):
    db, _, journal = legacy
    raw = SQLiteD1(db, journal, missing_meta=missing_meta, attempts=attempts)
    with pytest.raises(BudgetError, match=code):
        run_upgrade(raw, journal)
    assert journal.snapshot().get("schema_version") is None
    assert journal.snapshot()["stopped"] == code
    assert journal.snapshot()["reserved_written"] > 549


def test_exact_write_group_missing_attempts_uses_contract_without_fabricating_metadata(legacy):
    db, _, journal = legacy
    raw = SQLiteD1(db, journal, attempts_by_call={3: None})
    run_upgrade(raw, journal)
    state = journal.snapshot()
    assert state["schema_version"] == 5
    assert state["schema_upgrade"]["write_execution"] == {
        "native_attempts": [None] * 10,
        "provenance": "d1-nonretryable-write-contract-v1",
    }
    assert raw.calls == 4 and len(set(raw.reservations)) == 1
    assert state["reserved_written"] == raw.reservations[0][1]


@pytest.mark.parametrize("read_call", [1, 2, 4])
def test_missing_read_attempts_still_stops_and_retains_full_reservation(legacy, read_call):
    db, _, journal = legacy
    raw = SQLiteD1(db, journal, attempts_by_call={read_call: None})
    with pytest.raises(BudgetError, match="MIGRATION_ATTEMPTS_UNPROVEN"):
        run_upgrade(raw, journal)
    state = journal.snapshot()
    assert state.get("schema_version") is None
    assert state["schema_upgrade"]["complete"] is False
    assert state["stopped"] == "MIGRATION_ATTEMPTS_UNPROVEN"
    assert raw.calls == read_call
    assert state["reserved_written"] == raw.reservations[0][1]


@pytest.mark.parametrize("attempts", [0, 2, True, 1.0, "1"])
def test_write_attempts_other_than_integer_one_stop_without_refunding(legacy, attempts):
    db, _, journal = legacy
    raw = SQLiteD1(db, journal, attempts_by_call={3: attempts})
    with pytest.raises(BudgetError, match="MIGRATION_ATTEMPTS_UNPROVEN"):
        run_upgrade(raw, journal)
    state = journal.snapshot()
    assert state.get("schema_version") is None
    assert state["stopped"] == "MIGRATION_ATTEMPTS_UNPROVEN"
    assert raw.calls == 3 and len(set(raw.reservations)) == 1
    assert state["reserved_written"] == raw.reservations[0][1]


def test_case_reservation_cannot_be_replayed_while_active(legacy):
    _, _, journal = legacy
    plan, _ = _upgrade_plan(journal.snapshot())
    begin_product_schema_upgrade(journal, plan, now=10)
    state = journal.snapshot()
    with pytest.raises(BudgetError, match="SCHEMA_UPGRADE_UNREVIEWED"):
        begin_product_schema_upgrade(journal, plan, now=10)
    assert journal.snapshot() == state


@pytest.mark.parametrize("change", ["missing_initialization", "wrong_fingerprint", "stopped"])
def test_unreviewed_or_stopped_upgrade_cannot_dispatch(legacy, change):
    db, _, journal = legacy
    state = journal.snapshot()
    if change == "missing_initialization":
        state["cases"]["product-schema"] = 0
    elif change == "wrong_fingerprint":
        state["schema_fingerprint"] = "unreviewed"
    else:
        state["stopped"] = "MANUAL_STOP"
    journal._save(state)
    raw = SQLiteD1(db, journal)
    with pytest.raises(BudgetError):
        run_upgrade(raw, journal)
    assert raw.calls == 0 and journal.snapshot() == state


def test_cardinality_above_persisted_upper_stops_before_ddl(legacy):
    db, _, journal = legacy
    db.execute("INSERT INTO ledger_projects VALUES('prj_untracked','owner',303,'org/another',"
               "'','active',1,'old','old')")
    db.commit()
    raw = SQLiteD1(db, journal)
    with pytest.raises(BudgetError, match="PREVIEW_DATA_BOUND"):
        run_upgrade(raw, journal)
    assert raw.calls == 2
    assert not db.execute("SELECT 1 FROM sqlite_schema WHERE name='workspace_members'").fetchone()
    assert journal.snapshot()["stopped"] == "SCHEMA_UPGRADE_OUTCOME_UNKNOWN"


def test_post_ddl_schema_mismatch_never_publishes_current_version(legacy):
    db, _, journal = legacy
    class Drift(SQLiteD1):
        async def batch(self, statements):
            result = await super().batch(statements)
            if self.calls == 3:
                self.db.execute("CREATE TABLE unreviewed_after_ddl(id TEXT)")
                self.db.commit()
            return result
    raw = Drift(db, journal)
    with pytest.raises(BudgetError, match="PREVIEW_SCHEMA_MISMATCH"):
        run_upgrade(raw, journal)
    assert raw.calls == 4
    assert journal.snapshot().get("schema_version") is None
    assert journal.snapshot()["schema_upgrade"]["complete"] is False
    assert journal.snapshot()["stopped"] == "PREVIEW_SCHEMA_MISMATCH"


def test_unregistered_table_select_is_rejected_before_dispatch():
    db, storage = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    try:
        from pullwise_server.cloudflare_preview_budget import initial_data
        journal = BudgetJournal(LocalSql(storage), preview_product=True)
        state = journal.snapshot()
        state["product_data"] = initial_data()
        journal._save(state)
        ticket = journal.begin_product(now=10)
        raw = SQLiteD1(db, journal)
        meter = ProductMeteredD1(raw, journal, ticket, clock=lambda: 11)
        with pytest.raises(BudgetError, match="UNREVIEWED_SQL_BOUND"):
            asyncio.run(meter.batch([meter.prepare("SELECT * FROM unreviewed_data")]))
        assert raw.calls == 0
    finally:
        db.close()
        storage.close()


def test_fresh_schema_is_versioned_and_matches_all_canonical_migrations():
    db, storage = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        journal = BudgetJournal(LocalSql(storage), preview_product=True)
        raw = SQLiteD1(db, journal)
        asyncio.run(initialize_product(raw, journal, clock=lambda: 10))
        assert journal.snapshot()["schema_version"] == SCHEMA_VERSION
        assert journal.snapshot()["schema_fingerprint"] == SCHEMA_FINGERPRINT
        objects = tuple((row[0], row[1], row[2], " ".join(row[3].split()) if row[3] else None)
            for row in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"))
        assert objects == SCHEMA_OBJECTS
        assert len(objects) > len(LEGACY_SCHEMA_OBJECTS)
        for table, count in INDEX_COUNTS.items():
            assert len(db.execute("PRAGMA index_list(" + table + ")").fetchall()) == count
        assert len(SCHEMA_SQL) <= 64
        from pathlib import Path
        canonical = sqlite3.connect(":memory:")
        try:
            for migration in sorted((Path(__file__).resolve().parents[1] /
                                     "cloudflare/server/migrations").glob("*.sql")):
                canonical.executescript(migration.read_text(encoding="utf-8"))
            canonical_objects = tuple((row[0], row[1], row[2],
                " ".join(row[3].split()) if row[3] else None) for row in canonical.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"))
            assert objects == canonical_objects
        finally:
            canonical.close()
    finally:
        db.close()
        storage.close()
