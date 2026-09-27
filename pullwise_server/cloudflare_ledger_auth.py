"""Ledger scope and target authorization shared by future D1 routes."""
from __future__ import annotations

from typing import Any, Mapping

from .api_key_dto_rules import parse_api_key_restrictions
from .cloudflare_product_read import (
    ProductReadAuthError, _bearer, _header, _principal, _resource_auth_snapshot,
)


def target_allowed(restrictions: dict, kind: str, project_id: str | None) -> bool:
    """Project allowlists never grant shared-pool access."""
    if kind == "shared":
        return project_id is None and restrictions.get("shared") is True
    if kind != "project" or not isinstance(project_id, str) or not project_id:
        return False
    project_ids = restrictions.get("projectIds")
    return project_ids is None or project_id in project_ids


async def ledger_principal(*, binding: Any, headers: Mapping[str, object],
                           scope: str, now: int, target_kind: str | None = None,
                           project_id: str | None = None, proof: dict | None = None):
    """Return owner and same-snapshot recheck commands for a ledger operation.

    Resource SQL must be appended to returned statements in one D1 batch,
    followed by validate(...). Cookie owners have all ledger targets.
    """
    user, raw_restrictions = await _principal(binding, headers, scope=scope, now=now)
    token = _bearer(headers)
    key_token = token.startswith("pwk_") or bool(_header(headers, "X-Pullwise-Api-Key"))
    if key_token:
        try:
            restrictions = parse_api_key_restrictions(raw_restrictions)
        except ValueError:
            raise ProductReadAuthError(403, "INSUFFICIENT_SCOPE", "Invalid key restriction") from None
        if restrictions != raw_restrictions:
            raise ProductReadAuthError(403, "INSUFFICIENT_SCOPE", "Invalid key restriction")
        if target_kind is not None and not target_allowed(restrictions, target_kind, project_id):
            raise ProductReadAuthError(403, "TARGET_FORBIDDEN", "Target is outside API key restriction")
    else:
        restrictions = {}
    statements, validate = _resource_auth_snapshot(
        binding, headers, user, restrictions, now, scope, proof)
    return user, (restrictions if key_token else {"shared": True}), statements, validate
