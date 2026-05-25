from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pullwise_server import app, checkout, db, worker


class WorkspaceIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        with app.PREVIEW_SCAN_LOCKS_GUARD:
            app.PREVIEW_SCAN_LOCKS.clear()

    def test_checkout_paths_are_namespaced_by_user_and_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                first = checkout.checkout_path_for("usr_1", "sc_shared", "owner/repo")
                second = checkout.checkout_path_for("usr_2", "sc_shared", "owner/repo")

        self.assertNotEqual(first, second)
        self.assertIn(os.path.join("usr_1", "sc_shared"), first)
        self.assertIn(os.path.join("usr_2", "sc_shared"), second)

    def test_worker_does_not_reuse_repo_path_from_another_user_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                foreign_path = checkout.checkout_path_for("usr_2", "sc_1", "owner/repo")
                expected_path = checkout.checkout_path_for("usr_1", "sc_1", "owner/repo")
                snapshot = {
                    "userId": "usr_1",
                    "repo": "owner/repo",
                    "branch": "main",
                    "commit": "pending",
                    "repoPath": foreign_path,
                }

                with (
                    patch.object(worker.review, "provider_requires_checkout", return_value=True),
                    patch.object(worker.checkout, "prepare_checkout", return_value=expected_path) as prepare_checkout,
                ):
                    repo_path = worker._prepare_checkout_if_needed("sc_1", snapshot)

        self.assertEqual(repo_path, expected_path)
        prepare_checkout.assert_called_once()

    def test_worker_cleanup_waits_for_fix_preview_scan_lock(self) -> None:
        cleanup_called = threading.Event()
        cleanup_finished = threading.Event()
        snapshot = {
            "id": "sc_1",
            "userId": "usr_1",
            "repo": "owner/repo",
            "branch": "main",
            "commit": "pending",
            "repoPath": "checkout",
        }

        def cleanup_workspace(_user_id: str, _scan_id: str) -> None:
            cleanup_called.set()

        with (
            patch.object(worker.checkout, "cleanup_scan_workspace", side_effect=cleanup_workspace),
            patch.object(worker, "_patch_scan") as patch_scan,
            patch.object(worker, "_log_scan_event"),
        ):
            with app.preview_scan_lock("sc_1"):
                cleanup_thread = threading.Thread(
                    target=lambda: (worker._cleanup_checkout_workspace("sc_1", snapshot), cleanup_finished.set())
                )
                cleanup_thread.start()
                self.assertFalse(cleanup_called.wait(0.05))

            self.assertTrue(cleanup_called.wait(1))
            cleanup_thread.join(1)

        self.assertFalse(cleanup_thread.is_alive())
        self.assertTrue(cleanup_finished.is_set())
        patch_scan.assert_called_once_with("sc_1", {"repoPath": None}, allow_after_cancel=True)
        self.assertNotIn("sc_1", app.PREVIEW_SCAN_LOCKS)


