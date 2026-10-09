"""Server Worker with coordinated preview-only recurring expense scheduling."""
import asyncio
import hashlib
import io
import json
import time
from urllib.parse import urlsplit, parse_qs, unquote

from workers import DurableObject, Response, WorkerEntrypoint

from pullwise_server.cloudflare_validation_budget import (
    BUDGET_SCOPE, REQUEST_SECONDS, BudgetError, BudgetJournal, MeteredD1,
    REVIEWED_REMOTE_PLANS,
    REVIEWED_INITIALIZATION_PLAN, run_initialization,
    READ_CEILING, WRITE_CEILING, _field,
)

from pullwise_server.cloudflare_http_contract import handle_http_request
from pullwise_server.cloudflare_github_identity_http import handle_identity_request
from pullwise_server.cloudflare_email_auth import handle_email_request
from pullwise_server.cloudflare_email_gateway import WorkerEmailGateway
from pullwise_server.cloudflare_github_gateway import GitHubFailure, WorkerGitHubGateway
from pullwise_server.cloudflare_billing_mutations import handle_billing_mutation
from pullwise_server.cloudflare_billing_catalog_refresh import read_or_refresh_catalog
from pullwise_server.cloudflare_creem_gateway import WorkerCreemGateway, product_bindings, webhook_product_ids
from pullwise_server.cloudflare_ledger_profile import read_ledger_me
from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_ledger_recurring import run_due_recurring
from pullwise_server.cloudflare_jev_gateway import WorkerJevGateway
from pullwise_server.cloudflare_ledger_reports import CsvExport
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1, PlanLimitError
from pullwise_server.ledger_plan_policy import parse_policy
from pullwise_server.cloudflare_preview_budget import ProductMeteredD1, initialize_product, reconcile_schema_reads, upgrade_product_schema, upgrade_product_schema_v6, upgrade_product_schema_v7, upgrade_product_schema_v8, upgrade_product_schema_v9, migrate_product_state_records
from pullwise_server.cloudflare_preview_rate import PreviewRateLimiter, PreviewRateLimit, EmailRateLimiter, EmailRateLimit, request_channel
from pullwise_server.cloudflare_native_d1 import NativeD1
from pullwise_server.json_input import validate_json_unicode


def _request_target(request):
    """Bound decoded URL inputs before authentication, providers or D1."""
    raw_url = str(request.url)
    try:
        if len(raw_url.encode("utf-8")) > 32768:
            return None, Response.json({"error": {"code": "REQUEST_TOO_LARGE"}},
                status=413, headers={"Cache-Control": "no-store"})
        parsed = urlsplit(raw_url)
        decoded_path = unquote(parsed.path, encoding="utf-8", errors="strict")
        params = parse_qs(parsed.query, encoding="utf-8", errors="strict",
            keep_blank_values=True, max_num_fields=100)
        validate_json_unicode([decoded_path, params])
        # Routing retains the raw path, so path IDs must satisfy the SQL
        # envelope in that representation as well as after URL decoding.
        texts = [parsed.path, decoded_path, *params.keys(),
            *(value for values in params.values() for value in values)]
        if any(len(value.encode("utf-8")) > 8192 for value in texts):
            return None, Response.json({"error": {"code": "REQUEST_TOO_LARGE"}},
                status=413, headers={"Cache-Control": "no-store"})
    except (ValueError, UnicodeError):
        return None, Response.json({"error": {"code": "INVALID_INPUT"}},
            status=422, headers={"Cache-Control": "no-store"})
    return (parsed.path, params), None


def _csv_stream(export):
    """Let the runtime request one D1-backed CSV chunk at a time."""
    from js import Object, ReadableStream, TextEncoder
    from pyodide.ffi import create_proxy, to_js

    chunks = export.chunks().__aiter__()
    encoder = TextEncoder.new()

    async def pull(controller):
        try:
            chunk = await chunks.__anext__()
        except StopAsyncIteration:
            controller.close()
            return
        controller.enqueue(encoder.encode(chunk))

    return ReadableStream.new(to_js({"pull": create_proxy(pull)},
        dict_converter=Object.fromEntries))


