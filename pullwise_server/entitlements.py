from __future__ import annotations

from typing import Any

from . import quota
from .product_store import ProductStore
from .product_entitlement_rules import (
    PLAN_ENTITLEMENTS, entitlements_for_user, product_usage_payload_from_usage,
)
from .account_cycle_rules import period_start_for_key as _period_start


def product_usage_payload(
    store: ProductStore,
    user: dict[str, Any],
    *,
    timestamp: int | None = None,
) -> dict:
    entitlement = entitlements_for_user(user, timestamp=timestamp)
    usage = store.processing_usage(
        billing_owner_id=user["id"],
        period=entitlement["period"],
    )
    runtime_used = store.count_provider_attempts(
        billing_owner_id=user["id"],
        started_at=_period_start(entitlement["period"], entitlement["resetAt"]),
        ended_at=entitlement["resetAt"],
    )
    return product_usage_payload_from_usage(user, usage, runtime_used,
        timestamp=timestamp)
