from __future__ import annotations

import json
import sqlite3
import threading
import unittest

from pullwise_server._generated_agent_task_contract import (
    PACKAGE_TUPLE,
    package_tuple,
    schema_ids,
    tool_catalog,
    verify_document_digest,
)
from pullwise_server.agent_first_authority import AgentFirstAuthority, AuthorityError
from pullwise_server.agent_first_authority_store import (
    ACCEPT_FAULT_POINTS,
    REGISTER_FAULT_POINTS,
)
from pullwise_server.agent_first_authority_migrations import CURRENT_AUTHORITY_TABLES
from pullwise_server.agent_first_claim_authority import (
    ABANDON_FAULT_POINTS,
    CLAIM_FAULT_POINTS,
)
from tests.agent_first_authority_support import AuthorityHarness


class AgentFirstAuthorityTest(AuthorityHarness, unittest.TestCase):
    def test_installs_self_contained_current_tables_with_wal(self) -> None:
        with self.connect() as connection:
            installed = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertTrue(set(CURRENT_AUTHORITY_TABLES).issubset(installed))
        self.assertEqual(journal_mode.lower(), "wal")
    def test_accept_package_failure_has_stable_bytes_and_zero_writes(self) -> None:
        missing = self.accept_request()
        missing.pop("package")
        first = self.assert_error(
            "CURRENT_PACKAGE_PIN_MISSING",
            lambda: self.authority.accept_current_task(missing),
        )
        again = self.assert_error(
            "CURRENT_PACKAGE_PIN_MISSING",
            lambda: self.authority.accept_current_task(dict(missing)),
        )
        mismatch = self.accept_request()
        mismatch["package"] = {**package_tuple(), "package_version": "9.9.9"}
        self.assert_error(
            "CURRENT_PACKAGE_PIN_MISMATCH",
            lambda: self.authority.accept_current_task(mismatch),
        )
        self.assertEqual(first.response_bytes, again.response_bytes)
        self.assertEqual(self.counts("agent_current_task_requests"), (0,))
    def test_registration_and_acceptance_are_exact_and_immutable(self) -> None:
        self.assert_fault_rolls_back(
            REGISTER_FAULT_POINTS, lambda a: a.register_worker(self.register_request()),
            ("agent_current_worker_registrations", "agent_current_worker_registration_heads"))
        self.assert_fault_rolls_back(
            ACCEPT_FAULT_POINTS, lambda a: a.accept_current_task(self.accept_request()),
            ("agent_current_task_requests", "agent_current_task_heads", "agent_current_control_events"))
        registration = self.register()
        self.assertEqual(registration, self.register())
        verify_document_digest("agent-worker-register-response/v1", json.loads(registration))
        accepted = self.accept()
        self.assertEqual(accepted, self.accept())
        verify_document_digest("agent-task-accept-response/v1", json.loads(accepted))
        with self.connect() as connection:
            registered = connection.execute(
                "SELECT package_identity, package_version, content_sha256, root_sha256 "
                "FROM agent_current_worker_registrations"
            ).fetchone()
            head = connection.execute(
                "SELECT lifecycle, desired_state, task_version, deletion_version "
                "FROM agent_current_task_heads"
            ).fetchone()
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE agent_current_task_requests SET task_type='x'")
        self.assertEqual(registered, PACKAGE_TUPLE)
        self.assertEqual(head, ("QUEUED", "RUN", 1, 0))
    def test_claim_requires_package_bound_worker_registration(self) -> None:
        variants = (
            (list(schema_ids()), "0" * 64),
            (list(schema_ids())[:-1], tool_catalog()["catalog_digest"]),
        )
        for index, (supported, catalog_digest) in enumerate(variants, start=8):
            worker_id = f"worker_{str(index) * 32}"
            task_id = f"task_{str(index) * 32}"
            self.authority.register_worker(self.register_request(
                worker_id=worker_id, supported_schema_ids=supported,
                tool_catalog_digest=catalog_digest,
            ))
            self.accept(task_id)
            request = self.claim_request(
                worker_id=worker_id, task_id=task_id,
                idempotency_key=f"claim:bad:{index}",
                lease_id=f"lease_{str(index) * 32}",
            )
            before = self.counts("agent_current_attempts", "agent_current_claims")
            self.assert_error(
                "AGENT_GRANT_INVALID",
                lambda request=request: self.authority.claim_and_issue_current_grant(request),
            )
            self.assertEqual(before, self.counts("agent_current_attempts", "agent_current_claims"))
            self.authority.register_worker(self.register_request(worker_id=worker_id))
            self.authority.claim_and_issue_current_grant(request)
    def test_claim_is_complete_atomic_and_exactly_idempotent(self) -> None:
        self.register()
        self.accept()
        request = self.claim_request()
        first = self.authority.claim_and_issue_current_grant(request)
        self.assertEqual(first, self.authority.claim_and_issue_current_grant(dict(request)))
        envelope = verify_document_digest("server-authority-envelope/v1", json.loads(first))
        grant = verify_document_digest("agent-worker-grant/v1", envelope["grant"])
        bound = (
            "task_id", "attempt_id", "session_id", "owner_id", "lease_id",
            "task_version", "deletion_version", "owner_epoch", "native_epoch",
            "transport_epoch",
        )
        self.assertEqual(tuple(grant[key] for key in bound), tuple(envelope[key] for key in bound))
        self.assertEqual(envelope["task_version"], 2)
        with self.connect() as connection:
            claim, authority = connection.execute(
                "SELECT claim_bytes, authority_bytes FROM agent_current_claims"
            ).fetchone()
            head = connection.execute(
                "SELECT lifecycle, task_version, current_authority_digest "
                "FROM agent_current_task_heads"
            ).fetchone()
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE agent_current_grants SET grant_bytes=x'00'")
        verify_document_digest("agent-task-claim/v1", json.loads(claim))
        self.assertEqual(authority, first)
        self.assertEqual(head, ("ACTIVE", 2, envelope["authority_digest"]))
        self.assertEqual(
            self.counts(
                "agent_current_attempts", "agent_current_owner_incarnations",
                "agent_current_claims", "agent_current_grants",
                "agent_current_grant_authority",
            ),
            (1, 1, 1, 1, 1),
        )
    def test_claim_conflict_concurrency_and_every_fault_stage(self) -> None:
        self.register()
        self.accept()
        request = self.claim_request()
        self.assert_fault_rolls_back(
            CLAIM_FAULT_POINTS, lambda a: a.claim_and_issue_current_grant(request),
            ("agent_current_attempts", "agent_current_claims", "agent_current_grants"))
        changed = {**request, "tool_call_limit": 8}
        self.authority.claim_and_issue_current_grant(request)
        before = self.counts("agent_current_control_events", "agent_current_grants")
        self.assert_error(
            "IDEMPOTENCY_CONFLICT",
            lambda: self.authority.claim_and_issue_current_grant(changed),
        )
        self.assertEqual(before, self.counts("agent_current_control_events", "agent_current_grants"))
    def test_concurrent_claim_has_one_complete_winner(self) -> None:
        self.register()
        self.accept()
        barrier = threading.Barrier(2)
        outcomes: list[bytes | AuthorityError] = []

        def claim(suffix: str) -> None:
            request = self.claim_request(
                idempotency_key=f"claim:{suffix}",
                lease_id=f"lease_{suffix * 32}",
            )
            barrier.wait()
            try:
                outcomes.append(AgentFirstAuthority(self.connect).claim_and_issue_current_grant(request))
            except AuthorityError as error:
                outcomes.append(error)

        threads = [threading.Thread(target=claim, args=(suffix,)) for suffix in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sum(isinstance(item, bytes) for item in outcomes), 1)
        self.assertEqual([item.code for item in outcomes if isinstance(item, AuthorityError)], ["TASK_NOT_CLAIMABLE"])
        self.assertEqual(self.counts("agent_current_claims", "agent_current_grants"), (1, 1))
    def test_abandonment_fences_full_authority_and_preserves_task_fields(self) -> None:
        claim_request, envelope = self.prepare_claim()
        stale_receipt = self.receipt(envelope)
        stored_receipt = self.authority.store_transport_receipt(stale_receipt)
        self.assertEqual(stored_receipt, self.authority.store_transport_receipt(stale_receipt))
        request = {
            "schema_id": "agent-claim-abandon-request/v1",
            "package": package_tuple(),
            **{key: envelope[key] for key in (
                "task_id", "attempt_id", "session_id", "owner_id", "lease_id",
                "deletion_version", "owner_epoch", "native_epoch", "transport_epoch",
            )},
            "grant_id": envelope["grant"]["grant_id"],
            "expected_task_version": envelope["task_version"],
            "reason": "outer_lease_lost",
            "idempotency_key": "abandon:one",
        }
        fields = "lifecycle, desired_state, terminal_kind, result_ref, result_digest, outcome, terminal_at"
        with self.connect() as connection:
            before = connection.execute(f"SELECT {fields} FROM agent_current_task_heads").fetchone()
        self.assert_fault_rolls_back(
            ABANDON_FAULT_POINTS, lambda a: a.abandon_current_claim(request),
            ("agent_current_abandonments", "agent_current_fences", "agent_current_control_events"))
        first = self.authority.abandon_current_claim(request)
        self.assertEqual(first, self.authority.abandon_current_claim(dict(request)))
        response = verify_document_digest("agent-claim-abandon-response/v1", json.loads(first))
        with self.connect() as connection:
            after = connection.execute(
                f"SELECT {fields}, task_version, current_authority_schema_id, "
                "current_authority_digest FROM agent_current_task_heads"
            ).fetchone()
            states = (
                connection.execute("SELECT state FROM agent_current_attempts").fetchone()[0],
                connection.execute("SELECT state FROM agent_current_owner_incarnations").fetchone()[0],
                connection.execute("SELECT state FROM agent_current_grant_authority").fetchone()[0],
            )
        self.assertEqual(after[:7], before)
        self.assertEqual((after[7], response["task_version"]), (3, 3))
        self.assertEqual(
            after[8:], ("agent-claim-abandon-response/v1", response["response_digest"])
        )
        self.assertEqual(response["grant"], envelope["grant"])
        self.assertEqual(response["superseded_authority_digest"], envelope["authority_digest"])
        self.assertEqual(states, ("FENCED",) * 3)
        self.assertEqual(self.counts("agent_current_abandonments", "agent_current_fences"), (1, 4))
        with self.connect() as connection:
            for table in ("attempts", "owner_incarnations", "grant_authority"):
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(f"DELETE FROM agent_current_{table}")
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "UPDATE agent_current_attempts SET state='SUCCEEDED'"
                )
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "UPDATE agent_current_task_heads SET lifecycle='TERMINAL', "
                    "task_version=task_version+1, "
                    "current_authority_schema_id='server-authority-envelope/v1', "
                    "current_authority_digest=?",
                    (envelope["authority_digest"],),
                )
        before_stale = self.counts(
            "agent_current_control_events",
            "agent_current_transport_receipts",
            "agent_current_transport_receipt_bindings",
        )
        self.assert_error(
            "AUTHORITY_FENCED",
            lambda: self.authority.claim_and_issue_current_grant(claim_request),
        )
        self.assert_error(
            "AUTHORITY_FENCED",
            lambda: self.authority.store_transport_receipt(stale_receipt),
        )
        self.assertEqual(
            before_stale,
            self.counts(
                "agent_current_control_events",
                "agent_current_transport_receipts",
                "agent_current_transport_receipt_bindings",
            ),
        )
        changed = {**request, "reason": "authority_revoked"}
        self.assert_error("IDEMPOTENCY_CONFLICT", lambda: self.authority.abandon_current_claim(changed))


if __name__ == "__main__":
    unittest.main()
