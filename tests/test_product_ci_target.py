from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProductCiTargetTest(unittest.TestCase):
    def test_ci_uses_repository_authority_and_keeps_core_python_checks(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

        self.assertNotIn("check_current_reviewer_authority", workflow)
        self.assertNotIn("Check current Node.js/Pi Reviewer target", workflow)
        self.assertIn("pip-audit .", workflow)
        self.assertIn("python -m pip check", workflow)
        self.assertIn("python -m pytest", workflow)


if __name__ == "__main__":
    unittest.main()
