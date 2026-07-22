"""Render the dependency-free Python current-contract facade."""

from __future__ import annotations

import base64

from .agent_first_contract_bundle_python_semantics import PYTHON_SEMANTICS
from .agent_first_contract_bundle_python_verify import PYTHON_VERIFY


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
    rendered = _TEMPLATE.replace("@@SEMANTICS@@", PYTHON_SEMANTICS)
    rendered = rendered.replace("@@VERIFY@@", PYTHON_VERIFY)
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
    def __init__(self, code: str, detail: str, path: str) -> None:
        self.code, self.detail, self.path = code, detail, path
        super().__init__(f"{code}: {detail}: {path}")


def _fail(detail: str, path: str = "$", code: str | None = None) -> None:
    raise ContractValidationError(_public_error_code(detail, code), detail, path)


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
    if "oneOf" in rule:
        _validate_one_of(rule["oneOf"], value, path)
    if "$ref" in rule:
        _validate_node(schema(rule["$ref"]), value, path)
        _validate_reference_annotations(rule, value, path)
        return
    if "const" in rule and not _json_equal(value, rule["const"]):
        _fail("CONTRACT_CONST_INVALID", path)
    if "enum" in rule and not any(_json_equal(value, item) for item in rule["enum"]):
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
        if "pattern" in rule and not _pattern_matches(rule["pattern"], value):
            _fail("CONTRACT_PATTERN_INVALID", path)
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in rule and value < rule["minimum"]:
            _fail("CONTRACT_NUMBER_TOO_SMALL", path)
        if "maximum" in rule and value > rule["maximum"]:
            _fail("CONTRACT_NUMBER_TOO_LARGE", path)


@@SEMANTICS@@


def validate_document(schema_id: str, value: object) -> dict[str, object]:
    detached = json.loads(canonical_document_bytes(value).decode("utf-8"))
    _validate_node(schema(schema_id), detached, "$")
    _validate_semantics(schema_id, detached)
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


@@VERIFY@@
'''
