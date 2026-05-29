from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app, db


class RouteHarness(app.PullwiseHandler):
    def __init__(self, path: str, body: dict | None = None, *, headers: dict | None = None) -> None:
        self.path = path
        self._body = body or {}
        self._raw_body = json.dumps(self._body).encode("utf-8")
        self.headers = {"Host": "api.pullwise.dev", **(headers or {})}
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


class WorkerPullRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "PULLWISE_DB_PATH": os.path.join(self.temp_dir.name, "pullwise.sqlite3"),
                "PULLWISE_WORKER_TOKEN": "worker-secret",
                "PULLWISE_WORKER_ID": "wk_1",
                "PULLWISE_REVIEW_PROVIDER": "mock",
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
        db.initialize()
        self.auth = {"Authorization": "Bearer worker-secret"}

    def create_registry_worker(self, worker_id: str) -> tuple[dict, str]:
        worker = db.create_worker({"worker_id": worker_id, "name": worker_id, "provider": "codex"})
        return worker, worker["worker_token"]

    def test_worker_heartbeat_claim_progress_and_result_are_idempotent(self) -> None:
        scan = {
            "id": "sc_1",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc123",
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "installationId": "111",
            "repoId": "repo_123",
            "githubRepoId": "123",
            "cloneUrl": "https://github.com/acme/api.git",
        }
        app.SCANS = [scan]
        job = app.create_scan_job_for_scan(scan)

        heartbeat = RouteHarness(
            "/worker/heartbeat",
            {
                "worker_id": "wk_1",
                "version": "0.1.0",
                "provider": "codex",
                "max_concurrent_jobs": 1,
                "running_jobs": 0,
                "free_slots": 1,
                "hostname": "builder-1",
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(heartbeat, "POST")
        self.assertEqual(heartbeat.status, HTTPStatus.OK)
        self.assertEqual(heartbeat.payload["worker"]["worker_id"], "wk_1")

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)
        self.assertEqual(claim.payload["job"]["job_id"], job["job_id"])
        self.assertEqual(claim.payload["job"]["status"], "claimed")
        self.assertEqual(len(claim.payload["jobs"]), 1)
        self.assertEqual(claim.payload["job"]["scan_id"], "sc_1")
        self.assertEqual(app.SCANS[0]["status"], "running")
        self.assertEqual(app.SCANS[0]["claimedByWorkerId"], "wk_1")

        second_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_2"}, headers=self.auth)
        app.PullwiseHandler.route(second_claim, "POST")
        self.assertEqual(second_claim.status, HTTPStatus.FORBIDDEN)

        progress = RouteHarness(
            f"/worker/jobs/{job['job_id']}/progress",
            {"phase": "ai", "progress": 70, "message": "reviewing", "logs_summary": "ok"},
            headers=self.auth,
        )
        app.PullwiseHandler.route(progress, "POST")
        self.assertEqual(progress.status, HTTPStatus.OK)
        self.assertEqual(progress.payload["job"]["status"], "running")
        self.assertEqual(app.SCANS[0]["phase"], "ai")
        self.assertEqual(app.SCANS[0]["progress"], 70)

        result_body = {
            "status": "done",
            "attempt_id": "wk_1-1",
            "findings": [{"severity": "high", "title": "Hardcoded token", "file": "app.py", "line": 12}],
            "summary": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
            "duration_ms": 1234,
            "result_checksum": "checksum-1",
        }
        result = RouteHarness(f"/worker/jobs/{job['job_id']}/result", result_body, headers=self.auth)
        app.PullwiseHandler.route(result, "POST")
        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertTrue(result.payload["accepted"])
        self.assertEqual(app.SCANS[0]["status"], "done")
        self.assertEqual(len(app.ISSUES), 1)

        duplicate = RouteHarness(f"/worker/jobs/{job['job_id']}/result", result_body, headers=self.auth)
        app.PullwiseHandler.route(duplicate, "POST")
        self.assertEqual(duplicate.status, HTTPStatus.OK)
        self.assertTrue(duplicate.payload["duplicate"])

        conflict_body = {**result_body, "result_checksum": "checksum-2"}
        conflict = RouteHarness(f"/worker/jobs/{job['job_id']}/result", conflict_body, headers=self.auth)
        app.PullwiseHandler.route(conflict, "POST")
        self.assertEqual(conflict.status, HTTPStatus.CONFLICT)

        late_attempt = RouteHarness(
            f"/worker/jobs/{job['job_id']}/result",
            {**result_body, "attempt_id": "wk_1-2", "result_checksum": "checksum-3"},
            headers=self.auth,
        )
        app.PullwiseHandler.route(late_attempt, "POST")
        self.assertEqual(late_attempt.status, HTTPStatus.CONFLICT)
        self.assertEqual(len(app.ISSUES), 1)

    def test_claim_payload_includes_short_lived_clone_token_when_github_app_is_configured(self) -> None:
        job = {
            "job_id": "job_token",
            "scan_id": "sc_token",
            "repo": "acme/api",
            "branch": "main",
            "commit": "pending",
            "status": "claimed",
            "attempt": 1,
            "installation_id": "111",
            "clone_url": "https://github.com/acme/api.git",
        }

        with (
            patch.object(app.github_auth, "app_api_configured", return_value=True),
            patch.object(
                app.github_auth,
                "create_installation_access_token",
                return_value={"token": "short-token", "expires_at": "2026-05-29T12:00:00Z"},
            ) as create_token,
        ):
            payload = app.scan_job_payload(job, include_clone_token=True)

        create_token.assert_called_once_with("111")
        self.assertEqual(payload["clone_token"]["token"], "short-token")
        self.assertEqual(payload["clone_token"]["repo"], "acme/api")

    def test_worker_routes_require_enabled_token(self) -> None:
        denied = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"})
        app.PullwiseHandler.route(denied, "POST")
        self.assertEqual(denied.status, HTTPStatus.UNAUTHORIZED)

    def test_worker_token_cannot_impersonate_another_worker_or_claimed_job(self) -> None:
        scan = {
            "id": "sc_owner",
            "repo": "acme/api",
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
        }
        app.SCANS = [scan]
        job = app.create_scan_job_for_scan(scan)

        wrong_worker_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_2"}, headers=self.auth)
        app.PullwiseHandler.route(wrong_worker_claim, "POST")
        self.assertEqual(wrong_worker_claim.status, HTTPStatus.FORBIDDEN)

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)

        _other_payload, other_token = self.create_registry_worker("wk_2")
        wrong_progress = RouteHarness(
            f"/worker/jobs/{job['job_id']}/progress",
            {"phase": "ai", "progress": 50},
            headers={"Authorization": f"Bearer {other_token}"},
        )
        app.PullwiseHandler.route(wrong_progress, "POST")
        self.assertEqual(wrong_progress.status, HTTPStatus.FORBIDDEN)

        wrong_result = RouteHarness(
            f"/worker/jobs/{job['job_id']}/result",
            {"status": "done", "attempt_id": "wk_2-1", "result_checksum": "bad", "findings": []},
            headers={"Authorization": f"Bearer {other_token}"},
        )
        app.PullwiseHandler.route(wrong_result, "POST")
        self.assertEqual(wrong_result.status, HTTPStatus.FORBIDDEN)

    def test_worker_can_claim_multiple_jobs_up_to_capacity_and_limits(self) -> None:
        for index, user_id in enumerate(["usr_1", "usr_2", "usr_3"], start=1):
            scan = {
                "id": f"sc_{index}",
                "repo": f"acme/api-{index}",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "userId": user_id,
                "createdAt": app.now() + index,
                "queuedAt": app.now() + index,
                "progress": 0,
                "phase": None,
                "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                "repoId": f"repo_{index}",
                "githubRepoId": str(index),
            }
            app.SCANS.append(scan)
            app.create_scan_job_for_scan(scan)

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1", "max_jobs": 3}, headers=self.auth)
        with patch.dict(
            os.environ,
            {"PULLWISE_MAX_RUNNING_SCANS_GLOBAL": "2", "PULLWISE_MAX_RUNNING_SCANS_PER_USER": "1"},
            clear=False,
        ):
            app.PullwiseHandler.route(claim, "POST")

        self.assertEqual(claim.status, HTTPStatus.OK)
        self.assertEqual([job["scan_id"] for job in claim.payload["jobs"]], ["sc_1", "sc_2"])
        self.assertEqual(claim.payload["jobs"][0]["status"], "claimed")

    def test_multi_worker_queue_claims_progress_and_results_complete_without_duplicate_claims(self) -> None:
        _worker_two, worker_two_token = self.create_registry_worker("wk_2")
        worker_two_auth = {"Authorization": f"Bearer {worker_two_token}"}
        for index in range(1, 6):
            scan = {
                "id": f"sc_multi_{index}",
                "repo": f"acme/api-{index}",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "userId": f"usr_{index}",
                "createdAt": app.now() + index,
                "queuedAt": app.now() + index,
                "progress": 0,
                "phase": None,
                "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                "repoId": f"repo_multi_{index}",
                "githubRepoId": f"multi_{index}",
            }
            app.SCANS.append(scan)
            app.create_scan_job_for_scan(scan)

        with patch.dict(
            os.environ,
            {"PULLWISE_MAX_RUNNING_SCANS_GLOBAL": "4", "PULLWISE_MAX_RUNNING_SCANS_PER_USER": "1"},
            clear=False,
        ):
            first_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1", "max_jobs": 2}, headers=self.auth)
            second_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_2", "max_jobs": 2}, headers=worker_two_auth)
            app.PullwiseHandler.route(first_claim, "POST")
            app.PullwiseHandler.route(second_claim, "POST")

        self.assertEqual(first_claim.status, HTTPStatus.OK)
        self.assertEqual(second_claim.status, HTTPStatus.OK)
        first_jobs = first_claim.payload["jobs"]
        second_jobs = second_claim.payload["jobs"]
        claimed_job_ids = [job["job_id"] for job in first_jobs + second_jobs]
        claimed_scan_ids = [job["scan_id"] for job in first_jobs + second_jobs]
        self.assertEqual(len(claimed_job_ids), 4)
        self.assertEqual(len(set(claimed_job_ids)), 4)
        self.assertEqual(claimed_scan_ids, ["sc_multi_1", "sc_multi_2", "sc_multi_3", "sc_multi_4"])
        self.assertEqual(app.SCANS[4]["status"], "queued")
        queue = app.scan_queue_payload(app.SCANS[4])
        self.assertEqual(queue["position"], 1)
        self.assertEqual(queue["ahead"], 0)

        for worker_id, auth, jobs in (("wk_1", self.auth, first_jobs), ("wk_2", worker_two_auth, second_jobs)):
            for job in jobs:
                progress = RouteHarness(
                    f"/worker/jobs/{job['job_id']}/progress",
                    {"phase": "ai", "progress": 80, "message": f"{worker_id} reviewing"},
                    headers=auth,
                )
                app.PullwiseHandler.route(progress, "POST")
                self.assertEqual(progress.status, HTTPStatus.OK)
                result = RouteHarness(
                    f"/worker/jobs/{job['job_id']}/result",
                    {
                        "status": "done",
                        "attempt_id": f"{worker_id}-{job['attempt']}",
                        "result_checksum": f"checksum-{job['job_id']}",
                        "findings": [{"severity": "medium", "title": f"Finding {job['scan_id']}"}],
                        "summary": {"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 0},
                    },
                    headers=auth,
                )
                app.PullwiseHandler.route(result, "POST")
                self.assertEqual(result.status, HTTPStatus.OK)

        with patch.dict(
            os.environ,
            {"PULLWISE_MAX_RUNNING_SCANS_GLOBAL": "4", "PULLWISE_MAX_RUNNING_SCANS_PER_USER": "1"},
            clear=False,
        ):
            next_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1", "max_jobs": 2}, headers=self.auth)
            app.PullwiseHandler.route(next_claim, "POST")

        self.assertEqual(next_claim.status, HTTPStatus.OK)
        self.assertEqual([job["scan_id"] for job in next_claim.payload["jobs"]], ["sc_multi_5"])
        last_job = next_claim.payload["job"]
        final_result = RouteHarness(
            f"/worker/jobs/{last_job['job_id']}/result",
            {
                "status": "done",
                "attempt_id": f"wk_1-{last_job['attempt']}",
                "result_checksum": f"checksum-{last_job['job_id']}",
                "findings": [],
                "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(final_result, "POST")
        self.assertEqual(final_result.status, HTTPStatus.OK)
        self.assertEqual({scan["status"] for scan in app.SCANS}, {"done"})
        self.assertEqual(len(app.ISSUES), 4)

    def test_cancelled_running_job_rejects_late_worker_result(self) -> None:
        scan = {
            "id": "sc_cancel",
            "repo": "acme/api",
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
        app.SCANS = [scan]
        job = app.create_scan_job_for_scan(scan)
        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)

        scan["status"] = "cancelled"
        db.cancel_scan_job_for_scan(scan["id"])
        result = RouteHarness(
            f"/worker/jobs/{job['job_id']}/result",
            {
                "status": "done",
                "attempt_id": "wk_1-1",
                "result_checksum": "checksum-cancelled",
                "findings": [{"severity": "high", "title": "Late result"}],
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(result, "POST")

        self.assertEqual(result.status, HTTPStatus.CONFLICT)
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "cancelled")
        self.assertEqual(app.SCANS[0]["status"], "cancelled")
        self.assertEqual(app.ISSUES, [])

    def test_worker_result_must_match_current_claim_attempt(self) -> None:
        scan = {
            "id": "sc_attempt",
            "repo": "acme/api",
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
        app.SCANS = [scan]
        job = app.create_scan_job_for_scan(scan)
        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)

        wrong_attempt = RouteHarness(
            f"/worker/jobs/{job['job_id']}/result",
            {
                "status": "done",
                "attempt_id": "wk_1-99",
                "result_checksum": "checksum-wrong-attempt",
                "findings": [{"severity": "high", "title": "Wrong attempt"}],
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(wrong_attempt, "POST")
        self.assertEqual(wrong_attempt.status, HTTPStatus.CONFLICT)
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "claimed")
        self.assertEqual(app.SCANS[0]["status"], "running")
        self.assertEqual(app.ISSUES, [])

        current_attempt = RouteHarness(
            f"/worker/jobs/{job['job_id']}/result",
            {
                "status": "done",
                "attempt_id": "wk_1-1",
                "result_checksum": "checksum-current-attempt",
                "findings": [{"severity": "high", "title": "Current attempt"}],
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(current_attempt, "POST")
        self.assertEqual(current_attempt.status, HTTPStatus.OK)
        self.assertTrue(current_attempt.payload["accepted"])
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "done")
        self.assertEqual(app.SCANS[0]["status"], "done")
        self.assertEqual(len(app.ISSUES), 1)

    def test_retry_rejects_late_result_from_previous_attempt(self) -> None:
        timestamp = app.now()
        scan = {
            "id": "sc_retry",
            "repo": "acme/api",
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": "usr_1",
            "createdAt": timestamp,
            "queuedAt": timestamp,
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS = [scan]
        job = app.create_scan_job_for_scan(scan)

        first_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        with patch("pullwise_server.app.now", return_value=timestamp):
            app.PullwiseHandler.route(first_claim, "POST")
        self.assertEqual(first_claim.status, HTTPStatus.OK)
        self.assertEqual(first_claim.payload["job"]["attempt"], 1)

        recovered = db.recover_expired_scan_jobs(timestamp + 3700)
        with app.STATE_LOCK:
            app.apply_recovered_scan_jobs_locked(recovered)
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "queued")

        second_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        with patch("pullwise_server.app.now", return_value=timestamp + 3701):
            app.PullwiseHandler.route(second_claim, "POST")
        self.assertEqual(second_claim.status, HTTPStatus.OK)
        self.assertEqual(second_claim.payload["job"]["attempt"], 2)

        stale_result = RouteHarness(
            f"/worker/jobs/{job['job_id']}/result",
            {"status": "done", "attempt_id": "wk_1-1", "result_checksum": "stale", "findings": []},
            headers=self.auth,
        )
        app.PullwiseHandler.route(stale_result, "POST")
        self.assertEqual(stale_result.status, HTTPStatus.CONFLICT)
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "claimed")
        self.assertEqual(app.SCANS[0]["status"], "running")

        current_result = RouteHarness(
            f"/worker/jobs/{job['job_id']}/result",
            {"status": "done", "attempt_id": "wk_1-2", "result_checksum": "current", "findings": []},
            headers=self.auth,
        )
        app.PullwiseHandler.route(current_result, "POST")
        self.assertEqual(current_result.status, HTTPStatus.OK)
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "done")
        self.assertEqual(app.SCANS[0]["status"], "done")

    def test_queue_limits_reject_new_scan_before_job_creation(self) -> None:
        app.SCANS = [
            {
                "id": "sc_existing",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "userId": "usr_1",
                "createdAt": app.now(),
                "queuedAt": app.now(),
            }
        ]
        with patch.dict(os.environ, {"PULLWISE_MAX_QUEUED_SCANS_PER_USER": "1"}, clear=False):
            error = app.scan_queue_limit_error("usr_1")
        self.assertIsNotNone(error)
        self.assertEqual(error[2], "QUEUE_FULL_USER")

    def test_concurrent_claims_do_not_duplicate_jobs(self) -> None:
        scan = {
            "id": "sc_atomic",
            "repo": "acme/api",
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)
        claimed: list[str] = []
        lock = threading.Lock()

        def claim(worker_id: str) -> None:
            jobs = db.claim_next_scan_jobs(
                worker_id,
                max_jobs=1,
                global_running_limit=2,
                per_user_running_limit=2,
            )
            with lock:
                claimed.extend(job["job_id"] for job in jobs)

        threads = [threading.Thread(target=claim, args=(f"wk_{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(claimed), 1)
        self.assertEqual(len(set(claimed)), 1)

    def test_expired_job_exceeding_attempts_fails(self) -> None:
        timestamp = app.now()
        job = db.create_scan_job(
            {
                "job_id": "job_fail_timeout",
                "scan_id": "sc_fail_timeout",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "created_at": timestamp - 120,
                "user_id": "usr_1",
                "max_attempts": 1,
            }
        )
        db.claim_next_scan_jobs("wk_1", max_jobs=1, lease_seconds=60, timestamp=timestamp - 120)

        recovered = db.recover_expired_scan_jobs(timestamp)
        stored = db.get_scan_job(job["job_id"])

        self.assertEqual(recovered[0]["status"], "failed")
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["error"], "timed_out")


if __name__ == "__main__":
    unittest.main()
