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
_ATTEMPT_TERMINAL_STATES = {
    "COMPLETED": "SUCCEEDED",
    "NO_CHANGE_NEEDED": "SUCCEEDED",
    "COMPLETED_WITH_WAIVERS": "SUCCEEDED",
    "PARTIAL": "FAILED",
    "BLOCKED": "FAILED",
    "FAILED": "FAILED",
    "CANCELLED": "CANCELLED",
}


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
            current = self._current_authority(connection, values["task_id"])
            if current is None:
                raise AuthorityStoreError("AUTHORITY_FENCED")
            receipt = self._receipt(connection, values)
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
        try:
            connection.execute(
                """
                INSERT INTO agent_current_terminal_results (
                    transport_envelope_digest, task_result_digest,
                    task_result_core_digest, result_id, task_id, outcome,
                    published_from_version, terminal_task_version,
                    diagnostics_state, receipt_digest, receipt_ref_sha256,
                    attempt_id, session_id, owner_id, grant_id, lease_id,
                    deletion_version, owner_epoch, native_epoch, transport_epoch,
                    grant_digest, authority_digest, package_identity,
                    package_version, content_sha256, root_sha256,
                    task_result_bytes, task_result_core_bytes,
                    worker_debug_descriptor_bytes, envelope_bytes, response_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["transport_envelope_digest"],
                    values["task_result_digest"],
                    values["task_result_core_digest"],
                    values["result_id"],
                    values["task_id"],
                    values["outcome"],
                    values["published_from_version"],
                    values["terminal_task_version"],
                    values["diagnostics_state"],
                    None if receipt is None else receipt["receipt_digest"],
                    values["receipt_ref_sha256"],
                    values["attempt_id"],
                    values["session_id"],
                    values["owner_id"],
                    values["grant_id"],
                    values["lease_id"],
                    values["deletion_version"],
                    values["owner_epoch"],
                    values["native_epoch"],
                    values["transport_epoch"],
                    values["grant_digest"],
                    values["authority_digest"],
                    *values["package_tuple"],
                    values["task_result_bytes"],
                    values["task_result_core_bytes"],
                    values["worker_debug_descriptor_bytes"],
                    values["envelope_bytes"],
                    values["response_bytes"],
                ),
            )
        except sqlite3.IntegrityError:
            raise AuthorityStoreError("TERMINAL_RESULT_CONFLICT") from None
        self._fault("terminal.after_result")
        self._close_current_authority(connection, values)
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

    def _close_current_authority(
        self,
        connection: sqlite3.Connection,
        values: Mapping[str, object],
    ) -> None:
        attempt_state = _ATTEMPT_TERMINAL_STATES.get(values["outcome"])
        if attempt_state is None:
            raise AuthorityStoreError("TERMINAL_OUTCOME_INVALID")
        self._fault("terminal.before_attempt")
        changed = connection.execute(
            """
            UPDATE agent_current_attempts SET state=?
            WHERE attempt_id=? AND state='CLAIMED'
            """,
            (attempt_state, values["attempt_id"]),
        ).rowcount
        if changed != 1:
            raise AuthorityStoreError("AUTHORITY_FENCED")
        self._fault("terminal.after_attempt")
        self._fault("terminal.before_owner")
        changed = connection.execute(
            """
            UPDATE agent_current_owner_incarnations SET state='CLOSED'
            WHERE session_id=? AND state='STARTING'
            """,
            (values["session_id"],),
        ).rowcount
        if changed != 1:
            raise AuthorityStoreError("AUTHORITY_FENCED")
        self._fault("terminal.after_owner")
        self._fault("terminal.before_grant_authority")
        changed = connection.execute(
            """
            UPDATE agent_current_grant_authority
            SET state='REVOKED', authority_version=authority_version+1
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
                current_authority_schema_id=NULL,
                current_authority_digest=NULL, updated_at=strftime('%s','now')
            WHERE task_id=? AND task_version=? AND deletion_version=?
              AND current_attempt_id=? AND current_session_id=?
              AND current_grant_id=? AND current_lease_id=?
              AND current_authority_digest=?
              AND lifecycle IN ('ACTIVE','FINALIZING')
            """,
            (
                values["terminal_task_version"], values["transport_envelope_digest"],
                values["task_result_digest"], values["outcome"], values["task_id"],
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
                values["transport_envelope_digest"],
                values["binding_response_bytes"],
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
]
