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


def test_old_subscription_cancellation_cannot_revoke_current_subscription():
    account = _account()
    account["billing"]["plan"] = "max"
    decision = billing_account_rules.reduce_billing_update(account,
        {"eventId": "evt-old-cancel", "eventType": "subscription.canceled",
         "eventCreated": 200, "customerId": "cust-1", "subscriptionId": "old-sub",
         "status": "canceled", "plan": "pro"}, processed_at=300)
    assert decision["user"]["billing"] == account["billing"]
    assert decision["quotaRefresh"] is False
    assert decision["user"]["billingSubscriptionEvents"][0]["subscriptionId"] == "old-sub"


def test_signed_upgrade_completes_pending_change_and_clears_cancellation():
    account = _account()
    account["billing"].update(cancelAtPeriodEnd=True, canceledAt=90)
    account["billingChange"] = {"plan": "max", "interval": "year", "subscriptionId": "sub-1"}
    decision = billing_account_rules.reduce_billing_update(account,
        {"eventId": "evt-upgrade", "eventType": "subscription.update",
         "eventCreated": 200, "subscriptionId": "sub-1", "status": "active",
         "plan": "max", "interval": "year", "cancelAtPeriodEnd": False,
         "canceledAt": None}, processed_at=300)
    assert "billingChange" not in decision["user"]
    assert decision["user"]["billing"]["cancelAtPeriodEnd"] is False
    assert decision["user"]["billing"]["canceledAt"] is None


def test_old_subscription_renewal_does_not_replace_current_subscription():
    account = _account()
    account["billingSubscriptions"] = [{"subscriptionId": "old-sub", "plan": "pro", "status": "active"}]
    decision = billing_account_rules.reduce_billing_update(account,
        {"eventId": "evt-old-paid", "eventType": "subscription.paid", "eventCreated": 200,
         "subscriptionId": "old-sub", "status": "active", "plan": "pro"}, processed_at=300)
    assert decision["user"]["billing"] == account["billing"]
    assert decision["quotaRefresh"] is False


def test_late_unknown_subscription_cannot_replace_a_newer_current_subscription():
    account = _account()
    account["billing"].update(plan="max", interval="year", lastEventCreated=300)
    decision = billing_account_rules.reduce_billing_update(account,
        {"eventId": "evt-delayed-old-paid", "eventType": "subscription.paid", "eventCreated": 200,
         "subscriptionId": "unseen-old-sub", "status": "active", "plan": "pro", "interval": "month"},
        processed_at=400)
    assert decision["user"]["billing"] == account["billing"]
    assert decision["applied"] is False and decision["quotaRefresh"] is False
    assert decision["eventRecord"]["stale"] is True
    assert decision["user"]["billingSubscriptionEvents"][0]["subscriptionId"] == "unseen-old-sub"


def test_new_subscription_does_not_inherit_expired_period_or_item_identity():
    account = _account()
    account["billing"].update(status="canceled", currentPeriodEnd=150, subscriptionItemId="old-item")
    decision = billing_account_rules.reduce_billing_update(account,
        {"eventId": "evt-new-paid", "eventType": "subscription.paid", "eventCreated": 200,
         "subscriptionId": "new-sub", "status": "active", "plan": "max"}, processed_at=300)
    assert decision["user"]["billing"]["subscriptionId"] == "new-sub"
    assert decision["user"]["billing"]["currentPeriodEnd"] is None
    assert decision["user"]["billing"]["subscriptionItemId"] is None
