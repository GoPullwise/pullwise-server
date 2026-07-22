"""Dispatch declared document semantics in generated Python wrappers."""

from __future__ import annotations


PYTHON_DISPATCH = r'''
_DOCUMENT_RULE_HANDLERS = {
    "acceptance_source_ids_unique": _rule_task_request_acceptance_sources,
    "actor": _rule_actor,
    "agent_tool_request": _rule_agent_tool_request,
    "artifact_content_ref": _rule_artifact_content_ref,
    "artifact_content_registry": _rule_artifact_content_registry,
    "budget_ceiling_consistency": _rule_policy_budget_ceilings,
    "budget_summary": _rule_budget_summary,
    "capability_and_delivery_sets_sorted_unique": _rule_task_request_sets,
    "capability_sets_disjoint_sorted_unique": _rule_policy_capability_sets,
    "change_set_patch": _rule_change_set_patch,
    "debug_redaction_plan": _rule_debug_redaction_plan,
    "effect_ledger_snapshot": _rule_effect_ledger_snapshot,
    "elapsed_budget_ledger": _rule_elapsed_budget_ledger,
    "elapsed_budget_reservation": _rule_elapsed_budget_reservation,
    "elapsed_budget_settlement": _rule_elapsed_budget_settlement,
    "evidence_closure_manifest": _rule_evidence_closure_manifest,
    "execution_profile": _rule_execution_profile,
    "gate_decision": _rule_gate_decision,
    "gate_input_snapshot": _rule_gate_input_snapshot,
    "gate_predicate_registry": _rule_gate_predicate_registry,
    "local_tool_receipt": _rule_local_tool_receipt,
    "observation": _rule_observation,
    "policy_digest_exact": _rule_policy_digest,
    "pre_gate_evidence_closure_manifest": _rule_pre_gate_evidence_closure_manifest,
    "pre_gate_root_set": _rule_pre_gate_root_set,
    "publication_content_manifest": _rule_publication_content_manifest,
    "quality_policy_plan": _rule_quality_policy_plan,
    "r0_read_payload": _rule_r0_read_payload,
    "r0_read_result": _rule_r0_read_result,
    "risk_ceiling_current_mvp": _rule_policy_risk_ceiling,
    "root_and_origin_sets_sorted_unique": _rule_policy_roots_and_origins,
    "source_content": _rule_source_content,
    "source_state": _rule_source_state,
    "task_report": _rule_task_report,
    "terminalization_fact": _rule_terminalization_fact,
    "terminalization_input_snapshot": _rule_terminalization_input_snapshot,
    "tool_catalog": _rule_tool_catalog,
    "tool_dispatch_capability": _rule_tool_dispatch_capability,
    "tool_dispatch_intent": _rule_tool_dispatch_intent,
    "tool_invocation": _rule_tool_invocation,
    "utf8_nfc_byte_limits": _utf8_fields,
}


def _validate_semantics(
    schema_id: str, value: dict[str, object]
) -> None:
    semantics = schema(schema_id).get("x-pullwise-semantics")
    if semantics is None:
        return
    if not isinstance(semantics, dict) or set(semantics) != {
        "document_rules",
        "contextual_helpers",
    }:
        _fail("CONTRACT_SEMANTICS_INVALID", schema_id)
    rules = semantics["document_rules"]
    helpers = semantics["contextual_helpers"]
    if (
        not isinstance(rules, list)
        or not rules
        or not all(isinstance(item, str) for item in rules)
        or len(rules) != len(set(rules))
        or rules != sorted(rules)
        or not isinstance(helpers, list)
        or not all(isinstance(item, str) for item in helpers)
        or len(helpers) != len(set(helpers))
        or helpers != sorted(helpers)
    ):
        _fail("CONTRACT_SEMANTICS_INVALID", schema_id)
    for rule_id in rules:
        handler = _DOCUMENT_RULE_HANDLERS.get(rule_id)
        if handler is None:
            _fail("CONTRACT_SEMANTIC_RULE_UNIMPLEMENTED", rule_id)
        handler(value)
'''


__all__ = ["PYTHON_DISPATCH"]
