"""Finite local category removal through the canonical preview coordinator.

Synthetic local accounts and history stay in a fresh remote:false D1. Native
Python FFI, ProductMeteredD1, PlanLimitedD1 and the original ValidationBudget
singleton execute unchanged application code. No remote requests or retries.
"""
from __future__ import annotations

import argparse
import hashlib
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
HTTP_CAP = 20
READ_CAP, WRITE_CAP, PROBE_READ_CAP = 20000, 1000, 5000
BUNDLE_SHA256 = "3c3fd5a4179230e21e018e28b0e735e7c4abd8fe8e4ff39d2fd6bb2ea3a2e260"
OWNER, OUTSIDER = "usr_github_780001", "usr_github_780002"

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

OWNER,OUTSIDER = "usr_github_780001","usr_github_780002"
BUSINESS = ("app_state","account_entitlement_authority","expenses","expense_events",
    "expense_create_idempotency","expense_recurring_rules","expense_recurring_occurrences",
    "expense_suggestion_events","ledger_activity_events","ledger_projects","workspace_members","ledger_plan_usage")
observed=[]
provider_calls=[]

class ForbiddenGateway:
    def __init__(self,env): pass
    def __getattr__(self,name):
        async def blocked(*args,**kwargs):
            provider_calls.append(name)
            raise AssertionError("No providers in local category removal fixture")
        return blocked

canonical.WorkerGitHubGateway=ForbiddenGateway
canonical.WorkerCreemGateway=ForbiddenGateway

class ObservedStatement:
    def __init__(self,owner,sql,native):
        self.owner,self.sql,self.native=owner,sql,native
    def bind(self,*params):
        return ObservedStatement(self.owner,self.sql,self.native.bind(*params))

class ObservedD1(NativeD1):
    def __init__(self,binding):
        super().__init__(binding)
        self.phase="metered"
        self.groups=[]
        observed.append(self)
    def prepare(self,sql):
        return ObservedStatement(self,sql,super().prepare(sql))
    async def batch(self,statements):
        statements=list(statements)
        assert 0<len(statements)<=64 and all(item.owner is self for item in statements)
        result=await super().batch([item.native for item in statements])
        raw=[{"rowsRead":_field(_field(item,"meta"),"rows_read"),
            "rowsWritten":_field(_field(item,"meta"),"rows_written"),
            "attempts":_field(_field(item,"meta"),"total_attempts")} for item in result]
        assert all(type(item[key]) is int and item[key]>=0 for item in raw
            for key in ("rowsRead","rowsWritten"))
        group={"phase":self.phase,"statements":len(raw),
            "rowsRead":sum(item["rowsRead"] for item in raw),
            "rowsWritten":sum(item["rowsWritten"] for item in raw),
            "nativeAttempts":[item["attempts"] for item in raw]}
        self.groups.append(group)
        print(json.dumps({"nativeCategoryRemovalMeta":group}))
        return result

canonical.NativeD1=ObservedD1

async def commands(native,values):
    return await native.batch([native.prepare(sql).bind(*params) for sql,params in values])

