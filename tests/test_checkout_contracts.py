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
