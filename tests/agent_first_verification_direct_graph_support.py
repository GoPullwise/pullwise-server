from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import types

from pullwise_server.agent_first_contract_bundle import build_bundle
from tests.agent_first_verification_direct_graph_builder import (
    VerificationDirectGraphBuilderMixin,
    canonical_bytes,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "contracts/agent-first/current/source"
ALIASES = {
    "verify_completion_proposal_context": "verifyCompletionProposalContext",
    "verify_verifier_input_context": "verifyVerifierInputContext",
    "verify_verifier_work_context": "verifyVerifierWorkContext",
    "verify_attestation_context": "verifyAttestationContext",
    "verify_attestation_manifest_context": "verifyAttestationManifestContext",
}


class VerificationDirectGraphHarness(VerificationDirectGraphBuilderMixin):
    @classmethod
    def setUpClass(cls) -> None:
        built = build_bundle(SOURCE_ROOT)
        cls.bundle = built.document
        cls.schemas = {
            schema["$id"]: schema
            for family in cls.bundle["families"]
            for schema in family["schemas"]
        }
        cls.python = types.ModuleType("_verification_direct_public_python")
        exec(built.python_wrapper, cls.python.__dict__)
        cls.python_helpers = cls.python
        cls.npm_wrapper = built.npm_wrapper
        cls.npm_helper_wrapper = cls.npm_wrapper

    def fixture_document(self, fixture_id: str) -> dict[str, object]:
        for family in self.bundle["families"]:
            for fixture in family["fixtures"]:
                if fixture["fixture_id"] == fixture_id:
                    return deepcopy(fixture["document"])
        raise KeyError(fixture_id)

    def reseal(self, schema_id: str, document: dict[str, object]) -> dict[str, object]:
        spec = self.schemas[schema_id]["x-pullwise-digest"]
        field = spec["field"]
        unsigned = {key: value for key, value in document.items() if key != field}
        result = deepcopy(document)
        result[field] = hashlib.sha256(
            spec["domain"].encode("utf-8") + b"\0" + canonical_bytes(unsigned)
        ).hexdigest()
        return result

    def reseal_source_tree(self, document: dict[str, object]) -> dict[str, object]:
        result = deepcopy(document)
        result["entries"] = sorted(
            result["entries"], key=lambda item: item["path"].encode("utf-8")
        )
        result["entry_count"] = len(result["entries"])
        result["total_bytes"] = sum(
            item.get("size_bytes", 0)
            for item in result["entries"]
            if item["type"] == "file"
        )
        identity = {
            "base_revision": result["base_revision"],
            "selection_policy_digest": result["selection_policy_digest"],
            "entries": result["entries"],
        }
        result["source_state_id"] = hashlib.sha256(
            canonical_bytes(identity)
        ).hexdigest()
        return self.reseal("source-tree-manifest/v1", result)

    def reseal_execution_state(self, document: dict[str, object]) -> dict[str, object]:
        result = deepcopy(document)
        unsigned = {
            key: value
            for key, value in result.items()
            if key not in {"execution_state_id", "manifest_digest"}
        }
        result["execution_state_id"] = hashlib.sha256(
            canonical_bytes(unsigned)
        ).hexdigest()
        return self.reseal("execution-state-manifest/v1", result)

    def content_ref(
        self, artifact_id: str, schema_id: str, document: dict[str, object]
    ) -> dict[str, object]:
        raw = canonical_bytes(document)
        return {
            "schema_id": "content-ref/v1",
            "artifact_id": artifact_id,
            "content_schema_id": schema_id,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "media_type": "application/json",
            "encoding": "utf-8",
        }

    def _capture_python(self, module, callback) -> dict[str, object]:
        try:
            return {"ok": True, "value": callback()}
        except module.ContractValidationError as error:
            return {
                "ok": False,
                "code": error.code,
                "detail": error.detail,
                "path": error.path,
            }

    def document_results(
        self, cases: list[tuple[str, dict[str, object]]]
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        python = []
        for schema_id, document in cases:
            fn = (
                self.python.verify_document_digest
                if "x-pullwise-digest" in self.schemas[schema_id]
                else self.python.validate_document
            )
            python.append(
                self._capture_python(
                    self.python, lambda fn=fn, schema_id=schema_id, document=document: fn(schema_id, document)
                )
            )
        with tempfile.TemporaryDirectory(prefix="verification-direct-docs-") as scratch:
            scratch_path = Path(scratch)
            facade = scratch_path / "facade.mjs"
            runner = scratch_path / "runner.mjs"
            facade.write_bytes(self.npm_wrapper)
            runner.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade.as_uri())};",
                        f"const cases = {json.dumps(cases, separators=(',', ':'))};",
                        "const capture = async (schemaId, document) => {",
                        "  try {",
                        "    const digest = facade.schema(schemaId)['x-pullwise-digest'];",
                        "    const value = digest ?",
                        "      await facade.verifyDocumentDigest(schemaId, document) :",
                        "      facade.validateDocument(schemaId, document);",
                        "    return {ok: true, value};",
                        "  } catch (error) {",
                        "    return {ok: false, code: error.code, detail: error.detail, path: error.path};",
                        "  }",
                        "};",
                        "const results = [];",
                        "for (const [schemaId, document] of cases) results.push(await capture(schemaId, document));",
                        "process.stdout.write(JSON.stringify(results));",
                    )
                ),
                encoding="utf-8",
            )
            node = json.loads(
                subprocess.run(
                    ["node", str(runner)],
                    check=True,
                    capture_output=True,
                    encoding="utf-8",
                ).stdout
            )
        return python, node

    def public_helper_exports(self) -> tuple[dict[str, bool], dict[str, dict[str, object]]]:
        python = {name: hasattr(self.python, name) for name in ALIASES}
        with tempfile.TemporaryDirectory(prefix="verification-direct-exports-") as scratch:
            scratch_path = Path(scratch)
            facade = scratch_path / "facade.mjs"
            runner = scratch_path / "runner.mjs"
            facade.write_bytes(self.npm_wrapper)
            runner.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade.as_uri())};",
                        f"const aliases = {json.dumps(ALIASES, separators=(',', ':'))};",
                        "const out = {};",
                        "for (const [snake, camel] of Object.entries(aliases)) {",
                        "  out[snake] = {",
                        "    snake: typeof facade[snake] === 'function',",
                        "    camel: typeof facade[camel] === 'function',",
                        "    same: facade[snake] === facade[camel],",
                        "  };",
                        "}",
                        "process.stdout.write(JSON.stringify(out));",
                    )
                ),
                encoding="utf-8",
            )
            node = json.loads(
                subprocess.run(
                    ["node", str(runner)],
                    check=True,
                    capture_output=True,
                    encoding="utf-8",
                ).stdout
            )
        return python, node

    def helper_results(
        self, operations: list[dict[str, object]]
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        python = [
            self._capture_python(
                self.python_helpers,
                lambda op=operation: getattr(self.python_helpers, op["python"])(*op["args"]),
            )
            for operation in operations
        ]
        payload = [
            {"snake": item["python"], "camel": item["node"], "args": item["args"]}
            for item in operations
        ]
        with tempfile.TemporaryDirectory(prefix="verification-direct-helpers-") as scratch:
            scratch_path = Path(scratch)
            facade = scratch_path / "facade.mjs"
            runner = scratch_path / "runner.mjs"
            facade.write_bytes(self.npm_helper_wrapper)
            runner.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade.as_uri())};",
                        f"const ops = {json.dumps(payload, separators=(',', ':'))};",
                        "const capture = async (fn, args) => {",
                        "  try { return {ok: true, value: await fn(...args)}; }",
                        "  catch (error) {",
                        "    return {ok: false, code: error.code, detail: error.detail, path: error.path};",
                        "  }",
                        "};",
                        "const out = [];",
                        "for (const op of ops) {",
                        "  const snake = facade[op.snake];",
                        "  const camel = facade[op.camel];",
                        "  const result = await capture(camel, op.args);",
                        "  out.push({",
                        "    same: snake === camel,",
                        "    snake: result,",
                        "    camel: result,",
                        "  });",
                        "}",
                        "process.stdout.write(JSON.stringify(out));",
                    )
                ),
                encoding="utf-8",
            )
            node = json.loads(
                subprocess.run(
                    ["node", str(runner)],
                    check=True,
                    capture_output=True,
                    encoding="utf-8",
                ).stdout
            )
        return python, node
