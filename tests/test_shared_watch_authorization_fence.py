from pathlib import Path
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from pullwise_server.github_sources import release_source
from pullwise_server.product_discovery import DiscoveryAuthorizationProof, FactPage, ProductFactSync
from pullwise_server.product_store import ProductStore


class SharedWatchAuthorizationFenceTest(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "shared.sqlite3"
        self.store = ProductStore(self.path)
        self.store.initialize()
        self.now = 1_800_000_000
        clock = patch("pullwise_server.product_store._now", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.service = dict(repository_id="github:456", installation_id="22", billing_owner_id="owner",
            expected_revision=0, enabled=True, modules={"pr": True, "ci": True},
            analysis_enabled={"pr": False, "ci": False}, allow_member_sync=False,
            default_assignee_id=None, priority_order=0)
        self.store.put_repository_service(**self.service)
        watch = self.store.create_watch(owner_id="owner", target_repository_id="github:456",
            upstream_repository_id="github:123", billing_owner_id="owner", interests=["OAuth"],
            enabled=True, analysis_enabled=True)
        self.key = self.store.set_discovery_authorization(resource_kind="watch", resource_id=watch["id"],
            module="updates", github_repository_id="123", installation_id=None, app_id="7",
            authorization_revision=1, accessible=True, valid_until=self.now + 300, observed_at=self.now)
        self.now += 241

    def renew_during(self, mutation):
        def checker(**kwargs):
            mutation(ProductStore(self.path))
            return DiscoveryAuthorizationProof(True, self.now, self.now + 300)
        reader = Mock()
        sync = ProductFactSync(self.store, read_page=reader,
            processing_budget=Mock(), refresh_authorization=checker, app_id="7", webhook_secret="fixture")
        result = sync.run_authorization_due(now=self.now)
        reader.assert_not_called()
        return result

    def test_installation_change_fences_late_proof(self):
        result = self.renew_during(lambda other: other.put_repository_service(
            **dict(self.service, expected_revision=1, installation_id="33")))
        self.assertEqual(result, [{"status": "superseded"}])

    def test_configuration_change_fences_late_proof(self):
        result = self.renew_during(lambda other: other.put_repository_service(
            **dict(self.service, expected_revision=1, allow_member_sync=True)))
        self.assertEqual(result, [{"status": "superseded"}])

    def test_target_installation_revocation_fences_late_proof(self):
        result = self.renew_during(lambda other: other.revoke_discovery_installation(
            app_id="7", installation_id="22", repository_ids=["456"], observed_at=self.now))
        self.assertEqual(result, [{"status": "superseded"}])
        self.assertFalse(self.store.discovery_target(self.key)["accessible"])

    def test_owner_change_fences_late_proof(self):
        def mutation(other):
            with other._immediate() as connection:
                connection.execute("UPDATE repository_services SET billing_owner_id='other' WHERE repository_id='github:456'")
        self.assertEqual(self.renew_during(mutation), [{"status": "superseded"}])

    def test_target_revocation_requires_matching_app_installation_and_repository(self):
        for app, installation, repositories in (("8", "22", None), ("7", "33", None), ("7", "22", ["123"])):
            with self.subTest(app=app, installation=installation, repositories=repositories):
                count = ProductStore(self.path).revoke_discovery_installation(
                    app_id=app, installation_id=installation, repository_ids=repositories, observed_at=self.now)
                self.assertEqual(count, 0)
                self.assertTrue(self.store.discovery_target(self.key)["accessible"])

    def test_entire_target_installation_revocation_needs_no_repository_filter(self):
        count = ProductStore(self.path).revoke_discovery_installation(
            app_id="7", installation_id="22", repository_ids=None, observed_at=self.now)
        self.assertEqual(count, 1)
        self.assertFalse(self.store.discovery_target(self.key)["accessible"])

    def test_disabled_or_missing_target_does_not_renew(self):
        for remove in (False, True):
            with self.subTest(remove=remove):
                with self.store._immediate() as connection:
                    connection.execute("DELETE FROM repository_services" if remove else "UPDATE repository_services SET enabled=0")
                checker = Mock()
                sync = ProductFactSync(self.store, read_page=Mock(), processing_budget=Mock(), refresh_authorization=checker,
                    app_id="7", webhook_secret="fixture")
                self.assertEqual(sync.run_authorization_due(now=self.now), [{"status": "not_due"}])
                checker.assert_not_called()
                self.now += 61

    def test_public_upstream_needs_no_installation(self):
        snapshot = self.store.discovery_target(self.key)
        self.assertIsNone(snapshot["installation_id"])
        self.assertEqual(snapshot["target_installation_id"], "22")
        self.assertEqual(snapshot["target_repository_revision"], 1)
        self.assertEqual(snapshot["target_billing_owner_id"], "owner")
        self.assertEqual(self.renew_during(lambda other: None), [{"status": "renewed"}])

    def fact_sync(self, mutation=None):
        stamp = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        source = release_source(upstream_repository_id="github:123", release={
            "id": "1", "body": "OAuth changed", "tag_name": "v1", "published_at": stamp,
            "updated_at": stamp, "html_url": "https://github.com/acme/upstream/releases/1"})
        def reader(**kwargs):
            if mutation:
                mutation(ProductStore(self.path))
            return FactPage(sources=(source,), next_cursor=None, high_watermark="w1")
        return ProductFactSync(self.store, read_page=reader,
            processing_budget=lambda owner, now: ("fixture", 100), app_id="7", webhook_secret="fixture")

    def test_parent_installation_change_during_fact_read_discards_page(self):
        sync = self.fact_sync(lambda other: other.put_repository_service(
            **dict(self.service, expected_revision=1, installation_id="33")))
        self.assertEqual(sync.run_scheduled(self.key, now=self.now), {"status": "unauthorized"})
        self.assertEqual(self.store.list_sources_for_billing_owner("owner"), [])

    def test_parent_configuration_change_during_fact_read_discards_page(self):
        sync = self.fact_sync(lambda other: other.put_repository_service(
            **dict(self.service, expected_revision=1, allow_member_sync=True)))
        self.assertEqual(sync.run_scheduled(self.key, now=self.now), {"status": "unauthorized"})
        self.assertEqual(self.store.list_sources_for_billing_owner("owner"), [])

    def test_parent_configuration_cancels_queued_watch_job_and_releases_usage(self):
        self.fact_sync().run_scheduled(self.key, now=self.now)
        self.assertEqual(self.store.processing_usage(billing_owner_id="owner", period="fixture")["reserved"], 1)
        ProductStore(self.path).put_repository_service(**dict(self.service, expected_revision=1, allow_member_sync=True))
        self.assertEqual(self.store.list_jobs()[0]["status"], "cancelled")
        self.assertEqual(self.store.processing_usage(billing_owner_id="owner", period="fixture")["reserved"], 0)
        self.assertIsNone(self.store.claim_next_analysis_job(now=self.now))

    def test_parent_change_fences_saved_context_and_preserves_running_lease(self):
        self.fact_sync().run_scheduled(self.key, now=self.now)
        self.store.claim_next_analysis_job(now=self.now)
        with self.store._read() as connection:
            source = dict(connection.execute("SELECT * FROM source_records").fetchone())
            context = dict(connection.execute("SELECT * FROM source_contexts").fetchone())
            job = dict(connection.execute("SELECT * FROM background_jobs").fetchone())
        item = self.store.create_item(context_id=self.key, unit_type="update_release", unit_key="release:1")
        ProductStore(self.path).put_repository_service(**dict(self.service, expected_revision=1, allow_member_sync=True))
        with self.assertRaisesRegex(ValueError, "STALE_CONTEXT"):
            self.store.publish_item_snapshot(item_id=item["id"], expected_item_revision=item["revision"],
                sources=[{"sourceId": source["source_id"], "sourceVersion": source["latest_version"],
                          "sourceRevision": source["source_revision"]}],
                context_fences=[{"sourceId": source["source_id"], "contextId": self.key,
                    "contextVersion": context["context_version"], "configurationRevision": context["configuration_revision"],
                    "authorizationRevision": context["authorization_revision"]}],
                snapshot={"module": "updates", "evidence": []}, observed_at=self.now)
        with self.store._read() as connection:
            current = dict(connection.execute("SELECT * FROM background_jobs").fetchone())
        self.assertEqual(current["claimed_until"], job["claimed_until"])
        self.assertEqual(current["claim_token"], job["claim_token"])
        self.assertEqual(current["attempt"], job["attempt"])

    def test_parent_installation_change_requires_fresh_proof_before_new_sync(self):
        self.fact_sync().run_scheduled(self.key, now=self.now)
        self.store.claim_next_analysis_job(now=self.now)
        with self.store._read() as connection:
            before = dict(connection.execute("SELECT * FROM background_jobs").fetchone())
        ProductStore(self.path).put_repository_service(**dict(self.service, expected_revision=1, installation_id="33"))
        self.assertFalse(self.store.discovery_target(self.key)["accessible"])
        self.assertEqual(self.store.list_sources_for_billing_owner("owner"), [])
        with self.store._read() as connection:
            after = dict(connection.execute("SELECT * FROM background_jobs").fetchone())
        self.assertEqual(after["state"], "cancelled")
        self.assertEqual(after["attempt"], before["attempt"])
        self.assertEqual(after["claimed_until"], before["claimed_until"])
        self.assertEqual(self.store.processing_usage(billing_owner_id="owner", period="fixture")["reserved"], 0)
        self.assertEqual(self.fact_sync().run_manual(self.key, now=self.now), {"status": "unauthorized"})
        self.assertEqual(self.renew_during(lambda other: None), [{"status": "renewed"}])
        self.assertTrue(self.store.discovery_target(self.key)["accessible"])
