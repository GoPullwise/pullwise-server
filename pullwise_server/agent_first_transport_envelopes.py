"""Atomic current TaskResult transport-envelope terminal authority."""

from __future__ import annotations

import sqlite3
from typing import Callable, Mapping

from .agent_first_authority_store import AgentFirstAuthorityStore, AuthorityStoreError


TERMINAL_COMMON_FAULT_POINTS = tuple(
    f"terminal.{side}_{stage}"
    for stage in (
        "result",
        "attempt",
        "owner",
        "grant_authority",
        "task_head",
        "event",
    )
    for side in ("before", "after")
)
TERMINAL_BINDING_FAULT_POINTS = (
    "terminal.before_binding",
    "terminal.after_binding",
)


_TERMINAL_DDL = """
CREATE TABLE IF NOT EXISTS agent_current_terminal_results (
    transport_envelope_digest TEXT PRIMARY KEY CHECK(
        length(transport_envelope_digest) = 64
    ),
    task_result_digest TEXT NOT NULL UNIQUE CHECK(length(task_result_digest) = 64),
    task_result_core_digest TEXT NOT NULL CHECK(length(task_result_core_digest) = 64),
    result_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL UNIQUE,
    outcome TEXT NOT NULL,
    published_from_version INTEGER NOT NULL CHECK(published_from_version >= 1),
    terminal_task_version INTEGER NOT NULL CHECK(
        terminal_task_version = published_from_version + 1
    ),
    diagnostics_state TEXT NOT NULL CHECK(
        diagnostics_state IN ('uploaded','local_only','unavailable','not_applicable')
    ),
    receipt_digest TEXT UNIQUE,
    receipt_ref_sha256 TEXT CHECK(
        receipt_ref_sha256 IS NULL OR length(receipt_ref_sha256) = 64
    ),
    task_id_fence TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    deletion_version INTEGER NOT NULL CHECK(deletion_version >= 0),
    owner_epoch INTEGER NOT NULL CHECK(owner_epoch >= 1),
    native_epoch INTEGER NOT NULL CHECK(native_epoch >= 1),
    transport_epoch INTEGER NOT NULL CHECK(transport_epoch >= 1),
    grant_digest TEXT NOT NULL CHECK(length(grant_digest) = 64),
    authority_digest TEXT NOT NULL CHECK(length(authority_digest) = 64),
    package_identity TEXT NOT NULL,
    package_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    root_sha256 TEXT NOT NULL CHECK(length(root_sha256) = 64),
    task_result_bytes BLOB NOT NULL,
    task_result_core_bytes BLOB NOT NULL,
    worker_debug_descriptor_bytes BLOB,
    envelope_bytes BLOB NOT NULL,
    response_bytes BLOB NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    CHECK(
        (diagnostics_state='uploaded' AND receipt_digest IS NOT NULL
         AND receipt_ref_sha256 IS NOT NULL)
        OR
        (diagnostics_state!='uploaded' AND receipt_digest IS NULL
         AND receipt_ref_sha256 IS NULL)
    ),
    FOREIGN KEY(task_id) REFERENCES agent_current_task_heads(task_id),
    FOREIGN KEY(receipt_digest)
        REFERENCES agent_current_transport_receipts(receipt_digest)
)
"""


