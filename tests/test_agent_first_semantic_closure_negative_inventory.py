from __future__ import annotations

from collections import Counter
import unittest

from tests.agent_first_semantic_closure_support import SemanticClosureHarness
from tests.test_agent_first_semantic_closure_negative_gate_result import (
    OWNED_RULE_IDS as GATE_RESULT_RULE_IDS,
    build_gate_result_negative_cases,
)
from tests.test_agent_first_semantic_closure_negative_source_tool import (
    OWNED_RULE_IDS as SOURCE_TOOL_RULE_IDS,
    build_source_tool_negative_cases,
)
from tests.test_agent_first_semantic_closure_negative_task_control import (
    OWNED_RULE_IDS as TASK_CONTROL_RULE_IDS,
    build_task_control_negative_cases,
)


class AgentFirstSemanticClosureNegativeInventoryTest(
    SemanticClosureHarness, unittest.TestCase
):
    def test_every_live_document_rule_has_exactly_one_targeted_negative_case(
        self,
    ) -> None:
        task_control_cases = build_task_control_negative_cases(self)
        source_tool_cases = build_source_tool_negative_cases(self)
        gate_result_cases = build_gate_result_negative_cases(self)

        self.assertEqual(
            TASK_CONTROL_RULE_IDS,
            {case["rule_id"] for case in task_control_cases},
        )
        self.assertEqual(
            SOURCE_TOOL_RULE_IDS,
            {case["rule_id"] for case in source_tool_cases},
        )
        self.assertEqual(
            GATE_RESULT_RULE_IDS,
            {case["rule_id"] for case in gate_result_cases},
        )
        self.assertFalse(TASK_CONTROL_RULE_IDS & SOURCE_TOOL_RULE_IDS)
        self.assertFalse(TASK_CONTROL_RULE_IDS & GATE_RESULT_RULE_IDS)
        self.assertFalse(SOURCE_TOOL_RULE_IDS & GATE_RESULT_RULE_IDS)

        cases = task_control_cases + source_tool_cases + gate_result_cases
        counts = Counter(case["rule_id"] for case in cases)
        inventory_rule_ids = set(self.rule_inventory)
        self.assertEqual(
            inventory_rule_ids,
            set(counts),
            "missing=%s extra=%s"
            % (sorted(inventory_rule_ids - set(counts)), sorted(set(counts) - inventory_rule_ids)),
        )
        duplicates = {rule_id: count for rule_id, count in counts.items() if count != 1}
        self.assertFalse(duplicates, f"duplicates={duplicates}")

        for case in cases:
            rule_id = case["rule_id"]
            with self.subTest(rule_id=rule_id, fixture_id=case["fixture_id"]):
                self.assertIsInstance(case["fixture_id"], str)
                self.assertTrue(case["fixture_id"])
                self.assertIsInstance(case["semantic_document"], dict)
                expected = case["expected"]
                self.assertIsInstance(expected, dict)
                self.assertEqual({"ok", "code", "detail", "path"}, set(expected))
                self.assertIs(expected["ok"], False)
                self.assertIn(expected["code"], self.stable_error_codes)
                self.assertIsInstance(expected["detail"], str)
                self.assertTrue(expected["detail"])
                self.assertIsInstance(expected["path"], str)
                self.assertTrue(self.rule_inventory[rule_id])


if __name__ == "__main__":
    unittest.main()
