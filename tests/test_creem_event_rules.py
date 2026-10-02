"""Payment facts require a configured product and explicit billing cadence."""
import pytest

from pullwise_server.creem_event_rules import billing_update_from_creem_event
from pullwise_server.cloudflare_creem_gateway import webhook_product_ids
from pullwise_server.billing_account_rules import reduce_billing_update


def event(kind="subscription.active", **subscription):
    return {"id": "evt-1", "eventType": kind, "created_at": 1800000000000,
        "object": {"id": "sub-1", "customer": {"id": "cust-1"}, **subscription}}


def test_missing_or_unknown_product_never_grants_a_paid_subscription():
    products = {"pro": {"month": "prod-pro"}, "max": {"month": "prod-max"}}
    for product in (None, "prod-other"):
        assert billing_update_from_creem_event(event(product=product), products) is None


def test_configured_recurring_product_without_subscription_identity_cannot_grant_access():
    products = {"pro": {"month": "prod-pro"}, "max": {}}
    checkout = {"id": "evt-checkout", "eventType": "checkout.completed", "object": {
        "id": "checkout-1", "product": "prod-pro", "customer": {"id": "customer-1"},
        "metadata": {"userId": "owner", "plan": "pro"}}}
    assert billing_update_from_creem_event(checkout, products) is None


def test_year_only_binding_does_not_depend_on_dictionary_order_or_metadata():
    products = webhook_product_ids({"pro": {"year": "prod-pro-year"}, "max": {}})
    update = billing_update_from_creem_event(event(product="prod-pro-year",
        metadata={"interval": "month", "plan": "max"}), products)
    assert update["plan"] == "pro" and update["interval"] == "year"


def test_renewal_cancellation_flags_follow_signed_subscription_status():
    products = {"pro": {"month": "prod-pro"}, "max": {}}
    scheduled = billing_update_from_creem_event(event("subscription.scheduled_cancel",
        product="prod-pro"), products)
    assert scheduled["cancelAtPeriodEnd"] is True
    resumed = billing_update_from_creem_event(event("subscription.update", status="active",
        product="prod-pro"), products)
    assert resumed["cancelAtPeriodEnd"] is False and resumed["canceledAt"] is None


@pytest.mark.parametrize("kind", ["subscription.canceled", "subscription.past_due",
                                 "subscription.unpaid", "subscription.paused"])
@pytest.mark.parametrize("metadata", [{}, {"interval": "unknown"}])
def test_terminal_event_without_verified_cadence_preserves_existing_annual_history(kind, metadata):
    user = {"id": "owner", "billing": {"provider": "creem", "subscriptionId": "sub-1",
        "customerId": "cust-1", "plan": "max", "status": "active", "interval": "year",
        "currentPeriodStart": 1790000000, "currentPeriodEnd": 1820000000}}
    update = billing_update_from_creem_event(event(kind, metadata=metadata),
        {"pro": {}, "max": {"year": "prod-max-year"}})
    assert update["interval"] is None
    decision = reduce_billing_update(user, update, processed_at=1800000010)
    assert decision["user"]["billing"]["interval"] == "year"
    assert decision["user"]["billingSubscriptions"][0]["interval"] == "year"
    assert decision["user"]["billingSubscriptionEvents"][0]["interval"] == "year"
