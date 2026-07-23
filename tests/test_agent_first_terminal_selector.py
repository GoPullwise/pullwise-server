from __future__ import annotations

from copy import deepcopy
import hashlib
import unittest

from tests.test_agent_first_gate_decision_facades import (
    AgentFirstGateDecisionFacadesTest,
    GATE_SCHEMA_ID,
    canonical_bytes,
)


AXIS_ENUMS = {
    "profile": ["task_result", "tombstone_pre_fence"],
    "gate_mode": [
        "none",
        "completed",
        "completed_with_waivers",
        "no_change_needed",
    ],
    "cancel_state": [
        "none",
        "user_cancelled",
        "server_cancelled",
        "lease_cancelled",
    ],
    "effect_state": [
        "none",
        "committed",
        "unknown_pre_deadline",
        "unknown_post_deadline",
    ],
    "cause_family": [
        "none",
        "approval_required",
        "input_required",
        "capability_unavailable",
        "environment_unavailable",
        "interaction_unavailable",
        "policy_unsupported",
        "policy_invariant_broken",
        "budget_exhausted",
        "deadline_reached",
        "verification_incomplete",
        "contract_invalid",
        "protocol_failure",
        "quality_gate_failed",
        "runtime_failure",
        "storage_failure",
        "source_mutation_forbidden",
    ],
    "delivery_state": [
        "none",
        "safe_complete",
        "safe_complete_with_waivers",
        "safe_no_change",
        "safe_partial",
    ],
}


class AgentFirstTerminalSelectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        AgentFirstGateDecisionFacadesTest.setUpClass()
        cls.harness = AgentFirstGateDecisionFacadesTest(methodName="runTest")
        cls.schemas = AgentFirstGateDecisionFacadesTest.schemas
        cls.fixtures = AgentFirstGateDecisionFacadesTest.fixtures
        cls.registry = AgentFirstGateDecisionFacadesTest.registry
        cls.terminal = AgentFirstGateDecisionFacadesTest.terminal

    def inputs(
        self,
        **axis_overrides: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        snapshot = deepcopy(
            self.fixtures["gate_input_golden_terminalization_snapshot"]["document"]
        )
        snapshot["deletion_version"] = 4
        snapshot["predicate_registry_digest"] = self.registry["registry_digest"]
        snapshot = self.harness.reseal(
            "terminalization-input-snapshot/v1", snapshot
        )
        axes = {
            "profile": "task_result",
            "gate_mode": "none",
            "cancel_state": "none",
            "effect_state": "none",
            "cause_family": "runtime_failure",
            "delivery_state": "none",
            **axis_overrides,
        }
        context = {
            "input_snapshot_ref": self.harness.snapshot_ref(
                snapshot, "art_f3000000000000000000000000000003"
            ),
            **axes,
            "source_availability": deepcopy(snapshot["final_source"]),
            "evidence_availability": deepcopy(
                self.terminal["evidence_availability"]
            ),
            "effect_availability": {
                "availability": "available",
                "ref": deepcopy(snapshot["effect_ledger_ref"]),
            },
            "predicate_results": deepcopy(self.terminal["predicate_results"]),
        }
        return snapshot, context

    @staticmethod
    def selector_digest(
        snapshot: dict[str, object],
        context: dict[str, object],
        decision: dict[str, object],
    ) -> str:
        projection = {
            "input_digest": snapshot["input_digest"],
            "predicate_registry_digest": snapshot["predicate_registry_digest"],
            "task_id": snapshot["task_id"],
            "task_version": snapshot["task_version"],
            "deletion_version": snapshot["deletion_version"],
            **{field: context[field] for field in AXIS_ENUMS},
            "authoritative_fact_refs": snapshot["terminalization_fact_refs"],
            "source_availability": context["source_availability"],
            "evidence_availability": context["evidence_availability"],
            "effect_availability": context["effect_availability"],
            "predicate_results": context["predicate_results"],
            "selected_lifecycle": decision["selected_lifecycle"],
            "selected_outcome": decision["selected_outcome"],
            "selected_reason": decision["selected_reason"],
        }
        return hashlib.sha256(
            b"pullwise:terminal-selector-input:v1\0"
            + canonical_bytes(projection)
        ).hexdigest()

    def test_source_contract_freezes_six_axes_and_selector_binding(self) -> None:
        gate = self.schemas[GATE_SCHEMA_ID]
        properties = gate["properties"]
        for field, expected in AXIS_ENUMS.items():
            self.assertEqual(expected, properties[field]["enum"], field)
        self.assertEqual(
            ["RECONCILING", "TERMINAL"],
            properties["selected_lifecycle"]["enum"],
        )
        self.assertEqual(
            "^[0-9a-f]{64}$",
            properties["selector_input_digest"]["pattern"],
        )
        terminal = next(
            branch
            for branch in gate["oneOf"]
            if branch["properties"]["decision_kind"].get("const")
            == "terminalization"
        )
        self.assertTrue(
            {
                "task_id",
                "task_version",
                "deletion_version",
                *AXIS_ENUMS,
                "selected_lifecycle",
                "selected_outcome",
                "selected_reason",
                "selector_input_digest",
            }.issubset(terminal["required"])
        )
        snapshot = self.schemas["terminalization-input-snapshot/v1"]
        self.assertIn("deletion_version", snapshot["required"])
        self.assertIn("deletion_version", snapshot["properties"])
        for schema_id in ("task-result/v1", "task-result-core/v1"):
            self.assertIn("selector_input_digest", self.schemas[schema_id]["required"])
            self.assertIn(
                "selector_input_digest", self.schemas[schema_id]["properties"]
            )

    def test_selector_is_mechanical_exhaustive_and_python_node_identical(self) -> None:
        cases = [
            (
                {
                    "effect_state": "unknown_pre_deadline",
                    "cause_family": "none",
                },
                ("RECONCILING", None, None),
            ),
            (
                {
                    "effect_state": "unknown_post_deadline",
                    "cause_family": "deadline_reached",
                },
                (
                    "TERMINAL",
                    "TERMINATED_WITH_UNKNOWN_EFFECTS",
                    "DEADLINE_REACHED",
                ),
            ),
            (
                {
                    "cancel_state": "user_cancelled",
                    "effect_state": "committed",
                    "cause_family": "none",
                },
                ("TERMINAL", "CANCELLED_WITH_EFFECTS", "USER_CANCELLED"),
            ),
            (
                {
                    "cancel_state": "server_cancelled",
                    "cause_family": "none",
                },
                ("TERMINAL", "CANCELLED", "SERVER_CANCELLED"),
            ),
            (
                {
                    "gate_mode": "completed",
                    "effect_state": "committed",
                    "cause_family": "none",
                    "delivery_state": "safe_complete",
                },
                ("TERMINAL", "COMPLETED", "SUCCESS"),
            ),
            (
                {
                    "gate_mode": "completed_with_waivers",
                    "effect_state": "committed",
                    "cause_family": "none",
                    "delivery_state": "safe_complete_with_waivers",
                },
                (
                    "TERMINAL",
                    "COMPLETED_WITH_WAIVERS",
                    "AUTHORIZED_WAIVER",
                ),
            ),
            (
                {
                    "gate_mode": "no_change_needed",
                    "cause_family": "none",
                    "delivery_state": "safe_no_change",
                },
                ("TERMINAL", "NO_CHANGE_NEEDED", "ALREADY_SATISFIED"),
            ),
            (
                {
                    "effect_state": "committed",
                    "cause_family": "budget_exhausted",
                    "delivery_state": "safe_partial",
                },
                ("TERMINAL", "PARTIAL", "BUDGET_EXHAUSTED"),
            ),
            (
                {"cause_family": "approval_required"},
                ("TERMINAL", "BLOCKED", "APPROVAL_REQUIRED"),
            ),
            (
                {"cause_family": "runtime_failure"},
                ("TERMINAL", "FAILED", "RUNTIME_FAILURE"),
            ),
        ]
        inputs = [self.inputs(**axes) for axes, _ in cases]
        operations = [
            {"kind": "terminal", "snapshot": snapshot, "context": context}
            for snapshot, context in inputs
        ]
        results = self.harness.assert_operation_parity(operations)
        self.assertTrue(all(result["ok"] for result in results), results)
        for result, (snapshot, context), (_, expected) in zip(
            results, inputs, cases
        ):
            decision = result["value"]
            self.assertEqual(
                expected,
                (
                    decision["selected_lifecycle"],
                    decision["selected_outcome"],
                    decision["selected_reason"],
                ),
            )
            self.assertEqual(snapshot["task_id"], decision["task_id"])
            self.assertEqual(snapshot["task_version"], decision["task_version"])
            self.assertEqual(
                snapshot["deletion_version"], decision["deletion_version"]
            )
            self.assertEqual(
                self.selector_digest(snapshot, context, decision),
                decision["selector_input_digest"],
            )

    def test_selector_rejects_caller_output_tombstone_and_invalid_combinations(
        self,
    ) -> None:
        snapshot, valid = self.inputs()
        caller_selected = deepcopy(valid)
        caller_selected.update(
            {"selected_outcome": "COMPLETED", "selected_reason": "SUCCESS"}
        )
        _, tombstone = self.inputs(
            profile="tombstone_pre_fence", cause_family="none"
        )
        _, contradictory = self.inputs(
            gate_mode="completed",
            effect_state="unknown_pre_deadline",
            cause_family="none",
            delivery_state="safe_complete",
        )
        results = self.harness.assert_operation_parity(
            [
                {
                    "kind": "terminal",
                    "snapshot": snapshot,
                    "context": caller_selected,
                },
                {"kind": "terminal", "snapshot": snapshot, "context": tombstone},
                {
                    "kind": "terminal",
                    "snapshot": snapshot,
                    "context": contradictory,
                },
            ]
        )
        self.assertEqual(
            [
                "GATE_EVALUATION_CONTEXT_INVALID",
                "GATE_TERMINAL_SELECTOR_TOMBSTONE_PRE_FENCE",
                "GATE_TERMINAL_SELECTOR_COMBINATION_INVALID",
            ],
            [result["detail"] for result in results],
        )
        self.assertTrue(all(not result["ok"] for result in results))


if __name__ == "__main__":
    unittest.main()
