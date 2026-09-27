"""Candidate account API-key reads stay session-only and redact secrets."""
import asyncio
import json
import pytest

from pullwise_server.cloudflare_http_contract import handle_http_request
from pullwise_server.api_key_dto_rules import (
    api_key_public_payload, requested_api_key_scopes,
    parse_api_key_restrictions,
)
from ledger_d1_fixture import D1ShapedSQLite
from ledger_d1_fixture import TOKEN, seed_auth as _seed_auth
from ledger_d1_fixture import seed


def test_api_key_projection_redacts_secret_and_normalizes_fields():
    record = {"id": "key-local", "name": "  Synthetic  ", "user_id": "owner",
        "key_prefix": "pwk_synthetic", "scopes": '["profile:read","reports:read"]',
        "restrictions": '{"projectIds":["prj_project_a","prj_project_a"]}',
        "created_at": 123, "expires_at": None, "last_used_at": None,
        "revoked_at": None, "key_hash": "private-hash"}
    assert api_key_public_payload(record) == {
        "id": "key-local", "name": "Synthetic", "userId": "owner",
        "prefix": "pwk_synthetic", "scopes": ["profile:read", "reports:read"],
        "createdAt": 123, "expiresAt": None, "lastUsedAt": None,
        "revokedAt": None, "restrictions": {"shared": False,
            "projectIds": ["prj_project_a"]},
    }


@pytest.mark.parametrize("changes", [
    {"scopes": '["LEDGER:EXPENSES:READ","expenses:read","invalid"]',
     "restrictions": '{"repositoryIds":[123," repo ","repo",null]}'},
    {"name": "bad\nname", "scopes": "invalid-json", "expires_at": "123"},
    {"restrictions": '{"kind":"audit_bundle","scanId":"scan-1","repoId":123}'},
])
def test_api_key_projection_edge_cases_remain_public(changes):
    record = {"id": "key-local", "name": "Synthetic", "user_id": "owner",
        "key_prefix": "pwk_synthetic", "scopes": '["profile:read"]',
        "restrictions": "{}", "created_at": 123, "expires_at": None,
        "last_used_at": None, "revoked_at": None, **changes}
    payload = api_key_public_payload(record)
    assert payload["id"] == "key-local"
    assert "key_hash" not in payload and "key" not in payload
    assert set(payload["scopes"]) <= {"profile:read", "expenses:read"}


@pytest.mark.parametrize("value,provided", [
    (None, False), (["LEDGER:EXPENSES:READ", "expenses:read"], True),
    (["invalid"], True), (42, True), ("expenses:write", True),
])
def test_requested_scopes_validate_ledger_scopes(value, provided):
    scopes, error = requested_api_key_scopes(value, provided=provided)
    if value is None:
        assert "expenses:read" in scopes and error is None
    elif value == ["LEDGER:EXPENSES:READ", "expenses:read"]:
        assert scopes == [] and error is not None
    elif value == "expenses:write":
        assert scopes == ["expenses:write"] and error is None
    else:
        assert scopes == [] and error


@pytest.mark.parametrize("value", [
    {"projectIds": ["prj_project_a", "prj_project_a"]},
    {},
])
def test_restriction_normalization_keeps_valid_project_ids(value):
    normalized = parse_api_key_restrictions(value)
    assert normalized["shared"] is False
    assert normalized.get("projectIds", []) == list(dict.fromkeys(value.get("projectIds", [])))


@pytest.mark.parametrize("value", [
    {"kind": "audit_bundle", "scanId": "scan-1"}, "invalid-json",
])
def test_retired_api_key_restrictions_are_rejected(value):
    with pytest.raises(ValueError, match="INVALID_RESTRICTION"):
        parse_api_key_restrictions(value)


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
    assert payload["items"][0]["scopes"] == ["profile:read"]
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
    body = json.dumps({"name": "Automation", "scopes": ["expenses:read"],
        "restrictions": {"shared": False}}).encode()

    async def read_body():
        return body

    status, created = asyncio.run(handle_http_request(method="POST",
        path="/api-keys", headers={"Cookie": "pw_session=session-local",
            "Content-Length": str(len(body))}, read_body=read_body,
        binding=binding, creem_secret="", configured_products={}, now=fixture.now))
    assert status == 201 and created["key"].startswith("pwk_")
    assert created["scopes"] == ["expenses:read"]
    assert created["restrictions"] == {"shared": False}
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
    body = b'{"scopes":["expenses:read"]}'

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
