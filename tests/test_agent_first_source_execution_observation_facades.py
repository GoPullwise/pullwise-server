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
FAMILY_ROOT = ROOT / "contracts/agent-first/current/source/families"
FAMILY_FILES = (
    "core.json",
    "change-set-patch.json",
    "change-set.json",
    "execution-profile.json",
    "execution-state.json",
    "source-state.json",
    "task-result-identities.json",
    "task-observation-manifests.json",
)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class AgentFirstSourceExecutionObservationFacadesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.families = [
            json.loads((FAMILY_ROOT / name).read_text(encoding="utf-8"))
            for name in FAMILY_FILES
        ]
        cls.family_by_id = {
            family["family_id"]: family for family in cls.families
        }
        canonical = canonical_bytes({"families": cls.families})
        python_bytes = render_python_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            "1" * 64,
            canonical,
        )
        cls.python = types.ModuleType("_source_execution_observation_python")
        exec(python_bytes, cls.python.__dict__)
        cls.npm = render_npm_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            "1" * 64,
            canonical,
        )

    def python_results(
        self, cases: list[tuple[str, dict[str, object]]]
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for schema_id, document in cases:
            try:
                value = self.python.validate_document(schema_id, document)
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
        self, cases: list[tuple[str, dict[str, object]]]
    ) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory(prefix="source-state-facade-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade_path.as_uri())};",
                        f"const cases = {json.dumps(cases, separators=(',', ':'))};",
                        "const results = cases.map(([schemaId, document]) => {",
                        "  try {",
                        "    return {ok: true, value: facade.validateDocument(",
                        "      schemaId, document",
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

    def assert_parity(
        self, cases: list[tuple[str, dict[str, object]]]
    ) -> list[dict[str, object]]:
        python = self.python_results(cases)
        self.assertEqual(python, self.node_results(cases))
        return python

    def family_fixture_cases(
        self, family_id: str
    ) -> tuple[list[dict[str, object]], list[tuple[str, dict[str, object]]]]:
        fixtures = self.family_by_id[family_id]["fixtures"]
        cases = [
            (fixture["schema_id"], deepcopy(fixture["document"]))
            for fixture in fixtures
        ]
        return fixtures, cases

    def test_change_set_fixtures_execute_through_both_facades(self) -> None:
        fixtures, cases = self.family_fixture_cases("change-set")

        results = self.assert_parity(cases)

        for fixture, (_, document), result in zip(fixtures, cases, results):
            expected_code = fixture["expected_code"]
            self.assertEqual(
                expected_code,
                None if result["ok"] else result["code"],
                fixture["fixture_id"],
            )
            if expected_code is None:
                self.assertEqual(document, result["value"])
        self.assertEqual(
            canonical_bytes(results[0]["value"]),
            canonical_bytes(results[1]["value"]),
        )


if __name__ == "__main__":
    unittest.main()
