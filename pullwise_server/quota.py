from __future__ import annotations

import calendar
import datetime
import math
import sqlite3
import time
from contextlib import closing
from typing import Any

from . import db, system_config


PAID_PLAN_IDS = {"pro", "max"}
PLAN_IDS = {"free", *PAID_PLAN_IDS}


class QuotaExceeded(Exception):
    def __init__(self, code: str, message: str, *, repo_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.repo_id = repo_id


def current_period(timestamp: int | None = None) -> str:
    return time.strftime("%Y-%m", time.gmtime(current_timestamp(timestamp)))


def current_timestamp(timestamp: int | None = None) -> int:
    return int(time.time()) if timestamp is None else int(timestamp)


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


def timestamp_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        if not math.isfinite(value):
            return None
        candidate = int(value)
        return candidate if candidate >= 0 else None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
        try:
            normalized = text.replace("Z", "+00:00")
            parsed = datetime.datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        candidate = int(parsed.timestamp())
        return candidate if candidate >= 0 else None
    return None


def add_months_utc(timestamp: int, months: int) -> int:
    current = time.gmtime(timestamp)
    month_index = current.tm_year * 12 + current.tm_mon - 1 + months
    year = month_index // 12
    month = month_index % 12 + 1
    day = min(current.tm_mday, calendar.monthrange(year, month)[1])
    return calendar.timegm((year, month, day, current.tm_hour, current.tm_min, current.tm_sec))


def monthly_cycle_bounds(anchor: int, timestamp: int) -> tuple[int, int]:
    if timestamp < anchor:
        return anchor, add_months_utc(anchor, 1)
    anchor_time = time.gmtime(anchor)
    timestamp_time = time.gmtime(timestamp)
    months = (timestamp_time.tm_year - anchor_time.tm_year) * 12 + timestamp_time.tm_mon - anchor_time.tm_mon
    start = add_months_utc(anchor, months)
    if start > timestamp:
        months -= 1
        start = add_months_utc(anchor, months)
    reset_at = add_months_utc(anchor, months + 1)
    while reset_at <= timestamp:
        months += 1
        start = reset_at
        reset_at = add_months_utc(anchor, months + 1)
    return start, reset_at


def cycle_period(start: int) -> str:
    return f"cycle:{start}"


def non_negative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    try:
        return max(0, int(value or 0))
    except (OverflowError, TypeError, ValueError):
        return 0


def normalize_plan(plan: object, default: str = "free") -> str:
    normalized_default = default if default in PLAN_IDS else "free"
    normalized = str(plan or normalized_default).strip().lower()
    return normalized if normalized in PLAN_IDS else normalized_default


def effective_user_plan(user: dict[str, Any] | None, *, timestamp: int | None = None) -> str:
    if not user:
        return "free"
    current_time = current_timestamp(timestamp)
    billing = user.get("billing") if isinstance(user.get("billing"), dict) else {}
    status = str(billing.get("status") or "").lower()
    plan = normalize_plan(billing.get("plan"), default="free")
    current_period_start = timestamp_value(billing.get("currentPeriodStart"))
    current_period_end = timestamp_value(billing.get("currentPeriodEnd"))
    if current_period_start is not None and current_period_start > current_time:
        return "free"
    if current_period_end is not None and current_period_end <= current_time:
        return "free"
    if plan in PAID_PLAN_IDS and status in {"active", "trialing", "canceling"}:
        return plan
    return "free"


def user_limit_for_plan(plan: str) -> int:
    return system_config.plan_user_review_limit(normalize_plan(plan))


def repository_limit_for_plan(plan: str) -> int:
    return system_config.plan_repository_review_limit(normalize_plan(plan))


def quota_cycle_for_user(user: dict[str, Any] | None, plan: str, *, timestamp: int | None = None) -> tuple[str, int]:
    current_time = current_timestamp(timestamp)
    billing = user.get("billing") if user and isinstance(user.get("billing"), dict) else {}
    anchor: int | None = None
    period_end: int | None = None
    if plan in PAID_PLAN_IDS:
        current_period_start = timestamp_value(billing.get("currentPeriodStart"))
        current_period_end = timestamp_value(billing.get("currentPeriodEnd"))
        if current_period_start is not None:
            anchor = current_period_start
        elif current_period_end is not None and current_period_end > current_time:
            anchor = add_months_utc(current_period_end, -1)
        else:
            anchor = (
                timestamp_value(billing.get("lastEventCreated"))
                or timestamp_value(billing.get("updatedAt"))
                or timestamp_value(user.get("createdAt") if user else None)
            )
        period_end = current_period_end if current_period_end is not None and current_period_end > current_time else None
    else:
        expired_at = timestamp_value(billing.get("currentPeriodEnd"))
        if expired_at is not None and expired_at <= current_time:
            anchor = expired_at
        else:
            anchor = timestamp_value(user.get("createdAt") if user else None)

    if anchor is not None and anchor <= current_time:
        start, reset_at = monthly_cycle_bounds(anchor, current_time)
        if period_end is not None and current_time < period_end < reset_at:
            reset_at = period_end
        return cycle_period(start), reset_at

    period = current_period(current_time)
    return period, reset_at_for_period(period)


def quota_entitlement_for_user(user: dict[str, Any] | None, *, timestamp: int | None = None) -> dict[str, Any]:
    plan = effective_user_plan(user, timestamp=timestamp)
    period, reset_at = quota_cycle_for_user(user, plan, timestamp=timestamp)
    return {
        "plan": plan,
        "period": period,
        "resetAt": reset_at,
        "userLimit": user_limit_for_plan(plan),
        "repositoryLimit": repository_limit_for_plan(plan),
    }


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
    reset_at: int | None = None,
) -> dict[str, Any]:
    db.ensure_initialized()
    period = period or current_period()
    with closing(db.connect()) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            return _ensure_quota_bucket(connection, scope_type=scope_type, scope_id=scope_id, period=period, plan=plan, limit=limit, reset_at=reset_at)


