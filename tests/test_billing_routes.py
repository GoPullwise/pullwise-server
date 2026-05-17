from __future__ import annotations

import os
import unittest
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app


class HandlerHarness(app.PullwiseHandler):
    def __init__(self, body: dict | None = None, cookie: str = "") -> None:
        self._body = body or {}
        self.headers = {"Host": "api.pullwise.dev", "Cookie": cookie}
        self.payload = None
        self.status = None

    def read_json(self) -> dict:
        return self._body

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
    app.STATE_LOADED = True
    app.STATE_DIRTY = False
    return "pw_session=ses_1"


class BillingRoutesTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
