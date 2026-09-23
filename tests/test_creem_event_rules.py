"""The Creem event decision is shared by Server and the Worker adapter."""
from unittest.mock import patch

from pullwise_server import billing
from pullwise_server import creem_event_rules


PRODUCTS = {"pro": ("prod_monthly", "prod_yearly"), "max": ("prod_max_monthly",)}


def test_pure_creem_normalizer_matches_existing_billing_entry_for_lifecycle_events():
    events = [
        {"id": "evt-paid", "eventType": "subscription.paid", "created_at": 1728734327355,
         "object": {"id": "sub-1", "status": "active", "customer": {"id": "cust-1"},
                    "product": {"id": "prod_monthly", "billing_period": "every-month"},
                    "metadata": {"userId": "owner", "plan": "pro"}}},
        {"id": "evt-cancel", "eventType": "subscription.canceled",
         "object": {"id": "sub-1", "metadata": {"userId": "owner"}}},
        {"id": "evt-unknown-product", "eventType": "subscription.paid",
         "object": {"id": "sub-1", "status": "active", "customer": {"id": "cust-1"},
                    "product": {"id": "unknown"}, "metadata": {"userId": "owner", "plan": "pro"}}},
        {"id": "evt-ignored", "eventType": "other", "object": {}},
    ]
    with patch("pullwise_server.system_config.creem_product_ids_for_plan",
               side_effect=lambda plan: PRODUCTS.get(plan, ())):
        results = [creem_event_rules.billing_update_from_creem_event(event, PRODUCTS)
                   for event in events]
        assert results == [billing.billing_update_from_creem_event(event) for event in events]
    assert (results[0]["plan"], results[0]["status"], results[0]["interval"]) == \
        ("pro", "active", "month")
    assert results[0]["eventCreated"] == 1728734327.355
    assert results[1]["status"] == "canceled"
    assert results[2:] == [None, None]


def test_pure_creem_normalizer_uses_supplied_product_binding_for_paid_access():
    event = {"id": "evt-paid", "eventType": "subscription.paid",
        "object": {"id": "sub-1", "status": "active",
            "product": {"id": "prod_monthly"}, "metadata": {"userId": "owner", "plan": "pro"}}}
    assert creem_event_rules.billing_update_from_creem_event(event, PRODUCTS)["plan"] == "pro"
    assert creem_event_rules.billing_update_from_creem_event(event,
        {"pro": (), "max": ()}) is None
    yearly = {**event, "object": {**event["object"],
        "product": {"id": "prod_yearly"}}}
    assert creem_event_rules.billing_update_from_creem_event(yearly, PRODUCTS)["interval"] == "year"
