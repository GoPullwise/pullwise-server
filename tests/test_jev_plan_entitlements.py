"""Paid Jev allowance is a ledger-owner entitlement, separate from prices."""
import asyncio
import json
from pathlib import Path

import pytest

from ledger_d1_fixture import D1ShapedSQLite, TOKEN, seed, seed_auth
from pullwise_server.billing_catalog_rules import catalog_payload
from pullwise_server.cloudflare_ledger_profile import read_ledger_me
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1
from pullwise_server.cloudflare_state_records import encode_record, record_name
from pullwise_server.ledger_plan_policy import default_policy, entitlements, parse_policy


ROOT = Path(__file__).resolve().parents[1]


def save_user(db, identity, user, now):
    db.execute("INSERT OR REPLACE INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
               (record_name("users", identity), encode_record("users", identity, user), now))


@pytest.mark.parametrize("filename", ["wrangler.jsonc", "wrangler.preview.jsonc", "wrangler.production.jsonc"])
def test_each_runtime_config_uses_canonical_paid_allowances_and_keeps_activation_separate(filename):
    config = json.loads((ROOT / "cloudflare/server" / filename).read_text())
    assert parse_policy(config["vars"]["PULLWISE_PLAN_LIMITS_JSON"]) == default_policy()
    if filename == "wrangler.production.jsonc":
        assert config["vars"]["PULLWISE_D1_ACCESS_ENABLED"] == "0"
        assert config["vars"]["PULLWISE_JEV_SUGGESTIONS_ENABLED"] == "0"
        assert config["vars"]["PULLWISE_JEV_SUGGESTIONS_EVALUATED"] == "0"
        assert not config.get("triggers", {}).get("crons")
    elif filename == "wrangler.preview.jsonc":
        assert config["vars"]["PULLWISE_JEV_SUGGESTIONS_ENABLED"] == "1"
        assert config["vars"]["PULLWISE_JEV_SUGGESTIONS_EVALUATED"] == "1"


def test_example_policy_matches_default_without_enabling_free_jev():
    assert parse_policy((ROOT / "config/ledger-plans.example.json").read_text()) == default_policy()


@pytest.mark.parametrize("plan,budget", [("free", "0.00"), ("pro", "3.00"), ("max", "5.00")])
@pytest.mark.parametrize("enabled", [False, True])
def test_cookie_and_key_profile_report_current_owner_budget_and_gateway_availability(tmp_path, plan, budget, enabled):
    fixture, _, snapshot = seed(tmp_path / "profile.db")
    seed_auth(fixture, restrictions='{"projectIds":[],"shared":true}')
    with fixture.store._immediate() as db:
        user = json.loads(snapshot)
        user["billing"]["plan"] = plan
        save_user(db, "owner", user, fixture.now)
    raw = D1ShapedSQLite(fixture.store)
    binding = PlanLimitedD1(raw, now=fixture.now)
    binding.jev_available = enabled
    for headers in ({"Cookie": "pw_session=session-local"}, {"Authorization": "Bearer " + TOKEN}):
        status, payload = asyncio.run(read_ledger_me(binding=binding, headers=headers, now=fixture.now))
        assert status == 200 and payload["id"] == "owner"
        assert payload["entitlements"]["jev"] == {
            "eligible": plan != "free", "enabled": True, "available": plan != "free" and enabled,
            "monthlyBudgetUsd": budget, "period": "utc-calendar-month", "rollover": False}
    with fixture.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ledger_plan_usage").fetchone()[0] == 0


@pytest.mark.parametrize("owner_plan,actor_plan,budget", [
    ("pro", "free", "3.00"), ("max", "free", "5.00"), ("free", "max", "0.00")])
def test_shared_ledger_profile_qualifies_the_owner_instead_of_the_actual_actor(tmp_path, owner_plan, actor_plan, budget):
    fixture, _, snapshot = seed(tmp_path / "member-profile.db")
    seed_auth(fixture)
    actor = "usr_github_123"
    with fixture.store._immediate() as db:
        owner = json.loads(snapshot)
        owner["billing"]["plan"] = owner_plan
        save_user(db, "owner", owner, fixture.now)
        save_user(db, actor, {"id": actor, "githubId": "123", "createdAt": fixture.now - 100,
                             "billing": {"plan": actor_plan, "status": "active"}}, fixture.now)
        db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)",
                   (record_name("sessions", "member-session"), encode_record("sessions", "member-session",
                    {"userId": actor, "expiresAt": fixture.now + 3600}), fixture.now))
        db.execute("INSERT INTO workspace_members VALUES(?,?,'editor',1,'joined','updated',NULL,?)",
                   ("owner", actor, "owner"))
    binding = PlanLimitedD1(D1ShapedSQLite(fixture.store), now=fixture.now)
    binding.jev_available = True
    status, payload = asyncio.run(read_ledger_me(binding=binding, headers={
        "Cookie": "pw_session=member-session", "X-Pullwise-Workspace": "owner"}, now=fixture.now))
    assert status == 200 and payload["id"] == actor and payload["workspace"]["id"] == "owner"
    assert payload["entitlements"]["plan"] == owner_plan
    assert payload["entitlements"]["jev"]["monthlyBudgetUsd"] == budget
    assert payload["entitlements"]["jev"]["available"] is (owner_plan != "free")


@pytest.mark.parametrize("plan", ["pro", "max"])
def test_annual_entitlements_remain_monthly_and_expiry_cannot_keep_paid_jev(plan):
    user = {"billing": {"plan": plan, "status": "active", "interval": "year", "currentPeriodEnd": 101}}
    current = entitlements(user, now=100, jev_available=True)["jev"]
    assert current["available"] is True and current["period"] == "utc-calendar-month"
    assert current["monthlyBudgetUsd"] == ("3.00" if plan == "pro" else "5.00")
    assert entitlements(user, now=101, jev_available=True)["jev"] == {
        "eligible": False, "enabled": True, "available": False, "monthlyBudgetUsd": "0.00",
        "period": "utc-calendar-month", "rollover": False}


def test_public_catalog_projects_runtime_budget_overrides_without_changing_price_facts():
    policy = parse_policy('{"pro":{"jevMonthlyBudgetUsd":"2.125000"},"max":{"jevMonthlyBudgetUsd":"4.00"}}')
    catalog = {"provider": "creem", "enabled": True, "currency": "USD",
               "plans": [{"id": plan, "prices": {"month": {"amount": str(price)}}}
                         for plan, price in (("free", 0), ("pro", 29), ("max", 59))]}
    projected = catalog_payload([{"payload_json": json.dumps(catalog), "expires_at": 100,
                                  "source_revision": 1}], 10, policy=policy, jev_available=True)
    assert [item["prices"] for item in projected["plans"]] == [item["prices"] for item in catalog["plans"]]
    assert [item["entitlements"]["jev"]["monthlyBudgetUsd"] for item in projected["plans"]] == ["0.00", "2.125000", "4.00"]
    assert [item["entitlements"]["jev"]["available"] for item in projected["plans"]] == [False, True, True]
