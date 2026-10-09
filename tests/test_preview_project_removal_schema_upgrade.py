"""Populated v9-to-v10 preservation; native D1 costs are a separate proof."""
import asyncio
import ast
import json
import re
import sqlite3
from pathlib import Path

import pytest

from test_d1_validation_budget import LocalSql
from test_preview_activity_schema_upgrade import v8
from test_preview_invite_approval_schema_upgrade import v7
from test_preview_recurring_schema_upgrade import v6, schema
from test_preview_blank_schema_upgrade import v5
from test_preview_schema_upgrade import SQLiteD1
from pullwise_server.cloudflare_preview_schema import (
    V10_SCHEMA_VERSION as SCHEMA_VERSION, V10_SCHEMA_FINGERPRINT as SCHEMA_FINGERPRINT,
    V10_SCHEMA_OBJECTS as SCHEMA_OBJECTS, V10_SCHEMA_SQL as SCHEMA_SQL,
    V10_INDEX_COUNTS as INDEX_COUNTS,
    V9_SCHEMA_VERSION, V9_SCHEMA_FINGERPRINT, V9_SCHEMA_OBJECTS, V9_INDEX_COUNTS,
    UPGRADE_V10_SQL,
)
from pullwise_server.cloudflare_preview_budget import (
    upgrade_product_schema, upgrade_product_schema_v6, upgrade_product_schema_v7,
    upgrade_product_schema_v8, upgrade_product_schema_v9, upgrade_product_schema_v10,
    upgrade_product_schema_v11,
    migrate_product_state_records, _upgrade_v10_plan,
    begin_product_schema_upgrade_v10, _V9_COUNT_SQL, ProductMeteredD1,
)
from pullwise_server.cloudflare_validation_budget import BudgetJournal, BudgetError


@pytest.fixture
def v9(v8):
    db, storage, journal = v8
    asyncio.run(upgrade_product_schema_v9(SQLiteD1(db, journal), journal, clock=lambda: 11))
    assert journal.snapshot()['schema_version'] == V9_SCHEMA_VERSION == 9
    assert schema(db) == V9_SCHEMA_OBJECTS
    db.execute("UPDATE ledger_projects SET development_url=?,product_url=? WHERE id='prj_old'",
               ('https://dev.example.invalid/历史', 'https://example.invalid/product'))
    db.execute("INSERT INTO ledger_projects(id,owner_id,name,created_at,updated_at) "
               "VALUES('prj_standalone','owner','Standalone 项目','old','old')")
    db.execute("INSERT INTO expense_recurring_rules(id,owner_id,actor_user_id,target_kind,project_id,"
               "template_json,schedule_json,status,revision,next_run_at,next_occurrence_on,next_period_key,"
               "created_at,updated_at,create_key,create_sha256,create_response_json) "
               "VALUES('rule_retained','owner','owner','project','prj_old',?,?,'blocked',7,"
               "NULL,NULL,NULL,'old','old','retained-key',?,'{\"id\":\"rule_retained\",\"revision\":7}')",
               (json.dumps({'note': '周期历史🙂', 'amountMinor': 1250}, ensure_ascii=False),
                '{"frequency":"monthly","timezone":"Asia/Shanghai","anchorDay":31}', 'a' * 64))
    db.execute("INSERT INTO expense_recurring_occurrences VALUES('rule_retained','owner',"
               "'2026-10','2026-10-07','exp_old',6,'old')")
    actor = json.dumps({'kind': 'session', 'userId': 'owner', 'name': '旧记录🙂'}, ensure_ascii=False)
    for identity, resource, resource_id in (('activity_project', 'project', 'prj_old'),
                                           ('activity_expense', 'expense', 'exp_old'),
                                           ('activity_rule', 'recurring_rule', 'rule_retained')):
        db.execute("INSERT INTO ledger_activity_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            identity, 'operation-' + identity, 'owner', 'project', 'prj_old', actor,
            resource, resource_id, 'update', '{"status":"before","revision":6}',
            '{"status":"after","revision":7,"retained":"历史🙂"}', '2026-10-09T12:00:00Z'))
    db.commit()
    saved = journal.snapshot()
    saved['product_data']['rows'] = dict(db.execute(_V9_COUNT_SQL).fetchone())
    journal._save(saved)
    return db, storage, journal