def _ensure_quota_bucket(
    connection: sqlite3.Connection,
    *,
    scope_type: str,
    scope_id: str,
    period: str,
    plan: str,
    limit: int,
    reset_at: int | None = None,
) -> dict[str, Any]:
    bucket_id = db.quota_bucket_id(scope_type, scope_id, period, plan)
    limit = max(0, int(limit or 0))
    reset_at = non_negative_int(reset_at) or reset_at_for_period(period)
    connection.execute(
        """
        INSERT INTO quota_buckets (id, scope_type, scope_id, period, plan, quota_limit, used, reserved, reset_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, strftime('%s', 'now'), strftime('%s', 'now'))
        ON CONFLICT(scope_type, scope_id, period, plan) DO UPDATE SET
            quota_limit = excluded.quota_limit,
            reset_at = excluded.reset_at,
            updated_at = excluded.updated_at
        """,
        (bucket_id, scope_type, scope_id, period, plan, limit, reset_at),
    )
    row = connection.execute("SELECT * FROM quota_buckets WHERE id = ?", (bucket_id,)).fetchone()
    bucket = dict(row)
    used = non_negative_int(bucket.get("used"))
    reserved = non_negative_int(bucket.get("reserved"))
    if used != bucket.get("used") or reserved != bucket.get("reserved"):
        connection.execute(
            "UPDATE quota_buckets SET used = ?, reserved = ?, updated_at = strftime('%s', 'now') WHERE id = ?",
            (used, reserved, bucket_id),
        )
        bucket["used"] = used
        bucket["reserved"] = reserved
    return bucket


def quota_payload(bucket: dict[str, Any], *, scope: str) -> dict[str, Any]:
    used = non_negative_int(bucket.get("used"))
    reserved = non_negative_int(bucket.get("reserved"))
    limit = non_negative_int(bucket.get("quota_limit"))
    return {
        "scope": scope,
        "period": str(bucket.get("period") or current_period()),
        "plan": str(bucket.get("plan") or "free"),
        "used": used,
        "reserved": reserved,
        "limit": limit,
        "remaining": max(0, limit - used - reserved),
        "resetAt": non_negative_int(bucket.get("reset_at")),
        "bucketId": str(bucket.get("id") or ""),
    }


def quota_payload_for_user(user: dict[str, Any], *, timestamp: int | None = None) -> dict[str, Any]:
    entitlement = quota_entitlement_for_user(user, timestamp=timestamp)
    bucket = ensure_quota_bucket(
        scope_type="user",
        scope_id=str(user["id"]),
        period=entitlement["period"],
        plan=entitlement["plan"],
        limit=entitlement["userLimit"],
        reset_at=entitlement["resetAt"],
    )
    return quota_payload(bucket, scope="user")


