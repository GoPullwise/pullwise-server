from __future__ import annotations

import calendar
import math
import os
import sqlite3
import time
from contextlib import closing
from typing import Any

from . import db


class QuotaExceeded(Exception):
    def __init__(self, code: str, message: str, *, workspace_id: str | None = None, repo_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.workspace_id = workspace_id
        self.repo_id = repo_id


def env_int(names: str | list[str], default: int) -> int:
    candidates = [names] if isinstance(names, str) else names
    for name in candidates:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            continue
        try:
            return int(raw)
        except ValueError:
            return default
    return default


def current_period(timestamp: int | None = None) -> str:
    return time.strftime("%Y-%m", time.gmtime(timestamp or int(time.time())))


def reset_at_for_period(period: str) -> int:
    try:
        year_text, month_text = period.split("-", 1)
        year = int(year_text)
        month = int(month_text)
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
        return calendar.timegm((year, month, 1, 0, 0, 0))
    except (TypeError, ValueError):
        now = time.gmtime()
        return calendar.timegm((now.tm_year + (1 if now.tm_mon == 12 else 0), 1 if now.tm_mon == 12 else now.tm_mon + 1, 1, 0, 0, 0))


def non_negative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    try:
        return max(0, int(value or 0))
    except (OverflowError, TypeError, ValueError):
        return 0


def effective_workspace_plan(workspace: dict[str, Any] | None) -> str:
    if not workspace:
        return "free"
    status = str(workspace.get("billing_status") or workspace.get("status") or "").lower()
    plan = str(workspace.get("plan") or "free").lower()
    if plan == "pro" and status in {"active", "trialing", "canceling"}:
        return "pro"
    return "free"


def workspace_limit_for_plan(plan: str) -> int:
    if plan == "pro":
        return max(0, env_int(["PULLWISE_PRO_WORKSPACE_REVIEW_LIMIT", "PULLWISE_PRO_REVIEW_LIMIT"], 100))
    return max(0, env_int(["PULLWISE_FREE_WORKSPACE_REVIEW_LIMIT", "PULLWISE_FREE_REVIEW_LIMIT"], 10))


def repository_limit_for_plan(plan: str) -> int:
    if plan == "pro":
        return max(0, env_int("PULLWISE_PRO_REPO_REVIEW_LIMIT", workspace_limit_for_plan("pro")))
    return max(0, env_int("PULLWISE_FREE_REPO_REVIEW_LIMIT", 3))


def repository_quota_scope_id(repository: dict[str, Any]) -> str:
    if non_negative_int(repository.get("fork")) > 0:
        source_id = str(repository.get("source_github_repo_id") or repository.get("parent_github_repo_id") or "").strip()
        if source_id:
            return db.repository_id_for_github_repo(source_id)
    return str(repository["id"])


def ensure_quota_bucket(
    *,
    scope_type: str,
    scope_id: str,
    period: str | None = None,
    plan: str = "free",
    limit: int,
) -> dict[str, Any]:
    db.initialize()
    period = period or current_period()
    with closing(db.connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            return _ensure_quota_bucket(connection, scope_type=scope_type, scope_id=scope_id, period=period, plan=plan, limit=limit)


def _ensure_quota_bucket(
    connection: sqlite3.Connection,
    *,
    scope_type: str,
    scope_id: str,
    period: str,
    plan: str,
    limit: int,
) -> dict[str, Any]:
    bucket_id = db.quota_bucket_id(scope_type, scope_id, period, plan)
    limit = max(0, int(limit or 0))
    connection.execute(
        """
        INSERT INTO quota_buckets (id, scope_type, scope_id, period, plan, quota_limit, used, reset_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?, strftime('%s', 'now'), strftime('%s', 'now'))
        ON CONFLICT(scope_type, scope_id, period, plan) DO UPDATE SET
            quota_limit = excluded.quota_limit,
            updated_at = excluded.updated_at
        """,
        (bucket_id, scope_type, scope_id, period, plan, limit, reset_at_for_period(period)),
    )
    row = connection.execute("SELECT * FROM quota_buckets WHERE id = ?", (bucket_id,)).fetchone()
    bucket = dict(row)
    used = non_negative_int(bucket.get("used"))
    if used != bucket.get("used"):
        connection.execute("UPDATE quota_buckets SET used = ?, updated_at = strftime('%s', 'now') WHERE id = ?", (used, bucket_id))
        bucket["used"] = used
    return bucket


def quota_payload(bucket: dict[str, Any], *, scope: str) -> dict[str, Any]:
    used = non_negative_int(bucket.get("used"))
    limit = non_negative_int(bucket.get("quota_limit"))
    return {
        "scope": scope,
        "period": str(bucket.get("period") or current_period()),
        "plan": str(bucket.get("plan") or "free"),
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        "resetAt": non_negative_int(bucket.get("reset_at")),
        "bucketId": str(bucket.get("id") or ""),
    }


def quota_payload_for_workspace(workspace: dict[str, Any], *, timestamp: int | None = None) -> dict[str, Any]:
    plan = effective_workspace_plan(workspace)
    bucket = ensure_quota_bucket(
        scope_type="workspace",
        scope_id=str(workspace["id"]),
        period=current_period(timestamp),
        plan=plan,
        limit=workspace_limit_for_plan(plan),
    )
    return quota_payload(bucket, scope="workspace")


def migrate_workspace_usage(workspace: dict[str, Any], *, period: str, used: int, plan: str | None = None) -> dict[str, Any]:
    db.initialize()
    plan = plan or effective_workspace_plan(workspace)
    used = non_negative_int(used)
    with closing(db.connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            bucket = _ensure_quota_bucket(
                connection,
                scope_type="workspace",
                scope_id=str(workspace["id"]),
                period=period,
                plan=plan,
                limit=workspace_limit_for_plan(plan),
            )
            if non_negative_int(bucket.get("used")) < used:
                connection.execute(
                    "UPDATE quota_buckets SET used = ?, updated_at = strftime('%s', 'now') WHERE id = ?",
                    (used, bucket["id"]),
                )
                bucket = dict(connection.execute("SELECT * FROM quota_buckets WHERE id = ?", (bucket["id"],)).fetchone())
            return quota_payload(bucket, scope="workspace")


def quota_payload_for_repository(repository: dict[str, Any], workspace: dict[str, Any] | None = None, *, timestamp: int | None = None) -> dict[str, Any]:
    plan = effective_workspace_plan(workspace)
    bucket = ensure_quota_bucket(
        scope_type="repository",
        scope_id=repository_quota_scope_id(repository),
        period=current_period(timestamp),
        plan=plan,
        limit=repository_limit_for_plan(plan),
    )
    return quota_payload(bucket, scope="repository")


def consume_scan_quota(
    *,
    workspace: dict[str, Any],
    repository: dict[str, Any],
    requested_by_user_id: str,
    scan_id: str,
    request_id: str | None = None,
    timestamp: int | None = None,
) -> dict[str, Any]:
    db.initialize()
    plan = effective_workspace_plan(workspace)
    period = current_period(timestamp)
    workspace_limit = workspace_limit_for_plan(plan)
    repository_limit = repository_limit_for_plan(plan)
    workspace_id = str(workspace["id"])
    repository_id = str(repository["id"])
    repository_scope_id = repository_quota_scope_id(repository)
    github_repo_id = str(repository["github_repo_id"])

    with closing(db.connect()) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing_request = None
            if request_id:
                existing_request = connection.execute(
                    """
                    SELECT 1
                    FROM quota_ledger
                    WHERE requested_by_user_id = ?
                      AND request_id = ?
                      AND repository_id = ?
                      AND reason = 'scan_created'
                    LIMIT 1
                    """,
                    (requested_by_user_id, request_id, repository_id),
                ).fetchone()
            workspace_bucket = _ensure_quota_bucket(
                connection,
                scope_type="workspace",
                scope_id=workspace_id,
                period=period,
                plan=plan,
                limit=workspace_limit,
            )
            repository_bucket = _ensure_quota_bucket(
                connection,
                scope_type="repository",
                scope_id=repository_scope_id,
                period=period,
                plan=plan,
                limit=repository_limit,
            )
            if existing_request:
                connection.commit()
                return {
                    "deduplicated": True,
                    "workspace": quota_payload(workspace_bucket, scope="workspace"),
                    "repository": quota_payload(repository_bucket, scope="repository"),
                    "bucketIds": {
                        "workspace": workspace_bucket["id"],
                        "repository": repository_bucket["id"],
                    },
                }

            repo_updated = connection.execute(
                """
                UPDATE quota_buckets
                SET used = used + 1, updated_at = strftime('%s', 'now')
                WHERE id = ? AND used < quota_limit
                """,
                (repository_bucket["id"],),
            ).rowcount
            if repo_updated != 1:
                connection.rollback()
                raise QuotaExceeded(
                    "QUOTA_EXCEEDED_REPOSITORY",
                    "This repository has used its free scan quota for the current workspace.",
                    workspace_id=workspace_id,
                    repo_id=repository_id,
                )

            workspace_updated = connection.execute(
                """
                UPDATE quota_buckets
                SET used = used + 1, updated_at = strftime('%s', 'now')
                WHERE id = ? AND used < quota_limit
                """,
                (workspace_bucket["id"],),
            ).rowcount
            if workspace_updated != 1:
                connection.rollback()
                raise QuotaExceeded(
                    "QUOTA_EXCEEDED_WORKSPACE",
                    "This workspace has used its shared scan quota for the current billing period.",
                    workspace_id=workspace_id,
                    repo_id=repository_id,
                )

            for bucket_id in (workspace_bucket["id"], repository_bucket["id"]):
                connection.execute(
                    """
                    INSERT INTO quota_ledger (
                        id, workspace_id, repository_id, github_repo_id, scan_id,
                        requested_by_user_id, request_id, bucket_id, delta, reason, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'scan_created', strftime('%s', 'now'))
                    """,
                    (
                        db.quota_ledger_id(bucket_id, scan_id, requested_by_user_id, request_id),
                        workspace_id,
                        repository_id,
                        github_repo_id,
                        scan_id,
                        requested_by_user_id,
                        request_id,
                        bucket_id,
                    ),
                )

            workspace_bucket = dict(connection.execute("SELECT * FROM quota_buckets WHERE id = ?", (workspace_bucket["id"],)).fetchone())
            repository_bucket = dict(connection.execute("SELECT * FROM quota_buckets WHERE id = ?", (repository_bucket["id"],)).fetchone())
            connection.commit()
            return {
                "deduplicated": False,
                "workspace": quota_payload(workspace_bucket, scope="workspace"),
                "repository": quota_payload(repository_bucket, scope="repository"),
                "bucketIds": {
                    "workspace": workspace_bucket["id"],
                    "repository": repository_bucket["id"],
                },
            }
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
