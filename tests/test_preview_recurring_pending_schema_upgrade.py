"""One preserving v11-to-v12 upgrade and bounded pending-occurrence storage.

SQLite adapters supply synthetic metadata; native metering has its own probe.
"""
import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from pullwise_server.cloudflare_preview_budget import (
    ProductMeteredD1, STATE_RECORD_INTEGRITY_VERSION, _RECORD_COUNT_SQL,
    _V11_COUNT_SQL, _upgrade_v12_plan, begin_product_schema_upgrade_v12,
    upgrade_product_schema_v11, upgrade_product_schema_v12, _input_bound,
    sql_write_bound,
)
from pullwise_server.cloudflare_preview_schema import (
    INDEX_COUNTS, MIGRATIONS, PRIMARY_KEYS, SCHEMA_FINGERPRINT, SCHEMA_OBJECTS,
    SCHEMA_SQL, SCHEMA_VERSION, UPGRADE_V12_SQL, V11_INDEX_COUNTS,
    V11_SCHEMA_FINGERPRINT, V11_SCHEMA_OBJECTS, V11_SCHEMA_SQL,
)
from pullwise_server.cloudflare_validation_budget import BudgetError, BudgetJournal
from test_d1_validation_budget import LocalSql
from test_preview_schema_upgrade import SQLiteD1
from test_preview_business_erasure_schema_upgrade import facts, objects, seed

ROOT = Path(__file__).resolve().parents[1]
INSERT = """INSERT INTO expense_recurring_pending(rule_id,owner_id,period_key,
    scheduled_on,template_json,failed_code,rule_revision,created_at,recipient_user_id)
    VALUES(?,?,?,?,?,?,?,?,?)"""
TEMPLATE = json.dumps({"target_kind":"project","project_id":"prj_owner",
    "category_id":"cat_owner","amount_minor":1234,"currency":"USD","purpose":"Hosting"},
    ensure_ascii=False,separators=(",",":"))


def pending(period="2026-11", *, rule="rule_owner", owner="owner", template=TEMPLATE):
    return (rule,owner,period,"2026-11-09",template,"RECORD_LIMIT",7,
            "2026-11-09T12:00:00Z",owner)


@pytest.fixture
def old():
    with closing(sqlite3.connect(":memory:")) as db, closing(sqlite3.connect(":memory:")) as storage:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        for sql in V11_SCHEMA_SQL:
            db.execute(sql)
        seed(db)
        journal = BudgetJournal(LocalSql(storage), preview_product=True, product_operations=True)
        saved = journal.snapshot()
        markers = {"schema_upgrade": {"from":4,"to":5,"request":1,"complete":True}}
        markers.update({"schema_upgrade_v"+str(n):{"from":n-1,"to":n,"request":n,"complete":True}
                        for n in range(6,12)})
        saved.update(schema_ready=True,schema_version=11,schema_fingerprint=V11_SCHEMA_FINGERPRINT,
            product_data={"rows":dict(db.execute(_V11_COUNT_SQL).fetchone()),"json":{},"arrays":0,
                          "records":dict(db.execute(_RECORD_COUNT_SQL).fetchone())},
            product_data_verified=True,state_storage_version=1,
            state_record_migration={"version":1,"request":50,"complete":True,"copied_records":2},
            state_record_integrity_version=STATE_RECORD_INTEGRITY_VERSION,state_record_integrity_request=71,
            requests=89,cases={"product-schema":1,"product":88},reserved_read=120000,
            actual_read=500,reserved_written=2000,actual_written=300,
            evidence=[{"request":1,"operation":1,"rows_read":500,"rows_written":300}],**markers)
        journal._save(saved)
        yield db,storage,journal


def upgrade(raw,journal):
    asyncio.run(upgrade_product_schema_v12(raw,journal,clock=lambda:12))


