"""Cross-family publication gates for package-owned registries."""

from __future__ import annotations

import hashlib
import json


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
}
_TYPES = {"object", "array", "string", "integer", "boolean", "null"}


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
            target = rule.get("x-pullwise-content-schema-id")
            targets = rule.get("x-pullwise-content-schema-ids")
            if target is None and targets is None:
                continue
            if rule.get("$ref") != "content-ref/v1":
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
