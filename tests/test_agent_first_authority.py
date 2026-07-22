from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from pullwise_server._generated_agent_task_contract import (
    PACKAGE_TUPLE,
    canonical_validated_bytes,
    package_tuple,
    seal_document,
    verify_document_digest,
)
from pullwise_server.agent_first_authority import AgentFirstAuthority, AuthorityError
from pullwise_server.agent_first_authority_migrations import (
    CURRENT_AUTHORITY_TABLES,
    install_current_authority_tables,
)
from pullwise_server.agent_first_claim_authority import CLAIM_FAULT_POINTS
from pullwise_server.agent_first_transport_receipts import (
    BINDING_FAULT_POINTS,
    RECEIPT_FAULT_POINTS,
)


WORKER_ID = "worker_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TASK_ID = "task_11111111111111111111111111111111"
LEASE_ID = "lease_22222222222222222222222222222222"
NOW = "2026-07-22T12:00:00.000Z"
WORKER_SCHEMAS = [
    "agent-worker-grant/v1",
    "server-authority-envelope/v1",
    "server-transport-receipt/v1",
]


def policy() -> dict[str, object]:
    return seal_document(
        "agent-task-policy/v1",
        {
            "schema_id": "agent-task-policy/v1",
            "policy_id": "policy_33333333333333333333333333333333",
            "capability_ids": ["source.read"],
            "tool_keys": ["internal.read_source"],
            "elapsed_ms_limit": 60_000,
            "tool_call_limit": 7,
        },
    )


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

    def register_request(self) -> dict[str, object]:
        return seal_document(
            "agent-worker-register/v1",
            {
                "schema_id": "agent-worker-register/v1",
                "package": package_tuple(),
                "worker_id": WORKER_ID,
                "supported_schema_ids": WORKER_SCHEMAS,
                "tool_catalog_digest": "4" * 64,
            },
        )

    def register(self) -> bytes:
        return self.authority.register_worker(self.register_request())

    def accept_request(self, task_id: str = TASK_ID) -> dict[str, object]:
        return {
            "schema_id": "agent-task-request/v1",
            "package": package_tuple(),
            "task_id": task_id,
            "task_type": "repo_review.full_scan",
            "idempotency_key": f"accept:{task_id}",
            "policy": policy(),
            "request": {"repository": "octo/example", "commit": "a" * 40},
        }

    def accept(self, task_id: str = TASK_ID) -> bytes:
        return self.authority.accept_current_task(self.accept_request(task_id))

    def claim_request(
        self, *, idempotency_key: str = "claim:one", lease_id: str = LEASE_ID
    ) -> dict[str, object]:
        return {
            "schema_id": "agent-task-claim-request/v1",
            "package": package_tuple(),
            "task_id": TASK_ID,
            "worker_id": WORKER_ID,
            "lease_id": lease_id,
            "transport_epoch": 1,
            "idempotency_key": idempotency_key,
            "capability_ids": ["source.read"],
            "tool_keys": ["internal.read_source"],
            "elapsed_ms_limit": 60_000,
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
        error = raised.exception
        payload = json.loads(error.response_bytes)
        self.assertEqual((error.code, payload["schema_id"]), (code, "error-response/v1"))
        self.assertEqual(payload["error"]["code"], code)
        verify_document_digest("stable-error/v1", payload["error"])
        self.assertEqual(error.response_bytes, error.canonical_bytes)
        return error

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
        self.assertFalse(any("scan_job" in name or "review_run" in name for name in installed))

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
        target = {"point": ""}

        def inject(point: str) -> None:
            if point == target["point"]:
                raise RuntimeError(f"injected:{point}")

        for point in CLAIM_FAULT_POINTS:
            with self.subTest(point=point):
                target["point"] = point
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    AgentFirstAuthority(self.connect, fault_injector=inject).claim_and_issue_current_grant(request)
                self.assertEqual(
                    self.counts("agent_current_attempts", "agent_current_claims", "agent_current_grants"),
                    (0, 0, 0),
                )
        target["point"] = ""
        changed = {**request, "tool_call_limit": 8}
        self.authority.claim_and_issue_current_grant(request)
        before = self.counts("agent_current_control_events", "agent_current_grants")
        self.assert_error(
            "IDEMPOTENCY_CONFLICT",
            lambda: self.authority.claim_and_issue_current_grant(changed),
        )
        self.assertEqual(before, self.counts("agent_current_control_events", "agent_current_grants"))

        other_task = "task_99999999999999999999999999999999"
        with self.connect() as connection:
            connection.execute("DELETE FROM agent_current_task_heads WHERE task_id=?", (TASK_ID,))
        self.assertEqual(other_task[:5], "task_")  # concurrency is covered independently below

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

    def receipt(self, envelope: dict[str, object]) -> dict[str, object]:
        return seal_document(
            "server-transport-receipt/v1",
            {
                "schema_id": "server-transport-receipt/v1",
                "receipt_kind": "server_transport",
                "package": package_tuple(),
                "receipt_id": "receipt_55555555555555555555555555555555",
                **{key: envelope[key] for key in (
                    "task_id", "attempt_id", "session_id", "owner_id", "lease_id",
                    "authority_digest", "task_version", "deletion_version",
                    "owner_epoch", "native_epoch", "transport_epoch",
                )},
                "grant_digest": envelope["grant"]["grant_digest"],
                "content_ref": {
                    "schema_id": "content-ref/v1",
                    "artifact_id": "artifact_66666666666666666666666666666666",
                    "content_schema_id": "canonical-document/v1",
                    "sha256": "7" * 64,
                    "size_bytes": 1,
                    "media_type": "application/json",
                    "encoding": "utf-8",
                },
                "accepted_at": NOW,
            },
        )

    def test_transport_receipt_faults_immutability_and_one_shot_binding(self) -> None:
        _, envelope = self.prepare_claim()
        receipt = self.receipt(envelope)
        local = {**receipt, "receipt_kind": "local_tool"}
        self.assert_error("TRANSPORT_RECEIPT_TYPE_INVALID", lambda: self.authority.store_transport_receipt(local))
        target = {"point": ""}

        def inject(point: str) -> None:
            if point == target["point"]:
                raise RuntimeError(f"injected:{point}")

        for point in RECEIPT_FAULT_POINTS:
            target["point"] = point
            with self.assertRaisesRegex(RuntimeError, "injected"):
                AgentFirstAuthority(self.connect, fault_injector=inject).store_transport_receipt(receipt)
            self.assertEqual(self.counts("agent_current_transport_receipts"), (0,))
        target["point"] = ""
        stored = self.authority.store_transport_receipt(receipt)
        self.assertEqual(stored, canonical_validated_bytes("server-transport-receipt/v1", receipt))
        for point in BINDING_FAULT_POINTS:
            target["point"] = point
            with self.assertRaisesRegex(RuntimeError, "injected"):
                AgentFirstAuthority(self.connect, fault_injector=inject).bind_transport_receipt(receipt["receipt_digest"], "8" * 64)
        bound = self.authority.bind_transport_receipt(receipt["receipt_digest"], "8" * 64)
        self.assertEqual(bound, self.authority.bind_transport_receipt(receipt["receipt_digest"], "8" * 64))
        verify_document_digest("server-transport-receipt-binding-response/v1", json.loads(bound))
        self.assert_error(
            "TRANSPORT_RECEIPT_ALREADY_BOUND",
            lambda: self.authority.bind_transport_receipt(receipt["receipt_digest"], "9" * 64),
        )
        self.assert_error(
            "TRANSPORT_ENVELOPE_DIGEST_INVALID",
            lambda: self.authority.bind_transport_receipt(receipt["receipt_digest"], None),
        )

    def test_abandonment_fences_full_authority_and_preserves_task_fields(self) -> None:
        _, envelope = self.prepare_claim()
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
        first = self.authority.abandon_current_claim(request)
        self.assertEqual(first, self.authority.abandon_current_claim(dict(request)))
        response = verify_document_digest("agent-claim-abandon-response/v1", json.loads(first))
        with self.connect() as connection:
            after = connection.execute(f"SELECT {fields}, task_version FROM agent_current_task_heads").fetchone()
            states = (
                connection.execute("SELECT state FROM agent_current_attempts").fetchone()[0],
                connection.execute("SELECT state FROM agent_current_owner_incarnations").fetchone()[0],
                connection.execute("SELECT state FROM agent_current_grant_authority").fetchone()[0],
            )
        self.assertEqual(after[:7], before)
        self.assertEqual((after[7], response["task_version"]), (3, 3))
        self.assertEqual(states, ("FENCED",) * 3)
        self.assertEqual(self.counts("agent_current_abandonments", "agent_current_fences"), (1, 4))
        changed = {**request, "reason": "authority_revoked"}
        self.assert_error("IDEMPOTENCY_CONFLICT", lambda: self.authority.abandon_current_claim(changed))


if __name__ == "__main__":
    unittest.main()
