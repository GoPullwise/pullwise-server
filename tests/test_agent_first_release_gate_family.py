from __future__ import annotations

import json
from pathlib import Path
import unittest

from pullwise_server.agent_first_contract_bundle import REQUIRED_FAMILIES


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "contracts/agent-first/current/source"
D22_FAMILIES = (
    "benchmark-bundle",
    "release-gate-policy",
    "release-gate-report",
    "release-gate-attestation",
)


class AgentFirstReleaseGateFamilyTest(unittest.TestCase):
    def test_source_package_requires_the_complete_d22_evidence_chain(self) -> None:
        package = json.loads(
            (SOURCE_ROOT / "package.json").read_text(encoding="utf-8")
        )
        observed = {
            "python_inventory": [
                family_id
                for family_id in REQUIRED_FAMILIES
                if family_id in D22_FAMILIES
            ],
            "source_inventory": [
                family_id
                for family_id in package["required_families"]
                if family_id in D22_FAMILIES
            ],
            "source_files": [
                family_id
                for family_id in D22_FAMILIES
                if (SOURCE_ROOT / "families" / f"{family_id}.json").is_file()
            ],
        }
        self.assertEqual(
            {key: list(D22_FAMILIES) for key in observed},
            observed,
        )


if __name__ == "__main__":
    unittest.main()
