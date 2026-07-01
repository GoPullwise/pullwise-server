from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import unittest
from unittest.mock import patch

from pullwise_server import app


class ReviewWorkerProtocolV1Test(unittest.TestCase):
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
            manifest_item = {
                "artifact_id": "art_report_human",
                "name": "report.md",
                "required": True,
                "sha256": "abc",
                "size_bytes": 3,
            }
            body = {
                "attempt_id": attempt_id,
                "reviewWorkerProtocol": {
                    "protocol_version": "review-worker-protocol/v1",
                    "message_type": "review_run_result",
                    "job": {"job_id": job["job_id"], "run_id": "run_1", "lease_id": "lease_1"},
                    "artifact_manifest": [manifest_item],
                },
            }

            with self.assertRaisesRegex(ValueError, "not uploaded"):
                app.validate_review_worker_protocol_artifacts(claimed, body)

            app.db.store_review_run_artifact(
                job_id=job["job_id"],
                attempt_id=attempt_id,
                artifact_id="art_report_human",
                payload={"artifact_id": "art_report_human", "sha256": "abc", "size_bytes": 3},
            )
            app.validate_review_worker_protocol_artifacts(claimed, body)
            app.db.reset_initialization_cache()

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