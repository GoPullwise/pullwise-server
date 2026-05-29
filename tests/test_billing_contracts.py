from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

from pullwise_server import billing


class BillingContractsTest(unittest.TestCase):
    def test_public_plan_exposes_free_and_pro_monthly_yearly_catalog(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PULLWISE_BILLING_PROVIDER": "stripe",
                "PULLWISE_STRIPE_SECRET_KEY": "sk_test_123",
                "PULLWISE_STRIPE_PRO_MONTHLY_PRICE_ID": "price_monthly",
                "PULLWISE_STRIPE_PRO_YEARLY_PRICE_ID": "price_yearly",
                "PULLWISE_FREE_USER_REVIEW_LIMIT": "5",
                "PULLWISE_PRO_USER_REVIEW_LIMIT": "100",
                "PULLWISE_PRO_MONTHLY_AMOUNT": "29",
                "PULLWISE_PRO_YEARLY_AMOUNT": "290",
            },
            clear=True,
        ):
            plan = billing.public_plan()

        self.assertEqual(plan["provider"], "stripe")
        self.assertEqual(plan["currency"], "USD")
        self.assertEqual(plan["plans"][0]["id"], "free")
        self.assertEqual(plan["plans"][0]["reviewLimit"], 5)
        self.assertEqual(plan["plans"][1]["id"], "pro")
        self.assertEqual(plan["plans"][1]["reviewLimit"], 100)
        self.assertEqual(plan["plans"][1]["prices"]["month"]["amount"], "29")
        self.assertEqual(plan["plans"][1]["prices"]["year"]["amount"], "290")
        self.assertTrue(plan["plans"][1]["prices"]["month"]["configured"])
        self.assertTrue(plan["plans"][1]["prices"]["year"]["configured"])

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

    def test_billing_provider_requests_use_default_timeout_for_invalid_timeout_env(self) -> None:
        for timeout_value in ["abc", "0", "-5"]:
            with self.subTest(timeout_value=timeout_value):
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
                            "PULLWISE_BILLING_TIMEOUT_SECONDS": timeout_value,
                        },
                        clear=True,
                    ),
                    patch("pullwise_server.billing.requests.post", return_value=response) as post,
                ):
                    billing.create_checkout_session(
                        {"id": "usr_1", "email": "dev@example.com"},
                        success_url="https://app.pullwise.dev/?billing=success",
                        cancel_url="https://app.pullwise.dev/?billing=cancel",
                    )

                self.assertEqual(post.call_args.kwargs["timeout"], 15)

    def test_creates_stripe_yearly_checkout_session_with_subscription_metadata(self) -> None:
        response = Mock()
        response.json.return_value = {"id": "cs_test_123", "url": "https://checkout.stripe.com/cs/test"}
        response.raise_for_status.return_value = None

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_STRIPE_SECRET_KEY": "sk_test_123",
                    "PULLWISE_STRIPE_PRO_MONTHLY_PRICE_ID": "price_monthly",
                    "PULLWISE_STRIPE_PRO_YEARLY_PRICE_ID": "price_yearly",
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
                interval="year",
            )

        self.assertEqual(session["plan"], "pro")
        self.assertEqual(session["interval"], "year")
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["line_items[0][price]"], "price_yearly")
        self.assertEqual(data["metadata[userId]"], "usr_1")
        self.assertEqual(data["metadata[plan]"], "pro")
        self.assertEqual(data["metadata[interval]"], "year")
        self.assertEqual(data["subscription_data[metadata][userId]"], "usr_1")
        self.assertEqual(data["subscription_data[metadata][plan]"], "pro")
        self.assertEqual(data["subscription_data[metadata][interval]"], "year")

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

    def test_creates_creem_yearly_checkout_session(self) -> None:
        response = Mock()
        response.json.return_value = {"id": "chk_123", "checkout_url": "https://creem.io/checkout/chk_123"}
        response.raise_for_status.return_value = None

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_CREEM_API_KEY": "creem_123",
                    "PULLWISE_CREEM_PRO_MONTHLY_PRODUCT_ID": "prod_monthly",
                    "PULLWISE_CREEM_PRO_YEARLY_PRODUCT_ID": "prod_yearly",
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
                interval="year",
            )

        self.assertEqual(session["plan"], "pro")
        self.assertEqual(session["interval"], "year")
        json_payload = post.call_args.kwargs["json"]
        self.assertEqual(json_payload["product_id"], "prod_yearly")
        self.assertEqual(json_payload["metadata"]["plan"], "pro")
        self.assertEqual(json_payload["metadata"]["interval"], "year")

    def test_provider_redirect_urls_must_be_absolute_http_urls(self) -> None:
        user = {"id": "usr_1", "email": "dev@example.com", "billing": {"customerId": "cus_123"}}
        stripe_env = {
            "PULLWISE_STRIPE_SECRET_KEY": "sk_test_123",
            "PULLWISE_STRIPE_PRO_MONTHLY_PRICE_ID": "price_monthly",
            "PULLWISE_STRIPE_PRO_YEARLY_PRICE_ID": "price_yearly",
            "PULLWISE_APP_URL": "https://app.pullwise.dev",
        }
        creem_env = {
            "PULLWISE_CREEM_API_KEY": "creem_123",
            "PULLWISE_CREEM_PRO_MONTHLY_PRODUCT_ID": "prod_monthly",
            "PULLWISE_CREEM_PRO_YEARLY_PRODUCT_ID": "prod_yearly",
            "PULLWISE_CREEM_API_BASE_URL": "https://test-api.creem.io",
            "PULLWISE_APP_URL": "https://app.pullwise.dev",
        }
        scenarios = [
            (
                "stripe checkout",
                stripe_env,
                {"id": "cs_test_123", "url": "javascript:alert(1)"},
                lambda: billing.create_checkout_session(user, success_url="https://app.pullwise.dev/success", cancel_url="https://app.pullwise.dev/cancel"),
            ),
            (
                "creem checkout",
                creem_env,
                {"id": "chk_123", "checkout_url": "javascript:alert(1)"},
                lambda: billing.create_checkout_session(user, success_url="https://app.pullwise.dev/success", cancel_url="https://app.pullwise.dev/cancel"),
            ),
            (
                "stripe portal",
                stripe_env,
                {"id": "bps_123", "url": "javascript:alert(1)"},
                lambda: billing.create_portal_session(user, return_url="https://app.pullwise.dev/settings"),
            ),
            (
                "creem portal",
                creem_env,
                {"customer_portal_link": "javascript:alert(1)"},
                lambda: billing.create_portal_session(user, return_url="https://app.pullwise.dev/settings"),
            ),
            (
                "stripe interval change",
                stripe_env,
                {"id": "bps_123", "url": "javascript:alert(1)"},
                lambda: billing.change_subscription_interval(
                    {
                        "id": "usr_1",
                        "billing": {
                            "provider": "stripe",
                            "customerId": "cus_123",
                            "subscriptionId": "sub_123",
                            "subscriptionItemId": "si_123",
                            "plan": "pro",
                            "interval": "month",
                            "status": "active",
                        },
                    },
                    interval="year",
                    return_url="https://app.pullwise.dev/billing",
                ),
            ),
        ]

        for name, env_vars, payload, call in scenarios:
            with self.subTest(name=name):
                response = Mock()
                response.json.return_value = payload
                response.raise_for_status.return_value = None
                with (
                    patch.dict(os.environ, env_vars, clear=True),
                    patch("pullwise_server.billing.requests.post", return_value=response),
                    self.assertRaisesRegex(RuntimeError, "safe .* URL"),
                ):
                    call()

    def test_provider_request_redirect_urls_must_be_absolute_http_urls(self) -> None:
        user = {"id": "usr_1", "email": "dev@example.com", "billing": {"customerId": "cus_123"}}
        stripe_env = {
            "PULLWISE_STRIPE_SECRET_KEY": "sk_test_123",
            "PULLWISE_STRIPE_PRO_MONTHLY_PRICE_ID": "price_monthly",
            "PULLWISE_STRIPE_PRO_YEARLY_PRICE_ID": "price_yearly",
            "PULLWISE_APP_URL": "https://app.pullwise.dev",
        }
        creem_env = {
            "PULLWISE_CREEM_API_KEY": "creem_123",
            "PULLWISE_CREEM_PRO_MONTHLY_PRODUCT_ID": "prod_monthly",
            "PULLWISE_CREEM_PRO_YEARLY_PRODUCT_ID": "prod_yearly",
            "PULLWISE_CREEM_API_BASE_URL": "https://test-api.creem.io",
            "PULLWISE_APP_URL": "https://app.pullwise.dev",
        }
        interval_user = {
            "id": "usr_1",
            "billing": {
                "provider": "stripe",
                "customerId": "cus_123",
                "subscriptionId": "sub_123",
                "subscriptionItemId": "si_123",
                "plan": "pro",
                "interval": "month",
                "status": "active",
            },
        }
        scenarios = [
            (
                "stripe checkout success",
                stripe_env,
                lambda: billing.create_checkout_session(user, success_url="javascript:alert(1)", cancel_url="https://app.pullwise.dev/cancel"),
            ),
            (
                "stripe checkout cancel",
                stripe_env,
                lambda: billing.create_checkout_session(user, success_url="https://app.pullwise.dev/success", cancel_url="javascript:alert(1)"),
            ),
            (
                "creem checkout success",
                creem_env,
                lambda: billing.create_checkout_session(user, success_url="javascript:alert(1)", cancel_url="https://app.pullwise.dev/cancel"),
            ),
            (
                "stripe portal return",
                stripe_env,
                lambda: billing.create_portal_session(user, return_url="javascript:alert(1)"),
            ),
            (
                "stripe interval return",
                stripe_env,
                lambda: billing.change_subscription_interval(interval_user, interval="year", return_url="javascript:alert(1)"),
            ),
        ]

        for name, env_vars, call in scenarios:
            with self.subTest(name=name):
                response = Mock()
                response.json.return_value = {"id": "safe", "url": "https://provider.example/session"}
                response.raise_for_status.return_value = None
                with (
                    patch.dict(os.environ, env_vars, clear=True),
                    patch("pullwise_server.billing.requests.post", return_value=response) as post,
                    self.assertRaisesRegex(billing.BillingConfigurationError, "absolute HTTP"),
                ):
                    call()
                post.assert_not_called()

    def test_stripe_monthly_to_yearly_change_uses_portal_update_confirmation(self) -> None:
        response = Mock()
        response.json.return_value = {"id": "bps_123", "url": "https://billing.stripe.com/session"}
        response.raise_for_status.return_value = None

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_STRIPE_SECRET_KEY": "sk_test_123",
                    "PULLWISE_STRIPE_PRO_MONTHLY_PRICE_ID": "price_monthly",
                    "PULLWISE_STRIPE_PRO_YEARLY_PRICE_ID": "price_yearly",
                },
                clear=True,
            ),
            patch("pullwise_server.billing.requests.post", return_value=response) as post,
        ):
            result = billing.change_subscription_interval(
                {
                    "id": "usr_1",
                    "billing": {
                        "customerId": "cus_123",
                        "subscriptionId": "sub_123",
                        "subscriptionItemId": "si_123",
                        "plan": "pro",
                        "interval": "month",
                        "status": "active",
                    },
                },
                interval="year",
                return_url="https://app.pullwise.dev/?screen=billing",
            )

        self.assertEqual(result["provider"], "stripe")
        self.assertEqual(result["url"], "https://billing.stripe.com/session")
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["flow_data[type]"], "subscription_update_confirm")
        self.assertEqual(data["flow_data[subscription_update_confirm][subscription]"], "sub_123")
        self.assertEqual(data["flow_data[subscription_update_confirm][items][0][id]"], "si_123")
        self.assertEqual(data["flow_data[subscription_update_confirm][items][0][price]"], "price_yearly")

    def test_creem_monthly_to_yearly_change_uses_upgrade_endpoint(self) -> None:
        response = Mock()
        response.json.return_value = {"id": "sub_123", "status": "active"}
        response.raise_for_status.return_value = None

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_CREEM_API_KEY": "creem_123",
                    "PULLWISE_CREEM_PRO_MONTHLY_PRODUCT_ID": "prod_monthly",
                    "PULLWISE_CREEM_PRO_YEARLY_PRODUCT_ID": "prod_yearly",
                    "PULLWISE_CREEM_API_BASE_URL": "https://test-api.creem.io",
                },
                clear=True,
            ),
            patch("pullwise_server.billing.requests.post", return_value=response) as post,
        ):
            result = billing.change_subscription_interval(
                {
                    "id": "usr_1",
                    "billing": {
                        "provider": "creem",
                        "customerId": "cust_123",
                        "subscriptionId": "sub_123",
                        "plan": "pro",
                        "interval": "month",
                        "status": "active",
                    },
                },
                interval="year",
                return_url="https://app.pullwise.dev/?screen=billing",
            )

        self.assertEqual(result["provider"], "creem")
        self.assertEqual(result["interval"], "year")
        self.assertEqual(post.call_args.args[0], "https://test-api.creem.io/v1/subscriptions/sub_123/upgrade")
        self.assertEqual(post.call_args.kwargs["json"]["product_id"], "prod_yearly")
        self.assertEqual(post.call_args.kwargs["json"]["update_behavior"], "proration-charge-immediately")

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
