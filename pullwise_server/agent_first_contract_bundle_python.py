"""Render the dependency-free Python current-contract facade."""

from __future__ import annotations

import base64


def render_python_wrapper(
    identity: str,
    version: str,
    root_sha256: str,
    content_sha256: str,
    canonical: bytes,
) -> bytes:
    replacements = {
        "@@IDENTITY@@": repr(identity),
        "@@VERSION@@": repr(version),
        "@@ROOT@@": repr(root_sha256),
        "@@CONTENT@@": repr(content_sha256),
        "@@PAYLOAD@@": repr(base64.b64encode(canonical).decode("ascii")),
    }
    rendered = _TEMPLATE
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value)
    return rendered.encode("utf-8")


_TEMPLATE = '''"""Generated from the Server-owned Agent-First bundle; do not edit."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata

PACKAGE_IDENTITY = @@IDENTITY@@
PACKAGE_VERSION = @@VERSION@@
ROOT_SHA256 = @@ROOT@@
CONTENT_SHA256 = @@CONTENT@@
PACKAGE_TUPLE = (PACKAGE_IDENTITY, PACKAGE_VERSION, CONTENT_SHA256, ROOT_SHA256)
BUNDLE_BASE64 = @@PAYLOAD@@
SAFE_INTEGER = (1 << 53) - 1


class ContractValidationError(ValueError):
    pass


def _fail(code: str, path: str = "$") -> None:
    raise ContractValidationError(f"{code}: {path}")


def _validate_canonical(value: object, path: str = "$") -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if not -SAFE_INTEGER <= value <= SAFE_INTEGER:
            _fail("CANONICAL_INTEGER_UNSAFE", path)
        return
    if isinstance(value, str):
        if unicodedata.normalize("NFC", value) != value:
            _fail("CANONICAL_STRING_NOT_NFC", path)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_canonical(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key.isascii():
                _fail("CANONICAL_KEY_INVALID", path)
            _validate_canonical(item, f"{path}.{key}")
        return
    _fail("CANONICAL_TYPE_INVALID", path)


def canonical_document_bytes(value: object) -> bytes:
    _validate_canonical(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_document_sha256(value: object) -> str:
    return hashlib.sha256(canonical_document_bytes(value)).hexdigest()


def bundle_bytes() -> bytes:
    return base64.b64decode(BUNDLE_BASE64, validate=True)


def bundle() -> dict[str, object]:
    return json.loads(bundle_bytes().decode("utf-8"))


def root_manifest() -> dict[str, object]:
    return bundle()["root_manifest"]


def root_manifest_bytes() -> bytes:
    return canonical_document_bytes(root_manifest())


def package_tuple() -> dict[str, object]:
    return {
        "schema_id": "current-package-ref/v1",
        "package_identity": PACKAGE_IDENTITY,
        "package_version": PACKAGE_VERSION,
        "content_sha256": CONTENT_SHA256,
        "root_sha256": ROOT_SHA256,
    }


def _find(collection: str, identity_key: str, identity: str) -> dict[str, object]:
    for family in bundle()["families"]:
        for document in family[collection]:
            if document[identity_key] == identity:
                return json.loads(canonical_document_bytes(document).decode("utf-8"))
    raise KeyError(identity)


def schema(schema_id: str) -> dict[str, object]:
    return _find("schemas", "$id", schema_id)


def schema_bytes(schema_id: str) -> bytes:
    return canonical_document_bytes(schema(schema_id))


def fixture(fixture_id: str) -> dict[str, object]:
    return _find("fixtures", "fixture_id", fixture_id)


def fixture_bytes(fixture_id: str) -> bytes:
    return canonical_document_bytes(fixture(fixture_id))


def _type_matches(type_name: str, value: object) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(type_name, False)


def _validate_node(rule: dict[str, object], value: object, path: str) -> None:
    if "$ref" in rule:
        _validate_node(schema(rule["$ref"]), value, path)
        expected_schema = rule.get("x-pullwise-content-schema-id")
        if expected_schema is not None and (
            not isinstance(value, dict)
            or value.get("content_schema_id") != expected_schema
        ):
            _fail("CONTENT_REF_SCHEMA_INVALID", path)
        allowed_schemas = rule.get("x-pullwise-content-schema-ids")
        if allowed_schemas is not None and (
            not isinstance(value, dict)
            or value.get("content_schema_id") not in allowed_schemas
        ):
            _fail("CONTENT_REF_SCHEMA_INVALID", path)
        return
    if "const" in rule and value != rule["const"]:
        _fail("CONTRACT_CONST_INVALID", path)
    if "enum" in rule and value not in rule["enum"]:
        _fail("CONTRACT_ENUM_INVALID", path)
    declared = rule.get("type")
    if declared is not None:
        choices = declared if isinstance(declared, list) else [declared]
        if not any(_type_matches(choice, value) for choice in choices):
            _fail("CONTRACT_TYPE_INVALID", path)
    if isinstance(value, dict) and declared == "object":
        required = rule.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            _fail("CONTRACT_REQUIRED_MISSING", f"{path}.{missing[0]}")
        properties = rule.get("properties", {})
        if rule.get("additionalProperties") is False:
            unknown = sorted(set(value).difference(properties))
            if unknown:
                _fail("CONTRACT_FIELD_UNKNOWN", f"{path}.{unknown[0]}")
        for key, item in value.items():
            if key in properties:
                _validate_node(properties[key], item, f"{path}.{key}")
    if isinstance(value, list) and declared == "array":
        if len(value) < rule.get("minItems", 0):
            _fail("CONTRACT_ARRAY_TOO_SHORT", path)
        if "maxItems" in rule and len(value) > rule["maxItems"]:
            _fail("CONTRACT_ARRAY_TOO_LONG", path)
        if rule.get("uniqueItems"):
            encoded = [canonical_document_bytes(item) for item in value]
            if len(encoded) != len(set(encoded)):
                _fail("CONTRACT_ARRAY_NOT_UNIQUE", path)
        if "items" in rule:
            for index, item in enumerate(value):
                _validate_node(rule["items"], item, f"{path}[{index}]")
    if isinstance(value, str):
        if len(value) < rule.get("minLength", 0):
            _fail("CONTRACT_STRING_TOO_SHORT", path)
        if "maxLength" in rule and len(value) > rule["maxLength"]:
            _fail("CONTRACT_STRING_TOO_LONG", path)
        if "pattern" in rule and re.search(rule["pattern"], value) is None:
            _fail("CONTRACT_PATTERN_INVALID", path)
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in rule and value < rule["minimum"]:
            _fail("CONTRACT_NUMBER_TOO_SMALL", path)
        if "maximum" in rule and value > rule["maximum"]:
            _fail("CONTRACT_NUMBER_TOO_LARGE", path)


def validate_document(schema_id: str, value: object) -> dict[str, object]:
    detached = json.loads(canonical_document_bytes(value).decode("utf-8"))
    _validate_node(schema(schema_id), detached, "$")
    return detached


def canonical_validated_bytes(schema_id: str, value: object) -> bytes:
    return canonical_document_bytes(validate_document(schema_id, value))


def _digest_spec(schema_id: str) -> tuple[str, str]:
    spec = schema(schema_id).get("x-pullwise-digest")
    if not isinstance(spec, dict):
        _fail("CONTRACT_DIGEST_UNDECLARED", schema_id)
    field, domain = spec.get("field"), spec.get("domain")
    if not isinstance(field, str) or not isinstance(domain, str):
        _fail("CONTRACT_DIGEST_SPEC_INVALID", schema_id)
    return field, domain


def document_digest(schema_id: str, unsigned_value: object) -> str:
    field, domain = _digest_spec(schema_id)
    unsigned = json.loads(canonical_document_bytes(unsigned_value).decode("utf-8"))
    if field in unsigned:
        _fail("CONTRACT_DIGEST_FIELD_PRESENT", field)
    validate_document(schema_id, {**unsigned, field: "0" * 64})
    digest_input = domain.encode("utf-8") + b"\\0" + canonical_document_bytes(unsigned)
    return hashlib.sha256(digest_input).hexdigest()


def seal_document(schema_id: str, unsigned_value: object) -> dict[str, object]:
    field, _ = _digest_spec(schema_id)
    unsigned = json.loads(canonical_document_bytes(unsigned_value).decode("utf-8"))
    sealed = {**unsigned, field: document_digest(schema_id, unsigned)}
    return validate_document(schema_id, sealed)


def verify_document_digest(
    schema_id: str, complete_value: object
) -> dict[str, object]:
    complete = validate_document(schema_id, complete_value)
    field, _ = _digest_spec(schema_id)
    presented = complete[field]
    unsigned = {key: value for key, value in complete.items() if key != field}
    if presented != document_digest(schema_id, unsigned):
        _fail("CONTRACT_DIGEST_MISMATCH", field)
    return complete


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
    profile = fixture("core_golden_canonical_profile")["document"]
    validate_document("canonical-json-profile/v1", profile)
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
    "fixture", "fixture_bytes", "package_tuple", "root_manifest",
    "root_manifest_bytes", "schema", "schema_bytes", "seal_document",
    "validate_document", "verify_bundle", "verify_document_digest",
]
'''
