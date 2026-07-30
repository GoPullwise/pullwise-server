"""Atomic insert of the claim, Owner, authority, and runtime-bootstrap write set."""

from __future__ import annotations

import sqlite3
from typing import Mapping

from .agent_first_authority_store import AuthorityStoreError


class ClaimWriteSetStore:
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
        self._fault("claim.before_owner")
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
        self._fault("claim.before_grant")
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
        self._fault("claim.before_grant_authority")
        connection.execute(
            "INSERT INTO agent_current_grant_authority (grant_id, state) "
            "VALUES (?, 'ACTIVE')",
            (write["grant_id"],),
        )
        self._fault("claim.after_grant_authority")
        self._fault("claim.before_claim")
        connection.execute(
            """
            INSERT INTO agent_current_claims (
                claim_id, task_id, attempt_id, session_id, grant_id, worker_id,
                owner_id, lease_id, task_version, deletion_version, owner_epoch,
                native_epoch, transport_epoch, claim_digest, claim_bytes,
                authority_digest, authority_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                write["claim_id"], request["task_id"], write["attempt_id"],
                write["session_id"], write["grant_id"], request["worker_id"],
                write["owner_id"], request["lease_id"], write["task_version"],
                write["deletion_version"], write["owner_epoch"],
                write["native_epoch"], request["transport_epoch"],
                write["claim_digest"], write["claim_bytes"],
                write["authority_digest"], write["authority_bytes"],
            ),
        )
        self._fault("claim.after_claim")
        self._fault("claim.before_runtime_bootstrap")
        connection.execute(
            """
            INSERT INTO agent_current_runtime_bootstraps (
                claim_id, task_id, attempt_id, session_id, bootstrap_digest,
                bootstrap_bytes, task_record_bytes, attempt_record_bytes,
                owner_record_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(
                write[field]
                for field in (
                    "claim_id", "task_id", "attempt_id", "session_id",
                    "bootstrap_digest", "bootstrap_bytes", "task_record_bytes",
                    "attempt_record_bytes", "owner_record_bytes",
                )
            ),
        )
        self._fault("claim.after_runtime_bootstrap")
        self._fault("claim.before_task_head")
        updated = connection.execute(
            """
            UPDATE agent_current_task_heads
            SET lifecycle='ACTIVE', task_version=?, native_epoch=?, owner_epoch=?,
                transport_epoch=?, current_attempt_id=?, current_session_id=?,
                current_grant_id=?,
                current_authority_schema_id='server-authority-envelope/v1',
                current_authority_digest=?, current_lease_id=?,
                updated_at=strftime('%s','now')
            WHERE task_id=? AND lifecycle='QUEUED' AND desired_state='RUN'
              AND task_version=? AND deletion_version=? AND current_attempt_id IS NULL
            """,
            (
                write["task_version"], write["native_epoch"], write["owner_epoch"],
                request["transport_epoch"], write["attempt_id"], write["session_id"],
                write["grant_id"], write["authority_digest"], request["lease_id"],
                request["task_id"], write["previous_task_version"],
                write["deletion_version"],
            ),
        ).rowcount
        if updated != 1:
            raise AuthorityStoreError("TASK_NOT_CLAIMABLE")
        self._fault("claim.after_task_head")
        self._fault("claim.before_event")
        self._insert_event(
            connection,
            task_id=request["task_id"],
            event_type="attempt.claimed",
            idempotency_key=request["idempotency_key"],
            request_digest=request["request_digest"],
            response_bytes=write["response_bytes"],
            task_version=write["task_version"],
        )
        self._fault("claim.after_event")


__all__ = ["ClaimWriteSetStore"]
