from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import unittest
from unittest.mock import patch

from pullwise_server import app


def v1_envelope(job: dict, manifest: list[dict], *, status: str = "completed", worker_id: str = "wk_1") -> dict:
    job_id = job["job_id"]
    return {
        "protocol_version": "review-worker-protocol/v1",
        "message_type": "review_run_result",
        "job": {
            "job_id": job_id,
            "run_id": job.get("run_id") or f"run_{job_id}",
            "lease_id": job.get("lease_id") or f"lease_{job_id}",
            "job_type": "repo_review.full_scan",
        },
        "worker": {
            "worker_id": worker_id,
            "worker_version": "0.1.0",
            "concurrency": {"max_active_jobs": 1, "maintains_local_queue": False},
            "engine": {"type": "codex_app_server", "app_server_transport": "stdio"},
        },
        "execution": {"status": status, "review_mode": "full_repo"},
        "progress_final": {
            "overall_percent": 100.0 if status == "completed" else 41.5,
            "current_phase": "submit_result_envelope" if status == "completed" else "failure_handling",
            "status": "completed" if status == "completed" else status,
            "message": "terminal progress",
        },
        "summary": {
            "overall_risk": "unknown",
            "result_status": "complete" if status == "completed" else "incomplete",
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
            "top_findings": [],
        },
        "quality_gate": {"status": "pass", "errors": [], "warnings": []},
        "artifact_manifest": manifest,
    }


def manifest_item(**overrides: object) -> dict:
    item = {
        "artifact_id": "art_report_human",
        "kind": "report.human",
        "name": "report.md",
        "media_type": "text/markdown",
        "schema_id": "human-markdown-report",
        "schema_version": "v1",
        "required": True,
        "storage": {"type": "server_artifact", "url": "/v1/review-runs/run_1/artifacts/art_report_human"},
        "sha256": "abc",
        "size_bytes": 3,
    }
    item.update(overrides)
    return item


def required_completed_manifest() -> list[dict]:
    return [
        manifest_item(),
        manifest_item(
            artifact_id="art_report_agent",
            kind="report.agent",
            name="report.agent.json",
            media_type="application/json",
            schema_id="codex-full-repo-review",
        ),
        manifest_item(
            artifact_id="art_coverage",
            kind="coverage",
            name="coverage.json",
            media_type="application/json",
            schema_id="coverage",
        ),
        manifest_item(
            artifact_id="art_qa",
            kind="qa",
            name="qa.json",
            media_type="application/json",
            schema_id="qa-gate",
        ),
        manifest_item(
            artifact_id="art_token_budget",
            kind="token_budget",
            name="token-budget.json",
            media_type="application/json",
            schema_id="token-budget",
        ),
    ]


def required_terminal_manifest() -> list[dict]:
    return [
        manifest_item(
            artifact_id="art_worker_log",
            kind="worker_log",
            name="worker.log.jsonl",
            media_type="application/jsonl",
            schema_id="worker-log",
        ),
        manifest_item(
            artifact_id="art_qa",
            kind="qa",
            name="qa.json",
            media_type="application/json",
            schema_id="qa-gate",
        ),
        manifest_item(
            artifact_id="art_error_report",
            kind="error_report",
            name="error-report.json",
            media_type="application/json",
            schema_id="error-report",
        ),
    ]


