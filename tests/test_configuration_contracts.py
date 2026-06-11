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

    def test_env_example_declares_max_subscription_configuration(self) -> None:
        values = env_example_values()

        self.assertEqual(values.get("PULLWISE_PRO_USER_REVIEW_LIMIT"), "60")
        self.assertEqual(values.get("PULLWISE_PRO_REPO_REVIEW_LIMIT"), "60")
        self.assertEqual(values.get("PULLWISE_MAX_USER_REVIEW_LIMIT"), "90")
        self.assertEqual(values.get("PULLWISE_MAX_REPO_REVIEW_LIMIT"), "90")
        self.assertNotIn("PULLWISE_PRO_CODEX_REASONING_EFFORT", values)
        self.assertNotIn("PULLWISE_MAX_CODEX_REASONING_EFFORT", values)
        self.assertNotIn("PULLWISE_PRO_OPENCODE_VARIANT", values)
        self.assertNotIn("PULLWISE_MAX_OPENCODE_VARIANT", values)
        self.assertIn("PULLWISE_CREEM_PRO_PRODUCT_IDS", values)
        self.assertIn("PULLWISE_CREEM_MAX_PRODUCT_IDS", values)
        self.assertEqual(values.get("PULLWISE_CREEM_UPGRADE_BEHAVIOR"), "proration-charge-immediately")
        self.assertNotIn("PULLWISE_CREEM_DOWNGRADE_BEHAVIOR", values)

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
            {"maxFiles": 2000, "maxBytes": 50 * 1024 * 1024},
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
