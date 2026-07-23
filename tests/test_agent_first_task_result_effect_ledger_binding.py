from __future__ import annotations

from copy import deepcopy
import hashlib
import unittest

from tests.agent_first_task_result_selector_support import (
    _SELECTOR_FIELDS,
    bind_task_result_to_terminal_decision,
    canonical_bytes,
)
from tests.test_agent_first_result_debug_transport_adversarial import (
    AgentFirstResultDebugTransportAdversarialTest,
)


class AgentFirstTaskResultEffectLedgerBindingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        AgentFirstResultDebugTransportAdversarialTest.setUpClass()
        cls.matrix = AgentFirstResultDebugTransportAdversarialTest(
            "test_all_nine_task_result_outcomes_match_across_runtimes"
        )
        cls.facade = cls.matrix.facade

    def bound(
        self,
        outcome: str,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        return bind_task_result_to_terminal_decision(
            self.facade,
            self.matrix.task_result_branch(outcome),
        )

    def rebind_decision(
        self,
        result: dict[str, object],
        decision: dict[str, object],
        ledger: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        rebound_result = deepcopy(result)
        rebound_decision = deepcopy(decision)
        rebound_decision["effect_availability"] = {
            "availability": "available",
            "ref": self.facade.content_ref(
                "art_f1000000000000000000000000000002",
                "effect-ledger-snapshot/v1",
                ledger,
            ),
        }
        projection = {
            field: rebound_decision[field]
            for field in _SELECTOR_FIELDS
        }
        rebound_decision["selector_input_digest"] = hashlib.sha256(
            b"pullwise:terminal-selector-input:v1\0"
            + canonical_bytes(projection)
        ).hexdigest()
        rebound_decision = self.facade.reseal(
            "gate-decision/v1",
            rebound_decision,
        )
        rebound_result["selector_input_digest"] = rebound_decision[
            "selector_input_digest"
        ]
        rebound_result["gate_decision"]["ref"] = self.facade.content_ref(
            "art_f1000000000000000000000000000001",
            "gate-decision/v1",
            rebound_decision,
        )
        return rebound_result, rebound_decision

    @staticmethod
    def operation(
        result: dict[str, object],
        decision: dict[str, object],
        ledger: dict[str, object] | None,
    ) -> dict[str, object]:
        kwargs = {"terminal_gate_decision": decision}
        if ledger is not None:
            kwargs["effect_ledger_snapshot"] = ledger
        return {
            "python": "verify_task_result_context",
            "node": "verifyTaskResultContext",
            "args": [result],
            "kwargs": kwargs,
        }

    def test_nonempty_terminal_ledgers_bind_with_runtime_parity(self) -> None:
        bound = [
            self.bound(outcome)
            for outcome in (
                "PARTIAL",
                "CANCELLED_WITH_EFFECTS",
                "TERMINATED_WITH_UNKNOWN_EFFECTS",
            )
        ]
        operations = [
            self.operation(result, decision, ledger)
            for result, decision, ledger in bound
        ]
        expected = [
            {"ok": True, "value": result}
            for result, _, _ in bound
        ]
        self.assertEqual(expected, self.facade.python_helper_results(operations))
        self.assertEqual(expected, self.facade.node_helper_results(operations))

    def test_missing_cas_task_count_active_and_state_conflicts_reject(self) -> None:
        partial, partial_decision, partial_ledger = self.bound("PARTIAL")
        _, empty_decision, empty_ledger = self.bound("COMPLETED")

        count_conflict = deepcopy(partial)
        count_conflict["effects"]["committed"] += 1

        wrong_task = deepcopy(empty_ledger)
        wrong_task["task_id"] = "task_22222222222222222222222222222222"
        wrong_task = self.facade.reseal("effect-ledger-snapshot/v1", wrong_task)
        wrong_result, wrong_decision = self.rebind_decision(
            partial,
            partial_decision,
            wrong_task,
        )

        active = deepcopy(empty_ledger)
        active["rows"] = [
            {
                "effect_id": "effect_00000000000000000000000000000001",
                "state": "PREPARED",
            }
        ]
        active["watermark"] = 1
        active["state_counts"]["prepared"] = 1
        active = self.facade.reseal("effect-ledger-snapshot/v1", active)
        active_result, active_decision = self.rebind_decision(
            self.bound("COMPLETED")[0],
            empty_decision,
            active,
        )

        state_result = self.matrix.task_result_branch("COMPLETED")
        state_result["effects"]["committed"] = 1
        state_result, state_decision, state_ledger = (
            bind_task_result_to_terminal_decision(self.facade, state_result)
        )
        state_decision["effect_state"] = "none"
        state_result, state_decision = self.rebind_decision(
            state_result,
            state_decision,
            state_ledger,
        )

        operations = [
            self.operation(partial, partial_decision, None),
            self.operation(partial, partial_decision, empty_ledger),
            self.operation(wrong_result, wrong_decision, wrong_task),
            self.operation(count_conflict, partial_decision, partial_ledger),
            self.operation(active_result, active_decision, active),
            self.operation(state_result, state_decision, state_ledger),
        ]
        python = self.facade.python_helper_results(operations)
        node = self.facade.node_helper_results(operations)

        self.assertEqual(python, node)
        self.assertEqual(
            [
                ("TASK_RESULT_CONTEXT_INVALID", "$.effects"),
                ("CAS_CORRUPT", "$.gate_decision.effect_availability.ref"),
                ("TASK_RESULT_EFFECT_LEDGER_TASK_INVALID", "$.effects"),
                ("TASK_RESULT_EFFECT_COUNTS_INVALID", "$.effects"),
                ("TASK_RESULT_ACTIVE_EFFECTS", "$.effects"),
                (
                    "TASK_RESULT_EFFECT_STATE_INVALID",
                    "$.gate_decision.effect_state",
                ),
            ],
            [(item["detail"], item["path"]) for item in python],
        )


if __name__ == "__main__":
    unittest.main()
