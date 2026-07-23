from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import unittest

from pullwise_server.agent_first_contract_bundle_source import load_family
from tests.agent_first_task_evidence_support import (
    canonical_bytes,
    sealed,
    valid_availability,
    valid_content_ref,
)


ROOT = Path(__file__).resolve().parents[1]
FAMILY_PATH = (
    ROOT / "contracts/agent-first/current/source/families/gate-input.json"
)


class AgentFirstGateInputFamilyTest(unittest.TestCase):
    SUCCESS_FIELDS = {
        "schema_id",
        "task_id",
        "attempt_id",
        "native_epoch",
        "owner_id",
        "owner_epoch",
        "task_version",
        "lifecycle",
        "desired_state",
        "lease_id",
        "outer_lease_expires_at",
        "outer_lease_grace_expires_at",
        "authoritative_cancel_received",
        "absolute_deadline_at",
        "trusted_wall_time_at",
        "monotonic_deadline_remaining_ms",
        "terminal_budget_reserved_ms",
        "predicate_registry_digest",
        "request_ref",
        "policy_ref",
        "requirement_ledger_ref",
        "completion_proposal_ref",
        "quality_policy_plan_ref",
        "original_source_ref",
        "final_source_ref",
        "execution_state_refs",
        "change_set",
        "pre_observation_manifest_ref",
        "final_observation_manifest_ref",
        "verification_attestation_manifest_ref",
        "effect_ledger_ref",
        "budget_summary_ref",
        "publication_content_manifest_ref",
        "debug_redaction_plan_ref",
        "pre_gate_root_set_ref",
        "pre_gate_evidence_closure_ref",
        "pre_gate_closure_digest",
        "requested_outcome",
        "input_digest",
    }
    TERMINAL_FIELDS = {
        "schema_id",
        "task_id",
        "attempt_id",
        "native_epoch",
        "owner_id",
        "owner_epoch",
        "task_version",
        "deletion_version",
        "lifecycle",
        "desired_state",
        "lease_id",
        "outer_lease_expires_at",
        "outer_lease_grace_expires_at",
        "absolute_deadline_at",
        "trusted_wall_time_at",
        "monotonic_deadline_remaining_ms",
        "terminal_budget_reserved_ms",
        "predicate_registry_digest",
        "request_ref",
        "policy_ref",
        "requirement_ledger_ref",
        "original_source",
        "final_source",
        "final_observation_manifest",
        "effect_ledger_ref",
        "budget_summary_ref",
        "publication_content_manifest_ref",
        "debug_redaction_plan_ref",
        "terminalization_fact_refs",
        "pre_gate_root_set_ref",
        "pre_gate_evidence_closure_ref",
        "pre_gate_closure_digest",
        "input_digest",
    }
    SUCCESS_TARGETS = {
        "request_ref": {"task-request/v1"},
        "policy_ref": {"effective-execution-policy/v1"},
        "requirement_ledger_ref": {"requirement-ledger/v1"},
        "completion_proposal_ref": {"completion-proposal/v1"},
        "quality_policy_plan_ref": {"quality-policy-plan/v1"},
        "original_source_ref": {"source-tree-manifest/v1"},
        "final_source_ref": {"source-tree-manifest/v1"},
        "pre_observation_manifest_ref": {
            "pre-verifier-observation-manifest/v1"
        },
        "final_observation_manifest_ref": {"observation-manifest/v1"},
        "verification_attestation_manifest_ref": {
            "verification-attestation-manifest/v1"
        },
        "effect_ledger_ref": {"effect-ledger-snapshot/v1"},
        "budget_summary_ref": {"budget-summary/v1"},
        "publication_content_manifest_ref": {
            "publication-content-manifest/v1"
        },
        "debug_redaction_plan_ref": {"debug-redaction-plan/v1"},
        "pre_gate_root_set_ref": {"pre-gate-root-set/v1"},
        "pre_gate_evidence_closure_ref": {
            "pre-gate-evidence-closure-manifest/v1"
        },
    }
    TERMINAL_TARGETS = {
        key: value
        for key, value in SUCCESS_TARGETS.items()
        if key
        not in {
            "completion_proposal_ref",
            "quality_policy_plan_ref",
            "original_source_ref",
            "final_source_ref",
            "pre_observation_manifest_ref",
            "final_observation_manifest_ref",
            "verification_attestation_manifest_ref",
        }
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.family = load_family(FAMILY_PATH, "gate-input", {}, set())
        cls.schemas = {
            schema["$id"]: schema for schema in cls.family["schemas"]
        }

    def test_family_is_available_through_the_public_source_api(self) -> None:
        self.assertEqual("gate-input", self.family["family_id"])
        self.assertEqual(
            ["gate-input-snapshot/v1", "terminalization-input-snapshot/v1"],
            [schema["$id"] for schema in self.family["schemas"]],
        )

    @staticmethod
    def _ref_key(value: dict[str, object]) -> tuple[object, ...]:
        return (
            value["content_schema_id"],
            value["artifact_id"],
            value["sha256"],
        )

    def _valid_success(
        self,
        document: dict[str, object],
        *,
        allow_final_closure: bool = False,
    ) -> bool:
        schema = self.schemas["gate-input-snapshot/v1"]
        expected = set(schema["required"])
        if allow_final_closure:
            expected.add("final_closure_ref")
        if set(document) != expected or not sealed(document, schema):
            return False
        if not all(
            valid_content_ref(document[field], targets)
            for field, targets in self.SUCCESS_TARGETS.items()
        ):
            return False
        execution = document["execution_state_refs"]
        execution_keys = [self._ref_key(item) for item in execution]
        return (
            document["schema_id"] == "gate-input-snapshot/v1"
            and document["lifecycle"] in {"FINALIZING", "RECONCILING"}
            and document["desired_state"] == "RUN"
            and document["authoritative_cancel_received"] is False
            and document["requested_outcome"]
            in {
                "COMPLETED",
                "COMPLETED_WITH_WAIVERS",
                "NO_CHANGE_NEEDED",
            }
            and valid_availability(document["change_set"], {"change-set/v1"})
            and all(
                valid_content_ref(item, {"execution-state-manifest/v1"})
                for item in execution
            )
            and execution_keys == sorted(set(execution_keys))
            and document["outer_lease_expires_at"]
            <= document["outer_lease_grace_expires_at"]
            and document["trusted_wall_time_at"]
            <= document["absolute_deadline_at"]
            and (
                not allow_final_closure
                or valid_content_ref(
                    document["final_closure_ref"],
                    {"evidence-closure-manifest/v1"},
                )
            )
        )

    def _valid_terminal(
        self,
        document: dict[str, object],
        *,
        allow_empty_facts: bool = False,
    ) -> bool:
        schema = self.schemas["terminalization-input-snapshot/v1"]
        if set(document) != set(schema["required"]) or not sealed(
            document, schema
        ):
            return False
        if not all(
            valid_content_ref(document[field], targets)
            for field, targets in self.TERMINAL_TARGETS.items()
        ):
            return False
        availability = (
            ("original_source", {"source-tree-manifest/v1"}),
            ("final_source", {"source-tree-manifest/v1"}),
            ("final_observation_manifest", {"observation-manifest/v1"}),
        )
        facts = document["terminalization_fact_refs"]
        fact_keys = [self._ref_key(item) for item in facts]
        has_attempt = document["attempt_id"] is not None
        attempt_binding_valid = (
            has_attempt
            and document["native_epoch"] >= 1
            and document["owner_epoch"] >= 1
            and document["lease_id"] is not None
            and document["outer_lease_expires_at"] is not None
            and document["outer_lease_grace_expires_at"] is not None
            or not has_attempt
            and document["native_epoch"] == 0
            and document["owner_epoch"] == 0
            and document["lease_id"] is None
            and document["outer_lease_expires_at"] is None
            and document["outer_lease_grace_expires_at"] is None
        )
        return (
            document["schema_id"] == "terminalization-input-snapshot/v1"
            and document["lifecycle"] == "FINALIZING"
            and document["desired_state"] in {"CANCEL", "RUN"}
            and attempt_binding_valid
            and all(
                valid_availability(document[field], targets)
                for field, targets in availability
            )
            and all(
                valid_content_ref(item, {"terminalization-fact/v1"})
                for item in facts
            )
            and (allow_empty_facts or bool(facts))
            and fact_keys == sorted(set(fact_keys))
        )

    def _seal_input(
        self, schema_id: str, document: dict[str, object]
    ) -> dict[str, object]:
        result = copy.deepcopy(document)
        result.pop("input_digest", None)
        domain = self.schemas[schema_id]["x-pullwise-digest"]["domain"]
        result["input_digest"] = hashlib.sha256(
            domain.encode("utf-8") + b"\0" + canonical_bytes(result)
        ).hexdigest()
        return result

    def test_success_and_terminalization_matrices_remain_disjoint(self) -> None:
        fixtures = {
            item["fixture_id"]: item for item in self.family["fixtures"]
        }
        success = fixtures["gate_input_golden_success_snapshot"]["document"]
        terminal = fixtures[
            "gate_input_golden_terminalization_snapshot"
        ]["document"]
        self.assertTrue(self._valid_success(success))
        self.assertTrue(self._valid_terminal(terminal))
        self.assertEqual(
            "unavailable", terminal["final_observation_manifest"]["availability"]
        )

        cancelled = copy.deepcopy(terminal)
        cancelled["desired_state"] = "CANCEL"
        cancelled["trusted_wall_time_at"] = "2026-01-01T00:08:00.000Z"
        cancelled = self._seal_input(
            "terminalization-input-snapshot/v1", cancelled
        )
        self.assertTrue(self._valid_terminal(cancelled))

        before_attempt = copy.deepcopy(terminal)
        before_attempt.update(
            {
                "attempt_id": None,
                "native_epoch": 0,
                "owner_epoch": 0,
                "lease_id": None,
                "outer_lease_expires_at": None,
                "outer_lease_grace_expires_at": None,
            }
        )
        before_attempt = self._seal_input(
            "terminalization-input-snapshot/v1", before_attempt
        )
        self.assertTrue(self._valid_terminal(before_attempt))

        inconsistent = copy.deepcopy(before_attempt)
        inconsistent["lease_id"] = "stale-lease"
        inconsistent = self._seal_input(
            "terminalization-input-snapshot/v1", inconsistent
        )
        self.assertFalse(self._valid_terminal(inconsistent))

        partial_success = copy.deepcopy(success)
        partial_success["requested_outcome"] = "PARTIAL"
        partial_success = self._seal_input(
            "gate-input-snapshot/v1", partial_success
        )
        self.assertFalse(self._valid_success(partial_success))

        cancelled_success = copy.deepcopy(success)
        cancelled_success["authoritative_cancel_received"] = True
        cancelled_success = self._seal_input(
            "gate-input-snapshot/v1", cancelled_success
        )
        self.assertFalse(self._valid_success(cancelled_success))

    def test_fixtures_are_complete_sealed_and_byte_exact_on_retry(self) -> None:
        fixtures = {
            item["fixture_id"]: item for item in self.family["fixtures"]
        }
        self.assertEqual(sorted(fixtures), list(fixtures))
        pairs = (
            (
                "gate_input_golden_success_snapshot",
                "gate_input_idempotency_success_snapshot",
                self._valid_success,
            ),
            (
                "gate_input_golden_terminalization_snapshot",
                "gate_input_idempotency_terminalization_snapshot",
                self._valid_terminal,
            ),
        )
        for golden_id, retry_id, validator in pairs:
            golden = fixtures[golden_id]["document"]
            retry = fixtures[retry_id]["document"]
            self.assertTrue(validator(golden), golden_id)
            self.assertNotEqual("0" * 64, golden["input_digest"])
            self.assertEqual(canonical_bytes(golden), canonical_bytes(retry))

        direction = fixtures["gate_input_negative_success_final_closure_ref"]
        no_fact = fixtures["gate_input_negative_terminalization_without_fact"]
        self.assertEqual("CONTRACT_DOCUMENT_INVALID", direction["expected_code"])
        self.assertTrue(
            self._valid_success(
                direction["document"], allow_final_closure=True
            )
        )
        self.assertFalse(self._valid_success(direction["document"]))
        self.assertEqual("CONTRACT_DOCUMENT_INVALID", no_fact["expected_code"])
        self.assertTrue(
            self._valid_terminal(
                no_fact["document"], allow_empty_facts=True
            )
        )
        self.assertFalse(self._valid_terminal(no_fact["document"]))

    def test_snapshot_shapes_are_disjoint_typed_and_pre_gate_only(self) -> None:
        success = self.schemas["gate-input-snapshot/v1"]
        terminal = self.schemas["terminalization-input-snapshot/v1"]
        self.assertEqual(self.SUCCESS_FIELDS, set(success["required"]))
        self.assertEqual(self.SUCCESS_FIELDS, set(success["properties"]))
        self.assertEqual(self.TERMINAL_FIELDS, set(terminal["required"]))
        self.assertEqual(self.TERMINAL_FIELDS, set(terminal["properties"]))
        self.assertEqual(
            ["FINALIZING", "RECONCILING"],
            terminal["properties"]["lifecycle"]["enum"],
        )
        self.assertNotIn("final_closure_ref", success["properties"])
        self.assertNotIn("final_closure_ref", terminal["properties"])
        self.assertFalse(
            {
                "completion_proposal_ref",
                "quality_policy_plan_ref",
                "verification_attestation_manifest_ref",
            }.intersection(terminal["properties"])
        )

        typed_nodes = []
        for schema in (success, terminal):
            for node in schema["properties"].values():
                candidate = node.get("items", node)
                if candidate.get("$ref") in {
                    "availability-ref/v1",
                    "content-ref/v1",
                }:
                    typed_nodes.append(candidate)
        for node in typed_nodes:
            annotations = [
                value
                for key, value in node.items()
                if key.startswith("x-pullwise-")
            ]
            self.assertEqual(1, len(annotations), node)
            targets = (
                annotations[0]
                if isinstance(annotations[0], list)
                else [annotations[0]]
            )
            self.assertEqual(sorted(set(targets)), targets, node)
            self.assertNotIn("evidence-closure-manifest/v1", targets)

        fixtures = {
            item["fixture_id"]: item for item in self.family["fixtures"]
        }
        for fixture_id in (
            "gate_input_golden_success_snapshot",
            "gate_input_golden_terminalization_snapshot",
        ):
            document = fixtures[fixture_id]["document"]
            self.assertNotEqual(
                document["pre_gate_evidence_closure_ref"]["sha256"],
                document["pre_gate_closure_digest"],
                fixture_id,
            )


if __name__ == "__main__":
    unittest.main()
