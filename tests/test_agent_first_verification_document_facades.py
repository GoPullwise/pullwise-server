from __future__ import annotations

from copy import deepcopy
import unittest

from tests.agent_first_verification_facade_support import (
    VerificationFacadeHarness,
)


SCHEMA_IDS = (
    "completion-proposal/v1",
    "verifier-input-manifest/v1",
    "verifier-work-report/v1",
    "verification-attestation/v1",
    "verification-attestation-manifest/v1",
)

HELPER_ALIASES = {
    "verify_completion_proposal_context": "verifyCompletionProposalContext",
    "verify_verifier_input_context": "verifyVerifierInputContext",
    "verify_verifier_work_context": "verifyVerifierWorkContext",
    "verify_attestation_context": "verifyAttestationContext",
    "verify_attestation_manifest_context": "verifyAttestationManifestContext",
}


class AgentFirstVerificationDocumentFacadesTest(
    VerificationFacadeHarness, unittest.TestCase
):
    def test_source_fixtures_have_python_npm_expected_code_parity(self) -> None:
        fixture_cases = self.fixture_cases(SCHEMA_IDS)
        results = self.assert_document_parity([case for _, case in fixture_cases])

        for (fixture, (_, document)), result in zip(fixture_cases, results):
            with self.subTest(fixture_id=fixture["fixture_id"]):
                self.assertEqual(
                    fixture["expected_code"],
                    None if result["ok"] else result["code"],
                )
                if fixture["expected_code"] is None:
                    self.assertEqual(document, result["value"])

    def test_idempotency_fixtures_are_byte_identical_to_golden_fixtures(self) -> None:
        seen: dict[str, bytes] = {}
        for fixture, (_, document) in self.fixture_cases(SCHEMA_IDS):
            schema_id = fixture["schema_id"]
            if fixture["fixture_class"] == "golden":
                seen[schema_id] = self.python.canonical_document_bytes(document)
            if fixture["fixture_class"] == "idempotency":
                self.assertEqual(
                    seen[schema_id],
                    self.python.canonical_document_bytes(document),
                    fixture["fixture_id"],
                )

    def test_embedded_digest_drift_has_exact_python_node_parity(self) -> None:
        cases: list[tuple[str, dict[str, object]]] = []
        expected = []
        for fixture_id, schema_id in (
            ("task_completion_golden_proposal", "completion-proposal/v1"),
            ("task_verifier_input_golden_input", "verifier-input-manifest/v1"),
            ("task_verifier_work_golden_work", "verifier-work-report/v1"),
            ("task_attestation_golden_attestation", "verification-attestation/v1"),
            (
                "task_verification_golden_attestation_manifest",
                "verification-attestation-manifest/v1",
            ),
        ):
            document = self.fixture_document(fixture_id)
            field = self.schemas[schema_id]["x-pullwise-digest"]["field"]
            document[field] = ("0" if document[field][0] != "0" else "1") + document[field][1:]
            cases.append((schema_id, document))
            expected.append((field, schema_id))

        results = self.assert_document_parity(cases)

        self.assertEqual(
            [
                {
                    "ok": False,
                    "code": "CONTRACT_DOCUMENT_INVALID",
                    "detail": "CONTRACT_DIGEST_MISMATCH",
                    "path": f"$.{field}",
                }
                for field, _ in expected
            ],
            results,
        )

    def test_declared_rules_reject_resealed_semantic_drift(self) -> None:
        proposal = self.fixture_document("task_completion_golden_proposal")
        proposal["final_source_state_id"] = "3" * 64
        proposal = self.reseal("completion-proposal/v1", proposal)

        verifier_input = self.fixture_document("task_verifier_input_golden_input")
        verifier_input["owner_conclusion_excluded"] = False
        verifier_input = self.reseal("verifier-input-manifest/v1", verifier_input)

        verifier_work = self.fixture_document("task_verifier_work_golden_work")
        verifier_work["own_observation_ids"] = []
        verifier_work = self.reseal("verifier-work-report/v1", verifier_work)

        attestation = self.fixture_document("task_attestation_golden_attestation")
        attestation["requirement_verdicts"][0]["verdict"] = "NEEDS_WORK"
        attestation["requirement_verdicts"][0]["limitations"] = ["behavior regressed"]
        attestation = self.reseal("verification-attestation/v1", attestation)

        manifest = self.fixture_document(
            "task_verification_golden_attestation_manifest"
        )
        manifest["requirement_aggregates"][0]["required_slot_ids"].append(
            "slot_22222222222222222222222222222222"
        )
        manifest = self.reseal("verification-attestation-manifest/v1", manifest)

        cases = [
            ("completion-proposal/v1", proposal),
            ("verifier-input-manifest/v1", verifier_input),
            ("verifier-work-report/v1", verifier_work),
            ("verification-attestation/v1", attestation),
            ("verification-attestation-manifest/v1", manifest),
        ]
        results = self.assert_document_parity(cases)

        self.assertEqual(
            [
                ("PROPOSAL_NO_CHANGE_STATE_INVALID", "$"),
                ("VERIFIER_OWNER_CONCLUSION_INCLUDED", "$"),
                ("VERIFIER_OBSERVATION_REQUIRED", "$"),
                ("ATTESTATION_RUN_STATUS_INVALID", "$"),
                ("ATTESTATION_MISSING_SLOT_INVALID", "$"),
            ],
            [(item["detail"], item["path"]) for item in results],
        )
        self.assertTrue(all(item["code"] == "CONTRACT_DOCUMENT_INVALID" for item in results))

    def test_context_helpers_are_exported_without_alias_gaps(self) -> None:
        python_exports = self.python_helper_exports(list(HELPER_ALIASES))
        self.assertEqual(
            {name: True for name in HELPER_ALIASES},
            python_exports,
        )
        self.assertTrue(
            set(HELPER_ALIASES).issubset(set(self.python.__all__))
        )

        node_exports = self.node_helper_exports(HELPER_ALIASES)
        self.assertEqual(
            {
                name: {"snake": True, "camel": True, "same": True}
                for name in HELPER_ALIASES
            },
            node_exports,
        )


if __name__ == "__main__":
    unittest.main()