def facts(db):
    """Compare every historical column; the added nullable column is separate."""
    values = {}
    for table in V9_INDEX_COUNTS:
        columns = [row[1] for row in db.execute('PRAGMA table_info(' + table + ')')]
        if table == 'ledger_projects':
            columns = [column for column in columns if column != 'deleted_at']
        values[table] = sorted((tuple(row) for row in db.execute(
            'SELECT ' + ','.join(columns) + ' FROM ' + table)), key=repr)
    return values


def run(raw, journal):
    asyncio.run(upgrade_product_schema_v10(raw, journal, clock=lambda: 12))


async def completed_paths(raw, journal, *, include_current=False):
    paths = (upgrade_product_schema, upgrade_product_schema_v6, upgrade_product_schema_v7,
             upgrade_product_schema_v8, upgrade_product_schema_v9, migrate_product_state_records)
    if include_current:
        paths += (upgrade_product_schema_v10,)
    for migration in paths:
        await migration(raw, journal, clock=lambda: 12)


def test_upgrade_preserves_every_history_marker_and_cumulative_budget_across_restart(v9):
    db, storage, journal = v9
    original, before = facts(db), journal.snapshot()
    raw = SQLiteD1(db, journal, attempts=None)
    asyncio.run(completed_paths(raw, journal))
    assert raw.calls == 0 and journal.snapshot() == before
    run(raw, journal)
    after = journal.snapshot()
    assert SCHEMA_VERSION == after['schema_version'] == 10
    assert after['schema_fingerprint'] == SCHEMA_FINGERPRINT
    assert schema(db) == SCHEMA_OBJECTS and facts(db) == original
    assert db.execute("SELECT name,github_repo_id,deleted_at FROM ledger_projects WHERE id='prj_old'").fetchone()[:] == ('', 202, None)
    assert not db.execute('SELECT 1 FROM ledger_projects WHERE deleted_at IS NOT NULL').fetchall()
    assert after['product_data']['rows'] == before['product_data']['rows']
    for key in ('scope', 'schema_upgrade', 'schema_upgrade_v6', 'schema_upgrade_v7',
                'schema_upgrade_v8', 'schema_upgrade_v9', 'state_record_migration',
                'state_storage_version', 'state_record_integrity_version', 'state_record_integrity_request'):
        assert after[key] == before[key]
    assert after['requests'] == before['requests'] + 1
    assert after['cases'] == {**before['cases'], 'product': before['cases']['product'] + 1,
                             'product-schema-v9-to-v10': 1}
    assert after['schema_upgrade_v10']['complete'] is True
    assert after['schema_upgrade_v10']['write_execution'] == {
        'native_attempts': [None] * len(UPGRADE_V10_SQL),
        'provenance': 'd1-atomic-write-batch-with-fk-pragma-v1'}
    assert after['evidence'][:len(before['evidence'])] == before['evidence']
    for key in ('reserved_read', 'reserved_written', 'actual_read', 'actual_written'):
        assert after[key] >= before[key]
    assert after['reserved_written'] > before['reserved_written']
    assert after.get('read_margin_released', 0) == before.get('read_margin_released', 0)
    assert after['active'] is None and after['stopped'] is None
    assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert not list(db.execute('PRAGMA foreign_key_check'))
    assert raw.calls == 4 and len(set(raw.reservations)) == 1
    restarted = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
    asyncio.run(completed_paths(raw, restarted, include_current=True))
    assert raw.calls == 4 and restarted.snapshot() == after
    asyncio.run(upgrade_product_schema_v11(raw, restarted, clock=lambda: 12))
    calls = raw.calls
    ticket = restarted.begin_product(now=13)
    meter = ProductMeteredD1(raw, restarted, ticket, clock=lambda: 14)
    asyncio.run(meter.ensure_cardinality())
    assert raw.calls == calls and meter.records_integrity_verified is True
    restarted.finish(ticket, now=15)


@pytest.mark.parametrize('change', ['stopped', 'unverified', 'fingerprint', 'v6_incomplete',
                                    'v7_incomplete', 'v8_incomplete', 'v9_incomplete',
                                    'v10_incomplete', 'cutover_incomplete', 'unknown_table'])
