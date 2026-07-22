from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest

from pullwise_server.agent_first_contract_bundle_npm import render_npm_wrapper
from pullwise_server.agent_first_contract_bundle_python import render_python_wrapper


ROOT = Path(__file__).resolve().parents[1]
FAMILY_ROOT = ROOT / "contracts/agent-first/current/source/families"
FAMILY_IDS = ("core", "pre-gate", "task-evidence")


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class AgentFirstTaskEvidenceFacadesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        families = [
            json.loads(
                (FAMILY_ROOT / f"{family_id}.json").read_text(encoding="utf-8")
            )
            for family_id in FAMILY_IDS
        ]
        error_family = json.loads(
            (FAMILY_ROOT / "receipt-error.json").read_text(encoding="utf-8")
        )
        error_fixture = next(
            item
            for item in error_family["fixtures"]
            if item["fixture_id"] == "error_golden_current_registry"
        )
        families.append(
            {
                "family_id": "receipt-error",
                "schemas": [],
                "fixtures": [error_fixture],
            }
        )
        canonical = canonical_bytes(
            {"root_manifest": {"schema_registry": []}, "families": families}
        )
        cls.python = types.ModuleType("_task_evidence_python_facade")
        exec(
            render_python_wrapper(
                "@pullwise/agent-task-contract",
                "0.1.0",
                "0" * 64,
                hashlib.sha256(canonical).hexdigest(),
                canonical,
            ),
            cls.python.__dict__,
        )
        cls.npm_wrapper = render_npm_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            hashlib.sha256(canonical).hexdigest(),
            canonical,
        )
        cls.fixtures = {
            item["fixture_id"]: item
            for family in families
            for item in family["fixtures"]
        }

    def document(self, fixture_id: str) -> dict[str, object]:
        return deepcopy(self.fixtures[fixture_id]["document"])

    def test_golden_context_verifies_through_both_public_facades(self) -> None:
        manifest = self.document("task_evidence_golden_manifest")
        pre_gate = self.document("pre_gate_golden_evidence_closure")
        operations = [{"kind": "context", "args": [manifest, pre_gate]}]

        results = self.assert_parity(operations)

        self.assertEqual([{"ok": True, "value": manifest}], results)

    def python_results(
        self, operations: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        results = []
        for operation in operations:
            try:
                if operation["kind"] == "document":
                    value = self.python.verify_document_digest(
                        operation["schema_id"], *operation["args"]
                    )
                else:
                    value = self.python.verify_evidence_closure_context(
                        *operation["args"]
                    )
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

    def node_results(
        self, operations: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory(prefix="task-evidence-facade-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm_wrapper)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade_path.as_uri())};",
                        f"const operations = {json.dumps(operations, separators=(',', ':'))};",
                        "if (facade.verify_evidence_closure_context !==",
                        "    facade.verifyEvidenceClosureContext) {",
                        "  throw new Error('helper alias mismatch');",
                        "}",
                        "const results = [];",
                        "for (const operation of operations) {",
                        "  try {",
                        "    const value = operation.kind === 'document'",
                        "      ? await facade.verifyDocumentDigest(",
                        "          operation.schema_id, ...operation.args)",
                        "      : await facade.verifyEvidenceClosureContext(",
                        "          ...operation.args);",
                        "    results.push({ok: true, value});",
                        "  } catch (error) {",
                        "    results.push({",
                        "      ok: false, code: error.code, detail: error.detail,",
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

    def assert_parity(
        self, operations: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        python = self.python_results(operations)
        node = self.node_results(operations)
        self.assertEqual(python, node)
        return python


if __name__ == "__main__":
    unittest.main()
