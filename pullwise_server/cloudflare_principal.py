"""Session and API-key authorization shared by ledger, identity and platform billing."""
from __future__ import annotations
import hashlib
import json
import math
from typing import Any, Mapping
from .cloudflare_state_records import read_record, record_name

SESSION_COOKIE = "pw_session"
API_KEY_PREFIX = "pwk_"


def _observe_authenticated_actor(binding, actor_id):
    """Optional runtime abuse gate after authentication, with no D1 writes."""
    for _ in range(4):
        observer = getattr(binding, "observe_authenticated_actor", None)
        if callable(observer):
            observer(actor_id)
            return
        binding = getattr(binding, "binding", None)
        if binding is None:
            return


class PrincipalAuthError(Exception):
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
            if candidate and candidate not in sessions:
                sessions.append(candidate)
                if len(sessions) > 8:
                    raise PrincipalAuthError(400, "AMBIGUOUS_AUTH", "Too many session credentials.")
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
    user = await read_record(binding, "users", owner_id)
    return user if isinstance(user, dict) and user.get("id") == owner_id else None


def has_verified_email_identity(user: dict) -> bool:
    providers = user.get("providers")
    return (isinstance(providers, list) and "email" in providers
            and isinstance(user.get("email"), str) and bool(user["email"].strip())
            and type(user.get("emailVerifiedAt")) is int and user["emailVerifiedAt"] > 0)


def session_identity_usable(user: dict) -> bool:
    providers = user.get("providers")
    providers = providers if isinstance(providers, list) else []
    if has_verified_email_identity(user):
        return True
    if "github" in providers:
        return bool(user.get("githubAccessToken"))
    # Preserve legacy sessions without provider annotations; an explicit email
    # identity must have its own verified login facts, never billing contact data.
    return "email" not in providers


async def _principal(binding: Any, headers: Mapping[str, object],
                     *, scope: str, now: int) -> tuple[dict, dict]:
    bearer = _bearer(headers)
    header_key = _header(headers, "X-Pullwise-Api-Key")
    cookie_sessions = _cookie_sessions(headers)
    session_ids = ([bearer] if bearer and not bearer.startswith(API_KEY_PREFIX)
                   else cookie_sessions)
    api_token = bearer if bearer.startswith(API_KEY_PREFIX) else ""
    if header_key and not header_key.startswith(API_KEY_PREFIX):
        if cookie_sessions or bearer:
            raise PrincipalAuthError(400, "AMBIGUOUS_AUTH",
                "Use either a session or an API key, not both.")
        raise PrincipalAuthError(401, "UNAUTHENTICATED",
            "A session or API key is required.")
    if header_key.startswith(API_KEY_PREFIX):
        if api_token and header_key != api_token:
            raise PrincipalAuthError(400, "AMBIGUOUS_AUTH", "Use one API key.")
        api_token = header_key
    if api_token and (cookie_sessions or (bearer and not bearer.startswith(API_KEY_PREFIX))):
        raise PrincipalAuthError(400, "AMBIGUOUS_AUTH",
            "Use either a session or an API key, not both.")
    if api_token:
        key_hash = hashlib.sha256(api_token.encode("utf-8")).hexdigest()
        record = await binding.prepare("""SELECT user_id,scopes,expires_at,restrictions
            FROM api_keys WHERE key_hash=? AND revoked_at IS NULL""").bind(key_hash).first()
        expires_at = _timestamp(record["expires_at"]) if record else None
        if (not record or (record["expires_at"] is not None
                           and (expires_at is None or expires_at < now))):
            raise PrincipalAuthError(401, "UNAUTHENTICATED", "A session or API key is required.")
        try:
            scopes = json.loads(record["scopes"])
            restrictions = json.loads(record["restrictions"])
        except (TypeError, ValueError):
            raise PrincipalAuthError(403, "INSUFFICIENT_SCOPE", "API key scope is invalid.") from None
        if not isinstance(scopes, list) or scope not in scopes:
            raise PrincipalAuthError(403, "INSUFFICIENT_SCOPE", f"API key scope {scope} is required.")
        if not isinstance(restrictions, dict) or restrictions.get("kind") == "audit_bundle":
            raise PrincipalAuthError(403, "INSUFFICIENT_SCOPE", "API key restriction forbids this read.")
        user = await _user(binding, str(record["user_id"]))
        if user is None:
            raise PrincipalAuthError(401, "UNAUTHENTICATED", "A session or API key is required.")
        _observe_authenticated_actor(binding, user["id"])
        return user, restrictions
    if session_ids:
        for session_id in session_ids:
            try:
                record_name("sessions", session_id)
            except (ValueError, UnicodeError):
                continue
            session = await read_record(binding, "sessions", session_id)
            if not isinstance(session, dict):
                continue
            expires_at = _timestamp(session.get("expiresAt"))
            owner_id = session.get("userId")
            if expires_at is None or expires_at < now or not isinstance(owner_id, str):
                continue
            user = await _user(binding, owner_id)
            if user is not None and session_identity_usable(user):
                _observe_authenticated_actor(binding, user["id"])
                return user, {}
    raise PrincipalAuthError(401, "UNAUTHENTICATED", "A session or API key is required.")


