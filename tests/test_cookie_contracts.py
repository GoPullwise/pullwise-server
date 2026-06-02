from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from pullwise_server import app


class CookieContractsTest(unittest.TestCase):
    def test_session_cookie_lasts_seven_days(self) -> None:
        cookie = app.cookie_header("ses_1")

        self.assertIn("Max-Age=604800", cookie)
        self.assertEqual(app.SESSION_MAX_AGE, 60 * 60 * 24 * 7)

    def test_session_cookie_defaults_to_lax_same_site(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            cookie = app.cookie_header("ses_1")

        self.assertIn("SameSite=Lax", cookie)

    def test_session_cookie_same_site_none_is_secure_for_cross_site_admin(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_COOKIE_SAME_SITE": "None"}, clear=True):
            cookie = app.cookie_header("ses_1")

        self.assertIn("SameSite=None", cookie)
        self.assertIn("Secure", cookie)

    def test_session_cookie_is_secure_for_https_public_api_base(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_API_BASE_URL": "https://app.pullwise.dev/api"}, clear=True):
            self.assertIn("Secure", app.cookie_header("ses_1"))
            self.assertIn("Secure", app.clear_cookie_header())

    def test_session_cookie_is_not_secure_for_local_http_by_default(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_API_BASE_URL": "http://localhost:8080"}, clear=True):
            self.assertNotIn("Secure", app.cookie_header("ses_1"))

    def test_blank_cookie_secure_override_keeps_https_auto_detection(self) -> None:
        with patch.dict(
            os.environ,
            {"PULLWISE_API_BASE_URL": "https://app.pullwise.dev/api", "PULLWISE_COOKIE_SECURE": ""},
            clear=True,
        ):
            self.assertIn("Secure", app.cookie_header("ses_1"))

    def test_session_cookie_sets_domain_for_cross_subdomain_sharing(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_API_BASE_URL": "https://api.pull-wise.com"}, clear=True):
            cookie = app.cookie_header("ses_1")
            self.assertIn("Domain=.pull-wise.com", cookie)

    def test_session_cookie_skips_domain_for_localhost(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_API_BASE_URL": "http://localhost:8080"}, clear=True):
            cookie = app.cookie_header("ses_1")
            self.assertNotIn("Domain=", cookie)

    def test_session_cookie_domain_can_be_overridden(self) -> None:
        with patch.dict(
            os.environ,
            {"PULLWISE_API_BASE_URL": "https://api.pull-wise.com", "PULLWISE_COOKIE_DOMAIN": ".custom.dev"},
            clear=True,
        ):
            cookie = app.cookie_header("ses_1")
            self.assertIn("Domain=.custom.dev", cookie)


if __name__ == "__main__":
    unittest.main()
