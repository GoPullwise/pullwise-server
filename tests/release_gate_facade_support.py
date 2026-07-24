from __future__ import annotations

from copy import deepcopy
import hashlib

from pullwise_server.agent_first_contract_bundle_source import canonical_bytes


def stable_release_gate_documents(harness) -> tuple[dict[str, object], ...]:
    benchmark = harness.document("benchmark_bundle_golden_current")
    policy = harness.document("release_gate_policy_golden_bootstrap")
    policy["release_mode"] = "STABLE"
    policy["stable_package"] = deepcopy(policy["package"])
    policy["stable_candidate_digest"] = "9" * 64
    policy["stable_control_plane_digest"] = "8" * 64
    for gate in policy["relative_gates"]:
        gate["applicability"] = "REQUIRED"

    def digest(domain: str, value: object) -> str:
        return hashlib.sha256(
            domain.encode("ascii") + b"\0" + canonical_bytes(value)
        ).hexdigest()

    policy["threshold_table_digest"] = digest(
        "pullwise:release-threshold-table:v1",
        {
            key: policy[key]
            for key in (
                "absolute_gates",
                "relative_gates",
                "infrastructure_reason_codes",
            )
        },
    )
    policy["candidate_digest"] = digest(
        "pullwise:candidate-digest:v1",
        {
            key: policy[key]
            for key in (
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
    policy = harness.reseal(
        "release-gate-policy/v1", "policy_digest", policy
    )

    report = harness.document("release_gate_report_golden_bootstrap_pass")
    for key in (
        "package", "candidate_build_id", "candidate_digest", "release_mode",
        "stable_package", "stable_candidate_digest",
        "stable_control_plane_digest", "benchmark_digest", "benchmark_version",
        "task_inventory_digest", "oracle_rubric_digest",
        "environment_image_digest", "control_plane_digest",
        "evaluation_runtime_digest", "statistical_implementation_version",
        "threshold_table_digest", "profile_budget_digest", "canary_plan_digest",
    ):
        report[key] = deepcopy(policy[key])
    report["policy_digest"] = policy["policy_digest"]
    report["policy_ref"] = harness.content_ref(report["policy_ref"], policy)
    for result in report["relative_results"]:
        result["applicability"] = "REQUIRED"
        result["observed_regression_bps"] = 0
        result["status"] = "PASS"
    report = harness.reseal(
        "release-gate-report/v1", "report_digest", report
    )
    return benchmark, policy, report
