"""GitHub OAuth/App HTTP composition for the Server Worker.

The GitHub gateway owns bounded network I/O and token sealing. D1 writes use
the existing single-use state and guarded app_state transaction adapters.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit, urlunsplit

from .cloudflare_oauth_state_adapter import D1OAuthStates
from .cloudflare_session_adapter import D1SessionTransactions
from .cloudflare_account_adapter import D1AccountTransactions
from .cloudflare_principal import (
    _cookie_sessions, _header, _observe_authenticated_actor,
    has_verified_email_identity, session_identity_usable,
)
from .cloudflare_ledger_auth import ledger_principal
from .cloudflare_principal import PrincipalAuthError
from .cloudflare_state_records import encode_record, read_record, read_record_json, record_name

SESSION_AGE = 7 * 86400


def _param(params: Mapping[str, object], name: str) -> str:
    value = params.get(name)
    if isinstance(value, list):
        value = value[-1] if value else ""
    return value if isinstance(value, str) else ""


def _random_urlsafe(byte_count: int) -> str:
    try:
        import js
    except ModuleNotFoundError:
        data = secrets.token_bytes(byte_count)
    else:
        view = js.Uint8Array.new(byte_count)
        js.crypto.getRandomValues(view)
        data = bytes(int(view[index]) for index in range(byte_count))
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _redirect(value: str, app_url: str, fallback: str) -> str:
    app = urlsplit(app_url)
    if not app.scheme == "https" or not app.netloc:
        raise ValueError("invalid app origin")
    if not value or any(char in value for char in "\r\n\\") or value.startswith("//"):
        return app_url.rstrip("/") + fallback
    if value.startswith("/"):
        return app_url.rstrip("/") + value
    parsed = urlsplit(value)
    if parsed.scheme == "https" and parsed.netloc == app.netloc and not parsed.username:
        return value
    return app_url.rstrip("/") + fallback


def _redirect_result(location: str, cookie: str | None = None):
    headers = {"Location": location, "Cache-Control": "no-store"}
    if cookie:
        headers["Set-Cookie"] = cookie
    return 302, {"location": location}, headers


def _session_cookie(session_id: str, *, clear: bool = False, same_site: str = "Lax") -> str:
    if same_site not in {"Lax", "Strict", "None"}:
        same_site = "Lax"
    value = "" if clear else session_id
    age = 0 if clear else SESSION_AGE
    return f"pw_session={value}; Path=/; HttpOnly; Secure; SameSite={same_site}; Max-Age={age}"


async def _user(binding: Any, owner_id: str) -> dict | None:
    if not isinstance(owner_id, str) or not owner_id:
        return None
    user = await read_record(binding, "users", owner_id)
    return user if isinstance(user, dict) and user.get("id") == owner_id else None


async def _session_user(binding: Any, headers: Mapping[str, object], now: int):
    if _header(headers, "Authorization") or _header(headers, "X-Pullwise-Api-Key"):
        return None, None
    for session_id in _cookie_sessions(headers):
        try:
            record_name("sessions", session_id)
        except (ValueError, UnicodeError):
            continue
        session = await read_record(binding, "sessions", session_id)
        if (isinstance(session, dict) and type(session.get("expiresAt")) is int
                and session["expiresAt"] > now):
            user = await _user(binding, str(session.get("userId") or ""))
            if user is not None and session_identity_usable(user):
                _observe_authenticated_actor(binding, user["id"])
                return {**session, "id": session_id}, user
    return None, None


def session_payload(user: dict | None) -> dict:
    if user is None:
        return {"authenticated": False, "user": None,
                "github": {"identityConnected": False, "repositoriesConnected": False}}
    verified = has_verified_email_identity(user)
    providers = user.get("providers")
    providers = [value for value in providers if isinstance(value, str)] if isinstance(providers, list) else []
    access = user.get("githubRepositoryAccess")
    return {"authenticated": True,
            "user": {"id": user["id"], "name": user.get("name"), "avatarUrl": user.get("avatarUrl"),
                     "email": user.get("email") if verified else None,
                     "emailVerified": verified, "providers": providers},
            "github": {"identityConnected": bool(user.get("githubAccessToken")),
                       "repositoriesConnected": isinstance(access, dict) and access.get("status") == "authorized"}}


def _provider_union(user: dict, provider: str) -> list[str]:
    providers = user.get("providers")
    providers = [item for item in providers if isinstance(item, str)] if isinstance(providers, list) else []
    return list(dict.fromkeys([*providers, provider]))


async def _github_login_identity(binding: Any, github_id: str) -> tuple[str, str | None]:
    snapshot = await read_record_json(binding, "githubIdentities", github_id)
    if snapshot is None:
        return "usr_github_" + github_id, None
    mapping = json.loads(snapshot)
    owner_id = mapping.get("userId")
    user = await _user(binding, owner_id)
    if mapping.get("githubId") != github_id or user is None or str(user.get("githubId")) != github_id:
        raise ValueError("GITHUB_IDENTITY_CONFLICT")
    return owner_id, snapshot


async def _github_identity_owner(binding: Any, github_id: str) -> str:
    owner_id, _ = await _github_login_identity(binding, github_id)
    return owner_id


async def _write_linked_user(binding: Any, *, user: dict, expected_user: dict,
                             session: dict, github_id: str, now: int) -> None:
    """Preflight ordinary conflicts, then publish the identity/user in one CAS.

    The preview coordinator serializes these requests. Exact snapshots still
    fence the entire transaction, including revocation during provider awaits.
    """
    owner_id = user["id"]
    user_snapshot = await read_record_json(binding, "users", owner_id)
    if user_snapshot is None or json.loads(user_snapshot) != expected_user:
        raise ValueError("ACCOUNT_CHANGED")
    session_snapshot = await read_record_json(binding, "sessions", session["id"])
    saved_session = json.loads(session_snapshot) if session_snapshot is not None else None
    if (saved_session is None or {**saved_session, "id": session["id"]} != session
            or session.get("expiresAt", 0) <= now):
        raise ValueError("UNAUTHENTICATED")
    mapping_snapshot = await read_record_json(binding, "githubIdentities", github_id)
    mapping = json.loads(mapping_snapshot) if mapping_snapshot is not None else None
    if mapping is not None and (mapping.get("userId") != owner_id or mapping.get("githubId") != github_id):
        raise ValueError("GITHUB_IDENTITY_CONFLICT")
    current_github = expected_user.get("githubId")
    if current_github is not None and str(current_github) != github_id:
        raise ValueError("GITHUB_IDENTITY_CONFLICT")
    legacy_id = "usr_github_" + github_id
    legacy = await _user(binding, legacy_id) if legacy_id != owner_id else None
    if legacy is not None:
        raise ValueError("GITHUB_IDENTITY_CONFLICT")
    user_name, session_name = record_name("users", owner_id), record_name("sessions", session["id"])
    mapping_name = record_name("githubIdentities", github_id)
    checks = ["EXISTS(SELECT 1 FROM app_state WHERE name=? AND payload=?)",
              "EXISTS(SELECT 1 FROM app_state WHERE name=? AND payload=?)"]
    params = [user_name, user_snapshot, session_name, session_snapshot]
    if legacy_id != owner_id:
        checks.append("NOT EXISTS(SELECT 1 FROM app_state WHERE name=?)")
        params.append(record_name("users", legacy_id))
    if mapping_snapshot is None:
        checks.append("NOT EXISTS(SELECT 1 FROM app_state WHERE name=?)")
        params.append(mapping_name)
    else:
        checks.append("EXISTS(SELECT 1 FROM app_state WHERE name=? AND payload=?)")
        params.extend([mapping_name, mapping_snapshot])
    commands = [binding.prepare("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN "
        + " AND ".join(checks) + " THEN 1 ELSE 0 END)").bind(*params)]
    if mapping_snapshot is None:
        mapping = {"githubId": github_id, "userId": owner_id, "createdAt": now}
        commands.extend([binding.prepare("INSERT INTO app_state(name,payload,updated_at) VALUES(?,?,?)")
            .bind(mapping_name, encode_record("githubIdentities", github_id, mapping), now),
            binding.prepare("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)")])
    commands.extend([binding.prepare("UPDATE app_state SET payload=?,updated_at=? WHERE name=? AND payload=?")
        .bind(encode_record("users", owner_id, user), now, user_name, user_snapshot),
        binding.prepare("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"),
        binding.prepare("DELETE FROM d1_command_guard")])
    await binding.batch(commands)


async def _write_user(binding: Any, user: dict, now: int, expected_user: dict | None = None,
                      *, github_identity: tuple[str, str | None] | None = None) -> None:
    owner_id = user["id"]
    name = record_name("users", owner_id)
    snapshot = await read_record_json(binding, "users", owner_id)
    current = json.loads(snapshot) if snapshot is not None else {}
    if not isinstance(current, dict):
        raise ValueError("invalid user state")
    if expected_user is not None and current != expected_user:
        raise ValueError("ACCOUNT_CHANGED")
    commands = []
    if github_identity is not None:
        github_id, identity_snapshot = github_identity
        identity_name = record_name("githubIdentities", github_id)
        if await read_record_json(binding, "githubIdentities", github_id) != identity_snapshot:
            raise ValueError("GITHUB_IDENTITY_CONFLICT")
        if identity_snapshot is None:
            predicate, params = "NOT EXISTS(SELECT 1 FROM app_state WHERE name=?)", (identity_name,)
        else:
            predicate, params = "EXISTS(SELECT 1 FROM app_state WHERE name=? AND payload=?)", (identity_name, identity_snapshot)
        commands.append(binding.prepare("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN "
            + predicate + " THEN 1 ELSE 0 END)").bind(*params))
    next_payload = encode_record("users", owner_id, user)
    if snapshot is not None:
        command = binding.prepare("""UPDATE app_state SET payload=?,updated_at=?
            WHERE name=? AND payload=?""").bind(next_payload, now, name, snapshot)
    else:
        command = binding.prepare("""INSERT INTO app_state(name,payload,updated_at)
            SELECT ?,?,? WHERE NOT EXISTS(SELECT 1 FROM app_state WHERE name=?)""").bind(
                name, next_payload, now, name)
    await binding.batch([*commands, command,
        binding.prepare("INSERT INTO d1_command_guard(ok) VALUES(CASE WHEN changes()=1 THEN 1 ELSE 0 END)"),
        binding.prepare("DELETE FROM d1_command_guard")])


def _oauth_url(client_id: str, callback_url: str, state: str, verifier: str) -> str:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return "https://github.com/login/oauth/authorize?" + urlencode({
        "client_id": client_id, "redirect_uri": callback_url, "scope": "read:user user:email",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })


def _installation_account(value: object) -> dict | None:
    """Only complete current installation metadata can identify an Organization."""
    if not isinstance(value, dict):
        return None
    account_id, login, kind = value.get("id"), value.get("login"), value.get("type")
    if (type(account_id) is not int or not 1 <= account_id <= 9007199254740991
            or not isinstance(login, str) or not 1 <= len(login) <= 100
            or kind not in {"User", "Organization"}):
        return None
    from .json_input import validate_json_unicode
    validate_json_unicode(login)
    return {"id": account_id, "login": login, "type": kind}


def _repo_items(rows: object, installation_id: int, account: dict | None = None) -> list[dict]:
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("invalid repository list")
    items = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid repository")
        repo_id, name = row.get("id"), row.get("full_name")
        if (type(repo_id) is not int or not 1 <= repo_id <= 9007199254740991
                or not isinstance(name, str) or "/" not in name):
            raise ValueError("invalid repository")
        from .json_input import validate_json_unicode
        validate_json_unicode(name)
        if repo_id not in seen:
            seen.add(repo_id)
            items.append({"id": str(repo_id), "githubRepoId": repo_id,
                          "fullName": name[:300], "installationId": str(installation_id),
                          "account": account})
    return items


async def read_repository_access(user: dict, gateway: Any, now: int | None = None) -> dict:
    from .cloudflare_github_gateway import GitHubFailure
    from .cloudflare_github_refresh import refresh_required
    user = user.get("_actor", user)
    try:
        return await _read_repository_access(user, gateway, now)
    except GitHubFailure as error:
        if error.code == "GITHUB_REAUTHORIZATION_REQUIRED":
            return {"items": [], "githubAccess": "reauthorization_required",
                    **({"githubRefreshRequired": True} if refresh_required(user, now) else {})}
        if error.code == "GITHUB_PERMISSION_DENIED":
            return {"items": [], "githubAccess": "lost"}
        raise


async def _read_repository_access(user: dict, gateway: Any, now: int | None = None) -> dict:
    """Read current App grants without relying on a prior Setup callback.

    The App user token is the authority. GET/sync never write cached grants or
    trust caller-provided repository IDs. Aggregate at most ten installations
    and 1,000 repositories, using the gateway's bounded pagination.
    """
    access = user.get("githubRepositoryAccess")
    missing = "lost" if isinstance(access, dict) and access.get("status") == "authorized" else "not_connected"
    if not user.get("githubAccessToken"):
        return {"items": [], "githubAccess": missing}
    from .cloudflare_github_refresh import CLAIM_SECONDS, REFRESH_MARGIN, refresh_required
    expiry = user.get("githubAccessTokenExpiresAt")
    if (type(now) is int and type(expiry) is int and expiry <= now + REFRESH_MARGIN
            and "githubTokenRefresh" in user):
        from .cloudflare_github_gateway import GitHubFailure
        claim = user["githubTokenRefresh"]
        started = claim.get("startedAt") if isinstance(claim, dict) else None
        if type(started) is int and started <= now < started + CLAIM_SECONDS:
            raise GitHubFailure("GITHUB_UNAVAILABLE")
        return {"items": [], "githubAccess": "reauthorization_required"}
    if refresh_required(user, now):
        return {"items": [], "githubAccess": "reauthorization_required", "githubRefreshRequired": True}
    token = await gateway.unseal(user["githubAccessToken"])
    installations = await gateway.installations(token)
    if not isinstance(installations, list) or len(installations) > 10:
        raise ValueError("invalid or excessive GitHub installations")
    items, seen, installation_ids, organizations = [], set(), set(), {}
    for installation in installations:
        installation_id = installation.get("id") if isinstance(installation, dict) else None
        if type(installation_id) is not int or not 1 <= installation_id <= 9007199254740991:
            raise ValueError("invalid GitHub installation")
        if installation_id in installation_ids:
            continue
        installation_ids.add(installation_id)
        account = _installation_account(installation.get("account"))
        if account is not None and account["type"] == "Organization":
            organizations[account["id"]] = account
        rows = _repo_items(await gateway.repositories(token, installation_id), installation_id, account)
        for item in rows:
            if item["githubRepoId"] not in seen:
                seen.add(item["githubRepoId"])
                items.append(item)
        if len(items) > 1000:
            raise ValueError("excessive authorized repositories")
    return {"items": items, "githubAccess": "authorized" if items else missing,
            "organizations": list(organizations.values())}


async def handle_identity_request(*, binding: Any, gateway: Any, now: int,
                                  method: str, path: str, params: Mapping[str, object],
                                  headers: Mapping[str, object], app_url: str,
                                  callback_url: str, cookie_same_site: str,
                                  trusted_origins: set[str]):
    """Return (status, payload, headers), or None for another HTTP domain."""
    paths = {"/auth/session", "/auth/sign-out", "/auth/github/authorize",
             "/auth/github/callback", "/integrations", "/integrations/github/authorize",
             "/integrations/github/callback", "/integrations/github/refresh", "/repositories", "/repositories/sync", "/api/v1/repositories"}
    if path not in paths:
        return None
    no_store = {"Cache-Control": "no-store"}
    if path == "/integrations/github/refresh":
        if method != "POST":
            return 405, {"error": {"code": "METHOD_NOT_ALLOWED"}}, no_store
        origin = urlsplit(_header(headers, "Origin") or _header(headers, "Referer"))
        if f"{origin.scheme}://{origin.netloc}" not in trusted_origins:
            return 403, {"error": {"code": "UNTRUSTED_ORIGIN"}}, no_store
        session, user = await _session_user(binding, headers, now)
        if not session:
            return 401, {"error": {"code": "UNAUTHENTICATED"}}, no_store
        from .cloudflare_github_refresh import handle_github_refresh
        return await handle_github_refresh(binding=binding, gateway=gateway, session=session, user=user, now=now)
    if method == "GET" and path == "/auth/session":
        session, user = await _session_user(binding, headers, now)
        return 200, session_payload(user if session else None), no_store
    if method == "POST" and path == "/auth/sign-out":
        origin = _header(headers, "Origin") or _header(headers, "Referer")
        parsed = urlsplit(origin)
        if f"{parsed.scheme}://{parsed.netloc}" not in trusted_origins:
            return 403, {"error": {"code": "UNTRUSTED_ORIGIN"}}, no_store
        session, user = await _session_user(binding, headers, now)
        if not session:
            return 401, {"error": {"code": "UNAUTHENTICATED"}}, no_store
        await D1SessionTransactions(binding).revoke_session(
            owner_id=user["id"], session_id=session["id"], now=now)
        return 200, {"ok": True}, {**no_store, "Set-Cookie": _session_cookie("", clear=True, same_site=cookie_same_site)}
    if method == "GET" and path == "/auth/github/authorize":
        intent = _param(params, "intent")
        if intent not in {"", "login", "link"}:
            return 422, {"error": {"code": "INVALID_INPUT"}}, no_store
        link_session, link_user = None, None
        if intent == "link":
            origin = urlsplit(_header(headers, "Origin") or _header(headers, "Referer"))
            if f"{origin.scheme}://{origin.netloc}" not in trusted_origins:
                return 403, {"error": {"code": "UNTRUSTED_ORIGIN"}}, no_store
            link_session, link_user = await _session_user(binding, headers, now)
            if not link_session:
                return 401, {"error": {"code": "UNAUTHENTICATED"}}, no_store
        if not gateway.client_id:
            return 503, {"error": {"code": "GITHUB_NOT_CONFIGURED"}}, no_store
        destination = _redirect(_param(params, "redirectTo"), app_url, "/projects")
        verifier, state = _random_urlsafe(32), _random_urlsafe(32)
        state_record = {"kind": "login", "redirectTo": destination, "codeVerifier": verifier,
                        "expiresAt": now + 600}
        if intent == "link":
            state_record.update(intent="link", userId=link_user["id"], sessionId=link_session["id"])
        await D1OAuthStates(binding).issue(state_id=state, record=state_record, now=now)
        url = _oauth_url(gateway.client_id, callback_url, state, verifier)
        return (_redirect_result(url) if _param(params, "response") == "redirect"
                else (200, {"url": url, "mode": "github"}, no_store))
    if method == "GET" and path == "/auth/github/callback":
        state = _param(params, "state")
        try:
            record = await D1OAuthStates(binding).consume(
                state_id=state, expected_kind="login", now=now)
        except ValueError:
            return 400, {"error": {"code": "OAUTH_STATE_INVALID"}}, no_store
        linking = record.get("intent") == "link"
        if linking:
            session, linked_user = await _session_user(binding, headers, now)
            if not session or session["id"] != record.get("sessionId") or linked_user["id"] != record.get("userId"):
                return 401, {"error": {"code": "UNAUTHENTICATED"}}, no_store
        destination = _redirect(record.get("redirectTo", ""), app_url, "/projects")
        if _param(params, "error") or not _param(params, "code"):
            parsed = urlsplit(destination)
            error_query = urlencode({"github_error": _param(params, "error") or "missing_oauth_code"})
            query = parsed.query + ("&" if parsed.query else "") + error_query
            return _redirect_result(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment)))
        bundle = await gateway.exchange(_param(params, "code"), callback_url, record["codeVerifier"])
        profile = await gateway.profile(bundle.access_token)
        github_id, login = profile.get("id"), profile.get("login")
        if type(github_id) is not int or not 1 <= github_id <= 9007199254740991 or not isinstance(login, str) or not login:
            return 502, {"error": {"code": "GITHUB_PROFILE_INVALID"}}, no_store
        if linking:
            session, existing = await _session_user(binding, headers, now)
            if not session or session["id"] != record.get("sessionId") or existing["id"] != record.get("userId"):
                return 401, {"error": {"code": "UNAUTHENTICATED"}}, no_store
            owner_id = existing["id"]
        else:
            try:
                owner_id, identity_snapshot = await _github_login_identity(binding, str(github_id))
            except ValueError:
                return 409, {"error": {"code": "GITHUB_IDENTITY_CONFLICT"}}, no_store
            existing = await _user(binding, owner_id) or {}
        _observe_authenticated_actor(binding, owner_id)
        name = (existing.get("name") if has_verified_email_identity(existing) and existing.get("name")
                else str(profile.get("name") or login)[:200])
        from .cloudflare_github_refresh import TOKEN_FIELDS, sealed_token_fields
        fields = await sealed_token_fields(bundle, gateway, now)
        user = {**{key: value for key, value in existing.items() if key not in TOKEN_FIELDS}, "id": owner_id, "name": name,
                "avatarUrl": str(profile.get("avatar_url") or "")[:500],
                "createdAt": existing.get("createdAt", now), "providers": _provider_union(existing, "github"),
                "githubId": str(github_id), "githubLogin": login[:100], **fields}
        if linking:
            try:
                await _write_linked_user(binding, user=user, expected_user=existing,
                    session=session, github_id=str(github_id), now=now)
            except ValueError as failure:
                code = str(failure)
                if code not in {"GITHUB_IDENTITY_CONFLICT", "ACCOUNT_CHANGED", "UNAUTHENTICATED"}:
                    raise
                return (401 if code == "UNAUTHENTICATED" else 409), {"error": {"code": code}}, no_store
            return _redirect_result(destination)
        try:
            await _write_user(binding, user, now, expected_user=existing,
                              github_identity=(str(github_id), identity_snapshot))
        except ValueError as failure:
            code = str(failure)
            if code not in {"GITHUB_IDENTITY_CONFLICT", "ACCOUNT_CHANGED"}:
                raise
            return 409, {"error": {"code": code}}, no_store
        authority = await binding.prepare("SELECT owner_id FROM account_entitlement_authority WHERE owner_id=?").bind(owner_id).first()
        if authority is None:
            try:
                await D1AccountTransactions(binding).initialize_account(owner_id=owner_id, now=now)
            except Exception:
                # A concurrent callback may have inserted the same account.
                authority = await binding.prepare("SELECT owner_id FROM account_entitlement_authority WHERE owner_id=?").bind(owner_id).first()
                if authority is None:
                    raise
        session_id = "ses-" + _random_urlsafe(32)
        await D1SessionTransactions(binding).issue_session(
            owner_id=owner_id, session_id=session_id, now=now, expires_at=now + SESSION_AGE)
        return _redirect_result(destination, _session_cookie(session_id, same_site=cookie_same_site))
    if method == "GET" and path == "/integrations/github/authorize":
        session, user = await _session_user(binding, headers, now)
        if not session:
            return 401, {"error": {"code": "UNAUTHENTICATED"}}, no_store
        if not user.get("githubId") or not user.get("githubAccessToken"):
            return 409, {"error": {"code": "GITHUB_IDENTITY_REQUIRED"}}, no_store
        if not gateway.app_slug:
            return 503, {"error": {"code": "GITHUB_APP_NOT_CONFIGURED"}}, no_store
        destination = _redirect(_param(params, "redirectTo"), app_url, "/projects")
        state = _random_urlsafe(32)
        await D1OAuthStates(binding).issue(state_id=state,
            record={"kind": "install", "userId": user["id"], "sessionId": session["id"],
                    "redirectTo": destination, "expiresAt": now + 600}, now=now)
        url = f"https://github.com/apps/{gateway.app_slug}/installations/new?" + urlencode({"state": state})
        return 200, {"url": url, "mode": "github-app"}, no_store
    if method == "GET" and path == "/integrations/github/callback":
        try:
            record = await D1OAuthStates(binding).consume(
                state_id=_param(params, "state"), expected_kind="install", now=now)
        except ValueError:
            return 400, {"error": {"code": "OAUTH_STATE_INVALID"}}, no_store
        session, user = await _session_user(binding, headers, now)
        if not session or session["id"] != record.get("sessionId") or user["id"] != record.get("userId"):
            return 401, {"error": {"code": "UNAUTHENTICATED"}}, no_store
        if not user.get("githubId") or not user.get("githubAccessToken"):
            return 409, {"error": {"code": "GITHUB_IDENTITY_REQUIRED"}}, no_store
        installation_text = _param(params, "installation_id")
        if not installation_text.isdigit() or int(installation_text) <= 0:
            return 422, {"error": {"code": "INVALID_INSTALLATION"}}, no_store
        installation_id = int(installation_text)
        token = await gateway.unseal(user["githubAccessToken"])
        installations = await gateway.installations(token)
        if not isinstance(installations, list) or not any(
                isinstance(item, dict) and item.get("id") == installation_id for item in installations):
            return 403, {"error": {"code": "INSTALLATION_NOT_AUTHORIZED"}}, no_store
        installation = next(item for item in installations if isinstance(item, dict)
                            and item.get("id") == installation_id)
        items = _repo_items(await gateway.repositories(token, installation_id), installation_id,
                            _installation_account(installation.get("account")))
        next_user = {**user, "githubRepositoryAccess": {
            "mode": "github-app", "status": "authorized", "authorizedUserId": user["id"],
            "authorizedGithubId": user["githubId"], "authorizedGithubLogin": user["githubLogin"],
            "installationId": installation_text, "repositoryItems": items,
            "repositoriesNeedSync": False, "authorizedAt": now}}
        await _write_user(binding, next_user, now, expected_user=user)
        return _redirect_result(_redirect(record.get("redirectTo", ""), app_url, "/projects"))
    if (method == "GET" and path in {"/repositories", "/integrations", "/api/v1/repositories"}
            or method == "POST" and path == "/repositories/sync"):
        if method == "POST":
            origin = urlsplit(_header(headers, "Origin") or _header(headers, "Referer"))
            if f"{origin.scheme}://{origin.netloc}" not in trusted_origins:
                return 403, {"error": {"code": "UNTRUSTED_ORIGIN"}}, no_store
        if path == "/api/v1/repositories":
            try:
                user, _, auth, validate = await ledger_principal(
                    binding=binding, headers=headers, scope="projects:read", now=now)
                snapshot = await binding.batch(auth)
                validate([part.results for part in snapshot])
            except PrincipalAuthError as failure:
                return failure.status, {"error": {"code": failure.code}}, no_store
        else:
            _, user = await _session_user(binding, headers, now)
        if not user:
            return 401, {"error": {"code": "UNAUTHENTICATED"}}, no_store
        access = user.get("githubRepositoryAccess")
        result = await read_repository_access(user, gateway, now)
        if _header(headers, "Authorization") or _header(headers, "X-Pullwise-Api-Key"):
            result.pop("githubRefreshRequired", None)
        if path == "/repositories/sync":
            result = {**result, "needsAuthorization": result["githubAccess"] != "authorized"}
        if path == "/api/v1/repositories":
            result = {"items": result["items"], "nextCursor": None,
                      "githubAccess": result["githubAccess"],
                      "organizations": result.get("organizations", []),
                      **({"githubRefreshRequired": True} if result.get("githubRefreshRequired") else {})}
        if path == "/integrations":
            refresh = result.get("githubRefreshRequired") is True
            result = {"github": {"connected": result["githubAccess"] == "authorized",
                                 "authorizationPending": False, "mode": "github-app" if access else None,
                                 "repositories": [item["fullName"] for item in result["items"]]},
                      "items": [], **({"githubRefreshRequired": True} if refresh else {})}
            result["items"] = [result["github"]]
        return 200, result, no_store
    return 404, {"error": {"code": "NOT_FOUND"}}, no_store
