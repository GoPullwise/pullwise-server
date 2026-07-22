from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest

from pullwise_server.agent_first_contract_bundle_npm import render_npm_wrapper
from pullwise_server.agent_first_contract_bundle_python import render_python_wrapper


ROOT = Path(__file__).resolve().parents[1]
FAMILIES = ROOT / "contracts" / "agent-first" / "current" / "source" / "families"


class AgentFirstTaskControlSemanticFacadesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        families = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(FAMILIES.glob("*.json"))
        ]
        canonical = json.dumps(
            {"families": families},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        python_wrapper = render_python_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            "1" * 64,
            canonical,
        )
        cls.python = types.ModuleType("_task_control_python_facade")
        exec(python_wrapper, cls.python.__dict__)
        cls.npm_wrapper = render_npm_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            "1" * 64,
            canonical,
        )

    def test_task_request_sets_are_enforced_in_python_and_node(self) -> None:
        request = deepcopy(
            self.python.fixture("task_control_golden_task_request")["document"]
        )
        request["requested_capabilities"] = ["source.write", "source.read"]
        python_result = self.python_validation("task-request/v1", request)
        node_result = self.node_validation("task-request/v1", request)
        expected = {
            "ok": False,
            "code": "CONTRACT_DOCUMENT_INVALID",
            "detail": "TASK_REQUEST_CAPABILITY_ORDER_INVALID",
            "path": "$",
        }
        self.assertEqual(expected, python_result)
        self.assertEqual(expected, node_result)

    def python_validation(
        self, schema_id: str, document: dict[str, object]
    ) -> dict[str, object]:
        try:
            value = self.python.validate_document(schema_id, document)
        except self.python.ContractValidationError as error:
            return {
                "ok": False,
                "code": error.code,
                "detail": error.detail,
                "path": error.path,
            }
        return {"ok": True, "value": value}

    def node_validation(
        self, schema_id: str, document: dict[str, object]
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory(prefix="task-control-facade-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm_wrapper)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade_path.as_uri())};",
                        f"const document = {json.dumps(document, separators=(',', ':'))};",
                        "let result;",
                        "try {",
                        "  result = {ok: true, value: facade.validateDocument(",
                        f"    {json.dumps(schema_id)}, document",
                        "  )};",
                        "} catch (error) {",
                        "  result = {ok: false, code: error.code,",
                        "    detail: error.detail, path: error.path};",
                        "}",
                        "process.stdout.write(JSON.stringify(result));",
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


if __name__ == "__main__":
    unittest.main()
