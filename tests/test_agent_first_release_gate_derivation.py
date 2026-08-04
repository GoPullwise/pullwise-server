from __future__ import annotations

from copy import deepcopy
import unittest

from tests.release_gate_contract_test_support import (
    ReleaseGateContractTestCase,
)
from tests.release_gate_minimal_support import (
    bound_minimal_documents,
    rebind_minimal_documents,
)
from tests.release_gate_sample_set_support import (
    coherent_bootstrap_documents,
    coherent_stable_documents,
)


class AgentFirstReleaseGateDerivationTest(ReleaseGateContractTestCase):
    def test_derivation_rejects_sample_set_not_bound_to_benchmark(self) -> None:
        benchmark = self.contract.fixture(
            "benchmark_bundle_golden_current"
        )["document"]
        policy = self.contract.fixture(
            "release_gate_policy_golden_bootstrap"
        )["document"]
        sample_set = self.contract.fixture(
            "release_gate_sample_set_golden_bootstrap_included"
        )["document"]

        with self.assertRaises(self.contract.ContractValidationError) as raised:
            self.contract.derive_release_gate_evaluation(
                benchmark, policy, sample_set
            )
        self.assertEqual(
            "RELEASE_SAMPLE_BENCHMARK_BINDING_INVALID",
            raised.exception.detail,
        )

    def test_derivation_counts_complete_predeclared_candidate_samples(self) -> None:
        benchmark, policy, sample_set = coherent_bootstrap_documents(
            self.contract
        )
        observed = self.contract.derive_release_gate_evaluation(
            benchmark, policy, sample_set
        )
        self.assertEqual(
            {
                "raw_sample_count": 495,
                "valid_sample_count": 495,
                "excluded_sample_count": 0,
                "excluded_reason_counts": [],
            },
            {
                key: observed[key]
                for key in (
                    "raw_sample_count",
                    "valid_sample_count",
                    "excluded_sample_count",
                    "excluded_reason_counts",
                )
            },
        )

    def test_complete_bootstrap_derives_integer_metrics_and_pass(self) -> None:
        benchmark, policy, sample_set = coherent_bootstrap_documents(
            self.contract
        )
        observed = self.contract.derive_release_gate_evaluation(
            benchmark, policy, sample_set
        )
        self.assertEqual(
            observed,
            self.node_derivation(benchmark, policy, sample_set),
        )
        values = {
            item["gate_id"]: item["observed_value"]
            for item in observed["absolute_results"]
        }
        self.assertEqual(
            {
                "absolute_adversarial_false_verified_count": 0,
                "absolute_classification_accuracy_bps": 10000,
                "absolute_critical_false_verified_count": 0,
                "absolute_duplicate_effect_or_result_count": 0,
                "absolute_false_discovery_rate_bps": 0,
                "absolute_false_verified_rate_bps": 0,
                "absolute_false_verified_wilson_upper_bps": 80,
                "absolute_known_task_success_rate_bps": 10000,
                "absolute_known_unaided_completion_bps": 10000,
                "absolute_mandatory_requirement_coverage_bps": 10000,
                "absolute_safety_authority_violation_count": 0,
                "absolute_source_state_proof_coverage_bps": 10000,
                "absolute_stale_publish_count": 0,
                "absolute_unknown_task_success_rate_bps": 10000,
                "absolute_unknown_unaided_completion_bps": 10000,
            },
            values,
        )
        self.assertEqual([], observed["indeterminate_reason_codes"])
        self.assertTrue(
            all(
                item["status"] == "PASS"
                for item in observed["absolute_results"]
            )
        )
        self.assertTrue(
            all(
                item["observed_regression_bps"] is None
                and item["status"] == "NOT_APPLICABLE"
                for item in observed["relative_results"]
            )
        )
        self.assertEqual(
            [
                {
                    "profile_id": "profile_mvp_q1",
                    "wall_ms": 1000,
                    "token_count": 100,
                    "cost_microusd": 1000,
                    "status": "PASS",
                },
                {
                    "profile_id": "profile_mvp_q2",
                    "wall_ms": 1000,
                    "token_count": 100,
                    "cost_microusd": 1000,
                    "status": "PASS",
                },
            ],
            observed["profile_results"],
        )
        self.assertEqual(("PASS", 0), (observed["verdict"], observed["exit_code"]))

    def test_stable_equal_cohorts_derive_zero_relative_regression(self) -> None:
        benchmark, policy, sample_set = coherent_stable_documents(
            self.contract
        )
        observed = self.contract.derive_release_gate_evaluation(
            benchmark, policy, sample_set
        )
        self.assertEqual(
            observed,
            self.node_derivation(benchmark, policy, sample_set),
        )

        self.assertTrue(
            all(
                item["observed_regression_bps"] == 0
                and item["status"] == "PASS"
                for item in observed["relative_results"]
            )
        )
        self.assertEqual([], observed["indeterminate_reason_codes"])
        self.assertEqual(("PASS", 0), (observed["verdict"], observed["exit_code"]))

    def test_stable_relative_gates_use_directional_rates_and_p95(self) -> None:
        benchmark, policy, sample_set = coherent_stable_documents(
            self.contract
        )
        sample_set = deepcopy(sample_set)
        sample_set.pop("sample_set_digest")
        candidate = [
            item
            for item in sample_set["samples"]
            if item["cohort"] == "CANDIDATE"
        ]
        successes = [
            item
            for item in candidate
            if item["observation"]["terminal_outcome"] == "COMPLETED"
        ]
        for item in successes[:25]:
            item["observation"]["wall_ms"] = 1201
            item["observation"]["cost_microusd"] = 1201
        candidate[0]["observation"]["hidden_oracle_passed"] = False
        candidate[0]["observation"][
            "reported_oracle_in_scope_finding_count"
        ] = 0
        candidate[1]["observation"]["human_answer_supplied"] = True
        next(
            item
            for item in candidate
            if item["case_category"]
            == "ENVIRONMENT_OR_CAPABILITY_FAILURE"
        )["observation"]["terminal_outcome"] = "FAILED"
        sample_set = self.contract.seal_document(
            "release-gate-sample-set/v1", sample_set
        )

        observed = self.contract.derive_release_gate_evaluation(
            benchmark, policy, sample_set
        )
        regressions = {
            item["gate_id"]: item["observed_regression_bps"]
            for item in observed["relative_results"]
        }
        self.assertEqual(
            {
                "relative_classification_accuracy_drop_bps": 556,
                "relative_cost_increase_bps": 2010,
                "relative_false_discovery_increase_bps": 67,
                "relative_false_verified_regression_bps": 21,
                "relative_known_task_success_drop_bps": 29,
                "relative_known_unaided_completion_drop_bps": 57,
                "relative_unknown_task_success_drop_bps": 0,
                "relative_unknown_unaided_completion_drop_bps": 0,
                "relative_wall_time_increase_bps": 2010,
            },
            regressions,
        )
        self.assertEqual(("FAIL", 1), (observed["verdict"], observed["exit_code"]))

    def test_failed_samples_use_conservative_integer_rounding(self) -> None:
        benchmark, policy, sample_set = coherent_bootstrap_documents(
            self.contract
        )
        sample_set = deepcopy(sample_set)
        sample_set.pop("sample_set_digest")
        first = sample_set["samples"][0]["observation"]
        first["hidden_oracle_passed"] = False
        first["reported_oracle_in_scope_finding_count"] = 0
        first["covered_mandatory_requirement_count"] = 0
        first["covered_source_state_proof_count"] = 0
        first["safety_authority_violation_count"] = 1
        first["stale_publish_count"] = 1
        first["duplicate_effect_or_result_count"] = 1
        first["wall_ms"] = policy["profile_budgets"][0]["wall_ms"] + 1
        sample_set["samples"][1]["observation"][
            "human_answer_supplied"
        ] = True
        sample_set["samples"][18]["observation"][
            "terminal_outcome"
        ] = "FAILED"
        sample_set = self.contract.seal_document(
            "release-gate-sample-set/v1", sample_set
        )

        observed = self.contract.derive_release_gate_evaluation(
            benchmark, policy, sample_set
        )
        values = {
            item["gate_id"]: item["observed_value"]
            for item in observed["absolute_results"]
        }
        self.assertEqual(
            {
                "absolute_adversarial_false_verified_count": 1,
                "absolute_classification_accuracy_bps": 9444,
                "absolute_critical_false_verified_count": 1,
                "absolute_duplicate_effect_or_result_count": 1,
                "absolute_false_discovery_rate_bps": 67,
                "absolute_false_verified_rate_bps": 21,
                "absolute_false_verified_wilson_upper_bps": 118,
                "absolute_known_task_success_rate_bps": 9971,
                "absolute_known_unaided_completion_bps": 9943,
                "absolute_mandatory_requirement_coverage_bps": 9979,
                "absolute_safety_authority_violation_count": 1,
                "absolute_source_state_proof_coverage_bps": 9979,
                "absolute_stale_publish_count": 1,
                "absolute_unknown_task_success_rate_bps": 10000,
                "absolute_unknown_unaided_completion_bps": 10000,
            },
            values,
        )
        self.assertEqual("FAIL", observed["verdict"])
        self.assertEqual(1, observed["exit_code"])
        self.assertEqual("FAIL", observed["profile_results"][0]["status"])

    def test_derivation_exactly_binds_sample_set_to_policy(self) -> None:
        benchmark, policy, sample_set = bound_minimal_documents(self.contract)
        tampered = deepcopy(sample_set)
        tampered.pop("sample_set_digest")
        tampered["policy_digest"] = "0" * 64
        tampered = self.contract.seal_document(
            "release-gate-sample-set/v1", tampered
        )

        with self.assertRaises(self.contract.ContractValidationError) as raised:
            self.contract.derive_release_gate_evaluation(
                benchmark, policy, tampered
            )
        self.assertEqual(
            "RELEASE_SAMPLE_POLICY_BINDING_INVALID",
            raised.exception.detail,
        )

    def test_derivation_rejects_unapproved_exclusion_reason(self) -> None:
        benchmark, policy, sample_set = bound_minimal_documents(self.contract)
        tampered = deepcopy(sample_set)
        tampered.pop("sample_set_digest")
        sample = tampered["samples"][0]
        sample["disposition"] = "EXCLUDED"
        sample["infrastructure_reason_code"] = "INFRA_NOT_APPROVED"
        sample["observation"] = None
        tampered = self.contract.seal_document(
            "release-gate-sample-set/v1", tampered
        )

        with self.assertRaises(self.contract.ContractValidationError) as raised:
            self.contract.derive_release_gate_evaluation(
                benchmark, policy, tampered
            )
        self.assertEqual(
            "RELEASE_SAMPLE_EXCLUSION_REASON_INVALID",
            raised.exception.detail,
        )

    def test_sample_producer_and_completion_time_are_policy_bound(self) -> None:
        benchmark, policy, sample_set = bound_minimal_documents(self.contract)
        cases = (
            (
                "completed_at",
                "2026-08-01T00:00:00.000Z",
                "RELEASE_SAMPLE_TIME_INVALID",
            ),
            (
                "producer_id",
                policy["signer_id"],
                "RELEASE_SAMPLE_PRODUCER_INVALID",
            ),
        )
        for field, value, expected_detail in cases:
            with self.subTest(field=field):
                tampered = deepcopy(sample_set)
                tampered.pop("sample_set_digest")
                tampered[field] = value
                tampered = self.contract.seal_document(
                    "release-gate-sample-set/v1", tampered
                )
                with self.assertRaises(
                    self.contract.ContractValidationError
                ) as raised:
                    self.contract.derive_release_gate_evaluation(
                        benchmark, policy, tampered
                    )
                self.assertEqual(expected_detail, raised.exception.detail)

    def test_every_sample_profile_requires_a_signed_policy_budget(self) -> None:
        sample_set = deepcopy(
            self.contract.fixture(
                "release_gate_sample_set_golden_bootstrap_included"
            )["document"]
        )
        sample_set.pop("sample_set_digest")
        sample_set["samples"][0]["profile_id"] = "profile_unbudgeted"
        task = sample_set["samples"][0]
        inventory = [
            {
                field: task[field]
                for field in (
                    "task_id", "task_kind", "unknown_family_id",
                    "cluster_id", "case_category", "criticality",
                    "profile_id", "oracle_in_scope_finding_count",
                    "expected_failure_outcome",
                )
            }
        ]
        sample_set["task_inventory_digest"] = self.contract._release_digest(
            "pullwise:release-gate-task-inventory:v1", inventory
        )
        benchmark, policy, sample_set = rebind_minimal_documents(
            self.contract, sample_set
        )

        with self.assertRaises(self.contract.ContractValidationError) as raised:
            self.contract.derive_release_gate_evaluation(
                benchmark, policy, sample_set
            )
        self.assertEqual(
            "RELEASE_SAMPLE_PROFILE_INVALID",
            raised.exception.detail,
        )



if __name__ == "__main__":
    unittest.main()
