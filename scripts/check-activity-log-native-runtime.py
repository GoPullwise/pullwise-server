"""Finite local native recent-operation journey through the canonical application.

Synthetic accounts/sessions stay in a fresh local D1. The unmodified application
uses NativeD1, ProductMeteredD1, PlanLimitedD1 and a real SQLite Durable Object
journal. No remote bindings, provider calls, client retries or reset modes exist.
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
HTTP_CAP = 60
LOCAL_METER_READ_CAP = 20000
LOCAL_METER_WRITE_CAP = 3000
LOCAL_PROBE_READ_CAP = 10000
BUNDLE_SHA256 = "3c3fd5a4179230e21e018e28b0e735e7c4abd8fe8e4ff39d2fd6bb2ea3a2e260"
OWNER, EDITOR, EMAIL = ("usr_github_770001", "usr_github_770002", "usr_email_770003")
FIXTURE_NOW = 1791547200  # 2026-10-09 12:00:00 UTC; not a caller-controlled production clock.

ENTRY = r'''
import hashlib
import json
import re
import time
from urllib.parse import urlsplit
from workers import WorkerEntrypoint, DurableObject, Response, Request
import application_entry as canonical
from pullwise_server.cloudflare_native_d1 import NativeD1
from pullwise_server.cloudflare_validation_budget import BudgetJournal, _field
from pullwise_server.cloudflare_preview_budget import (
    ProductMeteredD1, initialize_product, _COUNT_SQL, _RECORD_COUNT_SQL, _STRICT_RECORD_SQL,
)
from pullwise_server.cloudflare_state_records import record_name, encode_record
from pullwise_server.cloudflare_d1_mapping import initialize_account
from pullwise_server.cloudflare_plan_limits import _USAGE_SQL
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1
from pullwise_server.cloudflare_ledger_activity import _stamp
from pullwise_server.cloudflare_ledger_recurring import run_due_recurring

OWNER, EDITOR, EMAIL = "usr_github_770001", "usr_github_770002", "usr_email_770003"
FIXTURE_NOW = 1791547200
FINANCE = {"ledger_projects","ledger_project_repositories","expense_categories","expenses",
    "expense_events","expense_create_idempotency","expense_recurring_rules",
    "expense_recurring_occurrences","expense_suggestion_budget","expense_suggestion_events"}
BUSINESS = ("ledger_activity_events","expenses","expense_events","ledger_projects",
    "expense_recurring_rules","expense_recurring_occurrences","ledger_plan_usage")
provider_calls = []


class ForbiddenGateway:
    def __init__(self, env):
        pass
    def __getattr__(self, name):
        async def blocked(*args, **kwargs):
            provider_calls.append(name)
            raise AssertionError("Provider transport excluded from local activity proof")
        return blocked


canonical.WorkerGitHubGateway = ForbiddenGateway
canonical.WorkerCreemGateway = ForbiddenGateway


class FixtureClock:
    """Local-only reproducible boundary input; packaged application stays identical."""
    now = FIXTURE_NOW
    @classmethod
    def time(cls):
        return cls.now


canonical.time = FixtureClock


class ObservedStatement:
    def __init__(self, owner, sql, native):
        self.owner,self.sql,self.native = owner,sql,native
    def bind(self, *values):
        return ObservedStatement(self.owner,self.sql,self.native.bind(*values))


class ObservedD1(NativeD1):
    def __init__(self, binding):
        super().__init__(binding)
        self.groups,self.resource_finance,self.accounting_finance,self.phase = [],set(),set(),"metered"
    def prepare(self, sql):
        return ObservedStatement(self,sql,super().prepare(sql))
    async def batch(self, statements):
        statements = list(statements)
        assert 0<len(statements)<=64 and all(item.owner is self for item in statements)
        results = await super().batch([item.native for item in statements])
        values = [{"rowsRead":_field(_field(item,"meta"),"rows_read"),
            "rowsWritten":_field(_field(item,"meta"),"rows_written"),
            "attempts":_field(_field(item,"meta"),"total_attempts")} for item in results]
        assert all(type(item[key]) is int and item[key]>=0 for item in values
            for key in ("rowsRead","rowsWritten"))
        for item in statements:
            if self.phase=="metered":
                references = set(re.findall(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)",item.sql,re.I))
                if item.sql in {_COUNT_SQL,_RECORD_COUNT_SQL,_STRICT_RECORD_SQL,_USAGE_SQL}:
                    # These exact canonical accounting statements traverse
                    # cardinalities and expose no financial resource fields.
                    self.accounting_finance.update(references&FINANCE)
                else:
                    self.resource_finance.update(references&FINANCE)
        group = {"phase":self.phase,"statements":len(values),
            "rowsRead":sum(item["rowsRead"] for item in values),
            "rowsWritten":sum(item["rowsWritten"] for item in values),
            "nativeAttempts":[item["attempts"] for item in values]}
        self.groups.append(group)
        print(json.dumps({"nativeActivityRuntimeMeta":group}))
        return results


async def commands(native, values):
    return await native.batch([native.prepare(sql).bind(*params) for sql,params in values])


def seed(now):
    values = []
    for role,identity in (("owner",OWNER),("editor",EDITOR),("email",EMAIL)):
        user = {"id":identity,"name":"Synthetic "+role,"createdAt":now-1000,
            "billing":{"plan":"free"}}
        if role != "email":
            user.update(githubId=identity.removeprefix("usr_github_"),githubLogin="synthetic-"+role)
        else:
            user.update(email="private-native-email@example.test",emailVerified=True)
        if role=="owner":
            user["billing"]={"provider":"creem","plan":"pro","status":"active",
                "subscriptionId":"sub_local_activity_proof","interval":"month",
                "currentPeriodStart":now-1000,"currentPeriodEnd":now+200000}
        payload = encode_record("users",identity,user)
        values.append(("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
            (record_name("users",identity),payload,now)))
        session = "activity-native-"+role
        values.append(("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
            (record_name("sessions",session),encode_record("sessions",session,
                {"id":session,"userId":identity,"expiresAt":now+200000}),now)))
        values.extend(initialize_account(owner_id=identity,account_snapshot=payload,now=now))
        if role != "owner":
            values.append(("""INSERT INTO workspace_members(workspace_id,user_id,role,revision,
                joined_at,updated_at,invited_by_user_id) VALUES(?,?,'editor',1,?,?,?)""",
                (OWNER,identity,_stamp(now-1000),_stamp(now-1000),OWNER)))
    values.extend([
        ("""INSERT INTO expense_categories(id,owner_id,name,revision,created_at,updated_at)
            VALUES(?,?,?,1,?,?)""",("cat_native_activity",OWNER,"Native operating costs","local","local")),
        ("""INSERT INTO expenses(id,owner_id,target_kind,project_id,category_id,occurred_on,
            amount_minor,currency,purpose,note,revision,created_at,updated_at)
            VALUES(?,?,'shared',NULL,?,'2026-10-09',1250,'USD',?,NULL,1,?,?)""",
            ("exp_native_old",OWNER,"cat_native_activity","Protected historical expense",_stamp(now-172800),_stamp(now-172800))),
        ("""INSERT INTO expense_events(id,expense_id,owner_id,actor_kind,actor_id,
            action,before_json,after_json,created_at) VALUES(?,?,?,'session',?,'create',NULL,?,?)""",
            ("evt_native_old","exp_native_old",OWNER,OWNER,'{"immutable":"historical financial audit"}',_stamp(now-172800))),
    ])
    before=None
    snapshot={"target":{"kind":"shared"},"purpose":"Native rolling fixture","amount":"12.50",
        "currency":"USD","occurredOn":"2026-10-08","categoryId":"cat_native_activity"}
    for index,stamp in [*((index,_stamp(now-86401-index)) for index in range(16)),
            (16,_stamp(now-86400)),(17,_stamp(now-86399)),(18,_stamp(now+1))]:
        identifier=("act_expired_"+str(index) if index<16 else
            "act_boundary" if index==16 else "act_inside" if index==17 else "act_future")
        values.append(("""INSERT INTO ledger_activity_events(id,operation_id,owner_id,target_kind,
            project_id,actor_json,resource_kind,resource_id,action,before_json,after_json,created_at)
            VALUES(?,?,?,'shared',NULL,?,'expense','exp_native_old','create',?,?,?)""",
            (identifier,"op_seed_"+str(index),OWNER,json.dumps({"kind":"user","userId":OWNER,
                "name":"Synthetic owner","githubLogin":"synthetic-owner"}),before,json.dumps(snapshot),stamp)))
    return values


async def business_snapshot(native):
    # Local-only exact integrity probes are read-only and outside the product
    # ticket. Record them separately; they never fabricate journal usage.
    native.phase="integrity-probe"
    result = await commands(native,[("SELECT * FROM "+table,()) for table in BUSINESS])
    data = {}
    for table,part in zip(BUSINESS,result):
        rows = [dict(row) for row in _field(part,"results")]
        rows.sort(key=lambda row:json.dumps(row,sort_keys=True,separators=(",",":")))
        data[table] = {"rows":len(rows),"sha256":hashlib.sha256(json.dumps(rows,
            sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()}
    rows = {table:list(_field(part,"results")) for table,part in zip(BUSINESS,result)}
    old_audit=[row for row in rows["expense_events"] if row["id"]=="evt_native_old"]
    return {"tables":data,
        "expiredSeedProjectionIds":sorted(row["id"] for row in rows["ledger_activity_events"] if row["id"].startswith("act_expired_")),
        "expiredProjectionRows":sum(row["created_at"]<=_stamp(FixtureClock.now-86400) for row in rows["ledger_activity_events"]),
        "activityRows":len(rows["ledger_activity_events"]),
        "activityOperations":len({row["operation_id"] for row in rows["ledger_activity_events"]}),
        "activityActions":[row["action"] for row in rows["ledger_activity_events"]],
        "activityResources":[{"kind":row["resource_kind"],"id":row["resource_id"],"action":row["action"]}
            for row in rows["ledger_activity_events"]],
        "legacyFinancialAuditRows":len(old_audit),
        "legacyFinancialAuditSha256":hashlib.sha256(json.dumps(old_audit,sort_keys=True,separators=(",",":")).encode()).hexdigest(),
        "deletedExpenses":sum(row["deleted_at"] is not None for row in rows["expenses"]),
        "occurrences":len(rows["expense_recurring_occurrences"]),
        "ruleStatuses":sorted(row["status"] for row in rows["expense_recurring_rules"]),
        "commercialWrites":sum(row["writes"] for row in rows["ledger_plan_usage"]),
        "commercialProjects":sum(row["projects"] for row in rows["ledger_plan_usage"]),
        "commercialRecords":sum(row["records"] for row in rows["ledger_plan_usage"])}


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if getattr(self.env,"PULLWISE_MODE","")!="local":
            return Response.json({"error":"LOCAL_ONLY"},status=503)
        raw=await request.bytes()
        raw=raw if isinstance(raw,bytes) else raw.to_bytes()
        assert len(raw)<=8192
        forwarded=Request(str(request.url),method=str(request.method),
            headers=dict(request.headers.items()),body=raw if raw else None)
        return await self.env.FIXTURE_JOURNAL.get(
            self.env.FIXTURE_JOURNAL.idFromName("local-activity-runtime")).fetch(forwarded)


class FixtureJournal(DurableObject):
    def __init__(self, ctx, env):
        self.ctx,self.env=ctx,env
    async def fetch(self, request):
        path=urlsplit(str(request.url)).path
        call=int(request.headers.get("x-native-fixture-call") or "0")
        count=int(await self.ctx.storage.get("httpCalls") or 0)+1
        assert count<=60 and call==count
        await self.ctx.storage.put("httpCalls",count)
        FixtureClock.now=int(await self.ctx.storage.get("fixtureNow") or FIXTURE_NOW)
        native=ObservedD1(self.env.DB)
        journal=BudgetJournal(self.ctx.storage.sql,preview_product=True,product_operations=True)
        before=journal.snapshot()
        provider_calls.clear()
        if path=="/_fixture/setup":
            assert count==1 and before["requests"]==0
            await initialize_product(native,journal)
            ticket=journal.begin_product(now=time.time())
            meter=ProductMeteredD1(native,journal,ticket)
            await meter.ensure_cardinality()
            await meter.batch([meter.prepare(sql).bind(*params) for sql,params in seed(FixtureClock.now)])
            assert meter.accounted_outcome()
            journal.finish(ticket,now=time.time())
            response=Response.json({"passed":True,"syntheticCookieAccounts":3,
                "seedThroughCanonicalMeter":True,"canonicalFreshSchemaVersion":9,
                "fixtureClock":_stamp(FixtureClock.now)})
        elif path=="/_fixture/final":
            state=journal.snapshot()
            assert state["active"] is None and state["stopped"] is None
            response=Response.json({"passed":True,"journalHealthy":True,
                "schemaVersion":state["schema_version"],"stateStorageVersion":state["state_storage_version"],
                "counters":{key:state[key] for key in
                    ("requests","reserved_read","reserved_written","actual_read","actual_written")},
                "nativeAttemptsFabricated":False})
        elif path=="/_fixture/advance-clock":
            # Finite local fixture input only. No runtime clock input exists in
            # the product, scheduler RPC or deployed public routes.
            assert FixtureClock.now==FIXTURE_NOW
            FixtureClock.now=FIXTURE_NOW+86401
            await self.ctx.storage.put("fixtureNow",FixtureClock.now)
            response=Response.json({"fixtureClock":_stamp(FixtureClock.now)})
        elif path in {"/_fixture/tick","/_fixture/rename-email"}:
            ticket=journal.begin_product(now=time.time())
            meter=ProductMeteredD1(native,journal,ticket)
            await meter.ensure_cardinality()
            if path=="/_fixture/tick":
                # Invoke the identical internal runner, with smaller reviewed
                # finite bounds. This is never a remote forced cron/tick.
                result=await run_due_recurring(binding=PlanLimitedD1(meter,now=FixtureClock.now),
                    maintenance_binding=meter,gateway=ForbiddenGateway(self.env),
                    now=FixtureClock.now,rule_limit=2,occurrence_limit=2)
            else:
                part=await meter.prepare("SELECT payload FROM app_state WHERE name=?").bind(record_name("users",EMAIL)).first()
                user=json.loads(part["payload"])
                assert user["name"]=="Synthetic email"
                user["name"]="Synthetic email renamed"
                await meter.batch([meter.prepare("UPDATE app_state SET payload=?,updated_at=? WHERE name=?").bind(
                    encode_record("users",EMAIL,user),FixtureClock.now,record_name("users",EMAIL))])
                result={"syntheticIdentityRenamed":True}
            assert meter.accounted_outcome()
            journal.finish(ticket,now=time.time())
            response=Response.json(result)
        else:
            assert (path.startswith("/api/v1/") or path=="/api-keys") and before["schema_ready"] is True
            ticket=journal.begin_product(now=time.time())
            meter=ProductMeteredD1(native,journal,ticket)
            await meter.ensure_cardinality()
            response=await canonical._Application(self.env,meter).fetch(request)
            assert meter.accounted_outcome()
            journal.finish(ticket,now=time.time())
        after=journal.snapshot()
        groups=[group for group in native.groups if group["phase"]=="metered"]
        assert after["actual_read"]-before["actual_read"]==sum(group["rowsRead"] for group in groups)
        assert after["actual_written"]-before["actual_written"]==sum(group["rowsWritten"] for group in groups)
        assert after["actual_read"]<=20000 and after["actual_written"]<=3000
        assert not provider_calls and after["active"] is None and after["stopped"] is None
        business=await business_snapshot(native)
        print(json.dumps({"nativeActivityRuntimeRequest":{"call":call,"path":path,"method":request.method,
            "status":response.status,"meteredGroups":groups,"business":business,
            "resourceFinanceTablesRead":sorted(native.resource_finance),"providerCalls":0,
            "accountingFinanceTablesCounted":sorted(native.accounting_finance),
            "journalActualDeltasMatchRawMeta":True,"journalHealthy":True}},ensure_ascii=False))
        return response
'''


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def redacted(value):
    if isinstance(value, dict):
        return {key:redacted(item) for key,item in value.items() if key.lower() not in {"token","key","invitationurl"}}
    if isinstance(value, list):
        return [redacted(item) for item in value]
    return value


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--port",type=int,default=8909)
    args=parser.parse_args()
    directory=args.run_dir.resolve()
    if not directory.is_relative_to(Path("/workspace")) or not 1024<=args.port<=64535:
        raise SystemExit("Use a fresh /workspace directory and an unprivileged port")
    directory.mkdir(parents=True,exist_ok=False)
    source=directory/"src"
    source.mkdir()
    (source/"entry.py").write_text(ENTRY,encoding="utf-8")
    shutil.copy2(WORKER/"src/entry.py",source/"application_entry.py")
    shutil.copytree(ROOT/"pullwise_server",source/"pullwise_server",ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
    shutil.copytree(WORKER/"python_modules",directory/"python_modules",ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
    dependencies=("pyproject.toml","uv.lock","pylock.toml","package.json","package-lock.json")
    for name in dependencies:
        shutil.copy2(WORKER/name,directory/name)
    (directory/"node_modules").symlink_to(WORKER/"node_modules",target_is_directory=True)
    base=f"http://127.0.0.1:{args.port}"
    config={"name":"pullwise-activity-runtime-local-only","main":"src/entry.py",
        "compatibility_date":"2026-09-23","compatibility_flags":["python_workers"],
        "workers_dev":False,"preview_urls":False,"routes":[],
        "vars":{"PULLWISE_MODE":"local","PULLWISE_D1_ACCESS_ENABLED":"1",
            "PULLWISE_APP_URL":base,"PULLWISE_ALLOWED_ORIGINS":base,
            "PULLWISE_JEV_ENABLED":"0","PULLWISE_JEV_QUALITY_EVALUATED":"0"},
        "d1_databases":[{"binding":"DB","database_name":"activity-runtime-local-only",
            "database_id":"00000000-0000-0000-0000-000000000039","remote":False}],
        "durable_objects":{"bindings":[{"name":"FIXTURE_JOURNAL","class_name":"FixtureJournal"}]},
        "migrations":[{"tag":"local-fixture-v1","new_sqlite_classes":["FixtureJournal"]}]}
    config_path=directory/"wrangler.jsonc"
    config_path.write_text(json.dumps(config,indent=2)+"\n")
    bundle=Path("/workspace/.cache/pullwise-pyodide/pyodide_314.0.6_2026-08-17_6.capnp.bin")
    if not bundle.is_file() or sha256(bundle)!=BUNDLE_SHA256:
        raise SystemExit("Official pinned Workerd Python cache missing or invalid")
    binary=WORKER/"node_modules/@cloudflare/workerd-linux-64/bin/workerd"
    wrapper=directory/"workerd-local-cache.sh"
    wrapper.write_text("#!/bin/sh\nexec "+shlex.quote(str(binary))+
        ' "$@" --pyodide-bundle-disk-cache-dir=/workspace/.cache/pullwise-pyodide\n')
    wrapper.chmod(0o700)
    env={**os.environ,"XDG_CACHE_HOME":"/workspace/.cache","XDG_CONFIG_HOME":"/workspace/.config",
        "UV_CACHE_DIR":"/workspace/.cache/uv","UV_PYTHON_INSTALL_DIR":"/workspace/.python",
        "UV_SYSTEM_CERTS":"true","NPM_CONFIG_CACHE":str(directory/"npm-cache"),
        "WRANGLER_SEND_METRICS":"false","MINIFLARE_WORKERD_PATH":str(wrapper),
        "WRANGLER_LOG_PATH":str(directory/"wrangler-debug.log")}
    files=sorted((source/"pullwise_server").glob("*.py"))
    manifest=[(str(path.relative_to(source)),sha256(path)) for path in files]
    evidence={"passed":False,"date":"2026-10-09","localOnly":True,
        "nativePythonFFI":True,"nativeDurableObjectSQLite":True,
        "canonicalApplicationEntrySha256":sha256(source/"application_entry.py"),
        "canonicalSourceTreeSha256":hashlib.sha256(json.dumps(manifest,separators=(",",":")).encode()).hexdigest(),
        "canonicalSourceFileCount":len(files),"fixtureEntrySha256":sha256(source/"entry.py"),
        "dependencySha256":{name:sha256(directory/name) for name in dependencies},
        "sourceSha256":{str(path.relative_to(source)):sha256(path) for path in files if path.name in {
            "cloudflare_ledger_activity.py","cloudflare_ledger_expenses.py","cloudflare_ledger_api.py",
            "cloudflare_ledger_recurring.py","cloudflare_native_d1.py","cloudflare_preview_budget.py",
            "cloudflare_plan_limits.py","cloudflare_validation_budget.py"}},
        "runtimeProvenance":{"workerdSha256":sha256(binary),"runtimeBundleSha256":BUNDLE_SHA256,
            "miniflareD1SourceSha256":sha256(WORKER/"node_modules/miniflare/dist/src/workers/d1/database.worker.js")},
        "productMeterAndCommercialPlanAdapter":True,"trustedOriginCheckedByCanonicalApplication":True,
        "syntheticCookieAccounts":3,"remoteD1Operations":0,"remoteApplicationRequests":0,
        "realProviderRequests":0,"realAccounts":0,"realPayments":0,"clientRetries":0,
        "localHttpCap":HTTP_CAP,"nativeAttemptsFabricated":False,
        "localMeteredRowCaps":{"read":LOCAL_METER_READ_CAP,"written":LOCAL_METER_WRITE_CAP},
        "localIntegrityProbeReadCap":LOCAL_PROBE_READ_CAP,
        "fixedFixtureServerClock":FIXTURE_NOW,"syntheticEmailOnlyAccounts":1,
        "scope":"Synthetic authenticated native recent-activity flow; real-account/browser/remote acceptance separate"}
    process,attempted,cases=None,0,[]
    log_path=directory/"runtime.log"
    try:
        with log_path.open("w") as log:
            command=[str(WORKER/".venv/bin/pywrangler"),"dev","--local","--config",str(config_path),
                "--ip","127.0.0.1","--port",str(args.port),"--inspector-port",str(args.port+1000),
                "--persist-to",str(directory/"state")]
            process=subprocess.Popen(command,cwd=directory,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            deadline=time.monotonic()+120
            while time.monotonic()<deadline:
                if process.poll() is not None:
                    raise AssertionError("Native activity Worker startup failed")
                if "Ready on http://127.0.0.1:"+str(args.port) in log_path.read_text(errors="replace"):
                    try:
                        with socket.create_connection(("127.0.0.1",args.port),timeout=.2):
                            break
                    except OSError:
                        pass
                time.sleep(.2)
            else:
                raise AssertionError("Native activity Worker startup timed out")
            opener=build_opener(ProxyHandler({}),NoRedirect())

            def call(name,method,path,actor=None,body=None,expected=200,workspace=False,revision=None,trusted=True,
                     idempotency=None,key=None):
                nonlocal attempted
                attempted+=1
                assert attempted<=HTTP_CAP
                headers={"Content-Type":"application/json","X-Native-Fixture-Call":str(attempted)}
                if actor:
                    headers["Cookie"]="pw_session=activity-native-"+actor
                if key:
                    headers["Authorization"]="Bearer "+key
                if idempotency:
                    headers["Idempotency-Key"]=idempotency
                if method in {"POST","PATCH","DELETE"}:
                    headers["Origin"]=base if trusted else "https://untrusted.example.test"
                if workspace:
                    headers["X-Pullwise-Workspace"]=OWNER
                if revision is not None:
                    headers["If-Match"]='"'+str(revision)+'"'
                request=Request(base+path,method=method,headers=headers,
                    data=json.dumps(body,separators=(",",":")).encode() if body is not None else None)
                try:
                    response=opener.open(request,timeout=60)
                except HTTPError as error:
                    response=error
                with response:
                    raw=response.read()
                    payload=json.loads(raw) if raw else None
                    cases.append({"name":name,"method":method,"path":path,"status":response.status,"result":redacted(payload)})
                    assert response.status==expected,(name,response.status,redacted(payload))
                    return payload

            call("setup","POST","/_fixture/setup",body={})
            floor=call("exact_24h_floor_readonly","GET","/api/v1/activity?target=shared","owner")
            assert [item["id"] for item in floor["items"]]==["act_inside"]
            assert floor["windowStart"]=="2026-10-08T12:00:00Z" and floor["windowEnd"]=="2026-10-09T12:00:00Z"
            call("login_required","GET","/api/v1/activity?target=shared",expected=401)
            call("untrusted_origin","POST","/api/v1/projects","owner",{"name":"Untrusted"},403,trusted=False)
            primary=call("project_create_cleanup_16","POST","/api/v1/projects","owner",{"name":"Native project P"},201)
            secondary=call("project_create_cleanup_remaining","POST","/api/v1/projects","owner",{"name":"Private target Q"},201)
            pid,qid=primary["id"],secondary["id"]
            project_path="/api/v1/projects/"+pid
            primary=call("project_settings_update","PATCH",project_path,"owner",{
                "name":"Native project P renamed","description":"Reviewable settings", 
                "developmentUrl":"https://example.test/native-development", "productUrl":"https://example.test/native-product"},revision=1)
            assert primary["revision"]==2
            call("project_stale_cas","PATCH",project_path,"owner",{"name":"Stale edit"},412,revision=1)
            project_activity="/api/v1/activity?target=project&projectId="+pid
            other_activity="/api/v1/activity?target=project&projectId="+qid
            shared_activity="/api/v1/activity?target=shared"

            def draft(kind="project",project=pid,**changes):
                return {"target":{"kind":kind,**({"projectId":project} if kind=="project" else {})},
                    "occurredOn":"2026-10-09","amount":"12.34","currency":"USD", 
                    "categoryId":"cat_native_activity","purpose":"Native expense", "note":"First value",**changes}

            expense_draft=draft()
            expense=call("project_expense_create","POST","/api/v1/expenses","owner",expense_draft,201,idempotency="native-activity-project")
            replay=call("project_create_exact_replay","POST","/api/v1/expenses","owner",expense_draft,201,idempotency="native-activity-project")
            assert replay==expense
            expense_path="/api/v1/expenses/"+expense["id"]
            updated_draft=draft(amount="23.45",purpose="Editor revised expense",note="Changed by editor")
            changed=call("editor_expense_update","PATCH",expense_path,"editor",updated_draft,workspace=True,revision=1)
            assert changed["revision"]==2
            call("expense_stale_cas","PATCH",expense_path,"editor",draft(amount="99.99"),412,workspace=True,revision=1)
            shared_draft=draft("shared",purpose="Email member expense")
            shared_expense=call("email_shared_expense_create","POST","/api/v1/expenses","email",shared_draft,201,workspace=True,idempotency="native-activity-email")
            email_page=call("email_identity_history","GET",shared_activity,"owner")
            email_event=next(item for item in email_page["items"] if item["resource"]["id"]==shared_expense["id"])
            assert email_event["actor"]=={"kind":"user","userId":EMAIL,"name":"Synthetic email","githubLogin":None}
            assert "private-native-email@example.test" not in json.dumps(email_page)
            call("rename_synthetic_email_identity","POST","/_fixture/rename-email",body={})
            unchanged=call("immutable_actor_snapshot","GET",shared_activity,"owner")
            assert next(item for item in unchanged["items"] if item["id"]==email_event["id"])["actor"]==email_event["actor"]
            key_metadata=call("restricted_key_create","POST","/api-keys","owner",{
                "name":"Synthetic native P-only key","scopes":["expenses:read","expenses:write"],
                "restrictions":{"shared":False,"projectIds":[pid]}},201)
            key=key_metadata["key"]
            moved_draft={**updated_draft,"target":{"kind":"project","projectId":qid}}
            moved=call("move_project_expense","PATCH",expense_path,"owner",moved_draft,revision=2)
            assert moved["revision"]==3
            restricted=call("key_redacts_moved_target","GET",project_activity,key=key)
            assert all(item["resource"]["kind"]=="expense" for item in restricted["items"])
            movement=next(item for item in restricted["items"] if item["action"]=="move")
            target_change=next(item for item in movement["changes"] if item["field"]=="target")
            assert target_change["before"]["projectId"]==pid and target_change["after"]=={"kind":"restricted"}
            assert qid not in json.dumps(restricted) and "Private target Q" not in json.dumps(restricted)
            mirrored=call("moved_target_history","GET",other_activity,"owner")
            assert next(item for item in mirrored["items"] if item["action"]=="move")["operationId"]==movement["operationId"]
            call("key_other_project_denied","GET",other_activity,expected=403,key=key)
            call("key_shared_denied","GET",shared_activity,expected=403,key=key)
            key_expense=call("api_key_expense_create","POST","/api/v1/expenses",body=draft(purpose="Key expense"),expected=201,
                idempotency="native-activity-key",key=key)
            key_page=call("api_key_identity_history","GET",project_activity,key=key)
            key_event=next(item for item in key_page["items"] if item["resource"]["id"]==key_expense["id"])
            assert key_event["actor"]=={"kind":"api_key","userId":OWNER,"name":"Synthetic owner","githubLogin":"synthetic-owner"}
            call("move_expense_to_shared","PATCH",expense_path,"owner",{**moved_draft,"target":{"kind":"shared"}},revision=3)
            call("shared_expense_delete","DELETE","/api/v1/expenses/"+shared_expense["id"],"editor",expected=204,workspace=True,revision=1)

            schedule={"frequency":"weekly","weekday":5,"timezone":"Asia/Shanghai","startOn":"2026-10-09"}
            rule_body={key:value for key,value in draft(purpose="Project recurring cost").items() if key!="occurredOn"}
            rule_body["schedule"]=schedule
            rule=call("project_rule_create","POST","/api/v1/expense-recurring-rules","owner",rule_body,201,idempotency="native-activity-project-rule")
            assert call("rule_exact_replay","POST","/api/v1/expense-recurring-rules","owner",rule_body,201,
                idempotency="native-activity-project-rule")==rule
            shared_rule_body={**rule_body,"target":{"kind":"shared"},"purpose":"Email recurring cost"}
            shared_rule=call("shared_rule_create","POST","/api/v1/expense-recurring-rules","email",shared_rule_body,201,
                workspace=True,idempotency="native-activity-shared-rule")
            generated=call("native_internal_recurring_generation","POST","/_fixture/tick",body={})
            assert generated=={"scanned":2,"created":2,"blocked":0,"replayed":0}
            assert call("native_internal_recurring_idle","POST","/_fixture/tick",body={})["created"]==0
            project_page=call("project_rule_and_generation_history","GET",project_activity,"owner")
            assert any(item["resource"]["kind"]=="recurring_rule" and item["action"]=="create" for item in project_page["items"])
            automatic=next(item for item in project_page["items"] if item["action"]=="generate")
            assert automatic["actor"]=={"kind":"system","userId":OWNER,"name":"Synthetic owner","githubLogin":"synthetic-owner"}
            restricted_rules=call("key_excludes_recurring_and_settings","GET",project_activity,key=key)
            assert all(item["resource"]["kind"]=="expense" for item in restricted_rules["items"])
            shared_page=call("email_scheduled_identity_history","GET",shared_activity,"owner")
            email_automatic=next(item for item in shared_page["items"] if item["action"]=="generate")
            assert email_automatic["actor"]=={"kind":"system","userId":EMAIL,"name":"Synthetic email renamed","githubLogin":None}
            rule_path="/api/v1/expense-recurring-rules/"+rule["id"]
            call("project_rule_pause","PATCH",rule_path,"owner",{"status":"paused"},revision=2)
            call("project_rule_resume","PATCH",rule_path,"owner",{"status":"active"},revision=3)
            call("project_rule_update","PATCH",rule_path,"owner",{**rule_body,"purpose":"Recurring cost edited"},revision=4)
            call("project_rule_cancel","DELETE",rule_path,"owner",expected=204,revision=5)
            call("shared_rule_cancel","DELETE","/api/v1/expense-recurring-rules/"+shared_rule["id"],"email",expected=204,
                workspace=True,revision=2)
            call("project_archive","PATCH",project_path,"owner",{"status":"archived"},revision=2)
            call("project_restore","PATCH",project_path,"owner",{"status":"active"},revision=3)
            initial_page=call("bounded_history_first_page","GET",project_activity+"&limit=2","owner")
            assert len(initial_page["items"])==2 and initial_page["nextCursor"]
            cursor=initial_page["nextCursor"]
            second_page=call("bounded_history_next_page","GET",project_activity+"&limit=2&cursor="+cursor,"owner")
            assert len(second_page["items"])==2 and not ({item["id"] for item in initial_page["items"]}&{item["id"] for item in second_page["items"]})
            call("remove_email_member","DELETE",f"/api/v1/workspaces/{OWNER}/members/{EMAIL}","owner",expected=204,revision=1)
            call("removed_member_history_denied","GET",shared_activity,"email",expected=404,workspace=True)
            retained=call("owner_history_after_member_removal","GET",shared_activity,"owner")
            assert next(item for item in retained["items"] if item["id"]==email_event["id"])["actor"]==email_event["actor"]
            restart_before=call("restart_before_journal","GET","/_fixture/final")
            previous_pid=process.pid
            os.killpg(process.pid,signal.SIGTERM)
            process.wait(timeout=15)
            process=subprocess.Popen(command,cwd=directory,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            deadline=time.monotonic()+120
            while time.monotonic()<deadline:
                if process.poll() is not None:
                    raise AssertionError("Native activity Worker restart failed")
                try:
                    with socket.create_connection(("127.0.0.1",args.port),timeout=.2):
                        break
                except OSError:
                    time.sleep(.2)
            else:
                raise AssertionError("Native activity Worker restart timed out")
            restart_after=call("restart_after_journal","GET","/_fixture/final")
            assert restart_after==restart_before and process.pid!=previous_pid
            restarted_history=call("history_after_process_restart","GET",shared_activity,"owner")
            assert restarted_history==retained
            evidence["actualProcessRestartSameStateAndCounters"]=True
            call("advance_local_fixture_clock","POST","/_fixture/advance-clock",body={})
            expired_cursor=call("cursor_cannot_extend_24h_window","GET",project_activity+"&limit=2&cursor="+cursor,"owner")
            assert expired_cursor["items"]==[] and expired_cursor["windowStart"]=="2026-10-09T12:00:01Z"
            aged_shared=call("expired_read_stays_readonly","GET",shared_activity,"owner")
            assert aged_shared["items"]==[]
            call("bounded_retention_after_day","PATCH",project_path,"owner",{"description":"Next-day change"},revision=4)
            idle_cleanup=call("scheduled_idle_retention_after_day","POST","/_fixture/tick",body={})
            assert idle_cleanup=={"scanned":0,"created":0,"blocked":0,"replayed":0}
            final=call("final_journal","GET","/_fixture/final")
            assert final["passed"] and final["journalHealthy"] and final["schemaVersion"]==9
            evidence["passed"]=True
    except Exception as error:
        evidence["failure"]=str(error)
    finally:
        if process and process.poll() is None:
            os.killpg(process.pid,signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL)
                process.wait(timeout=5)
        observations,groups=[],[]
        for line in log_path.read_text(errors="replace").splitlines() if log_path.exists() else []:
            if line.startswith('{"nativeActivityRuntimeRequest":'):
                observations.append(json.loads(line)["nativeActivityRuntimeRequest"])
            elif line.startswith('{"nativeActivityRuntimeMeta":'):
                groups.append(json.loads(line)["nativeActivityRuntimeMeta"])
        evidence.update(localHttpRequestsAttempted=attempted,localHttpRequestsCompleted=len(cases),cases=cases,
            nativeRequests=observations,nativeStatements=sum(group["statements"] for group in groups),
            meteredNativeRowsRead=sum(group["rowsRead"] for group in groups if group["phase"]=="metered"),
            meteredNativeRowsWritten=sum(group["rowsWritten"] for group in groups if group["phase"]=="metered"),
            integrityProbeNativeRowsRead=sum(group["rowsRead"] for group in groups if group["phase"]=="integrity-probe"),
            nativeAttemptsObserved=sorted({value for group in groups for value in group["nativeAttempts"]},key=str),
            runtimeStopped=process is None or process.poll() is not None,
            integrityProbesReadOnlyAndOutsideJournal=True)
        if evidence["passed"]:
            try:
                assert len(observations)==len(cases)
                by_name={case["name"]:observation for case,observation in zip(cases,observations)}
                by_case={case["name"]:case for case in cases}
                assert evidence["meteredNativeRowsRead"]<=LOCAL_METER_READ_CAP
                assert evidence["meteredNativeRowsWritten"]<=LOCAL_METER_WRITE_CAP
                assert evidence["integrityProbeNativeRowsRead"]<=LOCAL_PROBE_READ_CAP
                assert all(sum(group["rowsWritten"] for group in item["meteredGroups"])==0
                    for case,item in zip(cases,observations) if case["method"]=="GET")
                for current,previous in (("project_create_exact_replay","project_expense_create"),
                        ("expense_stale_cas","editor_expense_update"),
                        ("project_stale_cas","project_settings_update"),
                        ("rule_exact_replay","project_rule_create"),
                        ("native_internal_recurring_idle","native_internal_recurring_generation")):
                    assert by_name[current]["business"]["tables"]==by_name[previous]["business"]["tables"],current
                    assert sum(group["rowsWritten"] for group in by_name[current]["meteredGroups"])==0,current
                assert by_name["setup"]["business"]["expiredProjectionRows"]==17
                assert by_name["exact_24h_floor_readonly"]["business"]["activityRows"]==19
                assert by_name["project_create_cleanup_16"]["business"]["expiredProjectionRows"]==1
                assert by_name["project_create_cleanup_16"]["business"]["activityRows"]==4
                assert by_name["project_create_cleanup_remaining"]["business"]["expiredProjectionRows"]==0
                assert by_name["project_create_cleanup_remaining"]["business"]["activityRows"]==4
                legacy=by_name["setup"]["business"]["legacyFinancialAuditSha256"]
                assert all(item["business"]["legacyFinancialAuditRows"]==1 and
                    item["business"]["legacyFinancialAuditSha256"]==legacy for item in observations)
                next_day=by_name["bounded_retention_after_day"]["business"]
                previous=by_name["expired_read_stays_readonly"]["business"]
                assert previous["expiredProjectionRows"]>16
                assert next_day["expiredProjectionRows"]==previous["expiredProjectionRows"]-16
                assert next_day["activityRows"]==previous["activityRows"]-16+1
                idle_cleanup=by_name["scheduled_idle_retention_after_day"]["business"]
                assert idle_cleanup["expiredProjectionRows"]==0 and idle_cleanup["activityRows"]==1
                assert all(idle_cleanup["tables"][table]==next_day["tables"][table]
                    for table in idle_cleanup["tables"] if table!="ledger_activity_events")
                changes=by_case["project_rule_and_generation_history"]["result"]["items"]
                editor_event=next(item for item in changes if item["resource"]["kind"]=="expense" and
                    item["action"]=="update" and item["actor"]["userId"]==EDITOR)
                assert editor_event["actor"]=={"kind":"user","userId":EDITOR,"name":"Synthetic editor","githubLogin":"synthetic-editor"}
                amount=next(item for item in editor_event["changes"] if item["field"]=="amount")
                assert amount=={"field":"amount","before":{"amount":"12.34","currency":"USD"},
                    "after":{"amount":"23.45","currency":"USD"}}
                assert editor_event["createdAt"]=="2026-10-09T12:00:00Z"
                expected_writes={"project_create_cleanup_16":1,"project_create_cleanup_remaining":1,
                    "project_settings_update":1,"project_expense_create":1,"editor_expense_update":1,
                    "email_shared_expense_create":1,"restricted_key_create":1,"move_project_expense":1,
                    "api_key_expense_create":1,"move_expense_to_shared":1,"shared_expense_delete":1,
                    "project_rule_create":1,"shared_rule_create":1,"native_internal_recurring_generation":2,
                    "project_rule_resume":1,"project_rule_update":1,"project_archive":1,"project_restore":1,
                    "bounded_retention_after_day":1}
                commercial_before=0
                for case,item in zip(cases,observations):
                    commercial=item["business"]["commercialWrites"]
                    assert commercial-commercial_before==expected_writes.get(case["name"],0),(case["name"],commercial-commercial_before)
                    commercial_before=commercial
                final_business=observations[-1]["business"]
                assert final_business["commercialWrites"]==sum(expected_writes.values())
                assert final_business["commercialProjects"]==2 and final_business["commercialRecords"]==6
                assert final_business["deletedExpenses"]==1 and final_business["occurrences"]==2
                assert final_business["ruleStatuses"]==["canceled","canceled"]
                assert by_name["untrusted_origin"]["meteredGroups"]==[]
                assert not any(item["resourceFinanceTablesRead"] for item in observations
                    if item["call"] in {by_name[name]["call"] for name in
                        ("key_other_project_denied","key_shared_denied","removed_member_history_denied")})
                assert "private-native-email@example.test" not in json.dumps(cases)
                assert final_business["tables"]["expense_events"]["rows"]==10
                evidence.update(actorTimeRecordAndValueChangesProved=True,immutableActorSnapshotProved=True,
                    emailIdentityOmitsPrivateEmail=True,apiKeyAttributedWithoutCredentialExposure=True,
                    restrictedKeyMoveAndSettingsIsolationProved=True,removedMemberReadDenied=True,
                    strictRolling24hWindowAndAnchoredPaginationProved=True,allActivityGETsReadOnly=True,
                    staleCASAndExactReplaysNoWrites=True,scheduledGenerationAndIdleReplayProved=True,
                    boundedRetention16Then1AndNextDay16Proved=True,
                    scheduledIdleRetiresProjectionOnlyWithoutCommercialCharge=True,
                    financialAuditNeverRetiredOrChanged=True,commercialWrites=final_business["commercialWrites"],
                    commercialChargesUnchangedByProjectionRowsAndCleanup=True,
                    recurringPauseAndCancelRemainUncharged=True,originalFinancialAuditRows=10,
                    allJournalActualDeltasMatchRawMeta=True)
            except Exception as error:
                evidence["passed"]=False
                evidence["failure"]="Native observation assertion failed: "+str(error)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(evidence,indent=2,ensure_ascii=False)+"\n")
        print(json.dumps({key:evidence.get(key) for key in ("passed","failure","localHttpRequestsCompleted",
            "nativeStatements","meteredNativeRowsRead","meteredNativeRowsWritten","runtimeStopped")},ensure_ascii=False))
    if not evidence["passed"]:
        raise SystemExit("Native activity journey failed; preserve local fixture and evidence without retry")


if __name__=="__main__":
    main()
