"""Explicit, fenced rotation of a GitHub App user's expiring token pair.

Repository GETs never write. A persisted claim excludes concurrent consumers
and survives restarts or unknown provider/native outcomes without being retried.
"""
from __future__ import annotations

import json
from typing import Any

from .cloudflare_github_gateway import GitHubFailure, GitHubTokenBundle, token_bundle
from .cloudflare_state_records import encode_record, read_record_json, record_name
from .cloudflare_validation_budget import _field

REFRESH_MARGIN = 60
CLAIM_SECONDS = 60
MAX_SAFE_INTEGER = 9007199254740991
TOKEN_FIELDS = ("githubAccessTokenExpiresAt", "githubRefreshToken", "githubRefreshTokenExpiresAt", "githubTokenRefresh")


def refresh_required(user: dict, now: int | None) -> bool:
    """Only complete, still-valid refresh facts permit an automatic attempt."""
    access_expiry, refresh_expiry = user.get("githubAccessTokenExpiresAt"), user.get("githubRefreshTokenExpiresAt")
    return (type(now) is int and 0 <= now <= MAX_SAFE_INTEGER - REFRESH_MARGIN
            and bool(user.get("githubId"))
            and isinstance(user.get("githubAccessToken"), str) and bool(user["githubAccessToken"])
            and isinstance(user.get("githubRefreshToken"), str) and bool(user["githubRefreshToken"])
            and type(access_expiry) is int and 0 < access_expiry <= now + REFRESH_MARGIN
            and type(refresh_expiry) is int and now < refresh_expiry <= MAX_SAFE_INTEGER
            and "githubTokenRefresh" not in user)


async def sealed_token_fields(bundle: GitHubTokenBundle, gateway: Any, now: int) -> dict:
    """Validate and encrypt provider token facts before any persistence."""
    if not isinstance(bundle, GitHubTokenBundle) or type(now) is not int or not 0 <= now <= MAX_SAFE_INTEGER:
        raise GitHubFailure("GITHUB_RESPONSE_INVALID")
    raw = {"access_token": bundle.access_token, "token_type": "bearer"}
    if any(value is not None for value in (bundle.expires_in, bundle.refresh_token, bundle.refresh_token_expires_in)):
        raw.update(expires_in=bundle.expires_in, refresh_token=bundle.refresh_token,
                   refresh_token_expires_in=bundle.refresh_token_expires_in)
    bundle = token_bundle(raw)
    result = {"githubAccessToken": await gateway.seal(bundle.access_token), "githubAccessTokenUpdatedAt": now}
    if bundle.expires_in is not None:
        if now > MAX_SAFE_INTEGER - max(bundle.expires_in, bundle.refresh_token_expires_in):
            raise GitHubFailure("GITHUB_RESPONSE_INVALID")
        result.update(githubAccessTokenExpiresAt=now + bundle.expires_in,
                      githubRefreshToken=await gateway.seal(bundle.refresh_token),
                      githubRefreshTokenExpiresAt=now + bundle.refresh_token_expires_in)
    return result


async def _publish_user(binding: Any, *, user: dict, expected_json: str,
                        session: dict, session_json: str, now: int) -> bool:
    """One bounded CAS; a losing consumer cannot call the provider."""
    statement = binding.prepare("""UPDATE app_state SET payload=?,updated_at=?
        WHERE name=? AND payload=? AND EXISTS(SELECT 1 FROM app_state
          WHERE name=? AND payload=? AND json_extract(payload,'$.userId')=?
            AND json_extract(payload,'$.expiresAt')>?) RETURNING name""").bind(
                encode_record("users", user["id"], user), now,
                record_name("users", user["id"]), expected_json,
                record_name("sessions", session["id"]), session_json, user["id"], now)
    result = await binding.batch([statement])
    rows = list(_field(result[0], "results", []))
    return len(rows) == 1


async def _current_claim(binding: Any, *, expected: dict, claim: dict) -> tuple[dict | None, str | None]:
    snapshot = await read_record_json(binding, "users", expected["id"])
    current = json.loads(snapshot) if snapshot is not None else None
    if (not isinstance(current, dict) or current.get("githubId") != expected.get("githubId")
            or current.get("githubTokenRefresh") != claim
            or any(current.get(field) != expected.get(field) for field in (
                "githubAccessToken", "githubAccessTokenExpiresAt", "githubRefreshToken", "githubRefreshTokenExpiresAt"))):
        return None, None
    return current, snapshot


