from __future__ import annotations

from copy import deepcopy

from tests.release_gate_sample_set_support import _content_ref, _digest


def rebind_minimal_documents(contract, sample_set):
    sample_set = deepcopy(sample_set)
    benchmark = deepcopy(
        contract.fixture("benchmark_bundle_golden_current")["document"]
    )
    benchmark.pop("bundle_digest")
    benchmark["task_inventory_digest"] = sample_set[
        "task_inventory_digest"
    ]
    benchmark = contract.seal_document("benchmark-bundle/v1", benchmark)

    policy = deepcopy(
        contract.fixture("release_gate_policy_golden_bootstrap")["document"]
    )
    policy.pop("policy_digest")
    policy["benchmark_ref"] = _content_ref(
        contract, policy["benchmark_ref"], benchmark
    )
    policy["benchmark_digest"] = benchmark["bundle_digest"]
    policy["task_inventory_digest"] = benchmark["task_inventory_digest"]
    policy["candidate_digest"] = _digest(
        contract,
        "pullwise:candidate-digest:v1",
        {
            field: policy[field]
            for field in (
                "package",
                "candidate_build_id",
                "control_plane_digest",
                "evaluation_runtime_digest",
                "benchmark_ref",
                "benchmark_digest",
                "threshold_table_digest",
                "profile_budget_digest",
                "canary_plan_digest",
            )
        },
    )
    policy = contract.seal_document("release-gate-policy/v1", policy)

    sample_set.pop("sample_set_digest", None)
    for field in (
        "package",
        "candidate_build_id",
        "candidate_digest",
        "release_mode",
        "stable_package",
        "stable_candidate_digest",
        "stable_control_plane_digest",
        "benchmark_version",
        "task_inventory_digest",
        "oracle_rubric_digest",
        "environment_image_digest",
        "control_plane_digest",
        "evaluation_runtime_digest",
        "statistical_implementation_version",
        "organization_id",
    ):
        sample_set[field] = policy[field]
    sample_set["benchmark_ref"] = _content_ref(
        contract, sample_set["benchmark_ref"], benchmark
    )
    sample_set["benchmark_digest"] = benchmark["bundle_digest"]
    sample_set["policy_ref"] = _content_ref(
        contract, sample_set["policy_ref"], policy
    )
    sample_set["policy_digest"] = policy["policy_digest"]
    sample_set = contract.seal_document(
        "release-gate-sample-set/v1", sample_set
    )
    return benchmark, policy, sample_set


def bound_minimal_documents(contract):
    return rebind_minimal_documents(
        contract,
        contract.fixture(
            "release_gate_sample_set_golden_bootstrap_included"
        )["document"],
    )


def bound_minimal_report(contract):
    benchmark, policy, sample_set = bound_minimal_documents(contract)
    report = deepcopy(
        contract.fixture("release_gate_report_golden_bootstrap_pass")[
            "document"
        ]
    )
    report.pop("report_digest")
    for field in (
        "package",
        "candidate_build_id",
        "candidate_digest",
        "release_mode",
        "stable_package",
        "stable_candidate_digest",
        "stable_control_plane_digest",
        "benchmark_digest",
        "benchmark_version",
        "task_inventory_digest",
        "oracle_rubric_digest",
        "environment_image_digest",
        "control_plane_digest",
        "evaluation_runtime_digest",
        "statistical_implementation_version",
        "threshold_table_digest",
        "profile_budget_digest",
        "canary_plan_digest",
        "organization_id",
    ):
        report[field] = policy[field]
    report["benchmark_ref"] = _content_ref(
        contract, report["benchmark_ref"], benchmark
    )
    report["policy_ref"] = _content_ref(
        contract, report["policy_ref"], policy
    )
    report["policy_digest"] = policy["policy_digest"]
    report["sample_set_ref"] = _content_ref(
        contract, report["sample_set_ref"], sample_set
    )
    report["sample_set_digest"] = sample_set["sample_set_digest"]
    report.update(
        contract.derive_release_gate_evaluation(
            benchmark, policy, sample_set
        )
    )
    report["completed_at"] = sample_set["completed_at"]
    report["signer_role"] = sample_set["producer_role"]
    report["signer_id"] = sample_set["producer_id"]
    report = contract.seal_document("release-gate-report/v1", report)
    return benchmark, policy, sample_set, report


__all__ = [
    "bound_minimal_documents",
    "bound_minimal_report",
    "rebind_minimal_documents",
]
