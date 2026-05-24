from __future__ import annotations

import json
import os
import tempfile
import threading
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
                    "installationPermissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
                    "repositories": ["owner/repo"],
                    "repositoryItems": [
                        {
                            "fullName": "owner/repo",
                            "installationId": "123",
                            "defaultBranch": "main",
                            "cloneUrl": "https://github.com/owner/repo.git",
                            "installationPermissions": {
                                "metadata": "read",
                                "contents": "write",
                                "pull_requests": "write",
                            },
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

    def assert_no_external_pull_request_work(self, app_api_configured, prepare_checkout, run_git, create_token, create_pull_request) -> None:
        app_api_configured.assert_not_called()
        prepare_checkout.assert_not_called()
        run_git.assert_not_called()
        create_token.assert_not_called()
        create_pull_request.assert_not_called()

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

        with (
            patch("pullwise_server.app.github_auth.app_api_configured", return_value=True) as app_api_configured,
            patch("pullwise_server.app.checkout.prepare_checkout") as prepare_checkout,
            patch("pullwise_server.app.checkout.run_git") as run_git,
            patch("pullwise_server.app.github_auth.create_installation_access_token") as create_token,
            patch("pullwise_server.app.github_auth.create_pull_request") as create_pull_request,
        ):
            pull_request = app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        self.assertIs(pull_request, existing)
        app_api_configured.assert_called_once()
        prepare_checkout.assert_not_called()
        run_git.assert_not_called()
        create_token.assert_not_called()
        create_pull_request.assert_not_called()

    def test_existing_pull_request_does_not_bypass_github_app_configuration(self) -> None:
        app.ISSUES[0]["pullRequest"] = {
            "issueId": "f_123",
            "branch": "pullwise/fix-f_123-existing",
            "url": "https://github.com/owner/repo/pull/7",
            "number": 7,
            "title": "Fix Validate redirect targets",
        }

        with (
            patch("pullwise_server.app.github_auth.app_api_configured", return_value=False),
            patch("pullwise_server.app.checkout.prepare_checkout") as prepare_checkout,
            patch("pullwise_server.app.checkout.run_git") as run_git,
            patch("pullwise_server.app.github_auth.create_installation_access_token") as create_token,
            patch("pullwise_server.app.github_auth.create_pull_request") as create_pull_request,
        ):
            with self.assertRaisesRegex(ValueError, "GitHub App API"):
                app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        prepare_checkout.assert_not_called()
        run_git.assert_not_called()
        create_token.assert_not_called()
        create_pull_request.assert_not_called()

    def test_existing_pull_request_does_not_bypass_repository_write_permissions(self) -> None:
        app.USERS["usr_1"]["githubRepositoryAccess"]["installationPermissions"] = {
            "metadata": "read",
            "contents": "read",
            "pull_requests": "write",
        }
        app.USERS["usr_1"]["githubRepositoryAccess"]["repositoryItems"][0]["installationPermissions"] = {
            "metadata": "read",
            "contents": "read",
            "pull_requests": "write",
        }
        app.ISSUES[0]["pullRequest"] = {
            "issueId": "f_123",
            "branch": "pullwise/fix-f_123-existing",
            "url": "https://github.com/owner/repo/pull/7",
            "number": 7,
            "title": "Fix Validate redirect targets",
        }

        with (
            patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
            patch("pullwise_server.app.checkout.prepare_checkout") as prepare_checkout,
            patch("pullwise_server.app.checkout.run_git") as run_git,
            patch("pullwise_server.app.github_auth.create_installation_access_token") as create_token,
            patch("pullwise_server.app.github_auth.create_pull_request") as create_pull_request,
        ):
            with self.assertRaisesRegex(ValueError, "Contents: write.*Pull requests: write"):
                app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        prepare_checkout.assert_not_called()
        run_git.assert_not_called()
        create_token.assert_not_called()
        create_pull_request.assert_not_called()

    def test_existing_pull_request_does_not_bypass_issue_ownership_validation(self) -> None:
        app.ISSUES[0]["userId"] = "usr_2"
        app.ISSUES[0]["pullRequest"] = {
            "issueId": "f_123",
            "branch": "pullwise/fix-f_123-existing",
            "url": "https://github.com/owner/repo/pull/7",
            "number": 7,
            "title": "Fix Validate redirect targets",
        }

        with (
            patch("pullwise_server.app.github_auth.app_api_configured") as app_api_configured,
            patch("pullwise_server.app.checkout.prepare_checkout") as prepare_checkout,
            patch("pullwise_server.app.checkout.run_git") as run_git,
            patch("pullwise_server.app.github_auth.create_installation_access_token") as create_token,
            patch("pullwise_server.app.github_auth.create_pull_request") as create_pull_request,
        ):
            with self.assertRaisesRegex(ValueError, "Issue does not belong"):
                app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        app_api_configured.assert_not_called()
        prepare_checkout.assert_not_called()
        run_git.assert_not_called()
        create_token.assert_not_called()
        create_pull_request.assert_not_called()

    def test_existing_pull_request_does_not_bypass_scan_ownership_validation(self) -> None:
        app.SCANS[0]["userId"] = "usr_2"
        app.ISSUES[0]["pullRequest"] = {
            "issueId": "f_123",
            "branch": "pullwise/fix-f_123-existing",
            "url": "https://github.com/owner/repo/pull/7",
            "number": 7,
            "title": "Fix Validate redirect targets",
        }

        with (
            patch("pullwise_server.app.github_auth.app_api_configured") as app_api_configured,
            patch("pullwise_server.app.checkout.prepare_checkout") as prepare_checkout,
            patch("pullwise_server.app.checkout.run_git") as run_git,
            patch("pullwise_server.app.github_auth.create_installation_access_token") as create_token,
            patch("pullwise_server.app.github_auth.create_pull_request") as create_pull_request,
        ):
            with self.assertRaisesRegex(ValueError, "Scan does not belong"):
                app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        app_api_configured.assert_not_called()
        prepare_checkout.assert_not_called()
        run_git.assert_not_called()
        create_token.assert_not_called()
        create_pull_request.assert_not_called()

    def test_read_only_installation_permissions_are_rejected_before_checkout_or_git(self) -> None:
        app.USERS["usr_1"]["githubRepositoryAccess"]["installationPermissions"] = {
            "metadata": "read",
            "contents": "read",
            "pull_requests": "write",
        }
        app.USERS["usr_1"]["githubRepositoryAccess"]["repositoryItems"][0]["installationPermissions"] = {
            "metadata": "read",
            "contents": "read",
            "pull_requests": "write",
        }

        with (
            patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
            patch("pullwise_server.app.checkout.prepare_checkout") as prepare_checkout,
            patch("pullwise_server.app.checkout.run_git") as run_git,
            patch("pullwise_server.app.github_auth.create_installation_access_token") as create_token,
            patch("pullwise_server.app.github_auth.create_pull_request") as create_pull_request,
        ):
            with self.assertRaisesRegex(ValueError, "Contents: write.*Pull requests: write"):
                app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        prepare_checkout.assert_not_called()
        run_git.assert_not_called()
        create_token.assert_not_called()
        create_pull_request.assert_not_called()

    def test_missing_installation_permissions_are_rejected_before_checkout_or_git(self) -> None:
        app.USERS["usr_1"]["githubRepositoryAccess"].pop("installationPermissions", None)
        app.USERS["usr_1"]["githubRepositoryAccess"]["repositoryItems"][0].pop("installationPermissions", None)

        with (
            patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
            patch("pullwise_server.app.checkout.prepare_checkout") as prepare_checkout,
            patch("pullwise_server.app.checkout.run_git") as run_git,
            patch("pullwise_server.app.github_auth.create_installation_access_token") as create_token,
            patch("pullwise_server.app.github_auth.create_pull_request") as create_pull_request,
        ):
            with self.assertRaisesRegex(ValueError, "Contents: write.*Pull requests: write"):
                app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        prepare_checkout.assert_not_called()
        run_git.assert_not_called()
        create_token.assert_not_called()
        create_pull_request.assert_not_called()

    def test_repository_authorization_pending_is_rejected_before_checkout_or_git(self) -> None:
        app.USERS["usr_1"]["githubRepositoryAccessPending"] = {
            "state": "pending",
            "expiresAt": app.now() + 3600,
        }

        with (
            patch("pullwise_server.app.github_auth.app_api_configured") as app_api_configured,
            patch("pullwise_server.app.checkout.prepare_checkout") as prepare_checkout,
            patch("pullwise_server.app.checkout.run_git") as run_git,
            patch("pullwise_server.app.github_auth.create_installation_access_token") as create_token,
            patch("pullwise_server.app.github_auth.create_pull_request") as create_pull_request,
        ):
            with self.assertRaisesRegex(ValueError, "Complete GitHub repository authorization"):
                app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        self.assert_no_external_pull_request_work(app_api_configured, prepare_checkout, run_git, create_token, create_pull_request)

    def test_repository_sync_required_is_rejected_before_checkout_or_git(self) -> None:
        app.USERS["usr_1"]["githubRepositoryAccess"]["repositoriesNeedSync"] = True

        with (
            patch("pullwise_server.app.github_auth.app_api_configured") as app_api_configured,
            patch("pullwise_server.app.checkout.prepare_checkout") as prepare_checkout,
            patch("pullwise_server.app.checkout.run_git") as run_git,
            patch("pullwise_server.app.github_auth.create_installation_access_token") as create_token,
            patch("pullwise_server.app.github_auth.create_pull_request") as create_pull_request,
        ):
            with self.assertRaisesRegex(ValueError, "Sync GitHub repositories"):
                app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        self.assert_no_external_pull_request_work(app_api_configured, prepare_checkout, run_git, create_token, create_pull_request)

    def test_non_completed_scan_is_rejected_before_checkout_or_git(self) -> None:
        app.SCANS[0]["status"] = "running"

        with (
            patch("pullwise_server.app.github_auth.app_api_configured") as app_api_configured,
            patch("pullwise_server.app.checkout.prepare_checkout") as prepare_checkout,
            patch("pullwise_server.app.checkout.run_git") as run_git,
            patch("pullwise_server.app.github_auth.create_installation_access_token") as create_token,
            patch("pullwise_server.app.github_auth.create_pull_request") as create_pull_request,
        ):
            with self.assertRaisesRegex(ValueError, "completed"):
                app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        self.assert_no_external_pull_request_work(app_api_configured, prepare_checkout, run_git, create_token, create_pull_request)

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

    def test_pull_request_creation_sanitizes_legacy_source_metadata(self) -> None:
        token = "ghs_secret_token"
        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        github_access["installationId"] = "123"
        github_access["defaultBranch"] = "stable"
        github_access["repositoryItems"][0]["installationId"] = {"id": "123"}
        github_access["repositoryItems"][0]["defaultBranch"] = "release"
        github_access["repositoryItems"][0]["cloneUrl"] = "https://evil.example/owner/repo.git"
        app.ISSUES[0]["repo"] = {"fullName": "owner/repo"}
        app.ISSUES[0]["branch"] = "main\r\nX-Injected: bad"
        app.ISSUES[0]["title"] = "Validate redirect targets\r\nX-Injected: bad"
        app.SCANS[0]["branch"] = {"name": "main"}
        app.SCANS[0]["installationId"] = {"id": "123"}
        app.SCANS[0]["cloneUrl"] = "https://evil.example/owner/repo.git"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                repo_path = checkout.checkout_path_for("usr_1", "pr_f_123", "owner/repo")
                os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
                with open(os.path.join(repo_path, "src", "auth.py"), "w", encoding="utf-8") as handle:
                    handle.write("def redirect_target(next_url):\n    return redirect(next_url)\n")

                with (
                    patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
                    patch("pullwise_server.app.checkout.prepare_checkout", return_value=repo_path) as prepare_checkout,
                    patch("pullwise_server.app.checkout.run_git") as run_git,
                    patch("pullwise_server.app.github_auth.create_installation_access_token", return_value={"token": token}) as create_token,
                    patch(
                        "pullwise_server.app.github_auth.create_pull_request",
                        return_value={
                            "url": "https://github.com/owner/repo/pull/12",
                            "number": 12,
                            "title": "Fix Validate redirect targets",
                        },
                    ) as create_pull_request,
                    patch("pullwise_server.app.checkout.cleanup_scan_workspace"),
                    patch("pullwise_server.app.make_id", return_value="fix_fixedtoken"),
                ):
                    app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        scan_payload = prepare_checkout.call_args.args[1]
        self.assertEqual(scan_payload["branch"], "release")
        self.assertEqual(scan_payload["installationId"], "123")
        self.assertIsNone(scan_payload["cloneUrl"])
        create_token.assert_called_once_with("123")
        self.assertEqual(create_pull_request.call_args.kwargs["base"], "release")
        self.assertEqual(create_pull_request.call_args.kwargs["title"], "Fix f_123")
        commit_commands = [
            call.args[0]
            for call in run_git.call_args_list
            if call.args[0][:3] == ["git", "commit", "-m"]
        ]
        self.assertEqual(commit_commands, [["git", "commit", "-m", "Fix f_123"]])

    def test_successful_pull_request_transition_is_persisted_before_cleanup(self) -> None:
        token = "ghs_secret_token"
        events: list[str] = []

        def persist_state() -> None:
            if app.ISSUES[0].get("pullRequest"):
                self.assertNotIn("pullRequestPending", app.ISSUES[0])
                events.append("success_persist")
            elif app.ISSUES[0].get("pullRequestPending"):
                events.append("pending_persist")

        def cleanup(_user_id: str, _scan_id: str) -> None:
            events.append("cleanup")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                repo_path = checkout.checkout_path_for("usr_1", "pr_f_123", "owner/repo")
                os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
                with open(os.path.join(repo_path, "src", "auth.py"), "w", encoding="utf-8") as handle:
                    handle.write("def redirect_target(next_url):\n    return redirect(next_url)\n")

                with (
                    patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
                    patch("pullwise_server.app.persist_state", side_effect=persist_state),
                    patch("pullwise_server.app.checkout.prepare_checkout", return_value=repo_path),
                    patch("pullwise_server.app.checkout.run_git"),
                    patch("pullwise_server.app.github_auth.create_installation_access_token", return_value={"token": token}),
                    patch(
                        "pullwise_server.app.github_auth.create_pull_request",
                        return_value={
                            "url": "https://github.com/owner/repo/pull/12",
                            "number": 12,
                            "title": "Fix Validate redirect targets",
                        },
                    ),
                    patch("pullwise_server.app.checkout.cleanup_scan_workspace", side_effect=cleanup),
                    patch("pullwise_server.app.make_id", return_value="fix_fixedtoken"),
                ):
                    pull_request = app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        self.assertEqual(pull_request, app.ISSUES[0]["pullRequest"])
        self.assertEqual(events, ["pending_persist", "success_persist", "cleanup"])

    def test_pull_request_state_transition_mutations_hold_state_lock(self) -> None:
        token = "ghs_secret_token"
        mutations: list[tuple[str, str]] = []

        class TrackingLock:
            def __init__(self) -> None:
                self.depth = 0

            def __enter__(self) -> "TrackingLock":
                self.depth += 1
                return self

            def __exit__(self, *_exc_info: object) -> None:
                self.depth -= 1

        tracking_lock = TrackingLock()
        test_case = self

        class LockCheckingIssue(dict):
            def __setitem__(self, key: str, value: object) -> None:
                if key in {"pullRequestPending", "pullRequest"}:
                    test_case.assertGreater(tracking_lock.depth, 0, f"{key} set outside STATE_LOCK")
                    mutations.append(("set", key))
                super().__setitem__(key, value)

            def pop(self, key: str, *args: object) -> object:
                if key in {"pullRequestPending", "pullRequest"}:
                    test_case.assertGreater(tracking_lock.depth, 0, f"{key} popped outside STATE_LOCK")
                    mutations.append(("pop", key))
                return super().pop(key, *args)

        app.ISSUES[0] = LockCheckingIssue(app.ISSUES[0])

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                repo_path = checkout.checkout_path_for("usr_1", "pr_f_123", "owner/repo")
                os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
                with open(os.path.join(repo_path, "src", "auth.py"), "w", encoding="utf-8") as handle:
                    handle.write("def redirect_target(next_url):\n    return redirect(next_url)\n")

                with (
                    patch("pullwise_server.app.STATE_LOCK", tracking_lock),
                    patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
                    patch("pullwise_server.app.checkout.prepare_checkout", return_value=repo_path),
                    patch("pullwise_server.app.checkout.run_git"),
                    patch("pullwise_server.app.github_auth.create_installation_access_token", return_value={"token": token}),
                    patch(
                        "pullwise_server.app.github_auth.create_pull_request",
                        return_value={
                            "url": "https://github.com/owner/repo/pull/12",
                            "number": 12,
                            "title": "Fix Validate redirect targets",
                        },
                    ),
                    patch("pullwise_server.app.checkout.cleanup_scan_workspace"),
                    patch("pullwise_server.app.make_id", return_value="fix_fixedtoken"),
                ):
                    app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        self.assertIn(("set", "pullRequestPending"), mutations)
        self.assertIn(("pop", "pullRequestPending"), mutations)
        self.assertIn(("set", "pullRequest"), mutations)

    def test_concurrent_pull_request_creation_for_same_issue_runs_external_work_once(self) -> None:
        token = "ghs_secret_token"
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                repo_path = checkout.checkout_path_for("usr_1", "pr_f_123", "owner/repo")
                os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
                with open(os.path.join(repo_path, "src", "auth.py"), "w", encoding="utf-8") as handle:
                    handle.write("def redirect_target(next_url):\n    return redirect(next_url)\n")

                prepare_entered = threading.Event()
                release_prepare = threading.Event()
                results: list[dict] = []
                errors: list[Exception] = []

                def prepare_checkout(_scan_id, _scan, _is_cancelled):
                    prepare_entered.set()
                    release_prepare.wait(1)
                    return repo_path

                def run_workflow() -> None:
                    try:
                        results.append(app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0]))
                    except Exception as exc:
                        errors.append(exc)

                with (
                    patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
                    patch("pullwise_server.app.checkout.prepare_checkout", side_effect=prepare_checkout) as prepare,
                    patch("pullwise_server.app.checkout.run_git") as run_git,
                    patch("pullwise_server.app.github_auth.create_installation_access_token", return_value={"token": token}) as create_token,
                    patch(
                        "pullwise_server.app.github_auth.create_pull_request",
                        return_value={
                            "url": "https://github.com/owner/repo/pull/12",
                            "number": 12,
                            "title": "Fix Validate redirect targets",
                        },
                    ) as create_pull_request,
                    patch("pullwise_server.app.checkout.cleanup_scan_workspace"),
                    patch("pullwise_server.app.make_id", return_value="fix_fixedtoken"),
                ):
                    first = threading.Thread(target=run_workflow)
                    second = threading.Thread(target=run_workflow)
                    first.start()
                    self.assertTrue(prepare_entered.wait(1))
                    second.start()
                    release_prepare.set()
                    first.join()
                    second.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertIs(results[0], app.ISSUES[0]["pullRequest"])
        self.assertIs(results[1], app.ISSUES[0]["pullRequest"])
        self.assertEqual(results[0], results[1])
        prepare.assert_called_once()
        create_token.assert_called_once()
        self.assertEqual(run_git.call_count, 4)
        create_pull_request.assert_called_once()

    def test_pending_pull_request_marker_blocks_duplicate_external_work(self) -> None:
        app.ISSUES[0]["pullRequestPending"] = {
            "branch": "pullwise/fix-f_123-pending",
            "startedAt": app.now(),
        }

        with (
            patch("pullwise_server.app.github_auth.app_api_configured") as app_api_configured,
            patch("pullwise_server.app.checkout.prepare_checkout") as prepare_checkout,
            patch("pullwise_server.app.checkout.run_git") as run_git,
            patch("pullwise_server.app.github_auth.create_installation_access_token") as create_token,
            patch("pullwise_server.app.github_auth.create_pull_request") as create_pull_request,
        ):
            with self.assertRaisesRegex(ValueError, "already in progress"):
                app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        self.assert_no_external_pull_request_work(app_api_configured, prepare_checkout, run_git, create_token, create_pull_request)

    def test_stale_pending_marker_recovers_existing_github_pull_request(self) -> None:
        app.ISSUES[0]["pullRequestPending"] = {
            "branch": "pullwise/fix-f_123-stale",
            "startedAt": app.now() - 3600,
            "lastError": "push result unknown",
        }
        recovered = {
            "url": "https://github.com/owner/repo/pull/12",
            "number": 12,
            "title": "Fix Validate redirect targets",
        }

        with (
            patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
            patch("pullwise_server.app.github_auth.create_installation_access_token", return_value={"token": "ghs_secret_token"}),
            patch("pullwise_server.app.github_auth.find_pull_request_by_head", return_value=recovered) as find_pull_request,
            patch("pullwise_server.app.persist_state") as persist_state,
            patch("pullwise_server.app.checkout.prepare_checkout") as prepare_checkout,
            patch("pullwise_server.app.checkout.run_git") as run_git,
            patch("pullwise_server.app.github_auth.create_pull_request") as create_pull_request,
        ):
            pull_request = app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        self.assertEqual(pull_request["branch"], "pullwise/fix-f_123-stale")
        self.assertEqual(pull_request["number"], 12)
        self.assertEqual(app.ISSUES[0]["pullRequest"], pull_request)
        self.assertNotIn("pullRequestPending", app.ISSUES[0])
        find_pull_request.assert_called_once_with("ghs_secret_token", "owner/repo", head="pullwise/fix-f_123-stale")
        persist_state.assert_called()
        prepare_checkout.assert_not_called()
        run_git.assert_not_called()
        create_pull_request.assert_not_called()

    def test_stale_pending_marker_retries_using_existing_branch(self) -> None:
        token = "ghs_secret_token"
        app.ISSUES[0]["pullRequestPending"] = {
            "branch": "pullwise/fix-f_123-stale",
            "startedAt": app.now() - 3600,
            "lastError": "push result unknown",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                repo_path = checkout.checkout_path_for("usr_1", "pr_f_123", "owner/repo")
                os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
                with open(os.path.join(repo_path, "src", "auth.py"), "w", encoding="utf-8") as handle:
                    handle.write("def redirect_target(next_url):\n    return redirect(next_url)\n")

                with (
                    patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
                    patch("pullwise_server.app.github_auth.create_installation_access_token", return_value={"token": token}),
                    patch("pullwise_server.app.github_auth.find_pull_request_by_head", return_value=None),
                    patch("pullwise_server.app.github_auth.branch_exists", return_value=False),
                    patch("pullwise_server.app.checkout.prepare_checkout", return_value=repo_path),
                    patch("pullwise_server.app.checkout.run_git") as run_git,
                    patch(
                        "pullwise_server.app.github_auth.create_pull_request",
                        return_value={
                            "url": "https://github.com/owner/repo/pull/12",
                            "number": 12,
                            "title": "Fix Validate redirect targets",
                        },
                    ),
                    patch("pullwise_server.app.checkout.cleanup_scan_workspace"),
                    patch("pullwise_server.app.make_id", side_effect=AssertionError("new branch token should not be generated")),
                ):
                    pull_request = app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        self.assertEqual(pull_request["branch"], "pullwise/fix-f_123-stale")
        self.assertEqual(run_git.call_args_list[0].args[0], ["git", "checkout", "-B", "pullwise/fix-f_123-stale"])

    def test_stale_pending_marker_creates_pull_request_directly_when_remote_branch_exists(self) -> None:
        token = "ghs_secret_token"
        app.ISSUES[0]["pullRequestPending"] = {
            "branch": "pullwise/fix-f_123-stale",
            "startedAt": app.now() - 3600,
            "lastError": "process exited after push",
        }

        with (
            patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
            patch("pullwise_server.app.github_auth.create_installation_access_token", return_value={"token": token}),
            patch("pullwise_server.app.github_auth.find_pull_request_by_head", return_value=None),
            patch("pullwise_server.app.github_auth.branch_exists", return_value=True) as branch_exists,
            patch(
                "pullwise_server.app.github_auth.create_pull_request",
                return_value={
                    "url": "https://github.com/owner/repo/pull/12",
                    "number": 12,
                    "title": "Fix Validate redirect targets",
                },
            ) as create_pull_request,
            patch("pullwise_server.app.persist_state") as persist_state,
            patch("pullwise_server.app.checkout.prepare_checkout") as prepare_checkout,
            patch("pullwise_server.app.checkout.run_git") as run_git,
            patch("pullwise_server.app.make_id", side_effect=AssertionError("new branch token should not be generated")),
        ):
            pull_request = app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        self.assertEqual(pull_request["branch"], "pullwise/fix-f_123-stale")
        self.assertEqual(pull_request["number"], 12)
        self.assertEqual(app.ISSUES[0]["pullRequest"], pull_request)
        self.assertNotIn("pullRequestPending", app.ISSUES[0])
        branch_exists.assert_called_once_with(token, "owner/repo", "pullwise/fix-f_123-stale")
        create_pull_request.assert_called_once()
        self.assertEqual(create_pull_request.call_args.kwargs["head"], "pullwise/fix-f_123-stale")
        prepare_checkout.assert_not_called()
        run_git.assert_not_called()
        persist_state.assert_called()

    def test_direct_pull_request_failure_from_existing_remote_branch_preserves_pending_marker(self) -> None:
        token = "ghs_secret_token"
        app.ISSUES[0]["pullRequestPending"] = {
            "branch": "pullwise/fix-f_123-stale",
            "startedAt": app.now() - 3600,
            "lastError": "process exited after push",
        }

        with (
            patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
            patch("pullwise_server.app.github_auth.create_installation_access_token", return_value={"token": token}),
            patch("pullwise_server.app.github_auth.find_pull_request_by_head", return_value=None),
            patch("pullwise_server.app.github_auth.branch_exists", return_value=True),
            patch("pullwise_server.app.github_auth.create_pull_request", side_effect=github_auth.GitHubError("GitHub PR failed")),
            patch("pullwise_server.app.persist_state") as persist_state,
            patch("pullwise_server.app.checkout.prepare_checkout") as prepare_checkout,
            patch("pullwise_server.app.checkout.run_git") as run_git,
        ):
            with self.assertRaisesRegex(github_auth.GitHubError, "GitHub PR failed"):
                app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        pending = app.ISSUES[0]["pullRequestPending"]
        self.assertEqual(pending["branch"], "pullwise/fix-f_123-stale")
        self.assertIn("lastError", pending)
        self.assertIn("failedAt", pending)
        prepare_checkout.assert_not_called()
        run_git.assert_not_called()
        persist_state.assert_called()

    def test_stale_pending_marker_with_invalid_branch_is_cleared(self) -> None:
        for branch in (
            "../main",
            "pullwise/fix-f_123/.lock",
            "pullwise/fix-f_123//retry",
            "pullwise/fix-f_123-stale ",
            "\tpullwise/fix-f_123-stale",
        ):
            with self.subTest(branch=branch):
                app.ISSUES[0]["pullRequestPending"] = {
                    "branch": branch,
                    "startedAt": app.now() - 3600,
                    "lastError": "previous bad state",
                }

                with (
                    patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
                    patch("pullwise_server.app.github_auth.create_installation_access_token") as create_token,
                    patch("pullwise_server.app.github_auth.find_pull_request_by_head", return_value=None) as find_pull_request,
                    patch("pullwise_server.app.github_auth.branch_exists", side_effect=AssertionError("branch lookup should not run")),
                    patch("pullwise_server.app.checkout.prepare_checkout") as prepare_checkout,
                    patch("pullwise_server.app.persist_state") as persist_state,
                ):
                    with self.assertRaisesRegex(ValueError, "Stored pull request branch is invalid"):
                        app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

                self.assertNotIn("pullRequestPending", app.ISSUES[0])
                create_token.assert_not_called()
                find_pull_request.assert_not_called()
                prepare_checkout.assert_not_called()
                persist_state.assert_called()

    def test_failure_after_push_started_keeps_pending_branch_marker(self) -> None:
        token = "ghs_secret_token"
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                repo_path = checkout.checkout_path_for("usr_1", "pr_f_123", "owner/repo")
                os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
                with open(os.path.join(repo_path, "src", "auth.py"), "w", encoding="utf-8") as handle:
                    handle.write("def redirect_target(next_url):\n    return redirect(next_url)\n")

                def run_git(cmd: list[str], **_kwargs: object) -> None:
                    if cmd[:3] == ["git", "push", "origin"]:
                        raise RuntimeError("Git push failed")

                with (
                    patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
                    patch("pullwise_server.app.github_auth.create_installation_access_token", return_value={"token": token}),
                    patch("pullwise_server.app.checkout.prepare_checkout", return_value=repo_path),
                    patch("pullwise_server.app.checkout.run_git", side_effect=run_git),
                    patch("pullwise_server.app.checkout.cleanup_scan_workspace"),
                    patch("pullwise_server.app.persist_state") as persist_state,
                    patch("pullwise_server.app.make_id", return_value="fix_fixedtoken"),
                ):
                    with self.assertRaises(github_auth.GitHubError):
                        app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        pending = app.ISSUES[0]["pullRequestPending"]
        self.assertEqual(pending["branch"], "pullwise/fix-f_123-fixedtoken")
        self.assertIn("lastError", pending)
        self.assertIn("failedAt", pending)
        persist_state.assert_called()

    def test_remote_clone_failure_clears_pending_marker_and_raises_github_error(self) -> None:
        with (
            patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
            patch("pullwise_server.app.checkout.prepare_checkout", side_effect=RuntimeError("Git clone repository failed")),
            patch("pullwise_server.app.checkout.cleanup_scan_workspace"),
            patch("pullwise_server.app.persist_state") as persist_state,
            patch("pullwise_server.app.make_id", return_value="fix_fixedtoken"),
        ):
            with self.assertRaisesRegex(github_auth.GitHubError, "Git clone repository failed"):
                app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        self.assertNotIn("pullRequestPending", app.ISSUES[0])
        persist_state.assert_called()

    def test_checkout_missing_token_failure_clears_pending_marker_and_raises_github_error(self) -> None:
        message = "GitHub App did not return an installation access token."
        with (
            patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
            patch("pullwise_server.app.checkout.prepare_checkout", side_effect=RuntimeError(message)),
            patch("pullwise_server.app.checkout.cleanup_scan_workspace"),
            patch("pullwise_server.app.persist_state") as persist_state,
            patch("pullwise_server.app.make_id", return_value="fix_fixedtoken"),
        ):
            with self.assertRaisesRegex(github_auth.GitHubError, "installation access token"):
                app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        self.assertNotIn("pullRequestPending", app.ISSUES[0])
        persist_state.assert_called()

    def test_pending_marker_is_cleared_when_handled_operation_fails(self) -> None:
        with (
            patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
            patch("pullwise_server.app.checkout.prepare_checkout", side_effect=RuntimeError("clone failed")),
            patch("pullwise_server.app.checkout.cleanup_scan_workspace"),
            patch("pullwise_server.app.make_id", return_value="fix_fixedtoken"),
        ):
            with self.assertRaisesRegex(ValueError, "clone failed"):
                app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        self.assertNotIn("pullRequestPending", app.ISSUES[0])

    def test_pending_marker_is_persisted_before_checkout_starts(self) -> None:
        events: list[str] = []

        def persist_state() -> None:
            if app.ISSUES[0].get("pullRequestPending"):
                self.assertIn("branch", app.ISSUES[0]["pullRequestPending"])
                self.assertIn("startedAt", app.ISSUES[0]["pullRequestPending"])
                events.append("persist")
            else:
                events.append("clear_persist")

        def prepare_checkout(_scan_id, _scan, _is_cancelled):
            events.append("prepare")
            raise RuntimeError("stop after persistence check")

        with (
            patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
            patch("pullwise_server.app.persist_state", side_effect=persist_state) as persist,
            patch("pullwise_server.app.checkout.prepare_checkout", side_effect=prepare_checkout),
            patch("pullwise_server.app.checkout.cleanup_scan_workspace"),
            patch("pullwise_server.app.make_id", return_value="fix_fixedtoken"),
        ):
            with self.assertRaisesRegex(ValueError, "stop after persistence check"):
                app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        persist.assert_called()
        self.assertEqual(events[:2], ["persist", "prepare"])

    def test_cleanup_failure_after_success_does_not_mask_pull_request_result(self) -> None:
        token = "ghs_secret_token"
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                repo_path = checkout.checkout_path_for("usr_1", "pr_f_123", "owner/repo")
                os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
                with open(os.path.join(repo_path, "src", "auth.py"), "w", encoding="utf-8") as handle:
                    handle.write("def redirect_target(next_url):\n    return redirect(next_url)\n")

                with (
                    patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
                    patch("pullwise_server.app.checkout.prepare_checkout", return_value=repo_path),
                    patch("pullwise_server.app.checkout.run_git"),
                    patch("pullwise_server.app.github_auth.create_installation_access_token", return_value={"token": token}),
                    patch(
                        "pullwise_server.app.github_auth.create_pull_request",
                        return_value={
                            "url": "https://github.com/owner/repo/pull/12",
                            "number": 12,
                            "title": "Fix Validate redirect targets",
                        },
                    ),
                    patch("pullwise_server.app.checkout.cleanup_scan_workspace", side_effect=RuntimeError("cleanup failed")),
                    patch("pullwise_server.app.logger.warning") as warning,
                    patch("pullwise_server.app.make_id", return_value="fix_fixedtoken"),
                ):
                    pull_request = app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        self.assertEqual(pull_request, app.ISSUES[0]["pullRequest"])
        self.assertEqual(pull_request["number"], 12)
        warning.assert_called_once()

    def test_unsafe_issue_id_is_sanitized_before_branch_and_workspace_use(self) -> None:
        token = "ghs_secret_token"
        app.ISSUES[0]["id"] = "f/../bad value"
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                repo_path = checkout.checkout_path_for("usr_1", "pr_f-bad-value", "owner/repo")
                os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
                with open(os.path.join(repo_path, "src", "auth.py"), "w", encoding="utf-8") as handle:
                    handle.write("def redirect_target(next_url):\n    return redirect(next_url)\n")

                with (
                    patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
                    patch("pullwise_server.app.checkout.prepare_checkout", return_value=repo_path) as prepare_checkout,
                    patch("pullwise_server.app.checkout.run_git") as run_git,
                    patch("pullwise_server.app.github_auth.create_installation_access_token", return_value={"token": token}),
                    patch(
                        "pullwise_server.app.github_auth.create_pull_request",
                        return_value={
                            "url": "https://github.com/owner/repo/pull/12",
                            "number": 12,
                            "title": "Fix Validate redirect targets",
                        },
                    ),
                    patch("pullwise_server.app.checkout.cleanup_scan_workspace"),
                    patch("pullwise_server.app.make_id", return_value="fix_token/.. value"),
                ):
                    pull_request = app.create_issue_pull_request(app.USERS["usr_1"], app.ISSUES[0])

        branch = pull_request["branch"]
        self.assertEqual(prepare_checkout.call_args.args[0], "pr_f-bad-value")
        self.assertNotIn("..", branch)
        self.assertNotIn(" ", branch)
        self.assertTrue(branch.startswith("pullwise/fix-f-bad-value-token-value"))
        self.assertEqual(run_git.call_args_list[0].args[0], ["git", "checkout", "-B", branch])

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

    def test_route_returns_not_found_for_missing_pull_request_issue(self) -> None:
        handler = RouteHarness("/issues/missing/pull-requests", cookie=self.signed_in())

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.NOT_FOUND)
        self.assertEqual(handler.payload, {"message": "Issue not found."})

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

    def test_route_maps_installation_token_failure_to_service_unavailable(self) -> None:
        handler = RouteHarness("/issues/f_123/pull-requests", cookie=self.signed_in())

        with (
            patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
            patch("pullwise_server.app.github_auth.create_installation_access_token", side_effect=github_auth.GitHubError("GitHub token failed")),
        ):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(handler.payload, {"message": "GitHub token failed"})

    def test_route_maps_remote_git_push_failure_to_service_unavailable(self) -> None:
        token = "ghs_secret_token"
        handler = RouteHarness("/issues/f_123/pull-requests", cookie=self.signed_in())
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                repo_path = checkout.checkout_path_for("usr_1", "pr_f_123", "owner/repo")
                os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
                with open(os.path.join(repo_path, "src", "auth.py"), "w", encoding="utf-8") as handle:
                    handle.write("def redirect_target(next_url):\n    return redirect(next_url)\n")

                def run_git(cmd: list[str], **_kwargs: object) -> None:
                    if cmd[:3] == ["git", "push", "origin"]:
                        raise RuntimeError("Git push failed")

                with (
                    patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
                    patch("pullwise_server.app.github_auth.create_installation_access_token", return_value={"token": token}),
                    patch("pullwise_server.app.checkout.prepare_checkout", return_value=repo_path),
                    patch("pullwise_server.app.checkout.run_git", side_effect=run_git),
                    patch("pullwise_server.app.checkout.cleanup_scan_workspace"),
                    patch("pullwise_server.app.make_id", return_value="fix_fixedtoken"),
                ):
                    app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertIn("Git push failed", handler.payload["message"])

    def test_route_maps_remote_git_clone_failure_to_service_unavailable_and_clears_pending(self) -> None:
        handler = RouteHarness("/issues/f_123/pull-requests", cookie=self.signed_in())

        with (
            patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
            patch("pullwise_server.app.checkout.prepare_checkout", side_effect=RuntimeError("Git clone repository failed")),
            patch("pullwise_server.app.checkout.cleanup_scan_workspace"),
            patch("pullwise_server.app.make_id", return_value="fix_fixedtoken"),
        ):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertIn("Git clone repository failed", handler.payload["message"])
        self.assertNotIn("pullRequestPending", app.ISSUES[0])

    def test_route_maps_checkout_missing_token_failure_to_service_unavailable_and_clears_pending(self) -> None:
        handler = RouteHarness("/issues/f_123/pull-requests", cookie=self.signed_in())
        message = "GitHub App did not return an installation access token."

        with (
            patch("pullwise_server.app.github_auth.app_api_configured", return_value=True),
            patch("pullwise_server.app.checkout.prepare_checkout", side_effect=RuntimeError(message)),
            patch("pullwise_server.app.checkout.cleanup_scan_workspace"),
            patch("pullwise_server.app.make_id", return_value="fix_fixedtoken"),
        ):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertIn("installation access token", handler.payload["message"])
        self.assertNotIn("pullRequestPending", app.ISSUES[0])

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

    def test_create_pull_request_wraps_network_errors_as_github_error(self) -> None:
        with patch("pullwise_server.github_auth.requests.post", side_effect=RuntimeError("network down")):
            with self.assertRaisesRegex(github_auth.GitHubError, "GitHub pull request creation failed"):
                github_auth.create_pull_request(
                    "ghs_secret_token",
                    "owner/repo",
                    title="Fix issue",
                    head="pullwise/fix-f_123-fixedtoken",
                    base="main",
                    body="Automated fix.",
                )

    def test_create_installation_access_token_wraps_provider_errors_as_github_error(self) -> None:
        with patch("pullwise_server.github_auth.app_integration", side_effect=RuntimeError("provider down")):
            with self.assertRaisesRegex(github_auth.GitHubError, "installation token"):
                github_auth.create_installation_access_token("123")

    def test_create_installation_access_token_rejects_malformed_tokens(self) -> None:
        token = Mock()
        token.token = {"token": "bad"}
        token.expires_at = "2026-05-25T00:00:00Z"
        integration = Mock()
        integration.get_access_token.return_value = token

        with patch("pullwise_server.github_auth.app_integration", return_value=integration):
            with self.assertRaisesRegex(github_auth.GitHubError, "installation token"):
                github_auth.create_installation_access_token("123")

        integration.close.assert_called_once()

    def test_create_pull_request_wraps_invalid_json_as_github_error(self) -> None:
        response = Mock()
        response.status_code = 201
        response.json.side_effect = ValueError("invalid json")

        with patch("pullwise_server.github_auth.requests.post", return_value=response):
            with self.assertRaisesRegex(github_auth.GitHubError, "response"):
                github_auth.create_pull_request(
                    "ghs_secret_token",
                    "owner/repo",
                    title="Fix issue",
                    head="pullwise/fix-f_123-fixedtoken",
                    base="main",
                    body="Automated fix.",
                )

    def test_create_pull_request_rejects_non_object_success_response(self) -> None:
        response = Mock()
        response.status_code = 201
        response.json.return_value = []

        with patch("pullwise_server.github_auth.requests.post", return_value=response):
            with self.assertRaisesRegex(github_auth.GitHubError, "response"):
                github_auth.create_pull_request(
                    "ghs_secret_token",
                    "owner/repo",
                    title="Fix issue",
                    head="pullwise/fix-f_123-fixedtoken",
                    base="main",
                    body="Automated fix.",
                )

    def test_create_pull_request_rejects_success_response_missing_public_fields(self) -> None:
        response = Mock()
        response.status_code = 201
        response.json.return_value = {}

        with patch("pullwise_server.github_auth.requests.post", return_value=response):
            with self.assertRaisesRegex(github_auth.GitHubError, "response"):
                github_auth.create_pull_request(
                    "ghs_secret_token",
                    "owner/repo",
                    title="Fix issue",
                    head="pullwise/fix-f_123-fixedtoken",
                    base="main",
                    body="Automated fix.",
                )

    def test_create_pull_request_rejects_malformed_success_public_fields(self) -> None:
        malformed_payloads = [
            {
                "html_url": "javascript:alert(1)",
                "number": 12,
                "title": "Fix issue",
            },
            {
                "html_url": "https://github.com/owner/repo/pull/12",
                "number": {"value": 12},
                "title": "Fix issue",
            },
            {
                "html_url": "https://github.com/owner/repo/pull/12",
                "number": 12,
                "title": "Fix issue\r\nX-Injected: bad",
            },
        ]

        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                response = Mock()
                response.status_code = 201
                response.json.return_value = payload

                with patch("pullwise_server.github_auth.requests.post", return_value=response):
                    with self.assertRaisesRegex(github_auth.GitHubError, "response"):
                        github_auth.create_pull_request(
                            "ghs_secret_token",
                            "owner/repo",
                            title="Fix issue",
                            head="pullwise/fix-f_123-fixedtoken",
                            base="main",
                            body="Automated fix.",
                        )

    def test_branch_exists_returns_true_for_exact_ref_match(self) -> None:
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"ref": "refs/heads/pullwise/fix-f_123-stale"}

        with patch("pullwise_server.github_auth.requests.get", return_value=response) as get:
            exists = github_auth.branch_exists("ghs_secret_token", "owner/repo", "pullwise/fix-f_123-stale")

        self.assertTrue(exists)
        self.assertEqual(get.call_args.args[0], "https://api.github.com/repos/owner/repo/git/ref/heads/pullwise%2Ffix-f_123-stale")

    def test_branch_exists_returns_false_for_not_found(self) -> None:
        response = Mock()
        response.status_code = 404

        with patch("pullwise_server.github_auth.requests.get", return_value=response):
            self.assertFalse(github_auth.branch_exists("ghs_secret_token", "owner/repo", "pullwise/fix-f_123-stale"))

    def test_branch_exists_rejects_malformed_success_body(self) -> None:
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"ref": "refs/tags/pullwise/fix-f_123-stale"}

        with patch("pullwise_server.github_auth.requests.get", return_value=response):
            with self.assertRaisesRegex(github_auth.GitHubError, "branch lookup response"):
                github_auth.branch_exists("ghs_secret_token", "owner/repo", "pullwise/fix-f_123-stale")

    def test_branch_exists_rejects_malformed_success_list_body(self) -> None:
        response = Mock()
        response.status_code = 200
        response.json.return_value = [{"ref": "refs/tags/pullwise/fix-f_123-stale"}]

        with patch("pullwise_server.github_auth.requests.get", return_value=response):
            with self.assertRaisesRegex(github_auth.GitHubError, "branch lookup response"):
                github_auth.branch_exists("ghs_secret_token", "owner/repo", "pullwise/fix-f_123-stale")


if __name__ == "__main__":
    unittest.main()
