"""Compose actual adapters with HTTP response fixtures and durable discovery.

These tests validate integration, not live GitHub credentials or Cloudflare.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from pullwise_server.github_authorization import GitHubAuthorizationBinding, GitHubAuthorizationChecker
from pullwise_server.github_release_reader import GitHubReleaseReader
from pullwise_server.github_transport import GitHubRESTTransport
from pullwise_server.product_discovery import ProductFactSync
from pullwise_server.product_store import ProductStore


class GitHubIngestionContractsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = ProductStore(Path(directory.name) / "ingestion.sqlite3")
        self.store.initialize()
        self.now = 1_800_000_000
        clock = patch("pullwise_server.product_store._now", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        watch = self.store.create_watch(owner_id="usr_1", target_repository_id=None,
            upstream_repository_id="github:123", billing_owner_id="usr_1", interests=["API"],
            enabled=True, analysis_enabled=False)
        self.target = self.store.set_discovery_authorization(resource_kind="watch", resource_id=watch["id"],
            module="updates", github_repository_id="123", installation_id=None, app_id=None,
            authorization_revision=1, accessible=True, valid_until=self.now + 300, observed_at=self.now)
        self.paths = []
        self.release = {"id": 9, "tag_name": "v1", "name": "Version 1", "body": "API changed",
            "draft": False, "prerelease": False, "published_at": "2026-09-21T00:00:00Z",
            "html_url": "https://github.com/acme/lib/releases/tag/v1"}
        self.transport = GitHubRESTTransport(request=self.http, clock=lambda: self.now)
        self.binding = GitHubAuthorizationBinding(billing_owner_id="usr_1", github_user_id="7",
            repository_id="123", repository_full_name="acme/lib", user_token="fixture-token")
        self.checker = GitHubAuthorizationChecker(get_json=self.transport, resolve_binding=lambda target: self.binding)
        self.reader = GitHubReleaseReader(get_json=self.transport, token_for_target=lambda target: "fixture-token")
        self.sync = ProductFactSync(self.store, read_page=self.reader.read_page, refresh_authorization=self.checker,
            processing_budget=lambda owner, now: ("fixture", 10), app_id="7", webhook_secret="fixture")

    def http(self, url, **kwargs):
        path = urlsplit(url).path
        self.paths.append(path)
        if path == "/user":
            payload = {"id": 7}
        elif path in {"/repositories/123", "/repos/acme/lib"}:
            payload = {"id": 123, "full_name": "acme/lib", "private": False}
        elif path == "/repos/acme/lib/releases":
            payload = [self.release]
        else:
            raise AssertionError("Unexpected fixture route")
        response = Mock(status_code=200, headers={"Content-Type": "application/json"})
        response.iter_content.return_value = [json.dumps(payload).encode()]
        return response

    def test_expired_permission_renews_before_release_facts_are_saved(self):
        self.now += 301
        self.assertEqual(self.sync.run_manual(self.target, now=self.now), {"status": "unauthorized"})
        self.assertEqual(self.paths, [])
        self.assertEqual(self.sync.run_authorization_due(now=self.now), [{"status": "renewed"}])
        self.assertEqual(self.sync.run_due(now=self.now), [{"status": "completed", "sources": 1}])
        self.assertEqual(self.paths[0], "/user")
        with closing(self.store.connect()) as connection:
            source = connection.execute("SELECT * FROM source_records").fetchone()
            self.assertEqual(source["source_type"], "release")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM processing_usage_ledger").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_contexts").fetchone()[0], 1)

    def test_manual_release_edit_saves_facts_without_refresh_or_eligibility(self):
        self.assertEqual(self.sync.run_manual(self.target, now=self.now), {"status": "completed", "sources": 1})
        self.release["body"] = "Edited release notes"
        self.now += 1
        self.assertEqual(self.sync.run_manual(self.target, now=self.now), {"status": "completed", "sources": 1})
        self.assertNotIn("/user", self.paths)
        with closing(self.store.connect()) as connection:
            control = connection.execute("SELECT * FROM processing_controls WHERE control_key=?", (self.target,)).fetchone()
            self.assertIsNone(control["eligible_since"])
            self.assertIsNone(control["discovery_cursor"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0], 0)

    def test_transport_retry_after_survives_checker_and_persists(self):
        self.now += 241
        response = Mock(status_code=429, headers={"Retry-After": "600"})
        self.transport.request = Mock(return_value=response)
        with self.assertLogs("pullwise_server.product_discovery", level="WARNING"):
            self.assertEqual(self.sync.run_authorization_due(now=self.now), [{"status": "unavailable"}])
        with closing(self.store.connect()) as connection:
            retry_at = connection.execute("SELECT next_attempt_at FROM discovery_authorization_refreshes WHERE control_key=?",
                                          (self.target,)).fetchone()[0]
        self.assertEqual(retry_at, self.now + 600)
        response.close.assert_called_once()

    def credentials(self):
        from pullwise_server.github_credentials import GitHubCredentialResolver
        return GitHubCredentialResolver(current_target=self.store.discovery_target,
            current_account=lambda owner: {"id": owner, "githubId": "7"},
            identities_for_user=lambda account: [{"id": "identity", "userId": "usr_1", "githubUserId": "7",
                                                   "accessToken": "fixture-token", "status": "active"}],
            installation_access_for_user=Mock(side_effect=AssertionError("personal watch has no installation")),
            repository_for_account=lambda account, repository_id: {"id": 123, "fullName": "acme/lib"},
            app_id="7", app_token=Mock(side_effect=AssertionError("not needed")),
            installation_token=Mock(side_effect=AssertionError("not needed")), clock=lambda: self.now)

    def test_factory_composes_scheduled_reader_with_current_credential_snapshot(self):
        from pullwise_server.github_ingestion import build_github_fact_sync
        credentials = self.credentials()
        sync = build_github_fact_sync(self.store, credentials=credentials, get_json=self.transport,
            processing_budget=lambda owner, now: ("fixture", 10), app_id="7", webhook_secret="fixture")
        self.assertEqual(self.paths, [])  # Assembly itself does no network work.
        self.now += 301
        self.assertEqual(sync.run_authorization_due(now=self.now), [{"status": "renewed"}])
        self.paths.clear()
        self.assertEqual(sync.run_due(now=self.now), [{"status": "completed", "sources": 1}])
        self.assertNotIn("/user", self.paths)

    def test_factory_rejects_mismatched_app_and_unknown_reader_module(self):
        from pullwise_server.github_ingestion import build_github_fact_sync
        with self.assertRaisesRegex(ValueError, "GITHUB_APP_BINDING_MISMATCH"):
            build_github_fact_sync(self.store, credentials=self.credentials(), get_json=self.transport,
                processing_budget=Mock(), app_id="other", webhook_secret="fixture")
        sync = build_github_fact_sync(self.store, credentials=self.credentials(), get_json=self.transport,
            processing_budget=Mock(), app_id="7", webhook_secret="fixture")
        with self.assertRaisesRegex(ValueError, "GITHUB_MODULE_UNSUPPORTED"):
            sync.read_page(target={"module": "issues"}, cursor=None, high_watermark=None, event=None)
        self.assertEqual(self.paths, [])

    def test_factory_routes_pr_and_ci_facts_without_permission_refresh_or_analysis(self):
        from pullwise_server.github_ingestion import build_github_fact_sync
        from pullwise_server.github_transport import GitHubResponse
        from pullwise_server.github_ci_logs import CILogResult
        credentials = self.credentials()
        credentials.installation_access_for_user = lambda account, installation: {
            "githubIdentityId": "identity", "githubAppInstallationId": installation, "canAccess": True}
        credentials.installation_token = lambda installation: {"token": "install-fixture", "expires_at": self.now + 3600}
        self.store.put_repository_service(repository_id="github:123", installation_id="11", billing_owner_id="usr_1",
            expected_revision=0, enabled=True, modules={"pr": True, "ci": True},
            analysis_enabled={"pr": False, "ci": False}, allow_member_sync=False,
            default_assignee_id=None, priority_order=0)
        run = {"id": 7, "run_attempt": 1, "repository": {"id": 123}, "workflow_id": 3,
               "head_sha": "a" * 40, "status": "completed", "conclusion": "failure",
               "updated_at": "2026-09-22T01:00:00Z", "pull_requests": []}
        pull = {"id": 700, "number": 7, "state": "open", "title": "Fix", "body": "Description",
                "updated_at": "2026-09-22T01:00:00Z", "created_at": "2026-09-22T00:00:00Z",
                "merged_at": None, "draft": False, "user": {"id": 7, "login": "author"},
                "head": {"sha": "a" * 40}, "base": {"repo": {"id": 123}},
                "requested_reviewers": [], "requested_teams": [], "html_url": "https://github.com/acme/lib/pull/7"}
        def get_json(path, *, token):
            self.paths.append(path)
            self.assertEqual(token, "install-fixture")
            route = path.split("?")[0]
            if route in {"/repositories/123", "/repos/acme/lib"}:
                payload = {"id": 123, "private": False, "full_name": "acme/lib"}
            elif route == "/repos/acme/lib/pulls":
                payload = [pull]
            elif route == "/repos/acme/lib/actions/runs":
                payload = {"workflow_runs": [run], "total_count": 1}
            elif route == "/repos/acme/lib/actions/runs/7/attempts/1":
                payload = run
            elif route == "/repos/acme/lib/actions/runs/7/attempts/1/jobs":
                payload = {"jobs": [{"id": 9, "run_id": 7, "name": "test", "status": "completed",
                    "conclusion": "failure", "completed_at": "2026-09-22T01:00:00Z", "steps": []}], "total_count": 1}
            else:
                raise AssertionError("Unexpected fixture route")
            return GitHubResponse(200, payload)
        log_reader = Mock(return_value=CILogResult(text="failure detail\n", coverage="complete"))
        sync = build_github_fact_sync(self.store, credentials=credentials, get_json=get_json,
            ci_log_reader=log_reader,
            processing_budget=Mock(side_effect=AssertionError("manual reads do not resolve budgets")),
            app_id="7", webhook_secret="fixture")
        for module in ("pr", "ci"):
            key = self.store.set_discovery_authorization(resource_kind="repository", resource_id="github:123",
                module=module, github_repository_id="123", installation_id="11", app_id="7",
                authorization_revision=1, accessible=True, valid_until=self.now + 300, observed_at=self.now)
            self.assertEqual(sync.run_manual(key, now=self.now), {"status": "completed", "sources": 1})
        with closing(self.store.connect()) as connection:
            self.assertEqual({row[0] for row in connection.execute("SELECT source_type FROM source_records")}, {"pr_state", "ci_failure"})
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM github_run_states").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0], 0)
            content = connection.execute("SELECT v.content_json FROM source_versions v JOIN source_records s ON s.latest_version=v.id WHERE s.source_type='ci_failure'").fetchone()[0]
            self.assertIn("failure detail", content)
        log_reader.assert_called_once()
        credentials.app_token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
