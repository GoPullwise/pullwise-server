"""Malformed public IDs and numeric parameters stop before D1/provider work."""
import pytest

from test_project_repositories import ledger
from pullwise_server.cloudflare_ledger_api import _revision


@pytest.mark.parametrize("path", ["/api/v1/projects", "/api/v1/expenses"])
@pytest.mark.parametrize("params", [
    {"limit": "²"}, {"limit": "１２"}, {"limit": "9" * 5000},
    {"limit": "0"}, {"limit": "101"}, {"cursor": "x" * 121},
    {"cursor": "x" * 8193}, {"cursor": "prj_🚀"}, {"cursor": "prj_\x00"},
])
def test_invalid_pagination_never_reaches_d1_or_github(ledger, path, params):
    async def unexpected(_statements):
        pytest.fail("Malformed pagination reached D1")
    ledger.binding.batch = unexpected
    assert ledger.call("GET", path, params=params) == (422, {"error": {"code": "INVALID_INPUT"}})
    assert ledger.gateway.calls == []


@pytest.mark.parametrize("path", ["/api/v1/expenses", "/api/v1/expenses/export",
    "/api/v1/reports/summary", "/api/v1/reports/timeseries", "/api/v1/reports/categories"])
@pytest.mark.parametrize("params", [{"projectId": "prj_" + "x" * 8193},
    {"categoryId": "cat_" + "x" * 8193}, {"projectId": "prj_\x00"},
    {"categoryId": "cat_🚀"}])
def test_invalid_report_filters_never_reach_d1_or_github(ledger, path, params):
    async def unexpected(_statements):
        pytest.fail("Malformed filter reached D1")
    ledger.binding.batch = unexpected
    assert ledger.call("GET", path, params=params) == (422, {"error": {"code": "INVALID_INPUT"}})
    assert ledger.gateway.calls == []


@pytest.mark.parametrize("kind", ["projects", "categories", "expenses"])
@pytest.mark.parametrize("identifier", ["x" * 8193, "item_\x00", "item_🚀"])
def test_invalid_path_ids_never_reach_d1(ledger, kind, identifier):
    async def unexpected(_statements):
        pytest.fail("Malformed path ID reached D1")
    ledger.binding.batch = unexpected
    assert ledger.call("GET", f"/api/v1/{kind}/{identifier}")[0] == 404
    assert ledger.gateway.calls == []


@pytest.mark.parametrize("field,value", [("target", {"kind": "project", "projectId": "x" * 8193}),
    ("categoryId", "x" * 8193), ("categoryId", "cat_\x00")])
def test_invalid_expense_resource_ids_never_reach_d1(ledger, field, value):
    body = {"target": {"kind": "shared"}, "categoryId": "cat_valid",
        "amount": "1.00", "currency": "USD", "occurredOn": "2026-10-06", "purpose": "QA"}
    async def unexpected(_statements):
        pytest.fail("Malformed expense ID reached D1")
    ledger.binding.batch = unexpected
    assert ledger.call("POST", "/api/v1/expenses", {**body, field: value},
        {**ledger.headers, "Idempotency-Key": "input-bound"})[0] == 422
    assert ledger.gateway.calls == []


@pytest.mark.parametrize("identifier", ["prj_\x00", "prj_🚀", "prj_" + "x" * 8193])
def test_invalid_suggestion_project_ids_never_reach_d1_or_model(ledger, identifier):
    async def unexpected(_statements):
        pytest.fail("Malformed model draft ID reached D1")
    ledger.binding.batch = unexpected
    assert ledger.call("POST", "/api/v1/expense-suggestions",
        {"purpose": "QA", "target": {"kind": "project", "projectId": identifier}})[0] == 422
    assert ledger.gateway.calls == []


@pytest.mark.parametrize("identifier", ["cat_\x00", "cat_🚀", "cat_" + "x" * 8193])
def test_invalid_suggestion_category_ids_never_reach_d1(ledger, identifier):
    async def unexpected(_statements):
        pytest.fail("Malformed model decision ID reached D1")
    ledger.binding.batch = unexpected
    assert ledger.call("POST", "/api/v1/expense-suggestions/sg_" + "a" * 32 + "/decision",
        {"categoryId": identifier, "target": {"kind": "shared"}})[0] == 422


@pytest.mark.parametrize("value", ['"' + "9" * 5000 + '"', '"9007199254740992"', '"²"'])
def test_revision_rejects_unbounded_or_unsafe_numbers(value):
    assert _revision({"If-Match": value}) == -1


def test_revision_accepts_the_last_exact_javascript_integer():
    assert _revision({"If-Match": '"9007199254740991"'}) == 9007199254740991
