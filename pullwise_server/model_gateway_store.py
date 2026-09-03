from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from typing import Callable, Iterator


ConnectFactory = Callable[[], sqlite3.Connection]
class ModelGatewayStore:
    def __init__(self, connect_factory: ConnectFactory) -> None:
        self._connect_factory = connect_factory

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect_factory()
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_provider_connection(
        self,
        record: dict[str, object],
        *,
        audit_event: dict[str, object],
    ) -> dict[str, object]:
        with self._transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO provider_connections (
                        provider_connection_id, display_name, provider, adapter,
                        endpoint_origin, secret_ref, status, secret_version,
                        secret_fingerprint, validated_models_json,
                        candidate_secret_version,
                        candidate_secret_fingerprint, previous_secret_version,
                        previous_secret_fingerprint, last_validated_at, last_rotated_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["provider_connection_id"],
                        record["display_name"],
                        record["provider"],
                        record["adapter"],
                        record["endpoint_origin"],
                        record["secret_ref"],
                        record["status"],
                        record["secret_version"],
                        record["secret_fingerprint"],
                        record["validated_models_json"],
                        record["candidate_secret_version"],
                        record["candidate_secret_fingerprint"],
                        record["previous_secret_version"],
                        record["previous_secret_fingerprint"],
                        record["last_validated_at"],
                        record["last_rotated_at"],
                        record["created_at"],
                        record["updated_at"],
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("provider connection already exists") from exc
            connection.execute(
                """
                INSERT INTO provider_secret_versions (
                    provider_connection_id, secret_version, secret_fingerprint, validated_models_json,
                    state, created_at, validated_at, promoted_at, retired_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, NULL)
                """,
                (
                    record["provider_connection_id"],
                    record["secret_version"],
                    record["secret_fingerprint"],
                    record["validated_models_json"],
                    record["created_at"],
                    record["created_at"],
                    record["created_at"],
                ),
            )
            connection.execute(
                """
                INSERT INTO model_gateway_audit_events (
                    id, actor_user_id, action, subject_type, subject_id,
                    changed_fields_json, request_id, created_at, success, error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_event["id"],
                    audit_event["actor_user_id"],
                    audit_event["action"],
                    audit_event["subject_type"],
                    audit_event["subject_id"],
                    json.dumps(
                        audit_event["changed_fields"],
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    audit_event["request_id"],
                    audit_event["created_at"],
                    1 if audit_event["success"] else 0,
                    audit_event["error_code"],
                ),
            )
        return dict(record)

    def list_provider_connections(self) -> list[dict[str, object]]:
        connection = self._connect_factory()
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT provider_connection_id, display_name, provider, adapter,
                       endpoint_origin, secret_ref, status, secret_version,
                       secret_fingerprint, validated_models_json,
                       candidate_secret_version,
                       candidate_secret_fingerprint, previous_secret_version,
                       previous_secret_fingerprint, last_validated_at, last_rotated_at,
                       created_at, updated_at
                FROM provider_connections
                ORDER BY provider_connection_id
                """
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def record_gateway_token_grant(self, grant: dict[str, object]) -> None:
        with self._transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO gateway_token_grants (
                        jti, token_hash, worker_id, profile_set_id,
                        profile_revision, manifest_digest, route_ids_json, generation,
                        issued_at, expires_at, revoked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        grant["jti"],
                        grant["token_hash"],
                        grant["worker_id"],
                        grant["profile_set_id"],
                        grant["profile_revision"],
                        grant["manifest_digest"],
                        grant["route_ids_json"],
                        grant["generation"],
                        grant["issued_at"],
                        grant["expires_at"],
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("gateway token grant already exists") from exc

    def publish_profile_set(
        self,
        *,
        profile_set_id: str,
        display_name: str,
        routes: list[dict[str, object]],
        actor_user_id: str,
        request_id: str | None,
        timestamp: int,
        audit_id: str,
    ) -> dict[str, object]:
        with self._transaction() as connection:
            placeholders = ",".join("?" for _ in routes)
            connection_ids = [str(route["provider_connection_id"]) for route in routes]
            available = {
                str(row["provider_connection_id"]): set(json.loads(row["validated_models_json"]))
                for row in connection.execute(
                    f"""
                    SELECT provider_connection_id, validated_models_json
                    FROM provider_connections
                    WHERE status = 'configured'
                      AND provider_connection_id IN ({placeholders})
                    """,
                    connection_ids,
                ).fetchall()
            }
            missing = sorted(set(connection_ids) - set(available))
            if missing:
                raise ValueError(f"provider connection is unavailable: {missing[0]}")
            for route in routes:
                models = available[str(route["provider_connection_id"])]
                if route["upstream_model"] not in models:
                    raise ValueError(f"upstream model is unavailable: {route['upstream_model']}")
            current = connection.execute(
                "SELECT active_revision, created_at FROM profile_sets WHERE profile_set_id = ?",
                (profile_set_id,),
            ).fetchone()
            revision = int(current["active_revision"]) + 1 if current else 1
            manifest = {
                "schema_id": "pullwise-model-profile-set/v1",
                "profile_set_id": profile_set_id,
                "revision": revision,
                "routes": routes,
            }
            manifest_json = json.dumps(
                manifest,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            manifest_digest = hashlib.sha256(manifest_json.encode("ascii")).hexdigest()
            if current:
                connection.execute(
                    """
                    UPDATE profile_sets
                    SET display_name = ?, status = 'published', active_revision = ?, updated_at = ?
                    WHERE profile_set_id = ?
                    """,
                    (display_name, revision, timestamp, profile_set_id),
                )
                created_at = int(current["created_at"])
            else:
                connection.execute(
                    """
                    INSERT INTO profile_sets (
                        profile_set_id, display_name, status, active_revision,
                        created_at, updated_at
                    ) VALUES (?, ?, 'published', ?, ?, ?)
                    """,
                    (profile_set_id, display_name, revision, timestamp, timestamp),
                )
                created_at = timestamp
            connection.execute(
                """
                INSERT INTO profile_set_revisions (
                    profile_set_id, revision, status, manifest_json,
                    manifest_digest, created_by_user_id, request_id, created_at
                ) VALUES (?, ?, 'published', ?, ?, ?, ?, ?)
                """,
                (
                    profile_set_id,
                    revision,
                    manifest_json,
                    manifest_digest,
                    actor_user_id,
                    request_id,
                    timestamp,
                ),
            )
            for position, route in enumerate(routes):
                connection.execute(
                    """
                    INSERT INTO profile_set_routes (
                        profile_set_id, revision, route_position, route_id,
                        provider_connection_id, provider, model_alias,
                        upstream_model, api, enabled
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        profile_set_id,
                        revision,
                        position,
                        route["route_id"],
                        route["provider_connection_id"],
                        route["provider"],
                        route["model_alias"],
                        route["upstream_model"],
                        route["api"],
                        1 if route["enabled"] else 0,
                    ),
                )
            connection.execute(
                """
                INSERT INTO model_gateway_audit_events (
                    id, actor_user_id, action, subject_type, subject_id,
                    changed_fields_json, request_id, created_at, success, error_code
                ) VALUES (?, ?, ?, 'profile_set', ?, ?, ?, ?, 1, NULL)
                """,
                (
                    audit_id,
                    actor_user_id,
                    "profile_set.published",
                    profile_set_id,
                    json.dumps(
                        {
                            "manifestDigest": manifest_digest,
                            "revision": revision,
                            "routeIds": [route["route_id"] for route in routes],
                        },
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    request_id,
                    timestamp,
                ),
            )
        return {
            "profile_set_id": profile_set_id,
            "display_name": display_name,
            "revision": revision,
            "status": "published",
            "manifest": manifest,
            "manifest_digest": manifest_digest,
            "created_at": created_at,
            "updated_at": timestamp,
        }
