"""Background Max assistance has its own budget, not a second write allowance."""
import pytest

import test_ledger_automatic_assistance as assistance_fixture
from pullwise_server.ledger_plan_policy import default_policy, JEV_RESERVATION_MICROUSD

ledger = assistance_fixture.ledger


@pytest.mark.parametrize("quota", ["writesPerMinute", "writesPerMonth"])
@pytest.mark.parametrize("provider_failure", [None, TimeoutError])
def test_automatic_assistance_keeps_the_final_business_write_available(ledger, quota, provider_failure):
    category_id = assistance_fixture.category(ledger)
    policy = default_policy()
    policy["max"][quota] = 2  # Category setup has used one business write.
    ledger.binding.plan_policy = policy
    provider = assistance_fixture.Provider(failure=provider_failure() if provider_failure else None)

    status, saved = assistance_fixture.call(ledger,
        assistance_fixture.expense(category_id), provider)
    assert status == 201
    assert saved["categoryId"] == category_id
    assert len(provider.calls) == 1
    assert saved["assistance"]["status"] == ("unavailable" if provider_failure else "available")
    with ledger.store.connect() as db:
        usage = db.execute("SELECT writes,minute_writes,records,jev_reserved_microusd FROM ledger_plan_usage").fetchone()
        assert tuple(usage) == (2, 2, 1, JEV_RESERVATION_MICROUSD)
        assert db.execute("SELECT count(*) FROM expense_suggestion_events").fetchone()[0] == 1

    # The third actual operation remains capped; background counting cannot
    # create a loophole in ordinary category, expense or key mutations.
    status, error = assistance_fixture.call(ledger, {"name": "Over allowance"},
        path="/api/v1/categories")
    assert status == 429
    assert error["error"]["code"] == (
        "WRITE_RATE_LIMIT" if quota == "writesPerMinute" else "MONTHLY_WRITE_LIMIT")


def test_background_classification_can_use_the_last_monthly_write(ledger):
    category_id = assistance_fixture.category(ledger)
    policy = default_policy()
    policy["max"]["writesPerMonth"] = 2
    ledger.binding.plan_policy = policy
    status, saved = assistance_fixture.call(ledger, assistance_fixture.expense(),
        assistance_fixture.Provider())
    assert status == 201
    assert saved["categoryId"] == category_id
    assert saved["assistance"]["categorySource"] == "jev"


def test_uncertain_classification_spends_jev_budget_without_a_business_write(ledger):
    assistance_fixture.category(ledger)
    status, error = assistance_fixture.call(ledger, assistance_fixture.expense(),
        assistance_fixture.Provider(confidence=0.6))
    assert status == 422 and error["error"]["code"] == "CATEGORY_REQUIRED"
    with ledger.store.connect() as db:
        assert db.execute("SELECT count(*) FROM expenses").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM expense_suggestion_events").fetchone()[0] == 1
        assert tuple(db.execute("SELECT writes,minute_writes,records,jev_reserved_microusd FROM ledger_plan_usage").fetchone()) == (
            1, 1, 0, JEV_RESERVATION_MICROUSD)
