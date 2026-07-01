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
            body = {
                "attempt_id": attempt_id,
                "reviewWorkerProtocol": v1_envelope(claimed, [manifest_item()], worker_id="wk_1"),
            }

            with self.assertRaisesRegex(ValueError, "not uploaded"):
                app.validate_review_worker_protocol_artifacts(claimed, body, status="done")

            app.db.store_review_run_artifact(
                job_id=job["job_id"],
                attempt_id=attempt_id,
                artifact_id="art_report_human",
                payload={"artifact_id": "art_report_human", "sha256": "abc", "size_bytes": 3},
            )
            app.validate_review_worker_protocol_artifacts(claimed, body, status="done")
            app.db.reset_initialization_cache()

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

    def test_prepare_worker_result_accepts_v1_without_graph_verified_report(self) -> None:
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
        envelope = v1_envelope(job, [], worker_id="wk_1")
        envelope["summary"] = {"top_findings": [{"title": "Important issue", "severity": "high"}]}
        body = {
            "status": "done",
            "attempt_id": "wk_1-1",
            "reviewWorkerProtocol": envelope,
            "duration_ms": 123,
        }

        prepared = app.prepare_worker_job_result_state(job, body, status="done", checksum="abc")

        self.assertEqual(prepared["review_worker_protocol"], envelope)
        self.assertEqual(prepared["graph_verified_report"], {})
        self.assertEqual(prepared["normalized_findings"], [])
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