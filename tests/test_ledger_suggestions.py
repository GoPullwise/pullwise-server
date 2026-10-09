"""Optional ledger suggestions must fail closed and never mutate expenses."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_ledger_suggestions import _draft
from pullwise_server.cloudflare_state_records import encode_record, record_name
from pullwise_server.cloudflare_preview_schema import UPGRADE_V6_SQL, UPGRADE_V7_SQL, UPGRADE_V8_SQL, UPGRADE_V9_SQL, UPGRADE_V10_SQL
from test_cloudflare_github_identity_http import D1ShapedSQLite, GitHubStub, login, seed
from state_record_fixtures import normalize_legacy_state


class LedgerSuggestionTests(unittest.TestCase):
    def test_duplicate_context_requires_valid_money_and_date(self):
        draft = {"target": {"kind": "shared"}, "purpose": "Hosting",
                 "occurredOn": "2026-09-27", "amount": "12.50", "currency": "USD"}
        self.assertEqual(_draft(draft)[-3:], ("2026-09-27", 1250, "USD"))
        self.assertIsNone(_draft({**draft, "amount": "12.501"}))
        self.assertIsNone(_draft({**draft, "occurredOn": "2026-02-30"}))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        fixture, _, _ = seed(Path(self.directory.name) / "suggestions.db")
        self.store, self.now = fixture.store, fixture.now
        with self.store.connect() as db:
            db.executescript((Path(__file__).resolve().parents[1] /
                "cloudflare/server/migrations/0001_ledger.sql").read_text())
            db.executescript((Path(__file__).resolve().parents[1] /
                "cloudflare/server/migrations/0003_ledger_suggestions.sql").read_text())
            db.execute("""CREATE TABLE api_keys(id TEXT PRIMARY KEY,user_id TEXT,name TEXT,
                key_prefix TEXT,key_hash TEXT UNIQUE,scopes TEXT,expires_at INTEGER,
                restrictions TEXT,created_at INTEGER,last_used_at INTEGER,revoked_at INTEGER)""")
            db.executescript((Path(__file__).resolve().parents[1] /
                "cloudflare/server/migrations/0005_workspaces_repositories.sql").read_text())
            db.commit()
            db.execute("BEGIN")
            for sql in (*UPGRADE_V6_SQL, *UPGRADE_V7_SQL, *UPGRADE_V8_SQL, *UPGRADE_V9_SQL, *UPGRADE_V10_SQL):
                db.execute(sql)
            normalize_legacy_state(db, now=self.now)
        self.binding = D1ShapedSQLite(self.store)
        _, _, headers = login(self.binding, GitHubStub(), self.now)
        self.headers = {"Cookie": headers["Set-Cookie"].split(";", 1)[0],
                        "Origin": "https://app.example.test"}
        with self.store.connect() as db:
            owner_id = "usr_github_77"
            name = record_name("users", owner_id)
            user = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?", (name,)).fetchone()[0])
            user["billing"] = {"plan": "max", "status": "active", "currentPeriodEnd": self.now + 86400}
            db.execute("UPDATE app_state SET payload=? WHERE name=?",
                       (encode_record("users", owner_id, user), name))

    def tearDown(self):
        self.directory.cleanup()

    def call(self, body, provider=None, headers=None):
        return asyncio.run(handle_ledger_request(binding=self.binding, gateway=GitHubStub(),
            suggestion_gateway=provider, method="POST", path="/api/v1/expense-suggestions",
            headers=headers or self.headers, params={}, body=body, now=self.now + 3))

    def test_disabled_and_bad_input_do_not_call_provider(self):
        class Provider:
            enabled = False
            calls = 0
            async def evaluate(self, request):
                self.calls += 1
                raise AssertionError("disabled provider called")
        provider = Provider()
        status, payload = self.call({"target": {"kind": "shared"}, "purpose": "Hosting"}, provider)
        self.assertEqual((status, payload["status"]), (200, "unavailable"))
        self.assertEqual(provider.calls, 0)
        status, payload = self.call({"target": {"kind": "shared"}, "purpose": "x" * 501}, provider)
        self.assertEqual((status, payload["error"]["code"]), (422, "INVALID_INPUT"))

    def test_provider_failure_falls_back_without_expense_write(self):
        status, _ = asyncio.run(handle_ledger_request(binding=self.binding, gateway=GitHubStub(),
            method="POST", path="/api/v1/categories", headers=self.headers, params={},
            body={"name": "Hosting"}, now=self.now + 3))
        self.assertEqual(status, 201)
        class Provider:
            enabled = True
            async def evaluate(self, request):
                raise TimeoutError("synthetic timeout")
        status, payload = self.call({"target": {"kind": "shared"}, "purpose": "Hosting"}, Provider())
        self.assertEqual((status, payload["status"]), (200, "unavailable"))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM expenses").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT attempts FROM expense_suggestion_budget").fetchone()[0], 1)

    def test_suggestion_is_confirmable_and_daily_budget_is_atomic(self):
        status, category = asyncio.run(handle_ledger_request(binding=self.binding, gateway=GitHubStub(),
            method="POST", path="/api/v1/categories", headers=self.headers, params={},
            body={"name": "Hosting"}, now=self.now + 3))
        self.assertEqual(status, 201)
        class Provider:
            enabled = True
            daily_limit = 1
            calls = 0
            async def evaluate(self, request):
                self.calls += 1
                category_id = next(key for key in request["questions"]["category"]["criteria"]
                    if key != "uncertain")
                return json.dumps({"model": "jev-1.13.0", "answers": {
                    "target": {"type": "choice", "choice": "shared", "confidence": 0.9,
                        "probabilities": {"project": 0.05, "shared": 0.9, "uncertain": 0.05}},
                    "category": {"type": "choice", "choice": category_id, "confidence": 0.9,
                        "probabilities": {category_id: 0.9, "uncertain": 0.1}}},
                    "usage": {"input_tokens": 12, "output_tokens": 4}}).encode()
        provider = Provider()
        draft = {"target": {"kind": "shared"}, "purpose": "Hosting"}
        status, payload = self.call(draft, provider)
        self.assertEqual((status, payload["status"]), (200, "available"))
        self.assertEqual(payload["suggestions"], {"categoryId": category["id"], "targetKind": "shared"})
        status, _ = asyncio.run(handle_ledger_request(binding=self.binding, gateway=GitHubStub(),
            method="POST", path=f"/api/v1/expense-suggestions/{payload['suggestionId']}/decision",
            headers=self.headers, params={}, body={"target": {"kind": "shared"},
                "categoryId": category["id"]}, now=self.now + 4))
        self.assertEqual(status, 204)
        with self.store.connect() as db:
            decision = db.execute("SELECT accepted_category_id,accepted_target_kind FROM expense_suggestion_events").fetchone()
            self.assertEqual(tuple(decision), (category["id"], "shared"))
        status, payload = self.call(draft, provider)
        self.assertEqual((status, payload["error"]["code"]), (429, "SUGGESTION_LIMIT"))
        self.assertEqual(provider.calls, 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM expenses").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
