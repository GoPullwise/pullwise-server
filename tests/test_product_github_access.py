from __future__ import annotations

import unittest
from unittest.mock import patch

from pullwise_server import app


class ProductGitHubAccessTest(unittest.TestCase):
    def test_repository_access_payload_excludes_retired_scan_actions_and_quota(self) -> None:
        item = {"id": "acme/lib", "fullName": "acme/lib", "githubRepoId": "123"}
        with patch.object(app.db, "get_repository_by_github_repo_id") as old_lookup:
            payload = app.repository_item_for_access(item)
        old_lookup.assert_not_called()
        self.assertEqual(payload["githubRepoId"], "123")
        self.assertNotIn("scanAction", payload)
        self.assertNotIn("quota", payload)
        self.assertNotIn("href", payload)
        self.assertNotIn("repoId", payload)

    def test_read_only_installation_is_accepted_for_fact_discovery(self) -> None:
        installation = {
            "permissions": {"metadata": "read", "contents": "read",
                            "pull_requests": "read", "actions": "read"},
            "repository_selection": "selected",
            "account": {"login": "acme"},
        }
        with patch.object(app.github_auth, "app_api_configured", return_value=True), \
             patch.object(app.github_auth, "fetch_installation", return_value=installation), \
             patch.object(app.github_auth, "list_installation_repositories", return_value=[]):
            access = app.github_repository_access_for_installation("installation-1")
        self.assertEqual(access["installationPermissions"], installation["permissions"])
        self.assertEqual(access["repositories"], [])


if __name__ == "__main__":
    unittest.main()