class ReviewWorkerProtocolV1Test(unittest.TestCase):
    def test_quota_consumes_only_when_core_review_phase_starts(self) -> None:
        consuming = {
            "repo_map",
            "risk_routing",
            "reviewer_fanout",
            "clustering_and_voting",
            "validator_disproof",
            "final_report_json",
            "ai",
        }
        non_consuming = {
            "prepare_workspace",
            "start_codex_app_server",
            "initialize_codex_connection",
            "inventory_repository",
            "token_budget",
            "bundle_planning",
            "bundle_packing",
            "render_markdown_report",
            "qa_gate",
            "hash_artifacts",
            "upload_artifacts",
            "submit_result_envelope",
            "report",
        }

        self.assertTrue(all(app.worker_progress_phase_should_finalize_quota(phase) for phase in consuming))
        self.assertFalse(any(app.worker_progress_phase_should_finalize_quota(phase) for phase in non_consuming))

    def test_worker_artifact_upload_payload_validates_hash_and_size(self) -> None:
        content = b"report"
        payload = app.worker_artifact_upload_payload(
            {
                "artifact": {
                    "artifact_id": "art_report_human",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                    "name": "report.md",
                },
                "content_base64": base64.b64encode(content).decode("ascii"),
                "run_id": "run_1",
            }
        )

        self.assertEqual(payload["artifact_id"], "art_report_human")
        self.assertEqual(payload["size_bytes"], len(content))
        self.assertEqual(payload["run_id"], "run_1")

        empty_payload = app.worker_artifact_upload_payload(
            {
                "artifact": {
                    "artifact_id": "art_empty",
                    "sha256": hashlib.sha256(b"").hexdigest(),
                    "size_bytes": 0,
                },
                "content_base64": base64.b64encode(b"").decode("ascii"),
            }
        )
        self.assertEqual(empty_payload["size_bytes"], 0)

        with self.assertRaisesRegex(ValueError, "sha256"):
            app.worker_artifact_upload_payload(
                {
                    "artifact": {"artifact_id": "art_report_human", "sha256": "0" * 64},
                    "content_base64": base64.b64encode(content).decode("ascii"),
                }
            )

    def test_db_review_run_artifact_store_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, patch.dict(
            os.environ,
            {"PULLWISE_DB_PATH": os.path.join(tmp_dir, "test.sqlite3")},
            clear=False,
        ):
            app.db.reset_initialization_cache()
            job = app.db.create_scan_job(
                {
                    "job_id": "job_artifact_v1",
                    "scan_id": "scan_artifact_v1",
                    "repo": "acme/api",
                    "branch": "main",
                    "commit": "pending",
                    "status": "queued",
                    "created_at": app.now(),
                    "user_id": "usr_1",
                }
            )
            claimed = app.db.claim_next_scan_job("wk_1")
            attempt_id = f"wk_1-{claimed['attempt']}"
            payload = {"artifact_id": "art_report_human", "sha256": "abc", "size_bytes": 3}

            first = app.db.store_review_run_artifact(
                job_id=job["job_id"],
                attempt_id=attempt_id,
                artifact_id="art_report_human",
                payload=payload,
            )
            duplicate = app.db.store_review_run_artifact(
                job_id=job["job_id"],
                attempt_id=attempt_id,
                artifact_id="art_report_human",
                payload=payload,
            )
            conflict = app.db.store_review_run_artifact(
                job_id=job["job_id"],
                attempt_id=attempt_id,
                artifact_id="art_report_human",
                payload={"artifact_id": "art_report_human", "sha256": "def"},
            )
            stored = app.db.list_review_run_artifacts(job["job_id"], attempt_id)

        self.assertTrue(first["accepted"])
        self.assertFalse(first["duplicate"])
        self.assertTrue(duplicate["accepted"])
        self.assertTrue(duplicate["duplicate"])
        self.assertTrue(conflict["conflict"])
        self.assertEqual(stored, [payload])

    def test_review_worker_protocol_requires_uploaded_required_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, patch.dict(
            os.environ,
            {"PULLWISE_DB_PATH": os.path.join(tmp_dir, "test.sqlite3")},
            clear=False,
        ):
            app.db.reset_initialization_cache()
            job = app.db.create_scan_job(
                {
                    "job_id": "job_requires_artifact_v1",
                    "scan_id": "scan_requires_artifact_v1",
                    "repo": "acme/api",
                    "branch": "main",
                    "commit": "pending",
                    "status": "queued",
                    "created_at": app.now(),
                    "user_id": "usr_1",
                }
            )
            claimed = app.db.claim_next_scan_job("wk_1")
            attempt_id = f"wk_1-{claimed['attempt']}"
            manifest = required_completed_manifest()
            body = {"attempt_id": attempt_id, "reviewWorkerProtocol": v1_envelope(claimed, manifest, worker_id="wk_1")}

            with self.assertRaisesRegex(ValueError, "not uploaded"):
                app.validate_review_worker_protocol_artifacts(claimed, body, status="done")

            for item in manifest:
                app.db.store_review_run_artifact(
                    job_id=job["job_id"],
                    attempt_id=attempt_id,
                    artifact_id=item["artifact_id"],
                    payload={
                        "artifact_id": item["artifact_id"],
                        "sha256": item["sha256"],
                        "size_bytes": item["size_bytes"],
                    },
                )
            app.validate_review_worker_protocol_artifacts(claimed, body, status="done")
            app.db.reset_initialization_cache()

    def test_review_worker_protocol_requires_completed_artifact_kinds(self) -> None:
        job = {
            "job_id": "job_1",
            "run_id": "run_job_1",
            "lease_id": "lease_job_1",
            "claimed_by_worker_id": "wk_1",
        }
        body = {"reviewWorkerProtocol": v1_envelope(job, [manifest_item()], worker_id="wk_1")}

        with self.assertRaisesRegex(ValueError, "required_completed_kinds"):
            app.validate_review_worker_protocol_envelope(job, body, status="done")

    def test_review_worker_protocol_requires_terminal_diagnostic_artifacts(self) -> None:
        job = {
            "job_id": "job_1",
            "run_id": "run_job_1",
            "lease_id": "lease_job_1",
            "claimed_by_worker_id": "wk_1",
        }
        body = {
            "reviewWorkerProtocol": v1_envelope(
                job,
                [
                    manifest_item(
                        artifact_id="art_worker_log",
                        kind="worker_log",
                        name="worker.log.jsonl",
                        media_type="application/jsonl",
                        schema_id="worker-log",
                    )
                ],
                status="failed",
                worker_id="wk_1",
            )
        }

        with self.assertRaisesRegex(ValueError, "required_terminal"):
            app.validate_review_worker_protocol_envelope(job, body, status="failed")

        passing = {"reviewWorkerProtocol": v1_envelope(job, required_terminal_manifest(), status="failed", worker_id="wk_1")}
        self.assertEqual(
            app.validate_review_worker_protocol_envelope(job, passing, status="failed"),
            passing["reviewWorkerProtocol"],
        )

    def test_review_worker_protocol_rejects_mismatched_job_binding(self) -> None:
        job = {
            "job_id": "job_1",
            "run_id": "run_job_1",
            "lease_id": "lease_job_1",
            "claimed_by_worker_id": "wk_1",
        }
        body = {"reviewWorkerProtocol": v1_envelope(job, [], worker_id="wk_1")}
        body["reviewWorkerProtocol"]["job"]["run_id"] = "run_other"

        with self.assertRaisesRegex(ValueError, "job.run_id"):
            app.validate_review_worker_protocol_envelope(job, body, status="done")

    def test_review_worker_protocol_rejects_invalid_manifest_metadata(self) -> None:
        job = {
            "job_id": "job_1",
            "run_id": "run_job_1",
            "lease_id": "lease_job_1",
            "claimed_by_worker_id": "wk_1",
        }
        body = {"reviewWorkerProtocol": v1_envelope(job, [manifest_item(size_bytes="3")], worker_id="wk_1")}

        with self.assertRaisesRegex(ValueError, "size_bytes"):
            app.validate_review_worker_protocol_envelope(job, body, status="done")

    def test_review_worker_protocol_requires_progress_final(self) -> None:
        job = {
            "job_id": "job_1",
            "run_id": "run_job_1",
            "lease_id": "lease_job_1",
            "claimed_by_worker_id": "wk_1",
        }
        body = {"reviewWorkerProtocol": v1_envelope(job, required_completed_manifest(), worker_id="wk_1")}
        body["reviewWorkerProtocol"].pop("progress_final")

        with self.assertRaisesRegex(ValueError, "progress_final"):
            app.validate_review_worker_protocol_envelope(job, body, status="done")

    def test_review_worker_protocol_requires_summary_minimum_fields(self) -> None:
        job = {
            "job_id": "job_1",
            "run_id": "run_job_1",
            "lease_id": "lease_job_1",
            "claimed_by_worker_id": "wk_1",
        }
        body = {"reviewWorkerProtocol": v1_envelope(job, required_completed_manifest(), worker_id="wk_1")}
        body["reviewWorkerProtocol"]["summary"] = {"top_findings": []}

        with self.assertRaisesRegex(ValueError, "summary.overall_risk"):
            app.validate_review_worker_protocol_envelope(job, body, status="done")

    def test_prepare_worker_result_accepts_v1_protocol(self) -> None:
        job = {
            "job_id": "job_1",
            "scan_id": "scan_1",
            "run_id": "run_job_1",
            "lease_id": "lease_job_1",
            "claimed_by_worker_id": "wk_1",
            "attempt": 1,
            "repo": "acme/api",
            "branch": "main",
            "commit": "pending",
            "user_id": "usr_1",
        }
        with tempfile.TemporaryDirectory() as tmp_dir, patch.dict(
            os.environ,
            {"PULLWISE_DB_PATH": os.path.join(tmp_dir, "test.sqlite3")},
            clear=False,
        ):
            app.db.reset_initialization_cache()
            app.db.create_scan_job(
                {
                    "job_id": "job_1",
                    "scan_id": "scan_1",
                    "repo": "acme/api",
                    "branch": "main",
                    "commit": "pending",
                    "status": "queued",
                    "created_at": app.now(),
                    "user_id": "usr_1",
                }
            )
            claimed = app.db.claim_next_scan_job("wk_1")
            manifest = required_completed_manifest()
            envelope = v1_envelope(claimed, manifest, worker_id="wk_1")
            envelope["summary"]["top_findings"] = [{"title": "Important issue", "severity": "high"}]
            attempt_id = f"wk_1-{claimed['attempt']}"
            for item in manifest:
                app.db.store_review_run_artifact(
                    job_id=claimed["job_id"],
                    attempt_id=attempt_id,
                    artifact_id=item["artifact_id"],
                    payload={
                        "artifact_id": item["artifact_id"],
                        "sha256": item["sha256"],
                        "size_bytes": item["size_bytes"],
                    },
                )
            body = {
                "status": "done",
                "attempt_id": attempt_id,
                "reviewWorkerProtocol": envelope,
                "duration_ms": 123,
            }

            prepared = app.prepare_worker_job_result_state(claimed, body, status="done", checksum="abc")
            app.db.reset_initialization_cache()

        self.assertEqual(prepared["review_worker_protocol"], envelope)
        self.assertEqual(len(prepared["normalized_findings"]), 1)
        self.assertEqual(prepared["normalized_findings"][0]["title"], "Important issue")
        self.assertEqual(app.worker_result_issue_count(body), 1)
    def test_public_review_worker_protocol_accepts_only_v1_envelope(self) -> None:
        envelope = {
            "protocol_version": "review-worker-protocol/v1",
            "message_type": "review_run_result",
            "job": {"job_id": "job_1", "run_id": "run_1", "lease_id": "lease_1"},
            "artifact_manifest": [],
        }

        self.assertEqual(app.public_review_worker_protocol(envelope), envelope)
        self.assertEqual(app.public_review_worker_protocol({"protocol_version": "legacy"}), {})
        self.assertEqual(app.public_review_worker_protocol(None), {})


if __name__ == "__main__":
    unittest.main()
