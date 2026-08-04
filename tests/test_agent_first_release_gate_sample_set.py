from __future__ import annotations

from pathlib import Path
from types import ModuleType
import unittest

from pullwise_server.agent_first_contract_bundle import build_bundle


SOURCE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "agent-first"
    / "current"
    / "source"
)


class AgentFirstReleaseGateSampleSetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        bundle = build_bundle(SOURCE_ROOT)
        cls.contract = ModuleType("_release_gate_sample_set_source_contract")
        exec(bundle.python_wrapper, cls.contract.__dict__)

    def test_public_derivation_rejects_missing_typed_inputs(self) -> None:
        with self.assertRaises(self.contract.ContractValidationError):
            self.contract.derive_release_gate_evaluation(None, None, None)


if __name__ == "__main__":
    unittest.main()
