from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

import requests

from pullwise_server import billing, system_config


def creem_product(product_id: str, *, price: int, period: str, currency: str = "USD", name: str = "Pullwise Pro") -> dict:
    return {
        "id": product_id,
        "name": name,
        "description": "Repository review for production teams.",
        "price": price,
        "currency": currency,
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


def database_config(**overrides: object) -> dict:
    config = system_config.default_config()
    for path, value in overrides.items():
        current = config
        parts = path.split("__")
        for part in parts[:-1]:
            current = current[part]
        current[parts[-1]] = value
    return config


def creem_database_config(
    *,
    pro_product_ids: tuple[str, ...] = (),
    max_product_ids: tuple[str, ...] = (),
    api_base_url: str = "https://test-api.creem.io",
    billing_timeout_seconds: int = 15,
    creem_test_mode: bool = False,
    creem_upgrade_behavior: str = "proration-charge-immediately",
    free_review_limit: int = 5,
    pro_review_limit: int = 60,
    max_review_limit: int = 90,
) -> dict:
    return database_config(
        plans__free__userReviewLimit=free_review_limit,
        plans__pro__userReviewLimit=pro_review_limit,
        plans__max__userReviewLimit=max_review_limit,
        billing__billingTimeoutSeconds=billing_timeout_seconds,
        billing__creemProProductIds=list(pro_product_ids),
        billing__creemMaxProductIds=list(max_product_ids),
        billing__creemApiBaseUrl=api_base_url,
        billing__creemTestMode=creem_test_mode,
        billing__creemUpgradeBehavior=creem_upgrade_behavior,
    )


class BillingContractsTest(unittest.TestCase):
    def test_ignored_creem_product_environment_does_not_enable_billing(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_CREEM_API_KEY": "creem_123",
                    "PULLWISE_CREEM_PRODUCT_ID": "prod_ignored",
                    "PULLWISE_CREEM_PRO_PRODUCT_IDS": "prod_ignored_monthly,prod_ignored_yearly",
                },
                clear=True,
            ),
            patch("pullwise_server.system_config.config", return_value=database_config()),
        ):
            self.assertEqual(billing.selected_provider(), "disabled")

    def test_public_plan_uses_database_catalog_and_ignores_removed_billing_env(self) -> None:
        config = database_config(
            plans__free__userReviewLimit=6,
            plans__pro__userReviewLimit=66,
            billing__creemProProductIds=["prod_db_monthly", "prod_db_yearly"],
            billing__billingTimeoutSeconds=22,
        )
        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_BILLING_PROVIDER": "creem",
                    "PULLWISE_CREEM_API_KEY": "creem_123",
                    "PULLWISE_CREEM_PRODUCT_ID": "prod_ignored",
                    "PULLWISE_CREEM_PRO_PRODUCT_IDS": "prod_ignored_monthly,prod_ignored_yearly",
                    "PULLWISE_FREE_USER_REVIEW_LIMIT": "1",
                    "PULLWISE_PRO_USER_REVIEW_LIMIT": "2",
                },
                clear=True,
            ),
            patch("pullwise_server.system_config.config", return_value=config),
            patch(
                "pullwise_server.billing.requests.get",
                side_effect=creem_product_get(
                    creem_product("prod_db_monthly", price=2900, period="every-month", currency="USD"),
                    creem_product("prod_db_yearly", price=29000, period="every-year", currency="USD"),
                ),
            ),
        ):
            plan = billing.public_plan()

        self.assertEqual(plan["plans"][0]["reviewLimit"], 6)
        self.assertEqual(plan["plans"][1]["reviewLimit"], 66)
        self.assertEqual(plan["plans"][1]["prices"]["month"]["productId"], "prod_db_monthly")
        self.assertEqual(plan["plans"][1]["prices"]["year"]["productId"], "prod_db_yearly")
        self.assertEqual(plan["checkoutTimeoutMs"], 22_000)
    def test_creem_request_settings_use_database_and_ignore_removed_env(self) -> None:
        config = database_config(
            billing__billingTimeoutSeconds=22,
            billing__creemApiBaseUrl="https://db-creem.test/v1",
            billing__creemTestMode=False,
            billing__creemUpgradeBehavior="proration-none",
        )
        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_BILLING_TIMEOUT_SECONDS": "1",
                    "PULLWISE_CREEM_API_BASE_URL": "https://ignored-creem.test",
                    "PULLWISE_CREEM_TEST_MODE": "true",
                    "PULLWISE_CREEM_UPGRADE_BEHAVIOR": "proration-charge-immediately",
                },
                clear=True,
            ),
            patch("pullwise_server.system_config.config", return_value=config),
        ):
            self.assertEqual(billing.billing_timeout_seconds(), 22)
            self.assertEqual(billing.creem_api_base_url(), "https://db-creem.test")
            self.assertEqual(
                billing.creem_subscription_update_behavior("pro", "month", "max", "month"),
                "proration-none",
            )

    def test_public_plan_exposes_free_and_pro_monthly_yearly_catalog(self) -> None:
        with (
            patch.dict(os.environ, {"PULLWISE_CREEM_API_KEY": "creem_123"}, clear=True),
            patch(
                "pullwise_server.system_config.config",
                return_value=creem_database_config(pro_product_ids=("prod_monthly", "prod_yearly")),
            ),
            patch(
                "pullwise_server.billing.requests.get",
                side_effect=creem_product_get(
                    creem_product("prod_monthly", price=2900, period="every-month", currency="USD"),
                    creem_product("prod_yearly", price=29000, period="every-year", currency="USD"),
                ),
            ),
        ):
            plan = billing.public_plan()

        self.assertEqual(plan["provider"], "creem")
        self.assertEqual(plan["currency"], "USD")
        self.assertEqual(plan["plans"][0]["id"], "free")
        self.assertEqual(plan["plans"][0]["reviewLimit"], 5)
        self.assertEqual(plan["plans"][0]["repositoryLimits"], {"maxFiles": 200, "maxBytes": 5 * 1024 * 1024, "source": "database"})
        self.assertEqual(plan["plans"][1]["id"], "pro")
        self.assertEqual(plan["plans"][1]["reviewLimit"], 60)
        self.assertEqual(plan["plans"][1]["repositoryLimits"], {"maxFiles": 1000, "maxBytes": 20 * 1024 * 1024, "source": "database"})
        self.assertEqual(plan["plans"][1]["prices"]["month"]["amount"], "29")
        self.assertEqual(plan["plans"][1]["prices"]["year"]["amount"], "290")
        self.assertEqual(plan["plans"][1]["prices"]["month"]["productId"], "prod_monthly")
        self.assertEqual(plan["plans"][1]["prices"]["year"]["productId"], "prod_yearly")
        self.assertTrue(plan["plans"][1]["prices"]["month"]["configured"])
        self.assertTrue(plan["plans"][1]["prices"]["year"]["configured"])

    def test_public_plan_exposes_max_monthly_yearly_catalog(self) -> None:
        with (
            patch.dict(os.environ, {"PULLWISE_CREEM_API_KEY": "creem_123"}, clear=True),
            patch(
                "pullwise_server.system_config.config",
                return_value=creem_database_config(
                    pro_product_ids=("prod_pro_monthly", "prod_pro_yearly"),
                    max_product_ids=("prod_max_monthly", "prod_max_yearly"),
                ),
            ),
            patch(
                "pullwise_server.billing.requests.get",
                side_effect=creem_product_get(
                    creem_product("prod_pro_monthly", price=2900, period="every-month", currency="USD"),
                    creem_product("prod_pro_yearly", price=29000, period="every-year", currency="USD"),
                    creem_product("prod_max_monthly", price=4900, period="every-month", currency="USD", name="Pullwise Max"),
                    creem_product("prod_max_yearly", price=49000, period="every-year", currency="USD", name="Pullwise Max"),
                ),
            ),
        ):
            plan = billing.public_plan()

        self.assertEqual([item["id"] for item in plan["plans"]], ["free", "pro", "max"])
        max_plan = plan["plans"][2]
        self.assertEqual(max_plan["name"], "Pullwise Max")
        self.assertEqual(max_plan["reviewLimit"], 90)
        self.assertEqual(max_plan["prices"]["month"]["amount"], "49")
        self.assertEqual(max_plan["prices"]["year"]["amount"], "490")
        self.assertEqual(max_plan["prices"]["month"]["productId"], "prod_max_monthly")
        self.assertEqual(max_plan["prices"]["year"]["productId"], "prod_max_yearly")
        self.assertTrue(max_plan["prices"]["month"]["configured"])
        self.assertTrue(max_plan["prices"]["year"]["configured"])

    def test_public_plan_can_infer_creem_product_intervals_from_product_ids(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_CREEM_API_KEY": "creem_123",
                },
                clear=True,
            ),
            patch(
                "pullwise_server.system_config.config",
                return_value=creem_database_config(pro_product_ids=("prod_yearly", "prod_monthly")),
            ),
            patch(
                "pullwise_server.billing.requests.get",
                side_effect=creem_product_get(
                    creem_product("prod_yearly", price=29000, period="every-year", currency="EUR"),
                    creem_product("prod_monthly", price=2999, period="every-month", currency="EUR"),
                ),
            ) as get,
        ):
            plan = billing.public_plan()

        self.assertEqual(plan["currency"], "EUR")
        self.assertEqual(plan["plans"][1]["prices"]["month"]["amount"], "29.99")
        self.assertEqual(plan["plans"][1]["prices"]["year"]["amount"], "290")
        self.assertEqual(plan["plans"][1]["prices"]["month"]["productId"], "prod_monthly")
        self.assertEqual(plan["plans"][1]["prices"]["year"]["productId"], "prod_yearly")
        self.assertEqual(get.call_count, 2)

    def test_public_plan_ignores_removed_review_limit_aliases(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PULLWISE_FREE_REVIEW_LIMIT": "8",
                "PULLWISE_PRO_REVIEW_LIMIT": "80",
            },
            clear=True,
        ):
            plan = billing.public_plan()

        self.assertEqual(plan["plans"][0]["reviewLimit"], 5)
        self.assertEqual(plan["plans"][1]["reviewLimit"], 60)

    def test_unrelated_environment_does_not_enable_billing(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PULLWISE_REMOVED_BILLING_SECRET": "ignored_123",
                "PULLWISE_REMOVED_PRODUCT_ID": "ignored_product",
            },
            clear=True,
        ):
            self.assertEqual(billing.selected_provider(), "disabled")

    def test_removed_provider_environment_does_not_enable_billing(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PULLWISE_BILLING_PROVIDER": "paypal",
            },
            clear=True,
        ):
            self.assertEqual(billing.selected_provider(), "disabled")

    def test_selects_creem_when_system_config_and_api_key_are_configured(self) -> None:
        with (
            patch.dict(os.environ, {"PULLWISE_CREEM_API_KEY": "creem_123"}, clear=True),
            patch(
                "pullwise_server.system_config.config",
                return_value=creem_database_config(pro_product_ids=("prod_123",)),
            ),
        ):
            self.assertEqual(billing.selected_provider(), "creem")

    def test_creates_creem_checkout_session(self) -> None:
        response = Mock()
        response.json.return_value = {"id": "chk_123", "checkout_url": "https://creem.io/checkout/chk_123", "customer": "cust_123"}
        response.raise_for_status.return_value = None

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_CREEM_API_KEY": "creem_123",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch(
                "pullwise_server.system_config.config",
                return_value=creem_database_config(pro_product_ids=("prod_123",)),
            ),
            patch(
                "pullwise_server.billing.requests.get",
                side_effect=creem_product_get(creem_product("prod_123", price=2900, period="every-month")),
            ),
            patch("pullwise_server.billing.requests.post", return_value=response) as post,
        ):
            session = billing.create_checkout_session(
                {"id": "usr_1", "email": "dev@example.com"},
                success_url="https://app.pullwise.dev/?billing=success",
                cancel_url="https://app.pullwise.dev/?billing=cancel",
            )

        self.assertEqual(session["provider"], "creem")
        self.assertEqual(session["customerId"], "cust_123")
        self.assertEqual(session["url"], "https://creem.io/checkout/chk_123")
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "https://test-api.creem.io/v1/checkouts")
        self.assertEqual(post.call_args.kwargs["headers"]["x-api-key"], "creem_123")
        json_payload = post.call_args.kwargs["json"]
        self.assertEqual(json_payload["product_id"], "prod_123")
        self.assertEqual(json_payload["success_url"], "https://app.pullwise.dev/?billing=success")
        self.assertNotIn("cancel_url", json_payload)
        self.assertEqual(json_payload["customer"]["email"], "dev@example.com")
        self.assertNotIn("id", json_payload["customer"])
        self.assertEqual(json_payload["metadata"]["userId"], "usr_1")
        self.assertEqual(session["requestId"], json_payload["request_id"])
        self.assertTrue(json_payload["request_id"].startswith("pw_checkout_usr_1_"))

    def test_creem_checkout_request_id_is_stable_inside_retry_window(self) -> None:
        first = billing.creem_checkout_request_id(
            "usr_1",
            product_id="prod_123",
            plan="pro",
            interval="month",
            now=1200,
        )
        repeated = billing.creem_checkout_request_id(
            "usr_1",
            product_id="prod_123",
            plan="pro",
            interval="month",
            now=1200 + billing.CREEM_CHECKOUT_REQUEST_ID_WINDOW_SECONDS - 1,
        )
        later = billing.creem_checkout_request_id(
            "usr_1",
            product_id="prod_123",
            plan="pro",
            interval="month",
            now=1200 + billing.CREEM_CHECKOUT_REQUEST_ID_WINDOW_SECONDS,
        )
        different_interval = billing.creem_checkout_request_id(
            "usr_1",
            product_id="prod_123",
            plan="pro",
            interval="year",
            now=1200,
        )

        self.assertEqual(first, repeated)
        self.assertNotEqual(first, later)
        self.assertNotEqual(first, different_interval)
        self.assertTrue(first.startswith("pw_checkout_usr_1_"))

    def test_creem_checkout_keeps_existing_customer_id_without_sending_it_to_checkout(self) -> None:
        response = Mock()
        response.json.return_value = {"id": "chk_123", "checkout_url": "https://creem.io/checkout/chk_123"}
        response.raise_for_status.return_value = None

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_CREEM_API_KEY": "creem_123",
                },
                clear=True,
            ),
            patch(
                "pullwise_server.system_config.config",
                return_value=creem_database_config(pro_product_ids=("prod_123",)),
            ),
            patch(
                "pullwise_server.billing.requests.get",
                side_effect=creem_product_get(creem_product("prod_123", price=2900, period="every-month")),
            ),
            patch("pullwise_server.billing.requests.post", return_value=response) as post,
        ):
            session = billing.create_checkout_session(
                {"id": "usr_1", "email": "dev@example.com", "billing": {"customerId": "cust_existing"}},
                success_url="https://app.pullwise.dev/?billing=success",
            )

        self.assertEqual(session["customerId"], "cust_existing")
        self.assertEqual(post.call_args.kwargs["json"]["customer"], {"email": "dev@example.com"})

    def test_creem_provider_error_uses_documented_message_and_trace_id(self) -> None:
        response = Mock()
        response.status_code = 400
        response.json.return_value = {
            "trace_id": "trace_123",
            "status": 400,
            "error": "Bad Request",
            "message": ["Product not found"],
            "timestamp": 1706889600000,
        }
        response.raise_for_status.side_effect = requests.HTTPError("400 Client Error", response=response)

        with (
            patch.dict(os.environ, {"PULLWISE_CREEM_API_KEY": "creem_123"}, clear=True),
            patch(
                "pullwise_server.system_config.config",
                return_value=creem_database_config(pro_product_ids=("prod_123",)),
            ),
            patch(
                "pullwise_server.billing.requests.get",
                side_effect=creem_product_get(creem_product("prod_123", price=2900, period="every-month")),
            ),
            patch("pullwise_server.billing.requests.post", return_value=response),
            self.assertRaisesRegex(
                billing.BillingProviderResponseError,
                r"Creem checkout failed \(status 400\): Product not found\. Trace ID: trace_123\.",
            ),
        ):
            billing.create_checkout_session(
                {"id": "usr_1", "email": "dev@example.com"},
                success_url="https://app.pullwise.dev/?billing=success",
            )

    def test_billing_provider_requests_use_default_timeout_for_invalid_timeout_env(self) -> None:
        for timeout_value in ["abc", "0", "-5"]:
            with self.subTest(timeout_value=timeout_value):
                response = Mock()
                response.json.return_value = {"id": "chk_123", "checkout_url": "https://creem.io/checkout/chk_123"}
                response.raise_for_status.return_value = None

                with (
                    patch.dict(
                        os.environ,
                        {
                            "PULLWISE_CREEM_API_KEY": "creem_123",
                            "PULLWISE_BILLING_TIMEOUT_SECONDS": timeout_value,
                        },
                        clear=True,
                    ),
                    patch(
                        "pullwise_server.system_config.config",
                        return_value=creem_database_config(pro_product_ids=("prod_123",)),
                    ),
                    patch(
                        "pullwise_server.billing.requests.get",
                        side_effect=creem_product_get(creem_product("prod_123", price=2900, period="every-month")),
                    ),
                    patch("pullwise_server.billing.requests.post", return_value=response) as post,
                ):
                    billing.create_checkout_session(
                        {"id": "usr_1", "email": "dev@example.com"},
                        success_url="https://app.pullwise.dev/?billing=success",
                    )

                self.assertEqual(post.call_args.kwargs["timeout"], 15)

    def test_creates_creem_yearly_checkout_session(self) -> None:
        response = Mock()
        response.json.return_value = {"id": "chk_123", "checkout_url": "https://creem.io/checkout/chk_123"}
        response.raise_for_status.return_value = None

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_CREEM_API_KEY": "creem_123",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch(
                "pullwise_server.system_config.config",
                return_value=creem_database_config(pro_product_ids=("prod_monthly", "prod_yearly")),
            ),
            patch(
                "pullwise_server.billing.requests.get",
                side_effect=creem_product_get(
                    creem_product("prod_monthly", price=2900, period="every-month"),
                    creem_product("prod_yearly", price=29000, period="every-year"),
                ),
            ),
            patch("pullwise_server.billing.requests.post", return_value=response) as post,
        ):
            session = billing.create_checkout_session(
                {"id": "usr_1", "email": "dev@example.com"},
                success_url="https://app.pullwise.dev/?billing=success",
                interval="year",
            )

        self.assertEqual(session["plan"], "pro")
        self.assertEqual(session["interval"], "year")
        json_payload = post.call_args.kwargs["json"]
        self.assertEqual(json_payload["product_id"], "prod_yearly")
        self.assertEqual(json_payload["metadata"]["plan"], "pro")
        self.assertEqual(json_payload["metadata"]["interval"], "year")

    def test_creates_creem_max_checkout_session(self) -> None:
        response = Mock()
        response.json.return_value = {"id": "chk_123", "checkout_url": "https://creem.io/checkout/chk_123"}
        response.raise_for_status.return_value = None

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_CREEM_API_KEY": "creem_123",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch(
                "pullwise_server.system_config.config",
                return_value=creem_database_config(max_product_ids=("prod_max_monthly", "prod_max_yearly")),
            ),
            patch(
                "pullwise_server.billing.requests.get",
                side_effect=creem_product_get(
                    creem_product("prod_max_monthly", price=4900, period="every-month", name="Pullwise Max"),
                    creem_product("prod_max_yearly", price=49000, period="every-year", name="Pullwise Max"),
                ),
            ),
            patch("pullwise_server.billing.requests.post", return_value=response) as post,
        ):
            session = billing.create_checkout_session(
                {"id": "usr_1", "email": "dev@example.com"},
                success_url="https://app.pullwise.dev/?billing=success",
                plan="max",
                interval="year",
            )

        self.assertEqual(session["plan"], "max")
        self.assertEqual(session["interval"], "year")
        json_payload = post.call_args.kwargs["json"]
        self.assertEqual(json_payload["product_id"], "prod_max_yearly")
        self.assertEqual(json_payload["metadata"]["plan"], "max")
        self.assertEqual(json_payload["metadata"]["interval"], "year")

    def test_creem_api_base_url_uses_system_config_test_mode_and_explicit_v1_url(self) -> None:
        with patch(
            "pullwise_server.system_config.config",
            return_value=creem_database_config(api_base_url="", creem_test_mode=True),
        ):
            self.assertEqual(billing.creem_api_base_url(), "https://test-api.creem.io")
        with patch(
            "pullwise_server.system_config.config",
            return_value=creem_database_config(api_base_url="https://test-api.creem.io/v1"),
        ):
            self.assertEqual(billing.creem_api_base_url(), "https://test-api.creem.io")
        with patch("pullwise_server.system_config.config", return_value=creem_database_config(api_base_url="")):
            self.assertEqual(billing.creem_api_base_url(), "https://api.creem.io")

    def test_creem_api_base_url_rejects_relative_urls(self) -> None:
        with patch("pullwise_server.system_config.config", return_value=creem_database_config(api_base_url="/v1")):
            with self.assertRaisesRegex(billing.BillingConfigurationError, "absolute HTTP"):
                billing.creem_api_base_url()

    def test_provider_redirect_urls_must_be_absolute_http_urls(self) -> None:
        user = {"id": "usr_1", "email": "dev@example.com", "billing": {"customerId": "cust_123"}}
        creem_env = {
            "PULLWISE_CREEM_API_KEY": "creem_123",
            "PULLWISE_APP_URL": "https://app.pullwise.dev",
        }
        scenarios = [
            (
                "creem checkout",
                {"id": "chk_123", "checkout_url": "javascript:alert(1)"},
                lambda: billing.create_checkout_session(user, success_url="https://app.pullwise.dev/success"),
            ),
        ]

        for name, payload, call in scenarios:
            with self.subTest(name=name):
                response = Mock()
                response.json.return_value = payload
                response.raise_for_status.return_value = None
                with (
                    patch.dict(os.environ, creem_env, clear=True),
                    patch(
                        "pullwise_server.system_config.config",
                        return_value=creem_database_config(pro_product_ids=("prod_monthly", "prod_yearly")),
                    ),
                    patch(
                        "pullwise_server.billing.requests.get",
                        side_effect=creem_product_get(
                            creem_product("prod_monthly", price=2900, period="every-month"),
                            creem_product("prod_yearly", price=29000, period="every-year"),
                        ),
                    ),
                    patch("pullwise_server.billing.requests.post", return_value=response),
                    self.assertRaisesRegex(RuntimeError, "safe .* URL"),
                ):
                    call()

    def test_provider_request_redirect_urls_must_be_absolute_http_urls(self) -> None:
        user = {"id": "usr_1", "email": "dev@example.com"}
        response = Mock()
        response.json.return_value = {"id": "chk_123", "checkout_url": "https://creem.io/checkout/chk_123"}
        response.raise_for_status.return_value = None

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_CREEM_API_KEY": "creem_123",
                },
                clear=True,
            ),
            patch(
                "pullwise_server.system_config.config",
                return_value=creem_database_config(pro_product_ids=("prod_123",)),
            ),
            patch("pullwise_server.billing.requests.post", return_value=response) as post,
            self.assertRaisesRegex(billing.BillingConfigurationError, "absolute HTTP"),
        ):
            billing.create_checkout_session(user, success_url="javascript:alert(1)")
        post.assert_not_called()

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_CREEM_API_KEY": "creem_123",
                },
                clear=True,
            ),
            patch(
                "pullwise_server.system_config.config",
                return_value=creem_database_config(pro_product_ids=("prod_123",)),
            ),
            patch("pullwise_server.billing.requests.post", return_value=response) as post,
            self.assertRaisesRegex(billing.BillingConfigurationError, "absolute HTTP"),
        ):
            billing.create_checkout_session(
                user,
                success_url="https://app.pullwise.dev/?billing=success",
                cancel_url="javascript:alert(1)",
            )
        post.assert_not_called()

    def test_creem_monthly_to_yearly_change_uses_upgrade_endpoint(self) -> None:
        response = Mock()
        response.json.return_value = {"id": "sub_123", "status": "active"}
        response.raise_for_status.return_value = None

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_CREEM_API_KEY": "creem_123",
                },
                clear=True,
            ),
            patch(
                "pullwise_server.system_config.config",
                return_value=creem_database_config(pro_product_ids=("prod_monthly", "prod_yearly")),
            ),
            patch(
                "pullwise_server.billing.requests.get",
                side_effect=creem_product_get(
                    creem_product("prod_monthly", price=2900, period="every-month"),
                    creem_product("prod_yearly", price=29000, period="every-year"),
                ),
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

    def test_creem_change_from_canceling_subscription_resumes_before_upgrade(self) -> None:
        resume_response = Mock()
        resume_response.json.return_value = {"id": "sub_123", "status": "active", "cancel_at_period_end": False}
        resume_response.raise_for_status.return_value = None
        upgrade_response = Mock()
        upgrade_response.json.return_value = {"id": "sub_123", "status": "active"}
        upgrade_response.raise_for_status.return_value = None

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_CREEM_API_KEY": "creem_123",
                },
                clear=True,
            ),
            patch(
                "pullwise_server.system_config.config",
                return_value=creem_database_config(pro_product_ids=("prod_monthly", "prod_yearly")),
            ),
            patch(
                "pullwise_server.billing.requests.get",
                side_effect=creem_product_get(
                    creem_product("prod_monthly", price=2900, period="every-month"),
                    creem_product("prod_yearly", price=29000, period="every-year"),
                ),
            ),
            patch("pullwise_server.billing.requests.post", side_effect=[resume_response, upgrade_response]) as post,
        ):
            result = billing.change_subscription_interval(
                {
                    "id": "usr_1",
                    "billing": {
                        "provider": " Creem ",
                        "customerId": "cust_123",
                        "subscriptionId": "sub_123",
                        "plan": "pro",
                        "interval": "month",
                        "status": "canceling",
                        "cancelAtPeriodEnd": True,
                    },
                },
                interval="year",
                return_url="https://app.pullwise.dev/?screen=billing",
            )

        self.assertEqual(result["status"], "active")
        self.assertEqual(result["cancelAtPeriodEnd"], False)
        self.assertEqual(post.call_args_list[0].args[0], "https://test-api.creem.io/v1/subscriptions/sub_123/resume")
        self.assertEqual(post.call_args_list[1].args[0], "https://test-api.creem.io/v1/subscriptions/sub_123/upgrade")

    def test_creem_cancel_uses_configured_provider_for_existing_subscription(self) -> None:
        response = Mock()
        response.json.return_value = {"id": "sub_123", "status": "scheduled_cancel"}
        response.raise_for_status.return_value = None

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_CREEM_API_KEY": "creem_123",
                },
                clear=True,
            ),
            patch(
                "pullwise_server.system_config.config",
                return_value=creem_database_config(pro_product_ids=("prod_monthly",)),
            ),
            patch("pullwise_server.billing.requests.post", return_value=response) as post,
        ):
            result = billing.cancel_subscription(
                {
                    "id": "usr_1",
                    "billing": {
                        "provider": "removed-provider",
                        "customerId": "cust_123",
                        "subscriptionId": "sub_123",
                        "plan": "pro",
                        "interval": "month",
                        "status": "active",
                    },
                }
            )

        self.assertEqual(result["provider"], "creem")
        self.assertEqual(result["status"], "canceling")
        self.assertEqual(post.call_args.args[0], "https://test-api.creem.io/v1/subscriptions/sub_123/cancel")

    def test_creem_resume_uses_configured_provider_for_existing_subscription(self) -> None:
        response = Mock()
        response.json.return_value = {"id": "sub_123", "status": "active", "cancel_at_period_end": False}
        response.raise_for_status.return_value = None

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_CREEM_API_KEY": "creem_123",
                },
                clear=True,
            ),
            patch(
                "pullwise_server.system_config.config",
                return_value=creem_database_config(pro_product_ids=("prod_monthly",)),
            ),
            patch("pullwise_server.billing.requests.post", return_value=response) as post,
        ):
            result = billing.resume_subscription(
                {
                    "id": "usr_1",
                    "billing": {
                        "provider": "removed-provider",
                        "customerId": "cust_123",
                        "subscriptionId": "sub_123",
                        "plan": "pro",
                        "interval": "month",
                        "status": "canceling",
                    },
                }
            )

        self.assertEqual(result["provider"], "creem")
        self.assertEqual(result["status"], "active")
        self.assertEqual(post.call_args.args[0], "https://test-api.creem.io/v1/subscriptions/sub_123/resume")

    def test_creem_resume_subscription_uses_resume_endpoint(self) -> None:
        response = Mock()
        response.json.return_value = {
            "id": "sub_123",
            "status": "active",
            "cancel_at_period_end": False,
            "current_period_start_date": "2026-06-01T00:00:00.000Z",
            "current_period_end_date": "2026-07-01T00:00:00.000Z",
        }
        response.raise_for_status.return_value = None

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_CREEM_API_KEY": "creem_123",
                },
                clear=True,
            ),
            patch(
                "pullwise_server.system_config.config",
                return_value=creem_database_config(pro_product_ids=("prod_monthly",)),
            ),
            patch("pullwise_server.billing.requests.post", return_value=response) as post,
        ):
            result = billing.resume_subscription(
                {
                    "id": "usr_1",
                    "billing": {
                        "provider": "creem",
                        "customerId": "cust_123",
                        "subscriptionId": "sub_123",
                        "plan": "pro",
                        "interval": "month",
                        "status": "canceling",
                    },
                },
                return_url="https://app.pullwise.dev/?screen=billing",
            )

        self.assertEqual(result["provider"], "creem")
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["cancelAtPeriodEnd"], False)
        self.assertIsNone(result["canceledAt"])
        self.assertEqual(post.call_args.args[0], "https://test-api.creem.io/v1/subscriptions/sub_123/resume")

    def test_creem_update_behavior_allows_only_plan_and_interval_upgrades(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                billing.creem_subscription_update_behavior("pro", "month", "max", "month"),
                "proration-charge-immediately",
            )
            self.assertEqual(
                billing.creem_subscription_update_behavior("max", "month", "max", "year"),
                "proration-charge-immediately",
            )
            with self.assertRaisesRegex(billing.BillingConfigurationError, "Only subscription upgrades"):
                billing.creem_subscription_update_behavior("max", "month", "pro", "month")
            with self.assertRaisesRegex(billing.BillingConfigurationError, "Only subscription upgrades"):
                billing.creem_subscription_update_behavior("max", "year", "max", "month")

    def test_creem_rejects_subscription_changes_that_are_not_upgrades(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("pullwise_server.billing.requests.post") as post:
            for plan, interval in [("pro", "year"), ("max", "month")]:
                with self.subTest(plan=plan, interval=interval):
                    with self.assertRaisesRegex(billing.BillingConfigurationError, "Only subscription upgrades"):
                        billing.change_subscription_interval(
                            {
                                "id": "usr_1",
                                "billing": {
                                    "provider": "creem",
                                    "customerId": "cust_123",
                                    "subscriptionId": "sub_123",
                                    "plan": "max",
                                    "interval": "year",
                                    "status": "active",
                                },
                            },
                            plan=plan,
                            interval=interval,
                        )
            post.assert_not_called()

    def test_creem_update_behavior_maps_invalid_database_value_to_default(self) -> None:
        with patch(
            "pullwise_server.system_config.config",
            return_value=creem_database_config(creem_upgrade_behavior="proration-charge"),
        ):
            self.assertEqual(
                billing.creem_subscription_update_behavior("pro", "month", "max", "month"),
                "proration-charge-immediately",
            )

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
        with patch(
            "pullwise_server.system_config.config",
            return_value=creem_database_config(pro_product_ids=("prod_monthly",)),
        ):
            for event_type, status in [("subscription.trialing", "trialing"), ("subscription.update", "active")]:
                with self.subTest(event_type=event_type):
                    update = billing.billing_update_from_creem_event(
                        {
                            "eventType": event_type,
                            "object": {
                                "id": "sub_123",
                                "status": status,
                                "product": {"id": "prod_monthly", "billing_period": "every-month"},
                                "customer": {"id": "cust_123", "email": "dev@example.com"},
                            },
                        }
                    )

                    self.assertIsNotNone(update)
                    self.assertEqual(update["customerId"], "cust_123")
                    self.assertEqual(update["subscriptionId"], "sub_123")

    def test_creem_subscription_event_maps_max_product_to_max_plan(self) -> None:
        with patch(
            "pullwise_server.system_config.config",
            return_value=creem_database_config(max_product_ids=("prod_max_monthly", "prod_max_yearly")),
        ):
            update = billing.billing_update_from_creem_event(
                {
                    "eventType": "subscription.update",
                    "object": {
                        "id": "sub_123",
                        "status": "active",
                        "product": {"id": "prod_max_monthly", "billing_period": "every-month"},
                        "customer": {"id": "cust_123", "email": "dev@example.com"},
                        "metadata": {"userId": "usr_1"},
                    },
                }
            )

        self.assertIsNotNone(update)
        self.assertEqual(update["plan"], "max")
        self.assertEqual(update["interval"], "month")
        self.assertEqual(update["status"], "active")

    def test_creem_subscription_event_maps_product_id_from_subscription_items(self) -> None:
        with patch(
            "pullwise_server.system_config.config",
            return_value=creem_database_config(max_product_ids=("prod_max_monthly", "prod_max_yearly")),
        ):
            update = billing.billing_update_from_creem_event(
                {
                    "eventType": "subscription.update",
                    "object": {
                        "id": "sub_123",
                        "status": "active",
                        "customer": {"id": "cust_123", "email": "dev@example.com"},
                        "metadata": {"userId": "usr_1"},
                        "items": [{"product_id": "prod_max_yearly"}],
                    },
                }
            )

        self.assertIsNotNone(update)
        self.assertEqual(update["plan"], "max")
        self.assertEqual(update["interval"], "year")


if __name__ == "__main__":
    unittest.main()