def quota_payload_for_repository(repository: dict[str, Any], user: dict[str, Any] | None = None, *, timestamp: int | None = None) -> dict[str, Any]:
    entitlement = quota_entitlement_for_user(user, timestamp=timestamp)
    bucket = ensure_quota_bucket(
        scope_type="repository",
        scope_id=repository_quota_scope_id(repository),
        period=entitlement["period"],
        plan=entitlement["plan"],
        limit=entitlement["repositoryLimit"],
        reset_at=entitlement["resetAt"],
    )
    return quota_payload(bucket, scope="repository")


def quota_ledger_rows_for_user(
    user: dict[str, Any],
    *,
    scope_type: str = "user",
    limit: int = 100,
) -> list[dict[str, Any]]:
    db.ensure_initialized()
    user_id = str((user or {}).get("id") or "").strip()
    if not user_id:
        return []
    normalized_scope = str(scope_type or "user").strip().lower()
    if normalized_scope not in {"user", "repository"}:
        normalized_scope = "user"
    row_limit = non_negative_int(limit) or 100
    row_limit = min(200, max(1, row_limit))
    with closing(db.connect()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                q.id,
                q.repository_id,
                q.github_repo_id,
                q.scan_id,
                q.requested_by_user_id,
                q.request_id,
                q.bucket_id,
                q.delta,
                q.reason,
                q.created_at,
                b.scope_type,
                b.period,
                b.plan
            FROM quota_ledger q
            JOIN quota_buckets b ON b.id = q.bucket_id
            WHERE q.requested_by_user_id = ?
              AND b.scope_type = ?
            ORDER BY q.created_at DESC, q.id DESC
            LIMIT ?
            """,
            (user_id, normalized_scope, row_limit),
        ).fetchall()
        return [dict(row) for row in rows]


def _request_id_clause(request_id: str | None) -> tuple[str, list[object]]:
    if request_id:
        return "AND request_id = ?", [request_id]
    return "AND request_id IS NULL", []


def _quota_ledger_id(
    bucket_id: str,
    scan_id: str,
    requested_by_user_id: str,
    request_id: str | None,
    reason: str,
) -> str:
    return db.quota_ledger_id(bucket_id, scan_id, requested_by_user_id, request_id, reason)


def reserve_scan_quota(
    *,
    user: dict[str, Any],
    repository: dict[str, Any],
    requested_by_user_id: str,
    scan_id: str,
    request_id: str | None = None,
    timestamp: int | None = None,
) -> dict[str, Any]:
    db.ensure_initialized()
    entitlement = quota_entitlement_for_user(user, timestamp=timestamp)
    plan = entitlement["plan"]
    period = entitlement["period"]
    reset_at = entitlement["resetAt"]
    user_limit = entitlement["userLimit"]
    repository_limit = entitlement["repositoryLimit"]
    user_id = str(user["id"])
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
                      AND reason IN ('scan_reserved', 'scan_created', 'scan_consumed')
                      AND delta > 0
                    LIMIT 1
                    """,
                    (requested_by_user_id, request_id, repository_id),
                ).fetchone()
            user_bucket = _ensure_quota_bucket(
                connection,
                scope_type="user",
                scope_id=user_id,
                period=period,
                plan=plan,
                limit=user_limit,
                reset_at=reset_at,
            )
            repository_bucket = _ensure_quota_bucket(
                connection,
                scope_type="repository",
                scope_id=repository_scope_id,
                period=period,
                plan=plan,
                limit=repository_limit,
                reset_at=reset_at,
            )
            if existing_request:
                connection.commit()
                return {
                    "deduplicated": True,
                    "user": quota_payload(user_bucket, scope="user"),
                    "repository": quota_payload(repository_bucket, scope="repository"),
                    "bucketIds": {
                        "user": user_bucket["id"],
                        "repository": repository_bucket["id"],
                    },
                }

            repo_updated = connection.execute(
                """
                UPDATE quota_buckets
                SET reserved = reserved + 1, updated_at = strftime('%s', 'now')
                WHERE id = ? AND used + reserved < quota_limit
                """,
                (repository_bucket["id"],),
            ).rowcount
            if repo_updated != 1:
                connection.rollback()
                raise QuotaExceeded(
                    "QUOTA_EXCEEDED_REPOSITORY",
                    "This repository has used its scan quota for the current billing period.",
                    repo_id=repository_id,
                )

            user_updated = connection.execute(
                """
                UPDATE quota_buckets
                SET reserved = reserved + 1, updated_at = strftime('%s', 'now')
                WHERE id = ? AND used + reserved < quota_limit
                """,
                (user_bucket["id"],),
            ).rowcount
            if user_updated != 1:
                connection.rollback()
                raise QuotaExceeded(
                    "QUOTA_EXCEEDED_USER",
                    "Your account has used its scan quota for the current billing period.",
                    repo_id=repository_id,
                )

            for bucket_id in (user_bucket["id"], repository_bucket["id"]):
                connection.execute(
                    """
                    INSERT INTO quota_ledger (
                        id, repository_id, github_repo_id, scan_id,
                        requested_by_user_id, request_id, bucket_id, delta, reason, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'scan_reserved', strftime('%s', 'now'))
                    """,
                    (
                        _quota_ledger_id(bucket_id, scan_id, requested_by_user_id, request_id, "scan_reserved"),
                        repository_id,
                        github_repo_id,
                        scan_id,
                        requested_by_user_id,
                        request_id,
                        bucket_id,
                    ),
                )

            user_bucket = dict(connection.execute("SELECT * FROM quota_buckets WHERE id = ?", (user_bucket["id"],)).fetchone())
            repository_bucket = dict(connection.execute("SELECT * FROM quota_buckets WHERE id = ?", (repository_bucket["id"],)).fetchone())
            connection.commit()
            return {
                "deduplicated": False,
                "user": quota_payload(user_bucket, scope="user"),
                "repository": quota_payload(repository_bucket, scope="repository"),
                "bucketIds": {
                    "user": user_bucket["id"],
                    "repository": repository_bucket["id"],
                },
            }
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise


