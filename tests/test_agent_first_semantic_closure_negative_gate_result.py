from __future__ import annotations

from copy import deepcopy
import unittest

from tests.agent_first_semantic_closure_support import SemanticClosureHarness


OWNED_RULE_IDS = frozenset(
    {
        "completion_proposal",
        "debug_redaction_plan",
        "evidence_closure_manifest",
        "gate_decision",
        "gate_input_snapshot",
        "gate_predicate_registry",
        "observation",
        "observation_manifest",
        "pre_gate_evidence_closure_manifest",
        "pre_gate_root_set",
        "pre_verifier_observation_manifest",
        "publication_content_manifest",
        "quality_policy_plan",
        "benchmark_bundle",
        "release_gate_attestation",
        "release_gate_policy",
        "release_gate_report",
        "release_key_revocation",
        "release_principal",
        "release_signing_key",
        "task_report",
        "task_result",
        "task_result_core",
        "task_result_outcome_reason_registry",
        "task_result_transport_ack",
        "task_result_transport_envelope",
        "terminalization_fact",
        "terminalization_input_snapshot",
        "verification_attestation",
        "verification_attestation_manifest",
        "verifier_input_manifest",
        "verifier_work_report",
        "worker_debug_descriptor",
        "worker_debug_file_manifest",
        "worker_debug_fragment",
        "worker_debug_redaction_report",
    }
)


