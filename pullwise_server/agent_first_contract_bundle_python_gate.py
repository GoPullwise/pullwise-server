"""Generated Python facade semantics for GateDecision documents."""

from __future__ import annotations


PYTHON_GATE = r'''
_GATE_PREDICATE_ENTRIES = [
    {
        "predicate_id": "GATE_TASK_STATE",
        "decision_kind": "success",
        "input_schema_ids": ["attempt-record/v1", "gate-input-snapshot/v1", "task-record/v1"],
        "failure_codes": ["GATE_INPUT_STALE", "STATE_TRANSITION_INVALID", "TASK_VERSION_STALE"],
    },
    {
        "predicate_id": "GATE_LEASE_VALID",
        "decision_kind": "success",
        "input_schema_ids": ["gate-input-snapshot/v1"],
        "failure_codes": ["LEASE_INVALID", "NATIVE_EPOCH_STALE", "OWNER_EPOCH_STALE"],
    },
    {
        "predicate_id": "GATE_DEADLINE",
        "decision_kind": "success",
        "input_schema_ids": ["budget-summary/v1", "gate-input-snapshot/v1"],
        "failure_codes": ["ABSOLUTE_DEADLINE_EXCEEDED", "TERMINALIZATION_RESERVE_REACHED"],
    },
    {
        "predicate_id": "GATE_POLICY",
        "decision_kind": "success",
        "input_schema_ids": ["effective-execution-policy/v1"],
        "failure_codes": ["POLICY_INVARIANT_BROKEN", "POLICY_UNSUPPORTED"],
    },
    {
        "predicate_id": "GATE_LEDGER",
        "decision_kind": "success",
        "input_schema_ids": ["requirement-ledger/v1"],
        "failure_codes": ["CONTRACT_DOCUMENT_INVALID", "REQUIREMENT_ID_COLLISION"],
    },
    {
        "predicate_id": "GATE_SOURCE_FROZEN",
        "decision_kind": "success",
        "input_schema_ids": ["source-tree-manifest/v1"],
        "failure_codes": ["SOURCE_STATE_CHANGED", "SOURCE_STATE_MISMATCH"],
    },
    {
        "predicate_id": "GATE_PROPOSAL_FRESH",
        "decision_kind": "success",
        "input_schema_ids": ["completion-proposal/v1", "gate-input-snapshot/v1"],
        "failure_codes": ["GATE_INPUT_STALE", "SOURCE_STATE_MISMATCH"],
    },
    {
        "predicate_id": "GATE_QUALITY_PLAN",
        "decision_kind": "success",
        "input_schema_ids": ["quality-policy-plan/v1"],
        "failure_codes": ["POLICY_INVARIANT_BROKEN", "ROLE_NOT_ENABLED"],
    },
    {
        "predicate_id": "GATE_ATTESTATIONS",
        "decision_kind": "success",
        "input_schema_ids": ["observation-manifest/v1", "verification-attestation-manifest/v1"],
        "failure_codes": ["ATTESTATION_NOT_INDEPENDENT", "OBSERVATION_ACTOR_MISMATCH", "OBSERVATION_MISSING"],
    },
    {
        "predicate_id": "GATE_REQUIREMENTS",
        "decision_kind": "success",
        "input_schema_ids": ["requirement-ledger/v1", "verification-attestation-manifest/v1"],
        "failure_codes": ["MANDATORY_REQUIREMENT_FAILED", "MANDATORY_REQUIREMENT_UNVERIFIABLE", "WAIVER_INVALID"],
    },
    {
        "predicate_id": "GATE_OUTCOME_SHAPE",
        "decision_kind": "success",
        "input_schema_ids": ["completion-proposal/v1"],
        "failure_codes": ["CONTRACT_DOCUMENT_INVALID", "POLICY_INVARIANT_BROKEN"],
    },
    {
        "predicate_id": "GATE_EFFECTS_EMPTY",
        "decision_kind": "success",
        "input_schema_ids": ["effect-ledger-snapshot/v1"],
        "failure_codes": ["EVENT_DELIVERY_UNKNOWN", "POLICY_INVARIANT_BROKEN"],
    },
    {
        "predicate_id": "GATE_EVIDENCE_CLOSURE",
        "decision_kind": "success",
        "input_schema_ids": ["pre-gate-evidence-closure-manifest/v1", "pre-gate-root-set/v1"],
        "failure_codes": ["CAS_CORRUPT", "EVIDENCE_CLOSURE_INVALID"],
    },
    {
        "predicate_id": "GATE_BUDGET",
        "decision_kind": "success",
        "input_schema_ids": ["budget-summary/v1"],
        "failure_codes": ["BUDGET_EXHAUSTED", "TERMINALIZATION_RESERVE_REACHED"],
    },
    {
        "predicate_id": "GATE_SECRET_SCAN",
        "decision_kind": "success",
        "input_schema_ids": ["debug-redaction-plan/v1", "pre-gate-evidence-closure-manifest/v1", "publication-content-manifest/v1"],
        "failure_codes": ["DEBUG_REDACTION_FAILED", "POLICY_INVARIANT_BROKEN"],
    },
    {
        "predicate_id": "GATE_TERMINAL_AUTHORITY_FACT",
        "decision_kind": "terminalization",
        "input_schema_ids": ["terminalization-fact/v1", "terminalization-input-snapshot/v1"],
        "failure_codes": ["CONTRACT_DOCUMENT_INVALID", "GATE_INPUT_STALE"],
    },
    {
        "predicate_id": "GATE_TERMINAL_AVAILABILITY",
        "decision_kind": "terminalization",
        "input_schema_ids": ["terminalization-input-snapshot/v1"],
        "failure_codes": ["EXECUTION_STATE_UNAVAILABLE", "OBSERVATION_MISSING", "SOURCE_STATE_MISMATCH"],
    },
    {
        "predicate_id": "GATE_TERMINAL_NO_ACTIVE_EFFECTS",
        "decision_kind": "terminalization",
        "input_schema_ids": ["effect-ledger-snapshot/v1", "terminalization-input-snapshot/v1"],
        "failure_codes": ["EVENT_DELIVERY_UNKNOWN", "POLICY_INVARIANT_BROKEN"],
    },
    {
        "predicate_id": "GATE_TERMINAL_OUTCOME_CLASSIFICATION",
        "decision_kind": "terminalization",
        "input_schema_ids": ["terminalization-fact/v1", "terminalization-input-snapshot/v1"],
        "failure_codes": ["CONTRACT_DOCUMENT_INVALID", "POLICY_INVARIANT_BROKEN"],
    },
    {
        "predicate_id": "GATE_TERMINAL_ARTIFACT_DELIVERY",
        "decision_kind": "terminalization",
        "input_schema_ids": ["publication-content-manifest/v1", "terminalization-fact/v1"],
        "failure_codes": ["DEBUG_UPLOAD_FAILED", "EVENT_DELIVERY_UNKNOWN", "PROTOCOL_FAILURE"],
    },
]

_GATE_TERMINAL_REASONS = {
    "PARTIAL": {
        "BUDGET_EXHAUSTED", "CAPABILITY_UNAVAILABLE", "DEADLINE_REACHED",
        "INTERACTION_UNAVAILABLE", "SAFE_PARTIAL_DELIVERY", "VERIFICATION_INCOMPLETE",
    },
    "BLOCKED": {
        "APPROVAL_REQUIRED", "CAPABILITY_UNAVAILABLE", "ENVIRONMENT_UNAVAILABLE",
        "INPUT_REQUIRED", "INTERACTION_UNAVAILABLE", "POLICY_INVARIANT_BROKEN",
        "POLICY_UNSUPPORTED",
    },
    "FAILED": {
        "BUDGET_EXHAUSTED", "CONTRACT_INVALID", "DEADLINE_REACHED",
        "POLICY_INVARIANT_BROKEN", "POLICY_UNSUPPORTED", "PROTOCOL_FAILURE",
        "QUALITY_GATE_FAILED", "RUNTIME_FAILURE", "SOURCE_MUTATION_FORBIDDEN",
        "STORAGE_FAILURE",
    },
    "CANCELLED": {"LEASE_CANCELLED", "SERVER_CANCELLED", "USER_CANCELLED"},
}


def _gate_verify_digest(
    schema_id: str, value: dict[str, object], field: str
) -> None:
    presented = value[field]
    spec = schema(schema_id)["x-pullwise-digest"]
    unsigned = {key: item for key, item in value.items() if key != field}
    expected = hashlib.sha256(
        spec["domain"].encode("utf-8")
        + b"\0"
        + canonical_document_bytes(unsigned)
    ).hexdigest()
    _require(presented == expected, "CONTRACT_DIGEST_MISMATCH", f"$.{field}")


def _rule_gate_predicate_registry(value: dict[str, object]) -> None:
    _require(
        value["predicates"] == _GATE_PREDICATE_ENTRIES,
        "GATE_PREDICATE_REGISTRY_INVALID",
        "$.predicates",
    )
    _gate_verify_digest(
        "gate-predicate-registry/v1", value, "registry_digest"
    )


def _gate_registry_digest() -> str:
    return fixture("gate_golden_independent_registry")["document"][
        "registry_digest"
    ]


def _gate_expected_entries(decision_kind: str) -> list[dict[str, object]]:
    return [
        item
        for item in _GATE_PREDICATE_ENTRIES
        if item["decision_kind"] == decision_kind
    ]


def _gate_validate_ref_order(
    refs: list[dict[str, object]], path: str
) -> None:
    verify_content_ref_set(refs)
    keys = [_ref_key(item) for item in refs]
    _require(
        keys == sorted(set(keys)),
        "GATE_PREDICATE_EVIDENCE_INVALID",
        path,
    )


def _rule_gate_decision(value: dict[str, object]) -> None:
    _require(
        value["predicate_registry_digest"] == _gate_registry_digest(),
        "GATE_PREDICATE_REGISTRY_DIGEST_INVALID",
        "$.predicate_registry_digest",
    )
    expected = _gate_expected_entries(value["decision_kind"])
    results = value["predicate_results"]
    _require(
        [item["predicate_id"] for item in results]
        == [item["predicate_id"] for item in expected],
        "GATE_PREDICATE_ORDER_INVALID",
        "$.predicate_results",
    )
    for index, (result, predicate) in enumerate(zip(results, expected)):
        path = f"$.predicate_results[{index}]"
        _require(
            result["passed"] == (result["failure_code"] is None),
            "GATE_PREDICATE_RESULT_INVALID",
            f"{path}.failure_code",
        )
        _require(
            result["failure_code"] is None
            or result["failure_code"] in predicate["failure_codes"],
            "GATE_PREDICATE_FAILURE_CODE_INVALID",
            f"{path}.failure_code",
        )
        refs = result["evidence_refs"]
        _gate_validate_ref_order(refs, f"{path}.evidence_refs")
        _require(
            all(
                item["content_schema_id"] in predicate["input_schema_ids"]
                for item in refs
            ),
            "GATE_PREDICATE_EVIDENCE_INVALID",
            f"{path}.evidence_refs",
        )
    _require(
        value["passed"] == all(item["passed"] for item in results),
        "GATE_DECISION_PASS_INVALID",
        "$.passed",
    )
    if value["decision_kind"] == "terminalization":
        _require(
            value["selected_reason"]
            in _GATE_TERMINAL_REASONS[value["selected_outcome"]],
            "GATE_TERMINAL_OUTCOME_INVALID",
            "$.selected_reason",
        )
        refs = value["authoritative_fact_refs"]
        verify_content_ref_set(refs)
        keys = [_ref_key(item) for item in refs]
        _require(
            keys == sorted(set(keys)),
            "GATE_TERMINAL_FACT_ORDER_INVALID",
            "$.authoritative_fact_refs",
        )
    _gate_verify_digest("gate-decision/v1", value, "decision_digest")


def _gate_context(
    context: object, expected_keys: set[str]
) -> dict[str, object]:
    if not isinstance(context, dict) or set(context) != expected_keys:
        _fail("GATE_EVALUATION_CONTEXT_INVALID", "$.context")
    return json.loads(canonical_document_bytes(context).decode("utf-8"))


def _gate_passed(predicate_results: object) -> bool:
    if (
        not isinstance(predicate_results, list)
        or not all(
            isinstance(item, dict) and isinstance(item.get("passed"), bool)
            for item in predicate_results
        )
    ):
        _fail(
            "GATE_EVALUATION_CONTEXT_INVALID",
            "$.context.predicate_results",
        )
    return all(item["passed"] for item in predicate_results)


def _gate_snapshot_and_ref(
    schema_id: str, snapshot_value: object, reference_value: object
) -> tuple[dict[str, object], dict[str, object]]:
    snapshot = verify_document_digest(schema_id, snapshot_value)
    reference = validate_document("content-ref/v1", reference_value)
    raw = canonical_document_bytes(snapshot)
    if (
        reference["content_schema_id"] != schema_id
        or reference["sha256"] != hashlib.sha256(raw).hexdigest()
        or reference["size_bytes"] != len(raw)
        or reference["media_type"] != "application/json"
        or reference["encoding"] != "utf-8"
    ):
        _fail(
            "GATE_INPUT_SNAPSHOT_REF_MISMATCH",
            "$.context.input_snapshot_ref",
        )
    _require(
        snapshot["predicate_registry_digest"] == _gate_registry_digest(),
        "GATE_PREDICATE_REGISTRY_DIGEST_INVALID",
        "$.predicate_registry_digest",
    )
    return snapshot, reference


def evaluate_success_gate(
    input_snapshot: object, context: object
) -> dict[str, object]:
    evaluation = _gate_context(
        context, {"input_snapshot_ref", "predicate_results"}
    )
    snapshot, reference = _gate_snapshot_and_ref(
        "gate-input-snapshot/v1",
        input_snapshot,
        evaluation["input_snapshot_ref"],
    )
    results = evaluation["predicate_results"]
    passed = _gate_passed(results)
    return seal_document(
        "gate-decision/v1",
        {
            "schema_id": "gate-decision/v1",
            "decision_kind": "success",
            "input_snapshot_ref": reference,
            "input_digest": snapshot["input_digest"],
            "predicate_registry_digest": snapshot["predicate_registry_digest"],
            "requested_outcome": snapshot["requested_outcome"],
            "passed": passed,
            "predicate_results": results,
        },
    )


def evaluate_terminalization_gate(
    input_snapshot: object, context: object
) -> dict[str, object]:
    evaluation = _gate_context(
        context,
        {
            "input_snapshot_ref", "selected_outcome", "selected_reason",
            "source_availability", "evidence_availability",
            "effect_availability", "predicate_results",
        },
    )
    snapshot, reference = _gate_snapshot_and_ref(
        "terminalization-input-snapshot/v1",
        input_snapshot,
        evaluation["input_snapshot_ref"],
    )
    _require(
        evaluation["source_availability"] == snapshot["final_source"],
        "GATE_TERMINAL_AVAILABILITY_MISMATCH",
        "$.context.source_availability",
    )
    expected_effect = {
        "availability": "available",
        "ref": snapshot["effect_ledger_ref"],
    }
    _require(
        evaluation["effect_availability"] == expected_effect,
        "GATE_TERMINAL_AVAILABILITY_MISMATCH",
        "$.context.effect_availability",
    )
    results = evaluation["predicate_results"]
    passed = _gate_passed(results)
    return seal_document(
        "gate-decision/v1",
        {
            "schema_id": "gate-decision/v1",
            "decision_kind": "terminalization",
            "input_snapshot_ref": reference,
            "input_digest": snapshot["input_digest"],
            "predicate_registry_digest": snapshot["predicate_registry_digest"],
            "selected_outcome": evaluation["selected_outcome"],
            "selected_reason": evaluation["selected_reason"],
            "authoritative_fact_refs": snapshot["terminalization_fact_refs"],
            "source_availability": evaluation["source_availability"],
            "evidence_availability": evaluation["evidence_availability"],
            "effect_availability": evaluation["effect_availability"],
            "passed": passed,
            "predicate_results": results,
        },
    )
'''


__all__ = ["PYTHON_GATE"]
