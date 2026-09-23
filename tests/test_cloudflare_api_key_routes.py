"""Candidate account API-key reads stay session-only and redact secrets."""
import asyncio
import json
import pytest

from pullwise_server.cloudflare_http_contract import handle_http_request
from pullwise_server.api_key_dto_rules import (
    api_key_public_payload, requested_api_key_scopes,
    parse_api_key_restrictions,
)
from pullwise_server import app
from test_cloudflare_account_adapter import D1ShapedSQLite
from test_cloudflare_product_reads import TOKEN, _seed_auth
from test_cloudflare_server_mapping import seed


def test_api_key_projection_matches_existing_account_contract():
    record = {"id": "key-local", "name": "  Synthetic  ", "user_id": "owner",
        "key_prefix": "pwk_synthetic", "scopes": '["profile:read","usage:read"]',
        "restrictions": '{"watchIds":["watch-a","watch-a"]}',
        "created_at": 123, "expires_at": None, "last_used_at": None,
        "revoked_at": None, "key_hash": "private-hash"}
    assert api_key_public_payload(record) == app.api_key_public_payload(record)


@pytest.mark.parametrize("changes", [
    {"scopes": '["ITEMS:READ","items:read","invalid"]',
     "restrictions": '{"repositoryIds":[123," repo ","repo",null]}'},
    {"name": "bad\nname", "scopes": "invalid-json", "expires_at": "123"},
    {"restrictions": '{"kind":"audit_bundle","scanId":"scan-1","repoId":123}'},
])
def test_api_key_projection_edge_cases_match_local_contract(changes):
    record = {"id": "key-local", "name": "Synthetic", "user_id": "owner",
        "key_prefix": "pwk_synthetic", "scopes": '["profile:read"]',
        "restrictions": "{}", "created_at": 123, "expires_at": None,
        "last_used_at": None, "revoked_at": None, **changes}
    assert api_key_public_payload(record) == app.api_key_public_payload(record)


@pytest.mark.parametrize("value,provided", [
    (None, False), (["ITEMS:READ", "items:read"], True),
    (["invalid"], True), (42, True), ("watches:write", True),
])
def test_requested_scopes_match_existing_local_account_rules(value, provided):
    assert requested_api_key_scopes(value, provided=provided) == app.requested_api_key_scopes(
        value, provided=provided)


@pytest.mark.parametrize("value", [
    {"watchIds": ["watch-a", "watch-a", 42]},
    {"kind": "audit_bundle", "scanId": "scan-1", "repoId": 123},
    "invalid-json", {},
])
def test_restriction_normalization_matches_existing_local_account_rules(value):
    assert parse_api_key_restrictions(value) == app.parse_api_key_restrictions(value)


def get(binding, headers, now):
    async def no_body():
        raise AssertionError("GET must not read body")

    return asyncio.run(handle_http_request(method="GET", path="/api-keys",
        headers=headers, read_body=no_body, binding=binding,
        creem_secret="", configured_products={}, now=now))