def test_incomplete_or_unreviewed_state_never_dispatches(v9, change):
    db, _, journal = v9
    saved = journal.snapshot()
    if change == 'stopped':
        saved['stopped'] = 'D1_OUTCOME_UNKNOWN'
    elif change == 'unverified':
        saved['product_data_verified'] = False
    elif change == 'fingerprint':
        saved['schema_fingerprint'] = 'unreviewed'
    elif change == 'cutover_incomplete':
        saved['state_record_migration']['complete'] = False
    elif change == 'unknown_table':
        saved['product_data']['rows']['unreviewed_table'] = 0
    else:
        saved['schema_upgrade_' + change.split('_')[0]] = {'complete': False}
    journal._save(saved)
    raw = SQLiteD1(db, journal)
    with pytest.raises(BudgetError):
        run(raw, journal)
    assert raw.calls == 0 and journal.snapshot() == saved


def test_native_failure_after_parent_index_rebuild_rolls_back_all_facts_and_never_retries(v9):
    db, storage, journal = v9
    original, original_schema = facts(db), schema(db)

    class Failure(SQLiteD1):
        async def batch(self, statements):
            statements = list(statements)
            if tuple(item.sql for item in statements) == UPGRADE_V10_SQL:
                position = next(i for i, item in enumerate(statements)
                                if 'CREATE INDEX ledger_projects_owner_status ON ' in item.sql)
                statements.insert(position + 1, self.prepare('INSERT INTO d1_command_guard(ok) VALUES(0)').bind())
            return await super().batch(statements)

    raw = Failure(db, journal)
    before = journal.snapshot()
    with pytest.raises(BudgetError, match='D1_OUTCOME_UNKNOWN'):
        run(raw, journal)
    stopped = journal.snapshot()
    assert raw.calls == 3
    assert stopped['schema_version'] == 9 and stopped['schema_upgrade_v10']['complete'] is False
    assert stopped['stopped'] == 'D1_OUTCOME_UNKNOWN'
    assert stopped['reserved_written'] > before['reserved_written']
    assert facts(db) == original and schema(db) == original_schema
    assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert not list(db.execute('PRAGMA foreign_key_check'))
    restarted = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
    with pytest.raises(BudgetError, match='D1_OUTCOME_UNKNOWN'):
        run(raw, restarted)
    assert raw.calls == 3 and restarted.snapshot() == stopped


@pytest.mark.parametrize('call', [1, 2, 3, 4])
@pytest.mark.parametrize('failure,code', [('fail_call', 'D1_OUTCOME_UNKNOWN'),
                                         ('missing_meta', 'METERING_MISSING')])
def test_unknown_outcome_or_missing_metadata_retains_reservation_and_never_replays(v9, call, failure, code):
    db, storage, journal = v9
    raw = SQLiteD1(db, journal, **{failure: call})
    before = journal.snapshot()
    with pytest.raises(BudgetError, match=code):
        run(raw, journal)
    stopped = journal.snapshot()
    assert stopped['schema_version'] == 9 and stopped['schema_upgrade_v10']['complete'] is False
    assert stopped['reserved_written'] > before['reserved_written']
    restarted = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
    with pytest.raises(BudgetError, match=code):
        run(raw, restarted)
    assert raw.calls == call and restarted.snapshot() == stopped


@pytest.mark.parametrize('call,attempts', [(1, 0), (2, 4), (3, 2), (3, True), (3, '1'), (4, 1.0)])
def test_unproven_attempts_never_publish_new_version(v9, call, attempts):
    db, _, journal = v9
    with pytest.raises(BudgetError, match='MIGRATION_ATTEMPTS_UNPROVEN'):
        run(SQLiteD1(db, journal, attempts_by_call={call: attempts}), journal)
    assert journal.snapshot()['schema_version'] == 9
    assert journal.snapshot()['schema_upgrade_v10']['complete'] is False


