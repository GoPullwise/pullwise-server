"""Finite local Bearer REST parity through the canonical Worker and SQLite journal.

Every account, key, DB and clock is synthetic and local. The fixture adds only
bounded setup/tick/probe endpoints; application and authorization modules are
copied unchanged. No remote D1, providers, redirects, retries or deployments.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
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
HTTP_CAP = 50
READ_CAP, WRITE_CAP, PROBE_READ_CAP = 100000, 10000, 5000
BUNDLE_SHA256 = "3c3fd5a4179230e21e018e28b0e735e7c4abd8fe8e4ff39d2fd6bb2ea3a2e260"
OWNER, EDITOR, APPLICANT = "usr_github_790001", "usr_github_790002", "usr_github_790003"
FIXTURE_NOW = 1791547200
PINNED_TZDATA_VERSION = "2026.5"

ENTRY = r'''
import hashlib
import json
import time
from urllib.parse import urlsplit
from workers import Response, Request
import application_entry as canonical
from pullwise_server.cloudflare_native_d1 import NativeD1
from pullwise_server.cloudflare_validation_budget import _field, BUDGET_SCOPE
from pullwise_server.cloudflare_preview_budget import ProductMeteredD1, initialize_product
from pullwise_server.cloudflare_state_records import record_name, encode_record
from pullwise_server.cloudflare_d1_mapping import initialize_account
from pullwise_server.cloudflare_preview_schema import SCHEMA_VERSION

OWNER,EDITOR,APPLICANT="usr_github_790001","usr_github_790002","usr_github_790003"
FIXTURE_NOW=1791547200
TABLES=("app_state","account_entitlement_authority","api_keys","expenses","expense_events",
    "expense_create_idempotency","expense_recurring_rules","expense_recurring_occurrences",
    "ledger_activity_events","ledger_projects","workspace_members","workspace_invites",
    "workspace_join_requests","workspace_events","ledger_plan_usage","d1_command_guard")
observed=[]
provider_calls=[]

class ForbiddenGateway:
    def __init__(self,env): pass
    def __getattr__(self,name):
        async def blocked(*args,**kwargs):
            provider_calls.append(name)
            raise AssertionError("Providers excluded from finite local REST parity proof")
        return blocked
canonical.WorkerGitHubGateway=ForbiddenGateway
canonical.WorkerCreemGateway=ForbiddenGateway

class FixtureClock:
    now=FIXTURE_NOW
    @classmethod
    def time(cls): return cls.now
canonical.time=FixtureClock

class BoundedProductMeter(ProductMeteredD1):
    def __init__(self,*args,**kwargs):
        kwargs["clock"]=FixtureClock.time
        super().__init__(*args,**kwargs)
    async def _operation(self,statements,reads,writes):
        state=self.journal.snapshot()
        if state["reserved_read"]+reads>100000 or state["reserved_written"]+writes>10000:
            print(json.dumps({"nativeRestParityPreflightRejected":{
                "reservedRead":state["reserved_read"],"groupReadBound":reads,
                "reservedWritten":state["reserved_written"],"groupWriteBound":writes}}))
            raise AssertionError("Local fixed predispatch reservation cap exceeded")
        return await super()._operation(statements,reads,writes)
canonical.ProductMeteredD1=BoundedProductMeter

class ObservedStatement:
    def __init__(self,owner,sql,native): self.owner,self.sql,self.native=owner,sql,native
    def bind(self,*params): return ObservedStatement(self.owner,self.sql,self.native.bind(*params))
class ObservedD1(NativeD1):
    def __init__(self,binding):
        super().__init__(binding)
        self.phase,self.groups="metered",[]
        observed.append(self)
    def prepare(self,sql): return ObservedStatement(self,sql,super().prepare(sql))
    async def batch(self,statements):
        statements=list(statements)
        assert 0<len(statements)<=64 and all(item.owner is self for item in statements)
        results=await super().batch([item.native for item in statements])
        values=[{"read":_field(_field(item,"meta"),"rows_read"),
            "written":_field(_field(item,"meta"),"rows_written"),
            "attempts":_field(_field(item,"meta"),"total_attempts")} for item in results]
        assert all(type(item[key]) is int and item[key]>=0 for item in values for key in ("read","written"))
        group={"phase":self.phase,"statements":len(values),
            "rowsRead":sum(item["read"] for item in values),
            "rowsWritten":sum(item["written"] for item in values),
            "nativeAttempts":[item["attempts"] for item in values]}
        self.groups.append(group)
        print(json.dumps({"nativeRestParityMeta":group}))
        return results
canonical.NativeD1=ObservedD1

async def commands(native,values):
    return await native.batch([native.prepare(sql).bind(*params) for sql,params in values])
def seed(now):
    values=[]
    for role,identity in (("owner",OWNER),("editor",EDITOR),("applicant",APPLICANT)):
        user={"id":identity,"name":"Synthetic "+role,"createdAt":now-1000,
            "githubId":identity.removeprefix("usr_github_"),"githubLogin":"synthetic-"+role,
            "billing":{"plan":"free"}}
        if role=="owner":
            user["billing"]={"provider":"creem","plan":"pro","status":"active",
                "subscriptionId":"sub_native_rest_parity","interval":"month",
                "currentPeriodStart":now-1000,"currentPeriodEnd":now+8640000}
        payload=encode_record("users",identity,user)
        session="rest-parity-native-"+role
        values.extend([("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
            (record_name("users",identity),payload,now)),
            ("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
            (record_name("sessions",session),encode_record("sessions",session,
                {"id":session,"userId":identity,"expiresAt":now+8640000}),now)),
            *initialize_account(owner_id=identity,account_snapshot=payload,now=now)])
    values.append(("""INSERT INTO workspace_members(workspace_id,user_id,role,revision,
        joined_at,updated_at,invited_by_user_id) VALUES(?,?,'editor',1,'local','local',?)""",(OWNER,EDITOR,OWNER)))
    for identity,category in ((OWNER,"cat_native_rest"),(APPLICANT,"cat_native_free")):
        values.append(("""INSERT INTO expense_categories(id,owner_id,name,revision,
            created_at,updated_at) VALUES(?,?,'Hosting',1,'local','local')""",(category,identity)))
    for identifier in ("prj_native_rest","prj_native_remove"):
        values.append(("""INSERT INTO ledger_projects(id,owner_id,name,github_repo_id,
            github_full_name,description,status,revision,created_at,updated_at)
            VALUES(?,?,'Native standalone',NULL,NULL,'','active',1,'local','local')""",(identifier,OWNER)))
    return values

async def snapshot(native,journal):
    native.phase="integrity-probe"
    result=await commands(native,[("SELECT * FROM "+table,()) for table in TABLES])
    rows={table:[dict(row) for row in _field(part,"results")] for table,part in zip(TABLES,result)}
    tables={}
    for table,items in rows.items():
        items.sort(key=lambda row:json.dumps(row,sort_keys=True,separators=(",",":")))
        tables[table]={"rows":len(items),"sha256":hashlib.sha256(json.dumps(items,
            sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()}
    rules=[{"id":item["id"],"status":item["status"],"revision":item["revision"],
        "blockedCode":item["blocked_code"],"actor":item["actor_user_id"],
        "keyGrantPresent":"_api_key_hash" in json.loads(item["template_json"])}
        for item in rows["expense_recurring_rules"]]
    state=journal.snapshot()
    assert len(rows["api_keys"])<=5 and len(rules)<=3 and len(rows["expenses"])<=6
    assert len(rows["ledger_projects"])==2 and len(rows["workspace_members"])<=2
    assert tables["d1_command_guard"]["rows"]==0
    return {"tables":tables,"rules":rules,
        "members":[{"userId":item["user_id"],"role":item["role"],"revision":item["revision"],
            "removed":item["removed_at"] is not None} for item in rows["workspace_members"]],
        "keyRevokedCount":sum(item["revoked_at"] is not None for item in rows["api_keys"]),
        "expenseActors":[{"kind":item["actor_kind"],"actor":item["actor_id"]}
            for item in rows["expense_events"] if item["actor_kind"]=="schedule"],
        "usage":[{"owner":item["owner_id"],"projects":item["projects"],"records":item["records"],
            "writes":item["writes"]} for item in rows["ledger_plan_usage"]],
        "cardinalityVerified":state.get("product_data_verified"),
        "cardinality":state["product_data"]["rows"]}

class Default(canonical.Default):
    async def fetch(self,request):
        raw=await request.bytes()
        raw=raw if isinstance(raw,bytes) else raw.to_bytes()
        assert len(raw)<=8192
        request=Request(str(request.url),method=str(request.method),
            headers=dict(request.headers.items()),body=raw if raw else None)
        if urlsplit(str(request.url)).path.startswith("/_fixture/"):
            return await self.env.VALIDATION_BUDGET.get(
                self.env.VALIDATION_BUDGET.idFromName(BUDGET_SCOPE)).fetch(request)
        return await super().fetch(request)
class ValidationBudget(canonical.ValidationBudget):
    async def fetch(self,request):
        path=urlsplit(str(request.url)).path
        count=int(await self.ctx.storage.get("fixtureHttpCalls") or 0)+1
        assert count<=50 and count==int(request.headers.get("x-native-fixture-call") or "0")
        await self.ctx.storage.put("fixtureHttpCalls",count)
        FixtureClock.now=int(await self.ctx.storage.get("fixtureNow") or FIXTURE_NOW)
        observed.clear(); provider_calls.clear()
        journal=self._journal(); before=journal.snapshot()
        if path=="/_fixture/setup":
            assert count==1 and before["requests"]==0
            native=ObservedD1(self.env.DB)
            await initialize_product(native,journal)
            ticket=journal.begin_product(now=FixtureClock.time())
            meter=BoundedProductMeter(native,journal,ticket)
            await meter.ensure_cardinality()
            await meter.batch([meter.prepare(sql).bind(*params) for sql,params in seed(FixtureClock.now)])
            assert meter.accounted_outcome()
            journal.finish(ticket,now=FixtureClock.time())
            response=Response.json({"passed":True,"seedThroughCanonicalMeter":True,
                "syntheticCookieAccounts":3,"schemaVersion":SCHEMA_VERSION})
        elif path=="/_fixture/advance-clock":
            assert FixtureClock.now==FIXTURE_NOW
            FixtureClock.now=FIXTURE_NOW+32*86400
            await self.ctx.storage.put("fixtureNow",FixtureClock.now)
            response=Response.json({"fixtureClockAdvancedOnce":True})
        elif path=="/_fixture/tick":
            assert int(await self.ctx.storage.get("fixtureTicks") or 0)<2
            await self.ctx.storage.put("fixtureTicks",int(await self.ctx.storage.get("fixtureTicks") or 0)+1)
            # Binding-only canonical scheduler; its existing limits are 10/10.
            # The selected fixture contains exactly three rules, one due period each.
            response=Response.json(await self.runRecurring())
        elif path=="/_fixture/final":
            state=journal.snapshot()
            assert state["active"] is None and state["stopped"] is None
            response=Response.json({"passed":True,"journalHealthy":True,"scope":state["scope"],
                "schemaVersion":state["schema_version"],"stateStorageVersion":state["state_storage_version"],
                "counters":{key:state[key] for key in ("requests","reserved_read","reserved_written",
                    "actual_read","actual_written")}})
        else:
            response=await super().fetch(request)
        after=journal.snapshot()
        groups=[group for native in observed for group in native.groups if group["phase"]=="metered"]
        assert after["actual_read"]-before["actual_read"]==sum(g["rowsRead"] for g in groups)
        assert after["actual_written"]-before["actual_written"]==sum(g["rowsWritten"] for g in groups)
        assert after["actual_read"]<=100000 and after["actual_written"]<=10000
        assert not provider_calls and after["active"] is None and after["stopped"] is None
        integrity=await snapshot(ObservedD1(self.env.DB),journal)
        probe=sum(g["rowsRead"] for native in observed for g in native.groups if g["phase"]=="integrity-probe")
        cumulative=int(await self.ctx.storage.get("fixtureProbeReads") or 0)+probe
        assert cumulative<=5000
        await self.ctx.storage.put("fixtureProbeReads",cumulative)
        print(json.dumps({"nativeRestParityRequest":{"call":count,"path":path,"method":request.method,
            "status":response.status,"meteredGroups":groups,"integrity":integrity,
            "providerCalls":0,"journalActualDeltasMatchRawMeta":True,"journalHealthy":True}}))
        return response
'''

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args): return None

def sha256(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def redacted(value):
    if isinstance(value,dict):
        return {key:redacted(item) for key,item in value.items()
            if key.lower() not in {"key","token","url","invitationurl","prefix"}}
    if isinstance(value,list): return [redacted(item) for item in value]
    return value

def verified_tzdata_record(record):
    """Return an exact pinned distribution only after its RECORD hashes match."""
    record=Path(record)
    distribution=importlib.metadata.Distribution.at(record.parent)
    if distribution.metadata["Name"]!="tzdata" or distribution.version!=PINNED_TZDATA_VERSION:
        raise ValueError("tzdata distribution must be exactly "+PINNED_TZDATA_VERSION)
    package_root=record.parents[1]
    if not (package_root/"tzdata").is_dir():
        raise ValueError("tzdata package directory is missing")
    package_manifest=[]
    for name,digest,size in csv.reader(record.read_text().splitlines()):
        relative=Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("tzdata RECORD contains an invalid package path")
        target=package_root/relative
        if digest:
            algorithm,encoded=digest.split("=",1)
            if algorithm!="sha256" or not target.is_file():
                raise ValueError("tzdata RECORD contains an unavailable hashed file")
            actual=base64.urlsafe_b64encode(hashlib.sha256(target.read_bytes()).digest()).decode().rstrip("=")
            if actual!=encoded or size!=str(target.stat().st_size):
                raise ValueError("tzdata RECORD file integrity check failed")
        if target.is_file(): package_manifest.append((name,sha256(target)))
    if not package_manifest:
        raise ValueError("tzdata RECORD is empty")
    return package_root,package_manifest


def pinned_tzdata(cache=None):
    """Find installed pinned tzdata, then verified extracted/uv cache candidates."""
    candidates=[]
    try:
        installed=importlib.metadata.distribution("tzdata")
        if installed.version==PINNED_TZDATA_VERSION:
            candidates.extend(Path(installed.locate_file(item)) for item in installed.files or []
                if str(item).endswith(".dist-info/RECORD"))
    except importlib.metadata.PackageNotFoundError:
        pass
    for pattern in (".venv*/lib/python*/site-packages", ".venv*/Lib/site-packages"):
        for packages in WORKER.glob(pattern):
            candidates.extend(packages.glob("tzdata-"+PINNED_TZDATA_VERSION+".dist-info/RECORD"))
    caches=[]
    if cache is not None: caches.append(Path(cache))
    if os.environ.get("UV_CACHE_DIR"): caches.append(Path(os.environ["UV_CACHE_DIR"]))
    caches.extend((Path("/workspace/.cache/uv"),Path("/tmp/pullwise-uv-cache")))
    for directory in caches:
        if directory.name=="RECORD": candidates.append(directory)
        else:
            distribution="tzdata-"+PINNED_TZDATA_VERSION+".dist-info/RECORD"
            for pattern in (distribution,"*/"+distribution,"archive-v0/*/"+distribution):
                candidates.extend(sorted(directory.glob(pattern)))
    checked=set()
    for candidate in candidates:
        identity=candidate.resolve()
        if identity in checked: continue
        checked.add(identity)
        # A discovered pinned installation must be valid. Do not silently skip
        # a corrupt copy and select another distribution with the same version.
        if candidate.is_file(): return verified_tzdata_record(candidate)
    raise SystemExit("Install tzdata=="+PINNED_TZDATA_VERSION+
        " or pass --tzdata-cache with its extracted package directory or uv cache")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--port",type=int,default=8931)
    parser.add_argument("--tzdata-cache",type=Path,
        help="Optional extracted tzdata==2026.5 distribution directory or uv cache root")
    args=parser.parse_args()
    directory=args.run_dir.resolve()
    if not directory.is_relative_to(Path("/workspace")) or not 1024<=args.port<=64535:
        raise SystemExit("Use a fresh /workspace directory and an unprivileged port")
    directory.mkdir(parents=True,exist_ok=False)
    source=directory/"src"; source.mkdir()
    (source/"entry.py").write_text(ENTRY,encoding="utf-8")
    shutil.copy2(WORKER/"src/entry.py",source/"application_entry.py")
    shutil.copytree(ROOT/"pullwise_server",source/"pullwise_server",ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
    shutil.copytree(WORKER/"python_modules",directory/"python_modules",ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
    # Reuse installed/restored pinned packages without pywrangler's online
    # resolver. Every hashed distribution file is checked before copying.
    package_root,package_manifest=pinned_tzdata(args.tzdata_cache)
    shutil.copytree(package_root/"tzdata",directory/"python_modules/tzdata",
        ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
    dependencies=("pyproject.toml","uv.lock","pylock.toml","package.json","package-lock.json")
    for name in dependencies: shutil.copy2(WORKER/name,directory/name)
    (directory/"node_modules").symlink_to(WORKER/"node_modules",target_is_directory=True)
    base=f"http://127.0.0.1:{args.port}"
    config={"name":"pullwise-rest-parity-native-local-only","main":"src/entry.py",
        "compatibility_date":"2026-09-23","compatibility_flags":["python_workers"],
        "workers_dev":False,"preview_urls":False,"routes":[],
        "vars":{"PULLWISE_MODE":"preview","PULLWISE_D1_ACCESS_ENABLED":"1",
            "PULLWISE_PREVIEW_PRODUCT_ENABLED":"1","PULLWISE_RECURRING_EXPENSES_ENABLED":"1",
            "PULLWISE_APP_URL":"https://preview.pull-wise.com","PULLWISE_ALLOWED_ORIGINS":base,
            "PULLWISE_CREEM_API_BASE_URL":"https://test-api.creem.io",
            "PULLWISE_JEV_SUGGESTIONS_ENABLED":"0","PULLWISE_JEV_SUGGESTIONS_EVALUATED":"0"},
        "d1_databases":[{"binding":"DB","database_name":"rest-parity-native-local-only",
            "database_id":"00000000-0000-0000-0000-000000000041","remote":False}],
        "durable_objects":{"bindings":[{"name":"VALIDATION_BUDGET","class_name":"ValidationBudget"}]},
        "migrations":[{"tag":"local-fixture-v1","new_sqlite_classes":["ValidationBudget"]}]}
    config_path=directory/"wrangler.jsonc"; config_path.write_text(json.dumps(config,indent=2)+"\n")
    bundle=Path("/workspace/.cache/pullwise-pyodide/pyodide_314.0.6_2026-08-17_6.capnp.bin")
    if not bundle.is_file() or sha256(bundle)!=BUNDLE_SHA256:
        raise SystemExit("Official pinned Workerd Python cache missing or invalid")
    binary=WORKER/"node_modules/@cloudflare/workerd-linux-64/bin/workerd"
    wrapper=directory/"workerd-local-cache.sh"
    wrapper.write_text("#!/bin/sh\nexec "+shlex.quote(str(binary))+
        ' "$@" --pyodide-bundle-disk-cache-dir=/workspace/.cache/pullwise-pyodide\n'); wrapper.chmod(0o700)
    env={**os.environ,"XDG_CACHE_HOME":"/workspace/.cache","XDG_CONFIG_HOME":"/workspace/.config",
        "UV_CACHE_DIR":"/workspace/.cache/uv","UV_PYTHON_INSTALL_DIR":"/workspace/.python",
        "UV_SYSTEM_CERTS":"true","NPM_CONFIG_CACHE":str(directory/"npm-cache"),
        "WRANGLER_SEND_METRICS":"false","MINIFLARE_WORKERD_PATH":str(wrapper),
        "WRANGLER_LOG_PATH":str(directory/"wrangler-debug.log")}
    files=sorted((source/"pullwise_server").glob("*.py"))
    manifest=[(str(path.relative_to(source)),sha256(path)) for path in files]
    schema=int(re.findall(r"^SCHEMA_VERSION = ([0-9]+)$",(source/"pullwise_server/cloudflare_preview_schema.py").read_text(),re.M)[-1])
    evidence={"passed":False,"date":"2026-10-09","localOnly":True,"expectedSchemaVersion":schema,
        "nativePythonFFI":True,"nativeDurableObjectSQLite":True,"canonicalDefaultAndValidationBudget":True,
        "singletonScope":"pullwise-s17-s18-2026-09-28","fixtureClockStart":FIXTURE_NOW,
        "fixtureClockAdvanceSeconds":32*86400,"cachedTzdataFileManifestSha256":hashlib.sha256(json.dumps(package_manifest,separators=(",",":")).encode()).hexdigest(),"canonicalApplicationEntrySha256":sha256(source/"application_entry.py"),
        "canonicalSourceTreeSha256":hashlib.sha256(json.dumps(manifest,separators=(",",":")).encode()).hexdigest(),
        "canonicalSourceFileCount":len(files),"fixtureEntrySha256":sha256(source/"entry.py"),
        "sourceSha256":dict(manifest),"dependencySha256":{name:sha256(directory/name) for name in dependencies},
        "runtimeProvenance":{"workerdSha256":sha256(binary),"runtimeBundleSha256":BUNDLE_SHA256,
            "miniflareD1SourceSha256":sha256(WORKER/"node_modules/miniflare/dist/src/workers/d1/database.worker.js")},
        "productMeterAndCommercialPlanAdapter":True,"nativeAttemptsFabricated":False,
        "syntheticCookieAccounts":3,"syntheticKeysMaximum":5,"remoteD1Operations":0,
        "remoteApplicationRequests":0,"realProviderRequests":0,"clientRetries":0,
        "localHttpCap":HTTP_CAP,"localReservedRowCaps":{"read":READ_CAP,"written":WRITE_CAP},
        "localIntegrityProbeReadCap":PROBE_READ_CAP,
        "scope":"Native local REST parity, activity scope and quota usage; remote/browser/real accounts separate"}
    attempted,cases,process=0,[],None
    log_path=directory/"runtime.log"
    try:
        with log_path.open("w") as log:
            process=subprocess.Popen(["node",str(WORKER/"node_modules/wrangler/bin/wrangler.js"),"dev","--local","--config",str(config_path),
                "--ip","127.0.0.1","--port",str(args.port),"--inspector-port",str(args.port+1000),
                "--persist-to",str(directory/"state")],cwd=directory,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            deadline=time.monotonic()+120
            while time.monotonic()<deadline:
                if process.poll() is not None: raise AssertionError("Native REST parity Worker startup failed")
                if "Ready on http://127.0.0.1:"+str(args.port) in log_path.read_text(errors="replace"):
                    try:
                        with socket.create_connection(("127.0.0.1",args.port),timeout=.2): break
                    except OSError: pass
                time.sleep(.2)
            else: raise AssertionError("Native REST parity Worker startup timed out")
            opener=build_opener(ProxyHandler({}),NoRedirect())
            def call(name,method,path,*,cookie=None,token=None,body=None,expected=200,revision=None,
                    workspace=None,idempotency=None,trusted=True):
                nonlocal attempted
                attempted+=1; assert attempted<=HTTP_CAP
                headers={"X-Native-Fixture-Call":str(attempted),"Content-Type":"application/json"}
                if cookie: headers["Cookie"]="pw_session=rest-parity-native-"+cookie
                if token: headers["Authorization"]="Bearer "+token
                if cookie and method in {"POST","PATCH","DELETE"}:
                    headers["Origin"]=base if trusted else "https://untrusted.example.test"
                if workspace: headers["X-Pullwise-Workspace"]=workspace
                if revision is not None: headers["If-Match"]='"'+str(revision)+'"'
                if idempotency: headers["Idempotency-Key"]=idempotency
                request=Request(base+path,method=method,headers=headers,
                    data=json.dumps(body,separators=(",",":")).encode() if body is not None else None)
                try: response=opener.open(request,timeout=60)
                except HTTPError as error: response=error
                with response:
                    raw=response.read(); payload=json.loads(raw) if raw else None
                    assert "_api_key_hash" not in raw.decode(),name
                    cases.append({"name":name,"method":method,"path":path,"status":response.status,"result":redacted(payload)})
                    assert response.status==expected,(name,response.status,redacted(payload))
                    return payload
            def expense(project=False,category="cat_native_rest",**changes):
                return {"target":{"kind":"project","projectId":"prj_native_rest"} if project else {"kind":"shared"},
                    "occurredOn":"2026-10-09","amount":"12.00","currency":"USD","categoryId":category,
                    "purpose":"Hosting",**changes}
            def recurring(project=False):
                body=expense(project); body.pop("occurredOn")
                body["schedule"]={"frequency":"monthly","day":9,"timezone":"Asia/Shanghai","startOn":"2026-10-01"}
                return body
            call("setup","POST","/_fixture/setup",body={})
            scope=["profile:read","projects:read","projects:write","categories:read","categories:write",
                "expenses:read","expenses:write","reports:read","members:read","members:write"]
            full=call("owner_management_key","POST","/api-keys",cookie="owner",body={"name":"Native management",
                "scopes":scope,"restrictions":{"shared":True}},expected=201)
            mgmt=full["key"]
            rec=call("recurring_expense_key","POST","/api-keys",cookie="owner",body={"name":"Native recurring",
                "scopes":["expenses:read","expenses:write"],"restrictions":{"shared":True}},expected=201)
            project=call("restricted_project_key","POST","/api-keys",cookie="owner",body={"name":"Native restricted",
                "scopes":["expenses:read","expenses:write"],
                "restrictions":{"shared":False,"projectIds":["prj_native_rest"]}},expected=201)
            editor=call("member_expense_key","POST","/api-keys",cookie="editor",workspace=OWNER,
                body={"name":"Native member","scopes":["expenses:read","expenses:write"],
                    "restrictions":{"shared":True}},expected=201)
            free=call("free_guide_key","POST","/api-keys",cookie="applicant",body={"name":"Native Free guide",
                "scopes":["profile:read","categories:read","expenses:read","expenses:write","reports:read"],
                "restrictions":{"shared":True}},expected=201)
            guide=call("free_guide_exact_payload","POST","/api/v1/expenses",token=free["key"],
                body=expense(category="cat_native_free"),idempotency="native-free-guide",expected=201)
            assert guide["amount"]=="12.00"
            me=call("free_ledger_usage","GET","/api/v1/me",token=free["key"])
            assert me["ledgerUsage"]["expenseRecords"]["used"]==1 and me["ledgerUsage"]["expenseRecords"]["limit"]==100
            shared=call("shared_expense_create","POST","/api/v1/expenses",token=mgmt,body=expense(),
                idempotency="native-shared-expense",expected=201)
            ep="/api/v1/expenses/"+shared["id"]
            call("expense_stale_revision","PATCH",ep,token=mgmt,body=expense(amount="13.00"),revision=2,expected=412)
            call("shared_expense_edit","PATCH",ep,token=mgmt,body=expense(amount="13.00"),revision=1)
            call("restricted_key_shared_denied","GET",ep,token=project["key"],expected=403)
            call("shared_expense_remove","DELETE",ep,token=mgmt,revision=2,expected=204)
            ordinary=call("project_expense_create","POST","/api/v1/expenses",token=project["key"],body=expense(True),
                idempotency="native-project-expense",expected=201)
            call("project_expense_remove","DELETE","/api/v1/expenses/"+ordinary["id"],token=project["key"],revision=1,expected=204)
            project_rule=call("project_rule_create","POST","/api/v1/expense-recurring-rules",token=rec["key"],
                body=recurring(True),idempotency="native-project-rule",expected=201)
            shared_rule=call("shared_rule_create","POST","/api/v1/expense-recurring-rules",token=rec["key"],
                body=recurring(),idempotency="native-shared-rule",expected=201)
            member_rule=call("member_rule_create","POST","/api/v1/expense-recurring-rules",token=editor["key"],
                body=recurring(),idempotency="native-member-rule",expected=201)
            generated=call("native_tick_generate","POST","/_fixture/tick",body={})
            assert generated=={"ok":True,"scanned":3,"created":3,"blocked":0,"replayed":0},generated
            call("project_settings_update","PATCH","/api/v1/projects/prj_native_rest",token=mgmt,
                body={"productUrl":"https://example.test/native-product"},revision=1)
            expense_activity=call("expense_only_activity","GET","/api/v1/activity?target=project&projectId=prj_native_rest",token=project["key"])
            assert expense_activity["items"] and all(item["resource"]["kind"]!="project" for item in expense_activity["items"])
            project_activity=call("project_scope_activity","GET","/api/v1/activity?target=project&projectId=prj_native_rest",token=mgmt)
            assert any(item["resource"]["kind"]=="project" for item in project_activity["items"])
            limited=call("recurring_target_filtered_list","GET","/api/v1/expense-recurring-rules?limit=1",token=project["key"])
            assert [item["id"] for item in limited["items"]]==[project_rule["id"]] and limited["nextCursor"] is None
            rp="/api/v1/expense-recurring-rules/"+shared_rule["id"]
            current=call("generated_rule_read","GET",rp,token=rec["key"])
            assert current["revision"]==2
            edited=call("recurring_edit","PATCH",rp,token=rec["key"],body={**recurring(),"amount":"15.00"},revision=2)
            paused=call("recurring_pause","PATCH",rp,token=rec["key"],body={"status":"paused"},revision=edited["revision"])
            resumed=call("recurring_resume","PATCH",rp,token=rec["key"],body={"status":"active"},revision=paused["revision"])
            call("recurring_key_revoke","DELETE","/api-keys/"+rec["id"],cookie="owner")
            member=call("issuer_role_revision_change","PATCH",f"/api/v1/workspaces/{OWNER}/members/{EDITOR}",
                token=mgmt,body={"role":"viewer"},revision=1)
            assert member["revision"]==2
            invalid=call("member_key_revision_invalidated","GET","/api/v1/expenses",token=editor["key"],expected=403)
            assert invalid["error"]["code"]=="WORKSPACE_MEMBERSHIP_CHANGED"
            call("advance_fixture_clock_once","POST","/_fixture/advance-clock",body={})
            blocked=call("native_tick_revoked_and_member_blocked","POST","/_fixture/tick",body={})
            assert blocked=={"ok":True,"scanned":3,"created":0,"blocked":3,"replayed":0},blocked
            blocked_rule=call("blocked_rule_read","GET",rp,token=mgmt)
            assert blocked_rule["blockedCode"]=="SCHEDULE_AUTHORIZATION_CHANGED"
            call("shared_rule_remove","DELETE",rp,token=mgmt,revision=blocked_rule["revision"],expected=204)
            call("restricted_remove_project_denied","DELETE","/api/v1/projects/prj_native_remove",token=project["key"],revision=1,expected=403)
            call("owner_project_remove","DELETE","/api/v1/projects/prj_native_remove",token=mgmt,revision=1,expected=204)
            invite=call("bearer_member_invite","POST",f"/api/v1/workspaces/{OWNER}/invites",token=mgmt,
                body={"role":"editor"},expected=201)
            application=call("cookie_applicant_apply","POST","/api/v1/workspace-invitations/accept",cookie="applicant",
                body={"token":invite["token"]},expected=202)
            inbox=call("bearer_member_inbox","GET","/api/v1/workspace-invitation-requests",token=mgmt)
            assert len(inbox["items"])==1
            review=f"/api/v1/workspaces/{OWNER}/invites/{invite['id']}/requests/{application['request']['id']}/approve"
            approved=call("bearer_member_approve","POST",review,token=mgmt,body={},revision=1)
            assert approved["request"]["status"]=="approved"
            changed=call("bearer_member_change_role","PATCH",f"/api/v1/workspaces/{OWNER}/members/{APPLICANT}",
                token=mgmt,body={"role":"viewer"},revision=1)
            call("bearer_member_remove","DELETE",f"/api/v1/workspaces/{OWNER}/members/{APPLICANT}",
                token=mgmt,revision=changed["revision"],expected=204)
            call("owner_immutable","DELETE",f"/api/v1/workspaces/{OWNER}/members/{OWNER}",token=mgmt,revision=1,expected=403)
            call("cookie_origin_rejected","POST","/api/v1/expenses",cookie="owner",body=expense(),
                idempotency="native-untrusted",trusted=False,expected=403)
            replay=call("expense_replay_after_soft_delete","POST","/api/v1/expenses",token=mgmt,body=expense(),
                idempotency="native-shared-expense",expected=201)
            assert replay==shared
            project_current=call("project_blocked_rule_read","GET","/api/v1/expense-recurring-rules/"+project_rule["id"],token=mgmt)
            call("project_rule_remove","DELETE","/api/v1/expense-recurring-rules/"+project_rule["id"],token=mgmt,
                revision=project_current["revision"],expected=204)
            final=call("final_journal","GET","/_fixture/final")
            assert final["journalHealthy"] and final["schemaVersion"]==schema and final["scope"]==evidence["singletonScope"]
            evidence["passed"]=True
    except Exception as error: evidence["failure"]=str(error)
    finally:
        if process and process.poll() is None:
            os.killpg(process.pid,signal.SIGTERM)
            try: process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL); process.wait(timeout=5)
        observations,groups=[],[]
        for line in log_path.read_text(errors="replace").splitlines() if log_path.exists() else []:
            if line.startswith('{"nativeRestParityRequest":'): observations.append(json.loads(line)["nativeRestParityRequest"])
            elif line.startswith('{"nativeRestParityMeta":'): groups.append(json.loads(line)["nativeRestParityMeta"])
        evidence.update(localHttpRequestsAttempted=attempted,localHttpRequestsCompleted=len(cases),cases=cases,nativeRequests=observations,
            nativeStatements=sum(group["statements"] for group in groups),
            meteredNativeRowsRead=sum(group["rowsRead"] for group in groups if group["phase"]=="metered"),
            meteredNativeRowsWritten=sum(group["rowsWritten"] for group in groups if group["phase"]=="metered"),
            integrityProbeNativeRowsRead=sum(group["rowsRead"] for group in groups if group["phase"]=="integrity-probe"),
            nativeAttemptsObserved=sorted({value for group in groups for value in group["nativeAttempts"]},key=str),
            runtimeStopped=process is None or process.poll() is not None,integrityProbesReadOnlyAndOutsideJournal=True)
        if evidence["passed"]:
            try:
                assert len(observations)==len(cases)<=HTTP_CAP
                assert evidence["meteredNativeRowsRead"]<=READ_CAP and evidence["meteredNativeRowsWritten"]<=WRITE_CAP
                assert evidence["integrityProbeNativeRowsRead"]<=PROBE_READ_CAP
                by_name={case["name"]:obs for case,obs in zip(cases,observations)}
                generated=by_name["native_tick_generate"]["integrity"]
                assert generated["tables"]["expenses"]["rows"]==6
                assert generated["tables"]["expense_recurring_occurrences"]["rows"]==3
                assert all(rule["keyGrantPresent"] for rule in generated["rules"])
                blocked=by_name["native_tick_revoked_and_member_blocked"]["integrity"]
                assert all(rule["status"]=="blocked" and rule["blockedCode"]=="SCHEDULE_AUTHORIZATION_CHANGED" for rule in blocked["rules"])
                assert blocked["tables"]["expenses"]==generated["tables"]["expenses"]
                assert blocked["tables"]["expense_recurring_occurrences"]==generated["tables"]["expense_recurring_occurrences"]
                assert blocked["tables"]["expense_events"]==generated["tables"]["expense_events"]
                assert blocked["usage"]==by_name["advance_fixture_clock_once"]["integrity"]["usage"]
                assert all(actor["actor"].endswith((":"+OWNER,":"+EDITOR)) for actor in generated["expenseActors"])
                for case,obs in zip(cases,observations):
                    assert obs["journalActualDeltasMatchRawMeta"] and obs["journalHealthy"] and obs["providerCalls"]==0
                    assert obs["integrity"]["cardinalityVerified"]
                    if case["status"]>=400 or case["name"]=="expense_replay_after_soft_delete":
                        assert sum(group["rowsWritten"] for group in obs["meteredGroups"])==0,case["name"]
                final=cases[-1]["result"]
                assert final["counters"]["actual_read"]==evidence["meteredNativeRowsRead"]
                assert final["counters"]["actual_written"]==evidence["meteredNativeRowsWritten"]
                assert final["counters"]["reserved_read"]>=final["counters"]["actual_read"]
                evidence.update(nativeAttemptMetadataMissing=None in evidence["nativeAttemptsObserved"],
                    missingAttemptsRetainReadReservations=True)
                evidence.update(bearerRecurringGrantNativeAcceptance=True,bearerMemberManagementNativeAcceptance=True,
                    bearerExpenseCrudAndReplayNativeAcceptance=True,ownerProjectRemoveNativeAcceptance=True,
                    activityScopeNativeAdmission=True,freeGuidePayloadNativeAcceptance=True,publicDtosDoNotLeakKeyHash=True)
            except Exception as error:
                evidence["passed"]=False; evidence["failure"]="Native observation assertion failed: "+str(error)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(evidence,indent=2,ensure_ascii=False)+"\n")
        print(json.dumps({key:evidence.get(key) for key in ("passed","failure","localHttpRequestsCompleted",
            "nativeStatements","meteredNativeRowsRead","meteredNativeRowsWritten","integrityProbeNativeRowsRead","runtimeStopped")}))
    if not evidence["passed"]: raise SystemExit("Native REST parity failed; preserve evidence, no retry or reset")

if __name__=="__main__": main()
