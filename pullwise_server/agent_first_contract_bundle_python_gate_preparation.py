"""Generated Python facade semantics for GatePreparation documents."""

from __future__ import annotations


PYTHON_GATE_PREPARATION = r'''
_TERMINALIZATION_REASON_CODES = {
    "BUDGET_EXHAUSTED",
    "CAPABILITY_UNAVAILABLE",
    "DEADLINE_REACHED",
    "INTERACTION_UNAVAILABLE",
    "POLICY_INVARIANT_BROKEN",
    "PROTOCOL_FAILURE",
    "RUNTIME_FAILURE",
    "STORAGE_FAILURE",
}
_TERMINALIZATION_CONTROL_ACTOR_KINDS = {
    "server_control",
    "system_reconciler",
    "worker_control",
}
_TERMINALIZATION_REQUEST_LIFECYCLES = {
    "QUEUED",
    "ACTIVE",
    "WAITING_INPUT",
    "WAITING_APPROVAL",
    "FINALIZING",
}


def _rule_debug_redaction_plan(value: dict[str, object]) -> None:
    _require(
        _sorted_unique(value["allowed_json_pointers"]),
        "DEBUG_REDACTION_POINTER_ORDER_INVALID",
        "$.allowed_json_pointers",
    )
    _require(
        _sorted_unique(value["rule_ids"]),
        "DEBUG_REDACTION_RULE_ORDER_INVALID",
        "$.rule_ids",
    )
    _require(
        _ordered_unique(value["debug_input_refs"], _ref_key),
        "DEBUG_REDACTION_INPUT_ORDER_INVALID",
        "$.debug_input_refs",
    )
    verify_content_ref_set(value["debug_input_refs"])


def _rule_publication_content_manifest(value: dict[str, object]) -> None:
    entries = value["entries"]
    _require(
        value["entry_count"] == len(entries),
        "PUBLICATION_ENTRY_COUNT_INVALID",
        "$.entry_count",
    )
    _require(
        _ordered_unique(entries, lambda item: item["json_pointer"]),
        "PUBLICATION_ENTRY_ORDER_INVALID",
        "$.entries",
    )
    for index, entry in enumerate(entries):
        receipt = entry["redaction_receipt"]
        receipt_path = f"$.entries[{index}].redaction_receipt"
        _require(
            receipt["policy_digest"] == value["redaction_policy_digest"],
            "PUBLICATION_REDACTION_POLICY_INVALID",
            f"{receipt_path}.policy_digest",
        )
        original_sha256 = (
            entry["source_ref"]["sha256"]
            if entry["content_kind"] == "artifact_bytes"
            else entry["inline_digest"]
        )
        _require(
            receipt["original_sha256"] == original_sha256,
            "PUBLICATION_REDACTION_SOURCE_INVALID",
            f"{receipt_path}.original_sha256",
        )


def _rule_terminalization_fact(value: dict[str, object]) -> None:
    reason_code = value["reason_code"]
    _require(
        reason_code in _TERMINALIZATION_REASON_CODES,
        "TERMINALIZATION_REASON_INVALID",
        "$.reason_code",
    )
    expected_key = (
        f"terminalize:{reason_code.lower()}:{value['observed_task_version']}"
    )
    _require(
        value["idempotency_key"] == expected_key,
        "TERMINALIZATION_IDEMPOTENCY_KEY_INVALID",
        "$.idempotency_key",
    )
    _require(
        value["source"]["kind"] in _TERMINALIZATION_CONTROL_ACTOR_KINDS,
        "TERMINALIZATION_ACTOR_INVALID",
        "$.source.kind",
    )
    evidence_refs = value["evidence_refs"]
    _require(
        _ordered_unique(evidence_refs, _ref_key),
        "TERMINALIZATION_EVIDENCE_ORDER_INVALID",
        "$.evidence_refs",
    )
    verify_content_ref_set(evidence_refs)
    if reason_code == "BUDGET_EXHAUSTED":
        _require(
            any(
                item["content_schema_id"] == "budget-summary/v1"
                for item in evidence_refs
            ),
            "TERMINALIZATION_BUDGET_EVIDENCE_REQUIRED",
            "$.evidence_refs",
        )


def verify_terminalization_fact_context(
    fact: object,
    task_id: str,
    current_task_version: int,
    lifecycle_state: str,
    existing_fact: object | None = None,
) -> dict[str, object]:
    """Bind a fact to task state and enforce exact idempotency retry bytes.

    The stable public signature is ``(fact, task_id, current_task_version,
    lifecycle_state, existing_fact=None)``. ``existing_fact`` is the fact
    previously accepted for the candidate idempotency key, when one exists.
    """
    validated = verify_document_digest("terminalization-fact/v1", fact)
    _require(
        isinstance(task_id, str) and validated["task_id"] == task_id,
        "TASK_ID_COLLISION",
        "$.task_id",
    )
    _require(
        isinstance(current_task_version, int)
        and not isinstance(current_task_version, bool)
        and 1 <= current_task_version <= SAFE_INTEGER
        and validated["observed_task_version"] == current_task_version,
        "TASK_VERSION_STALE",
        "$.observed_task_version",
    )
    _require(
        lifecycle_state in _TERMINALIZATION_REQUEST_LIFECYCLES,
        "STATE_TRANSITION_INVALID",
        "$.lifecycle_state",
    )
    if existing_fact is not None:
        existing = verify_document_digest(
            "terminalization-fact/v1", existing_fact
        )
        if existing["idempotency_key"] == validated["idempotency_key"]:
            _require(
                canonical_document_bytes(existing)
                == canonical_document_bytes(validated),
                "IDEMPOTENCY_CONFLICT",
                "$.idempotency_key",
            )
    return validated
'''


__all__ = ["PYTHON_GATE_PREPARATION"]
