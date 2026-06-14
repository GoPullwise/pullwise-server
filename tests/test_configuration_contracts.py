from __future__ import annotations

import json
import os
import unittest
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app


def project_root() -> str:
    return os.path.dirname(os.path.dirname(__file__))


def env_example_values() -> dict[str, str]:
    values: dict[str, str] = {}
    with open(os.path.join(project_root(), ".env.example"), "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


class RouteHarness(app.PullwiseHandler):
    def __init__(self, path: str) -> None:
        self.path = path
        self.headers = {"Host": "api.pullwise.dev", "Cookie": ""}
        self.payload = None
        self.status = None

    def json(self, payload: dict, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.status = status
        self.headers_out = headers or {}

    def error(self, status: int, message: str) -> None:
        self.json({"message": message}, status)


class ConfigurationContractsTest(unittest.TestCase):
    def test_env_example_declares_runtime_mode(self) -> None:
        values = env_example_values()

        self.assertIn("PULLWISE_MODE", values)
        self.assertIn(values["PULLWISE_MODE"], {"local", "production"})

    def test_env_example_does_not_enable_local_mocks_by_default(self) -> None:
        values = env_example_values()

        self.assertNotEqual(values.get("PULLWISE_ENABLE_LOCAL_GITHUB_MOCKS"), "true")
        self.assertNotIn("PULLWISE_REVIEW_PROVIDER", values)

    def test_env_example_does_not_include_magic_link_configuration(self) -> None:
        values = env_example_values()

        self.assertNotIn("PULLWISE_ENABLE_DEV_MAGIC_LINKS", values)
        self.assertNotIn("PULLWISE_EMAIL_PROVIDER", values)
        self.assertNotIn("PULLWISE_EMAIL_FROM", values)
        self.assertNotIn("PULLWISE_SMTP_HOST", values)
        self.assertNotIn("PULLWISE_SMTP_PORT", values)
        self.assertNotIn("PULLWISE_SMTP_USERNAME", values)
        self.assertNotIn("PULLWISE_SMTP_PASSWORD", values)
        self.assertNotIn("PULLWISE_SMTP_STARTTLS", values)

    def test_env_example_does_not_require_cli_api_keys(self) -> None:
        values = env_example_values()

        self.assertNotIn("ANTHROPIC_API_KEY", values)
        self.assertNotIn("CODEX_API_KEY", values)

    def test_env_example_declares_database_backed_api_rate_limits(self) -> None:
        values = env_example_values()

        self.assertEqual(values.get("PULLWISE_RATE_LIMIT_ENABLED"), "true")
        self.assertEqual(values.get("PULLWISE_RATE_LIMIT_REQUESTS"), "600")
        self.assertEqual(values.get("PULLWISE_RATE_LIMIT_WINDOW_SECONDS"), "60")

    def test_env_example_does_not_declare_database_backed_subscription_configuration(self) -> None:
        values = env_example_values()

        self.assertNotIn("PULLWISE_BILLING_PROVIDER", values)
        self.assertNotIn("PULLWISE_FREE_USER_REVIEW_LIMIT", values)
        self.assertNotIn("PULLWISE_FREE_REPO_REVIEW_LIMIT", values)
        self.assertNotIn("PULLWISE_PRO_USER_REVIEW_LIMIT", values)
        self.assertNotIn("PULLWISE_PRO_REPO_REVIEW_LIMIT", values)
        self.assertNotIn("PULLWISE_MAX_USER_REVIEW_LIMIT", values)
        self.assertNotIn("PULLWISE_MAX_REPO_REVIEW_LIMIT", values)
        self.assertNotIn("PULLWISE_PRO_CODEX_REASONING_EFFORT", values)
        self.assertNotIn("PULLWISE_MAX_CODEX_REASONING_EFFORT", values)
        self.assertNotIn("PULLWISE_PRO_OPENCODE_VARIANT", values)
        self.assertNotIn("PULLWISE_MAX_OPENCODE_VARIANT", values)
        self.assertNotIn("PULLWISE_CREEM_PRO_PRODUCT_IDS", values)
        self.assertNotIn("PULLWISE_CREEM_MAX_PRODUCT_IDS", values)
        self.assertNotIn("PULLWISE_CREEM_PRO_MONTHLY_PRODUCT_ID", values)
        self.assertNotIn("PULLWISE_CREEM_PRO_YEARLY_PRODUCT_ID", values)
        self.assertNotIn("PULLWISE_CREEM_MAX_MONTHLY_PRODUCT_ID", values)
        self.assertNotIn("PULLWISE_CREEM_MAX_YEARLY_PRODUCT_ID", values)
        self.assertNotIn("PULLWISE_CREEM_TEST_MODE", values)
        self.assertNotIn("PULLWISE_CREEM_UPGRADE_BEHAVIOR", values)
        self.assertNotIn("PULLWISE_CREEM_API_BASE_URL", values)
        self.assertNotIn("PULLWISE_CREEM_DOWNGRADE_BEHAVIOR", values)
        self.assertIn("PULLWISE_CREEM_API_KEY", values)
        self.assertIn("PULLWISE_CREEM_WEBHOOK_SECRET", values)

    def test_main_uses_default_port_for_invalid_port_env(self) -> None:
        class ServerStub:
            def __init__(self) -> None:
                self.closed = False

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def server_close(self) -> None:
                self.closed = True

        for configured_port in ["abc", "0", "70000"]:
            with self.subTest(configured_port=configured_port):
                server = ServerStub()
                addresses = []

                def server_factory(address, handler_class):
                    addresses.append(address)
                    return server

                with (
                    patch.dict(os.environ, {"PULLWISE_PORT": configured_port}, clear=True),
                    patch("sys.argv", ["pullwise-server"]),
                    patch.object(app, "load_env_file"),
                    patch.object(app.logging_config, "configure_logging"),
                    patch.object(app, "ensure_state_loaded"),
                    patch.object(app, "recover_interrupted_scans", return_value=0),
                    patch.object(app, "ThreadingHTTPServer", side_effect=server_factory),
                ):
                    app.main()

                self.assertEqual([("0.0.0.0", 8080)], addresses)
                self.assertTrue(server.closed)

    def test_health_exposes_safe_readiness_details(self) -> None:
        handler = RouteHarness("/health")

        handler.handle_get("/health", {}, ["health"])

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["reviewProvider"], "worker")
        self.assertIn("github", handler.payload)
        self.assertIn("billing", handler.payload)
        self.assertIn("limits", handler.payload)
        self.assertEqual(
            handler.payload["limits"]["repository"],
            {"maxFiles": 2000, "maxBytes": 50 * 1024 * 1024, "source": "database"},
        )
        self.assertEqual(handler.payload["database"], {"type": "sqlite", "configured": True})
        self.assertNotIn("path", handler.payload["database"])
        self.assertIn("oauthConfigured", handler.payload["github"])
        self.assertIn("appApiConfigured", handler.payload["github"])
        serialized = json.dumps(handler.payload)
        self.assertNotIn("codex", serialized.lower())
        self.assertNotIn("opencode", serialized.lower())
        self.assertNotIn("secret", serialized.lower())
        self.assertNotIn("privateKey", serialized)
        self.assertNotIn("token", serialized.lower())

    def test_repository_checkout_limits_are_configured_per_subscription_plan(self) -> None:
        config = app.system_config.default_config()

        self.assertEqual(config["plans"]["free"]["maxRepoFiles"], 200)
        self.assertEqual(config["plans"]["free"]["maxRepoBytes"], 5 * 1024 * 1024)
        self.assertEqual(config["plans"]["pro"]["maxRepoFiles"], 1000)
        self.assertEqual(config["plans"]["pro"]["maxRepoBytes"], 20 * 1024 * 1024)
        self.assertEqual(config["plans"]["max"]["maxRepoFiles"], 2000)
        self.assertEqual(config["plans"]["max"]["maxRepoBytes"], 50 * 1024 * 1024)

        groups = app.system_config.public_docs_groups(config, pro_products=[], max_products=[])
        plan_fields = {
            field["path"]: field["value"]
            for group in groups
            if group["id"] == "plans"
            for field in group["fields"]
        }
        scan_fields = {
            field["path"]
            for group in groups
            if group["id"] == "scan"
            for field in group["fields"]
        }

        self.assertEqual(plan_fields["plans.free.maxRepoFiles"], 200)
        self.assertEqual(plan_fields["plans.pro.maxRepoBytes"], 20 * 1024 * 1024)
        self.assertEqual(plan_fields["plans.max.maxRepoBytes"], 50 * 1024 * 1024)
        self.assertNotIn("scan.maxRepoFiles", scan_fields)
        self.assertNotIn("scan.maxRepoBytes", scan_fields)

    def test_global_repository_checkout_limits_do_not_migrate_to_plan_limits(self) -> None:
        config = app.system_config.default_config()
        for plan in app.system_config.PLAN_IDS:
            config["plans"][plan].pop("maxRepoFiles", None)
            config["plans"][plan].pop("maxRepoBytes", None)
        config["scan"]["maxRepoFiles"] = 345
        config["scan"]["maxRepoBytes"] = 6 * 1024 * 1024

        normalized = app.system_config.normalize_config(config)

        self.assertEqual(normalized["plans"]["free"]["maxRepoFiles"], 200)
        self.assertEqual(normalized["plans"]["pro"]["maxRepoFiles"], 1000)
        self.assertEqual(normalized["plans"]["max"]["maxRepoFiles"], 2000)

    def test_settings_default_review_output_language_is_english(self) -> None:
        previous_users = app.USERS
        previous_settings = app.SETTINGS
        try:
            app.USERS = {"usr_1": {"id": "usr_1", "name": "Taylor", "email": "taylor@example.com"}}
            app.SETTINGS = {}

            with patch.object(app.db, "load_state_item", return_value={}):
                payload = app.settings_payload("usr_1")

            self.assertEqual(payload["profile"]["name"], "Taylor")
            self.assertEqual(payload["review"]["outputLanguage"], "en")
        finally:
            app.USERS = previous_users
            app.SETTINGS = previous_settings

    def test_review_output_language_rejects_non_canonical_aliases(self) -> None:
        self.assertEqual(app.clean_review_output_language("zh"), "en")
        self.assertEqual(app.clean_review_output_language("chinese"), "en")
        self.assertEqual(app.clean_review_output_language("zh-CN"), "zh-CN")

    def test_settings_update_accepts_supported_review_output_language(self) -> None:
        previous_users = app.USERS
        previous_settings = app.SETTINGS
        previous_dirty = app.STATE_DIRTY
        try:
            app.USERS = {"usr_1": {"id": "usr_1", "name": "Taylor", "email": "taylor@example.com"}}
            app.SETTINGS = {}
            app.STATE_DIRTY = False

            payload = app.apply_settings_update("usr_1", {"review": {"outputLanguage": "fr"}})

            self.assertEqual(payload["review"]["outputLanguage"], "fr")
            self.assertEqual(app.SETTINGS["usr_1"]["review"]["outputLanguage"], "fr")
            self.assertTrue(app.STATE_DIRTY)
        finally:
            app.USERS = previous_users
            app.SETTINGS = previous_settings
            app.STATE_DIRTY = previous_dirty


if __name__ == "__main__":
    unittest.main()
