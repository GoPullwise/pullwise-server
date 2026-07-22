from __future__ import annotations

from copy import deepcopy
import hashlib
import inspect
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
TERMINAL_SCHEMA_ID = "terminalization-fact/v1"


class AgentFirstGatePreparationFacadesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.family = cls.load_family("gate-preparation")
        cls.fixtures = {
            item["fixture_id"]: item for item in cls.family["fixtures"]
        }
        error_family = cls.load_family("receipt-error")
        error_fixture = next(
            item
            for item in error_family["fixtures"]
            if item["fixture_id"] == "error_golden_current_registry"
        )
        canonical = json.dumps(
            {
                "root_manifest": {"schema_registry": []},
                "families": [
                    cls.load_family("core"),
                    cls.load_family("task-result-identities"),
                    cls.family,
                    {
                        "family_id": "receipt-error",
                        "schemas": [],
                        "fixtures": [error_fixture],
                    },
                ],
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        python_wrapper = render_python_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            hashlib.sha256(canonical).hexdigest(),
            canonical,
        )
        cls.python = types.ModuleType("_gate_preparation_python_facade")
        exec(python_wrapper, cls.python.__dict__)
        cls.npm_wrapper = render_npm_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            hashlib.sha256(canonical).hexdigest(),
            canonical,
        )

    @staticmethod
    def load_family(family_id: str) -> dict[str, object]:
        return json.loads(
            (FAMILY_ROOT / f"{family_id}.json").read_text(encoding="utf-8")
        )

    def document(self, fixture_id: str) -> dict[str, object]:
        return deepcopy(self.fixtures[fixture_id]["document"])

    def test_all_family_fixtures_have_python_and_node_parity(self) -> None:
        operations = [
            {
                "kind": "document",
                "schema_id": fixture["schema_id"],
                "document": fixture["document"],
            }
            for fixture in self.family["fixtures"]
        ]

        python_results = self.python_results(operations)
        node_results = self.node_results(operations)

        self.assertEqual(python_results, node_results)
        for result, fixture in zip(python_results, self.family["fixtures"]):
            with self.subTest(fixture_id=fixture["fixture_id"]):
                if fixture["fixture_class"] == "negative":
                    self.assertEqual(
                        {
                            "ok": False,
                            "code": fixture["expected_code"],
                            "detail": result["detail"],
                            "path": result["path"],
                        },
                        result,
                    )
                else:
                    self.assertEqual(
                        {"ok": True, "value": fixture["document"]},
                        result,
                    )

    def test_declared_rules_reject_semantic_drift_with_stable_parity(self) -> None:
        debug_pointer_order = self.document("gate_preparation_golden_debug_plan")
        debug_pointer_order["allowed_json_pointers"].reverse()

        debug_rule_order = self.document("gate_preparation_golden_debug_plan")
        debug_rule_order["rule_ids"].reverse()

        debug_ref_order = self.document("gate_preparation_golden_debug_plan")
        debug_ref_order["debug_input_refs"].append(
            self.content_ref("1", "execution-state-manifest/v1")
        )

        publication_count = self.document(
            "gate_preparation_golden_publication_manifest"
        )
        publication_count["entry_count"] = 1

        publication_order = self.document(
            "gate_preparation_golden_publication_manifest"
        )
        publication_order["entries"].reverse()

        publication_policy = self.document(
            "gate_preparation_golden_publication_manifest"
        )
        publication_policy["entries"][0]["redaction_receipt"][
            "policy_digest"
        ] = "1" * 64

        publication_source = self.document(
            "gate_preparation_golden_publication_manifest"
        )
        publication_source["entries"][1]["redaction_receipt"][
            "original_sha256"
        ] = "f" * 64

        terminal_key = self.document(
            "gate_preparation_golden_terminalization_fact"
        )
        terminal_key["idempotency_key"] = "terminalize:deadline_reached:7"

        terminal_actor = self.document(
            "gate_preparation_golden_terminalization_fact"
        )
        terminal_actor["source"] = {
            "schema_id": "actor/v1",
            "kind": "user_control",
            "id": "user-control",
            "session_id": None,
        }

        terminal_ref_order = self.document(
            "gate_preparation_golden_terminalization_fact"
        )
        terminal_ref_order["evidence_refs"].insert(
            0, self.content_ref("1", "stable-error/v1")
        )

        terminal_budget = self.document(
            "gate_preparation_golden_terminalization_fact"
        )
        terminal_budget["evidence_refs"] = [
            self.content_ref("1", "stable-error/v1")
        ]

        cases = [
            (
                "debug-redaction-plan/v1",
                self.reseal("debug-redaction-plan/v1", debug_pointer_order),
                "DEBUG_REDACTION_POINTER_ORDER_INVALID",
                "$.allowed_json_pointers",
            ),
            (
                "debug-redaction-plan/v1",
                self.reseal("debug-redaction-plan/v1", debug_rule_order),
                "DEBUG_REDACTION_RULE_ORDER_INVALID",
                "$.rule_ids",
            ),
            (
                "debug-redaction-plan/v1",
                self.reseal("debug-redaction-plan/v1", debug_ref_order),
                "DEBUG_REDACTION_INPUT_ORDER_INVALID",
                "$.debug_input_refs",
            ),
            (
                "publication-content-manifest/v1",
                self.reseal(
                    "publication-content-manifest/v1", publication_count
                ),
                "PUBLICATION_ENTRY_COUNT_INVALID",
                "$.entry_count",
            ),
            (
                "publication-content-manifest/v1",
                self.reseal(
                    "publication-content-manifest/v1", publication_order
                ),
                "PUBLICATION_ENTRY_ORDER_INVALID",
                "$.entries",
            ),
            (
                "publication-content-manifest/v1",
                self.reseal(
                    "publication-content-manifest/v1", publication_policy
                ),
                "PUBLICATION_REDACTION_POLICY_INVALID",
                "$.entries[0].redaction_receipt.policy_digest",
            ),
            (
                "publication-content-manifest/v1",
                self.reseal(
                    "publication-content-manifest/v1", publication_source
                ),
                "PUBLICATION_REDACTION_SOURCE_INVALID",
                "$.entries[1].redaction_receipt.original_sha256",
            ),
            (
                TERMINAL_SCHEMA_ID,
                self.reseal(TERMINAL_SCHEMA_ID, terminal_key),
                "TERMINALIZATION_IDEMPOTENCY_KEY_INVALID",
                "$.idempotency_key",
            ),
            (
                TERMINAL_SCHEMA_ID,
                self.reseal(TERMINAL_SCHEMA_ID, terminal_actor),
                "TERMINALIZATION_ACTOR_INVALID",
                "$.source.kind",
            ),
            (
                TERMINAL_SCHEMA_ID,
                self.reseal(TERMINAL_SCHEMA_ID, terminal_ref_order),
                "TERMINALIZATION_EVIDENCE_ORDER_INVALID",
                "$.evidence_refs",
            ),
            (
                TERMINAL_SCHEMA_ID,
                self.reseal(TERMINAL_SCHEMA_ID, terminal_budget),
                "TERMINALIZATION_BUDGET_EVIDENCE_REQUIRED",
                "$.evidence_refs",
            ),
        ]
        operations = [
            {"kind": "document", "schema_id": schema_id, "document": document}
            for schema_id, document, _, _ in cases
        ]

        python_results = self.python_results(operations)
        node_results = self.node_results(operations)

        self.assertEqual(python_results, node_results)
        for result, (_, _, detail, path) in zip(python_results, cases):
            self.assertEqual(
                {
                    "ok": False,
                    "code": "CONTRACT_DOCUMENT_INVALID",
                    "detail": detail,
                    "path": path,
                },
                result,
            )

    def test_terminalization_context_helper_has_a_stable_non_noop_api(self) -> None:
        fact = self.document("gate_preparation_golden_terminalization_fact")
        conflict = deepcopy(fact)
        conflict["observed_at"] = "2026-01-01T00:00:04.000Z"
        conflict = self.reseal(TERMINAL_SCHEMA_ID, conflict)
        corrupt = deepcopy(fact)
        corrupt["observed_at"] = "2026-01-01T00:00:04.000Z"

        valid_operations = [
            self.helper_operation(fact, fact=fact),
            *[
                self.helper_operation(fact, lifecycle_state=lifecycle)
                for lifecycle in (
                    "QUEUED",
                    "ACTIVE",
                    "WAITING_INPUT",
                    "WAITING_APPROVAL",
                    "FINALIZING",
                )
            ],
        ]
        invalid_cases = [
            (
                self.helper_operation(
                    fact, task_id="task_22222222222222222222222222222222"
                ),
                "CONTRACT_DOCUMENT_INVALID",
                "TASK_ID_COLLISION",
                "$.task_id",
            ),
            (
                self.helper_operation(fact, current_task_version=8),
                "TASK_VERSION_STALE",
                "TASK_VERSION_STALE",
                "$.observed_task_version",
            ),
            (
                self.helper_operation(fact, lifecycle_state="TERMINAL"),
                "STATE_TRANSITION_INVALID",
                "STATE_TRANSITION_INVALID",
                "$.lifecycle_state",
            ),
            (
                self.helper_operation(fact, fact=conflict),
                "IDEMPOTENCY_CONFLICT",
                "IDEMPOTENCY_CONFLICT",
                "$.idempotency_key",
            ),
            (
                self.helper_operation(corrupt),
                "CONTRACT_DIGEST_MISMATCH",
                "CONTRACT_DIGEST_MISMATCH",
                "fact_digest",
            ),
        ]
        operations = valid_operations + [item[0] for item in invalid_cases]

        python_results = self.python_results(operations)
        node_results = self.node_results(operations)

        self.assertEqual(python_results, node_results)
        self.assertEqual(
            [{"ok": True, "value": fact}] * len(valid_operations),
            python_results[: len(valid_operations)],
        )
        for result, (_, code, detail, path) in zip(
            python_results[len(valid_operations) :], invalid_cases
        ):
            self.assertEqual(
                {"ok": False, "code": code, "detail": detail, "path": path},
                result,
            )

        helper = self.python.verify_terminalization_fact_context
        self.assertIn("verify_terminalization_fact_context", self.python.__all__)
        self.assertEqual(
            [
                "fact",
                "task_id",
                "current_task_version",
                "lifecycle_state",
                "existing_fact",
            ],
            list(inspect.signature(helper).parameters),
        )
        self.assertIsNone(inspect.signature(helper).parameters["existing_fact"].default)
        self.assertIn("exact idempotency retry", inspect.getdoc(helper))

    @staticmethod
    def helper_operation(
        document: dict[str, object],
        *,
        task_id: str = "task_11111111111111111111111111111111",
        current_task_version: int = 7,
        lifecycle_state: str = "FINALIZING",
        fact: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "kind": "helper",
            "args": [
                document,
                task_id,
                current_task_version,
                lifecycle_state,
                fact,
            ],
        }

    @staticmethod
    def content_ref(seed: str, schema_id: str) -> dict[str, object]:
        return {
            "schema_id": "content-ref/v1",
            "artifact_id": f"art_{seed * 32}",
            "content_schema_id": schema_id,
            "sha256": seed * 64,
            "size_bytes": 1,
            "media_type": "application/json",
            "encoding": "utf-8",
        }

    @staticmethod
    def reseal(schema_id: str, document: dict[str, object]) -> dict[str, object]:
        fields = {
            "debug-redaction-plan/v1": (
                "plan_digest",
                "pullwise:debug-redaction-plan:v1",
            ),
            "publication-content-manifest/v1": (
                "manifest_digest",
                "pullwise:publication-content-manifest:v1",
            ),
            TERMINAL_SCHEMA_ID: (
                "fact_digest",
                "pullwise:terminalization-fact:v1",
            ),
        }
        field, domain = fields[schema_id]
        unsigned = {key: value for key, value in document.items() if key != field}
        canonical = json.dumps(
            unsigned,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        document[field] = hashlib.sha256(
            domain.encode("utf-8") + b"\0" + canonical
        ).hexdigest()
        return document

    def python_results(
        self, operations: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        results = []
        for operation in operations:
            try:
                if operation["kind"] == "document":
                    value = self.python.verify_document_digest(
                        operation["schema_id"], operation["document"]
                    )
                else:
                    value = self.python.verify_terminalization_fact_context(
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
        with tempfile.TemporaryDirectory(prefix="gate-preparation-facade-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(self.npm_wrapper)
            runner_path.write_text(
                "\n".join(
                    (
                        f"import * as facade from {json.dumps(facade_path.as_uri())};",
                        f"const operations = {json.dumps(operations, separators=(',', ':'))};",
                        "if (facade.verify_terminalization_fact_context !==",
                        "    facade.verifyTerminalizationFactContext) {",
                        "  throw new Error('terminalization helper alias mismatch');",
                        "}",
                        "const results = [];",
                        "for (const operation of operations) {",
                        "  try {",
                        "    const value = operation.kind === 'document'",
                        "      ? await facade.verifyDocumentDigest(",
                        "          operation.schema_id, operation.document)",
                        "      : await facade.verifyTerminalizationFactContext(",
                        "          ...operation.args);",
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


if __name__ == "__main__":
    unittest.main()
