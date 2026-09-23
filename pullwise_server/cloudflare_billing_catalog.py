"""Read a fresh trusted public Creem catalog projection from D1."""
from __future__ import annotations

from typing import Any, Mapping

from .cloudflare_billing_read import billing_account_from_parts, billing_statements
from .cloudflare_product_read import (
    ProductReadAuthError, _cookie_sessions, _header, _principal,
    _resource_auth_snapshot,
)
from .product_public_catalog_rules import catalog_payload as _catalog_payload


_CATALOG_SQL = """SELECT payload_json,expires_at,source_revision
    FROM billing_public_catalog WHERE id=1"""


async def read_public_plan(*, binding: Any, headers: Mapping[str, object],
                           now: int) -> tuple[int, dict]:
    public_only = bool(_header(headers, "Authorization")
        or _header(headers, "X-Pullwise-Api-Key")
        or not _cookie_sessions(headers))
    user = None
    if not public_only:
        try:
            user, _ = await _principal(binding, headers, scope="profile:read", now=now)
        except ProductReadAuthError:
            user = None
    if user is None:
        result = await binding.batch([binding.prepare(_CATALOG_SQL)])
        catalog = _catalog_payload(result[0].results, now)
        return ((200, catalog) if catalog else
                (503, {"error": {"code": "BILLING_CATALOG_UNAVAILABLE"}}))
    auth, validate = _resource_auth_snapshot(binding, headers, user, {}, now,
        "profile:read")
    period, statements = billing_statements(binding, user, now)
    result = await binding.batch([*auth, *statements,
        binding.prepare(_CATALOG_SQL)])
    catalog = _catalog_payload(result[-1].results, now)
    if catalog is None:
        return 503, {"error": {"code": "BILLING_CATALOG_UNAVAILABLE"}}
    try:
        validate([part.results for part in result[:len(auth)]])
    except ProductReadAuthError:
        return 200, catalog
    catalog["account"] = billing_account_from_parts(user, period,
        result[len(auth):-1], now)
    return 200, catalog
