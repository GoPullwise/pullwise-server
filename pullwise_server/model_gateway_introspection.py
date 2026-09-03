from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3

from .model_gateway_route_availability import worker_assignment_routes_available
from .model_gateway_store import ConnectFactory


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class IntrospectionUnauthorized(ValueError):
    pass


class GatewayRouteNotFound(ValueError):
    pass


def _authorize(presented_token: str | None) -> None:
    expected_token = os.environ.get("PULLWISE_MODEL_GATEWAY_INTROSPECTION_TOKEN", "").strip()
    if not expected_token:
        raise RuntimeError("Model Gateway introspection is not configured")
    if not isinstance(presented_token, str) or not hmac.compare_digest(presented_token, expected_token):
        raise IntrospectionUnauthorized("Model Gateway introspection authorization failed")


def introspect_gateway_grant(
    connect_factory: ConnectFactory,
    *,
    presented_token: str | None,
    payload: object,
    timestamp: int,
) -> dict[str, object]:
    _authorize(presented_token)
    if not isinstance(payload, dict) or set(payload) != {"jti", "token_hash"}:
        raise ValueError("introspection payload must be a closed object")
    jti = payload.get("jti")
    token_hash = payload.get("token_hash")
    if not isinstance(jti, str) or not SAFE_ID.fullmatch(jti):
        raise ValueError("introspection jti is invalid")
    if not isinstance(token_hash, str) or not SHA256.fullmatch(token_hash):
        raise ValueError("introspection token_hash is invalid")
    connection = connect_factory()
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT g.worker_id, g.profile_set_id, g.profile_revision,
                   g.manifest_digest, g.generation AS grant_generation,
                   g.expires_at, g.revoked_at,
                   w.enabled AS worker_enabled, w.deleted_at,
                   p.status AS pool_status,
                   p.profile_set_id AS desired_profile_set_id,
                   m.desired_revision,
                   p.gateway_token_generation,
                   r.manifest_digest AS desired_manifest_digest
            FROM gateway_token_grants AS g
            LEFT JOIN workers AS w ON w.worker_id = g.worker_id
            LEFT JOIN worker_pool_memberships AS m ON m.worker_id = g.worker_id
            LEFT JOIN worker_pools AS p ON p.worker_pool_id = m.worker_pool_id
            LEFT JOIN profile_set_revisions AS r
              ON r.profile_set_id = p.profile_set_id
             AND r.revision = m.desired_revision
            WHERE g.jti = ? AND g.token_hash = ?
            """,
            (jti, token_hash),
        ).fetchone()
    finally:
        connection.close()
    if not row:
        return {
            "active": False,
            "worker_enabled": False,
            "generation": 0,
            "desired_profile_revision": 0,
        }
    worker_enabled = int(row["worker_enabled"] or 0) == 1 and row["deleted_at"] is None
    generation = int(row["gateway_token_generation"] or 0)
    desired_revision = int(row["desired_revision"] or 0)
    active = bool(
        row["revoked_at"] is None
        and int(row["expires_at"] or 0) > timestamp
        and worker_enabled
        and row["pool_status"] == "active"
        and row["profile_set_id"] == row["desired_profile_set_id"]
        and int(row["profile_revision"] or 0) == desired_revision
        and row["manifest_digest"] == row["desired_manifest_digest"]
        and int(row["grant_generation"] or 0) == generation
        and worker_assignment_routes_available(connect_factory, str(row["worker_id"]))
    )
    return {
        "active": active,
        "worker_enabled": worker_enabled,
        "generation": generation,
        "desired_profile_revision": desired_revision,
    }


def resolve_gateway_route(
    connect_factory: ConnectFactory,
    *,
    presented_token: str | None,
    payload: object,
) -> dict[str, object]:
    _authorize(presented_token)
    if not isinstance(payload, dict) or set(payload) != {
        "profile_set_id",
        "profile_revision",
        "model_alias",
    }:
        raise ValueError("route resolution payload must be a closed object")
    profile_set_id = payload.get("profile_set_id")
    model_alias = payload.get("model_alias")
    revision = payload.get("profile_revision")
    if not isinstance(profile_set_id, str) or not SAFE_ID.fullmatch(profile_set_id):
        raise ValueError("route profile_set_id is invalid")
    if not isinstance(model_alias, str) or not SAFE_ID.fullmatch(model_alias):
        raise ValueError("route model_alias is invalid")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise ValueError("route profile_revision is invalid")
    connection = connect_factory()
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT pr.route_id, pr.profile_set_id, pr.revision AS profile_revision,
                   psr.manifest_digest, pr.model_alias,
                   pr.provider_connection_id, pc.provider AS upstream_provider,
                   pc.adapter, pc.endpoint_origin,
                   pr.upstream_model, pc.secret_ref, pc.secret_version,
                   pc.validated_models_json
            FROM profile_set_routes AS pr
            JOIN profile_set_revisions AS psr
              ON psr.profile_set_id = pr.profile_set_id
             AND psr.revision = pr.revision
            JOIN provider_connections AS pc
              ON pc.provider_connection_id = pr.provider_connection_id
            WHERE pr.profile_set_id = ? AND pr.revision = ?
              AND pr.model_alias = ? AND pr.enabled = 1
              AND psr.status = 'published'
              AND pc.status IN ('configured', 'rotation_staged', 'rotation_promoted')
              AND pr.api = pc.adapter
              AND EXISTS (
                  SELECT 1
                  FROM worker_pools AS wp
                  LEFT JOIN worker_pool_memberships AS wm
                    ON wm.worker_pool_id = wp.worker_pool_id
                  WHERE wp.profile_set_id = pr.profile_set_id
                    AND wp.status = 'active'
                    AND (wp.desired_revision = pr.revision OR wm.desired_revision = pr.revision)
              )
            """,
            (profile_set_id, revision, model_alias),
        ).fetchall()
    finally:
        connection.close()
    available = []
    for row in rows:
        try:
            validated_models = json.loads(row["validated_models_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(validated_models, list) and row["upstream_model"] in validated_models:
            record = dict(row)
            record.pop("validated_models_json", None)
            available.append(record)
    if len(available) != 1:
        raise GatewayRouteNotFound("Gateway route is unavailable")
    return available[0]