def install_transport_envelope_tables(connection: sqlite3.Connection) -> None:
    connection.execute(_TERMINAL_DDL)
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS agent_current_terminal_results_update_immutable
        BEFORE UPDATE ON agent_current_terminal_results
        BEGIN
            SELECT RAISE(ABORT, 'AGENT_CURRENT_TERMINAL_RESULTS_IMMUTABLE');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS agent_current_terminal_results_delete_immutable
        BEFORE DELETE ON agent_current_terminal_results
        BEGIN
            SELECT RAISE(ABORT, 'AGENT_CURRENT_TERMINAL_RESULTS_IMMUTABLE');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS agent_current_task_head_monotonic
        BEFORE UPDATE ON agent_current_task_heads
        WHEN NEW.task_id IS NOT OLD.task_id
          OR NEW.owner_id IS NOT OLD.owner_id
          OR NEW.task_version != OLD.task_version + 1
          OR NEW.deletion_version < OLD.deletion_version
          OR NEW.owner_epoch < OLD.owner_epoch
          OR NEW.native_epoch < OLD.native_epoch
          OR NEW.transport_epoch < OLD.transport_epoch
          OR OLD.lifecycle='TERMINAL'
          OR (
            OLD.current_authority_schema_id='agent-claim-abandon-response/v1'
            AND NEW.lifecycle!='TERMINAL'
          )
        BEGIN
            SELECT RAISE(ABORT, 'AGENT_CURRENT_TASK_HEAD_CAS_INVALID');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS agent_current_task_head_delete_immutable
        BEFORE DELETE ON agent_current_task_heads
        BEGIN
            SELECT RAISE(ABORT, 'AGENT_CURRENT_TASK_HEAD_IMMUTABLE');
        END
        """
    )


class TransportEnvelopeStore(AgentFirstAuthorityStore):
    def commit(
        self,
        values: Mapping[str, object],
        build: Callable[[sqlite3.Row, sqlite3.Row | None], Mapping[str, object]],
    ) -> bytes:
        with self._immediate() as connection:
            replay = connection.execute(
                "SELECT * FROM agent_current_terminal_results WHERE task_id=?",
                (values["task_id"],),
            ).fetchone()
            if replay is not None:
                exact = (
                    replay["transport_envelope_digest"]
                    == values["transport_envelope_digest"]
                    and replay["task_result_digest"] == values["task_result_digest"]
                    and replay["receipt_ref_sha256"] == values["receipt_ref_sha256"]
                    and self._blob(replay["envelope_bytes"])
                    == values["envelope_bytes"]
                )
                if not exact:
                    raise AuthorityStoreError("TERMINAL_RESULT_CONFLICT")
                return self._blob(replay["response_bytes"])
            receipt = self._receipt(connection, values)
            current = self._current_authority(connection, values["task_id"])
            if current is None:
                raise AuthorityStoreError("AUTHORITY_FENCED")
            write = {**values, **build(current, receipt)}
            self._assert_exact_authority(current, write)
            self._insert_terminal_write_set(connection, current, receipt, write)
            return write["response_bytes"]  # type: ignore[return-value]

    def _receipt(
        self,
        connection: sqlite3.Connection,
        values: Mapping[str, object],
    ) -> sqlite3.Row | None:
        if values["diagnostics_state"] != "uploaded":
            if values["receipt_ref_sha256"] is not None:
                raise AuthorityStoreError("TRANSPORT_RECEIPT_BINDING_CONFLICT")
            return None
        row = connection.execute(
            """
            SELECT r.*, b.transport_envelope_digest AS bound_digest
            FROM agent_current_transport_receipts r
            JOIN agent_current_transport_receipt_bindings b USING (receipt_digest)
            WHERE r.receipt_bytes_sha256=? AND r.receipt_size_bytes=?
            """,
            (values["receipt_ref_sha256"], values["receipt_ref_size_bytes"]),
        ).fetchone()
        if row is None:
            raise AuthorityStoreError("TRANSPORT_RECEIPT_NOT_FOUND")
        if row["bound_digest"] is not None:
            raise AuthorityStoreError("TRANSPORT_RECEIPT_ALREADY_BOUND")
        return row

    @staticmethod
    def _current_authority(
        connection: sqlite3.Connection,
        task_id: object,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT h.*, r.package_identity, r.package_version, r.content_sha256,
                   r.root_sha256, a.state AS attempt_state,
                   o.state AS owner_state, ga.state AS grant_state,
                   g.grant_digest, g.grant_bytes, c.authority_digest,
                   c.authority_bytes
            FROM agent_current_task_heads h
            JOIN agent_current_task_requests r USING (task_id)
            JOIN agent_current_attempts a ON a.attempt_id=h.current_attempt_id
            JOIN agent_current_owner_incarnations o ON o.session_id=h.current_session_id
            JOIN agent_current_grants g ON g.grant_id=h.current_grant_id
            JOIN agent_current_grant_authority ga ON ga.grant_id=g.grant_id
            JOIN agent_current_claims c ON c.grant_id=g.grant_id
            WHERE h.task_id=?
              AND h.current_authority_schema_id='server-authority-envelope/v1'
              AND h.lifecycle IN ('ACTIVE','FINALIZING')
            """,
            (task_id,),
        ).fetchone()

    def _assert_exact_authority(
        self,
        current: sqlite3.Row,
        values: Mapping[str, object],
    ) -> None:
        if tuple(
            current[key] for key in ("attempt_state", "owner_state", "grant_state")
        ) != ("CLAIMED", "STARTING", "ACTIVE"):
            raise AuthorityStoreError("AUTHORITY_FENCED")
        pairs = (
            ("task_id", "task_id"),
            ("attempt_id", "current_attempt_id"),
            ("session_id", "current_session_id"),
            ("owner_id", "owner_id"),
            ("grant_id", "current_grant_id"),
            ("lease_id", "current_lease_id"),
            ("published_from_version", "task_version"),
            ("deletion_version", "deletion_version"),
            ("owner_epoch", "owner_epoch"),
            ("native_epoch", "native_epoch"),
            ("transport_epoch", "transport_epoch"),
            ("grant_digest", "grant_digest"),
            ("authority_digest", "current_authority_digest"),
        )
        exact = all(values[left] == current[right] for left, right in pairs)
        exact = exact and self._row_package(current) == values["package_tuple"]
        exact = exact and values["terminal_task_version"] == current["task_version"] + 1
        if not exact:
            raise AuthorityStoreError("AUTHORITY_MISMATCH")

    def _insert_terminal_write_set(
        self,
        connection: sqlite3.Connection,
        current: sqlite3.Row,
        receipt: sqlite3.Row | None,
        values: Mapping[str, object],
    ) -> None:
        self._fault("terminal.before_result")
        connection.execute(
            """
            INSERT INTO agent_current_terminal_results (
                transport_envelope_digest, task_result_digest,
                task_result_core_digest, result_id, task_id, outcome,
                published_from_version, terminal_task_version, diagnostics_state,
                receipt_digest, receipt_ref_sha256, task_id_fence, attempt_id,
                session_id, owner_id, grant_id, lease_id, deletion_version,
                owner_epoch, native_epoch, transport_epoch, grant_digest,
                authority_digest, package_identity, package_version,
                content_sha256, root_sha256, task_result_bytes,
                task_result_core_bytes, worker_debug_descriptor_bytes,
                envelope_bytes, response_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                values["transport_envelope_digest"], values["task_result_digest"],
                values["task_result_core_digest"], values["result_id"],
                values["task_id"], values["outcome"],
                values["published_from_version"], values["terminal_task_version"],
                values["diagnostics_state"],
                None if receipt is None else receipt["receipt_digest"],
                values["receipt_ref_sha256"], values["task_id"],
                values["attempt_id"], values["session_id"], values["owner_id"],
                values["grant_id"], values["lease_id"],
                values["deletion_version"], values["owner_epoch"],
                values["native_epoch"], values["transport_epoch"],
                values["grant_digest"], values["authority_digest"],
                *values["package_tuple"], values["task_result_bytes"],
                values["task_result_core_bytes"],
                values["worker_debug_descriptor_bytes"],
                values["envelope_bytes"], values["response_bytes"],
            ),
        )
        self._fault("terminal.after_result")
        self._fence_current_authority(connection, values)
        self._commit_task_head(connection, current, values)
        if receipt is not None:
            self._bind_receipt(connection, receipt, values)
        self._fault("terminal.before_event")
        self._insert_event(
            connection,
            task_id=values["task_id"],  # type: ignore[arg-type]
            event_type="task.terminal_committed",
            idempotency_key=f"terminal:{values['task_result_digest']}",
            request_digest=values["transport_envelope_digest"],  # type: ignore[arg-type]
            response_bytes=values["response_bytes"],  # type: ignore[arg-type]
            task_version=values["terminal_task_version"],  # type: ignore[arg-type]
        )
        self._fault("terminal.after_event")

    def _fence_current_authority(
        self,
        connection: sqlite3.Connection,
        values: Mapping[str, object],
    ) -> None:
        updates = (
            (
                "attempt",
                "agent_current_attempts",
                "attempt_id",
                values["attempt_id"],
                "CLAIMED",
            ),
            (
                "owner",
                "agent_current_owner_incarnations",
                "session_id",
                values["session_id"],
                "STARTING",
            ),
        )
        for stage, table, key, identity, state in updates:
            self._fault(f"terminal.before_{stage}")
            changed = connection.execute(
                f"UPDATE {table} SET state='FENCED', "
                "fenced_at=strftime('%s','now'), fence_reason='terminal_committed' "
                f"WHERE {key}=? AND state=?",
                (identity, state),
            ).rowcount
            if changed != 1:
                raise AuthorityStoreError("AUTHORITY_FENCED")
            self._fault(f"terminal.after_{stage}")
        self._fault("terminal.before_grant_authority")
        changed = connection.execute(
            """
            UPDATE agent_current_grant_authority
            SET state='FENCED', authority_version=authority_version+1,
                fenced_at=strftime('%s','now'), fence_reason='terminal_committed'
            WHERE grant_id=? AND state='ACTIVE'
            """,
            (values["grant_id"],),
        ).rowcount
        if changed != 1:
            raise AuthorityStoreError("AUTHORITY_FENCED")
        self._fault("terminal.after_grant_authority")

    def _commit_task_head(
        self,
        connection: sqlite3.Connection,
        current: sqlite3.Row,
        values: Mapping[str, object],
    ) -> None:
        self._fault("terminal.before_task_head")
        changed = connection.execute(
            """
            UPDATE agent_current_task_heads
            SET lifecycle='TERMINAL', task_version=?, terminal_kind='task_result',
                result_ref=?, result_digest=?, outcome=?,
                terminal_at=strftime('%s','now'),
                current_authority_schema_id='task-result-transport-envelope/v1',
                current_authority_digest=?, updated_at=strftime('%s','now')
            WHERE task_id=? AND task_version=? AND deletion_version=?
              AND current_attempt_id=? AND current_session_id=?
              AND current_grant_id=? AND current_lease_id=?
              AND current_authority_digest=?
              AND lifecycle IN ('ACTIVE','FINALIZING')
            """,
            (
                values["terminal_task_version"], values["transport_envelope_digest"],
                values["task_result_digest"], values["outcome"],
                values["transport_envelope_digest"], values["task_id"],
                values["published_from_version"], values["deletion_version"],
                values["attempt_id"], values["session_id"], values["grant_id"],
                values["lease_id"], current["current_authority_digest"],
            ),
        ).rowcount
        if changed != 1:
            raise AuthorityStoreError("TASK_NOT_CLAIMABLE")
        self._fault("terminal.after_task_head")

    def _bind_receipt(
        self,
        connection: sqlite3.Connection,
        receipt: sqlite3.Row,
        values: Mapping[str, object],
    ) -> None:
        self._fault("terminal.before_binding")
        changed = connection.execute(
            """
            UPDATE agent_current_transport_receipt_bindings
            SET transport_envelope_digest=?, response_bytes=?,
                bound_at=strftime('%s','now')
            WHERE receipt_digest=? AND transport_envelope_digest IS NULL
            """,
            (
                values["transport_envelope_digest"], values["response_bytes"],
                receipt["receipt_digest"],
            ),
        ).rowcount
        if changed != 1:
            raise AuthorityStoreError("TRANSPORT_RECEIPT_ALREADY_BOUND")
        self._fault("terminal.after_binding")


__all__ = [
    "TERMINAL_BINDING_FAULT_POINTS",
    "TERMINAL_COMMON_FAULT_POINTS",
    "TransportEnvelopeStore",
    "install_transport_envelope_tables",
]
