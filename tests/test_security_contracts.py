from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from pullwise_server import app


class SecurityContractsTest(unittest.TestCase):
    def test_wildcard_allowed_origin_does_not_allow_open_redirects(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PULLWISE_APP_URL": "https://app.pullwise.dev",
                "PULLWISE_ALLOWED_ORIGINS": "*",
            },
            clear=True,
        ):
            self.assertEqual(
                app.safe_redirect_to("https://evil.example/callback", "dashboard"),
                "https://app.pullwise.dev/?screen=dashboard",
            )


if __name__ == "__main__":
    unittest.main()
