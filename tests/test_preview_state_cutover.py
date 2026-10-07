"""Local SQL behavior of the closed cutover; native accounting is separate."""
import asyncio
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from test_d1_validation_budget import LocalSql
from pullwise_server.cloudflare_preview_budget import (
    migrate_product_state_records, ProductMeteredD1, _COUNT_SQL, _STATE_SQL,
    _STRICT_RECORD_SQL, _compile_state_record_cutover, _input_bound,
)
from pullwise_server.cloudflare_preview_schema import SCHEMA_SQL, SCHEMA_VERSION, SCHEMA_FINGERPRINT
from pullwise_server.cloudflare_state_records import record_name, encode_record, read_records
from pullwise_server.cloudflare_validation_budget import BudgetJournal, BudgetError


class Statement:
    def __init__(self, sql, params=()):
        self.sql, self.params = sql, params
    def bind(self, *params):
        return Statement(self.sql, params)


class SQLiteBehavior:
    """D1-shaped fixture; its placeholder read meta is not invoice evidence."""
    def __init__(self, connection):
        self.connection, self.groups, self.before_write = connection, [], None
    def prepare(self, sql):
        return Statement(sql)
    async def batch(self, statements):
        self.groups.append([item.sql for item in statements])
        if self.before_write and any(item.sql.startswith("INSERT INTO d1_command_guard") for item in statements):
            self.before_write()
        results = []
        with self.connection:
            for statement in statements:
                before = self.connection.total_changes
                cursor = self.connection.execute(statement.sql, statement.params)
                rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
                results.append(SimpleNamespace(success=True, results=rows,
                    meta=SimpleNamespace(rows_read=0, rows_written=self.connection.total_changes - before,
                                         total_attempts=None)))
        return results


def historical_fixture(connection, journal):
    connection.row_factory = sqlite3.Row
    for sql in SCHEMA_SQL:
        connection.execute(sql)
    user = {"id": "owner", "githubAccessToken": "opaque-encrypted-token", "billing": {"plan": "pro"}}
    state = {"users": {"owner": user}, "sessions": {"opaque-old-session": {"userId": "owner", "expiresAt": 100}},
        "githubStates": {"old-oauth": {"kind": "login", "expiresAt": 100}},
        "billingEvents": {"old-event": {"applied": True}},
        "billingPendingUpdates": [{"eventId": "pending-event", "userId": "owner", "status": "active"}]}
    for name, value in state.items():
        connection.execute("INSERT OR REPLACE INTO app_state VALUES(?,?,?)", (name, json.dumps(value), 1))
    connection.commit()
    counts = dict(connection.execute(_COUNT_SQL).fetchone())
    saved = journal.snapshot()
    saved.update(schema_ready=True, schema_version=SCHEMA_VERSION, schema_fingerprint=SCHEMA_FINGERPRINT,
        product_data={"rows": counts, "json": {name: len(value) for name, value in state.items()}, "arrays": 1},
        product_data_verified=True, requests=9, cases={"historical": 9},
        reserved_read=900, actual_read=40, reserved_written=700, actual_written=30,
        evidence=[{"request": 9, "operation": 1, "rows_read": 40, "rows_written": 30}])
    journal._save(saved)
    return state


def test_cutover_is_atomic_one_shot_preserves_facts_schema_and_cumulative_accounting():
    with closing(sqlite3.connect(":memory:")) as sql, closing(sqlite3.connect(":memory:")) as d1:
        journal = BudgetJournal(LocalSql(sql), preview_product=True, product_operations=True)
        legacy = historical_fixture(d1, journal)
        schema = [tuple(row) for row in d1.execute("SELECT name,sql FROM sqlite_schema ORDER BY name")]
        before, raw = journal.snapshot(), SQLiteBehavior(d1)
        asyncio.run(migrate_product_state_records(raw, journal, clock=lambda: 10))
        after = journal.snapshot()
        assert after["state_storage_version"] == 1 and after["state_record_migration"]["complete"] is True
        assert after["state_record_migration"]["copied_records"] == 5
        assert after["requests"] == before["requests"] + 1
        assert after["cases"]["historical"] == 9 and after["cases"]["product-state-record-v1"] == 1
        assert after["evidence"] == before["evidence"]
        assert after["reserved_written"] > before["reserved_written"] and after["actual_written"] > before["actual_written"]
        assert after["schema_version"] == SCHEMA_VERSION and after["schema_fingerprint"] == SCHEMA_FINGERPRINT
        assert [tuple(row) for row in d1.execute("SELECT name,sql FROM sqlite_schema ORDER BY name")] == schema
        assert len(raw.groups) == 3 and len(raw.groups[1]) <= 64
        for kind, values in legacy.items():
            items = ((item["eventId"], item) for item in values) if isinstance(values, list) else values.items()
            for identity, value in items:
                assert json.loads(d1.execute("SELECT payload FROM app_state WHERE name=?",
                    (record_name(kind, identity),)).fetchone()[0]) == value
            assert json.loads(d1.execute("SELECT payload FROM app_state WHERE name=?", (kind,)).fetchone()[0]) == ([] if isinstance(values, list) else {})
        reconstructed = BudgetJournal(LocalSql(sql), preview_product=True, product_operations=True)
        observed = reconstructed.snapshot()
        calls = len(raw.groups)
        asyncio.run(migrate_product_state_records(raw, reconstructed, clock=lambda: 11))
        assert len(raw.groups) == calls and reconstructed.snapshot() == observed