class _Application:
    def __init__(self, env, binding):
        self.env = env
        self.bounded_exports = isinstance(binding, ProductMeteredD1)
        self.binding = PlanLimitedD1(binding,
            policy=parse_policy(getattr(env, "PULLWISE_PLAN_LIMITS_JSON", None)), now=int(time.time()))
        self.jev_gateway = WorkerJevGateway(env)
        self.binding.jev_available = self.jev_gateway.enabled
        self.email_admission = None

    async def fetch(self, request):
        if str(getattr(self.env, "PULLWISE_D1_ACCESS_ENABLED", "0")) != "1":
            return Response.json({"error": {"code": "D1_ACCESS_PAUSED"}}, status=503,
                                 headers={"Cache-Control": "no-store"})
        target, invalid_target = _request_target(request)
        if invalid_target is not None:
            return invalid_target
        path, params = target
        raw_products = getattr(self.env, "PULLWISE_CREEM_PRODUCT_IDS_JSON", "")
        try:
            products = product_bindings(json.loads(raw_products)) if raw_products else {}
        except (TypeError, ValueError):
            products = {}

        async def read_body():
            body = await request.bytes()
            return body if isinstance(body, bytes) else body.to_bytes()

        headers = {
            "Content-Length": request.headers.get("content-length") or "",
            "creem-signature": request.headers.get("creem-signature") or "",
            "Cookie": request.headers.get("cookie") or "",
            "Authorization": request.headers.get("authorization") or "",
            "X-Pullwise-Api-Key": request.headers.get("x-pullwise-api-key") or "",
            "X-Pullwise-Workspace": request.headers.get("x-pullwise-workspace") or "",
            "X-Request-Id": request.headers.get("x-request-id") or "",
            "If-Match": request.headers.get("if-match") or "",
            "Idempotency-Key": request.headers.get("idempotency-key") or "",
            "Origin": request.headers.get("origin") or "",
            "Referer": request.headers.get("referer") or "",
        }
        now = int(time.time())
        if path.startswith("/api/v1/"):
            workspace_values = params.get("workspaceId", [])
            if (len(workspace_values) > 1 or workspace_values and (
                    not workspace_values[0] or headers["X-Pullwise-Workspace"]
                    and workspace_values[0] != headers["X-Pullwise-Workspace"])):
                return Response.json({"error": {"code": "INVALID_INPUT"}}, status=422,
                    headers={"Cache-Control": "no-store"})
            if workspace_values:
                headers["X-Pullwise-Workspace"] = workspace_values[0]
        self.binding.now = now
        trusted_origins = {value.strip() for value in (
            getattr(self.env, "PULLWISE_ALLOWED_ORIGINS", "") + "," +
            getattr(self.env, "PULLWISE_APP_URL", "")).split(",")
            if value.strip() and value.strip() != "*"}
        if path in {"/auth/email/request-code", "/auth/email/verify-code"}:
            try:
                body = None
                if request.method == "POST":
                    raw = await read_body()
                    if len(raw) > 8192:
                        return Response.json({"error": {"code": "REQUEST_TOO_LARGE"}},
                            status=413, headers={"Cache-Control": "no-store"})
                    body = json.loads(raw)
                    validate_json_unicode(body)
                result = await handle_email_request(
                    binding=self.binding, gateway=WorkerEmailGateway(self.env),
                    secret=getattr(self.env, "PULLWISE_EMAIL_CODE_SECRET", None),
                    admit=self.email_admission, method=request.method, path=path,
                    body=body, headers=headers, now=now,
                    cookie_same_site=getattr(self.env, "PULLWISE_COOKIE_SAME_SITE", "Lax"),
                    trusted_origins=trusted_origins)
                status, payload, response_headers = result
                return Response.json(payload, status=status, headers=response_headers)
            except EmailRateLimit as error:
                return Response.json(error.response(), status=429,
                    headers={"Cache-Control": "no-store", "Retry-After": str(error.retry_after)})
            except (ValueError, UnicodeError):
                return Response.json({"error": {"code": "INVALID_INPUT"}}, status=422,
                    headers={"Cache-Control": "no-store"})
            except Exception:
                # No exception text, OTP, email or provider diagnostic is public.
                return _unavailable("EMAIL_AUTH_UNAVAILABLE")
        try:
            identity = None if path == "/api/v1/repositories" else await handle_identity_request(
                method=request.method, path=path, params=params,
                headers=headers, binding=self.binding,
                gateway=WorkerGitHubGateway(self.env), now=now,
                app_url=getattr(self.env, "PULLWISE_APP_URL", ""),
                callback_url=getattr(self.env, "PULLWISE_GITHUB_CALLBACK_URL", ""),
                cookie_same_site=getattr(self.env, "PULLWISE_COOKIE_SAME_SITE", "Lax"),
                trusted_origins=trusted_origins,
            )
        except Exception as error:
            status, payload = _identity_failure(self.env, error)
            return Response.json(payload, status=status,
                                 headers={"Cache-Control": "no-store"})
        if identity is not None:
            identity_status, identity_payload, identity_headers = identity
            if identity_status == 302:
                return Response(None, status=302, headers=identity_headers)
            return Response.json(identity_payload, status=identity_status, headers=identity_headers)
        if path == "/api/v1/me" and request.method == "GET":
            try:
                status, payload = await read_ledger_me(
                    binding=self.binding, headers=headers, now=now)
            except Exception:
                status, payload = 503, {"error": {"code": "SERVER_UNAVAILABLE"}}
            return Response.json(payload, status=status, headers={"Cache-Control": "no-store"})
        if path.startswith(("/api/v1/projects", "/api/v1/categories",
                            "/api/v1/expenses", "/api/v1/reports/",
                            "/api/v1/expense-suggestions", "/api/v1/expense-recurring-rules", "/api/v1/workspaces",
                            "/api/v1/workspace-invitations", "/api/v1/workspace-invitation-requests", "/api/v1/repositories", "/api/v1/activity")):
            from pullwise_server.cloudflare_principal import _cookie_sessions
            if request.method in {"POST", "PATCH", "DELETE"} and _cookie_sessions(headers):
                from urllib.parse import urlsplit as split_origin
                origin = split_origin(headers["Origin"] or headers["Referer"])
                if f"{origin.scheme}://{origin.netloc}" not in trusted_origins:
                    return Response.json({"error": {"code": "UNTRUSTED_ORIGIN"}},
                        status=403, headers={"Cache-Control": "no-store"})
            try:
                body = None
                if request.method in {"POST", "PATCH"}:
                    raw = await read_body()
                    if len(raw) > 8192:
                        return Response.json({"error": {"code": "REQUEST_TOO_LARGE"}},
                            status=413, headers={"Cache-Control": "no-store"})
                    body = json.loads(raw)
                    validate_json_unicode(body)
                result = await handle_ledger_request(
                    binding=self.binding, gateway=WorkerGitHubGateway(self.env),
                    method=request.method, path=path,
                    headers=headers, params=params,
                    body=body, now=now, suggestion_gateway=self.jev_gateway)
                status, payload = result if result is not None else (404, {"error": {"code": "NOT_FOUND"}})
            except PlanLimitError as error:
                status, payload = error.response()
            except (ValueError, UnicodeError):
                status, payload = 422, {"error": {"code": "INVALID_INPUT"}}
            except Exception as error:
                if path == "/api/v1/repositories":
                    status, payload = _identity_failure(self.env, error)
                else:
                    status, payload = 503, {"error": {"code": "SERVER_UNAVAILABLE"}}
            response_headers = {"Cache-Control": "no-store",
                "Vary": "Cookie, Authorization, X-Pullwise-Api-Key, X-Pullwise-Workspace"}
            if status == 204:
                return Response(None, status=status, headers=response_headers)
            if isinstance(payload, CsvExport):
                if self.bounded_exports:
                    # Finish metered D1 work before closing the product ticket.
                    # UTF-8 bytes avoid whole-body Unicode expansion/copies;
                    # 24 MiB leaves room within Workers' 128 MiB memory limit.
                    # The size guard is a request error, never a global stop.
                    output, size = io.BytesIO(), 0
                    async for chunk in payload.chunks():
                        encoded = chunk.encode("utf-8")
                        size += len(encoded)
                        if size > 24 * 1024 * 1024:
                            return Response.json({"error": {"code": "EXPORT_TOO_LARGE",
                                "message": "Narrow the date range or project filter and export again."}},
                                status=413, headers=response_headers)
                        output.write(encoded)
                    return Response(output.getvalue(), status=status,
                        headers={**response_headers, "Content-Type": "text/csv; charset=utf-8",
                                 "Content-Disposition": 'attachment; filename="expenses.csv"'})
                return Response(_csv_stream(payload), status=status,
                    headers={**response_headers, "Content-Type": "text/csv; charset=utf-8",
                             "Content-Disposition": 'attachment; filename="expenses.csv"'})
            if isinstance(payload, str):
                return Response(payload, status=status,
                    headers={**response_headers, "Content-Type": "text/csv; charset=utf-8",
                             "Content-Disposition": 'attachment; filename="expenses.csv"'})
            if status in {200, 201} and isinstance(payload, dict) and isinstance(payload.get("revision"), int):
                response_headers["ETag"] = f'"{payload["revision"]}"'
            return Response.json(payload, status=status, headers=response_headers)
        if path == "/billing/plan" and request.method == "GET":
            try:
                status, payload = await read_or_refresh_catalog(
                    binding=self.binding, gateway=WorkerCreemGateway(self.env),
                    headers=headers, products=products, now=now)
            except Exception as error:
                status, payload = 503, {"error": {"code": "BILLING_CATALOG_UNAVAILABLE"}}
                if getattr(self.env, "PULLWISE_MODE", "") == "preview":
                    import traceback
                    frames = traceback.extract_tb(error.__traceback__)
                    if frames:
                        frame = frames[-1]
                        payload["error"]["diagnosticSite"] = f"{type(error).__name__}:{frame.name}:{frame.lineno}"
                    # Provider exception text can contain arbitrary credentials
                    # and body fragments; only the fixed code/type/site is safe.
            return Response.json(payload, status=status, headers={"Cache-Control": "no-store"})
        if path in {"/billing/checkout-sessions", "/billing/change-interval",
                    "/billing/cancel-subscription", "/billing/resume-subscription"}:
            try:
                raw = await read_body()
                if len(raw) > 8192:
                    return Response.json({"error": {"code": "REQUEST_TOO_LARGE"}}, status=413)
                body = json.loads(raw)
                validate_json_unicode(body)
            except (ValueError, UnicodeError):
                return Response.json({"error": {"code": "INVALID_REQUEST"}}, status=422,
                    headers={"Cache-Control": "no-store"})
            except Exception:
                return Response.json({"error": {"code": "BILLING_UNAVAILABLE"}}, status=503,
                    headers={"Cache-Control": "no-store"})
            try:
                status, payload = await handle_billing_mutation(
                    binding=self.binding, gateway=WorkerCreemGateway(self.env),
                    now=now, method=request.method, path=path, headers=headers, body=body,
                    app_url=getattr(self.env, "PULLWISE_APP_URL", ""),
                    trusted_origins=trusted_origins, products=products)
            except Exception:
                status, payload = 503, {"error": {"code": "BILLING_UNAVAILABLE"}}
            return Response.json(payload, status=status, headers={"Cache-Control": "no-store"})
        status, payload = await handle_http_request(
            method=request.method,
            path=path,
            params=params,
            headers=headers,
            read_body=read_body,
            binding=self.binding,
            creem_secret=getattr(self.env, "PULLWISE_CREEM_WEBHOOK_SECRET", ""),
            configured_products=webhook_product_ids(products),
            now=now,
            cookie_same_site=getattr(self.env, "PULLWISE_COOKIE_SAME_SITE", "Lax"),
            trusted_origins=trusted_origins,
        )
        if status == 204:
            return Response(None, status=status)
        response_headers = ({"Cache-Control": "no-store", "Pragma": "no-cache",
            "Vary": "Cookie, Authorization, X-Pullwise-Api-Key"}
            if path in {"/billing", "/billing/plan", "/api-keys"}
               or path.startswith("/api-keys/")
               or path.startswith("/api/v1/") else None)
        return Response.json(payload, status=status, headers=response_headers)


