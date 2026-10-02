"""Execute the real Worker application class with inert FFI/provider boundaries."""
import ast
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs

import pytest

from pullwise_server.cloudflare_plan_limits import PlanLimitError


class Response:
    def __init__(self, payload=None, *, status=200, headers=None):
        self.payload, self.status, self.headers = payload, status, headers

    @classmethod
    def json(cls, payload, **options):
        return cls(payload, **options)


def application(*, same_site="Lax", catalog_failure=None):
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
    namespace = {"Response": Response, "time": time, "urlsplit": urlsplit,
        "parse_qs": parse_qs, "json": json, "PlanLimitError": PlanLimitError,
        "handle_identity_request": identity, "handle_ledger_request": ledger,
        "read_or_refresh_catalog": catalog,
        "WorkerGitHubGateway": lambda _: None, "WorkerCreemGateway": lambda _: None}
    node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "_Application")
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
    instance = namespace["_Application"].__new__(namespace["_Application"])
    instance.env = SimpleNamespace(PULLWISE_D1_ACCESS_ENABLED="1", PULLWISE_COOKIE_SAME_SITE=same_site,
        PULLWISE_APP_URL="https://app.example.test", PULLWISE_MODE="preview")
    instance.binding = SimpleNamespace(now=0)
    instance.jev_gateway = None
    return instance, calls


def request(path="/api/v1/expenses", *, method="POST", headers=None):
    async def body():
        return b"{}"
    return SimpleNamespace(url="https://api.example.test" + path, method=method,
        headers=headers or {}, bytes=body)


@pytest.mark.parametrize("same_site", ["Lax", "Strict", "None"])
@pytest.mark.parametrize("origin", [None, "https://hostile.example.test"])
@pytest.mark.parametrize("authorization", [None, "Basic invalid", "Bearer"])
def test_cookie_writes_require_trusted_origin_for_every_samesite_mode(same_site, origin, authorization):
    app, calls = application(same_site=same_site)
    headers = {"cookie": "pw_session=synthetic"}
    if origin:
        headers["origin"] = origin
    if authorization:
        headers["authorization"] = authorization
    response = asyncio.run(app.fetch(request(headers=headers)))
    assert response.status == 403 and response.payload["error"]["code"] == "UNTRUSTED_ORIGIN"
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
