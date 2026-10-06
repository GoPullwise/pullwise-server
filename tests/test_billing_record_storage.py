"""Capacity and atomic replay tests over isolated per-owner/per-event state rows."""
import asyncio
import hashlib
import hmac
import json
import sqlite3
from contextlib import closing

import pytest

from ledger_d1_fixture import D1ShapedSQLite, seed
from state_record_fixtures import normalize_legacy_state
from pullwise_server.account_cycle_rules import effective_user_plan
from pullwise_server.billing_account_rules import reduce_billing_update
from pullwise_server.cloudflare_account_adapter import D1AccountTransactions
from pullwise_server.cloudflare_creem_handler import accept_signed_creem_webhook
from pullwise_server.cloudflare_github_identity_http import _repo_items
from pullwise_server.cloudflare_state_records import encode_record, record_name

PRODUCTS = {"pro": {"month": "prod-pro"}}
SECRET = "synthetic-record-storage-secret"


def fixture_state(tmp_path):
    fixture, _, frozen = seed(tmp_path / "records.db")
    with fixture.store._immediate() as db:
        normalize_legacy_state(db, now=fixture.now)
    return fixture, D1ShapedSQLite(fixture.store), json.loads(frozen)


def event(fixture, event_id, *, owner="owner", kind="subscription.paid", offset=0,
          customer="cust-record", subscription="sub_fixture"):
    return {"id": event_id, "eventType": kind, "created_at": (fixture.now + offset) * 1000,
        "object": {"id": subscription, "customer": {"id": customer},
            "product": {"id": "prod-pro", "billing_period": "every-month"},
            "current_period_start_date": fixture.now - 60,
            "current_period_end_date": fixture.now + 3600,
            "metadata": {"userId": owner}}}


def deliver(fixture, binding, payload):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return asyncio.run(accept_signed_creem_webhook(binding=binding, raw_body=raw,
        signature=signature, secret=SECRET, configured_products=PRODUCTS, now=fixture.now))


def stored_account(fixture, owner="owner"):
    with closing(fixture.store.connect()) as db:
        return json.loads(db.execute("SELECT payload FROM app_state WHERE name=?",
            (record_name("users", owner),)).fetchone()[0])


def test_signed_webhooks_preserve_maximum_history_and_repository_cache(tmp_path):
    fixture, binding, user = fixture_state(tmp_path)
    # Pure reducer builds the actual bounded product history, rather than padding
    # an arbitrary field to bypass the old global 8 KiB failure.
    for index in range(100):
        update = {"eventId": f"historical-{index}", "eventType": "subscription.paid",
            "eventCreated": fixture.now - 100 + index, "provider": "creem",
            "status": "active", "plan": "pro", "interval": "month",
            "customerId": "cust-record", "subscriptionId": "sub_fixture",
            "currentPeriodStart": fixture.now - 60, "currentPeriodEnd": fixture.now + 3600}
        user = reduce_billing_update(user, update, processed_at=fixture.now)["user"]
    repositories = _repo_items([{"id": 9007199254740991 - index,
        "full_name": "o" * 39 + "/" + "r" * 100} for index in range(1000)],
        9007199254740991, {"id": 9007199254740991, "login": "o" * 39,
                           "type": "Organization"})
    user["githubRepositoryAccess"] = {"status": "authorized", "repositoryItems": repositories}
    snapshot = encode_record("users", "owner", user)
    assert 8192 < len(snapshot.encode()) < 512 * 1024
    with fixture.store._immediate() as db:
        db.execute("UPDATE app_state SET payload=? WHERE name='record:users:owner'", (snapshot,))
        # The old global event-map boundary was already reached at 58 events.
        db.executemany("INSERT INTO app_state VALUES(?,?,?)", [
            (record_name("billingEvents", f"another-{index}"), '{"applied":true}', fixture.now)
            for index in range(60)])
    paid = event(fixture, "evt-large-paid", offset=1)
    unpaid = event(fixture, "evt-large-unpaid", kind="subscription.unpaid", offset=2)
    assert deliver(fixture, binding, paid)["state"] == "applied"
    assert deliver(fixture, binding, unpaid)["state"] == "applied"
    saved = stored_account(fixture)
    assert len(saved["billingSubscriptionEvents"]) == 100
    assert saved["githubRepositoryAccess"]["repositoryItems"] == repositories
    assert saved["githubAccessToken"] == user["githubAccessToken"]
    assert effective_user_plan(saved, timestamp=fixture.now) == "free"
    with closing(fixture.store.connect()) as db:
        revision = db.execute("SELECT revision FROM account_entitlement_authority WHERE owner_id='owner'").fetchone()[0]
    assert deliver(fixture, binding, paid)["state"] == "duplicate"
    assert effective_user_plan(stored_account(fixture), timestamp=fixture.now) == "free"
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT revision FROM account_entitlement_authority WHERE owner_id='owner'").fetchone()[0] == revision
        assert db.execute("SELECT COUNT(*) FROM app_state WHERE name GLOB 'record:billingEvents:*'").fetchone()[0] == 63
        assert db.execute("SELECT COUNT(*) FROM billing_webhook_receipts WHERE state='applied'").fetchone()[0] == 2