def _identity_failure(env, error):
    status = error.status if isinstance(error, GitHubFailure) else 503
    payload = {"error": {"code": error.code if isinstance(error, GitHubFailure) else "IDENTITY_UNAVAILABLE"}}
    if getattr(env, "PULLWISE_MODE", "") == "preview":
        import traceback
        frames = traceback.extract_tb(error.__traceback__)
        if frames:
            frame = frames[-1]
            payload["error"]["diagnosticSite"] = f"{type(error).__name__}:{frame.name}:{frame.lineno}"
        if isinstance(error, GitHubFailure) and error.provider_status is not None:
            payload["error"]["providerStatus"] = error.provider_status
        # Only allowlisted codes, function/line and numeric HTTP status. Never
        # stringify the exception, request, user, provider body or credentials.
        print(json.dumps({"identityFailure": payload["error"]}))
    return status, payload


def _unavailable(code):
    return Response.json({"error": {"code": code}}, status=503,
                         headers={"Cache-Control": "no-store"})


def _recurring_enabled(env):
    return (str(getattr(env, "PULLWISE_D1_ACCESS_ENABLED", "0")) == "1"
        and getattr(env, "PULLWISE_MODE", "") == "preview"
        and str(getattr(env, "PULLWISE_PREVIEW_PRODUCT_ENABLED", "0")) == "1"
        and str(getattr(env, "PULLWISE_RECURRING_EXPENSES_ENABLED", "0")) == "1")


