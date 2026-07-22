from __future__ import annotations

import unittest

from tests.agent_first_semantic_closure_support import SemanticClosureHarness


class AgentFirstSemanticClosureGateTest(
    SemanticClosureHarness, unittest.TestCase
):
    def test_document_rules_close_over_live_semantics_with_positive_fixture_parity(
        self,
    ) -> None:
        cases = self.positive_document_cases()
        declared_rules = set(self.rule_inventory)
        covered_rules = {
            rule_id
            for case in cases
            for rule_id in self.schema_rules(case["schema_id"])
        }
        self.assertEqual(
            declared_rules,
            covered_rules,
            f"missing positive document coverage: {sorted(declared_rules - covered_rules)}",
        )

        python = self.python_document_rule_results(cases)
        node = self.node_document_rule_results(cases)
        self.assertEqual(python["results"], node["results"])
        for case, result in zip(cases, python["results"]):
            self.assertTrue(result["ok"], case["fixture_id"])

        python_hits = set(python["hits"])
        node_hits = {item["ruleId"] for item in node["hits"]}
        self.assertEqual(declared_rules, python_hits)
        self.assertEqual(declared_rules, node_hits)
        self.assertFalse(node_hits - declared_rules)
        for hit in node["hits"]:
            self.assertIn(hit["schemaId"], self.schemas)
            self.assertIn(hit["ruleId"], self.schema_rules(hit["schemaId"]))

    def test_contextual_helpers_export_and_fail_closed_from_live_semantics(
        self,
    ) -> None:
        declared_helpers = set(self.helper_inventory)
        self.assertEqual(40, len(declared_helpers))
        self.assertEqual(
            {
                helper_id: {"present": True, "exported": True}
                for helper_id in self.helper_inventory
            },
            self.python_helper_exports(),
        )
        self.assertEqual(
            {
                helper_id: {"snake": True, "camel": True, "same": True}
                for helper_id in self.helper_inventory
            },
            self.node_helper_exports(),
        )

        operations = self.helper_probe_operations()
        self.assertEqual(
            declared_helpers,
            set(operations),
            f"missing helper probes: {sorted(declared_helpers - set(operations))}",
        )
        ordered = [operations[helper_id] for helper_id in sorted(operations)]
        python = self.python_helper_results(ordered)
        node = self.node_helper_results(ordered)
        self.assertEqual(python, node)

        for helper_id, result in zip(sorted(operations), python):
            self.assertFalse(result["ok"], helper_id)
            self.assertIn(result["code"], self.stable_error_codes, helper_id)
            self.assertIn(result["detail"], self.stable_error_codes, helper_id)
            self.assertIsInstance(result["path"], str, helper_id)


if __name__ == "__main__":
    unittest.main()
