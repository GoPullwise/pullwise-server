"""Guard session-only API-key revocation in a single D1 write batch."""
from __future__ import annotations

import hashlib
import base64
import json
import secrets
import uuid
from typing import Any, Mapping

from .api_key_dto_rules import (
    api_key_public_payload, parse_api_key_restrictions,
    requested_api_key_scopes, _text, _timestamp,
)

from .cloudflare_product_read import (
    ProductReadAuthError, _bearer, _cookie_sessions, _header,
    _principal, _resource_auth_snapshot,
)


def _new_api_token() -> str:
    try:
        import js
    except ModuleNotFoundError:
        random_bytes = secrets.token_bytes(32)
    else:
        view = js.Uint8Array.new(32)
        js.crypto.getRandomValues(view)
        random_bytes = bytes(int(view[index]) for index in range(32))
    return "pwk_" + base64.urlsafe_b64encode(random_bytes).decode().rstrip("=")


async def revoke_api_key(*, binding: Any, key_id: str,
                         headers: Mapping[str, object], now: int) -> tuple[int, dict]:
    if (_bearer(headers) or _header(headers, "Authorization")
            or _header(headers, "X-Pullwise-Api-Key")
            or not _cookie_sessions(headers)):
        return 401, {"error": {"code": "UNAUTHENTICATED"}}
    try:
        user, _ = await _principal(binding, headers, scope="profile:read", now=now)
    except ProductReadAuthError as failure:
        return failure.status, {"error": {"code": failure.code}}
    proof: dict = {}
    auth, validate = _resource_auth_snapshot(binding, headers, user, {}, now,
        "profile:read", proof)
    result = await binding.batch([*auth, binding.prepare("""SELECT id FROM api_keys
        WHERE id=? AND user_id=? AND revoked_at IS NULL""").bind(key_id, user["id"])])
    try:
        validate([part.results for part in result[:len(auth)]])
    except ProductReadAuthError as failure:
        return failure.status, {"error": {"code": failure.code}}
    if not result[-1].results:
        return 404, {"error": {"code": "NOT_FOUND"}}
    statements = [
        binding.prepare("""UPDATE api_keys SET revoked_at=? WHERE id=? AND user_id=?
            AND revoked_at IS NULL
            AND EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u
                WHERE a.name='users' AND u.key=? AND u.value=?)
            AND EXISTS(SELECT 1 FROM app_state WHERE name='sessions' AND payload=?)""").bind(
                now, key_id, user["id"], user["id"], proof["user"], proof["sessions"]),
        binding.prepare("""INSERT INTO d1_command_guard(ok)
            VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"""),
        binding.prepare("DELETE FROM d1_command_guard"),
    ]
    try:
        await binding.batch(statements)
    except Exception:
        return 409, {"error": {"code": "AUTHORIZATION_CHANGED"}}
    return 200, {"ok": True, "id": key_id, "revoked": True}


async def create_api_key(*, binding: Any, headers: Mapping[str, object],
                         body: object, now: int) -> tuple[int, dict]:
    if (_bearer(headers) or _header(headers, "Authorization")
            or _header(headers, "X-Pullwise-Api-Key")
            or not _cookie_sessions(headers)):
        return 401, {"error": {"code": "UNAUTHENTICATED"}}
    if not isinstance(body, dict):
        return 400, {"error": {"code": "INVALID_REQUEST"}}
    scopes, scope_error = requested_api_key_scopes(body.get("scopes"),
        provided="scopes" in body)
    if scope_error:
        return 400, {"error": {"code": "INVALID_SCOPE", "message": scope_error}}
    restrictions = parse_api_key_restrictions(body.get("restrictions"))
    if restrictions.get("kind") == "audit_bundle":
        return 400, {"error": {"code": "INVALID_RESTRICTION"}}
    expires_at = _timestamp(body.get("expiresAt") or body.get("expires_at"))
    raw_seconds = body.get("expiresInSeconds") or body.get("expires_in_seconds")
    if raw_seconds is not None:
        if type(raw_seconds) is not int or raw_seconds < 0:
            return 400, {"error": {"code": "INVALID_REQUEST"}}
        if raw_seconds:
            expires_at = now + raw_seconds
    if expires_at is not None and expires_at <= now:
        return 400, {"error": {"code": "INVALID_REQUEST"}}
    try:
        user, _ = await _principal(binding, headers, scope="profile:read", now=now)
    except ProductReadAuthError as failure:
        return failure.status, {"error": {"code": failure.code}}
    proof: dict = {}
    auth, validate = _resource_auth_snapshot(binding, headers, user, {}, now,
        "profile:read", proof)
    snapshot = await binding.batch(auth)
    try:
        validate([part.results for part in snapshot])
    except ProductReadAuthError as failure:
        return failure.status, {"error": {"code": failure.code}}
    token = _new_api_token()
    key_id = f"ak_{uuid.uuid4().hex}"
    record = {"id": key_id, "user_id": user["id"],
        "name": _text(body.get("name")) or "API key", "key_prefix": token[:16],
        "key_hash": hashlib.sha256(token.encode()).hexdigest(),
        "scopes": json.dumps(scopes, separators=(",", ":")),
        "restrictions": json.dumps(restrictions, separators=(",", ":")),
        "expires_at": expires_at, "created_at": now,
        "last_used_at": None, "revoked_at": None}
    statements = [
        binding.prepare("""INSERT INTO d1_command_guard(ok)
            VALUES(CASE WHEN EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u
                WHERE a.name='users' AND u.key=? AND u.value=?)
              AND EXISTS(SELECT 1 FROM app_state WHERE name='sessions' AND payload=?)
              THEN 1 ELSE 0 END)""").bind(user["id"], proof["user"], proof["sessions"]),
        binding.prepare("""INSERT INTO api_keys(id,user_id,name,key_prefix,key_hash,
            scopes,expires_at,restrictions,created_at,last_used_at,revoked_at)
            VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL)""").bind(
                record["id"], record["user_id"], record["name"],
                record["key_prefix"], record["key_hash"], record["scopes"],
                expires_at, record["restrictions"], now),
        binding.prepare("DELETE FROM d1_command_guard"),
    ]
    try:
        await binding.batch(statements)
    except Exception:
        return 503, {"error": {"code": "SERVER_UNAVAILABLE"}}
    return 201, api_key_public_payload(record, token=token)
