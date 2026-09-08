from __future__ import annotations

import unittest
from pullwise_server import app


class PiResultEvidenceTest(unittest.TestCase):
    def test_canonical_source_summary_survives_public_projection_and_export(self) -> None:
        excerpt = 'function total(items) {\n  // 价格\n  return items[0].price;\n}'
        issue = {"id": "finding_pi", "repo": "acme/api", "commit": "a" * 40,
            "file": "cart.js", "line": 7, "title": "Quantity omitted", "severity": "medium",
            "evidence": [{"path": "cart.js", "line": 7, "summary": excerpt}]}
        evidence = app.public_issue_evidence(issue)
        self.assertEqual(evidence[0]["summary"], excerpt)
        self.assertEqual(evidence[0]["file"], "cart.js")
        self.assertEqual(evidence[0]["startLine"], 7)
        markdown = app.audit_bundle_issue_markdown({**issue, "evidence": evidence})
        self.assertIn(excerpt, markdown)
