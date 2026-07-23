"""Generated Python facade semantics for PreGate documents."""

from __future__ import annotations


PYTHON_PRE_GATE = r'''
_PRE_GATE_ROOT_FIELDS = (
    "request",
    "policy",
    "charter",
    "ledger",
    "waiver_events",
    "proposal",
    "original_source",
    "final_source",
    "execution_states",
    "change_set",
    "pre_observation_manifest",
    "final_observation_manifest",
    "verifier_inputs",
    "verifier_work",
    "attestations",
    "artifacts",
    "report",
    "effect_ledger",
    "budget_summary",
    "termination_facts",
    "publication_content_manifest",
    "debug_redaction_plan",
)
_PRE_GATE_ROOT_KEYS = {
    "schema_id",
    "task_id",
    "root_set_digest",
    *_PRE_GATE_ROOT_FIELDS,
}
_PRE_GATE_ALWAYS_AVAILABLE = (
    "request",
    "policy",
    "ledger",
    "effect_ledger",
    "budget_summary",
    "publication_content_manifest",
    "debug_redaction_plan",
)
_PRE_GATE_SUCCESS_OUTCOMES = {
    "COMPLETED",
    "COMPLETED_WITH_WAIVERS",
    "NO_CHANGE_NEEDED",
}
_PRE_GATE_SUCCESS_AVAILABLE = (
    "charter",
    "proposal",
    "original_source",
    "final_source",
    "pre_observation_manifest",
    "final_observation_manifest",
    "attestations",
    "report",
)
_PRE_GATE_PARTIAL_AVAILABLE = (
    "proposal",
    "original_source",
    "final_source",
    "final_observation_manifest",
    "report",
)
_PRE_GATE_TERMINAL_OUTCOMES = {"BLOCKED", "CANCELLED", "FAILED"}
_PRE_GATE_FORBIDDEN_CLOSURE_TARGETS = {
    "error-response/v1",
    "evidence-closure-manifest/v1",
    "gate-decision/v1",
    "gate-input-snapshot/v1",
    "server-debug-assembly/v1",
    "server-debug-snapshot/v1",
    "task-result-core/v1",
    "task-result/v1",
    "terminalization-input-snapshot/v1",
    "worker-debug-fragment/v1",
}


def _availability(value: dict[str, object]) -> str:
    return value["availability"]


def _rule_pre_gate_root_set(value: dict[str, object]) -> None:
    _require(
        set(value) == _PRE_GATE_ROOT_KEYS,
        "PRE_GATE_ROOT_FIELDS_INVALID",
    )
    for field in _PRE_GATE_ALWAYS_AVAILABLE:
        _require(
            _availability(value[field]) == "available",
            "PRE_GATE_REQUIRED_ROOT_UNAVAILABLE",
            f"$.{field}",
        )

def _rule_pre_gate_evidence_closure_manifest(
    value: dict[str, object],
) -> None:
    entries = value["entries"]
    _require(
        value["entry_count"] == len(entries),
        "PRE_GATE_CLOSURE_ENTRY_COUNT_INVALID",
        "$.entry_count",
    )
    _require(
        _ordered_unique(entries, _ref_key),
        "PRE_GATE_CLOSURE_ENTRY_ORDER_INVALID",
        "$.entries",
    )
    verify_content_ref_set(entries)
    _require(
        value["pre_gate_root_set_ref"] in entries,
        "PRE_GATE_CLOSURE_ROOT_MISSING",
        "$.entries",
    )
    _require(
        not _PRE_GATE_FORBIDDEN_CLOSURE_TARGETS.intersection(
            item["content_schema_id"] for item in entries
        ),
        "PRE_GATE_CLOSURE_DIRECTION_INVALID",
        "$.entries",
    )
    expected_digest = hashlib.sha256(
        canonical_document_bytes(entries)
    ).hexdigest()
    _require(
        value["pre_gate_closure_digest"] == expected_digest,
        "PRE_GATE_CLOSURE_DIGEST_INVALID",
        "$.pre_gate_closure_digest",
    )


def _content_ref_matches_direct_document(
    ref: dict[str, object],
    schema_id: str,
    document: dict[str, object],
) -> bool:
    raw = canonical_document_bytes(document)
    return (
        ref["content_schema_id"] == schema_id
        and ref["sha256"] == hashlib.sha256(raw).hexdigest()
        and ref["size_bytes"] == len(raw)
        and ref["media_type"] == "application/json"
        and ref["encoding"] == "utf-8"
    )


def _pre_gate_available_root_refs(
    root_set: dict[str, object],
) -> list[dict[str, object]]:
    refs: list[dict[str, object]] = []
    for field in _PRE_GATE_ROOT_FIELDS:
        value = root_set[field]
        values = value if isinstance(value, list) else [value]
        refs.extend(
            item["ref"]
            for item in values
            if item["availability"] == "available"
        )
    return refs


def verify_pre_gate_root_set_context(
    root_set: object,
    task_id: str,
) -> dict[str, object]:
    """Verify an outcome-neutral root set against direct task context."""
    validated = verify_document_digest("pre-gate-root-set/v1", root_set)
    _require(
        isinstance(task_id, str) and validated["task_id"] == task_id,
        "PRE_GATE_TASK_BINDING_INVALID",
        "$.task_id",
    )
    return validated


def verify_pre_gate_evidence_closure_context(
    manifest: object,
    root_set: object,
) -> dict[str, object]:
    """Bind a PreGate closure to the direct root-set document."""
    validated = verify_document_digest(
        "pre-gate-evidence-closure-manifest/v1", manifest
    )
    roots = verify_document_digest("pre-gate-root-set/v1", root_set)
    _require(
        validated["task_id"] == roots["task_id"],
        "EVIDENCE_CLOSURE_INVALID",
        "$.task_id",
    )
    _require(
        _content_ref_matches_direct_document(
            validated["pre_gate_root_set_ref"],
            "pre-gate-root-set/v1",
            roots,
        ),
        "CAS_CORRUPT",
        "$.pre_gate_root_set_ref",
    )
    entry_bytes = {
        canonical_document_bytes(item) for item in validated["entries"]
    }
    _require(
        all(
            canonical_document_bytes(item) in entry_bytes
            for item in _pre_gate_available_root_refs(roots)
        ),
        "EVIDENCE_CLOSURE_INVALID",
        "$.entries",
    )
    return validated
'''


__all__ = ["PYTHON_PRE_GATE"]
