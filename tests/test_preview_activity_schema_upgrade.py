"""Closed v8-to-v9 extension; native provider accounting is proved separately."""
import asyncio
import json
import sqlite3

import pytest

from test_d1_validation_budget import LocalSql
from test_preview_invite_approval_schema_upgrade import v7, add_auth_records
from test_preview_recurring_schema_upgrade import v6, schema
from test_preview_blank_schema_upgrade import v5
from test_preview_schema_upgrade import SQLiteD1
from pullwise_server.cloudflare_preview_schema import (
    V9_SCHEMA_VERSION as SCHEMA_VERSION, V9_SCHEMA_FINGERPRINT as SCHEMA_FINGERPRINT,
    V9_SCHEMA_OBJECTS as SCHEMA_OBJECTS, V9_SCHEMA_SQL as SCHEMA_SQL, V9_INDEX_COUNTS as INDEX_COUNTS,
    V8_INDEX_COUNTS, UPGRADE_V9_SQL,
)
from pullwise_server.cloudflare_preview_budget import (
    upgrade_product_schema_v8, upgrade_product_schema_v9, _upgrade_v9_plan,
    begin_product_schema_upgrade_v9, _V8_COUNT_SQL, _input_bound, sql_write_bound,
    ProductMeteredD1, upgrade_product_schema_v10, upgrade_product_schema_v11,
)
from pullwise_server.cloudflare_validation_budget import BudgetJournal, BudgetError


@pytest.fixture
def v8(v7):
    db, storage, journal = v7
    add_auth_records(db, journal)
    asyncio.run(upgrade_product_schema_v8(SQLiteD1(db, journal), journal, clock=lambda: 10))
    db.execute("INSERT INTO workspace_join_requests(id,workspace_id,invite_id,applicant_user_id,"
               "created_at,updated_at) VALUES('retained','owner','invite-pending','applicant','old','old')")
    db.commit()
    saved = journal.snapshot()
    saved['product_data']['rows'] = dict(db.execute(_V8_COUNT_SQL).fetchone())
    journal._save(saved)
    return db, storage, journal


def facts(db):
    return {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)]
            for table in V8_INDEX_COUNTS}


def run(raw, journal):
    asyncio.run(upgrade_product_schema_v9(raw, journal, clock=lambda: 11))


def test_activity_extension_preserves_identity_finance_membership_and_journal(v8):
    db, storage, journal = v8
    original, before = facts(db), journal.snapshot()
    raw = SQLiteD1(db, journal, attempts=None)
    run(raw, journal)
    after = journal.snapshot()
    assert SCHEMA_VERSION == after['schema_version'] == 9
    assert after['schema_fingerprint'] == SCHEMA_FINGERPRINT
    assert schema(db) == SCHEMA_OBJECTS and facts(db) == original
    assert after['product_data']['rows'] == {**before['product_data']['rows'], 'ledger_activity_events': 0}
    for key in ('scope', 'schema_upgrade', 'schema_upgrade_v6', 'schema_upgrade_v7',
                'schema_upgrade_v8', 'state_record_migration', 'state_storage_version',
                'state_record_integrity_version', 'state_record_integrity_request'):
        assert after[key] == before[key]
    assert after['requests'] == before['requests'] + 1
    assert after['cases']['product-schema-v8-to-v9'] == 1
    assert after['schema_upgrade_v9']['complete'] is True
    assert after['schema_upgrade_v9']['write_execution'] == {
        'native_attempts': [None] * len(UPGRADE_V9_SQL),
        'provenance': 'd1-nonretryable-write-contract-v1'}
    assert after['evidence'][:len(before['evidence'])] == before['evidence']
    assert after['reserved_written'] == before['reserved_written'] + 128
    # SQLite total_changes omits DDL catalog writes; the native proof records
    # Cloudflare's actual rows_written for the three-statement CREATE group.
    assert after['actual_written'] >= before['actual_written']
    assert after['active'] is None and after['stopped'] is None
    restarted = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
    run(raw, restarted)
    assert raw.calls == 4 and restarted.snapshot() == after
    # Current product reads use v11. Every extension proves strict auth records
    # within their bounded upgrade, so a healthy first read needs no scan.
    asyncio.run(upgrade_product_schema_v10(raw, restarted, clock=lambda: 12))
    asyncio.run(upgrade_product_schema_v11(raw, restarted, clock=lambda: 12))
    calls = raw.calls
    ticket = restarted.begin_product(now=12)
    meter = ProductMeteredD1(raw, restarted, ticket, clock=lambda: 13)
    asyncio.run(meter.ensure_cardinality())
    assert raw.calls == calls and meter.records_integrity_verified is True
    restarted.finish(ticket, now=14)


@pytest.mark.parametrize('change', ['stopped', 'unverified', 'fingerprint', 'v7_incomplete',
                                    'v8_incomplete', 'cutover_incomplete', 'v9_incomplete'])
