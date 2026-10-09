"""Populated v7-to-v8 integrity; native D1 metadata is proved separately."""
import asyncio
import sqlite3

import pytest

from test_d1_validation_budget import LocalSql
from test_preview_blank_schema_upgrade import v5
from test_preview_recurring_schema_upgrade import v6, schema
from test_preview_schema_upgrade import SQLiteD1
from pullwise_server.cloudflare_preview_schema import (
    SCHEMA_VERSION, SCHEMA_FINGERPRINT, SCHEMA_OBJECTS, INDEX_COUNTS,
    V7_INDEX_COUNTS, UPGRADE_V8_SQL,
)
from pullwise_server.cloudflare_preview_budget import (
    upgrade_product_schema_v7, upgrade_product_schema_v8, _upgrade_v8_plan,
    begin_product_schema_upgrade_v8, _V7_COUNT_SQL,
)
from pullwise_server.cloudflare_validation_budget import BudgetJournal, BudgetError


@pytest.fixture
def v7(v6):
    db, storage, journal = v6
    asyncio.run(upgrade_product_schema_v7(SQLiteD1(db, journal), journal, clock=lambda: 10))
    db.execute("INSERT INTO workspace_members VALUES('owner','member','viewer',4,'old','old',NULL,'owner')")
    for index, status in enumerate(('pending', 'accepted', 'revoked'), 1):
        accepted = ('member', 'accepted-old') if status == 'accepted' else (None, None)
        db.execute("INSERT INTO workspace_invites VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            'invite-' + status, 'owner', 7000 + index, 'old-user-' + str(index),
            str(index) * 64, 'viewer', status, index + 2, 1000, 'owner', 7,
            'old', 'old', *accepted))
        db.execute("INSERT INTO workspace_events VALUES(?,?,?,?,?,?,?,?)", (
            'event-' + status, 'owner', 'owner', 'invite', 'invite-' + status,
            None, '{"retain":"' + status + '"}', 'old'))
    db.commit()
    state = journal.snapshot()
    state['product_data']['rows'] = dict(db.execute(_V7_COUNT_SQL).fetchone())
    journal._save(state)
    return db, storage, journal


def run(raw, journal):
    asyncio.run(upgrade_product_schema_v8(raw, journal, clock=lambda: 10))


def history(db):
    return {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)]
            for table in V7_INDEX_COUNTS}


def test_upgrade_preserves_every_fact_marker_and_accounting_across_restart(v7):
    db, storage, journal = v7
    before, original = journal.snapshot(), history(db)
    raw = SQLiteD1(db, journal, attempts=None)
    run(raw, journal)
    after = journal.snapshot()
    assert after['schema_version'] == SCHEMA_VERSION == 8
    assert after['schema_fingerprint'] == SCHEMA_FINGERPRINT
    assert history(db) == original
    for key in ('scope', 'schema_upgrade', 'schema_upgrade_v6', 'schema_upgrade_v7',
                'state_record_migration', 'state_storage_version'):
        assert after[key] == before[key]
    assert after['requests'] == before['requests'] + 1
    assert after['reserved_read'] > before['reserved_read']
    assert after['reserved_written'] > before['reserved_written']
    assert after['actual_read'] >= before['actual_read']
    assert after['actual_written'] > before['actual_written']
    assert after['evidence'][:len(before['evidence'])] == before['evidence']
    assert after.get('read_margin_released', 0) == before.get('read_margin_released', 0)
    assert after['cases']['product-schema-v7-to-v8'] == 1
    assert after['schema_upgrade_v8']['complete'] is True
    assert after['schema_upgrade_v8']['write_execution'] == {
        'native_attempts': [None] * len(UPGRADE_V8_SQL),
        'provenance': 'd1-nonretryable-write-contract-v1'}
    assert after['product_data_verified'] is True and after['active'] is None and after['stopped'] is None
    assert set(after['product_data']['rows']) == set(INDEX_COUNTS)
    assert after['product_data']['rows']['workspace_join_requests'] == 0
    assert schema(db) == SCHEMA_OBJECTS
    assert not list(db.execute('PRAGMA foreign_key_check'))
    assert raw.calls == 4 and len(set(raw.reservations)) == 1
    restarted = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
    run(raw, restarted)
    assert raw.calls == 4 and restarted.snapshot() == after


