from __future__ import annotations

import json
from pathlib import Path
import unittest

from tests.agent_first_task_evidence_support import (
    canonical_bytes,
    sealed,
    valid_availability,
    valid_content_ref,
)


ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "contracts/agent-first/current/source/families/gate.json"
ERROR_PATH = (
    ROOT / "contracts/agent-first/current/source/families/receipt-error.json"
)

SUCCESS_PREDICATES = (
    "GATE_TASK_STATE",
    "GATE_LEASE_VALID",
    "GATE_DEADLINE",
    "GATE_POLICY",
    "GATE_LEDGER",
    "GATE_SOURCE_FROZEN",
    "GATE_PROPOSAL_FRESH",
    "GATE_QUALITY_PLAN",
    "GATE_ATTESTATIONS",
    "GATE_REQUIREMENTS",
    "GATE_OUTCOME_SHAPE",
    "GATE_EFFECTS_EMPTY",
    "GATE_EVIDENCE_CLOSURE",
    "GATE_BUDGET",
    "GATE_SECRET_SCAN",
)
TERMINAL_PREDICATES = (
    "GATE_TERMINAL_AUTHORITY_FACT",
    "GATE_TERMINAL_AVAILABILITY",
    "GATE_TERMINAL_NO_ACTIVE_EFFECTS",
    "GATE_TERMINAL_OUTCOME_CLASSIFICATION",
    "GATE_TERMINAL_ARTIFACT_DELIVERY",
)
PREDICATE_CONTRACT = {
    "GATE_TASK_STATE": (
        ("attempt-record/v1", "gate-input-snapshot/v1", "task-record/v1"),
        ("GATE_INPUT_STALE", "STATE_TRANSITION_INVALID", "TASK_VERSION_STALE"),
    ),
    "GATE_LEASE_VALID": (
        ("gate-input-snapshot/v1",),
        ("LEASE_INVALID", "NATIVE_EPOCH_STALE", "OWNER_EPOCH_STALE"),
    ),
    "GATE_DEADLINE": (
        ("budget-summary/v1", "gate-input-snapshot/v1"),
        ("ABSOLUTE_DEADLINE_EXCEEDED", "TERMINALIZATION_RESERVE_REACHED"),
    ),
    "GATE_POLICY": (
        ("effective-execution-policy/v1",),
        ("POLICY_INVARIANT_BROKEN", "POLICY_UNSUPPORTED"),
    ),
    "GATE_LEDGER": (
        ("requirement-ledger/v1",),
        ("CONTRACT_DOCUMENT_INVALID", "REQUIREMENT_ID_COLLISION"),
    ),
    "GATE_SOURCE_FROZEN": (
        ("source-tree-manifest/v1",),
        ("SOURCE_STATE_CHANGED", "SOURCE_STATE_MISMATCH"),
    ),
    "GATE_PROPOSAL_FRESH": (
        ("completion-proposal/v1", "gate-input-snapshot/v1"),
        ("GATE_INPUT_STALE", "SOURCE_STATE_MISMATCH"),
    ),
    "GATE_QUALITY_PLAN": (
        ("quality-policy-plan/v1",),
        ("POLICY_INVARIANT_BROKEN", "ROLE_NOT_ENABLED"),
    ),
    "GATE_ATTESTATIONS": (
        ("observation-manifest/v1", "verification-attestation-manifest/v1"),
        (
            "ATTESTATION_NOT_INDEPENDENT",
            "OBSERVATION_ACTOR_MISMATCH",
            "OBSERVATION_MISSING",
        ),
    ),
    "GATE_REQUIREMENTS": (
        ("requirement-ledger/v1", "verification-attestation-manifest/v1"),
        (
            "MANDATORY_REQUIREMENT_FAILED",
            "MANDATORY_REQUIREMENT_UNVERIFIABLE",
            "WAIVER_INVALID",
        ),
    ),
    "GATE_OUTCOME_SHAPE": (
        ("completion-proposal/v1",),
        ("CONTRACT_DOCUMENT_INVALID", "POLICY_INVARIANT_BROKEN"),
    ),
    "GATE_EFFECTS_EMPTY": (
        ("effect-ledger-snapshot/v1",),
        ("EVENT_DELIVERY_UNKNOWN", "POLICY_INVARIANT_BROKEN"),
    ),
    "GATE_EVIDENCE_CLOSURE": (
        ("pre-gate-evidence-closure-manifest/v1", "pre-gate-root-set/v1"),
        ("CAS_CORRUPT", "EVIDENCE_CLOSURE_INVALID"),
    ),
    "GATE_BUDGET": (
        ("budget-summary/v1",),
        ("BUDGET_EXHAUSTED", "TERMINALIZATION_RESERVE_REACHED"),
    ),
    "GATE_SECRET_SCAN": (
        (
            "debug-redaction-plan/v1",
            "pre-gate-evidence-closure-manifest/v1",
            "publication-content-manifest/v1",
        ),
        ("DEBUG_REDACTION_FAILED", "POLICY_INVARIANT_BROKEN"),
    ),
    "GATE_TERMINAL_AUTHORITY_FACT": (
        ("terminalization-fact/v1", "terminalization-input-snapshot/v1"),
        ("CONTRACT_DOCUMENT_INVALID", "GATE_INPUT_STALE"),
    ),
    "GATE_TERMINAL_AVAILABILITY": (
        ("terminalization-input-snapshot/v1",),
        (
            "EXECUTION_STATE_UNAVAILABLE",
            "OBSERVATION_MISSING",
            "SOURCE_STATE_MISMATCH",
        ),
    ),
    "GATE_TERMINAL_NO_ACTIVE_EFFECTS": (
        ("effect-ledger-snapshot/v1", "terminalization-input-snapshot/v1"),
        ("EVENT_DELIVERY_UNKNOWN", "POLICY_INVARIANT_BROKEN"),
    ),
    "GATE_TERMINAL_OUTCOME_CLASSIFICATION": (
        ("terminalization-fact/v1", "terminalization-input-snapshot/v1"),
        ("CONTRACT_DOCUMENT_INVALID", "POLICY_INVARIANT_BROKEN"),
    ),
    "GATE_TERMINAL_ARTIFACT_DELIVERY": (
        ("publication-content-manifest/v1", "terminalization-fact/v1"),
        ("DEBUG_UPLOAD_FAILED", "EVENT_DELIVERY_UNKNOWN", "PROTOCOL_FAILURE"),
    ),
}
COMMON_DECISION_FIELDS = {
    "schema_id",
    "decision_kind",
    "input_snapshot_ref",
    "input_digest",
    "predicate_registry_digest",
    "passed",
    "predicate_results",
    "decision_digest",
}
TERMINAL_REASON_MAP = {
    "PARTIAL": {
        "BUDGET_EXHAUSTED",
        "CAPABILITY_UNAVAILABLE",
        "DEADLINE_REACHED",
        "INTERACTION_UNAVAILABLE",
        "SAFE_PARTIAL_DELIVERY",
        "VERIFICATION_INCOMPLETE",
    },
    "BLOCKED": {
        "APPROVAL_REQUIRED",
        "CAPABILITY_UNAVAILABLE",
        "ENVIRONMENT_UNAVAILABLE",
        "INPUT_REQUIRED",
        "INTERACTION_UNAVAILABLE",
        "POLICY_INVARIANT_BROKEN",
        "POLICY_UNSUPPORTED",
    },
    "FAILED": {
        "BUDGET_EXHAUSTED",
        "CONTRACT_INVALID",
        "DEADLINE_REACHED",
        "POLICY_INVARIANT_BROKEN",
        "POLICY_UNSUPPORTED",
        "PROTOCOL_FAILURE",
        "QUALITY_GATE_FAILED",
        "RUNTIME_FAILURE",
        "SOURCE_MUTATION_FORBIDDEN",
        "STORAGE_FAILURE",
    },
    "CANCELLED": {"LEASE_CANCELLED", "SERVER_CANCELLED", "USER_CANCELLED"},
}


