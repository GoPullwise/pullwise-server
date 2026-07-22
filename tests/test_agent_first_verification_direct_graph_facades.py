from __future__ import annotations

from copy import deepcopy
import unittest

from tests.agent_first_verification_direct_graph_support import (
    ALIASES,
    VerificationDirectGraphHarness,
)


class AgentFirstVerificationDirectGraphFacadesTest(
    VerificationDirectGraphHarness, unittest.TestCase
):
    def test_00_public_exports_exist(self) -> None:
        python, node = self.public_helper_exports()
        self.assertEqual({name: True for name in ALIASES}, python)
        self.assertEqual(
            {name: {"snake": True, "camel": True, "same": True} for name in ALIASES},
            node,
        )

    def test_01_graph_documents_are_intrinsically_valid(self) -> None:
        graph = self.build_graph()
        cases = [
            ("task-record/v1", graph["task_snapshot"]),
            ("attempt-record/v1", graph["attempt"]),
            ("task-owner/v1", graph["owner"]),
            ("observation/v1", graph["owner_observation"]),
            ("observation/v1", graph["verifier_observation"]),
            ("effective-execution-policy/v1", graph["policy"]),
            ("requirement-ledger/v1", graph["ledger"]),
            ("task-charter/v1", graph["charter"]),
            ("execution-profile/v1", graph["profile"]),
            ("source-selection-policy/v1", graph["selection_policy"]),
            ("source-tree-manifest/v1", graph["original_source"]),
            ("source-tree-manifest/v1", graph["final_source"]),
            ("change-set-patch/v1", graph["patch"]),
            ("change-set/v1", graph["change_set"]),
            ("pre-verifier-observation-manifest/v1", graph["pre_manifest"]),
            ("observation-manifest/v1", graph["final_manifest"]),
            ("execution-state-manifest/v1", graph["execution_states"][0]),
            ("completion-proposal/v1", graph["proposal"]),
            ("quality-policy-plan/v1", graph["plan"]),
            ("source-content/v1", graph["engineering_rules"][0]),
            ("verifier-input-manifest/v1", graph["input"]),
            ("verifier-work-report/v1", graph["work"]),
            ("verification-attestation/v1", graph["attestation"]),
            ("verification-attestation-manifest/v1", graph["aggregate"]),
        ]
        python, node = self.document_results(cases)
        self.assertEqual(python, node)
        for (schema_id, document), result in zip(cases, python):
            with self.subTest(schema_id=schema_id):
                self.assertEqual({"ok": True, "value": document}, result)

    def test_02_helper_parity_and_targeted_outcomes(self) -> None:
        graph = self.build_graph()
        bad_owner = deepcopy(graph["owner"])
        bad_owner["owner_epoch"] += 1
        bad_sha = self.reseal(
            "verifier-input-manifest/v1",
            {**deepcopy(graph["input"]), "completion_proposal_ref": {**graph["input"]["completion_proposal_ref"], "sha256": "0" * 64}},
        )
        bad_digest = self.reseal(
            "verifier-input-manifest/v1",
            {**deepcopy(graph["input"]), "quality_policy_plan_digest": "0" * 64},
        )
        bad_work = self.reseal(
            "verifier-work-report/v1",
            {**deepcopy(graph["work"]), "provisional_requirement_assessments": [deepcopy(graph["work"]["provisional_requirement_assessments"][0])]},
        )
        bad_manifest = self.reseal(
            "observation-manifest/v1",
            {**deepcopy(graph["final_manifest"]), "entries": [deepcopy(graph["final_manifest"]["entries"][0]), {**deepcopy(graph["final_manifest"]["entries"][1]), "actor": {**graph["final_manifest"]["entries"][1]["actor"], "session_id": "sess_" + "3" * 32}}]},
        )
        bad_aggregate = self.reseal(
            "verification-attestation-manifest/v1",
            {**deepcopy(graph["aggregate"]), "created_at": "2026-07-22T00:00:03.400Z"},
        )
        operations = [
            {"python": "verify_completion_proposal_context", "node": "verifyCompletionProposalContext", "args": [graph["proposal"], graph["task_snapshot"], graph["attempt"], graph["owner"], graph["request"], graph["policy"], graph["ledger"], graph["charter"], graph["original_source"], graph["final_source"], graph["execution_states"], graph["change_set"], graph["pre_manifest"]]},
            {"python": "verify_verifier_input_context", "node": "verifyVerifierInputContext", "args": [graph["input"], graph["proposal"], graph["plan"], graph["request"], graph["policy"], graph["ledger"], graph["charter"], graph["original_source"], graph["final_source"], graph["change_set"], graph["pre_manifest"], graph["engineering_rules"]]},
            {"python": "verify_verifier_work_context", "node": "verifyVerifierWorkContext", "args": [graph["work"], graph["input"], graph["proposal"], graph["final_manifest"]]},
            {"python": "verify_attestation_context", "node": "verifyAttestationContext", "args": [graph["attestation"], graph["input"], graph["work"], graph["proposal"], graph["plan"], graph["final_source"], graph["execution_states"], graph["final_manifest"]]},
            {"python": "verify_attestation_manifest_context", "node": "verifyAttestationManifestContext", "args": [graph["aggregate"], graph["plan"], graph["final_manifest"], [graph["attestation"]]]},
            {"python": "verify_completion_proposal_context", "node": "verifyCompletionProposalContext", "args": [graph["proposal"], graph["task_snapshot"], graph["attempt"], bad_owner, graph["request"], graph["policy"], graph["ledger"], graph["charter"], graph["original_source"], graph["final_source"], graph["execution_states"], graph["change_set"], graph["pre_manifest"]]},
            {"python": "verify_verifier_input_context", "node": "verifyVerifierInputContext", "args": [bad_sha, graph["proposal"], graph["plan"], graph["request"], graph["policy"], graph["ledger"], graph["charter"], graph["original_source"], graph["final_source"], graph["change_set"], graph["pre_manifest"], graph["engineering_rules"]]},
            {"python": "verify_verifier_input_context", "node": "verifyVerifierInputContext", "args": [bad_digest, graph["proposal"], graph["plan"], graph["request"], graph["policy"], graph["ledger"], graph["charter"], graph["original_source"], graph["final_source"], graph["change_set"], graph["pre_manifest"], graph["engineering_rules"]]},
            {"python": "verify_verifier_work_context", "node": "verifyVerifierWorkContext", "args": [bad_work, graph["input"], graph["proposal"], graph["final_manifest"]]},
            {"python": "verify_attestation_context", "node": "verifyAttestationContext", "args": [graph["attestation"], graph["input"], graph["work"], graph["proposal"], graph["plan"], graph["final_source"], graph["execution_states"], bad_manifest]},
            {"python": "verify_attestation_manifest_context", "node": "verifyAttestationManifestContext", "args": [bad_aggregate, graph["plan"], graph["final_manifest"], [graph["attestation"]]]},
        ]
        python, node = self.helper_results(operations)
        self.assertEqual([True] * len(node), [item["same"] for item in node])
        self.assertEqual(python, [item["camel"] for item in node])
        self.assertEqual([item["camel"] for item in node], [item["snake"] for item in node])
        expected = [
            {"ok": True, "value": graph["proposal"]},
            {"ok": True, "value": graph["input"]},
            {"ok": True, "value": graph["work"]},
            {"ok": True, "value": graph["attestation"]},
            {"ok": True, "value": graph["aggregate"]},
            {"ok": False, "code": "CONTRACT_DOCUMENT_INVALID", "detail": "VERIFICATION_CONTEXT_INVALID", "path": "$.owner_epoch"},
            {"ok": False, "code": "CONTRACT_DOCUMENT_INVALID", "detail": "VERIFICATION_CONTEXT_CAS_CORRUPT", "path": "$.completion_proposal_ref"},
            {"ok": False, "code": "CONTRACT_DOCUMENT_INVALID", "detail": "VERIFICATION_CONTEXT_DIGEST_INVALID", "path": "$.quality_policy_plan_digest"},
            {"ok": False, "code": "CONTRACT_DOCUMENT_INVALID", "detail": "VERIFICATION_CONTEXT_INVALID", "path": "$.provisional_requirement_assessments"},
            {"ok": False, "code": "CONTRACT_DOCUMENT_INVALID", "detail": "VERIFICATION_CONTEXT_INVALID", "path": "$.own_observation_ids[0]"},
            {"ok": False, "code": "CONTRACT_DOCUMENT_INVALID", "detail": "VERIFICATION_CONTEXT_TIME_INVALID", "path": "$.created_at"},
        ]
        self.assertEqual(expected, python)


if __name__ == "__main__":
    unittest.main()
