from __future__ import annotations

import json
import os
import tempfile
import unittest
from http import HTTPStatus
from unittest.mock import Mock, patch

from pullwise_server import app, checkout, github_auth


class RouteHarness(app.PullwiseHandler):
    def __init__(self, path: str, body: dict | None = None, cookie: str = "") -> None:
        self.path = path
        self._body = body or {}
        self.headers = {"Host": "api.pullwise.dev", "Cookie": cookie}
        self.payload = None
        self.status = None
        self.headers_out = {}

    def read_json(self) -> dict:
        return self._body

    def json(self, payload: dict, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.status = status
        self.headers_out = headers or {}

    def error(self, status: int, message: str) -> None:
        self.json({"message": message}, status)


class PullRequestWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.persist_patcher = patch.object(app, "persist_state")
        self.persist_patcher.start()
        self.addCleanup(self.persist_patcher.stop)
        app.USERS = {
            "usr_1": {
                "id": "usr_1",
                "name": "Dev",
                "email": "dev@example.com",
                "githubId": "1",
                "githubLogin": "octocat",
                "githubRepositoryAccess": {
                    "mode": "github-app",
                    "authorizedUserId": "usr_1",
                    "authorizedGithubId": "1",
                    "authorizedGithubLogin": "octocat",
                    "repositories": ["owner/repo"],
                    "repositoryItems": [
                        {
                            "fullName": "owner/repo",
                            "installationId": "123",
                            "defaultBranch": "main",
                            "cloneUrl": "https://github.com/owner/repo.git",
                        }
                    ],
                },
            }
        }
        app.SESSIONS = {}
        app.SCANS = [
            {
                "id": "sc_1",
                "userId": "usr_1",
                "repo": "owner/repo",
                "branch": "main",
                "status": "done",
            }
        ]
        app.ISSUES = [
            {
                "id": "f_123",
                "userId": "usr_1",
                "scanId": "sc_1",
                "repo": "owner/repo",
                "branch": "main",
                "title": "Validate redirect targets",
                "file": "src/auth.py",
                "autoFix": True,
                "badCode": [{"ln": 2, "code": "return redirect(next_url)", "t": "del"}],
                "goodCode": [{"ln": 2, "code": "return redirect(safe_redirect(next_url))", "t": "add"}],
            }
        ]
        app.STATE_LOADED = True
        app.STATE_DIRTY = False

    def signed_in(self) -> str:
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        return "pw_session=ses_1"

    def test_create_issue_pull_request_requires_github_app_api_configuration(self) -> None:
        with (
            patch("pullwise_server.app.github_auth.app_api_configured", return_value=False),
            patch("pullwise_server.app.checkout.prepare_checkout") as prepare_checkout,
        ):
            with self.assertRaisesRegex(ValueError, "GitHub App API"):
                app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        prepare_checkout.assert_not_called()

    def test_existing_pull_request_is_reused_idempotently(self) -> None:
        existing = {
            "issueId": "f_123",
            "branch": "pullwise/fix-f_123-existing",
            "url": "https://github.com/owner/repo/pull/7",
            "number": 7,
            "title": "Fix Validate redirect targets",
        }
        app.ISSUES[0]["pullRequest"] = existing

        with patch("pullwise_server.app.github_auth.app_api_configured", side_effect=AssertionError("config not needed")):
            pull_request = app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        self.assertIs(pull_request, existing)

    def test_successful_pull_request_creation_uses_backend_token_without_leaking_it(self) -> None:
        token = "ghs_secret_token"
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                repo_path = checkout.checkout_path_for("usr_1", "pr_f_123", "owner/repo")
                os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
                with open(os.path.join(repo_path, "src", "auth.py"), "w", encoding="utf-8") as handle:
                    handle.write("def redirect_target(next_url):\n    return redirect(next_url)\n")

                run_git_calls: list[tuple[list[str], dict]] = []

                def record_git(cmd: list[str], **kwargs: object) -> None:
                    run_git_calls.append((cmd, kwargs))

                with (
                    patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
                    patch("pullwise_server.app.checkout.prepare_checkout", return_value=repo_path) as prepare_checkout,
                    patch("pullwise_server.app.checkout.run_git", side_effect=record_git) as run_git,
                    patch("pullwise_server.app.github_auth.create_installation_access_token", return_value={"token": token}) as create_token,
                    patch(
                        "pullwise_server.app.github_auth.create_pull_request",
                        return_value={
                            "url": "https://github.com/owner/repo/pull/12",
                            "number": 12,
                            "title": "Fix Validate redirect targets",
                        },
                    ) as create_pull_request,
                    patch("pullwise_server.app.checkout.cleanup_scan_workspace") as cleanup,
                    patch("pullwise_server.app.make_id", return_value="fix_fixedtoken"),
                ):
                    pull_request = app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        prepare_checkout.assert_called_once()
        self.assertEqual(prepare_checkout.call_args.args[0], "pr_f_123")
        scan_payload = prepare_checkout.call_args.args[1]
        self.assertEqual(scan_payload["userId"], "usr_1")
        self.assertEqual(scan_payload["repo"], "owner/repo")
        self.assertEqual(scan_payload["branch"], "main")
        self.assertEqual(scan_payload["installationId"], "123")
        self.assertEqual(scan_payload["cloneUrl"], "https://github.com/owner/repo.git")
        create_token.assert_called_once_with("123")
        self.assertEqual(run_git.call_count, 4)
        commands = [call[0] for call in run_git_calls]
        self.assertEqual(commands[0][:3], ["git", "checkout", "-B"])
        self.assertEqual(commands[1], ["git", "add", "--", "src/auth.py"])
        self.assertEqual(commands[2][:3], ["git", "commit", "-m"])
        self.assertEqual(commands[3][:3], ["git", "push", "origin"])
        for command in commands:
            self.assertNotIn(token, " ".join(command))
        self.assertTrue(all(call[1]["cwd"] == repo_path for call in run_git_calls))
        self.assertTrue(all(call[1]["is_cancelled"]() is False for call in run_git_calls))
        self.assertTrue(all("Pullwise" in call[1]["extra_env"].get("GIT_AUTHOR_NAME", "") for call in run_git_calls))
        create_pull_request.assert_called_once()
        self.assertEqual(create_pull_request.call_args.args[:2], (token, "owner/repo"))
        self.assertEqual(create_pull_request.call_args.kwargs["head"], pull_request["branch"])
        self.assertEqual(create_pull_request.call_args.kwargs["base"], "main")
        cleanup.assert_called_once_with("usr_1", "pr_f_123")
        self.assertEqual(app.ISSUES[0]["pullRequest"], pull_request)
        self.assertTrue(app.STATE_DIRTY)
        self.assertNotIn(token, json.dumps(pull_request))
        self.assertNotIn(token, json.dumps(app.ISSUES[0]["pullRequest"]))

    def test_repository_authorization_failure_is_rejected_before_checkout_or_git(self) -> None:
        app.USERS["usr_1"]["githubRepositoryAccess"]["repositories"] = ["owner/other"]
        app.USERS["usr_1"]["githubRepositoryAccess"]["repositoryItems"] = []

        with (
            patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
            patch("pullwise_server.app.checkout.prepare_checkout") as prepare_checkout,
            patch("pullwise_server.app.checkout.run_git") as run_git,
            patch("pullwise_server.app.github_auth.create_installation_access_token") as create_token,
        ):
            with self.assertRaisesRegex(ValueError, "not authorized"):
                app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        prepare_checkout.assert_not_called()
        run_git.assert_not_called()
        create_token.assert_not_called()

    def test_route_requires_sign_in_for_pull_request_creation(self) -> None:
        handler = RouteHarness("/issues/f_123/pull-requests")

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.UNAUTHORIZED)

    def test_route_maps_value_error_to_bad_request(self) -> None:
        handler = RouteHarness("/issues/f_123/pull-requests", cookie=self.signed_in())

        with patch("pullwise_server.app.create_issue_pull_request", side_effect=ValueError("No checkout")):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload, {"message": "No checkout"})

    def test_route_maps_github_error_to_service_unavailable(self) -> None:
        handler = RouteHarness("/issues/f_123/pull-requests", cookie=self.signed_in())

        with patch("pullwise_server.app.create_issue_pull_request", side_effect=github_auth.GitHubError("GitHub unavailable")):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(handler.payload, {"message": "GitHub unavailable"})

    def test_route_returns_pull_request_payload_on_success(self) -> None:
        handler = RouteHarness("/issues/f_123/pull-requests", cookie=self.signed_in())
        payload = {
            "issueId": "f_123",
            "branch": "pullwise/fix-f_123-fixedtoken",
            "url": "https://github.com/owner/repo/pull/12",
            "number": 12,
            "title": "Fix Validate redirect targets",
        }

        with patch("pullwise_server.app.create_issue_pull_request", return_value=payload) as create_pull_request:
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload, payload)
        create_pull_request.assert_called_once_with(app.USERS["usr_1"], app.ISSUES[0])

    def test_create_pull_request_uses_github_rest_endpoint_and_safe_public_payload(self) -> None:
        response = Mock()
        response.status_code = 201
        response.json.return_value = {
            "html_url": "https://github.com/owner/repo/pull/12",
            "number": 12,
            "title": "Fix Validate redirect targets",
            "head": {"repo": {"clone_url": "https://github.com/owner/repo.git"}},
        }
        response.raise_for_status.return_value = None

        with (
            patch("pullwise_server.github_auth.github_api_url", return_value="https://api.github.test"),
            patch("pullwise_server.github_auth.request_timeout", return_value=9),
            patch("pullwise_server.github_auth.requests.post", return_value=response) as post,
        ):
            payload = github_auth.create_pull_request(
                "ghs_secret_token",
                "owner/repo",
                title="Fix Validate redirect targets",
                head="pullwise/fix-f_123-fixedtoken",
                base="main",
                body="Automated fix for f_123.",
            )

        self.assertEqual(payload, {
            "url": "https://github.com/owner/repo/pull/12",
            "number": 12,
            "title": "Fix Validate redirect targets",
        })
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "https://api.github.test/repos/owner/repo/pulls")
        self.assertEqual(post.call_args.kwargs["json"], {
            "title": "Fix Validate redirect targets",
            "head": "pullwise/fix-f_123-fixedtoken",
            "base": "main",
            "body": "Automated fix for f_123.",
        })
        self.assertEqual(post.call_args.kwargs["timeout"], 9)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer ghs_secret_token")

    def test_create_pull_request_raises_github_error_on_request_failure(self) -> None:
        response = Mock()
        response.status_code = 422
        response.text = "Validation Failed"
        response.raise_for_status.side_effect = RuntimeError("422 Client Error")

        with patch("pullwise_server.github_auth.requests.post", return_value=response):
            with self.assertRaisesRegex(github_auth.GitHubError, "pull request"):
                github_auth.create_pull_request(
                    "ghs_secret_token",
                    "owner/repo",
                    title="Fix issue",
                    head="pullwise/fix-f_123-fixedtoken",
                    base="main",
                    body="Automated fix.",
                )


if __name__ == "__main__":
    unittest.main()
