from __future__ import annotations

import json
import sqlite3

from .model_gateway_store import ConnectFactory


def admin_model_gateway_snapshot(
    connect_factory: ConnectFactory,
    *,
    timestamp: int,
) -> dict[str, object]:
    connection = connect_factory()
    connection.row_factory = sqlite3.Row
    try:
        providers = [
            {
                "providerConnectionId": row["provider_connection_id"],
                "displayName": row["display_name"],
                "provider": row["provider"],
                "adapter": row["adapter"],
                "endpointOrigin": row["endpoint_origin"],
                "status": row["status"],
                "secretVersion": row["secret_version"],
                "secretFingerprint": row["secret_fingerprint"],
                "validatedModels": json.loads(row["validated_models_json"]),
                "candidateSecretVersion": row["candidate_secret_version"],
                "candidateSecretFingerprint": row["candidate_secret_fingerprint"],
                "previousSecretVersion": row["previous_secret_version"],
                "previousSecretFingerprint": row["previous_secret_fingerprint"],
                "lastValidatedAt": row["last_validated_at"],
                "lastRotatedAt": row["last_rotated_at"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }
            for row in connection.execute(
                """
                SELECT provider_connection_id, display_name, provider, adapter,
                       endpoint_origin, status, secret_version, secret_fingerprint,
                       validated_models_json,
                       candidate_secret_version, candidate_secret_fingerprint,
                       previous_secret_version, previous_secret_fingerprint,
                       last_validated_at, last_rotated_at, created_at, updated_at
                FROM provider_connections ORDER BY provider_connection_id
                """
            ).fetchall()
        ]
        profile_sets = [
            {
                "profileSetId": row["profile_set_id"],
                "displayName": row["display_name"],
                "status": row["status"],
                "activeRevision": row["active_revision"],
                "manifest": json.loads(row["manifest_json"]),
                "manifestDigest": row["manifest_digest"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }
            for row in connection.execute(
                """
                SELECT p.profile_set_id, p.display_name, p.status,
                       p.active_revision, p.created_at, p.updated_at,
                       r.manifest_json, r.manifest_digest
                FROM profile_sets AS p
                JOIN profile_set_revisions AS r
                  ON r.profile_set_id = p.profile_set_id
                 AND r.revision = p.active_revision
                ORDER BY p.profile_set_id
                """
            ).fetchall()
        ]
        worker_pools = [
            {
                "workerPoolId": row["worker_pool_id"],
                "displayName": row["display_name"],
                "profileSetId": row["profile_set_id"],
                "desiredRevision": row["desired_revision"],
                "gatewayTokenGeneration": row["gateway_token_generation"],
                "status": row["status"],
                "memberCount": row["member_count"],
                "targetMemberCount": row["target_member_count"],
                "readyMemberCount": row["ready_member_count"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }
            for row in connection.execute(
                """
                SELECT p.worker_pool_id, p.display_name, p.profile_set_id,
                       p.desired_revision, p.gateway_token_generation, p.status,
                       p.created_at, p.updated_at,
                       COUNT(m.worker_id) AS member_count,
                       COALESCE(SUM(CASE WHEN m.desired_revision = p.desired_revision THEN 1 ELSE 0 END), 0)
                           AS target_member_count,
                       COALESCE(SUM(CASE
                           WHEN w.enabled = 1 AND w.deleted_at IS NULL
                            AND o.applied_revision = m.desired_revision
                            AND o.desired_revision = m.desired_revision
                            AND o.profile_set_id = p.profile_set_id
                            AND o.manifest_digest = mr.manifest_digest
                            AND o.last_apply_result = 'succeeded'
                            AND o.gateway_token_expires_at > ?
                           THEN 1 ELSE 0 END), 0) AS ready_member_count
                FROM worker_pools AS p
                JOIN profile_set_revisions AS r
                  ON r.profile_set_id = p.profile_set_id
                 AND r.revision = p.desired_revision
                LEFT JOIN worker_pool_memberships AS m
                  ON m.worker_pool_id = p.worker_pool_id
                LEFT JOIN profile_set_revisions AS mr
                  ON mr.profile_set_id = p.profile_set_id
                 AND mr.revision = m.desired_revision
                LEFT JOIN workers AS w ON w.worker_id = m.worker_id
                LEFT JOIN worker_profile_observations AS o ON o.worker_id = m.worker_id
                GROUP BY p.worker_pool_id
                ORDER BY p.worker_pool_id
                """,
                (timestamp,),
            ).fetchall()
        ]
        return {
            "providerConnections": providers,
            "profileSets": profile_sets,
            "workerPools": worker_pools,
        }
    finally:
        connection.close()
