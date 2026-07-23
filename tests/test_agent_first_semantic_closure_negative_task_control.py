from __future__ import annotations

from copy import deepcopy
import unittest

from tests.agent_first_semantic_closure_support import SemanticClosureHarness


OWNED_RULE_IDS = frozenset(
    {
        "acceptance_source_ids_unique",
        "attempt_state_nullability",
        "attempt_transport_binding_all_or_none",
        "budget_ceiling_consistency",
        "capability_and_delivery_sets_sorted_unique",
        "capability_sets_disjoint_sorted_unique",
        "charter_digest_exact",
        "derived_requirement_shape",
        "entries_normative_ingest_then_append_order",
        "fenced_reason_ownership_loss",
        "head_version_ref_pairs",
        "ledger_digest_exact",
        "owner_state_nullability",
        "policy_digest_exact",
        "requirement_id_source_kind_match",
        "risk_ceiling_current_mvp",
        "root_and_origin_sets_sorted_unique",
        "server_authority_envelope",
        "sorted_unique_active_requirement_ids",
        "sorted_unique_charter_sets",
        "sorted_unique_requirement_links",
        "task_record_transport_binding_all_or_none",
        "terminal_result_shape",
        "transport_abandonment_record",
        "utf8_nfc_byte_limits",
        "waiver_time_order",
    }
)


