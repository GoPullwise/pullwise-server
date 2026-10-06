"""Candidate account API-key reads stay session-only and redact secrets."""
import asyncio
import json
import pytest

from pullwise_server.cloudflare_http_contract import handle_http_request
from pullwise_server.api_key_dto_rules import (
    api_key_public_payload, requested_api_key_scopes,
    parse_api_key_restrictions,
)
from pullwise_server.cloudflare_state_records import record_name
from ledger_d1_fixture import D1ShapedSQLite
from ledger_d1_fixture import TOKEN, seed_auth as _fixture_seed_auth
from ledger_d1_fixture import seed
from state_record_fixtures import normalize_legacy_state


def _seed_auth(fixture):
    _fixture_seed_auth(fixture)
    with fixture.store._immediate() as db:
        normalize_legacy_state(db)


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
            db.execute("DELETE FROM app_state WHERE name=?",
                       (record_name("sessions", "session-local"),))

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
        headers={"Cookie": "pw_session=session-local", "Origin": "https://app.example"}, read_body=no_body,
        binding=binding, creem_secret="", configured_products={}, now=fixture.now,
        trusted_origins={"https://app.example"}))
    assert status == 200 and payload == {"ok": True, "id": "key-local", "revoked": True}
    assert get(binding, {"Cookie": "pw_session=session-local"}, fixture.now)[1]["items"] == []
    with fixture.store._immediate() as db:
        assert db.execute("SELECT revoked_at FROM api_keys WHERE id='key-local'").fetchone()[0] == fixture.now


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
                db.execute("DELETE FROM app_state WHERE name=?",
                           (record_name("sessions", "session-local"),))

    binding.before_batch = revoke_before_write

    async def no_body():
        raise AssertionError("DELETE must not read body")

    status, payload = asyncio.run(handle_http_request(method="DELETE",
        path="/api-keys/key-local",
        headers={"Cookie": "pw_session=session-local", "Origin": "https://app.example"}, read_body=no_body,
        binding=binding, creem_secret="", configured_products={}, now=fixture.now,
        trusted_origins={"https://app.example"}))
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
            "Origin": "https://app.example", "Content-Length": str(len(body))}, read_body=read_body,
        binding=binding, creem_secret="", configured_products={}, now=fixture.now,
        trusted_origins={"https://app.example"}))
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
                db.execute("DELETE FROM app_state WHERE name=?",
                           (record_name("sessions", "session-local"),))

    binding.before_batch = revoke_before_write
    body = b'{"scopes":["expenses:read"]}'

    async def read_body():
        return body

    status, payload = asyncio.run(handle_http_request(method="POST",
        path="/api-keys", headers={"Cookie": "pw_session=session-local",
            "Origin": "https://app.example", "Content-Length": str(len(body))}, read_body=read_body,
        binding=binding, creem_secret="", configured_products={}, now=fixture.now,
        trusted_origins={"https://app.example"}))
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


@pytest.mark.parametrize("method,path", [("POST", "/api-keys"), ("DELETE", "/api-keys/key-local")])
@pytest.mark.parametrize("same_site", ["Lax", "Strict", "None"])
@pytest.mark.parametrize("origin", [None, "https://hostile.app.example"])
def test_every_cookie_key_write_checks_origin_before_body_or_d1(tmp_path, method, path, same_site, origin):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    binding = D1ShapedSQLite(fixture.store)
    headers = {"Cookie": "pw_session=session-local", "Content-Length": "2"}
    if origin:
        headers["Origin"] = origin

    async def no_body():
        raise AssertionError("untrusted cookie request must not read body")

    status, payload = asyncio.run(handle_http_request(method=method, path=path,
        headers=headers, read_body=no_body, binding=binding, creem_secret="",
        configured_products={}, now=fixture.now, cookie_same_site=same_site,
        trusted_origins={"https://app.example"}))
    assert status == 403 and payload["error"]["code"] == "UNTRUSTED_ORIGIN"
    assert binding.batch_count == 0


