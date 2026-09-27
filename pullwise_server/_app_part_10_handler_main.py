from __future__ import annotations

import json

from . import product_api

# Loaded by app.py; keep definitions in that module's globals for compatibility.

from . import _app_part_09_billing_cookie_security as _previous_app_part
from ._app_imports import import_compat_globals as _import_compat_globals

_import_compat_globals(vars(_previous_app_part), globals())
del _import_compat_globals, _previous_app_part

def api_key_response_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "Vary": "Cookie, Authorization, X-Pullwise-Api-Key",
    }


def retired_product_route(segments: list[str]) -> bool:
    """Keep the superseded Reviewer/Worker service off the shared HTTP surface."""
    if not segments:
        return False
    if segments[0] in {"scans", "issues", "worker", "workers", "admin"}:
        return True
    if segments in (["dashboard", "overview"], ["status", "system"],
                    ["install-worker.sh"], ["docs", "subscription-plans"],
                    ["docs", "server-config"], ["api-docs"],
                    ["api", "docs"]):
        return True
    if segments[0] == "settings":
        return True
    if len(segments) == 3 and segments[0] == "repositories" and segments[2] == "branches":
        return True
    api_segments = segments[2:] if segments[:2] == ["api", "v1"] else (
        segments[1:] if segments[0] == "v1" else None
    )
    if api_segments is None:
        return False
    if api_segments and api_segments[0] in {"agent-first", "review-runs", "workers"}:
        return True
    return (len(api_segments) >= 3 and api_segments[0] == "repositories"
            and api_segments[2] in {"scans", "quota"})


