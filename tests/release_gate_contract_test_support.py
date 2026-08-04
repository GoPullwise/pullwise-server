from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
from types import ModuleType
import unittest

from pullwise_server.agent_first_contract_bundle import build_bundle


SOURCE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "agent-first"
    / "current"
    / "source"
)


class ReleaseGateContractTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        bundle = build_bundle(SOURCE_ROOT)
        cls.contract = ModuleType("_release_gate_source_contract")
        exec(bundle.python_wrapper, cls.contract.__dict__)
        cls.npm_wrapper = bundle.npm_wrapper

    def _node_call(self, statements: list[str]) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory(prefix="release-contract-node-") as scratch:
            root = Path(scratch)
            facade = root / "facade.mjs"
            runner = root / "runner.mjs"
            facade.write_bytes(self.npm_wrapper)
            runner.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade.as_uri())};",
                        *statements,
                    )
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                ["node", str(runner)],
                check=False,
                capture_output=True,
                encoding="utf-8",
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return completed

    def node_derivation(
        self,
        benchmark: dict[str, object],
        policy: dict[str, object],
        sample_set: dict[str, object],
    ) -> dict[str, object]:
        completed = self._node_call(
            [
                "const inputs = " + json.dumps(
                    [benchmark, policy, sample_set],
                    separators=(",", ":"),
                ) + ";",
                "const value = await facade."
                "deriveReleaseGateEvaluation(...inputs);",
                "process.stdout.write(JSON.stringify(value));",
            ]
        )
        return json.loads(completed.stdout)

    def node_derivation_error(self) -> dict[str, object]:
        completed = self._node_call(
            [
                "try {",
                "  await facade.deriveReleaseGateEvaluation(null, null, null);",
                "} catch (error) {",
                "  process.stdout.write(JSON.stringify({",
                "    code: error.code, detail: error.detail, path: error.path,",
                "  }));",
                "}",
            ]
        )
        return json.loads(completed.stdout)


__all__ = ["ReleaseGateContractTestCase"]
