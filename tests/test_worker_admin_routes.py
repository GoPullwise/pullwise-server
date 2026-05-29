from __future__ import annotations

import json
import os
import tempfile
import unittest
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app, db


class RouteHarness(app.PullwiseHandler):
    def __init__(self, path: str, body: dict | None = None, *, cookie: str = "", headers: dict | None = None) -> None:
        self.path = path
        self._body = body or {}
        self._raw_body = json.dumps(self._body).encode("utf-8")
        self.headers = {"Host": "api.pullwise.dev", "Cookie": cookie, **(headers or {})}
        self.payload = None
        self.status = None
        self.headers_out = {}
        self.client_address = ("203.0.113.10", 51234)

    def read_json(self) -> dict:
        return self._body

    def read_raw_body(self) -> bytes:
        return self._raw_body

    def json(self, payload: dict, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.status = status
        self.headers_out = headers or {}

    def error(self, status: int, message: str) -> None:
        self.json({"message": message}, status)


def reset_state() -> None:
    app.USERS = {
        "usr_admin": {"id": "usr_admin", "email": "admin@example.com", "name": "Admin"},
        "usr_user": {"id": "usr_user", "email": "user@example.com", "name": "User"},
    }
    app.SESSIONS = {
        "ses_admin": {"id": "ses_admin", "userId": "usr_admin", "createdAt": app.now(), "expiresAt": app.now() + 3600},
        "ses_user": {"id": "ses_user", "userId": "usr_user", "createdAt": app.now(), "expiresAt": app.now() + 3600},
    }
    app.SCANS = []
    app.ISSUES = []
    app.SETTINGS = {}
    app.BILLING_EVENTS = {}
    app.BILLING_PENDING_UPDATES = []
    app.STATE_LOADED = True
    app.STATE_DIRTY = False


class WorkerAdminRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_state()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "PULLWISE_DB_PATH": os.path.join(self.temp_dir.name, "pullwise.sqlite3"),
                "PULLWISE_ADMIN_USER_IDS": "usr_admin",
                "PULLWISE_ADMIN_EMAILS": "admin@example.com",
                "PULLWISE_WORKER_HEARTBEAT_TIMEOUT_SECONDS": "120",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        db.initialize()
        self.admin_cookie = "pw_session=ses_admin"
        self.user_cookie = "pw_session=ses_user"

    def create_worker(self) -> tuple[dict, str]:
        handler = RouteHarness(
            "/admin/workers",
            {"name": "US worker", "provider": "codex", "region": "us-east", "max_concurrent_jobs": 4},
            cookie=self.admin_cookie,
            headers={"X-Request-Id": "req_create"},
        )
        app.PullwiseHandler.route(handler, "POST")
        self.assertEqual(handler.status, HTTPStatus.CREATED)
        return handler.payload, handler.payload["worker_token"]

    def test_admin_can_create_worker_and_token_is_only_returned_once_as_hash(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        stored = db.get_worker(worker_id)
        audit = db.list_worker_audit_events(worker_id)

        self.assertTrue(token.startswith("pww_"))
        self.assertEqual(stored["worker_id"], worker_id)
        self.assertNotEqual(stored["token_hash"], token)
        self.assertEqual(stored["token_hash"], db.worker_token_hash(token))
        self.assertNotIn("worker_token", payload["worker"])
        self.assertEqual(payload["suggested_env"]["PULLWISE_WORKER_TOKEN"], token)
        self.assertEqual(audit[0]["action"], "create_worker")
        self.assertEqual(audit[0]["actor_user_id"], "usr_admin")

        detail = RouteHarness(f"/admin/workers/{worker_id}", cookie=self.admin_cookie)
        app.PullwiseHandler.route(detail, "GET")
        self.assertEqual(detail.status, HTTPStatus.OK)
        self.assertNotIn("worker_token", json.dumps(detail.payload))

    def test_non_admin_cannot_access_admin_workers(self) -> None:
        denied = RouteHarness("/admin/workers", cookie=self.user_cookie)
        app.PullwiseHandler.route(denied, "GET")

        self.assertEqual(denied.status, HTTPStatus.FORBIDDEN)

    def test_admin_can_update_enable_disable_delete_and_rotate_worker(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]

        update = RouteHarness(f"/admin/workers/{worker_id}", {"name": "EU worker", "region": "eu"}, cookie=self.admin_cookie)
        app.PullwiseHandler.route(update, "PATCH")
        self.assertEqual(update.status, HTTPStatus.OK)
        self.assertEqual(update.payload["worker"]["name"], "EU worker")
        self.assertEqual(update.payload["worker"]["region"], "eu")

        disable = RouteHarness(f"/admin/workers/{worker_id}/disable", cookie=self.admin_cookie)
        app.PullwiseHandler.route(disable, "POST")
        self.assertEqual(disable.status, HTTPStatus.OK)
        self.assertFalse(disable.payload["worker"]["enabled"])
        self.assertEqual(disable.payload["worker"]["status"], "disabled")

        disabled_heartbeat = RouteHarness(
            "/worker/heartbeat",
            {"worker_id": worker_id, "max_concurrent_jobs": 4, "running_jobs": 0, "free_slots": 4},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(disabled_heartbeat, "POST")
        self.assertEqual(disabled_heartbeat.status, HTTPStatus.OK)
        self.assertEqual(disabled_heartbeat.payload["worker"]["status"], "disabled")

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": worker_id}, headers={"Authorization": f"Bearer {token}"})
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.UNAUTHORIZED)

        enable = RouteHarness(f"/admin/workers/{worker_id}/enable", cookie=self.admin_cookie)
        app.PullwiseHandler.route(enable, "POST")
        self.assertEqual(enable.status, HTTPStatus.OK)
        self.assertTrue(enable.payload["worker"]["enabled"])

        rotate = RouteHarness(f"/admin/workers/{worker_id}/rotate-token", cookie=self.admin_cookie)
        app.PullwiseHandler.route(rotate, "POST")
        self.assertEqual(rotate.status, HTTPStatus.OK)
        new_token = rotate.payload["worker_token"]
        self.assertNotEqual(new_token, token)
        self.assertEqual(db.get_worker(worker_id)["token_hash"], db.worker_token_hash(new_token))

        old_token_claim = RouteHarness("/worker/jobs/claim", {"worker_id": worker_id}, headers={"Authorization": f"Bearer {token}"})
        app.PullwiseHandler.route(old_token_claim, "POST")
        self.assertEqual(old_token_claim.status, HTTPStatus.UNAUTHORIZED)

        delete = RouteHarness(f"/admin/workers/{worker_id}", cookie=self.admin_cookie)
        app.PullwiseHandler.route(delete, "DELETE")
        self.assertEqual(delete.status, HTTPStatus.OK)
        self.assertTrue(delete.payload["deleted"])
        self.assertIsNotNone(db.get_worker(worker_id, include_deleted=True)["deleted_at"])

        actions = [event["action"] for event in db.list_worker_audit_events(worker_id, limit=20)]
        for expected in ["update_worker", "disable_worker", "enable_worker", "rotate_worker_token", "delete_worker"]:
            self.assertIn(expected, actions)

    def test_heartbeat_status_public_and_admin_status_payloads(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        heartbeat = RouteHarness(
            "/worker/heartbeat",
            {
                "worker_id": worker_id,
                "provider": "codex",
                "version": "0.1.0",
                "max_concurrent_jobs": 4,
                "running_jobs": 2,
                "free_slots": 2,
                "hostname": "secret-host",
                "region": "us-east",
                "last_error": "",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(heartbeat, "POST")
        self.assertEqual(heartbeat.status, HTTPStatus.OK)

        worker = db.get_worker(worker_id)
        self.assertEqual(worker["max_concurrent_jobs"], 4)
        self.assertEqual(worker["running_jobs"], 2)
        self.assertIsNotNone(worker["last_heartbeat_at"])
        self.assertEqual(app.computed_worker_status(worker), "idle")

        db.upsert_worker_heartbeat({**worker, "worker_id": worker_id, "running_jobs": 4, "free_slots": 0, "timestamp": app.now()})
        self.assertEqual(app.computed_worker_status(db.get_worker(worker_id)), "busy")
        db.upsert_worker_heartbeat({**worker, "worker_id": worker_id, "last_error": "internal stack", "timestamp": app.now()})
        self.assertEqual(app.computed_worker_status(db.get_worker(worker_id)), "degraded")
        with patch("pullwise_server.app.now", return_value=app.now() + 1000):
            self.assertEqual(app.computed_worker_status(db.get_worker(worker_id)), "offline")

        app.SCANS = [{"id": "sc_queued", "status": "queued"}, {"id": "sc_running", "status": "running"}]
        public = RouteHarness("/status/system")
        app.PullwiseHandler.route(public, "GET")
        self.assertEqual(public.status, HTTPStatus.OK)
        self.assertIn(public.payload["scanSystemStatus"], {"ok", "degraded", "down"})
        self.assertEqual(public.payload["queuedJobs"], 1)
        self.assertEqual(public.payload["runningJobs"], 1)
        self.assertNotIn("secret-host", json.dumps(public.payload))
        self.assertNotIn("internal stack", json.dumps(public.payload))

        admin = RouteHarness("/admin/status", cookie=self.admin_cookie)
        app.PullwiseHandler.route(admin, "GET")
        self.assertEqual(admin.status, HTTPStatus.OK)
        self.assertEqual(admin.payload["workers"][0]["worker_id"], worker_id)
        self.assertIn("hostname", admin.payload["workers"][0])
        self.assertIn("last_error", admin.payload["workers"][0])

    def test_worker_test_records_audit(self) -> None:
        payload, _token = self.create_worker()
        worker_id = payload["worker_id"]

        test = RouteHarness(f"/admin/workers/{worker_id}/test", cookie=self.admin_cookie)
        app.PullwiseHandler.route(test, "POST")

        self.assertEqual(test.status, HTTPStatus.OK)
        self.assertIn("checks", test.payload["result"])
        self.assertIn("test_worker", [event["action"] for event in db.list_worker_audit_events(worker_id)])


if __name__ == "__main__":
    unittest.main()
