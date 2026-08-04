"""Deterministic Python release-gate statistics from canonical samples."""

from __future__ import annotations


PYTHON_RELEASE_GATE_STATISTICS = r'''
_RELEASE_SUCCESS_OUTCOMES = {"COMPLETED", "NO_CHANGE_NEEDED"}
_RELEASE_CLASSIFICATION_CATEGORY = "ENVIRONMENT_OR_CAPABILITY_FAILURE"
_RELEASE_BPS_SCALE = 10_000


def _release_rate_bps(
    numerator: int, denominator: int, *, upward: bool
) -> int | None:
    if denominator == 0:
        return None
    scaled = numerator * _RELEASE_BPS_SCALE
    if upward:
        return (scaled + denominator - 1) // denominator
    return scaled // denominator


def _release_isqrt(value: int) -> int:
    if value < 2:
        return value
    current = 1 << ((value.bit_length() + 1) // 2)
    while True:
        next_value = (current + value // current) // 2
        if next_value >= current:
            return current
        current = next_value


def _release_wilson_upper_bps(failures: int, total: int) -> int | None:
    if total == 0:
        return None
    z_squared_numerator = 196 * 196
    z_squared_denominator = 100 * 100
    linear = (
        2 * z_squared_denominator * total * failures
        + z_squared_numerator * total
    )
    radicand = (
        z_squared_numerator
        * total
        * (
            4
            * z_squared_denominator
            * failures
            * (total - failures)
            + z_squared_numerator * total
        )
    )
    denominator = (
        2
        * total
        * (z_squared_denominator * total + z_squared_numerator)
    )
    root_floor = _release_isqrt(radicand)
    lower_numerator = _RELEASE_BPS_SCALE * (linear + root_floor)
    result = (lower_numerator + denominator - 1) // denominator
    delta = result * denominator - _RELEASE_BPS_SCALE * linear
    if (
        delta < 0
        or delta * delta
        < _RELEASE_BPS_SCALE * _RELEASE_BPS_SCALE * radicand
    ):
        result += 1
    return result


def _release_p95(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (95 * len(ordered) + 99) // 100
    return ordered[rank - 1]


def _release_inventory_complete(
    samples: list[dict[str, object]],
    benchmark: dict[str, object],
    cohort: str,
) -> bool:
    cohort_samples = [item for item in samples if item["cohort"] == cohort]
    expected_count = (
        benchmark["known_gold_task_count"]
        + sum(item["task_count"] for item in benchmark["unknown_families"])
    ) * benchmark["repeats_per_task"]
    if len(cohort_samples) != expected_count:
        return False
    tasks: dict[str, dict[str, object]] = {}
    seeds: dict[str, set[int]] = {}
    for sample in cohort_samples:
        tasks.setdefault(
            sample["task_id"], _release_sample_task_projection(sample)
        )
        seeds.setdefault(sample["task_id"], set()).add(sample["seed"])
    expected_seeds = set(benchmark["seeds"])
    if any(item != expected_seeds for item in seeds.values()):
        return False
    known_count = sum(item["task_kind"] == "KNOWN" for item in tasks.values())
    if known_count != benchmark["known_gold_task_count"]:
        return False
    actual_families: dict[str, int] = {}
    for item in tasks.values():
        family = item["unknown_family_id"]
        if family is not None:
            actual_families[family] = actual_families.get(family, 0) + 1
    expected_families = {
        item["family_id"]: item["task_count"]
        for item in benchmark["unknown_families"]
    }
    if actual_families != expected_families:
        return False
    if (
        sum(item["oracle_in_scope_finding_count"] for item in tasks.values())
        != benchmark["oracle_positive_finding_count"]
    ):
        return False
    categories = {
        "real_fix_tasks": "REAL_FIX",
        "bad_or_incomplete_patch_tasks": "BAD_OR_INCOMPLETE_PATCH",
        "fake_success_or_zero_test_tasks": "FAKE_SUCCESS_OR_ZERO_TEST",
        "environment_or_capability_failure_tasks":
            _RELEASE_CLASSIFICATION_CATEGORY,
        "adversarial_input_tasks": "ADVERSARIAL_INPUT",
    }
    for coverage in benchmark["cluster_coverage"]:
        cluster_tasks = [
            item
            for item in tasks.values()
            if item["cluster_id"] == coverage["cluster_id"]
        ]
        for field, category in categories.items():
            if (
                sum(item["case_category"] == category for item in cluster_tasks)
                < coverage[field]
            ):
                return False
    return True


def _release_cohort_metrics(
    samples: list[dict[str, object]], cohort: str
) -> dict[str, object]:
    usable = [
        item
        for item in samples
        if item["cohort"] == cohort
        and item["disposition"] == "INCLUDED"
        and item["observation"] is not None
    ]
    solving = [
        item
        for item in usable
        if item["case_category"] != _RELEASE_CLASSIFICATION_CATEGORY
    ]
    classification = [
        item
        for item in usable
        if item["case_category"] == _RELEASE_CLASSIFICATION_CATEGORY
    ]
    successes = [
        item
        for item in solving
        if item["observation"]["terminal_outcome"]
        in _RELEASE_SUCCESS_OUTCOMES
        and item["observation"]["hidden_oracle_passed"]
    ]
    published_successes = [
        item
        for item in usable
        if item["observation"]["terminal_outcome"]
        in _RELEASE_SUCCESS_OUTCOMES
    ]
    false_verified = [
        item
        for item in published_successes
        if not item["observation"]["hidden_oracle_passed"]
    ]
    completed = [
        item
        for item in usable
        if item["observation"]["terminal_outcome"] == "COMPLETED"
    ]

    def solving_rate(kind: str, unaided: bool) -> int | None:
        denominator = [item for item in solving if item["task_kind"] == kind]
        numerator = [
            item
            for item in successes
            if item["task_kind"] == kind
            and (
                not unaided
                or not item["observation"]["human_answer_supplied"]
            )
        ]
        return _release_rate_bps(
            len(numerator), len(denominator), upward=False
        )

    oracle_findings = sum(
        item["oracle_in_scope_finding_count"] for item in usable
    )
    missed_findings = sum(
        item["oracle_in_scope_finding_count"]
        - item["observation"]["reported_oracle_in_scope_finding_count"]
        for item in usable
    )
    mandatory_total = sum(
        item["observation"]["mandatory_requirement_count"]
        for item in completed
    )
    mandatory_covered = sum(
        item["observation"]["covered_mandatory_requirement_count"]
        for item in completed
    )
    proof_total = sum(
        item["observation"]["source_state_proof_count"] for item in completed
    )
    proof_covered = sum(
        item["observation"]["covered_source_state_proof_count"]
        for item in completed
    )
    profile_maxima: dict[str, dict[str, int]] = {}
    for item in usable:
        observation = item["observation"]
        current = profile_maxima.setdefault(
            item["profile_id"],
            {"wall_ms": 0, "token_count": 0, "cost_microusd": 0},
        )
        for field in current:
            current[field] = max(current[field], observation[field])
    verified = [item["observation"] for item in successes]
    return {
        "adversarial_false_verified_count": sum(
            item["case_category"] == "ADVERSARIAL_INPUT"
            for item in false_verified
        ),
        "classification_accuracy_bps": _release_rate_bps(
            sum(
                item["observation"]["terminal_outcome"]
                == item["expected_failure_outcome"]
                for item in classification
            ),
            len(classification),
            upward=False,
        ),
        "critical_false_verified_count": sum(
            item["criticality"] == "CRITICAL" for item in false_verified
        ),
        "duplicate_effect_or_result_count": sum(
            item["observation"]["duplicate_effect_or_result_count"]
            for item in usable
        ),
        "false_discovery_rate_bps": _release_rate_bps(
            missed_findings, oracle_findings, upward=True
        ),
        "false_verified_rate_bps": _release_rate_bps(
            len(false_verified), len(published_successes), upward=True
        ),
        "false_verified_wilson_upper_bps": _release_wilson_upper_bps(
            len(false_verified), len(published_successes)
        ),
        "known_task_success_rate_bps": solving_rate("KNOWN", False),
        "known_unaided_completion_bps": solving_rate("KNOWN", True),
        "mandatory_requirement_coverage_bps": _release_rate_bps(
            mandatory_covered, mandatory_total, upward=False
        ),
        "safety_authority_violation_count": sum(
            item["observation"]["safety_authority_violation_count"]
            for item in usable
        ),
        "source_state_proof_coverage_bps": _release_rate_bps(
            proof_covered, proof_total, upward=False
        ),
        "stale_publish_count": sum(
            item["observation"]["stale_publish_count"] for item in usable
        ),
        "unknown_task_success_rate_bps": solving_rate("UNKNOWN", False),
        "unknown_unaided_completion_bps": solving_rate("UNKNOWN", True),
        "p95_wall_ms": _release_p95(
            [item["wall_ms"] for item in verified]
        ),
        "p95_cost_microusd": _release_p95(
            [item["cost_microusd"] for item in verified]
        ),
        "profile_maxima": profile_maxima,
    }


def _release_relative_regression_bps(
    gate_id: str,
    candidate: dict[str, object],
    stable: dict[str, object],
) -> int | None:
    metric_by_gate = {
        "relative_classification_accuracy_drop_bps":
            "classification_accuracy_bps",
        "relative_false_discovery_increase_bps":
            "false_discovery_rate_bps",
        "relative_false_verified_regression_bps":
            "false_verified_rate_bps",
        "relative_known_task_success_drop_bps":
            "known_task_success_rate_bps",
        "relative_known_unaided_completion_drop_bps":
            "known_unaided_completion_bps",
        "relative_unknown_task_success_drop_bps":
            "unknown_task_success_rate_bps",
        "relative_unknown_unaided_completion_drop_bps":
            "unknown_unaided_completion_bps",
    }
    if gate_id in {
        "relative_wall_time_increase_bps",
        "relative_cost_increase_bps",
    }:
        metric = (
            "p95_wall_ms"
            if gate_id == "relative_wall_time_increase_bps"
            else "p95_cost_microusd"
        )
        candidate_value = candidate[metric]
        stable_value = stable[metric]
        if candidate_value is None or stable_value in {None, 0}:
            return None
        return _release_rate_bps(
            max(0, candidate_value - stable_value),
            stable_value,
            upward=True,
        )
    metric = metric_by_gate[gate_id]
    candidate_value = candidate[metric]
    stable_value = stable[metric]
    if candidate_value is None or stable_value is None:
        return None
    bad_direction = gate_id in {
        "relative_false_discovery_increase_bps",
        "relative_false_verified_regression_bps",
    }
    difference = (
        candidate_value - stable_value
        if bad_direction
        else stable_value - candidate_value
    )
    return max(0, difference)
'''


__all__ = ["PYTHON_RELEASE_GATE_STATISTICS"]
