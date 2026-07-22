from __future__ import annotations

import unittest

from tests.agent_first_semantic_closure_support import SemanticClosureHarness


OWNED_RULE_IDS = frozenset(
    {
        "actor",
        "agent_tool_request",
        "artifact_content_ref",
        "artifact_content_registry",
        "availability_reason_registry",
        "availability_ref",
        "budget_summary",
        "change_set",
        "change_set_patch",
        "effect_ledger_snapshot",
        "elapsed_budget_ledger",
        "elapsed_budget_reservation",
        "elapsed_budget_settlement",
        "execution_profile",
        "execution_state_manifest",
        "local_tool_receipt",
        "r0_read_payload",
        "r0_read_result",
        "source_content",
        "source_selection_policy",
        "source_state",
        "source_tree_manifest",
        "tool_catalog",
        "tool_dispatch_capability",
        "tool_dispatch_intent",
        "tool_invocation",
    }
)


def build_source_tool_negative_cases(
    harness: SemanticClosureHarness,
) -> list[dict[str, object]]:
    raise NotImplementedError


class AgentFirstSemanticClosureNegativeSourceToolTest(
    SemanticClosureHarness, unittest.TestCase
):
    def test_source_and_tool_rules_have_targeted_negative_runtime_parity(
        self,
    ) -> None:
        cases = build_source_tool_negative_cases(self)
        self.assertEqual(OWNED_RULE_IDS, {case["rule_id"] for case in cases})


if __name__ == "__main__":
    unittest.main()
