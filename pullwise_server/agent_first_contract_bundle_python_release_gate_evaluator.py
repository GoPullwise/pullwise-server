"""Deterministic Python release-gate evaluator semantics."""

from __future__ import annotations


PYTHON_RELEASE_GATE_EVALUATOR = r'''
def derive_release_gate_evaluation(
    benchmark_bundle: object,
    policy: object,
    sample_set: object,
) -> dict[str, object]:
    checked_benchmark = verify_document_digest(
        "benchmark-bundle/v1", benchmark_bundle
    )
    checked_policy = verify_document_digest("release-gate-policy/v1", policy)
    checked_sample_set = verify_document_digest(
        "release-gate-sample-set/v1", sample_set
    )
    _release_require_equal(
        checked_policy["organization_id"],
        checked_benchmark["organization_id"],
        "RELEASE_POLICY_ORGANIZATION_MISMATCH",
        "$.organization_id",
    )
    _verify_release_gate_policy_binding(checked_policy, checked_benchmark)
    _release_require_ref(
        checked_sample_set["benchmark_ref"],
        "benchmark-bundle/v1",
        checked_benchmark,
        "RELEASE_SAMPLE_REF_INVALID",
        "$.benchmark_ref",
    )
    _release_require_ref(
        checked_sample_set["policy_ref"],
        "release-gate-policy/v1",
        checked_policy,
        "RELEASE_SAMPLE_REF_INVALID",
        "$.policy_ref",
    )
    _release_require_bindings(
        checked_sample_set,
        checked_policy,
        (
            "package", "candidate_build_id", "candidate_digest",
            "release_mode", "stable_package", "stable_candidate_digest",
            "stable_control_plane_digest", "benchmark_ref",
            "benchmark_digest", *_RELEASE_POLICY_BENCHMARK_FIELDS,
            "organization_id",
        ),
        "RELEASE_SAMPLE_POLICY_BINDING_INVALID",
    )
    _release_require(
        checked_sample_set["policy_digest"] == checked_policy["policy_digest"],
        "RELEASE_SAMPLE_POLICY_BINDING_INVALID",
        "$.policy_digest",
    )
    _release_require_bindings(
        checked_sample_set,
        checked_benchmark,
        ("package",) + _RELEASE_POLICY_BENCHMARK_FIELDS,
        "RELEASE_SAMPLE_BENCHMARK_BINDING_INVALID",
    )
    _release_require(
        checked_sample_set["benchmark_digest"]
        == checked_benchmark["bundle_digest"],
        "RELEASE_SAMPLE_BENCHMARK_BINDING_INVALID",
        "$.benchmark_digest",
    )
    candidate_samples = [
        item
        for item in checked_sample_set["samples"]
        if item["cohort"] == "CANDIDATE"
    ]
    excluded = [
        item
        for item in candidate_samples
        if item["disposition"] == "EXCLUDED"
    ]
    allowed_reasons = set(checked_policy["infrastructure_reason_codes"])
    _release_require(
        all(
            item["infrastructure_reason_code"] in allowed_reasons
            for item in excluded
        ),
        "RELEASE_SAMPLE_EXCLUSION_REASON_INVALID",
        "$.samples",
    )
    reason_counts: dict[str, int] = {}
    for item in excluded:
        reason = item["infrastructure_reason_code"]
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "raw_sample_count": len(candidate_samples),
        "valid_sample_count": len(candidate_samples) - len(excluded),
        "excluded_sample_count": len(excluded),
        "excluded_reason_counts": [
            {"reason_code": reason, "count": reason_counts[reason]}
            for reason in sorted(reason_counts)
        ],
    }


def _release_compare(comparator: str, observed: int, threshold: int) -> bool:
    return {
        "EQ": observed == threshold,
        "GTE": observed >= threshold,
        "LT": observed < threshold,
        "LTE": observed <= threshold,
    }[comparator]


def _release_validate_indeterminate_shape(value: dict[str, object]) -> None:
    reasons = value["indeterminate_reason_codes"]
    _release_require(
        _sorted_unique(reasons),
        "RELEASE_EVALUATOR_INDETERMINATE_INVALID",
        "$.indeterminate_reason_codes",
    )
    results = [
        *value["absolute_results"],
        *value["relative_results"],
        *value["profile_results"],
    ]
    _release_require(
        bool(reasons) == any(item["status"] == "INDETERMINATE" for item in results),
        "RELEASE_EVALUATOR_INDETERMINATE_INVALID",
        "$.indeterminate_reason_codes",
    )
    for index, item in enumerate(value["absolute_results"]):
        _release_require(
            (item["observed_value"] is None)
            == (item["status"] == "INDETERMINATE"),
            "RELEASE_EVALUATOR_INDETERMINATE_INVALID",
            f"$.absolute_results[{index}]",
        )
    for index, item in enumerate(value["relative_results"]):
        missing = item["observed_regression_bps"] is None
        expected_missing = item["status"] in {
            "INDETERMINATE", "NOT_APPLICABLE"
        }
        _release_require(
            missing == expected_missing,
            "RELEASE_EVALUATOR_INDETERMINATE_INVALID",
            f"$.relative_results[{index}]",
        )
    for index, item in enumerate(value["profile_results"]):
        measurements = [
            item["wall_ms"], item["token_count"], item["cost_microusd"]
        ]
        expected_missing = item["status"] == "INDETERMINATE"
        _release_require(
            (all(value is None for value in measurements) if expected_missing
             else all(value is not None for value in measurements)),
            "RELEASE_EVALUATOR_INDETERMINATE_INVALID",
            f"$.profile_results[{index}]",
        )


def _release_validate_absolute_results(value: dict[str, object]) -> None:
    for index, item in enumerate(value["absolute_results"]):
        if item["status"] == "INDETERMINATE":
            continue
        expected = (
            "PASS"
            if _release_compare(
                item["comparator"], item["observed_value"], item["threshold"]
            )
            else "FAIL"
        )
        _release_require(
            item["status"] == expected,
            "RELEASE_EVALUATOR_STATUS_INVALID",
            f"$.absolute_results[{index}].status",
        )


def _release_validate_relative_results(value: dict[str, object]) -> None:
    for index, item in enumerate(value["relative_results"]):
        if item["status"] in {"INDETERMINATE", "NOT_APPLICABLE"}:
            continue
        expected = (
            "PASS"
            if item["observed_regression_bps"] <= item["max_regression_bps"]
            else "FAIL"
        )
        _release_require(
            item["status"] == expected,
            "RELEASE_EVALUATOR_STATUS_INVALID",
            f"$.relative_results[{index}].status",
        )


def _release_validate_profile_results(
    report: dict[str, object], policy: dict[str, object]
) -> None:
    for index, (result, budget) in enumerate(
        zip(report["profile_results"], policy["profile_budgets"])
    ):
        if result["status"] == "INDETERMINATE":
            continue
        passed = (
            result["wall_ms"] <= budget["wall_ms"]
            and result["token_count"] <= budget["token_limit"]
            and result["cost_microusd"] <= budget["cost_microusd"]
        )
        _release_require(
            result["status"] == ("PASS" if passed else "FAIL"),
            "RELEASE_EVALUATOR_STATUS_INVALID",
            f"$.profile_results[{index}].status",
        )


def _release_validate_sample_inventory(
    report: dict[str, object], benchmark: dict[str, object]
) -> None:
    expected = (
        benchmark["known_gold_task_count"]
        + sum(item["task_count"] for item in benchmark["unknown_families"])
    ) * benchmark["repeats_per_task"]
    reasons = set(report["indeterminate_reason_codes"])
    valid = (
        ("SAMPLE_INSUFFICIENT" in reasons)
        == (report["raw_sample_count"] != expected)
        and ("ZERO_DENOMINATOR" in reasons)
        == (report["valid_sample_count"] == 0)
    )
    _release_require(
        valid,
        "RELEASE_EVALUATOR_SAMPLE_INVALID",
        "$.indeterminate_reason_codes",
    )


def evaluate_release_gate(
    benchmark_bundle: object,
    policy: object,
    report: object,
) -> dict[str, object]:
    checked = verify_release_gate_report_context(
        report, benchmark_bundle, policy
    )
    return {
        "verdict": checked["verdict"],
        "exit_code": checked["exit_code"],
    }
'''


__all__ = ["PYTHON_RELEASE_GATE_EVALUATOR"]
