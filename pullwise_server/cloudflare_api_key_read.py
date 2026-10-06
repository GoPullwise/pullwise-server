"""Session-only API-key metadata read over one D1 authorization snapshot."""
from __future__ import annotations

from typing import Any, Mapping

from .api_key_dto_rules import api_key_public_payload
from .cloudflare_principal import (
    PrincipalAuthError, _bearer, _cookie_sessions, _header,
    _principal, _resource_auth_snapshot,
)
from .cloudflare_ledger_auth import ledger_principal


async def list_api_keys(*, binding: Any, headers: Mapping[str, object],
                        now: int, workspace_id: str | None = None) -> tuple[int, dict]:
    if (_bearer(headers) or _header(headers, "Authorization")
            or _header(headers, "X-Pullwise-Api-Key")
            or not _cookie_sessions(headers)):
        return 401, {"error": {"code": "UNAUTHENTICATED"}}
    if workspace_id is not None:
        if _header(headers, "X-Pullwise-Workspace") not in {"", workspace_id}:
            return 422, {"error": {"code": "INVALID_INPUT"}}
        headers = {**headers, "X-Pullwise-Workspace": workspace_id}
    selected = _header(headers, "X-Pullwise-Workspace")
    try:
        user, _, auth, validate = await ledger_principal(binding=binding,
            headers=headers, scope="profile:read", now=now)
    except PrincipalAuthError as failure:
        return failure.status, {"error": {"code": failure.code}}
    result = await binding.batch([*auth, binding.prepare("""SELECT id,user_id,name,
        key_prefix,scopes,expires_at,restrictions,created_at,last_used_at,revoked_at
        FROM api_keys WHERE user_id=? AND revoked_at IS NULL
        ORDER BY created_at DESC,id DESC""").bind(user["_actor"]["id"])])
    try:
        validate([part.results for part in result[:len(auth)]])
    except PrincipalAuthError as failure:
        return failure.status, {"error": {"code": failure.code}}
    items = [api_key_public_payload(row) for row in result[-1].results]
    if selected:
        items = [item for item in items if item.get("restrictions", {}).get("workspaceId", user["_actor"]["id"]) == selected]
    return 200, {"items": items, "apiKeys": items}
