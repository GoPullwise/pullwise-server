from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app


class HandlerHarness(app.PullwiseHandler):
    def __init__(
        self,
        path: str,
        body: dict | None = None,
        cookie: str = "",
        headers: dict | None = None,
    ) -> None:
        self.path = path
        self._body = body or {}
        self.headers = {"Host": "api.pullwise.dev", "Cookie": cookie, **(headers or {})}
        self.payload = None
        self.status = None
        self.headers_out = {}
        self.client_address = ("203.0.113.10", 51234)

    def read_json(self) -> dict:
        return self._body

    def json(self, payload: dict, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.status = status
        self.headers_out = headers or {}

    def error(self, status: int, message: str) -> None:
        self.json({"message": message}, status)


class ApiSecurityExtensionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.persist_patcher = patch.object(app, "persist_state")
        self.persist_patcher.start()
        self.addCleanup(self.persist_patcher.stop)
        app.USERS = {
            "usr_1": {
                "id": "usr_1",
                "name": "Dev",
                "email": "dev@example.com",
                "createdAt": app.now(),
                "providers": ["github"],
            }
        }
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        app.SETTINGS = {}
        app.GITHUB_STATES = {}
        app.STATE_LOADED = True
        app.STATE_DIRTY = False

    def test_bearer_session_token_authenticates_private_routes(self) -> None:
        handler = HandlerHarness("/settings", headers={"Authorization": "Bearer ses_1"})

        with patch.dict(os.environ, {"PULLWISE_RATE_LIMIT_ENABLED": "false"}, clear=True):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["profile"]["email"], "dev@example.com")

    def test_sqlite_rate_limit_blocks_after_configured_window_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")

            with patch.dict(
                os.environ,
                {
                    "PULLWISE_DB_PATH": db_path,
                    "PULLWISE_RATE_LIMIT_ENABLED": "true",
                    "PULLWISE_RATE_LIMIT_REQUESTS": "1",
                    "PULLWISE_RATE_LIMIT_WINDOW_SECONDS": "60",
                },
                clear=True,
            ):
                first = HandlerHarness("/auth/session")
                second = HandlerHarness("/auth/session")

                app.PullwiseHandler.route(first, "GET")
                app.PullwiseHandler.route(second, "GET")

            self.assertEqual(first.status, HTTPStatus.OK)
            self.assertEqual(second.status, HTTPStatus.TOO_MANY_REQUESTS)
            self.assertIn("rate limit", second.payload["message"].lower())

            with closing(sqlite3.connect(db_path)) as connection:
                rows = connection.execute(
                    "SELECT subject, request_count FROM api_rate_limits"
                ).fetchall()

        self.assertEqual(rows, [("ip:203.0.113.10", 2)])

    def test_unauthenticated_worker_routes_are_rate_limited(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")

            with patch.dict(
                os.environ,
                {
                    "PULLWISE_DB_PATH": db_path,
                    "PULLWISE_RATE_LIMIT_ENABLED": "true",
                    "PULLWISE_RATE_LIMIT_REQUESTS": "1",
                    "PULLWISE_RATE_LIMIT_WINDOW_SECONDS": "60",
                },
                clear=True,
            ):
                first = HandlerHarness("/worker/heartbeat", {"worker_id": "wk_1"})
                second = HandlerHarness("/worker/heartbeat", {"worker_id": "wk_1"})

                app.PullwiseHandler.route(first, "POST")
                app.PullwiseHandler.route(second, "POST")

            self.assertEqual(first.status, HTTPStatus.UNAUTHORIZED)
            self.assertEqual(second.status, HTTPStatus.TOO_MANY_REQUESTS)
            self.assertIn("rate limit", second.payload["message"].lower())

    def test_authenticated_worker_routes_keep_worker_rate_limit_exemption(self) -> None:
        handler = HandlerHarness(
            "/worker/heartbeat",
            headers={"Authorization": "Bearer worker-token"},
        )

        with (
            patch.dict(os.environ, {"PULLWISE_RATE_LIMIT_ENABLED": "true"}, clear=True),
            patch.object(app.db, "get_worker_by_token", return_value={"worker_id": "wk_1"}),
            patch.object(app.db, "record_rate_limit_hit") as record_rate_limit_hit,
        ):
            limited = handler.apply_rate_limit("POST", "/worker/heartbeat")

        self.assertFalse(limited)
        record_rate_limit_hit.assert_not_called()

    def test_rate_limit_storage_failures_do_not_block_api_requests(self) -> None:
        handler = HandlerHarness("/auth/session")

        with (
            patch.dict(os.environ, {"PULLWISE_RATE_LIMIT_ENABLED": "true"}, clear=True),
            patch.object(app.db, "record_rate_limit_hit", side_effect=RuntimeError("database locked")),
            patch.object(app.logger, "exception") as log_exception,
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertFalse(handler.payload["authenticated"])
        self.assertEqual(handler.headers_out, {})
        log_exception.assert_called_once()

    def test_samesite_none_cookie_post_rejects_untrusted_origin_before_sign_out(self) -> None:
        handler = HandlerHarness(
            "/auth/sign-out",
            cookie="pw_session=ses_1",
            headers={"Origin": "https://evil.example"},
        )

        with patch.dict(
            os.environ,
            {
                "PULLWISE_COOKIE_SAME_SITE": "None",
                "PULLWISE_APP_URL": "https://app.pullwise.dev",
                "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
            },
            clear=True,
        ):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.FORBIDDEN)
        self.assertIn("ses_1", app.SESSIONS)

    def test_samesite_none_cookie_post_rejects_untrusted_origin_before_checkout_mutation(self) -> None:
        handler = HandlerHarness(
            "/billing/checkout-sessions",
            cookie="pw_session=ses_1",
            headers={"Origin": "https://evil.example"},
        )

        with patch.dict(
            os.environ,
            {
                "PULLWISE_COOKIE_SAME_SITE": "None",
                "PULLWISE_APP_URL": "https://app.pullwise.dev",
                "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                "PULLWISE_ADMIN_USER_IDS": "usr_1",
            },
            clear=True,
        ):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.FORBIDDEN)
        self.assertNotIn("billing", app.USERS["usr_1"])
        self.assertNotIn("billingCheckout", app.USERS["usr_1"])


if __name__ == "__main__":
    unittest.main()
