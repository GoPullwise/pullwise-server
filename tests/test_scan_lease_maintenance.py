from __future__ import annotations

import json
import os
import tempfile
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
        ):
            server.service_actions()

        stored_job = db.get_scan_job(claimed_job["job_id"])
        attempts = db.list_scan_job_attempts(claimed_job["job_id"])
        review_run = db.get_review_run(run_id)
        stored_scan = db.get_user_scan_snapshot(user["id"], scan["id"])
        self.assertEqual(stored_job["status"], "failed")
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
            patch.object(app.time, "monotonic", return_value=102.0),
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


if __name__ == "__main__":
    unittest.main()
