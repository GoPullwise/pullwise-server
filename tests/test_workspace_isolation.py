from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from pullwise_server import app, checkout, worker


class WorkspaceIsolationTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