def build_gate_result_negative_cases(
    harness: SemanticClosureHarness,
) -> list[dict[str, object]]:
    def failure(
        detail: str,
        path: str = "$",
        *,
        code: str = "CONTRACT_DOCUMENT_INVALID",
    ) -> dict[str, object]:
        return {"ok": False, "code": code, "detail": detail, "path": path}

    def case(
        rule_id: str,
        fixture_id: str,
        semantic_document: dict[str, object],
        expected: dict[str, object],
    ) -> dict[str, object]:
        return {
            "rule_id": rule_id,
            "fixture_id": fixture_id,
            "semantic_document": semantic_document,
            "expected": expected,
        }

    debug_plan_fixture = "gate_preparation_golden_debug_plan"
    debug_plan = harness.fixture_document(debug_plan_fixture)
    debug_plan["allowed_json_pointers"].append(
        deepcopy(debug_plan["allowed_json_pointers"][-1])
    )
    debug_plan = harness.reseal("debug-redaction-plan/v1", debug_plan)

    predicate_registry_fixture = "gate_golden_independent_registry"
    predicate_registry = harness.fixture_document(predicate_registry_fixture)
    predicate_registry["predicates"].reverse()
    predicate_registry = harness.reseal(
        "gate-predicate-registry/v1", predicate_registry
    )

    publication_manifest_fixture = "gate_preparation_golden_publication_manifest"
    publication_manifest = harness.fixture_document(publication_manifest_fixture)
    publication_manifest["entry_count"] += 1
    publication_manifest = harness.reseal(
        "publication-content-manifest/v1", publication_manifest
    )

    report_fixture = "publication_golden_report"
    report = harness.fixture_document(report_fixture)
    later_section = deepcopy(report["sections"][0])
    later_section["section_id"] = "section_" + "2" * 32
    report["sections"] = [later_section, report["sections"][0]]
    report = harness.reseal("task-report/v1", report)

    outcome_reason_registry_fixture = (
        "task_result_golden_outcome_reason_registry"
    )
    outcome_reason_registry = harness.fixture_document(
        outcome_reason_registry_fixture
    )
    outcome_reason_registry["reasons"].reverse()
    outcome_reason_registry = harness.reseal(
        "task-result-outcome-reason-registry/v1", outcome_reason_registry
    )

    transport_envelope = deepcopy(
        harness.build_uploaded_documents()["task_result_transport_envelope"]
    )
    transport_envelope["task_result_digest"] = "0" * 64

    debug_fragment_fixture = "worker_debug_transport_fragment_golden_terminal"
    debug_fragment = harness.fixture_document(debug_fragment_fixture)
    debug_fragment["last_server_acked_event_seq"] = (
        debug_fragment["local_event_seq"] + 1
    )

    return [
        case(
            "benchmark_bundle",
            "benchmark_bundle_negative_unsorted_seeds",
            harness.fixture_document("benchmark_bundle_negative_unsorted_seeds"),
            failure("RELEASE_BENCHMARK_ORDER_INVALID", "$.seeds"),
        ),
        case(
            "release_gate_policy",
            "release_gate_policy_negative_bootstrap_relative_required",
            harness.fixture_document(
                "release_gate_policy_negative_bootstrap_relative_required"
            ),
            failure("RELEASE_POLICY_MODE_INVALID", "$.relative_gates"),
        ),
        case(
            "release_gate_report",
            "release_gate_report_negative_exit_verdict_mismatch",
            harness.fixture_document(
                "release_gate_report_negative_exit_verdict_mismatch"
            ),
            failure("RELEASE_REPORT_VERDICT_INVALID", "$.exit_code"),
        ),
        case(
            "release_gate_attestation",
            "release_gate_attestation_negative_validity_window",
            harness.fixture_document(
                "release_gate_attestation_negative_validity_window"
            ),
            failure("RELEASE_ATTESTATION_WINDOW_INVALID", "$.expires_at"),
        ),
        case(
            "release_key_revocation",
            "release_key_revocation_negative_time_order",
            harness.fixture_document(
                "release_key_revocation_negative_time_order"
            ),
            failure("RELEASE_KEY_REVOCATION_TIME_INVALID", "$.effective_at"),
        ),
        case(
            "release_principal",
            "release_principal_negative_time_order",
            harness.fixture_document("release_principal_negative_time_order"),
            failure("RELEASE_PRINCIPAL_TIME_INVALID", "$.expires_at"),
        ),
        case(
            "release_signing_key",
            "release_signing_key_negative_time_order",
            harness.fixture_document("release_signing_key_negative_time_order"),
            failure("RELEASE_SIGNING_KEY_TIME_INVALID", "$.expires_at"),
        ),
        case(
            "completion_proposal",
            "task_completion_negative_no_change_source_drift",
            harness.fixture_document("task_completion_negative_no_change_source_drift"),
            failure("PROPOSAL_NO_CHANGE_STATE_INVALID"),
        ),
        case(
            "debug_redaction_plan",
            debug_plan_fixture,
            debug_plan,
            failure("DEBUG_REDACTION_POINTER_ORDER_INVALID", "$.allowed_json_pointers"),
        ),
        case(
            "evidence_closure_manifest",
            "task_evidence_negative_artifact_alias",
            harness.fixture_document("task_evidence_negative_artifact_alias"),
            failure("EVIDENCE_CLOSURE_CONTENT_ALIAS", "$.entries"),
        ),
        case(
            "gate_decision",
            "gate_decision_negative_cross_branch",
            harness.fixture_document("gate_decision_negative_cross_branch"),
            failure("GATE_PREDICATE_ORDER_INVALID", "$.predicate_results"),
        ),
        case(
            "gate_input_snapshot",
            "gate_input_negative_success_final_closure_ref",
            harness.fixture_document("gate_input_negative_success_final_closure_ref"),
            failure(
                "GATE_INPUT_CLOSURE_DIRECTION_INVALID",
                code="GATE_INPUT_CLOSURE_DIRECTION_INVALID",
            ),
        ),
        case(
            "gate_predicate_registry",
            predicate_registry_fixture,
            predicate_registry,
            failure("GATE_PREDICATE_REGISTRY_INVALID", "$.predicates"),
        ),
        case(
            "observation",
            "task_observation_negative_invalid_instant",
            harness.fixture_document("task_observation_negative_invalid_instant"),
            failure("OBSERVATION_TIME_INVALID"),
        ),
        case(
            "observation_manifest",
            "task_observation_negative_final_manifest_order",
            harness.fixture_document("task_observation_negative_final_manifest_order"),
            failure("OBSERVATION_MANIFEST_ORDER_INVALID", "$.entries"),
        ),
        case(
            "pre_gate_evidence_closure_manifest",
            "pre_gate_negative_evidence_reverse_edge",
            harness.fixture_document("pre_gate_negative_evidence_reverse_edge"),
            failure("PRE_GATE_CLOSURE_DIRECTION_INVALID", "$.entries"),
        ),
        case(
            "pre_gate_root_set",
            "pre_gate_negative_required_policy_unavailable",
            harness.fixture_document("pre_gate_negative_required_policy_unavailable"),
            failure("PRE_GATE_REQUIRED_ROOT_UNAVAILABLE", "$.policy"),
        ),
        case(
            "pre_verifier_observation_manifest",
            "task_observation_negative_pre_manifest_actor",
            harness.fixture_document("task_observation_negative_pre_manifest_actor"),
            failure("OBSERVATION_MANIFEST_ACTOR_INVALID", "$.entries[0].actor.kind"),
        ),
        case(
            "publication_content_manifest",
            publication_manifest_fixture,
            publication_manifest,
            failure("PUBLICATION_ENTRY_COUNT_INVALID", "$.entry_count"),
        ),
        case(
            "quality_policy_plan",
            "quality_policy_negative_q2_duplicate_concern",
            harness.fixture_document("quality_policy_negative_q2_duplicate_concern"),
            failure("QUALITY_POLICY_SLOT_TABLE_INVALID", "$.slots"),
        ),
        case(
            "task_report",
            report_fixture,
            report,
            failure("TASK_REPORT_SECTION_ORDER_INVALID", "$.sections"),
        ),
        case(
            "task_result",
            "task_result_negative_identity_matrix",
            harness.fixture_document("task_result_negative_identity_matrix"),
            failure("TASK_RESULT_IDENTITY_MATRIX_INVALID"),
        ),
        case(
            "task_result_core",
            "task_result_core_negative_debug_field",
            harness.fixture_document("task_result_core_negative_debug_field"),
            failure("TASK_RESULT_CORE_DEBUG_FIELD_INVALID"),
        ),
        case(
            "task_result_outcome_reason_registry",
            outcome_reason_registry_fixture,
            outcome_reason_registry,
            failure("TASK_RESULT_OUTCOME_REASON_REGISTRY_BIJECTION_INVALID"),
        ),
        case(
            "task_result_transport_ack",
            "task_result_transport_ack_negative_binding_matrix",
            harness.fixture_document(
                "task_result_transport_ack_negative_binding_matrix"
            ),
            failure("TRANSPORT_ACK_RECEIPT_MATRIX_INVALID"),
        ),
        case(
            "task_result_transport_envelope",
            "synthetic::uploaded-envelope-wrong-result-digest",
            transport_envelope,
            failure(
                "TRANSPORT_ENVELOPE_DIGEST_INVALID",
                code="TRANSPORT_ENVELOPE_DIGEST_INVALID",
            ),
        ),
        case(
            "terminalization_fact",
            "gate_preparation_negative_terminalization_actor",
            harness.fixture_document(
                "gate_preparation_negative_terminalization_actor"
            ),
            failure("TERMINALIZATION_ACTOR_INVALID", "$.source.kind"),
        ),
        case(
            "terminalization_input_snapshot",
            "gate_input_negative_terminalization_without_fact",
            harness.fixture_document(
                "gate_input_negative_terminalization_without_fact"
            ),
            failure(
                "TERMINALIZATION_FACT_ORDER_INVALID",
                "$.terminalization_fact_refs",
            ),
        ),
        case(
            "verification_attestation",
            "task_attestation_negative_run_status",
            harness.fixture_document("task_attestation_negative_run_status"),
            failure("ATTESTATION_RUN_STATUS_INVALID"),
        ),
        case(
            "verification_attestation_manifest",
            "task_verification_negative_missing_slot",
            harness.fixture_document("task_verification_negative_missing_slot"),
            failure("ATTESTATION_MISSING_SLOT_INVALID"),
        ),
        case(
            "verifier_input_manifest",
            "task_verifier_input_negative_owner_conclusion",
            harness.fixture_document("task_verifier_input_negative_owner_conclusion"),
            failure("VERIFIER_OWNER_CONCLUSION_INCLUDED"),
        ),
        case(
            "verifier_work_report",
            "task_verifier_work_negative_without_observation",
            harness.fixture_document("task_verifier_work_negative_without_observation"),
            failure("VERIFIER_OBSERVATION_REQUIRED"),
        ),
        case(
            "worker_debug_descriptor",
            "worker_debug_transport_descriptor_negative_uploaded_binding",
            harness.fixture_document(
                "worker_debug_transport_descriptor_negative_uploaded_binding"
            ),
            failure("DEBUG_DESCRIPTOR_BINDING_INVALID"),
        ),
        case(
            "worker_debug_file_manifest",
            "worker_debug_content_negative_file_manifest_count",
            harness.fixture_document(
                "worker_debug_content_negative_file_manifest_count"
            ),
            failure("DEBUG_FILE_MANIFEST_COUNT_INVALID"),
        ),
        case(
            "worker_debug_fragment",
            debug_fragment_fixture,
            debug_fragment,
            failure("DEBUG_EVENT_SEQUENCE_INVALID"),
        ),
        case(
            "worker_debug_redaction_report",
            "worker_debug_content_negative_redaction_report_matrix",
            harness.fixture_document(
                "worker_debug_content_negative_redaction_report_matrix"
            ),
            failure("DEBUG_REDACTION_RESCAN_FAILED"),
        ),
    ]


class AgentFirstSemanticClosureNegativeGateResultTest(
    SemanticClosureHarness, unittest.TestCase
):
    def test_gate_and_result_rules_reject_targeted_direct_handler_mutants(
        self,
    ) -> None:
        cases = build_gate_result_negative_cases(self)
        case_rule_ids = [case["rule_id"] for case in cases]

        self.assertEqual(len(OWNED_RULE_IDS), len(cases))
        self.assertEqual(OWNED_RULE_IDS, frozenset(case_rule_ids))
        self.assertEqual(len(case_rule_ids), len(set(case_rule_ids)))
        self.assertFalse(OWNED_RULE_IDS - set(self.rule_inventory))

        python_results = self.python_document_rule_handler_results(cases)
        node_results = self.node_document_rule_handler_results(cases)
        self.assertEqual(python_results, node_results)

        for case, result in zip(cases, python_results):
            with self.subTest(
                rule_id=case["rule_id"], fixture_id=case["fixture_id"]
            ):
                self.assertEqual(case["expected"], result)
                self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
