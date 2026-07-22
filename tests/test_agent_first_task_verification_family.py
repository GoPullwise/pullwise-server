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
FAMILY_PATH = ROOT / "contracts/agent-first/current/source/families/task-verification.json"
SCHEMAS = (
    "completion-proposal/v1",
    "verification-attestation-manifest/v1",
    "verification-attestation/v1",
    "verifier-input-manifest/v1",
    "verifier-work-report/v1",
)


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

    def valid_input(self, document: dict[str, object]) -> bool:
        refs = document["artifact_refs"]
        rules = document["engineering_rule_refs"]
        return (
            sealed(document, self.schemas["verifier-input-manifest/v1"])
            and document["owner_conclusion_excluded"] is True
            and valid_content_ref(document["task_request_ref"], {"agent-task-request/v1"})
            and valid_content_ref(document["effective_policy_ref"], {"agent-task-policy/v1"})
            and valid_content_ref(document["requirement_ledger_ref"], {"requirement-ledger/v1"})
            and valid_content_ref(document["charter_ref"], {"task-charter/v1"})
            and valid_content_ref(document["completion_proposal_ref"], {"completion-proposal/v1"})
            and valid_content_ref(document["original_source_ref"], {"source-tree-manifest/v1"})
            and valid_content_ref(document["final_source_ref"], {"source-tree-manifest/v1"})
            and valid_availability(document["change_set"], {"change-set/v1"})
            and valid_content_ref(
                document["pre_verifier_observation_manifest_ref"],
                {"pre-verifier-observation-manifest/v1"},
            )
            and ordered_unique(
                refs,
                lambda item: (
                    item["content_schema_id"],
                    item["artifact_id"],
                    item["sha256"],
                ),
            )
            and all(valid_content_ref(item) for item in refs)
            and ordered_unique(rules, lambda item: (item["artifact_id"], item["sha256"]))
            and all(valid_content_ref(item, {"source-content/v1"}) for item in rules)
            and document["requirement_ids"] == sorted(set(document["requirement_ids"]))
        )

    def valid_work(self, document: dict[str, object]) -> bool:
        assessments = document["provisional_requirement_assessments"]
        return (
            sealed(document, self.schemas["verifier-work-report/v1"])
            and valid_content_ref(
                document["verifier_input_manifest_ref"], {"verifier-input-manifest/v1"}
            )
            and document["counterexamples_searched"]
            == sorted(set(document["counterexamples_searched"]))
            and document["own_observation_ids"]
            == sorted(set(document["own_observation_ids"]))
            and bool(document["own_observation_ids"])
            and document["limitations"] == sorted(set(document["limitations"]))
            and ordered_unique(assessments, lambda item: item["requirement_id"])
            and all(
                item["verdict"] != "PASS" or not item["limitations"]
                for item in assessments
            )
        )

    def valid_attestation(self, document: dict[str, object]) -> bool:
        verdicts = document["requirement_verdicts"]
        return (
            sealed(document, self.schemas["verification-attestation/v1"])
            and valid_content_ref(
                document["verifier_input_manifest_ref"], {"verifier-input-manifest/v1"}
            )
            and valid_content_ref(
                document["final_observation_manifest_ref"], {"observation-manifest/v1"}
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

    def valid_attestation_manifest(self, document: dict[str, object]) -> bool:
        attestations = document["attestations"]
        slots = {item["slot_id"] for item in attestations}
        if not (
            sealed(document, self.schemas["verification-attestation-manifest/v1"])
            and valid_content_ref(
                document["final_observation_manifest_ref"], {"observation-manifest/v1"}
            )
            and ordered_unique(attestations, lambda item: (item["slot_id"], item["attestation_id"]))
            and all(
                valid_content_ref(item["attestation_ref"], {"verification-attestation/v1"})
                for item in attestations
            )
            and ordered_unique(
                document["requirement_aggregates"], lambda item: item["requirement_id"]
            )
        ):
            return False
        for aggregate in document["requirement_aggregates"]:
            required = set(aggregate["required_slot_ids"])
            missing = required.difference(slots)
            if missing and aggregate["verdict"] != "UNVERIFIABLE":
                return False
            if not set(aggregate["attestation_ids"]).issubset(
                {item["attestation_id"] for item in attestations}
            ):
                return False
        return True

    def valid_proposal(self, document: dict[str, object]) -> bool:
        claims = document["requirement_claims"]
        no_change = document["outcome_requested"] == "NO_CHANGE_NEEDED"
        return (
            sealed(document, self.schemas["completion-proposal/v1"])
            and document["execution_state_ids"]
            == sorted(set(document["execution_state_ids"]))
            and ordered_unique(document["artifact_refs"], lambda item: (
                item["content_schema_id"], item["artifact_id"], item["sha256"]
            ))
            and all(valid_content_ref(item) for item in document["artifact_refs"])
            and ordered_unique(claims, lambda item: item["requirement_id"])
            and all(item["evidence_ids"] == sorted(set(item["evidence_ids"])) for item in claims)
            and document["known_gaps"] == sorted(set(document["known_gaps"]))
            and document["residual_risks"] == sorted(set(document["residual_risks"]))
            and (
                not no_change
                or document["change_set_ref"] is None
                and document["original_source_state_id"] == document["final_source_state_id"]
            )
        )

    def test_closed_sorted_schema_and_semantic_registry(self) -> None:
        self.assert_family_contract("task-verification", SCHEMAS)

    def test_verifier_inputs_and_attestations_use_exact_typed_refs(self) -> None:
        props = self.schemas["verifier-input-manifest/v1"]["properties"]
        self.assertEqual(
            "pre-verifier-observation-manifest/v1",
            props["pre_verifier_observation_manifest_ref"]["x-pullwise-content-schema-id"],
        )
        attestation = self.schemas["verification-attestation/v1"]["properties"]
        self.assertEqual(
            "observation-manifest/v1",
            attestation["final_observation_manifest_ref"]["x-pullwise-content-schema-id"],
        )
        self.assertEqual(
            "verifier-input-manifest/v1",
            attestation["verifier_input_manifest_ref"]["x-pullwise-content-schema-id"],
        )

    def test_complete_fixtures_execute_and_retry_byte_exactly(self) -> None:
        self.assert_fixture_matrix(
            {
                "completion-proposal/v1": self.valid_proposal,
                "verification-attestation-manifest/v1": self.valid_attestation_manifest,
                "verification-attestation/v1": self.valid_attestation,
                "verifier-input-manifest/v1": self.valid_input,
                "verifier-work-report/v1": self.valid_work,
            }
        )

    def test_negative_fixtures_cover_priority_missing_slot_and_owner_bias(self) -> None:
        fixtures = {item["fixture_id"]: item for item in self.family["fixtures"]}
        self.assertEqual(
            "PASS",
            fixtures["task_verification_negative_attestation_priority"]["document"]["run_status"],
        )
        self.assertIs(
            False,
            fixtures["task_verification_negative_owner_conclusion"]["document"][
                "owner_conclusion_excluded"
            ],
        )
        aggregate = fixtures["task_verification_negative_missing_slot"]["document"][
            "requirement_aggregates"
        ][0]
        self.assertEqual("PASS", aggregate["verdict"])


if __name__ == "__main__":
    unittest.main()
