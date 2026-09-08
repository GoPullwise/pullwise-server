import json
from pathlib import Path
import subprocess

import pytest

from pullwise_server import app, db
from tests.test_scan_queue_admission import account  # noqa: F401
from tests.test_scan_quota_routes import RouteHarness as ScanRoute
from tests.test_worker_pull_routes import RouteHarness as WorkerRoute


@pytest.mark.parametrize("mode", ["failed", "unvalidated"])
def test_real_node_publication_upload_ingest_and_public_projection(account, mode):
    worker_id = "wk_result"
    token = db.create_worker({"worker_id": worker_id, "name": "Result fixture"})["worker_token"]
    created = ScanRoute({"repo": "acme/api", "requestId": "result-contract"}, cookie=account)
    app.PullwiseHandler.route(created, "POST")
    assert created.status == 201
    job = db.claim_next_scan_job(worker_id, lease_seconds=60, recover_before_claim=False, create_review_run=True)
    run_id = app.scan_job_attempt_run_id(job)
    wire_job = {**job, "run_id": run_id, "lease_id": f"lease_{job['job_id']}"}
    output = subprocess.run(["node", str(Path(__file__).parent / "fixtures/pi_result_publication.mjs")],
        input=json.dumps({"workerId": worker_id, "job": wire_job, "mode": mode}),
        text=True, encoding="utf-8", capture_output=True, timeout=30, check=True)
    publication = json.loads(output.stdout)
    headers = {"Authorization": f"Bearer {token}"}
    for artifact in publication["artifacts"]:
        upload = WorkerRoute(f"/v1/review-runs/{run_id}/artifacts", artifact, headers=headers)
        app.PullwiseHandler.route(upload, "POST")
        assert upload.status == 200, upload.payload
    result = WorkerRoute(f"/v1/review-runs/{run_id}/result", publication["result"], headers=headers)
    app.PullwiseHandler.route(result, "POST")
    assert result.status == 200, result.payload
    stored = db.get_review_run(run_id)
    assert json.loads(stored["usage_json"])["total"] == 40
    if mode == "failed":
        assert stored["status"] == "failed"
        assert abs(stored["started_at"] - app.now()) < 60
        assert stored["duration_ms"] == 250
        assert db.get_user_scan_snapshot("usr_queue", job["scan_id"])["quotaState"] == "released"
    else:
        envelope = publication["result"]["reviewWorkerProtocol"]
        assert envelope["summary"]["finding_counts"]["confirmed_high"] == 0
        assert envelope["summary"]["finding_counts"]["plausible"] == 1
        issues = app.worker_protocol_findings(job, envelope)
        public = app.issue_payload(issues[0])
        assert public["verificationStatus"] == "potential_risk"
        assert public["recommendation"] == "Validate first"