def test_cutover_guard_conflict_rolls_back_all_records_and_preserves_legacy_facts():
    with closing(sqlite3.connect(":memory:")) as sql, closing(sqlite3.connect(":memory:")) as d1:
        journal = BudgetJournal(LocalSql(sql), preview_product=True, product_operations=True)
        historical_fixture(d1, journal)
        raw = SQLiteBehavior(d1)
        def replace():
            d1.execute("UPDATE app_state SET payload=? WHERE name='users'", ('{"owner":{"id":"owner","changed":true}}',))
            d1.commit()
        raw.before_write = replace
        with pytest.raises(BudgetError, match="D1_OUTCOME_UNKNOWN"):
            asyncio.run(migrate_product_state_records(raw, journal, clock=lambda: 10))
        assert d1.execute("SELECT COUNT(*) FROM app_state WHERE name GLOB 'record:*'").fetchone()[0] == 0
        assert json.loads(d1.execute("SELECT payload FROM app_state WHERE name='sessions'").fetchone()[0])["opaque-old-session"]
        assert journal.snapshot()["state_record_migration"]["complete"] is False
        calls = len(raw.groups)
        with pytest.raises(BudgetError, match="D1_OUTCOME_UNKNOWN"):
            asyncio.run(migrate_product_state_records(raw, journal, clock=lambda: 11))
        assert len(raw.groups) == calls


def test_cutover_guards_observed_unknown_empty_row_before_any_copy():
    with closing(sqlite3.connect(":memory:")) as sql, closing(sqlite3.connect(":memory:")) as d1:
        journal = BudgetJournal(LocalSql(sql), preview_product=True, product_operations=True)
        historical_fixture(d1, journal)
        d1.execute("INSERT INTO app_state VALUES('legacy-unused','{}',1)")
        d1.commit()
        state = journal.snapshot()
        state["product_data"]["rows"]["app_state"] += 1
        journal._save(state)
        before = list(d1.execute("SELECT name,payload FROM app_state WHERE name!='legacy-unused' ORDER BY name"))
        raw = SQLiteBehavior(d1)
        def grow_unknown():
            d1.execute("UPDATE app_state SET payload='{\"unexpected\":true}' WHERE name='legacy-unused'")
            d1.commit()
        raw.before_write = grow_unknown
        with pytest.raises(BudgetError, match="D1_OUTCOME_UNKNOWN"):
            asyncio.run(migrate_product_state_records(raw, journal, clock=lambda: 10))
        assert list(d1.execute("SELECT name,payload FROM app_state WHERE name!='legacy-unused' ORDER BY name")) == before
        assert d1.execute("SELECT COUNT(*) FROM app_state WHERE name GLOB 'record:*'").fetchone()[0] == 0
        assert journal.snapshot()["state_record_migration"]["complete"] is False


@pytest.mark.parametrize("payload", [
    '{"owner":{"id":"owner","id":"owner"}}',
    '{"owner":{"id":"owner","opaque":{"x":1,"x":2}}}',
    '{"owner":{"id":"owner","opaque":1e999}}',
    '{"owner":{"id":"owner","opaque":"\\ud800"}}',
])
def test_cutover_strictly_decodes_all_legacy_fields_before_compiling(payload):
    with pytest.raises((ValueError, UnicodeError)):
        _compile_state_record_cutover([{"name": "users", "payload": payload}], 1, 10)


def test_cutover_refuses_even_empty_preexisting_normalized_rows():
    with pytest.raises(ValueError, match="pre-existing record"):
        _compile_state_record_cutover([
            {"name": "users", "payload": '{"owner":{"id":"owner"}}'},
            {"name": "record:billingEvents:imported", "payload": "{}"},
        ], 2, 10)


