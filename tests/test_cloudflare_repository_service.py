"""Trusted D1 RepositoryService writes preserve owner capacity and revisions."""
import asyncio
import json
import sqlite3
from contextlib import closing

import pytest

from pullwise_server.cloudflare_repository_adapter import D1RepositoryTransactions
from pullwise_server.product_entitlement_rules import entitlements_for_user
from test_cloudflare_account_adapter import D1ShapedSQLite
from test_cloudflare_server_mapping import seed


def arguments(fixture, *, repository_id="repo:123", expected_revision=0):
    return dict(repository_id=repository_id, installation_id="inst-1",
        owner_id="owner", expected_revision=expected_revision, enabled=True,
        modules={"pr": True, "ci": False},
        analysis_enabled={"pr": False, "ci": False},
        allow_member_sync=False, default_assignee_id=None,
        priority_order=0, now=fixture.now)


def test_repository_service_create_and_switch_share_store_dto(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    adapter = D1RepositoryTransactions(D1ShapedSQLite(fixture.store))
    created = asyncio.run(adapter.put_service(**arguments(fixture)))
    assert created == fixture.store.get_repository_service("repo:123")
    changed = asyncio.run(adapter.put_service(**{**arguments(fixture,
        expected_revision=created["revision"]),
        "modules": {"pr": True, "ci": True}}))
    assert changed["revision"] == 2 and changed["modules"]["ci"] is True
    assert changed == fixture.store.get_repository_service("repo:123")
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM provider_attempts").fetchone()[0] == 0


def test_repository_service_create_rechecks_last_owner_slot(tmp_path):
    fixture, _, frozen = seed(tmp_path / "domain.db")
    limit = entitlements_for_user(json.loads(frozen),
        timestamp=fixture.now)["entitlements"]["activeRepositoryLimit"]
    for number in range(limit - 1):
        fixture.store.put_repository_service(repository_id=f"repo:{number}",
            installation_id="inst-1", billing_owner_id="owner",
            expected_revision=0, enabled=True,
            modules={"pr": True, "ci": False},
            analysis_enabled={"pr": False, "ci": False},
            allow_member_sync=False, default_assignee_id=None,
            priority_order=number)
    binding = D1ShapedSQLite(fixture.store)

    def competitor_takes_slot():
        fixture.store.put_repository_service(repository_id="repo:competitor",
            installation_id="inst-1", billing_owner_id="owner",
            expected_revision=0, enabled=True,
            modules={"pr": True, "ci": False},
            analysis_enabled={"pr": False, "ci": False},
            allow_member_sync=False, default_assignee_id=None,
            priority_order=99)

    binding.before_batch = competitor_takes_slot
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(D1RepositoryTransactions(binding).put_service(**arguments(
            fixture, repository_id="repo:new")))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM repository_services WHERE enabled=1").fetchone()[0] == limit
        assert db.execute("SELECT COUNT(*) FROM repository_services WHERE repository_id='repo:new'").fetchone()[0] == 0


def test_repository_switch_cancels_queued_pr_and_releases_reservation(tmp_path):
    fixture, job, _ = seed(tmp_path / "domain.db")
    adapter = D1RepositoryTransactions(D1ShapedSQLite(fixture.store))
    initial = asyncio.run(adapter.put_service(**{**arguments(fixture,
        repository_id="repo"), "analysis_enabled": {"pr": True, "ci": False}}))
    changed = asyncio.run(adapter.put_service(**{**arguments(fixture,
        repository_id="repo", expected_revision=initial["revision"]),
        "analysis_enabled": {"pr": False, "ci": False}}))
    assert changed["revision"] == 2
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT state FROM background_jobs WHERE id=?", (job["id"],)).fetchone()[0] == "cancelled"
        assert db.execute("SELECT state FROM processing_usage_ledger WHERE charge_key='charge'").fetchone()[0] == "released"
        assert db.execute("SELECT reserved FROM processing_usage_buckets").fetchone()[0] == 0
        assert tuple(db.execute("SELECT configuration_revision,analysis_enabled FROM source_contexts WHERE source_id='1'").fetchone()) == (2, 0)


def test_repository_switch_preserves_running_lease_and_fences_context(tmp_path):
    fixture, job, _ = seed(tmp_path / "domain.db")
    adapter = D1RepositoryTransactions(D1ShapedSQLite(fixture.store))
    initial = asyncio.run(adapter.put_service(**{**arguments(fixture,
        repository_id="repo"), "analysis_enabled": {"pr": True, "ci": False}}))
    with fixture.store._immediate() as db:
        db.execute("UPDATE background_jobs SET state='running',claim_token='running' WHERE id=?", (job["id"],))
        db.execute("UPDATE processing_usage_ledger SET state='reserved' WHERE charge_key='charge'")
        db.execute("UPDATE processing_usage_buckets SET reserved=1")
    asyncio.run(adapter.put_service(**{**arguments(fixture,
        repository_id="repo", expected_revision=initial["revision"]),
        "analysis_enabled": {"pr": False, "ci": False}}))
    with closing(fixture.store.connect()) as db:
        assert tuple(db.execute("SELECT state,claim_token FROM background_jobs WHERE id=?", (job["id"],)).fetchone()) == ("running", "running")
        assert db.execute("SELECT state FROM processing_usage_ledger WHERE charge_key='charge'").fetchone()[0] == "reserved"
        assert tuple(db.execute("SELECT configuration_revision,analysis_enabled FROM source_contexts WHERE source_id='1'").fetchone()) == (2, 0)


def test_repository_switch_rolls_back_when_reservation_is_missing(tmp_path):
    fixture, job, _ = seed(tmp_path / "domain.db")
    adapter = D1RepositoryTransactions(D1ShapedSQLite(fixture.store))
    initial = asyncio.run(adapter.put_service(**{**arguments(fixture,
        repository_id="repo"), "analysis_enabled": {"pr": True, "ci": False}}))
    with fixture.store._immediate() as db:
        db.execute("UPDATE background_jobs SET state='queued' WHERE id=?", (job["id"],))
        db.execute("UPDATE processing_usage_ledger SET state='released' WHERE charge_key='charge'")
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(adapter.put_service(**{**arguments(fixture,
            repository_id="repo", expected_revision=initial["revision"]),
            "analysis_enabled": {"pr": False, "ci": False}}))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT revision FROM repository_services WHERE repository_id='repo'").fetchone()[0] == 1
        assert db.execute("SELECT state FROM background_jobs WHERE id=?", (job["id"],)).fetchone()[0] == "queued"
        assert db.execute("SELECT analysis_enabled FROM source_contexts WHERE source_id='1'").fetchone()[0] == 1