def _email_ip_subject(request):
    # Cloudflare supplies this header at ingress. Do not trust X-Forwarded-For
    # or expose raw network addresses to the admission RPC or journal.
    request_headers = getattr(request, "headers", None)
    address = (request_headers.get("cf-connecting-ip") if request_headers is not None else None) or "unknown"
    return hashlib.sha256(("pullwise-email-ip:" + address).encode("utf-8")).hexdigest()


def _remote_email_admission(env, request):
    namespace = getattr(env, "VALIDATION_BUDGET", None)
    if namespace is None:
        return None
    ip_subject = _email_ip_subject(request)

    async def admit(kind, email_subject):
        stub = namespace.get(namespace.idFromName(BUDGET_SCOPE))
        result = await stub.admitEmail(kind, email_subject, ip_subject)
        if not bool(_field(result, "ok", False)):
            retry_after = _field(result, "retryAfter", None)
            if retry_after is not None:
                raise EmailRateLimit(retry_after)
            raise RuntimeError("EMAIL_ADMISSION_UNAVAILABLE")
        return True
    return admit


class Default(WorkerEntrypoint):
    async def scheduled(self, controller, env, ctx):
        # Await the only background task. Keep all D1 work inside the existing
        # singleton coordinator; controller time never becomes a client clock.
        if not _recurring_enabled(self.env):
            return
        scheduled = float(controller.scheduledTime)
        if (str(controller.cron) != "0 * * * *" or not 0 <= scheduled <= 9007199254740991):
            raise RuntimeError("RECURRING_TRIGGER_INVALID")
        namespace = getattr(self.env, "VALIDATION_BUDGET", None)
        if namespace is None:
            raise RuntimeError("VALIDATION_CONTROL_REQUIRED")
        stub = namespace.get(namespace.idFromName(BUDGET_SCOPE))
        result = await stub.runRecurring()
        if not bool(_field(result, "ok", False)):
            raise RuntimeError("RECURRING_EXECUTION_UNAVAILABLE")

    async def fetch(self, request):
        if str(getattr(self.env, "PULLWISE_D1_ACCESS_ENABLED", "0")) != "1":
            return _unavailable("D1_ACCESS_PAUSED")
        mode = getattr(self.env, "PULLWISE_MODE", "")
        if mode in ("local", "production"):
            # Production activation uses the ordinary auth/quota application,
            # without preview initialization or its validation coordinator.
            # Local-only config exercises the same chain; remote config checks
            # reject that local mode. Both still require explicit D1 access.
            try:
                application = _Application(self.env, NativeD1(self.env.DB))
                if urlsplit(str(request.url)).path in {"/auth/email/request-code", "/auth/email/verify-code"}:
                    application.email_admission = _remote_email_admission(self.env, request)
                return await application.fetch(request)
            except ValueError:
                return _unavailable("PLAN_POLICY_INVALID")
            except Exception:
                # Configuration/native errors must not expose binding details,
                # credentials or arbitrary provider exception text.
                return _unavailable("SERVER_UNAVAILABLE")
        if mode != "preview" or getattr(self.env, "VALIDATION_BUDGET", None) is None:
            return _unavailable("VALIDATION_CONTROL_REQUIRED")
        try:
            namespace = self.env.VALIDATION_BUDGET
            # Fixed across callers, phases and databases. Never use a request,
            # account, env run ID or DB ID to create another budget instance.
            stub = namespace.get(namespace.idFromName(BUDGET_SCOPE))
            return await stub.fetch(request)
        except Exception:
            # No fallback to DB and no automatic RPC retry.
            return _unavailable("VALIDATION_UNAVAILABLE")


