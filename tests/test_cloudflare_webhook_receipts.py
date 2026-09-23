"""Signed Creem updates must have a durable receipt before acknowledgment."""
import asyncio
import hashlib
import hmac
import json
import sqlite3
from contextlib import closing

import pytest

from pullwise_server.cloudflare_webhook_receipts import D1WebhookReceipts
from pullwise_server import billing
from pullwise_server.cloudflare_account_adapter import D1AccountTransactions
from pullwise_server import creem_event_rules
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


def test_signed_receipt_rejects_normalized_id_unbound_to_raw_event(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    adapter = D1WebhookReceipts(D1ShapedSQLite(fixture.store))
    secret = "synthetic-secret"
    raw = b'{"id":"evt-real","eventType":"subscription.paid"}'
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    with pytest.raises(ValueError, match="event ID"):
        asyncio.run(adapter.record_signed_update(raw_body=raw, signature=signature,
            secret=secret, normalized_update={"eventId": "evt-other"}, now=fixture.now))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM billing_webhook_receipts").fetchone()[0] == 0


def test_signed_creem_event_uses_existing_billing_normalizer_before_receipt(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    adapter = D1WebhookReceipts(D1ShapedSQLite(fixture.store))
    secret = "synthetic-secret"
    raw = json.dumps({"id": "evt-cancel", "eventType": "subscription.canceled",
        "created_at": 1728734327355, "object": {"id": "sub-fixture",
            "metadata": {"userId": "owner"}}}, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    update = asyncio.run(adapter.record_signed_creem_event(raw_body=raw,
        signature=signature, secret=secret, normalize_event=billing.billing_update_from_creem_event,
        now=fixture.now))
    assert update["eventId"] == "evt-cancel" and update["status"] == "canceled"
    with closing(fixture.store.connect()) as db:
        saved = json.loads(db.execute("SELECT update_json FROM billing_webhook_receipts").fetchone()[0])
        assert saved == update


def test_signed_creem_event_skips_unsupported_and_never_normalizes_bad_signature(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    adapter = D1WebhookReceipts(D1ShapedSQLite(fixture.store))
    raw = b'{"id":"evt-unsupported","eventType":"other"}'
    signature = hmac.new(b"synthetic-secret", raw, hashlib.sha256).hexdigest()
    calls = []

    def normalize(event):
        calls.append(event["id"])
        return None

    with pytest.raises(ValueError, match="signature"):
        asyncio.run(adapter.record_signed_creem_event(raw_body=raw, signature="bad",
            secret="synthetic-secret", normalize_event=normalize, now=fixture.now))
    assert calls == []
    assert asyncio.run(adapter.record_signed_creem_event(raw_body=raw, signature=signature,
        secret="synthetic-secret", normalize_event=normalize, now=fixture.now)) is None
    assert calls == ["evt-unsupported"]
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM billing_webhook_receipts").fetchone()[0] == 0


def test_receipt_settlement_uses_persisted_account_without_decrypting_other_fields(tmp_path):
    fixture, _, frozen = seed(tmp_path / "domain.db")
    binding = D1ShapedSQLite(fixture.store)
    raw = json.dumps({"id": "evt-settle", "eventType": "subscription.canceled",
        "created_at": fixture.now * 1000, "object": {"id": "sub-fixture",
            "metadata": {"userId": "owner"}}}, separators=(",", ":")).encode()
    signature = hmac.new(b"synthetic-secret", raw, hashlib.sha256).hexdigest()
    asyncio.run(D1WebhookReceipts(binding).record_signed_creem_event(
        raw_body=raw, signature=signature, secret="synthetic-secret",
        normalize_event=lambda event: creem_event_rules.billing_update_from_creem_event(
            event, {"pro": (), "max": ()}), now=fixture.now))
    asyncio.run(D1AccountTransactions(binding).settle_webhook_receipt(
        receipt_event_id="evt-settle", owner_id="owner", now=fixture.now))
    with closing(fixture.store.connect()) as db:
        stored = json.loads(db.execute("SELECT value FROM app_state,json_each(payload) "
            "WHERE name='users' AND key='owner'").fetchone()[0])
        assert stored["githubAccessToken"] == json.loads(frozen)["githubAccessToken"]
        assert stored["githubIdentities"] == json.loads(frozen)["githubIdentities"]
        assert stored["billing"]["status"] == "canceled"
        events = json.loads(db.execute("SELECT payload FROM app_state WHERE name='billingEvents'").fetchone()[0])
        assert events["evt-settle"]["applied"] is True
        assert tuple(db.execute("SELECT revision,dirty FROM account_entitlement_authority").fetchone()) == (2, 1)
        assert db.execute("SELECT state FROM billing_webhook_receipts WHERE event_id='evt-settle'").fetchone()[0] == "applied"


def test_receipt_settlement_rejects_changed_receipt_between_read_and_batch(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    binding = D1ShapedSQLite(fixture.store)
    raw = b'{"id":"evt-race","eventType":"subscription.canceled"}'
    signature = hmac.new(b"synthetic-secret", raw, hashlib.sha256).hexdigest()
    asyncio.run(D1WebhookReceipts(binding).record_signed_update(
        raw_body=raw, signature=signature, secret="synthetic-secret",
        normalized_update={"eventId": "evt-race", "userId": "owner", "status": "canceled",
            "subscriptionId": "sub-fixture", "eventCreated": fixture.now}, now=fixture.now))

    def change_receipt():
        binding.before_batch = None
        with fixture.store._immediate() as db:
            db.execute("UPDATE billing_webhook_receipts SET update_json=? WHERE event_id='evt-race'",
                ('{"eventId":"evt-race","status":"active"}',))

    binding.before_batch = change_receipt
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(D1AccountTransactions(binding).settle_webhook_receipt(
            receipt_event_id="evt-race", owner_id="owner", now=fixture.now))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT state FROM billing_webhook_receipts WHERE event_id='evt-race'").fetchone()[0] == "pending"
        assert db.execute("SELECT revision FROM account_entitlement_authority").fetchone()[0] == 1
        assert db.execute("SELECT state FROM processing_usage_ledger WHERE charge_key='charge'").fetchone()[0] == "reserved"


def test_receipt_settlement_rejects_unrelated_billing_owner(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    binding = D1ShapedSQLite(fixture.store)
    raw = b'{"id":"evt-other-owner","eventType":"subscription.canceled"}'
    signature = hmac.new(b"synthetic-secret", raw, hashlib.sha256).hexdigest()
    asyncio.run(D1WebhookReceipts(binding).record_signed_update(
        raw_body=raw, signature=signature, secret="synthetic-secret",
        normalized_update={"eventId": "evt-other-owner", "userId": "elsewhere",
            "subscriptionId": "other-sub", "customerId": "other-customer",
            "status": "canceled", "eventCreated": fixture.now}, now=fixture.now))
    with pytest.raises(ValueError, match="owner"):
        asyncio.run(D1AccountTransactions(binding).settle_webhook_receipt(
            receipt_event_id="evt-other-owner", owner_id="owner", now=fixture.now))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT state FROM billing_webhook_receipts WHERE event_id='evt-other-owner'").fetchone()[0] == "pending"
        assert db.execute("SELECT revision FROM account_entitlement_authority").fetchone()[0] == 1


def test_applying_receipt_and_account_projection_dirtiness_is_atomic(tmp_path):
    fixture, _, frozen = seed(tmp_path / "domain.db")
    secret = "synthetic-secret"
    raw = b'{"id":"evt-1","eventType":"subscription.paid"}'
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    asyncio.run(D1WebhookReceipts(D1ShapedSQLite(fixture.store)).record_signed_update(
        raw_body=raw, signature=signature, secret=secret,
        normalized_update={"eventId": "evt-1"}, now=fixture.now))
    args = dict(owner_id="owner", expected_revision=1, account_snapshot=frozen,
        expected_update_json='{"eventId":"evt-1"}',
        next_account_json=frozen, expected_events_json='{"event_fixture":{"status":"processed"}}',
        next_events_json='{"event_fixture":{"status":"processed"},"evt-1":{"applied":true}}',
        expected_pending_json='[]', next_pending_json='[]', now=fixture.now)
    with pytest.raises(sqlite3.IntegrityError):
        execute(fixture.store, mapping().apply_webhook_receipt(**{**args,
            "expected_update_json": '{"eventId":"missing"}',
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