def test_receipts_remain_replay_authority_without_the_audit_cache(tmp_path):
    fixture, binding, _ = fixture_state(tmp_path)
    paid = event(fixture, "evt-original")
    assert deliver(fixture, binding, paid)["state"] == "applied"
    assert deliver(fixture, binding, event(fixture, "evt-unpaid", kind="subscription.unpaid", offset=1))["state"] == "applied"
    saved = stored_account(fixture)
    with fixture.store._immediate() as db:
        db.execute("DELETE FROM app_state WHERE name='record:billingEvents:evt-original'")
        proof = tuple(db.execute("SELECT raw_sha256,update_json,state FROM billing_webhook_receipts WHERE event_id='evt-original'").fetchone())
    assert deliver(fixture, binding, paid)["state"] == "duplicate"
    assert stored_account(fixture) == saved
    tampered = event(fixture, "evt-original", kind="subscription.canceled")
    with pytest.raises(sqlite3.IntegrityError):
        deliver(fixture, binding, tampered)
    with closing(fixture.store.connect()) as db:
        assert tuple(db.execute("SELECT raw_sha256,update_json,state FROM billing_webhook_receipts WHERE event_id='evt-original'").fetchone()) == proof


def test_unknown_pending_capacity_is_per_record_and_duplicate_does_not_consume_a_slot(tmp_path):
    fixture, binding, _ = fixture_state(tmp_path)
    with fixture.store._immediate() as db:
        db.executemany("INSERT INTO app_state VALUES(?,?,?)", [
            (record_name("billingPendingUpdates", f"synthetic-pending-{index}"),
             encode_record("billingPendingUpdates", f"synthetic-pending-{index}",
                {"eventId": f"synthetic-pending-{index}", "userId": "unknown-owner"}), fixture.now)
            for index in range(1000)])
    unknown = event(fixture, "evt-capacity", owner="unknown-owner", subscription="unknown-sub")
    with pytest.raises(sqlite3.IntegrityError):
        deliver(fixture, binding, unknown)
    with fixture.store._immediate() as db:
        assert db.execute("SELECT COUNT(*) FROM app_state WHERE name GLOB 'record:billingPendingUpdates:*'").fetchone()[0] == 1000
        assert db.execute("SELECT state FROM billing_webhook_receipts WHERE event_id='evt-capacity'").fetchone()[0] == "pending"
        db.execute("DELETE FROM app_state WHERE name='record:billingPendingUpdates:synthetic-pending-0'")
    assert deliver(fixture, binding, unknown)["state"] == "pending"
    assert deliver(fixture, binding, unknown)["state"] == "pending"
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM app_state WHERE name GLOB 'record:billingPendingUpdates:*'").fetchone()[0] == 1000


def test_reconciliation_reads_only_matching_pending_metadata_with_a_finite_limit(tmp_path):
    fixture, binding, owner = fixture_state(tmp_path)
    for index, kind in enumerate(("subscription.paid", "subscription.unpaid", "subscription.paid")):
        assert deliver(fixture, binding, event(fixture, f"evt-late-{index}", owner="late",
            subscription="late-sub", customer="late-customer", kind=kind, offset=index))["state"] == "pending"
    with fixture.store._immediate() as db:
        db.execute("INSERT INTO app_state VALUES(?,?,?)", (record_name("users", "late"),
            encode_record("users", "late", {"id": "late", "billing": {"plan": "free"}}), fixture.now))
    adapter = D1AccountTransactions(binding)
    asyncio.run(adapter.initialize_account(owner_id="late", now=fixture.now))
    assert asyncio.run(adapter.reconcile_pending_for_owner(owner_id="late", now=fixture.now, limit=2)) == {
        "settled": ["evt-late-0", "evt-late-1"], "remaining": 1}
    assert effective_user_plan(stored_account(fixture, "late"), timestamp=fixture.now) == "free"
    assert asyncio.run(adapter.reconcile_pending_for_owner(owner_id="late", now=fixture.now, limit=2)) == {
        "settled": ["evt-late-2"], "remaining": 0}
    assert effective_user_plan(stored_account(fixture, "late"), timestamp=fixture.now) == "pro"
    assert stored_account(fixture) == owner


