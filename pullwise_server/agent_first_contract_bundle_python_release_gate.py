"""Generated Python D22 release-gate rules and contextual verification."""

from __future__ import annotations


PYTHON_RELEASE_GATE = r'''
_RELEASE_INVALID_CODE = "CONTRACT_DOCUMENT_INVALID"
_RELEASE_CANARY_STAGE_IDS = ("CAPACITY_5", "CAPACITY_25", "FULL_CAPACITY")
_RELEASE_ATTESTATION_MAX_WINDOW_MS = 7 * 24 * 60 * 60 * 1000

_RELEASE_POLICY_BENCHMARK_FIELDS = (
    "benchmark_version", "task_inventory_digest", "oracle_rubric_digest",
    "environment_image_digest", "control_plane_digest",
    "evaluation_runtime_digest", "statistical_implementation_version",
)

_RELEASE_REPORT_POLICY_FIELDS = (
    "package", "candidate_build_id", "candidate_digest", "release_mode",
    "stable_package", "stable_candidate_digest", "stable_control_plane_digest",
    "benchmark_digest", "benchmark_version", "task_inventory_digest",
    "oracle_rubric_digest", "environment_image_digest", "control_plane_digest",
    "evaluation_runtime_digest", "statistical_implementation_version",
    "threshold_table_digest", "profile_budget_digest", "canary_plan_digest",
)
_RELEASE_REPORT_BINDING_FIELDS = (
    *_RELEASE_REPORT_POLICY_FIELDS[:7], "benchmark_ref",
    *_RELEASE_REPORT_POLICY_FIELDS[7:],
)

_RELEASE_ATTESTATION_IDENTITY_FIELDS = (
    "package", "candidate_build_id", "candidate_digest", "release_mode",
    "stable_package", "stable_candidate_digest", "stable_control_plane_digest",
)


def _release_require(
    condition: bool, detail: str, path: str = "$"
) -> None:
    _require(condition, detail, path, _RELEASE_INVALID_CODE)


def _release_digest(domain: str, value: object) -> str:
    return hashlib.sha256(
        domain.encode("utf-8") + b"\0" + canonical_document_bytes(value)
    ).hexdigest()


def _release_require_ref(
    ref: dict[str, object],
    schema_id: str,
    document: dict[str, object],
    detail: str,
    path: str,
) -> None:
    _release_require(
        _seo_ref_matches_document(ref, schema_id, document),
        detail,
        path,
    )


def _release_require_equal(
    actual: object, expected: object, detail: str, path: str
) -> None:
    _release_require(_json_equal(actual, expected), detail, path)


def _rule_benchmark_bundle(value: dict[str, object]) -> None:
    _release_require(
        _sorted_unique(value["seeds"]),
        "RELEASE_BENCHMARK_ORDER_INVALID",
        "$.seeds",
    )
    _release_require(
        _ordered_unique(value["unknown_families"], lambda item: item["family_id"]),
        "RELEASE_BENCHMARK_ORDER_INVALID",
        "$.unknown_families",
    )
    _release_require(
        _sorted_unique(value["core_cluster_ids"]),
        "RELEASE_BENCHMARK_ORDER_INVALID",
        "$.core_cluster_ids",
    )
    _release_require(
        _ordered_unique(value["cluster_coverage"], lambda item: item["cluster_id"]),
        "RELEASE_BENCHMARK_ORDER_INVALID",
        "$.cluster_coverage",
    )
    _release_require(
        [item["cluster_id"] for item in value["cluster_coverage"]]
        == value["core_cluster_ids"],
        "RELEASE_BENCHMARK_COVERAGE_INVALID",
        "$.cluster_coverage",
    )
    issued_at = _timestamp_millis(value["issued_at"])
    expires_at = _timestamp_millis(value["expires_at"])
    _release_require(
        issued_at is not None,
        "RELEASE_BENCHMARK_TIME_INVALID",
        "$.issued_at",
    )
    _release_require(
        expires_at is not None and expires_at > issued_at,
        "RELEASE_BENCHMARK_TIME_INVALID",
        "$.expires_at",
    )


def _rule_release_gate_policy(value: dict[str, object]) -> None:
    for field, identity in (
        ("absolute_gates", "gate_id"),
        ("relative_gates", "gate_id"),
        ("profile_budgets", "profile_id"),
    ):
        _release_require(
            _ordered_unique(value[field], lambda item, key=identity: item[key]),
            "RELEASE_POLICY_ORDER_INVALID",
            f"$.{field}",
        )
    _release_require(
        _sorted_unique(value["infrastructure_reason_codes"]),
        "RELEASE_POLICY_ORDER_INVALID",
        "$.infrastructure_reason_codes",
    )
    _release_require(
        tuple(item["stage_id"] for item in value["canary_stages"])
        == _RELEASE_CANARY_STAGE_IDS,
        "RELEASE_POLICY_CANARY_INVALID",
        "$.canary_stages",
    )
    stable_fields = (
        value["stable_package"],
        value["stable_candidate_digest"],
        value["stable_control_plane_digest"],
    )
    expected_applicability = (
        "NOT_APPLICABLE" if value["release_mode"] == "BOOTSTRAP" else "REQUIRED"
    )
    stable_shape_valid = (
        all(item is None for item in stable_fields)
        if value["release_mode"] == "BOOTSTRAP"
        else all(item is not None for item in stable_fields)
    )
    _release_require(
        stable_shape_valid,
        "RELEASE_POLICY_MODE_INVALID",
        "$.stable_package",
    )
    _release_require(
        all(
            item["applicability"] == expected_applicability
            for item in value["relative_gates"]
        ),
        "RELEASE_POLICY_MODE_INVALID",
        "$.relative_gates",
    )
    issued_at = _timestamp_millis(value["issued_at"])
    expires_at = _timestamp_millis(value["expires_at"])
    _release_require(
        issued_at is not None,
        "RELEASE_POLICY_TIME_INVALID",
        "$.issued_at",
    )
    _release_require(
        expires_at is not None and expires_at > issued_at,
        "RELEASE_POLICY_TIME_INVALID",
        "$.expires_at",
    )
    threshold_projection = {
        "absolute_gates": value["absolute_gates"],
        "relative_gates": value["relative_gates"],
        "infrastructure_reason_codes": value["infrastructure_reason_codes"],
    }
    _release_require(
        value["threshold_table_digest"]
        == _release_digest(
            "pullwise:release-threshold-table:v1",
            threshold_projection,
        ),
        "RELEASE_POLICY_THRESHOLD_DIGEST_INVALID",
        "$.threshold_table_digest",
    )
    _release_require(
        value["profile_budget_digest"]
        == _release_digest(
            "pullwise:release-profile-budgets:v1",
            value["profile_budgets"],
        ),
        "RELEASE_POLICY_PROFILE_DIGEST_INVALID",
        "$.profile_budget_digest",
    )
    canary_projection = {
        field: value[field]
        for field in (
            "canary_stages",
            "canary_platform_failure_rate_max_bps",
            "canary_relative_platform_failure_increase_max_bps",
            "canary_p95_wall_time_increase_max_bps",
            "canary_p95_cost_increase_max_bps",
        )
    }
    _release_require(
        value["canary_plan_digest"]
        == _release_digest(
            "pullwise:release-canary-plan:v1",
            canary_projection,
        ),
        "RELEASE_POLICY_CANARY_DIGEST_INVALID",
        "$.canary_plan_digest",
    )
    candidate_projection = {
        field: value[field]
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
    }
    _release_require(
        value["candidate_digest"]
        == _release_digest(
            "pullwise:candidate-digest:v1",
            candidate_projection,
        ),
        "RELEASE_POLICY_CANDIDATE_DIGEST_INVALID",
        "$.candidate_digest",
    )


def _rule_release_gate_report(value: dict[str, object]) -> None:
    _release_require(
        value["raw_sample_count"]
        == value["valid_sample_count"] + value["excluded_sample_count"],
        "RELEASE_REPORT_SAMPLE_INVALID",
        "$.raw_sample_count",
    )
    _release_require(
        value["excluded_sample_count"]
        == sum(item["count"] for item in value["excluded_reason_counts"]),
        "RELEASE_REPORT_SAMPLE_INVALID",
        "$.excluded_sample_count",
    )
    _release_require(
        _ordered_unique(
            value["excluded_reason_counts"],
            lambda item: item["reason_code"],
        ),
        "RELEASE_REPORT_ORDER_INVALID",
        "$.excluded_reason_counts",
    )
    for field, identity in (
        ("absolute_results", "gate_id"),
        ("relative_results", "gate_id"),
        ("profile_results", "profile_id"),
    ):
        _release_require(
            _ordered_unique(value[field], lambda item, key=identity: item[key]),
            "RELEASE_REPORT_ORDER_INVALID",
            f"$.{field}",
        )
    if value["release_mode"] == "BOOTSTRAP":
        relative_shape_valid = all(
            item["applicability"] == "NOT_APPLICABLE"
            and item["observed_regression_bps"] is None
            and item["status"] == "NOT_APPLICABLE"
            for item in value["relative_results"]
        )
    else:
        relative_shape_valid = all(
            item["applicability"] == "REQUIRED"
            and item["status"] != "NOT_APPLICABLE"
            and (
                item["status"] == "INDETERMINATE"
                or item["observed_regression_bps"] is not None
            )
            for item in value["relative_results"]
        )
    _release_require(
        relative_shape_valid,
        "RELEASE_REPORT_MODE_INVALID",
        "$.relative_results",
    )
    statuses = [
        *(item["status"] for item in value["absolute_results"]),
        *(
            item["status"]
            for item in value["relative_results"]
            if item["status"] != "NOT_APPLICABLE"
        ),
        *(item["status"] for item in value["profile_results"]),
    ]
    expected_verdict = (
        "FAIL"
        if "FAIL" in statuses
        else "INDETERMINATE"
        if "INDETERMINATE" in statuses
        else "PASS"
    )
    _release_require(
        value["verdict"] == expected_verdict,
        "RELEASE_REPORT_VERDICT_INVALID",
        "$.verdict",
    )
    _release_require(
        value["exit_code"] == {"PASS": 0, "FAIL": 1, "INDETERMINATE": 2}[
            value["verdict"]
        ],
        "RELEASE_REPORT_VERDICT_INVALID",
        "$.exit_code",
    )


def _rule_release_gate_attestation(value: dict[str, object]) -> None:
    _release_require(
        value["attested_verdict"] == "PASS",
        "RELEASE_ATTESTATION_VERDICT_INVALID",
        "$.attested_verdict",
    )
    _release_require(
        value["attested_exit_code"] == 0,
        "RELEASE_ATTESTATION_VERDICT_INVALID",
        "$.attested_exit_code",
    )
    issued_at = _timestamp_millis(value["issued_at"])
    expires_at = _timestamp_millis(value["expires_at"])
    _release_require(
        issued_at is not None,
        "RELEASE_ATTESTATION_WINDOW_INVALID",
        "$.issued_at",
    )
    _release_require(
        expires_at is not None
        and 0 < expires_at - issued_at <= _RELEASE_ATTESTATION_MAX_WINDOW_MS,
        "RELEASE_ATTESTATION_WINDOW_INVALID",
        "$.expires_at",
    )


def _verify_release_gate_policy_binding(
    policy: dict[str, object],
    benchmark_bundle: dict[str, object],
) -> None:
    _release_require_ref(
        policy["benchmark_ref"],
        "benchmark-bundle/v1",
        benchmark_bundle,
        "RELEASE_POLICY_BENCHMARK_REF_INVALID",
        "$.benchmark_ref",
    )
    _release_require_equal(
        policy["package"],
        benchmark_bundle["package"],
        "RELEASE_POLICY_BENCHMARK_BINDING_INVALID",
        "$.package",
    )
    _release_require(
        policy["benchmark_digest"] == benchmark_bundle["bundle_digest"],
        "RELEASE_POLICY_BENCHMARK_BINDING_INVALID",
        "$.benchmark_digest",
    )
    for field in _RELEASE_POLICY_BENCHMARK_FIELDS:
        _release_require_equal(
            policy[field],
            benchmark_bundle[field],
            "RELEASE_POLICY_BENCHMARK_BINDING_INVALID",
            f"$.{field}",
        )


def verify_release_gate_policy_context(
    policy: object,
    benchmark_bundle: object,
) -> dict[str, object]:
    checked_policy = verify_document_digest("release-gate-policy/v1", policy)
    checked_benchmark = verify_document_digest(
        "benchmark-bundle/v1",
        benchmark_bundle,
    )
    _verify_release_gate_policy_binding(checked_policy, checked_benchmark)
    return checked_policy


def _release_require_bindings(
    left: dict[str, object],
    right: dict[str, object],
    fields: tuple[str, ...],
    detail: str,
    prefix: str = "$",
) -> None:
    for field in fields:
        _release_require_equal(
            left[field], right[field], detail, f"{prefix}.{field}"
        )


def verify_release_gate_report_context(
    report: object,
    benchmark_bundle: object,
    policy: object,
) -> dict[str, object]:
    checked_report = verify_document_digest("release-gate-report/v1", report)
    checked_benchmark = verify_document_digest(
        "benchmark-bundle/v1",
        benchmark_bundle,
    )
    checked_policy = verify_document_digest("release-gate-policy/v1", policy)
    _release_require_ref(
        checked_report["benchmark_ref"],
        "benchmark-bundle/v1",
        checked_benchmark,
        "RELEASE_REPORT_REF_INVALID",
        "$.benchmark_ref",
    )
    _release_require_ref(
        checked_report["policy_ref"],
        "release-gate-policy/v1",
        checked_policy,
        "RELEASE_REPORT_REF_INVALID",
        "$.policy_ref",
    )
    _release_require_bindings(
        checked_report,
        checked_policy,
        _RELEASE_REPORT_BINDING_FIELDS,
        "RELEASE_REPORT_BINDING_INVALID",
    )
    _release_require_bindings(
        checked_report,
        checked_benchmark,
        ("package",) + _RELEASE_POLICY_BENCHMARK_FIELDS,
        "RELEASE_REPORT_BINDING_INVALID",
    )
    _release_require(
        checked_report["benchmark_digest"] == checked_benchmark["bundle_digest"],
        "RELEASE_REPORT_BINDING_INVALID",
        "$.benchmark_digest",
    )
    _release_require(
        checked_report["policy_digest"] == checked_policy["policy_digest"],
        "RELEASE_REPORT_BINDING_INVALID",
        "$.policy_digest",
    )
    actual_absolute = [
        {field: item[field] for field in ("gate_id", "comparator", "threshold")}
        for item in checked_report["absolute_results"]
    ]
    expected_absolute = [
        {field: item[field] for field in ("gate_id", "comparator", "threshold")}
        for item in checked_policy["absolute_gates"]
    ]
    _release_require_equal(
        actual_absolute,
        expected_absolute,
        "RELEASE_REPORT_POLICY_TABLE_INVALID",
        "$.absolute_results",
    )
    actual_relative = [
        {
            field: item[field]
            for field in ("gate_id", "applicability", "max_regression_bps")
        }
        for item in checked_report["relative_results"]
    ]
    expected_relative = [
        {
            field: item[field]
            for field in ("gate_id", "applicability", "max_regression_bps")
        }
        for item in checked_policy["relative_gates"]
    ]
    _release_require_equal(
        actual_relative,
        expected_relative,
        "RELEASE_REPORT_POLICY_TABLE_INVALID",
        "$.relative_results",
    )
    _release_require_equal(
        [item["profile_id"] for item in checked_report["profile_results"]],
        [item["profile_id"] for item in checked_policy["profile_budgets"]],
        "RELEASE_REPORT_POLICY_TABLE_INVALID",
        "$.profile_results",
    )
    allowed_reason_codes = set(checked_policy["infrastructure_reason_codes"])
    _release_require(
        all(
            item["reason_code"] in allowed_reason_codes
            for item in checked_report["excluded_reason_counts"]
        ),
        "RELEASE_REPORT_POLICY_TABLE_INVALID",
        "$.excluded_reason_counts",
    )
    return checked_report


def verify_release_gate_attestation_context(
    attestation: object,
    policy: object,
    report: object,
) -> dict[str, object]:
    checked_attestation = verify_document_digest(
        "release-gate-attestation/v1",
        attestation,
    )
    checked_policy = verify_document_digest("release-gate-policy/v1", policy)
    checked_report = verify_document_digest("release-gate-report/v1", report)
    _release_require_ref(
        checked_attestation["policy_ref"],
        "release-gate-policy/v1",
        checked_policy,
        "RELEASE_ATTESTATION_REF_INVALID",
        "$.policy_ref",
    )
    _release_require_ref(
        checked_attestation["report_ref"],
        "release-gate-report/v1",
        checked_report,
        "RELEASE_ATTESTATION_REF_INVALID",
        "$.report_ref",
    )
    _release_require_ref(
        checked_report["policy_ref"],
        "release-gate-policy/v1",
        checked_policy,
        "RELEASE_ATTESTATION_REF_INVALID",
        "$.report.policy_ref",
    )
    _release_require(
        checked_report["policy_digest"] == checked_policy["policy_digest"],
        "RELEASE_ATTESTATION_BINDING_INVALID",
        "$.report.policy_digest",
    )
    _release_require_bindings(
        checked_report,
        checked_policy,
        _RELEASE_REPORT_POLICY_FIELDS,
        "RELEASE_ATTESTATION_BINDING_INVALID",
        "$.report",
    )
    _release_require_bindings(
        checked_attestation,
        checked_policy,
        _RELEASE_ATTESTATION_IDENTITY_FIELDS + ("policy_id", "policy_digest"),
        "RELEASE_ATTESTATION_BINDING_INVALID",
    )
    _release_require_bindings(
        checked_attestation,
        checked_report,
        _RELEASE_ATTESTATION_IDENTITY_FIELDS
        + ("policy_digest", "report_id", "report_digest"),
        "RELEASE_ATTESTATION_BINDING_INVALID",
    )
    _release_require(
        checked_report["verdict"] == "PASS"
        and checked_report["exit_code"] == 0,
        "RELEASE_ATTESTATION_REPORT_NOT_PASS",
        "$.report.verdict",
    )
    _release_require(
        checked_attestation["attested_verdict"] == checked_report["verdict"]
        and checked_attestation["attested_exit_code"] == checked_report["exit_code"],
        "RELEASE_ATTESTATION_BINDING_INVALID",
        "$.attested_verdict",
    )
    return checked_attestation
'''


__all__ = ["PYTHON_RELEASE_GATE"]
