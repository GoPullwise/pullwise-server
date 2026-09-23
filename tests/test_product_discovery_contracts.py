from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from pullwise_server.github_sources import release_source
from pullwise_server.product_store import ProductStore


class ProductDiscoveryContractsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "discovery.sqlite3"
        self.store = ProductStore(self.path)
        self.store.initialize()
        self.now = 1_800_000_000
        self.clock = patch("pullwise_server.product_store._now", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.watch = self.create_watch()
        self.target = self.authorize()
        from pullwise_server.product_discovery import ProductFactSync
        self.reader = Mock(return_value=self.page([]))
        self.sync = ProductFactSync(self.store, read_page=self.reader,
                                   processing_budget=lambda owner, now: ("cycle:fixture", 100),
                                   app_id="7", webhook_secret="local-fixture-secret")

    def create_watch(self):
        return self.store.create_watch(owner_id="usr_1", target_repository_id=None,
            upstream_repository_id="github:123", billing_owner_id="usr_1", interests=["OAuth"],
            enabled=True, analysis_enabled=True)

    def configure_permission_refresh(self, callback=None):
        from pullwise_server.product_discovery import DiscoveryAuthorizationProof, ProductFactSync
        self.refresher = Mock(side_effect=callback or (lambda **kwargs: DiscoveryAuthorizationProof(
            accessible=True, observed_at=self.now, valid_until=self.now + 300)))
        self.sync = ProductFactSync(self.store, read_page=self.reader,
            processing_budget=lambda owner, now: ("cycle:fixture", 100),
            app_id="7", webhook_secret="local-fixture-secret",
            refresh_authorization=self.refresher)

    def test_permissions_renew_independently_of_six_hour_fact_schedule(self):
        self.poll([self.source()])
        before = self.store.discovery_target(self.target)
        self.configure_permission_refresh()
        self.now += 241
        self.assertEqual(self.sync.run_authorization_due(now=self.now), [{"status": "renewed"}])
        after = self.store.discovery_target(self.target)
        self.assertEqual(after["authorization_revision"], before["authorization_revision"])
        self.assertEqual(after["analysis_authorized_at"], before["analysis_authorized_at"])
        self.assertEqual(after["next_scheduled_at"], before["next_scheduled_at"])
        self.assertEqual(after["valid_until"], self.now + 300)
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.status(self.source()), "pending")
        self.assertEqual(self.sync.run_due(now=self.now), [])

    def test_expired_permission_refresh_does_not_discover_or_establish_eligibility(self):
        self.configure_permission_refresh()
        self.now += 301
        self.assertEqual(self.sync.run_authorization_due(now=self.now), [{"status": "renewed"}])
        self.reader.assert_not_called()
        self.assertEqual(self.counts(), (0, 0))
        with closing(self.store.connect()) as connection:
            row = connection.execute("SELECT * FROM processing_controls WHERE control_key=?", (self.target,)).fetchone()
        self.assertIsNone(row["eligible_since"])
        self.assertIsNone(row["discovery_cursor"])
        self.assertEqual(row["initial_backfill_state"], "not_started")

    def test_permission_denial_cancels_work_and_hides_sources(self):
        self.poll([self.source()])
        from pullwise_server.product_discovery import DiscoveryAuthorizationProof
        self.configure_permission_refresh(lambda **kwargs: DiscoveryAuthorizationProof(
            accessible=False, observed_at=self.now, valid_until=self.now))
        self.now += 241
        self.assertEqual(self.sync.run_authorization_due(now=self.now), [{"status": "denied"}])
        self.assertFalse(self.store.discovery_target(self.target)["accessible"])
        self.assertEqual(self.store.discovery_target(self.target)["authorization_revision"], 2)
        self.assertEqual(self.store.list_sources_for_billing_owner("usr_1"), [])
        self.assertEqual(self.counts(), (1, 0))
        self.assertEqual(self.store.list_jobs()[0]["status"], "cancelled")

    def test_late_permission_success_cannot_undo_signed_revocation(self):
        from pullwise_server.product_discovery import DiscoveryAuthorizationProof
        def callback(**kwargs):
            # A separate SQLite writer must remain usable during the network read.
            ProductStore(self.path).revoke_discovery_installation(
                app_id="7", installation_id="11", repository_ids=None, observed_at=self.now)
            return DiscoveryAuthorizationProof(accessible=True, observed_at=self.now, valid_until=self.now + 300)
        self.configure_permission_refresh(callback)
        self.now += 241
        self.assertEqual(self.sync.run_authorization_due(now=self.now), [{"status": "superseded"}])
        self.assertFalse(self.store.discovery_target(self.target)["accessible"])

    def test_permission_refresh_failure_backoff_survives_restart(self):
        self.configure_permission_refresh(lambda **kwargs: (_ for _ in ()).throw(RuntimeError("private token")))
        self.now += 241
        with self.assertLogs("pullwise_server.product_discovery", level="WARNING") as logs:
            self.assertEqual(self.sync.run_authorization_due(now=self.now), [{"status": "unavailable"}])
        self.assertNotIn("private token", " ".join(logs.output))
        self.store = ProductStore(self.path)
        self.store.initialize()
        self.sync.store = self.store
        self.assertEqual(self.sync.run_authorization_due(now=self.now + 59), [])
        self.refresher.assert_called_once()
        self.assertEqual(self.sync.run_manual(self.target, now=self.now + 60)["status"], "unauthorized")

    def test_permission_refresh_respects_github_retry_after_across_restart(self):
        from pullwise_server.github_transport import GitHubUnavailable
        self.now += 241
        retry_at = self.now + 600
        self.configure_permission_refresh(lambda **kwargs: (_ for _ in ()).throw(
            GitHubUnavailable(retry_at=retry_at)))
        with self.assertLogs("pullwise_server.product_discovery", level="WARNING"):
            self.assertEqual(self.sync.run_authorization_due(now=self.now), [{"status": "unavailable"}])
        self.sync.store = ProductStore(self.path)
        self.now = retry_at - 1
        self.assertEqual(self.sync.run_authorization_due(now=self.now), [])
        self.refresher.assert_called_once()

    def test_fact_rate_limit_blocks_manual_and_scheduled_reads_across_restart(self):
        from pullwise_server.github_transport import GitHubUnavailable
        retry_at = self.now + 180
        self.reader.side_effect = GitHubUnavailable(retry_at=retry_at)
        with self.assertRaises(GitHubUnavailable):
            self.sync.run_manual(self.target, now=self.now)
        self.sync.store = ProductStore(self.path)
        self.now = retry_at - 1
        self.assertEqual(self.sync.run_manual(self.target, now=self.now)["status"], "throttled")
        self.assertEqual(self.sync.run_scheduled(self.target, now=self.now)["status"], "throttled")
        self.reader.assert_called_once()
        self.reader.side_effect = None
        self.now = retry_at
        self.assertEqual(self.sync.run_manual(self.target, now=self.now)["status"], "completed")
        self.assertEqual(self.reader.call_count, 2)
        self.assertEqual(self.counts(), (0, 0))

    def test_due_reader_failure_does_not_block_later_repository(self):
        watch = self.store.create_watch(owner_id="usr_2", target_repository_id=None,
            upstream_repository_id="github:124", billing_owner_id="usr_2", interests=["API"],
            enabled=True, analysis_enabled=False)
        self.store.set_discovery_authorization(resource_kind="watch", resource_id=watch["id"],
            module="updates", github_repository_id="124", installation_id=None, app_id=None,
            authorization_revision=1, accessible=True, valid_until=self.now + 300, observed_at=self.now)
        from pullwise_server.github_transport import GitHubUnavailable
        def read_page(*, target, **kwargs):
            if target["control_key"] == self.target:
                raise GitHubUnavailable()
            return self.page([])
        self.reader.side_effect = read_page
        self.assertCountEqual(self.sync.run_due(now=self.now),
                              [{"status": "unavailable"}, {"status": "completed", "sources": 0}])

    def test_event_retry_after_is_durable_and_preserves_pending_receipt(self):
        from pullwise_server.github_transport import GitHubUnavailable
        self.event()
        self.now += 30
        retry_at = self.now + 180
        self.reader.side_effect = GitHubUnavailable(retry_at=retry_at)
        self.assertEqual(self.sync.run_events(now=self.now), [{"status": "unavailable"}])
        self.sync.store = ProductStore(self.path)
        self.now = retry_at - 1
        self.assertEqual(self.sync.run_events(now=self.now), [])
        self.reader.assert_called_once()
        with closing(self.store.connect()) as connection:
            receipt = connection.execute("SELECT state,ready_at FROM github_delivery_targets").fetchone()
        self.assertEqual(tuple(receipt), ("pending", retry_at))

    def test_permission_refresh_claim_prevents_overlapping_checks(self):
        from pullwise_server.product_discovery import DiscoveryAuthorizationProof, ProductFactSync
        def callback(**kwargs):
            other = ProductFactSync(ProductStore(self.path), read_page=self.reader,
                processing_budget=Mock(), app_id="7", webhook_secret="fixture",
                refresh_authorization=Mock(side_effect=AssertionError("overlapping refresh")))
            self.assertEqual(other.run_authorization_due(now=self.now), [])
            return DiscoveryAuthorizationProof(accessible=True, observed_at=self.now, valid_until=self.now + 300)
        self.configure_permission_refresh(callback)
        self.now += 241
        self.assertEqual(self.sync.run_authorization_due(now=self.now), [{"status": "renewed"}])

    def test_concurrent_permission_workers_claim_one_network_check(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from pullwise_server.product_discovery import DiscoveryAuthorizationProof, ProductFactSync
        entered, release = threading.Event(), threading.Event()
        def callback(**kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test worker was not released")
            return DiscoveryAuthorizationProof(accessible=True, observed_at=self.now, valid_until=self.now + 300)
        self.configure_permission_refresh(callback)
        self.now += 241
        other = ProductFactSync(ProductStore(self.path), read_page=self.reader,
            processing_budget=Mock(), app_id="7", webhook_secret="fixture",
            refresh_authorization=self.refresher)
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(self.sync.run_authorization_due, now=self.now)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(workers.submit(other.run_authorization_due, now=self.now).result(5), [])
            finally:
                release.set()
            self.assertEqual(first.result(5), [{"status": "renewed"}])
        self.refresher.assert_called_once()

    def test_invalid_permission_proofs_do_not_extend_authorization(self):
        from pullwise_server.product_discovery import DiscoveryAuthorizationProof
        self.configure_permission_refresh()
        self.now += 301
        for accessible, observed_offset, expiry_offset in (
            (1, 0, 300), (True, 1, 300), (True, -1, 299),
            (True, 0, 301), (True, 0, 0),
        ):
            options = {"accessible": accessible, "observed_at": self.now + observed_offset,
                       "valid_until": self.now + expiry_offset}
            self.refresher.side_effect = None
            self.refresher.return_value = DiscoveryAuthorizationProof(**options)
            with self.subTest(options=options), self.assertLogs("pullwise_server.product_discovery", level="WARNING"):
                self.assertEqual(self.sync.run_authorization_due(now=self.now), [{"status": "unavailable"}])
            self.assertLess(self.store.discovery_target(self.target)["valid_until"], self.now)
            self.now += 60

    def test_manual_sync_never_invokes_permission_refresher(self):
        self.configure_permission_refresh()
        self.sync.run_manual(self.target, now=self.now)
        self.now += 301
        self.sync.run_manual(self.target, now=self.now)
        self.refresher.assert_not_called()

    def test_permission_refresh_without_configured_checker_is_noop(self):
        self.now += 301
        self.assertEqual(self.sync.run_authorization_due(now=self.now), [])

    def test_permission_refresh_rejects_archived_or_reconfigured_target(self):
        from pullwise_server.product_discovery import DiscoveryAuthorizationProof
        def callback(**kwargs):
            self.store.archive_watch(self.watch["id"], expected_revision=self.watch["revision"])
            return DiscoveryAuthorizationProof(accessible=True, observed_at=self.now, valid_until=self.now + 300)
        self.configure_permission_refresh(callback)
        self.now += 241
        self.assertEqual(self.sync.run_authorization_due(now=self.now), [{"status": "superseded"}])
        self.assertIsNone(self.store.discovery_target(self.target))

    def test_permission_checker_receives_watch_owner_and_target_scope(self):
        self.configure_permission_refresh()
        self.now += 241
        self.sync.run_authorization_due(now=self.now)
        target = self.refresher.call_args.kwargs["target"]
        self.assertEqual(target["owner_id"], "usr_1")
        self.assertIsNone(target["target_repository_id"])

    def test_expired_refresh_lease_cannot_publish_and_can_be_reclaimed(self):
        from pullwise_server.product_discovery import DiscoveryAuthorizationProof
        def callback(**kwargs):
            self.now += 120
            return DiscoveryAuthorizationProof(accessible=True, observed_at=self.now, valid_until=self.now + 300)
        self.configure_permission_refresh(callback)
        self.now += 241
        old_expiry = self.store.discovery_target(self.target)["valid_until"]
        self.assertEqual(self.sync.run_authorization_due(now=self.now), [{"status": "superseded"}])
        self.assertEqual(self.store.discovery_target(self.target)["valid_until"], old_expiry)
        self.configure_permission_refresh()
        self.assertEqual(self.sync.run_authorization_due(now=self.now), [{"status": "renewed"}])

    def test_denied_permission_can_recover_only_after_fresh_check_and_backoff(self):
        self.authorize(accessible=False, revision=2)
        self.configure_permission_refresh()
        self.now += 301
        self.assertEqual(self.sync.run_authorization_due(now=self.now), [{"status": "renewed"}])
        self.assertEqual(self.store.discovery_target(self.target)["authorization_revision"], 3)
        self.assertEqual(self.sync.run_authorization_due(now=self.now), [])
        self.assertEqual(self.counts(), (0, 0))

    def test_permission_failure_does_not_block_next_target(self):
        from pullwise_server.product_discovery import DiscoveryAuthorizationProof
        watch = self.store.create_watch(owner_id="usr_2", target_repository_id=None,
            upstream_repository_id="github:124", billing_owner_id="usr_2", interests=["API"],
            enabled=True, analysis_enabled=True)
        key = self.store.set_discovery_authorization(resource_kind="watch", resource_id=watch["id"],
            module="updates", github_repository_id="124", installation_id=None, app_id=None,
            authorization_revision=1, accessible=True, valid_until=self.now + 300, observed_at=self.now)
        def callback(*, target, now):
            if target["control_key"] == self.target:
                raise RuntimeError("unavailable")
            return DiscoveryAuthorizationProof(accessible=True, observed_at=now, valid_until=now + 300)
        self.configure_permission_refresh(callback)
        self.now += 241
        with self.assertLogs("pullwise_server.product_discovery", level="WARNING"):
            results = self.sync.run_authorization_due(now=self.now)
        self.assertCountEqual(results, [{"status": "unavailable"}, {"status": "renewed"}])
        self.assertEqual(self.store.discovery_target(key)["valid_until"], self.now + 300)

    def authorize(self, *, accessible=True, revision=None):
        if revision is None:
            with closing(self.store.connect()) as connection:
                previous = connection.execute("""SELECT resource_id,authorization_revision
                    FROM discovery_targets WHERE control_key=?""", (self.target,)).fetchone() if hasattr(self, "target") else None
            revision = (1 if previous is None else int(previous["authorization_revision"])
                + (previous["resource_id"] != self.watch["id"]))
        return self.store.set_discovery_authorization(resource_kind="watch", resource_id=self.watch["id"],
            module="updates", github_repository_id="123", installation_id="11", app_id="7",
            authorization_revision=revision, accessible=accessible, valid_until=self.now + 300,
            observed_at=self.now)

    def source(self, identifier="1", *, age=0, body="OAuth changed"):
        from datetime import datetime, timezone
        changed = datetime.fromtimestamp(self.now - age, timezone.utc).isoformat()
        return release_source(upstream_repository_id="github:123", release={
            "id": identifier, "body": body, "tag_name": "v" + identifier,
            "published_at": changed, "updated_at": changed,
            "html_url": "https://github.com/acme/upstream/releases/" + identifier})

    @staticmethod
    def page(sources, *, cursor=None, watermark="w1"):
        from pullwise_server.product_discovery import FactPage
        return FactPage(sources=tuple(sources), next_cursor=cursor, high_watermark=watermark)

    def poll(self, sources, **page_options):
        self.reader.return_value = self.page(sources, **page_options)
        return self.sync.run_scheduled(self.target, now=self.now)

    def next_poll(self):
        self.now += 6 * 60 * 60
        self.authorize()

    def status(self, source):
        rows = self.store.list_sources_for_billing_owner("usr_1")
        return next(row for row in rows if row["id"] == source["sourceId"])["contexts"][0]["processingStatus"]

    def counts(self):
        usage = self.store.processing_usage(billing_owner_id="usr_1", period="cycle:fixture")
        return self.store.count_jobs(job_type="analyze_source"), usage["reserved"]

    def establish_without_backfill(self):
        self.poll([])
        self.next_poll()

    def event(self, delivery="d1", action="published"):
        raw = json.dumps({"action": action, "installation": {"id": 11},
                          "repository": {"id": 123}, "release": {"id": 1},
                          "trusted_trigger": "retry"}).encode()
        signature = "sha256=" + hmac.new(b"local-fixture-secret", raw, hashlib.sha256).hexdigest()
        return self.sync.receive_github_event(raw, signature=signature, event="release",
                                            delivery_id=delivery, now=self.now)

    def test_old_source_is_persisted_but_only_new_source_is_reserved_once(self):
        self.establish_without_backfill()
        old = self.source("old", age=90 * 86400)
        new = self.source("new")
        self.poll([old, new])
        self.assertEqual(self.status(old), "not_scheduled")
        self.assertEqual(self.status(new), "pending")
        self.assertEqual(self.counts(), (1, 1))
        self.next_poll()
        self.poll([old, new])
        self.assertEqual(self.counts(), (1, 1))

    def test_manual_sync_does_not_touch_controls_or_reservations(self):
        source = self.source()
        self.reader.return_value = self.page([source])
        self.sync.run_manual(self.target, now=self.now)
        self.assertEqual(self.counts(), (0, 0))
        with closing(self.store.connect()) as connection:
            self.assertIsNone(connection.execute("SELECT eligible_since FROM processing_controls WHERE control_key = ?",
                                                (self.target,)).fetchone()[0])
        self.assertEqual(self.status(source), "pending")
        self.poll([source])
        self.assertEqual(self.counts(), (1, 1))

    def test_signed_event_is_durable_deduplicated_and_waits_for_stability(self):
        self.establish_without_backfill()
        source = self.source()
        self.reader.return_value = self.page([source])
        self.assertEqual(self.event()["accepted"], 1)
        self.assertEqual(self.event()["accepted"], 0)
        self.assertEqual(self.sync.run_events(now=self.now), [])
        self.assertEqual(self.counts(), (0, 0))
        self.now += 30
        self.sync.run_events(now=self.now)
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.store.list_jobs()[0]["trustedTrigger"], "github_event")
        self.assertEqual(self.event()["accepted"], 0)

    def test_unsigned_and_wrong_installation_events_have_no_effect(self):
        with self.assertRaisesRegex(ValueError, "GITHUB_SIGNATURE_INVALID"):
            self.sync.receive_github_event(b"{}", signature="sha256=bad", event="release",
                                         delivery_id="bad", now=self.now)
        self.store.set_discovery_authorization(resource_kind="watch", resource_id=self.watch["id"],
            module="updates", github_repository_id="123", installation_id="22", app_id="7",
            authorization_revision=2, accessible=True, valid_until=self.now + 300, observed_at=self.now)
        self.assertEqual(self.event()["accepted"], 0)
        self.reader.assert_not_called()

    def test_restart_recreation_and_late_pagination_do_not_expand_backfill(self):
        recent = self.source("first", age=86400)
        self.poll([recent], cursor="page2")
        self.assertEqual(self.counts(), (1, 1))
        self.store.archive_watch(self.watch["id"], expected_revision=self.watch["revision"])
        self.watch = self.create_watch()
        self.store = ProductStore(self.path)
        self.store.initialize()
        self.sync.store = self.store
        self.next_poll()
        late = self.source("late", age=2 * 86400)
        self.poll([late], watermark="w2")
        self.assertEqual(self.status(late), "not_scheduled")
        self.assertEqual(self.counts(), (1, 0))  # Archiving released the old queued reservation.
        self.assertEqual(self.reader.call_args.kwargs["cursor"], "page2")

    def test_old_response_cannot_overwrite_newer_sync(self):
        self.establish_without_backfill()
        old, new = self.source(body="old"), self.source(body="new")
        def delayed(**kwargs):
            self.reader.side_effect = None
            self.reader.return_value = self.page([new])
            self.sync.run_manual(self.target, now=self.now)
            return self.page([old])
        self.reader.side_effect = delayed
        result = self.sync.run_scheduled(self.target, now=self.now)
        self.assertEqual(result["status"], "superseded")
        self.assertEqual(self.counts(), (0, 0))
        with closing(self.store.connect()) as connection:
            body = connection.execute("SELECT v.content_json FROM source_records s JOIN source_versions v ON v.id=s.latest_version").fetchone()[0]
        self.assertIn('"new"', body)

    def test_older_authoritative_response_does_not_regress_current_version(self):
        self.establish_without_backfill()
        old = self.source(body="old", age=60)
        new = self.source(body="new")
        self.poll([new])
        self.next_poll()
        self.poll([old])
        self.assertEqual(self.counts(), (1, 1))

    def test_revocation_during_fetch_prevents_publication_and_reservation(self):
        def revoked(**kwargs):
            self.authorize(accessible=False, revision=2)
            return self.page([self.source()])
        self.reader.side_effect = revoked
        self.assertEqual(self.sync.run_scheduled(self.target, now=self.now)["status"], "unauthorized")
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.store.list_sources_for_billing_owner("usr_1"), [])

    def test_queue_rejection_retains_facts_and_releases_new_reservation(self):
        self.sync.owner_active_limit = 1
        first, second = self.source("1"), self.source("2")
        self.poll([first, second])
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.status(second), "throttled")
        with closing(self.store.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM processing_usage_ledger WHERE state='released'").fetchone()[0], 1)

    def test_newest_version_supersedes_job_and_releases_old_reservation(self):
        source = self.source(body="first")
        self.poll([source])
        self.next_poll()
        self.poll([self.source(body="second")])
        self.assertEqual(self.counts(), (2, 1))
        self.assertEqual([job["status"] for job in self.store.list_jobs()], ["superseded", "queued"])

    def test_checkpoint_conflict_keeps_facts_but_cannot_admit_analysis(self):
        def raced(**kwargs):
            self.store.advance_discovery_checkpoint(self.target, expected_cursor=None,
                expected_high_watermark=None, next_cursor="other", next_high_watermark="other",
                observed_at=self.now)
            return self.page([self.source()])
        self.reader.side_effect = raced
        self.assertEqual(self.sync.run_scheduled(self.target, now=self.now)["status"], "checkpoint_conflict")
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(len(self.store.list_sources_for_billing_owner("usr_1")), 1)

    def test_normal_cadence_survives_restart_and_manual_sync_cannot_accelerate_it(self):
        self.poll([])
        source = self.source()
        self.reader.return_value = self.page([source])
        self.sync.run_manual(self.target, now=self.now)
        self.sync.store = ProductStore(self.path)
        self.assertEqual(self.poll([source])["status"], "not_due")
        self.assertEqual(self.counts(), (0, 0))
        self.next_poll()
        self.poll([source])
        self.assertEqual(self.counts(), (1, 1))

    def test_revocation_cancels_existing_job_and_fences_its_claim(self):
        self.poll([self.source()])
        self.store.claim_next_analysis_job(now=self.now)
        self.authorize(accessible=False, revision=2)
        self.assertEqual(self.counts(), (1, 0))
        self.assertEqual(self.store.list_jobs()[0]["status"], "cancelled")
        self.assertEqual(self.store.list_sources_for_billing_owner("usr_1"), [])

    def test_failed_admission_rolls_back_checkpoint_and_reservation_but_keeps_facts(self):
        with patch("pullwise_server.product_jobs.ProductJobScheduler.schedule_analysis", side_effect=RuntimeError("fixture crash")):
            with self.assertRaisesRegex(RuntimeError, "fixture crash"):
                self.poll([self.source()])
        self.assertEqual(self.counts(), (0, 0))
        with closing(self.store.connect()) as connection:
            self.assertIsNone(connection.execute("SELECT high_watermark FROM processing_controls WHERE control_key=?", (self.target,)).fetchone()[0])
        self.assertEqual(len(self.store.list_sources_for_billing_owner("usr_1")), 1)

    def test_analysis_disabled_and_missing_timestamp_never_schedule(self):
        source = self.source()
        source["sourceFacts"]["publishedAt"] = None
        source["sourceFacts"]["updatedAt"] = None
        self.poll([source])
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.status(source), "not_scheduled")

    def test_content_change_while_running_keeps_the_old_lease_fence(self):
        self.poll([self.source()])
        first = self.store.claim_next_analysis_job(now=self.now)
        self.event()
        self.now += 30
        self.reader.return_value = self.page([self.source(age=30, body="second")])
        self.sync.run_events(now=self.now)
        self.assertEqual(self.counts(), (2, 1))
        self.assertIsNone(self.store.claim_next_analysis_job(now=self.now))
        self.assertEqual(self.store.list_jobs()[1]["nextAttemptAt"], first["claimedUntil"])

    def test_exhausted_input_is_not_reset_by_auth_refresh_or_restart(self):
        source = self.source()
        self.poll([source])
        for attempt in range(3):
            claim = self.store.claim_next_analysis_job(now=self.now)
            self.store.record_analysis_failure(job_id=claim["id"], claim_token=claim["claimToken"],
                now=self.now, retryable=True, next_attempt_at=self.now + 1)
            self.now += 1
        self.next_poll()
        self.authorize(revision=2)
        self.sync.store = ProductStore(self.path)
        self.poll([source])
        self.assertEqual(self.counts(), (1, 0))
        self.assertEqual(self.status(source), "failed")

    def test_watch_recreated_after_multiple_config_revisions_retains_stable_context(self):
        self.watch = self.store.update_watch(self.watch["id"], expected_revision=1, enabled=True)
        self.poll([self.source()])
        self.store.archive_watch(self.watch["id"], expected_revision=self.watch["revision"])
        self.watch = self.create_watch()
        self.next_poll()
        self.poll([self.source(body="new content")])
        self.assertEqual(self.counts(), (2, 1))

    def test_http_webhook_route_verifies_raw_body_before_durable_ack(self):
        from pullwise_server import app
        from test_product_api_routes import RouteHarness
        raw = json.dumps({"action": "published", "installation": {"id": 11},
                          "repository": {"id": 123}, "release": {"id": 1}}).encode()
        signature = "sha256=" + hmac.new(b"local-fixture-secret", raw, hashlib.sha256).hexdigest()
        handler = RouteHarness("/webhooks/github", headers={"X-Hub-Signature-256": signature,
                               "X-GitHub-Event": "release", "X-GitHub-Delivery": "route"})
        handler.read_raw_body = lambda: raw
        with patch.dict("os.environ", {"PULLWISE_GITHUB_APP_ID": "7", "PULLWISE_GITHUB_WEBHOOK_SECRET": "local-fixture-secret"}), \
             patch.object(app.db, "database_path", return_value=str(self.path)):
            handler.handle_post(handler.path, {}, ["webhooks", "github"])
        self.assertEqual(handler.status, 202)
        with closing(self.store.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM github_deliveries").fetchone()[0], 1)
        self.assertEqual(self.counts(), (0, 0))

    def test_disabled_configuration_immediately_fences_queued_analysis(self):
        self.poll([self.source()])
        self.store.update_watch(self.watch["id"], expected_revision=1, analysis_enabled=False)
        self.assertIsNone(self.store.claim_next_analysis_job(now=self.now))
        self.assertEqual(self.counts(), (1, 0))

    def test_pr_and_ci_use_normal_fifteen_minute_discovery(self):
        from datetime import datetime, timezone
        from pullwise_server.github_sources import ci_failure_source, pr_issue_comment_source
        self.store.put_repository_service(repository_id="repo_1", installation_id="11", billing_owner_id="usr_1",
            expected_revision=0, enabled=True, modules={"pr": True, "ci": True},
            analysis_enabled={"pr": True, "ci": True}, allow_member_sync=True, default_assignee_id=None, priority_order=0)
        stamp = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        pr = pr_issue_comment_source(repository_id="repo_1", issue={"number": 1, "pull_request": {}},
            comment={"id": 1, "body": "Please add a test", "created_at": stamp, "updated_at": stamp,
                     "html_url": "https://github.com/acme/api/pull/1#issuecomment-1"})
        ci = ci_failure_source(repository_id="repo_1", run={"id": 1, "run_attempt": 1},
            job={"id": 2, "conclusion": "timed_out", "completed_at": stamp, "html_url": "https://github.com/acme/api/actions/runs/1/job/2"},
            evidence_windows=[{"windowId": "w1", "text": "test timed out"}])
        for module, source in (("pr", pr), ("ci", ci)):
            target = self.store.set_discovery_authorization(resource_kind="repository", resource_id="repo_1", module=module,
                github_repository_id="456", installation_id="11", app_id="7", authorization_revision=1,
                accessible=True, valid_until=self.now + 300, observed_at=self.now)
            self.reader.return_value = self.page([source])
            self.sync.run_scheduled(target, now=self.now)
            self.assertEqual(self.store.discovery_target(target)["next_scheduled_at"], self.now + 900)
        self.assertEqual(self.counts(), (2, 2))

    def test_old_pending_event_survives_restart_but_uses_current_authoritative_body(self):
        self.establish_without_backfill()
        self.event()
        self.now += 30
        self.sync.store = ProductStore(self.path)
        current = self.source(body="authority", age=30)
        self.reader.return_value = self.page([current])
        self.sync.run_events(now=self.now)
        self.event(delivery="out-of-order", action="edited")
        self.now += 30
        self.sync.run_events(now=self.now)
        self.assertEqual(self.counts(), (1, 1))

    def test_context_change_is_not_disguised_as_new_source_discovery(self):
        source = self.source()
        self.poll([source])
        self.store.update_watch(self.watch["id"], expected_revision=1, interests=["database"])
        self.next_poll()
        self.poll([source])
        self.assertEqual(self.counts(), (1, 0))
        self.assertEqual(self.status(source), "not_scheduled")

    def test_authorization_expiring_during_read_cannot_admit(self):
        source = self.source()
        def slow(**kwargs):
            self.now += 301
            return self.page([source])
        self.reader.side_effect = slow
        self.assertEqual(self.sync.run_scheduled(self.target, now=self.now)["status"], "unauthorized")
        self.assertEqual(self.counts(), (0, 0))

    def test_first_event_uses_verified_authorization_time_before_stability_delay(self):
        source = self.source()
        self.reader.return_value = self.page([source])
        self.event()
        self.now += 30
        self.sync.run_events(now=self.now)
        self.assertEqual(self.counts(), (1, 1))

    def test_released_backpressure_reserves_in_current_period_when_retried(self):
        self.sync.owner_active_limit = 1
        first, second = self.source("1"), self.source("2")
        self.poll([first, second])
        self.next_poll()
        self.sync.owner_active_limit = 2
        self.sync.processing_budget = lambda owner, now: ("cycle:next", 100)
        self.poll([second])
        self.assertEqual(self.store.processing_usage(billing_owner_id="usr_1", period="cycle:next")["reserved"], 1)

    def test_concurrent_normal_ticks_admit_only_once(self):
        from concurrent.futures import ThreadPoolExecutor
        self.reader.return_value = self.page([self.source()])
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: self.sync.run_due(now=self.now), range(6)))
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.reader.call_count, 1)

    def test_conflicting_source_ids_in_one_page_cannot_misbind_charge(self):
        with self.assertRaisesRegex(ValueError, "INVALID_FACT_PAGE"):
            self.poll([self.source(body="A"), self.source(body="B")])
        self.assertEqual(self.counts(), (0, 0))

    def test_signed_installation_revocation_cancels_and_hides_immediately(self):
        self.poll([self.source()])
        raw = json.dumps({"action": "deleted", "installation": {"id": 11}}).encode()
        signature = "sha256=" + hmac.new(b"local-fixture-secret", raw, hashlib.sha256).hexdigest()
        self.sync.receive_github_event(raw, signature=signature, event="installation", delivery_id="revoked", now=self.now)
        self.assertEqual(self.counts(), (1, 0))
        self.assertEqual(self.store.list_sources_for_billing_owner("usr_1"), [])

    def test_multiple_coalescings_preserve_original_running_lease(self):
        self.poll([self.source()])
        first = self.store.claim_next_analysis_job(now=self.now)
        for index in (2, 3):
            self.event(delivery=f"d{index}", action="edited")
            self.now += 30
            self.reader.return_value = self.page([self.source(age=30, body=f"version {index}")])
            self.sync.run_events(now=self.now)
        self.assertEqual(self.counts(), (3, 1))
        self.assertIsNone(self.store.claim_next_analysis_job(now=self.now))
        self.assertEqual(self.store.list_jobs()[-1]["nextAttemptAt"], first["claimedUntil"])

    def test_authorization_revision_changes_cannot_give_same_input_more_attempts(self):
        source = self.source()
        self.poll([source])
        for revision in range(2, 5):
            self.store.claim_next_analysis_job(now=self.now, lease_seconds=1)
            self.authorize(revision=revision)
            self.event(delivery=f"authorization-{revision}")
            self.now += 30
            self.reader.return_value = self.page([source])
            self.sync.run_events(now=self.now)
        self.assertEqual(self.counts(), (3, 0))
        self.assertEqual(self.status(source), "failed")

    def test_server_background_loop_runs_normal_schedule_without_http_requests(self):
        import threading
        from pullwise_server import app
        self.configure_permission_refresh()
        self.now += 301
        self.reader.return_value = self.page([self.source()])
        completed = threading.Event()
        original = self.sync.run_due
        def tick(*, now):
            result = original(now=now)
            completed.set()
            return result
        with patch.object(self.sync, "run_due", side_effect=tick):
            server = app.PullwiseThreadingHTTPServer(("127.0.0.1", 0), app.PullwiseHandler, fact_sync=self.sync)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
            thread.start()
            try:
                self.assertTrue(completed.wait(5), "normal server clock did not run fact sync")
                self.refresher.assert_called_once()
                self.assertEqual(self.counts(), (1, 1))
            finally:
                server.shutdown()
                thread.join(5)
                server.server_close()
            self.assertFalse(thread.is_alive())

    def test_manual_edit_marks_new_content_pending_and_preserves_context_staleness(self):
        source = self.source()
        self.poll([source])
        with closing(self.store.connect()) as connection, connection:
            connection.execute("UPDATE source_contexts SET processing_status='assessed', context_stale=1")
        self.reader.return_value = self.page([self.source(body="changed")])
        self.sync.run_manual(self.target, now=self.now)
        context = self.store.list_sources_for_billing_owner("usr_1")[0]["contexts"][0]
        self.assertEqual(context["processingStatus"], "pending")
        self.assertTrue(context["contextStale"])
        self.assertEqual(self.counts(), (1, 1))


if __name__ == "__main__":
    unittest.main()
