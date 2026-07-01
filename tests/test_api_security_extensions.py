from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app, db


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

    def test_sqlite_rate_limit_blocks_public_rest_api_after_configured_window_limit(self) -> None:
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
                first = HandlerHarness("/api/v1/repositories")
                second = HandlerHarness("/api/v1/repositories")

                app.PullwiseHandler.route(first, "GET")
                app.PullwiseHandler.route(second, "GET")

            self.assertEqual(first.status, HTTPStatus.UNAUTHORIZED)
            self.assertEqual(second.status, HTTPStatus.TOO_MANY_REQUESTS)
            self.assertIn("rate limit", second.payload["message"].lower())

            with closing(sqlite3.connect(db_path)) as connection:
                rows = connection.execute(
                    "SELECT subject, request_count FROM api_rate_limits"
                ).fetchall()

        self.assertEqual(rows, [("ip:203.0.113.10", 2)])

    def test_browser_session_routes_do_not_consume_public_rest_api_rate_limit(self) -> None:
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
                db.initialize()
                first = HandlerHarness("/auth/session")
                second = HandlerHarness("/auth/session")

                app.PullwiseHandler.route(first, "GET")
                app.PullwiseHandler.route(second, "GET")

            self.assertEqual(first.status, HTTPStatus.OK)
            self.assertEqual(second.status, HTTPStatus.OK)
            self.assertEqual(second.payload["authenticated"], False)

            with closing(sqlite3.connect(db_path)) as connection:
                rows = connection.execute(
                    "SELECT subject, request_count FROM api_rate_limits"
                ).fetchall()

        self.assertEqual(rows, [])

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
                first = HandlerHarness("/v1/workers/wk_1/heartbeat", {"worker_id": "wk_1"})
                second = HandlerHarness("/v1/workers/wk_1/heartbeat", {"worker_id": "wk_1"})

                app.PullwiseHandler.route(first, "POST")
                app.PullwiseHandler.route(second, "POST")

            self.assertEqual(first.status, HTTPStatus.UNAUTHORIZED)
            self.assertEqual(second.status, HTTPStatus.TOO_MANY_REQUESTS)
            self.assertIn("rate limit", second.payload["message"].lower())

    def test_authenticated_worker_routes_keep_worker_rate_limit_exemption(self) -> None:
        handler = HandlerHarness(
            "/v1/workers/wk_1/heartbeat",
            headers={"Authorization": "Bearer worker-token"},
        )

        with (
            patch.dict(os.environ, {"PULLWISE_RATE_LIMIT_ENABLED": "true"}, clear=True),
            patch.object(app.db, "get_enabled_worker_token", return_value={"worker_id": "wk_1"}),
            patch.object(app.db, "record_rate_limit_hit") as record_rate_limit_hit,
        ):
            limited = handler.apply_rate_limit("POST", "/v1/workers/wk_1/heartbeat")

        self.assertFalse(limited)
        record_rate_limit_hit.assert_not_called()

    def test_deleted_worker_command_poll_keeps_worker_rate_limit_exemption(self) -> None:
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
                db.initialize()
                worker = db.create_worker({"name": "Worker", "provider": "codex"})
                worker_id = worker["worker_id"]
                command = db.create_worker_command({"worker_id": worker_id, "command": "uninstall"})
                first = HandlerHarness("/auth/session")
                poll = HandlerHarness(
                    "/worker/commands/poll",
                    {"worker_id": worker_id},
                    headers={"Authorization": f"Bearer {worker['worker_token']}"},
                )

                app.PullwiseHandler.route(first, "GET")
                app.PullwiseHandler.route(poll, "POST")

            self.assertEqual(first.status, HTTPStatus.OK)
            self.assertEqual(poll.status, HTTPStatus.OK)
            self.assertEqual(poll.payload["command"]["id"], command["id"])

    def test_rate_limit_storage_failures_block_api_requests(self) -> None:
        handler = HandlerHarness("/api/v1/repositories")

        with (
            patch.dict(os.environ, {"PULLWISE_RATE_LIMIT_ENABLED": "true"}, clear=True),
            patch.object(app.db, "record_rate_limit_hit", side_effect=RuntimeError("database locked")),
            patch.object(app.logger, "exception") as log_exception,
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertIn("rate limit", handler.payload["message"].lower())
        self.assertEqual(handler.headers_out, {"Cache-Control": "no-store"})
        log_exception.assert_called_once()

    def test_untrusted_forwarded_for_does_not_change_rate_limit_subject(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "pullwise.sqlite3")
            with patch.dict(
                os.environ,
                {
                    "PULLWISE_DB_PATH": db_path,
                    "PULLWISE_RATE_LIMIT_ENABLED": "true",
                    "PULLWISE_RATE_LIMIT_REQUESTS": "5",
                    "PULLWISE_RATE_LIMIT_WINDOW_SECONDS": "60",
                    "PULLWISE_TRUST_PROXY_HEADERS": "true",
                    "PULLWISE_TRUSTED_PROXY_CIDRS": "127.0.0.1/32",
                },
                clear=True,
            ):
                handler = HandlerHarness(
                    "/api/v1/repositories",
                    headers={"X-Forwarded-For": "198.51.100.77"},
                )
                app.PullwiseHandler.route(handler, "GET")

            with closing(sqlite3.connect(db_path)) as connection:
                rows = connection.execute(
                    "SELECT subject, request_count FROM api_rate_limits"
                ).fetchall()

        self.assertEqual(handler.status, HTTPStatus.UNAUTHORIZED)
        self.assertEqual(rows, [("ip:203.0.113.10", 1)])

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

    def test_samesite_none_settings_patch_rejects_unconfigured_frontend_origin(self) -> None:
        handler = HandlerHarness(
            "/settings",
            {"review": {"outputLanguage": "zh-CN"}},
            cookie="pw_session=ses_1",
            headers={"Origin": "https://pull-wise.com"},
        )

        with patch.dict(
            os.environ,
            {
                "PULLWISE_COOKIE_SAME_SITE": "None",
                "PULLWISE_APP_URL": "https://admin.pull-wise.com",
                "PULLWISE_ALLOWED_ORIGINS": "https://admin.pull-wise.com",
            },
            clear=True,
        ):
            app.PullwiseHandler.route(handler, "PATCH")

        self.assertEqual(handler.status, HTTPStatus.FORBIDDEN)
        self.assertNotIn("usr_1", app.SETTINGS)

    def test_samesite_none_settings_patch_accepts_configured_frontend_origin(self) -> None:
        handler = HandlerHarness(
            "/settings",
            {"review": {"outputLanguage": "zh-CN"}},
            cookie="pw_session=ses_1",
            headers={"Origin": "https://pull-wise.com"},
        )

        with patch.dict(
            os.environ,
            {
                "PULLWISE_COOKIE_SAME_SITE": "None",
                "PULLWISE_APP_URL": "https://pull-wise.com",
                "PULLWISE_ALLOWED_ORIGINS": "https://pull-wise.com",
            },
            clear=True,
        ):
            app.PullwiseHandler.route(handler, "PATCH")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["review"]["outputLanguage"], "zh-CN")
        self.assertEqual(app.SETTINGS["usr_1"]["review"]["outputLanguage"], "zh-CN")

    def test_cors_allows_configured_app_url_origin(self) -> None:
        headers: list[tuple[str, str]] = []
        handler = app.PullwiseHandler.__new__(app.PullwiseHandler)
        handler.headers = {"Origin": "https://app.pullwise.dev"}
        handler.send_header = lambda key, value: headers.append((key, value))

        with patch.dict(
            os.environ,
            {
                "PULLWISE_APP_URL": "https://app.pullwise.dev",
                "PULLWISE_ALLOWED_ORIGINS": "https://admin.pullwise.dev",
            },
            clear=True,
        ):
            app.PullwiseHandler.send_cors_headers(handler)

        self.assertIn(("Access-Control-Allow-Origin", "https://app.pullwise.dev"), headers)
        self.assertIn(("Access-Control-Allow-Credentials", "true"), headers)


if __name__ == "__main__":
    unittest.main()
