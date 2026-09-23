"""Cookie and API-key product reads keep the shared Server DTOs on D1."""
import asyncio
import hashlib
import json
from contextlib import closing
import pytest

from pullwise_server.cloudflare_http_contract import handle_http_request
from pullwise_server.entitlements import product_usage_payload
from test_cloudflare_account_adapter import D1ShapedSQLite
from test_cloudflare_server_mapping import seed
from test_cloudflare_server_mapping import mapping, execute, claim_args, publication_args
from pullwise_server.product_jobs import ProductJobScheduler


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


def _get(binding, path, headers, now, params=None):
    async def no_body():
        raise AssertionError("GET must not read a request body")

    return asyncio.run(handle_http_request(method="GET", path=path,
        headers=headers, read_body=no_body, binding=binding,
        creem_secret="", configured_products={}, now=now, params=params))


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
    assert binding.batch_count == 2
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT last_used_at FROM api_keys WHERE id='key-local'").fetchone()[0] is None


def test_usage_events_match_store_and_recheck_identity_in_one_batch(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("usage:read",))
    with fixture.store._immediate() as db:
        db.execute("UPDATE processing_usage_ledger SET state='consumed',finished_at=? WHERE charge_key='charge'",
            (fixture.now,))
        db.execute("UPDATE processing_usage_buckets SET reserved=0,used=1")
    binding = D1ShapedSQLite(fixture.store)
    status, payload = _get(binding, "/api/v1/usage/events",
        {"Authorization": f"Bearer {TOKEN}"}, fixture.now, params={"module": ["pr"]})
    assert status == 200
    expected = fixture.store.list_processing_usage_events("owner", module="pr")
    assert {key: payload[key] for key in expected} == expected
    assert binding.batch_count == 1
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT last_used_at FROM api_keys WHERE id='key-local'").fetchone()[0] is None


