"""Generated Python facade semantics for GateInput snapshots."""

from __future__ import annotations


PYTHON_GATE_INPUT = r'''
_GATE_INPUT_KEYS = {
    "schema_id",
    "task_id",
    "attempt_id",
    "native_epoch",
    "owner_id",
    "owner_epoch",
    "task_version",
    "lifecycle",
    "desired_state",
    "lease_id",
    "outer_lease_expires_at",
    "outer_lease_grace_expires_at",
    "authoritative_cancel_received",
    "absolute_deadline_at",
    "trusted_wall_time_at",
    "monotonic_deadline_remaining_ms",
    "terminal_budget_reserved_ms",
    "predicate_registry_digest",
    "request_ref",
    "policy_ref",
    "requirement_ledger_ref",
    "completion_proposal_ref",
    "quality_policy_plan_ref",
    "original_source_ref",
    "final_source_ref",
    "execution_state_refs",
    "change_set",
    "pre_observation_manifest_ref",
    "final_observation_manifest_ref",
    "verification_attestation_manifest_ref",
    "effect_ledger_ref",
    "budget_summary_ref",
    "publication_content_manifest_ref",
    "debug_redaction_plan_ref",
    "pre_gate_root_set_ref",
    "pre_gate_evidence_closure_ref",
    "pre_gate_closure_digest",
    "requested_outcome",
    "input_digest",
}
_TERMINALIZATION_INPUT_KEYS = {
    "schema_id",
    "task_id",
    "attempt_id",
    "native_epoch",
    "owner_id",
    "owner_epoch",
    "task_version",
    "lifecycle",
    "desired_state",
    "lease_id",
    "outer_lease_expires_at",
    "outer_lease_grace_expires_at",
    "absolute_deadline_at",
    "trusted_wall_time_at",
    "monotonic_deadline_remaining_ms",
    "terminal_budget_reserved_ms",
    "predicate_registry_digest",
    "request_ref",
    "policy_ref",
    "requirement_ledger_ref",
    "original_source",
    "final_source",
    "final_observation_manifest",
    "effect_ledger_ref",
    "budget_summary_ref",
    "publication_content_manifest_ref",
    "debug_redaction_plan_ref",
    "terminalization_fact_refs",
    "pre_gate_root_set_ref",
    "pre_gate_evidence_closure_ref",
    "pre_gate_closure_digest",
    "input_digest",
}


def _rule_gate_input_snapshot(value: dict[str, object]) -> None:
    _require(
        set(value) == _GATE_INPUT_KEYS,
        "GATE_INPUT_CLOSURE_DIRECTION_INVALID",
    )
    execution_refs = value["execution_state_refs"]
    _require(
        _ordered_unique(execution_refs, _ref_key),
        "GATE_INPUT_EXECUTION_ORDER_INVALID",
        "$.execution_state_refs",
    )
    verify_content_ref_set(execution_refs)
    lease_expires = _timestamp_millis(value["outer_lease_expires_at"])
    grace_expires = _timestamp_millis(value["outer_lease_grace_expires_at"])
    _require(
        lease_expires is not None
        and grace_expires is not None
        and lease_expires <= grace_expires,
        "GATE_INPUT_LEASE_TIME_INVALID",
        "$.outer_lease_grace_expires_at",
    )
    wall_time = _timestamp_millis(value["trusted_wall_time_at"])
    deadline = _timestamp_millis(value["absolute_deadline_at"])
    _require(
        wall_time is not None and deadline is not None and wall_time <= deadline,
        "GATE_INPUT_DEADLINE_INVALID",
        "$.trusted_wall_time_at",
    )
    _require(
        value["terminal_budget_reserved_ms"]
        <= value["monotonic_deadline_remaining_ms"],
        "GATE_INPUT_TERMINAL_RESERVE_INVALID",
        "$.terminal_budget_reserved_ms",
    )


def _rule_terminalization_input_snapshot(value: dict[str, object]) -> None:
    _require(
        set(value) == _TERMINALIZATION_INPUT_KEYS,
        "GATE_INPUT_CLOSURE_DIRECTION_INVALID",
    )
    facts = value["terminalization_fact_refs"]
    _require(
        bool(facts) and _ordered_unique(facts, _ref_key),
        "TERMINALIZATION_FACT_ORDER_INVALID",
        "$.terminalization_fact_refs",
    )
    verify_content_ref_set(facts)
    has_attempt = value["attempt_id"] is not None
    if has_attempt:
        attempt_binding_valid = (
            value["native_epoch"] >= 1
            and value["owner_epoch"] >= 1
            and value["lease_id"] is not None
            and value["outer_lease_expires_at"] is not None
            and value["outer_lease_grace_expires_at"] is not None
        )
    else:
        attempt_binding_valid = (
            value["native_epoch"] == 0
            and value["owner_epoch"] == 0
            and value["lease_id"] is None
            and value["outer_lease_expires_at"] is None
            and value["outer_lease_grace_expires_at"] is None
        )
    _require(
        attempt_binding_valid,
        "TERMINALIZATION_ATTEMPT_BINDING_INVALID",
        "$.attempt_id",
    )
    if has_attempt:
        lease_expires = _timestamp_millis(value["outer_lease_expires_at"])
        grace_expires = _timestamp_millis(
            value["outer_lease_grace_expires_at"]
        )
        _require(
            lease_expires is not None
            and grace_expires is not None
            and lease_expires <= grace_expires,
            "GATE_INPUT_LEASE_TIME_INVALID",
            "$.outer_lease_grace_expires_at",
        )
    _require(
        _timestamp_millis(value["absolute_deadline_at"]) is not None
        and _timestamp_millis(value["trusted_wall_time_at"]) is not None,
        "GATE_INPUT_DEADLINE_INVALID",
        "$.trusted_wall_time_at",
    )


def _require_projection(
    actual: object,
    expected: object,
    path: str,
) -> None:
    _require(_json_equal(actual, expected), "GATE_INPUT_STALE", path)


def _verify_snapshot_pre_gate_context(
    snapshot: dict[str, object],
    root_set: dict[str, object],
    pre_gate_manifest: dict[str, object],
) -> None:
    _require(
        snapshot["task_id"]
        == root_set["task_id"]
        == pre_gate_manifest["task_id"],
        "GATE_INPUT_STALE",
        "$.task_id",
    )
    _require(
        _json_equal(
            snapshot["pre_gate_root_set_ref"],
            pre_gate_manifest["pre_gate_root_set_ref"],
        ),
        "GATE_INPUT_STALE",
        "$.pre_gate_root_set_ref",
    )
    _require(
        _content_ref_matches_direct_document(
            snapshot["pre_gate_root_set_ref"],
            "pre-gate-root-set/v1",
            root_set,
        ),
        "CAS_CORRUPT",
        "$.pre_gate_root_set_ref",
    )
    _require(
        _content_ref_matches_direct_document(
            snapshot["pre_gate_evidence_closure_ref"],
            "pre-gate-evidence-closure-manifest/v1",
            pre_gate_manifest,
        ),
        "CAS_CORRUPT",
        "$.pre_gate_evidence_closure_ref",
    )
    _require(
        snapshot["pre_gate_closure_digest"]
        == pre_gate_manifest["pre_gate_closure_digest"],
        "EVIDENCE_CLOSURE_INVALID",
        "$.pre_gate_closure_digest",
    )


def verify_gate_input_snapshot_context(
    snapshot: object,
    root_set: object,
    pre_gate_manifest: object,
    completion_proposal: object,
) -> dict[str, object]:
    """Bind a success snapshot to direct PreGate documents and projections."""
    validated = verify_document_digest("gate-input-snapshot/v1", snapshot)
    roots = verify_document_digest("pre-gate-root-set/v1", root_set)
    manifest = verify_pre_gate_evidence_closure_context(
        pre_gate_manifest, roots
    )
    proposal = verify_document_digest(
        "completion-proposal/v1", completion_proposal
    )
    _verify_snapshot_pre_gate_context(validated, roots, manifest)
    for field in _PRE_GATE_SUCCESS_AVAILABLE:
        _require(
            _availability(roots[field]) == "available",
            "PRE_GATE_SUCCESS_ROOT_UNAVAILABLE",
            f"$.{field}",
        )
    for field in ("verifier_inputs", "verifier_work"):
        _require(
            bool(roots[field])
            and all(_availability(item) == "available" for item in roots[field]),
            "PRE_GATE_SUCCESS_VERIFICATION_UNAVAILABLE",
            f"$.{field}",
        )
    _require(
        _content_ref_matches_direct_document(
            roots["proposal"]["ref"],
            "completion-proposal/v1",
            proposal,
        ),
        "CAS_CORRUPT",
        "$.completion_proposal_ref",
    )
    _require(
        proposal["task_id"] == validated["task_id"]
        and proposal["outcome_requested"] in _PRE_GATE_SUCCESS_OUTCOMES
        and validated["requested_outcome"] == proposal["outcome_requested"],
        "GATE_INPUT_STALE",
        "$.requested_outcome",
    )
    projections = {
        "request_ref": roots["request"]["ref"],
        "policy_ref": roots["policy"]["ref"],
        "requirement_ledger_ref": roots["ledger"]["ref"],
        "completion_proposal_ref": roots["proposal"]["ref"],
        "original_source_ref": roots["original_source"]["ref"],
        "final_source_ref": roots["final_source"]["ref"],
        "pre_observation_manifest_ref": roots["pre_observation_manifest"]["ref"],
        "final_observation_manifest_ref": roots["final_observation_manifest"]["ref"],
        "verification_attestation_manifest_ref": roots["attestations"]["ref"],
        "effect_ledger_ref": roots["effect_ledger"]["ref"],
        "budget_summary_ref": roots["budget_summary"]["ref"],
        "publication_content_manifest_ref": roots["publication_content_manifest"]["ref"],
        "debug_redaction_plan_ref": roots["debug_redaction_plan"]["ref"],
        "change_set": roots["change_set"],
        "execution_state_refs": [
            item["ref"]
            for item in roots["execution_states"]
            if item["availability"] == "available"
        ],
    }
    for field, expected in projections.items():
        _require_projection(validated[field], expected, f"$.{field}")
    _require(
        any(
            _json_equal(validated["quality_policy_plan_ref"], item)
            for item in manifest["entries"]
        ),
        "EVIDENCE_CLOSURE_INVALID",
        "$.quality_policy_plan_ref",
    )
    return validated


def verify_terminalization_input_snapshot_context(
    snapshot: object,
    root_set: object,
    pre_gate_manifest: object,
    terminalization_facts: object,
) -> dict[str, object]:
    """Bind a terminal snapshot to direct PreGate and authority-fact documents."""
    validated = verify_document_digest(
        "terminalization-input-snapshot/v1", snapshot
    )
    roots = verify_document_digest("pre-gate-root-set/v1", root_set)
    manifest = verify_pre_gate_evidence_closure_context(
        pre_gate_manifest, roots
    )
    _verify_snapshot_pre_gate_context(validated, roots, manifest)
    _require(
        bool(roots["termination_facts"])
        and all(
            _availability(item) == "available"
            for item in roots["termination_facts"]
        ),
        "PRE_GATE_TERMINATION_FACT_REQUIRED",
        "$.termination_facts",
    )
    projections = {
        "request_ref": roots["request"]["ref"],
        "policy_ref": roots["policy"]["ref"],
        "requirement_ledger_ref": roots["ledger"]["ref"],
        "original_source": roots["original_source"],
        "final_source": roots["final_source"],
        "final_observation_manifest": roots["final_observation_manifest"],
        "effect_ledger_ref": roots["effect_ledger"]["ref"],
        "budget_summary_ref": roots["budget_summary"]["ref"],
        "publication_content_manifest_ref": roots["publication_content_manifest"]["ref"],
        "debug_redaction_plan_ref": roots["debug_redaction_plan"]["ref"],
        "terminalization_fact_refs": [
            item["ref"]
            for item in roots["termination_facts"]
            if item["availability"] == "available"
        ],
    }
    for field, expected in projections.items():
        _require_projection(validated[field], expected, f"$.{field}")

    _require(
        isinstance(terminalization_facts, list)
        and len(terminalization_facts)
        == len(validated["terminalization_fact_refs"]),
        "GATE_INPUT_STALE",
        "$.terminalization_fact_refs",
    )
    facts = [
        verify_document_digest("terminalization-fact/v1", item)
        for item in terminalization_facts
    ]
    for index, fact in enumerate(facts):
        _require(
            fact["task_id"] == validated["task_id"]
            and fact["observed_task_version"] < validated["task_version"],
            "GATE_INPUT_STALE",
            f"$.terminalization_fact_refs[{index}]",
        )
    _require(
        max(fact["observed_task_version"] for fact in facts)
        == validated["task_version"] - 1,
        "GATE_INPUT_STALE",
        "$.terminalization_fact_refs",
    )
    unmatched = list(facts)
    for index, ref in enumerate(validated["terminalization_fact_refs"]):
        matches = [
            fact
            for fact in unmatched
            if _content_ref_matches_direct_document(
                ref, "terminalization-fact/v1", fact
            )
        ]
        _require(
            len(matches) == 1,
            "CAS_CORRUPT",
            f"$.terminalization_fact_refs[{index}]",
        )
        unmatched.remove(matches[0])
    return validated
'''


__all__ = ["PYTHON_GATE_INPUT"]
