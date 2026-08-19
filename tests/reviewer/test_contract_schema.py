"""Contract fixture and schema tests for the pullwise-review/v1 closed contract.

This module exercises the exact write set of R1-02: the frozen manifest, the
manifest-bound shared JSON Schema (draft 2020-12), and the valid/invalid fixture
catalogue.  No JSON Schema library is installed in the environment, so schema
evaluation is done by the small self-contained validator below.  It implements
only the keywords the contract actually uses (verified against the schema's
full keyword inventory): $ref (internal), oneOf, type, const, enum, pattern,
minLength/maxLength, minimum/maximum, minItems/maxItems, items, required,
properties, and additionalProperties.  Annotations (format, x-*, description,
title, $id) are ignored.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
import unittest

from pullwise_server.reviewer.canonical import canonical_sha256, decode_strict_json


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "pullwise-review" / "v1"

EXPECTED_MANIFEST_DIGEST = "sha256:71428f4dc199e7cbdbe99b64cbdeff03686cda59eb08e84f22224822f5a8167e"
SCHEMA_RELATIVE_PATH = "shared/schemas/pullwise-review.schema.json"
EXPECTED_SCHEMA_DIGEST = "39dd603502669542b9e16b30d60522796794014307ae0335b2f892d551f0c6dd"

HTTP_STATUS_BY_ERROR_CODE = {
    "REQUEST_INVALID": 400,
    "EVIDENCE_INVALID": 422,
}


def _load_json(relative_path: str) -> dict:
    return json.loads((CONTRACT / relative_path).read_text(encoding="utf-8"))


def _raw_digest(relative_path: str) -> str:
    return hashlib.sha256((CONTRACT / relative_path).read_bytes()).hexdigest()


def _iter_refs(node) -> list[tuple[str, str]]:
    """Return (json_path, $ref) pairs for every $ref reachable from ``node``."""

    found: list[tuple[str, str]] = []

    def walk(value, json_path):
        if isinstance(value, dict):
            for key, nested in value.items():
                if key == "$ref" and isinstance(nested, str):
                    found.append((json_path, nested))
                else:
                    walk(nested, f"{json_path}.{key}")
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                walk(nested, f"{json_path}[{index}]")

    walk(node, "$")
    return found


# ---------------------------------------------------------------------------
# Self-contained draft-2020-12 subset validator
# ---------------------------------------------------------------------------


def _type_name(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _type_matches(value, type_name: str) -> bool:
    actual = _type_name(value)
    if type_name == "number":
        return actual in ("integer", "number")
    return actual == type_name


def _validate(node, instance, path: str, errors: list, root: dict) -> None:
    ref = node.get("$ref")
    if ref is not None:
        if not ref.startswith("#/$defs/"):
            errors.append((path, f"unsupported external $ref {ref}"))
            return
        target = ref[len("#/$defs/") :]
        if target not in root["$defs"]:
            errors.append((path, f"unresolved $ref {ref}"))
            return
        _validate(root["$defs"][target], instance, path, errors, root)
        return

    if "oneOf" in node:
        matches = 0
        for index, branch in enumerate(node["oneOf"]):
            branch_errors: list = []
            _validate(branch, instance, f"{path}#oneOf[{index}]", branch_errors, root)
            if not branch_errors:
                matches += 1
        if matches != 1:
            errors.append((path, f"oneOf requires exactly one match, got {matches}"))
        return

    if "type" in node and not _type_matches(instance, node["type"]):
        errors.append((path, f"expected type {node['type']}, got {_type_name(instance)}"))
        return

    if "const" in node and instance != node["const"]:
        errors.append((path, f"expected const {node['const']!r}, got {instance!r}"))

    if "enum" in node and instance not in node["enum"]:
        errors.append((path, f"value {instance!r} not in enum {node['enum']}"))

    if "pattern" in node and isinstance(instance, str):
        if re.fullmatch(node["pattern"], instance) is None:
            errors.append((path, f"value {instance!r} does not match pattern {node['pattern']}"))

    if "minLength" in node and isinstance(instance, str):
        if len(instance) < node["minLength"]:
            errors.append((path, f"len {len(instance)} < minLength {node['minLength']}"))
    if "maxLength" in node and isinstance(instance, str):
        if len(instance) > node["maxLength"]:
            errors.append((path, f"len {len(instance)} > maxLength {node['maxLength']}"))

    if "minimum" in node and isinstance(instance, (int, float)):
        if instance < node["minimum"]:
            errors.append((path, f"{instance} < minimum {node['minimum']}"))
    if "maximum" in node and isinstance(instance, (int, float)):
        if instance > node["maximum"]:
            errors.append((path, f"{instance} > maximum {node['maximum']}"))

    if isinstance(instance, dict):
        if "required" in node:
            for name in node["required"]:
                if name not in instance:
                    errors.append((path, f"missing required property {name!r}"))
        if "properties" in node:
            for name, prop_schema in node["properties"].items():
                if name in instance:
                    _validate(prop_schema, instance[name], f"{path}.{name}", errors, root)
            if node.get("additionalProperties") is False:
                for key in instance:
                    if key not in node["properties"]:
                        errors.append((path, f"additional property {key!r} is not permitted"))

    if isinstance(instance, list) and "items" in node:
        if "minItems" in node and len(instance) < node["minItems"]:
            errors.append((path, f"len {len(instance)} < minItems {node['minItems']}"))
        if "maxItems" in node and len(instance) > node["maxItems"]:
            errors.append((path, f"len {len(instance)} > maxItems {node['maxItems']}"))
        for index, item in enumerate(instance):
            _validate(node["items"], item, f"{path}[{index}]", errors, root)


def validate_definition(schema: dict, definition: str, instance) -> list:
    """Return a list of (json_path, message) violations of ``instance``."""

    if definition not in schema["$defs"]:
        return [("$defs", f"unknown definition {definition}")]
    errors: list = []
    _validate(schema["$defs"][definition], instance, "$", errors, schema)
    return errors


def classify_error_code(errors: list) -> str:
    """Map a violation list to the contract error code.

    Any violation whose JSON path is the Finding evidence path classifies as
    EVIDENCE_INVALID (the caller's own evidence is malformed); every other
    validation failure classifies as REQUEST_INVALID.
    """

    for path, _message in errors:
        if path == "$.evidence.path" or path.startswith("$.evidence.path."):
            return "EVIDENCE_INVALID"
        if path.startswith("$.evidence.path#"):
            return "EVIDENCE_INVALID"
    return "REQUEST_INVALID"


def _decode_or_mark(raw: bytes):
    """Return (instance, decode_error) with at most one of them set."""

    try:
        return decode_strict_json(raw), None
    except ValueError as error:
        return None, str(error)


# ---------------------------------------------------------------------------
# Manifest binding
# ---------------------------------------------------------------------------


class ManifestBindingTest(unittest.TestCase):
    def test_manifest_digest_matches_frozen_value(self) -> None:
        manifest = _load_json("manifest.json")
        payload = {key: value for key, value in manifest.items() if key != "manifest_digest"}
        self.assertEqual(EXPECTED_MANIFEST_DIGEST, canonical_sha256(payload))
        self.assertEqual(EXPECTED_MANIFEST_DIGEST, manifest["manifest_digest"])

    def test_every_manifest_file_exists_with_exact_bytes(self) -> None:
        manifest = _load_json("manifest.json")
        for relative_path, spec in manifest["files"].items():
            with self.subTest(relative_path=relative_path):
                data = (CONTRACT / relative_path).read_bytes()
                self.assertEqual(spec["size_bytes"], len(data))
                self.assertEqual(spec["sha256"], hashlib.sha256(data).hexdigest())

    def test_shared_schema_is_manifest_bound(self) -> None:
        manifest = _load_json("manifest.json")
        self.assertEqual(EXPECTED_SCHEMA_DIGEST, _raw_digest(SCHEMA_RELATIVE_PATH))
        self.assertEqual(
            EXPECTED_SCHEMA_DIGEST,
            manifest["files"][SCHEMA_RELATIVE_PATH]["sha256"],
        )


# ---------------------------------------------------------------------------
# Shared JSON Schema structure
# ---------------------------------------------------------------------------


class SharedSchemaStructureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = _load_json(SCHEMA_RELATIVE_PATH)

    def test_schema_is_draft_2020_12_with_document_root(self) -> None:
        self.assertEqual("https://json-schema.org/draft/2020-12/schema", self.schema["$schema"])
        self.assertEqual("#/$defs/Document", self.schema["$ref"])
        self.assertIn("Document", self.schema["$defs"])

    def test_every_object_with_properties_is_closed(self) -> None:
        def walk(node, json_path):
            if isinstance(node, dict):
                if "properties" in node:
                    with self.subTest(json_path=json_path):
                        self.assertIn(
                            "additionalProperties",
                            node,
                            f"object {json_path} has properties but no additionalProperties",
                        )
                        self.assertIs(False, node["additionalProperties"])
                for key, nested in node.items():
                    walk(nested, f"{json_path}.{key}")
            elif isinstance(node, list):
                for index, nested in enumerate(node):
                    walk(nested, f"{json_path}[{index}]")

        walk(self.schema["$ref"], "$")  # Document root
        for definition, subschema in self.schema["$defs"].items():
            walk(subschema, f"$defs.{definition}")

    def test_every_internal_ref_resolves(self) -> None:
        for json_path, ref in _iter_refs(self.schema):
            with self.subTest(json_path=json_path, ref=ref):
                self.assertTrue(
                    ref.startswith("#/$defs/"),
                    f"unexpected external ref {ref} at {json_path}",
                )
                self.assertIn(ref[len("#/$defs/") :], self.schema["$defs"])

    def test_constraint_spot_checks(self) -> None:
        definitions = self.schema["$defs"]

        create_scan = definitions["CreateScanRequest"]
        self.assertEqual(
            ["schema_id", "request_id", "source", "instruction_policy_id", "review_policy_id"],
            create_scan["required"],
        )
        self.assertEqual(
            "GITHUB_REPOSITORY_SNAPSHOT",
            create_scan["properties"]["source"]["properties"]["kind"]["const"],
        )

        finding = definitions["Finding"]
        self.assertEqual(0, finding["properties"]["confidence_bps"]["minimum"])
        self.assertEqual(10000, finding["properties"]["confidence_bps"]["maximum"])
        self.assertEqual("#/$defs/ByteSpan", finding["properties"]["evidence"]["$ref"])

        byte_span = definitions["ByteSpan"]
        self.assertEqual("#/$defs/RepoPath", byte_span["properties"]["path"]["$ref"])
        repo_path = definitions["RepoPath"]
        self.assertIn("pattern", repo_path)

        item = definitions["UpdateIssueStatusItem"]
        self.assertEqual(
            ["OPEN", "FIXED", "SNOOZED"],
            item["properties"]["target_status"]["enum"],
        )


# ---------------------------------------------------------------------------
# Fixture catalogue
# ---------------------------------------------------------------------------


class FixtureCatalogueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = _load_json("manifest.json")
        cls.schema = _load_json(SCHEMA_RELATIVE_PATH)
        cls.fixture_set = _load_json("fixtures/fixture-set.json")
        cls.registry = _load_json("registry.json")

    def test_fixture_files_hash_match_manifest(self) -> None:
        for fixture in self.fixture_set["fixtures"]:
            relative_path = fixture["path"]
            with self.subTest(fixture=fixture["id"], relative_path=relative_path):
                self.assertIn(relative_path, self.manifest["files"])
                self.assertEqual(
                    self.manifest["files"][relative_path]["sha256"],
                    _raw_digest(relative_path),
                )
        self.assertEqual(
            self.manifest["files"]["fixtures/fixture-set.json"]["sha256"],
            _raw_digest("fixtures/fixture-set.json"),
        )

    def test_valid_fixtures_validate(self) -> None:
        for fixture in self.fixture_set["fixtures"]:
            if not fixture["valid"]:
                continue
            with self.subTest(fixture=fixture["id"]):
                instance, decode_error = _decode_or_mark(
                    (CONTRACT / fixture["path"]).read_bytes()
                )
                self.assertIsNone(decode_error)
                violations = validate_definition(self.schema, fixture["definition"], instance)
                self.assertEqual([], violations)

    def test_invalid_fixtures_fail_with_catalogued_error_code(self) -> None:
        status_map = self.registry["http_status_by_error_code"]
        for fixture in self.fixture_set["fixtures"]:
            if fixture["valid"]:
                continue
            with self.subTest(fixture=fixture["id"]):
                expected_code = fixture["error_code"]
                self.assertIn(expected_code, status_map)
                self.assertEqual(
                    HTTP_STATUS_BY_ERROR_CODE[expected_code],
                    status_map[expected_code],
                )

                instance, decode_error = _decode_or_mark(
                    (CONTRACT / fixture["path"]).read_bytes()
                )
                if decode_error is not None:
                    self.assertEqual("REQUEST_INVALID", expected_code)
                    continue
                violations = validate_definition(self.schema, fixture["definition"], instance)
                self.assertTrue(violations, "invalid fixture must produce violations")
                self.assertEqual(expected_code, classify_error_code(violations))


if __name__ == "__main__":
    unittest.main()