def _resource_auth_snapshot(binding: Any, headers: Mapping[str, object],
                            user: dict, restrictions: dict, now: int,
                            scope: str | tuple[str, ...], proof: dict | None = None):
    bearer = _bearer(headers)
    header_key = _header(headers, "X-Pullwise-Api-Key")
    token = bearer if bearer.startswith(API_KEY_PREFIX) else header_key
    sessions = ([bearer] if bearer and not bearer.startswith(API_KEY_PREFIX)
                else _cookie_sessions(headers))
    owner_id = user["id"]
    named_sessions = []
    for candidate in sessions:
        try:
            named_sessions.append(record_name("sessions", candidate))
        except (ValueError, UnicodeError):
            continue
    if not named_sessions:
        named_sessions = [record_name("sessions", "_no_session_")]
    statements = [
        binding.prepare("""SELECT user_id,scopes,expires_at,restrictions,revoked_at
            FROM api_keys WHERE key_hash=?""").bind(
                hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""),
        binding.prepare("SELECT name,payload FROM app_state WHERE name IN (" +
                        ",".join("?" for _ in named_sessions) + ")").bind(*named_sessions),
        binding.prepare("SELECT u.payload AS snapshot FROM app_state u WHERE u.name=?").bind(
            record_name("users", owner_id)),
    ]

    def validate(rows: list) -> None:
        key_rows, session_rows, user_rows = rows
        saved_user = json.loads(user_rows[0]["snapshot"]) if len(user_rows) == 1 else None
        if saved_user != user:
            raise PrincipalAuthError(401, "UNAUTHENTICATED", "A session or API key is required.")
        if token:
            record = key_rows[0] if len(key_rows) == 1 else None
            expiry = _timestamp(record["expires_at"]) if record else None
            if (not record or record["revoked_at"] is not None
                    or record["user_id"] != owner_id
                    or (record["expires_at"] is not None and (expiry is None or expiry < now))):
                raise PrincipalAuthError(401, "UNAUTHENTICATED", "A session or API key is required.")
            try:
                scopes = json.loads(record["scopes"])
                current_restrictions = json.loads(record["restrictions"])
            except (TypeError, ValueError):
                scopes, current_restrictions = None, None
            required_scopes = (scope,) if isinstance(scope, str) else scope
            if (not isinstance(scopes, list) or any(value not in scopes for value in required_scopes)
                    or current_restrictions != restrictions
                    or not isinstance(current_restrictions, dict)
                    or current_restrictions.get("kind") == "audit_bundle"):
                raise PrincipalAuthError(403, "INSUFFICIENT_SCOPE", "API key scope is invalid.")
            if proof is not None:
                proof.update(key=record, sessions=None, user=user_rows[0]["snapshot"], token=token)
            return
        saved_sessions = {row["name"]: row["payload"] for row in session_rows}
        if saved_sessions:
            for session_id in sessions:
                try:
                    payload = saved_sessions.get(record_name("sessions", session_id))
                except (ValueError, UnicodeError):
                    continue
                session = json.loads(payload) if payload is not None else None
                if (isinstance(session, dict) and session.get("userId") == owner_id
                        and (expiry := _timestamp(session.get("expiresAt"))) is not None
                        and expiry >= now):
                    if proof is not None:
                        proof.update(key=None, sessions=payload,
                                     user=user_rows[0]["snapshot"], token=None,
                                     session_id=session_id)
                    return
        raise PrincipalAuthError(401, "UNAUTHENTICATED", "A session or API key is required.")

    return statements, validate
