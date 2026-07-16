from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from pullwise_server import app, db
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections


class ScanLeaseMaintenanceTest(unittest.TestCase):
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
                "PULLWISE_SCAN_JOB_LEASE_RECOVERY_INTERVAL_SECONDS": "1",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        app.USERS = {}
        app.SESSIONS = {}
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

    def test_service_actions_reaps_expired_lease_and_converges_terminal_state(self) -> None:
        claimed_at = app.now()
        user = {"id": "usr_1", "name": "Owner", "providers": []}
        app.USERS = {user["id"]: user}
        repository = db.upsert_repository(
            {
                "github_repo_id": "lease-maintenance-repo",
                "full_name": "acme/lease-maintenance",
                "owner_login": "acme",
                "default_branch": "main",
            }
        )
        quota_result = app.quota.reserve_scan_quota(
            user=user,
            repository=repository,
            requested_by_user_id=user["id"],
            scan_id="sc_lease_maintenance",
            request_id="req_lease_maintenance",
        )
        scan = {
            "id": "sc_lease_maintenance",
            "repo": repository["full_name"],
            "branch": "main",
            "commit": "abc1234",
            "status": "queued",
            "userId": user["id"],
            "createdAt": claimed_at,
            "queuedAt": claimed_at,
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "repoId": repository["id"],
            "githubRepoId": repository["github_repo_id"],
            "requestId": "req_lease_maintenance",
            "quotaBucketIds": quota_result["bucketIds"],
            "billingUsage": quota_result["user"],
            "repoUsage": quota_result["repository"],
            "quotaState": "reserved",
            "quotaReservedAt": claimed_at,
        }
        app.SCANS = [scan]
        queued_job = app.create_scan_job_for_scan(scan)
        db.upsert_worker_heartbeat(
            {
                "worker_id": "wk_1",
                "version": "0.1.0",
                "provider": "codex",
                "provider_chain": ["codex"],
                "running_jobs": 1,
                "doctor_status": "ok",
                "codex_ready": 1,
                "ready_providers": ["codex"],
                "timestamp": claimed_at,
            }
        )
        claimed_job = db.claim_next_scan_job(
            "wk_1",
            lease_seconds=60,
            timestamp=claimed_at,
            recover_before_claim=False,
            create_review_run=True,
            protocol_version="review-worker-protocol/v1",
        )
        self.assertEqual(claimed_job["job_id"], queued_job["job_id"])
        scan.update(
            {
                "status": "running",
                "jobId": claimed_job["job_id"],
                "runId": app.scan_job_run_id(claimed_job),
                "claimedAt": claimed_job["claimed_at"],
                "claimedByWorkerId": "wk_1",
            }
        )
        db.upsert_scan(scan)
        run_id = app.scan_job_run_id(claimed_job)
        expires_at = int(claimed_job["timeout_at"])
        self.assertEqual(db.get_review_run(run_id)["status"], "leased")
        self.assertEqual(app.quota.quota_payload_for_user(user)["reserved"], 1)

        server = object.__new__(app.PullwiseThreadingHTTPServer)
        server._last_scan_job_lease_recovery_at = 0.0
        with (
            patch.object(app, "now", return_value=expires_at + 1),
            patch.object(app.time, "monotonic", return_value=100.0),
            patch.object(app, "apply_recovered_scan_jobs_locked", side_effect=RuntimeError("projection failed")),
            patch.object(app.logger, "exception"),
        ):
            server.service_actions()

        self.assertEqual(db.get_scan_job(claimed_job["job_id"])["status"], "failed")
        self.assertEqual(
            db.get_scan_job(claimed_job["job_id"])["projection_pending"],
            1,
        )
        self.assertEqual(db.get_review_run(run_id)["status"], "failed")
        self.assertEqual(scan["status"], "running")
        self.assertEqual(app.quota.quota_payload_for_user(user)["reserved"], 1)
        with (
            patch.object(app, "now", return_value=expires_at + 2),
            patch.object(app.time, "monotonic", return_value=102.0),
        ):
            server.service_actions()

        stored_job = db.get_scan_job(claimed_job["job_id"])
        attempts = db.list_scan_job_attempts(claimed_job["job_id"])
        review_run = db.get_review_run(run_id)
        stored_scan = db.get_user_scan_snapshot(user["id"], scan["id"])
        self.assertEqual(stored_job["status"], "failed")
        self.assertEqual(stored_job["projection_pending"], 0)
        self.assertEqual(stored_job["error"], "timed_out")
        self.assertIsNone(stored_job["timeout_at"])
        self.assertEqual(attempts[0]["status"], "failed")
        self.assertEqual(attempts[0]["error"], "timed_out")
        self.assertEqual(review_run["status"], "failed")
        self.assertEqual(review_run["result_status"], "failed")
        self.assertEqual(json.loads(review_run["error_json"])["code"], "timed_out")
        self.assertEqual(json.loads(review_run["error_json"])["source"], "server_lease_reaper")
        self.assertEqual(json.loads(review_run["progress_json"])["status"], "failed")
        self.assertEqual(scan["status"], "failed")
        self.assertEqual(scan["recoveryReason"], "timed_out")
        self.assertEqual(scan["quotaState"], "released")
        self.assertEqual(stored_scan["status"], "failed")
        self.assertEqual(stored_scan["recoveryReason"], "timed_out")
        self.assertEqual(stored_scan["quotaState"], "released")
        self.assertEqual(app.quota.quota_payload_for_user(user)["used"], 0)
        self.assertEqual(app.quota.quota_payload_for_user(user)["reserved"], 0)

        terminal_snapshot = {
            "job": db.get_scan_job(claimed_job["job_id"]),
            "attempts": db.list_scan_job_attempts(claimed_job["job_id"]),
            "review_run": db.get_review_run(run_id),
            "scan": db.get_user_scan_snapshot(user["id"], scan["id"]),
        }
        with (
            patch.object(app, "now", return_value=expires_at + 10),
            patch.object(app.time, "monotonic", return_value=104.0),
        ):
            server.service_actions()
        self.assertEqual(
            {
                "job": db.get_scan_job(claimed_job["job_id"]),
                "attempts": db.list_scan_job_attempts(claimed_job["job_id"]),
                "review_run": db.get_review_run(run_id),
                "scan": db.get_user_scan_snapshot(user["id"], scan["id"]),
            },
            terminal_snapshot,
        )

        heartbeat_update = db.record_active_worker_heartbeat(
            {
                "worker_id": "wk_1",
                "version": "0.1.0",
                "provider": "codex",
                "running_jobs": 1,
                "doctor_status": "ok",
                "timestamp": expires_at + 11,
            },
            [claimed_job["job_id"]],
            lease_seconds=3600,
            timestamp=expires_at + 11,
        )
        self.assertEqual(heartbeat_update["accepting"], [])
        self.assertEqual(heartbeat_update["no_longer_accepting"], [claimed_job["job_id"]])
        self.assertEqual(heartbeat_update["renewed_count"], 0)
        self.assertEqual(db.get_scan_job(claimed_job["job_id"])["status"], "failed")
        self.assertIsNone(db.get_scan_job(claimed_job["job_id"])["timeout_at"])

    def test_serve_forever_runs_lease_recovery_while_http_server_is_idle(self) -> None:
        recovery_called = threading.Event()
        server = app.PullwiseThreadingHTTPServer(("127.0.0.1", 0), app.PullwiseHandler)
        server._last_scan_job_lease_recovery_at = 0.0
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        try:
            with patch.object(
                app,
                "recover_expired_scan_leases_once",
                side_effect=lambda: (recovery_called.set(), 0)[1],
            ):
                thread.start()
                self.assertTrue(recovery_called.wait(2), "idle serve_forever never ran lease recovery")
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()
        self.assertFalse(thread.is_alive())

    def test_cold_core_phase_recovery_persists_consumed_quota_without_pending_replay(self) -> None:
        claimed_at = app.now()
        user = {"id": "usr_1", "name": "Owner", "providers": []}
        app.USERS = {user["id"]: user}
        repository = db.upsert_repository(
            {
                "github_repo_id": "cold-core-recovery-repo",
                "full_name": "acme/cold-core-recovery",
                "owner_login": "acme",
                "default_branch": "main",
            }
        )
        quota_result = app.quota.reserve_scan_quota(
            user=user,
            repository=repository,
            requested_by_user_id=user["id"],
            scan_id="sc_cold_core_recovery",
            request_id="req_cold_core_recovery",
        )
        scan = {
            "id": "sc_cold_core_recovery",
            "repo": repository["full_name"],
            "branch": "main",
            "commit": "abc1234",
            "status": "queued",
            "userId": user["id"],
            "createdAt": claimed_at,
            "queuedAt": claimed_at,
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "repoId": repository["id"],
            "githubRepoId": repository["github_repo_id"],
            "requestId": "req_cold_core_recovery",
            "quotaBucketIds": quota_result["bucketIds"],
            "billingUsage": quota_result["user"],
            "repoUsage": quota_result["repository"],
            "quotaState": "reserved",
            "quotaReservedAt": claimed_at,
        }
        app.SCANS = [scan]
        queued_job = app.create_scan_job_for_scan(scan)
        db.upsert_worker_heartbeat(
            {
                "worker_id": "wk_1",
                "provider": "codex",
                "running_jobs": 1,
                "doctor_status": "ok",
                "timestamp": claimed_at,
            }
        )
        job = db.claim_next_scan_job(
            "wk_1",
            lease_seconds=60,
            timestamp=claimed_at,
            recover_before_claim=False,
            create_review_run=True,
            protocol_version="review-worker-protocol/v1",
        )
        self.assertEqual(job["job_id"], queued_job["job_id"])
        db.update_scan_job_progress(
            job["job_id"],
            {
                "phase": "repo_map",
                "progress": 45,
                "message": "mapping repository",
                "started_at": claimed_at,
            },
        )
        scan.update(
            {
                "status": "running",
                "phase": "repo_map",
                "jobId": job["job_id"],
                "runId": app.scan_job_run_id(job),
            }
        )
        db.upsert_scan(scan)
        expires_at = int(job["timeout_at"])
        app.SCANS = []

        recovered = app.recover_expired_scan_leases_once(expires_at + 1)

        stored_scan = db.get_user_scan_snapshot(user["id"], scan["id"])
        self.assertEqual(recovered, 1)
        self.assertEqual((stored_scan["status"], stored_scan["quotaState"]), ("failed", "consumed"))
        self.assertEqual(app.quota.quota_payload_for_user(user)["used"], 1)
        self.assertEqual(app.quota.quota_payload_for_user(user)["reserved"], 0)
        self.assertEqual(app.recover_expired_scan_leases_once(expires_at + 2), 0)

    def test_pending_release_projection_repairs_reserved_payload_after_ledger_was_released(self) -> None:
        timestamp = app.now()
        user = {"id": "usr_1", "name": "Owner", "providers": []}
        app.USERS = {user["id"]: user}
        repository = db.upsert_repository(
            {
                "github_repo_id": "released-ledger-recovery-repo",
                "full_name": "acme/released-ledger-recovery",
                "owner_login": "acme",
                "default_branch": "main",
            }
        )
        quota_result = app.quota.reserve_scan_quota(
            user=user,
            repository=repository,
            requested_by_user_id=user["id"],
            scan_id="sc_released_ledger_recovery",
            request_id="req_released_ledger_recovery",
        )
        scan = {
            "id": "sc_released_ledger_recovery",
            "repo": repository["full_name"],
            "branch": "main",
            "commit": "abc1234",
            "status": "queued",
            "userId": user["id"],
            "createdAt": timestamp,
            "queuedAt": timestamp,
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "repoId": repository["id"],
            "githubRepoId": repository["github_repo_id"],
            "requestId": "req_released_ledger_recovery",
            "quotaBucketIds": quota_result["bucketIds"],
            "billingUsage": quota_result["user"],
            "repoUsage": quota_result["repository"],
            "quotaState": "reserved",
            "quotaReservedAt": timestamp,
        }
        app.SCANS = [scan]
        job = app.create_scan_job_for_scan(scan)
        release = app.quota.release_scan_quota_reservation(
            scan_id=scan["id"],
            requested_by_user_id=user["id"],
            request_id=scan["requestId"],
            record_ledger=True,
        )
        self.assertGreater(release["ledgerRows"], 0)
        with db.connect() as connection, connection:
            connection.execute(
                """
                UPDATE scan_jobs
                SET status = 'failed', error = 'timed_out', completed_at = ?,
                    updated_at = ?, projection_pending = 1
                WHERE job_id = ?
                """,
                (timestamp + 1, timestamp + 1, job["job_id"]),
            )
        app.SCANS = []

        recovered = app.recover_expired_scan_leases_once(timestamp + 2)

        stored_scan = db.get_user_scan_snapshot(user["id"], scan["id"])
        self.assertEqual(recovered, 1)
        self.assertEqual((stored_scan["status"], stored_scan["quotaState"]), ("failed", "released"))
        self.assertEqual(app.quota.quota_payload_for_user(user)["reserved"], 0)
        self.assertEqual(app.recover_expired_scan_leases_once(timestamp + 3), 0)

    def test_pending_recovery_query_uses_projection_pending_index(self) -> None:
        with db.connect() as connection:
            plan = connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT sj.job_id, sj.scan_id, sj.status, sj.error, sj.attempt
                FROM scan_jobs sj
                JOIN scans s ON s.scan_id = sj.scan_id
                WHERE sj.projection_pending = 1
                ORDER BY sj.updated_at ASC, sj.job_id ASC
                LIMIT 500
                """
            ).fetchall()

        details = " ".join(str(row[3]) for row in plan)
        self.assertIn("idx_scan_jobs_projection_pending", details)

    def test_stale_worker_recovery_finalizes_attempt_and_review_run_without_waiting_for_lease_expiry(self) -> None:
        claimed_at = app.now()
        scan = {
            "id": "sc_stale_worker",
            "repo": "acme/stale-worker",
            "branch": "main",
            "commit": "abc1234",
            "status": "queued",
            "userId": "usr_1",
            "createdAt": claimed_at,
            "queuedAt": claimed_at,
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS = [scan]
        queued_job = app.create_scan_job_for_scan(scan)
        db.upsert_worker_heartbeat(
            {
                "worker_id": "wk_1",
                "version": "0.1.0",
                "provider": "codex",
                "provider_chain": ["codex"],
                "running_jobs": 1,
                "doctor_status": "ok",
                "codex_ready": 1,
                "ready_providers": ["codex"],
                "timestamp": claimed_at,
            }
        )
        claimed_job = db.claim_next_scan_job(
            "wk_1",
            lease_seconds=3600,
            timestamp=claimed_at,
            recover_before_claim=False,
            create_review_run=True,
            protocol_version="review-worker-protocol/v1",
        )
        self.assertEqual(claimed_job["job_id"], queued_job["job_id"])
        self.assertGreater(int(claimed_job["timeout_at"]), claimed_at + 121)

        recovered = db.recover_expired_scan_jobs(
            claimed_at + 121,
            worker_heartbeat_timeout_seconds=60,
        )

        self.assertEqual(
            [(item["job_id"], item["reason"]) for item in recovered],
            [(claimed_job["job_id"], "worker_heartbeat_timed_out")],
        )
        stored_job = db.get_scan_job(claimed_job["job_id"])
        attempt = db.list_scan_job_attempts(claimed_job["job_id"])[0]
        review_run = db.get_review_run(app.scan_job_run_id(claimed_job))
        self.assertEqual((stored_job["status"], stored_job["error"]), ("failed", "worker_heartbeat_timed_out"))
        self.assertEqual((attempt["status"], attempt["error"]), ("failed", "worker_heartbeat_timed_out"))
        self.assertEqual((review_run["status"], review_run["result_status"]), ("failed", "failed"))
        self.assertEqual(json.loads(review_run["error_json"])["code"], "worker_heartbeat_timed_out")
        heartbeat = db.record_active_worker_heartbeat(
            {"worker_id": "wk_1", "running_jobs": 1, "timestamp": claimed_at + 122},
            [claimed_job["job_id"]],
            lease_seconds=3600,
            timestamp=claimed_at + 122,
        )
        self.assertEqual(heartbeat["renewed_count"], 0)
        self.assertEqual(heartbeat["no_longer_accepting"], [claimed_job["job_id"]])

    def test_expired_cancelling_lease_converges_cancelled_and_rejects_late_heartbeat(self) -> None:
        claimed_at = app.now()
        user = {"id": "usr_1", "name": "Owner", "providers": []}
        app.USERS = {user["id"]: user}
        repository = db.upsert_repository(
            {
                "github_repo_id": "cancel-timeout-repo",
                "full_name": "acme/cancel-timeout",
                "owner_login": "acme",
                "default_branch": "main",
            }
        )
        quota_result = app.quota.reserve_scan_quota(
            user=user,
            repository=repository,
            requested_by_user_id=user["id"],
            scan_id="sc_cancel_timeout",
            request_id="req_cancel_timeout",
        )
        scan = {
            "id": "sc_cancel_timeout",
            "repo": repository["full_name"],
            "branch": "main",
            "commit": "abc1234",
            "status": "queued",
            "userId": user["id"],
            "createdAt": claimed_at,
            "queuedAt": claimed_at,
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "repoId": repository["id"],
            "githubRepoId": repository["github_repo_id"],
            "requestId": "req_cancel_timeout",
            "quotaBucketIds": quota_result["bucketIds"],
            "billingUsage": quota_result["user"],
            "repoUsage": quota_result["repository"],
            "quotaState": "reserved",
            "quotaReservedAt": claimed_at,
        }
        app.SCANS = [scan]
        queued_job = app.create_scan_job_for_scan(scan)
        db.upsert_worker_heartbeat(
            {
                "worker_id": "wk_1",
                "version": "0.1.0",
                "provider": "codex",
                "provider_chain": ["codex"],
                "running_jobs": 1,
                "doctor_status": "ok",
                "codex_ready": 1,
                "ready_providers": ["codex"],
                "timestamp": claimed_at,
            }
        )
        claimed_job = db.claim_next_scan_job(
            "wk_1",
            lease_seconds=60,
            timestamp=claimed_at,
            recover_before_claim=False,
            create_review_run=True,
            protocol_version="review-worker-protocol/v1",
        )
        self.assertEqual(claimed_job["job_id"], queued_job["job_id"])
        scan.update(
            {
                "status": "cancelling",
                "jobId": claimed_job["job_id"],
                "runId": app.scan_job_run_id(claimed_job),
                "claimedAt": claimed_job["claimed_at"],
                "claimedByWorkerId": "wk_1",
            }
        )
        db.upsert_scan(scan)
        with db.connect() as connection, connection:
            connection.execute(
                "UPDATE scan_jobs SET status = 'cancelling', cancel_requested_at = ?, cancel_reason = ? WHERE job_id = ?",
                (claimed_at, "user_requested", claimed_job["job_id"]),
            )
        expires_at = int(claimed_job["timeout_at"])
        server = object.__new__(app.PullwiseThreadingHTTPServer)
        server._last_scan_job_lease_recovery_at = 0.0
        with (
            patch.object(app, "now", return_value=expires_at + 1),
            patch.object(app.time, "monotonic", return_value=100.0),
        ):
            server.service_actions()
        stored_job = db.get_scan_job(claimed_job["job_id"])
        attempt = db.list_scan_job_attempts(claimed_job["job_id"])[0]
        review_run = db.get_review_run(app.scan_job_run_id(claimed_job))
        self.assertEqual(
            (stored_job["status"], stored_job["error"], stored_job["cancel_reason"]),
            ("cancelled", "cancel_timed_out", "user_requested"),
        )
        self.assertEqual((attempt["status"], attempt["error"]), ("cancelled", "cancel_timed_out"))
        self.assertEqual((review_run["status"], review_run["result_status"]), ("cancelled", "cancelled"))
        self.assertEqual(json.loads(review_run["error_json"])["code"], "cancel_timed_out")
        self.assertEqual(
            (scan["status"], scan["error"], scan["recoveryReason"]),
            ("cancelled", "user_requested", "cancel_timed_out"),
        )
        stored_scan = db.get_user_scan_snapshot(user["id"], scan["id"])
        self.assertEqual(scan["quotaState"], "released")
        self.assertEqual(stored_scan["quotaState"], "released")
        self.assertEqual(app.quota.quota_payload_for_user(user)["used"], 0)
        self.assertEqual(app.quota.quota_payload_for_user(user)["reserved"], 0)
        heartbeat = db.record_active_worker_heartbeat(
            {"worker_id": "wk_1", "running_jobs": 1, "timestamp": expires_at + 2},
            [claimed_job["job_id"]],
            lease_seconds=3600,
            timestamp=expires_at + 2,
        )
        self.assertEqual(heartbeat["renewed_count"], 0)
        self.assertEqual(heartbeat["no_longer_accepting"], [claimed_job["job_id"]])


if __name__ == "__main__":
    unittest.main()
