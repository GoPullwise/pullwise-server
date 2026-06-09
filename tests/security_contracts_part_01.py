from __future__ import annotations

try:
    import security_contracts_base as _security_contracts_base
except ModuleNotFoundError:  # pragma: no cover - package-style unittest invocation
    from . import security_contracts_base as _security_contracts_base

globals().update(
    {name: getattr(_security_contracts_base, name) for name in dir(_security_contracts_base) if not name.startswith("_")}
)


class SecurityContractsPart01Test(SecurityContractsBase):
    def test_scans_route_filters_and_paginates_signed_in_user_results(self) -> None:
        app.SCANS = [
            {
                "id": "sc_new",
                "userId": "usr_1",
                "status": "done",
                "repo": "owner/repo",
                "createdAt": 300,
            },
            {
                "id": "sc_old",
                "userId": "usr_1",
                "status": "done",
                "repo": "owner/repo",
                "createdAt": 100,
            },
            {
                "id": "sc_other",
                "userId": "usr_1",
                "status": "running",
                "repo": "owner/other",
                "createdAt": 200,
            },
            {
                "id": "sc_foreign",
                "userId": "usr_2",
                "status": "done",
                "repo": "owner/repo",
                "createdAt": 400,
            },
        ]
        handler = RouteHarness(
            "/scans?status=done&repo=owner/repo&limit=1&offset=1",
            cookie=self.signed_in(),
        )

        app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["total"], 2)
        self.assertEqual(handler.payload["limit"], 1)
        self.assertEqual(handler.payload["offset"], 1)
        self.assertFalse(handler.payload["hasMore"])
        self.assertEqual([scan["id"] for scan in handler.payload["items"]], ["sc_old"])
        self.assertEqual(handler.payload["scans"], handler.payload["items"])
    def test_issues_route_filters_and_paginates_signed_in_user_results(self) -> None:
        app.ISSUES = [
            {
                "id": "iss_auth",
                "userId": "usr_1",
                "status": "open",
                "severity": "high",
                "title": "Auth redirect bypass",
                "repo": "owner/repo",
                "file": "src/auth.py",
            },
            {
                "id": "iss_fixed",
                "userId": "usr_1",
                "status": "fixed",
                "severity": "high",
                "title": "Auth token leak",
                "repo": "owner/repo",
                "file": "src/auth.py",
            },
            {
                "id": "iss_low",
                "userId": "usr_1",
                "status": "open",
                "severity": "low",
                "title": "Auth copy issue",
                "repo": "owner/repo",
                "file": "src/ui.py",
            },
            {
                "id": "iss_foreign",
                "userId": "usr_2",
                "status": "open",
                "severity": "high",
                "title": "Auth foreign issue",
                "repo": "owner/repo",
                "file": "src/auth.py",
            },
        ]
        handler = RouteHarness(
            "/issues?status=open&severity=high&q=redirect&limit=1",
            cookie=self.signed_in(),
        )

        app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["total"], 1)
        self.assertEqual(handler.payload["limit"], 1)
        self.assertEqual(handler.payload["offset"], 0)
        self.assertFalse(handler.payload["hasMore"])
        self.assertEqual([issue["id"] for issue in handler.payload["items"]], ["iss_auth"])
        self.assertEqual(handler.payload["issues"], handler.payload["items"])
    def test_route_ignores_client_disconnect_without_500_response(self) -> None:
        handler = DisconnectingRouteHarness("/auth/session")

        with (
            patch.object(app, "ensure_state_loaded"),
            patch.object(app, "rate_limit_enabled", return_value=False),
            patch.object(app.logger, "exception") as log_exception,
        ):
            app.PullwiseHandler.route(handler, "GET")

        log_exception.assert_not_called()
        self.assertIsNone(handler.status)
    def test_static_file_guard_does_not_authorize_sibling_directory_by_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = os.path.join(tmpdir, "web")
            sibling = os.path.join(tmpdir, "web-secret")
            os.makedirs(root)
            os.makedirs(sibling)
            index_path = os.path.join(root, "index.html")
            secret_path = os.path.join(sibling, "secret.txt")
            with open(index_path, "w", encoding="utf-8") as handle:
                handle.write("<div>app</div>")
            with open(secret_path, "w", encoding="utf-8") as handle:
                handle.write("secret")

            handler = RouteHarness("/../web-secret/secret.txt")
            served_paths: list[str] = []

            def capture_static_file(file_path: str) -> None:
                served_paths.append(os.path.normpath(file_path))
                handler.status = HTTPStatus.OK

            handler.serve_static_file = capture_static_file

            with (
                patch.dict(os.environ, {"PULLWISE_WEB_DIR": root}, clear=False),
                patch.object(app, "ensure_state_loaded"),
                patch.object(app, "rate_limit_enabled", return_value=False),
            ):
                app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(served_paths, [os.path.normpath(index_path)])
    def test_json_raises_client_disconnected_for_aborted_socket(self) -> None:
        class FailingWriter:
            def write(self, _: bytes) -> None:
                raise ConnectionAbortedError(10053, "aborted")

        handler = app.PullwiseHandler.__new__(app.PullwiseHandler)
        handler.path = "/auth/session"
        handler.command = "GET"
        handler.requestline = "GET /auth/session HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.client_address = ("127.0.0.1", 41229)
        handler.headers = {"Host": "api.pullwise.dev", "Cookie": ""}
        handler.wfile = FailingWriter()

        with self.assertRaises(app.ClientDisconnected):
            app.PullwiseHandler.json(handler, {"authenticated": False})
    def test_wildcard_allowed_origin_does_not_allow_open_redirects(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PULLWISE_APP_URL": "https://app.pullwise.dev",
                "PULLWISE_ALLOWED_ORIGINS": "*",
            },
            clear=True,
        ):
            self.assertEqual(
                app.safe_redirect_to("https://evil.example/callback", "dashboard"),
                "https://app.pullwise.dev/dashboard",
            )
    def test_github_login_authorize_defaults_to_dashboard_redirect(self) -> None:
        handler = RouteHarness("/auth/github/authorize")

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
            patch("pullwise_server.github_auth.build_oauth_authorize_url", return_value="https://github.com/login/oauth/authorize"),
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["url"], "https://github.com/login/oauth/authorize")
        record = next(iter(app.GITHUB_STATES.values()))
        self.assertEqual(record["redirectTo"], "https://app.pullwise.dev/dashboard")
    def test_github_login_authorize_can_redirect_to_github_for_browser_navigation(self) -> None:
        handler = RouteHarness(
            "/auth/github/authorize?response=redirect&redirectTo=https%3A%2F%2Fadmin.pull-wise.com%2Fworkers"
        )

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                    "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                    "PULLWISE_APP_URL": "https://admin.pull-wise.com",
                    "PULLWISE_ALLOWED_ORIGINS": "https://admin.pull-wise.com",
                    "PULLWISE_API_BASE_URL": "https://api.pull-wise.com",
                },
                clear=True,
            ),
            patch("pullwise_server.github_auth.build_oauth_authorize_url", return_value="https://github.com/login/oauth/authorize?client_id=pw"),
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.FOUND)
        self.assertEqual(handler.location, "https://github.com/login/oauth/authorize?client_id=pw")
        record = next(iter(app.GITHUB_STATES.values()))
        self.assertEqual(record["redirectTo"], "https://admin.pull-wise.com/workers")
    def test_github_login_authorize_keeps_pullwise_admin_redirect_when_only_main_origin_is_configured(self) -> None:
        handler = RouteHarness(
            "/auth/github/authorize?response=redirect&redirectTo=https%3A%2F%2Fadmin.pull-wise.com%2Fworkers"
        )

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                    "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                    "PULLWISE_APP_URL": "https://pull-wise.com",
                    "PULLWISE_ALLOWED_ORIGINS": "https://pull-wise.com",
                    "PULLWISE_API_BASE_URL": "https://api.pull-wise.com",
                },
                clear=True,
            ),
            patch("pullwise_server.github_auth.build_oauth_authorize_url", return_value="https://github.com/login/oauth/authorize?client_id=pw"),
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.FOUND)
        record = next(iter(app.GITHUB_STATES.values()))
        self.assertEqual(record["redirectTo"], "https://admin.pull-wise.com/workers")
    def test_local_github_login_authorize_redirect_mode_returns_callback_redirect(self) -> None:
        handler = RouteHarness(
            "/auth/github/authorize?response=redirect&redirectTo=https%3A%2F%2Fadmin.pull-wise.com%2Fworkers"
        )

        with patch.dict(
            os.environ,
            {
                "PULLWISE_APP_URL": "https://admin.pull-wise.com",
                "PULLWISE_ALLOWED_ORIGINS": "https://admin.pull-wise.com",
                "PULLWISE_API_BASE_URL": "https://api.pull-wise.com",
                "PULLWISE_ENABLE_LOCAL_GITHUB_MOCKS": "true",
            },
            clear=True,
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.FOUND)
        self.assertEqual(
            handler.location,
            "https://api.pull-wise.com/auth/github/callback?redirectTo=https%3A%2F%2Fadmin.pull-wise.com%2Fworkers",
        )
    def test_github_login_authorize_prefers_configured_callback_url_over_proxy_headers(self) -> None:
        handler = RouteHarness(
            "/auth/github/authorize?redirectTo=https%3A%2F%2Fpullwise-admin.danuberiverferryman.workers.dev%2Flogin",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "pullwise-admin.danuberiverferryman.workers.dev",
                "X-Forwarded-Prefix": "/api",
            },
        )

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_GITHUB_CLIENT_ID": "client_id",
                    "PULLWISE_GITHUB_CLIENT_SECRET": "client_secret",
                    "PULLWISE_APP_URL": "https://pullwise-admin.danuberiverferryman.workers.dev",
                    "PULLWISE_ALLOWED_ORIGINS": "https://pullwise-admin.danuberiverferryman.workers.dev",
                    "PULLWISE_API_BASE_URL": "https://api.pull-wise.com",
                    "PULLWISE_TRUST_PROXY_HEADERS": "true",
                },
                clear=True,
            ),
            patch("pullwise_server.github_auth.build_oauth_authorize_url", return_value="https://github.com/login/oauth/authorize") as build_authorize_url,
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(
            build_authorize_url.call_args.args[0],
            "https://api.pull-wise.com/auth/github/callback",
        )
    def test_github_callback_rejects_malformed_persisted_state_records(self) -> None:
        cases = {
            "non_object": "not-a-state-record",
            "malformed_expiry": {
                "kind": "login",
                "redirectTo": "https://app.pullwise.dev/?screen=dashboard",
                "expiresAt": {"value": app.now() + 60},
                "codeVerifier": "verifier",
            },
        }

        for state, record in cases.items():
            with self.subTest(state=state):
                app.GITHUB_STATES = {state: record}
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
                    patch.object(app.logger, "exception") as log_exception,
                ):
                    app.PullwiseHandler.route(handler, "GET")

                self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
                self.assertEqual(handler.payload["message"], "GitHub authorization state is invalid or expired.")
                self.assertEqual(app.GITHUB_STATES, {})
                log_exception.assert_not_called()
    def test_real_github_user_uses_safe_login_fallback_for_malformed_profile_id(self) -> None:
        app.USERS = {}

        user = app.get_or_create_real_github_user(
            {
                "id": {"node_id": "bad"},
                "login": "OctoCat",
                "primaryEmail": "octocat@example.com",
                "name": "Octo Cat",
            },
            {"access_token": "gho_user", "token_type": "bearer", "scope": "read:user"},
        )

        self.assertEqual(user["id"], "usr_github_octocat")
        self.assertEqual(user["githubId"], "octocat")
        self.assertNotIn("usr_github_{'node_id': 'bad'}", app.USERS)
    def test_real_github_user_sanitizes_malformed_profile_display_fields(self) -> None:
        app.USERS = {}

        user = app.get_or_create_real_github_user(
            {
                "id": 123,
                "login": "OctoCat",
                "primaryEmail": {"email": "bad@example.com"},
                "email": {"email": "bad@example.com"},
                "name": {"display": "Bad Name"},
                "avatar_url": {"url": "https://avatars.githubusercontent.com/u/123"},
                "html_url": "javascript:alert(1)",
            },
            {"access_token": "gho_user", "token_type": "bearer", "scope": "read:user"},
        )

        self.assertEqual(user["name"], "OctoCat")
        self.assertEqual(user["email"], "OctoCat@users.noreply.github.com")
        self.assertIsNone(user["avatarUrl"])
        self.assertIsNone(user["githubHtmlUrl"])
    def test_issue_status_update_requires_sign_in(self) -> None:
        handler = RouteHarness("/issues/iss_1/status", {"status": "ignored"})

        app.PullwiseHandler.route(handler, "PATCH")

        self.assertEqual(handler.status, HTTPStatus.UNAUTHORIZED)
    def test_issue_status_update_uses_identity_fields_when_issue_ids_collide(self) -> None:
        app.ISSUES = [
            {
                "id": "dup_issue",
                "userId": "usr_1",
                "scanId": "sc_1",
                "jobId": "job_1",
                "repo": "owner/repo",
                "file": "src/app.py",
                "line": 10,
                "title": "Duplicate issue",
                "createdAt": 100,
                "status": "open",
            },
            {
                "id": "dup_issue",
                "userId": "usr_1",
                "scanId": "sc_1",
                "jobId": "job_1",
                "repo": "owner/repo",
                "file": "src/app.py",
                "line": 20,
                "title": "Duplicate issue",
                "createdAt": 101,
                "status": "open",
            },
        ]
        handler = RouteHarness(
            "/issues/dup_issue/status",
            {
                "status": "fixed",
                "scanId": "sc_1",
                "jobId": "job_1",
                "repo": "owner/repo",
                "file": "src/app.py",
                "line": 20,
                "title": "Duplicate issue",
                "createdAt": 101,
            },
            cookie=self.signed_in(),
        )

        app.PullwiseHandler.route(handler, "PATCH")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(app.ISSUES[0]["status"], "open")
        self.assertEqual(app.ISSUES[1]["status"], "fixed")
        self.assertEqual(handler.payload["line"], 20)
    def test_resource_routes_decode_percent_encoded_path_ids(self) -> None:
        app.ISSUES[0]["id"] = "iss/with spaces#1"
        app.SCANS[0]["id"] = "sc/with spaces#1"
        app.SCANS[0]["status"] = "queued"
        cookie = self.signed_in()

        issue_read = RouteHarness("/issues/iss%2Fwith%20spaces%231", cookie=cookie)
        scan_read = RouteHarness("/scans/sc%2Fwith%20spaces%231", cookie=cookie)
        issue_update = RouteHarness(
            "/issues/iss%2Fwith%20spaces%231/status",
            {"status": "fixed"},
            cookie=cookie,
        )
        scan_cancel = RouteHarness("/scans/sc%2Fwith%20spaces%231/cancel", cookie=cookie)

        app.PullwiseHandler.route(issue_read, "GET")
        app.PullwiseHandler.route(scan_read, "GET")
        app.PullwiseHandler.route(issue_update, "PATCH")
        app.PullwiseHandler.route(scan_cancel, "POST")

        self.assertEqual(issue_read.status, HTTPStatus.OK)
        self.assertEqual(issue_read.payload["id"], "iss/with spaces#1")
        self.assertEqual(scan_read.status, HTTPStatus.OK)
        self.assertEqual(scan_read.payload["id"], "sc/with spaces#1")
        self.assertEqual(issue_update.status, HTTPStatus.OK)
        self.assertEqual(issue_update.payload["status"], "fixed")
        self.assertEqual(scan_cancel.status, HTTPStatus.OK)
        self.assertEqual(scan_cancel.payload["status"], "cancelled")
    def test_issue_fix_preview_requires_sign_in(self) -> None:
        handler = RouteHarness("/issues/iss_1/fixes/preview")

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.UNAUTHORIZED)
    def test_issue_fix_preview_returns_deterministic_preview(self) -> None:
        app.ISSUES = [
            {
                "id": "iss_1",
                "userId": "usr_1",
                "status": "open",
                "title": "Example",
                "repo": "owner/repo",
                "scanId": "sc_1",
                "autoFix": True,
                "file": "src/auth.py",
                "badCode": [{"ln": 1, "code": "old()", "t": "del"}],
                "goodCode": [{"ln": 1, "code": "new()", "t": "add"}],
            }
        ]
        preview = {
            "issueId": "iss_1",
            "autoFixable": True,
            "valid": True,
            "repository": "owner/repo",
            "branch": "main",
            "file": "src/auth.py",
            "diff": "--- a/src/auth.py\n+++ b/src/auth.py\n-old()\n+new()\n",
            "summary": "1 file changed",
        }
        handler = RouteHarness("/issues/iss_1/fixes/preview", cookie=self.signed_in())

        with patch("pullwise_server.app.preview_issue_fix_for_user", return_value=preview) as preview_fix:
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertTrue(handler.payload["valid"])
        self.assertIn("-old()", handler.payload["diff"])
        self.assertNotIn("originalContent", handler.payload)
        self.assertNotIn("updatedContent", handler.payload)
        preview_fix.assert_called_once_with(app.USERS["usr_1"], app.ISSUES[0])
    def test_issue_fix_preview_maps_helper_value_error_to_bad_request(self) -> None:
        handler = RouteHarness("/issues/iss_1/fixes/preview", cookie=self.signed_in())

        with patch("pullwise_server.app.preview_issue_fix_for_user", side_effect=ValueError("No checkout")):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload["message"], "No checkout")
    def test_issue_fix_preview_returns_bad_request_for_invalid_preview(self) -> None:
        preview = {
            "issueId": "iss_1",
            "autoFixable": True,
            "valid": False,
            "message": "Old block was not found.",
        }
        handler = RouteHarness("/issues/iss_1/fixes/preview", cookie=self.signed_in())

        with patch("pullwise_server.app.preview_issue_fix_for_user", return_value=preview):
            app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload, preview)
    def test_preview_issue_fix_for_user_prepares_checkout_after_worker_cleanup(self) -> None:
        app.ISSUES[0].update({
            "repo": "owner/repo",
            "scanId": "sc_1",
            "autoFix": True,
            "file": "src/auth.py",
            "badCode": [{"ln": 1, "code": "old()", "t": "del"}],
            "goodCode": [{"ln": 1, "code": "new()", "t": "add"}],
        })
        app.SCANS[0].update({
            "branch": "main",
            "commit": "abc1234",
            "repoPath": None,
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                repo_path = app.checkout.checkout_path_for("usr_1", "sc_1", "owner/repo")
                os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
                with open(os.path.join(repo_path, "src", "auth.py"), "w", encoding="utf-8") as handle:
                    handle.write("old()\n")

                with (
                    patch("pullwise_server.app.checkout.prepare_checkout", return_value=repo_path) as prepare_checkout,
                    patch("pullwise_server.app.checkout.cleanup_scan_workspace") as cleanup_scan_workspace,
                ):
                    preview = app.preview_issue_fix_for_user(app.USERS["usr_1"], app.ISSUES[0])

        self.assertTrue(preview["valid"])
        self.assertIn("-old()", preview["diff"])
        self.assertIn("+new()", preview["diff"])
        prepare_checkout.assert_called_once()
        cleanup_scan_workspace.assert_called_once_with("usr_1", "sc_1")
        self.assertIsNone(app.SCANS[0]["repoPath"])
        self.assertNotIn("sc_1", app.PREVIEW_SCAN_LOCKS)
    def test_preview_issue_fix_for_user_reuses_existing_workspace_repo_path(self) -> None:
        app.ISSUES[0].update({
            "repo": "owner/repo",
            "scanId": "sc_1",
            "autoFix": True,
            "file": "src/auth.py",
            "badCode": [{"ln": 1, "code": "old()", "t": "del"}],
            "goodCode": [{"ln": 1, "code": "new()", "t": "add"}],
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                workspace = app.checkout.workspace_path_for("usr_1", "sc_1")
                repo_path = os.path.join(workspace, "owner_repo")
                os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
                with open(os.path.join(repo_path, "src", "auth.py"), "w", encoding="utf-8") as handle:
                    handle.write("old()\n")
                app.SCANS[0]["repoPath"] = repo_path

                with (
                    patch("pullwise_server.app.checkout.prepare_checkout", side_effect=AssertionError("prepare_checkout should not run")),
                    patch("pullwise_server.app.checkout.cleanup_scan_workspace", side_effect=AssertionError("cleanup should not run")),
                ):
                    preview = app.preview_issue_fix_for_user(app.USERS["usr_1"], app.ISSUES[0])

        self.assertTrue(preview["valid"])
        self.assertIn("-old()", preview["diff"])
        self.assertIn("+new()", preview["diff"])
    def test_preview_issue_fix_for_user_refreshes_stale_workspace_repo_path(self) -> None:
        app.ISSUES[0].update({
            "repo": "owner/repo",
            "scanId": "sc_1",
            "autoFix": True,
            "file": "src/auth.py",
            "badCode": [{"ln": 1, "code": "old()", "t": "del"}],
            "goodCode": [{"ln": 1, "code": "new()", "t": "add"}],
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                workspace = app.checkout.workspace_path_for("usr_1", "sc_1")
                stale_path = os.path.join(workspace, "stale_repo")
                fresh_path = os.path.join(workspace, "fresh_repo")
                os.makedirs(os.path.join(fresh_path, "src"), exist_ok=True)
                with open(os.path.join(fresh_path, "src", "auth.py"), "w", encoding="utf-8") as handle:
                    handle.write("old()\n")
                app.SCANS[0]["repoPath"] = stale_path

                with (
                    patch("pullwise_server.app.checkout.prepare_checkout", return_value=fresh_path) as prepare_checkout,
                    patch("pullwise_server.app.checkout.cleanup_scan_workspace") as cleanup_scan_workspace,
                ):
                    preview = app.preview_issue_fix_for_user(app.USERS["usr_1"], app.ISSUES[0])

        self.assertTrue(preview["valid"])
        self.assertIn("-old()", preview["diff"])
        prepare_checkout.assert_called_once()
        cleanup_scan_workspace.assert_called_once_with("usr_1", "sc_1")
    def test_preview_issue_fix_for_user_cleans_up_after_checkout_prepare_failure(self) -> None:
        app.ISSUES[0].update({
            "repo": "owner/repo",
            "scanId": "sc_1",
            "autoFix": True,
            "file": "src/auth.py",
            "badCode": [{"ln": 1, "code": "old()", "t": "del"}],
            "goodCode": [{"ln": 1, "code": "new()", "t": "add"}],
        })
        app.SCANS[0]["repoPath"] = None

        with (
            patch("pullwise_server.app.checkout.prepare_checkout", side_effect=RuntimeError("clone failed")),
            patch("pullwise_server.app.checkout.cleanup_scan_workspace") as cleanup_scan_workspace,
        ):
            with self.assertRaises(ValueError) as context:
                app.preview_issue_fix_for_user(app.USERS["usr_1"], app.ISSUES[0])

        self.assertIn("clone failed", str(context.exception))
        cleanup_scan_workspace.assert_called_once_with("usr_1", "sc_1")
    def test_preview_issue_fix_for_user_rejects_non_completed_scan(self) -> None:
        app.ISSUES[0].update({
            "repo": "owner/repo",
            "scanId": "sc_1",
            "autoFix": True,
            "file": "src/auth.py",
            "badCode": [{"ln": 1, "code": "old()", "t": "del"}],
            "goodCode": [{"ln": 1, "code": "new()", "t": "add"}],
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                repo_path = app.checkout.checkout_path_for("usr_1", "sc_1", "owner/repo")
                os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
                app.SCANS[0].update({
                    "status": "running",
                    "repoPath": repo_path,
                })

                with (
                    patch("pullwise_server.app.checkout.prepare_checkout", side_effect=AssertionError("prepare_checkout should not run")),
                    patch("pullwise_server.app.checkout.cleanup_scan_workspace", side_effect=AssertionError("cleanup should not run")),
                    patch("pullwise_server.app.fix_workflow.preview_issue_fix", side_effect=AssertionError("preview should not run")),
                ):
                    with self.assertRaises(ValueError) as context:
                        app.preview_issue_fix_for_user(app.USERS["usr_1"], app.ISSUES[0])

        self.assertIn("completed", str(context.exception))
    def test_preview_issue_fix_for_user_serializes_same_scan_checkout_previews(self) -> None:
        app.ISSUES[0].update({
            "repo": "owner/repo",
            "scanId": "sc_1",
            "autoFix": True,
            "file": "src/auth.py",
            "badCode": [{"ln": 1, "code": "old()", "t": "del"}],
            "goodCode": [{"ln": 1, "code": "new()", "t": "add"}],
        })
        app.SCANS[0].update({
            "status": "done",
            "repoPath": None,
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                repo_path = app.checkout.checkout_path_for("usr_1", "sc_1", "owner/repo")
                os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
                with open(os.path.join(repo_path, "src", "auth.py"), "w", encoding="utf-8") as handle:
                    handle.write("old()\n")

                active_prepares = 0
                max_active_prepares = 0
                prepare_calls = 0
                counter_lock = threading.Lock()
                first_prepare_entered = threading.Event()
                release_first_prepare = threading.Event()

                def prepare_checkout(_scan_id, _scan, _is_cancelled):
                    nonlocal active_prepares, max_active_prepares, prepare_calls
                    with counter_lock:
                        active_prepares += 1
                        prepare_calls += 1
                        max_active_prepares = max(max_active_prepares, active_prepares)
                        is_first_prepare = prepare_calls == 1
                    if is_first_prepare:
                        first_prepare_entered.set()
                        release_first_prepare.wait(1)
                    else:
                        time.sleep(0.01)
                    with counter_lock:
                        active_prepares -= 1
                    return repo_path

                previews = []
                errors = []

                def run_preview():
                    try:
                        previews.append(app.preview_issue_fix_for_user(app.USERS["usr_1"], app.ISSUES[0]))
                    except Exception as exc:
                        errors.append(exc)

                with (
                    patch("pullwise_server.app.checkout.prepare_checkout", side_effect=prepare_checkout),
                    patch("pullwise_server.app.checkout.cleanup_scan_workspace") as cleanup_scan_workspace,
                ):
                    first = threading.Thread(target=run_preview)
                    second = threading.Thread(target=run_preview)
                    first.start()
                    second.start()
                    self.assertTrue(first_prepare_entered.wait(1))
                    time.sleep(0.05)
                    release_first_prepare.set()
                    first.join()
                    second.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(previews), 2)
        self.assertTrue(all(preview["valid"] for preview in previews))
        self.assertEqual(prepare_calls, 2)
        self.assertEqual(cleanup_scan_workspace.call_count, 2)
        self.assertEqual(max_active_prepares, 1)
    def test_preview_issue_fix_for_user_rejects_scan_owned_by_another_user(self) -> None:
        app.ISSUES[0]["scanId"] = "sc_2"
        app.SCANS.append({
            "id": "sc_2",
            "userId": "usr_2",
            "status": "done",
            "repo": "owner/repo",
            "repoPath": None,
        })

        with self.assertRaises(ValueError) as context:
            app.preview_issue_fix_for_user(app.USERS["usr_1"], app.ISSUES[0])

        self.assertIn("signed-in user", str(context.exception))
    def test_preview_issue_fix_for_user_rejects_repo_path_outside_scan_workspace(self) -> None:
        app.ISSUES[0]["scanId"] = "sc_1"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": os.path.join(tmpdir, "checkouts")}, clear=False):
                outside_path = os.path.join(tmpdir, "outside", "repo")
                app.SCANS[0]["repoPath"] = outside_path

                with self.assertRaises(ValueError) as context:
                    app.preview_issue_fix_for_user(app.USERS["usr_1"], app.ISSUES[0])

        self.assertIn("outside the scan workspace", str(context.exception))
    def test_issue_status_update_rejects_unknown_status(self) -> None:
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        handler = RouteHarness("/issues/iss_1/status", {"status": "archived"}, cookie="pw_session=ses_1")

        app.PullwiseHandler.route(handler, "PATCH")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(app.ISSUES[0]["status"], "open")
    def test_issue_status_update_normalizes_status_text(self) -> None:
        handler = RouteHarness("/issues/iss_1/status", {"status": " Fixed "}, cookie=self.signed_in())

        app.PullwiseHandler.route(handler, "PATCH")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(app.ISSUES[0]["status"], "fixed")
    def test_issue_status_update_rejects_non_object_body(self) -> None:
        handler = RouteHarness("/issues/iss_1/status", ["fixed"], cookie=self.signed_in())

        app.PullwiseHandler.route(handler, "PATCH")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(app.ISSUES[0]["status"], "open")
    def test_settings_update_rejects_non_object_body(self) -> None:
        handler = RouteHarness("/settings", ["not", "an", "object"], cookie=self.signed_in())

        app.PullwiseHandler.route(handler, "PATCH")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload["message"], "Request body must be a JSON object.")
    def test_settings_update_persists_review_output_language_for_next_read(self) -> None:
        update = RouteHarness("/settings", {"review": {"outputLanguage": "ja"}}, cookie=self.signed_in())

        app.PullwiseHandler.route(update, "PATCH")

        self.assertEqual(update.status, HTTPStatus.OK)
        self.assertEqual(update.payload["review"]["outputLanguage"], "ja")

        read = RouteHarness("/settings", cookie=self.signed_in())

        app.PullwiseHandler.route(read, "GET")

        self.assertEqual(read.status, HTTPStatus.OK)
        self.assertEqual(read.payload["review"]["outputLanguage"], "ja")
    def test_settings_read_prefers_database_user_scope_over_stale_process_cache(self) -> None:
        app.SETTINGS = {
            "usr_1": {
                "profile": {"name": "Dev", "email": "dev@example.com"},
                "review": {"outputLanguage": "en"},
            }
        }
        app.db.save_state_item(
            "settings",
            {
                "usr_1": {
                    "profile": {"name": "Dev", "email": "dev@example.com"},
                    "review": {"outputLanguage": "ja"},
                },
                "usr_2": {
                    "profile": {"name": "Other", "email": "other@example.com"},
                    "review": {"outputLanguage": "fr"},
                },
            },
        )
        handler = RouteHarness("/settings", cookie=self.signed_in())

        app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.payload["review"]["outputLanguage"], "ja")
        self.assertEqual(app.SETTINGS["usr_1"]["review"]["outputLanguage"], "ja")
        self.assertEqual(app.SETTINGS["usr_2"]["review"]["outputLanguage"], "fr")
    def test_settings_payload_sanitizes_profile_fields(self) -> None:
        app.SETTINGS["usr_1"] = {
            "profile": {
                "name": "Mallory\r\nX-Injected: bad",
                "email": {"value": "mallory@example.com"},
            },
            "review": {
                "outputLanguage": {"value": "fr"},
            },
            "extra": {"unsafe": True},
        }
        handler = RouteHarness("/settings", cookie=self.signed_in())

        app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(
            handler.payload,
            {
                "profile": {"name": "Dev", "email": "dev@example.com"},
                "review": {"outputLanguage": "en"},
            },
        )


__all__ = ["SecurityContractsPart01Test"]
