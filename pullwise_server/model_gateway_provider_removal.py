from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Callable, Protocol

from .model_gateway_store import ConnectFactory


class RemovalSecretWriter(Protocol):
    def retire_version(self, secret_ref: str, version: str) -> None: ...


class ProviderDependencyError(ValueError):
    def __init__(self, dependencies: list[str]) -> None:
        super().__init__("provider connection still has active dependencies")
        self.dependencies = dependencies


class ProviderRemovalService:
    def __init__(
        self,
        *,
        connect_factory: ConnectFactory,
        secret_writer: RemovalSecretWriter,
        clock: Callable[[], int],
        audit_id_factory: Callable[[], str],
    ) -> None:
        self._connect_factory = connect_factory
        self._secret_writer = secret_writer
        self._clock = clock
        self._audit_id_factory = audit_id_factory

    def prepare(
        self,
        *,
        provider_connection_id: str,
        actor_user_id: str,
        request_id: str | None,
    ) -> dict[str, object]:
        timestamp = int(self._clock())
        with closing(self._connect_factory()) as connection:
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM provider_connections WHERE provider_connection_id = ?",
                    (provider_connection_id,),
                ).fetchone()
                if not row:
                    raise ValueError("provider connection does not exist")
                if row["status"] != "configured" or row["candidate_secret_version"] or row["previous_secret_version"]:
                    raise ValueError("provider connection is not ready for removal")
                dependencies = self._dependencies(connection, provider_connection_id)
                if dependencies:
                    raise ProviderDependencyError(dependencies)
                connection.execute(
                    "UPDATE provider_connections SET status = 'removal_prepared', updated_at = ? WHERE provider_connection_id = ?",
                    (timestamp, provider_connection_id),
                )
                self._audit(connection, actor_user_id, request_id, provider_connection_id, "prepared", timestamp)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return {
            "providerConnectionId": provider_connection_id,
            "status": "removal_prepared",
            "updatedAt": timestamp,
        }

    def finalize(
        self,
        *,
        provider_connection_id: str,
        actor_user_id: str,
        request_id: str | None,
        upstream_revoked: bool,
    ) -> dict[str, object]:
        if upstream_revoked is not True:
            raise ValueError("explicit upstream revocation confirmation is required")
        connection = self._connect_factory()
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT * FROM provider_connections WHERE provider_connection_id = ?",
                (provider_connection_id,),
            ).fetchone()
        finally:
            connection.close()
        if not row or row["status"] != "removal_prepared":
            raise ValueError("provider connection removal is not prepared")
        try:
            self._secret_writer.retire_version(str(row["secret_ref"]), str(row["secret_version"]))
        except BaseException:
            raise RuntimeError("provider secret retirement failed") from None
        timestamp = int(self._clock())
        with closing(self._connect_factory()) as transaction:
            try:
                transaction.execute("BEGIN IMMEDIATE")
                updated = transaction.execute(
                    """
                    UPDATE provider_connections SET status = 'removed', updated_at = ?
                    WHERE provider_connection_id = ? AND status = 'removal_prepared'
                      AND secret_version = ?
                    """,
                    (timestamp, provider_connection_id, row["secret_version"]),
                )
                if updated.rowcount != 1:
                    raise ValueError("provider connection changed during removal")
                transaction.execute(
                    """
                    UPDATE provider_secret_versions
                    SET state = 'retired', retired_at = ?
                    WHERE provider_connection_id = ? AND secret_version = ?
                    """,
                    (timestamp, provider_connection_id, row["secret_version"]),
                )
                self._audit(transaction, actor_user_id, request_id, provider_connection_id, "finalized", timestamp)
                transaction.commit()
            except BaseException:
                transaction.rollback()
                raise
        return {
            "providerConnectionId": provider_connection_id,
            "status": "removed",
            "updatedAt": timestamp,
        }

    @staticmethod
    def _dependencies(connection: sqlite3.Connection, provider_connection_id: str) -> list[str]:
        dependencies = {
            f"profile_set:{row['profile_set_id']}@{row['revision']}"
            for row in connection.execute(
                """
                SELECT pr.profile_set_id, pr.revision
                FROM profile_set_routes AS pr
                JOIN profile_sets AS ps
                  ON ps.profile_set_id = pr.profile_set_id
                 AND ps.active_revision = pr.revision
                WHERE pr.provider_connection_id = ? AND pr.enabled = 1
                  AND ps.status = 'published'
                """,
                (provider_connection_id,),
            ).fetchall()
        }
        dependencies.update(
            f"worker_pool:{row['worker_pool_id']}"
            for row in connection.execute(
                """
                SELECT wp.worker_pool_id
                FROM worker_pools AS wp
                JOIN profile_set_routes AS pr
                  ON pr.profile_set_id = wp.profile_set_id
                 AND pr.revision = wp.desired_revision
                WHERE pr.provider_connection_id = ? AND pr.enabled = 1
                  AND wp.status = 'active'
                """,
                (provider_connection_id,),
            ).fetchall()
        )
        dependencies.update(
            f"worker_pool:{row['worker_pool_id']}"
            for row in connection.execute(
                """
                SELECT DISTINCT wp.worker_pool_id
                FROM worker_pools AS wp
                JOIN worker_pool_memberships AS wm
                  ON wm.worker_pool_id = wp.worker_pool_id
                JOIN profile_set_routes AS pr
                  ON pr.profile_set_id = wp.profile_set_id
                 AND pr.revision = wm.desired_revision
                WHERE pr.provider_connection_id = ? AND pr.enabled = 1
                  AND wp.status = 'active'
                """,
                (provider_connection_id,),
            ).fetchall()
        )
        return sorted(dependencies)

    def _audit(
        self,
        connection: sqlite3.Connection,
        actor_user_id: str,
        request_id: str | None,
        provider_connection_id: str,
        phase: str,
        timestamp: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO model_gateway_audit_events (
                id, actor_user_id, action, subject_type, subject_id,
                changed_fields_json, request_id, created_at, success, error_code
            ) VALUES (?, ?, ?, 'provider_connection', ?, ?, ?, ?, 1, NULL)
            """,
            (
                self._audit_id_factory(),
                actor_user_id,
                f"provider_connection.removal_{phase}",
                provider_connection_id,
                json.dumps({"phase": phase}, separators=(",", ":")),
                request_id,
                timestamp,
            ),
        )
