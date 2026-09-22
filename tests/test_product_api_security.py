from __future__ import annotations

import unittest
from unittest.mock import patch

from pullwise_server import app


class HeaderHarness:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


class ProductApiSecurityTest(unittest.TestCase):
    def test_cookie_session_product_v1_write_requires_trusted_origin(self) -> None:
        handler = HeaderHarness({"Cookie": f"{app.SESSION_COOKIE}=ses_1"})

        with patch.object(app, "cookie_same_site", return_value="None"):
            required = app.cookie_state_change_needs_origin_check(
                "POST",
                "/api/v1/watches",
                ["api", "v1", "watches"],
                handler,
            )

        self.assertTrue(required)

    def test_api_key_only_product_v1_write_does_not_require_browser_origin(self) -> None:
        handler = HeaderHarness({"X-Pullwise-Api-Key": "pw_live_example"})

        with patch.object(app, "cookie_same_site", return_value="None"):
            required = app.cookie_state_change_needs_origin_check(
                "POST",
                "/api/v1/watches",
                ["api", "v1", "watches"],
                handler,
            )

        self.assertFalse(required)

    def test_narrow_api_automation_rate_limit_does_not_apply_to_cookie_session(self) -> None:
        class RateHarness:
            headers = {"Cookie": f"{app.SESSION_COOKIE}=ses_1"}
            client_address = ("203.0.113.10", 51234)
            _rate_limit_headers = {}

            def current_session(self):
                return {"userId": "usr_1"}

            def admin_rate_limit_exempt(self, _method, _path):
                return False

            def json(self, *_args, **_kwargs):
                raise AssertionError("session request must not hit narrow API limiter")

        handler = RateHarness()
        with (
            patch.object(app, "rate_limit_enabled", return_value=True),
            patch.object(app, "rate_limit_exempt_path", return_value=False),
            patch.object(app.db, "record_rate_limit_hit") as record,
        ):
            limited = app.PullwiseHandler.apply_rate_limit(
                handler,
                "GET",
                "/api/v1/items",
                ["api", "v1", "items"],
            )

        self.assertFalse(limited)
        record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
