"""Execute the real Worker application class with inert FFI/provider boundaries."""
import ast
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs, unquote

import pytest

from pullwise_server.cloudflare_plan_limits import PlanLimitError
from pullwise_server.cloudflare_creem_gateway import CreemRequestRejected
from pullwise_server.cloudflare_ledger_reports import CsvExport
from pullwise_server.json_input import validate_json_unicode


class Response:
    def __init__(self, payload=None, *, status=200, headers=None):
        self.payload, self.status, self.headers = payload, status, headers

    @classmethod
    def json(cls, payload, **options):
        return cls(payload, **options)


def application(*, same_site="Lax", catalog_failure=None, billing_failure=None,
                billing_gateway_failure=None):
    source = Path(__file__).resolve().parents[1] / "cloudflare/server/src/entry.py"
    tree = ast.parse(source.read_text())
    calls = []
    async def identity(**kwargs):
        return None
    async def ledger(**kwargs):
        calls.append("ledger")
        return 204, None
    async def catalog(**kwargs):
        raise catalog_failure
    async def billing(**kwargs):
        calls.append("billing")
        if billing_failure is not None:
            raise billing_failure
        return 200, {"accepted": True}
    def creem_gateway(env):
        if billing_gateway_failure is not None:
            raise billing_gateway_failure
        return None
    namespace = {"Response": Response, "time": time, "urlsplit": urlsplit,
        "parse_qs": parse_qs, "unquote": unquote, "json": json, "PlanLimitError": PlanLimitError,
        "handle_identity_request": identity, "handle_ledger_request": ledger,
        "read_or_refresh_catalog": catalog,
        "handle_billing_mutation": billing, "validate_json_unicode": validate_json_unicode,
        "CsvExport": CsvExport,
        "WorkerGitHubGateway": lambda _: None, "WorkerCreemGateway": creem_gateway}
    target = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_request_target")
    exec(compile(ast.Module(body=[target], type_ignores=[]), str(source), "exec"), namespace)
    node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "_Application")
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
    instance = namespace["_Application"].__new__(namespace["_Application"])
    instance.env = SimpleNamespace(PULLWISE_D1_ACCESS_ENABLED="1", PULLWISE_COOKIE_SAME_SITE=same_site,
        PULLWISE_APP_URL="https://app.example.test", PULLWISE_MODE="preview")
    instance.binding = SimpleNamespace(now=0)
    instance.jev_gateway = None
    return instance, calls


def request(path="/api/v1/expenses", *, method="POST", headers=None, raw=b"{}"):
    async def body():
        return raw
    return SimpleNamespace(url="https://api.example.test" + path, method=method,
        headers=headers or {}, bytes=body)


@pytest.mark.parametrize("same_site", ["Lax", "Strict", "None"])
@pytest.mark.parametrize("origin", [None, "https://hostile.example.test"])
@pytest.mark.parametrize("authorization", [None, "Basic invalid", "Bearer"])
@pytest.mark.parametrize("path,method", [
    ("/api/v1/expenses", "POST"),
    ("/api/v1/account/jev", "PATCH"),
    ("/api/v1/expense-recurring-rules", "POST"),
    ("/api/v1/expense-recurring-rules/rule_1", "PATCH"),
    ("/api/v1/expense-recurring-rules/rule_1", "DELETE"),
])
def test_cookie_writes_require_trusted_origin_for_every_samesite_mode(same_site, origin, authorization, path, method):
    app, calls = application(same_site=same_site)
    headers = {"cookie": "pw_session=synthetic"}
    if origin:
        headers["origin"] = origin
    if authorization:
        headers["authorization"] = authorization
    incoming = request(path, method=method, headers=headers)
    async def forbidden_body():
        raise AssertionError("Untrusted Cookie write read its body")
    incoming.bytes = forbidden_body
    response = asyncio.run(app.fetch(incoming))
    assert response.status == 403 and response.payload["error"]["code"] == "UNTRUSTED_ORIGIN"
    assert calls == []


