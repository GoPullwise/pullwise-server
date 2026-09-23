"""Trusted D1 manual fact-sync queue never reserves model processing."""
import asyncio
import json
import sqlite3
from contextlib import closing

import pytest

from pullwise_server.cloudflare_manual_sync import D1ManualSyncTransactions
from pullwise_server.product_jobs import ProductJobScheduler
from test_cloudflare_account_adapter import D1ShapedSQLite
from test_cloudflare_server_mapping import seed
from test_cloudflare_product_reads import TOKEN, _seed_auth


def test_trusted_public_watch_manual_sync_is_deduplicated_without_model_cost(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    adapter = D1ManualSyncTransactions(D1ShapedSQLite(fixture.store))
    first = asyncio.run(adapter.request(resource_kind="watch", resource_id=watch["id"],
        owner_id="owner", job_id="job-manual-1", now=fixture.now))
    again = asyncio.run(adapter.request(resource_kind="watch", resource_id=watch["id"],
        owner_id="owner", job_id="job-manual-2", now=fixture.now))
    assert first["id"] == again["id"] == "job-manual-1"
    assert first["jobType"] == "sync_watch" and again["reused"] is True
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM background_jobs WHERE job_type='sync_watch'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM provider_attempts").fetchone()[0] == 0
        assert db.execute("SELECT reserved FROM processing_usage_buckets LIMIT 1").fetchone()[0] == 1


def test_manual_sync_does_not_enqueue_after_watch_archived_before_write(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    binding = D1ShapedSQLite(fixture.store)
    batches = 0

    def archive_before_write():
        nonlocal batches
        batches += 1
        if batches == 2:
            fixture.store.archive_watch(watch["id"], expected_revision=watch["revision"])

    binding.before_batch = archive_before_write
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(D1ManualSyncTransactions(binding).request(
            resource_kind="watch", resource_id=watch["id"],
            owner_id="owner", job_id="job-late", now=fixture.now))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM background_jobs WHERE id='job-late'").fetchone()[0] == 0


def test_manual_sync_competitor_wins_one_active_logical_key(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    binding = D1ShapedSQLite(fixture.store)
    batches = 0

    def competitor_before_write():
        nonlocal batches
        batches += 1
        if batches == 2:
            ProductJobScheduler(fixture.store).request_manual_sync(
                resource_kind="watch", resource_id=watch["id"], requester_id="owner")

    binding.before_batch = competitor_before_write
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(D1ManualSyncTransactions(binding).request(
            resource_kind="watch", resource_id=watch["id"],
            owner_id="owner", job_id="job-late", now=fixture.now))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM background_jobs WHERE job_type='sync_watch'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM background_jobs WHERE id='job-late'").fetchone()[0] == 0


def test_manual_sync_new_generation_after_completed_job(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    adapter = D1ManualSyncTransactions(D1ShapedSQLite(fixture.store))
    first = asyncio.run(adapter.request(resource_kind="watch", resource_id=watch["id"],
        owner_id="owner", job_id="job-first", now=fixture.now))
    with fixture.store._immediate() as db:
        db.execute("UPDATE background_jobs SET state='succeeded' WHERE id='job-first'")
    next_job = asyncio.run(adapter.request(resource_kind="watch", resource_id=watch["id"],
        owner_id="owner", job_id="job-second", now=fixture.now))
    assert first["generation"] == 1 and next_job["generation"] == 2
    assert next_job["status"] == "queued" and next_job["reused"] is False


def test_manual_sync_does_not_return_another_requesters_active_job(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    ProductJobScheduler(fixture.store).request_manual_sync(
        resource_kind="watch", resource_id=watch["id"], requester_id="other")
    with pytest.raises(ValueError, match="RESOURCE_NOT_AUTHORIZED"):
        asyncio.run(D1ManualSyncTransactions(D1ShapedSQLite(fixture.store)).request(
            resource_kind="watch", resource_id=watch["id"],
            owner_id="owner", job_id="job-owner", now=fixture.now))


def _repo_fixture(fixture):
    fixture.store.put_repository_service(repository_id="repo", installation_id="inst-1",
        billing_owner_id="owner", expected_revision=0, enabled=True,
        modules={"pr": True, "ci": False}, analysis_enabled={"pr": False, "ci": False},
        allow_member_sync=False, default_assignee_id=None, priority_order=0)
    fixture.store.set_discovery_authorization(resource_kind="repository",
        resource_id="repo", module="pr", github_repository_id="101",
        installation_id="inst-1", app_id="app", authorization_revision=1,
        accessible=True, valid_until=fixture.now + 300, observed_at=fixture.now)
    with fixture.store._immediate() as db:
        users = json.loads(db.execute("SELECT payload FROM app_state WHERE name='users'").fetchone()[0])
        users["owner"]["githubRepositoryAccess"] = {
            "mode": "github-app", "authorizedUserId": "owner",
            "authorizedGithubId": "author", "repositoriesNeedSync": False,
            "repositoryItems": [{"id": "repo", "installationId": "inst-1"}],
        }
        db.execute("UPDATE app_state SET payload=? WHERE name='users'", (json.dumps(users),))


def test_trusted_repository_manual_sync_requires_account_and_fresh_installation_proof(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _repo_fixture(fixture)
    adapter = D1ManualSyncTransactions(D1ShapedSQLite(fixture.store))
    first = asyncio.run(adapter.request(resource_kind="repository", resource_id="repo",
        owner_id="owner", job_id="job-repo-1", now=fixture.now))
    replay = asyncio.run(adapter.request(resource_kind="repository", resource_id="repo",
        owner_id="owner", job_id="job-repo-2", now=fixture.now))
    assert first["jobType"] == "sync_repository" and first["reused"] is False
    assert replay["id"] == first["id"] and replay["reused"] is True
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM provider_attempts").fetchone()[0] == 0
        assert db.execute("SELECT reserved FROM processing_usage_buckets LIMIT 1").fetchone()[0] == 1


def test_repository_manual_sync_fails_closed_on_expiry_or_account_removal(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _repo_fixture(fixture)
    adapter = D1ManualSyncTransactions(D1ShapedSQLite(fixture.store))
    with pytest.raises(ValueError, match="RESOURCE_NOT_AUTHORIZED"):
        asyncio.run(adapter.request(resource_kind="repository", resource_id="repo",
            owner_id="owner", job_id="job-expired", now=fixture.now + 301))
    with fixture.store._immediate() as db:
        users = json.loads(db.execute("SELECT payload FROM app_state WHERE name='users'").fetchone()[0])
        users["owner"].pop("githubRepositoryAccess")
        db.execute("UPDATE app_state SET payload=? WHERE name='users'", (json.dumps(users),))
    with pytest.raises(ValueError, match="RESOURCE_NOT_AUTHORIZED"):
        asyncio.run(adapter.request(resource_kind="repository", resource_id="repo",
            owner_id="owner", job_id="job-no-access", now=fixture.now))


@pytest.mark.parametrize("change", ["account", "proof"])
def test_repository_manual_sync_rechecks_authority_in_write_batch(tmp_path, change):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _repo_fixture(fixture)
    binding = D1ShapedSQLite(fixture.store)
    batches = 0

    def revoke_before_write():
        nonlocal batches
        batches += 1
        if batches == 2:
            with fixture.store._immediate() as db:
                if change == "account":
                    users = json.loads(db.execute("SELECT payload FROM app_state WHERE name='users'").fetchone()[0])
                    users["owner"].pop("githubRepositoryAccess")
                    db.execute("UPDATE app_state SET payload=? WHERE name='users'", (json.dumps(users),))
                else:
                    db.execute("UPDATE discovery_targets SET accessible=0 WHERE resource_kind='repository'")

    binding.before_batch = revoke_before_write
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(D1ManualSyncTransactions(binding).request(
            resource_kind="repository", resource_id="repo",
            owner_id="owner", job_id="job-stale", now=fixture.now))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM background_jobs WHERE id='job-stale'").fetchone()[0] == 0


def test_manual_sync_rechecks_api_key_before_enqueue(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("watches:read", "sync:write"))
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    with closing(fixture.store.connect()) as db:
        key = dict(db.execute("SELECT * FROM api_keys WHERE id='key-local'").fetchone())
        user = db.execute("SELECT value FROM app_state,json_each(payload) WHERE name='users' AND key='owner'").fetchone()[0]
    proof = {"user": user, "token": TOKEN, "key": key, "sessions": None}
    binding = D1ShapedSQLite(fixture.store)
    batches = 0

    def revoke_before_write():
        nonlocal batches
        batches += 1
        if batches == 2:
            with fixture.store._immediate() as db:
                db.execute("UPDATE api_keys SET revoked_at=? WHERE id='key-local'", (fixture.now,))

    binding.before_batch = revoke_before_write
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(D1ManualSyncTransactions(binding).request(
            resource_kind="watch", resource_id=watch["id"], owner_id="owner",
            job_id="job-revoked-key", now=fixture.now, proof=proof))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM background_jobs WHERE id='job-revoked-key'").fetchone()[0] == 0


def test_manual_sync_rechecks_cookie_session_before_enqueue(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("watches:read", "sync:write"))
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    with closing(fixture.store.connect()) as db:
        user = db.execute("SELECT value FROM app_state,json_each(payload) WHERE name='users' AND key='owner'").fetchone()[0]
        sessions = db.execute("SELECT payload FROM app_state WHERE name='sessions'").fetchone()[0]
    proof = {"user": user, "token": None, "key": None, "sessions": sessions,
             "session_id": "session-local"}
    binding = D1ShapedSQLite(fixture.store)
    batches = 0

    def revoke_before_write():
        nonlocal batches
        batches += 1
        if batches == 2:
            with fixture.store._immediate() as db:
                db.execute("UPDATE app_state SET payload='{}' WHERE name='sessions'")

    binding.before_batch = revoke_before_write
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(D1ManualSyncTransactions(binding).request(
            resource_kind="watch", resource_id=watch["id"], owner_id="owner",
            job_id="job-revoked-session", now=fixture.now, proof=proof))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM background_jobs WHERE id='job-revoked-session'").fetchone()[0] == 0


@pytest.mark.parametrize("scopes,restrictions", [
    (("watches:read",), "{}"),
    (("watches:read", "sync:write"), '{"watchIds":["another-watch"]}'),
])
def test_manual_sync_rejects_key_without_sync_scope_or_resource(tmp_path, scopes, restrictions):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=scopes, restrictions=restrictions)
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    with closing(fixture.store.connect()) as db:
        key = dict(db.execute("SELECT * FROM api_keys WHERE id='key-local'").fetchone())
        user = db.execute("SELECT value FROM app_state,json_each(payload) WHERE name='users' AND key='owner'").fetchone()[0]
    proof = {"user": user, "token": TOKEN, "key": key, "sessions": None}
    with pytest.raises(ValueError, match="RESOURCE_NOT_AUTHORIZED"):
        asyncio.run(D1ManualSyncTransactions(D1ShapedSQLite(fixture.store)).request(
            resource_kind="watch", resource_id=watch["id"], owner_id="owner",
            job_id="job-bad-scope", now=fixture.now, proof=proof))


def test_manual_sync_rejects_expired_api_key_proof(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("watches:read", "sync:write"),
        key_expires=fixture.now - 1)
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    with closing(fixture.store.connect()) as db:
        key = dict(db.execute("SELECT * FROM api_keys WHERE id='key-local'").fetchone())
        user = db.execute("SELECT value FROM app_state,json_each(payload) WHERE name='users' AND key='owner'").fetchone()[0]
    proof = {"user": user, "token": TOKEN, "key": key, "sessions": None}
    with pytest.raises(ValueError, match="RESOURCE_NOT_AUTHORIZED"):
        asyncio.run(D1ManualSyncTransactions(D1ShapedSQLite(fixture.store)).request(
            resource_kind="watch", resource_id=watch["id"], owner_id="owner",
            job_id="job-expired-key", now=fixture.now, proof=proof))


def test_manual_sync_rejects_expired_cookie_proof(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture, session_expires=fixture.now - 1)
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    with closing(fixture.store.connect()) as db:
        user = db.execute("SELECT value FROM app_state,json_each(payload) WHERE name='users' AND key='owner'").fetchone()[0]
        sessions = db.execute("SELECT payload FROM app_state WHERE name='sessions'").fetchone()[0]
    proof = {"user": user, "token": None, "key": None, "sessions": sessions,
             "session_id": "session-local"}
    with pytest.raises(ValueError, match="RESOURCE_NOT_AUTHORIZED"):
        asyncio.run(D1ManualSyncTransactions(D1ShapedSQLite(fixture.store)).request(
            resource_kind="watch", resource_id=watch["id"], owner_id="owner",
            job_id="job-expired-session", now=fixture.now, proof=proof))


def test_idempotent_manual_sync_commits_job_and_response_together(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    adapter = D1ManualSyncTransactions(D1ShapedSQLite(fixture.store))
    args = dict(resource_kind="watch", resource_id=watch["id"], owner_id="owner",
        idempotency_key="sync-one", request_id="req-one", now=fixture.now)
    first = asyncio.run(adapter.request_idempotent(**args, job_id="job-sync-one"))
    replay = asyncio.run(adapter.request_idempotent(**{**args, "request_id": "req-retry"},
        job_id="job-sync-other"))
    assert first == replay
    assert first["id"] == "job-sync-one" and first["operation"] == "sync_watch"
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM background_jobs WHERE job_type='sync_watch'").fetchone()[0] == 1
        row = db.execute("SELECT state,status_code,response_json FROM request_idempotency").fetchone()
        assert row["state"] == "completed" and row["status_code"] == 202
        assert json.loads(row["response_json"]) == first


def test_different_idempotency_keys_reuse_one_active_manual_job(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    adapter = D1ManualSyncTransactions(D1ShapedSQLite(fixture.store))
    first = asyncio.run(adapter.request_idempotent(resource_kind="watch",
        resource_id=watch["id"], owner_id="owner", job_id="job-one", now=fixture.now,
        idempotency_key="key-one", request_id="req-one"))
    second = asyncio.run(adapter.request_idempotent(resource_kind="watch",
        resource_id=watch["id"], owner_id="owner", job_id="job-two", now=fixture.now,
        idempotency_key="key-two", request_id="req-two"))
    assert first["id"] == second["id"] == "job-one"
    assert first["requestId"] == "req-one" and second["requestId"] == "req-two"
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM background_jobs WHERE job_type='sync_watch'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM request_idempotency WHERE state='completed'").fetchone()[0] == 2


def test_idempotent_manual_sync_rolls_back_job_and_receipt_on_archive_race(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    binding = D1ShapedSQLite(fixture.store)
    batches = 0

    def archive_before_write():
        nonlocal batches
        batches += 1
        if batches == 3:
            fixture.store.archive_watch(watch["id"], expected_revision=watch["revision"])

    binding.before_batch = archive_before_write
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(D1ManualSyncTransactions(binding).request_idempotent(
            resource_kind="watch", resource_id=watch["id"], owner_id="owner",
            job_id="job-raced", now=fixture.now, idempotency_key="key-raced",
            request_id="req-raced"))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM background_jobs WHERE id='job-raced'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM request_idempotency").fetchone()[0] == 0
