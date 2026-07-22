from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from pullwise_server.agent_first_contract_bundle_source import canonical_bytes, load_family


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

    def test_fixture_matrix_covers_each_mvp_quality_branch(self) -> None:
        fixtures = {item["fixture_id"]: item for item in self.family["fixtures"]}
        expected = {
            "quality_policy_golden_q1_plan", "quality_policy_golden_q2_plan",
            "quality_policy_golden_q3_unsupported", "quality_policy_idempotency_q1_plan",
            "quality_policy_idempotency_q2_plan", "quality_policy_idempotency_q3_unsupported",
            "quality_policy_negative_q0_removed", "quality_policy_negative_q1_missing_slot",
            "quality_policy_negative_q1_self_attestation", "quality_policy_negative_q2_duplicate_concern",
            "quality_policy_negative_q2_missing_slot", "quality_policy_negative_q3_has_slot",
        }
        self.assertEqual(expected, set(fixtures))
        self.assertEqual(sorted(fixtures), [item["fixture_id"] for item in self.family["fixtures"]])
        pairs = (
            ("quality_policy_golden_q1_plan", "quality_policy_idempotency_q1_plan"),
            ("quality_policy_golden_q2_plan", "quality_policy_idempotency_q2_plan"),
            ("quality_policy_golden_q3_unsupported", "quality_policy_idempotency_q3_unsupported"),
        )
        for golden_id, retry_id in pairs:
            golden = fixtures[golden_id]["document"]
            retry = fixtures[retry_id]["document"]
            self.assertEqual(canonical_bytes(golden), canonical_bytes(retry))
            self.assertTrue(self._valid_plan(golden), golden_id)
            self.assertIsNone(fixtures[golden_id]["expected_code"])
            self.assertIsNone(fixtures[retry_id]["expected_code"])
        for fixture_id, fixture in fixtures.items():
            if fixture["fixture_class"] == "negative":
                self.assertEqual("CONTRACT_DOCUMENT_INVALID", fixture["expected_code"])
                self.assertFalse(self._valid_plan(fixture["document"]), fixture_id)

    def _valid_plan(self, document: dict[str, object]) -> bool:
        unsigned = {key: value for key, value in document.items() if key != "plan_digest"}
        expected_digest = hashlib.sha256(
            b"pullwise:quality-policy-plan:v1\0" + canonical_bytes(unsigned)
        ).hexdigest()
        slots = document["slots"]
        risk = document["quality_risk"]
        expected_concerns = {
            "Q1": ["contract_and_data"],
            "Q2": ["contract_and_data", "security_and_concurrency"],
            "Q3": [],
        }
        return (
            document["plan_digest"] == expected_digest
            and document["self_attestation_allowed"] is False
            and risk in expected_concerns
            and len(slots) == len(expected_concerns[risk])
            and [item["slot_id"] for item in slots]
            == sorted({item["slot_id"] for item in slots})
            and [item["concern"] for item in slots] == expected_concerns[risk]
            and all(
                item["requirement_ids"] == sorted(set(item["requirement_ids"]))
                and bool(item["requirement_ids"])
                for item in slots
            )
        )


if __name__ == "__main__":
    unittest.main()
