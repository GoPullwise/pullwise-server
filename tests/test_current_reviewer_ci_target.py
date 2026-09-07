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