@pytest.mark.parametrize("method,path", [
    ("GET", "/api-keys"), ("POST", "/api-keys"), ("DELETE", "/api-keys/key-local"),
])
def test_key_routes_ignore_other_accounts_changed_during_authorization(tmp_path, method, path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    binding = D1ShapedSQLite(fixture.store)

    def change_other_records():
        with fixture.store._immediate() as db:
            for kind, identifier, payload in (
                ("users", "other", {"id": "other", "name": f"Changed {binding.batch_count}"}),
                ("sessions", "other-session", {"userId": "other",
                    "expiresAt": fixture.now + 100 + binding.batch_count}),
            ):
                db.execute("""INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)
                    ON CONFLICT(name) DO UPDATE SET payload=excluded.payload,
                    updated_at=excluded.updated_at""", (record_name(kind, identifier),
                        json.dumps(payload, separators=(",", ":")), fixture.now))

    binding.before_batch = change_other_records
    body = b'{"scopes":["expenses:read"]}'

    async def read_body():
        assert method == "POST", "read-only and DELETE routes must not read a body"
        return body

    status, payload = asyncio.run(handle_http_request(method=method, path=path,
        headers={"Cookie": "pw_session=session-local", "Origin": "https://app.example",
                 "Content-Length": str(len(body))}, read_body=read_body,
        binding=binding, creem_secret="", configured_products={}, now=fixture.now,
        trusted_origins={"https://app.example"}))
    assert status == (201 if method == "POST" else 200)
    if method == "GET":
        assert [item["id"] for item in payload["items"]] == ["key-local"]
    elif method == "DELETE":
        assert payload == {"ok": True, "id": "key-local", "revoked": True}
    else:
        assert payload["userId"] == "owner" and payload["key"].startswith("pwk_")


@pytest.mark.parametrize("method,path,expected_status,expected_code", [
    ("POST", "/api-keys", 503, "SERVER_UNAVAILABLE"),
    ("DELETE", "/api-keys/key-local", 409, "AUTHORIZATION_CHANGED"),
])
def test_key_mutations_roll_back_if_own_account_changes_before_write(
        tmp_path, method, path, expected_status, expected_code):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    binding = D1ShapedSQLite(fixture.store)
    batches = 0

    def change_own_record_before_write():
        nonlocal batches
        batches += 1
        if batches == 2:
            with fixture.store._immediate() as db:
                db.execute("UPDATE app_state SET payload=json_set(payload,'$.name','Changed') WHERE name=?",
                           (record_name("users", "owner"),))

    binding.before_batch = change_own_record_before_write
    body = b'{"scopes":["expenses:read"]}'

    async def read_body():
        assert method == "POST"
        return body

    status, payload = asyncio.run(handle_http_request(method=method, path=path,
        headers={"Cookie": "pw_session=session-local", "Origin": "https://app.example",
                 "Content-Length": str(len(body))}, read_body=read_body,
        binding=binding, creem_secret="", configured_products={}, now=fixture.now,
        trusted_origins={"https://app.example"}))
    assert status == expected_status and payload["error"]["code"] == expected_code
    with fixture.store._immediate() as db:
        assert [tuple(row) for row in db.execute("SELECT id,revoked_at FROM api_keys")] == [("key-local", None)]
        assert db.execute("SELECT COUNT(*) FROM d1_command_guard").fetchone()[0] == 0


def test_key_revocation_cannot_revoke_another_accounts_key(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    binding = D1ShapedSQLite(fixture.store)
    with fixture.store._immediate() as db:
        db.execute("""INSERT INTO api_keys VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                   ("key-other", "other", "Other", "pwk_other", "other-hash",
                    '["profile:read"]', None, "{}", fixture.now, None, None))

    async def no_body():
        raise AssertionError("DELETE must not read body")

    status, payload = asyncio.run(handle_http_request(method="DELETE", path="/api-keys/key-other",
        headers={"Cookie": "pw_session=session-local", "Origin": "https://app.example"},
        read_body=no_body, binding=binding, creem_secret="", configured_products={},
        now=fixture.now, trusted_origins={"https://app.example"}))
    assert status == 404 and payload["error"]["code"] == "NOT_FOUND"
    with fixture.store._immediate() as db:
        assert db.execute("SELECT revoked_at FROM api_keys WHERE id='key-other'").fetchone()[0] is None


def test_key_revocation_uses_valid_cookie_among_multiple_session_candidates(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    binding = D1ShapedSQLite(fixture.store)

    async def no_body():
        raise AssertionError("DELETE must not read body")

    status, payload = asyncio.run(handle_http_request(method="DELETE", path="/api-keys/key-local",
        headers={"Cookie": "pw_session=missing; pw_session=session-local", "Origin": "https://app.example"},
        read_body=no_body, binding=binding, creem_secret="", configured_products={},
        now=fixture.now, trusted_origins={"https://app.example"}))
    assert status == 200 and payload["revoked"] is True
