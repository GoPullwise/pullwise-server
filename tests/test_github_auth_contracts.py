from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

from pullwise_server import github_auth


class GitHubAuthContractsTest(unittest.TestCase):
    def test_authlib_rest_requests_use_official_github_headers(self) -> None:
        response = Mock()
        response.json.return_value = {"login": "octocat"}
        response.raise_for_status.return_value = None
        client = Mock()
        client.get.return_value = response

        payload = github_auth.authlib_get_json(client, "/user")

        self.assertEqual(payload["login"], "octocat")
        headers = client.get.call_args.kwargs["headers"]
        self.assertEqual(headers["Accept"], "application/vnd.github+json")
        self.assertIn("X-GitHub-Api-Version", headers)

    def test_app_slug_public_visibility_uses_official_get_app_endpoint(self) -> None:
        response = Mock()
        response.status_code = 200
        response.raise_for_status.return_value = None

        with (
            patch.dict(os.environ, {"PULLWISE_GITHUB_APP_SLUG": "pullwise"}, clear=True),
            patch("pullwise_server.github_auth.requests.get", return_value=response) as get,
        ):
            public_installable = github_auth.app_slug_publicly_installable()

        self.assertTrue(public_installable)
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], "https://api.github.com/apps/pullwise")
        headers = get.call_args.kwargs["headers"]
        self.assertEqual(headers["Accept"], "application/vnd.github+json")
        self.assertIn("X-GitHub-Api-Version", headers)
        self.assertIn("User-Agent", headers)

    def test_app_slug_public_visibility_returns_false_on_private_or_missing_slug(self) -> None:
        response = Mock()
        response.status_code = 404

        with (
            patch.dict(os.environ, {"PULLWISE_GITHUB_APP_SLUG": "gopullwise"}, clear=True),
            patch("pullwise_server.github_auth.requests.get", return_value=response),
        ):
            public_installable = github_auth.app_slug_publicly_installable()

        self.assertFalse(public_installable)
        response.raise_for_status.assert_not_called()

    def test_installation_to_dict_preserves_app_slug_and_permission_levels(self) -> None:
        account = Mock()
        account.login = "octocat"
        permissions = Mock()
        permissions.raw_data = {"metadata": "read", "contents": "read"}
        installation = Mock()
        installation.id = 123
        installation.repository_selection = "selected"
        installation.target_type = "User"
        installation.account = account
        installation.app_slug = "pullwise"
        installation.permissions = permissions

        payload = github_auth.installation_to_dict(installation)

        self.assertEqual(payload["account"]["login"], "octocat")
        self.assertEqual(payload["app_slug"], "pullwise")
        self.assertEqual(payload["permissions"]["contents"], "read")

    def test_list_installation_repositories_uses_official_installation_repositories_endpoint(self) -> None:
        response = Mock()
        response.json.return_value = {
            "repositories": [
                {
                    "id": 1296269,
                    "name": "Hello-World",
                    "full_name": "octocat/Hello-World",
                    "description": "This is your first repo!",
                    "language": "Python",
                    "private": False,
                    "stargazers_count": 80,
                    "default_branch": "main",
                    "updated_at": "2026-05-17T00:00:00Z",
                    "html_url": "https://github.com/octocat/Hello-World",
                    "clone_url": "https://github.com/octocat/Hello-World.git",
                    "permissions": {"admin": False, "push": False, "pull": True},
                }
            ]
        }
        response.links = {}
        response.raise_for_status.return_value = None

        with (
            patch.object(github_auth, "create_installation_access_token", return_value={"token": "ghs_123"}),
            patch("pullwise_server.github_auth.requests.get", return_value=response) as get,
        ):
            repositories = github_auth.list_installation_repositories("123")

        self.assertEqual(repositories[0]["fullName"], "octocat/Hello-World")
        self.assertEqual(repositories[0]["cloneUrl"], "https://github.com/octocat/Hello-World.git")
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], "https://api.github.com/installation/repositories")
        self.assertEqual(get.call_args.kwargs["params"], {"per_page": 100})
        headers = get.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer ghs_123")
        self.assertEqual(headers["Accept"], "application/vnd.github+json")
        self.assertIn("X-GitHub-Api-Version", headers)


if __name__ == "__main__":
    unittest.main()
