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
FAMILY_PATH = ROOT / "contracts/agent-first/current/source/families/task-verifier-work.json"
SCHEMAS = ("verifier-work-report/v1",)
HELPERS = {"verifier-work-report/v1": ["verify_verifier_work_context"]}


class TaskVerifierWorkFamilyTest(FamilyAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.load_family(FAMILY_PATH)

    def valid_work(self, document: dict[str, object]) -> bool:
        assessments = document["provisional_requirement_assessments"]
        return (
            sealed(document, self.schemas["verifier-work-report/v1"])
            and document["sandbox_mode"] == "read_only_or_cow"
            and valid_content_ref(
                document["verifier_input_manifest_ref"],
                {"verifier-input-manifest/v1"},
            )
            and document["counterexamples_searched"]
            == sorted(set(document["counterexamples_searched"]))
            and document["own_observation_ids"]
            == sorted(set(document["own_observation_ids"]))
            and bool(document["own_observation_ids"])
            and document["limitations"] == sorted(set(document["limitations"]))
            and ordered_unique(assessments, lambda item: item["requirement_id"])
            and all(
                item["evidence_ids"] == sorted(set(item["evidence_ids"]))
                and item["limitations"] == sorted(set(item["limitations"]))
                and (item["verdict"] != "PASS" or not item["limitations"])
                for item in assessments
            )
        )

    def test_closed_sorted_schema_and_semantic_registry(self) -> None:
        self.assert_family_contract("task-verifier-work", SCHEMAS, HELPERS)

    def test_work_report_binds_exact_input_manifest(self) -> None:
        props = self.schemas["verifier-work-report/v1"]["properties"]
        self.assertEqual(
            "verifier-input-manifest/v1",
            props["verifier_input_manifest_ref"]["x-pullwise-content-schema-id"],
        )
        self.assertEqual("read_only_or_cow", props["sandbox_mode"]["const"])

    def test_complete_fixtures_execute_and_retry_byte_exactly(self) -> None:
        self.assert_fixture_matrix(
            {"verifier-work-report/v1": self.valid_work}
        )

    def test_negative_requires_fresh_independent_observation(self) -> None:
        fixtures = {item["fixture_id"]: item for item in self.family["fixtures"]}
        self.assertEqual(
            [],
            fixtures["task_verifier_work_negative_without_observation"]["document"][
                "own_observation_ids"
            ],
        )


if __name__ == "__main__":
    unittest.main()
