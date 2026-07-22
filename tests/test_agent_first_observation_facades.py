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
FAMILY_ROOT = ROOT / "contracts" / "agent-first" / "current" / "source" / "families"
FAMILY_FILES = (
    "core.json",
    "receipt-error.json",
    "task-result-identities.json",
    "task-result-reasons.json",
    "task-observation.json",
)
SCHEMA_ID = "observation/v1"


def canonical_bundle(families: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {"families": families},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def render_facades(
    families: list[dict[str, object]], module_name: str
) -> tuple[types.ModuleType, bytes]:
    canonical = canonical_bundle(families)
    python_bytes = render_python_wrapper(
        "@pullwise/agent-task-contract",
        "0.1.0",
        "0" * 64,
        "1" * 64,
        canonical,
    )
    python = types.ModuleType(module_name)
    exec(python_bytes, python.__dict__)
    npm = render_npm_wrapper(
        "@pullwise/agent-task-contract",
        "0.1.0",
        "0" * 64,
        "1" * 64,
        canonical,
    )
    return python, npm


class AgentFirstObservationFacadesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.families = [
            json.loads((FAMILY_ROOT / name).read_text(encoding="utf-8"))
            for name in FAMILY_FILES
        ]
        cls.family = next(
            family
            for family in cls.families
            if family["family_id"] == "task-observation"
        )
        cls.fixtures = {
            fixture["fixture_id"]: fixture for fixture in cls.family["fixtures"]
        }
        cls.golden = deepcopy(
            cls.fixtures["task_observation_golden_observation"]["document"]
        )
        cls.python, cls.npm = render_facades(
            cls.families, "_observation_python_facade"
        )

        probe_families = deepcopy(cls.families)
        observation_family = next(
            family
            for family in probe_families
            if family["family_id"] == "task-observation"
        )
        observation_schema = observation_family["schemas"][0]
        observation_schema["properties"]["partial_side_effect"] = {
            "type": "boolean"
        }
        identity_family = next(
            family
            for family in probe_families
            if family["family_id"] == "task-result-identities"
        )
        actor_schema = next(
            schema for schema in identity_family["schemas"] if schema["$id"] == "actor/v1"
        )
        del actor_schema["oneOf"]
        cls.probe_python, cls.probe_npm = render_facades(
            probe_families, "_observation_semantic_probe_python_facade"
        )

    def python_results(
        self, facade: types.ModuleType, documents: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for document in documents:
            try:
                value = facade.validate_document(SCHEMA_ID, document)
            except facade.ContractValidationError as error:
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
        self, facade: bytes, documents: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory(prefix="observation-facade-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(facade)
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

    def assert_facade_parity(
        self,
        documents: list[dict[str, object]],
        *,
        python: types.ModuleType | None = None,
        npm: bytes | None = None,
    ) -> list[dict[str, object]]:
        python_results = self.python_results(python or self.python, documents)
        node_results = self.node_results(npm or self.npm, documents)
        self.assertEqual(python_results, node_results)
        return python_results

    def test_source_family_fixtures_execute_with_stable_facade_parity(self) -> None:
        fixture_ids = [fixture["fixture_id"] for fixture in self.family["fixtures"]]
        documents = [deepcopy(self.fixtures[fixture_id]["document"]) for fixture_id in fixture_ids]

        results = self.assert_facade_parity(documents)

        self.assertEqual(
            [
                {"ok": True, "value": documents[0]},
                {"ok": True, "value": documents[1]},
                {
                    "ok": False,
                    "code": "CONTRACT_DOCUMENT_INVALID",
                    "detail": "OBSERVATION_TIME_INVALID",
                    "path": "$",
                },
                {
                    "ok": False,
                    "code": "CONTRACT_DOCUMENT_INVALID",
                    "detail": "CONTRACT_CONST_INVALID",
                    "path": "$.partial_side_effect",
                },
                {
                    "ok": False,
                    "code": "CONTRACT_DOCUMENT_INVALID",
                    "detail": "OBSERVATION_STATUS_MATRIX_INVALID",
                    "path": "$",
                },
            ],
            results,
        )
        for fixture_id, result in zip(fixture_ids, results):
            expected_code = self.fixtures[fixture_id]["expected_code"]
            self.assertEqual(expected_code, None if result["ok"] else result["code"])

    def test_adversarial_status_and_time_matrix_has_exact_parity(self) -> None:
        failed = deepcopy(self.golden)
        failed["status"] = "failed"
        failed["exit_code"] = 1
        failed["result_ref"] = {
            "availability": "unavailable",
            "reason_code": "CAPABILITY_NOT_IMPLEMENTED",
        }

        denied = deepcopy(self.golden)
        for key in ("started_at", "completed_at", "duration_ms", "exit_code"):
            denied[key] = None
        denied["status"] = "policy_denied"
        denied["result_ref"]["ref"]["content_schema_id"] = "error-response/v1"

        control_actor = deepcopy(self.golden)
        control_actor["actor"] = {
            "schema_id": "actor/v1",
            "kind": "worker_control",
            "id": "worker-control-1",
            "session_id": None,
        }

        invalid_cases: list[tuple[dict[str, object], str]] = []

        invalid_day = deepcopy(self.golden)
        invalid_day["completed_at"] = "2026-02-30T00:00:00.005Z"
        invalid_cases.append((invalid_day, "OBSERVATION_TIME_INVALID"))

        reversed_time = deepcopy(self.golden)
        reversed_time["completed_at"] = "2025-12-31T23:59:59.999Z"
        invalid_cases.append((reversed_time, "OBSERVATION_TIME_INVALID"))

        wrong_duration = deepcopy(self.golden)
        wrong_duration["duration_ms"] = 4
        invalid_cases.append((wrong_duration, "OBSERVATION_TIME_INVALID"))

        missing_failed_time = deepcopy(failed)
        missing_failed_time["started_at"] = None
        invalid_cases.append((missing_failed_time, "OBSERVATION_TIME_INVALID"))

        missing_success_result = deepcopy(self.golden)
        missing_success_result["result_ref"] = {
            "availability": "unavailable",
            "reason_code": "CAPABILITY_NOT_IMPLEMENTED",
        }
        invalid_cases.append((missing_success_result, "OBSERVATION_RESULT_REQUIRED"))

        denied_with_time = deepcopy(denied)
        denied_with_time["started_at"] = self.golden["started_at"]
        invalid_cases.append((denied_with_time, "OBSERVATION_STATUS_MATRIX_INVALID"))

        denied_with_read_result = deepcopy(denied)
        denied_with_read_result["result_ref"]["ref"][
            "content_schema_id"
        ] = "r0-read-result/v1"
        invalid_cases.append((denied_with_read_result, "OBSERVATION_STATUS_MATRIX_INVALID"))

        denied_with_mutation = deepcopy(denied)
        denied_with_mutation["source_state_after_id"] = "4" * 64
        invalid_cases.append((denied_with_mutation, "OBSERVATION_STATUS_MATRIX_INVALID"))

        denied_with_execution = deepcopy(denied)
        denied_with_execution["execution_state_id"] = "5" * 64
        invalid_cases.append((denied_with_execution, "OBSERVATION_STATUS_MATRIX_INVALID"))

        valid_documents = [self.golden, failed, denied, control_actor]
        documents = valid_documents + [document for document, _ in invalid_cases]
        results = self.assert_facade_parity(documents)

        self.assertEqual(
            [{"ok": True, "value": document} for document in valid_documents],
            results[: len(valid_documents)],
        )
        for result, (_, detail) in zip(results[len(valid_documents) :], invalid_cases):
            self.assertEqual(
                {
                    "ok": False,
                    "code": "CONTRACT_DOCUMENT_INVALID",
                    "detail": detail,
                    "path": "$",
                },
                result,
            )

    def test_semantic_guard_branches_survive_permissive_schema_probe(self) -> None:
        partial_side_effect = deepcopy(self.golden)
        partial_side_effect["partial_side_effect"] = True

        owner_without_session = deepcopy(self.golden)
        owner_without_session["actor"]["session_id"] = None

        control_with_session = deepcopy(self.golden)
        control_with_session["actor"] = {
            "schema_id": "actor/v1",
            "kind": "worker_control",
            "id": "worker-control-1",
            "session_id": "sess_11111111111111111111111111111111",
        }

        cases = [
            (partial_side_effect, "OBSERVATION_PARTIAL_SIDE_EFFECT"),
            (owner_without_session, "ACTOR_SESSION_INVALID"),
            (control_with_session, "ACTOR_SESSION_INVALID"),
        ]
        results = self.assert_facade_parity(
            [document for document, _ in cases],
            python=self.probe_python,
            npm=self.probe_npm,
        )

        for result, (_, detail) in zip(results, cases):
            self.assertEqual(
                {
                    "ok": False,
                    "code": "CONTRACT_DOCUMENT_INVALID",
                    "detail": detail,
                    "path": "$",
                },
                result,
            )


if __name__ == "__main__":
    unittest.main()
