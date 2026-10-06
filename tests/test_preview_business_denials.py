"""Ordinary user conflicts and commercial limits keep preview available.

The wrapper stack and SQL are real; native row metadata is synthetic.
"""
import asyncio

import pytest

from test_cloudflare_github_identity_http import GitHubStub
from test_jev_preview_admission import MeteredSQLite, preview
from test_jev_preview_payload import session
from test_jev_preview_unicode import wire_request
from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1
from pullwise_server.cloudflare_preview_budget import ProductMeteredD1


def call(client, method, path, body, *, revision=None):
    class Repositories(GitHubStub):
        async def repositories(self, token, installation_id):
            rows = await super().repositories(token, installation_id)
            return [*rows, {**rows[0], "id": 303, "full_name": "alice/second"}]
    async def run():
        ticket = client.journal.begin_product(now=10)
        meter = ProductMeteredD1(MeteredSQLite(client.preview.fixture.store),
            client.journal, ticket, clock=lambda: 11)
        await meter.refresh()
        binding = PlanLimitedD1(meter, policy=client.preview.policy, now=client.preview.now)
        headers = dict(client.preview.fixture.headers)
        if revision is not None:
            headers["If-Match"] = f'"{revision}"'
        result = await handle_ledger_request(binding=binding, gateway=Repositories(),
            method=method, path=path, params={}, body=body, now=client.preview.now,
            headers=headers)
        client.journal.finish(ticket, now=12)
        return result
    result = asyncio.run(run())
    assert client.journal.snapshot()["stopped"] is None
    return result


def test_duplicate_category_creation_and_rename_do_not_stop_preview(preview):
    client = session(preview)
    assert call(client, "POST", "/api/v1/categories", {"name": "hosting"}) == (
        409, {"error": {"code": "CATEGORY_CONFLICT"}})
    status, category = call(client, "POST", "/api/v1/categories", {"name": "Tools"})
    assert status == 201
    assert call(client, "PATCH", "/api/v1/categories/" + category["id"],
        {"name": "HOSTING"}, revision=1) == (409, {"error": {"code": "CATEGORY_CONFLICT"}})
    status, renamed = call(client, "PATCH", "/api/v1/categories/" + category["id"],
        {"name": "Subscriptions"}, revision=1)
    assert status == 200 and renamed["revision"] == 2


def test_duplicate_project_does_not_stop_preview_or_consume_another_slot(preview):
    client = session(preview)
    assert call(client, "POST", "/api/v1/projects", {"githubRepoId": 202})[0] == 201
    assert call(client, "POST", "/api/v1/projects", {"githubRepoId": 202}) == (
        409, {"error": {"code": "PROJECT_CONFLICT"}})
    assert call(client, "POST", "/api/v1/categories", {"name": "Tools"})[0] == 201
    with preview.fixture.store.connect() as db:
        assert tuple(db.execute("SELECT projects,writes FROM ledger_plan_usage").fetchone()) == (1, 2)


@pytest.mark.parametrize("allowance,code", [("projects", "PROJECT_LIMIT"),
    ("writesPerMinute", "WRITE_RATE_LIMIT"), ("writesPerMonth", "MONTHLY_WRITE_LIMIT")])
def test_plan_limit_returns_a_business_error_without_stopping_preview(preview, allowance, code):
    preview.policy["max"][allowance] = 1
    client = session(preview)
    assert call(client, "POST", "/api/v1/projects", {"githubRepoId": 202})[0] == 201
    path, body = ("/api/v1/projects", {"githubRepoId": 303}) if allowance == "projects" else (
        "/api/v1/categories", {"name": "Tools"})
    status, response = call(client, "POST", path, body)
    assert status == (403 if allowance == "projects" else 429)
    assert response == {"error": {"code": code}}
    assert call(client, "GET", "/api/v1/categories", None)[0] == 200


@pytest.mark.parametrize("body", [
    {"expiresAt": 9007199254740992}, {"expiresInSeconds": 9007199254740991},
    {"expiresAt": "invalid"}, {"expiresAt": "9" * 5000},
    {"expiresAt": True}, {"expiresAt": 1800003600.5},
    {"expiresInSeconds": False},
])
def test_invalid_key_expiry_never_reaches_native_parameters(preview, body):
    client = session(preview)
    assert wire_request(client, body, path="/api-keys") == (
        400, {"error": {"code": "INVALID_REQUEST"}})
    assert client.journal.snapshot()["stopped"] is None
    with preview.fixture.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 0


@pytest.mark.parametrize("kind", [[], {}, None])
def test_invalid_expense_target_kind_is_a_validation_error(preview, kind):
    from test_ledger_automatic_assistance import expense
    client = session(preview)
    assert wire_request(client, expense("cat_host", target={"kind": kind})) == (
        422, {"error": {"code": "INVALID_INPUT"}})
    assert client.journal.snapshot()["stopped"] is None


@pytest.mark.parametrize("status", [[], {}, None])
def test_invalid_project_status_is_a_validation_error(preview, status):
    client = session(preview)
    assert call(client, "PATCH", "/api/v1/projects/prj_missing", {"status": status}, revision=1) == (
        422, {"error": {"code": "INVALID_INPUT"}})


def test_unsafe_repository_id_is_rejected_before_github_or_d1(preview):
    client = session(preview)
    assert call(client, "POST", "/api/v1/projects", {"githubRepoId": 9007199254740992}) == (
        422, {"error": {"code": "INVALID_INPUT"}})
