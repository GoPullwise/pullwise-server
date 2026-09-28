"""Verified synthetic Creem products become a safe public plan catalog."""
import pytest

from pullwise_server.creem_public_catalog_rules import verified_public_catalog


def product(product_id, price, period, currency="USD"):
    return {"id": product_id, "name": "Pullwise Pro",
        "description": "PR CI Updates", "price": price, "currency": currency,
        "billing_type": "recurring", "billing_period": period,
        "status": "active"}


def test_verified_catalog_binds_configured_ids_intervals_and_product_capacity():
    configured = {"pro": ["prod-month", "prod-year"], "max": []}
    fetched = {"prod-month": product("prod-month", 2900, "every-month"),
        "prod-year": product("prod-year", 29000, "every-year")}
    result = verified_public_catalog(configured, fetched)
    assert result["provider"] == "creem" and result["currency"] == "USD"
    assert result["plans"][1]["prices"]["month"]["amount"] == "29"
    assert result["plans"][1]["prices"]["year"]["productId"] == "prod-year"
    assert result["plans"][2]["prices"]["month"]["configured"] is False
    assert result["plans"][1]["entitlements"] is None
    assert all(plan["entitlements"] is None for plan in result["plans"])
    from pullwise_server.billing_catalog_rules import catalog_payload
    import json
    projected = catalog_payload([{"payload_json": json.dumps(result), "expires_at": 100, "source_revision": 1}], 10)
    assert [plan["entitlements"]["limits"]["projects"] for plan in projected["plans"]] == [3, 100, 100]
    assert projected["plans"][2]["entitlements"]["jev"] == {
        "eligible": True, "available": False, "monthlyBudgetUsd": "5.00",
        "period": "utc-calendar-month", "rollover": False}
    assert all("expense" in plan["description"] for plan in result["plans"])


@pytest.mark.parametrize("fetched", [
    {"prod-month": product("wrong", 2900, "every-month")},
    {"prod-month": product("prod-month", 2900, "every-month", "EUR"),
     "prod-year": product("prod-year", 29000, "every-year", "USD")},
    {"prod-month": product("prod-month", -1, "every-month")},
])
def test_verified_catalog_rejects_uncertain_or_conflicting_price_facts(fetched):
    with pytest.raises(ValueError):
        verified_public_catalog({"pro": ["prod-month", "prod-year"], "max": []}, fetched)
