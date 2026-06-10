from __future__ import annotations

try:
    import security_contracts_base as _security_contracts_base
except ModuleNotFoundError:  # pragma: no cover - package-style unittest invocation
    from . import security_contracts_base as _security_contracts_base

globals().update(
    {name: getattr(_security_contracts_base, name) for name in dir(_security_contracts_base) if not name.startswith("_")}
)


class SecurityContractsPart03Test(SecurityContractsBase):
    def test_auth_session_remains_valid_until_expiry_then_requires_login(self) -> None:
        app.SESSIONS = {
            "active": {
                "id": "active",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 60,
            },
            "expired": {
                "id": "expired",
                "userId": "usr_1",
                "createdAt": app.now() - app.SESSION_MAX_AGE - 1,
                "expiresAt": app.now() - 1,
            },
        }

        active_handler = RouteHarness("/auth/session", cookie="pw_session=active")
        app.PullwiseHandler.route(active_handler, "GET")

        expired_handler = RouteHarness("/auth/session", cookie="pw_session=expired")
        app.PullwiseHandler.route(expired_handler, "GET")

        self.assertTrue(active_handler.payload["authenticated"])
        self.assertFalse(expired_handler.payload["authenticated"])
        self.assertNotIn("expired", app.SESSIONS)
    def test_auth_session_uses_valid_session_when_duplicate_session_cookies_exist(self) -> None:
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness("/auth/session", cookie="pw_session=ses_1; pw_session=stale_host_cookie")

        app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertTrue(handler.payload["authenticated"])
        self.assertEqual(handler.payload["user"]["id"], "usr_1")
    def test_auth_session_ignores_malformed_persisted_sessions(self) -> None:
        cases = {
            "non_object": "not-a-session",
            "missing_user_id": {
                "id": "missing_user_id",
                "expiresAt": app.now() + 60,
            },
            "malformed_user_id": {
                "id": "malformed_user_id",
                "userId": {"id": "usr_1"},
                "expiresAt": app.now() + 60,
            },
            "missing_expiry": {
                "id": "missing_expiry",
                "userId": "usr_1",
            },
            "malformed_expiry": {
                "id": "malformed_expiry",
                "userId": "usr_1",
                "expiresAt": {"value": app.now() + 60},
            },
        }

        for session_id, session in cases.items():
            with self.subTest(session_id=session_id):
                app.SESSIONS = {session_id: session}
                handler = RouteHarness("/auth/session", cookie=f"pw_session={session_id}")

                with patch.object(app.logger, "exception") as log_exception:
                    app.PullwiseHandler.route(handler, "GET")

                self.assertEqual(handler.status, HTTPStatus.OK)
                self.assertFalse(handler.payload["authenticated"])
                self.assertNotIn(session_id, app.SESSIONS)
                log_exception.assert_not_called()
    def test_state_loader_ignores_malformed_persisted_sections(self) -> None:
        app.STATE_LOADED = False
        state = {
            "users": ["not", "a", "dict"],
            "sessions": "not-a-dict",
            "githubStates": [{"not": "a mapping"}],
            "settings": "not-a-dict",
            "billingEvents": ["not", "a", "dict"],
            "billingPendingUpdates": {"not": "a-list"},
            "scans": {"not": "a-list"},
            "issues": "not-a-list",
        }

        with patch.object(app.db, "load_state", return_value=state):
            app.ensure_state_loaded()

        self.assertTrue(app.STATE_LOADED)
        self.assertEqual(app.USERS, {})
        self.assertEqual(app.SESSIONS, {})
        self.assertEqual(app.GITHUB_STATES, {})
        self.assertEqual(app.SETTINGS, {})
        self.assertEqual(app.BILLING_EVENTS, {})
        self.assertEqual(app.BILLING_PENDING_UPDATES, [])
        self.assertEqual(app.SCANS, [])
        self.assertEqual(app.ISSUES, [])
    def test_auth_session_sanitizes_malformed_user_public_fields(self) -> None:
        app.USERS["usr_1"].update({
            "id": "usr_1\r\nX-Injected: bad",
            "name": "Dev\r\nX-Injected: bad",
            "email": {"value": "dev@example.com"},
            "avatarUrl": "javascript:alert(1)",
            "createdAt": {"value": app.now()},
            "providers": ["github", "email\r\nX-Injected: bad", {"provider": "bad"}],
            "githubLogin": "octocat\r\nX-Injected: bad",
            "githubRepositoryAccess": {
                "mode": "github-app",
                "scope": "selected\r\nX-Injected: bad",
                "authorizedUserId": "usr_1",
                "authorizedGithubId": "1",
                "authorizedGithubLogin": "octocat",
                "authorizedAt": {"value": app.now()},
                "repositories": ["owner/repo"],
                "repositoryItems": [
                    {
                        "fullName": "owner/repo",
                        "installationId": "123",
                    }
                ],
                "repositoriesNeedSync": False,
            },
        })
        handler = RouteHarness("/auth/session", cookie=self.signed_in())

        app.PullwiseHandler.route(handler, "GET")

        self.assertTrue(handler.payload["authenticated"])
        self.assertEqual(
            handler.payload["user"],
            {
                "id": "",
                "name": "User",
                "email": "",
                "avatarUrl": None,
                "createdAt": 0,
                "providers": ["github"],
            },
        )
        self.assertIsNone(handler.payload["github"]["login"])
        self.assertIsNone(handler.payload["github"]["repositoryScope"])
        self.assertIsNone(handler.payload["github"]["authorizedAt"])
    def test_auth_session_filters_github_noreply_user_email(self) -> None:
        app.USERS["usr_1"]["email"] = "SanChai20@users.noreply.github.com"
        handler = RouteHarness("/auth/session", cookie=self.signed_in())

        app.PullwiseHandler.route(handler, "GET")

        self.assertTrue(handler.payload["authenticated"])
        self.assertEqual(handler.payload["user"]["email"], "")
    def test_auth_session_does_not_report_pending_empty_repository_access_connected(self) -> None:
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "installationId": "999",
            "repositories": [],
            "repositoryItems": [],
            "repositoriesNeedSync": True,
        }
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness("/auth/session", cookie="pw_session=ses_1")

        app.PullwiseHandler.route(handler, "GET")

        self.assertTrue(handler.payload["authenticated"])
        self.assertFalse(handler.payload["github"]["repositoriesConnected"])
        self.assertEqual(handler.payload["github"]["repositoryCount"], 0)
        self.assertEqual(handler.payload["nextStep"], "connect_github_repositories")
    def test_auth_session_does_not_report_repository_names_only_access_connected(self) -> None:
        app.USERS["usr_1"]["githubRepositoryAccess"] = {"repositories": ["owner/repo"]}
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness("/auth/session", cookie="pw_session=ses_1")

        app.PullwiseHandler.route(handler, "GET")

        self.assertTrue(handler.payload["authenticated"])
        self.assertFalse(handler.payload["github"]["repositoriesConnected"])
        self.assertEqual(handler.payload["nextStep"], "connect_github_repositories")
    def test_auth_session_does_not_report_stale_repository_access_connected_while_authorization_is_pending(self) -> None:
        app.USERS["usr_1"]["githubLogin"] = "DFerryman"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "installationId": "111",
            "installationAccount": "SanChai20",
            "repositories": ["SanChai20/private-repo"],
            "repositoryItems": [
                {
                    "id": "repo_sanchai20_private",
                    "name": "private-repo",
                    "fullName": "SanChai20/private-repo",
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
        handler = RouteHarness("/auth/session", cookie="pw_session=ses_1")

        app.PullwiseHandler.route(handler, "GET")

        self.assertTrue(handler.payload["authenticated"])
        self.assertFalse(handler.payload["github"]["repositoriesConnected"])
        self.assertEqual(handler.payload["nextStep"], "connect_github_repositories")
    def test_auth_session_ignores_malformed_repository_authorization_pending_state(self) -> None:
        app.USERS["usr_1"]["githubLogin"] = "DFerryman"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "installationId": "111",
            "installationAccount": "DFerryman",
            "repositories": ["DFerryman/private-repo"],
            "repositoryItems": [{"fullName": "DFerryman/private-repo"}],
            "repositoriesNeedSync": False,
        }
        app.USERS["usr_1"]["githubRepositoryAccessPending"] = {
            "state": "pending_state",
            "startedAt": app.now(),
            "expiresAt": {"value": app.now() + app.GITHUB_STATE_MAX_AGE},
            "previousInstallationId": "111",
            "manage": True,
        }
        app.GITHUB_STATES = {
            "bad_state": "not-a-state-record",
            "bad_expiry": {
                "kind": "install",
                "userId": "usr_1",
                "expiresAt": {"value": app.now() + app.GITHUB_STATE_MAX_AGE},
            },
        }
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness("/auth/session", cookie="pw_session=ses_1")

        with patch.object(app.logger, "exception") as log_exception:
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertTrue(handler.payload["authenticated"])
        self.assertFalse(handler.payload["github"]["repositoriesConnected"])
        self.assertFalse(handler.payload["github"]["repositoriesAuthorizationPending"])
        self.assertEqual(handler.payload["nextStep"], "connect_github_repositories")
        self.assertNotIn("githubRepositoryAccessPending", app.USERS["usr_1"])
        self.assertEqual(
            app.GITHUB_STATES,
            {
                "bad_state": "not-a-state-record",
                "bad_expiry": {
                    "kind": "install",
                    "userId": "usr_1",
                    "expiresAt": {"value": app.now() + app.GITHUB_STATE_MAX_AGE},
                },
            },
        )
        log_exception.assert_not_called()
    def test_clear_repository_authorization_pending_ignores_malformed_state_records(self) -> None:
        app.USERS["usr_1"]["githubRepositoryAccessPending"] = {
            "state": "pending_state",
            "startedAt": app.now(),
            "expiresAt": app.now() + app.GITHUB_STATE_MAX_AGE,
        }
        other_user_record = {
            "kind": "install",
            "userId": "usr_2",
            "expiresAt": app.now() + app.GITHUB_STATE_MAX_AGE,
        }
        app.GITHUB_STATES = {
            "malformed": "not-a-state-record",
            "matching": {
                "kind": "install",
                "userId": "usr_1",
                "expiresAt": app.now() + app.GITHUB_STATE_MAX_AGE,
            },
            "other_user": other_user_record,
        }

        app.clear_github_repository_authorization_pending(app.USERS["usr_1"])

        self.assertNotIn("githubRepositoryAccessPending", app.USERS["usr_1"])
        self.assertEqual(app.GITHUB_STATES, {"other_user": other_user_record})
    def test_auth_session_requires_explicit_repository_authorization_pending_record(self) -> None:
        app.USERS["usr_1"]["githubLogin"] = "DFerryman"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "installationId": "111",
            "installationAccount": "SanChai20",
            "repositories": ["SanChai20/private-repo"],
            "repositoryItems": [
                {
                    "id": "repo_sanchai20_private",
                    "name": "private-repo",
                    "fullName": "SanChai20/private-repo",
                }
            ],
            "repositoriesNeedSync": False,
        }
        app.GITHUB_STATES = {
            "install_state": {
                "kind": "install",
                "redirectTo": "https://app.pullwise.dev/?screen=repos",
                "userId": "usr_1",
                "requestedScope": "all",
                "expiresAt": app.now() + app.GITHUB_STATE_MAX_AGE,
            }
        }
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness("/auth/session", cookie="pw_session=ses_1")

        app.PullwiseHandler.route(handler, "GET")

        self.assertTrue(handler.payload["authenticated"])
        self.assertFalse(handler.payload["github"]["repositoriesConnected"])
        self.assertFalse(handler.payload["github"]["repositoriesAuthorizationPending"])
    def test_auth_session_ignores_install_state_without_pending_record(self) -> None:
        app.USERS["usr_1"]["githubLogin"] = "DFerryman"
        app.USERS["usr_1"]["githubRepositoryAccess"] = "not-a-repository-access-record"
        app.GITHUB_STATES = {
            "install_state": {
                "kind": "install",
                "redirectTo": "https://app.pullwise.dev/?screen=repos",
                "userId": "usr_1",
                "requestedScope": "all",
                "expiresAt": app.now() + app.GITHUB_STATE_MAX_AGE,
            }
        }
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness("/auth/session", cookie="pw_session=ses_1")

        with patch.object(app.logger, "exception") as log_exception:
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertTrue(handler.payload["authenticated"])
        self.assertFalse(handler.payload["github"]["repositoriesConnected"])
        self.assertFalse(handler.payload["github"]["repositoriesAuthorizationPending"])
        log_exception.assert_not_called()
    def test_auth_session_does_not_report_stale_personal_installation_connected_for_current_github_login(self) -> None:
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
        handler = RouteHarness("/auth/session", cookie="pw_session=ses_1")

        app.PullwiseHandler.route(handler, "GET")

        self.assertTrue(handler.payload["authenticated"])
        self.assertFalse(handler.payload["github"]["repositoriesConnected"])
        self.assertEqual(handler.payload["nextStep"], "connect_github_repositories")
    def test_integrations_do_not_expose_stale_personal_installation_for_current_github_login(self) -> None:
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
        handler = RouteHarness("/integrations", cookie="pw_session=ses_1")

        app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertFalse(handler.payload["github"]["connected"])
        self.assertIsNone(handler.payload["github"]["installationAccount"])
        self.assertEqual(handler.payload["github"]["repositories"], [])
    def test_integrations_payload_sanitizes_malformed_github_access_metadata(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "scope": {"scope": "selected"},
            "repositorySelection": "selected\r\nX-Test: bad",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
            "installationId": {"id": "111"},
            "installationIds": [{"id": "111"}, "222\r\nX-Test: bad", "333", 444],
            "installationAccount": {"login": "octocat"},
            "installationAccounts": [{"login": "octocat"}, "acme\r\nX-Test: bad", "valid-org"],
            "installationTargetType": {"type": "Organization"},
            "installationHtmlUrl": "https://github.com/settings/installations/111",
            "installations": [],
            "repositories": {"octocat/private-repo": True},
            "repositoryItems": [{"id": "repo_private", "name": "private-repo", "fullName": "octocat/private-repo"}],
            "repositoriesNeedSync": "false",
        }
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness("/integrations", cookie="pw_session=ses_1")

        app.PullwiseHandler.route(handler, "GET")

        github = handler.payload["github"]
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertTrue(github["connected"])
        self.assertIsNone(github["scope"])
        self.assertIsNone(github["repositorySelection"])
        self.assertIsNone(github["installationId"])
        self.assertEqual(github["installationIds"], ["333", "444"])
        self.assertIsNone(github["installationAccount"])
        self.assertEqual(github["installationAccounts"], ["valid-org"])
        self.assertIsNone(github["installationHtmlUrl"])
        self.assertEqual(github["identities"][0]["login"], "octocat")
        self.assertEqual(github["repositories"], [])
        self.assertFalse(github["repositoriesNeedSync"])
    def test_auth_session_ignores_malformed_aggregate_installation_records(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "scope": "selected",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
            "installationIds": ["111"],
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
                "not-an-installation-record",
                {"installationTargetType": "User", "installationAccount": "octocat"},
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
        handler = RouteHarness("/auth/session", cookie="pw_session=ses_1")

        with patch.object(app.logger, "exception") as log_exception:
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertTrue(handler.payload["authenticated"])
        self.assertTrue(handler.payload["github"]["repositoriesConnected"])
        log_exception.assert_not_called()
    def test_auth_session_ignores_malformed_github_repository_access_record(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = "not-a-repository-access-record"
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness("/auth/session", cookie="pw_session=ses_1")

        with patch.object(app.logger, "exception") as log_exception:
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertTrue(handler.payload["authenticated"])
        self.assertFalse(handler.payload["github"]["repositoriesConnected"])
        self.assertEqual(handler.payload["nextStep"], "connect_github_repositories")
        log_exception.assert_not_called()
    def test_repository_sync_requires_sign_in(self) -> None:
        handler = RouteHarness("/repositories/sync")

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.UNAUTHORIZED)
    def test_github_repository_authorize_rejects_private_app_slug_for_user_installs(self) -> None:
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
                    "PULLWISE_GITHUB_APP_SLUG": "gopullwise",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                    "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.github_auth.app_slug_publicly_installable", return_value=False),
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.CONFLICT)
        self.assertIn("GitHub App 'gopullwise' is private", handler.payload["message"])
        self.assertIn("keep PULLWISE_GITHUB_APP_VISIBILITY_CHECK enabled", handler.payload["message"])
        self.assertEqual(app.GITHUB_STATES, {})
    def test_github_repository_authorize_fails_closed_when_app_visibility_is_unknown(self) -> None:
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
                    "PULLWISE_GITHUB_APP_SLUG": "gopullwise",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                    "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.github_auth.app_slug_publicly_installable", return_value=None),
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertIn("Unable to verify GitHub App 'gopullwise' is public", handler.payload["message"])
        self.assertIn("keep PULLWISE_GITHUB_APP_VISIBILITY_CHECK enabled", handler.payload["message"])
        self.assertEqual(app.GITHUB_STATES, {})
    def test_github_repository_authorize_requires_slug_even_with_install_url_override(self) -> None:
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

        with patch.dict(
            os.environ,
            {
                "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                "PULLWISE_GITHUB_APP_INSTALL_URL": "https://github.com/apps/gopullwise/installations/new",
                "PULLWISE_APP_URL": "https://app.pullwise.dev",
                "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
            },
            clear=True,
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.NOT_IMPLEMENTED)
        self.assertIn("PULLWISE_GITHUB_APP_SLUG is required", handler.payload["message"])
        self.assertEqual(app.GITHUB_STATES, {})
    def test_github_repository_authorize_checks_public_slug_even_with_install_url_override(self) -> None:
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
                    "PULLWISE_GITHUB_APP_SLUG": "gopullwise",
                    "PULLWISE_GITHUB_APP_INSTALL_URL": "https://github.com/apps/gopullwise/installations/new",
                    "PULLWISE_APP_URL": "https://app.pullwise.dev",
                    "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                },
                clear=True,
            ),
            patch("pullwise_server.github_auth.app_slug_publicly_installable", return_value=False),
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.CONFLICT)
        self.assertIn("GitHub App 'gopullwise' is private", handler.payload["message"])
        self.assertEqual(app.GITHUB_STATES, {})
    def test_github_repository_authorize_returns_install_url_for_public_app_slug(self) -> None:
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
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["mode"], "github-app")
        self.assertIn("https://github.com/apps/pullwise/installations/new?state=", handler.payload["url"])
        self.assertEqual(len(app.GITHUB_STATES), 1)
    def test_github_repository_authorize_ignores_malformed_existing_access_when_starting_install(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubAccessToken"] = "gho_user"
        app.USERS["usr_1"]["githubRepositoryAccess"] = "not-a-repository-access-record"
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
            patch("pullwise_server.github_auth.list_current_app_installations_for_user", return_value=[]),
            patch.object(app.logger, "exception") as log_exception,
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["mode"], "github-app")
        self.assertIn("https://github.com/apps/pullwise/installations/new?state=", handler.payload["url"])
        self.assertIn("githubRepositoryAccessPending", app.USERS["usr_1"])
        self.assertEqual(app.USERS["usr_1"]["githubRepositoryAccessPending"]["previousInstallationId"], None)
        self.assertEqual(len(app.GITHUB_STATES), 1)
        log_exception.assert_not_called()
    def test_github_repository_authorize_requires_pullwise_github_oauth_identity(self) -> None:
        app.USERS["usr_1"]["providers"] = ["email"]
        app.USERS["usr_1"].pop("githubAccessToken", None)
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
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.UNAUTHORIZED)
        self.assertIn("Sign in with GitHub", handler.payload["message"])
        self.assertEqual(app.GITHUB_STATES, {})


__all__ = ["SecurityContractsPart03Test"]