class RepositoryRiskDecisionTest(unittest.TestCase):
    def test_matching_source_fingerprint_in_same_workspace_is_limited_trial(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database_path = os.path.join(tmpdir, "pullwise.sqlite3")
            with patch.dict(os.environ, {"PULLWISE_DB_PATH": database_path}, clear=False):
                workspace = db.upsert_workspace(
                    {
                        "id": "ws_1",
                        "name": "acme",
                        "github_app_installation_id": "111",
                    }
                )
                first_repo = db.upsert_repository(
                    {
                        "id": "repo_1",
                        "github_repo_id": "1",
                        "full_name": "acme/first",
                    }
                )
                second_repo = db.upsert_repository(
                    {
                        "id": "repo_2",
                        "github_repo_id": "2",
                        "full_name": "acme/second",
                    }
                )
                db.upsert_workspace_repository(workspace["id"], first_repo["id"])
                db.upsert_workspace_repository(workspace["id"], second_repo["id"])
                db.upsert_repo_fingerprint(
                    first_repo["id"],
                    {
                        "defaultBranch": "main",
                        "headSha": "a" * 40,
                        "treeSha": "b" * 40,
                        "sourceFingerprint": "same-source",
                    },
                )

                decision = worker._repo_risk_decision(
                    {"workspaceId": workspace["id"], "repoId": second_repo["id"]},
                    {
                        "defaultBranch": "main",
                        "headSha": "c" * 40,
                        "treeSha": "d" * 40,
                        "sourceFingerprint": "same-source",
                    },
                )
                stored = db.get_repo_fingerprint(second_repo["id"])

        self.assertEqual(decision["decision"], "allow_limited_trial")
        self.assertEqual(decision["matchedRepositoryId"], first_repo["id"])
        self.assertEqual(stored["source_fingerprint"], "same-source")


class ScanQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.persist_patcher = patch.object(app, "persist_state")
        self.persist_patcher.start()
        self.addCleanup(self.persist_patcher.stop)
        app.STATE_LOADED = True
        app.STATE_DIRTY = False
        app.ISSUES = []
        app.SCANS = [
            {
                "id": "sc_1",
                "userId": "usr_1",
                "repo": "owner/repo",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "createdAt": 100,
            },
            {
                "id": "sc_2",
                "userId": "usr_2",
                "repo": "owner/repo",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "createdAt": 101,
            },
        ]

    def test_start_scan_uses_fixed_worker_pool_instead_of_one_thread_per_scan(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_MAX_CONCURRENT_SCANS": "3",
                    "PULLWISE_MAX_CONCURRENT_SCANS_PER_USER": "1",
                },
                clear=False,
            ),
            patch.object(worker, "_WORKER_THREADS", []),
            patch.object(worker, "_WORKER_CONFIG", None),
            patch("pullwise_server.worker.threading.Thread") as thread_class,
        ):
            for index in range(100):
                worker.start_scan(f"sc_{index}")

        self.assertEqual(thread_class.call_count, 3)
        self.assertEqual(thread_class.return_value.start.call_count, 3)

    def test_queue_claim_skips_user_at_per_user_limit_without_blocking_other_users(self) -> None:
        app.SCANS.insert(
            0,
            {
                "id": "sc_running",
                "userId": "usr_1",
                "repo": "owner/repo",
                "branch": "main",
                "commit": "pending",
                "status": "running",
                "createdAt": 99,
            },
        )

        with (
            patch.dict(
                os.environ,
                {
                    "PULLWISE_MAX_CONCURRENT_SCANS": "3",
                    "PULLWISE_MAX_CONCURRENT_SCANS_PER_USER": "1",
                },
                clear=False,
            ),
            patch.object(app, "persist_state"),
        ):
            claimed = worker._claim_next_scan()

        self.assertEqual(claimed["id"], "sc_2")
        self.assertEqual(app.SCANS[1]["status"], "queued")

    def test_queue_claim_sanitizes_legacy_scan_metadata_snapshot(self) -> None:
        app.SCANS = [
            {
                "id": "sc_dirty",
                "userId": "usr_1",
                "repo": "owner/repo",
                "branch": "main\r\nX-Injected: bad",
                "commit": {"sha": "abc1234"},
                "installationId": 123,
                "cloneUrl": "https://github.com/owner/repo.git\r\nX-Injected: bad",
                "status": "queued",
                "createdAt": 100,
            }
        ]

        with patch.object(app, "persist_state"):
            snapshot = worker._claim_next_scan()

        self.assertEqual(snapshot["branch"], "main")
        self.assertEqual(snapshot["commit"], "pending")
        self.assertEqual(snapshot["installationId"], "123")
        self.assertIsNone(snapshot["cloneUrl"])

    def test_queued_scan_payload_reports_global_position_and_reason(self) -> None:
        app.SCANS.insert(
            0,
            {
                "id": "sc_running",
                "userId": "usr_1",
                "repo": "owner/repo",
                "branch": "main",
                "commit": "pending",
                "status": "running",
                "createdAt": 99,
            },
        )

        with patch.dict(
            os.environ,
            {
                "PULLWISE_MAX_CONCURRENT_SCANS": "3",
                "PULLWISE_MAX_CONCURRENT_SCANS_PER_USER": "1",
            },
            clear=False,
        ):
            payload = app.scan_payload(app.SCANS[1])

        self.assertEqual(payload["queue"]["position"], 1)
        self.assertEqual(payload["queue"]["ahead"], 0)
        self.assertEqual(payload["queue"]["reason"], "user_limit")
        self.assertEqual(payload["queue"]["limits"]["perUser"], 1)
        self.assertIn("already have 1 scan running", payload["queue"]["message"])

    def test_later_queued_scan_reports_how_many_scans_are_ahead(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PULLWISE_MAX_CONCURRENT_SCANS": "1",
                "PULLWISE_MAX_CONCURRENT_SCANS_PER_USER": "1",
            },
            clear=False,
        ):
            payload = app.scan_payload(app.SCANS[1])

        self.assertEqual(payload["queue"]["position"], 2)
        self.assertEqual(payload["queue"]["ahead"], 1)
        self.assertEqual(payload["queue"]["reason"], "waiting_for_turn")
        self.assertIn("1 scan ahead", payload["queue"]["message"])

    def test_execute_scan_cleans_checkout_workspace_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                repo_path = checkout.checkout_path_for("usr_1", "sc_success", "owner/repo")
                os.makedirs(repo_path, exist_ok=True)
                workspace = checkout.workspace_path_for("usr_1", "sc_success")
                app.SCANS = [self._running_scan("sc_success")]

                with (
                    patch("pullwise_server.worker.time.sleep"),
                    patch.object(worker.review, "provider_requires_checkout", return_value=True),
                    patch.object(worker.checkout, "prepare_checkout", return_value=repo_path),
                    patch.object(worker.checkout, "current_commit", return_value="a" * 40),
                    patch.object(worker.review, "run_review", return_value=[]) as run_review,
                ):
                    worker._execute_scan("sc_success", self._snapshot("sc_success"), 100)

                self.assertFalse(os.path.exists(workspace))

        self.assertEqual(app.SCANS[0]["status"], "done")
        self.assertEqual(app.SCANS[0]["commit"], "a" * 40)
        self.assertIsNone(app.SCANS[0].get("repoPath"))
        self.assertEqual(run_review.call_args.kwargs["commit"], "a" * 40)

    def test_execute_scan_cleans_checkout_workspace_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                repo_path = checkout.checkout_path_for("usr_1", "sc_failure", "owner/repo")
                os.makedirs(repo_path, exist_ok=True)
                workspace = checkout.workspace_path_for("usr_1", "sc_failure")
                app.SCANS = [self._running_scan("sc_failure")]

                with (
                    patch("pullwise_server.worker.time.sleep"),
                    patch("pullwise_server.worker.traceback.print_exc"),
                    patch.object(worker.review, "provider_requires_checkout", return_value=True),
                    patch.object(worker.checkout, "prepare_checkout", return_value=repo_path),
                    patch.object(worker.checkout, "current_commit", return_value="b" * 40),
                    patch.object(worker.review, "run_review", side_effect=RuntimeError("ai failed")),
                ):
                    worker._execute_scan("sc_failure", self._snapshot("sc_failure"), 100)

                self.assertFalse(os.path.exists(workspace))

        self.assertEqual(app.SCANS[0]["status"], "failed")
        self.assertIn("ai failed", app.SCANS[0]["error"])
        self.assertIsNone(app.SCANS[0].get("repoPath"))

    def test_execute_scan_cleans_checkout_workspace_after_cancellation(self) -> None:
        def cancel_checkout(*_args: object, **_kwargs: object) -> str:
            app.SCANS[0]["status"] = "cancelled"
            raise checkout.CheckoutCancelled()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"PULLWISE_CHECKOUT_ROOT": tmpdir}, clear=False):
                repo_path = checkout.checkout_path_for("usr_1", "sc_cancelled", "owner/repo")
                os.makedirs(repo_path, exist_ok=True)
                workspace = checkout.workspace_path_for("usr_1", "sc_cancelled")
                app.SCANS = [self._running_scan("sc_cancelled")]

                with (
                    patch("pullwise_server.worker.time.sleep"),
                    patch.object(worker.review, "provider_requires_checkout", return_value=True),
                    patch.object(worker.checkout, "prepare_checkout", side_effect=cancel_checkout),
                ):
                    worker._execute_scan("sc_cancelled", self._snapshot("sc_cancelled"), 100)

                self.assertFalse(os.path.exists(workspace))

        self.assertEqual(app.SCANS[0]["status"], "cancelled")
        self.assertIsNone(app.SCANS[0].get("repoPath"))

    def _running_scan(self, scan_id: str) -> dict:
        return {
            "id": scan_id,
            "userId": "usr_1",
            "repo": "owner/repo",
            "branch": "main",
            "commit": "pending",
            "status": "running",
            "createdAt": 100,
            "startedAt": 100,
            "repoPath": None,
        }

    def _snapshot(self, scan_id: str) -> dict:
        return {
            "id": scan_id,
            "userId": "usr_1",
            "repo": "owner/repo",
            "branch": "main",
            "commit": "pending",
            "installationId": "123",
            "cloneUrl": "https://github.com/owner/repo.git",
            "repoPath": None,
            "startedAt": 100,
        }


if __name__ == "__main__":
    unittest.main()
