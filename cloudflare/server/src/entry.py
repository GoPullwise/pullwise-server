"""Candidate Server Worker entry; no probe endpoints or scheduled trigger."""
import asyncio
import json
import time
from urllib.parse import urlsplit, parse_qs

from workers import DurableObject, Response, WorkerEntrypoint

from pullwise_server.cloudflare_validation_budget import (
    BUDGET_SCOPE, REQUEST_SECONDS, BudgetError, BudgetJournal, MeteredD1,
    REVIEWED_REMOTE_PLANS,
)

from pullwise_server.cloudflare_http_contract import handle_http_request
from pullwise_server.cloudflare_github_identity_http import handle_identity_request
from pullwise_server.cloudflare_github_gateway import WorkerGitHubGateway
from pullwise_server.cloudflare_billing_mutations import handle_billing_mutation
from pullwise_server.cloudflare_billing_catalog_refresh import read_or_refresh_catalog
from pullwise_server.cloudflare_creem_gateway import WorkerCreemGateway, product_bindings, webhook_product_ids
from pullwise_server.cloudflare_ledger_profile import read_ledger_me
from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_jev_gateway import WorkerJevGateway
from pullwise_server.cloudflare_ledger_reports import CsvExport
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1, PlanLimitError
from pullwise_server.ledger_plan_policy import parse_policy


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
        self.binding = PlanLimitedD1(binding,
            policy=parse_policy(getattr(env, "PULLWISE_PLAN_LIMITS_JSON", None)), now=int(time.time()))
        self.jev_gateway = WorkerJevGateway(env)
        self.binding.jev_available = self.jev_gateway.enabled

    async def fetch(self, request):
        if str(getattr(self.env, "PULLWISE_D1_ACCESS_ENABLED", "0")) != "1":
            return Response.json({"error": {"code": "D1_ACCESS_PAUSED"}}, status=503,
                                 headers={"Cache-Control": "no-store"})
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
            "X-Request-Id": request.headers.get("x-request-id") or "",
            "If-Match": request.headers.get("if-match") or "",
            "Idempotency-Key": request.headers.get("idempotency-key") or "",
            "Origin": request.headers.get("origin") or "",
            "Referer": request.headers.get("referer") or "",
        }
        path = urlsplit(request.url).path
        now = int(time.time())
        self.binding.now = now
        trusted_origins = {value.strip() for value in (
            getattr(self.env, "PULLWISE_ALLOWED_ORIGINS", "") + "," +
            getattr(self.env, "PULLWISE_APP_URL", "")).split(",")
            if value.strip() and value.strip() != "*"}
        try:
            identity = await handle_identity_request(
                method=request.method, path=path, params=parse_qs(urlsplit(request.url).query),
                headers=headers, binding=self.binding,
                gateway=WorkerGitHubGateway(self.env), now=now,
                app_url=getattr(self.env, "PULLWISE_APP_URL", ""),
                callback_url=getattr(self.env, "PULLWISE_GITHUB_CALLBACK_URL", ""),
                cookie_same_site=getattr(self.env, "PULLWISE_COOKIE_SAME_SITE", "Lax"),
                trusted_origins=trusted_origins,
            )
        except Exception:
            return Response.json({"error": {"code": "IDENTITY_UNAVAILABLE"}}, status=503,
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
                            "/api/v1/expense-suggestions")):
            if request.method in {"POST", "PATCH", "DELETE"} and (
                    getattr(self.env, "PULLWISE_COOKIE_SAME_SITE", "Lax").casefold() == "none"
                    and headers["Cookie"] and not headers["Authorization"]
                    and not headers["X-Pullwise-Api-Key"]):
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
                result = await handle_ledger_request(
                    binding=self.binding, gateway=WorkerGitHubGateway(self.env),
                    method=request.method, path=path,
                    headers=headers, params=parse_qs(urlsplit(request.url).query),
                    body=body, now=now, suggestion_gateway=self.jev_gateway)
                status, payload = result if result is not None else (404, {"error": {"code": "NOT_FOUND"}})
            except PlanLimitError as error:
                status, payload = error.response()
            except (ValueError, UnicodeError):
                status, payload = 422, {"error": {"code": "INVALID_INPUT"}}
            except Exception:
                status, payload = 503, {"error": {"code": "SERVER_UNAVAILABLE"}}
            response_headers = {"Cache-Control": "no-store",
                "Vary": "Cookie, Authorization, X-Pullwise-Api-Key"}
            if status == 204:
                return Response(None, status=status, headers=response_headers)
            if isinstance(payload, CsvExport):
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
            except Exception:
                status, payload = 503, {"error": {"code": "BILLING_CATALOG_UNAVAILABLE"}}
            return Response.json(payload, status=status, headers={"Cache-Control": "no-store"})
        if path in {"/billing/checkout-sessions", "/billing/change-interval",
                    "/billing/cancel-subscription", "/billing/resume-subscription"}:
            try:
                raw = await read_body()
                if len(raw) > 8192:
                    return Response.json({"error": {"code": "REQUEST_TOO_LARGE"}}, status=413)
                body = json.loads(raw)
                status, payload = await handle_billing_mutation(
                    binding=self.binding, gateway=WorkerCreemGateway(self.env),
                    now=now, method=request.method, path=path, headers=headers, body=body,
                    app_url=getattr(self.env, "PULLWISE_APP_URL", ""),
                    trusted_origins=trusted_origins, products=products)
            except (ValueError, UnicodeError):
                status, payload = 422, {"error": {"code": "INVALID_REQUEST"}}
            except Exception:
                status, payload = 503, {"error": {"code": "BILLING_UNAVAILABLE"}}
            return Response.json(payload, status=status, headers={"Cache-Control": "no-store"})
        status, payload = await handle_http_request(
            method=request.method,
            path=path,
            params=parse_qs(urlsplit(request.url).query),
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


def _unavailable(code):
    return Response.json({"error": {"code": code}}, status=503,
                         headers={"Cache-Control": "no-store"})


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if str(getattr(self.env, "PULLWISE_D1_ACCESS_ENABLED", "0")) != "1":
            return _unavailable("D1_ACCESS_PAUSED")
        mode = getattr(self.env, "PULLWISE_MODE", "")
        if mode == "local":
            # Only the explicitly local-only config has this mode. Offline
            # config checks reject it in every remote deployment config.
            try:
                return await _Application(self.env, self.env.DB).fetch(request)
            except ValueError:
                return _unavailable("PLAN_POLICY_INVALID")
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

    def _journal(self):
        if self.journal is None:
            self.journal = BudgetJournal(self.ctx.storage.sql)
        return self.journal

    async def stop(self):
        journal = self._journal()
        journal.stop("MANUAL_STOP")
        return journal.snapshot()

    async def evidence(self):
        return self._journal().snapshot()

    async def fetch(self, request):
        if str(getattr(self.env, "PULLWISE_D1_ACCESS_ENABLED", "0")) != "1":
            return _unavailable("D1_ACCESS_PAUSED")
        if getattr(self.env, "PULLWISE_MODE", "") != "preview":
            return _unavailable("VALIDATION_CONTROL_REQUIRED")
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
            binding = MeteredD1(self.env.DB, journal, ticket, plan)
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
