"""Generated Python facade inventory and bundle verification."""

from __future__ import annotations


PYTHON_VERIFY = r'''
def schema_ids() -> tuple[str, ...]:
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


def _references(value: object) -> set[str]:
    if isinstance(value, dict):
        found = {
            item for key, item in value.items() if key == "$ref" and isinstance(item, str)
        }
        for item in value.values():
            found.update(_references(item))
        return found
    if isinstance(value, list):
        found: set[str] = set()
        for item in value:
            found.update(_references(item))
        return found
    return set()


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
    family_ids = [family["family_id"] for family in document["families"]]
    if family_ids != root["required_families"]:
        _fail("CONTRACT_FAMILY_CLOSURE_INVALID")

    schemas, fixtures, family_entries = [], [], []
    known_schema_ids: set[str] = set()
    for family in document["families"]:
        local_schemas, local_fixtures = [], []
        for item in family["schemas"]:
            entry = {
                "schema_id": item["$id"],
                "family_id": family["family_id"],
                "references": sorted(_references(item)),
                "sha256": canonical_document_sha256(item),
            }
            local_schemas.append(entry)
            known_schema_ids.add(item["$id"])
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
    expected_dag = sorted(
        (
            {
                "schema_id": item["schema_id"],
                "family_id": item["family_id"],
                "references": item["references"],
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
    "assert_pin", "bundle", "bundle_bytes", "canonical_document_bytes",
    "canonical_document_sha256", "canonical_validated_bytes", "document_digest",
    "fixture", "fixture_bytes", "gate_predicate_registry", "package_tuple",
    "root_manifest", "root_manifest_bytes", "schema", "schema_bytes", "schema_ids",
    "seal_document", "stable_error_registry", "tool_catalog", "validate_document",
    "verify_budget_transition", "verify_bundle", "verify_content_ref_set",
    "verify_document_digest",
]
'''


__all__ = ["PYTHON_VERIFY"]
