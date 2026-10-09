"""Finite local native invitation approval journey through the canonical application.

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
HTTP_CAP = 30
BUNDLE_SHA256 = "3c3fd5a4179230e21e018e28b0e735e7c4abd8fe8e4ff39d2fd6bb2ea3a2e260"
OWNER, REJECTED, APPROVED = ("usr_github_760001", "usr_github_760002", "usr_github_760003")

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

OWNER, REJECTED, APPROVED = "usr_github_760001", "usr_github_760002", "usr_github_760003"
FINANCE = {"ledger_projects","ledger_project_repositories","expense_categories","expenses",
    "expense_events","expense_create_idempotency","expense_recurring_rules",
    "expense_recurring_occurrences","expense_suggestion_budget","expense_suggestion_events"}
BUSINESS = ("workspace_members","workspace_invites","workspace_join_requests",
    "workspace_events","ledger_plan_usage")
provider_calls = []


class ForbiddenGateway:
    def __init__(self, env):
        pass
    def __getattr__(self, name):
        async def blocked(*args, **kwargs):
            provider_calls.append(name)
            raise AssertionError("Provider transport excluded from local invitation proof")
        return blocked


canonical.WorkerGitHubGateway = ForbiddenGateway
canonical.WorkerCreemGateway = ForbiddenGateway


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
        print(json.dumps({"nativeInviteRuntimeMeta":group}))
        return results


async def commands(native, values):
    return await native.batch([native.prepare(sql).bind(*params) for sql,params in values])


def seed(now):
    values = []
    for role,identity in (("owner",OWNER),("rejected",REJECTED),("approved",APPROVED)):
        user = {"id":identity,"githubId":identity.removeprefix("usr_github_"),
            "githubLogin":"synthetic-"+role,"name":"Synthetic "+role,"createdAt":now-1000,
            "billing":{"plan":"free"}}
        if role=="owner":
            user["billing"]={"provider":"creem","plan":"pro","status":"active",
                "subscriptionId":"sub_local_invitation_proof","interval":"month",
                "currentPeriodStart":now-1000,"currentPeriodEnd":now+86400}
        payload = encode_record("users",identity,user)
        values.append(("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
            (record_name("users",identity),payload,now)))
        session = "invite-native-"+role
        values.append(("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
            (record_name("sessions",session),encode_record("sessions",session,
                {"id":session,"userId":identity,"expiresAt":now+3600}),now)))
        values.extend(initialize_account(owner_id=identity,account_snapshot=payload,now=now))
    values.extend([
        ("""INSERT INTO expense_categories(id,owner_id,name,revision,created_at,updated_at)
            VALUES(?,?,?,1,?,?)""",("cat_native_invitation",OWNER,"Protected operating costs","local","local")),
        ("""INSERT INTO expenses(id,owner_id,target_kind,project_id,category_id,occurred_on,
            amount_minor,currency,purpose,note,revision,created_at,updated_at)
            VALUES(?,?,'shared',NULL,?,'2026-10-09',1250,'USD',?,NULL,1,?,?)""",
            ("exp_native_invitation",OWNER,"cat_native_invitation","Protected historical expense","local","local")),
    ])
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
    return {"tables":data,
        "activeMembers":sum(row["removed_at"] is None for row in rows["workspace_members"]),
        "members":[{"userId":row["user_id"],"role":row["role"],"revision":row["revision"],
            "removed":row["removed_at"] is not None} for row in rows["workspace_members"]],
        "invitations":[{"id":row["id"],"status":row["status"],"revision":row["revision"],
            "recipientNull":row["github_recipient_id"] is None and row["github_login"] is None,
            "acceptedBy":row["accepted_by_user_id"]} for row in rows["workspace_invites"]],
        "requests":[{"id":row["id"],"userId":row["applicant_user_id"],"status":row["status"],
            "revision":row["revision"]} for row in rows["workspace_join_requests"]],
        "eventActions":[row["action"] for row in rows["workspace_events"]],
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
            self.env.FIXTURE_JOURNAL.idFromName("local-invite-approval-runtime")).fetch(forwarded)


class FixtureJournal(DurableObject):
    def __init__(self, ctx, env):
        self.ctx,self.env=ctx,env
    async def fetch(self, request):
        path=urlsplit(str(request.url)).path
        call=int(request.headers.get("x-native-fixture-call") or "0")
        count=int(await self.ctx.storage.get("httpCalls") or 0)+1
        assert count<=30 and call==count
        await self.ctx.storage.put("httpCalls",count)
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
            await meter.batch([meter.prepare(sql).bind(*params) for sql,params in seed(int(time.time()))])
            assert meter.accounted_outcome()
            journal.finish(ticket,now=time.time())
            response=Response.json({"passed":True,"syntheticCookieAccounts":3,
                "seedThroughCanonicalMeter":True,"canonicalFreshSchemaVersion":8})
        elif path=="/_fixture/final":
            state=journal.snapshot()
            assert state["active"] is None and state["stopped"] is None
            response=Response.json({"passed":True,"journalHealthy":True,
                "schemaVersion":state["schema_version"],"stateStorageVersion":state["state_storage_version"],
                "counters":{key:state[key] for key in
                    ("requests","reserved_read","reserved_written","actual_read","actual_written")},
                "nativeAttemptsFabricated":False})
        else:
            assert path.startswith("/api/v1/") and before["schema_ready"] is True
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
        assert not provider_calls and after["active"] is None and after["stopped"] is None
        business=await business_snapshot(native)
        print(json.dumps({"nativeInviteRuntimeRequest":{"call":call,"path":path,"method":request.method,
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
        return {key:redacted(item) for key,item in value.items() if key.lower() not in {"token","url","invitationurl"}}
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
    config={"name":"pullwise-invite-approval-runtime-local-only","main":"src/entry.py",
        "compatibility_date":"2026-09-23","compatibility_flags":["python_workers"],
        "workers_dev":False,"preview_urls":False,"routes":[],
        "vars":{"PULLWISE_MODE":"local","PULLWISE_D1_ACCESS_ENABLED":"1",
            "PULLWISE_APP_URL":base,"PULLWISE_ALLOWED_ORIGINS":base,
            "PULLWISE_JEV_ENABLED":"0","PULLWISE_JEV_QUALITY_EVALUATED":"0"},
        "d1_databases":[{"binding":"DB","database_name":"invite-approval-runtime-local-only",
            "database_id":"00000000-0000-0000-0000-000000000038","remote":False}],
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
            "cloudflare_workspaces.py","cloudflare_native_d1.py","cloudflare_preview_budget.py",
            "cloudflare_plan_limits.py","cloudflare_validation_budget.py"}},
        "runtimeProvenance":{"workerdSha256":sha256(binary),"runtimeBundleSha256":BUNDLE_SHA256,
            "miniflareD1SourceSha256":sha256(WORKER/"node_modules/miniflare/dist/src/workers/d1/database.worker.js")},
        "productMeterAndCommercialPlanAdapter":True,"trustedOriginCheckedByCanonicalApplication":True,
        "syntheticCookieAccounts":3,"remoteD1Operations":0,"remoteApplicationRequests":0,
        "realProviderRequests":0,"realAccounts":0,"realPayments":0,"clientRetries":0,
        "localHttpCap":HTTP_CAP,"nativeAttemptsFabricated":False,
        "scope":"Synthetic authenticated native approval flow; real-account/browser/remote acceptance separate"}
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
                    raise AssertionError("Native invitation Worker startup failed")
                if "Ready on http://127.0.0.1:"+str(args.port) in log_path.read_text(errors="replace"):
                    try:
                        with socket.create_connection(("127.0.0.1",args.port),timeout=.2):
                            break
                    except OSError:
                        pass
                time.sleep(.2)
            else:
                raise AssertionError("Native invitation Worker startup timed out")
            opener=build_opener(ProxyHandler({}),NoRedirect())

            def call(name,method,path,actor=None,body=None,expected=200,workspace=False,revision=None,trusted=True):
                nonlocal attempted
                attempted+=1
                assert attempted<=HTTP_CAP
                headers={"Content-Type":"application/json","X-Native-Fixture-Call":str(attempted)}
                if actor:
                    headers["Cookie"]="pw_session=invite-native-"+actor
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

            call("setup","POST","/_fixture/setup",body={},expected=200)
            denied=call("untrusted_origin","POST",f"/api/v1/workspaces/{OWNER}/invites","owner",{"role":"editor"},403,trusted=False)
            assert denied["error"]["code"]=="UNTRUSTED_ORIGIN"
            invite=call("role_only_create","POST",f"/api/v1/workspaces/{OWNER}/invites","owner",{"role":"editor"},201)
            assert invite["recipient"] is None and invite["role"]=="editor"
            token=invite["token"]
            payload={"token":token}
            call("login_required","POST","/api/v1/workspace-invitations/preview",body=payload,expected=401)
            preview=call("applicant_preview","POST","/api/v1/workspace-invitations/preview","rejected",payload)
            assert preview["request"] is None and not any(preview["workspace"]["permissions"].values())
            pending=call("first_request","POST","/api/v1/workspace-invitations/accept","rejected",payload,202)
            assert pending["request"]["status"]=="pending" and pending["request"]["revision"]==1
            repeat=call("repeat_request","POST","/api/v1/workspace-invitations/accept","rejected",payload)
            assert repeat==pending
            call("pending_ledger_denied","GET","/api/v1/expenses","rejected",expected=404,workspace=True)
            own_inbox=call("applicant_inbox_isolated","GET","/api/v1/workspace-invitation-requests","rejected")
            assert own_inbox["items"]==[]
            inbox=call("inviter_identity_notification","GET","/api/v1/workspace-invitation-requests","owner")
            assert len(inbox["items"])==1 and inbox["items"][0]["applicant"]=={
                "userId":REJECTED,"name":"Synthetic rejected","githubLogin":"synthetic-rejected"}
            scoped=call("scoped_identity_notification","GET",f"/api/v1/workspaces/{OWNER}/join-requests","owner")
            assert scoped["items"]==inbox["items"]
            review=f"/api/v1/workspaces/{OWNER}/invites/{invite['id']}/requests/"
            reject=call("inviter_reject","POST",review+pending["request"]["id"]+"/reject","owner",{},revision=1)
            assert reject["request"]["status"]=="rejected" and reject["request"]["revision"]==2
            rejected_retry=call("rejected_retry_no_write","POST","/api/v1/workspace-invitations/accept","rejected",payload)
            assert rejected_retry["request"]["status"]=="rejected"
            call("rejected_ledger_denied","GET","/api/v1/expenses","rejected",expected=404,workspace=True)
            second=call("second_applicant_request","POST","/api/v1/workspace-invitations/accept","approved",payload,202)
            assert second["request"]["applicant"]["userId"]==APPROVED
            approved=call("inviter_approve","POST",review+second["request"]["id"]+"/approve","owner",{},revision=1)
            assert approved["request"]["status"]=="approved" and approved["workspace"]["role"]=="editor"
            ledger=call("approved_ledger_access","GET","/api/v1/expenses","approved",workspace=True)
            assert any(item["id"]=="exp_native_invitation" for item in ledger["items"])
            replay=call("consumed_link_replay","POST","/api/v1/workspace-invitations/accept","approved",payload,410)
            assert replay["error"]["code"]=="INVITATION_ACCEPTED"
            accepted=call("accepted_preview_recovery","POST","/api/v1/workspace-invitations/preview","approved",payload)
            assert accepted["status"]=="accepted" and accepted["workspace"]["permissions"]["writeExpenses"] is True
            call("remove_member","DELETE",f"/api/v1/workspaces/{OWNER}/members/{APPROVED}","owner",expected=204,revision=1)
            call("removed_ledger_denied","GET","/api/v1/expenses","approved",expected=404,workspace=True)
            removed=call("removed_old_preview_denied","POST","/api/v1/workspace-invitations/preview","approved",payload,410)
            assert removed["error"]["code"]=="INVITATION_ACCEPTED"
            final=call("final_journal","GET","/_fixture/final")
            assert final["passed"] and final["journalHealthy"] and final["schemaVersion"]==8
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
            if line.startswith('{"nativeInviteRuntimeRequest":'):
                observations.append(json.loads(line)["nativeInviteRuntimeRequest"])
            elif line.startswith('{"nativeInviteRuntimeMeta":'):
                groups.append(json.loads(line)["nativeInviteRuntimeMeta"])
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
                for name in ("applicant_preview","first_request","repeat_request","pending_ledger_denied",
                    "inviter_reject","rejected_retry_no_write","rejected_ledger_denied","second_applicant_request"):
                    assert by_name[name]["business"]["activeMembers"]==0
                for name in ("applicant_preview","first_request","repeat_request","pending_ledger_denied",
                    "rejected_ledger_denied","removed_ledger_denied","removed_old_preview_denied"):
                    assert by_name[name]["resourceFinanceTablesRead"]==[],name
                for current,previous in (("repeat_request","first_request"),
                        ("rejected_retry_no_write","inviter_reject"),
                        ("consumed_link_replay","approved_ledger_access"),
                        ("removed_old_preview_denied","removed_ledger_denied")):
                    assert by_name[current]["business"]==by_name[previous]["business"],current
                    assert sum(group["rowsWritten"] for group in by_name[current]["meteredGroups"])==0,current
                assert by_name["inviter_approve"]["business"]["activeMembers"]==1
                final_business=observations[-1]["business"]
                assert final_business["activeMembers"]==0 and final_business["commercialWrites"]==5
                assert final_business["commercialProjects"]==0 and final_business["commercialRecords"]==1
                assert sorted(final_business["eventActions"])==sorted([
                    "invite","request_join","reject_join","request_join","approve_join","remove_member"])
                assert final_business["members"]==[{"userId":APPROVED,"role":"editor","revision":2,"removed":True}]
                assert sum(group["rowsWritten"] for group in by_name["untrusted_origin"]["meteredGroups"])==0
                assert by_name["untrusted_origin"]["meteredGroups"]==[]
                evidence.update(noAccessBeforeApproval=True,noMembershipBeforeApproval=True,
                    noFinancialResourceReadsWhilePendingOrDenied=True,repeatAndReplayNoWrites=True,
                    inviterInboxIdentifiesActualApplicant=True,applicantInboxIsolated=True,
                    approvalGrantsMembershipAndLedgerAccess=True,rejectionGrantsNoAccess=True,
                    removedMembershipOldLinkCannotRecover=True,commercialWrites=5,workspaceAuditEvents=6,
                    seededHistoricalExpenseCapacityPreserved=True,
                    allJournalActualDeltasMatchRawMeta=True)
            except Exception as error:
                evidence["passed"]=False
                evidence["failure"]="Native observation assertion failed: "+str(error)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(evidence,indent=2,ensure_ascii=False)+"\n")
        print(json.dumps({key:evidence.get(key) for key in ("passed","failure","localHttpRequestsCompleted",
            "nativeStatements","meteredNativeRowsRead","meteredNativeRowsWritten","runtimeStopped")},ensure_ascii=False))
    if not evidence["passed"]:
        raise SystemExit("Native approval journey failed; preserve local fixture and evidence without retry")


if __name__=="__main__":
    main()
