"""Fixed 39-request expense-retention journey in a fresh native local Worker.

Uses unchanged canonical modules and the existing REST parity runtime helpers.
Only synthetic local DB/DO state is seeded. No providers, remote D1, redirects,
client retries, journal reset, caller-selected clocks or deployment are allowed.
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
HTTP_CAP, FIXED_HTTP_COUNT = 100, 39
READ_CAP, WRITE_CAP, PROBE_READ_CAP = 250000, 25000, 10000
OWNER, EDITOR = parity.OWNER, parity.EDITOR
FIXTURE_NOW = parity.FIXTURE_NOW
FULL_KEY = "pwk_synthetic_native_retention_full"
PROJECT_KEY = "pwk_synthetic_native_retention_project"
PREFERENCE = "/api/v1/account/expense-retention"

SEED_AND_SNAPSHOT = r'''
def seed(now):
    values=[]
    for role,identity in (("owner",OWNER),("editor",EDITOR)):
        user={"id":identity,"name":"Synthetic retention "+role,"createdAt":now-1000,
            "githubId":identity.removeprefix("usr_github_"),"githubLogin":"synthetic-"+role,
            "billing":{"plan":"free"}}
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
    values.append(("""INSERT INTO expense_categories(id,owner_id,name,revision,
        created_at,updated_at) VALUES('cat_native_retention',?,'Hosting',1,'local','local')""",(OWNER,)))
    for identifier,status,deleted in (("prj_native_active","active",None),
            ("prj_native_archived","archived",None),
            ("prj_native_removed","archived","2025-01-01T00:00:00Z")):
        values.append(("""INSERT INTO ledger_projects(id,owner_id,name,github_repo_id,
            github_full_name,description,status,revision,created_at,updated_at,deleted_at)
            VALUES(?,?,'Synthetic retention',NULL,NULL,'',?,1,'local','local',?)""",
            (identifier,OWNER,status,deleted)))
    for identifier,token,scopes,restrictions in (
        ("ak_native_full","pwk_synthetic_native_retention_full",
            ["profile:read","projects:read","projects:write","expenses:read","expenses:write"],{"shared":True}),
        ("ak_native_project","pwk_synthetic_native_retention_project",
            ["expenses:read","expenses:write"],{"shared":False,"projectIds":["prj_native_active"]})):
        values.append(("""INSERT INTO api_keys(id,user_id,name,key_prefix,key_hash,scopes,
            expires_at,restrictions,created_at) VALUES(?,?,'Synthetic retention','synthetic',?,?,NULL,?,?)""",
            (identifier,OWNER,hashlib.sha256(token.encode()).hexdigest(),
                json.dumps(scopes,separators=(",",":")),json.dumps(restrictions,separators=(",",":")),now)))
    # 101 current visible rows: archived-project rows count; removed-project and
    # already-soft-deleted rows do not. Distinct ties prove all three sort keys.
    rows=[("exp_native_cleanup","prj_native_active","2024-01-01","2024-01-01T00:00:00Z",None),
        ("exp_native_shared_a",None,"2025-01-01","2025-01-01T00:00:00Z",None),
        ("exp_native_shared_b",None,"2025-01-01","2025-01-01T00:00:00Z",None),
        ("exp_native_late_created",None,"2025-01-01","2025-01-02T00:00:00Z",None),
        ("exp_native_archived","prj_native_archived","2025-02-01","2025-02-01T00:00:00Z",None),
        ("exp_native_project_live","prj_native_active","2025-03-01","2025-03-01T00:00:00Z",None),
        ("exp_native_predeleted",None,"2023-01-01","2023-01-01T00:00:00Z","2025-01-01T00:00:00Z"),
        ("exp_native_removed_project","prj_native_removed","2023-01-01","2023-01-01T00:00:00Z",None)]
    rows.extend(("exp_native_fill_"+str(index).zfill(3),None,"2026-01-01","2026-01-01T00:00:00Z",None)
        for index in range(95))
    assert len(rows)==103
    for identifier,project,date,created,deleted in rows:
        values.append(("""INSERT INTO expenses(id,owner_id,target_kind,project_id,category_id,
            occurred_on,amount_minor,currency,purpose,revision,created_at,updated_at,deleted_at)
            VALUES(?,?,?,?, 'cat_native_retention',?,100,'USD','Synthetic retention',1,?,?,?)""",
            (identifier,OWNER,"project" if project else "shared",project,date,created,created,deleted)))
    # This legal legacy cumulative value must remain unchanged on GET/settings.
    # The first financial mutation reconciles it from current visible expenses.
    values.append(("""INSERT INTO ledger_plan_usage(owner_id,projects,records,month,writes,minute,
        minute_writes,jev_reserved_microusd,project_cap,record_cap,minute_cap,month_cap,jev_cap,
        project_delta,record_delta,jev_delta,previous_month,previous_minute)
        VALUES(?,3,999,'2026-10',0,?,0,0,3,100,10,1000,0,0,0,0,'2026-10',?)""",
        (OWNER,now//60,now//60)))
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
    projects={row["id"]:row for row in rows["ledger_projects"]}
    active=[row for row in rows["expenses"] if row["deleted_at"] is None and
        (row["target_kind"]=="shared" or projects[row["project_id"]]["deleted_at"] is None)]
    rules=[{"id":item["id"],"status":item["status"],"revision":item["revision"],
        "blockedCode":item["blocked_code"],"actor":item["actor_user_id"],
        "keyGrantPresent":"_api_key_hash" in json.loads(item["template_json"])}
        for item in rows["expense_recurring_rules"]]
    state=journal.snapshot()
    assert len(rows["api_keys"])==2 and len(rules)<=1 and len(rows["expenses"])<=107
    assert len(rows["ledger_projects"])==3 and len(rows["workspace_members"])==1
    assert tables["d1_command_guard"]["rows"]==0
    return {"tables":tables,"rules":rules,"activeExpenseCount":len(active),
        "retiredSeedIds":sorted(row["id"] for row in rows["expenses"]
            if row["id"].startswith("exp_native_") and row["deleted_at"] is not None),
        "expenseEvents":[{"expenseId":row["expense_id"],"action":row["action"],
            "actorKind":row["actor_kind"],"actorId":row["actor_id"] if row["actor_kind"]=="schedule" else None}
            for row in rows["expense_events"]],
        "preferences":[{"owner":json.loads(row["payload"])["id"],
            "enabled":json.loads(row["payload"]).get("autoRemoveOldestExpense",False),
            "revision":json.loads(row["payload"]).get("expenseRetentionRevision",1)}
            for row in rows["app_state"] if row["name"].startswith("record:users:")],
        "usage":[{"owner":item["owner_id"],"projects":item["projects"],"records":item["records"],
            "writes":item["writes"]} for item in rows["ledger_plan_usage"]],
        "cardinalityVerified":state.get("product_data_verified"),"cardinality":state["product_data"]["rows"]}
'''

# Reuse the reviewed native FFI/DO accounting fixture, while replacing only its
# local seed/probe implementation and its fixed numeric admission limits.
BASE_ENTRY = re.sub(r"\b(?:100000|10000|5000)\b",
    lambda match: {"100000":"250000","10000":"25000","5000":"10000"}[match.group()], parity.ENTRY)
ENTRY = (BASE_ENTRY[:BASE_ENTRY.index("def seed(now):")] + SEED_AND_SNAPSHOT +
    BASE_ENTRY[BASE_ENTRY.index("class Default(canonical.Default):"):])
ENTRY = ENTRY.replace("count<=50", "count<=100")
ENTRY = ENTRY.replace("nativeRestParity", "nativeRetention")
ENTRY = ENTRY.replace('await meter.batch([meter.prepare(sql).bind(*params) for sql,params in seed(FixtureClock.now)])',
    'values=seed(FixtureClock.now)\n            assert len(values)<=130\n'
    '            for offset in range(0,len(values),32):\n'
    '                await meter.batch([meter.prepare(sql).bind(*params) for sql,params in values[offset:offset+32]])')
ENTRY = ENTRY.replace('"syntheticCookieAccounts":3', '"syntheticCookieAccounts":2')
ENTRY = ENTRY.replace("FIXTURE_NOW+32*86400", "FIXTURE_NOW+61")
ENTRY = ENTRY.replace("# The selected fixture contains exactly three rules, one due period each.",
    "# The selected fixture contains exactly one rule, one due period.")
ENTRY = ENTRY.replace('        integrity=await snapshot(ObservedD1(self.env.DB),journal)',
    '        cumulative=int(await self.ctx.storage.get("fixtureProbeReads") or 0)\n'
    '        probe_bound=sum(after["product_data"]["rows"][table] for table in TABLES)+32\n'
    '        assert cumulative+probe_bound<=10000\n'
    '        integrity=await snapshot(ObservedD1(self.env.DB),journal)')


def journey(call):
    def expense(project=False, **changes):
        return {"target":{"kind":"project","projectId":"prj_native_active"} if project else {"kind":"shared"},
            "occurredOn":"2026-10-09","amount":"12.00","currency":"USD",
            "categoryId":"cat_native_retention","purpose":"Hosting",**changes}
    def usage(name, expected):
        response=call(name,"GET","/api/v1/me",token=FULL_KEY)
        assert response["ledgerUsage"]["expenseRecords"]=={"used":expected,"limit":100,"remaining":max(0,100-expected)}
        assert response["ledgerUsage"]["projects"]=={"used":3,"limit":3,"remaining":0}
    call("setup","POST","/_fixture/setup",body={})
    assert call("default_off","GET",PREFERENCE,cookie="owner")=={"autoRemoveOldestExpense":False,"revision":1}
    call("bearer_preference_forbidden","GET",PREFERENCE,token=FULL_KEY,expected=401)
    usage("legacy_usage_read_counts_active",101)
    off=call("off_over_capacity","POST","/api/v1/expenses",token=FULL_KEY,body=expense(),idempotency="native-off-over",expected=403)
    assert off["error"]["code"]=="RECORD_LIMIT"
    call("stale_preference","PATCH",PREFERENCE,cookie="owner",body={"autoRemoveOldestExpense":True},revision=2,expected=412)
    call("missing_preference_revision","PATCH",PREFERENCE,cookie="owner",body={"autoRemoveOldestExpense":True},expected=428)
    call("invalid_preference","PATCH",PREFERENCE,cookie="owner",body={"autoRemoveOldestExpense":"true"},revision=1,expected=422)
    call("untrusted_preference_origin","PATCH",PREFERENCE,cookie="owner",body={"autoRemoveOldestExpense":True},revision=1,trusted=False,expected=403)
    assert call("enable_owner","PATCH",PREFERENCE,cookie="owner",body={"autoRemoveOldestExpense":True},revision=1)=={"autoRemoveOldestExpense":True,"revision":2}
    call("same_preference_no_write","PATCH",PREFERENCE,cookie="owner",body={"autoRemoveOldestExpense":True},revision=2)
    assert call("member_selector_reads_own_account","GET",PREFERENCE,cookie="editor",workspace=OWNER)=={"autoRemoveOldestExpense":False,"revision":1}
    assert call("member_selector_changes_own_account","PATCH",PREFERENCE,cookie="editor",workspace=OWNER,body={"autoRemoveOldestExpense":True},revision=1)=={"autoRemoveOldestExpense":True,"revision":2}
    assert call("owner_preference_remains_independent","GET",PREFERENCE,cookie="owner")=={"autoRemoveOldestExpense":True,"revision":2}
    over=call("enabled_over_capacity_requires_cleanup","POST","/api/v1/expenses",token=FULL_KEY,body=expense(),idempotency="native-on-over",expected=403)
    assert over["error"]["code"]=="RETENTION_CLEANUP_REQUIRED"
    call("manual_cleanup_to_capacity","DELETE","/api/v1/expenses/exp_native_cleanup",token=FULL_KEY,revision=1,expected=204)
    usage("manual_cleanup_usage",100)
    denied=call("restricted_key_cannot_skip_global_oldest","POST","/api/v1/expenses",token=PROJECT_KEY,body=expense(True),idempotency="native-project-forbidden",expected=403)
    assert denied["error"]["code"]=="RETENTION_TARGET_FORBIDDEN"
    call("invalid_create_no_retirement","POST","/api/v1/expenses",token=FULL_KEY,body=expense(categoryId="cat_missing"),idempotency="native-invalid",expected=422)
    saved=call("replace_oldest_id_tie","POST","/api/v1/expenses",token=FULL_KEY,body=expense(),idempotency="native-first-replacement",expected=201)
    assert call("replacement_idempotent_replay","POST","/api/v1/expenses",token=FULL_KEY,body=expense(),idempotency="native-first-replacement",expected=201)==saved
    usage("replacement_keeps_capacity",100)
    assert call("disable_owner","PATCH",PREFERENCE,cookie="owner",body={"autoRemoveOldestExpense":False},revision=2)=={"autoRemoveOldestExpense":False,"revision":3}
    off=call("off_exact_capacity_rejects","POST","/api/v1/expenses",token=FULL_KEY,body=expense(),idempotency="native-off-full",expected=403)
    assert off["error"]["code"]=="RECORD_LIMIT"
    call("manual_delete_frees_slot","DELETE","/api/v1/expenses/exp_native_shared_b",token=FULL_KEY,revision=1,expected=204)
    call("off_refills_free_slot","POST","/api/v1/expenses",token=FULL_KEY,body=expense(),idempotency="native-refill",expected=201)
    assert call("reenable_owner","PATCH",PREFERENCE,cookie="owner",body={"autoRemoveOldestExpense":True},revision=3)=={"autoRemoveOldestExpense":True,"revision":4}
    call("advance_fixture_minute_once","POST","/_fixture/advance-clock",body={})
    recurring=expense(); recurring.pop("occurredOn")
    recurring["schedule"]={"frequency":"monthly","day":9,"timezone":"Asia/Shanghai","startOn":"2026-10-01"}
    rule=call("recurring_rule_create","POST","/api/v1/expense-recurring-rules",token=FULL_KEY,body=recurring,idempotency="native-recurring",expected=201)
    assert call("recurring_replaces_oldest_created_tie","POST","/_fixture/tick",body={})=={"ok":True,"scanned":1,"created":1,"blocked":0,"replayed":0}
    assert call("recurring_no_extra_occurrence","POST","/_fixture/tick",body={})=={"ok":True,"scanned":0,"created":0,"blocked":0,"replayed":0}
    call("replace_archived_project_expense","POST","/api/v1/expenses",token=FULL_KEY,body=expense(),idempotency="native-archived",expected=201)
    usage("archived_replacement_keeps_capacity",100)
    call("remove_project_frees_visible_expenses","DELETE","/api/v1/projects/prj_native_active",token=FULL_KEY,revision=1,expected=204)
    usage("removed_project_frees_expense_capacity",99)
    failed=call("project_slots_remain_cumulative","POST","/api/v1/projects",token=FULL_KEY,body={"name":"Fourth cumulative project"},idempotency="native-fourth-project",expected=403)
    assert failed["error"]["code"]=="PROJECT_LIMIT"
    call("late_stale_preference_no_write","PATCH",PREFERENCE,cookie="owner",body={"autoRemoveOldestExpense":False},revision=3,expected=412)
    assert call("final_owner_preference","GET",PREFERENCE,cookie="owner")=={"autoRemoveOldestExpense":True,"revision":4}
    final=call("final_journal","GET","/_fixture/final")
    assert final["journalHealthy"] and final["scope"]=="pullwise-s17-s18-2026-09-28"
    return rule


def verify(cases, observations):
    assert len(cases)==len(observations)==FIXED_HTTP_COUNT
    by_name={case["name"]:obs for case,obs in zip(cases,observations)}
    previous=None
    for case,obs in zip(cases,observations):
        current=obs["integrity"]
        assert obs["journalActualDeltasMatchRawMeta"] and obs["journalHealthy"] and obs["providerCalls"]==0
        assert current["cardinalityVerified"]
        if case["status"]>=400 or case["name"] in {"replacement_idempotent_replay","same_preference_no_write","recurring_no_extra_occurrence"}:
            assert sum(group["rowsWritten"] for group in obs["meteredGroups"])==0,case["name"]
            assert current["tables"]==previous["tables"],case["name"]
        if case["method"]=="GET":
            assert sum(group["rowsWritten"] for group in obs["meteredGroups"])==0,case["name"]
            assert current["tables"]==previous["tables"],case["name"]
        previous=current
    seeded=by_name["setup"]["integrity"]
    assert seeded["activeExpenseCount"]==101 and seeded["tables"]["expenses"]["rows"]==103
    assert seeded["usage"]==[{"owner":OWNER,"projects":3,"records":999,"writes":0}]
    assert by_name["enabled_over_capacity_requires_cleanup"]["integrity"]["usage"]==seeded["usage"]
    cleanup=by_name["manual_cleanup_to_capacity"]["integrity"]
    assert cleanup["activeExpenseCount"]==100 and cleanup["usage"][0]["records"]==100
    assert cleanup["retiredSeedIds"]==["exp_native_cleanup","exp_native_predeleted"]
    replaced=by_name["replace_oldest_id_tie"]["integrity"]
    assert replaced["retiredSeedIds"]==cleanup["retiredSeedIds"]+["exp_native_shared_a"]
    assert replaced["tables"]["expenses"]["rows"]==104 and replaced["activeExpenseCount"]==100
    assert replaced["tables"]["expense_events"]["rows"]==3
    assert replaced["tables"]["ledger_activity_events"]["rows"]==3
    refilled=by_name["off_refills_free_slot"]["integrity"]
    assert refilled["activeExpenseCount"]==100 and refilled["tables"]["expenses"]["rows"]==105
    generated=by_name["recurring_replaces_oldest_created_tie"]["integrity"]
    assert generated["activeExpenseCount"]==100 and generated["tables"]["expenses"]["rows"]==106
    assert generated["tables"]["expense_recurring_occurrences"]["rows"]==1
    assert "exp_native_late_created" in generated["retiredSeedIds"]
    scheduled=[event for event in generated["expenseEvents"] if event["actorKind"]=="schedule"]
    assert len(scheduled)==2 and {event["action"] for event in scheduled}=={"delete","create"}
    assert all(event["actorId"]==generated["rules"][0]["id"]+":"+OWNER for event in scheduled)
    assert generated["rules"][0]["keyGrantPresent"]
    archived=by_name["replace_archived_project_expense"]["integrity"]
    assert "exp_native_archived" in archived["retiredSeedIds"]
    assert archived["activeExpenseCount"]==100 and archived["tables"]["expenses"]["rows"]==107
    removed=by_name["remove_project_frees_visible_expenses"]["integrity"]
    assert removed["activeExpenseCount"]==99 and removed["tables"]["expenses"]==archived["tables"]["expenses"]
    assert removed["usage"][0]["records"]==99 and removed["usage"][0]["projects"]==3


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
    config={"name":"pullwise-retention-native-local-only","main":"src/entry.py",
        "compatibility_date":"2026-09-23","compatibility_flags":["python_workers"],
        "workers_dev":False,"preview_urls":False,"routes":[],
        "vars":{"PULLWISE_MODE":"preview","PULLWISE_D1_ACCESS_ENABLED":"1",
            "PULLWISE_PREVIEW_PRODUCT_ENABLED":"1","PULLWISE_RECURRING_EXPENSES_ENABLED":"1",
            "PULLWISE_APP_URL":"https://preview.pull-wise.com","PULLWISE_ALLOWED_ORIGINS":base,
            "PULLWISE_CREEM_API_BASE_URL":"https://test-api.creem.io",
            "PULLWISE_JEV_SUGGESTIONS_ENABLED":"0","PULLWISE_JEV_SUGGESTIONS_EVALUATED":"0"},
        "d1_databases":[{"binding":"DB","database_name":"retention-native-local-only",
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
    evidence={"passed":False,"date":"2026-10-09","localOnly":True,
        "nativePythonFFI":True,"nativeDurableObjectSQLite":True,"canonicalDefaultAndValidationBudget":True,
        "singletonScope":"pullwise-s17-s18-2026-09-28","fixtureClockStart":FIXTURE_NOW,"fixtureClockAdvanceSeconds":61,
        "canonicalApplicationEntrySha256":parity.sha256(source/"application_entry.py"),
        "canonicalSourceTreeSha256":hashlib.sha256(json.dumps(manifest,separators=(",",":")).encode()).hexdigest(),
        "canonicalSourceFileCount":len(files),"fixtureEntrySha256":parity.sha256(source/"entry.py"),
        "sourceSha256":dict(manifest),"dependencySha256":{name:parity.sha256(directory/name) for name in dependencies},
        "cachedTzdataFileManifestSha256":hashlib.sha256(json.dumps(package_manifest,separators=(",",":")).encode()).hexdigest(),
        "runtimeProvenance":{"workerdSha256":parity.sha256(binary),"runtimeBundleSha256":parity.BUNDLE_SHA256,
            "miniflareD1SourceSha256":parity.sha256(WORKER/"node_modules/miniflare/dist/src/workers/d1/database.worker.js")},
        "productMeterAndCommercialPlanAdapter":True,"nativeAttemptsFabricated":False,
        "syntheticCookieAccounts":2,"syntheticKeysMaximum":2,"syntheticSeedPhysicalExpenses":103,
        "syntheticSeedActiveExpenses":101,"syntheticPhysicalExpensesMaximum":107,
        "remoteD1Operations":0,"remoteApplicationRequests":0,"realProviderRequests":0,"clientRetries":0,
        "fixedHttpJourney":FIXED_HTTP_COUNT,"localHttpCap":HTTP_CAP,
        "localReservedRowCaps":{"read":READ_CAP,"written":WRITE_CAP},"localIntegrityProbeReadCap":PROBE_READ_CAP,
        "scope":"Native local expense retention, active capacity and original financial/accounting fences; remote/browser/real accounts separate"}
    attempted,cases,process=0,[],None
    log_path=directory/"runtime.log"
    try:
        with log_path.open("w") as log:
            process=subprocess.Popen(["node",str(WORKER/"node_modules/wrangler/bin/wrangler.js"),"dev","--local","--config",str(config_path),
                "--ip","127.0.0.1","--port",str(args.port),"--inspector-port",str(args.port+1000),
                "--persist-to",str(directory/"state")],cwd=directory,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            deadline=time.monotonic()+120
            while time.monotonic()<deadline:
                if process.poll() is not None: raise AssertionError("Native retention Worker startup failed")
                if "Ready on http://127.0.0.1:"+str(args.port) in log_path.read_text(errors="replace"):
                    try:
                        with socket.create_connection(("127.0.0.1",args.port),timeout=.2): break
                    except OSError: pass
                time.sleep(.2)
            else: raise AssertionError("Native retention Worker startup timed out")
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
            if line.startswith('{"nativeRetentionRequest":'): observations.append(json.loads(line)["nativeRetentionRequest"])
            elif line.startswith('{"nativeRetentionMeta":'): groups.append(json.loads(line)["nativeRetentionMeta"])
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
                    defaultOffFullRejectionNativeAcceptance=True,oneOldestReplacementAndAuditNativeAcceptance=True,
                    activeArchivedRemovedAndLegacyCapacityNativeAcceptance=True,cookieAccountRevisionIsolationNativeAcceptance=True,
                    keyRestrictionAndIdempotencyNativeAcceptance=True,recurringReplacementAndNoExtraOccurrenceNativeAcceptance=True,
                    cumulativeProjectCapacityNativeAcceptance=True,publicDtosDoNotLeakKeyHash=True)
            except Exception as error:
                evidence["passed"]=False; evidence["failure"]="Native observation assertion failed: "+str(error)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(evidence,indent=2,ensure_ascii=False)+"\n")
        print(json.dumps({key:evidence.get(key) for key in ("passed","failure","localHttpRequestsCompleted","nativeStatements",
            "meteredNativeRowsRead","meteredNativeRowsWritten","integrityProbeNativeRowsRead","runtimeStopped")}))
    if not evidence["passed"]: raise SystemExit("Native retention failed; preserve evidence, no retry or reset")


if __name__=="__main__": main()
