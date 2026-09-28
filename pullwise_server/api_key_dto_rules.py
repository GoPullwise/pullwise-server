"""Pure public projection of persisted API-key metadata; never return a hash."""
from __future__ import annotations

import json
import math
import re
from typing import Any


ALLOWED_SCOPES = frozenset({"profile:read", "projects:read", "projects:write",
    "categories:read", "categories:write", "expenses:read", "expenses:write",
    "reports:read", "suggestions:use"})
DEFAULT_SCOPES = ["profile:read", "projects:read", "categories:read",
                  "expenses:read", "reports:read"]


def _text(value: object) -> str:
    if not isinstance(value, str) or any(char in value for char in "\r\n\x00"):
        return ""
    return value.strip()



def _timestamp(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _scopes(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    elif not isinstance(value, list):
        return list(DEFAULT_SCOPES)
    if not isinstance(value, list):
        return []
    result = []
    for scope in value:
        if not isinstance(scope, str):
            continue
        normalized = scope.strip().lower()
        if normalized in ALLOWED_SCOPES and normalized not in result:
            result.append(normalized)
    return result


def _restrictions(value: object) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    if not isinstance(value, dict):
        return {}
    result = {"shared": value.get("shared") is True}
    values = value.get("projectIds")
    if isinstance(values, list):
        result["projectIds"] = list(dict.fromkeys(
            item for item in values if isinstance(item, str) and
            re.fullmatch(r"prj_[A-Za-z0-9_-]{1,100}", item)))
    return result


def requested_api_key_scopes(value: object, *, provided: bool) -> tuple[list[str], str | None]:
    if not provided or value is None:
        return list(DEFAULT_SCOPES), None
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        candidates = value
    else:
        return [], "API key scopes must be a string or a list of strings."
    scopes = []
    invalid = []
    for candidate in candidates:
        normalized = candidate.strip().lower()
        if normalized in ALLOWED_SCOPES:
            if normalized not in scopes:
                scopes.append(normalized)
        else:
            invalid.append(candidate)
    if invalid or not scopes:
        return [], "API key scopes must include only: " + ", ".join(sorted(ALLOWED_SCOPES)) + "."
    return scopes, None


def parse_api_key_restrictions(value: object) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ValueError("INVALID_RESTRICTION") from None
    if value is not None and (not isinstance(value, dict)
            or set(value) - {"projectIds", "shared"}):
        raise ValueError("INVALID_RESTRICTION")
    if isinstance(value, dict):
        if "shared" in value and type(value["shared"]) is not bool:
            raise ValueError("INVALID_RESTRICTION")
        if "projectIds" in value and (not isinstance(value["projectIds"], list)
                or len(value["projectIds"]) > 100
                or any(not isinstance(item, str) or not re.fullmatch(
                    r"prj_[A-Za-z0-9_-]{1,100}", item) for item in value["projectIds"])):
            raise ValueError("INVALID_RESTRICTION")
    return _restrictions(value if value is not None else {})


def api_key_public_payload(record: dict[str, Any], *, token: str | None = None) -> dict:
    payload = {"id": _text(record.get("id")),
        "name": _text(record.get("name")) or "API key",
        "userId": _text(record.get("user_id")),
        "prefix": _text(record.get("key_prefix")),
        "scopes": _scopes(record.get("scopes")),
        "createdAt": _timestamp(record.get("created_at")) or 0,
        "expiresAt": _timestamp(record.get("expires_at")),
        "lastUsedAt": _timestamp(record.get("last_used_at")),
        "revokedAt": _timestamp(record.get("revoked_at"))}
    restrictions = _restrictions(record.get("restrictions"))
    if restrictions:
        payload["restrictions"] = restrictions
    if token:
        payload["key"] = token
    return payload
