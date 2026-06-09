from __future__ import annotations

try:
    import security_contracts_base as _security_contracts_base
except ModuleNotFoundError:  # pragma: no cover - package-style unittest invocation
    from . import security_contracts_base as _security_contracts_base

globals().update(
    {name: getattr(_security_contracts_base, name) for name in dir(_security_contracts_base) if not name.startswith("_")}
)


class SecurityContractsPart02Test(SecurityContractsBase):
    def test_issue_read_routes_reject_malformed_issue_fields(self) -> None:
        app.ISSUES[0].update({
            "scanId": {"value": "sc_1"},
            "repo": {"fullName": "owner/repo"},
            "repository": "owner/repo",
            "branch": "main\r\nX-Injected: bad",
            "severity": "critical\r\nX-Injected: bad",
            "category": "security\r\nX-Injected: bad",
            "title": "Unsafe redirect\r\nX-Injected: bad",
            "summary": {"text": "bad shape"},
            "impact": "Phishing risk.\x00",
            "file": "src/app.py\r\n../secret",
            "line": {"value": 12},
            "confidence": float("nan"),
            "autoFix": "false",
            "autoFixable": "true",
            "effort": {"value": "5 min"},
            "tags": ["safe-tag", "bad\r\nextra", {"value": "bad"}],
            "steps": ["Use allowlist.", "bad\r\nextra"],
            "badCode": [
                {"ln": 1, "code": "bad\r\nextra", "t": "del"},
                {"ln": 2, "code": "return redirect(next_url)", "t": "bad"},
            ],
            "goodCode": [{"ln": {"value": 2}, "code": "return safe_redirect(next_url)", "t": "add"}],
            "references": [
                {"label": "Docs\r\nInjected", "url": "https://example.com/docs"},
                {"label": "Unsafe", "url": "javascript:alert(1)"},
                {"label": "Safe", "url": "https://example.com/safe"},
            ],
            "createdAt": {"value": app.now()},
        })

        expected = {
            "id": "iss_1",
            "userId": "usr_1",
            "scanId": "",
            "jobId": "",
            "repo": "",
            "branch": "main",
            "commit": "pending",
            "status": "open",
            "severity": "medium",
            "category": "Quality",
            "title": "Untitled finding",
            "summary": "",
            "impact": "",
            "detectionReasoning": "",
            "reproductionPath": "",
            "verificationStatus": "potential_risk",
            "verificationSummary": "",
            "affectedLocations": [],
            "evidence": [],
            "reproduction": {
                "commands": [],
                "input": "",
                "expected": "",
                "actual": "",
                "testFile": "",
                "logPath": "",
            },
            "whyNotFalsePositive": [],
            "limitations": [],
            "evidenceChecklist": [
                {"label": "Fixed commit", "met": False},
                {"label": "Precise file and line", "met": False},
                {"label": "Evidence chain", "met": False},
                {"label": "Reproduction command", "met": False},
                {"label": "Runtime output", "met": False},
                {"label": "Raw log or test", "met": False},
            ],
            "confidenceLevel": "low",
            "evidenceTrace": [
                {
                    "key": "code",
                    "label": "Code",
                    "status": "missing",
                    "summary": "No code location evidence was captured.",
                    "items": [],
                },
                {
                    "key": "path",
                    "label": "Path",
                    "status": "missing",
                    "summary": "No reachability or data-flow evidence was captured.",
                    "items": [],
                },
                {
                    "key": "trigger",
                    "label": "Trigger",
                    "status": "missing",
                    "summary": "No trigger input or reproduction command was captured.",
                    "items": [],
                },
                {
                    "key": "runtime",
                    "label": "Runtime",
                    "status": "missing",
                    "summary": "No runtime output or test evidence was captured.",
                    "items": [],
                },
                {
                    "key": "impact",
                    "label": "Impact",
                    "status": "missing",
                    "summary": "No impact statement was captured.",
                    "items": [],
                },
                {
                    "key": "fix",
                    "label": "Fix",
                    "status": "present",
                    "summary": "Remediation step: Use allowlist.",
                    "items": [
                        "Remediation step: Use allowlist.",
                        "Suggested patch evidence is available for review.",
                    ],
                },
            ],
            "reasoningBreakdown": {
                "facts": [],
                "inferences": [],
                "recommendations": [
                    "Use allowlist.",
                    "Inspect the suggested patch evidence and validate it before applying changes.",
                ],
            },
            "audit": {"branch": "main", "commit": "pending"},
            "file": "",
            "line": 0,
            "confidence": 0.0,
            "confidenceRationale": "",
            "autoFix": False,
            "autoFixable": False,
            "effort": "-",
            "fixBenefits": "",
            "fixRisks": "",
            "tags": ["safe-tag"],
            "steps": ["Use allowlist."],
            "badCode": [
                {"ln": 1, "code": "bad\nextra", "t": "del"},
                {"ln": 2, "code": "return redirect(next_url)", "t": None},
            ],
            "goodCode": [{"ln": 0, "code": "return safe_redirect(next_url)", "t": "add"}],
            "references": [{"label": "Safe", "url": "https://example.com/safe"}],
            "createdAt": 0,
        }

        for path in ("/issues", "/issues/iss_1"):
            with self.subTest(path=path):
                handler = RouteHarness(path, cookie=self.signed_in())

                app.PullwiseHandler.route(handler, "GET")

                self.assertEqual(handler.status, HTTPStatus.OK)
                issue = handler.payload["items"][0] if path == "/issues" else handler.payload
                self.assertEqual(issue, expected)
    def test_issue_payload_hides_auto_fix_for_empty_replacement(self) -> None:
        app.ISSUES[0].update({
            "scanId": "sc_1",
            "repo": "owner/repo",
            "file": "src/auth.py",
            "autoFix": True,
            "badCode": [{"ln": 1, "code": "old()", "t": "del"}],
            "goodCode": [],
        })

        payload = app.issue_payload(app.ISSUES[0])

        self.assertIs(payload["autoFix"], False)
        self.assertIs(payload["autoFixable"], False)
    def test_issue_payload_hides_auto_fix_for_non_contiguous_bad_code(self) -> None:
        app.ISSUES[0].update({
            "scanId": "sc_1",
            "repo": "owner/repo",
            "file": "src/auth.py",
            "autoFix": True,
            "badCode": [
                {"ln": 1, "code": "first()", "t": "del"},
                {"ln": 3, "code": "third()", "t": "del"},
            ],
            "goodCode": [{"ln": 1, "code": "fixed()", "t": "add"}],
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                repo_path = app.checkout.checkout_path_for("usr_1", "sc_1", "owner/repo")
                os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
                with open(os.path.join(repo_path, "src", "auth.py"), "w", encoding="utf-8") as handle:
                    handle.write("first()\nsecond()\nthird()\n")
                app.SCANS[0]["repoPath"] = repo_path

                payload = app.issue_payload(app.ISSUES[0])

        self.assertIs(payload["autoFix"], False)
        self.assertIs(payload["autoFixable"], False)
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
    def test_issue_reads_require_sign_in(self) -> None:
        for path in ["/issues", "/issues/iss_1"]:
            with self.subTest(path=path):
                handler = RouteHarness(path)

                app.PullwiseHandler.route(handler, "GET")

                self.assertEqual(handler.status, HTTPStatus.UNAUTHORIZED)
    def test_scan_reads_require_sign_in(self) -> None:
        for path in ["/scans", "/scans/sc_1"]:
            with self.subTest(path=path):
                handler = RouteHarness(path)

                app.PullwiseHandler.route(handler, "GET")

                self.assertEqual(handler.status, HTTPStatus.UNAUTHORIZED)
    def test_missing_resource_routes_return_not_found_without_internal_details(self) -> None:
        cases = [
            ("GET", "/scans/missing", {}, "Scan not found."),
            ("POST", "/scans/missing/cancel", {}, "Scan not found."),
            ("GET", "/issues/missing", {}, "Issue not found."),
            ("POST", "/issues/missing/fixes/preview", {}, "Issue not found."),
            ("PATCH", "/issues/missing/status", {"status": "fixed"}, "Issue not found."),
        ]

        for method, path, body, message in cases:
            with self.subTest(method=method, path=path):
                handler = RouteHarness(path, body, cookie=self.signed_in())

                with patch.object(app.logger, "exception") as log_exception:
                    app.PullwiseHandler.route(handler, method)

                self.assertEqual(handler.status, HTTPStatus.NOT_FOUND)
                self.assertEqual(handler.payload, {"message": message})
                log_exception.assert_not_called()
    def test_scan_read_routes_reject_malformed_scan_fields(self) -> None:
        app.SCANS[0].update({
            "repo": {"fullName": "owner/repo"},
            "repository": "owner/repo",
            "branch": "main\r\nX-Injected: bad",
            "commit": {"sha": "abc123"},
            "status": "done",
            "phase": "ai\r\nX-Injected: bad",
            "progress": float("nan"),
            "issues": {
                "critical": -1,
                "high": "3",
                "medium": float("nan"),
                "low": {"count": 2},
                "info": True,
                "unexpected": 99,
            },
            "error": "Provider failed\r\nX-Injected: bad",
            "createdAt": {"value": app.now()},
            "queuedAt": {"value": app.now()},
            "startedAt": "123",
            "completedAt": True,
            "repoPath": "C:\\secret\\checkout",
            "cloneUrl": "javascript:alert(1)",
            "installationId": {"id": "123"},
        })

        expected = {
            "id": "sc_1",
            "userId": "usr_1",
            "repo": "",
            "branch": "main",
            "commit": "pending",
            "status": "done",
            "phase": "",
            "progress": 0,
            "issues": {"critical": 0, "high": 3, "medium": 0, "low": 0, "info": 0},
            "verification": {"verified": 0, "static_proof": 0, "potential_risk": 0, "unverified": 0},
            "error": "Provider failed",
            "createdAt": 0,
            "queuedAt": 0,
            "startedAt": 123,
            "completedAt": 0,
            "installationId": None,
            "cloneUrl": None,
        }

        for path in ("/scans", "/scans/sc_1"):
            with self.subTest(path=path):
                handler = RouteHarness(path, cookie=self.signed_in())

                app.PullwiseHandler.route(handler, "GET")

                self.assertEqual(handler.status, HTTPStatus.OK)
                scan = handler.payload["items"][0] if path == "/scans" else handler.payload
                self.assertEqual(scan, expected)
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
    def test_scan_creation_rejects_non_object_body(self) -> None:
        initial_scan_count = len(app.SCANS)
        handler = RouteHarness("/scans", ["owner/repo"], cookie=self.signed_in())

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload["message"], "Request body must be a JSON object.")
        self.assertEqual(len(app.SCANS), initial_scan_count)
    def test_scan_creation_uses_repository_item_installation_id(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "scope": "mixed",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
            "installationIds": ["111", "222"],
            "installationAccounts": ["octocat", "acme"],
            "repositories": ["octocat/private-repo", "acme/service"],
            "repositoryItems": [
                {
                    "id": "repo_private",
                    "name": "private-repo",
                    "fullName": "octocat/private-repo",
                    "installationId": "111",
                    "installationAccount": "octocat",
                    "defaultBranch": "main",
                    "cloneUrl": "https://github.com/octocat/private-repo.git",
                },
                {
                    "id": "repo_service",
                    "name": "service",
                    "fullName": "acme/service",
                    "installationId": "222",
                    "installationAccount": "acme",
                    "defaultBranch": "develop",
                    "cloneUrl": "https://github.com/acme/service.git",
                },
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
        handler = RouteHarness("/scans", {"repo": "acme/service"}, cookie="pw_session=ses_1")

        with (
            patch.dict(os.environ, {"PULLWISE_DB_PATH": self.db_path}, clear=True),
        ):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.CREATED)
        self.assertEqual(handler.payload["installationId"], "222")
        self.assertEqual(handler.payload["installationAccount"], "acme")
        self.assertEqual(handler.payload["branch"], "develop")
        self.assertEqual(handler.payload["cloneUrl"], "https://github.com/acme/service.git")
    def test_scan_creation_sanitizes_malformed_branch_and_commit_inputs(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "scope": "selected",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
            "installationId": "111",
            "repositories": ["owner/repo"],
            "repositoryItems": [
                {
                    "id": "repo_1",
                    "name": "repo",
                    "fullName": "owner/repo",
                    "installationId": "111",
                    "defaultBranch": "main",
                    "cloneUrl": "https://github.com/owner/repo.git",
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
            "/scans",
            {
                "repo": "owner/repo",
                "branch": "feature\r\nX-Test: bad",
                "commit": {"sha": "bad"},
                "requestId": {"bad": "shape"},
                "idempotencyKey": "safe_scan_req",
            },
            cookie="pw_session=ses_1",
        )

        with (
            patch.dict(os.environ, {"PULLWISE_DB_PATH": self.db_path}, clear=True),
        ):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.CREATED)
        self.assertEqual(handler.payload["branch"], "main")
        self.assertEqual(handler.payload["commit"], "pending")
        self.assertEqual(app.SCANS[0]["requestId"], "safe_scan_req")
    def test_scan_creation_is_idempotent_for_repeated_request_id(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "scope": "selected",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
            "installationId": "111",
            "repositories": ["owner/repo"],
            "repositoryItems": [
                {
                    "id": "repo_1",
                    "name": "repo",
                    "fullName": "owner/repo",
                    "installationId": "111",
                    "defaultBranch": "main",
                    "cloneUrl": "https://github.com/owner/repo.git",
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

        first = RouteHarness(
            "/scans",
            {"repo": "owner/repo", "requestId": "scan_req_1"},
            cookie="pw_session=ses_1",
        )
        second = RouteHarness(
            "/scans",
            {"repo": "owner/repo", "requestId": "scan_req_1"},
            cookie="pw_session=ses_1",
        )

        with (
            patch.dict(os.environ, {"PULLWISE_DB_PATH": self.db_path}, clear=True),
        ):
            app.PullwiseHandler.route(first, "POST")
            app.PullwiseHandler.route(second, "POST")

        self.assertEqual(first.status, HTTPStatus.CREATED)
        self.assertEqual(second.status, HTTPStatus.OK)
        self.assertEqual(first.payload["id"], second.payload["id"])
        self.assertEqual(len([scan for scan in app.SCANS if scan.get("requestId") == "scan_req_1"]), 1)
    def test_scan_creation_rejects_request_id_reuse_for_different_repo(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "scope": "selected",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
            "installationId": "111",
            "repositories": ["owner/repo", "owner/other"],
            "repositoryItems": [
                {
                    "id": "repo_1",
                    "githubRepoId": "101",
                    "name": "repo",
                    "fullName": "owner/repo",
                    "installationId": "111",
                    "defaultBranch": "main",
                    "cloneUrl": "https://github.com/owner/repo.git",
                },
                {
                    "id": "repo_2",
                    "githubRepoId": "202",
                    "name": "other",
                    "fullName": "owner/other",
                    "installationId": "111",
                    "defaultBranch": "main",
                    "cloneUrl": "https://github.com/owner/other.git",
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

        first = RouteHarness(
            "/scans",
            {"repo": "owner/repo", "requestId": "scan_req_shared"},
            cookie="pw_session=ses_1",
        )
        second = RouteHarness(
            "/scans",
            {"repo": "owner/other", "requestId": "scan_req_shared"},
            cookie="pw_session=ses_1",
        )

        with (
            patch.dict(os.environ, {"PULLWISE_DB_PATH": self.db_path}, clear=True),
        ):
            app.PullwiseHandler.route(first, "POST")
            app.PullwiseHandler.route(second, "POST")

        self.assertEqual(first.status, HTTPStatus.CREATED)
        self.assertEqual(second.status, HTTPStatus.CONFLICT)
        self.assertEqual(second.payload["code"], "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(second.payload["repoId"], first.payload["repoId"])
        self.assertEqual(len([scan for scan in app.SCANS if scan.get("requestId") == "scan_req_shared"]), 1)
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
    def test_scan_creation_rejects_repository_access_that_needs_sync_even_with_stale_repo_names(self) -> None:
        app.USERS["usr_1"]["providers"] = ["github"]
        app.USERS["usr_1"]["githubRepositoryAccess"] = {
            "mode": "github-app",
            "scope": "selected",
            "authorizedUserId": "usr_1",
            "authorizedGithubId": "1",
            "authorizedGithubLogin": "octocat",
            "installationId": "111",
            "installationAccount": "octocat",
            "installationTargetType": "User",
            "repositories": ["octocat/stale-repo"],
            "repositoryItems": [
                {
                    "id": "repo_stale",
                    "name": "stale-repo",
                    "fullName": "octocat/stale-repo",
                    "installationId": "111",
                    "installationAccount": "octocat",
                }
            ],
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
        initial_scan_count = len(app.SCANS)
        handler = RouteHarness("/scans", {"repo": "octocat/stale-repo"}, cookie="pw_session=ses_1")

        with (
            patch.dict(os.environ, {"PULLWISE_DB_PATH": self.db_path}, clear=True),
        ):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.FORBIDDEN)
        self.assertIn("Sync GitHub repositories", handler.payload["message"])
        self.assertEqual(len(app.SCANS), initial_scan_count)
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
