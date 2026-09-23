"""Session-only Billing account read from one D1 authorization snapshot."""
from __future__ import annotations

from typing import Any, Mapping

from .cloudflare_product_read import (
    ProductReadAuthError, _bearer, _cookie_sessions, _header,
    _principal, _resource_auth_snapshot, _usage_from_results,
    _usage_statements,
)
from .product_billing_projection import billing_account_dto
from .product_usage_events import usage_event_dto


def billing_statements(binding: Any, user: dict, now: int):
    period, usage_statements = _usage_statements(binding, user, now)
    events = binding.prepare("""SELECT reservation_id,module,period,finished_at
        FROM processing_usage_ledger WHERE billing_owner_id=?
          AND state='consumed' AND finished_at IS NOT NULL
        ORDER BY finished_at DESC,reservation_id DESC LIMIT 20""").bind(user["id"])
    return period, [*usage_statements, events]


def billing_account_from_parts(user: dict, period: str,
                               parts: list, now: int) -> dict:
    product = _usage_from_results(user, period, parts[:3], now)
    activity = [usage_event_dto(row) for row in parts[3].results]
    return billing_account_dto(user, product, activity)


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
