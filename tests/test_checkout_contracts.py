from __future__ import annotations

import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from pullwise_server import app, checkout


class CheckoutContractsTest(unittest.TestCase):
    def test_clone_url_must_stay_on_configured_github_host(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_GITHUB_WEB_URL": "https://github.com"}, clear=False):
            self.assertEqual(
                checkout.clone_url_for("owner/repo", "https://github.com/owner/repo.git"),
                "https://github.com/owner/repo.git",
            )
            with self.assertRaisesRegex(RuntimeError, "host"):
                checkout.clone_url_for("owner/repo", "https://evil.example/owner/repo.git")

    def test_clone_url_must_match_authorized_repository_path(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_GITHUB_WEB_URL": "https://github.com"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "repository"):
                checkout.clone_url_for("owner/repo", "https://github.com/other/repo.git")
            with self.assertRaisesRegex(RuntimeError, "repository"):
                checkout.clone_url_for("owner/repo", "https://github.com/owner/other.git")
            self.assertEqual(
                checkout.scan_clone_url_for("owner/repo", "https://github.com/other/repo.git"),
                "https://github.com/owner/repo.git",
            )

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

    def test_prepare_checkout_sanitizes_malformed_scan_source_metadata(self) -> None:
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
                    self.fail(f"prepare_checkout should sanitize malformed scan metadata: {exc}")

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

    def test_cleanup_scan_workspace_retries_readonly_git_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                repo_path = checkout.checkout_path_for("usr_1", "sc_1", "owner/repo")
                object_dir = os.path.join(repo_path, ".git", "objects", "aa")
                os.makedirs(object_dir, exist_ok=True)
                object_path = os.path.join(object_dir, "object")
                with open(object_path, "w", encoding="utf-8") as handle:
                    handle.write("git object")
                os.chmod(object_path, stat.S_IREAD)

                try:
                    checkout.cleanup_scan_workspace("usr_1", "sc_1")
                finally:
                    if os.path.exists(object_path):
                        os.chmod(object_path, stat.S_IREAD | stat.S_IWRITE)

                self.assertFalse(os.path.exists(checkout.workspace_path_for("usr_1", "sc_1")))

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


class RepositoryFingerprintTest(unittest.TestCase):
    def test_repository_fingerprint_hashes_dependency_and_source_files(self) -> None:
        with tempfile.TemporaryDirectory() as repo_path:
            os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
            with open(os.path.join(repo_path, "package-lock.json"), "w", encoding="utf-8") as handle:
                handle.write('{"lockfileVersion": 3}\n')
            with open(os.path.join(repo_path, "package.json"), "w", encoding="utf-8") as handle:
                handle.write('{"name": "demo"}\n')
            with open(os.path.join(repo_path, "src", "app.py"), "w", encoding="utf-8") as handle:
                handle.write("print('ok')\n")
            os.makedirs(os.path.join(repo_path, ".git"), exist_ok=True)
            with open(os.path.join(repo_path, ".git", "ignored.py"), "w", encoding="utf-8") as handle:
                handle.write("print('ignored')\n")

            with patch.object(checkout, "run_git_output", return_value="b" * 40):
                fingerprint = checkout.repository_fingerprint(
                    repo_path,
                    lambda: False,
                    head_sha="a" * 40,
                )

        self.assertEqual(fingerprint["headSha"], "a" * 40)
        self.assertEqual(fingerprint["treeSha"], "b" * 40)
        self.assertRegex(fingerprint["lockfileHash"], r"^[0-9a-f]{64}$")
        self.assertRegex(fingerprint["manifestHash"], r"^[0-9a-f]{64}$")
        self.assertRegex(fingerprint["sourceFingerprint"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