@pytest.mark.parametrize('call', [1, 2, 3, 4])
def test_missing_native_metadata_never_replays_or_refunds(v7, call):
    db, storage, journal = v7
    raw = SQLiteD1(db, journal, missing_meta=call)
    with pytest.raises(BudgetError, match='METERING_MISSING'):
        run(raw, journal)
    stopped = journal.snapshot()
    assert stopped['schema_version'] == 7 and stopped['schema_upgrade_v8']['complete'] is False
    restarted = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
    with pytest.raises(BudgetError, match='METERING_MISSING'):
        run(raw, restarted)
    assert raw.calls == call and restarted.snapshot() == stopped


@pytest.mark.parametrize('call', [1, 3, 4])
def test_unknown_native_outcome_is_never_retried(v7, call):
    db, storage, journal = v7
    raw = SQLiteD1(db, journal, fail_call=call)
    with pytest.raises(BudgetError, match='D1_OUTCOME_UNKNOWN'):
        run(raw, journal)
    stopped = journal.snapshot()
    restarted = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
    with pytest.raises(BudgetError, match='D1_OUTCOME_UNKNOWN'):
        run(raw, restarted)
    assert raw.calls == call and restarted.snapshot() == stopped


@pytest.mark.parametrize('call,attempts', [(1, 0), (2, 4), (3, 2), (3, True), (3, '1'), (4, 1.0)])
def test_invalid_native_attempts_never_publish_schema_version(v7, call, attempts):
    db, _, journal = v7
    with pytest.raises(BudgetError, match='MIGRATION_ATTEMPTS_UNPROVEN'):
        run(SQLiteD1(db, journal, attempts_by_call={call: attempts}), journal)
    assert journal.snapshot()['schema_version'] == 7
    assert journal.snapshot()['schema_upgrade_v8']['complete'] is False


def test_three_read_attempts_retain_complete_reserved_margin(v7):
    db, _, journal = v7
    before = journal.snapshot()
    run(SQLiteD1(db, journal, attempts_by_call={1: 3, 2: 3, 4: 3}), journal)
    assert journal.snapshot().get('read_margin_released', 0) == before.get('read_margin_released', 0)


def test_failure_after_invite_drop_rolls_back_entire_schema_and_history(v7):
    db, _, journal = v7
    original, prior_schema = history(db), schema(db)

    class Injected(SQLiteD1):
        async def batch(self, statements):
            if any(item.sql == UPGRADE_V8_SQL[2] for item in statements):
                statements[3].sql = 'INSERT INTO d1_command_guard(ok) VALUES(0)'
            return await super().batch(statements)

    with pytest.raises(BudgetError, match='D1_OUTCOME_UNKNOWN'):
        run(Injected(db, journal), journal)
    assert history(db) == original and schema(db) == prior_schema
    assert not list(db.execute('PRAGMA foreign_key_check'))


@pytest.mark.parametrize('change', ['stopped', 'schema6_incomplete', 'schema7_incomplete',
    'cutover_incomplete', 'schema8_incomplete', 'unverified', 'wrong_fingerprint', 'unknown_table'])
def test_unsafe_prior_state_never_dispatches(v7, change):
    db, _, journal = v7
    state = journal.snapshot()
    if change == 'stopped':
        state['stopped'] = 'MANUAL_STOP'
    elif change == 'schema6_incomplete':
        state['schema_upgrade_v6']['complete'] = False
    elif change == 'schema7_incomplete':
        state['schema_upgrade_v7']['complete'] = False
    elif change == 'cutover_incomplete':
        state['state_record_migration']['complete'] = False
    elif change == 'schema8_incomplete':
        state['schema_upgrade_v8'] = {'from': 7, 'to': 8, 'complete': False}
    elif change == 'unverified':
        state['product_data_verified'] = False
    elif change == 'wrong_fingerprint':
        state['schema_fingerprint'] = 'unknown'
    else:
        state['product_data']['rows']['unknown'] = 0
    journal._save(state)
    raw = SQLiteD1(db, journal)
    with pytest.raises(BudgetError):
        run(raw, journal)
    assert raw.calls == 0 and journal.snapshot() == state


