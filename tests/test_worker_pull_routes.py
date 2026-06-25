from __future__ import annotations

import gzip
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
    def read_json(self) -> dict:
        return app.PullwiseHandler.read_json(self)


class GraphVerifiedReportContractsTest(unittest.TestCase):
    def graph_verified_item(self) -> dict:
        return {
            "candidate": {
                "issue_id": "issue_1",
                "claim": "Confirmed claim",
                "graph_evidence": {
                    "slice_id": "slice-1",
                    "codegraph_files": ["src/app.py"],
                },
                "evidence": [
                    {
                        "file": "src/app.py",
                        "line": 10,
                        "end_line": 12,
                        "why_it_matters": "The handler reaches the failing path.",
                    }
                ],
            },
            "judge": {
                "status": "confirmed",
                "level": "L2",
                "safe_to_show_user": True,
                "evidence_summary": {
                    "command": "pytest tests/test_issue.py",
                    "log_path": "logs/repro.log",
                    "observable": "Assertion failed as expected.",
                },
            },
            "repro": {
                "status": "reproduced",
                "level": "L2",
                "commands_run": [
                    {
                        "cmd": "pytest tests/test_issue.py",
                        "cwd": "/worker/repo",
                        "exit_code": 1,
                        "log_path": "logs/repro.log",
                    }
                ],
                "graph_path_exercised": True,
            },
            "verification": {"verdict": "confirmed", "safe_to_show_user": True},
        }

    def test_public_graph_verified_report_sanitizes_and_preserves_confirmed_only_artifacts(self) -> None:
        confirmed_item = self.graph_verified_item()
        confirmed_item["candidate"]["internal_secret"] = "must not leak"
        confirmed_item["judge"]["raw_prompt"] = "must not leak"
        confirmed_item["repro"]["commands_run"][0]["stdout"] = "must not leak"

        report = app.public_graph_verified_report(
            {
                "runId": "run_1",
                "mode": "standard",
                "scanMode": "full-strict",
                "head": "HEAD",
                "confirmedCount": 1,
                "rejectedCount": 2,
                "blockedCount": 0,
                "finalMarkdown": "# Graph-Verified Code Review Report\n\nConfirmed only.",
                "debugMarkdown": "# Debug Report\n\nRejected candidates: 2",
                "finalJson": {"confirmed": [confirmed_item]},
            }
        )

        self.assertEqual(report["version"], "graph-verified-code-review/1")
        self.assertEqual(report["runId"], "run_1")
        self.assertEqual(report["scanMode"], "full-strict")
        self.assertEqual(report["confirmedCount"], 1)
        self.assertEqual(report["rejectedCount"], 2)
        confirmed = report["finalJson"]["confirmed"][0]
        self.assertEqual(confirmed["candidate"]["issue_id"], "issue_1")
        self.assertEqual(confirmed["candidate"]["claim"], "Confirmed claim")
        self.assertEqual(confirmed["candidate"]["evidence"][0]["lines"], "10-12")
        self.assertEqual(confirmed["judge"]["evidence_summary"]["command"], "pytest tests/test_issue.py")
        self.assertEqual(confirmed["repro"]["commands_run"][0]["exit_code"], 1)
        self.assertNotIn("internal_secret", json.dumps(report))
        self.assertNotIn("raw_prompt", json.dumps(report))
        self.assertNotIn("stdout", json.dumps(report))
        self.assertNotIn("finalMarkdown", report)
        self.assertNotIn("debugMarkdown", report)
        findings = app.worker_graph_verified_findings({"repo": "acme/app"}, report)
        self.assertEqual(findings[0]["codeEvidence"][0]["lines"], "10-12")
        self.assertEqual(findings[0]["line"], 10)

        full_report = app.public_graph_verified_report(
            {
                "runId": "run_1",
                "mode": "standard",
                "confirmedCount": 1,
                "finalMarkdown": "# Graph-Verified Code Review Report\n\nConfirmed only.",
                "debugMarkdown": "# Debug Report\n\nRejected candidates: 2",
                "finalJson": {"confirmed": [{"candidate": {"issue_id": "issue_1"}}]},
            },
            include_markdown=True,
            include_debug=True,
        )
        self.assertIn("Confirmed only.", full_report["finalMarkdown"])
        self.assertIn("Rejected candidates: 2", full_report["debugMarkdown"])

    def test_graph_verified_report_gate_rejects_items_missing_required_public_evidence(self) -> None:
        unsafe = self.graph_verified_item()
        unsafe["judge"]["safe_to_show_user"] = False
        weak_level = self.graph_verified_item()
        weak_level["judge"]["level"] = "L1"
        unreproduced = self.graph_verified_item()
        unreproduced["repro"]["status"] = "not_reproduced"
        no_graph_path = self.graph_verified_item()
        no_graph_path["repro"]["graph_path_exercised"] = False
        no_log = self.graph_verified_item()
        no_log["judge"]["evidence_summary"].pop("log_path")
        no_log["repro"]["commands_run"][0].pop("log_path")
        no_exit_code = self.graph_verified_item()
        no_exit_code["repro"]["commands_run"][0].pop("exit_code")
        no_line = self.graph_verified_item()
        no_line["candidate"]["evidence"][0].pop("line")
        no_line["candidate"]["evidence"][0].pop("end_line")

        report = {
            "runId": "run_1",
            "mode": "standard",
            "confirmedCount": 7,
            "finalJson": {
                "confirmed": [
                    unsafe,
                    weak_level,
                    unreproduced,
                    no_graph_path,
                    no_log,
                    no_exit_code,
                    no_line,
                ]
            },
        }

        public_report = app.public_graph_verified_report(report)
        self.assertEqual(public_report["confirmedCount"], 0)
        self.assertEqual(public_report["finalJson"]["confirmed"], [])
        self.assertEqual(app.worker_graph_verified_findings({"repo": "acme/app"}, public_report), [])

    def test_audit_bundle_includes_graph_verified_report_artifacts(self) -> None:
        report = app.public_graph_verified_report(
            {
                "runId": "run_1",
                "mode": "standard",
                "confirmedCount": 1,
                "finalMarkdown": "# Final\n",
                "debugMarkdown": "# Debug\n",
                "finalJson": {"confirmed": [{"candidate": {"issue_id": "issue_1"}}]},
            },
            include_markdown=True,
            include_debug=True,
        )

        artifacts = app.audit_bundle_graph_verified_artifacts(report)
        paths = {artifact["path"] for artifact in artifacts}

        self.assertIn("graph-verified/final.json", paths)
        self.assertIn("graph-verified/final.md", paths)
        self.assertIn("graph-verified/debug.md", paths)


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


def graph_verified_severity(value: object) -> str:
    return {
        "p0": "critical",
        "p1": "high",
        "p2": "medium",
        "p3": "low",
        "p4": "info",
        "critical": "critical",
        "high": "high",
        "medium": "medium",
        "low": "low",
        "info": "info",
    }.get(str(value or "").lower(), "medium")


def graph_verified_fixture_file(value: object) -> str:
    text = str(value or "").replace("\\", "/")
    for marker in ("/src/", "/tests/", "/test/"):
        if marker in text:
            return marker.strip("/") + "/" + text.split(marker, 1)[1]
    return text if text and not text.startswith("/") else "src/app.py"


