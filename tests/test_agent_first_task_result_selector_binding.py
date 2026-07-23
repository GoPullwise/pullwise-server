from __future__ import annotations

from copy import deepcopy
import unittest

from tests import test_agent_first_result_debug_transport_adversarial as adversarial
from tests.agent_first_task_result_selector_support import (
    bind_task_result_to_terminal_decision,
)


class AgentFirstTaskResultSelectorBindingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        adversarial.AgentFirstResultDebugTransportAdversarialTest.setUpClass()
        cls.builder = adversarial.AgentFirstResultDebugTransportAdversarialTest(
            "test_all_nine_task_result_outcomes_match_across_runtimes"
        )

    def bound_result(
        self,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        return bind_task_result_to_terminal_decision(
            self.builder.facade,
            self.builder.task_result_branch("BLOCKED"),
        )

    def operation(
        self,
        result: dict[str, object],
        decision: dict[str, object] | None,
        ledger: dict[str, object],
    ) -> dict[str, object]:
        return {
            "python": "verify_task_result_context",
            "node": "verifyTaskResultContext",
            "args": [result],
            "kwargs": {
                "terminal_gate_decision": decision,
                "effect_ledger_snapshot": ledger,
            },
        }

    def test_terminal_gate_decision_binds_task_result_selector(self) -> None:
        result, decision, ledger = self.bound_result()
        expected = [{"ok": True, "value": result}]
        operations = [self.operation(result, decision, ledger)]

        self.assertEqual(expected, self.builder.facade.python_helper_results(operations))
        self.assertEqual(expected, self.builder.facade.node_helper_results(operations))

    def test_selector_context_mismatches_fail_closed_with_parity(self) -> None:
        result, decision, ledger = self.bound_result()
        missing = self.operation(result, None, ledger)
        bad_ref = deepcopy(result)
        bad_ref["gate_decision"]["ref"]["sha256"] = "0" * 64
        bad_task = deepcopy(result)
        bad_task["task_id"] = "task_22222222222222222222222222222222"
        success_decision = self.builder.document("gate_decision_golden_success")
        non_terminal = deepcopy(result)
        non_terminal["gate_decision"]["ref"] = self.builder.facade.content_ref(
            "art_f0000000000000000000000000000001",
            "gate-decision/v1",
            success_decision,
        )
        bad_version = deepcopy(result)
        bad_version["published_from_version"] -= 1
        bad_version["terminal_task_version"] -= 1
        bad_outcome = self.builder.task_result_branch("FAILED")
        for field in ("task_id", "published_from_version", "terminal_task_version",
                      "gate_decision", "selector_input_digest"):
            bad_outcome[field] = deepcopy(result[field])
        bad_reason = deepcopy(result)
        bad_reason["reason_code"] = "APPROVAL_REQUIRED"
        bad_digest = deepcopy(result)
        bad_digest["selector_input_digest"] = "0" * 64
        operations = [
            missing,
            self.operation(bad_ref, decision, ledger),
            self.operation(bad_task, decision, ledger),
            self.operation(non_terminal, success_decision, ledger),
            self.operation(bad_version, decision, ledger),
            self.operation(bad_outcome, decision, ledger),
            self.operation(bad_reason, decision, ledger),
            self.operation(bad_digest, decision, ledger),
        ]
        expected = [
            {"ok": False, "code": code, "detail": detail, "path": path}
            for code, detail, path in (
                ("CONTRACT_DOCUMENT_INVALID", "TASK_RESULT_CONTEXT_INVALID", "$.gate_decision.ref"),
                ("CAS_CORRUPT", "CAS_CORRUPT", "$.gate_decision.ref"),
                ("CONTRACT_DOCUMENT_INVALID", "TASK_RESULT_CONTEXT_INVALID", "$.task_id"),
                ("CONTRACT_DOCUMENT_INVALID", "TASK_RESULT_CONTEXT_INVALID", "$.gate_decision"),
                ("CONTRACT_DOCUMENT_INVALID", "TASK_RESULT_CONTEXT_INVALID", "$.published_from_version"),
                ("CONTRACT_DOCUMENT_INVALID", "TASK_RESULT_CONTEXT_INVALID", "$.outcome"),
                ("CONTRACT_DOCUMENT_INVALID", "TASK_RESULT_CONTEXT_INVALID", "$.reason_code"),
                ("CONTRACT_DOCUMENT_INVALID", "TASK_RESULT_CONTEXT_INVALID", "$.selector_input_digest"),
            )
        ]

        self.assertEqual(expected, self.builder.facade.python_helper_results(operations))
        self.assertEqual(expected, self.builder.facade.node_helper_results(operations))


if __name__ == "__main__":
    unittest.main()
