from __future__ import annotations

from copy import deepcopy
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


class AgentFirstReleaseGateSampleSetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        bundle = build_bundle(SOURCE_ROOT)
        cls.contract = ModuleType("_release_gate_sample_set_source_contract")
        exec(bundle.python_wrapper, cls.contract.__dict__)
        cls.npm_wrapper = bundle.npm_wrapper

    def test_public_derivation_rejects_missing_typed_inputs(self) -> None:
        with self.assertRaises(self.contract.ContractValidationError):
            self.contract.derive_release_gate_evaluation(None, None, None)

    def test_public_derivation_has_python_node_error_parity(self) -> None:
        with self.assertRaises(self.contract.ContractValidationError) as raised:
            self.contract.derive_release_gate_evaluation(None, None, None)
        expected = {
            "code": raised.exception.code,
            "detail": raised.exception.detail,
            "path": raised.exception.path,
        }
        with tempfile.TemporaryDirectory(prefix="release-sample-set-") as scratch:
            root = Path(scratch)
            facade = root / "facade.mjs"
            runner = root / "runner.mjs"
            facade.write_bytes(self.npm_wrapper)
            runner.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade.as_uri())};",
                        "try {",
                        "  await facade.deriveReleaseGateEvaluation(null, null, null);",
                        "} catch (error) {",
                        "  process.stdout.write(JSON.stringify({",
                        "    code: error.code, detail: error.detail, path: error.path,",
                        "  }));",
                        "}",
                    )
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                ["node", str(runner)],
                check=True,
                capture_output=True,
                encoding="utf-8",
            )
        self.assertEqual(expected, json.loads(completed.stdout))

    def test_sample_set_is_a_closed_public_included_excluded_union(self) -> None:
        schema = self.contract.schema("release-gate-sample-set/v1")
        self.assertIs(False, schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        samples = schema["properties"]["samples"]["items"]
        self.assertEqual(
            ["EXCLUDED", "INCLUDED"],
            sorted(
                branch["properties"]["disposition"]["const"]
                for branch in samples["oneOf"]
            ),
        )

        golden = self.contract.fixture(
            "release_gate_sample_set_golden_bootstrap_included"
        )
        self.assertEqual(
            golden["document"],
            self.contract.validate_document(
                "release-gate-sample-set/v1", golden["document"]
            ),
        )
        negative = self.contract.fixture(
            "release_gate_sample_set_negative_excluded_without_reason"
        )
        with self.assertRaises(self.contract.ContractValidationError) as raised:
            self.contract.validate_document(
                "release-gate-sample-set/v1", negative["document"]
            )
        self.assertEqual(negative["expected_code"], raised.exception.code)

    def test_sample_identity_and_document_digest_are_deterministic(self) -> None:
        golden = self.contract.fixture(
            "release_gate_sample_set_golden_bootstrap_included"
        )["document"]
        self.assertEqual(
            golden,
            self.contract.verify_document_digest(
                "release-gate-sample-set/v1", golden
            ),
        )

        tampered = deepcopy(golden)
        tampered.pop("sample_set_digest")
        tampered["samples"][0]["seed"] = 202
        with self.assertRaises(self.contract.ContractValidationError) as raised:
            self.contract.seal_document(
                "release-gate-sample-set/v1", tampered
            )
        self.assertEqual("RELEASE_SAMPLE_ID_INVALID", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
