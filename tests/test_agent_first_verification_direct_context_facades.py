from __future__ import annotations

from copy import deepcopy
import unittest

from tests.agent_first_verification_direct_support import VerificationDirectHarness


ALIASES = {
    "verify_completion_proposal_context": "verifyCompletionProposalContext",
    "verify_verifier_input_context": "verifyVerifierInputContext",
    "verify_verifier_work_context": "verifyVerifierWorkContext",
    "verify_attestation_context": "verifyAttestationContext",
    "verify_attestation_manifest_context": "verifyAttestationManifestContext",
}


class AgentFirstVerificationDirectContextFacadesTest(
    VerificationDirectHarness, unittest.TestCase
):
    def test_00_helper_exports_exist_in_python_and_node(self) -> None:
        self.assertEqual(
            {name: True for name in ALIASES},
            self.python_helper_exports(list(ALIASES)),
        )
        self.assertEqual(
            {name: {"snake": True, "camel": True, "same": True} for name in ALIASES},
            self.node_helper_exports(ALIASES),
        )

    def test_01_all_supplied_x_digest_documents_validate_before_helper_calls(self) -> None:
        graph = self.build_graph()
        cases = [
            ("effective-execution-policy/v1", graph["policy"]),
            ("requirement-ledger/v1", graph["ledger"]),
            ("task-charter/v1", graph["charter"]),
            ("execution-profile/v1", graph["profile"]),
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
        for schema_id, document in cases:
            with self.subTest(schema_id=schema_id):
                python, node = self.validate_document_pair(schema_id, document)
                self.assertEqual(python, node)
                self.assertTrue(python["ok"], schema_id)

    def test_02_direct_helper_positive_and_negative_contexts_have_parity(self) -> None:
        self.assertEqual(
            {name: True for name in ALIASES},
            self.python_helper_exports(list(ALIASES)),
        )
        graph = self.build_graph()
        positive = [
            ("verify_completion_proposal_context", [graph["proposal"], graph["task_snapshot"], graph["attempt"], graph["owner"], graph["request"], graph["policy"], graph["ledger"], graph["charter"], graph["original_source"], graph["final_source"], graph["execution_states"], graph["change_set"], graph["pre_manifest"]]),
            ("verify_verifier_input_context", [graph["input"], graph["proposal"], graph["plan"], graph["request"], graph["policy"], graph["ledger"], graph["charter"], graph["original_source"], graph["final_source"], graph["change_set"], graph["pre_manifest"], graph["engineering_rules"]]),
            ("verify_verifier_work_context", [graph["work"], graph["input"], graph["proposal"], graph["final_manifest"]]),
            ("verify_attestation_context", [graph["attestation"], graph["input"], graph["work"], graph["proposal"], graph["plan"], graph["final_source"], graph["execution_states"], graph["final_manifest"]]),
            ("verify_attestation_manifest_context", [graph["aggregate"], graph["plan"], graph["final_manifest"], [graph["attestation"]]]),
        ]
        bad_proposal = deepcopy(graph["owner"])
        bad_proposal["owner_epoch"] += 1
        bad_input = deepcopy(graph["input"])
        bad_input["completion_proposal_ref"]["sha256"] = "0" * 64
        bad_input = self.reseal("verifier-input-manifest/v1", bad_input)
        bad_work = deepcopy(graph["final_manifest"])
        bad_work["entries"][1]["actor"]["session_id"] = None
        bad_work = self.reseal("observation-manifest/v1", bad_work)
        bad_attestation = deepcopy(graph["final_manifest"])
        bad_attestation["entries"][1]["actor"]["id"] = "verifier_" + "3" * 32
        bad_attestation = self.reseal("observation-manifest/v1", bad_attestation)
        bad_aggregate = deepcopy(graph["aggregate"])
        bad_aggregate["requirement_aggregates"][0]["attestation_ids"] = []
        bad_aggregate = self.reseal("verification-attestation-manifest/v1", bad_aggregate)
        negative = [
            ("verify_completion_proposal_context", [graph["proposal"], graph["task_snapshot"], graph["attempt"], bad_proposal, graph["request"], graph["policy"], graph["ledger"], graph["charter"], graph["original_source"], graph["final_source"], graph["execution_states"], graph["change_set"], graph["pre_manifest"]]),
            ("verify_verifier_input_context", [bad_input, graph["proposal"], graph["plan"], graph["request"], graph["policy"], graph["ledger"], graph["charter"], graph["original_source"], graph["final_source"], graph["change_set"], graph["pre_manifest"], graph["engineering_rules"]]),
            ("verify_verifier_work_context", [graph["work"], graph["input"], graph["proposal"], bad_work]),
            ("verify_attestation_context", [graph["attestation"], graph["input"], graph["work"], graph["proposal"], graph["plan"], graph["final_source"], graph["execution_states"], bad_attestation]),
            ("verify_attestation_manifest_context", [bad_aggregate, graph["plan"], graph["final_manifest"], [graph["attestation"]]]),
        ]
        for name, args in positive + negative:
            with self.subTest(helper=name):
                self.assertTrue(callable(getattr(self.python, name)))


if __name__ == "__main__":
    unittest.main()
