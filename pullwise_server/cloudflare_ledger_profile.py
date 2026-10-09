"""Current ledger principal over the shared Cookie/Bearer authorization fence."""
from __future__ import annotations

import json
from typing import Any, Mapping

from .api_key_dto_rules import ALLOWED_SCOPES
from .cloudflare_ledger_auth import ledger_principal, ROLE_SCOPES
from .cloudflare_principal import PrincipalAuthError, _bearer, _header
from .cloudflare_plan_limits import capacity_usage_payload, capacity_usage_statement
from .ledger_plan_policy import entitlements


async def read_ledger_me(*, binding: Any, headers: Mapping[str, object], now: int) -> tuple[int, dict]:
    try:
        user, _, commands, validate = await ledger_principal(
            binding=binding, headers=headers, scope="profile:read", now=now)
        parts = await binding.batch([*commands, capacity_usage_statement(binding, user["id"])])
        validate([part.results for part in parts[:len(commands)]])
    except PrincipalAuthError as failure:
        return failure.status, {"error": {"code": failure.code}}
    key = _bearer(headers).startswith("pwk_") or bool(_header(headers, "X-Pullwise-Api-Key"))
    scopes = json.loads(parts[0].results[0]["scopes"]) if key else sorted(ALLOWED_SCOPES)
    workspace = user["_workspace"]
    scopes = [value for value in scopes if value in ROLE_SCOPES[workspace["role"]]]
    return 200, {"id": user["_actor"]["id"], "workspace": workspace, "scopes": scopes,
                 "ledgerUsage": capacity_usage_payload(user, parts[-1].results, now=now,
                                                       policy=getattr(binding, "plan_policy", None)),
                 "entitlements": entitlements(user, now=now, policy=getattr(binding, "plan_policy", None),
                                               jev_available=getattr(binding, "jev_available", False))}
