from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from pullwise_server._generated_agent_task_contract import (
    CONTENT_SHA256,
    PACKAGE_IDENTITY,
    PACKAGE_TUPLE,
    PACKAGE_VERSION,
    ROOT_SHA256,
    canonical_document_bytes,
)
from pullwise_server.agent_first_authority import AgentFirstAuthority, AuthorityError
from pullwise_server.agent_first_authority_migrations import (
    CURRENT_AUTHORITY_TABLES,
    install_current_authority_tables,
)


def package_ref() -> dict[str, object]:
    return {
        "schema_id": "current-package-ref/v1",
        "package_identity": PACKAGE_IDENTITY,
        "package_version": PACKAGE_VERSION,
        "content_sha256": CONTENT_SHA256,
        "root_sha256": ROOT_SHA256,
    }


def digest_document(document: dict[str, object], field: str) -> str:
    unsigned = {key: value for key, value in document.items() if key != field}
    return hashlib.sha256(canonical_document_bytes(unsigned)).hexdigest()


class AgentFirstAuthorityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "authority.sqlite3"
        with self.connect() as connection:
            install_current_authority_tables(connection)
        self.authority = AgentFirstAuthority(self.connect)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def register(self, worker_id: str = "worker_alpha") -> bytes:
        return self.authority.register_worker(
            {
                "schema_id": "agent-worker-register/v1",
                "worker_id": worker_id,
                "package": package_ref(),
            }
        )

    def accept(self, task_id: str = "task_11111111111111111111111111111111") -> bytes:
        return self.authority.accept_current_task(
            {
                "schema_id": "agent-task-request/v1",
                "package": package_ref(),
                "task_id": task_id,
                "task_type": "repo_review.full_scan",
                "idempotency_key": f"accept:{task_id}",
                "request": {"repository": "octo/example", "commit": "a" * 40},
            }
        )

    def claim_request(
        self,
        task_id: str = "task_11111111111111111111111111111111",
        *,
        idempotency_key: str = "claim:one",
        lease_id: str = "lease-one",
    ) -> dict[str, object]:
        return {
            "schema_id": "agent-task-claim-request/v1",
            "package": package_ref(),
            "task_id": task_id,
            "worker_id": "worker_alpha",
            "lease_id": lease_id,
            "transport_epoch": 1,
            "idempotency_key": idempotency_key,
            "capability_ids": ["source.read"],
            "tool_keys": ["internal.read_source"],
            "tool_call_limit": 7,
        }

    def prepare_claim(self) -> tuple[dict[str, object], dict[str, object]]:
        self.register()
        self.accept()
        request = self.claim_request()
        response = json.loads(self.authority.claim_and_issue_current_grant(request))
        return request, response

    def counts(self, *tables: str) -> tuple[int, ...]:
        with self.connect() as connection:
            return tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in tables
            )

    def assert_error(self, code: str, callback) -> AuthorityError:
        with self.assertRaises(AuthorityError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(
            json.loads(raised.exception.response_bytes),
            {"code": code, "schema_id": "agent-first-authority-error/v1"},
        )
        return raised.exception

    def test_installs_only_self_contained_current_authority_tables(self) -> None:
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
        self.assertFalse(any("scan_job" in name or "review_run" in name for name in installed))

    def test_accept_rejects_missing_or_mismatched_package_with_zero_writes(self) -> None:
        missing = {
            "schema_id": "agent-task-request/v1",
            "task_id": "task_11111111111111111111111111111111",
            "task_type": "repo_review.full_scan",
            "idempotency_key": "accept:missing",
        }
        first = self.assert_error(
            "CURRENT_PACKAGE_PIN_MISSING",
            lambda: self.authority.accept_current_task(missing),
        )
        mismatched = {**missing, "package": {**package_ref(), "package_version": "9.9.9"}}
        second = self.assert_error(
            "CURRENT_PACKAGE_PIN_MISMATCH",
            lambda: self.authority.accept_current_task(mismatched),
        )
        self.assertEqual(first.response_bytes, first.canonical_bytes)
        self.assertEqual(second.response_bytes, second.canonical_bytes)
        self.assertEqual(
            self.counts("agent_current_task_requests", "agent_current_task_heads"),
            (0, 0),
        )

    def test_registration_and_acceptance_store_exact_package_and_immutable_request(self) -> None:
        registration = self.register()
        self.assertEqual(registration, self.register())
        accepted = json.loads(self.accept())
        self.assertEqual(accepted["task_version"], 1)
        with self.connect() as connection:
            registration_row = connection.execute(
                "SELECT package_identity, package_version, content_sha256, root_sha256 "
                "FROM agent_current_worker_registrations"
            ).fetchone()
            task_row = connection.execute(
                "SELECT lifecycle, desired_state, task_version, deletion_version "
                "FROM agent_current_task_heads"
            ).fetchone()
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "UPDATE agent_current_task_requests SET task_type='changed'"
                )
        self.assertEqual(registration_row, PACKAGE_TUPLE)
        self.assertEqual(task_row, ("QUEUED", "RUN", 1, 0))

    def test_claim_is_atomic_complete_and_exactly_idempotent(self) -> None:
        self.register()
        self.accept()
        request = self.claim_request()
        first = self.authority.claim_and_issue_current_grant(request)
        second = self.authority.claim_and_issue_current_grant(dict(request))
        self.assertEqual(first, second)
        envelope = json.loads(first)
        grant = envelope["grant"]
        self.assertEqual(envelope["package"], package_ref())
        self.assertEqual(envelope["task_version"], 2)
        self.assertEqual(envelope["deletion_version"], 0)
        self.assertEqual(envelope["lifecycle"], "ACTIVE")
        self.assertEqual(envelope["desired_state"], "RUN")
        self.assertEqual(envelope["owner_epoch"], 1)
        self.assertEqual(envelope["native_epoch"], 1)
        self.assertEqual(envelope["transport_epoch"], 1)
        self.assertEqual(grant["grant_digest"], digest_document(grant, "grant_digest"))
        self.assertEqual(
            envelope["authority_digest"], digest_document(envelope, "authority_digest")
        )
        self.assertEqual(
            self.counts(
                "agent_current_attempts",
                "agent_current_owner_incarnations",
                "agent_current_claims",
                "agent_current_grants",
                "agent_current_grant_authority",
            ),
            (1, 1, 1, 1, 1),
        )
        with self.connect() as connection:
            head = connection.execute(
                "SELECT lifecycle, task_version, current_attempt_id, current_session_id, "
                "current_grant_id, current_lease_id FROM agent_current_task_heads"
            ).fetchone()
            event_count = connection.execute(
                "SELECT COUNT(*) FROM agent_current_control_events"
            ).fetchone()[0]
        self.assertEqual(head[:2], ("ACTIVE", 2))
        self.assertTrue(all(head[index] for index in range(2, 6)))
        self.assertEqual(event_count, 2)

    def test_claim_idempotency_conflict_has_no_writes(self) -> None:
        request, _ = self.prepare_claim()
        changed = {**request, "tool_call_limit": 8}
        before = self.counts("agent_current_control_events", "agent_current_grants")
        self.assert_error(
            "IDEMPOTENCY_CONFLICT",
            lambda: self.authority.claim_and_issue_current_grant(changed),
        )
        self.assertEqual(
            self.counts("agent_current_control_events", "agent_current_grants"), before
        )

    def test_concurrent_claim_has_one_complete_winner(self) -> None:
        self.register()
        self.accept()
        barrier = threading.Barrier(2)
        outcomes: list[bytes | AuthorityError] = []
        lock = threading.Lock()

        def claim(suffix: str) -> None:
            request = self.claim_request(
                idempotency_key=f"claim:{suffix}", lease_id=f"lease-{suffix}"
            )
            barrier.wait()
            try:
                result: bytes | AuthorityError = AgentFirstAuthority(
                    self.connect
                ).claim_and_issue_current_grant(request)
            except AuthorityError as error:
                result = error
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=claim, args=(suffix,)) for suffix in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sum(isinstance(item, bytes) for item in outcomes), 1)
        errors = [item for item in outcomes if isinstance(item, AuthorityError)]
        self.assertEqual([error.code for error in errors], ["TASK_NOT_CLAIMABLE"])
        self.assertEqual(
            self.counts(
                "agent_current_attempts",
                "agent_current_owner_incarnations",
                "agent_current_claims",
                "agent_current_grants",
            ),
            (1, 1, 1, 1),
        )

    def test_injected_claim_fault_rolls_back_every_row_and_pointer(self) -> None:
        self.register()
        self.accept()

        def fail(point: str) -> None:
            if point == "claim.after_grant_authority":
                raise RuntimeError("injected")

        faulty = AgentFirstAuthority(self.connect, fault_injector=fail)
        with self.assertRaisesRegex(RuntimeError, "injected"):
            faulty.claim_and_issue_current_grant(self.claim_request())
        self.assertEqual(
            self.counts(
                "agent_current_attempts",
                "agent_current_owner_incarnations",
                "agent_current_claims",
                "agent_current_grants",
                "agent_current_grant_authority",
            ),
            (0, 0, 0, 0, 0),
        )
        with self.connect() as connection:
            head = connection.execute(
                "SELECT lifecycle, task_version, current_attempt_id FROM agent_current_task_heads"
            ).fetchone()
            events = connection.execute(
                "SELECT COUNT(*) FROM agent_current_control_events"
            ).fetchone()[0]
        self.assertEqual(head, ("QUEUED", 1, None))
        self.assertEqual(events, 1)

    def transport_receipt(
        self, envelope: dict[str, object], receipt_type: str = "server_transport"
    ) -> dict[str, object]:
        receipt = {
            "schema_id": "server-transport-receipt/v1",
            "receipt_type": receipt_type,
            "package": package_ref(),
            "receipt_id": "receipt_22222222222222222222222222222222",
            "task_id": envelope["task_id"],
            "attempt_id": envelope["attempt_id"],
            "session_id": envelope["session_id"],
            "grant_digest": envelope["grant"]["grant_digest"],
            "transport_epoch": envelope["transport_epoch"],
            "payload_digest": "3" * 64,
        }
        return {**receipt, "receipt_digest": digest_document(receipt, "receipt_digest")}

    def test_transport_receipt_is_typed_immutable_and_binding_is_one_shot(self) -> None:
        _, envelope = self.prepare_claim()
        local = self.transport_receipt(envelope, "local_tool")
        self.assert_error(
            "TRANSPORT_RECEIPT_TYPE_INVALID",
            lambda: self.authority.store_transport_receipt(local),
        )
        self.assertEqual(self.counts("agent_current_transport_receipts"), (0,))
        receipt = self.transport_receipt(envelope)
        stored = self.authority.store_transport_receipt(receipt)
        self.assertEqual(stored, self.authority.store_transport_receipt(dict(receipt)))
        bound = self.authority.bind_transport_receipt(receipt["receipt_digest"], "4" * 64)
        self.assertEqual(
            bound,
            self.authority.bind_transport_receipt(receipt["receipt_digest"], "4" * 64),
        )
        self.assert_error(
            "TRANSPORT_RECEIPT_BINDING_CONFLICT",
            lambda: self.authority.bind_transport_receipt(
                receipt["receipt_digest"], "5" * 64
            ),
        )
        self.assert_error(
            "TRANSPORT_ENVELOPE_DIGEST_INVALID",
            lambda: self.authority.bind_transport_receipt(receipt["receipt_digest"], None),
        )
        with self.connect() as connection:
            row = connection.execute(
                "SELECT transport_envelope_digest FROM "
                "agent_current_transport_receipt_bindings"
            ).fetchone()
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "UPDATE agent_current_transport_receipts SET receipt_type='local_tool'"
                )
        self.assertEqual(row, ("4" * 64,))

    def test_abandonment_atomically_fences_full_authority_and_preserves_task(self) -> None:
        _, envelope = self.prepare_claim()
        request = {
            "schema_id": "agent-claim-abandon-request/v1",
            "package": package_ref(),
            "task_id": envelope["task_id"],
            "attempt_id": envelope["attempt_id"],
            "session_id": envelope["session_id"],
            "grant_id": envelope["grant"]["grant_id"],
            "lease_id": envelope["lease_id"],
            "expected_task_version": envelope["task_version"],
            "deletion_version": envelope["deletion_version"],
            "owner_epoch": envelope["owner_epoch"],
            "native_epoch": envelope["native_epoch"],
            "transport_epoch": envelope["transport_epoch"],
            "reason": "outer_lease_lost",
            "idempotency_key": "abandon:one",
        }
        with self.connect() as connection:
            before = connection.execute(
                "SELECT lifecycle, desired_state, terminal_kind, result_ref, result_digest, "
                "outcome, terminal_at FROM agent_current_task_heads"
            ).fetchone()
        first = self.authority.abandon_current_claim(request)
        self.assertEqual(first, self.authority.abandon_current_claim(dict(request)))
        response = json.loads(first)
        self.assertEqual(response["task_version"], 3)
        with self.connect() as connection:
            after = connection.execute(
                "SELECT lifecycle, desired_state, terminal_kind, result_ref, result_digest, "
                "outcome, terminal_at, task_version FROM agent_current_task_heads"
            ).fetchone()
            attempt_state = connection.execute(
                "SELECT state FROM agent_current_attempts"
            ).fetchone()[0]
            owner_state = connection.execute(
                "SELECT state FROM agent_current_owner_incarnations"
            ).fetchone()[0]
            grant_state = connection.execute(
                "SELECT state FROM agent_current_grant_authority"
            ).fetchone()[0]
        self.assertEqual(after[:7], before)
        self.assertEqual(after[7], 3)
        self.assertEqual((attempt_state, owner_state, grant_state), ("FENCED",) * 3)
        self.assertEqual(
            self.counts("agent_current_abandonments", "agent_current_fences"), (1, 4)
        )
        changed = {**request, "reason": "different"}
        self.assert_error(
            "IDEMPOTENCY_CONFLICT",
            lambda: self.authority.abandon_current_claim(changed),
        )
        with self.connect() as connection:
            version = connection.execute(
                "SELECT task_version FROM agent_current_task_heads"
            ).fetchone()[0]
        self.assertEqual(version, 3)


if __name__ == "__main__":
    unittest.main()
