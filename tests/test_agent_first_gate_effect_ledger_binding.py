from __future__ import annotations

from copy import deepcopy
import unittest

from tests.test_agent_first_gate_decision_facades import (
    AgentFirstGateDecisionFacadesTest,
)


class AgentFirstGateEffectLedgerBindingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        AgentFirstGateDecisionFacadesTest.setUpClass()
        cls.harness = AgentFirstGateDecisionFacadesTest(
            "test_helpers_aggregate_exact_inputs_and_seal_idempotently"
        )

    def test_terminal_gate_binds_cas_task_active_and_derived_state(self) -> None:
        base_snapshot, base_context = self.harness.terminal_inputs()

        def bind(
            states: list[str],
            *,
            ledger_task_id: str | None = None,
        ) -> tuple[dict[str, object], dict[str, object]]:
            snapshot = deepcopy(base_snapshot)
            context = deepcopy(base_context)
            ledger = self.harness.effect_ledger(
                ledger_task_id or snapshot["task_id"],
                states,
            )
            snapshot["effect_ledger_ref"] = self.harness.snapshot_ref(
                ledger,
                "art_f4000000000000000000000000000001",
            )
            snapshot = self.harness.reseal(
                "terminalization-input-snapshot/v1",
                snapshot,
            )
            context["input_snapshot_ref"] = self.harness.snapshot_ref(
                snapshot,
                "art_f4000000000000000000000000000002",
            )
            context["effect_availability"] = {
                "availability": "available",
                "ref": deepcopy(snapshot["effect_ledger_ref"]),
            }
            context["effect_ledger"] = ledger
            return snapshot, context

        committed_snapshot, committed = bind(["COMMITTED"])
        committed.update(
            {
                "cancel_state": "user_cancelled",
                "effect_state": "committed",
                "cause_family": "none",
            }
        )
        cas_snapshot, cas = bind(["COMMITTED"])
        cas["effect_ledger"] = self.harness.effect_ledger(
            cas_snapshot["task_id"],
            [],
        )
        wrong_task_snapshot, wrong_task = bind(
            [],
            ledger_task_id="task_22222222222222222222222222222222",
        )
        active_snapshot, active = bind(["PREPARED"])
        mismatch_snapshot, mismatch = bind(["COMMITTED"])

        results = self.harness.assert_operation_parity(
            [
                {
                    "kind": "terminal",
                    "snapshot": committed_snapshot,
                    "context": committed,
                },
                {"kind": "terminal", "snapshot": cas_snapshot, "context": cas},
                {
                    "kind": "terminal",
                    "snapshot": wrong_task_snapshot,
                    "context": wrong_task,
                },
                {
                    "kind": "terminal",
                    "snapshot": active_snapshot,
                    "context": active,
                },
                {
                    "kind": "terminal",
                    "snapshot": mismatch_snapshot,
                    "context": mismatch,
                },
            ]
        )

        self.assertTrue(results[0]["ok"], results)
        self.assertEqual(
            "CANCELLED_WITH_EFFECTS",
            results[0]["value"]["selected_outcome"],
        )
        self.assertEqual(
            [
                ("CAS_CORRUPT", "$.context.effect_ledger"),
                (
                    "GATE_TERMINAL_EFFECT_LEDGER_TASK_INVALID",
                    "$.context.effect_ledger.task_id",
                ),
                (
                    "GATE_TERMINAL_ACTIVE_EFFECTS",
                    "$.context.effect_ledger.state_counts",
                ),
                (
                    "GATE_TERMINAL_EFFECT_STATE_INVALID",
                    "$.context.effect_state",
                ),
            ],
            [(item["detail"], item["path"]) for item in results[1:]],
        )


if __name__ == "__main__":
    unittest.main()
