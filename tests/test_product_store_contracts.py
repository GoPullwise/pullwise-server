from __future__ import annotations

import os
import tempfile
import threading
import unittest
from contextlib import closing

from pullwise_server.product_store import ProductStore


class ProductStoreContractsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "product.sqlite3")
        self.store = ProductStore(self.db_path)
        self.store.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_watch(self, interests: list[str]) -> dict:
        return self.store.create_watch(
            owner_id="usr_1",
            target_repository_id=None,
            upstream_repository_id="github:123",
            billing_owner_id="usr_1",
            interests=interests,
            enabled=True,
            analysis_enabled=False,
        )

    def test_watch_delete_recreate_keeps_scope_context_and_control_history(self) -> None:
        first = self.create_watch(["OAuth 登录"])
        self.store.archive_watch(first["id"], expected_revision=first["revision"])
        recreated = self.create_watch(["OAuth 登录"])

        self.assertNotEqual(first["id"], recreated["id"])
        self.assertEqual(first["watchScopeKey"], recreated["watchScopeKey"])
        self.assertEqual(recreated["contextVersion"], 1)
        self.assertEqual(recreated["contextHash"], first["contextHash"])

        with closing(self.store.connect()) as connection:
            control = connection.execute(
                "SELECT initial_backfill_state FROM processing_controls WHERE control_key = ?",
                (first["watchScopeKey"],),
            ).fetchone()
        self.assertEqual(control[0], "not_started")

    def test_processing_eligibility_is_frozen_across_restart_and_watch_recreation(self) -> None:
        watch = self.create_watch(["OAuth"])
        control_key = watch["watchScopeKey"]
        first = self.store.establish_processing_eligibility(
            control_key,
            eligible_since=1_800_000_000,
        )
        replay = ProductStore(self.db_path).establish_processing_eligibility(
            control_key,
            eligible_since=1_800_000_999,
        )
        self.store.archive_watch(watch["id"], expected_revision=watch["revision"])
        recreated = self.create_watch(["OAuth"])
        after_recreate = self.store.establish_processing_eligibility(
            recreated["watchScopeKey"],
            eligible_since=1_700_000_000,
        )

        self.assertEqual(first, {"eligibleSince": 1_800_000_000, "created": True})
        self.assertEqual(replay, {"eligibleSince": 1_800_000_000, "created": False})
        self.assertEqual(after_recreate, {"eligibleSince": 1_800_000_000, "created": False})

    def test_discovery_eligibility_uses_authoritative_time_or_fixed_backfill(self) -> None:
        watch = self.create_watch(["OAuth"])
        control_key = watch["watchScopeKey"]
        self.store.establish_processing_eligibility(control_key, eligible_since=100)
        self.store.freeze_initial_backfill(
            control_key,
            source_keys=["release-old"],
            limit=1,
        )

        self.assertFalse(
            self.store.discovery_source_eligible(
                control_key,
                source_key="release-late-page",
                authoritative_changed_at=99,
            )["eligible"]
        )
        self.assertTrue(
            self.store.discovery_source_eligible(
                control_key,
                source_key="release-at-boundary",
                authoritative_changed_at=100,
            )["eligible"]
        )
        backfill = self.store.discovery_source_eligible(
            control_key,
            source_key="release-old",
            authoritative_changed_at=1,
        )
        self.assertEqual(backfill, {"eligible": True, "reason": "initial_backfill"})

    def test_discovery_checkpoint_compare_and_swap_survives_restart(self) -> None:
        watch = self.create_watch(["OAuth"])
        control_key = watch["watchScopeKey"]
        first = self.store.advance_discovery_checkpoint(
            control_key,
            expected_cursor=None,
            expected_high_watermark=None,
            next_cursor="cursor-2",
            next_high_watermark="github-updated-at:200",
            observed_at=200,
        )

        with self.assertRaisesRegex(ValueError, "DISCOVERY_CHECKPOINT_MISMATCH"):
            self.store.advance_discovery_checkpoint(
                control_key,
                expected_cursor=None,
                expected_high_watermark=None,
                next_cursor="stale-cursor",
                next_high_watermark="github-updated-at:150",
                observed_at=201,
            )

        resumed = ProductStore(self.db_path).advance_discovery_checkpoint(
            control_key,
            expected_cursor="cursor-2",
            expected_high_watermark="github-updated-at:200",
            next_cursor=None,
            next_high_watermark="github-updated-at:250",
            observed_at=202,
        )
        self.assertEqual(
            first,
            {"cursor": "cursor-2", "highWatermark": "github-updated-at:200", "observedAt": 200},
        )
        self.assertEqual(
            resumed,
            {"cursor": None, "highWatermark": "github-updated-at:250", "observedAt": 202},
        )

    def test_watch_a_b_a_increments_context_version_without_changing_semantic_hash(self) -> None:
        first = self.create_watch(["OAuth 登录"])
        second = self.store.update_watch(
            first["id"],
            expected_revision=first["revision"],
            interests=["数据库迁移"],
        )
        third = self.store.update_watch(
            first["id"],
            expected_revision=second["revision"],
            interests=["OAuth 登录"],
        )

        self.assertEqual(second["contextVersion"], 2)
        self.assertEqual(third["contextVersion"], 3)
        self.assertEqual(third["contextHash"], first["contextHash"])

    def test_watch_revision_compare_and_swap_rejects_stale_writer(self) -> None:
        watch = self.create_watch(["OAuth"])
        current = self.store.update_watch(
            watch["id"],
            expected_revision=watch["revision"],
            enabled=False,
        )

        with self.assertRaisesRegex(ValueError, "REVISION_MISMATCH"):
            self.store.update_watch(
                watch["id"],
                expected_revision=watch["revision"],
                analysis_enabled=True,
            )
        self.assertEqual(current["revision"], 2)

    def test_item_snapshot_publication_fences_every_source_revision(self) -> None:
        item = self.store.create_item(context_id="repo:1", unit_type="pr_thread", unit_key="thread:7")
        self.store.set_source_revision("parent", 1)
        self.store.set_source_revision("reply", 1)
        first = self.store.publish_item_snapshot(
            item_id=item["id"],
            expected_item_revision=item["revision"],
            sources=[
                {"sourceId": "parent", "sourceVersion": "sv1", "sourceRevision": 1},
                {"sourceId": "reply", "sourceVersion": "sv2", "sourceRevision": 1},
            ],
            snapshot={"actionTypes": ["change_requested"]},
        )

        self.store.set_source_revision("parent", 2)
        with self.assertRaisesRegex(ValueError, "STALE_SOURCE"):
            self.store.publish_item_snapshot(
                item_id=item["id"],
                expected_item_revision=first["revision"],
                sources=[
                    {"sourceId": "parent", "sourceVersion": "sv1", "sourceRevision": 1},
                    {"sourceId": "reply", "sourceVersion": "sv2", "sourceRevision": 1},
                ],
                snapshot={"actionTypes": ["change_requested"]},
            )

    def test_item_publication_is_rejected_after_authorization_revision_changes(self) -> None:
        item = self.store.create_item(context_id="repo:1", unit_type="pr_comment", unit_key="comment:11")
        self.store.set_source_revision("comment-11", 1)
        self.store.set_source_context(
            source_id="comment-11",
            context_id="repo:1",
            context_version=1,
            configuration_revision=1,
            authorization_revision=1,
            authorization_valid_until=1_900_000_000,
            accessible=True,
        )
        self.store.set_source_context(
            source_id="comment-11",
            context_id="repo:1",
            context_version=1,
            configuration_revision=1,
            authorization_revision=2,
            authorization_valid_until=1_900_000_000,
            accessible=False,
        )

        with self.assertRaisesRegex(ValueError, "STALE_AUTHORIZATION"):
            self.store.publish_item_snapshot(
                item_id=item["id"],
                expected_item_revision=item["revision"],
                sources=[{"sourceId": "comment-11", "sourceVersion": "sv-1", "sourceRevision": 1}],
                context_fences=[{
                    "sourceId": "comment-11",
                    "contextId": "repo:1",
                    "contextVersion": 1,
                    "configurationRevision": 1,
                    "authorizationRevision": 1,
                }],
                snapshot={"actionTypes": ["reply_needed"]},
                observed_at=1_800_000_000,
            )

    def test_duplicate_source_revision_is_idempotent_but_regression_is_rejected(self) -> None:
        self.store.set_source_revision("comment", 2)
        self.store.set_source_revision("comment", 2)

        with self.assertRaisesRegex(ValueError, "SOURCE_REVISION_NOT_MONOTONIC"):
            self.store.set_source_revision("comment", 1)

    def test_item_snapshot_a_b_a_never_reuses_item_version(self) -> None:
        item = self.store.create_item(context_id="repo:1", unit_type="pr_comment", unit_key="comment:9")
        self.store.set_source_revision("comment", 1)
        first = self.store.publish_item_snapshot(
            item_id=item["id"],
            expected_item_revision=item["revision"],
            sources=[{"sourceId": "comment", "sourceVersion": "sv-a", "sourceRevision": 1}],
            snapshot={"actionTypes": ["reply_needed"]},
        )
        self.store.set_source_revision("comment", 2)
        second = self.store.publish_item_snapshot(
            item_id=item["id"],
            expected_item_revision=first["revision"],
            sources=[{"sourceId": "comment", "sourceVersion": "sv-b", "sourceRevision": 2}],
            snapshot={"actionTypes": ["change_requested"]},
        )
        self.store.set_source_revision("comment", 3)
        third = self.store.publish_item_snapshot(
            item_id=item["id"],
            expected_item_revision=second["revision"],
            sources=[{"sourceId": "comment", "sourceVersion": "sv-a", "sourceRevision": 3}],
            snapshot={"actionTypes": ["reply_needed"]},
        )

        self.assertEqual(first["itemVersion"], 1)
        self.assertEqual(second["itemVersion"], 2)
        self.assertEqual(third["itemVersion"], 3)

    def test_provider_attempt_admission_is_atomic_across_owner_and_global_budgets(self) -> None:
        barrier = threading.Barrier(2)
        results: list[str] = []

        def admit(owner_id: str) -> None:
            barrier.wait()
            try:
                self.store.admit_provider_attempt(
                    attempt_id=f"attempt-{owner_id}",
                    billing_owner_id=owner_id,
                    input_key=f"input-{owner_id}",
                    occurred_at=1_790_000_000,
                    owner_monthly_limit=10,
                    global_monthly_limit=1,
                    owner_rolling_limit=10,
                    global_rolling_limit=10,
                )
                results.append("admitted")
            except ValueError as error:
                results.append(str(error))

        threads = [threading.Thread(target=admit, args=(owner,)) for owner in ("usr_1", "usr_2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertCountEqual(results, ["admitted", "GLOBAL_MONTHLY_PROVIDER_BUDGET"])

    def test_provider_attempt_budget_survives_store_restart_and_idempotent_replay(self) -> None:
        admitted = self.store.admit_provider_attempt(
            attempt_id="attempt-1",
            billing_owner_id="usr_1",
            input_key="input-1",
            occurred_at=1_790_000_000,
            owner_monthly_limit=1,
            global_monthly_limit=10,
            owner_rolling_limit=10,
            global_rolling_limit=10,
        )
        replay = ProductStore(self.db_path).admit_provider_attempt(
            attempt_id="attempt-1",
            billing_owner_id="usr_1",
            input_key="input-1",
            occurred_at=1_790_000_001,
            owner_monthly_limit=1,
            global_monthly_limit=10,
            owner_rolling_limit=10,
            global_rolling_limit=10,
        )
        with self.assertRaisesRegex(ValueError, "OWNER_MONTHLY_PROVIDER_BUDGET"):
            ProductStore(self.db_path).admit_provider_attempt(
                attempt_id="attempt-2",
                billing_owner_id="usr_1",
                input_key="input-2",
                occurred_at=1_790_000_061,
                owner_monthly_limit=1,
                global_monthly_limit=10,
                owner_rolling_limit=10,
                global_rolling_limit=10,
            )

        self.assertFalse(admitted["reused"])
        self.assertTrue(replay["reused"])

    def test_provider_attempt_admission_requires_global_monthly_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "PRODUCTION_JEV_DISABLED"):
            self.store.admit_provider_attempt(
                attempt_id="attempt-1",
                billing_owner_id="usr_1",
                input_key="input-1",
                occurred_at=1_790_000_000,
                owner_monthly_limit=10,
                global_monthly_limit=0,
                owner_rolling_limit=6,
                global_rolling_limit=60,
            )

    def test_processing_last_slot_is_reserved_atomically(self) -> None:
        barrier = threading.Barrier(2)
        results: list[str] = []

        def reserve(charge_key: str) -> None:
            barrier.wait()
            try:
                self.store.reserve_processing_unit(
                    charge_key=charge_key,
                    billing_owner_id="usr_1",
                    period="cycle:2026-09",
                    module="pr",
                    limit=1,
                )
                results.append("reserved")
            except ValueError as error:
                results.append(str(error))

        threads = [threading.Thread(target=reserve, args=(key,)) for key in ("charge-a", "charge-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertCountEqual(results, ["reserved", "PROCESSING_QUOTA"])

    def test_processing_charge_key_is_idempotent_and_success_consumes_once(self) -> None:
        reserved = self.store.reserve_processing_unit(
            charge_key="charge-1",
            billing_owner_id="usr_1",
            period="cycle:2026-09",
            module="updates",
            limit=2,
        )
        replay = ProductStore(self.db_path).reserve_processing_unit(
            charge_key="charge-1",
            billing_owner_id="usr_1",
            period="cycle:2026-09",
            module="updates",
            limit=2,
        )
        consumed = self.store.finish_processing_unit(reserved["reservationId"], succeeded=True)
        after_success = self.store.reserve_processing_unit(
            charge_key="charge-1",
            billing_owner_id="usr_1",
            period="cycle:2026-09",
            module="updates",
            limit=2,
        )
        usage = self.store.processing_usage(billing_owner_id="usr_1", period="cycle:2026-09")

        self.assertEqual(replay["reservationId"], reserved["reservationId"])
        self.assertTrue(replay["reused"])
        self.assertEqual(consumed["state"], "consumed")
        self.assertEqual(after_success["state"], "consumed")
        self.assertTrue(after_success["reused"])
        self.assertEqual(usage["used"], 1)
        self.assertEqual(usage["reserved"], 0)
        self.assertEqual(usage["byModule"], {"pr": 0, "ci": 0, "updates": 1})

    def test_failed_processing_releases_reservation_for_same_charge_key_retry(self) -> None:
        first = self.store.reserve_processing_unit(
            charge_key="charge-1",
            billing_owner_id="usr_1",
            period="cycle:2026-09",
            module="ci",
            limit=1,
        )
        self.store.finish_processing_unit(first["reservationId"], succeeded=False)
        retry = ProductStore(self.db_path).reserve_processing_unit(
            charge_key="charge-1",
            billing_owner_id="usr_1",
            period="cycle:2026-09",
            module="ci",
            limit=1,
        )

        self.assertNotEqual(retry["reservationId"], first["reservationId"])
        self.assertFalse(retry["reused"])

    def test_handling_patch_requires_current_item_version_and_revision(self) -> None:
        item = self.store.create_item(context_id="repo:1", unit_type="pr_comment", unit_key="comment:10")
        self.store.set_source_revision("comment-10", 1)
        current = self.store.publish_item_snapshot(
            item_id=item["id"],
            expected_item_revision=item["revision"],
            sources=[{"sourceId": "comment-10", "sourceVersion": "sv-1", "sourceRevision": 1}],
            snapshot={"actionTypes": ["reply_needed"]},
        )
        handled = self.store.patch_item_handling(
            item_id=item["id"],
            item_version=current["itemVersion"],
            expected_revision=current["revision"],
            actor_id="usr_1",
            disposition="done",
        )

        self.assertEqual(handled["handling"]["disposition"], "done")
        self.assertIsNone(handled["handling"]["note"])
        with self.assertRaisesRegex(ValueError, "REVISION_MISMATCH"):
            self.store.patch_item_handling(
                item_id=item["id"],
                item_version=current["itemVersion"],
                expected_revision=current["revision"],
                actor_id="usr_1",
                disposition="dismissed",
            )

        self.store.set_source_revision("comment-10", 2)
        next_version = self.store.publish_item_snapshot(
            item_id=item["id"],
            expected_item_revision=handled["revision"],
            sources=[{"sourceId": "comment-10", "sourceVersion": "sv-2", "sourceRevision": 2}],
            snapshot={"actionTypes": ["change_requested"]},
        )
        with self.assertRaisesRegex(ValueError, "STALE_ITEM"):
            self.store.patch_item_handling(
                item_id=item["id"],
                item_version=current["itemVersion"],
                expected_revision=next_version["revision"],
                actor_id="usr_1",
                disposition="done",
            )

    def test_handling_feedback_is_append_only_and_does_not_change_disposition(self) -> None:
        item = self.store.create_item(context_id="repo:1", unit_type="ci_job", unit_key="run:1:job:2")
        self.store.set_source_revision("ci-2", 1)
        current = self.store.publish_item_snapshot(
            item_id=item["id"],
            expected_item_revision=item["revision"],
            sources=[{"sourceId": "ci-2", "sourceVersion": "sv-1", "sourceRevision": 1}],
            snapshot={"actionTypes": ["investigate_failure"]},
        )
        feedback = self.store.patch_item_handling(
            item_id=item["id"],
            item_version=current["itemVersion"],
            expected_revision=current["revision"],
            actor_id="usr_1",
            feedback="classification_inaccurate",
        )
        events = self.store.list_handling_events(item["id"])

        self.assertEqual(feedback["handling"]["disposition"], "open")
        self.assertEqual(feedback["handling"]["feedback"], "classification_inaccurate")
        self.assertEqual(events[-1]["actorId"], "usr_1")
        self.assertEqual(events[-1]["itemVersion"], current["itemVersion"])

    def test_source_content_cache_reuses_a_while_source_revision_tracks_a_b_a(self) -> None:
        first = self.store.upsert_source_snapshot(
            source_id="source-1",
            source_type="pr_comment",
            external_key="github:issue_comment:10",
            repository_id="repo-1",
            content={"text": "Please add a test."},
            source_facts={"pullNumber": 7},
            source_url="https://github.com/acme/repo/pull/7#issuecomment-10",
            processing_mode="model",
            completeness="complete",
            lifecycle="active",
            observed_at=1_800_000_000,
        )
        duplicate = self.store.upsert_source_snapshot(
            source_id="source-1",
            source_type="pr_comment",
            external_key="github:issue_comment:10",
            repository_id="repo-1",
            content={"text": "Please add a test."},
            source_facts={"pullNumber": 7},
            source_url="https://github.com/acme/repo/pull/7#issuecomment-10",
            processing_mode="model",
            completeness="complete",
            lifecycle="active",
            observed_at=1_800_000_001,
        )
        changed = self.store.upsert_source_snapshot(
            source_id="source-1",
            source_type="pr_comment",
            external_key="github:issue_comment:10",
            repository_id="repo-1",
            content={"text": "Please add two tests."},
            source_facts={"pullNumber": 7},
            source_url="https://github.com/acme/repo/pull/7#issuecomment-10",
            processing_mode="model",
            completeness="complete",
            lifecycle="active",
            observed_at=1_800_000_002,
        )
        returned = self.store.upsert_source_snapshot(
            source_id="source-1",
            source_type="pr_comment",
            external_key="github:issue_comment:10",
            repository_id="repo-1",
            content={"text": "Please add a test."},
            source_facts={"pullNumber": 7},
            source_url="https://github.com/acme/repo/pull/7#issuecomment-10",
            processing_mode="model",
            completeness="complete",
            lifecycle="active",
            observed_at=1_800_000_003,
        )

        self.assertEqual(duplicate["sourceRevision"], 1)
        self.assertEqual(duplicate["sourceVersion"], first["sourceVersion"])
        self.assertEqual(changed["sourceRevision"], 2)
        self.assertEqual(returned["sourceRevision"], 3)
        self.assertEqual(returned["sourceVersion"], first["sourceVersion"])

    def test_permission_revocation_removes_source_and_item_from_authorized_reads(self) -> None:
        source = self.store.upsert_source_snapshot(
            source_id="release-1",
            source_type="release",
            external_key="github:release:1",
            repository_id="upstream-1",
            content={"body": "OAuth migration required."},
            source_facts={"releaseId": 1},
            source_url="https://github.com/acme/upstream/releases/tag/v2",
            processing_mode="model",
            completeness="partial",
            lifecycle="active",
            observed_at=1_800_000_000,
        )
        self.store.set_source_context(
            source_id=source["id"],
            context_id="watch-scope-1",
            context_version=1,
            configuration_revision=1,
            authorization_revision=1,
            authorization_valid_until=1_900_000_000,
            accessible=True,
            billing_owner_id="usr_1",
            processing_status="assessed",
            analysis_enabled=True,
            context_stale=False,
            coverage={
                "state": "partial",
                "totalUnits": 10,
                "selectedUnits": 8,
                "omittedUnits": 2,
                "rawSourcePartial": False,
                "selectionRuleVersion": "updates-units/v1",
                "limitations": ["unit_limit"],
            },
        )
        item = self.store.create_item(
            context_id="watch-scope-1",
            unit_type="update_release",
            unit_key="release:1",
        )
        published = self.store.publish_item_snapshot(
            item_id=item["id"],
            expected_item_revision=item["revision"],
            sources=[{
                "sourceId": source["id"],
                "sourceVersion": source["sourceVersion"],
                "sourceRevision": source["sourceRevision"],
            }],
            context_fences=[{
                "sourceId": source["id"],
                "contextId": "watch-scope-1",
                "contextVersion": 1,
                "configurationRevision": 1,
                "authorizationRevision": 1,
            }],
            snapshot={
                "module": "updates",
                "title": "Review v2 update",
                "actionTypes": ["review_update"],
                "attentionState": "needs_action",
                "lifecycle": "active",
                "evidence": [],
            },
            observed_at=1_800_000_000,
        )
        self.assertEqual(published["itemVersion"], 1)
        self.assertEqual(len(self.store.list_sources_for_billing_owner("usr_1")), 1)
        self.assertEqual(len(self.store.list_items_for_billing_owner("usr_1")), 1)

        self.store.set_source_context(
            source_id=source["id"],
            context_id="watch-scope-1",
            context_version=1,
            configuration_revision=1,
            authorization_revision=2,
            authorization_valid_until=1_900_000_000,
            accessible=False,
            billing_owner_id="usr_1",
            processing_status="pending",
            analysis_enabled=True,
            context_stale=True,
            coverage={"state": "unavailable", "selectedUnits": 0, "rawSourcePartial": False, "limitations": ["permission_revoked"]},
        )

        self.assertEqual(self.store.list_sources_for_billing_owner("usr_1"), [])
        self.assertEqual(self.store.list_items_for_billing_owner("usr_1"), [])

    def test_concurrent_multi_source_publication_has_one_cas_winner(self) -> None:
        item = self.store.create_item(context_id="repo:1", unit_type="pr_thread", unit_key="thread:concurrent")
        self.store.set_source_revision("parent-concurrent", 1)
        self.store.set_source_revision("reply-concurrent", 1)
        barrier = threading.Barrier(2)
        results: list[str] = []

        def publish(action: str) -> None:
            barrier.wait()
            try:
                self.store.publish_item_snapshot(
                    item_id=item["id"],
                    expected_item_revision=item["revision"],
                    sources=[
                        {"sourceId": "parent-concurrent", "sourceVersion": "sv-parent", "sourceRevision": 1},
                        {"sourceId": "reply-concurrent", "sourceVersion": "sv-reply", "sourceRevision": 1},
                    ],
                    snapshot={"actionTypes": [action]},
                )
                results.append("published")
            except ValueError as error:
                results.append(str(error))

        threads = [
            threading.Thread(target=publish, args=("reply_needed",)),
            threading.Thread(target=publish, args=("change_requested",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertCountEqual(results, ["published", "REVISION_MISMATCH"])

    def test_context_refresh_stability_and_cooldown_survive_restart(self) -> None:
        watch = self.create_watch(["OAuth"])
        control_key = watch["watchScopeKey"]
        self.store.note_context_configuration(control_key, changed_at=1_800_000_000)
        self.store.note_context_configuration(control_key, changed_at=1_800_000_100)

        early = self.store.admit_context_refresh(control_key, timestamp=1_800_000_699)
        allowed = ProductStore(self.db_path).admit_context_refresh(
            control_key,
            timestamp=1_800_000_700,
        )
        cooldown = ProductStore(self.db_path).admit_context_refresh(
            control_key,
            timestamp=1_800_004_299,
        )
        next_hour = ProductStore(self.db_path).admit_context_refresh(
            control_key,
            timestamp=1_800_004_300,
        )

        self.assertFalse(early["allowed"])
        self.assertEqual(early["nextEligibleAt"], 1_800_000_700)
        self.assertTrue(allowed["allowed"])
        self.assertFalse(cooldown["allowed"])
        self.assertEqual(cooldown["nextEligibleAt"], 1_800_004_300)
        self.assertTrue(next_hour["allowed"])

    def test_request_idempotency_is_shared_across_session_and_keys_and_survives_restart(self) -> None:
        first = self.store.begin_idempotent_request(
            subject_id="usr_1",
            method="POST",
            path="/api/v1/watches",
            idempotency_key="create-watch-1",
            body_hash="body-a",
            timestamp=1_800_000_000,
        )
        self.store.complete_idempotent_request(
            subject_id="usr_1",
            method="POST",
            path="/api/v1/watches",
            idempotency_key="create-watch-1",
            body_hash="body-a",
            status_code=201,
            response={"id": "watch-1"},
            timestamp=1_800_000_001,
        )
        replay = ProductStore(self.db_path).begin_idempotent_request(
            subject_id="usr_1",
            method="POST",
            path="/api/v1/watches",
            idempotency_key="create-watch-1",
            body_hash="body-a",
            timestamp=1_800_000_100,
        )

        self.assertEqual(first["state"], "reserved")
        self.assertEqual(replay, {"state": "replay", "statusCode": 201, "response": {"id": "watch-1"}})
        with self.assertRaisesRegex(ValueError, "IDEMPOTENCY_CONFLICT"):
            ProductStore(self.db_path).begin_idempotent_request(
                subject_id="usr_1",
                method="POST",
                path="/api/v1/watches",
                idempotency_key="create-watch-1",
                body_hash="body-b",
                timestamp=1_800_000_100,
            )

    def test_expired_idempotency_record_can_be_reused(self) -> None:
        self.store.begin_idempotent_request(
            subject_id="usr_1",
            method="POST",
            path="/api/v1/watches",
            idempotency_key="expired",
            body_hash="body-a",
            timestamp=1_800_000_000,
            ttl_seconds=10,
        )

        fresh = ProductStore(self.db_path).begin_idempotent_request(
            subject_id="usr_1",
            method="POST",
            path="/api/v1/watches",
            idempotency_key="expired",
            body_hash="body-b",
            timestamp=1_800_000_011,
            ttl_seconds=10,
        )

        self.assertEqual(fresh["state"], "reserved")

    def test_repository_service_uses_stable_repo_identity_and_revision_cas(self) -> None:
        created = self.store.put_repository_service(
            repository_id="repo-1",
            installation_id="installation-1",
            billing_owner_id="usr_1",
            expected_revision=0,
            enabled=True,
            modules={"pr": True, "ci": False},
            analysis_enabled={"pr": False, "ci": False},
            allow_member_sync=False,
            default_assignee_id=None,
            priority_order=0,
        )
        updated = self.store.put_repository_service(
            repository_id="repo-1",
            installation_id="installation-1",
            billing_owner_id="usr_1",
            expected_revision=created["revision"],
            enabled=True,
            modules={"pr": True, "ci": True},
            analysis_enabled={"pr": True, "ci": False},
            allow_member_sync=True,
            default_assignee_id="usr_1",
            priority_order=2,
        )

        self.assertEqual(created["revision"], 1)
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["modules"], {"pr": True, "ci": True})
        self.assertEqual(updated["analysisEnabled"], {"pr": True, "ci": False})
        self.assertEqual(self.store.count_active_repository_services("usr_1"), 1)
        with self.assertRaisesRegex(ValueError, "REVISION_MISMATCH"):
            self.store.put_repository_service(
                repository_id="repo-1",
                installation_id="installation-1",
                billing_owner_id="usr_1",
                expected_revision=created["revision"],
                enabled=False,
                modules={"pr": True, "ci": True},
                analysis_enabled={"pr": True, "ci": False},
                allow_member_sync=True,
                default_assignee_id="usr_1",
                priority_order=2,
            )

    def test_repository_service_last_capacity_slot_is_atomic(self) -> None:
        barrier = threading.Barrier(2)
        results: list[str] = []

        def enable(repository_id: str) -> None:
            barrier.wait()
            try:
                self.store.put_repository_service(
                    repository_id=repository_id,
                    installation_id=f"installation-{repository_id}",
                    billing_owner_id="usr_1",
                    expected_revision=0,
                    enabled=True,
                    modules={"pr": True, "ci": False},
                    analysis_enabled={"pr": False, "ci": False},
                    allow_member_sync=False,
                    default_assignee_id=None,
                    priority_order=0,
                    active_limit=1,
                )
                results.append("enabled")
            except ValueError as error:
                results.append(str(error))

        threads = [threading.Thread(target=enable, args=(repository_id,)) for repository_id in ("repo-a", "repo-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertCountEqual(results, ["enabled", "REPOSITORY_LIMIT_REACHED"])

    def test_watch_last_capacity_slot_is_atomic(self) -> None:
        barrier = threading.Barrier(2)
        results: list[str] = []

        def create(upstream_id: str) -> None:
            barrier.wait()
            try:
                self.store.create_watch(
                    owner_id="usr_1",
                    target_repository_id=None,
                    upstream_repository_id=upstream_id,
                    billing_owner_id="usr_1",
                    interests=["OAuth"],
                    enabled=True,
                    analysis_enabled=False,
                    active_limit=1,
                )
                results.append("enabled")
            except ValueError as error:
                results.append(str(error))

        threads = [threading.Thread(target=create, args=(upstream_id,)) for upstream_id in ("github:1", "github:2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertCountEqual(results, ["enabled", "WATCH_LIMIT_REACHED"])

    def test_assessment_cache_uses_context_hash_not_audit_version_and_excludes_discarded(self) -> None:
        assessment = self.store.store_assessment(
            billing_owner_id="usr_1",
            source_version_id="sv-a",
            context_hash="ctx-a",
            evaluated_context_version=1,
            question_version="updates-filter/v3",
            extractor_version="updates-units/v1",
            model="jev-1.13.0",
            input_hash="input-a",
            dependencies=[{"sourceId": "release-1", "sourceVersion": "sv-a"}],
            answers={"u0_relevance": {"choice": "relevant"}},
            usage={"inputTokens": 100, "outputTokens": 10},
            status="succeeded",
        )
        reused = ProductStore(self.db_path).find_reusable_assessment(
            billing_owner_id="usr_1",
            source_version_id="sv-a",
            context_hash="ctx-a",
            question_version="updates-filter/v3",
            extractor_version="updates-units/v1",
            model="jev-1.13.0",
            input_hash="input-a",
            publish_context_version=3,
        )
        different_context = self.store.find_reusable_assessment(
            billing_owner_id="usr_1",
            source_version_id="sv-a",
            context_hash="ctx-b",
            question_version="updates-filter/v3",
            extractor_version="updates-units/v1",
            model="jev-1.13.0",
            input_hash="input-a",
            publish_context_version=2,
        )
        self.store.store_assessment(
            billing_owner_id="usr_1",
            source_version_id="sv-discarded",
            context_hash="ctx-a",
            evaluated_context_version=1,
            question_version="updates-filter/v3",
            extractor_version="updates-units/v1",
            model="jev-1.13.0",
            input_hash="input-discarded",
            dependencies=[],
            answers={"u0_relevance": {"choice": "unclear"}},
            usage={"inputTokens": 100, "outputTokens": 10},
            status="discarded",
        )
        discarded = self.store.find_reusable_assessment(
            billing_owner_id="usr_1",
            source_version_id="sv-discarded",
            context_hash="ctx-a",
            question_version="updates-filter/v3",
            extractor_version="updates-units/v1",
            model="jev-1.13.0",
            input_hash="input-discarded",
            publish_context_version=2,
        )

        self.assertEqual(reused["id"], assessment["id"])
        self.assertEqual(reused["publishContextVersion"], 3)
        self.assertEqual(reused["evaluatedContextVersion"], 1)
        self.assertIsNone(different_context)
        self.assertIsNone(discarded)

    def test_initial_backfill_fixed_set_survives_restart_and_watch_recreation(self) -> None:
        watch = self.create_watch(["OAuth"])
        control_key = watch["watchScopeKey"]
        selected = self.store.freeze_initial_backfill(
            control_key,
            source_keys=["release-3", "release-2", "release-1"],
            limit=1,
        )
        self.store.archive_watch(watch["id"], expected_revision=watch["revision"])
        recreated = self.create_watch(["OAuth"])
        replay = ProductStore(self.db_path).freeze_initial_backfill(
            recreated["watchScopeKey"],
            source_keys=["release-9", "release-8", "release-7"],
            limit=1,
        )
        progress = ProductStore(self.db_path).complete_initial_backfill_source(
            control_key,
            source_key="release-3",
        )

        self.assertEqual(selected["sourceKeys"], ["release-3"])
        self.assertEqual(replay["sourceKeys"], ["release-3"])
        self.assertEqual(progress["state"], "completed")
        self.assertEqual(progress["sourceKeys"], ["release-3"])

    def test_assessment_item_and_processing_usage_publish_atomically(self) -> None:
        source = self.store.upsert_source_snapshot(
            source_id="release-atomic",
            source_type="release",
            external_key="github:release:atomic",
            repository_id="upstream-1",
            content={"body": "OAuth migration required."},
            source_facts={"releaseId": "atomic"},
            source_url="https://github.com/acme/upstream/releases/tag/atomic",
            processing_mode="model",
            completeness="complete",
            lifecycle="active",
            observed_at=1_800_000_000,
        )
        self.store.set_source_context(
            source_id=source["id"],
            context_id="watch:atomic",
            context_version=1,
            configuration_revision=1,
            authorization_revision=1,
            authorization_valid_until=1_900_000_000,
            accessible=True,
            billing_owner_id="usr_1",
            processing_status="processing",
            analysis_enabled=True,
            coverage={"state": "complete", "selectedUnits": 1, "rawSourcePartial": False, "limitations": []},
        )
        item = self.store.create_item(
            context_id="watch:atomic",
            unit_type="update_release",
            unit_key="release:atomic",
        )
        reservation = self.store.reserve_processing_unit(
            charge_key="charge-atomic",
            billing_owner_id="usr_1",
            period="cycle:1800000000",
            module="updates",
            limit=10,
        )

        result = self.store.publish_assessment_result(
            reservation_id=reservation["reservationId"],
            assessment={
                "billingOwnerId": "usr_1",
                "sourceVersionId": source["sourceVersion"],
                "contextHash": "ctx-a",
                "evaluatedContextVersion": 1,
                "questionVersion": "updates-filter/v3",
                "extractorVersion": "updates-units/v1",
                "model": "jev-1.13.0",
                "inputHash": "input-atomic",
                "dependencies": [{"sourceId": source["id"], "sourceVersion": source["sourceVersion"]}],
                "bindings": {
                    "u0_relevance": {
                        "sourceId": source["id"],
                        "sourceVersion": source["sourceVersion"],
                        "changeUnitId": "u0",
                        "evidenceIds": ["ev-atomic"],
                        "contextVersion": 1,
                    }
                },
                "answers": {"u0_relevance": {"choice": "relevant"}},
                "usage": {"inputTokens": 100, "outputTokens": 0},
            },
            item_id=item["id"],
            expected_item_revision=item["revision"],
            sources=[{
                "sourceId": source["id"],
                "sourceVersion": source["sourceVersion"],
                "sourceRevision": source["sourceRevision"],
            }],
            context_fences=[{
                "sourceId": source["id"],
                "contextId": "watch:atomic",
                "contextVersion": 1,
                "configurationRevision": 1,
                "authorizationRevision": 1,
            }],
            snapshot={
                "module": "updates",
                "actionTypes": ["review_update"],
                "attentionState": "needs_action",
                "lifecycle": "active",
                "evidence": [{
                    "id": "ev-atomic",
                    "sourceId": source["id"],
                    "sourceVersion": source["sourceVersion"],
                    "status": "available",
                    "text": "OAuth migration required.",
                    "segmentAnchor": "u0",
                    "actionTypes": ["review_update"],
                }],
            },
            observed_at=1_800_000_001,
        )

        usage = self.store.processing_usage(billing_owner_id="usr_1", period="cycle:1800000000")
        self.assertEqual(result["item"]["itemVersion"], 1)
        self.assertEqual(result["assessment"]["status"], "succeeded")
        self.assertEqual(usage["used"], 1)
        self.assertEqual(usage["reserved"], 0)
        published = self.store.list_items_for_billing_owner("usr_1")[0]["assessments"][0]
        self.assertEqual(published["sourceId"], source["id"])
        self.assertEqual(published["sourceVersion"], source["sourceVersion"])
        self.assertEqual(published["evidenceIds"], ["ev-atomic"])
        self.assertEqual(published["bindings"]["u0_relevance"]["changeUnitId"], "u0")
        self.assertEqual(published["publishContextVersion"], 1)

    def test_stale_atomic_publication_rolls_back_assessment_item_and_reservation(self) -> None:
        source = self.store.upsert_source_snapshot(
            source_id="release-stale-atomic",
            source_type="release",
            external_key="github:release:stale-atomic",
            repository_id="upstream-1",
            content={"body": "Old."},
            source_facts={"releaseId": "stale-atomic"},
            source_url="https://github.com/acme/upstream/releases/tag/stale",
            processing_mode="model",
            completeness="complete",
            lifecycle="active",
            observed_at=1_800_000_000,
        )
        self.store.set_source_context(
            source_id=source["id"],
            context_id="watch:stale",
            context_version=1,
            configuration_revision=1,
            authorization_revision=1,
            authorization_valid_until=1_900_000_000,
            accessible=True,
            billing_owner_id="usr_1",
        )
        item = self.store.create_item(context_id="watch:stale", unit_type="update_release", unit_key="release:stale")
        reservation = self.store.reserve_processing_unit(
            charge_key="charge-stale-atomic",
            billing_owner_id="usr_1",
            period="cycle:1800000000",
            module="updates",
            limit=10,
        )
        self.store.set_source_revision(source["id"], 2)

        with self.assertRaisesRegex(ValueError, "STALE_SOURCE"):
            self.store.publish_assessment_result(
                reservation_id=reservation["reservationId"],
                assessment={
                    "billingOwnerId": "usr_1",
                    "sourceVersionId": source["sourceVersion"],
                    "contextHash": "ctx-a",
                    "evaluatedContextVersion": 1,
                    "questionVersion": "updates-filter/v3",
                    "extractorVersion": "updates-units/v1",
                    "model": "jev-1.13.0",
                    "inputHash": "input-stale",
                    "dependencies": [],
                    "bindings": {},
                    "answers": {},
                    "usage": {"inputTokens": 10, "outputTokens": 0},
                },
                item_id=item["id"],
                expected_item_revision=item["revision"],
                sources=[{
                    "sourceId": source["id"],
                    "sourceVersion": source["sourceVersion"],
                    "sourceRevision": 1,
                }],
                context_fences=[{
                    "sourceId": source["id"],
                    "contextId": "watch:stale",
                    "contextVersion": 1,
                    "configurationRevision": 1,
                    "authorizationRevision": 1,
                }],
                snapshot={"module": "updates", "actionTypes": ["review_update"]},
                observed_at=1_800_000_001,
            )

        usage = self.store.processing_usage(billing_owner_id="usr_1", period="cycle:1800000000")
        with closing(self.store.connect()) as connection:
            assessment_count = connection.execute("SELECT COUNT(*) FROM assessments").fetchone()[0]
            version_count = connection.execute("SELECT COUNT(*) FROM item_versions").fetchone()[0]
        self.assertEqual(assessment_count, 0)
        self.assertEqual(version_count, 0)
        self.assertEqual(usage["used"], 0)
        self.assertEqual(usage["reserved"], 1)

    def test_cached_assessment_rebinds_current_evidence_without_reviving_handling(self) -> None:
        source = self.store.upsert_source_snapshot(
            source_id="release-rebind",
            source_type="release",
            external_key="github:release:rebind",
            repository_id="upstream-1",
            content={"body": "OAuth migration required."},
            source_facts={"releaseId": "rebind"},
            source_url="https://github.com/acme/upstream/releases/tag/rebind",
            processing_mode="model",
            completeness="complete",
            lifecycle="active",
            observed_at=1_800_000_000,
        )
        item = self.store.create_item(
            context_id="watch:rebind",
            unit_type="update_release",
            unit_key="release:rebind",
        )
        self.store.set_source_context(
            source_id=source["id"],
            context_id="watch:rebind",
            context_version=1,
            configuration_revision=1,
            authorization_revision=1,
            authorization_valid_until=1_900_000_000,
            accessible=True,
            billing_owner_id="usr_1",
            item_id=item["id"],
            processing_status="processing",
            analysis_enabled=True,
        )
        semantic = {
            "billingOwnerId": "usr_1",
            "sourceVersionId": source["sourceVersion"],
            "contextHash": "ctx-rebind-a",
            "evaluatedContextVersion": 1,
            "questionVersion": "updates-filter/v3",
            "extractorVersion": "updates-units/v1",
            "model": "jev-1.13.0",
            "inputHash": "input-rebind",
            "dependencies": [{"sourceId": source["id"], "sourceVersion": source["sourceVersion"]}],
            "answers": {"u0_relevance": {"choice": "relevant"}},
            "usage": {"inputTokens": 100, "outputTokens": 0},
        }
        first_reservation = self.store.reserve_processing_unit(
            charge_key="charge-rebind-1",
            billing_owner_id="usr_1",
            period="cycle:1800000000",
            module="updates",
            limit=10,
        )
        first = self.store.publish_assessment_result(
            reservation_id=first_reservation["reservationId"],
            assessment={
                **semantic,
                "bindings": {
                    "u0_relevance": {
                        "sourceId": source["id"],
                        "sourceVersion": source["sourceVersion"],
                        "changeUnitId": "u0",
                        "evidenceIds": ["ev-old"],
                        "contextVersion": 1,
                    }
                },
            },
            item_id=item["id"],
            expected_item_revision=item["revision"],
            sources=[{
                "sourceId": source["id"],
                "sourceVersion": source["sourceVersion"],
                "sourceRevision": source["sourceRevision"],
            }],
            context_fences=[{
                "sourceId": source["id"],
                "contextId": "watch:rebind",
                "contextVersion": 1,
                "configurationRevision": 1,
                "authorizationRevision": 1,
            }],
            snapshot={
                "module": "updates",
                "actionTypes": ["review_update"],
                "evidence": [{"id": "ev-old"}],
            },
            observed_at=1_800_000_001,
        )
        handled = self.store.patch_item_handling(
            item_id=item["id"],
            item_version=first["item"]["itemVersion"],
            expected_revision=first["item"]["revision"],
            actor_id="usr_1",
            disposition="done",
        )
        self.store.set_source_context(
            source_id=source["id"],
            context_id="watch:rebind",
            context_version=2,
            configuration_revision=2,
            authorization_revision=1,
            authorization_valid_until=1_900_000_000,
            accessible=True,
            billing_owner_id="usr_1",
            item_id=item["id"],
            processing_status="processing",
            analysis_enabled=True,
        )
        second_reservation = self.store.reserve_processing_unit(
            charge_key="charge-rebind-2",
            billing_owner_id="usr_1",
            period="cycle:1800000000",
            module="updates",
            limit=10,
        )
        second = self.store.publish_assessment_result(
            reservation_id=second_reservation["reservationId"],
            assessment={
                **semantic,
                "bindings": {
                    "u0_relevance": {
                        "sourceId": source["id"],
                        "sourceVersion": source["sourceVersion"],
                        "changeUnitId": "u0",
                        "evidenceIds": ["ev-current"],
                        "contextVersion": 2,
                    }
                },
            },
            item_id=item["id"],
            expected_item_revision=handled["revision"],
            sources=[{
                "sourceId": source["id"],
                "sourceVersion": source["sourceVersion"],
                "sourceRevision": source["sourceRevision"],
            }],
            context_fences=[{
                "sourceId": source["id"],
                "contextId": "watch:rebind",
                "contextVersion": 2,
                "configurationRevision": 2,
                "authorizationRevision": 1,
            }],
            snapshot={
                "module": "updates",
                "actionTypes": ["review_update"],
                "evidence": [{"id": "ev-current"}],
            },
            observed_at=1_800_000_002,
        )

        current = self.store.list_items_for_billing_owner("usr_1")[0]
        rebound = current["assessments"][0]
        self.assertEqual(second["assessment"]["id"], first["assessment"]["id"])
        self.assertEqual(rebound["evidenceIds"], ["ev-current"])
        self.assertEqual(rebound["bindings"]["u0_relevance"]["contextVersion"], 2)
        self.assertEqual(rebound["publishContextVersion"], 2)
        self.assertEqual(current["handling"]["disposition"], "open")
        self.assertEqual(self.store.list_handling_events(item["id"])[-1]["eventKind"], "assessment_rebound")


if __name__ == "__main__":
    unittest.main()
