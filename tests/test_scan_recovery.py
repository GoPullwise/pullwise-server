from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from pullwise_server import app, db


def legacy_result_fields(title: str) -> dict:
    return {
        "audit_protocol": "audit-swarm/0.1",
        "issue_cards": [
            {
                "issue_id": "issue-legacy",
                "title": title,
                "category": "Quality",
                "severity": "P1",
                "locations": [{"file": "src/app.py", "startLine": 12, "endLine": 12}],
                "claim": title,
            }
        ],
        "verification_results": [],
    }


def completed_protocol_manifest(run_id: str) -> list[dict]:
    items = [
        ("art_report_human", "report.human", "report.md", "text/markdown", "human-markdown-report"),
        ("art_report_agent", "report.agent", "report.agent.json", "application/json", "codex-full-repo-review"),
        ("art_coverage", "coverage", "coverage.json", "application/json", "coverage"),
        ("art_qa", "qa", "qa.json", "application/json", "qa-gate"),
        ("art_token_budget", "token_budget", "token-budget.json", "application/json", "token-budget"),
    ]
    manifest = []
    for artifact_id, kind, name, media_type, schema_id in items:
        content = f"{kind}:{name}\n".encode("utf-8")
        manifest.append(
            {
                "artifact_id": artifact_id,
                "kind": kind,
                "name": name,
                "media_type": media_type,
                "schema_id": schema_id,
                "schema_version": "v1",
                "encoding": "utf-8",
                "compression": "none",
                "required": True,
                "storage": {"type": "server_artifact", "url": f"/v1/review-runs/{run_id}/artifacts/{artifact_id}"},
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
    return manifest


def store_completed_protocol_artifacts(job: dict, attempt_id: str, manifest: list[dict]) -> None:
    for item in manifest:
        db.store_review_run_artifact(
            job_id=job["job_id"],
            attempt_id=attempt_id,
            artifact_id=item["artifact_id"],
            payload={
                "run_id": job.get("run_id") or f"run_{job['job_id']}",
                "artifact_id": item["artifact_id"],
                "sha256": item["sha256"],
                "size_bytes": item["size_bytes"],
            },
        )


def completed_protocol_summary(top_findings: list[dict] | None = None) -> dict:
    findings = top_findings or []
    return {
        "overall_risk": "unknown",
        "result_status": "complete",
        "finding_counts": {
            "confirmed_critical": 0,
            "confirmed_high": 0,
            "confirmed_medium": 0,
            "confirmed_low": 0,
            "plausible": 0,
            "weak_appendix": 0,
            "disproven": 0,
            "suppressed": 0,
        },
        "coverage": {
            "source_like_files_total": 0,
            "deep_reviewed_files": 0,
            "standard_reviewed_files": 0,
            "light_reviewed_files": 0,
            "inventory_only_files": 0,
            "skipped_files": 0,
            "intent_tests_planned": 0,
            "intent_tests_run": 0,
        },
        "top_findings": findings,
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

    def seed_reserved_scan(
        self,
        *,
        scan_id: str,
        request_id: str,
        status: str = "queued",
        phase: str | None = None,
        timestamp: int | None = None,
    ) -> tuple[dict, dict, dict]:
        timestamp = timestamp or app.now()
        user = {"id": "usr_1", "email": "dev@example.com", "providers": []}
        app.USERS = {user["id"]: user}
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
        quota_result = app.quota.reserve_scan_quota(
            user=user,
            repository=repository,
            requested_by_user_id=user["id"],
            scan_id=scan_id,
            request_id=request_id,
            timestamp=timestamp,
        )
        scan = {
            "id": scan_id,
            "repo": repository["full_name"],
            "branch": "main",
            "commit": "pending",
            "status": status,
            "userId": user["id"],
            "createdAt": timestamp,
            "queuedAt": timestamp,
            "progress": 0,
            "phase": phase,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "repoId": repository["id"],
            "githubRepoId": repository["github_repo_id"],
            "requestId": request_id,
            "quotaBucketIds": quota_result["bucketIds"],
            "billingUsage": quota_result["user"],
            "repoUsage": quota_result["repository"],
            "quotaState": "reserved",
            "quotaReservedAt": timestamp,
        }
        app.SCANS = [scan]
        return user, repository, scan

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

    def test_state_loader_uses_database_scan_snapshots_as_memory_cache(self) -> None:
        db.reset_initialization_cache()
        db.initialize()
        db.upsert_scan(
            {
                "id": "sc_db_loaded",
                "userId": "usr_1",
                "repo": "acme/api",
                "repoId": "repo_1",
                "status": "queued",
                "createdAt": app.now(),
                "requestId": "req_db_loaded",
            }
        )
        app.STATE_LOADED = False
        app.SCANS = []
        app.SCAN_BY_ID = {}

        with patch.object(app.db, "load_state", return_value={}):
            app.ensure_state_loaded()

        self.assertTrue(app.STATE_LOADED)
        self.assertEqual(app.SCANS[0]["id"], "sc_db_loaded")
        self.assertEqual(app.memory_scan_by_id("sc_db_loaded")["requestId"], "req_db_loaded")

    def test_state_loader_skips_legacy_scan_issue_replay_when_normalized_tables_exist(self) -> None:
        db.reset_initialization_cache()
        db.initialize()
        timestamp = app.now()
        db.upsert_scan(
            {
                "id": "sc_normalized",
                "userId": "usr_1",
                "repo": "acme/api",
                "repoId": "repo_1",
                "status": "fixed",
                "createdAt": timestamp,
                "requestId": "req_normalized",
            }
        )
        db.upsert_issue(
            {
                "id": "iss_normalized",
                "userId": "usr_1",
                "scanId": "sc_normalized",
                "repo": "acme/api",
                "status": "fixed",
                "severity": "high",
                "title": "Fixed normalized issue",
                "createdAt": timestamp,
            }
        )
        legacy_scan = {
            "id": "sc_legacy_deleted",
            "userId": "usr_1",
            "repo": "acme/api",
            "repoId": "repo_1",
            "status": "running",
            "createdAt": timestamp - 60,
            "requestId": "req_legacy",
        }
        legacy_issue = {
            "id": "iss_legacy_deleted",
            "userId": "usr_1",
            "scanId": "sc_legacy_deleted",
            "repo": "acme/api",
            "status": "open",
            "severity": "critical",
            "title": "Legacy issue should not revive",
            "createdAt": timestamp - 60,
        }
        app.STATE_LOADED = False
        app.SCANS = []
        app.SCAN_BY_ID = {}
        app.ISSUES = []

        with patch.object(app.db, "load_state", return_value={"scans": [legacy_scan], "issues": [legacy_issue]}):
            app.ensure_state_loaded()

        self.assertTrue(app.STATE_LOADED)
        self.assertEqual(["sc_normalized"], [scan.get("id") for scan in app.SCANS])
        self.assertIsNone(app.memory_scan_by_id("sc_legacy_deleted"))
        self.assertEqual(1, db.count_scan_snapshots())
        self.assertEqual(1, db.count_issue_snapshots())
        self.assertIsNone(db.load_state_item("scans"))
        self.assertIsNone(db.load_state_item("issues"))
        marker = db.load_state_item(app.LEGACY_SCAN_ISSUE_IMPORT_STATE_KEY)
        self.assertIsInstance(marker, dict)
        self.assertTrue(marker.get("imported"))
        self.assertEqual(1, marker.get("scansSkipped"))
        self.assertEqual(1, marker.get("issuesSkipped"))

    def test_memory_scan_by_id_rejects_stale_scan_index_entry(self) -> None:
        current_scan = {"id": "sc_current", "status": "queued"}
        stale_scan = {"id": "sc_stale", "status": "running"}
        app.SCANS = [current_scan]
        app.SCAN_BY_ID = {"sc_stale": stale_scan}

        self.assertIsNone(app.memory_scan_by_id("sc_stale"))
        self.assertNotIn("sc_stale", app.SCAN_BY_ID)
        self.assertIs(app.memory_scan_by_id("sc_current"), current_scan)

    def test_scan_lookup_helpers_use_database_after_memory_cache_is_empty(self) -> None:
        timestamp = app.now()
        db.upsert_scan(
            {
                "id": "sc_db_lookup",
                "userId": "usr_1",
                "repo": "acme/api",
                "repoId": "repo_1",
                "status": "queued",
                "createdAt": timestamp,
                "requestId": "req_db_lookup",
            }
        )
        db.create_scan_job(
            {
                "job_id": "job_db_lookup",
                "scan_id": "sc_db_lookup",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "created_at": timestamp,
                "user_id": "usr_1",
                "repo_id": "repo_1",
            }
        )
        app.SCANS = []
        app.SCAN_BY_ID = {}

        by_request = app.user_scan_by_request_id("usr_1", "req_db_lookup")
        active = app.active_scan_for_user_repo("usr_1", "repo_1")

        self.assertEqual(by_request["id"], "sc_db_lookup")
        self.assertEqual(active["id"], "sc_db_lookup")
        self.assertEqual(active["status"], "queued")
        self.assertEqual(app.memory_scan_by_id("sc_db_lookup")["id"], "sc_db_lookup")

    def test_recover_interrupted_scans_preserves_matching_unexpired_job(self) -> None:
        timestamp = app.now()
        app.SCANS = [
            {
                "id": "sc_unexpired",
                "jobId": "job_unexpired",
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
        db.claim_next_scan_job("wk_1", lease_seconds=3600, timestamp=timestamp)

        with patch("pullwise_server.app.now", return_value=timestamp + 30):
            recovered = app.recover_interrupted_scans()
        stored = db.get_scan_job(job["job_id"])

        self.assertEqual(recovered, 1)
        self.assertEqual(stored["status"], "claimed")
        self.assertIsNone(stored["error"])
        self.assertEqual(stored["claimed_by_worker_id"], "wk_1")
        self.assertGreater(stored["timeout_at"], timestamp + 30)
        self.assertEqual(app.SCANS[0]["status"], "running")
        self.assertEqual(app.SCANS[0]["jobId"], "job_unexpired")
        self.assertEqual(app.SCANS[0]["claimedByWorkerId"], "wk_1")
        self.assertNotIn("recoveryReason", app.SCANS[0])

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
        claimed = db.claim_next_scan_job("wk_1", lease_seconds=3600, timestamp=timestamp)
        attempt_id = f"wk_1-{claimed['attempt']}"
        manifest = completed_protocol_manifest("run_job_done_in_db")
        store_completed_protocol_artifacts(job, attempt_id, manifest)
        db.record_scan_job_result(
            job["job_id"],
            attempt_id=attempt_id,
            status="done",
            result_checksum="checksum-done",
            payload={
                "status": "done",
                "attempt_id": attempt_id,
                "result_checksum": "checksum-done",
                "reviewWorkerProtocol": {
                    "protocol_version": "review-worker-protocol/v1",
                    "message_type": "review_run_result",
                    "job": {
                        "job_id": job["job_id"],
                        "run_id": "run_job_done_in_db",
                        "lease_id": "lease_job_done_in_db",
                    },
                    "worker": {"worker_id": "wk_1"},
                    "execution": {"status": "completed"},
                    "progress_final": {
                        "overall_percent": 100.0,
                        "current_phase": "submit_result_envelope",
                        "status": "completed",
                        "message": "terminal progress",
                    },
                    "quality_gate": {"status": "pass"},
                    "summary": completed_protocol_summary(
                        [
                            {
                                "id": "issue-recovered",
                                "title": "Recovered finding",
                                "severity": "high",
                                "location": {"file": "src/app.py", "line": 12},
                                "description": "Recovered completed worker result.",
                                "recommendation": "Fix the recovered issue.",
                            }
                        ]
                    ),
                    "artifact_manifest": manifest,
                },                "summary": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
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

    def test_recover_interrupted_scans_rejects_legacy_completed_job_result(self) -> None:
        timestamp = app.now()
        app.SCANS = [
            {
                "id": "sc_legacy_done_in_db",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "running",
                "createdAt": timestamp - 60,
                "queuedAt": timestamp - 50,
                "startedAt": timestamp - 40,
                "progress": 80,
                "phase": "ai",
                "jobId": "job_legacy_done_in_db",
                "userId": "usr_1",
                "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            }
        ]
        job = db.create_scan_job(
            {
                "job_id": "job_legacy_done_in_db",
                "scan_id": "sc_legacy_done_in_db",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "created_at": timestamp - 30,
                "user_id": "usr_1",
            }
        )
        claimed = db.claim_next_scan_job("wk_1", lease_seconds=3600, timestamp=timestamp)
        db.record_scan_job_result(
            job["job_id"],
            attempt_id=f"wk_1-{claimed['attempt']}",
            status="done",
            result_checksum="checksum-legacy-done",
            payload={
                "status": "done",
                "attempt_id": f"wk_1-{claimed['attempt']}",
                "result_checksum": "checksum-legacy-done",
                **legacy_result_fields("Legacy finding must not be recovered"),
                "summary": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
            },
        )

        recovered = app.recover_interrupted_scans()

        self.assertEqual(recovered, 1)
        self.assertEqual(app.SCANS[0]["status"], "failed")
        self.assertEqual(app.SCANS[0]["errorCode"], "WORKER_PROTOCOL_MISSING")
        self.assertEqual(app.ISSUES, [])

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
        db.claim_next_scan_job("wk_1", lease_seconds=60, timestamp=timestamp - 120)

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

    def test_recover_interrupted_scans_fails_exhausted_queued_job_and_scan(self) -> None:
        timestamp = app.now()
        app.SCANS = [
            {
                "id": "sc_exhausted",
                "status": "queued",
                "progress": 0,
                "phase": None,
                "createdAt": timestamp - 180,
                "queuedAt": timestamp - 180,
            },
        ]
        job = db.create_scan_job(
            {
                "job_id": "job_exhausted",
                "scan_id": "sc_exhausted",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "created_at": timestamp - 180,
                "user_id": "usr_1",
                "max_attempts": 2,
            }
        )
        with closing(sqlite3.connect(os.environ["PULLWISE_DB_PATH"])) as connection:
            with connection:
                connection.execute(
                    "UPDATE scan_jobs SET attempt = 2 WHERE job_id = ?",
                    (job["job_id"],),
                )

        recovered = app.recover_interrupted_scans()
        stored = db.get_scan_job(job["job_id"])

        self.assertEqual(recovered, 1)
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["error"], "retry_attempts_exhausted")
        self.assertEqual(app.SCANS[0]["status"], "failed")
        self.assertEqual(app.SCANS[0]["error"], "Scan exceeded the configured retry attempts before completing.")
        self.assertEqual(app.SCANS[0]["recoveryReason"], "retry_attempts_exhausted")

    def test_recover_interrupted_scans_releases_reserved_quota_for_exhausted_job_before_ai(self) -> None:
        timestamp = app.now()
        user, repository, scan = self.seed_reserved_scan(
            scan_id="sc_reserved_exhausted",
            request_id="req_reserved_exhausted",
            timestamp=timestamp,
        )
        job = db.create_scan_job(
            {
                "job_id": "job_reserved_exhausted",
                "scan_id": scan["id"],
                "repo": repository["full_name"],
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "created_at": timestamp - 180,
                "user_id": user["id"],
                "repo_id": repository["id"],
                "github_repo_id": repository["github_repo_id"],
                "max_attempts": 1,
            }
        )
        with closing(sqlite3.connect(os.environ["PULLWISE_DB_PATH"])) as connection:
            with connection:
                connection.execute("UPDATE scan_jobs SET attempt = 1 WHERE job_id = ?", (job["job_id"],))

        recovered = app.recover_interrupted_scans()

        usage = app.quota.quota_payload_for_user(user)
        self.assertEqual(recovered, 1)
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "failed")
        self.assertEqual(app.SCANS[0]["status"], "failed")
        self.assertEqual(app.SCANS[0]["quotaState"], "released")
        self.assertEqual(app.SCANS[0]["quotaReleaseReason"], "retry_attempts_exhausted")
        self.assertEqual(usage["used"], 0)
        self.assertEqual(usage["reserved"], 0)
        self.assertEqual(app.SCANS[0]["billingUsage"]["reserved"], 0)

    def test_recover_interrupted_scans_releases_historical_failed_reserved_scan_without_queue(self) -> None:
        timestamp = app.now()
        user, _repository, scan = self.seed_reserved_scan(
            scan_id="sc_reserved_failed_history",
            request_id="req_reserved_failed_history",
            status="failed",
            timestamp=timestamp,
        )
        scan["error"] = "worker_job_startup_lost"

        recovered = app.recover_interrupted_scans()

        usage = app.quota.quota_payload_for_user(user)
        self.assertEqual(recovered, 1)
        self.assertEqual(app.SCANS[0]["status"], "failed")
        self.assertEqual(app.SCANS[0]["quotaState"], "released")
        self.assertEqual(app.SCANS[0]["quotaReleaseReason"], "worker_job_startup_lost")
        self.assertEqual(usage["used"], 0)
        self.assertEqual(usage["reserved"], 0)

    def test_recover_interrupted_scans_consumes_reserved_quota_for_failed_job_after_ai_started(self) -> None:
        timestamp = app.now()
        user, repository, scan = self.seed_reserved_scan(
            scan_id="sc_reserved_ai_failed",
            request_id="req_reserved_ai_failed",
            status="running",
            phase="ai",
            timestamp=timestamp,
        )
        job = db.create_scan_job(
            {
                "job_id": "job_reserved_ai_failed",
                "scan_id": scan["id"],
                "repo": repository["full_name"],
                "branch": "main",
                "commit": "pending",
                "status": "failed",
                "created_at": timestamp - 180,
                "user_id": user["id"],
                "repo_id": repository["id"],
                "github_repo_id": repository["github_repo_id"],
                "max_attempts": 1,
            }
        )
        scan["jobId"] = job["job_id"]
        with closing(sqlite3.connect(os.environ["PULLWISE_DB_PATH"])) as connection:
            with connection:
                connection.execute(
                    """
                    UPDATE scan_jobs
                    SET progress_phase = 'ai',
                        progress = 60,
                        completed_at = ?,
                        error = 'timed_out'
                    WHERE job_id = ?
                    """,
                    (timestamp, job["job_id"]),
                )

        recovered = app.recover_interrupted_scans()

        usage = app.quota.quota_payload_for_user(user)
        self.assertEqual(recovered, 1)
        self.assertEqual(app.SCANS[0]["status"], "failed")
        self.assertEqual(app.SCANS[0]["quotaState"], "consumed")
        self.assertEqual(usage["used"], 1)
        self.assertEqual(usage["reserved"], 0)
        self.assertEqual(app.SCANS[0]["billingUsage"]["used"], 1)
        self.assertNotIn("quotaReleaseReason", app.SCANS[0])

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
            db.upsert_worker_heartbeat(
                {
                    "worker_id": "wk_ready",
                    "provider": "codex",
                    "running_jobs": 0,
                    "free_slots": 1,
                    "timestamp": timestamp + 1,
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
            db.claim_next_scan_job("wk_stale", lease_seconds=3600, timestamp=timestamp + 1)

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
