from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import types

from pullwise_server.agent_first_contract_bundle import build_bundle


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "contracts/agent-first/current/source"


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class VerificationFacadeHarness:
    @classmethod
    def setUpClass(cls) -> None:
        bundle = build_bundle(SOURCE_ROOT)
        cls.bundle = bundle.document
        cls.families = {item["family_id"]: item for item in cls.bundle["families"]}
        cls.schemas = {
            schema["$id"]: schema
            for family in cls.bundle["families"]
            for schema in family["schemas"]
        }
        cls.python = types.ModuleType("_verification_python_facade")
        exec(bundle.python_wrapper, cls.python.__dict__)
        cls.npm_wrapper = bundle.npm_wrapper

    def fixture_cases(
        self, schema_ids: tuple[str, ...]
    ) -> list[tuple[dict[str, object], tuple[str, dict[str, object]]]]:
        cases: list[tuple[dict[str, object], tuple[str, dict[str, object]]]] = []
        for family in self.bundle["families"]:
            for fixture in family["fixtures"]:
                if fixture["schema_id"] not in schema_ids:
                    continue
                cases.append(
                    (
                        fixture,
                        (
                            fixture["schema_id"],
                            deepcopy(fixture["document"]),
                        ),
                    )
                )
        return cases

    def fixture_document(self, fixture_id: str) -> dict[str, object]:
        for family in self.bundle["families"]:
            for fixture in family["fixtures"]:
                if fixture["fixture_id"] == fixture_id:
                    return deepcopy(fixture["document"])
        raise KeyError(fixture_id)

    def reseal(self, schema_id: str, document: dict[str, object]) -> dict[str, object]:
        schema = self.schemas[schema_id]
        spec = schema["x-pullwise-digest"]
        field = spec["field"]
        unsigned = {key: value for key, value in deepcopy(document).items() if key != field}
        result = deepcopy(document)
        result[field] = hashlib.sha256(
            spec["domain"].encode("utf-8") + b"\0" + canonical_bytes(unsigned)
        ).hexdigest()
        return result

    def python_document_results(
        self, cases: list[tuple[str, dict[str, object]]]
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for schema_id, document in cases:
            try:
                value = self.python.verify_document_digest(schema_id, document)
            except self.python.ContractValidationError as error:
                results.append(
                    {
                        "ok": False,
                        "code": error.code,
                        "detail": error.detail,
                        "path": error.path,
                    }
                )
            else:
                results.append({"ok": True, "value": value})
        return results

    def node_document_results(
        self, cases: list[tuple[str, dict[str, object]]]
    ) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory(prefix="verification-facade-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm_wrapper)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade_path.as_uri())};",
                        f"const cases = {json.dumps(cases, separators=(',', ':'))};",
                        "const results = [];",
                        "for (const [schemaId, document] of cases) {",
                        "  try {",
                        "    results.push({",
                        "      ok: true,",
                        "      value: await facade.verifyDocumentDigest(schemaId, document),",
                        "    });",
                        "  } catch (error) {",
                        "    results.push({",
                        "      ok: false,",
                        "      code: error.code,",
                        "      detail: error.detail,",
                        "      path: error.path,",
                        "    });",
                        "  }",
                        "}",
                        "process.stdout.write(JSON.stringify(results));",
                    )
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                ["node", str(runner_path)],
                check=True,
                capture_output=True,
                encoding="utf-8",
            )
        return json.loads(completed.stdout)

    def assert_document_parity(
        self, cases: list[tuple[str, dict[str, object]]]
    ) -> list[dict[str, object]]:
        python = self.python_document_results(cases)
        node = self.node_document_results(cases)
        self.assertEqual(python, node)
        return python

    def python_helper_exports(self, names: list[str]) -> dict[str, bool]:
        return {name: hasattr(self.python, name) for name in names}

    def node_helper_exports(self, aliases: dict[str, str]) -> dict[str, dict[str, object]]:
        with tempfile.TemporaryDirectory(prefix="verification-helper-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm_wrapper)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade_path.as_uri())};",
                        f"const aliases = {json.dumps(aliases, separators=(',', ':'))};",
                        "const results = {};",
                        "for (const [snake, camel] of Object.entries(aliases)) {",
                        "  const snakeFn = facade[snake];",
                        "  const camelFn = facade[camel];",
                        "  results[snake] = {",
                        "    snake: typeof snakeFn === 'function',",
                        "    camel: typeof camelFn === 'function',",
                        "    same: snakeFn === camelFn,",
                        "  };",
                        "}",
                        "process.stdout.write(JSON.stringify(results));",
                    )
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                ["node", str(runner_path)],
                check=True,
                capture_output=True,
                encoding="utf-8",
            )
        return json.loads(completed.stdout)
