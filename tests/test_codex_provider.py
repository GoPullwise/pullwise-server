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

        with (
            patch("pullwise_server.review.shutil.which", return_value=None),
            patch("pullwise_server.review.subprocess.run", side_effect=fake_run),
        ):
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

    def test_codex_provider_resolves_path_shim_before_subprocess(self) -> None:
        captured = {}
        resolved = "C:\\Users\\Dev\\AppData\\Roaming\\npm\\codex.CMD"

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            output_path = cmd[cmd.index("--output-last-message") + 1]
            with open(output_path, "w", encoding="utf-8") as output_file:
                output_file.write('{"findings":[]}')
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with (
            patch("pullwise_server.review.shutil.which", return_value=resolved),
            patch("pullwise_server.review.subprocess.run", side_effect=fake_run),
        ):
            review._run_codex(
                repo="owner/repo",
                branch="main",
                commit="pending",
                repo_path="F:\\tmp\\repo",
            )

        self.assertEqual(captured["cmd"][0], resolved)
        self.assertEqual(captured["cmd"][1], "exec")

    def test_codex_provider_prefers_configured_cli_path(self) -> None:
        captured = {}
        configured = "D:\\tools\\codex.cmd"

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            output_path = cmd[cmd.index("--output-last-message") + 1]
            with open(output_path, "w", encoding="utf-8") as output_file:
                output_file.write('{"findings":[]}')
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with (
            patch.dict("os.environ", {"PULLWISE_CODEX_BIN": configured}, clear=True),
            patch("pullwise_server.review.shutil.which", return_value="C:\\npm\\codex.CMD"),
            patch("pullwise_server.review.subprocess.run", side_effect=fake_run),
        ):
            review._run_codex(
                repo="owner/repo",
                branch="main",
                commit="pending",
                repo_path="F:\\tmp\\repo",
            )

        self.assertEqual(captured["cmd"][0], configured)
        self.assertEqual(captured["cmd"][1], "exec")

    def test_codex_provider_reports_missing_cli(self) -> None:
        with patch("pullwise_server.review.subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaisesRegex(RuntimeError, "Codex CLI is not installed"):
                review._run_codex(
                    repo="owner/repo",
                    branch="main",
                    commit="pending",
                    repo_path="F:\\tmp\\repo",
                )

    def test_codex_provider_reports_missing_cli_login(self) -> None:
        completed = subprocess.CompletedProcess(
            ["codex"],
            1,
            stdout="",
            stderr="not logged in; run codex login",
        )

        with patch("pullwise_server.review.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "Run `codex login`"):
                review._run_codex(
                    repo="owner/repo",
                    branch="main",
                    commit="pending",
                    repo_path="F:\\tmp\\repo",
                )

    def test_codex_provider_reports_timeout(self) -> None:
        error = subprocess.TimeoutExpired(["codex"], timeout=5)

        with patch("pullwise_server.review.subprocess.run", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "timed out after 5 seconds"):
                review._run_codex(
                    repo="owner/repo",
                    branch="main",
                    commit="pending",
                    repo_path="F:\\tmp\\repo",
                )

    def test_codex_provider_reports_invalid_json(self) -> None:
        completed = subprocess.CompletedProcess(["codex"], 0, stdout="not json", stderr="")

        with patch("pullwise_server.review.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "valid JSON"):
                review._run_codex(
                    repo="owner/repo",
                    branch="main",
                    commit="pending",
                    repo_path="F:\\tmp\\repo",
                )


class ClaudeCodeProviderTest(unittest.TestCase):
    def test_claude_code_provider_reports_missing_cli(self) -> None:
        with patch("pullwise_server.review.subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaisesRegex(RuntimeError, "Claude Code CLI is not installed"):
                review._run_claude_code(
                    repo="owner/repo",
                    branch="main",
                    commit="pending",
                    repo_path="F:\\tmp\\repo",
                )


if __name__ == "__main__":
    unittest.main()
