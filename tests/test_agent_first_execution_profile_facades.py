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
FAMILY_PATH = (
    ROOT
    / "contracts"
    / "agent-first"
    / "current"
    / "source"
    / "families"
    / "execution-profile.json"
)
SCHEMA_ID = "execution-profile/v1"


class AgentFirstExecutionProfileFacadesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        family = json.loads(FAMILY_PATH.read_text(encoding="utf-8"))
        schema = deepcopy(family["schemas"][0])
        properties = schema["properties"]
        for field in ("image_identity", "operating_system", "cpu_architecture"):
            properties[field] = {"type": "string", "minLength": 1}

        canonical = json.dumps(
            {
                "families": [
                    {
                        "family_id": "execution-profile-semantic-probe",
                        "schemas": [schema],
                        "fixtures": [],
                    }
                ]
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        cls.golden = deepcopy(family["fixtures"][0]["document"])

        python_wrapper = render_python_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            "1" * 64,
            canonical,
        )
        cls.python = types.ModuleType("_execution_profile_python_facade")
        exec(python_wrapper, cls.python.__dict__)
        cls.npm_wrapper = render_npm_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            "1" * 64,
            canonical,
        )

    def test_declared_rule_has_python_and_node_stable_code_parity(self) -> None:
        valid_documents = [
            self.golden,
            {**self.golden, "cpu_architecture": "aarch64"},
        ]
        invalid_cases = [
            (
                {**self.golden, "image_identity": "mutable:latest"},
                "EXECUTION_PROFILE_IMAGE_MUTABLE",
            ),
            (
                {**self.golden, "operating_system": "darwin"},
                "EXECUTION_PROFILE_OS_INVALID",
            ),
            (
                {**self.golden, "cpu_architecture": "amd64"},
                "EXECUTION_PROFILE_ARCH_INVALID",
            ),
        ]
        documents = valid_documents + [document for document, _ in invalid_cases]

        python_results = [self.python_result(document) for document in documents]
        node_results = self.node_results(documents)

        self.assertEqual(python_results, node_results)
        self.assertEqual(
            [{"ok": True, "value": document} for document in valid_documents],
            python_results[: len(valid_documents)],
        )
        for result, (_, expected_detail) in zip(
            python_results[len(valid_documents) :], invalid_cases
        ):
            self.assertEqual(
                {
                    "ok": False,
                    "code": "CONTRACT_DOCUMENT_INVALID",
                    "detail": expected_detail,
                    "path": "$",
                },
                result,
            )

    def python_result(self, document: dict[str, object]) -> dict[str, object]:
        try:
            value = self.python.validate_document(SCHEMA_ID, document)
        except self.python.ContractValidationError as error:
            return {
                "ok": False,
                "code": error.code,
                "detail": error.detail,
                "path": error.path,
            }
        return {"ok": True, "value": value}

    def node_results(
        self, documents: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory(prefix="execution-profile-facade-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm_wrapper)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade_path.as_uri())};",
                        f"const documents = {json.dumps(documents, separators=(',', ':'))};",
                        "const results = documents.map((document) => {",
                        "  try {",
                        "    return {ok: true, value: facade.validateDocument(",
                        f"      {json.dumps(SCHEMA_ID)}, document",
                        "    )};",
                        "  } catch (error) {",
                        "    return {",
                        "      ok: false, code: error.code, detail: error.detail,",
                        "      path: error.path,",
                        "    };",
                        "  }",
                        "});",
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


if __name__ == "__main__":
    unittest.main()
