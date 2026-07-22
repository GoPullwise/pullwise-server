from __future__ import annotations

import json
from pathlib import Path
import unittest

from pullwise_server.agent_first_contract_bundle_source import load_family


ROOT = Path(__file__).resolve().parents[1]
FAMILY_PATH = (
    ROOT
    / "contracts"
    / "agent-first"
    / "current"
    / "source"
    / "families"
    / "quality-policy-plan.json"
)


class QualityPolicyPlanFamilyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.family = json.loads(FAMILY_PATH.read_text(encoding="utf-8"))
        loaded = load_family(
            FAMILY_PATH,
            "quality-policy-plan",
            {},
            set(),
        )
        cls.schema = loaded["schemas"][0]

    def test_schema_closes_the_mvp_quality_branches(self) -> None:
        self.assertEqual("quality-policy-plan", self.family["family_id"])
        self.assertEqual("quality-policy-plan/v1", self.schema["$id"])
        self.assertIs(False, self.schema["additionalProperties"])
        self.assertEqual(
            set(self.schema["properties"]),
            set(self.schema["required"]),
        )
        self.assertEqual(
            {
                "document_rules": ["quality_policy_plan"],
                "contextual_helpers": ["verify_quality_policy_plan_context"],
            },
            self.schema["x-pullwise-semantics"],
        )

        properties = self.schema["properties"]
        self.assertEqual(["Q1", "Q2", "Q3"], properties["quality_risk"]["enum"])
        self.assertIs(False, properties["self_attestation_allowed"]["const"])
        self.assertEqual(
            "pullwise-quality-policy/v1",
            properties["policy_implementation_version"]["const"],
        )
        self.assertEqual(2, properties["slots"]["maxItems"])
        self.assertEqual(
            ["contract_and_data", "security_and_concurrency"],
            properties["slots"]["items"]["properties"]["concern"]["enum"],
        )
        self.assertEqual(
            [
                ("Q1", 1, 1),
                ("Q2", 2, 2),
                ("Q3", 0, 0),
            ],
            [
                (
                    branch["properties"]["quality_risk"]["const"],
                    branch["properties"]["slots"]["minItems"],
                    branch["properties"]["slots"]["maxItems"],
                )
                for branch in self.schema["oneOf"]
            ],
        )


if __name__ == "__main__":
    unittest.main()
