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

    def test_api_proxy_installs_standard_ubuntu_dependencies(self) -> None:
        script = (project_root() / "ops" / "configure_api_proxy.sh").read_text(encoding="utf-8")

        self.assertIn("ensure_host_dependencies", script)
        self.assertIn('ensure_command_available "nginx" nginx nginx', script)
        self.assertIn('ensure_command_available "certbot" certbot certbot python3-certbot-dns-cloudflare', script)
        self.assertIn('ensure_command_available "curl" curl curl', script)
        self.assertIn('ensure_command_available "python3.10" python3.10 python3.10 python3.10-venv python3-pip', script)
        self.assertIn('"$apt_get" update', script)
        self.assertIn('"$apt_get" install -y --no-install-recommends "${packages[@]}"', script)


if __name__ == "__main__":
    unittest.main()
