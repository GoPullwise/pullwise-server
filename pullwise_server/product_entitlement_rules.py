from __future__ import annotations

from typing import Any

from . import account_cycle_rules as quota


PLAN_ENTITLEMENTS = {
    "free": {
        "activeRepositoryLimit": 1,
        "activeWatchLimit": 3,
        "monthlyProcessingLimit": 200,
    },
    "pro": {
        "activeRepositoryLimit": 5,
        "activeWatchLimit": 25,
        "monthlyProcessingLimit": 5_000,
    },
    "max": {
        "activeRepositoryLimit": 20,
        "activeWatchLimit": 100,
        "monthlyProcessingLimit": 25_000,
    },
}


def entitlements_for_user(user: dict[str, Any] | None, *, timestamp: int | None = None) -> dict:
    current = quota.current_timestamp(timestamp)
    plan = quota.effective_user_plan(user, timestamp=current)
    period, reset_at = quota.quota_cycle_for_user(user, plan, timestamp=current)
    return {
        "plan": plan,
        "period": period,
        "resetAt": reset_at,
        "entitlements": dict(PLAN_ENTITLEMENTS[plan]),
    }


def product_usage_payload_from_usage(
    user: dict[str, Any], usage: dict, runtime_used: int,
    *, timestamp: int | None = None,
) -> dict:
    entitlement = entitlements_for_user(user, timestamp=timestamp)
    processing_limit = entitlement["entitlements"]["monthlyProcessingLimit"]
    usage = {**usage, "limit": processing_limit,
             "remaining": max(0, processing_limit - usage["used"] - usage["reserved"]),
             "resetAt": entitlement["resetAt"]}
    return {
        "service": "github_followups",
        "plan": entitlement["plan"],
        "entitlements": entitlement["entitlements"],
        "usage": usage,
        "runtimeUsage": {
            "metric": "provider_attempts",
            "used": runtime_used,
            "limit": processing_limit * 3,
            "resetAt": entitlement["resetAt"],
        },
    }