def test_incomplete_or_unreviewed_state_never_dispatches(v8, change):
    db, _, journal = v8
    saved = journal.snapshot()
    if change == 'stopped':
        saved['stopped'] = 'D1_OUTCOME_UNKNOWN'
    elif change == 'unverified':
        saved['product_data_verified'] = False
    elif change == 'fingerprint':
        saved['schema_fingerprint'] = 'unreviewed'
    elif change == 'cutover_incomplete':
        saved['state_record_migration']['complete'] = False
    else:
        saved['schema_upgrade_' + change[:2]] = {'complete': False}
    journal._save(saved)
    raw = SQLiteD1(db, journal)
    with pytest.raises(BudgetError):
        run(raw, journal)
    assert raw.calls == 0 and journal.snapshot() == saved


def test_native_index_failure_rolls_back_and_retains_unknown_reservation(v8):
    db, storage, journal = v8
    original, original_schema = facts(db), schema(db)

    class Failure(SQLiteD1):
        async def batch(self, statements):
            if tuple(item.sql for item in statements) == UPGRADE_V9_SQL:
                db.execute('BEGIN')
                try:
                    db.execute(statements[0].sql)
                    db.execute(statements[1].sql)
                    raise RuntimeError('injected after native index creation')
                finally:
                    db.rollback()
            return await super().batch(statements)

    raw = Failure(db, journal)
    with pytest.raises(BudgetError, match='D1_OUTCOME_UNKNOWN'):
        run(raw, journal)
    stopped = journal.snapshot()
    assert stopped['schema_version'] == 8 and stopped['schema_upgrade_v9']['complete'] is False
    assert stopped['stopped'] == 'D1_OUTCOME_UNKNOWN'
    assert facts(db) == original and schema(db) == original_schema
    restarted = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
    calls = raw.calls
    with pytest.raises(BudgetError, match='D1_OUTCOME_UNKNOWN'):
        run(raw, restarted)
    assert raw.calls == calls and restarted.snapshot() == stopped


def test_closed_plan_never_scans_or_backfills_expense_history(v8):
    db, _, journal = v8
    saved = journal.snapshot()
    plan = _upgrade_v9_plan(saved)
    rows = saved['product_data']['rows']
    assert plan.rows_read == 15744 + 96 * sum(rows.values()) + 384 * rows['app_state']
    assert plan.rows_written == 128 and len(plan.operations) == 4
    assert plan.operations[2].sql == UPGRADE_V9_SQL and len(UPGRADE_V9_SQL) == 3
    assert all(sql.lstrip().splitlines()[-1].strip().endswith(';') for sql in UPGRADE_V9_SQL)
    with pytest.raises(BudgetError, match='SCHEMA_V9_UPGRADE_UNREVIEWED'):
        begin_product_schema_upgrade_v9(journal, None, now=11)
    assert journal.snapshot() == saved


def test_activity_snapshots_keep_exact_json_envelopes_and_index_costs():
    sql = 'INSERT INTO ledger_activity_events(before_json,after_json) VALUES(?,?)'
    snapshot = json.dumps({'note': '界' * 4000}, ensure_ascii=False)
    assert 8192 < len(snapshot.encode()) < 16384
    assert _input_bound((snapshot, snapshot), sql=sql) == 0
    with pytest.raises(ValueError):
        _input_bound((snapshot, '[]'), sql=sql)
    with pytest.raises(ValueError):
        _input_bound((json.dumps({'note': '界' * 6000}, ensure_ascii=False), None), sql=sql)
    actor_sql = 'INSERT INTO ledger_activity_events(actor_json) VALUES(?)'
    assert _input_bound(('{"name":"member"}',), sql=actor_sql) == 0
    with pytest.raises(ValueError):
        _input_bound((json.dumps({'name': '界' * 1000}, ensure_ascii=False),), sql=actor_sql)
    with pytest.raises(ValueError):
        _input_bound(('[]',), sql=actor_sql)
    assert sql_write_bound(sql) == 4
    assert sql_write_bound('DELETE FROM ledger_activity_events WHERE id=?') == 4
    with pytest.raises(ValueError):
        sql_write_bound('DELETE FROM ledger_activity_events WHERE created_at<?')


def test_fresh_schema_matches_canonical_migrations_within_original_batch_cap():
    from pathlib import Path
    from pullwise_server.cloudflare_preview_schema import SCHEMA_SQL, SCHEMA_OBJECTS, INDEX_COUNTS
    with sqlite3.connect(':memory:') as canonical, sqlite3.connect(':memory:') as compiled:
        for migration in sorted((Path(__file__).resolve().parents[1] / 'cloudflare/server/migrations').glob('*.sql')):
            canonical.executescript(migration.read_text())
        for sql in SCHEMA_SQL:
            compiled.execute(sql)
        assert len(SCHEMA_SQL) == 48 <= 64
        assert schema(canonical) == schema(compiled) == SCHEMA_OBJECTS
        assert len(INDEX_COUNTS) == 22 and sum(INDEX_COUNTS.values()) == 50