def _error(status: int, code: str):
    return status, {"error": {"code": code}}, {"Cache-Control": "no-store"}


async def handle_github_refresh(*, binding: Any, gateway: Any, session: dict, user: dict, now: int):
    """Called only after a real cookie session and trusted Origin were checked."""
    claim = user.get("githubTokenRefresh")
    if "githubTokenRefresh" in user:
        started = claim.get("startedAt") if isinstance(claim, dict) else None
        if type(started) is int and started <= now < started + CLAIM_SECONDS:
            return _error(409, "GITHUB_REFRESH_PENDING")
        return _error(403, "GITHUB_REAUTHORIZATION_REQUIRED")
    access_expiry, refresh_expiry = user.get("githubAccessTokenExpiresAt"), user.get("githubRefreshTokenExpiresAt")
    if (not user.get("githubId") or not isinstance(user.get("githubAccessToken"), str)
            or not user.get("githubAccessToken") or type(access_expiry) is not int
            or not 0 < access_expiry <= MAX_SAFE_INTEGER
            or not isinstance(user.get("githubRefreshToken"), str) or not user["githubRefreshToken"]
            or type(refresh_expiry) is not int or not now < refresh_expiry <= MAX_SAFE_INTEGER):
        return _error(403, "GITHUB_REAUTHORIZATION_REQUIRED")
    if access_expiry > now + REFRESH_MARGIN:
        return 200, {"ok": True, "refreshed": False}, {"Cache-Control": "no-store"}

    # Decryption/configuration are local and cannot consume the provider token.
    refresh_token = await gateway.unseal(user["githubRefreshToken"])
    validate = getattr(gateway, "validate_refresh_configuration", None)
    if validate is not None:
        await validate()
    user_json = await read_record_json(binding, "users", user["id"])
    session_json = await read_record_json(binding, "sessions", session["id"])
    if (user_json is None or json.loads(user_json) != user or session_json is None
            or {**json.loads(session_json), "id": session["id"]} != session):
        return _error(409, "ACCOUNT_CHANGED")
    from .cloudflare_github_identity_http import _random_urlsafe
    claim = {"id": _random_urlsafe(32), "startedAt": now}
    claimed = {**user, "githubTokenRefresh": claim}
    if not await _publish_user(binding, user=claimed, expected_json=user_json,
                               session=session, session_json=session_json, now=now):
        return _error(409, "ACCOUNT_CHANGED")

    try:
        bundle = await gateway.refresh(refresh_token)
    except GitHubFailure as failure:
        if failure.request_rejected:
            current, snapshot = await _current_claim(binding, expected=user, claim=claim)
            if current is not None:
                next_user = {key: value for key, value in current.items() if key != "githubTokenRefresh"}
                if failure.code == "GITHUB_REAUTHORIZATION_REQUIRED":
                    next_user.pop("githubRefreshToken", None)
                    next_user.pop("githubRefreshTokenExpiresAt", None)
                await _publish_user(binding, user=next_user, expected_json=snapshot,
                                    session=session, session_json=session_json, now=now)
        # Transport, malformed success and unknown D1 outcomes keep the claim.
        raise
    fields = await sealed_token_fields(bundle, gateway, now)
    if not all(field in fields for field in ("githubAccessTokenExpiresAt", "githubRefreshToken", "githubRefreshTokenExpiresAt")):
        raise GitHubFailure("GITHUB_RESPONSE_INVALID")
    current, snapshot = await _current_claim(binding, expected=user, claim=claim)
    if current is None:
        return _error(409, "ACCOUNT_CHANGED")
    next_user = {**{key: value for key, value in current.items() if key not in TOKEN_FIELDS}, **fields}
    if not await _publish_user(binding, user=next_user, expected_json=snapshot,
                               session=session, session_json=session_json, now=now):
        return _error(401, "UNAUTHENTICATED")
    return 200, {"ok": True, "refreshed": True}, {"Cache-Control": "no-store"}