class PullwiseHandler(BaseHTTPRequestHandler):
    server_version = "PullwiseDevAPI/0.1"

    def log_message(self, fmt: str, *args) -> None:
        access_logger.info("%s - %s", self.address_string(), fmt % args)

    def apply_rate_limit(self, method: str, path: str, segments: list[str] | None = None) -> bool:
        if not rate_limit_enabled() or rate_limit_exempt_path(method, path):
            self._rate_limit_headers = {}
            return False
        segments = segments if segments is not None else [unquote(part) for part in path.split("/") if part]
        if external_api_segments(segments) is None:
            self._rate_limit_headers = {}
            return False
        if self.current_session():
            self._rate_limit_headers = {}
            return False
        limit = rate_limit_requests()
        if limit <= 0:
            self._rate_limit_headers = {}
            return False

        try:
            rate = db.record_rate_limit_hit(
                self.rate_limit_subject(),
                limit=limit,
                window_seconds=rate_limit_window_seconds(),
            )
        except Exception:
            logger.exception("Failed to apply API rate limit.")
            self._rate_limit_headers = {}
            self.json(
                {"message": "API rate limit is temporarily unavailable. Try again later."},
                HTTPStatus.SERVICE_UNAVAILABLE,
                headers={"Cache-Control": "no-store"},
            )
            return True
        headers = {
            "X-RateLimit-Limit": str(rate["limit"]),
            "X-RateLimit-Remaining": str(rate["remaining"]),
            "X-RateLimit-Reset": str(rate["resetAt"]),
        }
        self._rate_limit_headers = headers
        if rate["allowed"]:
            return False

        retry_after = str(rate["retryAfter"])
        self.json(
            {"message": "API rate limit exceeded. Try again later."},
            HTTPStatus.TOO_MANY_REQUESTS,
            headers={**headers, "Retry-After": retry_after},
        )
        return True

    def rate_limit_subject(self) -> str:
        session = self.current_session()
        if session:
            return f"user:{session['userId']}"
        return f"ip:{self.client_ip_address()}"

    def client_ip_address(self) -> str:
        if proxy_headers_trusted(self):
            forwarded = forwarded_client_ip(self)
            if forwarded:
                return forwarded
        return direct_client_ip(self)

    def do_OPTIONS(self) -> None:
        try:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_cors_headers()
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,PATCH,DELETE,OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type,Authorization,X-Pullwise-Api-Key,If-Match,Idempotency-Key",
            )
            self.end_headers()
        except _CLIENT_DISCONNECT_EXCEPTIONS:
            logger.debug("Client disconnected while handling OPTIONS %s", self.path)

    def do_GET(self) -> None:
        self.route("GET")

    def do_POST(self) -> None:
        self.route("POST")

    def do_PUT(self) -> None:
        self.route("PUT")

    def do_PATCH(self) -> None:
        self.route("PATCH")

    def do_DELETE(self) -> None:
        self.route("DELETE")

    def route(self, method: str) -> None:
        ensure_state_loaded()
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        segments = [unquote(part) for part in path.split("/") if part]
        self._rate_limit_headers = {}

        try:
            try:
                if retired_product_route(segments):
                    return self.error(HTTPStatus.NOT_FOUND, "Route not found")
                if self.apply_rate_limit(method, path, segments):
                    return
                self.enforce_body_size_limit(method, path, segments)
                if cookie_state_change_needs_origin_check(method, path, segments, self) and not request_origin_is_trusted(self):
                    return self.error(HTTPStatus.FORBIDDEN, "State-changing requests must come from a trusted origin.")
                if method == "GET":
                    return self.handle_get(path, params, segments)
                if method == "POST":
                    return self.handle_post(path, params, segments)
                if method == "PUT":
                    return self.handle_put(segments)
                if method == "PATCH":
                    return self.handle_patch(segments)
                if method == "DELETE":
                    return self.handle_delete(segments)
                return self.error(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed")
            except ClientDisconnected:
                raise
            except RequestBodyTooLarge as exc:
                return self.error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(exc))
            except ResourceNotFound as exc:
                return self.error(HTTPStatus.NOT_FOUND, str(exc))
            except ValueError as exc:
                return self.error(HTTPStatus.BAD_REQUEST, str(exc))
            except billing.BillingProviderResponseError as exc:
                return self.error(HTTPStatus.BAD_GATEWAY, str(exc))
            except billing.BillingConfigurationError as exc:
                return self.error(HTTPStatus.NOT_IMPLEMENTED, str(exc))
            except Exception as exc:
                logger.exception("Unhandled server error while handling %s %s", method, self.path)
                return self.error(HTTPStatus.INTERNAL_SERVER_ERROR, "Server error.")
        except ClientDisconnected:
            logger.debug("Client disconnected while handling %s %s", method, self.path)
            return
        finally:
            persist_state()

    def handle_get(self, path: str, params: dict, segments: list[str]) -> None:
        if path == "/health":
            return self.json({
                "ok": True,
                "service": "pullwise-server",
                "time": now(),
                "mode": env("PULLWISE_MODE", "local"),
                "database": {"type": "sqlite", "configured": True},
                **readiness_payload(),
            })
        if path == "/pricing":
            session = self.current_session()
            user = USERS.get(session["userId"]) if session else None
            return self.json(pricing_payload(user))
        api_segments = external_api_segments(segments)
        if api_segments is not None:
            return self.handle_external_api_get(api_segments, params)
        if path == "/auth/session":
            return self.json(session_payload(self.current_session()))
        if path == "/api-keys":
            return self.handle_api_keys_get(params)
        if path == "/auth/github/authorize":
            return self.handle_github_authorize(params)
        if path == "/auth/github/callback":
            return self.handle_github_callback(params)
        if path == "/integrations":
            return self.json(self.integrations_payload())
        if path == "/integrations/github/authorize":
            return self.handle_github_repository_authorize(params)
        if path == "/integrations/github/callback":
            return self.handle_github_repository_callback(params)
        if path == "/integrations/github/install/start":
            return self.handle_github_install_start(params)
        if path == "/integrations/github/manage/start":
            return self.handle_github_manage_start(params)
        if path == "/dev/magic-links" or path == "/auth/email/callback":
            return self.error(HTTPStatus.NOT_FOUND, "Route not found")
        if path == "/repositories":
            return self.json(self.repositories_payload(params=params))
        if path == "/billing/plan":
            session = self.current_session()
            user = USERS.get(session["userId"]) if session else None
            return self.json(pricing_payload(user))
        if path == "/billing":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing billing.")
            return self.json(billing_page_payload(USERS[session["userId"]]))
        # Static file serving + SPA fallback for client-side routing
        root = web_root()
        if os.path.isdir(root):
            # Try to serve the exact file
            rel = path.lstrip("/")
            root_path = os.path.abspath(root)
            candidate = os.path.abspath(os.path.join(root_path, rel))
            # Prevent path traversal
            try:
                inside_root = os.path.commonpath([root_path, candidate]) == root_path
            except ValueError:
                inside_root = False
            if inside_root and os.path.isfile(candidate):
                return self.serve_static_file(candidate)
            # SPA fallback: serve index.html for any other GET
            return self.serve_spa()
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_post(self, path: str, params: dict, segments: list[str]) -> None:
        if path == "/webhooks/github":
            from .github_webhooks import handle_github_webhook
            return handle_github_webhook(self)
        if path == "/webhooks/creem":
            return self.handle_creem_webhook()
        body = self.read_json()
        api_segments = external_api_segments(segments)
        if api_segments is not None:
            return self.handle_external_api_post(api_segments, body)
        if path == "/auth/sign-out":
            self.clear_current_session()
            return self.json({"ok": True}, headers={"Set-Cookie": clear_cookie_header()})
        if path == "/api-keys":
            return self.handle_api_keys_post(body)
        if (
            len(segments) == 5
            and segments[0] == "integrations"
            and segments[1] == "github"
            and segments[2] == "installations"
            and segments[4] == "manage-sessions"
        ):
            return self.handle_github_installation_manage_session(segments[3], body)
        if path == "/repositories/sync":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before syncing repositories.")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            installation_id = clean_github_access_text(body.get("installationId"), allow_int=True)
            github_identity_id = clean_github_access_text(body.get("githubIdentityId"))
            user = USERS.get(session["userId"])
            if not user:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before syncing repositories.")
            github_access = user.get("githubRepositoryAccess")
            if installation_id or github_identity_id:
                if not installation_id:
                    return self.error(HTTPStatus.BAD_REQUEST, "installationId is required for scoped repository sync.")
                sync_github_repository_installation_scope(
                    user,
                    installation_id,
                    github_identity_id=github_identity_id,
                )
                payload = self.repositories_payload(refresh=False)
            elif (
                isinstance(github_access, dict)
                and github_access.get("mode") == "local"
                and local_github_mocks_enabled(self)
            ):
                with STATE_LOCK:
                    current_user = USERS.get(session["userId"])
                    current_access = current_user.get("githubRepositoryAccess") if current_user else None
                    if not isinstance(current_access, dict) or current_access.get("mode") != "local":
                        return self.error(HTTPStatus.CONFLICT, "Local repository authorization changed during sync.")
                    current_access["repositoriesNeedSync"] = False
                    current_access["syncedAt"] = now()
                    mark_state_dirty()
                payload = self.repositories_payload(refresh=False)
            else:
                payload = self.repositories_payload(
                    refresh=repository_sync_should_refresh(
                        user,
                        user.get("githubRepositoryAccess"),
                        body,
                    )
                )
            payload.update({"ok": True, "syncedAt": now()})
            return self.json(payload)
        if len(segments) == 2 and segments[0] == "integrations":
            return self.error(HTTPStatus.NOT_IMPLEMENTED, f"{segments[1]} integration writes are not implemented on this backend.")
        if path == "/billing/checkout-sessions":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before starting checkout.")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            user = USERS[session["userId"]]
            if effective_billing_plan(user) in billing.PAID_PLAN_IDS:
                return self.error(HTTPStatus.CONFLICT, "An active paid subscription already exists. Change or cancel billing from the Billing page.")
            success_url = safe_redirect_to(body.get("successUrl"), "settings")
            plan = str(body.get("plan") or "pro")
            interval = str(body.get("interval") or "month")
            checkout = billing.create_checkout_session(
                user,
                success_url=success_url,
                cancel_url=safe_redirect_to(body.get("cancelUrl"), "settings"),
                plan=plan,
                interval=interval,
            )
            checkout = safe_billing_redirect_response(checkout, "Checkout", require_url=True)
            if checkout.get("customerId"):
                current_billing = user.get("billing") or {}
                user["billing"] = {
                    **current_billing,
                    "provider": checkout.get("provider") or current_billing.get("provider"),
                    "customerId": checkout.get("customerId"),
                }
            user["billingCheckout"] = {
                "provider": checkout.get("provider"),
                "id": checkout.get("id"),
                "requestId": checkout.get("requestId"),
                "plan": checkout.get("plan"),
                "interval": checkout.get("interval"),
                "createdAt": now(),
            }
            mark_state_dirty()
            return self.json(checkout)
        if path == "/billing/change-interval":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before changing your subscription.")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            user = USERS[session["userId"]]
            result = billing.change_subscription_interval(
                user,
                interval=str(body.get("interval") or "year"),
                plan=str(body.get("plan") or ""),
                return_url=safe_redirect_to(body.get("returnUrl"), "billing"),
            )
            result = safe_billing_redirect_response(result, "subscription change")
            if result.get("alreadyActive"):
                return self.json(result)
            if result.get("provider") == "creem":
                current_billing = user.get("billing") or {}
                next_status = result.get("status") or current_billing.get("status") or "active"
                restored_subscription = billing.normalize_subscription_status(next_status) in {"active", "trialing"}
                user["billing"] = {
                    **current_billing,
                    "provider": "creem",
                    "subscriptionId": result.get("subscriptionId") or current_billing.get("subscriptionId"),
                    "status": next_status,
                    "plan": billing.normalize_plan(result.get("plan") or current_billing.get("plan") or "pro"),
                    "interval": billing.normalize_interval(result.get("interval") or current_billing.get("interval")),
                    "cancelAtPeriodEnd": result.get("cancelAtPeriodEnd")
                    if isinstance(result.get("cancelAtPeriodEnd"), bool)
                    else (False if restored_subscription else current_billing.get("cancelAtPeriodEnd")),
                    "canceledAt": result.get("canceledAt")
                    if "canceledAt" in result
                    else (None if restored_subscription else current_billing.get("canceledAt")),
                    "currentPeriodStart": result.get("currentPeriodStart") or current_billing.get("currentPeriodStart"),
                    "currentPeriodEnd": result.get("currentPeriodEnd") or current_billing.get("currentPeriodEnd"),
                }
                mark_state_dirty()
            return self.json(result)
        if path == "/billing/cancel-subscription":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before canceling your subscription.")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            user = USERS[session["userId"]]
            result = billing.cancel_subscription(
                user,
                mode=str(body.get("mode") or "scheduled"),
                return_url=safe_redirect_to(body.get("returnUrl"), "billing"),
            )
            result = safe_billing_redirect_response(result, "subscription cancellation")
            if result.get("provider") == "creem":
                current_billing = user.get("billing") or {}
                user["billing"] = {
                    **current_billing,
                    "provider": "creem",
                    "subscriptionId": result.get("subscriptionId") or current_billing.get("subscriptionId"),
                    "status": result.get("status") or current_billing.get("status") or "canceling",
                    "plan": billing.normalize_plan(result.get("plan") or current_billing.get("plan") or "pro"),
                    "interval": billing.normalize_interval(result.get("interval") or current_billing.get("interval")),
                    "cancelAtPeriodEnd": result.get("cancelAtPeriodEnd")
                    if isinstance(result.get("cancelAtPeriodEnd"), bool)
                    else current_billing.get("cancelAtPeriodEnd"),
                    "canceledAt": result.get("canceledAt") or current_billing.get("canceledAt"),
                    "currentPeriodStart": result.get("currentPeriodStart") or current_billing.get("currentPeriodStart"),
                    "currentPeriodEnd": result.get("currentPeriodEnd") or current_billing.get("currentPeriodEnd"),
                }
                mark_state_dirty()
            return self.json(result)
        if path == "/billing/resume-subscription":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before resuming your subscription.")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            user = USERS[session["userId"]]
            result = billing.resume_subscription(
                user,
                return_url=safe_redirect_to(body.get("returnUrl"), "billing"),
            )
            result = safe_billing_redirect_response(result, "subscription resume")
            if result.get("provider") == "creem":
                current_billing = user.get("billing") or {}
                next_status = result.get("status") or current_billing.get("status") or "active"
                restored_subscription = billing.normalize_subscription_status(next_status) in {"active", "trialing"}
                user["billing"] = {
                    **current_billing,
                    "provider": "creem",
                    "subscriptionId": result.get("subscriptionId") or current_billing.get("subscriptionId"),
                    "status": next_status,
                    "plan": billing.normalize_plan(result.get("plan") or current_billing.get("plan") or "pro"),
                    "interval": billing.normalize_interval(result.get("interval") or current_billing.get("interval")),
                    "cancelAtPeriodEnd": result.get("cancelAtPeriodEnd")
                    if isinstance(result.get("cancelAtPeriodEnd"), bool)
                    else (False if restored_subscription else current_billing.get("cancelAtPeriodEnd")),
                    "canceledAt": result.get("canceledAt")
                    if "canceledAt" in result
                    else (None if restored_subscription else current_billing.get("canceledAt")),
                    "currentPeriodStart": result.get("currentPeriodStart") or current_billing.get("currentPeriodStart"),
                    "currentPeriodEnd": result.get("currentPeriodEnd") or current_billing.get("currentPeriodEnd"),
                }
                mark_state_dirty()
            return self.json(result)
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_patch(self, segments: list[str]) -> None:
        body = self.read_json()
        api_segments = external_api_segments(segments)
        if api_segments is not None and product_api.handle_patch(self, api_segments, body, USERS):
            return
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_put(self, segments: list[str]) -> None:
        body = self.read_json()
        api_segments = external_api_segments(segments)
        if api_segments is not None and product_api.handle_put(self, api_segments, body, USERS):
            return
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_delete(self, segments: list[str]) -> None:
        api_segments = external_api_segments(segments)
        if api_segments is not None and product_api.handle_delete(self, api_segments, USERS):
            return
        if len(segments) == 2 and segments[0] == "api-keys":
            return self.handle_api_key_delete(segments[1])
        if len(segments) == 2 and segments[0] == "integrations":
            session = self.current_session()
            if segments[1] != "github":
                return self.error(HTTPStatus.NOT_IMPLEMENTED, f"{segments[1]} integration disconnect is not implemented on this backend.")
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before disconnecting GitHub.")
            with STATE_LOCK:
                USERS[session["userId"]]["githubRepositoryAccess"] = None
                USERS[session["userId"]].pop("githubRepositoryAccessPending", None)
                mark_state_dirty()
            return self.json({"ok": True, "provider": "github", "connected": False})
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_github_authorize(self, params: dict) -> None:
        redirect_to = safe_redirect_to(params.get("redirectTo"), "dashboard")
        redirect_response = params.get("response") == "redirect"
        if not github_auth.oauth_configured():
            if not local_github_mocks_enabled(self):
                return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub OAuth is not configured. Set PULLWISE_GITHUB_CLIENT_ID and PULLWISE_GITHUB_CLIENT_SECRET.")
            callback = f"{api_base_url(self)}/auth/github/callback?{urlencode({'redirectTo': redirect_to})}"
            if redirect_response:
                return self.redirect(callback)
            return self.json({"url": callback, "mode": "local"})

        verifier = github_auth.make_code_verifier()
        state = remember_github_state("login", redirect_to, codeVerifier=verifier)
        callback_url = f"{api_base_url(self)}/auth/github/callback"
        authorize_url = github_auth.build_oauth_authorize_url(
            callback_url,
            state,
            verifier,
        )
        if redirect_response:
            return self.redirect(authorize_url)
        return self.json({"url": authorize_url, "mode": "github"})

    def handle_github_callback(self, params: dict) -> None:
        if not github_auth.oauth_configured():
            if not local_github_mocks_enabled(self):
                return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub OAuth is not configured.")
            user = get_or_create_github_user()
            session = create_session(user)
            return self.redirect(safe_redirect_to(params.get("redirectTo"), "dashboard"), cookie_header(session["id"]))

        state = params.get("state") or ""
        record = pop_any_github_state(state)
        if record.get("kind") == "manage_installation":
            return self.handle_github_manage_callback(params, record, state)
        if record.get("kind") == "install_identity":
            return self.handle_github_install_identity_callback(params, record, state)
        if record.get("kind") != "login":
            raise ValueError("GitHub authorization state is invalid or expired.")
        redirect_to = str(record["redirectTo"])
        if params.get("error"):
            return self.redirect(redirect_with_params(redirect_to, {"github_error": params.get("error_description") or params["error"]}))
        if not params.get("code"):
            return self.redirect(redirect_with_params(redirect_to, {"github_error": "missing_oauth_code"}))

        token_payload = github_auth.exchange_oauth_code(
            params["code"],
            f"{api_base_url(self)}/auth/github/callback",
            str(record.get("codeVerifier") or ""),
            state,
        )
        profile = github_auth.fetch_user_profile(token_payload["access_token"])
        user = get_or_create_real_github_user(profile, token_payload)
        session = create_session(user)
        return self.redirect(redirect_to, cookie_header(session["id"]))

    def handle_github_installation_manage_session(self, installation_id: str, body: dict) -> None:
        session = self.current_session()
        if not session:
            return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before managing GitHub installations.")
        if not isinstance(body, dict):
            return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        if not github_auth.oauth_configured():
            return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub OAuth is not configured. Set PULLWISE_GITHUB_CLIENT_ID and PULLWISE_GITHUB_CLIENT_SECRET.")
        if not github_auth.app_install_configured():
            return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub App installation is not configured. Set PULLWISE_GITHUB_APP_SLUG or PULLWISE_GITHUB_APP_INSTALL_URL.")

        user = USERS.get(session["userId"])
        github_access = user.get("githubRepositoryAccess") if user else None
        if not github_repository_access_authorized_for_user(user, github_access) or not github_repository_access_connected(github_access):
            return self.error(HTTPStatus.FORBIDDEN, "Connect GitHub repositories before managing an installation.")

        clean_installation_id = clean_github_access_text(installation_id, allow_int=True)
        if not clean_installation_id:
            return self.error(HTTPStatus.BAD_REQUEST, "A GitHub App installation id is required.")
        installation = installation_summary_by_id(github_access, clean_installation_id)
        if not installation:
            return self.error(HTTPStatus.NOT_FOUND, "GitHub App installation is not connected to this Pullwise account.")

        identity_id = clean_github_access_text(body.get("githubIdentityId"))
        if identity_id and not github_identity_by_id(user, identity_id):
            return self.error(HTTPStatus.BAD_REQUEST, "GitHub identity is not linked to this Pullwise account.")

        redirect_to = safe_redirect_to(body.get("returnUrl") or body.get("redirectTo"), "repos")
        state = remember_github_installation_manage_state(
            user,
            installation,
            redirect_to,
            expected_github_identity_id=identity_id,
        )
        url = f"{api_base_url(self)}/integrations/github/manage/start?{urlencode({'state': state})}"
        return self.json({
            "mode": "github-installation-manage",
            "url": url,
            "installationId": clean_installation_id,
        })

    def handle_github_install_start(self, params: dict) -> None:
        state = params.get("state") or ""
        record = peek_github_state("install_identity", state)
        if not github_auth.oauth_configured():
            return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub OAuth is not configured. Set PULLWISE_GITHUB_CLIENT_ID and PULLWISE_GITHUB_CLIENT_SECRET.")
        verifier = github_auth.make_code_verifier()
        record["codeVerifier"] = verifier
        record["oauthStartedAt"] = now()
        mark_state_dirty()
        authorize_url = github_auth.build_oauth_authorize_url(
            f"{api_base_url(self)}/auth/github/callback",
            state,
            verifier,
            prompt="select_account",
        )
        return self.redirect(authorize_url)

    def handle_github_install_identity_callback(self, params: dict, record: dict, state: str) -> None:
        redirect_to = str(record["redirectTo"])
        user = USERS.get(str(record.get("userId") or ""))
        if not user:
            raise ValueError("The GitHub installation identity session belongs to a user session that no longer exists.")
        if params.get("error"):
            clear_github_repository_authorization_pending(user, state)
            return self.redirect(redirect_with_params(redirect_to, {"github_error": params.get("error_description") or params["error"]}))
        if not params.get("code"):
            clear_github_repository_authorization_pending(user, state)
            return self.redirect(redirect_with_params(redirect_to, {"github_error": "missing_oauth_code"}))

        token_payload = github_auth.exchange_oauth_code(
            params["code"],
            f"{api_base_url(self)}/auth/github/callback",
            str(record.get("codeVerifier") or ""),
            state,
        )
        profile = github_auth.fetch_user_profile(token_payload["access_token"])
        identity = upsert_github_identity(user, profile, token_payload)
        install_state = remember_github_repository_authorization(
            user,
            redirect_to,
            str(record.get("requestedScope") or "selected"),
            manage=record.get("manage") is True,
            selected_github_identity_id=clean_github_access_text(identity.get("id")),
        )
        return self.redirect(github_auth.build_app_install_url(install_state))

    def handle_github_manage_start(self, params: dict) -> None:
        state = params.get("state") or ""
        record = peek_github_state("manage_installation", state)
        if not github_auth.oauth_configured():
            return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub OAuth is not configured. Set PULLWISE_GITHUB_CLIENT_ID and PULLWISE_GITHUB_CLIENT_SECRET.")
        verifier = github_auth.make_code_verifier()
        record["codeVerifier"] = verifier
        record["oauthStartedAt"] = now()
        mark_state_dirty()
        authorize_url = github_auth.build_oauth_authorize_url(
            f"{api_base_url(self)}/auth/github/callback",
            state,
            verifier,
            prompt="select_account",
        )
        return self.redirect(authorize_url)

    def handle_github_manage_callback(self, params: dict, record: dict, state: str) -> None:
        redirect_to = str(record["redirectTo"])
        if params.get("error"):
            return self.redirect(redirect_with_params(redirect_to, {"github_error": params.get("error_description") or params["error"]}))
        if not params.get("code"):
            return self.redirect(redirect_with_params(redirect_to, {"github_error": "missing_oauth_code"}))

        user = USERS.get(str(record.get("userId") or ""))
        if not user:
            raise ValueError("The GitHub manage session belongs to a user session that no longer exists.")
        token_payload = github_auth.exchange_oauth_code(
            params["code"],
            f"{api_base_url(self)}/auth/github/callback",
            str(record.get("codeVerifier") or ""),
            state,
        )
        profile = github_auth.fetch_user_profile(token_payload["access_token"])
        identity = upsert_github_identity(user, profile, token_payload)
        expected_installation_id = clean_github_access_text(record.get("expectedInstallationId"), allow_int=True)
        if not expected_installation_id:
            return self.redirect(redirect_with_params(redirect_to, {"github_error": "github_installation_not_visible"}))

        if self.github_manage_identity_mismatch(identity, record):
            upsert_github_identity_installation_access(
                user,
                identity,
                expected_installation_id,
                can_access=False,
                last_error_code="github_account_mismatch",
            )
            return self.redirect(self.github_manage_error_redirect(redirect_to, "github_account_mismatch", identity, record))

        try:
            installations = github_auth.list_current_app_installations_for_user(identity.get("accessToken"))
        except github_auth.GitHubError:
            identity["status"] = "needs_reauth"
            upsert_github_identity_installation_access(
                user,
                identity,
                expected_installation_id,
                can_access=False,
                last_error_code="github_identity_reauth_required",
            )
            return self.redirect(self.github_manage_error_redirect(redirect_to, "github_identity_reauth_required", identity, record))

        installation = next(
            (
                item
                for item in installations
                if str(item.get("id") or "") == str(expected_installation_id)
            ),
            None,
        )
        if not installation:
            upsert_github_identity_installation_access(
                user,
                identity,
                expected_installation_id,
                can_access=False,
                last_error_code="github_installation_not_visible",
            )
            return self.redirect(self.github_manage_error_redirect(redirect_to, "github_installation_not_visible", identity, record))
        if installation.get("suspended_at"):
            upsert_github_identity_installation_access(
                user,
                identity,
                expected_installation_id,
                can_access=False,
                last_error_code="github_installation_deleted",
            )
            return self.redirect(self.github_manage_error_redirect(redirect_to, "github_installation_deleted", identity, record))

        html_url = trusted_github_web_url(installation.get("html_url") or record.get("expectedInstallationHtmlUrl"))
        if not html_url:
            upsert_github_identity_installation_access(
                user,
                identity,
                expected_installation_id,
                can_access=False,
                last_error_code="github_installation_not_visible",
            )
            return self.redirect(self.github_manage_error_redirect(redirect_to, "github_installation_not_visible", identity, record))

        upsert_github_identity_installation_access(
            user,
            identity,
            expected_installation_id,
            can_access=True,
        )
        return self.redirect(
            redirect_with_params(redirect_to, {"github_manage_continue_url": html_url})
        )

    def github_manage_identity_mismatch(self, identity: dict, record: dict) -> bool:
        expected_identity_id = clean_github_access_text(record.get("expectedGithubIdentityId"))
        if expected_identity_id and identity.get("id") != expected_identity_id:
            return True
        expected_target_type = str(record.get("expectedInstallationTargetType") or "").casefold()
        expected_account = str(record.get("expectedAccountLogin") or "").casefold()
        selected_login = str(identity.get("githubLogin") or identity.get("login") or "").casefold()
        return expected_target_type == "user" and expected_account and selected_login and selected_login != expected_account

    def github_manage_error_redirect(self, redirect_to: str, code: str, identity: dict, record: dict) -> str:
        return redirect_with_params(
            redirect_to,
            {
                "github_error": code,
                "github_login": clean_github_access_text(identity.get("githubLogin") or identity.get("login")) or "",
                "installation_account": clean_github_access_text(record.get("expectedAccountLogin")) or "",
            },
        )

    def handle_github_repository_authorize(self, params: dict) -> None:
        scope = params.get("scope") if params.get("scope") in {"all", "selected"} else "all"
        manage = str(params.get("manage") or "").lower() in {"1", "true", "yes", "on"}
        add_installation = str(params.get("add") or "").lower() in {"1", "true", "yes", "on"}
        redirect_to = safe_redirect_to(params.get("redirectTo"), "repos")
        if not github_auth.app_install_configured():
            if not local_github_mocks_enabled(self):
                return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub App installation is not configured. Set PULLWISE_GITHUB_APP_SLUG or PULLWISE_GITHUB_APP_INSTALL_URL.")
            callback = f"{api_base_url(self)}/integrations/github/callback?{urlencode({'scope': scope, 'redirectTo': redirect_to})}"
            return self.json({"url": callback, "mode": "local"})

        session = self.current_session()
        if not session:
            return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before authorizing GitHub repositories.")
        user = USERS.get(session["userId"])
        if not has_github_repository_authorization_identity(user):
            return self.error(HTTPStatus.UNAUTHORIZED, "Sign in with GitHub before authorizing repositories.")
        if github_auth.app_visibility_check_enabled():
            if not github_auth.app_slug():
                return self.error(
                    HTTPStatus.NOT_IMPLEMENTED,
                    "PULLWISE_GITHUB_APP_SLUG is required for user repository installs so Pullwise can verify the GitHub App is public.",
                )
            public_installable = github_auth.app_slug_publicly_installable()
            if public_installable is False:
                return self.error(
                    HTTPStatus.CONFLICT,
                    (
                        f"GitHub App '{github_auth.app_slug()}' is private or not publicly visible. "
                        "Make the GitHub App public before connecting repositories from user accounts, "
                        "and keep PULLWISE_GITHUB_APP_VISIBILITY_CHECK enabled for user repository installs."
                    ),
                )
            if public_installable is None:
                return self.error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    (
                        f"Unable to verify GitHub App '{github_auth.app_slug()}' is public before repository authorization. "
                        "Try again after GitHub API access is available, and keep PULLWISE_GITHUB_APP_VISIBILITY_CHECK enabled for user repository installs."
                    ),
                )

        existing_access = try_bind_existing_github_repository_access(user)
        if add_installation:
            state = remember_github_repository_identity_authorization(user, redirect_to, scope, add=True)
            url = f"{api_base_url(self)}/integrations/github/install/start?{urlencode({'state': state})}"
            return self.json({"url": url, "mode": "github-app-add"})

        if manage:
            existing_installations = installation_summaries_for_access(existing_access)
            if github_repository_access_connected(existing_access) and len(existing_installations) == 1:
                installation = existing_installations[0]
                installation_id = clean_github_access_text(installation.get("installationId"), allow_int=True)
                state = remember_github_installation_manage_state(user, installation, redirect_to)
                url = f"{api_base_url(self)}/integrations/github/manage/start?{urlencode({'state': state})}"
                return self.json({
                    "ok": True,
                    "connected": True,
                    "url": url,
                    "mode": "github-installation-manage",
                    "installationId": installation_id,
                })
            if github_repository_access_connected(existing_access) and existing_installations:
                return self.json({
                    "ok": True,
                    "connected": True,
                    "mode": "github-app-existing-manage-list",
                    "installationId": clean_github_access_text(existing_access.get("installationId"), allow_int=True),
                    "installationIds": clean_github_access_text_list(existing_access.get("installationIds"), allow_int=True),
                    "installationAccount": clean_github_access_text(existing_access.get("installationAccount")),
                    "installationAccounts": clean_github_access_text_list(existing_access.get("installationAccounts")),
                    "installations": public_installation_summaries(user, existing_access),
                    "identities": public_github_identities(user),
                })
            state = remember_github_repository_authorization(user, redirect_to, scope, manage=True)
            return self.json({"url": github_auth.build_app_install_url(state), "mode": "github-app"})

        if github_repository_access_connected(existing_access):
            payload = {
                "ok": True,
                "connected": True,
                "mode": "github-app-existing",
                "installationId": clean_github_access_text(existing_access.get("installationId"), allow_int=True),
            }
            return self.json(payload)
        existing_url = trusted_github_web_url(existing_access.get("installationHtmlUrl") if existing_access else None)
        if existing_access and existing_url:
            return self.json({
                "ok": True,
                "url": existing_url,
                "mode": "github-app-existing-pending",
                "installationId": existing_access.get("installationId"),
            })

        state = remember_github_repository_authorization(user, redirect_to, scope)
        return self.json({"url": github_auth.build_app_install_url(state), "mode": "github-app"})

    def handle_github_repository_callback(self, params: dict) -> None:
        if not github_auth.app_install_configured():
            if not local_github_mocks_enabled(self):
                return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub App installation is not configured.")
            session = self.current_or_demo_session()
            scope = params.get("scope") or "all"
            repository_items = REPOSITORIES if scope == "all" else REPOSITORIES[:1]
            USERS[session["userId"]]["githubRepositoryAccess"] = {
                "mode": "local",
                "scope": scope,
                "authorizedAt": now(),
                "installationId": "dev_installation_1",
                "repositories": [repo["fullName"] for repo in repository_items],
                "repositoryItems": repository_items,
                "repositoriesNeedSync": True,
            }
            mark_state_dirty()
            return self.redirect(safe_redirect_to(params.get("redirectTo"), "repos"), cookie_header(session["id"]))

        record = self.github_install_record_from_callback(params)
        user = USERS.get(str(record["userId"]))
        if not user:
            raise ValueError("The GitHub installation belongs to a user session that no longer exists.")
        if not has_github_repository_authorization_identity(user):
            raise ValueError("Sign in with GitHub before authorizing repositories.")
        state = params.get("state") or None
        if params.get("setup_action") == "request":
            clear_github_repository_authorization_pending(user, state)
            return self.redirect(
                redirect_with_params(str(record["redirectTo"]), {"github_error": "github_app_installation_not_completed"})
            )
        if not params.get("installation_id"):
            clear_github_repository_authorization_pending(user, state)
            return self.redirect(
                redirect_with_params(str(record["redirectTo"]), {"github_error": "missing_installation_id"})
            )

        installation_id = str(params["installation_id"])
        selected_identity = github_identity_by_id(
            user,
            clean_github_access_text(record.get("selectedGithubIdentityId")),
        )
        selected_token = selected_identity.get("accessToken") if selected_identity else user.get("githubAccessToken")
        installations = (
            [
                installation
                for installation in github_auth.list_current_app_installations_for_user(selected_token)
                if installation_allowed_for_identity(selected_identity, installation)
            ]
            if selected_identity
            else current_user_github_app_installations(user)
        )
        target_installation = next(
            (
                installation
                for installation in installations
                if str(installation.get("id") or "") == installation_id
            ),
            None,
        )
        if not target_installation:
            if selected_identity:
                upsert_github_identity_installation_access(
                    user,
                    selected_identity,
                    installation_id,
                    can_access=False,
                    last_error_code="github_installation_not_visible",
                )
            raise ValueError("Unable to verify this GitHub App installation belongs to the signed-in GitHub user.")

        requested_scope = params.get("scope") or record.get("requestedScope") or "selected"
        if selected_identity:
            bind_github_repository_installation_for_identity(
                user,
                target_installation,
                selected_token,
                requested_scope,
            )
            identity = selected_identity
        else:
            bind_github_repository_installations(
                user,
                installations,
                requested_scope,
            )
            identity = upsert_github_identity(
                user,
                {
                    "id": user.get("githubId"),
                    "login": user.get("githubLogin"),
                    "html_url": user.get("githubHtmlUrl"),
                    "avatar_url": user.get("avatarUrl"),
                },
                {
                    "access_token": user.get("githubAccessToken"),
                    "scope": user.get("githubOAuthScope"),
                },
            )
        upsert_github_identity_installation_access(
            user,
            identity,
            installation_id,
            can_access=True,
            verification_method="setup_callback",
        )
        clear_github_repository_authorization_pending(user, state)
        session = create_session(user)
        return self.redirect(str(record["redirectTo"]), cookie_header(session["id"]))

    def github_install_record_from_callback(self, params: dict) -> dict:
        state = params.get("state") or ""
        if not state:
            raise ValueError("GitHub authorization state is invalid or expired.")
        return pop_github_state("install", state)

    def integrations_payload(self) -> dict:
        session = self.current_session()
        user = USERS.get(session["userId"]) if session else None
        github_access = user.get("githubRepositoryAccess") if user else None
        pending = bool(github_repository_authorization_pending(user))
        visible_access = None if pending or not github_repository_access_authorized_for_user(user, github_access) else github_access
        github = {
            "provider": "github",
            "connected": github_repository_access_authorized_for_user(user, github_access)
            and github_repository_access_connected(github_access)
            and not pending,
            "authorizationPending": pending,
            "mode": clean_github_access_text(visible_access.get("mode")) if visible_access else None,
            "scope": clean_github_access_text(visible_access.get("scope")) if visible_access else None,
            "repositorySelection": clean_github_access_text(visible_access.get("repositorySelection")) if visible_access else None,
            "installationId": clean_github_access_text(visible_access.get("installationId"), allow_int=True) if visible_access else None,
            "installationIds": clean_github_access_text_list(visible_access.get("installationIds"), allow_int=True) if visible_access else [],
            "installationAccount": clean_github_access_text(visible_access.get("installationAccount")) if visible_access else None,
            "installationAccounts": clean_github_access_text_list(visible_access.get("installationAccounts")) if visible_access else [],
            "installationHtmlUrl": None,
            "identities": public_github_identities(user),
            "installations": public_installation_summaries(user, visible_access),
            "repositories": clean_github_access_text_list(visible_access.get("repositories")) if visible_access else [],
            "repositoriesNeedSync": github_repositories_need_sync(visible_access),
        }
        items = [github]
        return {"items": items, "github": github}



    def repositories_payload(self, refresh: bool = False, params: dict | None = None) -> dict:
        session = self.current_session()
        if not session:
            return {"items": [], "repositories": [], "needsAuthorization": True}

        user = USERS.get(session["userId"])
        github_access = user.get("githubRepositoryAccess") if user else None
        bound_existing_access = False
        pending = bool(github_repository_authorization_pending(user))
        if pending:
            if not refresh:
                return pending_repositories_payload()
            github_access = (
                bind_pending_selected_github_identity_access(user)
                or try_bind_existing_github_repository_access(user, force_refresh=True)
            )
            if github_repository_access_connected(github_access):
                clear_github_repository_authorization_pending(user)
                pending = False
                bound_existing_access = True
            else:
                return pending_repositories_payload()

        if github_access and not github_repository_access_authorized_for_user(user, github_access):
            github_access = try_bind_existing_github_repository_access(user, force_refresh=True)
            bound_existing_access = bool(github_access)

        if not github_access:
            github_access = try_bind_existing_github_repository_access(user)
            bound_existing_access = bool(github_access)
        if not github_access:
            return {"items": [], "repositories": [], "needsAuthorization": True}

        if refresh and not bound_existing_access and github_access.get("mode") == "github-app":
            refreshed_access = try_bind_existing_github_repository_access(user, force_refresh=True)
            if refreshed_access:
                github_access = refreshed_access
                bound_existing_access = True

        if not github_repository_access_connected(github_access):
            return unavailable_repositories_payload(github_access)
        if repository_list_params_active(params):
            payload = paginated_repository_items_for_response(user, github_access, params or {})
            payload.update(
                {
                    "needsAuthorization": False,
                    "installationId": clean_github_access_text(github_access.get("installationId"), allow_int=True),
                    "installationIds": clean_github_access_text_list(github_access.get("installationIds"), allow_int=True),
                    "repositorySelection": clean_github_access_text(github_access.get("repositorySelection")),
                    "installationAccount": clean_github_access_text(github_access.get("installationAccount")),
                    "installationAccounts": clean_github_access_text_list(github_access.get("installationAccounts")),
                    "installations": public_installation_summaries(user, github_access),
                    "repositoriesNeedSync": github_repositories_need_sync(github_access),
                }
            )
            return payload

        repository_items = repository_items_for_response(user, github_access)
        return {
            "items": repository_items,
            "repositories": repository_items,
            "needsAuthorization": False,
            "installationId": clean_github_access_text(github_access.get("installationId"), allow_int=True),
            "installationIds": clean_github_access_text_list(github_access.get("installationIds"), allow_int=True),
            "repositorySelection": clean_github_access_text(github_access.get("repositorySelection")),
            "installationAccount": clean_github_access_text(github_access.get("installationAccount")),
            "installationAccounts": clean_github_access_text_list(github_access.get("installationAccounts")),
            "installations": public_installation_summaries(user, github_access),
            "repositoriesNeedSync": github_repositories_need_sync(github_access),
        }

    def repositories_connected(self) -> bool:
        session = self.current_session()
        if not session:
            return False
        return github_repositories_connected_for_user(USERS.get(session["userId"]))

    def current_or_demo_session(self) -> dict:
        session = self.current_session()
        if session:
            return session
        user = get_or_create_github_user()
        return create_session(user)

    def current_session(self) -> dict | None:
        for session_id in self.current_session_id_candidates():
            session = self.current_session_for_id(session_id)
            if session:
                return session
        return None

    def current_session_for_id(self, session_id: str) -> dict | None:
        with STATE_LOCK:
            session = SESSIONS.get(session_id)
            if not session:
                return None
            if not isinstance(session, dict):
                SESSIONS.pop(session_id, None)
                mark_state_dirty()
                return None
            expires_at = pull_request_timestamp(session.get("expiresAt"))
            user_id = session.get("userId")
            if expires_at is None or not isinstance(user_id, str) or not user_id:
                SESSIONS.pop(session_id, None)
                mark_state_dirty()
                return None
            if expires_at < now():
                SESSIONS.pop(session_id, None)
                mark_state_dirty()
                return None
            user = USERS.get(user_id)
            if not user:
                SESSIONS.pop(session_id, None)
                mark_state_dirty()
                return None
            if github_auth.oauth_configured() and user and "github" in user.get("providers", []) and not user.get("githubAccessToken"):
                SESSIONS.pop(session_id, None)
                mark_state_dirty()
                return None
            return session

    def current_session_id(self) -> str | None:
        candidates = self.current_session_id_candidates()
        for session_id in candidates:
            if self.current_session_for_id(session_id):
                return session_id
        return candidates[0] if candidates else None

    def current_session_id_candidates(self) -> list[str]:
        authorization_token = bearer_token(self)
        if authorization_token and not authorization_token.startswith(API_KEY_PREFIX):
            return [authorization_token]
        raw_cookie = request_header(self, "Cookie") or ""
        session_ids: list[str] = []
        for item in raw_cookie.split(";"):
            name, separator, value = item.partition("=")
            if separator and name.strip() == SESSION_COOKIE:
                session_id = value.strip().strip('"')
                if session_id:
                    session_ids.append(session_id)
        return session_ids

    def current_api_key_context(self) -> dict | None:
        cached = getattr(self, "_api_key_context", None)
        if cached is not None:
            return cached
        token = api_key_token(self)
        if not token:
            self._api_key_context = None
            return None
        token_hash = api_key_hash(token)
        record = db.get_api_key_by_hash(token_hash)
        if not record:
            self._api_key_context = None
            return None
        expires_at = pull_request_timestamp(record.get("expires_at"))
        if expires_at and expires_at < now():
            self._api_key_context = None
            return None
        user = USERS.get(str(record.get("user_id") or ""))
        if not user:
            self._api_key_context = None
            return None
        db.mark_api_key_used(record["id"])
        context = {
            "apiKey": record,
            "user": user,
            "scopes": parse_api_key_scopes(record.get("scopes")),
            "restrictions": parse_api_key_restrictions(record.get("restrictions")),
        }
        self._api_key_context = context
        return context

    def require_api_key_context(self, scope: str, *, allow_restricted: bool = False) -> dict | None:
        context = self.current_api_key_context()
        if not context:
            self.error(HTTPStatus.UNAUTHORIZED, "A valid Pullwise API key is required.")
            return None
        if scope not in context.get("scopes", []):
            self.error(HTTPStatus.FORBIDDEN, f"API key scope {scope} is required.")
            return None
        if context.get("restrictions") and not allow_restricted:
            self.error(HTTPStatus.FORBIDDEN, "This API key is restricted to a specific audit bundle download.")
            return None
        return context

    def api_key_context_allows_audit_bundle(self, context: dict, scan: dict, requested_repo_id: str = "") -> bool:
        restrictions = context.get("restrictions") if isinstance(context.get("restrictions"), dict) else {}
        if not restrictions:
            return True
        if restrictions.get("kind") != "audit_bundle":
            return False
        scan_id = public_issue_text(scan.get("id"))
        restricted_scan_id = public_issue_text(restrictions.get("scanId"))
        if restricted_scan_id and restricted_scan_id != scan_id:
            return False
        restricted_repo_id = clean_github_access_text(restrictions.get("repoId"), allow_int=True)
        if restricted_repo_id:
            scan_repo_id = clean_github_access_text(scan.get("repoId"), allow_int=True)
            request_repo_id = clean_github_access_text(requested_repo_id, allow_int=True)
            if restricted_repo_id not in {scan_repo_id, request_repo_id}:
                return False
        return True

    def api_repository_context(self, context: dict, repo_id: str) -> tuple[dict, dict] | None:
        repo_id = clean_github_access_text(repo_id, allow_int=True) or ""
        if not repo_id:
            self.error(HTTPStatus.BAD_REQUEST, "repoId is required.")
            return None
        user = context.get("user") if isinstance(context.get("user"), dict) else None
        github_access = user.get("githubRepositoryAccess") if user else None
        if api_repository_access_denial_for_user(user, github_access):
            self.error(HTTPStatus.NOT_FOUND, "Repository is not authorized for this account.")
            return None
        if user and isinstance(github_access, dict):
            sync_repository_access_for_user(user, github_access)
        repository = db.get_repository(repo_id)
        if not repository or not api_repository_authorized_for_user(user, repository):
            self.error(HTTPStatus.NOT_FOUND, "Repository is not authorized for this account.")
            return None
        repository_item_meta = (
            repository_item_by_repo_id(github_access, repo_id)
            or repository_item_by_repo_id(github_access, str(repository.get("id") or ""))
            or repository_item_by_repo_id(github_access, str(repository.get("github_repo_id") or ""))
            or repository_item(github_access, str(repository.get("full_name") or ""))
            or {}
        )
        return repository, repository_item_meta

    def handle_api_keys_get(self, params: dict) -> None:
        session = self.current_session()
        if not session:
            return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing API keys.")
        user = USERS[session["userId"]]
        keys = [api_key_public_payload(item) for item in db.list_api_keys_for_user(user["id"])]
        return self.json({"items": keys, "apiKeys": keys}, headers=api_key_response_headers())

    def handle_api_keys_post(self, body: dict) -> None:
        session = self.current_session()
        if not session:
            return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before creating API keys.")
        if not isinstance(body, dict):
            return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        user = USERS[session["userId"]]
        scopes, scopes_error = requested_api_key_scopes(body.get("scopes"), provided="scopes" in body)
        if scopes_error:
            return self.error(HTTPStatus.BAD_REQUEST, scopes_error)
        try:
            restrictions = parse_api_key_restrictions(body.get("restrictions"))
        except ValueError:
            return self.error(HTTPStatus.BAD_REQUEST, "Unsupported API key restriction.")
        expires_at = pull_request_timestamp(body.get("expiresAt") or body.get("expires_at"))
        expires_in_seconds = non_negative_int(body.get("expiresInSeconds") or body.get("expires_in_seconds"))
        if expires_in_seconds:
            expires_at = now() + expires_in_seconds
        token = API_KEY_PREFIX + secrets.token_urlsafe(32)
        record = db.create_api_key(
            {
                "id": make_id("ak"),
                "user_id": user["id"],
                "name": public_issue_text(body.get("name")) or "API key",
                "key_prefix": api_key_prefix(token),
                "key_hash": api_key_hash(token),
                "scopes": scopes,
                "expires_at": expires_at,
                "restrictions": restrictions,
            }
        )
        return self.json(api_key_public_payload(record, token=token), HTTPStatus.CREATED, headers=api_key_response_headers())

    def handle_api_key_delete(self, key_id: str) -> None:
        session = self.current_session()
        if not session:
            return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before revoking API keys.")
        if not db.revoke_api_key(key_id, session["userId"]):
            return self.error(HTTPStatus.NOT_FOUND, "API key not found.")
        return self.json({"ok": True, "id": public_issue_text(key_id), "revoked": True})






    def handle_external_api_get(self, segments: list[str], params: dict) -> None:
        if product_api.handle_get(self, segments, params, USERS):
            return
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_external_api_post(self, segments: list[str], body: dict) -> None:
        if product_api.handle_post(self, segments, body, USERS):
            return
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")



    def clear_current_session(self) -> None:
        session_id = self.current_session_id()
        with STATE_LOCK:
            if session_id and SESSIONS.pop(session_id, None):
                mark_state_dirty()

    def find_or_404(self, collection: list[dict], item_id: str, label: str) -> dict:
        for item in collection:
            if item.get("id") == item_id:
                return item
        raise ResourceNotFound(label)

    def read_json(self) -> dict:
        return decode_json_body(
            self.read_raw_body(),
            self.headers.get("Content-Encoding", ""),
            max_decompressed_bytes=self.request_decompressed_body_limit(),
        )

    def request_decompressed_body_limit(self) -> int:
        if self.current_session() or self.current_api_key_context():
            return max_decompressed_body_bytes()
        return max_unauthenticated_decompressed_body_bytes()

    def read_raw_body(self) -> bytes:
        length = self.request_content_length()
        if length == 0:
            return b""
        if length > self.request_body_size_limit():
            raise RequestBodyTooLarge("Request body is too large.")
        return self.rfile.read(length)

    def request_body_size_limit(self) -> int:
        value = getattr(self, "_request_body_size_limit", None)
        if isinstance(value, int) and value >= 0:
            return value
        return max_body_bytes()

    def enforce_body_size_limit(self, method: str, path: str = "", segments: list[str] | None = None) -> None:
        if method not in {"POST", "PUT", "PATCH"}:
            return
        length = self.request_content_length()
        limit = max_body_bytes()
        self._request_body_size_limit = limit
        if length > limit:
            raise RequestBodyTooLarge("Request body is too large.")

    def request_content_length(self) -> int:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return 0
        raw_text = str(raw_length).strip()
        if not raw_text:
            return 0
        if not raw_text.isdigit():
            raise ValueError("Invalid Content-Length header.")
        return int(raw_text)

    def handle_creem_webhook(self) -> None:
        raw = self.read_raw_body()
        if not billing.verify_creem_webhook(raw, self.headers.get("creem-signature")):
            logger.warning("Rejected Creem webhook with invalid signature.")
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid Creem webhook signature.")
        event = decode_json_body(raw)
        if not isinstance(event, dict):
            return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        update = billing.billing_update_from_creem_event(event)
        if update:
            result = self.apply_billing_update(update)
            logger.info(
                "Processed Creem webhook eventType=%s eventId=%s result=%s",
                update.get("eventType"),
                update.get("eventId"),
                result,
            )
        else:
            logger.info(
                "Ignored Creem webhook eventType=%s eventId=%s result=unsupported_or_unmapped",
                event.get("eventType") or event.get("type"),
                event.get("id") or event.get("eventId"),
            )
        return self.json({"received": True})

    def apply_billing_update(self, update: dict) -> str:
        with STATE_LOCK:
            if billing_event_processed(update):
                return "duplicate"
            user = billing_user_for_update(update)
            if user:
                applied = apply_billing_update_to_user(user, update)
                apply_pending_billing_updates_for_user(user)
                return "applied" if applied else "stale"
            pending_count = len(BILLING_PENDING_UPDATES)
            remember_pending_billing_update(update)
            if len(BILLING_PENDING_UPDATES) > pending_count:
                return "pending"
            return "unmatched"

    def send_cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        allowed = trusted_browser_origins()
        if origin and origin in allowed:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")

    def json(self, payload: dict, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            response_headers = {**getattr(self, "_rate_limit_headers", {}), **(headers or {})}
            for key, value in response_headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_DISCONNECT_EXCEPTIONS as exc:
            raise ClientDisconnected("Client disconnected before the response was sent.") from exc

    def text(self, payload: str, status: int = HTTPStatus.OK, *, content_type: str = "text/plain; charset=utf-8") -> None:
        body = payload.encode("utf-8")
        try:
            self.send_response(status)
            self.send_cors_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in getattr(self, "_rate_limit_headers", {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_DISCONNECT_EXCEPTIONS as exc:
            raise ClientDisconnected("Client disconnected before the response was sent.") from exc

    def binary(
        self,
        payload: bytes,
        status: int = HTTPStatus.OK,
        *,
        content_type: str = "application/octet-stream",
        headers: dict[str, str] | None = None,
    ) -> None:
        body = payload if isinstance(payload, bytes) else bytes(payload or b"")
        try:
            self.send_response(status)
            self.send_cors_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            response_headers = {**getattr(self, "_rate_limit_headers", {}), **(headers or {})}
            for key, value in response_headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_DISCONNECT_EXCEPTIONS as exc:
            raise ClientDisconnected("Client disconnected before the response was sent.") from exc

    def redirect(self, location: str, set_cookie: str | None = None) -> None:
        try:
            self.send_response(HTTPStatus.FOUND)
            self.send_cors_headers()
            self.send_header("Location", location)
            if set_cookie:
                self.send_header("Set-Cookie", set_cookie)
            self.end_headers()
        except _CLIENT_DISCONNECT_EXCEPTIONS as exc:
            raise ClientDisconnected("Client disconnected before the response was sent.") from exc

    def serve_static_file(self, file_path: str) -> None:
        """Serve a static file from disk with appropriate headers."""
        try:
            stat = os.stat(file_path)
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            return self.error(HTTPStatus.NOT_FOUND, "File not found")
        content_type, _ = mimetypes.guess_type(file_path)
        content_type = content_type or "application/octet-stream"
        try:
            with open(file_path, "rb") as f:
                body = f.read()
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            return self.error(HTTPStatus.NOT_FOUND, "File not found")
        try:
            self.send_response(HTTPStatus.OK)
            self.send_cors_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(stat.st_size))
            # Cache static assets for 1 year (hashed filenames), don't cache index.html
            if os.path.basename(file_path) == "index.html":
                self.send_header("Cache-Control", "no-cache")
            elif "/assets/" in file_path.replace("\\", "/"):
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_DISCONNECT_EXCEPTIONS as exc:
            raise ClientDisconnected("Client disconnected before the response was sent.") from exc

    def serve_spa(self) -> None:
        """Serve the SPA index.html for client-side routing."""
        root = web_root()
        index = os.path.join(root, "index.html")
        if os.path.isfile(index):
            self.serve_static_file(index)
        else:
            self.error(HTTPStatus.NOT_FOUND, "Frontend not built. Run 'npm run build' in pullwise-web.")

    def error(self, status: int, message: str) -> None:
        self.json({"message": message}, status)


def http_request_queue_size() -> int:
    return max(5, env_int("PULLWISE_HTTP_REQUEST_QUEUE_SIZE", 512))


class PullwiseThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, fact_sync=None, **kwargs) -> None:
        self.request_queue_size = http_request_queue_size()
        self._fact_sync = fact_sync
        super().__init__(*args, **kwargs)

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        if self._fact_sync is None:
            return super().serve_forever(poll_interval=poll_interval)
        stop = threading.Event()
        worker = threading.Thread(target=self._fact_sync.run_forever, args=(stop,), name="pullwise-fact-sync", daemon=True)
        worker.start()
        try:
            super().serve_forever(poll_interval=poll_interval)
        finally:
            stop.set()
            worker.join(timeout=5)

def main(*, fact_sync=None) -> None:
    load_env_file()
    logging_config.configure_logging(project_root=project_root())
    parser = argparse.ArgumentParser(description="Run the Pullwise local API server.")
    parser.add_argument("--host", default=env("PULLWISE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=parse_port, default=server_port())
    args = parser.parse_args()

    ensure_state_loaded()
    persist_state()
    if fact_sync is None:
        from copy import deepcopy
        from .github_local_runtime import build_local_fact_sync
        from .product_store import ProductStore

        app_id = github_auth.app_id()
        webhook_secret = os.environ.get("PULLWISE_GITHUB_WEBHOOK_SECRET", "")
        if re.fullmatch(r"[1-9][0-9]*", app_id) and webhook_secret and github_auth.app_api_configured():
            store = ProductStore(db.database_path())
            store.initialize()

            def account_snapshot(owner):
                with STATE_LOCK:
                    account = USERS.get(owner)
                    return deepcopy(account) if account is not None else None

            def app_token():
                Auth, _, _ = github_auth.import_pygithub()
                return Auth.AppAuth(github_auth.app_id_int(), github_auth.app_private_key()).token

            fact_sync = build_local_fact_sync(store, account_snapshot=account_snapshot,
                identities_for_user=github_identities_for_user,
                installation_access_for_user=latest_installation_access_record,
                app_id=app_id, webhook_secret=webhook_secret, app_token=app_token,
                installation_token=github_auth.create_installation_access_token)
            logger.info("Local GitHub fact collection enabled; analysis admission disabled.")
        else:
            logger.warning("Local GitHub fact collection unavailable: App ID, private key or webhook secret missing.")
    server_options = {"fact_sync": fact_sync} if fact_sync is not None else {}
    httpd = PullwiseThreadingHTTPServer((args.host, args.port), PullwiseHandler, **server_options)
    logger.info("Pullwise API listening on http://%s:%s", args.host, args.port)
    logger.info("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
