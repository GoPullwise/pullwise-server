from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import unittest

from pullwise_server.agent_first_contract_bundle_source import (
    canonical_bytes,
    load_family,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
FAMILY_DIRECTORY = (
    REPO_ROOT / "contracts" / "agent-first" / "current" / "source" / "families"
)
FAMILY_FILES = (
    ("effective-execution-policy", "effective-execution-policy.json"),
    ("task-attempt-owner", "task-attempt-owner.json"),
    ("task-record", "task-record.json"),
    ("task-request", "task-request.json"),
)
TERMINAL_ATTEMPT_STATES = {
    "SUCCEEDED",
    "SUSPENDED",
    "FAILED",
    "CANCELLED",
    "FENCED",
}


class AgentFirstTaskControlContractTest(unittest.TestCase):
    def test_exact_task_control_shapes_and_contextual_transitions(self) -> None:
        schema_owner: dict[str, str] = {}
        fixture_ids: set[str] = set()
        families = [
            load_family(
                FAMILY_DIRECTORY / filename,
                family_id,
                schema_owner,
                fixture_ids,
            )
            for family_id, filename in FAMILY_FILES
        ]
        schemas = {
            item["$id"]: item
            for item in sorted(
                (
                    item
                    for family in families
                    for item in family["schemas"]
                ),
                key=lambda item: item["$id"],
            )
        }
        fixtures = {
            item["fixture_id"]: item
            for family in families
            for item in family["fixtures"]
        }
        self.assertEqual(
            {
                "effective-execution-policy": ["effective-execution-policy/v1"],
                "task-attempt-owner": ["attempt-record/v1", "task-owner/v1"],
                "task-record": ["task-record/v1"],
                "task-request": ["task-request/v1"],
            },
            {
                family["family_id"]: [
                    item["$id"] for item in family["schemas"]
                ]
                for family in families
            },
        )

        self.assertEqual(
            [
                "attempt-record/v1",
                "effective-execution-policy/v1",
                "task-owner/v1",
                "task-record/v1",
                "task-request/v1",
            ],
            list(schemas),
        )
        expected_fields = {
            "task-request/v1": {
                "schema_id", "task_id", "task_type", "intent_kind", "objective",
                "acceptance_criteria", "constraints", "delivery",
                "requested_capabilities", "requested_budgets", "interaction_policy",
                "submitted_at", "submitted_by",
            },
            "effective-execution-policy/v1": {
                "schema_id", "policy_id", "policy_version", "issued_at", "issuer",
                "task_type", "granted_capabilities", "denied_capabilities",
                "capability_risk_ceiling", "quality_risk_floor", "source_write_mode",
                "allowed_read_roots", "allowed_write_roots", "agent_tool_network",
                "dependency_install", "command_policy_ref", "secret_policy_ref",
                "redaction_policy_ref", "budgets", "terminalization_reserve_ms",
                "max_agents", "max_agent_sessions_total", "max_attempts",
                "interaction_mode", "authorized_waiver_issuers", "digest",
            },
            "task-record/v1": {
                "schema_id", "task_id", "task_type", "request_ref", "request_digest",
                "policy_ref", "policy_digest", "policy_version", "protocol_mode",
                "lifecycle", "desired_state", "task_version", "deletion_version",
                "outer_job_id", "run_id", "lease_id", "transport_epoch",
                "native_epoch", "current_attempt_id", "owner_id", "owner_epoch",
                "ledger_version", "ledger_head_digest", "charter_version",
                "charter_ref", "current_checkpoint_generation",
                "current_checkpoint_hash", "quality_risk", "absolute_deadline_at",
                "terminalization_reserve_ms", "completion_proposal_ref",
                "final_observation_manifest_ref", "terminal_kind", "result_ref",
                "result_digest", "outcome", "created_at", "updated_at", "terminal_at",
            },
            "attempt-record/v1": {
                "schema_id", "attempt_id", "task_id", "native_epoch",
                "transport_binding", "state", "state_version",
                "predecessor_checkpoint_generation", "owner_session_id",
                "lease_acquired_at", "started_at", "ended_at",
                "termination_reason", "budget_reservation_id",
            },
            "task-owner/v1": {
                "schema_id", "task_id", "owner_id", "owner_epoch", "session_id",
                "attempt_id", "native_epoch", "state", "state_version", "started_at",
                "ended_at", "termination_reason",
            },
        }
        for schema_id, fields in expected_fields.items():
            with self.subTest(schema_id=schema_id):
                schema = schemas[schema_id]
                self.assertEqual(fields, set(schema["required"]))
                self.assertEqual(fields, set(schema["properties"]))
                self.assertFalse(schema["additionalProperties"])

        self.assertEqual(
            ["SUCCEEDED", "SUSPENDED", "FAILED", "CANCELLED", "FENCED"],
            [
                state
                for state in schemas["attempt-record/v1"]["properties"]["state"][
                    "enum"
                ]
                if state in TERMINAL_ATTEMPT_STATES
            ],
        )
        self.assertEqual(
            ["STARTING", "ACTIVE", "CLOSED", "FENCED"],
            schemas["task-owner/v1"]["properties"]["state"]["enum"],
        )
        self.assertEqual(
            "agent_task_v1",
            schemas["task-record/v1"]["properties"]["protocol_mode"]["const"],
        )
        for field in (
            "command_policy_ref",
            "secret_policy_ref",
            "redaction_policy_ref",
        ):
            with self.subTest(policy_ref=field):
                self.assertEqual(
                    "canonical-document/v1",
                    schemas["effective-execution-policy/v1"]["properties"][
                        field
                    ]["x-pullwise-content-schema-id"],
                )

        request = fixtures["task_control_golden_task_request"]["document"]
        policy = fixtures["task_control_golden_effective_policy"]["document"]
        queued = fixtures["task_control_golden_task_record"]["document"]
        attempt = fixtures["task_control_golden_attempt_record"]["document"]
        owner = fixtures["task_control_golden_task_owner"]["document"]
        self._assert_digest(
            policy,
            schemas["effective-execution-policy/v1"]["x-pullwise-digest"],
        )
        self.assertEqual(
            hashlib.sha256(canonical_bytes(request)).hexdigest(),
            queued["request_ref"]["sha256"],
        )
        self.assertEqual(
            hashlib.sha256(canonical_bytes(policy)).hexdigest(),
            queued["policy_ref"]["sha256"],
        )
        self._validate_pullwise_policy(policy)
        self.assertEqual(
            {"canonical-document/v1"},
            {
                policy[field]["content_schema_id"]
                for field in (
                    "command_policy_ref",
                    "secret_policy_ref",
                    "redaction_policy_ref",
                )
            },
        )
        self._validate_transport_binding(attempt["transport_binding"])

        claimed = deepcopy(queued)
        claimed.update(
            lifecycle="ACTIVE",
            task_version=2,
            native_epoch=1,
            current_attempt_id=attempt["attempt_id"],
            owner_epoch=1,
            updated_at="2026-07-22T00:00:01.000Z",
        )
        self._validate_task_record_transition(queued, claimed)
        self._validate_claim_write_set(queued, claimed, attempt, owner)

        version_jump = deepcopy(claimed)
        version_jump["task_version"] = 3
        with self.assertRaisesRegex(ValueError, "TASK_VERSION_STALE"):
            self._validate_task_record_transition(queued, version_jump)

        orphan_owner = deepcopy(owner)
        orphan_owner["attempt_id"] = "attempt_ffffffffffffffffffffffffffffffff"
        with self.assertRaisesRegex(ValueError, "CLAIM_WRITE_SET_INVALID"):
            self._validate_claim_write_set(queued, claimed, attempt, orphan_owner)

        preparing = deepcopy(attempt)
        preparing.update(state="PREPARING", state_version=2)
        self._validate_attempt_transition(attempt, preparing)
        fenced_attempt = deepcopy(attempt)
        fenced_attempt.update(
            state="FENCED",
            state_version=2,
            ended_at="2026-07-22T00:00:02.000Z",
            termination_reason="OWNERSHIP_LOST",
        )
        self._validate_attempt_transition(attempt, fenced_attempt)
        wrong_fence = deepcopy(fenced_attempt)
        wrong_fence["termination_reason"] = "RUNTIME_FAILURE"
        with self.assertRaisesRegex(ValueError, "ATTEMPT_TRANSITION_INVALID"):
            self._validate_attempt_transition(attempt, wrong_fence)

        active_owner = deepcopy(owner)
        active_owner.update(state="ACTIVE", state_version=2)
        self._validate_owner_transition(owner, active_owner)
        fenced_owner = deepcopy(active_owner)
        fenced_owner.update(
            state="FENCED",
            state_version=3,
            ended_at="2026-07-22T00:00:02.000Z",
            termination_reason="OWNERSHIP_LOST",
        )
        self._validate_owner_transition(active_owner, fenced_owner)
        with self.assertRaisesRegex(ValueError, "OWNER_TRANSITION_INVALID"):
            self._validate_owner_transition(fenced_owner, active_owner)

    def _validate_pullwise_policy(self, policy: dict[str, object]) -> None:
        if policy["capability_risk_ceiling"] not in {"R0", "R1"}:
            raise ValueError("POLICY_INVARIANT_BROKEN")
        if policy["source_write_mode"] != "read_only":
            raise ValueError("POLICY_INVARIANT_BROKEN")
        if policy["agent_tool_network"] != {"mode": "deny", "origins": []}:
            raise ValueError("POLICY_INVARIANT_BROKEN")
        if policy["dependency_install"] != "deny":
            raise ValueError("POLICY_INVARIANT_BROKEN")
        if policy["interaction_mode"] != "unavailable":
            raise ValueError("POLICY_INVARIANT_BROKEN")
        if policy["authorized_waiver_issuers"] != []:
            raise ValueError("POLICY_INVARIANT_BROKEN")

    def _validate_transport_binding(self, binding: dict[str, object]) -> None:
        if binding["protocol_mode"] != "agent_task_v1":
            raise ValueError("TRANSPORT_IDENTITY_MISMATCH")
        values = [
            binding[key]
            for key in ("outer_job_id", "run_id", "lease_id", "transport_epoch")
        ]
        if not (all(value is None for value in values) or all(value is not None for value in values)):
            raise ValueError("TRANSPORT_IDENTITY_MISMATCH")

    def _validate_task_record_transition(
        self,
        before: dict[str, object],
        after: dict[str, object],
    ) -> None:
        if after["task_version"] != before["task_version"] + 1:
            raise ValueError("TASK_VERSION_STALE")
        allowed = {
            "QUEUED": {"ACTIVE", "FINALIZING"},
            "ACTIVE": {"ACTIVE", "WAITING_INPUT", "WAITING_APPROVAL", "FINALIZING"},
            "WAITING_INPUT": {"QUEUED", "FINALIZING"},
            "WAITING_APPROVAL": {"QUEUED", "FINALIZING"},
            "FINALIZING": {"ACTIVE", "QUEUED", "RECONCILING", "FINALIZING", "TERMINAL"},
            "RECONCILING": {"RECONCILING", "TERMINAL"},
            "TERMINAL": set(),
        }
        if after["lifecycle"] not in allowed[before["lifecycle"]]:
            raise ValueError("STATE_TRANSITION_INVALID")

    def _validate_claim_write_set(
        self,
        before: dict[str, object],
        after: dict[str, object],
        attempt: dict[str, object],
        owner: dict[str, object],
    ) -> None:
        expected = (
            after["task_id"] == attempt["task_id"] == owner["task_id"]
            and after["native_epoch"] == before["native_epoch"] + 1
            and after["native_epoch"] == attempt["native_epoch"] == owner["native_epoch"]
            and after["owner_epoch"] == before["owner_epoch"] + 1
            and after["owner_epoch"] == owner["owner_epoch"]
            and after["current_attempt_id"] == attempt["attempt_id"] == owner["attempt_id"]
            and attempt["owner_session_id"] == owner["session_id"]
            and attempt["state"] == "LEASED"
            and owner["state"] == "STARTING"
        )
        if not expected:
            raise ValueError("CLAIM_WRITE_SET_INVALID")

    def _validate_attempt_transition(
        self,
        before: dict[str, object],
        after: dict[str, object],
    ) -> None:
        allowed = {
            "CREATED": {"LEASED", "FENCED"},
            "LEASED": {"PREPARING", "FAILED", "CANCELLED", "FENCED"},
            "PREPARING": {"RUNNING", "FAILED", "CANCELLED", "FENCED"},
            "RUNNING": {"VERIFYING", "SUSPENDING", "FAILED", "CANCELLED", "FENCED"},
            "VERIFYING": {"RUNNING", "SUSPENDING", "PUBLISHING", "FAILED", "CANCELLED", "FENCED"},
            "SUSPENDING": {"SUSPENDED"},
            "PUBLISHING": {"RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "FENCED"},
            **{state: set() for state in TERMINAL_ATTEMPT_STATES},
        }
        terminal = after["state"] in TERMINAL_ATTEMPT_STATES
        valid = (
            after["state_version"] == before["state_version"] + 1
            and after["state"] in allowed[before["state"]]
            and ((after["ended_at"] is not None and after["termination_reason"] is not None) == terminal)
            and (after["state"] != "FENCED" or after["termination_reason"] == "OWNERSHIP_LOST")
        )
        if not valid:
            raise ValueError("ATTEMPT_TRANSITION_INVALID")

    def _validate_owner_transition(
        self,
        before: dict[str, object],
        after: dict[str, object],
    ) -> None:
        allowed = {
            "STARTING": {"ACTIVE", "FENCED"},
            "ACTIVE": {"CLOSED", "FENCED"},
            "CLOSED": set(),
            "FENCED": set(),
        }
        terminal = after["state"] in {"CLOSED", "FENCED"}
        valid = (
            after["state_version"] == before["state_version"] + 1
            and after["state"] in allowed[before["state"]]
            and ((after["ended_at"] is not None and after["termination_reason"] is not None) == terminal)
            and (after["state"] != "FENCED" or after["termination_reason"] == "OWNERSHIP_LOST")
        )
        if not valid:
            raise ValueError("OWNER_TRANSITION_INVALID")

    def _assert_digest(
        self,
        document: dict[str, object],
        specification: dict[str, str],
    ) -> None:
        field = specification["field"]
        unsigned = {key: value for key, value in document.items() if key != field}
        expected = hashlib.sha256(
            specification["domain"].encode("utf-8")
            + b"\0"
            + canonical_bytes(unsigned)
        ).hexdigest()
        self.assertEqual(expected, document[field])


if __name__ == "__main__":
    unittest.main()
