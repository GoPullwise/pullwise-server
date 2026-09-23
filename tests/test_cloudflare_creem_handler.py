"""Trusted Creem composition over the local async D1 boundary."""
import asyncio
import hashlib
import hmac
import json
import sqlite3
from contextlib import closing

import pytest

from pullwise_server.cloudflare_creem_handler import accept_signed_creem_webhook
from test_cloudflare_account_adapter import D1ShapedSQLite
from test_cloudflare_server_mapping import seed


PRODUCTS = {"pro": ("prod-pro",), "max": ("prod-max",)}


def _signed(raw: bytes) -> str:
    return hmac.new(b"synthetic-secret", raw, hashlib.sha256).hexdigest()


def test_signed_paid_upgrade_settles_and_refreshes_existing_bucket(tmp_path):
    fixture, _, frozen = seed(tmp_path / "domain.db")
    binding = D1ShapedSQLite(fixture.store)
    raw = json.dumps({"id": "evt-upgrade", "eventType": "subscription.paid",
        "created_at": fixture.now * 1000, "object": {"id": "sub_fixture",
            "status": "active", "customer": {"id": "cust-fixture"},
            "product": {"id": "prod-max", "billing_period": "every-month"},
            "metadata": {"userId": "owner", "plan": "max"}}},
        separators=(",", ":")).encode()
    args = dict(binding=binding, raw_body=raw, signature=_signed(raw),
        secret="synthetic-secret", configured_products=PRODUCTS, now=fixture.now)
    assert asyncio.run(accept_signed_creem_webhook(**args)) == {
        "received": True, "state": "applied", "eventId": "evt-upgrade"}
    with closing(fixture.store.connect()) as db:
        user = json.loads(db.execute("SELECT value FROM app_state,json_each(payload) "
            "WHERE name='users' AND key='owner'").fetchone()[0])
        assert user["billing"]["plan"] == "max"
        assert user["githubAccessToken"] == json.loads(frozen)["githubAccessToken"]
        assert tuple(db.execute("SELECT revision,dirty,plan FROM account_entitlement_authority").fetchone()) == (3, 0, "max")
        assert tuple(db.execute("SELECT used,reserved,limit_value FROM processing_usage_buckets").fetchone()) == (0, 1, 25000)
        assert db.execute("SELECT state FROM billing_webhook_receipts WHERE event_id='evt-upgrade'").fetchone()[0] == "applied"
    assert asyncio.run(accept_signed_creem_webhook(**args)) == {
        "received": True, "state": "duplicate", "eventId": "evt-upgrade"}
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT revision FROM account_entitlement_authority").fetchone()[0] == 3


def test_signed_event_without_owner_is_durably_parked(tmp_path):
    fixture, _, frozen = seed(tmp_path / "domain.db")
    binding = D1ShapedSQLite(fixture.store)
    raw = b'{"id":"evt-early","eventType":"subscription.canceled","object":{"id":"new-sub","customer":{"id":"new-customer"}}}'
    result = asyncio.run(accept_signed_creem_webhook(binding=binding,
        raw_body=raw, signature=_signed(raw), secret="synthetic-secret",
        configured_products=PRODUCTS, now=fixture.now))
    assert result == {"received": True, "state": "pending", "eventId": "evt-early"}
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT state FROM billing_webhook_receipts WHERE event_id='evt-early'").fetchone()[0] == "pending"
        assert len(json.loads(db.execute("SELECT payload FROM app_state WHERE name='billingPendingUpdates'").fetchone()[0])) == 1
        assert db.execute("SELECT value FROM app_state,json_each(payload) "
            "WHERE name='users' AND key='owner'").fetchone()[0] == frozen
        assert db.execute("SELECT revision FROM account_entitlement_authority").fetchone()[0] == 1


