"""OpenAPI 3.1 contract tests for the pullwise-review/v1 API surface.

Every OpenAPI request/response schema must resolve to the manifest-bound shared
schema, every operation must declare an explicit status map, and every $ref in
the document must resolve (either into the shared schema's $defs or into the
local components sections).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "pullwise-review" / "v1"

OPENAPI_RELATIVE_PATH = "openapi.json"
SCHEMA_RELATIVE_PATH = "shared/schemas/pullwise-review.schema.json"
EXPECTED_OPENAPI_VERSION = "3.1.0"

SHARED_SCHEMA_REF = re.compile(
    r"^\./shared/schemas/pullwise-review\.schema\.json#/\$defs/([A-Za-z0-9_]+)$"
)
COMPONENT_REF = re.compile(r"^#/components/(parameters|responses|schemas|securitySchemes)/[A-Za-z0-9_]+$")

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head", "trace")


def _raw_digest(relative_path: str) -> str:
    return hashlib.sha256((CONTRACT / relative_path).read_bytes()).hexdigest()


def _walk(node, on_ref: callable) -> None:
    if isinstance(node, dict):
        for key, nested in node.items():
            if key == "$ref" and isinstance(nested, str):
                on_ref(nested)
            else:
                _walk(nested, on_ref)
    elif isinstance(node, list):
        for nested in node:
            _walk(nested, on_ref)


class OpenApiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.openapi = json.loads(
            (CONTRACT / OPENAPI_RELATIVE_PATH).read_text(encoding="utf-8")
        )
        cls.manifest = json.loads((CONTRACT / "manifest.json").read_text(encoding="utf-8"))
        cls.schema = json.loads((CONTRACT / SCHEMA_RELATIVE_PATH).read_text(encoding="utf-8"))

    def test_openapi_version_is_3_1_0(self) -> None:
        self.assertEqual(EXPECTED_OPENAPI_VERSION, self.openapi["openapi"])

    def test_openapi_and_shared_schema_are_manifest_bound(self) -> None:
        self.assertEqual(
            self.manifest["files"][OPENAPI_RELATIVE_PATH]["sha256"],
            _raw_digest(OPENAPI_RELATIVE_PATH),
        )
        self.assertEqual(
            self.manifest["files"][SCHEMA_RELATIVE_PATH]["sha256"],
            _raw_digest(SCHEMA_RELATIVE_PATH),
        )

    def test_every_operation_has_an_explicit_status_map(self) -> None:
        operations = 0
        for path, methods in self.openapi["paths"].items():
            for method, operation in methods.items():
                if method not in HTTP_METHODS:
                    continue
                operations += 1
                with self.subTest(method=method, path=path):
                    self.assertIn("operationId", operation)
                    self.assertIn("responses", operation)
                    self.assertTrue(operation["responses"], "responses map must be explicit")
        self.assertGreater(operations, 0)

    def test_every_shared_schema_ref_resolves_to_existing_definition(self) -> None:
        refs = []

        def collect(ref: str) -> None:
            refs.append(ref)

        _walk(self.openapi, collect)
        shared_refs = [ref for ref in refs if ref.startswith("./shared/schemas/")]
        self.assertTrue(shared_refs, "expected request/response schemas from the shared schema")

        for ref in shared_refs:
            with self.subTest(ref=ref):
                match = SHARED_SCHEMA_REF.match(ref)
                self.assertIsNotNone(match, f"unexpected shared schema ref {ref}")
                self.assertIn(match.group(1), self.schema["$defs"])

    def test_every_component_ref_resolves(self) -> None:
        refs = []

        def collect(ref: str) -> None:
            refs.append(ref)

        _walk(self.openapi, collect)
        components = self.openapi["components"]
        component_refs = [ref for ref in refs if ref.startswith("#/components/")]
        self.assertTrue(component_refs)

        for ref in component_refs:
            with self.subTest(ref=ref):
                self.assertIsNotNone(COMPONENT_REF.match(ref), f"unexpected component ref {ref}")
                kind, name = ref[len("#/components/") :].split("/", 1)
                self.assertIn(name, components[kind])


if __name__ == "__main__":
    unittest.main()
