"""Cookie and API-key product reads keep the shared Server DTOs on D1."""
import asyncio
import hashlib
import json
from contextlib import closing

from pullwise_server.cloudflare_http_contract import handle_http_request
from pullwise_server.entitlements import product_usage_payload
from test_cloudflare_account_adapter import D1ShapedSQLite
from test_cloudflare_server_mapping import seed


TOKEN = "pwk_synthetic_profile_and_usage"


def _seed_auth(fixture, *, scopes=("profile:read", "usage:read"),
               session_expires=None, key_expires=None, restrictions="{}"):
    with fixture.store._immediate() as db:
        db.execute("INSERT INTO app_state(name,payload,updated_at) VALUES('sessions',?,?)",
            (json.dumps({"session-local": {"userId": "owner",
                "expiresAt": fixture.now + 3600 if session_expires is None else session_expires}}), fixture.now))
        db.execute("""CREATE TABLE api_keys(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,
            name TEXT NOT NULL,key_prefix TEXT NOT NULL,key_hash TEXT NOT NULL UNIQUE,
            scopes TEXT NOT NULL,expires_at INTEGER,restrictions TEXT NOT NULL,
            created_at INTEGER NOT NULL,last_used_at INTEGER,revoked_at INTEGER)""")
        db.execute("INSERT INTO api_keys VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("key-local", "owner", "Synthetic", TOKEN[:16],
             hashlib.sha256(TOKEN.encode()).hexdigest(), json.dumps(scopes),
             key_expires, restrictions, fixture.now, None, None))


def _get(binding, path, headers, now):
    async def no_body():
        raise AssertionError("GET must not read a request body")

    return asyncio.run(handle_http_request(method="GET", path=path,
        headers=headers, read_body=no_body, binding=binding,
        creem_secret="", configured_products={}, now=now))


def test_cookie_me_and_api_key_usage_match_existing_read_dtos(tmp_path):
    fixture, _, frozen = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    binding = D1ShapedSQLite(fixture.store)
    status, me = _get(binding, "/api/v1/me", {"Cookie": "pw_session=session-local"}, fixture.now)
    assert (status, me) == (200, {"id": "owner", "name": "", "email": "",
                                "modules": ["pr", "ci", "updates"]})
    status, usage = _get(binding, "/api/v1/usage",
        {"Authorization": f"Bearer {TOKEN}"}, fixture.now)
    assert status == 200
    assert usage == product_usage_payload(fixture.store, json.loads(frozen), timestamp=fixture.now)
    assert binding.batch_count == 0
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT last_used_at FROM api_keys WHERE id='key-local'").fetchone()[0] is None


def test_product_read_rejects_mixed_expired_and_unscoped_credentials(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("profile:read",),
        session_expires=fixture.now - 1, key_expires=fixture.now + 3600)
    binding = D1ShapedSQLite(fixture.store)
    assert _get(binding, "/api/v1/me", {"Cookie": "pw_session=session-local",
        "Authorization": f"Bearer {TOKEN}"}, fixture.now)[0] == 400
    assert _get(binding, "/api/v1/me", {"Cookie": "pw_session=session-local"}, fixture.now)[0] == 401
    assert _get(binding, "/api/v1/usage", {"Authorization": f"Bearer {TOKEN}"}, fixture.now)[0] == 403
    assert _get(binding, "/api/v1/me", {"Authorization": "Bearer pwk_wrong"}, fixture.now)[0] == 401
    assert binding.batch_count == 0


def test_product_read_rejects_github_session_without_token_and_corrupt_key_expiry(tmp_path):
    fixture, _, frozen = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    stored = json.loads(frozen)
    stored.pop("githubAccessToken")
    stored["providers"] = ["github"]
    with fixture.store._immediate() as db:
        users = json.loads(db.execute("SELECT payload FROM app_state WHERE name='users'").fetchone()[0])
        users["owner"] = stored
        db.execute("UPDATE app_state SET payload=? WHERE name='users'", (json.dumps(users),))
        db.execute("UPDATE api_keys SET expires_at='corrupt' WHERE id='key-local'")
    binding = D1ShapedSQLite(fixture.store)
    assert _get(binding, "/api/v1/me", {"Cookie": "pw_session=session-local"}, fixture.now)[0] == 401
    assert _get(binding, "/api/v1/me", {"Authorization": f"Bearer {TOKEN}"}, fixture.now)[0] == 401


def test_api_key_header_obeys_audit_restriction_without_usage_write(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    binding = D1ShapedSQLite(fixture.store)
    assert _get(binding, "/api/v1/me", {"X-Pullwise-Api-Key": TOKEN}, fixture.now)[0] == 200
    with fixture.store._immediate() as db:
        db.execute("UPDATE api_keys SET restrictions=? WHERE id='key-local'",
            ('{"kind":"audit_bundle"}',))
    status, payload = _get(binding, "/api/v1/usage",
        {"X-Pullwise-Api-Key": TOKEN}, fixture.now)
    assert status == 403 and payload["error"]["code"] == "INSUFFICIENT_SCOPE"
    assert binding.batch_count == 0


def test_watches_list_matches_store_and_filters_api_key_watch_scope(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("watches:read",))
    first = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    second = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:102", billing_owner_id="owner",
        interests=["database"], enabled=True, analysis_enabled=False)
    binding = D1ShapedSQLite(fixture.store)
    status, payload = _get(binding, "/api/v1/watches",
        {"Cookie": "pw_session=session-local"}, fixture.now)
    assert status == 200
    assert payload["items"] == fixture.store.list_watches_for_billing_owner("owner")
    assert [item["id"] for item in payload["items"]] == [first["id"], second["id"]]
    assert payload["nextCursor"] is None and payload["hasMore"] is False
    with fixture.store._immediate() as db:
        db.execute("UPDATE api_keys SET restrictions=? WHERE id='key-local'",
            (json.dumps({"watchIds": [first["id"]]}),))
    status, restricted = _get(binding, "/api/v1/watches",
        {"Authorization": f"Bearer {TOKEN}"}, fixture.now)
    assert status == 200 and [item["id"] for item in restricted["items"]] == [first["id"]]
    with fixture.store._immediate() as db:
        db.execute("UPDATE api_keys SET restrictions=? WHERE id='key-local'",
            ('{"repositoryIds":["repo-other"]}',))
    status, restricted = _get(binding, "/api/v1/watches",
        {"Authorization": f"Bearer {TOKEN}"}, fixture.now)
    assert status == 200 and restricted["items"] == []
    assert binding.batch_count == 0