def test_user_record_enumeration_caps_byte_envelope_and_default_remains_usable():
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE app_state(name TEXT PRIMARY KEY,payload TEXT,updated_at INTEGER)")
        for number in range(17):
            identity = f"owner-{number:02}"
            payload = encode_record("users", identity, {"id": identity, "opaque": "x" * (512 * 1024 - 100)})
            connection.execute("INSERT INTO app_state VALUES(?,?,0)", (record_name("users", identity), payload))
        connection.commit()
        before = connection.total_changes
        raw = SQLiteBehavior(connection)
        page = asyncio.run(read_records(raw, "users", limit=17))
        assert len(page) == 16 and sum(len(item["snapshot"].encode()) for item in page) <= 8 * 1024 * 1024
        remaining = asyncio.run(read_records(raw, "users", cursor=page[-1]["id"]))
        assert [item["id"] for item in remaining] == ["owner-16"]
        assert connection.total_changes == before


@pytest.mark.parametrize("identity", ["bad\x00id", "bad\x01id", "bad\x1fid", "bad\x7fid"])
def test_structural_aggregate_rejects_record_identity_controls(identity):
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("CREATE TABLE app_state(name TEXT PRIMARY KEY,payload TEXT,updated_at INTEGER)")
        connection.execute("INSERT INTO app_state VALUES(?,?,0)",
            ("record:users:" + identity, json.dumps({"id": identity})))
        assert connection.execute(_STRICT_RECORD_SQL).fetchone()[0] == 1


@pytest.mark.parametrize("reason", ["TIMEOUT", "INCOMPLETE_REQUEST", "MANUAL_STOP", "METERING_MISSING"])
def test_existing_unknown_or_operator_stop_never_dispatches_cutover(reason):
    with closing(sqlite3.connect(":memory:")) as sql, closing(sqlite3.connect(":memory:")) as d1:
        journal = BudgetJournal(LocalSql(sql), preview_product=True, product_operations=True)
        historical_fixture(d1, journal)
        journal.stop(reason)
        raw, before = SQLiteBehavior(d1), journal.snapshot()
        with pytest.raises(BudgetError, match=reason):
            asyncio.run(migrate_product_state_records(raw, journal, clock=lambda: 10))
        assert raw.groups == [] and journal.snapshot() == before


def test_typed_user_envelope_requires_matching_exact_key_and_rejects_before_dispatch():
    user = {"id": "owner", "history": "x" * 20000}
    payload = encode_record("users", "owner", user)
    sql = "UPDATE app_state SET payload=?,updated_at=? WHERE name=? AND payload=?"
    _input_bound((payload, 1, record_name("users", "owner"), payload), sql=sql)
    for name in ("users", record_name("users", "other"), record_name("sessions", "owner")):
        with pytest.raises(ValueError):
            _input_bound((payload, 1, name, payload), sql=sql)
    with pytest.raises(ValueError):
        _input_bound((json.dumps({"id": "owner", "extra": "x" * (512 * 1024)}), 1,
                      record_name("users", "owner"), payload), sql=sql)


def test_record_mutation_refresh_uses_numeric_queries_and_unverified_state_checks_once():
    with closing(sqlite3.connect(":memory:")) as sql, closing(sqlite3.connect(":memory:")) as d1:
        journal = BudgetJournal(LocalSql(sql), preview_product=True, product_operations=True)
        historical_fixture(d1, journal)
        raw = SQLiteBehavior(d1)
        asyncio.run(migrate_product_state_records(raw, journal, clock=lambda: 10))
        ticket = journal.begin_product(now=11)
        meter = ProductMeteredD1(raw, journal, ticket, clock=lambda: 12)
        payload = encode_record("users", "owner", {"id": "owner", "large": "x" * 20000})
        asyncio.run(meter.batch([meter.prepare("UPDATE app_state SET payload=?,updated_at=? WHERE name=?").bind(
            payload, 12, record_name("users", "owner"))]))
        assert len(raw.groups[-1]) == 2 and _STRICT_RECORD_SQL not in raw.groups[-1]
        assert all(query != _STATE_SQL for query in raw.groups[-1])
        journal.finish(ticket, now=13)
        state = journal.snapshot()
        state["product_data_verified"] = False
        journal._save(state)
        ticket = journal.begin_product(now=14)
        meter = ProductMeteredD1(raw, journal, ticket, clock=lambda: 15)
        asyncio.run(meter.ensure_cardinality())
        assert _STRICT_RECORD_SQL in raw.groups[-1]
        previous = len(raw.groups)
        asyncio.run(meter.ensure_cardinality())
        assert len(raw.groups) == previous
        journal.finish(ticket, now=16)