def test_closed_plan_reserves_parent_rebuild_and_rejects_forged_admission(v9):
    _, _, journal = v9
    saved = journal.snapshot()
    plan = _upgrade_v10_plan(saved)
    assert len(plan.operations) == 4
    assert plan.operations[2].sql == UPGRADE_V10_SQL and len(UPGRADE_V10_SQL) == 9
    rows = saved['product_data']['rows']
    children = rows['expenses'] + rows['ledger_project_repositories'] + rows['expense_recurring_rules']
    assert plan.operations[2].rows_read == (384 * 9 + 16 * sum(rows.values())
                                          + 32 * (rows['ledger_projects'] + 1) * (1 + children))
    assert plan.rows_written == 256 + 10 * rows['ledger_projects']
    high = journal.snapshot()
    high['product_data']['rows'] = {table: 0 if table == 'd1_command_guard' else 1_000_000
                                    for table in V9_INDEX_COUNTS}
    upper = _upgrade_v10_plan(high)
    assert plan.rows_read < upper.rows_read < 9007199254740991
    assert plan.rows_written < upper.rows_written < 9007199254740991
    with pytest.raises(BudgetError, match='SCHEMA_V10_UPGRADE_UNREVIEWED'):
        begin_product_schema_upgrade_v10(journal, None, now=12)
    assert journal.snapshot() == saved


def test_tombstone_constraints_preserve_legacy_empty_name_and_financial_foreign_keys(v9):
    db, _, journal = v9
    run(SQLiteD1(db, journal), journal)
    original = facts(db)
    timestamp = '2026-10-09T12:00:00Z'
    invalid = [
        ("UPDATE ledger_projects SET deleted_at=? WHERE id='prj_old'", (timestamp,)),
        ("UPDATE ledger_projects SET deleted_at=?,status='active',github_repo_id=NULL,"
         "github_full_name=NULL,github_organization_id=NULL WHERE id='prj_old'", (timestamp,)),
        ("UPDATE ledger_projects SET github_repo_id=NULL,github_full_name=NULL,github_organization_id=NULL "
         "WHERE id='prj_old'", ()),
    ]
    for malformed in ('', '2026-10-09', '2026-10-09T12:00:00', '2026-10-09T12:00:00+00:00',
                      '2026-10-09T12:00:00.0Z', '2026-10-09t12:00:00Z', '2026-10-09T12:00:00z',
                      '２０２６-10-09T12:00:00Z', '2026-1a-09T12:00:00Z', 'x' * 20):
        invalid.append(("UPDATE ledger_projects SET deleted_at=?,status='archived',github_repo_id=NULL,"
                        "github_full_name=NULL,github_organization_id=NULL WHERE id='prj_old'", (malformed,)))
    for sql, values in invalid:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql, values)
        db.rollback()
        assert facts(db) == original
    db.execute("UPDATE ledger_projects SET deleted_at=?,status='archived',github_repo_id=NULL,"
               "github_full_name=NULL,github_organization_id=NULL WHERE id='prj_old'", (timestamp,))
    db.commit()
    assert db.execute("SELECT name,status,deleted_at FROM ledger_projects WHERE id='prj_old'").fetchone()[:] == ('', 'archived', timestamp)
    for table in V9_INDEX_COUNTS:
        if table != 'ledger_projects':
            assert facts(db)[table] == original[table]
    for sql in ("DELETE FROM ledger_projects WHERE id='prj_old'",
                "UPDATE ledger_projects SET deleted_at=NULL WHERE id='prj_old'",
                "UPDATE ledger_projects SET status='active' WHERE id='prj_old'",
                "UPDATE ledger_projects SET github_repo_id=909 WHERE id='prj_old'",
                "UPDATE ledger_projects SET github_full_name='org/repo' WHERE id='prj_old'",
                "UPDATE ledger_projects SET github_organization_id=77 WHERE id='prj_old'"):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql)
        db.rollback()
    assert not list(db.execute('PRAGMA foreign_key_check'))
    assert [row[2] for row in db.execute('PRAGMA index_info(ledger_projects_owner_status)')] == ['owner_id', 'deleted_at', 'status']
    details = [row[3] for row in db.execute('EXPLAIN QUERY PLAN SELECT id FROM ledger_projects '
               'WHERE owner_id=? AND deleted_at IS NULL AND status=?', ('owner', 'active'))]
    assert any('SEARCH' in detail and 'ledger_projects_owner_status' in detail for detail in details)


