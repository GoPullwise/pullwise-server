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
    allowed_profiles = {
        item["profile_id"] for item in checked_policy["profile_budgets"]
    }
    _release_require(
        all(
            item["profile_id"] in allowed_profiles
            for item in checked_sample_set["samples"]
        ),
        "RELEASE_SAMPLE_PROFILE_INVALID",
        "$.samples",
    )
    completed_at = _timestamp_millis(checked_sample_set["completed_at"])
    _release_require(
        completed_at is not None
        and max(
            _timestamp_millis(checked_benchmark["issued_at"]),
            _timestamp_millis(checked_policy["issued_at"]),
        )
        <= completed_at
        <= min(
            _timestamp_millis(checked_benchmark["expires_at"]),
            _timestamp_millis(checked_policy["expires_at"]),
        ),
        "RELEASE_SAMPLE_TIME_INVALID",
        "$.completed_at",
    )
    _release_require(
        len(
            {
                checked_benchmark["signer_id"],
                checked_policy["signer_id"],
                checked_sample_set["producer_id"],
            }
        )
        == 3,
        "RELEASE_SAMPLE_PRODUCER_INVALID",
        "$.producer_id",
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
    reasons = {
        code
        for item in checked_sample_set["samples"]
        for code in item["evidence_issue_codes"]
    }
    if not _release_inventory_complete(
        checked_sample_set["samples"], checked_benchmark, "CANDIDATE"
    ):
        reasons.add("SAMPLE_INSUFFICIENT")
    candidate_metrics = _release_cohort_metrics(
        checked_sample_set["samples"], "CANDIDATE"
    )
    stable_metrics = None
    if checked_policy["release_mode"] == "STABLE":
        candidate_keys = {
            (item["task_id"], item["seed"])
            for item in checked_sample_set["samples"]
            if item["cohort"] == "CANDIDATE"
        }
        stable_keys = {
            (item["task_id"], item["seed"])
            for item in checked_sample_set["samples"]
            if item["cohort"] == "STABLE"
        }
        if (
            candidate_keys != stable_keys
            or not _release_inventory_complete(
                checked_sample_set["samples"], checked_benchmark, "STABLE"
            )
        ):
            reasons.add("BASELINE_INCOMPARABLE")
        stable_metrics = _release_cohort_metrics(
            checked_sample_set["samples"], "STABLE"
        )
    metric_names = [
        item["gate_id"].removeprefix("absolute_")
        for item in checked_policy["absolute_gates"]
    ]
    if (
        not candidate_samples
        or any(candidate_metrics[name] is None for name in metric_names)
        or any(
            budget["profile_id"] not in candidate_metrics["profile_maxima"]
            for budget in checked_policy["profile_budgets"]
        )
    ):
        reasons.add("ZERO_DENOMINATOR")
    absolute_results = []
    for gate, metric_name in zip(
        checked_policy["absolute_gates"], metric_names
    ):
        observed = None if reasons else candidate_metrics[metric_name]
        status = (
            "INDETERMINATE"
            if observed is None
            else "PASS"
            if _release_compare(
                gate["comparator"], observed, gate["threshold"]
            )
            else "FAIL"
        )
        absolute_results.append(
            {
                "gate_id": gate["gate_id"],
                "comparator": gate["comparator"],
                "threshold": gate["threshold"],
                "observed_value": observed,
                "status": status,
            }
        )
    relative_results = []
    for gate in checked_policy["relative_gates"]:
        if gate["applicability"] == "NOT_APPLICABLE":
            observed_regression = None
            relative_status = "NOT_APPLICABLE"
        else:
            observed_regression = (
                None
                if reasons or stable_metrics is None
                else _release_relative_regression_bps(
                    gate["gate_id"], candidate_metrics, stable_metrics
                )
            )
            if observed_regression is None:
                reasons.add("BASELINE_INCOMPARABLE")
                relative_status = "INDETERMINATE"
            else:
                relative_status = (
                    "PASS"
                    if observed_regression <= gate["max_regression_bps"]
                    else "FAIL"
                )
        relative_results.append(
            {
                "gate_id": gate["gate_id"],
                "applicability": gate["applicability"],
                "max_regression_bps": gate["max_regression_bps"],
                "observed_regression_bps": observed_regression,
                "status": relative_status,
            }
        )
    profile_results = []
    for budget in checked_policy["profile_budgets"]:
        measurements = candidate_metrics["profile_maxima"].get(
            budget["profile_id"]
        )
        if reasons or measurements is None:
            profile_results.append(
                {
                    "profile_id": budget["profile_id"],
                    "wall_ms": None,
                    "token_count": None,
                    "cost_microusd": None,
                    "status": "INDETERMINATE",
                }
            )
            continue
        passed = (
            measurements["wall_ms"] <= budget["wall_ms"]
            and measurements["token_count"] <= budget["token_limit"]
            and measurements["cost_microusd"] <= budget["cost_microusd"]
        )
        profile_results.append(
            {
                "profile_id": budget["profile_id"],
                **measurements,
                "status": "PASS" if passed else "FAIL",
            }
        )
    statuses = [
        *(item["status"] for item in absolute_results),
        *(
            item["status"]
            for item in relative_results
            if item["status"] != "NOT_APPLICABLE"
        ),
        *(item["status"] for item in profile_results),
    ]
    verdict = (
        "FAIL"
        if "FAIL" in statuses
        else "INDETERMINATE"
        if "INDETERMINATE" in statuses
        else "PASS"
    )
    return {
        "indeterminate_reason_codes": sorted(reasons),
        "raw_sample_count": len(candidate_samples),
        "valid_sample_count": len(candidate_samples) - len(excluded),
        "excluded_sample_count": len(excluded),
        "excluded_reason_counts": [
            {"reason_code": reason, "count": reason_counts[reason]}
            for reason in sorted(reason_counts)
        ],
        "absolute_results": absolute_results,
        "relative_results": relative_results,
        "profile_results": profile_results,
        "verdict": verdict,
        "exit_code": {"PASS": 0, "FAIL": 1, "INDETERMINATE": 2}[verdict],
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


_RELEASE_EVALUATION_FIELDS = (
    "indeterminate_reason_codes",
    "raw_sample_count",
    "valid_sample_count",
    "excluded_sample_count",
    "excluded_reason_counts",
    "absolute_results",
    "relative_results",
    "profile_results",
    "verdict",
    "exit_code",
)


def verify_release_gate_report_context(
    report: object,
    benchmark_bundle: object,
    policy: object,
    sample_set: object,
) -> dict[str, object]:
    expected_evaluation = derive_release_gate_evaluation(
        benchmark_bundle, policy, sample_set
    )
    checked_report = verify_document_digest("release-gate-report/v1", report)
    checked_benchmark = verify_document_digest(
        "benchmark-bundle/v1", benchmark_bundle
    )
    checked_policy = verify_document_digest("release-gate-policy/v1", policy)
    checked_sample_set = verify_document_digest(
        "release-gate-sample-set/v1", sample_set
    )
    _release_require(
        checked_report["organization_id"]
        == checked_policy["organization_id"]
        == checked_benchmark["organization_id"]
        == checked_sample_set["organization_id"],
        "RELEASE_REPORT_ORGANIZATION_MISMATCH",
        "$.organization_id",
    )
    for field, schema_id, document in (
        ("benchmark_ref", "benchmark-bundle/v1", checked_benchmark),
        ("policy_ref", "release-gate-policy/v1", checked_policy),
        ("sample_set_ref", "release-gate-sample-set/v1", checked_sample_set),
    ):
        _release_require_ref(
            checked_report[field],
            schema_id,
            document,
            "RELEASE_REPORT_REF_INVALID",
            f"$.{field}",
        )
    _release_require_bindings(
        checked_report,
        checked_policy,
        _RELEASE_REPORT_BINDING_FIELDS,
        "RELEASE_REPORT_BINDING_INVALID",
    )
    _release_require_bindings(
        checked_report,
        checked_sample_set,
        (
            "package", "candidate_build_id", "candidate_digest",
            "release_mode", "stable_package", "stable_candidate_digest",
            "stable_control_plane_digest", "benchmark_ref",
            "benchmark_digest", "policy_ref", "policy_digest",
            *_RELEASE_POLICY_BENCHMARK_FIELDS, "organization_id",
        ),
        "RELEASE_REPORT_BINDING_INVALID",
    )
    _release_require(
        checked_report["sample_set_digest"]
        == checked_sample_set["sample_set_digest"],
        "RELEASE_REPORT_BINDING_INVALID",
        "$.sample_set_digest",
    )
    _release_require(
        checked_report["completed_at"] == checked_sample_set["completed_at"]
        and checked_report["signer_role"]
        == checked_sample_set["producer_role"]
        and checked_report["signer_id"] == checked_sample_set["producer_id"],
        "RELEASE_REPORT_PRODUCER_BINDING_INVALID",
        "$.completed_at",
    )
    actual_evaluation = {
        field: checked_report[field] for field in _RELEASE_EVALUATION_FIELDS
    }
    _release_require_equal(
        actual_evaluation,
        expected_evaluation,
        "RELEASE_REPORT_EVALUATION_INVALID",
        "$.absolute_results",
    )
    return checked_report


def evaluate_release_gate(
    benchmark_bundle: object,
    policy: object,
    sample_set: object,
    report: object,
) -> dict[str, object]:
    checked = verify_release_gate_report_context(
        report, benchmark_bundle, policy, sample_set
    )
    return {
        "verdict": checked["verdict"],
        "exit_code": checked["exit_code"],
    }
'''


__all__ = ["PYTHON_RELEASE_GATE_EVALUATOR"]
