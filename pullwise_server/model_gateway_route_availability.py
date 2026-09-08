from __future__ import annotations

import sqlite3

from .model_gateway_store import ConnectFactory


PROVIDER_ROUTE_AVAILABLE_SQL = """
    pc.provider_connection_id IS NOT NULL
    AND pc.status IN ('configured', 'rotation_staged', 'rotation_promoted')
    AND pc.adapter = pr.api
    AND COALESCE(pc.secret_ref, '') != ''
    AND COALESCE(pc.secret_version, '') != ''
    AND json_type(CASE WHEN json_valid(pc.validated_models_json)
                      THEN pc.validated_models_json ELSE 'null' END) = 'array'
    AND NOT EXISTS (
        SELECT 1 FROM json_each(CASE WHEN json_valid(pc.validated_models_json)
                                   THEN pc.validated_models_json ELSE '[]' END) AS invalid_model
        WHERE invalid_model.type != 'text' OR trim(invalid_model.value) = ''
    )
    AND EXISTS (
        SELECT 1 FROM json_each(CASE WHEN json_valid(pc.validated_models_json)
                                   THEN pc.validated_models_json ELSE '[]' END) AS model
        WHERE model.type = 'text' AND model.value = pr.upstream_model
    )
"""


def worker_assignment_routes_available(
    connect_factory: ConnectFactory,
    worker_id: str,
) -> bool:
    """Return true only when every enabled desired route can resolve safely."""
    connection = connect_factory()
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            f"""
            SELECT COUNT(*) AS route_count,
                   MIN(CASE WHEN {PROVIDER_ROUTE_AVAILABLE_SQL} THEN 1 ELSE 0 END) AS available
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
        ).fetchone()
    finally:
        connection.close()
    return bool(row and row["route_count"] and row["available"])
