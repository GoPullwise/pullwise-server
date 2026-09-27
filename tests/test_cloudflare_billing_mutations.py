"""S04 synthetic purchase and subscription operations, without Creem traffic."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from test_cloudflare_github_identity_http import Store, D1ShapedSQLite
from pullwise_server.cloudflare_billing_mutations import handle_billing_mutation
from pullwise_server.cloudflare_billing_catalog_refresh import read_or_refresh_catalog


PRODUCTS = {"pro": {"month": "prod-pro-month", "year": "prod-pro-year"},
            "max": {"month": "prod-max-month", "year": "prod-max-year"}}


class CreemStub:
    def __init__(self):
        self.calls = []

    async def post(self, path, payload):
        self.calls.append((path, payload))
        if path == "v1/checkouts":
            return {"id": "ch_1", "checkout_url": "https://checkout.creem.io/ch_1"}
        return {"id": "sub_1", "status": "active" if path.endswith("resume") or path.endswith("upgrade") else "scheduled_cancel",
                "cancel_at_period_end": path.endswith("cancel")}

    async def product(self, product_id):
        self.calls.append(("product", product_id))
        return {"id": product_id, "status": "active", "billing_type": "recurring",
                "billing_period": "every-year" if product_id.endswith("year") else "every-month",
                "price": 2900, "currency": "USD", "name": "Pullwise subscription"}


class BillingMutationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "billing.db")
        self.binding = D1ShapedSQLite(self.store)
        self.gateway = CreemStub()
        self.now = 1_800_000_000
        self.user = {"id": "owner", "name": "Alice", "providers": ["github"],
                     "githubAccessToken": "sealed", "billing": {"plan": "free"}}
        self._save_user()
        with self.store.connect() as db:
            db.execute("INSERT INTO app_state VALUES('sessions', ?, ?)",
                       (json.dumps({"ses-1": {"id": "ses-1", "userId": "owner", "expiresAt": self.now + 1000}}), self.now))

    def _save_user(self):
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO app_state VALUES('users', ?, ?)",
                       (json.dumps({"owner": self.user}), self.now))

    def call(self, path, body, headers=None):
        return asyncio.run(handle_billing_mutation(
            binding=self.binding, gateway=self.gateway, now=self.now,
            method="POST", path=path, headers=headers or {"Cookie": "pw_session=ses-1", "Origin": "https://app.example.test"},
            body=body, app_url="https://app.example.test", trusted_origins={"https://app.example.test"},
            products=PRODUCTS))

    def test_checkout_is_session_only_and_does_not_grant_subscription(self):
        status, payload = self.call("/billing/checkout-sessions", {"plan": "pro", "interval": "month"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["url"], "https://checkout.creem.io/ch_1")
        self.assertEqual(self.gateway.calls[0][1]["metadata"]["userId"], "owner")
        with self.store.connect() as db:
            saved = json.loads(db.execute("SELECT payload FROM app_state WHERE name='users'").fetchone()[0])["owner"]
        self.assertEqual(saved["billing"]["plan"], "free")
        self.assertEqual(saved["billingCheckout"]["id"], "ch_1")
        status, _ = self.call("/billing/checkout-sessions", {"plan": "pro"}, {"Authorization": "Bearer pwk_bad"})
        self.assertEqual(status, 401)
        self.assertEqual(len(self.gateway.calls), 1)

    def test_cancel_resume_and_upgrade_keep_subscription_id(self):
        self.user["billing"] = {"provider": "creem", "plan": "pro", "interval": "month",
                                "status": "active", "subscriptionId": "sub_1"}
        self._save_user()
        status, payload = self.call("/billing/cancel-subscription", {"mode": "scheduled"})
        self.assertEqual((status, payload["status"]), (200, "canceling"))
        status, payload = self.call("/billing/resume-subscription", {})
        self.assertEqual((status, payload["status"]), (200, "active"))
        status, payload = self.call("/billing/change-interval", {"plan": "max", "interval": "month"})
        self.assertEqual((status, payload["plan"]), (200, "max"))
        self.assertEqual([call[0] for call in self.gateway.calls],
                         ["v1/subscriptions/sub_1/cancel", "v1/subscriptions/sub_1/resume", "v1/subscriptions/sub_1/upgrade"])

    def test_origin_and_invalid_plan_do_not_call_provider(self):
        status, _ = self.call("/billing/checkout-sessions", {"plan": "pro"},
                              {"Cookie": "pw_session=ses-1", "Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.call("/billing/checkout-sessions", {"plan": "unknown"})
        self.assertEqual(status, 422)
        status, _ = self.call("/billing/checkout-sessions", {"successUrl": 42})
        self.assertEqual(status, 422)
        self.assertEqual(self.gateway.calls, [])

    def test_catalog_refresh_uses_configured_products_and_reuses_fresh_snapshot(self):
        with self.store.connect() as db:
            db.execute("""CREATE TABLE billing_public_catalog(id INTEGER PRIMARY KEY,
                payload_json TEXT NOT NULL,expires_at INTEGER NOT NULL,
                source_revision INTEGER NOT NULL,updated_at INTEGER NOT NULL)""")
        first = asyncio.run(read_or_refresh_catalog(binding=self.binding, gateway=self.gateway,
            headers={}, products=PRODUCTS, now=self.now))
        second = asyncio.run(read_or_refresh_catalog(binding=self.binding, gateway=self.gateway,
            headers={}, products=PRODUCTS, now=self.now + 1))
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200)
        self.assertEqual(len([call for call in self.gateway.calls if call[0] == "product"]), 4)
