"""Pure public projection of persisted API-key metadata; never return a hash."""
from __future__ import annotations

import json
import math
from typing import Any


ALLOWED_SCOPES = frozenset({"profile:read", "repositories:read",
    "repositories:manage", "items:read", "items:write", "sync:write",
    "watches:read", "watches:write", "usage:read", "scans:read",
    "scans:write", "quota:read"})
DEFAULT_SCOPES = ["profile:read", "repositories:read", "items:read",
                  "watches:read", "usage:read"]


def _text(value: object) -> str:
    if not isinstance(value, str) or any(char in value for char in "\r\n\x00"):
        return ""
    return value.strip()


def _access_text(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return _text(value)


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
    kind = _text(value.get("kind") or value.get("purpose")).replace("-", "_")
    if kind == "audit_bundle":
        result = {"kind": "audit_bundle"}
        scan_id = _text(value.get("scanId") or value.get("scan_id"))
        repo_id = _access_text(value.get("repoId") or value.get("repo_id"))
        if scan_id:
            result["scanId"] = scan_id
        if repo_id:
            result["repoId"] = repo_id
        return result
    result = {}
    for key in ("repositoryIds", "watchIds"):
        values = value.get(key)
        if not isinstance(values, list):
            continue
        normalized = []
        for item in values:
            text = _access_text(item)
            if text and text not in normalized:
                normalized.append(text)
        result[key] = normalized
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
    return _restrictions(value)


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
