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
FAMILY_ROOT = ROOT / "contracts" / "agent-first" / "current" / "source" / "families"
GATE_SCHEMA_ID = "gate-decision/v1"
REGISTRY_SCHEMA_ID = "gate-predicate-registry/v1"


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class AgentFirstGateDecisionFacadesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source_families = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(FAMILY_ROOT.glob("*.json"))
        ]
        cls.schemas = {
            schema["$id"]: schema
            for family in source_families
            for schema in family["schemas"]
        }
        cls.fixtures = {
            fixture["fixture_id"]: fixture
            for family in source_families
            for fixture in family["fixtures"]
        }
        cls.registry = deepcopy(
            cls.fixtures["gate_golden_independent_registry"]["document"]
        )
        cls.success = deepcopy(
            cls.fixtures["gate_decision_golden_success"]["document"]
        )
        cls.terminal = deepcopy(
            cls.fixtures["gate_decision_golden_terminalization"]["document"]
        )

        # Snapshot semantic validation is owned by a separate facade slice. This
        # probe retains the authoritative snapshot schemas and digest contracts
        # while isolating the GateDecision helper behavior under test.
        facade_families = deepcopy(source_families)
        for family in facade_families:
            for schema in family["schemas"]:
                if schema["$id"] in {
                    "gate-input-snapshot/v1",
                    "terminalization-input-snapshot/v1",
                }:
                    schema.pop("x-pullwise-semantics", None)
                if schema["$id"] == "availability-ref/v1":
                    # Each oneOf branch is already a closed object. Removing
                    # redundant outer object keywords keeps the source schema
                    # equivalent for the intentionally small facade evaluator.
                    schema.pop("type", None)
                    schema.pop("additionalProperties", None)

        canonical = canonical_bytes({"families": facade_families})
        python_bytes = render_python_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            "1" * 64,
            canonical,
        )
        cls.python = types.ModuleType("_gate_decision_python_facade")
        exec(python_bytes, cls.python.__dict__)
        cls.npm = render_npm_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            "0" * 64,
            "1" * 64,
            canonical,
        )

    @classmethod
    def reseal(cls, schema_id: str, value: dict[str, object]) -> dict[str, object]:
        document = deepcopy(value)
        spec = cls.schemas[schema_id]["x-pullwise-digest"]
        field = spec["field"]
        unsigned = {key: item for key, item in document.items() if key != field}
        document[field] = hashlib.sha256(
            spec["domain"].encode("utf-8")
            + b"\0"
            + canonical_bytes(unsigned)
        ).hexdigest()
        return document

    @staticmethod
    def snapshot_ref(snapshot: dict[str, object], artifact: str) -> dict[str, object]:
        raw = canonical_bytes(snapshot)
        return {
            "schema_id": "content-ref/v1",
            "artifact_id": artifact,
            "content_schema_id": snapshot["schema_id"],
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "media_type": "application/json",
            "encoding": "utf-8",
        }

    @classmethod
    def success_inputs(cls) -> tuple[dict[str, object], dict[str, object]]:
        snapshot = deepcopy(
            cls.fixtures["gate_input_golden_success_snapshot"]["document"]
        )
        snapshot["predicate_registry_digest"] = cls.registry["registry_digest"]
        snapshot = cls.reseal("gate-input-snapshot/v1", snapshot)
        context = {
            "input_snapshot_ref": cls.snapshot_ref(
                snapshot, "art_f1000000000000000000000000000001"
            ),
            "predicate_results": deepcopy(cls.success["predicate_results"]),
        }
        return snapshot, context

    @classmethod
    def terminal_inputs(cls) -> tuple[dict[str, object], dict[str, object]]:
        snapshot = deepcopy(
            cls.fixtures["gate_input_golden_terminalization_snapshot"]["document"]
        )
        snapshot["predicate_registry_digest"] = cls.registry["registry_digest"]
        snapshot = cls.reseal("terminalization-input-snapshot/v1", snapshot)
        context = {
            "input_snapshot_ref": cls.snapshot_ref(
                snapshot, "art_f2000000000000000000000000000002"
            ),
            "profile": cls.terminal["profile"],
            "gate_mode": cls.terminal["gate_mode"],
            "cancel_state": cls.terminal["cancel_state"],
            "effect_state": cls.terminal["effect_state"],
            "cause_family": cls.terminal["cause_family"],
            "delivery_state": cls.terminal["delivery_state"],
            "source_availability": deepcopy(snapshot["final_source"]),
            "evidence_availability": deepcopy(cls.terminal["evidence_availability"]),
            "effect_availability": {
                "availability": "available",
                "ref": deepcopy(snapshot["effect_ledger_ref"]),
            },
            "predicate_results": deepcopy(cls.terminal["predicate_results"]),
        }
        return snapshot, context

    def python_validate(self, schema_id: str, value: object) -> dict[str, object]:
        try:
            validated = self.python.validate_document(schema_id, value)
        except self.python.ContractValidationError as error:
            return {
                "ok": False,
                "code": error.code,
                "detail": error.detail,
                "path": error.path,
            }
        return {"ok": True, "value": validated}

    def python_operation(self, operation: dict[str, object]) -> dict[str, object]:
        try:
            if operation["kind"] == "validate":
                value = self.python.validate_document(
                    operation["schema_id"], operation["value"]
                )
            elif operation["kind"] in {"success", "success_snake"}:
                value = self.python.evaluate_success_gate(
                    operation["snapshot"], operation["context"]
                )
            else:
                value = self.python.evaluate_terminalization_gate(
                    operation["snapshot"], operation["context"]
                )
        except self.python.ContractValidationError as error:
            return {
                "ok": False,
                "code": error.code,
                "detail": error.detail,
                "path": error.path,
            }
        return {"ok": True, "value": value}

    def node_operations(
        self, operations: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        with tempfile.TemporaryDirectory(prefix="gate-decision-facade-") as scratch:
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
                        "    let value;",
                        "    if (operation.kind === 'validate') {",
                        "      value = facade.validateDocument(operation.schema_id, operation.value);",
                        "    } else if (operation.kind === 'success') {",
                        "      value = await facade.evaluateSuccessGate(operation.snapshot, operation.context);",
                        "    } else if (operation.kind === 'success_snake') {",
                        "      value = await facade.evaluate_success_gate(operation.snapshot, operation.context);",
                        "    } else if (operation.kind === 'terminal_snake') {",
                        (
                            "      value = await facade.evaluate_terminalization_gate("
                            "operation.snapshot, operation.context);"
                        ),
                        "    } else {",
                        (
                            "      value = await facade.evaluateTerminalizationGate("
                            "operation.snapshot, operation.context);"
                        ),
                        "    }",
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

    def assert_operation_parity(
        self, operations: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        python_results = [self.python_operation(item) for item in operations]
        self.assertEqual(python_results, self.node_operations(operations))
        return python_results

    def test_source_fixtures_have_public_python_and_node_parity(self) -> None:
        fixture_ids = [
            "gate_golden_independent_registry",
            "gate_decision_crash_runtime_failure",
            "gate_decision_fence_lease_invalid",
            "gate_decision_golden_success",
            "gate_decision_golden_terminalization",
            "gate_decision_idempotency_success",
            "gate_decision_negative_cross_branch",
            "gate_decision_negative_missing_predicate",
        ]
        operations = [
            {
                "kind": "validate",
                "schema_id": self.fixtures[fixture_id]["schema_id"],
                "value": deepcopy(self.fixtures[fixture_id]["document"]),
            }
            for fixture_id in fixture_ids
        ]

        results = self.assert_operation_parity(operations)

        self.assertTrue(all(result["ok"] for result in results[:6]))
        consumers: dict[str, set[str]] = {}
        for entry in results[0]["value"]["predicates"]:
            for code in entry["failure_codes"]:
                consumers.setdefault(code, set()).add(entry["predicate_id"])
        self.assertTrue(any(len(predicate_ids) > 1 for predicate_ids in consumers.values()))
        for fixture_id, result in zip(fixture_ids[6:], results[6:]):
            self.assertEqual(
                self.fixtures[fixture_id]["expected_code"],
                result["code"],
                (fixture_id, result),
            )

    def test_registry_and_decision_rules_enforce_exact_frozen_contract(self) -> None:
        registry_order = deepcopy(self.registry)
        registry_order["predicates"][0], registry_order["predicates"][1] = (
            registry_order["predicates"][1],
            registry_order["predicates"][0],
        )
        registry_order = self.reseal(REGISTRY_SCHEMA_ID, registry_order)

        predicate_order = deepcopy(self.success)
        predicate_order["predicate_results"][0], predicate_order["predicate_results"][1] = (
            predicate_order["predicate_results"][1],
            predicate_order["predicate_results"][0],
        )
        predicate_order = self.reseal(GATE_SCHEMA_ID, predicate_order)

        pass_failure = deepcopy(self.success)
        pass_failure["predicate_results"][0]["failure_code"] = "TASK_VERSION_STALE"
        pass_failure = self.reseal(GATE_SCHEMA_ID, pass_failure)

        wrong_code = deepcopy(self.success)
        wrong_code["predicate_results"][0].update(
            {"passed": False, "failure_code": "LEASE_INVALID"}
        )
        wrong_code["passed"] = False
        wrong_code = self.reseal(GATE_SCHEMA_ID, wrong_code)

        wrong_evidence = deepcopy(self.success)
        wrong_evidence["predicate_results"][0]["evidence_refs"][0][
            "content_schema_id"
        ] = "effect-ledger-snapshot/v1"
        wrong_evidence = self.reseal(GATE_SCHEMA_ID, wrong_evidence)

        aggregate = deepcopy(self.success)
        aggregate["passed"] = False
        aggregate = self.reseal(GATE_SCHEMA_ID, aggregate)

        registry_digest = deepcopy(self.success)
        registry_digest["predicate_registry_digest"] = "0" * 64
        registry_digest = self.reseal(GATE_SCHEMA_ID, registry_digest)

        decision_digest = deepcopy(self.success)
        decision_digest["decision_digest"] = "0" * 64

        outcome_reason = deepcopy(self.terminal)
        outcome_reason["selected_outcome"] = "CANCELLED"
        outcome_reason["selected_reason"] = "RUNTIME_FAILURE"
        outcome_reason = self.reseal(GATE_SCHEMA_ID, outcome_reason)

        cases = [
            (REGISTRY_SCHEMA_ID, registry_order, "GATE_PREDICATE_REGISTRY_INVALID", "$.predicates"),
            (GATE_SCHEMA_ID, predicate_order, "GATE_PREDICATE_ORDER_INVALID", "$.predicate_results"),
            (GATE_SCHEMA_ID, pass_failure, "GATE_PREDICATE_RESULT_INVALID", "$.predicate_results[0].failure_code"),
            (GATE_SCHEMA_ID, wrong_code, "GATE_PREDICATE_FAILURE_CODE_INVALID", "$.predicate_results[0].failure_code"),
            (GATE_SCHEMA_ID, wrong_evidence, "GATE_PREDICATE_EVIDENCE_INVALID", "$.predicate_results[0].evidence_refs"),
            (GATE_SCHEMA_ID, aggregate, "GATE_DECISION_PASS_INVALID", "$.passed"),
            (GATE_SCHEMA_ID, registry_digest, "GATE_PREDICATE_REGISTRY_DIGEST_INVALID", "$.predicate_registry_digest"),
            (GATE_SCHEMA_ID, decision_digest, "CONTRACT_DIGEST_MISMATCH", "$.decision_digest"),
            (GATE_SCHEMA_ID, outcome_reason, "CONTRACT_ONE_OF_INVALID", "$"),
        ]
        operations = [
            {"kind": "validate", "schema_id": schema_id, "value": document}
            for schema_id, document, _, _ in cases
        ]

        results = self.assert_operation_parity(operations)

        for result, (_, _, detail, path) in zip(results, cases):
            self.assertEqual(
                {
                    "ok": False,
                    "code": "CONTRACT_DOCUMENT_INVALID",
                    "detail": detail,
                    "path": path,
                },
                result,
            )

    def test_helpers_aggregate_exact_inputs_and_seal_idempotently(self) -> None:
        success_snapshot, success_context = self.success_inputs()
        terminal_snapshot, terminal_context = self.terminal_inputs()
        operations = [
            {"kind": "success", "snapshot": success_snapshot, "context": success_context},
            {"kind": "success", "snapshot": success_snapshot, "context": success_context},
            {"kind": "terminal", "snapshot": terminal_snapshot, "context": terminal_context},
            {"kind": "terminal", "snapshot": terminal_snapshot, "context": terminal_context},
        ]

        results = self.assert_operation_parity(operations)

        self.assertTrue(all(result["ok"] for result in results), results)
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[2], results[3])
        success = results[0]["value"]
        terminal = results[2]["value"]
        self.assertEqual("success", success["decision_kind"])
        self.assertEqual(success_snapshot["input_digest"], success["input_digest"])
        self.assertEqual(success_context["input_snapshot_ref"], success["input_snapshot_ref"])
        self.assertEqual(
            [item["predicate_id"] for item in self.success["predicate_results"]],
            [item["predicate_id"] for item in success["predicate_results"]],
        )
        self.assertEqual("terminalization", terminal["decision_kind"])
        self.assertEqual(
            terminal_snapshot["terminalization_fact_refs"],
            terminal["authoritative_fact_refs"],
        )
        self.assertEqual(self.terminal["selected_reason"], terminal["selected_reason"])
        self.assertEqual(
            terminal["decision_digest"],
            self.python.document_digest(
                GATE_SCHEMA_ID,
                {key: value for key, value in terminal.items() if key != "decision_digest"},
            ),
        )

    def test_helpers_preserve_failure_results_and_reject_open_context(self) -> None:
        snapshot, context = self.success_inputs()
        failed = deepcopy(context)
        failed["predicate_results"][1].update(
            {"passed": False, "failure_code": "LEASE_INVALID", "repairable": False}
        )
        extra = deepcopy(context)
        extra["invented_live_predicate"] = True
        malformed = deepcopy(context)
        malformed["predicate_results"] = {}
        wrong_ref = deepcopy(context)
        wrong_ref["input_snapshot_ref"]["sha256"] = "0" * 64
        operations = [
            {"kind": "success", "snapshot": snapshot, "context": failed},
            {"kind": "success", "snapshot": snapshot, "context": extra},
            {"kind": "success", "snapshot": snapshot, "context": malformed},
            {"kind": "success", "snapshot": snapshot, "context": wrong_ref},
        ]

        results = self.assert_operation_parity(operations)

        self.assertTrue(results[0]["ok"], results[0])
        self.assertFalse(results[0]["value"]["passed"])
        self.assertEqual(
            "LEASE_INVALID",
            results[0]["value"]["predicate_results"][1]["failure_code"],
        )
        self.assertEqual(
            {
                "ok": False,
                "code": "CONTRACT_DOCUMENT_INVALID",
                "detail": "GATE_EVALUATION_CONTEXT_INVALID",
                "path": "$.context",
            },
            results[1],
        )
        self.assertEqual(
            {
                "ok": False,
                "code": "CONTRACT_DOCUMENT_INVALID",
                "detail": "GATE_EVALUATION_CONTEXT_INVALID",
                "path": "$.context.predicate_results",
            },
            results[2],
        )
        self.assertEqual(
            {
                "ok": False,
                "code": "CONTRACT_DOCUMENT_INVALID",
                "detail": "GATE_INPUT_SNAPSHOT_REF_MISMATCH",
                "path": "$.context.input_snapshot_ref",
            },
            results[3],
        )

    def test_public_export_names_include_both_gate_helpers(self) -> None:
        self.assertIn("evaluate_success_gate", self.python.__all__)
        self.assertIn("evaluate_terminalization_gate", self.python.__all__)
        success_snapshot, success_context = self.success_inputs()
        terminal_snapshot, terminal_context = self.terminal_inputs()
        operations = [
            {
                "kind": "success_snake",
                "snapshot": success_snapshot,
                "context": success_context,
            },
            {
                "kind": "terminal_snake",
                "snapshot": terminal_snapshot,
                "context": terminal_context,
            },
        ]
        self.assertTrue(all(item["ok"] for item in self.node_operations(operations)))


if __name__ == "__main__":
    unittest.main()
