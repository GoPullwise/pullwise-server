from __future__ import annotations

import unittest
import os
import sqlite3
import tempfile
from contextlib import closing
from unittest.mock import patch

from pullwise_server import app, db


def graph_verified_result_fields(title: str) -> dict:
    return {
        "graphVerifiedReport": {
            "version": "graph-verified-code-review/1",
            "runId": "gv_recovery_run",
            "mode": "standard",
            "base": "origin/main",
            "head": "HEAD",
            "confirmedCount": 1,
            "rejectedCount": 0,
            "blockedCount": 0,
            "finalJson": {
                "confirmed": [
                    {
                        "candidate": {
                            "candidate_id": "issue-recovered",
                            "dedupe_key": "quality|src/app.py|recovered",
                            "category": "correctness",
                            "severity": "high",
                            "confidence": "high",
                            "claim": title,
                            "graph_evidence": {
                                "slice_id": "slice-recovered",
                                "codegraph_files": ["src/app.py"],
                                "path_summary": ["src/app.py:12", "candidate -> repro -> judge"],
                            },
                            "evidence": [
                                {
                                    "file": "src/app.py",
                                    "lines": "12",
                                    "why_it_matters": "Recovered completed worker result.",
                                }
                            ],
                            "trigger_condition": "Run the local reproduction.",
                            "expected_behavior": "The app handles the path.",
                            "actual_behavior_hypothesis": title,
                            "minimal_repro_idea": "pytest tests/recovered.py",
                            "repro_likelihood": "high",
                        },
                        "repro": {
                            "candidate_id": "issue-recovered",
                            "status": "reproduced",
                            "level": "L2",
                            "summary": title,
                            "commands_run": [
                                {
                                    "cmd": "pytest tests/recovered.py",
                                    "cwd": ".",
                                    "exit_code": 1,
                                    "log_path": "logs/recovered.log",
                                }
                            ],
                            "files_written": [],
                            "proof": {
                                "type": "failing_test",
                                "expected": "The app handles the path.",
                                "actual": title,
                                "log_excerpt": title,
                            },
                            "graph_path_exercised": True,
                            "why_valid": "The recovered result has graph evidence and local repro.",
                            "why_not_reproduced": "",
                            "safety_notes": "Local test fixture.",
                        },
                        "judge": {
                            "candidate_id": "issue-recovered",
                            "status": "confirmed",
                            "level": "L2",
                            "safe_to_show_user": True,
                            "reason": "Recovered completed worker result.",
                            "evidence_summary": {
                                "command": "pytest tests/recovered.py",
                                "log_path": "logs/recovered.log",
                                "observable": title,
                            },
                            "limitations": [],
                        },
                        "verification": {"status": "confirmed", "level": "L2", "safe_to_show_user": True},
                    }
                ]
            },
        },
    }


class ScanRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env = patch.dict(
            os.environ,
            {"PULLWISE_DB_PATH": os.path.join(self.temp_dir.name, "pullwise.sqlite3")},
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.persist_patcher = patch.object(app, "persist_state")
        self.persist_state = self.persist_patcher.start()
        self.addCleanup(self.persist_patcher.stop)
        app.STATE_LOADED = True
        app.STATE_DIRTY = False
        app.ISSUES = []

    def test_recover_interrupted_scans_requeues_running_scans(self) -> None:
        app.SCANS = [
            {
                "id": "sc_1",
                "status": "running",
                "progress": 44,
                "phase": "ai",
                "createdAt": app.now() - 60,
            },
        ]

        recovered = app.recover_interrupted_scans()

        self.assertEqual(recovered, 1)
        self.assertEqual(app.SCANS[0]["status"], "queued")
        self.assertEqual(app.SCANS[0]["progress"], 0)
        self.assertIsNone(app.SCANS[0]["phase"])
        self.assertIn("recoveredAt", app.SCANS[0])
        self.assertEqual(app.SCANS[0]["recoveryReason"], "server_restart")
        self.persist_state.assert_called_once()

    def test_recover_interrupted_scans_requeues_matching_unexpired_job(self) -> None:
        timestamp = app.now()
        app.SCANS = [
            {
                "id": "sc_unexpired",
                "status": "running",
                "progress": 44,
                "phase": "ai",
                "claimedAt": timestamp,
                "claimedByWorkerId": "wk_1",
                "createdAt": timestamp - 10,
                "queuedAt": timestamp - 10,
            },
        ]
        job = db.create_scan_job(
            {
                "job_id": "job_unexpired",
                "scan_id": "sc_unexpired",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "created_at": timestamp - 10,
                "user_id": "usr_1",
                "max_attempts": 3,
            }
        )
        db.claim_next_scan_jobs("wk_1", max_jobs=1, lease_seconds=3600, timestamp=timestamp)

        with patch("pullwise_server.app.now", return_value=timestamp + 30):
            recovered = app.recover_interrupted_scans()
        stored = db.get_scan_job(job["job_id"])

        self.assertEqual(recovered, 1)
        self.assertEqual(stored["status"], "queued")
        self.assertEqual(stored["error"], "server_restart")
        self.assertIsNone(stored["claimed_by_worker_id"])
        self.assertIsNone(stored["timeout_at"])
        self.assertEqual(app.SCANS[0]["status"], "queued")
        self.assertEqual(app.SCANS[0]["recoveryReason"], "server_restart")

    def test_recover_interrupted_scans_reconstructs_orphan_scan_job(self) -> None:
        timestamp = app.now()
        app.USERS = {"usr_1": {"id": "usr_1", "email": "dev@example.com"}}
        app.SCANS = []
        repository = db.upsert_repository(
            {
                "id": db.repository_id_for_github_repo("123"),
                "github_repo_id": "123",
                "full_name": "acme/api",
                "owner_login": "acme",
                "default_branch": "main",
                "private": True,
                "clone_url": "https://github.com/acme/api.git",
            }
        )
        db.create_scan_job(
            {
                "job_id": "job_orphan",
                "scan_id": "sc_orphan",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "created_at": timestamp,
                "user_id": "usr_1",
                "repo_id": repository["id"],
                "github_repo_id": "123",
                "installation_id": "111",
                "clone_url": "https://github.com/acme/api.git",
            }
        )

        recovered = app.recover_interrupted_scans()

        self.assertEqual(recovered, 1)
        self.assertEqual(len(app.SCANS), 1)
        self.assertEqual(app.SCANS[0]["id"], "sc_orphan")
        self.assertEqual(app.SCANS[0]["jobId"], "job_orphan")
        self.assertEqual(app.SCANS[0]["status"], "queued")
        self.assertEqual(app.SCANS[0]["repoId"], repository["id"])
        self.assertEqual(app.SCANS[0]["githubRepoId"], "123")
        self.persist_state.assert_called_once()

    def test_recover_interrupted_scans_rolls_back_quota_without_scan_or_job(self) -> None:
        timestamp = app.now()
        app.USERS = {
            "usr_1": {
                "id": "usr_1",
                "email": "dev@example.com",
                "billing": {"status": "active", "plan": "pro"},
            }
        }
        app.SCANS = []
        repository = db.upsert_repository(
            {
                "id": db.repository_id_for_github_repo("456"),
                "github_repo_id": "456",
                "full_name": "acme/worker",
                "owner_login": "acme",
                "default_branch": "main",
                "private": True,
                "clone_url": "https://github.com/acme/worker.git",
            }
        )
        app.quota.consume_scan_quota(
            user=app.USERS["usr_1"],
            repository=repository,
            requested_by_user_id="usr_1",
            scan_id="sc_quota_only",
            request_id="req_quota_only",
            timestamp=timestamp,
        )
        with closing(sqlite3.connect(os.environ["PULLWISE_DB_PATH"])) as connection:
            before_ledger_count = connection.execute("SELECT COUNT(*) FROM quota_ledger").fetchone()[0]
            before_used = connection.execute("SELECT COALESCE(SUM(used), 0) FROM quota_buckets").fetchone()[0]

        recovered = app.recover_interrupted_scans()

        with closing(sqlite3.connect(os.environ["PULLWISE_DB_PATH"])) as connection:
            after_ledger_count = connection.execute("SELECT COUNT(*) FROM quota_ledger").fetchone()[0]
            after_used = connection.execute("SELECT COALESCE(SUM(used), 0) FROM quota_buckets").fetchone()[0]
        self.assertGreater(before_ledger_count, 0)
        self.assertGreater(before_used, 0)
        self.assertEqual(recovered, 1)
        self.assertEqual(after_ledger_count, 0)
        self.assertEqual(after_used, 0)
        self.assertEqual(app.SCANS, [])

    def test_recover_interrupted_scans_reconciles_completed_job_result_before_requeue(self) -> None:
        timestamp = app.now()
        app.SCANS = [
            {
                "id": "sc_done_in_db",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "running",
                "progress": 80,
                "phase": "ai",
                "claimedAt": timestamp,
                "claimedByWorkerId": "wk_1",
                "jobId": "job_done_in_db",
                "createdAt": timestamp - 30,
                "queuedAt": timestamp - 30,
            },
        ]
        job = db.create_scan_job(
            {
                "job_id": "job_done_in_db",
                "scan_id": "sc_done_in_db",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "created_at": timestamp - 30,
                "user_id": "usr_1",
            }
        )
        claimed = db.claim_next_scan_jobs("wk_1", max_jobs=1, lease_seconds=3600, timestamp=timestamp)[0]
        db.record_scan_job_result(
            job["job_id"],
            attempt_id=f"wk_1-{claimed['attempt']}",
            status="done",
            result_checksum="checksum-done",
            payload={
                "status": "done",
                "attempt_id": f"wk_1-{claimed['attempt']}",
                "result_checksum": "checksum-done",
                **graph_verified_result_fields("Recovered finding"),
                "summary": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
                "duration_ms": 123,
            },
        )

        recovered = app.recover_interrupted_scans()

        self.assertEqual(recovered, 1)
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "done")
        self.assertEqual(app.SCANS[0]["status"], "done")
        self.assertEqual(app.SCANS[0]["progress"], 100)
        self.assertEqual(app.SCANS[0]["issues"]["high"], 1)
        self.assertEqual(len(app.ISSUES), 1)
        self.assertEqual(app.ISSUES[0]["title"], "Recovered finding")
        self.persist_state.assert_called_once()

    def test_recover_interrupted_scans_reconciles_terminal_jobs_without_results(self) -> None:
        timestamp = app.now()
        app.SCANS = [
            {
                "id": "sc_failed_in_db",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "running",
                "progress": 80,
                "phase": "ai",
                "jobId": "job_failed_in_db",
                "createdAt": timestamp - 30,
                "queuedAt": timestamp - 30,
            },
            {
                "id": "sc_cancelled_in_db",
                "repo": "acme/site",
                "branch": "main",
                "commit": "pending",
                "status": "running",
                "progress": 20,
                "phase": "clone",
                "jobId": "job_cancelled_in_db",
                "createdAt": timestamp - 20,
                "queuedAt": timestamp - 20,
            },
        ]
        db.create_scan_job(
            {
                "job_id": "job_failed_in_db",
                "scan_id": "sc_failed_in_db",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "created_at": timestamp - 30,
                "user_id": "usr_1",
            }
        )
        db.create_scan_job(
            {
                "job_id": "job_cancelled_in_db",
                "scan_id": "sc_cancelled_in_db",
                "repo": "acme/site",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "created_at": timestamp - 20,
                "user_id": "usr_1",
            }
        )
        with closing(sqlite3.connect(os.environ["PULLWISE_DB_PATH"])) as connection:
            with connection:
                connection.execute(
                    """
                    UPDATE scan_jobs
                    SET status = 'failed', completed_at = ?, error = 'worker_crashed', updated_at = ?
                    WHERE job_id = 'job_failed_in_db'
                    """,
                    (timestamp + 1, timestamp + 1),
                )
                connection.execute(
                    """
                    UPDATE scan_jobs
                    SET status = 'cancelled', completed_at = ?, error = 'cancelled_by_user', updated_at = ?
                    WHERE job_id = 'job_cancelled_in_db'
                    """,
                    (timestamp + 2, timestamp + 2),
                )

        recovered = app.recover_interrupted_scans()

        self.assertEqual(recovered, 2)
        self.assertEqual(app.SCANS[0]["status"], "failed")
        self.assertEqual(app.SCANS[0]["completedAt"], timestamp + 1)
        self.assertEqual(app.SCANS[0]["error"], "worker_crashed")
        self.assertEqual(app.SCANS[1]["status"], "cancelled")
        self.assertEqual(app.SCANS[1]["completedAt"], timestamp + 2)
        self.assertEqual(app.SCANS[1]["error"], "cancelled_by_user")
        self.persist_state.assert_called_once()

    def test_recover_interrupted_scans_leaves_terminal_scans_unchanged(self) -> None:
        app.SCANS = [
            {"id": "sc_done", "status": "done", "progress": 100, "phase": "report"},
            {"id": "sc_failed", "status": "failed", "progress": 80, "phase": "ai"},
            {"id": "sc_cancelled", "status": "cancelled", "progress": 0, "phase": None},
        ]
        original = [dict(scan) for scan in app.SCANS]

        recovered = app.recover_interrupted_scans()

        self.assertEqual(recovered, 0)
        self.assertEqual(app.SCANS, original)
        self.persist_state.assert_not_called()

    def test_recover_interrupted_scans_requeues_timed_out_job_and_scan(self) -> None:
        timestamp = app.now()
        app.SCANS = [
            {
                "id": "sc_job_timeout",
                "status": "running",
                "progress": 70,
                "phase": "ai",
                "claimedAt": timestamp - 120,
                "claimedByWorkerId": "wk_1",
                "createdAt": timestamp - 180,
                "queuedAt": timestamp - 180,
            },
        ]
        job = db.create_scan_job(
            {
                "job_id": "job_timeout",
                "scan_id": "sc_job_timeout",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "created_at": timestamp - 180,
                "user_id": "usr_1",
                "max_attempts": 3,
            }
        )
        db.claim_next_scan_jobs("wk_1", max_jobs=1, lease_seconds=60, timestamp=timestamp - 120)

        recovered = app.recover_interrupted_scans()
        stored = db.get_scan_job(job["job_id"])

        self.assertEqual(recovered, 1)
        self.assertEqual(stored["status"], "queued")
        self.assertEqual(stored["attempt"], 1)
        self.assertEqual(app.SCANS[0]["status"], "queued")
        self.assertEqual(app.SCANS[0]["progress"], 0)
        self.assertIsNone(app.SCANS[0]["phase"])
        self.assertIsNone(app.SCANS[0]["claimedAt"])
        self.assertIsNone(app.SCANS[0]["claimedByWorkerId"])

    def test_recover_interrupted_scans_requeues_job_when_worker_heartbeat_times_out(self) -> None:
        timestamp = app.now()
        with patch.dict(os.environ, {"PULLWISE_WORKER_HEARTBEAT_TIMEOUT_SECONDS": "60"}, clear=False):
            db.upsert_worker_heartbeat(
                {
                    "worker_id": "wk_stale",
                    "provider": "codex",
                    "max_concurrent_jobs": 1,
                    "running_jobs": 1,
                    "free_slots": 0,
                    "timestamp": timestamp,
                }
            )
            app.SCANS = [
                {
                    "id": "sc_stale_worker",
                    "status": "running",
                    "progress": 35,
                    "phase": "ai",
                    "claimedAt": timestamp + 1,
                    "claimedByWorkerId": "wk_stale",
                    "createdAt": timestamp,
                    "queuedAt": timestamp,
                },
            ]
            job = db.create_scan_job(
                {
                    "job_id": "job_stale_worker",
                    "scan_id": "sc_stale_worker",
                    "repo": "acme/api",
                    "branch": "main",
                    "commit": "pending",
                    "status": "queued",
                    "created_at": timestamp,
                    "user_id": "usr_1",
                    "max_attempts": 3,
                }
            )
            db.claim_next_scan_jobs("wk_stale", max_jobs=1, lease_seconds=3600, timestamp=timestamp + 1)

            with patch("pullwise_server.app.now", return_value=timestamp + 121):
                recovered = app.recover_interrupted_scans()
            stored = db.get_scan_job(job["job_id"])

        self.assertEqual(recovered, 1)
        self.assertEqual(stored["status"], "queued")
        self.assertEqual(stored["error"], "worker_heartbeat_timed_out")
        self.assertEqual(app.SCANS[0]["status"], "queued")
        self.assertEqual(app.SCANS[0]["progress"], 0)
        self.assertIsNone(app.SCANS[0]["phase"])
        self.assertIsNone(app.SCANS[0]["claimedAt"])
        self.assertIsNone(app.SCANS[0]["claimedByWorkerId"])
        self.assertEqual(app.SCANS[0]["recoveryReason"], "worker_heartbeat_timed_out")
