from __future__ import annotations

import os
import hashlib
import hmac
import json
import time
import tempfile
import threading
import unittest
from http import HTTPStatus
from unittest.mock import Mock, patch

from pullwise_server import app


def creem_product(product_id: str, *, price: int, period: str) -> dict:
    return {
        "id": product_id,
        "name": "Pullwise Pro",
        "description": "Repository review for production teams.",
        "price": price,
        "currency": "USD",
        "billing_type": "recurring",
        "billing_period": period,
        "status": "active",
    }


def creem_product_get(*products: dict):
    by_id = {product["id"]: product for product in products}

    def side_effect(*_args, **kwargs):
        product_id = (kwargs.get("params") or {}).get("product_id")
        response = Mock()
        response.json.return_value = by_id[product_id]
        response.raise_for_status.return_value = None
        return response

    return side_effect


class HandlerHarness(app.PullwiseHandler):
    def __init__(
        self,
        body: dict | None = None,
        cookie: str = "",
        raw_body: bytes | None = None,
        headers: dict | None = None,
        path: str = "/",
    ) -> None:
        self.path = path
        self._body = body or {}
        self._raw_body = raw_body if raw_body is not None else json.dumps(self._body).encode("utf-8")
        self.headers = {"Host": "api.pullwise.dev", "Cookie": cookie, **(headers or {})}
        self.payload = None
        self.status = None

    def read_json(self) -> dict:
        return self._body

    def read_raw_body(self) -> bytes:
        return self._raw_body

    def json(self, payload: dict, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.status = status
        self.headers_out = headers or {}

    def error(self, status: int, message: str) -> None:
        self.json({"message": message}, status)


def seed_session() -> str:
    app.USERS = {
        "usr_1": {
            "id": "usr_1",
            "name": "Dev",
            "email": "dev@example.com",
            "createdAt": app.now(),
            "providers": ["email"],
        }
    }
    app.SESSIONS = {
        "ses_1": {
            "id": "ses_1",
            "userId": "usr_1",
            "createdAt": app.now(),
            "expiresAt": app.now() + 3600,
        }
    }
    app.SETTINGS = {}
    app.BILLING_EVENTS = {}
    app.BILLING_PENDING_UPDATES = []
    app.SCANS = []
    app.ISSUES = []
    app.STATE_LOADED = True
    app.STATE_DIRTY = False
    return "pw_session=ses_1"


def authorize_repo_for_seed_user() -> None:
    app.USERS["usr_1"].update({"githubLogin": "dev"})
    app.USERS["usr_1"]["githubRepositoryAccess"] = {
        "mode": "github-app",
        "authorizedUserId": "usr_1",
        "authorizedGithubLogin": "dev",
        "repositories": ["owner/repo"],
        "repositoriesNeedSync": False,
        "repositoryItems": [
            {
                "id": "owner/repo",
                "name": "repo",
                "fullName": "owner/repo",
                "defaultBranch": "main",
                "installationId": "123",
                "installationAccount": "dev",
                "repositorySelection": "selected",
                "cloneUrl": "https://github.com/owner/repo.git",
                "private": True,
            }
        ],
    }


class BillingRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.persist_patcher = patch.object(app, "persist_state")
        self.persist_patcher.start()
        self.addCleanup(self.persist_patcher.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = os.path.join(self.temp_dir.name, "pullwise.sqlite3")
        self.db_patcher = patch.dict(os.environ, {"PULLWISE_DB_PATH": self.db_path}, clear=False)
        self.db_patcher.start()
        self.addCleanup(self.db_patcher.stop)

    def test_billing_plan_exposes_selected_provider(self) -> None:
        handler = HandlerHarness()
        with patch.dict(
            os.environ,
            {
                "PULLWISE_CREEM_API_KEY": "creem_123",
                "PULLWISE_CREEM_PRO_PRODUCT_IDS": "prod_monthly,prod_yearly",
            },
            clear=True,
        ), patch(
            "pullwise_server.billing.requests.get",
            side_effect=creem_product_get(
                creem_product("prod_monthly", price=2900, period="every-month"),
                creem_product("prod_yearly", price=29000, period="every-year"),
            ),
        ):
            app.PullwiseHandler.handle_get(handler, "/billing/plan", {}, ["billing", "plan"])

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["provider"], "creem")
        self.assertTrue(handler.payload["enabled"])

    def test_checkout_requires_sign_in(self) -> None:
        app.USERS = {}
        app.SESSIONS = {}
        app.STATE_LOADED = True
        handler = HandlerHarness()

        app.PullwiseHandler.handle_post(handler, "/billing/checkout-sessions", {}, ["billing", "checkout-sessions"])

        self.assertEqual(handler.status, HTTPStatus.UNAUTHORIZED)

    def test_checkout_session_rejects_non_object_body(self) -> None:
        cookie = seed_session()
        handler = HandlerHarness(["invalid"], cookie=cookie)

        with patch("pullwise_server.billing.create_checkout_session") as create:
            app.PullwiseHandler.handle_post(handler, "/billing/checkout-sessions", {}, ["billing", "checkout-sessions"])

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("JSON object", handler.payload["message"])
        create.assert_not_called()

    def test_checkout_session_returns_provider_url_for_signed_in_user(self) -> None:
        cookie = seed_session()
        handler = HandlerHarness(
            {
                "successUrl": "https://app.pullwise.dev/?screen=settings&billing=success",
                "cancelUrl": "https://app.pullwise.dev/?screen=settings&billing=cancel",
            },
            cookie=cookie,
        )

        with patch("pullwise_server.billing.create_checkout_session", return_value={"provider": "creem", "id": "chk_1", "url": "https://creem.io/checkout/chk_1"}) as create:
            app.PullwiseHandler.handle_post(handler, "/billing/checkout-sessions", {}, ["billing", "checkout-sessions"])

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["url"], "https://creem.io/checkout/chk_1")
        create.assert_called_once()
        self.assertEqual(create.call_args.args[0]["id"], "usr_1")

    def test_checkout_session_passes_selected_billing_interval(self) -> None:
        cookie = seed_session()
        handler = HandlerHarness(
            {
                "interval": "year",
                "successUrl": "https://app.pullwise.dev/?screen=billing&billing=success",
                "cancelUrl": "https://app.pullwise.dev/?screen=billing&billing=cancel",
            },
            cookie=cookie,
        )

        with patch("pullwise_server.billing.create_checkout_session", return_value={"provider": "creem", "id": "chk_1", "url": "https://creem.io/checkout/chk_1"}) as create:
            app.PullwiseHandler.handle_post(handler, "/billing/checkout-sessions", {}, ["billing", "checkout-sessions"])

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(create.call_args.kwargs["interval"], "year")

    def test_checkout_session_rejects_active_pro_subscription(self) -> None:
        cookie = seed_session()
        app.USERS["usr_1"]["billing"] = {
            "provider": "creem",
            "customerId": "cust_1",
            "subscriptionId": "sub_1",
            "status": "active",
            "plan": "pro",
            "interval": "month",
        }
        handler = HandlerHarness(
            {
                "interval": "month",
                "successUrl": "https://app.pullwise.dev/?screen=billing&billing=success",
                "cancelUrl": "https://app.pullwise.dev/?screen=billing&billing=cancel",
            },
            cookie=cookie,
        )

        with patch("pullwise_server.billing.create_checkout_session") as create:
            app.PullwiseHandler.handle_post(handler, "/billing/checkout-sessions", {}, ["billing", "checkout-sessions"])

        self.assertEqual(handler.status, HTTPStatus.CONFLICT)
        self.assertIn("active Pro subscription", handler.payload["message"])
        create.assert_not_called()

    def test_admin_checkout_uses_creem_checkout_without_local_pro(self) -> None:
        cookie = seed_session()
        handler = HandlerHarness(
            {
                "interval": "year",
                "successUrl": "https://app.pullwise.dev/?screen=pricing&billing=success",
                "cancelUrl": "https://app.pullwise.dev/?screen=pricing&billing=cancel",
            },
            cookie=cookie,
        )

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_ADMIN_EMAILS": "dev@example.com",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                },
                clear=False,
            ),
            patch("pullwise_server.billing.create_checkout_session", return_value={"provider": "creem", "id": "chk_1", "url": "https://creem.io/checkout/chk_1"}) as create,
        ):
            app.PullwiseHandler.handle_post(handler, "/billing/checkout-sessions", {}, ["billing", "checkout-sessions"])

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["provider"], "creem")
        self.assertEqual(handler.payload["url"], "https://creem.io/checkout/chk_1")
        create.assert_called_once()
        self.assertNotIn("billing", app.USERS["usr_1"])
        self.assertEqual(app.billing_account_payload(app.USERS["usr_1"])["plan"], "free")

    def test_checkout_session_falls_back_for_non_string_redirect_urls(self) -> None:
        cookie = seed_session()
        handler = HandlerHarness(
            {
                "successUrl": {"url": "https://evil.example/success"},
                "cancelUrl": ["https://evil.example/cancel"],
            },
            cookie=cookie,
        )

        with (
            patch.dict(os.environ, {"PULLWISE_APP_URL": "https://app.pullwise.dev"}, clear=True),
            patch(
                "pullwise_server.billing.create_checkout_session",
                return_value={"provider": "creem", "id": "chk_1", "url": "https://creem.io/checkout/chk_1"},
            ) as create,
        ):
            app.PullwiseHandler.handle_post(handler, "/billing/checkout-sessions", {}, ["billing", "checkout-sessions"])

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(create.call_args.kwargs["success_url"], "https://app.pullwise.dev/settings")
        self.assertEqual(create.call_args.kwargs["cancel_url"], "https://app.pullwise.dev/settings")

    def test_portal_session_rejects_non_object_body(self) -> None:
        cookie = seed_session()
        handler = HandlerHarness(["invalid"], cookie=cookie)

        with patch("pullwise_server.billing.create_portal_session") as create:
            app.PullwiseHandler.handle_post(handler, "/billing/portal-sessions", {}, ["billing", "portal-sessions"])

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("JSON object", handler.payload["message"])
        create.assert_not_called()

    def test_change_interval_rejects_non_object_body(self) -> None:
        cookie = seed_session()
        handler = HandlerHarness(["invalid"], cookie=cookie)

        with patch("pullwise_server.billing.change_subscription_interval") as change:
            app.PullwiseHandler.handle_post(handler, "/billing/change-interval", {}, ["billing", "change-interval"])

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("JSON object", handler.payload["message"])
        change.assert_not_called()

    def test_change_interval_requires_signed_in_user_and_returns_provider_result(self) -> None:
        cookie = seed_session()
        app.USERS["usr_1"]["billing"] = {
            "provider": "creem",
            "customerId": "cust_1",
            "subscriptionId": "sub_1",
            "status": "active",
            "plan": "pro",
            "interval": "month",
        }
        handler = HandlerHarness(
            {"interval": "year", "returnUrl": "https://app.pullwise.dev/?screen=billing"},
            cookie=cookie,
        )

        with patch("pullwise_server.billing.change_subscription_interval", return_value={"provider": "creem", "interval": "year", "subscriptionId": "sub_1", "status": "active"}) as change:
            app.PullwiseHandler.handle_post(handler, "/billing/change-interval", {}, ["billing", "change-interval"])

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["provider"], "creem")
        self.assertEqual(change.call_args.kwargs["interval"], "year")

    def test_billing_redirect_routes_reject_unsafe_internal_urls(self) -> None:
        scenarios = [
            (
                "/billing/checkout-sessions",
                {},
                "pullwise_server.billing.create_checkout_session",
                {"provider": "creem", "id": "chk_1", "url": "javascript:alert(1)"},
                lambda: None,
            ),
            (
                "/billing/portal-sessions",
                {},
                "pullwise_server.billing.create_portal_session",
                {"provider": "creem", "url": "javascript:alert(1)"},
                lambda: app.USERS["usr_1"].update({"billing": {"customerId": "cust_1"}}),
            ),
            (
                "/billing/change-interval",
                {"interval": "year"},
                "pullwise_server.billing.change_subscription_interval",
                {"provider": "creem", "interval": "year", "url": "javascript:alert(1)"},
                lambda: app.USERS["usr_1"].update({
                    "billing": {
                        "provider": "creem",
                        "customerId": "cust_1",
                        "subscriptionId": "sub_1",
                        "status": "active",
                        "plan": "pro",
                        "interval": "month",
                    }
                }),
            ),
        ]

        for path, body, patch_target, provider_result, setup in scenarios:
            with self.subTest(path=path):
                cookie = seed_session()
                setup()
                handler = HandlerHarness(body, cookie=cookie, path=path)

                with patch(patch_target, return_value=provider_result):
                    app.PullwiseHandler.route(handler, "POST")

                self.assertEqual(handler.status, HTTPStatus.BAD_GATEWAY)
                self.assertNotIn("url", handler.payload)

    def test_free_plan_blocks_scans_after_monthly_review_limit(self) -> None:
        cookie = seed_session()
        authorize_repo_for_seed_user()
        first = HandlerHarness({"repo": "owner/repo", "requestId": "scan_req_1"}, cookie=cookie)
        app.USERS["usr_1"]["githubRepositoryAccess"]["repositories"].append("owner/other")
        app.USERS["usr_1"]["githubRepositoryAccess"]["repositoryItems"].append(
            {
                "id": "owner/other",
                "githubRepoId": "456",
                "name": "other",
                "fullName": "owner/other",
                "defaultBranch": "main",
                "installationId": "123",
                "installationAccount": "dev",
                "repositorySelection": "selected",
                "cloneUrl": "https://github.com/owner/other.git",
                "private": True,
            }
        )
        second = HandlerHarness({"repo": "owner/other", "requestId": "scan_req_2"}, cookie=cookie)

        with (
            patch.dict(os.environ, {"PULLWISE_DB_PATH": self.db_path, "PULLWISE_FREE_USER_REVIEW_LIMIT": "1"}, clear=True),
        ):
            app.PullwiseHandler.handle_post(first, "/scans", {}, ["scans"])
            app.PullwiseHandler.handle_post(second, "/scans", {}, ["scans"])
            billing_payload = app.billing_account_payload(app.USERS["usr_1"])

        self.assertEqual(first.status, HTTPStatus.CREATED)
        self.assertEqual(second.status, HTTPStatus.PAYMENT_REQUIRED)
        self.assertEqual(second.payload["code"], "QUOTA_EXCEEDED_USER")
        self.assertEqual(first.payload["billingUsage"]["used"], 1)
        self.assertEqual(billing_payload["usage"]["used"], 1)
        self.assertEqual(billing_payload["usage"]["limit"], 1)
        self.assertEqual(billing_payload["usage"]["remaining"], 0)
        self.assertEqual(billing_payload["usage"]["resetAt"], first.payload["billingUsage"]["resetAt"])
        self.assertGreater(billing_payload["usage"]["resetAt"], app.now())

    def test_legacy_consume_review_quota_uses_db_backed_user_quota(self) -> None:
        seed_session()

        with patch.dict(os.environ, {"PULLWISE_DB_PATH": self.db_path, "PULLWISE_FREE_USER_REVIEW_LIMIT": "1"}, clear=True):
            first_ok, first_payload = app.consume_review_quota(app.USERS["usr_1"])
            second_ok, second_payload = app.consume_review_quota(app.USERS["usr_1"])
            account_payload = app.billing_account_payload(app.USERS["usr_1"])

        self.assertTrue(first_ok)
        self.assertFalse(second_ok)
        self.assertEqual(first_payload["used"], 1)
        self.assertEqual(second_payload["used"], 1)
        self.assertEqual(second_payload["remaining"], 0)
        self.assertEqual(account_payload["usage"]["used"], 1)
        self.assertNotIn("billingUsage", app.USERS["usr_1"])

    def test_billing_account_payload_ignores_non_finite_usage(self) -> None:
        seed_session()
        app.USERS["usr_1"]["billingUsage"] = {
            "period": app.current_review_usage_period(),
            "plan": "free",
            "used": float("inf"),
        }

        payload = app.billing_account_payload(app.USERS["usr_1"])

        self.assertEqual(payload["usage"]["used"], 0)
        self.assertEqual(payload["usage"]["remaining"], payload["usage"]["limit"])

    def test_billing_account_payload_sanitizes_malformed_public_state(self) -> None:
        seed_session()
        app.USERS["usr_1"]["billing"] = {
            "provider": "creem\r\nX-Injected: bad",
            "status": "active\r\nX-Injected: bad",
            "plan": "pro",
            "interval": {"value": "year"},
            "customerId": {"id": "cus_1"},
            "subscriptionId": "sub_1\r\nX-Injected: bad",
            "subscriptionItemId": "si_1",
            "customerEmail": "dev@example.com\r\nX-Injected: bad",
            "currentPeriodStart": {"value": 1710000000},
            "currentPeriodEnd": "1712592000",
            "cancelAtPeriodEnd": "false",
            "canceledAt": float("nan"),
            "lastEventId": ["evt_1"],
            "lastEventType": "checkout.completed\r\nX-Injected: bad",
            "lastEventCreated": "1710000123",
            "updatedAt": True,
            "raw": {"unsafe": True},
        }

        payload = app.billing_account_payload(app.USERS["usr_1"])

        self.assertEqual(payload["status"], "none")
        self.assertEqual(payload["plan"], "free")
        self.assertEqual(payload["interval"], "month")
        self.assertIsNone(payload["provider"])
        self.assertIsNone(payload["customerId"])
        self.assertIsNone(payload["subscriptionId"])
        self.assertEqual(payload["subscriptionItemId"], "si_1")
        self.assertIsNone(payload["customerEmail"])
        self.assertIsNone(payload["currentPeriodStart"])
        self.assertEqual(payload["currentPeriodEnd"], 1712592000)
        self.assertIsNone(payload["cancelAtPeriodEnd"])
        self.assertIsNone(payload["canceledAt"])
        self.assertIsNone(payload["lastEventId"])
        self.assertIsNone(payload["lastEventType"])
        self.assertEqual(payload["lastEventCreated"], 1710000123)
        self.assertIsNone(payload["updatedAt"])
        self.assertNotIn("raw", payload)

    def test_billing_account_payload_includes_current_subscription_record_for_legacy_state(self) -> None:
        seed_session()
        app.USERS["usr_1"]["billing"] = {
            "provider": "creem",
            "customerId": "cust_1",
            "subscriptionId": "sub_1",
            "status": "active",
            "plan": "pro",
            "interval": "month",
            "lastEventType": "checkout.completed",
            "lastEventId": "evt_1",
            "lastEventCreated": 1728734325,
            "updatedAt": 1728734330,
        }

        payload = app.billing_account_payload(app.USERS["usr_1"])

        self.assertEqual(payload["subscriptions"], [
            {
                "provider": "creem",
                "customerId": "cust_1",
                "customerEmail": None,
                "subscriptionId": "sub_1",
                "subscriptionItemId": None,
                "status": "active",
                "plan": "pro",
                "interval": "month",
                "currentPeriodStart": None,
                "currentPeriodEnd": None,
                "cancelAtPeriodEnd": None,
                "canceledAt": None,
                "lastEventType": "checkout.completed",
                "lastEventId": "evt_1",
                "lastEventCreated": 1728734325,
                "updatedAt": 1728734330,
            }
        ])

    def test_checkout_returns_not_implemented_when_billing_is_disabled(self) -> None:
        cookie = seed_session()
        handler = HandlerHarness(path="/billing/checkout-sessions", cookie=cookie)

        with patch.dict(os.environ, {}, clear=True):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.NOT_IMPLEMENTED)
        self.assertIn("Billing is not configured", handler.payload["message"])

    def test_creem_webhook_updates_user_billing_after_signature_verification(self) -> None:
        seed_session()
        raw = json.dumps(
            {
                "eventType": "checkout.completed",
                "object": {
                    "customer": {"id": "cust_1", "email": "dev@example.com"},
                    "product": {"id": "prod_monthly", "billing_period": "every-month"},
                    "subscription": {"id": "sub_1", "status": "active"},
                    "metadata": {"userId": "usr_1"},
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(b"whsec_test", raw, hashlib.sha256).hexdigest()
        handler = HandlerHarness(raw_body=raw, headers={"creem-signature": signature})

        with patch.dict(
            os.environ,
            {
                "PULLWISE_CREEM_WEBHOOK_SECRET": "whsec_test",
                "PULLWISE_CREEM_PRO_MONTHLY_PRODUCT_ID": "prod_monthly",
            },
            clear=True,
        ):
            app.PullwiseHandler.handle_post(handler, "/webhooks/creem", {}, ["webhooks", "creem"])

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(app.USERS["usr_1"]["billing"]["provider"], "creem")
        self.assertEqual(app.USERS["usr_1"]["billing"]["status"], "active")
        self.assertEqual(app.USERS["usr_1"]["billing"]["customerId"], "cust_1")

    def test_creem_webhook_rejects_malformed_json_without_parser_details(self) -> None:
        raw = b"{"
        signature = hmac.new(b"whsec_test", raw, hashlib.sha256).hexdigest()
        handler = HandlerHarness(path="/webhooks/creem", raw_body=raw, headers={"Content-Length": str(len(raw)), "creem-signature": signature})

        with patch.dict(os.environ, {"PULLWISE_CREEM_WEBHOOK_SECRET": "whsec_test"}, clear=True):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload["message"], "Request body must be valid JSON.")

    def test_creem_webhook_rejects_non_utf8_json_without_decoder_details(self) -> None:
        raw = b"\xff"
        signature = hmac.new(b"whsec_test", raw, hashlib.sha256).hexdigest()
        handler = HandlerHarness(path="/webhooks/creem", raw_body=raw, headers={"Content-Length": str(len(raw)), "creem-signature": signature})

        with patch.dict(os.environ, {"PULLWISE_CREEM_WEBHOOK_SECRET": "whsec_test"}, clear=True):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload["message"], "Request body must be valid JSON.")

    def test_creem_webhook_rejects_non_object_json_body(self) -> None:
        raw = b"[]"
        signature = hmac.new(b"whsec_test", raw, hashlib.sha256).hexdigest()
        handler = HandlerHarness(path="/webhooks/creem", raw_body=raw, headers={"Content-Length": str(len(raw)), "creem-signature": signature})

        with patch.dict(os.environ, {"PULLWISE_CREEM_WEBHOOK_SECRET": "whsec_test"}, clear=True):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload["message"], "Request body must be a JSON object.")

    def test_billing_updates_are_idempotent_by_event_id(self) -> None:
        seed_session()
        handler = HandlerHarness()

        app.PullwiseHandler.apply_billing_update(
            handler,
            {
                "userId": "usr_1",
                "provider": "creem",
                "customerId": "cus_1",
                "subscriptionId": "sub_1",
                "status": "active",
                "eventType": "subscription.update",
                "eventId": "evt_1",
                "eventCreated": 200,
            },
        )
        app.PullwiseHandler.apply_billing_update(
            handler,
            {
                "userId": "usr_1",
                "provider": "creem",
                "customerId": "cus_1",
                "subscriptionId": "sub_1",
                "status": "canceled",
                "eventType": "subscription.canceled",
                "eventId": "evt_1",
                "eventCreated": 300,
            },
        )

        self.assertEqual(app.USERS["usr_1"]["billing"]["status"], "active")
        self.assertIn("evt_1", app.BILLING_EVENTS)

    def test_billing_updates_ignore_events_older_than_current_billing_state(self) -> None:
        seed_session()
        handler = HandlerHarness()

        app.PullwiseHandler.apply_billing_update(
            handler,
            {
                "userId": "usr_1",
                "provider": "creem",
                "customerId": "cus_1",
                "subscriptionId": "sub_1",
                "status": "active",
                "eventType": "subscription.update",
                "eventId": "evt_new",
                "eventCreated": 200,
            },
        )
        app.PullwiseHandler.apply_billing_update(
            handler,
            {
                "userId": "usr_1",
                "provider": "creem",
                "customerId": "cus_1",
                "subscriptionId": "sub_1",
                "status": "canceled",
                "eventType": "subscription.canceled",
                "eventId": "evt_old",
                "eventCreated": 100,
            },
        )

        self.assertEqual(app.USERS["usr_1"]["billing"]["status"], "active")
        self.assertIn("evt_old", app.BILLING_EVENTS)

    def test_billing_update_with_malformed_user_id_can_match_existing_customer(self) -> None:
        seed_session()
        app.USERS["usr_1"]["billing"] = {
            "provider": "creem",
            "customerId": "cus_1",
            "status": "active",
            "plan": "pro",
            "interval": "month",
        }
        handler = HandlerHarness()

        app.PullwiseHandler.apply_billing_update(
            handler,
            {
                "userId": ["usr_1"],
                "provider": "creem",
                "customerId": "cus_1",
                "subscriptionId": "sub_1",
                "status": "past_due",
                "eventType": "subscription.update",
                "eventId": "evt_malformed_user",
                "eventCreated": 400,
            },
        )

        self.assertEqual(app.USERS["usr_1"]["billing"]["status"], "past_due")
        self.assertEqual(app.USERS["usr_1"]["billing"]["subscriptionId"], "sub_1")

    def test_billing_update_ignores_malformed_identifier_fields_when_applying(self) -> None:
        seed_session()
        app.USERS["usr_1"]["billing"] = {
            "provider": "creem",
            "customerId": "cus_existing",
            "subscriptionId": "sub_existing",
            "subscriptionItemId": "si_existing",
            "status": "active",
            "plan": "pro",
            "interval": "month",
        }
        handler = HandlerHarness()

        app.PullwiseHandler.apply_billing_update(
            handler,
            {
                "userId": "usr_1",
                "provider": "creem",
                "customerId": ["cus_bad"],
                "subscriptionId": {"id": "sub_bad"},
                "subscriptionItemId": ["si_bad"],
                "status": "past_due",
                "eventType": "subscription.update",
                "eventId": "evt_bad_ids",
                "eventCreated": 500,
            },
        )

        self.assertEqual(app.USERS["usr_1"]["billing"]["customerId"], "cus_existing")
        self.assertEqual(app.USERS["usr_1"]["billing"]["subscriptionId"], "sub_existing")
        self.assertEqual(app.USERS["usr_1"]["billing"]["subscriptionItemId"], "si_existing")
        self.assertEqual(app.USERS["usr_1"]["billing"]["status"], "past_due")

    def test_billing_update_ignores_malformed_event_id_when_recording_state(self) -> None:
        seed_session()
        app.USERS["usr_1"]["billing"] = {
            "provider": "creem",
            "customerId": "cus_1",
            "status": "active",
            "plan": "pro",
            "interval": "month",
            "lastEventId": "evt_existing",
        }
        handler = HandlerHarness()

        app.PullwiseHandler.apply_billing_update(
            handler,
            {
                "userId": "usr_1",
                "provider": "creem",
                "customerId": "cus_1",
                "status": "past_due",
                "eventType": "subscription.update",
                "eventId": {"id": "evt_bad"},
                "eventCreated": 700,
            },
        )

        self.assertEqual(app.USERS["usr_1"]["billing"]["status"], "past_due")
        self.assertEqual(app.USERS["usr_1"]["billing"]["lastEventId"], "evt_existing")
        self.assertEqual(app.BILLING_EVENTS, {})

    def test_billing_update_ignores_malformed_customer_email_when_applying(self) -> None:
        seed_session()
        app.USERS["usr_1"]["billing"] = {
            "provider": "creem",
            "customerId": "cus_1",
            "customerEmail": "dev@example.com",
            "status": "active",
            "plan": "pro",
            "interval": "month",
        }
        handler = HandlerHarness()

        app.PullwiseHandler.apply_billing_update(
            handler,
            {
                "userId": "usr_1",
                "provider": "creem",
                "customerId": "cus_1",
                "customerEmail": ["bad@example.com"],
                "status": "past_due",
                "eventType": "subscription.update",
                "eventId": "evt_bad_email",
                "eventCreated": 800,
            },
        )

        self.assertEqual(app.USERS["usr_1"]["billing"]["customerEmail"], "dev@example.com")
        self.assertEqual(app.USERS["usr_1"]["billing"]["status"], "past_due")

    def test_billing_update_ignores_malformed_status_plan_and_interval_when_applying(self) -> None:
        seed_session()
        app.USERS["usr_1"]["billing"] = {
            "provider": "creem",
            "customerId": "cus_1",
            "status": "active",
            "plan": "pro",
            "interval": "month",
        }
        handler = HandlerHarness()

        app.PullwiseHandler.apply_billing_update(
            handler,
            {
                "userId": "usr_1",
                "provider": "creem",
                "customerId": "cus_1",
                "status": {"state": "past_due"},
                "plan": ["free"],
                "interval": {"period": "year"},
                "eventType": "subscription.update",
                "eventId": "evt_bad_billing_values",
                "eventCreated": 900,
            },
        )

        billing_state = app.USERS["usr_1"]["billing"]
        self.assertEqual(billing_state["status"], "active")
        self.assertEqual(billing_state["plan"], "pro")
        self.assertEqual(billing_state["interval"], "month")

    def test_billing_update_ignores_malformed_period_fields_when_applying(self) -> None:
        seed_session()
        app.USERS["usr_1"]["billing"] = {
            "provider": "creem",
            "customerId": "cus_1",
            "status": "active",
            "plan": "pro",
            "interval": "month",
            "currentPeriodStart": 1710000000,
            "currentPeriodEnd": 1712592000,
            "cancelAtPeriodEnd": False,
            "canceledAt": 1713000000,
        }
        handler = HandlerHarness()

        app.PullwiseHandler.apply_billing_update(
            handler,
            {
                "userId": "usr_1",
                "provider": "creem",
                "customerId": "cus_1",
                "status": "past_due",
                "currentPeriodStart": {"seconds": 1710000001},
                "currentPeriodEnd": ["1712592001"],
                "cancelAtPeriodEnd": "false",
                "canceledAt": {"seconds": 1713000001},
                "eventType": "subscription.update",
                "eventId": "evt_bad_periods",
                "eventCreated": 1000,
            },
        )

        billing_state = app.USERS["usr_1"]["billing"]
        self.assertEqual(billing_state["status"], "past_due")
        self.assertEqual(billing_state["currentPeriodStart"], 1710000000)
        self.assertEqual(billing_state["currentPeriodEnd"], 1712592000)
        self.assertFalse(billing_state["cancelAtPeriodEnd"])
        self.assertEqual(billing_state["canceledAt"], 1713000000)

    def test_billing_update_ignores_non_finite_period_fields_when_applying(self) -> None:
        seed_session()
        app.USERS["usr_1"]["billing"] = {
            "provider": "creem",
            "customerId": "cus_1",
            "status": "active",
            "plan": "pro",
            "interval": "month",
            "currentPeriodStart": 1710000000,
            "currentPeriodEnd": 1712592000,
            "canceledAt": 1713000000,
        }
        handler = HandlerHarness()

        app.PullwiseHandler.apply_billing_update(
            handler,
            {
                "userId": "usr_1",
                "provider": "creem",
                "customerId": "cus_1",
                "status": "past_due",
                "currentPeriodStart": float("nan"),
                "currentPeriodEnd": float("inf"),
                "canceledAt": float("-inf"),
                "eventType": "subscription.update",
                "eventId": "evt_bad_period_numbers",
                "eventCreated": 1200,
            },
        )

        billing_state = app.USERS["usr_1"]["billing"]
        self.assertEqual(billing_state["status"], "past_due")
        self.assertEqual(billing_state["currentPeriodStart"], 1710000000)
        self.assertEqual(billing_state["currentPeriodEnd"], 1712592000)
        self.assertEqual(billing_state["canceledAt"], 1713000000)

    def test_concurrent_billing_updates_do_not_let_stale_event_overwrite_newer_state(self) -> None:
        seed_session()
        handler = HandlerHarness()
        older_reached_write_path = threading.Event()
        release_older = threading.Event()
        original_billing_update_text = app.billing_update_text

        def pausing_billing_update_text(value):
            if (
                threading.current_thread().name == "older-billing-update"
                and value == "cus_1"
                and not older_reached_write_path.is_set()
            ):
                older_reached_write_path.set()
                self.assertTrue(release_older.wait(2), "timed out releasing older billing update")
            return original_billing_update_text(value)

        older_update = {
            "userId": "usr_1",
            "provider": "creem",
            "customerId": "cus_1",
            "status": "canceled",
            "plan": "pro",
            "eventType": "subscription.canceled",
            "eventId": "evt_older",
            "eventCreated": 100,
        }
        newer_update = {
            "userId": "usr_1",
            "provider": "creem",
            "customerId": "cus_1",
            "status": "active",
            "plan": "pro",
            "eventType": "subscription.update",
            "eventId": "evt_newer",
            "eventCreated": 200,
        }

        with patch.object(app, "billing_update_text", side_effect=pausing_billing_update_text):
            older_thread = threading.Thread(
                target=app.PullwiseHandler.apply_billing_update,
                args=(handler, older_update),
                name="older-billing-update",
            )
            newer_thread = threading.Thread(
                target=app.PullwiseHandler.apply_billing_update,
                args=(handler, newer_update),
                name="newer-billing-update",
            )
            older_thread.start()
            self.assertTrue(older_reached_write_path.wait(2), "older update did not reach the write path")
            newer_thread.start()
            time.sleep(0.05)
            release_older.set()
            older_thread.join(2)
            newer_thread.join(2)

        self.assertFalse(older_thread.is_alive())
        self.assertFalse(newer_thread.is_alive())
        self.assertEqual(app.USERS["usr_1"]["billing"]["status"], "active")
        self.assertEqual(app.USERS["usr_1"]["billing"]["lastEventCreated"], 200)

    def test_billing_update_ignores_malformed_provider_and_event_type_when_applying(self) -> None:
        seed_session()
        app.USERS["usr_1"]["billing"] = {
            "provider": "creem",
            "customerId": "cus_1",
            "status": "active",
            "plan": "pro",
            "interval": "month",
            "lastEventType": "checkout.completed",
        }
        handler = HandlerHarness()

        app.PullwiseHandler.apply_billing_update(
            handler,
            {
                "userId": "usr_1",
                "provider": ["creem"],
                "customerId": "cus_1",
                "status": "past_due",
                "eventType": {"type": "subscription.update"},
                "eventId": "evt_bad_text_fields",
                "eventCreated": 1100,
            },
        )

        billing_state = app.USERS["usr_1"]["billing"]
        self.assertEqual(billing_state["status"], "past_due")
        self.assertEqual(billing_state["provider"], "creem")
        self.assertEqual(billing_state["lastEventType"], "checkout.completed")
        self.assertIsNone(app.BILLING_EVENTS["evt_bad_text_fields"]["eventType"])

    def test_billing_update_ignores_non_finite_event_created_when_applying(self) -> None:
        seed_session()
        handler = HandlerHarness()

        app.PullwiseHandler.apply_billing_update(
            handler,
            {
                "userId": "usr_1",
                "provider": "creem",
                "customerId": "cus_1",
                "status": "active",
                "eventType": "subscription.update",
                "eventId": "evt_bad_created",
                "eventCreated": float("nan"),
            },
        )

        self.assertEqual(app.USERS["usr_1"]["billing"]["status"], "active")
        self.assertIsNone(app.USERS["usr_1"]["billing"].get("lastEventCreated"))
        self.assertIsNone(app.BILLING_EVENTS["evt_bad_created"]["eventCreated"])

    def test_creem_subscription_update_waits_for_checkout_customer_mapping(self) -> None:
        seed_session()
        handler = HandlerHarness()

        app.PullwiseHandler.apply_billing_update(
            handler,
            {
                "provider": "creem",
                "customerId": "cus_1",
                "subscriptionId": "sub_1",
                "status": "past_due",
                "eventType": "subscription.update",
                "eventId": "evt_subscription",
                "eventCreated": 300,
            },
        )

        self.assertNotIn("billing", app.USERS["usr_1"])
        self.assertEqual(len(app.BILLING_PENDING_UPDATES), 1)

        app.PullwiseHandler.apply_billing_update(
            handler,
            {
                "userId": "usr_1",
                "provider": "creem",
                "customerId": "cus_1",
                "customerEmail": "dev@example.com",
                "subscriptionId": "sub_1",
                "status": "active",
                "eventType": "checkout.completed",
                "eventId": "evt_checkout",
                "eventCreated": 200,
            },
        )

        self.assertEqual(app.BILLING_PENDING_UPDATES, [])
        self.assertEqual(app.USERS["usr_1"]["billing"]["customerId"], "cus_1")
        self.assertEqual(app.USERS["usr_1"]["billing"]["status"], "past_due")

    def test_billing_update_with_malformed_pending_identifiers_is_not_queued(self) -> None:
        seed_session()
        handler = HandlerHarness()

        app.PullwiseHandler.apply_billing_update(
            handler,
            {
                "provider": "creem",
                "customerId": ["cus_bad"],
                "subscriptionId": {"id": "sub_bad"},
                "status": "past_due",
                "eventType": "subscription.update",
                "eventId": "evt_bad_pending_ids",
                "eventCreated": 600,
            },
        )

        self.assertNotIn("billing", app.USERS["usr_1"])
        self.assertEqual(app.BILLING_PENDING_UPDATES, [])


class BillingWebhookPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = os.path.join(self.temp_dir.name, "pullwise.sqlite3")
        self.db_patcher = patch.dict(
            os.environ,
            {
                "PULLWISE_DB_PATH": self.db_path,
                "PULLWISE_CREEM_WEBHOOK_SECRET": "whsec_test",
                "PULLWISE_CREEM_PRO_MONTHLY_PRODUCT_ID": "prod_monthly",
                "PULLWISE_PRO_USER_REVIEW_LIMIT": "60",
                "PULLWISE_FREE_USER_REVIEW_LIMIT": "5",
            },
            clear=False,
        )
        self.db_patcher.start()
        self.addCleanup(self.db_patcher.stop)

    def test_creem_webhook_persists_billing_and_refreshes_quota_bucket(self) -> None:
        seed_session()
        app.USERS["usr_1"]["billingCheckout"] = {
            "provider": "creem",
            "id": "ch_1",
            "requestId": "pw_usr_1_req_1",
            "plan": "pro",
            "interval": "month",
            "createdAt": app.now(),
        }
        raw = json.dumps(
            {
                "id": "evt_creem_checkout_real_1",
                "eventType": "checkout.completed",
                "created_at": 1728734325927,
                "object": {
                    "id": "ch_1",
                    "request_id": "pw_usr_1_req_1",
                    "customer": {"id": "cust_1", "email": "dev@example.com"},
                    "product": {"id": "prod_monthly", "billing_period": "every-month"},
                    "subscription": {
                        "id": "sub_1",
                        "customer": "cust_1",
                        "product": "prod_monthly",
                        "status": "active",
                    },
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(b"whsec_test", raw, hashlib.sha256).hexdigest()
        handler = HandlerHarness(
            path="/webhooks/creem",
            raw_body=raw,
            headers={"Content-Length": str(len(raw)), "creem-signature": signature},
        )

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.OK)
        persisted_state = app.db.load_state()
        persisted_user = persisted_state["users"]["usr_1"]
        self.assertEqual(persisted_user["billing"]["provider"], "creem")
        self.assertEqual(persisted_user["billing"]["status"], "active")
        self.assertEqual(persisted_user["billing"]["plan"], "pro")
        self.assertEqual(persisted_user["billing"]["customerId"], "cust_1")
        self.assertEqual(persisted_user["billing"]["subscriptionId"], "sub_1")
        self.assertEqual(persisted_user["billingCheckout"]["status"], "completed")
        self.assertEqual(
            persisted_user["billingSubscriptions"],
            [
                {
                    "provider": "creem",
                    "customerId": "cust_1",
                    "customerEmail": "dev@example.com",
                    "subscriptionId": "sub_1",
                    "subscriptionItemId": None,
                    "status": "active",
                    "plan": "pro",
                    "interval": "month",
                    "currentPeriodStart": None,
                    "currentPeriodEnd": None,
                    "cancelAtPeriodEnd": None,
                    "canceledAt": None,
                    "lastEventType": "checkout.completed",
                    "lastEventId": "evt_creem_checkout_real_1",
                    "lastEventCreated": 1728734325,
                    "updatedAt": persisted_user["billingSubscriptions"][0]["updatedAt"],
                }
            ],
        )
        self.assertIn("evt_creem_checkout_real_1", persisted_state["billingEvents"])

        app.USERS = {}
        app.SESSIONS = {}
        app.BILLING_EVENTS = {}
        app.BILLING_PENDING_UPDATES = []
        app.STATE_LOADED = False
        app.ensure_state_loaded()
        billing_payload = app.billing_account_payload(app.USERS["usr_1"])

        self.assertEqual(billing_payload["plan"], "pro")
        self.assertEqual(billing_payload["status"], "active")
        self.assertEqual(billing_payload["usage"]["limit"], 60)
        self.assertEqual(billing_payload["usage"]["remaining"], 60)
        self.assertEqual(billing_payload["usage"]["plan"], "pro")
        self.assertEqual(len(billing_payload["subscriptions"]), 1)
        self.assertEqual(billing_payload["subscriptions"][0]["subscriptionId"], "sub_1")
        self.assertEqual(billing_payload["subscriptions"][0]["status"], "active")
        self.assertEqual(billing_payload["subscriptions"][0]["plan"], "pro")
        self.assertEqual(billing_payload["subscriptions"][0]["interval"], "month")
        self.assertEqual(billing_payload["subscriptions"][0]["lastEventId"], "evt_creem_checkout_real_1")
        connection = app.db.connect()
        try:
            rows = connection.execute(
                "SELECT scope_type, scope_id, plan, quota_limit, used FROM quota_buckets WHERE scope_type = 'user'"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(rows, [("user", "usr_1", "pro", 60, 0)])


if __name__ == "__main__":
    unittest.main()
