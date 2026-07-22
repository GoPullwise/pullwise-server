from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "contracts/agent-first/current/source/families/gate.json"
ERROR_PATH = (
    ROOT / "contracts/agent-first/current/source/families/receipt-error.json"
)


class GateFamilyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.gate_family = json.loads(GATE_PATH.read_text(encoding="utf-8"))
        cls.error_family = json.loads(ERROR_PATH.read_text(encoding="utf-8"))
        cls.schemas = {
            item["$id"]: item for item in cls.gate_family["schemas"]
        }

    def test_obsolete_abbreviated_gate_input_is_removed(self) -> None:
        self.assertEqual(
            ["gate-decision/v1", "gate-predicate-registry/v1"],
            [item["$id"] for item in self.gate_family["schemas"]],
        )


if __name__ == "__main__":
    unittest.main()
