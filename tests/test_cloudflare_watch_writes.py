"""Public-watch D1 commands preserve scope versions and owner limits."""
import asyncio
import json
import sqlite3
from contextlib import closing

import pytest

from pullwise_server.cloudflare_watch_adapter import D1WatchTransactions
from test_cloudflare_account_adapter import D1ShapedSQLite
from test_cloudflare_server_mapping import seed, mapping, execute, claim_args, publication_args
from pullwise_server.product_entitlement_rules import entitlements_for_user


def test_public_watch_creation_uses_persisted_owner_entitlement(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    adapter = D1WatchTransactions(D1ShapedSQLite(fixture.store))
    created = asyncio.run(adapter.create_public_watch(owner_id="owner",
        resolved_public_repository_id="github:101", interests=["OAuth"],
        enabled=True, analysis_enabled=False, now=fixture.now))
    assert created == fixture.store.get_watch(created["id"])
    assert created["contextVersion"] == 1
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM processing_controls WHERE control_key=?",
            (created["watchScopeKey"],)).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM provider_attempts").fetchone()[0] == 0


def test_recreated_watch_scope_advances_context_version_after_changed_interest(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    adapter = D1WatchTransactions(D1ShapedSQLite(fixture.store))
    first = asyncio.run(adapter.create_public_watch(owner_id="owner",
        resolved_public_repository_id="github:101", interests=["OAuth"],
        enabled=True, analysis_enabled=False, now=fixture.now))
    with fixture.store._immediate() as db:
        db.execute("UPDATE update_watches SET archived_at=? WHERE id=?", (fixture.now, first["id"]))
    second = asyncio.run(adapter.create_public_watch(owner_id="owner",
        resolved_public_repository_id="github:101", interests=["database"],
        enabled=True, analysis_enabled=False, now=fixture.now))
    assert second["watchScopeKey"] == first["watchScopeKey"]
    assert second["contextVersion"] == 2
    with pytest.raises(ValueError, match="WATCH_ALREADY_EXISTS"):
        asyncio.run(adapter.create_public_watch(owner_id="owner",
            resolved_public_repository_id="github:101", interests=["OAuth"],
            enabled=True, analysis_enabled=False, now=fixture.now))
    with fixture.store._immediate() as db:
        db.execute("UPDATE update_watches SET archived_at=? WHERE id=?", (fixture.now, second["id"]))
    third = asyncio.run(adapter.create_public_watch(owner_id="owner",
        resolved_public_repository_id="github:101", interests=["OAuth"],
        enabled=True, analysis_enabled=False, now=fixture.now))
    assert third["contextVersion"] == 3


def test_watch_create_rechecks_account_and_last_active_slot_in_batch(tmp_path):
    fixture, _, frozen = seed(tmp_path / "domain.db")
    limit = entitlements_for_user(json.loads(frozen),
        timestamp=fixture.now)["entitlements"]["activeWatchLimit"]
    for number in range(limit - 1):
        fixture.store.create_watch(owner_id="owner", target_repository_id=None,
            upstream_repository_id=f"github:{number + 100}", billing_owner_id="owner",
            interests=["OAuth"], enabled=True, analysis_enabled=False)
    binding = D1ShapedSQLite(fixture.store)
    adapter = D1WatchTransactions(binding)

    def competitor_takes_last_slot():
        fixture.store.create_watch(owner_id="owner", target_repository_id=None,
            upstream_repository_id="github:999", billing_owner_id="owner",
            interests=["OAuth"], enabled=True, analysis_enabled=False)

    binding.before_batch = competitor_takes_last_slot
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(adapter.create_public_watch(owner_id="owner",
            resolved_public_repository_id="github:200", interests=["OAuth"],
            enabled=True, analysis_enabled=False, now=fixture.now))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM update_watches WHERE enabled=1 AND archived_at IS NULL").fetchone()[0] == limit
        assert db.execute("SELECT COUNT(*) FROM watch_controls WHERE upstream_repository_id='github:200'").fetchone()[0] == 0



def test_watch_create_rejects_account_change_before_batch(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    binding = D1ShapedSQLite(fixture.store)

    def revoke_account():
        with fixture.store._immediate() as db:
            db.execute("UPDATE app_state SET payload='{}' WHERE name='users'")

    binding.before_batch = revoke_account
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(D1WatchTransactions(binding).create_public_watch(
            owner_id="owner", resolved_public_repository_id="github:101",
            interests=["OAuth"], enabled=True, analysis_enabled=False,
            now=fixture.now))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM update_watches").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM watch_controls").fetchone()[0] == 0


def test_archive_watch_revokes_context_and_releases_queued_reservation_atomically(tmp_path):
    fixture, job, _ = seed(tmp_path / "domain.db")
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    with fixture.store._immediate() as db:
        db.execute("UPDATE source_contexts SET watch_id=? WHERE source_id='1'", (watch["id"],))
    adapter = D1WatchTransactions(D1ShapedSQLite(fixture.store))
    asyncio.run(adapter.archive_watch(owner_id="owner", watch_id=watch["id"],
        expected_revision=watch["revision"], now=fixture.now))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT archived_at FROM update_watches WHERE id=?", (watch["id"],)).fetchone()[0] == fixture.now
        assert db.execute("SELECT accessible FROM source_contexts WHERE source_id='1'").fetchone()[0] == 0
        assert db.execute("SELECT state FROM background_jobs WHERE id=?", (job["id"],)).fetchone()[0] == "cancelled"
        assert db.execute("SELECT state FROM processing_usage_ledger WHERE charge_key='charge'").fetchone()[0] == "released"
        assert db.execute("SELECT reserved FROM processing_usage_buckets").fetchone()[0] == 0


def test_archive_watch_rolls_back_on_inconsistent_reserved_bucket(tmp_path):
    fixture, job, _ = seed(tmp_path / "domain.db")
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    with fixture.store._immediate() as db:
        db.execute("UPDATE source_contexts SET watch_id=? WHERE source_id='1'", (watch["id"],))
        db.execute("UPDATE processing_usage_buckets SET reserved=0")
    adapter = D1WatchTransactions(D1ShapedSQLite(fixture.store))
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(adapter.archive_watch(owner_id="owner", watch_id=watch["id"],
            expected_revision=watch["revision"], now=fixture.now))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT archived_at FROM update_watches WHERE id=?", (watch["id"],)).fetchone()[0] is None
        assert db.execute("SELECT accessible FROM source_contexts WHERE source_id='1'").fetchone()[0] == 1
        assert db.execute("SELECT state FROM background_jobs WHERE id=?", (job["id"],)).fetchone()[0] == "queued"


def test_archive_watch_fences_running_job_and_keeps_attempt_spent(tmp_path):
    fixture, job, frozen = seed(tmp_path / "domain.db")
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    with fixture.store._immediate() as db:
        db.execute("UPDATE source_contexts SET watch_id=? WHERE source_id='1'", (watch["id"],))
    publication = publication_args(fixture, job, frozen)
    execute(fixture.store, mapping().claim(**claim_args(fixture, job, frozen)))
    asyncio.run(D1WatchTransactions(D1ShapedSQLite(fixture.store)).archive_watch(
        owner_id="owner", watch_id=watch["id"],
        expected_revision=watch["revision"], now=fixture.now))
    with pytest.raises(sqlite3.IntegrityError):
        execute(fixture.store, mapping().publication(**publication))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT state FROM background_jobs WHERE id=?", (job["id"],)).fetchone()[0] == "cancelled"
        assert db.execute("SELECT reserved FROM processing_usage_buckets").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM provider_attempts").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM assessments").fetchone()[0] == 0


