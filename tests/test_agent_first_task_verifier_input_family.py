from __future__ import annotations

from pathlib import Path
import unittest

from tests.agent_first_task_evidence_support import (
    FamilyAssertions,
    ordered_unique,
    sealed,
    valid_availability,
    valid_content_ref,
)


ROOT = Path(__file__).resolve().parents[1]
FAMILY_PATH = ROOT / "contracts/agent-first/current/source/families/task-verifier-input.json"
SCHEMAS = ("verifier-input-manifest/v1",)
HELPERS = {"verifier-input-manifest/v1": ["verify_verifier_input_context"]}


class TaskVerifierInputFamilyTest(FamilyAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.load_family(FAMILY_PATH)

    def valid_input(self, document: dict[str, object]) -> bool:
        refs = document["artifact_refs"]
        rules = document["engineering_rule_refs"]
        return (
            sealed(document, self.schemas["verifier-input-manifest/v1"])
            and document["owner_conclusion_excluded"] is True
            and valid_content_ref(
                document["task_request_ref"], {"agent-task-request/v1"}
            )
            and valid_content_ref(
                document["effective_policy_ref"], {"agent-task-policy/v1"}
            )
            and valid_content_ref(
                document["requirement_ledger_ref"], {"requirement-ledger/v1"}
            )
            and valid_content_ref(document["charter_ref"], {"task-charter/v1"})
            and valid_content_ref(
                document["completion_proposal_ref"], {"completion-proposal/v1"}
            )
            and valid_content_ref(
                document["quality_policy_plan_ref"], {"quality-policy-plan/v1"}
            )
            and valid_content_ref(
                document["original_source_ref"], {"source-tree-manifest/v1"}
            )
            and valid_content_ref(
                document["final_source_ref"], {"source-tree-manifest/v1"}
            )
            and valid_availability(document["change_set"], {"change-set/v1"})
            and valid_content_ref(
                document["pre_verifier_observation_manifest_ref"],
                {"pre-verifier-observation-manifest/v1"},
            )
            and ordered_unique(refs, lambda item: item["artifact_id"])
            and ordered_unique(
                rules, lambda item: (item["artifact_id"], item["sha256"])
            )
            and all(
                valid_content_ref(item, {"source-content/v1"}) for item in rules
            )
            and document["requirement_ids"]
            == sorted(set(document["requirement_ids"]))
        )

    def test_closed_sorted_schema_and_semantic_registry(self) -> None:
        self.assert_family_contract("task-verifier-input", SCHEMAS, HELPERS)

    def test_input_edges_are_finite_and_typed(self) -> None:
        props = self.schemas["verifier-input-manifest/v1"]["properties"]
        self.assertEqual(
            "pre-verifier-observation-manifest/v1",
            props["pre_verifier_observation_manifest_ref"][
                "x-pullwise-content-schema-id"
            ],
        )
        self.assertEqual(
            "quality-policy-plan/v1",
            props["quality_policy_plan_ref"]["x-pullwise-content-schema-id"],
        )
        self.assertEqual(
            "artifact-content-ref/v1", props["artifact_refs"]["items"]["$ref"]
        )

    def test_complete_fixtures_execute_and_retry_byte_exactly(self) -> None:
        self.assert_fixture_matrix(
            {"verifier-input-manifest/v1": self.valid_input}
        )

    def test_negative_excludes_owner_conclusion_bias(self) -> None:
        fixtures = {item["fixture_id"]: item for item in self.family["fixtures"]}
        self.assertIs(
            False,
            fixtures["task_verifier_input_negative_owner_conclusion"]["document"][
                "owner_conclusion_excluded"
            ],
        )


if __name__ == "__main__":
    unittest.main()
