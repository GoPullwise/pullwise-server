"""Six finite loopback requests prove the preserving v12 pending-occurrence schema.

Uses isolated local D1 bindings and the pinned native Python runtime. Every
schema change, scalar pending mutation and cap probe is local and synthetic;
raw D1 metadata is retained, never fabricated. No providers or remote work.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "cloudflare/server"
BUNDLE_SHA256 = "3c3fd5a4179230e21e018e28b0e735e7c4abd8fe8e4ff39d2fd6bb2ea3a2e260"
HTTP_PATHS = ("setup", "upgrade", "mutations", "restart", "rollback", "rollback-restart")

ENTRY = r'''
import hashlib
import json
from workers import WorkerEntrypoint, DurableObject, Response
from pullwise_server.cloudflare_native_d1 import NativeD1
from pullwise_server.cloudflare_validation_budget import BudgetJournal, BudgetError, _field
from pullwise_server.cloudflare_preview_budget import (
    ProductMeteredD1, _upgrade_v12_plan, upgrade_product_schema_v11,
    upgrade_product_schema_v12, _COUNT_SQL, _RECORD_COUNT_SQL,
)
from pullwise_server.cloudflare_preview_schema import (
    V11_SCHEMA_SQL,V11_SCHEMA_OBJECTS,V11_SCHEMA_FINGERPRINT,V11_INDEX_COUNTS,
    SCHEMA_SQL,SCHEMA_OBJECTS,SCHEMA_FINGERPRINT,SCHEMA_VERSION,INDEX_COUNTS,UPGRADE_V12_SQL,
)
from pullwise_server.cloudflare_state_records import STATE_KINDS, record_name, encode_record

OWNER="owner"
INSERT="""INSERT INTO expense_recurring_pending(rule_id,owner_id,period_key,scheduled_on,
    template_json,failed_code,rule_revision,created_at,recipient_user_id) VALUES(?,?,?,?,?,?,?,?,?)"""
TEMPLATE=json.dumps({"target_kind":"project","project_id":"prj_owner","category_id":"cat_owner",
    "amount_minor":9007199254740991,"currency":"USD","purpose":"Native frozen 历史🙂",
    "note":"Retained occurrence values"},ensure_ascii=False,separators=(",",":"))
COUNTERS=("requests","reserved_read","reserved_written","actual_read","actual_written")


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()


async def save(storage,key,value):
    await storage.put(key,json.dumps(value,separators=(",",":")))


async def load(storage,key):
    return json.loads(await storage.get(key))


class ObservedStatement:
    def __init__(self,owner,sql,native):
        self.owner,self.sql,self.native=owner,sql,native
    def bind(self,*values):
        return ObservedStatement(self.owner,self.sql,self.native.bind(*values))


class ObservedD1(NativeD1):
    def __init__(self,binding,label,*,inject=False):
        super().__init__(binding)
        self.label,self.inject=label,inject
        self.groups,self.dispatches,self.failed=[],0,[]
    def prepare(self,sql):
        return ObservedStatement(self,sql,super().prepare(sql))
    async def batch(self,statements):
        statements=list(statements)
        assert statements and len(statements)<=64
        assert all(item.owner is self for item in statements)
        native=[item.native for item in statements]
        injected=self.inject and tuple(item.sql for item in statements)==UPGRADE_V12_SQL
        if injected:
            native.insert(-1,super().prepare("INSERT INTO d1_command_guard(ok) VALUES(0)"))
        self.dispatches+=1
        try:
            results=await super().batch(native)
        except BaseException:
            failure={"binding":self.label,"statements":len(native),"injectedLateFailure":injected,
                "nativeResultsUnavailable":True}
            self.failed.append(failure)
            print(json.dumps({"nativePendingSchemaFailure":failure}))
            raise
        values=[{"rowsRead":_field(_field(item,"meta"),"rows_read"),
            "rowsWritten":_field(_field(item,"meta"),"rows_written"),
            "attempts":_field(_field(item,"meta"),"total_attempts")} for item in results]
        assert all(type(item[key]) is int and item[key]>=0 for item in values for key in ("rowsRead","rowsWritten"))
        group={"binding":self.label,"statements":len(values),"rowsRead":sum(item["rowsRead"] for item in values),
            "rowsWritten":sum(item["rowsWritten"] for item in values),
            "nativeAttempts":[item["attempts"] for item in values]}
        self.groups.append(group)
        print(json.dumps({"nativePendingSchemaMeta":group}))
        return results


async def execute(native,commands):
    result=[]
    for start in range(0,len(commands),64):
        result.extend(await native.batch([native.prepare(sql).bind(*values) for sql,values in commands[start:start+64]]))
    return result


def seed():
    commands=[]
    def add(sql,*values): commands.append((sql,values))
    for owner in ("owner","other"):
        add("INSERT INTO expense_categories(id,owner_id,name,created_at,updated_at) VALUES(?,?,?,?,?)",
            "cat_"+owner,owner,"Hosting "+owner,"old","old")
        add("INSERT INTO ledger_projects(id,owner_id,name,created_at,updated_at) VALUES(?,?,?,?,?)",
            "prj_"+owner,owner,"Retained project "+owner,"old","old")
        add("""INSERT INTO expense_recurring_rules(id,owner_id,actor_user_id,target_kind,project_id,
            template_json,schedule_json,status,revision,created_at,updated_at,create_key,create_sha256,
            create_response_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            "rule_"+owner,owner,owner,"project","prj_"+owner,TEMPLATE,'{"frequency":"monthly","timezone":"Asia/Shanghai","day":31}',
            "active",7,"old","old","create-"+owner,"a"*64,'{"revision":7,"history":"周期🙂"}')
        add("""INSERT INTO expenses(id,owner_id,target_kind,project_id,category_id,occurred_on,
            amount_minor,currency,purpose,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            "exp_"+owner,owner,"project","prj_"+owner,"cat_"+owner,"2026-10-09",9007199254740991,
            "USD","Retained finance 历史🙂",3,"old","old")
        add("INSERT INTO expense_recurring_occurrences VALUES(?,?,?,?,?,?,?)",
            "rule_"+owner,owner,"M2026-10","2026-10-09","exp_"+owner,6,"old")
        add("""INSERT INTO expense_events(id,expense_id,owner_id,actor_kind,actor_id,action,after_json,created_at)
            VALUES(?,?,?,?,?,?,?,?)""","evt_"+owner,"exp_"+owner,owner,"schedule","rule_"+owner,"create",'{"history":"Audit🙂"}',"old")
        add("INSERT INTO expense_create_idempotency VALUES(?,?,?,?,?,?)",
            owner,"idem_"+owner,"b"*64,"exp_"+owner,'{"revision":3,"history":"Replay🙂"}',"old")
        add("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",record_name("users",owner),
            encode_record("users",owner,{"id":owner,"jevEnabled":False,"jevPreferenceRevision":8}),1)
    add("INSERT INTO billing_webhook_receipts VALUES(?,?,?,?,?)","retained-billing","c"*64,'{"plan":"max"}',1,"applied")
    add("""INSERT INTO api_keys(id,user_id,name,key_prefix,key_hash,created_at) VALUES(?,?,?,?,?,?)""",
        "retained-key","owner","Retained key","local","d"*64,1)
    return commands


def values(period,rule="rule_owner",owner="owner"):
    return (rule,owner,period,"2026-11-09",TEMPLATE,"RECORD_LIMIT",7,"2026-11-09T12:00:00Z",owner)


async def snapshot(native,*,current=False):
    tables=sorted(INDEX_COUNTS if current else V11_INDEX_COUNTS)
    commands=[("PRAGMA table_info("+table+")",()) for table in tables]
    commands += [("SELECT * FROM "+table,()) for table in tables]
    commands += [("PRAGMA foreign_keys",()),("PRAGMA foreign_key_check",()),
        ("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT GLOB '_cf_*' "
         "AND (name NOT GLOB 'sqlite_*' OR name GLOB 'sqlite_autoindex_*') ORDER BY type,name",())]
    results=await execute(native,commands)
    data={}
    for index,table in enumerate(tables):
        columns=[_field(row,"name") for row in _field(results[index],"results")]
        rows=[[_field(row,column) for column in columns] for row in _field(results[index+len(tables)],"results")]
        rows.sort(key=lambda row:json.dumps(row,ensure_ascii=False,separators=(",",":")))
        data[table]={"rows":len(rows),"sha256":digest({"columns":columns,"rows":rows})}
    assert _field(list(_field(results[-3],"results"))[0],"foreign_keys")==1
    assert not list(_field(results[-2],"results"))
    objects=tuple((_field(row,"type"),_field(row,"name"),_field(row,"tbl_name"),
        " ".join(_field(row,"sql").split()) if _field(row,"sql") else None) for row in _field(results[-1],"results"))
    return {"tables":data,"schemaObjects":objects,"foreignKeyViolations":0,"foreignKeysEnabled":True}


def historical(sql,baseline):
    journal=BudgetJournal(sql,preview_product=True,product_operations=True)
    state=journal.snapshot()
    rows={table:item["rows"] for table,item in baseline["tables"].items()}
    markers={"schema_upgrade":{"from":4,"to":5,"request":1,"complete":True}}
    markers.update({"schema_upgrade_v"+str(n):{"from":n-1,"to":n,"request":n,"complete":True} for n in range(6,12)})
    state.update(schema_ready=True,schema_version=11,schema_fingerprint=V11_SCHEMA_FINGERPRINT,
        requests=89,cases={"product-schema":1,"product":88},reserved_read=120000,reserved_written=2000,
        actual_read=500,actual_written=300,evidence=[{"request":1,"operation":1,"rows_read":500,"rows_written":300}],
        state_record_migration={"version":1,"request":50,"complete":True,"copied_records":2},state_storage_version=1,
        state_record_integrity_version=2,state_record_integrity_request=71,
        product_data={"rows":rows,"json":{},"arrays":262144,"records":{kind:2 if kind=="users" else 0 for kind in STATE_KINDS}},
        product_data_verified=True,**markers)
    journal._save(state)
    return journal


class Default(WorkerEntrypoint):
    async def fetch(self,request):
        if getattr(self.env,"PULLWISE_MODE","")!="local":return Response.json({"error":"LOCAL_ONLY"},status=503)
        path=str(request.url).rsplit("/",1)[-1]
        if path not in {"setup","upgrade","mutations","restart","rollback","rollback-restart"} or request.method!="POST":
            return Response.json({"error":"NOT_FOUND"},status=404)
        name="local-pending-rollback-fixture" if path.startswith("rollback") else "local-pending-main-fixture"
        return await self.env.FIXTURE_JOURNAL.get(self.env.FIXTURE_JOURNAL.idFromName(name)).fetch(request)


class FixtureJournal(DurableObject):
    def __init__(self,ctx,env):self.ctx,self.env=ctx,env
    async def fetch(self,request):
        path=str(request.url).rsplit("/",1)[-1]
        native=ObservedD1(self.env.ROLLBACK_DB if path.startswith("rollback") else self.env.DB,
            "rollback" if path.startswith("rollback") else "main",inject=path=="rollback")
        if path=="setup":
            baselines=[]
            for item in (native,ObservedD1(self.env.ROLLBACK_DB,"rollback")):
                await execute(item,[(sql,()) for sql in V11_SCHEMA_SQL])
                await execute(item,seed())
                value=await snapshot(item)
                assert value["schemaObjects"]==V11_SCHEMA_OBJECTS
                baselines.append(value)
            assert baselines[0]["tables"]==baselines[1]["tables"]
            fresh=ObservedD1(self.env.FRESH_DB,"fresh")
            await execute(fresh,[(sql,()) for sql in SCHEMA_SQL]); await execute(fresh,seed())
            assert (await snapshot(fresh,current=True))["schemaObjects"]==SCHEMA_OBJECTS
            # Native schema cap checks stay in the fresh, unjournaled fixture.
            await execute(fresh,[(INSERT,values(str(n))) for n in range(10)])
            await execute(fresh,[(INSERT,values("other","rule_other","other"))])
            before=await snapshot(fresh,current=True)
            probes=[(INSERT,values("overflow")),
                ("UPDATE expense_recurring_pending SET rule_id=?,owner_id=? WHERE rule_id=? AND period_key=?",("rule_owner","owner","rule_other","other")),
                ("UPDATE expense_recurring_pending SET template_json=? WHERE rule_id=? AND period_key=?",("[]","rule_owner","0"))]
            for probe in probes:
                try:await execute(fresh,[probe])
                except BaseException:pass
                else:raise AssertionError("Native pending integrity probe unexpectedly committed")
                assert (await snapshot(fresh,current=True))["tables"]==before["tables"]
            historical(self.ctx.storage.sql,baselines[0]);await save(self.ctx.storage,"baseline",baselines[0])
            return Response.json({"passed":True,"freshV12Schema":True,"nativePerRuleCap10":True,
                "reparentingCapGuard":True,"invalidFrozenTemplateGuard":True,"nativeSafeIntegerMoney":True,
                "historicalTables":baselines[0]["tables"],"freshFailedProbes":len(probes)})
        if path=="upgrade":
            journal=BudgetJournal(self.ctx.storage.sql,preview_product=True,product_operations=True)
            baseline=await load(self.ctx.storage,"baseline");before=journal.snapshot();plan=_upgrade_v12_plan(before)
            await upgrade_product_schema_v11(native,journal)
            assert native.dispatches==0 and journal.snapshot()==before
            await upgrade_product_schema_v12(native,journal);after=journal.snapshot();groups=list(native.groups)
            verified=await snapshot(native,current=True)
            assert {key:verified["tables"][key] for key in V11_INDEX_COUNTS}==baseline["tables"]
            assert verified["schemaObjects"]==SCHEMA_OBJECTS and verified["tables"]["expense_recurring_pending"]["rows"]==0
            assert after["schema_version"]==12 and after["schema_upgrade_v12"]["complete"] is True
            assert after["product_data"]["rows"]=={**before["product_data"]["rows"],"expense_recurring_pending":0}
            for key in ("scope","evidence","schema_upgrade",*("schema_upgrade_v"+str(n) for n in range(6,12)),
                    "state_record_migration","state_storage_version","state_record_integrity_version","state_record_integrity_request"):
                assert after[key]==before[key] if key!="evidence" else after[key][:len(before[key])]==before[key]
            assert after["requests"]==before["requests"]+1
            assert after["reserved_written"]==before["reserved_written"]+plan.rows_written
            assert after["actual_read"]-before["actual_read"]==sum(group["rowsRead"] for group in groups)
            assert after["actual_written"]-before["actual_written"]==sum(group["rowsWritten"] for group in groups)
            await save(self.ctx.storage,"postUpgrade",after)
            return Response.json({"passed":True,"schemaVersion":12,"schemaFingerprint":SCHEMA_FINGERPRINT,
                "all22HistoricalTablesPreserved":True,"sameJournalAndPriorMarkers":True,"accountingMatchesRawNativeMeta":True,
                "migrationNativeBatches":groups,"reservedRead":plan.rows_read,"reservedWritten":plan.rows_written,
                "migrationWriteExecution":after["schema_upgrade_v12"]["write_execution"]})
        if path=="mutations":
            journal=BudgetJournal(self.ctx.storage.sql,preview_product=True,product_operations=True)
            before=journal.snapshot();ticket=journal.begin_product(now=100)
            meter=ProductMeteredD1(native,journal,ticket,clock=lambda:101)
            await meter.ensure_cardinality();assert native.dispatches==0
            for n in range(10):await meter.batch([meter.prepare(INSERT).bind(*values(str(n)))])
            await meter.batch([meter.prepare(INSERT).bind(*values("other","rule_other","other"))])
            await meter.batch([meter.prepare("UPDATE expense_recurring_pending SET notification_state=? WHERE rule_id=? AND period_key=?").bind("inbox","rule_owner","0")])
            await meter.batch([meter.prepare("DELETE FROM expense_recurring_pending WHERE rule_id=? AND period_key=?").bind("rule_owner","9")])
            journal.finish(ticket,now=102);after=journal.snapshot();groups=list(native.groups)
            assert after["product_data"]["rows"]["expense_recurring_pending"]==10
            assert after["reserved_written"]-before["reserved_written"]==11*3+5+3
            assert after["actual_read"]-before["actual_read"]==sum(group["rowsRead"] for group in groups)
            assert after["actual_written"]-before["actual_written"]==sum(group["rowsWritten"] for group in groups)
            assert after["stopped"] is None and after["active"] is None
            data=await snapshot(native,current=True);baseline=await load(self.ctx.storage,"baseline")
            assert {key:data["tables"][key] for key in V11_INDEX_COUNTS}==baseline["tables"]
            await save(self.ctx.storage,"afterMutations",after);await save(self.ctx.storage,"afterMutationTables",data["tables"])
            return Response.json({"passed":True,"productPendingRows":10,"scalarInserts":11,"notificationUpdates":1,
                "manualRetryDeletes":1,"commercialHistoryUnchanged":True,"allIndexWritesReserved":True,
                "reservedWrites":after["reserved_written"]-before["reserved_written"],"rawNativeBatches":groups})
        if path=="restart":
            journal=BudgetJournal(self.ctx.storage.sql,preview_product=True,product_operations=True)
            saved=await load(self.ctx.storage,"afterMutations")
            await upgrade_product_schema_v11(native,journal);await upgrade_product_schema_v12(native,journal)
            assert native.dispatches==0 and journal.snapshot()==saved
            assert (await snapshot(native,current=True))["tables"]==await load(self.ctx.storage,"afterMutationTables")
            return Response.json({"passed":True,"schemaVersion":12,"pendingRowsRetained":10,
                "sameJournalRestart":True,"upgradeReplayDispatches":0})
        if path=="rollback":
            baseline=await snapshot(native);journal=historical(self.ctx.storage.sql,baseline)
            before=journal.snapshot();start=native.dispatches
            try:await upgrade_product_schema_v12(native,journal)
            except BudgetError as error:assert str(error)=="D1_OUTCOME_UNKNOWN"
            else:raise AssertionError("Late native batch failure unexpectedly committed")
            stopped=journal.snapshot()
            assert stopped["schema_version"]==11 and stopped["stopped"]=="D1_OUTCOME_UNKNOWN"
            assert stopped["schema_upgrade_v12"]["complete"] is False
            assert stopped["reserved_written"]==before["reserved_written"]+_upgrade_v12_plan(before).rows_written
            verified=await snapshot(native)
            assert verified["schemaObjects"]==V11_SCHEMA_OBJECTS and verified["tables"]==baseline["tables"]
            await save(self.ctx.storage,"stopped",stopped);await save(self.ctx.storage,"rollbackTables",baseline["tables"])
            return Response.json({"passed":True,"schemaVersionRetained":11,"lateAtomicRollback":True,
                "allHistoricalRowsRetained":True,"unknownOutcomeStopRetained":True,"reservationRetained":True})
        if path=="rollback-restart":
            journal=BudgetJournal(self.ctx.storage.sql,preview_product=True,product_operations=True)
            stopped=await load(self.ctx.storage,"stopped")
            try:await upgrade_product_schema_v12(native,journal)
            except BudgetError as error:assert str(error)=="D1_OUTCOME_UNKNOWN"
            else:raise AssertionError("Stopped migration replayed")
            assert native.dispatches==0 and journal.snapshot()==stopped
            assert (await snapshot(native))["tables"]==await load(self.ctx.storage,"rollbackTables")
            return Response.json({"passed":True,"schemaVersionRetained":11,"upgradeReplayDispatches":0,
                "fullReservationAndUnknownStopRetained":True})

'''

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8910)
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    if not directory.is_relative_to(Path("/workspace")) or not 1024 <= args.port <= 64535:
        raise SystemExit("Use a fresh /workspace directory and an unprivileged port")
    directory.mkdir(parents=True, exist_ok=False)
    source = directory / "src"
    source.mkdir()
    (source / "entry.py").write_text(ENTRY, encoding="utf-8")
    shutil.copytree(ROOT / "pullwise_server", source / "pullwise_server",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(WORKER / "python_modules", directory / "python_modules",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # Keep the fixture's Worker dependencies and lock identical to the release,
    # including the pinned IANA tzdata package used by recurring execution.
    for name in ("pyproject.toml", "uv.lock", "pylock.toml", "package.json", "package-lock.json"):
        shutil.copy2(WORKER / name, directory / name)
    # Resolve npx to the same already-installed Wrangler and Workerd release;
    # do not install a different tooling version inside this generated fixture.
    (directory / "node_modules").symlink_to(WORKER / "node_modules", target_is_directory=True)
    config = {"name": "pullwise-recurring-pending-schema-native-local-only", "main": "src/entry.py",
        "compatibility_date": "2026-09-23", "compatibility_flags": ["python_workers"],
        "workers_dev": False, "preview_urls": False, "routes": [],
        "vars": {"PULLWISE_MODE": "local", "PULLWISE_D1_ACCESS_ENABLED": "1"},
        "d1_databases": [{"binding": binding, "database_name": name,
            "database_id": identity, "remote": False} for binding, name, identity in (
                ("DB", "recurring-pending-schema-main-local-only", "00000000-0000-0000-0000-000000000040"),
                ("ROLLBACK_DB", "recurring-pending-schema-rollback-local-only", "00000000-0000-0000-0000-000000000041"),
                ("FRESH_DB", "recurring-pending-schema-fresh-local-only", "00000000-0000-0000-0000-000000000042"))],
        "durable_objects": {"bindings": [{"name": "FIXTURE_JOURNAL", "class_name": "FixtureJournal"}]},
        "migrations": [{"tag": "local-fixture-v1", "new_sqlite_classes": ["FixtureJournal"]}]}
    config_path = directory / "wrangler.jsonc"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    bundle = Path("/workspace/.cache/pullwise-pyodide/pyodide_314.0.6_2026-08-17_6.capnp.bin")
    if not bundle.is_file() or sha256(bundle) != BUNDLE_SHA256:
        raise SystemExit("Official pinned Workerd Python cache is missing or invalid")
    binary = WORKER / "node_modules/@cloudflare/workerd-linux-64/bin/workerd"
    wrapper = directory / "workerd-local-cache.sh"
    wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(str(binary)) +
        ' "$@" --pyodide-bundle-disk-cache-dir=/workspace/.cache/pullwise-pyodide\n')
    wrapper.chmod(0o700)
    env = {**os.environ, "XDG_CACHE_HOME": "/workspace/.cache", "XDG_CONFIG_HOME": "/workspace/.config",
        "UV_CACHE_DIR": "/workspace/.cache/uv", "UV_PYTHON_INSTALL_DIR": "/workspace/.python",
        "UV_SYSTEM_CERTS": "true", "NPM_CONFIG_CACHE": str(directory / "npm-cache"),
        "WRANGLER_SEND_METRICS": "false", "MINIFLARE_WORKERD_PATH": str(wrapper),
        "WRANGLER_LOG_PATH": str(directory / "wrangler-debug.log")}
    files = sorted((source / "pullwise_server").glob("*.py"))
    manifest = [(str(path.relative_to(source)), sha256(path)) for path in files]
    d1_source = WORKER / "node_modules/miniflare/dist/src/workers/d1/database.worker.js"
    evidence = {"passed": False, "date": "2026-10-10", "localOnly": True,
        "nativePythonFFI": True, "nativeDurableObjectSQLite": True, "syntheticPriorJournal": True,
        "remoteRequests": 0, "remoteD1Operations": 0, "realProviderRequests": 0,
        "realAccounts": 0, "realPayments": 0, "clientRetries": 0, "localHttpCap": len(HTTP_PATHS),
        "nativeAttemptsFabricated": False, "fixtureBindingCount": 3,
        "canonicalSourceTreeSha256": hashlib.sha256(json.dumps(manifest, separators=(",", ":")).encode()).hexdigest(),
        "canonicalSourceFileCount": len(files), "fixtureEntrySha256": sha256(source / "entry.py"),
        "dependencySha256": {name: sha256(directory / name) for name in
            ("pyproject.toml", "uv.lock", "pylock.toml", "package.json", "package-lock.json")},
        "sourceSha256": {str(path.relative_to(source)): sha256(path) for path in files
            if path.name in {"cloudflare_preview_schema.py", "cloudflare_preview_budget.py",
                "cloudflare_validation_budget.py", "cloudflare_native_d1.py", "cloudflare_state_records.py"}},
        "migrationsSha256": {path.name: sha256(path) for path in sorted((WORKER / "migrations").glob("*.sql"))},
        "runtimeProvenance": {"workerdSha256": sha256(binary), "runtimeBundleSha256": BUNDLE_SHA256,
            "miniflareD1SourceSha256": sha256(d1_source),
            "localTransaction": "state.storage.transactionSync(() => queries.map(query))",
            "totalAttempts": "Raw native fields retained; no fixture metadata substitution"},
        "scope": "Preserving v11-to-v12 schema, bounded pending native metering, caps and restart; authenticated API flow separate"}
    process, attempted, processes_started, results = None, 0, 0, []
    log_path = directory / "runtime.log"
    try:
        with log_path.open("w") as log:
            command = [str(WORKER / ".venv/bin/pywrangler"), "dev", "--local",
                "--config", str(config_path), "--ip", "127.0.0.1", "--port", str(args.port),
                "--inspector-port", str(args.port + 1000), "--persist-to", str(directory / "state")]
            def start_runtime():
                nonlocal process, processes_started
                offset = log_path.stat().st_size
                process = subprocess.Popen(command, cwd=directory, env=env, stdout=log,
                    stderr=subprocess.STDOUT, start_new_session=True)
                processes_started += 1
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise AssertionError("Native schema Worker startup failed")
                    recent = log_path.read_bytes()[offset:].decode(errors="replace")
                    if "Ready on http://127.0.0.1:" + str(args.port) in recent:
                        try:
                            with socket.create_connection(("127.0.0.1", args.port), timeout=.2):
                                return
                        except OSError:
                            pass
                    time.sleep(.2)
                raise AssertionError("Native schema Worker startup timed out")
            def stop_runtime():
                if process is not None and process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
            start_runtime()
            opener = build_opener(ProxyHandler({}), NoRedirect())
            for path in HTTP_PATHS:
                if path in {"restart", "rollback-restart"}:
                    stop_runtime()
                    start_runtime()
                attempted += 1
                assert attempted <= len(HTTP_PATHS)
                request = Request(f"http://127.0.0.1:{args.port}/_fixture/" + path,
                    method="POST", data=b"", headers={"Content-Type": "application/json"})
                try:
                    response = opener.open(request, timeout=60)
                except HTTPError as error:
                    response = error
                with response:
                    raw = response.read()
                    try:
                        payload = json.loads(raw)
                    except ValueError:
                        payload = {"nonJsonResponse": True, "bodyBytes": len(raw)}
                    results.append({"path": path, "status": response.status, "result": payload})
                    assert response.status == 200 and payload.get("passed") is True, (path, response.status, payload)
            evidence["passed"] = True
    except Exception as error:
        evidence["failure"] = str(error)
    finally:
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        groups, failures = [], []
        for line in log_path.read_text(errors="replace").splitlines() if log_path.exists() else []:
            if line.startswith('{"nativePendingSchemaMeta":'):
                groups.append(json.loads(line)["nativePendingSchemaMeta"])
            elif line.startswith('{"nativePendingSchemaFailure":'):
                failures.append(json.loads(line)["nativePendingSchemaFailure"])
        evidence.update(localHttpRequestsAttempted=attempted, localHttpRequestsCompleted=len(results),
            runtimeProcessesStarted=processes_started, actualProcessRestarts=max(0, processes_started - 1),
            nativeResultStatements=sum(group["statements"] for group in groups),
            nativeRowsRead=sum(group["rowsRead"] for group in groups),
            nativeRowsWritten=sum(group["rowsWritten"] for group in groups),
            nativeAttemptsObserved=sorted({value for group in groups for value in group["nativeAttempts"]}, key=str),
            failedNativeDispatches=failures, nativeFailedBatchTotalsUnavailable=bool(failures),
            cases=results, runtimeStopped=process is None or process.poll() is not None,
            setupAndIntegritySnapshotsOutsideMigrationJournal=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps({key: evidence.get(key) for key in ("passed", "failure",
            "localHttpRequestsCompleted", "nativeResultStatements", "nativeRowsRead", "nativeRowsWritten",
            "actualProcessRestarts", "runtimeStopped")}, ensure_ascii=False))
    if not evidence["passed"]:
        raise SystemExit("Native schema proof failed; preserve fixture and evidence without retry")


if __name__ == "__main__":
    main()
