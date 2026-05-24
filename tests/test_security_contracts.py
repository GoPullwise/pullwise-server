from __future__ import annotations

import os
import tempfile
import threading
import time
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


class RawBodyRouteHarness(RouteHarness):
    def read_json(self) -> dict:
        return app.PullwiseHandler.read_json(self)


class SecurityContractsTest(unittest.TestCase):
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
                "providers": ["email"],
                "githubId": "1",
                "githubLogin": "octocat",
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
        with app.PREVIEW_SCAN_LOCKS_GUARD:
            app.PREVIEW_SCAN_LOCKS.clear()

    def signed_in(self):
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        return "pw_session=ses_1"

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

    def test_real_github_user_uses_safe_login_fallback_for_malformed_profile_id(self) -> None:
        app.USERS = {}

        user = app.get_or_create_real_github_user(
            {
                "id": {"node_id": "bad"},
                "login": "OctoCat",
                "primaryEmail": "octocat@example.com",
                "name": "Octo Cat",
            },
            {"access_token": "gho_user", "token_type": "bearer", "scope": "read:user"},
        )

        self.assertEqual(user["id"], "usr_github_octocat")
        self.assertEqual(user["githubId"], "octocat")
        self.assertNotIn("usr_github_{'node_id': 'bad'}", app.USERS)

    def test_real_github_user_sanitizes_malformed_profile_display_fields(self) -> None:
        app.USERS = {}

        user = app.get_or_create_real_github_user(
            {
                "id": 123,
                "login": "OctoCat",
                "primaryEmail": {"email": "bad@example.com"},
                "email": {"email": "bad@example.com"},
                "name": {"display": "Bad Name"},
                "avatar_url": {"url": "https://avatars.githubusercontent.com/u/123"},
                "html_url": "javascript:alert(1)",
            },
            {"access_token": "gho_user", "token_type": "bearer", "scope": "read:user"},
        )

        self.assertEqual(user["name"], "OctoCat")
        self.assertEqual(user["email"], "OctoCat@users.noreply.github.com")
        self.assertIsNone(user["avatarUrl"])
        self.assertIsNone(user["githubHtmlUrl"])

    def test_issue_status_update_requires_sign_in(self) -> None:
        handler = RouteHarness("/issues/iss_1/status", {"status": "ignored"})

        app.PullwiseHandler.route(handler, "PATCH")

        self.assertEqual(handler.status, HTTPStatus.UNAUTHORIZED)

    def test_issue_fix_preview_requires_sign_in(self) -> None:
        handler = RouteHarness("/issues/iss_1/fixes/preview")

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.UNAUTHORIZED)

    def test_issue_fix_preview_returns_deterministic_preview(self) -> None:
        app.ISSUES = [
            {
                "id": "iss_1",
                "userId": "usr_1",
                "status": "open",
                "title": "Example",
                "repo": "owner/repo",
                "scanId": "sc_1",
                "autoFix": True,
                "file": "src/auth.py",
                "badCode": [{"ln": 1, "code": "old()", "t": "del"}],
                "goodCode": [{"ln": 1, "code": "new()", "t": "add"}],
            }
        ]
        preview = {
            "issueId": "iss_1",
            "autoFixable": True,
            "valid": True,
            "repository": "owner/repo",
            "branch": "main",
            "file": "src/auth.py",
            "diff": "--- a/src/auth.py\n+++ b/src/auth.py\n-old()\n+new()\n",
            "summary": "1 file changed",
        }
        handler = RouteHarness("/issues/iss_1/fixes/preview", cookie=self.signed_in())

        with patch("pullwise_server.app.preview_issue_fix_for_user", return_value=preview) as preview_fix:
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertTrue(handler.payload["valid"])
        self.assertIn("-old()", handler.payload["diff"])
        self.assertNotIn("originalContent", handler.payload)
        self.assertNotIn("updatedContent", handler.payload)
        preview_fix.assert_called_once_with(app.USERS["usr_1"], app.ISSUES[0])

    def test_issue_fix_preview_maps_helper_value_error_to_bad_request(self) -> None:
        handler = RouteHarness("/issues/iss_1/fixes/preview", cookie=self.signed_in())

        with patch("pullwise_server.app.preview_issue_fix_for_user", side_effect=ValueError("No checkout")):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload["message"], "No checkout")

    def test_issue_fix_preview_returns_bad_request_for_invalid_preview(self) -> None:
        preview = {
            "issueId": "iss_1",
            "autoFixable": True,
            "valid": False,
            "message": "Old block was not found.",
        }
        handler = RouteHarness("/issues/iss_1/fixes/preview", cookie=self.signed_in())

        with patch("pullwise_server.app.preview_issue_fix_for_user", return_value=preview):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload, preview)

    def test_preview_issue_fix_for_user_prepares_checkout_after_worker_cleanup(self) -> None:
        app.ISSUES[0].update({
            "repo": "owner/repo",
            "scanId": "sc_1",
            "autoFix": True,
            "file": "src/auth.py",
            "badCode": [{"ln": 1, "code": "old()", "t": "del"}],
            "goodCode": [{"ln": 1, "code": "new()", "t": "add"}],
        })
        app.SCANS[0].update({
            "branch": "main",
            "commit": "abc1234",
            "repoPath": None,
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                repo_path = app.checkout.checkout_path_for("usr_1", "sc_1", "owner/repo")
                os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
                with open(os.path.join(repo_path, "src", "auth.py"), "w", encoding="utf-8") as handle:
                    handle.write("old()\n")

                with (
                    patch("pullwise_server.app.checkout.prepare_checkout", return_value=repo_path) as prepare_checkout,
                    patch("pullwise_server.app.checkout.cleanup_scan_workspace") as cleanup_scan_workspace,
                ):
                    preview = app.preview_issue_fix_for_user(app.USERS["usr_1"], app.ISSUES[0])

        self.assertTrue(preview["valid"])
        self.assertIn("-old()", preview["diff"])
        self.assertIn("+new()", preview["diff"])
        prepare_checkout.assert_called_once()
        cleanup_scan_workspace.assert_called_once_with("usr_1", "sc_1")
        self.assertIsNone(app.SCANS[0]["repoPath"])
        self.assertNotIn("sc_1", app.PREVIEW_SCAN_LOCKS)

    def test_preview_issue_fix_for_user_reuses_existing_workspace_repo_path(self) -> None:
        app.ISSUES[0].update({
            "repo": "owner/repo",
            "scanId": "sc_1",
            "autoFix": True,
            "file": "src/auth.py",
            "badCode": [{"ln": 1, "code": "old()", "t": "del"}],
            "goodCode": [{"ln": 1, "code": "new()", "t": "add"}],
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                workspace = app.checkout.workspace_path_for("usr_1", "sc_1")
                repo_path = os.path.join(workspace, "owner_repo")
                os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
                with open(os.path.join(repo_path, "src", "auth.py"), "w", encoding="utf-8") as handle:
                    handle.write("old()\n")
                app.SCANS[0]["repoPath"] = repo_path

                with (
                    patch("pullwise_server.app.checkout.prepare_checkout", side_effect=AssertionError("prepare_checkout should not run")),
                    patch("pullwise_server.app.checkout.cleanup_scan_workspace", side_effect=AssertionError("cleanup should not run")),
                ):
                    preview = app.preview_issue_fix_for_user(app.USERS["usr_1"], app.ISSUES[0])

        self.assertTrue(preview["valid"])
        self.assertIn("-old()", preview["diff"])
        self.assertIn("+new()", preview["diff"])

    def test_preview_issue_fix_for_user_refreshes_stale_workspace_repo_path(self) -> None:
        app.ISSUES[0].update({
            "repo": "owner/repo",
            "scanId": "sc_1",
            "autoFix": True,
            "file": "src/auth.py",
            "badCode": [{"ln": 1, "code": "old()", "t": "del"}],
            "goodCode": [{"ln": 1, "code": "new()", "t": "add"}],
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                workspace = app.checkout.workspace_path_for("usr_1", "sc_1")
                stale_path = os.path.join(workspace, "stale_repo")
                fresh_path = os.path.join(workspace, "fresh_repo")
                os.makedirs(os.path.join(fresh_path, "src"), exist_ok=True)
                with open(os.path.join(fresh_path, "src", "auth.py"), "w", encoding="utf-8") as handle:
                    handle.write("old()\n")
                app.SCANS[0]["repoPath"] = stale_path

                with (
                    patch("pullwise_server.app.checkout.prepare_checkout", return_value=fresh_path) as prepare_checkout,
                    patch("pullwise_server.app.checkout.cleanup_scan_workspace") as cleanup_scan_workspace,
                ):
                    preview = app.preview_issue_fix_for_user(app.USERS["usr_1"], app.ISSUES[0])

        self.assertTrue(preview["valid"])
        self.assertIn("-old()", preview["diff"])
        prepare_checkout.assert_called_once()
        cleanup_scan_workspace.assert_called_once_with("usr_1", "sc_1")

    def test_preview_issue_fix_for_user_cleans_up_after_checkout_prepare_failure(self) -> None:
        app.ISSUES[0].update({
            "repo": "owner/repo",
            "scanId": "sc_1",
            "autoFix": True,
            "file": "src/auth.py",
            "badCode": [{"ln": 1, "code": "old()", "t": "del"}],
            "goodCode": [{"ln": 1, "code": "new()", "t": "add"}],
        })
        app.SCANS[0]["repoPath"] = None

        with (
            patch("pullwise_server.app.checkout.prepare_checkout", side_effect=RuntimeError("clone failed")),
            patch("pullwise_server.app.checkout.cleanup_scan_workspace") as cleanup_scan_workspace,
        ):
            with self.assertRaises(ValueError) as context:
                app.preview_issue_fix_for_user(app.USERS["usr_1"], app.ISSUES[0])

        self.assertIn("clone failed", str(context.exception))
        cleanup_scan_workspace.assert_called_once_with("usr_1", "sc_1")

    def test_preview_issue_fix_for_user_rejects_non_completed_scan(self) -> None:
        app.ISSUES[0].update({
            "repo": "owner/repo",
            "scanId": "sc_1",
            "autoFix": True,
            "file": "src/auth.py",
            "badCode": [{"ln": 1, "code": "old()", "t": "del"}],
            "goodCode": [{"ln": 1, "code": "new()", "t": "add"}],
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                repo_path = app.checkout.checkout_path_for("usr_1", "sc_1", "owner/repo")
                os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
                app.SCANS[0].update({
                    "status": "running",
                    "repoPath": repo_path,
                })

                with (
                    patch("pullwise_server.app.checkout.prepare_checkout", side_effect=AssertionError("prepare_checkout should not run")),
                    patch("pullwise_server.app.checkout.cleanup_scan_workspace", side_effect=AssertionError("cleanup should not run")),
                    patch("pullwise_server.app.fix_workflow.preview_issue_fix", side_effect=AssertionError("preview should not run")),
                ):
                    with self.assertRaises(ValueError) as context:
                        app.preview_issue_fix_for_user(app.USERS["usr_1"], app.ISSUES[0])

        self.assertIn("completed", str(context.exception))

    def test_preview_issue_fix_for_user_serializes_same_scan_checkout_previews(self) -> None:
        app.ISSUES[0].update({
            "repo": "owner/repo",
            "scanId": "sc_1",
            "autoFix": True,
            "file": "src/auth.py",
            "badCode": [{"ln": 1, "code": "old()", "t": "del"}],
            "goodCode": [{"ln": 1, "code": "new()", "t": "add"}],
        })
        app.SCANS[0].update({
            "status": "done",
            "repoPath": None,
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                repo_path = app.checkout.checkout_path_for("usr_1", "sc_1", "owner/repo")
                os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
                with open(os.path.join(repo_path, "src", "auth.py"), "w", encoding="utf-8") as handle:
                    handle.write("old()\n")

                active_prepares = 0
                max_active_prepares = 0
                prepare_calls = 0
                counter_lock = threading.Lock()
                first_prepare_entered = threading.Event()
                release_first_prepare = threading.Event()

                def prepare_checkout(_scan_id, _scan, _is_cancelled):
                    nonlocal active_prepares, max_active_prepares, prepare_calls
                    with counter_lock:
                        active_prepares += 1
                        prepare_calls += 1
                        max_active_prepares = max(max_active_prepares, active_prepares)
                        is_first_prepare = prepare_calls == 1
                    if is_first_prepare:
                        first_prepare_entered.set()
                        release_first_prepare.wait(1)
                    else:
                        time.sleep(0.01)
                    with counter_lock:
                        active_prepares -= 1
                    return repo_path

                previews = []
                errors = []

                def run_preview():
                    try:
                        previews.append(app.preview_issue_fix_for_user(app.USERS["usr_1"], app.ISSUES[0]))
                    except Exception as exc:
                        errors.append(exc)

                with (
                    patch("pullwise_server.app.checkout.prepare_checkout", side_effect=prepare_checkout),
                    patch("pullwise_server.app.checkout.cleanup_scan_workspace") as cleanup_scan_workspace,
                ):
                    first = threading.Thread(target=run_preview)
                    second = threading.Thread(target=run_preview)
                    first.start()
                    second.start()
                    self.assertTrue(first_prepare_entered.wait(1))
                    time.sleep(0.05)
                    release_first_prepare.set()
                    first.join()
                    second.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(previews), 2)
        self.assertTrue(all(preview["valid"] for preview in previews))
        self.assertEqual(prepare_calls, 2)
        self.assertEqual(cleanup_scan_workspace.call_count, 2)
        self.assertEqual(max_active_prepares, 1)

    def test_preview_issue_fix_for_user_rejects_scan_owned_by_another_user(self) -> None:
        app.ISSUES[0]["scanId"] = "sc_2"
        app.SCANS.append({
            "id": "sc_2",
            "userId": "usr_2",
            "status": "done",
            "repo": "owner/repo",
            "repoPath": None,
        })

        with self.assertRaises(ValueError) as context:
            app.preview_issue_fix_for_user(app.USERS["usr_1"], app.ISSUES[0])

        self.assertIn("signed-in user", str(context.exception))

    def test_preview_issue_fix_for_user_rejects_repo_path_outside_scan_workspace(self) -> None:
        app.ISSUES[0]["scanId"] = "sc_1"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": os.path.join(tmpdir, "checkouts")}, clear=False):
                outside_path = os.path.join(tmpdir, "outside", "repo")
                app.SCANS[0]["repoPath"] = outside_path

                with self.assertRaises(ValueError) as context:
                    app.preview_issue_fix_for_user(app.USERS["usr_1"], app.ISSUES[0])

        self.assertIn("outside the scan workspace", str(context.exception))

    def test_issue_status_update_rejects_unknown_status(self) -> None:
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness("/issues/iss_1/status", {"status": "archived"}, cookie="pw_session=ses_1")

        app.PullwiseHandler.route(handler, "PATCH")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(app.ISSUES[0]["status"], "open")

    def test_issue_status_update_normalizes_status_text(self) -> None:
        handler = RouteHarness("/issues/iss_1/status", {"status": " Fixed "}, cookie=self.signed_in())

        app.PullwiseHandler.route(handler, "PATCH")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(app.ISSUES[0]["status"], "fixed")

    def test_issue_status_update_rejects_non_object_body(self) -> None:
        handler = RouteHarness("/issues/iss_1/status", ["fixed"], cookie=self.signed_in())

        app.PullwiseHandler.route(handler, "PATCH")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(app.ISSUES[0]["status"], "open")

    def test_github_installation_html_url_must_match_configured_github_host(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_GITHUB_WEB_URL": "https://github.com"}, clear=False):
            self.assertEqual(
                app.trusted_github_web_url("https://github.com/settings/installations/123"),
                "https://github.com/settings/installations/123",
            )
            self.assertIsNone(app.trusted_github_web_url("javascript:alert(1)"))
            self.assertIsNone(app.trusted_github_web_url("https://evil.example/settings/installations/123"))

    def test_github_installation_html_url_rejects_crlf_values(self) -> None:
        unsafe_url = "https://github.com/settings/installations/123\r\nX-Pullwise-Test: bad"

        with patch.dict(os.environ, {"PULLWISE_GITHUB_WEB_URL": "https://github.com"}, clear=False):
            self.assertIsNone(app.trusted_github_web_url(unsafe_url))
            summary = app.installation_summary_from_access({
                "installationId": "123",
                "installationHtmlUrl": unsafe_url,
            })

        self.assertIsNone(summary["installationHtmlUrl"])

    def test_installation_summary_drops_untrusted_html_url(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_GITHUB_WEB_URL": "https://github.com"}, clear=False):
            summary = app.installation_summary_from_access({
                "installationId": "123",
                "installationHtmlUrl": "javascript:alert(1)",
            })

        self.assertIsNone(summary["installationHtmlUrl"])

    def test_installation_summary_sanitizes_malformed_metadata(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_GITHUB_WEB_URL": "https://github.com"}, clear=False):
            summary = app.installation_summary_from_access({
                "installationId": {"id": "123"},
                "installationAccount": {"login": "octocat"},
                "installationTargetType": ["User"],
                "installationAppSlug": {"slug": "pullwise"},
                "installationHtmlUrl": "https://github.com/settings/installations/123",
                "repositorySelection": "selected\r\nX-Test: bad",
                "scope": {"scope": "selected"},
                "repositories": {"octocat/repo": True},
                "repositoriesNeedSync": "false",
            })

        self.assertIsNone(summary["installationId"])
        self.assertIsNone(summary["installationAccount"])
        self.assertIsNone(summary["installationTargetType"])
        self.assertIsNone(summary["installationAppSlug"])
        self.assertEqual(summary["installationHtmlUrl"], "https://github.com/settings/installations/123")
        self.assertIsNone(summary["repositorySelection"])
        self.assertIsNone(summary["scope"])
        self.assertEqual(summary["repositoryCount"], 0)
        self.assertFalse(summary["repositoriesNeedSync"])

    def test_safe_installation_summaries_sanitize_legacy_url_aliases(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_GITHUB_WEB_URL": "https://github.com"}, clear=False):
            summaries = app.safe_installation_summaries([
                {"installationId": "123", "htmlUrl": "javascript:alert(1)", "html_url": "https://evil.example/install"}
            ])

        self.assertIsNone(summaries[0]["installationHtmlUrl"])
        self.assertIsNone(summaries[0]["htmlUrl"])
        self.assertIsNone(summaries[0]["html_url"])

    def test_safe_installation_summaries_sanitize_malformed_metadata(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_GITHUB_WEB_URL": "https://github.com"}, clear=False):
            summaries = app.safe_installation_summaries([
                {
                    "installationId": {"id": "123"},
                    "installationAccount": ["octocat"],
                    "installationTargetType": {"type": "User"},
                    "installationAppSlug": {"slug": "pullwise"},
                    "installationHtmlUrl": "https://github.com/settings/installations/123",
                    "repositorySelection": "selected\r\nX-Test: bad",
                    "scope": {"scope": "selected"},
                    "repositoryCount": -4,
                    "repositoriesNeedSync": "false",
                    "raw": {"unexpected": "value"},
                }
            ])

        self.assertEqual(summaries, [
            {
                "installationId": None,
                "installationAccount": None,
                "installationTargetType": None,
                "installationAppSlug": None,
                "installationHtmlUrl": "https://github.com/settings/installations/123",
                "htmlUrl": "https://github.com/settings/installations/123",
                "html_url": "https://github.com/settings/installations/123",
                "repositorySelection": None,
                "scope": None,
                "repositoryCount": 0,
                "repositoriesNeedSync": False,
            }
        ])

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

    def test_scan_creation_rejects_non_object_body(self) -> None:
        initial_scan_count = len(app.SCANS)
        handler = RouteHarness("/scans", ["owner/repo"], cookie=self.signed_in())

        with patch.object(app.worker, "start_scan") as start_scan:
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload["message"], "Request body must be a JSON object.")
        self.assertEqual(len(app.SCANS), initial_scan_count)
        start_scan.assert_not_called()

    def test_scan_creation_uses_repository_item_installation_id(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "scope": "mixed",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
            "installationIds": ["111", "222"],
            "installationAccounts": ["octocat", "acme"],
            "repositories": ["octocat/private-repo", "acme/service"],
            "repositoryItems": [
                {
                    "id": "repo_private",
                    "name": "private-repo",
                    "fullName": "octocat/private-repo",
                    "installationId": "111",
                    "installationAccount": "octocat",
                    "defaultBranch": "main",
                    "cloneUrl": "https://github.com/octocat/private-repo.git",
                },
                {
                    "id": "repo_service",
                    "name": "service",
                    "fullName": "acme/service",
                    "installationId": "222",
                    "installationAccount": "acme",
                    "defaultBranch": "develop",
                    "cloneUrl": "https://github.com/acme/service.git",
                },
            ],
            "installations": [
                {"installationId": "111", "installationAccount": "octocat"},
                {"installationId": "222", "installationAccount": "acme"},
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
        handler = RouteHarness("/scans", {"repo": "acme/service"}, cookie="pw_session=ses_1")

        with (
            patch.dict(os.environ, {"PULLWISE_REVIEW_PROVIDER": "mock"}, clear=True),
            patch.object(app.worker, "start_scan") as start_scan,
        ):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.CREATED)
        self.assertEqual(handler.payload["installationId"], "222")
        self.assertEqual(handler.payload["installationAccount"], "acme")
        self.assertEqual(handler.payload["branch"], "develop")
        self.assertEqual(handler.payload["cloneUrl"], "https://github.com/acme/service.git")
        start_scan.assert_called_once()

    def test_scan_creation_is_idempotent_for_repeated_request_id(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "scope": "selected",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
            "installationId": "111",
            "repositories": ["owner/repo"],
            "repositoryItems": [
                {
                    "id": "repo_1",
                    "name": "repo",
                    "fullName": "owner/repo",
                    "installationId": "111",
                    "defaultBranch": "main",
                    "cloneUrl": "https://github.com/owner/repo.git",
                },
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

        first = RouteHarness(
            "/scans",
            {"repo": "owner/repo", "requestId": "scan_req_1"},
            cookie="pw_session=ses_1",
        )
        second = RouteHarness(
            "/scans",
            {"repo": "owner/repo", "requestId": "scan_req_1"},
            cookie="pw_session=ses_1",
        )

        with (
            patch.dict(os.environ, {"PULLWISE_REVIEW_PROVIDER": "mock"}, clear=True),
            patch.object(app.worker, "start_scan") as start_scan,
        ):
            app.PullwiseHandler.route(first, "POST")
            app.PullwiseHandler.route(second, "POST")

        self.assertEqual(first.status, HTTPStatus.CREATED)
        self.assertEqual(second.status, HTTPStatus.OK)
        self.assertEqual(first.payload["id"], second.payload["id"])
        self.assertEqual(len([scan for scan in app.SCANS if scan.get("requestId") == "scan_req_1"]), 1)
        start_scan.assert_called_once_with(first.payload["id"])

    def test_repositories_payload_treats_string_false_need_sync_as_connected(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "scope": "selected",
            "repositorySelection": "selected",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
            "installationId": "111",
            "installationIds": ["111"],
            "installationAccount": "octocat",
            "installationAccounts": ["octocat"],
            "repositories": ["octocat/private-repo"],
            "repositoryItems": [
                {
                    "id": "repo_private",
                    "name": "private-repo",
                    "fullName": "octocat/private-repo",
                    "installationId": "111",
                    "installationAccount": "octocat",
                    "defaultBranch": "main",
                    "cloneUrl": "https://github.com/octocat/private-repo.git",
                }
            ],
            "repositoriesNeedSync": "false",
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
        self.assertFalse(handler.payload["repositoriesNeedSync"])
        self.assertEqual([item["fullName"] for item in handler.payload["items"]], ["octocat/private-repo"])

    def test_scan_creation_rejects_repository_access_that_needs_sync_even_with_stale_repo_names(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "scope": "selected",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
            "installationId": "111",
            "installationAccount": "octocat",
            "installationTargetType": "User",
            "repositories": ["octocat/stale-repo"],
            "repositoryItems": [
                {
                    "id": "repo_stale",
                    "name": "stale-repo",
                    "fullName": "octocat/stale-repo",
                    "installationId": "111",
                    "installationAccount": "octocat",
                }
            ],
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
        initial_scan_count = len(app.SCANS)
        handler = RouteHarness("/scans", {"repo": "octocat/stale-repo"}, cookie="pw_session=ses_1")

        with (
            patch.dict(os.environ, {"PULLWISE_REVIEW_PROVIDER": "mock"}, clear=True),
            patch.object(app.worker, "start_scan") as start_scan,
        ):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.FORBIDDEN)
        self.assertIn("Sync GitHub repositories", handler.payload["message"])
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

    def test_auth_session_does_not_report_stale_personal_installation_connected_for_current_github_login(self) -> None:
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
        handler = RouteHarness("/auth/session", cookie="pw_session=ses_1")

        app.PullwiseHandler.route(handler, "GET")

        self.assertTrue(handler.payload["authenticated"])
        self.assertFalse(handler.payload["github"]["repositoriesConnected"])
        self.assertEqual(handler.payload["nextStep"], "connect_github_repositories")

    def test_integrations_do_not_expose_stale_personal_installation_for_current_github_login(self) -> None:
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
        handler = RouteHarness("/integrations", cookie="pw_session=ses_1")

        app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertFalse(handler.payload["github"]["connected"])
        self.assertIsNone(handler.payload["github"]["installationAccount"])
        self.assertEqual(handler.payload["github"]["repositories"], [])

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

    def test_github_repository_authorize_requires_pullwise_github_oauth_identity(self) -> None:
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

        self.assertEqual(handler.status, HTTPStatus.UNAUTHORIZED)
        self.assertIn("Sign in with GitHub", handler.payload["message"])
        self.assertEqual(app.GITHUB_STATES, {})

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
                    "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
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

    def test_github_repository_authorize_returns_configure_url_for_managing_verified_existing_installation(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "installationId": "999",
            "installationHtmlUrl": "https://github.com/settings/installations/999",
            "installationAccount": "octocat",
            "installationTargetType": "User",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
            "repositories": ["octocat/private-repo"],
            "repositoryItems": [
                {
                    "id": "repo_private",
                    "name": "private-repo",
                    "fullName": "octocat/private-repo",
                    "private": True,
                    "cloneUrl": "https://github.com/octocat/private-repo.git",
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
        self.assertEqual(handler.payload["mode"], "github-app-existing-manage")
        self.assertEqual(handler.payload["url"], "https://github.com/settings/installations/999")
        self.assertEqual(app.GITHUB_STATES, {})

    def test_github_repository_authorize_manage_lists_existing_aggregate_installations(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "scope": "mixed",
            "repositorySelection": "selected",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
            "installationId": None,
            "installationIds": ["111", "222"],
            "installationAccounts": ["octocat", "acme"],
            "repositories": ["octocat/private-repo", "acme/service"],
            "repositoryItems": [
                {"fullName": "octocat/private-repo", "installationId": "111"},
                {"fullName": "acme/service", "installationId": "222"},
            ],
            "installations": [
                {
                    "installationId": "111",
                    "installationAccount": "octocat",
                    "installationHtmlUrl": "https://github.com/settings/installations/111",
                    "repositorySelection": "selected",
                    "repositoryCount": 1,
                },
                {
                    "installationId": "222",
                    "installationAccount": "acme",
                    "installationHtmlUrl": "https://github.com/organizations/acme/settings/installations/222",
                    "repositorySelection": "all",
                    "repositoryCount": 1,
                },
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
        self.assertTrue(handler.payload["connected"])
        self.assertEqual(handler.payload["mode"], "github-app-existing-manage-list")
        self.assertNotIn("url", handler.payload)
        self.assertEqual(
            [installation["installationAccount"] for installation in handler.payload["installations"]],
            ["octocat", "acme"],
        )
        self.assertEqual(app.GITHUB_STATES, {})

    def test_github_repository_authorize_add_returns_install_url_for_existing_aggregate_installations(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "scope": "mixed",
            "repositorySelection": "selected",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
            "installationIds": ["111", "222"],
            "installationAccounts": ["octocat", "acme"],
            "repositories": ["octocat/private-repo", "acme/service"],
            "repositoryItems": [
                {"fullName": "octocat/private-repo", "installationId": "111"},
                {"fullName": "acme/service", "installationId": "222"},
            ],
            "installations": [
                {
                    "installationId": "111",
                    "installationAccount": "octocat",
                    "installationHtmlUrl": "https://github.com/settings/installations/111",
                    "repositorySelection": "selected",
                    "repositoryCount": 1,
                },
                {
                    "installationId": "222",
                    "installationAccount": "acme",
                    "installationHtmlUrl": "https://github.com/organizations/acme/settings/installations/222",
                    "repositorySelection": "all",
                    "repositoryCount": 1,
                },
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
            "/integrations/github/authorize?add=1&redirectTo=https%3A%2F%2Fapp.pullwise.dev%2F%3Fscreen%3Drepos",
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
        self.assertEqual(handler.payload["mode"], "github-app-add")
        self.assertIn("https://github.com/apps/pullwise/installations/new?state=", handler.payload["url"])
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
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
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

    def test_github_repository_authorize_repairs_stale_personal_installation_before_opening_install_url(self) -> None:
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
                        "id": 111,
                        "repository_selection": "selected",
                        "target_type": "User",
                        "account": {"login": "SanChai20"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
                    },
                    {
                        "id": 222,
                        "repository_selection": "selected",
                        "target_type": "User",
                        "account": {"login": "DFerryman"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
                    },
                ],
            ),
            patch(
                "pullwise_server.github_auth.list_user_installation_repositories",
                return_value=[
                    {
                        "id": "repo_private",
                        "name": "private-repo",
                        "fullName": "DFerryman/private-repo",
                        "private": True,
                        "cloneUrl": "https://github.com/DFerryman/private-repo.git",
                    }
                ],
            ) as list_user_repositories,
        ):
            app.PullwiseHandler.route(handler, "GET")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertTrue(handler.payload["connected"])
        self.assertNotIn("url", handler.payload)
        self.assertEqual(github_access["installationId"], "222")
        self.assertEqual(github_access["installationAccount"], "DFerryman")
        self.assertEqual(github_access["repositories"], ["DFerryman/private-repo"])
        self.assertEqual([call.args for call in list_user_repositories.call_args_list], [("gho_user", "222")])

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
                    "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
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
            "installationAccount": "octocat",
            "installationTargetType": "User",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
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
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
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

    def test_repositories_sync_aggregates_all_accessible_github_app_installations(self) -> None:
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

        def installation_repositories(_token: str, installation_id: str) -> list[dict]:
            if installation_id == "111":
                return [
                    {
                        "id": "repo_private",
                        "name": "private-repo",
                        "fullName": "octocat/private-repo",
                        "private": True,
                        "cloneUrl": "https://github.com/octocat/private-repo.git",
                    }
                ]
            if installation_id == "222":
                return [
                    {
                        "id": "repo_service",
                        "name": "service",
                        "fullName": "acme/service",
                        "private": True,
                        "cloneUrl": "https://github.com/acme/service.git",
                    }
                ]
            return []

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
                        "repository_selection": "selected",
                        "target_type": "User",
                        "account": {"login": "octocat"},
                        "app_slug": "pullwise",
                        "html_url": "https://github.com/settings/installations/111",
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
                    },
                    {
                        "id": 222,
                        "repository_selection": "selected",
                        "target_type": "Organization",
                        "account": {"login": "acme"},
                        "app_slug": "pullwise",
                        "html_url": "https://github.com/organizations/acme/settings/installations/222",
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
                    },
                ],
            ),
            patch(
                "pullwise_server.github_auth.list_user_installation_repositories",
                side_effect=installation_repositories,
            ) as list_user_repositories,
            patch("pullwise_server.github_auth.fetch_installation") as fetch_installation,
            patch("pullwise_server.github_auth.list_installation_repositories") as list_repositories,
        ):
            app.PullwiseHandler.route(handler, "POST")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertFalse(handler.payload["needsAuthorization"])
        self.assertEqual(
            [item["fullName"] for item in handler.payload["items"]],
            ["octocat/private-repo", "acme/service"],
        )
        self.assertEqual(handler.payload["items"][0]["installationId"], "111")
        self.assertEqual(handler.payload["items"][1]["installationId"], "222")
        self.assertEqual(github_access["installationIds"], ["111", "222"])
        self.assertEqual(github_access["installationAccounts"], ["octocat", "acme"])
        self.assertEqual(len(github_access["installations"]), 2)
        self.assertEqual(
            [call.args for call in list_user_repositories.call_args_list],
            [("gho_user", "111"), ("gho_user", "222")],
        )
        fetch_installation.assert_not_called()
        list_repositories.assert_not_called()

    def test_repositories_sync_refreshes_existing_aggregate_installations(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "scope": "mixed",
            "repositorySelection": "selected",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
            "installationId": None,
            "installationIds": ["111", "222"],
            "installationAccounts": ["octocat", "acme"],
            "repositories": ["octocat/old-repo", "acme/old-service"],
            "repositoryItems": [
                {"fullName": "octocat/old-repo", "installationId": "111"},
                {"fullName": "acme/old-service", "installationId": "222"},
            ],
            "installations": [
                {"installationId": "111", "installationAccount": "octocat"},
                {"installationId": "222", "installationAccount": "acme"},
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

        def installation_repositories(_token: str, installation_id: str) -> list[dict]:
            if installation_id == "111":
                return [{"id": "repo_private", "name": "private-repo", "fullName": "octocat/private-repo"}]
            if installation_id == "222":
                return [{"id": "repo_service", "name": "service", "fullName": "acme/service"}]
            self.fail(f"unexpected installation id: {installation_id}")

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
                        "repository_selection": "selected",
                        "target_type": "User",
                        "account": {"login": "octocat"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
                    },
                    {
                        "id": 222,
                        "repository_selection": "selected",
                        "target_type": "Organization",
                        "account": {"login": "acme"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
                    },
                ],
            ) as list_installations,
            patch(
                "pullwise_server.github_auth.list_user_installation_repositories",
                side_effect=installation_repositories,
            ) as list_user_repositories,
        ):
            app.PullwiseHandler.route(handler, "POST")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual([item["fullName"] for item in handler.payload["items"]], ["octocat/private-repo", "acme/service"])
        self.assertEqual(github_access["repositories"], ["octocat/private-repo", "acme/service"])
        list_installations.assert_called_once_with("gho_user")
        self.assertEqual(
            [call.args for call in list_user_repositories.call_args_list],
            [("gho_user", "111"), ("gho_user", "222")],
        )

    def test_repositories_list_migrates_legacy_single_installation_access_to_aggregate_model(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "installationId": "111",
            "installationAccount": "octocat",
            "installationTargetType": "User",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
            "repositories": ["octocat/private-repo"],
            "repositoryItems": [
                {
                    "id": "repo_private",
                    "name": "private-repo",
                    "fullName": "octocat/private-repo",
                    "installationId": "111",
                    "installationAccount": "octocat",
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
        handler = RouteHarness("/repositories", cookie="pw_session=ses_1")

        def installation_repositories(_token: str, installation_id: str) -> list[dict]:
            if installation_id == "111":
                return [{"id": "repo_private", "name": "private-repo", "fullName": "octocat/private-repo"}]
            if installation_id == "222":
                return [{"id": "repo_service", "name": "service", "fullName": "acme/service"}]
            return []

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
                        "repository_selection": "selected",
                        "target_type": "User",
                        "account": {"login": "octocat"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
                    },
                    {
                        "id": 222,
                        "repository_selection": "selected",
                        "target_type": "Organization",
                        "account": {"login": "acme"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
                    },
                ],
            ),
            patch("pullwise_server.github_auth.list_user_installation_repositories", side_effect=installation_repositories),
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertFalse(handler.payload["needsAuthorization"])
        self.assertEqual([item["fullName"] for item in handler.payload["items"]], ["octocat/private-repo", "acme/service"])
        self.assertEqual(app.USERS["usr_1"]["githubRepositoryAccess"]["installationIds"], ["111", "222"])

    def test_repositories_list_repairs_stale_personal_installation_for_current_github_login(self) -> None:
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
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
                    },
                    {
                        "id": 222,
                        "repository_selection": "all",
                        "target_type": "User",
                        "account": {"login": "DFerryman"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
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
            app.PullwiseHandler.route(handler, "GET")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertFalse(handler.payload["needsAuthorization"])
        self.assertEqual(handler.payload["installationAccount"], "DFerryman")
        self.assertEqual(handler.payload["items"][0]["fullName"], "DFerryman/private-repo")
        self.assertNotIn("SanChai20/private-repo", github_access["repositories"])

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
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
                    },
                    {
                        "id": 222,
                        "repository_selection": "all",
                        "target_type": "User",
                        "account": {"login": "DFerryman"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
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
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
                    },
                    {
                        "id": 222,
                        "repository_selection": "all",
                        "target_type": "User",
                        "account": {"login": "DFerryman"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
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

    def test_github_repository_installation_permissions_must_support_pull_request_creation(self) -> None:
        self.assertTrue(
            app.installation_supports_pull_request_creation(
                {"permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"}}
            )
        )
        self.assertFalse(
            app.installation_supports_pull_request_creation(
                {"permissions": {"metadata": "read", "contents": "read", "pull_requests": "write"}}
            )
        )
        self.assertFalse(
            app.installation_supports_pull_request_creation(
                {"permissions": {"metadata": "read", "contents": "write", "pull_requests": "read"}}
            )
        )
        self.assertFalse(app.installation_supports_pull_request_creation({"permissions": {"metadata": "read"}}))

    def test_repository_item_with_installation_context_preserves_installation_permissions(self) -> None:
        item = app.repository_item_with_installation_context(
            {"fullName": "owner/repo"},
            {
                "installationId": "123",
                "installationPermissions": {
                    "metadata": "read",
                    "contents": "write",
                    "pull_requests": "write",
                },
            },
        )

        self.assertEqual(item["installationId"], "123")
        self.assertEqual(item["installationPermissions"]["contents"], "write")
        self.assertEqual(item["installationPermissions"]["pull_requests"], "write")

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

    def test_github_installation_callback_with_state_requires_pullwise_github_oauth_identity(self) -> None:
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
                    "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
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
        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("Sign in with GitHub", handler.payload["message"])
        self.assertIsNone(github_access)
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
            patch(
                "pullwise_server.github_auth.list_current_app_installations_for_user",
                return_value=[
                    {
                        "id": 999,
                        "repository_selection": "selected",
                        "target_type": "User",
                        "account": {"login": "octocat"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
                    }
                ],
            ),
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
            patch(
                "pullwise_server.github_auth.list_current_app_installations_for_user",
                return_value=[
                    {
                        "id": 999,
                        "repository_selection": "selected",
                        "target_type": "User",
                        "account": {"login": "octocat"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
                    }
                ],
            ),
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
                "pullwise_server.github_auth.list_current_app_installations_for_user",
                return_value=[
                    {
                        "id": 999,
                        "repository_selection": "selected",
                        "target_type": "User",
                        "account": {"login": "octocat"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
                    }
                ],
            ),
            patch(
                "pullwise_server.github_auth.fetch_installation",
                return_value={
                    "repository_selection": "selected",
                    "target_type": "User",
                    "account": {"login": "octocat"},
                    "app_slug": "pullwise",
                    "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
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
        self.assertEqual(github_access["installationPermissions"]["contents"], "write")
        self.assertEqual(github_access["installationPermissions"]["pull_requests"], "write")
        self.assertEqual(github_access["repositories"], ["octocat/private-repo"])
        self.assertTrue(github_access["repositoryItems"][0]["private"])

    def test_github_installation_callback_rejects_installation_without_pull_request_write_permissions(self) -> None:
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
                "pullwise_server.github_auth.list_current_app_installations_for_user",
                return_value=[
                    {
                        "id": 999,
                        "repository_selection": "selected",
                        "target_type": "User",
                        "account": {"login": "octocat"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "read", "pull_requests": "write"},
                    }
                ],
            ),
            patch(
                "pullwise_server.github_auth.fetch_installation",
                return_value={
                    "repository_selection": "selected",
                    "target_type": "User",
                    "account": {"login": "octocat"},
                    "app_slug": "pullwise",
                    "permissions": {"metadata": "read", "contents": "read", "pull_requests": "write"},
                },
            ),
            patch("pullwise_server.github_auth.list_installation_repositories") as list_repositories,
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("Contents: write", handler.payload["message"])
        self.assertIn("Pull requests: write", handler.payload["message"])
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
            self.assertEqual(app.api_base_url(handler), "http://localhost:8080")

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

    def test_request_body_rejects_negative_content_length(self) -> None:
        handler = RouteHarness(
            "/auth/sign-out",
            headers={"Content-Length": "-1"},
        )

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload["message"], "Invalid Content-Length header.")

    def test_request_body_rejects_malformed_content_length(self) -> None:
        handler = RouteHarness(
            "/auth/sign-out",
            headers={"Content-Length": "not-a-number"},
        )

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload["message"], "Invalid Content-Length header.")

    def test_request_body_rejects_malformed_json_without_parser_details(self) -> None:
        handler = RawBodyRouteHarness(
            "/auth/sign-out",
            headers={"Content-Length": "1"},
            raw_body=b"{",
        )

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload["message"], "Request body must be valid JSON.")

    def test_request_body_rejects_non_utf8_json_without_decoder_details(self) -> None:
        handler = RawBodyRouteHarness(
            "/auth/sign-out",
            headers={"Content-Length": "1"},
            raw_body=b"\xff",
        )

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload["message"], "Request body must be valid JSON.")


if __name__ == "__main__":
    unittest.main()
