from __future__ import annotations

import os
import unittest


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


class ConfigurationContractsTest(unittest.TestCase):
    def test_env_example_declares_runtime_mode(self) -> None:
        values = env_example_values()

        self.assertIn("PULLWISE_MODE", values)
        self.assertIn(values["PULLWISE_MODE"], {"local", "production"})

    def test_env_example_does_not_enable_local_mocks_by_default(self) -> None:
        values = env_example_values()

        self.assertNotEqual(values.get("PULLWISE_ENABLE_LOCAL_GITHUB_MOCKS"), "true")
        self.assertNotEqual(values.get("PULLWISE_REVIEW_PROVIDER"), "mock")

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


if __name__ == "__main__":
    unittest.main()
