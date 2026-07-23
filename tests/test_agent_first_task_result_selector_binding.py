from __future__ import annotations

from copy import deepcopy
import unittest

from tests import test_agent_first_result_debug_transport_adversarial as adversarial


class AgentFirstTaskResultSelectorBindingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        adversarial.AgentFirstResultDebugTransportAdversarialTest.setUpClass()
        cls.builder = adversarial.AgentFirstResultDebugTransportAdversarialTest(
            "test_all_nine_task_result_outcomes_match_across_runtimes"
        )

    def bound_result(self) -> tuple[dict[str, object], dict[str, object]]:
        decision = self.builder.document("gate_decision_golden_terminalization")
        result = self.builder.task_result_branch("BLOCKED")
        result["task_id"] = decision["task_id"]
        result["published_from_version"] = decision["task_version"]
        result["terminal_task_version"] = decision["task_version"] + 1
        result["reason_code"] = decision["selected_reason"]
        result["selector_input_digest"] = decision["selector_input_digest"]
        result["gate_decision"] = {
            "availability": "available",
            "ref": self.builder.facade.content_ref(
                "art_f0000000000000000000000000000001",
                "gate-decision/v1",
                decision,
            ),
        }
        return result, decision

    def operation(
        self,
        result: dict[str, object],
        decision: dict[str, object] | None,
    ) -> dict[str, object]:
        return {
            "python": "verify_task_result_context",
            "node": "verifyTaskResultContext",
            "args": [result],
            "kwargs": {"terminal_gate_decision": decision},
        }

    def test_terminal_gate_decision_binds_task_result_selector(self) -> None:
        result, decision = self.bound_result()
        expected = [{"ok": True, "value": result}]
        operations = [self.operation(result, decision)]

        self.assertEqual(expected, self.builder.facade.python_helper_results(operations))
        self.assertEqual(expected, self.builder.facade.node_helper_results(operations))

    def test_selector_context_mismatches_fail_closed_with_parity(self) -> None:
        result, decision = self.bound_result()
        missing = self.operation(result, None)
        bad_ref = deepcopy(result)
        bad_ref["gate_decision"]["ref"]["sha256"] = "0" * 64
        bad_version = deepcopy(result)
        bad_version["published_from_version"] -= 1
        bad_version["terminal_task_version"] -= 1
        bad_outcome = self.builder.task_result_branch("FAILED")
        for field in ("task_id", "published_from_version", "terminal_task_version",
                      "gate_decision", "selector_input_digest"):
            bad_outcome[field] = deepcopy(result[field])
        bad_reason = deepcopy(result)
        bad_reason["reason_code"] = "INPUT_REQUIRED"
        bad_digest = deepcopy(result)
        bad_digest["selector_input_digest"] = "0" * 64
        operations = [
            missing,
            self.operation(bad_ref, decision),
            self.operation(bad_version, decision),
            self.operation(bad_outcome, decision),
            self.operation(bad_reason, decision),
            self.operation(bad_digest, decision),
        ]
        expected = [
            {"ok": False, "code": code, "detail": detail, "path": path}
            for code, detail, path in (
                ("CONTRACT_DOCUMENT_INVALID", "TASK_RESULT_CONTEXT_INVALID", "$.gate_decision.ref"),
                ("CAS_CORRUPT", "CAS_CORRUPT", "$.gate_decision.ref"),
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
