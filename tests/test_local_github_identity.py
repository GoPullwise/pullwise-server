from __future__ import annotations

import tempfile
import unittest
from unittest.mock import Mock, patch

from pullwise_server.github_local_identity import repository_for_target
from pullwise_server.github_transport import GitHubResponse, GitHubUnavailable
from pullwise_server.product_store import ProductStore
from pullwise_server.github_local_runtime import build_local_fact_sync


class LocalGitHubIdentityTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ProductStore(self.directory.name + "/product.sqlite3")
        self.store.initialize()
        self.account = {"id": "owner", "githubId": "7", "githubLogin": "owner",
                        "githubRepositoryAccess": {
                            "mode": "github-app", "authorizedUserId": "owner",
                            "authorizedGithubId": "7", "authorizedGithubLogin": "owner",
                            "repositoriesNeedSync": False,
                            "repositoryItems": [{"githubRepoId": "123", "fullName": "org/repo",
                                                 "installationId": "11"}]}}

    def test_repository_service_requires_saved_id_name_and_installation(self):
        target = {"module": "pr", "resource_kind": "repository", "github_repository_id": "123",
                  "installation_id": "11", "billing_owner_id": "owner"}
        self.assertEqual(repository_for_target(self.store, self.account, "123", target),
                         {"id": "123", "fullName": "org/repo"})
        for change in ({"installation_id": "12"}, {"github_repository_id": "124"}):
            with self.assertRaises(GitHubUnavailable):
                repository_for_target(self.store, self.account, "123", {**target, **change})
        self.account["githubRepositoryAccess"]["repositoriesNeedSync"] = True
        with self.assertRaises(GitHubUnavailable):
            repository_for_target(self.store, self.account, "123", target)
        self.account["githubRepositoryAccess"]["repositoriesNeedSync"] = False
        self.account["githubRepositoryAccessPending"] = {"installationId": "11"}
        with self.assertRaises(GitHubUnavailable):
            repository_for_target(self.store, self.account, "123", target)
        del self.account["githubRepositoryAccessPending"]
        self.account["githubRepositoryAccess"]["repositoryItems"].append(
            {"githubRepoId": "123", "fullName": "org/renamed", "installationId": "11"})
        with self.assertRaises(GitHubUnavailable):
            repository_for_target(self.store, self.account, "123", target)

    def test_updates_requires_saved_watch_identity_and_rejects_null_name(self):
        watch = self.store.create_watch(owner_id="owner", target_repository_id=None,
            upstream_repository_id="github:456", upstream_full_name="upstream/lib",
            billing_owner_id="owner", interests=["security"], enabled=True,
            analysis_enabled=False)
        target = {"module": "updates", "resource_kind": "watch", "resource_id": watch["id"],
                  "owner_id": "owner", "billing_owner_id": "owner", "github_repository_id": "456"}
        self.assertEqual(repository_for_target(self.store, self.account, "456", target),
                         {"id": "456", "fullName": "upstream/lib"})
        reopened = ProductStore(self.store.database_path)
        self.assertEqual(repository_for_target(reopened, self.account, "456", target)["fullName"],
                         "upstream/lib")
        self.account["githubRepositoryAccess"]["repositoryItems"] = [
            {"githubRepoId": "456", "fullName": "owned/lib", "installationId": "11"}]
        shared = {**target, "target_repository_id": "github:456", "target_installation_id": "11"}
        self.assertEqual(repository_for_target(self.store, self.account, "456", shared, "upstream")["fullName"],
                         "upstream/lib")
        self.assertEqual(repository_for_target(self.store, self.account, "456", shared, "target")["fullName"],
                         "owned/lib")
        with self.assertRaises(GitHubUnavailable):
            repository_for_target(self.store, self.account, "456", {**target, "owner_id": "other"})
        with self.assertRaises(GitHubUnavailable):
            repository_for_target(self.store, self.account, "456", {**target, "resource_id": "missing"})
        old = self.store.create_watch(owner_id="owner", target_repository_id=None,
            upstream_repository_id="github:457", upstream_full_name=None,
            billing_owner_id="owner", interests=["security"], enabled=True,
            analysis_enabled=False)
        with self.assertRaises(GitHubUnavailable):
            repository_for_target(self.store, self.account, "457", {**target,
                "resource_id": old["id"], "github_repository_id": "457"})

    def test_local_runtime_uses_account_snapshot_and_disables_model_admission(self):
        account = {**self.account, "githubAccessToken": "user-token"}
        account["githubIdentityInstallationAccess"] = [{"githubIdentityId": "identity",
            "githubAppInstallationId": "11", "canAccess": True}]
        identity = {"id": "identity", "userId": "owner", "githubUserId": "7",
                    "accessToken": "user-token", "status": "active"}
        graphql_calls = []
        def query_json(document, *, variables, token):
            graphql_calls.append((document, variables, token))
            field = "latestOpinionatedReviews" if "latestOpinionatedReviews" in document else "reviewThreads"
            return GitHubResponse(200, {"data": {"repository": {"databaseId": 123,
                "pullRequest": {"number": 7, field: {"nodes": [],
                    "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}})
        sync = build_local_fact_sync(self.store, account_snapshot=lambda owner: account,
            identities_for_user=lambda user: [identity],
            installation_access_for_user=lambda user, installation:
                user["githubIdentityInstallationAccess"][0],
            app_id="30", webhook_secret="fixture", app_token=lambda: "app-token",
            installation_token=lambda installation: {"token": "install-token", "expires_at": 9999999999},
            clock=lambda: 100, get_json=lambda *args, **kwargs: None,
            query_json=query_json)
        self.assertFalse(sync.analysis_admission_enabled)
        pr_reader = sync.read_page.__self__.readers["pr"]
        ci_reader = sync.read_page.__self__.readers["ci"]
        from pullwise_server.github_ci_logs import GitHubCILogReader
        from pullwise_server.github_ci_transport import GitHubCILogTransport
        self.assertIsInstance(ci_reader.log_reader, GitHubCILogReader)
        self.assertIsInstance(ci_reader.log_reader.request, GitHubCILogTransport)
        self.assertEqual(pr_reader.thread_reader.read(owner="org", name="repo",
            github_repository_id="123", pull_number=7, token="fixture").coverage, "complete")
        self.assertEqual(pr_reader.review_reader.read(owner="org", name="repo",
            github_repository_id="123", pull_number=7, token="fixture").coverage, "complete")
        self.assertEqual(len(graphql_calls), 2)
        self.assertEqual(sync.credentials.repository_for_account(account, "123", {
            "module": "pr", "resource_kind": "repository", "github_repository_id": "123",
            "installation_id": "11", "billing_owner_id": "owner"}, "upstream"),
            {"id": "123", "fullName": "org/repo"})
        with self.assertRaisesRegex(ValueError, "GITHUB_FACT_SYNC_UNCONFIGURED"):
            build_local_fact_sync(self.store, account_snapshot=lambda owner: account,
                identities_for_user=lambda user: [identity],
                installation_access_for_user=lambda user, installation: None,
                app_id="030", webhook_secret="fixture", app_token=lambda: "app-token",
                installation_token=lambda installation: None)

    def test_default_main_attaches_fact_worker_only_with_complete_configuration(self):
        from pullwise_server import _app_part_10_handler_main as entry
        server = Mock()
        with patch.object(entry, "load_env_file"), patch.object(entry, "ensure_state_loaded"), \
             patch.object(entry, "persist_state"), patch.object(entry.logging_config, "configure_logging"), \
             patch.object(entry, "PullwiseThreadingHTTPServer", return_value=server) as server_type, \
             patch.object(entry.db, "database_path", return_value=self.store.database_path), \
             patch.object(entry.github_auth, "app_id", return_value="30"), \
             patch.object(entry.github_auth, "app_api_configured", return_value=True), \
             patch.dict(entry.os.environ, {"PULLWISE_GITHUB_WEBHOOK_SECRET": "fixture"}), \
             patch("sys.argv", ["pullwise-server", "--host", "127.0.0.1", "--port", "8099"]):
            entry.main()
        sync = server_type.call_args.kwargs["fact_sync"]
        self.assertFalse(sync.analysis_admission_enabled)
        server.serve_forever.assert_called_once_with()
        server.server_close.assert_called_once_with()

    def test_default_main_leaves_fact_worker_unavailable_without_configuration(self):
        from pullwise_server import _app_part_10_handler_main as entry
        with patch.object(entry, "load_env_file"), patch.object(entry, "ensure_state_loaded"), \
             patch.object(entry, "persist_state"), patch.object(entry.logging_config, "configure_logging"), \
             patch.object(entry, "PullwiseThreadingHTTPServer") as server_type, \
             patch.object(entry.github_auth, "app_id", return_value=""), \
             patch("sys.argv", ["pullwise-server", "--host", "127.0.0.1", "--port", "8099"]):
            entry.main()
        self.assertNotIn("fact_sync", server_type.call_args.kwargs)

    def test_seed_missing_discovery_targets_without_replacing_existing_proof(self):
        now = 1_800_000_000
        self.store.put_repository_service(repository_id="github:123", installation_id="11",
            billing_owner_id="owner", expected_revision=0, enabled=True,
            modules={"pr": True, "ci": True}, analysis_enabled={"pr": False, "ci": False},
            allow_member_sync=False, default_assignee_id=None, priority_order=0)
        self.store.create_watch(owner_id="owner", target_repository_id=None,
            upstream_repository_id="github:456", upstream_full_name="upstream/lib",
            billing_owner_id="owner", interests=["security"], enabled=True,
            analysis_enabled=False)
        self.assertEqual(self.store.seed_discovery_targets(app_id="30", now=now), 3)
        with self.store._read() as connection:
            rows = connection.execute("SELECT module, accessible, valid_until FROM discovery_targets ORDER BY module").fetchall()
        self.assertEqual([(row["module"], row["accessible"], row["valid_until"]) for row in rows],
                         [("ci", 0, now), ("pr", 0, now), ("updates", 0, now)])
        self.assertEqual(self.store.seed_discovery_targets(app_id="30", now=now + 1), 0)
        with self.store._read() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM discovery_targets WHERE valid_until=?",
                (now,)).fetchone()[0], 3)
        from pullwise_server.product_discovery import ProductFactSync
        reader = Mock(side_effect=AssertionError("inaccessible targets cannot read GitHub"))
        sync = ProductFactSync(self.store, read_page=reader,
            processing_budget=Mock(), app_id="30", webhook_secret="fixture")
        self.assertEqual(sync.run_due(now=now), [])
        reader.assert_not_called()

    def test_saved_installation_revocation_and_expired_proof_block_read_token(self):
        now = 1_800_000_000
        self.store.put_repository_service(repository_id="github:123", installation_id="11",
            billing_owner_id="owner", expected_revision=0, enabled=True,
            modules={"pr": True, "ci": False}, analysis_enabled={"pr": False, "ci": False},
            allow_member_sync=False, default_assignee_id=None, priority_order=0)
        key = self.store.set_discovery_authorization(resource_kind="repository",
            resource_id="github:123", module="pr", github_repository_id="123",
            installation_id="11", app_id="30", authorization_revision=1,
            accessible=True, valid_until=now + 300, observed_at=now)
        account = self.account
        account["githubIdentityInstallationAccess"] = [{"githubIdentityId": "identity",
            "githubAppInstallationId": "11", "canAccess": True}]
        identity = {"id": "identity", "userId": "owner", "githubUserId": "7",
                    "accessToken": "user-token", "status": "active"}
        minted = Mock(return_value={"token": "install-token", "expires_at": now + 600})
        clock = [now]
        sync = build_local_fact_sync(self.store, account_snapshot=lambda owner: account,
            identities_for_user=lambda user: [identity],
            installation_access_for_user=lambda user, installation:
                user["githubIdentityInstallationAccess"][0],
            app_id="30", webhook_secret="fixture", app_token=lambda: "app-token",
            installation_token=minted, clock=lambda: clock[0],
            get_json=lambda *args, **kwargs: None)
        target = self.store.discovery_target(key)
        self.assertEqual(sync.credentials.token_for_target(target), "install-token")
        account["githubIdentityInstallationAccess"][0]["canAccess"] = False
        with self.assertRaises(GitHubUnavailable):
            sync.credentials.token_for_target(target)
        account["githubIdentityInstallationAccess"][0]["canAccess"] = True
        clock[0] = now + 300
        with self.assertRaises(GitHubUnavailable):
            sync.credentials.token_for_target(target)
        self.assertEqual(minted.call_count, 1)

    def test_worker_seeds_before_authorization_tick(self):
        from pullwise_server.product_discovery import ProductFactSync
        order = []
        sync = ProductFactSync(self.store, read_page=Mock(), processing_budget=Mock(),
            app_id="30", webhook_secret="fixture",
            seed_targets=lambda *, now: order.append("seed"))
        sync.run_authorization_due = lambda *, now: order.append("authorize")
        sync.run_events = lambda *, now: order.append("events")
        sync.run_due = lambda *, now: order.append("discover")

        class Stop:
            stopped = False
            def is_set(self):
                return self.stopped
            def wait(self, seconds):
                self.stopped = True

        sync.run_forever(Stop())
        self.assertEqual(order, ["seed", "authorize", "events", "discover"])


if __name__ == "__main__":
    unittest.main()
