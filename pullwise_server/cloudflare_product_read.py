"""Authenticated product-v1 profile and usage reads over async D1."""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from typing import Any, Mapping

from .account_cycle_rules import period_start_for_key
from .product_entitlement_rules import (
    entitlements_for_user,
    product_usage_payload_from_usage,
)
from .product_dto_rules import watch_dto

SESSION_COOKIE = "pw_session"
API_KEY_PREFIX = "pwk_"


class ProductReadAuthError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code, self.message = status, code, message


def _header(headers: Mapping[str, object], name: str) -> str:
    expected = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == expected:
            return str(value).strip()
    return ""


def _bearer(headers: Mapping[str, object]) -> str:
    parts = _header(headers, "Authorization").split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1] if not any(char in parts[1] for char in "\r\n") else ""
    return ""


def _cookie_sessions(headers: Mapping[str, object]) -> list[str]:
    sessions = []
    for entry in _header(headers, "Cookie").split(";"):
        name, separator, value = entry.partition("=")
        if separator and name.strip() == SESSION_COOKIE:
            candidate = value.strip().strip('"')
            if candidate:
                sessions.append(candidate)
    return sessions


def _timestamp(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


async def _user(binding: Any, owner_id: str) -> dict | None:
    row = await binding.prepare("""SELECT u.value AS snapshot FROM app_state a,
        json_each(a.payload) u WHERE a.name='users' AND u.key=?""").bind(owner_id).first()
    if not row:
        return None
    user = json.loads(row["snapshot"])
    return user if isinstance(user, dict) and user.get("id") == owner_id else None


async def _principal(binding: Any, headers: Mapping[str, object],
                     *, scope: str, now: int) -> tuple[dict, dict]:
    bearer = _bearer(headers)
    header_key = _header(headers, "X-Pullwise-Api-Key")
    cookie_sessions = _cookie_sessions(headers)
    session_ids = ([bearer] if bearer and not bearer.startswith(API_KEY_PREFIX)
                   else cookie_sessions)
    api_token = bearer if bearer.startswith(API_KEY_PREFIX) else ""
    if header_key.startswith(API_KEY_PREFIX):
        if api_token and header_key != api_token:
            raise ProductReadAuthError(400, "AMBIGUOUS_AUTH", "Use one API key.")
        api_token = header_key
    if api_token and (cookie_sessions or (bearer and not bearer.startswith(API_KEY_PREFIX))):
        raise ProductReadAuthError(400, "AMBIGUOUS_AUTH",
            "Use either a session or an API key, not both.")
    if api_token:
        key_hash = hashlib.sha256(api_token.encode("utf-8")).hexdigest()
        record = await binding.prepare("""SELECT user_id,scopes,expires_at,restrictions
            FROM api_keys WHERE key_hash=? AND revoked_at IS NULL""").bind(key_hash).first()
        expires_at = _timestamp(record["expires_at"]) if record else None
        if (not record or (record["expires_at"] is not None
                           and (expires_at is None or expires_at < now))):
            raise ProductReadAuthError(401, "UNAUTHENTICATED", "A session or API key is required.")
        try:
            scopes = json.loads(record["scopes"])
            restrictions = json.loads(record["restrictions"])
        except (TypeError, ValueError):
            raise ProductReadAuthError(403, "INSUFFICIENT_SCOPE", "API key scope is invalid.") from None
        if not isinstance(scopes, list) or scope not in scopes:
            raise ProductReadAuthError(403, "INSUFFICIENT_SCOPE", f"API key scope {scope} is required.")
        if not isinstance(restrictions, dict) or restrictions.get("kind") == "audit_bundle":
            raise ProductReadAuthError(403, "INSUFFICIENT_SCOPE", "API key restriction forbids this read.")
        user = await _user(binding, str(record["user_id"]))
        if user is None:
            raise ProductReadAuthError(401, "UNAUTHENTICATED", "A session or API key is required.")
        return user, restrictions
    if session_ids:
        row = await binding.prepare("SELECT payload FROM app_state WHERE name='sessions'").first()
        sessions = json.loads(row["payload"]) if row else None
        if isinstance(sessions, dict):
            for session_id in session_ids:
                session = sessions.get(session_id)
                if not isinstance(session, dict):
                    continue
                expires_at = _timestamp(session.get("expiresAt"))
                owner_id = session.get("userId")
                if expires_at is None or expires_at < now or not isinstance(owner_id, str):
                    continue
                user = await _user(binding, owner_id)
                if (user is not None and not (
                        "github" in (user.get("providers") or [])
                        and not user.get("githubAccessToken"))):
                    return user, {}
    raise ProductReadAuthError(401, "UNAUTHENTICATED", "A session or API key is required.")


async def _usage(binding: Any, user: dict, *, now: int) -> dict:
    entitlement = entitlements_for_user(user, timestamp=now)
    owner_id, period = user["id"], entitlement["period"]
    bucket = await binding.prepare("""SELECT used,reserved,limit_value
        FROM processing_usage_buckets WHERE billing_owner_id=? AND period=?
        AND metric='intelligent_processing'""").bind(owner_id, period).first()
    counts = await binding.prepare("""SELECT module,COUNT(*) AS count
        FROM processing_usage_ledger WHERE billing_owner_id=? AND period=?
        AND state='consumed' GROUP BY module""").bind(owner_id, period).all()
    by_module = {"pr": 0, "ci": 0, "updates": 0}
    for row in counts.results:
        if row["module"] in by_module:
            by_module[row["module"]] = int(row["count"])
    attempts = await binding.prepare("""SELECT COUNT(*) AS count FROM provider_attempts
        WHERE billing_owner_id=? AND occurred_at>=? AND occurred_at<?""").bind(
            owner_id, period_start_for_key(period, entitlement["resetAt"]),
            entitlement["resetAt"]).first()
    usage = {"metric": "intelligent_processing", "period": period,
        "used": int(bucket["used"]) if bucket else 0,
        "reserved": int(bucket["reserved"]) if bucket else 0,
        "limit": int(bucket["limit_value"]) if bucket else 0,
        "byModule": by_module}
    return product_usage_payload_from_usage(user, usage,
        int(attempts["count"]) if attempts else 0, timestamp=now)


async def _watches(binding: Any, user: dict, restrictions: dict,
                   headers: Mapping[str, object]) -> dict:
    rows = await binding.prepare("""SELECT * FROM update_watches
        WHERE billing_owner_id=? AND archived_at IS NULL
        ORDER BY created_at,id""").bind(user["id"]).all()
    items = [watch_dto(row) for row in rows.results]
    if "watchIds" in restrictions:
        ids = restrictions["watchIds"]
        allowed = set(ids) if isinstance(ids, list) and all(
            isinstance(value, str) for value in ids) else set()
        items = [item for item in items if item["id"] in allowed]
    elif restrictions:
        items = []
    return {"items": items, "nextCursor": None, "hasMore": False,
            "requestId": _header(headers, "X-Request-Id") or f"req_{uuid.uuid4().hex}"}


async def read_product(*, binding: Any, path: str, headers: Mapping[str, object],
                       now: int) -> tuple[int, dict]:
    if path not in {"/api/v1/me", "/api/v1/usage", "/api/v1/watches"}:
        return 404, {"error": {"code": "NOT_FOUND"}}
    scope = ("profile:read" if path.endswith("/me") else "usage:read"
             if path.endswith("/usage") else "watches:read")
    try:
        user, restrictions = await _principal(binding, headers, scope=scope, now=now)
    except ProductReadAuthError as error:
        return error.status, {"error": {"code": error.code,
            "message": error.message, "retryable": False},
            "requestId": f"req_{uuid.uuid4().hex}"}
    if path.endswith("/me"):
        return 200, {"id": user["id"], "name": user.get("name") or "",
                     "email": user.get("email") or "",
                     "modules": ["pr", "ci", "updates"]}
    if path.endswith("/watches"):
        return 200, await _watches(binding, user, restrictions, headers)
    return 200, await _usage(binding, user, now=now)
