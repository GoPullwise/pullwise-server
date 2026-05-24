from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from pullwise_server import app, checkout, review


class CheckoutContractsTest(unittest.TestCase):
    def test_review_provider_checkout_contract(self) -> None:
        self.assertFalse(review.provider_requires_checkout("mock"))
        self.assertTrue(review.provider_requires_checkout("claude_code"))
        self.assertTrue(review.provider_requires_checkout("codex"))

    def test_review_provider_defaults_to_disabled(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(review.selected_provider(), "disabled")
            self.assertFalse(review.provider_requires_checkout())

    def test_disabled_review_provider_does_not_emit_mock_findings(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "provider is not configured"):
                review.run_review(
                    repo="owner/repo",
                    branch="main",
                    commit="pending",
                    user_id="usr_1",
                    scan_id="sc_1",
                )

    def test_clone_url_must_stay_on_configured_github_host(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_GITHUB_WEB_URL": "https://github.com"}, clear=False):
            self.assertEqual(
                checkout.clone_url_for("owner/repo", "https://github.com/owner/repo.git"),
                "https://github.com/owner/repo.git",
            )
            with self.assertRaisesRegex(RuntimeError, "host"):
                checkout.clone_url_for("owner/repo", "https://evil.example/owner/repo.git")

    def test_prepare_checkout_uses_installation_token_without_putting_it_in_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            scan = {
                "userId": "usr_1",
                "repo": "owner/repo",
                "branch": "main",
                "commit": "pending",
                "installationId": "123",
                "cloneUrl": "https://github.com/owner/repo.git",
            }
            with (
                patch.dict(
                    os.environ,
                    {
                        "PULLWISE_CHECKOUT_ROOT": tmpdir,
                        "PULLWISE_GITHUB_WEB_URL": "https://github.com",
                    },
                    clear=False,
                ),
                patch.object(checkout.github_auth, "app_api_configured", return_value=True),
                patch.object(
                    checkout.github_auth,
                    "create_installation_access_token",
                    return_value={"token": "ghs_secret_token"},
                ),
                patch.object(checkout, "run_git") as run_git,
            ):
                path = checkout.prepare_checkout("sc_123", scan, lambda: False)

            command = run_git.call_args.args[0]
            extra_env = run_git.call_args.kwargs["extra_env"]
            self.assertTrue(path.startswith(tmpdir))
            self.assertIn(os.path.join("usr_1", "sc_123"), path)
            self.assertEqual(command[:2], ["git", "clone"])
            self.assertNotIn("push", command)
            self.assertNotIn("ghs_secret_token", " ".join(command))
            self.assertNotIn("ghs_secret_token", " ".join(extra_env.values()))
            self.assertIn("http.extraHeader", extra_env.values())

    def test_prepare_checkout_sanitizes_legacy_scan_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            scan = {
                "userId": "usr_1",
                "repo": "owner/repo",
                "branch": "main\r\nX-Injected: bad",
                "commit": {"sha": "abc1234"},
                "installationId": 123,
                "cloneUrl": "https://github.com/owner/repo.git\r\nX-Injected: bad",
            }
            with (
                patch.dict(
                    os.environ,
                    {
                        "PULLWISE_CHECKOUT_ROOT": tmpdir,
                        "PULLWISE_GITHUB_WEB_URL": "https://github.com",
                    },
                    clear=False,
                ),
                patch.object(checkout.github_auth, "app_api_configured", return_value=True),
                patch.object(
                    checkout.github_auth,
                    "create_installation_access_token",
                    return_value={"token": "ghs_secret_token"},
                ) as create_token,
                patch.object(checkout, "run_git") as run_git,
            ):
                try:
                    checkout.prepare_checkout("sc_123", scan, lambda: False)
                except RuntimeError as exc:
                    self.fail(f"prepare_checkout should sanitize legacy scan metadata: {exc}")

            create_token.assert_called_once_with("123")
            command = run_git.call_args.args[0]
            self.assertEqual(command[command.index("--branch") + 1], "main")
            self.assertEqual(command[-2], "https://github.com/owner/repo.git")
            self.assertEqual(run_git.call_count, 1)

    def test_checkout_root_defaults_inside_server_state_directory(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            root = os.path.abspath(checkout.checkout_root())

        self.assertEqual(
            root,
            os.path.abspath(os.path.join(checkout.project_root(), ".pullwise", "checkouts")),
        )

    def test_git_auth_env_disables_system_and_user_git_config(self) -> None:
        git_env = checkout.git_auth_env("ghs_secret_token")

        self.assertEqual(git_env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(os.path.normcase(git_env["GIT_CONFIG_GLOBAL"]), os.path.normcase(os.devnull))
        self.assertEqual(git_env["GIT_CONFIG_KEY_0"], "http.extraHeader")

    def test_prepare_checkout_runs_clone_from_server_managed_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            scan = {
                "userId": "usr_1",
                "repo": "owner/repo",
                "branch": "main",
                "commit": "pending",
                "installationId": "123",
                "cloneUrl": "https://github.com/owner/repo.git",
            }
            with (
                patch.dict(
                    os.environ,
                    {
                        "PULLWISE_CHECKOUT_ROOT": tmpdir,
                        "PULLWISE_GITHUB_WEB_URL": "https://github.com",
                    },
                    clear=False,
                ),
                patch.object(checkout.github_auth, "app_api_configured", return_value=True),
                patch.object(
                    checkout.github_auth,
                    "create_installation_access_token",
                    return_value={"token": "ghs_secret_token"},
                ),
                patch.object(checkout, "run_git") as run_git,
            ):
                checkout.prepare_checkout("sc_123", scan, lambda: False)
                expected_cwd = checkout.workspace_path_for("usr_1", "sc_123")
                cwd_in_workspace = checkout.path_in_scan_workspace(expected_cwd, "usr_1", "sc_123")

            clone_cwd = run_git.call_args.kwargs["cwd"]
            self.assertEqual(clone_cwd, expected_cwd)
            self.assertTrue(cwd_in_workspace)

    def test_run_git_rejects_cwd_outside_checkout_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                with self.assertRaisesRegex(RuntimeError, "checkout root"):
                    checkout.run_git(
                        ["git", "status"],
                        cwd=os.path.dirname(tmpdir),
                        extra_env={},
                        is_cancelled=lambda: False,
                        action="status",
                    )

    def test_run_git_decodes_subprocess_output_as_utf8_with_replacement(self) -> None:
        class FakeProcess:
            returncode = 0

            def communicate(self, timeout: float) -> tuple[str, str]:
                return "", ""

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False),
                patch("pullwise_server.checkout.subprocess.Popen", return_value=FakeProcess()) as popen,
            ):
                checkout.run_git(
                    ["git", "status"],
                    cwd=tmpdir,
                    extra_env={},
                    is_cancelled=lambda: False,
                    action="status",
                )

        self.assertEqual(popen.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(popen.call_args.kwargs["errors"], "replace")

    def test_repository_authorization_requires_explicit_repo_match(self) -> None:
        self.assertFalse(app.repository_is_authorized(None, "owner/repo"))
        self.assertFalse(app.repository_is_authorized({"repositoriesNeedSync": True}, "owner/repo"))
        self.assertTrue(app.repository_is_authorized({"repositories": ["owner/repo"]}, "owner/repo"))
        self.assertFalse(app.repository_is_authorized({"repositories": ["owner/other"]}, "owner/repo"))
        self.assertTrue(
            app.repository_is_authorized(
                {"repositoryItems": [{"fullName": "owner/repo"}]},
                "owner/repo",
            )
        )


if __name__ == "__main__":
    unittest.main()
