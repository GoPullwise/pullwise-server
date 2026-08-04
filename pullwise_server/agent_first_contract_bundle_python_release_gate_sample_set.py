"""Deterministic Python release-gate raw sample semantics."""

from __future__ import annotations


PYTHON_RELEASE_GATE_SAMPLE_SET = r'''
_RELEASE_SAMPLE_TASK_FIELDS = (
    "task_id", "task_kind", "unknown_family_id", "cluster_id",
    "case_category", "criticality", "profile_id",
    "oracle_in_scope_finding_count", "expected_failure_outcome",
)


def _release_sample_task_projection(
    sample: dict[str, object],
) -> dict[str, object]:
    return {field: sample[field] for field in _RELEASE_SAMPLE_TASK_FIELDS}


def _rule_release_gate_sample_set(value: dict[str, object]) -> None:
    samples = value["samples"]
    _release_require(
        _ordered_unique(
            samples,
            lambda item: (item["cohort"], item["task_id"], item["seed"]),
        ),
        "RELEASE_SAMPLE_ORDER_INVALID",
        "$.samples",
    )
    expected_cohorts = (
        {"CANDIDATE"}
        if value["release_mode"] == "BOOTSTRAP"
        else {"CANDIDATE", "STABLE"}
    )
    _release_require(
        {item["cohort"] for item in samples} == expected_cohorts,
        "RELEASE_SAMPLE_MODE_INVALID",
        "$.samples",
    )
    tasks: dict[str, dict[str, object]] = {}
    for index, sample in enumerate(samples):
        identity = {
            field: sample[field] for field in ("cohort", "task_id", "seed")
        }
        expected_id = "sample_" + _release_digest(
            "pullwise:release-gate-sample-identity:v1",
            identity,
        )
        _release_require(
            sample["sample_id"] == expected_id,
            "RELEASE_SAMPLE_ID_INVALID",
            f"$.samples[{index}].sample_id",
        )
        task = _release_sample_task_projection(sample)
        previous = tasks.setdefault(sample["task_id"], task)
        _release_require(
            _json_equal(previous, task),
            "RELEASE_SAMPLE_TASK_DRIFT",
            f"$.samples[{index}]",
        )
    candidate_task_ids = {
        item["task_id"] for item in samples if item["cohort"] == "CANDIDATE"
    }
    task_inventory = [tasks[task_id] for task_id in sorted(candidate_task_ids)]
    _release_require(
        value["task_inventory_digest"]
        == _release_digest(
            "pullwise:release-gate-task-inventory:v1",
            task_inventory,
        ),
        "RELEASE_SAMPLE_TASK_INVENTORY_INVALID",
        "$.task_inventory_digest",
    )
'''


__all__ = ["PYTHON_RELEASE_GATE_SAMPLE_SET"]