@pytest.mark.parametrize("path,method", [
    ("/api/v1/expense-recurring-rules", "POST"),
    ("/api/v1/expense-recurring-rules/rule_1", "PATCH"),
    ("/api/v1/expense-recurring-rules/rule_1", "DELETE"),
])
def test_trusted_recurring_cookie_writes_reach_the_ledger_handler(path, method):
    app, calls = application()
    response = asyncio.run(app.fetch(request(path, method=method,
        headers={"cookie": "pw_session=synthetic", "origin": "https://app.example.test"})))
    assert response.status == 204 and calls == ["ledger"]
    assert response.headers["Cache-Control"] == "no-store"


def test_account_preference_cookie_write_rejects_too_many_sessions_before_body_or_handler():
    app, calls = application()
    incoming = request("/api/v1/account/jev", method="PATCH", headers={
        "cookie": "; ".join("pw_session=synthetic" + str(value) for value in range(9)),
        "origin": "https://app.example.test"})
    async def forbidden_body():
        raise AssertionError("Ambiguous credential write read its body")
    incoming.bytes = forbidden_body
    response = asyncio.run(app.fetch(incoming))
    assert response.status == 400 and response.payload == {"error": {"code": "AMBIGUOUS_AUTH"}}
    assert calls == []


@pytest.mark.parametrize("path,method", [
    ("/api/v1/expense-recurring-rules", "POST"),
    ("/api/v1/expense-recurring-rules/rule_1", "PATCH"),
])
@pytest.mark.parametrize("raw,status,code", [
    (b'{"schedule":', 422, "INVALID_INPUT"),
    (b'\xff', 422, "INVALID_INPUT"),
    (b'{"purpose":"\\ud800"}', 422, "INVALID_INPUT"),
    (b'{"schedule":{"\\udfff":true}}', 422, "INVALID_INPUT"),
    (b"x" * 8193, 413, "REQUEST_TOO_LARGE"),
])
def test_recurring_body_safety_fails_before_business_dispatch(path, method, raw, status, code):
    app, calls = application()
    response = asyncio.run(app.fetch(request(path, method=method, raw=raw,
        headers={"cookie": "pw_session=synthetic", "origin": "https://app.example.test"})))
    assert response.status == status and response.payload == {"error": {"code": code}}
    assert response.headers["Cache-Control"] == "no-store"
    assert calls == []


def test_trusted_cookie_write_and_external_key_write_remain_supported():
    app, calls = application()
    assert asyncio.run(app.fetch(request(headers={"cookie": "pw_session=synthetic",
        "origin": "https://app.example.test"}))).status == 204
    assert asyncio.run(app.fetch(request(headers={"authorization": "Bearer pwk_synthetic"}))).status == 204
    assert calls == ["ledger", "ledger"]


def test_preview_catalog_diagnostics_never_return_provider_exception_text():
    class JsException(Exception):
        pass
    app, _ = application(catalog_failure=JsException("provider body includes unregistered_secret_value"))
    response = asyncio.run(app.fetch(request("/billing/plan", method="GET")))
    assert response.status == 503
    assert "unregistered_secret_value" not in json.dumps(response.payload)
    assert "transportDiagnostic" not in response.payload["error"]


@pytest.mark.parametrize("error", [
    CreemRequestRejected("provider rejected secret_provider_payload"),
    ValueError("unknown provider outcome secret_provider_payload"),
    TimeoutError("lost provider acknowledgement secret_provider_payload"),
    UnicodeError("invalid provider response secret_provider_payload"),
])
def test_billing_provider_failures_are_service_errors_without_exposing_provider_payloads(error):
    app, calls = application(billing_failure=error)
    response = asyncio.run(app.fetch(request("/billing/change-interval", raw=b'{"plan":"max","interval":"year"}')))
    assert response.status == 503 and response.payload == {"error": {"code": "BILLING_UNAVAILABLE"}}
    assert response.headers["Cache-Control"] == "no-store"
    assert "secret_provider_payload" not in json.dumps(response.payload)
    assert calls == ["billing"]


def test_billing_gateway_configuration_failure_is_a_service_error():
    app, calls = application(billing_gateway_failure=ValueError("provider origin invalid"))
    response = asyncio.run(app.fetch(request("/billing/checkout-sessions")))
    assert response.status == 503 and response.payload == {"error": {"code": "BILLING_UNAVAILABLE"}}
    assert calls == []


