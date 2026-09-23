"""Session-only API-key metadata read over one D1 authorization snapshot."""
from __future__ import annotations

from typing import Any, Mapping

from .api_key_dto_rules import api_key_public_payload
from .cloudflare_product_read import (
    ProductReadAuthError, _bearer, _cookie_sessions, _header,
    _principal, _resource_auth_snapshot,
)


async def list_api_keys(*, binding: Any, headers: Mapping[str, object],
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
    result = await binding.batch([*auth, binding.prepare("""SELECT id,user_id,name,
        key_prefix,scopes,expires_at,restrictions,created_at,last_used_at,revoked_at
        FROM api_keys WHERE user_id=? AND revoked_at IS NULL
        ORDER BY created_at DESC,id DESC""").bind(user["id"])])
    try:
        validate([part.results for part in result[:len(auth)]])
    except ProductReadAuthError as failure:
        return failure.status, {"error": {"code": failure.code}}
    items = [api_key_public_payload(row) for row in result[-1].results]
    return 200, {"items": items, "apiKeys": items}
