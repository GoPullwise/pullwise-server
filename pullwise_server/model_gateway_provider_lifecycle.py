from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import closing

from .model_gateway_store import ConnectFactory


def emergency_revoke_provider_connection(
    connect_factory: ConnectFactory,
    *,
    provider_connection_id: str,
    actor_user_id: str,
    request_id: str | None,
    timestamp: int,
) -> dict[str, object]:
    with closing(connect_factory()) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM provider_connections WHERE provider_connection_id = ?",
                (provider_connection_id,),
            ).fetchone()
            if not row:
                raise ValueError("provider connection does not exist")
            if row["status"] != "emergency_revoked":
                connection.execute(
                    """
                    UPDATE provider_connections
                    SET status = 'emergency_revoked', updated_at = ?
                    WHERE provider_connection_id = ?
                    """,
                    (timestamp, provider_connection_id),
                )
                connection.execute(
                    """
                    INSERT INTO model_gateway_audit_events (
                        id, actor_user_id, action, subject_type, subject_id,
                        changed_fields_json, request_id, created_at, success, error_code
                    ) VALUES (?, ?, 'provider_connection.emergency_revoked',
                              'provider_connection', ?, ?, ?, ?, 1, NULL)
                    """,
                    (
                        f"audit_{secrets.token_urlsafe(18)}",
                        actor_user_id,
                        provider_connection_id,
                        json.dumps({"status": "emergency_revoked"}, separators=(",", ":")),
                        request_id,
                        timestamp,
                    ),
                )
            connection.execute(
                """
                UPDATE gateway_token_grants AS g
                SET revoked_at = ?
                WHERE g.revoked_at IS NULL AND EXISTS (
                    SELECT 1 FROM profile_set_routes AS pr
                    WHERE pr.profile_set_id = g.profile_set_id
                      AND pr.revision = g.profile_revision
                      AND pr.provider_connection_id = ?
                      AND pr.enabled = 1
                )
                """,
                (timestamp, provider_connection_id),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return {
        "providerConnectionId": provider_connection_id,
        "status": "emergency_revoked",
        "updatedAt": timestamp,
    }
