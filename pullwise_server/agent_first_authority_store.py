"""Transactional SQLite store for the current Agent-First authority service."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import contextmanager
from typing import Callable, Iterator, Mapping


PackageTuple = tuple[str, str, str, str]
FaultInjector = Callable[[str], None]


class AuthorityStoreError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class AgentFirstAuthorityStore:
    def __init__(
        self,
        connect_factory: Callable[[], sqlite3.Connection],
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self._connect_factory = connect_factory
        self._fault_injector = fault_injector

    def _fault(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    @contextmanager
    def _immediate(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect_factory()
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _row_package(row: sqlite3.Row) -> PackageTuple:
        return (
            row["package_identity"],
            row["package_version"],
            row["content_sha256"],
            row["root_sha256"],
        )

    @staticmethod
    def _blob(value: object) -> bytes:
        if not isinstance(value, bytes):
            raise AuthorityStoreError("AUTHORITY_STORAGE_CORRUPT")
        return value

    @staticmethod
    def _event_replay(
        connection: sqlite3.Connection,
        task_id: str,
        idempotency_key: str,
        request_digest: str,
    ) -> bytes | None:
        row = connection.execute(
            """
            SELECT request_digest, response_bytes
            FROM agent_current_control_events
            WHERE task_id = ? AND idempotency_key = ?
            """,
            (task_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["request_digest"] != request_digest:
            raise AuthorityStoreError("IDEMPOTENCY_CONFLICT")
        return AgentFirstAuthorityStore._blob(row["response_bytes"])

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        task_id: str,
        event_type: str,
        idempotency_key: str,
        request_digest: str,
        response_bytes: bytes,
        task_version: int,
    ) -> None:
        sequence = connection.execute(
            """
            SELECT COALESCE(MAX(event_seq), 0) + 1
            FROM agent_current_control_events WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO agent_current_control_events (
                event_id, task_id, event_seq, event_type, idempotency_key,
                request_digest, response_digest, response_bytes,
                applied_task_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"event_{secrets.token_hex(16)}",
                task_id,
                sequence,
                event_type,
                idempotency_key,
                request_digest,
                hashlib.sha256(response_bytes).hexdigest(),
                response_bytes,
                task_version,
            ),
        )

    def register_worker(self, values: Mapping[str, object]) -> bytes:
        with self._immediate() as connection:
            row = connection.execute(
                "SELECT * FROM agent_current_worker_registrations WHERE worker_id = ?",
                (values["worker_id"],),
            ).fetchone()
            if row is not None:
                exact = (
                    self._row_package(row) == values["package_tuple"]
                    and row["request_digest"] == values["request_digest"]
                )
                if not exact:
                    raise AuthorityStoreError("WORKER_REGISTRATION_CONFLICT")
                return self._blob(row["response_bytes"])
            self._fault("register.before_registration")
            connection.execute(
                """
                INSERT INTO agent_current_worker_registrations (
                    worker_id, package_identity, package_version, content_sha256,
                    root_sha256, request_digest, request_bytes, response_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["worker_id"],
                    *values["package_tuple"],
                    values["request_digest"],
                    values["request_bytes"],
                    values["response_bytes"],
                ),
            )
            self._fault("register.after_registration")
            return values["response_bytes"]  # type: ignore[return-value]

    def accept_task(self, values: Mapping[str, object]) -> bytes:
        with self._immediate() as connection:
            replay = self._event_replay(
                connection,
                values["task_id"],  # type: ignore[arg-type]
                values["idempotency_key"],  # type: ignore[arg-type]
                values["request_digest"],  # type: ignore[arg-type]
            )
            if replay is not None:
                return replay
            exists = connection.execute(
                "SELECT 1 FROM agent_current_task_requests WHERE task_id = ?",
                (values["task_id"],),
            ).fetchone()
            if exists is not None:
                raise AuthorityStoreError("TASK_ALREADY_EXISTS")
            self._fault("accept.before_task_request")
            connection.execute(
                """
                INSERT INTO agent_current_task_requests (
                    task_id, task_type, package_identity, package_version,
                    content_sha256, root_sha256, idempotency_key,
                    request_digest, request_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["task_id"],
                    values["task_type"],
                    *values["package_tuple"],
                    values["idempotency_key"],
                    values["request_digest"],
                    values["request_bytes"],
                ),
            )
            self._fault("accept.after_task_request")
            connection.execute(
                """
                INSERT INTO agent_current_task_heads (
                    task_id, owner_id, lifecycle, desired_state, task_version
                ) VALUES (?, ?, 'QUEUED', 'RUN', 1)
                """,
                (values["task_id"], values["owner_id"]),
            )
            self._fault("accept.after_task_head")
            self._insert_event(
                connection,
                task_id=values["task_id"],  # type: ignore[arg-type]
                event_type="task.accepted",
                idempotency_key=values["idempotency_key"],  # type: ignore[arg-type]
                request_digest=values["request_digest"],  # type: ignore[arg-type]
                response_bytes=values["response_bytes"],  # type: ignore[arg-type]
                task_version=1,
            )
            self._fault("accept.after_event")
            return values["response_bytes"]  # type: ignore[return-value]

    def claim_task(
        self,
        values: Mapping[str, object],
        build: Callable[[sqlite3.Row], Mapping[str, object]],
    ) -> bytes:
        with self._immediate() as connection:
            replay = self._event_replay(
                connection,
                values["task_id"],  # type: ignore[arg-type]
                values["idempotency_key"],  # type: ignore[arg-type]
                values["request_digest"],  # type: ignore[arg-type]
            )
            if replay is not None:
                return replay
            worker = connection.execute(
                "SELECT * FROM agent_current_worker_registrations WHERE worker_id = ?",
                (values["worker_id"],),
            ).fetchone()
            if worker is None:
                raise AuthorityStoreError("WORKER_NOT_REGISTERED")
            if self._row_package(worker) != values["package_tuple"]:
                raise AuthorityStoreError("WORKER_PACKAGE_MISMATCH")
            head = connection.execute(
                """
                SELECT h.*, r.package_identity, r.package_version,
                       r.content_sha256, r.root_sha256
                FROM agent_current_task_heads h
                JOIN agent_current_task_requests r USING (task_id)
                WHERE h.task_id = ?
                """,
                (values["task_id"],),
            ).fetchone()
            if head is None:
                raise AuthorityStoreError("TASK_NOT_FOUND")
            if self._row_package(head) != values["package_tuple"]:
                raise AuthorityStoreError("TASK_PACKAGE_MISMATCH")
            claimable = (
                head["lifecycle"] == "QUEUED"
                and head["desired_state"] == "RUN"
                and head["current_attempt_id"] is None
                and values["transport_epoch"] == head["transport_epoch"] + 1
            )
            if not claimable:
                raise AuthorityStoreError("TASK_NOT_CLAIMABLE")
            write = build(head)
            self._insert_claim_write_set(connection, values, write)
            return write["response_bytes"]  # type: ignore[return-value]

    def _insert_claim_write_set(
        self,
        connection: sqlite3.Connection,
        request: Mapping[str, object],
        write: Mapping[str, object],
    ) -> None:
        self._fault("claim.before_attempt")
        connection.execute(
            "INSERT INTO agent_current_attempts "
            "(attempt_id, task_id, native_epoch, transport_epoch, lease_id, state) "
            "VALUES (?, ?, ?, ?, ?, 'LEASED')",
            (
                write["attempt_id"], request["task_id"], write["native_epoch"],
                request["transport_epoch"], request["lease_id"],
            ),
        )
        self._fault("claim.after_attempt")
        connection.execute(
            "INSERT INTO agent_current_owner_incarnations "
            "(session_id, task_id, attempt_id, owner_id, owner_epoch, state) "
            "VALUES (?, ?, ?, ?, ?, 'STARTING')",
            (
                write["session_id"], request["task_id"], write["attempt_id"],
                write["owner_id"], write["owner_epoch"],
            ),
        )
        self._fault("claim.after_owner")
        connection.execute(
            "INSERT INTO agent_current_grants "
            "(grant_id, task_id, package_identity, package_version, content_sha256, "
            "root_sha256, grant_digest, grant_bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                write["grant_id"], request["task_id"], *request["package_tuple"],
                write["grant_digest"], write["grant_bytes"],
            ),
        )
        self._fault("claim.after_grant")
        connection.execute(
            "INSERT INTO agent_current_grant_authority (grant_id, state) VALUES (?, 'ACTIVE')",
            (write["grant_id"],),
        )
        self._fault("claim.after_grant_authority")
        connection.execute(
            """
            INSERT INTO agent_current_claims (
                claim_id, task_id, attempt_id, session_id, grant_id, worker_id,
                owner_id, lease_id, task_version, deletion_version, owner_epoch,
                native_epoch, transport_epoch, claim_digest, claim_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                write["claim_id"], request["task_id"], write["attempt_id"],
                write["session_id"], write["grant_id"], request["worker_id"],
                write["owner_id"], request["lease_id"], write["task_version"],
                write["deletion_version"], write["owner_epoch"],
                write["native_epoch"], request["transport_epoch"],
                write["claim_digest"], write["claim_bytes"],
            ),
        )
        self._fault("claim.after_claim")
        updated = connection.execute(
            """
            UPDATE agent_current_task_heads
            SET lifecycle='ACTIVE', task_version=?, native_epoch=?, owner_epoch=?,
                transport_epoch=?, current_attempt_id=?, current_session_id=?,
                current_grant_id=?, current_lease_id=?, updated_at=strftime('%s','now')
            WHERE task_id=? AND lifecycle='QUEUED' AND desired_state='RUN'
              AND task_version=? AND deletion_version=? AND current_attempt_id IS NULL
            """,
            (
                write["task_version"], write["native_epoch"], write["owner_epoch"],
                request["transport_epoch"], write["attempt_id"], write["session_id"],
                write["grant_id"], request["lease_id"], request["task_id"],
                write["task_version"] - 1, write["deletion_version"],
            ),
        ).rowcount
        if updated != 1:
            raise AuthorityStoreError("TASK_NOT_CLAIMABLE")
        self._fault("claim.after_task_head")
        self._insert_event(
            connection,
            task_id=request["task_id"],  # type: ignore[arg-type]
            event_type="attempt.claimed",
            idempotency_key=request["idempotency_key"],  # type: ignore[arg-type]
            request_digest=request["request_digest"],  # type: ignore[arg-type]
            response_bytes=write["response_bytes"],  # type: ignore[arg-type]
            task_version=write["task_version"],  # type: ignore[arg-type]
        )
        self._fault("claim.after_event")

    def store_receipt(self, values: Mapping[str, object]) -> bytes:
        with self._immediate() as connection:
            existing = connection.execute(
                "SELECT receipt_bytes, response_bytes FROM agent_current_transport_receipts "
                "WHERE receipt_digest = ?",
                (values["receipt_digest"],),
            ).fetchone()
            if existing is not None:
                if self._blob(existing["receipt_bytes"]) != values["receipt_bytes"]:
                    raise AuthorityStoreError("TRANSPORT_RECEIPT_CONFLICT")
                return self._blob(existing["response_bytes"])
            authority = connection.execute(
                """
                SELECT h.current_attempt_id, h.current_session_id, h.transport_epoch,
                       a.state AS attempt_state, o.state AS owner_state,
                       g.grant_digest, ga.state AS grant_state,
                       r.package_identity, r.package_version,
                       r.content_sha256, r.root_sha256
                FROM agent_current_task_heads h
                JOIN agent_current_task_requests r USING (task_id)
                JOIN agent_current_attempts a ON a.attempt_id=h.current_attempt_id
                JOIN agent_current_owner_incarnations o ON o.session_id=h.current_session_id
                JOIN agent_current_grants g ON g.grant_id=h.current_grant_id
                JOIN agent_current_grant_authority ga ON ga.grant_id=g.grant_id
                WHERE h.task_id=?
                """,
                (values["task_id"],),
            ).fetchone()
            if authority is None:
                raise AuthorityStoreError("AUTHORITY_NOT_FOUND")
            if any(authority[name] == "FENCED" for name in (
                "attempt_state", "owner_state", "grant_state"
            )):
                raise AuthorityStoreError("AUTHORITY_FENCED")
            exact = (
                authority["current_attempt_id"] == values["attempt_id"]
                and authority["current_session_id"] == values["session_id"]
                and authority["transport_epoch"] == values["transport_epoch"]
                and authority["grant_digest"] == values["grant_digest"]
                and self._row_package(authority) == values["package_tuple"]
            )
            if not exact:
                raise AuthorityStoreError("AUTHORITY_MISMATCH")
            self._fault("receipt.before_receipt")
            connection.execute(
                """
                INSERT INTO agent_current_transport_receipts (
                    receipt_digest, receipt_id, receipt_type, task_id, attempt_id,
                    session_id, grant_digest, transport_epoch, package_identity,
                    package_version, content_sha256, root_sha256, receipt_bytes,
                    response_bytes
                ) VALUES (?, ?, 'server_transport', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["receipt_digest"], values["receipt_id"], values["task_id"],
                    values["attempt_id"], values["session_id"], values["grant_digest"],
                    values["transport_epoch"], *values["package_tuple"],
                    values["receipt_bytes"], values["response_bytes"],
                ),
            )
            self._fault("receipt.after_receipt")
            connection.execute(
                "INSERT INTO agent_current_transport_receipt_bindings (receipt_digest) VALUES (?)",
                (values["receipt_digest"],),
            )
            self._fault("receipt.after_binding_head")
            return values["response_bytes"]  # type: ignore[return-value]

    def bind_receipt(
        self, receipt_digest: str, envelope_digest: str, response_bytes: bytes
    ) -> bytes:
        with self._immediate() as connection:
            row = connection.execute(
                """
                SELECT b.transport_envelope_digest, b.response_bytes
                FROM agent_current_transport_receipts r
                JOIN agent_current_transport_receipt_bindings b USING (receipt_digest)
                WHERE r.receipt_digest = ? AND r.receipt_type = 'server_transport'
                """,
                (receipt_digest,),
            ).fetchone()
            if row is None:
                raise AuthorityStoreError("TRANSPORT_RECEIPT_NOT_FOUND")
            current = row["transport_envelope_digest"]
            if current is not None:
                if current != envelope_digest:
                    raise AuthorityStoreError("TRANSPORT_RECEIPT_BINDING_CONFLICT")
                return self._blob(row["response_bytes"])
            self._fault("binding.before_cas")
            changed = connection.execute(
                """
                UPDATE agent_current_transport_receipt_bindings
                SET transport_envelope_digest=?, response_bytes=?,
                    bound_at=strftime('%s','now')
                WHERE receipt_digest=? AND transport_envelope_digest IS NULL
                """,
                (envelope_digest, response_bytes, receipt_digest),
            ).rowcount
            if changed != 1:
                raise AuthorityStoreError("TRANSPORT_RECEIPT_BINDING_CONFLICT")
            self._fault("binding.after_cas")
            return response_bytes

    def abandon_claim(self, values: Mapping[str, object]) -> bytes:
        with self._immediate() as connection:
            replay = self._event_replay(
                connection,
                values["task_id"],  # type: ignore[arg-type]
                values["idempotency_key"],  # type: ignore[arg-type]
                values["request_digest"],  # type: ignore[arg-type]
            )
            if replay is not None:
                return replay
            head = connection.execute(
                "SELECT * FROM agent_current_task_heads WHERE task_id=?",
                (values["task_id"],),
            ).fetchone()
            if head is None:
                raise AuthorityStoreError("TASK_NOT_FOUND")
            keys = (
                "owner_id", "current_attempt_id", "current_session_id",
                "current_grant_id", "current_lease_id", "task_version",
                "deletion_version", "owner_epoch", "native_epoch", "transport_epoch",
            )
            expected = (
                values["owner_id"], values["attempt_id"], values["session_id"],
                values["grant_id"], values["lease_id"], values["expected_task_version"],
                values["deletion_version"], values["owner_epoch"],
                values["native_epoch"], values["transport_epoch"],
            )
            if head["lifecycle"] not in ("ACTIVE", "FINALIZING"):
                raise AuthorityStoreError("AUTHORITY_FENCED")
            if tuple(head[key] for key in keys) != expected:
                raise AuthorityStoreError("AUTHORITY_MISMATCH")
            states = connection.execute(
                """
                SELECT a.state, o.state, ga.state
                FROM agent_current_attempts a
                JOIN agent_current_owner_incarnations o ON o.attempt_id=a.attempt_id
                JOIN agent_current_grant_authority ga ON ga.grant_id=?
                WHERE a.attempt_id=? AND o.session_id=?
                """,
                (values["grant_id"], values["attempt_id"], values["session_id"]),
            ).fetchone()
            if states is None or tuple(states) != ("LEASED", "STARTING", "ACTIVE"):
                raise AuthorityStoreError("AUTHORITY_FENCED")
            self._insert_abandon_write_set(connection, values)
            return values["response_bytes"]  # type: ignore[return-value]

    def _insert_abandon_write_set(
        self, connection: sqlite3.Connection, values: Mapping[str, object]
    ) -> None:
        self._fault("abandon.before_abandonment")
        connection.execute(
            """
            INSERT INTO agent_current_abandonments (
                abandonment_id, task_id, attempt_id, session_id, grant_id, owner_id,
                lease_id, previous_task_version, abandoned_task_version,
                deletion_version, owner_epoch, native_epoch, transport_epoch, reason,
                abandonment_digest, abandonment_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(values[key] for key in (
                "abandonment_id", "task_id", "attempt_id", "session_id", "grant_id",
                "owner_id", "lease_id", "expected_task_version", "task_version",
                "deletion_version", "owner_epoch", "native_epoch", "transport_epoch",
                "reason", "abandonment_digest", "abandonment_bytes",
            )),
        )
        self._fault("abandon.after_abandonment")
        targets = (
            ("transport", values["lease_id"]),
            ("attempt", values["attempt_id"]),
            ("owner", values["session_id"]),
            ("grant", values["grant_id"]),
        )
        for target_type, target_id in targets:
            connection.execute(
                "INSERT INTO agent_current_fences "
                "(fence_id, abandonment_id, target_type, target_id) VALUES (?, ?, ?, ?)",
                (
                    f"fence_{secrets.token_hex(16)}", values["abandonment_id"],
                    target_type, target_id,
                ),
            )
        self._fault("abandon.after_fences")
        attempt = connection.execute(
            "UPDATE agent_current_attempts SET state='FENCED', "
            "fenced_at=strftime('%s','now'), fence_reason=? "
            "WHERE attempt_id=? AND state='LEASED'",
            (values["reason"], values["attempt_id"]),
        ).rowcount
        self._fault("abandon.after_attempt")
        owner = connection.execute(
            "UPDATE agent_current_owner_incarnations SET state='FENCED', "
            "fenced_at=strftime('%s','now'), fence_reason=? "
            "WHERE session_id=? AND state='STARTING'",
            (values["reason"], values["session_id"]),
        ).rowcount
        self._fault("abandon.after_owner")
        grant = connection.execute(
            "UPDATE agent_current_grant_authority SET state='FENCED', "
            "authority_version=authority_version+1, fenced_at=strftime('%s','now'), "
            "fence_reason=? WHERE grant_id=? AND state='ACTIVE'",
            (values["reason"], values["grant_id"]),
        ).rowcount
        self._fault("abandon.after_grant_authority")
        task = connection.execute(
            """
            UPDATE agent_current_task_heads
            SET task_version=?, updated_at=strftime('%s','now')
            WHERE task_id=? AND task_version=? AND deletion_version=?
              AND owner_id=? AND owner_epoch=? AND native_epoch=? AND transport_epoch=?
              AND current_attempt_id=? AND current_session_id=? AND current_grant_id=?
              AND current_lease_id=? AND lifecycle IN ('ACTIVE','FINALIZING')
            """,
            (
                values["task_version"], values["task_id"],
                values["expected_task_version"], values["deletion_version"],
                values["owner_id"], values["owner_epoch"], values["native_epoch"],
                values["transport_epoch"], values["attempt_id"], values["session_id"],
                values["grant_id"], values["lease_id"],
            ),
        ).rowcount
        if (attempt, owner, grant, task) != (1, 1, 1, 1):
            raise AuthorityStoreError("AUTHORITY_FENCED")
        self._fault("abandon.after_task_head")
        self._insert_event(
            connection,
            task_id=values["task_id"],  # type: ignore[arg-type]
            event_type="outer_lease.fenced",
            idempotency_key=values["idempotency_key"],  # type: ignore[arg-type]
            request_digest=values["request_digest"],  # type: ignore[arg-type]
            response_bytes=values["response_bytes"],  # type: ignore[arg-type]
            task_version=values["task_version"],  # type: ignore[arg-type]
        )
        self._fault("abandon.after_event")


__all__ = ["AgentFirstAuthorityStore", "AuthorityStoreError", "FaultInjector"]
