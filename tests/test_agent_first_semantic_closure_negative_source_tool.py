from __future__ import annotations

from copy import deepcopy
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
    cases: list[dict[str, object]] = []

    def add(
        rule_id: str,
        fixture_id: str,
        semantic_document: dict[str, object],
        code: str,
        detail: str,
        path: str = "$",
    ) -> None:
        cases.append(
            {
                "rule_id": rule_id,
                "fixture_id": fixture_id,
                "semantic_document": semantic_document,
                "expected": {
                    "ok": False,
                    "code": code,
                    "detail": detail,
                    "path": path,
                },
            }
        )

    actor = harness.fixture_document("task_result_identity_golden_actor")
    actor["kind"] = "system_scheduler"
    add(
        "actor",
        "task_result_identity_golden_actor",
        actor,
        "CONTRACT_DOCUMENT_INVALID",
        "ACTOR_SESSION_INVALID",
    )

    agent_tool_request = harness.synthetic_agent_tool_request()
    agent_tool_request["tool_input"]["relative_path"] = "../secret"
    add(
        "agent_tool_request",
        "synthetic_agent_tool_request",
        agent_tool_request,
        "CONTRACT_DOCUMENT_INVALID",
        "TOOL_SOURCE_PATH_INVALID",
        "$.tool_input.relative_path",
    )

    artifact_content_ref = harness.synthetic_artifact_content_ref()
    artifact_content_ref["ref"]["content_schema_id"] = "task-report/v1"
    add(
        "artifact_content_ref",
        "synthetic_artifact_content_ref",
        artifact_content_ref,
        "CONTRACT_DOCUMENT_INVALID",
        "ARTIFACT_CONTENT_TUPLE_INVALID",
    )

    artifact_registry = harness.fixture_document("publication_golden_artifact_registry")
    artifact_registry["entries"][0]["content_schema_id"] = "task-report/v1"
    artifact_registry = harness.reseal(
        "artifact-content-registry/v1", artifact_registry
    )
    add(
        "artifact_content_registry",
        "publication_golden_artifact_registry",
        artifact_registry,
        "CONTRACT_DOCUMENT_INVALID",
        "ARTIFACT_CONTENT_REGISTRY_INVALID",
    )

    availability_registry = harness.fixture_document(
        "task_result_golden_availability_reason_registry"
    )
    availability_registry["reasons"].reverse()
    availability_registry = harness.reseal(
        "availability-reason-registry/v1", availability_registry
    )
    add(
        "availability_reason_registry",
        "task_result_golden_availability_reason_registry",
        availability_registry,
        "CONTRACT_DOCUMENT_INVALID",
        "AVAILABILITY_REASON_REGISTRY_BIJECTION_INVALID",
    )

    availability_ref = harness.synthetic_availability_ref()
    availability_ref["reason_code"] = harness.fixture_document(
        "task_result_golden_availability_reason_registry"
    )["reasons"][0]
    add(
        "availability_ref",
        "synthetic_availability_ref",
        availability_ref,
        "CONTRACT_DOCUMENT_INVALID",
        "AVAILABILITY_REF_SHAPE_INVALID",
    )

    budget_summary = harness.fixture_document("publication_golden_budget_summary")
    budget_summary["consumed_ms"] = budget_summary["elapsed_limit_ms"] + 1
    budget_summary = harness.reseal("budget-summary/v1", budget_summary)
    add(
        "budget_summary",
        "publication_golden_budget_summary",
        budget_summary,
        "BUDGET_EXHAUSTED",
        "BUDGET_SUMMARY_ELAPSED_INVALID",
    )

    change_set = harness.fixture_document("source_evidence_golden_change_set")
    change_set["original_source_state_id"] = change_set["final_source_state_id"]
    change_set = harness.reseal("change-set/v1", change_set)
    add(
        "change_set",
        "source_evidence_golden_change_set",
        change_set,
        "CONTRACT_DOCUMENT_INVALID",
        "CHANGE_SET_STATE_UNCHANGED",
    )

    patch = harness.fixture_document("source_evidence_golden_patch")
    patch["size_bytes"] += 1
    patch = harness.reseal("change-set-patch/v1", patch)
    add(
        "change_set_patch",
        "source_evidence_golden_patch",
        patch,
        "CONTRACT_DOCUMENT_INVALID",
        "SOURCE_CONTENT_SIZE_MISMATCH",
        "$.size_bytes",
    )

    effect_ledger = harness.fixture_document("publication_golden_effect_ledger")
    effect_ledger["watermark"] = 1
    effect_ledger = harness.reseal("effect-ledger-snapshot/v1", effect_ledger)
    add(
        "effect_ledger_snapshot",
        "publication_golden_effect_ledger",
        effect_ledger,
        "CONTRACT_DOCUMENT_INVALID",
        "EFFECT_LEDGER_NOT_EMPTY",
    )

    add(
        "elapsed_budget_ledger",
        "budget_negative_ledger_elapsed_limit",
        harness.fixture_document("budget_negative_ledger_elapsed_limit"),
        "BUDGET_EXHAUSTED",
        "BUDGET_ELAPSED_LIMIT_EXCEEDED",
    )

    reservation = harness.fixture_document("budget_golden_reservation")
    reservation["started_at"] = "2026-02-30T00:00:00.000Z"
    reservation = harness.reseal("elapsed-budget-reservation/v1", reservation)
    add(
        "elapsed_budget_reservation",
        "budget_golden_reservation",
        reservation,
        "CONTRACT_DOCUMENT_INVALID",
        "BUDGET_RESERVATION_TIME_INVALID",
        "$.started_at",
    )

    settlement = harness.fixture_document("budget_golden_settlement")
    settlement["released_calls"] = 1
    settlement = harness.reseal("elapsed-budget-settlement/v1", settlement)
    add(
        "elapsed_budget_settlement",
        "budget_golden_settlement",
        settlement,
        "CONTRACT_DOCUMENT_INVALID",
        "BUDGET_CALL_CONSERVATION_INVALID",
    )

    add(
        "execution_profile",
        "source_evidence_negative_execution_profile_mutable_image",
        harness.fixture_document(
            "source_evidence_negative_execution_profile_mutable_image"
        ),
        "CONTRACT_DOCUMENT_INVALID",
        "EXECUTION_PROFILE_IMAGE_MUTABLE",
    )
    add(
        "execution_state_manifest",
        "source_evidence_negative_execution_state_order",
        harness.fixture_document("source_evidence_negative_execution_state_order"),
        "CONTRACT_DOCUMENT_INVALID",
        "EXECUTION_TOOLCHAIN_ORDER_INVALID",
        "$.toolchain",
    )

    receipt = harness.fixture_document("tool_golden_local_receipt")
    receipt["elapsed_ms"] += 1
    receipt = harness.reseal("local-tool-receipt/v1", receipt)
    add(
        "local_tool_receipt",
        "tool_golden_local_receipt",
        receipt,
        "CONTRACT_DOCUMENT_INVALID",
        "LOCAL_RECEIPT_TIMING_INVALID",
    )

    r0_payload = harness.fixture_document("tool_golden_r0_payload")
    r0_payload["relative_path"] = "../secret"
    r0_payload = harness.reseal("r0-read-payload/v1", r0_payload)
    add(
        "r0_read_payload",
        "tool_golden_r0_payload",
        r0_payload,
        "CONTRACT_DOCUMENT_INVALID",
        "TOOL_SOURCE_PATH_INVALID",
        "$.relative_path",
    )

    r0_result = harness.fixture_document("tool_golden_r0_result")
    r0_result["source_state_after_id"] = "f" * 64
    r0_result = harness.reseal("r0-read-result/v1", r0_result)
    add(
        "r0_read_result",
        "tool_golden_r0_result",
        r0_result,
        "SOURCE_STATE_CHANGED",
        "SOURCE_STATE_CHANGED",
    )

    source_content = harness.synthetic_source_content()
    source_content["size_bytes"] += 1
    source_content = harness.reseal("source-content/v1", source_content)
    add(
        "source_content",
        "synthetic_source_content",
        source_content,
        "CONTRACT_DOCUMENT_INVALID",
        "SOURCE_CONTENT_SIZE_MISMATCH",
        "$.size_bytes",
    )
    add(
        "source_selection_policy",
        "source_evidence_negative_selection_missing_git",
        harness.fixture_document("source_evidence_negative_selection_missing_git"),
        "CONTRACT_DOCUMENT_INVALID",
        "SOURCE_SELECTION_CONTROL_ROOT_MISSING",
        "$.excluded_control_roots",
    )

    source_state = harness.synthetic_source_state()
    source_state["source_state_id"] = "f" * 64
    add(
        "source_state",
        "synthetic_source_state",
        source_state,
        "CONTRACT_DOCUMENT_INVALID",
        "CONTRACT_DIGEST_MISMATCH",
        "$.source_state_id",
    )

    source_tree = harness.fixture_document("source_evidence_golden_source_tree")
    source_tree["total_bytes"] += 1
    source_tree = harness.reseal("source-tree-manifest/v1", source_tree)
    add(
        "source_tree_manifest",
        "source_evidence_golden_source_tree",
        source_tree,
        "CONTRACT_DOCUMENT_INVALID",
        "SOURCE_TREE_SIZE_INVALID",
        "$.total_bytes",
    )

    catalog = harness.fixture_document("tool_golden_current_catalog")
    catalog["tools"].append(deepcopy(catalog["tools"][0]))
    catalog = harness.reseal("tool-catalog/v1", catalog)
    add(
        "tool_catalog",
        "tool_golden_current_catalog",
        catalog,
        "CONTRACT_DOCUMENT_INVALID",
        "TOOL_CATALOG_ORDER_INVALID",
    )

    capability = harness.fixture_document("tool_golden_dispatch_capability")
    capability["issued_at"] = "2026-02-30T00:00:00.000Z"
    capability = harness.reseal("tool-dispatch-capability/v1", capability)
    add(
        "tool_dispatch_capability",
        "tool_golden_dispatch_capability",
        capability,
        "CONTRACT_DOCUMENT_INVALID",
        "TOOL_CAPABILITY_TIME_INVALID",
        "$.issued_at",
    )

    intent = harness.fixture_document("tool_crash_after_intent")
    intent["tool_input"]["relative_path"] = "../secret"
    intent = harness.reseal("tool-dispatch-intent/v1", intent)
    add(
        "tool_dispatch_intent",
        "tool_crash_after_intent",
        intent,
        "CONTRACT_DOCUMENT_INVALID",
        "TOOL_SOURCE_PATH_INVALID",
        "$.tool_input.relative_path",
    )

    invocation = harness.fixture_document("tool_golden_invocation")
    invocation["tool_input"]["relative_path"] = "../secret"
    invocation = harness.reseal("tool-invocation/v1", invocation)
    add(
        "tool_invocation",
        "tool_golden_invocation",
        invocation,
        "CONTRACT_DOCUMENT_INVALID",
        "TOOL_SOURCE_PATH_INVALID",
        "$.tool_input.relative_path",
    )

    return cases


class AgentFirstSemanticClosureNegativeSourceToolTest(
    SemanticClosureHarness, unittest.TestCase
):
    def test_source_and_tool_rules_have_targeted_negative_runtime_parity(
        self,
    ) -> None:
        cases = build_source_tool_negative_cases(self)
        declared_rules = set(self.rule_inventory)
        covered_rules = {case["rule_id"] for case in cases}

        self.assertEqual(
            OWNED_RULE_IDS,
            declared_rules & OWNED_RULE_IDS,
            f"missing declared rules: {sorted(OWNED_RULE_IDS - declared_rules)}",
        )
        self.assertEqual(OWNED_RULE_IDS, covered_rules)
        self.assertEqual(len(cases), len(OWNED_RULE_IDS))

        python_results = self.python_document_rule_handler_results(cases)
        node_results = self.node_document_rule_handler_results(cases)
        self.assertEqual(python_results, node_results)

        for case, result in zip(cases, python_results):
            with self.subTest(
                rule_id=case["rule_id"], fixture_id=case["fixture_id"]
            ):
                self.assertFalse(result["ok"])
                self.assertEqual(case["expected"], result)
                self.assertIn(result["code"], self.stable_error_codes)


if __name__ == "__main__":
    unittest.main()
