"""Cross-family publication gates for package-owned registries."""

from __future__ import annotations

import hashlib
import json

from .agent_first_contract_bundle_closure import validate_cross_family_closure


_RULE_KEYS = {
    "$schema",
    "$id",
    "$ref",
    "type",
    "additionalProperties",
    "required",
    "properties",
    "items",
    "const",
    "enum",
    "pattern",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "uniqueItems",
    "oneOf",
    "x-pullwise-digest",
    "x-pullwise-content-schema-id",
    "x-pullwise-content-schema-ids",
    "x-pullwise-availability-content-schema-id",
    "x-pullwise-availability-content-schema-ids",
    "x-pullwise-semantics",
}
_TYPES = {"object", "array", "string", "integer", "boolean", "null"}
DOCUMENT_RULE_IDS = frozenset(
    {
        "acceptance_source_ids_unique",
        "actor",
        "agent_claim_abandon_response",
        "agent_tool_request",
        "artifact_content_ref",
        "artifact_content_registry",
        "attempt_record",
        "attempt_state_nullability",
        "attempt_transport_binding_all_or_none",
        "availability_reason_registry",
        "availability_ref",
        "budget_ceiling_consistency",
        "budget_summary",
        "capability_and_delivery_sets_sorted_unique",
        "capability_sets_disjoint_sorted_unique",
        "change_set",
        "change_set_patch",
        "charter_digest_exact",
        "completion_proposal",
        "debug_redaction_plan",
        "derived_requirement_shape",
        "effect_ledger_snapshot",
        "effective_execution_policy",
        "elapsed_budget_ledger",
        "elapsed_budget_reservation",
        "elapsed_budget_settlement",
        "entries_normative_ingest_then_append_order",
        "evidence_closure_manifest",
        "execution_profile",
        "execution_state_manifest",
        "fenced_reason_ownership_loss",
        "gate_decision",
        "gate_input_snapshot",
        "gate_predicate_registry",
        "head_version_ref_pairs",
        "ledger_digest_exact",
        "local_tool_receipt",
        "observation",
        "observation_manifest",
        "owner_state_nullability",
        "policy_digest_exact",
        "pre_gate_evidence_closure_manifest",
        "pre_gate_root_set",
        "pre_verifier_observation_manifest",
        "publication_content_manifest",
        "quality_policy_plan",
        "r0_read_payload",
        "r0_read_result",
        "requirement_id_source_kind_match",
        "risk_ceiling_current_mvp",
        "root_and_origin_sets_sorted_unique",
        "sorted_unique_active_requirement_ids",
        "sorted_unique_charter_sets",
        "sorted_unique_requirement_links",
        "source_content",
        "source_selection_policy",
        "source_state",
        "source_tree_manifest",
        "task_owner",
        "task_record",
        "task_record_transport_binding_all_or_none",
        "task_report",
        "task_request",
        "task_result",
        "task_result_core",
        "task_result_outcome_reason_registry",
        "task_result_transport_ack",
        "task_result_transport_envelope",
        "terminal_result_shape",
        "terminalization_fact",
        "terminalization_input_snapshot",
        "tool_catalog",
        "tool_dispatch_capability",
        "tool_dispatch_intent",
        "tool_invocation",
        "utf8_nfc_byte_limits",
        "verification_attestation",
        "verification_attestation_manifest",
        "verifier_input_manifest",
        "verifier_work_report",
        "waiver_time_order",
        "worker_debug_descriptor",
        "worker_debug_file_manifest",
        "worker_debug_fragment",
        "worker_debug_redaction_report",
    }
)
CONTEXTUAL_HELPER_IDS = frozenset(
    {
        "evaluate_success_gate",
        "evaluate_terminalization_gate",
        "validate_attempt_transition",
        "validate_claim_write_set",
        "validate_effective_policy_derivation",
        "validate_tool_capability_consumption",
        "validate_tool_invocation_binding",
        "validate_tool_journal_begin",
        "validate_tool_journal_settlement",
        "validate_requirement_entry_ingest",
        "validate_requirement_ledger_transition",
        "validate_task_charter_transition",
        "validate_task_owner_transition",
        "validate_task_record_transition",
        "validate_task_request_acceptance",
        "validate_task_result_publication",
        "verify_attestation_context",
        "verify_attestation_manifest_context",
        "verify_budget_transition",
        "verify_change_set_context",
        "verify_completion_proposal_context",
        "verify_content_ref_content",
        "verify_evidence_closure_context",
        "verify_execution_state_context",
        "verify_observation_manifest_extension",
        "verify_pre_gate_evidence_closure_context",
        "verify_pre_gate_root_set_context",
        "verify_quality_policy_plan_context",
        "verify_source_tree_context",
        "verify_task_result_context",
        "verify_task_result_core",
        "verify_task_result_transport_ack",
        "verify_task_result_transport_envelope",
        "verify_terminalization_fact_context",
        "verify_terminalization_input_snapshot_context",
        "verify_gate_input_snapshot_context",
        "verify_verifier_input_context",
        "verify_verifier_work_context",
        "verify_waiver_event_authority",
        "verify_worker_debug_descriptor_content",
        "verify_worker_debug_fragment_content",
    }
)
WAIVER_SIGNATURE_CONTRACT = {
    "algorithm": "Ed25519",
    "domain": "pullwise-waiver-event/v1",
    "domain_separator": "NUL",
    "encoding": "base64url_no_padding",
    "signed_projection": "event_without_signature",
}