class ValidationBudget(DurableObject):
    def __init__(self, ctx, env):
        self.ctx, self.env = ctx, env
        self.journal = None
        self.rate_limiter = None
        self.email_rate_limiter = None
        self._product_lock = asyncio.Lock()
        self._waiting = 0

    def _journal(self):
        if self.journal is None:
            preview_product = (getattr(self.env, "PULLWISE_MODE", "") == "preview"
                and str(getattr(self.env, "PULLWISE_PREVIEW_PRODUCT_ENABLED", "0")) == "1")
            self.journal = BudgetJournal(self.ctx.storage.sql, preview_product=preview_product,
                product_operations=preview_product)
        return self.journal

    def _email_admit(self, kind, email_subject, ip_subject):
        if (str(getattr(self.env, "PULLWISE_D1_ACCESS_ENABLED", "0")) != "1"
                or str(getattr(self.env, "PULLWISE_EMAIL_AUTH_ENABLED", "0")) != "1"):
            return {"ok": False, "error": "EMAIL_AUTH_NOT_CONFIGURED"}
        if self.email_rate_limiter is None:
            self.email_rate_limiter = EmailRateLimiter(self.ctx.storage.sql)
        self.email_rate_limiter.email(kind, email_subject64hex=email_subject,
            ip_subject64hex=ip_subject, now=time.time())
        return {"ok": True}

    async def admitEmail(self, kind, email_subject, ip_subject):
        """Binding-only admission for non-preview email auth; never accesses D1."""
        try:
            return self._email_admit(kind, email_subject, ip_subject)
        except EmailRateLimit as error:
            return {"ok": False, "retryAfter": error.retry_after}

    async def stop(self):
        journal = self._journal()
        journal.stop("MANUAL_STOP")
        return journal.snapshot()

    async def evidence(self):
        return self._journal().evidence_snapshot()

    async def initialize(self):
        # Only a Worker possessing the coordinator binding can invoke RPC.
        # No SQL, params, plan or reset option is supplied by the caller.
        if str(getattr(self.env, "PULLWISE_D1_ACCESS_ENABLED", "0")) != "1":
            return {"initialized": False, "error": "D1_ACCESS_PAUSED"}
        if getattr(self.env, "PULLWISE_MODE", "") != "preview":
            return {"initialized": False, "error": "VALIDATION_CONTROL_REQUIRED"}
        if REVIEWED_INITIALIZATION_PLAN is None:
            return {"initialized": False, "error": "UNREVIEWED_INITIALIZATION"}
        journal = self._journal()
        try:
            evidence = await run_initialization(NativeD1(self.env.DB), journal, REVIEWED_INITIALIZATION_PLAN)
            return {"initialized": True, "evidence": evidence}
        except BudgetError as error:
            return {"initialized": False, "error": str(error), "evidence": journal.snapshot()}
        except BaseException as error:
            journal.stop("INITIALIZATION_OUTCOME_UNKNOWN")
            if isinstance(error, asyncio.CancelledError):
                raise
            return {"initialized": False, "error": journal.snapshot()["stopped"],
                    "evidence": journal.snapshot()}

    async def fetch(self, request):
        if str(getattr(self.env, "PULLWISE_D1_ACCESS_ENABLED", "0")) != "1":
            return _unavailable("D1_ACCESS_PAUSED")
        if getattr(self.env, "PULLWISE_MODE", "") != "preview":
            return _unavailable("VALIDATION_CONTROL_REQUIRED")
        if str(getattr(self.env, "PULLWISE_PREVIEW_PRODUCT_ENABLED", "0")) == "1":
            return await self._product_fetch(request)
        path = urlsplit(request.url).path
        # Lazy CSV pulls outlive a Response. Keep this path closed until a
        # finite preview stream lifetime/cardinality plan is implemented.
        if path == "/api/v1/expenses/export":
            return _unavailable("STREAMING_CASE_UNBOUNDED")
        plan = next((item for item in REVIEWED_REMOTE_PLANS
                     if item.method == request.method and item.path == path), None)
        if plan is None:
            return _unavailable("UNREVIEWED_CASE")
        journal = self._journal()
        try:
            ticket = journal.begin(plan, now=time.time())
        except BudgetError as error:
            return _unavailable(str(error))
        try:
            binding = MeteredD1(NativeD1(self.env.DB), journal, ticket, plan)
            response = await asyncio.wait_for(
                _Application(self.env, binding).fetch(request), timeout=REQUEST_SECONDS)
            if response.status not in plan.expected_statuses:
                journal.stop("UNEXPECTED_RESPONSE")
            else:
                journal.finish(ticket, now=time.time())
            print(json.dumps({"validation_budget": journal.snapshot()}))
            return response
        except BaseException as error:
            journal.stop("TIMEOUT" if isinstance(error, asyncio.TimeoutError) else "REQUEST_OUTCOME_UNKNOWN")
            print(json.dumps({"validation_budget": journal.snapshot()}))
            if isinstance(error, asyncio.CancelledError):
                raise
            return _unavailable(journal.snapshot()["stopped"])

    async def runRecurring(self):
        """Binding-only RPC; no HTTP tick endpoint, reset or caller-supplied clock."""
        if not _recurring_enabled(self.env):
            return {"ok": False, "error": "RECURRING_DISABLED"}
        if self._waiting >= 16:
            return {"ok": False, "error": "VALIDATION_BUSY"}
        self._waiting += 1
        try:
            async with self._product_lock:
                journal = self._journal()
                state = journal.snapshot()
                # Scheduler never initializes or migrates user data. Publication
                # upgrades once through the reviewed ordinary preview path first.
                if not state.get("schema_ready") or state.get("schema_version") != 9:
                    return {"ok": False, "error": "SCHEMA_UPGRADE_REQUIRED"}
                ticket = binding = None
                try:
                    async def execute():
                        nonlocal ticket, binding
                        now = int(time.time())
                        ticket = journal.begin_product(now=time.time())
                        binding = ProductMeteredD1(NativeD1(self.env.DB), journal, ticket)
                        await binding.ensure_cardinality()
                        application = PlanLimitedD1(binding, now=now,
                            policy=parse_policy(getattr(self.env, "PULLWISE_PLAN_LIMITS_JSON", "")))
                        result = await run_due_recurring(binding=application,
                            maintenance_binding=binding, gateway=WorkerGitHubGateway(self.env),
                            now=now, rule_limit=10, occurrence_limit=10)
                        journal.finish(ticket, now=time.time())
                        return {"ok": True, **result}
                    return await asyncio.wait_for(execute(), timeout=REQUEST_SECONDS)
                except BudgetError as error:
                    if journal.snapshot()["active"] is not None:
                        journal.stop(str(error))
                    return {"ok": False, "error": str(error)}
                except BaseException as error:
                    if (not isinstance(error, asyncio.CancelledError)
                            and ticket is not None and binding is not None
                            and binding.accounted_outcome()):
                        journal.finish_accounted_product_failure(ticket, now=time.time(),
                            timeout=isinstance(error, asyncio.TimeoutError))
                        return {"ok": False, "error": "RECURRING_EXECUTION_UNAVAILABLE"}
                    journal.stop("TIMEOUT" if isinstance(error, asyncio.TimeoutError)
                                 else "REQUEST_OUTCOME_UNKNOWN")
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    return {"ok": False, "error": journal.snapshot()["stopped"]}
        finally:
            self._waiting -= 1

    async def _product_fetch(self, request):
        target, invalid_target = _request_target(request)
        if invalid_target is not None:
            return invalid_target
        path, _ = target
        if self.rate_limiter is None:
            self.rate_limiter = PreviewRateLimiter(self.ctx.storage.sql)
        try:
            self.rate_limiter.ingress(getattr(request, "headers", None), method=request.method,
                path=path, now=time.time())
        except PreviewRateLimit as error:
            return Response.json(error.response(), status=429,
                headers={"Cache-Control": "no-store", "Retry-After": str(error.retry_after)})
        if path == "/_preview/budget" and request.method == "GET":
            journal = self._journal()
            state = journal.snapshot()
            return Response.json({"productOperationMode": journal.product_operations,
                "limits": {"rowsRead": None if journal.product_operations else journal.read_ceiling,
                           "rowsWritten": None if journal.product_operations else WRITE_CEILING},
                "historicalCeilings": {"rowsRead": journal.read_ceiling, "rowsWritten": WRITE_CEILING},
                "reserved": {"rowsRead": state["reserved_read"], "rowsWritten": state["reserved_written"]},
                "observed": {"rowsRead": state["actual_read"], "rowsWritten": state["actual_written"]},
                "schemaReady": bool(state.get("schema_ready")),
                "schemaVersion": state.get("schema_version", 4 if state.get("schema_ready") else 0),
                "stateStorageVersion": state.get("state_storage_version", 0),
                "stopped": state["stopped"]},
                headers={"Cache-Control": "no-store"})
        if (request.method not in {"GET", "POST", "PATCH", "DELETE"} or
                not (path.startswith(("/api/v1/", "/api-keys", "/auth/", "/integrations"))
                       or path in {"/health", "/repositories", "/repositories/sync", "/billing", "/webhooks/creem"}
                     or path.startswith("/billing/"))):
            return Response.json({"error": {"code": "NOT_FOUND"}}, status=404)
        if self._waiting >= 16:
            return _unavailable("VALIDATION_BUSY")
        self._waiting += 1
        try:
            async with self._product_lock:
                journal = self._journal()
                ticket = binding = None
                try:
                    async def execute():
                        nonlocal ticket, binding
                        reconcile_schema_reads(journal)
                        native = NativeD1(self.env.DB)
                        if (str(getattr(self.env, "PULLWISE_PREVIEW_SCHEMA_UPGRADE_ENABLED", "0")) == "1"
                                and journal.snapshot().get("schema_ready")):
                            await upgrade_product_schema(native, journal)
                        if (str(getattr(self.env, "PULLWISE_PREVIEW_SCHEMA_V6_UPGRADE_ENABLED", "0")) == "1"
                                and journal.snapshot().get("schema_ready")):
                            await upgrade_product_schema_v6(native, journal)
                        if (str(getattr(self.env, "PULLWISE_PREVIEW_SCHEMA_V7_UPGRADE_ENABLED", "0")) == "1"
                                and journal.snapshot().get("schema_ready")):
                            await upgrade_product_schema_v7(native, journal)
                        if (str(getattr(self.env, "PULLWISE_PREVIEW_SCHEMA_V8_UPGRADE_ENABLED", "0")) == "1"
                                and journal.snapshot().get("schema_ready")):
                            await upgrade_product_schema_v8(native, journal)
                        if (str(getattr(self.env, "PULLWISE_PREVIEW_SCHEMA_V9_UPGRADE_ENABLED", "0")) == "1"
                                and journal.snapshot().get("schema_ready")):
                            await upgrade_product_schema_v9(native, journal)
                        await initialize_product(native, journal)
                        await migrate_product_state_records(native, journal)
                        ticket = journal.begin_product(now=time.time())
                        binding = ProductMeteredD1(native, journal, ticket, rate_limiter=self.rate_limiter,
                            rate_channel=request_channel(request.method, path))
                        await binding.ensure_cardinality()
                        application = _Application(self.env, binding)
                        ip_subject = _email_ip_subject(request)

                        async def admit_email(kind, email_subject):
                            result = self._email_admit(kind, email_subject, ip_subject)
                            if not result["ok"]:
                                raise RuntimeError("EMAIL_ADMISSION_UNAVAILABLE")
                            return True
                        application.email_admission = admit_email
                        response = await application.fetch(request)
                        if binding.rate_rejection is not None:
                            error = binding.rate_rejection
                            response = Response.json(error.response(), status=429,
                                headers={"Cache-Control": "no-store", "Retry-After": str(error.retry_after)})
                        # Product exports are consumed within _Application;
                        # their Response body no longer performs lazy D1 IO.
                        # Native D1 ambiguity already stops the journal. A
                        # provider/business HTTP error with complete D1 meta
                        # retains its reservation but may be retried manually.
                        journal.finish(ticket, now=time.time())
                        return response
                    return await asyncio.wait_for(execute(), timeout=REQUEST_SECONDS)
                except PreviewRateLimit as error:
                    if ticket is not None and binding is not None and binding.accounted_outcome():
                        journal.finish_accounted_product_failure(ticket, now=time.time())
                    else:
                        journal.stop("REQUEST_OUTCOME_UNKNOWN")
                    return Response.json(error.response(), status=429,
                        headers={"Cache-Control": "no-store", "Retry-After": str(error.retry_after)})
                except BudgetError as error:
                    if journal.snapshot()["active"] is not None:
                        journal.stop(str(error))
                    return _unavailable(str(error))
                except BaseException as error:
                    # wait_for awaits canceled application completion. A
                    # canceled native dispatch already sets its persistent
                    # unknown-outcome stop, and unfinished mutation refresh
                    # keeps cardinality_verified false. Isolate only a fully
                    # accounted provider/application failure in this request.
                    if (not isinstance(error, asyncio.CancelledError)
                            and ticket is not None and binding is not None
                            and binding.accounted_outcome()):
                        journal.finish_accounted_product_failure(ticket, now=time.time(),
                            timeout=isinstance(error, asyncio.TimeoutError))
                        return _unavailable("REQUEST_TIMEOUT" if isinstance(error, asyncio.TimeoutError)
                                            else "SERVER_UNAVAILABLE")
                    journal.stop("TIMEOUT" if isinstance(error, asyncio.TimeoutError)
                                 else "REQUEST_OUTCOME_UNKNOWN")
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    return _unavailable(journal.snapshot()["stopped"])
        finally:
            self._waiting -= 1