def test_upgrade_retains_every_historical_fact_and_same_journal_restarts_once(old):
    db,storage,journal=old
    columns={table:[row[1] for row in db.execute("PRAGMA table_info("+table+")")]
             for table in V11_INDEX_COUNTS}
    original,before=facts(db,columns),journal.snapshot()
    raw=SQLiteD1(db,journal,attempts=None)
    asyncio.run(upgrade_product_schema_v11(raw,journal,clock=lambda:12))
    assert raw.calls==0 and journal.snapshot()==before
    plan=_upgrade_v12_plan(before)
    upgrade(raw,journal)
    after=journal.snapshot()
    assert objects(db)==SCHEMA_OBJECTS and facts(db,columns)==original
    assert after["schema_version"]==12 and after["schema_fingerprint"]==SCHEMA_FINGERPRINT
    assert after["product_data"]["rows"]=={**before["product_data"]["rows"],"expense_recurring_pending":0}
    assert after["product_data"]["records"]==before["product_data"]["records"]
    for key in ("scope","state_storage_version","state_record_migration","state_record_integrity_version",
                "state_record_integrity_request", "schema_upgrade", *("schema_upgrade_v"+str(n) for n in range(6,12))):
        assert after[key]==before[key]
    assert after["requests"]==before["requests"]+1
    assert after["cases"]=={**before["cases"],"product":89,"product-schema-v11-to-v12":1}
    assert after["reserved_written"]==before["reserved_written"]+plan.rows_written
    assert after["reserved_read"]==before["reserved_read"]+plan.rows_read
    assert after["evidence"][:len(before["evidence"])]==before["evidence"]
    assert after["schema_upgrade_v12"]["complete"] is True
    assert after["schema_upgrade_v12"]["write_execution"]["native_attempts"]==[None]*6
    assert after["active"] is None and after["stopped"] is None and raw.calls==4
    restarted=BudgetJournal(LocalSql(storage),preview_product=True,product_operations=True)
    upgrade(raw,restarted)
    assert raw.calls==4 and restarted.snapshot()==after


@pytest.mark.parametrize("change",["fingerprint","unverified","incomplete_v10","incomplete_v11",
    "used","guard","unknown_table","legacy_storage","stopped"])
def test_unreviewed_upgrade_never_dispatches(change,old):
    db,_,journal=old
    saved=journal.snapshot()
    if change=="fingerprint": saved["schema_fingerprint"]="unreviewed"
    elif change=="unverified": saved["product_data_verified"]=False
    elif change.startswith("incomplete_"): saved["schema_upgrade_"+change.removeprefix("incomplete_")]={"complete":False}
    elif change=="used": saved["cases"]["product-schema-v11-to-v12"]=1
    elif change=="guard": saved["product_data"]["rows"]["d1_command_guard"]=1
    elif change=="unknown_table": saved["product_data"]["rows"]["unreviewed"]=0
    elif change=="legacy_storage": saved["state_storage_version"]=0
    elif change=="stopped": saved["stopped"]="D1_OUTCOME_UNKNOWN"
    journal._save(saved)
    raw=SQLiteD1(db,journal)
    with pytest.raises(BudgetError): upgrade(raw,journal)
    assert raw.calls==0 and journal.snapshot()==saved


@pytest.mark.parametrize("call,failure",[(1,"fail_call"),(2,"missing_meta"),(3,"fail_call"),(4,"missing_meta")])
def test_unknown_upgrade_never_replays_or_releases_reservation(old,call,failure):
    db,storage,journal=old
    before=journal.snapshot()
    raw=SQLiteD1(db,journal,**{failure:call})
    with pytest.raises(BudgetError): upgrade(raw,journal)
    stopped=journal.snapshot()
    assert stopped["schema_version"]==11 and stopped["schema_upgrade_v12"]["complete"] is False
    assert stopped["reserved_written"]==before["reserved_written"]+_upgrade_v12_plan(before).rows_written
    restarted=BudgetJournal(LocalSql(storage),preview_product=True,product_operations=True)
    with pytest.raises(BudgetError): upgrade(raw,restarted)
    assert raw.calls==call and restarted.snapshot()==stopped


