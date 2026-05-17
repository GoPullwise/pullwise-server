from __future__ import annotations

import os
import unittest
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app


class RouteHarness(app.PullwiseHandler):
    def __init__(self, path: str, body: dict | None = None, cookie: str = "") -> None:
        self.path = path
        self._body = body or {}
        self.headers = {"Host": "api.pullwise.dev", "Cookie": cookie}
        self.payload = None
        self.status = None

    def read_json(self) -> dict:
        return self._body

    def json(self, payload: dict, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.status = status
        self.headers_out = headers or {}

    def error(self, status: int, message: str) -> None:
        self.json({"message": message}, status)

    def redirect(self, location: str, set_cookie: str | None = None) -> None:
        self.status = HTTPStatus.FOUND
        self.location = location
        self.headers_out = {"Set-Cookie": set_cookie} if set_cookie else {}


class SecurityContractsTest(unittest.TestCase):
    def setUp(self) -> None:
        app.USERS = {
            "usr_1": {
                "id": "usr_1",
                "name": "Dev",
                "email": "dev@example.com",
                "createdAt": app.now(),
                "providers": ["email"],
                "githubRepositoryAccess": {"repositories": ["owner/repo"]},
            }
        }
        app.SESSIONS = {}
        app.ISSUES = [
            {
                "id": "iss_1",
                "userId": "usr_1",
                "status": "open",
                "title": "Example",
            }
        ]
        app.SCANS = [
            {
                "id": "sc_1",
                "userId": "usr_1",
                "status": "done",
                "repo": "owner/repo",
            }
        ]
        app.STATE_LOADED = True
        app.STATE_DIRTY = False
        app.GITHUB_STATES = {}

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

    def test_issue_status_update_requires_sign_in(self) -> None:
        handler = RouteHarness("/issues/iss_1/status", {"status": "ignored"})

        app.PullwiseHandler.route(handler, "PATCH")

        self.assertEqual(handler.status, HTTPStatus.UNAUTHORIZED)

    def test_issue_reads_require_sign_in(self) -> None:
        for path in ["/issues", "/issues/iss_1"]:
            with self.subTest(path=path):
                handler = RouteHarness(path)

                app.PullwiseHandler.route(handler, "GET")

                self.assertEqual(handler.status, HTTPStatus.UNAUTHORIZED)

    def test_scan_reads_require_sign_in(self) -> None:
        for path in ["/scans", "/scans/sc_1"]:
            with self.subTest(path=path):
                handler = RouteHarness(path)

                app.PullwiseHandler.route(handler, "GET")

                self.assertEqual(handler.status, HTTPStatus.UNAUTHORIZED)

    def test_github_disconnect_requires_sign_in(self) -> None:
        handler = RouteHarness("/integrations/github")

        app.PullwiseHandler.route(handler, "DELETE")

        self.assertEqual(handler.status, HTTPStatus.UNAUTHORIZED)

    def test_repository_sync_requires_sign_in(self) -> None:
        handler = RouteHarness("/repositories/sync")

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.UNAUTHORIZED)

    def test_unhandled_errors_do_not_echo_internal_exception_details(self) -> None:
        handler = RouteHarness("/boom")

        def boom(_path, _params, _segments):
            raise RuntimeError("secret-token-path")

        handler.handle_get = boom

        with patch.object(app.logger, "exception") as log_exception:
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.assertEqual(handler.payload["message"], "Server error.")
        log_exception.assert_called_once()

    def test_github_installation_callback_fails_closed_when_user_access_is_unknown(self) -> None:
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = None
        state = app.remember_github_state(
            "install",
            "https://app.pullwise.dev/?screen=repos",
            userId="usr_1",
            requestedScope="selected",
        )
        handler = RouteHarness(f"/integrations/github/callback?state={state}&installation_id=999")

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                    "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                    "PULLWISE_GITHUB_APP_SLUG": "pullwise",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                    "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.github_auth.user_can_access_installation", return_value=None),
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertIsNone(app.USERS["usr_1"].get("githubRepositoryAccess"))


if __name__ == "__main__":
    unittest.main()
