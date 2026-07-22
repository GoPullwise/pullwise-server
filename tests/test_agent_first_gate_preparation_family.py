from __future__ import annotations

from pathlib import Path
import unittest

from pullwise_server.agent_first_contract_bundle_source import load_family


ROOT = Path(__file__).resolve().parents[1]
FAMILY_PATH = (
    ROOT / "contracts/agent-first/current/source/families/gate-preparation.json"
)
SCHEMA_IDS = (
    "debug-redaction-plan/v1",
    "publication-content-manifest/v1",
    "terminalization-fact/v1",
)


class AgentFirstGatePreparationFamilyTest(unittest.TestCase):
    def test_public_source_loader_accepts_the_bounded_family(self) -> None:
        loaded = load_family(FAMILY_PATH, "gate-preparation", {}, set())

        self.assertEqual(
            list(SCHEMA_IDS),
            [schema["$id"] for schema in loaded["schemas"]],
        )


if __name__ == "__main__":
    unittest.main()
