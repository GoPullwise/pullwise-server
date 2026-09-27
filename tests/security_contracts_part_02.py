from __future__ import annotations

try:
    import security_contracts_base as _security_contracts_base
except ModuleNotFoundError:  # pragma: no cover - package-style unittest invocation
    from . import security_contracts_base as _security_contracts_base

globals().update(
    {name: getattr(_security_contracts_base, name) for name in dir(_security_contracts_base) if not name.startswith("_")}
)


class SecurityContractsPart02Test(SecurityContractsBase):
    def test_github_installation_html_url_must_match_configured_github_host(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_GITHUB_WEB_URL": "https://github.com"}, clear=False):
            self.assertEqual(
                app.trusted_github_web_url("https://github.com/settings/installations/123"),
                "https://github.com/settings/installations/123",
            )
            self.assertIsNone(app.trusted_github_web_url("javascript:alert(1)"))
            self.assertIsNone(app.trusted_github_web_url("https://evil.example/settings/installations/123"))
    def test_github_installation_html_url_rejects_crlf_values(self) -> None:
        unsafe_url = "https://github.com/settings/installations/123\r\nX-Pullwise-Test: bad"

        with patch.dict(os.environ, {"PULLWISE_GITHUB_WEB_URL": "https://github.com"}, clear=False):
            self.assertIsNone(app.trusted_github_web_url(unsafe_url))
            summary = app.installation_summary_from_access({
                "installationId": "123",
                "installationHtmlUrl": unsafe_url,
            })

        self.assertIsNone(summary["installationHtmlUrl"])
    def test_installation_summary_drops_untrusted_html_url(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_GITHUB_WEB_URL": "https://github.com"}, clear=False):
            summary = app.installation_summary_from_access({
                "installationId": "123",
                "installationHtmlUrl": "javascript:alert(1)",
            })

        self.assertIsNone(summary["installationHtmlUrl"])
    def test_installation_summary_sanitizes_malformed_metadata(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_GITHUB_WEB_URL": "https://github.com"}, clear=False):
            summary = app.installation_summary_from_access({
                "installationId": {"id": "123"},
                "installationAccount": {"login": "octocat"},
                "installationTargetType": ["User"],
                "installationAppSlug": {"slug": "pullwise"},
                "installationHtmlUrl": "https://github.com/settings/installations/123",
                "repositorySelection": "selected\r\nX-Test: bad",
                "scope": {"scope": "selected"},
                "repositories": {"octocat/repo": True},
                "repositoriesNeedSync": "false",
            })

        self.assertIsNone(summary["installationId"])
        self.assertIsNone(summary["installationAccount"])
        self.assertIsNone(summary["installationTargetType"])
        self.assertIsNone(summary["installationAppSlug"])
        self.assertEqual(summary["installationHtmlUrl"], "https://github.com/settings/installations/123")
        self.assertIsNone(summary["repositorySelection"])
        self.assertIsNone(summary["scope"])
        self.assertEqual(summary["repositoryCount"], 0)
        self.assertFalse(summary["repositoriesNeedSync"])
    def test_safe_installation_summaries_do_not_emit_url_aliases(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_GITHUB_WEB_URL": "https://github.com"}, clear=False):
            summaries = app.safe_installation_summaries([
                {"installationId": "123", "htmlUrl": "javascript:alert(1)", "html_url": "https://evil.example/install"}
            ])

        self.assertIsNone(summaries[0]["installationHtmlUrl"])
        self.assertNotIn("htmlUrl", summaries[0])
        self.assertNotIn("html_url", summaries[0])
    def test_safe_installation_summaries_sanitize_malformed_metadata(self) -> None:
        with patch.dict(os.environ, {"PULLWISE_GITHUB_WEB_URL": "https://github.com"}, clear=False):
            summaries = app.safe_installation_summaries([
                {
                    "installationId": {"id": "123"},
                    "installationAccount": ["octocat"],
                    "installationTargetType": {"type": "User"},
                    "installationAppSlug": {"slug": "pullwise"},
                    "installationHtmlUrl": "https://github.com/settings/installations/123",
                    "repositorySelection": "selected\r\nX-Test: bad",
                    "scope": {"scope": "selected"},
                    "repositoryCount": -4,
                    "repositoriesNeedSync": "false",
                    "raw": {"unexpected": "value"},
                }
            ])

        self.assertEqual(summaries, [
            {
                "installationId": None,
                "installationAccount": None,
                "installationTargetType": None,
                "installationAppSlug": None,
                "installationHtmlUrl": "https://github.com/settings/installations/123",
                "repositorySelection": None,
                "scope": None,
                "repositoryCount": 0,
                "repositoriesNeedSync": False,
            }
        ])
    def test_magic_link_routes_are_not_available(self) -> None:
        cases = [
            ("GET", "/dev/magic-links"),
            ("GET", "/auth/email/callback?token=tok_1"),
            ("POST", "/auth/email/magic-link"),
        ]

        for method, path in cases:
            with self.subTest(method=method, path=path):
                handler = RouteHarness(path, {"email": "dev@example.com"})

                app.PullwiseHandler.route(handler, method)

                self.assertEqual(handler.status, HTTPStatus.NOT_FOUND)
    def test_repositories_payload_treats_string_false_need_sync_as_connected(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
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
                    "defaultBranch": "main",
                    "cloneUrl": "https://github.com/octocat/private-repo.git",
                }
            ],
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
        handler = RouteHarness("/repositories", cookie="pw_session=ses_1")

        app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertFalse(handler.payload["needsAuthorization"])
        self.assertFalse(handler.payload["repositoriesNeedSync"])
        self.assertEqual([item["fullName"] for item in handler.payload["items"]], ["octocat/private-repo"])

    def test_repositories_route_pages_before_full_response_enrichment(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
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
            "repositories": [
                "octocat/alpha",
                "octocat/beta",
                "octocat/gamma",
            ],
            "repositoryItems": [
                {
                    "id": str(100 + index),
                    "name": name,
                    "fullName": f"octocat/{name}",
                    "installationId": "111",
                    "installationAccount": "octocat",
                    "defaultBranch": "main",
                    "cloneUrl": f"https://github.com/octocat/{name}.git",
                }
                for index, name in enumerate(("alpha", "beta", "gamma"))
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

        with patch.object(app, "repository_items_for_response", wraps=app.repository_items_for_response) as full_response:
            handler = RouteHarness("/repositories?limit=1", cookie="pw_session=ses_1")
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["total"], 3)
        self.assertEqual(handler.payload["limit"], 1)
        self.assertEqual(handler.payload["offset"], 0)
        self.assertTrue(handler.payload["hasMore"])
        self.assertEqual([item["fullName"] for item in handler.payload["items"]], ["octocat/alpha"])
        self.assertEqual(handler.payload["repositories"], handler.payload["items"])
        self.assertEqual(full_response.call_count, 0)

    def test_repositories_payload_sanitizes_malformed_repository_items(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "scope": "selected",
            "repositorySelection": "selected",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
            "installationId": "111",
            "repositories": ["octocat/private-repo"],
            "repositoryItems": [
                "not a repository object",
                {
                    "id": {"id": "bad"},
                    "name": {"name": "bad"},
                    "fullName": {"owner": "octocat", "repo": "bad"},
                    "cloneUrl": "https://github.com/octocat/bad.git",
                },
                {
                    "id": {"id": "repo_private"},
                    "name": {"name": "private-repo"},
                    "fullName": "octocat/private-repo",
                    "desc": {"text": "bad"},
                    "description": {"text": "bad"},
                    "lang": {"name": "Python"},
                    "private": "false",
                    "stars": {"count": 5},
                    "branches": {"count": 2},
                    "defaultBranch": {"name": "main"},
                    "updated": {"at": "2026-05-25"},
                    "htmlUrl": "javascript:alert(1)",
                    "cloneUrl": "https://evil.example/octocat/private-repo.git",
                    "permissions": {"pull": True, "push": "false"},
                    "installationId": {"id": "111"},
                    "installationAccount": {"login": "octocat"},
                    "installationTargetType": ["User"],
                    "repositorySelection": "selected\r\nX-Test: bad",
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
        handler = RouteHarness("/repositories", cookie="pw_session=ses_1")

        app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertFalse(handler.payload["needsAuthorization"])
        self.assertEqual(len(handler.payload["items"]), 1)
        item = handler.payload["items"][0]
        self.assertEqual(item["id"], "octocat/private-repo")
        self.assertEqual(item["name"], "private-repo")
        self.assertEqual(item["fullName"], "octocat/private-repo")
        self.assertEqual(item["desc"], "")
        self.assertEqual(item["description"], "")
        self.assertEqual(item["lang"], "-")
        self.assertFalse(item["private"])
        self.assertEqual(item["stars"], "-")
        self.assertEqual(item["branches"], "-")
        self.assertEqual(item["defaultBranch"], "main")
        self.assertEqual(item["updated"], "")
        self.assertIsNone(item["htmlUrl"])
        self.assertIsNone(item["cloneUrl"])
        self.assertEqual(item["permissions"], {"pull": True})
        self.assertIsNone(item["installationId"])
        self.assertIsNone(item["installationAccount"])
        self.assertIsNone(item["installationTargetType"])
        self.assertIsNone(item["repositorySelection"])
    def test_github_disconnect_requires_sign_in(self) -> None:
        handler = RouteHarness("/integrations/github")

        app.PullwiseHandler.route(handler, "DELETE")

        self.assertEqual(handler.status, HTTPStatus.UNAUTHORIZED)
    def test_sign_out_clears_current_session_and_cookie(self) -> None:
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + app.SESSION_MAX_AGE,
            }
        }
        handler = RouteHarness("/auth/sign-out", cookie="pw_session=ses_1")

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertNotIn("ses_1", app.SESSIONS)
        self.assertIn("Max-Age=0", handler.headers_out["Set-Cookie"])
    def test_sign_out_clears_valid_session_when_duplicate_session_cookies_exist(self) -> None:
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + app.SESSION_MAX_AGE,
            }
        }
        handler = RouteHarness("/auth/sign-out", cookie="pw_session=ses_1; pw_session=stale_host_cookie")

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertNotIn("ses_1", app.SESSIONS)
        self.assertIn("Max-Age=0", handler.headers_out["Set-Cookie"])


__all__ = ["SecurityContractsPart02Test"]
