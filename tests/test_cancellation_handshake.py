from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import unittest
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app, db
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections
from tests.test_worker_pull_routes import (
    RouteHarness,
    audit_result_fields,
    protocol_artifact_manifest,
    v1_worker_heartbeat_payload,
    v1_worker_lease_payload,
)


class CancellationHandshakeTest(unittest.TestCase):
    def setUp(self) -> None:
        start_fast_sqlite_connections(self)
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp_dir.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "PULLWISE_DB_PATH": os.path.join(self.temp_dir.name, "pullwise.sqlite3"),
                "PULLWISE_WORKER_TOKEN": "worker-secret",
                "PULLWISE_WORKER_ID": "wk_1",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        timestamp = app.now()
        app.USERS = {"usr_1": {"id": "usr_1", "name": "Owner", "providers": []}}
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": timestamp,
                "expiresAt": timestamp + 3600,
            }
        }
        app.SETTINGS = {}
        app.BILLING_EVENTS = {}
        app.BILLING_PENDING_UPDATES = []
        app.SCANS = []
        app.ISSUES = []
        app.STATE_LOADED = True
        app.STATE_DIRTY = False
        install_initialized_db_template(
            os.environ["PULLWISE_DB_PATH"],
            worker_token="worker-secret",
            worker_id="wk_1",
        )
        db.upsert_worker_heartbeat(
            {
                "worker_id": "wk_1",
                "version": "0.1.0",
                "provider": "codex",
                "provider_chain": ["codex"],
                "max_concurrent_jobs": 1,
                "running_jobs": 0,
                "free_slots": 1,
                "doctor_status": "ok",
                "codex_ready": 1,
                "ready_providers": ["codex"],
                "timestamp": timestamp,
            }
        )
        self.worker_auth = {"Authorization": "Bearer worker-secret"}
        self.user_auth = {"Cookie": "pw_session=ses_1"}

    def create_scan(self, scan_id: str) -> dict:
        scan = {
            "id": scan_id,
            "repo": f"acme/{scan_id}",
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS.append(scan)
        app.create_scan_job_for_scan(scan)
        return scan

    def post(self, path: str, body: dict, *, worker: bool = False) -> RouteHarness:
        handler = RouteHarness(path, body, headers=self.worker_auth if worker else self.user_auth)
        app.PullwiseHandler.route(handler, "POST")
        return handler

    def lease(self) -> dict:
        response = self.post("/v1/workers/wk_1/lease", v1_worker_lease_payload(), worker=True)
        self.assertEqual(response.status, HTTPStatus.OK)
        return response.payload["job"]

    def event(self, job: dict, event_type: str, sequence: int) -> RouteHarness:
        return self.post(
            f"/v1/review-runs/{job['run_id']}/events",
            {
                "protocol_version": "review-worker-protocol/v1",
                "run_id": job["run_id"],
                "worker_id": "wk_1",
                "sequence": sequence,
                "timestamp": "2026-07-01T10:22:00Z",
                "event_type": event_type,
                "phase": "failure_handling",
                "severity": "info",
                "message": event_type,
                "progress": {
                    "overall_percent": 41.5,
                    "current_phase_percent": 100,
                    "status": "cancelled" if event_type == "run_cancelled" else "running",
                },
            },
            worker=True,
        )

    def upload_cancelled_artifacts(self, job: dict) -> None:
        attempt_id = f"wk_1-{job['attempt']}"
        for artifact in protocol_artifact_manifest(job["run_id"], "cancelled"):
            content = f"{artifact['kind']}:{artifact['name']}\n".encode("utf-8")
            self.assertEqual(hashlib.sha256(content).hexdigest(), artifact["sha256"])
            upload = self.post(
                f"/v1/review-runs/{job['run_id']}/artifacts",
                {
                    "protocol_version": "review-worker-protocol/v1",
                    "attempt_id": attempt_id,
                    "run_id": job["run_id"],
                    "artifact": artifact,
                    "content_base64": base64.b64encode(content).decode("ascii"),
                },
                worker=True,
            )
            self.assertEqual(upload.status, HTTPStatus.OK, upload.payload)

    def test_queued_user_cancel_is_immediately_terminal(self) -> None:
        scan = self.create_scan("sc_cancel_queued")
        job = db.get_scan_job_for_scan(scan["id"])

        cancelled = self.post(f"/scans/{scan['id']}/cancel", {})

        self.assertEqual(cancelled.status, HTTPStatus.OK)
        self.assertEqual(cancelled.payload["status"], "cancelled")
        stored_job = db.get_scan_job(job["job_id"])
        self.assertEqual(stored_job["status"], "cancelled")
        self.assertIsNotNone(stored_job["completed_at"])
        self.assertEqual(db.list_scan_job_attempts(job["job_id"]), [])

    def test_running_user_cancel_waits_for_worker_evidence_before_terminal(self) -> None:
        scan = self.create_scan("sc_cancel_running")
        job = self.lease()

        requested = self.post(f"/scans/{scan['id']}/cancel", {})

        self.assertEqual(requested.status, HTTPStatus.OK)
        self.assertEqual(requested.payload["status"], "cancel_requested")
        requested_job = db.get_scan_job(job["job_id"])
        self.assertEqual(requested_job["status"], "cancel_requested")
        self.assertIsNone(requested_job["completed_at"])
        self.assertEqual(db.list_scan_job_attempts(job["job_id"])[0]["status"], "claimed")

        heartbeat_payload = v1_worker_heartbeat_payload(status="busy", run_id=job["run_id"])
        heartbeat = self.post("/v1/workers/wk_1/heartbeat", heartbeat_payload, worker=True)
        self.assertEqual(heartbeat.status, HTTPStatus.OK)
        self.assertIn(job["job_id"], heartbeat.payload["cancelled_job_ids"])
        self.assertEqual(heartbeat.payload["commands"][0]["action"], "cancel_run")
        self.assertEqual(db.get_worker_heartbeat("wk_1")["running_jobs"], 0)

        cancel_requested_event = self.event(job, "run_cancel_requested", 1)
        self.assertEqual(cancel_requested_event.status, HTTPStatus.OK, cancel_requested_event.payload)
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "cancelling")
        self.assertEqual(db.get_review_run(job["run_id"])["status"], "cancelling")

        cancelled_event = self.event(job, "run_cancelled", 2)
        self.assertEqual(cancelled_event.status, HTTPStatus.OK, cancelled_event.payload)
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "cancelling")
        self.assertEqual(db.get_review_run(job["run_id"])["status"], "cancelling")
        self.assertIsNone(db.get_scan_job(job["job_id"])["completed_at"])

        self.upload_cancelled_artifacts(job)
        result_body = {
            "status": "cancelled",
            "attempt_id": f"wk_1-{job['attempt']}",
            "result_checksum": "checksum-cancelled-handshake",
            "error": "Cancelled by user.",
            **audit_result_fields([], execution_status="cancelled"),
        }
        result = self.post(f"/v1/review-runs/{job['run_id']}/result", result_body, worker=True)
        self.assertEqual(result.status, HTTPStatus.OK, result.payload)
        self.assertTrue(result.payload["accepted"])
        self.assertFalse(result.payload["duplicate"])

        terminal_job = db.get_scan_job(job["job_id"])
        self.assertEqual(terminal_job["status"], "cancelled")
        self.assertIsNotNone(terminal_job["completed_at"])
        self.assertEqual(db.list_scan_job_attempts(job["job_id"])[0]["status"], "cancelled")
        self.assertEqual(db.get_review_run(job["run_id"])["status"], "cancelled")
        self.assertEqual(app.SCANS[0]["status"], "cancelled")
        self.assertEqual(db.count_scan_job_results(job["job_id"]), 1)

        duplicate = self.post(f"/v1/review-runs/{job['run_id']}/result", result_body, worker=True)
        self.assertEqual(duplicate.status, HTTPStatus.OK)
        self.assertTrue(duplicate.payload["duplicate"])
        self.assertEqual(db.count_scan_job_results(job["job_id"]), 1)

    def test_cancel_requested_job_rejects_non_cancelled_result(self) -> None:
        scan = self.create_scan("sc_cancel_wrong_terminal")
        job = self.lease()
        requested = self.post(f"/scans/{scan['id']}/cancel", {})
        self.assertEqual(requested.status, HTTPStatus.OK)

        rejected = self.post(
            f"/v1/review-runs/{job['run_id']}/result",
            {
                "status": "done",
                "attempt_id": f"wk_1-{job['attempt']}",
                "result_checksum": "checksum-wrong-terminal",
                **audit_result_fields([]),
            },
            worker=True,
        )

        self.assertEqual(rejected.status, HTTPStatus.CONFLICT)
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "cancel_requested")
        self.assertEqual(db.count_scan_job_results(job["job_id"]), 0)


if __name__ == "__main__":
    unittest.main()
