"""The candidate Server Worker keeps the existing Creem HTTP boundary."""
import asyncio
import hashlib
import hmac
import json
import sqlite3
from contextlib import closing

from pullwise_server.cloudflare_http_contract import handle_http_request
from test_cloudflare_account_adapter import D1ShapedSQLite
from test_cloudflare_server_mapping import seed


def _request(binding, *, method, path, raw=b"", signature=None, secret="synthetic-secret",
             products=None, content_length=None):
    reads = []

    async def read_body():
        reads.append(True)
        return raw

    headers = {"Content-Length": str(len(raw) if content_length is None else content_length)}
    if signature is not None:
        headers["creem-signature"] = signature
    result = asyncio.run(handle_http_request(method=method, path=path,
        headers=headers, read_body=read_body, binding=binding,
        creem_secret=secret, configured_products=products or {"pro": (), "max": ()},
        now=1800000000))
    return result, reads


def test_candidate_webhook_uses_raw_signature_and_existing_ack_shape(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    binding = D1ShapedSQLite(fixture.store)
    raw = json.dumps({"id": "evt-http", "eventType": "subscription.canceled",
        "object": {"id": "sub_fixture", "metadata": {"userId": "owner"}}},
        separators=(",", ":")).encode()
    signature = hmac.new(b"synthetic-secret", raw, hashlib.sha256).hexdigest()
    (status, payload), reads = _request(binding, method="POST", path="/webhooks/creem",
        raw=raw, signature=signature)
    assert (status, payload) == (200, {"received": True}) and reads == [True]
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT state FROM billing_webhook_receipts WHERE event_id='evt-http'").fetchone()[0] == "applied"


def test_candidate_webhook_rejects_bad_body_before_d1_and_hides_errors(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    binding = D1ShapedSQLite(fixture.store)
    (status, payload), reads = _request(binding, method="POST", path="/webhooks/creem",
        raw=b"x" * 5, signature="bad", content_length=65537)
    assert status == 413 and reads == [] and "x" not in json.dumps(payload)
    (status, payload), reads = _request(binding, method="POST", path="/webhooks/creem",
        raw=b'{"id":"evt-bad"}', signature="bad")
    assert status == 400 and reads == [True] and "signature" not in json.dumps(payload)
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM billing_webhook_receipts").fetchone()[0] == 0


def test_candidate_worker_has_read_only_health_and_no_fake_product_api(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    with fixture.store._immediate() as db:
        db.execute("CREATE TABLE api_keys(id TEXT PRIMARY KEY,key_hash TEXT)")
    binding = D1ShapedSQLite(fixture.store)
    before = binding.batch_count
    (status, health), reads = _request(binding, method="GET", path="/health")
    assert status == 200 and health["ok"] is True and health["service"] == "pullwise-server"
    assert reads == [] and binding.batch_count == before
    (status, payload), reads = _request(binding, method="GET", path="/api/v1/items")
    assert status == 404 and reads == [] and binding.batch_count == before


def test_health_rejects_incomplete_d1_auth_schema(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    binding = D1ShapedSQLite(fixture.store)
    status, payload = _get_health(binding)
    assert status == 503 and payload["ok"] is False


def _get_health(binding):
    async def no_body():
        raise AssertionError("health must not read a body")

    return asyncio.run(handle_http_request(method="GET", path="/health",
        headers={}, read_body=no_body, binding=binding,
        creem_secret="", configured_products={}, now=1800000000))


def test_candidate_webhook_rejects_missing_config_and_malformed_signed_json(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    binding = D1ShapedSQLite(fixture.store)
    (status, payload), reads = _request(binding, method="POST", path="/webhooks/creem",
        raw=b"{}", secret="")
    assert status == 503 and reads == [] and payload["error"]["code"] == "BILLING_UNAVAILABLE"
    raw = b"not-json"
    signature = hmac.new(b"synthetic-secret", raw, hashlib.sha256).hexdigest()
    (status, payload), reads = _request(binding, method="POST", path="/webhooks/creem",
        raw=raw, signature=signature)
    assert status == 400 and reads == [True] and payload["error"]["code"] == "INVALID_REQUEST"
    assert binding.batch_count == 0


def test_http_retry_repairs_projection_after_durable_receipt(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    binding = D1ShapedSQLite(fixture.store)
    raw = b'{"id":"evt-http-retry","eventType":"subscription.canceled","object":{"id":"sub_fixture","metadata":{"userId":"owner"}}}'
    signature = hmac.new(b"synthetic-secret", raw, hashlib.sha256).hexdigest()
    batches = 0

    def fail_refresh_once():
        nonlocal batches
        batches += 1
        if batches == 3:
            binding.before_batch = None
            raise sqlite3.OperationalError("synthetic refresh failure")

    binding.before_batch = fail_refresh_once
    (status, payload), _ = _request(binding, method="POST", path="/webhooks/creem",
        raw=raw, signature=signature)
    assert status == 503 and payload == {"error": {"code": "BILLING_RETRY"}}
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT state FROM billing_webhook_receipts "
            "WHERE event_id='evt-http-retry'").fetchone()[0] == "applied"
        assert db.execute("SELECT dirty FROM account_entitlement_authority").fetchone()[0] == 1
    assert _request(binding, method="POST", path="/webhooks/creem",
        raw=raw, signature=signature)[0] == (200, {"received": True})
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT dirty FROM account_entitlement_authority").fetchone()[0] == 0