def test_api_key_list_is_session_only_and_redacts_hash_and_token(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    binding = D1ShapedSQLite(fixture.store)
    status, payload = get(binding, {"Cookie": "pw_session=session-local"}, fixture.now)
    assert status == 200 and len(payload["items"]) == 1
    assert payload["items"] == payload["apiKeys"]
    assert payload["items"][0]["id"] == "key-local"
    assert payload["items"][0]["scopes"] == ["profile:read", "usage:read"]
    assert TOKEN not in json.dumps(payload)
    assert "key_hash" not in json.dumps(payload)
    assert get(binding, {"Authorization": f"Bearer {TOKEN}"}, fixture.now)[0] == 401


def test_api_key_list_rechecks_cookie_in_the_same_batch(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    binding = D1ShapedSQLite(fixture.store)

    def revoke_before_batch():
        with fixture.store._immediate() as db:
            db.execute("UPDATE app_state SET payload='{}' WHERE name='sessions'")

    binding.before_batch = revoke_before_batch
    status, payload = get(binding, {"Cookie": "pw_session=session-local"}, fixture.now)
    assert status == 401 and payload["error"]["code"] == "UNAUTHENTICATED"
    assert binding.batch_count == 1


def test_session_delete_revokes_api_key_without_usage_or_model_write(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    binding = D1ShapedSQLite(fixture.store)

    async def no_body():
        raise AssertionError("DELETE must not read body")

    status, payload = asyncio.run(handle_http_request(method="DELETE",
        path="/api-keys/key-local",
        headers={"Cookie": "pw_session=session-local"}, read_body=no_body,
        binding=binding, creem_secret="", configured_products={}, now=fixture.now))
    assert status == 200 and payload == {"ok": True, "id": "key-local", "revoked": True}
    assert get(binding, {"Cookie": "pw_session=session-local"}, fixture.now)[1]["items"] == []
    with fixture.store._immediate() as db:
        assert db.execute("SELECT revoked_at FROM api_keys WHERE id='key-local'").fetchone()[0] == fixture.now
        assert db.execute("SELECT COUNT(*) FROM provider_attempts").fetchone()[0] == 0


def test_api_key_delete_rolls_back_if_session_changes_before_write(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    binding = D1ShapedSQLite(fixture.store)
    batches = 0

    def revoke_before_write():
        nonlocal batches
        batches += 1
        if batches == 2:
            with fixture.store._immediate() as db:
                db.execute("UPDATE app_state SET payload='{}' WHERE name='sessions'")

    binding.before_batch = revoke_before_write

    async def no_body():
        raise AssertionError("DELETE must not read body")

    status, payload = asyncio.run(handle_http_request(method="DELETE",
        path="/api-keys/key-local",
        headers={"Cookie": "pw_session=session-local"}, read_body=no_body,
        binding=binding, creem_secret="", configured_products={}, now=fixture.now))
    assert status == 409 and payload["error"]["code"] == "AUTHORIZATION_CHANGED"
    with fixture.store._immediate() as db:
        assert db.execute("SELECT revoked_at FROM api_keys WHERE id='key-local'").fetchone()[0] is None


def test_same_site_none_key_delete_rejects_untrusted_origin_before_d1(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    binding = D1ShapedSQLite(fixture.store)

    async def no_body():
        raise AssertionError("DELETE must not read body")

    status, payload = asyncio.run(handle_http_request(method="DELETE",
        path="/api-keys/key-local",
        headers={"Cookie": "pw_session=session-local",
                 "Origin": "https://evil.example"}, read_body=no_body,
        binding=binding, creem_secret="", configured_products={}, now=fixture.now,
        cookie_same_site="None", trusted_origins={"https://app.example"}))
    assert status == 403 and payload["error"]["code"] == "UNTRUSTED_ORIGIN"
    assert binding.batch_count == 0


def test_session_create_returns_one_time_key_and_persists_only_hash(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    binding = D1ShapedSQLite(fixture.store)
    body = json.dumps({"name": "Automation", "scopes": ["items:read"],
        "restrictions": {"watchIds": ["watch-a"]}}).encode()

    async def read_body():
        return body

    status, created = asyncio.run(handle_http_request(method="POST",
        path="/api-keys", headers={"Cookie": "pw_session=session-local",
            "Content-Length": str(len(body))}, read_body=read_body,
        binding=binding, creem_secret="", configured_products={}, now=fixture.now))
    assert status == 201 and created["key"].startswith("pwk_")
    assert created["scopes"] == ["items:read"]
    assert created["restrictions"] == {"watchIds": ["watch-a"]}
    listed = get(binding, {"Cookie": "pw_session=session-local"}, fixture.now)[1]
    assert any(row["id"] == created["id"] for row in listed["items"])
    assert all("key" not in row for row in listed["items"])
    with fixture.store._immediate() as db:
        row = db.execute("SELECT key_hash,last_used_at FROM api_keys WHERE id=?", (created["id"],)).fetchone()
        assert row[0] != created["key"] and len(row[0]) == 64 and row[1] is None


def test_key_creation_rolls_back_if_session_changes_before_write(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    binding = D1ShapedSQLite(fixture.store)
    batches = 0

    def revoke_before_write():
        nonlocal batches
        batches += 1
        if batches == 2:
            with fixture.store._immediate() as db:
                db.execute("UPDATE app_state SET payload='{}' WHERE name='sessions'")

    binding.before_batch = revoke_before_write
    body = b'{"scopes":["items:read"]}'

    async def read_body():
        return body

    status, payload = asyncio.run(handle_http_request(method="POST",
        path="/api-keys", headers={"Cookie": "pw_session=session-local",
            "Content-Length": str(len(body))}, read_body=read_body,
        binding=binding, creem_secret="", configured_products={}, now=fixture.now))
    assert status == 503 and payload["error"]["code"] == "SERVER_UNAVAILABLE"
    with fixture.store._immediate() as db:
        assert db.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 1


def test_same_site_none_key_create_rejects_untrusted_origin_before_body(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    binding = D1ShapedSQLite(fixture.store)

    async def no_body():
        raise AssertionError("untrusted request must not read body")

    status, payload = asyncio.run(handle_http_request(method="POST",
        path="/api-keys", headers={"Cookie": "pw_session=session-local",
            "Origin": "https://evil.example", "Content-Length": "2"},
        read_body=no_body, binding=binding, creem_secret="",
        configured_products={}, now=fixture.now,
        cookie_same_site="None", trusted_origins={"https://app.example"}))
    assert status == 403 and payload["error"]["code"] == "UNTRUSTED_ORIGIN"
    assert binding.batch_count == 0
