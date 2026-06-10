from __future__ import annotations

import math
import os
import unittest
from unittest.mock import Mock, patch

from pullwise_server import github_auth


class GitHubAuthContractsTest(unittest.TestCase):
    def test_oauth_authorize_url_can_force_account_picker_prompt(self) -> None:
        client = Mock()
        client.create_authorization_url.return_value = ("https://github.com/login/oauth/authorize", "state")

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                    "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                },
                clear=True,
            ),
            patch.object(github_auth, "oauth_session", return_value=client),
        ):
            url = github_auth.build_oauth_authorize_url(
                "https://api.pullwise.dev/auth/github/callback",
                "state",
                "verifier",
                prompt="select_account",
            )

        self.assertEqual(url, "https://github.com/login/oauth/authorize")
        self.assertEqual(client.create_authorization_url.call_args.kwargs["prompt"], "select_account")

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

    def test_list_repository_branches_reads_paginated_branch_names(self) -> None:
        first = Mock()
        first.raise_for_status.return_value = None
        first.json.return_value = [{"name": "main"}, {"name": "release/1.0"}]
        first.links = {"next": {"url": "https://api.github.test/repos/acme/api/branches?page=2"}}
        second = Mock()
        second.raise_for_status.return_value = None
        second.json.return_value = [{"name": "develop"}, {"name": "bad\r\nbranch"}, {"name": ""}]
        second.links = {}

        with (
            patch.dict(os.environ, {"PULLWISE_GITHUB_API_URL": "https://api.github.test"}, clear=True),
            patch("pullwise_server.github_auth.requests.get", side_effect=[first, second]) as get,
        ):
            branches = github_auth.list_repository_branches("ghs_installation", "acme/api")

        self.assertEqual(branches, ["main", "release/1.0", "develop"])
        self.assertEqual(
            [call.args[0] for call in get.call_args_list],
            [
                "https://api.github.test/repos/acme/api/branches",
                "https://api.github.test/repos/acme/api/branches?page=2",
            ],
        )
        self.assertEqual(get.call_args_list[0].kwargs["params"], {"per_page": 100})
        self.assertIsNone(get.call_args_list[1].kwargs["params"])
        self.assertEqual(get.call_args_list[0].kwargs["headers"]["Authorization"], "Bearer ghs_installation")

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
        installation.html_url = "https://github.com/settings/installations/123"
        installation.permissions = permissions

        payload = github_auth.installation_to_dict(installation)

        self.assertEqual(payload["account"]["login"], "octocat")
        self.assertEqual(payload["app_slug"], "pullwise")
        self.assertEqual(payload["html_url"], "https://github.com/settings/installations/123")
        self.assertEqual(payload["permissions"]["contents"], "read")

    def test_installation_to_dict_ignores_malformed_permission_levels(self) -> None:
        installation = {
            "id": 123,
            "account": {"login": "octocat"},
            "permissions": {
                "metadata": "read",
                "contents": {"level": "write"},
                "pull_requests": ["write"],
                "statuses": True,
            },
        }

        payload = github_auth.installation_payload_to_dict(installation)

        self.assertEqual(payload["permissions"], {"metadata": "read"})

    def test_installation_payload_to_dict_sanitizes_malformed_identity_fields(self) -> None:
        installation = {
            "id": {"value": 123},
            "repository_selection": ["selected"],
            "target_type": {"type": "User"},
            "account": {"login": {"name": "octocat"}},
            "app_slug": {"slug": "pullwise"},
            "app_id": {"id": 456},
            "html_url": "javascript:alert(1)",
            "permissions": {"metadata": "read"},
        }

        payload = github_auth.installation_payload_to_dict(installation)

        self.assertIsNone(payload["id"])
        self.assertIsNone(payload["repository_selection"])
        self.assertIsNone(payload["target_type"])
        self.assertEqual(payload["account"], {})
        self.assertIsNone(payload["app_slug"])
        self.assertIsNone(payload["app_id"])
        self.assertIsNone(payload["html_url"])
        self.assertEqual(payload["permissions"], {"metadata": "read"})

    def test_installation_to_dict_sanitizes_malformed_identity_fields(self) -> None:
        account = Mock()
        account.login = {"name": "octocat"}
        installation = Mock()
        installation.id = {"value": 123}
        installation.repository_selection = ["selected"]
        installation.target_type = {"type": "User"}
        installation.account = account
        installation.app_slug = {"slug": "pullwise"}
        installation.app_id = {"id": 456}
        installation.html_url = "javascript:alert(1)"
        installation.permissions = {"metadata": "read"}

        payload = github_auth.installation_to_dict(installation)

        self.assertIsNone(payload["id"])
        self.assertIsNone(payload["repository_selection"])
        self.assertIsNone(payload["target_type"])
        self.assertEqual(payload["account"], {})
        self.assertIsNone(payload["app_slug"])
        self.assertIsNone(payload["app_id"])
        self.assertIsNone(payload["html_url"])
        self.assertEqual(payload["permissions"], {"metadata": "read"})

    def test_list_current_app_installations_matches_pygithub_installations_by_app_id(self) -> None:
        account = Mock()
        account.login = "octocat"
        installation = Mock()
        installation.id = 123
        installation.repository_selection = "selected"
        installation.target_type = "User"
        installation.account = account
        installation.app_slug = None
        installation.app_id = 456
        installation.html_url = "https://github.com/settings/installations/123"
        installation.permissions = {"metadata": "read", "contents": "read"}
        user = Mock()
        user.get_installations.return_value = [installation]
        github = Mock()
        github.get_user.return_value = user

        with (
            patch.dict(os.environ, {"PULLWISE_GITHUB_APP_ID": "456"}, clear=True),
            patch.object(github_auth, "github_client", return_value=github),
        ):
            installations = github_auth.list_current_app_installations_for_user("gho_user")

        self.assertEqual([item["id"] for item in installations], [123])
        github.close.assert_called_once()

    def test_app_api_configured_rejects_malformed_app_id(self) -> None:
        for app_id in ["abc", "0", "-1"]:
            with self.subTest(app_id=app_id):
                with patch.dict(
                    os.environ,
                    {
                        "PULLWISE_GITHUB_APP_ID": app_id,
                        "PULLWISE_GITHUB_APP_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----",
                    },
                    clear=True,
                ):
                    self.assertFalse(github_auth.app_api_configured())

    def test_app_private_key_rejects_invalid_private_key_base64(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PULLWISE_GITHUB_APP_ID": "456",
                "PULLWISE_GITHUB_APP_PRIVATE_KEY_BASE64": "%%%",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(github_auth.GitHubError, "not valid base64"):
                github_auth.app_private_key()

    def test_exchange_oauth_code_rejects_malformed_access_token(self) -> None:
        client = Mock()
        client.fetch_token.return_value = {"access_token": {"token": "bad"}, "token_type": "bearer"}

        with patch.object(github_auth, "oauth_session", return_value=client):
            with self.assertRaisesRegex(github_auth.GitHubError, "access token"):
                github_auth.exchange_oauth_code("code", "https://api.pullwise.dev/auth/github/callback", "verifier", "state")

    def test_fetch_primary_email_skips_malformed_email_items(self) -> None:
        emails = [
            "not an email object",
            {"verified": True, "primary": True, "email": {"address": "bad@example.com"}},
            {"verified": True, "primary": True, "email": "octocat@example.com"},
        ]

        with (
            patch.object(github_auth, "oauth_session", return_value=Mock()),
            patch.object(github_auth, "authlib_get_json", return_value=emails),
        ):
            try:
                primary_email = github_auth.fetch_primary_email("gho_user")
            except AttributeError as exc:
                self.fail(f"fetch_primary_email should skip malformed email items: {exc}")

        self.assertEqual(primary_email, "octocat@example.com")

    def test_fetch_primary_email_ignores_unverified_email_records(self) -> None:
        emails = [
            {"verified": False, "primary": True, "email": "admin@example.com"},
            {"verified": False, "primary": False, "email": "backup@example.com"},
        ]

        with (
            patch.object(github_auth, "oauth_session", return_value=Mock()),
            patch.object(github_auth, "authlib_get_json", return_value=emails),
        ):
            primary_email = github_auth.fetch_primary_email("gho_user")

        self.assertIsNone(primary_email)

    def test_fetch_primary_email_skips_github_noreply_email_records(self) -> None:
        emails = [
            {"verified": True, "primary": True, "email": "123+octocat@users.noreply.github.com"},
            {"verified": True, "primary": False, "email": "octocat@example.com"},
        ]

        with (
            patch.object(github_auth, "oauth_session", return_value=Mock()),
            patch.object(github_auth, "authlib_get_json", return_value=emails),
        ):
            primary_email = github_auth.fetch_primary_email("gho_user")

        self.assertEqual(primary_email, "octocat@example.com")

    def test_fetch_user_profile_keeps_all_verified_emails_for_admin_allowlist(self) -> None:
        profile_response = {"login": "octocat", "email": "primary@example.com"}
        email_response = [
            {"verified": True, "primary": True, "email": "primary@example.com"},
            {"verified": True, "primary": False, "email": "Admin@Example.com"},
            {"verified": False, "primary": False, "email": "unverified@example.com"},
            {"verified": True, "primary": False, "email": "123+octocat@users.noreply.github.com"},
        ]

        with (
            patch.object(github_auth, "oauth_session", return_value=Mock()),
            patch.object(github_auth, "authlib_get_json", side_effect=[profile_response, email_response]) as get_json,
        ):
            profile = github_auth.fetch_user_profile("gho_user")

        self.assertEqual(profile["primaryEmail"], "primary@example.com")
        self.assertEqual(profile["verifiedEmails"], ["primary@example.com", "Admin@Example.com"])
        self.assertEqual([call.args[1] for call in get_json.call_args_list], ["/user", "/user/emails"])

    def test_fetch_user_profile_uses_primary_email_fallback_for_malformed_profile_email(self) -> None:
        profile_response = {"login": "octocat", "email": {"address": "bad@example.com"}}
        email_response = [{"verified": True, "primary": True, "email": "octocat@example.com"}]

        with (
            patch.object(github_auth, "oauth_session", return_value=Mock()),
            patch.object(github_auth, "authlib_get_json", side_effect=[profile_response, email_response]) as get_json,
        ):
            profile = github_auth.fetch_user_profile("gho_user")

        self.assertEqual(profile["primaryEmail"], "octocat@example.com")
        self.assertEqual([call.args[1] for call in get_json.call_args_list], ["/user", "/user/emails"])

    def test_fetch_user_profile_uses_primary_email_fallback_for_github_noreply_email(self) -> None:
        profile_response = {"login": "octocat", "email": "octocat@users.noreply.github.com"}
        email_response = [{"verified": True, "primary": True, "email": "octocat@example.com"}]

        with (
            patch.object(github_auth, "oauth_session", return_value=Mock()),
            patch.object(github_auth, "authlib_get_json", side_effect=[profile_response, email_response]) as get_json,
        ):
            profile = github_auth.fetch_user_profile("gho_user")

        self.assertEqual(profile["primaryEmail"], "octocat@example.com")
        self.assertEqual([call.args[1] for call in get_json.call_args_list], ["/user", "/user/emails"])

    def test_fetch_user_profile_rejects_malformed_profile_body(self) -> None:
        with (
            patch.object(github_auth, "oauth_session", return_value=Mock()),
            patch.object(github_auth, "authlib_get_json", return_value=[]),
        ):
            with self.assertRaisesRegex(github_auth.GitHubError, "user profile response"):
                github_auth.fetch_user_profile("gho_user")

    def test_fetch_user_profile_rejects_malformed_login_field(self) -> None:
        profile_response = {"login": {"name": "octocat"}, "email": "octocat@example.com"}

        with (
            patch.object(github_auth, "oauth_session", return_value=Mock()),
            patch.object(github_auth, "authlib_get_json", return_value=profile_response),
        ):
            with self.assertRaisesRegex(github_auth.GitHubError, "missing login"):
                github_auth.fetch_user_profile("gho_user")

    def test_installation_matches_configured_app_slug_case_insensitively(self) -> None:
        self.assertTrue(
            github_auth.installation_matches_configured_app(
                {"app_slug": "gopullwise", "app_id": "456"},
                "GoPullwise",
                "",
            )
        )

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

    def test_repo_payload_preserves_stable_ids_owner_and_fork_source(self) -> None:
        payload = github_auth.repo_payload_to_pullwise(
            {
                "id": 1296269,
                "node_id": "R_kgDOABC",
                "name": "Hello-World",
                "full_name": "octocat/Hello-World",
                "owner": {"id": 583231, "login": "octocat", "type": "User"},
                "fork": True,
                "parent": {"id": 2, "node_id": "R_parent", "full_name": "octocat/Parent"},
                "source": {"id": 1, "node_id": "R_source", "full_name": "octocat/Source"},
                "permissions": {"pull": True},
            }
        )

        self.assertEqual(payload["githubRepoId"], "1296269")
        self.assertEqual(payload["githubNodeId"], "R_kgDOABC")
        self.assertEqual(payload["ownerLogin"], "octocat")
        self.assertEqual(payload["ownerId"], "583231")
        self.assertTrue(payload["fork"])
        self.assertEqual(payload["parentGithubRepoId"], "2")
        self.assertEqual(payload["sourceGithubRepoId"], "1")

    def test_list_installation_repositories_sanitizes_malformed_star_counts(self) -> None:
        response = Mock()
        response.json.return_value = {
            "repositories": [
                {
                    "id": 1,
                    "name": "MalformedObject",
                    "full_name": "octocat/MalformedObject",
                    "stargazers_count": {"count": 80},
                },
                {
                    "id": 2,
                    "name": "NegativeCount",
                    "full_name": "octocat/NegativeCount",
                    "stargazers_count": -5,
                },
                {
                    "id": 3,
                    "name": "NonFiniteCount",
                    "full_name": "octocat/NonFiniteCount",
                    "stargazers_count": math.inf,
                },
            ]
        }
        response.links = {}
        response.raise_for_status.return_value = None

        with (
            patch.object(github_auth, "create_installation_access_token", return_value={"token": "ghs_123"}),
            patch("pullwise_server.github_auth.requests.get", return_value=response),
        ):
            repositories = github_auth.list_installation_repositories("123")

        self.assertEqual([repo["stars"] for repo in repositories], ["-", "-", "-"])

    def test_list_installation_repositories_rejects_non_object_success_response(self) -> None:
        response = Mock()
        response.json.return_value = []
        response.links = {}
        response.raise_for_status.return_value = None

        with (
            patch.object(github_auth, "create_installation_access_token", return_value={"token": "ghs_123"}),
            patch("pullwise_server.github_auth.requests.get", return_value=response),
        ):
            with self.assertRaisesRegex(github_auth.GitHubError, "repositories response"):
                github_auth.list_installation_repositories("123")

    def test_list_installation_repositories_skips_non_object_repository_items(self) -> None:
        response = Mock()
        response.json.return_value = {
            "repositories": [
                ["not", "a", "repo"],
                "bad repo",
                {
                    "id": 1296269,
                    "name": "Hello-World",
                    "full_name": "octocat/Hello-World",
                    "stargazers_count": 80,
                    "permissions": {"pull": True},
                },
            ]
        }
        response.links = {}
        response.raise_for_status.return_value = None

        with (
            patch.object(github_auth, "create_installation_access_token", return_value={"token": "ghs_123"}),
            patch("pullwise_server.github_auth.requests.get", return_value=response),
        ):
            repositories = github_auth.list_installation_repositories("123")

        self.assertEqual([repo["fullName"] for repo in repositories], ["octocat/Hello-World"])

    def test_list_installation_repositories_skips_items_without_valid_full_name(self) -> None:
        response = Mock()
        response.json.return_value = {
            "repositories": [
                {
                    "id": 1,
                    "name": "MissingFullName",
                    "stargazers_count": 3,
                },
                {
                    "id": 2,
                    "name": "ObjectFullName",
                    "full_name": {"owner": "octocat", "repo": "ObjectFullName"},
                    "stargazers_count": 5,
                },
                {
                    "id": 3,
                    "name": "Hello-World",
                    "full_name": "octocat/Hello-World",
                    "stargazers_count": 80,
                },
            ]
        }
        response.links = {}
        response.raise_for_status.return_value = None

        with (
            patch.object(github_auth, "create_installation_access_token", return_value={"token": "ghs_123"}),
            patch("pullwise_server.github_auth.requests.get", return_value=response),
        ):
            repositories = github_auth.list_installation_repositories("123")

        self.assertEqual([repo["fullName"] for repo in repositories], ["octocat/Hello-World"])

    def test_list_installation_repositories_sanitizes_malformed_repository_urls(self) -> None:
        response = Mock()
        response.json.return_value = {
            "repositories": [
                {
                    "id": 1296269,
                    "name": "Hello-World",
                    "full_name": "octocat/Hello-World",
                    "html_url": "javascript:alert(1)",
                    "clone_url": {"url": "https://github.com/octocat/Hello-World.git"},
                    "permissions": {"pull": True},
                },
                {
                    "id": 1296270,
                    "name": "UnsafeHost",
                    "full_name": "octocat/UnsafeHost",
                    "html_url": "https://evil.example/octocat/UnsafeHost",
                    "clone_url": "https://evil.example/octocat/UnsafeHost.git",
                    "permissions": {"pull": True},
                },
            ]
        }
        response.links = {}
        response.raise_for_status.return_value = None

        with (
            patch.object(github_auth, "create_installation_access_token", return_value={"token": "ghs_123"}),
            patch("pullwise_server.github_auth.requests.get", return_value=response),
        ):
            repositories = github_auth.list_installation_repositories("123")

        self.assertEqual([repo["htmlUrl"] for repo in repositories], [None, None])
        self.assertEqual([repo["cloneUrl"] for repo in repositories], [None, None])

    def test_list_installation_repositories_sanitizes_malformed_repository_text_fields(self) -> None:
        response = Mock()
        response.json.return_value = {
            "repositories": [
                {
                    "id": {"repo": 1},
                    "name": {"display": "Bad Name"},
                    "full_name": "octocat/Hello-World",
                    "description": {"text": "Bad description"},
                    "language": ["Python"],
                    "default_branch": {"name": "main"},
                    "updated_at": {"date": "2026-05-17"},
                    "permissions": {"pull": True},
                }
            ]
        }
        response.links = {}
        response.raise_for_status.return_value = None

        with (
            patch.object(github_auth, "create_installation_access_token", return_value={"token": "ghs_123"}),
            patch("pullwise_server.github_auth.requests.get", return_value=response),
        ):
            repositories = github_auth.list_installation_repositories("123")

        self.assertEqual(repositories[0]["id"], "octocat/Hello-World")
        self.assertEqual(repositories[0]["name"], "Hello-World")
        self.assertEqual(repositories[0]["desc"], "")
        self.assertEqual(repositories[0]["description"], "")
        self.assertEqual(repositories[0]["lang"], "-")
        self.assertEqual(repositories[0]["defaultBranch"], "main")
        self.assertEqual(repositories[0]["updated"], "")

    def test_list_installation_repositories_sanitizes_malformed_private_flags(self) -> None:
        response = Mock()
        response.json.return_value = {
            "repositories": [
                {"id": 1, "name": "StringFalse", "full_name": "octocat/StringFalse", "private": "false"},
                {"id": 2, "name": "ObjectPrivate", "full_name": "octocat/ObjectPrivate", "private": {"private": True}},
                {"id": 3, "name": "PrivateRepo", "full_name": "octocat/PrivateRepo", "private": True},
            ]
        }
        response.links = {}
        response.raise_for_status.return_value = None

        with (
            patch.object(github_auth, "create_installation_access_token", return_value={"token": "ghs_123"}),
            patch("pullwise_server.github_auth.requests.get", return_value=response),
        ):
            repositories = github_auth.list_installation_repositories("123")

        self.assertEqual([repo["private"] for repo in repositories], [False, False, True])

    def test_list_user_installation_repositories_uses_official_user_installation_endpoint(self) -> None:
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

        with patch("pullwise_server.github_auth.requests.get", return_value=response) as get:
            repositories = github_auth.list_user_installation_repositories("ghu_user", "123")

        self.assertEqual(repositories[0]["fullName"], "octocat/Hello-World")
        self.assertEqual(repositories[0]["cloneUrl"], "https://github.com/octocat/Hello-World.git")
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], "https://api.github.com/user/installations/123/repositories")
        self.assertEqual(get.call_args.kwargs["params"], {"per_page": 100})
        headers = get.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer ghu_user")
        self.assertEqual(headers["Accept"], "application/vnd.github+json")
        self.assertIn("X-GitHub-Api-Version", headers)


if __name__ == "__main__":
    unittest.main()