def test_usage_events_do_not_return_after_cookie_revocation_before_batch(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    binding = D1ShapedSQLite(fixture.store)

    def revoke_before_snapshot():
        with fixture.store._immediate() as db:
            db.execute("UPDATE app_state SET payload='{}' WHERE name='sessions'")

    binding.before_batch = revoke_before_snapshot
    status, payload = _get(binding, "/api/v1/usage/events",
        {"Cookie": "pw_session=session-local"}, fixture.now)
    assert status == 401 and payload["error"]["code"] == "UNAUTHENTICATED"
    assert binding.batch_count == 1


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


def test_malformed_api_key_header_cannot_fall_back_to_cookie(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    binding = D1ShapedSQLite(fixture.store)
    status, payload = _get(binding, "/api/v1/me",
        {"Cookie": "pw_session=session-local", "X-Pullwise-Api-Key": "invalid-key"},
        fixture.now)
    assert status == 400 and payload["error"]["code"] == "AMBIGUOUS_AUTH"
    assert binding.batch_count == 0


@pytest.mark.parametrize("path,scope", [("/api/v1/me", "profile:read"),
                                          ("/api/v1/usage", "usage:read"),
                                          ("/api/v1/watches", "watches:read")])
def test_profile_usage_and_watches_recheck_key_in_read_snapshot(tmp_path, path, scope):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=(scope,))
    binding = D1ShapedSQLite(fixture.store)

    def revoke_before_snapshot():
        with fixture.store._immediate() as db:
            db.execute("UPDATE api_keys SET revoked_at=? WHERE id='key-local'", (fixture.now,))

    binding.before_batch = revoke_before_snapshot
    status, payload = _get(binding, path,
        {"Authorization": f"Bearer {TOKEN}"}, fixture.now)
    assert status == 401 and payload["error"]["code"] == "UNAUTHENTICATED"
    assert binding.batch_count == 1


def test_usage_rechecks_cookie_in_usage_snapshot(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    binding = D1ShapedSQLite(fixture.store)

    def revoke_before_snapshot():
        with fixture.store._immediate() as db:
            db.execute("UPDATE app_state SET payload='{}' WHERE name='sessions'")

    binding.before_batch = revoke_before_snapshot
    status, payload = _get(binding, "/api/v1/usage",
        {"Cookie": "pw_session=session-local"}, fixture.now)
    assert status == 401 and payload["error"]["code"] == "UNAUTHENTICATED"
    assert binding.batch_count == 1


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
    assert binding.batch_count == 1


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
    assert {item["id"] for item in payload["items"]} == {first["id"], second["id"]}
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
    assert binding.batch_count == 3


def test_watch_detail_matches_store_and_hides_out_of_scope_watch(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("watches:read",))
    first = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    second = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:102", billing_owner_id="owner",
        interests=["database"], enabled=True, analysis_enabled=False)
    binding = D1ShapedSQLite(fixture.store)
    status, detail = _get(binding, f"/api/v1/watches/{first['id']}",
        {"Cookie": "pw_session=session-local"}, fixture.now)
    assert status == 200 and detail == fixture.store.get_watch(first["id"])
    with fixture.store._immediate() as db:
        db.execute("UPDATE api_keys SET restrictions=? WHERE id='key-local'",
            (json.dumps({"watchIds": [first["id"]]}),))
    assert _get(binding, f"/api/v1/watches/{first['id']}",
        {"Authorization": f"Bearer {TOKEN}"}, fixture.now)[0] == 200
    assert _get(binding, f"/api/v1/watches/{second['id']}",
        {"Authorization": f"Bearer {TOKEN}"}, fixture.now)[0] == 404


def test_public_watch_patch_uses_if_match_and_never_enqueues_analysis(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("watches:write",))
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    binding = D1ShapedSQLite(fixture.store)
    body = json.dumps({"interests": ["database"], "analysisEnabled": True}).encode()

    async def read_body():
        return body

    async def patch(revision, headers=None):
        return await handle_http_request(method="PATCH",
            path=f"/api/v1/watches/{watch['id']}",
            headers={"Cookie": "pw_session=session-local", "If-Match": str(revision),
                     "Content-Length": str(len(body)), **(headers or {})},
            read_body=read_body, binding=binding, creem_secret="",
            configured_products={}, now=fixture.now)

    status, updated = asyncio.run(patch(watch["revision"]))
    assert status == 200 and updated["contextVersion"] == 2
    assert updated["analysisEnabled"] is True
    assert asyncio.run(patch(watch["revision"]))[0] == 412
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM provider_attempts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM background_jobs WHERE job_type='analyze_source'").fetchone()[0] == 1


def test_public_watch_patch_rolls_back_when_api_key_revoked_before_write(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("watches:write",))
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    binding = D1ShapedSQLite(fixture.store)
    batches = 0

    def revoke_before_write():
        nonlocal batches
        batches += 1
        if batches == 2:
            with fixture.store._immediate() as db:
                db.execute("UPDATE api_keys SET revoked_at=? WHERE id='key-local'", (fixture.now,))

    binding.before_batch = revoke_before_write
    body = b'{"analysisEnabled":true}'

    async def read_body():
        return body

    status, payload = asyncio.run(handle_http_request(method="PATCH",
        path=f"/api/v1/watches/{watch['id']}",
        headers={"Authorization": f"Bearer {TOKEN}", "If-Match": "1",
                 "Content-Length": str(len(body))}, read_body=read_body,
        binding=binding, creem_secret="", configured_products={}, now=fixture.now))
    assert status == 412 and payload["error"]["code"] == "REVISION_MISMATCH"
    assert fixture.store.get_watch(watch["id"])["analysisEnabled"] is False
    assert fixture.store.get_watch(watch["id"])["revision"] == 1


def test_public_watch_delete_revokes_linked_context_and_releases_job(tmp_path):
    fixture, job, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("watches:write",))
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    with fixture.store._immediate() as db:
        db.execute("UPDATE source_contexts SET watch_id=? WHERE source_id='1'", (watch["id"],))
    binding = D1ShapedSQLite(fixture.store)

    async def no_body():
        raise AssertionError("DELETE must not read request body")

    status, payload = asyncio.run(handle_http_request(method="DELETE",
        path=f"/api/v1/watches/{watch['id']}",
        headers={"Cookie": "pw_session=session-local", "If-Match": str(watch["revision"])},
        read_body=no_body, binding=binding, creem_secret="",
        configured_products={}, now=fixture.now))
    assert status == 204 and payload == {}
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT accessible FROM source_contexts WHERE source_id='1'").fetchone()[0] == 0
        assert db.execute("SELECT state FROM background_jobs WHERE id=?", (job["id"],)).fetchone()[0] == "cancelled"
        assert db.execute("SELECT reserved FROM processing_usage_buckets").fetchone()[0] == 0


