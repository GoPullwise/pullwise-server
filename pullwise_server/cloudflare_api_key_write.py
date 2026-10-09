"""Guard session-only API-key revocation in a single D1 write batch."""
from __future__ import annotations

from .cloudflare_plan_limits import PlanLimitError

import hashlib
import base64
import json
import secrets
import uuid
from typing import Any, Mapping

from .api_key_dto_rules import (
    api_key_public_payload, parse_api_key_restrictions,
    requested_api_key_scopes, _text,
)

from .cloudflare_principal import (
    PrincipalAuthError, _bearer, _cookie_sessions, _header,
    _principal, _resource_auth_snapshot,
)
from .cloudflare_ledger_auth import ledger_principal, ROLE_SCOPES
from .cloudflare_state_records import record_name


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
    except PrincipalAuthError as failure:
        return failure.status, {"error": {"code": failure.code}}
    proof: dict = {}
    auth, validate = _resource_auth_snapshot(binding, headers, user, {}, now,
        "profile:read", proof)
    result = await binding.batch([*auth, binding.prepare("""SELECT id FROM api_keys
        WHERE id=? AND user_id=? AND revoked_at IS NULL""").bind(key_id, user["id"])])
    try:
        validate([part.results for part in result[:len(auth)]])
    except PrincipalAuthError as failure:
        return failure.status, {"error": {"code": failure.code}}
    if not result[-1].results:
        return 404, {"error": {"code": "NOT_FOUND"}}
    statements = [
        binding.prepare("""UPDATE api_keys SET revoked_at=? WHERE id=? AND user_id=?
            AND revoked_at IS NULL
            AND EXISTS(SELECT 1 FROM app_state WHERE name=? AND payload=?)
            AND EXISTS(SELECT 1 FROM app_state WHERE name=? AND payload=?
                AND json_extract(payload,'$.userId')=?
                AND CAST(json_extract(payload,'$.expiresAt') AS INTEGER)>=?)""").bind(
                now, key_id, user["id"], record_name("users", user["id"]), proof["user"],
                record_name("sessions", proof["session_id"]), proof["sessions"], user["id"], now),
        binding.prepare("""INSERT INTO d1_command_guard(ok)
            VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"""),
        binding.prepare("DELETE FROM d1_command_guard"),
    ]
    try:
        await binding.batch(statements)
    except PlanLimitError as error:
        return error.response()
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
    try:
        restrictions = parse_api_key_restrictions(body.get("restrictions"))
    except ValueError:
        return 400, {"error": {"code": "INVALID_RESTRICTION"}}
    if "projectIds" in restrictions and any(scope.startswith("members:") for scope in scopes):
        return 400, {"error": {"code": "INVALID_RESTRICTION"}}
    raw_expiry = body.get("expiresAt", body.get("expires_at"))
    expires_at = None
    if raw_expiry is not None:
        if not (type(raw_expiry) is int or isinstance(raw_expiry, str)
                and len(raw_expiry) <= 16 and raw_expiry.isascii() and raw_expiry.isdigit()):
            return 400, {"error": {"code": "INVALID_REQUEST"}}
        expires_at = int(raw_expiry)
        if not 0 <= expires_at <= 9007199254740991:
            return 400, {"error": {"code": "INVALID_REQUEST"}}
    raw_seconds = body.get("expiresInSeconds", body.get("expires_in_seconds"))
    if raw_seconds is not None:
        if type(raw_seconds) is not int or not 0 <= raw_seconds <= 9007199254740991 - now:
            return 400, {"error": {"code": "INVALID_REQUEST"}}
        if raw_seconds:
            expires_at = now + raw_seconds
    if expires_at is not None and expires_at <= now:
        return 400, {"error": {"code": "INVALID_REQUEST"}}
    proof: dict = {}
    selected = restrictions.get("workspaceId") or _header(headers, "X-Pullwise-Workspace")
    if restrictions.get("workspaceId") and _header(headers, "X-Pullwise-Workspace") not in {"", restrictions["workspaceId"]}:
        return 400, {"error": {"code": "INVALID_RESTRICTION"}}
    selected_headers = {**headers, "X-Pullwise-Workspace": selected or ""}
    try:
        user, _, auth, validate = await ledger_principal(binding=binding,
            headers=selected_headers, scope="profile:read", now=now, proof=proof)
        snapshot = await binding.batch(auth)
        validate([part.results for part in snapshot])
    except PrincipalAuthError as failure:
        return failure.status, {"error": {"code": failure.code}}
    workspace = user["_workspace"]
    if any(scope not in ROLE_SCOPES[workspace["role"]] for scope in scopes):
        return 403, {"error": {"code": "ROLE_FORBIDDEN"}}
    if selected:
        if restrictions.get("workspaceMemberRevision", workspace["revision"]) != workspace["revision"]:
            return 403, {"error": {"code": "WORKSPACE_MEMBERSHIP_CHANGED"}}
        restrictions.update(workspaceId=workspace["id"], workspaceMemberRevision=workspace["revision"])
    token = _new_api_token()
    key_id = f"ak_{uuid.uuid4().hex}"
    record = {"id": key_id, "user_id": user["_actor"]["id"],
        "name": _text(body.get("name")) or "API key", "key_prefix": token[:16],
        "key_hash": hashlib.sha256(token.encode()).hexdigest(),
        "scopes": json.dumps(scopes, separators=(",", ":")),
        "restrictions": json.dumps(restrictions, separators=(",", ":")),
        "expires_at": expires_at, "created_at": now,
        "last_used_at": None, "revoked_at": None}
    from .cloudflare_ledger_api import _write_guard
    statements = [
        _write_guard(binding, proof, user["id"], now),
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
    except PlanLimitError as error:
        return error.response()
    except Exception:
        return 503, {"error": {"code": "SERVER_UNAVAILABLE"}}
    return 201, api_key_public_payload(record, token=token)
