"""Finite local Python Workers/NativeD1 acceptance for standalone projects.

All identities, sessions, memberships and provider transports are synthetic.
Only fresh local D1 is used; the canonical entry and application modules are
copied byte for byte. This is authenticated API evidence, not real-account,
remote-preview, payment, browser or populated-schema migration acceptance.
There are no client retries or resume/reset modes.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pullwise_server.cloudflare_d1_mapping import initialize_account
from pullwise_server.cloudflare_state_records import encode_record, record_name

spec = importlib.util.spec_from_file_location(
    "projects_native_helpers", ROOT / "scripts/check-projects-native-runtime.py")
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
WORKER = helpers.WORKER
HTTP_CAP, MUTATION_CAP = 50, 18
ACTORS = {role: "usr_github_" + str(750001 + index)
          for index, role in enumerate(("owner", "admin", "editor", "viewer"))}
OWNER = ACTORS["owner"]
CATEGORY = "cat_native_blank"
BUNDLE_SHA256 = "3c3fd5a4179230e21e018e28b0e735e7c4abd8fe8e4ff39d2fd6bb2ea3a2e260"
PROTECTED_MODULES = ("cloudflare_ledger_api.py", "cloudflare_ledger_expenses.py",
                     "cloudflare_project_repositories.py")

ENTRY = r'''
import json
from urllib.parse import urlsplit
from workers import Response
import application_entry as canonical
from pullwise_server.cloudflare_native_d1 import NativeD1

observed_meta, observed_provider = [], []


def field(value, name):
    try:
        return value.get(name)
    except (AttributeError, TypeError):
        return getattr(value, name, None)


def record(results):
    for result in results:
        meta = field(result, "meta")
        observed_meta.append({"rowsRead": field(meta, "rows_read"),
            "rowsWritten": field(meta, "rows_written"),
            "attempts": field(meta, "total_attempts")})


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
            raise ValueError("native fixture statement ownership mismatch")
        results = await self.original.batch([item.statement for item in statements])
        record(results)
        return results


class FixtureGitHub:
    mode = "deny"
    def __init__(self, env):
        pass
    def count(self, operation):
        observed_provider.append("github:" + operation)
        if self.mode == "deny":
            raise ValueError("standalone request touched GitHub fixture")
    async def unseal(self, value):
        self.count("unseal")
        if value not in {"sealed:synthetic-owner", "sealed:synthetic-admin",
                         "sealed:synthetic-editor", "sealed:synthetic-viewer"}:
            raise ValueError("unexpected synthetic token")
        return value.removeprefix("sealed:")
    async def installations(self, token):
        self.count("installations")
        if self.mode == "lost":
            return []
        return [{"id": 551, "account": {"id": 750001,
            "login": "synthetic-owner", "type": "User"}}]
    async def repositories(self, token, installation_id):
        self.count("repositories")
        if installation_id != 551:
            raise ValueError("unexpected synthetic installation")
        return [{"id": 701, "full_name": "private/blank-native-linked"}]
    async def _json(self, *args, **kwargs):
        self.count("unexpected_json")
        raise ValueError("unreviewed synthetic GitHub operation")


class FixtureCreem:
    def __init__(self, env):
        pass
    def __getattr__(self, name):
        async def blocked(*args, **kwargs):
            observed_provider.append("creem:" + name)
            raise ValueError("payment transport excluded from standalone acceptance")
        return blocked


canonical.WorkerGitHubGateway = FixtureGitHub
canonical.WorkerCreemGateway = FixtureCreem
canonical.NativeD1 = RecordingD1


class Default(canonical.Default):
    async def fetch(self, request):
        if getattr(self.env, "PULLWISE_MODE", "") != "local":
            return Response.json({"error": {"code": "LOCAL_FIXTURE_ONLY"}}, status=503)
        observed_meta.clear()
        observed_provider.clear()
        FixtureGitHub.mode = request.headers.get("x-native-fixture-access") or "deny"
        if FixtureGitHub.mode not in {"deny", "normal", "lost"}:
            return Response.json({"error": {"code": "INVALID_FIXTURE_MODE"}}, status=422)
        response = await super().fetch(request)
        print(json.dumps({"nativeBlankRequest": {
            "id": int(request.headers.get("x-native-fixture-call") or "0"),
            "method": request.method, "path": urlsplit(str(request.url)).path,
            "status": response.status, "meta": list(observed_meta),
            "providerOperations": list(observed_provider)}}))
        return response
'''


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def statement_sql(sql, values):
    pieces = sql.split("?")
    assert len(pieces) == len(values) + 1
    return "".join(piece + (helpers.literal(values[index]) if index < len(values) else "")
                   for index, piece in enumerate(pieces)) + ";"


def seed_sql(now):
    commands = []
    for role, identity in ACTORS.items():
        user = {"id": identity, "githubId": identity.removeprefix("usr_github_"),
            "githubLogin": "synthetic-" + role, "name": "Synthetic " + role,
            "providers": ["github"], "githubAccessToken": "sealed:synthetic-" + role,
            "createdAt": now - 1000, "billing": {"plan": "free"}}
        if role == "owner":
            user["billing"] = {"plan": "pro", "status": "active",
                "subscriptionId": "sub_synthetic_blank_owner", "interval": "month",
                "currentPeriodStart": now - 1000, "currentPeriodEnd": now + 86400}
        snapshot = encode_record("users", identity, user)
        commands.append(statement_sql("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
            (record_name("users", identity), snapshot, now)))
        session_id = "native-blank-" + role
        session = {"id": session_id, "userId": identity, "expiresAt": now + 3600}
        commands.append(statement_sql("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
            (record_name("sessions", session_id), encode_record("sessions", session_id, session), now)))
        commands.extend(statement_sql(sql, values) for sql, values in initialize_account(
            owner_id=identity, account_snapshot=snapshot, now=now))
        if role != "owner":
            commands.append(statement_sql("""INSERT INTO workspace_members(workspace_id,user_id,
                role,revision,joined_at,updated_at,invited_by_user_id) VALUES(?,?,?,1,?,?,?)""",
                (OWNER, identity, role, "2026-10-07", "2026-10-07", OWNER)))
    commands.append(statement_sql("""INSERT INTO expense_categories(id,owner_id,name,
        revision,created_at,updated_at) VALUES(?,?,?,1,?,?)""",
        (CATEGORY, OWNER, "Synthetic operating costs", "2026-10-07", "2026-10-07")))
    return "\n".join(commands)


def assert_blank(project, *, active=True, totals=None):
    assert project["githubRepoId"] is None and project["githubFullName"] is None
    assert project["githubRepoIds"] == [] and project["repositories"] == []
    assert project["githubOrganizationId"] is None and project["githubOrganization"] is None
    assert project["githubAccess"] == "not_linked" and project["canCreateExpense"] is active
    assert project["status"] == ("active" if active else "archived")
    assert project["name"].strip()
    if totals is not None:
        assert project["totals"] == totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8894)
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    if not any(directory.is_relative_to(root) for root in (Path("/workspace"), Path("/tmp"))):
        raise SystemExit("Run directory must be a new writable workspace or /tmp directory")
    if directory.exists() or not 1024 <= args.port <= 64000:
        raise SystemExit("Fresh run directory and bounded unprivileged port required")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", args.port))
    config = helpers.prepare(directory)
    (directory / "src/entry.py").write_text(ENTRY, encoding="utf-8")
    raw_config = json.loads(config.read_text())
    raw_config["name"] = "pullwise-blank-projects-native-local"
    raw_config["vars"].update({"PULLWISE_APP_URL": "https://local.example.test",
        "PULLWISE_ALLOWED_ORIGINS": f"http://127.0.0.1:{args.port}"})
    assert raw_config["vars"]["PULLWISE_MODE"] == "local"
    assert raw_config["vars"]["PULLWISE_D1_ACCESS_ENABLED"] == "1"
    assert not raw_config.get("routes") and not raw_config.get("workers_dev")
    assert len(raw_config["d1_databases"]) == 1
    assert raw_config["d1_databases"][0]["remote"] is False
    assert not any(name in raw_config for name in ("services", "durable_objects", "r2_buckets", "kv_namespaces"))
    config.write_text(json.dumps(raw_config, indent=2) + "\n", encoding="utf-8")
    entry_hash = sha256(WORKER / "src/entry.py")
    module_hashes = {name: sha256(ROOT / "pullwise_server" / name) for name in PROTECTED_MODULES}
    assert entry_hash == sha256(directory / "src/application_entry.py")
    assert all(value == sha256(directory / "src/pullwise_server" / name)
               for name, value in module_hashes.items())
    persistence = directory / "state"
    bundle_cache = Path("/workspace/.cache/pullwise-pyodide")
    bundle = bundle_cache / "pyodide_314.0.6_2026-08-17_6.capnp.bin"
    if not bundle.is_file() or sha256(bundle) != BUNDLE_SHA256:
        raise SystemExit("Pinned official native Python bundle cache missing or invalid")
    wrapper = directory / "workerd-local-cache.sh"
    wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(str(
        WORKER / "node_modules/@cloudflare/workerd-linux-64/bin/workerd"))
        + ' "$@" ' + shlex.quote("--pyodide-bundle-disk-cache-dir=" + str(bundle_cache)) + "\n")
    wrapper.chmod(0o700)
    env = {**os.environ, "UV_CACHE_DIR": "/workspace/.cache/uv",
        "UV_PYTHON_INSTALL_DIR": "/workspace/.python", "XDG_CACHE_HOME": "/workspace/.cache",
        "XDG_CONFIG_HOME": "/workspace/.config", "WRANGLER_SEND_METRICS": "false",
        "WRANGLER_LOG_PATH": str(directory / "wrangler-debug.log"),
        "MINIFLARE_WORKERD_PATH": str(wrapper)}
    cli = ["node", str(WORKER / "node_modules/wrangler/wrangler-dist/cli.js")]
    setup = []

    def d1(arguments, label):
        run = subprocess.run([*cli, "d1", *arguments, "--local", "--config", str(config),
            "--persist-to", str(persistence)], cwd=WORKER, env=env,
            capture_output=True, text=True, timeout=90)
        (directory / (label + ".log")).write_text(run.stdout + run.stderr, encoding="utf-8")
        assert run.returncode == 0, "Local D1 " + label + " failed; inspect private log"
        setup.append(label)
        return run.stdout

    count, mutations, responses, checks = 0, 0, [], []
    process = None
    runtime_log = directory / "native-runtime.log"
    evidence = {"passed": False, "localOnly": True, "nativePythonFFI": True,
        "canonicalApplicationLocalMode": True, "previewJournalExercised": False,
        "syntheticAccounts": 4, "realAccounts": 0, "remoteRequests": 0,
        "remoteD1RowsRead": 0, "remoteD1RowsWritten": 0, "realProviderRequests": 0,
        "realPayments": 0, "clientRetries": 0, "freshLocalDatabase": True,
        "populatedMigrationAcceptance": False, "canonicalEntrySha256": entry_hash,
        "canonicalProtectedModuleSha256": module_hashes,
        "localHttpCap": HTTP_CAP, "localMutationHttpCap": MUTATION_CAP,
        "fixtureRoles": list(ACTORS), "fixtureCategorySeeded": True,
        "fixtureWorkspacePlan": "pro", "fixtureOtherAccountPlans": "free",
        "fixtureProjectsSeeded": 0, "fixtureExpensesSeeded": 0}
    opener = build_opener(ProxyHandler({}), helpers.NoRedirect())

    def call(method, path, body=None, *, actor="owner", headers=None,
             expected=200, code=None, providers="zero", access="deny"):
        nonlocal count, mutations
        count += 1
        mutations += int(method != "GET")
        assert count <= HTTP_CAP and mutations <= MUTATION_CAP, "Finite HTTP cap exceeded"
        assert method in {"GET", "POST", "PATCH", "DELETE"}
        assert path.startswith(("/api/v1/", "/api-keys"))
        assert "#" not in path and not path.startswith("//")
        assert providers in {"zero", "positive"}
        auth = {"Cookie": "pw_session=native-blank-" + actor,
                "X-Pullwise-Workspace": OWNER} if actor else {}
        outgoing = {**auth, "Origin": f"http://127.0.0.1:{args.port}",
            "Content-Type": "application/json", "X-Native-Fixture-Call": str(count),
            "X-Native-Fixture-Access": access, **(headers or {})}
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode() if body is not None else None
        request = Request(f"http://127.0.0.1:{args.port}{path}", method=method,
                          headers=outgoing, data=data)
        try:
            response = opener.open(request, timeout=45)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read().decode("utf-8")
            payload = json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw
            responses.append({"id": count, "method": method, "path": path.split("?")[0],
                "status": response.status, "actor": actor or "restricted_key",
                "expectedProviderDependency": providers})
            if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                responses[-1]["errorCode"] = payload["error"].get("code")
            assert response.status == expected, f"Request {count} {method} {path.split('?')[0]} status {response.status}, expected {expected}"
            if code:
                assert payload["error"]["code"] == code, "Unexpected error code on request " + str(count)
            return payload

    try:
        d1(["migrations", "apply", "DB"], "fresh-migrations")
        seed = directory / "seed.sql"
        seed.write_text(seed_sql(int(time.time())), encoding="utf-8")
        d1(["execute", "DB", "--file", str(seed), "--json"], "synthetic-seed")
        with runtime_log.open("w") as log:
            process = subprocess.Popen([str(WORKER / ".venv/bin/pywrangler"), "dev", "--local",
                "--config", str(config), "--ip", "127.0.0.1", "--port", str(args.port),
                "--inspector-port", str(args.port + 1000), "--persist-to", str(persistence)],
                cwd=WORKER, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError("Native Worker startup failed; inspect private log")
                if "Ready on http://127.0.0.1:" + str(args.port) not in runtime_log.read_text():
                    time.sleep(0.2)
                    continue
                try:
                    with socket.create_connection(("127.0.0.1", args.port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.2)
            else:
                raise AssertionError("Native Worker did not bind within 120 seconds")

            for role, identity in ACTORS.items():
                profile = call("GET", "/api/v1/me", actor=role)
                assert profile["id"] == identity and profile["workspace"]["id"] == OWNER
                assert profile["workspace"]["role"] == role
                permissions = profile["workspace"]["permissions"]
                assert permissions["manageProjects"] is (role in {"owner", "admin"})
                assert permissions["manageCategories"] is (role in {"owner", "admin"})
                assert permissions["manageMembers"] is (role in {"owner", "admin"})
                assert permissions["writeExpenses"] is (role != "viewer")
                assert ("projects:write" in profile["scopes"]) is (role in {"owner", "admin"})
                assert ("categories:write" in profile["scopes"]) is (role in {"owner", "admin"})
                assert ("expenses:write" in profile["scopes"]) is (role != "viewer")
            for role in ("admin", "editor", "viewer"):
                assert call("GET", "/api/v1/categories", actor=role)[0]["id"] == CATEGORY
                members = call("GET", f"/api/v1/workspaces/{OWNER}/members", actor=role)
                assert len(members["items"]) == 4
            checks.append("Four synthetic cookie actors expose exact project/category/member/expense role permissions and can read authorized workspace data")

            blank_a = call("POST", "/api/v1/projects", {"name": "  Independent costs 汉字🙂  "}, expected=201)
            blank_b = call("POST", "/api/v1/projects", {"name": "Admin operating costs", "githubRepoIds": [],
                "githubOrganizationId": None}, actor="admin", expected=201)
            assert_blank(blank_a, totals=[])
            assert_blank(blank_b, totals=[])
            assert blank_a["name"] == "Independent costs 汉字🙂" and blank_a["id"] != blank_b["id"]
            pid, pp = blank_a["id"], "/api/v1/projects/" + blank_a["id"]
            assert len(call("GET", "/api/v1/projects")["items"]) == 2
            call("POST", "/api/v1/projects", {"name": "Editor forbidden"}, actor="editor", expected=403, code="ROLE_FORBIDDEN")
            call("POST", "/api/v1/categories", {"name": "Editor forbidden"}, actor="editor", expected=403, code="ROLE_FORBIDDEN")
            draft = {"target": {"kind": "project", "projectId": pid}, "occurredOn": "2026-10-07",
                "amount": "12.30", "currency": "USD", "categoryId": CATEGORY, "purpose": "=公式 汉字🙂"}
            call("POST", "/api/v1/expenses", draft, actor="viewer",
                headers={"Idempotency-Key": "native-blank-viewer-denied"}, expected=403, code="ROLE_FORBIDDEN")
            key = call("POST", "/api-keys", {"name": "Temporary synthetic restricted key",
                "scopes": ["projects:read", "expenses:read", "expenses:write", "reports:read"],
                "restrictions": {"workspaceId": OWNER, "projectIds": [pid], "shared": False}},
                actor="editor", expected=201)
            assert key["restrictions"]["workspaceMemberRevision"] == 1
            key_headers = {"Authorization": "Bearer " + key["key"]}
            call("GET", "/api/v1/me", actor=None, headers=key_headers, expected=403, code="INSUFFICIENT_SCOPE")
            call("GET", "/api/v1/categories", actor=None, headers=key_headers, expected=403, code="INSUFFICIENT_SCOPE")
            call("GET", f"/api/v1/workspaces/{OWNER}/members", actor=None, headers=key_headers, expected=403, code="INSUFFICIENT_SCOPE")
            assert [item["id"] for item in call("GET", "/api/v1/projects", actor=None, headers=key_headers)["items"]] == [pid]
            assert_blank(call("GET", pp, actor=None, headers=key_headers), totals=[])
            call("GET", "/api/v1/projects/" + blank_b["id"], actor=None, headers=key_headers, expected=403, code="TARGET_FORBIDDEN")
            call("GET", "/api/v1/expenses?target=shared", actor=None, headers=key_headers, expected=403, code="TARGET_FORBIDDEN")
            key_write = {**key_headers, "Idempotency-Key": "native-blank-key-create"}
            expense = call("POST", "/api/v1/expenses", draft, actor=None, headers=key_write, expected=201)
            replay = call("POST", "/api/v1/expenses", draft, actor=None, headers=key_write, expected=201)
            assert replay == expense and expense["amountMinor"] == 1230
            ep = "/api/v1/expenses/" + expense["id"]
            assert call("GET", ep, actor=None, headers=key_headers)["id"] == expense["id"]
            assert_blank(call("GET", pp, actor="viewer"), totals=[{"currency": "USD", "amountMinor": 1230}])
            summary = call("GET", "/api/v1/reports/summary?projectId=" + pid, actor=None, headers=key_headers)
            assert summary["groups"] and all(item["amountMinor"] == 1230 for item in summary["groups"])
            assert call("GET", "/api/v1/reports/timeseries?projectId=" + pid)["groups"]
            assert call("GET", "/api/v1/reports/categories?projectId=" + pid)["groups"]
            exported = list(csv.DictReader(io.StringIO(call("GET", "/api/v1/expenses/export?projectId=" + pid,
                actor=None, headers=key_headers))))
            assert len(exported) == 1 and exported[0]["purpose"] == "'=公式 汉字🙂"
            checks.append("Named and explicit-empty standalone create; restricted Editor key exact-once expense create/replay/read, missing scopes, project/shared visibility, reports and UTF-8/formula-safe CSV")

            moved = call("PATCH", ep, {**draft, "target": {"kind": "shared"}}, actor="editor", headers={"If-Match": '"1"'})
            assert moved["target"] == {"kind": "shared"} and moved["revision"] == 2
            call("GET", ep, actor=None, headers=key_headers, expected=403, code="TARGET_FORBIDDEN")
            returned = call("PATCH", ep, draft, actor="editor", headers={"If-Match": '"2"'})
            assert returned["id"] == expense["id"] and returned["revision"] == 3
            archived = call("PATCH", pp, {"name": "Renamed independent costs", "status": "archived"}, headers={"If-Match": '"1"'})
            assert archived["revision"] == 2 and archived["name"] == "Renamed independent costs"
            assert_blank(archived, active=False, totals=[{"currency": "USD", "amountMinor": 1230}])
            edited = call("PATCH", ep, {**draft, "purpose": "Edited archived 汉字🙂"}, actor="editor", headers={"If-Match": '"3"'})
            assert edited["id"] == expense["id"] and edited["revision"] == 4
            active = call("PATCH", pp, {"status": "active"}, actor="admin", headers={"If-Match": '"2"'})
            assert active["id"] == pid and active["revision"] == 3
            assert_blank(active, totals=[{"currency": "USD", "amountMinor": 1230}])
            checks.append("Cookie Editor shared↔standalone move keeps one expense; Owner rename/archive and Editor historic edit survive; Admin standalone reactivation needs no provider")

            call("PATCH", pp, {"githubRepoIds": [701]}, headers={"If-Match": '"2"'}, expected=412, code="PRECONDITION_FAILED")
            attached = call("PATCH", pp, {"githubRepoIds": [701]}, headers={"If-Match": '"3"'}, access="normal", providers="positive")
            assert attached["id"] == pid and attached["revision"] == 4 and attached["githubRepoIds"] == [701]
            assert attached["totals"] == [{"currency": "USD", "amountMinor": 1230}]
            lost = call("GET", pp, actor=None, headers=key_headers, access="lost", providers="positive")
            assert lost["githubAccess"] == "lost" and not lost["canCreateExpense"]
            assert lost["githubFullName"] is None and lost["repositories"][0]["githubFullName"] is None
            assert "private/blank-native-linked" not in json.dumps(lost)
            call("POST", "/api/v1/expenses", draft, actor=None,
                headers={**key_headers, "Idempotency-Key": "native-blank-lost-new"}, access="lost", providers="positive",
                expected=403, code="GITHUB_ACCESS_REQUIRED")
            detached = call("PATCH", pp, {"githubRepoIds": []}, actor="admin", headers={"If-Match": '"4"'})
            assert detached["id"] == pid and detached["revision"] == 5
            assert_blank(detached, totals=[{"currency": "USD", "amountMinor": 1230}])
            assert call("GET", ep, actor=None, headers=key_headers)["revision"] == 4
            checks.append("Stale attach CAS fails before provider; attach preserves stable history; lost linked grants hide metadata/reject new key writes; Admin detach returns to standalone without provider")

            call("DELETE", "/api-keys/" + key["id"], actor="editor")
            call("GET", ep, actor=None, headers=key_headers, expected=401, code="UNAUTHENTICATED")
            listed_keys = call("GET", "/api-keys", actor="editor")
            assert listed_keys["apiKeys"] == [] and listed_keys["items"] == []
            del key_headers, key_write, key
            assert_blank(call("GET", pp), totals=[{"currency": "USD", "amountMinor": 1230}])
            assert_blank(call("GET", "/api/v1/projects/" + blank_b["id"]), totals=[])
            checks.append("Temporary key revoked through issuing cookie account; next authenticated read fails immediately; revoked key is absent from account list")
    except Exception as error:
        evidence["failure"] = {"type": type(error).__name__, "lastRequest": count,
            "lastStatus": responses[-1]["status"] if responses else None}
        raise
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        logs = runtime_log.read_text() if runtime_log.exists() else ""
        observed = []
        for line in logs.splitlines():
            if '{"nativeBlankRequest"' in line:
                observed.append(json.loads(line[line.index('{"nativeBlankRequest"'):])["nativeBlankRequest"])
        observed_rows = [row for request in observed for row in request["meta"]]
        complete_rows = all(type(row[name]) is int and row[name] >= 0
            for row in observed_rows for name in ("rowsRead", "rowsWritten"))
        evidence.update({"localHttpRequests": count, "localMutationHttpRequests": mutations,
            "runtimeStopped": process is None or process.poll() is not None, "checks": checks,
            "setupLocalD1Commands": setup, "responses": responses,
            "observedNativeRequestEvidence": len(observed),
            "localNativeStatements": len(observed_rows),
            "nativeRowsMetadataComplete": complete_rows,
            "localNativeRowsRead": sum(row["rowsRead"] for row in observed_rows) if complete_rows else None,
            "localNativeRowsWritten": sum(row["rowsWritten"] for row in observed_rows) if complete_rows else None,
            "nativeAttemptsValues": sorted({row["attempts"] for row in observed_rows}, key=str),
            "nativeAttemptsFabricated": False,
            "nativeAccountingScope": "authenticated Worker API requests only; setup and final SQL outside request totals",
            "fixtureGithubCallsIncludingUnseal": sum(len(request["providerOperations"])
                for request in observed)})
        if "failure" not in evidence:
            try:
                assert len(observed) == len(responses), "Missing native request evidence"
                meta = []
                for expected_request, actual in zip(responses, observed):
                    assert all(actual[name] == expected_request[name] for name in ("id", "method", "path", "status"))
                    assert all(type(row[name]) is int and row[name] >= 0 for row in actual["meta"]
                               for name in ("rowsRead", "rowsWritten"))
                    assert not any(value.startswith("creem:") for value in actual["providerOperations"])
                    assert bool(actual["providerOperations"]) is (expected_request["expectedProviderDependency"] == "positive")
                    if actual["method"] == "GET":
                        assert sum(row["rowsWritten"] for row in actual["meta"]) == 0
                    expected_request["nativeStatements"] = len(actual["meta"])
                    expected_request["nativeRowsRead"] = sum(row["rowsRead"] for row in actual["meta"])
                    expected_request["nativeRowsWritten"] = sum(row["rowsWritten"] for row in actual["meta"])
                    expected_request["fixtureProviderCalls"] = len(actual["providerOperations"])
                    meta.extend(actual["meta"])
                assert meta and len(meta) <= 1500
                assert sum(row["rowsRead"] for row in meta) <= 15000
                assert sum(row["rowsWritten"] for row in meta) <= 1000
                sql = """SELECT (SELECT COUNT(*) FROM ledger_projects) AS projects,
                    (SELECT COUNT(*) FROM ledger_projects WHERE github_repo_id IS NULL AND github_full_name IS NULL
                        AND github_organization_id IS NULL AND length(trim(name))>0) AS standalone_projects,
                    (SELECT COUNT(*) FROM ledger_project_repositories) AS bindings,
                    (SELECT COUNT(*) FROM expenses) AS expenses,
                    (SELECT COUNT(*) FROM expense_events) AS expense_events,
                    (SELECT COUNT(*) FROM expense_create_idempotency) AS idempotency_records,
                    (SELECT COUNT(*) FROM api_keys) AS keys,
                    (SELECT COUNT(*) FROM api_keys WHERE revoked_at IS NOT NULL) AS revoked_keys,
                    (SELECT COUNT(*) FROM d1_command_guard) AS outstanding_guards,
                    (SELECT COUNT(*) FROM workspace_members) AS members,
                    (SELECT COUNT(*) FROM app_state WHERE name GLOB 'record:*') AS typed_records,
                    (SELECT COUNT(*) FROM pragma_foreign_key_check) AS foreign_key_violations;"""
                assertions = json.loads(d1(["execute", "DB", "--command", sql, "--json"], "final-assertions"))[0]["results"][0]
                assert assertions == {"projects": 2, "standalone_projects": 2, "bindings": 0,
                    "expenses": 1, "expense_events": 4, "idempotency_records": 1,
                    "keys": 1, "revoked_keys": 1, "outstanding_guards": 0,
                    "members": 3, "typed_records": 8, "foreign_key_violations": 0}, "Final native SQL counts differ"
                rows = json.loads(d1(["execute", "DB", "--command", """SELECT owner_id,project_id,revision,
                    amount_minor,purpose FROM expenses; SELECT owner_id,projects,records,writes FROM ledger_plan_usage;
                    SELECT actor_kind,actor_id,action FROM expense_events ORDER BY created_at,id;""",
                    "--json"], "final-history"))
                stored = rows[0]["results"]
                assert stored == [{"owner_id": OWNER, "project_id": pid, "revision": 4,
                    "amount_minor": 1230, "purpose": "Edited archived 汉字🙂"}], "Expense history changed"
                usage = rows[1]["results"]
                assert usage == [{"owner_id": OWNER, "projects": 2, "records": 1, "writes": 11}], "Plan usage differs"
                events = rows[2]["results"]
                assert sum(row["actor_kind"] == "api_key" and row["actor_id"].startswith(ACTORS["editor"] + ":sha256:") for row in events) == 1
                assert sum(row["actor_kind"] == "session" and row["actor_id"] == ACTORS["editor"] for row in events) == 3
                evidence.update({"passed": True, "sqlAssertions": assertions,
                    "planUsage": usage[0], "stableExpenseHistoryVerified": True,
                    "actualActorAuditVerified": True, "allStandaloneProviderCalls": 0,
                    "localNativeStatements": len(meta), "localNativeRowsRead": sum(row["rowsRead"] for row in meta),
                    "localNativeRowsWritten": sum(row["rowsWritten"] for row in meta),
                    "nativeAccountingScope": "authenticated Worker API requests only; setup and final SQL outside request totals",
                    "nativeAttemptsValues": sorted({row["attempts"] for row in meta}, key=str),
                    "nativeAttemptsFabricated": False,
                    "fixtureGithubCallsIncludingUnseal": sum(len(row["providerOperations"]) for row in observed),
                    "fixtureCreemCalls": 0, "temporaryKeyRevoked": True,
                    "allGetRequestsWriteZeroRows": True})
            except Exception as error:
                evidence["failure"] = {"type": type(error).__name__, "phase": "evidence-or-native-sql-verification"}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({name: evidence.get(name) for name in ("passed", "localHttpRequests",
            "localMutationHttpRequests", "localNativeStatements", "localNativeRowsRead",
            "localNativeRowsWritten", "runtimeStopped", "failure")}))
        if not evidence["passed"] and "failure" in evidence and sys.exc_info()[0] is None:
            raise SystemExit("Native evidence/SQL assertion failed; inspect preserved local evidence")


if __name__ == "__main__":
    main()
