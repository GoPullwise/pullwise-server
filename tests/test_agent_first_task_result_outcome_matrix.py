from __future__ import annotations

from copy import deepcopy
import unittest

from tests import test_agent_first_result_debug_transport_adversarial as adversarial


class AgentFirstTaskResultOutcomeMatrixTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        adversarial.AgentFirstResultDebugTransportAdversarialTest.setUpClass()
        cls.builder = adversarial.AgentFirstResultDebugTransportAdversarialTest(
            "test_all_nine_task_result_outcomes_match_across_runtimes"
        )

    def assert_rejected(self, documents: list[dict[str, object]]) -> None:
        cases = [("task-result/v1", document) for document in documents]
        expected = [
            {
                "ok": False,
                "code": "CONTRACT_DOCUMENT_INVALID",
                "detail": "CONTRACT_ONE_OF_INVALID",
                "path": "$",
            }
            for _ in documents
        ]
        python = self.builder.facade.python_document_results(cases)
        node = self.builder.facade.node_document_results(cases)
        operations = [
            {
                "python": "verify_task_result_context",
                "node": "verifyTaskResultContext",
                "args": [document],
            }
            for document in documents
        ]
        self.assertEqual(expected, python)
        self.assertEqual(expected, node)
        self.assertEqual(expected, self.builder.facade.python_helper_results(operations))
        self.assertEqual(expected, self.builder.facade.node_helper_results(operations))

    def test_every_outcome_rejects_an_effect_summary_conflict(self) -> None:
        conflicts = (
            ("COMPLETED", {"unknown": 1}),
            ("NO_CHANGE_NEEDED", {"committed": 1}),
            ("COMPLETED_WITH_WAIVERS", {"unknown": 1}),
            ("PARTIAL", {"committed": 0}),
            ("BLOCKED", {"unknown": 1}),
            ("FAILED", {"committed": 1}),
            ("CANCELLED", {"unknown": 1}),
            ("CANCELLED_WITH_EFFECTS", {"committed": 0}),
            ("TERMINATED_WITH_UNKNOWN_EFFECTS", {"unknown": 0}),
        )
        documents = []
        for outcome, overrides in conflicts:
            document = self.builder.task_result_branch(outcome)
            document["effects"].update(overrides)
            documents.append(document)

        self.assert_rejected(documents)

    def test_new_effect_bearing_outcomes_reject_not_started_identity(self) -> None:
        self.assert_rejected(
            [
                self.builder.task_result_branch(
                    "CANCELLED_WITH_EFFECTS", not_started=True
                ),
                self.builder.task_result_branch(
                    "TERMINATED_WITH_UNKNOWN_EFFECTS", not_started=True
                ),
            ]
        )


if __name__ == "__main__":
    unittest.main()
