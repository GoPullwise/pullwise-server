from __future__ import annotations

try:
    import security_contracts_base as _security_contracts_base
except ModuleNotFoundError:  # pragma: no cover - package-style unittest invocation
    from . import security_contracts_base as _security_contracts_base

globals().update(
    {name: getattr(_security_contracts_base, name) for name in dir(_security_contracts_base) if not name.startswith("_")}
)


class SecurityContractsPart04Test(SecurityContractsBase):
    def test_github_repository_authorize_binds_existing_app_installation_without_popup(self) -> None:
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
                    "html_url": "https://github.com/settings/installations/999",
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
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertTrue(handler.payload["connected"])
        self.assertEqual(handler.payload["mode"], "github-app-existing")
        self.assertNotIn("url", handler.payload)
        self.assertEqual(app.GITHUB_STATES, {})
        self.assertEqual(github_access["installationId"], "999")
        self.assertEqual(github_access["repositories"], ["octocat/private-repo"])
        list_existing.assert_called_once_with("gho_user")
    def test_github_repository_authorize_returns_configure_url_for_managing_verified_existing_installation(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "installationId": "999",
            "installationHtmlUrl": "https://github.com/settings/installations/999",
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
                    "private": True,
                    "cloneUrl": "https://github.com/octocat/private-repo.git",
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
            "/integrations/github/authorize?manage=1&redirectTo=https%3A%2F%2Fapp.pullwise.dev%2F%3Fscreen%3Drepos",
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
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["mode"], "github-installation-manage")
        self.assertIn("/integrations/github/manage/start?state=", handler.payload["url"])
        self.assertNotIn("/settings/installations/999", handler.payload["url"])
        self.assertEqual(len(app.GITHUB_STATES), 1)
        record = next(iter(app.GITHUB_STATES.values()))
        self.assertEqual(record["kind"], "manage_installation")
        self.assertEqual(record["expectedInstallationId"], "999")
        self.assertEqual(record["expectedAccountLogin"], "octocat")
    def test_github_repository_authorize_manage_lists_existing_aggregate_installations(self) -> None:
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
            "repositories": ["octocat/private-repo", "acme/service"],
            "repositoryItems": [
                {"fullName": "octocat/private-repo", "installationId": "111"},
                {"fullName": "acme/service", "installationId": "222"},
            ],
            "installations": [
                {
                    "installationId": "111",
                    "installationAccount": "octocat",
                    "installationHtmlUrl": "https://github.com/settings/installations/111",
                    "repositorySelection": "selected",
                    "repositoryCount": 1,
                },
                {
                    "installationId": "222",
                    "installationAccount": "acme",
                    "installationHtmlUrl": "https://github.com/organizations/acme/settings/installations/222",
                    "repositorySelection": "all",
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
            "/integrations/github/authorize?manage=1&redirectTo=https%3A%2F%2Fapp.pullwise.dev%2F%3Fscreen%3Drepos",
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
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertTrue(handler.payload["connected"])
        self.assertEqual(handler.payload["mode"], "github-app-existing-manage-list")
        self.assertNotIn("url", handler.payload)
        self.assertEqual(
            [installation["installationAccount"] for installation in handler.payload["installations"]],
            ["octocat", "acme"],
        )
        self.assertTrue(all(installation["installationHtmlUrl"] is None for installation in handler.payload["installations"]))
        self.assertTrue(all("manage" in installation for installation in handler.payload["installations"]))
        self.assertEqual(app.GITHUB_STATES, {})
    def test_github_installation_manage_session_creates_controlled_state_without_raw_settings_url(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubIdentities"] = [
            {
                "id": "ghi_1",
                "githubUserId": "1",
                "githubLogin": "octocat",
                "accessToken": "gho_user",
                "status": "active",
            }
        ]
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
            "installationIds": ["111", "222"],
            "installationAccounts": ["octocat", "acme"],
            "repositories": ["octocat/private-repo", "acme/service"],
            "repositoryItems": [
                {"fullName": "octocat/private-repo", "installationId": "111"},
                {"fullName": "acme/service", "installationId": "222"},
            ],
            "installations": [
                {
                    "installationId": "111",
                    "installationAccount": "octocat",
                    "installationTargetType": "User",
                    "installationHtmlUrl": "https://github.com/settings/installations/111",
                },
                {
                    "installationId": "222",
                    "installationAccount": "acme",
                    "installationTargetType": "Organization",
                    "installationHtmlUrl": "https://github.com/organizations/acme/settings/installations/222",
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
            "/integrations/github/installations/222/manage-sessions",
            {"githubIdentityId": "ghi_1", "returnUrl": "https://app.pullwise.dev/?screen=settings"},
            cookie="pw_session=ses_1",
        )

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
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["mode"], "github-installation-manage")
        self.assertIn("/integrations/github/manage/start?state=", handler.payload["url"])
        self.assertNotIn("github.com/organizations/acme/settings/installations/222", handler.payload["url"])
        record = next(iter(app.GITHUB_STATES.values()))
        self.assertEqual(record["kind"], "manage_installation")
        self.assertEqual(record["expectedInstallationId"], "222")
        self.assertEqual(record["expectedAccountLogin"], "acme")
        self.assertEqual(record["expectedGithubIdentityId"], "ghi_1")
        self.assertEqual(record["redirectTo"], "https://app.pullwise.dev/?screen=settings")
    def test_github_manage_start_redirects_to_oauth_account_picker(self) -> None:
        state = app.remember_github_state(
            "manage_installation",
            "https://app.pullwise.dev/?screen=repos",
            userId="usr_1",
            expectedInstallationId="999",
        )
        handler = RouteHarness(f"/integrations/github/manage/start?state={state}")

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                    "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                    "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.github_auth.make_code_verifier", return_value="verifier"),
            patch(
                "pullwise_server.github_auth.build_oauth_authorize_url",
                return_value="https://github.com/login/oauth/authorize?prompt=select_account",
            ) as build_authorize_url,
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.FOUND)
        self.assertEqual(handler.location, "https://github.com/login/oauth/authorize?prompt=select_account")
        self.assertEqual(app.GITHUB_STATES[state]["codeVerifier"], "verifier")
        self.assertEqual(build_authorize_url.call_args.kwargs["prompt"], "select_account")
    def test_github_manage_callback_redirects_to_app_bridge_before_installation_settings_url(self) -> None:
        state = app.remember_github_state(
            "manage_installation",
            "https://app.pullwise.dev/?screen=repos",
            userId="usr_1",
            expectedInstallationId="999",
            expectedAccountLogin="octocat",
            expectedInstallationTargetType="User",
            expectedInstallationHtmlUrl="https://github.com/settings/installations/999",
            codeVerifier="verifier",
        )
        handler = RouteHarness(f"/auth/github/callback?state={state}&code=oauth_code")

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                    "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                    "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.github_auth.exchange_oauth_code", return_value={"access_token": "gho_user", "scope": "read:user"}),
            patch(
                "pullwise_server.github_auth.fetch_user_profile",
                return_value={"id": 1, "login": "octocat", "html_url": "https://github.com/octocat"},
            ),
            patch(
                "pullwise_server.github_auth.list_current_app_installations_for_user",
                return_value=[
                    {
                        "id": 999,
                        "target_type": "User",
                        "account": {"login": "octocat"},
                        "html_url": "https://github.com/settings/installations/999",
                    }
                ],
            ),
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.FOUND)
        self.assertIn("https://app.pullwise.dev/?screen=repos", handler.location)
        self.assertIn("github_manage_continue_url=", handler.location)
        self.assertIn(
            "https%3A%2F%2Fgithub.com%2Fsettings%2Finstallations%2F999",
            handler.location,
        )
        self.assertEqual(app.USERS["usr_1"]["githubIdentities"][0]["githubLogin"], "octocat")
        access = app.USERS["usr_1"]["githubIdentityInstallationAccess"][0]
        self.assertTrue(access["canAccess"])
        self.assertEqual(access["githubAppInstallationId"], "999")
    def test_github_manage_callback_wrong_identity_returns_account_mismatch(self) -> None:
        state = app.remember_github_state(
            "manage_installation",
            "https://app.pullwise.dev/?screen=repos",
            userId="usr_1",
            expectedInstallationId="999",
            expectedAccountLogin="octocat",
            expectedInstallationTargetType="User",
            expectedGithubIdentityId="ghi_1",
            codeVerifier="verifier",
        )
        handler = RouteHarness(f"/auth/github/callback?state={state}&code=oauth_code")

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                    "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                    "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.github_auth.exchange_oauth_code", return_value={"access_token": "gho_wrong", "scope": "read:user"}),
            patch(
                "pullwise_server.github_auth.fetch_user_profile",
                return_value={"id": 2, "login": "wrong-user", "html_url": "https://github.com/wrong-user"},
            ),
            patch("pullwise_server.github_auth.list_current_app_installations_for_user") as list_installations,
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.FOUND)
        self.assertIn("github_error=github_account_mismatch", handler.location)
        self.assertIn("github_login=wrong-user", handler.location)
        list_installations.assert_not_called()
    def test_github_manage_callback_invisible_installation_returns_pullwise_error(self) -> None:
        state = app.remember_github_state(
            "manage_installation",
            "https://app.pullwise.dev/?screen=repos",
            userId="usr_1",
            expectedInstallationId="999",
            expectedAccountLogin="acme",
            expectedInstallationTargetType="Organization",
            codeVerifier="verifier",
        )
        handler = RouteHarness(f"/auth/github/callback?state={state}&code=oauth_code")

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                    "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                    "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.github_auth.exchange_oauth_code", return_value={"access_token": "gho_user", "scope": "read:user"}),
            patch(
                "pullwise_server.github_auth.fetch_user_profile",
                return_value={"id": 1, "login": "octocat", "html_url": "https://github.com/octocat"},
            ),
            patch("pullwise_server.github_auth.list_current_app_installations_for_user", return_value=[]),
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.FOUND)
        self.assertIn("github_error=github_installation_not_visible", handler.location)
        self.assertNotIn("settings%2Finstallations%2F999", handler.location)
    def test_github_repository_authorize_add_returns_identity_picker_url_for_existing_aggregate_installations(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "scope": "mixed",
            "repositorySelection": "selected",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
            "installationIds": ["111", "222"],
            "installationAccounts": ["octocat", "acme"],
            "repositories": ["octocat/private-repo", "acme/service"],
            "repositoryItems": [
                {"fullName": "octocat/private-repo", "installationId": "111"},
                {"fullName": "acme/service", "installationId": "222"},
            ],
            "installations": [
                {
                    "installationId": "111",
                    "installationAccount": "octocat",
                    "installationHtmlUrl": "https://github.com/settings/installations/111",
                    "repositorySelection": "selected",
                    "repositoryCount": 1,
                },
                {
                    "installationId": "222",
                    "installationAccount": "acme",
                    "installationHtmlUrl": "https://github.com/organizations/acme/settings/installations/222",
                    "repositorySelection": "all",
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
            "/integrations/github/authorize?add=1&redirectTo=https%3A%2F%2Fapp.pullwise.dev%2F%3Fscreen%3Drepos",
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
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["mode"], "github-app-add")
        self.assertIn("/integrations/github/install/start?state=", handler.payload["url"])
        self.assertEqual(len(app.GITHUB_STATES), 1)
        self.assertEqual(next(iter(app.GITHUB_STATES.values()))["kind"], "install_identity")
    def test_github_repository_authorize_add_uses_selected_identity_for_other_personal_account_installation(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubId"] = "1"
        app.USERS["usr_1"]["githubLogin"] = "DFerryman"
        app.USERS["usr_1"]["githubAccessToken"] = "gho_dferryman"
        app.USERS["usr_1"]["githubOAuthScope"] = "read:user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "DFerryman",
            "installationId": "111",
            "installationIds": ["111"],
            "installationAccount": "DFerryman",
            "installationAccounts": ["DFerryman"],
            "repositories": ["DFerryman/service"],
            "repositoryItems": [{"fullName": "DFerryman/service", "installationId": "111"}],
            "installations": [
                {
                    "installationId": "111",
                    "installationAccount": "DFerryman",
                    "installationTargetType": "User",
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
        authorize = RouteHarness(
            "/integrations/github/authorize?add=1&redirectTo=https%3A%2F%2Fapp.pullwise.dev%2F%3Fscreen%3Drepos",
            cookie="pw_session=ses_1",
        )

        env = {
            "PULLWISE_GITHUB_CLIENT_ID": "client_id",
            "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
            "PULLWISE_GITHUB_APP_SLUG": "pullwise",
            "PULLWISE_APP_URL": "https://app.pullwise.dev",
            "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("pullwise_server.github_auth.app_slug_publicly_installable", return_value=True),
        ):
            app.PullwiseHandler.route(authorize, "GET")

        self.assertEqual(authorize.status, HTTPStatus.OK)
        self.assertEqual(authorize.payload["mode"], "github-app-add")
        self.assertIn("/integrations/github/install/start?state=", authorize.payload["url"])
        identity_state = next(iter(app.GITHUB_STATES))

        start = RouteHarness(f"/integrations/github/install/start?state={identity_state}")
        with (
            patch.dict(os.environ, env, clear=True),
            patch("pullwise_server.github_auth.make_code_verifier", return_value="verifier"),
            patch(
                "pullwise_server.github_auth.build_oauth_authorize_url",
                return_value="https://github.com/login/oauth/authorize?prompt=select_account",
            ) as build_authorize_url,
        ):
            app.PullwiseHandler.route(start, "GET")

        self.assertEqual(start.status, HTTPStatus.FOUND)
        self.assertEqual(start.location, "https://github.com/login/oauth/authorize?prompt=select_account")
        self.assertEqual(build_authorize_url.call_args.kwargs["prompt"], "select_account")

        oauth = RouteHarness(f"/auth/github/callback?state={identity_state}&code=oauth_code")
        with (
            patch.dict(os.environ, env, clear=True),
            patch("pullwise_server.github_auth.exchange_oauth_code", return_value={"access_token": "gho_other", "scope": "read:user"}),
            patch(
                "pullwise_server.github_auth.fetch_user_profile",
                return_value={"id": 2, "login": "other-user", "html_url": "https://github.com/other-user"},
            ),
        ):
            app.PullwiseHandler.route(oauth, "GET")

        self.assertEqual(oauth.status, HTTPStatus.FOUND)
        self.assertIn("https://github.com/apps/pullwise/installations/new?state=", oauth.location)
        install_state = oauth.location.rsplit("state=", 1)[1]
        self.assertEqual(app.GITHUB_STATES[install_state]["selectedGithubIdentityId"], "ghi_2")

        callback = RouteHarness(f"/integrations/github/callback?state={install_state}&installation_id=222")
        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "pullwise_server.github_auth.list_current_app_installations_for_user",
                return_value=[
                    {
                        "id": 222,
                        "repository_selection": "selected",
                        "target_type": "User",
                        "account": {"login": "other-user"},
                        "app_slug": "pullwise",
                        "html_url": "https://github.com/settings/installations/222",
                        "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
                    }
                ],
            ) as list_installations,
            patch("pullwise_server.github_auth.app_api_configured", return_value=False),
            patch(
                "pullwise_server.github_auth.list_user_installation_repositories",
                return_value=[
                    {
                        "id": "repo_other_private",
                        "name": "private-repo",
                        "fullName": "other-user/private-repo",
                        "private": True,
                        "cloneUrl": "https://github.com/other-user/private-repo.git",
                    }
                ],
            ) as list_repositories,
        ):
            app.PullwiseHandler.route(callback, "GET")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(callback.status, HTTPStatus.FOUND)
        self.assertEqual(callback.location, "https://app.pullwise.dev/?screen=repos")
        list_installations.assert_called_once_with("gho_other")
        list_repositories.assert_called_once_with("gho_other", "222")
        self.assertIn("DFerryman/service", github_access["repositories"])
        self.assertIn("other-user/private-repo", github_access["repositories"])
        self.assertEqual(github_access["installationIds"], ["111", "222"])
        access = app.USERS["usr_1"]["githubIdentityInstallationAccess"][0]
        self.assertEqual(access["githubIdentityId"], "ghi_2")
        self.assertEqual(access["githubAppInstallationId"], "222")
        self.assertTrue(access["canAccess"])

        repositories = RouteHarness("/repositories", cookie="pw_session=ses_1")
        app.PullwiseHandler.route(repositories, "GET")

        self.assertEqual(repositories.status, HTTPStatus.OK)
        self.assertFalse(repositories.payload["needsAuthorization"])
        self.assertEqual(
            [item["fullName"] for item in repositories.payload["items"]],
            ["DFerryman/service", "other-user/private-repo"],
        )
    def test_github_repository_authorize_does_not_return_cached_configure_url_for_manage(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "installationId": "999",
            "installationHtmlUrl": "https://github.com/settings/installations/999",
            "installationAccount": "other-user",
            "installationTargetType": "User",
            "repositories": ["other-user/private-repo"],
            "repositoryItems": [
                {
                    "id": "repo_private",
                    "name": "private-repo",
                    "fullName": "other-user/private-repo",
                    "private": True,
                    "cloneUrl": "https://github.com/other-user/private-repo.git",
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
            "/integrations/github/authorize?manage=1&redirectTo=https%3A%2F%2Fapp.pullwise.dev%2F%3Fscreen%3Drepos",
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
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["mode"], "github-app")
        self.assertIn("https://github.com/apps/pullwise/installations/new?state=", handler.payload["url"])
        self.assertNotIn("/settings/installations/999", handler.payload["url"])
        self.assertEqual(len(app.GITHUB_STATES), 1)
    def test_github_repository_authorize_keeps_install_url_for_pending_empty_installation(self) -> None:
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
                return_value=[{"id": 999, "repository_selection": "selected"}],
            ),
            patch("pullwise_server.github_auth.list_user_installation_repositories", return_value=[]),
        ):
            app.PullwiseHandler.route(handler, "GET")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["mode"], "github-app")
        self.assertNotIn("connected", handler.payload)
        self.assertIn("https://github.com/apps/pullwise/installations/new?state=", handler.payload["url"])
        self.assertEqual(len(app.GITHUB_STATES), 1)
        self.assertEqual(github_access["installationId"], "999")
        self.assertTrue(github_access["repositoriesNeedSync"])
    def test_github_repository_authorize_opens_existing_installation_configure_url_when_repositories_are_empty(self) -> None:
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
                        "html_url": "https://github.com/settings/installations/999",
                    }
                ],
            ),
            patch("pullwise_server.github_auth.list_user_installation_repositories", return_value=[]),
        ):
            app.PullwiseHandler.route(handler, "GET")

        github_access = app.USERS["usr_1"]["githubRepositoryAccess"]
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["mode"], "github-app-existing-pending")
        self.assertEqual(handler.payload["url"], "https://github.com/settings/installations/999")
        self.assertNotIn("connected", handler.payload)
        self.assertEqual(app.GITHUB_STATES, {})
        self.assertEqual(github_access["installationId"], "999")
        self.assertTrue(github_access["repositoriesNeedSync"])


__all__ = ["SecurityContractsPart04Test"]
