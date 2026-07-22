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


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def seal(schema: dict[str, object], value: dict[str, object]) -> dict[str, object]:
    result = deepcopy(value)
    spec = schema["x-pullwise-digest"]
    field = spec["field"]
    unsigned = {key: item for key, item in result.items() if key != field}
    result[field] = hashlib.sha256(
        spec["domain"].encode("utf-8") + b"\0" + canonical_bytes(unsigned)
    ).hexdigest()
    return result


class AgentFirstBudgetFacadesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.family = json.loads(
            (FAMILY_ROOT / "budget.json").read_text(encoding="utf-8")
        )
        core = json.loads(
            (FAMILY_ROOT / "core.json").read_text(encoding="utf-8")
        )
        errors = json.loads(
            (FAMILY_ROOT / "receipt-error.json").read_text(encoding="utf-8")
        )
        error_fixture = next(
            item
            for item in errors["fixtures"]
            if item["fixture_id"] == "error_golden_current_registry"
        )
        families = [
            core,
            cls.family,
            {
                "family_id": "receipt-error",
                "schemas": [],
                "fixtures": [error_fixture],
            },
        ]
        payload = canonical_bytes(
            {"root_manifest": {"schema_registry": []}, "families": families}
        )
        render_args = (
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            hashlib.sha256(payload).hexdigest(),
            payload,
        )
        cls.python = types.ModuleType("_budget_python_facade")
        exec(render_python_wrapper(*render_args), cls.python.__dict__)
        cls.npm = render_npm_wrapper(*render_args)
        cls.schemas = {item["$id"]: item for item in cls.family["schemas"]}
        cls.fixtures = {
            item["fixture_id"]: item for item in cls.family["fixtures"]
        }

    def document(self, fixture_id: str) -> dict[str, object]:
        return deepcopy(self.fixtures[fixture_id]["document"])

    def test_golden_settlement_executes_through_both_public_facades(self) -> None:
        operation = {
            "kind": "transition",
            "args": [
                self.document("budget_golden_ledger_before"),
                self.document("budget_golden_reservation"),
                self.document("budget_golden_ledger_reserved"),
                self.document("budget_golden_settlement"),
                self.document("budget_golden_ledger_after_settlement"),
            ],
        }

        self.assertEqual(
            [{"ok": True, "value": True}], self.assert_parity([operation])
        )

    def python_results(
        self, operations: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for operation in operations:
            try:
                if operation["kind"] == "document":
                    value = self.python.verify_document_digest(
                        operation["schema_id"], *operation["args"]
                    )
                else:
                    value = self.python.verify_budget_transition(
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
        with tempfile.TemporaryDirectory(prefix="budget-facade-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade_path.as_uri())};",
                        f"const operations = {json.dumps(operations, separators=(',', ':'))};",
                        "const results = [];",
                        "for (const operation of operations) {",
                        "  try {",
                        "    const value = operation.kind === 'document'",
                        "      ? await facade.verifyDocumentDigest(",
                        "          operation.schema_id, ...operation.args)",
                        "      : await facade.verifyBudgetTransition(...operation.args);",
                        "    results.push({ok: true, value});",
                        "  } catch (error) {",
                        "    results.push({ok: false, code: error.code,",
                        "      detail: error.detail, path: error.path});",
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
