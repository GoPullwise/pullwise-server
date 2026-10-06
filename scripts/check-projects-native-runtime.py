"""Finite native Worker/D1 acceptance with two explicitly synthetic identities.

No remote bindings, provider requests, resets, client retries or real payments.
The generated local entry copies canonical application code byte for byte and
replaces only GitHub/Creem transports with deterministic fixture gateways. Native
SQL results and metadata pass through the canonical NativeD1 integer adapter.
Generated configuration/credentials stay in a new run directory outside Git.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import shutil
import signal
import shlex
import socket
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "cloudflare/server"
OWNER, MEMBER = "usr_github_710001", "usr_github_710002"
PRODUCTS = {"pro": {"month": "prod_pro_month", "year": "prod_pro_year"},
            "max": {"month": "prod_max_month", "year": "prod_max_year"}}
SECRET = "local-only-synthetic-webhook-secret"

FIXTURE_ENTRY = r'''
import json
from workers import Response
import application_entry as canonical
from pullwise_server.cloudflare_native_d1 import NativeD1
from pullwise_server.cloudflare_creem_gateway import CreemRequestRejected


def field(value, name):
    try:
        return value.get(name)
    except (AttributeError, TypeError):
        return getattr(value, name, None)


def record(results):
    values = []
    for result in results:
        meta = field(result, "meta")
        values.append({"rowsRead": field(meta, "rows_read"),
            "rowsWritten": field(meta, "rows_written"),
            "attempts": field(meta, "total_attempts")})
    print(json.dumps({"nativeAcceptanceMeta": values}))


class RecordingStatement:
    def __init__(self, owner, statement):
        self.owner, self.statement = owner, statement

    def bind(self, *values):
        return RecordingStatement(self.owner, self.statement.bind(*values))

    async def first(self, column=None):
        result = await self.all()
        values = result.results
        row = values[0] if len(values) else None
        return row if column is None or row is None else row[column]

    async def all(self):
        result = await self.statement.all()
        record([result])
        return result

    async def run(self):
        result = await self.statement.run()
        record([result])
        return result


class RecordingD1:
    def __init__(self, binding):
        self.original = NativeD1(binding)

    def prepare(self, sql):
        return RecordingStatement(self, self.original.prepare(sql))

    async def batch(self, statements):
        statements = list(statements)
        if any(item.owner is not self for item in statements):
            raise ValueError("native fixture ownership mismatch")
        results = await self.original.batch([item.statement for item in statements])
        record(results)
        return results


class FixtureGitHub:
    mode = "normal"

    def __init__(self, env):
        pass

    async def unseal(self, value):
        if value not in {"sealed:local-owner", "sealed:local-member"}:
            raise ValueError("unknown synthetic actor")
        return value.removeprefix("sealed:")

    async def _json(self, url, *, token, **kwargs):
        print(json.dumps({"nativeAcceptanceProvider": "github_fixture"}))
        if url == "https://api.github.com/users/local-member":
            return {"id": 710002, "login": "local-member", "type": "User"}
        raise ValueError("unexpected synthetic GitHub URL")

    async def installations(self, token):
        print(json.dumps({"nativeAcceptanceProvider": "github_fixture"}))
        if self.mode == "lost":
            return []
        ids = [501, 502] if token == "local-owner" else [502]
        return [{"id": value, "account": {"id": 77 if value == 501 else 1001,
            "login": "local-owner" if value == 501 else "local-team",
            "type": "User" if value == 501 else "Organization"}} for value in ids]

    async def repositories(self, token, installation_id):
        print(json.dumps({"nativeAcceptanceProvider": "github_fixture"}))
        ids = [101] if installation_id == 501 else [102, 103] if token == "local-owner" else [102]
        return [{"id": value, "full_name": "private/local-repo-" + str(value)} for value in ids]


class FixtureCreem:
    def __init__(self, env):
        pass

    async def post(self, path, payload):
        print(json.dumps({"nativeAcceptanceProvider": "creem_fixture"}))
        if path == "v1/checkouts":
            return {"id": "ch_local", "checkout_url": "https://test-checkout.creem.io/ch_local"}
        identifier = path.split("/")[2]
        if path.endswith("/upgrade"):
            if payload["product_id"] == "prod_max_month":
                raise CreemRequestRejected("synthetic definitive rejection")
            if payload["product_id"] == "prod_pro_year":
                raise ValueError("synthetic unknown provider outcome")
        return {"id": identifier, "status": "scheduled_cancel" if path.endswith("/cancel") else "active"}

    async def product(self, identifier):
        print(json.dumps({"nativeAcceptanceProvider": "creem_fixture"}))
        return {"id": identifier, "status": "active", "name": identifier,
            "price": 1000, "currency": "USD", "billing_type": "recurring",
            "billing_period": "every-year" if identifier.endswith("year") else "every-month"}


canonical.WorkerGitHubGateway = FixtureGitHub
canonical.WorkerCreemGateway = FixtureCreem
canonical.NativeD1 = RecordingD1


class Default(canonical.Default):
    async def fetch(self, request):
        if getattr(self.env, "PULLWISE_MODE", "") != "local":
            return Response.json({"error": {"code": "LOCAL_FIXTURE_ONLY"}}, status=503)
        if str(request.url).endswith("/_fixture/bytes"):
            return Response("汉字🙂".encode("utf-8"),
                headers={"Content-Type": "text/csv; charset=utf-8"})
        if str(request.url).endswith("/_fixture/account-init"):
            from pullwise_server.cloudflare_account_adapter import D1AccountTransactions
            import time
            binding = RecordingD1(self.env.DB)
            initialized = 0
            for owner in ("usr_github_710001", "usr_github_710002"):
                row = await binding.prepare("SELECT owner_id FROM account_entitlement_authority WHERE owner_id=?").bind(owner).first()
                if row is None:
                    await D1AccountTransactions(binding).initialize_account(owner_id=owner, now=int(time.time()))
                    initialized += 1
            return Response.json({"initialized": initialized})
        if request.headers.get("x-native-fixture-bounded-export") == "1":
            application = canonical._Application(self.env, RecordingD1(self.env.DB))
            # Exercise the exact canonical preview byte-spool branch while
            # retaining actual local SQL and avoiding remote attempts metadata.
            application.bounded_exports = True
            return await application.fetch(request)
        FixtureGitHub.mode = request.headers.get("x-native-fixture-access") or "normal"
        return await super().fetch(request)
'''


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def literal(value):
    return "NULL" if value is None else "'" + str(value).replace("'", "''") + "'"


def seed_sql(now):
    users = {
        OWNER: {"id": OWNER, "githubId": "710001", "githubLogin": "local-owner",
            "name": "Synthetic Owner", "githubAccessToken": "sealed:local-owner",
            "createdAt": now - 1000, "billing": {"plan": "pro", "status": "active",
                "subscriptionId": "sub_local_owner", "interval": "month",
                "currentPeriodStart": now - 1000, "currentPeriodEnd": now + 86400}},
        MEMBER: {"id": MEMBER, "githubId": "710002", "githubLogin": "local-member",
            "name": "Synthetic Member", "githubAccessToken": "sealed:local-member",
            "createdAt": now - 1000, "billing": {"plan": "free"}},
    }
    sessions = {"local-owner-session": {"userId": OWNER, "expiresAt": now + 3600},
                "local-member-session": {"userId": MEMBER, "expiresAt": now + 3600}}
    return "\n".join("UPDATE app_state SET payload=" + literal(json.dumps(value,
        separators=(",", ":"))) + ",updated_at=" + str(now) + " WHERE name=" + literal(name) + ";"
        for name, value in (("users", users), ("sessions", sessions)))


def prepare(directory):
    directory.mkdir(parents=True, exist_ok=False)
    source = directory / "src"
    source.mkdir()
    shutil.copy2(WORKER / "src/entry.py", source / "application_entry.py")
    shutil.copytree(ROOT / "pullwise_server", source / "pullwise_server",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (source / "entry.py").write_text(FIXTURE_ENTRY, encoding="utf-8")
    shutil.copytree(WORKER / "migrations", directory / "migrations")
    # Wrangler packages the external SDK relative to the selected config.
    shutil.copytree(WORKER / "python_modules", directory / "python_modules",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    config = json.loads((WORKER / "wrangler.jsonc").read_text())
    config["name"] = "pullwise-native-acceptance-local"
    config["$schema"] = str(WORKER / "node_modules/wrangler/config-schema.json")
    config["vars"].update({"PULLWISE_APP_URL": "http://127.0.0.1:8896",
        "PULLWISE_ALLOWED_ORIGINS": "http://127.0.0.1:8896",
        "PULLWISE_CREEM_PRODUCT_IDS_JSON": json.dumps(PRODUCTS, separators=(",", ":")),
        "PULLWISE_CREEM_WEBHOOK_SECRET": SECRET,
        "PULLWISE_JEV_ENABLED": "0", "PULLWISE_JEV_QUALITY_EVALUATED": "0"})
    (directory / "wrangler.jsonc").write_text(json.dumps(config, indent=2) + "\n")
    return directory / "wrangler.jsonc"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8896)
    parser.add_argument("--resume-at-billing", action="store_true", help="Resume only billing in an existing preserved local fixture")
    parser.add_argument("--resume-paid-webhook", action="store_true", help="Resume exact pending paid receipt after fixture-authority initialization")
    parser.add_argument("--resume-corrected-period", action="store_true", help="Continue with newer paid fixture using official ISO period dates")
    parser.add_argument("--resume-provider-failures", action="store_true", help="Continue only provider failure and remaining subscription checks")
    parser.add_argument("--bounded-csv-only", action="store_true", help="Two-request native canonical byte-spool acceptance with 251 synthetic rows")
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    if not directory.is_relative_to(Path("/workspace")) and not directory.is_relative_to(Path("/tmp")):
        raise SystemExit("Run directory must be in the writable workspace or /tmp")
    config = directory / "wrangler.jsonc" if args.resume_at_billing else prepare(directory)
    raw_config = json.loads(config.read_text())
    raw_config["vars"]["PULLWISE_APP_URL"] = "https://local.example.test"
    raw_config["vars"]["PULLWISE_ALLOWED_ORIGINS"] = f"http://127.0.0.1:{args.port}"
    assert not raw_config.get("routes") and not raw_config.get("workers_dev")
    assert all(db.get("remote") is False for db in raw_config["d1_databases"])
    config.write_text(json.dumps(raw_config, indent=2) + "\n")
    if args.resume_at_billing:
        shutil.copy2(WORKER / "src/entry.py", directory / "src/application_entry.py")
        shutil.copytree(ROOT / "pullwise_server", directory / "src/pullwise_server",
                       dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (directory / "src/entry.py").write_text(FIXTURE_ENTRY, encoding="utf-8")
    persistence = directory / "state"
    env = {**os.environ, "UV_CACHE_DIR": "/workspace/.cache/uv",
        "UV_PYTHON_INSTALL_DIR": "/workspace/.python", "XDG_CACHE_HOME": "/workspace/.cache",
        "XDG_CONFIG_HOME": "/workspace/.config", "WRANGLER_SEND_METRICS": "false",
        "WRANGLER_LOG_PATH": str(directory / "wrangler-debug.log")}
    # Workerd's native bundle loader does not honor the session's HTTP proxy.
    # Preload the pinned official bundle (verified against Workerd's embedded
    # hash) through that proxy, then pass only a local cache directory.
    bundle_cache = Path("/workspace/.cache/pullwise-pyodide")
    bundle = bundle_cache / "pyodide_314.0.6_2026-08-17_6.capnp.bin"
    if (not bundle.is_file() or hashlib.sha256(bundle.read_bytes()).hexdigest()
            != "3c3fd5a4179230e21e018e28b0e735e7c4abd8fe8e4ff39d2fd6bb2ea3a2e260"):
        raise SystemExit("Official pinned Workerd Python bundle cache is missing or invalid")
    wrapper = directory / "workerd-local-cache.sh"
    wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(str(WORKER / "node_modules/@cloudflare/workerd-linux-64/bin/workerd"))
        + ' "$@" ' + shlex.quote("--pyodide-bundle-disk-cache-dir=" + str(bundle_cache)) + "\n")
    wrapper.chmod(0o700)
    env["MINIFLARE_WORKERD_PATH"] = str(wrapper)
    cli = ["node", str(WORKER / "node_modules/wrangler/wrangler-dist/cli.js")]

    def d1(arguments):
        run = subprocess.run([*cli, "d1", *arguments, "--local", "--config", str(config),
            "--persist-to", str(persistence)], cwd=WORKER, env=env,
            capture_output=True, text=True, timeout=90)
        if run.returncode:
            raise AssertionError("Local D1 setup/verification failed: " + run.stderr[-2000:])
        return run.stdout

    if not args.resume_at_billing:
        d1(["migrations", "apply", "DB"])
        seed = directory / "seed.sql"
        fixture_sql = seed_sql(int(time.time()))
        if args.bounded_csv_only:
            fixture_sql += "\nINSERT INTO expense_categories(id,owner_id,name,revision,created_at,updated_at) VALUES('csv_category'," + literal(OWNER) + ",'Synthetic CSV',1,'local','local');"
            for index in range(251):
                purpose, note = "=公式 " + "汉字🙂" * 100, "汉字🙂" * 450
                fixture_sql += "\nINSERT INTO expenses(id,owner_id,target_kind,category_id,occurred_on,amount_minor,currency,purpose,note,revision,created_at,updated_at) VALUES(" + ",".join(literal(value) for value in
                    (f"csv_{index:04}", OWNER, "shared", "csv_category", "2026-10-06", 1, "USD", purpose, note, 1, "local", "local")) + ");"
        seed.write_text(fixture_sql, encoding="utf-8")
        d1(["execute", "DB", "--file", str(seed), "--json"])
    count, checks, responses = 0, [], []
    process = None
    evidence = {"passed": False, "localOnly": True, "nativePythonFFI": True,
        "syntheticProvenance": True, "syntheticAccounts": 2, "realAccounts": 0,
        "remoteRequests": 0, "remoteD1RowsRead": 0, "remoteD1RowsWritten": 0,
        "realProviderRequests": 0, "realPayments": 0, "clientRetries": 0,
        "canonicalEntrySha256": hashlib.sha256((WORKER / "src/entry.py").read_bytes()).hexdigest()}
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def call(method, path, body=None, *, actor="owner", shared=False, headers=None,
             expected=200, code=None, raw_body=None):
        nonlocal count
        count += 1
        if count > 100:
            raise AssertionError("Finite local HTTP cap exceeded")
        auth = {"Cookie": f"pw_session=local-{actor}-session"} if actor else {}
        if shared:
            auth["X-Pullwise-Workspace"] = OWNER
        outgoing = {**auth, "Origin": f"http://127.0.0.1:{args.port}",
            "Content-Type": "application/json", **(headers or {})}
        data = raw_body if raw_body is not None else json.dumps(body,
            separators=(",", ":"), ensure_ascii=False).encode() if body is not None else None
        request = Request(f"http://127.0.0.1:{args.port}{path}", method=method,
                          headers=outgoing, data=data)
        try:
            response = opener.open(request, timeout=45)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read().decode("utf-8")
            payload = json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw
            responses.append({"method": method, "path": path.split("?")[0], "status": response.status})
            assert response.status == expected, (method, path, response.status, payload)
            if code:
                assert payload["error"]["code"] == code, (code, payload)
            return payload

    def webhook(identifier, kind, product="prod_max_year", *, offset=0, status="active", bad=False):
        now = int(time.time())
        cached = directory / (identifier + ".json")
        existing = None
        if args.resume_paid_webhook and identifier == "evt_native_paid" and not cached.exists():
            existing = json.loads(d1(["execute", "DB", "--command",
                "SELECT raw_sha256,update_json FROM billing_webhook_receipts WHERE event_id='evt_native_paid';", "--json"]))[0]["results"][0]
            now = json.loads(existing["update_json"])["eventCreated"]
        raw = json.dumps({"id": identifier, "eventType": kind, "created_at": (now + offset) * 1000,
            "object": {"id": "sub_local_member", "status": status,
                "customer": {"id": "cust_local_member"},
                "current_period_start_date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 1000)),
                "current_period_end_date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 86400)),
                "product": {"id": product, "billing_period": "every-year" if product.endswith("year") else "every-month"},
                "metadata": {"userId": MEMBER}}}, separators=(",", ":")).encode()
        if cached.exists():
            raw = cached.read_bytes()
        else:
            if existing:
                assert hashlib.sha256(raw).hexdigest() == existing["raw_sha256"], "Pending receipt reconstruction must be exact"
            cached.write_bytes(raw)
        signature = "bad" if bad else hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        return call("POST", "/webhooks/creem", actor=None, raw_body=raw,
            headers={"creem-signature": signature}, expected=400 if bad else 200,
            code="INVALID_SIGNATURE" if bad else None)

    runtime_log = directory / (args.output.stem + "-runtime.log")
    try:
        with runtime_log.open("w") as log:
            process = subprocess.Popen([str(WORKER / ".venv/bin/pywrangler"), "dev", "--local",
                "--config", str(config), "--ip", "127.0.0.1", "--port", str(args.port),
                "--inspector-port", str(args.port + 1000), "--persist-to", str(persistence)],
                cwd=WORKER, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            # Wait for TCP only. No health polling or business requests precede the run.
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError("Native Worker startup failed: " + runtime_log.read_text()[-2500:])
                try:
                    with socket.create_connection(("127.0.0.1", args.port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.2)
            else:
                raise AssertionError("Native Worker did not bind within 120 seconds")
            if args.bounded_csv_only:
                assert call("GET", "/health")["ok"]
                exported = call("GET", "/api/v1/expenses/export?target=shared", headers={"X-Native-Fixture-Bounded-Export": "1"})
                rows = list(csv.reader(io.StringIO(exported)))
                assert len(rows) == 252 and len(exported.encode("utf-8")) > 1024 * 1024
                assert all(row[7] == "'=公式 " + "汉字🙂" * 100 and row[8] == "汉字🙂" * 450 for row in rows[1:])
                evidence.update({"passed": True, "canonicalBoundedCsvBranch": True,
                    "csvRecords": 251, "csvBytes": len(exported.encode("utf-8")),
                    "utf8Exact": True, "spreadsheetFormulaEscaped": True})
                checks.append("Canonical bounded CSV byte spool exceeds old 1 MiB cap, paginates 251 native records, and preserves exact UTF-8/formula escaping")
                return
            if not args.resume_at_billing:
                assert call("GET", "/health")["ok"]
                assert call("POST", "/_fixture/account-init", {}, actor=None)["initialized"] == 2
                assert call("GET", "/_fixture/bytes", actor=None) == "汉字🙂"
                checks.append("native Response(bytes) preserves multibyte UTF-8")
                assert call("GET", "/api/v1/me")["id"] == OWNER
                assert call("GET", "/api/v1/me", actor="member")["id"] == MEMBER
                assert len(call("GET", "/api/v1/workspaces", actor="member")["items"]) == 1
                repos = call("GET", "/api/v1/repositories?limit=2")
                assert [item["githubRepoId"] for item in repos["items"]] == [101, 102]
                assert call("GET", "/api/v1/repositories?limit=2&cursor=102")["items"][0]["githubRepoId"] == 103
                project = call("POST", "/api/v1/projects", {"name": "Native project 汉字🙂",
                    "githubRepoIds": [101, 102], "githubOrganizationId": 1001,
                    "description": "Synthetic finite native acceptance"}, expected=201)
                pid, pp = project["id"], "/api/v1/projects/" + project["id"]
                assert project["githubRepoIds"] == [101, 102]
                assert project["githubOrganization"]["login"] == "local-team"
                call("POST", "/api/v1/projects", {"githubRepoIds": [102, 103]}, expected=409, code="PROJECT_CONFLICT")
                call("POST", "/api/v1/projects", {"githubRepoIds": [101, 101]}, expected=422)
                assert call("GET", "/api/v1/projects")["items"][0]["id"] == pid
                call("GET", "/api/v1/projects?limit=%C2%B2", expected=422)
                call("GET", "/api/v1/projects?cursor=" + "x" * 8193, expected=413)
                call("PATCH", pp, {"description": "stale"}, headers={"If-Match": '"2"'}, expected=412)
                call("PATCH", pp, {"description": "missing revision"}, expected=428)
                changed = call("PATCH", pp, {"name": "Native project renamed", "description": "Edited"}, headers={"If-Match": '"1"'})
                assert changed["revision"] == 2
                category = call("POST", "/api/v1/categories", {"name": "Native QA"}, expected=201)
                draft = {"target": {"kind": "project", "projectId": pid}, "occurredOn": "2026-10-06",
                    "amount": "12.30", "currency": "USD", "categoryId": category["id"],
                    "purpose": "=公式 汉字🙂"}
                expense = call("POST", "/api/v1/expenses", draft, headers={"Idempotency-Key": "native-project-create"}, expected=201)
                assert call("POST", "/api/v1/expenses", draft, headers={"Idempotency-Key": "native-project-create"}, expected=201)["id"] == expense["id"]
                assert call("GET", pp)["totals"] == [{"currency": "USD", "amountMinor": 1230}]
                summary = call("GET", "/api/v1/reports/summary?projectId=" + pid)
                assert next(x for x in summary["groups"] if x["target"] == "account")["amountMinor"] == 1230
                assert call("GET", "/api/v1/reports/timeseries?projectId=" + pid)["groups"]
                assert call("GET", "/api/v1/reports/categories?projectId=" + pid)["groups"]
                exported = list(csv.reader(io.StringIO(call("GET", "/api/v1/expenses/export?projectId=" + pid))))
                assert len(exported) == 2 and exported[1][7] == "'=公式 汉字🙂"
                archived = call("PATCH", pp, {"status": "archived"}, headers={"If-Match": '"2"'})
                assert archived["status"] == "archived" and archived["totals"][0]["amountMinor"] == 1230
                call("POST", "/api/v1/expenses", draft, headers={"Idempotency-Key": "native-archived-create"}, expected=403)
                call("PATCH", pp, {"status": "active"}, headers={"If-Match": '"3"', "X-Native-Fixture-Access": "lost"}, expected=403)
                restored = call("PATCH", pp, {"status": "active"}, headers={"If-Match": '"3"'})
                assert restored["revision"] == 4
                rebound = call("PATCH", pp, {"githubRepoIds": [102, 103]}, headers={"If-Match": '"4"'})
                assert rebound["id"] == pid and rebound["totals"] == [{"currency": "USD", "amountMinor": 1230}]
                reused = call("POST", "/api/v1/projects", {"githubRepoIds": [101]}, expected=201)
                assert reused["id"] != pid
                checks.append("Projects create/list/detail/edit revisions/archive/restore/rebind and repository uniqueness; historical totals exact")

                invite_path = f"/api/v1/workspaces/{OWNER}/invites"
                invitation = call("POST", invite_path, {"githubLogin": "local-member", "role": "editor"}, expected=201)
                call("POST", "/api/v1/workspace-invitations/preview", {"token": invitation["token"]}, expected=403)
                assert call("POST", "/api/v1/workspace-invitations/preview", {"token": invitation["token"]}, actor="member")["workspace"]["id"] == OWNER
                assert call("POST", "/api/v1/workspace-invitations/accept", {"token": invitation["token"]}, actor="member")["workspace"]["role"] == "editor"
                recovered = call("POST", "/api/v1/workspace-invitations/preview", {"token": invitation["token"]}, actor="member")
                assert recovered["workspace"]["role"] == "editor" and recovered["workspace"]["revision"] == 1
                call("POST", "/api/v1/workspace-invitations/accept", {"token": invitation["token"]}, actor="member", expected=410)
                profile = call("GET", "/api/v1/me", actor="member", shared=True)
                assert profile["id"] == MEMBER and profile["workspace"]["id"] == OWNER and profile["entitlements"]["plan"] == "pro"
                history = call("GET", pp, actor="member", shared=True)
                assert history["githubAccess"] == "partial" and history["repositories"][1]["githubFullName"] is None
                assert "private/local-repo-103" not in json.dumps(history)
                member_expense = call("POST", "/api/v1/expenses", draft, actor="member", shared=True,
                    headers={"Idempotency-Key": "native-project-create"}, expected=201)
                assert member_expense["id"] != expense["id"]
                assert call("GET", "/api/v1/expenses", actor="member")["items"] == []
                assert len(call("GET", "/api/v1/expenses", actor="member", shared=True)["items"]) == 2
                key = call("POST", "/api-keys", {"name": "Synthetic member key",
                    "scopes": ["expenses:read", "expenses:write", "profile:read"],
                    "restrictions": {"shared": True, "workspaceId": OWNER}}, actor="member", shared=True, expected=201)
                key_headers = {"Cookie": "", "Authorization": "Bearer " + key["key"]}
                assert call("GET", "/api/v1/me", actor=None, headers=key_headers)["id"] == MEMBER
                mp = f"/api/v1/workspaces/{OWNER}/members/{MEMBER}"
                assert call("PATCH", mp, {"role": "viewer"}, headers={"If-Match": '"1"'})["revision"] == 2
                call("GET", "/api/v1/me", actor=None, headers=key_headers, expected=403)
                call("POST", "/api/v1/expenses", draft, actor="member", shared=True,
                    headers={"Idempotency-Key": "native-viewer-denied"}, expected=403)
                call("PATCH", mp, {"role": "admin"}, headers={"If-Match": '"2"'})
                call("PATCH", f"/api/v1/workspaces/{OWNER}/members/{OWNER}", {"role": "viewer"}, actor="member", shared=True,
                     headers={"If-Match": '"1"'}, expected=403, code="OWNER_IMMUTABLE")
                call("DELETE", mp, headers={"If-Match": '"3"'}, expected=204)
                call("GET", "/api/v1/expenses", actor="member", shared=True, expected=404)
                checks.append("Two synthetic accounts invite/accept single use, actual actor grants/idempotency, workspace isolation, Editor/Viewer/Admin restrictions and removal/key invalidation")

            else:
                row = json.loads(d1(["execute", "DB", "--command", "SELECT id FROM ledger_projects WHERE name='Native project renamed';", "--json"]))[0]["results"][0]
                pid, pp = row["id"], "/api/v1/projects/" + row["id"]
                evidence["resumedPreservedLocalState"] = True
                evidence["priorCompletedProjectMemberRequests"] = 50
            if not args.resume_provider_failures:
                if args.resume_paid_webhook:
                    assert call("POST", "/_fixture/account-init", {}, actor=None)["initialized"] == (0 if args.resume_corrected_period else 2)
                    evidence["recoveredExactPendingPaidReceipt"] = True
                    evidence["priorCompletedBillingRequests"] = 4
                else:
                    checkout = call("POST", "/billing/checkout-sessions", {"plan": "pro", "interval": "month"}, actor="member")
                    assert checkout["url"].startswith("https://test-checkout.creem.io/")
                    assert call("GET", "/billing", actor="member")["account"]["plan"] == "free"
                    webhook("evt_native_bad", "subscription.paid", bad=True)
                    assert call("GET", "/billing", actor="member")["account"]["plan"] == "free"
                paid_id = "evt_native_paid_corrected" if args.resume_corrected_period else "evt_native_paid"
                webhook(paid_id, "subscription.paid", product="prod_pro_month", offset=10 if args.resume_corrected_period else 0)
                before = call("GET", "/billing", actor="member")["account"]
                assert before["plan"] == "pro" and before["subscriptionId"] == "sub_local_member"
                webhook(paid_id, "subscription.paid", product="prod_pro_month", offset=10 if args.resume_corrected_period else 0)
                assert call("GET", "/billing", actor="member")["account"] == before
            else:
                evidence["resumedAtProviderFailures"] = True
            call("POST", "/billing/change-interval", {"plan": "max", "interval": "month"}, actor="member", expected=503)
            assert call("GET", "/billing", actor="member")["account"]["pendingChange"] is None
            pending = call("POST", "/billing/change-interval", {"plan": "max", "interval": "year"}, actor="member")
            assert pending["pending"]
            current = call("GET", "/billing", actor="member")["account"]
            assert current["plan"] == "pro" and current["pendingChange"]["plan"] == "max"
            assert call("POST", "/billing/change-interval", {"plan": "max", "interval": "year"}, actor="member")["pending"]
            call("POST", "/billing/cancel-subscription", {}, actor="member", expected=409, code="SUBSCRIPTION_CHANGE_PENDING")
            webhook("evt_native_max", "subscription.paid", offset=11)
            settled = call("GET", "/billing", actor="member")["account"]
            assert settled["plan"] == "max" and settled["interval"] == "year" and settled["pendingChange"] is None
            assert call("POST", "/billing/cancel-subscription", {}, actor="member")["status"] == "canceling"
            assert call("POST", "/billing/cancel-subscription", {}, actor="member")["alreadyScheduled"]
            assert call("POST", "/billing/resume-subscription", {}, actor="member")["status"] == "active"
            assert call("POST", "/billing/resume-subscription", {}, actor="member")["alreadyActive"]
            webhook("evt_native_unpaid", "subscription.unpaid", offset=12, status="unpaid")
            assert call("GET", "/billing", actor="member")["account"]["plan"] == "free"
            webhook("evt_native_restored", "subscription.paid", offset=13)
            assert call("GET", "/billing", actor="member")["account"]["plan"] == "max"
            call("POST", "/billing/change-interval", {"plan": "pro", "interval": "year"}, expected=503)
            assert call("GET", "/billing")["account"]["pendingChange"]["interval"] == "year"
            assert call("POST", "/billing/change-interval", {"plan": "pro", "interval": "year"})["pending"]
            checks.append("Checkout cannot grant entitlement; signed paid exact-once replay, bad signature, provider rejection/unknown outcomes, pending upgrade, cancel/resume, unpaid downgrade and paid recovery")

            final = call("GET", pp)
            assert final["totals"] == [{"currency": "USD", "amountMinor": 2460}]
            sql = """SELECT (SELECT COUNT(*) FROM expenses) AS expenses,
                (SELECT COUNT(*) FROM expense_events WHERE actor_id='usr_github_710002') AS member_events,
                (SELECT COUNT(*) FROM d1_command_guard) AS outstanding_guards,
                (SELECT COUNT(*) FROM billing_webhook_receipts WHERE event_id='evt_native_bad') AS bad_receipts,
                (SELECT COUNT(*) FROM billing_webhook_receipts WHERE event_id='evt_native_paid') AS paid_receipts,
                (SELECT COUNT(*) FROM workspace_members WHERE removed_at IS NOT NULL) AS removed_members,
                (SELECT COUNT(*) FROM workspace_invites WHERE status='accepted' AND length(token_hash)=64) AS hashed_invites,
                (SELECT COUNT(*) FROM workspace_events) AS workspace_events,
                (SELECT COUNT(*) FROM ledger_plan_usage WHERE owner_id='usr_github_710002') AS member_usage;"""
            sql_results = json.loads(d1(["execute", "DB", "--command", sql, "--json"]))[0]["results"][0]
            assert sql_results == {"expenses": 2, "member_events": 1, "outstanding_guards": 0,
                "bad_receipts": 0, "paid_receipts": 1, "removed_members": 1,
                "hashed_invites": 1, "workspace_events": 5, "member_usage": 0}, sql_results
            evidence.update({"passed": True, "checks": checks, "sqlAssertions": sql_results})
    except Exception as error:
        evidence["failure"] = str(error)
        raise
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        log_text = runtime_log.read_text() if runtime_log.exists() else ""
        meta, fixtures = [], {"github_fixture": 0, "creem_fixture": 0}
        for line in log_text.splitlines():
            if '{"nativeAcceptance' not in line:
                continue
            try:
                parsed = json.loads(line[line.index('{"nativeAcceptance'):])
            except ValueError:
                continue
            meta.extend(parsed.get("nativeAcceptanceMeta", []))
            if "nativeAcceptanceProvider" in parsed:
                fixtures[parsed["nativeAcceptanceProvider"]] += 1
        assert all(type(row["rowsRead"]) is int and type(row["rowsWritten"]) is int for row in meta)
        evidence.update({"localHttpRequests": count, "localHttpCap": 100,
            "checks": checks,
            "localNativeStatements": len(meta), "localNativeRowsRead": sum(row["rowsRead"] for row in meta),
            "localNativeRowsWritten": sum(row["rowsWritten"] for row in meta),
            "nativeAttemptsValues": sorted({row["attempts"] for row in meta}, key=str),
            "nativeAttemptsFabricated": False, "fixtureGatewayCalls": fixtures,
            "responses": responses, "runtimeStopped": process is None or process.poll() is not None})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({name: evidence[name] for name in ("passed", "localHttpRequests",
            "localNativeStatements", "localNativeRowsRead", "localNativeRowsWritten", "runtimeStopped")}))


if __name__ == "__main__":
    main()
