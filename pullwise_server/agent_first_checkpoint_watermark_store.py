"""Atomic Server checkpoint watermark acknowledgement storage."""

from __future__ import annotations

import sqlite3
from typing import Mapping

from .agent_first_authority_store import (
    AgentFirstAuthorityStore,
    AuthorityStoreError,
    FaultInjector,
)


CHECKPOINT_WATERMARK_FAULT_POINTS = (
    "checkpoint_watermark.before_watermark",
    "checkpoint_watermark.after_watermark",
    "checkpoint_watermark.before_head",
    "checkpoint_watermark.after_head",
)


class CheckpointWatermarkStore(AgentFirstAuthorityStore):
    def __init__(
        self,
        connect_factory,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        super().__init__(connect_factory, fault_injector)

    def acknowledge(self, values: Mapping[str, object]) -> bytes:
        with self._immediate() as connection:
            existing = connection.execute(
                """
                SELECT request_bytes, ack_bytes
                FROM agent_current_checkpoint_watermarks
                WHERE task_id=? AND generation=?
                """,
                (values["task_id"], values["generation"]),
            ).fetchone()
            if existing is not None:
                if self._blob(existing["request_bytes"]) != values["request_bytes"]:
                    raise AuthorityStoreError("CHECKPOINT_WATERMARK_CONFLICT")
                return self._blob(existing["ack_bytes"])
            current = self._current_authority(connection, values["task_id"])
            self._assert_current_authority(current, values)
            head = connection.execute(
                """
                SELECT generation, manifest_hash, committed_task_version
                FROM agent_current_checkpoint_watermark_heads WHERE task_id=?
                """,
                (values["task_id"],),
            ).fetchone()
            self._assert_next(current, head, values)
            self._insert_write_set(connection, head, values)
            return values["ack_bytes"]  # type: ignore[return-value]

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
                   g.grant_digest, c.authority_digest
            FROM agent_current_task_heads h
            JOIN agent_current_task_requests r USING (task_id)
            JOIN agent_current_attempts a ON a.attempt_id=h.current_attempt_id
            JOIN agent_current_owner_incarnations o
              ON o.session_id=h.current_session_id
            JOIN agent_current_grants g ON g.grant_id=h.current_grant_id
            JOIN agent_current_grant_authority ga ON ga.grant_id=g.grant_id
            JOIN agent_current_claims c ON c.grant_id=g.grant_id
            WHERE h.task_id=?
              AND h.current_authority_schema_id='server-authority-envelope/v1'
              AND h.lifecycle='ACTIVE' AND h.desired_state='RUN'
            """,
            (task_id,),
        ).fetchone()

    def _assert_current_authority(
        self,
        current: sqlite3.Row | None,
        values: Mapping[str, object],
    ) -> None:
        if current is None or tuple(
            current[field] for field in ("attempt_state", "owner_state", "grant_state")
        ) != ("LEASED", "STARTING", "ACTIVE"):
            raise AuthorityStoreError("AUTHORITY_FENCED")
        pairs = (
            ("task_id", "task_id"),
            ("attempt_id", "current_attempt_id"),
            ("owner_id", "owner_id"),
            ("owner_epoch", "owner_epoch"),
            ("lease_id", "current_lease_id"),
            ("authority_digest", "current_authority_digest"),
            ("grant_id", "current_grant_id"),
            ("grant_digest", "grant_digest"),
            ("deletion_version", "deletion_version"),
            ("native_epoch", "native_epoch"),
            ("transport_epoch", "transport_epoch"),
        )
        if (
            any(values[left] != current[right] for left, right in pairs)
            or self._row_package(current) != values["package_tuple"]
            or current["authority_digest"] != current["current_authority_digest"]
        ):
            raise AuthorityStoreError("AUTHORITY_FENCED")

    @staticmethod
    def _assert_next(
        current: sqlite3.Row,
        head: sqlite3.Row | None,
        values: Mapping[str, object],
    ) -> None:
        if head is None:
            expected = (1, None, current["task_version"])
        else:
            expected = (
                head["generation"] + 1,
                head["manifest_hash"],
                head["committed_task_version"],
            )
        actual = (
            values["generation"],
            values["previous_manifest_hash"],
            values["committed_from_task_version"],
        )
        if actual != expected:
            raise AuthorityStoreError("CHECKPOINT_WATERMARK_SEQUENCE_INVALID")

    def _insert_write_set(
        self,
        connection: sqlite3.Connection,
        head: sqlite3.Row | None,
        values: Mapping[str, object],
    ) -> None:
        self._fault("checkpoint_watermark.before_watermark")
        try:
            connection.execute(
                """
                INSERT INTO agent_current_checkpoint_watermarks (
                    manifest_hash, task_id, generation, previous_manifest_hash,
                    committed_from_task_version, committed_task_version,
                    attempt_id, owner_id, owner_epoch, lease_id,
                    authority_digest, grant_id, grant_digest, deletion_version,
                    native_epoch, transport_epoch, same_run_resume_selected,
                    package_identity, package_version, content_sha256, root_sha256,
                    request_digest, request_bytes, ack_digest, ack_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1,
                          ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(
                    values[field]
                    for field in (
                        "manifest_hash", "task_id", "generation",
                        "previous_manifest_hash", "committed_from_task_version",
                        "committed_task_version", "attempt_id", "owner_id",
                        "owner_epoch", "lease_id", "authority_digest", "grant_id",
                        "grant_digest", "deletion_version", "native_epoch",
                        "transport_epoch",
                    )
                )
                + tuple(values["package_tuple"])
                + tuple(
                    values[field]
                    for field in (
                        "request_digest", "request_bytes", "ack_digest", "ack_bytes",
                    )
                ),
            )
        except sqlite3.IntegrityError:
            raise AuthorityStoreError("CHECKPOINT_WATERMARK_CONFLICT") from None
        self._fault("checkpoint_watermark.after_watermark")
        self._fault("checkpoint_watermark.before_head")
        if head is None:
            connection.execute(
                """
                INSERT INTO agent_current_checkpoint_watermark_heads (
                    task_id, generation, manifest_hash, committed_task_version
                ) VALUES (?, ?, ?, ?)
                """,
                tuple(
                    values[field]
                    for field in (
                        "task_id", "generation", "manifest_hash",
                        "committed_task_version",
                    )
                ),
            )
        else:
            changed = connection.execute(
                """
                UPDATE agent_current_checkpoint_watermark_heads
                SET generation=?, manifest_hash=?, committed_task_version=?,
                    updated_at=strftime('%s','now')
                WHERE task_id=? AND generation=? AND manifest_hash=?
                  AND committed_task_version=?
                """,
                (
                    values["generation"], values["manifest_hash"],
                    values["committed_task_version"], values["task_id"],
                    head["generation"], head["manifest_hash"],
                    head["committed_task_version"],
                ),
            ).rowcount
            if changed != 1:
                raise AuthorityStoreError("CHECKPOINT_WATERMARK_SEQUENCE_INVALID")
        self._fault("checkpoint_watermark.after_head")


__all__ = [
    "CHECKPOINT_WATERMARK_FAULT_POINTS",
    "CheckpointWatermarkStore",
]
