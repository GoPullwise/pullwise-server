"""Shared transactions for current worker registration and Task acceptance."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import contextmanager
from typing import Callable, Iterator, Mapping


PackageTuple = tuple[str, str, str, str]
FaultInjector = Callable[[str], None]
ACCEPT_FAULT_POINTS = (
    "accept.before_task_request",
    "accept.after_task_request",
    "accept.before_task_head",
    "accept.after_task_head",
    "accept.before_event",
    "accept.after_event",
)


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

    @classmethod
    def _event_replay(
        cls,
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
        return cls._blob(row["response_bytes"])

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
            current = connection.execute(
                """
                SELECT r.* FROM agent_current_worker_registration_heads h
                JOIN agent_current_worker_registrations r
                  ON r.registration_id = h.registration_id
                WHERE h.worker_id = ?
                """,
                (values["worker_id"],),
            ).fetchone()
            if current is not None:
                exact = (
                    self._row_package(current) == values["package_tuple"]
                    and current["request_digest"] == values["request_digest"]
                )
                if exact:
                    return self._blob(current["response_bytes"])
            existing = connection.execute(
                """
                SELECT * FROM agent_current_worker_registrations
                WHERE worker_id=? AND package_identity=? AND package_version=?
                  AND content_sha256=? AND root_sha256=?
                """,
                (values["worker_id"], *values["package_tuple"]),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != values["request_digest"]:
                    raise AuthorityStoreError("WORKER_REGISTRATION_CONFLICT")
                registration_id = existing["registration_id"]
                response = self._blob(existing["response_bytes"])
            else:
                registration_id = values["registration_id"]
                response = values["response_bytes"]
                self._fault("register.before_registration")
                connection.execute(
                    """
                    INSERT INTO agent_current_worker_registrations (
                        registration_id, worker_id, package_identity, package_version,
                        content_sha256, root_sha256, supported_schema_ids,
                        tool_catalog_digest, request_digest, request_bytes, response_bytes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        registration_id,
                        values["worker_id"],
                        *values["package_tuple"],
                        values["supported_schema_ids"],
                        values["tool_catalog_digest"],
                        values["request_digest"],
                        values["request_bytes"],
                        response,
                    ),
                )
                self._fault("register.after_registration")
            connection.execute(
                """
                INSERT INTO agent_current_worker_registration_heads
                    (worker_id, registration_id)
                VALUES (?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    registration_id=excluded.registration_id,
                    updated_at=strftime('%s','now')
                """,
                (values["worker_id"], registration_id),
            )
            return response  # type: ignore[return-value]

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
            if connection.execute(
                "SELECT 1 FROM agent_current_task_requests WHERE task_id = ?",
                (values["task_id"],),
            ).fetchone() is not None:
                raise AuthorityStoreError("TASK_ALREADY_EXISTS")
            self._fault("accept.before_task_request")
            connection.execute(
                """
                INSERT INTO agent_current_task_requests (
                    task_id, task_type, package_identity, package_version,
                    content_sha256, root_sha256, policy_digest, policy_bytes,
                    idempotency_key, request_digest, request_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["task_id"],
                    values["task_type"],
                    *values["package_tuple"],
                    values["policy_digest"],
                    values["policy_bytes"],
                    values["idempotency_key"],
                    values["request_digest"],
                    values["request_bytes"],
                ),
            )
            self._fault("accept.after_task_request")
            self._fault("accept.before_task_head")
            connection.execute(
                """
                INSERT INTO agent_current_task_heads (
                    task_id, owner_id, lifecycle, desired_state, task_version
                ) VALUES (?, ?, 'QUEUED', 'RUN', 1)
                """,
                (values["task_id"], values["owner_id"]),
            )
            self._fault("accept.after_task_head")
            self._fault("accept.before_event")
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


__all__ = [
    "ACCEPT_FAULT_POINTS",
    "AgentFirstAuthorityStore",
    "AuthorityStoreError",
    "FaultInjector",
    "PackageTuple",
]
