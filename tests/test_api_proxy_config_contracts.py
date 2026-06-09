from __future__ import annotations

import unittest
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


class ApiProxyConfigContractsTest(unittest.TestCase):
    def test_api_proxy_defaults_allow_admin_origin(self) -> None:
        script = (project_root() / "ops" / "configure_api_proxy.sh").read_text(encoding="utf-8")

        self.assertIn('ADMIN_ORIGIN="${ADMIN_ORIGIN:-https://admin.${ROOT_DOMAIN}}"', script)
        self.assertIn('ALLOWED_ORIGINS="${ALLOWED_ORIGINS:-${APP_ORIGIN},${ADMIN_ORIGIN}}"', script)
        self.assertIn('upsert_env PULLWISE_ALLOWED_ORIGINS "$ALLOWED_ORIGINS"', script)
        self.assertNotIn('upsert_env PULLWISE_ALLOWED_ORIGINS "$APP_ORIGIN"', script)


if __name__ == "__main__":
    unittest.main()
