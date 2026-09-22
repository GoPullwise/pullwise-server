from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from pullwise_server.product_jobs import (
    ProductJobExecutor,
    ProductJobScheduler,
    TrustedTrigger,
)
from pullwise_server.product_store import ProductStore


class ProductJobsContractsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = ProductStore(os.path.join(self.temp_dir.name, "jobs.sqlite3"))
        self.store.initialize()
        self.scheduler = ProductJobScheduler(self.store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_analysis_job(
        self,
        suffix: str = "job",
        *,
        billing_owner_id: str = "usr_1",
        global_active_limit: int = 1000,
        owner_active_limit: int = 100,
    ) -> tuple[dict, dict, dict, dict]:
        source_id = f"release-{suffix}"
        context_id = f"watch:{suffix}"
        source = self.store.upsert_source_snapshot(
            source_id=source_id,
            source_type="release",
            external_key=f"github:release:{suffix}",
            repository_id="upstream-1",
            content={"body": "OAuth migration required."},
            source_facts={"releaseId": suffix},
            source_url=f"https://github.com/acme/upstream/releases/tag/{suffix}",
            processing_mode="model",
            completeness="complete",
            lifecycle="active",
            observed_at=1_800_000_000,
        )
        item = self.store.create_item(
            context_id=context_id,
            unit_type="update_release",
            unit_key=f"release:{suffix}",
        )
        self.store.set_source_context(
            source_id=source["id"],
            context_id=context_id,
            context_version=1,
            configuration_revision=1,
            authorization_revision=1,
            authorization_valid_until=1_900_000_000,
            accessible=True,
            billing_owner_id=billing_owner_id,
            item_id=item["id"],
            processing_status="pending",
            analysis_enabled=True,
            coverage={
                "state": "complete",
                "selectedUnits": 1,
                "rawSourcePartial": False,
                "limitations": [],
            },
        )
        reservation = self.store.reserve_processing_unit(
            charge_key=f"charge-release-{suffix}",
            billing_owner_id=billing_owner_id,
            period="cycle:1800000000",
            module="updates",
            limit=10,
        )
        job = self.scheduler.schedule_analysis(
            source_context_key=f"{source_id}:{context_id}",
            source_id=source["id"],
            context_id=context_id,
            reservation_id=reservation["reservationId"],
            trigger=TrustedTrigger.GITHUB_EVENT,
            global_active_limit=global_active_limit,
            owner_active_limit=owner_active_limit,
        )
        return source, item, reservation, job

    @staticmethod
    def assessment_for(source: dict) -> dict:
        return {
            "billingOwnerId": "usr_1",
            "sourceVersionId": source["sourceVersion"],
            "contextHash": "ctx-job",
            "evaluatedContextVersion": 1,
            "questionVersion": "updates-filter/v3",
            "extractorVersion": "updates-units/v1",
            "model": "jev-1.13.0",
            "inputHash": "input-release-job",
            "dependencies": [
                {"sourceId": source["id"], "sourceVersion": source["sourceVersion"]}
            ],
            "bindings": {
                "u0_relevance": {
                    "sourceId": source["id"],
                    "sourceVersion": source["sourceVersion"],
                    "changeUnitId": "u0",
                    "evidenceIds": [],
                    "contextVersion": 1,
                }
            },
            "answers": {"u0_relevance": {"choice": "relevant"}},
            "usage": {"inputTokens": 100, "outputTokens": 0},
        }

    def publish_claimed_job(
        self,
        executor: ProductJobExecutor,
        *,
        claim: dict,
        source: dict,
        item: dict,
        observed_at: int,
    ) -> dict:
        return executor.publish_validated_assessment(
            job_id=claim["id"],
            claim_token=claim["claimToken"],
            assessment=self.assessment_for(source),
            item_id=item["id"],
            expected_item_revision=item["revision"],
            sources=[
                {
                    "sourceId": source["id"],
                    "sourceVersion": source["sourceVersion"],
                    "sourceRevision": source["sourceRevision"],
                }
            ],
            context_fences=[
                {
                    "sourceId": source["id"],
                    "contextId": "watch:job",
                    "contextVersion": 1,
                    "configurationRevision": 1,
                    "authorizationRevision": 1,
                }
            ],
            snapshot={
                "module": "updates",
                "actionTypes": ["review_update"],
                "attentionState": "needs_action",
                "lifecycle": "active",
                "evidence": [],
            },
            observed_at=observed_at,
        )

    def test_manual_sync_can_only_enqueue_fact_sync(self) -> None:
        job = self.scheduler.request_manual_sync(
            resource_kind="repository",
            resource_id="repo_1",
            requester_id="usr_1",
        )

        self.assertEqual(job["jobType"], "sync_repository")
        self.assertEqual(job["trustedTrigger"], "manual_sync")
        self.assertEqual(self.store.count_jobs(job_type="analyze_source"), 0)

    def test_manual_sync_reuses_active_resource_job_even_with_new_request(self) -> None:
        first = self.scheduler.request_manual_sync(
            resource_kind="watch",
            resource_id="watch_1",
            requester_id="usr_1",
        )
        second = self.scheduler.request_manual_sync(
            resource_kind="watch",
            resource_id="watch_1",
            requester_id="usr_1",
        )

        self.assertEqual(second["id"], first["id"])
        self.assertTrue(second["reused"])

    def test_analysis_requires_non_forgeable_internal_trigger_enum(self) -> None:
        with self.assertRaisesRegex(ValueError, "trusted internal trigger"):
            self.scheduler.schedule_analysis(  # type: ignore[arg-type]
                source_context_key="source:1:context:1",
                source_id="source-1",
                context_id="context-1",
                reservation_id="reservation-1",
                trigger="github_event",
            )

        _source, _item, _reservation, job = self.create_analysis_job()
        self.assertEqual(job["jobType"], "analyze_source")
        self.assertEqual(job["trustedTrigger"], "github_event")

    def test_claimed_analysis_job_publishes_assessment_item_and_usage(self) -> None:
        source, item, reservation, job = self.create_analysis_job()
        executor = ProductJobExecutor(self.store)

        claim = executor.claim_next_analysis(now=1_800_000_001)
        result = self.publish_claimed_job(
            executor,
            claim=claim,
            source=source,
            item=item,
            observed_at=1_800_000_002,
        )

        self.assertEqual(claim["id"], job["id"])
        self.assertEqual(claim["status"], "running")
        self.assertEqual(claim["attempt"], 1)
        self.assertTrue(claim["claimToken"])
        self.assertEqual(result["assessment"]["status"], "succeeded")
        self.assertEqual(result["item"]["itemVersion"], 1)
        self.assertEqual(self.store.get_background_job(job["id"])["status"], "succeeded")
        usage = self.store.processing_usage(
            billing_owner_id="usr_1",
            period="cycle:1800000000",
        )
        self.assertEqual(usage["used"], 1)
        self.assertEqual(usage["reserved"], 0)
        self.assertEqual(reservation["state"], "reserved")

    def test_expired_analysis_claim_cannot_publish_or_consume(self) -> None:
        source, item, _reservation, job = self.create_analysis_job()
        executor = ProductJobExecutor(self.store)
        stale_claim = executor.claim_next_analysis(now=1_800_000_001, lease_seconds=1)

        with self.assertRaisesRegex(ValueError, "JOB_CLAIM_EXPIRED"):
            self.publish_claimed_job(
                executor,
                claim=stale_claim,
                source=source,
                item=item,
                observed_at=1_800_000_003,
            )

        usage = self.store.processing_usage(
            billing_owner_id="usr_1",
            period="cycle:1800000000",
        )
        self.assertEqual(usage["used"], 0)
        self.assertEqual(usage["reserved"], 1)
        self.assertEqual(self.store.list_items_for_billing_owner("usr_1"), [])
        with closing(self.store.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM assessments").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM item_versions").fetchone()[0], 0)

        fresh_claim = executor.claim_next_analysis(now=1_800_000_003)
        self.assertEqual(fresh_claim["id"], job["id"])
        self.assertEqual(fresh_claim["attempt"], 2)
        self.assertNotEqual(fresh_claim["claimToken"], stale_claim["claimToken"])

    def test_claim_recheck_cancels_disabled_analysis_and_releases_reservation(self) -> None:
        source, item, _reservation, job = self.create_analysis_job()
        self.store.set_source_context(
            source_id=source["id"],
            context_id="watch:job",
            context_version=1,
            configuration_revision=1,
            authorization_revision=1,
            authorization_valid_until=1_900_000_000,
            accessible=True,
            billing_owner_id="usr_1",
            item_id=item["id"],
            processing_status="analysis_disabled",
            analysis_enabled=False,
        )

        claim = ProductJobExecutor(self.store).claim_next_analysis(now=1_800_000_001)

        self.assertIsNone(claim)
        self.assertEqual(self.store.get_background_job(job["id"])["status"], "cancelled")
        usage = self.store.processing_usage(
            billing_owner_id="usr_1",
            period="cycle:1800000000",
        )
        self.assertEqual(usage["used"], 0)
        self.assertEqual(usage["reserved"], 0)

    def test_publication_rechecks_analysis_switch_after_claim(self) -> None:
        source, item, _reservation, job = self.create_analysis_job()
        executor = ProductJobExecutor(self.store)
        claim = executor.claim_next_analysis(now=1_800_000_001, lease_seconds=10)
        self.store.set_source_context(
            source_id=source["id"],
            context_id="watch:job",
            context_version=1,
            configuration_revision=1,
            authorization_revision=1,
            authorization_valid_until=1_900_000_000,
            accessible=True,
            billing_owner_id="usr_1",
            item_id=item["id"],
            processing_status="analysis_disabled",
            analysis_enabled=False,
        )

        with self.assertRaisesRegex(ValueError, "ANALYSIS_DISABLED"):
            self.publish_claimed_job(
                executor,
                claim=claim,
                source=source,
                item=item,
                observed_at=1_800_000_003,
            )

        self.assertIsNone(executor.claim_next_analysis(now=1_800_000_012))
        self.assertEqual(self.store.get_background_job(job["id"])["status"], "cancelled")
        usage = self.store.processing_usage(
            billing_owner_id="usr_1",
            period="cycle:1800000000",
        )
        self.assertEqual((usage["used"], usage["reserved"]), (0, 0))

    def test_publication_rechecks_configuration_revision_after_claim(self) -> None:
        source, item, _reservation, job = self.create_analysis_job()
        executor = ProductJobExecutor(self.store)
        claim = executor.claim_next_analysis(now=1_800_000_001, lease_seconds=10)
        self.store.set_source_context(
            source_id=source["id"],
            context_id="watch:job",
            context_version=1,
            configuration_revision=2,
            authorization_revision=1,
            authorization_valid_until=1_900_000_000,
            accessible=True,
            billing_owner_id="usr_1",
            item_id=item["id"],
            processing_status="pending",
            analysis_enabled=True,
        )

        with self.assertRaisesRegex(ValueError, "STALE_CONTEXT"):
            self.publish_claimed_job(
                executor,
                claim=claim,
                source=source,
                item=item,
                observed_at=1_800_000_003,
            )

        self.assertIsNone(executor.claim_next_analysis(now=1_800_000_012))
        self.assertEqual(self.store.get_background_job(job["id"])["status"], "superseded")
        usage = self.store.processing_usage(
            billing_owner_id="usr_1",
            period="cycle:1800000000",
        )
        self.assertEqual((usage["used"], usage["reserved"]), (0, 0))

    def test_retryable_analysis_failure_waits_and_stops_after_three_attempts(self) -> None:
        _source, _item, _reservation, job = self.create_analysis_job()
        executor = ProductJobExecutor(self.store)

        first = executor.claim_next_analysis(now=1_800_000_001)
        first_retry = executor.record_analysis_failure(
            job_id=first["id"],
            claim_token=first["claimToken"],
            now=1_800_000_002,
            retryable=True,
            next_attempt_at=1_800_000_010,
        )
        self.assertEqual(first_retry["status"], "retry_wait")
        self.assertEqual(first_retry["nextAttemptAt"], 1_800_000_010)
        self.assertIsNone(executor.claim_next_analysis(now=1_800_000_009))

        second = executor.claim_next_analysis(now=1_800_000_010)
        self.assertEqual(second["attempt"], 2)
        executor.record_analysis_failure(
            job_id=second["id"],
            claim_token=second["claimToken"],
            now=1_800_000_011,
            retryable=True,
            next_attempt_at=1_800_000_020,
        )
        third = executor.claim_next_analysis(now=1_800_000_020)
        self.assertEqual(third["attempt"], 3)
        terminal = executor.record_analysis_failure(
            job_id=third["id"],
            claim_token=third["claimToken"],
            now=1_800_000_021,
            retryable=True,
            next_attempt_at=1_800_000_030,
        )

        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["attempt"], 3)
        self.assertIsNone(executor.claim_next_analysis(now=1_800_000_030))
        self.assertEqual(self.store.get_background_job(job["id"])["status"], "failed")
        usage = self.store.processing_usage(
            billing_owner_id="usr_1",
            period="cycle:1800000000",
        )
        self.assertEqual((usage["used"], usage["reserved"]), (0, 0))

    def test_old_claim_cannot_change_reclaimed_job_retry_state(self) -> None:
        self.create_analysis_job()
        executor = ProductJobExecutor(self.store)
        old_claim = executor.claim_next_analysis(now=1_800_000_001, lease_seconds=1)
        new_claim = executor.claim_next_analysis(now=1_800_000_003)

        with self.assertRaisesRegex(ValueError, "JOB_CLAIM_LOST"):
            executor.record_analysis_failure(
                job_id=old_claim["id"],
                claim_token=old_claim["claimToken"],
                now=1_800_000_004,
                retryable=True,
                next_attempt_at=1_800_000_010,
            )

        current = self.store.get_background_job(new_claim["id"])
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["attempt"], 2)

    def test_analysis_queue_limits_are_atomic_and_release_rejected_reservation(self) -> None:
        self.create_analysis_job("limit-a", global_active_limit=1)

        with self.assertRaisesRegex(ValueError, "ANALYSIS_QUEUE_GLOBAL_LIMIT"):
            self.create_analysis_job(
                "limit-b",
                billing_owner_id="usr_2",
                global_active_limit=1,
            )

        self.assertEqual(self.store.count_jobs(job_type="analyze_source"), 1)
        rejected_usage = self.store.processing_usage(
            billing_owner_id="usr_2",
            period="cycle:1800000000",
        )
        self.assertEqual((rejected_usage["used"], rejected_usage["reserved"]), (0, 0))

        owner_store = ProductStore(os.path.join(self.temp_dir.name, "owner-limit.sqlite3"))
        owner_store.initialize()
        owner_scheduler = ProductJobScheduler(owner_store)
        original_store, original_scheduler = self.store, self.scheduler
        self.store, self.scheduler = owner_store, owner_scheduler
        try:
            self.create_analysis_job("owner-a", owner_active_limit=1)
            with self.assertRaisesRegex(ValueError, "ANALYSIS_QUEUE_OWNER_LIMIT"):
                self.create_analysis_job("owner-b", owner_active_limit=1)
        finally:
            self.store, self.scheduler = original_store, original_scheduler

        self.assertEqual(owner_store.count_jobs(job_type="analyze_source"), 1)
        owner_usage = owner_store.processing_usage(
            billing_owner_id="usr_1",
            period="cycle:1800000000",
        )
        self.assertEqual((owner_usage["used"], owner_usage["reserved"]), (0, 1))

    def test_analysis_claim_rotates_across_billing_owners(self) -> None:
        first_a = self.create_analysis_job("fair-a1", billing_owner_id="usr_a")[3]
        second_a = self.create_analysis_job("fair-a2", billing_owner_id="usr_a")[3]
        first_b = self.create_analysis_job("fair-b1", billing_owner_id="usr_b")[3]
        executor = ProductJobExecutor(self.store)

        claim_a = executor.claim_next_analysis(now=1_800_000_001)
        executor.record_analysis_failure(
            job_id=claim_a["id"],
            claim_token=claim_a["claimToken"],
            now=1_800_000_002,
            retryable=False,
            next_attempt_at=None,
        )
        claim_b = executor.claim_next_analysis(now=1_800_000_003)
        executor.record_analysis_failure(
            job_id=claim_b["id"],
            claim_token=claim_b["claimToken"],
            now=1_800_000_004,
            retryable=False,
            next_attempt_at=None,
        )
        claim_second_a = executor.claim_next_analysis(now=1_800_000_005)

        self.assertEqual(claim_a["id"], first_a["id"])
        self.assertEqual(claim_b["id"], first_b["id"])
        self.assertEqual(claim_second_a["id"], second_a["id"])

    def test_new_source_version_supersedes_queued_job_and_releases_old_reservation(self) -> None:
        source, _item, _reservation, old_job = self.create_analysis_job("coalesce")
        updated_source = self.store.upsert_source_snapshot(
            source_id=source["id"],
            source_type="release",
            external_key="github:release:coalesce",
            repository_id="upstream-1",
            content={"body": "OAuth migration changed again."},
            source_facts={"releaseId": "coalesce"},
            source_url="https://github.com/acme/upstream/releases/tag/coalesce",
            processing_mode="model",
            completeness="complete",
            lifecycle="active",
            observed_at=1_800_000_001,
        )
        replacement_reservation = self.store.reserve_processing_unit(
            charge_key="charge-release-coalesce-v2",
            billing_owner_id="usr_1",
            period="cycle:1800000000",
            module="updates",
            limit=10,
        )

        replacement = self.scheduler.schedule_analysis(
            source_context_key="release-coalesce:watch:coalesce",
            source_id=source["id"],
            context_id="watch:coalesce",
            reservation_id=replacement_reservation["reservationId"],
            trigger=TrustedTrigger.GITHUB_EVENT,
        )

        self.assertNotEqual(replacement["id"], old_job["id"])
        self.assertEqual(replacement["generation"], 2)
        self.assertEqual(replacement["sourceVersionId"], updated_source["sourceVersion"])
        self.assertEqual(self.store.get_background_job(old_job["id"])["status"], "superseded")
        self.assertEqual(self.store.count_jobs(job_type="analyze_source"), 2)
        usage = self.store.processing_usage(
            billing_owner_id="usr_1",
            period="cycle:1800000000",
        )
        self.assertEqual((usage["used"], usage["reserved"]), (0, 1))
        claim = ProductJobExecutor(self.store).claim_next_analysis(now=1_800_000_002)
        self.assertEqual(claim["id"], replacement["id"])

    def test_exhausted_input_cannot_reset_attempts_with_new_generation(self) -> None:
        source, _item, _reservation, failed_job = self.create_analysis_job("exhausted")
        executor = ProductJobExecutor(self.store)
        for attempt, now in enumerate((1_800_000_001, 1_800_000_010, 1_800_000_020), start=1):
            claim = executor.claim_next_analysis(now=now)
            terminal = executor.record_analysis_failure(
                job_id=claim["id"],
                claim_token=claim["claimToken"],
                now=now + 1,
                retryable=True,
                next_attempt_at=now + 9,
            )
            self.assertEqual(terminal["attempt"], attempt)
        self.assertEqual(terminal["status"], "failed")
        replacement_reservation = self.store.reserve_processing_unit(
            charge_key="charge-release-exhausted-again",
            billing_owner_id="usr_1",
            period="cycle:1800000000",
            module="updates",
            limit=10,
        )

        replay = self.scheduler.schedule_analysis(
            source_context_key="release-exhausted:watch:exhausted",
            source_id=source["id"],
            context_id="watch:exhausted",
            reservation_id=replacement_reservation["reservationId"],
            trigger=TrustedTrigger.SCHEDULED_DISCOVERY,
        )

        self.assertEqual(replay["id"], failed_job["id"])
        self.assertTrue(replay["reused"])
        self.assertEqual(replay["status"], "failed")
        self.assertEqual(replay["attempt"], 3)
        self.assertEqual(self.store.count_jobs(job_type="analyze_source"), 1)
        usage = self.store.processing_usage(
            billing_owner_id="usr_1",
            period="cycle:1800000000",
        )
        self.assertEqual((usage["used"], usage["reserved"]), (0, 0))

    def test_running_old_version_is_fenced_until_its_lease_before_successor_claim(self) -> None:
        source, item, _reservation, old_job = self.create_analysis_job("running-coalesce")
        executor = ProductJobExecutor(self.store)
        old_claim = executor.claim_next_analysis(now=1_800_000_001, lease_seconds=10)
        updated_source = self.store.upsert_source_snapshot(
            source_id=source["id"],
            source_type="release",
            external_key="github:release:running-coalesce",
            repository_id="upstream-1",
            content={"body": "A later authoritative release body."},
            source_facts={"releaseId": "running-coalesce"},
            source_url="https://github.com/acme/upstream/releases/tag/running-coalesce",
            processing_mode="model",
            completeness="complete",
            lifecycle="active",
            observed_at=1_800_000_002,
        )
        reservation = self.store.reserve_processing_unit(
            charge_key="charge-release-running-coalesce-v2",
            billing_owner_id="usr_1",
            period="cycle:1800000000",
            module="updates",
            limit=10,
        )
        successor = self.scheduler.schedule_analysis(
            source_context_key="release-running-coalesce:watch:running-coalesce",
            source_id=source["id"],
            context_id="watch:running-coalesce",
            reservation_id=reservation["reservationId"],
            trigger=TrustedTrigger.GITHUB_EVENT,
        )

        with self.assertRaisesRegex(ValueError, "JOB_CLAIM_LOST"):
            self.publish_claimed_job(
                executor,
                claim=old_claim,
                source=source,
                item=item,
                observed_at=1_800_000_003,
            )
        self.assertEqual(self.store.get_background_job(old_job["id"])["status"], "superseded")
        self.assertEqual(successor["sourceVersionId"], updated_source["sourceVersion"])
        self.assertEqual(successor["nextAttemptAt"], 1_800_000_011)
        self.assertIsNone(executor.claim_next_analysis(now=1_800_000_010))
        successor_claim = executor.claim_next_analysis(now=1_800_000_011)
        self.assertEqual(successor_claim["id"], successor["id"])
        usage = self.store.processing_usage(
            billing_owner_id="usr_1",
            period="cycle:1800000000",
        )
        self.assertEqual((usage["used"], usage["reserved"]), (0, 1))

    def test_get_style_store_reads_never_create_analysis_jobs(self) -> None:
        before = self.store.count_jobs(job_type="analyze_source")
        for _ in range(5):
            self.store.list_jobs()
        after = self.store.count_jobs(job_type="analyze_source")

        self.assertEqual(before, after)

    def test_initialize_upgrades_pre_claim_background_job_table(self) -> None:
        legacy_path = os.path.join(self.temp_dir.name, "pre-claim.sqlite3")
        with closing(sqlite3.connect(legacy_path)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE background_jobs (
                    id TEXT PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    logical_key TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    trusted_trigger TEXT NOT NULL,
                    requester_id TEXT,
                    state TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )

        ProductStore(legacy_path).initialize()

        with closing(sqlite3.connect(legacy_path)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(background_jobs)")}
        self.assertTrue(
            {
                "billing_owner_id",
                "source_id",
                "context_id",
                "reservation_id",
                "source_revision",
                "source_version_id",
                "context_version",
                "configuration_revision",
                "authorization_revision",
                "claim_token",
                "claimed_until",
            }.issubset(columns)
        )
        with closing(sqlite3.connect(legacy_path)) as connection:
            fairness_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'analysis_claim_owners'"
            ).fetchone()
        self.assertIsNotNone(fairness_table)


if __name__ == "__main__":
    unittest.main()
