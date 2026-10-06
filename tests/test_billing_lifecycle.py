"""Signed local payment lifecycles; no network traffic or real transactions."""
import asyncio
import hashlib
import hmac
import json
from contextlib import closing

import pytest

from ledger_d1_fixture import D1ShapedSQLite, seed, seed_auth
from pullwise_server.account_cycle_rules import effective_user_plan
from pullwise_server.cloudflare_billing_mutations import handle_billing_mutation
from pullwise_server.cloudflare_creem_handler import accept_signed_creem_webhook


PRODUCTS = {"pro": {"month": "prod-pro", "year": "prod-pro-year"},
            "max": {"month": "prod-max", "year": "prod-max-year"}}
SECRET = "synthetic-lifecycle-secret"
ORIGIN = "https://preview.example.test"


class Provider:
    def __init__(self):
        self.calls = []

    async def post(self, path, payload):
        self.calls.append((path, payload))
        if path == "v1/checkouts":
            return {"id": "ch_local", "checkout_url": "https://test-checkout.creem.io/ch_local"}
        return {"id": "sub_local", "status": "scheduled_cancel" if path.endswith("cancel") else "active"}


def account(fixture):
    with closing(fixture.store.connect()) as db:
        return json.loads(db.execute("SELECT payload FROM app_state WHERE name='users'").fetchone()[0])["owner"]


def mutation(fixture, binding, provider, path, body):
    return asyncio.run(handle_billing_mutation(binding=binding, gateway=provider,
        now=fixture.now, method="POST", path=path,
        headers={"Cookie": "pw_session=session-local", "Origin": ORIGIN}, body=body,
        app_url=ORIGIN, trusted_origins={ORIGIN}, products=PRODUCTS))


def deliver(fixture, binding, event):
    raw = json.dumps(event, separators=(",", ":")).encode()
    signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return asyncio.run(accept_signed_creem_webhook(binding=binding, raw_body=raw,
        signature=signature, secret=SECRET, configured_products=PRODUCTS, now=fixture.now))


def subscription_event(fixture, event_id, kind, *, created_offset=0, plan="pro", interval="month", status=None):
    return {"id": event_id, "eventType": kind, "created_at": (fixture.now + created_offset) * 1000,
        "object": {"id": "sub_local", "product": PRODUCTS[plan][interval],
            "customer": {"id": "cust_local"}, "status": status,
            "current_period_start_date": fixture.now - 60,
            "current_period_end_date": fixture.now + 3600,
            "metadata": {"userId": "owner"}}}