def test_bad_signature_has_no_durable_effect(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    binding = D1ShapedSQLite(fixture.store)
    raw = b'{"id":"evt-bad","eventType":"subscription.canceled"}'
    with pytest.raises(ValueError, match="signature"):
        asyncio.run(accept_signed_creem_webhook(binding=binding, raw_body=raw,
            signature="bad", secret="synthetic-secret",
            configured_products=PRODUCTS, now=fixture.now))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM billing_webhook_receipts").fetchone()[0] == 0


def test_duplicate_delivery_repairs_dirty_projection_after_refresh_failure(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    binding = D1ShapedSQLite(fixture.store)
    raw = json.dumps({"id": "evt-refresh-retry", "eventType": "subscription.paid",
        "object": {"id": "sub_fixture", "status": "active",
            "product": {"id": "prod-max"},
            "metadata": {"userId": "owner", "plan": "max"}}}).encode()
    args = dict(binding=binding, raw_body=raw, signature=_signed(raw),
        secret="synthetic-secret", configured_products=PRODUCTS, now=fixture.now)
    batches = 0

    def fail_refresh_once():
        nonlocal batches
        batches += 1
        if batches == 3:
            binding.before_batch = None
            raise sqlite3.OperationalError("synthetic refresh failure")

    binding.before_batch = fail_refresh_once
    with pytest.raises(sqlite3.OperationalError, match="synthetic refresh failure"):
        asyncio.run(accept_signed_creem_webhook(**args))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT state FROM billing_webhook_receipts "
            "WHERE event_id='evt-refresh-retry'").fetchone()[0] == "applied"
        assert tuple(db.execute("SELECT revision,dirty FROM account_entitlement_authority").fetchone()) == (2, 1)
        assert db.execute("SELECT limit_value FROM processing_usage_buckets").fetchone()[0] == 5000
    assert asyncio.run(accept_signed_creem_webhook(**args))["state"] == "duplicate"
    with closing(fixture.store.connect()) as db:
        assert tuple(db.execute("SELECT revision,dirty FROM account_entitlement_authority").fetchone()) == (3, 0)
        assert db.execute("SELECT limit_value FROM processing_usage_buckets").fetchone()[0] == 25000


def test_oversized_body_never_reaches_d1(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    binding = D1ShapedSQLite(fixture.store)
    with pytest.raises(ValueError, match="body size"):
        asyncio.run(accept_signed_creem_webhook(binding=binding,
            raw_body=b"x" * (64 * 1024 + 1), signature="bad",
            secret="synthetic-secret", configured_products=PRODUCTS,
            now=fixture.now))
    assert binding.batch_count == 0


def test_ambiguous_customer_binding_does_not_charge_an_arbitrary_owner(tmp_path):
    fixture, _, frozen = seed(tmp_path / "domain.db")
    binding = D1ShapedSQLite(fixture.store)
    first = {**json.loads(frozen), "billing": {
        **json.loads(frozen)["billing"], "customerId": "shared-customer"}}
    second = {"id": "another", "billing": {"customerId": "shared-customer"}}
    with fixture.store._immediate() as db:
        db.execute("UPDATE app_state SET payload=? WHERE name='users'",
            (json.dumps({"owner": first, "another": second}),))
    raw = b'{"id":"evt-ambiguous","eventType":"subscription.canceled","object":{"id":"new-sub","customer":{"id":"shared-customer"}}}'
    with pytest.raises(ValueError, match="ambiguous"):
        asyncio.run(accept_signed_creem_webhook(binding=binding,
            raw_body=raw, signature=_signed(raw), secret="synthetic-secret",
            configured_products=PRODUCTS, now=fixture.now))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT state FROM billing_webhook_receipts "
            "WHERE event_id='evt-ambiguous'").fetchone()[0] == "pending"
        assert db.execute("SELECT revision FROM account_entitlement_authority").fetchone()[0] == 1
        assert db.execute("SELECT payload FROM app_state WHERE name='billingEvents'").fetchone()[0] == '{"event_fixture":{"status":"processed"}}'
