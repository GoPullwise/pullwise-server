"""Finite local GitHub rotation through unchanged Worker, D1 and DO accounting.

Only the GitHub HTTPS transport is synthetic. Exchange parsing and WebCrypto
AES-GCM execute unchanged. All accounts, keys and databases remain local; no
remote provider/D1 requests, retries, migrations of user data or deployments.
"""
from __future__ import annotations

import argparse
import base64
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
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "cloudflare/server"
HTTP_CAP, READ_CAP, WRITE_CAP, PROBE_READ_CAP = 24, 40000, 1000, 2000
BUNDLE_SHA256 = "3c3fd5a4179230e21e018e28b0e735e7c4abd8fe8e4ff39d2fd6bb2ea3a2e260"

ENTRY = r'''
import hashlib
import json
from urllib.parse import urlsplit,parse_qs
from workers import Response,Request
import application_entry as canonical
from pullwise_server.cloudflare_native_d1 import NativeD1
from pullwise_server.cloudflare_validation_budget import _field,BUDGET_SCOPE
from pullwise_server.cloudflare_preview_budget import ProductMeteredD1,initialize_product
from pullwise_server.cloudflare_state_records import record_name,encode_record,read_record,read_record_json
from pullwise_server.cloudflare_d1_mapping import initialize_account
from pullwise_server.cloudflare_github_gateway import WorkerGitHubGateway
from pullwise_server.cloudflare_preview_schema import SCHEMA_VERSION

OWNER="usr_github_810001"
FIXTURE_NOW=1791633600
TABLES=("account_entitlement_authority","ledger_projects","ledger_project_repositories","expense_categories","expenses","expense_events",
    "expense_create_idempotency","expense_recurring_rules","expense_recurring_occurrences",
    "workspace_members","workspace_events","ledger_plan_usage")
observed=[]
provider_calls=[]
rotations=0

class FixtureClock:
    now=FIXTURE_NOW
    @classmethod
    def time(cls): return cls.now
canonical.time=FixtureClock

class SyntheticTransport(WorkerGitHubGateway):
    async def _json(self,url,*,method="GET",token="",body=""):
        global rotations
        assert url.startswith(("https://github.com/","https://api.github.com/"))
        if url=="https://github.com/login/oauth/access_token":
            form=parse_qs(body)
            if form.get("grant_type")==["refresh_token"]:
                assert form.get("refresh_token")==["synthetic-refresh-1"] and rotations==0
                rotations+=1
                provider_calls.append("refresh")
                return {"access_token":"synthetic-access-2","token_type":"bearer",
                    "expires_in":28800,"refresh_token":"synthetic-refresh-2",
                    "refresh_token_expires_in":15897600}
            assert method=="POST" and form.get("code")==["synthetic-code"]
            provider_calls.append("exchange")
            return {"access_token":"synthetic-access-1","token_type":"bearer",
                "expires_in":28800,"refresh_token":"synthetic-refresh-1",
                "refresh_token_expires_in":15897600}
        assert token=="synthetic-access-"+str(rotations+1)
        if url=="https://api.github.com/user":
            provider_calls.append("profile")
            return {"id":810001,"login":"synthetic-owner","name":"Synthetic owner"}
        if url.startswith("https://api.github.com/user/installations?"):
            provider_calls.append("installations")
            return {"total_count":1,"installations":[{"id":810002,
                "account":{"id":810003,"login":"SyntheticOrg","type":"Organization"}}]}
        assert url.startswith("https://api.github.com/user/installations/810002/repositories?")
        provider_calls.append("repositories")
        return {"total_count":1,"repositories":[{"id":810004,"full_name":"SyntheticOrg/native"}]}

class ForbiddenGateway:
    def __init__(self,env): pass
    def __getattr__(self,name):
        async def blocked(*args,**kwargs):
            raise AssertionError("Non-GitHub providers excluded from local fixture")
        return blocked
canonical.WorkerGitHubGateway=SyntheticTransport
canonical.WorkerCreemGateway=ForbiddenGateway

class BoundedProductMeter(ProductMeteredD1):
    def __init__(self,*args,**kwargs):
        kwargs["clock"]=FixtureClock.time
        super().__init__(*args,**kwargs)
    async def _operation(self,statements,reads,writes):
        state=self.journal.snapshot()
        assert state["reserved_read"]+reads<=40000
        assert state["reserved_written"]+writes<=1000
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
        result=await super().batch([item.native for item in statements])
        raw=[{"read":_field(_field(item,"meta"),"rows_read"),
            "written":_field(_field(item,"meta"),"rows_written"),
            "attempts":_field(_field(item,"meta"),"total_attempts")} for item in result]
        assert all(type(item[key]) is int and item[key]>=0 for item in raw for key in ("read","written"))
        group={"phase":self.phase,"statements":len(raw),
            "rowsRead":sum(item["read"] for item in raw),
            "rowsWritten":sum(item["written"] for item in raw),
            "nativeAttempts":[item["attempts"] for item in raw]}
        self.groups.append(group)
        print(json.dumps({"nativeGithubRefreshMeta":group}))
        return result
canonical.NativeD1=ObservedD1

def seed(now):
    user={"id":OWNER,"githubId":"810001","githubLogin":"synthetic-owner",
        "name":"Synthetic owner","createdAt":now-1000,"billing":{"plan":"free"}}
    payload=encode_record("users",OWNER,user)
    session="github-refresh-native-owner"
    return [("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
        (record_name("users",OWNER),payload,now)),
        ("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
        (record_name("sessions",session),encode_record("sessions",session,
            {"id":session,"userId":OWNER,"expiresAt":now+7*86400}),now)),
        *initialize_account(owner_id=OWNER,account_snapshot=payload,now=now),
        ("""INSERT INTO ledger_projects(id,owner_id,name,github_repo_id,github_full_name,
            description,status,revision,created_at,updated_at)
            VALUES(?,?,'Preserved linked project',810004,'SyntheticOrg/native','',
            'active',1,'local','local')""",("prj_native_refresh",OWNER)),
        ("""INSERT INTO ledger_project_repositories(owner_id,project_id,github_repo_id,
            github_full_name,installation_id,created_at) VALUES(?,?,810004,'SyntheticOrg/native',810002,'local')""",
            (OWNER,"prj_native_refresh")),
        ("""INSERT INTO expense_categories(id,owner_id,name,revision,created_at,updated_at)
            VALUES('cat_native_refresh',?,'Hosting',1,'local','local')""",(OWNER,)),
        ("""INSERT INTO expenses(id,owner_id,target_kind,project_id,category_id,occurred_on,
            amount_minor,currency,purpose,revision,created_at,updated_at)
            VALUES('exp_native_refresh',?,'project','prj_native_refresh','cat_native_refresh',
            '2026-10-10',1250,'USD','Preserved original expense',1,'local','local')""",(OWNER,)),
        ("""INSERT INTO expense_events(id,expense_id,owner_id,actor_kind,actor_id,action,
            before_json,after_json,created_at) VALUES('evt_native_refresh','exp_native_refresh',
            ?,'session',?,'create',NULL,?,'local')""",
            (OWNER,OWNER,'{"id":"exp_native_refresh","categoryId":"cat_native_refresh","purpose":"Preserved original expense"}'))]

async def snapshot(native,journal,env):
    native.phase="integrity-probe"
    results=await native.batch([native.prepare("SELECT * FROM "+table) for table in (*TABLES,"app_state")])
    fingerprints={}
    for table,result in zip((*TABLES,"app_state"),results):
        rows=[dict(row) for row in _field(result,"results")]
        rows.sort(key=lambda row:json.dumps(row,sort_keys=True,separators=(",",":")))
        fingerprints[table]={"rows":len(rows),"sha256":hashlib.sha256(json.dumps(rows,
            sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()}
    user=json.loads(next(row["payload"] for row in _field(results[-1],"results")
        if row["name"]==record_name("users",OWNER)))
    access,refresh=user.get("githubAccessToken"),user.get("githubRefreshToken")
    sealed=bool(access and refresh and access.startswith("gcm1:") and refresh.startswith("gcm1:"))
    if access or refresh:
        assert sealed and access!=refresh
        gateway=SyntheticTransport(env)
        assert await gateway.unseal(access)=="synthetic-access-"+str(rotations+1)
        assert await gateway.unseal(refresh)=="synthetic-refresh-"+str(rotations+1)
    state=journal.snapshot()
    return {"tables":fingerprints,"encryptedPair":sealed,"nativeAesGcmRoundTrip":sealed,
        "accessExpiresAt":user.get("githubAccessTokenExpiresAt"),
        "refreshExpiresAt":user.get("githubRefreshTokenExpiresAt"),
        "durableRefreshClaim":isinstance(user.get("githubTokenRefresh"),dict),
        "cardinalityVerified":state.get("product_data_verified")}

class Default(canonical.Default):
    async def fetch(self,request):
        raw=await request.bytes(); raw=raw if isinstance(raw,bytes) else raw.to_bytes()
        assert len(raw)<=8192
        request=Request(str(request.url),method=str(request.method),redirect="manual",
            headers=dict(request.headers.items()),body=raw if raw else None)
        if urlsplit(str(request.url)).path.startswith("/_fixture/"):
            return await self.env.VALIDATION_BUDGET.get(
                self.env.VALIDATION_BUDGET.idFromName(BUDGET_SCOPE)).fetch(request)
        return await super().fetch(request)

class ValidationBudget(canonical.ValidationBudget):
    async def fetch(self,request):
        global rotations
        path=urlsplit(str(request.url)).path
        count=int(await self.ctx.storage.get("fixtureHttpCalls") or 0)+1
        assert count<=24 and count==int(request.headers.get("x-native-fixture-call") or "0")
        await self.ctx.storage.put("fixtureHttpCalls",count)
        FixtureClock.now=int(await self.ctx.storage.get("fixtureNow") or FIXTURE_NOW)
        rotations=int(await self.ctx.storage.get("fixtureRotations") or 0)
        observed.clear(); provider_calls.clear()
        journal=self._journal(); before=journal.snapshot()
        if path=="/_fixture/setup":
            assert count==1 and before["requests"]==0
            native=ObservedD1(self.env.DB)
            await initialize_product(native,journal,clock=FixtureClock.time)
            ticket=journal.begin_product(now=FixtureClock.time())
            meter=BoundedProductMeter(native,journal,ticket)
            await meter.ensure_cardinality()
            await meter.batch([meter.prepare(sql).bind(*params) for sql,params in seed(FixtureClock.now)])
            assert meter.accounted_outcome()
            journal.finish(ticket,now=FixtureClock.time())
            response=Response.json({"passed":True,"schemaVersion":SCHEMA_VERSION})
        elif path=="/_fixture/advance-clock":
            advances=int(await self.ctx.storage.get("fixtureAdvances") or 0)
            assert advances<3
            FixtureClock.now+=(28801 if advances<2 else 61)
            await self.ctx.storage.put("fixtureAdvances",advances+1)
            await self.ctx.storage.put("fixtureNow",FixtureClock.now)
            response=Response.json({"advanced":True})
        elif path=="/_fixture/rejected-cas":
            checks=int(await self.ctx.storage.get("fixtureCasChecks") or 0)
            assert checks<2
            await self.ctx.storage.put("fixtureCasChecks",checks+1)
            ticket=journal.begin_product(now=FixtureClock.time())
            meter=BoundedProductMeter(ObservedD1(self.env.DB),journal,ticket)
            await meter.ensure_cardinality()
            from pullwise_server.cloudflare_github_refresh import _publish_user
            user=await read_record(meter,"users",OWNER)
            session=await read_record(meter,"sessions","github-refresh-native-owner")
            user_json=await read_record_json(meter,"users",OWNER)
            session_json=await read_record_json(meter,"sessions",session["id"])
            if checks==0:
                session_json=encode_record("sessions",session["id"],{**session,"expiresAt":session["expiresAt"]-1})
            else:
                user_json=encode_record("users",OWNER,{**user,"nativeStaleSnapshot":True})
            assert not await _publish_user(meter,user={**user,"nativeRejectedWrite":True},
                expected_json=user_json,session=session,session_json=session_json,now=FixtureClock.now)
            assert await read_record(meter,"users",OWNER)==user
            assert meter.accounted_outcome()
            journal.finish(ticket,now=FixtureClock.time())
            response=Response.json({"fenced":True})
        elif path=="/_fixture/pending-claim":
            assert rotations==1 and int(await self.ctx.storage.get("fixtureClaims") or 0)==0
            await self.ctx.storage.put("fixtureClaims",1)
            ticket=journal.begin_product(now=FixtureClock.time())
            meter=BoundedProductMeter(ObservedD1(self.env.DB),journal,ticket)
            await meter.ensure_cardinality()
            from pullwise_server.cloudflare_github_identity_http import _write_user
            user=await read_record(meter,"users",OWNER)
            await _write_user(meter,{**user,"githubTokenRefresh":
                {"id":"synthetic-persisted-claim","startedAt":FixtureClock.now}},
                FixtureClock.now,expected_user=user)
            assert meter.accounted_outcome()
            journal.finish(ticket,now=FixtureClock.time())
            response=Response.json({"simulatedInterruptedClaim":True})
        elif path=="/_fixture/final":
            state=journal.snapshot()
            response=Response.json({"passed":True,"scope":state["scope"],"schemaVersion":state["schema_version"],
                "stateStorageVersion":state["state_storage_version"],"rotations":rotations,
                "counters":{key:state[key] for key in ("requests","reserved_read","reserved_written",
                    "actual_read","actual_written")}})
        else:
            response=await super().fetch(request)
        await self.ctx.storage.put("fixtureRotations",rotations)
        after=journal.snapshot()
        groups=[group for native in observed for group in native.groups if group["phase"]=="metered"]
        assert after["actual_read"]-before["actual_read"]==sum(g["rowsRead"] for g in groups)
        assert after["actual_written"]-before["actual_written"]==sum(g["rowsWritten"] for g in groups)
        assert after["actual_read"]<=40000 and after["actual_written"]<=1000
        assert after["active"] is None and after["stopped"] is None
        integrity=await snapshot(ObservedD1(self.env.DB),journal,self.env)
        probe=sum(g["rowsRead"] for native in observed for g in native.groups if g["phase"]=="integrity-probe")
        cumulative=int(await self.ctx.storage.get("fixtureProbeReads") or 0)+probe
        assert cumulative<=2000
        await self.ctx.storage.put("fixtureProbeReads",cumulative)
        print(json.dumps({"nativeGithubRefreshRequest":{"call":count,"path":path,
            "status":response.status,"meteredGroups":groups,"integrity":integrity,
            "syntheticTransportCalls":list(provider_calls),"rotations":rotations,
            "journalActualDeltasMatchRawMeta":True,"journalHealthy":True}}))
        return response
'''


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_observations(evidence):
    cases, requests = evidence["cases"], evidence["nativeRequests"]
    assert len(cases) == len(requests) <= HTTP_CAP
    baseline = requests[0]["integrity"]
    assert baseline["tables"]["expenses"]["rows"] == baseline["tables"]["expense_events"]["rows"] == 1
    by_name = {case["name"]: request for case, request in zip(cases, requests)}
    for case, request in zip(cases, requests):
        assert request["journalHealthy"] and request["journalActualDeltasMatchRawMeta"]
        assert request["integrity"]["cardinalityVerified"]
        for table in baseline["tables"]:
            if table != "app_state":
                assert request["integrity"]["tables"][table] == baseline["tables"][table], (case["name"], table)
        if case["name"] not in {"setup", "authorize_link", "callback_saves_encrypted_pair", "rotate_once", "persist_interrupted_claim"}:
            assert sum(group["rowsWritten"] for group in request["meteredGroups"]) == 0, case["name"]
        if case["name"] in {"expired_read_signals_refresh", "untrusted_origin", "cookie_with_bearer_rejected",
                "anonymous_rejected", "repeat_refresh_no_rotation", "pending_claim_denied", "unknown_claim_after_restart_denied"}:
            assert not request["syntheticTransportCalls"], case["name"]
    callback = by_name["callback_saves_encrypted_pair"]["integrity"]
    rotated = by_name["rotate_once"]["integrity"]
    assert callback["encryptedPair"] and callback["nativeAesGcmRoundTrip"]
    assert rotated["encryptedPair"] and rotated["nativeAesGcmRoundTrip"]
    assert rotated["accessExpiresAt"] > callback["accessExpiresAt"]
    assert rotated["refreshExpiresAt"] > callback["refreshExpiresAt"]
    assert by_name["restart_reads_rotated_pair"]["integrity"] == by_name["read_after_rotation"]["integrity"]
    assert by_name["stale_session_snapshot_cas"]["integrity"] == by_name["stale_user_snapshot_cas"]["integrity"] == by_name["read_after_rotation"]["integrity"]
    assert by_name["unknown_claim_after_restart_denied"]["integrity"]["durableRefreshClaim"]
    assert all(request["rotations"] <= 1 for request in requests)
    final = cases[-1]["result"]
    assert final["rotations"] == 1 and final["scope"] == evidence["singletonScope"]
    assert final["schemaVersion"] == evidence["expectedSchemaVersion"] and final["stateStorageVersion"] == 1
    assert final["counters"]["actual_read"] == evidence["meteredNativeRowsRead"]
    assert final["counters"]["actual_written"] == evidence["meteredNativeRowsWritten"]
    assert evidence["meteredNativeRowsRead"] <= READ_CAP and evidence["meteredNativeRowsWritten"] <= WRITE_CAP
    assert evidence["integrityProbeNativeRowsRead"] <= PROBE_READ_CAP and evidence["runtimeStopped"]
    assert all(value is None or type(value) is int and value == 1 for value in evidence["nativeAttemptsObserved"])
    return {"nativeAesGcmEncryptedPairPersistence": True, "callbackStoresExpiryAndRefresh": True,
        "expiredReadsSignalWithoutWritesOrProvider": True, "cookieAndOriginFencesNative": True,
        "singleUseRotationAndNoopNative": True, "restartRetainsRotatedPair": True,
        "staleUserAndSessionCasNative": True,
        "interruptedClaimNeverRedispatchedAfterRestart": True, "financialAndBindingRowsPreserved": True,
        "allJournalDeltasMatchNativeMeta": True, "nativeAttemptMetadataMissing": None in evidence["nativeAttemptsObserved"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8941)
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    if not directory.is_relative_to(Path("/workspace")) or not 1024 <= args.port <= 63535:
        raise SystemExit("Use a fresh /workspace directory and an unprivileged port")
    directory.mkdir(parents=True, exist_ok=False)
    source = directory / "src"
    source.mkdir()
    (source / "entry.py").write_text(ENTRY, encoding="utf-8")
    shutil.copy2(WORKER / "src/entry.py", source / "application_entry.py")
    shutil.copytree(ROOT / "pullwise_server", source / "pullwise_server", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(WORKER / "python_modules", directory / "python_modules", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    spec = importlib.util.spec_from_file_location("native_rest_helpers", ROOT / "scripts/check-rest-parity-native-runtime.py")
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    package_root, _ = helpers.pinned_tzdata()
    shutil.copytree(package_root / "tzdata", directory / "python_modules/tzdata", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"), dirs_exist_ok=True)
    dependencies = ("pyproject.toml", "uv.lock", "pylock.toml", "package.json", "package-lock.json")
    for name in dependencies:
        shutil.copy2(WORKER / name, directory / name)
    (directory / "node_modules").symlink_to(WORKER / "node_modules", target_is_directory=True)
    base = f"http://127.0.0.1:{args.port}"
    config = {"name": "pullwise-github-refresh-native-local-only", "main": "src/entry.py",
        "compatibility_date": "2026-09-23", "compatibility_flags": ["python_workers"],
        "workers_dev": False, "preview_urls": False, "routes": [],
        "vars": {"PULLWISE_MODE": "preview", "PULLWISE_D1_ACCESS_ENABLED": "1",
            "PULLWISE_PREVIEW_PRODUCT_ENABLED": "1", "PULLWISE_APP_URL": "https://preview.pull-wise.com",
            "PULLWISE_ALLOWED_ORIGINS": base, "PULLWISE_CREEM_API_BASE_URL": "https://test-api.creem.io",
            "PULLWISE_GITHUB_CLIENT_ID": "synthetic-client", "PULLWISE_GITHUB_CLIENT_SECRET": "synthetic-secret",
            "PULLWISE_GITHUB_CALLBACK_URL": base + "/auth/github/callback", "PULLWISE_GITHUB_APP_SLUG": "synthetic-app",
            "PULLWISE_GITHUB_TOKEN_KEY": base64.urlsafe_b64encode(bytes(range(32))).decode(),
            "PULLWISE_JEV_SUGGESTIONS_ENABLED": "0", "PULLWISE_JEV_SUGGESTIONS_EVALUATED": "0"},
        "d1_databases": [{"binding": "DB", "database_name": "github-refresh-native-local-only",
            "database_id": "00000000-0000-0000-0000-000000000081", "remote": False}],
        "durable_objects": {"bindings": [{"name": "VALIDATION_BUDGET", "class_name": "ValidationBudget"}]},
        "migrations": [{"tag": "local-fixture-v1", "new_sqlite_classes": ["ValidationBudget"]}]}
    config_path = directory / "wrangler.jsonc"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    bundle = Path("/workspace/.cache/pullwise-pyodide/pyodide_314.0.6_2026-08-17_6.capnp.bin")
    if not bundle.is_file() or sha256(bundle) != BUNDLE_SHA256:
        raise SystemExit("Official pinned Workerd Python cache missing or invalid")
    binary = WORKER / "node_modules/@cloudflare/workerd-linux-64/bin/workerd"
    wrapper = directory / "workerd-local-cache.sh"
    wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(str(binary)) + ' "$@" --pyodide-bundle-disk-cache-dir=/workspace/.cache/pullwise-pyodide\n')
    wrapper.chmod(0o700)
    env = {**os.environ, "XDG_CACHE_HOME": "/workspace/.cache", "XDG_CONFIG_HOME": str(directory / "xdg-config"),
        "UV_CACHE_DIR": "/workspace/.cache/uv", "UV_PYTHON_INSTALL_DIR": "/workspace/.python", "UV_SYSTEM_CERTS": "true",
        "NPM_CONFIG_CACHE": str(directory / "npm-cache"), "WRANGLER_SEND_METRICS": "false",
        "MINIFLARE_WORKERD_PATH": str(wrapper), "WRANGLER_LOG_PATH": str(directory / "wrangler-debug.log"),
        "PATH": str(WORKER / ".venv/bin") + os.pathsep + os.environ.get("PATH", "")}
    files = sorted((source / "pullwise_server").glob("*.py"))
    manifest = [(str(path.relative_to(source)), sha256(path)) for path in files]
    schema = int(re.findall(r"^SCHEMA_VERSION = ([0-9]+)$", (source / "pullwise_server/cloudflare_preview_schema.py").read_text(), re.M)[-1])
    evidence = {"passed": False, "date": "2026-10-10", "localOnly": True, "expectedSchemaVersion": schema,
        "nativePythonFFI": True, "nativeDurableObjectSQLite": True, "nativeWebCryptoAesGcm": True,
        "canonicalDefaultAndValidationBudget": True, "singletonScope": "pullwise-s17-s18-2026-09-28",
        "canonicalApplicationEntrySha256": sha256(source / "application_entry.py"), "sourceSha256": dict(manifest),
        "canonicalSourceTreeSha256": hashlib.sha256(json.dumps(manifest, separators=(",", ":")).encode()).hexdigest(),
        "fixtureEntrySha256": sha256(source / "entry.py"), "dependencySha256": {name: sha256(directory / name) for name in dependencies},
        "runtimeProvenance": {"workerdSha256": sha256(binary), "runtimeBundleSha256": BUNDLE_SHA256,
            "miniflareD1SourceSha256": sha256(WORKER / "node_modules/miniflare/dist/src/workers/d1/database.worker.js")},
        "nativeAttemptsFabricated": False, "realProviderRequests": 0, "remoteD1Operations": 0,
        "remoteApplicationRequests": 0, "clientRetries": 0, "localHttpCap": HTTP_CAP,
        "localReservedRowCaps": {"read": READ_CAP, "written": WRITE_CAP}, "localIntegrityProbeReadCap": PROBE_READ_CAP,
        "scope": "Local synthetic GitHub transport; real provider and browser acceptance separate"}
    process, attempted, cases = None, 0, []
    log_path = directory / "runtime.log"
    log = None

    def stop():
        nonlocal process, log
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        if log:
            log.close()

    def start():
        nonlocal process, log
        log = log_path.open("a")
        process = subprocess.Popen(["node", str(WORKER / "node_modules/wrangler/bin/wrangler.js"), "dev", "--local",
            "--config", str(config_path), "--ip", "127.0.0.1", "--port", str(args.port), "--inspector-port", str(args.port + 1000),
            "--persist-to", str(directory / "state")], cwd=directory, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError("Native GitHub refresh Worker startup failed")
            if "Ready on http://127.0.0.1:" + str(args.port) in log_path.read_text(errors="replace"):
                try:
                    with socket.create_connection(("127.0.0.1", args.port), timeout=.2):
                        return
                except OSError:
                    pass
            time.sleep(.2)
        raise AssertionError("Native GitHub refresh Worker startup timed out")

    try:
        start()
        opener = build_opener(ProxyHandler({}), NoRedirect())

        def call(name, method, path, *, cookie=True, bearer=False, trusted=True, expected=200):
            nonlocal attempted
            attempted += 1
            assert attempted <= HTTP_CAP
            headers = {"X-Native-Fixture-Call": str(attempted), "Content-Type": "application/json",
                "Origin": base if trusted else "https://untrusted.example.test"}
            if cookie:
                headers["Cookie"] = "pw_session=github-refresh-native-owner"
            if bearer:
                headers["Authorization"] = "Bearer synthetic-bearer"
            request = Request(base + path, method=method, headers=headers, data=b"{}" if method == "POST" else None)
            try:
                response = opener.open(request, timeout=60)
            except HTTPError as error:
                response = error
            with response:
                raw = response.read()
                payload = json.loads(raw) if raw else None
                assert b"synthetic-access" not in raw and b"synthetic-refresh" not in raw and b"gcm1:" not in raw
                # OAuth locations include random browser state, so record the
                # fixed route only and omit the authorization URL from evidence.
                recorded = None if "callback" in path or "authorize" in path else payload
                cases.append({"name": name, "method": method, "path": urlsplit(path).path,
                    "status": response.status, "result": recorded})
                assert response.status == expected, (name, response.status, recorded)
                return payload

        call("setup", "POST", "/_fixture/setup", cookie=False)
        authorization = call("authorize_link", "GET", "/auth/github/authorize?intent=link&redirectTo=/projects")
        state = parse_qs(urlsplit(authorization["url"]).query)["state"][0]
        call("callback_saves_encrypted_pair", "GET", "/auth/github/callback?" + urlencode({"state": state, "code": "synthetic-code"}), expected=302)
        first = call("read_before_expiry", "GET", "/api/v1/repositories")
        assert first["githubAccess"] == "authorized" and len(first["items"]) == 1
        call("advance_to_expiry", "POST", "/_fixture/advance-clock", cookie=False)
        expired = call("expired_read_signals_refresh", "GET", "/api/v1/repositories")
        assert expired.get("githubRefreshRequired") is True and not expired["items"]
        call("untrusted_origin", "POST", "/integrations/github/refresh", trusted=False, expected=403)
        call("cookie_with_bearer_rejected", "POST", "/integrations/github/refresh", bearer=True, expected=401)
        call("anonymous_rejected", "POST", "/integrations/github/refresh", cookie=False, expected=401)
        result = call("rotate_once", "POST", "/integrations/github/refresh")
        assert result == {"ok": True, "refreshed": True}
        result = call("repeat_refresh_no_rotation", "POST", "/integrations/github/refresh")
        assert result == {"ok": True, "refreshed": False}
        current = call("read_after_rotation", "GET", "/api/v1/repositories")
        assert current["githubAccess"] == "authorized" and current["items"] == first["items"]
        assert call("stale_session_snapshot_cas", "POST", "/_fixture/rejected-cas", cookie=False) == {"fenced": True}
        assert call("stale_user_snapshot_cas", "POST", "/_fixture/rejected-cas", cookie=False) == {"fenced": True}
        stop(); start()
        recovered = call("restart_reads_rotated_pair", "GET", "/api/v1/repositories")
        assert recovered == current
        call("advance_rotated_token_to_expiry", "POST", "/_fixture/advance-clock", cookie=False)
        call("persist_interrupted_claim", "POST", "/_fixture/pending-claim", cookie=False)
        pending = call("pending_claim_denied", "POST", "/integrations/github/refresh", expected=409)
        assert pending["error"]["code"] == "GITHUB_REFRESH_PENDING"
        stop(); start()
        call("advance_claim_beyond_ambiguity_window", "POST", "/_fixture/advance-clock", cookie=False)
        unknown = call("unknown_claim_after_restart_denied", "POST", "/integrations/github/refresh", expected=403)
        assert unknown["error"]["code"] == "GITHUB_REAUTHORIZATION_REQUIRED"
        call("final_journal", "GET", "/_fixture/final", cookie=False)
        evidence["passed"] = True
    except Exception as error:
        evidence["failure"] = str(error)
    finally:
        stop()
        requests, groups = [], []
        for line in log_path.read_text(errors="replace").splitlines() if log_path.exists() else []:
            if line.startswith('{"nativeGithubRefreshRequest":'):
                requests.append(json.loads(line)["nativeGithubRefreshRequest"])
            elif line.startswith('{"nativeGithubRefreshMeta":'):
                groups.append(json.loads(line)["nativeGithubRefreshMeta"])
        evidence.update(localHttpRequestsAttempted=attempted, localHttpRequestsCompleted=len(cases), cases=cases, nativeRequests=requests,
            nativeStatements=sum(group["statements"] for group in groups),
            meteredNativeRowsRead=sum(group["rowsRead"] for group in groups if group["phase"] == "metered"),
            meteredNativeRowsWritten=sum(group["rowsWritten"] for group in groups if group["phase"] == "metered"),
            integrityProbeNativeRowsRead=sum(group["rowsRead"] for group in groups if group["phase"] == "integrity-probe"),
            nativeAttemptsObserved=sorted({value for group in groups for value in group["nativeAttempts"]}, key=str),
            runtimeStopped=process is None or process.poll() is not None, integrityProbesReadOnlyAndOutsideJournal=True)
        if evidence["passed"]:
            try:
                evidence.update(validate_observations(evidence))
            except Exception as error:
                evidence["passed"] = False
                evidence["failure"] = "Native observation assertion failed: " + str(error)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps({key: evidence.get(key) for key in ("passed", "failure", "localHttpRequestsCompleted", "nativeStatements",
            "meteredNativeRowsRead", "meteredNativeRowsWritten", "integrityProbeNativeRowsRead", "runtimeStopped")}))
    if not evidence["passed"]:
        raise SystemExit("Native GitHub refresh failed; preserve evidence and fixture without retry/reset")


if __name__ == "__main__":
    main()
