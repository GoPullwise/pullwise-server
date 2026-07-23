"""Atomic claim/grant issuance and full-fence abandonment transactions."""

from __future__ import annotations

import json
import secrets
import sqlite3
from typing import Callable, Mapping

from .agent_first_authority_store import AgentFirstAuthorityStore, AuthorityStoreError


CLAIM_FAULT_POINTS = tuple(
    f"claim.{side}_{stage}"
    for stage in (
        "attempt",
        "owner",
        "grant",
        "grant_authority",
        "claim",
        "task_head",
        "event",
    )
    for side in ("before", "after")
)
ABANDON_FAULT_POINTS = tuple(
    f"abandon.{side}_{stage}"
    for stage in (
        "abandonment",
        "fences",
        "attempt",
        "owner",
        "grant_authority",
        "task_head",
        "event",
    )
    for side in ("before", "after")
)


class ClaimAuthorityStore(AgentFirstAuthorityStore):
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
                current = connection.execute(
                    """
                    SELECT h.lifecycle, h.desired_state, a.state AS attempt_state,
                           o.state AS owner_state, ga.state AS grant_state
                    FROM agent_current_task_heads h
                    JOIN agent_current_claims c
                      ON c.task_id=h.task_id
                     AND c.attempt_id=h.current_attempt_id
                     AND c.session_id=h.current_session_id
                     AND c.grant_id=h.current_grant_id
                     AND c.authority_digest=h.current_authority_digest
                    JOIN agent_current_attempts a ON a.attempt_id=c.attempt_id
                    JOIN agent_current_owner_incarnations o ON o.session_id=c.session_id
                    JOIN agent_current_grant_authority ga ON ga.grant_id=c.grant_id
                    WHERE h.task_id=? AND c.worker_id=? AND c.authority_bytes=?
                      AND h.current_authority_schema_id='server-authority-envelope/v1'
                    """,
                    (values["task_id"], values["worker_id"], replay),
                ).fetchone()
                if current is None or tuple(current) != (
                    "ACTIVE", "RUN", "CLAIMED", "STARTING", "ACTIVE"
                ):
                    raise AuthorityStoreError("AUTHORITY_FENCED")
                return replay
            worker = connection.execute(
                """
                SELECT r.* FROM agent_current_worker_registration_heads h
                JOIN agent_current_worker_registrations r
                  ON r.registration_id=h.registration_id
                WHERE h.worker_id=?
                """,
                (values["worker_id"],),
            ).fetchone()
            if worker is None:
                raise AuthorityStoreError("WORKER_NOT_REGISTERED")
            if self._row_package(worker) != values["package_tuple"]:
                raise AuthorityStoreError("WORKER_PACKAGE_MISMATCH")
            try:
                supported = set(json.loads(self._blob(worker["supported_schema_ids"])))
            except (TypeError, ValueError):
                raise AuthorityStoreError("WORKER_REGISTRATION_INVALID") from None
            if (
                worker["tool_catalog_digest"] != values["expected_tool_catalog_digest"]
                or not set(values["required_schema_ids"]).issubset(supported)
            ):
                raise AuthorityStoreError("WORKER_REGISTRATION_INVALID")
            head = connection.execute(
                """
                SELECT h.*, r.package_identity, r.package_version,
                       r.content_sha256, r.root_sha256, r.policy_digest,
                       r.policy_bytes, r.accepted_at, r.absolute_deadline_at,
                       r.terminalization_reserve_ms
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
                and head["current_authority_schema_id"] is None
                and head["current_authority_digest"] is None
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
            "VALUES (?, ?, ?, ?, ?, 'CLAIMED')",
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
            "INSERT INTO agent_current_grant_authority (grant_id, state) VALUES (?, 'ACTIVE')",
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
                request["task_id"],
                write["previous_task_version"], write["deletion_version"],
            ),
        ).rowcount
        if updated != 1:
            raise AuthorityStoreError("TASK_NOT_CLAIMABLE")
        self._fault("claim.after_task_head")
        self._fault("claim.before_event")
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

    def abandon_claim(
        self,
        values: Mapping[str, object],
        build: Callable[[sqlite3.Row, sqlite3.Row], Mapping[str, object]],
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
            if (
                head["lifecycle"] not in ("ACTIVE", "FINALIZING")
                or head["current_authority_schema_id"]
                != "server-authority-envelope/v1"
            ):
                raise AuthorityStoreError("AUTHORITY_FENCED")
            if tuple(head[key] for key in keys) != expected:
                raise AuthorityStoreError("AUTHORITY_MISMATCH")
            states = connection.execute(
                """
                SELECT a.state AS attempt_state, o.state AS owner_state,
                       ga.state AS grant_state, g.grant_bytes, g.grant_digest,
                       c.authority_digest
                FROM agent_current_claims c
                JOIN agent_current_attempts a ON a.attempt_id=c.attempt_id
                JOIN agent_current_owner_incarnations o ON o.session_id=c.session_id
                JOIN agent_current_grants g ON g.grant_id=c.grant_id
                JOIN agent_current_grant_authority ga ON ga.grant_id=c.grant_id
                WHERE c.task_id=? AND c.attempt_id=? AND c.session_id=? AND c.grant_id=?
                """,
                (
                    values["task_id"], values["attempt_id"],
                    values["session_id"], values["grant_id"],
                ),
            ).fetchone()
            if (
                states is None
                or tuple(states)[:3] != ("CLAIMED", "STARTING", "ACTIVE")
                or states["authority_digest"] != head["current_authority_digest"]
            ):
                raise AuthorityStoreError("AUTHORITY_FENCED")
            write = {**values, **build(head, states)}
            self._insert_abandon_write_set(connection, write)
            return write["response_bytes"]  # type: ignore[return-value]

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
                grant_digest, superseded_authority_digest, abandonment_digest,
                abandonment_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(values[key] for key in (
                "abandonment_id", "task_id", "attempt_id", "session_id", "grant_id",
                "owner_id", "lease_id", "expected_task_version", "task_version",
                "deletion_version", "owner_epoch", "native_epoch", "transport_epoch",
                "reason", "grant_digest", "superseded_authority_digest",
                "abandonment_digest", "abandonment_bytes",
            )),
        )
        self._fault("abandon.after_abandonment")
        self._fault("abandon.before_fences")
        for target_type, target_id in (
            ("transport", values["lease_id"]),
            ("attempt", values["attempt_id"]),
            ("owner", values["session_id"]),
            ("grant", values["grant_id"]),
        ):
            connection.execute(
                "INSERT INTO agent_current_fences "
                "(fence_id, abandonment_id, target_type, target_id) VALUES (?, ?, ?, ?)",
                (
                    f"fence_{secrets.token_hex(16)}", values["abandonment_id"],
                    target_type, target_id,
                ),
            )
        self._fault("abandon.after_fences")
        self._fault("abandon.before_attempt")
        attempt = connection.execute(
            "UPDATE agent_current_attempts SET state='FENCED', "
            "fenced_at=strftime('%s','now'), fence_reason=? "
            "WHERE attempt_id=? AND state='CLAIMED'",
            (values["reason"], values["attempt_id"]),
        ).rowcount
        self._fault("abandon.after_attempt")
        self._fault("abandon.before_owner")
        owner = connection.execute(
            "UPDATE agent_current_owner_incarnations SET state='FENCED', "
            "fenced_at=strftime('%s','now'), fence_reason=? "
            "WHERE session_id=? AND state='STARTING'",
            (values["reason"], values["session_id"]),
        ).rowcount
        self._fault("abandon.after_owner")
        self._fault("abandon.before_grant_authority")
        grant = connection.execute(
            "UPDATE agent_current_grant_authority SET state='FENCED', "
            "authority_version=authority_version+1, fenced_at=strftime('%s','now'), "
            "fence_reason=? WHERE grant_id=? AND state='ACTIVE'",
            (values["reason"], values["grant_id"]),
        ).rowcount
        self._fault("abandon.after_grant_authority")
        self._fault("abandon.before_task_head")
        task = connection.execute(
            """
            UPDATE agent_current_task_heads
            SET task_version=?,
                current_authority_schema_id='agent-claim-abandon-response/v1',
                current_authority_digest=?, updated_at=strftime('%s','now')
            WHERE task_id=? AND task_version=? AND deletion_version=?
              AND owner_id=? AND owner_epoch=? AND native_epoch=? AND transport_epoch=?
              AND current_attempt_id=? AND current_session_id=? AND current_grant_id=?
              AND current_lease_id=? AND current_authority_digest=?
              AND lifecycle IN ('ACTIVE','FINALIZING')
            """,
            (
                values["task_version"], values["abandonment_digest"], values["task_id"],
                values["expected_task_version"], values["deletion_version"],
                values["owner_id"], values["owner_epoch"], values["native_epoch"],
                values["transport_epoch"], values["attempt_id"], values["session_id"],
                values["grant_id"], values["lease_id"],
                values["superseded_authority_digest"],
            ),
        ).rowcount
        if (attempt, owner, grant, task) != (1, 1, 1, 1):
            raise AuthorityStoreError("AUTHORITY_FENCED")
        self._fault("abandon.after_task_head")
        self._fault("abandon.before_event")
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


__all__ = ["ABANDON_FAULT_POINTS", "CLAIM_FAULT_POINTS", "ClaimAuthorityStore"]
