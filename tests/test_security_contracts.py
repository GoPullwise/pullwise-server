from __future__ import annotations

import os
import unittest
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app


class RouteHarness(app.PullwiseHandler):
    def __init__(
        self,
        path: str,
        body: dict | None = None,
        cookie: str = "",
        headers: dict | None = None,
        raw_body: bytes | None = None,
    ) -> None:
        self.path = path
        self._body = body or {}
        self._raw_body = raw_body
        self.headers = {"Host": "api.pullwise.dev", "Cookie": cookie, **(headers or {})}
        self.payload = None
        self.status = None

    def read_json(self) -> dict:
        return self._body

    def read_raw_body(self) -> bytes:
        return self._raw_body if self._raw_body is not None else super().read_raw_body()

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

    def test_github_login_authorize_defaults_to_dashboard_redirect(self) -> None:
        handler = RouteHarness("/auth/github/authorize")

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                    "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                    "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.github_auth.build_oauth_authorize_url", return_value="https://github.com/login/oauth/authorize"),
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["url"], "https://github.com/login/oauth/authorize")
        record = next(iter(app.GITHUB_STATES.values()))
        self.assertEqual(record["redirectTo"], "https://app.pullwise.dev/?screen=dashboard")

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

    def test_github_repository_authorize_rejects_private_app_slug_for_user_installs(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = None
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness(
            "/integrations/github/authorize?redirectTo=https%3A%2F%2Fapp.pullwise.dev%2F%3Fscreen%3Drepos",
            cookie="pw_session=ses_1",
        )

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                    "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                    "PULLWISE_GITHUB_APP_SLUG": "gopullwise",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                    "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.github_auth.app_slug_publicly_installable", return_value=False),
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.CONFLICT)
        self.assertIn("GitHub App 'gopullwise' is private", handler.payload["message"])
        self.assertIn("keep PULLWISE_GITHUB_APP_VISIBILITY_CHECK enabled", handler.payload["message"])
        self.assertEqual(app.GITHUB_STATES, {})

    def test_github_repository_authorize_fails_closed_when_app_visibility_is_unknown(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = None
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness(
            "/integrations/github/authorize?redirectTo=https%3A%2F%2Fapp.pullwise.dev%2F%3Fscreen%3Drepos",
            cookie="pw_session=ses_1",
        )

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                    "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                    "PULLWISE_GITHUB_APP_SLUG": "gopullwise",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                    "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.github_auth.app_slug_publicly_installable", return_value=None),
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertIn("Unable to verify GitHub App 'gopullwise' is public", handler.payload["message"])
        self.assertIn("keep PULLWISE_GITHUB_APP_VISIBILITY_CHECK enabled", handler.payload["message"])
        self.assertEqual(app.GITHUB_STATES, {})

    def test_github_repository_authorize_requires_slug_even_with_install_url_override(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = None
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness(
            "/integrations/github/authorize?redirectTo=https%3A%2F%2Fapp.pullwise.dev%2F%3Fscreen%3Drepos",
            cookie="pw_session=ses_1",
        )

        with patch.dict(
            os.environ,
            {
                "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                "PULLWISE_GITHUB_APP_INSTALL_URL": "https://github.com/apps/gopullwise/installations/new",
                "PULLWISE_APP_URL": "https://app.pullwise.dev",
                "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
            },
            clear=True,
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.NOT_IMPLEMENTED)
        self.assertIn("PULLWISE_GITHUB_APP_SLUG is required", handler.payload["message"])
        self.assertEqual(app.GITHUB_STATES, {})

    def test_github_repository_authorize_checks_public_slug_even_with_install_url_override(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = None
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness(
            "/integrations/github/authorize?redirectTo=https%3A%2F%2Fapp.pullwise.dev%2F%3Fscreen%3Drepos",
            cookie="pw_session=ses_1",
        )

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                    "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                    "PULLWISE_GITHUB_APP_SLUG": "gopullwise",
                    "PULLWISE_GITHUB_APP_INSTALL_URL": "https://github.com/apps/gopullwise/installations/new",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                    "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.github_auth.app_slug_publicly_installable", return_value=False),
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.CONFLICT)
        self.assertIn("GitHub App 'gopullwise' is private", handler.payload["message"])
        self.assertEqual(app.GITHUB_STATES, {})

    def test_github_repository_authorize_returns_install_url_for_public_app_slug(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = None
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness(
            "/integrations/github/authorize?redirectTo=https%3A%2F%2Fapp.pullwise.dev%2F%3Fscreen%3Drepos",
            cookie="pw_session=ses_1",
        )

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
            patch("pullwise_server.github_auth.app_slug_publicly_installable", return_value=True),
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["mode"], "github-app")
        self.assertIn("https://github.com/apps/pullwise/installations/new?state=", handler.payload["url"])
        self.assertEqual(len(app.GITHUB_STATES), 1)

    def test_github_repository_content_permission_must_be_read_only(self) -> None:
        self.assertTrue(
            app.installation_has_read_only_repository_contents(
                {"permissions": {"metadata": "read", "contents": "read"}}
            )
        )
        self.assertFalse(
            app.installation_has_read_only_repository_contents(
                {"permissions": {"metadata": "read", "contents": "write"}}
            )
        )
        self.assertFalse(app.installation_has_read_only_repository_contents({"permissions": {"metadata": "read"}}))

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

    def test_github_installation_request_without_installation_id_reports_not_completed(self) -> None:
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = None
        state = app.remember_github_state(
            "install",
            "https://app.pullwise.dev/?screen=repos",
            userId="usr_1",
            requestedScope="selected",
        )
        handler = RouteHarness(f"/integrations/github/callback?state={state}&setup_action=request")

        with patch.dict(
            os.environ,
            {
                "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                "PULLWISE_GITHUB_APP_SLUG": "pullwise",
                "PULLWISE_APP_URL": "https://app.pullwise.dev",
                "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
            },
            clear=True,
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.FOUND)
        self.assertIn("github_error=github_app_installation_not_completed", handler.location)
        self.assertIsNone(app.USERS["usr_1"].get("githubRepositoryAccess"))

    def test_github_installation_callback_without_request_or_installation_id_reports_missing_installation_id(self) -> None:
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = None
        state = app.remember_github_state(
            "install",
            "https://app.pullwise.dev/?screen=repos",
            userId="usr_1",
            requestedScope="selected",
        )
        handler = RouteHarness(f"/integrations/github/callback?state={state}&setup_action=install")

        with patch.dict(
            os.environ,
            {
                "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                "PULLWISE_GITHUB_APP_SLUG": "pullwise",
                "PULLWISE_APP_URL": "https://app.pullwise.dev",
                "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
            },
            clear=True,
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.FOUND)
        self.assertIn("github_error=missing_installation_id", handler.location)
        self.assertIsNone(app.USERS["usr_1"].get("githubRepositoryAccess"))

    def test_github_installation_callback_can_bind_without_state_when_session_user_matches_installation(self) -> None:
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = None
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness(
            "/integrations/github/callback?installation_id=999&setup_action=install",
            cookie="pw_session=ses_1",
        )

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
            patch("pullwise_server.github_auth.user_can_access_installation", return_value=True),
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.FOUND)
        self.assertEqual(app.USERS["usr_1"]["githubRepositoryAccess"]["installationId"], "999")

    def test_github_installation_callback_records_selected_private_repo_access(self) -> None:
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
                    "PULLWISE_GITHUB_APP_ID": "123",
                    "PULLWISE_GITHUB_APP_PRIVATE_KEY": "private-key",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                    "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.github_auth.user_can_access_installation", return_value=True),
            patch(
                "pullwise_server.github_auth.fetch_installation",
                return_value={
                    "repository_selection": "selected",
                    "target_type": "User",
                    "account": {"login": "octocat"},
                    "app_slug": "pullwise",
                    "permissions": {"metadata": "read", "contents": "read"},
                },
            ),
            patch(
                "pullwise_server.github_auth.list_installation_repositories",
                return_value=[
                    {
                        "id": "repo_private",
                        "name": "private-repo",
                        "fullName": "octocat/private-repo",
                        "private": True,
                        "cloneUrl": "https://github.com/octocat/private-repo.git",
                    }
                ],
            ),
        ):
            app.PullwiseHandler.route(handler, "GET")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.FOUND)
        self.assertEqual(github_access["installationAccount"], "octocat")
        self.assertEqual(github_access["installationTargetType"], "User")
        self.assertEqual(github_access["installationPermissions"]["contents"], "read")
        self.assertEqual(github_access["repositories"], ["octocat/private-repo"])
        self.assertTrue(github_access["repositoryItems"][0]["private"])

    def test_github_installation_callback_rejects_installation_without_contents_read(self) -> None:
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
                    "PULLWISE_GITHUB_APP_ID": "123",
                    "PULLWISE_GITHUB_APP_PRIVATE_KEY": "private-key",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                    "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.github_auth.user_can_access_installation", return_value=True),
            patch(
                "pullwise_server.github_auth.fetch_installation",
                return_value={
                    "repository_selection": "selected",
                    "target_type": "User",
                    "account": {"login": "octocat"},
                    "app_slug": "pullwise",
                    "permissions": {"metadata": "read"},
                },
            ),
            patch("pullwise_server.github_auth.list_installation_repositories") as list_repositories,
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("Contents: read", handler.payload["message"])
        self.assertIsNone(app.USERS["usr_1"].get("githubRepositoryAccess"))
        list_repositories.assert_not_called()

    def test_github_installation_callback_without_state_fails_closed_when_session_user_cannot_access_installation(self) -> None:
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = None
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness(
            "/integrations/github/callback?installation_id=999&setup_action=install",
            cookie="pw_session=ses_1",
        )

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
            patch("pullwise_server.github_auth.user_can_access_installation", return_value=False),
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertIsNone(app.USERS["usr_1"].get("githubRepositoryAccess"))

    def test_api_base_url_rejects_untrusted_host_header_for_magic_links(self) -> None:
        handler = RouteHarness("/", headers={"Host": "evil.example"})

        with patch.dict(
            os.environ,
            {
                "PULLWISE_APP_URL": "https://app.pullwise.dev",
                "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
            },
            clear=True,
        ):
            self.assertEqual(app.api_base_url(handler), "http://localhost:3000")

    def test_root_relative_redirect_rejects_control_characters(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_APP_URL": "https://app.pullwise.dev"}, clear=True):
            self.assertEqual(
                app.safe_redirect_to("/repos\r\nSet-Cookie:pw=bad", "dashboard"),
                "https://app.pullwise.dev/?screen=dashboard",
            )

    def test_request_body_size_is_limited(self) -> None:
        handler = RouteHarness(
            "/auth/email/magic-link",
            headers={"Content-Length": "8"},
            raw_body=b"12345678",
        )

        with patch.dict(os.environ, {"PULLWISE_MAX_BODY_BYTES": "4"}, clear=True):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)


if __name__ == "__main__":
    unittest.main()
