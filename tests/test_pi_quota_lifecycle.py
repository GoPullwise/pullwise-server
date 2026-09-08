from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app, db
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections
from tests.test_scan_quota_routes import RouteHarness as UserRoute, quota_config, reset_state, seed_user
from tests.test_worker_admin_routes import RouteHarness as WorkerRoute
from tests.test_review_worker_protocol_v1 import (
    required_completed_manifest, required_terminal_manifest, store_manifest_artifacts_for_test, v1_envelope,
)


class PiQuotaLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_state()
        start_fast_sqlite_connections(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"PULLWISE_DB_PATH": os.path.join(temporary.name, "server.sqlite3")}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        config = patch("pullwise_server.system_config.config", return_value=quota_config(free_limit=5))
        config.start()
        self.addCleanup(config.stop)
        persist = patch.object(app, "persist_state")
        persist.start()
        self.addCleanup(persist.stop)
        install_initialized_db_template(os.environ["PULLWISE_DB_PATH"])
        self.cookie = seed_user("usr_pi", "ses_pi")
        created = UserRoute({"repoId": "123", "requestId": "pi-quota"}, cookie=self.cookie)
        app.PullwiseHandler.route(created, "POST")
        self.assertEqual(created.status, HTTPStatus.CREATED, created.payload)
        self.scan_id = created.payload["id"]
        self.job_id = created.payload["jobId"]
        self.worker = db.create_worker({"name": "Pi fixture", "version": "0.10.24"})
        with closing(db.connect()) as connection, connection:
            connection.execute(
                "UPDATE scan_jobs SET status='claimed', claimed_by_worker_id=?, claimed_at=?, attempt=1 WHERE job_id=?",
                (self.worker["worker_id"], app.now(), self.job_id),
            )

    def buckets(self) -> dict[str, tuple[int, int]]:
        with closing(db.connect()) as connection:
            return {row[0]: (row[1], row[2]) for row in connection.execute("SELECT scope_type, used, reserved FROM quota_buckets")}

    def event(self, sequence: int, phase: str, event_type: str = "phase_started") -> None:
        job = db.get_scan_job(self.job_id)
        run_id = app.scan_job_attempt_run_id(job)
        handler = WorkerRoute(f"/v1/review-runs/{run_id}/events", {
            "protocol_version": "review-worker-protocol/v1", "run_id": run_id,
            "worker_id": self.worker["worker_id"], "sequence": sequence,
            "timestamp": datetime.now(timezone.utc).isoformat(), "event_type": event_type,
            "phase": phase, "severity": "info", "message": phase,
            "progress": {"overall_percent": 25, "current_phase_percent": 25, "status": "running"},
            "data": {"engine": "pi_agent_session"},
        }, headers={"Authorization": f"Bearer {self.worker['worker_token']}"})
        app.PullwiseHandler.route(handler, "POST")
        self.assertEqual(handler.status, HTTPStatus.OK, handler.payload)

    def result(self, status: str) -> dict:
        job = db.get_scan_job(self.job_id)
        job["run_id"] = app.scan_job_attempt_run_id(job)
        attempt = app.expected_worker_attempt_id(job)
        manifest = required_completed_manifest() if status == "done" else required_terminal_manifest()
        store_manifest_artifacts_for_test(job, attempt, manifest)
        envelope = v1_envelope(job, manifest, status="completed" if status == "done" else status, worker_id=self.worker["worker_id"])
        envelope["worker"]["engine"] = {"type": "pi_agent_session", "app_server_transport": "embedded"}
        payload = {"status": status, "attempt_id": attempt, "summary": {}, "reviewWorkerProtocol": envelope}
        if status != "done":
            payload["error"] = "Fixture terminal outcome"
        result = app.apply_worker_job_result(job, payload)
        self.assertTrue(result["accepted"], result)
        return payload

    def test_pi_review_start_consumes_both_reservations(self) -> None:
        self.assertEqual(self.buckets(), {"user": (0, 1), "repository": (0, 1)})
        self.event(1, "preparing", "run_started")
        self.assertIsNotNone(db.get_scan_job(self.job_id)["started_at"])
        self.assertEqual(self.buckets(), {"user": (0, 1), "repository": (0, 1)})
        self.event(2, "review")
        self.assertEqual(self.buckets(), {"user": (1, 0), "repository": (1, 0)})

    def test_stopping_worker_can_publish_its_cancelled_result(self) -> None:
        self.event(1, "preparing", "run_started")
        db.create_worker_command({'worker_id': self.worker['worker_id'], 'command': 'stop'})
        job = db.get_scan_job(self.job_id)
        job['run_id'] = app.scan_job_attempt_run_id(job)
        attempt = app.expected_worker_attempt_id(job)
        manifest = required_terminal_manifest()
        store_manifest_artifacts_for_test(job, attempt, manifest)
        envelope = v1_envelope(job, manifest, status='cancelled', worker_id=self.worker['worker_id'])
        envelope['worker']['engine'] = {'type': 'pi_agent_session', 'app_server_transport': 'embedded'}
        handler = WorkerRoute(f"/v1/review-runs/{job['run_id']}/result", {
            'status': 'cancelled', 'attempt_id': attempt, 'summary': {},
            'error': 'Instance stopped', 'reviewWorkerProtocol': envelope,
        }, headers={'Authorization': f"Bearer {self.worker['worker_token']}"})
        app.PullwiseHandler.route(handler, 'POST')
        self.assertEqual(handler.status, HTTPStatus.OK, handler.payload)
        self.assertEqual(db.get_scan_job(self.job_id)['status'], 'cancelled')
        self.assertEqual(self.buckets(), {'user': (0, 0), 'repository': (0, 0)})

    def test_pre_review_failure_releases_both_reservations_at_ingest(self) -> None:
        self.event(1, "preparing", "run_started")
        self.result("failed")
        self.assertEqual(self.buckets(), {"user": (0, 0), "repository": (0, 0)})
        self.assertEqual(db.get_user_scan_snapshot("usr_pi", self.scan_id)["quotaState"], "released")

    def test_terminal_replay_and_cold_memory_keep_core_work_billable(self) -> None:
        self.event(1, "review")
        self.event(2, "publishing")
        app.SCANS = []
        payload = self.result("done")
        app.apply_worker_job_result(db.get_scan_job(self.job_id), payload)
        self.assertEqual(self.buckets(), {"user": (1, 0), "repository": (1, 0)})
        self.assertEqual(db.get_user_scan_snapshot("usr_pi", self.scan_id)["quotaState"], "consumed")

    def test_completed_result_settles_without_optional_intermediate_events(self) -> None:
        self.result("done")
        self.assertEqual(self.buckets(), {"user": (1, 0), "repository": (1, 0)})
