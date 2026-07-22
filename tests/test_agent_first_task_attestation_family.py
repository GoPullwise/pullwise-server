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
FAMILY_PATH = ROOT / "contracts/agent-first/current/source/families/task-attestation.json"
SCHEMAS = ("verification-attestation/v1",)
HELPERS = {"verification-attestation/v1": ["verify_attestation_context"]}


def run_status(verdicts: list[dict[str, object]]) -> str:
    values = {item["verdict"] for item in verdicts}
    for candidate in ("POLICY_VIOLATION", "NEEDS_WORK", "UNVERIFIABLE"):
        if candidate in values:
            return candidate
    return "PASS"


class TaskAttestationFamilyTest(FamilyAssertions, unittest.TestCase):
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
                document["verifier_work_report_ref"],
                {"verifier-work-report/v1"},
            )
            and valid_content_ref(
                document["quality_policy_plan_ref"],
                {"quality-policy-plan/v1"},
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

    def test_closed_sorted_schema_and_semantic_registry(self) -> None:
        self.assert_family_contract("task-attestation", SCHEMAS, HELPERS)

    def test_attestation_binds_all_frozen_verifier_inputs(self) -> None:
        props = self.schemas["verification-attestation/v1"]["properties"]
        expected = {
            "verifier_input_manifest_ref": "verifier-input-manifest/v1",
            "verifier_work_report_ref": "verifier-work-report/v1",
            "quality_policy_plan_ref": "quality-policy-plan/v1",
            "final_observation_manifest_ref": "observation-manifest/v1",
        }
        for field, target in expected.items():
            self.assertEqual(target, props[field]["x-pullwise-content-schema-id"])

    def test_complete_fixtures_execute_and_retry_byte_exactly(self) -> None:
        self.assert_fixture_matrix(
            {"verification-attestation/v1": self.valid_attestation}
        )

    def test_negative_run_status_obeys_fixed_priority(self) -> None:
        fixtures = {item["fixture_id"]: item for item in self.family["fixtures"]}
        invalid = fixtures["task_attestation_negative_run_status"]["document"]
        self.assertEqual("NEEDS_WORK", invalid["requirement_verdicts"][0]["verdict"])
        self.assertEqual("PASS", invalid["run_status"])


if __name__ == "__main__":
    unittest.main()
