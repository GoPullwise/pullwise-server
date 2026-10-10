"""Explicit saved-record checks remain advisory, scoped and budgeted."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

import test_ledger_automatic_assistance as automatic
from test_ledger_automatic_edit import financial_rows, set_plan, setup_expense
from pullwise_server.cloudflare_api_key_write import create_api_key
from pullwise_server.ledger_plan_policy import JEV_RESERVATION_MICROUSD

ledger = automatic.ledger


def review(ledger, saved, provider=None, *, body=None, revision='"1"', headers=None, method="POST"):
    merged = {**(headers or ledger.headers), **({"If-Match": revision} if revision is not None else {})}
    return automatic.call(ledger, {} if body is None else body, provider, method=method,
        path=f"/api/v1/expenses/{saved['id']}/review", headers=merged)


def usage(ledger):
    with ledger.store.connect() as db:
        return dict(db.execute("SELECT * FROM ledger_plan_usage").fetchone())


def duplicate(ledger, body, **changes):
    status, saved = automatic.call(ledger, {**body, "purpose": body["purpose"].upper(),
        "note": "PRIVATE OTHER RECORD NOTE", **changes}, key="duplicate")
    assert status == 201
    return saved


@pytest.mark.parametrize("plan", ["pro", "max"])
@pytest.mark.parametrize("target", ["shared", "project"])
def test_saved_review_has_per_dimension_results_without_financial_or_commercial_write(ledger, plan, target):
    set_plan(ledger, plan)
    body, saved, chosen = setup_expense(ledger, target)
    other = duplicate(ledger, body)
    before, before_usage = financial_rows(ledger), usage(ledger)
    provider = automatic.Provider()
    status, result = review(ledger, saved, provider)
    assert status == 200
    assert result["expenseId"] == saved["id"] and result["revision"] == 1
    checks = result["checks"]
    assert checks["category"] == {"status": "issue", "current": saved["categoryId"],
        "suggested": chosen, "confidence": 0.95}
    assert checks["target"] == {"status": "issue" if target == "project" else "checked",
        "current": saved["target"], "suggested": {"kind": "shared"}, "confidence": 0.95}
    assert checks["duplicate"] == {"status": "issue", "candidate": {"id": other["id"], "revision": 1}}
    assert result["questionVersion"] == "ledger-suggest-v2" and result["modelVersion"] == "jev-1.13.0"
    assert set(result) == {"expenseId", "revision", "questionVersion", "modelVersion", "suggestionId", "checks"}
    assert len(provider.calls) == 1
    request = provider.calls[0]
    assert request["state"] == {"purpose": saved["purpose"], "note": saved["note"]}
    assert "PRIVATE OTHER RECORD NOTE" not in json.dumps(request)
    assert other["purpose"] not in json.dumps(request)
    assert financial_rows(ledger) == before
    after_usage = usage(ledger)
    for key in ("projects", "records", "writes", "minute_writes"):
        assert after_usage[key] == before_usage[key]
    assert after_usage["jev_reserved_microusd"] == JEV_RESERVATION_MICROUSD
    with ledger.store.connect() as db:
        assert db.execute("SELECT count(*) FROM expense_suggestion_budget").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM expense_suggestion_events").fetchone()[0] == 1
        assert db.execute("SELECT recorded_expense_id FROM expense_suggestion_events").fetchone()[0] == saved["id"]
        assert db.execute("SELECT count(*) FROM d1_command_guard").fetchone()[0] == 0
    assert automatic.call(ledger, method="GET", path=f"/api/v1/expenses/{saved['id']}")[1] == {
        key: value for key, value in saved.items() if key != "assistance"}


def test_saved_shared_review_is_attributed_and_erased_after_source_moves_into_project(ledger):
    body, saved, _ = setup_expense(ledger)
    status, checked = review(ledger, saved, automatic.Provider())
    assert status == 200
    status, project = automatic.call(ledger, {"name": "Erase current attribution"}, path="/api/v1/projects")
    assert status == 201
    status, moved = automatic.call(ledger, {**body,
        "target": {"kind": "project", "projectId": project["id"]}}, method="PATCH",
        path=f"/api/v1/expenses/{saved['id']}", headers={**ledger.headers, "If-Match": '"1"'})
    assert status == 200 and moved["revision"] == 2
    with ledger.store.connect() as db:
        row = db.execute("SELECT draft_target_kind,recorded_expense_id FROM expense_suggestion_events WHERE id=?",
            (checked["suggestionId"],)).fetchone()
        assert tuple(row) == ("shared", saved["id"])
        reserved = db.execute("SELECT jev_reserved_microusd FROM ledger_plan_usage").fetchone()[0]
    assert automatic.call(ledger, method="DELETE", path=f"/api/v1/projects/{project['id']}",
        headers={**ledger.headers, "If-Match": '"1"'}) == (204, None)
    with ledger.store.connect() as db:
        assert db.execute("SELECT count(*) FROM expense_suggestion_events WHERE id=?",
            (checked["suggestionId"],)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM expenses WHERE id=?", (saved["id"],)).fetchone()[0] == 0
        assert db.execute("SELECT jev_reserved_microusd FROM ledger_plan_usage").fetchone()[0] == reserved


@pytest.mark.parametrize("provider", [None, automatic.Provider(failure=TimeoutError("synthetic transport")),
    automatic.Provider(confidence=0.6)])
def test_disabled_timeout_or_uncertain_keeps_independent_local_duplicate_result(ledger, provider):
    body, saved, _ = setup_expense(ledger)
    other = duplicate(ledger, body)
    before = financial_rows(ledger)
    status, result = review(ledger, saved, provider)
    assert status == 200
    assert result["checks"]["duplicate"] == {"status": "issue", "candidate": {"id": other["id"], "revision": 1}}
    for name in ("category", "target"):
        check = result["checks"][name]
        assert check["status"] == ("uncertain" if provider and not provider.failure else "unavailable")
        if provider and not provider.failure:
            assert check["confidence"] == 0.6 and "suggested" not in check
        else:
            assert check["reason"] == ("provider_unavailable" if provider else "disabled")
    assert financial_rows(ledger) == before
    assert usage(ledger)["jev_reserved_microusd"] == (JEV_RESERVATION_MICROUSD if provider else 0)


def test_no_active_categories_still_checks_target_and_local_duplicate(ledger):
    body, saved, _ = setup_expense(ledger, "project")
    with ledger.store.connect() as db:
        db.execute("UPDATE expense_categories SET archived_at='local-archive'")
    before = financial_rows(ledger)
    provider = automatic.Provider()
    status, result = review(ledger, saved, provider)
    assert status == 200 and len(provider.calls) == 1
    assert set(provider.calls[0]["questions"]) == {"target"}
    assert result["checks"]["category"] == {"status": "unavailable", "current": saved["categoryId"], "reason": "no_categories"}
    assert result["checks"]["target"]["status"] == "issue"
    assert result["checks"]["duplicate"] == {"status": "checked"}
    assert financial_rows(ledger) == before


@pytest.mark.parametrize("change", [
    {"amount": "13.50"}, {"currency": "EUR"}, {"occurredOn": "2026-09-26"},
    {"occurredOn": "2026-09-28"}, {"target": {"kind": "shared"}},
])
def test_local_duplicate_requires_same_target_money_and_exact_date(ledger, change):
    body, saved, _ = setup_expense(ledger, "project")
    duplicate(ledger, body, **change)
    status, result = review(ledger, saved)
    assert status == 200 and result["checks"]["duplicate"] == {"status": "checked"}


def test_local_duplicate_matches_same_day_money_even_with_a_different_purpose(ledger):
    body, saved, _ = setup_expense(ledger)
    other = duplicate(ledger, body, purpose="A differently described purchase")
    status, result = review(ledger, saved)
    assert status == 200
    assert result["checks"]["duplicate"] == {
        "status": "issue", "candidate": {"id": other["id"], "revision": other["revision"]}}


@pytest.mark.parametrize("revision,status", [(None, 428), ('"invalid"', 422), ('"0"', 422), ('"2"', 412)])
def test_review_validates_revision_before_model_or_budget(ledger, revision, status):
    _, saved, _ = setup_expense(ledger)
    provider = automatic.Provider()
    before = financial_rows(ledger)
    assert review(ledger, saved, provider, revision=revision)[0] == status
    assert not provider.calls and financial_rows(ledger) == before
    with ledger.store.connect() as db:
        assert db.execute("SELECT count(*) FROM expense_suggestion_budget").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM expense_suggestion_events").fetchone()[0] == 0


@pytest.mark.parametrize("plan,maximum", [("pro", 3_000_000), ("max", 5_000_000)])
def test_review_reuses_existing_paid_monthly_budget(ledger, plan, maximum):
    set_plan(ledger, plan)
    _, saved, _ = setup_expense(ledger)
    with ledger.store.connect() as db:
        db.execute("UPDATE ledger_plan_usage SET jev_reserved_microusd=?", (maximum,))
    provider = automatic.Provider()
    before = financial_rows(ledger)
    status, result = review(ledger, saved, provider)
    assert status == 429
    assert result["error"]["code"] == "JEV_BUDGET_LIMIT"
    assert not provider.calls and financial_rows(ledger) == before


def test_review_ignores_historical_daily_attempts_without_resetting_them(ledger):
    _, saved, _ = setup_expense(ledger)
    day = datetime.fromtimestamp(ledger.now + 3, timezone.utc).date().isoformat()
    with ledger.store.connect() as db:
        db.execute("INSERT INTO expense_suggestion_budget VALUES(?,?,20)", ("usr_github_77", day))
    before, before_usage = financial_rows(ledger), usage(ledger)
    provider = automatic.Provider()
    status, result = review(ledger, saved, provider)
    assert status == 200 and len(provider.calls) == 1
    assert financial_rows(ledger) == before
    assert usage(ledger)["jev_reserved_microusd"] == before_usage["jev_reserved_microusd"] + JEV_RESERVATION_MICROUSD
    with ledger.store.connect() as db:
        assert db.execute("SELECT attempts FROM expense_suggestion_budget WHERE owner_id=? AND day=?",
            ("usr_github_77", day)).fetchone()[0] == 20


def test_write_only_key_gets_minimal_review_without_full_saved_or_candidate_dto(ledger):
    body, saved, _ = setup_expense(ledger)
    other = duplicate(ledger, body)
    status, token = asyncio.run(create_api_key(binding=ledger.binding, headers=ledger.headers,
        body={"name": "review only writer", "scopes": ["expenses:write"],
            "restrictions": {"shared": True}}, now=ledger.now + 3))
    assert status == 201
    status, result = review(ledger, saved, automatic.Provider(), headers={"Authorization": "Bearer " + token["key"]})
    assert status == 200
    assert result["checks"]["duplicate"]["candidate"] == {"id": other["id"], "revision": 1}
    assert all(key not in result for key in ("purpose", "note", "amount", "currency", "target"))
    assert "PRIVATE OTHER RECORD NOTE" not in json.dumps(result)


def test_openapi_has_explicit_saved_record_review_and_closed_structured_checks():
    contract = yaml.safe_load((Path(__file__).resolve().parents[1] / "openapi/ledger-v1.yaml").read_text())
    route = contract["paths"]["/api/v1/expenses/{id}/review"]["post"]
    assert route["x-pullwise-scope"] == "expenses:write"
    body = route["requestBody"]["content"]["application/json"]["schema"]
    assert body["type"] == "object" and body["maxProperties"] == 0 and body["additionalProperties"] is False
    schema = contract["components"]["schemas"]["ExpenseReview"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["checks"]["additionalProperties"] is False
