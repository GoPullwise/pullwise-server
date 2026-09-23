from __future__ import annotations

import calendar
import time
from typing import Any

from . import quota
from .product_store import ProductStore
from .product_entitlement_rules import PLAN_ENTITLEMENTS, entitlements_for_user


def _period_start(period: str, reset_at: int) -> int:
    if period.startswith("cycle:"):
        try:
            return max(0, int(period.removeprefix("cycle:")))
        except ValueError:
            return max(0, reset_at - 31 * 24 * 60 * 60)
    try:
        year_text, month_text = period.split("-", 1)
        return calendar.timegm((int(year_text), int(month_text), 1, 0, 0, 0))
    except (TypeError, ValueError):
        current = time.gmtime()
        return calendar.timegm((current.tm_year, current.tm_mon, 1, 0, 0, 0))


def product_usage_payload(
    store: ProductStore,
    user: dict[str, Any],
    *,
    timestamp: int | None = None,
) -> dict:
    entitlement = entitlements_for_user(user, timestamp=timestamp)
    processing_limit = entitlement["entitlements"]["monthlyProcessingLimit"]
    usage = store.processing_usage(
        billing_owner_id=user["id"],
        period=entitlement["period"],
    )
    usage["limit"] = processing_limit
    usage["remaining"] = max(0, processing_limit - usage["used"] - usage["reserved"])
    usage["resetAt"] = entitlement["resetAt"]
    runtime_limit = processing_limit * 3
    runtime_used = store.count_provider_attempts(
        billing_owner_id=user["id"],
        started_at=_period_start(entitlement["period"], entitlement["resetAt"]),
        ended_at=entitlement["resetAt"],
    )
    return {
        "service": "github_followups",
        "plan": entitlement["plan"],
        "entitlements": entitlement["entitlements"],
        "usage": usage,
        "runtimeUsage": {
            "metric": "provider_attempts",
            "used": runtime_used,
            "limit": runtime_limit,
            "resetAt": entitlement["resetAt"],
        },
    }
