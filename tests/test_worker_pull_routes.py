from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
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


def audit_issue_card(
    title: str,
    *,
    issue_id: str = "issue-1",
    severity: str = "P2",
    category: str = "Quality",
    file: str = "src/app.py",
    line: int = 12,
    end_line: int | None = None,
    claim: str | None = None,
    impact: str = "",
    evidence: list | None = None,
    reproduction: dict | None = None,
    reproduction_idea: str = "",
    suggested_test: str = "",
    false_positive_checks: list[str] | None = None,
    limitations: list[str] | None = None,
) -> dict:
    return {
        "issue_id": issue_id,
        "shard_id": "app",
        "agent_role": "correctness-reviewer",
        "title": title,
        "category": category,
        "severity": severity,
        "confidence": 0.9,
        "locations": [{"file": file, "startLine": line, "endLine": end_line or line}] if file else [],
        "claim": claim or title,
        "impact": impact,
        "evidence": evidence if evidence is not None else ["Concrete evidence was captured."],
        "reproduction": reproduction or {},
        "reproduction_idea": reproduction_idea,
        "suggested_test": suggested_test,
        "false_positive_checks": (
            false_positive_checks if false_positive_checks is not None else ["No upstream guard was found."]
        ),
        "limitations": limitations or [],
    }


def audit_verification(
    issue_id: str,
    *,
    verdict: str = "confirmed",
    verifier_role: str = "prover",
    proof_type: str = "static_proof",
    proof_strength: int = 2,
    evidence: list[str] | None = None,
    commands_run: list[str] | None = None,
    result_summary: str = "Static proof confirms the candidate.",
    notes_for_fix: list[str] | None = None,
    log_path: str = "",
    output: str = "",
) -> dict:
    return {
        "issue_id": issue_id,
        "verifier_role": verifier_role,
        "verdict": verdict,
        "confidence": 0.86,
        "proof_type": proof_type,
        "proof_strength": proof_strength,
        "evidence": evidence or ["Verifier reviewed the relevant code path."],
        "commands_run": commands_run or [],
        "result_summary": result_summary,
        "notes_for_fix": notes_for_fix or [],
        "logPath": log_path,
        "output": output,
    }


def audit_result_fields(issue_cards: list[dict], verification_results: list[dict] | None = None) -> dict:
    return {
        "audit_protocol": "audit-swarm/0.1",
        "issue_cards": issue_cards,
        "verification_results": verification_results or [],
    }