def test_sqlite_archive_matches_d1_job_and_reservation_cascade(tmp_path):
    fixture, job, _ = seed(tmp_path / "domain.db")
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=False)
    with fixture.store._immediate() as db:
        db.execute("UPDATE source_contexts SET watch_id=? WHERE source_id='1'", (watch["id"],))
    fixture.store.archive_watch(watch["id"], expected_revision=watch["revision"])
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT accessible FROM source_contexts WHERE source_id='1'").fetchone()[0] == 0
        assert db.execute("SELECT state FROM background_jobs WHERE id=?", (job["id"],)).fetchone()[0] == "cancelled"
        assert db.execute("SELECT state FROM processing_usage_ledger WHERE charge_key='charge'").fetchone()[0] == "released"
        assert db.execute("SELECT reserved FROM processing_usage_buckets").fetchone()[0] == 0


def test_d1_watch_update_preserves_scope_and_monotonic_semantic_version(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    adapter = D1WatchTransactions(D1ShapedSQLite(fixture.store))
    original = asyncio.run(adapter.create_public_watch(owner_id="owner",
        resolved_public_repository_id="github:101", interests=["OAuth"],
        enabled=True, analysis_enabled=False, now=fixture.now))
    changed = asyncio.run(adapter.update_public_watch(owner_id="owner",
        watch_id=original["id"], expected_revision=original["revision"],
        changes={"interests": ["database"], "analysisEnabled": True},
        now=fixture.now))
    restored = asyncio.run(adapter.update_public_watch(owner_id="owner",
        watch_id=original["id"], expected_revision=changed["revision"],
        changes={"interests": ["OAuth"]}, now=fixture.now))
    assert [original["contextVersion"], changed["contextVersion"],
            restored["contextVersion"]] == [1, 2, 3]
    assert restored["watchScopeKey"] == original["watchScopeKey"]
    assert restored["contextHash"] == original["contextHash"]
    assert restored["analysisEnabled"] is True
    assert restored == fixture.store.get_watch(original["id"])


def test_d1_analysis_toggle_changes_revision_without_semantic_version(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    adapter = D1WatchTransactions(D1ShapedSQLite(fixture.store))
    original = asyncio.run(adapter.create_public_watch(owner_id="owner",
        resolved_public_repository_id="github:101", interests=["OAuth"],
        enabled=True, analysis_enabled=True, now=fixture.now))
    disabled = asyncio.run(adapter.update_public_watch(owner_id="owner",
        watch_id=original["id"], expected_revision=original["revision"],
        changes={"analysisEnabled": False}, now=fixture.now))
    assert disabled["revision"] == original["revision"] + 1
    assert disabled["contextVersion"] == original["contextVersion"]
    assert disabled["contextHash"] == original["contextHash"]


def test_watch_update_disables_queued_analysis_and_releases_reservation(tmp_path):
    fixture, job, _ = seed(tmp_path / "domain.db")
    adapter = D1WatchTransactions(D1ShapedSQLite(fixture.store))
    watch = asyncio.run(adapter.create_public_watch(owner_id="owner",
        resolved_public_repository_id="github:101", interests=["OAuth"],
        enabled=True, analysis_enabled=True, now=fixture.now))
    with fixture.store._immediate() as db:
        db.execute("UPDATE source_contexts SET watch_id=? WHERE source_id='1'", (watch["id"],))
    changed = asyncio.run(adapter.update_public_watch(owner_id="owner",
        watch_id=watch["id"], expected_revision=watch["revision"],
        changes={"analysisEnabled": False}, now=fixture.now))
    assert changed["contextVersion"] == watch["contextVersion"]
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT state FROM background_jobs WHERE id=?", (job["id"],)).fetchone()[0] == "cancelled"
        assert db.execute("SELECT state FROM processing_usage_ledger WHERE charge_key='charge'").fetchone()[0] == "released"
        assert db.execute("SELECT reserved FROM processing_usage_buckets").fetchone()[0] == 0
        assert tuple(db.execute("SELECT configuration_revision,analysis_enabled FROM source_contexts WHERE source_id='1'").fetchone()) == (2, 0)


def test_watch_semantic_update_fences_running_job_without_refunding_attempt(tmp_path):
    fixture, job, frozen = seed(tmp_path / "domain.db")
    adapter = D1WatchTransactions(D1ShapedSQLite(fixture.store))
    watch = asyncio.run(adapter.create_public_watch(owner_id="owner",
        resolved_public_repository_id="github:101", interests=["OAuth"],
        enabled=True, analysis_enabled=True, now=fixture.now))
    with fixture.store._immediate() as db:
        db.execute("UPDATE source_contexts SET watch_id=? WHERE source_id='1'", (watch["id"],))
    publication = publication_args(fixture, job, frozen)
    execute(fixture.store, mapping().claim(**claim_args(fixture, job, frozen)))
    changed = asyncio.run(adapter.update_public_watch(owner_id="owner",
        watch_id=watch["id"], expected_revision=watch["revision"],
        changes={"interests": ["database"]}, now=fixture.now))
    assert changed["contextVersion"] == 2
    with pytest.raises(sqlite3.IntegrityError):
        execute(fixture.store, mapping().publication(**publication))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT state FROM background_jobs WHERE id=?", (job["id"],)).fetchone()[0] == "running"
        assert db.execute("SELECT COUNT(*) FROM provider_attempts").fetchone()[0] == 1
        assert tuple(db.execute("SELECT context_version,context_stale FROM source_contexts WHERE source_id='1'").fetchone()) == (2, 1)


def test_sqlite_watch_update_matches_d1_queued_cancellation(tmp_path):
    fixture, job, _ = seed(tmp_path / "domain.db")
    watch = fixture.store.create_watch(owner_id="owner", target_repository_id=None,
        upstream_repository_id="github:101", billing_owner_id="owner",
        interests=["OAuth"], enabled=True, analysis_enabled=True)
    with fixture.store._immediate() as db:
        db.execute("UPDATE source_contexts SET watch_id=? WHERE source_id='1'", (watch["id"],))
    fixture.store.update_watch(watch["id"], expected_revision=watch["revision"],
        analysis_enabled=False)
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT state FROM background_jobs WHERE id=?", (job["id"],)).fetchone()[0] == "cancelled"
        assert db.execute("SELECT reserved FROM processing_usage_buckets").fetchone()[0] == 0
        assert tuple(db.execute("SELECT configuration_revision,analysis_enabled FROM source_contexts WHERE source_id='1'").fetchone()) == (2, 0)
