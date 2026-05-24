from __future__ import annotations

import unittest
from unittest.mock import patch

from pullwise_server import app


class ScanRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
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
