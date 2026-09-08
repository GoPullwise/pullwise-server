from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Callable, Protocol

from .model_gateway_secret_cleanup import SecretCleanupCoordinator
from .model_gateway_store import ConnectFactory


class RotationSecretWriter(Protocol):
    def put_candidate(self, secret_ref: str, secret: bytes): ...
    def validate_candidate(self, secret_ref: str, version: str, **metadata: object) -> None: ...
    def canary_candidate(self, secret_ref: str, version: str, **metadata: object) -> None: ...
    def retire_version(self, secret_ref: str, version: str) -> None: ...


class ProviderRotationService:
    def __init__(
        self,
        *,
        connect_factory: ConnectFactory,
        secret_writer: RotationSecretWriter,
        clock: Callable[[], int],
        audit_id_factory: Callable[[], str],
    ) -> None:
        self._connect_factory = connect_factory
        self._secret_writer = secret_writer
        self._secret_cleanup = SecretCleanupCoordinator(
            connect_factory=connect_factory, secret_writer=secret_writer, clock=clock
        )
        self._clock = clock
        self._audit_id_factory = audit_id_factory

    def _connection(self, provider_connection_id: str) -> dict[str, object]:
        connection = self._connect_factory()
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT * FROM provider_connections WHERE provider_connection_id = ?",
                (provider_connection_id,),
            ).fetchone()
            if not row:
                raise ValueError("provider connection does not exist")
            return dict(row)
        finally:
            connection.close()

    def _required_models(self, connection: sqlite3.Connection, provider_connection_id: str) -> set[str]:
        rows = connection.execute(
                """
                SELECT DISTINCT pr.upstream_model
                FROM profile_set_routes AS pr
                WHERE pr.provider_connection_id = ? AND pr.enabled = 1
                  AND (
                    EXISTS (
                      SELECT 1 FROM profile_sets AS ps
                      WHERE ps.profile_set_id = pr.profile_set_id
                        AND ps.active_revision = pr.revision
                        AND ps.status = 'published'
                    )
                    OR EXISTS (
                      SELECT 1 FROM worker_pools AS wp
                      LEFT JOIN worker_pool_memberships AS wm
                        ON wm.worker_pool_id = wp.worker_pool_id
                      WHERE wp.profile_set_id = pr.profile_set_id
                        AND wp.status = 'active'
                        AND (wp.desired_revision = pr.revision OR wm.desired_revision = pr.revision)
                    )
                  )
                """,
                (provider_connection_id,),
        ).fetchall()
        return {str(row[0]) for row in rows}

    def stage(
        self,
        *,
        provider_connection_id: str,
        actor_user_id: str,
        request_id: str | None,
        secret: str,
    ) -> dict[str, object]:
        if not isinstance(secret, str) or not secret or len(secret.encode("utf-8")) > 16_384:
            raise ValueError("provider secret is invalid")
        current = self._connection(provider_connection_id)
        if current["status"] != "configured" or current.get("candidate_secret_version") or current.get("previous_secret_version"):
            raise ValueError("provider connection already has a rotation in progress")
        secret_ref = str(current["secret_ref"])
        metadata = {
            "provider": current["provider"],
            "adapter": current["adapter"],
            "endpoint_origin": current["endpoint_origin"],
        }
        stored = None
        try:
            stored = self._secret_writer.put_candidate(secret_ref, secret.encode("utf-8"))
            discovered_models = self._secret_writer.validate_candidate(secret_ref, stored.version, **metadata)
            self._secret_writer.canary_candidate(secret_ref, stored.version, **metadata)
        except BaseException:
            if stored is not None:
                self._secret_cleanup.retire_or_queue(
                    secret_ref, stored.version, reason="provider_rotation_validation_failed"
                )
            raise RuntimeError("provider rotation validation failed") from None
        if (
            not isinstance(discovered_models, list)
            or not discovered_models
            or len(discovered_models) > 512
            or any(not isinstance(model, str) or not model for model in discovered_models)
            or len(discovered_models) != len(set(discovered_models))
        ):
            self._secret_cleanup.retire_or_queue(
                secret_ref, stored.version, reason="provider_rotation_models_invalid"
            )
            raise RuntimeError("provider rotation validation returned invalid model metadata")
        validated_models_json = json.dumps(sorted(discovered_models), separators=(",", ":"))
        timestamp = int(self._clock())
        with closing(self._connect_factory()) as connection:
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("BEGIN IMMEDIATE")
                fresh = connection.execute(
                    "SELECT * FROM provider_connections WHERE provider_connection_id = ?",
                    (provider_connection_id,),
                ).fetchone()
                if (
                    not fresh
                    or fresh["status"] != "configured"
                    or fresh["secret_version"] != current["secret_version"]
                    or fresh["candidate_secret_version"] is not None
                ):
                    raise ValueError("provider connection changed during rotation")
                if not self._required_models(connection, provider_connection_id).issubset(set(discovered_models)):
                    raise ValueError("provider rotation candidate does not cover active route models")
                connection.execute(
                    """
                    INSERT INTO provider_secret_versions (
                        provider_connection_id, secret_version, secret_fingerprint, validated_models_json,
                        state, created_at, validated_at, promoted_at, retired_at
                    ) VALUES (?, ?, ?, ?, 'canary_passed', ?, ?, NULL, NULL)
                    """,
                    (
                        provider_connection_id,
                        stored.version,
                        stored.fingerprint,
                        validated_models_json,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE provider_connections
                    SET status = 'rotation_staged', candidate_secret_version = ?,
                        candidate_secret_fingerprint = ?, last_validated_at = ?, updated_at = ?
                    WHERE provider_connection_id = ?
                    """,
                    (stored.version, stored.fingerprint, timestamp, timestamp, provider_connection_id),
                )
                self._audit(connection, actor_user_id, request_id, provider_connection_id, "staged", timestamp)
                connection.commit()
            except BaseException:
                connection.rollback()
                self._secret_cleanup.retire_or_queue(
                    secret_ref, stored.version, reason="provider_rotation_persistence_failed"
                )
                raise
        return self._payload(self._connection(provider_connection_id))

    def promote(
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
                if not row or row["status"] != "rotation_staged" or not row["candidate_secret_version"]:
                    raise ValueError("provider rotation is not ready for promotion")
                candidate = connection.execute(
                    """
                    SELECT validated_models_json FROM provider_secret_versions
                    WHERE provider_connection_id = ? AND secret_version = ? AND state = 'canary_passed'
                    """,
                    (provider_connection_id, row["candidate_secret_version"]),
                ).fetchone()
                if not candidate:
                    raise ValueError("provider rotation candidate metadata is unavailable")
                candidate_models = set(json.loads(candidate["validated_models_json"]))
                if not self._required_models(connection, provider_connection_id).issubset(candidate_models):
                    raise ValueError("provider rotation candidate does not cover active route models")
                connection.execute(
                    """
                    UPDATE provider_connections
                    SET status = 'rotation_promoted',
                        previous_secret_version = secret_version,
                        previous_secret_fingerprint = secret_fingerprint,
                        secret_version = candidate_secret_version,
                        secret_fingerprint = candidate_secret_fingerprint,
                        validated_models_json = ?,
                        candidate_secret_version = NULL,
                        candidate_secret_fingerprint = NULL,
                        last_rotated_at = ?, updated_at = ?
                    WHERE provider_connection_id = ?
                    """,
                    (candidate["validated_models_json"], timestamp, timestamp, provider_connection_id),
                )
                connection.execute(
                    "UPDATE provider_secret_versions SET state = 'retiring' WHERE provider_connection_id = ? AND secret_version = ?",
                    (provider_connection_id, row["secret_version"]),
                )
                connection.execute(
                    "UPDATE provider_secret_versions SET state = 'active', promoted_at = ? WHERE provider_connection_id = ? AND secret_version = ? AND state = 'canary_passed'",
                    (timestamp, provider_connection_id, row["candidate_secret_version"]),
                )
                self._audit(connection, actor_user_id, request_id, provider_connection_id, "promoted", timestamp)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self._payload(self._connection(provider_connection_id))

    def retire_previous(
        self,
        *,
        provider_connection_id: str,
        actor_user_id: str,
        request_id: str | None,
        upstream_revoked: bool,
    ) -> dict[str, object]:
        if upstream_revoked is not True:
            raise ValueError("explicit upstream revocation confirmation is required")
        current = self._connection(provider_connection_id)
        previous = current.get("previous_secret_version")
        if current["status"] != "rotation_promoted" or not previous:
            raise ValueError("provider rotation has no previous version to retire")
        try:
            self._secret_writer.retire_version(str(current["secret_ref"]), str(previous))
        except BaseException:
            raise RuntimeError("provider secret retirement failed") from None
        timestamp = int(self._clock())
        with closing(self._connect_factory()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                updated = connection.execute(
                    """
                    UPDATE provider_connections
                    SET status = 'configured', previous_secret_version = NULL,
                        previous_secret_fingerprint = NULL, updated_at = ?
                    WHERE provider_connection_id = ? AND status = 'rotation_promoted'
                      AND previous_secret_version = ?
                    """,
                    (timestamp, provider_connection_id, previous),
                )
                if updated.rowcount != 1:
                    raise ValueError("provider connection changed during retirement")
                connection.execute(
                    "UPDATE provider_secret_versions SET state = 'retired', retired_at = ? WHERE provider_connection_id = ? AND secret_version = ? AND state = 'retiring'",
                    (timestamp, provider_connection_id, previous),
                )
                self._audit(connection, actor_user_id, request_id, provider_connection_id, "retired", timestamp)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self._payload(self._connection(provider_connection_id))

    def _audit(self, connection, actor: str, request_id: str | None, subject: str, phase: str, timestamp: int) -> None:
        connection.execute(
            """
            INSERT INTO model_gateway_audit_events (
                id, actor_user_id, action, subject_type, subject_id,
                changed_fields_json, request_id, created_at, success, error_code
            ) VALUES (?, ?, ?, 'provider_connection', ?, ?, ?, ?, 1, NULL)
            """,
            (
                self._audit_id_factory(), actor, f"provider_connection.rotation_{phase}", subject,
                json.dumps({"phase": phase}, separators=(",", ":")), request_id, timestamp,
            ),
        )

    @staticmethod
    def _payload(row: dict[str, object]) -> dict[str, object]:
        return {
            "providerConnectionId": row["provider_connection_id"],
            "status": row["status"],
            "secretVersion": row["secret_version"],
            "candidateSecretVersion": row.get("candidate_secret_version"),
            "previousSecretVersion": row.get("previous_secret_version"),
            "lastValidatedAt": row.get("last_validated_at"),
            "lastRotatedAt": row.get("last_rotated_at"),
            "updatedAt": row["updated_at"],
        }
