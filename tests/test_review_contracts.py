from __future__ import annotations

import math
import os
import tempfile
import unittest
from unittest.mock import patch

from pullwise_server import review


class ReviewContractsTest(unittest.TestCase):
    def test_parse_findings_json_ignores_trailing_provider_logs(self) -> None:
        raw = 'INFO starting review\n{"findings":[{"id":"f_1","title":"Issue"}]}\nINFO review complete'

        findings = review._parse_findings_json(raw)

        self.assertEqual(findings, [{"id": "f_1", "title": "Issue"}])

    def test_parse_findings_json_skips_structured_logs_before_findings(self) -> None:
        raw = '{"event":"review_started"}\n{"findings":[{"id":"f_1","title":"Issue"}]}'

        findings = review._parse_findings_json(raw)

        self.assertEqual(findings, [{"id": "f_1", "title": "Issue"}])

    def test_parse_findings_json_skips_nested_log_payload_findings(self) -> None:
        raw = (
            '{"event":"review_progress","payload":{"findings":[]}}\n'
            '{"findings":[{"id":"f_1","title":"Issue"}]}'
        )

        findings = review._parse_findings_json(raw)

        self.assertEqual(findings, [{"id": "f_1", "title": "Issue"}])

    def test_parse_findings_json_skips_top_level_log_findings(self) -> None:
        raw = (
            '{"event":"review_progress","findings":[]}\n'
            '{"findings":[{"id":"f_1","title":"Issue"}]}'
        )

        findings = review._parse_findings_json(raw)

        self.assertEqual(findings, [{"id": "f_1", "title": "Issue"}])

    def test_parse_findings_json_treats_malformed_findings_field_as_empty(self) -> None:
        findings = review._parse_findings_json('{"findings":{"id":"f_bad","title":"Issue"}}')

        self.assertEqual(findings, [])

    def test_run_review_sanitizes_malformed_provider_finding_fields(self) -> None:
        malformed_finding = {
            "id": {"value": "f_bad"},
            "severity": {"level": "critical"},
            "category": ["Security"],
            "title": {"text": "Unsafe redirect"},
            "summary": ["bad"],
            "impact": {"risk": "high"},
            "file": {"path": "app.py"},
            "line": float("inf"),
            "confidence": float("nan"),
            "autoFix": "false",
            "effort": {"minutes": 5},
            "tags": {"name": "security"},
            "steps": "Patch redirect handling",
            "badCode": {"code": "redirect(next_url)"},
            "goodCode": {"code": "redirect(safe_redirect(next_url))"},
            "references": {"label": "docs", "url": "https://example.com"},
        }

        with (
            patch.dict(os.environ, {"PULLWISE_REVIEW_PROVIDER": "mock"}, clear=False),
            patch.object(review, "_run_mock", return_value=[malformed_finding]),
        ):
            findings = review.run_review(
                repo="owner/repo",
                branch="main",
                commit="pending",
                user_id="usr_1",
                scan_id="sc_1",
            )

        self.assertEqual(1, len(findings))
        finding = findings[0]
        self.assertRegex(finding["id"], r"^f_")
        self.assertEqual(finding["severity"], "medium")
        self.assertEqual(finding["category"], "Quality")
        self.assertEqual(finding["title"], "Untitled finding")
        self.assertEqual(finding["summary"], "")
        self.assertEqual(finding["impact"], "")
        self.assertEqual(finding["detectionReasoning"], "")
        self.assertEqual(finding["reproductionPath"], "")
        self.assertEqual(finding["file"], "")
        self.assertEqual(finding["line"], 0)
        self.assertEqual(finding["confidence"], 0.0)
        self.assertFalse(math.isnan(finding["confidence"]))
        self.assertEqual(finding["confidenceRationale"], "")
        self.assertIs(finding["autoFix"], False)
        self.assertEqual(finding["effort"], "-")
        self.assertEqual(finding["fixBenefits"], "")
        self.assertEqual(finding["fixRisks"], "")
        self.assertEqual(finding["tags"], [])
        self.assertEqual(finding["steps"], [])
        self.assertEqual(finding["badCode"], [])
        self.assertEqual(finding["goodCode"], [])
        self.assertEqual(finding["references"], [])

    def test_run_review_downgrades_verified_without_runtime_evidence(self) -> None:
        provider_finding = {
            "id": "f_static_only",
            "title": "Issue",
            "file": "src/app.py",
            "line": 7,
            "verificationStatus": "verified",
            "affectedLocations": [{"file": "src/app.py", "startLine": 7, "endLine": 9}],
            "evidence": [
                {
                    "type": "code",
                    "label": "Bounds check",
                    "summary": "The branch does not validate the lower bound.",
                    "file": "src/app.py",
                    "startLine": 7,
                    "endLine": 9,
                    "command": "",
                    "exitCode": 0,
                    "logPath": "",
                    "url": "",
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

        with (
            patch.dict(os.environ, {"PULLWISE_REVIEW_PROVIDER": "mock"}, clear=False),
            patch.object(review, "_run_mock", return_value=[provider_finding]),
        ):
            findings = review.run_review(
                repo="owner/repo",
                branch="main",
                commit="pending",
                user_id="usr_1",
                scan_id="sc_1",
            )

        finding = findings[0]
        self.assertEqual(finding["verificationStatus"], "static_proof")
        self.assertEqual(finding["affectedLocations"], [{"file": "src/app.py", "startLine": 7, "endLine": 9}])
        self.assertEqual(finding["evidence"][0]["type"], "code")

    def test_run_review_downgrades_verified_runtime_without_reproduction_command(self) -> None:
        provider_finding = {
            "id": "f_runtime_no_repro",
            "title": "Issue",
            "file": "src/app.py",
            "line": 7,
            "verificationStatus": "verified",
            "affectedLocations": [{"file": "src/app.py", "startLine": 7, "endLine": 9}],
            "evidence": [
                {
                    "type": "runtime_log",
                    "label": "Focused test",
                    "summary": "The focused command failed.",
                    "file": "",
                    "startLine": 0,
                    "endLine": 0,
                    "command": "pytest tests/repro/test_bounds.py",
                    "exitCode": 1,
                    "logPath": "logs/f_runtime_no_repro.log",
                    "url": "",
                }
            ],
            "reproduction": {
                "commands": [],
                "input": "",
                "expected": "",
                "actual": "Command exited 1.",
                "testFile": "",
                "logPath": "logs/f_runtime_no_repro.log",
            },
        }

        with (
            patch.dict(os.environ, {"PULLWISE_REVIEW_PROVIDER": "mock"}, clear=False),
            patch.object(review, "_run_mock", return_value=[provider_finding]),
        ):
            findings = review.run_review(
                repo="owner/repo",
                branch="main",
                commit="pending",
                user_id="usr_1",
                scan_id="sc_1",
            )

        self.assertEqual(findings[0]["verificationStatus"], "static_proof")

    def test_run_review_downgrades_verified_runtime_without_raw_output(self) -> None:
        provider_finding = {
            "id": "f_runtime_no_raw",
            "title": "Issue",
            "file": "src/app.py",
            "line": 7,
            "verificationStatus": "verified",
            "affectedLocations": [{"file": "src/app.py", "startLine": 7, "endLine": 9}],
            "evidence": [
                {
                    "type": "runtime_log",
                    "label": "Focused test",
                    "summary": "A focused command was suggested.",
                    "file": "src/app.py",
                    "startLine": 7,
                    "endLine": 9,
                    "command": "pytest tests/repro/test_bounds.py",
                    "logPath": "",
                    "url": "",
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

        with (
            patch.dict(os.environ, {"PULLWISE_REVIEW_PROVIDER": "mock"}, clear=False),
            patch.object(review, "_run_mock", return_value=[provider_finding]),
        ):
            findings = review.run_review(
                repo="owner/repo",
                branch="main",
                commit="pending",
                user_id="usr_1",
                scan_id="sc_1",
            )

        self.assertEqual(findings[0]["verificationStatus"], "static_proof")

    def test_run_review_downgrades_verified_runtime_with_only_exit_code(self) -> None:
        provider_finding = {
            "id": "f_exit_code_only",
            "title": "Issue",
            "file": "src/app.py",
            "line": 7,
            "verificationStatus": "verified",
            "affectedLocations": [{"file": "src/app.py", "startLine": 7, "endLine": 9}],
            "evidence": [
                {
                    "type": "runtime_log",
                    "label": "Focused test",
                    "summary": "The focused command exited non-zero, but no output was captured.",
                    "file": "",
                    "startLine": 0,
                    "endLine": 0,
                    "command": "pytest tests/repro/test_bounds.py",
                    "exitCode": 1,
                    "logPath": "",
                    "output": "",
                    "url": "",
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

        with (
            patch.dict(os.environ, {"PULLWISE_REVIEW_PROVIDER": "mock"}, clear=False),
            patch.object(review, "_run_mock", return_value=[provider_finding]),
        ):
            findings = review.run_review(
                repo="owner/repo",
                branch="main",
                commit="pending",
                user_id="usr_1",
                scan_id="sc_1",
            )

        finding = findings[0]
        self.assertEqual(finding["verificationStatus"], "static_proof")
        self.assertEqual(finding["evidence"][0]["exitCode"], 1)

    def test_run_review_keeps_verified_with_reproduction_and_runtime_log(self) -> None:
        provider_finding = {
            "id": "f_runtime_verified",
            "title": "Issue",
            "file": "src/app.py",
            "line": 7,
            "verificationStatus": "verified",
            "affectedLocations": [{"file": "src/app.py", "startLine": 7, "endLine": 9}],
            "evidence": [
                {
                    "type": "runtime_log",
                    "label": "Focused test",
                    "summary": "The focused command failed with the observed error.",
                    "file": "",
                    "startLine": 0,
                    "endLine": 0,
                    "command": "pytest tests/repro/test_bounds.py",
                    "exitCode": 1,
                    "logPath": "logs/f_runtime_verified.log",
                    "output": "FAIL tests/repro/test_bounds.py\nAssertionError: expected validation error",
                    "url": "",
                }
            ],
            "reproduction": {
                "commands": ["pytest tests/repro/test_bounds.py"],
                "input": "",
                "expected": "validation error",
                "actual": "Command exited 1.",
                "testFile": "tests/repro/test_bounds.py",
                "logPath": "logs/f_runtime_verified.log",
            },
        }

        with (
            patch.dict(os.environ, {"PULLWISE_REVIEW_PROVIDER": "mock"}, clear=False),
            patch.object(review, "_run_mock", return_value=[provider_finding]),
        ):
            findings = review.run_review(
                repo="owner/repo",
                branch="main",
                commit="pending",
                user_id="usr_1",
                scan_id="sc_1",
            )

        self.assertEqual(findings[0]["verificationStatus"], "verified")
        self.assertIn("AssertionError", findings[0]["evidence"][0]["output"])

    def test_run_review_preserves_runtime_output_as_raw_evidence(self) -> None:
        provider_finding = {
            "id": "f_runtime_output",
            "title": "Issue",
            "file": "src/app.py",
            "line": 7,
            "verificationStatus": "verified",
            "affectedLocations": [{"file": "src/app.py", "startLine": 7, "endLine": 9}],
            "evidence": [
                {
                    "type": "runtime_log",
                    "label": "Focused test",
                    "summary": "The focused command failed with the observed assertion.",
                    "file": "",
                    "startLine": 0,
                    "endLine": 0,
                    "command": "pytest tests/repro/test_bounds.py",
                    "exitCode": 0,
                    "logPath": "",
                    "output": "FAIL tests/repro/test_bounds.py\nAssertionError: expected 400 received 500",
                    "url": "",
                }
            ],
            "reproduction": {
                "commands": ["pytest tests/repro/test_bounds.py"],
                "input": "",
                "expected": "400 validation error",
                "actual": "",
                "testFile": "",
                "logPath": "",
            },
        }

        with (
            patch.dict(os.environ, {"PULLWISE_REVIEW_PROVIDER": "mock"}, clear=False),
            patch.object(review, "_run_mock", return_value=[provider_finding]),
        ):
            findings = review.run_review(
                repo="owner/repo",
                branch="main",
                commit="pending",
                user_id="usr_1",
                scan_id="sc_1",
            )

        finding = findings[0]
        self.assertEqual(finding["verificationStatus"], "verified")
        self.assertEqual(
            finding["evidence"][0]["output"],
            "FAIL tests/repro/test_bounds.py\nAssertionError: expected 400 received 500",
        )

    def test_run_review_ignores_non_object_findings_and_sanitizes_code_rows(self) -> None:
        provider_findings = [
            "not a finding",
            {
                "id": " f_valid ",
                "severity": "HIGH",
                "category": "security",
                "title": " Unsafe redirect ",
                "summary": " User-controlled redirect. ",
                "impact": " Phishing risk. ",
                "file": " app.py ",
                "line": "12",
                "confidence": 2,
                "autoFix": True,
                "effort": " 5 min ",
                "tags": [" redirect ", {"bad": "tag"}, ""],
                "steps": [" Validate redirect targets. ", {"bad": "step"}],
                "badCode": [
                    {"ln": float("inf"), "code": "return redirect(next_url)", "t": ["del"]},
                    {"ln": 13, "code": {"text": "bad"}, "t": "del"},
                ],
                "goodCode": [
                    {"ln": -1, "code": "return redirect(safe_redirect(next_url))", "t": "add"},
                ],
                "references": [
                    {"label": "Bad", "url": "javascript:alert(1)"},
                    {"label": " Docs ", "url": " https://example.com/docs "},
                    {"label": {"bad": "label"}, "url": "https://example.com/bad"},
                ],
            },
        ]

        with (
            patch.dict(os.environ, {"PULLWISE_REVIEW_PROVIDER": "mock"}, clear=False),
            patch.object(review, "_run_mock", return_value=provider_findings),
        ):
            findings = review.run_review(
                repo="owner/repo",
                branch="main",
                commit="pending",
                user_id="usr_1",
                scan_id="sc_1",
            )

        self.assertEqual(1, len(findings))
        finding = findings[0]
        self.assertEqual(finding["id"], "f_valid")
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(finding["category"], "Security")
        self.assertEqual(finding["title"], "Unsafe redirect")
        self.assertEqual(finding["summary"], "User-controlled redirect.")
        self.assertEqual(finding["impact"], "Phishing risk.")
        self.assertEqual(finding["file"], "app.py")
        self.assertEqual(finding["line"], 12)
        self.assertEqual(finding["confidence"], 1.0)
        self.assertIs(finding["autoFix"], True)
        self.assertEqual(finding["effort"], "5 min")
        self.assertEqual(finding["tags"], ["redirect"])
        self.assertEqual(finding["steps"], ["Validate redirect targets."])
        self.assertEqual(finding["badCode"], [{"ln": 0, "code": "return redirect(next_url)", "t": None}])
        self.assertEqual(finding["goodCode"], [{"ln": 0, "code": "return redirect(safe_redirect(next_url))", "t": "add"}])
        self.assertEqual(finding["references"], [{"label": "Docs", "url": "https://example.com/docs"}])

    def test_run_review_downgrades_auto_fix_with_empty_replacement(self) -> None:
        provider_finding = {
            "id": "f_empty_fix",
            "title": "Issue",
            "file": "src/app.py",
            "autoFix": True,
            "badCode": [{"ln": 1, "code": "return redirect(next_url)", "t": "del"}],
            "goodCode": [],
        }

        with tempfile.TemporaryDirectory() as repo_path:
            os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
            with open(os.path.join(repo_path, "src", "app.py"), "w", encoding="utf-8") as handle:
                handle.write("return redirect(next_url)\n")

            with (
                patch.dict(os.environ, {"PULLWISE_REVIEW_PROVIDER": "mock"}, clear=False),
                patch.object(review, "_run_mock", return_value=[provider_finding]),
            ):
                findings = review.run_review(
                    repo="owner/repo",
                    branch="main",
                    commit="pending",
                    user_id="usr_1",
                    scan_id="sc_1",
                    repo_path=repo_path,
                )

        finding = findings[0]
        self.assertIs(finding["autoFix"], False)
        self.assertEqual(finding["badCode"], [])
        self.assertEqual(finding["goodCode"], [])

    def test_run_review_downgrades_auto_fix_when_bad_code_is_not_contiguous(self) -> None:
        provider_finding = {
            "id": "f_non_contiguous",
            "title": "Issue",
            "file": "src/app.py",
            "autoFix": True,
            "badCode": [
                {"ln": 1, "code": "const token = readToken()", "t": "del"},
                {"ln": 3, "code": "return redirect(nextUrl)", "t": "del"},
            ],
            "goodCode": [{"ln": 1, "code": "return safeRedirect(nextUrl)", "t": "add"}],
        }

        with tempfile.TemporaryDirectory() as repo_path:
            os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
            with open(os.path.join(repo_path, "src", "app.py"), "w", encoding="utf-8") as handle:
                handle.write(
                    "const token = readToken()\n"
                    "audit(token)\n"
                    "return redirect(nextUrl)\n"
                )

            with (
                patch.dict(os.environ, {"PULLWISE_REVIEW_PROVIDER": "mock"}, clear=False),
                patch.object(review, "_run_mock", return_value=[provider_finding]),
            ):
                findings = review.run_review(
                    repo="owner/repo",
                    branch="main",
                    commit="pending",
                    user_id="usr_1",
                    scan_id="sc_1",
                    repo_path=repo_path,
                )

        finding = findings[0]
        self.assertIs(finding["autoFix"], False)
        self.assertEqual(finding["badCode"], [])
        self.assertEqual(finding["goodCode"], [])

    def test_run_review_normalizes_absolute_provider_file_inside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as repo_path:
            os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
            app_path = os.path.join(repo_path, "src", "app.py")
            with open(app_path, "w", encoding="utf-8") as handle:
                handle.write("return redirect(next_url)\n")

            provider_finding = {
                "id": "f_absolute_path",
                "title": "Issue",
                "file": app_path,
                "autoFix": True,
                "badCode": [{"ln": 1, "code": "return redirect(next_url)", "t": "del"}],
                "goodCode": [{"ln": 1, "code": "return redirect(safe_redirect(next_url))", "t": "add"}],
            }

            with (
                patch.dict(os.environ, {"PULLWISE_REVIEW_PROVIDER": "mock"}, clear=False),
                patch.object(review, "_run_mock", return_value=[provider_finding]),
            ):
                findings = review.run_review(
                    repo="owner/repo",
                    branch="main",
                    commit="pending",
                    user_id="usr_1",
                    scan_id="sc_1",
                    repo_path=repo_path,
                )

        finding = findings[0]
        self.assertEqual(finding["file"], "src/app.py")
        self.assertIs(finding["autoFix"], True)
        self.assertEqual(finding["badCode"], [{"ln": 1, "code": "return redirect(next_url)", "t": "del"}])

    def test_run_review_downgrades_auto_fix_when_target_file_is_not_utf8(self) -> None:
        provider_finding = {
            "id": "f_binary",
            "title": "Issue",
            "file": "src/app.py",
            "autoFix": True,
            "badCode": [{"ln": 1, "code": "return redirect(next_url)", "t": "del"}],
            "goodCode": [{"ln": 1, "code": "return redirect(safe_redirect(next_url))", "t": "add"}],
        }

        with tempfile.TemporaryDirectory() as repo_path:
            os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
            with open(os.path.join(repo_path, "src", "app.py"), "wb") as handle:
                handle.write(b"\xff\xfe\x00")

            with (
                patch.dict(os.environ, {"PULLWISE_REVIEW_PROVIDER": "mock"}, clear=False),
                patch.object(review, "_run_mock", return_value=[provider_finding]),
            ):
                findings = review.run_review(
                    repo="owner/repo",
                    branch="main",
                    commit="pending",
                    user_id="usr_1",
                    scan_id="sc_1",
                    repo_path=repo_path,
                )

        finding = findings[0]
        self.assertIs(finding["autoFix"], False)
        self.assertEqual(finding["badCode"], [])
        self.assertEqual(finding["goodCode"], [])

    def test_run_review_clears_code_blocks_when_auto_fix_is_false(self) -> None:
        provider_finding = {
            "id": "f_manual",
            "title": "Issue",
            "file": "src/app.py",
            "autoFix": False,
            "badCode": [{"ln": 1, "code": "return redirect(next_url)", "t": "del"}],
            "goodCode": [{"ln": 1, "code": "return redirect(safe_redirect(next_url))", "t": "add"}],
        }

        with (
            patch.dict(os.environ, {"PULLWISE_REVIEW_PROVIDER": "mock"}, clear=False),
            patch.object(review, "_run_mock", return_value=[provider_finding]),
        ):
            findings = review.run_review(
                repo="owner/repo",
                branch="main",
                commit="pending",
                user_id="usr_1",
                scan_id="sc_1",
            )

        finding = findings[0]
        self.assertIs(finding["autoFix"], False)
        self.assertEqual(finding["badCode"], [])
        self.assertEqual(finding["goodCode"], [])

    def test_run_review_drops_control_characters_from_provider_finding_text(self) -> None:
        provider_finding = {
            "id": "f_bad\r\nX-Injected: bad",
            "severity": "high\r\nX-Injected: bad",
            "category": "security\r\nX-Injected: bad",
            "title": "Unsafe redirect\r\nX-Injected: bad",
            "summary": "Uses a raw redirect.\x00",
            "impact": "Phishing risk.\r",
            "file": "src/app.py\r\n../secret",
            "effort": "5 min\n",
            "tags": ["security\r\nbad", "redirect"],
            "steps": ["Validate redirects.\r\nextra", "Use allowlist."],
            "references": [
                {"label": "Docs\r\nInjected", "url": "https://example.com/docs"},
                {"label": "Safe", "url": "https://example.com/safe"},
            ],
        }

        with (
            patch.dict(os.environ, {"PULLWISE_REVIEW_PROVIDER": "mock"}, clear=False),
            patch.object(review, "_run_mock", return_value=[provider_finding]),
        ):
            findings = review.run_review(
                repo="owner/repo",
                branch="main",
                commit="pending",
                user_id="usr_1",
                scan_id="sc_1",
            )

        finding = findings[0]
        self.assertRegex(finding["id"], r"^f_")
        self.assertEqual(finding["severity"], "medium")
        self.assertEqual(finding["category"], "Quality")
        self.assertEqual(finding["title"], "Untitled finding")
        self.assertEqual(finding["summary"], "")
        self.assertEqual(finding["impact"], "Phishing risk.")
        self.assertEqual(finding["file"], "")
        self.assertEqual(finding["effort"], "-")
        self.assertEqual(finding["tags"], ["redirect"])
        self.assertEqual(finding["steps"], ["Use allowlist."])
        self.assertEqual(finding["references"], [{"label": "Safe", "url": "https://example.com/safe"}])

    def test_run_review_drops_control_characters_from_provider_code_rows(self) -> None:
        provider_finding = {
            "id": "f_valid",
            "title": "Issue",
            "file": "app.py",
            "autoFix": True,
            "badCode": [
                {"ln": 1, "code": "return redirect(next_url)", "t": "del"},
                {"ln": 2, "code": "bad\r\nextra", "t": "del"},
                {"ln": 3, "code": "bad\x00nul", "t": "del"},
            ],
            "goodCode": [
                {"ln": 1, "code": "return redirect(safe_redirect(next_url))", "t": "add"},
                {"ln": 2, "code": "bad\nextra", "t": "add"},
            ],
        }

        with (
            patch.dict(os.environ, {"PULLWISE_REVIEW_PROVIDER": "mock"}, clear=False),
            patch.object(review, "_run_mock", return_value=[provider_finding]),
        ):
            findings = review.run_review(
                repo="owner/repo",
                branch="main",
                commit="pending",
                user_id="usr_1",
                scan_id="sc_1",
            )

        finding = findings[0]
        self.assertEqual(
            finding["badCode"],
            [
                {"ln": 1, "code": "return redirect(next_url)", "t": "del"},
                {"ln": 2, "code": "bad\nextra", "t": "del"},
            ],
        )
        self.assertEqual(
            finding["goodCode"],
            [
                {"ln": 1, "code": "return redirect(safe_redirect(next_url))", "t": "add"},
                {"ln": 2, "code": "bad\nextra", "t": "add"},
            ],
        )

    def test_run_review_sanitizes_source_metadata_before_provider_dispatch(self) -> None:
        with (
            patch.dict(os.environ, {"PULLWISE_REVIEW_PROVIDER": "mock"}, clear=False),
            patch.object(review, "_run_mock", return_value=[{"id": "f_1", "title": "Issue"}]) as run_mock,
        ):
            findings = review.run_review(
                repo="owner/repo\r\nX-Injected: bad",
                branch="main\r\nX-Injected: bad",
                commit={"sha": "abc1234"},
                user_id="usr_1\r\nX-Injected: bad",
                scan_id={"id": "sc_1"},
            )

        self.assertEqual(run_mock.call_args.kwargs["repo"], "")
        self.assertEqual(run_mock.call_args.kwargs["branch"], "main")
        self.assertEqual(run_mock.call_args.kwargs["commit"], "pending")
        self.assertEqual(findings[0]["repo"], "")
        self.assertEqual(findings[0]["userId"], "")
        self.assertEqual(findings[0]["scanId"], "")


if __name__ == "__main__":
    unittest.main()
