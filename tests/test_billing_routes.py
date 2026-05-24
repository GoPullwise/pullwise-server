from __future__ import annotations

import os
import hashlib
import hmac
import json
import time
import unittest
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app


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

    def test_billing_plan_exposes_selected_provider(self) -> None:
        handler = HandlerHarness()
        with patch.dict(
            os.environ,
            {
                "PULLWISE_STRIPE_SECRET_KEY": "sk_test_123",
                "PULLWISE_STRIPE_PRICE_ID": "price_123",
            },
            clear=True,
        ):
            app.PullwiseHandler.handle_get(handler, "/billing/plan", {}, ["billing", "plan"])

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["provider"], "stripe")
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

        with patch("pullwise_server.billing.create_checkout_session", return_value={"provider": "stripe", "id": "cs_1", "url": "https://checkout.stripe.com/cs/test"}) as create:
            app.PullwiseHandler.handle_post(handler, "/billing/checkout-sessions", {}, ["billing", "checkout-sessions"])

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["url"], "https://checkout.stripe.com/cs/test")
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

        with patch("pullwise_server.billing.create_checkout_session", return_value={"provider": "stripe", "id": "cs_1", "url": "https://checkout.stripe.com/cs/test"}) as create:
            app.PullwiseHandler.handle_post(handler, "/billing/checkout-sessions", {}, ["billing", "checkout-sessions"])

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(create.call_args.kwargs["interval"], "year")

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
            "provider": "stripe",
            "customerId": "cus_1",
            "subscriptionId": "sub_1",
            "subscriptionItemId": "si_1",
            "status": "active",
            "plan": "pro",
            "interval": "month",
        }
        handler = HandlerHarness(
            {"interval": "year", "returnUrl": "https://app.pullwise.dev/?screen=billing"},
            cookie=cookie,
        )

        with patch("pullwise_server.billing.change_subscription_interval", return_value={"provider": "stripe", "url": "https://billing.stripe.com/session", "interval": "year"}) as change:
            app.PullwiseHandler.handle_post(handler, "/billing/change-interval", {}, ["billing", "change-interval"])

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["url"], "https://billing.stripe.com/session")
        self.assertEqual(change.call_args.kwargs["interval"], "year")

    def test_free_plan_blocks_scans_after_monthly_review_limit(self) -> None:
        cookie = seed_session()
        authorize_repo_for_seed_user()
        first = HandlerHarness({"repo": "owner/repo", "requestId": "scan_req_1"}, cookie=cookie)
        second = HandlerHarness({"repo": "owner/repo", "requestId": "scan_req_2"}, cookie=cookie)

        with (
            patch.dict(os.environ, {"PULLWISE_FREE_REVIEW_LIMIT": "1"}, clear=True),
            patch("pullwise_server.review.selected_provider", return_value="codex"),
            patch.object(app.worker, "start_scan"),
        ):
            app.PullwiseHandler.handle_post(first, "/scans", {}, ["scans"])
            app.PullwiseHandler.handle_post(second, "/scans", {}, ["scans"])

        self.assertEqual(first.status, HTTPStatus.CREATED)
        self.assertEqual(second.status, HTTPStatus.PAYMENT_REQUIRED)
        self.assertIn("review limit", second.payload["message"].lower())
        self.assertEqual(app.USERS["usr_1"]["billingUsage"]["used"], 1)

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
                    "subscription": {"id": "sub_1", "status": "active"},
                    "metadata": {"userId": "usr_1"},
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(b"whsec_test", raw, hashlib.sha256).hexdigest()
        handler = HandlerHarness(raw_body=raw, headers={"creem-signature": signature})

        with patch.dict(os.environ, {"PULLWISE_CREEM_WEBHOOK_SECRET": "whsec_test"}, clear=True):
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

    def test_stripe_webhook_rejects_malformed_json_without_parser_details(self) -> None:
        raw = b"{"
        timestamp = str(int(time.time()))
        signed = timestamp.encode("utf-8") + b"." + raw
        signature = hmac.new(b"whsec_test", signed, hashlib.sha256).hexdigest()
        handler = HandlerHarness(
            path="/webhooks/stripe",
            raw_body=raw,
            headers={"Content-Length": str(len(raw)), "Stripe-Signature": f"t={timestamp},v1={signature}"},
        )

        with patch.dict(os.environ, {"PULLWISE_STRIPE_WEBHOOK_SECRET": "whsec_test"}, clear=True):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload["message"], "Request body must be valid JSON.")

    def test_stripe_webhook_rejects_non_utf8_json_without_decoder_details(self) -> None:
        raw = b"\xff"
        timestamp = str(int(time.time()))
        signed = timestamp.encode("utf-8") + b"." + raw
        signature = hmac.new(b"whsec_test", signed, hashlib.sha256).hexdigest()
        handler = HandlerHarness(
            path="/webhooks/stripe",
            raw_body=raw,
            headers={"Content-Length": str(len(raw)), "Stripe-Signature": f"t={timestamp},v1={signature}"},
        )

        with patch.dict(os.environ, {"PULLWISE_STRIPE_WEBHOOK_SECRET": "whsec_test"}, clear=True):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload["message"], "Request body must be valid JSON.")

    def test_stripe_webhook_rejects_non_object_json_body(self) -> None:
        raw = b"[]"
        timestamp = str(int(time.time()))
        signed = timestamp.encode("utf-8") + b"." + raw
        signature = hmac.new(b"whsec_test", signed, hashlib.sha256).hexdigest()
        handler = HandlerHarness(
            path="/webhooks/stripe",
            raw_body=raw,
            headers={"Content-Length": str(len(raw)), "Stripe-Signature": f"t={timestamp},v1={signature}"},
        )

        with patch.dict(os.environ, {"PULLWISE_STRIPE_WEBHOOK_SECRET": "whsec_test"}, clear=True):
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
                "provider": "stripe",
                "customerId": "cus_1",
                "subscriptionId": "sub_1",
                "status": "active",
                "eventType": "customer.subscription.updated",
                "eventId": "evt_1",
                "eventCreated": 200,
            },
        )
        app.PullwiseHandler.apply_billing_update(
            handler,
            {
                "userId": "usr_1",
                "provider": "stripe",
                "customerId": "cus_1",
                "subscriptionId": "sub_1",
                "status": "canceled",
                "eventType": "customer.subscription.deleted",
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
                "provider": "stripe",
                "customerId": "cus_1",
                "subscriptionId": "sub_1",
                "status": "active",
                "eventType": "customer.subscription.updated",
                "eventId": "evt_new",
                "eventCreated": 200,
            },
        )
        app.PullwiseHandler.apply_billing_update(
            handler,
            {
                "userId": "usr_1",
                "provider": "stripe",
                "customerId": "cus_1",
                "subscriptionId": "sub_1",
                "status": "canceled",
                "eventType": "customer.subscription.deleted",
                "eventId": "evt_old",
                "eventCreated": 100,
            },
        )

        self.assertEqual(app.USERS["usr_1"]["billing"]["status"], "active")
        self.assertIn("evt_old", app.BILLING_EVENTS)

    def test_stripe_subscription_update_waits_for_checkout_customer_mapping(self) -> None:
        seed_session()
        handler = HandlerHarness()

        app.PullwiseHandler.apply_billing_update(
            handler,
            {
                "provider": "stripe",
                "customerId": "cus_1",
                "subscriptionId": "sub_1",
                "status": "past_due",
                "eventType": "customer.subscription.updated",
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
                "provider": "stripe",
                "customerId": "cus_1",
                "customerEmail": "dev@example.com",
                "subscriptionId": "sub_1",
                "status": "active",
                "eventType": "checkout.session.completed",
                "eventId": "evt_checkout",
                "eventCreated": 200,
            },
        )

        self.assertEqual(app.BILLING_PENDING_UPDATES, [])
        self.assertEqual(app.USERS["usr_1"]["billing"]["customerId"], "cus_1")
        self.assertEqual(app.USERS["usr_1"]["billing"]["status"], "past_due")


if __name__ == "__main__":
    unittest.main()