def test_schema_drift_stops_before_any_mutation(v7):
    db, _, journal = v7
    db.execute('ALTER TABLE expenses ADD COLUMN unreviewed TEXT')
    db.commit()
    raw = SQLiteD1(db, journal)
    with pytest.raises(BudgetError, match='PREVIEW_SCHEMA_MISMATCH'):
        run(raw, journal)
    assert raw.calls == 1
    assert not db.execute("SELECT 1 FROM sqlite_schema WHERE name='workspace_join_requests'").fetchone()


def test_closed_plan_covers_maximum_cardinality_and_rejects_forged_operations(v7):
    _, _, journal = v7
    state = journal.snapshot()
    state['product_data']['rows'] = {table: 0 if table == 'd1_command_guard' else 1_000_000
                                    for table in V7_INDEX_COUNTS}
    plan = _upgrade_v8_plan(state)
    assert 100_000 < plan.rows_read < 9007199254740991
    assert 1000 < plan.rows_written < 9007199254740991
    assert len(plan.operations[2].sql) == 17
    journal._save(state)
    with pytest.raises(BudgetError, match='SCHEMA_V8_UPGRADE_UNREVIEWED'):
        begin_product_schema_upgrade_v8(journal, None, now=10)
    assert journal.snapshot() == state


def test_unassigned_invitation_and_review_constraints_preserve_targeted_rows(v7):
    db, _, journal = v7
    run(SQLiteD1(db, journal), journal)
    db.execute("INSERT INTO workspace_invites(id,workspace_id,token_hash,role,expires_at,"
               "created_by_user_id,created_by_revision,created_at,updated_at) "
               "VALUES('new-link','owner',?,'viewer',1000,'owner',7,'new','new')", ('a' * 64,))
    db.execute("INSERT INTO workspace_join_requests(id,workspace_id,invite_id,applicant_user_id,created_at,updated_at) "
               "VALUES('request','owner','new-link','new-user','new','new')")
    db.execute("INSERT INTO workspace_events VALUES('new-event','owner','new-user','request_join','request',NULL,'{}','new')")
    db.commit()
    assert db.execute("SELECT github_recipient_id,github_login FROM workspace_invites WHERE id='new-link'").fetchone()[:] == (None, None)
    for sql, params in (
        ("UPDATE workspace_invites SET github_login='wrong' WHERE id='new-link'", ()),
        ("UPDATE workspace_join_requests SET invite_id='missing' WHERE id='request'", ()),
        ("UPDATE workspace_join_requests SET status='approved' WHERE id='request'", ()),
        ("UPDATE workspace_join_requests SET revision=9007199254740992 WHERE id='request'", ()),
        ("DELETE FROM workspace_invites WHERE id='new-link'", ()),
        ("INSERT INTO workspace_join_requests(id,workspace_id,invite_id,applicant_user_id,created_at,updated_at) "
         "VALUES('duplicate','owner','new-link','new-user','new','new')", ()),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql, params)
        db.rollback()
    db.execute("UPDATE workspace_join_requests SET status='approved',revision=revision+1,reviewed_by_user_id='owner',"
               "reviewed_at='later' WHERE id='request'")
    db.commit()
    assert not list(db.execute('PRAGMA foreign_key_check'))


def test_final_empty_schema_matches_all_canonical_migrations_within_batch_cap():
    from pathlib import Path
    from pullwise_server.cloudflare_preview_schema import SCHEMA_SQL
    canonical = sqlite3.connect(':memory:')
    compiled = sqlite3.connect(':memory:')
    try:
        canonical.execute('PRAGMA foreign_keys=ON')
        compiled.execute('PRAGMA foreign_keys=ON')
        directory = Path(__file__).resolve().parents[1] / 'cloudflare/server/migrations'
        for migration in sorted(directory.glob('*.sql')):
            canonical.executescript(migration.read_text())
        for sql in SCHEMA_SQL:
            compiled.execute(sql)
        assert len(SCHEMA_SQL) <= 64
        assert schema(canonical) == schema(compiled) == SCHEMA_OBJECTS
        assert list(canonical.execute('SELECT * FROM app_state ORDER BY name')) == list(
            compiled.execute('SELECT * FROM app_state ORDER BY name'))
        assert not list(compiled.execute('PRAGMA foreign_key_check'))
    finally:
        canonical.close()
        compiled.close()
