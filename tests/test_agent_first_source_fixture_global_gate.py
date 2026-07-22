from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import types
from types import MappingProxyType
import unittest

from pullwise_server.agent_first_contract_bundle import build_bundle


SOURCE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "agent-first"
    / "current"
    / "source"
)

STRUCTURAL_PREEMPTIONS = MappingProxyType(
    {
        "receipt_negative_transport_as_local": (
            "CONTRACT_DOCUMENT_INVALID",
            "CONTRACT_CONST_INVALID",
            "$.receipt_kind",
        )
    }
)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class AgentFirstSourceFixtureGlobalGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        built = build_bundle(SOURCE_ROOT)
        cls.bundle = built.document
        cls.python = types.ModuleType("_agent_first_live_source_facade")
        exec(built.python_wrapper, cls.python.__dict__)
        cls.scratch = tempfile.TemporaryDirectory(prefix="agent-first-global-gate-")
        scratch = Path(cls.scratch.name)
        facade = scratch / "facade.mjs"
        runner = scratch / "runner.mjs"
        facade.write_bytes(built.npm_wrapper)
        runner.write_text(
            "\n".join(
                (
                    f"import * as facade from {json.dumps(facade.as_uri())};",
                    "function capture(operation) {",
                    "  return Promise.resolve().then(operation).then(",
                    "    (value) => ({kind: 'ok', value}),",
                    "    (error) => error instanceof facade.ContractValidationError",
                    "      ? {kind: 'contract_error', code: error.code, detail: error.detail, path: error.path}",
                    "      : {kind: 'language_error', name: error?.name ?? 'Error', message: String(error?.message ?? error)},",
                    "  );",
                    "}",
                    "const documents = [];",
                    "const digests = [];",
                    "for (const family of facade.bundle().families) {",
                    "  const digestSchemas = new Set(family.schemas.filter((schema) =>",
                    "    Object.prototype.hasOwnProperty.call(schema, 'x-pullwise-digest')).map((schema) => schema.$id));",
                    "  for (const fixture of family.fixtures) {",
                    "    documents.push(await capture(() => facade.validateDocument(fixture.schema_id, fixture.document)));",
                    "    if (digestSchemas.has(fixture.schema_id)) {",
                    "      digests.push(await capture(() => facade.verifyDocumentDigest(fixture.schema_id, fixture.document)));",
                    "    }",
                    "  }",
                    "}",
                    "const metadata = {",
                    "  verifyBundle: await capture(() => facade.verifyBundle()),",
                    "  rootManifest: await capture(() => facade.rootManifest()),",
                    "  packageTuple: await capture(() => facade.packageTuple()),",
                    "  PACKAGE_TUPLE: await capture(() => facade.PACKAGE_TUPLE),",
                    "  schemaIds: await capture(() => facade.schemaIds()),",
                    "};",
                    "process.stdout.write(JSON.stringify({documents, digests, metadata}));",
                )
            ),
            encoding="utf-8",
        )
        completed = subprocess.run(
            ["node", str(runner)],
            check=True,
            capture_output=True,
            encoding="utf-8",
            timeout=600,
        )
        cls.node = json.loads(completed.stdout)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scratch.cleanup()

    @classmethod
    def capture_python(cls, operation: object) -> dict[str, object]:
        try:
            value = operation()
        except cls.python.ContractValidationError as error:
            return {
                "kind": "contract_error",
                "code": error.code,
                "detail": error.detail,
                "path": error.path,
            }
        except Exception as error:  # pragma: no cover - parity assertion reports it.
            return {
                "kind": "language_error",
                "name": type(error).__name__,
                "message": str(error),
            }
        return {"kind": "ok", "value": value}

    def test_inventory_and_execution(self) -> None:
        root = self.bundle["root_manifest"]
        families = self.bundle["families"]
        self.assertEqual([family["family_id"] for family in families], root["required_families"])
        self.assertEqual(
            {family["family_id"] for family in families}, set(root["required_families"])
        )

        fixture_ids: set[str] = set()
        computed_registry: list[dict[str, object]] = []
        fixtures: list[dict[str, object]] = []
        digest_fixtures: list[dict[str, object]] = []
        for family in families:
            self.assertTrue(family["fixtures"])
            digest_schema_ids = {
                schema["$id"]
                for schema in family["schemas"]
                if "x-pullwise-digest" in schema
            }
            local_registry = []
            for fixture in family["fixtures"]:
                self.assertNotIn(fixture["fixture_id"], fixture_ids)
                fixture_ids.add(fixture["fixture_id"])
                # This registry hash seals the whole fixture, not just its document.
                local_registry.append(
                    {
                        "fixture_id": fixture["fixture_id"],
                        "family_id": family["family_id"],
                        "schema_id": fixture["schema_id"],
                        "fixture_class": fixture["fixture_class"],
                        "expected_code": fixture["expected_code"],
                        "sha256": hashlib.sha256(canonical_bytes(fixture)).hexdigest(),
                    }
                )
                fixtures.append(fixture)
                if fixture["schema_id"] in digest_schema_ids:
                    digest_fixtures.append(fixture)
            self.assertEqual(local_registry, family["fixture_registry"])
            computed_registry.extend(local_registry)
        self.assertEqual(computed_registry, [entry for family in families for entry in family["fixture_registry"]])
        self.assertEqual(computed_registry, root["fixture_registry"])

        stable_codes = {
            entry["code"] for entry in self.python.stable_error_registry()["entries"]
        }
        for fixture in fixtures:
            if fixture["expected_code"] is not None:
                self.assertIn(fixture["expected_code"], stable_codes)

        python_documents = [
            self.capture_python(
                lambda fixture=fixture: self.python.validate_document(
                    fixture["schema_id"], fixture["document"]
                )
            )
            for fixture in fixtures
        ]
        self.assertEqual(python_documents, self.node["documents"])
        self._assert_fixture_results(
            fixtures, python_documents, stable_codes, digest_operation=False
        )

        python_digests = [
            self.capture_python(
                lambda fixture=fixture: self.python.verify_document_digest(
                    fixture["schema_id"], fixture["document"]
                )
            )
            for fixture in digest_fixtures
        ]
        self.assertEqual(python_digests, self.node["digests"])
        self._assert_fixture_results(
            digest_fixtures, python_digests, stable_codes, digest_operation=True
        )

    def _assert_fixture_results(
        self,
        fixtures: list[dict[str, object]],
        results: list[dict[str, object]],
        stable_codes: set[str],
        *,
        digest_operation: bool,
    ) -> None:
        for fixture, result in zip(fixtures, results, strict=True):
            self.assertNotEqual(result["kind"], "language_error")
            if fixture["expected_code"] is None:
                self.assertEqual(result, {"kind": "ok", "value": fixture["document"]})
            elif result["kind"] == "ok":
                self.assertEqual(result["value"], fixture["document"])
            else:
                self.assertEqual(result["kind"], "contract_error")
                self.assertIn(result["code"], stable_codes)
                if not digest_operation:
                    preemption = STRUCTURAL_PREEMPTIONS.get(fixture["fixture_id"])
                    if preemption is None:
                        self.assertEqual(result["code"], fixture["expected_code"])
                    else:
                        code, detail, path = preemption
                        self.assertEqual(
                            result,
                            {
                                "kind": "contract_error",
                                "code": code,
                                "detail": detail,
                                "path": path,
                            },
                        )

    def test_integrity_and_metadata(self) -> None:
        self.assertTrue(self.python.verify_bundle())
        metadata = self.node["metadata"]
        self.assertEqual(metadata["verifyBundle"], {"kind": "ok", "value": True})
        self.assertEqual(metadata["rootManifest"], {"kind": "ok", "value": self.python.root_manifest()})
        self.assertEqual(metadata["packageTuple"], {"kind": "ok", "value": self.python.package_tuple()})
        self.assertEqual(metadata["PACKAGE_TUPLE"], {"kind": "ok", "value": list(self.python.PACKAGE_TUPLE)})
        self.assertEqual(metadata["schemaIds"], {"kind": "ok", "value": list(self.python.schema_ids())})


if __name__ == "__main__":
    unittest.main()
