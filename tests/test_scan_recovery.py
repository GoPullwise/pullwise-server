from __future__ import annotations

import unittest
import os
import tempfile
from unittest.mock import patch

from pullwise_server import app, db


class ScanRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env = patch.dict(
            os.environ,
            {"PULLWISE_DB_PATH": os.path.join(self.temp_dir.name, "pullwise.sqlite3")},
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.persist_patcher = patch.object(app, "persist_state")
        self.persist_state = self.persist_patcher.start()
        self.addCleanup(self.persist_patcher.stop)
        app.STATE_LOADED = True
        app.STATE_DIRTY = False
        app.ISSUES = []

    def test_recover_interrupted_scans_requeues_running_scans(self) -> None:
        app.SCANS = [
            {
                "id": "sc_1",
                "status": "running",
                "progress": 44,
                "phase": "ai",
                "createdAt": app.now() - 60,
            },
        ]

        recovered = app.recover_interrupted_scans()

        self.assertEqual(recovered, 1)
        self.assertEqual(app.SCANS[0]["status"], "queued")
        self.assertEqual(app.SCANS[0]["progress"], 0)
        self.assertIsNone(app.SCANS[0]["phase"])
        self.assertIn("recoveredAt", app.SCANS[0])
        self.assertEqual(app.SCANS[0]["recoveryReason"], "server_restart")
        self.persist_state.assert_called_once()

    def test_recover_interrupted_scans_requeues_matching_unexpired_job(self) -> None:
        timestamp = app.now()
        app.SCANS = [
            {
                "id": "sc_unexpired",
                "status": "running",
                "progress": 44,
                "phase": "ai",
                "claimedAt": timestamp,
                "claimedByWorkerId": "wk_1",
                "createdAt": timestamp - 10,
                "queuedAt": timestamp - 10,
            },
        ]
        job = db.create_scan_job(
            {
                "job_id": "job_unexpired",
                "scan_id": "sc_unexpired",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "created_at": timestamp - 10,
                "user_id": "usr_1",
                "max_attempts": 3,
            }
        )
        db.claim_next_scan_jobs("wk_1", max_jobs=1, lease_seconds=3600, timestamp=timestamp)

        with patch("pullwise_server.app.now", return_value=timestamp + 30):
            recovered = app.recover_interrupted_scans()
        stored = db.get_scan_job(job["job_id"])

        self.assertEqual(recovered, 1)
        self.assertEqual(stored["status"], "queued")
        self.assertEqual(stored["error"], "server_restart")
        self.assertIsNone(stored["claimed_by_worker_id"])
        self.assertIsNone(stored["timeout_at"])
        self.assertEqual(app.SCANS[0]["status"], "queued")
        self.assertEqual(app.SCANS[0]["recoveryReason"], "server_restart")

    def test_recover_interrupted_scans_reconciles_completed_job_result_before_requeue(self) -> None:
        timestamp = app.now()
        app.SCANS = [
            {
                "id": "sc_done_in_db",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "running",
                "progress": 80,
                "phase": "ai",
                "claimedAt": timestamp,
                "claimedByWorkerId": "wk_1",
                "jobId": "job_done_in_db",
                "createdAt": timestamp - 30,
                "queuedAt": timestamp - 30,
            },
        ]
        job = db.create_scan_job(
            {
                "job_id": "job_done_in_db",
                "scan_id": "sc_done_in_db",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "created_at": timestamp - 30,
                "user_id": "usr_1",
            }
        )
        claimed = db.claim_next_scan_jobs("wk_1", max_jobs=1, lease_seconds=3600, timestamp=timestamp)[0]
        db.record_scan_job_result(
            job["job_id"],
            attempt_id=f"wk_1-{claimed['attempt']}",
            status="done",
            result_checksum="checksum-done",
            payload={
                "status": "done",
                "attempt_id": f"wk_1-{claimed['attempt']}",
                "result_checksum": "checksum-done",
                "findings": [{"severity": "high", "title": "Recovered finding"}],
                "summary": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
                "duration_ms": 123,
            },
        )

        recovered = app.recover_interrupted_scans()

        self.assertEqual(recovered, 1)
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "done")
        self.assertEqual(app.SCANS[0]["status"], "done")
        self.assertEqual(app.SCANS[0]["progress"], 100)
        self.assertEqual(app.SCANS[0]["issues"]["high"], 1)
        self.assertEqual(len(app.ISSUES), 1)
        self.assertEqual(app.ISSUES[0]["title"], "Recovered finding")
        self.persist_state.assert_called_once()

    def test_recover_interrupted_scans_leaves_terminal_scans_unchanged(self) -> None:
        app.SCANS = [
            {"id": "sc_done", "status": "done", "progress": 100, "phase": "report"},
            {"id": "sc_failed", "status": "failed", "progress": 80, "phase": "ai"},
            {"id": "sc_cancelled", "status": "cancelled", "progress": 0, "phase": None},
        ]
        original = [dict(scan) for scan in app.SCANS]

        recovered = app.recover_interrupted_scans()

        self.assertEqual(recovered, 0)
        self.assertEqual(app.SCANS, original)
        self.persist_state.assert_not_called()

    def test_recover_interrupted_scans_requeues_timed_out_job_and_scan(self) -> None:
        timestamp = app.now()
        app.SCANS = [
            {
                "id": "sc_job_timeout",
                "status": "running",
                "progress": 70,
                "phase": "ai",
                "claimedAt": timestamp - 120,
                "claimedByWorkerId": "wk_1",
                "createdAt": timestamp - 180,
                "queuedAt": timestamp - 180,
            },
        ]
        job = db.create_scan_job(
            {
                "job_id": "job_timeout",
                "scan_id": "sc_job_timeout",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "created_at": timestamp - 180,
                "user_id": "usr_1",
                "max_attempts": 3,
            }
        )
        db.claim_next_scan_jobs("wk_1", max_jobs=1, lease_seconds=60, timestamp=timestamp - 120)

        recovered = app.recover_interrupted_scans()
        stored = db.get_scan_job(job["job_id"])

        self.assertEqual(recovered, 1)
        self.assertEqual(stored["status"], "queued")
        self.assertEqual(stored["attempt"], 1)
        self.assertEqual(app.SCANS[0]["status"], "queued")
        self.assertEqual(app.SCANS[0]["progress"], 0)
        self.assertIsNone(app.SCANS[0]["phase"])
        self.assertIsNone(app.SCANS[0]["claimedAt"])
        self.assertIsNone(app.SCANS[0]["claimedByWorkerId"])

    def test_recover_interrupted_scans_requeues_job_when_worker_heartbeat_times_out(self) -> None:
        timestamp = app.now()
        with patch.dict(os.environ, {"PULLWISE_WORKER_HEARTBEAT_TIMEOUT_SECONDS": "60"}, clear=False):
            db.upsert_worker_heartbeat(
                {
                    "worker_id": "wk_stale",
                    "provider": "codex",
                    "max_concurrent_jobs": 1,
                    "running_jobs": 1,
                    "free_slots": 0,
                    "timestamp": timestamp,
                }
            )
            app.SCANS = [
                {
                    "id": "sc_stale_worker",
                    "status": "running",
                    "progress": 35,
                    "phase": "ai",
                    "claimedAt": timestamp + 1,
                    "claimedByWorkerId": "wk_stale",
                    "createdAt": timestamp,
                    "queuedAt": timestamp,
                },
            ]
            job = db.create_scan_job(
                {
                    "job_id": "job_stale_worker",
                    "scan_id": "sc_stale_worker",
                    "repo": "acme/api",
                    "branch": "main",
                    "commit": "pending",
                    "status": "queued",
                    "created_at": timestamp,
                    "user_id": "usr_1",
                    "max_attempts": 3,
                }
            )
            db.claim_next_scan_jobs("wk_stale", max_jobs=1, lease_seconds=3600, timestamp=timestamp + 1)

            with patch("pullwise_server.app.now", return_value=timestamp + 121):
                recovered = app.recover_interrupted_scans()
            stored = db.get_scan_job(job["job_id"])

        self.assertEqual(recovered, 1)
        self.assertEqual(stored["status"], "queued")
        self.assertEqual(stored["error"], "worker_heartbeat_timed_out")
        self.assertEqual(app.SCANS[0]["status"], "queued")
        self.assertEqual(app.SCANS[0]["progress"], 0)
        self.assertIsNone(app.SCANS[0]["phase"])
        self.assertIsNone(app.SCANS[0]["claimedAt"])
        self.assertIsNone(app.SCANS[0]["claimedByWorkerId"])
        self.assertEqual(app.SCANS[0]["recoveryReason"], "worker_heartbeat_timed_out")