def test_new_ambiguous_owner_between_lookup_and_batch_rolls_back_account_receipt_and_audit(tmp_path):
    fixture, binding, owner = fixture_state(tmp_path)
    batches = 0

    def concurrent_owner():
        nonlocal batches
        batches += 1
        if batches == 2:
            with fixture.store._immediate() as db:
                db.execute("INSERT INTO app_state VALUES(?,?,?)", (record_name("users", "another"),
                    encode_record("users", "another", {"id": "another", "billing": {"customerId": "cust-record"}}), fixture.now))

    binding.before_batch = concurrent_owner
    with pytest.raises(sqlite3.IntegrityError):
        deliver(fixture, binding, event(fixture, "evt-race"))
    assert stored_account(fixture) == owner
    with closing(fixture.store.connect()) as db:
        assert tuple(db.execute("SELECT revision,dirty FROM account_entitlement_authority WHERE owner_id='owner'").fetchone()) == (1, 0)
        assert db.execute("SELECT state FROM billing_webhook_receipts WHERE event_id='evt-race'").fetchone()[0] == "pending"
        assert db.execute("SELECT COUNT(*) FROM app_state WHERE name='record:billingEvents:evt-race'").fetchone()[0] == 0


def test_unrelated_account_event_and_pending_changes_do_not_conflict_with_owner_cas(tmp_path):
    fixture, binding, _ = fixture_state(tmp_path)
    with fixture.store._immediate() as db:
        db.execute("INSERT INTO app_state VALUES(?,?,?)", (record_name("users", "another"),
            encode_record("users", "another", {"id": "another", "name": "Before"}), fixture.now))
    batches = 0

    def unrelated_changes():
        nonlocal batches
        batches += 1
        if batches == 2:
            with fixture.store._immediate() as db:
                db.execute("UPDATE app_state SET payload=? WHERE name='record:users:another'",
                    (encode_record("users", "another", {"id": "another", "name": "After"}),))
                db.execute("INSERT INTO app_state VALUES(?,?,?)", (
                    record_name("billingPendingUpdates", "unrelated-pending"),
                    encode_record("billingPendingUpdates", "unrelated-pending",
                        {"eventId": "unrelated-pending", "userId": "unknown"}), fixture.now))
                db.execute("INSERT INTO app_state VALUES(?,?,?)", (
                    record_name("billingEvents", "unrelated-event"), '{"applied":true}', fixture.now))

    binding.before_batch = unrelated_changes
    assert deliver(fixture, binding, event(fixture, "evt-independent"))["state"] == "applied"
    assert stored_account(fixture, "another")["name"] == "After"
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM app_state WHERE name IN (?,?)",
            (record_name("billingPendingUpdates", "unrelated-pending"),
             record_name("billingEvents", "unrelated-event"))).fetchone()[0] == 2


def test_receiptless_legacy_audit_is_preserved_and_cannot_fabricate_payment_authority(tmp_path):
    fixture, binding, owner = fixture_state(tmp_path)
    # This deliberately unsupported legacy fixture has an audit fact and no
    # signed receipt. Cutover copies the audit verbatim, never inventing a hash.
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM billing_webhook_receipts WHERE event_id='event_fixture'").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError):
        deliver(fixture, binding, event(fixture, "event_fixture", kind="subscription.unpaid"))
    assert stored_account(fixture) == owner
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT payload FROM app_state WHERE name='record:billingEvents:event_fixture'").fetchone()[0] == '{"status":"processed"}'
        assert db.execute("SELECT state FROM billing_webhook_receipts WHERE event_id='event_fixture'").fetchone()[0] == 'pending'
        assert tuple(db.execute("SELECT revision,dirty FROM account_entitlement_authority WHERE owner_id='owner'").fetchone()) == (1, 0)