def seed(now):
    values=[]
    for role,identity in (("owner",OWNER),("outsider",OUTSIDER)):
        user={"id":identity,"githubId":identity.removeprefix("usr_github_"),
            "githubLogin":"synthetic-"+role,"name":"Synthetic "+role,"createdAt":now-1000,
            "billing":{"plan":"free"}}
        if role=="owner":
            user["billing"]={"provider":"creem","plan":"pro","status":"active",
                "subscriptionId":"sub_native_category_proof","interval":"month",
                "currentPeriodStart":now-1000,"currentPeriodEnd":now+86400}
        payload=encode_record("users",identity,user)
        session="category-removal-native-"+role
        values.extend([("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
            (record_name("users",identity),payload,now)),
            ("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
            (record_name("sessions",session),encode_record("sessions",session,
                {"id":session,"userId":identity,"expiresAt":now+86400}),now)),
            *initialize_account(owner_id=identity,account_snapshot=payload,now=now)])
    for kind in ("active","archived","current","deleted","audit","idempotency","rule","rule_response",
            "assistance","suggestion","accepted_suggestion"):
        values.append(("""INSERT INTO expense_categories(id,owner_id,name,archived_at,
            revision,created_at,updated_at) VALUES(?,?,?,?,1,?,?)""",
            ("cat_native_"+kind,OWNER,"Native "+kind,"local" if kind=="archived" else None,"local","local")))
    for kind in ("current","deleted"):
        values.append(("""INSERT INTO expenses(id,owner_id,target_kind,project_id,category_id,
            occurred_on,amount_minor,currency,purpose,revision,created_at,updated_at,deleted_at)
            VALUES(?,?,'shared',NULL,?,'2026-10-09',1250,'USD',?,1,?,?,?)""",
            ("exp_native_"+kind,OWNER,"cat_native_"+kind,"Preserved "+kind,"local","local",
                "local" if kind=="deleted" else None)))
    values.extend([
        ("""INSERT INTO expense_events(id,expense_id,owner_id,actor_kind,actor_id,action,
            before_json,after_json,created_at) VALUES(?,?,?,'session',?,'update',?,?,?)""",
            ("evt_native_category","exp_native_current",OWNER,OWNER,
                '{"categoryId":"cat_native_audit","purpose":"Before category move"}',
                '{"categoryId":"cat_native_current","purpose":"Preserved current"}',"local")),
        ("""INSERT INTO expense_create_idempotency(owner_id,idempotency_key,request_sha256,
            expense_id,response_json,created_at) VALUES(?,?,?,?,?,?)""",
            (OWNER,"native-category-replay","0"*64,"exp_native_current",
                '{"id":"exp_native_current","categoryId":"cat_native_idempotency",'
                '"assistance":{"suggestions":{"categoryId":"cat_native_assistance"}}}',"local")),
    ])
    for kind,template_category,response_category,status in (
            ("rule","cat_native_rule","cat_native_current","canceled"),
            ("rule_response","cat_native_current","cat_native_rule_response","paused")):
        values.append(("""INSERT INTO expense_recurring_rules(id,owner_id,actor_user_id,
            target_kind,project_id,template_json,schedule_json,status,revision,
            created_at,updated_at,create_key,create_sha256,create_response_json)
            VALUES(?,?,?,'shared',NULL,?,?,?,1,?,?,?,?,?)""",
            ("rule_native_"+kind,OWNER,OWNER,json.dumps({"category_id":template_category}),
                '{"frequency":"monthly","timezone":"Asia/Shanghai"}',status,"local","local",
                "native-category-"+kind,"1"*64,json.dumps({"categoryId":response_category}))))
    for kind,category,accepted in (("suggestion","cat_native_suggestion",None),
            ("accepted","cat_native_current","cat_native_accepted_suggestion")):
        values.append(("""INSERT INTO expense_suggestion_events(id,owner_id,created_at,
            question_version,draft_target_kind,outcome,category_id,accepted_category_id)
            VALUES(?,?,?,'local','shared','available',?,?)""",
            ("suggestion_native_"+kind,OWNER,"local",category,accepted)))
    values.append(("""INSERT INTO ledger_activity_events(id,operation_id,owner_id,target_kind,
        project_id,actor_json,resource_kind,resource_id,action,before_json,after_json,created_at)
        VALUES(?,?,?,'shared',NULL,?,'expense','exp_native_current','update',?,?,?)""",
        ("act_native_category","op_native_category",OWNER,
            json.dumps({"kind":"user","userId":OWNER,"name":"Synthetic owner"}),
            '{"categoryId":"cat_native_audit"}','{"categoryId":"cat_native_current"}',"local")))
    return values

