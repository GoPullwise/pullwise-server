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
FAMILY_PATH = (
    ROOT
    / "contracts"
    / "agent-first"
    / "current"
    / "source"
    / "families"
    / "effective-execution-policy.json"
)
SCHEMA_ID = "effective-execution-policy/v1"


class AgentFirstEffectivePolicyFacadesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        family = json.loads(FAMILY_PATH.read_text(encoding="utf-8"))
        dependencies = [
            json.loads(
                (FAMILY_PATH.parent / f"{family_id}.json").read_text(
                    encoding="utf-8"
                )
            )
            for family_id in ("core", "task-result-identities")
        ]
        canonical = json.dumps(
            {
                "families": dependencies
                + [
                    {
                        "family_id": "effective-policy-semantic-probe",
                        "schemas": family["schemas"],
                        "fixtures": family["fixtures"],
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
        cls.python = types.ModuleType("_effective_policy_python_facade")
        exec(python_wrapper, cls.python.__dict__)
        cls.npm_wrapper = render_npm_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            "1" * 64,
            canonical,
        )

    def test_declared_rules_have_python_and_node_error_parity(self) -> None:
        valid = self.seal({"capability_risk_ceiling": "R1"})
        reserve = deepcopy(self.golden)
        reserve["terminalization_reserve_ms"] = reserve["budgets"]["wall_ms"] + 1
        capabilities = deepcopy(self.golden)
        capabilities["granted_capabilities"] = ["source.write", "source.read"]
        quality = self.seal({"quality_risk_floor": "Q2"})
        network = deepcopy(self.golden)
        network["agent_tool_network"] = {
            "mode": "allowlist",
            "origins": ["https://example.invalid"],
        }
        roots = deepcopy(self.golden)
        roots["allowed_read_roots"] = ["z-root", "a-root"]
        digest = self.seal({"policy_version": 2})
        digest["digest"] = self.golden["digest"]
        invalid_cases = [
            (self.reseal(reserve), "POLICY_RESERVE_INVALID", "$"),
            (
                self.reseal(capabilities),
                "POLICY_CAPABILITY_ORDER_INVALID",
                "$",
            ),
            (quality, "POLICY_QUALITY_RISK_FLOOR_INVALID", "$"),
            (self.reseal(network), "POLICY_NETWORK_INVALID", "$"),
            (self.reseal(roots), "POLICY_ROOT_ORDER_INVALID", "$"),
            (digest, "CONTRACT_DIGEST_MISMATCH", "$.digest"),
        ]
        documents = [valid] + [document for document, _, _ in invalid_cases]

        python_results = [self.python_result(document) for document in documents]
        node_results = self.node_results(documents)

        self.assertEqual(python_results, node_results)
        self.assertEqual({"ok": True, "value": valid}, python_results[0])
        for result, (_, detail, path) in zip(python_results[1:], invalid_cases):
            self.assertEqual(
                {
                    "ok": False,
                    "code": "CONTRACT_DOCUMENT_INVALID",
                    "detail": detail,
                    "path": path,
                },
                result,
            )

    def seal(self, changes: dict[str, object]) -> dict[str, object]:
        document = deepcopy(self.golden)
        document.update(changes)
        return self.reseal(document)

    @staticmethod
    def reseal(document: dict[str, object]) -> dict[str, object]:
        unsigned = {key: value for key, value in document.items() if key != "digest"}
        canonical = json.dumps(
            unsigned,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        document["digest"] = hashlib.sha256(
            b"pullwise:effective-execution-policy/v1\0" + canonical
        ).hexdigest()
        return document

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
        with tempfile.TemporaryDirectory(prefix="effective-policy-facade-") as scratch:
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
                        "    return {ok: false, code: error.code,",
                        "      detail: error.detail, path: error.path};",
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
