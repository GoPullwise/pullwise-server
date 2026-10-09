"""Browser-bound, one-use email codes over the existing metered identity store."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from urllib.parse import urlsplit

from .cloudflare_d1_batch import execute_d1_batch
from .cloudflare_d1_mapping import initialize_account
from .cloudflare_github_identity_http import (
    SESSION_AGE, _random_urlsafe, _session_cookie, _session_user, session_payload,
)
from .cloudflare_principal import _header, has_verified_email_identity
from .cloudflare_state_records import (
    changed_guard, encode_record, expired_record_commands, read_record_json, record_name,
    write_record_commands,
)
from .json_input import validate_json_unicode

CODE_AGE = 600
SEND_INTERVAL = 60
MAX_ATTEMPTS = 5
BROWSER_COOKIE = "pw_email_challenge"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_LOCAL = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}")
_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_NO_STORE = {"Cache-Control": "no-store", "Vary": "Cookie"}


def normalize_email(value):
    """Use one case-insensitive ASCII mailbox identity, without provider aliases."""
    if not isinstance(value, str):
        raise ValueError("INVALID_INPUT")
    email = value.strip().lower()
    if len(email) > 254 or email.count("@") != 1 or not email.isascii():
        raise ValueError("INVALID_INPUT")
    local, domain = email.split("@")
    if (not _LOCAL.fullmatch(local) or not _DOMAIN.fullmatch(domain)
            or local.startswith(".") or local.endswith(".") or ".." in local):
        raise ValueError("INVALID_INPUT")
    return email


def email_key(email):
    return hashlib.sha256(("pullwise-email:" + email).encode("ascii")).hexdigest()


def _digest(secret, *parts):
    return hmac.new(secret.encode("utf-8"), "\0".join(parts).encode("utf-8"), hashlib.sha256).hexdigest()


def _new_code():
    # Rejection sampling avoids the bias of reducing all uint32 values modulo a million.
    while True:
        value = int.from_bytes(base64.urlsafe_b64decode(_random_urlsafe(4) + "=="), "big")
        if value < 4_294_000_000:
            return f"{value % 1_000_000:06d}"


def _challenge_cookie(token, same_site, *, clear=False):
    same_site = same_site if same_site in {"Lax", "Strict", "None"} else "Lax"
    return (f"{BROWSER_COOKIE}={'' if clear else token}; Path=/; HttpOnly; Secure; "
            f"SameSite={same_site}; Max-Age={0 if clear else CODE_AGE}")


def _browser_token(headers):
    values = set()
    for item in _header(headers, "Cookie").split(";"):
        name, separator, value = item.partition("=")
        if separator and name.strip() == BROWSER_COOKIE:
            values.add(value.strip().strip('"'))
    return next(iter(values)) if len(values) == 1 else ""


def _error(status, code, *, retry_after=None, cookie=None):
    headers = dict(_NO_STORE)
    if retry_after is not None:
        headers["Retry-After"] = str(max(1, int(retry_after)))
    if cookie:
        headers["Set-Cookie"] = cookie
    return status, {"error": {"code": code}}, headers


def _guard(predicate, params):
    return ("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN " + predicate + " THEN 1 ELSE 0 END)", tuple(params))


def _exists(kind, identity, snapshot):
    return _guard("EXISTS(SELECT 1 FROM app_state WHERE name=? AND payload=?)",
                  (record_name(kind, identity), snapshot))


def _put(kind, identity, value, expected, now):
    return write_record_commands(kind, identity, value, expected_json=expected, now=now)[:-1]


def _consume(key, snapshot):
    return [("DELETE FROM app_state WHERE name=? AND payload=?",
             (record_name("emailChallenges", key), snapshot)), changed_guard()]


def _verified_email(user):
    return user.get("email") if has_verified_email_identity(user) else None


async def handle_email_request(*, binding, gateway, secret, admit, method, path, body,
                               headers, now, cookie_same_site="Lax", trusted_origins):
    """Return an HTTP tuple or None; delivery and abuse admission are injected."""
    paths = {"/auth/email/request-code", "/auth/email/verify-code"}
    if path not in paths:
        return None
    if method != "POST":
        return _error(405, "METHOD_NOT_ALLOWED")
    try:
        validate_json_unicode(body)
        if len(json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 8192:
            return _error(413, "REQUEST_TOO_LARGE")
        request_code = path.endswith("/request-code")
        required = {"email", "purpose"} if request_code else {"email", "challengeId", "code"}
        if not isinstance(body, dict) or set(body) != required:
            raise ValueError("INVALID_INPUT")
        email = normalize_email(body["email"])
        if request_code:
            if body["purpose"] not in {"login", "link"}:
                raise ValueError("INVALID_INPUT")
        elif (not isinstance(body["challengeId"], str) or not _TOKEN.fullmatch(body["challengeId"])
                or not isinstance(body["code"], str) or not re.fullmatch(r"[0-9]{6}", body["code"])):
            raise ValueError("INVALID_INPUT")
    except (ValueError, TypeError, UnicodeError):
        return _error(422, "INVALID_INPUT")
    try:
        origin = urlsplit(_header(headers, "Origin"))
    except ValueError:
        return _error(403, "UNTRUSTED_ORIGIN")
    if f"{origin.scheme}://{origin.netloc}" not in trusted_origins:
        return _error(403, "UNTRUSTED_ORIGIN")
    if (type(now) is not int or now <= 0 or not isinstance(secret, str) or len(secret) < 32
            or getattr(gateway, "configured", False) is not True or not callable(admit)):
        return _error(503, "EMAIL_AUTH_NOT_CONFIGURED")
    key = email_key(email)
    admitted = await admit("send" if request_code else "verify", key)
    if admitted is False:
        return _error(429, "EMAIL_RATE_LIMIT", retry_after=SEND_INTERVAL)
    if admitted is not True:
        return _error(503, "EMAIL_AUTH_UNAVAILABLE")
    snapshot = await read_record_json(binding, "emailChallenges", key)
    challenge = json.loads(snapshot) if snapshot is not None else None

    if request_code:
        purpose = body["purpose"]
        session, user = (await _session_user(binding, headers, now) if purpose == "link" else (None, None))
        if purpose == "link" and not session:
            return _error(401, "UNAUTHENTICATED")
        if purpose == "link" and _verified_email(user) not in {None, email}:
            return _error(409, "EMAIL_CHANGE_NOT_SUPPORTED")
        if challenge and challenge["createdAt"] + SEND_INTERVAL > now:
            return _error(429, "EMAIL_RATE_LIMIT", retry_after=challenge["createdAt"] + SEND_INTERVAL - now)
        challenge_id, browser_token, code = _random_urlsafe(32), _random_urlsafe(32), _new_code()
        next_challenge = {"email": email, "challengeId": challenge_id, "purpose": purpose,
            "codeHash": _digest(secret, "code", key, challenge_id, purpose, code),
            "browserHash": _digest(secret, "browser", key, challenge_id, browser_token),
            "attempts": 0, "createdAt": now, "expiresAt": now + CODE_AGE}
        if purpose == "link":
            next_challenge.update(userId=user["id"], sessionId=session["id"])
        cleanup = await expired_record_commands(binding, "emailChallenges", now=now)
        own_name = record_name("emailChallenges", key)
        cleanup = [command for index in range(0, len(cleanup), 2)
                   if cleanup[index][1][0] != own_name for command in cleanup[index:index + 2]]
        await execute_d1_batch(binding, [*cleanup,
                                       *_put("emailChallenges", key, next_challenge, snapshot, now),
                                       ("DELETE FROM d1_command_guard", ())])
        try:
            # Persist before dispatch; unknown delivery never causes an automatic resend.
            await gateway.send_code(email, code, expires_in=CODE_AGE)
        except Exception:
            return _error(503, "EMAIL_SEND_UNAVAILABLE")
        return 202, {"challengeId": challenge_id, "expiresIn": CODE_AGE, "retryAfter": SEND_INTERVAL}, {
            **_NO_STORE, "Set-Cookie": _challenge_cookie(browser_token, cookie_same_site)}

    if not challenge or not hmac.compare_digest(challenge["challengeId"], body["challengeId"]):
        return _error(400, "EMAIL_CODE_INVALID")
    browser_token = _browser_token(headers)
    if (not _TOKEN.fullmatch(browser_token) or not hmac.compare_digest(challenge["browserHash"],
            _digest(secret, "browser", key, challenge["challengeId"], browser_token))):
        return _error(400, "EMAIL_CODE_INVALID")
    if challenge["expiresAt"] <= now:
        return _error(400, "EMAIL_CODE_EXPIRED")
    if challenge["attempts"] >= MAX_ATTEMPTS:
        return _error(429, "EMAIL_ATTEMPTS_EXCEEDED", retry_after=challenge["expiresAt"] - now)
    session, actor = (await _session_user(binding, headers, now) if challenge["purpose"] == "link" else (None, None))
    if challenge["purpose"] == "link" and (not session or session["id"] != challenge["sessionId"]
            or actor["id"] != challenge["userId"]):
        return _error(401, "UNAUTHENTICATED")
    if not hmac.compare_digest(challenge["codeHash"], _digest(secret, "code", key,
            challenge["challengeId"], challenge["purpose"], body["code"])):
        await execute_d1_batch(binding, [*_put("emailChallenges", key,
            {**challenge, "attempts": challenge["attempts"] + 1}, snapshot, now),
            ("DELETE FROM d1_command_guard", ())])
        return _error(400, "EMAIL_CODE_INVALID")

    identity_snapshot = await read_record_json(binding, "emailIdentities", key)
    identity = json.loads(identity_snapshot) if identity_snapshot is not None else None
    linking = challenge["purpose"] == "link"
    if linking and (_verified_email(actor) not in {None, email}
                    or identity and identity["userId"] != actor["id"]):
        await execute_d1_batch(binding, [*_consume(key, snapshot), ("DELETE FROM d1_command_guard", ())])
        return _error(409, "EMAIL_ALREADY_LINKED", cookie=_challenge_cookie("", cookie_same_site, clear=True))
    user_id = actor["id"] if linking else identity["userId"] if identity else "usr_email_" + _random_urlsafe(32)
    user_snapshot = await read_record_json(binding, "users", user_id)
    user = json.loads(user_snapshot) if user_snapshot is not None else None
    if (linking and user != actor) or (identity and (not user or _verified_email(user) != email)):
        return _error(409 if linking else 503, "ACCOUNT_CHANGED" if linking else "EMAIL_IDENTITY_UNAVAILABLE")
    created = user is None
    if linking or created:
        providers = (user or {}).get("providers")
        providers = [value for value in providers if isinstance(value, str)] if isinstance(providers, list) else []
        user = {**(user or {}), "id": user_id,
            "name": (user or {}).get("name") or email.split("@", 1)[0],
            "createdAt": (user or {}).get("createdAt", now), "email": email,
            "emailVerified": True, "emailVerifiedAt": now,
            "providers": list(dict.fromkeys([*providers, "email"]))}
    commands = [*_consume(key, snapshot)]
    if identity is not None:
        commands.append(_exists("emailIdentities", key, identity_snapshot))
    else:
        commands.extend(_put("emailIdentities", key,
            {"email": email, "userId": user_id, "createdAt": now, "verifiedAt": now}, None, now))
    if linking or created:
        commands.extend(_put("users", user_id, user, user_snapshot, now))
    else:
        commands.append(_exists("users", user_id, user_snapshot))
    if created:
        commands.extend(initialize_account(owner_id=user_id,
            account_snapshot=encode_record("users", user_id, user), now=now)[:-1])
    if linking:
        session_snapshot = await read_record_json(binding, "sessions", session["id"])
        if session_snapshot is None or {**json.loads(session_snapshot), "id": session["id"]} != session:
            return _error(401, "UNAUTHENTICATED")
        commands.append(_exists("sessions", session["id"], session_snapshot))
        cookie = _challenge_cookie("", cookie_same_site, clear=True)
    else:
        session_id = "ses-" + _random_urlsafe(32)
        commands.extend(await expired_record_commands(binding, "sessions", now=now))
        commands.extend(_put("sessions", session_id, {"id": session_id, "userId": user_id,
            "createdAt": now, "expiresAt": now + SESSION_AGE}, None, now))
        cookie = _session_cookie(session_id, same_site=cookie_same_site)
    commands.append(("DELETE FROM d1_command_guard", ()))
    await execute_d1_batch(binding, commands)
    return 200, session_payload(user), {**_NO_STORE, "Set-Cookie": cookie}