def build_task_control_negative_cases(
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

    request_fixture = "task_control_golden_task_request"
    request = harness.fixture_document(request_fixture)

    duplicate_source = deepcopy(request)
    duplicate_source["constraints"][0]["source_id"] = (
        duplicate_source["acceptance_criteria"][0]["source_id"]
    )

    unordered_capabilities = deepcopy(request)
    unordered_capabilities["requested_capabilities"] = [
        "source.write",
        "source.read",
    ]

    oversized_utf8 = deepcopy(request)
    oversized_utf8["acceptance_criteria"][0]["statement"] = "茅" * 8193

    policy_fixture = "task_control_golden_effective_policy"
    policy = harness.fixture_document(policy_fixture)

    invalid_budget = deepcopy(policy)
    invalid_budget["terminalization_reserve_ms"] = (
        invalid_budget["budgets"]["wall_ms"] + 1
    )
    invalid_budget = harness.reseal(
        "effective-execution-policy/v1", invalid_budget
    )

    overlapping_capabilities = deepcopy(policy)
    overlapping_capabilities["granted_capabilities"] = [
        "source.read",
        "source.write",
    ]
    overlapping_capabilities = harness.reseal(
        "effective-execution-policy/v1", overlapping_capabilities
    )

    invalid_policy_digest = deepcopy(policy)
    invalid_policy_digest["digest"] = "0" * 64

    invalid_mvp_risk = deepcopy(policy)
    invalid_mvp_risk["quality_risk_floor"] = "Q2"
    invalid_mvp_risk = harness.reseal(
        "effective-execution-policy/v1", invalid_mvp_risk
    )

    unordered_roots = deepcopy(policy)
    unordered_roots["allowed_read_roots"] = ["worker-run", "repository"]
    unordered_roots = harness.reseal(
        "effective-execution-policy/v1", unordered_roots
    )

    attempt_fixture = "task_control_golden_attempt_record"
    attempt = harness.fixture_document(attempt_fixture)

    invalid_attempt_nullability = deepcopy(attempt)
    invalid_attempt_nullability["ended_at"] = "2026-07-22T00:00:02.000Z"

    partial_attempt_transport = deepcopy(attempt)
    partial_attempt_transport["transport_binding"]["lease_id"] = None

    invalid_fenced_reason = deepcopy(attempt)
    invalid_fenced_reason.update(
        state="FENCED",
        ended_at="2026-07-22T00:00:02.000Z",
        termination_reason="FAILED",
    )

    owner_fixture = "task_control_golden_task_owner"
    invalid_owner_nullability = harness.fixture_document(owner_fixture)
    invalid_owner_nullability["ended_at"] = "2026-07-22T00:00:02.000Z"

    record_fixture = "task_control_golden_task_record"
    record = harness.fixture_document(record_fixture)

    invalid_record_head = deepcopy(record)
    invalid_record_head["charter_version"] = 1

    partial_record_transport = deepcopy(record)
    partial_record_transport["lease_id"] = None

    invalid_terminal_shape = deepcopy(record)
    invalid_terminal_shape["lifecycle"] = "TERMINAL"

    authority_fixture = "authority_negative_agent_selected_fence"
    invalid_authority_binding = harness.fixture_document(authority_fixture)

    abandonment_fixture = "transport_abandonment_negative_successor_version"
    invalid_abandonment_version = harness.fixture_document(abandonment_fixture)

    ledger_fixture = "requirements_golden_ledger"
    ledger = harness.fixture_document(ledger_fixture)

    unordered_entries = deepcopy(ledger)
    unordered_entries["entries"].reverse()
    unordered_entries = harness.reseal("requirement-ledger/v1", unordered_entries)

    invalid_ledger_digest = deepcopy(ledger)
    invalid_ledger_digest["ledger_digest"] = "0" * 64

    invalid_active_set = deepcopy(ledger)
    invalid_active_set["active_requirement_ids"] = invalid_active_set[
        "active_requirement_ids"
    ][1:]
    invalid_active_set = harness.reseal("requirement-ledger/v1", invalid_active_set)

    id_kind_mismatch = deepcopy(ledger["entries"][0])
    id_kind_mismatch["source_kind"] = "policy"

    self_link = deepcopy(ledger["entries"][0])
    self_link["parent_requirement_ids"] = [self_link["requirement_id"]]

    derived_fixture = "requirements_negative_derived_mandatory_without_rationale"
    invalid_derived_shape = harness.fixture_document(derived_fixture)

    charter_fixture = "requirements_golden_charter"
    charter = harness.fixture_document(charter_fixture)

    invalid_charter_digest = deepcopy(charter)
    invalid_charter_digest["digest"] = "0" * 64

    unordered_charter_sets = deepcopy(charter)
    unordered_charter_sets["requirement_ids"].reverse()
    unordered_charter_sets = harness.reseal("task-charter/v1", unordered_charter_sets)

    waiver_fixture = "requirements_negative_waiver_empty_issuer_profile"
    invalid_waiver_time = harness.fixture_document(waiver_fixture)
    invalid_waiver_time["expires_at"] = invalid_waiver_time["issued_at"]

    return [
        case(
            "acceptance_source_ids_unique",
            request_fixture,
            duplicate_source,
            failure("TASK_REQUEST_SOURCE_ID_INVALID"),
        ),
        case(
            "attempt_state_nullability",
            attempt_fixture,
            invalid_attempt_nullability,
            failure("ATTEMPT_STATE_NULLABILITY_INVALID"),
        ),
        case(
            "attempt_transport_binding_all_or_none",
            attempt_fixture,
            partial_attempt_transport,
            failure(
                "ATTEMPT_TRANSPORT_BINDING_INVALID", "$.transport_binding"
            ),
        ),
        case(
            "budget_ceiling_consistency",
            policy_fixture,
            invalid_budget,
            failure("POLICY_RESERVE_INVALID"),
        ),
        case(
            "capability_and_delivery_sets_sorted_unique",
            request_fixture,
            unordered_capabilities,
            failure("TASK_REQUEST_CAPABILITY_ORDER_INVALID"),
        ),
        case(
            "capability_sets_disjoint_sorted_unique",
            policy_fixture,
            overlapping_capabilities,
            failure("POLICY_CAPABILITY_OVERLAP"),
        ),
        case(
            "charter_digest_exact",
            charter_fixture,
            invalid_charter_digest,
            failure("CONTRACT_DIGEST_MISMATCH", "$.digest"),
        ),
        case(
            "derived_requirement_shape",
            derived_fixture,
            invalid_derived_shape,
            failure(
                "DERIVED_REQUIREMENT_RATIONALE_REQUIRED", "$.rationale"
            ),
        ),
        case(
            "entries_normative_ingest_then_append_order",
            ledger_fixture,
            unordered_entries,
            failure("REQUIREMENT_INGEST_ORDER_INVALID"),
        ),
        case(
            "fenced_reason_ownership_loss",
            attempt_fixture,
            invalid_fenced_reason,
            failure("FENCED_REASON_INVALID", "$.termination_reason"),
        ),
        case(
            "head_version_ref_pairs",
            record_fixture,
            invalid_record_head,
            failure("TASK_RECORD_CHARTER_HEAD_INVALID", "$.charter_ref"),
        ),
        case(
            "ledger_digest_exact",
            ledger_fixture,
            invalid_ledger_digest,
            failure("CONTRACT_DIGEST_MISMATCH", "$.ledger_digest"),
        ),
        case(
            "owner_state_nullability",
            owner_fixture,
            invalid_owner_nullability,
            failure("OWNER_STATE_NULLABILITY_INVALID"),
        ),
        case(
            "policy_digest_exact",
            policy_fixture,
            invalid_policy_digest,
            failure("CONTRACT_DIGEST_MISMATCH", "$.digest"),
        ),
        case(
            "requirement_id_source_kind_match",
            ledger_fixture,
            id_kind_mismatch,
            failure("REQUIREMENT_ID_KIND_INVALID", "$.requirement_id"),
        ),
        case(
            "risk_ceiling_current_mvp",
            policy_fixture,
            invalid_mvp_risk,
            failure("POLICY_QUALITY_RISK_FLOOR_INVALID"),
        ),
        case(
            "root_and_origin_sets_sorted_unique",
            policy_fixture,
            unordered_roots,
            failure("POLICY_ROOT_ORDER_INVALID"),
        ),
        case(
            "server_authority_envelope",
            authority_fixture,
            invalid_authority_binding,
            failure(
                "AUTHORITY_GRANT_BINDING_MISMATCH",
                code="AUTHORITY_INPUT_UNTRUSTED",
            ),
        ),
        case(
            "sorted_unique_active_requirement_ids",
            ledger_fixture,
            invalid_active_set,
            failure(
                "REQUIREMENT_ACTIVE_SET_INVALID", "$.active_requirement_ids"
            ),
        ),
        case(
            "sorted_unique_charter_sets",
            charter_fixture,
            unordered_charter_sets,
            failure("CHARTER_SET_ORDER_INVALID", "$.requirement_ids"),
        ),
        case(
            "sorted_unique_requirement_links",
            ledger_fixture,
            self_link,
            failure("REQUIREMENT_SELF_LINK_INVALID", "$.parent_requirement_ids"),
        ),
        case(
            "task_record_transport_binding_all_or_none",
            record_fixture,
            partial_record_transport,
            failure("TASK_RECORD_TRANSPORT_BINDING_INVALID"),
        ),
        case(
            "terminal_result_shape",
            record_fixture,
            invalid_terminal_shape,
            failure("TASK_RECORD_TERMINAL_RESULT_INVALID"),
        ),
        case(
            "transport_abandonment_record",
            abandonment_fixture,
            invalid_abandonment_version,
            failure(
                "AUTHORITY_SUCCESSOR_VERSION_INVALID",
                "$.abandoned_task_version",
                code="AUTHORITY_INPUT_UNTRUSTED",
            ),
        ),
        case(
            "utf8_nfc_byte_limits",
            request_fixture,
            oversized_utf8,
            failure(
                "UTF8_BYTE_LIMIT_INVALID",
                "$.acceptance_criteria[0].statement",
            ),
        ),
        case(
            "waiver_time_order",
            waiver_fixture,
            invalid_waiver_time,
            failure(
                "WAIVER_TIME_RANGE_INVALID",
                code="WAIVER_INVALID",
            ),
        ),
    ]


class AgentFirstSemanticClosureNegativeTaskControlTest(
    SemanticClosureHarness, unittest.TestCase
):
    def test_task_control_rules_reject_targeted_direct_handler_mutants(self) -> None:
        cases = build_task_control_negative_cases(self)
        case_rule_ids = [case["rule_id"] for case in cases]

        self.assertEqual(len(OWNED_RULE_IDS), len(cases))
        self.assertEqual(OWNED_RULE_IDS, frozenset(case_rule_ids))
        self.assertEqual(len(case_rule_ids), len(set(case_rule_ids)))
        self.assertFalse(OWNED_RULE_IDS - set(self.rule_inventory))

        utf8_case = next(
            case for case in cases if case["rule_id"] == "utf8_nfc_byte_limits"
        )
        self.python._validate_node(
            self.schemas["task-request/v1"],
            utf8_case["semantic_document"],
            "$",
        )

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
