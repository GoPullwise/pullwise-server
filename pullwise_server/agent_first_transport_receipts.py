"""Immutable Server transport receipt storage and one-shot envelope binding."""

from __future__ import annotations

from typing import Callable, Mapping

from .agent_first_authority_store import AgentFirstAuthorityStore, AuthorityStoreError


RECEIPT_FAULT_POINTS = (
    "receipt.before_receipt",
    "receipt.after_receipt",
    "receipt.before_binding_head",
    "receipt.after_binding_head",
)
BINDING_FAULT_POINTS = ("binding.before_cas", "binding.after_cas")


class TransportReceiptStore(AgentFirstAuthorityStore):
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
            historical = connection.execute(
                """
                SELECT a.state AS attempt_state, o.state AS owner_state,
                       ga.state AS grant_state
                FROM agent_current_claims c
                JOIN agent_current_attempts a ON a.attempt_id=c.attempt_id
                JOIN agent_current_owner_incarnations o ON o.session_id=c.session_id
                JOIN agent_current_grants g ON g.grant_id=c.grant_id
                JOIN agent_current_grant_authority ga ON ga.grant_id=c.grant_id
                WHERE c.task_id=? AND c.attempt_id=? AND c.session_id=?
                  AND c.owner_id=? AND c.lease_id=? AND c.authority_digest=?
                  AND c.task_version=? AND c.deletion_version=?
                  AND c.owner_epoch=? AND c.native_epoch=? AND c.transport_epoch=?
                  AND g.grant_digest=?
                """,
                tuple(values[key] for key in (
                    "task_id", "attempt_id", "session_id", "owner_id", "lease_id",
                    "authority_digest", "task_version", "deletion_version",
                    "owner_epoch", "native_epoch", "transport_epoch", "grant_digest",
                )),
            ).fetchone()
            if historical is not None and any(
                historical[name] == "FENCED"
                for name in ("attempt_state", "owner_state", "grant_state")
            ):
                raise AuthorityStoreError("AUTHORITY_FENCED")
            authority = connection.execute(
                """
                SELECT h.current_attempt_id, h.current_session_id, h.owner_id,
                       h.current_lease_id, h.current_authority_digest, h.task_version,
                       h.deletion_version, h.owner_epoch, h.native_epoch,
                       h.transport_epoch, a.state AS attempt_state,
                       o.state AS owner_state, g.grant_digest,
                       ga.state AS grant_state, r.package_identity,
                       r.package_version, r.content_sha256, r.root_sha256
                FROM agent_current_task_heads h
                JOIN agent_current_task_requests r USING (task_id)
                JOIN agent_current_attempts a ON a.attempt_id=h.current_attempt_id
                JOIN agent_current_owner_incarnations o ON o.session_id=h.current_session_id
                JOIN agent_current_grants g ON g.grant_id=h.current_grant_id
                JOIN agent_current_grant_authority ga ON ga.grant_id=g.grant_id
                WHERE h.task_id=?
                  AND h.current_authority_schema_id='server-authority-envelope/v1'
                """,
                (values["task_id"],),
            ).fetchone()
            if authority is None:
                raise AuthorityStoreError("AUTHORITY_NOT_FOUND")
            if any(
                authority[name] == "FENCED"
                for name in ("attempt_state", "owner_state", "grant_state")
            ):
                raise AuthorityStoreError("AUTHORITY_FENCED")
            fields = (
                "attempt_id",
                "session_id",
                "owner_id",
                "lease_id",
                "authority_digest",
                "task_version",
                "deletion_version",
                "owner_epoch",
                "native_epoch",
                "transport_epoch",
                "grant_digest",
            )
            columns = (
                "current_attempt_id",
                "current_session_id",
                "owner_id",
                "current_lease_id",
                "current_authority_digest",
                "task_version",
                "deletion_version",
                "owner_epoch",
                "native_epoch",
                "transport_epoch",
                "grant_digest",
            )
            exact = (
                tuple(authority[column] for column in columns)
                == tuple(values[field] for field in fields)
                and self._row_package(authority) == values["package_tuple"]
            )
            if not exact:
                raise AuthorityStoreError("AUTHORITY_MISMATCH")
            self._fault("receipt.before_receipt")
            connection.execute(
                """
                INSERT INTO agent_current_transport_receipts (
                    receipt_digest, receipt_id, receipt_kind, task_id, attempt_id,
                    session_id, owner_id, lease_id, authority_digest, grant_digest,
                    task_version, deletion_version,
                    owner_epoch, native_epoch, transport_epoch, package_identity,
                    package_version, content_sha256, root_sha256,
                    receipt_bytes_sha256, receipt_size_bytes, receipt_bytes,
                    response_bytes
                ) VALUES (?, ?, 'server_transport', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["receipt_digest"], values["receipt_id"], values["task_id"],
                    values["attempt_id"], values["session_id"], values["owner_id"],
                    values["lease_id"], values["authority_digest"],
                    values["grant_digest"],
                    values["task_version"], values["deletion_version"],
                    values["owner_epoch"], values["native_epoch"],
                    values["transport_epoch"], *values["package_tuple"],
                    values["receipt_bytes_sha256"], values["receipt_size_bytes"],
                    values["receipt_bytes"], values["response_bytes"],
                ),
            )
            self._fault("receipt.after_receipt")
            self._fault("receipt.before_binding_head")
            connection.execute(
                "INSERT INTO agent_current_transport_receipt_bindings (receipt_digest) VALUES (?)",
                (values["receipt_digest"],),
            )
            self._fault("receipt.after_binding_head")
            return values["response_bytes"]  # type: ignore[return-value]

    def bind_receipt(
        self,
        receipt_digest: str,
        envelope_digest: str,
        build: Callable[[str], bytes],
    ) -> bytes:
        with self._immediate() as connection:
            row = connection.execute(
                """
                SELECT r.receipt_id, b.transport_envelope_digest, b.response_bytes
                FROM agent_current_transport_receipts r
                JOIN agent_current_transport_receipt_bindings b USING (receipt_digest)
                WHERE r.receipt_digest = ? AND r.receipt_kind = 'server_transport'
                """,
                (receipt_digest,),
            ).fetchone()
            if row is None:
                raise AuthorityStoreError("TRANSPORT_RECEIPT_NOT_FOUND")
            current = row["transport_envelope_digest"]
            if current is not None:
                if current != envelope_digest:
                    raise AuthorityStoreError("TRANSPORT_RECEIPT_ALREADY_BOUND")
                return self._blob(row["response_bytes"])
            self._fault("binding.before_cas")
            response_bytes = build(row["receipt_id"])
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
                raise AuthorityStoreError("TRANSPORT_RECEIPT_ALREADY_BOUND")
            self._fault("binding.after_cas")
            return response_bytes


__all__ = [
    "BINDING_FAULT_POINTS",
    "RECEIPT_FAULT_POINTS",
    "TransportReceiptStore",
]
