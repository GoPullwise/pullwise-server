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
