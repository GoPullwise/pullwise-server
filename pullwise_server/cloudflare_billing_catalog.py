"""Read a fresh trusted public Creem catalog projection from D1."""
from __future__ import annotations

from typing import Any, Mapping

from .billing_projection import billing_account_dto
from .account_cycle_rules import effective_user_plan
from .cloudflare_principal import (
    PrincipalAuthError, _cookie_sessions, _header, _principal,
    _resource_auth_snapshot,
)
from .billing_catalog_rules import catalog_payload as _catalog_payload


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
        except PrincipalAuthError:
            user = None
    if user is None:
        result = await binding.batch([binding.prepare(_CATALOG_SQL)])
        catalog = _catalog_payload(result[0].results, now, policy=getattr(binding, "plan_policy", None),
                                   jev_available=getattr(binding, "jev_available", False))
        return ((200, catalog) if catalog else
                (503, {"error": {"code": "BILLING_CATALOG_UNAVAILABLE"}}))
    auth, validate = _resource_auth_snapshot(binding, headers, user, {}, now,
        "profile:read")
    result = await binding.batch([*auth,
        binding.prepare(_CATALOG_SQL)])
    catalog = _catalog_payload(result[-1].results, now, policy=getattr(binding, "plan_policy", None),
                               jev_available=getattr(binding, "jev_available", False))
    if catalog is None:
        return 503, {"error": {"code": "BILLING_CATALOG_UNAVAILABLE"}}
    try:
        validate([part.results for part in result[:len(auth)]])
    except PrincipalAuthError:
        return 200, catalog
    catalog["account"] = billing_account_dto(user, effective_user_plan(user, timestamp=now))
    return 200, catalog