@pytest.mark.parametrize("call,attempts",[(1,0),(2,4),(3,2),(3,True),(4,"1")])
def test_unproven_native_attempts_cannot_publish_schema(old,call,attempts):
    db,_,journal=old
    with pytest.raises(BudgetError,match="MIGRATION_ATTEMPTS_UNPROVEN"):
        upgrade(SQLiteD1(db,journal,attempts_by_call={call:attempts}),journal)
    assert journal.snapshot()["schema_version"]==11


def test_current_incomplete_marker_does_not_silently_noop(old):
    db,_,journal=old
    saved=journal.snapshot()
    saved.update(schema_version=12,schema_fingerprint=SCHEMA_FINGERPRINT,schema_upgrade_v12={"complete":False})
    journal._save(saved)
    raw=SQLiteD1(db,journal)
    with pytest.raises(BudgetError,match="SCHEMA_V12_UPGRADE_UNREVIEWED"): upgrade(raw,journal)
    assert raw.calls==0 and journal.snapshot()==saved


def test_closed_linear_upgrade_bounds_include_catalogs_fk_proofs_and_all_attempts(old):
    _,_,journal=old
    state=journal.snapshot(); plan=_upgrade_v12_plan(state)
    assert plan.operations[2].sql==UPGRADE_V12_SQL and plan.rows_written==256
    assert plan.operations[2].rows_read==384*6+32*sum(state["product_data"]["rows"].values())
    high=journal.snapshot()
    high["product_data"]["rows"]={table:0 if table=="d1_command_guard" else 1000000 for table in V11_INDEX_COUNTS}
    assert plan.rows_read<_upgrade_v12_plan(high).rows_read<9007199254740991
    with pytest.raises(BudgetError,match="SCHEMA_V12_UPGRADE_UNREVIEWED"):
        begin_product_schema_upgrade_v12(journal,None,now=12)
    assert journal.snapshot()==state


def test_pending_cap_is_per_rule_indexed_and_cannot_be_bypassed_by_reparenting(old):
    db,_,journal=old
    upgrade(SQLiteD1(db,journal),journal)
    for number in range(10): db.execute(INSERT,pending(str(number)))
    assert db.execute("SELECT COUNT(*) FROM expense_recurring_pending").fetchone()[0]==10
    with pytest.raises(sqlite3.IntegrityError,match="recurring_pending_limit"):
        db.execute(INSERT,pending("overflow"))
    db.execute(INSERT,pending("other",rule="rule_other",owner="other"))
    with pytest.raises(sqlite3.IntegrityError,match="recurring_pending_limit"):
        db.execute("UPDATE expense_recurring_pending SET rule_id='rule_owner',owner_id='owner' WHERE rule_id='rule_other'")
    details=[row[3] for row in db.execute("EXPLAIN QUERY PLAN SELECT COUNT(*) FROM expense_recurring_pending WHERE rule_id=?",("rule_owner",))]
    assert any("sqlite_autoindex_expense_recurring_pending_1 (rule_id=?)" in row for row in details)
    db.execute("DELETE FROM expense_recurring_pending WHERE rule_id='rule_owner' AND period_key='0'")
    db.execute(INSERT,pending("replacement"))
    assert db.execute("SELECT COUNT(*) FROM expense_recurring_pending WHERE rule_id='rule_owner'").fetchone()[0]==10


@pytest.mark.parametrize("column,value",[("period_key",""),("period_key","x"*17),("scheduled_on","2026/11/09"),
    ("template_json","[]"),("template_json","{bad"),("template_json",json.dumps({"note":"x"*8192})),
    ("rule_revision",0),("rule_revision",9007199254740992),("recipient_user_id",""),
    ("notification_state","sent"),("owner_id","other")])
