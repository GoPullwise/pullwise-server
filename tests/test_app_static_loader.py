from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest

import pullwise_server.app as app


class AppStaticLoaderTest(unittest.TestCase):
    def test_app_uses_static_module_assembly_without_exec_loader(self) -> None:
        source = Path(app.__file__).read_text(encoding="utf-8")

        self.assertNotIn("exec(", source)
        self.assertTrue(hasattr(app, "PullwiseHandler"))

    def test_app_module_entrypoint_shows_cli_help(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "pullwise_server.app", "--help"],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Run the Pullwise local API server.", completed.stdout)
