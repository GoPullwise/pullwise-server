from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_current_reviewer_authority.py"
END = "<!-- PULLWISE_REVIEWER_TARGET_END -->"
TEXT = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
TARGET = TEXT[:TEXT.index(END) + len(END)] + "\n"


class CurrentReviewerCiTargetTest(unittest.TestCase):
    def test_ci_provisions_the_worker_used_by_gateway_integration(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        # The real TLS integration executes ../pullwise-worker/src/main.ts.
        # A full developer workspace must not hide a missing CI prerequisite.
        self.assertIn("working-directory: pullwise-server", workflow)
        self.assertIn("path: pullwise-server", workflow)
        worker_checkout = workflow.index("repository: GoPullwise/pullwise-worker")
        node_setup = workflow.index("uses: actions/setup-node@v4")
        worker_install = workflow.index("run: npm ci --ignore-scripts")
        test_run = workflow.index("run: python -m pytest")
        self.assertIn("path: pullwise-worker", workflow[worker_checkout:node_setup])
        self.assertIn("persist-credentials: false", workflow[worker_checkout:node_setup])
        self.assertRegex(workflow[worker_checkout:node_setup], r"ref: [a-f0-9]{40}")
        self.assertIn('node-version: "22.23.1"', workflow[node_setup:worker_install])
        self.assertIn("working-directory: pullwise-worker", workflow[node_setup:worker_install])
        self.assertLess(worker_checkout, node_setup)
        self.assertLess(node_setup, worker_install)
        self.assertLess(worker_install, test_run)

    def invoke(self, script: Path):
        result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=10)
        return result.returncode, json.loads(result.stdout)

    def test_current_repository_passes_without_retired_authority_prefix(self):
        status, report = self.invoke(SCRIPT)
        self.assertEqual(status, 0, report)

    def test_local_target_is_sufficient_and_invalid_targets_fail_closed(self):
        cases = [
            (TARGET.encode(), 0),
            (TARGET.replace("\n", "\r\n").encode(), 0),
            (TARGET.replace("@earendil-works/pi-coding-agent", "retired-sdk").encode(), 1),
            ((TARGET + TARGET).encode(), 1),
            (("# External authority\n" + TARGET).encode(), 1),
            (b"\xff", 2),
        ]
        for content, expected in cases:
            with self.subTest(expected=expected, content=content[:30]):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (root / "scripts").mkdir()
                    script = root / "scripts" / SCRIPT.name
                    shutil.copyfile(SCRIPT, script)
                    (root / "AGENTS.md").write_bytes(content)
                    status, report = self.invoke(script)
                    self.assertEqual(status, expected, report)
