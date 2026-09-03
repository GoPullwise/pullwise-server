from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .model_gateway_store import ConnectFactory


class ModelGatewayPoolStore:
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

    def create_worker_pool(
        self,
        *,
        worker_pool_id: str,
        display_name: str,
        profile_set_id: str,
        profile_revision: int,
        actor_user_id: str,
        request_id: str | None,
        timestamp: int,
        audit_id: str,
    ) -> dict[str, object]:
        with self._transaction() as connection:
            revision = connection.execute(
                """
                SELECT status FROM profile_set_revisions
                WHERE profile_set_id = ? AND revision = ?
                """,
                (profile_set_id, profile_revision),
            ).fetchone()
            if not revision or revision["status"] != "published":
                raise ValueError("worker pool requires a published profile revision")
            try:
                connection.execute(
                    """
                    INSERT INTO worker_pools (
                        worker_pool_id, display_name, profile_set_id,
                        desired_revision, gateway_token_generation, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, 'active', ?, ?)
                    """,
                    (
                        worker_pool_id,
                        display_name,
                        profile_set_id,
                        profile_revision,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("worker pool already exists") from exc
            self._insert_audit(
                connection,
                audit_id=audit_id,
                actor_user_id=actor_user_id,
                action="worker_pool.created",
                subject_id=worker_pool_id,
                changed_fields={
                    "profileSetId": profile_set_id,
                    "desiredRevision": profile_revision,
                    "gatewayTokenGeneration": 1,
                },
                request_id=request_id,
                timestamp=timestamp,
            )
        return {
            "worker_pool_id": worker_pool_id,
            "display_name": display_name,
            "profile_set_id": profile_set_id,
            "desired_revision": profile_revision,
            "gateway_token_generation": 1,
            "status": "active",
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    def bind_worker_to_pool(
        self,
        *,
        worker_id: str,
        worker_pool_id: str,
        actor_user_id: str,
        request_id: str | None,
        timestamp: int,
        audit_id: str,
    ) -> dict[str, object]:
        with self._transaction() as connection:
            if not connection.execute(
                "SELECT 1 FROM workers WHERE worker_id = ? AND deleted_at IS NULL",
                (worker_id,),
            ).fetchone():
                raise ValueError("worker does not exist")
            pool = connection.execute(
                "SELECT desired_revision FROM worker_pools WHERE worker_pool_id = ? AND status = 'active'",
                (worker_pool_id,),
            ).fetchone()
            if not pool:
                raise ValueError("worker pool does not exist")
            existing = connection.execute(
                "SELECT created_at FROM worker_pool_memberships WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            created_at = int(existing["created_at"]) if existing else timestamp
            connection.execute(
                """
                INSERT INTO worker_pool_memberships (
                    worker_id, worker_pool_id, desired_revision, bound_by_user_id, request_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    worker_pool_id = excluded.worker_pool_id,
                    desired_revision = excluded.desired_revision,
                    bound_by_user_id = excluded.bound_by_user_id,
                    request_id = excluded.request_id,
                    updated_at = excluded.updated_at
                """,
                (
                    worker_id,
                    worker_pool_id,
                    pool["desired_revision"],
                    actor_user_id,
                    request_id,
                    created_at,
                    timestamp,
                ),
            )
            self._insert_audit(
                connection,
                audit_id=audit_id,
                actor_user_id=actor_user_id,
                action="worker_pool.member_bound",
                subject_id=worker_pool_id,
                changed_fields={"workerId": worker_id},
                request_id=request_id,
                timestamp=timestamp,
            )
        return {
            "worker_id": worker_id,
            "worker_pool_id": worker_pool_id,
            "created_at": created_at,
            "updated_at": timestamp,
        }

    def worker_profile_assignment(self, worker_id: str) -> dict[str, object] | None:
        connection = self._connect_factory()
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """
                SELECT m.worker_id, p.worker_pool_id, p.profile_set_id,
                       m.desired_revision, p.gateway_token_generation,
                       r.manifest_json, r.manifest_digest
                FROM worker_pool_memberships AS m
                JOIN worker_pools AS p ON p.worker_pool_id = m.worker_pool_id
                JOIN profile_set_revisions AS r
                  ON r.profile_set_id = p.profile_set_id
                 AND r.revision = m.desired_revision
                JOIN workers AS w ON w.worker_id = m.worker_id
                WHERE m.worker_id = ? AND p.status = 'active'
                  AND r.status = 'published' AND w.enabled = 1
                  AND w.deleted_at IS NULL
                """,
                (worker_id,),
            ).fetchone()
            if not row:
                return None
            manifest = json.loads(row["manifest_json"])
            return {
                **dict(row),
                "manifest": manifest,
                "route_ids": [
                    route["route_id"]
                    for route in manifest["routes"]
                    if route["enabled"] is True
                ],
            }
        finally:
            connection.close()

    def rotate_gateway_token_generation(
        self,
        *,
        worker_pool_id: str,
        actor_user_id: str,
        request_id: str | None,
        timestamp: int,
    ) -> dict[str, object]:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM worker_pools WHERE worker_pool_id = ? AND status = 'active'",
                (worker_pool_id,),
            ).fetchone()
            if not row:
                raise ValueError("worker pool does not exist")
            generation = int(row["gateway_token_generation"]) + 1
            connection.execute(
                """
                UPDATE worker_pools
                SET gateway_token_generation = ?, updated_at = ?
                WHERE worker_pool_id = ?
                """,
                (generation, timestamp, worker_pool_id),
            )
            connection.execute(
                """
                UPDATE gateway_token_grants
                SET revoked_at = ?
                WHERE revoked_at IS NULL AND worker_id IN (
                    SELECT worker_id FROM worker_pool_memberships
                    WHERE worker_pool_id = ?
                )
                """,
                (timestamp, worker_pool_id),
            )
            self._insert_audit(
                connection,
                audit_id=f"audit_{secrets.token_urlsafe(18)}",
                actor_user_id=actor_user_id,
                action="worker_pool.gateway_tokens_rotated",
                subject_id=worker_pool_id,
                changed_fields={"gatewayTokenGeneration": generation},
                request_id=request_id,
                timestamp=timestamp,
            )
        return {
            "worker_pool_id": worker_pool_id,
            "gateway_token_generation": generation,
            "updated_at": timestamp,
        }

    def set_desired_revision(
        self,
        *,
        worker_pool_id: str,
        profile_revision: int,
        actor_user_id: str,
        request_id: str | None,
        timestamp: int,
    ) -> dict[str, object]:
        with self._transaction() as connection:
            pool = connection.execute(
                "SELECT * FROM worker_pools WHERE worker_pool_id = ? AND status = 'active'",
                (worker_pool_id,),
            ).fetchone()
            if not pool:
                raise ValueError("worker pool does not exist")
            if int(pool["desired_revision"]) == profile_revision:
                return dict(pool)
            revision = connection.execute(
                """
                SELECT 1 FROM profile_set_revisions
                WHERE profile_set_id = ? AND revision = ? AND status = 'published'
                """,
                (pool["profile_set_id"], profile_revision),
            ).fetchone()
            if not revision:
                raise ValueError("worker pool requires a published revision from its Profile Set")
            unavailable = connection.execute(
                """
                SELECT pr.route_id
                FROM profile_set_routes AS pr
                JOIN provider_connections AS pc
                  ON pc.provider_connection_id = pr.provider_connection_id
                WHERE pr.profile_set_id = ? AND pr.revision = ? AND pr.enabled = 1
                  AND pc.status NOT IN ('configured', 'rotation_staged', 'rotation_promoted')
                LIMIT 1
                """,
                (pool["profile_set_id"], profile_revision),
            ).fetchone()
            if unavailable:
                raise ValueError(f"worker pool revision contains unavailable route: {unavailable['route_id']}")
            generation = int(pool["gateway_token_generation"]) + 1
            connection.execute(
                """
                UPDATE worker_pools
                SET desired_revision = ?, gateway_token_generation = ?, updated_at = ?
                WHERE worker_pool_id = ?
                """,
                (profile_revision, generation, timestamp, worker_pool_id),
            )
            connection.execute(
                """
                UPDATE worker_pool_memberships
                SET desired_revision = ?, updated_at = ?
                WHERE worker_pool_id = ?
                """,
                (profile_revision, timestamp, worker_pool_id),
            )
            connection.execute(
                """
                UPDATE gateway_token_grants SET revoked_at = ?
                WHERE revoked_at IS NULL AND worker_id IN (
                    SELECT worker_id FROM worker_pool_memberships WHERE worker_pool_id = ?
                )
                """,
                (timestamp, worker_pool_id),
            )
            self._insert_audit(
                connection,
                audit_id=f"audit_{secrets.token_urlsafe(18)}",
                actor_user_id=actor_user_id,
                action="worker_pool.desired_revision_changed",
                subject_id=worker_pool_id,
                changed_fields={
                    "previousRevision": pool["desired_revision"],
                    "desiredRevision": profile_revision,
                    "gatewayTokenGeneration": generation,
                },
                request_id=request_id,
                timestamp=timestamp,
            )
            updated = connection.execute(
                "SELECT * FROM worker_pools WHERE worker_pool_id = ?",
                (worker_pool_id,),
            ).fetchone()
            return dict(updated)

    @staticmethod
    def _insert_audit(
        connection: sqlite3.Connection,
        *,
        audit_id: str,
        actor_user_id: str,
        action: str,
        subject_id: str,
        changed_fields: dict[str, object],
        request_id: str | None,
        timestamp: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO model_gateway_audit_events (
                id, actor_user_id, action, subject_type, subject_id,
                changed_fields_json, request_id, created_at, success, error_code
            ) VALUES (?, ?, ?, 'worker_pool', ?, ?, ?, ?, 1, NULL)
            """,
            (
                audit_id,
                actor_user_id,
                action,
                subject_id,
                json.dumps(changed_fields, sort_keys=True, separators=(",", ":")),
                request_id,
                timestamp,
            ),
        )