def repository_graph_fixture() -> dict:
    return {
        "version": "repository-graph/0.1",
        "generatedAt": app.now(),
        "repo": "acme/api",
        "branch": "main",
        "commit": "abc123",
        "summary": "API graph",
        "stats": {"nodes": 3, "edges": 2, "files": 3, "languages": ["Python"], "truncated": False},
        "nodes": [
            {
                "id": "file:src/app.py",
                "label": "app.py",
                "type": "entrypoint",
                "path": "src/app.py",
                "importance": 0.9,
                "tags": ["backend"],
                "raw": "secret",
            },
            {"id": "dir:src", "label": "src", "type": "module", "path": "src"},
            {"id": "bad\nid", "label": "bad", "type": "unknown", "path": "C:\\worker\\repo\\bad.py"},
        ],
        "edges": [
            {"id": "e1", "source": "file:src/app.py", "target": "dir:src", "type": "imports", "weight": 2},
            {"id": "e2", "source": "bad\nid", "target": "x", "type": "unknown"},
        ],
        "architectureSummary": {
            "entrypoints": ["src/app.py"],
            "modules": ["src"],
            "reviewHints": ["Review request handlers."],
            "promptText": "Repository architecture: src/app.py handles requests.",
        },
        "semanticGraph": {
            "version": "semantic-code-graph/0.1",
            "summary": "API semantic graph",
            "stats": {
                "files": 1,
                "symbols": 3,
                "relationships": 1,
                "routes": 1,
                "source": "static",
                "truncated": False,
            },
            "nodes": [
                {
                    "id": "symbol:src/app.py:GET_/health",
                    "label": "GET /health",
                    "type": "route",
                    "path": "src/app.py",
                    "line": 5,
                    "signature": "GET /health",
                    "importance": 0.95,
                    "tags": ["route"],
                },
                {
                    "id": "symbol:src/app.py:health",
                    "label": "health",
                    "type": "function",
                    "path": "src/app.py",
                    "line": 6,
                    "signature": "health()",
                },
                {
                    "id": "symbol:bad",
                    "label": "bad",
                    "type": "function",
                    "path": "C:\\worker\\repo\\bad.py",
                },
            ],
            "edges": [
                {
                    "id": "handles:symbol:src/app.py:GET_/health->symbol:src/app.py:health",
                    "source": "symbol:src/app.py:GET_/health",
                    "target": "symbol:src/app.py:health",
                    "type": "handles",
                    "weight": 1,
                },
                {"id": "bad", "source": "symbol:bad", "target": "missing", "type": "calls"},
            ],
            "reviewHints": ["Start with API routes."],
        },
        "absolutePath": "C:\\worker\\repo",
    }


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
        db.upsert_worker_heartbeat(
            {
                "worker_id": "wk_1",
                "version": "0.1.0",
                "provider": "codex",
                "max_concurrent_jobs": 2,
                "running_jobs": 0,
                "free_slots": 2,
                "doctor_status": "ok",
                "codex_ready": 1,
                "timestamp": app.now(),
            }
        )
        self.auth = {"Authorization": "Bearer worker-secret"}

    def create_registry_worker(self, worker_id: str) -> tuple[dict, str]:
        worker = db.create_worker({"worker_id": worker_id, "name": worker_id, "provider": "codex"})
        db.upsert_worker_heartbeat(
            {
                "worker_id": worker_id,
                "version": "0.1.0",
                "provider": "codex",
                "max_concurrent_jobs": 2,
                "running_jobs": 0,
                "free_slots": 2,
                "doctor_status": "ok",
                "codex_ready": 1,
                "timestamp": app.now(),
            }
        )
        return worker, worker["worker_token"]

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

    def test_worker_result_findings_make_duplicate_issue_ids_unique(self) -> None:
        job = {
            "scan_id": "sc_1",
            "job_id": "job_1",
            "user_id": "usr_1",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc123",
        }
        app.ISSUES = [
            {
                "id": "issue-duplicate",
                "scanId": "sc_old",
                "jobId": "job_old",
                "userId": "usr_1",
                "status": "open",
            }
        ]

        findings = app.worker_audit_swarm_findings(
            job,
            audit_result_fields(
                [
                    audit_issue_card("First duplicate", issue_id="issue-duplicate", file="src/first.py"),
                    audit_issue_card("Second duplicate", issue_id="issue-duplicate", file="src/second.py"),
                ]
            ),
            reserved_ids=app.worker_issue_reserved_ids(job),
        )

        self.assertEqual([finding["id"] for finding in findings], ["issue-duplicate-2", "issue-duplicate-3"])

    def test_worker_audit_swarm_confirmed_verdict_without_evidence_is_not_static_proof(self) -> None:
        findings = app.worker_audit_swarm_findings(
            {
                "scan_id": "sc_1",
                "job_id": "job_1",
                "user_id": "usr_1",
                "repo": "acme/api",
                "branch": "main",
                "commit": "abc123",
            },
            audit_result_fields(
                [audit_issue_card("Unsupported verifier confirmation", issue_id="issue-unsupported")],
                [{"issue_id": "issue-unsupported", "verdict": "confirmed"}],
            ),
        )

        self.assertEqual(findings[0]["verificationStatus"], "potential_risk")

    def test_worker_audit_swarm_confirmed_verdict_with_only_proof_strength_is_not_static_proof(self) -> None:
        findings = app.worker_audit_swarm_findings(
            {
                "scan_id": "sc_1",
                "job_id": "job_1",
                "user_id": "usr_1",
                "repo": "acme/api",
                "branch": "main",
                "commit": "abc123",
            },
            audit_result_fields(
                [audit_issue_card("Proof strength only confirmation", issue_id="issue-proof-strength")],
                [{"issue_id": "issue-proof-strength", "verdict": "confirmed", "proof_strength": 3}],
            ),
        )

        self.assertEqual(findings[0]["verificationStatus"], "potential_risk")

    def test_public_repository_graph_sanitizes_worker_payload(self) -> None:
        payload = app.public_repository_graph(repository_graph_fixture())

        self.assertEqual(payload["version"], "repository-graph/0.1")
        self.assertEqual(payload["nodes"][0]["path"], "src/app.py")
        self.assertNotIn("raw", payload["nodes"][0])
        self.assertEqual(len(payload["nodes"]), 2)
        self.assertEqual(len(payload["edges"]), 1)
        serialized = json.dumps(payload)
        self.assertNotIn("C:\\worker", serialized)
        self.assertNotIn("secret", serialized)
        self.assertEqual(payload["architectureSummary"]["entrypoints"], ["src/app.py"])
        self.assertEqual(payload["semanticGraph"]["version"], "semantic-code-graph/0.1")
        self.assertEqual(len(payload["semanticGraph"]["nodes"]), 2)
        self.assertEqual(len(payload["semanticGraph"]["edges"]), 1)
        self.assertEqual(payload["semanticGraph"]["stats"]["source"], "static")

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
                "doctor_status": "ok",
                "codex_ready": True,
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
        calibration_context = claim.payload["job"]["review_calibration_context"]
        self.assertEqual(calibration_context["protocol"], "pullwise-review-calibration/0.2")
        self.assertEqual(calibration_context["scope_key"], "user:usr_1|repo:repo_123|branch:main")
        self.assertEqual(calibration_context["mode"], "shadow")
        self.assertEqual(calibration_context["rollout_policy"]["effective_mode"], "shadow")
        self.assertEqual(calibration_context["source_reliability"], {})
        self.assertEqual(app.SCANS[0]["status"], "running")
        self.assertEqual(app.SCANS[0]["claimedByWorkerId"], "wk_1")

        second_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_2"}, headers=self.auth)
        app.PullwiseHandler.route(second_claim, "POST")
        self.assertEqual(second_claim.status, HTTPStatus.FORBIDDEN)

        progress = RouteHarness(
            f"/worker/jobs/{job['job_id']}/progress",
            {
                "phase": "ai",
                "progress": 70,
                "message": "reviewing",
                "logs_summary": "ok",
                "repository_graph": repository_graph_fixture(),
                "audit_swarm": {
                    "protocol": "audit-swarm/0.1",
                    "stage": "discovery",
                    "adapter": "codex",
                    "summary": "Reviewer agents are discovering issue cards.",
                    "counts": {"issueCards": 2, "manifestCount": 1},
                    "roles": ["security-reviewer"],
                    "evidenceBlocks": [
                        {
                            "kind": "summary",
                            "title": "Discovery",
                            "summary": "Reviewer agents are discovering issue cards.",
                            "stage": "discovery",
                        }
                    ],
                },
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(progress, "POST")
        self.assertEqual(progress.status, HTTPStatus.OK)
        self.assertEqual(progress.payload["job"]["status"], "running")
        self.assertEqual(app.SCANS[0]["phase"], "ai")
        self.assertEqual(app.SCANS[0]["progress"], 70)
        running_scan_payload = app.scan_payload(app.SCANS[0])
        self.assertEqual(running_scan_payload["repositoryGraph"]["version"], "repository-graph/0.1")
        self.assertEqual(running_scan_payload["repositoryGraph"]["architectureSummary"]["entrypoints"], ["src/app.py"])
        self.assertEqual(running_scan_payload["auditSwarm"]["stage"], "discovery")
        self.assertEqual(running_scan_payload["auditSwarm"]["adapter"], "codex")
        self.assertEqual(running_scan_payload["auditSwarm"]["counts"]["issueCards"], 2)
        self.assertEqual(running_scan_payload["auditSwarm"]["counts"]["evidenceBlocks"], 1)
        self.assertEqual(running_scan_payload["auditSwarm"]["roles"], ["security-reviewer"])
        self.assertEqual(running_scan_payload["auditSwarm"]["evidenceBlocks"][0]["kind"], "summary")

        final_repository_graph = repository_graph_fixture()
        final_repository_graph["summary"] = "Final graph"
        final_repository_graph["architectureSummary"]["entrypoints"] = ["src/final.py"]
        result_body = {
            "status": "done",
            "attempt_id": "wk_1-1",
            **audit_result_fields(
                [
                    audit_issue_card(
                        "Hardcoded token",
                        issue_id="issue-hardcoded-token",
                        severity="P1",
                        file="app.py",
                        line=12,
                    )
                ]
            ),
            "summary": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
            "duration_ms": 1234,
            "ai_usage": {
                "model": "gpt-5.5",
                "input_tokens": 123,
                "output_tokens": 45,
                "total_tokens": 168,
            },
            "repository_graph": final_repository_graph,
            "result_checksum": "checksum-1",
        }
        result = RouteHarness(f"/worker/jobs/{job['job_id']}/result", result_body, headers=self.auth)
        app.PullwiseHandler.route(result, "POST")
        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertTrue(result.payload["accepted"])
        self.assertEqual(app.SCANS[0]["status"], "done")
        self.assertEqual(len(app.ISSUES), 1)
        final_scan_payload = app.scan_payload(app.SCANS[0])
        self.assertEqual(
            final_scan_payload["aiUsage"],
            {"model": "gpt-5.5"},
        )
        self.assertEqual(final_scan_payload["repositoryGraph"]["summary"], "Final graph")
        self.assertEqual(final_scan_payload["repositoryGraph"]["architectureSummary"]["entrypoints"], ["src/final.py"])
        self.assertEqual(final_scan_payload["auditSwarm"]["stage"], "report")
        self.assertEqual(final_scan_payload["auditSwarm"]["counts"]["issueCards"], 1)
        self.assertEqual(final_scan_payload["auditSwarm"]["issueCards"][0]["issueId"], "issue-hardcoded-token")
        self.assertEqual(final_scan_payload["auditSwarm"]["issueCards"][0]["claim"], "Hardcoded token")
        final_block_kinds = {block["kind"] for block in final_scan_payload["auditSwarm"]["evidenceBlocks"]}
        self.assertIn("claim", final_block_kinds)
        self.assertIn("code_location", final_block_kinds)

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

    def test_worker_result_persists_review_decision_events_idempotently_and_sanitizes_payload(self) -> None:
        scan = {
            "id": "sc_events",
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
        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)

        event = {
            "protocol": "pullwise-review-decision/0.1",
            "event_id": "evt_shadow_1",
            "candidate_observation_key": "obs_shadow_1",
            "candidate_id": "candidate-1",
            "fingerprint": "fp1",
            "source": "correctness-reviewer",
            "provider": "codex",
            "model": "gpt-5.5",
            "category": "correctness",
            "severity": "high",
            "verification_status": "potential_risk",
            "file_path": "src/app.py",
            "line_start": 12,
            "normalized_title": "hardcoded token",
            "raw_confidence": 0.9,
            "calibrated_confidence": 0.88,
            "decision_score": 0.83,
            "decision": "reported",
            "decision_reason": "passed_convergence_gate",
            "scoring_protocol": "pullwise-review-score/0.1",
            "score_factors": {
                "scoreKind": "ranking_score",
                "proposedDecision": "reported",
                "providerChain": "codex,opencode",
                "workerVersion": "0.3.5",
                "auditProtocol": "audit-swarm/0.1",
                "promptVersion": "pullwise-review-prompt/0.1",
                "verifierVersion": "pullwise-worker-verifier/0.1",
                "staticCheckerVersion": "pullwise-static-checker/0.1",
                "truthProbability": 0.83,
                "baseSha": "a" * 40,
                "headSha": "b" * 40,
                "rawSnippet": "secret code must not be stored",
            },
        }
        result_body = {
            "status": "done",
            "attempt_id": "wk_1-1",
            **audit_result_fields([audit_issue_card("Hardcoded token", issue_id="issue-hardcoded-token", severity="P1")]),
            "summary": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
            "review_decision_events": [event, {**event, "event_id": "bad_protocol", "protocol": "unknown"}],
            "result_checksum": "checksum-events",
        }
        result = RouteHarness(f"/worker/jobs/{job['job_id']}/result", result_body, headers=self.auth)
        app.PullwiseHandler.route(result, "POST")

        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertEqual(result.payload["reviewDecisionEvents"], {"inserted": 1, "duplicates": 0})
        rows = db.list_review_decision_events(job_id=job["job_id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_id"], "evt_shadow_1")
        self.assertEqual(rows[0]["user_id"], "usr_1")
        self.assertEqual(rows[0]["repo_id"], "repo_123")
        self.assertEqual(rows[0]["decision"], "reported")
        factors = json.loads(rows[0]["score_factors_json"])
        self.assertEqual(factors["scoreKind"], "ranking_score")
        self.assertEqual(factors["providerChain"], "codex,opencode")
        self.assertEqual(factors["workerVersion"], "0.3.5")
        self.assertEqual(factors["auditProtocol"], "audit-swarm/0.1")
        self.assertEqual(factors["promptVersion"], "pullwise-review-prompt/0.1")
        self.assertEqual(factors["truthProbability"], 0.83)
        self.assertEqual(factors["baseSha"], "a" * 40)
        self.assertEqual(factors["headSha"], "b" * 40)
        self.assertNotIn("rawSnippet", factors)
        evaluation = app.review_shadow_evaluation("user:usr_1|repo:repo_123|branch:main")
        self.assertEqual(evaluation["candidateCount"], 1)
        self.assertEqual(evaluation["currentReportedCount"], 1)
        self.assertEqual(evaluation["proposedReportedCount"], 1)
        self.assertEqual(evaluation["verifiedSuppressionCount"], 0)

        app.record_manual_review_outcome(
            event_id="evt_shadow_1",
            candidate_observation_key="obs_shadow_1",
            outcome_label="valid",
            reviewer_id="admin_1",
            reason="manual review confirmed the candidate",
        )
        labeled_evaluation = app.review_shadow_evaluation("user:usr_1|repo:repo_123|branch:main")
        self.assertEqual(labeled_evaluation["labeledOutcomeCount"], 1)
        self.assertEqual(labeled_evaluation["currentReportedLabeledCount"], 1)
        self.assertEqual(labeled_evaluation["currentFalsePositiveProxy"], 0.0)
        self.assertEqual(labeled_evaluation["proposedFalsePositiveProxy"], 0.0)
        self.assertEqual(labeled_evaluation["estimatedFalsePositiveReduction"], 0)
        labeled_gate = app.review_calibration_enforce_gate(labeled_evaluation)
        self.assertTrue(labeled_gate["hasLabeledShadowOutcomes"])
        self.assertTrue(labeled_gate["falsePositiveProxyNotIncreased"])
        snapshots = db.list_review_calibration_snapshots("user:usr_1|repo:repo_123|branch:main")
        snapshots_by_cohort = {item["cohort_key"]: item for item in snapshots}
        self.assertIn("source:correctness reviewer", snapshots_by_cohort)
        source_snapshot = snapshots_by_cohort["source:correctness reviewer"]
        self.assertGreater(source_snapshot["posterior_mean"], 0.60)
        source_buckets = json.loads(source_snapshot["confidence_buckets_json"])
        self.assertIn("0.90-0.95", source_buckets)

        next_scan = {
            "id": "sc_events_next",
            "repo": "acme/api",
            "branch": "main",
            "commit": "def456",
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now() + 1,
            "queuedAt": app.now() + 1,
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "repoId": "repo_123",
            "githubRepoId": "123",
        }
        app.SCANS.append(next_scan)
        app.create_scan_job_for_scan(next_scan)
        next_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(next_claim, "POST")
        self.assertEqual(next_claim.status, HTTPStatus.OK)
        context = next_claim.payload["job"]["review_calibration_context"]
        self.assertIn("source:correctness reviewer", context["source_reliability"])
        self.assertIn("provider:codex|model:gpt 5 5|source:correctness reviewer", context["source_reliability"])
        self.assertIn("global", context["confidence_calibration"])
        self.assertIn("0.90-0.95", context["confidence_calibration"]["global"])

        duplicate = RouteHarness(f"/worker/jobs/{job['job_id']}/result", result_body, headers=self.auth)
        app.PullwiseHandler.route(duplicate, "POST")
        self.assertEqual(duplicate.status, HTTPStatus.OK)
        self.assertTrue(duplicate.payload["duplicate"])
        self.assertEqual(len(db.list_review_decision_events(job_id=job["job_id"])), 1)

    def test_worker_result_exposes_safe_review_calibration_summary_on_issue_payload(self) -> None:
        scan = {
            "id": "sc_public_calibration",
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
        job = app.create_scan_job_for_scan(scan)
        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)

        result_body = {
            "status": "done",
            "attempt_id": "wk_1-1",
            "commit": "a" * 40,
            **audit_result_fields(
                [
                    {
                        **audit_issue_card("Calibrated issue", issue_id="issue-calibrated"),
                        "review_calibration": {
                            "protocol": "pullwise-review-calibration-public/0.1",
                            "decision": "reported",
                            "reason": "verified_or_static_proof_guardrail",
                            "scoreBand": "report_band",
                            "scoreKind": "ranking_score",
                            "verificationStatus": "static_proof",
                            "auditOnly": False,
                            "guardrailApplied": True,
                            "rawConfidence": 0.99,
                            "cohortKey": "source:secret",
                        },
                    }
                ]
            ),
            "summary": {"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 0},
            "result_checksum": "checksum-public-calibration",
        }
        result = RouteHarness(f"/worker/jobs/{job['job_id']}/result", result_body, headers=self.auth)
        app.PullwiseHandler.route(result, "POST")
        self.assertEqual(result.status, HTTPStatus.OK)

        payload = app.issue_payload(app.ISSUES[0])

        self.assertEqual(
            payload["reviewCalibration"],
            {
                "protocol": "pullwise-review-calibration-public/0.1",
                "decision": "reported",
                "reason": "verified_or_static_proof_guardrail",
                "scoreBand": "report_band",
                "scoreKind": "ranking_score",
                "verificationStatus": "static_proof",
                "auditOnly": False,
                "guardrailApplied": True,
            },
        )
        serialized = json.dumps(payload)
        self.assertNotIn("rawConfidence", serialized)
        self.assertNotIn("cohortKey", serialized)

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
            claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
            app.PullwiseHandler.route(claim, "POST")

        self.assertEqual(claim.status, HTTPStatus.OK)
        context = claim.payload["job"]["review_calibration_context"]
        self.assertEqual(context["rollout_policy"]["requested_mode"], "enforce")
        self.assertEqual(context["rollout_policy"]["effective_mode"], "shadow")
        self.assertEqual(context["mode"], "shadow")
        self.assertIn("missing_shadow_candidates", context["rollout_policy"]["enforce_gate"]["blockers"])

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
        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)

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

        result = RouteHarness(
            f"/worker/jobs/{job['job_id']}/result",
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
            headers=self.auth,
        )
        app.PullwiseHandler.route(result, "POST")

        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertEqual(result.payload["reviewDecisionEvents"], {"inserted": 2, "duplicates": 0})
        confirmed = db.list_review_outcome_labels("obs_worker_confirmed")
        rejected = db.list_review_outcome_labels("obs_worker_rejected")
        self.assertEqual(confirmed[0]["label_source"], "verifier_explicit")
        self.assertEqual(confirmed[0]["outcome_label"], "valid")
        self.assertEqual(rejected[0]["label_source"], "verifier_explicit")
        self.assertEqual(rejected[0]["outcome_label"], "false_positive")
        snapshots = db.list_review_calibration_snapshots("user:usr_1|repo:repo_123|branch:main")
        self.assertIn("global", {snapshot["cohort_key"] for snapshot in snapshots})

    def test_issue_status_updates_record_user_feedback_outcome_labels(self) -> None:
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
                "id": "sc_feedback",
                "repo": "acme/api",
                "branch": "main",
                "commit": "abc123",
                "status": "done",
                "userId": "usr_1",
                "createdAt": app.now(),
                "completedAt": app.now(),
                "issues": {"critical": 0, "high": 3, "medium": 0, "low": 0, "info": 0},
                "repoId": "repo_123",
                "githubRepoId": "123",
            }
        ]
        app.ISSUES = [
            {
                "id": "issue-fixed",
                "userId": "usr_1",
                "scanId": "sc_feedback",
                "jobId": "job_feedback",
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
                "id": "issue-fp",
                "userId": "usr_1",
                "scanId": "sc_feedback",
                "jobId": "job_feedback",
                "repo": "acme/api",
                "branch": "main",
                "status": "open",
                "severity": "high",
                "title": "False positive issue",
                "file": "src/app.py",
                "line": 22,
                "verificationStatus": "potential_risk",
            },
            {
                "id": "issue-duplicate",
                "userId": "usr_1",
                "scanId": "sc_feedback",
                "jobId": "job_feedback",
                "repo": "acme/api",
                "branch": "main",
                "status": "open",
                "severity": "high",
                "title": "Duplicate issue",
                "file": "src/app.py",
                "line": 32,
                "verificationStatus": "potential_risk",
            },
        ]
        db.record_review_decision_events(
            [
                {
                    "protocol": "pullwise-review-decision/0.1",
                    "event_id": "evt_status_fixed",
                    "candidate_observation_key": "obs_status_fixed",
                    "scan_id": "sc_feedback",
                    "job_id": "job_feedback",
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
                    "event_id": "evt_status_fp",
                    "candidate_observation_key": "obs_status_fp",
                    "scan_id": "sc_feedback",
                    "job_id": "job_feedback",
                    "attempt_id": "wk_1-1",
                    "user_id": "usr_1",
                    "repo_id": "repo_123",
                    "repo_full_name": "acme/api",
                    "branch": "main",
                    "candidate_id": "issue-fp",
                    "source": "correctness-reviewer",
                    "category": "correctness",
                    "severity": "high",
                    "verification_status": "potential_risk",
                    "file_path": "src/app.py",
                    "line_start": 22,
                    "raw_confidence": 0.92,
                    "normalized_title": "False positive issue",
                    "decision": "reported",
                    "scoring_protocol": "pullwise-review-score/0.1",
                },
                {
                    "protocol": "pullwise-review-decision/0.1",
                    "event_id": "evt_status_duplicate",
                    "candidate_observation_key": "obs_status_duplicate",
                    "scan_id": "sc_feedback",
                    "job_id": "job_feedback",
                    "attempt_id": "wk_1-1",
                    "user_id": "usr_1",
                    "repo_id": "repo_123",
                    "repo_full_name": "acme/api",
                    "branch": "main",
                    "candidate_id": "issue-duplicate",
                    "source": "correctness-reviewer",
                    "category": "correctness",
                    "severity": "high",
                    "verification_status": "potential_risk",
                    "file_path": "src/app.py",
                    "line_start": 32,
                    "raw_confidence": 0.92,
                    "normalized_title": "Duplicate issue",
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

        false_positive = RouteHarness(
            "/issues/issue-fp/status",
            {"falsePositive": True, "reason": "Not reachable in this repo."},
            headers=headers,
        )
        app.PullwiseHandler.route(false_positive, "PATCH")

        self.assertEqual(false_positive.status, HTTPStatus.OK)
        self.assertEqual(false_positive.payload["status"], "open")
        fp_labels = db.list_review_outcome_labels("obs_status_fp")
        self.assertEqual(fp_labels[0]["label_source"], "user_explicit")
        self.assertEqual(fp_labels[0]["outcome_label"], "false_positive")
        self.assertEqual(fp_labels[0]["label_reason"], "Not reachable in this repo.")

        duplicate = RouteHarness(
            "/issues/issue-duplicate/status",
            {"feedbackReason": "duplicate", "reason": "Duplicate issue."},
            headers=headers,
        )
        app.PullwiseHandler.route(duplicate, "PATCH")

        self.assertEqual(duplicate.status, HTTPStatus.OK)
        self.assertEqual(duplicate.payload["status"], "open")
        self.assertEqual(duplicate.payload["feedbackReason"], "duplicate")
        duplicate_labels = db.list_review_outcome_labels("obs_status_duplicate")
        self.assertEqual(duplicate_labels[0]["label_source"], "user_explicit")
        self.assertEqual(duplicate_labels[0]["outcome_label"], "ambiguous")
        self.assertEqual(duplicate_labels[0]["label_reason"], "feedback:duplicate - Duplicate issue.")

        next_scan = {
            "id": "sc_feedback_next",
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
        next_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(next_claim, "POST")

        self.assertEqual(next_claim.status, HTTPStatus.OK)
        context = next_claim.payload["job"]["review_calibration_context"]
        self.assertEqual(context["scope_key"], "user:usr_1|repo:repo_123|branch:main")
        self.assertAlmostEqual(context["effective_sample_counts"]["global"], 2.0, delta=0.01)
        self.assertAlmostEqual(context["source_reliability"]["global"]["effective_samples"], 2.0, delta=0.01)
        self.assertIn("0.90-0.95", context["confidence_calibration"]["global"])
        bucket = context["confidence_calibration"]["global"]["0.90-0.95"]
        self.assertAlmostEqual(bucket["positive_truth_weight"], 1.0, delta=0.01)
        self.assertAlmostEqual(bucket["labeled_weight"], 2.0, delta=0.01)
        self.assertAlmostEqual(
            bucket["bucket_precision"],
            (bucket["positive_truth_weight"] + 3.0) / (bucket["labeled_weight"] + 5.0),
        )

    def test_repeated_user_feedback_uses_latest_selection(self) -> None:
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
                "id": "sc_feedback_repeat",
                "repo": "acme/api",
                "branch": "main",
                "commit": "abc123",
                "status": "done",
                "userId": "usr_1",
                "createdAt": app.now(),
                "completedAt": app.now(),
                "issues": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
                "repoId": "repo_123",
                "githubRepoId": "123",
            }
        ]
        app.ISSUES = [
            {
                "id": "issue-repeat",
                "userId": "usr_1",
                "scanId": "sc_feedback_repeat",
                "jobId": "job_feedback_repeat",
                "repo": "acme/api",
                "branch": "main",
                "status": "open",
                "severity": "high",
                "title": "Repeat feedback issue",
                "file": "src/app.py",
                "line": 12,
            }
        ]
        db.record_review_decision_events(
            [
                {
                    "protocol": "pullwise-review-decision/0.1",
                    "event_id": "evt_status_repeat",
                    "candidate_observation_key": "obs_status_repeat",
                    "scan_id": "sc_feedback_repeat",
                    "job_id": "job_feedback_repeat",
                    "attempt_id": "wk_1-1",
                    "user_id": "usr_1",
                    "repo_id": "repo_123",
                    "repo_full_name": "acme/api",
                    "branch": "main",
                    "candidate_id": "issue-repeat",
                    "source": "correctness-reviewer",
                    "category": "correctness",
                    "severity": "high",
                    "verification_status": "potential_risk",
                    "file_path": "src/app.py",
                    "line_start": 12,
                    "raw_confidence": 0.92,
                    "normalized_title": "Repeat feedback issue",
                    "decision": "reported",
                    "scoring_protocol": "pullwise-review-score/0.1",
                }
            ]
        )
        headers = {"Cookie": "pw_session=ses_owner"}

        with patch.object(app, "now", return_value=123456):
            useful = RouteHarness(
                "/issues/issue-repeat/status",
                {
                    "feedbackReason": "useful",
                    "falsePositive": False,
                    "reason": "User marked issue useful / valid.",
                },
                headers=headers,
            )
            app.PullwiseHandler.route(useful, "PATCH")
            false_positive = RouteHarness(
                "/issues/issue-repeat/status",
                {
                    "feedbackReason": "false_positive",
                    "falsePositive": True,
                    "reason": "False positive.",
                },
                headers=headers,
            )
            app.PullwiseHandler.route(false_positive, "PATCH")

        self.assertEqual(useful.status, HTTPStatus.OK)
        self.assertEqual(useful.payload["feedbackReason"], "useful")
        self.assertEqual(false_positive.status, HTTPStatus.OK)
        self.assertEqual(false_positive.payload["status"], "open")
        self.assertEqual(false_positive.payload["feedbackReason"], "false_positive")
        labels = db.list_review_outcome_labels("obs_status_repeat")
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0]["label_source"], "user_explicit")
        self.assertEqual(labels[0]["outcome_label"], "false_positive")
        self.assertEqual(app.effective_review_outcome_label("obs_status_repeat")["outcome_label"], "false_positive")
        app.ISSUES[0].pop("feedbackReason", None)
        self.assertEqual(app.issue_payload(app.ISSUES[0])["feedbackReason"], "false_positive")

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
        gate = app.review_calibration_enforce_gate(evaluation)
        self.assertTrue(gate["canConsiderEnforce"])

    def test_review_calibration_snapshots_shrink_sparse_cohorts_toward_parent(self) -> None:
        def event(index: int, *, category: str, observation_key: str) -> dict:
            return {
                "protocol": "pullwise-review-decision/0.1",
                "event_id": f"evt_backoff_{index}",
                "candidate_observation_key": observation_key,
                "scan_id": "sc_backoff",
                "job_id": "job_backoff",
                "attempt_id": "wk_1-1",
                "user_id": "usr_1",
                "repo_id": "repo_123",
                "github_repo_id": "123",
                "repo_full_name": "acme/api",
                "branch": "main",
                "commit_sha": "a" * 40,
                "candidate_id": f"candidate-{index}",
                "fingerprint": f"fp-{index}",
                "source": "correctness reviewer",
                "provider": "codex",
                "model": "gpt-5.5",
                "category": category,
                "severity": "medium",
                "verification_status": "potential_risk",
                "file_path": "src/app.py",
                "line_start": 12,
                "raw_confidence": 0.9,
                "calibrated_confidence": 0.9,
                "decision_score": 0.8,
                "decision": "reported",
                "decision_reason": "test",
                "scoring_protocol": "pullwise-review-score/0.1",
                "score_factors": {"scoreKind": "ranking_score", "proposedDecision": "reported"},
                "created_at": app.now(),
            }

        events = [event(index, category="correctness", observation_key=f"obs_valid_{index}") for index in range(6)]
        events.append(event(99, category="docs", observation_key="obs_sparse_docs"))
        db.record_review_decision_events(events)
        for index in range(6):
            app.record_manual_review_outcome(
                event_id=f"evt_backoff_{index}",
                candidate_observation_key=f"obs_valid_{index}",
                outcome_label="valid",
                reviewer_id="admin_1",
            )
        app.record_manual_review_outcome(
            event_id="evt_backoff_99",
            candidate_observation_key="obs_sparse_docs",
            outcome_label="false_positive",
            reviewer_id="admin_1",
        )

        snapshots = {
            item["cohort_key"]: item
            for item in db.list_review_calibration_snapshots("user:usr_1|repo:repo_123|branch:main")
        }
        self.assertIn("provider:codex", snapshots)
        self.assertIn("provider:codex|model:gpt 5 5", snapshots)
        self.assertIn("provider:codex|model:gpt 5 5|source:correctness reviewer", snapshots)
        sparse = snapshots["source:correctness reviewer|category:docs|status:potential_risk"]
        metadata = json.loads(sparse["metadata_json"])
        self.assertEqual(metadata["parent_cohort_key"], "source:correctness reviewer|category:docs")
        self.assertLess(metadata["shrinkage_weight"], 0.1)
        self.assertGreater(sparse["posterior_mean"], metadata["raw_posterior_mean"])
        self.assertGreater(sparse["posterior_lb"], metadata["raw_posterior_lb"])
        provider_sparse = snapshots[
            "provider:codex|model:gpt 5 5|source:correctness reviewer|category:docs|status:potential_risk"
        ]
        provider_metadata = json.loads(provider_sparse["metadata_json"])
        self.assertEqual(
            provider_metadata["parent_cohort_key"],
            "provider:codex|model:gpt 5 5|source:correctness reviewer|category:docs",
        )
        self.assertLess(provider_metadata["shrinkage_weight"], 0.1)
        self.assertGreater(provider_sparse["posterior_mean"], provider_metadata["raw_posterior_mean"])
        self.assertGreater(provider_sparse["posterior_lb"], provider_metadata["raw_posterior_lb"])

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

    def test_duplicate_worker_result_with_same_checksum_does_not_reapply_body(self) -> None:
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
        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)

        first = RouteHarness(
            f"/worker/jobs/{job['job_id']}/result",
            {
                "status": "done",
                "attempt_id": "wk_1-1",
                "result_checksum": "same-worker-result",
                **audit_result_fields(
                    [audit_issue_card("First result", issue_id="issue-first", severity="P1")]
                ),
                "summary": {"critical": 1, "high": 0, "medium": 0, "low": 0, "info": 0},
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(first, "POST")
        self.assertEqual(first.status, HTTPStatus.OK)
        self.assertEqual([issue["id"] for issue in app.ISSUES], ["issue-first"])
        self.assertEqual(app.SCANS[0]["issues"]["critical"], 1)

        duplicate = RouteHarness(
            f"/worker/jobs/{job['job_id']}/result",
            {
                "status": "done",
                "attempt_id": "wk_1-1",
                "result_checksum": "same-worker-result",
                **audit_result_fields(
                    [audit_issue_card("Second result", issue_id="issue-second", severity="P2")]
                ),
                "summary": {"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 0},
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(duplicate, "POST")

        self.assertEqual(duplicate.status, HTTPStatus.OK)
        self.assertTrue(duplicate.payload["duplicate"])
        self.assertEqual([issue["id"] for issue in app.ISSUES], ["issue-first"])
        self.assertEqual(app.SCANS[0]["issues"]["critical"], 1)
        self.assertEqual(app.SCANS[0]["issues"]["medium"], 0)

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

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)

        result = RouteHarness(
            f"/worker/jobs/{job['job_id']}/result",
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
                "summary": {"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 0},
                "verification_audit": {
                    "candidateCount": 2,
                    "reportedCount": 1,
                    "rejectedCount": 1,
                    "verifiedCount": 1,
                    "rejectedReasons": [{"reason": "missing_evidence", "count": 1}],
                    "rejectedSamples": [
                        {
                            "reason": "missing_evidence",
                            "title": "Only a vague model guess",
                            "severity": "low",
                            "category": "Quality",
                            "file": "src/guess.py",
                            "line": 9,
                            "verificationStatus": "unverified",
                            "summary": "This unconfirmed text should not be exposed.",
                        }
                    ],
                    "summary": "2 candidates evaluated; 1 reported; 1 rejected before reporting.",
                },
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(result, "POST")

        self.assertEqual(result.status, HTTPStatus.OK)
        payload = app.issue_payload(app.ISSUES[0])
        self.assertEqual(payload["verificationStatus"], "verified")
        self.assertEqual(payload["confidenceLevel"], "high")
        self.assertEqual(payload["reproduction"]["commands"], ["pytest tests/repro/test_page_zero.py"])
        self.assertEqual(payload["affectedLocations"][0]["url"], "https://github.com/acme/api/blob/abc1234/src/app.py#L12-L14")
        self.assertEqual(payload["evidence"][0]["url"], "https://github.com/acme/api/blob/abc1234/src/app.py#L12-L14")
        self.assertEqual(payload["evidence"][1]["type"], "test")
        self.assertTrue(payload["evidence"][1]["outputRedacted"])
        self.assertNotIn("output", payload["evidence"][1])
        checklist = {item["label"]: item["met"] for item in payload["evidenceChecklist"]}
        self.assertTrue(checklist["Fixed commit"])
        self.assertTrue(checklist["Reproduction command"])
        self.assertTrue(checklist["Raw log or test"])
        self.assertEqual(payload["audit"]["commit"], "abc1234")
        self.assertEqual(
            payload["auditSwarm"],
            {
                "protocol": "audit-swarm/0.1",
                "shardId": "app",
                "agentRole": "correctness-reviewer",
                "verdict": "confirmed",
            },
        )
        self.assertEqual(payload["whyNotFalsePositive"], ["The parameter is read from the request query."])
        self.assertEqual(payload["limitations"], ["A production API gateway could reject page < 1 before the app."])
        trace = {stage["key"]: stage for stage in payload["evidenceTrace"]}
        self.assertEqual(set(trace), {"code", "path", "trigger", "runtime", "impact", "fix"})
        self.assertEqual(trace["code"]["status"], "present")
        self.assertIn("Affected code location: src/app.py:L12-L14", trace["code"]["items"])
        self.assertEqual(trace["path"]["status"], "present")
        self.assertIn("Reachability check: The parameter is read from the request query.", trace["path"]["items"])
        self.assertEqual(trace["trigger"]["status"], "present")
        self.assertIn("Input: GET /users?page=0", trace["trigger"]["items"])
        self.assertEqual(trace["runtime"]["status"], "present")
        self.assertIn("Observed result: 500 internal server error", trace["runtime"]["items"])
        self.assertEqual(trace["impact"]["status"], "present")
        self.assertIn("Impact: malformed input returns 500", trace["impact"]["items"])
        self.assertEqual(trace["fix"]["status"], "missing")
        self.assertIn("Finding is pinned to commit abc1234.", payload["reasoningBreakdown"]["facts"])
        self.assertIn(
            "Offset calculation: page is used without a lower bound.",
            payload["reasoningBreakdown"]["facts"],
        )
        self.assertIn(
            "Impact: malformed input returns 500",
            payload["reasoningBreakdown"]["inferences"],
        )
        self.assertIn(
            "After a fix, rerun the captured reproduction command and compare the expected and observed results.",
            payload["reasoningBreakdown"]["recommendations"],
        )
        scan_payload = app.scan_payload(app.SCANS[0])
        self.assertEqual(
            scan_payload["verification"],
            {"verified": 1, "static_proof": 0, "potential_risk": 0, "unverified": 0},
        )
        self.assertEqual(scan_payload["verificationAudit"]["candidateCount"], 2)
        self.assertEqual(scan_payload["verificationAudit"]["reportedCount"], 1)
        self.assertEqual(scan_payload["verificationAudit"]["rejectedCount"], 1)
        self.assertEqual(scan_payload["auditSwarm"]["protocol"], "audit-swarm/0.1")
        self.assertEqual(scan_payload["auditSwarm"]["stage"], "report")
        self.assertEqual(scan_payload["auditSwarm"]["counts"]["issueCards"], 1)
        self.assertEqual(scan_payload["auditSwarm"]["counts"]["verificationResults"], 1)
        self.assertEqual(scan_payload["auditSwarm"]["counts"]["candidateCount"], 2)
        self.assertEqual(scan_payload["auditSwarm"]["issueCards"][0]["claim"], "page=0 creates a negative offset.")
        self.assertEqual(scan_payload["auditSwarm"]["issueCards"][0]["evidence"][0], "page is used without a lower bound.")
        self.assertEqual(scan_payload["auditSwarm"]["issueCards"][0].get("suggestedTest", ""), "")
        self.assertEqual(
            scan_payload["auditSwarm"]["issueCards"][0]["falsePositiveChecks"],
            ["The parameter is read from the request query."],
        )
        self.assertEqual(scan_payload["auditSwarm"]["verificationResults"][0]["verdict"], "confirmed")
        self.assertEqual(
            scan_payload["auditSwarm"]["verificationResults"][0]["command"],
            "pytest tests/repro/test_page_zero.py",
        )
        self.assertEqual(scan_payload["auditSwarm"]["verificationResults"][0]["summary"], "500 internal server error")
        evidence_blocks = scan_payload["auditSwarm"]["evidenceBlocks"]
        evidence_block_kinds = {block["kind"] for block in evidence_blocks}
        self.assertIn("claim", evidence_block_kinds)
        self.assertIn("code_location", evidence_block_kinds)
        self.assertIn("false_positive_check", evidence_block_kinds)
        self.assertIn("verifier_verdict", evidence_block_kinds)
        self.assertIn("command", evidence_block_kinds)
        self.assertEqual(
            next(block for block in evidence_blocks if block["kind"] == "claim")["summary"],
            "page=0 creates a negative offset.",
        )
        self.assertEqual(
            next(block for block in evidence_blocks if block["kind"] == "code_location")["file"],
            "src/app.py",
        )
        self.assertEqual(
            next(block for block in evidence_blocks if block["kind"] == "command" and block.get("command"))["command"],
            "pytest tests/repro/test_page_zero.py",
        )
        self.assertEqual(
            scan_payload["verificationAudit"]["rejectedReasons"],
            [{"reason": "missing_evidence", "count": 1}],
        )
        self.assertEqual(
            scan_payload["verificationAudit"]["rejectedSamples"],
            [
                {
                    "reason": "missing_evidence",
                    "title": "Only a vague model guess",
                    "severity": "low",
                    "category": "Quality",
                    "file": "src/guess.py",
                    "line": 9,
                    "verificationStatus": "unverified",
                }
            ],
        )

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
                },
                "verificationAudit": {
                    "candidateCount": 3,
                    "reportedCount": 1,
                    "rejectedCount": 2,
                    "verifiedCount": 1,
                    "rejectedReasons": [{"reason": "missing_evidence", "count": 2}],
                    "rejectedSamples": [
                        {"reason": "missing_evidence", "title": "Only a vague model guess", "severity": "low"}
                    ],
                    "summary": "3 candidates evaluated; 1 reported; 2 rejected before reporting.",
                },
            }
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

        owner = RouteHarness(
            "/scans/sc_bundle/audit-bundle",
            headers={"Cookie": f"{app.SESSION_COOKIE}=ses_owner"},
        )
        app.PullwiseHandler.route(owner, "GET")

        self.assertEqual(owner.status, HTTPStatus.OK)
        self.assertEqual(owner.payload["kind"], "pullwise.audit_bundle")
        self.assertEqual(owner.payload["schemaVersion"], 1)
        self.assertEqual(owner.payload["scan"]["id"], "sc_bundle")
        self.assertEqual(owner.payload["preflight"]["verifier"]["runs"][0]["status"], "failed")
        self.assertEqual(
            owner.payload["verification"],
            {"verified": 1, "static_proof": 0, "potential_risk": 0, "unverified": 0},
        )
        self.assertEqual(owner.payload["verificationAudit"]["candidateCount"], 3)
        self.assertEqual(owner.payload["verificationAudit"]["reportedCount"], 1)
        self.assertEqual(owner.payload["verificationAudit"]["rejectedCount"], 2)
        self.assertEqual(
            owner.payload["verificationAudit"]["rejectedSamples"],
            [{"reason": "missing_evidence", "title": "Only a vague model guess", "severity": "low"}],
        )
        self.assertEqual(owner.payload["evidenceSummary"]["issueCount"], 1)
        self.assertEqual([issue["id"] for issue in owner.payload["issues"]], ["f_page_zero"])
        self.assertEqual(owner.payload["evidenceSummary"]["evidenceItemCount"], 2)
        self.assertEqual(owner.payload["evidenceSummary"]["reproductionCommandCount"], 1)
        self.assertEqual(owner.payload["evidenceSummary"]["logArtifactCount"], 0)
        self.assertEqual(
            owner.payload["reproductionCommands"],
            ["npm run test -- tests/repro/page-zero.test.js"],
        )
        self.assertEqual(owner.payload["issues"][0]["verificationStatus"], "verified")
        self.assertTrue(owner.payload["preflight"]["verifier"]["runs"][0]["outputRedacted"])
        self.assertNotIn("output", owner.payload["preflight"]["verifier"]["runs"][0])
        self.assertEqual(
            owner.payload["issues"][0]["affectedLocations"][0]["url"],
            "https://github.com/acme/api/blob/abc1234/src/users.js#L42-L45",
        )
        artifact_paths = [artifact["path"] for artifact in owner.payload["artifacts"]]
        self.assertEqual(
            artifact_paths,
            [
                "README.md",
                "report.md",
                "reproduction/commands.txt",
                "environment.json",
                "tool-versions.json",
                "audit.json",
                "patches/f_page_zero.diff",
                "issues/f_page_zero.md",
                "artifact-manifest.json",
            ],
        )
        self.assertNotIn("logs/verification/sc_bundle/test.log", artifact_paths)
        self.assertNotIn("repro.sh", artifact_paths)
        self.assertNotIn("Dockerfile", artifact_paths)
        self.assertNotIn("reproduction/issues/f_page_zero.sh", artifact_paths)
        self.assertIn("patches/f_page_zero.diff", artifact_paths)
        self.assertIn("issues/f_page_zero.md", artifact_paths)
        self.assertIn("artifact-manifest.json", artifact_paths)
        manifest_paths = [item["path"] for item in owner.payload["artifactManifest"]]
        self.assertEqual(manifest_paths, artifact_paths)
        artifacts = {artifact["path"]: artifact for artifact in owner.payload["artifacts"]}
        self.assertIn("reproduction/commands.txt as untrusted text", artifacts["README.md"]["content"])
        self.assertIn("Treat every command as untrusted input", artifacts["README.md"]["content"])
        self.assertIn("Verifier stdout/stderr is withheld", artifacts["README.md"]["content"])
        self.assertNotIn("PULLWISE_RUN_REPRO", artifacts["README.md"]["content"])
        self.assertNotIn("repro.sh", artifacts["README.md"]["content"])
        self.assertNotIn("docker run", artifacts["README.md"]["content"])
        self.assertNotIn("reproduction/issues/", artifacts["README.md"]["content"])
        self.assertIn("patches/", artifacts["README.md"]["content"])
        self.assertIn("tool-versions.json", artifacts["README.md"]["content"])
        self.assertIn("artifact-manifest.json", artifacts["README.md"]["content"])
        self.assertNotIn("Dockerfile", artifacts)
        self.assertNotIn("repro.sh", artifacts)
        self.assertIn("Verifier log artifacts: 0", artifacts["report.md"]["content"])
        self.assertIn(
            "Rejected sample: missing_evidence - Only a vague model guess",
            artifacts["report.md"]["content"],
        )
        self.assertIn(
            "# Untrusted reproduction commands captured by Pullwise.",
            artifacts["reproduction/commands.txt"]["content"],
        )
        self.assertIn(
            "# Review manually before copying any command into a shell.",
            artifacts["reproduction/commands.txt"]["content"],
        )
        self.assertIn(
            "npm run test -- tests/repro/page-zero.test.js",
            artifacts["reproduction/commands.txt"]["content"],
        )
        self.assertIn("# Pullwise suggested patch", artifacts["patches/f_page_zero.diff"]["content"])
        self.assertIn("--- a/src/users.js", artifacts["patches/f_page_zero.diff"]["content"])
        self.assertIn("-const offset = (page - 1) * limit", artifacts["patches/f_page_zero.diff"]["content"])
        self.assertIn("+const pageNumber = Math.max(1, page)", artifacts["patches/f_page_zero.diff"]["content"])
        self.assertIn('"verificationAudit"', artifacts["environment.json"]["content"])
        self.assertIn('"os": "Linux"', artifacts["environment.json"]["content"])
        self.assertIn('"pythonVersion": "3.12.3"', artifacts["environment.json"]["content"])
        self.assertIn('"tools"', artifacts["tool-versions.json"]["content"])
        self.assertIn('"git --version"', artifacts["tool-versions.json"]["content"])
        self.assertIn('"v22.21.0"', artifacts["tool-versions.json"]["content"])
        self.assertIn('"selfExcluded": true', artifacts["artifact-manifest.json"]["content"])
        self.assertIn('"README.md"', artifacts["artifact-manifest.json"]["content"])
        self.assertNotIn('"Dockerfile"', artifacts["artifact-manifest.json"]["content"])
        self.assertNotIn('"repro.sh"', artifacts["artifact-manifest.json"]["content"])
        self.assertNotIn('"reproduction/issues/f_page_zero.sh"', artifacts["artifact-manifest.json"]["content"])
        self.assertIn('"patches/f_page_zero.diff"', artifacts["artifact-manifest.json"]["content"])
        self.assertIn('"tool-versions.json"', artifacts["artifact-manifest.json"]["content"])
        self.assertNotIn('"artifact-manifest.json"', artifacts["artifact-manifest.json"]["content"])
        self.assertIn("page=0 returns 500", artifacts["issues/f_page_zero.md"]["content"])
        self.assertIn("## Confidence Evidence", artifacts["issues/f_page_zero.md"]["content"])
        self.assertIn("Fixed commit: met", artifacts["issues/f_page_zero.md"]["content"])
        self.assertIn("Reproduction command: met", artifacts["issues/f_page_zero.md"]["content"])
        self.assertIn("Runtime output: met", artifacts["issues/f_page_zero.md"]["content"])
        self.assertIn("## Evidence Trace", artifacts["issues/f_page_zero.md"]["content"])
        self.assertIn("Code [present]", artifacts["issues/f_page_zero.md"]["content"])
        self.assertIn("Path [present]", artifacts["issues/f_page_zero.md"]["content"])
        self.assertIn("Runtime [present]", artifacts["issues/f_page_zero.md"]["content"])
        self.assertIn("Fix [present]", artifacts["issues/f_page_zero.md"]["content"])
        self.assertIn("## Facts, Inferences, and Recommendations", artifacts["issues/f_page_zero.md"]["content"])
        self.assertIn("### Facts", artifacts["issues/f_page_zero.md"]["content"])
        self.assertIn("Offset calculation: page is used without a lower bound.", artifacts["issues/f_page_zero.md"]["content"])
        self.assertIn("### Recommendations", artifacts["issues/f_page_zero.md"]["content"])
        self.assertIn("Inspect the suggested patch evidence", artifacts["issues/f_page_zero.md"]["content"])
        self.assertIn("../patches/f_page_zero.diff", artifacts["issues/f_page_zero.md"]["content"])
        self.assertNotIn("AssertionError: expected 400 received 500", artifacts["issues/f_page_zero.md"]["content"])
        self.assertIn("Worker log: logs/f_page_zero.log", artifacts["issues/f_page_zero.md"]["content"])
        self.assertRegex(artifacts["README.md"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("Verifier stdout/stderr is not embedded", owner.payload["limitations"][1])
        self.assertIn("untrusted text", owner.payload["limitations"][2])

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
            self.assertIn("README.md", archive.namelist())
            self.assertNotIn("repro.sh", archive.namelist())
            self.assertNotIn("Dockerfile", archive.namelist())
            self.assertIn("environment.json", archive.namelist())
            self.assertIn("tool-versions.json", archive.namelist())
            self.assertIn("artifact-manifest.json", archive.namelist())
            self.assertNotIn("reproduction/issues/f_page_zero.sh", archive.namelist())
            self.assertIn("patches/f_page_zero.diff", archive.namelist())
            self.assertIn("issues/f_page_zero.md", archive.namelist())
            self.assertNotIn("logs/verification/sc_bundle/test.log", archive.namelist())
            self.assertIn("untrusted text", archive.read("README.md").decode("utf-8"))
            self.assertNotIn("PULLWISE_RUN_REPRO", archive.read("README.md").decode("utf-8"))
            self.assertIn(
                "npm run test -- tests/repro/page-zero.test.js",
                archive.read("reproduction/commands.txt").decode("utf-8"),
            )
            self.assertIn(
                "+const pageNumber = Math.max(1, page)",
                archive.read("patches/f_page_zero.diff").decode("utf-8"),
            )
            self.assertIn(
                '"node --version"',
                archive.read("tool-versions.json").decode("utf-8"),
            )
            self.assertIn(
                '"selfExcluded": true',
                archive.read("artifact-manifest.json").decode("utf-8"),
            )
            self.assertNotIn(
                "AssertionError: expected 400 received 500",
                archive.read("issues/f_page_zero.md").decode("utf-8"),
            )

        other_user = RouteHarness(
            "/scans/sc_bundle/audit-bundle",
            headers={"Cookie": f"{app.SESSION_COOKIE}=ses_other"},
        )
        app.PullwiseHandler.route(other_user, "GET")
        self.assertEqual(other_user.status, HTTPStatus.NOT_FOUND)

        anonymous = RouteHarness("/scans/sc_bundle/audit-bundle")
        app.PullwiseHandler.route(anonymous, "GET")
        self.assertEqual(anonymous.status, HTTPStatus.UNAUTHORIZED)

    def test_scan_audit_bundle_includes_repository_graph(self) -> None:
        scan = self.audit_bundle_cache_fixture()
        scan["repositoryGraph"] = app.public_repository_graph(repository_graph_fixture())

        owner = RouteHarness(
            "/scans/sc_cache/audit-bundle",
            headers={"Cookie": f"{app.SESSION_COOKIE}=ses_owner"},
        )
        app.PullwiseHandler.route(owner, "GET")

        self.assertEqual(owner.status, HTTPStatus.OK)
        self.assertEqual(owner.payload["repositoryGraph"]["version"], "repository-graph/0.1")
        archive = app.scan_audit_bundle_zip_bytes(scan)
        with zipfile.ZipFile(io.BytesIO(archive), "r") as bundle:
            self.assertIn("repository-graph.json", bundle.namelist())
            graph = json.loads(bundle.read("repository-graph.json").decode("utf-8"))
        self.assertEqual(graph["version"], "repository-graph/0.1")

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
        self.assertEqual(scan_payload["verificationAudit"]["candidateCount"], 1)
        self.assertEqual(scan_payload["verificationAudit"]["reportedCount"], 1)
        self.assertEqual(scan_payload["verificationAudit"]["downgradedCount"], 1)

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
        self.assertEqual(scan_payload["verificationAudit"]["downgradedCount"], 1)

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

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)

        result = RouteHarness(
            f"/worker/jobs/{job['job_id']}/result",
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
                    "providerChain": ["codex"],
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
                "verification_audit": {
                    "candidate_count": 4,
                    "reported_count": 0,
                    "rejected_count": 2,
                    "downgraded_count": 1,
                    "verified_count": 0,
                    "static_proof_count": 0,
                    "potential_risk_count": 0,
                    "unverified_count": 0,
                    "rejectedReasons": [
                        {"reason": "missing_evidence", "count": 2},
                        {"reason": "", "count": 99},
                    ],
                    "summary": "4 candidates evaluated.\n2 rejected.",
                },
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(result, "POST")

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
        self.assertEqual(payload["verificationAudit"]["candidateCount"], 4)
        self.assertEqual(payload["verificationAudit"]["reportedCount"], 0)
        self.assertEqual(payload["verificationAudit"]["rejectedCount"], 2)
        self.assertEqual(payload["verificationAudit"]["downgradedCount"], 1)
        self.assertEqual(
            payload["verificationAudit"]["rejectedReasons"],
            [{"reason": "missing_evidence", "count": 2}],
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

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)
        claimed_job = claim.payload["job"]
        self.assertEqual(claimed_job["scan_id"], "sc_repo_limit_0")

        result = RouteHarness(
            f"/worker/jobs/{claimed_job['job_id']}/result",
            {
                "status": "failed",
                "attempt_id": f"wk_1-{claimed_job['attempt']}",
                "result_checksum": "checksum-repository-too-large",
                "error": "Repository is too large for Pullwise scanning.",
                "error_code": "REPOSITORY_TOO_LARGE",
                **audit_result_fields([]),
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
            headers=self.auth,
        )
        app.PullwiseHandler.route(result, "POST")

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

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)

        result = RouteHarness(
            f"/worker/jobs/{job['job_id']}/result",
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
            headers=self.auth,
        )
        app.PullwiseHandler.route(result, "POST")

        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertEqual(app.SCANS[0]["commit"], resolved_commit)
        self.assertEqual(db.get_scan_job(job["job_id"])["commit"], resolved_commit)
        self.assertEqual(app.ISSUES[0]["commit"], resolved_commit)
        payload = app.issue_payload(app.ISSUES[0])
        self.assertEqual(payload["verificationStatus"], "verified")
        self.assertEqual(payload["audit"]["commit"], resolved_commit)
        self.assertIn(f"/blob/{resolved_commit}/src/app.py#L12", payload["affectedLocations"][0]["url"])
        self.assertTrue(payload["evidence"][1]["outputRedacted"])
        self.assertNotIn("output", payload["evidence"][1])

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

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")

        self.assertEqual(claim.status, HTTPStatus.OK)
        self.assertEqual(claim.payload["job"]["review_output_language"], "ja")
        self.assertEqual(claim.payload["job"]["review_output_language_label"], "Japanese")

    def test_claim_payload_includes_previous_convergence_context_across_workers(self) -> None:
        _worker_two, worker_two_token = self.create_registry_worker("wk_2")
        worker_two_auth = {"Authorization": f"Bearer {worker_two_token}"}
        first_commit = "a" * 40
        second_commit = "b" * 40
        first_scan = {
            "id": "sc_converge_first",
            "repo": "acme/api",
            "branch": "main",
            "commit": first_commit,
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
        first_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(first_claim, "POST")
        self.assertEqual(first_claim.status, HTTPStatus.OK)

        first_result = RouteHarness(
            f"/worker/jobs/{first_job['job_id']}/result",
            {
                "status": "done",
                "attempt_id": "wk_1-1",
                "result_checksum": "checksum-converge-first",
                **audit_result_fields(
                    [
                        audit_issue_card(
                            "Old bug",
                            issue_id="issue-old",
                            severity="P1",
                            file="src/app.py",
                            line=12,
                        )
                    ]
                ),
                "summary": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
                "convergence_state": {
                    "protocol": "pullwise-convergence/0.1",
                    "scope_key": "repo:acme/api|branch:main",
                    "head_sha": first_commit,
                    "open_findings": [
                        {
                            "fingerprint": "fp-old",
                            "issue_id": "issue-old",
                            "title": "Old bug",
                            "file": "src/app.py",
                            "line": 12,
                            "confidence": 0.93,
                            "source": "correctness-reviewer",
                            "status": "open",
                        }
                    ],
                    "resolved_fingerprints": [],
                    "source_stats": {
                        "correctness-reviewer": {"reported": 1, "confirmed": 1, "resolved": 0, "rejected": 0}
                    },
                },
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(first_result, "POST")
        self.assertEqual(first_result.status, HTTPStatus.OK)
        self.assertEqual(app.SCANS[0]["convergenceState"]["headSha"], first_commit)

        second_scan = {
            "id": "sc_converge_second",
            "repo": "acme/api",
            "branch": "main",
            "commit": second_commit,
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now() + 1,
            "queuedAt": app.now() + 1,
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS.append(second_scan)
        app.create_scan_job_for_scan(second_scan)

        second_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_2"}, headers=worker_two_auth)
        app.PullwiseHandler.route(second_claim, "POST")

        self.assertEqual(second_claim.status, HTTPStatus.OK)
        context = second_claim.payload["job"]["convergence_context"]
        self.assertEqual(context["protocol"], "pullwise-convergence/0.1")
        self.assertEqual(context["scope_key"], "repo:acme/api|branch:main")
        self.assertEqual(context["previous_head_sha"], first_commit)
        self.assertEqual(context["open_findings"][0]["fingerprint"], "fp-old")
        self.assertEqual(context["source_stats"]["correctness-reviewer"]["confirmed"], 1)

    def test_claim_payload_matches_convergence_context_by_canonical_scope(self) -> None:
        _worker_two, worker_two_token = self.create_registry_worker("wk_2")
        worker_two_auth = {"Authorization": f"Bearer {worker_two_token}"}
        first_commit = "a" * 40
        second_commit = "b" * 40
        first_scan = {
            "id": "sc_converge_case_first",
            "repo": "Acme/API",
            "branch": "Main",
            "commit": first_commit,
            "status": "done",
            "userId": "usr_1",
            "createdAt": app.now(),
            "completedAt": app.now(),
            "issues": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
            "convergenceState": {
                "protocol": "pullwise-convergence/0.1",
                "scopeKey": "repo:acme/api|branch:main",
                "headSha": first_commit,
                "openFindings": [
                    {
                        "fingerprint": "fp-case",
                        "issue_id": "issue-case",
                        "title": "Case-stable bug",
                        "file": "src/app.py",
                        "line": 12,
                        "confidence": 0.93,
                        "source": "correctness-reviewer",
                        "status": "open",
                    }
                ],
                "resolvedFingerprints": [],
                "sourceStats": {
                    "correctness-reviewer": {"reported": 1, "confirmed": 1, "resolved": 0, "rejected": 0}
                },
            },
        }
        second_scan = {
            "id": "sc_converge_case_second",
            "repo": "acme/api",
            "branch": "main",
            "commit": second_commit,
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now() + 1,
            "queuedAt": app.now() + 1,
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS = [first_scan, second_scan]
        app.create_scan_job_for_scan(second_scan)

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_2"}, headers=worker_two_auth)
        app.PullwiseHandler.route(claim, "POST")

        self.assertEqual(claim.status, HTTPStatus.OK)
        context = claim.payload["job"]["convergence_context"]
        self.assertEqual(context["scope_key"], "repo:acme/api|branch:main")
        self.assertEqual(context["previous_head_sha"], first_commit)
        self.assertEqual(context["open_findings"][0]["fingerprint"], "fp-case")

    def test_worker_result_ignores_convergence_state_for_different_scope(self) -> None:
        first_commit = "a" * 40
        scan = {
            "id": "sc_wrong_scope",
            "repo": "acme/api",
            "branch": "main",
            "commit": first_commit,
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

        result = RouteHarness(
            f"/worker/jobs/{job['job_id']}/result",
            {
                "status": "done",
                "attempt_id": "wk_1-1",
                "result_checksum": "checksum-wrong-scope",
                **audit_result_fields([]),
                "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                "convergence_state": {
                    "protocol": "pullwise-convergence/0.1",
                    "scope_key": "repo:acme/other|branch:main",
                    "head_sha": first_commit,
                    "open_findings": [
                        {
                            "fingerprint": "fp-other",
                            "title": "Other repo bug",
                            "file": "src/app.py",
                            "status": "open",
                        }
                    ],
                    "resolved_fingerprints": [],
                    "source_stats": {},
                },
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(result, "POST")

        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertNotIn("convergenceState", app.SCANS[0])

        next_scan = {
            "id": "sc_wrong_scope_next",
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now() + 1,
            "queuedAt": app.now() + 1,
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS.append(next_scan)
        app.create_scan_job_for_scan(next_scan)
        next_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(next_claim, "POST")

        self.assertEqual(next_claim.status, HTTPStatus.OK)
        self.assertNotIn("convergence_context", next_claim.payload["job"])

    def test_claim_payload_does_not_resurrect_stale_convergence_context(self) -> None:
        old_done = {
            "id": "sc_old_convergence",
            "repo": "acme/api",
            "branch": "main",
            "commit": "a" * 40,
            "status": "done",
            "userId": "usr_1",
            "createdAt": app.now(),
            "completedAt": app.now(),
            "issues": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
            "convergenceState": {
                "protocol": "pullwise-convergence/0.1",
                "scopeKey": "repo:acme/api|branch:main",
                "headSha": "a" * 40,
                "openFindings": [
                    {
                        "fingerprint": "fp-stale",
                        "issue_id": "issue-stale",
                        "title": "Stale bug",
                        "file": "src/app.py",
                        "status": "open",
                    }
                ],
                "resolvedFingerprints": [],
                "sourceStats": {},
            },
        }
        newer_done_without_state = {
            "id": "sc_new_without_state",
            "repo": "acme/api",
            "branch": "main",
            "commit": "b" * 40,
            "status": "done",
            "userId": "usr_1",
            "createdAt": app.now() + 1,
            "completedAt": app.now() + 1,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        queued = {
            "id": "sc_after_missing_state",
            "repo": "acme/api",
            "branch": "main",
            "commit": "c" * 40,
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now() + 2,
            "queuedAt": app.now() + 2,
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        app.SCANS = [old_done, newer_done_without_state, queued]
        app.create_scan_job_for_scan(queued)

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")

        self.assertEqual(claim.status, HTTPStatus.OK)
        self.assertNotIn("convergence_context", claim.payload["job"])

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

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)

        result = RouteHarness(
            f"/worker/jobs/{job['job_id']}/result",
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
            headers=self.auth,
        )
        app.PullwiseHandler.route(result, "POST")

        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertEqual(app.ISSUES[0]["file"], "src/app.py")

        app.ISSUES[0]["file"] = f"/var/lib/pullwise-worker/checkouts/{job['job_id']}/src/app.py"
        self.assertEqual(app.issue_payload(app.ISSUES[0])["file"], "src/app.py")

        app.ISSUES[0]["file"] = "/var/log/pullwise/server.log"
        self.assertEqual(app.issue_payload(app.ISSUES[0])["file"], "")

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
            claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
            app.PullwiseHandler.route(claim, "POST")

        self.assertEqual(claim.status, HTTPStatus.SERVICE_UNAVAILABLE)
        stored_job = db.get_scan_job(job["job_id"])
        self.assertEqual(stored_job["status"], "queued")
        self.assertIsNone(stored_job["claimed_by_worker_id"])
        self.assertEqual(app.SCANS[0]["status"], "queued")
        self.assertNotIn("claimedByWorkerId", app.SCANS[0])

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
            {"status": "done", "attempt_id": "wk_2-1", "result_checksum": "bad", **audit_result_fields([])},
            headers={"Authorization": f"Bearer {other_token}"},
        )
        app.PullwiseHandler.route(wrong_result, "POST")
        self.assertEqual(wrong_result.status, HTTPStatus.FORBIDDEN)

    def test_worker_can_claim_multiple_jobs_up_to_capacity_and_limits(self) -> None:
        db.upsert_worker_heartbeat(
            {
                "worker_id": "wk_1",
                "version": "0.1.0",
                "provider": "codex",
                "max_concurrent_jobs": 3,
                "running_jobs": 0,
                "free_slots": 3,
                "doctor_status": "ok",
                "codex_ready": 1,
                "timestamp": app.now(),
            }
        )
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
            {"PULLWISE_MAX_RUNNING_SCANS_PER_USER": "1"},
            clear=False,
        ):
            app.PullwiseHandler.route(claim, "POST")

        self.assertEqual(claim.status, HTTPStatus.OK)
        self.assertEqual([job["scan_id"] for job in claim.payload["jobs"]], ["sc_1", "sc_2", "sc_3"])
        self.assertEqual(claim.payload["jobs"][0]["status"], "claimed")

    def test_worker_claim_uses_worker_slots_for_global_capacity_and_keeps_user_fairness(self) -> None:
        db.upsert_worker_heartbeat(
            {
                "worker_id": "wk_1",
                "version": "0.1.0",
                "provider": "codex",
                "max_concurrent_jobs": 3,
                "running_jobs": 0,
                "free_slots": 3,
                "doctor_status": "ok",
                "codex_ready": 1,
                "timestamp": app.now(),
            }
        )
        for index, user_id in enumerate(["usr_same", "usr_same", "usr_other", "usr_third"], start=1):
            scan = {
                "id": f"sc_slots_{index}",
                "repo": f"acme/slots-{index}",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "userId": user_id,
                "createdAt": app.now() + index,
                "queuedAt": app.now() + index,
                "progress": 0,
                "phase": None,
                "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                "repoId": f"repo_slots_{index}",
                "githubRepoId": f"slots_{index}",
            }
            app.SCANS.append(scan)
            app.create_scan_job_for_scan(scan)

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1", "max_jobs": 3}, headers=self.auth)
        with patch.dict(
            os.environ,
            {"PULLWISE_MAX_RUNNING_SCANS_GLOBAL": "1", "PULLWISE_MAX_RUNNING_SCANS_PER_USER": "1"},
            clear=False,
        ):
            app.PullwiseHandler.route(claim, "POST")

        self.assertEqual(claim.status, HTTPStatus.OK)
        self.assertEqual([job["scan_id"] for job in claim.payload["jobs"]], ["sc_slots_1", "sc_slots_3", "sc_slots_4"])
        self.assertEqual(app.SCANS[1]["status"], "queued")

    def test_worker_claim_with_no_free_slots_leaves_job_queued(self) -> None:
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

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1", "free_slots": 0}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")

        self.assertEqual(claim.status, HTTPStatus.OK)
        self.assertEqual(claim.payload["jobs"], [])
        self.assertIsNone(claim.payload["job"])
        self.assertEqual(scan["status"], "queued")

    def test_busy_worker_cannot_claim_by_requesting_extra_jobs(self) -> None:
        scan = {
            "id": "sc_busy_worker",
            "repo": "acme/busy",
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "repoId": "repo_busy",
            "githubRepoId": "busy",
        }
        app.SCANS.append(scan)
        job = app.create_scan_job_for_scan(scan)
        db.upsert_worker_heartbeat(
            {
                "worker_id": "wk_1",
                "version": "0.1.0",
                "provider": "codex",
                "max_concurrent_jobs": 2,
                "running_jobs": 2,
                "free_slots": 0,
                "doctor_status": "ok",
                "codex_ready": 1,
                "timestamp": app.now(),
            }
        )

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1", "max_jobs": 2}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")

        self.assertEqual(claim.status, HTTPStatus.OK)
        self.assertEqual(claim.payload["jobs"], [])
        self.assertIsNone(claim.payload["job"])
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "queued")
        self.assertEqual(scan["status"], "queued")

    def test_worker_claim_requires_ready_worker(self) -> None:
        scan = {
            "id": "sc_not_ready",
            "repo": "acme/not-ready",
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
        }
        app.SCANS.append(scan)
        job = app.create_scan_job_for_scan(scan)
        db.upsert_worker_heartbeat(
            {
                "worker_id": "wk_1",
                "version": "0.1.0",
                "provider": "codex",
                "max_concurrent_jobs": 1,
                "running_jobs": 0,
                "free_slots": 1,
                "doctor_status": "degraded",
                "codex_ready": 0,
                "timestamp": app.now(),
            }
        )

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")

        self.assertEqual(claim.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "queued")
        self.assertEqual(scan["status"], "queued")

    def test_worker_claim_requires_supported_provider(self) -> None:
        scan = {
            "id": "sc_bad_provider",
            "repo": "acme/bad-provider",
            "branch": "main",
            "commit": "pending",
            "status": "queued",
            "userId": "usr_1",
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "progress": 0,
            "phase": None,
        }
        app.SCANS.append(scan)
        job = app.create_scan_job_for_scan(scan)
        db.upsert_worker_heartbeat(
            {
                "worker_id": "wk_1",
                "version": "0.1.0",
                "provider": "unknown",
                "max_concurrent_jobs": 1,
                "running_jobs": 0,
                "free_slots": 1,
                "doctor_status": "ok",
                "codex_ready": 1,
                "timestamp": app.now(),
            }
        )

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")

        self.assertEqual(claim.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "queued")
        self.assertEqual(scan["status"], "queued")

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
            {"PULLWISE_MAX_RUNNING_SCANS_PER_USER": "1"},
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
                app.PullwiseHandler.route(result, "POST")
                self.assertEqual(result.status, HTTPStatus.OK)

        with patch.dict(
            os.environ,
            {"PULLWISE_MAX_RUNNING_SCANS_PER_USER": "1"},
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
                **audit_result_fields([]),
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
                **audit_result_fields(
                    [audit_issue_card("Late result", issue_id="issue-late-result", severity="P1")]
                ),
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(result, "POST")

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
        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)

        scan["status"] = "cancelled"
        db.cancel_scan_job_for_scan(scan["id"])
        progress = RouteHarness(
            f"/worker/jobs/{job['job_id']}/progress",
            {"phase": "ai", "progress": 70, "message": "late update"},
            headers=self.auth,
        )
        app.PullwiseHandler.route(progress, "POST")

        self.assertEqual(progress.status, HTTPStatus.CONFLICT)
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "cancelled")
        self.assertEqual(db.get_scan_job(job["job_id"])["progress"], 0)
        self.assertEqual(app.SCANS[0]["status"], "cancelled")
        self.assertEqual(app.SCANS[0]["progress"], 0)

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
                **audit_result_fields(
                    [audit_issue_card("Wrong attempt", issue_id="issue-wrong-attempt", severity="P1")]
                ),
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
                **audit_result_fields(
                    [audit_issue_card("Current attempt", issue_id="issue-current-attempt", severity="P1")]
                ),
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
        db.upsert_worker_heartbeat(
            {
                "worker_id": "wk_1",
                "version": "0.1.0",
                "provider": "codex",
                "max_concurrent_jobs": 2,
                "running_jobs": 0,
                "free_slots": 2,
                "doctor_status": "ok",
                "codex_ready": 1,
                "timestamp": timestamp + 3701,
            }
        )

        second_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        with patch("pullwise_server.app.now", return_value=timestamp + 3701):
            app.PullwiseHandler.route(second_claim, "POST")
        self.assertEqual(second_claim.status, HTTPStatus.OK)
        self.assertEqual(second_claim.payload["job"]["attempt"], 2)

        stale_result = RouteHarness(
            f"/worker/jobs/{job['job_id']}/result",
            {"status": "done", "attempt_id": "wk_1-1", "result_checksum": "stale", **audit_result_fields([])},
            headers=self.auth,
        )
        app.PullwiseHandler.route(stale_result, "POST")
        self.assertEqual(stale_result.status, HTTPStatus.CONFLICT)
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "claimed")
        self.assertEqual(app.SCANS[0]["status"], "running")

        current_result = RouteHarness(
            f"/worker/jobs/{job['job_id']}/result",
            {"status": "done", "attempt_id": "wk_1-2", "result_checksum": "current", **audit_result_fields([])},
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
            jobs = db.claim_next_scan_jobs(
                worker_id,
                max_jobs=1,
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