def free_fixture(tmp_path):
    fixture, _, frozen = seed(tmp_path / "billing.db")
    seed_auth(fixture)
    user = {**json.loads(frozen), "billing": {"plan": "free", "status": "none"}}
    with fixture.store._immediate() as db:
        db.execute("UPDATE app_state SET payload=? WHERE name='users'", (json.dumps({"owner": user}),))
        db.execute("UPDATE account_entitlement_authority SET plan='free' WHERE owner_id='owner'")
        db.execute("INSERT INTO expense_categories VALUES(?,?,?,?,?,?,?,?)",
            ("cat_local", "owner", "Hosting", None, None, 1, "2026-10-06", "2026-10-06"))
        db.execute("""INSERT INTO expenses(id,owner_id,target_kind,category_id,occurred_on,
            amount_minor,currency,purpose,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("expense_local", "owner", "shared", "cat_local", "2026-10-06", 2500, "USD",
             "Existing hosting expense", "2026-10-06", "2026-10-06"))
    return fixture, D1ShapedSQLite(fixture.store), Provider()


def test_checkout_success_upgrade_failure_and_recovery_stay_webhook_driven(tmp_path):
    fixture, binding, provider = free_fixture(tmp_path)
    status, checkout = mutation(fixture, binding, provider, "/billing/checkout-sessions",
        {"plan": "pro", "interval": "month"})
    assert status == 200 and checkout["url"].startswith("https://test-checkout.creem.io/")
    assert effective_user_plan(account(fixture), timestamp=fixture.now) == "free"
    signed_checkout = {"id": "evt-checkout", "eventType": "checkout.completed",
        "created_at": fixture.now * 1000, "object": {"id": "ch_local",
            "request_id": checkout["requestId"], "subscription": {
                "id": "sub_local", "status": "active", "product": "prod-pro",
                "customer": "cust_local", "current_period_start_date": fixture.now - 60,
                "current_period_end_date": fixture.now + 3600}}}
    assert deliver(fixture, binding, signed_checkout)["state"] == "applied"
    assert effective_user_plan(account(fixture), timestamp=fixture.now) == "pro"
    assert account(fixture)["billingCheckout"]["status"] == "completed"
    assert deliver(fixture, binding, signed_checkout)["state"] == "duplicate"
    status, change = mutation(fixture, binding, provider, "/billing/change-interval",
        {"plan": "max", "interval": "year"})
    assert status == 200 and change["pending"] is True
    assert effective_user_plan(account(fixture), timestamp=fixture.now) == "pro"
    assert mutation(fixture, binding, provider, "/billing/change-interval",
        {"plan": "max", "interval": "year"})[1]["pending"] is True
    assert len(provider.calls) == 2
    assert mutation(fixture, binding, provider, "/billing/cancel-subscription", {}) == (
        409, {"error": {"code": "SUBSCRIPTION_CHANGE_PENDING"}})
    failed = subscription_event(fixture, "evt-upgrade-failed", "subscription.unpaid",
        created_offset=1, plan="max", interval="year", status="unpaid")
    assert deliver(fixture, binding, failed)["state"] == "applied"
    assert effective_user_plan(account(fixture), timestamp=fixture.now) == "free"
    assert "billingChange" not in account(fixture)
    restored = subscription_event(fixture, "evt-payment-recovered", "subscription.paid",
        created_offset=2, plan="max", interval="year", status="active")
    assert deliver(fixture, binding, restored)["state"] == "applied"
    assert effective_user_plan(account(fixture), timestamp=fixture.now) == "max"
    assert account(fixture)["billing"]["interval"] == "year"
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT plan FROM account_entitlement_authority WHERE owner_id='owner'").fetchone()[0] == "max"
        assert tuple(db.execute("SELECT id,amount_minor,purpose,revision,deleted_at FROM expenses").fetchone()) == (
            "expense_local", 2500, "Existing hosting expense", 1, None)
        assert db.execute("SELECT count(*) FROM expense_events").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM expense_categories").fetchone()[0] == 1


@pytest.mark.parametrize("kind,status", [
    ("subscription.past_due", "past_due"), ("subscription.unpaid", "unpaid"),
    ("subscription.paused", "paused"), ("subscription.canceled", "canceled"),
    ("subscription.expired", "canceled"),
    ("dispute.created", "past_due"),
])
def test_signed_payment_failure_revokes_paid_access_and_later_paid_event_recovers(tmp_path, kind, status):
    fixture, binding, _ = free_fixture(tmp_path)
    active = subscription_event(fixture, "evt-active", "subscription.active", status="active")
    assert deliver(fixture, binding, active)["state"] == "applied"
    failure = subscription_event(fixture, "evt-failure", kind, created_offset=1, status=status)
    if kind == "dispute.created":
        failure["object"] = {"id": "dispute_local", "subscription": failure["object"]}
    assert deliver(fixture, binding, failure)["state"] == "applied"
    assert effective_user_plan(account(fixture), timestamp=fixture.now) == "free"
    recovered = subscription_event(fixture, "evt-recovered", "subscription.paid", created_offset=2, status="active")
    assert deliver(fixture, binding, recovered)["state"] == "applied"
    assert effective_user_plan(account(fixture), timestamp=fixture.now) == "pro"
    assert deliver(fixture, binding, failure)["state"] == "duplicate"
    assert effective_user_plan(account(fixture), timestamp=fixture.now) == "pro"


def test_scheduled_cancellation_resume_and_period_end_are_consistent(tmp_path):
    fixture, binding, provider = free_fixture(tmp_path)
    deliver(fixture, binding, subscription_event(fixture, "evt-active", "subscription.active", status="active"))
    assert mutation(fixture, binding, provider, "/billing/cancel-subscription", {})[1]["status"] == "canceling"
    assert effective_user_plan(account(fixture), timestamp=fixture.now) == "pro"
    assert effective_user_plan(account(fixture), timestamp=fixture.now + 3600) == "free"
    assert mutation(fixture, binding, provider, "/billing/resume-subscription", {})[1]["status"] == "active"
    assert effective_user_plan(account(fixture), timestamp=fixture.now) == "pro"
    assert account(fixture)["billing"]["cancelAtPeriodEnd"] is False
    assert len(provider.calls) == 2


def test_partial_refund_preserves_access_and_full_refund_revokes_it(tmp_path):
    fixture, binding, _ = free_fixture(tmp_path)
    deliver(fixture, binding, subscription_event(fixture, "evt-active", "subscription.active", status="active"))
    for event_id, transaction_status, subscription_status, expected_state, expected_plan in (
        ("evt-partial-refund", "partialRefund", "active", "ignored", "pro"),
        ("evt-full-refund", "refunded", "canceled", "applied", "free"),
    ):
        event = subscription_event(fixture, event_id, "refund.created", created_offset=1,
            status=subscription_status)
        event["object"] = {"id": "refund_local", "subscription": event["object"],
            "transaction": {"subscription": "sub_local", "status": transaction_status}}
        assert deliver(fixture, binding, event)["state"] == expected_state
        assert effective_user_plan(account(fixture), timestamp=fixture.now) == expected_plan
