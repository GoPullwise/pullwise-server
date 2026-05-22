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

    def test_session_cookie_is_secure_for_https_public_api_base(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_API_BASE_URL": "https://app.pullwise.dev/api"}, clear=True):
            self.assertIn("Secure", app.cookie_header("ses_1"))
            self.assertIn("Secure", app.clear_cookie_header())

    def test_session_cookie_is_not_secure_for_local_http_by_default(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_API_BASE_URL": "http://localhost:3000"}, clear=True):
            self.assertNotIn("Secure", app.cookie_header("ses_1"))

    def test_blank_cookie_secure_override_keeps_https_auto_detection(self) -> None:
        with patch.dict(
            os.environ,
            {"PULLWISE_API_BASE_URL": "https://app.pullwise.dev/api", "PULLWISE_COOKIE_SECURE": ""},
            clear=True,
        ):
            self.assertIn("Secure", app.cookie_header("ses_1"))


if __name__ == "__main__":
    unittest.main()