def consume_reserved_scan_quota(
    *,
    user: dict[str, Any],
    repository: dict[str, Any],
    requested_by_user_id: str,
    scan_id: str,
    request_id: str | None = None,
    timestamp: int | None = None,
) -> dict[str, Any]:
    db.ensure_initialized()
    requested_by_user_id = str(requested_by_user_id or "").strip()
    scan_id = str(scan_id or "").strip()
    request_id = str(request_id or "").strip() if request_id else None
    if not requested_by_user_id or not scan_id:
        return {"deduplicated": False, "consumed": False, "bucketIds": {}}
    request_clause, request_params = _request_id_clause(request_id)
    with closing(db.connect()) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        try:
            entitlement = quota_entitlement_for_user(user, timestamp=timestamp)
            user_bucket = _ensure_quota_bucket(
                connection,
                scope_type="user",
                scope_id=str(user["id"]),
                period=entitlement["period"],
                plan=entitlement["plan"],
                limit=entitlement["userLimit"],
                reset_at=entitlement["resetAt"],
            )
            repository_bucket = _ensure_quota_bucket(
                connection,
                scope_type="repository",
                scope_id=repository_quota_scope_id(repository),
                period=entitlement["period"],
                plan=entitlement["plan"],
                limit=entitlement["repositoryLimit"],
                reset_at=entitlement["resetAt"],
            )
            existing_consumed = connection.execute(
                f"""
                SELECT 1
                FROM quota_ledger
                WHERE scan_id = ?
                  AND requested_by_user_id = ?
                  {request_clause}
                  AND reason IN ('scan_created', 'scan_consumed')
                  AND delta > 0
                LIMIT 1
                """,
                [scan_id, requested_by_user_id, *request_params],
            ).fetchone()
            if existing_consumed:
                connection.commit()
                return {
                    "deduplicated": True,
                    "consumed": True,
                    "user": quota_payload(user_bucket, scope="user"),
                    "repository": quota_payload(repository_bucket, scope="repository"),
                    "bucketIds": {
                        "user": user_bucket["id"],
                        "repository": repository_bucket["id"],
                    },
                }
            existing_release = connection.execute(
                f"""
                SELECT 1
                FROM quota_ledger
                WHERE scan_id = ?
                  AND requested_by_user_id = ?
                  {request_clause}
                  AND reason = 'scan_reservation_released'
                  AND delta < 0
                LIMIT 1
                """,
                [scan_id, requested_by_user_id, *request_params],
            ).fetchone()
            if existing_release:
                connection.commit()
                return {
                    "deduplicated": True,
                    "consumed": False,
                    "released": True,
                    "user": quota_payload(user_bucket, scope="user"),
                    "repository": quota_payload(repository_bucket, scope="repository"),
                    "bucketIds": {
                        "user": user_bucket["id"],
                        "repository": repository_bucket["id"],
                    },
                }
            reservation_rows = connection.execute(
                f"""
                SELECT id, bucket_id, delta
                FROM quota_ledger
                WHERE scan_id = ?
                  AND requested_by_user_id = ?
                  {request_clause}
                  AND reason = 'scan_reserved'
                  AND delta > 0
                """,
                [scan_id, requested_by_user_id, *request_params],
            ).fetchall()
            if not reservation_rows:
                connection.rollback()
                return consume_scan_quota(
                    user=user,
                    repository=repository,
                    requested_by_user_id=requested_by_user_id,
                    scan_id=scan_id,
                    request_id=request_id,
                    timestamp=timestamp,
                )

            bucket_deltas: dict[str, int] = {}
            for row in reservation_rows:
                bucket_id = str(row["bucket_id"] or "")
                delta = non_negative_int(row["delta"])
                if bucket_id and delta:
                    bucket_deltas[bucket_id] = bucket_deltas.get(bucket_id, 0) + delta
            repository_id = str(repository["id"])
            github_repo_id = str(repository["github_repo_id"])
            for bucket_id, delta in bucket_deltas.items():
                connection.execute(
                    """
                    UPDATE quota_buckets
                    SET reserved = CASE WHEN reserved >= ? THEN reserved - ? ELSE 0 END,
                        used = used + ?,
                        updated_at = strftime('%s', 'now')
                    WHERE id = ?
                    """,
                    (delta, delta, delta, bucket_id),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO quota_ledger (
                        id, repository_id, github_repo_id, scan_id,
                        requested_by_user_id, request_id, bucket_id, delta, reason, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'scan_consumed', strftime('%s', 'now'))
                    """,
                    (
                        _quota_ledger_id(bucket_id, scan_id, requested_by_user_id, request_id, "scan_consumed"),
                        repository_id,
                        github_repo_id,
                        scan_id,
                        requested_by_user_id,
                        request_id,
                        bucket_id,
                        delta,
                    ),
                )

            user_bucket = dict(connection.execute("SELECT * FROM quota_buckets WHERE id = ?", (user_bucket["id"],)).fetchone())
            repository_bucket = dict(connection.execute("SELECT * FROM quota_buckets WHERE id = ?", (repository_bucket["id"],)).fetchone())
            connection.commit()
            return {
                "deduplicated": False,
                "consumed": True,
                "user": quota_payload(user_bucket, scope="user"),
                "repository": quota_payload(repository_bucket, scope="repository"),
                "bucketIds": {
                    "user": user_bucket["id"],
                    "repository": repository_bucket["id"],
                },
            }
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise


def release_scan_quota_reservation(
    *,
    scan_id: str,
    requested_by_user_id: str,
    request_id: str | None = None,
    record_ledger: bool = True,
) -> dict[str, int]:
    db.ensure_initialized()
    scan_id = str(scan_id or "").strip()
    requested_by_user_id = str(requested_by_user_id or "").strip()
    request_id = str(request_id or "").strip() if request_id else None
    if not scan_id or not requested_by_user_id:
        return {"ledgerRows": 0, "bucketRows": 0}
    request_clause, request_params = _request_id_clause(request_id)
    with closing(db.connect()) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing_consumed = connection.execute(
                f"""
                SELECT 1
                FROM quota_ledger
                WHERE scan_id = ?
                  AND requested_by_user_id = ?
                  {request_clause}
                  AND reason IN ('scan_created', 'scan_consumed')
                  AND delta > 0
                LIMIT 1
                """,
                [scan_id, requested_by_user_id, *request_params],
            ).fetchone()
            if existing_consumed:
                connection.commit()
                return {"ledgerRows": 0, "bucketRows": 0, "consumedRows": 1}
            existing_release = connection.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM quota_ledger
                WHERE scan_id = ?
                  AND requested_by_user_id = ?
                  {request_clause}
                  AND reason = 'scan_reservation_released'
                  AND delta < 0
                """,
                [scan_id, requested_by_user_id, *request_params],
            ).fetchone()
            if existing_release and non_negative_int(existing_release["count"]):
                connection.commit()
                return {"ledgerRows": 0, "bucketRows": 0, "releasedRows": non_negative_int(existing_release["count"])}
            rows = connection.execute(
                f"""
                SELECT q.id, q.repository_id, q.github_repo_id, q.bucket_id, q.delta
                FROM quota_ledger q
                WHERE q.scan_id = ?
                  AND q.requested_by_user_id = ?
                  {request_clause}
                  AND q.reason = 'scan_reserved'
                  AND q.delta > 0
                """,
                [scan_id, requested_by_user_id, *request_params],
            ).fetchall()
            if not rows:
                connection.commit()
                return {"ledgerRows": 0, "bucketRows": 0}
            bucket_deltas: dict[str, int] = {}
            for row in rows:
                bucket_id = str(row["bucket_id"] or "")
                delta = non_negative_int(row["delta"])
                if bucket_id and delta:
                    bucket_deltas[bucket_id] = bucket_deltas.get(bucket_id, 0) + delta
            bucket_rows = 0
            for bucket_id, delta in bucket_deltas.items():
                bucket_rows += connection.execute(
                    """
                    UPDATE quota_buckets
                    SET reserved = CASE WHEN reserved >= ? THEN reserved - ? ELSE 0 END,
                        updated_at = strftime('%s', 'now')
                    WHERE id = ?
                    """,
                    (delta, delta, bucket_id),
                ).rowcount
            if record_ledger:
                for row in rows:
                    bucket_id = str(row["bucket_id"] or "")
                    delta = non_negative_int(row["delta"])
                    if not bucket_id or not delta:
                        continue
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO quota_ledger (
                            id, repository_id, github_repo_id, scan_id,
                            requested_by_user_id, request_id, bucket_id, delta, reason, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'scan_reservation_released', strftime('%s', 'now'))
                        """,
                        (
                            _quota_ledger_id(bucket_id, scan_id, requested_by_user_id, request_id, "scan_reservation_released"),
                            row["repository_id"],
                            row["github_repo_id"],
                            scan_id,
                            requested_by_user_id,
                            request_id,
                            bucket_id,
                            -delta,
                        ),
                    )
            else:
                ledger_ids = [str(row["id"]) for row in rows if str(row["id"] or "")]
                if ledger_ids:
                    placeholders = ",".join("?" for _ in ledger_ids)
                    connection.execute(f"DELETE FROM quota_ledger WHERE id IN ({placeholders})", ledger_ids)
            connection.commit()
            return {"ledgerRows": len(rows) if record_ledger else 0, "bucketRows": bucket_rows}
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise


def consume_scan_quota(
    *,
    user: dict[str, Any],
    repository: dict[str, Any],
    requested_by_user_id: str,
    scan_id: str,
    request_id: str | None = None,
    timestamp: int | None = None,
) -> dict[str, Any]:
    db.ensure_initialized()
    entitlement = quota_entitlement_for_user(user, timestamp=timestamp)
    plan = entitlement["plan"]
    period = entitlement["period"]
    reset_at = entitlement["resetAt"]
    user_limit = entitlement["userLimit"]
    repository_limit = entitlement["repositoryLimit"]
    user_id = str(user["id"])
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
            user_bucket = _ensure_quota_bucket(
                connection,
                scope_type="user",
                scope_id=user_id,
                period=period,
                plan=plan,
                limit=user_limit,
                reset_at=reset_at,
            )
            repository_bucket = _ensure_quota_bucket(
                connection,
                scope_type="repository",
                scope_id=repository_scope_id,
                period=period,
                plan=plan,
                limit=repository_limit,
                reset_at=reset_at,
            )
            if existing_request:
                connection.commit()
                return {
                    "deduplicated": True,
                    "user": quota_payload(user_bucket, scope="user"),
                    "repository": quota_payload(repository_bucket, scope="repository"),
                    "bucketIds": {
                        "user": user_bucket["id"],
                        "repository": repository_bucket["id"],
                    },
                }

            repo_updated = connection.execute(
                """
                UPDATE quota_buckets
                SET used = used + 1, updated_at = strftime('%s', 'now')
                WHERE id = ? AND used + reserved < quota_limit
                """,
                (repository_bucket["id"],),
            ).rowcount
            if repo_updated != 1:
                connection.rollback()
                raise QuotaExceeded(
                    "QUOTA_EXCEEDED_REPOSITORY",
                    "This repository has used its scan quota for the current billing period.",
                    repo_id=repository_id,
                )

            user_updated = connection.execute(
                """
                UPDATE quota_buckets
                SET used = used + 1, updated_at = strftime('%s', 'now')
                WHERE id = ? AND used + reserved < quota_limit
                """,
                (user_bucket["id"],),
            ).rowcount
            if user_updated != 1:
                connection.rollback()
                raise QuotaExceeded(
                    "QUOTA_EXCEEDED_USER",
                    "Your account has used its scan quota for the current billing period.",
                    repo_id=repository_id,
                )

            for bucket_id in (user_bucket["id"], repository_bucket["id"]):
                connection.execute(
                    """
                    INSERT INTO quota_ledger (
                        id, repository_id, github_repo_id, scan_id,
                        requested_by_user_id, request_id, bucket_id, delta, reason, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'scan_created', strftime('%s', 'now'))
                    """,
                    (
                        db.quota_ledger_id(bucket_id, scan_id, requested_by_user_id, request_id),
                        repository_id,
                        github_repo_id,
                        scan_id,
                        requested_by_user_id,
                        request_id,
                        bucket_id,
                    ),
                )

            user_bucket = dict(connection.execute("SELECT * FROM quota_buckets WHERE id = ?", (user_bucket["id"],)).fetchone())
            repository_bucket = dict(connection.execute("SELECT * FROM quota_buckets WHERE id = ?", (repository_bucket["id"],)).fetchone())
            connection.commit()
            return {
                "deduplicated": False,
                "user": quota_payload(user_bucket, scope="user"),
                "repository": quota_payload(repository_bucket, scope="repository"),
                "bucketIds": {
                    "user": user_bucket["id"],
                    "repository": repository_bucket["id"],
                },
            }
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise


def rollback_scan_quota(
    *,
    scan_id: str,
    requested_by_user_id: str,
    request_id: str | None = None,
    match_request_id: bool = True,
) -> dict[str, int]:
    db.ensure_initialized()
    scan_id = str(scan_id or "").strip()
    requested_by_user_id = str(requested_by_user_id or "").strip()
    request_id = str(request_id or "").strip() if request_id else None
    if not scan_id or not requested_by_user_id:
        return {"ledgerRows": 0, "bucketRows": 0}
    request_clause = ""
    if match_request_id:
        request_clause = "AND request_id = ?" if request_id else "AND request_id IS NULL"
    params: list[object] = [scan_id, requested_by_user_id]
    if match_request_id and request_id:
        params.append(request_id)
    with closing(db.connect()) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = connection.execute(
                f"""
                SELECT id, bucket_id, delta
                FROM quota_ledger
                WHERE scan_id = ?
                  AND requested_by_user_id = ?
                  {request_clause}
                  AND reason IN ('scan_created', 'scan_consumed')
                  AND delta > 0
                """,
                params,
            ).fetchall()
            if not rows:
                connection.commit()
                return {"ledgerRows": 0, "bucketRows": 0}
            bucket_deltas: dict[str, int] = {}
            ledger_ids: list[str] = []
            for row in rows:
                bucket_id = str(row["bucket_id"] or "")
                delta = non_negative_int(row["delta"])
                if bucket_id and delta:
                    bucket_deltas[bucket_id] = bucket_deltas.get(bucket_id, 0) + delta
                ledger_ids.append(str(row["id"]))
            bucket_rows = 0
            for bucket_id, delta in bucket_deltas.items():
                bucket_rows += connection.execute(
                    """
                    UPDATE quota_buckets
                    SET used = CASE WHEN used >= ? THEN used - ? ELSE 0 END,
                        updated_at = strftime('%s', 'now')
                    WHERE id = ?
                    """,
                    (delta, delta, bucket_id),
                ).rowcount
            placeholders = ",".join("?" for _ in ledger_ids)
            connection.execute(f"DELETE FROM quota_ledger WHERE id IN ({placeholders})", ledger_ids)
            connection.commit()
            return {"ledgerRows": len(ledger_ids), "bucketRows": bucket_rows}
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
