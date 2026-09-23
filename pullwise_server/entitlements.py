from __future__ import annotations

from typing import Any

from . import quota
from .product_store import ProductStore
from .product_entitlement_rules import PLAN_ENTITLEMENTS, entitlements_for_user
from .account_cycle_rules import period_start_for_key as _period_start


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
