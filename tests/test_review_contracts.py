from __future__ import annotations

import math
import os
import tempfile
import unittest

from pullwise_server import review


def normalize(raw_findings: object, *, repo_path: str | None = None) -> list[dict]:
    return review.normalize_findings(
        raw_findings,
        repo="owner/repo",
        branch="main",
        commit="pending",
        user_id="usr_1",
        scan_id="sc_1",
        repo_path=repo_path,
    )


class ReviewContractsTest(unittest.TestCase):
    def test_normalize_findings_sanitizes_malformed_fields(self) -> None:
        findings = normalize(
            [
                {
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
            ]
        )

        self.assertEqual(1, len(findings))
        finding = findings[0]
        self.assertRegex(finding["id"], r"^f_")
        self.assertEqual(finding["severity"], "medium")
        self.assertEqual(finding["category"], "Quality")
        self.assertEqual(finding["title"], "Untitled finding")
        self.assertEqual(finding["summary"], "")
        self.assertEqual(finding["impact"], "")
        self.assertEqual(finding["file"], "")
        self.assertEqual(finding["line"], 0)
        self.assertEqual(finding["confidence"], 0.0)
        self.assertFalse(math.isnan(finding["confidence"]))
        self.assertIs(finding["autoFix"], False)
        self.assertEqual(finding["effort"], "-")
        self.assertEqual(finding["tags"], [])
        self.assertEqual(finding["steps"], [])
        self.assertEqual(finding["badCode"], [])
        self.assertEqual(finding["goodCode"], [])
        self.assertEqual(finding["references"], [])

    def test_normalize_findings_ignores_non_objects_and_sanitizes_rows(self) -> None:
        findings = normalize(
            [
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
                    "badCode": [{"ln": float("inf"), "code": "return redirect(next_url)", "t": ["del"]}],
                    "goodCode": [{"ln": -1, "code": "return redirect(safe_redirect(next_url))", "t": "add"}],
                    "references": [
                        {"label": "Bad", "url": "javascript:alert(1)"},
                        {"label": " Docs ", "url": " https://example.com/docs "},
                    ],
                },
            ]
        )

        self.assertEqual(1, len(findings))
        finding = findings[0]
        self.assertEqual(finding["id"], "f_valid")
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(finding["category"], "Security")
        self.assertEqual(finding["title"], "Unsafe redirect")
        self.assertEqual(finding["file"], "app.py")
        self.assertEqual(finding["line"], 12)
        self.assertEqual(finding["confidence"], 1.0)
        self.assertEqual(finding["tags"], ["redirect"])
        self.assertEqual(finding["steps"], ["Validate redirect targets."])
        self.assertEqual(finding["badCode"], [{"ln": 0, "code": "return redirect(next_url)", "t": None}])
        self.assertEqual(finding["goodCode"], [{"ln": 0, "code": "return redirect(safe_redirect(next_url))", "t": "add"}])
        self.assertEqual(finding["references"], [{"label": "Docs", "url": "https://example.com/docs"}])

    def test_normalize_findings_maps_priority_style_severities_to_canonical_levels(self) -> None:
        findings = normalize(
            [
                {"id": "f_p0", "title": "Critical", "severity": "P0"},
                {"id": "f_p1", "title": "High", "severity": "P1"},
                {"id": "f_p2", "title": "Medium", "severity": "P2"},
                {"id": "f_p3", "title": "Low", "severity": "P3"},
                {"id": "f_p4", "title": "Info", "severity": "P4"},
            ]
        )

        self.assertEqual(
            [finding["severity"] for finding in findings],
            ["critical", "high", "medium", "low", "info"],
        )

    def test_normalize_findings_downgrades_verified_without_runtime_evidence(self) -> None:
        findings = normalize(
            [
                {
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
                        }
                    ],
                    "reproduction": {"commands": ["pytest tests/repro/test_bounds.py"]},
                }
            ]
        )

        self.assertEqual(findings[0]["verificationStatus"], "static_proof")
        self.assertEqual(findings[0]["affectedLocations"], [{"file": "src/app.py", "startLine": 7, "endLine": 9}])

    def test_normalize_findings_keeps_verified_with_runtime_output_and_reproduction(self) -> None:
        findings = normalize(
            [
                {
                    "id": "f_runtime_verified",
                    "title": "Issue",
                    "file": "src/app.py",
                    "line": 7,
                    "verificationStatus": "verified",
                    "evidence": [
                        {
                            "type": "runtime_log",
                            "label": "Focused test",
                            "summary": "The focused command failed with the observed error.",
                            "command": "pytest tests/repro/test_bounds.py",
                            "exitCode": 1,
                            "logPath": "logs/f_runtime_verified.log",
                            "output": "FAIL tests/repro/test_bounds.py\nAssertionError",
                        }
                    ],
                    "reproduction": {
                        "commands": ["pytest tests/repro/test_bounds.py"],
                        "actual": "Command exited 1.",
                        "testFile": "tests/repro/test_bounds.py",
                    },
                }
            ]
        )

        self.assertEqual(findings[0]["verificationStatus"], "verified")
        self.assertIn("AssertionError", findings[0]["evidence"][0]["output"])

    def test_normalize_findings_validates_auto_fix_blocks_against_repo_file(self) -> None:
        with tempfile.TemporaryDirectory() as repo_path:
            os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
            app_path = os.path.join(repo_path, "src", "app.py")
            with open(app_path, "w", encoding="utf-8") as handle:
                handle.write("return redirect(next_url)\n")

            findings = normalize(
                [
                    {
                        "id": "f_absolute_path",
                        "title": "Issue",
                        "file": app_path,
                        "autoFix": True,
                        "badCode": [{"ln": 1, "code": "return redirect(next_url)", "t": "del"}],
                        "goodCode": [{"ln": 1, "code": "return redirect(safe_redirect(next_url))", "t": "add"}],
                    }
                ],
                repo_path=repo_path,
            )

        self.assertEqual(findings[0]["file"], "src/app.py")
        self.assertIs(findings[0]["autoFix"], True)
        self.assertEqual(findings[0]["badCode"], [{"ln": 1, "code": "return redirect(next_url)", "t": "del"}])

    def test_normalize_findings_downgrades_invalid_auto_fix(self) -> None:
        with tempfile.TemporaryDirectory() as repo_path:
            os.makedirs(os.path.join(repo_path, "src"), exist_ok=True)
            with open(os.path.join(repo_path, "src", "app.py"), "w", encoding="utf-8") as handle:
                handle.write("const token = readToken()\naudit(token)\nreturn redirect(nextUrl)\n")

            findings = normalize(
                [
                    {
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
                ],
                repo_path=repo_path,
            )

        self.assertIs(findings[0]["autoFix"], False)
        self.assertEqual(findings[0]["badCode"], [])
        self.assertEqual(findings[0]["goodCode"], [])

    def test_normalize_findings_sanitizes_source_metadata(self) -> None:
        findings = review.normalize_findings(
            [{"id": "f_1", "title": "Issue"}],
            repo="owner/repo\r\nX-Injected: bad",
            branch="main\r\nX-Injected: bad",
            commit={"sha": "abc1234"},
            user_id="usr_1\r\nX-Injected: bad",
            scan_id={"id": "sc_1"},
        )

        self.assertEqual(findings[0]["repo"], "")
        self.assertEqual(findings[0]["userId"], "")
        self.assertEqual(findings[0]["scanId"], "")


if __name__ == "__main__":
    unittest.main()
