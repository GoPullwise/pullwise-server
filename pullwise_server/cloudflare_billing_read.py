"""Session-only Billing account read from one D1 authorization snapshot."""
from __future__ import annotations

from typing import Any, Mapping

from .cloudflare_principal import (
    PrincipalAuthError, _bearer, _cookie_sessions, _header,
    _principal, _resource_auth_snapshot,
)
from .billing_projection import billing_account_dto
from .account_cycle_rules import effective_user_plan






async def read_billing(*, binding: Any, headers: Mapping[str, object],
                       now: int) -> tuple[int, dict]:
    if (_bearer(headers) or _header(headers, "Authorization")
            or _header(headers, "X-Pullwise-Api-Key")
            or not _cookie_sessions(headers)):
        return 401, {"error": {"code": "UNAUTHENTICATED"}}
    try:
        user, _ = await _principal(binding, headers, scope="profile:read", now=now)
    except PrincipalAuthError as failure:
        return failure.status, {"error": {"code": failure.code}}
    auth, validate = _resource_auth_snapshot(binding, headers, user, {}, now,
        "profile:read")
    result = await binding.batch(auth)
    try:
        validate([part.results for part in result[:len(auth)]])
    except PrincipalAuthError as failure:
        return failure.status, {"error": {"code": failure.code}}
    return 200, {"page": {"id": "billing",
        "subscriptionAction": {"label": "View pricing", "href": "/pricing"},
        "checkoutAction": None},
        "account": billing_account_dto(user, effective_user_plan(user, timestamp=now))}