def validate_supported_schema(
    schema: dict[str, object], error_type: type[Exception], path: str = "$"
) -> None:
    unknown = sorted(set(schema).difference(_RULE_KEYS))
    if unknown:
        raise error_type(f"schema_keyword_unsupported: {path}: {unknown[0]}")
    declared = schema.get("type")
    if declared is not None:
        choices = declared if isinstance(declared, list) else [declared]
        if (
            not isinstance(choices, list)
            or not choices
            or any(item not in _TYPES for item in choices)
        ):
            raise error_type(f"schema_type_unsupported: {path}")
    if "pattern" in schema and not isinstance(schema["pattern"], str):
        raise error_type(f"schema_pattern_invalid: {path}")
    if "enum" in schema and (
        not isinstance(schema["enum"], list) or not schema["enum"]
    ):
        raise error_type(f"schema_enum_invalid: {path}")
    for key in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and (
            not isinstance(schema[key], int)
            or isinstance(schema[key], bool)
            or (
                key in {"minLength", "maxLength", "minItems", "maxItems"}
                and schema[key] < 0
            )
        ):
            raise error_type(f"schema_limit_invalid: {path}.{key}")
    if "required" in schema:
        required = schema["required"]
        if (
            not isinstance(required, list)
            or any(not isinstance(item, str) for item in required)
            or len(required) != len(set(required))
        ):
            raise error_type(f"schema_required_invalid: {path}")
    if "x-pullwise-digest" in schema:
        digest = schema["x-pullwise-digest"]
        if (
            not isinstance(digest, dict)
            or set(digest) != {"field", "domain"}
            or any(not isinstance(item, str) or not item for item in digest.values())
        ):
            raise error_type(f"schema_digest_invalid: {path}")
    if "x-pullwise-semantics" in schema:
        _validate_semantic_metadata(
            schema["x-pullwise-semantics"], error_type, path
        )
    if "additionalProperties" in schema and schema["additionalProperties"] is not False:
        raise error_type(f"schema_additional_properties_unsupported: {path}")
    properties = schema.get("properties", {})
    if properties:
        if not isinstance(properties, dict):
            raise error_type(f"schema_properties_invalid: {path}")
        for key, rule in properties.items():
            if not isinstance(rule, dict):
                raise error_type(f"schema_rule_invalid: {path}.{key}")
            validate_supported_schema(rule, error_type, f"{path}.{key}")
    if "items" in schema:
        items = schema["items"]
        if not isinstance(items, dict):
            raise error_type(f"schema_items_invalid: {path}")
        validate_supported_schema(items, error_type, f"{path}[]")
    if "oneOf" in schema:
        options = schema["oneOf"]
        if not isinstance(options, list) or not options:
            raise error_type(f"schema_one_of_invalid: {path}")
        for index, option in enumerate(options):
            if not isinstance(option, dict):
                raise error_type(f"schema_one_of_invalid: {path}")
            validate_supported_schema(option, error_type, f"{path}.oneOf[{index}]")


