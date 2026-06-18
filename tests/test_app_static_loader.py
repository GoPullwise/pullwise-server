from __future__ import annotations

from pathlib import Path
import unittest

import pullwise_server.app as app


class AppStaticLoaderTest(unittest.TestCase):
    def test_app_uses_static_module_assembly_without_exec_loader(self) -> None:
        source = Path(app.__file__).read_text(encoding="utf-8")

        self.assertNotIn("exec(", source)
        self.assertTrue(hasattr(app, "PullwiseHandler"))
