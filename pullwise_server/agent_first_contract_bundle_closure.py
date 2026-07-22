"""Cross-family closure gates for the current Agent-First contract."""

from __future__ import annotations


def validate_cross_family_closure(
    schemas: dict[str, dict[str, object]],
    fixtures: dict[str, dict[str, object]],
    availability_registry: dict[str, object],
    outcome_registry: dict[str, object],
    artifact_registry: dict[str, object],
    error_type: type[Exception],
) -> None:
    _validate_reason_registries(
        schemas,
        fixtures,
        availability_registry,
        outcome_registry,
        error_type,
    )
    _validate_artifact_registry(schemas, artifact_registry, error_type)
    _validate_task_result_variants(schemas, error_type)


def _validate_reason_registries(
    schemas: dict[str, dict[str, object]],
    fixtures: dict[str, dict[str, object]],
    availability_registry: dict[str, object],
    outcome_registry: dict[str, object],
    error_type: type[Exception],
) -> None:
    availability_reasons = _unique_sorted(
        availability_registry["reasons"],
        "availability_reason_registry_order_invalid",
        error_type,
    )
    availability_schema = schemas["availability-ref/v1"]
    branch_enums = [
        option["properties"]["reason_code"]["enum"]
        for option in availability_schema["oneOf"]
        if "reason_code" in option.get("properties", {})
    ]
    if not branch_enums or any(item != availability_reasons for item in branch_enums):
        raise error_type("availability_reason_registry_bijection_invalid")
    used_fixture_reasons: set[str] = set()
    for fixture in fixtures.values():
        for item in _walk(fixture["document"]):
            if (
                isinstance(item, dict)
                and item.get("availability") in {"unavailable", "not_applicable"}
                and isinstance(item.get("reason_code"), str)
            ):
                used_fixture_reasons.add(item["reason_code"])
    if not used_fixture_reasons.issubset(availability_reasons):
        raise error_type("availability_reason_fixture_unregistered")

    outcome_reasons = _unique_sorted(
        outcome_registry["reasons"],
        "task_result_outcome_reason_registry_order_invalid",
        error_type,
    )
    variant_ids = (
        "task-result-completed-variant/v1",
        "task-result-no-change-needed-variant/v1",
        "task-result-completed-with-waivers-variant/v1",
        "task-result-partial-variant/v1",
        "task-result-blocked-variant/v1",
        "task-result-failed-variant/v1",
        "task-result-cancelled-variant/v1",
    )
    declared: set[str] = set()
    for schema_id in variant_ids:
        rule = schemas[schema_id]["properties"]["reason_code"]
        if "const" in rule:
            declared.add(rule["const"])
        else:
            declared.update(rule["enum"])
    if sorted(declared) != outcome_reasons:
        raise error_type("task_result_outcome_reason_registry_bijection_invalid")


def _validate_artifact_registry(
    schemas: dict[str, dict[str, object]],
    registry: dict[str, object],
    error_type: type[Exception],
) -> None:
    entries = registry["entries"]
    keys = [item["artifact_kind"] for item in entries]
    if keys != sorted(set(keys)):
        raise error_type("artifact_content_registry_order_invalid")
    schema_ids = [item["content_schema_id"] for item in entries]
    if len(schema_ids) != len(set(schema_ids)):
        raise error_type("artifact_content_registry_schema_duplicate")
    artifact_ref = schemas["artifact-content-ref/v1"]["properties"]
    registry_item = schemas["artifact-content-registry/v1"]["properties"][
        "entries"
    ]["items"]["properties"]
    if (
        artifact_ref["artifact_kind"]["enum"] != keys
        or artifact_ref["ref"]["x-pullwise-content-schema-ids"]
        != sorted(schema_ids)
        or registry_item["artifact_kind"]["enum"] != keys
        or registry_item["content_schema_id"]["enum"] != sorted(schema_ids)
    ):
        raise error_type("artifact_content_registry_bijection_invalid")
    for entry in entries:
        if entry["media_type"] != "application/json" or entry["encoding"] != "utf-8":
            raise error_type("artifact_content_registry_tuple_invalid")
    for schema in schemas.values():
        for rule in _walk(schema):
            if not isinstance(rule, dict):
                continue
            properties = rule.get("properties")
            if not isinstance(properties, dict) or "artifact_refs" not in properties:
                continue
            artifact_refs = properties["artifact_refs"]
            if (
                not isinstance(artifact_refs, dict)
                or artifact_refs.get("type") != "array"
                or artifact_refs.get("items") != {"$ref": "artifact-content-ref/v1"}
            ):
                raise error_type("artifact_refs_shape_invalid")


def _validate_task_result_variants(
    schemas: dict[str, dict[str, object]], error_type: type[Exception]
) -> None:
    expected_order = [
        "task-result-completed-variant/v1",
        "task-result-no-change-needed-variant/v1",
        "task-result-completed-with-waivers-variant/v1",
        "task-result-partial-variant/v1",
        "task-result-blocked-variant/v1",
        "task-result-failed-variant/v1",
        "task-result-cancelled-variant/v1",
    ]
    main = schemas["task-result/v1"]
    core = schemas["task-result-core/v1"]
    main_keys = set(main["properties"])
    if len(main_keys) != 37 or set(core["properties"]) != main_keys:
        raise error_type("task_result_core_property_parity_invalid")
    for schema_id in expected_order:
        variant = schemas[schema_id]
        if (
            set(variant["properties"]) != main_keys
            or set(variant["required"]) != main_keys
            or variant.get("additionalProperties") is not False
        ):
            raise error_type(
                f"task_result_variant_property_parity_invalid: {schema_id}"
            )
    expected_refs = [{"$ref": item} for item in expected_order]
    if main.get("oneOf") != expected_refs or core.get("oneOf") != expected_refs:
        raise error_type("task_result_variant_order_invalid")


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


__all__ = ["validate_cross_family_closure"]
