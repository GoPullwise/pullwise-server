from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import closing

from .model_gateway_store import ConnectFactory


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _text(value: object, label: str, maximum: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        if not required and value is None:
            return ""
        raise ValueError(f"{label} is invalid")
    text = value.strip()
    if (required and not text) or len(text) > maximum or any(ord(character) < 32 for character in text):
        raise ValueError(f"{label} is invalid")
    return text


def create_worker_batch(
    connect_factory: ConnectFactory,
    *,
    worker_pool_id: str,
    actor_user_id: str,
    request_id: str | None,
    payload: object,
    timestamp: int,
) -> list[dict[str, object]]:
    if not isinstance(payload, dict) or set(payload) != {"count", "name_prefix", "region", "version"}:
        raise ValueError("worker batch payload must be a closed object")
    count = payload.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
        raise ValueError("worker batch count must be an integer from 1 through 100")
    name_prefix = _text(payload.get("name_prefix"), "name_prefix", 100)
    region = _text(payload.get("region"), "region", 120, required=False)
    version = _text(payload.get("version"), "version", 64, required=False)
    actor = _text(actor_user_id, "actor_user_id", 128)
    items: list[dict[str, object]] = []
    with closing(connect_factory()) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            pool = connection.execute(
                "SELECT desired_revision FROM worker_pools WHERE worker_pool_id = ? AND status = 'active'",
                (worker_pool_id,),
            ).fetchone()
            if not pool:
                raise ValueError("worker pool does not exist")
            for index in range(1, count + 1):
                worker_id = f"wk_{secrets.token_hex(24)}"
                bootstrap_id = f"wbc_{secrets.token_hex(16)}"
                bootstrap_token = f"pwb_{secrets.token_urlsafe(32)}"
                connection.execute(
                    """
                    INSERT INTO workers (
                        worker_id, name, token_hash, worker_scope, owner_user_id,
                        provider, provider_chain, enabled, status, running_jobs,
                        version, hostname, region, last_error, created_at, updated_at
                    ) VALUES (?, ?, NULL, 'shared', NULL, 'pullwise-gateway', ?,
                              1, 'offline', 0, ?, NULL, ?, NULL, ?, ?)
                    """,
                    (
                        worker_id,
                        f"{name_prefix} {index}"[:120],
                        json.dumps(["pullwise-gateway"], separators=(",", ":")),
                        version or None,
                        region or None,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO worker_pool_memberships (
                        worker_id, worker_pool_id, desired_revision, bound_by_user_id, request_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        worker_id,
                        worker_pool_id,
                        pool["desired_revision"],
                        actor,
                        request_id,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO worker_bootstrap_credentials (
                        bootstrap_id, worker_id, token_hash, expires_at, used_at, created_at
                    ) VALUES (?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        bootstrap_id,
                        worker_id,
                        _token_hash(bootstrap_token),
                        timestamp + 600,
                        timestamp,
                    ),
                )
                items.append(
                    {
                        "workerId": worker_id,
                        "name": f"{name_prefix} {index}"[:120],
                        "bootstrapToken": bootstrap_token,
                        "bootstrapExpiresAt": timestamp + 600,
                        "workerPoolId": worker_pool_id,
                    }
                )
            connection.execute(
                """
                INSERT INTO model_gateway_audit_events (
                    id, actor_user_id, action, subject_type, subject_id,
                    changed_fields_json, request_id, created_at, success, error_code
                ) VALUES (?, ?, 'worker_pool.batch_created', 'worker_pool', ?, ?, ?, ?, 1, NULL)
                """,
                (
                    f"audit_{secrets.token_urlsafe(18)}",
                    actor,
                    worker_pool_id,
                    json.dumps(
                        {"count": count, "workerIds": [item["workerId"] for item in items]},
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
    return items


def exchange_worker_bootstrap(
    connect_factory: ConnectFactory,
    *,
    presented_token: str | None,
    payload: object,
    timestamp: int,
) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != {"schema_id", "worker_id"}:
        raise ValueError("worker bootstrap exchange payload must be a closed object")
    if payload.get("schema_id") != "pullwise-worker-bootstrap-exchange/v1":
        raise ValueError("worker bootstrap exchange schema is invalid")
    worker_id = _text(payload.get("worker_id"), "worker_id", 128)
    if not isinstance(presented_token, str) or not presented_token.startswith("pwb_"):
        raise PermissionError("worker bootstrap credential is invalid")
    bootstrap_hash = _token_hash(presented_token)
    worker_token = f"pww_{secrets.token_urlsafe(32)}"
    with closing(connect_factory()) as connection:
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            credential = connection.execute(
                """
                SELECT worker_id, expires_at, used_at
                FROM worker_bootstrap_credentials
                WHERE token_hash = ? AND worker_id = ?
                """,
                (bootstrap_hash, worker_id),
            ).fetchone()
            if not credential or credential["used_at"] is not None or int(credential["expires_at"]) <= timestamp:
                raise PermissionError("worker bootstrap credential is invalid or expired")
            updated = connection.execute(
                "UPDATE workers SET token_hash = ?, token_last_used_at = ?, updated_at = ? WHERE worker_id = ? AND token_hash IS NULL",
                (_token_hash(worker_token), timestamp, timestamp, worker_id),
            )
            if updated.rowcount != 1:
                raise PermissionError("worker bootstrap credential has already been exchanged")
            connection.execute(
                "UPDATE worker_bootstrap_credentials SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
                (timestamp, bootstrap_hash),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return {"worker_id": worker_id, "worker_token": worker_token}
