"""Trusted D1 manual fact-sync queue never reserves model processing."""
import asyncio
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