def graph_verified_item_from_card(card: dict, results: list[dict], index: int) -> dict:
    issue_id = str(card.get("issue_id") or card.get("issueId") or card.get("id") or f"issue-{index + 1}")
    confirmed_results = [item for item in results if str(item.get("verdict") or "confirmed").lower() == "confirmed"]
    result = confirmed_results[0] if confirmed_results else {}
    reproduction = card.get("reproduction") if isinstance(card.get("reproduction"), dict) else {}
    locations = card.get("locations") if isinstance(card.get("locations"), list) else []
    primary = locations[0] if locations and isinstance(locations[0], dict) else {}
    file_path = graph_verified_fixture_file(card.get("file") or primary.get("file"))
    start_line = int(primary.get("startLine") or primary.get("start_line") or primary.get("line") or 1)
    end_line = int(primary.get("endLine") or primary.get("end_line") or start_line)
    commands = (
        result.get("commands_run")
        if isinstance(result.get("commands_run"), list)
        else reproduction.get("commands")
        if isinstance(reproduction.get("commands"), list)
        else []
    )
    command = str(commands[0]) if commands else "python -m pytest"
    log_path = str(result.get("logPath") or result.get("log_path") or reproduction.get("logPath") or f"logs/{issue_id}.log")
    proof_actual = str(
        result.get("result_summary")
        or result.get("resultSummary")
        or reproduction.get("actual")
        or "Local reproduction captured the observed behavior."
    )
    proof_expected = str(reproduction.get("expected") or "Expected behavior should hold.")
    evidence_items = []
    raw_evidence = card.get("evidence") if isinstance(card.get("evidence"), list) else []
    for evidence in raw_evidence[:4]:
        if isinstance(evidence, dict):
            evidence_file = graph_verified_fixture_file(evidence.get("file") or evidence.get("path") or file_path)
            evidence_start = int(evidence.get("startLine") or evidence.get("start_line") or evidence.get("line") or start_line)
            evidence_end = int(evidence.get("endLine") or evidence.get("end_line") or evidence_start)
            why = str(evidence.get("why_it_matters") or evidence.get("summary") or evidence.get("text") or "Code evidence")
        else:
            evidence_file = file_path
            evidence_start = start_line
            evidence_end = end_line
            why = str(evidence or "Code evidence")
        evidence_items.append({"file": evidence_file, "lines": f"{evidence_start}-{evidence_end}", "why_it_matters": why})
    if not evidence_items:
        evidence_items.append({"file": file_path, "lines": f"{start_line}-{end_line}", "why_it_matters": "Code evidence"})
    return {
        "candidate": {
            "issue_id": issue_id,
            "candidate_id": issue_id,
            "dedupe_key": str(card.get("dedupe_key") or issue_id),
            "severity": graph_verified_severity(card.get("severity")),
            "category": str(card.get("category") or "Quality"),
            "confidence": "high",
            "claim": str(card.get("claim") or card.get("title") or f"GraphVerified issue {index + 1}"),
            "trigger_condition": str(reproduction.get("input") or card.get("reproduction_idea") or "Run the local reproduction."),
            "expected_behavior": proof_expected,
            "actual_behavior_hypothesis": proof_actual,
            "minimal_repro_idea": str(card.get("suggested_test") or command),
            "repro_likelihood": "high",
            "graph_evidence": {
                "slice_id": f"slice-{issue_id}",
                "codegraph_files": [file_path],
                "path_summary": [f"{file_path}:{start_line}-{end_line}", "candidate -> repro -> judge"],
            },
            "evidence": evidence_items,
            "fix_direction": str(card.get("fix_direction") or "Fix the confirmed behavior and rerun the reproduction."),
        },
        "repro": {
            "candidate_id": issue_id,
            "status": "reproduced",
            "level": "L2",
            "summary": proof_actual,
            "commands_run": [{"cmd": command, "cwd": ".", "exit_code": 1, "log_path": log_path}],
            "files_written": [],
            "proof": {
                "type": str(result.get("proof_type") or "failing_test"),
                "expected": proof_expected,
                "actual": proof_actual,
                "log_excerpt": str(result.get("output") or proof_actual),
            },
            "graph_path_exercised": True,
            "why_valid": "The local command exercises the Graph-Verified path.",
            "why_not_reproduced": "",
            "safety_notes": "Local test fixture.",
        },
        "judge": {
            "candidate_id": issue_id,
            "status": "confirmed",
            "level": "L2",
            "safe_to_show_user": True,
            "reason": "Graph evidence, local reproduction, and judge validation are present.",
            "evidence_summary": {
                "command": command,
                "log_path": log_path,
                "observable": proof_actual,
            },
            "limitations": card.get("limitations") or [],
        },
        "verification": {"status": "confirmed", "level": "L2", "safe_to_show_user": True},
    }


def audit_result_fields(issue_cards: list[dict], verification_results: list[dict] | None = None) -> dict:
    results = verification_results or []
    results_by_issue = {}
    for result in results:
        issue_id = str(result.get("issue_id") or result.get("issueId") or "")
        if issue_id:
            results_by_issue.setdefault(issue_id, []).append(result)
    confirmed = []
    for index, card in enumerate(issue_cards):
        issue_id = str(card.get("issue_id") or card.get("issueId") or card.get("id") or "")
        card_results = results_by_issue.get(issue_id, [])
        if any(str(result.get("verdict") or "").lower() == "rejected" for result in card_results):
            continue
        confirmed.append(graph_verified_item_from_card(card, card_results, index))
    return {
        "graphVerifiedReport": {
            "version": "graph-verified-code-review/1",
            "runId": "gv_test_run",
            "mode": "standard",
            "head": "HEAD",
            "confirmedCount": len(confirmed),
            "rejectedCount": max(0, len(issue_cards) - len(confirmed)),
            "blockedCount": 0,
            "finalJson": {"confirmed": confirmed},
        }
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
        "absolutePath": "C:\\worker\\repo",
    }


def semantic_graph_fixture() -> dict:
    return {
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
    }


def impact_graph_fixture() -> dict:
    return {
        "version": "impact-graph/0.1",
        "mode": "repository",
        "summary": "Impact graph: 1 target, 1 test link, 1 doc link, 1 config link.",
        "stats": {
            "targets": 1,
            "testedTargets": 1,
            "documentedTargets": 1,
            "configuredTargets": 1,
            "testsEdges": 1,
            "documentsEdges": 1,
            "configuresEdges": 1,
            "repositoryFiles": 1,
            "truncated": False,
        },
        "repositoryFiles": ["src/app.py", "C:\\worker\\repo\\secret.py"],
        "targets": [
            {
                "id": "file:src/app.py",
                "path": "src/app.py",
                "label": "app.py",
                "type": "file",
                "risk": 0.74,
                "relations": {
                    "tests": [
                        {
                            "id": "file:tests/test_app.py",
                            "path": "tests/test_app.py",
                            "label": "test_app.py",
                            "type": "test",
                            "confidence": 0.95,
                            "evidenceKind": "import",
                            "evidence": [
                                {
                                    "kind": "import",
                                    "file": "tests/test_app.py",
                                    "line": 3,
                                    "text": "from src.app import app\nwith newline",
                                }
                            ],
                        }
                    ],
                    "documents": [{"id": "file:docs/api.md", "path": "docs/api.md", "type": "doc"}],
                    "configures": [{"id": "file:package.json", "path": "package.json", "type": "config"}],
                    "importedBy": [{"id": "file:src/server.py", "path": "src/server.py", "type": "file"}],
                    "symbols": [
                        {
                            "id": "symbol:src/app.py:health",
                            "path": "src/app.py",
                            "label": "health",
                            "type": "function",
                            "line": 6,
                        }
                    ],
                },
                "gaps": ["no_direct_docs"],
            },
            {"id": "file:bad", "path": "C:\\worker\\repo\\bad.py", "label": "bad", "type": "file"},
        ],
        "coverage": {
            "sourceFilesWithoutTests": ["src/untested.py"],
            "sourceFilesWithoutDocs": ["src/app.py"],
            "testsWithoutTargets": ["tests/orphan_test.py"],
            "docsWithoutTargets": ["docs/orphan.md"],
        },
        "promptText": "Impact context:\n- src/app.py -> tests: tests/test_app.py\n- C:\\worker\\repo\\secret.py leaked",
    }


