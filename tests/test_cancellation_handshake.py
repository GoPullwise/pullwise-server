from __future__ import annotations

import base64
import hashlib
import json
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
        db.upsert_scan(scan)
        return scan

    def create_quota_scan(self, scan_id: str) -> dict:
        user = app.USERS["usr_1"]
        repository = db.upsert_repository(
            {
                "github_repo_id": f"quota-{scan_id}",
                "full_name": f"acme/{scan_id}",
                "owner_login": "acme",
                "default_branch": "main",
            }
        )
        request_id = f"req_{scan_id}"
        quota_result = app.quota.reserve_scan_quota(
            user=user,
            repository=repository,
            requested_by_user_id=user["id"],
            scan_id=scan_id,
            request_id=request_id,
        )
        scan = {
            "id": scan_id,
            "repo": repository["full_name"],
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": user["id"],
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "repoId": repository["id"],
            "githubRepoId": repository["github_repo_id"],
            "requestId": request_id,
            "quotaBucketIds": quota_result["bucketIds"],
            "billingUsage": quota_result["user"],
            "repoUsage": quota_result["repository"],
            "quotaState": "reserved",
            "quotaReservedAt": app.now(),
        }
        app.SCANS.append(scan)
        app.create_scan_job_for_scan(scan)
        db.upsert_scan(scan)
        return scan

    def post(self, path: str, body: dict, *, worker: bool = False) -> RouteHarness:
        handler = RouteHarness(path, body, headers=self.worker_auth if worker else self.user_auth)
        app.PullwiseHandler.route(handler, "POST")
        return handler

    def lease(self) -> dict:
        response = self.post("/v1/workers/wk_1/lease", v1_worker_lease_payload(), worker=True)
        self.assertEqual(response.status, HTTPStatus.OK)
        return response.payload["job"]

    def event(
        self,
        job: dict,
        event_type: str,
        sequence: int,
        *,
        phase: str = "failure_handling",
        steps: list[dict] | None = None,
    ) -> RouteHarness:
        progress = {
            "overall_percent": 41.5,
            "current_phase_percent": 100,
            "status": (
                "cancelled" if event_type == "run_cancelled" else "running"
            ),
        }
        if steps is not None:
            progress["steps"] = steps
        return self.post(
            f"/v1/review-runs/{job['run_id']}/events",
            {
                "protocol_version": "review-worker-protocol/v1",
                "run_id": job["run_id"],
                "worker_id": "wk_1",
                "sequence": sequence,
                "timestamp": "2026-07-01T10:22:00Z",
                "event_type": event_type,
                "phase": phase,
                "severity": "info",
                "message": event_type,
                "progress": progress,
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

        requested = self.post(
            f"/scans/{scan['id']}/cancel",
            {"reason": "keep cancellation authority"},
        )

        self.assertEqual(requested.status, HTTPStatus.OK)
        self.assertEqual(requested.payload["status"], "cancel_requested")
        requested_job = db.get_scan_job(job["job_id"])
        self.assertEqual(requested_job["status"], "cancel_requested")
        durable_cancellation = db.get_user_scan_snapshot("usr_1", scan["id"])
        expected_cancel_reason = durable_cancellation["cancelReason"]
        expected_cancel_requested_at = durable_cancellation["cancelRequestedAt"]
        self.assertIsNone(requested_job["completed_at"])
        self.assertEqual(db.list_scan_job_attempts(job["job_id"])[0]["status"], "claimed")
        cancellation_deadline = int(requested_job["timeout_at"])

        heartbeat_payload = v1_worker_heartbeat_payload(status="busy", run_id=job["run_id"])
        heartbeat = self.post("/v1/workers/wk_1/heartbeat", heartbeat_payload, worker=True)
        self.assertEqual(heartbeat.status, HTTPStatus.OK)
        self.assertIn(job["job_id"], heartbeat.payload["cancelled_job_ids"])
        self.assertEqual(heartbeat.payload["commands"][0]["action"], "cancel_run")
        self.assertEqual(db.get_worker_heartbeat("wk_1")["running_jobs"], 0)

        with patch.object(app, "now", return_value=cancellation_deadline - 100):
            cancel_requested_event = self.event(job, "run_cancel_requested", 1)
        self.assertEqual(cancel_requested_event.status, HTTPStatus.OK, cancel_requested_event.payload)
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "cancelling")
        self.assertEqual(db.get_review_run(job["run_id"])["status"], "cancelling")
        self.assertEqual(db.get_scan_job(job["job_id"])["timeout_at"], cancellation_deadline)

        with patch.object(app, "now", return_value=cancellation_deadline - 50):
            cancelled_event = self.event(job, "run_cancelled", 2)
        self.assertEqual(cancelled_event.status, HTTPStatus.OK, cancelled_event.payload)
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "cancelling")
        self.assertEqual(db.get_review_run(job["run_id"])["status"], "cancelling")
        self.assertIsNone(db.get_scan_job(job["job_id"])["completed_at"])
        self.assertEqual(db.get_scan_job(job["job_id"])["timeout_at"], cancellation_deadline)

        self.upload_cancelled_artifacts(job)
        scan["status"] = "running"
        scan.pop("cancelReason", None)
        scan.pop("cancelRequestedAt", None)
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
        stored_scan = db.get_user_scan_snapshot("usr_1", scan["id"])
        self.assertEqual(stored_scan["cancelReason"], expected_cancel_reason)
        self.assertEqual(
            stored_scan["cancelRequestedAt"],
            expected_cancel_requested_at,
        )

        duplicate = self.post(f"/v1/review-runs/{job['run_id']}/result", result_body, worker=True)
        self.assertEqual(duplicate.status, HTTPStatus.OK)
        self.assertTrue(duplicate.payload["duplicate"])
        self.assertEqual(db.count_scan_job_results(job["job_id"]), 1)

        scan["status"] = "running"
        scan.pop("completedAt", None)
        db.upsert_scan(scan)
        with patch.object(
            app,
            "validate_review_worker_protocol_artifacts",
            side_effect=ValueError("Uploaded review artifacts do not match result manifest: art_worker_log"),
        ):
            app.reconcile_scan_job_state_locked(scan)

        self.assertEqual(scan["status"], "cancelled")
        self.assertNotEqual(scan.get("errorCode"), "WORKER_ARTIFACT_INVALID")
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "cancelled")
        self.assertEqual(db.get_review_run(job["run_id"])["status"], "cancelled")

    def test_cancel_requested_job_rejects_non_cancelled_result(self) -> None:
        scan = self.create_scan("sc_cancel_wrong_terminal")
        job = self.lease()
        requested = self.post(f"/scans/{scan['id']}/cancel", {})
        self.assertEqual(requested.status, HTTPStatus.OK)

        rejected = self.post(
            f"/v1/review-runs/{job['run_id']}/result",
            {
                "status": "done",
                "attempt_id": "wk_1-spoofed",
                "result_checksum": "checksum-wrong-terminal",
                **audit_result_fields([]),
            },
            worker=True,
        )

        self.assertEqual(rejected.status, HTTPStatus.CONFLICT)
        self.assertEqual(rejected.payload["code"], "JOB_CANCELLATION_AUTHORITATIVE")
        self.assertEqual(rejected.payload["jobStatus"], "cancel_requested")
        self.assertEqual(rejected.payload["jobId"], job["job_id"])
        self.assertEqual(rejected.payload["runId"], job["run_id"])
        self.assertEqual(rejected.payload["attemptId"], f"wk_1-{job['attempt']}")
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "cancel_requested")
        self.assertEqual(db.count_scan_job_results(job["job_id"]), 0)

    def test_deadline_cancelled_job_accepts_late_cancelled_worker_receipt(self) -> None:
        scan = self.create_scan("sc_cancel_late_receipt")
        job = self.lease()
        requested = self.post(f"/scans/{scan['id']}/cancel", {})
        self.assertEqual(requested.status, HTTPStatus.OK)
        deadline = int(db.get_scan_job(job["job_id"])["timeout_at"])
        self.assertEqual(app.recover_expired_scan_leases_once(deadline + 1), 1)
        authoritative_job = db.get_scan_job(job["job_id"])
        authoritative_attempt = db.list_scan_job_attempts(job["job_id"])[0]
        authoritative_run = db.get_review_run(job["run_id"])
        authoritative_scan = db.get_user_scan_snapshot("usr_1", scan["id"])
        self.assertEqual(authoritative_job["status"], "cancelled")

        audit_fields = audit_result_fields([], execution_status="cancelled")
        audit_fields["reviewWorkerProtocol"].setdefault("extensions", {}).setdefault(
            "worker_internal",
            {},
        )["artifact_upload_error"] = "job already terminal before cancellation evidence upload"
        audit_fields["reviewWorkerProtocol"]["execution"]["completed_at"] = (
            "2020-01-01T00:00:00Z"
        )
        audit_fields["reviewWorkerProtocol"]["error"] = {
            "code": "WORKER_SUPERSEDED",
            "message": "Worker observed cancellation after preparing done.",
            "source": "worker",
        }
        result_body = {
            "status": "cancelled",
            "attempt_id": f"wk_1-{job['attempt']}",
            "result_checksum": "checksum-late-cancelled-receipt",
            "error": "Cancellation authority superseded a durable completed result.",
            **audit_fields,
        }

        receipt = self.post(
            f"/v1/review-runs/{job['run_id']}/result",
            result_body,
            worker=True,
        )

        self.assertEqual(receipt.status, HTTPStatus.OK, receipt.payload)
        self.assertTrue(receipt.payload["accepted"])
        self.assertFalse(receipt.payload["duplicate"])
        self.assertEqual(db.count_scan_job_results(job["job_id"]), 1)
        stored_job = db.get_scan_job(job["job_id"])
        stored_attempt = db.list_scan_job_attempts(job["job_id"])[0]
        self.assertEqual(stored_job["status"], "cancelled")
        self.assertEqual(stored_job["completed_at"], authoritative_job["completed_at"])
        self.assertEqual(stored_job["error"], authoritative_job["error"])
        self.assertEqual(stored_job["cancel_reason"], authoritative_job["cancel_reason"])
        self.assertEqual(
            (stored_attempt["completed_at"], stored_attempt["error"]),
            (
                authoritative_attempt["completed_at"],
                authoritative_attempt["error"],
            ),
        )
        stored_run = db.get_review_run(job["run_id"])
        self.assertEqual(stored_run["completed_at"], authoritative_run["completed_at"])
        self.assertEqual(stored_run["duration_ms"], authoritative_run["duration_ms"])
        self.assertEqual(
            json.loads(stored_run["error_json"]),
            json.loads(authoritative_run["error_json"]),
        )
        stored_scan = db.get_user_scan_snapshot("usr_1", scan["id"])
        for key in (
            "completedAt",
            "durationMs",
            "error",
            "recoveredAt",
            "recoveryReason",
            "cancelReason",
            "cancelRequestedAt",
        ):
            self.assertEqual(
                stored_scan.get(key),
                authoritative_scan.get(key),
                key,
            )
        duplicate = self.post(
            f"/v1/review-runs/{job['run_id']}/result",
            result_body,
            worker=True,
        )
        self.assertEqual(duplicate.status, HTTPStatus.OK, duplicate.payload)
        self.assertTrue(duplicate.payload["duplicate"])

    def test_cancelling_after_core_progress_consumes_quota_instead_of_releasing(self) -> None:
        scan = self.create_quota_scan("sc_cancel_core_progress")
        job = self.lease()
        db.update_scan_job_progress(
            job["job_id"],
            {
                "phase": "repo_map",
                "progress": 45,
                "message": "core repository map completed",
                "started_at": app.now(),
            },
        )

        requested = self.post(f"/scans/{scan['id']}/cancel", {})

        self.assertEqual(requested.status, HTTPStatus.OK, requested.payload)
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "cancel_requested")
        stored_scan = db.get_user_scan_snapshot("usr_1", scan["id"])
        self.assertEqual(stored_scan["quotaState"], "consumed")
        self.assertEqual(scan["quotaState"], "consumed")
        self.assertEqual(scan["quotaConsumedAt"], stored_scan["quotaConsumedAt"])
        self.assertEqual(
            db.get_scan_job(job["job_id"])["projection_pending"],
            0,
        )
        usage = app.quota.quota_payload_for_user(app.USERS["usr_1"])
        self.assertEqual((usage["used"], usage["reserved"]), (1, 0))
        with db.connect() as connection:
            reasons = [
                row[0]
                for row in connection.execute(
                    "SELECT reason FROM quota_ledger WHERE scan_id = ?",
                    (scan["id"],),
                ).fetchall()
            ]
        self.assertIn("scan_consumed", reasons)
        self.assertNotIn("scan_reservation_released", reasons)

    def test_historical_core_event_prevents_release_after_later_cleanup_phase(self) -> None:
        scan = self.create_quota_scan("sc_cancel_historical_core")
        job = self.lease()
        with patch.object(app, "finalize_scan_quota_for_job", return_value={}):
            core = self.event(job, "phase_completed", 1, phase="repo_map")
            cleanup = self.event(
                job,
                "phase_completed",
                2,
                phase="cleanup_active_job",
            )
        self.assertEqual(core.status, HTTPStatus.OK, core.payload)
        self.assertEqual(cleanup.status, HTTPStatus.OK, cleanup.payload)
        self.assertEqual(db.get_scan_job(job["job_id"])["progress_phase"], "cleanup_active_job")

        requested = self.post(
            f"/scans/{scan['id']}/cancel",
            {"reason": "historical core work must be billed"},
        )

        self.assertEqual(requested.status, HTTPStatus.OK, requested.payload)
        stored_scan = db.get_user_scan_snapshot("usr_1", scan["id"])
        self.assertEqual(stored_scan["quotaState"], "consumed")
        self.assertEqual(
            stored_scan["cancelReason"],
            "historical core work must be billed",
        )
        self.assertIsNotNone(stored_scan["cancelRequestedAt"])
        with db.connect() as connection:
            reasons = [
                row[0]
                for row in connection.execute(
                    "SELECT reason FROM quota_ledger WHERE scan_id = ?",
                    (scan["id"],),
                ).fetchall()
            ]
        self.assertIn("scan_consumed", reasons)
        self.assertNotIn("scan_reservation_released", reasons)

    def test_timeout_recovery_uses_historical_core_event_after_cleanup_phase(self) -> None:
        scan = self.create_quota_scan("sc_cancel_timeout_historical_core")
        job = self.lease()
        with patch.object(app, "finalize_scan_quota_for_job", return_value={}):
            self.assertEqual(
                self.event(job, "phase_completed", 1, phase="repo_map").status,
                HTTPStatus.OK,
            )
            self.assertEqual(
                self.event(
                    job,
                    "phase_completed",
                    2,
                    phase="cleanup_active_job",
                ).status,
                HTTPStatus.OK,
            )
        requested = db.request_scan_job_cancellation(
            scan["id"],
            reason="historical_core_timeout",
            timeout_seconds=60,
            timestamp=app.now(),
        )

        self.assertEqual(
            app.recover_expired_scan_leases_once(int(requested["timeout_at"]) + 1),
            1,
        )

        stored_scan = db.get_user_scan_snapshot("usr_1", scan["id"])
        self.assertEqual(stored_scan["quotaState"], "consumed")
        usage = app.quota.quota_payload_for_user(app.USERS["usr_1"])
        self.assertEqual((usage["used"], usage["reserved"]), (1, 0))

    def test_historical_progress_steps_prevent_release_after_steps_are_dropped(
        self,
    ) -> None:
        scan = self.create_quota_scan("sc_cancel_historical_steps")
        job = self.lease()
        core_steps = [
            {
                "id": "repo_map",
                "status": "completed",
                "percent": 100,
            }
        ]
        first = self.event(
            job,
            "progress_updated",
            1,
            phase="failure_handling",
            steps=core_steps,
        )
        second = self.event(
            job,
            "phase_completed",
            2,
            phase="cleanup_active_job",
            steps=[],
        )
        self.assertEqual(first.status, HTTPStatus.OK, first.payload)
        self.assertEqual(second.status, HTTPStatus.OK, second.payload)
        stored_events = db.list_review_run_events(job["run_id"])
        self.assertEqual(
            json.loads(stored_events[0]["payload"])["progress"]["steps"],
            core_steps,
        )
        self.assertTrue(app.scan_job_has_billable_core_evidence(
            db.get_scan_job(job["job_id"])
        ))

        requested = self.post(
            f"/scans/{scan['id']}/cancel",
            {"reason": "historical core steps must be billed"},
        )

        self.assertEqual(requested.status, HTTPStatus.OK, requested.payload)
        stored_scan = db.get_user_scan_snapshot("usr_1", scan["id"])
        self.assertEqual(stored_scan["quotaState"], "consumed")
        usage = app.quota.quota_payload_for_user(app.USERS["usr_1"])
        self.assertEqual((usage["used"], usage["reserved"]), (1, 0))

    def test_refundable_release_replay_repairs_stale_scan_projection(self) -> None:
        scan = self.create_quota_scan("sc_refundable_release_replay")
        job = db.get_scan_job_for_scan(scan["id"])
        released = app.quota.release_scan_quota_reservation(
            scan_id=scan["id"],
            requested_by_user_id="usr_1",
            request_id=scan["requestId"],
            record_ledger=True,
        )
        self.assertEqual(released["ledgerRows"], 2)
        self.assertEqual(
            db.get_user_scan_snapshot("usr_1", scan["id"])["quotaState"],
            "reserved",
        )

        replayed = app.rollback_scan_quota_for_refundable_worker_failure(
            job,
            {"error_code": "CODEX_QUOTA_EXHAUSTED"},
            status="failed",
        )

        self.assertEqual(replayed["releasedRows"], 2)
        stored_scan = db.get_user_scan_snapshot("usr_1", scan["id"])
        self.assertEqual(stored_scan["quotaState"], "released")
        self.assertEqual(stored_scan["quotaReleaseReason"], "CODEX_QUOTA_EXHAUSTED")

    def test_refundable_release_replay_uses_durable_identity_and_keeps_timestamp(self) -> None:
        scan = self.create_quota_scan("sc_refundable_durable_replay")
        job = db.get_scan_job_for_scan(scan["id"])
        released = app.quota.release_scan_quota_reservation(
            scan_id=scan["id"],
            requested_by_user_id="usr_1",
            request_id=scan["requestId"],
            record_ledger=True,
        )
        self.assertEqual(released["ledgerRows"], 2)

        scan.pop("requestId", None)
        scan.pop("repoId", None)
        with patch.object(app, "now", return_value=100):
            first = app.rollback_scan_quota_for_refundable_worker_failure(
                job,
                {"error_code": "CODEX_QUOTA_EXHAUSTED"},
                status="failed",
            )

        self.assertEqual(first["releasedRows"], 2)
        first_projection = db.get_user_scan_snapshot("usr_1", scan["id"])
        self.assertEqual(first_projection["quotaState"], "released")
        self.assertEqual(first_projection["quotaReleasedAt"], 100)

        with patch.object(app, "now", return_value=200):
            second = app.rollback_scan_quota_for_refundable_worker_failure(
                job,
                {"error_code": "CODEX_QUOTA_EXHAUSTED"},
                status="failed",
            )

        self.assertEqual(second["releasedRows"], 2)
        second_projection = db.get_user_scan_snapshot("usr_1", scan["id"])
        self.assertEqual(second_projection["quotaReleasedAt"], 100)

    def test_refundable_consumed_rollback_preserves_durable_cancellation_fields(self) -> None:
        scan = self.create_quota_scan("sc_refundable_consumed_stale_memory")
        job = db.get_scan_job_for_scan(scan["id"])
        consumed = app.finalize_scan_quota_for_job(job, trigger="repo_map")
        self.assertTrue(consumed["consumed"])

        authoritative = db.get_user_scan_snapshot("usr_1", scan["id"])
        authoritative.update(
            {
                "status": "cancelled",
                "completedAt": 103,
                "cancelReason": "user requested cancellation",
                "cancelRequestedAt": 101,
            }
        )
        db.upsert_scan(authoritative)
        scan["status"] = "running"
        scan.pop("completedAt", None)
        scan.pop("cancelReason", None)
        scan.pop("cancelRequestedAt", None)

        rolled_back = app.rollback_scan_quota_for_refundable_worker_failure(
            job,
            {"error_code": "CODEX_QUOTA_EXHAUSTED"},
            status="failed",
        )

        self.assertEqual(rolled_back["ledgerRows"], 2)
        stored_scan = db.get_user_scan_snapshot("usr_1", scan["id"])
        self.assertEqual(stored_scan["quotaState"], "refunded")
        for key in (
            "status",
            "completedAt",
            "cancelReason",
            "cancelRequestedAt",
        ):
            self.assertEqual(stored_scan.get(key), authoritative.get(key), key)
            self.assertEqual(scan.get(key), authoritative.get(key), key)

    def test_cancelled_core_evidence_consumes_previously_released_quota_idempotently(self) -> None:
        scan = self.create_quota_scan("sc_cancel_released_then_core")
        job = self.lease()
        requested = self.post(f"/scans/{scan['id']}/cancel", {})
        self.assertEqual(requested.status, HTTPStatus.OK, requested.payload)
        stored_scan = db.get_user_scan_snapshot("usr_1", scan["id"])
        self.assertEqual(stored_scan["quotaState"], "released")

        audit_fields = audit_result_fields([], execution_status="cancelled")
        audit_fields["reviewWorkerProtocol"]["progress_final"].update(
            {
                "current_phase": "reviewer_fanout",
                "steps": [
                    {
                        "id": "reviewer_fanout",
                        "status": "completed",
                        "percent": 100,
                    }
                ],
            }
        )
        result_body = {
            "status": "cancelled",
            "attempt_id": f"wk_1-{job['attempt']}",
            "result_checksum": "checksum-cancelled-core-after-release",
            "error": "Cancelled after core review work.",
            **audit_fields,
        }
        receipt = self.post(
            f"/v1/review-runs/{job['run_id']}/result",
            result_body,
            worker=True,
        )

        self.assertEqual(receipt.status, HTTPStatus.OK, receipt.payload)
        self.assertTrue(receipt.payload["accepted"])
        self.assertTrue(receipt.payload["quotaConsumed"])
        stored_scan = db.get_user_scan_snapshot("usr_1", scan["id"])
        self.assertEqual(stored_scan["quotaState"], "consumed")
        usage = app.quota.quota_payload_for_user(app.USERS["usr_1"])
        self.assertEqual((usage["used"], usage["reserved"]), (1, 0))
        with db.connect() as connection:
            reasons_before = [
                row[0]
                for row in connection.execute(
                    "SELECT reason FROM quota_ledger WHERE scan_id = ? ORDER BY reason",
                    (scan["id"],),
                ).fetchall()
            ]
        self.assertIn("scan_reservation_released", reasons_before)
        self.assertIn("scan_consumed", reasons_before)

        duplicate = self.post(
            f"/v1/review-runs/{job['run_id']}/result",
            result_body,
            worker=True,
        )
        self.assertEqual(duplicate.status, HTTPStatus.OK, duplicate.payload)
        self.assertTrue(duplicate.payload["duplicate"])
        with db.connect() as connection:
            reasons_after = [
                row[0]
                for row in connection.execute(
                    "SELECT reason FROM quota_ledger WHERE scan_id = ? ORDER BY reason",
                    (scan["id"],),
                ).fetchall()
            ]
        self.assertEqual(reasons_after, reasons_before)
        duplicate_usage = app.quota.quota_payload_for_user(app.USERS["usr_1"])
        self.assertEqual(
            (duplicate_usage["used"], duplicate_usage["reserved"]),
            (1, 0),
        )

    def test_cancellation_timeout_recovery_consumes_reserved_core_work(self) -> None:
        scan = self.create_quota_scan("sc_cancel_timeout_core")
        job = self.lease()
        db.update_scan_job_progress(
            job["job_id"],
            {
                "phase": "repo_map",
                "progress": 45,
                "message": "core work persisted before cancellation timeout",
                "started_at": app.now(),
            },
        )
        requested = db.request_scan_job_cancellation(
            scan["id"],
            reason="user_requested",
            timeout_seconds=60,
            timestamp=app.now(),
        )
        deadline = int(requested["timeout_at"])

        self.assertEqual(app.recover_expired_scan_leases_once(deadline + 1), 1)

        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "cancelled")
        stored_scan = db.get_user_scan_snapshot("usr_1", scan["id"])
        self.assertEqual(stored_scan["quotaState"], "consumed")
        self.assertEqual(scan["quotaState"], "consumed")
        self.assertEqual(scan["quotaConsumedAt"], stored_scan["quotaConsumedAt"])
        self.assertEqual(
            db.get_scan_job(job["job_id"])["projection_pending"],
            0,
        )
        usage = app.quota.quota_payload_for_user(app.USERS["usr_1"])
        self.assertEqual((usage["used"], usage["reserved"]), (1, 0))

    def test_timeout_recovery_preserves_durable_cancellation_fields_over_stale_memory(
        self,
    ) -> None:
        scan = self.create_scan("sc_cancel_timeout_projection")
        job = self.lease()
        requested = self.post(
            f"/scans/{scan['id']}/cancel",
            {"reason": "keep timeout cancellation authority"},
        )
        self.assertEqual(requested.status, HTTPStatus.OK, requested.payload)
        durable_cancellation = db.get_user_scan_snapshot("usr_1", scan["id"])
        expected_cancel_reason = durable_cancellation["cancelReason"]
        expected_cancel_requested_at = durable_cancellation["cancelRequestedAt"]
        scan["status"] = "running"
        scan.pop("cancelReason", None)
        scan.pop("cancelRequestedAt", None)
        deadline = int(db.get_scan_job(job["job_id"])["timeout_at"])

        self.assertEqual(app.recover_expired_scan_leases_once(deadline + 1), 1)

        stored_scan = db.get_user_scan_snapshot("usr_1", scan["id"])
        self.assertEqual(stored_scan["status"], "cancelled")
        self.assertEqual(stored_scan["cancelReason"], expected_cancel_reason)
        self.assertEqual(
            stored_scan["cancelRequestedAt"],
            expected_cancel_requested_at,
        )
        self.assertEqual(app.SCANS[0]["cancelReason"], expected_cancel_reason)

    def test_continuous_recovery_repairs_reaper_projection_with_stored_late_receipt(
        self,
    ) -> None:
        scan = self.create_quota_scan("sc_cancel_receipt_projection_recovery")
        job = self.lease()
        requested = db.request_scan_job_cancellation(
            scan["id"],
            reason="keep authoritative cancellation",
            timeout_seconds=60,
            timestamp=app.now(),
        )
        deadline = int(requested["timeout_at"])
        recovered_jobs = db.recover_expired_scan_jobs(
            deadline + 1,
            worker_heartbeat_timeout_seconds=300,
        )
        self.assertEqual(len(recovered_jobs), 1)
        reaper_job = db.get_scan_job(job["job_id"])
        self.assertEqual(reaper_job["status"], "cancelled")

        audit_fields = audit_result_fields([], execution_status="cancelled")
        audit_fields["reviewWorkerProtocol"].setdefault("extensions", {}).setdefault(
            "worker_internal",
            {},
        )["artifact_upload_error"] = "reaper finalized before evidence upload"
        result_body = {
            "status": "cancelled",
            "attempt_id": f"wk_1-{job['attempt']}",
            "result_checksum": "checksum-reaper-projection-recovery",
            "error": "late worker cancellation",
            **audit_fields,
        }
        recorded = db.record_scan_job_result(
            job["job_id"],
            attempt_id=result_body["attempt_id"],
            status="cancelled",
            result_checksum=result_body["result_checksum"],
            payload=result_body,
        )
        self.assertTrue(recorded["accepted"])

        corrupted_scan = db.get_user_scan_snapshot("usr_1", scan["id"])
        corrupted_scan.update(
            {
                "status": "cancelled",
                "error": "late worker cancellation",
                "quotaState": "reserved",
            }
        )
        corrupted_scan.pop("recoveredAt", None)
        corrupted_scan.pop("recoveryReason", None)
        db.upsert_scan(corrupted_scan)
        scan.clear()
        scan.update(corrupted_scan)

        recovered = app.recover_expired_scan_leases_once(deadline + 2)

        self.assertGreaterEqual(recovered, 1)
        stored_scan = db.get_user_scan_snapshot("usr_1", scan["id"])
        self.assertEqual(stored_scan["status"], "cancelled")
        self.assertEqual(
            stored_scan["error"],
            "keep authoritative cancellation",
        )
        self.assertEqual(stored_scan["recoveryReason"], "cancel_timed_out")
        self.assertEqual(stored_scan["quotaState"], "released")
        self.assertEqual(
            stored_scan["resultChecksum"],
            result_body["result_checksum"],
        )
        usage = app.quota.quota_payload_for_user(app.USERS["usr_1"])
        self.assertEqual((usage["used"], usage["reserved"]), (0, 0))

    def test_restart_preserves_unexpired_cancel_request_from_stale_running_mirror(self) -> None:
        scan = self.create_scan("sc_cancel_restart")
        job = self.lease()
        requested_at = app.now()
        requested_job = db.request_scan_job_cancellation(
            scan["id"],
            reason="user_requested",
            timeout_seconds=600,
            timestamp=requested_at,
        )
        self.assertEqual(requested_job["status"], "cancel_requested")
        self.assertEqual(scan["status"], "running")

        with patch.object(app, "now", return_value=requested_at + 1):
            app.recover_interrupted_scans()

        stored_job = db.get_scan_job(job["job_id"])
        self.assertEqual(stored_job["status"], "cancel_requested")
        self.assertEqual(scan["status"], "cancel_requested")
        self.assertNotIn("completedAt", scan)
        self.assertEqual(db.list_scan_job_attempts(job["job_id"])[0]["status"], "claimed")
        self.assertEqual(db.get_review_run(job["run_id"])["status"], "leased")

    def test_cancellation_pending_blocks_same_worker_from_claiming_next_job(self) -> None:
        first_scan = self.create_scan("sc_cancel_slot_first")
        first_job = self.lease()
        requested = self.post(f"/scans/{first_scan['id']}/cancel", {})
        self.assertEqual(requested.status, HTTPStatus.OK)
        self.assertEqual(db.get_scan_job(first_job["job_id"])["status"], "cancel_requested")
        second_scan = self.create_scan("sc_cancel_slot_second")
        second_job = db.get_scan_job_for_scan(second_scan["id"])

        next_lease = self.post(
            "/v1/workers/wk_1/lease",
            v1_worker_lease_payload(),
            worker=True,
        )

        self.assertEqual(next_lease.status, HTTPStatus.OK)
        self.assertIsNone(next_lease.payload["job"])
        self.assertEqual(db.get_scan_job(second_job["job_id"])["status"], "queued")

if __name__ == "__main__":
    unittest.main()
