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

    def test_magic_link_routes_are_not_available(self) -> None:
        cases = [
            ("GET", "/dev/magic-links"),
            ("GET", "/auth/email/callback?token=tok_1"),
            ("POST", "/auth/email/magic-link"),
        ]

        for method, path in cases:
            with self.subTest(method=method, path=path):
                handler = RouteHarness(path, {"email": "dev@example.com"})

                app.PullwiseHandler.route(handler, method)

                self.assertEqual(handler.status, HTTPStatus.NOT_FOUND)

    def test_scan_creation_rejects_disabled_review_provider(self) -> None:
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        initial_scan_count = len(app.SCANS)
        handler = RouteHarness("/scans", {"repo": "owner/repo"}, cookie="pw_session=ses_1")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(app.worker, "start_scan") as start_scan,
        ):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertIn("Code review provider is not configured", handler.payload["message"])
        self.assertEqual(len(app.SCANS), initial_scan_count)
        start_scan.assert_not_called()

    def test_github_disconnect_requires_sign_in(self) -> None:
        handler = RouteHarness("/integrations/github")

        app.PullwiseHandler.route(handler, "DELETE")

        self.assertEqual(handler.status, HTTPStatus.UNAUTHORIZED)

    def test_sign_out_clears_current_session_and_cookie(self) -> None:
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + app.SESSION_MAX_AGE,
            }
        }
        handler = RouteHarness("/auth/sign-out", cookie="pw_session=ses_1")

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertNotIn("ses_1", app.SESSIONS)
        self.assertIn("Max-Age=0", handler.headers_out["Set-Cookie"])

    def test_auth_session_remains_valid_until_expiry_then_requires_login(self) -> None:
        app.SESSIONS = {
            "active": {
                "id": "active",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 60,
            },
            "expired": {
                "id": "expired",
                "userId": "usr_1",
                "createdAt": app.now() - app.SESSION_MAX_AGE - 1,
                "expiresAt": app.now() - 1,
            },
        }

        active_handler = RouteHarness("/auth/session", cookie="pw_session=active")
        app.PullwiseHandler.route(active_handler, "GET")

        expired_handler = RouteHarness("/auth/session", cookie="pw_session=expired")
        app.PullwiseHandler.route(expired_handler, "GET")

        self.assertTrue(active_handler.payload["authenticated"])
        self.assertFalse(expired_handler.payload["authenticated"])
        self.assertNotIn("expired", app.SESSIONS)

    def test_auth_session_does_not_report_pending_empty_repository_access_connected(self) -> None:
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "installationId": "999",
            "repositories": [],
            "repositoryItems": [],
            "repositoriesNeedSync": True,
        }
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness("/auth/session", cookie="pw_session=ses_1")

        app.PullwiseHandler.route(handler, "GET")

        self.assertTrue(handler.payload["authenticated"])
        self.assertFalse(handler.payload["github"]["repositoriesConnected"])
        self.assertEqual(handler.payload["github"]["repositoryCount"], 0)
        self.assertEqual(handler.payload["nextStep"], "connect_github_repositories")

    def test_auth_session_does_not_report_legacy_repository_names_only_access_connected(self) -> None:
        app.USERS["usr_1"]["githubRepositoryAccess"] = {"repositories": ["owner/repo"]}
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness("/auth/session", cookie="pw_session=ses_1")

        app.PullwiseHandler.route(handler, "GET")

        self.assertTrue(handler.payload["authenticated"])
        self.assertFalse(handler.payload["github"]["repositoriesConnected"])
        self.assertEqual(handler.payload["nextStep"], "connect_github_repositories")

    def test_auth_session_does_not_report_stale_repository_access_connected_while_authorization_is_pending(self) -> None:
        app.USERS["usr_1"]["githubLogin"] = "DFerryman"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "installationId": "111",
            "installationAccount": "SanChai20",
            "repositories": ["SanChai20/private-repo"],
            "repositoryItems": [
                {
                    "id": "repo_sanchai20_private",
                    "name": "private-repo",
                    "fullName": "SanChai20/private-repo",
                }
            ],
            "repositoriesNeedSync": False,
        }
        app.USERS["usr_1"]["githubRepositoryAccessPending"] = {
            "state": "pending_state",
            "startedAt": app.now(),
            "expiresAt": app.now() + app.GITHUB_STATE_MAX_AGE,
            "previousInstallationId": "111",
            "manage": True,
        }
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness("/auth/session", cookie="pw_session=ses_1")

        app.PullwiseHandler.route(handler, "GET")

        self.assertTrue(handler.payload["authenticated"])
        self.assertFalse(handler.payload["github"]["repositoriesConnected"])
        self.assertEqual(handler.payload["nextStep"], "connect_github_repositories")

    def test_auth_session_treats_legacy_install_state_as_pending_repository_authorization(self) -> None:
        app.USERS["usr_1"]["githubLogin"] = "DFerryman"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "installationId": "111",
            "installationAccount": "SanChai20",
            "repositories": ["SanChai20/private-repo"],
            "repositoryItems": [
                {
                    "id": "repo_sanchai20_private",
                    "name": "private-repo",
                    "fullName": "SanChai20/private-repo",
                }
            ],
            "repositoriesNeedSync": False,
        }
        app.GITHUB_STATES = {
            "legacy_state": {
                "kind": "install",
                "redirectTo": "https://app.pullwise.dev/?screen=repos",
                "userId": "usr_1",
                "requestedScope": "all",
                "expiresAt": app.now() + app.GITHUB_STATE_MAX_AGE,
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
        handler = RouteHarness("/auth/session", cookie="pw_session=ses_1")

        app.PullwiseHandler.route(handler, "GET")

        self.assertTrue(handler.payload["authenticated"])
        self.assertFalse(handler.payload["github"]["repositoriesConnected"])
        self.assertTrue(handler.payload["github"]["repositoriesAuthorizationPending"])

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

    def test_github_repository_authorize_does_not_require_pullwise_github_oauth_identity(self) -> None:
        app.USERS["usr_1"]["providers"] = ["email"]
        app.USERS["usr_1"].pop("githubAccessToken", None)
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

    def test_github_repository_authorize_binds_existing_app_installation_without_popup(self) -> None:
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
            patch("pullwise_server.github_auth.app_api_configured", return_value=True),
            patch(
                "pullwise_server.github_auth.list_current_app_installations_for_user",
                return_value=[{"id": 999}],
                create=True,
            ) as list_existing,
            patch(
                "pullwise_server.github_auth.fetch_installation",
                return_value={
                    "repository_selection": "selected",
                    "target_type": "User",
                    "account": {"login": "octocat"},
                    "app_slug": "pullwise",
                    "html_url": "https://github.com/settings/installations/999",
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
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertTrue(handler.payload["connected"])
        self.assertEqual(handler.payload["mode"], "github-app-existing")
        self.assertNotIn("url", handler.payload)
        self.assertEqual(app.GITHUB_STATES, {})
        self.assertEqual(github_access["installationId"], "999")
        self.assertEqual(github_access["repositories"], ["octocat/private-repo"])
        list_existing.assert_called_once_with("gho_user")

    def test_github_repository_authorize_returns_install_url_for_managing_connected_existing_installation(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "installationId": "999",
            "installationHtmlUrl": "https://github.com/settings/installations/999",
            "installationAccount": "any-account",
            "installationTargetType": "User",
            "repositories": ["any-account/private-repo"],
            "repositoryItems": [
                {
                    "id": "repo_private",
                    "name": "private-repo",
                    "fullName": "any-account/private-repo",
                    "private": True,
                    "cloneUrl": "https://github.com/any-account/private-repo.git",
                }
            ],
            "repositoriesNeedSync": False,
        }
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness(
            "/integrations/github/authorize?manage=1&redirectTo=https%3A%2F%2Fapp.pullwise.dev%2F%3Fscreen%3Drepos",
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
        self.assertNotIn("/settings/installations/999", handler.payload["url"])
        self.assertEqual(len(app.GITHUB_STATES), 1)

    def test_github_repository_authorize_does_not_return_cached_configure_url_for_manage(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "installationId": "999",
            "installationHtmlUrl": "https://github.com/settings/installations/999",
            "installationAccount": "other-user",
            "installationTargetType": "User",
            "repositories": ["other-user/private-repo"],
            "repositoryItems": [
                {
                    "id": "repo_private",
                    "name": "private-repo",
                    "fullName": "other-user/private-repo",
                    "private": True,
                    "cloneUrl": "https://github.com/other-user/private-repo.git",
                }
            ],
            "repositoriesNeedSync": False,
        }
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness(
            "/integrations/github/authorize?manage=1&redirectTo=https%3A%2F%2Fapp.pullwise.dev%2F%3Fscreen%3Drepos",
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
        self.assertNotIn("/settings/installations/999", handler.payload["url"])
        self.assertEqual(len(app.GITHUB_STATES), 1)

    def test_github_repository_authorize_keeps_install_url_for_pending_empty_installation(self) -> None:
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
            patch("pullwise_server.github_auth.app_api_configured", return_value=False),
            patch(
                "pullwise_server.github_auth.list_current_app_installations_for_user",
                return_value=[{"id": 999, "repository_selection": "selected"}],
            ),
            patch("pullwise_server.github_auth.list_user_installation_repositories", return_value=[]),
        ):
            app.PullwiseHandler.route(handler, "GET")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["mode"], "github-app")
        self.assertNotIn("connected", handler.payload)
        self.assertIn("https://github.com/apps/pullwise/installations/new?state=", handler.payload["url"])
        self.assertEqual(len(app.GITHUB_STATES), 1)
        self.assertEqual(github_access["installationId"], "999")
        self.assertTrue(github_access["repositoriesNeedSync"])

    def test_github_repository_authorize_opens_existing_installation_configure_url_when_repositories_are_empty(self) -> None:
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
            patch("pullwise_server.github_auth.app_api_configured", return_value=False),
            patch(
                "pullwise_server.github_auth.list_current_app_installations_for_user",
                return_value=[
                    {
                        "id": 999,
                        "repository_selection": "selected",
                        "html_url": "https://github.com/settings/installations/999",
                    }
                ],
            ),
            patch("pullwise_server.github_auth.list_user_installation_repositories", return_value=[]),
        ):
            app.PullwiseHandler.route(handler, "GET")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["mode"], "github-app-existing-pending")
        self.assertEqual(handler.payload["url"], "https://github.com/settings/installations/999")
        self.assertNotIn("connected", handler.payload)
        self.assertEqual(app.GITHUB_STATES, {})
        self.assertEqual(github_access["installationId"], "999")
        self.assertTrue(github_access["repositoriesNeedSync"])

    def test_github_repository_authorize_connects_existing_installation_from_user_token_repositories(self) -> None:
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
            patch("pullwise_server.github_auth.app_api_configured", return_value=False),
            patch(
                "pullwise_server.github_auth.list_current_app_installations_for_user",
                return_value=[
                    {
                        "id": 999,
                        "repository_selection": "selected",
                        "target_type": "User",
                        "account": {"login": "octocat"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "read"},
                    }
                ],
            ),
            patch(
                "pullwise_server.github_auth.list_user_installation_repositories",
                return_value=[
                    {
                        "id": "repo_private",
                        "name": "private-repo",
                        "fullName": "octocat/private-repo",
                        "private": True,
                        "cloneUrl": "https://github.com/octocat/private-repo.git",
                    }
                ],
            ) as list_user_repositories,
        ):
            app.PullwiseHandler.route(handler, "GET")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertTrue(handler.payload["connected"])
        self.assertNotIn("url", handler.payload)
        self.assertEqual(github_access["repositories"], ["octocat/private-repo"])
        self.assertFalse(github_access["repositoriesNeedSync"])
        list_user_repositories.assert_called_once_with("gho_user", "999")

    def test_repositories_auto_bind_existing_github_app_installation_for_dashboard(self) -> None:
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
        handler = RouteHarness("/repositories", cookie="pw_session=ses_1")

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
            patch("pullwise_server.github_auth.app_api_configured", return_value=True),
            patch(
                "pullwise_server.github_auth.list_current_app_installations_for_user",
                return_value=[{"id": 999}],
                create=True,
            ) as list_existing,
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

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertFalse(handler.payload["needsAuthorization"])
        self.assertEqual(handler.payload["items"][0]["fullName"], "octocat/private-repo")
        self.assertEqual(app.USERS["usr_1"]["githubRepositoryAccess"]["installationId"], "999")
        list_existing.assert_called_once_with("gho_user")

    def test_repositories_synthesizes_items_for_github_app_access_with_repository_names_only(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "installationId": "999",
            "repositories": ["octocat/private-repo"],
            "repositoriesNeedSync": False,
        }
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness("/repositories", cookie="pw_session=ses_1")

        app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertFalse(handler.payload["needsAuthorization"])
        self.assertEqual(handler.payload["items"][0]["fullName"], "octocat/private-repo")
        self.assertEqual(handler.payload["items"][0]["name"], "private-repo")

    def test_repositories_keep_authorization_needed_for_pending_empty_installation(self) -> None:
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
        handler = RouteHarness("/repositories/sync", cookie="pw_session=ses_1")

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
            patch("pullwise_server.github_auth.app_api_configured", return_value=False),
            patch(
                "pullwise_server.github_auth.list_current_app_installations_for_user",
                return_value=[{"id": 999, "repository_selection": "selected"}],
            ) as list_existing,
            patch("pullwise_server.github_auth.list_user_installation_repositories", return_value=[]),
            patch("pullwise_server.github_auth.fetch_installation") as fetch_installation,
            patch("pullwise_server.github_auth.list_installation_repositories") as list_repositories,
        ):
            app.PullwiseHandler.route(handler, "POST")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertTrue(handler.payload["needsAuthorization"])
        self.assertEqual(handler.payload["items"], [])
        self.assertTrue(handler.payload["repositoriesNeedSync"])
        self.assertEqual(handler.payload["authorizationIssue"], "github_app_api_unconfigured")
        self.assertIn("GitHub App API", handler.payload["message"])
        self.assertEqual(github_access["installationId"], "999")
        self.assertTrue(github_access["repositoriesNeedSync"])
        list_existing.assert_called_once_with("gho_user")
        fetch_installation.assert_not_called()
        list_repositories.assert_not_called()

    def test_repositories_auto_bind_existing_installation_from_user_token_repositories(self) -> None:
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
        handler = RouteHarness("/repositories/sync", cookie="pw_session=ses_1")

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
            patch("pullwise_server.github_auth.app_api_configured", return_value=False),
            patch(
                "pullwise_server.github_auth.list_current_app_installations_for_user",
                return_value=[
                    {
                        "id": 999,
                        "repository_selection": "selected",
                        "target_type": "User",
                        "account": {"login": "octocat"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "read"},
                    }
                ],
            ),
            patch(
                "pullwise_server.github_auth.list_user_installation_repositories",
                return_value=[
                    {
                        "id": "repo_private",
                        "name": "private-repo",
                        "fullName": "octocat/private-repo",
                        "private": True,
                        "cloneUrl": "https://github.com/octocat/private-repo.git",
                    }
                ],
            ) as list_user_repositories,
            patch("pullwise_server.github_auth.fetch_installation") as fetch_installation,
            patch("pullwise_server.github_auth.list_installation_repositories") as list_repositories,
        ):
            app.PullwiseHandler.route(handler, "POST")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertFalse(handler.payload["needsAuthorization"])
        self.assertEqual(handler.payload["items"][0]["fullName"], "octocat/private-repo")
        self.assertEqual(github_access["repositories"], ["octocat/private-repo"])
        self.assertFalse(github_access["repositoriesNeedSync"])
        list_user_repositories.assert_called_once_with("gho_user", "999")
        fetch_installation.assert_not_called()
        list_repositories.assert_not_called()

    def test_repositories_sync_rebinds_pending_manage_to_current_user_installation(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubLogin"] = "DFerryman"
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "installationId": "111",
            "installationAccount": "SanChai20",
            "installationTargetType": "User",
            "repositories": ["SanChai20/private-repo"],
            "repositoryItems": [
                {
                    "id": "repo_sanchai20_private",
                    "name": "private-repo",
                    "fullName": "SanChai20/private-repo",
                    "private": True,
                    "cloneUrl": "https://github.com/SanChai20/private-repo.git",
                }
            ],
            "repositoriesNeedSync": False,
        }
        app.USERS["usr_1"]["githubRepositoryAccessPending"] = {
            "state": "pending_state",
            "startedAt": app.now(),
            "expiresAt": app.now() + app.GITHUB_STATE_MAX_AGE,
            "previousInstallationId": "111",
            "manage": True,
        }
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness("/repositories/sync", cookie="pw_session=ses_1")

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
            patch("pullwise_server.github_auth.app_api_configured", return_value=False),
            patch(
                "pullwise_server.github_auth.list_current_app_installations_for_user",
                return_value=[
                    {
                        "id": 111,
                        "repository_selection": "all",
                        "target_type": "User",
                        "account": {"login": "SanChai20"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "read"},
                    },
                    {
                        "id": 222,
                        "repository_selection": "all",
                        "target_type": "User",
                        "account": {"login": "DFerryman"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "read"},
                    },
                ],
            ),
            patch(
                "pullwise_server.github_auth.list_user_installation_repositories",
                return_value=[
                    {
                        "id": "repo_dferryman_private",
                        "name": "private-repo",
                        "fullName": "DFerryman/private-repo",
                        "private": True,
                        "cloneUrl": "https://github.com/DFerryman/private-repo.git",
                    }
                ],
            ) as list_user_repositories,
        ):
            app.PullwiseHandler.route(handler, "POST")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertFalse(handler.payload["needsAuthorization"])
        self.assertEqual(handler.payload["installationAccount"], "DFerryman")
        self.assertEqual(handler.payload["items"][0]["fullName"], "DFerryman/private-repo")
        self.assertEqual(github_access["installationId"], "222")
        self.assertEqual(github_access["installationAccount"], "DFerryman")
        self.assertNotIn("githubRepositoryAccessPending", app.USERS["usr_1"])
        list_user_repositories.assert_called_once_with("gho_user", "222")

    def test_repositories_sync_repairs_stale_personal_installation_for_current_github_login(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubLogin"] = "DFerryman"
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "installationId": "111",
            "installationAccount": "SanChai20",
            "installationTargetType": "User",
            "repositories": ["SanChai20/private-repo"],
            "repositoryItems": [
                {
                    "id": "repo_sanchai20_private",
                    "name": "private-repo",
                    "fullName": "SanChai20/private-repo",
                    "private": True,
                    "cloneUrl": "https://github.com/SanChai20/private-repo.git",
                }
            ],
            "repositoriesNeedSync": False,
        }
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness("/repositories/sync", cookie="pw_session=ses_1")

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
            patch("pullwise_server.github_auth.app_api_configured", return_value=False),
            patch(
                "pullwise_server.github_auth.list_current_app_installations_for_user",
                return_value=[
                    {
                        "id": 111,
                        "repository_selection": "all",
                        "target_type": "User",
                        "account": {"login": "SanChai20"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "read"},
                    },
                    {
                        "id": 222,
                        "repository_selection": "all",
                        "target_type": "User",
                        "account": {"login": "DFerryman"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "read"},
                    },
                ],
            ),
            patch(
                "pullwise_server.github_auth.list_user_installation_repositories",
                return_value=[
                    {
                        "id": "repo_dferryman_private",
                        "name": "private-repo",
                        "fullName": "DFerryman/private-repo",
                        "private": True,
                        "cloneUrl": "https://github.com/DFerryman/private-repo.git",
                    }
                ],
            ),
        ):
            app.PullwiseHandler.route(handler, "POST")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["installationAccount"], "DFerryman")
        self.assertEqual(handler.payload["items"][0]["fullName"], "DFerryman/private-repo")
        self.assertEqual(github_access["installationId"], "222")

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

    def test_github_installation_callback_with_state_does_not_require_pullwise_github_oauth_identity(self) -> None:
        app.USERS["usr_1"]["providers"] = ["email"]
        app.USERS["usr_1"].pop("githubAccessToken", None)
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
            patch("pullwise_server.github_auth.app_api_configured", return_value=True),
            patch("pullwise_server.github_auth.user_can_access_installation") as user_can_access,
            patch(
                "pullwise_server.github_auth.fetch_installation",
                return_value={
                    "repository_selection": "selected",
                    "target_type": "User",
                    "account": {"login": "browser-github-user"},
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
                        "fullName": "browser-github-user/private-repo",
                        "private": True,
                        "cloneUrl": "https://github.com/browser-github-user/private-repo.git",
                    }
                ],
            ),
        ):
            app.PullwiseHandler.route(handler, "GET")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.FOUND)
        self.assertEqual(github_access["installationId"], "999")
        self.assertEqual(github_access["repositories"], ["browser-github-user/private-repo"])
        user_can_access.assert_not_called()

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
            patch("pullwise_server.github_auth.list_user_installation_repositories", return_value=[]),
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.FOUND)
        self.assertEqual(app.USERS["usr_1"]["githubRepositoryAccess"]["installationId"], "999")

    def test_github_installation_callback_with_missing_private_key_path_binds_pending_sync(self) -> None:
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
                    "PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH": "D:\\missing-pullwise.private-key.pem",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                    "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.github_auth.user_can_access_installation", return_value=True),
            patch("pullwise_server.github_auth.list_user_installation_repositories", return_value=[]),
            patch("pullwise_server.github_auth.fetch_installation") as fetch_installation,
            patch("pullwise_server.github_auth.list_installation_repositories") as list_repositories,
        ):
            app.PullwiseHandler.route(handler, "GET")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.FOUND)
        self.assertEqual(github_access["installationId"], "999")
        self.assertEqual(github_access["repositories"], [])
        self.assertTrue(github_access["repositoriesNeedSync"])
        fetch_installation.assert_not_called()
        list_repositories.assert_not_called()

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

    def test_api_base_url_rejects_untrusted_host_header(self) -> None:
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
            "/repositories/sync",
            headers={"Content-Length": "8"},
            raw_body=b"12345678",
        )

        with patch.dict(os.environ, {"PULLWISE_MAX_BODY_BYTES": "4"}, clear=True):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)


if __name__ == "__main__":
    unittest.main()
