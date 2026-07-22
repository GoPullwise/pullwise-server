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
SCHEMAS = (
    "verification-attestation-manifest/v1",
    "verification-attestation/v1",
)
HELPERS = {
    "verification-attestation-manifest/v1": [
        "verify_attestation_manifest_context"
    ],
    "verification-attestation/v1": ["verify_attestation_context"],
}


def run_status(verdicts: list[dict[str, object]]) -> str:
    values = {item["verdict"] for item in verdicts}
    for candidate in ("POLICY_VIOLATION", "NEEDS_WORK", "UNVERIFIABLE"):
        if candidate in values:
            return candidate
    return "PASS"


class TaskVerificationFamilyTest(FamilyAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.load_family(FAMILY_PATH)

    def valid_attestation(self, document: dict[str, object]) -> bool:
        verdicts = document["requirement_verdicts"]
        return (
            sealed(document, self.schemas["verification-attestation/v1"])
            and valid_content_ref(
                document["verifier_input_manifest_ref"],
                {"verifier-input-manifest/v1"},
            )
            and valid_content_ref(
                document["final_observation_manifest_ref"],
                {"observation-manifest/v1"},
            )
            and document["execution_state_ids"]
            == sorted(set(document["execution_state_ids"]))
            and document["own_observation_ids"]
            == sorted(set(document["own_observation_ids"]))
            and bool(document["own_observation_ids"])
            and ordered_unique(verdicts, lambda item: item["requirement_id"])
            and all(
                item["evidence_ids"] == sorted(set(item["evidence_ids"]))
                and item["limitations"] == sorted(set(item["limitations"]))
                and (item["verdict"] != "PASS" or not item["limitations"])
                for item in verdicts
            )
            and document["run_status"] == run_status(verdicts)
        )

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
                document["final_observation_manifest_ref"],
                {"observation-manifest/v1"},
            )
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

    def test_attestations_use_exact_typed_frozen_inputs(self) -> None:
        props = self.schemas["verification-attestation/v1"]["properties"]
        self.assertEqual(
            "observation-manifest/v1",
            props["final_observation_manifest_ref"][
                "x-pullwise-content-schema-id"
            ],
        )
        self.assertEqual(
            "verifier-input-manifest/v1",
            props["verifier_input_manifest_ref"]["x-pullwise-content-schema-id"],
        )

    def test_complete_fixtures_execute_and_retry_byte_exactly(self) -> None:
        self.assert_fixture_matrix(
            {
                "verification-attestation-manifest/v1": self.valid_manifest,
                "verification-attestation/v1": self.valid_attestation,
            }
        )

    def test_negatives_cover_priority_and_missing_required_slot(self) -> None:
        fixtures = {item["fixture_id"]: item for item in self.family["fixtures"]}
        self.assertEqual(
            "PASS",
            fixtures["task_verification_negative_attestation_priority"][
                "document"
            ]["run_status"],
        )
        aggregate = fixtures["task_verification_negative_missing_slot"][
            "document"
        ]["requirement_aggregates"][0]
        self.assertEqual("PASS", aggregate["verdict"])


if __name__ == "__main__":
    unittest.main()
