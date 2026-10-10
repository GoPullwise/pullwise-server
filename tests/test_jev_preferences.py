"""Account preferences persist without changing model budgets or financial facts."""
import asyncio
import json
from pathlib import Path

import pytest
import yaml

import test_ledger_automatic_assistance as automatic
from test_ledger_automatic_edit import edit, financial_rows, set_plan, setup_expense
from test_ledger_review import duplicate, review, usage
from pullwise_server.cloudflare_jev_preferences import jev_enabled, jev_preference
from pullwise_server.cloudflare_ledger_profile import read_ledger_me
from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_state_records import MAX_SAFE_INTEGER, encode_record, record_name
from pullwise_server.ledger_plan_policy import JEV_RESERVATION_MICROUSD, entitlements, plan_entitlements

ledger = automatic.ledger
OWNER = "usr_github_77"


def account(ledger, *, method="GET", body=None, revision='"1"', headers=None):
    return automatic.call(ledger, body, method=method, path="/api/v1/account/jev",
        headers={**(headers or ledger.headers), **({"If-Match": revision} if revision is not None else {})})


def user_record(ledger):
    with ledger.store.connect() as db:
        return dict(db.execute("SELECT * FROM app_state WHERE name=?", (record_name("users", OWNER),)).fetchone())


def patch_user(ledger, changes):
    row = user_record(ledger)
    user = {**json.loads(row["payload"]), **changes}
    with ledger.store.connect() as db:
        db.execute("UPDATE app_state SET payload=? WHERE name=?", (encode_record("users", OWNER, user), row["name"]))


