"""Ledger REST behavior with synthetic identity, GitHub access and SQLite D1."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_api_key_write import create_api_key
from pullwise_server.cloudflare_preview_schema import UPGRADE_V6_SQL, UPGRADE_V7_SQL, UPGRADE_V8_SQL, UPGRADE_V9_SQL, UPGRADE_V10_SQL, UPGRADE_V11_SQL
from test_cloudflare_github_identity_http import D1ShapedSQLite, GitHubStub, call, login, seed
from urllib.parse import parse_qs, urlsplit


class LedgerRoutesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        fixture, _, _ = seed(Path(self.directory.name) / "domain.db")
        self.store, self.now = fixture.store, fixture.now
        migration = Path(__file__).resolve().parents[1] / "cloudflare/server/migrations/0001_ledger.sql"
        with self.store.connect() as db:
            db.executescript(migration.read_text())
            db.executescript((migration.parent / "0003_ledger_suggestions.sql").read_text())
            db.executescript((migration.parent / "0004_ledger_plan_usage.sql").read_text())
            db.execute("""CREATE TABLE api_keys(id TEXT PRIMARY KEY,user_id TEXT,name TEXT,
                key_prefix TEXT,key_hash TEXT UNIQUE,scopes TEXT,expires_at INTEGER,
                restrictions TEXT,created_at INTEGER,last_used_at INTEGER,revoked_at INTEGER)""")
            db.executescript((migration.parent / "0005_workspaces_repositories.sql").read_text())
            db.commit()
            db.execute("BEGIN")
            for sql in (*UPGRADE_V6_SQL, *UPGRADE_V7_SQL, *UPGRADE_V8_SQL, *UPGRADE_V9_SQL, *UPGRADE_V10_SQL, *UPGRADE_V11_SQL):
                db.execute(sql)
        self.binding = D1ShapedSQLite(self.store)
        _, _, headers = login(self.binding, GitHubStub(), self.now)
        self.headers = {"Cookie": headers["Set-Cookie"].split(";", 1)[0],
                        "Origin": "https://app.example.test"}
        status, payload, _ = call(self.binding, GitHubStub(), self.now + 2,
            "GET", "/integrations/github/authorize", headers=self.headers)
        self.assertEqual(status, 200)
        state = parse_qs(urlsplit(payload["url"]).query)["state"][0]
        status, _, _ = call(self.binding, GitHubStub(), self.now + 3,
            "GET", "/integrations/github/callback",
            {"state": state, "installation_id": "501"}, self.headers)
        self.assertEqual(status, 302)

    def tearDown(self):
        self.directory.cleanup()

    def call(self, method, path, body=None, headers=None, params=None, gateway=None):
        return asyncio.run(handle_ledger_request(binding=self.binding, gateway=gateway or GitHubStub(),
            method=method, path=path, headers=headers or self.headers, params=params or {},
            body=body, now=self.now + 3))

    def test_project_category_owner_and_revision(self):
        status, project = self.call("POST", "/api/v1/projects", {"githubRepoId": 202, "description": "My work"})
        self.assertEqual(status, 201)
        self.assertEqual(project["githubRepoId"], 202)
        status, page = self.call("GET", "/api/v1/projects")
        self.assertEqual(status, 200)
        self.assertEqual(page["items"][0]["id"], project["id"])
        status, stale = self.call("PATCH", "/api/v1/projects/" + project["id"],
                                  {"description": "changed"}, {**self.headers, "If-Match": '"2"'})
        self.assertEqual(status, 412)
        status, updated = self.call("PATCH", "/api/v1/projects/" + project["id"],
                                    {"description": "changed"}, {**self.headers, "If-Match": '"1"'})
        self.assertEqual(status, 200)
        self.assertEqual(updated["revision"], 2)
        status, category = self.call("POST", "/api/v1/categories", {"name": "Tools"})
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/api/v1/categories", {"name": "tools"})
        self.assertEqual(status, 409)
        status, _ = self.call("DELETE", "/api/v1/categories/" + category["id"],
                              headers={**self.headers, "If-Match": '"1"'})
        self.assertEqual(status, 204)

    def test_expense_precision_idempotency_move_audit_and_remove(self):
        _, project = self.call("POST", "/api/v1/projects", {"githubRepoId": 202})
        _, category = self.call("POST", "/api/v1/categories", {"name": "Tools"})
        draft = {"target": {"kind": "project", "projectId": project["id"]},
            "occurredOn": "2026-09-27", "amount": "12.30", "currency": "USD",
            "categoryId": category["id"], "purpose": "Hosting"}
        write = {**self.headers, "Idempotency-Key": "create-1"}
        status, expense = self.call("POST", "/api/v1/expenses", draft, write)
        self.assertEqual(status, 201)
        self.assertEqual(expense["amountMinor"], 1230)
        status, project_before = self.call("GET", "/api/v1/projects/" + project["id"])
        self.assertEqual(project_before["totals"], [{"currency": "USD", "amountMinor": 1230}])
        status, replay = self.call("POST", "/api/v1/expenses", draft, write)
        self.assertEqual(status, 201)
        self.assertEqual(replay["id"], expense["id"])
        status, _ = self.call("POST", "/api/v1/expenses", {**draft, "amount": "12.31"}, write)
        self.assertEqual(status, 409)
        status, _ = self.call("POST", "/api/v1/expenses", {**draft, "amount": "12.301"},
                              {**self.headers, "Idempotency-Key": "create-2"})
        self.assertEqual(status, 422)
        moved = {**draft, "target": {"kind": "shared"}}
        status, updated = self.call("PATCH", "/api/v1/expenses/" + expense["id"], moved,
                                    {**self.headers, "If-Match": '"1"'})
        self.assertEqual(status, 200)
        self.assertEqual(updated["revision"], 2)
        status, project_after = self.call("GET", "/api/v1/projects/" + project["id"])
        self.assertEqual(project_after["totals"], [])
        status, _ = self.call("PATCH", "/api/v1/expenses/" + expense["id"], draft,
                              {**self.headers, "If-Match": '"1"'})
        self.assertEqual(status, 412)
        status, _ = self.call("DELETE", "/api/v1/expenses/" + expense["id"],
                              headers={**self.headers, "If-Match": '"2"'})
        self.assertEqual(status, 204)
        status, page = self.call("GET", "/api/v1/expenses")
        self.assertEqual(page["items"], [])
        with self.store.connect() as db:
            actions = [row[0] for row in db.execute("SELECT action FROM expense_events ORDER BY created_at,id")]
        self.assertCountEqual(actions, ["create", "update", "delete"])

    def test_reports_filter_currency_and_export(self):
        _, project = self.call("POST", "/api/v1/projects", {"githubRepoId": 202})
        _, category = self.call("POST", "/api/v1/categories", {"name": "Tools"})
        base = {"occurredOn": "2026-09-27", "currency": "USD",
            "categoryId": category["id"], "purpose": "=SUM(1,2)"}
        for index, target in enumerate(({"kind": "project", "projectId": project["id"]},
                                        {"kind": "shared"})):
            status, _ = self.call("POST", "/api/v1/expenses",
                {**base, "target": target, "amount": "10.00"},
                {**self.headers, "Idempotency-Key": f"report-{index}"})
            self.assertEqual(status, 201)
        status, summary = self.call("GET", "/api/v1/reports/summary")
        self.assertEqual(status, 200)
        self.assertEqual({(group["target"], group["amountMinor"]) for group in summary["groups"]},
                         {("project", 1000), ("shared", 1000), ("account", 2000)})
        status, daily = self.call("GET", "/api/v1/reports/timeseries", params={"target": "shared"})
        self.assertEqual(status, 200)
        self.assertEqual(daily["groups"][0]["bucket"], "2026-09-27")
        status, export = self.call("GET", "/api/v1/expenses/export")
        self.assertEqual(status, 200)
        async def collect():
            return "".join([chunk async for chunk in export.chunks()])
        csv = asyncio.run(collect())
        self.assertIn("'=SUM(1,2)", csv)
        status, page = self.call("GET", "/api/v1/expenses", params={"limit": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(len(page["items"]), 1)
        self.assertIsNotNone(page["nextCursor"])

    def test_export_reads_multiple_pages_without_a_row_cap(self):
        _, category = self.call("POST", "/api/v1/categories", {"name": "Hosting"})
        with self.store.connect() as db:
            owner = db.execute("SELECT owner_id FROM expense_categories WHERE id=?",
                (category["id"],)).fetchone()[0]
            db.executemany("""INSERT INTO expenses(id,owner_id,target_kind,category_id,
                occurred_on,amount_minor,currency,purpose,created_at,updated_at)
                VALUES(?,?,'shared',?,'2026-09-27',100,'USD','Hosting',?,?)""",
                [(f"exp_bulk_{number}", owner, category["id"],
                  "2026-09-27T00:00:00Z", "2026-09-27T00:00:00Z")
                 for number in range(260)])
        status, export = self.call("GET", "/api/v1/expenses/export")
        self.assertEqual(status, 200)
        async def collect():
            return [chunk async for chunk in export.chunks()]
        chunks = asyncio.run(collect())
        self.assertEqual(len(chunks), 3)
        self.assertEqual(sum(chunk.count("exp_bulk_") for chunk in chunks), 260)

    def test_lost_github_access_preserves_history_but_blocks_new_project_expense(self):
        _, project = self.call("POST", "/api/v1/projects", {"githubRepoId": 202})
        _, category = self.call("POST", "/api/v1/categories", {"name": "Tools"})
        draft = {"target": {"kind": "project", "projectId": project["id"]},
            "occurredOn": "2026-09-27", "amount": "1.00", "currency": "USD",
            "categoryId": category["id"], "purpose": "History"}
        _, expense = self.call("POST", "/api/v1/expenses", draft,
            {**self.headers, "Idempotency-Key": "history"})
        gateway = GitHubStub()
        async def missing(_token, _installation):
            return []
        gateway.repositories = missing
        status, hidden = self.call("GET", "/api/v1/projects/" + project["id"], gateway=gateway)
        self.assertEqual(status, 200)
        self.assertIsNone(hidden["githubFullName"])
        self.assertEqual(hidden["githubAccess"], "lost")
        status, _ = self.call("POST", "/api/v1/expenses", draft,
            {**self.headers, "Idempotency-Key": "new"}, gateway=gateway)
        self.assertEqual(status, 403)
        status, updated = self.call("PATCH", "/api/v1/expenses/" + expense["id"],
            {**draft, "purpose": "Edited history"},
            {**self.headers, "If-Match": '"1"'}, gateway=gateway)
        self.assertEqual(status, 200)
        self.assertEqual(updated["purpose"], "Edited history")

    def test_key_restrictions_apply_to_list_report_and_write(self):
        _, project = self.call("POST", "/api/v1/projects", {"githubRepoId": 202})
        status, key = asyncio.run(create_api_key(binding=self.binding, headers=self.headers,
            body={"scopes": ["expenses:read", "reports:read", "expenses:write"],
                  "restrictions": {"projectIds": [project["id"]], "shared": False}},
            now=self.now + 3))
        self.assertEqual(status, 201)
        token = {"Authorization": "Bearer " + key["key"]}
        status, _ = self.call("GET", "/api/v1/expenses", headers=token, params={"target": "shared"})
        self.assertEqual(status, 403)
        status, report = self.call("GET", "/api/v1/reports/summary", headers=token)
        self.assertEqual(status, 200)
        self.assertEqual(report["groups"], [])
        status, _ = self.call("POST", "/api/v1/expenses", {"target": {"kind": "shared"},
            "occurredOn": "2026-09-27", "amount": "1.00", "currency": "USD",
            "categoryId": "cat_missing", "purpose": "Denied"},
            {**token, "Idempotency-Key": "denied"})
        self.assertEqual(status, 403)

    def test_other_owner_project_is_hidden(self):
        with self.store.connect() as db:
            db.execute("""INSERT INTO ledger_projects(id,owner_id,github_repo_id,github_full_name,
                description,status,revision,created_at,updated_at)
                VALUES('prj_other','usr_other',999,'other/private','','active',1,'now','now')""")
        status, _ = self.call("GET", "/api/v1/projects/prj_other")
        self.assertEqual(status, 404)
        status, _ = self.call("PATCH", "/api/v1/projects/prj_other",
            {"description": "takeover"}, {**self.headers, "If-Match": '"1"'})
        self.assertEqual(status, 404)

    def test_github_failure_preserves_history_and_blocks_new_targets_without_writes(self):
        from pullwise_server.cloudflare_github_gateway import GitHubFailure
        _, project = self.call("POST", "/api/v1/projects", {"githubRepoId": 202})
        _, category = self.call("POST", "/api/v1/categories", {"name": "Tools"})
        draft = {"target": {"kind": "project", "projectId": project["id"]},
                 "occurredOn": "2026-09-27", "amount": "1.00", "currency": "USD",
                 "categoryId": category["id"], "purpose": "History"}
        _, expense = self.call("POST", "/api/v1/expenses", draft,
                              {**self.headers, "Idempotency-Key": "history-failure"})
        for code, state, status in (("GITHUB_REAUTHORIZATION_REQUIRED", "reauthorization_required", 403),
                                    ("GITHUB_PERMISSION_DENIED", "lost", 403),
                                    ("GITHUB_UNAVAILABLE", "unavailable", 503),
                                    ("GITHUB_RATE_LIMITED", "unavailable", 503),
                                    ("GITHUB_TOKEN_UNREADABLE", "unavailable", 503)):
            with self.subTest(code=code):
                class Broken(GitHubStub):
                    async def installations(self, _token):
                        raise GitHubFailure(code)
                gateway = Broken()
                with self.store.connect() as db:
                    before = list(db.iterdump())
                read_status, hidden = self.call("GET", "/api/v1/projects/" + project["id"], gateway=gateway)
                self.assertEqual(read_status, 200)
                self.assertIsNone(hidden["githubFullName"])
                self.assertEqual(hidden["githubAccess"], state)
                self.assertEqual(hidden["totals"], [{"currency": "USD", "amountMinor": 100}])
                read_status, page = self.call("GET", "/api/v1/projects", gateway=gateway)
                self.assertEqual(read_status, 200)
                self.assertEqual(page["items"][0]["githubAccess"], state)
                write_status, _ = self.call("POST", "/api/v1/projects", {"githubRepoId": 303}, gateway=gateway)
                self.assertEqual(write_status, status)
                write_status, _ = self.call("POST", "/api/v1/expenses", draft,
                    {**self.headers, "Idempotency-Key": "failure-new"}, gateway=gateway)
                self.assertEqual(write_status, status)
                with self.store.connect() as db:
                    self.assertEqual(list(db.iterdump()), before)
        status, updated = self.call("PATCH", "/api/v1/expenses/" + expense["id"],
            {**draft, "purpose": "Edited during outage"}, {**self.headers, "If-Match": '"1"'}, gateway=gateway)
        self.assertEqual(status, 200)
        self.assertEqual(updated["purpose"], "Edited during outage")

    def test_explicit_reauthorization_restores_grants_for_the_same_owner_and_history(self):
        _, project = self.call("POST", "/api/v1/projects", {"githubRepoId": 202, "description": "Retained history"})
        class Renewed(GitHubStub):
            async def exchange(self, code, redirect_uri, verifier):
                await super().exchange(code, redirect_uri, verifier)
                from pullwise_server.cloudflare_github_gateway import GitHubTokenBundle
                return GitHubTokenBundle("synthetic-renewed-token")

            async def profile(self, token):
                assert token == "synthetic-renewed-token"
                return {"id": 77, "login": "alice"}

            async def installations(self, token):
                assert token == "synthetic-renewed-token"
                return [{"id": 501}]
        gateway = Renewed()
        status, _, headers = login(self.binding, gateway, self.now + 5)
        self.assertEqual(status, 302)
        new_headers = {"Cookie": headers["Set-Cookie"].split(";", 1)[0]}
        status, history = self.call("GET", "/api/v1/projects/" + project["id"],
                                   headers=new_headers, gateway=gateway)
        self.assertEqual(status, 200)
        self.assertEqual(history["description"], "Retained history")
        self.assertEqual(history["githubAccess"], "authorized")
        self.assertEqual(history["githubFullName"], "alice/project")

    def test_currency_exponents_and_invalid_business_dates(self):
        _, category = self.call("POST", "/api/v1/categories", {"name": "Tools"})
        draft = {"target": {"kind": "shared"}, "occurredOn": "2026-09-27",
                 "categoryId": category["id"], "purpose": "Usage"}
        for index, (currency, amount, minor) in enumerate((
                ("JPY", "120", 120), ("KWD", "1.234", 1234), ("CLF", "1.2345", 12345))):
            status, saved = self.call("POST", "/api/v1/expenses",
                {**draft, "amount": amount, "currency": currency},
                {**self.headers, "Idempotency-Key": f"money-{index}"})
            self.assertEqual(status, 201)
            self.assertEqual(saved["amountMinor"], minor)
        status, report = self.call("GET", "/api/v1/reports/summary")
        self.assertEqual(status, 200)
        self.assertEqual({(group["currency"], group["amountMinor"]) for group in report["groups"]
                          if group["target"] == "account"},
                         {("JPY", 120), ("KWD", 1234), ("CLF", 12345)})
        for index, invalid in enumerate(({"amount": "-1.00", "currency": "USD"},
                                         {"amount": "1.001", "currency": "USD"},
                                         {"amount": "1.0", "currency": "JPY"},
                                         {"amount": "1.00", "currency": "XYZ"},
                                         {"amount": "90071992547409.92", "currency": "USD"},
                                         {"amount": "1.00", "currency": "USD",
                                          "occurredOn": "2026-02-30"})):
            status, _ = self.call("POST", "/api/v1/expenses", {**draft, **invalid},
                {**self.headers, "Idempotency-Key": f"invalid-{index}"})
            self.assertEqual(status, 422)
