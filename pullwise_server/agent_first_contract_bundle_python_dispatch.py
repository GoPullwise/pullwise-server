"""Dispatch declared document semantics in generated Python wrappers."""

from __future__ import annotations


PYTHON_DISPATCH = r'''
_DOCUMENT_RULE_HANDLERS = {
    "acceptance_source_ids_unique": _task_control_rule_request_acceptance_sources,
    "actor": _rule_actor,
    "agent_tool_request": _rule_agent_tool_request,
    "artifact_content_ref": _rule_artifact_content_ref,
    "artifact_content_registry": _rule_artifact_content_registry,
    "attempt_state_nullability": _task_control_rule_attempt_nullability,
    "attempt_transport_binding_all_or_none": _task_control_rule_attempt_transport,
    "availability_reason_registry": _rule_availability_reason_registry,
    "availability_ref": _rule_availability_ref,
    "benchmark_bundle": _rule_benchmark_bundle,
    "budget_ceiling_consistency": _task_control_rule_policy_budgets,
    "budget_summary": _rule_budget_summary,
    "capability_and_delivery_sets_sorted_unique": _task_control_rule_request_sets,
    "capability_sets_disjoint_sorted_unique": _task_control_rule_policy_capabilities,
    "change_set": _rule_change_set_complete,
    "change_set_patch": _rule_change_set_patch,
    "completion_proposal": _rule_completion_proposal,
    "charter_digest_exact": _task_control_rule_charter_digest,
    "debug_redaction_plan": _rule_debug_redaction_plan,
    "derived_requirement_shape": _task_control_rule_requirement_shape,
    "effect_ledger_snapshot": _rule_effect_ledger_snapshot,
    "elapsed_budget_ledger": _rule_elapsed_budget_ledger,
    "elapsed_budget_reservation": _rule_elapsed_budget_reservation,
    "elapsed_budget_settlement": _rule_elapsed_budget_settlement,
    "entries_normative_ingest_then_append_order": _task_control_rule_ledger_entries,
    "evidence_closure_manifest": _rule_evidence_closure_manifest,
    "execution_profile": _rule_execution_profile,
    "execution_state_manifest": _rule_execution_state_manifest,
    "fenced_reason_ownership_loss": _task_control_rule_fenced_reason,
    "gate_decision": _rule_gate_decision,
    "gate_input_snapshot": _rule_gate_input_snapshot,
    "gate_predicate_registry": _rule_gate_predicate_registry,
    "head_version_ref_pairs": _task_control_rule_record_heads,
    "ledger_digest_exact": _task_control_rule_ledger_digest,
    "local_tool_receipt": _rule_local_tool_receipt,
    "observation": _rule_observation,
    "observation_manifest": _rule_observation_manifest_complete,
    "owner_state_nullability": _task_control_rule_owner_nullability,
    "policy_digest_exact": _task_control_rule_policy_digest,
    "pre_gate_evidence_closure_manifest": _rule_pre_gate_evidence_closure_manifest,
    "pre_gate_root_set": _rule_pre_gate_root_set,
    "pre_verifier_observation_manifest": _rule_pre_verifier_observation_manifest,
    "publication_content_manifest": _rule_publication_content_manifest,
    "quality_policy_plan": _rule_quality_policy_plan,
    "r0_read_payload": _rule_r0_read_payload,
    "r0_read_result": _rule_r0_read_result,
    "requirement_id_source_kind_match": _task_control_rule_requirement_id,
    "release_principal": _rule_release_principal,
    "release_gate_attestation": _rule_release_gate_attestation,
    "release_gate_policy": _rule_release_gate_policy,
    "release_gate_report": _rule_release_gate_report,
    "risk_ceiling_current_mvp": _task_control_rule_policy_mvp,
    "root_and_origin_sets_sorted_unique": _task_control_rule_policy_roots,
    "server_authority_envelope": _rule_server_authority_envelope,
    "sorted_unique_active_requirement_ids": _task_control_rule_ledger_active,
    "sorted_unique_charter_sets": _task_control_rule_charter_sets,
    "sorted_unique_requirement_links": _task_control_rule_requirement_links,
    "source_content": _rule_source_content,
    "source_selection_policy": _rule_source_selection_policy_complete,
    "source_tree_manifest": _rule_source_tree_manifest,
    "source_state": _rule_source_state,
    "task_record_transport_binding_all_or_none": _task_control_rule_record_transport,
    "task_report": _rule_task_report,
    "task_result": _rule_task_result_complete,
    "task_result_core": _rule_task_result_core,
    "task_result_outcome_reason_registry": _rule_task_result_outcome_reason_registry,
    "task_result_transport_ack": _rule_task_result_transport_ack,
    "task_result_transport_envelope": _rule_task_result_transport_envelope,
    "terminal_result_shape": _task_control_rule_record_terminal,
    "terminalization_fact": _rule_terminalization_fact,
    "terminalization_input_snapshot": _rule_terminalization_input_snapshot,
    "tool_catalog": _rule_tool_catalog,
    "tool_dispatch_capability": _rule_tool_dispatch_capability,
    "tool_dispatch_intent": _rule_tool_dispatch_intent,
    "tool_invocation": _rule_tool_invocation,
    "transport_abandonment_record": _rule_transport_abandonment_record,
    "utf8_nfc_byte_limits": _task_control_rule_utf8,
    "verification_attestation": _rule_attestation,
    "verification_attestation_manifest": _rule_attestation_manifest,
    "verifier_input_manifest": _rule_verifier_input,
    "verifier_work_report": _rule_verifier_work,
    "waiver_time_order": _task_control_rule_waiver_time,
    "worker_debug_descriptor": _rule_worker_debug_descriptor,
    "worker_debug_file_manifest": _rule_worker_debug_file_manifest,
    "worker_debug_fragment": _rule_worker_debug_fragment,
    "worker_debug_redaction_report": _rule_worker_debug_redaction_report,
}


def _validate_semantics(
    schema_id: str, value: dict[str, object]
) -> None:
    semantics = schema(schema_id).get("x-pullwise-semantics")
    if semantics is None:
        return
    expected_keys = {"document_rules", "contextual_helpers"}
    signature_contracts = {
        "waiver-event/v1": {
            "algorithm": "Ed25519",
            "domain": "pullwise-waiver-event/v1",
            "domain_separator": "NUL",
            "encoding": "base64url_no_padding",
            "signed_projection": "event_without_signature",
        },
        "release-principal/v1": {
            "algorithm": "Ed25519",
            "domain": "pullwise-release-principal/v1",
            "domain_separator": "NUL",
            "encoding": "base64url_no_padding",
            "signed_projection": "document_without_signature_and_digest",
        },
    }
    if schema_id in signature_contracts:
        expected_keys.add("signature_contract")
    if not isinstance(semantics, dict) or set(semantics) != expected_keys:
        _fail("CONTRACT_SEMANTICS_INVALID", schema_id)
    if (
        schema_id in signature_contracts
        and semantics["signature_contract"] != signature_contracts[schema_id]
    ):
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
