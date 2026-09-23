"""Signed Creem updates must have a durable receipt before acknowledgment."""
import asyncio
import hashlib
import hmac
import json
import sqlite3
from contextlib import closing

import pytest

from pullwise_server.cloudflare_webhook_receipts import D1WebhookReceipts
from pullwise_server.cloudflare_account_adapter import D1AccountTransactions
from test_cloudflare_account_adapter import D1ShapedSQLite
from test_cloudflare_server_mapping import seed, mapping, execute


def test_signed_receipt_is_durable_deduplicated_and_conflict_safe(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    adapter = D1WebhookReceipts(D1ShapedSQLite(fixture.store))
    secret = "synthetic-secret"
    raw = b'{"id":"evt-1","eventType":"subscription.paid"}'
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    update = {"eventId": "evt-1", "customerId": "synthetic-customer"}
    with pytest.raises(ValueError, match="signature"):
        asyncio.run(adapter.record_signed_update(raw_body=raw, signature="bad", secret=secret,
            normalized_update=update, now=fixture.now))
    asyncio.run(adapter.record_signed_update(raw_body=raw, signature=signature, secret=secret,
        normalized_update=update, now=fixture.now))
    asyncio.run(adapter.record_signed_update(raw_body=raw, signature=signature, secret=secret,
        normalized_update=update, now=fixture.now))
    other = b'{"id":"evt-1","eventType":"subscription.canceled"}'
    other_signature = hmac.new(secret.encode(), other, hashlib.sha256).hexdigest()
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(adapter.record_signed_update(raw_body=other, signature=other_signature,
            secret=secret, normalized_update=update, now=fixture.now + 1))
    with closing(fixture.store.connect()) as db:
        row = db.execute("SELECT event_id,raw_sha256,update_json FROM billing_webhook_receipts").fetchone()
        assert row[0] == "evt-1" and row[1] == hashlib.sha256(raw).hexdigest()
        assert json.loads(row[2]) == update
        assert db.execute("SELECT COUNT(*) FROM billing_webhook_receipts").fetchone()[0] == 1


def test_applying_receipt_and_account_projection_dirtiness_is_atomic(tmp_path):
    fixture, _, frozen = seed(tmp_path / "domain.db")
    secret = "synthetic-secret"
    raw = b'{"id":"evt-1","eventType":"subscription.paid"}'
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    asyncio.run(D1WebhookReceipts(D1ShapedSQLite(fixture.store)).record_signed_update(
        raw_body=raw, signature=signature, secret=secret,
        normalized_update={"eventId": "evt-1"}, now=fixture.now))
    args = dict(owner_id="owner", expected_revision=1, account_snapshot=frozen,
        next_account_json=frozen, expected_events_json='{"event_fixture":{"status":"processed"}}',
        next_events_json='{"event_fixture":{"status":"processed"},"evt-1":{"applied":true}}',
        expected_pending_json='[]', next_pending_json='[]', now=fixture.now)
    with pytest.raises(sqlite3.IntegrityError):
        execute(fixture.store, mapping().apply_webhook_receipt(**{**args,
            "next_events_json": '{"event_fixture":{"status":"processed"},"missing":{"applied":true}}'},
            receipt_event_id="missing"))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT revision FROM account_entitlement_authority").fetchone()[0] == 1
        assert db.execute("SELECT state FROM billing_webhook_receipts").fetchone()[0] == "pending"
    asyncio.run(D1AccountTransactions(D1ShapedSQLite(fixture.store)).apply_webhook_receipt(
        receipt_event_id="evt-1", owner_id="owner", expected_revision=1,
        next_account_json=frozen, next_events_json=args["next_events_json"],
        next_pending_json='[]', now=fixture.now))
    with pytest.raises(sqlite3.IntegrityError):
        execute(fixture.store, mapping().apply_webhook_receipt(**args, receipt_event_id="evt-1"))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT state FROM billing_webhook_receipts").fetchone()[0] == "applied"
        assert tuple(db.execute("SELECT revision,dirty FROM account_entitlement_authority").fetchone()) == (2, 1)
        assert json.loads(db.execute("SELECT payload FROM app_state WHERE name='billingEvents'").fetchone()[0])["evt-1"] == {"applied": True}
