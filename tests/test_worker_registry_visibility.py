from __future__ import annotations

import json
import os
import tempfile
import unittest
from http import HTTPStatus
from unittest.mock import patch
from pullwise_server import app, db
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections
from tests.test_worker_admin_routes import RouteHarness, reset_state


class WorkerRegistryVisibilityTest(unittest.TestCase):
    def test_admin_can_find_a_new_worker_before_first_registration(self) -> None:
        reset_state()
        start_fast_sqlite_connections(self)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "PULLWISE_DB_PATH": os.path.join(directory, "server.sqlite3"),
            "PULLWISE_ADMIN_EMAILS": "admin@example.com",
        }, clear=True):
            install_initialized_db_template(os.environ["PULLWISE_DB_PATH"])
            worker = db.create_worker({"name": "New unassigned Worker", "version": "0.10.24"})
            handler = RouteHarness("/admin/workers", cookie="pw_session=ses_admin")
            app.PullwiseHandler.route(handler, "GET")
            self.assertEqual(handler.status, HTTPStatus.OK, handler.payload)
            self.assertEqual([item["worker_id"] for item in handler.payload["items"]], [worker["worker_id"]])
            self.assertEqual(handler.payload["total"], 1)
            self.assertIsNone(handler.payload["items"][0]["last_heartbeat_at"])
            self.assertFalse(worker["worker_token"] in json.dumps(handler.payload), "Worker token leaked into list")
