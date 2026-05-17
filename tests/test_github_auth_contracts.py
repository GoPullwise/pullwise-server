from __future__ import annotations

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
        self.assertIn("User-Agent", headers)

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
