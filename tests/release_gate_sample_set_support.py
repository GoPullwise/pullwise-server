from __future__ import annotations

from copy import deepcopy
import hashlib


TASK_FIELDS = (
    "task_id",
    "task_kind",
    "unknown_family_id",
    "cluster_id",
    "case_category",
    "criticality",
    "profile_id",
    "oracle_in_scope_finding_count",
    "expected_failure_outcome",
)
CASE_CATEGORIES = (
    "ADVERSARIAL_INPUT",
    "BAD_OR_INCOMPLETE_PATCH",
    "ENVIRONMENT_OR_CAPABILITY_FAILURE",
    "FAKE_SUCCESS_OR_ZERO_TEST",
    "REAL_FIX",
)


def _digest(contract, domain: str, value: object) -> str:
    return hashlib.sha256(
        domain.encode("ascii")
        + b"\0"
        + contract.canonical_document_bytes(value)
    ).hexdigest()


def _content_ref(contract, original: dict[str, object], document: object):
    encoded = contract.canonical_document_bytes(document)
    return {
        **original,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "size_bytes": len(encoded),
    }


def _task(index: int) -> dict[str, object]:
    known = index < 120
    local_index = index if known else index - 120
    category = (
        CASE_CATEGORIES[local_index // 3]
        if local_index < 15
        else "REAL_FIX"
    )
    unknown_family_id = (
        None
        if known
        else ("unknown_alpha", "unknown_beta", "unknown_gamma")[
            local_index // 15
        ]
    )
    return {
        "task_id": f"benchmark_task_{index + 1:032x}",
        "task_kind": "KNOWN" if known else "UNKNOWN",
        "unknown_family_id": unknown_family_id,
        "cluster_id": "cluster_known" if known else "cluster_unknown",
        "case_category": category,
        "criticality": "CRITICAL" if index < 5 else "NON_CRITICAL",
        "profile_id": "profile_mvp_q1" if index % 2 == 0 else "profile_mvp_q2",
        "oracle_in_scope_finding_count": 1 if index < 50 else 0,
        "expected_failure_outcome": (
            "BLOCKED"
            if category == "ENVIRONMENT_OR_CAPABILITY_FAILURE"
            else None
        ),
    }


def _sample(contract, task: dict[str, object], seed: int) -> dict[str, object]:
    classification = (
        task["case_category"] == "ENVIRONMENT_OR_CAPABILITY_FAILURE"
    )
    sample = {
        "cohort": "CANDIDATE",
        **task,
        "seed": seed,
        "disposition": "INCLUDED",
        "infrastructure_reason_code": None,
        "evidence_issue_codes": [],
        "observation": {
            "terminal_outcome": (
                task["expected_failure_outcome"]
                if classification
                else "COMPLETED"
            ),
            "hidden_oracle_passed": True,
            "human_answer_supplied": False,
            "reported_oracle_in_scope_finding_count": task[
                "oracle_in_scope_finding_count"
            ],
            "mandatory_requirement_count": 0 if classification else 1,
            "covered_mandatory_requirement_count": 0 if classification else 1,
            "source_state_proof_count": 0 if classification else 1,
            "covered_source_state_proof_count": 0 if classification else 1,
            "safety_authority_violation_count": 0,
            "stale_publish_count": 0,
            "duplicate_effect_or_result_count": 0,
            "wall_ms": 1000,
            "token_count": 100,
            "cost_microusd": 1000,
        },
    }
    identity = {
        field: sample[field] for field in ("cohort", "task_id", "seed")
    }
    sample["sample_id"] = "sample_" + _digest(
        contract,
        "pullwise:release-gate-sample-identity:v1",
        identity,
    )
    return sample


def coherent_bootstrap_documents(contract):
    tasks = [_task(index) for index in range(165)]
    task_inventory = [
        {field: task[field] for field in TASK_FIELDS} for task in tasks
    ]
    task_inventory_digest = _digest(
        contract,
        "pullwise:release-gate-task-inventory:v1",
        task_inventory,
    )

    benchmark = deepcopy(
        contract.fixture("benchmark_bundle_golden_current")["document"]
    )
    benchmark.pop("bundle_digest")
    benchmark["task_inventory_digest"] = task_inventory_digest
    benchmark = contract.seal_document("benchmark-bundle/v1", benchmark)

    policy = deepcopy(
        contract.fixture("release_gate_policy_golden_bootstrap")["document"]
    )
    policy.pop("policy_digest")
    policy["benchmark_ref"] = _content_ref(
        contract, policy["benchmark_ref"], benchmark
    )
    policy["benchmark_digest"] = benchmark["bundle_digest"]
    for field in (
        "benchmark_version",
        "task_inventory_digest",
        "oracle_rubric_digest",
        "environment_image_digest",
        "control_plane_digest",
        "evaluation_runtime_digest",
        "statistical_implementation_version",
    ):
        policy[field] = benchmark[field]
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

    sample_set = deepcopy(
        contract.fixture(
            "release_gate_sample_set_golden_bootstrap_included"
        )["document"]
    )
    sample_set.pop("sample_set_digest")
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
        "organization_id",
    ):
        sample_set[field] = policy[field]
    sample_set["benchmark_ref"] = _content_ref(
        contract, sample_set["benchmark_ref"], benchmark
    )
    sample_set["policy_ref"] = _content_ref(
        contract, sample_set["policy_ref"], policy
    )
    sample_set["policy_digest"] = policy["policy_digest"]
    sample_set["samples"] = [
        _sample(contract, task, seed)
        for task in tasks
        for seed in benchmark["seeds"]
    ]
    sample_set = contract.seal_document(
        "release-gate-sample-set/v1", sample_set
    )
    return benchmark, policy, sample_set


def coherent_stable_documents(contract):
    benchmark, policy, sample_set = coherent_bootstrap_documents(contract)
    policy = deepcopy(policy)
    policy.pop("policy_digest")
    policy["release_mode"] = "STABLE"
    policy["stable_package"] = deepcopy(policy["package"])
    policy["stable_candidate_digest"] = "9" * 64
    policy["stable_control_plane_digest"] = policy["control_plane_digest"]
    for gate in policy["relative_gates"]:
        gate["applicability"] = "REQUIRED"
    policy["threshold_table_digest"] = _digest(
        contract,
        "pullwise:release-threshold-table:v1",
        {
            "absolute_gates": policy["absolute_gates"],
            "relative_gates": policy["relative_gates"],
            "infrastructure_reason_codes": policy[
                "infrastructure_reason_codes"
            ],
        },
    )
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

    sample_set = deepcopy(sample_set)
    sample_set.pop("sample_set_digest")
    for field in (
        "candidate_digest",
        "release_mode",
        "stable_package",
        "stable_candidate_digest",
        "stable_control_plane_digest",
    ):
        sample_set[field] = policy[field]
    sample_set["policy_ref"] = _content_ref(
        contract, sample_set["policy_ref"], policy
    )
    sample_set["policy_digest"] = policy["policy_digest"]
    stable_samples = []
    for candidate in sample_set["samples"]:
        stable = deepcopy(candidate)
        stable["cohort"] = "STABLE"
        stable["sample_id"] = "sample_" + _digest(
            contract,
            "pullwise:release-gate-sample-identity:v1",
            {
                field: stable[field]
                for field in ("cohort", "task_id", "seed")
            },
        )
        stable_samples.append(stable)
    sample_set["samples"].extend(stable_samples)
    sample_set = contract.seal_document(
        "release-gate-sample-set/v1", sample_set
    )
    return benchmark, policy, sample_set


__all__ = ["coherent_bootstrap_documents", "coherent_stable_documents"]
