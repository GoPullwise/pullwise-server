"""Dispatch declared document semantics in generated Python wrappers."""

from __future__ import annotations


PYTHON_DISPATCH = r'''
_DOCUMENT_RULE_HANDLERS = {
    "acceptance_source_ids_unique": _rule_task_request_acceptance_sources,
    "actor": _rule_actor,
    "budget_ceiling_consistency": _rule_policy_budget_ceilings,
    "capability_and_delivery_sets_sorted_unique": _rule_task_request_sets,
    "capability_sets_disjoint_sorted_unique": _rule_policy_capability_sets,
    "change_set_patch": _rule_change_set_patch,
    "debug_redaction_plan": _rule_debug_redaction_plan,
    "evidence_closure_manifest": _rule_evidence_closure_manifest,
    "execution_profile": _rule_execution_profile,
    "gate_decision": _rule_gate_decision,
    "gate_input_snapshot": _rule_gate_input_snapshot,
    "gate_predicate_registry": _rule_gate_predicate_registry,
    "observation": _rule_observation,
    "policy_digest_exact": _rule_policy_digest,
    "pre_gate_evidence_closure_manifest": _rule_pre_gate_evidence_closure_manifest,
    "pre_gate_root_set": _rule_pre_gate_root_set,
    "publication_content_manifest": _rule_publication_content_manifest,
    "quality_policy_plan": _rule_quality_policy_plan,
    "risk_ceiling_current_mvp": _rule_policy_risk_ceiling,
    "root_and_origin_sets_sorted_unique": _rule_policy_roots_and_origins,
    "terminalization_fact": _rule_terminalization_fact,
    "terminalization_input_snapshot": _rule_terminalization_input_snapshot,
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
