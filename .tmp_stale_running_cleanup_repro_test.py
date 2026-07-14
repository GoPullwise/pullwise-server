from __future__ import annotations

import os
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app, db
from tests.test_worker_admin_routes import RouteHarness, WorkerAdminRoutesTest


class StaleRunningCleanupReproTest(WorkerAdminRoutesTest):
    def test_stale_running_uninstall_is_soft_deleted_after_timeout(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        self.assertEqual(self.post_v1_heartbeat(worker_id, token).status, HTTPStatus.OK)

        with patch.object(app, "now", return_value=100):
            uninstall = RouteHarness(
                f"/admin/workers/{worker_id}/commands",
                {"command": "uninstall"},
                cookie=self.admin_cookie,
            )
            app.PullwiseHandler.route(uninstall, "POST")

        running = RouteHarness(
            f"/worker/commands/{uninstall.payload['command']['id']}/status",
            {"worker_id": worker_id, "status": "running"},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(running, "POST")
        self.assertEqual(running.status, HTTPStatus.OK)

        with patch.dict(os.environ, {"PULLWISE_WORKER_CLEANUP_PENDING_TIMEOUT_SECONDS": "864"}, clear=False):
            removed = app.cleanup_server_resources(timestamp=1000)

        self.assertEqual(removed["stale_worker_cleanup_pending"], 1)
        self.assertIsNone(db.get_worker(worker_id))


if __name__ == "__main__":
    import unittest

    unittest.main()
