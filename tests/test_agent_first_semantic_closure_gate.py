from __future__ import annotations

import unittest

from tests.agent_first_semantic_closure_support import SemanticClosureHarness


class AgentFirstSemanticClosureGateTest(
    SemanticClosureHarness, unittest.TestCase
):
    @staticmethod
    def batch_names(names: list[str], size: int) -> list[list[str]]:
        return [names[index:index + size] for index in range(0, len(names), size)]

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
        for index, (case, result) in enumerate(zip(cases, python["results"])):
            fixture_id = case["fixture_id"]
            declared_rule_sequence = list(self.schema_rules(case["schema_id"]))
            validation_passes = (
                2
                if "x-pullwise-digest" in self.schemas[case["schema_id"]]
                else 1
            )
            expected_rules = declared_rule_sequence * validation_passes
            python_case_events = python["case_events"][index]
            node_case_hits = node["case_hits"][index]
            python_root_hits = [
                event["ruleId"]
                for event in python_case_events
                if event["schemaId"] == case["schema_id"]
            ]
            node_root_hits = [
                event["ruleId"]
                for event in node_case_hits
                if event["schemaId"] == case["schema_id"]
            ]
            self.assertTrue(result["ok"], fixture_id)
            self.assertEqual(python_case_events, node_case_hits, fixture_id)
            self.assertEqual(expected_rules, python_root_hits, fixture_id)
            self.assertEqual(expected_rules, node_root_hits, fixture_id)
            self.assertFalse(python["case_failures"][index], fixture_id)
            self.assertFalse(node["case_failures"][index], fixture_id)

        python_hits = set(python["hits"])
        node_hits = {item["ruleId"] for item in node["hits"]}
        self.assertEqual(declared_rules, python_hits)
        self.assertEqual(declared_rules, node_hits)
        self.assertFalse(node_hits - declared_rules)
        for hit in node["hits"]:
            self.assertIn(hit["schemaId"], self.schemas)
            self.assertIn(hit["ruleId"], self.schema_rules(hit["schemaId"]))

    def test_contextual_helpers_export_presence_matches_live_inventory(self) -> None:
        declared_helpers = set(self.helper_inventory)
        self.assertTrue(declared_helpers)
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

    def test_contextual_helpers_positive_execution_is_live_and_parity_safe(self) -> None:
        declared_helpers = set(self.helper_inventory)
        positive = self.positive_helper_operations()
        negative = self.helper_probe_operations()
        self.assertEqual(declared_helpers, set(positive))
        self.assertEqual(declared_helpers, set(negative))

        ordered = [
            "verify_waiver_event_authority",
            "evaluate_success_gate",
            "evaluate_terminalization_gate",
            "validate_attempt_transition",
            "validate_claim_write_set",
        ] + [
            helper_id
            for helper_id in sorted(declared_helpers)
            if helper_id
            not in {
                "verify_waiver_event_authority",
                "evaluate_success_gate",
                "evaluate_terminalization_gate",
                "validate_attempt_transition",
                "validate_claim_write_set",
            }
        ]
        for names in self.batch_names(ordered, 10):
            payload = [positive[helper_id] for helper_id in names]
            python = self.python_helper_results(payload)
            node = self.node_helper_results(payload)
            self.assertEqual(python, node)
            for helper_id, result in zip(names, python):
                if helper_id == "verify_waiver_event_authority":
                    # authorized_waiver_issuers is fixed empty; authority material is not modeled.
                    self.assertEqual(
                        result,
                        {
                            "ok": False,
                            "code": "WAIVER_INVALID",
                            "detail": "WAIVER_ISSUER_NOT_AUTHORIZED",
                            "path": "$",
                        },
                    )
                    continue
                self.assertTrue(result["ok"], helper_id)

    def assert_negative_helper_batch(self, batch_index: int) -> None:
        declared_helpers = set(self.helper_inventory)
        operations = self.helper_probe_operations()
        self.assertEqual(declared_helpers, set(operations))
        names = self.batch_names(sorted(operations), 10)[batch_index]
        payload = [operations[helper_id] for helper_id in names]
        python = self.python_helper_results(payload)
        node = self.node_helper_results(payload)
        self.assertEqual(python, node)
        for helper_id, result in zip(names, python):
            if helper_id == "verify_waiver_event_authority":
                self.assertEqual(
                    result,
                    {
                        "ok": False,
                        "code": "WAIVER_INVALID",
                        "detail": "WAIVER_TIME_INVALID",
                        "path": "$",
                    },
                )
            self.assertFalse(result["ok"], helper_id)
            self.assertIn(result["code"], self.stable_error_codes, helper_id)
            self.assertIsInstance(result["detail"], str, helper_id)
            self.assertTrue(result["detail"], helper_id)
            self.assertIsInstance(result["path"], str, helper_id)

    def test_contextual_helpers_negative_execution_batch_1(self) -> None:
        self.assert_negative_helper_batch(0)

    def test_contextual_helpers_negative_execution_batch_2(self) -> None:
        self.assert_negative_helper_batch(1)

    def test_contextual_helpers_negative_execution_batch_3(self) -> None:
        self.assert_negative_helper_batch(2)

    def test_contextual_helpers_negative_execution_batch_4(self) -> None:
        self.assert_negative_helper_batch(3)


if __name__ == "__main__":
    unittest.main()
