"""Cross-family closure checks for package-owned sealed registries."""

from __future__ import annotations

import hashlib
import json

from .agent_first_contract_bundle_closure import validate_cross_family_closure


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
    stable_schema_codes = schemas["stable-error/v1"]["properties"]["code"][
        "enum"
    ]
    registry_schema_codes = schemas["stable-error-registry/v1"]["properties"][
        "entries"
    ]["items"]["properties"]["code"]["enum"]
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

    validate_gate_predicate_registry(
        schemas,
        gate_registry,
        set(stable_codes),
        schema_owner,
        error_type,
    )

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
                (
                    "x-pullwise-content-schema-id",
                    "x-pullwise-content-schema-ids",
                    "content-ref/v1",
                ),
                (
                    "x-pullwise-availability-content-schema-id",
                    "x-pullwise-availability-content-schema-ids",
                    "availability-ref/v1",
                ),
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


def validate_gate_predicate_registry(
    schemas: dict[str, dict[str, object]],
    gate_registry: dict[str, object],
    stable_codes: set[str],
    schema_owner: dict[str, str],
    error_type: type[Exception],
) -> None:
    entries = gate_registry["predicates"]
    predicate_ids = [entry["predicate_id"] for entry in entries]
    if len(predicate_ids) != len(set(predicate_ids)):
        raise error_type("gate_predicate_registry_duplicate")
    registry_properties = schemas["gate-predicate-registry/v1"]["properties"][
        "predicates"
    ]["items"]["properties"]
    registry_predicates = registry_properties["predicate_id"]["enum"]
    decision_properties = schemas["gate-decision/v1"]["properties"][
        "predicate_results"
    ]["items"]["properties"]
    decision_predicates = decision_properties["predicate_id"]["enum"]
    if (
        predicate_ids != registry_predicates
        or predicate_ids != decision_predicates
    ):
        raise error_type("gate_predicate_registry_bijection_invalid")

    consumed_codes: set[str] = set()
    for entry in entries:
        failure_codes = entry["failure_codes"]
        if (
            not failure_codes
            or failure_codes != sorted(set(failure_codes))
        ):
            raise error_type("gate_failure_code_order_invalid")
        if any(code not in stable_codes for code in failure_codes):
            raise error_type("gate_failure_code_unregistered")
        consumed_codes.update(failure_codes)
        input_schema_ids = entry["input_schema_ids"]
        if input_schema_ids != sorted(set(input_schema_ids)):
            raise error_type("gate_input_schema_order_invalid")
        if any(item not in schema_owner for item in input_schema_ids):
            raise error_type("gate_input_schema_unregistered")

    registry_codes = registry_properties["failure_codes"]["items"]["enum"]
    decision_codes = decision_properties["failure_code"]["enum"]
    if (
        sorted(consumed_codes) != registry_codes
        or [None, *registry_codes] != decision_codes
    ):
        raise error_type("gate_failure_code_coverage_invalid")


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