@pytest.mark.parametrize("raw", [b'{"plan":', b'\xff', b'{"plan":"\\ud800"}'])
def test_malformed_billing_input_remains_a_request_error_before_provider_dispatch(raw):
    app, calls = application(billing_failure=AssertionError("provider must not be called"))
    response = asyncio.run(app.fetch(request("/billing/checkout-sessions", raw=raw)))
    assert response.status == 422 and response.payload == {"error": {"code": "INVALID_REQUEST"}}
    assert calls == []


@pytest.mark.parametrize("path,status,code", [
    ("/api/v1/expenses?cursor=" + "x" * 8193, 413, "REQUEST_TOO_LARGE"),
    ("/api/v1/expenses/" + "x" * 8193, 413, "REQUEST_TOO_LARGE"),
    ("/api/v1/expenses/" + "%61" * 3000, 413, "REQUEST_TOO_LARGE"),
    ("/api/v1/expenses?cursor=%ED%A0%80", 422, "INVALID_INPUT"),
    ("/api/v1/expenses?cursor=%00", 422, "INVALID_INPUT"),
    ("/api/v1/expenses/%00", 422, "INVALID_INPUT"),
    ("/api/v1/expenses?" + "&".join("x=" for _ in range(101)), 422, "INVALID_INPUT"),
])
def test_invalid_url_inputs_are_rejected_before_application_dispatch(path, status, code):
    app, calls = application()
    response = asyncio.run(app.fetch(request(path, method="GET")))
    assert response.status == status and response.payload == {"error": {"code": code}}
    assert calls == []


def test_valid_encoded_emoji_query_reaches_application():
    app, calls = application()
    response = asyncio.run(app.fetch(request("/api/v1/expenses?cursor=%F0%9F%98%80", method="GET")))
    assert response.status == 204 and calls == ["ledger"]


@pytest.mark.parametrize("path,headers", [
    ("/api/v1/expenses/export?workspaceId=owner&workspaceId=other", {}),
    ("/api/v1/expenses/export?workspaceId=owner", {"x-pullwise-workspace": "other"}),
    ("/api/v1/me?workspaceId=", {}),
])
def test_conflicting_ledger_selectors_fail_before_authentication(path, headers):
    app, calls = application()
    response = asyncio.run(app.fetch(request(path, method="GET", headers=headers)))
    assert response.status == 422 and response.payload["error"]["code"] == "INVALID_INPUT"
    assert calls == []


@pytest.mark.parametrize("path", ["/api/v1/workspaces/owner/invites", "/api/v1/workspaces/owner/members/editor", "/api/v1/workspace-invitations/accept"])
def test_workspace_cookie_operations_require_trusted_origin_before_body_or_database(path):
    app, calls = application()
    response = asyncio.run(app.fetch(request(path, headers={"cookie": "pw_session=synthetic", "origin": "https://hostile.test"})))
    assert response.status == 403 and response.payload["error"]["code"] == "UNTRUSTED_ORIGIN"
    assert calls == []


@pytest.mark.parametrize("path,method", [
    ("/api/v1/workspace-invitation-requests", "GET"),
    ("/api/v1/workspaces/owner/invites/link/requests", "GET"),
    ("/api/v1/workspaces/owner/join-requests", "GET"),
    ("/api/v1/workspaces/owner/invites/link/requests/request/approve", "POST"),
    ("/api/v1/workspaces/owner/invites/link/requests/request/reject", "POST"),
])
def test_inviter_inbox_and_approval_routes_reach_the_ledger_handler(path, method):
    app, calls = application()
    response = asyncio.run(app.fetch(request(path, method=method,
        headers={"cookie": "pw_session=synthetic", "origin": "https://app.example.test"})))
    assert response.status == 204 and calls == ["ledger"]
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_invitation_reviews_reject_untrusted_cookie_origin_before_database(decision):
    app, calls = application()
    response = asyncio.run(app.fetch(request(
        "/api/v1/workspaces/owner/invites/link/requests/request/" + decision,
        headers={"cookie": "pw_session=synthetic", "origin": "https://hostile.test"})))
    assert response.status == 403 and response.payload["error"]["code"] == "UNTRUSTED_ORIGIN"
    assert calls == []