def test_fresh_schema_matches_canonical_v10_migration_without_new_tables_or_indexes():
    migrations = sorted(path for path in
        (Path(__file__).resolve().parents[1] / 'cloudflare/server/migrations').glob('*.sql')
        if int(path.name[:4]) <= 10)
    assert migrations[-1].name == '0010_project_removal.sql'
    with sqlite3.connect(':memory:') as canonical, sqlite3.connect(':memory:') as compiled:
        for migration in migrations:
            canonical.executescript(migration.read_text())
        for sql in SCHEMA_SQL:
            compiled.execute(sql)
        assert schema(canonical) == schema(compiled) == SCHEMA_OBJECTS
        assert len(SCHEMA_SQL) == 41 <= 64
        assert INDEX_COUNTS == V9_INDEX_COUNTS and len(INDEX_COUNTS) == 22
        assert INDEX_COUNTS['ledger_projects'] == 4 and sum(INDEX_COUNTS.values()) == 49
        assert [row[2] for row in compiled.execute('PRAGMA index_info(ledger_projects_owner_status)')] == ['owner_id', 'deleted_at', 'status']
        assert V9_SCHEMA_FINGERPRINT != SCHEMA_FINGERPRINT
        # Native D1 has a lower LIKE/GLOB pattern limit than desktop SQLite.
        # The full 76-byte ISO timestamp pattern previously failed on removal;
        # date/time parts retain all format checks within the provider limit.
        project_sql = next(sql for kind, name, table, sql in SCHEMA_OBJECTS
                           if kind == 'table' and name == 'ledger_projects')
        patterns = re.findall(r"\bGLOB\s+'([^']*)'", project_sql, re.I)
        assert [len(pattern.encode()) for pattern in patterns] == [42, 32]


def test_native_fixture_seed_is_valid_v9_and_imports_frozen_v10_target():
    """Validate fixture inputs locally without starting or simulating Workerd."""
    script = Path(__file__).resolve().parents[1] / 'scripts/check-project-removal-schema-native.py'
    driver = ast.parse(script.read_text())
    entry = next(ast.literal_eval(node.value) for node in driver.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == 'ENTRY' for target in node.targets))
    parsed = ast.parse(entry)
    allowed = []
    for node in parsed.body:
        if isinstance(node, ast.Import) and all(alias.name in ('hashlib', 'json') for alias in node.names):
            allowed.append(node)
        elif isinstance(node, ast.ImportFrom) and node.module in (
                'pullwise_server.cloudflare_preview_schema', 'pullwise_server.cloudflare_state_records'):
            allowed.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(target, ast.Tuple) and any(
                isinstance(item, ast.Name) and item.id == 'OWNER' for item in target.elts)
                for target in node.targets):
            allowed.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == 'seed_commands':
            allowed.append(node)
    namespace = {}
    exec(compile(ast.Module(body=allowed, type_ignores=[]), str(script), 'exec'), namespace)
    assert namespace['SCHEMA_VERSION'] == 10 and namespace['SCHEMA_SQL'] == SCHEMA_SQL
    assert namespace['SCHEMA_OBJECTS'] == SCHEMA_OBJECTS
    assert namespace['SCHEMA_FINGERPRINT'] == SCHEMA_FINGERPRINT
    assert namespace['V9_SCHEMA_OBJECTS'] == V9_SCHEMA_OBJECTS
    with sqlite3.connect(':memory:') as db:
        db.execute('PRAGMA foreign_keys=ON')
        for sql in namespace['V9_SCHEMA_SQL']:
            db.execute(sql)
        for sql, values in namespace['seed_commands']():
            db.execute(sql, values)
        assert schema(db) == V9_SCHEMA_OBJECTS
        assert db.execute("SELECT name,github_repo_id FROM ledger_projects WHERE id='prj_other'").fetchone() == ('', 303)
        assert db.execute('SELECT COUNT(*) FROM ledger_activity_events WHERE before_json IS NOT NULL AND after_json IS NOT NULL').fetchone()[0] == 4
        assert db.execute('SELECT COUNT(*) FROM expense_recurring_occurrences').fetchone()[0] == 2
        assert not list(db.execute('PRAGMA foreign_key_check'))
