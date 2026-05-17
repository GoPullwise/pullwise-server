from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

from pullwise_server import billing


class BillingContractsTest(unittest.TestCase):
    def test_selects_stripe_when_only_stripe_environment_is_configured(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PULLWISE_STRIPE_SECRET_KEY": "sk_test_123",
                "PULLWISE_STRIPE_PRICE_ID": "price_123",
            },
            clear=True,
        ):
            self.assertEqual(billing.selected_provider(), "stripe")

    def test_selects_creem_when_only_creem_environment_is_configured(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PULLWISE_CREEM_API_KEY": "creem_123",
                "PULLWISE_CREEM_PRODUCT_ID": "prod_123",
            },
            clear=True,
        ):
            self.assertEqual(billing.selected_provider(), "creem")

    def test_creates_stripe_checkout_session(self) -> None:
        response = Mock()
        response.json.return_value = {"id": "cs_test_123", "url": "https://checkout.stripe.com/cs/test"}
        response.raise_for_status.return_value = None

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_STRIPE_SECRET_KEY": "sk_test_123",
                    "PULLWISE_STRIPE_PRICE_ID": "price_123",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.billing.requests.post", return_value=response) as post,
        ):
            session = billing.create_checkout_session(
                {"id": "usr_1", "email": "dev@example.com"},
                success_url="https://app.pullwise.dev/?billing=success",
                cancel_url="https://app.pullwise.dev/?billing=cancel",
            )

        self.assertEqual(session["provider"], "stripe")
        self.assertEqual(session["url"], "https://checkout.stripe.com/cs/test")
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "https://api.stripe.com/v1/checkout/sessions")
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["mode"], "subscription")
        self.assertEqual(data["line_items[0][price]"], "price_123")
        self.assertEqual(data["customer_email"], "dev@example.com")

    def test_stripe_checkout_reuses_existing_customer_id(self) -> None:
        response = Mock()
        response.json.return_value = {"id": "cs_test_123", "url": "https://checkout.stripe.com/cs/test"}
        response.raise_for_status.return_value = None

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_STRIPE_SECRET_KEY": "sk_test_123",
                    "PULLWISE_STRIPE_PRICE_ID": "price_123",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.billing.requests.post", return_value=response) as post,
        ):
            billing.create_checkout_session(
                {"id": "usr_1", "email": "dev@example.com", "billing": {"customerId": "cus_123"}},
                success_url="https://app.pullwise.dev/?billing=success",
                cancel_url="https://app.pullwise.dev/?billing=cancel",
            )

        data = post.call_args.kwargs["data"]
        self.assertEqual(data["customer"], "cus_123")
        self.assertNotIn("customer_email", data)

    def test_creates_creem_checkout_session(self) -> None:
        response = Mock()
        response.json.return_value = {"id": "chk_123", "checkout_url": "https://creem.io/checkout/chk_123"}
        response.raise_for_status.return_value = None

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_CREEM_API_KEY": "creem_123",
                    "PULLWISE_CREEM_PRODUCT_ID": "prod_123",
                    "PULLWISE_CREEM_API_BASE_URL": "https://test-api.creem.io",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.billing.requests.post", return_value=response) as post,
        ):
            session = billing.create_checkout_session(
                {"id": "usr_1", "email": "dev@example.com"},
                success_url="https://app.pullwise.dev/?billing=success",
                cancel_url="https://app.pullwise.dev/?billing=cancel",
            )

        self.assertEqual(session["provider"], "creem")
        self.assertEqual(session["url"], "https://creem.io/checkout/chk_123")
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "https://test-api.creem.io/v1/checkouts")
        json_payload = post.call_args.kwargs["json"]
        self.assertEqual(json_payload["product_id"], "prod_123")
        self.assertEqual(json_payload["customer"]["email"], "dev@example.com")
        self.assertNotIn("id", json_payload["customer"])
        self.assertEqual(json_payload["metadata"]["userId"], "usr_1")

    def test_creem_subscription_event_can_update_by_customer_id_without_metadata(self) -> None:
        update = billing.billing_update_from_creem_event(
            {
                "eventType": "subscription.canceled",
                "object": {
                    "id": "sub_123",
                    "status": "canceled",
                    "customer": {"id": "cust_123", "email": "dev@example.com"},
                },
            }
        )

        self.assertIsNotNone(update)
        self.assertEqual(update["customerId"], "cust_123")
        self.assertEqual(update["subscriptionId"], "sub_123")
        self.assertEqual(update["status"], "canceled")

    def test_creem_subscription_trialing_and_update_events_are_supported(self) -> None:
        for event_type, status in [("subscription.trialing", "trialing"), ("subscription.update", "active")]:
            with self.subTest(event_type=event_type):
                update = billing.billing_update_from_creem_event(
                    {
                        "eventType": event_type,
                        "object": {
                            "id": "sub_123",
                            "status": status,
                            "customer": {"id": "cust_123", "email": "dev@example.com"},
                        },
                    }
                )

                self.assertIsNotNone(update)
                self.assertEqual(update["customerId"], "cust_123")
                self.assertEqual(update["subscriptionId"], "sub_123")


if __name__ == "__main__":
    unittest.main()
