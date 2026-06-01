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
        self.text_payload = None
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

    def text(self, payload: str, status: int = HTTPStatus.OK, *, content_type: str = "text/plain; charset=utf-8") -> None:
        self.text_payload = payload
        self.status = status
        self.headers_out = {"Content-Type": content_type}

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
                "PULLWISE_SERVER_URL": "http://localhost:8080",
                "PULLWISE_API_BASE_URL": "",
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
        self.assertEqual(payload["install_url"], "http://localhost:8080/install-worker.sh")
        self.assertIn("read -rsp", payload["install_command"])
        self.assertIn("PULLWISE_WORKER_TOKEN", payload["install_command"])
        self.assertNotIn("--worker-token", payload["install_command"])
        self.assertNotIn(token, payload["install_command"])
        self.assertIn("'US worker'", payload["install_command"])
        self.assertEqual(audit[0]["action"], "create_worker")
        self.assertEqual(audit[0]["actor_user_id"], "usr_admin")

        detail = RouteHarness(f"/admin/workers/{worker_id}", cookie=self.admin_cookie)
        app.PullwiseHandler.route(detail, "GET")
        self.assertEqual(detail.status, HTTPStatus.OK)
        self.assertNotIn("worker_token", json.dumps(detail.payload))

    def test_public_install_script_contains_deploy_assets_but_no_worker_secrets(self) -> None:
        install = RouteHarness("/install-worker.sh")

        app.PullwiseHandler.route(install, "GET")

        self.assertEqual(install.status, HTTPStatus.OK)
        self.assertIn("text/x-shellscript", install.headers_out["Content-Type"])
        self.assertIn("systemd", install.text_payload)
        self.assertIn("pullwise-worker.service", install.text_payload)
        self.assertIn("logrotate", install.text_payload)
        self.assertIn("doctor", install.text_payload)
        self.assertIn("codex login", install.text_payload)
        self.assertIn("PULLWISE_WORKER_PACKAGE", install.text_payload)
        self.assertIn("PULLWISE_WORKER_TOKEN", install.text_payload)
        self.assertIn("--worker-token-file", install.text_payload)
        self.assertNotIn("--worker-token) WORKER_TOKEN", install.text_payload)
        self.assertNotIn("$(dirname \"$0\")", install.text_payload)
        self.assertNotIn("cp \"$(dirname", install.text_payload)
        self.assertNotIn("\r\n", install.text_payload)
        self.assertNotIn("pww_", install.text_payload)
        self.assertNotIn("WORKER_TOKEN=pww_", install.text_payload)

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
        self.assertIn("PULLWISE_WORKER_TOKEN", rotate.payload["install_command"])
        self.assertNotIn("--worker-token", rotate.payload["install_command"])
        self.assertNotIn(new_token, rotate.payload["install_command"])

        old_token_claim = RouteHarness("/worker/jobs/claim", {"worker_id": worker_id}, headers={"Authorization": f"Bearer {token}"})
        app.PullwiseHandler.route(old_token_claim, "POST")
        self.assertEqual(old_token_claim.status, HTTPStatus.UNAUTHORIZED)

        new_token_heartbeat = RouteHarness(
            "/worker/heartbeat",
            {"worker_id": worker_id, "max_concurrent_jobs": 4, "running_jobs": 0, "free_slots": 4},
            headers={"Authorization": f"Bearer {new_token}"},
        )
        app.PullwiseHandler.route(new_token_heartbeat, "POST")
        self.assertEqual(new_token_heartbeat.status, HTTPStatus.OK)
        self.assertEqual(new_token_heartbeat.payload["worker"]["worker_id"], worker_id)

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
                "doctor_status": "ok",
                "codex_ready": True,
                "systemd_active": True,
                "doctor_checked_at": app.now(),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(heartbeat, "POST")
        self.assertEqual(heartbeat.status, HTTPStatus.OK)

        worker = db.get_worker(worker_id)
        self.assertEqual(worker["max_concurrent_jobs"], 4)
        self.assertEqual(worker["running_jobs"], 2)
        self.assertIsNotNone(worker["last_heartbeat_at"])
        self.assertEqual(worker["doctor_status"], "ok")
        self.assertEqual(worker["codex_ready"], 1)
        self.assertEqual(worker["systemd_active"], 1)
        self.assertEqual(app.computed_worker_status(worker), "idle")

        db.upsert_worker_heartbeat({**worker, "worker_id": worker_id, "running_jobs": 4, "free_slots": 0, "timestamp": app.now()})
        self.assertEqual(app.computed_worker_status(db.get_worker(worker_id)), "busy")
        db.upsert_worker_heartbeat({**worker, "worker_id": worker_id, "running_jobs": 0, "free_slots": 4, "doctor_status": "degraded", "codex_ready": 0, "timestamp": app.now()})
        self.assertEqual(app.computed_worker_status(db.get_worker(worker_id)), "degraded")
        db.upsert_worker_heartbeat({**worker, "worker_id": worker_id, "last_error": "internal stack", "timestamp": app.now()})
        self.assertEqual(app.computed_worker_status(db.get_worker(worker_id)), "degraded")
        with patch("pullwise_server.app.now", return_value=app.now() + 1000):
            self.assertEqual(app.computed_worker_status(db.get_worker(worker_id)), "offline")

        app.SCANS = [{"id": "sc_queued", "status": "queued"}, {"id": "sc_running", "status": "running"}]
        public = RouteHarness("/status/system")
        app.PullwiseHandler.route(public, "GET")
        self.assertEqual(public.status, HTTPStatus.OK)
        public_text = json.dumps(public.payload)
        self.assertIn(public.payload["scanSystemStatus"], {"ok", "degraded", "down"})
        self.assertEqual(public.payload["queuedJobs"], 1)
        self.assertEqual(public.payload["runningJobs"], 1)
        self.assertNotIn("workers", public.payload)
        self.assertNotIn("US worker", public_text)
        self.assertNotIn("us-east", public_text)
        self.assertNotIn("0.1.0", public_text)
        self.assertNotIn("secret-host", public_text)
        self.assertNotIn("internal stack", public_text)
        self.assertNotIn("doctor_status", public_text)
        self.assertNotIn("systemd_active", public_text)
        self.assertNotIn("codex_ready", public_text)
        self.assertNotIn("auditEvents", public_text)
        self.assertNotIn("worker_token", public_text)
        self.assertNotIn("token_hash", public_text)
        self.assertNotIn(token, public_text)

        admin = RouteHarness("/admin/status", cookie=self.admin_cookie)
        app.PullwiseHandler.route(admin, "GET")
        self.assertEqual(admin.status, HTTPStatus.OK)
        self.assertEqual(admin.payload["workers"][0]["worker_id"], worker_id)
        self.assertIn("hostname", admin.payload["workers"][0])
        self.assertIn("last_error", admin.payload["workers"][0])
        self.assertEqual(admin.payload["workers"][0]["doctor_status"], "ok")
        self.assertTrue(admin.payload["workers"][0]["codex_ready"])
        self.assertTrue(admin.payload["workers"][0]["systemd_active"])

    def test_status_capacity_increases_with_multiple_online_workers(self) -> None:
        payload_one, token_one = self.create_worker()
        worker_one_id = payload_one["worker_id"]
        payload_two, token_two = self.create_worker()
        worker_two_id = payload_two["worker_id"]

        for worker_id, token, capacity, running in (
            (worker_one_id, token_one, 4, 1),
            (worker_two_id, token_two, 2, 0),
        ):
            heartbeat = RouteHarness(
                "/worker/heartbeat",
                {
                    "worker_id": worker_id,
                    "provider": "codex",
                    "version": "0.1.0",
                    "max_concurrent_jobs": capacity,
                    "running_jobs": running,
                    "free_slots": capacity - running,
                    "doctor_status": "ok",
                    "codex_ready": True,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            app.PullwiseHandler.route(heartbeat, "POST")
            self.assertEqual(heartbeat.status, HTTPStatus.OK)

        public = RouteHarness("/status/system")
        app.PullwiseHandler.route(public, "GET")

        self.assertEqual(public.status, HTTPStatus.OK)
        self.assertEqual(public.payload["onlineWorkerCount"], 2)
        self.assertEqual(public.payload["totalWorkerCount"], 2)
        self.assertEqual(public.payload["totalCapacity"], 6)
        self.assertEqual(public.payload["availableCapacity"], 5)
        self.assertNotIn("workers", public.payload)

    def test_worker_capacity_is_capped_for_admin_writes_and_heartbeat(self) -> None:
        create = RouteHarness(
            "/admin/workers",
            {"name": "Too large", "provider": "codex", "max_concurrent_jobs": 99},
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(create, "POST")
        self.assertEqual(create.status, HTTPStatus.BAD_REQUEST)

        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        update = RouteHarness(
            f"/admin/workers/{worker_id}",
            {"max_concurrent_jobs": 99},
            cookie=self.admin_cookie,
        )
        app.PullwiseHandler.route(update, "PATCH")
        self.assertEqual(update.status, HTTPStatus.BAD_REQUEST)

        heartbeat = RouteHarness(
            "/worker/heartbeat",
            {"worker_id": worker_id, "max_concurrent_jobs": 99, "running_jobs": 99, "free_slots": 99},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(heartbeat, "POST")

        self.assertEqual(heartbeat.status, HTTPStatus.OK)
        stored = db.get_worker(worker_id)
        self.assertEqual(stored["max_concurrent_jobs"], 32)
        self.assertEqual(stored["running_jobs"], 32)
        self.assertEqual(stored["free_slots"], 32)
        self.assertEqual(stored["last_error"], "max_concurrent_jobs clamped to 32")

        heartbeat_with_error = RouteHarness(
            "/worker/heartbeat",
            {
                "worker_id": worker_id,
                "max_concurrent_jobs": 99,
                "running_jobs": 1,
                "free_slots": 1,
                "last_error": "disk pressure",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(heartbeat_with_error, "POST")

        self.assertEqual(heartbeat_with_error.status, HTTPStatus.OK)
        stored = db.get_worker(worker_id)
        self.assertEqual(stored["last_error"], "disk pressure; max_concurrent_jobs clamped to 32")

    def test_disabling_worker_blocks_new_claims_but_allows_current_job_result(self) -> None:
        payload, token = self.create_worker()
        worker_id = payload["worker_id"]
        scan = {
            "id": "sc_active",
            "repo": "acme/api",
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": "usr_user",
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
        }
        app.SCANS = [scan]
        job = app.create_scan_job_for_scan(scan)

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": worker_id}, headers={"Authorization": f"Bearer {token}"})
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)

        disable = RouteHarness(f"/admin/workers/{worker_id}/disable", cookie=self.admin_cookie)
        app.PullwiseHandler.route(disable, "POST")
        self.assertEqual(disable.status, HTTPStatus.OK)

        new_claim = RouteHarness("/worker/jobs/claim", {"worker_id": worker_id}, headers={"Authorization": f"Bearer {token}"})
        app.PullwiseHandler.route(new_claim, "POST")
        self.assertEqual(new_claim.status, HTTPStatus.UNAUTHORIZED)

        progress = RouteHarness(
            f"/worker/jobs/{job['job_id']}/progress",
            {"phase": "ai", "progress": 70},
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(progress, "POST")
        self.assertEqual(progress.status, HTTPStatus.OK)

        result_body = {
            "status": "done",
            "findings": [],
            "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "duration_ms": 1000,
            "attempt_id": f"{worker_id}-1",
            "result_checksum": "checksum-disabled-worker-finish",
        }
        result = RouteHarness(
            f"/worker/jobs/{job['job_id']}/result",
            result_body,
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(result, "POST")
        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertEqual(app.SCANS[0]["status"], "done")

    def test_worker_test_records_audit(self) -> None:
        payload, _token = self.create_worker()
        worker_id = payload["worker_id"]

        test = RouteHarness(f"/admin/workers/{worker_id}/test", cookie=self.admin_cookie)
        app.PullwiseHandler.route(test, "POST")

        self.assertEqual(test.status, HTTPStatus.OK)
        self.assertIn("checks", test.payload["result"])
        self.assertIn("test_worker", [event["action"] for event in db.list_worker_audit_events(worker_id)])

    def test_worker_audit_records_required_fields_for_success_and_failure(self) -> None:
        payload, _token = self.create_worker()
        worker_id = payload["worker_id"]
        update = RouteHarness(
            f"/admin/workers/{worker_id}",
            {"name": "Audit worker", "region": "eu-west"},
            cookie=self.admin_cookie,
            headers={"X-Request-Id": "req_update"},
        )
        app.PullwiseHandler.route(update, "PATCH")
        missing = RouteHarness(
            "/admin/workers/missing_worker/disable",
            cookie=self.admin_cookie,
            headers={"X-Request-Id": "req_missing"},
        )
        app.PullwiseHandler.route(missing, "POST")

        self.assertEqual(update.status, HTTPStatus.OK)
        self.assertEqual(missing.status, HTTPStatus.NOT_FOUND)
        events = db.list_worker_audit_events(limit=20)
        update_event = next(event for event in events if event["action"] == "update_worker")
        failure_event = next(event for event in events if event["action"] == "disable_worker" and event["success"] == 0)

        self.assertEqual(update_event["actor_user_id"], "usr_admin")
        self.assertEqual(update_event["worker_id"], worker_id)
        self.assertEqual(update_event["request_id"], "req_update")
        self.assertEqual(update_event["success"], 1)
        self.assertEqual(json.loads(update_event["changed_fields"]), {"name": "Audit worker", "region": "eu-west"})
        self.assertIsNotNone(update_event["created_at"])

        self.assertEqual(failure_event["actor_user_id"], "usr_admin")
        self.assertEqual(failure_event["worker_id"], "missing_worker")
        self.assertEqual(failure_event["request_id"], "req_missing")
        self.assertEqual(failure_event["error"], "Worker not found.")


if __name__ == "__main__":
    unittest.main()
