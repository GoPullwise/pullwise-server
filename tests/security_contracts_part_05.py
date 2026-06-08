from __future__ import annotations

try:
    import security_contracts_base as _security_contracts_base
except ModuleNotFoundError:  # pragma: no cover - package-style unittest invocation
    from . import security_contracts_base as _security_contracts_base

globals().update(
    {name: getattr(_security_contracts_base, name) for name in dir(_security_contracts_base) if not name.startswith("_")}
)


class SecurityContractsPart05Test(SecurityContractsBase):
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
    def test_repositories_sync_reuses_connected_access_without_force(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
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
                }
            ],
            "installations": [
                {
                    "installationId": "111",
                    "installationAccount": "octocat",
                    "repositorySelection": "selected",
                    "repositoryCount": 1,
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

        with patch("pullwise_server.github_auth.list_current_app_installations_for_user") as list_installations:
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertFalse(handler.payload["needsAuthorization"])
        self.assertEqual(handler.payload["items"][0]["fullName"], "octocat/private-repo")
        list_installations.assert_not_called()
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
        handler = RouteHarness("/repositories/sync", {"force": True}, cookie="pw_session=ses_1")

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
        self.assertIsInstance(github_access.get("repositoriesSyncedAt"), int)
        list_installations.assert_called_once_with("gho_user")
        self.assertEqual(
            [call.args for call in list_user_repositories.call_args_list],
            [("gho_user", "111"), ("gho_user", "222")],
        )
    def test_repositories_sync_can_refresh_only_target_installation_with_selected_identity(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubIdentities"] = [
            {
                "id": "ghi_bob",
                "githubUserId": "2",
                "githubLogin": "bob",
                "accessToken": "gho_bob",
                "status": "active",
            }
        ]
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "scope": "mixed",
            "repositorySelection": "selected",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
            "installationIds": ["111", "222"],
            "installationAccounts": ["octocat", "acme"],
            "repositories": ["octocat/private-repo", "acme/old-service"],
            "repositoryItems": [
                {"fullName": "octocat/private-repo", "installationId": "111"},
                {"fullName": "acme/old-service", "installationId": "222"},
            ],
            "installations": [
                {
                    "installationId": "111",
                    "installationAccount": "octocat",
                    "installationTargetType": "User",
                    "repositorySelection": "selected",
                    "repositoryCount": 1,
                },
                {
                    "installationId": "222",
                    "installationAccount": "acme",
                    "installationTargetType": "Organization",
                    "repositorySelection": "selected",
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
            "/repositories/sync",
            {"installationId": "222", "githubIdentityId": "ghi_bob"},
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
            patch("pullwise_server.github_auth.app_api_configured", return_value=False),
            patch(
                "pullwise_server.github_auth.list_current_app_installations_for_user",
                return_value=[
                    {
                        "id": 222,
                        "repository_selection": "selected",
                        "target_type": "Organization",
                        "account": {"login": "acme"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
                    }
                ],
            ) as list_installations,
            patch(
                "pullwise_server.github_auth.list_user_installation_repositories",
                return_value=[{"id": "repo_service", "name": "service", "fullName": "acme/service"}],
            ) as list_user_repositories,
        ):
            app.PullwiseHandler.route(handler, "POST")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertFalse(handler.payload["needsAuthorization"])
        self.assertEqual(github_access["repositories"], ["octocat/private-repo", "acme/service"])
        self.assertEqual(
            [item["fullName"] for item in handler.payload["items"]],
            ["octocat/private-repo", "acme/service"],
        )
        list_installations.assert_called_once_with("gho_bob")
        list_user_repositories.assert_called_once_with("gho_bob", "222")
        access = app.USERS["usr_1"]["githubIdentityInstallationAccess"][0]
        self.assertEqual(access["githubIdentityId"], "ghi_bob")
        self.assertEqual(access["githubAppInstallationId"], "222")
        self.assertTrue(access["canAccess"])
    def test_repositories_sync_binds_pending_add_flow_with_selected_identity(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubLogin"] = "alice"
        app.USERS["usr_1"]["githubAccessToken"] = "gho_alice"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "authorizedUserId": "usr_1",
            "authorizedGithubLogin": "alice",
            "installationIds": ["111"],
            "installationAccounts": ["alice"],
            "repositories": ["alice/service"],
            "repositoryItems": [{"fullName": "alice/service", "installationId": "111"}],
            "installations": [
                {
                    "installationId": "111",
                    "installationAccount": "alice",
                    "installationTargetType": "User",
                    "repositorySelection": "selected",
                    "repositoryCount": 1,
                }
            ],
            "repositoriesNeedSync": False,
        }
        app.USERS["usr_1"]["githubIdentities"] = [
            {
                "id": "ghi_bob",
                "userId": "usr_1",
                "githubUserId": "2",
                "githubLogin": "bob",
                "login": "bob",
                "accessToken": "gho_bob",
                "status": "active",
            }
        ]
        state = app.remember_github_state(
            "install",
            "https://app.pullwise.dev/?screen=repos",
            userId="usr_1",
            requestedScope="selected",
            selectedGithubIdentityId="ghi_bob",
        )
        app.USERS["usr_1"]["githubRepositoryAccessPending"] = {
            "state": state,
            "startedAt": app.now(),
            "expiresAt": app.now() + app.GITHUB_STATE_MAX_AGE,
            "previousInstallationId": "111",
            "add": True,
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
                        "id": 222,
                        "repository_selection": "selected",
                        "target_type": "User",
                        "account": {"login": "bob"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
                    }
                ],
            ) as list_installations,
            patch(
                "pullwise_server.github_auth.list_user_installation_repositories",
                return_value=[{"id": "repo_bob_private", "name": "private", "fullName": "bob/private"}],
            ) as list_user_repositories,
        ):
            app.PullwiseHandler.route(handler, "POST")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertFalse(handler.payload["needsAuthorization"])
        self.assertEqual(
            [item["fullName"] for item in handler.payload["items"]],
            ["alice/service", "bob/private"],
        )
        self.assertEqual(github_access["installationIds"], ["111", "222"])
        self.assertNotIn("githubRepositoryAccessPending", app.USERS["usr_1"])
        list_installations.assert_called_once_with("gho_bob")
        list_user_repositories.assert_called_once_with("gho_bob", "222")


__all__ = ["SecurityContractsPart05Test"]
