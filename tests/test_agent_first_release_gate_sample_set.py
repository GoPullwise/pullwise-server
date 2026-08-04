from __future__ import annotations

from copy import deepcopy
import unittest

from tests.release_gate_contract_test_support import (
    ReleaseGateContractTestCase,
)
from tests.release_gate_minimal_support import (
    bound_minimal_documents,
    bound_minimal_report,
)


class AgentFirstReleaseGateSampleSetTest(ReleaseGateContractTestCase):

    def test_public_derivation_rejects_missing_typed_inputs(self) -> None:
        with self.assertRaises(self.contract.ContractValidationError):
            self.contract.derive_release_gate_evaluation(None, None, None)

    def test_public_derivation_has_python_node_error_parity(self) -> None:
        with self.assertRaises(self.contract.ContractValidationError) as raised:
            self.contract.derive_release_gate_evaluation(None, None, None)
        expected = {
            "code": raised.exception.code,
            "detail": raised.exception.detail,
            "path": raised.exception.path,
        }
        self.assertEqual(expected, self.node_derivation_error())

    def test_public_derivation_has_python_node_projection_parity(self) -> None:
        benchmark, policy, sample_set = bound_minimal_documents(self.contract)
        expected = self.contract.derive_release_gate_evaluation(
            benchmark, policy, sample_set
        )
        self.assertEqual(
            expected,
            self.node_derivation(benchmark, policy, sample_set),
        )

    def test_sample_set_is_a_closed_public_included_excluded_union(self) -> None:
        schema = self.contract.schema("release-gate-sample-set/v1")
        self.assertIs(False, schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        samples = schema["properties"]["samples"]["items"]
        self.assertEqual(
            ["EXCLUDED", "INCLUDED"],
            sorted(
                branch["properties"]["disposition"]["const"]
                for branch in samples["oneOf"]
            ),
        )

        golden = self.contract.fixture(
            "release_gate_sample_set_golden_bootstrap_included"
        )
        self.assertEqual(
            golden["document"],
            self.contract.validate_document(
                "release-gate-sample-set/v1", golden["document"]
            ),
        )
        negative = self.contract.fixture(
            "release_gate_sample_set_negative_excluded_without_reason"
        )
        with self.assertRaises(self.contract.ContractValidationError) as raised:
            self.contract.validate_document(
                "release-gate-sample-set/v1", negative["document"]
            )
        self.assertEqual(negative["expected_code"], raised.exception.code)

    def test_report_schema_exactly_binds_typed_sample_set(self) -> None:
        schema = self.contract.schema("release-gate-report/v1")
        self.assertIn("sample_set_ref", schema["required"])
        self.assertIn("sample_set_digest", schema["required"])
        self.assertEqual(
            "release-gate-sample-set/v1",
            schema["properties"]["sample_set_ref"][
                "x-pullwise-content-schema-id"
            ],
        )
        self.assertEqual(
            "^[0-9a-f]{64}$",
            schema["properties"]["sample_set_digest"]["pattern"],
        )
        self.assertIn(
            "derive_release_gate_evaluation",
            schema["x-pullwise-semantics"]["contextual_helpers"],
        )

    def test_report_context_rederives_exact_sample_projection(self) -> None:
        benchmark, policy, sample_set, report = bound_minimal_report(
            self.contract
        )
        self.assertEqual(
            report,
            self.contract.verify_release_gate_report_context(
                report, benchmark, policy, sample_set
            ),
        )
        self.assertEqual(
            {"verdict": "INDETERMINATE", "exit_code": 2},
            self.contract.evaluate_release_gate(
                benchmark, policy, sample_set, report
            ),
        )

    def test_sample_identity_and_document_digest_are_deterministic(self) -> None:
        golden = self.contract.fixture(
            "release_gate_sample_set_golden_bootstrap_included"
        )["document"]
        idempotency = self.contract.fixture(
            "release_gate_sample_set_idempotency_bootstrap_included"
        )["document"]
        self.assertEqual(golden, idempotency)
        self.assertEqual(
            golden,
            self.contract.verify_document_digest(
                "release-gate-sample-set/v1", golden
            ),
        )

        tampered = deepcopy(golden)
        tampered.pop("sample_set_digest")
        tampered["samples"][0]["seed"] = 202
        with self.assertRaises(self.contract.ContractValidationError) as raised:
            self.contract.seal_document(
                "release-gate-sample-set/v1", tampered
            )
        self.assertEqual("RELEASE_SAMPLE_ID_INVALID", raised.exception.detail)

    def test_included_sample_requires_complete_observation_or_named_issue(self) -> None:
        golden = self.contract.fixture(
            "release_gate_sample_set_golden_bootstrap_included"
        )["document"]
        cases = []

        missing_without_issue = deepcopy(golden)
        missing_without_issue.pop("sample_set_digest")
        missing_without_issue["samples"][0]["observation"] = None
        cases.append(missing_without_issue)

        observation_with_issue = deepcopy(golden)
        observation_with_issue.pop("sample_set_digest")
        observation_with_issue["samples"][0]["evidence_issue_codes"] = [
            "EVIDENCE_MISSING"
        ]
        cases.append(observation_with_issue)

        for document in cases:
            with self.subTest(document=document):
                with self.assertRaises(
                    self.contract.ContractValidationError
                ) as raised:
                    self.contract.seal_document(
                        "release-gate-sample-set/v1", document
                    )
                self.assertEqual(
                    "RELEASE_SAMPLE_EVIDENCE_INVALID",
                    raised.exception.detail,
                )

    def test_evidence_issue_codes_are_canonical_ordered(self) -> None:
        golden = self.contract.fixture(
            "release_gate_sample_set_golden_bootstrap_included"
        )["document"]
        tampered = deepcopy(golden)
        tampered.pop("sample_set_digest")
        sample = tampered["samples"][0]
        sample["evidence_issue_codes"] = [
            "TIMEOUT", "EVIDENCE_MISSING"
        ]
        sample["observation"] = None

        with self.assertRaises(self.contract.ContractValidationError) as raised:
            self.contract.seal_document(
                "release-gate-sample-set/v1", tampered
            )
        self.assertEqual(
            "RELEASE_SAMPLE_EVIDENCE_ORDER_INVALID",
            raised.exception.detail,
        )

    def test_sample_kind_requires_matching_unknown_family(self) -> None:
        golden = self.contract.fixture(
            "release_gate_sample_set_golden_bootstrap_included"
        )["document"]
        tampered = deepcopy(golden)
        tampered.pop("sample_set_digest")
        tampered["samples"][0]["task_kind"] = "UNKNOWN"

        with self.assertRaises(self.contract.ContractValidationError) as raised:
            self.contract.seal_document(
                "release-gate-sample-set/v1", tampered
            )
        self.assertEqual(
            "RELEASE_SAMPLE_TASK_KIND_INVALID",
            raised.exception.detail,
        )

    def test_expected_failure_is_only_for_classification_tasks(self) -> None:
        golden = self.contract.fixture(
            "release_gate_sample_set_golden_bootstrap_included"
        )["document"]
        tampered = deepcopy(golden)
        tampered.pop("sample_set_digest")
        tampered["samples"][0]["expected_failure_outcome"] = "BLOCKED"

        with self.assertRaises(self.contract.ContractValidationError) as raised:
            self.contract.seal_document(
                "release-gate-sample-set/v1", tampered
            )
        self.assertEqual(
            "RELEASE_SAMPLE_EXPECTED_OUTCOME_INVALID",
            raised.exception.detail,
        )

    def test_observation_numerators_cannot_exceed_declared_facts(self) -> None:
        golden = self.contract.fixture(
            "release_gate_sample_set_golden_bootstrap_included"
        )["document"]
        cases = (
            ("reported_oracle_in_scope_finding_count", 2),
            ("covered_mandatory_requirement_count", 2),
            ("covered_source_state_proof_count", 2),
        )
        for field, value in cases:
            with self.subTest(field=field):
                tampered = deepcopy(golden)
                tampered.pop("sample_set_digest")
                tampered["samples"][0]["observation"][field] = value
                with self.assertRaises(
                    self.contract.ContractValidationError
                ) as raised:
                    self.contract.seal_document(
                        "release-gate-sample-set/v1", tampered
                    )
                self.assertEqual(
                    "RELEASE_SAMPLE_OBSERVATION_INVALID",
                    raised.exception.detail,
                )

if __name__ == "__main__":
    unittest.main()
