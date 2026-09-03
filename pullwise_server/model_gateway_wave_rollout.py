from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import closing

from .model_gateway_store import ConnectFactory


class WorkerPoolWaveRollout:
    def __init__(self, connect_factory: ConnectFactory) -> None:
        self._connect_factory = connect_factory

    def apply(
        self,
        *,
        worker_pool_id: str,
        profile_revision: int,
        worker_ids: list[str],
        actor_user_id: str,
        request_id: str | None,
        timestamp: int,
    ) -> dict[str, object]:
        if (
            not isinstance(worker_ids, list)
            or not worker_ids
            or len(worker_ids) > 100
            or any(not isinstance(worker_id, str) or not worker_id for worker_id in worker_ids)
            or len(worker_ids) != len(set(worker_ids))
        ):
            raise ValueError("rollout wave worker_ids must contain 1 through 100 unique Workers")
        if isinstance(profile_revision, bool) or not isinstance(profile_revision, int) or profile_revision <= 0:
            raise ValueError("rollout wave profile_revision is invalid")
        with closing(self._connect_factory()) as connection:
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("BEGIN IMMEDIATE")
                pool = connection.execute(
                    "SELECT * FROM worker_pools WHERE worker_pool_id = ? AND status = 'active'",
                    (worker_pool_id,),
                ).fetchone()
                if not pool:
                    raise ValueError("worker pool does not exist")
                if not connection.execute(
                    """
                    SELECT 1 FROM profile_set_revisions
                    WHERE profile_set_id = ? AND revision = ? AND status = 'published'
                    """,
                    (pool["profile_set_id"], profile_revision),
                ).fetchone():
                    raise ValueError("rollout wave requires a published revision from the Pool Profile Set")
                placeholders = ",".join("?" for _ in worker_ids)
                members = connection.execute(
                    f"""
                    SELECT worker_id FROM worker_pool_memberships
                    WHERE worker_pool_id = ? AND worker_id IN ({placeholders})
                    """,
                    (worker_pool_id, *worker_ids),
                ).fetchall()
                if {row["worker_id"] for row in members} != set(worker_ids):
                    raise ValueError("rollout wave contains a Worker outside the Pool")
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
                    f"""
                    UPDATE worker_pool_memberships
                    SET desired_revision = ?, updated_at = ?
                    WHERE worker_pool_id = ? AND worker_id IN ({placeholders})
                    """,
                    (profile_revision, timestamp, worker_pool_id, *worker_ids),
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
                connection.execute(
                    """
                    INSERT INTO model_gateway_audit_events (
                        id, actor_user_id, action, subject_type, subject_id,
                        changed_fields_json, request_id, created_at, success, error_code
                    ) VALUES (?, ?, 'worker_pool.rollout_wave', 'worker_pool', ?, ?, ?, ?, 1, NULL)
                    """,
                    (
                        f"audit_{secrets.token_urlsafe(18)}",
                        actor_user_id,
                        worker_pool_id,
                        json.dumps(
                            {
                                "desiredRevision": profile_revision,
                                "gatewayTokenGeneration": generation,
                                "workerIds": sorted(worker_ids),
                            },
                            separators=(",", ":"),
                        ),
                        request_id,
                        timestamp,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return {
            "worker_pool_id": worker_pool_id,
            "desired_revision": profile_revision,
            "gateway_token_generation": generation,
            "updated_worker_ids": sorted(worker_ids),
            "updated_at": timestamp,
        }
