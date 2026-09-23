"""Billing keeps payment facts while exposing PR/CI/Updates entitlements."""
from unittest.mock import patch

from pullwise_server import app, billing, db
from pullwise_server.product_entitlement_rules import PLAN_ENTITLEMENTS
from pullwise_server.product_store import ProductStore


def test_billing_account_uses_product_entitlements_and_processing_usage(tmp_path):
    path = tmp_path / "billing.db"
    store = ProductStore(path)
    store.initialize()
    user = {"id": "owner", "billing": {"plan": "pro", "status": "active",
        "subscriptionId": "sub-synthetic", "customerId": "customer-synthetic"}}
    with patch.dict("os.environ", {"PULLWISE_DB_PATH": str(path)}):
        db.reset_initialization_cache()
        account = app.billing_account_payload(user)
    assert account["subscriptionId"] == "sub-synthetic"
    assert account["customerId"] == "customer-synthetic"
    assert account["entitlements"] == PLAN_ENTITLEMENTS["pro"]
    assert account["usage"]["metric"] == "intelligent_processing"
    assert account["usage"]["limit"] == PLAN_ENTITLEMENTS["pro"]["monthlyProcessingLimit"]
    assert "reviewLimit" not in account and "quotaActivity" not in account


def test_public_pricing_catalog_uses_the_same_product_capacity_source():
    plans = billing.public_plan()["plans"]
    for plan in plans:
        assert plan["entitlements"] == PLAN_ENTITLEMENTS[plan["id"]]
        assert "reviewLimit" not in plan and "repositoryLimits" not in plan
