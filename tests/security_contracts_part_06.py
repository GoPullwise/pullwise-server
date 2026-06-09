from __future__ import annotations

try:
    import security_contracts_base as _security_contracts_base
except ModuleNotFoundError:  # pragma: no cover - package-style unittest invocation
    from . import security_contracts_base as _security_contracts_base

globals().update(
    {name: getattr(_security_contracts_base, name) for name in dir(_security_contracts_base) if not name.startswith("_")}
)


class SecurityContractsPart06Test(SecurityContractsBase):
    def test_repositories_list_does_not_migrate_single_installation_access_to_aggregate_model(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "installationId": "111",
            "installationAccount": "octocat",
            "installationTargetType": "User",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
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

        def installation_repositories(_token: str, installation_id: str) -> list[dict]:
            if installation_id == "111":
                return [{"id": "repo_private", "name": "private-repo", "fullName": "octocat/private-repo"}]
            if installation_id == "222":
                return [{"id": "repo_service", "name": "service", "fullName": "acme/service"}]
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
            ),
            patch("pullwise_server.github_auth.list_user_installation_repositories", side_effect=installation_repositories),
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertFalse(handler.payload["needsAuthorization"])
        self.assertEqual([item["fullName"] for item in handler.payload["items"]], ["octocat/private-repo"])
        self.assertNotIn("installationIds", app.USERS["usr_1"]["githubRepositoryAccess"])
    def test_repositories_list_repairs_stale_personal_installation_for_current_github_login(self) -> None:
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
            patch("pullwise_server.github_auth.app_api_configured", return_value=False),
            patch(
                "pullwise_server.github_auth.list_current_app_installations_for_user",
                return_value=[
                    {
                        "id": 111,
                        "repository_selection": "all",
                        "target_type": "User",
                        "account": {"login": "SanChai20"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
                    },
                    {
                        "id": 222,
                        "repository_selection": "all",
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
                        "id": "repo_dferryman_private",
                        "name": "private-repo",
                        "fullName": "DFerryman/private-repo",
                        "private": True,
                        "cloneUrl": "https://github.com/DFerryman/private-repo.git",
                    }
                ],
            ),
        ):
            app.PullwiseHandler.route(handler, "GET")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertFalse(handler.payload["needsAuthorization"])
        self.assertEqual(handler.payload["installationAccount"], "DFerryman")
        self.assertEqual(handler.payload["items"][0]["fullName"], "DFerryman/private-repo")
        self.assertNotIn("SanChai20/private-repo", github_access["repositories"])
    def test_repositories_sync_rebinds_pending_manage_to_current_user_installation(self) -> None:
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
        app.USERS["usr_1"]["githubRepositoryAccessPending"] = {
            "state": "pending_state",
            "startedAt": app.now(),
            "expiresAt": app.now() + app.GITHUB_STATE_MAX_AGE,
            "previousInstallationId": "111",
            "manage": True,
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
                        "id": 111,
                        "repository_selection": "all",
                        "target_type": "User",
                        "account": {"login": "SanChai20"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
                    },
                    {
                        "id": 222,
                        "repository_selection": "all",
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
                        "id": "repo_dferryman_private",
                        "name": "private-repo",
                        "fullName": "DFerryman/private-repo",
                        "private": True,
                        "cloneUrl": "https://github.com/DFerryman/private-repo.git",
                    }
                ],
            ) as list_user_repositories,
        ):
            app.PullwiseHandler.route(handler, "POST")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertFalse(handler.payload["needsAuthorization"])
        self.assertEqual(handler.payload["installationAccount"], "DFerryman")
        self.assertEqual(handler.payload["items"][0]["fullName"], "DFerryman/private-repo")
        self.assertEqual(github_access["installationId"], "222")
        self.assertEqual(github_access["installationAccount"], "DFerryman")
        self.assertNotIn("githubRepositoryAccessPending", app.USERS["usr_1"])
        list_user_repositories.assert_called_once_with("gho_user", "222")
    def test_repositories_sync_repairs_stale_personal_installation_for_current_github_login(self) -> None:
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
                        "id": 111,
                        "repository_selection": "all",
                        "target_type": "User",
                        "account": {"login": "SanChai20"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
                    },
                    {
                        "id": 222,
                        "repository_selection": "all",
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
                        "id": "repo_dferryman_private",
                        "name": "private-repo",
                        "fullName": "DFerryman/private-repo",
                        "private": True,
                        "cloneUrl": "https://github.com/DFerryman/private-repo.git",
                    }
                ],
            ),
        ):
            app.PullwiseHandler.route(handler, "POST")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["installationAccount"], "DFerryman")
        self.assertEqual(handler.payload["items"][0]["fullName"], "DFerryman/private-repo")
        self.assertEqual(github_access["installationId"], "222")
    def test_github_repository_installation_permissions_must_support_pull_request_creation(self) -> None:
        self.assertTrue(
            app.installation_supports_pull_request_creation(
                {"permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"}}
            )
        )
        self.assertFalse(
            app.installation_supports_pull_request_creation(
                {"permissions": {"metadata": "read", "contents": "read", "pull_requests": "write"}}
            )
        )
        self.assertFalse(
            app.installation_supports_pull_request_creation(
                {"permissions": {"metadata": "read", "contents": "write", "pull_requests": "read"}}
            )
        )
        self.assertFalse(app.installation_supports_pull_request_creation({"permissions": {"metadata": "read"}}))
    def test_repository_item_with_installation_context_preserves_installation_permissions(self) -> None:
        item = app.repository_item_with_installation_context(
            {"fullName": "owner/repo"},
            {
                "installationId": "123",
                "installationPermissions": {
                    "metadata": "read",
                    "contents": "write",
                    "pull_requests": "write",
                },
            },
        )

        self.assertEqual(item["installationId"], "123")
        self.assertEqual(item["installationPermissions"]["contents"], "write")
        self.assertEqual(item["installationPermissions"]["pull_requests"], "write")
    def test_unhandled_errors_do_not_echo_internal_exception_details(self) -> None:
        handler = RouteHarness("/boom")

        def boom(_path, _params, _segments):
            raise RuntimeError("secret-token-path")

        handler.handle_get = boom

        with patch.object(app.logger, "exception") as log_exception:
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.assertEqual(handler.payload["message"], "Server error.")
        log_exception.assert_called_once()
    def test_github_installation_callback_with_state_requires_pullwise_github_oauth_identity(self) -> None:
        app.USERS["usr_1"]["providers"] = ["email"]
        app.USERS["usr_1"].pop("githubAccessToken", None)
        app.USERS["usr_1"]["githubRepositoryAccess"] = None
        state = app.remember_github_state(
            "install",
            "https://app.pullwise.dev/?screen=repos",
            userId="usr_1",
            requestedScope="selected",
        )
        handler = RouteHarness(f"/integrations/github/callback?state={state}&installation_id=999")

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
            patch("pullwise_server.github_auth.user_can_access_installation") as user_can_access,
            patch(
                "pullwise_server.github_auth.fetch_installation",
                return_value={
                    "repository_selection": "selected",
                    "target_type": "User",
                    "account": {"login": "browser-github-user"},
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
                        "fullName": "browser-github-user/private-repo",
                        "private": True,
                        "cloneUrl": "https://github.com/browser-github-user/private-repo.git",
                    }
                ],
            ),
        ):
            app.PullwiseHandler.route(handler, "GET")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("Sign in with GitHub", handler.payload["message"])
        self.assertIsNone(github_access)
        user_can_access.assert_not_called()
    def test_github_installation_request_without_installation_id_reports_not_completed(self) -> None:
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = None
        state = app.remember_github_state(
            "install",
            "https://app.pullwise.dev/?screen=repos",
            userId="usr_1",
            requestedScope="selected",
        )
        handler = RouteHarness(f"/integrations/github/callback?state={state}&setup_action=request")

        with patch.dict(
            os.environ,
            {
                "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                "PULLWISE_GITHUB_APP_SLUG": "pullwise",
                "PULLWISE_APP_URL": "https://app.pullwise.dev",
                "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
            },
            clear=True,
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.FOUND)
        self.assertIn("github_error=github_app_installation_not_completed", handler.location)
        self.assertIsNone(app.USERS["usr_1"].get("githubRepositoryAccess"))
    def test_github_installation_callback_without_request_or_installation_id_reports_missing_installation_id(self) -> None:
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = None
        state = app.remember_github_state(
            "install",
            "https://app.pullwise.dev/?screen=repos",
            userId="usr_1",
            requestedScope="selected",
        )
        handler = RouteHarness(f"/integrations/github/callback?state={state}&setup_action=install")

        with patch.dict(
            os.environ,
            {
                "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                "PULLWISE_GITHUB_APP_SLUG": "pullwise",
                "PULLWISE_APP_URL": "https://app.pullwise.dev",
                "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
            },
            clear=True,
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.FOUND)
        self.assertIn("github_error=missing_installation_id", handler.location)
        self.assertIsNone(app.USERS["usr_1"].get("githubRepositoryAccess"))
    def test_github_installation_callback_rejects_missing_state(self) -> None:
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
            "/integrations/github/callback?installation_id=999&setup_action=install",
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
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertIsNone(app.USERS["usr_1"].get("githubRepositoryAccess"))
    def test_github_installation_callback_with_missing_private_key_path_binds_pending_sync(self) -> None:
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = None
        state = app.remember_github_state(
            "install",
            "https://app.pullwise.dev/?screen=repos",
            userId="usr_1",
            requestedScope="selected",
        )
        handler = RouteHarness(f"/integrations/github/callback?state={state}&installation_id=999")

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                    "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                    "PULLWISE_GITHUB_APP_SLUG": "pullwise",
                    "PULLWISE_GITHUB_APP_ID": "123",
                    "PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH": "D:\\missing-pullwise.private-key.pem",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                    "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.github_auth.user_can_access_installation", return_value=True),
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
            patch("pullwise_server.github_auth.list_user_installation_repositories", return_value=[]),
            patch("pullwise_server.github_auth.fetch_installation") as fetch_installation,
            patch("pullwise_server.github_auth.list_installation_repositories") as list_repositories,
        ):
            app.PullwiseHandler.route(handler, "GET")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.FOUND)
        self.assertEqual(github_access["installationId"], "999")
        self.assertEqual(github_access["repositories"], [])
        self.assertTrue(github_access["repositoriesNeedSync"])
        fetch_installation.assert_not_called()
        list_repositories.assert_not_called()
    def test_github_installation_callback_records_selected_private_repo_access(self) -> None:
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = None
        state = app.remember_github_state(
            "install",
            "https://app.pullwise.dev/?screen=repos",
            userId="usr_1",
            requestedScope="selected",
        )
        handler = RouteHarness(f"/integrations/github/callback?state={state}&installation_id=999")

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                    "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                    "PULLWISE_GITHUB_APP_SLUG": "pullwise",
                    "PULLWISE_GITHUB_APP_ID": "123",
                    "PULLWISE_GITHUB_APP_PRIVATE_KEY": "private-key",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                    "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.github_auth.user_can_access_installation", return_value=True),
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

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.FOUND)
        self.assertEqual(github_access["installationAccount"], "octocat")
        self.assertEqual(github_access["installationTargetType"], "User")
        self.assertEqual(github_access["installationPermissions"]["contents"], "write")
        self.assertEqual(github_access["installationPermissions"]["pull_requests"], "write")
        self.assertEqual(github_access["repositories"], ["octocat/private-repo"])
        self.assertTrue(github_access["repositoryItems"][0]["private"])
    def test_github_installation_callback_rejects_installation_without_pull_request_write_permissions(self) -> None:
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = None
        state = app.remember_github_state(
            "install",
            "https://app.pullwise.dev/?screen=repos",
            userId="usr_1",
            requestedScope="selected",
        )
        handler = RouteHarness(f"/integrations/github/callback?state={state}&installation_id=999")

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                    "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                    "PULLWISE_GITHUB_APP_SLUG": "pullwise",
                    "PULLWISE_GITHUB_APP_ID": "123",
                    "PULLWISE_GITHUB_APP_PRIVATE_KEY": "private-key",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                    "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.github_auth.user_can_access_installation", return_value=True),
            patch(
                "pullwise_server.github_auth.list_current_app_installations_for_user",
                return_value=[
                    {
                        "id": 999,
                        "repository_selection": "selected",
                        "target_type": "User",
                        "account": {"login": "octocat"},
                        "app_slug": "pullwise",
                        "permissions": {"metadata": "read", "contents": "read", "pull_requests": "write"},
                    }
                ],
            ),
            patch(
                "pullwise_server.github_auth.fetch_installation",
                return_value={
                    "repository_selection": "selected",
                    "target_type": "User",
                    "account": {"login": "octocat"},
                    "app_slug": "pullwise",
                    "permissions": {"metadata": "read", "contents": "read", "pull_requests": "write"},
                },
            ),
            patch("pullwise_server.github_auth.list_installation_repositories") as list_repositories,
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("Contents: write", handler.payload["message"])
        self.assertIn("Pull requests: write", handler.payload["message"])
        self.assertIsNone(app.USERS["usr_1"].get("githubRepositoryAccess"))
        list_repositories.assert_not_called()
    def test_github_installation_callback_without_state_fails_closed_when_session_user_cannot_access_installation(self) -> None:
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
            "/integrations/github/callback?installation_id=999&setup_action=install",
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
            patch("pullwise_server.github_auth.user_can_access_installation", return_value=False),
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertIsNone(app.USERS["usr_1"].get("githubRepositoryAccess"))
    def test_api_base_url_rejects_untrusted_host_header(self) -> None:
        handler = RouteHarness("/", headers={"Host": "evil.example"})

        with patch.dict(
            os.environ,
            {
                "PULLWISE_APP_URL": "https://app.pullwise.dev",
                "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
            },
            clear=True,
        ):
            self.assertEqual(app.api_base_url(handler), "http://localhost:8080")
    def test_api_base_url_prefers_configured_base_over_trusted_proxy_headers(self) -> None:
        handler = RouteHarness(
            "/auth/github/authorize",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "pullwise-admin.danuberiverferryman.workers.dev",
                "X-Forwarded-Prefix": "/api",
            },
        )

        with patch.dict(
            os.environ,
            {
                "PULLWISE_API_BASE_URL": "https://api.pull-wise.com",
                "PULLWISE_TRUST_PROXY_HEADERS": "true",
            },
            clear=True,
        ):
            self.assertEqual(
                app.api_base_url(handler),
                "https://api.pull-wise.com",
            )
    def test_api_base_url_uses_trusted_proxy_headers_when_configured_base_is_missing(self) -> None:
        handler = RouteHarness(
            "/auth/github/authorize",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "pullwise-admin.danuberiverferryman.workers.dev",
                "X-Forwarded-Prefix": "/api",
            },
        )

        with patch.dict(os.environ, {"PULLWISE_TRUST_PROXY_HEADERS": "true"}, clear=True):
            self.assertEqual(
                app.api_base_url(handler),
                "https://pullwise-admin.danuberiverferryman.workers.dev/api",
            )
    def test_root_relative_redirect_rejects_control_characters(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_APP_URL": "https://app.pullwise.dev"}, clear=True):
            self.assertEqual(
                app.safe_redirect_to("/repos\r\nSet-Cookie:pw=bad", "dashboard"),
                "https://app.pullwise.dev/dashboard",
            )
    def test_request_body_size_is_limited(self) -> None:
        handler = RouteHarness(
            "/repositories/sync",
            headers={"Content-Length": "8"},
            raw_body=b"12345678",
        )

        with patch.dict(os.environ, {"PULLWISE_MAX_BODY_BYTES": "4"}, clear=True):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
    def test_request_body_rejects_negative_content_length(self) -> None:
        handler = RouteHarness(
            "/auth/sign-out",
            headers={"Content-Length": "-1"},
        )

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload["message"], "Invalid Content-Length header.")
    def test_request_body_rejects_malformed_content_length(self) -> None:
        handler = RouteHarness(
            "/auth/sign-out",
            headers={"Content-Length": "not-a-number"},
        )

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload["message"], "Invalid Content-Length header.")


__all__ = ["SecurityContractsPart06Test"]
