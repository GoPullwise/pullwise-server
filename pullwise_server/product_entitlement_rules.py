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
