"""Current ledger principal over the shared Cookie/Bearer authorization fence."""
from __future__ import annotations

import json
from typing import Any, Mapping

from .api_key_dto_rules import ALLOWED_SCOPES
from .cloudflare_ledger_auth import ledger_principal
from .cloudflare_principal import PrincipalAuthError, _bearer, _header


async def read_ledger_me(*, binding: Any, headers: Mapping[str, object], now: int) -> tuple[int, dict]:
    try:
        user, _, commands, validate = await ledger_principal(
            binding=binding, headers=headers, scope="profile:read", now=now)
        parts = await binding.batch(commands)
        validate([part.results for part in parts])
    except PrincipalAuthError as failure:
        return failure.status, {"error": {"code": failure.code}}
    key = _bearer(headers).startswith("pwk_") or bool(_header(headers, "X-Pullwise-Api-Key"))
    scopes = json.loads(parts[0].results[0]["scopes"]) if key else sorted(ALLOWED_SCOPES)
    billing = user.get("billing") if isinstance(user.get("billing"), dict) else {}
    return 200, {"id": user["id"], "scopes": scopes,
                 "entitlements": {"plan": billing.get("plan") or "free"}}
