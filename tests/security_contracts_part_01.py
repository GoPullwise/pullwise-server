from __future__ import annotations

try:
    import security_contracts_base as _security_contracts_base
except ModuleNotFoundError:  # pragma: no cover - package-style unittest invocation
    from . import security_contracts_base as _security_contracts_base

globals().update(
    {name: getattr(_security_contracts_base, name) for name in dir(_security_contracts_base) if not name.startswith("_")}
)


class SecurityContractsPart01Test(SecurityContractsBase):





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
            "/auth/github/authorize?response=redirect&redirectTo=http%3A%2F%2Flocalhost%3A5173%2Fworkers",
            headers={"Host": "localhost:8080"},
        )

        with patch.dict(
            os.environ,
            {
                "PULLWISE_APP_URL": "http://localhost:5173",
                "PULLWISE_ALLOWED_ORIGINS": "http://localhost:5173,http://127.0.0.1:5173",
                "PULLWISE_API_BASE_URL": "http://localhost:8080",
                "PULLWISE_ENABLE_LOCAL_GITHUB_MOCKS": "true",
            },
            clear=True,
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.FOUND)
        self.assertEqual(
            handler.location,
            "http://localhost:8080/auth/github/callback?redirectTo=http%3A%2F%2Flocalhost%3A5173%2Fworkers",
        )

    def test_local_github_mock_callback_rejects_public_host_even_when_flag_is_set(self) -> None:
        handler = RouteHarness(
            "/auth/github/callback?redirectTo=https%3A%2F%2Fapp.pullwise.dev%2Fdashboard",
            headers={"Host": "api.pull-wise.com"},
        )

        with patch.dict(
            os.environ,
            {
                "PULLWISE_APP_URL": "https://app.pullwise.dev",
                "PULLWISE_ALLOWED_ORIGINS": "https://app.pullwise.dev",
                "PULLWISE_API_BASE_URL": "https://api.pull-wise.com",
                "PULLWISE_ENABLE_LOCAL_GITHUB_MOCKS": "true",
            },
            clear=True,
        ):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.NOT_IMPLEMENTED)
        self.assertEqual(app.SESSIONS, {})
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
        self.assertEqual(user["email"], "")
        self.assertIsNone(user["avatarUrl"])
        self.assertIsNone(user["githubHtmlUrl"])
    def test_real_github_user_does_not_store_github_noreply_email(self) -> None:
        app.USERS = {}

        user = app.get_or_create_real_github_user(
            {
                "id": 123,
                "login": "OctoCat",
                "primaryEmail": "OctoCat@users.noreply.github.com",
                "email": "123+OctoCat@users.noreply.github.com",
                "name": "Octo Cat",
            },
            {"access_token": "gho_user", "token_type": "bearer", "scope": "read:user"},
        )

        self.assertEqual(user["email"], "")



__all__ = ["SecurityContractsPart01Test"]