def test_public_watch_delete_rolls_back_after_key_revocation(tmp_path):
    fixture, job, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("watches:write",))
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    with fixture.store._immediate() as db:
        db.execute("UPDATE source_contexts SET watch_id=? WHERE source_id='1'", (watch["id"],))
    binding = D1ShapedSQLite(fixture.store)
    batches = 0

    def revoke_before_write():
        nonlocal batches
        batches += 1
        if batches == 2:
            with fixture.store._immediate() as db:
                db.execute("UPDATE api_keys SET revoked_at=? WHERE id='key-local'", (fixture.now,))

    binding.before_batch = revoke_before_write

    async def no_body():
        raise AssertionError("DELETE must not read body")

    status, payload = asyncio.run(handle_http_request(method="DELETE",
        path=f"/api/v1/watches/{watch['id']}",
        headers={"Authorization": f"Bearer {TOKEN}", "If-Match": "1"},
        read_body=no_body, binding=binding, creem_secret="",
        configured_products={}, now=fixture.now))
    assert status == 412 and payload["error"]["code"] == "REVISION_MISMATCH"
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT archived_at FROM update_watches WHERE id=?", (watch["id"],)).fetchone()[0] is None
        assert db.execute("SELECT state FROM background_jobs WHERE id=?", (job["id"],)).fetchone()[0] == "queued"
        assert db.execute("SELECT reserved FROM processing_usage_buckets").fetchone()[0] == 1


def test_source_read_rechecks_api_key_in_the_source_snapshot(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("items:read",))
    binding = D1ShapedSQLite(fixture.store)

    def revoke_before_snapshot():
        with fixture.store._immediate() as db:
            db.execute("UPDATE api_keys SET revoked_at=? WHERE id='key-local'", (fixture.now,))

    binding.before_batch = revoke_before_snapshot
    status, payload = _get(binding, "/api/v1/sources",
        {"Authorization": f"Bearer {TOKEN}"}, fixture.now)
    assert status == 401 and payload["error"]["code"] == "UNAUTHENTICATED"
    assert binding.batch_count == 1


def test_source_read_rechecks_cookie_in_the_source_snapshot(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture)
    binding = D1ShapedSQLite(fixture.store)

    def revoke_before_snapshot():
        with fixture.store._immediate() as db:
            db.execute("UPDATE app_state SET payload='{}' WHERE name='sessions'")

    binding.before_batch = revoke_before_snapshot
    status, payload = _get(binding, "/api/v1/sources",
        {"Cookie": "pw_session=session-local"}, fixture.now)
    assert status == 401 and payload["error"]["code"] == "UNAUTHENTICATED"
    assert binding.batch_count == 1


def test_source_list_detail_filter_and_restrictions_match_store(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("items:read",))
    binding = D1ShapedSQLite(fixture.store)
    cookie = {"Cookie": "pw_session=session-local"}
    status, listed = _get(binding, "/api/v1/sources", cookie, fixture.now,
        params={"module": ["pr"]})
    assert status == 200
    assert listed["items"] == fixture.store.list_sources_for_billing_owner("owner")
    assert listed["nextCursor"] is None and listed["hasMore"] is False
    assert binding.batch_count == 1
    status, detail = _get(binding, "/api/v1/sources/1", cookie, fixture.now)
    assert status == 200
    assert detail == fixture.store.list_sources_for_billing_owner("owner",
        source_id="1", include_content=True)[0]
    assert _get(binding, "/api/v1/sources/missing", cookie, fixture.now)[0] == 404
    assert _get(binding, "/api/v1/sources", cookie, fixture.now,
        params={"updateSignal": ["security_fix_stated"]})[0] == 422
    with fixture.store._immediate() as db:
        db.execute("UPDATE api_keys SET restrictions=? WHERE id='key-local'",
            ('{"repositoryIds":["other"]}',))
    status, restricted = _get(binding, "/api/v1/sources",
        {"Authorization": f"Bearer {TOKEN}"}, fixture.now)
    assert status == 200 and restricted["items"] == []
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM provider_attempts").fetchone()[0] == 0
        assert db.execute("SELECT last_used_at FROM api_keys WHERE id='key-local'").fetchone()[0] is None


def test_source_read_rechecks_changed_user_and_scope(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("items:read",))
    binding = D1ShapedSQLite(fixture.store)

    def change_user_before_snapshot():
        with fixture.store._immediate() as db:
            db.execute("UPDATE app_state SET payload='{}' WHERE name='users'")

    binding.before_batch = change_user_before_snapshot
    assert _get(binding, "/api/v1/sources",
        {"Authorization": f"Bearer {TOKEN}"}, fixture.now)[0] == 401

    fixture, _, _ = seed(tmp_path / "second.db")
    _seed_auth(fixture, scopes=("items:read",))
    binding = D1ShapedSQLite(fixture.store)

    def revoke_scope_before_snapshot():
        with fixture.store._immediate() as db:
            db.execute("UPDATE api_keys SET scopes='[]' WHERE id='key-local'")

    binding.before_batch = revoke_scope_before_snapshot
    assert _get(binding, "/api/v1/sources",
        {"Authorization": f"Bearer {TOKEN}"}, fixture.now)[0] == 403