def _validate_semantic_metadata(
    metadata: object, error_type: type[Exception], path: str
) -> None:
    if not isinstance(metadata, dict):
        raise error_type(f"schema_semantics_shape_invalid: {path}")
    allowed = {"document_rules", "contextual_helpers", "signature_contract"}
    if set(metadata).difference(allowed) or not {
        "document_rules",
        "contextual_helpers",
    }.issubset(metadata):
        raise error_type(f"schema_semantics_shape_invalid: {path}")
    for key, supported in (
        ("document_rules", DOCUMENT_RULE_IDS),
        ("contextual_helpers", CONTEXTUAL_HELPER_IDS),
    ):
        values = metadata[key]
        if (
            not isinstance(values, list)
            or values != sorted(set(values))
            or any(not isinstance(item, str) for item in values)
        ):
            raise error_type(f"schema_semantics_order_invalid: {path}.{key}")
        unknown = set(values).difference(supported)
        if unknown:
            raise error_type(
                f"schema_semantics_unknown: {path}.{key}: {sorted(unknown)[0]}"
            )
    signature = metadata.get("signature_contract")
    if signature is not None and signature != WAIVER_SIGNATURE_CONTRACT:
        raise error_type(f"schema_signature_contract_invalid: {path}")
    if (
        not metadata["document_rules"]
        and not metadata["contextual_helpers"]
        and signature is None
    ):
        raise error_type(f"schema_semantics_empty: {path}")


