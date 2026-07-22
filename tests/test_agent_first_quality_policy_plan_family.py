from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import ModuleType
import unittest

from pullwise_server.agent_first_contract_bundle_python import render_python_wrapper
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
ERROR_FAMILY_PATH = (
    ROOT
    / "contracts"
    / "agent-first"
    / "current"
    / "source"
    / "families"
    / "receipt-error.json"
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
        cls.fixtures = {
            item["fixture_id"]: item for item in cls.family["fixtures"]
        }
        error_family = json.loads(ERROR_FAMILY_PATH.read_text(encoding="utf-8"))
        error_fixture = next(
            item
            for item in error_family["fixtures"]
            if item["fixture_id"] == "error_golden_current_registry"
        )
        canonical_bundle = canonical_bytes(
            {
                "root_manifest": {
                    "schema_registry": [
                        {
                            "schema_id": "quality-policy-plan/v1",
                            "role": "public_document",
                        }
                    ]
                },
                "families": [
                    cls.family,
                    {
                        "family_id": "receipt-error",
                        "schemas": [],
                        "fixtures": [error_fixture],
                    },
                ],
            }
        )
        wrapper_bytes = render_python_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            hashlib.sha256(b"quality-policy-family-test-root").hexdigest(),
            hashlib.sha256(canonical_bundle).hexdigest(),
            canonical_bundle,
        )
        cls.wrapper = ModuleType("_agent_first_quality_policy_test_wrapper")
        exec(wrapper_bytes, cls.wrapper.__dict__)

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
        fixtures = self.fixtures
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

    def test_public_facade_executes_the_fixture_matrix(self) -> None:
        self.assertIn(
            "verify_quality_policy_plan_context",
            self.wrapper.__all__,
        )
        self.assertTrue(callable(self.wrapper.verify_quality_policy_plan_context))
        for fixture_id, fixture in self.fixtures.items():
            document = fixture["document"]
            if fixture["fixture_class"] == "negative":
                with self.subTest(fixture_id=fixture_id), self.assertRaises(
                    self.wrapper.ContractValidationError
                ) as raised:
                    self.wrapper.verify_document_digest(
                        "quality-policy-plan/v1",
                        document,
                    )
                self.assertEqual(fixture["expected_code"], raised.exception.code)
            else:
                self.assertEqual(
                    document,
                    self.wrapper.verify_document_digest(
                        "quality-policy-plan/v1",
                        document,
                    ),
                )

    def test_public_document_rule_recomputes_inputs_and_fixed_slot_table(self) -> None:
        requirement_id = (
            "req_user_objective_"
            + "2" * 64
        )
        cases = []
        wrong_input = deepcopy(
            self.fixtures["quality_policy_golden_q1_plan"]["document"]
        )
        wrong_input["input_digest"] = "0" * 64
        cases.append(("input_digest", self._reseal(wrong_input)))
        wrong_slot = deepcopy(
            self.fixtures["quality_policy_golden_q1_plan"]["document"]
        )
        wrong_slot["slots"][0]["slot_id"] = "slot_" + "3" * 32
        cases.append(("slot_id", self._reseal(wrong_slot)))
        wrong_order = deepcopy(
            self.fixtures["quality_policy_golden_q2_plan"]["document"]
        )
        wrong_order["slots"].reverse()
        cases.append(("slot_order", self._reseal(wrong_order)))
        wrong_requirements = deepcopy(
            self.fixtures["quality_policy_golden_q1_plan"]["document"]
        )
        wrong_requirements["slots"][0]["requirement_ids"].insert(
            0,
            requirement_id,
        )
        cases.append(("requirement_order", self._reseal(wrong_requirements)))

        for case, document in cases:
            with self.subTest(case=case), self.assertRaises(
                self.wrapper.ContractValidationError
            ) as raised:
                self.wrapper.verify_document_digest(
                    "quality-policy-plan/v1",
                    document,
                )
            self.assertEqual("CONTRACT_DOCUMENT_INVALID", raised.exception.code)

    def _valid_plan(self, document: dict[str, object]) -> bool:
        unsigned = {key: value for key, value in document.items() if key != "plan_digest"}
        expected_digest = hashlib.sha256(
            b"pullwise:quality-policy-plan:v1\0" + canonical_bytes(unsigned)
        ).hexdigest()
        slots = document["slots"]
        input_fields = (
            "proposal_digest",
            "policy_digest",
            "task_type",
            "requirement_ledger_digest",
            "change_set_classification_digest",
            "capability_usage_digest",
        )
        expected_input_digest = hashlib.sha256(
            canonical_bytes({field: document[field] for field in input_fields})
        ).hexdigest()
        risk = document["quality_risk"]
        expected_slots = {
            "Q1": [
                ("slot_11111111111111111111111111111111", "contract_and_data")
            ],
            "Q2": [
                ("slot_11111111111111111111111111111111", "contract_and_data"),
                (
                    "slot_22222222222222222222222222222222",
                    "security_and_concurrency",
                ),
            ],
            "Q3": [],
        }
        return (
            document["plan_digest"] == expected_digest
            and document["input_digest"] == expected_input_digest
            and document["self_attestation_allowed"] is False
            and risk in expected_slots
            and [
                (item["slot_id"], item["concern"])
                for item in slots
            ]
            == expected_slots[risk]
            and all(
                item["requirement_ids"] == sorted(set(item["requirement_ids"]))
                and bool(item["requirement_ids"])
                for item in slots
            )
        )

    def _reseal(self, document: dict[str, object]) -> dict[str, object]:
        unsigned = {
            key: value for key, value in document.items() if key != "plan_digest"
        }
        document["plan_digest"] = hashlib.sha256(
            b"pullwise:quality-policy-plan:v1\0" + canonical_bytes(unsigned)
        ).hexdigest()
        return document


if __name__ == "__main__":
    unittest.main()
