from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import closing
import zipfile
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app, db
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections


class RouteHarness(app.PullwiseHandler):
    def __init__(
        self,
        path: str,
        body: dict | None = None,
        *,
        headers: dict | None = None,
        raw_body: bytes | None = None,
    ) -> None:
        self.path = path
        self._body = body or {}
        self._raw_body = raw_body if raw_body is not None else json.dumps(self._body).encode("utf-8")
        self.headers = {"Host": "api.pullwise.dev", **(headers or {})}
        self.payload = None
        self.status = None
        self.headers_out = {}
        self.binary_payload = b""
        self.content_type = ""
        self.client_address = ("203.0.113.10", 51234)

    def read_json(self) -> dict:
        return self._body

    def read_raw_body(self) -> bytes:
        return self._raw_body

    def json(self, payload: dict, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.status = status
        self.headers_out = headers or {}

    def binary(
        self,
        payload: bytes,
        status: int = HTTPStatus.OK,
        *,
        content_type: str = "application/octet-stream",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.binary_payload = payload
        self.status = status
        self.content_type = content_type
        self.headers_out = headers or {}

    def error(self, status: int, message: str) -> None:
        self.json({"message": message}, status)


class RawBodyRouteHarness(RouteHarness):
    def read_raw_body(self) -> bytes:
        self.rfile = io.BytesIO(self._raw_body)
        return app.PullwiseHandler.read_raw_body(self)

    def read_json(self) -> dict:
        return app.PullwiseHandler.read_json(self)


def review_worker_severity(value: object) -> str:
    text = str(value or "medium").lower()
    return {"p0": "critical", "p1": "high", "p2": "medium", "p3": "low"}.get(text, text if text in {"critical", "high", "medium", "low", "info"} else "medium")


def review_worker_fixture_file(value: object) -> str:
    text = str(value or "src/app.py").replace("\\", "/").strip()
    if not text or text.startswith("/") or ":" in text:
        return "src/app.py"
    return text


def audit_verification(issue_id: str, *, verdict: str = "confirmed", evidence: list[str] | None = None, **overrides: object) -> dict:
    payload = {
        "issue_id": issue_id,
        "verdict": verdict,
        "evidence": evidence or [],
        "resultSummary": str(overrides.get("result_summary") or "; ".join(evidence or []) or verdict),
    }
    payload.update(overrides)
    return payload

def review_worker_top_finding_from_card(card: dict, results: list[dict], index: int) -> dict:
    issue_id = str(card.get("issue_id") or card.get("issueId") or card.get("id") or f"issue-{index + 1}")
    location = card.get("location") if isinstance(card.get("location"), dict) else {}
    file_path = review_worker_fixture_file(card.get("file") or location.get("file") or "src/app.py")
    line = int(card.get("line") or location.get("line") or location.get("startLine") or 10)
    title = str(card.get("title") or card.get("claim") or f"Review worker issue {index + 1}")
    result = next((item for item in results if str(item.get("issue_id") or item.get("issueId") or "") == issue_id), {})
    evidence = card.get("evidence") if isinstance(card.get("evidence"), list) else []
    evidence_items = []
    for item in evidence[:4]:
        if isinstance(item, dict):
            evidence_items.append({
                "type": str(item.get("type") or "code"),
                "file": review_worker_fixture_file(item.get("file") or item.get("path") or file_path),
                "line": int(item.get("line") or item.get("startLine") or line),
                "summary": str(item.get("summary") or item.get("text") or item.get("why_it_matters") or "Code evidence"),
            })
    if not evidence_items:
        evidence_items.append({"type": "code", "file": file_path, "line": line, "summary": "Code evidence"})
    return {
        "id": issue_id,
        "title": title,
        "severity": review_worker_severity(card.get("severity")),
        "description": str(card.get("summary") or card.get("description") or result.get("resultSummary") or title),
        "recommendation": str(card.get("fix_direction") or card.get("recommendation") or "Fix the confirmed behavior and rerun the relevant checks."),
        "location": {"file": file_path, "line": line},
        "locations": [{"file": file_path, "startLine": line, "endLine": int(card.get("end_line") or card.get("endLine") or line)}],
        "evidence": evidence_items,
        "reproduction": card.get("reproduction") if isinstance(card.get("reproduction"), dict) else {},
        "whyNotFalsePositive": card.get("false_positive_checks") or card.get("whyNotFalsePositive") or [],
        "limitations": card.get("limitations") or [],
        "status": "open",
    }


def current_review_worker_job_for_test() -> dict:
    import inspect
    frame = inspect.currentframe()
    caller = frame.f_back if frame is not None else None
    while caller is not None:
        for name in ("remaining_job", "second_job", "claimed_job", "claimed", "job", "first_job"):
            value = caller.f_locals.get(name)
            if isinstance(value, dict) and value.get("job_id"):
                stored = db.get_scan_job(str(value.get("job_id")))
                if stored:
                    return {**value, **stored}
                return value
        caller = caller.f_back
    connection = app.db.connect()
    try:
        cursor = connection.execute(
            """
            SELECT * FROM scan_jobs
            WHERE status IN ('claimed', 'running', 'uploading_result', 'queued', 'done', 'failed')
            ORDER BY claimed_at DESC, created_at DESC, job_id DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()
        columns = [column[0] for column in cursor.description or []]
    finally:
        connection.close()
    return dict(zip(columns, row)) if row is not None else {}


def audit_issue_card(title: str, **overrides: object) -> dict:
    card = {
        "issue_id": overrides.pop("issue_id", overrides.pop("issueId", None)) or "issue-test",
        "title": title,
        "severity": overrides.pop("severity", "P1"),
        "file": overrides.pop("file", "src/app.py"),
        "line": overrides.pop("line", 12),
        "summary": overrides.pop("summary", title),
        "recommendation": overrides.pop("recommendation", "Fix the issue and rerun the relevant checks."),
    }
    card.update(overrides)
    return card


def protocol_artifact_item(
    run_id: str,
    artifact_id: str,
    kind: str,
    name: str,
    media_type: str,
    schema_id: str,
    *,
    required: bool = True,
) -> dict:
    content = f"{kind}:{name}\n".encode("utf-8")
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "name": name,
        "media_type": media_type,
        "schema_id": schema_id,
        "schema_version": "v1",
        "encoding": "utf-8",
        "compression": "none",
        "required": required,
        "storage": {"type": "server_artifact", "url": f"/v1/review-runs/{run_id}/artifacts/{artifact_id}"},
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def protocol_artifact_manifest(run_id: str, execution_status: str) -> list[dict]:
    if execution_status == "completed":
        return [
            protocol_artifact_item(run_id, "art_report_human", "report.human", "report.md", "text/markdown", "human-markdown-report"),
            protocol_artifact_item(run_id, "art_report_agent", "report.agent", "report.agent.json", "application/json", "codex-full-repo-review"),
            protocol_artifact_item(run_id, "art_coverage", "coverage", "coverage.json", "application/json", "coverage"),
            protocol_artifact_item(run_id, "art_qa", "qa", "qa.json", "application/json", "qa-gate"),
            protocol_artifact_item(run_id, "art_token_budget", "token_budget", "token-budget.json", "application/json", "token-budget"),
        ]
    return [
        protocol_artifact_item(run_id, "art_worker_log", "worker_log", "worker.log.jsonl", "application/jsonl", "worker-log"),
        protocol_artifact_item(run_id, "art_qa", "qa", "qa.json", "application/json", "qa-gate"),
        protocol_artifact_item(run_id, "art_error_report", "error_report", "error-report.json", "application/json", "error-report"),
    ]


def upload_protocol_artifacts_for_test(job: dict, manifest: list[dict]) -> None:
    job_id = str(job.get("job_id") or "").strip()
    worker_id = str(job.get("claimed_by_worker_id") or "wk_1").strip()
    run_id = str(job.get("run_id") or f"run_{job_id}").strip()
    if not job_id or not worker_id:
        return
    attempt_id = f"{worker_id}-{int(job.get('attempt') or 1)}"
    for item in manifest:
        existing = db.get_review_run_artifact(run_id, item["artifact_id"])
        if existing:
            item["sha256"] = existing["sha256"]
            item["size_bytes"] = existing["size_bytes"]
            if existing.get("storage_json"):
                item["storage"] = json.loads(existing["storage_json"])
            continue
        db.store_review_run_artifact(
            job_id=job_id,
            attempt_id=attempt_id,
            artifact_id=item["artifact_id"],
            payload={
                "run_id": run_id,
                "artifact_id": item["artifact_id"],
                "sha256": item["sha256"],
                "size_bytes": item["size_bytes"],
                "artifact": dict(item),
            },
        )


def protocol_summary(findings: list[dict], execution_status: str) -> dict:
    def count(severity: str) -> int:
        return sum(1 for finding in findings if str(finding.get("severity") or "").lower() == severity)

    return {
        "overall_risk": "unknown",
        "result_status": "complete" if execution_status == "completed" else "incomplete",
        "finding_counts": {
            "confirmed_critical": count("critical"),
            "confirmed_high": count("high"),
            "confirmed_medium": count("medium"),
            "confirmed_low": count("low"),
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


def v1_worker_heartbeat_payload(*, status: str = "idle", run_id: str | None = None) -> dict:
    active = status != "idle"
    payload = {
        "protocol_version": "review-worker-protocol/v1",
        "worker_id": "wk_1",
        "status": status,
        "active_run_id": run_id if active else None,
        "concurrency": {
            "max_active_jobs": 1,
            "active_jobs": 1 if active else 0,
            "available_job_slots": 0 if active else 1,
            "maintains_local_queue": False,
            "local_queue_depth": 0,
        },
        "codex_app_server": {
            "status": "ready",
            "transport": "stdio",
            "active_thread_id": "thr_1" if active else None,
        },
    }
    if active:
        payload["progress"] = {
            "run_id": run_id,
            "overall_percent": 12.5,
            "current_phase": "repo_map",
            "current_phase_status": "running",
            "current_phase_percent": 50.0,
            "message": "Mapping repository.",
            "counters": {
                "source_like_files_total": 10,
                "source_like_files_classified": 10,
                "bundles_total": 2,
                "bundles_packed": 2,
                "reviewer_runs_total": 4,
                "reviewer_runs_completed": 1,
                "intent_tests_total": 0,
                "intent_tests_written": 0,
                "intent_tests_run": 0,
                "validator_candidates_total": 0,
                "validator_candidates_completed": 0,
                "artifacts_total": 0,
                "artifacts_uploaded": 0,
            },
            "active_unit": {},
            "last_event_sequence": 4,
            "updated_at": "2026-07-01T10:42:00Z",
        }
    return payload


def v1_worker_lease_payload() -> dict:
    return {
        "protocol_version": "review-worker-protocol/v1",
        "worker_id": "wk_1",
        "capacity": {
            "available_job_slots": 1,
            "active_jobs": 0,
            "maintains_local_queue": False,
            "local_queue_depth": 0,
        },
        "capabilities": {
            "full_repo_scan": True,
            "codex_app_server": True,
            "isolated_codex_home": True,
            "progress_events": True,
            "cancellation": True,
            "intent_test_validation": True,
        },
    }


def audit_result_fields(
    issue_cards: list[dict],
    verification_results: list[dict] | None = None,
    *,
    execution_status: str = "completed",
) -> dict:
    job = current_review_worker_job_for_test()
    job_id = str(job.get("job_id") or "job_test")
    run_id = str(job.get("run_id") or f"run_{job_id}")
    lease_id = str(job.get("lease_id") or f"lease_{job_id}")
    worker_id = str(job.get("claimed_by_worker_id") or "wk_1")
    manifest = protocol_artifact_manifest(run_id, execution_status)
    upload_protocol_artifacts_for_test(job, manifest)
    results = verification_results or []
    results_by_issue: dict[str, list[dict]] = {}
    for result in results:
        issue_id = str(result.get("issue_id") or result.get("issueId") or "")
        if issue_id:
            results_by_issue.setdefault(issue_id, []).append(result)
    findings = []
    for index, card in enumerate(issue_cards):
        issue_id = str(card.get("issue_id") or card.get("issueId") or card.get("id") or "")
        card_results = results_by_issue.get(issue_id, [])
        if any(str(result.get("verdict") or "").lower() == "rejected" for result in card_results):
            continue
        findings.append(review_worker_top_finding_from_card(card, card_results, index))
    return {
        "reviewWorkerProtocol": {
            "protocol_version": "review-worker-protocol/v1",
            "message_type": "review_run_result",
            "job": {"job_id": job_id, "run_id": run_id, "lease_id": lease_id, "job_type": "repo_review.full_scan"},
            "worker": {
                "worker_id": worker_id,
                "worker_version": "0.1.0",
                "concurrency": {"max_active_jobs": 1, "maintains_local_queue": False},
                "engine": {"type": "codex_app_server", "app_server_transport": "stdio"},
            },
            "execution": {"status": execution_status, "review_mode": "full_repo"},
            "progress_final": {
                "overall_percent": 100.0 if execution_status == "completed" else 41.5,
                "current_phase": "submit_result_envelope" if execution_status == "completed" else "failure_handling",
                "status": "completed" if execution_status == "completed" else execution_status,
                "message": "terminal progress",
            },
            "quality_gate": {"status": "pass" if execution_status == "completed" else "fail", "errors": [], "warnings": []},
            "artifact_manifest": manifest,
            "summary": protocol_summary(findings, execution_status),
        }
    }

class WorkerPullRoutesTest(unittest.TestCase):
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
        install_initialized_db_template(os.environ["PULLWISE_DB_PATH"], worker_token="worker-secret", worker_id="wk_1")
        db.upsert_worker_heartbeat(
            {
                "worker_id": "wk_1",
                "version": "0.1.0",
                "provider": "codex",
                "provider_chain": ["codex"],
                "max_concurrent_jobs": 2,
                "running_jobs": 0,
                "free_slots": 2,
                "doctor_status": "ok",
                "codex_ready": 1,
                "ready_providers": ["codex"],
                "timestamp": app.now(),
            }
        )
        self.auth = {"Authorization": "Bearer worker-secret"}

    def test_scan_payload_exposes_uploaded_worker_debug_bundle_url(self) -> None:
        scan = {
            "id": "sc_debug_bundle",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc1234",
            "status": "running",
            "userId": "usr_1",
            "repoId": "repo_1",
            "createdAt": app.now(),
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)
        job = db.claim_next_scan_job("wk_1", ready_providers=["codex"], recover_before_claim=False)
        self.assertIsNotNone(job)
        run_id = app.scan_job_run_id(job)
        artifact = protocol_artifact_item(
            run_id,
            "art_debug_bundle",
            "debug_bundle",
            "debug-bundle.zip",
            "application/zip",
            "pullwise-debug-bundle",
            required=False,
        )
        debug_buffer = io.BytesIO()
        with zipfile.ZipFile(debug_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("debug-summary.json", "{}\n")
            archive.writestr("run/worker.log.jsonl", "{\"event\":\"phase_failed\"}\n")
        debug_zip = debug_buffer.getvalue()
        artifact["sha256"] = hashlib.sha256(debug_zip).hexdigest()
        artifact["size_bytes"] = len(debug_zip)
        upload = RouteHarness(
            f"/v1/review-runs/{run_id}/artifacts",
            {
                "protocol_version": "review-worker-protocol/v1",
                "attempt_id": "wk_1-1",
                "run_id": run_id,
                "artifact": artifact,
                "content_base64": base64.b64encode(debug_zip).decode("ascii"),
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(upload, "POST")
        self.assertEqual(upload.status, HTTPStatus.OK)
        db.update_review_run_progress(
            {
                "run_id": run_id,
                "job_id": job["job_id"],
                "worker_id": "wk_1",
                "event_type": "progress_updated",
                "phase": "upload_artifacts",
                "severity": "info",
                "status": "running",
                "progress": 99,
                "created_at": app.now(),
            }
        )

        payload = app.scan_payload(scan)

        self.assertEqual(payload["reviewRun"]["debugBundleUrl"], f"/v1/review-runs/{run_id}/artifacts/art_debug_bundle")
        self.assertEqual(payload["debugBundleUrl"], f"/v1/review-runs/{run_id}/artifacts/art_debug_bundle")
        list_payload = app.scan_list_payload(scan)
        self.assertEqual(list_payload["debugBundleUrl"], f"/v1/review-runs/{run_id}/artifacts/art_debug_bundle")
        debug_artifacts = [item for item in payload["reviewRun"]["artifacts"] if item.get("kind") == "debug_bundle"]
        self.assertEqual(debug_artifacts[0]["name"], "debug-bundle.zip")

        app.USERS = {"usr_1": {"id": "usr_1", "createdAt": app.now(), "providers": ["github"]}}
        app.SESSIONS = {"ses_1": {"id": "ses_1", "userId": "usr_1", "createdAt": app.now(), "expiresAt": app.now() + 3600}}
        download = RouteHarness(
            f"/v1/review-runs/{run_id}/artifacts/art_debug_bundle",
            headers={"Cookie": f"{app.SESSION_COOKIE}=ses_1"},
        )
        app.PullwiseHandler.route(download, "GET")
        self.assertEqual(download.status, HTTPStatus.OK)
        self.assertEqual(download.content_type, "application/zip")
        with zipfile.ZipFile(io.BytesIO(download.binary_payload), "r") as archive:
            names = set(archive.namelist())
            self.assertIn("worker/debug-summary.json", names)
            self.assertIn("worker/run/worker.log.jsonl", names)
            self.assertIn("server/server-debug-evidence.json", names)
            self.assertNotIn("audit.json", names)
            self.assertFalse(any(name.endswith("audit-bundle.zip") for name in names))
            server_evidence = json.loads(archive.read("server/server-debug-evidence.json").decode("utf-8"))
        self.assertEqual(server_evidence["schema_version"], "pullwise-server-debug-evidence/v1")
        self.assertEqual(server_evidence["run_id"], run_id)
        self.assertEqual(server_evidence["scan"]["id"], "sc_debug_bundle")
        self.assertEqual(server_evidence["scan_job_attempts"][0]["worker_id"], "wk_1")
        self.assertEqual(server_evidence["database_records"]["scan_job_attempts"][0]["worker_id"], "wk_1")
        self.assertIn("review_run_events", server_evidence["server_logs"])

    def test_failed_result_exposes_debug_bundle_when_later_artifact_uploads_fail(self) -> None:
        scan = {
            "id": "sc_failed_debug_bundle",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc1234",
            "status": "running",
            "userId": "usr_1",
            "repoId": "repo_1",
            "createdAt": app.now(),
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)
        job = db.claim_next_scan_job("wk_1", ready_providers=["codex"], recover_before_claim=False)
        self.assertIsNotNone(job)
        run_id = app.scan_job_run_id(job)
        manifest = protocol_artifact_manifest(run_id, "failed")
        debug_artifact = protocol_artifact_item(
            run_id,
            "art_debug_bundle",
            "debug_bundle",
            "debug-bundle.zip",
            "application/zip",
            "pullwise-debug-bundle",
            required=False,
        )
        debug_buffer = io.BytesIO()
        with zipfile.ZipFile(debug_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("debug-summary.json", json.dumps({"status": "failed"}) + "\n")
            archive.writestr("run/worker.log.jsonl", "{\"event\":\"job_failed\"}\n")
        debug_zip = debug_buffer.getvalue()
        debug_artifact["sha256"] = hashlib.sha256(debug_zip).hexdigest()
        debug_artifact["size_bytes"] = len(debug_zip)
        manifest.append(debug_artifact)

        upload = RouteHarness(
            f"/v1/review-runs/{run_id}/artifacts",
            {
                "protocol_version": "review-worker-protocol/v1",
                "attempt_id": "wk_1-1",
                "run_id": run_id,
                "artifact": debug_artifact,
                "content_base64": base64.b64encode(debug_zip).decode("ascii"),
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(upload, "POST")
        self.assertEqual(upload.status, HTTPStatus.OK)

        result_body = {
            "status": "failed",
            "attempt_id": "wk_1-1",
            "duration_ms": 1234,
            "error": "artifact upload failed after debug bundle upload",
            "error_code": "CODEX_QUOTA_EXHAUSTED",
            "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "reviewWorkerProtocol": {
                "protocol_version": "review-worker-protocol/v1",
                "message_type": "review_run_result",
                "job": {
                    "job_id": job["job_id"],
                    "run_id": run_id,
                    "lease_id": f"lease_{job['job_id']}",
                    "job_type": "repo_review.full_scan",
                },
                "worker": {
                    "worker_id": "wk_1",
                    "worker_version": "0.1.0",
                    "concurrency": {"max_active_jobs": 1, "maintains_local_queue": False},
                    "engine": {"type": "codex_app_server", "app_server_transport": "stdio"},
                },
                "execution": {"status": "failed", "review_mode": "full_repo"},
                "progress_final": {
                    "run_id": run_id,
                    "overall_percent": 41.5,
                    "current_phase": "failure_handling",
                    "status": "failed",
                    "message": "Run failed.",
                },
                "error": {
                    "code": "CODEX_QUOTA_EXHAUSTED",
                    "category": "codex_usage_limit_exceeded",
                    "message": "artifact upload failed after debug bundle upload",
                    "retryable": False,
                    "failure_action": "fail_job_terminal",
                },
                "quality_gate": {"status": "fail", "errors": ["Run failed."], "warnings": []},
                "artifact_manifest": manifest,
                "summary": protocol_summary([], "failed"),
                "extensions": {"worker_internal": {"artifact_upload_error": "qa upload failed"}},
            },
        }
        result = RouteHarness(f"/v1/review-runs/{run_id}/result", result_body, headers=self.auth)
        app.PullwiseHandler.route(result, "POST")
        self.assertEqual(result.status, HTTPStatus.OK)

        payload = app.scan_payload(scan)

        self.assertEqual(payload["reviewRun"]["status"], "failed")
        self.assertEqual(payload["reviewRun"]["debugBundleUrl"], f"/v1/review-runs/{run_id}/artifacts/art_debug_bundle")

    def test_legacy_worker_review_routes_are_removed(self) -> None:
        legacy_routes = [
            ("/worker/heartbeat", {"worker_id": "wk_1", "status": "idle"}),
            ("/worker/agent-configs", {"worker_id": "wk_1"}),
            ("/worker/jobs/claim", {"worker_id": "wk_1"}),
            ("/worker/jobs/job_1/progress", {"worker_id": "wk_1", "progress": 10}),
            (
                "/worker/jobs/job_1/artifacts/art_1",
                {"worker_id": "wk_1", "content_base64": base64.b64encode(b"{}").decode("ascii")},
            ),
            ("/worker/jobs/job_1/result", {"worker_id": "wk_1", "status": "done"}),
        ]

        for path, payload in legacy_routes:
            with self.subTest(path=path):
                handler = RouteHarness(path, payload, headers=self.auth)
                app.PullwiseHandler.route(handler, "POST")

                self.assertEqual(handler.status, HTTPStatus.NOT_FOUND)
                self.assertEqual(handler.payload["message"], "Route not found")

    def test_scan_payload_omits_ai_usage_even_when_scan_has_legacy_usage(self) -> None:
        scan = {
            "id": "sc_ai_usage_legacy",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc1234",
            "status": "done",
            "userId": "usr_1",
            "createdAt": app.now(),
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "aiUsage": {"provider": "codex", "model": "gpt-5.5", "reasoningEffort": "medium"},
            "effectiveAgentConfig": {
                "provider": "codex",
                "agent": {"command": "codex", "model": "gpt-5.5", "reasoningEffort": "medium"},
            },
        }

        payload = app.scan_payload(scan)
        list_payload = app.scan_list_payload(scan)

        self.assertIn("effectiveAgentConfig", payload)
        self.assertIn("effectiveAgentConfig", list_payload)
        self.assertNotIn("aiUsage", payload)
        self.assertNotIn("aiUsage", list_payload)

    def test_scan_payload_includes_agent_fix_prompt_with_bundle_url(self) -> None:
        scan = {
            "id": "sc_agent_prompt",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc1234",
            "status": "done",
            "userId": "usr_1",
            "repoId": "repo_123",
            "createdAt": app.now(),
            "issues": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
            "agentReport": {
                "oneLine": "Review worker completed with 1 confirmed finding.",
                "issueIndex": [
                    {
                        "id": "issue-auth-cache",
                        "severity": "high",
                        "title": "Auth cache can return stale permissions",
                        "primaryFile": "src/auth.py",
                        "primaryLine": 42,
                    }
                ],
            },
            "readingGuide": {"forAgentFix": "agentFixPrompt"},
        }

        with patch.dict(os.environ, {"PULLWISE_API_BASE_URL": "https://api.pullwise.dev"}):
            payload = app.scan_payload(scan)

        prompt = payload["agentFixPrompt"]
        self.assertIn("Task: fix the Pullwise scan findings", prompt)
        self.assertIn("Repository: acme/api", prompt)
        self.assertIn("Confirmed issues: 1", prompt)
        self.assertIn("high: Auth cache can return stale permissions", prompt)
        self.assertIn("src/auth.py:42", prompt)
        self.assertIn(
            "https://api.pullwise.dev/api/v1/repositories/repo_123/scans/sc_agent_prompt/audit-bundle.zip",
            prompt,
        )
        self.assertEqual(payload["readingGuide"]["forAgentFix"], "agentFixPrompt")

    def test_v1_worker_protocol_routes_lease_events_artifacts_and_result(self) -> None:
        scan = {
            "id": "sc_v1_protocol",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc1234",
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.USERS = {
            "usr_1": {"id": "usr_1", "name": "Owner", "providers": []},
            "usr_2": {"id": "usr_2", "name": "Other", "providers": []},
        }
        app.SESSIONS = {
            "ses_owner": {"id": "ses_owner", "userId": "usr_1", "createdAt": app.now(), "expiresAt": app.now() + 3600},
            "ses_other": {"id": "ses_other", "userId": "usr_2", "createdAt": app.now(), "expiresAt": app.now() + 3600},
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)

        register = RouteHarness(
            "/v1/workers/register",
            {
                "protocol_version": "review-worker-protocol/v1",
                "worker": {
                    "worker_id": "wk_1",
                    "worker_group": "default",
                    "worker_version": "0.1.0",
                    "hostname": "worker-host",
                    "concurrency": {
                        "max_active_jobs": 1,
                        "maintains_local_queue": False,
                        "prefetch_jobs": False,
                    },
                    "platform": {"os": "linux", "arch": "x86_64"},
                    "capabilities": {
                        "codex_app_server": True,
                        "full_repo_scan": True,
                        "progress_events": True,
                        "cancellation": True,
                        "intent_test_validation": True,
                        "max_active_jobs": 1,
                    },
                },
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(register, "POST")

        self.assertEqual(register.status, HTTPStatus.OK)
        self.assertTrue(register.payload["accepted"])
        self.assertEqual(register.payload["accepted_protocol_versions"], ["review-worker-protocol/v1"])
        self.assertEqual(register.payload["worker"]["worker_id"], "wk_1")
        self.assertEqual(register.payload["token_delivery"], "preissued")
        self.assertNotIn("worker_token", register.payload)
        stored_worker = db.get_worker("wk_1")
        self.assertEqual(stored_worker["protocol_version"], "review-worker-protocol/v1")
        self.assertEqual(stored_worker["worker_group"], "default")
        self.assertEqual(json.loads(stored_worker["registration_json"])["worker"]["worker_id"], "wk_1")
        self.assertTrue(json.loads(stored_worker["worker_capabilities"])["progress_events"])

        lease = RouteHarness(
            "/v1/workers/wk_1/lease",
            v1_worker_lease_payload(),
            headers=self.auth,
        )
        app.PullwiseHandler.route(lease, "POST")

        self.assertEqual(lease.status, HTTPStatus.OK)
        claimed = lease.payload["job"]
        run_id = lease.payload["lease"]["run_id"]
        attempt_id = f"wk_1-{claimed['attempt']}"
        self.assertEqual(claimed["job_type"], "repo_review.full_scan")
        self.assertEqual(claimed["priority"], "normal")
        self.assertEqual(claimed["repository"]["provider"], "github")
        self.assertEqual(claimed["repository"]["owner"], "acme")
        self.assertEqual(claimed["repository"]["name"], "api")
        self.assertEqual(claimed["repository"]["commit_sha"], "abc1234")
        self.assertEqual(claimed["model_profile"]["default_model"], claimed["agentConfig"]["codex"]["model"])
        self.assertEqual(claimed["model_profile"]["core_effort"], claimed["agentConfig"]["codex"]["reasoningEffort"])
        self.assertEqual(claimed["model_profile"]["non_core_effort"], "medium")
        self.assertEqual(claimed["review_request"]["mode"], "full_repo")
        self.assertEqual(claimed["review_request"]["profile"], "standard")
        self.assertFalse(claimed["review_request"]["policy"]["allow_source_modification"])
        self.assertFalse(claimed["review_request"]["policy"]["allow_dependency_install"])
        self.assertFalse(claimed["review_request"]["policy"]["allow_network"])
        self.assertTrue(claimed["review_request"]["policy"]["helper_scripts_standard_library_only"])
        self.assertEqual(
            claimed["review_request"]["policy"]["turn_timeout_seconds"],
            claimed["agentConfig"]["reviewWorker"]["turnTimeoutSeconds"],
        )
        self.assertEqual(
            claimed["review_request"]["budget"]["max_wall_time_seconds"],
            claimed["agentConfig"]["reviewWorker"]["scanDeadlineSeconds"],
        )
        self.assertEqual(
            claimed["review_request"]["policy"]["intent_test_validation"]["max_tests_per_run"],
            20,
        )
        self.assertEqual(claimed["run_id"], run_id)
        self.assertEqual(claimed["lease_id"], lease.payload["lease"]["lease_id"])
        claimed_run = db.get_review_run(run_id)
        self.assertEqual(claimed_run["job_id"], claimed["job_id"])
        self.assertEqual(claimed_run["worker_id"], "wk_1")
        self.assertEqual(claimed_run["status"], "leased")
        self.assertEqual(claimed_run["protocol_version"], "review-worker-protocol/v1")

        heartbeat = RouteHarness(
            "/v1/workers/wk_1/heartbeat",
            v1_worker_heartbeat_payload(status="busy", run_id=run_id),
            headers=self.auth,
        )
        app.PullwiseHandler.route(heartbeat, "POST")
        self.assertEqual(heartbeat.status, HTTPStatus.OK)
        self.assertTrue(heartbeat.payload["ack"])
        self.assertEqual(heartbeat.payload["commands"], [])
        self.assertEqual(db.get_worker("wk_1")["running_jobs"], 1)
        progress_after_heartbeat = json.loads(db.get_review_run(run_id)["progress_json"])
        self.assertEqual(progress_after_heartbeat["event_type"], "progress_updated")
        self.assertEqual(progress_after_heartbeat["phase"], "repo_map")
        self.assertEqual(progress_after_heartbeat["overall_percent"], 12)
        self.assertEqual(scan["progress"], 12.5)
        self.assertEqual(scan["phase"], "repo_map")

        event = RouteHarness(
            f"/v1/review-runs/{run_id}/events",
            {
                "protocol_version": "review-worker-protocol/v1",
                "run_id": run_id,
                "worker_id": "wk_1",
                "sequence": 1,
                "timestamp": "2026-07-01T10:22:00Z",
                "event_type": "phase_started",
                "phase": "reviewer_fanout",
                "severity": "info",
                "message": "Reviewer fanout started.",
                "progress": {"overall_percent": 42.0, "current_phase_percent": 0, "status": "running"},
                "data": {"bundle_count": 3},
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(event, "POST")

        self.assertEqual(event.status, HTTPStatus.OK)
        self.assertTrue(event.payload["ack"])
        self.assertEqual(event.payload["sequence"], 1)
        self.assertEqual(app.SCANS[0]["phase"], "reviewer_fanout")
        self.assertEqual(app.SCANS[0]["progress"], 42)
        self.assertEqual(app.SCANS[0]["progressLogs"][0]["logsSummary"], "phase_started")
        stored_events = db.list_review_run_events(run_id)
        self.assertEqual(len(stored_events), 1)
        self.assertEqual(stored_events[0]["sequence"], 1)
        self.assertEqual(stored_events[0]["event_type"], "phase_started")
        self.assertEqual(json.loads(stored_events[0]["payload"])["data"]["bundle_count"], 3)
        event_run = db.get_review_run(run_id)
        self.assertEqual(event_run["status"], "running")
        self.assertEqual(json.loads(event_run["progress_json"])["sequence"], 1)
        self.assertEqual(json.loads(event_run["progress_json"])["phase"], "reviewer_fanout")

        duplicate_event = RouteHarness(
            f"/v1/review-runs/{run_id}/events",
            {
                "protocol_version": "review-worker-protocol/v1",
                "run_id": run_id,
                "worker_id": "wk_1",
                "sequence": 1,
                "timestamp": "2026-07-01T10:23:00Z",
                "event_type": "progress_updated",
                "phase": "reviewer_fanout",
                "severity": "info",
                "message": "Duplicate sequence.",
                "progress": {"overall_percent": 43.0, "current_phase_percent": 10, "status": "running"},
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(duplicate_event, "POST")

        self.assertEqual(duplicate_event.status, HTTPStatus.CONFLICT)
        self.assertIn("monotonic", duplicate_event.payload["message"])
        self.assertEqual(len(db.list_review_run_events(run_id)), 1)

        unsupported_event = RouteHarness(
            f"/v1/review-runs/{run_id}/events",
            {
                "protocol_version": "review-worker-protocol/v1",
                "run_id": run_id,
                "worker_id": "wk_1",
                "sequence": 2,
                "timestamp": "2026-07-01T10:24:00Z",
                "event_type": "unsupported_v1_event",
                "phase": "reviewer_fanout",
                "severity": "info",
                "message": "Unsupported event type.",
                "progress": {"overall_percent": 44.0, "current_phase_percent": 12, "status": "running"},
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(unsupported_event, "POST")

        self.assertEqual(unsupported_event.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(len(db.list_review_run_events(run_id)), 1)

        def clone_event(payload: dict) -> dict:
            return json.loads(json.dumps(payload))

        valid_event = {
            "protocol_version": "review-worker-protocol/v1",
            "run_id": run_id,
            "worker_id": "wk_1",
            "sequence": 2,
            "timestamp": "2026-07-01T10:25:00Z",
            "event_type": "progress_updated",
            "phase": "reviewer_fanout",
            "severity": "info",
            "message": "Invalid supported event shape.",
            "progress": {"overall_percent": 45.0, "current_phase_percent": 15, "status": "running"},
            "data": {},
        }
        missing_progress = clone_event(valid_event)
        missing_progress.pop("progress")
        missing_phase = clone_event(valid_event)
        missing_phase.pop("phase")
        worker_mismatch = clone_event(valid_event)
        worker_mismatch["worker_id"] = "wk_other"
        missing_severity = clone_event(valid_event)
        missing_severity.pop("severity")
        invalid_data = clone_event(valid_event)
        invalid_data["data"] = []
        for label, payload in [
            ("missing_progress", missing_progress),
            ("missing_phase", missing_phase),
            ("worker_mismatch", worker_mismatch),
            ("missing_severity", missing_severity),
            ("invalid_data", invalid_data),
        ]:
            with self.subTest(label=label):
                invalid_event = RouteHarness(f"/v1/review-runs/{run_id}/events", payload, headers=self.auth)
                app.PullwiseHandler.route(invalid_event, "POST")
                self.assertEqual(invalid_event.status, HTTPStatus.BAD_REQUEST)
                self.assertIn("Invalid review-worker-protocol/v1 event", invalid_event.payload["message"])
                self.assertEqual(len(db.list_review_run_events(run_id)), 1)

        partial_event_payload = clone_event(valid_event)
        partial_event_payload.update(
            {
                "event_type": "run_partial_completed",
                "phase": "qa_gate",
                "message": "Partial result available.",
                "progress": {"overall_percent": 99.0, "current_phase_percent": 100, "status": "partial_completed"},
                "data": {"reason": "qa gate failed after artifacts were produced"},
            }
        )
        partial_event = RouteHarness(f"/v1/review-runs/{run_id}/events", partial_event_payload, headers=self.auth)
        app.PullwiseHandler.route(partial_event, "POST")

        self.assertEqual(partial_event.status, HTTPStatus.OK)
        self.assertTrue(partial_event.payload["ack"])
        self.assertEqual(len(db.list_review_run_events(run_id)), 2)
        partial_run = db.get_review_run(run_id)
        self.assertEqual(partial_run["status"], "partial_completed")
        self.assertEqual(json.loads(partial_run["progress_json"])["status"], "partial_completed")

        content = b"{}"
        artifact_payload = {
            "protocol_version": "review-worker-protocol/v1",
            "attempt_id": attempt_id,
            "run_id": run_id,
            "artifact": {
                "artifact_id": "art_report_agent",
                "kind": "report.agent",
                "name": "report.agent.json",
                "media_type": "application/json",
                "schema_id": "codex-full-repo-review",
                "schema_version": "v1",
                "encoding": "utf-8",
                "compression": "none",
                "sha256": __import__("hashlib").sha256(content).hexdigest(),
                "size_bytes": len(content),
                "required": False,
            },
            "content_base64": base64.b64encode(content).decode("ascii"),
        }
        artifact = RouteHarness(
            f"/v1/review-runs/{run_id}/artifacts",
            artifact_payload,
            headers=self.auth,
        )
        app.PullwiseHandler.route(artifact, "POST")

        self.assertEqual(artifact.status, HTTPStatus.OK)
        self.assertTrue(artifact.payload["accepted"])
        self.assertEqual(artifact.payload["storage"]["url"], f"/v1/review-runs/{run_id}/artifacts/art_report_agent")
        stored_artifact = db.get_review_run_artifact(run_id, "art_report_agent")
        self.assertEqual(stored_artifact["artifact_id"], "art_report_agent")
        self.assertEqual(stored_artifact["run_id"], run_id)
        self.assertEqual(stored_artifact["kind"], "report.agent")
        self.assertEqual(stored_artifact["sha256"], __import__("hashlib").sha256(content).hexdigest())
        self.assertEqual(stored_artifact["size_bytes"], len(content))
        self.assertEqual(stored_artifact["storage_url"], f"/v1/review-runs/{run_id}/artifacts/art_report_agent")
        self.assertEqual(json.loads(stored_artifact["storage_json"])["url"], f"/v1/review-runs/{run_id}/artifacts/art_report_agent")
        self.assertEqual(json.loads(stored_artifact["inline_json"]), {})
        listed_artifacts = db.list_review_run_artifacts(claimed["job_id"], attempt_id)
        self.assertEqual(listed_artifacts[0]["artifact_id"], "art_report_agent")
        self.assertEqual(listed_artifacts[0]["run_id"], run_id)
        with db.connect() as connection:
            legacy_artifacts = connection.execute(
                "SELECT COUNT(*) FROM job_result_artifacts WHERE kind LIKE 'review_artifact:%'"
            ).fetchone()[0]
        self.assertEqual(legacy_artifacts, 0)

        def clone_artifact_payload(payload: dict) -> dict:
            return json.loads(json.dumps(payload))

        missing_protocol = clone_artifact_payload(artifact_payload)
        missing_protocol.pop("protocol_version")
        unsupported_kind = clone_artifact_payload(artifact_payload)
        unsupported_kind["artifact"]["kind"] = "unsupported_worker_artifact"
        missing_encoding = clone_artifact_payload(artifact_payload)
        missing_encoding["artifact"].pop("encoding")
        invalid_sha = clone_artifact_payload(artifact_payload)
        invalid_sha["artifact"]["sha256"] = "not-a-sha"
        for label, payload in [
            ("missing_protocol", missing_protocol),
            ("unsupported_kind", unsupported_kind),
            ("missing_encoding", missing_encoding),
            ("invalid_sha", invalid_sha),
        ]:
            with self.subTest(label=label):
                invalid_artifact = RouteHarness(f"/v1/review-runs/{run_id}/artifacts", payload, headers=self.auth)
                app.PullwiseHandler.route(invalid_artifact, "POST")
                self.assertEqual(invalid_artifact.status, HTTPStatus.BAD_REQUEST)
                self.assertIn("Invalid review-worker-protocol/v1 artifact upload", invalid_artifact.payload["message"])

        duplicate_artifact = RouteHarness(
            f"/v1/review-runs/{run_id}/artifacts",
            clone_artifact_payload(artifact_payload),
            headers=self.auth,
        )
        app.PullwiseHandler.route(duplicate_artifact, "POST")

        self.assertEqual(duplicate_artifact.status, HTTPStatus.OK)
        self.assertTrue(duplicate_artifact.payload["duplicate"])

        artifact_url = f"/v1/review-runs/{run_id}/artifacts/art_report_agent"
        download = RouteHarness(artifact_url, headers={"Cookie": "pw_session=ses_owner"})
        app.PullwiseHandler.route(download, "GET")

        self.assertEqual(download.status, HTTPStatus.OK)
        self.assertEqual(download.binary_payload, content)
        self.assertEqual(download.content_type, "application/json")
        self.assertEqual(download.headers_out["X-Pullwise-Artifact-Id"], "art_report_agent")
        self.assertEqual(download.headers_out["ETag"], f'"{__import__("hashlib").sha256(content).hexdigest()}"')
        self.assertIn("report.agent.json", download.headers_out["Content-Disposition"])

        unauthenticated_download = RouteHarness(artifact_url)
        app.PullwiseHandler.route(unauthenticated_download, "GET")
        self.assertEqual(unauthenticated_download.status, HTTPStatus.UNAUTHORIZED)

        other_user_download = RouteHarness(artifact_url, headers={"Cookie": "pw_session=ses_other"})
        app.PullwiseHandler.route(other_user_download, "GET")
        self.assertEqual(other_user_download.status, HTTPStatus.NOT_FOUND)

        result = RouteHarness(
            f"/v1/review-runs/{run_id}/result",
            {
                "status": "done",
                "attempt_id": attempt_id,
                **audit_result_fields([]),
                "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(result, "POST")

        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertTrue(result.payload["accepted"])
        self.assertEqual(result.payload["reviewRun"]["run_id"], run_id)
        self.assertEqual(result.payload["reviewRun"]["status"], "completed")
        self.assertEqual(result.payload["reviewRun"]["result_status"], "done")
        self.assertEqual(db.get_scan_job(claimed["job_id"])["status"], "done")
        finished_run = db.get_review_run(run_id)
        self.assertEqual(finished_run["status"], "completed")
        self.assertEqual(finished_run["engine_type"], "codex_app_server")
        self.assertEqual(json.loads(finished_run["summary_json"])["top_findings"], [])
        self.assertEqual(json.loads(finished_run["quality_gate_json"])["status"], "pass")
        self.assertEqual(json.loads(finished_run["raw_result_envelope_json"])["message_type"], "review_run_result")

        scan_detail = RouteHarness(f"/scans/{scan['id']}", headers={"Cookie": "pw_session=ses_owner"})
        app.PullwiseHandler.route(scan_detail, "GET")

        self.assertEqual(scan_detail.status, HTTPStatus.OK)
        review_run_payload = scan_detail.payload["reviewRun"]
        self.assertEqual(review_run_payload["runId"], run_id)
        self.assertEqual(review_run_payload["jobId"], claimed["job_id"])
        self.assertEqual(review_run_payload["status"], "completed")
        self.assertEqual(review_run_payload["resultStatus"], "done")
        self.assertEqual(review_run_payload["summary"]["top_findings"], [])
        self.assertEqual(review_run_payload["qualityGate"]["status"], "pass")
        self.assertEqual(review_run_payload["artifactCount"], 5)
        artifacts_by_id = {item["artifactId"]: item for item in review_run_payload["artifacts"]}
        self.assertEqual(artifacts_by_id["art_report_agent"]["storage"]["url"], artifact_url)
        self.assertNotIn("content_base64", json.dumps(artifacts_by_id["art_report_agent"]))

    def test_terminal_final_log_artifact_upload_replaces_existing_log_only(self) -> None:
        self.create_claimable_scan_job(job_id="job_terminal_log_replace", scan_id="sc_terminal_log_replace", user_id="usr_1")
        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        claimed = claim.payload["job"]
        run_id = claimed["run_id"]
        attempt_id = f"wk_1-{claimed['attempt']}"

        old_content = b"old progress\n"
        log_item = protocol_artifact_item(
            run_id,
            "art_progress_log",
            "progress_log",
            "progress.log.jsonl",
            "application/jsonl",
            "progress-log",
            required=False,
        )
        log_item["sha256"] = hashlib.sha256(old_content).hexdigest()
        log_item["size_bytes"] = len(old_content)
        initial_log = RouteHarness(
            f"/v1/review-runs/{run_id}/artifacts",
            {
                "protocol_version": "review-worker-protocol/v1",
                "attempt_id": attempt_id,
                "run_id": run_id,
                "artifact": dict(log_item),
                "content_base64": base64.b64encode(old_content).decode("ascii"),
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(initial_log, "POST")
        self.assertEqual(initial_log.status, HTTPStatus.OK)
        self.assertTrue(initial_log.payload["accepted"])

        result = self.v1_result(
            claimed,
            {
                "status": "done",
                "attempt_id": attempt_id,
                **audit_result_fields([]),
                "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            },
        )
        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertEqual(db.get_scan_job(claimed["job_id"])["status"], "done")

        report_content = b"late report"
        report_item = protocol_artifact_item(run_id, "art_late_report", "report.human", "late-report.md", "text/markdown", "human-markdown-report")
        report_item["sha256"] = hashlib.sha256(report_content).hexdigest()
        report_item["size_bytes"] = len(report_content)
        late_report = RouteHarness(
            f"/v1/review-runs/{run_id}/artifacts",
            {
                "protocol_version": "review-worker-protocol/v1",
                "attempt_id": attempt_id,
                "run_id": run_id,
                "artifact": report_item,
                "content_base64": base64.b64encode(report_content).decode("ascii"),
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(late_report, "POST")
        self.assertEqual(late_report.status, HTTPStatus.CONFLICT)

        new_content = b"new progress with run_completed\n"
        log_item["sha256"] = hashlib.sha256(new_content).hexdigest()
        log_item["size_bytes"] = len(new_content)
        final_log = RouteHarness(
            f"/v1/review-runs/{run_id}/artifacts",
            {
                "protocol_version": "review-worker-protocol/v1",
                "attempt_id": attempt_id,
                "run_id": run_id,
                "artifact": dict(log_item),
                "content_base64": base64.b64encode(new_content).decode("ascii"),
                "final_log_upload": True,
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(final_log, "POST")
        self.assertEqual(final_log.status, HTTPStatus.OK)
        self.assertTrue(final_log.payload["replaced"])
        stored = db.get_review_run_artifact(run_id, "art_progress_log")
        self.assertEqual(stored["sha256"], hashlib.sha256(new_content).hexdigest())
    def test_terminal_final_debug_bundle_upload_replaces_existing_bundle(self) -> None:
        self.create_claimable_scan_job(job_id="job_terminal_debug_replace", scan_id="sc_terminal_debug_replace", user_id="usr_1")
        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        claimed = claim.payload["job"]
        run_id = claimed["run_id"]
        attempt_id = f"wk_1-{claimed['attempt']}"

        old_content = b"old debug bundle"
        debug_item = protocol_artifact_item(
            run_id,
            "art_debug_bundle",
            "debug_bundle",
            "debug-bundle.zip",
            "application/zip",
            "pullwise-debug-bundle",
            required=False,
        )
        debug_item["sha256"] = hashlib.sha256(old_content).hexdigest()
        debug_item["size_bytes"] = len(old_content)
        initial_debug = RouteHarness(
            f"/v1/review-runs/{run_id}/artifacts",
            {
                "protocol_version": "review-worker-protocol/v1",
                "attempt_id": attempt_id,
                "run_id": run_id,
                "artifact": dict(debug_item),
                "content_base64": base64.b64encode(old_content).decode("ascii"),
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(initial_debug, "POST")
        self.assertEqual(initial_debug.status, HTTPStatus.OK)

        result = self.v1_result(
            claimed,
            {
                "status": "done",
                "attempt_id": attempt_id,
                **audit_result_fields([]),
                "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            },
        )
        self.assertEqual(result.status, HTTPStatus.OK)

        new_content = b"final debug bundle"
        debug_item["sha256"] = hashlib.sha256(new_content).hexdigest()
        debug_item["size_bytes"] = len(new_content)
        final_debug = RouteHarness(
            f"/v1/review-runs/{run_id}/artifacts",
            {
                "protocol_version": "review-worker-protocol/v1",
                "attempt_id": attempt_id,
                "run_id": run_id,
                "artifact": dict(debug_item),
                "content_base64": base64.b64encode(new_content).decode("ascii"),
                "final_log_upload": True,
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(final_debug, "POST")

        self.assertEqual(final_debug.status, HTTPStatus.OK)
        self.assertTrue(final_debug.payload["replaced"])
        stored = db.get_review_run_artifact(run_id, "art_debug_bundle")
        self.assertEqual(stored["sha256"], hashlib.sha256(new_content).hexdigest())

    def test_terminal_review_run_status_is_not_regressed_by_late_progress(self) -> None:
        self.create_claimable_scan_job(job_id="job_terminal_progress_regress", scan_id="sc_terminal_progress_regress", user_id="usr_1")
        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        claimed = claim.payload["job"]
        run_id = claimed["run_id"]
        attempt_id = f"wk_1-{claimed['attempt']}"

        result = self.v1_result(
            claimed,
            {
                "status": "failed",
                "attempt_id": attempt_id,
                "error": "intent-test-source.json generated_tests[0].path does not exist",
                **audit_result_fields([], execution_status="failed"),
            },
        )
        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertEqual(db.get_review_run(run_id)["status"], "failed")

        db.update_review_run_progress(
            {
                "run_id": run_id,
                "job_id": claimed["job_id"],
                "worker_id": "wk_1",
                "sequence": 99,
                "event_type": "progress_updated",
                "phase": "bootstrap_helper_scripts",
                "severity": "info",
                "status": "running",
                "progress": 17,
                "timestamp": "2026-07-07T06:33:10Z",
                "created_at": app.now() + 1,
            }
        )

        stored_run = db.get_review_run(run_id)
        self.assertEqual(stored_run["status"], "failed")
        self.assertEqual(stored_run["result_status"], "failed")

    def test_terminal_run_event_after_result_is_accepted_without_regressing_job(self) -> None:
        self.create_claimable_scan_job(job_id="job_terminal_event_after_result", scan_id="sc_terminal_event_after_result", user_id="usr_1")
        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        claimed = claim.payload["job"]
        run_id = claimed["run_id"]
        attempt_id = f"wk_1-{claimed['attempt']}"

        result = self.v1_result(
            claimed,
            {
                "status": "done",
                "attempt_id": attempt_id,
                **audit_result_fields([]),
            },
        )
        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertEqual(db.get_scan_job(claimed["job_id"])["status"], "done")

        terminal_event = RouteHarness(
            f"/v1/review-runs/{run_id}/events",
            {
                "protocol_version": "review-worker-protocol/v1",
                "run_id": run_id,
                "worker_id": "wk_1",
                "sequence": 1,
                "timestamp": "2026-07-07T06:34:10Z",
                "event_type": "run_completed",
                "phase": "submit_result_envelope",
                "severity": "info",
                "message": "Run completed.",
                "progress": {"overall_percent": 100.0, "current_phase_percent": 100, "status": "completed"},
                "data": {},
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(terminal_event, "POST")

        self.assertEqual(terminal_event.status, HTTPStatus.OK)
        self.assertEqual(db.get_scan_job(claimed["job_id"])["status"], "done")
        stored_run = db.get_review_run(run_id)
        self.assertEqual(stored_run["status"], "completed")
        self.assertEqual(stored_run["result_status"], "done")
        stored_events = db.list_review_run_events(run_id)
        self.assertEqual(stored_events[-1]["event_type"], "run_completed")

    def test_v1_worker_result_accepts_cancelled_terminal_status(self) -> None:
        scan = {
            "id": "sc_v1_cancelled_result",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc1234",
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)

        lease = RouteHarness("/v1/workers/wk_1/lease", v1_worker_lease_payload(), headers=self.auth)
        app.PullwiseHandler.route(lease, "POST")
        self.assertEqual(lease.status, HTTPStatus.OK)
        claimed = lease.payload["job"]
        run_id = lease.payload["lease"]["run_id"]
        attempt_id = f"wk_1-{claimed['attempt']}"

        result = RouteHarness(
            f"/v1/review-runs/{run_id}/result",
            {
                "status": "cancelled",
                "attempt_id": attempt_id,
                "error": "cancel requested: user_requested",
                **audit_result_fields([], execution_status="cancelled"),
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(result, "POST")

        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertTrue(result.payload["accepted"])
        self.assertEqual(result.payload["reviewRun"]["status"], "cancelled")
        self.assertEqual(result.payload["reviewRun"]["result_status"], "cancelled")
        self.assertEqual(db.get_scan_job(claimed["job_id"])["status"], "cancelled")
        self.assertEqual(app.SCANS[0]["status"], "cancelled")
        with closing(db.connect()) as connection:
            stored_result = connection.execute("SELECT status FROM job_results WHERE job_id = ?", (claimed["job_id"],)).fetchone()
        self.assertEqual(stored_result[0], "cancelled")

    def test_v1_worker_result_accepts_partial_completed_terminal_status(self) -> None:
        scan = {
            "id": "sc_v1_partial_result",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc1234",
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 100,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.USERS = {"usr_1": {"id": "usr_1", "name": "Owner", "providers": []}}
        app.SESSIONS = {"ses_owner": {"id": "ses_owner", "userId": "usr_1", "createdAt": app.now(), "expiresAt": app.now() + 3600}}
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)

        lease = RouteHarness("/v1/workers/wk_1/lease", v1_worker_lease_payload(), headers=self.auth)
        app.PullwiseHandler.route(lease, "POST")
        self.assertEqual(lease.status, HTTPStatus.OK)
        claimed = lease.payload["job"]
        run_id = lease.payload["lease"]["run_id"]
        attempt_id = f"wk_1-{claimed['attempt']}"

        result = RouteHarness(
            f"/v1/review-runs/{run_id}/result",
            {
                "status": "partial_completed",
                "attempt_id": attempt_id,
                "error": "partial result after qa repair failed",
                **audit_result_fields([], execution_status="partial_completed"),
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(result, "POST")

        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertTrue(result.payload["accepted"])
        self.assertEqual(result.payload["reviewRun"]["status"], "partial_completed")
        self.assertEqual(result.payload["reviewRun"]["result_status"], "partial_completed")
        self.assertEqual(db.get_scan_job(claimed["job_id"])["status"], "partial_completed")
        self.assertEqual(app.SCANS[0]["status"], "partial_completed")
        scan_detail = RouteHarness(f"/scans/{scan['id']}", headers={"Cookie": "pw_session=ses_owner"})
        app.PullwiseHandler.route(scan_detail, "GET")
        self.assertEqual(scan_detail.status, HTTPStatus.OK)
        self.assertEqual(scan_detail.payload["status"], "partial_completed")
        self.assertLess(scan_detail.payload["progress"], 100)
        self.assertEqual(scan_detail.payload["reviewRun"]["status"], "partial_completed")
        with closing(db.connect()) as connection:
            stored_result = connection.execute("SELECT status FROM job_results WHERE job_id = ?", (claimed["job_id"],)).fetchone()
        self.assertEqual(stored_result[0], "partial_completed")

    def test_v1_worker_register_rejects_mismatch_prefetch_and_non_linux(self) -> None:
        mismatched = RouteHarness(
            "/v1/workers/register",
            {
                "protocol_version": "review-worker-protocol/v1",
                "worker": {
                    "worker_id": "wk_other",
                    "concurrency": {"max_active_jobs": 1, "maintains_local_queue": False, "prefetch_jobs": False},
                },
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(mismatched, "POST")
        self.assertEqual(mismatched.status, HTTPStatus.FORBIDDEN)

        prefetch = RouteHarness(
            "/v1/workers/register",
            {
                "protocol_version": "review-worker-protocol/v1",
                "worker": {
                    "worker_id": "wk_1",
                    "concurrency": {"max_active_jobs": 1, "maintains_local_queue": False, "prefetch_jobs": True},
                },
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(prefetch, "POST")
        self.assertEqual(prefetch.status, HTTPStatus.BAD_REQUEST)

        non_linux = RouteHarness(
            "/v1/workers/register",
            {
                "protocol_version": "review-worker-protocol/v1",
                "worker": {
                    "worker_id": "wk_1",
                    "concurrency": {"max_active_jobs": 1, "maintains_local_queue": False, "prefetch_jobs": False},
                    "platform": {"os": "windows", "arch": "x86_64"},
                },
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(non_linux, "POST")
        self.assertEqual(non_linux.status, HTTPStatus.BAD_REQUEST)

    def test_v1_worker_heartbeat_rejects_malformed_fixed_protocol_shape(self) -> None:
        def clone(payload: dict) -> dict:
            return json.loads(json.dumps(payload))

        missing_concurrency = clone(v1_worker_heartbeat_payload())
        missing_concurrency.pop("concurrency")

        idle_with_active_run = clone(v1_worker_heartbeat_payload())
        idle_with_active_run["active_run_id"] = "run_busy"

        busy_with_available_slot = clone(v1_worker_heartbeat_payload(status="busy", run_id="run_busy"))
        busy_with_available_slot["concurrency"]["available_job_slots"] = 1

        busy_without_sequence = clone(v1_worker_heartbeat_payload(status="busy", run_id="run_busy"))
        busy_without_sequence["progress"].pop("last_event_sequence")

        busy_without_counters = clone(v1_worker_heartbeat_payload(status="busy", run_id="run_busy"))
        busy_without_counters["progress"].pop("counters")

        busy_with_bad_counter = clone(v1_worker_heartbeat_payload(status="busy", run_id="run_busy"))
        busy_with_bad_counter["progress"]["counters"]["reviewer_runs_total"] = -1

        busy_without_active_unit = clone(v1_worker_heartbeat_payload(status="busy", run_id="run_busy"))
        busy_without_active_unit["progress"].pop("active_unit")

        busy_progress_mismatch = clone(v1_worker_heartbeat_payload(status="busy", run_id="run_busy"))
        busy_progress_mismatch["progress"]["run_id"] = "run_other"

        legacy_running_jobs = clone(v1_worker_heartbeat_payload())
        legacy_running_jobs["running_jobs"] = 0

        legacy_active_job_ids = clone(v1_worker_heartbeat_payload(status="busy", run_id="run_busy"))
        legacy_active_job_ids["active_job_ids"] = ["job_1"]

        cases = [
            ("missing_concurrency", missing_concurrency),
            ("idle_with_active_run", idle_with_active_run),
            ("busy_with_available_slot", busy_with_available_slot),
            ("busy_without_sequence", busy_without_sequence),
            ("busy_without_counters", busy_without_counters),
            ("busy_with_bad_counter", busy_with_bad_counter),
            ("busy_without_active_unit", busy_without_active_unit),
            ("busy_progress_mismatch", busy_progress_mismatch),
            ("legacy_running_jobs", legacy_running_jobs),
            ("legacy_active_job_ids", legacy_active_job_ids),
        ]
        for label, payload in cases:
            with self.subTest(label=label):
                heartbeat = RouteHarness("/v1/workers/wk_1/heartbeat", payload, headers=self.auth)
                app.PullwiseHandler.route(heartbeat, "POST")
                self.assertEqual(heartbeat.status, HTTPStatus.BAD_REQUEST)
                self.assertIn("Invalid review-worker-protocol/v1 heartbeat", heartbeat.payload["message"])

    def test_v1_worker_heartbeat_rejects_unknown_active_run_id(self) -> None:
        heartbeat = RouteHarness(
            "/v1/workers/wk_1/heartbeat",
            v1_worker_heartbeat_payload(status="busy", run_id="run_unknown"),
            headers=self.auth,
        )

        app.PullwiseHandler.route(heartbeat, "POST")

        self.assertEqual(heartbeat.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("active_run_id", heartbeat.payload["message"])
    def test_v1_worker_lease_rejects_non_idle_or_incomplete_capacity(self) -> None:
        def clone(payload: dict) -> dict:
            return json.loads(json.dumps(payload))

        missing_capacity = clone(v1_worker_lease_payload())
        missing_capacity.pop("capacity")

        busy_capacity = clone(v1_worker_lease_payload())
        busy_capacity["capacity"]["active_jobs"] = 1
        busy_capacity["capacity"]["available_job_slots"] = 0

        local_queue = clone(v1_worker_lease_payload())
        local_queue["capacity"]["maintains_local_queue"] = True
        local_queue["capacity"]["local_queue_depth"] = 1

        missing_capability = clone(v1_worker_lease_payload())
        missing_capability["capabilities"].pop("codex_app_server")

        cases = [
            ("missing_capacity", missing_capacity),
            ("busy_capacity", busy_capacity),
            ("local_queue", local_queue),
            ("missing_capability", missing_capability),
        ]
        for label, payload in cases:
            with self.subTest(label=label):
                lease = RouteHarness("/v1/workers/wk_1/lease", payload, headers=self.auth)
                app.PullwiseHandler.route(lease, "POST")
                self.assertEqual(lease.status, HTTPStatus.BAD_REQUEST)
                self.assertIn("Invalid review-worker-protocol/v1 lease request", lease.payload["message"])

    def test_v1_worker_lease_with_unavailable_intent_validation_gets_no_job(self) -> None:
        payload = v1_worker_lease_payload()
        payload["capabilities"]["intent_test_validation"] = False
        lease = RouteHarness("/v1/workers/wk_1/lease", payload, headers=self.auth)
        app.PullwiseHandler.route(lease, "POST")

        self.assertEqual(lease.status, HTTPStatus.OK)
        self.assertIsNone(lease.payload["job"])
        self.assertEqual(lease.payload["reason"], "intent_test_validation_unavailable")
    def test_v1_worker_lease_blocks_idle_worker_when_codex_quota_is_not_ready(self) -> None:
        user = {"id": "usr_quota_blocked", "name": "Owner", "providers": []}
        app.USERS = {user["id"]: user}
        scan = {
            "id": "sc_quota_blocked",
            "repo": "acme/quota-blocked",
            "branch": "main",
            "commit": "abc1234",
            "status": "queued",
            "userId": user["id"],
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS = [scan]
        job = app.create_scan_job_for_scan(scan)
        heartbeat_payload = v1_worker_heartbeat_payload(status="idle")
        heartbeat_payload["codex_ready"] = False
        heartbeat_payload["ready_providers"] = []
        heartbeat_payload["codex_app_server"]["status"] = "needs_attention"
        heartbeat_payload["codex_quota"] = {
            "provider": "codex",
            "status": "exhausted",
            "ready": False,
            "reason": "codex_quota_exhausted",
            "remainingPercent": 0,
            "windows": [
                {"windowKind": "five_hour", "label": "5 hour", "usedPercent": 100, "remainingPercent": 0, "windowDurationMins": 300},
                {"windowKind": "weekly", "label": "weekly", "usedPercent": 50, "remainingPercent": 50, "windowDurationMins": 10080},
            ],
        }

        heartbeat = RouteHarness("/v1/workers/wk_1/heartbeat", heartbeat_payload, headers=self.auth)
        app.PullwiseHandler.route(heartbeat, "POST")
        lease = RouteHarness("/v1/workers/wk_1/lease", v1_worker_lease_payload(), headers=self.auth)
        app.PullwiseHandler.route(lease, "POST")

        self.assertEqual(heartbeat.status, HTTPStatus.OK)
        self.assertEqual(lease.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertIn("degraded", lease.payload["message"])
        self.assertIsNone(db.get_scan_job(job["job_id"]).get("claimed_by_worker_id"))
        stored_worker = db.get_worker("wk_1")
        self.assertEqual(stored_worker["codex_ready"], 0)
        self.assertEqual(app.worker_record_ready_providers(stored_worker), [])
        stored_quota = json.loads(stored_worker["codex_quota"])
        self.assertEqual([window["windowKind"] for window in stored_quota["windows"]], ["five_hour", "weekly"])

    def create_registry_worker(self, worker_id: str) -> tuple[dict, str]:
        worker = db.create_worker({"worker_id": worker_id, "name": worker_id, "provider": "codex"})
        db.upsert_worker_heartbeat(
            {
                "worker_id": worker_id,
                "version": "0.1.0",
                "provider": "codex",
                "provider_chain": ["codex"],
                "max_concurrent_jobs": 2,
                "running_jobs": 0,
                "free_slots": 2,
                "doctor_status": "ok",
                "codex_ready": 1,
                "ready_providers": ["codex"],
                "timestamp": app.now(),
            }
        )
        return worker, worker["worker_token"]

    def create_claimable_scan_job(
        self,
        *,
        job_id: str,
        scan_id: str,
        user_id: str,
    ) -> dict:
        return db.create_scan_job(
            {
                "job_id": job_id,
                "scan_id": scan_id,
                "repo": f"acme/{scan_id}",
                "branch": "main",
                "commit": "abc1234",
                "status": "queued",
                "created_at": app.now(),
                "user_id": user_id,
                "max_attempts": 2,
            }
        )

    def v1_lease(
        self,
        worker_id: str = "wk_1",
        *,
        headers: dict | None = None,
        payload: dict | None = None,
    ) -> RouteHarness:
        lease_payload = v1_worker_lease_payload()
        lease_payload["worker_id"] = worker_id
        if payload:
            lease_payload.update(payload)
        handler = RouteHarness(f"/v1/workers/{worker_id}/lease", lease_payload, headers=headers or self.auth)
        app.PullwiseHandler.route(handler, "POST")
        return handler

    def v1_heartbeat(
        self,
        worker_id: str = "wk_1",
        *,
        status: str = "idle",
        run_id: str | None = None,
        headers: dict | None = None,
        payload: dict | None = None,
    ) -> RouteHarness:
        heartbeat_payload = v1_worker_heartbeat_payload(status=status, run_id=run_id)
        heartbeat_payload["worker_id"] = worker_id
        if payload:
            heartbeat_payload.update(payload)
        handler = RouteHarness(f"/v1/workers/{worker_id}/heartbeat", heartbeat_payload, headers=headers or self.auth)
        app.PullwiseHandler.route(handler, "POST")
        return handler

    def v1_result(
        self,
        job: dict,
        payload: dict,
        *,
        headers: dict | None = None,
        raw_body: bytes | None = None,
        harness: type[RouteHarness] = RouteHarness,
    ) -> RouteHarness:
        run_id = str(job.get("run_id") or f"run_{job.get('job_id')}")
        handler = harness(
            f"/v1/review-runs/{run_id}/result",
            payload,
            headers=headers or self.auth,
            raw_body=raw_body,
        )
        app.PullwiseHandler.route(handler, "POST")
        return handler

    def v1_event(
        self,
        job: dict,
        *,
        phase: str = "repo_map",
        progress: float = 55,
        message: str = "Repository map: classifying source files",
        event_type: str = "progress_updated",
        sequence: int = 1,
        worker_id: str = "wk_1",
        headers: dict | None = None,
        data: dict | None = None,
        progress_steps: list[dict] | None = None,
    ) -> RouteHarness:
        run_id = str(job.get("run_id") or f"run_{job.get('job_id')}")
        payload = {
            "protocol_version": "review-worker-protocol/v1",
            "run_id": run_id,
            "worker_id": worker_id,
            "sequence": sequence,
            "timestamp": "2026-07-01T10:22:00Z",
            "event_type": event_type,
            "phase": phase,
            "severity": "info",
            "message": message,
            "progress": {
                "overall_percent": progress,
                "current_phase_percent": progress,
                "status": "running",
            },
        }
        if progress_steps is not None:
            payload["progress"]["steps"] = progress_steps
        if data is not None:
            payload["data"] = data
        handler = RouteHarness(f"/v1/review-runs/{run_id}/events", payload, headers=headers or self.auth)
        app.PullwiseHandler.route(handler, "POST")
        return handler

    def test_worker_auth_rejection_is_logged_without_token_value(self) -> None:
        claim = RouteHarness(
            "/v1/workers/wk_1/lease",
            v1_worker_lease_payload(),
            headers={"Authorization": "Bearer invalid-worker-token"},
        )
        with self.assertLogs(app.logger, level="WARNING") as logs:
            app.PullwiseHandler.route(claim, "POST")

        self.assertEqual(claim.status, HTTPStatus.UNAUTHORIZED)
        output = "\n".join(logs.output)
        self.assertIn("Rejected worker request path=/v1/workers/wk_1/lease", output)
        self.assertIn("bearer_present=True", output)
        self.assertNotIn("invalid-worker-token", output)

    def test_disabled_or_deleted_worker_token_cannot_access_control_plane_without_active_command(self) -> None:
        worker, token = self.create_registry_worker("wk_disabled_control_plane")
        db.set_worker_enabled(worker["worker_id"], False)
        headers = {"Authorization": f"Bearer {token}"}

        requests = [
            RouteHarness(
                f"/v1/workers/{worker['worker_id']}/heartbeat",
                v1_worker_heartbeat_payload(),
                headers=headers,
            ),
            RouteHarness(
                f"/v1/workers/{worker['worker_id']}/agent-configs",
                {"worker_id": worker["worker_id"]},
                headers=headers,
            ),
            RouteHarness("/worker/commands/poll", {"worker_id": worker["worker_id"]}, headers=headers),
            RouteHarness(
                "/worker/log-streams/log_disabled_control_plane/lines",
                {"worker_id": worker["worker_id"], "lines": []},
                headers=headers,
            ),
        ]

        for request in requests:
            app.PullwiseHandler.route(request, "POST")
            self.assertEqual(request.status, HTTPStatus.UNAUTHORIZED)

        deleted_worker, deleted_token = self.create_registry_worker("wk_deleted_control_plane")
        db.soft_delete_worker(deleted_worker["worker_id"])
        deleted_poll = RouteHarness(
            "/worker/commands/poll",
            {"worker_id": deleted_worker["worker_id"]},
            headers={"Authorization": f"Bearer {deleted_token}"},
        )
        app.PullwiseHandler.route(deleted_poll, "POST")
        self.assertEqual(deleted_poll.status, HTTPStatus.UNAUTHORIZED)

    def test_worker_result_route_accepts_gzip_json_body(self) -> None:
        scan = {
            "id": "sc_gzip_result",
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
        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]
        result_body = {
            "status": "done",
            "attempt_id": "wk_1-1",
            "result_checksum": "checksum-gzip-result",
            **audit_result_fields([]),
            "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        raw_body = gzip.compress(json.dumps(result_body).encode("utf-8"))
        result = self.v1_result(
            job,
            result_body,
            harness=RawBodyRouteHarness,
            raw_body=raw_body,
            headers={**self.auth, "Content-Encoding": "gzip", "Content-Length": str(len(raw_body))},
        )

        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertTrue(result.payload["accepted"])
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "done")

    def test_worker_artifact_route_allows_authenticated_gzip_body_over_public_limit(self) -> None:
        scan = {
            "id": "sc_gzip_artifact",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc1234",
            "status": "queued",
            "userId": "usr_1",
            "repoId": "repo_1",
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)
        claim = self.v1_lease()
        job = claim.payload["job"]
        run_id = job["run_id"]
        content = b"{\"status\":\"pass\"}\n"
        artifact_payload = {
            "protocol_version": "review-worker-protocol/v1",
            "attempt_id": "wk_1-1",
            "run_id": run_id,
            "artifact": {
                "artifact_id": "art_qa",
                "kind": "qa",
                "name": "qa.json",
                "media_type": "application/json",
                "schema_id": "qa-gate",
                "schema_version": "v1",
                "encoding": "utf-8",
                "compression": "none",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "required": True,
            },
            "content_base64": base64.b64encode(content).decode("ascii"),
        }
        raw_body = gzip.compress(json.dumps(artifact_payload).encode("utf-8"))

        with patch.dict(
            app.os.environ,
            {"PULLWISE_MAX_BODY_BYTES": "1", "PULLWISE_MAX_DECOMPRESSED_BODY_BYTES": "65536"},
            clear=False,
        ):
            artifact = RawBodyRouteHarness(
                f"/v1/review-runs/{run_id}/artifacts",
                artifact_payload,
                headers={**self.auth, "Content-Encoding": "gzip", "Content-Length": str(len(raw_body))},
                raw_body=raw_body,
            )
            app.PullwiseHandler.route(artifact, "POST")

        self.assertEqual(artifact.status, HTTPStatus.OK)
        self.assertTrue(artifact.payload["accepted"])
        stored_artifact = db.get_review_run_artifact(run_id, "art_qa")
        self.assertEqual(stored_artifact["sha256"], hashlib.sha256(content).hexdigest())

    def test_decode_json_body_rejects_oversized_gzip_json(self) -> None:
        raw_body = gzip.compress(json.dumps({"payload": "x" * 256}).encode("utf-8"))

        with patch.dict(
            app.os.environ,
            {"PULLWISE_MAX_BODY_BYTES": "32", "PULLWISE_MAX_DECOMPRESSED_BODY_BYTES": "32"},
            clear=False,
        ):
            with self.assertRaises(app.RequestBodyTooLarge):
                app.decode_json_body(raw_body, "gzip")

    def test_scan_and_issue_reads_use_database_pages_when_indexed(self) -> None:
        app.USERS = {"usr_1": {"id": "usr_1", "name": "Owner", "providers": []}}
        app.SESSIONS = {
            "ses_owner": {
                "id": "ses_owner",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        scans = [
            {
                "id": f"sc_{index}",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "done",
                "userId": "usr_1",
                "createdAt": 300 - index,
                "queuedAt": 300 - index,
                "progress": 100,
                "phase": "report",
                "issues": {"critical": 0, "high": index, "medium": 0, "low": 0, "info": 0},
            }
            for index in range(3)
        ]
        app.SCANS = scans
        for scan in scans:
            app.create_scan_job_for_scan(scan)
        class ExplodingScans(list):
            def __iter__(self):
                raise AssertionError("scan route should not iterate global SCANS when snapshots exist")

        app.SCANS = ExplodingScans()
        db.upsert_issue(
            {
                "id": "iss_db",
                "userId": "usr_1",
                "scanId": "sc_1",
                "jobId": scans[1]["jobId"],
                "repo": "acme/api",
                "status": "open",
                "severity": "high",
                "title": "Indexed issue",
                "createdAt": 200,
            }
        )

        with (
            patch.object(app, "cleanup_server_resources_if_due", return_value={}),
            patch.object(app, "user_scans_for_read", side_effect=AssertionError("scan route should page in DB")),
            patch.object(
                app.db,
                "list_completed_scan_job_results_for_job_ids",
                side_effect=AssertionError("scan route should not read full result artifacts when snapshots exist"),
            ),
        ):
            scans_route = RouteHarness("/scans?limit=1&offset=1", headers={"Cookie": "pw_session=ses_owner"})
            app.PullwiseHandler.route(scans_route, "GET")
        with (
            patch.object(app, "cleanup_server_resources_if_due", return_value={}),
            patch.object(app, "user_issues", side_effect=AssertionError("issue route should page in DB")),
        ):
            issues_route = RouteHarness("/issues?status=open&severity=high&limit=1", headers={"Cookie": "pw_session=ses_owner"})
            app.PullwiseHandler.route(issues_route, "GET")

        self.assertEqual(scans_route.status, HTTPStatus.OK)
        self.assertEqual(scans_route.payload["total"], 3)
        self.assertEqual([scan["id"] for scan in scans_route.payload["items"]], ["sc_1"])
        self.assertEqual(issues_route.status, HTTPStatus.OK)
        self.assertEqual(issues_route.payload["total"], 1)
        self.assertEqual(issues_route.payload["items"][0]["id"], "iss_db")

    def test_batch_scan_and_issue_status_routes(self) -> None:
        app.USERS = {"usr_1": {"id": "usr_1", "name": "Owner", "providers": []}}
        app.SESSIONS = {
            "ses_owner": {
                "id": "ses_owner",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        scan = {
            "id": "sc_batch_status",
            "repo": "acme/api",
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": "usr_1",
            "createdAt": 100,
            "queuedAt": 100,
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)
        db.upsert_issue(
            {
                "id": "iss_batch_status",
                "userId": "usr_1",
                "scanId": scan["id"],
                "jobId": scan["jobId"],
                "repo": "acme/api",
                "status": "open",
                "severity": "high",
                "title": "Batch issue",
                "createdAt": 101,
            }
        )

        scans_route = RouteHarness(
            "/scans/status",
            {"ids": ["sc_batch_status", "missing"]},
            headers={"Cookie": "pw_session=ses_owner"},
        )
        app.PullwiseHandler.route(scans_route, "POST")
        issues_route = RouteHarness(
            "/issues/status",
            {"updates": [{"id": "iss_batch_status", "status": "fixed"}]},
            headers={"Cookie": "pw_session=ses_owner"},
        )
        app.PullwiseHandler.route(issues_route, "PATCH")

        self.assertEqual(scans_route.status, HTTPStatus.OK)
        self.assertEqual([item["id"] for item in scans_route.payload["items"]], ["sc_batch_status"])
        self.assertEqual(issues_route.status, HTTPStatus.OK)
        self.assertEqual(issues_route.payload["items"][0]["id"], "iss_batch_status")
        self.assertEqual(issues_route.payload["items"][0]["status"], "fixed")
        self.assertEqual(issues_route.payload["errors"], [])

    def test_issue_status_update_survives_scan_filtered_history_navigation(self) -> None:
        app.USERS = {"usr_1": {"id": "usr_1", "name": "Owner", "providers": []}}
        app.SESSIONS = {
            "ses_owner": {
                "id": "ses_owner",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        scan = {
            "id": "sc_history_status",
            "repo": "acme/api",
            "branch": "main",
            "commit": "pending",
            "status": "done",
            "userId": "usr_1",
            "createdAt": 100,
            "completedAt": 120,
            "progress": 100,
            "phase": "report",
            "issues": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)
        issue = {
            "id": "iss_history_status",
            "userId": "usr_1",
            "scanId": scan["id"],
            "jobId": scan["jobId"],
            "repo": "acme/api",
            "status": "open",
            "severity": "high",
            "category": "Security",
            "title": "Validate redirects",
            "file": "src/auth.py",
            "line": 42,
            "createdAt": 101,
        }
        app.ISSUES = [dict(issue)]
        db.upsert_issue(dict(issue))

        update_route = RouteHarness(
            "/issues/iss_history_status/status",
            {
                "status": "fixed",
                "scanId": scan["id"],
                "jobId": scan["jobId"],
                "repo": "acme/api",
                "file": "src/auth.py",
                "line": 42,
                "title": "Validate redirects",
                "createdAt": 101,
            },
            headers={"Cookie": "pw_session=ses_owner"},
        )
        app.PullwiseHandler.route(update_route, "PATCH")

        self.assertEqual(update_route.status, HTTPStatus.OK)
        self.assertEqual(update_route.payload["status"], "fixed")
        self.assertEqual(app.ISSUES[0]["status"], "fixed")

        db.delete_issues_for_scan(scan["id"], user_id="usr_1", job_id=scan["jobId"])
        issues_route = RouteHarness(
            "/issues?scanId=sc_history_status&status=all",
            headers={"Cookie": "pw_session=ses_owner"},
        )
        app.PullwiseHandler.route(issues_route, "GET")

        self.assertEqual(issues_route.status, HTTPStatus.OK)
        self.assertEqual(issues_route.payload["total"], 1)
        self.assertEqual(issues_route.payload["items"][0]["id"], "iss_history_status")
        self.assertEqual(issues_route.payload["items"][0]["status"], "fixed")

    def audit_bundle_cache_fixture(self, *, issue_title: str = "Cached issue") -> dict:
        timestamp = app.now()
        app.USERS = {"usr_1": {"id": "usr_1", "name": "Owner", "providers": []}}
        app.SESSIONS = {
            "ses_owner": {
                "id": "ses_owner",
                "userId": "usr_1",
                "createdAt": timestamp,
                "expiresAt": timestamp + 3600,
            }
        }
        scan = {
            "id": "sc_cache",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc1234",
            "status": "done",
            "userId": "usr_1",
            "createdAt": timestamp,
            "completedAt": timestamp,
            "issues": {"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 0},
        }
        app.SCANS = [scan]
        app.ISSUES = [
            {
                "id": "f_cache",
                "scanId": "sc_cache",
                "userId": "usr_1",
                "repo": "acme/api",
                "branch": "main",
                "commit": "abc1234",
                "severity": "medium",
                "category": "Quality",
                "title": issue_title,
                "file": "src/app.py",
                "line": 12,
                "verificationStatus": "static_proof",
            }
        ]
        return scan

    def test_scan_job_payload_uses_repository_scan_context(self) -> None:
        scan = {
            "id": "sc_changes",
            "repo": "acme/api",
            "branch": "feature/impact",
            "commit": "abc123",
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
        }
        app.SCANS = [scan]
        job = app.create_scan_job_for_scan(scan)

        payload = app.scan_job_payload(job)
        scan_public = app.scan_payload(scan)

        self.assertEqual(payload["repo"], "acme/api")
        self.assertEqual(payload["commit"], "abc123")
        self.assertEqual(scan_public["repo"], "acme/api")
        self.assertEqual(scan_public["commit"], "abc123")
        agent_config = payload["agentConfig"]
        codex_config = agent_config["codex"]
        review_worker_config = agent_config["reviewWorker"]
        repository_limits = payload["repositoryLimits"]
        self.assertEqual(payload["model_profile"]["default_model"], codex_config["model"])
        self.assertEqual(payload["model_profile"]["core_effort"], codex_config["reasoningEffort"])
        self.assertEqual(payload["model_profile"]["non_core_effort"], "medium")
        self.assertEqual(payload["review_request"]["budget"]["max_wall_time_seconds"], review_worker_config["scanDeadlineSeconds"])
        self.assertGreater(payload["review_request"]["budget"]["max_estimated_input_tokens"], 0)
        self.assertEqual(payload["review_request"]["policy"]["turn_timeout_seconds"], review_worker_config["turnTimeoutSeconds"])
        self.assertGreater(repository_limits["maxFiles"], 0)
        self.assertGreater(repository_limits["maxBytes"], 0)

    def test_scan_job_payload_uses_subscription_plan_worker_config_and_limits(self) -> None:
        app.USERS = {
            "usr_max": {
                "id": "usr_max",
                "name": "Max Owner",
                "providers": [],
                "billing": {"status": "active", "plan": "max"},
            }
        }
        scan = {
            "id": "sc_plan_limits",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc123",
            "status": "queued",
            "userId": "usr_max",
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
        }
        app.SCANS = [scan]
        job = app.create_scan_job_for_scan(scan)

        payload = app.scan_job_payload(job)
        expected_limits = app.system_config.repository_scan_limits("max")
        expected_worker = app.billing.review_agent_config("max")["reviewWorker"]

        self.assertEqual(payload["repositoryLimits"]["maxFiles"], expected_limits["maxFiles"])
        self.assertEqual(payload["repositoryLimits"]["maxBytes"], expected_limits["maxBytes"])
        self.assertEqual(payload["model_profile"]["core_effort"], "xhigh")
        self.assertEqual(payload["model_profile"]["validator_effort"], "xhigh")
        self.assertEqual(payload["model_profile"]["non_core_effort"], "medium")
        self.assertEqual(payload["review_request"]["policy"]["turn_timeout_seconds"], expected_worker["turnTimeoutSeconds"])
        self.assertEqual(payload["review_request"]["budget"]["max_wall_time_seconds"], expected_worker["scanDeadlineSeconds"])

    def test_claim_payload_caps_enforce_mode_until_shadow_gate_passes(self) -> None:
        scan = {
            "id": "sc_enforce_gate",
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
            "repoId": "repo_123",
            "githubRepoId": "123",
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)

        with patch.dict(os.environ, {"PULLWISE_REVIEW_CALIBRATION_MODE": "enforce"}, clear=False):
            claim = self.v1_lease()

        self.assertEqual(claim.status, HTTPStatus.OK)
        self.assertNotIn("review_calibration_context", claim.payload["job"])

    def test_worker_result_records_verifier_outcome_labels_for_review_events(self) -> None:
        scan = {
            "id": "sc_verifier_labels",
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
            "repoId": "repo_123",
            "githubRepoId": "123",
        }
        app.SCANS = [scan]
        job = app.create_scan_job_for_scan(scan)
        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]

        def event(issue_id: str, observation_key: str, verdict: str) -> dict:
            return {
                "protocol": "pullwise-review-decision/0.1",
                "event_id": f"evt_{observation_key}",
                "candidate_observation_key": observation_key,
                "candidate_id": issue_id,
                "fingerprint": f"fp_{issue_id}",
                "source": "correctness-reviewer",
                "category": "correctness",
                "severity": "high",
                "verification_status": "verified" if verdict == "confirmed" else "unverified",
                "file_path": "src/app.py",
                "line_start": 12,
                "normalized_title": issue_id,
                "raw_confidence": 0.9,
                "decision": "reported",
                "scoring_protocol": "pullwise-review-score/0.1",
            }

        result = self.v1_result(
            job,
            {
                "status": "done",
                "attempt_id": "wk_1-1",
                "result_checksum": "checksum-verifier-labels",
                **audit_result_fields(
                    [
                        audit_issue_card("Confirmed issue", issue_id="issue-confirmed", severity="P1"),
                        audit_issue_card("Rejected issue", issue_id="issue-rejected", severity="P2"),
                    ],
                    [
                        audit_verification("issue-confirmed", verdict="confirmed", evidence=["A verifier reproduced it."]),
                        audit_verification("issue-rejected", verdict="rejected", evidence=[]),
                    ],
                ),
                "review_decision_events": [
                    event("issue-confirmed", "obs_worker_confirmed", "confirmed"),
                    event("issue-rejected", "obs_worker_rejected", "rejected"),
                ],
                "summary": {"critical": 0, "high": 1, "medium": 1, "low": 0, "info": 0},
            },
        )

        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertEqual(result.payload["reviewDecisionEvents"], {"inserted": 2, "duplicates": 0})
        confirmed = db.list_review_outcome_labels("obs_worker_confirmed")
        rejected = db.list_review_outcome_labels("obs_worker_rejected")
        self.assertEqual(confirmed, [])
        self.assertEqual(rejected, [])

    def test_issue_status_updates_record_lifecycle_outcome_labels(self) -> None:
        app.USERS = {"usr_1": {"id": "usr_1", "name": "Owner", "providers": []}}
        app.SESSIONS = {
            "ses_owner": {
                "id": "ses_owner",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        app.SCANS = [
            {
                "id": "sc_lifecycle",
                "repo": "acme/api",
                "branch": "main",
                "commit": "abc123",
                "status": "done",
                "userId": "usr_1",
                "createdAt": app.now(),
                "completedAt": app.now(),
                "issues": {"critical": 0, "high": 2, "medium": 0, "low": 0, "info": 0},
                "repoId": "repo_123",
                "githubRepoId": "123",
            }
        ]
        app.ISSUES = [
            {
                "id": "issue-fixed",
                "userId": "usr_1",
                "scanId": "sc_lifecycle",
                "jobId": "job_lifecycle",
                "repo": "acme/api",
                "branch": "main",
                "status": "open",
                "severity": "high",
                "title": "Fixed issue",
                "file": "src/app.py",
                "line": 12,
                "verificationStatus": "static_proof",
            },
            {
                "id": "issue-snoozed",
                "userId": "usr_1",
                "scanId": "sc_lifecycle",
                "jobId": "job_lifecycle",
                "repo": "acme/api",
                "branch": "main",
                "status": "open",
                "severity": "high",
                "title": "Snoozed issue",
                "file": "src/app.py",
                "line": 22,
                "verificationStatus": "potential_risk",
            },
        ]
        db.record_review_decision_events(
            [
                {
                    "protocol": "pullwise-review-decision/0.1",
                    "event_id": "evt_status_fixed",
                    "candidate_observation_key": "obs_status_fixed",
                    "scan_id": "sc_lifecycle",
                    "job_id": "job_lifecycle",
                    "attempt_id": "wk_1-1",
                    "user_id": "usr_1",
                    "repo_id": "repo_123",
                    "repo_full_name": "acme/api",
                    "branch": "main",
                    "candidate_id": "issue-fixed",
                    "source": "correctness-reviewer",
                    "category": "correctness",
                    "severity": "high",
                    "verification_status": "static_proof",
                    "file_path": "src/app.py",
                    "line_start": 12,
                    "raw_confidence": 0.92,
                    "normalized_title": "Fixed issue",
                    "decision": "reported",
                    "scoring_protocol": "pullwise-review-score/0.1",
                },
                {
                    "protocol": "pullwise-review-decision/0.1",
                    "event_id": "evt_status_snoozed",
                    "candidate_observation_key": "obs_status_snoozed",
                    "scan_id": "sc_lifecycle",
                    "job_id": "job_lifecycle",
                    "attempt_id": "wk_1-1",
                    "user_id": "usr_1",
                    "repo_id": "repo_123",
                    "repo_full_name": "acme/api",
                    "branch": "main",
                    "candidate_id": "issue-snoozed",
                    "source": "correctness-reviewer",
                    "category": "correctness",
                    "severity": "high",
                    "verification_status": "potential_risk",
                    "file_path": "src/app.py",
                    "line_start": 22,
                    "raw_confidence": 0.92,
                    "normalized_title": "Snoozed issue",
                    "decision": "reported",
                    "scoring_protocol": "pullwise-review-score/0.1",
                },
            ]
        )
        headers = {"Cookie": "pw_session=ses_owner"}

        fixed = RouteHarness("/issues/issue-fixed/status", {"status": "fixed"}, headers=headers)
        app.PullwiseHandler.route(fixed, "PATCH")

        self.assertEqual(fixed.status, HTTPStatus.OK)
        self.assertEqual(fixed.payload["status"], "fixed")
        self.assertNotIn("candidateObservationKey", fixed.payload)
        self.assertNotIn("reviewDecisionEvents", fixed.payload)
        fixed_labels = db.list_review_outcome_labels("obs_status_fixed")
        self.assertEqual(fixed_labels[0]["label_source"], "user_explicit")
        self.assertEqual(fixed_labels[0]["outcome_label"], "valid")

        snoozed = RouteHarness(
            "/issues/issue-snoozed/status",
            {"status": "snoozed", "reason": "Review later."},
            headers=headers,
        )
        app.PullwiseHandler.route(snoozed, "PATCH")

        self.assertEqual(snoozed.status, HTTPStatus.OK)
        self.assertEqual(snoozed.payload["status"], "snoozed")
        self.assertNotIn("feedbackReason", snoozed.payload)
        snoozed_labels = db.list_review_outcome_labels("obs_status_snoozed")
        self.assertEqual(snoozed_labels[0]["label_source"], "weak_lifecycle")
        self.assertEqual(snoozed_labels[0]["outcome_label"], "ambiguous")
        self.assertEqual(snoozed_labels[0]["label_reason"], "Review later.")

        next_scan = {
            "id": "sc_lifecycle_next",
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
            "repoId": "repo_123",
            "githubRepoId": "123",
        }
        app.SCANS.append(next_scan)
        app.create_scan_job_for_scan(next_scan)
        next_claim = self.v1_lease()

        self.assertEqual(next_claim.status, HTTPStatus.OK)
        self.assertNotIn("review_calibration_context", next_claim.payload["job"])

    def test_worker_result_fallback_checksum_includes_review_decision_events(self) -> None:
        base = {
            "status": "done",
            **audit_result_fields([]),
            "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        first = app.worker_result_checksum(
            {
                **base,
                "review_decision_events": [
                    {
                        "protocol": "pullwise-review-decision/0.1",
                        "event_id": "evt_checksum_1",
                        "candidate_observation_key": "obs_checksum_1",
                    }
                ],
            }
        )
        second = app.worker_result_checksum(
            {
                **base,
                "review_decision_events": [
                    {
                        "protocol": "pullwise-review-decision/0.1",
                        "event_id": "evt_checksum_2",
                        "candidate_observation_key": "obs_checksum_2",
                    }
                ],
            }
        )

        self.assertNotEqual(first, second)

    def test_worker_result_checksum_ignores_worker_supplied_checksum(self) -> None:
        base = {
            "status": "done",
            **audit_result_fields([]),
            "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        first = app.worker_result_checksum({**base, "result_checksum": "same-worker-result", "duration_ms": 1})
        second = app.worker_result_checksum({**base, "result_checksum": "same-worker-result", "duration_ms": 2})

        self.assertNotEqual(first, "same-worker-result")
        self.assertNotEqual(first, second)

    def test_review_outcome_label_priority_keeps_pipeline_and_weak_signals_separate(self) -> None:
        self.assertEqual(app.effective_review_outcome_label("missing_observation"), {})

        weak = app.record_weak_lifecycle_signal(
            candidate_observation_key="obs_priority",
            outcome_label="false_positive",
            reason="candidate disappeared later",
        )
        self.assertEqual(weak["outcome_label"], "false_positive")
        manual = app.record_manual_review_outcome(
            candidate_observation_key="obs_priority",
            outcome_label="valid",
            reviewer_id="admin_1",
            reason="manual review confirmed it",
        )
        self.assertEqual(manual["outcome_label"], "valid")

        effective = app.effective_review_outcome_label("obs_priority")
        self.assertEqual(effective["outcome_label"], "valid")
        self.assertEqual(effective["label_source"], "manual_review")

    def test_review_shadow_evaluation_reports_false_positive_proxy_and_audit_promotion(self) -> None:
        def event(index: int, *, proposed: str, score: float) -> dict:
            return {
                "protocol": "pullwise-review-decision/0.1",
                "event_id": f"evt_shadow_metrics_{index}",
                "candidate_observation_key": f"obs_shadow_metrics_{index}",
                "scan_id": "sc_shadow_metrics",
                "job_id": "job_shadow_metrics",
                "attempt_id": "wk_1-1",
                "user_id": "usr_1",
                "repo_id": "repo_123",
                "github_repo_id": "123",
                "repo_full_name": "acme/api",
                "branch": "main",
                "commit_sha": "a" * 40,
                "candidate_id": f"candidate-{index}",
                "fingerprint": f"fp-shadow-metrics-{index}",
                "source": "correctness reviewer",
                "provider": "codex",
                "model": "gpt-5.5",
                "category": "correctness",
                "severity": "medium",
                "verification_status": "potential_risk",
                "file_path": "src/app.py",
                "line_start": 12,
                "raw_confidence": score,
                "calibrated_confidence": score,
                "decision_score": score,
                "decision": "reported",
                "decision_reason": "test",
                "scoring_protocol": "pullwise-review-score/0.1",
                "score_factors": {"scoreKind": "ranking_score", "proposedDecision": proposed, "decisionScore": score},
                "created_at": app.now(),
            }

        db.record_review_decision_events(
            [
                event(1, proposed="reported", score=0.83),
                event(2, proposed="audit_only", score=0.75),
                event(3, proposed="audit_only", score=0.55),
            ]
        )
        app.record_manual_review_outcome(
            event_id="evt_shadow_metrics_1",
            candidate_observation_key="obs_shadow_metrics_1",
            outcome_label="valid",
            reviewer_id="admin_1",
        )
        app.record_manual_review_outcome(
            event_id="evt_shadow_metrics_2",
            candidate_observation_key="obs_shadow_metrics_2",
            outcome_label="false_positive",
            reviewer_id="admin_1",
        )
        app.record_manual_review_outcome(
            event_id="evt_shadow_metrics_3",
            candidate_observation_key="obs_shadow_metrics_3",
            outcome_label="valid",
            reviewer_id="admin_1",
        )

        evaluation = app.review_shadow_evaluation("user:usr_1|repo:repo_123|branch:main")

        self.assertEqual(evaluation["labeledOutcomeCount"], 3)
        self.assertEqual(evaluation["currentReportedLabeledCount"], 3)
        self.assertEqual(evaluation["currentReportedFalsePositiveCount"], 1)
        self.assertAlmostEqual(evaluation["currentFalsePositiveProxy"], 1 / 3)
        self.assertEqual(evaluation["proposedReportedLabeledCount"], 1)
        self.assertEqual(evaluation["proposedReportedFalsePositiveCount"], 0)
        self.assertEqual(evaluation["estimatedFalsePositiveReduction"], 1)
        self.assertEqual(evaluation["auditOnlyReviewedCount"], 2)
        self.assertEqual(evaluation["auditOnlyValidCount"], 1)
        self.assertEqual(evaluation["auditOnlyPromotionRate"], 0.5)
        distribution = evaluation["scoreDistributionByVerificationStatus"]["potential_risk"]
        self.assertEqual(distribution["0_82_0_90"], 1)
        self.assertEqual(distribution["0_70_0_82"], 1)
        self.assertEqual(distribution["lt_0_60"], 1)

    def test_review_shadow_evaluation_counts_verified_suppression_guardrail(self) -> None:
        db.record_review_decision_events(
            [
                {
                    "protocol": "pullwise-review-decision/0.1",
                    "event_id": "evt_verified_suppression",
                    "candidate_observation_key": "obs_verified_suppression",
                    "scan_id": "sc_guardrail",
                    "job_id": "job_guardrail",
                    "attempt_id": "wk_1-1",
                    "user_id": "usr_1",
                    "repo_id": "repo_123",
                    "github_repo_id": "123",
                    "repo_full_name": "acme/api",
                    "branch": "main",
                    "commit_sha": "a" * 40,
                    "candidate_id": "candidate-verified",
                    "fingerprint": "fp-verified",
                    "source": "static checker",
                    "provider": "deterministic",
                    "model": "rules",
                    "category": "build",
                    "severity": "high",
                    "verification_status": "static_proof",
                    "file_path": "Dockerfile",
                    "line_start": 4,
                    "raw_confidence": 0.95,
                    "calibrated_confidence": 0.95,
                    "decision_score": 0.95,
                    "decision": "reported",
                    "decision_reason": "reported",
                    "scoring_protocol": "pullwise-review-score/0.1",
                    "score_factors": {
                        "scoreKind": "ranking_score",
                        "proposedDecision": "audit_only",
                        "proposedReason": "bad source history",
                    },
                    "created_at": app.now(),
                }
            ]
        )

        evaluation = app.review_shadow_evaluation("user:usr_1|repo:repo_123|branch:main")

        self.assertEqual(evaluation["candidateCount"], 1)
        self.assertEqual(evaluation["currentReportedCount"], 1)
        self.assertEqual(evaluation["proposedAuditOnlyCount"], 1)
        self.assertEqual(evaluation["verifiedSuppressionCount"], 1)

    def test_worker_supplied_checksum_cannot_mask_different_result_body(self) -> None:
        scan = {
            "id": "sc_duplicate_body",
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
        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]

        first = self.v1_result(
            job,
            {
                "status": "done",
                "attempt_id": "wk_1-1",
                "result_checksum": "same-worker-result",
                **audit_result_fields(
                    [audit_issue_card("First result", issue_id="issue-first", severity="P1")]
                ),
                "summary": {"critical": 1, "high": 0, "medium": 0, "low": 0, "info": 0},
            },
        )
        self.assertEqual(first.status, HTTPStatus.OK)
        self.assertEqual([issue["id"] for issue in app.ISSUES], ["issue-first"])
        self.assertEqual(app.SCANS[0]["issues"]["high"], 1)

        duplicate = self.v1_result(
            job,
            {
                "status": "done",
                "attempt_id": "wk_1-1",
                "result_checksum": "same-worker-result",
                **audit_result_fields(
                    [audit_issue_card("Second result", issue_id="issue-second", severity="P2")]
                ),
                "summary": {"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 0},
            },
        )

        self.assertEqual(duplicate.status, HTTPStatus.CONFLICT)
        self.assertIn("checksum conflicts", duplicate.payload["message"])
        self.assertEqual([issue["id"] for issue in app.ISSUES], ["issue-first"])
        self.assertEqual(app.SCANS[0]["issues"]["high"], 1)
        self.assertEqual(app.SCANS[0]["issues"]["medium"], 0)

    def test_worker_protocol_finding_source_preserves_agent_report_detail_fields(self) -> None:
        finding = {
            "id": "cluster-p0-b002-correctness-001",
            "title": "Bundle planner emits over-cap bundles",
            "severity": "medium",
            "category": "pipeline-correctness",
            "confidence": 0.98,
            "locations": [
                {"path": "pullwise_worker/review_worker_v1.py", "start_line": 3795, "end_line": 3806},
                {"path": "tests/test_review_worker_v1.py", "start_line": 1208, "end_line": 1224},
            ],
            "impact": "Oversized single-file hotspots are sent above the intended token cap.",
            "recommendation": "Split or specially route single files above the token cap.",
            "next_agent_task": "Add a regression test for one oversized file.",
            "evidence": [
                "Location verification kept the cited planner range valid.",
                "Intent test ITV-002 reproduced the defect.",
            ],
            "validation_sources": {
                "location_verification": "location-verification.json",
                "related_code": ["pullwise_worker/review_worker_v1.py:3795-3806"],
                "intent_test": {"test_id": "ITV-002", "classification": "confirmed_bug"},
            },
            "disproof_attempt": "Existing tests only cover multi-file overflow.",
        }
        source = app.worker_protocol_finding_source(finding)
        issue = app.worker_finding_payload(
            {
                "job_id": "job_agent_detail",
                "scan_id": "sc_agent_detail",
                "user_id": "usr_1",
                "repo": "acme/api",
                "branch": "main",
                "commit": "abc1234",
            },
            source,
            0,
        )

        payload = app.issue_payload(issue)

        self.assertEqual(payload["impact"], finding["impact"])
        self.assertEqual(payload["recommendation"], finding["recommendation"])
        self.assertEqual(payload["nextAgentTask"], finding["next_agent_task"])
        self.assertEqual(payload["disproofAttempt"], finding["disproof_attempt"])
        self.assertEqual(payload["validationSources"]["intent_test"]["classification"], "confirmed_bug")
        self.assertGreaterEqual(len(payload["affectedLocations"]), 2)
        self.assertEqual(payload["affectedLocations"][0]["file"], "pullwise_worker/review_worker_v1.py")
        self.assertEqual(payload["affectedLocations"][1]["file"], "tests/test_review_worker_v1.py")
        self.assertTrue(any("Location verification" in item.get("summary", "") for item in payload["evidence"]))

    def test_worker_result_exposes_reproducible_evidence_chain(self) -> None:
        scan = {
            "id": "sc_evidence",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc1234",
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

        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]

        result = self.v1_result(
            job,
            {
                "status": "done",
                "attempt_id": "wk_1-1",
                "result_checksum": "checksum-evidence",
                **audit_result_fields(
                    [
                        audit_issue_card(
                            "Reject invalid page numbers",
                            issue_id="f_page_zero",
                            severity="P2",
                            category="Quality",
                            file="src/app.py",
                            line=12,
                            end_line=14,
                            claim="page=0 creates a negative offset.",
                            impact="malformed input returns 500",
                            evidence=[
                                {
                                    "type": "code",
                                    "label": "Offset calculation",
                                    "summary": "page is used without a lower bound.",
                                    "file": "src/app.py",
                                    "startLine": 12,
                                    "endLine": 14,
                                }
                            ],
                            reproduction={
                                "commands": ["pytest tests/repro/test_page_zero.py"],
                                "input": "GET /users?page=0",
                                "expected": "400 validation error",
                                "actual": "500 internal server error",
                                "testFile": "tests/repro/test_page_zero.py",
                                "logPath": "logs/f_page_zero.log",
                            },
                            false_positive_checks=["The parameter is read from the request query."],
                            limitations=["A production API gateway could reject page < 1 before the app."],
                        )
                    ],
                    [
                        audit_verification(
                            "f_page_zero",
                            proof_type="failing_test",
                            proof_strength=3,
                            evidence=["A focused test reproduces the 500 response."],
                            commands_run=["pytest tests/repro/test_page_zero.py"],
                            result_summary="500 internal server error",
                            log_path="logs/f_page_zero.log",
                            output="FAIL tests/repro/test_page_zero.py\nAssertionError: expected 400 received 500",
                        )
                    ],
                ),
                "summary": {"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 0}            },
        )

        self.assertEqual(result.status, HTTPStatus.OK)
        payload = app.issue_payload(app.ISSUES[0])
        self.assertEqual(payload["reproduction"]["commands"], ["pytest tests/repro/test_page_zero.py"])
        self.assertEqual(payload["affectedLocations"][0]["url"], "https://github.com/acme/api/blob/abc1234/src/app.py#L12-L14")
        self.assertEqual(payload["limitations"], ["A production API gateway could reject page < 1 before the app."])
        self.assertEqual(payload["verificationStatus"], "verified")
        scan_payload = app.scan_payload(app.SCANS[0])

    def test_scan_audit_bundle_route_returns_owner_scoped_evidence(self) -> None:
        timestamp = app.now()
        app.USERS = {
            "usr_1": {"id": "usr_1", "name": "Owner", "providers": []},
            "usr_2": {"id": "usr_2", "name": "Other", "providers": []},
        }
        app.SESSIONS = {
            "ses_owner": {
                "id": "ses_owner",
                "userId": "usr_1",
                "createdAt": timestamp,
                "expiresAt": timestamp + 3600,
            },
            "ses_other": {
                "id": "ses_other",
                "userId": "usr_2",
                "createdAt": timestamp,
                "expiresAt": timestamp + 3600,
            },
        }
        app.SCANS = [
            {
                "id": "sc_bundle",
                "repo": "acme/api",
                "branch": "main",
                "commit": "abc1234",
                "status": "done",
                "userId": "usr_1",
                "createdAt": timestamp,
                "completedAt": timestamp,
                "issues": {"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 0},
                "preflight": {
                    "mode": "static",
                    "execution": "allowlisted_verifier_scripts",
                    "summary": "Detected npm package scripts and one failed verifier run.",
                    "packageManagers": ["npm"],
                    "languages": ["JavaScript"],
                    "availableScripts": ["test"],
                    "environment": {
                        "os": "Linux",
                        "osRelease": "6.8.0",
                        "platform": "Linux-6.8.0-x86_64",
                        "machine": "x86_64",
                        "pythonVersion": "3.12.3",
                    },
                    "toolVersions": [
                        {
                            "name": "git",
                            "command": "git --version",
                            "available": True,
                            "exitCode": 0,
                            "output": "git version 2.45.0",
                        },
                        {
                            "name": "node",
                            "command": "node --version",
                            "available": True,
                            "exitCode": 0,
                            "output": "v22.21.0",
                        },
                    ],
                    "verifier": {
                        "enabled": True,
                        "summary": "1 verifier command failed.",
                        "runs": [
                            {
                                "script": "test",
                                "command": "npm run test",
                                "status": "failed",
                                "exitCode": 1,
                                "durationMs": 100,
                                "logPath": "verification/sc_bundle/test.log",
                                "output": "FAIL tests/repro/page-zero.test.js\nAssertionError: expected 400 received 500",
                            }
                        ],
                    },
                }            }
        ]
        app.ISSUES = [
            {
                "id": "f_page_zero",
                "scanId": "sc_bundle",
                "jobId": "job_1",
                "userId": "usr_1",
                "repo": "acme/api",
                "branch": "main",
                "commit": "abc1234",
                "severity": "medium",
                "category": "Quality",
                "title": "page=0 returns 500",
                "file": "src/users.js",
                "line": 42,
                "badCode": [{"ln": 42, "code": "const offset = (page - 1) * limit", "t": "del"}],
                "goodCode": [
                    {"ln": 42, "code": "const pageNumber = Math.max(1, page)", "t": "add"},
                    {"ln": 43, "code": "const offset = (pageNumber - 1) * limit", "t": "add"},
                ],
                "verificationStatus": "verified",
                "verificationSummary": "A focused test reproduces the 500 response.",
                "affectedLocations": [{"file": "src/users.js", "startLine": 42, "endLine": 45}],
                "evidence": [
                    {
                        "type": "code",
                        "label": "Offset calculation",
                        "summary": "page is used without a lower bound.",
                        "file": "src/users.js",
                        "startLine": 42,
                        "endLine": 45,
                    },
                    {
                        "type": "runtime_log",
                        "label": "Repro run",
                        "summary": "The focused test failed with the observed 500 response.",
                        "command": "npm run test -- tests/repro/page-zero.test.js",
                        "exitCode": 1,
                        "logPath": "logs/f_page_zero.log",
                        "output": "FAIL tests/repro/page-zero.test.js\nAssertionError: expected 400 received 500",
                    },
                ],
                "reproduction": {
                    "commands": ["npm run test -- tests/repro/page-zero.test.js"],
                    "input": "GET /api/users?page=0",
                    "expected": "400 validation error",
                    "actual": "500 internal server error",
                    "testFile": "tests/repro/page-zero.test.js",
                    "logPath": "logs/f_page_zero.log",
                },
                "whyNotFalsePositive": ["The page parameter is read from the request query."],
                "limitations": ["A production API gateway could reject page < 1 first."],
            },
            {
                "id": "f_wrong_user",
                "scanId": "sc_bundle",
                "userId": "usr_2",
                "repo": "acme/api",
                "branch": "main",
                "commit": "abc1234",
                "severity": "high",
                "title": "Should not be bundled",
                "file": "src/other.js",
                "line": 1,
            },
        ]
        db.upsert_issue(app.ISSUES[0])
        app.ISSUES = [app.ISSUES[1]]

        owner = RouteHarness(
            "/scans/sc_bundle/audit-bundle",
            headers={"Cookie": f"{app.SESSION_COOKIE}=ses_owner"},
        )
        app.PullwiseHandler.route(owner, "GET")

        self.assertEqual(owner.status, HTTPStatus.OK)
        self.assertEqual(owner.payload["kind"], "pullwise.review_audit_bundle")
        self.assertEqual(owner.payload["schemaVersion"], 1)
        self.assertEqual(owner.payload["scan"]["id"], "sc_bundle")
        self.assertEqual(owner.payload["preflight"]["verifier"]["runs"][0]["status"], "failed")
        self.assertTrue(owner.payload["preflight"]["verifier"]["runs"][0]["outputRedacted"])
        self.assertNotIn("output", owner.payload["preflight"]["verifier"]["runs"][0])
        artifact_paths = [artifact["path"] for artifact in owner.payload["artifacts"]]
        self.assertIn("scan/scan.json", artifact_paths)
        self.assertIn("preflight/preflight.json", artifact_paths)
        self.assertIn("audit.json", artifact_paths)
        artifacts = {artifact["path"]: artifact for artifact in owner.payload["artifacts"]}
        self.assertIn('"mode": "static"', artifacts["preflight/preflight.json"]["content"])

        owner_zip = RouteHarness(
            "/scans/sc_bundle/audit-bundle.zip",
            headers={"Cookie": f"{app.SESSION_COOKIE}=ses_owner"},
        )
        app.PullwiseHandler.route(owner_zip, "GET")

        self.assertEqual(owner_zip.status, HTTPStatus.OK)
        self.assertEqual(owner_zip.content_type, "application/zip")
        self.assertEqual(
            owner_zip.headers_out["Content-Disposition"],
            'attachment; filename="pullwise-audit-sc_bundle.zip"',
        )
        with zipfile.ZipFile(io.BytesIO(owner_zip.binary_payload), "r") as archive:
            self.assertIn("scan/scan.json", archive.namelist())
            self.assertIn("preflight/preflight.json", archive.namelist())
            self.assertIn("audit.json", archive.namelist())

        other_user = RouteHarness(
            "/scans/sc_bundle/audit-bundle",
            headers={"Cookie": f"{app.SESSION_COOKIE}=ses_other"},
        )
        app.PullwiseHandler.route(other_user, "GET")
        self.assertEqual(other_user.status, HTTPStatus.NOT_FOUND)

        anonymous = RouteHarness("/scans/sc_bundle/audit-bundle")
        app.PullwiseHandler.route(anonymous, "GET")
        self.assertEqual(anonymous.status, HTTPStatus.UNAUTHORIZED)

    def test_scan_audit_bundle_zip_route_reuses_cached_archive(self) -> None:
        self.audit_bundle_cache_fixture()

        with patch("pullwise_server.app.scan_audit_bundle_zip_bytes", return_value=b"zip-v1") as build:
            first = RouteHarness(
                "/scans/sc_cache/audit-bundle.zip",
                headers={"Cookie": f"{app.SESSION_COOKIE}=ses_owner"},
            )
            app.PullwiseHandler.route(first, "GET")

        self.assertEqual(first.status, HTTPStatus.OK)
        self.assertEqual(first.binary_payload, b"zip-v1")
        build.assert_called_once()

        with patch(
            "pullwise_server.app.scan_audit_bundle_zip_bytes",
            side_effect=AssertionError("cached archive was regenerated"),
        ) as build_again:
            second = RouteHarness(
                "/scans/sc_cache/audit-bundle.zip",
                headers={"Cookie": f"{app.SESSION_COOKIE}=ses_owner"},
            )
            app.PullwiseHandler.route(second, "GET")

        self.assertEqual(second.status, HTTPStatus.OK)
        self.assertEqual(second.binary_payload, b"zip-v1")
        build_again.assert_not_called()

    def test_scan_audit_bundle_zip_cache_invalidates_when_issue_content_changes(self) -> None:
        scan = self.audit_bundle_cache_fixture(issue_title="Original cached issue")

        with patch("pullwise_server.app.scan_audit_bundle_zip_bytes", return_value=b"zip-v1"):
            self.assertEqual(app.get_or_create_scan_audit_bundle_zip_bytes(scan), b"zip-v1")

        app.ISSUES[0]["title"] = "Updated cached issue"
        with patch("pullwise_server.app.scan_audit_bundle_zip_bytes", return_value=b"zip-v2") as build:
            self.assertEqual(app.get_or_create_scan_audit_bundle_zip_bytes(scan), b"zip-v2")

        build.assert_called_once_with(scan)
        cache_files = os.listdir(app.audit_bundle_cache_dir())
        self.assertEqual(len([name for name in cache_files if name.endswith(".zip")]), 1)

    def test_scan_audit_bundle_zip_cache_deduplicates_concurrent_generation(self) -> None:
        scan = self.audit_bundle_cache_fixture()
        entered = threading.Event()
        second_entered = threading.Event()
        release = threading.Event()
        call_lock = threading.Lock()
        calls = 0
        results: list[bytes] = []
        errors: list[BaseException] = []

        def build_archive(target_scan: dict) -> bytes:
            nonlocal calls
            with call_lock:
                calls += 1
                current_call = calls
            if current_call == 1:
                entered.set()
            else:
                second_entered.set()
            release.wait(timeout=5)
            return b"zip-shared"

        def download() -> None:
            try:
                results.append(app.get_or_create_scan_audit_bundle_zip_bytes(scan))
            except BaseException as exc:  # pragma: no cover - surfaced by assertion below
                errors.append(exc)

        with patch("pullwise_server.app.scan_audit_bundle_zip_bytes", side_effect=build_archive):
            first = threading.Thread(target=download)
            first.start()
            self.assertTrue(entered.wait(timeout=2))

            others = [threading.Thread(target=download) for _ in range(4)]
            for thread in others:
                thread.start()

            self.assertFalse(second_entered.wait(timeout=0.2))
            release.set()
            first.join(timeout=2)
            for thread in others:
                thread.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertEqual(calls, 1)
        self.assertEqual(results, [b"zip-shared"] * 5)

    def test_issue_payload_downgrades_verified_command_without_runtime_output(self) -> None:
        app.SCANS = [
            {
                "id": "sc_command_only",
                "repo": "acme/api",
                "branch": "main",
                "commit": "abc1234",
                "status": "done",
                "userId": "usr_1",
                "createdAt": app.now(),
                "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            }
        ]
        issue = {
            "id": "f_command_only",
            "scanId": "sc_command_only",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc1234",
            "severity": "medium",
            "category": "Quality",
            "title": "Command-only proof",
            "file": "src/app.py",
            "line": 12,
            "verificationStatus": "verified",
            "reportedVerificationStatus": "verified",
            "affectedLocations": [{"file": "src/app.py", "startLine": 12, "endLine": 14}],
            "evidence": [
                {
                    "type": "code",
                    "label": "Bounds check",
                    "summary": "Static code evidence only.",
                    "file": "src/app.py",
                    "startLine": 12,
                    "endLine": 14,
                }
            ],
            "reproduction": {
                "commands": ["pytest tests/repro/test_bounds.py"],
                "input": "",
                "expected": "",
                "actual": "",
                "testFile": "",
                "logPath": "",
            },
        }

        payload = app.issue_payload(issue)

        self.assertEqual(payload["verificationStatus"], "static_proof")
        self.assertEqual(payload["reportedVerificationStatus"], "verified")
        checklist = {item["label"]: item["met"] for item in payload["evidenceChecklist"]}
        self.assertTrue(checklist["Reproduction command"])
        self.assertFalse(checklist["Runtime output"])
        app.ISSUES = [issue]
        scan_payload = app.scan_payload(app.SCANS[0])
        self.assertEqual(scan_payload["verification"]["static_proof"], 1)

    def test_issue_payload_downgrades_verified_runtime_without_fixed_commit(self) -> None:
        app.SCANS = [
            {
                "id": "sc_pending_commit",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "done",
                "userId": "usr_1",
                "createdAt": app.now(),
                "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            }
        ]
        issue = {
            "id": "f_pending_runtime",
            "scanId": "sc_pending_commit",
            "repo": "acme/api",
            "branch": "main",
            "commit": "pending",
            "severity": "medium",
            "category": "Quality",
            "title": "Runtime proof without fixed commit",
            "file": "src/app.py",
            "line": 12,
            "verificationStatus": "verified",
            "reportedVerificationStatus": "verified",
            "affectedLocations": [{"file": "src/app.py", "startLine": 12, "endLine": 14}],
            "evidence": [
                {
                    "type": "runtime_log",
                    "label": "Verifier output",
                    "summary": "A command failed in the verifier.",
                    "command": "pytest tests/repro.py",
                    "exitCode": 1,
                    "output": "AssertionError",
                }
            ],
            "reproduction": {
                "commands": ["pytest tests/repro.py"],
                "actual": "Command exited 1.",
            },
        }

        payload = app.issue_payload(issue)

        self.assertEqual(payload["verificationStatus"], "static_proof")
        self.assertEqual(payload["reportedVerificationStatus"], "verified")
        checklist = {item["label"]: item["met"] for item in payload["evidenceChecklist"]}
        self.assertFalse(checklist["Fixed commit"])
        self.assertTrue(checklist["Runtime output"])
        self.assertIsNone(payload["evidence"][0].get("url"))
        app.ISSUES = [issue]
        scan_payload = app.scan_payload(app.SCANS[0])
        self.assertEqual(scan_payload["verification"]["verified"], 0)
        self.assertEqual(scan_payload["verification"]["static_proof"], 1)

    def test_issue_payload_downgrades_verified_runtime_without_reproduction_command(self) -> None:
        issue = {
            "id": "f_no_repro_command",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc1234",
            "severity": "medium",
            "category": "Quality",
            "title": "Runtime proof without copyable command",
            "file": "src/app.py",
            "line": 12,
            "verificationStatus": "verified",
            "reportedVerificationStatus": "verified",
            "affectedLocations": [{"file": "src/app.py", "startLine": 12, "endLine": 14}],
            "evidence": [
                {
                    "type": "runtime_log",
                    "label": "Verifier output",
                    "summary": "A command failed in the verifier.",
                    "command": "pytest tests/repro.py",
                    "exitCode": 1,
                    "output": "AssertionError",
                }
            ],
            "reproduction": {"actual": "Command exited 1."},
        }

        payload = app.issue_payload(issue)

        self.assertEqual(payload["verificationStatus"], "static_proof")
        self.assertEqual(payload["reportedVerificationStatus"], "verified")
        checklist = {item["label"]: item["met"] for item in payload["evidenceChecklist"]}
        self.assertTrue(checklist["Fixed commit"])
        self.assertFalse(checklist["Reproduction command"])
        self.assertFalse(checklist["Runtime output"])

    def test_issue_payload_downgrades_verified_runtime_without_raw_output(self) -> None:
        issue = {
            "id": "f_no_raw_output",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc1234",
            "severity": "medium",
            "category": "Quality",
            "title": "Runtime command without raw output",
            "file": "src/app.py",
            "line": 12,
            "verificationStatus": "verified",
            "reportedVerificationStatus": "verified",
            "affectedLocations": [{"file": "src/app.py", "startLine": 12, "endLine": 14}],
            "evidence": [
                {
                    "type": "runtime_log",
                    "label": "Verifier command",
                    "summary": "A verifier command was identified.",
                    "command": "pytest tests/repro.py",
                }
            ],
            "reproduction": {"commands": ["pytest tests/repro.py"]},
        }

        payload = app.issue_payload(issue)

        self.assertEqual(payload["verificationStatus"], "static_proof")
        self.assertEqual(payload["reportedVerificationStatus"], "verified")
        checklist = {item["label"]: item["met"] for item in payload["evidenceChecklist"]}
        self.assertTrue(checklist["Fixed commit"])
        self.assertTrue(checklist["Reproduction command"])
        self.assertFalse(checklist["Runtime output"])
        self.assertFalse(checklist["Raw log or test"])

    def test_issue_payload_downgrades_verified_runtime_with_only_exit_code(self) -> None:
        issue = {
            "id": "f_exit_code_only",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc1234",
            "severity": "medium",
            "category": "Quality",
            "title": "Runtime command without inspectable output",
            "file": "src/app.py",
            "line": 12,
            "verificationStatus": "verified",
            "reportedVerificationStatus": "verified",
            "affectedLocations": [{"file": "src/app.py", "startLine": 12, "endLine": 14}],
            "evidence": [
                {
                    "type": "runtime_log",
                    "label": "Verifier exit",
                    "summary": "A verifier command exited non-zero, but no raw output was captured.",
                    "command": "pytest tests/repro.py",
                    "exitCode": 1,
                }
            ],
            "reproduction": {"commands": ["pytest tests/repro.py"]},
        }

        payload = app.issue_payload(issue)

        self.assertEqual(payload["verificationStatus"], "static_proof")
        self.assertEqual(payload["reportedVerificationStatus"], "verified")
        checklist = {item["label"]: item["met"] for item in payload["evidenceChecklist"]}
        self.assertTrue(checklist["Reproduction command"])
        self.assertFalse(checklist["Runtime output"])
        self.assertFalse(checklist["Raw log or test"])
        self.assertEqual(payload["evidence"][1]["exitCode"], 1)

    def test_issue_payload_downgrades_verified_runtime_without_precise_line(self) -> None:
        issue = {
            "id": "f_no_precise_line",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc1234",
            "severity": "medium",
            "category": "Quality",
            "title": "Runtime proof without precise line",
            "file": "src/app.py",
            "verificationStatus": "verified",
            "reportedVerificationStatus": "verified",
            "evidence": [
                {
                    "type": "runtime_log",
                    "label": "Verifier output",
                    "summary": "A command failed in the verifier.",
                    "command": "pytest tests/repro.py",
                    "exitCode": 1,
                    "output": "AssertionError",
                }
            ],
            "reproduction": {
                "commands": ["pytest tests/repro.py"],
                "actual": "Command exited 1.",
            },
        }

        payload = app.issue_payload(issue)

        self.assertEqual(payload["verificationStatus"], "static_proof")
        self.assertEqual(payload["reportedVerificationStatus"], "verified")
        checklist = {item["label"]: item["met"] for item in payload["evidenceChecklist"]}
        self.assertTrue(checklist["Fixed commit"])
        self.assertFalse(checklist["Precise file and line"])
        self.assertTrue(checklist["Reproduction command"])
        self.assertTrue(checklist["Runtime output"])

    def test_worker_result_persists_scan_preflight_metadata(self) -> None:
        scan = {
            "id": "sc_preflight",
            "repo": "acme/app",
            "branch": "main",
            "commit": "abc1234",
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

        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]

        result = self.v1_result(
            job,
            {
                "status": "done",
                "attempt_id": "wk_1-1",
                "result_checksum": "checksum-preflight",
                **audit_result_fields([]),
                "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                "preflight": {
                    "mode": "static",
                    "execution": "no_project_scripts",
                    "summary": "Static preflight\nwithout scripts.",
                    "repo": "acme/app",
                    "branch": "main",
                    "commit": "abc1234",
                    "workerVersion": "0.2.0",
                    "provider": "codex",
                    "environment": {
                        "os": "Linux",
                        "osRelease": "6.8.0",
                        "platform": "Linux-6.8.0-x86_64",
                        "machine": "x86_64",
                        "pythonVersion": "3.12.3",
                        "checkoutRoot": "/srv/pullwise/checkouts/job",
                    },
                    "languages": ["JavaScript/TypeScript"],
                    "packageManagers": ["pnpm"],
                    "availableScripts": ["build", "test"],
                    "manifests": [
                        {"file": "package.json", "type": "node"},
                        {"file": "../secret", "type": "bad"},
                    ],
                    "toolVersions": [
                        {
                            "name": "git",
                            "command": "git --version",
                            "available": True,
                            "exitCode": 0,
                            "output": "git version 2.45.0\nextra",
                        },
                        {"name": "", "command": "bad", "available": True, "exitCode": 0, "output": "bad"},
                    ],
                    "verifier": {
                        "enabled": True,
                        "summary": "Verifier ran 1 command.\n1 failed.",
                        "runs": [
                            {
                                "script": "test",
                                "command": "npm run test",
                                "status": "failed",
                                "exitCode": 1,
                                "durationMs": 1234,
                                "logPath": "verification/job/test.log",
                                "output": "FAIL\nAssertionError",
                            },
                            {
                                "script": "lint",
                                "command": "npm run lint",
                                "status": "flaky",
                                "exitCode": 1,
                                "durationMs": 2345,
                                "confirmedFailure": False,
                                "logPath": "verification/job/lint.log",
                                "output": "--- attempt 1 (failed exit 1) ---\nFAIL\n--- attempt 2 (passed exit 0) ---\nPASS",
                                "attempts": [
                                    {
                                        "attempt": 1,
                                        "status": "failed",
                                        "exitCode": 1,
                                        "durationMs": 100,
                                        "output": "FAIL",
                                    },
                                    {
                                        "attempt": 2,
                                        "status": "passed",
                                        "exitCode": 0,
                                        "durationMs": 90,
                                        "output": "PASS",
                                    },
                                ],
                            },
                            {"script": "", "command": "", "status": "bad"},
                        ],
                    },
                    "limitations": ["No dependency installation was executed."],
                },
            },
        )

        self.assertEqual(result.status, HTTPStatus.OK)
        payload = app.scan_payload(app.SCANS[0])
        self.assertEqual(payload["preflight"]["mode"], "static")
        self.assertEqual(payload["preflight"]["execution"], "no_project_scripts")
        self.assertEqual(payload["preflight"]["summary"], "Static preflight without scripts.")
        self.assertEqual(
            payload["preflight"]["environment"],
            {
                "os": "Linux",
                "osRelease": "6.8.0",
                "platform": "Linux-6.8.0-x86_64",
                "machine": "x86_64",
                "pythonVersion": "3.12.3",
            },
        )
        self.assertNotIn("checkoutRoot", payload["preflight"]["environment"])
        self.assertEqual(payload["preflight"]["packageManagers"], ["pnpm"])
        self.assertEqual(payload["preflight"]["availableScripts"], ["build", "test"])
        self.assertEqual(payload["preflight"]["manifests"], [{"file": "package.json", "type": "node"}])
        self.assertEqual(payload["preflight"]["toolVersions"][0]["name"], "git")
        self.assertEqual(payload["preflight"]["toolVersions"][0]["output"], "git version 2.45.0 extra")
        self.assertTrue(payload["preflight"]["verifier"]["enabled"])
        self.assertEqual(payload["preflight"]["verifier"]["summary"], "Verifier ran 1 command. 1 failed.")
        self.assertEqual(
            payload["preflight"]["verifier"]["runs"],
            [
                {
                    "script": "test",
                    "command": "npm run test",
                    "status": "failed",
                    "exitCode": 1,
                    "durationMs": 1234,
                    "logPath": "verification/job/test.log",
                    "outputRedacted": True,
                },
                {
                    "script": "lint",
                    "command": "npm run lint",
                    "status": "flaky",
                    "exitCode": 1,
                    "durationMs": 2345,
                    "confirmedFailure": False,
                    "attempts": [
                        {
                            "attempt": 1,
                            "status": "failed",
                            "exitCode": 1,
                            "durationMs": 100,
                            "outputRedacted": True,
                        },
                        {
                            "attempt": 2,
                            "status": "passed",
                            "exitCode": 0,
                            "durationMs": 90,
                            "outputRedacted": True,
                        },
                    ],
                    "logPath": "verification/job/lint.log",
                    "outputRedacted": True,
                }
            ],
        )

    def test_repository_too_large_worker_result_refunds_only_that_scan_quota(self) -> None:
        user = {"id": "usr_1", "name": "Owner", "providers": []}
        app.USERS = {"usr_1": user}
        repositories = []
        for index in range(4):
            repository = db.upsert_repository(
                {
                    "github_repo_id": str(10_000 + index),
                    "full_name": f"acme/repo-{index}",
                    "owner_login": "acme",
                    "default_branch": "main",
                    "private": False,
                    "clone_url": f"https://github.com/acme/repo-{index}.git",
                }
            )
            repositories.append(repository)
            scan_id = f"sc_repo_limit_{index}"
            quota_result = app.quota.consume_scan_quota(
                user=user,
                repository=repository,
                requested_by_user_id=user["id"],
                scan_id=scan_id,
                request_id=f"req_repo_limit_{index}",
            )
            scan = {
                "id": scan_id,
                "repo": repository["full_name"],
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "userId": user["id"],
                "createdAt": app.now() + index,
                "queuedAt": app.now() + index,
                "progress": 0,
                "phase": None,
                "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                "repoId": repository["id"],
                "githubRepoId": repository["github_repo_id"],
                "requestId": f"req_repo_limit_{index}",
                "quotaBucketIds": quota_result["bucketIds"],
                "billingUsage": quota_result["user"],
                "repoUsage": quota_result["repository"],
            }
            app.SCANS.append(scan)
            app.create_scan_job_for_scan(scan)

        self.assertEqual(app.quota.quota_payload_for_user(user)["used"], 4)

        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        claimed_job = claim.payload["job"]
        self.assertEqual(claimed_job["scan_id"], "sc_repo_limit_0")

        result = self.v1_result(
            claimed_job,
            {
                "status": "failed",
                "attempt_id": f"wk_1-{claimed_job['attempt']}",
                "result_checksum": "checksum-repository-too-large",
                "error": "Repository is too large for Pullwise scanning.",
                "error_code": "REPOSITORY_TOO_LARGE",
                **audit_result_fields([], execution_status="failed"),
                "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                "preflight": {
                    "mode": "static",
                    "execution": "repository_limit_check",
                    "summary": "Repository checkout exceeds Pullwise worker repository limits.",
                    "repositoryStats": {"fileCount": 2001, "totalBytes": 50 * 1024 * 1024 + 1, "scanStoppedEarly": True},
                    "repositoryLimits": {"maxFiles": 2000, "maxBytes": 50 * 1024 * 1024},
                    "repositoryLimitExceeded": True,
                    "repositoryLimitReasons": ["file_count", "total_bytes"],
                },
            },
        )

        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertEqual(result.payload["quotaRollback"]["ledgerRows"], 2)
        self.assertEqual(app.quota.quota_payload_for_user(user)["used"], 3)
        self.assertEqual(
            [app.quota.quota_payload_for_repository(repository, user)["used"] for repository in repositories],
            [0, 1, 1, 1],
        )
        payload = app.scan_payload(app.SCANS[0])
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["errorCode"], "REPOSITORY_TOO_LARGE")
        self.assertEqual(payload["quotaRefunded"]["reason"], "REPOSITORY_TOO_LARGE")
        self.assertEqual(payload["billingUsage"]["used"], 3)
        self.assertEqual(payload["repoUsage"]["used"], 0)
        self.assertEqual(
            payload["preflight"]["repositoryStats"],
            {"fileCount": 2001, "totalBytes": 50 * 1024 * 1024 + 1, "scanStoppedEarly": True},
        )
        self.assertEqual(payload["preflight"]["repositoryLimits"], {"maxFiles": 2000, "maxBytes": 50 * 1024 * 1024})
        self.assertTrue(payload["preflight"]["repositoryLimitExceeded"])
        self.assertEqual(payload["preflight"]["repositoryLimitReasons"], ["file_count", "total_bytes"])

    def test_fake_repository_too_large_without_preflight_limit_evidence_does_not_refund(self) -> None:
        user = {"id": "usr_1", "name": "Owner", "providers": []}
        app.USERS = {"usr_1": user}
        repository = db.upsert_repository(
            {
                "github_repo_id": "11001",
                "full_name": "acme/fake-large",
                "owner_login": "acme",
                "default_branch": "main",
            }
        )
        quota_result = app.quota.consume_scan_quota(
            user=user,
            repository=repository,
            requested_by_user_id=user["id"],
            scan_id="sc_fake_large",
            request_id="req_fake_large",
        )
        scan = {
            "id": "sc_fake_large",
            "repo": repository["full_name"],
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": user["id"],
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "repoId": repository["id"],
            "githubRepoId": repository["github_repo_id"],
            "requestId": "req_fake_large",
            "quotaBucketIds": quota_result["bucketIds"],
            "billingUsage": quota_result["user"],
            "repoUsage": quota_result["repository"],
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)
        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        claimed_job = claim.payload["job"]

        result = self.v1_result(
            claimed_job,
            {
                "status": "failed",
                "attempt_id": f"wk_1-{claimed_job['attempt']}",
                "result_checksum": "checksum-fake-repository-too-large",
                "error": "Repository is too large for Pullwise scanning.",
                "error_code": "REPOSITORY_TOO_LARGE",
                **audit_result_fields([], execution_status="failed"),
            },
        )

        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertNotIn("quotaRollback", result.payload)
        self.assertEqual(app.quota.quota_payload_for_user(user)["used"], 1)
        self.assertEqual(app.quota.quota_payload_for_repository(repository, user)["used"], 1)
        self.assertNotIn("quotaRefunded", app.scan_payload(app.SCANS[0]))

    def test_worker_progress_records_worker_reported_phase_steps_message_and_log_summary(self) -> None:
        scan = {
            "id": "sc_progress_phase",
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
        app.create_scan_job_for_scan(scan)
        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]

        with patch.object(app.scan_logging, "log_event") as log_event:
            progress = self.v1_event(
                job,
                phase="worker_custom_review",
                progress=55,
                message="Custom worker reviewing billing rules",
                progress_steps=[
                    {"id": "checkout", "label": "Checkout", "status": "completed", "percent": 100},
                    {"id": "worker_custom_review", "label": "Custom worker review", "status": "running", "percent": 55, "failureReason": "custom review stalled"},
                    {"id": "qa_gate", "label": "QA gate", "status": "partial_completed", "percent": 100},
                ],
            )

        self.assertEqual(progress.status, HTTPStatus.OK)
        log_event.assert_called_once()
        self.assertEqual(log_event.call_args.args[0], "review_run_event")
        self.assertEqual(log_event.call_args.kwargs["scanId"], "sc_progress_phase")
        self.assertEqual(log_event.call_args.kwargs["workerId"], "wk_1")
        self.assertEqual(log_event.call_args.kwargs["jobId"], job["job_id"])
        self.assertEqual(log_event.call_args.kwargs["runId"], job["run_id"])
        self.assertEqual(log_event.call_args.kwargs["eventType"], "progress_updated")
        self.assertEqual(log_event.call_args.kwargs["phase"], "worker_custom_review")
        self.assertEqual(log_event.call_args.kwargs["progress"], 55)
        payload = app.scan_payload(app.SCANS[0])
        self.assertEqual(payload["phase"], "worker_custom_review")
        self.assertEqual(payload["progressMessage"], "Custom worker reviewing billing rules")
        self.assertEqual([step["id"] for step in payload["progressSteps"]], ["checkout", "worker_custom_review", "qa_gate"])
        self.assertEqual(payload["progressSteps"][1]["label"], "Custom worker review")
        self.assertEqual(payload["progressSteps"][1]["error"], "custom review stalled")
        self.assertEqual(payload["progressSteps"][2]["status"], "partial_completed")
        self.assertEqual(payload["logsSummary"], "progress_updated")
        self.assertIsInstance(payload.get("updatedAt"), int)

    def test_failed_worker_result_does_not_requeue(self) -> None:
        user = {"id": "usr_no_retry_worker", "name": "Owner", "providers": []}
        app.USERS = {user["id"]: user}
        scan = {
            "id": "sc_no_retry_worker",
            "repo": "acme/no-retry-worker",
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": user["id"],
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)

        first_claim = self.v1_lease()
        self.assertEqual(first_claim.status, HTTPStatus.OK)
        first_job = first_claim.payload["job"]
        failed_result = self.v1_result(
            first_job,
            {
                "status": "failed",
                "attempt_id": f"wk_1-{first_job['attempt']}",
                "result_checksum": "checksum-worker-failed-no-retry",
                "error": "Worker failed while running review worker.",
                "error_code": "REVIEW_WORKER_COMPLETION_FAILED",
                **audit_result_fields([], execution_status="failed"),
                "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            },
        )
        self.assertEqual(failed_result.status, HTTPStatus.OK)
        stored_after_failure = db.get_scan_job(first_job["job_id"])
        self.assertEqual(stored_after_failure["status"], "failed")
        self.assertEqual(stored_after_failure["attempt"], 1)
        self.assertEqual(app.SCANS[0]["status"], "failed")
        self.assertNotIn("retry", app.SCANS[0])
    def test_v1_worker_core_progress_consumes_reserved_scan_quota(self) -> None:
        user = {"id": "usr_1", "name": "Owner", "providers": []}
        app.USERS = {"usr_1": user}
        repository = db.upsert_repository(
            {
                "github_repo_id": "11902",
                "full_name": "acme/v1-reserved",
                "owner_login": "acme",
                "default_branch": "main",
            }
        )
        quota_result = app.quota.reserve_scan_quota(
            user=user,
            repository=repository,
            requested_by_user_id=user["id"],
            scan_id="sc_v1_reserved_core",
            request_id="req_v1_reserved_core",
        )
        scan = {
            "id": "sc_v1_reserved_core",
            "repo": repository["full_name"],
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": user["id"],
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "repoId": repository["id"],
            "githubRepoId": repository["github_repo_id"],
            "requestId": "req_v1_reserved_core",
            "quotaBucketIds": quota_result["bucketIds"],
            "billingUsage": quota_result["user"],
            "repoUsage": quota_result["repository"],
            "quotaState": "reserved",
            "quotaReservedAt": app.now(),
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)
        self.assertEqual(app.quota.quota_payload_for_user(user)["used"], 0)
        self.assertEqual(app.quota.quota_payload_for_user(user)["reserved"], 1)

        lease = RouteHarness("/v1/workers/wk_1/lease", v1_worker_lease_payload(), headers=self.auth)
        app.PullwiseHandler.route(lease, "POST")
        self.assertEqual(lease.status, HTTPStatus.OK)
        job = lease.payload["job"]
        run_id = job["run_id"]
        self.assertEqual(app.quota.quota_payload_for_user(user)["used"], 0)
        self.assertEqual(app.quota.quota_payload_for_user(user)["reserved"], 1)

        setup_event = RouteHarness(
            f"/v1/review-runs/{run_id}/events",
            {
                "protocol_version": "review-worker-protocol/v1",
                "run_id": run_id,
                "worker_id": "wk_1",
                "sequence": 1,
                "timestamp": "2026-07-01T10:20:00Z",
                "event_type": "phase_started",
                "phase": "prepare_workspace",
                "severity": "info",
                "message": "Preparing workspace.",
                "progress": {"overall_percent": 1.0, "current_phase_percent": 0, "status": "running"},
                "data": {},
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(setup_event, "POST")
        self.assertEqual(setup_event.status, HTTPStatus.OK)
        self.assertEqual(app.quota.quota_payload_for_user(user)["used"], 0)
        self.assertEqual(app.quota.quota_payload_for_user(user)["reserved"], 1)

        core_event = RouteHarness(
            f"/v1/review-runs/{run_id}/events",
            {
                "protocol_version": "review-worker-protocol/v1",
                "run_id": run_id,
                "worker_id": "wk_1",
                "sequence": 2,
                "timestamp": "2026-07-01T10:21:00Z",
                "event_type": "phase_started",
                "phase": "repo_map",
                "severity": "info",
                "message": "Repository map started.",
                "progress": {"overall_percent": 20.0, "current_phase_percent": 0, "status": "running"},
                "data": {},
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(core_event, "POST")

        self.assertEqual(core_event.status, HTTPStatus.OK)
        self.assertEqual(app.quota.quota_payload_for_user(user)["used"], 1)
        self.assertEqual(app.quota.quota_payload_for_user(user)["reserved"], 0)
        payload = app.scan_payload(app.SCANS[0])
        self.assertEqual(payload["quotaState"], "consumed")
        self.assertEqual(payload["quotaConsumeTrigger"], "phase_repo_map")
        self.assertEqual(payload["billingUsage"]["used"], 1)
        self.assertEqual(payload["billingUsage"]["reserved"], 0)

    def test_worker_repo_map_progress_consumes_reserved_scan_quota(self) -> None:
        user = {"id": "usr_1", "name": "Owner", "providers": []}
        app.USERS = {"usr_1": user}
        repository = db.upsert_repository(
            {
                "github_repo_id": "11901",
                "full_name": "acme/reserved",
                "owner_login": "acme",
                "default_branch": "main",
            }
        )
        quota_result = app.quota.reserve_scan_quota(
            user=user,
            repository=repository,
            requested_by_user_id=user["id"],
            scan_id="sc_reserved_ai",
            request_id="req_reserved_ai",
        )
        scan = {
            "id": "sc_reserved_ai",
            "repo": repository["full_name"],
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": user["id"],
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "repoId": repository["id"],
            "githubRepoId": repository["github_repo_id"],
            "requestId": "req_reserved_ai",
            "quotaBucketIds": quota_result["bucketIds"],
            "billingUsage": quota_result["user"],
            "repoUsage": quota_result["repository"],
            "quotaState": "reserved",
            "quotaReservedAt": app.now(),
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)
        self.assertEqual(app.quota.quota_payload_for_user(user)["used"], 0)
        self.assertEqual(app.quota.quota_payload_for_user(user)["reserved"], 1)

        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]
        self.assertEqual(app.quota.quota_payload_for_user(user)["used"], 0)
        self.assertEqual(app.quota.quota_payload_for_user(user)["reserved"], 1)

        progress = self.v1_event(job, phase="repo_map", progress=50, event_type="phase_started")

        self.assertEqual(progress.status, HTTPStatus.OK)
        self.assertEqual(app.quota.quota_payload_for_user(user)["used"], 1)
        self.assertEqual(app.quota.quota_payload_for_user(user)["reserved"], 0)
        payload = app.scan_payload(app.SCANS[0])
        self.assertEqual(payload["quotaState"], "consumed")
        self.assertEqual(payload["billingUsage"]["used"], 1)
        self.assertEqual(payload["billingUsage"]["reserved"], 0)

    def test_worker_result_backfills_pending_commit_with_resolved_sha(self) -> None:
        resolved_commit = "1234567890abcdef1234567890abcdef12345678"
        scan = {
            "id": "sc_resolved_commit",
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

        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]

        result = self.v1_result(
            job,
            {
                "status": "done",
                "attempt_id": "wk_1-1",
                "resolved_commit": resolved_commit,
                "result_checksum": "checksum-resolved-commit",
                **audit_result_fields(
                    [
                        audit_issue_card(
                            "Reject invalid page numbers",
                            issue_id="f_resolved_commit",
                            severity="P2",
                            file="src/app.py",
                            line=12,
                            evidence=[
                                {
                                    "type": "code",
                                    "label": "Bounds check",
                                    "summary": "page is used without a lower bound.",
                                    "file": "src/app.py",
                                    "startLine": 12,
                                    "endLine": 12,
                                }
                            ],
                            reproduction={
                                "commands": ["pytest tests/repro/test_page_zero.py"],
                                "actual": "Command exited 1.",
                                "logPath": "logs/f_resolved_commit.log",
                            },
                        )
                    ],
                    [
                        audit_verification(
                            "f_resolved_commit",
                            proof_type="failing_test",
                            proof_strength=3,
                            commands_run=["pytest tests/repro/test_page_zero.py"],
                            result_summary="Command exited 1.",
                            log_path="logs/f_resolved_commit.log",
                            output="AssertionError",
                        )
                    ],
                ),
                "summary": {"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 0},
            },
        )

        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertEqual(app.SCANS[0]["commit"], resolved_commit)
        self.assertEqual(db.get_scan_job(job["job_id"])["commit"], resolved_commit)
        self.assertEqual(app.ISSUES[0]["commit"], resolved_commit)
        payload = app.issue_payload(app.ISSUES[0])
        self.assertEqual(payload["commit"], resolved_commit)
        self.assertIn(f"/blob/{resolved_commit}/src/app.py#L12", payload["affectedLocations"][0]["url"])
        self.assertEqual(payload["verificationStatus"], "verified")

    def test_worker_result_backfills_pending_commit_from_protocol_repository(self) -> None:
        resolved_commit = "1234567890abcdef1234567890abcdef12345678"
        scan = {
            "id": "sc_protocol_repo_commit",
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

        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]
        payload = {
            "status": "done",
            "attempt_id": "wk_1-1",
            "result_checksum": "checksum-protocol-repo-commit",
            **audit_result_fields([]),
            "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        payload["reviewWorkerProtocol"]["repository"] = {"commit_sha": resolved_commit}

        result = self.v1_result(job, payload)

        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertEqual(app.SCANS[0]["commit"], resolved_commit)
        self.assertEqual(db.get_scan_job(job["job_id"])["commit"], resolved_commit)

    def test_worker_result_flattens_review_run_overall_risk(self) -> None:
        scan = {
            "id": "sc_review_run_risk",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc1234",
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)

        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]
        payload = {
            "status": "done",
            "attempt_id": "wk_1-1",
            "result_checksum": "checksum-review-run-risk",
            **audit_result_fields([]),
            "summary": {"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 0},
        }
        payload["reviewWorkerProtocol"]["summary"]["overall_risk"] = "medium"

        result = self.v1_result(job, payload)

        self.assertEqual(result.status, HTTPStatus.OK)
        run_id = job["run_id"]
        self.assertEqual(db.get_review_run(run_id)["overall_risk"], "medium")
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

    def test_claim_payload_includes_review_output_language_from_scan_job(self) -> None:
        scan = {
            "id": "sc_language",
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
            "reviewOutputLanguage": "ja",
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)

        claim = self.v1_lease()

        self.assertEqual(claim.status, HTTPStatus.OK)
        self.assertEqual(claim.payload["job"]["review_output_language"], "ja")
        self.assertEqual(claim.payload["job"]["review_output_language_label"], "Japanese")

    def test_worker_result_normalizes_checkout_absolute_issue_file_path(self) -> None:
        scan = {
            "id": "sc_worker_file",
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

        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]

        result = self.v1_result(
            job,
            {
                "status": "done",
                "attempt_id": "wk_1-1",
                "result_checksum": "checksum-worker-file",
                **audit_result_fields(
                    [
                        audit_issue_card(
                            "Leaked checkout path",
                            issue_id="issue-worker-file",
                            severity="P1",
                            file=f"/var/lib/pullwise-worker/checkouts/{job['job_id']}/src/app.py",
                            line=12,
                        )
                    ]
                ),
                "summary": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
            },
        )

        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertEqual(app.ISSUES[0]["file"], "src/app.py")

        app.ISSUES[0]["file"] = f"/var/lib/pullwise-worker/checkouts/{job['job_id']}/src/app.py"
        self.assertEqual(app.issue_payload(app.ISSUES[0])["file"], "src/app.py")

        app.ISSUES[0]["file"] = "/var/log/pullwise/server.log"
        self.assertEqual(app.issue_payload(app.ISSUES[0]).get("file"), "")

    def test_claim_token_failure_requeues_job_without_marking_scan_running(self) -> None:
        scan = {
            "id": "sc_token_fail",
            "repo": "acme/api",
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
            "installationId": "111",
            "cloneUrl": "https://github.com/acme/api.git",
        }
        app.SCANS = [scan]
        job = app.create_scan_job_for_scan(scan)

        with (
            patch.object(app.github_auth, "app_api_configured", return_value=True),
            patch.object(
                app.github_auth,
                "create_installation_access_token",
                side_effect=app.github_auth.GitHubError("token unavailable"),
            ),
        ):
            claim = self.v1_lease()

        self.assertEqual(claim.status, HTTPStatus.SERVICE_UNAVAILABLE)
        stored_job = db.get_scan_job(job["job_id"])
        self.assertEqual(stored_job["status"], "queued")
        self.assertIsNone(stored_job["claimed_by_worker_id"])
        self.assertEqual(app.SCANS[0]["status"], "queued")
        self.assertNotIn("claimedByWorkerId", app.SCANS[0])

    def test_worker_routes_require_enabled_token(self) -> None:
        denied = RouteHarness("/v1/workers/wk_1/lease", v1_worker_lease_payload())
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

        wrong_worker_claim = self.v1_lease("wk_2", headers=self.auth)
        self.assertEqual(wrong_worker_claim.status, HTTPStatus.FORBIDDEN)

        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]

        _other_payload, other_token = self.create_registry_worker("wk_2")
        wrong_progress = self.v1_event(
            job,
            phase="repo_map",
            progress=50,
            worker_id="wk_2",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        self.assertEqual(wrong_progress.status, HTTPStatus.FORBIDDEN)

        wrong_result = self.v1_result(
            job,
            {"status": "done", "attempt_id": "wk_2-1", "result_checksum": "bad", **audit_result_fields([])},
            headers={"Authorization": f"Bearer {other_token}"},
        )
        self.assertEqual(wrong_result.status, HTTPStatus.FORBIDDEN)

    def test_worker_v1_lease_claims_one_job_with_fixed_capacity(self) -> None:
        scan = {
            "id": "sc_no_slots",
            "repo": "acme/no-slots",
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "repoId": "repo_no_slots",
            "githubRepoId": "no_slots",
        }
        app.SCANS.append(scan)
        app.create_scan_job_for_scan(scan)

        claim = self.v1_lease(payload={"free_slots": 0})

        self.assertEqual(claim.status, HTTPStatus.OK)
        self.assertEqual(claim.payload["job"]["scan_id"], "sc_no_slots")
        self.assertEqual(claim.payload["lease"]["job_id"], claim.payload["job"]["job_id"])
        self.assertEqual(claim.payload["lease"]["run_id"], claim.payload["job"]["run_id"])
        self.assertEqual(scan["status"], "running")

    def test_worker_claim_waits_until_current_job_completes_before_next_claim(self) -> None:
        for index in range(1, 4):
            scan = {
                "id": f"sc_refill_{index}",
                "repo": f"acme/refill-{index}",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "userId": "usr_refill",
                "createdAt": app.now() + index,
                "queuedAt": app.now() + index,
                "progress": 0,
                "phase": None,
                "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                "repoId": f"repo_refill_{index}",
                "githubRepoId": f"refill_{index}",
            }
            app.SCANS.append(scan)
            app.create_scan_job_for_scan(scan)

        first_claim = self.v1_lease()

        self.assertEqual(first_claim.status, HTTPStatus.OK)
        self.assertEqual(first_claim.payload["job"]["scan_id"], "sc_refill_1")
        first_job = first_claim.payload["job"]

        blocked_claim = self.v1_lease()
        self.assertEqual(blocked_claim.status, HTTPStatus.OK)
        self.assertIsNone(blocked_claim.payload["job"])

        result = self.v1_result(
            first_job,
            {
                "status": "done",
                "attempt_id": f"wk_1-{first_job['attempt']}",
                "result_checksum": f"checksum-{first_job['job_id']}",
                **audit_result_fields([]),
                "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            },
        )
        self.assertEqual(result.status, HTTPStatus.OK)

        refill_claim = self.v1_lease()

        self.assertEqual(refill_claim.status, HTTPStatus.OK)
        self.assertEqual(refill_claim.payload["job"]["scan_id"], "sc_refill_2")

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
                "userId": "usr_multi",
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

        first_claim = self.v1_lease()
        second_claim = self.v1_lease("wk_2", headers=worker_two_auth)

        self.assertEqual(first_claim.status, HTTPStatus.OK)
        self.assertEqual(second_claim.status, HTTPStatus.OK)
        first_jobs = [first_claim.payload["job"]]
        second_jobs = [second_claim.payload["job"]]
        claimed_job_ids = [job["job_id"] for job in first_jobs + second_jobs]
        claimed_scan_ids = [job["scan_id"] for job in first_jobs + second_jobs]
        self.assertEqual(len(claimed_job_ids), 2)
        self.assertEqual(len(set(claimed_job_ids)), 2)
        self.assertEqual(claimed_scan_ids, ["sc_multi_1", "sc_multi_2"])
        self.assertEqual(app.SCANS[2]["status"], "queued")
        queue = app.scan_queue_payload(app.SCANS[2])
        self.assertEqual(queue["position"], 1)
        self.assertEqual(queue["ahead"], 0)

        for worker_id, auth, jobs in (("wk_1", self.auth, first_jobs), ("wk_2", worker_two_auth, second_jobs)):
            for job in jobs:
                progress = self.v1_event(
                    job,
                    phase="repo_map",
                    progress=80,
                    message=f"{worker_id} reviewing",
                    worker_id=worker_id,
                    headers=auth,
                )
                self.assertEqual(progress.status, HTTPStatus.OK)
                result = self.v1_result(
                    job,
                    {
                        "status": "done",
                        "attempt_id": f"{worker_id}-{job['attempt']}",
                        "result_checksum": f"checksum-{job['job_id']}",
                        **audit_result_fields(
                            [
                                audit_issue_card(
                                    f"Finding {job['scan_id']}",
                                    issue_id=f"issue-{job['scan_id']}",
                                    severity="P2",
                                )
                            ]
                        ),
                        "summary": {"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 0},
                    },
                    headers=auth,
                )
                self.assertEqual(result.status, HTTPStatus.OK)

        next_claim = self.v1_lease()

        self.assertEqual(next_claim.status, HTTPStatus.OK)
        self.assertEqual(next_claim.payload["job"]["scan_id"], "sc_multi_3")

        remaining_job = next_claim.payload["job"]
        for expected_scan_id in ("sc_multi_3", "sc_multi_4", "sc_multi_5"):
            self.assertEqual(remaining_job["scan_id"], expected_scan_id)
            final_result = self.v1_result(
                remaining_job,
                {
                    "status": "done",
                    "attempt_id": f"wk_1-{remaining_job['attempt']}",
                    "result_checksum": f"checksum-{remaining_job['job_id']}",
                    **audit_result_fields([]),
                    "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                },
            )
            self.assertEqual(final_result.status, HTTPStatus.OK)
            if expected_scan_id != "sc_multi_5":
                followup_claim = self.v1_lease()
                self.assertEqual(followup_claim.status, HTTPStatus.OK)
                remaining_job = followup_claim.payload["job"]

        self.assertEqual({scan["status"] for scan in app.SCANS}, {"done"})
        self.assertEqual(len(app.ISSUES), 2)

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
        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]

        scan["status"] = "cancelled"
        db.cancel_scan_job_for_scan(scan["id"])
        result = self.v1_result(
            job,
            {
                "status": "done",
                "attempt_id": "wk_1-1",
                "result_checksum": "checksum-cancelled",
                **audit_result_fields(
                    [audit_issue_card("Late result", issue_id="issue-late-result", severity="P1")]
                ),
            },
        )

        self.assertEqual(result.status, HTTPStatus.CONFLICT)
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "cancelled")
        self.assertEqual(app.SCANS[0]["status"], "cancelled")
        self.assertEqual(app.ISSUES, [])

    def test_cancelled_running_job_rejects_late_worker_progress(self) -> None:
        scan = {
            "id": "sc_cancel_progress",
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
        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]

        scan["status"] = "cancelled"
        db.cancel_scan_job_for_scan(scan["id"])
        progress = self.v1_event(job, phase="repo_map", progress=70, message="late update")

        self.assertEqual(progress.status, HTTPStatus.CONFLICT)
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "cancelled")
        self.assertEqual(db.get_scan_job(job["job_id"])["progress"], 0)
        self.assertEqual(app.SCANS[0]["status"], "cancelled")
        self.assertEqual(app.SCANS[0]["progress"], 0)

    def test_cancelled_claimed_job_does_not_block_same_worker_from_new_same_ref_scan(self) -> None:
        commit = "a" * 40
        first_scan = {
            "id": "sc_cancel_same_ref_first",
            "repo": "acme/api",
            "branch": "main",
            "commit": commit,
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS = [first_scan]
        first_job = app.create_scan_job_for_scan(first_scan)
        first_claim = self.v1_lease()
        self.assertEqual(first_claim.status, HTTPStatus.OK)
        self.assertEqual(first_claim.payload["job"]["scan_id"], first_scan["id"])

        first_scan["status"] = "cancelled"
        db.cancel_scan_job_for_scan(first_scan["id"])

        second_scan = {
            "id": "sc_cancel_same_ref_second",
            "repo": "acme/api",
            "branch": "main",
            "commit": commit,
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now() + 1,
            "queuedAt": app.now() + 1,
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS.append(second_scan)
        second_job = app.create_scan_job_for_scan(second_scan)

        second_claim = self.v1_lease()

        self.assertEqual(second_claim.status, HTTPStatus.OK)
        self.assertEqual(second_claim.payload["job"]["scan_id"], second_scan["id"])
        self.assertEqual(second_claim.payload["job"]["job_id"], second_job["job_id"])
        self.assertEqual(db.get_scan_job(first_job["job_id"])["status"], "cancelled")
        self.assertEqual(db.get_scan_job(second_job["job_id"])["claimed_by_worker_id"], "wk_1")

    def test_scan_read_reconciles_cancelled_job_when_state_is_stale(self) -> None:
        app.USERS = {"usr_1": {"id": "usr_1", "name": "Owner", "providers": []}}
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        scan = {
            "id": "sc_stale_cancel",
            "repo": "acme/api",
            "branch": "main",
            "commit": "pending",
            "status": "running",
            "userId": "usr_1",
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 35,
            "phase": "ai",
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS = [scan]
        job = app.create_scan_job_for_scan(scan)
        db.cancel_scan_job_for_scan(scan["id"])
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "cancelled")
        cookie = {"Cookie": "pw_session=ses_1"}

        detail = RouteHarness("/scans/sc_stale_cancel", headers=cookie)
        listing = RouteHarness("/scans", headers=cookie)
        app.PullwiseHandler.route(detail, "GET")
        app.PullwiseHandler.route(listing, "GET")

        self.assertEqual(detail.status, HTTPStatus.OK)
        self.assertEqual(detail.payload["status"], "cancelled")
        self.assertEqual(detail.payload["phase"], "")
        self.assertEqual(listing.status, HTTPStatus.OK)
        self.assertEqual(listing.payload["items"][0]["status"], "cancelled")
        self.assertEqual(app.SCANS[0]["status"], "cancelled")

    def test_scan_list_reconciles_running_job_progress_when_state_is_stale(self) -> None:
        app.USERS = {"usr_1": {"id": "usr_1", "name": "Owner", "providers": []}}
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        scan = {
            "id": "sc_stale_running",
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
        claimed = db.claim_next_scan_job("wk_1", timestamp=app.now())
        self.assertEqual(claimed["job_id"], job["job_id"])
        db.update_scan_job_progress(
            job["job_id"],
            {"phase": "ai", "progress": 70, "message": "reviewing"},
        )
        self.assertEqual(app.SCANS[0]["status"], "queued")

        listing = RouteHarness("/scans", headers={"Cookie": "pw_session=ses_1"})
        app.PullwiseHandler.route(listing, "GET")

        self.assertEqual(listing.status, HTTPStatus.OK)
        row = listing.payload["items"][0]
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["phase"], "ai")
        self.assertEqual(row["progress"], 70)
        self.assertEqual(app.SCANS[0]["status"], "running")

    def test_scan_list_isolates_rejected_worker_artifact_result_to_failed_row(self) -> None:
        app.USERS = {"usr_1": {"id": "usr_1", "name": "Owner", "providers": []}}
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        scan = {
            "id": "sc_bad_artifact_result",
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
        other_scan = {
            "id": "sc_other_done",
            "repo": "acme/other",
            "branch": "main",
            "commit": "def456",
            "status": "done",
            "userId": "usr_1",
            "createdAt": app.now() - 10,
            "completedAt": app.now() - 5,
            "progress": 100,
            "phase": "report",
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS = [scan, other_scan]
        app.create_scan_job_for_scan(scan)
        claim = self.v1_lease()
        claimed = claim.payload["job"]
        self.v1_event(claimed, phase="failure_handling", progress=80, message="Repository exceeds checkout limit")
        body = {
            "status": "failed",
            "attempt_id": "wk_1-1",
            "result_checksum": "checksum-bad-artifact-result",
            "error": "Repository exceeds checkout limit.",
            "error_code": "REPOSITORY_TOO_LARGE",
            **audit_result_fields([], execution_status="failed"),
        }
        artifact_manifest = body["reviewWorkerProtocol"]["artifact_manifest"]
        worker_log = next(item for item in artifact_manifest if item["artifact_id"] == "art_worker_log")
        worker_log["size_bytes"] = int(worker_log["size_bytes"]) + 1

        rejected = self.v1_result(claimed, body)
        self.assertEqual(rejected.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("Uploaded review artifacts do not match result manifest", rejected.payload["message"])

        listing = RouteHarness("/scans", headers={"Cookie": "pw_session=ses_1"})
        app.PullwiseHandler.route(listing, "GET")

        self.assertEqual(listing.status, HTTPStatus.OK)
        by_id = {item["id"]: item for item in listing.payload["items"]}
        self.assertEqual(by_id["sc_bad_artifact_result"]["status"], "failed")
        self.assertEqual(by_id["sc_bad_artifact_result"]["errorCode"], "WORKER_ARTIFACT_INVALID")
        self.assertIn("art_worker_log", by_id["sc_bad_artifact_result"]["error"])
    def test_scan_list_reconciles_completed_job_result_when_state_is_stale(self) -> None:
        app.USERS = {"usr_1": {"id": "usr_1", "name": "Owner", "providers": []}}
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        scan = {
            "id": "sc_stale_done",
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
        claim = self.v1_lease()
        claimed = claim.payload["job"]
        self.v1_event(claimed, phase="repo_map", progress=80, message="reviewing")
        result = self.v1_result(
            claimed,
            {
                "status": "done",
                "attempt_id": "wk_1-1",
                "result_checksum": "checksum-stale-done",
                **audit_result_fields(
                    [audit_issue_card("Completed finding", issue_id="issue-stale-done", severity="P1")]
                ),
                "summary": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
            },
        )
        self.assertEqual(result.status, HTTPStatus.OK)
        app.SCANS[0].update(
            {
                "status": "running",
                "phase": "ai",
                "progress": 80,
                "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            }
        )
        app.ISSUES = []

        listing = RouteHarness("/scans", headers={"Cookie": "pw_session=ses_1"})
        app.PullwiseHandler.route(listing, "GET")

        self.assertEqual(listing.status, HTTPStatus.OK)
        row = listing.payload["items"][0]
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["phase"], "report")
        self.assertEqual(row["progress"], 100)
        self.assertEqual(row["issues"]["high"], 1)
        self.assertEqual(app.SCANS[0]["status"], "done")
        self.assertEqual(len(app.ISSUES), 1)

    def test_worker_failed_result_caps_progress_below_complete(self) -> None:
        scan = {
            "id": "sc_failed_progress_cap",
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
        with patch.object(app.system_config, "scan_job_max_attempts", return_value=1):
            job = app.create_scan_job_for_scan(scan)
        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        claimed = claim.payload["job"]
        self.assertEqual(claimed["job_id"], job["job_id"])
        progress = self.v1_event(
            claimed,
            phase="final_report_json",
            progress=100,
            message="Uploading failed result",
        )
        self.assertEqual(progress.status, HTTPStatus.OK)

        result = self.v1_result(
            claimed,
            {
                "status": "failed",
                "attempt_id": "wk_1-1",
                "result_checksum": "checksum-failed-progress-cap",
                "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                "error": "Review worker completion gate failed.",
                "error_code": "CODEX_AUTH_REQUIRED",
                "errorCode": "CODEX_AUTH_REQUIRED",
                **audit_result_fields([], execution_status="failed"),
            },
        )

        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertEqual(app.SCANS[0]["status"], "failed")
        self.assertEqual(app.SCANS[0]["phase"], "failure_handling")
        self.assertEqual(app.SCANS[0]["progress"], app.INCOMPLETE_TERMINAL_SCAN_PROGRESS_MAX)
        self.assertEqual(app.scan_payload(app.SCANS[0])["progress"], app.INCOMPLETE_TERMINAL_SCAN_PROGRESS_MAX)
        self.assertEqual(app.scan_list_payload(app.SCANS[0])["progress"], app.INCOMPLETE_TERMINAL_SCAN_PROGRESS_MAX)

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
        claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]

        wrong_attempt = self.v1_result(
            job,
            {
                "status": "done",
                "attempt_id": "wk_1-99",
                "result_checksum": "checksum-wrong-attempt",
                **audit_result_fields(
                    [audit_issue_card("Wrong attempt", issue_id="issue-wrong-attempt", severity="P1")]
                ),
            },
        )
        self.assertEqual(wrong_attempt.status, HTTPStatus.CONFLICT)
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "claimed")
        self.assertEqual(app.SCANS[0]["status"], "running")
        self.assertEqual(app.ISSUES, [])

        current_attempt = self.v1_result(
            job,
            {
                "status": "done",
                "attempt_id": "wk_1-1",
                "result_checksum": "checksum-current-attempt",
                **audit_result_fields(
                    [audit_issue_card("Current attempt", issue_id="issue-current-attempt", severity="P1")]
                ),
            },
        )
        self.assertEqual(current_attempt.status, HTTPStatus.OK)
        self.assertTrue(current_attempt.payload["accepted"])
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "done")
        self.assertEqual(app.SCANS[0]["status"], "done")
        self.assertEqual(len(app.ISSUES), 1)

    def test_worker_heartbeat_renews_active_job_lease(self) -> None:
        timestamp = app.now()
        scan = {
            "id": "sc_active_lease",
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

        with patch("pullwise_server.app.now", return_value=timestamp):
            claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]
        lease_seconds = app.system_config.scan_job_lease_seconds()
        original_timeout_at = db.get_scan_job(job["job_id"])["timeout_at"]
        self.assertGreaterEqual(original_timeout_at, timestamp + lease_seconds)
        self.assertLessEqual(original_timeout_at, timestamp + lease_seconds + 1)

        heartbeat_at = original_timeout_at + 100
        with patch("pullwise_server.app.now", return_value=heartbeat_at):
            heartbeat = self.v1_heartbeat(status="busy", run_id=job["run_id"])
        self.assertEqual(heartbeat.status, HTTPStatus.OK)

        stored = db.get_scan_job(job["job_id"])
        self.assertEqual(stored["status"], "running")
        self.assertEqual(stored["claimed_by_worker_id"], "wk_1")
        self.assertGreater(stored["timeout_at"], original_timeout_at)
        self.assertEqual(stored["timeout_at"], heartbeat_at + lease_seconds)
        self.assertEqual(db.recover_expired_scan_jobs(heartbeat_at + 1), [])
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "running")

    def test_worker_v1_heartbeat_reports_cancelled_active_run(self) -> None:
        timestamp = app.now()
        scan = {
            "id": "sc_cancelled_active_heartbeat",
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
        with patch("pullwise_server.app.now", return_value=timestamp):
            claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]
        db.cancel_scan_job_for_scan(scan["id"])

        with patch("pullwise_server.app.now", return_value=timestamp + 1):
            heartbeat = self.v1_heartbeat(status="busy", run_id=job["run_id"])

        self.assertEqual(heartbeat.status, HTTPStatus.OK)
        self.assertEqual(heartbeat.payload["cancelled_job_ids"], [job["job_id"]])
        self.assertEqual(heartbeat.payload["cancelledJobIds"], [job["job_id"]])

    def test_cancelled_active_heartbeat_clears_worker_slot_and_allows_next_claim(self) -> None:
        timestamp = app.now()
        first_scan = {
            "id": "sc_cancelled_slot_first",
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
        app.SCANS = [first_scan]
        first_job = app.create_scan_job_for_scan(first_scan)
        with patch("pullwise_server.app.now", return_value=timestamp):
            first_claim = self.v1_lease()
        self.assertEqual(first_claim.status, HTTPStatus.OK)
        first_job = first_claim.payload["job"]
        db.cancel_scan_job_for_scan(first_scan["id"])

        with patch("pullwise_server.app.now", return_value=timestamp + 1):
            stale_heartbeat = self.v1_heartbeat(status="busy", run_id=first_job["run_id"])
        self.assertEqual(stale_heartbeat.status, HTTPStatus.OK)
        self.assertEqual(stale_heartbeat.payload["cancelled_job_ids"], [first_job["job_id"]])
        self.assertEqual(db.get_worker("wk_1")["running_jobs"], 0)

        second_scan = {
            "id": "sc_cancelled_slot_second",
            "repo": "acme/next",
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": "usr_1",
            "createdAt": timestamp + 2,
            "queuedAt": timestamp + 2,
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS.append(second_scan)
        second_job = app.create_scan_job_for_scan(second_scan)
        with patch("pullwise_server.app.now", return_value=timestamp + 2):
            second_claim = self.v1_lease()

        self.assertEqual(second_claim.status, HTTPStatus.OK)
        self.assertEqual(second_claim.payload["job"]["job_id"], second_job["job_id"])
        self.assertEqual(db.get_scan_job(first_job["job_id"])["status"], "cancelled")
        self.assertEqual(db.get_scan_job(second_job["job_id"])["claimed_by_worker_id"], "wk_1")

    def test_worker_heartbeat_requeues_unstarted_claim_missing_from_active_jobs(self) -> None:
        timestamp = app.now()
        self.create_registry_worker("wk_2")
        scan = {
            "id": "sc_startup_lost",
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

        with patch("pullwise_server.app.now", return_value=timestamp):
            claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]
        connection = db.connect()
        try:
            with connection:
                connection.execute(
                    "UPDATE scan_jobs SET claimed_at = ?, timeout_at = ? WHERE job_id = ?",
                    (timestamp, timestamp + 3600, job["job_id"]),
                )
        finally:
            connection.close()
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "claimed")

        with patch("pullwise_server.app.now", return_value=timestamp + 121):
            heartbeat = self.v1_heartbeat(status="idle")
        self.assertEqual(heartbeat.status, HTTPStatus.OK)

        stored = db.get_scan_job(job["job_id"])
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["claimed_by_worker_id"], "wk_1")
        self.assertEqual(stored["started_at"], None)
        self.assertEqual(stored["timeout_at"], None)
        self.assertEqual(stored["error"], "worker_job_startup_lost")
        self.assertEqual(app.SCANS[0]["status"], "failed")
        self.assertEqual(app.SCANS[0]["recoveryReason"], "worker_job_startup_lost")

    def test_worker_heartbeat_keeps_unstarted_claim_during_startup_grace(self) -> None:
        timestamp = app.now()
        scan = {
            "id": "sc_startup_grace",
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

        with patch("pullwise_server.app.now", return_value=timestamp):
            claim = self.v1_lease()
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]
        connection = db.connect()
        try:
            with connection:
                connection.execute(
                    "UPDATE scan_jobs SET claimed_at = ?, timeout_at = ? WHERE job_id = ?",
                    (timestamp, timestamp + 3600, job["job_id"]),
                )
        finally:
            connection.close()

        with patch("pullwise_server.app.now", return_value=timestamp + 119):
            heartbeat = self.v1_heartbeat(status="idle")
        self.assertEqual(heartbeat.status, HTTPStatus.OK)

        stored = db.get_scan_job(job["job_id"])
        self.assertEqual(stored["status"], "claimed")
        self.assertEqual(stored["claimed_by_worker_id"], "wk_1")

    def test_many_queued_scans_for_same_user_do_not_hit_user_limit(self) -> None:
        app.SCANS = [
            {
                "id": f"sc_existing_{index}",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "userId": "usr_1",
                "createdAt": app.now() + index,
                "queuedAt": app.now() + index,
            }
            for index in range(25)
        ]
        error = app.scan_queue_limit_error("usr_1")
        self.assertIsNone(error)

    def test_global_queue_limit_rejects_new_scan_before_job_creation(self) -> None:
        app.SCANS = [
            {
                "id": "sc_queued",
                "repo": "acme/api",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "userId": "usr_1",
                "createdAt": app.now(),
                "queuedAt": app.now(),
            }
        ]
        with patch.dict(os.environ, {"PULLWISE_MAX_QUEUED_SCANS_GLOBAL": "1"}, clear=False):
            error = app.scan_queue_limit_error("usr_2")
        self.assertIsNotNone(error)
        self.assertEqual(error[2], "QUEUE_FULL_GLOBAL")

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
            job = db.claim_next_scan_job(worker_id)
            with lock:
                if job:
                    claimed.append(job["job_id"])

        threads = [threading.Thread(target=claim, args=(f"wk_{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(claimed), 1)
        self.assertEqual(len(set(claimed)), 1)

    def test_concurrent_worker_artifact_uploads_store_each_run_independently(self) -> None:
        worker_tokens = {"wk_1": "worker-secret"}
        for index in range(2, 13):
            _worker, token = self.create_registry_worker(f"wk_{index}")
            worker_tokens[f"wk_{index}"] = token

        claimed_jobs: list[dict] = []
        for index, worker_id in enumerate(worker_tokens, start=1):
            self.create_claimable_scan_job(
                job_id=f"job_concurrent_upload_{index}",
                scan_id=f"sc_concurrent_upload_{index}",
                user_id="usr_1",
            )
            job = db.claim_next_scan_job(worker_id, ready_providers=["codex"], recover_before_claim=False)
            self.assertIsNotNone(job)
            claimed_jobs.append(job)

        barrier = threading.Barrier(len(claimed_jobs))
        outcomes: list[tuple[str, int | None, dict | None]] = []
        errors: list[str] = []
        lock = threading.Lock()

        def upload(job: dict) -> None:
            worker_id = str(job.get("claimed_by_worker_id") or "")
            run_id = app.scan_job_run_id(job)
            content = json.dumps(
                {
                    "run_id": run_id,
                    "worker_id": worker_id,
                    "padding": "x" * (128 * 1024),
                },
                sort_keys=True,
            ).encode("utf-8")
            artifact_id = f"art_concurrent_{worker_id}"
            payload = {
                "protocol_version": "review-worker-protocol/v1",
                "attempt_id": f"{worker_id}-{int(job.get('attempt') or 1)}",
                "run_id": run_id,
                "artifact": {
                    "artifact_id": artifact_id,
                    "kind": "report.agent",
                    "name": f"{artifact_id}.json",
                    "media_type": "application/json",
                    "schema_id": "codex-full-repo-review",
                    "schema_version": "v1",
                    "encoding": "utf-8",
                    "compression": "none",
                    "required": True,
                    "storage": {"type": "server_artifact", "url": f"/v1/review-runs/{run_id}/artifacts/{artifact_id}"},
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                },
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
            try:
                barrier.wait(timeout=5)
                request = RouteHarness(
                    f"/v1/review-runs/{run_id}/artifacts",
                    payload,
                    headers={"Authorization": f"Bearer {worker_tokens[worker_id]}"},
                )
                app.PullwiseHandler.route(request, "POST")
                with lock:
                    outcomes.append((run_id, request.status, request.payload))
            except Exception as exc:
                with lock:
                    errors.append(f"{worker_id}: {exc}")

        threads = [threading.Thread(target=upload, args=(job,)) for job in claimed_jobs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), len(claimed_jobs))
        self.assertTrue(all(status == HTTPStatus.OK for _run_id, status, _payload in outcomes))
        for job in claimed_jobs:
            worker_id = str(job.get("claimed_by_worker_id") or "")
            run_id = app.scan_job_run_id(job)
            artifact_id = f"art_concurrent_{worker_id}"
            stored = db.get_review_run_artifact(run_id, artifact_id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored["run_id"], run_id)
            self.assertEqual(stored["artifact_id"], artifact_id)
            self.assertEqual(stored["job_id"], job["job_id"])
            self.assertEqual(stored["attempt_id"], f"{worker_id}-{int(job.get('attempt') or 1)}")
            payload = json.loads(stored["payload_json"])
            content = base64.b64decode(payload["content_base64"])
            self.assertEqual(hashlib.sha256(content).hexdigest(), stored["sha256"])
            self.assertEqual(len(content), stored["size_bytes"])

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
        db.claim_next_scan_job("wk_1", lease_seconds=60, timestamp=timestamp - 120)

        recovered = db.recover_expired_scan_jobs(timestamp)
        stored = db.get_scan_job(job["job_id"])

        self.assertEqual(recovered[0]["status"], "failed")
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["error"], "timed_out")


if __name__ == "__main__":
    unittest.main()