def validate_semantic_registries(
    families: list[dict[str, object]],
    schema_owner: dict[str, str],
    error_type: type[Exception],
) -> None:
    schemas = {
        item["$id"]: item
        for family in families
        for item in family["schemas"]
    }
    fixtures = {
        item["fixture_id"]: item
        for family in families
        for item in family["fixtures"]
    }
    stable_registry = _sealed_document(
        schemas,
        fixtures,
        "stable-error-registry/v1",
        "error_golden_current_registry",
        error_type,
    )
    tool_catalog = _sealed_document(
        schemas,
        fixtures,
        "tool-catalog/v1",
        "tool_golden_current_catalog",
        error_type,
    )
    gate_registry = _sealed_document(
        schemas,
        fixtures,
        "gate-predicate-registry/v1",
        "gate_golden_independent_registry",
        error_type,
    )
    availability_registry = _sealed_document(
        schemas,
        fixtures,
        "availability-reason-registry/v1",
        "task_result_golden_availability_reason_registry",
        error_type,
    )
    outcome_registry = _sealed_document(
        schemas,
        fixtures,
        "task-result-outcome-reason-registry/v1",
        "task_result_golden_outcome_reason_registry",
        error_type,
    )
    artifact_registry = _sealed_document(
        schemas,
        fixtures,
        "artifact-content-registry/v1",
        "publication_golden_artifact_registry",
        error_type,
    )

    stable_codes = _unique_sorted(
        [entry["code"] for entry in stable_registry["entries"]],
        "stable_error_registry_duplicate",
        error_type,
    )
    stable_schema_codes = schemas["stable-error/v1"]["properties"]["code"]["enum"]
    registry_schema_codes = schemas["stable-error-registry/v1"][
        "properties"
    ]["entries"]["items"]["properties"]["code"]["enum"]
    if stable_codes != stable_schema_codes or stable_codes != registry_schema_codes:
        raise error_type("stable_error_registry_bijection_invalid")
    expected_codes = {
        item["expected_code"]
        for item in fixtures.values()
        if item["expected_code"] is not None
    }
    if not expected_codes.issubset(stable_codes):
        raise error_type("fixture_error_code_unregistered")

    validate_cross_family_closure(
        schemas,
        fixtures,
        availability_registry,
        outcome_registry,
        artifact_registry,
        error_type,
    )

    predicates = _unique_sorted(
        [entry["predicate_id"] for entry in gate_registry["predicates"]],
        "gate_predicate_registry_duplicate",
        error_type,
    )
    registry_predicates = schemas["gate-predicate-registry/v1"][
        "properties"
    ]["predicates"]["items"]["properties"]["predicate_id"]["enum"]
    decision_predicates = schemas["gate-decision/v1"]["properties"][
        "predicate_results"
    ]["items"]["properties"]["predicate_id"]["enum"]
    if predicates != registry_predicates or predicates != decision_predicates:
        raise error_type("gate_predicate_registry_bijection_invalid")
    for entry in gate_registry["predicates"]:
        if entry["failure_code"] not in stable_codes:
            raise error_type("gate_failure_code_unregistered")
        if any(item not in schema_owner for item in entry["input_schema_ids"]):
            raise error_type("gate_input_schema_unregistered")

    tool_keys = [entry["tool_key"] for entry in tool_catalog["tools"]]
    if len(tool_keys) != len(set(tool_keys)) or tool_keys != sorted(tool_keys):
        raise error_type("tool_catalog_registry_invalid")
    for entry in tool_catalog["tools"]:
        if any(
            entry[key]
            for key in (
                "uses_command",
                "uses_network",
                "uses_secret",
                "requests_approval",
            )
        ):
            raise error_type("r0_tool_control_fact_invalid")
        if entry["request_schema_id"] not in schema_owner:
            raise error_type("tool_request_schema_unregistered")
        if entry["result_schema_id"] not in schema_owner:
            raise error_type("tool_result_schema_unregistered")

    for schema in schemas.values():
        for rule in _walk(schema):
            annotations = (
                ("x-pullwise-content-schema-id", "x-pullwise-content-schema-ids", "content-ref/v1"),
                ("x-pullwise-availability-content-schema-id", "x-pullwise-availability-content-schema-ids", "availability-ref/v1"),
            )
            for singular, plural, base in annotations:
                target, targets = rule.get(singular), rule.get(plural)
                if target is None and targets is None:
                    continue
                if rule.get("$ref") != base:
                    raise error_type("typed_content_ref_base_invalid")
                expected = [target] if target is not None else targets
                if not isinstance(expected, list) or not expected:
                    raise error_type("typed_content_ref_target_invalid")
                if expected != sorted(set(expected)):
                    raise error_type("typed_content_ref_target_invalid")
                if any(item not in schema_owner for item in expected):
                    raise error_type("typed_content_ref_target_unregistered")


def _sealed_document(
    schemas: dict[str, dict[str, object]],
    fixtures: dict[str, dict[str, object]],
    schema_id: str,
    fixture_id: str,
    error_type: type[Exception],
) -> dict[str, object]:
    schema = schemas[schema_id]
    fixture = fixtures[fixture_id]
    if fixture["schema_id"] != schema_id or fixture["fixture_class"] != "golden":
        raise error_type(f"registry_fixture_identity_invalid: {fixture_id}")
    document = fixture["document"]
    spec = schema.get("x-pullwise-digest")
    if not isinstance(spec, dict):
        raise error_type(f"registry_digest_spec_missing: {schema_id}")
    field, domain = spec.get("field"), spec.get("domain")
    if not isinstance(field, str) or not isinstance(domain, str):
        raise error_type(f"registry_digest_spec_invalid: {schema_id}")
    presented = document.get(field)
    unsigned = {key: value for key, value in document.items() if key != field}
    expected = hashlib.sha256(
        domain.encode("utf-8") + b"\0" + _canonical_bytes(unsigned)
    ).hexdigest()
    if presented != expected:
        raise error_type(f"registry_fixture_digest_invalid: {fixture_id}")
    return document


def _unique_sorted(
    values: list[str], code: str, error_type: type[Exception]
) -> list[str]:
    if len(values) != len(set(values)) or values != sorted(values):
        raise error_type(code)
    return values


def _walk(value: object):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
