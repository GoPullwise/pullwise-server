from __future__ import annotations

import json
import os
import tempfile
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

    def test_env_example_does_not_include_alert_email_configuration(self) -> None:
        values = env_example_values()

        for key in (
            "PULLWISE_ALERT_EMAIL_ENABLED",
            "PULLWISE_ALERT_EMAIL_TO",
            "PULLWISE_ALERT_EMAIL_FROM",
            "PULLWISE_ALERT_SMTP_HOST",
            "PULLWISE_ALERT_SMTP_PORT",
            "PULLWISE_ALERT_SMTP_USERNAME",
            "PULLWISE_ALERT_SMTP_PASSWORD",
            "PULLWISE_ALERT_SMTP_SSL",
            "PULLWISE_ALERT_SMTP_STARTTLS",
        ):
            self.assertNotIn(key, values)

    def test_env_example_does_not_require_cli_api_keys(self) -> None:
        values = env_example_values()

        self.assertNotIn("ANTHROPIC_API_KEY", values)
        self.assertNotIn("CODEX_API_KEY", values)

    def test_env_example_declares_database_backed_api_rate_limits(self) -> None:
        values = env_example_values()

        self.assertEqual(values.get("PULLWISE_RATE_LIMIT_ENABLED"), "true")
        self.assertEqual(values.get("PULLWISE_RATE_LIMIT_REQUESTS"), "600")
        self.assertEqual(values.get("PULLWISE_RATE_LIMIT_WINDOW_SECONDS"), "60")

    def test_production_mode_enables_api_rate_limit_when_not_configured(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_MODE": "production"}, clear=True):
            self.assertTrue(app.rate_limit_enabled())

        with patch.dict(
            os.environ,
            {"PULLWISE_MODE": "production", "PULLWISE_RATE_LIMIT_ENABLED": "false"},
            clear=True,
        ):
            self.assertFalse(app.rate_limit_enabled())

    def test_http_request_queue_size_defaults_for_worker_bursts(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertGreaterEqual(app.http_request_queue_size(), 512)

        with patch.dict(os.environ, {"PULLWISE_HTTP_REQUEST_QUEUE_SIZE": "1024"}, clear=True):
            self.assertEqual(app.http_request_queue_size(), 1024)

        with patch.dict(os.environ, {"PULLWISE_HTTP_REQUEST_QUEUE_SIZE": "1"}, clear=True):
            self.assertEqual(app.http_request_queue_size(), 5)
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

    def test_repository_review_quota_is_global_admin_config(self) -> None:
        config = app.system_config.default_config()
        metadata_paths = {
            field["path"]
            for group in app.system_config.metadata()
            for field in group["fields"]
        }
        public_paths = {
            field["path"]
            for group in app.system_config.public_docs_groups(config, pro_products=[], max_products=[])
            for field in group["fields"]
        }

        self.assertEqual(config["quota"]["repositoryReviewLimit"], 1000)
        self.assertIn("quota.repositoryReviewLimit", metadata_paths)
        self.assertIn("quota.repositoryReviewLimit", public_paths)
        self.assertNotIn("plans.free.repositoryReviewLimit", metadata_paths)
        self.assertNotIn("plans.pro.repositoryReviewLimit", metadata_paths)
        self.assertNotIn("plans.max.repositoryReviewLimit", metadata_paths)
    def test_scan_job_lease_and_worker_codex_timeout_are_admin_system_config_fields(self) -> None:
        config = app.system_config.default_config()
        scan_fields = {
            field["path"]: field
            for group in app.system_config.metadata()
            if group["id"] == "scan"
            for field in group["fields"]
        }
        worker_fields = {
            field["path"]: field
            for group in app.system_config.metadata()
            if group["id"] == "worker"
            for field in group["fields"]
        }

        self.assertEqual(config["scan"]["jobLeaseSeconds"], 14400)
        self.assertEqual(scan_fields["scan.jobLeaseSeconds"]["type"], "integer")
        self.assertEqual(scan_fields["scan.jobLeaseSeconds"]["min"], 60)
        self.assertEqual(config["worker"]["codexTimeoutSeconds"], 3600)
        self.assertEqual(worker_fields["worker.codexTimeoutSeconds"]["type"], "integer")
        self.assertEqual(worker_fields["worker.codexTimeoutSeconds"]["min"], 60)

    def test_previous_timeout_defaults_migrate_to_current_defaults(self) -> None:
        migrated = app.system_config.normalize_config(
            {"version": 1, "scan": {"jobLeaseSeconds": 3600}, "worker": {"codexTimeoutSeconds": 1800}}
        )
        custom = app.system_config.normalize_config(
            {"version": 1, "scan": {"jobLeaseSeconds": 7200}, "worker": {"codexTimeoutSeconds": 2400}}
        )

        self.assertEqual(migrated["scan"]["jobLeaseSeconds"], 14400)
        self.assertEqual(migrated["worker"]["codexTimeoutSeconds"], 3600)
        self.assertEqual(custom["scan"]["jobLeaseSeconds"], 7200)
        self.assertEqual(custom["worker"]["codexTimeoutSeconds"], 2400)

    def test_alert_email_is_admin_system_config_field_with_redacted_password(self) -> None:
        config = app.system_config.default_config()
        fields = {
            field["path"]: field
            for group in app.system_config.metadata()
            if group["id"] == "alerts"
            for field in group["fields"]
        }

        self.assertFalse(config["alerts"]["email"]["enabled"])
        self.assertEqual(config["alerts"]["email"]["smtpPort"], 465)
        self.assertEqual(fields["alerts.email.enabled"]["type"], "boolean")
        self.assertEqual(fields["alerts.email.to"]["type"], "stringList")
        self.assertEqual(fields["alerts.email.smtpPassword"]["type"], "password")

        config["alerts"]["email"]["smtpPassword"] = "smtp-secret"
        settings, secrets = app.system_config.admin_settings_payload(config)

        self.assertEqual(settings["alerts"]["email"]["smtpPassword"], "")
        self.assertEqual(secrets["alerts.email.smtpPassword"], {"hasValue": True})

    def test_system_config_update_keeps_existing_alert_password_when_admin_payload_is_blank(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            key_path = os.path.join(temp_dir, "state-encryption-key")
            with open(key_path, "w", encoding="ascii") as key_file:
                key_file.write("01" * 32)
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": db_path, "PULLWISE_STATE_ENCRYPTION_KEY_PATH": key_path}, clear=False):
                app.db.reset_initialization_cache()
                app.system_config.invalidate_cache()
                config = app.system_config.default_config()
                config["alerts"]["email"].update(
                    {
                        "enabled": False,
                        "to": ["ops@example.com"],
                        "smtpHost": "smtp.example.com",
                        "smtpUsername": "mailer",
                        "smtpPassword": "smtp-secret",
                    }
                )
                app.db.save_state_item(app.system_config.STATE_KEY, config)
                app.system_config.invalidate_cache()

                payload = app.system_config.admin_payload()
                updated = app.system_config.update({"settings": {"alerts": {"email": {"enabled": True, "smtpPassword": ""}}}})
                runtime_config = app.system_config.config()

        app.db.reset_initialization_cache()
        app.system_config.invalidate_cache()

        self.assertEqual(payload["settings"]["alerts"]["email"]["smtpPassword"], "")
        self.assertEqual(payload["secrets"]["alerts.email.smtpPassword"], {"hasValue": True})
        self.assertEqual(updated["settings"]["alerts"]["email"]["smtpPassword"], "")
        self.assertEqual(runtime_config["alerts"]["email"]["smtpPassword"], "smtp-secret")
        self.assertTrue(runtime_config["alerts"]["email"]["enabled"])

    def test_scan_job_attempts_are_single_run(self) -> None:
        self.assertEqual(app.system_config.scan_job_max_attempts(), 1)
    def test_worker_allowed_providers_filters_unsupported_values(self) -> None:
        with patch.object(app.system_config, "list_setting", return_value=["unsupported", " CODEX "]):
            self.assertEqual(app.system_config.worker_allowed_providers(), {"codex"})
        with patch.object(app.system_config, "list_setting", return_value=["unsupported"]):
            self.assertEqual(app.system_config.worker_allowed_providers(), {"codex"})

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