def ordered_refs(values: object, targets: set[str]) -> bool:
    if not isinstance(values, list) or not values:
        return False
    keys = [
        (item["content_schema_id"], item["artifact_id"], item["sha256"])
        for item in values
        if valid_content_ref(item, targets)
    ]
    return len(keys) == len(values) and keys == sorted(set(keys))


class GateFamilyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.gate_family = json.loads(GATE_PATH.read_text(encoding="utf-8"))
        cls.error_family = json.loads(ERROR_PATH.read_text(encoding="utf-8"))
        cls.schemas = {
            item["$id"]: item for item in cls.gate_family["schemas"]
        }

    def test_obsolete_abbreviated_gate_input_is_removed(self) -> None:
        self.assertEqual(
            ["gate-decision/v1", "gate-predicate-registry/v1"],
            [item["$id"] for item in self.gate_family["schemas"]],
        )

    def test_predicate_registry_is_complete_ordered_and_many_to_many(self) -> None:
        fixture = next(
            item
            for item in self.gate_family["fixtures"]
            if item["fixture_id"] == "gate_golden_independent_registry"
        )
        entries = fixture["document"]["predicates"]
        expected_ids = SUCCESS_PREDICATES + TERMINAL_PREDICATES
        self.assertEqual(list(expected_ids), [item["predicate_id"] for item in entries])
        self.assertEqual(
            ["success"] * len(SUCCESS_PREDICATES)
            + ["terminalization"] * len(TERMINAL_PREDICATES),
            [item["decision_kind"] for item in entries],
        )
        for entry in entries:
            inputs, codes = PREDICATE_CONTRACT[entry["predicate_id"]]
            self.assertEqual(list(inputs), entry["input_schema_ids"])
            self.assertEqual(list(codes), entry["failure_codes"])
        consumers: dict[str, set[str]] = {}
        for entry in entries:
            for code in entry["failure_codes"]:
                consumers.setdefault(code, set()).add(entry["predicate_id"])
        self.assertTrue(any(len(value) > 1 for value in consumers.values()))
        self.assertTrue(any(len(item["failure_codes"]) > 1 for item in entries))
        stable = next(
            item
            for item in self.error_family["fixtures"]
            if item["fixture_id"] == "error_golden_current_registry"
        )["document"]["entries"]
        stable_codes = {item["code"] for item in stable}
        self.assertLessEqual(set(consumers), stable_codes)

    def test_registries_are_sealed_and_stable_code_views_are_bijective(self) -> None:
        gate_fixture = next(
            item
            for item in self.gate_family["fixtures"]
            if item["fixture_id"] == "gate_golden_independent_registry"
        )
        self.assertTrue(
            sealed(
                gate_fixture["document"],
                self.schemas["gate-predicate-registry/v1"],
            )
        )
        error_schemas = {
            item["$id"]: item for item in self.error_family["schemas"]
        }
        error_fixture = next(
            item
            for item in self.error_family["fixtures"]
            if item["fixture_id"] == "error_golden_current_registry"
        )
        entries = error_fixture["document"]["entries"]
        codes = [item["code"] for item in entries]
        self.assertEqual(sorted(set(codes)), codes)
        self.assertEqual(
            codes,
            error_schemas["stable-error/v1"]["properties"]["code"]["enum"],
        )
        self.assertEqual(
            codes,
            error_schemas["stable-error-registry/v1"]["properties"]["entries"]
            ["items"]["properties"]["code"]["enum"],
        )
        self.assertTrue(
            sealed(
                error_fixture["document"],
                error_schemas["stable-error-registry/v1"],
            )
        )
        self.assertTrue(
            {
                "GATE_BUDGET_UNSETTLED",
                "GATE_EVIDENCE_INCOMPLETE",
                "GATE_POLICY_UNSATISFIED",
                "GATE_SOURCE_UNSTABLE",
            }.isdisjoint(codes)
        )

    def test_gate_decision_has_two_strict_typed_branches(self) -> None:
        schema = self.schemas["gate-decision/v1"]
        self.assertEqual(
            {
                "document_rules": ["gate_decision"],
                "contextual_helpers": [
                    "evaluate_success_gate",
                    "evaluate_terminalization_gate",
                ],
            },
            schema["x-pullwise-semantics"],
        )
        self.assertEqual(
            {
                "schema_id",
                "decision_kind",
                "input_snapshot_ref",
                "input_digest",
                "predicate_registry_digest",
                "passed",
                "predicate_results",
                "decision_digest",
            },
            set(schema["required"]),
        )
        props = schema["properties"]
        self.assertEqual(
            ["gate-input-snapshot/v1", "terminalization-input-snapshot/v1"],
            props["input_snapshot_ref"]["x-pullwise-content-schema-ids"],
        )
        branches = schema["oneOf"]
        self.assertEqual(2, len(branches))
        success, terminal = branches
        self.assertIs(False, success["additionalProperties"])
        self.assertIs(False, terminal["additionalProperties"])
        self.assertEqual(
            set(schema["required"]) | {"requested_outcome"},
            set(success["required"]),
        )
        self.assertEqual(
            "success", success["properties"]["decision_kind"]["const"]
        )
        self.assertEqual(
            "gate-input-snapshot/v1",
            success["properties"]["input_snapshot_ref"]
            ["x-pullwise-content-schema-id"],
        )
        success_results = success["properties"]["predicate_results"]
        self.assertEqual((15, 15), (success_results["minItems"], success_results["maxItems"]))
        self.assertEqual(
            list(SUCCESS_PREDICATES),
            success_results["items"]["properties"]["predicate_id"]["enum"],
        )
        terminal_fields = {
            "selected_outcome",
            "selected_reason",
            "authoritative_fact_refs",
            "source_availability",
            "evidence_availability",
            "effect_availability",
        }
        self.assertEqual(
            set(schema["required"]) | terminal_fields,
            set(terminal["required"]),
        )
        self.assertEqual(
            "terminalization",
            terminal["properties"]["decision_kind"]["const"],
        )
        self.assertEqual(
            "terminalization-input-snapshot/v1",
            terminal["properties"]["input_snapshot_ref"]
            ["x-pullwise-content-schema-id"],
        )
        terminal_results = terminal["properties"]["predicate_results"]
        self.assertEqual((5, 5), (terminal_results["minItems"], terminal_results["maxItems"]))
        self.assertEqual(
            list(TERMINAL_PREDICATES),
            terminal_results["items"]["properties"]["predicate_id"]["enum"],
        )
        targets = {
            "authoritative_fact_refs": "terminalization-fact/v1",
            "source_availability": "source-tree-manifest/v1",
            "evidence_availability": "pre-gate-evidence-closure-manifest/v1",
            "effect_availability": "effect-ledger-snapshot/v1",
        }
        for field, target in targets.items():
            rule = props[field]
            if field == "authoritative_fact_refs":
                rule = rule["items"]
                annotation = "x-pullwise-content-schema-id"
            else:
                annotation = "x-pullwise-availability-content-schema-id"
            self.assertEqual(target, rule[annotation])

    def valid_decision(self, document: dict[str, object]) -> bool:
        kind = document.get("decision_kind")
        expected_ids = (
            SUCCESS_PREDICATES if kind == "success" else TERMINAL_PREDICATES
        )
        branch_fields = (
            {"requested_outcome"}
            if kind == "success"
            else {
                "selected_outcome",
                "selected_reason",
                "authoritative_fact_refs",
                "source_availability",
                "evidence_availability",
                "effect_availability",
            }
        )
        if set(document) != COMMON_DECISION_FIELDS | branch_fields:
            return False
        results = document["predicate_results"]
        if (
            not isinstance(results, list)
            or [item.get("predicate_id") for item in results]
            != list(expected_ids)
        ):
            return False
        for result in results:
            if set(result) != {
                "predicate_id",
                "passed",
                "failure_code",
                "repairable",
                "evidence_refs",
            }:
                return False
            failure = result["failure_code"]
            if result["passed"] != (failure is None):
                return False
            inputs, allowed_failures = PREDICATE_CONTRACT[result["predicate_id"]]
            if failure is not None and failure not in allowed_failures:
                return False
            if not ordered_refs(result["evidence_refs"], set(inputs)):
                return False
        if document["passed"] != all(item["passed"] for item in results):
            return False
        if document["predicate_registry_digest"] != next(
            item
            for item in self.gate_family["fixtures"]
            if item["fixture_id"] == "gate_golden_independent_registry"
        )["document"]["registry_digest"]:
            return False
        if not sealed(document, self.schemas["gate-decision/v1"]):
            return False
        if kind == "success":
            return (
                document["requested_outcome"]
                in {"COMPLETED", "COMPLETED_WITH_WAIVERS", "NO_CHANGE_NEEDED"}
                and valid_content_ref(
                    document["input_snapshot_ref"], {"gate-input-snapshot/v1"}
                )
            )
        if kind != "terminalization":
            return False
        outcome = document["selected_outcome"]
        return (
            outcome in TERMINAL_REASON_MAP
            and document["selected_reason"] in TERMINAL_REASON_MAP[outcome]
            and valid_content_ref(
                document["input_snapshot_ref"],
                {"terminalization-input-snapshot/v1"},
            )
            and ordered_refs(
                document["authoritative_fact_refs"], {"terminalization-fact/v1"}
            )
            and valid_availability(
                document["source_availability"], {"source-tree-manifest/v1"}
            )
            and valid_availability(
                document["evidence_availability"],
                {"pre-gate-evidence-closure-manifest/v1"},
            )
            and valid_availability(
                document["effect_availability"], {"effect-ledger-snapshot/v1"}
            )
        )

    def test_decision_fixtures_cover_success_terminal_fence_and_crash(self) -> None:
        fixtures = {
            item["fixture_id"]: item for item in self.gate_family["fixtures"]
        }
        self.assertEqual(
            sorted(fixtures),
            [item["fixture_id"] for item in self.gate_family["fixtures"]],
        )
        required = {
            "gate_decision_crash_runtime_failure": "crash",
            "gate_decision_fence_lease_invalid": "fence",
            "gate_decision_golden_success": "golden",
            "gate_decision_golden_terminalization": "golden",
            "gate_decision_idempotency_success": "idempotency",
            "gate_decision_negative_cross_branch": "negative",
            "gate_decision_negative_missing_predicate": "negative",
        }
        for fixture_id, fixture_class in required.items():
            self.assertEqual(fixture_class, fixtures[fixture_id]["fixture_class"])
        valid_ids = {
            "gate_decision_crash_runtime_failure",
            "gate_decision_fence_lease_invalid",
            "gate_decision_golden_success",
            "gate_decision_golden_terminalization",
            "gate_decision_idempotency_success",
        }
        for fixture_id in valid_ids:
            self.assertTrue(
                self.valid_decision(fixtures[fixture_id]["document"]), fixture_id
            )
        self.assertEqual(
            canonical_bytes(fixtures["gate_decision_golden_success"]["document"]),
            canonical_bytes(
                fixtures["gate_decision_idempotency_success"]["document"]
            ),
        )
        fenced = fixtures["gate_decision_fence_lease_invalid"]["document"]
        failures = [item for item in fenced["predicate_results"] if not item["passed"]]
        self.assertEqual(
            [("GATE_LEASE_VALID", "LEASE_INVALID", False)],
            [
                (item["predicate_id"], item["failure_code"], item["repairable"])
                for item in failures
            ],
        )
        crashed = fixtures["gate_decision_crash_runtime_failure"]["document"]
        self.assertEqual(
            ("terminalization", "FAILED", "RUNTIME_FAILURE"),
            (
                crashed["decision_kind"],
                crashed["selected_outcome"],
                crashed["selected_reason"],
            ),
        )
        for fixture_id in {
            "gate_decision_negative_cross_branch",
            "gate_decision_negative_missing_predicate",
        }:
            self.assertFalse(
                self.valid_decision(fixtures[fixture_id]["document"]), fixture_id
            )

    def test_owned_contract_sources_stay_within_readability_limits(self) -> None:
        for path in (GATE_PATH, ERROR_PATH, Path(__file__)):
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertLessEqual(len(lines), 600, path.name)
            self.assertLessEqual(max(map(len, lines), default=0), 200, path.name)


if __name__ == "__main__":
    unittest.main()
