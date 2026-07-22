from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import unittest

from pullwise_server._generated_agent_task_contract import (
    canonical_validated_bytes,
    derive_task_result_core,
    package_tuple,
    verify_document_digest,
)
from pullwise_server.agent_first_transport_envelopes import (
    TERMINAL_BINDING_FAULT_POINTS,
    TERMINAL_COMMON_FAULT_POINTS,
)
from pullwise_server.agent_first_transport_receipts import RECEIPT_FAULT_POINTS
from tests.agent_first_authority_support import AuthorityHarness
from tests.agent_first_transport_support import TransportEnvelopeHarness


class AgentFirstTransportEnvelopeTest(
    TransportEnvelopeHarness,
    AuthorityHarness,
    unittest.TestCase,
):
    def prepare_transport(
        self,
        diagnostics_state: str,
        *,
        outcome: str = "COMPLETED",
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object] | None]:
        _, authority = self.prepare_claim()
        envelope, receipt = self.transport_envelope(
            authority,
            diagnostics_state=diagnostics_state,
            outcome=outcome,
        )
        if receipt is not None:
            self.authority.store_transport_receipt(receipt)
        return authority, envelope, receipt

    def authority_states(self) -> tuple[str, str, str]:
        with self.connect() as connection:
            attempt = connection.execute(
                "SELECT state FROM agent_current_attempts"
            ).fetchone()[0]
            owner = connection.execute(
                "SELECT state FROM agent_current_owner_incarnations"
            ).fetchone()[0]
            grant = connection.execute(
                "SELECT state FROM agent_current_grant_authority"
            ).fetchone()[0]
        return attempt, owner, grant

    def test_uploaded_commit_is_atomic_exact_and_binds_distinct_response(self) -> None:
        _, envelope, receipt = self.prepare_transport("uploaded")
        assert receipt is not None
        points = TERMINAL_COMMON_FAULT_POINTS + TERMINAL_BINDING_FAULT_POINTS
        self.assert_fault_rolls_back(
            points,
            lambda authority: authority.commit_current_transport_envelope(envelope),
            (
                "agent_current_terminal_results",
                "agent_current_control_events",
            ),
        )
        self.assertEqual(
            self.authority_states(),
            ("CLAIMED", "STARTING", "ACTIVE"),
        )

        first = self.authority.commit_current_transport_envelope(envelope)
        self.assertEqual(
            first,
            self.authority.commit_current_transport_envelope(copy.deepcopy(envelope)),
        )
        ack = verify_document_digest("task-result-transport-ack/v1", json.loads(first))
        receipt_document = verify_document_digest(
            "server-transport-receipt/v1", receipt
        )
        verify_document_digest("task-fence/v1", envelope["full_fence"])
        verify_document_digest(
            "server-authority-envelope/v1", envelope["authority"]
        )
        self.assertEqual(ack["receipt_binding_state"], "bound")
        self.assertEqual(ack["receipt_digest"], receipt_document["receipt_digest"])

        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            head = connection.execute(
                """
                SELECT lifecycle, task_version, result_ref, result_digest, outcome,
                       current_authority_schema_id, current_authority_digest
                FROM agent_current_task_heads
                """
            ).fetchone()
            stored = connection.execute(
                "SELECT * FROM agent_current_terminal_results"
            ).fetchone()
            binding = connection.execute(
                "SELECT * FROM agent_current_transport_receipt_bindings"
            ).fetchone()
            binding_response = verify_document_digest(
                "server-transport-receipt-binding-response/v1",
                json.loads(binding["response_bytes"]),
            )
            self.assertNotEqual(binding["response_bytes"], first)
            self.assertEqual(
                binding_response["transport_envelope_digest"],
                ack["transport_envelope_digest"],
            )
            self.assertEqual(stored["response_bytes"], first)
            self.assertEqual(
                stored["envelope_bytes"],
                canonical_validated_bytes(
                    "task-result-transport-envelope/v1", envelope
                ),
            )
            self.assertIsNotNone(stored["worker_debug_descriptor_bytes"])
            descriptor = json.loads(stored["worker_debug_descriptor_bytes"])
            self.assertEqual(
                canonical_validated_bytes(
                    "worker-debug-fragment-descriptor/v1", descriptor
                ),
                stored["worker_debug_descriptor_bytes"],
            )
            self.assertEqual(
                descriptor["fragment_ref"],
                receipt_document["content_ref"],
            )
            self.assertEqual(
                descriptor["server_receipt_ref"],
                envelope["transport_receipt"]["ref"],
            )
            core = derive_task_result_core(envelope["task_result"])
            self.assertEqual(
                stored["task_result_core_bytes"],
                canonical_validated_bytes("task-result-core/v1", core),
            )
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "UPDATE agent_current_transport_receipt_bindings "
                    "SET transport_envelope_digest=NULL"
                )
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM agent_current_terminal_results")
        self.assertEqual(
            tuple(head),
            (
                "TERMINAL",
                envelope["task_result"]["terminal_task_version"],
                ack["transport_envelope_digest"],
                envelope["task_result_digest"],
                "COMPLETED",
                None,
                None,
            ),
        )
        self.assertEqual(self.authority_states(), ("SUCCEEDED", "CLOSED", "REVOKED"))

    def test_local_only_failed_commit_skips_receipt_and_marks_attempt_failed(self) -> None:
        _, envelope, receipt = self.prepare_transport("local_only", outcome="FAILED")
        self.assertIsNone(receipt)
        response = self.authority.commit_current_transport_envelope(envelope)
        ack = verify_document_digest(
            "task-result-transport-ack/v1", json.loads(response)
        )
        self.assertEqual(ack["receipt_binding_state"], "not_applicable")
        self.assertIsNone(ack["receipt_digest"])
        self.assertEqual(self.authority_states(), ("FAILED", "CLOSED", "REVOKED"))
        self.assertEqual(
            self.counts(
                "agent_current_transport_receipts",
                "agent_current_transport_receipt_bindings",
            ),
            (0, 0),
        )
        with self.connect() as connection:
            terminal = connection.execute(
                "SELECT diagnostics_state, receipt_digest, "
                "worker_debug_descriptor_bytes FROM agent_current_terminal_results"
            ).fetchone()
        self.assertEqual(terminal[:2], ("local_only", None))
        self.assertIsNotNone(terminal[2])

    def test_unavailable_cancelled_commit_has_no_descriptor_or_receipt(self) -> None:
        _, envelope, receipt = self.prepare_transport(
            "unavailable",
            outcome="CANCELLED",
        )
        self.assertIsNone(receipt)
        response = self.authority.commit_current_transport_envelope(envelope)
        ack = verify_document_digest(
            "task-result-transport-ack/v1", json.loads(response)
        )
        self.assertEqual(ack["receipt_binding_state"], "not_applicable")
        self.assertEqual(self.authority_states(), ("CANCELLED", "CLOSED", "REVOKED"))
        with self.connect() as connection:
            terminal = connection.execute(
                "SELECT diagnostics_state, receipt_digest, "
                "worker_debug_descriptor_bytes FROM agent_current_terminal_results"
            ).fetchone()
        self.assertEqual(terminal, ("unavailable", None, None))

    def test_not_applicable_commit_has_no_descriptor_or_receipt(self) -> None:
        _, envelope, receipt = self.prepare_transport("not_applicable")
        self.assertIsNone(receipt)
        self.authority.commit_current_transport_envelope(envelope)
        self.assertEqual(self.authority_states(), ("SUCCEEDED", "CLOSED", "REVOKED"))
        with self.connect() as connection:
            terminal = connection.execute(
                "SELECT diagnostics_state, receipt_digest, "
                "worker_debug_descriptor_bytes FROM agent_current_terminal_results"
            ).fetchone()
        self.assertEqual(terminal, ("not_applicable", None, None))

    def test_transport_errors_are_stable_and_conflict_has_zero_writes(self) -> None:
        _, envelope, _ = self.prepare_transport("uploaded")
        before = self.counts(
            "agent_current_terminal_results",
            "agent_current_control_events",
        )
        bad_digest = copy.deepcopy(envelope)
        bad_digest["task_result_digest"] = "0" * 64
        self.assert_error(
            "TRANSPORT_ENVELOPE_DIGEST_INVALID",
            lambda: self.authority.commit_current_transport_envelope(bad_digest),
        )
        bad_type = copy.deepcopy(envelope)
        bad_type["transport_receipt"]["ref"]["content_schema_id"] = (
            "worker-debug-fragment/v1"
        )
        self.assert_error(
            "TRANSPORT_RECEIPT_TYPE_INVALID",
            lambda: self.authority.commit_current_transport_envelope(bad_type),
        )
        missing_receipt = copy.deepcopy(envelope)
        missing_receipt["transport_receipt"]["ref"]["sha256"] = "1" * 64
        self.assert_error(
            "TRANSPORT_RECEIPT_BINDING_CONFLICT",
            lambda: self.authority.commit_current_transport_envelope(missing_receipt),
        )
        self.assertEqual(
            before,
            self.counts(
                "agent_current_terminal_results",
                "agent_current_control_events",
            ),
        )

        accepted = self.authority.commit_current_transport_envelope(envelope)
        changed = copy.deepcopy(envelope)
        changed["transport_receipt"]["ref"]["artifact_id"] = (
            "art_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        )
        snapshot = self.counts(
            "agent_current_terminal_results",
            "agent_current_control_events",
        )
        self.assert_error(
            "IDEMPOTENCY_CONFLICT",
            lambda: self.authority.commit_current_transport_envelope(changed),
        )
        self.assertEqual(snapshot, self.counts(
            "agent_current_terminal_results",
            "agent_current_control_events",
        ))
        self.assertEqual(
            accepted,
            self.authority.commit_current_transport_envelope(envelope),
        )

    def test_receipt_faults_and_cross_task_ids_are_stable(self) -> None:
        _, authority = self.prepare_claim()
        receipt = self.receipt(authority)
        self.assert_fault_rolls_back(
            RECEIPT_FAULT_POINTS,
            lambda service: service.store_transport_receipt(receipt),
            (
                "agent_current_transport_receipts",
                "agent_current_transport_receipt_bindings",
            ),
        )
        self.authority.store_transport_receipt(receipt)

        second_task = "task_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        self.accept(second_task)
        second_claim = self.claim_request(
            task_id=second_task,
            idempotency_key="claim:second",
            lease_id="lease_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )
        second_authority = json.loads(
            self.authority.claim_and_issue_current_grant(second_claim)
        )
        colliding_receipt = self.receipt(second_authority)
        self.assert_error(
            "TRANSPORT_RECEIPT_BINDING_CONFLICT",
            lambda: self.authority.store_transport_receipt(colliding_receipt),
        )

    def test_cross_task_result_id_collision_is_stable_conflict(self) -> None:
        _, first_authority = self.prepare_claim()
        first_envelope, _ = self.transport_envelope(
            first_authority,
            diagnostics_state="local_only",
        )
        self.authority.commit_current_transport_envelope(first_envelope)

        second_task = "task_cccccccccccccccccccccccccccccccc"
        self.accept(second_task)
        second_claim = self.claim_request(
            task_id=second_task,
            idempotency_key="claim:result-collision",
            lease_id="lease_cccccccccccccccccccccccccccccccc",
        )
        second_authority = json.loads(
            self.authority.claim_and_issue_current_grant(second_claim)
        )
        second_envelope, _ = self.transport_envelope(
            second_authority,
            diagnostics_state="local_only",
        )
        self.assert_error(
            "IDEMPOTENCY_CONFLICT",
            lambda: self.authority.commit_current_transport_envelope(
                second_envelope
            ),
        )

    def test_abandoned_authority_cannot_commit_prepared_terminal_envelope(self) -> None:
        _, authority = self.prepare_claim()
        envelope, _ = self.transport_envelope(
            authority,
            diagnostics_state="local_only",
        )
        request = {
            "schema_id": "agent-claim-abandon-request/v1",
            "package": package_tuple(),
            **{
                key: authority[key]
                for key in (
                    "task_id",
                    "attempt_id",
                    "session_id",
                    "owner_id",
                    "lease_id",
                    "deletion_version",
                    "owner_epoch",
                    "native_epoch",
                    "transport_epoch",
                )
            },
            "grant_id": authority["grant"]["grant_id"],
            "expected_task_version": authority["task_version"],
            "reason": "outer_lease_lost",
            "idempotency_key": "abandon:before-terminal",
        }
        self.authority.abandon_current_claim(request)
        before = self.counts(
            "agent_current_terminal_results",
            "agent_current_control_events",
        )
        self.assert_error(
            "AUTHORITY_FENCED",
            lambda: self.authority.commit_current_transport_envelope(envelope),
        )
        self.assertEqual(
            before,
            self.counts(
                "agent_current_terminal_results",
                "agent_current_control_events",
            ),
        )

    def test_corrupt_stored_authority_requests_reload_not_worker_blame(self) -> None:
        _, envelope, _ = self.prepare_transport("local_only")
        with self.connect() as connection:
            connection.execute(
                "DROP TRIGGER agent_current_claims_update_immutable"
            )
            connection.execute(
                "UPDATE agent_current_claims SET authority_bytes=x'00'"
            )
        self.assert_error(
            "AUTHORITY_RELOAD_REQUIRED",
            lambda: self.authority.commit_current_transport_envelope(envelope),
        )


if __name__ == "__main__":
    unittest.main()
