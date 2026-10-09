"""Preview operator grants preserve payment/data facts and expire on use."""
import asyncio
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from ledger_d1_fixture import D1ShapedSQLite, seed
from pullwise_server.account_cycle_rules import effective_user_plan, quota_cycle_for_user
from pullwise_server.billing_projection import billing_account_dto
from pullwise_server.cloudflare_state_records import encode_record
from pullwise_server.preview_plan_grants import (
    PreviewPlanGrantError, apply_preview_plan_grant, parse_preview_plan_grant_request,
)


def grant(now=1_800_000_000):
    return {"grantId": "test_trial", "plan": "max", "startsAt": now,
            "expiresAt": now + 3600, "issuedAt": now, "reason": "Preview Jev evaluation",
            "source": "operator", "environment": "preview"}


def request(now=1_800_000_000, **changes):
    return json.dumps({"grantId": "test_trial", "ownerId": "owner", "githubLogin": "synthetic",
        "email": "synthetic@example.test", "plan": "max", "startsAt": now,
        "expiresAt": now + 3600, "reason": "Preview Jev evaluation", **changes})


@pytest.mark.parametrize("baseline", ["free", "pro", "max"])
def test_grant_uses_exact_time_window_then_restores_current_payment_plan(baseline):
    now = 1_800_000_000
    user = {"previewPlanGrant": grant(now), "createdAt": now - 100,
            "billing": {"plan": baseline, "status": "active",
                        "currentPeriodStart": now - 100, "currentPeriodEnd": now + 7200}}
    assert effective_user_plan(user, timestamp=now - 1) == baseline
    assert effective_user_plan(user, timestamp=now) == "max"
    assert effective_user_plan(user, timestamp=now + 3599) == "max"
    assert effective_user_plan(user, timestamp=now + 3600) == baseline
    assert effective_user_plan(user, timestamp=now + 7200) == "free"
    _, expiry = quota_cycle_for_user(user, "max", timestamp=now)
    assert expiry == now + (7200 if baseline == "max" else 3600)


@pytest.mark.parametrize("change", [{"source": "http"}, {"environment": "production"},
    {"plan": "pro"}, {"startsAt": True}, {"expiresAt": 1_800_000_000},
    {"expiresAt": 1_800_000_000 + 32 * 86400}])
def test_invalid_stored_grant_cannot_grant_max(change):
    assert effective_user_plan({"previewPlanGrant": {**grant(), **change}}, timestamp=1_800_000_000) == "free"


def test_billing_exposes_grant_expiry_separately_from_provider_facts():
    user = {"billing": None, "previewPlanGrant": grant()}
    dto = billing_account_dto(user, "max", timestamp=1_800_000_000)
    assert dto["plan"] == "max" and dto["previewPlanGrant"]["active"] is True
    assert dto["previewPlanGrant"]["expiresAt"] == 1_800_003_600
    for key in ("subscriptionId", "customerId"):
        assert dto[key] is None
    assert dto["provider"] == "preview_grant" and dto["status"] == "trialing"
    assert dto["currentPeriodStart"] == 1_800_000_000 and dto["currentPeriodEnd"] == 1_800_003_600
    assert dto["subscriptionEvents"] == [] and user["billing"] is None
    assert billing_account_dto(user, "free", timestamp=1_800_003_600)["previewPlanGrant"]["active"] is False


def test_existing_paid_provider_facts_remain_separate_from_operator_entitlement():
    user = {"billing": {"provider": "creem", "plan": "pro", "status": "active",
                       "subscriptionId": "sub_real_baseline", "currentPeriodStart": 1_799_999_000,
                       "currentPeriodEnd": 1_800_010_000}, "previewPlanGrant": grant()}
    dto = billing_account_dto(user, "max", timestamp=1_800_000_000)
    assert dto["previewPlanGrant"]["active"] is True and dto["plan"] == "max"
    assert dto["provider"] == "creem" and dto["subscriptionId"] == "sub_real_baseline"
    assert dto["currentPeriodEnd"] == 1_800_010_000


def setup(fixture):
    with fixture.store._immediate() as db:
        user = json.loads(db.execute("SELECT payload FROM app_state WHERE name='record:users:owner'").fetchone()[0])
        user.update(githubLogin="synthetic", email="synthetic@example.test",
                    retainedData={"projects": ["synthetic_project"], "expense": {"minor": 120}})
        db.execute("UPDATE app_state SET payload=? WHERE name='record:users:owner'",
                   (encode_record("users", "owner", user),))
        return user


def stored(fixture):
    with closing(fixture.store.connect()) as db:
        user = json.loads(db.execute("SELECT payload FROM app_state WHERE name='record:users:owner'").fetchone()[0])
        authority = tuple(db.execute("SELECT revision,plan,valid_until,dirty FROM account_entitlement_authority WHERE owner_id='owner'").fetchone())
        events = dict(db.execute("SELECT name,payload FROM app_state WHERE name GLOB 'record:billingEvents:*'"))
        return user, authority, events


def test_grant_atomic_audit_projection_preserves_account_and_replays_without_writes(tmp_path):
    fixture, _, _ = seed(tmp_path / "grant.db")
    before = setup(fixture)
    binding = D1ShapedSQLite(fixture.store)
    result = asyncio.run(apply_preview_plan_grant(binding=binding, request_json=request(), now=fixture.now))
    assert result["applied"] is True and result["replayed"] is False
    after, authority, events = stored(fixture)
    assert {key: value for key, value in after.items() if key != "previewPlanGrant"} == before
    assert authority == (3, "max", fixture.now + 3600, 0)
    audit = json.loads(events["record:billingEvents:preview-plan-grant:test_trial"])
    assert audit["grant"] == after["previewPlanGrant"] and audit["ownerId"] == "owner"
    assert events["record:billingEvents:event_fixture"] == '{"status":"processed"}'
    assert binding.batch_count == 1
    baseline = stored(fixture)
    for clock in (fixture.now + 10, fixture.now + 3600):
        result = asyncio.run(apply_preview_plan_grant(binding=binding, request_json=request(), now=clock))
        assert result["replayed"] is True and result["applied"] is False
        assert binding.batch_count == 1 and stored(fixture) == baseline