def repository_graph_v2_fixture() -> dict:
    payload = repository_graph_fixture()
    payload["version"] = "repository-graph/0.2"
    payload["nodes"] = [
        *payload["nodes"][:2],
        {"id": "file:tests/test_app.py", "label": "test_app.py", "type": "test", "path": "tests/test_app.py"},
        {"id": "file:docs/api.md", "label": "api.md", "type": "doc", "path": "docs/api.md"},
        {"id": "file:package.json", "label": "package.json", "type": "config", "path": "package.json"},
    ]
    payload["edges"] = [
        *payload["edges"][:1],
        {
            "id": "tests:file:tests/test_app.py->file:src/app.py",
            "source": "file:tests/test_app.py",
            "target": "file:src/app.py",
            "type": "tests",
            "weight": 1,
            "confidence": 0.946,
            "evidence": [
                {
                    "kind": "import",
                    "file": "tests/test_app.py",
                    "line": 3,
                    "text": "from src.app import app",
                },
                {
                    "kind": "absolute",
                    "file": "C:\\worker\\repo\\secret.py",
                    "line": 4,
                    "text": "secret\nline",
                },
            ],
        },
        {
            "id": "documents:file:docs/api.md->file:src/app.py",
            "source": "file:docs/api.md",
            "target": "file:src/app.py",
            "type": "documents",
            "confidence": 0.85,
        },
        {
            "id": "configures:file:package.json->dir:src",
            "source": "file:package.json",
            "target": "dir:src",
            "type": "configures",
            "confidence": 0.75,
        },
    ]
    payload["impactGraph"] = impact_graph_fixture()
    return payload


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

    def test_worker_auth_rejection_is_logged_without_token_value(self) -> None:
        claim = RouteHarness(
            "/worker/jobs/claim",
            {"worker_id": "wk_1"},
            headers={"Authorization": "Bearer invalid-worker-token"},
        )
        with self.assertLogs(app.logger, level="WARNING") as logs:
            app.PullwiseHandler.route(claim, "POST")

        self.assertEqual(claim.status, HTTPStatus.UNAUTHORIZED)
        output = "\n".join(logs.output)
        self.assertIn("Rejected worker request path=/worker/jobs/claim", output)
        self.assertIn("bearer_present=True", output)
        self.assertNotIn("invalid-worker-token", output)

    def test_worker_agent_configs_accepts_disabled_worker_token_without_impersonation(self) -> None:
        worker, token = self.create_registry_worker("wk_disabled_agent_configs")
        db.set_worker_enabled(worker["worker_id"], False)
        headers = {"Authorization": f"Bearer {token}"}

        agent_configs = RouteHarness(
            "/worker/agent-configs",
            {"worker_id": worker["worker_id"]},
            headers=headers,
        )
        app.PullwiseHandler.route(agent_configs, "POST")

        self.assertEqual(agent_configs.status, HTTPStatus.OK)
        self.assertIn("agentConfigs", agent_configs.payload)

        impersonation = RouteHarness(
            "/worker/agent-configs",
            {"worker_id": "wk_other"},
            headers=headers,
        )
        app.PullwiseHandler.route(impersonation, "POST")

        self.assertEqual(impersonation.status, HTTPStatus.FORBIDDEN)

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
        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)
        result_body = {
            "status": "done",
            "attempt_id": "wk_1-1",
            "result_checksum": "checksum-gzip-result",
            **audit_result_fields([]),
            "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        raw_body = gzip.compress(json.dumps(result_body).encode("utf-8"))
        result = RawBodyRouteHarness(
            f"/worker/jobs/{job['job_id']}/result",
            raw_body=raw_body,
            headers={**self.auth, "Content-Encoding": "gzip", "Content-Length": str(len(raw_body))},
        )

        app.PullwiseHandler.route(result, "POST")

        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertTrue(result.payload["accepted"])
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "done")

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

        report = audit_result_fields(
            [
                audit_issue_card("First duplicate", issue_id="issue-duplicate", file="src/first.py"),
                audit_issue_card("Second duplicate", issue_id="issue-duplicate", file="src/second.py"),
            ]
        )["graphVerifiedReport"]
        findings = app.worker_graph_verified_findings(
            job,
            report,
            reserved_ids=app.worker_issue_reserved_ids(job),
        )

        self.assertEqual([finding["id"] for finding in findings], ["issue-duplicate-2", "issue-duplicate-3"])

    def test_worker_result_merges_deterministic_findings_into_issues(self) -> None:
        job = {
            "scan_id": "sc_static",
            "job_id": "job_static",
            "user_id": "usr_1",
            "repo": "acme/api",
            "branch": "main",
            "commit": "abc1234",
        }
        body = {
            "status": "done",
            "graphVerifiedReport": {
                "version": "graph-verified-code-review/1",
                "runId": "gv_run",
                "confirmedCount": 0,
                "rejectedCount": 0,
                "blockedCount": 0,
                "finalJson": {"confirmed": []},
            },
            "deterministicFindings": [
                {
                    "id": "static_secret_1",
                    "severity": "high",
                    "category": "Security",
                    "title": "Committed token",
                    "summary": "A committed token was detected.",
                    "file": "app.env",
                    "line": 1,
                    "verificationStatus": "static_proof",
                    "affectedLocations": [{"file": "app.env", "startLine": 1, "endLine": 1}],
                    "evidence": [
                        {
                            "type": "code",
                            "summary": "Line 1 contains a token-shaped value.",
                            "file": "app.env",
                            "startLine": 1,
                            "endLine": 1,
                        }
                    ],
                }
            ],
        }

        prepared = app.prepare_worker_job_result_state(job, body, status="done", checksum="checksum")
        findings = prepared["normalized_findings"]

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["id"], "static_secret_1")
        self.assertEqual(findings[0]["verificationStatus"], "static_proof")
        self.assertEqual(findings[0]["affectedLocations"][0]["file"], "app.env")
        self.assertEqual(prepared["summary"]["high"], 1)

    def test_worker_graph_verified_missing_repro_log_is_not_reported(self) -> None:
        report = audit_result_fields(
            [audit_issue_card("Unsupported verifier confirmation", issue_id="issue-unsupported")]
        )["graphVerifiedReport"]
        report["finalJson"]["confirmed"][0]["repro"]["commands_run"][0].pop("log_path", None)
        report["finalJson"]["confirmed"][0]["judge"]["evidence_summary"].pop("log_path", None)

        findings = app.worker_graph_verified_findings(
            {
                "scan_id": "sc_1",
                "job_id": "job_1",
                "user_id": "usr_1",
                "repo": "acme/api",
                "branch": "main",
                "commit": "abc123",
            },
            report,
        )

        self.assertEqual(findings, [])

    def test_worker_graph_verified_missing_graph_path_exercised_is_not_reported(self) -> None:
        report = audit_result_fields(
            [audit_issue_card("Proof strength only confirmation", issue_id="issue-proof-strength")]
        )["graphVerifiedReport"]
        report["finalJson"]["confirmed"][0]["repro"]["graph_path_exercised"] = False

        findings = app.worker_graph_verified_findings(
            {
                "scan_id": "sc_1",
                "job_id": "job_1",
                "user_id": "usr_1",
                "repo": "acme/api",
                "branch": "main",
                "commit": "abc123",
            },
            report,
        )

        self.assertEqual(findings, [])

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

    def test_worker_result_exposes_graph_verified_judge_and_repro_summary_on_issue_payload(self) -> None:
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

        self.assertTrue(payload["graphVerified"])
        self.assertEqual(payload["verificationLevel"], "L2")
        self.assertEqual(payload["safeToShowUser"], True)
        self.assertEqual(payload["graphEvidence"]["slice_id"], "slice-issue-calibrated")
        self.assertEqual(payload["judgeEvidence"]["status"], "confirmed")
        self.assertEqual(payload["judgeEvidence"]["level"], "L2")
        self.assertEqual(payload["reproProof"]["graphPathExercised"], True)
        self.assertNotIn("reviewCalibration", payload)
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
        self.assertEqual(confirmed, [])
        self.assertEqual(rejected, [])

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
        self.assertNotIn("review_calibration_context", next_claim.payload["job"])

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
        self.assertEqual(app.SCANS[0]["issues"]["high"], 1)

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
        self.assertEqual(app.SCANS[0]["issues"]["high"], 1)
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
        self.assertTrue(payload["graphVerified"])
        self.assertEqual(payload["verificationLevel"], "L2")
        self.assertEqual(payload["safeToShowUser"], True)
        self.assertEqual(payload["reproduction"]["commands"], ["pytest tests/repro/test_page_zero.py"])
        self.assertEqual(payload["reproduction"]["exitCode"], 1)
        self.assertEqual(payload["affectedLocations"][0]["url"], "https://github.com/acme/api/blob/abc1234/src/app.py#L12-L14")
        self.assertEqual(payload["codeEvidence"][0]["file"], "src/app.py")
        self.assertEqual(payload["codeEvidence"][0]["lines"], "12-14")
        self.assertEqual(payload["graphEvidence"]["slice_id"], "slice-f_page_zero")
        self.assertEqual(payload["graphEvidence"]["codegraph_files"], ["src/app.py"])
        self.assertEqual(payload["triggerCondition"], "GET /users?page=0")
        self.assertEqual(payload["expectedBehavior"], "400 validation error")
        self.assertEqual(payload["observedBehavior"], "500 internal server error")
        self.assertEqual(payload["judgeEvidence"]["status"], "confirmed")
        self.assertEqual(payload["judgeEvidence"]["command"], "pytest tests/repro/test_page_zero.py")
        self.assertEqual(payload["judgeEvidence"]["observable"], "500 internal server error")
        self.assertEqual(payload["reproProof"]["type"], "failing_test")
        self.assertEqual(payload["reproProof"]["actual"], "500 internal server error")
        self.assertTrue(payload["reproProof"]["graphPathExercised"])
        self.assertEqual(payload["limitations"], ["A production API gateway could reject page < 1 before the app."])
        self.assertNotIn("verificationStatus", payload)
        self.assertNotIn("auditSwarm", payload)
        self.assertNotIn("verificationAudit", payload)
        scan_payload = app.scan_payload(app.SCANS[0])
        self.assertEqual(scan_payload["graphVerifiedReport"]["confirmedCount"], 1)
        self.assertEqual(
            scan_payload["graphVerifiedReport"]["finalJson"]["confirmed"][0]["candidate"]["issue_id"],
            "f_page_zero",
        )
        self.assertNotIn("verificationAudit", scan_payload)
        self.assertNotIn("auditSwarm", scan_payload)

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
        self.assertEqual(owner.payload["kind"], "pullwise.graph_verified_audit_bundle")
        self.assertEqual(owner.payload["schemaVersion"], 1)
        self.assertEqual(owner.payload["scan"]["id"], "sc_bundle")
        self.assertEqual(owner.payload["preflight"]["verifier"]["runs"][0]["status"], "failed")
        self.assertTrue(owner.payload["preflight"]["verifier"]["runs"][0]["outputRedacted"])
        self.assertNotIn("output", owner.payload["preflight"]["verifier"]["runs"][0])
        self.assertNotIn("verificationAudit", owner.payload)
        self.assertNotIn("repositoryGraph", owner.payload)
        self.assertNotIn("semanticGraph", owner.payload)
        self.assertNotIn("impactGraph", owner.payload)
        artifact_paths = [artifact["path"] for artifact in owner.payload["artifacts"]]
        self.assertIn("scan/scan.json", artifact_paths)
        self.assertIn("preflight/preflight.json", artifact_paths)
        self.assertIn("graph-verified/final.json", artifact_paths)
        self.assertNotIn("repository-graph.json", artifact_paths)
        self.assertNotIn("semantic-graph.json", artifact_paths)
        self.assertNotIn("impact-graph.json", artifact_paths)
        self.assertNotIn("audit.json", artifact_paths)
        artifacts = {artifact["path"]: artifact for artifact in owner.payload["artifacts"]}
        self.assertNotIn("verificationAudit", artifacts["scan/scan.json"]["content"])
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
            self.assertIn("graph-verified/final.json", archive.namelist())
            self.assertNotIn("audit.json", archive.namelist())
            self.assertNotIn("repository-graph.json", archive.namelist())

        other_user = RouteHarness(
            "/scans/sc_bundle/audit-bundle",
            headers={"Cookie": f"{app.SESSION_COOKIE}=ses_other"},
        )
        app.PullwiseHandler.route(other_user, "GET")
        self.assertEqual(other_user.status, HTTPStatus.NOT_FOUND)

        anonymous = RouteHarness("/scans/sc_bundle/audit-bundle")
        app.PullwiseHandler.route(anonymous, "GET")
        self.assertEqual(anonymous.status, HTTPStatus.UNAUTHORIZED)

    def test_scan_audit_bundle_ignores_legacy_repository_graph_fields(self) -> None:
        scan = self.audit_bundle_cache_fixture()
        scan["graphVerifiedReport"] = app.public_graph_verified_report(
            {"runId": "gv_bundle", "mode": "standard", "finalJson": {"confirmed": []}}
        )
        scan["repositoryGraph"] = {"version": "repository-graph/legacy"}
        scan["semanticGraph"] = {"version": "semantic-code-graph/legacy"}
        scan["impactGraph"] = {"version": "impact-graph/legacy"}

        owner = RouteHarness(
            "/scans/sc_cache/audit-bundle",
            headers={"Cookie": f"{app.SESSION_COOKIE}=ses_owner"},
        )
        app.PullwiseHandler.route(owner, "GET")

        self.assertEqual(owner.status, HTTPStatus.OK)
        self.assertEqual(owner.payload["kind"], "pullwise.graph_verified_audit_bundle")
        self.assertNotIn("repositoryGraph", owner.payload)
        self.assertNotIn("semanticGraph", owner.payload)
        self.assertNotIn("impactGraph", owner.payload)
        archive = app.scan_audit_bundle_zip_bytes(scan)
        with zipfile.ZipFile(io.BytesIO(archive), "r") as bundle:
            names = bundle.namelist()
        self.assertNotIn("repository-graph.json", names)
        self.assertNotIn("semantic-graph.json", names)
        self.assertNotIn("impact-graph.json", names)
        self.assertNotIn("impact-summary.md", names)

    def test_scan_impact_graph_routes_are_gone(self) -> None:
        scan = self.audit_bundle_cache_fixture()
        scan["repositoryGraph"] = {"version": "repository-graph/legacy"}
        scan["impactGraph"] = {"version": "impact-graph/legacy"}

        owner = RouteHarness(
            "/scans/sc_cache/impact-graph",
            headers={"Cookie": f"{app.SESSION_COOKIE}=ses_owner"},
        )
        app.PullwiseHandler.route(owner, "GET")

        self.assertEqual(owner.status, HTTPStatus.GONE)

        focus = RouteHarness(
            "/scans/sc_cache/impact-graph/focus?path=src/app.py",
            headers={"Cookie": f"{app.SESSION_COOKIE}=ses_owner"},
        )
        app.PullwiseHandler.route(focus, "GET")

        self.assertEqual(focus.status, HTTPStatus.GONE)

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
        self.assertNotIn("verificationAudit", scan_payload)

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
        self.assertNotIn("verificationAudit", scan_payload)

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
        self.assertEqual(payload["graphVerifiedReport"]["confirmedCount"], 0)
        self.assertEqual(payload["graphVerifiedReport"]["finalJson"]["confirmed"], [])
        self.assertNotIn("verificationAudit", payload)

    def test_worker_result_persists_canonical_graph_verified_report(self) -> None:
        scan = {
            "id": "sc_graph_verified",
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
                "result_checksum": "checksum-graph-verified-report",
                **audit_result_fields([]),
                "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                "graphVerifiedReport": {
                    "runId": "gv_run_1",
                    "mode": "standard",
                    "head": "HEAD",
                    "confirmedCount": 1,
                    "rejectedCount": 2,
                    "blockedCount": 0,
                    "finalMarkdown": "# Graph-Verified Code Review Report\n\nConfirmed only.",
                    "debugMarkdown": "# Debug Report\n\nRejected candidates: 2",
                    "finalJson": {
                        "confirmed": [
                            {
                                "candidate": {
                                    "issue_id": "issue-confirmed",
                                    "candidate_id": "candidate-confirmed",
                                    "dedupe_key": "graph:confirmed",
                                    "severity": "high",
                                    "category": "Quality",
                                    "claim": "Confirmed GraphVerified issue.",
                                    "trigger_condition": "Call the broken path.",
                                    "expected_behavior": "The path should succeed.",
                                    "actual_behavior_hypothesis": "The path fails.",
                                    "graph_evidence": {
                                        "slice_id": "slice-1",
                                        "codegraph_files": ["src/app.py"],
                                        "path_summary": ["route -> handler -> broken_call"],
                                    },
                                    "evidence": [
                                        {
                                            "file": "src/app.py",
                                            "lines": "10-12",
                                            "why_it_matters": "The handler reaches broken_call.",
                                        }
                                    ],
                                    "fix_direction": "Guard the broken call.",
                                },
                                "judge": {
                                    "status": "confirmed",
                                    "level": "L2",
                                    "safe_to_show_user": True,
                                    "evidence_summary": {
                                        "command": "pytest tests/test_app.py",
                                        "log_path": "logs/repro.log",
                                        "observable": "Assertion failed as expected.",
                                    },
                                },
                                "repro": {
                                    "status": "reproduced",
                                    "level": "L2",
                                    "summary": "Local reproduction failed as expected.",
                                    "commands_run": [
                                        {"cmd": "pytest tests/test_app.py", "exit_code": 1, "log_path": "logs/repro.log"}
                                    ],
                                    "proof": {"expected": "pass", "actual": "failure"},
                                    "graph_path_exercised": True,
                                },
                                "verification": {"verdict": "confirmed", "safe_to_show_user": True},
                            }
                        ]
                    },
                },
                "graph_verified_report": {
                    "runId": "snake_case_must_not_win",
                    "confirmedCount": 99,
                    "finalMarkdown": "snake case report",
                    "finalJson": {"confirmed": [{"candidate": {"issue_id": "snake-case"}}]},
                },
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(result, "POST")
        self.assertEqual(result.status, HTTPStatus.OK)

        stored_report = app.SCANS[0]["graphVerifiedReport"]
        public_payload = app.scan_payload(app.SCANS[0])
        public_report = public_payload["graphVerifiedReport"]

        self.assertEqual(stored_report["runId"], "gv_run_1")
        self.assertEqual(stored_report["confirmedCount"], 1)
        self.assertEqual(stored_report["finalMarkdown"], "# Graph-Verified Code Review Report\n\nConfirmed only.")
        self.assertEqual(stored_report["finalJson"]["confirmed"][0]["candidate"]["issue_id"], "issue-confirmed")
        self.assertEqual(public_report["runId"], "gv_run_1")
        self.assertEqual(public_report["confirmedCount"], 1)
        self.assertEqual(public_report["finalJson"]["confirmed"][0]["verification"]["verdict"], "confirmed")
        self.assertNotIn("finalMarkdown", public_report)
        self.assertNotIn("debugMarkdown", public_report)
        self.assertEqual(len(app.ISSUES), 1)
        self.assertTrue(app.ISSUES[0]["graphVerified"])
        self.assertEqual(app.ISSUES[0]["candidateId"], "candidate-confirmed")
        self.assertEqual(app.ISSUES[0]["graphEvidence"]["slice_id"], "slice-1")
        self.assertEqual(app.ISSUES[0]["reproduction"]["commands"], ["pytest tests/test_app.py"])
        self.assertNotIn("graph_verified_report", app.SCANS[0])
        self.assertNotIn("graph_verified_report", public_payload)
        self.assertNotIn("snake_case_must_not_win", json.dumps(public_payload))

        app.SCANS[0]["repositoryGraph"] = {"version": "repository-graph/legacy"}
        app.SCANS[0]["semanticGraph"] = {"version": "semantic-code-graph/legacy"}
        app.SCANS[0]["impactGraph"] = {"version": "impact-graph/legacy"}
        app.SCANS[0]["verificationAudit"] = {"candidateCount": 99}
        bundle = app.scan_audit_bundle_payload(app.SCANS[0])
        paths = {artifact["path"] for artifact in bundle["artifacts"]}
        self.assertEqual(bundle["kind"], "pullwise.graph_verified_audit_bundle")
        self.assertNotIn("verificationAudit", bundle)
        self.assertNotIn("repositoryGraph", bundle)
        self.assertNotIn("semanticGraph", bundle)
        self.assertNotIn("impactGraph", bundle)
        self.assertIn("scan/scan.json", paths)
        self.assertIn("preflight/preflight.json", paths)
        self.assertIn("graph-verified/final.json", paths)
        self.assertNotIn("repository-graph.json", paths)
        self.assertNotIn("semantic-graph.json", paths)
        self.assertNotIn("impact-graph.json", paths)

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
        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)
        claimed_job = claim.payload["job"]

        result = RouteHarness(
            f"/worker/jobs/{claimed_job['job_id']}/result",
            {
                "status": "failed",
                "attempt_id": f"wk_1-{claimed_job['attempt']}",
                "result_checksum": "checksum-fake-repository-too-large",
                "error": "Repository is too large for Pullwise scanning.",
                "error_code": "REPOSITORY_TOO_LARGE",
                **audit_result_fields([]),
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(result, "POST")

        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertNotIn("quotaRollback", result.payload)
        self.assertEqual(app.quota.quota_payload_for_user(user)["used"], 1)
        self.assertEqual(app.quota.quota_payload_for_repository(repository, user)["used"], 1)
        self.assertNotIn("quotaRefunded", app.scan_payload(app.SCANS[0]))

    def test_worker_progress_ignores_completion_audit_and_job_trace(self) -> None:
        scan = {
            "id": "sc_progress_audit",
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
        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]

        progress = RouteHarness(
            f"/worker/jobs/{job['job_id']}/progress",
            {
                "phase": "ai",
                "progress": 55,
                "message": "Graph: mapping shards 3/12",
                "logs_summary": "stage=graph progress=3/12 task=graph-0003",
                "completion_audit": {
                    "protocol": "completion-audit/0.1",
                    "status": "warning",
                    "retry_recommended": True,
                    "retry_reason": "Worker detected partial output.",
                    "checks": [{"label": "artifact", "status": "warning", "summary": "Missing optional bundle."}],
                    "raw": "drop me",
                },
                "job_trace": {
                    "protocol": "job-trace/0.1",
                    "candidate_findings_before_filter": 7,
                    "checkpoints": [{"key": "review", "status": "running", "duration_ms": 123}],
                    "rejected_reasons": [{"reason": "duplicate", "count": 2}],
                    "next_retry_hint": "retry with a clean workspace",
                    "raw": "drop me",
                },
            },
            headers=self.auth,
        )
        with patch.object(app.scan_logging, "log_event") as log_event:
            app.PullwiseHandler.route(progress, "POST")

        self.assertEqual(progress.status, HTTPStatus.OK)
        log_event.assert_called_once()
        self.assertEqual(log_event.call_args.args[0], "worker_job_progress")
        self.assertEqual(log_event.call_args.kwargs["scanId"], "sc_progress_audit")
        self.assertEqual(log_event.call_args.kwargs["workerId"], "wk_1")
        self.assertEqual(log_event.call_args.kwargs["jobId"], job["job_id"])
        self.assertEqual(log_event.call_args.kwargs["phase"], "ai")
        self.assertEqual(log_event.call_args.kwargs["progress"], 55)
        self.assertEqual(log_event.call_args.kwargs["message"], "Graph: mapping shards 3/12")
        payload = app.scan_payload(app.SCANS[0])
        self.assertEqual(payload["progressMessage"], "Graph: mapping shards 3/12")
        self.assertEqual(payload["logsSummary"], "stage=graph progress=3/12 task=graph-0003")
        self.assertIsInstance(payload.get("updatedAt"), int)
        self.assertNotIn("completionAudit", payload)
        self.assertNotIn("jobTrace", payload)

    def test_worker_progress_exposes_graphverified_detail_on_scan_routes(self) -> None:
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
            "id": "sc_graphverified_progress",
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
        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]

        progress = RouteHarness(
            f"/worker/jobs/{job['job_id']}/progress",
            {
                "phase": "ai",
                "progress": 80,
                "message": "Graph: mapping shards 12/80",
                "logs_summary": "run=gv_run stage=graph progress=12/80 task=graph-0012",
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(progress, "POST")
        self.assertEqual(progress.status, HTTPStatus.OK)

        headers = {"Cookie": "pw_session=ses_owner"}
        listing = RouteHarness("/scans", headers=headers)
        detail = RouteHarness("/scans/sc_graphverified_progress", headers=headers)
        app.PullwiseHandler.route(listing, "GET")
        app.PullwiseHandler.route(detail, "GET")

        self.assertEqual(listing.status, HTTPStatus.OK)
        self.assertEqual(detail.status, HTTPStatus.OK)
        list_scan = listing.payload["items"][0]
        for payload in (list_scan, detail.payload):
            self.assertEqual(payload["phase"], "ai")
            self.assertEqual(payload["progress"], 80)
            self.assertEqual(payload["progressMessage"], "Graph: mapping shards 12/80")
            self.assertEqual(
                payload["logsSummary"],
                "run=gv_run stage=graph progress=12/80 task=graph-0012",
            )
            self.assertIsInstance(payload.get("updatedAt"), int)

    def test_worker_result_log_event_includes_failure_diagnostics(self) -> None:
        scan = {
            "id": "sc_result_diagnostics",
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
        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]

        graph_report = {
            "version": "graph-verified-code-review/1",
            "runId": "run_diag",
            "mode": "standard",
            "head": "HEAD",
            "confirmedCount": 0,
            "rejectedCount": 0,
            "blockedCount": 97,
            "finalJson": {"confirmed": []},
            "summary": {
                "finder": {"tasks": 97, "blocked": 97, "candidates": 0},
                "candidates": {"valid": 0},
                "reports": {"blocked": 97},
            },
        }
        result_body = {
            "status": "failed",
            "attempt_id": f"wk_1-{job['attempt']}",
            "result_checksum": "checksum-result-diagnostics",
            "error": "GraphVerified finder pipeline blocked every finder task before producing candidates",
            "error_code": "GRAPH_VERIFIED_COMPLETION_FAILED",
            "graphVerifiedReport": graph_report,
            "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        result = RouteHarness(f"/worker/jobs/{job['job_id']}/result", result_body, headers=self.auth)
        with patch.object(app.scan_logging, "log_event") as log_event:
            app.PullwiseHandler.route(result, "POST")

        self.assertEqual(result.status, HTTPStatus.OK)
        log_event.assert_called_once()
        self.assertEqual(log_event.call_args.args[0], "worker_job_result")
        self.assertEqual(log_event.call_args.kwargs["scanId"], "sc_result_diagnostics")
        self.assertEqual(log_event.call_args.kwargs["jobId"], job["job_id"])
        self.assertEqual(log_event.call_args.kwargs["attemptId"], f"wk_1-{job['attempt']}")
        self.assertEqual(log_event.call_args.kwargs["workerId"], "wk_1")
        self.assertEqual(log_event.call_args.kwargs["status"], "failed")
        self.assertEqual(log_event.call_args.kwargs["errorCode"], "GRAPH_VERIFIED_COMPLETION_FAILED")
        self.assertEqual(log_event.call_args.kwargs["error"], result_body["error"])
        self.assertEqual(log_event.call_args.kwargs["graphVerifiedRunId"], "run_diag")
        self.assertEqual(log_event.call_args.kwargs["graphVerifiedMode"], "standard")
        self.assertEqual(log_event.call_args.kwargs["graphVerifiedBlockedCount"], 97)
        self.assertEqual(log_event.call_args.kwargs["graphVerifiedFinderTasks"], 97)
        self.assertEqual(log_event.call_args.kwargs["graphVerifiedFinderBlocked"], 97)
        self.assertEqual(log_event.call_args.kwargs["graphVerifiedFinderCandidates"], 0)
        self.assertEqual(log_event.call_args.kwargs["graphVerifiedValidCandidates"], 0)

    def test_failed_worker_result_requeues_once_for_different_worker_without_extra_quota(self) -> None:
        _worker_two, worker_two_token = self.create_registry_worker("wk_2")
        worker_two_auth = {"Authorization": f"Bearer {worker_two_token}"}
        user = {"id": "usr_retry_worker", "name": "Owner", "providers": []}
        app.USERS = {user["id"]: user}
        repository = db.upsert_repository(
            {
                "github_repo_id": "12001",
                "full_name": "acme/retry-worker",
                "owner_login": "acme",
                "default_branch": "main",
                "private": False,
                "clone_url": "https://github.com/acme/retry-worker.git",
            }
        )
        quota_result = app.quota.consume_scan_quota(
            user=user,
            repository=repository,
            requested_by_user_id=user["id"],
            scan_id="sc_retry_worker",
            request_id="req_retry_worker",
        )
        scan = {
            "id": "sc_retry_worker",
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
            "requestId": "req_retry_worker",
            "quotaBucketIds": quota_result["bucketIds"],
            "billingUsage": quota_result["user"],
            "repoUsage": quota_result["repository"],
            "quotaState": "consumed",
            "quotaConsumedAt": app.now(),
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)

        first_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(first_claim, "POST")
        self.assertEqual(first_claim.status, HTTPStatus.OK)
        first_job = first_claim.payload["job"]

        first_failure_body = {
            "status": "failed",
            "attempt_id": f"wk_1-{first_job['attempt']}",
            "result_checksum": "checksum-worker-failed-first",
            "error": "Worker failed while running GraphVerified.",
            "error_code": "GRAPH_VERIFIED_COMPLETION_FAILED",
            **audit_result_fields([]),
            "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        }
        failed_result = RouteHarness(
            f"/worker/jobs/{first_job['job_id']}/result",
            first_failure_body,
            headers=self.auth,
        )
        app.PullwiseHandler.route(failed_result, "POST")
        self.assertEqual(failed_result.status, HTTPStatus.OK)
        self.assertTrue(failed_result.payload["retryQueued"])
        stored_after_failure = db.get_scan_job(first_job["job_id"])
        self.assertEqual(stored_after_failure["status"], "queued")
        self.assertEqual(stored_after_failure["attempt"], 1)
        self.assertEqual(app.SCANS[0]["status"], "queued")
        self.assertEqual(app.SCANS[0]["retry"]["attempt"], 1)
        self.assertEqual(app.SCANS[0]["retry"]["maxAttempts"], 2)
        self.assertEqual(app.SCANS[0]["retry"]["remainingAttempts"], 1)
        self.assertEqual(app.quota.quota_payload_for_user(user)["used"], 1)

        duplicate_failed_result = RouteHarness(
            f"/worker/jobs/{first_job['job_id']}/result",
            first_failure_body,
            headers=self.auth,
        )
        app.PullwiseHandler.route(duplicate_failed_result, "POST")
        self.assertEqual(duplicate_failed_result.status, HTTPStatus.OK)
        self.assertTrue(duplicate_failed_result.payload["duplicate"])
        self.assertFalse(duplicate_failed_result.payload.get("retryQueued", False))
        self.assertEqual(db.get_scan_job(first_job["job_id"])["status"], "queued")
        self.assertEqual(app.quota.quota_payload_for_user(user)["used"], 1)

        same_worker_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(same_worker_claim, "POST")
        self.assertEqual(same_worker_claim.status, HTTPStatus.OK)
        self.assertIsNone(same_worker_claim.payload["job"])

        second_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_2"}, headers=worker_two_auth)
        app.PullwiseHandler.route(second_claim, "POST")
        self.assertEqual(second_claim.status, HTTPStatus.OK)
        second_job = second_claim.payload["job"]
        self.assertEqual(second_job["job_id"], first_job["job_id"])
        self.assertEqual(second_job["attempt"], 2)
        self.assertEqual(second_job["retry"]["maxAttempts"], 2)

        late_duplicate_failed_result = RouteHarness(
            f"/worker/jobs/{first_job['job_id']}/result",
            first_failure_body,
            headers=self.auth,
        )
        app.PullwiseHandler.route(late_duplicate_failed_result, "POST")
        self.assertEqual(late_duplicate_failed_result.status, HTTPStatus.OK)
        self.assertTrue(late_duplicate_failed_result.payload["duplicate"])
        self.assertEqual(db.get_scan_job(first_job["job_id"])["status"], "claimed")

        done_result = RouteHarness(
            f"/worker/jobs/{second_job['job_id']}/result",
            {
                "status": "done",
                "attempt_id": f"wk_2-{second_job['attempt']}",
                "result_checksum": "checksum-worker-retry-done",
                **audit_result_fields([]),
                "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            },
            headers=worker_two_auth,
        )
        app.PullwiseHandler.route(done_result, "POST")
        self.assertEqual(done_result.status, HTTPStatus.OK)
        self.assertEqual(db.get_scan_job(first_job["job_id"])["status"], "done")
        self.assertEqual(app.SCANS[0]["status"], "done")
        self.assertEqual(app.quota.quota_payload_for_user(user)["used"], 1)

    def test_second_worker_failure_exhausts_auto_retry(self) -> None:
        _worker_two, worker_two_token = self.create_registry_worker("wk_2")
        worker_two_auth = {"Authorization": f"Bearer {worker_two_token}"}
        scan = {
            "id": "sc_retry_exhausted",
            "repo": "acme/retry-exhausted",
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

        first_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(first_claim, "POST")
        self.assertEqual(first_claim.status, HTTPStatus.OK)
        first_job = first_claim.payload["job"]

        first_failed_result = RouteHarness(
            f"/worker/jobs/{first_job['job_id']}/result",
            {
                "status": "failed",
                "attempt_id": f"wk_1-{first_job['attempt']}",
                "result_checksum": "checksum-worker-exhaust-first",
                "error": "First worker failed.",
                "error_code": "GRAPH_VERIFIED_COMPLETION_FAILED",
                **audit_result_fields([]),
                "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(first_failed_result, "POST")
        self.assertEqual(first_failed_result.status, HTTPStatus.OK)
        self.assertTrue(first_failed_result.payload["retryQueued"])

        second_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_2"}, headers=worker_two_auth)
        app.PullwiseHandler.route(second_claim, "POST")
        self.assertEqual(second_claim.status, HTTPStatus.OK)
        second_job = second_claim.payload["job"]
        self.assertEqual(second_job["attempt"], 2)

        second_failed_result = RouteHarness(
            f"/worker/jobs/{second_job['job_id']}/result",
            {
                "status": "failed",
                "attempt_id": f"wk_2-{second_job['attempt']}",
                "result_checksum": "checksum-worker-exhaust-second",
                "error": "Second worker failed.",
                "error_code": "GRAPH_VERIFIED_COMPLETION_FAILED",
                **audit_result_fields([]),
                "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            },
            headers=worker_two_auth,
        )
        app.PullwiseHandler.route(second_failed_result, "POST")
        self.assertEqual(second_failed_result.status, HTTPStatus.OK)
        self.assertFalse(second_failed_result.payload.get("retryQueued", False))
        stored = db.get_scan_job(first_job["job_id"])
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["attempt"], 2)
        self.assertEqual(db.scan_job_retry_state(stored)["remainingAttempts"], 0)
        self.assertEqual(app.SCANS[0]["status"], "failed")

    def test_retry_attempt_capacity_is_capped_by_enabled_worker_count(self) -> None:
        self.create_registry_worker("wk_2")
        self.create_registry_worker("wk_3")
        job = db.create_scan_job(
            {
                "job_id": "job_retry_cap",
                "scan_id": "sc_retry_cap",
                "repo": "acme/cap",
                "branch": "main",
                "commit": "pending",
                "status": "queued",
                "user_id": "usr_1",
                "max_attempts": 6,
            }
        )

        retry = db.scan_job_retry_state(job)

        self.assertEqual(retry["maxAttempts"], 3)
        self.assertEqual(retry["retryAttempts"], 2)

    def test_worker_graph_verified_progress_ignores_legacy_artifacts(self) -> None:
        scan = {
            "id": "sc_progress_graph_verified",
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
        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]
        self.assertEqual(job["agentConfig"]["graphVerified"], {"maxRepro": 0, "minScoreForRepro": 8, "requireRedGreen": False})

        progress = RouteHarness(
            f"/worker/jobs/{job['job_id']}/progress",
            {
                "phase": "ai",
                "progress": 60,
                "audit_swarm": {"protocol": "audit-swarm/0.1", "stage": "discovery"},
                "completion_audit": {"protocol": "completion-audit/0.1", "status": "warning"},
                "job_trace": {"protocol": "job-trace/0.1", "status": "running"},
                "repositoryGraph": repository_graph_v2_fixture(),
                "semanticGraph": semantic_graph_fixture(),
                "impactGraph": impact_graph_fixture(),
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(progress, "POST")

        self.assertEqual(progress.status, HTTPStatus.OK)
        payload = app.scan_payload(app.SCANS[0])
        self.assertEqual(payload["phase"], "ai")
        self.assertEqual(payload["progress"], 60)
        self.assertNotIn("auditSwarm", payload)
        self.assertNotIn("completionAudit", payload)
        self.assertNotIn("jobTrace", payload)
        self.assertNotIn("repositoryGraph", payload)
        self.assertNotIn("semanticGraph", payload)
        self.assertNotIn("impactGraph", payload)

    def test_worker_ai_progress_consumes_reserved_scan_quota(self) -> None:
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

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)
        job = claim.payload["job"]
        self.assertEqual(app.quota.quota_payload_for_user(user)["used"], 0)
        self.assertEqual(app.quota.quota_payload_for_user(user)["reserved"], 1)

        progress = RouteHarness(
            f"/worker/jobs/{job['job_id']}/progress",
            {"phase": "ai", "progress": 50},
            headers=self.auth,
        )
        app.PullwiseHandler.route(progress, "POST")

        self.assertEqual(progress.status, HTTPStatus.OK)
        self.assertEqual(app.quota.quota_payload_for_user(user)["used"], 1)
        self.assertEqual(app.quota.quota_payload_for_user(user)["reserved"], 0)
        payload = app.scan_payload(app.SCANS[0])
        self.assertEqual(payload["quotaState"], "consumed")
        self.assertEqual(payload["billingUsage"]["used"], 1)
        self.assertEqual(payload["billingUsage"]["reserved"], 0)

    def test_retry_consumes_quota_and_requeues_failed_scan(self) -> None:
        user = {"id": "usr_1", "name": "Owner", "providers": []}
        app.USERS = {"usr_1": user}
        app.SESSIONS = {"ses_owner": {"id": "ses_owner", "userId": "usr_1", "createdAt": app.now(), "expiresAt": app.now() + 3600}}
        repository = db.upsert_repository(
            {
                "github_repo_id": "12001",
                "full_name": "acme/retry",
                "owner_login": "acme",
                "default_branch": "main",
            }
        )
        initial_quota = app.quota.consume_scan_quota(
            user=user,
            repository=repository,
            requested_by_user_id=user["id"],
            scan_id="sc_retry_route",
            request_id="req_initial",
        )
        scan = {
            "id": "sc_retry_route",
            "repo": repository["full_name"],
            "branch": "main",
            "commit": "pending",
            "status": "failed",
            "userId": user["id"],
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "completedAt": app.now(),
            "progress": 80,
            "phase": "report",
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "repoId": repository["id"],
            "githubRepoId": repository["github_repo_id"],
            "requestId": "req_initial",
            "quotaBucketIds": initial_quota["bucketIds"],
            "billingUsage": initial_quota["user"],
            "repoUsage": initial_quota["repository"],
            "error": "worker failed",
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)
        connection = db.connect()
        try:
            connection.execute("UPDATE scan_jobs SET status = 'failed' WHERE scan_id = ?", (scan["id"],))
            connection.commit()
        finally:
            connection.close()

        retry = RouteHarness(
            "/scans/sc_retry_route/retry",
            {"requestId": "req_retry_route"},
            headers={"Cookie": f"{app.SESSION_COOKIE}=ses_owner"},
        )
        app.PullwiseHandler.route(retry, "POST")

        self.assertEqual(retry.status, HTTPStatus.CREATED)
        self.assertEqual(retry.payload["status"], "queued")
        self.assertEqual(app.SCANS[0]["requestId"], "req_retry_route")
        user_quota = app.quota.quota_payload_for_user(user)
        repo_quota = app.quota.quota_payload_for_repository(repository, user)
        self.assertEqual(user_quota["used"], 1)
        self.assertEqual(user_quota["reserved"], 1)
        self.assertEqual(repo_quota["used"], 1)
        self.assertEqual(repo_quota["reserved"], 1)
        self.assertEqual(db.get_scan_job_for_scan("sc_retry_route")["status"], "queued")
        self.assertEqual(app.SCANS[0]["progress"], 0)

    def test_retry_repository_too_large_refunds_only_current_retry_request(self) -> None:
        user = {"id": "usr_1", "name": "Owner", "providers": []}
        app.USERS = {"usr_1": user}
        app.SESSIONS = {"ses_owner": {"id": "ses_owner", "userId": "usr_1", "createdAt": app.now(), "expiresAt": app.now() + 3600}}
        repository = db.upsert_repository(
            {
                "github_repo_id": "12002",
                "full_name": "acme/retry-large",
                "owner_login": "acme",
                "default_branch": "main",
            }
        )
        initial_quota = app.quota.consume_scan_quota(
            user=user,
            repository=repository,
            requested_by_user_id=user["id"],
            scan_id="sc_retry_large",
            request_id="req_initial_large",
        )
        scan = {
            "id": "sc_retry_large",
            "repo": repository["full_name"],
            "branch": "main",
            "commit": "pending",
            "status": "failed",
            "userId": user["id"],
            "createdAt": app.now(),
            "queuedAt": app.now(),
            "completedAt": app.now(),
            "progress": 90,
            "phase": "report",
            "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "repoId": repository["id"],
            "githubRepoId": repository["github_repo_id"],
            "requestId": "req_initial_large",
            "quotaBucketIds": initial_quota["bucketIds"],
            "billingUsage": initial_quota["user"],
            "repoUsage": initial_quota["repository"],
        }
        app.SCANS = [scan]
        app.create_scan_job_for_scan(scan)
        connection = db.connect()
        try:
            connection.execute("UPDATE scan_jobs SET status = 'failed' WHERE scan_id = ?", (scan["id"],))
            connection.commit()
        finally:
            connection.close()

        retry = RouteHarness(
            "/scans/sc_retry_large/retry",
            {"requestId": "req_retry_large"},
            headers={"Cookie": f"{app.SESSION_COOKIE}=ses_owner"},
        )
        app.PullwiseHandler.route(retry, "POST")
        self.assertEqual(retry.status, HTTPStatus.CREATED)
        self.assertEqual(retry.payload["status"], "queued")
        self.assertEqual(app.SCANS[0]["requestId"], "req_retry_large")
        user_quota = app.quota.quota_payload_for_user(user)
        self.assertEqual(user_quota["used"], 1)
        self.assertEqual(user_quota["reserved"], 1)

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)
        claimed_job = claim.payload["job"]
        result = RouteHarness(
            f"/worker/jobs/{claimed_job['job_id']}/result",
            {
                "status": "failed",
                "attempt_id": f"wk_1-{claimed_job['attempt']}",
                "result_checksum": "checksum-retry-repository-too-large",
                "error": "Repository is too large for Pullwise scanning.",
                "error_code": "REPOSITORY_TOO_LARGE",
                **audit_result_fields([]),
                "preflight": {
                    "mode": "static",
                    "execution": "repository_limit_check",
                    "repositoryStats": {"fileCount": 2001, "totalBytes": 50 * 1024 * 1024 + 1},
                    "repositoryLimits": {"maxFiles": 2000, "maxBytes": 50 * 1024 * 1024},
                    "repositoryLimitExceeded": True,
                    "repositoryLimitReasons": ["file_count"],
                },
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(result, "POST")

        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertEqual(result.payload["quotaRelease"]["ledgerRows"], 2)
        self.assertEqual(app.quota.quota_payload_for_user(user)["used"], 1)
        self.assertEqual(app.quota.quota_payload_for_user(user)["reserved"], 0)
        self.assertEqual(app.quota.quota_payload_for_repository(repository, user)["used"], 1)
        self.assertEqual(app.quota.quota_payload_for_repository(repository, user)["reserved"], 0)
        self.assertEqual(app.SCANS[0]["requestId"], "req_retry_large")

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
        self.assertTrue(payload["graphVerified"])
        self.assertEqual(payload["commit"], resolved_commit)
        self.assertEqual(payload["verificationLevel"], "L2")
        self.assertIn(f"/blob/{resolved_commit}/src/app.py#L12", payload["affectedLocations"][0]["url"])
        self.assertEqual(payload["codeEvidence"][0]["file"], "src/app.py")
        self.assertNotIn("audit", payload)
        self.assertNotIn("verificationStatus", payload)

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

    def test_claim_payload_ignores_previous_convergence_context_across_workers(self) -> None:
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
        self.assertNotIn("convergenceState", app.SCANS[0])

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
        self.assertNotIn("convergence_context", second_claim.payload["job"])

    def test_claim_payload_ignores_stored_convergence_context_by_canonical_scope(self) -> None:
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
        self.assertNotIn("convergence_context", claim.payload["job"])

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
        self.assertNotIn("file", app.issue_payload(app.ISSUES[0]))

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

    def test_worker_claim_ignores_client_free_slots_and_claims_one_job(self) -> None:
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
        self.assertEqual([job["scan_id"] for job in claim.payload["jobs"]], ["sc_no_slots"])
        self.assertIsNotNone(claim.payload["job"])
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

        first_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1", "max_jobs": 4}, headers=self.auth)
        app.PullwiseHandler.route(first_claim, "POST")

        self.assertEqual(first_claim.status, HTTPStatus.OK)
        self.assertEqual([job["scan_id"] for job in first_claim.payload["jobs"]], ["sc_refill_1"])
        first_job = first_claim.payload["job"]

        blocked_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1", "max_jobs": 4}, headers=self.auth)
        app.PullwiseHandler.route(blocked_claim, "POST")
        self.assertEqual(blocked_claim.status, HTTPStatus.OK)
        self.assertEqual(blocked_claim.payload["jobs"], [])
        self.assertIsNone(blocked_claim.payload["job"])

        result = RouteHarness(
            f"/worker/jobs/{first_job['job_id']}/result",
            {
                "status": "done",
                "attempt_id": f"wk_1-{first_job['attempt']}",
                "result_checksum": f"checksum-{first_job['job_id']}",
                **audit_result_fields([]),
                "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(result, "POST")
        self.assertEqual(result.status, HTTPStatus.OK)

        refill_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1", "max_jobs": 4}, headers=self.auth)
        app.PullwiseHandler.route(refill_claim, "POST")

        self.assertEqual(refill_claim.status, HTTPStatus.OK)
        self.assertEqual([job["scan_id"] for job in refill_claim.payload["jobs"]], ["sc_refill_2"])

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
        self.assertEqual(len(claimed_job_ids), 2)
        self.assertEqual(len(set(claimed_job_ids)), 2)
        self.assertEqual(claimed_scan_ids, ["sc_multi_1", "sc_multi_2"])
        self.assertEqual(app.SCANS[2]["status"], "queued")
        queue = app.scan_queue_payload(app.SCANS[2])
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

        next_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1", "max_jobs": 2}, headers=self.auth)
        app.PullwiseHandler.route(next_claim, "POST")

        self.assertEqual(next_claim.status, HTTPStatus.OK)
        self.assertEqual([job["scan_id"] for job in next_claim.payload["jobs"]], ["sc_multi_3"])

        remaining_job = next_claim.payload["job"]
        for expected_scan_id in ("sc_multi_3", "sc_multi_4", "sc_multi_5"):
            self.assertEqual(remaining_job["scan_id"], expected_scan_id)
            final_result = RouteHarness(
                f"/worker/jobs/{remaining_job['job_id']}/result",
                {
                    "status": "done",
                    "attempt_id": f"wk_1-{remaining_job['attempt']}",
                    "result_checksum": f"checksum-{remaining_job['job_id']}",
                    **audit_result_fields([]),
                    "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                },
                headers=self.auth,
            )
            app.PullwiseHandler.route(final_result, "POST")
            self.assertEqual(final_result.status, HTTPStatus.OK)
            if expected_scan_id != "sc_multi_5":
                followup_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1", "max_jobs": 2}, headers=self.auth)
                app.PullwiseHandler.route(followup_claim, "POST")
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
        first_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(first_claim, "POST")
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

        second_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        app.PullwiseHandler.route(second_claim, "POST")

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
        claimed = db.claim_next_scan_job("wk_1", timestamp=app.now())
        db.update_scan_job_progress(
            job["job_id"],
            {"phase": "ai", "progress": 80, "message": "reviewing"},
        )
        result = RouteHarness(
            f"/worker/jobs/{claimed['job_id']}/result",
            {
                "status": "done",
                "attempt_id": "wk_1-1",
                "result_checksum": "checksum-stale-done",
                **audit_result_fields(
                    [audit_issue_card("Completed finding", issue_id="issue-stale-done", severity="P1")]
                ),
                "summary": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
            },
            headers=self.auth,
        )
        app.PullwiseHandler.route(result, "POST")
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

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        with patch("pullwise_server.app.now", return_value=timestamp):
            app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)
        original_timeout_at = db.get_scan_job(job["job_id"])["timeout_at"]
        self.assertLess(original_timeout_at, timestamp + 3700)

        heartbeat = RouteHarness(
            "/worker/heartbeat",
            {
                "worker_id": "wk_1",
                "version": "0.1.0",
                "provider": "codex",
                "max_concurrent_jobs": 2,
                "running_jobs": 1,
                "free_slots": 1,
                "doctor_status": "ok",
                "codex_ready": True,
                "active_job_ids": [job["job_id"]],
            },
            headers=self.auth,
        )
        with patch("pullwise_server.app.now", return_value=timestamp + 3700):
            app.PullwiseHandler.route(heartbeat, "POST")
        self.assertEqual(heartbeat.status, HTTPStatus.OK)

        stored = db.get_scan_job(job["job_id"])
        self.assertEqual(stored["status"], "claimed")
        self.assertEqual(stored["claimed_by_worker_id"], "wk_1")
        self.assertGreater(stored["timeout_at"], original_timeout_at)
        self.assertEqual(stored["timeout_at"], timestamp + 7300)
        self.assertEqual(db.recover_expired_scan_jobs(timestamp + 3701), [])
        self.assertEqual(db.get_scan_job(job["job_id"])["status"], "claimed")

    def test_worker_heartbeat_reports_cancelled_active_job_ids(self) -> None:
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
        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        with patch("pullwise_server.app.now", return_value=timestamp):
            app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)
        db.cancel_scan_job_for_scan(scan["id"])

        heartbeat = RouteHarness(
            "/worker/heartbeat",
            {
                "worker_id": "wk_1",
                "version": "0.1.0",
                "provider": "codex",
                "running_jobs": 1,
                "doctor_status": "ok",
                "codex_ready": True,
                "active_job_ids": [job["job_id"]],
            },
            headers=self.auth,
        )
        with patch("pullwise_server.app.now", return_value=timestamp + 1):
            app.PullwiseHandler.route(heartbeat, "POST")

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
        first_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        with patch("pullwise_server.app.now", return_value=timestamp):
            app.PullwiseHandler.route(first_claim, "POST")
        self.assertEqual(first_claim.status, HTTPStatus.OK)
        db.cancel_scan_job_for_scan(first_scan["id"])

        stale_heartbeat = RouteHarness(
            "/worker/heartbeat",
            {
                "worker_id": "wk_1",
                "version": "0.1.0",
                "provider": "codex",
                "running_jobs": 1,
                "doctor_status": "ok",
                "codex_ready": True,
                "active_job_ids": [first_job["job_id"]],
            },
            headers=self.auth,
        )
        with patch("pullwise_server.app.now", return_value=timestamp + 1):
            app.PullwiseHandler.route(stale_heartbeat, "POST")
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
        second_claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        with patch("pullwise_server.app.now", return_value=timestamp + 2):
            app.PullwiseHandler.route(second_claim, "POST")

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

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        with patch("pullwise_server.app.now", return_value=timestamp):
            app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)
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

        heartbeat = RouteHarness(
            "/worker/heartbeat",
            {
                "worker_id": "wk_1",
                "version": "0.1.0",
                "provider": "codex",
                "max_concurrent_jobs": 2,
                "running_jobs": 0,
                "free_slots": 2,
                "doctor_status": "ok",
                "codex_ready": True,
                "active_job_ids": [],
            },
            headers=self.auth,
        )
        with patch("pullwise_server.app.now", return_value=timestamp + 121):
            app.PullwiseHandler.route(heartbeat, "POST")
        self.assertEqual(heartbeat.status, HTTPStatus.OK)

        stored = db.get_scan_job(job["job_id"])
        self.assertEqual(stored["status"], "queued")
        self.assertEqual(stored["claimed_by_worker_id"], None)
        self.assertEqual(stored["claimed_at"], None)
        self.assertEqual(stored["started_at"], None)
        self.assertEqual(stored["timeout_at"], None)
        self.assertEqual(stored["error"], "worker_job_startup_lost")
        self.assertEqual(app.SCANS[0]["status"], "queued")
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

        claim = RouteHarness("/worker/jobs/claim", {"worker_id": "wk_1"}, headers=self.auth)
        with patch("pullwise_server.app.now", return_value=timestamp):
            app.PullwiseHandler.route(claim, "POST")
        self.assertEqual(claim.status, HTTPStatus.OK)
        connection = db.connect()
        try:
            with connection:
                connection.execute(
                    "UPDATE scan_jobs SET claimed_at = ?, timeout_at = ? WHERE job_id = ?",
                    (timestamp, timestamp + 3600, job["job_id"]),
                )
        finally:
            connection.close()

        heartbeat = RouteHarness(
            "/worker/heartbeat",
            {
                "worker_id": "wk_1",
                "version": "0.1.0",
                "provider": "codex",
                "max_concurrent_jobs": 2,
                "running_jobs": 0,
                "free_slots": 2,
                "doctor_status": "ok",
                "codex_ready": True,
                "active_job_ids": [],
            },
            headers=self.auth,
        )
        with patch("pullwise_server.app.now", return_value=timestamp + 119):
            app.PullwiseHandler.route(heartbeat, "POST")
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
