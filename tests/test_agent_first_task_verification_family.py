from __future__ import annotations

from pathlib import Path
import unittest

from tests.agent_first_task_evidence_support import (
    FamilyAssertions,
    ordered_unique,
    sealed,
    valid_content_ref,
)


ROOT = Path(__file__).resolve().parents[1]
FAMILY_PATH = ROOT / "contracts/agent-first/current/source/families/task-verification.json"
SCHEMAS = ("verification-attestation-manifest/v1",)
HELPERS = {
    "verification-attestation-manifest/v1": [
        "verify_attestation_manifest_context"
    ]
}


class TaskVerificationFamilyTest(FamilyAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.load_family(FAMILY_PATH)

    def valid_manifest(self, document: dict[str, object]) -> bool:
        attestations = document["attestations"]
        slots = {item["slot_id"] for item in attestations}
        ids = {item["attestation_id"] for item in attestations}
        if not (
            sealed(
                document,
                self.schemas["verification-attestation-manifest/v1"],
            )
            and valid_content_ref(
                document["quality_policy_plan_ref"],
                {"quality-policy-plan/v1"},
            )
            and valid_content_ref(
                document["final_observation_manifest_ref"],
                {"observation-manifest/v1"},
            )
            and document["attestation_count"] == len(attestations)
            and ordered_unique(
                attestations,
                lambda item: (item["slot_id"], item["attestation_id"]),
            )
            and all(
                valid_content_ref(
                    item["attestation_ref"], {"verification-attestation/v1"}
                )
                for item in attestations
            )
            and ordered_unique(
                document["requirement_aggregates"],
                lambda item: item["requirement_id"],
            )
        ):
            return False
        for aggregate in document["requirement_aggregates"]:
            required = aggregate["required_slot_ids"]
            contributing = aggregate["attestation_ids"]
            if required != sorted(set(required)) or contributing != sorted(
                set(contributing)
            ):
                return False
            if set(required).difference(slots) and aggregate["verdict"] != "UNVERIFIABLE":
                return False
            if not set(contributing).issubset(ids):
                return False
        return True

    def test_closed_sorted_schema_and_semantic_registry(self) -> None:
        self.assert_family_contract("task-verification", SCHEMAS, HELPERS)

    def test_manifest_uses_exact_typed_policy_observation_and_attestations(self) -> None:
        props = self.schemas["verification-attestation-manifest/v1"]["properties"]
        self.assertEqual(
            "quality-policy-plan/v1",
            props["quality_policy_plan_ref"]["x-pullwise-content-schema-id"],
        )
        self.assertEqual(
            "observation-manifest/v1",
            props["final_observation_manifest_ref"][
                "x-pullwise-content-schema-id"
            ],
        )
        self.assertEqual(
            "verification-attestation/v1",
            props["attestations"]["items"]["properties"]["attestation_ref"][
                "x-pullwise-content-schema-id"
            ],
        )

    def test_complete_fixtures_execute_and_retry_byte_exactly(self) -> None:
        self.assert_fixture_matrix(
            {"verification-attestation-manifest/v1": self.valid_manifest}
        )

    def test_negative_missing_slot_cannot_aggregate_to_pass(self) -> None:
        fixtures = {item["fixture_id"]: item for item in self.family["fixtures"]}
        aggregate = fixtures["task_verification_negative_missing_slot"][
            "document"
        ]["requirement_aggregates"][0]
        self.assertEqual("PASS", aggregate["verdict"])


if __name__ == "__main__":
    unittest.main()
