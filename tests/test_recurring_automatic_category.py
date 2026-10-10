"""Recurring saves share real Jev eligibility, budgets and replay protection."""
import asyncio
import json
from contextlib import closing

import pytest

from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1
from pullwise_server.cloudflare_state_records import encode_record, record_name
from test_ledger_automatic_assistance import Provider
from test_ledger_recurring import app, draft, NOW, NoGitHub


def call(app, body, provider, *, method="POST", identifier=None, revision=None,
         actor="owner", key="auto-recurring"):
    headers = {"Cookie": "pw_session=" + actor,
               "Origin": "https://app.example.test", "Idempotency-Key": key}
    if actor != "owner":
        headers["X-Pullwise-Workspace"] = "owner"
    if revision is not None:
        headers["If-Match"] = f'"{revision}"'
    return asyncio.run(handle_ledger_request(
        binding=PlanLimitedD1(app.raw, policy=app.policy, now=NOW),
        gateway=NoGitHub(), suggestion_gateway=provider, method=method,
        path="/api/v1/expense-recurring-rules" + ("/" + identifier if identifier else ""),
        headers=headers, params={}, body=body, now=NOW))


def automatic(**changes):
    body = draft(start="2026-11-01", **changes)
    body.pop("categoryId")
    return body


def change_user(app, identity="owner", **changes):
    with closing(app.store.connect()) as db:
        name = record_name("users", identity)
        user = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?", (name,)).fetchone()[0])
        for field, value in changes.items():
            if field == "plan":
                user["billing"]["plan"] = value
            else:
                user[field] = value
        db.execute("UPDATE app_state SET payload=? WHERE name=?",
                   (encode_record("users", identity, user), name))
        db.commit()


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("plan", ["pro", "max"])
def test_plan_save_infers_once_and_exact_replay_and_get_spend_nothing(app, project, plan):
    change_user(app, plan=plan)
    provider = Provider()
    body = automatic(project=project)
    status, saved = call(app, body, provider)
    assert status == 201, saved
    assert saved["categoryId"] == "cat_1"
    assert saved["assistance"]["categorySource"] == "jev"
    assert saved["target"] == body["target"] and saved["amount"] == "12.34"
    assert saved["schedule"] == {**body["schedule"], "endOn": None}
    assert call(app, body, provider) == (201, saved)
    assert call(app, None, provider, method="GET", identifier=saved["id"])[0] == 200
    assert len(provider.calls) == 1
    assert app.rows("expense_suggestion_budget")[0]["attempts"] == 1
    assert app.rows("expenses") == []
    assert call(app, {**body, "amount": "99.00"}, provider)[0] == 409
    assert len(provider.calls) == 1


def test_edit_can_request_automatic_category_and_keeps_submitted_fields(app):
    status, saved = app.call("POST", body=draft(start="2026-11-01"))
    assert status == 201
    provider = Provider()
    body = automatic(amount="88.99", note="User note")
    status, edited = call(app, body, provider, method="PATCH", identifier=saved["id"],
                          revision=saved["revision"])
    assert status == 200, edited
    assert edited["categoryId"] == "cat_1" and edited["amount"] == "88.99"
    assert edited["note"] == "User note" and edited["revision"] == saved["revision"] + 1
    assert len(provider.calls) == 1


@pytest.mark.parametrize("provider", [None, Provider(confidence=0.6), Provider(failure=TimeoutError())])
def test_fallback_requires_manual_category_and_never_persists_incomplete_plan(app, provider):
    status, result = call(app, automatic(), provider)
    assert status == 422 and result["error"]["code"] == "CATEGORY_REQUIRED"
    assert app.rows("expense_recurring_rules") == app.rows("expenses") == []
    status, result = call(app, draft(start="2026-11-01"), provider, key="manual")
    assert status == 201 and result["categoryId"] == "cat_1"


@pytest.mark.parametrize("condition", ["free", "disabled", "exhausted"])
def test_owner_eligibility_preference_and_allowance_retain_manual_path(app, condition):
    if condition == "free":
        change_user(app, plan="free")
    elif condition == "disabled":
        change_user(app, jevEnabled=False)
    else:
        app.policy["pro"]["jevMonthlyBudgetUsd"] = "0.00"
    provider = Provider()
    status, result = call(app, automatic(), provider)
    assert status == 422 and result["error"]["code"] == "CATEGORY_REQUIRED"
    assert provider.calls == []
    assert call(app, draft(start="2026-11-01"), provider, key="manual")[0] == 201


def test_viewer_and_unknown_target_are_denied_before_model_or_budget(app):
    provider = Provider()
    assert call(app, automatic(), provider, actor="viewer")[0] == 403
    body = automatic(project=True)
    body["target"]["projectId"] = "missing"
    assert call(app, body, provider)[0] in {403, 404}
    assert provider.calls == []
    assert app.rows("expense_suggestion_budget") == []


def test_owner_preference_change_during_inference_cannot_commit_plan(app):
    provider = Provider(on_call=lambda: change_user(app, jevEnabled=False,
                                                   jevPreferenceRevision=2))
    status, _ = call(app, automatic(), provider)
    assert status in {401, 403, 409, 412}
    assert app.rows("expense_recurring_rules") == app.rows("expenses") == []


def test_archived_inferred_category_cannot_be_saved(app):
    def archive():
        with closing(app.store.connect()) as db:
            db.execute("UPDATE expense_categories SET archived_at='2026-10-08' WHERE id='cat_1'")
            db.commit()
    provider = Provider(on_call=archive)
    status, result = call(app, automatic(), provider)
    assert status == 422 and result["error"]["code"] == "INVALID_CATEGORY"
    assert app.rows("expense_recurring_rules") == []
