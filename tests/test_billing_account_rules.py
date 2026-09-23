"""A billing event decision must be reusable without app globals or SQLite."""
from copy import deepcopy

from pullwise_server import billing_account_rules


def _account():
    return {"id": "owner", "billing": {"provider": "creem", "plan": "pro",
        "status": "active", "subscriptionId": "sub-1", "customerId": "cust-1",
        "lastEventId": "evt-old", "lastEventCreated": 100},
        "billingCheckout": {"requestId": "req-1", "status": "pending"}}


def test_applied_billing_decision_preserves_input_and_checkout_history():
    account = _account()
    original = deepcopy(account)
    update = {"eventId": "evt-paid", "eventType": "subscription.paid",
        "eventCreated": 200, "provider": "creem", "customerId": "cust-1",
        "subscriptionId": "sub-1", "status": "active", "plan": "max",
        "interval": "year", "requestId": "req-1"}
    decision = billing_account_rules.reduce_billing_update(account, update, processed_at=300)
    assert account == original
    assert decision["applied"] is True and decision["quotaRefresh"] is True
    assert decision["user"]["billing"]["plan"] == "max"
    assert decision["user"]["billingCheckout"] == {"requestId": "req-1",
        "status": "completed", "completedAt": 300, "eventId": "evt-paid"}
    assert decision["user"]["billingSubscriptions"][0]["lastEventId"] == "evt-paid"
    assert decision["user"]["billingSubscriptionEvents"][0]["eventId"] == "evt-paid"
    assert decision["eventRecord"] == {"eventType": "subscription.paid",
        "eventCreated": 200, "processedAt": 300, "applied": True, "stale": False}


def test_stale_billing_decision_keeps_current_plan_and_audits_event():
    account = _account()
    decision = billing_account_rules.reduce_billing_update(account,
        {"eventId": "evt-late", "eventType": "subscription.canceled",
         "eventCreated": 99, "subscriptionId": "sub-1", "status": "canceled"},
        processed_at=300)
    assert decision["applied"] is False and decision["quotaRefresh"] is False
    assert decision["user"]["billing"] == account["billing"]
    assert decision["user"]["billingSubscriptionEvents"][0]["stale"] is True
    assert decision["eventRecord"] == {"eventType": "subscription.canceled",
        "eventCreated": 99, "processedAt": 300, "applied": False, "stale": True}
