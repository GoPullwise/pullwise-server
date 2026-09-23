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
