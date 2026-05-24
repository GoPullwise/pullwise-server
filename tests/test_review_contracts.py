from __future__ import annotations

import math
import os
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
        self.assertEqual(finding["file"], "")
        self.assertEqual(finding["line"], 0)
        self.assertEqual(finding["confidence"], 0.7)
        self.assertFalse(math.isnan(finding["confidence"]))
        self.assertIs(finding["autoFix"], False)
        self.assertEqual(finding["effort"], "-")
        self.assertEqual(finding["tags"], [])
        self.assertEqual(finding["steps"], [])
        self.assertEqual(finding["badCode"], [])
        self.assertEqual(finding["goodCode"], [])
        self.assertEqual(finding["references"], [])

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
        self.assertEqual(finding["impact"], "")
        self.assertEqual(finding["file"], "")
        self.assertEqual(finding["effort"], "-")
        self.assertEqual(finding["tags"], ["redirect"])
        self.assertEqual(finding["steps"], ["Use allowlist."])
        self.assertEqual(finding["references"], [{"label": "Safe", "url": "https://example.com/safe"}])

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