async def snapshot(native,journal):
    native.phase="integrity-probe"
    result=await commands(native,[("SELECT * FROM "+table,()) for table in BUSINESS]+
        [("SELECT * FROM expense_categories",())])
    tables={}
    for table,part in zip(BUSINESS,result):
        rows=[dict(row) for row in _field(part,"results")]
        rows.sort(key=lambda row:json.dumps(row,sort_keys=True,separators=(",",":")))
        tables[table]={"rows":len(rows),"sha256":hashlib.sha256(json.dumps(rows,
            sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()}
    categories=sorted(({"id":row["id"],"revision":row["revision"],
        "archived":row["archived_at"] is not None} for row in _field(result[-1],"results")),
        key=lambda item:item["id"])
    usage=list(_field(result[BUSINESS.index("ledger_plan_usage")],"results"))
    state=journal.snapshot()
    return {"tables":tables,"categories":categories,
        "commercialWrites":sum(row["writes"] for row in usage),
        "commercialProjects":sum(row["projects"] for row in usage),
        "commercialRecords":sum(row["records"] for row in usage),
        "cardinalityVerified":state.get("product_data_verified"),
        "cardinality":state["product_data"]["rows"],
        "recordIntegrityVersion":state.get("state_record_integrity_version")}

class Default(canonical.Default):
    async def fetch(self,request):
        # Buffer the local HTTP body before crossing the fixture DO boundary.
        # Early unauthenticated responses and setup otherwise leave a borrowed
        # input stream unread after the outer response has been sent.
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
        assert count<=20 and count==int(request.headers.get("x-native-fixture-call") or "0")
        await self.ctx.storage.put("fixtureHttpCalls",count)
        observed.clear()
        provider_calls.clear()
        journal=self._journal()
        before=journal.snapshot()
        if path=="/_fixture/setup":
            assert count==1 and before["requests"]==0
            native=ObservedD1(self.env.DB)
            await initialize_product(native,journal)
            ticket=journal.begin_product(now=time.time())
            meter=ProductMeteredD1(native,journal,ticket)
            await meter.ensure_cardinality()
            await meter.batch([meter.prepare(sql).bind(*params) for sql,params in seed(int(time.time()))])
            assert meter.accounted_outcome()
            journal.finish(ticket,now=time.time())
            response=Response.json({"passed":True,"seedThroughCanonicalMeter":True,
                "syntheticCookieAccounts":2,"canonicalFreshSchemaVersion":SCHEMA_VERSION})
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
        assert after["actual_read"]<=20000 and after["actual_written"]<=1000
        assert not provider_calls and after["active"] is None and after["stopped"] is None
        integrity=await snapshot(ObservedD1(self.env.DB),journal)
        probe=sum(g["rowsRead"] for native in observed for g in native.groups if g["phase"]=="integrity-probe")
        cumulative_probe=int(await self.ctx.storage.get("fixtureProbeReads") or 0)+probe
        assert cumulative_probe<=5000
        await self.ctx.storage.put("fixtureProbeReads",cumulative_probe)
        print(json.dumps({"nativeCategoryRemovalRequest":{"call":count,"path":path,
            "status":response.status,"meteredGroups":groups,"integrity":integrity,
            "providerCalls":0,"journalActualDeltasMatchRawMeta":True,"journalHealthy":True}}))
        return response
'''


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_observations(evidence):
    """Validate captured native results without dispatching another request."""
    requests, cases = evidence["nativeRequests"], evidence["cases"]
    assert len(requests) == len(cases) == 20, "complete finite journey"
    baseline = requests[0]["integrity"]
    successful = {"remove_unused_active": "cat_native_active", "remove_unused_archived": "cat_native_archived"}
    categories = baseline["categories"]
    writes = 0
    for case, request in zip(cases, requests):
        current = request["integrity"]
        assert request["providerCalls"] == 0 and request["journalHealthy"] is True, case["name"]
        assert request["journalActualDeltasMatchRawMeta"] is True, case["name"]
        for table in baseline["tables"]:
            if table != "ledger_plan_usage":
                assert current["tables"][table] == baseline["tables"][table], (case["name"], table)
        if case["name"] in successful:
            assert case["status"] == 204, case["name"]
            categories = [item for item in categories if item["id"] != successful[case["name"]]]
            writes += 1
        elif case["name"] != "setup":
            assert sum(group["rowsWritten"] for group in request["meteredGroups"]) == 0, case["name"]
        assert current["categories"] == categories, case["name"]
        assert current["commercialWrites"] == writes, (case["name"], "commercial writes")
        assert current["commercialProjects"] == 0, (case["name"], "commercial projects")
        # The first successful mutation initializes the canonical usage row
        # from both historical expenses, including the soft-deleted record.
        assert current["commercialRecords"] == (2 if writes else 0), (case["name"], "historical capacity")
        assert current["cardinalityVerified"] is True, case["name"]
        assert current["cardinality"]["expense_categories"] == len(categories), case["name"]
        assert current["recordIntegrityVersion"] == baseline["recordIntegrityVersion"], case["name"]
        if case["name"].startswith("preserve_"):
            assert case["status"] == 409 and case["result"]["error"]["code"] == "CATEGORY_IN_USE"
            assert current == baseline, case["name"]
    assert writes == 2 and len(categories) == 9
    assert all(value is None or type(value) is int and value == 1 for value in evidence["nativeAttemptsObserved"])
    assert evidence["meteredNativeRowsRead"] <= READ_CAP and evidence["meteredNativeRowsWritten"] <= WRITE_CAP
    assert evidence["integrityProbeNativeRowsRead"] <= PROBE_READ_CAP
    final = cases[-1]["result"]
    assert final["schemaVersion"] == evidence.get("expectedSchemaVersion", 9) and final["stateStorageVersion"] == 1
    assert final["scope"] == evidence["singletonScope"]
    assert final["counters"]["actual_read"] == evidence["meteredNativeRowsRead"]
    assert final["counters"]["actual_written"] == evidence["meteredNativeRowsWritten"]
    assert evidence["runtimeStopped"] is True
    return {"activeAndArchivedUnusedCategoriesRemoved": True,
        "allHistoricalReferencesBlockRemoval": True, "deniedOperationsNoWrites": True,
        "originalBusinessRowsAndAccountProofPreserved": True, "failedRemovalPreservesCommercialCounters": True,
        "successfulRemovalsCommercialWrites": 2, "historicalExpenseCapacityPreserved": 2,
        "noProviders": True, "allJournalActualDeltasMatchRawMeta": True,
        "nativeAttemptMetadataMissing": None in evidence["nativeAttemptsObserved"],
        "missingAttemptsRetainReadReservations": True,
        "missingWriteAttemptsCanonicalProvenance": "d1-nonretryable-write-contract-v1"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8922)
    args = parser.parse_args()
    expected_schema = int(re.findall(r"^SCHEMA_VERSION = ([0-9]+)$",
        (ROOT / "pullwise_server/cloudflare_preview_schema.py").read_text(), re.MULTILINE)[-1])
    directory = args.run_dir.resolve()
    if not directory.is_relative_to(Path("/workspace")) or not 1024 <= args.port <= 64535:
        raise SystemExit("Use a fresh /workspace directory and an unprivileged port")
    directory.mkdir(parents=True, exist_ok=False)
    source = directory / "src"
    source.mkdir()
    (source / "entry.py").write_text(ENTRY, encoding="utf-8")
    shutil.copy2(WORKER / "src/entry.py", source / "application_entry.py")
    shutil.copytree(ROOT / "pullwise_server", source / "pullwise_server",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(WORKER / "python_modules", directory / "python_modules",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    dependencies = ("pyproject.toml", "uv.lock", "pylock.toml", "package.json", "package-lock.json")
    for name in dependencies:
        shutil.copy2(WORKER / name, directory / name)
    (directory / "node_modules").symlink_to(WORKER / "node_modules", target_is_directory=True)
    base = f"http://127.0.0.1:{args.port}"
    config = {"name": "pullwise-category-removal-native-local-only", "main": "src/entry.py",
        "compatibility_date": "2026-09-23", "compatibility_flags": ["python_workers"],
        "workers_dev": False, "preview_urls": False, "routes": [],
        "vars": {"PULLWISE_MODE": "preview", "PULLWISE_D1_ACCESS_ENABLED": "1",
            "PULLWISE_PREVIEW_PRODUCT_ENABLED": "1", "PULLWISE_APP_URL": "https://preview.pull-wise.com",
            "PULLWISE_ALLOWED_ORIGINS": base, "PULLWISE_CREEM_API_BASE_URL": "https://test-api.creem.io",
            "PULLWISE_JEV_SUGGESTIONS_ENABLED": "0", "PULLWISE_JEV_SUGGESTIONS_EVALUATED": "0"},
        "d1_databases": [{"binding": "DB", "database_name": "category-removal-native-local-only",
            "database_id": "00000000-0000-0000-0000-000000000040", "remote": False}],
        "durable_objects": {"bindings": [{"name": "VALIDATION_BUDGET", "class_name": "ValidationBudget"}]},
        "migrations": [{"tag": "local-fixture-v1", "new_sqlite_classes": ["ValidationBudget"]}]}
    config_path = directory / "wrangler.jsonc"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    bundle = Path("/workspace/.cache/pullwise-pyodide/pyodide_314.0.6_2026-08-17_6.capnp.bin")
    if not bundle.is_file() or sha256(bundle) != BUNDLE_SHA256:
        raise SystemExit("Official pinned Workerd Python cache missing or invalid")
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
    evidence = {"passed": False, "date": "2026-10-09", "localOnly": True,
        "expectedSchemaVersion": expected_schema,
        "nativePythonFFI": True, "nativeDurableObjectSQLite": True,
        "canonicalDefaultAndValidationBudget": True, "singletonScope": "pullwise-s17-s18-2026-09-28",
        "canonicalApplicationEntrySha256": sha256(source / "application_entry.py"),
        "canonicalSourceTreeSha256": hashlib.sha256(json.dumps(manifest, separators=(",", ":")).encode()).hexdigest(),
        "canonicalSourceFileCount": len(files), "fixtureEntrySha256": sha256(source / "entry.py"),
        "dependencySha256": {name: sha256(directory / name) for name in dependencies},
        "sourceSha256": {str(path.relative_to(source)): sha256(path) for path in files if path.name in {
            "cloudflare_ledger_api.py", "cloudflare_native_d1.py", "cloudflare_preview_budget.py",
            "cloudflare_plan_limits.py", "cloudflare_validation_budget.py"}},
        "runtimeProvenance": {"workerdSha256": sha256(binary), "runtimeBundleSha256": BUNDLE_SHA256,
            "miniflareD1SourceSha256": sha256(WORKER / "node_modules/miniflare/dist/src/workers/d1/database.worker.js")},
        "productMeterAndCommercialPlanAdapter": True, "trustedOriginCheckedByCanonicalApplication": True,
        "syntheticCookieAccounts": 2, "remoteD1Operations": 0, "remoteApplicationRequests": 0,
        "realProviderRequests": 0, "realAccounts": 0, "realPayments": 0, "clientRetries": 0,
        "localHttpCap": HTTP_CAP, "nativeAttemptsFabricated": False,
        "localMeteredRowCaps": {"read": READ_CAP, "written": WRITE_CAP},
        "localIntegrityProbeReadCap": PROBE_READ_CAP,
        "scope": "Native synthetic category removal; real-account/browser/remote acceptance separate"}
    process, attempted, cases = None, 0, []
    log_path = directory / "runtime.log"
    try:
        with log_path.open("w") as log:
            process = subprocess.Popen([str(WORKER / ".venv/bin/pywrangler"), "dev", "--local",
                "--config", str(config_path), "--ip", "127.0.0.1", "--port", str(args.port),
                "--inspector-port", str(args.port + 1000), "--persist-to", str(directory / "state")],
                cwd=directory, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError("Native category removal Worker startup failed")
                if "Ready on http://127.0.0.1:" + str(args.port) in log_path.read_text(errors="replace"):
                    try:
                        with socket.create_connection(("127.0.0.1", args.port), timeout=.2):
                            break
                    except OSError:
                        pass
                time.sleep(.2)
            else:
                raise AssertionError("Native category removal Worker startup timed out")
            opener = build_opener(ProxyHandler({}), NoRedirect())

            def call(name, method, path, actor=None, expected=200, revision=None, trusted=True, workspace=False):
                nonlocal attempted
                attempted += 1
                assert attempted <= HTTP_CAP
                headers = {"X-Native-Fixture-Call": str(attempted)}
                if actor:
                    headers["Cookie"] = "pw_session=category-removal-native-" + actor
                if method == "POST":
                    headers["Origin"] = base if trusted else "https://untrusted.example.test"
                    headers["Content-Type"] = "application/json"
                if revision is not None:
                    headers["If-Match"] = '"' + str(revision) + '"'
                if workspace:
                    headers["X-Pullwise-Workspace"] = OWNER
                request = Request(base + path, method=method, headers=headers,
                    data=b"{}" if method == "POST" else None)
                try:
                    response = opener.open(request, timeout=60)
                except HTTPError as error:
                    response = error
                with response:
                    raw = response.read()
                    payload = json.loads(raw) if raw else None
                    cases.append({"name": name, "method": method, "path": path,
                        "status": response.status, "result": payload})
                    assert response.status == expected, (name, response.status, payload)
                    return payload

            def remove(name, category, actor="owner", expected=204, revision=1, **options):
                return call(name, "POST", "/api/v1/categories/cat_native_" + category + "/remove",
                    actor, expected, revision, **options)

            call("setup", "POST", "/_fixture/setup")
            remove("login_required", "active", actor=None, expected=401)
            remove("untrusted_origin", "active", expected=403, trusted=False)
            remove("missing_revision", "active", expected=428, revision=None)
            remove("stale_revision", "active", expected=412, revision=2)
            remove("outsider_cannot_remove", "active", actor="outsider", expected=404)
            protected = ("current", "deleted", "audit", "idempotency", "rule", "rule_response",
                "assistance", "suggestion", "accepted_suggestion")
            for kind in protected:
                denied = remove("preserve_" + kind, kind, expected=409)
                assert denied["error"]["code"] == "CATEGORY_IN_USE"
            remove("remove_unused_active", "active")
            remove("remove_unused_archived", "archived")
            remove("removed_category_missing", "active", expected=404)
            listed = call("canonical_categories_list", "GET", "/api/v1/categories", "owner")
            assert {item["id"] for item in listed} == {"cat_native_" + kind for kind in protected}
            final = call("final_journal", "GET", "/_fixture/final")
            assert final["journalHealthy"] and final["schemaVersion"] == expected_schema and final["stateStorageVersion"] == 1
            assert final["scope"] == evidence["singletonScope"]
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
        requests, groups = [], []
        for line in log_path.read_text(errors="replace").splitlines() if log_path.exists() else []:
            if line.startswith('{"nativeCategoryRemovalRequest":'):
                requests.append(json.loads(line)["nativeCategoryRemovalRequest"])
            elif line.startswith('{"nativeCategoryRemovalMeta":'):
                groups.append(json.loads(line)["nativeCategoryRemovalMeta"])
        evidence.update(localHttpRequestsAttempted=attempted, localHttpRequestsCompleted=len(cases), cases=cases,
            nativeRequests=requests, nativeStatements=sum(group["statements"] for group in groups),
            meteredNativeRowsRead=sum(group["rowsRead"] for group in groups if group["phase"] == "metered"),
            meteredNativeRowsWritten=sum(group["rowsWritten"] for group in groups if group["phase"] == "metered"),
            integrityProbeNativeRowsRead=sum(group["rowsRead"] for group in groups if group["phase"] == "integrity-probe"),
            nativeAttemptsObserved=sorted({value for group in groups for value in group["nativeAttempts"]}, key=str),
            runtimeStopped=process is None or process.poll() is not None,
            integrityProbesReadOnlyAndOutsideJournal=True)
        if evidence["passed"]:
            try:
                evidence.update(validate_observations(evidence))
            except Exception as error:
                evidence["passed"] = False
                evidence["failure"] = "Native observation assertion failed: " + str(error)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps({key: evidence.get(key) for key in ("passed", "failure", "localHttpRequestsCompleted",
            "nativeStatements", "meteredNativeRowsRead", "meteredNativeRowsWritten", "runtimeStopped")}))
    if not evidence["passed"]:
        raise SystemExit("Native category removal failed; preserve fixture and evidence without retry")


if __name__ == "__main__":
    main()
