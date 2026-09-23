"""Pure Billing account projection preserves payment facts and product usage."""
from unittest.mock import patch

from pullwise_server import app, db
from pullwise_server.entitlements import product_usage_payload
from pullwise_server.product_billing_projection import billing_account_dto
from pullwise_server.product_store import ProductStore


def test_pure_billing_projection_matches_local_account_and_payment_history(tmp_path):
    path = tmp_path / "billing.db"
    store = ProductStore(path)
    store.initialize()
    user = {"id": "owner", "billing": {"provider": "creem", "plan": "pro",
        "status": "active", "interval": "year", "customerId": "customer-1",
        "subscriptionId": "sub-1"},
        "billingSubscriptionEvents": [{"provider": "creem", "subscriptionId": "sub-1",
            "status": "active", "plan": "pro", "interval": "year",
            "eventType": "subscription.paid", "eventId": "evt-1",
            "eventCreated": 100, "processedAt": 101, "stale": False}]}
    with patch.dict("os.environ", {"PULLWISE_DB_PATH": str(path)}):
        db.reset_initialization_cache()
        local = app.billing_account_payload(user)
    pure = billing_account_dto(user, product_usage_payload(store, user), [])
    assert pure == local
    assert pure["subscriptionEvents"][0]["eventId"] == "evt-1"
