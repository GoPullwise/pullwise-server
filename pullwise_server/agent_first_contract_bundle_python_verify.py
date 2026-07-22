"""Generated Python facade inventory and bundle verification."""

from __future__ import annotations


PYTHON_VERIFY = r'''
_INTERNAL_CONSTRAINT_SCHEMA_IDS = {
    "task-result-blocked-variant/v1",
    "task-result-cancelled-variant/v1",
    "task-result-completed-variant/v1",
    "task-result-completed-with-waivers-variant/v1",
    "task-result-failed-variant/v1",
    "task-result-no-change-needed-variant/v1",
    "task-result-partial-variant/v1",
}


def _schema_role(schema_id: str) -> str:
    return (
        "internal_constraint"
        if schema_id in _INTERNAL_CONSTRAINT_SCHEMA_IDS
        else "public_document"
    )


def schema_ids() -> tuple[str, ...]:
    return tuple(
        item["schema_id"]
        for item in root_manifest()["schema_registry"]
        if item["role"] == "public_document"
    )


def all_schema_ids() -> tuple[str, ...]:
    return tuple(item["schema_id"] for item in root_manifest()["schema_registry"])


def tool_catalog() -> dict[str, object]:
    return verify_document_digest(
        "tool-catalog/v1", fixture("tool_golden_current_catalog")["document"]
    )


def gate_predicate_registry() -> dict[str, object]:
    return verify_document_digest(
        "gate-predicate-registry/v1",
        fixture("gate_golden_independent_registry")["document"],
    )


def stable_error_registry() -> dict[str, object]:
    return verify_document_digest(
        "stable-error-registry/v1",
        fixture("error_golden_current_registry")["document"],
    )


_SEMANTIC_CYCLE_EXCEPTIONS = [
    {
        "schema_id": "task-charter/v1",
        "kind": "content_ref_target",
        "path": "$.properties.previous_charter_ref.oneOf[0]",
        "target_schema_id": "task-charter/v1",
    },
    {
        "schema_id": "task-record/v1",
        "kind": "content_ref_target",
        "path": "$.properties.result_ref.oneOf[0]",
        "target_schema_id": "task-result/v1",
    },
]


def _schema_edges(value: object) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []

    def visit(item: object, path: str) -> None:
        if isinstance(item, dict):
            target = item.get("$ref")
            if isinstance(target, str):
                found.append(
                    {"kind": "schema_ref", "path": path, "target_schema_id": target}
                )
            annotations = (
                ("x-pullwise-content-schema-id", "x-pullwise-content-schema-ids", "content_ref_target"),
                ("x-pullwise-availability-content-schema-id", "x-pullwise-availability-content-schema-ids", "availability_ref_target"),
            )
            for singular, plural, kind in annotations:
                targets: list[object] = []
                if singular in item:
                    targets.append(item[singular])
                if plural in item and isinstance(item[plural], list):
                    targets.extend(item[plural])
                for annotated in targets:
                    if isinstance(annotated, str):
                        found.append(
                            {"kind": kind, "path": path, "target_schema_id": annotated}
                        )
            for key, child in item.items():
                visit(child, f"{path}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")

    visit(value, "$")
    unique = {canonical_document_bytes(item): item for item in found}
    return sorted(
        unique.values(),
        key=lambda item: (item["path"], item["kind"], item["target_schema_id"]),
    )


def _verify_schema_edge_dag(
    edges_by_schema: dict[str, list[dict[str, str]]]
) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(schema_id: str) -> None:
        if schema_id in visiting:
            _fail("CONTRACT_REFERENCE_CYCLE", schema_id)
        if schema_id in visited:
            return
        visiting.add(schema_id)
        for edge in edges_by_schema[schema_id]:
            exception = {"schema_id": schema_id, **edge}
            if exception not in _SEMANTIC_CYCLE_EXCEPTIONS:
                visit(edge["target_schema_id"])
        visiting.remove(schema_id)
        visited.add(schema_id)

    for schema_id in sorted(edges_by_schema):
        visit(schema_id)


def verify_bundle() -> bool:
    raw = bundle_bytes()
    if hashlib.sha256(raw).hexdigest() != CONTENT_SHA256:
        _fail("CONTRACT_BUNDLE_DIGEST_MISMATCH")
    document = bundle()
    if (
        document["package_identity"] != PACKAGE_IDENTITY
        or document["package_version"] != PACKAGE_VERSION
    ):
        _fail("CURRENT_PACKAGE_PIN_MISMATCH")
    root = document["root_manifest"]
    presented_root = root["root_sha256"]
    root_body = {key: value for key, value in root.items() if key != "root_sha256"}
    if presented_root != ROOT_SHA256 or canonical_document_sha256(root_body) != ROOT_SHA256:
        _fail("CONTRACT_ROOT_DIGEST_MISMATCH")
    if (
        root["semantic_cycle_exceptions"] != _SEMANTIC_CYCLE_EXCEPTIONS
        or root["semantic_cycle_exceptions_sha256"]
        != canonical_document_sha256(_SEMANTIC_CYCLE_EXCEPTIONS)
    ):
        _fail("CONTRACT_SEMANTIC_CYCLE_EXCEPTION_INVALID")
    family_ids = [family["family_id"] for family in document["families"]]
    if family_ids != root["required_families"]:
        _fail("CONTRACT_FAMILY_CLOSURE_INVALID")

    schemas, fixtures, family_entries = [], [], []
    known_schema_ids: set[str] = set()
    edges_by_schema: dict[str, list[dict[str, str]]] = {}
    for family in document["families"]:
        local_schemas, local_fixtures = [], []
        for item in family["schemas"]:
            edges = _schema_edges(item)
            entry = {
                "schema_id": item["$id"],
                "family_id": family["family_id"],
                "role": _schema_role(item["$id"]),
                "references": sorted({edge["target_schema_id"] for edge in edges}),
                "edges": edges,
                "sha256": canonical_document_sha256(item),
            }
            local_schemas.append(entry)
            known_schema_ids.add(item["$id"])
            edges_by_schema[item["$id"]] = edges
        for item in family["fixtures"]:
            local_fixtures.append(
                {
                    "fixture_id": item["fixture_id"],
                    "family_id": family["family_id"],
                    "schema_id": item["schema_id"],
                    "fixture_class": item["fixture_class"],
                    "expected_code": item["expected_code"],
                    "sha256": canonical_document_sha256(item),
                }
            )
        if local_schemas != family["schema_registry"]:
            _fail("CONTRACT_SCHEMA_REGISTRY_INVALID")
        if local_fixtures != family["fixture_registry"]:
            _fail("CONTRACT_FIXTURE_REGISTRY_INVALID")
        schemas.extend(local_schemas)
        fixtures.extend(local_fixtures)
        family_entries.append(
            {
                "family_id": family["family_id"],
                "schema_ids": [item["$id"] for item in family["schemas"]],
                "fixture_ids": [item["fixture_id"] for item in family["fixtures"]],
                "sha256": canonical_document_sha256(family),
            }
        )
    if schemas != root["schema_registry"] or fixtures != root["fixture_registry"]:
        _fail("CONTRACT_ROOT_REGISTRY_INVALID")
    if family_entries != root["families"]:
        _fail("CONTRACT_FAMILY_DIGEST_INVALID")
    if any(
        ref not in known_schema_ids for item in schemas for ref in item["references"]
    ):
        _fail("CONTRACT_REFERENCE_UNKNOWN")
    _verify_schema_edge_dag(edges_by_schema)
    expected_dag = sorted(
        (
            {
                "schema_id": item["schema_id"],
                "family_id": item["family_id"],
                "references": item["references"],
                "edges": item["edges"],
            }
            for item in schemas
        ),
        key=lambda item: item["schema_id"],
    )
    if expected_dag != root["reference_dag"]:
        _fail("CONTRACT_REFERENCE_DAG_INVALID")
    classes = {item["fixture_class"] for item in fixtures}
    if classes != set(root["fixture_classes"]):
        _fail("CONTRACT_FIXTURE_CLASS_INVALID")
    validate_document(
        "canonical-json-profile/v1",
        fixture("core_golden_canonical_profile")["document"],
    )
    tool_catalog()
    gate_predicate_registry()
    stable_error_registry()
    return True


def assert_pin(
    identity: str,
    version: str,
    content_sha256: str,
    root_sha256: str | None = None,
) -> None:
    if (identity, version, content_sha256) != PACKAGE_TUPLE[:3]:
        raise RuntimeError("CURRENT_PACKAGE_PIN_MISMATCH")
    if root_sha256 is not None and root_sha256 != ROOT_SHA256:
        raise RuntimeError("CURRENT_PACKAGE_PIN_MISMATCH")


__all__ = [
    "BUNDLE_BASE64", "CONTENT_SHA256", "ContractValidationError",
    "PACKAGE_IDENTITY", "PACKAGE_TUPLE", "PACKAGE_VERSION", "ROOT_SHA256",
    "all_schema_ids", "assert_pin", "bundle", "bundle_bytes", "canonical_document_bytes",
    "canonical_document_sha256", "canonical_validated_bytes", "document_digest",
    "validate_tool_capability_consumption", "validate_tool_invocation_binding",
    "validate_tool_journal_begin", "validate_tool_journal_settlement",
    "evaluate_success_gate", "evaluate_terminalization_gate",
    "fixture", "fixture_bytes", "gate_predicate_registry", "package_tuple",
    "root_manifest", "root_manifest_bytes", "schema", "schema_bytes", "schema_ids",
    "seal_document", "stable_error_registry", "tool_catalog", "validate_document",
    "verify_budget_transition", "verify_bundle", "verify_content_ref_set",
    "verify_document_digest", "verify_evidence_closure_context",
    "verify_quality_policy_plan_context",
    "verify_gate_input_snapshot_context", "verify_pre_gate_evidence_closure_context",
    "verify_pre_gate_root_set_context", "verify_terminalization_fact_context",
    "verify_terminalization_input_snapshot_context", "verify_waiver_authorization",
]
'''


__all__ = ["PYTHON_VERIFY"]
