from __future__ import annotations

from copy import deepcopy
import hashlib

from tests.agent_first_result_debug_transport_facade_support import canonical_bytes


_SELECTOR_FIELDS = (
    "input_digest",
    "predicate_registry_digest",
    "task_id",
    "task_version",
    "deletion_version",
    "profile",
    "gate_mode",
    "cancel_state",
    "effect_state",
    "cause_family",
    "delivery_state",
    "authoritative_fact_refs",
    "source_availability",
    "evidence_availability",
    "effect_availability",
    "predicate_results",
    "selected_lifecycle",
    "selected_outcome",
    "selected_reason",
)

_OUTCOME_AXES = {
    "COMPLETED": ("completed", "none", "none", "none", "safe_complete"),
    "NO_CHANGE_NEEDED": (
        "no_change_needed",
        "none",
        "none",
        "none",
        "safe_no_change",
    ),
    "COMPLETED_WITH_WAIVERS": (
        "completed_with_waivers",
        "none",
        "none",
        "none",
        "safe_complete_with_waivers",
    ),
    "PARTIAL": ("none", "none", "committed", "none", "safe_partial"),
    "BLOCKED": ("none", "none", "none", "input_required", "none"),
    "FAILED": ("none", "none", "none", "runtime_failure", "none"),
    "CANCELLED": ("none", "user_cancelled", "none", "none", "none"),
    "CANCELLED_WITH_EFFECTS": (
        "none",
        "user_cancelled",
        "committed",
        "none",
        "none",
    ),
    "TERMINATED_WITH_UNKNOWN_EFFECTS": (
        "none",
        "none",
        "unknown_post_deadline",
        "deadline_reached",
        "none",
    ),
}


def bind_task_result_to_terminal_decision(
    harness: object,
    task_result: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    result = deepcopy(task_result)
    decision = harness.fixture_document("gate_decision_golden_terminalization")
    axes = _OUTCOME_AXES[result["outcome"]]
    decision.update(
        {
            "task_id": result["task_id"],
            "task_version": result["published_from_version"],
            "gate_mode": axes[0],
            "cancel_state": axes[1],
            "effect_state": axes[2],
            "cause_family": axes[3],
            "delivery_state": axes[4],
            "selected_outcome": result["outcome"],
            "selected_reason": result["reason_code"],
        }
    )
    projection = {field: decision[field] for field in _SELECTOR_FIELDS}
    decision["selector_input_digest"] = hashlib.sha256(
        b"pullwise:terminal-selector-input:v1\0" + canonical_bytes(projection)
    ).hexdigest()
    decision = harness.reseal("gate-decision/v1", decision)
    result["selector_input_digest"] = decision["selector_input_digest"]
    result["gate_decision"] = {
        "availability": "available",
        "ref": harness.content_ref(
            "art_f0000000000000000000000000000001",
            "gate-decision/v1",
            decision,
        ),
    }
    return result, decision
