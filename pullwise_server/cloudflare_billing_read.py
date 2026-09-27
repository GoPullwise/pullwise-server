"""Session-only Billing account read from one D1 authorization snapshot."""
from __future__ import annotations

from typing import Any, Mapping

from .cloudflare_principal import (
    ProductReadAuthError, _bearer, _cookie_sessions, _header,
    _principal, _resource_auth_snapshot,
)
from .billing_projection import billing_account_dto
from .account_cycle_rules import effective_user_plan


def billing_statements(binding: Any, user: dict, now: int):
    return None, []


def billing_account_from_parts(user: dict, period: str,
                               parts: list, now: int) -> dict:
    product = {"plan": effective_user_plan(user, timestamp=now),
        "entitlements": None, "usage": None, "runtimeUsage": None}
    return billing_account_dto(user, product, [])


async def read_billing(*, binding: Any, headers: Mapping[str, object],
                       now: int) -> tuple[int, dict]:
    if (_bearer(headers) or _header(headers, "Authorization")
            or _header(headers, "X-Pullwise-Api-Key")
            or not _cookie_sessions(headers)):
        return 401, {"error": {"code": "UNAUTHENTICATED"}}
    try:
        user, _ = await _principal(binding, headers, scope="profile:read", now=now)
    except ProductReadAuthError as failure:
        return failure.status, {"error": {"code": failure.code}}
    auth, validate = _resource_auth_snapshot(binding, headers, user, {}, now,
        "profile:read")
    period, statements = billing_statements(binding, user, now)
    result = await binding.batch([*auth, *statements])
    try:
        validate([part.results for part in result[:len(auth)]])
    except ProductReadAuthError as failure:
        return failure.status, {"error": {"code": failure.code}}
    return 200, {"page": {"id": "billing",
        "subscriptionAction": {"label": "View pricing", "href": "/pricing"},
        "checkoutAction": None},
        "account": billing_account_from_parts(user, period,
            result[len(auth):], now)}
