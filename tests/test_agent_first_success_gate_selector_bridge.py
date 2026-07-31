from __future__ import annotations

from copy import deepcopy
import hashlib
import unittest

from tests import test_agent_first_gate_decision_facades as gate_facades


GATE_SCHEMA_ID = gate_facades.GATE_SCHEMA_ID
canonical_bytes = gate_facades.canonical_bytes

SELECTOR_FIELDS = {
    "task_id",
    "task_version",
    "deletion_version",
    "profile",
    "gate_mode",
    "cancel_state",
    "effect_state",
    "cause_family",
    "delivery_state",
    "selected_lifecycle",
    "selected_outcome",
    "selected_reason",
    "selector_input_digest",
    "authoritative_fact_refs",
    "source_availability",
    "evidence_availability",
    "effect_availability",
}

SELECTOR_DIGEST_FIELDS = (
    "input_digest",
    "predicate_registry_digest",
    "task_id",
    "task_version",
    "deletion_version",
    "profile",
    "gate_mode",
    "cancel_state",
    "effect_state",
    "cause_family",
    "delivery_state",
    "authoritative_fact_refs",
    "source_availability",
    "evidence_availability",
    "effect_availability",
    "predicate_results",
    "selected_lifecycle",
    "selected_outcome",
    "selected_reason",
)


class AgentFirstSuccessGateSelectorBridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        gate_facades.AgentFirstGateDecisionFacadesTest.setUpClass()
        cls.harness = gate_facades.AgentFirstGateDecisionFacadesTest(
            methodName="runTest"
        )
        cls.schemas = gate_facades.AgentFirstGateDecisionFacadesTest.schemas

    def passed_success_inputs(
        self,
    ) -> tuple[dict[str, object], dict[str, object]]:
        snapshot, context = self.harness.success_inputs()
        snapshot["deletion_version"] = 4
        snapshot = self.harness.reseal("gate-input-snapshot/v1", snapshot)
        context["input_snapshot_ref"] = self.harness.snapshot_ref(
            snapshot,
            "art_f1000000000000000000000000000001",
        )
        return snapshot, context

    def test_source_contract_freezes_passed_success_selector_bridge(self) -> None:
        snapshot = self.schemas["gate-input-snapshot/v1"]
        self.assertIn("deletion_version", snapshot["required"])
        self.assertIn("deletion_version", snapshot["properties"])

        gate = self.schemas[GATE_SCHEMA_ID]
        passed_success = next(
            branch
            for branch in gate["oneOf"]
            if branch["properties"]["decision_kind"].get("const") == "success"
            and branch["properties"]["passed"].get("const") is True
        )
        failed_success = next(
            branch
            for branch in gate["oneOf"]
            if branch["properties"]["decision_kind"].get("const") == "success"
            and branch["properties"]["passed"].get("const") is False
        )
        terminal = next(
            branch
            for branch in gate["oneOf"]
            if branch["properties"]["decision_kind"].get("const")
            == "terminalization"
        )

        self.assertTrue(SELECTOR_FIELDS.issubset(passed_success["required"]))
        self.assertTrue(SELECTOR_FIELDS.isdisjoint(failed_success["required"]))
        self.assertEqual(
            0,
            passed_success["properties"]["authoritative_fact_refs"]["maxItems"],
        )
        self.assertEqual(
            1,
            terminal["properties"]["authoritative_fact_refs"]["minItems"],
        )

    def test_passed_success_gate_enters_only_mechanical_selector_without_fact(
        self,
    ) -> None:
        snapshot, context = self.passed_success_inputs()
        results = self.harness.assert_operation_parity(
            [{"kind": "success", "snapshot": snapshot, "context": context}]
        )

        self.assertTrue(results[0]["ok"], results[0])
        decision = results[0]["value"]
        self.assertEqual(
            {
                "decision_kind": "success",
                "task_id": snapshot["task_id"],
                "task_version": snapshot["task_version"],
                "deletion_version": snapshot["deletion_version"],
                "profile": "task_result",
                "gate_mode": "no_change_needed",
                "cancel_state": "none",
                "effect_state": "none",
                "cause_family": "none",
                "delivery_state": "safe_no_change",
                "selected_lifecycle": "TERMINAL",
                "selected_outcome": "NO_CHANGE_NEEDED",
                "selected_reason": "ALREADY_SATISFIED",
                "authoritative_fact_refs": [],
                "source_availability": {
                    "availability": "available",
                    "ref": snapshot["final_source_ref"],
                },
                "evidence_availability": {
                    "availability": "available",
                    "ref": snapshot["pre_gate_evidence_closure_ref"],
                },
                "effect_availability": {
                    "availability": "available",
                    "ref": snapshot["effect_ledger_ref"],
                },
            },
            {
                field: decision[field]
                for field in (
                    "decision_kind",
                    "task_id",
                    "task_version",
                    "deletion_version",
                    "profile",
                    "gate_mode",
                    "cancel_state",
                    "effect_state",
                    "cause_family",
                    "delivery_state",
                    "selected_lifecycle",
                    "selected_outcome",
                    "selected_reason",
                    "authoritative_fact_refs",
                    "source_availability",
                    "evidence_availability",
                    "effect_availability",
                )
            },
        )
        projection = {
            field: deepcopy(decision[field]) for field in SELECTOR_DIGEST_FIELDS
        }
        expected_digest = hashlib.sha256(
            b"pullwise:terminal-selector-input:v1\0"
            + canonical_bytes(projection)
        ).hexdigest()
        self.assertEqual(expected_digest, decision["selector_input_digest"])


if __name__ == "__main__":
    unittest.main()
