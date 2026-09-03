from __future__ import annotations

import json
import sqlite3

from .model_gateway_store import ConnectFactory


def worker_assignment_routes_available(
    connect_factory: ConnectFactory,
    worker_id: str,
) -> bool:
    """Return true only when every enabled desired route can resolve safely."""
    connection = connect_factory()
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT pr.api, pr.upstream_model, pc.provider_connection_id,
                   pc.status, pc.adapter, pc.secret_ref, pc.secret_version,
                   pc.validated_models_json
            FROM worker_pool_memberships AS m
            JOIN worker_pools AS p ON p.worker_pool_id = m.worker_pool_id
            JOIN workers AS w ON w.worker_id = m.worker_id
            JOIN profile_set_revisions AS r
              ON r.profile_set_id = p.profile_set_id
             AND r.revision = m.desired_revision
            JOIN profile_set_routes AS pr
              ON pr.profile_set_id = r.profile_set_id
             AND pr.revision = r.revision
             AND pr.enabled = 1
            LEFT JOIN provider_connections AS pc
              ON pc.provider_connection_id = pr.provider_connection_id
            WHERE m.worker_id = ?
              AND p.status = 'active'
              AND r.status = 'published'
              AND w.enabled = 1
              AND w.deleted_at IS NULL
            """,
            (worker_id,),
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        return False
    for row in rows:
        try:
            validated_models = json.loads(row["validated_models_json"])
        except (TypeError, json.JSONDecodeError):
            return False
        if (
            row["provider_connection_id"] is None
            or row["status"] not in {"configured", "rotation_staged", "rotation_promoted"}
            or row["adapter"] != row["api"]
            or not row["secret_ref"]
            or not row["secret_version"]
            or not isinstance(validated_models, list)
            or row["upstream_model"] not in validated_models
        ):
            return False
    return True
