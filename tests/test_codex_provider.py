from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from pullwise_server import review


class CodexProviderTest(unittest.TestCase):
    def test_codex_provider_uses_official_non_interactive_exec_mode(self) -> None:
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            output_path = cmd[cmd.index("--output-last-message") + 1]
            with open(output_path, "w", encoding="utf-8") as output_file:
                output_file.write('{"findings":[]}')
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("pullwise_server.review.subprocess.run", side_effect=fake_run):
            findings = review._run_codex(
                repo="owner/repo",
                branch="main",
                commit="pending",
                repo_path="F:\\tmp\\repo",
            )

        self.assertEqual(findings, [])
        cmd = captured["cmd"]
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertIn("--sandbox", cmd)
        self.assertIn("read-only", cmd)
        self.assertIn("--output-last-message", cmd)
        self.assertIn("--output-schema", cmd)
        self.assertEqual(captured["kwargs"]["cwd"], "F:\\tmp\\repo")


if __name__ == "__main__":
    unittest.main()