@pytest.mark.parametrize("change", [{"githubLogin": "other"}, {"email": "other@example.test"},
                                    {"ownerId": "other"}])
def test_identity_mismatch_has_no_writes(tmp_path, change):
    fixture, _, _ = seed(tmp_path / "grant.db")
    setup(fixture)
    binding = D1ShapedSQLite(fixture.store)
    before = stored(fixture)
    with pytest.raises(PreviewPlanGrantError, match="IDENTITY_MISMATCH"):
        asyncio.run(apply_preview_plan_grant(binding=binding, request_json=request(**change), now=fixture.now))
    assert binding.batch_count == 0 and stored(fixture) == before


@pytest.mark.parametrize("change", [{"reason": "Changed reason"}, {"grantId": "other_trial"},
                                    {"expiresAt": 1_800_007_200}])
def test_conflicting_reissue_cannot_overwrite_a_grant(tmp_path, change):
    fixture, _, _ = seed(tmp_path / "grant.db")
    setup(fixture)
    binding = D1ShapedSQLite(fixture.store)
    asyncio.run(apply_preview_plan_grant(binding=binding, request_json=request(), now=fixture.now))
    before = stored(fixture)
    with pytest.raises(PreviewPlanGrantError, match="CONFLICT"):
        asyncio.run(apply_preview_plan_grant(binding=binding, request_json=request(**change), now=fixture.now))
    assert binding.batch_count == 1 and stored(fixture) == before


def test_concurrent_account_change_rolls_back_grant_and_audit(tmp_path):
    fixture, _, _ = seed(tmp_path / "grant.db")
    setup(fixture)
    binding = D1ShapedSQLite(fixture.store)
    def change_account():
        with fixture.store._immediate() as db:
            user = json.loads(db.execute("SELECT payload FROM app_state WHERE name='record:users:owner'").fetchone()[0])
            user["name"] = "Concurrent actual name"
            db.execute("UPDATE app_state SET payload=? WHERE name='record:users:owner'",
                       (encode_record("users", "owner", user),))
    binding.before_batch = change_account
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(apply_preview_plan_grant(binding=binding, request_json=request(), now=fixture.now))
    user, authority, events = stored(fixture)
    assert "previewPlanGrant" not in user and user["name"] == "Concurrent actual name"
    assert authority[0] == 1 and "record:billingEvents:preview-plan-grant:test_trial" not in events


def test_expired_new_grant_is_rejected_and_future_grant_keeps_free_until_start(tmp_path):
    fixture, _, _ = seed(tmp_path / "grant.db")
    before = setup(fixture)
    before["billing"] = None
    with fixture.store._immediate() as db:
        db.execute("UPDATE app_state SET payload=? WHERE name='record:users:owner'",
                   (encode_record("users", "owner", before),))
    binding = D1ShapedSQLite(fixture.store)
    with pytest.raises(PreviewPlanGrantError, match="EXPIRED"):
        asyncio.run(apply_preview_plan_grant(binding=binding,
            request_json=request(fixture.now - 3600), now=fixture.now))
    assert binding.batch_count == 0
    asyncio.run(apply_preview_plan_grant(binding=binding,
        request_json=request(fixture.now + 100), now=fixture.now))
    user, authority, _ = stored(fixture)
    assert authority[1] == "free" and effective_user_plan(user, timestamp=fixture.now) == "free"
    assert effective_user_plan(user, timestamp=fixture.now + 100) == "max"


@pytest.mark.parametrize("change", [{"plan": "pro"}, {"startsAt": True}, {"expiresAt": float("nan")},
    {"reason": "x" * 501}, {"startsAt": 1_800_000_000 + 32 * 86400}, {"ownerId": "invalid\nowner"},
    {"expiresAt": 1_800_000_000 + 32 * 86400}, {"unexpected": "caller SQL"}])
def test_operator_input_is_bounded_before_database_access(change):
    with pytest.raises(PreviewPlanGrantError, match="INVALID_PREVIEW_PLAN_GRANT"):
        parse_preview_plan_grant_request(request(**change), now=1_800_000_000)


@pytest.mark.parametrize("change", [{"PULLWISE_MODE": "production"}, {"PULLWISE_MODE": "local"},
    {"PULLWISE_D1_ACCESS_ENABLED": "0"}, {"PULLWISE_PREVIEW_PRODUCT_ENABLED": "0"},
    {"PULLWISE_APP_URL": "https://pull-wise.com"}, {"PULLWISE_CREEM_API_BASE_URL": "https://api.creem.io"}])
def test_operator_rpc_rejects_production_or_other_environment_before_storage(change):
    from test_worker_cost_pause import load_entry
    env = SimpleNamespace(PULLWISE_MODE="preview", PULLWISE_D1_ACCESS_ENABLED="1",
        PULLWISE_PREVIEW_PRODUCT_ENABLED="1", PULLWISE_APP_URL="https://preview.pull-wise.com",
        PULLWISE_CREEM_API_BASE_URL="https://test-api.creem.io")
    for key, value in change.items():
        setattr(env, key, value)
    coordinator = load_entry().ValidationBudget(SimpleNamespace(), env)
    assert asyncio.run(coordinator.grantPreviewPlan(request())) == {
        "ok": False, "error": "PREVIEW_PLAN_GRANT_DISABLED"}
    assert coordinator.journal is None
