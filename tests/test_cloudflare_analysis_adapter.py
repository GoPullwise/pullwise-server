"""Async D1 claim/publication use durable owner and claim authority."""
import asyncio
import json
import sqlite3
from contextlib import closing

import pytest

from pullwise_server.cloudflare_analysis_adapter import D1AnalysisTransactions
from test_cloudflare_account_adapter import D1ShapedSQLite
from test_cloudflare_server_mapping import seed, claim_args, publication_args


def test_async_claim_and_publication_use_persisted_account_and_claim_revision(tmp_path):
    fixture, job, frozen = seed(tmp_path / "domain.db")
    args = claim_args(fixture, job, frozen)
    publish = publication_args(fixture, job, frozen)
    binding = D1ShapedSQLite(fixture.store)
    adapter = D1AnalysisTransactions(binding)
    asyncio.run(adapter.claim(job_id=job["id"], token=args["token"], now=fixture.now,
        global_monthly_limit=args["global_monthly_limit"],
        owner_rolling_limit=args["owner_rolling_limit"],
        global_rolling_limit=args["global_rolling_limit"]))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT account_revision FROM d1_claim_authority").fetchone()[0] == 1
    asyncio.run(adapter.publication(**{key: value for key, value in publish.items()
        if key not in {"account_snapshot", "account_revision"}}))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM assessments").fetchone()[0] == 1
        assert db.execute("SELECT state FROM processing_usage_ledger").fetchone()[0] == "consumed"
    assert binding.batch_count == 2


def test_async_publication_rejects_account_transition_after_claim(tmp_path):
    fixture, job, frozen = seed(tmp_path / "domain.db")
    args = claim_args(fixture, job, frozen)
    publish = publication_args(fixture, job, frozen)
    binding = D1ShapedSQLite(fixture.store)
    adapter = D1AnalysisTransactions(binding)
    asyncio.run(adapter.claim(job_id=job["id"], token=args["token"], now=fixture.now,
        global_monthly_limit=args["global_monthly_limit"],
        owner_rolling_limit=args["owner_rolling_limit"],
        global_rolling_limit=args["global_rolling_limit"]))
    changed = json.dumps({**json.loads(frozen), "githubLogin": "changed"}, separators=(",", ":"))
    from pullwise_server.cloudflare_account_adapter import D1AccountTransactions
    asyncio.run(D1AccountTransactions(binding).stage_account_write(owner_id="owner",
        expected_revision=1, next_account_json=changed, now=fixture.now))
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(adapter.publication(**{key: value for key, value in publish.items()
            if key not in {"account_snapshot", "account_revision"}}))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM assessments").fetchone()[0] == 0
        assert db.execute("SELECT state FROM processing_usage_ledger").fetchone()[0] == "reserved"


def test_async_reservation_derives_account_revision_and_limit_from_d1(tmp_path):
    fixture, _, frozen = seed(tmp_path / "domain.db")
    binding = D1ShapedSQLite(fixture.store)
    adapter = D1AnalysisTransactions(binding)
    asyncio.run(adapter.reserve_first_processing_unit(owner_id="owner", charge_key="second",
        reservation_id="second-reservation", module="ci", now=fixture.now))
    with closing(fixture.store.connect()) as db:
        assert tuple(db.execute("SELECT used,reserved,limit_value FROM processing_usage_buckets").fetchone()) == (0, 2, 5000)
    changed = json.dumps({**json.loads(frozen), "githubLogin": "changed"}, separators=(",", ":"))
    from pullwise_server.cloudflare_account_adapter import D1AccountTransactions
    asyncio.run(D1AccountTransactions(binding).stage_account_write(owner_id="owner",
        expected_revision=1, next_account_json=changed, now=fixture.now))
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(adapter.reserve_first_processing_unit(owner_id="owner", charge_key="third",
            reservation_id="third-reservation", module="ci", now=fixture.now))


def test_async_claim_failure_retains_attempt_spend_and_releases_terminal_reservation(tmp_path):
    fixture, job, frozen = seed(tmp_path / "domain.db")
    args = claim_args(fixture, job, frozen)
    binding = D1ShapedSQLite(fixture.store)
    adapter = D1AnalysisTransactions(binding)
    asyncio.run(adapter.claim(job_id=job["id"], token=args["token"], now=fixture.now,
        global_monthly_limit=10, owner_rolling_limit=6, global_rolling_limit=60))
    asyncio.run(adapter.record_claim_failure(job_id=job["id"], token=args["token"],
        now=fixture.now + 1, retryable=False, next_attempt_at=None))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT state FROM background_jobs").fetchone()[0] == "failed"
        assert db.execute("SELECT state FROM processing_usage_ledger").fetchone()[0] == "released"
        assert db.execute("SELECT COUNT(*) FROM provider_attempts").fetchone()[0] == 1
