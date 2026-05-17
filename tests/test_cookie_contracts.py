from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from pullwise_server import app


class CookieContractsTest(unittest.TestCase):
    def test_session_cookie_is_secure_for_https_public_api_base(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_API_BASE_URL": "https://app.pullwise.dev/api"}, clear=True):
            self.assertIn("Secure", app.cookie_header("ses_1"))
            self.assertIn("Secure", app.clear_cookie_header())

    def test_session_cookie_is_not_secure_for_local_http_by_default(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_API_BASE_URL": "http://localhost:3000"}, clear=True):
            self.assertNotIn("Secure", app.cookie_header("ses_1"))


if __name__ == "__main__":
    unittest.main()