def test_item_list_detail_uses_saved_snapshot_and_resource_scope(tmp_path):
    fixture, job, frozen = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("items:read",))
    publication = publication_args(fixture, job, frozen)
    execute(fixture.store, mapping().claim(**claim_args(fixture, job, frozen)))
    execute(fixture.store, mapping().publication(**publication))
    item_id = publication["item"]["id"]
    binding = D1ShapedSQLite(fixture.store)
    cookie = {"Cookie": "pw_session=session-local"}
    status, listed = _get(binding, "/api/v1/items", cookie, fixture.now)
    assert status == 200
    assert listed["items"] == fixture.store.list_items_for_billing_owner("owner")
    status, filtered = _get(binding, "/api/v1/items", cookie, fixture.now,
        params={"actionType": ["change_requested"], "lifecycle": ["active"]})
    assert status == 200 and [entry["id"] for entry in filtered["items"]] == [item_id]
    status, invalid = _get(binding, "/api/v1/items", cookie, fixture.now,
        params={"pullNumber": ["12"]})
    assert status == 422 and invalid["error"]["code"] == "INVALID_CONFIGURATION"
    status, detail = _get(binding, f"/api/v1/items/{item_id}", cookie, fixture.now)
    assert status == 200
    assert detail == fixture.store.list_items_for_billing_owner("owner",
        item_id=item_id, include_history=True)[0]
    with fixture.store._immediate() as db:
        db.execute("UPDATE api_keys SET restrictions=? WHERE id='key-local'",
            ('{"repositoryIds":["other"]}',))
    assert _get(binding, "/api/v1/items", {"Authorization": f"Bearer {TOKEN}"},
        fixture.now)[1]["items"] == []


def test_item_read_rechecks_revoked_key_in_the_item_snapshot(tmp_path):
    fixture, job, frozen = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("items:read",))
    publication = publication_args(fixture, job, frozen)
    execute(fixture.store, mapping().claim(**claim_args(fixture, job, frozen)))
    execute(fixture.store, mapping().publication(**publication))
    binding = D1ShapedSQLite(fixture.store)

    def revoke_before_snapshot():
        with fixture.store._immediate() as db:
            db.execute("UPDATE api_keys SET revoked_at=? WHERE id='key-local'", (fixture.now,))

    binding.before_batch = revoke_before_snapshot
    status, payload = _get(binding, "/api/v1/items",
        {"Authorization": f"Bearer {TOKEN}"}, fixture.now)
    assert status == 401 and payload["error"]["code"] == "UNAUTHENTICATED"
    assert binding.batch_count == 1


def test_item_handling_patch_is_atomic_and_does_not_charge_usage(tmp_path):
    fixture, job, frozen = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("items:read", "items:write"))
    publication = publication_args(fixture, job, frozen)
    execute(fixture.store, mapping().claim(**claim_args(fixture, job, frozen)))
    execute(fixture.store, mapping().publication(**publication))
    item_id = publication["item"]["id"]
    binding = D1ShapedSQLite(fixture.store)
    item = fixture.store.list_items_for_billing_owner("owner", item_id=item_id)[0]
    body = json.dumps({"itemVersion": item["itemVersion"],
                       "disposition": "done"}).encode()

    async def read_body():
        return body

    async def patch(revision):
        return await handle_http_request(method="PATCH", path=f"/api/v1/items/{item_id}",
            headers={"Cookie": "pw_session=session-local", "If-Match": str(revision),
                     "Content-Length": str(len(body))}, read_body=read_body,
            binding=binding, creem_secret="", configured_products={}, now=fixture.now)

    status, changed = asyncio.run(patch(item["revision"]))
    assert status == 200 and changed["handling"]["disposition"] == "done"
    assert changed["revision"] == item["revision"] + 1
    assert asyncio.run(patch(item["revision"]))[0] == 412
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM item_handling_events").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM provider_attempts").fetchone()[0] == 1


