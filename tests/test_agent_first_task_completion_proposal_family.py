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
FAMILY_PATH = (
    ROOT
    / "contracts/agent-first/current/source/families/task-completion-proposal.json"
)
SCHEMAS = ("completion-proposal/v1",)
HELPERS = {"completion-proposal/v1": ["verify_completion_proposal_context"]}


class TaskCompletionProposalFamilyTest(FamilyAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.load_family(FAMILY_PATH)

    def valid_proposal(self, document: dict[str, object]) -> bool:
        claims = document["requirement_claims"]
        change_set = document["change_set_ref"]
        no_change = document["outcome_requested"] == "NO_CHANGE_NEEDED"
        return (
            sealed(document, self.schemas["completion-proposal/v1"])
            and document["execution_state_ids"]
            == sorted(set(document["execution_state_ids"]))
            and (
                change_set is None
                or valid_content_ref(change_set, {"change-set/v1"})
            )
            and ordered_unique(
                document["artifact_refs"],
                lambda item: item["artifact_id"],
            )
            and ordered_unique(claims, lambda item: item["requirement_id"])
            and all(
                item["evidence_ids"] == sorted(set(item["evidence_ids"]))
                for item in claims
            )
            and document["known_gaps"] == sorted(set(document["known_gaps"]))
            and document["residual_risks"]
            == sorted(set(document["residual_risks"]))
            and (
                not no_change
                or change_set is None
                and document["original_source_state_id"]
                == document["final_source_state_id"]
            )
        )

    def test_closed_sorted_schema_and_semantic_registry(self) -> None:
        self.assert_family_contract("task-completion-proposal", SCHEMAS, HELPERS)

    def test_change_set_and_delivery_artifacts_are_typed(self) -> None:
        props = self.schemas["completion-proposal/v1"]["properties"]
        self.assertEqual(
            "change-set/v1",
            props["change_set_ref"]["oneOf"][0]["x-pullwise-content-schema-id"],
        )
        self.assertEqual(
            "artifact-content-ref/v1", props["artifact_refs"]["items"]["$ref"]
        )

    def test_complete_fixtures_execute_and_retry_byte_exactly(self) -> None:
        self.assert_fixture_matrix(
            {"completion-proposal/v1": self.valid_proposal}
        )

    def test_no_change_negative_cannot_hide_source_drift(self) -> None:
        fixtures = {item["fixture_id"]: item for item in self.family["fixtures"]}
        invalid = fixtures["task_completion_negative_no_change_source_drift"][
            "document"
        ]
        self.assertEqual("NO_CHANGE_NEEDED", invalid["outcome_requested"])
        self.assertNotEqual(
            invalid["original_source_state_id"], invalid["final_source_state_id"]
        )


if __name__ == "__main__":
    unittest.main()
