"""Billing reuses exact monthly reservations without changing stored usage."""
import asyncio
from contextlib import closing
from datetime import datetime, timezone

import pytest

from ledger_d1_fixture import D1ShapedSQLite, seed, seed_auth
from pullwise_server.cloudflare_billing_catalog import read_public_plan
from pullwise_server.cloudflare_billing_read import read_billing
from pullwise_server.cloudflare_ledger_profile import read_ledger_me
from pullwise_server.cloudflare_plan_limits import (
    PlanLimitedD1, PlanLimitError, capacity_usage_payload,
)
from pullwise_server.ledger_plan_policy import default_policy
from test_cloudflare_billing_read import seed_public_catalog
from test_ledger_plan_limits import account_plan, write


COOKIE = {"Cookie": "pw_session=session-local"}


@pytest.mark.parametrize("reader", [read_billing, read_public_plan, read_ledger_me])
@pytest.mark.parametrize("plan,limit", [("pro", "1.234567"), ("max", "4.250001")])
def test_paid_usage_uses_exact_configured_allowance_even_when_jev_unavailable(tmp_path, reader, plan, limit):
    fixture, _, _ = seed(tmp_path / "billing.db")
    seed_auth(fixture)
    seed_public_catalog(fixture)
    frozen = account_plan(fixture, plan, currentPeriodEnd=fixture.now + 864000)
    policy = default_policy()
    policy[plan]["jevMonthlyBudgetUsd"] = limit
    raw = D1ShapedSQLite(fixture.store)
    binding = PlanLimitedD1(raw, policy=policy, now=fixture.now)
    write(binding, frozen, fixture, 1, kind="jev")
    # Store a non-cent amount to prove that projection never rounds usage.
    with fixture.store._immediate() as db:
        db.execute("UPDATE ledger_plan_usage SET jev_reserved_microusd=1234567,jev_delta=0")
    with closing(fixture.store.connect()) as db:
        before = tuple(db.execute("SELECT * FROM ledger_plan_usage").fetchone())
    raw.batch_count = 0
    status, payload = asyncio.run(reader(binding=binding, headers=COOKIE, now=fixture.now))
    assert status == 200
    assert payload["ledgerUsage"]["jev"] == {
        "month": datetime.fromtimestamp(fixture.now, timezone.utc).strftime("%Y-%m"),
        "currency": "USD", "usedMicrousd": 1234567,
        "limitMicrousd": 1234567 if plan == "pro" else 4250001,
    }
    assert raw.batch_count == 1
    with closing(fixture.store.connect()) as db:
        assert tuple(db.execute("SELECT * FROM ledger_plan_usage").fetchone()) == before


@pytest.mark.parametrize("interval", ["month", "year"])
def test_month_boundary_projects_zero_without_resetting_annual_or_monthly_usage(tmp_path, interval):
    before_boundary = int(datetime(2026, 10, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    fixture, _, _ = seed(tmp_path / "boundary.db", now=before_boundary)
    seed_auth(fixture)
    frozen = account_plan(fixture, "max", interval=interval, currentPeriodEnd=fixture.now + 864000)
    binding = PlanLimitedD1(D1ShapedSQLite(fixture.store), now=fixture.now)
    write(binding, frozen, fixture, 1, kind="jev")
    with closing(fixture.store.connect()) as db:
        before = tuple(db.execute("SELECT * FROM ledger_plan_usage").fetchone())
    status, payload = asyncio.run(read_billing(binding=binding, headers=COOKIE, now=fixture.now + 1))
    assert status == 200
    assert payload["ledgerUsage"]["jev"] == {"month": "2026-11", "currency": "USD",
        "usedMicrousd": 0, "limitMicrousd": 5000000}
    with closing(fixture.store.connect()) as db:
        assert tuple(db.execute("SELECT * FROM ledger_plan_usage").fetchone()) == before


@pytest.mark.parametrize("month,reserved", [
    ("2026-13", 0), ("2026-11", 0), ("2026-10", None),
    ("2026-10", -1), ("2026-10", 9007199254740992), (None, 12),
])
def test_malformed_reservation_is_unavailable_instead_of_successful_zero(month, reserved):
    now = int(datetime(2026, 10, 10, tzinfo=timezone.utc).timestamp())
    user = {"id": "owner", "billing": {"plan": "pro", "status": "active"}}
    rows = [{"projects": 0, "records": 0, "jev_month": month, "jev_reserved_microusd": reserved}]
    with pytest.raises(PlanLimitError, match="USAGE_GUARD_UNAVAILABLE"):
        capacity_usage_payload(user, rows, now=now)


def test_paid_usage_does_not_disclose_selected_ledger_reservations(tmp_path):
    fixture, _, frozen = seed(tmp_path / "owner.db")
    seed_auth(fixture)
    seed_public_catalog(fixture)
    binding = PlanLimitedD1(D1ShapedSQLite(fixture.store), now=fixture.now)
    write(binding, frozen, fixture, 1, kind="jev")
    # Another ledger header must not select another owner's billing usage.
    status, personal = asyncio.run(read_public_plan(binding=binding,
        headers={**COOKIE, "X-Pullwise-Workspace": "different-owner"}, now=fixture.now))
    assert status == 200 and personal["ledgerUsage"]["workspaceId"] == "owner"
    assert personal["ledgerUsage"]["jev"]["usedMicrousd"] > 0
    assert "jev" not in personal["account"]