def test_item_handling_patch_rolls_back_when_key_revoked_before_write_batch(tmp_path):
    fixture, job, frozen = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("items:read", "items:write"))
    publication = publication_args(fixture, job, frozen)
    execute(fixture.store, mapping().claim(**claim_args(fixture, job, frozen)))
    execute(fixture.store, mapping().publication(**publication))
    item_id = publication["item"]["id"]
    item = fixture.store.list_items_for_billing_owner("owner", item_id=item_id)[0]
    binding = D1ShapedSQLite(fixture.store)
    batches = 0

    def revoke_before_write():
        nonlocal batches
        batches += 1
        if batches == 2:
            with fixture.store._immediate() as db:
                db.execute("UPDATE api_keys SET revoked_at=? WHERE id='key-local'", (fixture.now,))

    binding.before_batch = revoke_before_write
    body = json.dumps({"itemVersion": item["itemVersion"],
                       "disposition": "done"}).encode()

    async def read_body():
        return body

    status, payload = asyncio.run(handle_http_request(method="PATCH",
        path=f"/api/v1/items/{item_id}",
        headers={"Authorization": f"Bearer {TOKEN}", "If-Match": str(item["revision"]),
                 "Content-Length": str(len(body))}, read_body=read_body,
        binding=binding, creem_secret="", configured_products={}, now=fixture.now))
    assert status == 412 and payload["error"]["code"] == "REVISION_MISMATCH"
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM item_handling_events").fetchone()[0] == 0
        assert db.execute("SELECT revision FROM items WHERE id=?", (item_id,)).fetchone()[0] == item["revision"]


def test_handling_saved_item_remains_available_after_analysis_is_disabled(tmp_path):
    fixture, job, frozen = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("items:read", "items:write"))
    publication = publication_args(fixture, job, frozen)
    execute(fixture.store, mapping().claim(**claim_args(fixture, job, frozen)))
    execute(fixture.store, mapping().publication(**publication))
    with fixture.store._immediate() as db:
        db.execute("""UPDATE source_contexts SET analysis_enabled=0,
            configuration_revision=configuration_revision+1
            WHERE source_id='2'""")
    item_id = publication["item"]["id"]
    item = fixture.store.list_items_for_billing_owner("owner", item_id=item_id)[0]
    body = json.dumps({"itemVersion": item["itemVersion"], "disposition": "done"}).encode()

    async def read_body():
        return body

    status, updated = asyncio.run(handle_http_request(method="PATCH",
        path=f"/api/v1/items/{item_id}",
        headers={"Cookie": "pw_session=session-local", "If-Match": str(item["revision"]),
                 "Content-Length": str(len(body))}, read_body=read_body,
        binding=D1ShapedSQLite(fixture.store), creem_secret="",
        configured_products={}, now=fixture.now))
    assert status == 200 and updated["handling"]["disposition"] == "done"


def test_item_overview_counts_items_and_source_contexts(tmp_path):
    fixture, job, frozen = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("items:read",))
    publication = publication_args(fixture, job, frozen)
    execute(fixture.store, mapping().claim(**claim_args(fixture, job, frozen)))
    execute(fixture.store, mapping().publication(**publication))
    binding = D1ShapedSQLite(fixture.store)
    status, overview = _get(binding, "/api/v1/items/overview",
        {"Cookie": "pw_session=session-local"}, fixture.now,
        params={"view": ["all"]})
    assert status == 200
    assert overview["totalCount"] == 1
    assert overview["sourceCoverage"]["total"] == 2
    assert overview["viewCounts"]["all"] == 1
    assert binding.batch_count == 1


def test_overview_rechecks_auth_in_combined_item_source_snapshot(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("items:read",))
    binding = D1ShapedSQLite(fixture.store)

    def revoke_before_snapshot():
        with fixture.store._immediate() as db:
            db.execute("UPDATE api_keys SET revoked_at=? WHERE id='key-local'", (fixture.now,))

    binding.before_batch = revoke_before_snapshot
    status, payload = _get(binding, "/api/v1/items/overview",
        {"Authorization": f"Bearer {TOKEN}"}, fixture.now)
    assert status == 401 and payload["error"]["code"] == "UNAUTHENTICATED"
    assert binding.batch_count == 1


def test_sync_job_get_is_requester_scoped_and_read_only(tmp_path):
    fixture, analysis_job, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("items:read",))
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    job = ProductJobScheduler(fixture.store).request_manual_sync(
        resource_kind="watch", resource_id=watch["id"], requester_id="owner")
    binding = D1ShapedSQLite(fixture.store)
    cookie = {"Cookie": "pw_session=session-local"}
    status, payload = _get(binding, f"/api/v1/jobs/{job['id']}", cookie, fixture.now)
    assert status == 200
    assert payload["operation"] == "sync_watch" and payload["status"] == job["status"]
    assert payload["attempt"] == 0
    assert _get(binding, f"/api/v1/jobs/{analysis_job['id']}", cookie, fixture.now)[0] == 404
    with fixture.store._immediate() as db:
        db.execute("UPDATE update_watches SET archived_at=? WHERE id=?", (fixture.now, watch["id"]))
    assert _get(binding, f"/api/v1/jobs/{job['id']}", cookie, fixture.now)[0] == 404
    assert binding.batch_count == 3
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM provider_attempts").fetchone()[0] == 0