def test_pending_constraints_keep_frozen_occurrence_and_owner_scope(old,column,value):
    db,_,journal=old
    upgrade(SQLiteD1(db,journal),journal)
    db.execute(INSERT,pending())
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE expense_recurring_pending SET "+column+"=? WHERE rule_id='rule_owner' AND period_key='2026-11'",(value,))
    assert not list(db.execute("PRAGMA foreign_key_check"))


def test_product_meter_accepts_pending_scalar_mutations_and_tracks_rows_indexes(old):
    db,_,journal=old
    raw=SQLiteD1(db,journal)
    upgrade(raw,journal)
    ticket=journal.begin_product(now=13)
    meter=ProductMeteredD1(raw,journal,ticket,clock=lambda:14)
    asyncio.run(meter.ensure_cardinality())
    assert raw.calls==4
    before=journal.snapshot()["reserved_written"]
    asyncio.run(meter.batch([meter.prepare(INSERT).bind(*pending())]))
    assert journal.snapshot()["product_data"]["rows"]["expense_recurring_pending"]==1
    assert journal.snapshot()["reserved_written"]==before+3
    asyncio.run(meter.batch([meter.prepare("UPDATE expense_recurring_pending SET notification_state=? WHERE rule_id=? AND period_key=?").bind("inbox","rule_owner","2026-11")]))
    assert journal.snapshot()["reserved_written"]==before+8
    asyncio.run(meter.batch([meter.prepare("DELETE FROM expense_recurring_pending WHERE rule_id=? AND period_key=?").bind("rule_owner","2026-11")]))
    assert journal.snapshot()["product_data"]["rows"]["expense_recurring_pending"]==0
    assert journal.snapshot()["reserved_written"]==before+11
    assert journal.snapshot()["stopped"] is None
    journal.finish(ticket,now=15)


def test_pending_input_bound_and_unique_fences_include_both_indexes():
    assert PRIMARY_KEYS["expense_recurring_pending"]==["rule_id","period_key"]
    assert INDEX_COUNTS["expense_recurring_pending"]==2
    assert sql_write_bound(INSERT)==3
    assert sql_write_bound("UPDATE expense_recurring_pending SET notification_state=? WHERE rule_id=? AND period_key=?")==5
    assert sql_write_bound("DELETE FROM expense_recurring_pending WHERE rule_id=? AND period_key=?")==3
    for sql in ("DELETE FROM expense_recurring_pending WHERE rule_id=?",
                "UPDATE expense_recurring_pending SET notification_state=? WHERE period_key=?"):
        with pytest.raises(ValueError,match="unique key"): sql_write_bound(sql)
    with pytest.raises(ValueError): _input_bound(pending(template=json.dumps({"note":"x"*8192})),sql=INSERT)


def test_fresh_v12_canonical_migrations_fingerprint_and_historical_v11_agree():
    paths=sorted((ROOT/"cloudflare/server/migrations").glob("*.sql"))
    assert paths[-1].name=="0012_recurring_pending.sql" and SCHEMA_VERSION==12
    assert len(SCHEMA_SQL)==54<=64 and len(UPGRADE_V12_SQL)==6
    assert {item["name"]:item["sha256"] for item in MIGRATIONS}=={path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    with closing(sqlite3.connect(":memory:")) as canonical, closing(sqlite3.connect(":memory:")) as compiled:
        for path in paths: canonical.executescript(path.read_text())
        for sql in SCHEMA_SQL: compiled.execute(sql)
        assert objects(canonical)==objects(compiled)==SCHEMA_OBJECTS
    assert hashlib.sha256(json.dumps(SCHEMA_OBJECTS,separators=(",",":")).encode()).hexdigest()==SCHEMA_FINGERPRINT
    old={(kind,name):(table,sql) for kind,name,table,sql in V11_SCHEMA_OBJECTS}
    current={(kind,name):(table,sql) for kind,name,table,sql in SCHEMA_OBJECTS}
    assert all(current[key]==value for key,value in old.items())
    assert INDEX_COUNTS=={**V11_INDEX_COUNTS,"expense_recurring_pending":2}
