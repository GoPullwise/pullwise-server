"""Finite local native recurring posting, capacity recovery and inbox journey.

Only synthetic local D1/DO state is used. The fixed clock advances once;
providers, remote bindings, redirects, client retries and deployment are absent.
Canonical application modules and the original product meter run unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
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
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "cloudflare/server"
spec = importlib.util.spec_from_file_location("rest_parity_native", ROOT / "scripts/check-rest-parity-native-runtime.py")
parity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parity)
OWNER, EDITOR = parity.OWNER, parity.EDITOR
FIXTURE_NOW = parity.FIXTURE_NOW
HTTP_CAP, FIXED_HTTP_COUNT = 50, 22
READ_CAP, WRITE_CAP, PROBE_READ_CAP = 250000, 25000, 10000
RESOURCE = "/api/v1/expense-recurring-rules"

SEED_AND_SNAPSHOT = r'''
def seed(now):
    values=[]
    for role,identity in (("owner",OWNER),("editor",EDITOR)):
        user={"id":identity,"name":"Synthetic recurring "+role,"createdAt":now-1000,
            "billing":{"plan":"free"}}
        payload=encode_record("users",identity,user)
        session="rest-parity-native-"+role
        values.extend([("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
            (record_name("users",identity),payload,now)),
            ("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
            (record_name("sessions",session),encode_record("sessions",session,
                {"id":session,"userId":identity,"expiresAt":now+86400000}),now)),
            *initialize_account(owner_id=identity,account_snapshot=payload,now=now),
            ("INSERT INTO expense_categories(id,owner_id,name,revision,created_at,updated_at) VALUES(?,?,'Hosting',1,'local','local')",
                ("cat_"+role,identity))])
    for index in range(2):
        values.append(("""INSERT INTO expenses(id,owner_id,target_kind,project_id,category_id,
            occurred_on,amount_minor,currency,purpose,revision,created_at,updated_at)
            VALUES(?,?,'shared',NULL,'cat_owner',?,100,'USD','Synthetic seed',1,'local','local')""",
            ("exp_seed_"+str(index),OWNER,"2026-01-0"+str(index+1))))
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
    state=journal.snapshot()
    assert len(rows["expense_recurring_pending"])<=10
    assert len(rows["expenses"])<=6 and len(rows["expense_recurring_rules"])<=3
    assert tables["d1_command_guard"]["rows"]==0
    return {"tables":tables,"pending":[{"ruleId":row["rule_id"],"periodKey":row["period_key"],
        "date":row["scheduled_on"],"notificationState":row["notification_state"]}
        for row in rows["expense_recurring_pending"]],
        "expenses":[{"id":row["id"],"owner":row["owner_id"],"date":row["occurred_on"],
            "amountMinor":row["amount_minor"],"deleted":row["deleted_at"] is not None} for row in rows["expenses"]],
        "rules":[{"id":row["id"],"nextDate":row["next_occurrence_on"],"status":row["status"]}
            for row in rows["expense_recurring_rules"]],
        "cardinalityVerified":state.get("product_data_verified"),"cardinality":state["product_data"]["rows"]}
'''

BASE_ENTRY = re.sub(r"\b(?:100000|10000|5000)\b",
    lambda match: {"100000":"250000","10000":"25000","5000":"10000"}[match.group()], parity.ENTRY)
ENTRY = BASE_ENTRY[:BASE_ENTRY.index("def seed(now):")] + SEED_AND_SNAPSHOT + BASE_ENTRY[BASE_ENTRY.index("class Default(canonical.Default):"):]
ENTRY = ENTRY.replace('"ledger_activity_events","ledger_projects"', '"expense_recurring_pending","ledger_activity_events","ledger_projects"')
ENTRY = ENTRY.replace('"syntheticCookieAccounts":3', '"syntheticCookieAccounts":2')
ENTRY = ENTRY.replace("FIXTURE_NOW+32*86400", "FIXTURE_NOW+370*86400")
ENTRY = ENTRY.replace('get("fixtureTicks") or 0)<2', 'get("fixtureTicks") or 0)<4')
ENTRY = ENTRY.replace("nativeRestParity", "nativeRecurringPending")


def journey(call):
    def draft(category, *, start="2026-09-15", frequency="monthly", **changes):
        schedule={"frequency":frequency,"day":15,"timezone":"Asia/Shanghai","startOn":start}
        if frequency=="yearly": schedule["month"]=9
        return {"target":{"kind":"shared"},"categoryId":category,"amount":"12.34",
            "currency":"USD","purpose":"Synthetic hosting","schedule":schedule,**changes}
    call("setup","POST","/_fixture/setup",body={})
    recorded=call("historical_start_posts_one","POST",RESOURCE,cookie="editor",
        body=draft("cat_editor",frequency="yearly"),idempotency="native-recorded",expected=201)
    assert recorded["nextOccurrenceOn"]=="2027-09-15" and not recorded["pendingOccurrences"]
    body=draft("cat_owner")
    failed=call("full_start_retained","POST",RESOURCE,cookie="owner",body=body,
        idempotency="native-pending",expected=201)
    assert failed["nextOccurrenceOn"]=="2026-10-15" and len(failed["pendingOccurrences"])==1
    assert call("creation_replay","POST",RESOURCE,cookie="owner",body=body,
        idempotency="native-pending",expected=201)==failed
    future=call("future_start_does_not_post","POST",RESOURCE,cookie="owner",
        body=draft("cat_owner",start="2027-12-01"),idempotency="native-future",expected=201)
    assert future["nextOccurrenceOn"]=="2027-12-01" and not future["pendingOccurrences"]
    inbox=call("no_email_falls_back_to_inbox","GET","/api/v1/recurring-expense-notifications",cookie="owner")
    assert len(inbox["items"])==1 and inbox["items"][0]["scheduledOn"]=="2026-09-15"
    denied=call("full_retry_preserves_pending","PATCH",RESOURCE+"/"+failed["id"],cookie="owner",
        body={"retryPeriodKey":"M2026-09"},revision=failed["revision"],expected=403)
    assert denied["error"]["code"]=="RECORD_LIMIT"
    call("advance_fixture_clock_once","POST","/_fixture/advance-clock",body={})
    projected=call("legacy_due_projects_future","GET",RESOURCE+"/"+failed["id"],cookie="owner")
    assert projected["nextOccurrenceOn"]>"2027-10-14" and projected["awaitingSync"]
    for index in range(4): call("bounded_tick_"+str(index),"POST","/_fixture/tick",body={})
    current=call("first_ten_pending_retained","GET",RESOURCE+"/"+failed["id"],cookie="owner")
    assert len(current["pendingOccurrences"])==10 and current["nextOccurrenceOn"]>"2027-10-14"
    assert current["pendingOccurrences"][0]["scheduledOn"]=="2026-09-15"
    assert current["pendingOccurrences"][-1]["scheduledOn"]=="2027-06-15"
    call("manual_cleanup_frees_one_slot","DELETE","/api/v1/expenses/exp_seed_0",cookie="owner",revision=1,expected=204)
    recovered=call("manual_retry_records_frozen_period","PATCH",RESOURCE+"/"+failed["id"],cookie="owner",
        body={"retryPeriodKey":"M2026-09"},revision=current["revision"])
    assert len(recovered["pendingOccurrences"])==9
    replay=call("manual_retry_replay_no_extra_expense","PATCH",RESOURCE+"/"+failed["id"],cookie="owner",
        body={"retryPeriodKey":"M2026-09"},revision=recovered["revision"])
    assert replay==recovered
    call("enable_existing_oldest_replacement","PATCH","/api/v1/account/expense-retention",cookie="owner",
        body={"autoRemoveOldestExpense":True},revision=1)
    second=call("retry_uses_atomic_oldest_replacement","PATCH",RESOURCE+"/"+failed["id"],cookie="owner",
        body={"retryPeriodKey":"M2026-10"},revision=recovered["revision"])
    assert len(second["pendingOccurrences"])==8
    editor_records=call("each_period_is_independent","GET","/api/v1/expenses?target=shared",cookie="editor")
    assert len(editor_records["items"])==2
    assert {item["amount"] for item in editor_records["items"]}=={"12.34"}
    assert {item["occurredOn"] for item in editor_records["items"]}=={"2026-09-15","2027-09-15"}
    inbox=call("recovered_notices_leave_inbox","GET","/api/v1/recurring-expense-notifications",cookie="owner")
    assert len(inbox["items"])==8
    call("final_journal","GET","/_fixture/final")


def verify(cases,observations):
    assert len(cases)==len(observations)==FIXED_HTTP_COUNT
    previous=None
    for case,obs in zip(cases,observations):
        current=obs["integrity"]
        assert obs["journalActualDeltasMatchRawMeta"] and obs["journalHealthy"] and obs["providerCalls"]==0
        assert current["cardinalityVerified"]
        if case["method"]=="GET" or case["status"]>=400 or case["name"] in {"creation_replay","manual_retry_replay_no_extra_expense"}:
            assert sum(group["rowsWritten"] for group in obs["meteredGroups"])==0,case["name"]
            assert current["tables"]==previous["tables"],case["name"]
        previous=current
    by_name={case["name"]:obs["integrity"] for case,obs in zip(cases,observations)}
    assert by_name["historical_start_posts_one"]["tables"]["expenses"]["rows"]==3
    assert by_name["full_start_retained"]["tables"]["expenses"]["rows"]==3
    cap=by_name["first_ten_pending_retained"]
    assert len(cap["pending"])==10 and all(item["notificationState"]=="inbox" for item in cap["pending"])
    assert cap["tables"]["expenses"]["rows"]==4
    assert previous["tables"]["expense_recurring_occurrences"]["rows"]==4
    owner_active=[item for item in previous["expenses"] if item["owner"]==OWNER and not item["deleted"]]
    assert len(owner_active)==2 and {item["amountMinor"] for item in owner_active}=={1234}
    assert {item["date"] for item in owner_active}=={"2026-09-15","2026-10-15"}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--port",type=int,default=8935)
    parser.add_argument("--tzdata-cache",type=Path)
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
    package_root,package_manifest=parity.pinned_tzdata(args.tzdata_cache)
    shutil.copytree(package_root/"tzdata",directory/"python_modules/tzdata",dirs_exist_ok=True,ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
    dependencies=("pyproject.toml","uv.lock","pylock.toml","package.json","package-lock.json")
    for name in dependencies: shutil.copy2(WORKER/name,directory/name)
    (directory/"node_modules").symlink_to(WORKER/"node_modules",target_is_directory=True)
    base=f"http://127.0.0.1:{args.port}"
    config={"name":"pullwise-recurring-pending-native-local-only","main":"src/entry.py",
        "compatibility_date":"2026-09-23","compatibility_flags":["python_workers"],
        "workers_dev":False,"preview_urls":False,"routes":[],
        "vars":{"PULLWISE_MODE":"preview","PULLWISE_D1_ACCESS_ENABLED":"1",
            "PULLWISE_PREVIEW_PRODUCT_ENABLED":"1","PULLWISE_RECURRING_EXPENSES_ENABLED":"1",
            "PULLWISE_APP_URL":"https://preview.pull-wise.com","PULLWISE_ALLOWED_ORIGINS":base,
            "PULLWISE_CREEM_API_BASE_URL":"https://test-api.creem.io",
            "PULLWISE_PLAN_LIMITS_JSON":json.dumps({"free":{"records":2,"writesPerMinute":60}}),
            "PULLWISE_JEV_SUGGESTIONS_ENABLED":"0","PULLWISE_JEV_SUGGESTIONS_EVALUATED":"0"},
        "d1_databases":[{"binding":"DB","database_name":"recurring-pending-native-local-only",
            "database_id":"00000000-0000-0000-0000-000000000045","remote":False}],
        "durable_objects":{"bindings":[{"name":"VALIDATION_BUDGET","class_name":"ValidationBudget"}]},
        "migrations":[{"tag":"local-fixture-v1","new_sqlite_classes":["ValidationBudget"]}]}
    config_path=directory/"wrangler.jsonc"; config_path.write_text(json.dumps(config,indent=2)+"\n")
    bundle=Path("/workspace/.cache/pullwise-pyodide/pyodide_314.0.6_2026-08-17_6.capnp.bin")
    if not bundle.is_file() or parity.sha256(bundle)!=parity.BUNDLE_SHA256:
        raise SystemExit("Official pinned Workerd Python cache missing or invalid")
    binary=WORKER/"node_modules/@cloudflare/workerd-linux-64/bin/workerd"
    wrapper=directory/"workerd-local-cache.sh"
    wrapper.write_text("#!/bin/sh\nexec "+shlex.quote(str(binary))+' "$@" --pyodide-bundle-disk-cache-dir=/workspace/.cache/pullwise-pyodide\n'); wrapper.chmod(0o700)
    env={**os.environ,"XDG_CACHE_HOME":"/workspace/.cache","XDG_CONFIG_HOME":"/workspace/.config",
        "UV_CACHE_DIR":"/workspace/.cache/uv","UV_PYTHON_INSTALL_DIR":"/workspace/.python",
        "UV_SYSTEM_CERTS":"true","NPM_CONFIG_CACHE":str(directory/"npm-cache"),
        "WRANGLER_SEND_METRICS":"false","MINIFLARE_WORKERD_PATH":str(wrapper),
        "WRANGLER_LOG_PATH":str(directory/"wrangler-debug.log")}
    files=sorted((source/"pullwise_server").glob("*.py"))
    manifest=[(str(path.relative_to(source)),parity.sha256(path)) for path in files]
    evidence={"passed":False,"date":"2026-10-10","localOnly":True,
        "nativePythonFFI":True,"nativeDurableObjectSQLite":True,"canonicalDefaultAndValidationBudget":True,
        "singletonScope":"pullwise-s17-s18-2026-09-28","fixtureClockStart":FIXTURE_NOW,"fixtureClockAdvanceSeconds":370*86400,
        "canonicalApplicationEntrySha256":parity.sha256(source/"application_entry.py"),
        "canonicalSourceTreeSha256":hashlib.sha256(json.dumps(manifest,separators=(",",":")).encode()).hexdigest(),
        "canonicalSourceFileCount":len(files),"fixtureEntrySha256":parity.sha256(source/"entry.py"),
        "sourceSha256":dict(manifest),"dependencySha256":{name:parity.sha256(directory/name) for name in dependencies},
        "cachedTzdataFileManifestSha256":hashlib.sha256(json.dumps(package_manifest,separators=(",",":")).encode()).hexdigest(),
        "runtimeProvenance":{"workerdSha256":parity.sha256(binary),"runtimeBundleSha256":parity.BUNDLE_SHA256,
            "miniflareD1SourceSha256":parity.sha256(WORKER/"node_modules/miniflare/dist/src/workers/d1/database.worker.js")},
        "productMeterAndCommercialPlanAdapter":True,"nativeAttemptsFabricated":False,
        "syntheticCookieAccounts":2,"syntheticKeysMaximum":0,"syntheticSeedPhysicalExpenses":2,
        "syntheticSeedActiveExpenses":2,"syntheticPhysicalExpensesMaximum":6,
        "remoteD1Operations":0,"remoteApplicationRequests":0,"realProviderRequests":0,"clientRetries":0,
        "fixedHttpJourney":FIXED_HTTP_COUNT,"localHttpCap":HTTP_CAP,
        "localReservedRowCaps":{"read":READ_CAP,"written":WRITE_CAP},"localIntegrityProbeReadCap":PROBE_READ_CAP,
        "scope":"Native local expense recurring-pending, active capacity and original financial/accounting fences; remote/browser/real accounts separate"}
    attempted,cases,process=0,[],None
    log_path=directory/"runtime.log"
    try:
        with log_path.open("w") as log:
            process=subprocess.Popen(["node",str(WORKER/"node_modules/wrangler/bin/wrangler.js"),"dev","--local","--config",str(config_path),
                "--ip","127.0.0.1","--port",str(args.port),"--inspector-port",str(args.port+1000),
                "--persist-to",str(directory/"state")],cwd=directory,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            deadline=time.monotonic()+120
            while time.monotonic()<deadline:
                if process.poll() is not None: raise AssertionError("Native recurring-pending Worker startup failed")
                if "Ready on http://127.0.0.1:"+str(args.port) in log_path.read_text(errors="replace"):
                    try:
                        with socket.create_connection(("127.0.0.1",args.port),timeout=.2): break
                    except OSError: pass
                time.sleep(.2)
            else: raise AssertionError("Native recurring-pending Worker startup timed out")
            opener=build_opener(ProxyHandler({}),parity.NoRedirect())
            def call(name,method,path,*,cookie=None,token=None,body=None,expected=200,revision=None,workspace=None,idempotency=None,trusted=True):
                nonlocal attempted
                attempted+=1; assert attempted<=HTTP_CAP
                headers={"X-Native-Fixture-Call":str(attempted),"Content-Type":"application/json"}
                if cookie: headers["Cookie"]="pw_session=rest-parity-native-"+cookie
                if token: headers["Authorization"]="Bearer "+token
                if cookie and method in {"POST","PATCH","DELETE"}: headers["Origin"]=base if trusted else "https://untrusted.example.test"
                if workspace: headers["X-Pullwise-Workspace"]=workspace
                if revision is not None: headers["If-Match"]='"'+str(revision)+'"'
                if idempotency: headers["Idempotency-Key"]=idempotency
                request=Request(base+path,method=method,headers=headers,data=json.dumps(body,separators=(",",":")).encode() if body is not None else None)
                try: response=opener.open(request,timeout=60)
                except HTTPError as error: response=error
                with response:
                    raw=response.read(); payload=json.loads(raw) if raw else None
                    assert "_api_key_hash" not in raw.decode(),name
                    cases.append({"name":name,"method":method,"path":path,"status":response.status,"result":parity.redacted(payload)})
                    assert response.status==expected,(name,response.status,parity.redacted(payload))
                    return payload
            journey(call)
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
            if line.startswith('{"nativeRecurringPendingRequest":'): observations.append(json.loads(line)["nativeRecurringPendingRequest"])
            elif line.startswith('{"nativeRecurringPendingMeta":'): groups.append(json.loads(line)["nativeRecurringPendingMeta"])
        evidence.update(localHttpRequestsAttempted=attempted,localHttpRequestsCompleted=len(cases),cases=cases,nativeRequests=observations,
            nativeStatements=sum(group["statements"] for group in groups),
            meteredNativeRowsRead=sum(group["rowsRead"] for group in groups if group["phase"]=="metered"),
            meteredNativeRowsWritten=sum(group["rowsWritten"] for group in groups if group["phase"]=="metered"),
            integrityProbeNativeRowsRead=sum(group["rowsRead"] for group in groups if group["phase"]=="integrity-probe"),
            nativeAttemptsObserved=sorted({value for group in groups for value in group["nativeAttempts"]},key=str),
            runtimeStopped=process is None or process.poll() is not None,integrityProbesReadOnlyAndOutsideJournal=True)
        if evidence["passed"]:
            try:
                verify(cases,observations)
                final=cases[-1]["result"]
                assert final["counters"]["actual_read"]==evidence["meteredNativeRowsRead"]<=READ_CAP
                assert final["counters"]["actual_written"]==evidence["meteredNativeRowsWritten"]<=WRITE_CAP
                assert final["counters"]["reserved_read"]<=READ_CAP and final["counters"]["reserved_written"]<=WRITE_CAP
                assert evidence["integrityProbeNativeRowsRead"]<=PROBE_READ_CAP
                evidence.update(nativeAttemptMetadataMissing=None in evidence["nativeAttemptsObserved"],missingAttemptsRetainReadReservations=True,
                    historicalStartAndIndependentPeriodsNativeAcceptance=True,
                    firstTenPendingAndOverflowDiscardNativeAcceptance=True,
                    inboxFallbackAndManualRetryNativeAcceptance=True,
                    retryReplayAndOldestReplacementNativeAcceptance=True,
                    futureDatesAndReadOnlyProjectionNativeAcceptance=True,
                    publicDtosDoNotLeakKeyHash=True)
            except Exception as error:
                evidence["passed"]=False; evidence["failure"]="Native observation assertion failed: "+str(error)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(evidence,indent=2,ensure_ascii=False)+"\n")
        print(json.dumps({key:evidence.get(key) for key in ("passed","failure","localHttpRequestsCompleted","nativeStatements",
            "meteredNativeRowsRead","meteredNativeRowsWritten","integrityProbeNativeRowsRead","runtimeStopped")}))
    if not evidence["passed"]: raise SystemExit("Native recurring-pending failed; preserve evidence, no retry or reset")


if __name__=="__main__": main()