def suggestion_rows(ledger):
    with ledger.store.connect() as db:
        return {table: [dict(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
            for table in ("expense_suggestion_budget", "expense_suggestion_events", "ledger_plan_usage",
                          "account_entitlement_authority", "d1_command_guard")}


@pytest.mark.parametrize("plan,budget", [("free", "0.00"), ("pro", "3.00"), ("max", "5.00")])
@pytest.mark.parametrize("runtime", [False, True])
def test_legacy_get_is_read_only_and_expresses_stored_preference_separately(ledger, plan, budget, runtime):
    set_plan(ledger, plan)
    ledger.binding.jev_available = runtime
    before, before_models = user_record(ledger), suggestion_rows(ledger)
    assert account(ledger) == (200, {"enabled": True, "revision": 1, "eligible": plan != "free",
        "available": runtime and plan != "free", "monthlyBudgetUsd": budget})
    assert user_record(ledger) == before and suggestion_rows(ledger) == before_models


@pytest.mark.parametrize("plan", ["pro", "max"])
def test_preference_cas_merges_full_raw_user_and_preserves_all_usage_and_authority(ledger, plan):
    set_plan(ledger, plan)
    patch_user(ledger, {"opaquePreserved": {"nested": [1, False, "retained"]}})
    _, saved, _ = setup_expense(ledger)
    ledger.binding.jev_available = True
    before, models, financial = json.loads(user_record(ledger)["payload"]), suggestion_rows(ledger), financial_rows(ledger)
    assert account(ledger, method="PATCH", body={"enabled": False}) == (200,
        {"enabled": False, "revision": 2, "eligible": True, "available": False,
         "monthlyBudgetUsd": "3.00" if plan == "pro" else "5.00"})
    saved_user = json.loads(user_record(ledger)["payload"])
    assert saved_user == {**before, "jevEnabled": False, "jevPreferenceRevision": 2}
    assert "_actor" not in saved_user and "_workspace" not in saved_user
    assert suggestion_rows(ledger) == models and financial_rows(ledger) == financial
    confirmed = user_record(ledger)
    assert account(ledger, method="PATCH", body={"enabled": False}, revision='"2"')[1]["revision"] == 2
    assert user_record(ledger) == confirmed and suggestion_rows(ledger) == models
    assert account(ledger, method="PATCH", body={"enabled": True}, revision='"2"')[1]["available"] is True
    assert account(ledger)[1]["revision"] == 3
    status, me = asyncio.run(read_ledger_me(binding=ledger.binding, headers=ledger.headers, now=ledger.now + 3))
    assert status == 200 and me["entitlements"]["jev"]["enabled"] is True
    assert automatic.call(ledger, method="GET", path=f"/api/v1/expenses/{saved['id']}")[0] == 200


@pytest.mark.parametrize("revision,status", [(None, 428), ('"0"', 422), ('"invalid"', 422), ('"2"', 412)])
def test_patch_requires_current_independent_revision_without_writes(ledger, revision, status):
    before, models = user_record(ledger), suggestion_rows(ledger)
    assert account(ledger, method="PATCH", body={"enabled": False}, revision=revision)[0] == status
    assert user_record(ledger) == before and suggestion_rows(ledger) == models


@pytest.mark.parametrize("body", [None, {}, {"enabled": "false"}, {"enabled": 0},
    {"enabled": None}, {"enabled": False, "ownerId": "other"}, [False]])
def test_patch_rejects_nonboolean_or_extra_fields_before_write(ledger, body):
    before = user_record(ledger)
    assert account(ledger, method="PATCH", body=body)[0] == 422
    assert user_record(ledger) == before


@pytest.mark.parametrize("changes", [{"billing": {"plan": "free", "status": "active"}},
    {"billing": {"plan": "pro", "status": "active", "currentPeriodEnd": 1}}])
def test_free_or_expired_account_cannot_change_retained_preference(ledger, changes):
    patch_user(ledger, {**changes, "jevEnabled": False, "jevPreferenceRevision": 5})
    before, models = user_record(ledger), suggestion_rows(ledger)
    assert account(ledger, method="PATCH", body={"enabled": True}, revision='"5"') == (
        403, {"error": {"code": "JEV_PLAN_REQUIRED"}})
    assert account(ledger)[1]["enabled"] is False and account(ledger)[1]["eligible"] is False
    assert user_record(ledger) == before and suggestion_rows(ledger) == models


@pytest.mark.parametrize("changes", [{"jevEnabled": "false"}, {"jevEnabled": 0},
    {"jevPreferenceRevision": True}, {"jevPreferenceRevision": 0},
    {"jevPreferenceRevision": MAX_SAFE_INTEGER + 1}])
def test_malformed_persisted_preference_fails_closed_without_reset_or_model_call(ledger, changes):
    manual = automatic.category(ledger)
    patch_user(ledger, changes)
    before, models = user_record(ledger), suggestion_rows(ledger)
    assert account(ledger) == (503, {"error": {"code": "JEV_PREFERENCE_UNAVAILABLE"}})
    assert account(ledger, method="PATCH", body={"enabled": False})[0] == 503
    provider = automatic.Provider()
    result = automatic.call(ledger, {"target": {"kind": "shared"}, "purpose": "Hosting"}, provider,
        path="/api/v1/expense-suggestions")
    assert result[0] == 200 and result[1]["reason"] == "disabled" and not provider.calls
    assert user_record(ledger) == before and suggestion_rows(ledger) == models
    user = json.loads(before["payload"])
    assert jev_preference(user) is None and jev_enabled(user) is False
    assert entitlements(user, now=ledger.now + 3, jev_available=True)["jev"]["available"] is False
    assert manual


def test_max_preference_revision_allows_read_and_noop_but_never_overflows(ledger):
    patch_user(ledger, {"jevEnabled": True, "jevPreferenceRevision": MAX_SAFE_INTEGER})
    before = user_record(ledger)
    assert account(ledger, method="PATCH", body={"enabled": True}, revision=f'"{MAX_SAFE_INTEGER}"')[0] == 200
    assert account(ledger, method="PATCH", body={"enabled": False}, revision=f'"{MAX_SAFE_INTEGER}"') == (
        409, {"error": {"code": "PREFERENCE_REVISION_EXHAUSTED"}})
    assert user_record(ledger) == before


@pytest.mark.parametrize("entry", ["draft", "create", "edit", "review"])
def test_off_during_provider_rejects_old_authority_without_financial_or_suggestion_event(ledger, entry):
    body, saved, _ = setup_expense(ledger)
    before, before_usage = financial_rows(ledger), usage(ledger)
    class DisableDuringProvider(automatic.Provider):
        async def evaluate(self, request):
            status, _ = await handle_ledger_request(binding=ledger.binding, gateway=None,
                method="PATCH", path="/api/v1/account/jev", params={},
                headers={**ledger.headers, "If-Match": '"1"'}, body={"enabled": False}, now=ledger.now + 3)
            assert status == 200
            return await super().evaluate(request)
    provider = DisableDuringProvider()
    if entry == "draft":
        status, result = automatic.call(ledger, {"target": body["target"], "purpose": body["purpose"]},
            provider, path="/api/v1/expense-suggestions")
    elif entry == "create":
        status, result = automatic.call(ledger, body, provider, key="during-provider")
    elif entry == "edit":
        status, result = edit(ledger, saved, {**body, "note": "Unconfirmed edit"}, provider)
    else:
        status, result = review(ledger, saved, provider)
    assert (status, result) == (403, {"error": {"code": "AUTHORIZATION_CHANGED"}})
    assert len(provider.calls) == 1 and financial_rows(ledger) == before
    with ledger.store.connect() as db:
        assert db.execute("SELECT count(*) FROM expense_suggestion_events").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM expense_suggestion_budget").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM d1_command_guard").fetchone()[0] == 0
    after_usage = usage(ledger)
    for key in ("projects", "records", "writes", "minute_writes"):
        assert after_usage[key] == before_usage[key]
    assert after_usage["jev_reserved_microusd"] - before_usage["jev_reserved_microusd"] == JEV_RESERVATION_MICROUSD


@pytest.mark.parametrize("plan", ["pro", "max"])
def test_off_prevents_all_model_admission_keeps_manual_and_local_review_and_reenable_budget(ledger, plan):
    set_plan(ledger, plan)
    body, saved, _ = setup_expense(ledger)
    other = duplicate(ledger, body)
    assert account(ledger, method="PATCH", body={"enabled": False})[0] == 200
    models = suggestion_rows(ledger)
    provider = automatic.Provider()
    status, local = review(ledger, saved, provider)
    assert status == 200 and local["checks"]["duplicate"]["candidate"] == {"id": other["id"], "revision": 1}
    assert local["checks"]["category"]["reason"] == local["checks"]["target"]["reason"] == "disabled"
    assert automatic.call(ledger, {"target": body["target"], "purpose": body["purpose"]}, provider,
        path="/api/v1/expense-suggestions")[1]["reason"] == "disabled"
    assert suggestion_rows(ledger) == models and provider.enabled is True and provider.calls == []
    assert automatic.call(ledger, {key: value for key, value in body.items() if key != "categoryId"},
        provider, key="automatic-disabled")[0] == 422
    status, manual = automatic.call(ledger, {**body, "purpose": "Manual while disabled"}, provider, key="manual-off")
    assert status == 201 and manual["assistance"]["reason"] == "disabled"
    assert edit(ledger, saved, {**body, "note": "Manual edit while disabled"}, provider)[0] == 200
    assert not provider.calls
    assert account(ledger, method="PATCH", body={"enabled": True}, revision='"2"')[0] == 200
    assert automatic.call(ledger, automatic.expense(), provider, key="after-enabled")[0] == 201
    assert len(provider.calls) == 1 and usage(ledger)["jev_reserved_microusd"] == JEV_RESERVATION_MICROUSD


def test_preference_does_not_change_public_plan_templates():
    for plan, budget in (("free", "0.00"), ("pro", "3.00"), ("max", "5.00")):
        template = plan_entitlements(plan, jev_available=True)["jev"]
        private = entitlements({"jevEnabled": False, "billing": {"plan": plan, "status": "active"}},
            now=1, jev_available=True)["jev"]
        assert "enabled" not in template and template["monthlyBudgetUsd"] == private["monthlyBudgetUsd"] == budget
        assert template["available"] is (plan != "free") and private["available"] is False


def test_openapi_account_preference_is_cookie_only_strict_and_independently_versioned():
    contract = yaml.safe_load((Path(__file__).parents[1] / "openapi/ledger-v1.yaml").read_text())
    path = contract["paths"]["/api/v1/account/jev"]
    assert path["get"]["security"] == path["patch"]["security"] == [{"cookieSession": []}]
    body = path["patch"]["requestBody"]["content"]["application/json"]["schema"]
    assert body["required"] == ["enabled"] and body["additionalProperties"] is False
    assert body["properties"] == {"enabled": {"type": "boolean"}}
    assert {ref["$ref"] for ref in path["patch"]["parameters"]} == {
        "#/components/parameters/IfMatch", "#/components/parameters/AuthOrigin"}
