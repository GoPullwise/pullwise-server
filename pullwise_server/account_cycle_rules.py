from __future__ import annotations

import calendar
import datetime
import math
import time
from typing import Any


PAID_PLAN_IDS = {"pro", "max"}
PLAN_IDS = {"free", *PAID_PLAN_IDS}


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
